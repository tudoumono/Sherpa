"""Codex の準備・退避の失敗が、成功したように見えない（失敗を回答の注記・失敗の回答で見せる）ことの試験。偽 codex 実行ファイル方式。"""
from __future__ import annotations

import os
import shutil

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import test_codex_ledger_gate as G  # noqa: E402

from sherpa import codex_agents_md  # noqa: E402


def _kinds(env) -> list:
    return [n["kind"] for n in env.get("notices") or []]


def test_retire_failure_is_reported_and_run_dir_is_kept(tmp_path, monkeypatch):
    """未完了の台帳を退避できなかったら、retained:false のまま作業領域を消さず、注記で知らせる。"""
    def _boom(*_a, **_k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(shutil, "copy2", _boom)
    _, env, _ = G._t(tmp_path, monkeypatch, [G._st("TH-RF", G._final("途中です。"), ledger=G._open())],
                     "rf-retire", 32001)
    assert env["investigation"]["retained"] is False and env["investigation"]["retain_failed"] is True
    assert "ledger_retire_failed" in _kinds(env)
    kept = list((tmp_path / G._users("rf-retire") / "rf-retire" / "workspace" / "authoring").glob("run-*/.tmp/investigation"))
    assert kept, "退避できなかった作業データが消されている"


def test_agents_md_failure_does_not_run_codex(tmp_path, monkeypatch):
    """AGENTS.md を置けなかったら Codex を起動せず、準備できなかったことを回答に出す。"""
    def _boom(*_a, **_k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(codex_agents_md, "write_agents_md", _boom)
    _, env, calls = G._t(tmp_path, monkeypatch, [G._st("TH-AG", G._final("答えです。"))], "rf-agents", 32002)
    assert calls == []
    assert "run_files_failed" in _kinds(env)
    assert env["completion"] == "failed"
