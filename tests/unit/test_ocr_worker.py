from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
import signal
import time

import pytest

from sherpa.ingest import ai_observation, evidence_ir, evidence_spike, ocr_router, ocr_settings, ocr_worker
from tests._replace_row import offline_row

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "fixtures/eval/excel_ja/inputs/JPX-015.xlsx"
PDF_SOURCE = ROOT / "fixtures/eval/office_ja/inputs/OJA-PDF-MEDIUM.pdf"
GENERATION_ID = "c" * 64
EMPTY_SNAPSHOT = {"row_count": 0, "min_id": None, "max_id": None, "id_sum": 0}


class FakeEngine:
    engine_profile_hash = ocr_worker.profile_hash()
    model_revision = ocr_worker.profile_hash()

    def __init__(self):
        self.calls = 0

    def predict(self, image_bytes: bytes, *, media_type: str) -> ocr_worker.OCRPrediction:
        assert image_bytes and media_type.startswith("image/")
        self.calls += 1
        return ocr_worker.OCRPrediction([
            ocr_worker.EngineLine(text="  A_01  ", confidence=0.92, bbox=[1, 2, 30, 40], line_id="0"),
        ])


def _hanging_inference_child(_cache_home, requests, responses):
    responses.put({"kind": "ready"})
    requests.get()
    time.sleep(60)


def _echo_inference_child(_cache_home, requests, responses):
    responses.put({"kind": "ready"})
    while True:
        request = requests.get()
        if request is None:
            return
        responses.put({
            "kind": "result", "request_id": request["request_id"],
            "payload": ocr_worker.OCRPrediction([]).to_payload(),
        })


def _picture_route(tmp_path):
    ir = evidence_spike.extract(SOURCE)
    asset_root = tmp_path / "assets"
    evidence_spike.extract_assets(SOURCE, ir, asset_root)
    manifest = ocr_router.build_manifest(
        ir, source_rel_path="excel/JPX-015.xlsx", assets=ocr_router.inventory_assets(asset_root),
    )
    decision = next(item for item in manifest.decisions if item.status == "selected")
    job = {
        "id": 1,
        "world": "world-a",
        "source_rel_path": manifest.source_rel_path,
        "canonical_generation_id": GENERATION_ID,
        "source_content_hash": manifest.source_content_hash,
        "route_manifest_hash": manifest.route_manifest_hash,
        "route_input": asdict(decision),
        "engine_profile_hash": ocr_worker.profile_hash(),
        "lease_token": "lease-token",
    }
    return ir, asset_root, decision, job


def _patch_lease(monkeypatch, job, *, renew=True, cached=None):
    monkeypatch.setattr(ocr_worker.ocr_jobs, "lease_next", lambda worker_id, lease_seconds, **kwargs: job)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "renew_lease", lambda *args, **kwargs: renew)
    if cached != "unset":
        monkeypatch.setattr(ocr_worker.ocr_jobs, "get_cached_result", lambda *args: cached)


def _run_once(ir, asset_root, engine=None, *, current=True, **kwargs):
    return ocr_worker.run_once(
        "worker-1", engine=engine or FakeEngine(), canonical_is_current=lambda world, generation: current,
        load_ir=lambda leased: ir, resolve_source=lambda leased: SOURCE,
        resolve_asset_root=lambda leased: asset_root, **kwargs,
    )


def _run_once_unreadable(engine=None, *, current):
    """`load_ir` へ到達したら失敗する run_once（本文を読む前に終端すべき経路用）。"""
    return ocr_worker.run_once(
        "worker-1", engine=engine or FakeEngine(), canonical_is_current=lambda world, generation: current,
        load_ir=lambda leased: (_ for _ in ()).throw(AssertionError("must not load")),
        resolve_source=lambda leased: Path("unreachable"), resolve_asset_root=lambda leased: Path("unreachable"),
    )


def _build_result(ir, decision, prepared, engine):
    prediction = engine.predict(prepared.image_bytes, media_type=prepared.media_type)
    return ocr_worker.build_observation_set(
        ir=ir, decision=decision, prepared=prepared, prediction=prediction,
        canonical_generation_id=GENERATION_ID, engine=engine,
    )


# ===== profile / model pin =====

def test_paddle_availability_requires_pinned_versions_and_model_hashes(monkeypatch, tmp_path):
    model_root = tmp_path / "official_models"
    for name in (ocr_worker.PADDLE_CPU_PROFILE.detection_model, ocr_worker.PADDLE_CPU_PROFILE.recognition_model):
        (model_root / name).mkdir(parents=True)
    expected = {
        ocr_worker.PADDLE_CPU_PROFILE.detection_model:
            ocr_worker.PADDLE_CPU_PROFILE.detection_model_tree_sha256,
        ocr_worker.PADDLE_CPU_PROFILE.recognition_model:
            ocr_worker.PADDLE_CPU_PROFILE.recognition_model_tree_sha256,
    }
    monkeypatch.setattr(ocr_worker, "_installed_version", lambda name: {
        "paddleocr": "3.7.0", "paddlepaddle": "3.3.0",
        "pypdfium2": "5.11.0", "Pillow": "12.3.0",
    }[name])
    monkeypatch.setattr(ocr_worker, "_tree_digest", lambda path: expected[path.name])
    ocr_worker._paddle_availability_cached.cache_clear()

    availability = ocr_worker.paddle_availability(tmp_path)
    assert availability.available is True
    assert availability.unavailable_reason is None
    assert availability.model_hashes_valid is True
    assert availability.engine_profile_hash == ocr_worker.profile_hash()

    (model_root / ocr_worker.PADDLE_CPU_PROFILE.detection_model).rmdir()
    ocr_worker._paddle_availability_cached.cache_clear()
    unavailable = ocr_worker.paddle_availability(tmp_path)
    assert unavailable.available is False
    assert unavailable.unavailable_reason == "offline_model_missing"
    assert unavailable.model_hashes_valid is False


def test_model_tree_digest_ignores_downloader_cache_metadata(tmp_path):
    # downloader が `.cache/huggingface/` へ書く取得メタデータの差で hash を変えない（変えると同一 model が
    # `model_hash_mismatch` で起動拒否される）。model 本体が変われば必ず変わる。
    model = tmp_path / "PP-OCRv6_medium_det"
    (model / ".cache" / "huggingface" / "download").mkdir(parents=True)
    (model / "inference.pdiparams").write_bytes(b"weights")
    (model / "inference.yml").write_text("pinned", encoding="utf-8")
    baseline = ocr_worker._tree_digest(model)

    (model / ".cache" / "huggingface" / "download" / "inference.pdiparams.metadata").write_text(
        "etag-and-timestamp", encoding="utf-8")
    assert ocr_worker._tree_digest(model) == baseline

    (model / "inference.pdiparams").write_bytes(b"weights-v2")
    assert ocr_worker._tree_digest(model) != baseline


def test_pinned_model_hashes_match_the_distributed_lock_file():
    lock = json.loads((ROOT / "docker/ocr-models.lock.json").read_text(encoding="utf-8"))
    locked = {item["name"]: item["tree_sha256"] for item in lock["models"]}
    assert set(lock["sets"]) == set(ocr_worker.OCR_MODEL_SETS) and ocr_worker.DEFAULT_OCR_MODEL in lock["sets"]
    for label, model_set in ocr_worker.OCR_MODEL_SETS.items():
        assert lock["sets"][label] == [model_set.detection, model_set.recognition]
        assert locked[model_set.detection] == model_set.detection_tree_sha256
        assert locked[model_set.recognition] == model_set.recognition_tree_sha256
    assert lock["runtime_download_allowed"] is False
    assert ".cache" in lock["tree_hash_excludes"]


@pytest.mark.parametrize("media_type, expected", [
    ("image/png", ".png"),
    ("image/jpeg", ".jpg"),
    ("image/bmp", ".bmp"),
    ("image/webp", ".webp"),
    ("image/tiff", ".tiff"),
    ("IMAGE/PNG; charset=binary", ".png"),
    ("application/octet-stream", ".png"),
    ("", ".png"),
    ("image/gif", ".png"),   # Paddle 非対応形式は png 扱いで中身判定に委ねる
])
def test_paddle_input_suffix_maps_media_types_to_supported_extensions(media_type, expected):
    assert ocr_worker._paddle_input_suffix(media_type) == expected


# ===== 入力準備・観測集合 =====

