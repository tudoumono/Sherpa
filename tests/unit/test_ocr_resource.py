"""OCR の資源まわり: 設定が PaddleOCR と profile hash に入る・入力の上限・強制終了の後始末。
設計: docs/design/rag.md「OCR（非同期・隔離ワーカー）」
"""
from __future__ import annotations

import dataclasses
import sys
import types
from dataclasses import asdict
from pathlib import Path

import pytest

from sherpa.ingest import ocr_router, ocr_workdir, ocr_worker
from sherpa.ingest import evidence_spike
from tests.unit.test_ocr_worker import PDF_SOURCE, SOURCE, FakeEngine, _patch_lease, _picture_route, _run_once

_ENV_NAMES = (
    "SHERPA_OCR_DET_LIMIT_TYPE", "SHERPA_OCR_DET_LIMIT_SIDE_LEN", "SHERPA_OCR_REC_BATCH_SIZE",
    "SHERPA_OCR_THREADS", "SHERPA_OCR_MAX_PIXELS", "SHERPA_OCR_MAX_INPUT_BYTES",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda **kw: {"ocr_enabled": True})


def _set_limit(monkeypatch, name, value):
    """入力の上限は profile の一部。環境変数を変えたら profile も作り直す。"""
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(ocr_worker, "PADDLE_CPU_PROFILE", ocr_worker.PaddleCPUProfile())


# ===== 検出・推論の資源 =====

def test_profile_defaults_are_small_resource_values():
    profile = ocr_worker.PaddleCPUProfile()
    assert (profile.text_det_limit_type, profile.text_det_limit_side_len,
            profile.text_recognition_batch_size) == ("max", 1280, 1)
    assert profile.enable_mkldnn is False
    assert profile.detection_model == "PP-OCRv6_small_det"


@pytest.mark.parametrize("name, value", [
    ("SHERPA_OCR_DET_LIMIT_TYPE", "min"),
    ("SHERPA_OCR_DET_LIMIT_SIDE_LEN", "1600"),
    ("SHERPA_OCR_REC_BATCH_SIZE", "6"),
    ("SHERPA_OCR_MAX_PIXELS", "2000000"),
    ("SHERPA_OCR_MAX_INPUT_BYTES", "2097152"),
])
def test_each_resource_setting_changes_the_profile_hash(monkeypatch, name, value):
    base = ocr_worker.profile_hash(ocr_worker.PaddleCPUProfile())
    monkeypatch.setenv(name, value)
    changed = ocr_worker.profile_hash(ocr_worker.PaddleCPUProfile())
    assert changed != base


def test_invalid_resource_settings_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("SHERPA_OCR_DET_LIMIT_TYPE", "huge")
    monkeypatch.setenv("SHERPA_OCR_DET_LIMIT_SIDE_LEN", "-5")
    monkeypatch.setenv("SHERPA_OCR_REC_BATCH_SIZE", "abc")
    profile = ocr_worker.PaddleCPUProfile()
    assert (profile.text_det_limit_type, profile.text_det_limit_side_len,
            profile.text_recognition_batch_size) == ("max", 1280, 1)


def test_paddleocr_constructor_receives_the_profile_resources(monkeypatch, tmp_path):
    monkeypatch.setenv("SHERPA_OCR_DET_LIMIT_SIDE_LEN", "1600")
    monkeypatch.setenv("SHERPA_OCR_REC_BATCH_SIZE", "2")
    monkeypatch.setenv("SHERPA_OCR_THREADS", "3")
    monkeypatch.setattr(ocr_worker, "PADDLE_CPU_PROFILE", ocr_worker.PaddleCPUProfile())
    captured: dict = {}

    class _FakePaddleOCR:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=_FakePaddleOCR))
    monkeypatch.setattr(ocr_worker, "paddle_availability", lambda cache_home=None: ocr_worker.OCRAvailability(
        available=True, unavailable_reason=None, model_hashes_valid=True, cache_home=str(tmp_path),
        paddleocr_version="3.7.0", paddlepaddle_version="3.3.0", pypdfium2_version="5.11.0",
        pillow_version="12.3.0", model_hashes={}, engine_profile_hash=ocr_worker.profile_hash()))

    ocr_worker.PaddleOCREngine(tmp_path)

    assert captured["text_det_limit_type"] == "max"
    assert captured["text_det_limit_side_len"] == 1600
    assert captured["text_recognition_batch_size"] == 2
    assert captured["cpu_threads"] == 3
    assert captured["enable_mkldnn"] is False
    assert "text_det_max_side_limit" not in captured


