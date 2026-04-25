#!/bin/bash
set -euo pipefail

if [ -z "${JIT_CONFIG:-}" ]; then
    echo "ERROR: JIT_CONFIG environment variable is required"
    exit 1
fi

# Fix docker socket permissions if mounted
if [ -S /var/run/docker.sock ]; then
    DOCKER_GID=$(stat -c "%g" /var/run/docker.sock)
    if [ "$DOCKER_GID" != "0" ]; then
        sudo groupmod -g "$DOCKER_GID" docker 2>/dev/null || true
    fi
    sudo chmod 666 /var/run/docker.sock 2>/dev/null || true
fi

exec ./run.sh --jitconfig "$JIT_CONFIG"