@pytest.mark.parametrize("confidence, text, answer_eligible", [
    pytest.param(0.92, "  A_01  ", True, id="confident-line-answer-eligible"),
    # 既存の使用可否ルール（MIN_ANSWER_CONFIDENCE）未満は検索可のまま回答材料にしない
    pytest.param(0.5, "判読不可気味", False, id="low-confidence-not-answer-eligible"),
])
def test_asset_preparation_builds_ocr_only_set_with_confidence_rule(tmp_path, confidence, text, answer_eligible):
    ir, asset_root, decision, job = _picture_route(tmp_path)
    prepared = ocr_worker.prepare_input(job, decision, source_path=SOURCE, asset_root=asset_root)

    class _Engine(FakeEngine):
        def predict(self, image_bytes, *, media_type):
            self.calls += 1
            return ocr_worker.OCRPrediction([
                ocr_worker.EngineLine(text=text, confidence=confidence, bbox=[1, 2, 30, 40], line_id="0")])

    result = _build_result(ir, decision, prepared, _Engine())

    assert ai_observation.validation_errors(result, ir=ir) == []
    assert result.canonical_generation_id == GENERATION_ID
    obs = result.observations[0]
    assert obs.kind == "ocr_text"
    assert obs.text == text
    assert obs.searchable is True
    assert obs.confidence == confidence
    assert obs.use_for_answer is answer_eligible


def test_source_hash_is_reused_for_same_stat_and_recomputed_after_change(monkeypatch, tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"first-source")
    first_hash = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    calls = []
    original = ocr_worker._source_hash_uncached

    def counted(path, *, on_progress=None):
        calls.append(path)
        return original(path, on_progress=on_progress)

    ocr_worker._clear_source_hash_cache()
    monkeypatch.setattr(ocr_worker, "_source_hash_uncached", counted)
    assert ocr_worker._source_hash(source, expected_hash=first_hash) == first_hash
    assert ocr_worker._source_hash(source, expected_hash=first_hash) == first_hash
    assert len(calls) == 1

    source.write_bytes(b"second-source-is-different")
    second_hash = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    assert ocr_worker._source_hash(source, expected_hash=second_hash) == second_hash
    assert len(calls) == 2


def test_fixed_page_render_contract_outputs_hash_bound_png(monkeypatch, tmp_path):
    ir = evidence_spike.extract(PDF_SOURCE)
    page = next(item for item in ir.elements if item.type == "page")
    decision = ocr_router.OCRRouteDecision(
        route_input_id="render-1", target_evidence_id=page.element_id, input_kind="page_render",
        status="selected", reason_code="scan_page_render_fallback", priority=90, media_type="image/png",
        page_render={**ocr_router.PAGE_RENDER_PROFILE, "page_1_based": page.locator.page},
    )
    job = {"source_content_hash": ir.source.content_hash}
    opens = []
    original_open = ocr_worker._open_pdf_document

    def counted_open(path):
        opens.append(path)
        return original_open(path)

    ocr_worker._clear_pdf_document_cache()
    monkeypatch.setattr(ocr_worker, "_open_pdf_document", counted_open)
    try:
        prepared = ocr_worker.prepare_input(job, decision, source_path=PDF_SOURCE, asset_root=tmp_path)
        repeated = ocr_worker.prepare_input(job, decision, source_path=PDF_SOURCE, asset_root=tmp_path)
    finally:
        ocr_worker._clear_pdf_document_cache()
    assert prepared.image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert prepared.asset_sha256 == "sha256:" + hashlib.sha256(prepared.image_bytes).hexdigest()
    assert prepared.input_kind == "page_render"
    assert all(value > 0 for value in prepared.pixel_size)
    assert repeated.image_bytes == prepared.image_bytes
    assert len(opens) == 1


# ===== run_once（lease・世代・秘匿・失敗の契約） =====

def test_worker_uses_cache_contract_and_completes_without_changing_canonical(monkeypatch, tmp_path):
    ir, asset_root, decision, job = _picture_route(tmp_path)
    engine = FakeEngine()
    completed = {}
    prediction = ocr_worker.OCRPrediction([
        ocr_worker.EngineLine(text="CACHE_01", confidence=0.88, bbox=[1, 1, 20, 20], line_id="cached"),
    ])
    _patch_lease(monkeypatch, job, cached={"result_payload": prediction.to_payload()})
    monkeypatch.setattr(ocr_worker.ocr_jobs, "put_cached_result_for_lease", lambda *args, **kwargs: None)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "complete_job",
                        lambda job_id, token, **kwargs: completed.update(kwargs) or job)

    result = _run_once(ir, asset_root, engine, current=True)

    assert result.status == "succeeded"
    assert result.cache_hit is True
    assert engine.calls == 0
    assert completed["result_payload"]["canonical_generation_id"] == GENERATION_ID
    assert completed["result_payload"]["observations"][0]["text"] == "CACHE_01"


def test_worker_marks_job_stale_before_reading_source(monkeypatch):
    job = {"id": 9, "world": "world-a", "canonical_generation_id": GENERATION_ID, "lease_token": "token"}
    calls = []
    _patch_lease(monkeypatch, job, cached="unset")
    monkeypatch.setattr(ocr_worker.ocr_jobs, "mark_stale", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = _run_once_unreadable(current=False)
    assert result.status == "stale"
    assert calls and calls[0][0] == (9, "token")


def test_worker_excludes_sensitive_source_before_reading_body(monkeypatch, tmp_path):
    # 更新前に投入された秘匿名ジョブは `load_ir`（本文＝画像読み取り）へ到達する前に対象外化する。
    # 再試行させない（mark_stale で終端）・失敗件数に数えない（excluded_sensitive≠failed）。
    _ir, _asset_root, _decision, job = _picture_route(tmp_path)
    job = {**job, "source_rel_path": "credentials.png"}
    calls = []
    _patch_lease(monkeypatch, job, cached="unset")
    monkeypatch.setattr(ocr_worker.ocr_jobs, "mark_stale", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = _run_once_unreadable(current=True)
    assert result.status == "excluded_sensitive"
    assert calls and calls[0][0] == (job["id"], job["lease_token"])


def test_fake_engine_protocol_still_supports_successful_non_cached_unit_run(monkeypatch, tmp_path):
    ir, asset_root, _decision, job = _picture_route(tmp_path)
    engine = FakeEngine()
    commits = []
    _patch_lease(monkeypatch, job, cached=None)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "put_cached_result_for_lease",
                        lambda *args, **kwargs: {"result_payload": args[-1]})
    monkeypatch.setattr(ocr_worker.ocr_jobs, "complete_job",
                        lambda job_id, token, **kwargs: commits.append(kwargs) or {**job, "status": "succeeded"})

    result = _run_once(ir, asset_root, engine)

    assert result.status == "succeeded" and result.cache_hit is False
    assert engine.calls == 1
    assert commits[0]["result_payload"]["observations"][0]["text"] == "  A_01  "


def test_failed_ocr_run_keeps_canonical_artifacts_byte_identical(monkeypatch, tmp_path):
    ir, asset_root, _decision, job = _picture_route(tmp_path)
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "design.evidence.json").write_text(evidence_ir.to_json_str(ir), encoding="utf-8")
    (canonical / "design.rag.md").write_text("原値はCANONICAL_01。\n", encoding="utf-8")
    (canonical / "design.rag_chunks.jsonl").write_text('{"search_text":"CANONICAL_01"}\n', encoding="utf-8")

    def _digests():
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in canonical.iterdir() if path.is_file()}

    before = _digests()

    class FailingEngine(FakeEngine):
        def predict(self, image_bytes: bytes, *, media_type: str) -> ocr_worker.OCRPrediction:
            raise RuntimeError("synthetic OCR failure")

    _patch_lease(monkeypatch, job, cached=None)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "fail_job", lambda *args, **kwargs: {**job, "status": "queued"})

    result = _run_once(ir, asset_root, FailingEngine())

    assert result.status == "failed" and result.error_code == "engine_failure"
    assert _digests() == before


def test_lease_loss_during_monitored_inference_never_commits_cache_or_job(monkeypatch, tmp_path):
    ir, asset_root, _decision, job = _picture_route(tmp_path)
    renewals = iter([True, True, False])
    writes = []

    class MonitoredFake(FakeEngine):
        def predict_monitored(self, image_bytes, *, media_type, timeout_seconds, on_tick):
            time.sleep(0.01)
            on_tick()
            raise AssertionError("lease loss must interrupt before a prediction is returned")

    _patch_lease(monkeypatch, job, cached=None)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "renew_lease", lambda *args, **kwargs: next(renewals))
    monkeypatch.setattr(ocr_worker.ocr_jobs, "put_cached_result_for_lease",
                        lambda *args, **kwargs: writes.append("cache"))
    monkeypatch.setattr(ocr_worker.ocr_jobs, "complete_job", lambda *args, **kwargs: writes.append("complete"))
    monkeypatch.setattr(ocr_worker.ocr_jobs, "fail_job", lambda *args, **kwargs: writes.append("failed"))

    result = _run_once(ir, asset_root, MonitoredFake(), lease_renew_interval_seconds=0.001)

    assert result.status == "lease_lost"
    assert writes == []


