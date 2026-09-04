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
    """model本体が同じなら、再download由来のメタデータ差でhashを変えない（2026-08-16実測の是正）。

    downloaderは `.cache/huggingface/` へ取得時刻・etag・lockを書くため、同一modelを取り直すだけで
    tree hashが変わり `model_hash_mismatch` で起動できなくなっていた（精度は一致していたのに拒否された）。
    """
    model = tmp_path / "PP-OCRv6_medium_det"
    (model / ".cache" / "huggingface" / "download").mkdir(parents=True)
    (model / "inference.pdiparams").write_bytes(b"weights")
    (model / "inference.yml").write_text("pinned", encoding="utf-8")
    baseline = ocr_worker._tree_digest(model)

    # 取得メタデータだけが変わった状態（＝2回目のdownload）
    (model / ".cache" / "huggingface" / "download" / "inference.pdiparams.metadata").write_text(
        "etag-and-timestamp", encoding="utf-8")
    assert ocr_worker._tree_digest(model) == baseline

    # model本体が変われば必ず変わる（除外が検知を緩めていないこと）
    (model / "inference.pdiparams").write_bytes(b"weights-v2")
    assert ocr_worker._tree_digest(model) != baseline


def test_pinned_model_hashes_match_the_distributed_lock_file():
    """profileのpinとオフライン配布用lockが同じmodelを指す（片方だけ更新して食い違わせない）。"""
    lock = json.loads((ROOT / "docker/ocr-models.lock.json").read_text(encoding="utf-8"))
    locked = {item["name"]: item["tree_sha256"] for item in lock["models"]}
    assert locked[ocr_worker.PADDLE_CPU_PROFILE.detection_model] == \
        ocr_worker.PADDLE_CPU_PROFILE.detection_model_tree_sha256
    assert locked[ocr_worker.PADDLE_CPU_PROFILE.recognition_model] == \
        ocr_worker.PADDLE_CPU_PROFILE.recognition_model_tree_sha256
    assert lock["runtime_download_allowed"] is False
    assert ".cache" in lock["tree_hash_excludes"]


def test_asset_preparation_rehashes_source_and_asset_and_builds_ocr_only_set(tmp_path):
    ir, asset_root, decision, job = _picture_route(tmp_path)
    prepared = ocr_worker.prepare_input(
        job, decision, source_path=SOURCE, asset_root=asset_root,
    )
    engine = FakeEngine()
    prediction = engine.predict(prepared.image_bytes, media_type=prepared.media_type)
    result = ocr_worker.build_observation_set(
        ir=ir, decision=decision, prepared=prepared, prediction=prediction,
        canonical_generation_id=GENERATION_ID, engine=engine,
    )

    assert ai_observation.validation_errors(result, ir=ir) == []
    assert result.canonical_generation_id == GENERATION_ID
    assert result.observations[0].kind == "ocr_text"
    assert result.observations[0].text == "  A_01  "
    assert result.observations[0].searchable is True
    # O1: use_for_answer は行の実測confidence（0.92）を既存の使用可否ルール
    # （ai_observation.MIN_ANSWER_CONFIDENCE=0.70）と比べて決める。VLM と同じ既存ルールを
    # 適用しているだけ＝OCR専用の新しい閾値は無い。
    assert result.observations[0].confidence == 0.92
    assert result.observations[0].use_for_answer is True