def test_poc_engine_uses_the_same_profile_and_reports_its_hash(monkeypatch, tmp_path):
    from sherpa.eval import ocr_engine_poc

    captured: dict = {}

    class _FakePaddleOCR:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=_FakePaddleOCR))
    monkeypatch.setitem(sys.modules, "paddle", types.SimpleNamespace(__version__="3.3.0"))
    monkeypatch.setattr(ocr_engine_poc.importlib.metadata, "version", lambda name: "3.7.0")
    models = tmp_path / "official_models"
    for name in ("det", "rec"):
        (models / name).mkdir(parents=True)
        (models / name / "m.bin").write_bytes(b"x")
    profile = dataclasses.replace(ocr_worker.PADDLE_CPU_PROFILE, text_det_limit_side_len=1600)

    engine = ocr_engine_poc.PaddleEngine(tmp_path, detection_model="det", recognition_model="rec", cpu_threads=4, profile=profile)

    assert captured["cpu_threads"] == 4 and captured["text_det_limit_side_len"] == 1600
    assert engine.metadata["engine_profile_hash"] == ocr_worker.profile_hash(profile)
    assert engine.metadata["engine_profile_hash"] != ocr_worker.profile_hash()


# ===== 入力の上限（描画・読込の前） =====

def test_oversized_page_is_refused_before_render(monkeypatch, tmp_path):
    _set_limit(monkeypatch, "SHERPA_OCR_MAX_PIXELS", "1000000")
    rendered: list[float] = []

    class _Page:
        def get_size(self):
            return (595.0, 842.0)

        def render(self, scale):
            rendered.append(scale)
            raise AssertionError("render must not run")

        def close(self):
            pass

    class _Doc:
        def __len__(self):
            return 1

        def __getitem__(self, _index):
            return _Page()

        def close(self):
            pass

    ir = evidence_spike.extract(PDF_SOURCE)
    page = next(item for item in ir.elements if item.type == "page")
    decision = ocr_router.OCRRouteDecision(
        route_input_id="render-1", target_evidence_id=page.element_id, input_kind="page_render",
        status="selected", reason_code="scan_page_render_fallback", priority=90, media_type="image/png",
        page_render={**ocr_router.PAGE_RENDER_PROFILE, "page_1_based": 1},
    )
    ocr_worker._clear_pdf_document_cache()
    monkeypatch.setattr(ocr_worker, "_open_pdf_document", lambda path: _Doc())
    try:
        with pytest.raises(ocr_worker.OCRInputTooLargeError, match="SHERPA_OCR_MAX_PIXELS"):
            ocr_worker.prepare_input({"source_content_hash": ir.source.content_hash}, decision,
                                     source_path=PDF_SOURCE, asset_root=tmp_path)
    finally:
        ocr_worker._clear_pdf_document_cache()
    assert rendered == []


def test_oversized_asset_is_refused_before_reading(monkeypatch, tmp_path):
    ir, asset_root, decision, job = _picture_route(tmp_path)
    _set_limit(monkeypatch, "SHERPA_OCR_MAX_INPUT_BYTES", str(1024 * 1024))
    (asset_root / decision.asset_rel_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (1024 * 1024 + 1))
    monkeypatch.setattr(ocr_worker, "_read_bytes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read")))
    with pytest.raises(ocr_worker.OCRInputTooLargeError, match="SHERPA_OCR_MAX_INPUT_BYTES"):
        ocr_worker.prepare_input(job, decision, source_path=SOURCE, asset_root=asset_root)