def test_unreadable_image_is_cancelled_once_without_retry(monkeypatch, tmp_path):
    ir, asset_root, decision, job = _picture_route(tmp_path)
    wmf = b"\xd7\xcd\xc6\x9a" + b"\x00" * 32
    (asset_root / decision.asset_rel_path).write_bytes(wmf)
    decision = ocr_router.OCRRouteDecision(
        **{**asdict(decision), "asset_sha256": "sha256:" + hashlib.sha256(wmf).hexdigest(),
           "pixel_size": None},
    )
    job = {**job, "route_input": asdict(decision)}
    failures = []
    cancels = []
    _patch_lease(monkeypatch, job, cached=None)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "fail_job",
                        lambda *args, **kwargs: failures.append(kwargs) or {**job, "status": "failed"})
    monkeypatch.setattr(ocr_worker.ocr_jobs, "cancel_unsupported_image_job",
                        lambda *args: cancels.append(args) or {**job, "status": "cancelled"})

    result = _run_once(ir, asset_root)

    assert result.status == "cancelled" and result.error_code == "unsupported_image_format"
    assert failures == []                                   # 失敗には数えない
    assert cancels == [(job["id"], job["lease_token"])]


# ===== 標準 publisher（世代確認・world_lock・公開後フック） =====

def _patch_snapshot(monkeypatch, *, snapshot=EMPTY_SNAPSHOT, rows=(), publish=None):
    monkeypatch.setattr(ocr_worker.ocr_jobs, "succeeded_results_snapshot", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "iter_succeeded_results", lambda *args, **kwargs: iter(rows))
    monkeypatch.setattr(ocr_worker.observation_render, "publish_snapshot_stream",
                        publish or (lambda *args, **kwargs: {"status": "published"}))


def _no_ir(row):
    raise AssertionError("no rows")


def _publish_job():
    return {"world": "world-a", "canonical_generation_id": GENERATION_ID}


@pytest.mark.parametrize("post_publish_fails", [False, True], ids=["success", "post-publish-fails"])
def test_standard_publisher_runs_post_publish_before_marking_jobs(monkeypatch, tmp_path, post_publish_fails):
    # 公開後フック（再索引）が先・成功した時だけ job を公開済みにする。失敗は握り潰さず伝播し、job は未公開のまま。
    events = []
    _patch_snapshot(monkeypatch)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "mark_snapshot_artifacts_published",
                        lambda world, generation, selected, **kwargs: events.append(("marked", world, generation, selected)))
    monkeypatch.setattr(ocr_worker, "world_lock", lambda world: nullcontext())

    def _reindex(world, generation):
        if post_publish_fails:
            raise RuntimeError("index unavailable")
        events.append(("reindexed", world, generation))

    publisher = ocr_worker.build_standard_publish_callback(
        resolve_derived_root=lambda world: tmp_path,
        canonical_is_current=lambda world, generation: True,
        load_ir=_no_ir,
        on_published=_reindex,
    )

    if post_publish_fails:
        with pytest.raises(RuntimeError, match="^index unavailable$"):
            publisher(_publish_job(), None)
        assert events == []
    else:
        publisher(_publish_job(), None)
        assert events == [
            ("reindexed", "world-a", GENERATION_ID),
            ("marked", "world-a", GENERATION_ID, EMPTY_SNAPSHOT),
        ]


def test_standard_publisher_rechecks_generation_and_reindexes_inside_world_lock(monkeypatch, tmp_path):
    events = []
    lock_depth = 0
    _patch_snapshot(monkeypatch)

    @contextmanager
    def locked(world):
        nonlocal lock_depth
        events.append(("lock_enter", world))
        lock_depth += 1
        try:
            yield
        finally:
            lock_depth -= 1
            events.append(("lock_exit", world))

    def inside_lock(name):
        def _record(*args, **kwargs):
            assert lock_depth == 1
            events.append((name, *args))
            return True
        return _record

    monkeypatch.setattr(ocr_worker, "world_lock", locked)
    monkeypatch.setattr(ocr_worker, "garbage_collect_observation_generations",
                        lambda root, *, active_canonical_generation_id: inside_lock("gc")(root))
    monkeypatch.setattr(ocr_worker.ocr_jobs, "mark_snapshot_artifacts_published", inside_lock("mark"))
    publisher = ocr_worker.build_standard_publish_callback(
        resolve_derived_root=lambda _world: tmp_path,
        canonical_is_current=inside_lock("current"),
        load_ir=_no_ir,
        on_published=inside_lock("reindex"),
    )

    publisher(_publish_job(), None)

    assert [event[0] for event in events] == ["lock_enter", "current", "reindex", "gc", "mark", "lock_exit"]


def test_standard_publisher_stale_after_pointer_publish_never_reindexes_or_marks(monkeypatch, tmp_path):
    events = []
    _patch_snapshot(monkeypatch)
    monkeypatch.setattr(ocr_worker, "world_lock", lambda world: nullcontext())
    monkeypatch.setattr(ocr_worker, "garbage_collect_observation_generations",
                        lambda *args, **kwargs: events.append("gc"))
    monkeypatch.setattr(ocr_worker.ocr_jobs, "mark_snapshot_artifacts_published",
                        lambda *args, **kwargs: events.append("mark"))
    publisher = ocr_worker.build_standard_publish_callback(
        resolve_derived_root=lambda _world: tmp_path,
        canonical_is_current=lambda world, generation: False,
        load_ir=_no_ir,
        on_published=lambda world, generation: events.append("reindex"),
    )

    publisher(_publish_job(), None)

    assert events == []


def test_standard_publisher_skips_sensitive_succeeded_rows_without_loading_ir(monkeypatch, tmp_path):
    # 更新前に成功した秘匿名ジョブは再公開経路でも Evidence を読み直さず、records にも出さない。
    loaded = []
    captured = {}
    rows = [{"source_rel_path": "img/credentials.png", "result_payload": {}, "result_observation_set_hash": "x"}]

    def fake_publish_snapshot_stream(*args, **kwargs):
        captured["n"] = sum(1 for _ in kwargs["records"])
        return {"status": "published"}

    _patch_snapshot(monkeypatch, snapshot={"row_count": 1, "min_id": 1, "max_id": 1, "id_sum": 1},
                    rows=rows, publish=fake_publish_snapshot_stream)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "mark_snapshot_artifacts_published", lambda *a, **kw: None)
    monkeypatch.setattr(ocr_worker, "world_lock", lambda world: nullcontext())
    publisher = ocr_worker.build_standard_publish_callback(
        resolve_derived_root=lambda world: tmp_path,
        canonical_is_current=lambda world, generation: True,
        load_ir=lambda row: loaded.append(row) or object(),
        on_published=lambda world, generation: None,
    )
    publisher(_publish_job(), None)
    assert loaded == []
    assert captured.get("n") == 0


def _patch_runtime_worlds(monkeypatch, tmp_path, *, active=GENERATION_ID):
    from sherpa import worlds
    from sherpa.ingest import derived_generation

    monkeypatch.setattr(derived_generation, "active_generation_id",
                        active if callable(active) else (lambda _root: active))
    monkeypatch.setattr(worlds, "derived_dir", lambda _world: tmp_path / "canonical")
    monkeypatch.setattr(worlds, "observation_dir", lambda _world, **_kwargs: tmp_path / "observations")
    monkeypatch.setattr(worlds, "validate_ocr_registered_sources", lambda: tmp_path)
    monkeypatch.setattr(worlds, "validate_ocr_source_root", lambda root, **_kwargs: Path(root))
    monkeypatch.setattr(ocr_worker, "world_lock", lambda world: nullcontext())
    _patch_snapshot(monkeypatch)


def test_runtime_reindex_defensively_rechecks_generation_before_es_delete(monkeypatch, tmp_path):
    from sherpa import es_index

    active_generations = iter([GENERATION_ID, "d" * 64])
    indexed = []
    marked = []
    _patch_runtime_worlds(monkeypatch, tmp_path, active=lambda _root: next(active_generations))
    monkeypatch.setattr(es_index, "index_world", lambda *args, **kwargs: indexed.append((args, kwargs)))
    monkeypatch.setattr(ocr_worker.ocr_jobs, "mark_snapshot_artifacts_published",
                        lambda *args, **kwargs: marked.append((args, kwargs)))
    publisher = ocr_worker._runtime_callbacks()[-1]

    with pytest.raises(ocr_worker.OCRBindingError, match="changed before observation reindex"):
        publisher(_publish_job(), None)

    assert indexed == []
    assert marked == []


