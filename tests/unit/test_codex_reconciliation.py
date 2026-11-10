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


def test_office_and_pdf_positions_keep_the_verdict_and_carry_loc(monkeypatch):
    monkeypatch.setattr(A, "verify_doc_exists", lambda d, w, sp=None: True)
    monkeypatch.setattr(ST, "_line_count", lambda d, w, sp=None: 200)
    rows = [_row(item="表", verdict="match", spec_ref="設計/料金表.xlsx:料金!A1:G7"),
            _row(item="段落", verdict="match", spec_ref="設計/運用規程.docx:段落12"),
            _row(item="頁", verdict="match", spec_ref="設計/点検報告.pdf:4ページ")]
    out, meta = ST.verify_reconciliation(rows, "w", None)
    by = {r["item"]: r for r in out}
    assert meta["unverified"] == 0
    assert by["表"]["spec"]["doc_id"] == "設計/料金表.xlsx" and by["表"]["spec"]["loc"] == "料金!A1:G7"
    assert "line" not in by["表"]["spec"]
    assert by["段落"]["spec"]["loc"] == "段落12" and by["頁"]["spec"]["loc"] == "4ページ"
    assert by["表"]["source"]["loc"] == "120" and by["表"]["source"]["line"] == 120


def test_line_list_is_checked_against_total_and_multiple_refs_are_all_checked(monkeypatch):
    monkeypatch.setattr(A, "verify_doc_exists", lambda d, w, sp=None: d != "src/GONE.cbl")
    monkeypatch.setattr(ST, "_line_count", lambda d, w, sp=None: 200)
    rows = [_row(item="列挙", verdict="match", source_ref="src/CALC01.cbl:3,7-10"),
            _row(item="列挙外", verdict="match", source_ref="src/CALC01.cbl:3,7-999"),
            _row(item="複数", verdict="match", spec_ref="docs/詳細設計.md:12; 設計/料金表.xlsx:料金!A1:B2"),
            _row(item="続き", verdict="match", spec_ref="docs/詳細設計.md:12; src/GONE.cbl:5"),
            _row(item="秘匿", verdict="match", spec_ref="docs/詳細設計.md:12; config/id_rsa:1")]
    out, meta = ST.verify_reconciliation(rows, "w", None)
    by = {r["item"]: r for r in out}
    assert by["列挙"]["verdict"] == "match" and by["列挙"]["source"]["loc"] == "3,7-10" and "line" not in by["列挙"]["source"]
    assert by["列挙外"]["verdict"] == "unverified"
    spec = by["複数"]["spec"]
    assert spec["loc"] == "12" and spec["also"] == [{"doc_id": "設計/料金表.xlsx", "loc": "料金!A1:B2"}]
    assert by["続き"]["verdict"] == "unverified" and "doc_id" not in by["続き"]["spec"]
    assert by["秘匿"]["verdict"] == "unverified" and by["秘匿"]["spec"] == {"text": ""}
    assert meta["unverified"] == 3


def test_position_mixing_lines_and_other_text_is_unverified_but_free_text_list_is_kept(monkeypatch):
    docs = {"src/CALC01.cbl", "設計/料金表.xlsx"}
    monkeypatch.setattr(A, "verify_doc_exists", lambda d, w, sp=None: d in docs)
    monkeypatch.setattr(ST, "_line_count", lambda d, w, sp=None: 200)
    for ref in ["src/CALC01.cbl:12; src/GONE.cbl:5", "src/CALC01.cbl:12、src/GONE.cbl", "src/CALC01.cbl:12, 初期化の段"]:
        out, _ = ST.verify_reconciliation([_row(verdict="match", source_ref=ref)], "w", None)
        assert out[0]["verdict"] == "unverified", ref
    out, _ = ST.verify_reconciliation([_row(verdict="match", spec_ref="設計/料金表.xlsx:料金!A1:B2、料金!C1:D2")], "w", None)
    assert out[0]["verdict"] == "match" and out[0]["spec"]["loc"] == "料金!A1:B2、料金!C1:D2" and "also" not in out[0]["spec"]


