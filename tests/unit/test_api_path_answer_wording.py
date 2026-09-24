"""API 経路（OpenAI/Gemini/Ollama/Bedrock 頭脳）の回答方針を、Codex 経路で先に実施した
「回答を絞らない・確定した事実と推定は分けて推定は明示する・一覧は全件パス付き」へ揃える契約テスト
（2026-09-10・ユーザー裁定「API 経路も同じ『絞らない』方針に揃える」・
提案書 docs/proposals/2026-09-10-Codex原本直読と調査スキル.md §6 #6・
RV 台帳 docs/rv/2026-09-10-Codex原本直読と調査スキル.md #3 起点）。

`tests/unit/test_codex_workspace_authoring.py::test_answer_simplification_wording_contract_2026_09_10`
（Codex 経路側の同種契約テスト）と同じ流儀＝禁止語の非存在＋必須語の存在を固定する。DB/ES/Neo4j
不要（文字列定数の検証のみ）。
"""
from __future__ import annotations

from sherpa import agentic_search as A
from sherpa.providers import prompts as P


def test_api_path_answer_wording_contract_2026_09_10():
    """system_prompt()／_FINAL_SYNTHESIS／_FINAL_SYNTHESIS_SUFFICIENT／_RESYNTH_INSTRUCTION／
    prompts._answer_prompt(...) から「簡潔（2〜4文）」「推測しない」「出典の列挙は不要」を撤去し、
    「推定」（確定と推定を分ける・5 箇所）「全件」（一覧は全件パス付き＝system_prompt のみ。清書
    _answer_prompt は「省略せず」「補わない」＝事実に載っている項目に限定した契約）
    を含む——回答を絞らず・確定と推定を分けて答える方針への置換（API 経路も Codex 経路と同じ語彙）。
    """
    system_text = A.system_prompt()
    answer_prompt_text = P._answer_prompt("消費税率を変えたい", "qa", {"data": {}})

    forbidden = ("簡潔（2〜4文）", "推測しない", "出典の列挙は不要")
    texts = {
        "system_prompt()": system_text,
        "_FINAL_SYNTHESIS": A._FINAL_SYNTHESIS,
        "_FINAL_SYNTHESIS_SUFFICIENT": A._FINAL_SYNTHESIS_SUFFICIENT,
        "_RESYNTH_INSTRUCTION": A._RESYNTH_INSTRUCTION,
        "prompts._answer_prompt(...)": answer_prompt_text,
    }
    for name, text in texts.items():
        for phrase in forbidden:
            assert phrase not in text, f"{name} に撤去したはずの文言が残っている: {phrase!r}"

    # 「推定」は5箇所すべてで確認できたこと（確定）と分ける指示の一部として必須。
    for name, text in texts.items():
        assert "推定" in text, f"{name} に必須文言が無い: '推定'"

    # 「全件」は一覧の全件パス付き列挙を指示する system_prompt() に必須。清書（_answer_prompt）は
    # 「事実に載っている項目を省略せず・事実に無いパスは補わない」に限定した契約。
    # （_FINAL_SYNTHESIS 系・_RESYNTH_INSTRUCTION は根拠を絞らない指示であり一覧列挙の役割外）。
    assert "全件" in system_text, "system_prompt() に必須文言が無い: '全件'"
    assert "省略せず" in answer_prompt_text and "補わない" in answer_prompt_text, \
        "prompts._answer_prompt(...) に必須文言が無い: '省略せず'/'補わない'"
