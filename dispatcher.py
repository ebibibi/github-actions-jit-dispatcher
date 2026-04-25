#!/usr/bin/env python3
"""GitHub Actions JIT Runner Dispatcher

Monitors repositories for queued GitHub Actions jobs and spawns
ephemeral JIT runners in Docker containers on demand.

Configuration via environment variables:
  GITHUB_TOKEN          GitHub PAT with repo + actions scope (required)
  GITHUB_OWNER          GitHub username or org (required)
  RUNNER_IMAGE          Docker image for runners (default: ghactions-runner:latest)
  MAX_CONCURRENT        Max parallel runners (default: 10)
  REPOS_REFRESH_SEC     Repo discovery interval in seconds (default: 300)
  API_BUDGET_PER_HOUR   GitHub API calls budget per hour (default: 4000)
  RUNNER_LABELS         Comma-separated runner labels (default: self-hosted,linux,x64)
  DOCKER_SOCKET         Path to Docker socket (default: /var/run/docker.sock)
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
REPOS_REFRESH_INTERVAL = int(os.environ.get("REPOS_REFRESH_SEC", "300"))
API_CALLS_PER_HOUR_BUDGET = int(os.environ.get("API_BUDGET_PER_HOUR", "4000"))
RUNNER_LABELS = os.environ.get("RUNNER_LABELS", "self-hosted,linux,x64").split(",")
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
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


def load_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log.error("GITHUB_TOKEN environment variable is required")
        sys.exit(1)
    return token


def github_api(method: str, path: str, data: dict | None = None):
    token = load_token()
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            log.warning("Rate limit or permission denied: %s %s -> %d", method, path, e.code)
        else:
            log.warning("API %s %s -> %d", method, path, e.code)
        return None
    except Exception as e:
        log.error("API error: %s", e)
        return None


def discover_repos() -> list[str]:
    global cached_repos, repos_last_refreshed

    now = time.time()
    if cached_repos and now - repos_last_refreshed < REPOS_REFRESH_INTERVAL:
        return cached_repos

    all_repos: list[dict] = []
    page = 1
    while True:
        result = github_api("GET", f"/user/repos?per_page=100&affiliation=owner&page={page}")
        if not result or not isinstance(result, list):
            break
        all_repos.extend(result)
        if len(result) < 100:
            break
        page += 1

    if not all_repos:
        log.warning("Failed to list repos, keeping previous cache")
        return cached_repos

    repos_with_workflows: list[str] = []
    for repo in all_repos:
        name = repo["name"]
        wf = github_api("GET", f"/repos/{OWNER}/{name}/actions/workflows?per_page=1")
        if wf and isinstance(wf, dict) and wf.get("total_count", 0) > 0:
            repos_with_workflows.append(name)

    if repos_with_workflows:
        if set(repos_with_workflows) != set(cached_repos):
            log.info(
                "Repos with workflows (%d): %s",
                len(repos_with_workflows),
                ", ".join(sorted(repos_with_workflows)),
            )
        cached_repos = repos_with_workflows
        repos_last_refreshed = now
    else:
        log.warning("No repos with workflows found, keeping previous cache")

    return cached_repos


def calc_poll_interval(num_repos: int) -> int:
    if num_repos == 0:
        return MIN_POLL_INTERVAL
    return max(MIN_POLL_INTERVAL, math.ceil(3600 * (num_repos + 2) / API_CALLS_PER_HOUR_BUDGET))


def get_active_runs(repo: str) -> list[dict]:
    result = github_api("GET", f"/repos/{OWNER}/{repo}/actions/runs?per_page=10")
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


def spawn_runner(jit_config: dict, repo: str) -> bool:
    encoded = jit_config.get("encoded_jit_config", "")
    runner_info = jit_config.get("runner", {})
    name = runner_info.get("name", f"jit-{int(time.time())}")

    cmd = [
        "docker", "run", "--rm",
        "--name", name,
        "-e", f"JIT_CONFIG={encoded}",
        "-v", f"{DOCKER_SOCKET}:/var/run/docker.sock",
        RUNNER_IMAGE,
    ]

    log.info("Spawning container %s for %s", name, repo)
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
    cleanup_finished()
    prune_spawned_jobs()

    with runner_lock:
        total_active = len(active_runners)

    for repo in repos:
        if total_active >= MAX_CONCURRENT:
            break

        active_runs = get_active_runs(repo)
        for run in active_runs:
            run_id = run["id"]
            pending_jobs = get_pending_jobs(repo, run_id)

            for job in pending_jobs:
                job_id = job["id"]
                if job_id in spawned_jobs:
                    continue
                if total_active >= MAX_CONCURRENT:
                    break
                jit = generate_jit_config(repo)
                if jit and spawn_runner(jit, repo):
                    spawned_jobs[job_id] = time.time()
                    total_active += 1


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
