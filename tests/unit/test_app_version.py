from __future__ import annotations

from sherpa import app_version


def _current(monkeypatch, version: str, sha: str | None) -> str | None:
    monkeypatch.setattr(app_version, "_read_version", lambda: version)
    monkeypatch.setattr(app_version, "_short_sha", lambda: sha)
    monkeypatch.setattr(app_version, "_computed", False)
    return app_version.current()


def test_version_with_commit_is_used_as_is(monkeypatch):
    assert _current(monkeypatch, "0.15.3+e664bebcf", "abcdef12") == "0.15.3+e664bebcf"


def test_plain_version_gets_git_sha(monkeypatch):
    assert _current(monkeypatch, "0.15.3", "abcdef12") == "0.15.3+abcdef12"