def test_runtime_reindex_observations_never_touches_es_or_human_md_marker(monkeypatch, tmp_path):
    # ocr_worker は隔離 profile（`/derived` read-only・ES 到達不可）のため、観測公開後フックが ES や
    # `.human_md_es_sig` へ触れると通常の公開のたびに必ず失敗する（ES は観測チャンクを読まず grep 経路で足りる）。
    from sherpa import es_index
    from sherpa.ingest import office_md, worker as ingest_worker

    _patch_runtime_worlds(monkeypatch, tmp_path)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "mark_snapshot_artifacts_published", lambda *args, **kwargs: None)
    from sherpa.ingest import background
    monkeypatch.setattr(background, "enqueue_ocr_catchup", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("OCR のコンテナは更新を予約しない（コンテナに版が無く、ホストの取り込み worker が拾えない）")))
    monkeypatch.setattr(background, "submit", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("OCR のコンテナは取り込みの依頼を積まない")))

    def _must_not_call(name):
        def _boom(*a, **kw):
            raise AssertionError(f"reindex_observations は {name} を呼んではいけない（隔離 profile では必ず失敗する）")
        return _boom
    monkeypatch.setattr(ingest_worker, "index_world_with_human_md_holdback",
                        _must_not_call("index_world_with_human_md_holdback"))
    monkeypatch.setattr(es_index, "index_world", _must_not_call("es_index.index_world"))
    monkeypatch.setattr(office_md, "drop_human_md_es_sig_marker", _must_not_call("drop_human_md_es_sig_marker"))
    monkeypatch.setattr(office_md, "confirm_human_md_es_sig", _must_not_call("confirm_human_md_es_sig"))

    ocr_worker._runtime_callbacks()[-1](_publish_job(), None)   # 例外が飛べば失敗


def test_runtime_callbacks_fail_closed_before_worker_loop_when_ocr_root_is_invalid(monkeypatch, tmp_path):
    from sherpa import worlds

    monkeypatch.setattr(worlds, "observation_dir", lambda _world, **_kwargs: tmp_path / "observations")

    def _invalid():
        raise ValueError("registered World is outside OCR root")

    monkeypatch.setattr(worlds, "validate_ocr_registered_sources", _invalid)
    with pytest.raises(ValueError, match="outside OCR root"):
        ocr_worker._runtime_callbacks()


def test_runtime_source_resolver_rechecks_ocr_root_for_each_job(monkeypatch, tmp_path):
    from sherpa import worlds

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setattr(worlds, "observation_dir", lambda _world, **_kwargs: tmp_path / "observations")
    monkeypatch.setattr(worlds, "validate_ocr_registered_sources", lambda: allowed)
    monkeypatch.setattr(worlds, "world_dir", lambda _world: outside)

    def _reject(_root, **_kwargs):
        raise ValueError("outside")

    monkeypatch.setattr(worlds, "validate_ocr_source_root", _reject)
    resolve_source = ocr_worker._runtime_callbacks()[2]
    with pytest.raises(ocr_worker.OCRBindingError, match="outside the configured OCR root"):
        resolve_source({"world": "new-world", "source_rel_path": "image.png"})


# ===== supervisor・世代 GC・シグナル =====

def test_paddle_supervisor_enforces_wall_clock_timeout_and_kills_hung_child(tmp_path):
    supervisor = ocr_worker.PaddleProcessSupervisor(
        tmp_path, start_method="fork", process_target=_hanging_inference_child,
    )
    started = time.monotonic()
    ticks = []
    with pytest.raises(TimeoutError, match="timeout"):
        supervisor.predict_monitored(
            b"pixels", media_type="image/png", timeout_seconds=0.15,
            poll_seconds=0.02, on_tick=lambda: ticks.append(time.monotonic()),
        )
    assert time.monotonic() - started < 2
    assert len(ticks) >= 3
    assert supervisor._process is None


def test_snapshot_callback_runs_only_when_generation_becomes_terminal(monkeypatch):
    readiness = iter([False, True])
    published = []
    monkeypatch.setattr(ocr_worker.ocr_jobs, "generation_ready_for_publication", lambda *args, **kwargs: next(readiness))

    def callback(job, observation):
        published.append((job["id"], observation))

    first = {"id": 1, "world": "w", "canonical_generation_id": GENERATION_ID}
    last = {"id": 2, "world": "w", "canonical_generation_id": GENERATION_ID}

    assert ocr_worker._publish_terminal_generation(first, callback, None, ocr_worker.profile_hash()) is False
    assert ocr_worker._publish_terminal_generation(last, callback, None, ocr_worker.profile_hash()) is True
    assert published == [(2, None)]


def _refresh_world(tmp_path, source_rel, refresh_id):
    ir, _asset_root, _decision, _job = _picture_route(tmp_path)
    generation_root = tmp_path / "canonical"
    route_path = generation_root / f"{source_rel}.ocr_route.json"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        ocr_router.to_json_str(ocr_router.build_manifest(ir, source_rel_path=source_rel, assets=[])),
        encoding="utf-8")
    (generation_root / f"{source_rel}.evidence.json").write_text(evidence_ir.to_json_str(ir), encoding="utf-8")
    refresh = {
        "id": refresh_id, "world": "world-a", "canonical_generation_id": GENERATION_ID,
        "engine_profile_hash": ocr_worker.profile_hash(), "lease_token": "refresh-token",
        "cursor_rel_path": None,
    }
    return generation_root, refresh


def _patch_refresh(monkeypatch, refresh, enqueue_calls, progress):
    monkeypatch.setattr(ocr_worker.ocr_jobs, "lease_refresh_run", lambda *args, **kwargs: refresh)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "renew_refresh_run", lambda *args, **kwargs: True)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "enqueue_manifest_jobs",
                        lambda *args, **kwargs: enqueue_calls.append(args) or [{"id": 1}])
    monkeypatch.setattr(ocr_worker.ocr_jobs, "update_refresh_run_progress",
                        lambda *args, **kwargs: progress.append(kwargs) or True)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "complete_refresh_run", lambda *args, **kwargs: refresh)


def _run_refresh(generation_root):
    return ocr_worker.run_refresh_once(
        "worker-1", engine_profile_hash=ocr_worker.profile_hash(),
        canonical_is_current=lambda world, generation: True,
        resolve_generation_root=lambda world, generation: generation_root,
    )


def test_refresh_worker_streams_manifests_and_persists_cursor(monkeypatch, tmp_path):
    generation_root, refresh = _refresh_world(tmp_path, "excel/JPX-015.xlsx", 7)
    enqueue_calls, progress = [], []
    _patch_refresh(monkeypatch, refresh, enqueue_calls, progress)

    result = _run_refresh(generation_root)

    assert result.status == "refresh_completed"
    assert result.manifests_processed == 1 and result.jobs_enqueued == 1
    assert progress[0]["cursor_rel_path"].endswith(".ocr_route.json")


def test_refresh_worker_pauses_without_consuming_attempts_when_switched_off_during_run(monkeypatch, tmp_path):
    generation_root, refresh = _refresh_world(tmp_path, "excel/JPX-015.xlsx", 9)
    enqueue_calls, progress, failed, paused = [], [], [], []
    _patch_refresh(monkeypatch, refresh, enqueue_calls, progress)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "fail_refresh_run",
                        lambda *args, **kwargs: failed.append(kwargs))
    monkeypatch.setattr(ocr_worker.ocr_jobs, "pause_refresh_run",
                        lambda *args, **kwargs: paused.append(kwargs))
    monkeypatch.setattr(ocr_worker.ocr_switch, "lease_refresh_run", lambda *a, **kw: refresh)
    monkeypatch.setattr(ocr_worker.ocr_switch, "enabled", lambda *a, **kw: False)

    result = _run_refresh(generation_root)

    assert result.status == "stopping"
    assert enqueue_calls == [] and progress == []
    assert failed == [] and len(paused) == 1


