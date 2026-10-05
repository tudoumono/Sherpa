"""`worker.sync()` の evidence/rag drift 軽量再生成分岐を pin する。

実際の tmp world（本物の xlsx を `office_md.build_derived()` で派生生成）を `worker.sync()` 経由で
駆動する。monkeypatch するのは DB/Neo4j/ES（`store.*`／`es_index.*`）と全再構築本体
（`worker.run`／`worker._run_locked`）だけ。

分岐の優先順:
  ⓪ force=True または prev!=sig → 常に `run()`。
  ① arms drift（`_derived_stale`）→ 常に `run()`。
  ②③④ ⓪①に該当しない時だけ `_refresh_derived_representations()` を評価する:
     - sidecar 欠落は drift の有無によらず先に確認 → 同一 `store.world_lock` 区間で `_run_locked`＋`.rag_sig` 削除。
     - evidence drift → `refresh_evidence_ir()` のみ／rag drift のみ → `refresh_rag()` のみ。
     - RAG_ES 有効時は refresh 成功後に `index_world(content_sig=sig)`、成功時だけ `.rag_sig` を確定（保留方式）。
     - drift 無し → 既存の backfill/ES `needs_reindex` 自己修復経路。
"""
from __future__ import annotations

import contextlib

import openpyxl
import pytest

from sherpa import es_index, store, worlds
from sherpa.ingest import office_md, worker, world_neo4j


def _build_world(tmp_path):
    wd = tmp_path / "world"
    wd.mkdir()
    dmd = tmp_path / "derived"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "明細"
    ws["A1"], ws["B1"] = "No", "内容"
    ws["A2"], ws["B2"] = 1, "サンプル内容"
    wb.save(wd / "a.xlsx")
    rep = office_md.build_derived(wd, dmd)
    assert rep["evidence_ir_failed"] == 0 and rep["rag_failed"] == 0
    return wd, dmd


def _bump_marker(dmd, name):
    marker = dmd / name
    marker.write_text(marker.read_text(encoding="utf-8") + ";simulated-version-bump", encoding="utf-8")


def _fake_es_meta_req(calls: list):
    # 外部境界（実 ES への通信）を遮断する。`index_world` を短絡しても bulk 成功後の
    # `confirm_human_md_meta` が GET→PUT で `_req` を呼ぶため、(method, path) を記録する。
    def _req(method, path, body=None, **kw):
        calls.append((method, path))
        return {}
    return _req


def _index_world_returns(monkeypatch, **result):
    monkeypatch.setattr(es_index, "index_world",
                        lambda world, content_sig=None, **kw: {"available": True, "indexed": 1, "chunks": 1, **result})


def _capture_finish(monkeypatch) -> list:
    finished: list[dict] = []
    monkeypatch.setattr(store, "finish_ingest_run",
                        lambda run_id, **kw: finished.append({"run_id": run_id, **kw}))
    return finished


def _flag_reasons(finished) -> list:
    return [f.get("reason") for f in finished[0]["extraction_snapshot"].get("flags", [])]


@pytest.fixture
def _stub(monkeypatch, tmp_path):
    """`sync()` を DB/Neo4j/ES 無しで駆動する。`office_md`/派生ファイルは実物のまま。"""
    wd, dmd = _build_world(tmp_path)
    calls: dict[str, list] = {"run": [], "index_world": [], "needs_reindex": [], "reflect_graph": [],
                              "req": []}

    # Neo4j 反映（`_reflect_graph_after_rag_rewrite`）とグラフ修復（`check_graph_counts`）は実 Neo4j に
    # 触れるため差し替える（後者は整合済み None を返し、本ファイルの対象外の修復分岐を発火させない）。
    monkeypatch.setattr(worker, "_reflect_graph_after_rag_rewrite",
                        lambda world: calls["reflect_graph"].append(world))
    monkeypatch.setattr(world_neo4j, "check_graph_counts", lambda *a, **kw: None)

    monkeypatch.setattr(worker, "world_state", lambda world, **kw: ("sig", {}))
    monkeypatch.setattr(store, "get_world",
                        lambda world: {"last_sig": "sig", "last_manifest": {}, "last_doc_count": 0})
    monkeypatch.setattr(worker, "_derived_stale", lambda world: False)
    monkeypatch.setattr(worlds, "world_dir", lambda world: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world: dmd)

    @contextlib.contextmanager
    def _noop_lock(world_id):
        yield
    monkeypatch.setattr(store, "world_lock", _noop_lock)

    monkeypatch.setattr(es_index, "_req", _fake_es_meta_req(calls["req"]))

    def _index_world(world, content_sig=None, **kw):
        calls["index_world"].append(content_sig)
        return {"available": True, "indexed": 1, "chunks": 1}
    monkeypatch.setattr(es_index, "index_world", _index_world)

    def _needs_reindex(world, sig, **kw):
        calls["needs_reindex"].append(sig)
        return False
    monkeypatch.setattr(es_index, "needs_reindex", _needs_reindex)

    def _run(world, **kw):
        # `run()`（⓪①）と `_run_locked()`（sidecar 欠落）のどちらでも同じ形で記録する
        calls["run"].append({"reflect": kw.get("reflect", True)})
        return {"status": "auto_published", "ledger": 0, "flags": []}
    monkeypatch.setattr(worker, "run", _run)
    monkeypatch.setattr(worker, "_run_locked", _run)

    return {"calls": calls, "wd": wd, "dmd": dmd}


