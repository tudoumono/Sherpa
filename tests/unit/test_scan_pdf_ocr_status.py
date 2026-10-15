"""文字の層が無いスキャン PDF（method=`pdf_scan_ocr`）の OCR の状態を、失敗の一覧へ反映する評価（`scan_pdf_ocr_status.settle`）の単体テスト。

来歴ファイルと OCR 観測ファイルは実ファイル、失敗の一覧は実 DB。外部境界の OCR job／worker の状態（`store.ocr_jobs` の読み取り）だけ差し替える。
最後に worker の配線（全件 run の後・unchanged の分岐の後で呼ばれること）を固定する。
"""
from __future__ import annotations

import contextlib
import json
import uuid

import pytest

from sherpa import corpus_docs, es_index, store, worlds
from sherpa.ingest import ai_observation, scan_pdf_ocr_status, world_neo4j, worker
from sherpa.store import ocr_jobs

SCAN = "scan/a.pdf"
_HEX = "a" * 64


def _write_meta(dmd, rel, method):
    path = dmd / (rel + ".md.meta.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"arm": "pdf_text", "method": method, "confidence": 1.0, "notes": []}), encoding="utf-8")


def _write_observation(obs_dir, rel, *, texts, confidence=0.9):
    """公開中の観測ツリーへ、`texts` を本文とする Observation Set を1行書く（空リストは観測0件の Set）。"""
    inp = ai_observation.ObservationInput(
        input_id="in1", target_evidence_id="e1", asset_sha256="sha256:" + _HEX, media_type="image/png",
        pixel_size=None, input_kind="asset", render_profile=None, parent_asset_sha256=None)
    observations = [ai_observation.AIObservation(
        observation_id=f"o{i}", input_id="in1", kind="summary", text=t, confidence=confidence,
        use_for_answer=confidence >= ai_observation.MIN_ANSWER_CONFIDENCE)
        for i, t in enumerate(texts)]
    obs_set = ai_observation.AIObservationSet(
        schema_version=ai_observation.AI_OBSERVATION_SCHEMA_VERSION, source_content_hash="sha256:" + _HEX,
        canonical_generation_id=_HEX, provider="ocr", model="m", model_revision=None, execution_mode="local",
        prompt_schema_version="v1", preprocessing_profile="p1", engine_profile_hash="sha256:" + _HEX,
        response_hash="sha256:" + _HEX, inputs=[inp], observations=observations, observation_set_hash="")
    obs_set.observation_set_hash = ai_observation.content_hash(obs_set)
    path = obs_dir / (rel + ".ai_observations.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ai_observation.to_json_str(obs_set) + "\n", encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """スキャン PDF 1件（SCAN）と通常の PDF 1件を持つ人向け MD の木・観測ツリー・OCR の外部状態（worker／job）を用意する。"""
    w = f"ocr-{uuid.uuid4().hex[:8]}"
    dmd, obs = tmp_path / "md", tmp_path / "obs"
    obs.mkdir()
    _write_meta(dmd, SCAN, "pdf_scan_ocr")
    _write_meta(dmd, "plain.pdf", "pdfplumber")
    monkeypatch.setattr(worlds, "derived_md_dir", lambda _w: dmd)
    monkeypatch.setattr(worlds, "observation_current_dir", lambda _w: obs)
    state = {"available": True, "terminal": False, "unpublished_jobs": 0}
    monkeypatch.setattr(ocr_jobs, "worker_availability_summary", lambda _h: {"available": state["available"]})
    monkeypatch.setattr(ocr_jobs, "generation_state",
                        lambda _w, _g: {"terminal": state["terminal"], "unpublished_jobs": state["unpublished_jobs"]})
    monkeypatch.delenv("SHERPA_OCR_ENABLED", raising=False)
    try:
        yield w, dmd, obs, state
    finally:
        store.clear_failed_docs(w)


def _reasons(w):
    return {r["rel"]: r["reason"] for r in store.list_failed_docs(w)}


def test_worker_unavailable_is_listed_as_ocr_unavailable(env):
    w, _dmd, _obs, state = env
    state["available"] = False
    scan_pdf_ocr_status.settle(w, "sig")
    assert _reasons(w) == {SCAN: "ocr_unavailable"}                    # 通常の PDF は載らない


def test_terminal_generation_without_observation_is_listed_as_ocr_failed(env):
    w, _dmd, obs, state = env
    state["terminal"] = True
    _write_observation(obs, "other.pdf", texts=["別の資料の文字"])        # 別の rel の観測では足りない
    scan_pdf_ocr_status.settle(w, "sig")
    assert _reasons(w) == {SCAN: "ocr_failed"}


def test_terminal_with_empty_observation_is_ocr_failed(env):
    w, _dmd, obs, state = env
    state["terminal"] = True
    _write_observation(obs, SCAN, texts=[])
    scan_pdf_ocr_status.settle(w, "sig")
    assert _reasons(w) == {SCAN: "ocr_failed"}


def test_terminal_with_only_low_confidence_observation_is_ocr_failed(env):
    w, _dmd, obs, state = env
    state["terminal"] = True
    _write_observation(obs, SCAN, texts=["かすれた文字"], confidence=0.5)    # rag.md に載らない低信頼の観測だけ
    scan_pdf_ocr_status.settle(w, "sig")
    assert _reasons(w) == {SCAN: "ocr_failed"}


def test_published_observation_is_not_listed_and_clears_ocr_reasons(env):
    w, _dmd, obs, state = env
    store.record_failed_doc(w, SCAN, "ocr_unavailable")
    state["available"] = False                                          # 観測が公開済みなら worker の状態は見ない
    _write_observation(obs, SCAN, texts=["請求書 合計 1,000円"])
    scan_pdf_ocr_status.settle(w, "sig")
    assert _reasons(w) == {}


def test_published_observation_keeps_other_failure_reason(env):
    w, _dmd, obs, _state = env
    store.record_failed_doc(w, SCAN, "source_parse_failed")
    _write_observation(obs, SCAN, texts=["本文"])
    scan_pdf_ocr_status.settle(w, "sig")
    assert _reasons(w) == {SCAN: "source_parse_failed"}


@pytest.mark.parametrize("terminal, unpublished_jobs", [(False, 0), (True, 1)], ids=["processing", "publishing"])
def test_pending_does_nothing_and_keeps_existing_record(env, terminal, unpublished_jobs):
    w, _dmd, _obs, state = env
    state.update(terminal=terminal, unpublished_jobs=unpublished_jobs)
    scan_pdf_ocr_status.settle(w, "sig")
    assert _reasons(w) == {}
    store.record_failed_doc(w, SCAN, "ocr_unavailable")                 # 前回の記録は処理中のあいだ残る
    scan_pdf_ocr_status.settle(w, "sig")
    assert _reasons(w) == {SCAN: "ocr_unavailable"}


def test_same_reason_is_not_retried_every_sync(env):
    w, _dmd, _obs, state = env
    state["available"] = False
    scan_pdf_ocr_status.settle(w, "sig")
    scan_pdf_ocr_status.settle(w, "sig")
    assert [r["tries"] for r in store.list_failed_docs(w)] == [1]


def test_non_scan_method_does_nothing(env):
    w, dmd, _obs, state = env
    _write_meta(dmd, SCAN, "pdfplumber")                                # 通常に変換できた PDF
    state["available"] = False
    scan_pdf_ocr_status.settle(w, "sig")
    assert _reasons(w) == {}


def test_ocr_disabled_does_nothing(env, monkeypatch):
    w, _dmd, _obs, state = env
    monkeypatch.setenv("SHERPA_OCR_ENABLED", "0")
    state["available"] = False
    scan_pdf_ocr_status.settle(w, "sig")
    assert _reasons(w) == {}


def test_unexpected_error_is_swallowed(env, monkeypatch):
    w, _dmd, _obs, _state = env

    def boom(_h):
        raise RuntimeError("db down")
    monkeypatch.setattr(ocr_jobs, "worker_availability_summary", boom)
    scan_pdf_ocr_status.settle(w, "sig")                                # 取り込みの失敗にしない
    assert _reasons(w) == {}


def test_catalog_describes_ocr_reasons_without_falling_to_other():
    from sherpa.ingest import failure_reasons
    for code in ("ocr_unavailable", "ocr_failed"):
        assert failure_reasons.describe(code)["code"] == code


# ---- worker の配線 ----

@pytest.mark.parametrize("res, called", [
    ({"status": "auto_published"}, True),
    ({"status": "auto_published_with_flags"}, True),
    ({"status": "failed"}, False),
    ({"status": "auto_published", "_pending_finalize": {}}, False),
], ids=["published", "published-flags", "failed", "pending-finalize"])
def test_full_run_settles_after_run_record(monkeypatch, res, called):
    order = []
    monkeypatch.setattr(worker, "_run_locked_body", lambda world, **kw: order.append("record") or res)
    monkeypatch.setattr(scan_pdf_ocr_status, "settle", lambda world, sig=None: order.append("settle"))
    assert worker._run_locked("w") is res
    assert order == (["record", "settle"] if called else ["record"])


def test_unchanged_sync_settles_with_current_sig(monkeypatch, tmp_path):
    calls = []

    @contextlib.contextmanager
    def _noop_lock(world_id):
        yield
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(store, "get_world",
                        lambda world: {"last_sig": "sig", "last_manifest": {}, "last_doc_count": 0, "last_scan_report": {}})
    monkeypatch.setattr(corpus_docs, "scan_report_missing_fields", lambda report: [])
    monkeypatch.setattr(worker, "world_state", lambda world, **kw: ("sig", {}))
    monkeypatch.setattr(worker, "_derived_stale", lambda world: False)
    monkeypatch.setattr(worker, "_seed_failed_docs_locked", lambda world: False)
    monkeypatch.setattr(worker, "_settle_notice_markers", lambda world, sig: None)
    monkeypatch.setattr(worker, "_refresh_derived_representations", lambda world, sig: (None, None, None))
    monkeypatch.setattr(world_neo4j, "check_graph_counts", lambda *a, **kw: None)
    monkeypatch.setattr(es_index, "needs_reindex", lambda world, sig, **kw: False)
    monkeypatch.setattr(worker, "run", lambda *a, **kw: pytest.fail("全件 run は走らない"))
    monkeypatch.setattr(scan_pdf_ocr_status, "settle", lambda world, sig=None: calls.append((world, sig)))
    res = worker.sync("w")
    assert res["status"] == "unchanged" and calls == [("w", "sig")]

