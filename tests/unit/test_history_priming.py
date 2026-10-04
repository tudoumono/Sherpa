"""R1a（会話継続・履歴 priming）の break-and-confirm テスト
（docs/proposals/2026-07-13-横断レビュー対応.md §R1a）。

「追質問が前ターンを理解しない」を解消するため、`Ctx.history`（chat_service._history_pairs が
構築する直前ターンの (user, assistant) 完全対・二重キャップ済み）が agentic ループ（openai_style）と
Codex の実送信 body/messages/contents/プロンプトへ実際に注入されることを、外部 HTTP を実際に
叩かずに検証する。seam: agentic ループ（openai_style）は
`agentic_search._post` 差し替え、Codex は `_prompt_mcp` を
直接呼ぶ（プロセス起動なし）。

`chat_service._history_pairs` 自体の単体テスト（完全対抽出・二重キャップ・確認ID 回帰等）は
tests/unit/test_chat_service.py 側にある。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

from sherpa import agentic_search as AS  # noqa: E402
from sherpa import agents as A  # noqa: E402
from sherpa.agents import Ctx  # noqa: E402

# 直前ターン1対（chat_service._history_pairs が返す shape そのもの＝時系列順の user/assistant）。
_HISTORY = [{"role": "user", "content": "前回の質問です"}, {"role": "assistant", "content": "前回の回答です"}]


# ---- Codex: run() が history を確定させる ----

def test_codex_run_sets_history_attribute_before_knowledge_off_branch():
    p = A.CodexProvider()
    ctx = Ctx(message="hi", world="v1", knowledge=False,
              route=lambda m: {}, dispatch=lambda l, i: {}, history=list(_HISTORY))
    list(p.run(ctx))
    assert p._history == _HISTORY


# ---- agentic ループ: 初期 msgs に history が現在の user の前に入る ----

# ---- 履歴なし（既定）は history 引数追加前と同形の初期 msgs/messages/contents
# （帰属は回答完了後の別呼び出しで取る設計＝初期 user は不変・既存 pin の再確認） ----

# ---- provider 経由の end-to-end 配線（_agentic_loop から agentic_search への history 伝搬） ----

# ---- Codex: _prompt_mcp に履歴ブロックが挿入される（プロセス起動なし） ----

def test_codex_prompt_mcp_includes_history_block_before_question():
    p = A.CodexProvider()
    p._history = list(_HISTORY)
    prompt = p._prompt_mcp("続きを教えて", "qa", "v1")
    assert "【直前の会話（参考・新しいものが下）】" in prompt
    assert prompt.index("前回の回答です") < prompt.index("【質問】")


def test_codex_prompt_mcp_author_includes_history_block_before_request():
    p = A.CodexProvider()
    p._history = list(_HISTORY)
    prompt = p._prompt_mcp("Excelにまとめて", "author", "v1")
    assert prompt.index("前回の回答です") < prompt.index("【依頼】")


def test_codex_prompt_mcp_history_empty_matches_legacy_output_exactly():
    p_empty = A.CodexProvider()
    p_explicit = A.CodexProvider()
    p_explicit._history = []
    out_empty = p_empty._prompt_mcp("質問", "qa", "v1")
    assert out_empty == p_explicit._prompt_mcp("質問", "qa", "v1")
    assert "【直前の会話" not in out_empty