def _rag_md(stub) -> str:
    return (stub["dmd"].parent / "rag" / "a.xlsx.rag.md").read_text(encoding="utf-8")


# ---- ⓪①: 全再構築の優先分岐 ----

@pytest.mark.parametrize("kwargs, prev", [
    pytest.param({"force": True}, "sig", id="force-true"),
    pytest.param({}, "old", id="prev-sig-mismatch"),
])
def test_sync_always_runs_full(_stub, monkeypatch, kwargs, prev):
    monkeypatch.setattr(store, "get_world", lambda world: {"last_sig": prev, "last_manifest": {},
                                                           "last_doc_count": 0})
    res = worker.sync("w", **kwargs)
    assert _stub["calls"]["run"] == [{"reflect": True}]
    assert res["changed"] is True


def test_sync_arms_drift_runs_full_via_derived_stale(_stub, monkeypatch):
    monkeypatch.setattr(worker, "_derived_stale", lambda world: True)
    old_rag_md = _rag_md(_stub)
    res = worker.sync("w")
    assert _stub["calls"]["run"] == [{"reflect": True}]
    assert _rag_md(_stub) == old_rag_md   # 軽量 refresh は実行されない
    assert res["changed"] is True


# ---- ②③: evidence/rag drift の排他分岐 ----

def test_sync_rag_drift_only_regenerates_via_real_refresh_rag_and_confirms_markers(_stub, monkeypatch):
    dmd = _stub["dmd"]
    _bump_marker(dmd, ".rag_sig")
    assert office_md.rag_sig_drift(dmd) is True
    finished = _capture_finish(monkeypatch)

    res = worker.sync("w", run_id=999)

    assert office_md.rag_sig_drift(dmd) is False                 # RAG_ES 有効＝ES 成功後に worker が確定する
    assert office_md.evidence_ir_sig_drift(dmd) is False         # evidence 側は無関係
    assert _stub["calls"]["run"] == []
    assert res["status"] == "unchanged" and res["changed"] is False
    assert _stub["calls"]["reflect_graph"] == ["w"]              # rag.md 書換え成功後にグラフも追随
    # 実 ES へは通信せず、フェイクの GET/PUT だけで confirm_human_md_meta が完結する
    mapping_path = f"/{es_index._index('w')}/_mapping"
    assert _stub["calls"]["req"] == [("GET", mapping_path), ("PUT", mapping_path)]
    assert _stub["calls"]["index_world"] == ["sig"]              # refresh 内部で content_sig 明示・1 回だけ
    assert _stub["calls"]["needs_reindex"] == ["sig"]            # 外側の明示チェックは False＝再索引しない
    marker = dmd / ".human_md_es_sig"                            # bulk 成功で human_md も確定
    assert marker.read_text(encoding="utf-8").strip() == office_md._current_human_md_sig()

    # 内部再索引の結果を同じ run の snapshot へ畳み込む（stage_timings の各段に時刻と elapsed）
    assert len(finished) == 1
    snap = finished[0]["extraction_snapshot"]
    for stage in ("scanning", "refresh_derived", "es_index"):
        assert stage in snap["stage_timings"], f"stage_timings に {stage} が無い"
        st = snap["stage_timings"][stage]
        assert st["started_at"] and st["finished_at"]
        assert st["elapsed_ms"] >= 0
    assert snap["counts"]["es_indexed"] == 1
    assert "embedded_chunks" not in snap["counts"]               # 返していない項目はキーごと省略


def test_sync_evidence_drift_regenerates_via_real_refresh_evidence_ir_only(_stub):
    # `refresh_evidence_ir()` は rag もまとめて再生成する契約（`refresh_rag()` は呼ばれない）。
    dmd = _stub["dmd"]
    _bump_marker(dmd, ".evidence_ir_sig")
    _bump_marker(dmd, ".rag_sig")
    res = worker.sync("w")
    assert office_md.evidence_ir_sig_drift(dmd) is False
    assert office_md.rag_sig_drift(dmd) is False
    assert _stub["calls"]["run"] == []
    assert res["status"] == "unchanged" and res["changed"] is False


