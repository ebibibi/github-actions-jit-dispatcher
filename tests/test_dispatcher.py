"""Tests for the runner container's isolation boundary.

The Docker socket is the whole game here: a runner that can reach it can run
`docker run -v /:/host` and read or write anything on the host. Public repos
run fork-authored code on `pull_request`, so "which repos get the socket" is a
security decision, not a convenience toggle.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))


def load_dispatcher(monkeypatch: pytest.MonkeyPatch, **env: str):
    for key in (
        "DOCKER_SOCKET_REPOS",
        "DOCKER_SOCKET",
        "RUNNER_MEMORY",
        "RUNNER_CPUS",
        "RUNNER_PIDS_LIMIT",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GITHUB_OWNER", "example")
    module = importlib.import_module("dispatcher")
    return importlib.reload(module)


def test_docker_socket_is_not_mounted_by_default(monkeypatch) -> None:
    dispatcher = load_dispatcher(monkeypatch)

    cmd = dispatcher.build_run_command("jit-1", "cfg", "any-repo")

    assert "/var/run/docker.sock" not in " ".join(cmd)


def test_docker_socket_is_mounted_only_for_listed_repos(monkeypatch) -> None:
    dispatcher = load_dispatcher(
        monkeypatch, DOCKER_SOCKET_REPOS="image-builder, other-builder"
    )

    allowed = dispatcher.build_run_command("jit-1", "cfg", "image-builder")
    denied = dispatcher.build_run_command("jit-2", "cfg", "public-repo")

    assert "/var/run/docker.sock:/var/run/docker.sock" in allowed
    assert "/var/run/docker.sock" not in " ".join(denied)
    assert "other-builder" in dispatcher.DOCKER_SOCKET_REPOS


def test_allow_list_ignores_blanks_and_does_not_prefix_match(monkeypatch) -> None:
    dispatcher = load_dispatcher(monkeypatch, DOCKER_SOCKET_REPOS="builder,, ,")

    assert dispatcher.DOCKER_SOCKET_REPOS == frozenset({"builder"})
    denied = dispatcher.build_run_command("jit-1", "cfg", "builder-evil")
    assert "/var/run/docker.sock" not in " ".join(denied)


def test_every_container_is_resource_capped(monkeypatch) -> None:
    dispatcher = load_dispatcher(
        monkeypatch, RUNNER_MEMORY="2g", RUNNER_CPUS="1", RUNNER_PIDS_LIMIT="256"
    )

    cmd = dispatcher.build_run_command("jit-1", "cfg", "any-repo")

    assert cmd[cmd.index("--memory") + 1] == "2g"
    assert cmd[cmd.index("--cpus") + 1] == "1"
    assert cmd[cmd.index("--pids-limit") + 1] == "256"


def test_container_is_ephemeral_and_carries_the_jit_config(monkeypatch) -> None:
    dispatcher = load_dispatcher(monkeypatch)

    cmd = dispatcher.build_run_command("jit-1", "encoded-config", "any-repo")

    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "JIT_CONFIG=encoded-config" in cmd
    assert cmd[-1] == os.environ.get("RUNNER_IMAGE", "ghactions-runner:latest")