def test_sensitive_name_inside_position_text_blanks_the_side(monkeypatch):
    monkeypatch.setattr(A, "verify_doc_exists", lambda d, w, sp=None: True)
    monkeypatch.setattr(ST, "_line_count", lambda d, w, sp=None: 200)
    row = _row(verdict="match", source_ref="src/CALC01.cbl:10 and config/id_rsa:1", source_text="BEGIN PRIVATE KEY")
    out, _ = ST.verify_reconciliation([row], "w", None)
    assert out[0]["verdict"] == "unverified" and out[0]["source"] == {"text": ""}


def _only(monkeypatch, **side):
    monkeypatch.setattr(A, "verify_doc_exists", lambda d, w, sp=None: d != "src/GONE.cbl" and not d.startswith("src/NONE"))
    monkeypatch.setattr(ST, "_line_count", lambda d, w, sp=None: 200)
    out, _ = ST.verify_reconciliation([_row(verdict="match", **side)], "w", None)
    return out[0]


def test_sensitive_name_in_any_word_of_the_field_blanks_the_side(monkeypatch):
    for ref in ["src/CALC01.cbl:10, /abs/elsewhere/id_rsa:1", "src/CALC01.cbl:10、config/.env：3",
                "src/CALC01.cbl:10（config/.env も参照）", "src/CALC01.cbl:10 id_rsa:1",
                'src/CALC01.cbl:10、"config/secret.pem":3', "src/CALC01.cbl:10 secret.pem 3",
                "src/CALC01.cbl:10、docs/_重要度.txt：1"]:
        row = _only(monkeypatch, source_ref=ref, source_text="x")
        assert row["verdict"] == "unverified" and row["source"] == {"text": ""}, ref


def test_sensitive_name_glued_to_text_without_separator_blanks_the_side(monkeypatch):
    for ref in ["src/CALC01.cbl:10; config/secret.pemの3行目", "src/CALC01.cbl:10・id_rsa:1",
                "src/CALC01.cbl:10、config/server (1).key 3行目"]:
        row = _only(monkeypatch, source_ref=ref, source_text="x")
        assert row["verdict"] == "unverified" and row["source"] == {"text": ""}, ref


def test_doc_whose_name_contains_a_sensitive_word_but_is_not_sensitive_passes(monkeypatch):
    for ref in ["docs/API credentials 手順.md:12", "docs/credentials 管理/手順.md:3"]:
        row = _only(monkeypatch, source_ref=ref, source_text="x")
        assert row["verdict"] == "match" and row["source"]["doc_id"] == ref.split(":")[0], ref


def test_fullwidth_line_number_is_checked_as_a_line(monkeypatch):
    row = _only(monkeypatch, source_ref="src/CALC01.cbl:１２")
    assert row["verdict"] == "match" and row["source"]["line"] == 12 and row["source"]["loc"] == "12"
    assert _only(monkeypatch, source_ref="src/CALC01.cbl:９９９９")["verdict"] == "unverified"


def test_unencodable_surrogate_and_many_absolute_candidates_are_unverified(monkeypatch):
    for ref in ["/\ud800:1", "src/CALC01.cbl:10;/\ud800x:1", "/a:1;" * 100]:
        assert _only(monkeypatch, source_ref=ref)["verdict"] == "unverified", ref[:20]


def test_many_absolute_candidates_in_the_position_blank_the_side(monkeypatch):
    row = _only(monkeypatch, source_ref="src/CALC01.cbl:/1" + "/a:" * 100)
    assert row["verdict"] == "unverified" and row["source"] == {"text": ""}
    row = _only(monkeypatch, source_ref="src/CALC01.cbl:/1")
    assert row["verdict"] == "match" and row["source"]["doc_id"] == "src/CALC01.cbl" and row["source"]["loc"] == "/1"


def test_ref_with_separator_in_path_and_line_pieces_checked_by_each_separator(monkeypatch):
    row = _only(monkeypatch, spec_ref="docs/議事録、第2版.md:12")
    assert row["verdict"] == "match" and row["spec"]["doc_id"] == "docs/議事録、第2版.md" and row["spec"]["line"] == 12
    for ref in ["src/CALC01.cbl:3、999", "docs/詳細設計.md:9999; src/GONE.cbl:5", "src/NONE.cbl:5; src/CALC01.cbl:10"]:
        assert _only(monkeypatch, source_ref=ref)["verdict"] == "unverified", ref
    row = _only(monkeypatch, source_ref="src/CALC01.cbl:3、7-10")
    assert row["verdict"] == "match" and row["source"]["loc"] == "3,7-10" and "line" not in row["source"]