def _old_route_world_with_failed_wmf_job(_stub):
    """ルート版 v3・読めない画像（WMF）を持つ公開済み資料フォルダと、その入力の failed job を作る。
    返り値: (route_path, route_input_id, generation_id)。"""
    import hashlib
    import io
    import json

    from openpyxl.drawing.image import Image as XImage
    from PIL import Image
    from sherpa.ingest import derived_generation, evidence_ir, ocr_router
    from sherpa.store import ocr_jobs

    wd, dmd = _stub["wd"], _stub["dmd"]
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    png = wd.parent / "p.png"
    png.write_bytes(buf.getvalue())
    wb = openpyxl.Workbook()
    wb.active["A1"] = "x"
    wb.active.add_image(XImage(str(png)), "B2")
    wb.save(wd / "a.xlsx")
    assert office_md.build_derived(wd, dmd)["evidence_ir_failed"] == 0
    # 画像の実体を WMF のバイト列へ差し替え、Evidence の hash も合わせる（実環境の WMF 図と同じ状態）
    wmf = b"\xd7\xcd\xc6\x9a" + b"\x00" * 32
    asset = next((dmd.parent / "rag" / "a.xlsx.assets").iterdir())
    old_hash = asset.stem
    new_hash = hashlib.sha256(wmf).hexdigest()
    asset.unlink()
    (asset.parent / (new_hash + asset.suffix)).write_bytes(wmf)
    for name in ("a.xlsx.evidence.json", "a.xlsx.derived.json"):
        doc = dmd.parent / "ir" / name
        doc.write_text(doc.read_text(encoding="utf-8").replace(old_hash, new_hash), encoding="utf-8")
    evidence = dmd.parent / "ir" / "a.xlsx.evidence.json"
    ir = evidence_ir.from_json_str(evidence.read_text(encoding="utf-8"))
    manifest = ocr_router.build_manifest(
        ir, source_rel_path="a.xlsx", assets=ocr_router.inventory_assets(asset.parent))
    route_input_id = ocr_jobs.unsupported_route_ids(manifest)[0]
    route = dmd.parent / "ir" / "a.xlsx.ocr_route.json"
    route.write_text(json.dumps({**json.loads(ocr_router.to_json_str(manifest)),
                                 "router_profile": "evidence-raster-router-v3"}), encoding="utf-8")
    (dmd / ".world_sig").write_text("sig\n", encoding="utf-8")
    generation = derived_generation.generation_id_for("sig")
    ocr_jobs._ensure()
    with ocr_jobs._connect() as connection:
        ocr_jobs._insert_job(connection, ocr_jobs._job_values(
            world="w", source_rel_path="a.xlsx", canonical_generation_id=generation,
            source_content_hash=manifest.source_content_hash, route_manifest_hash=manifest.route_manifest_hash,
            route_input={"route_input_id": route_input_id, "input_kind": "asset", "status": "selected",
                         "asset_rel_path": "x.wmf"},
            engine_profile_hash="sha256:" + "e" * 64,
        ), 0, 3)
        connection.execute(
            "UPDATE ocr_jobs SET status='failed', error_code='engine_failure' WHERE world='w'")
    return route, route_input_id, generation


def _job_state(generation):
    from sherpa.store import ocr_jobs
    with ocr_jobs._connect() as connection:
        return connection.execute(
            "SELECT status, error_code FROM ocr_jobs WHERE world='w' AND canonical_generation_id=%s",
            (generation,)).fetchone()


def test_sync_rewrites_old_route_and_cancels_old_failed_job_without_touching_rag(_stub, monkeypatch):
    # ルートだけを現行版へ書き直し、読めない画像になった入力の過去の failed job を cancelled に終端してから
    # マーカーを確定する。Evidence/rag は再生成しない。
    import json
    from sherpa.ingest import ocr_router
    from sherpa.store import ocr_jobs

    ocr_jobs.purge_world("w")
    try:
        route, _rid, generation = _old_route_world_with_failed_wmf_job(_stub)
        rag_before = (_stub["dmd"].parent / "rag" / "a.xlsx.rag.md").read_bytes()
        monkeypatch.setattr(ocr_jobs, "enqueue_refresh_run", lambda *a, **kw: {})

        worker.sync("w")

        assert json.loads(route.read_text(encoding="utf-8"))["router_profile"] == ocr_router.OCR_ROUTER_PROFILE
        assert dict(_job_state(generation)) == {"status": "cancelled", "error_code": "unsupported_image_format"}
        assert ocr_router.ocr_route_sig_drift(_stub["dmd"]) is False
        assert (_stub["dmd"].parent / "rag" / "a.xlsx.rag.md").read_bytes() == rag_before
        assert _stub["calls"]["index_world"] == []
    finally:
        ocr_jobs.purge_world("w")


