"""ラスタ画像の台帳/検索 first-class 化（A4 スライス）の単体テスト（DB不要）。

最重要の契約: **既定構成（vision 無効）では scan_report / 一覧 / カウントが従来どおり不変**
（画像は「その他」に落ちる）。vision 有効 かつ VLM 実効可時**だけ**、画像を doctype「画像」で
台帳・ES 対象に載せる。

worlds.world_dir / derived_md_dir を tmp に差し替え、si.safe_files は実際に tmp を走査する。vision の
VLM は既定でローカル Ollama（設定として使える扱い＝ネットワーク接続はしない）なので、追加のモックなしで
`vlm_usable()` は True になる。VLM 実行自体はしない（派生MD の有無だけで判定するため）。

tesseract 直の `ocr` アームは撤去済み（2026-07-08）。
"""
from __future__ import annotations

from sherpa import corpus_docs, worlds


def _world(monkeypatch, tmp_path):
    wd = tmp_path / "world"; wd.mkdir()
    der = tmp_path / "derived"; der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    # §8.1 三階層＝rag（RAG正本）は md と別ディレクトリ（`der` の兄弟）。
    monkeypatch.setattr(worlds, "derived_rag_dir", lambda w: der.parent / "rag")
    return wd, der


def test_default_config_png_without_derived_is_reported_as_pending_failure(monkeypatch, tmp_path):
    """PNGは通常対象なので、sync前など派生が無い状態をその他ではなく変換失敗として可視化する。"""
    monkeypatch.delenv("SHERPA_ARMS", raising=False)          # 既定 ooxml,pdf_text
    wd, der = _world(monkeypatch, tmp_path)
    (wd / "scan.png").write_bytes(b"img")
    (wd / "note.txt").write_text("本文", encoding="utf-8")
    rep = corpus_docs.scan_report("w")
    assert rep["skipped_other"] == 0
    assert rep["office_failed"] == 1
    assert rep["skipped_ext"].get(".png") == 1
    assert "画像" not in rep["by_doctype"]
    assert rep["indexed"] == 1                                # note.txt のみ
    assert [d["name"] for d in corpus_docs.world_documents("w")] == ["note.txt"]   # 画像は台帳外＝ES 対象外


def test_png_metadata_is_first_class_even_when_vision_enabled(monkeypatch, tmp_path):
    """PNGの決定的metadata派生があればdoctype「画像」で台帳・検索対象に載せる。"""
    monkeypatch.setenv("SHERPA_ARMS", "ooxml,pdf_text,vision")
    wd, der = _world(monkeypatch, tmp_path)
    (wd / "scan.png").write_bytes(b"img")
    (der / "scan.png.md").write_text("画像内容は未解釈である。", encoding="utf-8")
    rep = corpus_docs.scan_report("w")
    assert rep["by_doctype"].get("画像") == 1
    assert rep["indexed"] == 1
    assert ".png" not in rep["skipped_ext"] and rep["skipped_other"] == 0
    docs = corpus_docs.world_documents("w")
    img = next(d for d in docs if d["name"] == "scan.png")
    assert img["doctype"] == "画像" and img["branch"] == "office"
    assert img["md_path"] == str(der / "scan.png.md")
    assert img["label"] == "使えます（画像メタデータ・内容未解釈）"


def test_vision_enabled_image_without_md_is_failed(monkeypatch, tmp_path):
    """vision 有効だが派生MD が無い（VLM で文字が取れなかった）画像は変換失敗＝台帳に載せない。"""
    monkeypatch.setenv("SHERPA_ARMS", "ooxml,pdf_text,vision")
    wd, der = _world(monkeypatch, tmp_path)
    (wd / "scan.png").write_bytes(b"img")                     # 派生MD 無し（VLM 失敗相当）
    rep = corpus_docs.scan_report("w")
    assert rep["office_failed"] == 1
    assert rep["skipped_ext"].get(".png") == 1
    assert "画像" not in rep["by_doctype"] and rep["indexed"] == 0
    assert "scan.png" not in [d["name"] for d in corpus_docs.world_documents("w")]


def test_vision_disabled_png_metadata_is_still_available(monkeypatch, tmp_path):
    """visionアームが無効でもPNG metadata派生は台帳へ載る。"""
    monkeypatch.setenv("SHERPA_ARMS", "ooxml,pdf_text")       # vision 無効
    wd, der = _world(monkeypatch, tmp_path)
    (wd / "scan.png").write_bytes(b"img")
    (der / "scan.png.md").write_text("本文", encoding="utf-8")  # 派生MD が有っても載せない（アーム無効）
    rep = corpus_docs.scan_report("w")
    assert rep["by_doctype"] == {"画像": 1} and rep["skipped_other"] == 0
    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["scan.png"]
    assert docs[0]["label"] == "使えます（画像メタデータ・内容未解釈）"


def test_structured_listing_includes_image_only_pdf_with_rag_md(monkeypatch, tmp_path):
    """通常MDがなくてもEvidence RAG MDがあればstructured索引だけは文書を列挙できる。"""
    monkeypatch.setenv("SHERPA_ARMS", "ooxml,pdf_text")
    wd, der = _world(monkeypatch, tmp_path)
    der_rag = der.parent / "rag"; der_rag.mkdir()
    (wd / "scan.pdf").write_bytes(b"%PDF fixture")
    rag_md = der_rag / "scan.pdf.rag.md"
    rag_md.write_text("画像内容は未解釈である。", encoding="utf-8")

    assert corpus_docs.world_documents("w") == []
    docs = corpus_docs.world_documents("w", include_rag=True)

    assert [doc["name"] for doc in docs] == ["scan.pdf"]
    assert docs[0]["md_path"] == str(rag_md)
    assert docs[0]["label"] == "使えます（RAG MD化）"


def test_include_rag_prefers_rag_md_over_legacy_md_when_both_exist(monkeypatch, tmp_path):
    """D1: 両方が存在する場合、include_rag=True は legacy `.md` より `.rag.md` を優先する
    （`grep_tool.preferred_derived_name` と同じ優先順位＝grep/ES/グラフが同じ物理ファイルを見る）。
    include_rag=False（既定）は従来どおり legacy のみを見る。"""
    monkeypatch.setenv("SHERPA_ARMS", "ooxml,pdf_text")
    wd, der = _world(monkeypatch, tmp_path)
    der_rag = der.parent / "rag"; der_rag.mkdir()
    (wd / "a.docx").write_bytes(b"docx fixture")
    legacy_md = der / "a.docx.md"
    legacy_md.write_text("人間向けMD", encoding="utf-8")
    rag_md = der_rag / "a.docx.rag.md"
    rag_md.write_text("RAG向けMD", encoding="utf-8")

    legacy_docs = corpus_docs.world_documents("w")
    assert legacy_docs[0]["md_path"] == str(legacy_md)

    rag_docs = corpus_docs.world_documents("w", include_rag=True)
    assert rag_docs[0]["md_path"] == str(rag_md)
    assert rag_docs[0]["label"] == "使えます（RAG MD化）"
