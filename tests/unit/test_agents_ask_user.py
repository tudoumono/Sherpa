"""S2（ask_user-improvements.md）: Codex の ask_user（MCP ツール）→ question 化 & 乱用ガードの単体テスト。

①question 化ヘルパ `_codex_ask_question`／ガード②の判定ヘルパ `_codex_ask_capture` を直接検証し、
②ラッパー（CodexProvider.run）の確認カードの出方は `test_codex_turn_run.py`（偽 codex で実際に動かす）で確かめる。
③プロンプト/AGENTS.md に使用条件・確認ID ガード・author 親和の文言があること。
"""
from __future__ import annotations

import inspect
import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import agents as A  # noqa: E402
from sherpa.providers.codex import mcp as MCP  # noqa: E402


def _ask_item(**args):
    return {"tool": "ask_user", "arguments": args}


# ---- ① question 化ヘルパ ----

def test_codex_ask_question_rounds_ask_user_item():
    q = MCP._codex_ask_question(_ask_item(
        mode="single", prompt="対象範囲は？",
        options=[{"id": "a", "label": "A案"}, {"id": "b", "label": "B案"}],
        allow_free_text=True))
    assert q["type"] == "question" and q["mode"] == "single"
    assert q["prompt"] == "対象範囲は？" and q["allow_free_text"] is True
    assert [o["label"] for o in q["options"]] == ["A案", "B案"]
    assert q["interaction_id"]                                   # フロントの回答再送/回答済み判定に必須
    assert "original_message" not in q                           # 元の依頼は chat_service が setdefault で補完


def test_codex_ask_question_none_for_non_ask_user():
    assert MCP._codex_ask_question({"tool": "graph_neighbors", "arguments": {"name": "x"}}) is None
    assert MCP._codex_ask_question("nope") is None
    assert MCP._codex_ask_question({}) is None


def test_codex_ask_question_defaults_when_args_missing():
    # 非 dict 引数でも落とさず既定質問（はい/いいえ）へ丸める（_question_from_args の堅牢性を継承）。
    q = MCP._codex_ask_question({"tool": "ask_user", "arguments": None})
    assert q["type"] == "question" and len(q["options"]) >= 2


# ---- ①' ガード②の判定ヘルパ（実行ベース・RV Low-3） ----

def test_codex_ask_capture_returns_question_when_enabled():
    item = _ask_item(mode="single", prompt="対象範囲は？", options=[{"label": "A"}, {"label": "B"}])
    q = MCP._codex_ask_capture(item, ask_disabled=False)
    assert q is not None and q["type"] == "question" and q["prompt"] == "対象範囲は？"


def test_codex_ask_capture_returns_none_when_disabled():
    """ガード②: 確認ID 付き再送（ask_disabled=True）では ask_user を無視する（None）。"""
    item = _ask_item(mode="single", prompt="対象範囲は？", options=[{"label": "A"}, {"label": "B"}])
    assert MCP._codex_ask_capture(item, ask_disabled=True) is None


# ---- ③ プロンプト/AGENTS.md の使用条件・確認ID ガード・author 親和 ----

def test_prompt_mcp_carries_ask_user_conditions_and_confirm_id_guard():
    p = A.CodexProvider()
    mcp_prompt = p._prompt_mcp("案件一覧を Excel にまとめて", "author", "v1")
    assert "ask_user" in mcp_prompt and "確認ID" in mcp_prompt        # 使用条件＋確認ID ガード（多層防御）
    assert "着手前" in mcp_prompt                                     # author 親和（曖昧なら着手前に確認）


def test_agents_md_carries_ask_user_conditions():
    from sherpa import codex_agents_md
    assert "ask_user" in codex_agents_md.AGENTS_MD and "確認ID" in codex_agents_md.AGENTS_MD