def test_route_marker_stays_unwritten_when_cancel_fails_and_next_sync_retries(_stub, monkeypatch):
    from sherpa.ingest import ocr_router
    from sherpa.store import ocr_jobs

    ocr_jobs.purge_world("w")
    try:
        _route, _rid, generation = _old_route_world_with_failed_wmf_job(_stub)
        monkeypatch.setattr(ocr_jobs, "enqueue_refresh_run", lambda *a, **kw: {})
        real = ocr_jobs.cancel_unsupported_routes

        def _fail(*a, **kw):
            raise RuntimeError("db down")
        monkeypatch.setattr(ocr_jobs, "cancel_unsupported_routes", _fail)
        worker.sync("w")
        assert ocr_router.ocr_route_sig_drift(_stub["dmd"]) is True      # 再試行の入口が残る
        assert _job_state(generation)["status"] == "failed"

        monkeypatch.setattr(ocr_jobs, "cancel_unsupported_routes", real)
        worker.sync("w")
        assert _job_state(generation)["status"] == "cancelled"
        assert ocr_router.ocr_route_sig_drift(_stub["dmd"]) is False
    finally:
        ocr_jobs.purge_world("w")


def test_route_generation_failure_in_build_leaves_marker_unwritten_and_next_sync_creates_routes(
        _stub, monkeypatch):
    from sherpa.ingest import ocr_router
    from sherpa.store import ocr_jobs

    dmd = _stub["dmd"]
    monkeypatch.setattr(ocr_jobs, "enqueue_refresh_run", lambda *a, **kw: {})
    real = office_md._write_ocr_routes

    def _boom(*a, **kw):
        raise RuntimeError("route failure")
    monkeypatch.setattr(office_md, "_write_ocr_routes", _boom)
    worker._build_derived("w", world_sig="sig")
    route = dmd.parent / "ir" / "a.xlsx.ocr_route.json"
    assert not route.exists()
    assert ocr_router.ocr_route_sig_drift(dmd) is True

    monkeypatch.setattr(office_md, "_write_ocr_routes", real)
    worker.sync("w")
    assert route.is_file()
    assert ocr_router.ocr_route_sig_drift(dmd) is False


def test_sync_no_drift_keeps_existing_backfill_and_es_repair(_stub, monkeypatch):
    old_rag_md = _rag_md(_stub)
    res = worker.sync("w")
    assert _rag_md(_stub) == old_rag_md
    assert _stub["calls"]["needs_reindex"] == ["sig"]        # 既存の ES 自己修復チェックは走る
    assert res["status"] == "unchanged" and res["changed"] is False
    assert _stub["calls"]["reflect_graph"] == []             # rag.md が書き換わっていない＝グラフ反映も不要

    # `reflect=False` はグラフ照合（`check_graph_counts`）にも入らない
    graph_check_calls = []
    monkeypatch.setattr(world_neo4j, "check_graph_counts", lambda *a, **kw: graph_check_calls.append(1))
    res2 = worker.sync("w", reflect=False)
    assert res2["status"] == "unchanged" and res2["changed"] is False
    assert graph_check_calls == []


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy-format-backfilled", "current-format-not-rebackfilled"])
def test_sync_unchanged_scan_report_backfill(_stub, monkeypatch, legacy):
    # 旧形式（3 項目を持たない）の last_scan_report だけ、sig 一致でも scan_report を再実行して補完する。
    from sherpa import corpus_docs
    if legacy:
        report = {"scanned": 1, "indexed": 1, "by_doctype": {}, "office_md": 0, "skipped_office": 0,
                  "office_failed": 0, "skipped_other": 0, "skipped_ext": {}, "analyzer_declined": 0,
                  "analyzer_declined_as_document": 0, "unreadable": 0}
    else:
        report = {**corpus_docs.empty_scan_report(), "scanned": 1, "indexed": 1}
    row = {"last_sig": "sig", "last_manifest": {}, "last_doc_count": 0, "last_scan_report": report}
    monkeypatch.setattr(store, "get_world", lambda world: row)
    calls = []
    monkeypatch.setattr(store, "set_scan_report", lambda world, rep: calls.append((world, rep)))

    res = worker.sync("w")
    assert res["status"] == "unchanged"
    if legacy:
        assert len(calls) == 1
        saved_world, saved_report = calls[0]
        assert saved_world == "w"
        assert {"sensitive_excluded", "unreachable_as_text", "unreachable_as_text_by_ext"} <= saved_report.keys()
    else:
        assert calls == []


def test_sync_human_md_only_drift_still_reaches_es_repair_same_call(_stub, monkeypatch):
    # human_md drift で `refresh_human_md` が "handled" を返しても、同じ sync 内で ES の自己修復まで到達する
    # （rag_chunks が無効な文書は legacy `{rel}.md` へ縮退するため、ここで打ち切ると ES が古いまま残る）。
    dmd = _stub["dmd"]
    monkeypatch.setattr(office_md, "_current_human_md_sig", lambda: "bumped-human-md-version")
    assert office_md.human_md_sig_drift(_stub["wd"], dmd) is True
    monkeypatch.setattr(es_index, "needs_reindex", lambda world, sig, **kw: True)

    res = worker.sync("w")
    assert office_md.human_md_sig_drift(_stub["wd"], dmd) is False
    assert _stub["calls"]["index_world"] == ["sig"]
    assert res["status"] == "unchanged" and res["changed"] is False