def test_overlong_field_is_unverified_without_error(monkeypatch):
    for ref in ["src/CALC01.cbl:" + "1," * 400, "src/CALC01.cbl:" + "9" * 20]:
        assert _only(monkeypatch, source_ref=ref)["verdict"] == "unverified"


def test_share_keeps_only_known_loc_and_also_fields():
    side = {"text": "t", "doc_id": "a.xlsx", "loc": "S!A1:B2", "x": 1,
            "also": [{"doc_id": "b.md", "loc": "3", "line": 3, "secret": "s"}, {"doc_id": "_重要度.txt", "loc": "1"}, "bad"]}
    out = shares._redact_importance_from_answer_data(
        {"reconciliation": [{"item": "i", "verdict": "match", "spec": side, "source": {"text": ""}}]})
    assert out["reconciliation"][0]["spec"] == {"text": "t", "doc_id": "a.xlsx", "loc": "S!A1:B2",
                                                "also": [{"doc_id": "b.md", "loc": "3", "line": 3}]}


_REAL_DOCS = {"docs/詳細設計.md", "src/CALC01.cbl", "src/CALC02.cbl", "docs/認証、credentials管理.md", "docs/環境変数(.env、.env.local)の設定手順.md"}


def _in_set(monkeypatch, ref, docs=_REAL_DOCS, counter=None):
    def exists(d, w, sp=None):
        if counter is not None:
            counter.append(d)
        return d in docs
    monkeypatch.setattr(A, "verify_doc_exists", exists)
    monkeypatch.setattr(ST, "_line_count", lambda d, w, sp=None: 200)
    out, _ = ST.verify_reconciliation([_row(verdict="match", source_ref=ref, source_text="x")], "w", None)
    return out[0]


def test_sensitive_name_at_the_edge_of_a_position_is_not_glued_to_the_next_one(monkeypatch):
    for ref in ["src/CALC01.cbl:10, /etc/ssl/server.key, src/CALC02.cbl:3", "src/CALC01.cbl:10、src/CALC02.cbl:id_rsa",
                "src/CALC01.cbl:10、「config/secret.pem」:3"]:
        row = _in_set(monkeypatch, ref)
        assert row["verdict"] == "unverified" and row["source"] == {"text": ""}, ref


def test_doc_path_with_separator_and_sensitive_word_inside_passes(monkeypatch):
    for doc in ["docs/認証、credentials管理.md", "docs/環境変数(.env、.env.local)の設定手順.md"]:
        row = _in_set(monkeypatch, doc + ":12")
        assert row["verdict"] == "match" and row["source"]["doc_id"] == doc and row["source"]["line"] == 12, doc


def test_many_distinct_fragments_stop_at_the_verify_limit_without_error(monkeypatch):
    calls: list[str] = []
    ref = "src/CALC01.cbl:10, " + ", ".join(f"memo{i}:x{i}" for i in range(40))
    row = _in_set(monkeypatch, ref, counter=calls)
    assert row["verdict"] == "unverified" and row["source"] == {"text": ""}
    assert len(calls) <= ST._RECON_VERIFY_MAX + 1


def test_world_root_is_resolved_once_per_call(tmp_path, monkeypatch):
    from sherpa import store, worlds
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "CALC01.cbl").write_text("x\n" * 200, encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "詳細設計.md").write_text("y\n" * 200, encoding="utf-8")
    calls = []
    monkeypatch.setattr(store, "get_world_root", lambda w: calls.append(w) or {"root_path": str(tmp_path)})
    rows, meta = ST.verify_reconciliation([_row(item=f"i{i}") for i in range(5)], "w", None)
    assert meta["unverified"] == 0 and len(rows) == 5
    assert calls == ["w"]
    assert worlds._pinned_root.get() is None
