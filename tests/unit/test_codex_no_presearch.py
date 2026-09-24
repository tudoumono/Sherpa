"""MCP 付き Codex 経路は qa／troubleshoot／author で事前の下調べ（`providers/base.py::_gather` の
`ctx.dispatch`＝grep／ES 検索）を省き、impact は省かない契約のテスト。

実害: MCP 版プロンプトは下調べの結果を Codex に渡さないのに、実環境の大きな資料フォルダでは
トラブルシュートの下調べ（語ごとに資料フォルダ全体を読み直す grep）が Codex の起動を約 8 分止めていた。
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


@pytest.mark.parametrize("lens, mcp_on, expect_dispatch", [
    ("qa", True, False),        # 省く（Codex が MCP ツールで自分で調べる）
    ("impact", True, True),     # 省かない（影響一覧を回答と並べて表示する）
    ("qa", False, True),        # MCP 無効は下調べの結果をプロンプトへ渡すので省かない
])
def test_codex_presearch_skipped_only_for_mcp_non_impact(tmp_path, monkeypatch, lens, mcp_on, expect_dispatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(_FAKE_CODEX_PY)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    monkeypatch.setenv("SHERPA_CODEX_MCP", "1" if mcp_on else "0")
    calls: list = []

    def _dispatch(lens_, inp):
        calls.append(lens_)
        return {"lens": lens_, "headline": "下調べの回答", "summary": {"total": 1}, "data": {},
                "sources": [{"doc_id": "presearch.md"}]}

    ctx = A.Ctx(message="消費税率について教えて", world="v1",
                route=lambda msg: {"lens": lens, "input": msg, "reason": "test", "confident": True},
                dispatch=_dispatch, knowledge=True, uid="no-presearch-u1", make_sources=lambda docs: [])
    results = [e for e in A.CodexProvider().run(ctx) if isinstance(e, dict) and e.get("type") == "_result"]
    env = results[0]["env"]

    assert calls == ([lens] if expect_dispatch else [])
    assert env["headline"] == "Codex の回答"
    if not expect_dispatch:
        assert env.get("sources") == []          # 下調べ由来の出典が混ざらない
        assert "agentic_failure" not in env      # 回答できたターンは失敗の印を外す