def test_sync_unchanged_es_repair_progress_throttled_and_dedupes_same_value(_stub, monkeypatch):
    # unchanged 分岐の ES 自己修復の progress は `_run_locked` 側と同じ「100 件間隔の間引き＋同値抑止」を適用する。
    monkeypatch.setattr(es_index, "needs_reindex", lambda world, sig, **kw: True)

    def _index_world_holdback(world, *, content_sig=None, run_id=None, progress=None):
        for done in (0, 1, 1, 2, 150, 150, 300, 300):
            progress(done, 300)
        return {"available": True, "indexed": 300, "chunks": 300}
    monkeypatch.setattr(worker, "index_world_with_human_md_holdback", _index_world_holdback)

    recorded: list[dict] = []
    monkeypatch.setattr(store, "update_ingest_run_progress", lambda run_id, payload: recorded.append(payload))
    monkeypatch.setattr(store, "finish_ingest_run", lambda *a, **k: None)
    monkeypatch.setattr(worker.webhooks, "notify_run_terminal", lambda *a, **k: None)

    res = worker.sync("w", run_id=999)
    assert res["status"] == "unchanged"
    # total == 300 で絞り、直前のステージ遷移記録（total=None）を除外する
    done_values = [r["done"] for r in recorded if r["stage"] == "es_index" and r["total"] == 300]
    assert done_values == [0, 150, 300]


def test_sync_self_heal_success_confirms_human_md_markers_and_finalizes_run(_stub, monkeypatch):
    # ES 自己修復の index_world 成功で `.human_md_es_sig` と ES の `_meta.human_md_sig` を確定する
    # （meta を放置すると次回 sync が古い値を検知して再索引し続け、収束しない）。受付 run は auto_published。
    dmd = _stub["dmd"]
    finished = _capture_finish(monkeypatch)
    monkeypatch.setattr(es_index, "needs_reindex", lambda world, sig, **kw: True)
    meta_calls: list[str] = []
    monkeypatch.setattr(es_index, "confirm_human_md_meta", lambda world: meta_calls.append(world) or True)

    res = worker.sync("w", run_id=999)

    assert res["status"] == "unchanged"
    marker = dmd / ".human_md_es_sig"
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == office_md._current_human_md_sig()
    assert meta_calls == ["w"]
    assert len(finished) == 1 and finished[0]["run_id"] == 999
    assert finished[0]["status"] == "auto_published"
    # 実行した工程の所要時間と取得できた計数を snapshot に残す。返していない embedded はキーごと省略
    snap = finished[0]["extraction_snapshot"]
    assert "scanning" in snap["stage_timings"] and "es_index" in snap["stage_timings"]
    assert snap["stage_timings"]["scanning"]["elapsed_ms"] >= 0
    assert snap["counts"]["es_indexed"] == 1
    assert snap["counts"]["scanned"] == 0                   # `world_state` フェイクの manifest={} 由来
    assert "embedded_chunks" not in snap["counts"]


def test_sync_self_heal_bulk_errors_skips_marker_and_records_failure(_stub, monkeypatch):
    # 部分失敗なら `.human_md_es_sig` を確定せず（次回 sync が再試行）、失敗を ingest_runs へ記録する。
    dmd = _stub["dmd"]
    recorded: list[dict] = []
    monkeypatch.setattr(store, "add_ingest_run", lambda world, **kw: recorded.append({"world": world, **kw}))
    monkeypatch.setattr(es_index, "needs_reindex", lambda world, sig, **kw: True)
    _index_world_returns(monkeypatch, error="bulk_errors")

    res = worker.sync("w")
    assert res["status"] == "unchanged"
    marker = dmd / ".human_md_es_sig"
    assert not marker.is_file()
    assert len(recorded) == 1
    assert recorded[0]["status"] == "failed"
    assert recorded[0]["extraction_snapshot"]["error"] == "bulk_errors"

    _index_world_returns(monkeypatch)   # 次回 sync で直る
    assert worker.sync("w")["status"] == "unchanged"
    assert marker.is_file()
    assert len(recorded) == 1           # 成功時は失敗記録を増やさない