def test_low_confidence_ocr_line_is_not_answer_eligible(tmp_path):
    """O1: confidence が既存の使用可否ルール未満の行は use_for_answer=False のまま
    （rag.md の「AI観測」レコードには出ない＝検索可のままだが回答材料にはしない）。"""
    ir, asset_root, decision, job = _picture_route(tmp_path)
    prepared = ocr_worker.prepare_input(
        job, decision, source_path=SOURCE, asset_root=asset_root,
    )

    class LowConfidenceEngine(FakeEngine):
        def predict(self, image_bytes, *, media_type):
            self.calls += 1
            return ocr_worker.OCRPrediction([
                ocr_worker.EngineLine(text="判読不可気味", confidence=0.5, bbox=[1, 2, 30, 40], line_id="0"),
            ])

    engine = LowConfidenceEngine()
    prediction = engine.predict(prepared.image_bytes, media_type=prepared.media_type)
    result = ocr_worker.build_observation_set(
        ir=ir, decision=decision, prepared=prepared, prediction=prediction,
        canonical_generation_id=GENERATION_ID, engine=engine,
    )
    assert ai_observation.validation_errors(result, ir=ir) == []
    assert result.observations[0].confidence == 0.5
    assert result.observations[0].searchable is True
    assert result.observations[0].use_for_answer is False


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
        prepared = ocr_worker.prepare_input(
            job, decision, source_path=PDF_SOURCE, asset_root=tmp_path,
        )
        repeated = ocr_worker.prepare_input(
            job, decision, source_path=PDF_SOURCE, asset_root=tmp_path,
        )
    finally:
        ocr_worker._clear_pdf_document_cache()
    assert prepared.image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert prepared.asset_sha256 == "sha256:" + hashlib.sha256(prepared.image_bytes).hexdigest()
    assert prepared.input_kind == "page_render"
    assert all(value > 0 for value in prepared.pixel_size)
    assert repeated.image_bytes == prepared.image_bytes
    assert len(opens) == 1


def test_worker_uses_cache_contract_and_completes_without_changing_canonical(monkeypatch, tmp_path):
    ir, asset_root, decision, job = _picture_route(tmp_path)
    engine = FakeEngine()
    completed = {}
    prediction = ocr_worker.OCRPrediction([
        ocr_worker.EngineLine(text="CACHE_01", confidence=0.88, bbox=[1, 1, 20, 20], line_id="cached"),
    ])

    monkeypatch.setattr(ocr_worker.ocr_jobs, "lease_next", lambda worker_id, lease_seconds: job)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "renew_lease", lambda *args, **kwargs: True)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "get_cached_result", lambda *args: {
        "result_payload": prediction.to_payload(),
    })
    monkeypatch.setattr(ocr_worker.ocr_jobs, "put_cached_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "complete_job", lambda job_id, token, **kwargs: completed.update(kwargs) or job)

    result = ocr_worker.run_once(
        "worker-1", engine=engine, canonical_is_current=lambda world, generation: generation == GENERATION_ID,
        load_ir=lambda leased: ir, resolve_source=lambda leased: SOURCE,
        resolve_asset_root=lambda leased: asset_root,
    )

    assert result.status == "succeeded"
    assert result.cache_hit is True
    assert engine.calls == 0
    assert completed["result_payload"]["canonical_generation_id"] == GENERATION_ID
    assert completed["result_payload"]["observations"][0]["text"] == "CACHE_01"


def test_worker_marks_job_stale_before_reading_source(monkeypatch):
    job = {"id": 9, "world": "world-a", "canonical_generation_id": GENERATION_ID, "lease_token": "token"}
    calls = []
    monkeypatch.setattr(ocr_worker.ocr_jobs, "lease_next", lambda worker_id, lease_seconds: job)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "renew_lease", lambda *args, **kwargs: True)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "mark_stale", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = ocr_worker.run_once(
        "worker-1", engine=FakeEngine(), canonical_is_current=lambda world, generation: False,
        load_ir=lambda leased: (_ for _ in ()).throw(AssertionError("must not load")),
        resolve_source=lambda leased: Path("unreachable"), resolve_asset_root=lambda leased: Path("unreachable"),
    )
    assert result.status == "stale"
    assert calls and calls[0][0] == (9, "token")


