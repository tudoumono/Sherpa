"""`sherpa.investigation_state`（根拠種別の語彙・網羅要求の検知語）の単体テスト。"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

import sherpa.investigation_state as IS  # noqa: E402


def test_demote_reason_for_missing_kinds_is_shared_wording():
    """API（`providers/base.py::_demote_claim_for_missing_kinds`）と Codex
    （`providers/codex/provider.py::_apply_codex_evidence_gate`）の両方の最終ゲートが、
    確定を推定へ落とす理由文言をこの1関数から得る（文言の食い違いを防ぐ唯一の真実源）。"""
    assert (IS.demote_reason_for_missing_kinds(("spec_doc",))
           == "設計書を確認できていないため確定できません")
    assert (IS.demote_reason_for_missing_kinds(("source", "callgraph"))
           == "ソース・呼出関係を確認できていないため確定できません")


def test_coverage_requested_detects_each_keyword_and_ignores_ordinary_or_non_string():
    for kw in IS.COVERAGE_KEYWORDS:
        assert IS.coverage_requested(f"{kw}の条件を教えて") is True
    assert IS.coverage_requested("TAX-RATEは?") is False
    assert IS.coverage_requested("") is False
    assert IS.coverage_requested(None) is False


def test_coverage_keywords_is_the_single_source_for_agents_md():
    """`codex_agents_md.py` の検知語一覧はこの定数から作られる（語の定義は1か所）。"""
    from sherpa import codex_agents_md
    for kw in IS.COVERAGE_KEYWORDS:
        assert f"「{kw}」" in codex_agents_md.AGENTS_MD