def test_refresh_worker_excludes_sensitive_evidence_and_does_not_reenqueue(monkeypatch, tmp_path):
    # 秘匿名の Evidence は読まず（`from_json_str` へ到達させない）、再投入もしない。
    generation_root, refresh = _refresh_world(tmp_path, "credentials.png", 8)
    enqueue_calls, progress = [], []
    _patch_refresh(monkeypatch, refresh, enqueue_calls, progress)
    monkeypatch.setattr(ocr_worker.evidence_ir, "from_json_str",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not read sensitive evidence")))

    result = _run_refresh(generation_root)

    assert result.status == "refresh_completed"
    assert result.manifests_processed == 0 and result.jobs_enqueued == 0
    assert enqueue_calls == []
    assert progress == []


def test_observation_generation_gc_keeps_pointer_current_and_previous(tmp_path):
    active = "a" * 64
    old_canonical = "b" * 64
    current, previous, oldest = "1" * 64, "2" * 64, "3" * 64
    base = tmp_path / ocr_worker.observation_render.OBSERVATION_GENERATIONS_NAME
    for generation in (current, previous, oldest):
        (base / active / generation).mkdir(parents=True)
    (base / active / ".staging-concurrent").mkdir(parents=True)
    (base / old_canonical / ("4" * 64)).mkdir(parents=True)
    (tmp_path / ocr_worker.observation_render.OBSERVATION_POINTER_NAME).write_text(json.dumps({
        "schema": ocr_worker.observation_render.OBSERVATION_POINTER_SCHEMA,
        "canonical_generation_id": active,
        "observation_generation_id": current,
        "previous_observation_generation_id": previous,
    }), encoding="utf-8")

    result = ocr_worker.garbage_collect_observation_generations(tmp_path, active_canonical_generation_id=active)

    assert {path.name for path in (base / active).iterdir()} == {current, previous, ".staging-concurrent"}
    assert not (base / old_canonical).exists()
    assert result == {"generations_removed": 1, "canonical_roots_removed": 1}


def test_sigterm_sets_stop_flag_and_records_stopping_heartbeat(monkeypatch, tmp_path):
    handlers = {}
    heartbeat_statuses = []

    def fake_signal(signum, handler):
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        return previous

    availability = ocr_worker.OCRAvailability(
        available=True, unavailable_reason=None, model_hashes_valid=True, cache_home=str(tmp_path),
        paddleocr_version="3.7.0", paddlepaddle_version="3.3.0", pypdfium2_version="5.11.0",
        pillow_version="12.3.0", model_hashes={}, engine_profile_hash=ocr_worker.profile_hash(),
    )

    class FakeSupervisor:
        engine_profile_hash = ocr_worker.profile_hash()

        def __init__(self, cache_home):
            self.cache_home = cache_home

        def close(self):
            return None

    def fake_run_once(*args, **kwargs):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert kwargs["should_stop"]() is True
        return ocr_worker.WorkerResult(status="stopping")

    monkeypatch.setattr(ocr_worker.signal, "signal", fake_signal)
    monkeypatch.setattr(ocr_worker, "paddle_availability", lambda cache: availability)
    monkeypatch.setattr(ocr_worker, "PaddleProcessSupervisor", FakeSupervisor)
    monkeypatch.setattr(
        ocr_worker, "_runtime_callbacks",
        lambda: (
            lambda *args: True, lambda *args: None, lambda *args: SOURCE, lambda *args: tmp_path,
            lambda *args: tmp_path, lambda *args: None,
        ),
    )
    monkeypatch.setattr(ocr_worker, "run_once", fake_run_once)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "record_worker_heartbeat",
                        lambda *args, **kwargs: heartbeat_statuses.append(kwargs["status"]) or {})
    monkeypatch.setattr(ocr_worker.ocr_jobs, "retire_other_profile_work", lambda *args, **kwargs: {})

    assert ocr_worker.main(["--worker-id", "test-worker", "--poll-seconds", "0.01"]) == 0
    assert heartbeat_statuses[-1] == "stopping"


def test_idle_polls_print_one_line_until_status_changes(monkeypatch, tmp_path, capsys):
    """待機が続く間、ワーカーは待機の行を 1 回だけ出す（2 秒ごとの出力でログが膨らまない）。"""
    availability = ocr_worker.OCRAvailability(
        available=True, unavailable_reason=None, model_hashes_valid=True, cache_home=str(tmp_path),
        paddleocr_version="3.7.0", paddlepaddle_version="3.3.0", pypdfium2_version="5.11.0",
        pillow_version="12.3.0", model_hashes={}, engine_profile_hash=ocr_worker.profile_hash(),
    )

    class FakeSupervisor:
        engine_profile_hash = ocr_worker.profile_hash()

        def __init__(self, cache_home):
            pass

        def close(self):
            return None

    statuses = iter(["idle", "idle", "idle", "succeeded", "idle", "idle"])
    handlers = {}

    def fake_signal(signum, handler):
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        return previous

    def fake_run_once(*args, **kwargs):
        status = next(statuses, None)
        if status is None:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return ocr_worker.WorkerResult(status="stopping")
        return ocr_worker.WorkerResult(status=status)

    monkeypatch.setattr(ocr_worker.signal, "signal", fake_signal)
    monkeypatch.setattr(ocr_worker, "paddle_availability", lambda cache: availability)
    monkeypatch.setattr(ocr_worker, "PaddleProcessSupervisor", FakeSupervisor)
    monkeypatch.setattr(
        ocr_worker, "_runtime_callbacks",
        lambda: (
            lambda *args: True, lambda *args: None, lambda *args: SOURCE, lambda *args: tmp_path,
            lambda *args: tmp_path, lambda *args: None,
        ),
    )
    monkeypatch.setattr(ocr_worker, "run_once", fake_run_once)
    monkeypatch.setattr(ocr_worker, "run_refresh_once",
                        lambda *args, **kwargs: ocr_worker.WorkerResult(status="idle"))
    monkeypatch.setattr(ocr_worker.ocr_jobs, "list_unpublished_generations", lambda **kwargs: [])
    monkeypatch.setattr(ocr_worker.ocr_jobs, "record_worker_heartbeat", lambda *args, **kwargs: {})
    monkeypatch.setattr(ocr_worker.ocr_jobs, "retire_other_profile_work", lambda *args, **kwargs: {})

    assert ocr_worker.main(["--worker-id", "test-worker", "--poll-seconds", "0.001"]) == 0
    printed = [json.loads(line)["status"] for line in capsys.readouterr().out.splitlines()]
    assert printed == ["idle", "succeeded", "idle", "stopping"]


class _ClosableEngine:
    def __init__(self):
        self.closes = 0

    def close(self):
        self.closes += 1


def test_idle_unload_closes_engine_once_after_the_wait_and_reloads_on_next_work():
    now = [0.0]
    engine = _ClosableEngine()
    unload = ocr_worker.IdleUnload(300, clock=lambda: now[0])

    assert unload.idle(engine) is False  # 待ちの始まり
    now[0] = 299
    assert unload.idle(engine) is False and engine.closes == 0
    now[0] = 300
    assert unload.idle(engine) is True and engine.closes == 1 and unload.unloaded
    now[0] = 900
    assert unload.idle(engine) is False and engine.closes == 1  # 手放した後は重ねて閉じない

    unload.worked()
    assert unload.unloaded is False
    assert unload.idle(engine) is False
    now[0] = 1199
    assert unload.idle(engine) is False
    now[0] = 1200
    assert unload.idle(engine) is True and engine.closes == 2


def test_idle_unload_work_in_between_restarts_the_wait():
    now = [0.0]
    engine = _ClosableEngine()
    unload = ocr_worker.IdleUnload(300, clock=lambda: now[0])
    unload.idle(engine)
    now[0] = 250
    unload.worked()
    now[0] = 400
    assert unload.idle(engine) is False
    now[0] = 699
    assert unload.idle(engine) is False and engine.closes == 0


def test_idle_unload_zero_disables():
    now = [0.0]
    engine = _ClosableEngine()
    unload = ocr_worker.IdleUnload(0, clock=lambda: now[0])
    for now[0] in (0, 10_000, 1_000_000):
        assert unload.idle(engine) is False
    assert engine.closes == 0 and unload.unloaded is False


@pytest.mark.parametrize("raw, expected", [
    (None, 300), ("", 300), ("abc", 300), ("1.5", 300), ("-5", 300), ("0", 0), ("60", 60),
])
def test_idle_unload_seconds_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("SHERPA_OCR_IDLE_UNLOAD_SECONDS", raising=False)
    else:
        monkeypatch.setenv("SHERPA_OCR_IDLE_UNLOAD_SECONDS", raw)
    assert ocr_worker._idle_unload_seconds() == expected


def test_idle_unload_ends_the_inference_child_and_the_next_job_loads_it_again(tmp_path):
    supervisor = ocr_worker.PaddleProcessSupervisor(
        tmp_path, start_method="fork", process_target=_echo_inference_child,
    )
    now = [0.0]
    unload = ocr_worker.IdleUnload(300, clock=lambda: now[0])
    try:
        assert supervisor.predict_monitored(
            b"pixels", media_type="image/png", timeout_seconds=10, on_tick=lambda: None, poll_seconds=0.05,
        ).observations == []
        first = supervisor._process
        assert first is not None and first.is_alive()

        unload.idle(supervisor)
        now[0] = 300
        assert unload.idle(supervisor) is True
        assert supervisor._process is None and not first.is_alive()

        unload.worked()
        assert supervisor.predict_monitored(
            b"pixels", media_type="image/png", timeout_seconds=10, on_tick=lambda: None, poll_seconds=0.05,
        ).observations == []
        second = supervisor._process
        assert second is not None and second is not first and second.is_alive()
    finally:
        supervisor.close()


