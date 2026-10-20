"""取り込みの「小さな段」の進み具合（substage）。

4 万件超の取り込みで、MD 化が終わった後の「OCR の対象を決めています」が 50 分以上、進み具合もログも出さず、
止まったように見えた。小さな段の名前と件数が `ingest_runs.progress` とログに出ること、件数を出せない処理でも
一定の間隔で更新時刻と「まだ動いています」が出ること、状態 API の応答にそれらが載ることを固定する。
"""
from __future__ import annotations

import logging
import os
import time
from types import SimpleNamespace

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest  # noqa: E402

from sherpa import api, store  # noqa: E402
from sherpa.ingest import evidence_ir, ocr_router, office_md, substage, worker  # noqa: E402
from sherpa.schemas import RunProgress  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_substage():
    substage.unbind()
    yield
    substage.unbind()


def _capture_progress(monkeypatch) -> list[dict]:
    payloads: list[dict] = []
    monkeypatch.setattr(store, "update_ingest_run_progress", lambda run_id, p: payloads.append(dict(p)))
    return payloads


def test_ocr_route_step_reports_count_and_logs_start_middle_end(tmp_path, monkeypatch, caplog):
    """MD 化の後の OCR の対象の決定が、小さな段として件数つきで progress とログに出る（途中の行も出る）。"""
    monkeypatch.setenv(office_md._OCR_ENABLED_ENV, "1")
    monkeypatch.setattr(substage, "WRITE_EVERY", 1)
    monkeypatch.setattr(substage, "WRITE_MIN_SEC", 0.0)
    payloads = _capture_progress(monkeypatch)
    n_docs = 20

    def _fake_stage(wd, derived, **kw):
        md, rag, ir = office_md._staging_dirs(derived)
        for d in (md, rag, ir):
            d.mkdir(parents=True, exist_ok=True)
        for i in range(n_docs):
            (ir / f"d{i}.docx.evidence.json").write_text("{}", encoding="utf-8")
        return {"converted": n_docs}

    monkeypatch.setattr(office_md, "_build_derived_into_staging", _fake_stage)
    monkeypatch.setattr(evidence_ir, "read_json_file", lambda p: {})
    monkeypatch.setattr(ocr_router, "inventory_assets", lambda p: [])
    monkeypatch.setattr(ocr_router, "build_manifest", lambda ir, **kw: SimpleNamespace(decisions=[]))
    monkeypatch.setattr(ocr_router, "write_json_atomic", lambda p, m: None)

    src = tmp_path / "src"
    src.mkdir()
    rp = worker._RunProgress(1, "w", fail_note="test")
    rp.attach()
    rp.set_stage("office_md", done=n_docs, total=n_docs)
    caplog.set_level(logging.INFO, logger="sherpa.ingest.convert")

    office_md.build_derived(str(src), str(tmp_path / "md"))

    route = [p for p in payloads if p.get("step") == "ocr_routes"]
    assert route, "OCR の対象を決める間の progress が無い"
    assert route[0]["step_label"] == "OCR の対象を決めています"
    assert all(p["stage"] == "office_md" and p["done"] == n_docs for p in route)
    assert max(p["step_done"] for p in route) == n_docs and route[-1]["step_total"] == n_docs
    assert [p["step"] for p in payloads if p.get("step")][-1] == "publish"   # 続けて公開の小さな段へ進む
    lines = [r.getMessage() for r in caplog.records if "OCR の対象を決めています" in r.getMessage()]
    assert lines[0].endswith("始めます")
    assert any(f"/{n_docs} 件" in m for m in lines[1:-1]), lines
    assert "終わりました" in lines[-1] and f"{n_docs} 件" in lines[-1]


def test_long_step_without_count_keeps_updated_at_moving_and_logs_still_running(monkeypatch, caplog):
    """件数を出せない長い処理でも、一定の間隔で updated_at が進み「まだ動いています」が出る。"""
    monkeypatch.setattr(substage, "HEARTBEAT_SEC", 0.2)
    payloads = _capture_progress(monkeypatch)
    caplog.set_level(logging.INFO, logger="sherpa.ingest.convert")
    rp = worker._RunProgress(1, "w", fail_note="test")
    rp.attach()
    rp.set_stage("graph_build")

    st = substage.begin("ledger_rows", "台帳の行を作っています")
    time.sleep(0.9)
    st.end()

    beats = [p for p in payloads if p.get("step") == "ledger_rows"]
    stamps = [p["updated_at"] for p in beats]
    assert len(beats) >= 3 and stamps == sorted(stamps) and len(set(stamps)) == len(stamps), stamps
    still = [r.getMessage() for r in caplog.records if r.getMessage().startswith("まだ動いています")]
    assert still and "関係グラフを構築中・台帳の行を作っています" in still[0]
    assert payloads[-1].get("step") is None                     # 閉じた後は小さな段の欄が消える


def test_status_api_running_progress_carries_step_and_counts(monkeypatch):
    """状態 API の running_progress に小さな段と件数が載る（応答モデルに無い項目は黙って落ちる）。"""
    payloads = _capture_progress(monkeypatch)
    rp = worker._RunProgress(1, "w", fail_note="test")
    rp.attach()
    rp.set_stage("graph_build")
    st = substage.begin("neo4j_load", "Neo4j に反映しています", total=500)
    st.tick(300)
    substage._write(st)
    written = payloads[-1]

    monkeypatch.setattr(store, "get_recent_es_attempts", lambda wid, limit=200: [])
    monkeypatch.setattr(store, "get_latest_run_summary", lambda wid: {
        "status": "extracting", "extraction_snapshot": {}, "progress": written})
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: None)
    summary = api._ingest_summary("w", {"last_scan_report": None, "last_scan_report_at": None})

    out = RunProgress.model_validate(summary["running_progress"]).model_dump()
    assert (out["step"], out["step_label"], out["step_done"], out["step_total"]) == (
        "neo4j_load", "Neo4j に反映しています", 300, 500)
    assert out["stage"] == "graph_build" and out["step_started_at"]
