#!/usr/bin/env python3
"""GitHub Actions JIT Runner Dispatcher

Monitors repositories for queued GitHub Actions jobs and spawns
ephemeral JIT runners in Docker containers on demand.

Configuration via environment variables:
  GITHUB_TOKEN          GitHub PAT with repo + actions scope (required)
  GITHUB_OWNER          GitHub username or org (required)
  RUNNER_IMAGE          Docker image for runners (default: ghactions-runner:latest)
  MAX_CONCURRENT        Max parallel runners (default: 10)
  REPOS_REFRESH_SEC     Repo discovery interval in seconds (default: 3600)
  API_BUDGET_PER_HOUR   GitHub API calls budget per hour for polling (default: 1500)
  RATE_LIMIT_RESERVE    Pause all calls until the rate-limit reset once fewer
                        than this many requests remain (default: 1000). The
                        limit is shared with every other tool on the account.
  MAX_PER_REPO_PER_CYCLE Max runners spawned per repo in one poll cycle (default: 1)
  RUNNER_LABELS         Comma-separated runner labels (default: self-hosted,linux,x64)
  DOCKER_SOCKET         Path to Docker socket (default: /var/run/docker.sock)
  DOCKER_SOCKET_REPOS   Comma-separated repos allowed to mount the Docker
                        socket (default: none). Mounting the socket grants
                        root on the host, so it is denied unless listed.
  RUNNER_MEMORY         Memory cap per runner container (default: 8g)
  RUNNER_CPUS           CPU cap per runner container (default: 4)
  RUNNER_PIDS_LIMIT     Process cap per runner container (default: 2048)
"""

import json
import logging
import math
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from threading import Lock

OWNER = os.environ.get("GITHUB_OWNER", "")
RUNNER_IMAGE = os.environ.get("RUNNER_IMAGE", "ghactions-runner:latest")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "10"))
REPOS_REFRESH_INTERVAL = int(os.environ.get("REPOS_REFRESH_SEC", "3600"))
API_CALLS_PER_HOUR_BUDGET = int(os.environ.get("API_BUDGET_PER_HOUR", "1500"))
RATE_LIMIT_RESERVE = int(os.environ.get("RATE_LIMIT_RESERVE", "1000"))
RUNNER_LABELS = os.environ.get("RUNNER_LABELS", "self-hosted,linux,x64").split(",")
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
# Mounting the host Docker socket into a runner is equivalent to giving that
# job root on the host: any workflow step can `docker run -v /:/host`. Public
# repositories accept pull requests from forks, and a fork's `pull_request`
# workflow runs code the fork controls, so the socket must never be the
# default. Only repositories that genuinely build images are listed here, and
# they must be ones that do not accept outside pull requests.
DOCKER_SOCKET_REPOS = frozenset(
    name.strip()
    for name in os.environ.get("DOCKER_SOCKET_REPOS", "").split(",")
    if name.strip()
)
RUNNER_MEMORY = os.environ.get("RUNNER_MEMORY", "8g")
RUNNER_CPUS = os.environ.get("RUNNER_CPUS", "4")
RUNNER_PIDS_LIMIT = os.environ.get("RUNNER_PIDS_LIMIT", "2048")
MAX_PER_REPO_PER_CYCLE = int(os.environ.get("MAX_PER_REPO_PER_CYCLE", "1"))
MIN_POLL_INTERVAL = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("jit-dispatcher")

running = True
active_runners: dict[int, dict] = {}
runner_lock = Lock()
spawned_jobs: dict[int, float] = {}
cached_repos: list[str] = []
repos_last_refreshed: float = 0
# Rotating scan offset for poll_cycle, advanced each cycle to keep repos fair
poll_offset: int = 0
# Epoch seconds until which no API call is made (rate limit exhausted or low)
rate_limited_until: float = 0
# repo name -> (pushed_at, targets self-hosted). Lets discovery skip repos that
# have not been pushed since their workflows were last read.
repo_scan_cache: dict[str, tuple[str, bool]] = {}
# HTTP status of the last failed API call (None after a success)
last_error_status: int | None = None


