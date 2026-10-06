"""設計書とソースの照らし合わせ（`reconciliation`）: 受け取りの検証・根拠の確認・共有での再構築。
設計: docs/design/codex.md「出力スキーマ」
"""
from __future__ import annotations

import json

import pytest

from sherpa import agentic_search as A
from sherpa.providers.codex import structured as ST
from sherpa.store import shares

pytestmark = pytest.mark.unit


def _row(**kw):
    base = {"item": "割引率", "spec_text": "10%", "spec_ref": "docs/詳細設計.md:12",
            "source_text": "0.08", "source_ref": "src/CALC01.cbl:120", "verdict": "mismatch"}
    return {**base, **kw}


def _text(rows):
    return json.dumps({"status": "final", "answer": "a", "next_step": None, "claims": [],
                       "reconciliation": rows}, ensure_ascii=False)


def test_parse_keeps_valid_rows_and_counts_broken_ones():
    out = ST._parse_structured_v2(_text([_row(), _row(verdict="bogus"), {"item": "x"}]))
    assert len(out["reconciliation"]) == 1 and out["reconciliation_invalid"] == 2
    # 照らし合わせの無い4キー形は従来どおり読める
    old = json.dumps({"status": "final", "answer": "a", "next_step": None, "claims": []})
    assert ST._parse_structured_v2(old)["reconciliation"] == []


def test_unverifiable_ref_lowers_verdict_and_sensitive_text_is_hidden(monkeypatch):
    monkeypatch.setattr(A, "verify_doc_exists", lambda d, w, sp=None: d != "src/GONE.cbl")
    monkeypatch.setattr(ST, "_line_count", lambda d, w, sp=None: 200)
    rows = [_row(verdict="match"),
            _row(item="端数", verdict="match", source_ref="src/GONE.cbl:3"),
            _row(item="鍵", verdict="spec_missing", spec_ref="", spec_text="",
                 source_ref="config/id_rsa:1", source_text="BEGIN PRIVATE KEY")]
    out, meta = ST.verify_reconciliation(rows, "w", None)
    by = {r["item"]: r for r in out}
    assert by["割引率"]["verdict"] == "match" and by["割引率"]["source"]["line"] == 120
    assert by["端数"]["verdict"] == "unverified" and "doc_id" not in by["端数"]["source"]
    assert by["鍵"]["verdict"] == "unverified" and by["鍵"]["source"]["text"] == ""
    assert meta["unverified"] == 2
    assert out[-1]["verdict"] == "match"  # 一致は最後に並ぶ


def test_share_rebuilds_rows_from_known_fields_only():
    row = {"item": "割引率", "verdict": "mismatch", "extra": "x",
           "spec": {"text": "10%", "doc_id": "a.md", "line": 3, "line_end": 9, "secret": "s"},
           "source": {"text": "0.08"}}
    out = shares._redact_importance_from_answer_data({"reconciliation": [row, {"item": "bad", "verdict": "zzz"}]})
    assert out["reconciliation"] == [{"item": "割引率", "verdict": "mismatch",
                                      "spec": {"text": "10%", "doc_id": "a.md", "line": 3, "line_end": 9},
                                      "source": {"text": "0.08"}}]


def test_missing_side_is_always_blank_and_out_of_range_line_is_unverified(monkeypatch):
    monkeypatch.setattr(A, "verify_doc_exists", lambda d, w, sp=None: True)
    monkeypatch.setattr(ST, "_line_count", lambda d, w, sp=None: 200)
    rows = [_row(item="欠け", verdict="spec_missing", spec_ref="workspace/mine.md:1", spec_text="私的な記述"),
            _row(item="行外", verdict="match", source_ref="src/CALC01.cbl:999999"),
            _row(item="範囲外", verdict="match", source_ref="src/CALC01.cbl:150-300"),
            _row(item="範囲", verdict="match", source_ref="src/CALC01.cbl:150-200"),
            _row(item="行なし", verdict="match", source_ref="src/CALC01.cbl"),
            _row(item="記述なし", verdict="match", spec_text="  ")]
    by = {r["item"]: r for r in ST.verify_reconciliation(rows, "w", None)[0]}
    assert by["欠け"]["verdict"] == "spec_missing" and by["欠け"]["spec"] == {"text": ""}
    assert by["行外"]["verdict"] == "unverified" and by["行外"]["source"] == {"text": ""}
    assert by["範囲外"]["verdict"] == "unverified"
    assert by["範囲"]["verdict"] == "match" and by["範囲"]["source"]["line"] == 150 and by["範囲"]["source"]["line_end"] == 200
    assert by["行なし"]["verdict"] == "unverified" and by["記述なし"]["verdict"] == "unverified"
    assert by["行なし"]["source"] == {"text": ""} and by["記述なし"]["spec"] == {"text": ""}


def test_office_ref_line_is_checked_against_converted_body(tmp_path, monkeypatch):
    from sherpa import worlds
    der_md, der_rag = tmp_path / "md", tmp_path / "rag"
    (der_md / "設計").mkdir(parents=True)
    der_rag.mkdir()
    (der_md / "設計" / "仕様.xlsx.md").write_text("\n".join(f"行{i}" for i in range(1, 7)) + "\n", encoding="utf-8")
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der_md)
    monkeypatch.setattr(worlds, "derived_rag_dir", lambda w: der_rag)
    monkeypatch.setattr(A, "verify_doc_exists", lambda d, w, sp=None: True)
    real = ST._line_count
    monkeypatch.setattr(ST, "_line_count", lambda d, w, sp=None: real(d, w, sp) if d.endswith(".xlsx") else 200)
    rows = [_row(item="内", verdict="match", spec_ref="設計/仕様.xlsx:6"),
            _row(item="外", verdict="match", spec_ref="設計/仕様.xlsx:7")]
    by = {r["item"]: r for r in ST.verify_reconciliation(rows, "w", None)[0]}
    assert by["内"]["verdict"] == "match" and by["内"]["spec"]["line"] == 6
    assert by["外"]["verdict"] == "unverified"


def test_cap_is_applied_before_verification_and_docs_are_checked_once(monkeypatch):
    calls = []
    monkeypatch.setattr(A, "verify_doc_exists", lambda d, w, sp=None: calls.append(d) or True)
    monkeypatch.setattr(ST, "_line_count", lambda d, w, sp=None: 500)
    rows = [_row(item=f"一致{i}", verdict="match") for i in range(60)] + [_row(item="差")]
    out, meta = ST.verify_reconciliation(rows, "w", None)
    assert len(out) == 40 and meta["more"] == 21 and out[0]["item"] == "差"
    assert len(calls) == 2  # 資料ごとに 1 回（行・側ごとに確かめ直さない）
