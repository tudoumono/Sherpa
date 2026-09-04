"""「どう読み取ったか」バッジのデータ源（S2・表示のみ）の単体テスト（DB不要）。

`corpus_docs.provenance_summary` は取り込み時の来歴サイドカー `{md}.meta.json`（A1）を読んで画面表示用の
要約（method / confidence / legacy_backend? / has_conflicts?）を作るだけ（**追加の記録はしない**）。
ソース文書（md_path 無し）や meta.json 欠落は None＝バッジを出さない（後方互換）。

`doc_ledger.preview_documents` は派生MD を持つ文書にこの要約を付け、**物理パス（md_path）は出さない**。
"""
from __future__ import annotations

import json

from sherpa import corpus_docs, doc_ledger, worlds


def _write_meta(md, meta: dict):
    md.write_text("x", encoding="utf-8")
    (md.parent / (md.name + ".meta.json")).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


# ---- provenance_summary（純関数）----

def test_summary_ooxml(tmp_path):
    md = tmp_path / "a.docx.md"
    _write_meta(md, {"arm": "ooxml", "method": "ooxml", "confidence": 1.0, "notes": []})
    assert corpus_docs.provenance_summary(str(md)) == {"method": "ooxml", "confidence": 1.0}


def test_summary_pdf_text(tmp_path):
    md = tmp_path / "a.pdf.md"
    _write_meta(md, {"method": "pdf_text", "confidence": 0.9, "notes": []})
    assert corpus_docs.provenance_summary(str(md)) == {"method": "pdf_text", "confidence": 0.9}


def test_summary_markitdown_ocr(tmp_path):
    """視覚読み取り（VLM・markitdown_ocr）の来歴。tesseract 直の `ocr` アームは撤去済み（2026-07-08）。"""
    md = tmp_path / "scan.png.md"
    _write_meta(md, {"method": "markitdown_ocr", "confidence": 0.4, "notes": ["numeric_verified=false"]})
    assert corpus_docs.provenance_summary(str(md)) == {"method": "markitdown_ocr", "confidence": 0.4}


def test_summary_legacy_backend_from_notes(tmp_path):
    """旧形式(.xls 等)の前段変換は notes の legacy_backend=… を summary に上げる。"""
    md = tmp_path / "old.xls.md"
    _write_meta(md, {"method": "ooxml", "confidence": 1.0,
                     "notes": ["legacy_backend=libreoffice", "soffice=7.5"]})
    assert corpus_docs.provenance_summary(str(md)) == {
        "method": "ooxml", "confidence": 1.0, "legacy_backend": "libreoffice"}


def test_summary_office_com_backend(tmp_path):
    md = tmp_path / "old.ppt.md"
    _write_meta(md, {"method": "ooxml", "confidence": 1.0,
                     "notes": ["legacy_backend=office_com", "office_com_versions=word=16.0"]})
    assert corpus_docs.provenance_summary(str(md))["legacy_backend"] == "office_com"


def test_summary_has_conflicts_only_when_merge_produced_them(tmp_path):
    """A4 マージが差分を出した文書だけ has_conflicts=True。空/無しは付けない（後方互換）。"""
    md = tmp_path / "a.docx.md"
    _write_meta(md, {"method": "ooxml", "confidence": 1.0, "merge": "deterministic-v1",
                     "conflicts": [{"type": "numeric_only_in_secondary", "value": "9"}]})
    assert corpus_docs.provenance_summary(str(md)) == {
        "method": "ooxml", "confidence": 1.0, "has_conflicts": True}


def test_summary_empty_conflicts_omits_flag(tmp_path):
    md = tmp_path / "b.docx.md"
    _write_meta(md, {"method": "ooxml", "confidence": 1.0, "conflicts": []})
    out = corpus_docs.provenance_summary(str(md))
    assert "has_conflicts" not in out and out == {"method": "ooxml", "confidence": 1.0}


def test_summary_source_doc_and_missing_sidecar_are_none(tmp_path):
    assert corpus_docs.provenance_summary(None) is None          # ソース文書（md_path 無し）
    md = tmp_path / "c.md"; md.write_text("x", encoding="utf-8")   # サイドカー欠落＝後方互換
    assert corpus_docs.provenance_summary(str(md)) is None


def test_summary_malformed_or_methodless_is_none(tmp_path):
    md = tmp_path / "d.docx.md"
    (md.parent / (md.name + ".meta.json")).write_text("{ not json", encoding="utf-8")
    assert corpus_docs.provenance_summary(str(md)) is None       # 壊れ JSON
    md2 = tmp_path / "e.docx.md"
    _write_meta(md2, {"confidence": 1.0, "notes": []})            # method 欠落＝アンカー無し
    assert corpus_docs.provenance_summary(str(md2)) is None


def test_note_value_helper(tmp_path):
    assert corpus_docs._note_value(["legacy_backend=libreoffice"], "legacy_backend") == "libreoffice"
    assert corpus_docs._note_value(["vlm_provider=ollama"], "legacy_backend") is None
    assert corpus_docs._note_value(None, "legacy_backend") is None
    assert corpus_docs._note_value(["legacy_backend="], "legacy_backend") is None   # 空値は None


# ---- preview_documents（一覧 API の形）----

def test_preview_documents_attaches_provenance_and_hides_md_path(monkeypatch, tmp_path):
    """派生MD を持つ文書には provenance を付け、物理パス（md_path）は応答に出さない。"""
    md = tmp_path / "old.xls.md"
    _write_meta(md, {"method": "ooxml", "confidence": 1.0,
                     "notes": ["legacy_backend=libreoffice"], "conflicts": [{"x": 1}]})
    docs = [
        {"name": "4期/設計/old.xls", "doctype": "Excel(旧)", "branch": "office",
         "top_scope": "4期", "phase": "設計", "category": None, "md_path": str(md)},
        {"name": "4期/src/A.cbl", "doctype": "cobol", "branch": "source",
         "top_scope": "4期", "phase": "src", "category": None, "md_path": None},
    ]
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)      # root 解決成功＝fail-closed 早期return を回避
    monkeypatch.setattr(doc_ledger, "documents_for", lambda w, **kw: docs)
    out = {d["name"]: d for d in doc_ledger.preview_documents("w")}

    office = out["4期/設計/old.xls"]
    assert office["provenance"] == {"method": "ooxml", "confidence": 1.0,
                                    "legacy_backend": "libreoffice", "has_conflicts": True}
    assert "md_path" not in office                               # 物理パスは出さない

    # ソース文書（md_path 無し）は provenance を付けない＝後方互換。
    assert "provenance" not in out["4期/src/A.cbl"]
    assert "md_path" not in out["4期/src/A.cbl"]
