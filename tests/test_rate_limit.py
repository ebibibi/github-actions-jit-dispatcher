"""Tests for keeping the dispatcher inside the account's shared rate limit.

The REST limit (5,000 requests/hour) belongs to the user, so every request the
dispatcher wastes is taken from gh CLI and other tools on the same account.
"""

from __future__ import annotations

import time

import pytest

from test_dispatcher import load_dispatcher


@pytest.fixture
def dispatcher(monkeypatch):
    return load_dispatcher(monkeypatch, RATE_LIMIT_RESERVE="1000")


def fake_api(dispatcher, monkeypatch, routes: dict):
    """Route github_api calls to canned responses and record the paths."""
    calls: list[str] = []

    def api(method, path, data=None, raw=False):
        calls.append(path)
        status, body = routes.get(path, (404, None))
        dispatcher.last_error_status = None if status == 200 else status
        return body

    monkeypatch.setattr(dispatcher, "github_api", api)
    return calls


SELF_HOSTED = "jobs:\n  build:\n    runs-on: [self-hosted, linux]\n"
HOSTED = "jobs:\n  check:\n    runs-on: ubuntu-latest  # not self-hosted\n"


def wf_routes(repo: str, text: str) -> dict:
    return {
        f"/repos/example/{repo}/contents/.github/workflows": (
            200,
            [{"name": "ci.yml", "type": "file", "path": ".github/workflows/ci.yml"}],
        ),
        f"/repos/example/{repo}/contents/.github/workflows/ci.yml": (200, text),
    }


def test_label_in_a_comment_does_not_count(dispatcher) -> None:
    assert not dispatcher.workflow_targets_self_hosted(HOSTED, "self-hosted")
    assert dispatcher.workflow_targets_self_hosted(SELF_HOSTED, "self-hosted")


def test_only_repos_targeting_self_hosted_are_polled(dispatcher, monkeypatch) -> None:
    repos = [
        {"name": "uses-runner", "pushed_at": "t1"},
        {"name": "hosted-only", "pushed_at": "t1"},
        {"name": "no-workflows", "pushed_at": "t1"},
        {"name": "old", "pushed_at": "t1", "archived": True},
    ]
    routes = {"/user/repos?per_page=100&affiliation=owner&page=1": (200, repos)}
    routes |= wf_routes("uses-runner", SELF_HOSTED) | wf_routes("hosted-only", HOSTED)
    calls = fake_api(dispatcher, monkeypatch, routes)

    assert dispatcher.discover_repos() == ["uses-runner"]
    assert not any("/old/" in c for c in calls)


def test_unchanged_repos_are_not_rescanned(dispatcher, monkeypatch) -> None:
    repos = [{"name": "uses-runner", "pushed_at": "t1"}]
    routes = {"/user/repos?per_page=100&affiliation=owner&page=1": (200, repos)}
    routes |= wf_routes("uses-runner", SELF_HOSTED)
    calls = fake_api(dispatcher, monkeypatch, routes)

    dispatcher.discover_repos()
    dispatcher.repos_last_refreshed = 0  # force the next discovery
    calls.clear()
    assert dispatcher.discover_repos() == ["uses-runner"]
    assert calls == ["/user/repos?per_page=100&affiliation=owner&page=1"]


def test_failed_scan_keeps_previous_selection(dispatcher, monkeypatch) -> None:
    dispatcher.cached_repos = ["uses-runner"]
    repos = [{"name": "uses-runner", "pushed_at": "t2"}]
    routes = {
        "/user/repos?per_page=100&affiliation=owner&page=1": (200, repos),
        "/repos/example/uses-runner/contents/.github/workflows": (500, None),
    }
    fake_api(dispatcher, monkeypatch, routes)

    assert dispatcher.discover_repos() == ["uses-runner"]
    assert "uses-runner" not in dispatcher.repo_scan_cache
    assert dispatcher.repos_last_refreshed == 0


def test_exhausted_limit_pauses_until_reset(dispatcher) -> None:
    reset = int(time.time()) + 600
    dispatcher.note_rate_limit(
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)}, 403
    )
    assert 590 < dispatcher.rate_limit_pause() <= 601


def test_low_remaining_leaves_reserve_for_other_tools(dispatcher) -> None:
    reset = int(time.time()) + 300
    dispatcher.note_rate_limit({"X-RateLimit-Remaining": "1500", "X-RateLimit-Reset": str(reset)})
    assert dispatcher.rate_limit_pause() == 0
    dispatcher.note_rate_limit({"X-RateLimit-Remaining": "999", "X-RateLimit-Reset": str(reset)})
    assert dispatcher.rate_limit_pause() > 290


def test_permission_denied_without_limit_headers_does_not_pause(dispatcher) -> None:
    dispatcher.note_rate_limit({"X-RateLimit-Remaining": "4000"}, 403)
    assert dispatcher.rate_limit_pause() == 0


def test_no_request_is_sent_while_paused(dispatcher, monkeypatch) -> None:
    dispatcher.rate_limited_until = time.time() + 60

    def boom(*_a, **_k):
        raise AssertionError("request sent while paused")

    monkeypatch.setattr(dispatcher.urllib.request, "urlopen", boom)
    assert dispatcher.github_api("GET", "/user/repos") is None