def _run_main_with_idle_polls(monkeypatch, tmp_path, statuses, env_seconds):
    availability = ocr_worker.OCRAvailability(
        available=True, unavailable_reason=None, model_hashes_valid=True, cache_home=str(tmp_path),
        paddleocr_version="3.7.0", paddlepaddle_version="3.3.0", pypdfium2_version="5.11.0",
        pillow_version="12.3.0", model_hashes={}, engine_profile_hash=ocr_worker.profile_hash(),
    )
    engines = []

    class FakeSupervisor:
        engine_profile_hash = ocr_worker.profile_hash()

        def __init__(self, cache_home):
            self.closes = 0
            engines.append(self)

        def close(self):
            self.closes += 1

    handlers = {}
    beats = []
    closes_at_job = []
    queue_ = iter(statuses)
    now = [0.0]

    def fake_clock():
        now[0] += 100
        return now[0]

    def fake_signal(signum, handler):
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        return previous

    def fake_run_once(*args, **kwargs):
        status = next(queue_, None)
        if status is None:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return ocr_worker.WorkerResult(status="stopping")
        if status != "idle":
            closes_at_job.append(engines[0].closes)
        return ocr_worker.WorkerResult(status=status)

    original = ocr_worker.IdleUnload
    monkeypatch.setenv("SHERPA_OCR_IDLE_UNLOAD_SECONDS", env_seconds)
    monkeypatch.setattr(ocr_worker, "IdleUnload", lambda seconds: original(seconds, clock=fake_clock))
    monkeypatch.setattr(ocr_worker.signal, "signal", fake_signal)
    monkeypatch.setattr(ocr_worker, "paddle_availability", lambda cache: availability)
    monkeypatch.setattr(ocr_worker, "PaddleProcessSupervisor", FakeSupervisor)
    monkeypatch.setattr(
        ocr_worker, "_runtime_callbacks",
        lambda: (
            lambda *args: True, lambda *args: None, lambda *args: SOURCE, lambda *args: tmp_path,
            lambda *args: tmp_path, lambda *args: None,
        ),
    )
    monkeypatch.setattr(ocr_worker, "run_once", fake_run_once)
    monkeypatch.setattr(ocr_worker, "run_refresh_once",
                        lambda *args, **kwargs: ocr_worker.WorkerResult(status="idle"))
    monkeypatch.setattr(ocr_worker.ocr_jobs, "list_unpublished_generations", lambda **kwargs: [])
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "record_worker_heartbeat",
        lambda *args, **kwargs: beats.append((kwargs["status"], kwargs["metadata"]["idle_unloaded"])) or {},
    )
    monkeypatch.setattr(ocr_worker.ocr_jobs, "retire_other_profile_work", lambda *args, **kwargs: {})

    assert ocr_worker.main(["--worker-id", "test-worker", "--poll-seconds", "0.001"]) == 0
    return engines[0], beats, closes_at_job


def test_worker_loop_releases_the_model_after_the_wait_and_marks_the_heartbeat(monkeypatch, tmp_path, capsys):
    engine, beats, closes_at_job = _run_main_with_idle_polls(
        monkeypatch, tmp_path, ["idle"] * 5 + ["succeeded", "idle"], "300",
    )
    # 4 回目の待ちで 300 秒に届き手放す。以降の待ちは「解放済み」、仕事を取ると戻る
    assert [flag for status, flag in beats if status == "idle"] == [False, False, False, False, True, True, False, False]
    assert closes_at_job == [1]
    assert engine.closes == 2  # 待ちでの解放 1 回 + 終了時の後始末
    printed = [json.loads(line)["status"] for line in capsys.readouterr().out.splitlines()]
    assert printed.count("idle_unloaded") == 1


def test_worker_loop_keeps_the_model_when_idle_unload_is_zero(monkeypatch, tmp_path):
    engine, beats, closes_at_job = _run_main_with_idle_polls(
        monkeypatch, tmp_path, ["idle"] * 8 + ["succeeded", "idle"], "0",
    )
    assert not any(flag for _status, flag in beats)
    assert closes_at_job == [0] and engine.closes == 1  # 終了時の後始末だけ


