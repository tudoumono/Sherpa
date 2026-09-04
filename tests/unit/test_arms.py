"""MD化アーム・プラグイン基盤（A1）の単体テスト（DB不要）。

- レジストリ: `SHERPA_ARMS` 解決（既定・カスタム・未知名無視・重複除去）。
- ooxml_arm / pdf_text_arm の accepts / convert（office_md への委譲・バックエンド有無）。
- arms_sig drift: 構成変更で True・同一で False・旧 `.pdf_backend` からの移行。
- 来歴サイドカー `{rel}.md.meta.json` の生成（内容・決定性）。
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import zipfile

from pypdf import PdfWriter

from sherpa.ingest import arms, office_md
from sherpa.ingest.arms import ooxml_arm, pdf_text_arm

# arms は system_settings > env > 既定 で解決する（S1）。tests/unit/conftest.py の autouse fixture が
# 既定で `store.get_system_settings` を空 dict に固定する（DB 依存・他テスト残骸からの遮断・Low RV 対応）。
# system 優先を検証する下記の個別テストは、各テスト本体で store.get_system_settings を明示的に上書きする
# （conftest の autouse より後に適用されるため確実に効く）。

# test_office_md.py と同じ最小 OOXML（外部ライブラリ不要の経路）。
_DOCX_XML = """<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:body><w:p><w:r><w:t>本文テキストABC</w:t></w:r></w:p></w:body>
</w:document>"""


def _docx(dirpath, name="a.docx"):
    p = pathlib.Path(dirpath) / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", _DOCX_XML)
    return p


# ---- レジストリ（SHERPA_ARMS 解決）----

def test_default_arms_when_unset(monkeypatch):
    monkeypatch.delenv("SHERPA_ARMS", raising=False)
    assert arms.enabled_arm_names() == ["ooxml", "pdf_text"]      # 既定＝現行と同一構成
    names = [a.name for a in arms.enabled_arms()]
    assert names == ["ooxml", "pdf_text"]


def test_custom_arms_selection_and_order(monkeypatch):
    monkeypatch.setenv("SHERPA_ARMS", "pdf_text, ooxml")          # 空白 trim・指定順を保持
    assert arms.enabled_arm_names() == ["pdf_text", "ooxml"]
    monkeypatch.setenv("SHERPA_ARMS", "ooxml")
    assert arms.enabled_arm_names() == ["ooxml"]


def test_unknown_arm_ignored_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("SHERPA_ARMS", "ooxml,bogus,pdf_text")
    import logging
    with caplog.at_level(logging.WARNING):
        names = arms.enabled_arm_names()
    assert names == ["ooxml", "pdf_text"]                          # 未知名 bogus は落ちる（fail-safe）
    assert any("bogus" in r.message for r in caplog.records)       # 警告に未知名が出る


def test_duplicate_arms_deduped(monkeypatch):
    monkeypatch.setenv("SHERPA_ARMS", "ooxml,ooxml,pdf_text")
    assert arms.enabled_arm_names() == ["ooxml", "pdf_text"]       # 重複は畳む（二重変換防止）


def test_empty_arms_is_none(monkeypatch):
    monkeypatch.setenv("SHERPA_ARMS", " , ")
    assert arms.enabled_arm_names() == []                          # 全部空＝有効アーム無し（fail-safe）


# ---- 優先順（system_settings > env > 既定・S1 2026-07-08-設定分離とUI整備.md）----

def test_system_settings_arms_take_priority_over_env(monkeypatch):
    from sherpa import store
    monkeypatch.setenv("SHERPA_ARMS", "ooxml")                     # env は ooxml のみ
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {"arms_enabled": ["pdf_text", "ooxml"]})
    assert arms.enabled_arm_names() == ["pdf_text", "ooxml"]       # 全体設定が env に優先（順序も尊重）


def test_system_settings_empty_arms_falls_back_to_env(monkeypatch):
    from sherpa import store
    monkeypatch.setenv("SHERPA_ARMS", "ooxml")
    monkeypatch.setattr(store, "get_system_settings", lambda: {"arms_enabled": []})
    assert arms.enabled_arm_names() == ["ooxml"]                   # 空リスト＝未設定扱い＝env へ


def test_system_settings_unset_arms_falls_back_to_default(monkeypatch):
    from sherpa import store
    monkeypatch.delenv("SHERPA_ARMS", raising=False)
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    assert arms.enabled_arm_names() == ["ooxml", "pdf_text"]       # env も無し＝コード既定


def test_system_settings_unreadable_is_failsafe_to_env(monkeypatch):
    """store を読めない文脈（MCP サブプロセスは PG creds 無し・DB 停止）は env へ倒す（fail-safe）。"""
    from sherpa import store

    def _boom():
        raise RuntimeError("no PG creds (MCP subprocess)")

    monkeypatch.setattr(store, "get_system_settings", _boom)
    monkeypatch.setenv("SHERPA_ARMS", "ooxml")
    assert arms.enabled_arm_names() == ["ooxml"]


def test_system_settings_arms_unknown_names_filtered(monkeypatch):
    from sherpa import store
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {"arms_enabled": ["ooxml", "bogus"]})
    monkeypatch.setattr(arms, "_warned_unknown", set())
    assert arms.enabled_arm_names() == ["ooxml"]                   # 未知名は除外（既存 fail-safe）


def test_known_and_env_default_arm_names(monkeypatch):
    monkeypatch.delenv("SHERPA_ARMS", raising=False)
    # 登録済み全アーム（ソート済み・tesseract直OCRとMarkItDownは不採用）。
    assert arms.known_arm_names() == ["ooxml", "pdf_text", "vision"]
    assert arms.env_default_arm_names() == ["ooxml", "pdf_text"]   # env/既定の実効（system 抜き・既定は不変）
    monkeypatch.setenv("SHERPA_ARMS", "ooxml")
    assert arms.env_default_arm_names() == ["ooxml"]               # env を反映（system は見ない）


# ---- ooxml_arm / pdf_text_arm（accepts / convert・office_md 委譲）----

def test_ooxml_arm_accepts_and_convert():
    arm = ooxml_arm.OoxmlArm()
    assert arm.name == "ooxml"
    assert arm.accepts(pathlib.Path("x.docx")) and arm.accepts(pathlib.Path("x.XLSX"))
    assert not arm.accepts(pathlib.Path("x.pdf"))
    d = tempfile.mkdtemp()
    res = arm.convert(_docx(d))
    assert res is not None and res.method == "ooxml" and res.confidence == 1.0
    assert "本文テキストABC" in res.md            # document-ir 経由の human_md.render_docx が生成（H2）


def test_ooxml_arm_broken_returns_none():
    """壊れた docx（非zip）は document-ir 構築自体が失敗する（fail-closed）:
    `ArmResult(md=None, document=None)` を返し、notes に `document_ir_failed:` 理由を残す
    （bare None ではない＝呼び出し元が `document_ir_failed` を計上できるようにするため）。"""
    d = tempfile.mkdtemp()
    bad = pathlib.Path(d) / "b.docx"
    bad.write_bytes(b"not a zip")
    res = ooxml_arm.OoxmlArm().convert(bad)
    assert res is not None
    assert res.md is None and res.document is None
    assert any(n.startswith("document_ir_failed:") for n in res.notes)


def test_pdf_text_arm_accepts_and_backend_gating():
    arm = pdf_text_arm.PdfTextArm()
    assert arm.name == "pdf_text" and arm.accepts(pathlib.Path("x.pdf"))
    assert not arm.accepts(pathlib.Path("x.docx"))
    o_b, o_p = office_md._pdf_backend, office_md._pdf_pages
    try:
        office_md._pdf_backend = lambda: None                      # バックエンド無＝変換不可（None）
        assert arm.convert(pathlib.Path("x.pdf")) is None
        office_md._pdf_backend = lambda: "pypdf"                    # バックエンド有＋本文あり＝変換成功
        office_md._pdf_pages = lambda p: ["税率10%の説明"]
        res = arm.convert(pathlib.Path("x.pdf"))
        assert res is not None and res.method == "pdf_text" and res.confidence == 0.9
        assert "税率10%" in res.md and res.notes == ["backend=pypdf"]
    finally:
        office_md._pdf_backend, office_md._pdf_pages = o_b, o_p


# ---- arms_sig drift（構成変更/同一/旧マーカー移行）----

def test_arms_sig_drift_same_config_false(monkeypatch):
    monkeypatch.delenv("SHERPA_ARMS", raising=False)
    d = pathlib.Path(tempfile.mkdtemp())
    o_b = office_md._pdf_backend
    try:
        office_md._pdf_backend = lambda: None
        office_md._write_arms_sig_marker(d)                        # 現構成でマーカーを書く
        assert office_md.arms_sig_drift(d) is False                # 同一構成＝drift 無し（無限ループしない）
    finally:
        office_md._pdf_backend = o_b


def test_arms_sig_drift_on_backend_change(monkeypatch):
    monkeypatch.delenv("SHERPA_ARMS", raising=False)
    d = pathlib.Path(tempfile.mkdtemp())
    o_b = office_md._pdf_backend
    try:
        office_md._pdf_backend = lambda: None
        office_md._write_arms_sig_marker(d)
        office_md._pdf_backend = lambda: "pypdf"                   # 後からバックエンド導入
        assert office_md.arms_sig_drift(d) is True                 # PDF 変換可否が変わった＝作り直す
    finally:
        office_md._pdf_backend = o_b


def test_arms_sig_drift_on_arm_change(monkeypatch):
    d = pathlib.Path(tempfile.mkdtemp())
    o_b = office_md._pdf_backend
    try:
        office_md._pdf_backend = lambda: None
        monkeypatch.setenv("SHERPA_ARMS", "ooxml,pdf_text")
        office_md._write_arms_sig_marker(d)
        monkeypatch.setenv("SHERPA_ARMS", "ooxml")                 # 有効アーム構成を変更
        assert office_md.arms_sig_drift(d) is True
    finally:
        office_md._pdf_backend = o_b


def test_arms_sig_drift_migrates_old_pdf_marker(monkeypatch):
    """旧 `.pdf_backend` マーカーしか無い派生 dir を既定アーム構成と読み替えて判定（後方互換）。

    DOC-IR-001.5（修正3）で document-ir 版（`docir`）成分は arms_sig から分離済み（`.document_ir_sig` という
    別マーカーへ移動＝`es_index._arms_config_sig()` 経由の全 world ES 再索引誘発を避けるため）。よって
    arms_sig の読み替えロジック自体（PDF バックエンドの一致判定）は DOC-IR-001 以前の挙動に戻る。
    """
    monkeypatch.delenv("SHERPA_ARMS", raising=False)               # 既定 ooxml,pdf_text
    d = pathlib.Path(tempfile.mkdtemp())
    o_b = office_md._pdf_backend
    try:
        (d / office_md._OLD_PDF_MARKER).write_text("none", encoding="utf-8")
        office_md._pdf_backend = lambda: None
        assert office_md.arms_sig_drift(d) is False                # 旧「none」＝既定構成・バックエンド無しと一致
        office_md._pdf_backend = lambda: "pypdf"                   # バックエンドが変わった＝drift
        assert office_md.arms_sig_drift(d) is True
    finally:
        office_md._pdf_backend = o_b


def test_arms_sig_drift_no_marker_baseline(monkeypatch):
    """マーカー皆無＝既定構成・バックエンド無しを基準に判定（marker 書込失敗時の無限ループ回避）。

    DOC-IR-001.5（修正3）で document-ir 版成分は arms_sig から分離済み（`test_arms_sig_drift_migrates_old_pdf_marker`
    docstring 参照）＝DOC-IR-001 以前の挙動に戻る。
    """
    monkeypatch.delenv("SHERPA_ARMS", raising=False)
    d = pathlib.Path(tempfile.mkdtemp())
    o_b = office_md._pdf_backend
    try:
        office_md._pdf_backend = lambda: None
        assert office_md.arms_sig_drift(d) is False                # 既定＋バックエンド無し＝基準と一致
        office_md._pdf_backend = lambda: "pypdf"
        assert office_md.arms_sig_drift(d) is True
    finally:
        office_md._pdf_backend = o_b


def test_arms_sig_drift_unwritable_dir_does_not_loop(monkeypatch):
    """RV Med（2026-07-08）: marker を書けない派生 dir では drift=True を返し続けない（毎 sync フルリビルド
    のループ防止）。書込 probe が失敗 → 警告して据え置き（False）。書ける dir では従来どおり True。"""
    monkeypatch.delenv("SHERPA_ARMS", raising=False)
    d = pathlib.Path(tempfile.mkdtemp())
    o_b = office_md._pdf_backend
    try:
        office_md._pdf_backend = lambda: "pypdf"                   # 基準（none）と不一致＝本来は drift
        assert office_md.arms_sig_drift(d) is True                 # 書ける dir → True（再ビルドで marker が残る）
        d.chmod(0o555)                                             # 読み取り専用＝marker を永続できない
        try:
            assert office_md.arms_sig_drift(d) is False            # ループ回避＝据え置き
        finally:
            d.chmod(0o755)
    finally:
        office_md._pdf_backend = o_b


def test_arms_sig_drift_existing_marker_mismatch_unwritable_dir_does_not_loop(monkeypatch):
    """RV Med（Codex gpt-5.5/xhigh・2026-07-08 R1）: **marker が既に存在**するが今の署名と不一致
    （例: tesseract 直の `ocr` アーム撤去で署名フォーマットが変わり、旧 `;ocr=...` 付き marker が不一致に
    なるケース）でも、dir が書けなければ drift=True を返し続けない（no-marker ケースと同じ fail-safe）。"""
    monkeypatch.delenv("SHERPA_ARMS", raising=False)                # 既定 ooxml,pdf_text
    d = pathlib.Path(tempfile.mkdtemp())
    o_b = office_md._pdf_backend
    try:
        office_md._pdf_backend = lambda: None
        # 旧フォーマット（;ocr= 付き）の marker を直接書く＝今の署名（;ocr= 無し）とは文字列不一致。
        (d / office_md._ARMS_SIG_MARKER).write_text(
            "arms=ooxml,pdf_text;pdf=none;legacy=none;md=none;ocr=none;vlm=none", encoding="utf-8")
        assert office_md.arms_sig_drift(d) is True                  # 書ける dir → True（再ビルドで marker が更新される）
        d.chmod(0o555)                                              # 読み取り専用＝marker を更新できない
        try:
            assert office_md.arms_sig_drift(d) is False             # ループ回避＝据え置き（no-marker と同じ fail-safe）
        finally:
            d.chmod(0o755)
    finally:
        office_md._pdf_backend = o_b


def test_unknown_arm_warns_once(monkeypatch, caplog):
    """未知アーム名の警告はプロセス内1回だけ（sync/scan 毎のスパム防止・RV Low）。"""
    monkeypatch.setenv("SHERPA_ARMS", "ooxml,nosuch-arm")
    monkeypatch.setattr(arms, "_warned_unknown", set())            # プロセス内状態をテスト用にリセット
    with caplog.at_level("WARNING"):
        assert arms.enabled_arm_names() == ["ooxml"]
        assert arms.enabled_arm_names() == ["ooxml"]               # 2回呼んでも警告は増えない
    warnings = [r for r in caplog.records if "未知アーム" in r.getMessage()]
    assert len(warnings) == 1


# ---- 来歴サイドカー（build_derived が {rel}.md.meta.json を生成・決定的）----

def test_provenance_sidecar_written_for_ooxml(monkeypatch):
    monkeypatch.delenv("SHERPA_ARMS", raising=False)
    d = tempfile.mkdtemp()
    src = pathlib.Path(d) / "src"; src.mkdir()
    _docx(src, "doc.docx")
    der = pathlib.Path(d) / "derived"
    rep = office_md.build_derived(src, der)
    assert rep["converted"] == 1
    md = der / "doc.docx.md"
    side = der / "doc.docx.md.meta.json"
    assert md.is_file() and side.is_file()
    meta = json.loads(side.read_text(encoding="utf-8"))
    assert meta == {"arm": "ooxml", "method": "ooxml", "confidence": 1.0, "notes": []}


def test_provenance_sidecar_records_pdf_backend(monkeypatch):
    monkeypatch.delenv("SHERPA_ARMS", raising=False)
    d = tempfile.mkdtemp()
    src = pathlib.Path(d) / "src"; src.mkdir()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with (src / "doc.pdf").open("wb") as stream:
        writer.write(stream)
    der = pathlib.Path(d) / "derived"
    o_b, o_p = office_md._pdf_backend, office_md._pdf_pages
    try:
        office_md._pdf_backend = lambda: "pypdf"
        office_md._pdf_pages = lambda p: ["税率10%の説明"]
        office_md.build_derived(src, der)
        meta = json.loads((der / "doc.pdf.md.meta.json").read_text(encoding="utf-8"))
        assert meta == {"arm": "pdf_text", "method": "pdf_text",
                        "confidence": 0.9, "notes": ["backend=pypdf"]}
    finally:
        office_md._pdf_backend, office_md._pdf_pages = o_b, o_p


def test_provenance_sidecar_is_deterministic(monkeypatch):
    """同一入力・同一構成なら bytes 一致（タイムスタンプ無し・キー順固定）。"""
    monkeypatch.delenv("SHERPA_ARMS", raising=False)
    d = tempfile.mkdtemp()
    src = pathlib.Path(d) / "src"; src.mkdir()
    _docx(src, "doc.docx")
    der = pathlib.Path(d) / "derived"
    office_md.build_derived(src, der)
    first = (der / "doc.docx.md.meta.json").read_bytes()
    office_md.build_derived(src, der)                              # 全消去→再作成（鏡）でも同一 bytes
    assert (der / "doc.docx.md.meta.json").read_bytes() == first


def test_sidecar_not_grepped_or_ledgered(monkeypatch):
    """サイドカー（`.meta.json`）は grep に載らない。RAG MD は `grep_tool.rag_grep_enabled` の
    向きどおりに載る（常時 ON＝載る・legacy 側は直接差し替えて模擬——TOGGLE-RM・2026-09-03 で
    グローバルな系統切替トグルは撤去済みのため env では OFF にできない）。台帳はsource起点。"""
    monkeypatch.delenv("SHERPA_ARMS", raising=False)
    d = tempfile.mkdtemp()
    src = pathlib.Path(d) / "src"; src.mkdir()
    _docx(src, "doc.docx")
    der = pathlib.Path(d) / "derived"
    office_md.build_derived(src, der)
    # `.meta.json` はMarkdownではない。E3以降は通常MDと並行評価用RAG MDの2つが生成される
    # （§8.1 三階層のフォルダ分離により物理的には md/rag の別ディレクトリへ配置される）。
    from sherpa import grep_tool, worlds
    der_rag = der.parent / "rag"
    md_files = [p for root in (der, der_rag) for p in root.rglob("*")
               if p.is_file() and p.suffix.lower() in grep_tool._MD_EXT]
    assert sorted(p.name for p in md_files) == ["doc.docx.md", "doc.docx.rag.md"]

    rag_path = der_rag / "doc.docx.rag.md"
    rag_path.write_text(rag_path.read_text(encoding="utf-8") + "\nRAG_ONLY_MARKER\n", encoding="utf-8")
    monkeypatch.setattr(worlds, "world_dir", lambda _world: src)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda _world: der)
    monkeypatch.setattr(worlds, "derived_rag_dir", lambda _world: der_rag)

    # legacy モード（直接差し替えて模擬）: RAG MD だけにある語を legacy grep は拾わない。
    monkeypatch.setattr(grep_tool, "rag_grep_enabled", lambda: False)
    assert grep_tool.grep_search("RAG_ONLY_MARKER", world="v1") == []

    # 既定（常時 ON）: 同じ語を RAG MD 側から拾う（doc_id は原本のまま＝派生名は露出しない）。
    monkeypatch.setattr(grep_tool, "rag_grep_enabled", lambda: True)
    hits = grep_tool.grep_search("RAG_ONLY_MARKER", world="v1")
    assert len(hits) == 1 and hits[0]["doc_id"] == "doc.docx"