def test_fake_engine_protocol_still_supports_successful_non_cached_unit_run(monkeypatch, tmp_path):
    ir, asset_root, _decision, job = _picture_route(tmp_path)
    engine = FakeEngine()
    commits = []
    monkeypatch.setattr(ocr_worker.ocr_jobs, "lease_next", lambda worker_id, lease_seconds: job)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "renew_lease", lambda *args, **kwargs: True)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "get_cached_result", lambda *args: None)
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "put_cached_result_for_lease",
        lambda *args, **kwargs: {"result_payload": args[-1]},
    )
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "complete_job",
        lambda job_id, token, **kwargs: commits.append(kwargs) or {**job, "status": "succeeded"},
    )

    result = ocr_worker.run_once(
        "worker-1", engine=engine, canonical_is_current=lambda world, generation: True,
        load_ir=lambda leased: ir, resolve_source=lambda leased: SOURCE,
        resolve_asset_root=lambda leased: asset_root,
    )

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
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in canonical.iterdir() if path.is_file()
    }

    class FailingEngine(FakeEngine):
        def predict(self, image_bytes: bytes, *, media_type: str) -> ocr_worker.OCRPrediction:
            raise RuntimeError("synthetic OCR failure")

    monkeypatch.setattr(ocr_worker.ocr_jobs, "lease_next", lambda worker_id, lease_seconds: job)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "renew_lease", lambda *args, **kwargs: True)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "get_cached_result", lambda *args: None)
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "fail_job",
        lambda *args, **kwargs: {**job, "status": "queued"},
    )

    result = ocr_worker.run_once(
        "worker-1", engine=FailingEngine(), canonical_is_current=lambda world, generation: True,
        load_ir=lambda leased: ir, resolve_source=lambda leased: SOURCE,
        resolve_asset_root=lambda leased: asset_root,
    )

    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in canonical.iterdir() if path.is_file()
    }
    assert result.status == "failed" and result.error_code == "engine_failure"
    assert after == before