def load_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log.error("GITHUB_TOKEN environment variable is required")
        sys.exit(1)
    return token


def _header_int(headers, name: str) -> int | None:
    value = headers.get(name) if headers is not None else None
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def note_rate_limit(headers, status: int | None = None) -> None:
    """Pause every API call until the reset time when the limit is low or hit.

    The REST limit belongs to the account, not to this process: gh CLI and
    other scripts draw from the same 5,000 requests/hour. Stopping at a reserve
    leaves them room, and stopping on a rejection avoids hammering the API with
    requests that can only fail until the window resets.
    """
    global rate_limited_until

    remaining = _header_int(headers, "X-RateLimit-Remaining")
    reset = _header_int(headers, "X-RateLimit-Reset")
    retry_after = _header_int(headers, "Retry-After")
    now = time.time()

    until = 0.0
    if retry_after is not None and status in (403, 429):
        until = now + retry_after
    elif remaining is not None and reset is not None:
        if remaining == 0 or (remaining < RATE_LIMIT_RESERVE and status is None):
            until = reset + 1

    if until > rate_limited_until:
        rate_limited_until = until
        log.warning(
            "GitHub rate limit low (remaining=%s); pausing API calls for %.0fs",
            remaining,
            until - now,
        )


def rate_limit_pause() -> float:
    """Seconds left before API calls may resume (0 when not paused)."""
    return max(0.0, rate_limited_until - time.time())


def github_api(method: str, path: str, data: dict | None = None, raw: bool = False):
    global last_error_status
    last_error_status = None
    if rate_limit_pause() > 0:
        last_error_status = 429
        return None
    token = load_token()
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            note_rate_limit(resp.headers)
            content = resp.read()
            return content.decode("utf-8", "replace") if raw else json.loads(content)
    except urllib.error.HTTPError as e:
        last_error_status = e.code
        note_rate_limit(e.headers, e.code)
        if e.code == 404:
            log.debug("API %s %s -> 404", method, path)
        elif e.code in (403, 429):
            log.warning("Rate limit or permission denied: %s %s -> %d", method, path, e.code)
        else:
            log.warning("API %s %s -> %d", method, path, e.code)
        return None
    except Exception as e:
        last_error_status = -1
        log.error("API error: %s", e)
        return None


def workflow_targets_self_hosted(text: str, label: str) -> bool:
    """True when a workflow file mentions the self-hosted label outside comments."""
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        if label in code:
            return True
    return False


def repo_targets_self_hosted(repo: str) -> bool | None:
    """Whether any workflow in the repo targets our runners; None if unknown."""
    listing = github_api("GET", f"/repos/{OWNER}/{repo}/contents/.github/workflows")
    if listing is None:
        # No workflows directory means no jobs; any other failure is unknown
        # and must not be cached as "no self-hosted jobs".
        return False if last_error_status == 404 else None
    if not isinstance(listing, list):
        return False
    for entry in listing:
        name = entry.get("name", "")
        if entry.get("type") != "file" or not name.endswith((".yml", ".yaml")):
            continue
        text = github_api("GET", f"/repos/{OWNER}/{repo}/contents/{entry['path']}", raw=True)
        if text is None:
            return None
        if workflow_targets_self_hosted(text, RUNNER_LABELS[0]):
            return True
    return False


