"""取り込みで欠けを公開しない・失敗を見せる（全体の取り込み直しの旧本文の引き継ぎ ほか）。"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import openpyxl  # noqa: E402

from sherpa.ingest import office_md  # noqa: E402
from tests._replace_row import offline_row


def _xlsx(path, **cells):
    wb = openpyxl.Workbook()
    for addr, value in cells.items():
        wb.active[addr] = value
    wb.save(path)


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_rebuild_keeps_previous_outputs_of_failed_document(tmp_path, monkeypatch):
    """全体の取り込み直しで 1 文書が作れなくても、その文書の旧い派生物（md／rag／ir）を消さずに公開する。"""
    src = tmp_path / "src"
    src.mkdir()
    der = tmp_path / "derived" / "md"
    _xlsx(src / "a.xlsx", A1="a-old")
    _xlsx(src / "b.xlsx", A1="b-old")
    assert not office_md.build_derived(src, der, row=offline_row("w")).get("error")
    old_b = {p: (tmp_path / "derived" / p).read_bytes() for p in _tree(tmp_path / "derived") if "b.xlsx" in p}
    assert any(p.endswith("b.xlsx.rag.md") for p in old_b), old_b.keys()

    _xlsx(src / "a.xlsx", A1="a-new")
    _xlsx(src / "b.xlsx", A1="b-new")
    real = office_md._convert_with_arms

    def _fail_b(path, enabled):
        if str(path).endswith("b.xlsx"):
            raise RuntimeError("simulated crash")
        return real(path, enabled)

    monkeypatch.setattr(office_md, "_convert_with_arms", _fail_b)
    rep = office_md.build_derived(src, der, row=offline_row("w"))

    assert rep["unhandled_failed"] == 1 and not rep.get("error")
    assert rep["derived_kept_previous"] == ["b.xlsx"]
    new_b = {p: (tmp_path / "derived" / p).read_bytes() for p in _tree(tmp_path / "derived") if "b.xlsx" in p}
    assert new_b == old_b
    assert "a-new" in (tmp_path / "derived" / "rag" / "a.xlsx.rag.md").read_text(encoding="utf-8")


def test_carryover_failure_after_clearing_staging_does_not_publish_the_generation(tmp_path, monkeypatch):
    """旧い派生物の置き換えが途中で失敗したら、旧本文も新出力も欠けた世代を公開せず、公開中の世代をそのまま残す。"""
    import pathlib
    from sherpa.ingest import derived_carryover
    src = tmp_path / "src"
    src.mkdir()
    der = tmp_path / "derived" / "md"
    _xlsx(src / "a.xlsx", A1="a-old")
    _xlsx(src / "b.xlsx", A1="b-old")
    assert not office_md.build_derived(src, der, row=offline_row("w")).get("error")
    _xlsx(src / "a.xlsx", A1="a-new")
    _xlsx(src / "b.xlsx", A1="b-new")
    real = office_md._convert_with_arms
    monkeypatch.setattr(office_md, "_convert_with_arms",
                        lambda path, enabled: (_ for _ in ()).throw(RuntimeError("x")) if str(path).endswith("b.xlsx")
                        else real(path, enabled))
    real_rename = pathlib.Path.rename

    def _rename(self, target):
        if self.name.endswith(derived_carryover._KEEP_SUFFIX):
            raise OSError("disk full")              # 控えを置く段の失敗（ファイルシステムの失敗の再現）
        return real_rename(self, target)
    monkeypatch.setattr(pathlib.Path, "rename", _rename)
    try:
        rep = office_md.build_derived(src, der, row=offline_row("w"))
    except derived_carryover.CarryoverError:
        rep = {"error": "carryover"}
    assert rep.get("error")
    assert "a-old" in (tmp_path / "derived" / "rag" / "a.xlsx.rag.md").read_text(encoding="utf-8")


def test_pdf_extraction_failure_is_not_an_empty_text_layer(tmp_path, monkeypatch):
    """PDF の文字の抽出が失敗したら「文字の層が無い」と見なさず、視覚読み取りへ切り替えない。"""
    from sherpa.ingest import arms as _arms

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(_arms, "enabled_arm_names", lambda: ["pdf_text", "vision"])
    monkeypatch.setattr(office_md, "_vision_pdf_ready", lambda names: True)
    office_md._PDF_QUALITY_CACHE.clear()

    def _boom(p):
        raise RuntimeError("extract failed")

    monkeypatch.setattr(office_md, "_pdf_pages", _boom)
    assert office_md.pdf_text_quality(pdf) == "failed"
    assert office_md.pdf_escalation_target(pdf) is None
    office_md._PDF_QUALITY_CACHE.clear()


# ---- OCR・VLM の観測（#26 / #27）と図の中の文字（#28） ----------------------------------------------

def _ocr_fixture(tmp_path):
    from sherpa.ingest import ai_observation, evidence_spike
    from tests.unit.test_ocr_rag_integration import _XLSX_WITH_PICTURE, _build_ocr_set

    ir = evidence_spike.extract(_XLSX_WITH_PICTURE)
    asset_dir = tmp_path / "assets"
    ocr_set = _build_ocr_set(ir, "a.xlsx", asset_dir, confidence=0.9, text="読めるOCR")
    obs_dir = tmp_path / "obs"
    obs_dir.mkdir()
    return ir, asset_dir, obs_dir, ai_observation.to_json_str(ocr_set)


def test_ocr_jsonl_with_one_broken_line_keeps_the_good_line_and_reports(tmp_path):
    """OCR 結果の 1 行が壊れていても、読める行は捨てない。読み残しは理由として返す。"""
    from sherpa.ingest import office_observation as oo

    ir, _assets, obs_dir, good = _ocr_fixture(tmp_path)
    (obs_dir / "a.xlsx.ai_observations.jsonl").write_text(good + "{broken json\n", encoding="utf-8")
    drops: dict = {}
    sets = oo._load_ocr_observation_sets(ir, "a.xlsx", obs_dir, drops)
    assert len(sets) == 1
    assert drops == {"observation_issues": ["ocr_observation_unreadable"]}


def test_stale_ocr_line_is_not_reported_as_unreadable(tmp_path):
    """今の Evidence と合わない古い観測は、従来どおり使わないだけで読み残しにはしない。"""
    import dataclasses

    from sherpa.ingest import office_observation as oo

    ir, _assets, obs_dir, good = _ocr_fixture(tmp_path)
    (obs_dir / "a.xlsx.ai_observations.jsonl").write_text(good, encoding="utf-8")
    other = dataclasses.replace(
        ir, source=dataclasses.replace(ir.source, content_hash="sha256:" + "ab" * 32))
    drops: dict = {}
    assert oo._load_ocr_observation_sets(other, "a.xlsx", obs_dir, drops) == []
    assert drops == {}


def test_observation_merge_failure_is_reported_not_silently_one_sided(tmp_path, monkeypatch):
    """OCR と VLM の合流が失敗したら、片方だけを通常の結果として返さず、未反映の理由を返す。"""
    from sherpa.ingest import ai_observation, office_observation as oo

    ir, assets, obs_dir, good = _ocr_fixture(tmp_path)
    (obs_dir / "a.xlsx.ai_observations.jsonl").write_text(good, encoding="utf-8")
    sentinel_vlm = ai_observation.from_json_str(good, ir=ir)
    monkeypatch.setattr(oo, "_build_vlm_observation_set", lambda *a, **k: sentinel_vlm)

    def _boom(*args, **kwargs):
        raise ValueError("conflict")

    monkeypatch.setattr(ai_observation, "merge_sets", _boom)
    drops: dict = {}
    assert oo._build_observation_set(ir, "a.xlsx", assets, obs_dir=obs_dir, drops=drops) is None
    assert drops == {"observation_issues": ["observation_merge_failed"]}


def test_incomplete_metafile_text_is_counted(tmp_path, monkeypatch):
    """WMF/EMF の文字の解析が途中で止まった図は、完全に読めた図と区別して数える。"""
    from sherpa.ingest import metafile_text, office_observation as oo, ocr_router

    class _Binding:
        parent_sha256 = None
        asset_sha256 = "sha256:" + "11" * 32
        relative_path = "f.wmf"

        def is_readable_raster(self):
            return False

    class _Element:
        element_id = "e1"

    class _Ev:
        elements = [_Element()]

    monkeypatch.setattr(ocr_router, "inventory_assets", lambda d: [_Binding()])
    monkeypatch.setattr(ocr_router, "_raster_candidates", lambda el: [{"asset_sha256": "sha256:" + "11" * 32}])
    monkeypatch.setattr(metafile_text, "read_asset_content",
                        lambda path: metafile_text.MetafileContent(kind="wmf", lines=["一部の文字"], reason="truncated"))
    drops: dict = {}
    texts = oo._build_figure_texts(_Ev(), tmp_path, drops)
    assert texts == {"e1": [["一部の文字"]]}
    assert drops == {"figures_incomplete": 1}


def test_observation_issues_reach_provenance_and_report(tmp_path):
    """読み残しの理由は、文書の来歴（provenance_summary）と作り直しの結果（observation_failures）に出る。"""
    from sherpa import corpus_docs
    from sherpa.ingest import office_observation as oo

    dr = tmp_path / "md"
    dr.mkdir()
    md = dr / "a.xlsx.md"
    (dr / "a.xlsx.md.meta.json").write_text('{"arm": "ooxml", "method": "ooxml", "notes": []}', encoding="utf-8")
    issues = oo.record_figure_notes(dr, "a.xlsx", {}, {"observation_issues": ["ocr_observation_unreadable"],
                                                        "figures_incomplete": 2})
    assert issues == ["ocr_observation_unreadable"]
    summary = corpus_docs.provenance_summary(md)
    assert summary["observation_issues"] == ["ocr_observation_unreadable"]
    assert summary["metafile_truncated"] == {"figures_incomplete": 2}
    oo.record_figure_notes(dr, "a.xlsx", {}, {}, refresh=True)             # 作り直しで問題が無くなれば記録も消える
    assert "observation_issues" not in corpus_docs.provenance_summary(md)


def test_build_and_refresh_keep_good_ocr_and_report_broken_line(monkeypatch, tmp_path):
    """実ファイルで build_derived／refresh_rag を通し、壊れた行があっても正常な OCR は rag.md に残り、結果に理由が出る。"""
    import shutil

    from sherpa.ingest import ai_observation, evidence_spike
    from tests.unit.test_ocr_rag_integration import _XLSX_WITH_PICTURE, _build_ocr_set

    probe_ir = evidence_spike.extract(_XLSX_WITH_PICTURE)
    ocr_set = _build_ocr_set(probe_ir, _XLSX_WITH_PICTURE.name, tmp_path / "probe-assets",
                             confidence=0.9, text="壊れた行の隣のOCR")
    obs_dir = tmp_path / "observations-current"
    obs_dir.mkdir()
    (obs_dir / f"{_XLSX_WITH_PICTURE.name}.ai_observations.jsonl").write_text(
        ai_observation.to_json_str(ocr_set) + "{not json\n", encoding="utf-8")
    monkeypatch.setattr("sherpa.worlds.observation_current_dir", lambda _w: obs_dir)
    monkeypatch.setenv("SHERPA_MCP_ARMS", "ooxml,pdf_text")
    wd = tmp_path / "world"
    wd.mkdir()
    shutil.copy(_XLSX_WITH_PICTURE, wd / _XLSX_WITH_PICTURE.name)
    dmd = tmp_path / "derived" / "md"

    rep = office_md.build_derived(wd, dmd, world="w-broken-ocr", row=offline_row("w-broken-ocr"))
    rag_path = dmd.parent / "rag" / f"{_XLSX_WITH_PICTURE.name}.rag.md"
    assert "壊れた行の隣のOCR" in rag_path.read_text(encoding="utf-8")
    assert rep["observation_failures"] == [{"doc": _XLSX_WITH_PICTURE.name, "reason": "ocr_observation_unreadable"}]

    rag_path.unlink()
    res = office_md.refresh_rag(wd, dmd, world="w-broken-ocr", row=offline_row("w-broken-ocr"))
    assert res["observation_failures"] == [{"doc": _XLSX_WITH_PICTURE.name, "reason": "ocr_observation_unreadable"}]
    assert "壊れた行の隣のOCR" in rag_path.read_text(encoding="utf-8")


def test_excel_display_gap_reaches_provenance_summary(tmp_path):
    """Excel の表示値の補完が失敗・不足した文書は、基本方式の値のままであることを来歴に出す。"""
    from sherpa import corpus_docs

    def _summary(report):
        md = tmp_path / "a.xlsx.md"
        meta = {"arm": "ooxml", "method": "ooxml", "notes": [], "office_display": report}
        (tmp_path / "a.xlsx.md.meta.json").write_text(__import__("json").dumps(meta), encoding="utf-8")
        return corpus_docs.provenance_summary(md)

    gap = _summary({"enabled": True, "status": "fallback_linux", "requested_cells": 5, "applied_cells": 0,
                    "reason": "microsoft_excel_worker_unavailable"})
    assert gap["office_display"] == {"status": "fallback_linux", "reason": "microsoft_excel_worker_unavailable",
                                     "requested_cells": 5, "applied_cells": 0}
    assert _summary({"enabled": True, "status": "partial", "requested_cells": 5, "applied_cells": 3,
                     "reason": "worker_cell_missing"})["office_display"]["applied_cells"] == 3
    assert "office_display" not in _summary({"enabled": True, "status": "applied", "requested_cells": 5,
                                             "applied_cells": 5, "reason": None})
    assert "office_display" not in _summary({"enabled": False, "status": "disabled", "requested_cells": 0,
                                             "applied_cells": 0, "reason": None})


# ---- アーカイブ・孤児の掃除・OCR の準備・取り込みの結果（flags） ---------------------------------------

def test_archive_row_does_not_look_ready_after_failed_update(monkeypatch, tmp_path):
    """更新を読めず前回の内容を残しているアーカイブは、展開済み・ready に見せない。"""
    from sherpa import corpus_docs, worlds

    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(worlds, "archive_manifest_path", lambda w: manifest)
    base = {"status": "ok", "extracted_count": 3, "content_hash": "h"}
    manifest.write_text(__import__("json").dumps({"a.zip": base, "b.zip": {**base, "last_sync_error": "read_failed"}}),
                        encoding="utf-8")
    ok = corpus_docs._archive_row("a.zip", "w")
    stale = corpus_docs._archive_row("b.zip", "w")
    assert ok["state"] == "ready"
    assert stale["state"] == "unreadable" and stale["reason"] == "archive_sync_failed"
    assert "前回の内容" in stale["label"]


def test_reconcile_reports_failed_cleanups_instead_of_deleted(monkeypatch, tmp_path):
    """孤児の掃除で消せなかった派生物は削除済みに入れず、失敗として返す。失敗した world の掃除の行は残す。"""
    import contextlib
    import shutil

    from sherpa import es_index, reconcile, store
    from sherpa.store import replace_rows
    from tests._replace_row import offline_row

    parent = tmp_path / "derived"
    for name in ("orphana", "orphanb"):
        (parent / name).mkdir(parents=True)
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(parent))
    real = shutil.rmtree

    def _flaky(path, *args, **kwargs):
        if str(path).endswith("orphana"):
            raise PermissionError("denied")
        return real(path, *args, **kwargs)

    dropped: list = []

    @contextlib.contextmanager
    def _begin(world):
        yield offline_row(world, kind="cleanup")

    monkeypatch.setattr(reconcile.shutil, "rmtree", _flaky)
    monkeypatch.setattr(replace_rows, "begin_cleanup", _begin)
    monkeypatch.setattr(replace_rows, "drop_own", lambda drop: dropped.append(drop.id))
    monkeypatch.setattr(replace_rows, "stale_cleanup_worlds", lambda: [])
    monkeypatch.setattr(store, "list_document_worlds", lambda: [])
    monkeypatch.setattr(store, "replace_documents", lambda w, rows, **kw: 0)
    monkeypatch.setattr(store, "stage_ledger", lambda w, rows, names, **_kw: len(rows))
    monkeypatch.setattr(store, "switch_ledger", lambda w, **_kw: None)
    monkeypatch.setattr(es_index, "delete_world", lambda w, **kw: True)
    failed: list = []
    out = reconcile._reconcile_worlds(set(), [], False, failed)
    assert out["derived"] == ["orphanb"]
    assert failed == ["derived:orphana"] and len(dropped) == 1
    assert (parent / "orphana").is_dir()


def test_reconcile_documents_reports_unreadable_ledger(monkeypatch):
    from sherpa import reconcile, store

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "list_document_worlds", _boom)
    failed: list = []
    assert reconcile._documents_orphans(set(), failed) == []
    assert failed == ["documents"]


def test_run_flags_surface_each_failure_in_the_result():
    from sherpa.ingest import run_flags

    flags = run_flags.office_flags({
        "ocr_routes_error": "OSError", "ocr_enqueue_error": "enqueue_failed",
        "observation_failures": [{"doc": "a.xlsx", "reason": "ocr_observation_unreadable"}],
        "archive_sync_errors": ["a.zip", None]})
    reasons = [f["reason"] for f in flags]
    assert "office_md:ocr_routes_failed:OSError" in reasons
    assert "office_md:ocr_enqueue_failed" in reasons
    assert "office_md:observation_issues:ocr_observation_unreadable" in reasons
    assert [f["doc"] for f in flags if f["reason"] == "archive_sync_failed"] == ["a.zip", None]
    assert all(f["action"] == "warn" for f in flags)
    assert run_flags.office_flags({}) == []

    assert run_flags.reconcile_flags({"es": [], "failed": ["derived:orphana"]})[0]["reason"] \
        == "reconcile_failed:derived:orphana"
    assert run_flags.reconcile_flags({"skipped": "registry_unavailable"})[0]["reason"] \
        == "reconcile_skipped:registry_unavailable"
    assert run_flags.reconcile_flags({"skipped": "test_db_isolated"}) == []
    assert run_flags.reconcile_flags({"failed": []}) == [] and run_flags.reconcile_flags(None) == []


def test_ocr_enqueue_failure_is_in_the_derived_report(monkeypatch, tmp_path):
    """OCR の再実行の予約に失敗したら、本体の取り込みは成功のまま、結果に理由が残る（確定の印も付けない）。"""
    from sherpa import worlds
    from sherpa.ingest import worker

    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: tmp_path / "md")
    monkeypatch.setattr(worker.office_md, "build_derived", lambda *a, **k: {"converted": 1})
    monkeypatch.setattr(worker.office_md, "ocr_enabled", lambda: True)
    monkeypatch.setattr(worker, "_enqueue_ocr_refresh", lambda world, sig: False)
    marked: list = []
    monkeypatch.setattr(worker.office_md, "write_ocr_route_sig_marker", lambda d: marked.append(d))
    rep = worker._build_derived("w", world_sig="sig", row=offline_row("w"))
    assert rep["ocr_enqueue_error"] == "enqueue_failed" and marked == []

    monkeypatch.setattr(worker, "_enqueue_ocr_refresh", lambda world, sig: True)
    rep = worker._build_derived("w", world_sig="sig", row=offline_row("w"))
    assert "ocr_enqueue_error" not in rep and marked == [tmp_path / "md"]