def test_sync_self_heal_failure_with_run_id_folds_into_same_run_not_a_new_one(_stub, monkeypatch):
    new_runs: list[dict] = []
    monkeypatch.setattr(store, "add_ingest_run", lambda world, **kw: new_runs.append({"world": world, **kw}))
    finished = _capture_finish(monkeypatch)
    monkeypatch.setattr(es_index, "needs_reindex", lambda world, sig, **kw: True)
    monkeypatch.setattr(es_index, "index_world",
                        lambda world, content_sig=None, **kw: {"available": True, "error": "bulk_errors"})

    res = worker.sync("w", run_id=999)
    assert res["status"] == "unchanged"
    assert new_runs == []                                  # 別 run は作られない
    assert len(finished) == 1 and finished[0]["run_id"] == 999
    assert finished[0]["status"] == "failed"
    assert any(r and r.startswith("es_repair_failed") for r in _flag_reasons(finished))


def test_index_world_holdback_drops_marker_before_reindex_prevents_stale_confirmation(_stub, monkeypatch):
    # 確定済みの `.human_md_es_sig` から別の再索引が走り bulk が部分失敗しても、再索引前にマーカーを
    # 無効化しているので確定値は残らず、bulk が直るまで pending のまま再索引され続ける。
    wd, dmd = _stub["wd"], _stub["dmd"]
    assert office_md.confirm_human_md_es_sig(wd, dmd) is True
    assert office_md.human_md_es_sig_drift(dmd) is False

    _index_world_returns(monkeypatch, error="bulk_errors")
    for _ in range(2):
        esr = worker.index_world_with_human_md_holdback("w", content_sig="sig")
        assert esr.get("error") == "bulk_errors"
        assert office_md.human_md_es_sig_drift(dmd) is True

    _index_world_returns(monkeypatch)
    esr3 = worker.index_world_with_human_md_holdback("w", content_sig="sig")
    assert not esr3.get("error")
    assert office_md.human_md_es_sig_drift(dmd) is False


def test_derived_dir_missing_skips_sidecar_and_drift_check(_stub, monkeypatch):
    empty_dir = _stub["dmd"].parent / "no-such-derived-dir"
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world: empty_dir)
    res = worker.sync("w")
    assert _stub["calls"]["run"] == []
    assert res["status"] == "unchanged"


# ---- document_ir drift 連鎖（document_ir→evidence→rag・失敗分離とマーカー確定順） ----

def test_document_ir_partial_failure_still_cascades_to_evidence_and_rag(_stub, monkeypatch):
    # document_ir が失敗しても evidence→rag への連鎖は打ち切らない（打ち切ると World 全体の RAG/ES が
    # 永久に旧世代のまま固定される）。`.document_ir_sig` は world 単位 1 つなので、失敗が残る限り次回も再実行される。
    dmd = _stub["dmd"]
    _bump_marker(dmd, ".document_ir_sig")
    original_refresh_document_ir = office_md.refresh_document_ir

    def _failing(wd_, dmd_, **kw):
        return {"document_ir_generated": 0, "document_ir_failed": 1,
                "document_ir_failures": [{"doc": "a.xlsx", "reason": "build_failed:RuntimeError"}]}
    monkeypatch.setattr(office_md, "refresh_document_ir", _failing)

    res = worker.sync("w")
    assert office_md.document_ir_sig_drift(dmd) is True          # 失敗のまま＝次回 sync 再試行
    assert office_md.evidence_ir_sig_drift(dmd) is False         # evidence/rag の連鎖は実行され成功する
    assert office_md.rag_sig_drift(dmd) is False
    assert res["status"] == "unchanged" and res["changed"] is False

    monkeypatch.setattr(office_md, "refresh_document_ir", original_refresh_document_ir)
    worker.sync("w")
    assert office_md.document_ir_sig_drift(dmd) is False


def test_document_ir_marker_confirm_waits_for_downstream_holdback_success(_stub, monkeypatch):
    # RAG_ES=1 で evidence 側の `.rag_sig` ホールドバック削除が失敗したら、document_ir 自体が成功でも
    # マーカーを確定しない（先に確定すると次回 sync で再試行の入口を失い、恒久的に旧世代のまま固定される）。
    dmd = _stub["dmd"]
    _bump_marker(dmd, ".document_ir_sig")
    original_drop = office_md.drop_rag_sig_marker
    monkeypatch.setattr(office_md, "drop_rag_sig_marker", lambda dr: False)

    res = worker.sync("w")
    assert _stub["calls"]["index_world"] == []                    # evidence 側が着手前に打ち切り＝ES も呼ばない
    assert office_md.document_ir_sig_drift(dmd) is True
    assert office_md.evidence_ir_sig_drift(dmd) is False
    assert office_md.rag_sig_drift(dmd) is False
    assert res["status"] == "unchanged"

    monkeypatch.setattr(office_md, "drop_rag_sig_marker", original_drop)
    worker.sync("w")
    assert office_md.document_ir_sig_drift(dmd) is False
    assert office_md.rag_sig_drift(dmd) is False