def test_asset_with_too_many_pixels_is_refused_before_reading(monkeypatch, tmp_path):
    ir, asset_root, decision, job = _picture_route(tmp_path)
    _set_limit(monkeypatch, "SHERPA_OCR_MAX_PIXELS", "1000000")
    decision = ocr_router.OCRRouteDecision(**{**asdict(decision), "pixel_size": [2000, 2000]})
    monkeypatch.setattr(ocr_worker, "_read_bytes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read")))
    with pytest.raises(ocr_worker.OCRInputTooLargeError, match="SHERPA_OCR_MAX_PIXELS"):
        ocr_worker.prepare_input(job, decision, source_path=SOURCE, asset_root=asset_root)


def test_oversized_input_fails_with_a_reason_and_is_not_retried(monkeypatch, tmp_path):
    ir, asset_root, decision, job = _picture_route(tmp_path)
    _set_limit(monkeypatch, "SHERPA_OCR_MAX_PIXELS", "1000000")
    job = {**job, "route_input": asdict(ocr_router.OCRRouteDecision(**{**asdict(decision), "pixel_size": [2000, 2000]}))}
    failures: list[dict] = []
    _patch_lease(monkeypatch, job, cached=None)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "fail_job",
                        lambda *args, **kwargs: failures.append(kwargs) or {**job, "status": "failed"})
    engine = FakeEngine()

    result = _run_once(ir, asset_root, engine)

    assert result.status == "failed" and result.error_code == "input_too_large"
    assert failures and failures[0]["retryable"] is False and failures[0]["error_code"] == "input_too_large"
    assert "SHERPA_OCR_MAX_PIXELS" in failures[0]["error_detail"]
    assert engine.calls == 0



# ===== 子の強制終了・入力ファイルの後始末・再試行の分類 =====

def _killed_child(_cache_home, requests, responses):
    import os
    import signal
    import time
    responses.put({"kind": "ready"})
    request = requests.get()
    assert os.path.exists(request["image_path"])
    time.sleep(0.3)                       # 送信スレッドが ready を流し終えるまで待つ（実機ではモデルの読込が間に入る）
    os.kill(os.getpid(), signal.SIGKILL)


def _crashing_child(_cache_home, requests, responses):
    import os
    responses.put({"kind": "ready"})
    requests.get()
    os._exit(3)


def _oom_counting_child(events_path):
    def child(_cache_home, requests, responses):
        import os
        responses.put({"kind": "ready"})
        requests.get()
        with open(events_path, "w", encoding="utf-8") as stream:
            stream.write("low 0\nhigh 0\nmax 4\noom 4\noom_kill 3\n")
        os._exit(1)
    return child


def _supervisor(tmp_path, target):
    return ocr_worker.PaddleProcessSupervisor(
        tmp_path, start_method="fork", process_target=target, work_root=tmp_path / "work",
    )


def _predict(supervisor, **kwargs):
    return supervisor.predict_monitored(
        b"pixels", media_type="image/png", timeout_seconds=10, poll_seconds=0.02, on_tick=lambda: None, **kwargs,
    )


def _leftovers(tmp_path):
    return [path for path in (tmp_path / "work").rglob("*") if path.is_file()]


def test_killed_child_leaves_no_input_file_and_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr_workdir, "CGROUP_MEMORY_EVENTS", tmp_path / "absent-memory.events")
    supervisor = _supervisor(tmp_path, _killed_child)
    with pytest.raises(ocr_worker.OCRResourceExhaustedError) as caught:
        _predict(supervisor)
    assert caught.value.retryable is False
    assert caught.value.error_code == "engine_resource_exhausted"
    assert "exitcode=-9" in str(caught.value) and "stage=inference" in str(caught.value)
    assert _leftovers(tmp_path) == []
    assert supervisor._process is None


