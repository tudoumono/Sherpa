"""固定Paddle profileを実modelで動かす任意のlive Gate。

通常のunit/contractではPaddleを本体環境へ導入しない。このtestは隔離worker用依存と、
hash固定済みmodel cacheを明示した環境だけで実行し、spawn子process・timeout境界・
原本read-onlyを実物で確認する。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import uuid

import pytest

from sherpa.ingest import (
    ai_observation,
    evidence_ir,
    evidence_render,
    observation_render,
    ocr_router,
    ocr_worker,
    raster_evidence,
)
from sherpa.store import ocr_jobs


ROOT = Path(__file__).resolve().parents[2]
STAMP = ROOT / "fixtures/eval/excel_ja/assets/廃止スタンプ.png"


def test_fixed_paddle_worker_reads_image_without_modifying_source() -> None:
    cache_value = os.environ.get("PADDLE_PDX_CACHE_HOME") or os.environ.get("SHERPA_OCR_MODEL_CACHE")
    if not cache_value:
        pytest.skip("固定Paddle model cacheが指定されていない")
    cache = Path(cache_value)
    availability = ocr_worker.paddle_availability(cache)
    if not availability.available:
        pytest.skip(f"固定Paddle workerを利用できない: {availability.unavailable_reason}")

    before = STAMP.stat()
    source_hash = hashlib.sha256(STAMP.read_bytes()).hexdigest()
    supervisor = ocr_worker.PaddleProcessSupervisor(cache)
    ticks = 0

    def tick() -> None:
        nonlocal ticks
        ticks += 1

    try:
        prediction = supervisor.predict_monitored(
            STAMP.read_bytes(),
            media_type="image/png",
            timeout_seconds=120,
            on_tick=tick,
            poll_seconds=0.25,
        )
    finally:
        supervisor.close()

    assert ticks > 0
    assert any(line.text == "廃止" for line in prediction.observations)
    assert all(0.0 <= line.confidence <= 1.0 and len(line.bbox) == 4 for line in prediction.observations)
    after = STAMP.stat()
    assert hashlib.sha256(STAMP.read_bytes()).hexdigest() == source_hash
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_router_queue_worker_and_separate_observation_generation_with_real_paddle(tmp_path: Path) -> None:
    cache_value = os.environ.get("PADDLE_PDX_CACHE_HOME") or os.environ.get("SHERPA_OCR_MODEL_CACHE")
    if not cache_value:
        pytest.skip("固定Paddle model cacheが指定されていない")
    cache = Path(cache_value)
    availability = ocr_worker.paddle_availability(cache)
    if not availability.available:
        pytest.skip(f"固定Paddle workerを利用できない: {availability.unavailable_reason}")

    world = "ocr-live-" + uuid.uuid4().hex[:12]
    generation_id = hashlib.sha256(b"ocr-live-canonical-generation").hexdigest()
    source_rel_path = "scan.png"
    source = tmp_path / "source" / source_rel_path
    source.parent.mkdir()
    source.write_bytes(STAMP.read_bytes())
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    observation_root = tmp_path / "observations"

    ir = raster_evidence.extract(source)
    evidence_ir.write_json_atomic(canonical / f"{source_rel_path}.evidence.json", ir)
    rendered = evidence_render.render(ir, source_name=source_rel_path)
    (canonical / f"{source_rel_path}.rag.md").write_text(rendered.markdown, encoding="utf-8")
    evidence_render.write_chunks_atomic(
        canonical / f"{source_rel_path}.rag_chunks.jsonl", rendered.chunks,
    )
    asset_root = canonical / f"{source_rel_path}.assets"
    raster_evidence.extract_assets(source, ir, asset_root)
    route = ocr_router.build_manifest(
        ir,
        source_rel_path=source_rel_path,
        assets=ocr_router.inventory_assets(asset_root),
    )
    ocr_router.write_json_atomic(canonical / f"{source_rel_path}.ocr_route.json", route)
    canonical_hashes = {
        path.relative_to(canonical).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in canonical.rglob("*") if path.is_file()
    }
    source_before = (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns)

    ocr_jobs.purge_world(world)
    supervisor = ocr_worker.PaddleProcessSupervisor(cache)
    try:
        refresh = ocr_jobs.enqueue_refresh_run(world, generation_id, supervisor.engine_profile_hash)
        assert refresh["status"] == "queued"
        expanded = ocr_worker.run_refresh_once(
            "ocr-live-worker",
            engine_profile_hash=supervisor.engine_profile_hash,
            canonical_is_current=lambda selected_world, selected_generation: (
                selected_world == world and selected_generation == generation_id
            ),
            resolve_generation_root=lambda selected_world, selected_generation: canonical,
        )
        assert expanded.status == "refresh_completed"
        assert expanded.jobs_enqueued == 1

        def load_ir(_job):
            return evidence_ir.from_json_str(
                (canonical / f"{source_rel_path}.evidence.json").read_text(encoding="utf-8")
            )

        publisher = ocr_worker.build_standard_publish_callback(
            resolve_derived_root=lambda _world: observation_root,
            canonical_is_current=lambda selected_world, selected_generation: (
                selected_world == world and selected_generation == generation_id
            ),
            load_ir=load_ir,
        )
        result = ocr_worker.run_once(
            "ocr-live-worker",
            engine=supervisor,
            canonical_is_current=lambda selected_world, selected_generation: (
                selected_world == world and selected_generation == generation_id
            ),
            load_ir=load_ir,
            resolve_source=lambda _job: source,
            resolve_asset_root=lambda _job: asset_root,
            publish_observation=publisher,
            inference_timeout_seconds=120,
        )
        assert result.status == "succeeded"

        active = observation_render.active_observation_dir(
            observation_root,
            canonical_generation_id=generation_id,
        )
        assert active is not None
        # O1（2026-09-03）以降、公開する成果物は検索用Markdown/chunkではなく、office_md.py が
        # rag.md への合流時に読む Observation Set 本体（`.ai_observations.jsonl`）だけになった。
        jsonl_path = active / f"{source_rel_path}.ai_observations.jsonl"
        sets = [
            ai_observation.from_json_str(line, ir=ir)
            for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        observations = [item for observation_set in sets for item in observation_set.observations]
        assert observations and any("廃止" in item.text for item in observations)
        # O1: use_for_answer は行の実測confidenceを既存の使用可否ルール（VLMも従う同じ閾値
        # ai_observation.MIN_ANSWER_CONFIDENCE）と比べて決める——実機の値は毎回変わりうるため
        # 固定値ではなく、記録済みconfidenceとの整合だけを固定する。
        assert all(
            item.searchable is True
            and item.use_for_answer == (item.confidence >= ai_observation.MIN_ANSWER_CONFIDENCE)
            for item in observations
        )
    finally:
        supervisor.close()
        ocr_jobs.purge_world(world)

    after_hashes = {
        path.relative_to(canonical).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in canonical.rglob("*") if path.is_file()
    }
    assert after_hashes == canonical_hashes
    assert (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns) == source_before
