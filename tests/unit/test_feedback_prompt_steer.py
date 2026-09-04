"""F1/F2（2026-07-07-フィードバック一括.md）: 影響質問のクエリ計画・ask_user 発動基準のプロンプト文言テスト。

F1: 影響を問う質問では症状語（落ちる/止まる/エラー等）をそのまま検索語にしない指示が `_prompt_mcp`／
    `_facts` の impact steer に含まれること。
F2: ①lens 別の具体基準（影響分析で候補が割れる/確実0件）②「確認してから進めて」で必ず ask_user から
    始める③S2 の確認ID ガードが上の指示より優先される旨が `_prompt_mcp`／`_DESC_ASK`／AGENTS_MD に
    含まれること。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import agents as A  # noqa: E402


# ---- F1: 影響質問の型・症状語禁止 ----

def test_prompt_mcp_impact_query_plan_and_no_symptom_words():
    p = A.CodexProvider()
    for lens in ("qa", "impact", "troubleshoot"):
        prompt = p._prompt_mcp("税率を変えたら夜間バッチが落ちる？", lens, "v1")
        assert "影響を問う質問" in prompt
        assert "接続" in prompt and "graph_neighbors" in prompt        # 接続（経路）を最優先で調べる
        assert "症状表現" in prompt and "検索語にしない" in prompt      # 症状語をそのまま検索語にしない
        assert "トラブルシュート" in prompt                             # 原因調査明示時のみ症状語可


def test_facts_impact_steer_points_to_connection_not_symptoms():
    # 構造的な影響0件（presumed のみ）で steer が付く＝接続の確認へ誘導・症状語を追わない
    # （K12・2026-09-04-グラフのソース正典化.md §4＝確実/要確認の2値判定は撤去済み・全件同格）。
    env = {"data": {"start": "税率", "items": [],
                    "presumed": [{"name": "夜間バッチ運用", "category": "機能"}]},
           "summary": {"total": 0, "presumed": 1, "code_silent": True}}
    facts = A._facts("impact", env)
    assert "接続" in facts and "経路" in facts
    assert "症状語をそのまま探さない" in facts


# ---- F2-1: lens 別の具体基準 ----

def test_prompt_mcp_ask_user_has_lens_specific_criteria():
    p = A.CodexProvider()
    prompt = p._prompt_mcp("税率を変えたら夜間バッチが落ちる？", "impact", "v1")
    assert "影響分析" in prompt and "複数候補" in prompt
    assert "要確認だけ" in prompt


def test_desc_ask_has_lens_specific_criteria():
    from sherpa import agentic_search
    assert "影響分析" in agentic_search._DESC_ASK
    assert "確認してから進めて" in agentic_search._DESC_ASK


# ---- F2-2: ユーザー主導の確認＋S2 ガード優先 ----

def test_prompt_mcp_user_driven_confirmation_and_guard_priority():
    p = A.CodexProvider()
    prompt = p._prompt_mcp("税率の一覧を Excel にまとめて。確認してから進めて。", "author", "v1")
    assert "確認してから進めて" in prompt                              # ユーザー主導の発動条件
    assert "調査より先に" in prompt or "先に必ず ask_user" in prompt
    # S2 ガードとの優先関係が明記されていること（確認ID 付き再送は再質問禁止が勝つ）。
    i_user = prompt.index("確認してから進めて")
    i_guard = prompt.index("確認ID")
    assert i_guard > i_user, "確認ID ガードの優先明記が『確認してから進めて』より前にあり矛盾する"
    assert "再質問禁止を優先" in prompt


def test_agents_md_user_driven_confirmation_and_guard_priority():
    from sherpa import codex_agents_md
    md = codex_agents_md.AGENTS_MD
    assert "確認してから進めて" in md
    assert "再質問禁止を優先" in md
    assert "確認ID" in md