def test_standard_publisher_runs_post_publish_before_marking_jobs(monkeypatch, tmp_path):
    events = []
    snapshot = {"row_count": 0, "min_id": None, "max_id": None, "id_sum": 0}
    monkeypatch.setattr(ocr_worker.ocr_jobs, "succeeded_results_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "iter_succeeded_results", lambda *args: iter(()))
    monkeypatch.setattr(
        ocr_worker.observation_render,
        "publish_snapshot_stream",
        lambda *args, **kwargs: {"status": "published"},
    )
    monkeypatch.setattr(
        ocr_worker.ocr_jobs,
        "mark_snapshot_artifacts_published",
        lambda world, generation, selected: events.append(("marked", world, generation, selected)),
    )
    monkeypatch.setattr(ocr_worker, "world_lock", lambda world: nullcontext())
    publisher = ocr_worker.build_standard_publish_callback(
        resolve_derived_root=lambda world: tmp_path,
        canonical_is_current=lambda world, generation: True,
        load_ir=lambda row: (_ for _ in ()).throw(AssertionError("no rows")),
        on_published=lambda world, generation: events.append(("reindexed", world, generation)),
    )

    publisher({"world": "world-a", "canonical_generation_id": GENERATION_ID}, None)

    assert events == [
        ("reindexed", "world-a", GENERATION_ID),
        ("marked", "world-a", GENERATION_ID, snapshot),
    ]


def test_standard_publisher_keeps_jobs_unpublished_when_post_publish_fails(monkeypatch, tmp_path):
    marked = []
    snapshot = {"row_count": 0, "min_id": None, "max_id": None, "id_sum": 0}
    monkeypatch.setattr(ocr_worker.ocr_jobs, "succeeded_results_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "iter_succeeded_results", lambda *args: iter(()))
    monkeypatch.setattr(
        ocr_worker.observation_render,
        "publish_snapshot_stream",
        lambda *args, **kwargs: {"status": "published"},
    )
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "mark_snapshot_artifacts_published",
        lambda *args: marked.append(args),
    )
    monkeypatch.setattr(ocr_worker, "world_lock", lambda world: nullcontext())
    publisher = ocr_worker.build_standard_publish_callback(
        resolve_derived_root=lambda world: tmp_path,
        canonical_is_current=lambda world, generation: True,
        load_ir=lambda row: (_ for _ in ()).throw(AssertionError("no rows")),
        on_published=lambda world, generation: (_ for _ in ()).throw(RuntimeError("index unavailable")),
    )

    try:
        publisher({"world": "world-a", "canonical_generation_id": GENERATION_ID}, None)
    except RuntimeError as exc:
        assert str(exc) == "index unavailable"
    else:
        raise AssertionError("post-publish failure must propagate")

    assert marked == []


def test_standard_publisher_rechecks_generation_and_reindexes_inside_world_lock(monkeypatch, tmp_path):
    events = []
    lock_depth = 0
    snapshot = {"row_count": 0, "min_id": None, "max_id": None, "id_sum": 0}
    monkeypatch.setattr(ocr_worker.ocr_jobs, "succeeded_results_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "iter_succeeded_results", lambda *args: iter(()))
    monkeypatch.setattr(
        ocr_worker.observation_render, "publish_snapshot_stream",
        lambda *args, **kwargs: {"status": "published"},
    )

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

    def current(world, generation):
        assert lock_depth == 1
        events.append(("current", world, generation))
        return True

    def reindex(world, generation):
        assert lock_depth == 1
        events.append(("reindex", world, generation))

    def collect(root, *, active_canonical_generation_id):
        assert lock_depth == 1
        events.append(("gc", root, active_canonical_generation_id))

    def mark(world, generation, selected):
        assert lock_depth == 1
        events.append(("mark", world, generation, selected))

    monkeypatch.setattr(ocr_worker, "world_lock", locked)
    monkeypatch.setattr(ocr_worker, "garbage_collect_observation_generations", collect)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "mark_snapshot_artifacts_published", mark)
    publisher = ocr_worker.build_standard_publish_callback(
        resolve_derived_root=lambda _world: tmp_path,
        canonical_is_current=current,
        load_ir=lambda row: (_ for _ in ()).throw(AssertionError("no rows")),
        on_published=reindex,
    )

    publisher({"world": "world-a", "canonical_generation_id": GENERATION_ID}, None)

    assert [event[0] for event in events] == ["lock_enter", "current", "reindex", "gc", "mark", "lock_exit"]


def test_standard_publisher_stale_after_pointer_publish_never_reindexes_or_marks(monkeypatch, tmp_path):
    events = []
    snapshot = {"row_count": 0, "min_id": None, "max_id": None, "id_sum": 0}
    monkeypatch.setattr(ocr_worker.ocr_jobs, "succeeded_results_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "iter_succeeded_results", lambda *args: iter(()))
    monkeypatch.setattr(
        ocr_worker.observation_render, "publish_snapshot_stream",
        lambda *args, **kwargs: {"status": "published"},
    )
    monkeypatch.setattr(ocr_worker, "world_lock", lambda world: nullcontext())
    monkeypatch.setattr(
        ocr_worker, "garbage_collect_observation_generations",
        lambda *args, **kwargs: events.append("gc"),
    )
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "mark_snapshot_artifacts_published",
        lambda *args, **kwargs: events.append("mark"),
    )
    publisher = ocr_worker.build_standard_publish_callback(
        resolve_derived_root=lambda _world: tmp_path,
        canonical_is_current=lambda world, generation: False,
        load_ir=lambda row: (_ for _ in ()).throw(AssertionError("no rows")),
        on_published=lambda world, generation: events.append("reindex"),
    )

    publisher({"world": "world-a", "canonical_generation_id": GENERATION_ID}, None)

    assert events == []


def test_runtime_reindex_defensively_rechecks_generation_before_es_delete(monkeypatch, tmp_path):
    from sherpa import es_index, worlds
    from sherpa.ingest import derived_generation

    active_generations = iter([GENERATION_ID, "d" * 64])
    indexed = []
    marked = []
    snapshot = {"row_count": 0, "min_id": None, "max_id": None, "id_sum": 0}
    monkeypatch.setattr(derived_generation, "active_generation_id", lambda _root: next(active_generations))
    monkeypatch.setattr(worlds, "derived_dir", lambda _world: tmp_path / "canonical")
    monkeypatch.setattr(worlds, "observation_dir", lambda _world, **_kwargs: tmp_path / "observations")
    monkeypatch.setattr(worlds, "validate_ocr_registered_sources", lambda: tmp_path)
    monkeypatch.setattr(worlds, "validate_ocr_source_root", lambda root, **_kwargs: Path(root))
    monkeypatch.setattr(ocr_worker, "world_lock", lambda world: nullcontext())
    monkeypatch.setattr(ocr_worker.ocr_jobs, "succeeded_results_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "iter_succeeded_results", lambda *args: iter(()))
    monkeypatch.setattr(
        ocr_worker.observation_render, "publish_snapshot_stream",
        lambda *args, **kwargs: {"status": "published"},
    )
    monkeypatch.setattr(es_index, "index_world", lambda *args, **kwargs: indexed.append((args, kwargs)))
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "mark_snapshot_artifacts_published",
        lambda *args, **kwargs: marked.append((args, kwargs)),
    )
    publisher = ocr_worker._runtime_callbacks()[-1]

    with pytest.raises(ocr_worker.OCRBindingError, match="changed before observation reindex"):
        publisher({"world": "world-a", "canonical_generation_id": GENERATION_ID}, None)

    assert indexed == []
    assert marked == []


def test_runtime_reindex_observations_never_touches_es_or_human_md_marker(monkeypatch, tmp_path):
    """`reindex_observations`（OCR観測の公開直後フック）は ES・`.human_md_es_sig` マーカーへ
    一切触れない（裁定: ocr_worker は隔離 profile で `/derived` read-only・ES 到達不可のため、
    ここで触れると通常の OCR 公開のたびに必ず失敗する）。ES（legacy チャンク/kNN）は観測
    チャンクを読まないため、OCR 公開後の ES 反映自体が不要（grep 経路のみで足りる）。"""
    from sherpa import es_index, worlds
    from sherpa.ingest import derived_generation, office_md, worker as ingest_worker

    monkeypatch.setattr(derived_generation, "active_generation_id", lambda _root: GENERATION_ID)
    monkeypatch.setattr(worlds, "derived_dir", lambda _world: tmp_path / "canonical")
    monkeypatch.setattr(worlds, "observation_dir", lambda _world, **_kwargs: tmp_path / "observations")
    monkeypatch.setattr(worlds, "validate_ocr_registered_sources", lambda: tmp_path)
    monkeypatch.setattr(worlds, "validate_ocr_source_root", lambda root, **_kwargs: Path(root))
    monkeypatch.setattr(ocr_worker, "world_lock", lambda world: nullcontext())
    snapshot = {"row_count": 0, "min_id": None, "max_id": None, "id_sum": 0}
    monkeypatch.setattr(ocr_worker.ocr_jobs, "succeeded_results_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "iter_succeeded_results", lambda *args: iter(()))
    monkeypatch.setattr(
        ocr_worker.observation_render, "publish_snapshot_stream",
        lambda *args, **kwargs: {"status": "published"},
    )
    monkeypatch.setattr(ocr_worker.ocr_jobs, "mark_snapshot_artifacts_published", lambda *args, **kwargs: None)

    def _must_not_call(name):
        def _boom(*a, **kw):
            raise AssertionError(f"reindex_observations は {name} を呼んではいけない（隔離 profile では必ず失敗する）")
        return _boom
    monkeypatch.setattr(ingest_worker, "index_world_with_human_md_holdback", _must_not_call("index_world_with_human_md_holdback"))
    monkeypatch.setattr(es_index, "index_world", _must_not_call("es_index.index_world"))
    monkeypatch.setattr(office_md, "drop_human_md_es_sig_marker", _must_not_call("drop_human_md_es_sig_marker"))
    monkeypatch.setattr(office_md, "confirm_human_md_es_sig", _must_not_call("confirm_human_md_es_sig"))

    publisher = ocr_worker._runtime_callbacks()[-1]
    publisher({"world": "world-a", "canonical_generation_id": GENERATION_ID}, None)  # 例外が飛べば失敗


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


def test_lease_loss_during_monitored_inference_never_commits_cache_or_job(monkeypatch, tmp_path):
    ir, asset_root, _decision, job = _picture_route(tmp_path)
    renewals = iter([True, True, False])
    writes = []

    class MonitoredFake(FakeEngine):
        def predict_monitored(self, image_bytes, *, media_type, timeout_seconds, on_tick):
            time.sleep(0.01)
            on_tick()
            raise AssertionError("lease loss must interrupt before a prediction is returned")

    monkeypatch.setattr(ocr_worker.ocr_jobs, "lease_next", lambda worker_id, lease_seconds: job)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "renew_lease", lambda *args, **kwargs: next(renewals))
    monkeypatch.setattr(ocr_worker.ocr_jobs, "get_cached_result", lambda *args: None)
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "put_cached_result_for_lease", lambda *args, **kwargs: writes.append("cache"),
    )
    monkeypatch.setattr(ocr_worker.ocr_jobs, "complete_job", lambda *args, **kwargs: writes.append("complete"))
    monkeypatch.setattr(ocr_worker.ocr_jobs, "fail_job", lambda *args, **kwargs: writes.append("failed"))

    result = ocr_worker.run_once(
        "worker-1", engine=MonitoredFake(), canonical_is_current=lambda world, generation: True,
        load_ir=lambda leased: ir, resolve_source=lambda leased: SOURCE,
        resolve_asset_root=lambda leased: asset_root, lease_renew_interval_seconds=0.001,
    )

    assert result.status == "lease_lost"
    assert writes == []


