"""Codex 経路には事前の下調べ（grep／ES 検索・グラフでの影響のたどり）が無く、調べる作業はすべて Codex の道具で行う契約のテスト。

実害: 事前の下調べは Codex に結果を渡さないのに、実環境の大きな資料フォルダでは Codex の起動を約 8 分止めていた。
Codex CLI が実行の直前に見つからなくなったときも、旧い事前検索へ切り替えず「未接続」と明示して終える。
偽 `codex`（PATH に差し込む実行ファイル）だけをモックする。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import agents as A  # noqa: E402

_FAKE_CODEX_PY = r'''#!/usr/bin/env python3
import json
print(json.dumps({"type": "thread.started", "thread_id": "SID-PRESEARCH"}))
print(json.dumps({"type": "item.completed",
                  "item": {"id": "m0", "type": "agent_message", "text": "Codex の回答"}}))
print(json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0}}))
'''


@pytest.mark.parametrize("lens", ["qa", "impact", "troubleshoot", "author"])
def test_codex_presearch_skipped_for_every_lens(tmp_path, monkeypatch, lens):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(_FAKE_CODEX_PY)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    ctx = A.Ctx(message="消費税率について教えて", world="v1",
                route=lambda msg: {"lens": lens, "input": msg, "reason": "test", "confident": True},
                knowledge=True, uid="no-presearch-u1", make_sources=lambda docs: [])
    results = [e for e in A.CodexProvider().run(ctx) if isinstance(e, dict) and e.get("type") == "_result"]
    env = results[0]["env"]

    assert env["headline"] == "Codex の回答"
    assert env.get("sources") == []          # 下調べ由来の出典が混ざらない
    assert "agentic_failure" not in env      # 回答できたターンは失敗の印を外す
    assert not (env.get("data") or {}).get("items")   # 影響一覧の表は作らない（「影響なし」とも出さない）


@pytest.mark.parametrize("lens", ["qa", "impact", "troubleshoot", "author"])
def test_codex_cli_missing_at_run_time_is_explicit_unconnected_failure(tmp_path, monkeypatch, lens):
    """選択後〜実行開始の間に Codex CLI が見つからなくなったら、別の検索の結果を返さず、選択時と同じ「未接続」の案内で終える。"""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    ctx = A.Ctx(message="消費税率について教えて", world="v1",
                route=lambda msg: {"lens": lens, "input": msg, "reason": "test", "confident": True},
                knowledge=True, uid="no-presearch-u2", make_sources=lambda docs: [])
    events = list(A.CodexProvider().run(ctx))
    env = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"][0]["env"]

    assert "Codex はまだ接続されていません" in env["headline"]
    assert "Codex CLI が見つかりません" in env["headline"]
    assert env["agentic_failure"] == "error"
    assert env["data"] == {} and env["sources"] == []
    assert not any(e.get("id") == "codex" for e in events if isinstance(e, dict))   # Codex の調査には入っていない
