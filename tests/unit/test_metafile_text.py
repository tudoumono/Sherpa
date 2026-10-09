"""WMF/EMF の描画命令から文字と埋込ビットマップを取り出す（OBS-08 の C と B 核）。

実環境の資料は使わず、合成した最小の WMF/EMF/docx（`tests/_metafile_builders.py`）で固定する。
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest  # noqa: E402

import _metafile_builders as mb  # noqa: E402
from sherpa import json_io  # noqa: E402
from sherpa.ingest import evidence_ir, metafile_text, ocr_router, office_md  # noqa: E402


@pytest.fixture(autouse=True)
def _no_libreoffice(monkeypatch, tmp_path):
    """この file は埋込ビットマップと文字の抽出が対象。全体描画（LibreOffice）は test_metafile_render.py で見る。"""
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(tmp_path / "no-such-soffice"))


# ---- 文字（C） ------------------------------------------------------------------------------

def test_wmf_text_follows_font_charset_and_reads_top_to_bottom():
    data = mb.wmf([
        mb.wmf_font(128),                                  # SHIFTJIS_CHARSET -> cp932
        mb.wmf_select(0),
        mb.wmf_textout("検査結果".encode("cp932"), 100, 20),
        mb.wmf_exttextout(b"ABC", 10, 20),
        mb.wmf_textout(b"title", 5, 5),
    ])
    content = metafile_text.extract(data)
    assert content.kind == "wmf" and content.reason is None
    # y が小さい行が先、同じ y は x の小さい順。描画順（title が最後）には従わない。
    assert content.lines == ["title", "ABC 検査結果"]


def test_wmf_without_placeable_header_and_ansi_charset():
    data = mb.wmf([mb.wmf_font(0), mb.wmf_select(0), mb.wmf_textout("café".encode("cp1252"), 0, 0)],
                  placeable=False)
    assert metafile_text.extract(data).lines == ["café"]


def test_emf_wide_and_ansi_text_use_their_own_encodings():
    data = mb.emf([
        mb.emf_exttextout_w("こんにちは", 10, 50),
        mb.emf_font(1, 128),
        mb.emf_select(1),
        mb.emf_exttextout_a("日本語".encode("cp932"), 10, 10),
    ])
    content = metafile_text.extract(data)
    assert content.kind == "emf"
    assert content.lines == ["日本語", "こんにちは"]


def test_emf_plus_dual_text_is_not_doubled():
    data = mb.emf([
        mb.emf_exttextout_w("Total 100", 10, 50),               # EMF 側の描画
        mb.emf_plus_drawstring("Total 100", 10.0, 50.0),        # 同じ文字の EMF+ 側（デュアル）
        mb.emf_plus_drawstring("Only plus", 1.0, 5.0),
    ])
    assert metafile_text.extract(data).lines == ["Only plus", "Total 100"]


@pytest.mark.parametrize("data", [
    b"",
    b"\x01\x00\x00\x00" + b"\x00" * 36 + b" EMF" + b"\xff" * 64,         # 記録の大きさが壊れた EMF
    mb.wmf([mb.wmf_textout(b"cut", 0, 0)])[:40],                         # 途中で切れた WMF
    mb.emf([mb.emf_exttextout_w("cut", 0, 0)])[:140],                    # 途中で切れた EMF
    os.urandom(4096),
])
def test_malformed_input_never_raises_and_reports_a_reason(data):
    content = metafile_text.extract(data)
    assert isinstance(content.lines, list)
    assert content.reason is not None or content.lines == []


def test_extracted_text_is_capped():
    records = [mb.wmf_textout(("x" * 200).encode(), 0, index) for index in range(100)]
    lines = metafile_text.extract(mb.wmf(records)).lines
    assert sum(len(line) for line in lines) <= metafile_text.MAX_TEXT_CHARS


# ---- ビットマップ（B 核） -------------------------------------------------------------------

def test_embedded_bitmaps_become_png_small_ones_are_skipped_and_duplicates_dropped():
    big = mb.dib(64, 48)
    wmf = metafile_text.extract(mb.wmf([
        mb.wmf_stretchdib(big), mb.wmf_stretchdib(big), mb.wmf_stretchdib(mb.dib(8, 8)),
    ]))
    assert [(item.width, item.height) for item in wmf.bitmaps] == [(64, 48)]
    assert wmf.bitmaps[0].png.startswith(b"\x89PNG")
    emf = metafile_text.extract(mb.emf([mb.emf_stretchdibits(big)]))
    assert [(item.width, item.height) for item in emf.bitmaps] == [(64, 48)]


# ---- ルーター ---------------------------------------------------------------------------------

def _docx_world(tmp_path: Path, emf: bytes) -> Path:
    wd = tmp_path / "world"
    wd.mkdir()
    (wd / "a.docx").write_bytes(mb.docx_with_media("image1.emf", emf))
    return wd


def _figure_emf() -> bytes:
    return mb.emf([
        mb.emf_exttextout_w("帳票の見出し", 10, 10),
        mb.emf_plus_drawstring("Total 100", 10.0, 40.0),
        mb.emf_stretchdibits(mb.dib(64, 48)),
    ])


def _build(monkeypatch, tmp_path: Path, emf: bytes | None = None):
    monkeypatch.setenv("SHERPA_MCP_ARMS", "ooxml,pdf_text")
    wd = _docx_world(tmp_path, emf if emf is not None else _figure_emf())
    dmd = tmp_path / "derived" / "md"
    rep = office_md.build_derived(wd, dmd, world="metafile-world")
    assert not rep.get("error") and rep["rag_failed"] == 0 and rep["evidence_ir_failed"] == 0, rep
    return wd, dmd


def _route(dmd: Path) -> dict:
    return json.loads((dmd.parent / "ir" / "a.docx.ocr_route.json").read_text(encoding="utf-8"))


def test_router_selects_child_png_linked_to_its_parent_metafile(monkeypatch, tmp_path):
    _, dmd = _build(monkeypatch, tmp_path)
    decisions = _route(dmd)["decisions"]
    parent = next(item for item in decisions if item["reason_code"] == ocr_router.METAFILE_EXPANDED)
    child = next(item for item in decisions if item["reason_code"] == "metafile_embedded_bitmap")
    assert parent["status"] == "excluded" and child["status"] == "selected"
    assert child["target_evidence_id"] == parent["target_evidence_id"]      # 同じ図の位置
    assert child["detail"]["parent_asset_sha256"] == parent["asset_sha256"]
    assert child["detail"]["parent_route_input_id"] == parent["route_input_id"]
    assert child["media_type"] == "image/png" and child["pixel_size"] == [64, 48]
    assets = dmd.parent / "rag" / "a.docx.assets"
    assert (assets / child["asset_rel_path"]).is_file()
    assert not (tmp_path / "world" / "_metafile").exists()                   # 資料フォルダには書かない


def test_metafile_without_bitmaps_and_without_libreoffice_is_unsupported(monkeypatch, tmp_path):
    _, dmd = _build(monkeypatch, tmp_path, mb.emf([mb.emf_exttextout_w("文字だけ", 0, 0)]))
    decisions = _route(dmd)["decisions"]
    assert [item["reason_code"] for item in decisions] == [ocr_router.METAFILE_RENDER_UNAVAILABLE]


# ---- rag.md / 人間向け MD ----------------------------------------------------------------------

def test_rag_md_and_human_md_carry_the_figure_text_block(monkeypatch, tmp_path):
    _, dmd = _build(monkeypatch, tmp_path)
    rag = (dmd.parent / "rag" / "a.docx.rag.md").read_text(encoding="utf-8")
    block = "図の中の文字（元の値）\n帳票の見出し\nTotal 100"
    assert block in rag
    # 図（画像）の chunk の直後・次の段落より前に置く。
    assert rag.index("画像内容は未解釈") < rag.index(block) < rag.index("後の段落")
    chunks = [json.loads(line) for line in
              json_io.read_text_maybe_gzip(dmd.parent / "rag" / "a.docx.rag_chunks.jsonl").splitlines()]
    assert any(chunk.get("content_type") == "figure_text" for chunk in chunks)
    human = (dmd / "a.docx.md").read_text(encoding="utf-8")
    block = "図の中の文字（元の値）\n- 帳票の見出し\n- Total 100"
    # 図を含む段落の位置（前の段落の後・後の段落の前）に出る。
    assert human.index("前の段落") < human.index(block) < human.index("後の段落")


def test_xlsx_figure_text_follows_the_anchor_cell(tmp_path):
    path = tmp_path / "b.xlsx"
    path.write_bytes(mb.xlsx_with_media("image1.emf", _figure_emf()))          # アンカーは B12
    human = office_md._xlsx_md(path)
    assert human is not None
    block = "図の中の文字（元の値）（B12付近）\n- 帳票の見出し\n- Total 100"
    assert human.index("上3") < human.index(block) < human.index("下20")      # 上の表の直後・下の表の前

    path.write_bytes(mb.xlsx_with_media("image1.emf", _figure_emf(), anchor_row0=0, anchor_col0=3))
    human = office_md._xlsx_md(path)
    assert human.index("上1") < human.index("図の中の文字（元の値）（D1付近）") < human.index("下20")


def _legacy_world(monkeypatch, tmp_path):
    from sherpa.ingest.arms import legacy_convert

    wd = tmp_path / "world"
    wd.mkdir()
    (wd / "old.doc").write_bytes(b"legacy-binary-not-a-real-doc")
    dmd = tmp_path / "derived" / "md"
    cache = legacy_convert.cache_root_for(dmd)
    converted = cache / "old.doc.docx"
    converted.parent.mkdir(parents=True)
    converted.write_bytes(mb.docx_with_media("image1.emf", _figure_emf()))
    monkeypatch.setattr(legacy_convert, "legacy_exts", lambda: {".doc"})
    monkeypatch.setattr(legacy_convert, "ensure_ooxml", lambda src, rel, cache_root: (converted, []))
    monkeypatch.setenv("SHERPA_MCP_ARMS", "ooxml,pdf_text")
    rep = office_md.build_derived(wd, dmd)
    assert not rep.get("error") and rep["rag_failed"] == 0, rep
    key = Path(str(converted) + ".key")
    key.write_text(legacy_convert._source_key(wd / "old.doc"), encoding="utf-8")
    return wd, dmd, converted


def test_already_ingested_legacy_doc_gets_the_figure_text_from_the_cached_conversion(monkeypatch, tmp_path):
    from sherpa.ingest.arms import legacy_convert

    wd, dmd, converted = _legacy_world(monkeypatch, tmp_path)
    md_path = dmd / "old.doc.md"
    assert "図の中の文字（元の値）" in md_path.read_text(encoding="utf-8")
    # 旧版で取り込み済みだった状態: 図の文字が無い MD・版の記録なし。
    md_path.write_text("前の段落\n\n後の段落", encoding="utf-8")
    manifest = dmd.parent / "ir" / "old.doc.derived.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data.pop("asset_versions", None)
    manifest.write_text(json.dumps(data), encoding="utf-8")

    def _no_conversion(*_args, **_kwargs):
        raise AssertionError("変換を再実行してはいけない")

    monkeypatch.setattr(legacy_convert, "convert_to_ooxml", _no_conversion)
    assert office_md.human_md_sig_drift(wd, dmd) is True
    result = office_md.refresh_human_md(wd, dmd)
    assert result["human_md_failed"] == 0 and result["human_md_generated"] == 1
    text = md_path.read_text(encoding="utf-8")
    assert text.index("前の段落") < text.index("図の中の文字（元の値）\n- 帳票の見出し") < text.index("後の段落")
    assert office_md.human_md_sig_drift(wd, dmd) is False


def test_emf_smalltextout_reads_8bit_and_wide_text_with_or_without_bounds():
    data = mb.emf([
        mb.emf_font(1, 128), mb.emf_select(1),
        mb.emf_smalltextout("日本語".encode("cp932"), 5, 10, small=True, no_rect=False),
        mb.emf_smalltextout("Wide text", 5, 20, small=False, no_rect=True),
        mb.emf_smalltextout(b"Narrow", 5, 30, small=True, no_rect=True),
    ])
    assert metafile_text.extract(data).lines == ["日本語", "Wide text", "Narrow"]


@pytest.mark.parametrize("kind", [114, 116, 78, 79])
def test_blit_records_with_a_source_dib_yield_child_pngs_for_the_route(monkeypatch, tmp_path, kind):
    emf = mb.emf([mb.emf_blit(kind, mb.dib(64, 48))])
    assert [(b.width, b.height) for b in metafile_text.extract(emf).bitmaps] == [(64, 48)]
    _, dmd = _build(monkeypatch, tmp_path, emf)
    decisions = _route(dmd)["decisions"]
    assert [item["reason_code"] for item in decisions if item["status"] == "selected"] == [
        "metafile_embedded_bitmap"]


def test_wmf_object_table_scan_is_linear_and_bounded():
    import time

    churn = [mb.wmf_create_pen(), mb.wmf_delete(0)] * 100_000 + [mb.wmf_textout(b"end", 0, 0)]
    started = time.monotonic()
    content = metafile_text.extract(mb.wmf(churn))
    assert content.lines == ["end"] and content.reason is None
    # 同時に存在するオブジェクトがヘッダの nObjects を超える作成だけの列は、理由を残して止める。
    flood = mb.wmf([mb.wmf_create_pen()] * 120_000 + [mb.wmf_textout(b"late", 0, 0)], objects=60_000)
    stopped = metafile_text.extract(flood)
    assert stopped.reason == "object_table_limit" and stopped.lines == []
    big = metafile_text.extract(mb.wmf([mb.wmf_create_pen()] * 120_000, objects=65_535))
    assert big.reason == "object_table_limit"
    assert time.monotonic() - started < 10


def test_pptx_figure_text_follows_the_picture_in_shape_order(tmp_path):
    path = tmp_path / "p.pptx"
    path.write_bytes(mb.pptx_with_media("image1.emf", _figure_emf()))
    md = office_md._pptx_md(path)
    block = "図の中の文字（元の値）\n- 帳票の見出し\n- Total 100"
    assert md.index("前の文") < md.index(block) < md.index("後の文")


def test_already_ingested_legacy_ppt_gets_the_figure_text_from_the_cached_conversion(monkeypatch, tmp_path):
    from sherpa.ingest.arms import legacy_convert

    wd = tmp_path / "world"
    wd.mkdir()
    (wd / "old.ppt").write_bytes(b"legacy-binary-not-a-real-ppt")
    dmd = tmp_path / "derived" / "md"
    converted = legacy_convert.cache_root_for(dmd) / "old.ppt.pptx"
    converted.parent.mkdir(parents=True)
    converted.write_bytes(mb.pptx_with_media("image1.emf", _figure_emf()))
    monkeypatch.setattr(legacy_convert, "legacy_exts", lambda: {".ppt"})
    monkeypatch.setattr(legacy_convert, "ensure_ooxml", lambda src, rel, cache_root: (converted, []))
    monkeypatch.setenv("SHERPA_MCP_ARMS", "ooxml,pdf_text")
    office_md.build_derived(wd, dmd)
    Path(str(converted) + ".key").write_text(legacy_convert._source_key(wd / "old.ppt"), encoding="utf-8")
    md_path = dmd / "old.ppt.md"
    assert "図の中の文字（元の値）" in md_path.read_text(encoding="utf-8")

    md_path.write_text("旧", encoding="utf-8")
    manifest = dmd.parent / "ir" / "old.ppt.derived.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data.pop("asset_versions", None)
    manifest.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(legacy_convert, "convert_to_ooxml", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("変換を再実行してはいけない")))
    assert office_md.human_md_sig_drift(wd, dmd) is True
    assert office_md.refresh_human_md(wd, dmd)["human_md_generated"] == 1
    text = md_path.read_text(encoding="utf-8")
    assert text.index("前の文") < text.index("図の中の文字（元の値）") < text.index("後の文")
    assert office_md.human_md_sig_drift(wd, dmd) is False


def test_legacy_doc_without_cached_conversion_is_left_alone(monkeypatch, tmp_path):
    wd, dmd, converted = _legacy_world(monkeypatch, tmp_path)
    md_path = dmd / "old.doc.md"
    md_path.write_text("旧", encoding="utf-8")
    Path(str(converted) + ".key").unlink()
    manifest = dmd.parent / "ir" / "old.doc.derived.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data.pop("asset_versions", None)
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert office_md.human_md_sig_drift(wd, dmd) is False            # 変換が要るので通常の取り込みまで据え置く
    assert office_md.refresh_human_md(wd, dmd)["human_md_generated"] == 0
    assert md_path.read_text(encoding="utf-8") == "旧"


def test_figure_text_is_independent_of_ocr_being_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv(office_md._OCR_ENABLED_ENV, "0")
    _, dmd = _build(monkeypatch, tmp_path)
    rag = (dmd.parent / "rag" / "a.docx.rag.md").read_text(encoding="utf-8")
    assert "図の中の文字（元の値）" in rag
    assert not list(dmd.parent.rglob("*.ocr_route.json"))


# ---- 取り込み済みの資料フォルダの追随（v4 -> v5） ---------------------------------------------------

def _chunk_bodies(rag: str) -> list[str]:
    return [part.strip() for part in re.split(r"<!-- chunk:\S+ -->", rag)[1:]]


def test_already_ingested_folder_picks_up_children_and_text_once_without_changing_other_chunks(
        monkeypatch, tmp_path):
    from sherpa.ingest import derived_generation, evidence_render

    wd, dmd = _build(monkeypatch, tmp_path)
    rag_path = dmd.parent / "rag" / "a.docx.rag.md"
    current = rag_path.read_text(encoding="utf-8")

    # 旧版（v4）で取り込み済みだった状態を作る: 図の文字の chunk も子 PNG も無く、ルートは旧 profile。
    ir = evidence_ir.read_json_file(dmd.parent / "ir" / "a.docx.evidence.json")
    old = evidence_render.render(ir, source_name="a.docx")
    rag_path.write_text(office_md._stamp_rule_only_rag_markdown(old.markdown), encoding="utf-8")
    shutil.rmtree(dmd.parent / "rag" / "a.docx.assets" / metafile_text.CHILD_DIR)
    route_path = dmd.parent / "ir" / "a.docx.ocr_route.json"
    route_path.write_text(json.dumps({**_route(dmd), "router_profile": "evidence-raster-router-v5"}), encoding="utf-8")
    (dmd / ocr_router.OCR_ROUTE_SIG_MARKER).write_text("sha256:old\n", encoding="utf-8")
    sig = (dmd / office_md._RAG_SIG_MARKER)
    sig.write_text(sig.read_text(encoding="utf-8").replace(
        f"metafile_text={metafile_text.METAFILE_EXTRACT_VERSION};", ""), encoding="utf-8")

    assert office_md.ocr_route_refresh_needed(dmd) is True
    assert office_md.rag_sig_drift(dmd) is True

    result = office_md.refresh_ocr_routes(
        dmd, world="metafile-world", generation_id=derived_generation.generation_id_for("sig"))
    assert result["ocr_routes_failed"] == 0 and result["ocr_routes_rewritten"] == 1
    assert any(item["status"] == "selected" for item in _route(dmd)["decisions"])

    refreshed = office_md.refresh_rag(wd, dmd, write_rag_sig_marker=True, world="metafile-world")
    assert refreshed.get("rag_failed", 0) == 0, refreshed
    new_bodies = set(_chunk_bodies(rag_path.read_text(encoding="utf-8")))
    old_bodies = set(_chunk_bodies(old.markdown))
    # 追加されたのは図の文字だけ。既存 chunk の本文は変わらない＝埋め込みキャッシュ（chunk 本文がキー）を再利用できる。
    assert old_bodies < new_bodies
    assert new_bodies - old_bodies == {"図の中の文字（元の値）\n帳票の見出し\nTotal 100"}
    assert set(_chunk_bodies(current)) == new_bodies
    assert office_md.rag_sig_drift(dmd) is False


# ---- 子 PNG の OCR 結果は親の図の位置に出る -------------------------------------------------------

def test_ocr_result_of_a_child_png_renders_at_the_parent_figure(monkeypatch, tmp_path):
    from sherpa.ingest import ai_observation, evidence_render, ocr_worker

    wd, dmd = _build(monkeypatch, tmp_path)
    ir = evidence_ir.read_json_file(dmd.parent / "ir" / "a.docx.evidence.json")
    assets = dmd.parent / "rag" / "a.docx.assets"
    manifest = ocr_router.from_json_str(
        (dmd.parent / "ir" / "a.docx.ocr_route.json").read_text(encoding="utf-8"), ir=ir)
    decision = next(item for item in manifest.decisions if item.status == "selected")
    job = {"source_content_hash": manifest.source_content_hash}
    prepared = ocr_worker.prepare_input(job, decision, source_path=wd / "a.docx", asset_root=assets)
    engine = type("E", (), {"engine_profile_hash": ocr_worker.profile_hash(),
                            "model_revision": ocr_worker.profile_hash()})()
    prediction = ocr_worker.OCRPrediction([
        ocr_worker.EngineLine(text="埋込画像の文字", confidence=0.95, bbox=[1, 2, 30, 40], line_id="0")])
    observation_set = ocr_worker.build_observation_set(
        ir=ir, decision=decision, prepared=prepared, prediction=prediction,
        canonical_generation_id=ai_observation.evidence_binding_id(ir), engine=engine)
    assert observation_set.inputs[0].parent_asset_sha256 == decision.detail["parent_asset_sha256"]
    assert ai_observation.from_json_str(ai_observation.to_json_str(observation_set), ir=ir) == observation_set

    rag = evidence_render.render(ir, source_name="a.docx", observation_set=observation_set).markdown
    assert rag.index("画像内容は未解釈") < rag.index("埋込画像の文字") < rag.index("後の段落")


def test_image_pixel_size_png_shares_metafile_reader_and_gif_jpeg_unchanged():
    from sherpa.ingest import evidence_spike
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (640).to_bytes(4, "big") + (480).to_bytes(4, "big")
    gif = b"GIF89a" + (30).to_bytes(2, "little") + (20).to_bytes(2, "little") + b"\x00" * 4
    jpeg = (b"\xff\xd8" + b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + (50).to_bytes(2, "big")
            + (70).to_bytes(2, "big") + b"\x00" * 10)
    assert evidence_spike._image_pixel_size(png) == metafile_text.child_png_size(png) == [640, 480]
    assert evidence_spike._image_pixel_size(png[:20]) is None and metafile_text.child_png_size(png[:20]) is None
    assert evidence_spike._image_pixel_size(gif) == [30, 20]
    assert evidence_spike._image_pixel_size(jpeg) == [70, 50]
    assert evidence_spike._image_pixel_size(b"not an image") is None
