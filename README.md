# GitHub Actions JIT Dispatcher

A lightweight, single-file dispatcher that monitors your GitHub repositories for queued Actions jobs and spawns ephemeral JIT (Just-In-Time) runners inside Docker containers.

## Why?

GitHub Actions' free tier includes 2,000 minutes/month for private repos. If you exceed that, you either pay or self-host. This project lets you run a single dispatcher process on any Linux machine that automatically:

1. **Discovers** all your repos with workflows (no manual config needed)
2. **Detects** queued jobs via the GitHub API
3. **Spawns** ephemeral Docker containers as JIT runners
4. **Cleans up** automatically when jobs finish (`docker run --rm`)

No Kubernetes. No Actions Runner Controller. Just Python + Docker.

## Architecture

```
┌─────────────────────────────────────────────┐
│  dispatcher.py (systemd service)            │
│                                             │
│  Poll GitHub API ──► Queued job found?      │
│                         │                   │
│                         ▼                   │
│              docker run --rm                │
│              ┌──────────────────┐           │
│              │ ghactions-runner │           │
│              │ (ephemeral)      │           │
│              └──────────────────┘           │
│              Container auto-removed on exit  │
└─────────────────────────────────────────────┘
```

**Key design decisions:**

- **JIT runners** — each job gets a fresh runner via GitHub's `generate-jitconfig` API. No persistent registration needed.
- **Docker isolation** — jobs run in containers, not on the host. Clean environment every time.
- **Adaptive polling** — interval auto-adjusts based on repo count to stay within API rate limits: `max(10, ceil(3600 × (N+2) / budget))` seconds.
- **Dynamic repo discovery** — repos are discovered every 5 minutes via the API. Add a workflow to any repo and it's automatically picked up.

## Quick Start

### 1. Create a GitHub Personal Access Token

Create a [fine-grained PAT](https://github.com/settings/tokens?type=beta) with:
- **Repository access**: All repositories (or select specific ones)
- **Permissions**: Actions (Read and Write), Administration (Read and Write)

### 2. Build the runner image

```bash
docker build -t ghactions-runner:latest .
```

You can customize the runner version:

```bash
docker build --build-arg RUNNER_VERSION=2.333.1 -t ghactions-runner:latest .
```

### 3. Run the dispatcher

```bash
export GITHUB_TOKEN="ghp_xxxxxxxxxxxx"
export GITHUB_OWNER="your-username"

python3 dispatcher.py
```

### 4. Update your workflows

Change `runs-on` in your workflow files:

```yaml
jobs:
  build:
    runs-on: [self-hosted, linux, x64]
```

That's it. The dispatcher will detect queued jobs and spawn containers automatically.

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_TOKEN` | Yes | — | GitHub PAT with `repo` + `actions` scope |
| `GITHUB_OWNER` | Yes | — | GitHub username or organization |
| `RUNNER_IMAGE` | No | `ghactions-runner:latest` | Docker image for runners |
| `MAX_CONCURRENT` | No | `10` | Maximum parallel runner containers |
| `REPOS_REFRESH_SEC` | No | `300` | Repo discovery interval (seconds) |
| `API_BUDGET_PER_HOUR` | No | `4000` | GitHub API call budget per hour |
| `RUNNER_LABELS` | No | `self-hosted,linux,x64` | Comma-separated runner labels |
| `DOCKER_SOCKET` | No | `/var/run/docker.sock` | Path to Docker socket |

## Running as a systemd Service

Copy the template and edit it:

```bash
sudo cp jit-dispatcher.service /etc/systemd/system/
sudo systemctl edit jit-dispatcher.service  # add your env vars
sudo systemctl enable --now jit-dispatcher.service
```

See `jit-dispatcher.service` for the template. Set your environment variables in an override or an `EnvironmentFile`.

> **Tip:** The service uses `KillMode=process` so that restarting the dispatcher doesn't kill running containers.

## Customizing the Runner Image

The included `Dockerfile` provides a batteries-included image with:

- Node.js 22
- Python 3.12
- Azure CLI
- GitHub CLI
- Docker CLI (for workflows that build/push images)

To add your own tools, extend the Dockerfile:

```dockerfile
FROM ghactions-runner:latest
USER root
RUN apt-get update && apt-get install -y your-tool
USER runner
```

## How It Works

1. **Repo Discovery** — Every 5 minutes, queries `/user/repos` + `/actions/workflows` to find repos with workflows. New repos are picked up automatically.

2. **Job Detection** — For each repo, checks `/actions/runs` for queued/in-progress runs, then `/actions/runs/{id}/jobs` for queued jobs matching the configured labels.

3. **JIT Config** — Calls `generate-jitconfig` to get a one-time runner configuration. No need to register/deregister runners.

4. **Container Spawn** — Runs `docker run --rm` with the JIT config. The Docker socket is mounted so workflows can use Docker commands.

5. **Cleanup** — Containers auto-remove on exit (`--rm`). The dispatcher tracks PIDs and logs completion status.

## API Rate Limit Management

GitHub allows 5,000 API calls/hour. The dispatcher uses an adaptive polling interval:

```
interval = max(10s, ceil(3600 × (num_repos + 2) / budget))
```

With the default budget of 4,000 and 37 repos, this gives ~36 second intervals — well within limits.

## License

MIT