def test_oom_kill_increment_is_resource_exhaustion_even_without_sigkill(tmp_path, monkeypatch):
    events = tmp_path / "memory.events"
    events.write_text("low 0\nhigh 0\nmax 4\noom 4\noom_kill 1\n", encoding="utf-8")
    monkeypatch.setattr(ocr_workdir, "CGROUP_MEMORY_EVENTS", events)
    supervisor = _supervisor(tmp_path, _oom_counting_child(str(events)))
    with pytest.raises(ocr_worker.OCRResourceExhaustedError) as caught:
        _predict(supervisor)
    assert "oom_kill_delta=2" in str(caught.value)
    assert _leftovers(tmp_path) == []


def test_other_child_exit_stays_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr_workdir, "CGROUP_MEMORY_EVENTS", tmp_path / "absent-memory.events")
    supervisor = _supervisor(tmp_path, _crashing_child)
    with pytest.raises(ocr_worker.OCRInferenceProcessError) as caught:
        _predict(supervisor)
    assert caught.value.retryable is True and caught.value.error_code == "engine_process_failed"
    assert "exitcode=3" in str(caught.value) and "oom_kill_delta=unknown" in str(caught.value)
    assert _leftovers(tmp_path) == []


def test_timeout_removes_input_file_and_stays_retryable(tmp_path):
    supervisor = ocr_worker.PaddleProcessSupervisor(
        tmp_path, start_method="fork", process_target=_hanging_child, work_root=tmp_path / "work",
    )
    with pytest.raises(TimeoutError):
        supervisor.predict_monitored(
            b"pixels", media_type="image/png", timeout_seconds=0.15, poll_seconds=0.02, on_tick=lambda: None,
        )
    assert _leftovers(tmp_path) == []


def _hanging_child(_cache_home, requests, responses):
    import time
    responses.put({"kind": "ready"})
    requests.get()
    time.sleep(60)


def test_startup_reclaims_only_dead_or_own_work_dirs(tmp_path):
    import os
    import subprocess

    root = tmp_path / "work"
    root.mkdir()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    for name in (str(dead.pid), str(os.getpid()), str(os.getppid())):
        (root / name).mkdir(exist_ok=True)
        (root / name / "stale.png").write_bytes(b"x")
    (root / "not-a-pid").mkdir()
    (root / "note.txt").write_text("keep", encoding="utf-8")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")

    removed = ocr_workdir.reclaim_work_dirs(root)

    assert removed == 2
    assert not (root / str(dead.pid)).exists() and not (root / str(os.getpid())).exists()
    assert (root / str(os.getppid()) / "stale.png").exists()
    assert (root / "not-a-pid").is_dir() and (root / "note.txt").exists() and outside.exists()


def test_supervisor_construction_reclaims_leftovers_of_a_crashed_predecessor(tmp_path):
    import os
    work = tmp_path / "work"
    (work / str(os.getpid())).mkdir(parents=True)
    (work / str(os.getpid()) / "stale.png").write_bytes(b"x")
    _supervisor(tmp_path, _killed_child)
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("raised, retryable", [
    (TimeoutError("slow"), True),
    (ocr_worker.OCRInferenceProcessError("exit"), True),
    (ocr_worker.OCRResourceExhaustedError("killed"), False),
])
def test_run_once_retries_only_what_can_succeed_next_time(monkeypatch, tmp_path, raised, retryable):
    ir, asset_root, _decision, job = _picture_route(tmp_path)
    failures: list[dict] = []

    class _Engine(FakeEngine):
        def predict_monitored(self, image_bytes, *, media_type, timeout_seconds, on_tick):
            raise raised

    _patch_lease(monkeypatch, job, cached=None)
    monkeypatch.setattr(ocr_worker.ocr_jobs, "fail_job",
                        lambda *args, **kwargs: failures.append(kwargs) or {**job, "status": "queued"})

    _run_once(ir, asset_root, _Engine())

    assert [item["retryable"] for item in failures] == [retryable]


def test_remove_input_also_removes_the_scaled_file_beside_it(tmp_path):
    (tmp_path / "abc.png").write_bytes(b"x")
    (tmp_path / "abc.scaled.png").write_bytes(b"x")
    (tmp_path / "other.png").write_bytes(b"x")

    ocr_workdir.remove_input(tmp_path / "abc.png")

    assert sorted(path.name for path in tmp_path.iterdir()) == ["other.png"]