def test_run_once_leases_only_the_engine_profile(monkeypatch, tmp_path):
    ir, asset_root, _decision, _job = _picture_route(tmp_path)
    seen = {}

    def lease(worker_id, lease_seconds, **kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(ocr_worker.ocr_jobs, "lease_next", lease)

    assert _run_once(ir, asset_root).status == "idle"
    assert seen == {"engine_profile_hash": FakeEngine.engine_profile_hash}


def test_refresh_run_leases_only_the_engine_profile(monkeypatch, tmp_path):
    seen = {}

    def lease(worker_id, **kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(ocr_worker.ocr_jobs, "lease_refresh_run", lease)

    result = ocr_worker.run_refresh_once(
        "worker-1", engine_profile_hash=FakeEngine.engine_profile_hash,
        canonical_is_current=lambda world, generation: True,
        resolve_generation_root=lambda world, generation: tmp_path,
    )

    assert result.status == "idle"
    assert seen["engine_profile_hash"] == FakeEngine.engine_profile_hash


def test_publish_callback_reads_and_marks_only_the_current_profile(monkeypatch, tmp_path):
    seen = {}

    def record(name, value):
        def _fn(*args, **kwargs):
            seen[name] = kwargs.get("engine_profile_hash")
            return value
        return _fn

    def publish_stream(*args, **kwargs):
        list(kwargs["records"])
        return {"status": "published"}

    monkeypatch.setattr(ocr_worker.ocr_jobs, "succeeded_results_snapshot", record("snapshot", EMPTY_SNAPSHOT))
    monkeypatch.setattr(ocr_worker.ocr_jobs, "iter_succeeded_results", record("iter", iter(())))
    monkeypatch.setattr(ocr_worker.ocr_jobs, "mark_snapshot_artifacts_published", record("mark", 0))
    monkeypatch.setattr(ocr_worker.observation_render, "publish_snapshot_stream", publish_stream)
    monkeypatch.setattr(ocr_worker, "world_lock", lambda world: nullcontext())
    monkeypatch.setattr(ocr_worker, "garbage_collect_observation_generations", lambda *args, **kwargs: {})
    publisher = ocr_worker.build_standard_publish_callback(
        resolve_derived_root=lambda world: tmp_path,
        canonical_is_current=lambda world, generation: True,
        load_ir=_no_ir,
    )

    publisher(_publish_job(), None)

    assert seen == {
        "snapshot": ocr_worker.profile_hash(), "iter": ocr_worker.profile_hash(), "mark": ocr_worker.profile_hash(),
    }


def test_publishing_with_results_of_two_engine_profiles_keeps_only_the_current_one(monkeypatch, tmp_path):
    """新旧 profile の成功結果が並んでいても、現行 profile の結果だけを公開し、観測が合流で落ちない（実 PostgreSQL）。"""
    import uuid
    from psycopg.types.json import Json
    from sherpa import store
    from sherpa.ingest import office_md
    from sherpa.store import ocr_jobs
    try:
        store.init_schema()
    except Exception as exc:
        pytest.skip(f"infra down: {exc}")

    class OldEngine(FakeEngine):
        engine_profile_hash = "sha256:" + "e" * 64

    ir, asset_root, decision, job = _picture_route(tmp_path)
    prepared = ocr_worker.prepare_input(job, decision, source_path=SOURCE, asset_root=asset_root)
    prediction = FakeEngine().predict(prepared.image_bytes, media_type=prepared.media_type)
    sets = {
        engine.engine_profile_hash: ocr_worker.build_observation_set(
            ir=ir, decision=decision, prepared=prepared, prediction=prediction,
            canonical_generation_id=GENERATION_ID, engine=engine,
        ) for engine in (OldEngine(), FakeEngine())
    }
    old_set, new_set = sets[OldEngine.engine_profile_hash], sets[FakeEngine.engine_profile_hash]
    with pytest.raises(ValueError, match="duplicate observation id"):
        ai_observation.merge_sets([old_set, new_set], ir=ir)    # 両方を公開すると合流で必ず落ちる組

    world = "test-ocr-profile-" + uuid.uuid4().hex
    try:
        with ocr_jobs._connect() as connection:
            for profile, observation_set in sets.items():
                connection.execute(
                    "INSERT INTO ocr_jobs (world, source_rel_path, canonical_generation_id, source_content_hash, "
                    "route_manifest_hash, route_input_id, route_input, engine_profile_hash, status, "
                    "result_observation_set_hash, result_payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'succeeded',%s,%s)",
                    (
                        world, job["source_rel_path"], GENERATION_ID, job["source_content_hash"],
                        job["route_manifest_hash"], decision.route_input_id, Json(job["route_input"]), profile,
                        observation_set.observation_set_hash,
                        Json(json.loads(ai_observation.to_json_str(observation_set))),
                    ),
                )
        monkeypatch.setattr(ocr_worker, "world_lock", lambda _world: nullcontext())
        publisher = ocr_worker.build_standard_publish_callback(
            resolve_derived_root=lambda _world: tmp_path / "obs",
            canonical_is_current=lambda _world, _generation: True,
            load_ir=lambda _row: ir,
            engine_profile_hash=FakeEngine.engine_profile_hash,
        )

        publisher({"world": world, "canonical_generation_id": GENERATION_ID}, None)

        published = list((tmp_path / "obs").rglob("*.ai_observations.jsonl"))
        assert len(published) == 1
        obs_root = published[0].parents[len(Path(job["source_rel_path"]).parts) - 1]
        loaded = office_md._load_ocr_observation_sets(ir, job["source_rel_path"], obs_root)
        assert [item.observation_set_hash for item in loaded] == [new_set.observation_set_hash]
        merged = office_md._build_observation_set(
            ir, job["source_rel_path"], tmp_path, obs_dir=obs_root, with_vlm=False)
        assert merged is not None and merged.observations
        assert ocr_jobs.generation_state(
            world, GENERATION_ID, engine_profile_hash=FakeEngine.engine_profile_hash)["unpublished_jobs"] == 0
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_identical_copies_at_two_paths_each_get_an_observation_with_one_inference(monkeypatch, tmp_path):
    """同じ内容の原本を 2 つのパスに置いても、ルート→積む→読む→公開→読み込みで両方に観測が付き、推論は 1 回で済む（実 PostgreSQL）。"""
    import uuid
    from sherpa import store
    from sherpa.ingest import office_md
    from sherpa.store import ocr_jobs
    try:
        store.init_schema()
    except Exception as exc:
        pytest.skip(f"infra down: {exc}")

    ir = evidence_spike.extract(SOURCE)
    asset_root = tmp_path / "assets"
    evidence_spike.extract_assets(SOURCE, ir, asset_root)
    paths = ("copy-a/JPX-015.xlsx", "copy-b/nested/JPX-015.xlsx")
    manifests = [
        ocr_router.build_manifest(ir, source_rel_path=path, assets=ocr_router.inventory_assets(asset_root))
        for path in paths
    ]
    selected = [[item for item in manifest.decisions if item.status == "selected"] for manifest in manifests]
    assert selected[0] and [d.route_input_id for d in selected[0]] == [d.route_input_id for d in selected[1]]

    world = "test-ocr-copies-" + uuid.uuid4().hex
    engine = FakeEngine()
    real_lease_next = ocr_jobs.lease_next
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "lease_next", lambda worker_id, **kw: real_lease_next(worker_id, world=world, **kw))
    monkeypatch.setattr(ocr_worker, "world_lock", lambda _world: nullcontext())
    publisher = ocr_worker.build_standard_publish_callback(
        resolve_derived_root=lambda _world: tmp_path / "obs",
        canonical_is_current=lambda _world, _generation: True,
        load_ir=lambda _row: ir,
        engine_profile_hash=engine.engine_profile_hash,
    )
    try:
        for manifest in manifests:
            rows = ocr_jobs.enqueue_manifest_jobs(
                world, manifest, canonical_generation_id=GENERATION_ID, engine_profile_hash=engine.engine_profile_hash)
            assert len(rows) == len(selected[0])
        results = []
        while True:
            result = _run_once(ir, asset_root, engine, publish_observation=publisher)
            if result.status == "idle":
                break
            results.append(result)

        assert [r.status for r in results] == ["succeeded"] * (len(paths) * len(selected[0]))
        assert engine.calls == len(selected[0])
        assert sum(1 for r in results if r.cache_hit) == len(selected[0])
        for path in paths:
            matched = [
                f for f in (tmp_path / "obs").rglob("*.ai_observations.jsonl")
                if f.as_posix().endswith(f"/{path}.ai_observations.jsonl")
            ]
            assert len(matched) == 1, path
            obs_root = matched[0].parents[len(Path(path).parts) - 1]
            loaded = office_md._load_ocr_observation_sets(ir, path, obs_root)
            assert len(loaded) == 1 and loaded[0].observations, path
        assert ocr_jobs.generation_state(
            world, GENERATION_ID, engine_profile_hash=engine.engine_profile_hash)["unpublished_jobs"] == 0
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_refresh_expansion_queues_one_job_per_copy_of_identical_content(monkeypatch, tmp_path):
    """作り直したルートを展開すると、同じ内容のコピーごとに job が積まれる（実 PostgreSQL）。"""
    import uuid
    from sherpa import store
    from sherpa.store import ocr_jobs
    try:
        store.init_schema()
    except Exception as exc:
        pytest.skip(f"infra down: {exc}")

    ir = evidence_spike.extract(SOURCE)
    asset_root = tmp_path / "assets"
    evidence_spike.extract_assets(SOURCE, ir, asset_root)
    paths = ("copy-a/JPX-015.xlsx", "copy-b/nested/JPX-015.xlsx")
    generation_root = tmp_path / "canonical"
    for path in paths:
        route_path = generation_root / f"{path}.ocr_route.json"
        route_path.parent.mkdir(parents=True)
        route_path.write_text(ocr_router.to_json_str(ocr_router.build_manifest(
            ir, source_rel_path=path, assets=ocr_router.inventory_assets(asset_root))), encoding="utf-8")
        (generation_root / f"{path}.evidence.json").write_text(evidence_ir.to_json_str(ir), encoding="utf-8")

    world = "test-ocr-refresh-copies-" + uuid.uuid4().hex
    real_lease = ocr_jobs.lease_refresh_run
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "lease_refresh_run", lambda worker_id, **kw: real_lease(worker_id, world=world, **kw))
    try:
        ocr_jobs.enqueue_refresh_run(world, GENERATION_ID, FakeEngine.engine_profile_hash)
        result = _run_refresh(generation_root)

        assert result.status == "refresh_completed" and result.manifests_processed == len(paths)
        with ocr_jobs._connect() as connection:
            queued = connection.execute(
                "SELECT source_rel_path FROM ocr_jobs WHERE world=%s ORDER BY source_rel_path", (world,)
            ).fetchall()
        assert sorted({row["source_rel_path"] for row in queued}) == sorted(paths)
        assert len(queued) == result.jobs_enqueued
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_worker_start_retires_work_of_other_profiles_before_the_loop(monkeypatch, tmp_path):
    calls = []
    handlers = {}
    availability = ocr_worker.OCRAvailability(
        available=True, unavailable_reason=None, model_hashes_valid=True, cache_home=str(tmp_path),
        paddleocr_version="3.7.0", paddlepaddle_version="3.3.0", pypdfium2_version="5.11.0",
        pillow_version="12.3.0", model_hashes={}, engine_profile_hash=ocr_worker.profile_hash(),
    )

    class FakeSupervisor:
        engine_profile_hash = ocr_worker.profile_hash()

        def __init__(self, cache_home):
            pass

        def close(self):
            return None

    def stop(*args, **kwargs):
        assert calls == [FakeSupervisor.engine_profile_hash]     # 最初の job を取る前に終端済み
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return ocr_worker.WorkerResult(status="stopping")

    def fake_signal(signum, handler):
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        return previous

    monkeypatch.setattr(ocr_worker.signal, "signal", fake_signal)
    monkeypatch.setattr(ocr_worker, "paddle_availability", lambda cache: availability)
    monkeypatch.setattr(ocr_worker, "PaddleProcessSupervisor", FakeSupervisor)
    monkeypatch.setattr(
        ocr_worker, "_runtime_callbacks",
        lambda: (
            lambda *args: True, lambda *args: None, lambda *args: SOURCE, lambda *args: tmp_path,
            lambda *args: tmp_path, lambda *args: None,
        ),
    )
    monkeypatch.setattr(ocr_worker, "run_once", stop)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "record_worker_heartbeat", lambda *args, **kwargs: {})
    monkeypatch.setattr(ocr_worker.ocr_jobs, "retire_other_profile_work",
                        lambda profile, **kwargs: calls.append(profile) or {})

    assert ocr_worker.main(["--worker-id", "test-worker", "--poll-seconds", "0.001"]) == 0
    assert calls == [FakeSupervisor.engine_profile_hash]