def test_evidence_only_marker_missing_rag_marker_current_es_failure_retried(_stub, monkeypatch):
    # `.evidence_ir_sig` のみ欠落（`.rag_sig` は現在値と一致）で index_world が失敗すると、ホールドバックの
    # 事前 unlink で `.rag_sig` も未確定へ戻り、次回 sync が rag 側だけを再試行する。
    dmd = _stub["dmd"]
    (dmd / ".evidence_ir_sig").unlink()
    assert office_md.evidence_ir_sig_drift(dmd) is True
    assert office_md.rag_sig_drift(dmd) is False
    _index_world_returns(monkeypatch, indexed=0, chunks=0, error="bulk_failed")

    res = worker.sync("w")
    assert office_md.evidence_ir_sig_drift(dmd) is False
    assert office_md.rag_sig_drift(dmd) is True
    assert res["status"] == "unchanged"

    _index_world_returns(monkeypatch)
    worker.sync("w")
    assert office_md.rag_sig_drift(dmd) is False


# ---- RAG_ES 接続（マーカー保留方式） ----

def test_sync_index_world_failure_leaves_marker_unconfirmed_then_retries(_stub, monkeypatch):
    dmd = _stub["dmd"]
    _bump_marker(dmd, ".rag_sig")
    _index_world_returns(monkeypatch, indexed=0, chunks=0, error="bulk_failed")
    res = worker.sync("w")
    assert office_md.rag_sig_drift(dmd) is True                # マーカー保留・次回 sync 再試行
    assert res["status"] == "unchanged"

    _index_world_returns(monkeypatch)
    worker.sync("w")
    assert office_md.rag_sig_drift(dmd) is False


def test_sync_refresh_failure_skips_es_and_marker(_stub, monkeypatch):
    dmd = _stub["dmd"]
    _bump_marker(dmd, ".rag_sig")
    monkeypatch.setattr(office_md, "refresh_rag",
                        lambda wd_, dmd_, **kw: {"rag_generated": 0, "rag_failed": 1,
                                                  "rag_failures": [{"doc": "x", "reason": "write_failed"}]})
    worker.sync("w")
    assert _stub["calls"]["index_world"] == []
    assert office_md.rag_sig_drift(dmd) is True


def test_holdback_unlink_failure_aborts_before_generation_and_keeps_marker(_stub):
    # holdback（生成開始前の `.rag_sig` 削除）が実際に OSError で失敗すると、refresh は着手せず
    # 既存の `.rag_sig`／`.rag.md` は無傷（chmod で unlink を失敗させる）。
    dmd = _stub["dmd"]
    _bump_marker(dmd, ".rag_sig")
    old_marker = (dmd / ".rag_sig").read_text(encoding="utf-8")
    old_rag_md = _rag_md(_stub)
    dmd.chmod(0o555)
    try:
        res = worker.sync("w")
    finally:
        dmd.chmod(0o755)
    assert _stub["calls"]["index_world"] == []
    assert (dmd / ".rag_sig").read_text(encoding="utf-8") == old_marker
    assert _rag_md(_stub) == old_rag_md
    assert res["status"] == "unchanged"


# ---- sidecar 欠落フォールバック ----

def _replace_with_directory(path):
    path.unlink()
    path.mkdir()


def _replace_with_broken_symlink(path):
    path.unlink()
    path.symlink_to(path.with_name("does-not-exist"))


def _sidecar_path(dmd, rel_stem, suffix):
    # `.evidence.json` は ir 層、それ以外（`.md`/`.md.meta.json`）は dmd（md 層）
    if suffix == ".evidence.json":
        return dmd.parent / "ir" / f"{rel_stem}{suffix}"
    return dmd / f"{rel_stem}{suffix}"


@pytest.mark.parametrize("suffix", [".md", ".md.meta.json", ".evidence.json"])
@pytest.mark.parametrize("mutate", [
    lambda p: p.unlink(),
    _replace_with_directory,
    _replace_with_broken_symlink,
], ids=["missing", "directory", "broken_symlink"])
def test_sidecar_missing_falls_back_to_full_run(_stub, suffix, mutate):
    # 存在判定は `is_file()`（ディレクトリ／壊れた symlink も「無い」扱い）。バージョン drift が無くても検知する。
    assert office_md.rag_sig_drift(_stub["dmd"]) is False
    assert office_md.evidence_ir_sig_drift(_stub["dmd"]) is False
    mutate(_sidecar_path(_stub["dmd"], "a.xlsx", suffix))
    res = worker.sync("w")
    assert _stub["calls"]["run"] == [{"reflect": True}]
    assert res["changed"] is True