def discover_repos() -> list[str]:
    """Return the repos whose workflows target self-hosted runners.

    Repos that only use GitHub-hosted runners can never queue a job for us, so
    polling them wastes the shared rate limit. Workflow files are re-read only
    for repos pushed since the last scan.
    """
    global cached_repos, repos_last_refreshed

    now = time.time()
    if repos_last_refreshed and now - repos_last_refreshed < REPOS_REFRESH_INTERVAL:
        return cached_repos

    all_repos: list[dict] = []
    page = 1
    while True:
        result = github_api("GET", f"/user/repos?per_page=100&affiliation=owner&page={page}")
        if not result or not isinstance(result, list):
            if result is None:
                log.warning("Failed to list repos, keeping previous cache")
                return cached_repos
            break
        all_repos.extend(result)
        if len(result) < 100:
            break
        page += 1

    selected: list[str] = []
    for repo in all_repos:
        if repo.get("archived") or repo.get("disabled"):
            continue
        name = repo["name"]
        pushed_at = repo.get("pushed_at") or ""
        cached = repo_scan_cache.get(name)
        if cached and cached[0] == pushed_at:
            uses = cached[1]
        else:
            uses = repo_targets_self_hosted(name)
            if uses is None:
                log.warning("Discovery interrupted at %s, keeping previous cache", name)
                return cached_repos
            repo_scan_cache[name] = (pushed_at, uses)
        if uses:
            selected.append(name)

    if set(selected) != set(cached_repos):
        log.info(
            "Repos targeting self-hosted runners (%d of %d): %s",
            len(selected),
            len(all_repos),
            ", ".join(sorted(selected)),
        )
    cached_repos = selected
    repos_last_refreshed = now
    return cached_repos


def calc_poll_interval(num_repos: int) -> int:
    if num_repos == 0:
        return MIN_POLL_INTERVAL
    return max(MIN_POLL_INTERVAL, math.ceil(3600 * (num_repos + 2) / API_CALLS_PER_HOUR_BUDGET))


def get_active_runs(repo: str) -> list[dict]:
    result = github_api("GET", f"/repos/{OWNER}/{repo}/actions/runs?per_page=50")
    if result and isinstance(result, dict):
        return [
            r for r in result.get("workflow_runs", [])
            if r["status"] in ("queued", "in_progress")
        ]
    return []


def get_pending_jobs(repo: str, run_id: int) -> list[dict]:
    result = github_api("GET", f"/repos/{OWNER}/{repo}/actions/runs/{run_id}/jobs?per_page=30")
    if result and isinstance(result, dict):
        return [
            j for j in result.get("jobs", [])
            if j["status"] == "queued" and RUNNER_LABELS[0] in j.get("labels", [])
        ]
    return []


def generate_jit_config(repo: str) -> dict | None:
    data = {
        "name": f"jit-{repo}-{int(time.time())}",
        "runner_group_id": 1,
        "labels": RUNNER_LABELS,
        "work_folder": "_work",
    }
    return github_api("POST", f"/repos/{OWNER}/{repo}/actions/runners/generate-jitconfig", data)


def build_run_command(name: str, encoded_jit_config: str, repo: str) -> list[str]:
    """Build the docker run argv for one ephemeral runner.

    The Docker socket is mounted only for repositories on the allow list. Every
    container is also capped so that a runaway or hostile job cannot exhaust
    the host's memory, CPU or process table.
    """
    cmd = [
        "docker", "run", "--rm",
        "--name", name,
        "--memory", RUNNER_MEMORY,
        "--cpus", RUNNER_CPUS,
        "--pids-limit", RUNNER_PIDS_LIMIT,
        "-e", f"JIT_CONFIG={encoded_jit_config}",
    ]
    if repo in DOCKER_SOCKET_REPOS:
        cmd += ["-v", f"{DOCKER_SOCKET}:/var/run/docker.sock"]
    cmd.append(RUNNER_IMAGE)
    return cmd