def _limit_profile(side=1000, pixels=500_000):
    import dataclasses
    return dataclasses.replace(ocr_worker.PADDLE_CPU_PROFILE, max_image_side=side, max_image_pixels=pixels)


def test_downscale_settings_are_part_of_the_engine_profile(monkeypatch):
    base = ocr_worker.profile_hash()
    assert ocr_worker.profile_hash(_limit_profile(side=1234, pixels=ocr_worker.PADDLE_CPU_PROFILE.max_image_pixels)) != base
    assert ocr_worker.profile_hash(_limit_profile(side=ocr_worker.PADDLE_CPU_PROFILE.max_image_side, pixels=1_234_567)) != base

    monkeypatch.setenv("SHERPA_OCR_MAX_IMAGE_SIDE", "1800")
    monkeypatch.setenv("SHERPA_OCR_MAX_IMAGE_PIXELS", "3000000")
    from_env = ocr_worker.PaddleCPUProfile()
    assert (from_env.max_image_side, from_env.max_image_pixels) == (1800, 3_000_000)
    assert ocr_worker.profile_hash(from_env) != base


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "999999999", ""])
def test_invalid_downscale_env_falls_back_to_defaults(monkeypatch, raw):
    monkeypatch.setenv("SHERPA_OCR_MAX_IMAGE_SIDE", raw)
    monkeypatch.setenv("SHERPA_OCR_MAX_IMAGE_PIXELS", raw)
    assert ocr_worker.PaddleCPUProfile() == ocr_worker.PaddleCPUProfile(max_image_side=2560, max_image_pixels=6_000_000)


def test_default_model_is_small_and_each_choice_changes_the_profile_hash_and_the_model_names(monkeypatch):
    monkeypatch.delenv("SHERPA_OCR_MODEL", raising=False)
    small = ocr_worker.PaddleCPUProfile()
    assert (small.detection_model, small.recognition_model) == ("PP-OCRv6_small_det", "PP-OCRv6_small_rec")
    assert ocr_worker.DEFAULT_OCR_MODEL == "small"

    monkeypatch.setenv("SHERPA_OCR_MODEL", "medium")
    medium = ocr_worker.PaddleCPUProfile()
    assert (medium.detection_model, medium.recognition_model) == ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec")

    monkeypatch.setenv("SHERPA_OCR_MODEL", "mobile")
    mobile = ocr_worker.PaddleCPUProfile()
    assert (mobile.detection_model, mobile.recognition_model) == ("PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec")
    assert (mobile.detection_model_tree_sha256, mobile.recognition_model_tree_sha256) == (
        ocr_worker.OCR_MODEL_SETS["mobile"].detection_tree_sha256,
        ocr_worker.OCR_MODEL_SETS["mobile"].recognition_tree_sha256,
    )
    assert len({ocr_worker.profile_hash(item) for item in (small, medium, mobile)}) == 3

    monkeypatch.setenv("SHERPA_OCR_MODEL", " Small ")
    assert ocr_worker.PaddleCPUProfile() == small


def test_unknown_model_falls_back_to_the_default_with_a_warning(monkeypatch, caplog):
    monkeypatch.delenv("SHERPA_OCR_MODEL", raising=False)
    default = ocr_worker.PaddleCPUProfile()
    monkeypatch.setenv("SHERPA_OCR_MODEL", "huge")
    ocr_settings.model_set_for.cache_clear()
    with caplog.at_level("WARNING", logger="sherpa"):
        profile = ocr_worker.PaddleCPUProfile()
    assert profile == default and ocr_worker.profile_hash(profile) == ocr_worker.profile_hash(default)
    assert any("SHERPA_OCR_MODEL" in m and "huge" in m for m in caplog.messages)


def test_onednn_is_part_of_the_engine_profile(monkeypatch):
    monkeypatch.delenv("SHERPA_OCR_ONEDNN", raising=False)
    base = ocr_worker.PaddleCPUProfile()
    assert base.enable_mkldnn is False

    monkeypatch.setenv("SHERPA_OCR_ONEDNN", "1")
    on = ocr_worker.PaddleCPUProfile()
    assert on.enable_mkldnn is True and ocr_worker.profile_hash(on) != ocr_worker.profile_hash(base)

    for raw in ("abc", "2", "-1", ""):
        monkeypatch.setenv("SHERPA_OCR_ONEDNN", raw)
        assert ocr_worker.PaddleCPUProfile() == base


def test_engine_passes_onednn_and_thread_count_to_paddle_but_threads_stay_out_of_the_profile(monkeypatch, tmp_path):
    import sys
    import types

    captured: dict = {}

    class FakePaddleOCR:                      # 外部境界（paddleocr 本体）だけを差し替える
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=FakePaddleOCR))
    available = ocr_worker.OCRAvailability(
        available=True, unavailable_reason=None, model_hashes_valid=True, cache_home=str(tmp_path),
        paddleocr_version="3.7.0", paddlepaddle_version="3.3.0", pypdfium2_version="5.11.0", pillow_version="12.3.0",
        model_hashes={}, engine_profile_hash=ocr_worker.profile_hash(),
    )
    monkeypatch.setattr(ocr_worker, "paddle_availability", lambda cache_home=None: available)

    monkeypatch.delenv("SHERPA_OCR_THREADS", raising=False)
    ocr_worker.PaddleOCREngine(tmp_path)
    assert "cpu_threads" not in captured and captured["enable_mkldnn"] is False

    base = ocr_worker.profile_hash(ocr_worker.PaddleCPUProfile())
    monkeypatch.setenv("SHERPA_OCR_THREADS", "3")
    captured.clear()
    ocr_worker.PaddleOCREngine(tmp_path)
    assert captured["cpu_threads"] == 3
    assert ocr_worker.profile_hash(ocr_worker.PaddleCPUProfile()) == base


# ===== 入力パスの拒否理由（不在・資料フォルダの外・symlink を分ける） =====

@pytest.fixture
def path_root(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "real.txt").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "to-outside").symlink_to(outside)
    (root / "to-inside").symlink_to(root / "sub" / "real.txt")
    (root / "dangling").symlink_to(tmp_path / "nowhere")
    (root / "loop").symlink_to(root / "loop")
    return root


@pytest.mark.parametrize("resolver", [ocr_worker._safe_world_file, ocr_worker._safe_asset])
@pytest.mark.parametrize("relative, error, code", [
    pytest.param("sub/real.txt", None, None, id="regular-file-ok"),
    pytest.param("sub/missing.txt", ocr_worker.OCRInputNotFoundError, "input_not_found", id="missing"),
    pytest.param("missing-dir/x.txt", ocr_worker.OCRInputNotFoundError, "input_not_found", id="missing-parent"),
    pytest.param("sub/real.txt/child", ocr_worker.OCRInputNotFoundError, "input_not_found", id="parent-is-a-file"),
    pytest.param("to-outside", ocr_worker.OCRInputEscapedError, "input_path_escaped", id="symlink-pointing-outside"),
    pytest.param("to-inside", ocr_worker.OCRInputSymlinkError, "input_path_symlink", id="symlink-inside-root"),
    pytest.param("dangling", ocr_worker.OCRInputSymlinkError, "input_path_symlink", id="dangling-symlink"),
    pytest.param("loop", ocr_worker.OCRInputSymlinkError, "input_path_symlink", id="symlink-loop"),
])
def test_input_path_rejections_have_distinct_reasons(path_root, resolver, relative, error, code):
    if error is None:
        assert resolver(path_root, relative) == (path_root / relative).resolve()
        return
    with pytest.raises(error) as caught:
        resolver(path_root, relative)
    assert caught.value.error_code == code and isinstance(caught.value, ocr_worker.OCRBindingError)
    assert caught.value.retryable is False
    assert ("escaped" in str(caught.value)) is False   # 旧文言「escaped root」を不在や symlink に使わない


def test_asset_parent_traversal_is_reported_as_outside_the_root(path_root):
    with pytest.raises(ocr_worker.OCRInputEscapedError):
        ocr_worker._safe_asset(path_root, "../outside.txt")


def test_world_file_with_non_relative_path_keeps_its_own_reason(path_root):
    with pytest.raises(ocr_worker.OCRBindingError) as caught:
        ocr_worker._safe_world_file(path_root, "../outside.txt")
    assert type(caught.value) is ocr_worker.OCRBindingError and "relative" in str(caught.value)
