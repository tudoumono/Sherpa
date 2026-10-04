from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
import signal
import time

import pytest

from sherpa.ingest import ai_observation, evidence_ir, evidence_spike, ocr_router, ocr_worker


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
    monkeypatch.setattr(ocr_worker.ocr_jobs, "lease_next", lambda worker_id, lease_seconds: job)
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
    assert locked[ocr_worker.PADDLE_CPU_PROFILE.detection_model] == \
        ocr_worker.PADDLE_CPU_PROFILE.detection_model_tree_sha256
    assert locked[ocr_worker.PADDLE_CPU_PROFILE.recognition_model] == \
        ocr_worker.PADDLE_CPU_PROFILE.recognition_model_tree_sha256
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
    monkeypatch.setattr(ocr_worker.ocr_jobs, "succeeded_results_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "iter_succeeded_results", lambda *args: iter(rows))
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
                        lambda world, generation, selected: events.append(("marked", world, generation, selected)))
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
    monkeypatch.setattr(ocr_worker.ocr_jobs, "mark_snapshot_artifacts_published", lambda *a: None)
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
    monkeypatch.setattr(ocr_worker.ocr_jobs, "generation_ready_for_publication", lambda *args: next(readiness))

    def callback(job, observation):
        published.append((job["id"], observation))

    first = {"id": 1, "world": "w", "canonical_generation_id": GENERATION_ID}
    last = {"id": 2, "world": "w", "canonical_generation_id": GENERATION_ID}

    assert ocr_worker._publish_terminal_generation(first, callback, None) is False
    assert ocr_worker._publish_terminal_generation(last, callback, None) is True
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

    assert ocr_worker.main(["--worker-id", "test-worker", "--poll-seconds", "0.01"]) == 0
    assert heartbeat_statuses[-1] == "stopping"
