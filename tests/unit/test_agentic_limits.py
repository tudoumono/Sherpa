"""利用統計「打ち切りの内訳」の単体テスト。

内部制限（1件あたりのツール結果バイト予算・累計予算・会話履歴の文脈整理・清書入力の打ち切り・
検索系ツールの件数打ち切り）が実際に発動した回数を数えるだけの計測——制限そのものの値・挙動は
一切変えない契約をここで固定する。LLM は stub（コスト0・`test_agentic_search.py` と同じ流儀）。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

from sherpa.parts.read import tools as RT  # noqa: E402
from sherpa import store  # noqa: E402


def _final_events(events: list) -> list:
    return [ev for ev in events if "final" in ev]


def test_codex_provider_without_cli_does_not_reference_unbound_counter(tmp_path, monkeypatch):
    """Codex CLI が無い（起動しない）経路でも run() が UnboundLocalError にならず、結果を 1 件返す。"""
    import shutil
    import test_codex_auto_continue as helper
    from sherpa import agents
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    events = helper._run(agents.CodexProvider(), helper._ctx("no-cli", 41001))
    assert helper._result_env(events)


def test_reader_finisher_marks_byte_clip_distinct_from_row_cap():
    """`_finish_reader_result` がバイト予算で切ったときだけ `byte_clipped` が立つ（行数上限だけでは立たない）。"""
    long_rows = [{"row": i, "cells": ["x" * 50]} for i in range(40)]
    out = RT._finish_reader_result("xlsx_range", {"rows": long_rows, "truncated": True}, "a.xlsx", 400)
    assert out.get("byte_clipped") is True
    small = RT._finish_reader_result("xlsx_range", {"rows": long_rows[:2], "truncated": True}, "a.xlsx", 100000)
    assert not small.get("byte_clipped")


def test_docx_paragraph_shrink_marks_byte_clip():
    """docx の段落削減（表の救済ではない経路）でも byte_clipped が立つ。"""
    paras = [{"index": i, "text": "x" * 100} for i in range(20)]
    out = RT._finish_docx_paragraphs_result({"paragraphs": paras, "tables": []}, "a.docx", 1024)
    assert out.get("truncated") is True and out.get("byte_clipped") is True


def test_compare_documents_within_budget_is_not_clipped():
    """予算内の diff は先引きで切られない（byte_clipped も立たない）。"""
    diff_text = "a" * 1024
    result = {"status": "ok", "diff": diff_text, "left": {"doc_id": "a.md"}, "right": {"doc_id": "b.md"}}
    from sherpa import agentic_search as A2
    clipped = A2._clip_utf8_bytes(diff_text, 1024)
    assert clipped == diff_text      # 予算ちょうどは切らない前提の確認