def spawn_runner(jit_config: dict, repo: str) -> bool:
    encoded = jit_config.get("encoded_jit_config", "")
    runner_info = jit_config.get("runner", {})
    name = runner_info.get("name", f"jit-{int(time.time())}")

    cmd = build_run_command(name, encoded, repo)

    log.info(
        "Spawning container %s for %s (docker socket: %s)",
        name,
        repo,
        "mounted" if repo in DOCKER_SOCKET_REPOS else "denied",
    )
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except Exception as e:
        log.error("Failed to spawn container %s: %s", name, e)
        return False

    with runner_lock:
        active_runners[proc.pid] = {
            "repo": repo,
            "name": name,
            "process": proc,
            "started": time.time(),
        }
    return True


def cleanup_finished():
    finished = []
    with runner_lock:
        for pid, info in active_runners.items():
            proc = info["process"]
            if proc.poll() is not None:
                finished.append((pid, info))

    for pid, info in finished:
        rc = info["process"].returncode
        elapsed = time.time() - info["started"]
        log.info(
            "Container %s finished (repo=%s, rc=%d, %.0fs)",
            info["name"],
            info["repo"],
            rc,
            elapsed,
        )
        with runner_lock:
            del active_runners[pid]


def prune_spawned_jobs():
    now = time.time()
    expired = [jid for jid, ts in spawned_jobs.items() if now - ts > 300]
    for jid in expired:
        del spawned_jobs[jid]


def poll_cycle(repos: list[str]):
    global poll_offset

    cleanup_finished()
    prune_spawned_jobs()

    with runner_lock:
        total_active = len(active_runners)

    # Scanning in a fixed order every cycle lets repos that continuously produce
    # jobs hold all MAX_CONCURRENT slots, so repos later in the list never get
    # served. Rotate the starting offset so every repo takes its turn at the front.
    if repos:
        poll_offset %= len(repos)
        repos = repos[poll_offset:] + repos[:poll_offset]
        poll_offset += 1

    for repo in repos:
        if total_active >= MAX_CONCURRENT or rate_limit_pause() > 0:
            break

        # A repo with a burst of queued jobs (a batch of Dependabot PRs, say)
        # would otherwise claim every free slot in a single cycle and keep doing
        # so on the next one. Take at most one slot per repo per cycle so the
        # capacity is spread across repos.
        spawned_here = 0

        active_runs = get_active_runs(repo)
        for run in active_runs:
            if spawned_here >= MAX_PER_REPO_PER_CYCLE:
                break
            run_id = run["id"]
            pending_jobs = get_pending_jobs(repo, run_id)

            for job in pending_jobs:
                job_id = job["id"]
                if job_id in spawned_jobs:
                    continue
                if total_active >= MAX_CONCURRENT or spawned_here >= MAX_PER_REPO_PER_CYCLE:
                    break
                jit = generate_jit_config(repo)
                if jit and spawn_runner(jit, repo):
                    spawned_jobs[job_id] = time.time()
                    total_active += 1
                    spawned_here += 1


def signal_handler(sig, _frame):
    global running
    log.info("Received signal %d, shutting down...", sig)
    running = False


def main():
    global running

    if not OWNER:
        log.error("GITHUB_OWNER environment variable is required")
        sys.exit(1)

    load_token()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    log.info(
        "JIT Dispatcher started (owner=%s, max_concurrent=%d, image=%s, labels=%s)",
        OWNER, MAX_CONCURRENT, RUNNER_IMAGE, RUNNER_LABELS,
    )

    while running:
        pause = rate_limit_pause()
        if pause > 0:
            cleanup_finished()
            time.sleep(min(pause, 60))
            continue
        try:
            repos = discover_repos()
            poll_interval = calc_poll_interval(len(repos))
            poll_cycle(repos)
        except Exception as e:
            log.error("Poll cycle error: %s", e, exc_info=True)
            poll_interval = MIN_POLL_INTERVAL
        time.sleep(poll_interval)

    log.info("Waiting for active containers to finish...")
    with runner_lock:
        for info in active_runners.values():
            info["process"].wait(timeout=300)
    log.info("Dispatcher stopped.")


if __name__ == "__main__":
    main()