def test_sidecar_missing_fallback_runs_inside_same_lock_as_detection(_stub, monkeypatch):
    # 検知→全再構築→`.rag_sig` 削除は同一 `world_lock` 区間・この順序（lock を解放すると他プロセスの sync が
    # 割り込み、全再構築の重複や削除の取り残しが起きる）。公開 `run()` は非再入 lock と衝突するため `_run_locked` を直接呼ぶ。
    (_stub["dmd"].parent / "ir" / "a.xlsx.evidence.json").unlink()
    lock_active = {"value": False}
    events: list[str] = []

    @contextlib.contextmanager
    def _tracking_lock(world_id):
        lock_active["value"] = True
        try:
            yield
        finally:
            lock_active["value"] = False
    monkeypatch.setattr(store, "world_lock", _tracking_lock)

    orig_missing = office_md.rag_sidecars_missing

    def _missing(wd_, dmd_, world=None):
        events.append("detect")
        return orig_missing(wd_, dmd_, world=world)
    monkeypatch.setattr(office_md, "rag_sidecars_missing", _missing)

    run_seen_lock_active = []

    def _run_locked(world, **kw):
        events.append("run")
        run_seen_lock_active.append(lock_active["value"])
        _stub["calls"]["run"].append({"reflect": kw.get("reflect", True)})
        return {"status": "auto_published", "ledger": 0, "flags": []}
    monkeypatch.setattr(worker, "_run_locked", _run_locked)

    drop_seen_lock_active = []
    orig_drop = office_md.drop_rag_sig_marker

    def _drop(dr):
        events.append("drop")
        drop_seen_lock_active.append(lock_active["value"])
        return orig_drop(dr)
    monkeypatch.setattr(office_md, "drop_rag_sig_marker", _drop)

    res = worker.sync("w")
    assert events == ["detect", "run", "drop"]
    assert run_seen_lock_active == [True]
    assert drop_seen_lock_active == [True]
    assert lock_active["value"] is False                    # sync() 復帰後は lock を手放している
    assert _stub["calls"]["run"] == [{"reflect": True}]
    assert res["changed"] is True
    assert not (_stub["dmd"] / ".rag_sig").is_file()        # RAG_ES 有効時は `.rag_sig` が実際に削除される


def test_merge_es_runs_accumulates_embedding_and_elapsed_and_keeps_first_start():
    from sherpa.ingest import worker as W
    prev_s = {"available": True, "error": None, "chunks": 10, "indexed": 3, "embedded": 7, "embed_elapsed_ms": 500}
    prev_t = {"started_at": "2026-09-13T00:00:00+00:00", "finished_at": "2026-09-13T00:00:01+00:00", "elapsed_ms": 1000}
    new_s = {"available": True, "error": None, "chunks": 10, "indexed": 3, "embedded": 0, "embed_elapsed_ms": 0}
    new_t = {"started_at": "2026-09-13T00:00:02+00:00", "finished_at": "2026-09-13T00:00:03+00:00", "elapsed_ms": 800}
    s, t = W._merge_es_runs(prev_s, prev_t, new_s, new_t)
    assert s["embedded"] == 7 and s["embed_elapsed_ms"] == 500 and s["indexed"] == 3
    assert t["started_at"] == prev_t["started_at"] and t["finished_at"] == new_t["finished_at"]
    assert t["elapsed_ms"] == 1800
    assert W._merge_es_runs(None, None, new_s, new_t) == (new_s, new_t)   # 初回が無ければ新しい値のまま


@pytest.mark.parametrize("drift_attr, refresh_attr, refresh_result, reason_check", [
    pytest.param("rag_sig_drift", "refresh_rag", {"rag_failed": 1, "rag_regenerated": 0},
                 lambda r: r.startswith("rag_refresh_failed"), id="rag-refresh-failed"),
    pytest.param("document_ir_sig_drift", "refresh_document_ir",
                 {"document_ir_failed": 1, "document_ir_regenerated": 2},
                 lambda r: "document_ir_refresh_failed" in r, id="document-ir-partial-failure"),
])
def test_sync_refresh_failure_with_run_id_finalizes_as_failed(
        _stub, monkeypatch, drift_attr, refresh_attr, refresh_result, reason_check):
    # 軽量再生成が一部失敗したら、後段が成功しても受付 run は auto_published ではなく failed＋理由 flag で終端する。
    finished = _capture_finish(monkeypatch)
    monkeypatch.setattr(office_md, drift_attr, lambda dmd, **kw: True)
    monkeypatch.setattr(office_md, refresh_attr, lambda wd, dmd, **kw: refresh_result)
    if refresh_attr == "refresh_document_ir":
        monkeypatch.setattr(office_md, "refresh_evidence_ir",
                            lambda wd, dmd, **kw: {"evidence_ir_failed": 0, "rag_failed": 0})
    monkeypatch.setattr(es_index, "needs_reindex", lambda world, sig, **kw: False)

    res = worker.sync("w", run_id=999)
    assert res["status"] == "unchanged"
    assert len(finished) == 1 and finished[0]["run_id"] == 999
    assert finished[0]["status"] == "failed"
    assert any(r and reason_check(r) for r in _flag_reasons(finished))