def test_snapshot_callback_runs_only_when_generation_becomes_terminal(monkeypatch):
    readiness = iter([False, True])
    published = []
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "generation_ready_for_publication", lambda *args: next(readiness),
    )

    def callback(job, observation):
        published.append((job["id"], observation))

    first = {"id": 1, "world": "w", "canonical_generation_id": GENERATION_ID}
    last = {"id": 2, "world": "w", "canonical_generation_id": GENERATION_ID}

    assert ocr_worker._publish_terminal_generation(first, callback, None) is False
    assert ocr_worker._publish_terminal_generation(last, callback, None) is True
    assert published == [(2, None)]


def test_refresh_worker_streams_manifests_and_persists_cursor(monkeypatch, tmp_path):
    ir, asset_root, _decision, job = _picture_route(tmp_path)
    del asset_root
    generation_root = tmp_path / "canonical"
    route_path = generation_root / f"{job['source_rel_path']}.ocr_route.json"
    evidence_path = generation_root / f"{job['source_rel_path']}.evidence.json"
    route_path.parent.mkdir(parents=True)
    source_manifest = ocr_router.build_manifest(ir, source_rel_path=job["source_rel_path"], assets=[])
    route_path.write_text(ocr_router.to_json_str(source_manifest), encoding="utf-8")
    evidence_path.write_text(evidence_ir.to_json_str(ir), encoding="utf-8")
    refresh = {
        "id": 7, "world": "world-a", "canonical_generation_id": GENERATION_ID,
        "engine_profile_hash": ocr_worker.profile_hash(), "lease_token": "refresh-token",
        "cursor_rel_path": None,
    }
    progress = []
    monkeypatch.setattr(ocr_worker.ocr_jobs, "lease_refresh_run", lambda *args, **kwargs: refresh)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "renew_refresh_run", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "enqueue_manifest_jobs", lambda *args, **kwargs: [{"id": 1}],
    )
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "update_refresh_run_progress",
        lambda *args, **kwargs: progress.append(kwargs) or True,
    )
    monkeypatch.setattr(ocr_worker.ocr_jobs, "complete_refresh_run", lambda *args, **kwargs: refresh)

    result = ocr_worker.run_refresh_once(
        "worker-1", engine_profile_hash=ocr_worker.profile_hash(),
        canonical_is_current=lambda world, generation: True,
        resolve_generation_root=lambda world, generation: generation_root,
    )

    assert result.status == "refresh_completed"
    assert result.manifests_processed == 1 and result.jobs_enqueued == 1
    assert progress[0]["cursor_rel_path"].endswith(".ocr_route.json")


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

    result = ocr_worker.garbage_collect_observation_generations(
        tmp_path, active_canonical_generation_id=active,
    )

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
    monkeypatch.setattr(
        ocr_worker.ocr_jobs, "record_worker_heartbeat",
        lambda *args, **kwargs: heartbeat_statuses.append(kwargs["status"]) or {},
    )

    assert ocr_worker.main(["--worker-id", "test-worker", "--poll-seconds", "0.01"]) == 0
    assert heartbeat_statuses[-1] == "stopping"
