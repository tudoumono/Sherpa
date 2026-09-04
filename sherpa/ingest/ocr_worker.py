"""隔離PaddleOCR workerの実行契約。

Paddleはlazy importし、固定version・固定model hashがoffline cacheに揃わない限り起動しない。
このmoduleは既存MD変換armから呼ばず、公開済みCanonical generationに紐づくDB jobだけを処理する。
"""
from __future__ import annotations

import atexit
import hashlib
import importlib.metadata
import io
import json
import logging
import multiprocessing
import os
import queue
import shutil
import signal
import socket
import tempfile
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable, Protocol

from . import ai_observation, evidence_ir, observation_render, ocr_router
from ..store import ocr_jobs
from ..store.db import world_lock


_log = logging.getLogger("sherpa")


OCR_ENGINE_RESULT_SCHEMA = "ocr-engine-lines-v1"
OCR_PROMPT_SCHEMA_VERSION = "ocr-text-lines-v1"


@dataclass(frozen=True)
class PaddleCPUProfile:
    engine: str = "paddleocr"
    paddleocr_version: str = "3.7.0"
    paddlepaddle_version: str = "3.3.0"
    pypdfium2_version: str = "5.11.0"
    pillow_version: str = "12.3.0"
    device: str = "cpu"
    enable_mkldnn: bool = False
    detection_model: str = "PP-OCRv6_medium_det"
    # 2026-08-16 再ロック: 取得メタデータ（`.cache/`）を除いたmodel本体だけのtree hash。
    # 旧値（e597…/1de1…）は`.cache/`込みで、再downloadのたびに変わるため再現しなかった。
    detection_model_tree_sha256: str = "f74ebd70d463d5fe627dd1d0d235bc5097acda0ff1c05f527c363054bd6975ee"
    recognition_model: str = "PP-OCRv6_medium_rec"
    recognition_model_tree_sha256: str = "a0e6515f7e4c2745c07de72ffa867836f68a7aec988989754b8afb0d019bf3db"
    runtime_model_download: bool = False
    max_input_seconds: int = 600


PADDLE_CPU_PROFILE = PaddleCPUProfile()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def profile_hash(profile: PaddleCPUProfile = PADDLE_CPU_PROFILE) -> str:
    return "sha256:" + hashlib.sha256(_canonical(asdict(profile)).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OCRAvailability:
    available: bool
    unavailable_reason: str | None
    model_hashes_valid: bool
    cache_home: str
    paddleocr_version: str | None
    paddlepaddle_version: str | None
    pypdfium2_version: str | None
    pillow_version: str | None
    model_hashes: dict[str, str]
    engine_profile_hash: str


def _default_cache_home() -> Path:
    configured = os.environ.get("PADDLE_PDX_CACHE_HOME")
    return Path(configured) if configured else Path.home() / ".paddlex"


# downloaderが書く取得メタデータ（取得時刻・etag・lock）。model本体ではなく取得のたびに変わるため、
# 同一modelでもtree hashが一致しなくなる（2026-08-16実測: 同一modelで file_count=18・1バイト差・
# 精度は完全一致なのにhash不一致で `model_hash_mismatch` 起動拒否）。hashはmodel本体だけを対象にする。
_VOLATILE_CACHE_DIRS = frozenset({".cache"})


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda candidate: candidate.relative_to(path).as_posix()):
        if _VOLATILE_CACHE_DIRS & set(item.relative_to(path).parts):
            continue
        if item.is_symlink():
            raise ValueError("model cache contains symlink")
        if not item.is_file():
            continue
        relative = item.relative_to(path).as_posix().encode("utf-8")
        file_digest = hashlib.sha256()
        with item.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                file_digest.update(block)
        encoded_hash = file_digest.hexdigest().encode("ascii")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(encoded_hash)
    return digest.hexdigest()


def _installed_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


@lru_cache(maxsize=8)
def _paddle_availability_cached(cache_home_value: str) -> OCRAvailability:
    cache_home = Path(cache_home_value)
    paddleocr_version = _installed_version("paddleocr")
    paddlepaddle_version = _installed_version("paddlepaddle")
    pypdfium2_version = _installed_version("pypdfium2")
    pillow_version = _installed_version("Pillow")
    versions_valid = (
        paddleocr_version == PADDLE_CPU_PROFILE.paddleocr_version
        and paddlepaddle_version == PADDLE_CPU_PROFILE.paddlepaddle_version
        and pypdfium2_version == PADDLE_CPU_PROFILE.pypdfium2_version
        and pillow_version == PADDLE_CPU_PROFILE.pillow_version
    )
    model_root = cache_home / "official_models"
    expected = {
        PADDLE_CPU_PROFILE.detection_model: PADDLE_CPU_PROFILE.detection_model_tree_sha256,
        PADDLE_CPU_PROFILE.recognition_model: PADDLE_CPU_PROFILE.recognition_model_tree_sha256,
    }
    actual: dict[str, str] = {}
    missing = False
    invalid_cache = False
    for name in sorted(expected):
        directory = model_root / name
        if not directory.is_dir() or directory.is_symlink():
            missing = True
            continue
        try:
            actual[name] = _tree_digest(directory)
        except (OSError, ValueError):
            invalid_cache = True
    hashes_valid = not missing and not invalid_cache and actual == expected
    if None in {paddleocr_version, paddlepaddle_version, pypdfium2_version, pillow_version}:
        reason = "dependency_missing"
    elif not versions_valid:
        reason = "version_mismatch"
    elif missing:
        reason = "offline_model_missing"
    elif invalid_cache:
        reason = "model_cache_invalid"
    elif not hashes_valid:
        reason = "model_hash_mismatch"
    else:
        reason = None
    return OCRAvailability(
        available=reason is None,
        unavailable_reason=reason,
        model_hashes_valid=hashes_valid,
        cache_home=str(cache_home),
        paddleocr_version=paddleocr_version,
        paddlepaddle_version=paddlepaddle_version,
        pypdfium2_version=pypdfium2_version,
        pillow_version=pillow_version,
        model_hashes=actual,
        engine_profile_hash=profile_hash(),
    )


def paddle_availability(cache_home: Path | None = None) -> OCRAvailability:
    """依存versionとoffline model hashを検査する。modelをdownloadしない。"""
    selected = (cache_home or _default_cache_home()).expanduser().resolve()
    return _paddle_availability_cached(str(selected))


@dataclass(frozen=True)
class EngineLine:
    text: str
    confidence: float
    bbox: list[int | float]
    line_id: str


@dataclass(frozen=True)
class OCRPrediction:
    observations: list[EngineLine]

    def to_payload(self) -> dict[str, Any]:
        return {"schema": OCR_ENGINE_RESULT_SCHEMA, "observations": [asdict(item) for item in self.observations]}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "OCRPrediction":
        if payload.get("schema") != OCR_ENGINE_RESULT_SCHEMA or not isinstance(payload.get("observations"), list):
            raise ValueError("invalid OCR engine result")
        observations = [EngineLine(**item) for item in payload["observations"]]
        for item in observations:
            if (not item.text.strip() or not 0 <= item.confidence <= 1 or len(item.bbox) != 4
                    or item.bbox[2] <= item.bbox[0] or item.bbox[3] <= item.bbox[1]):
                raise ValueError("invalid OCR engine observation")
        return cls(observations=observations)


class OCREngine(Protocol):
    engine_profile_hash: str
    model_revision: str

    def predict(self, image_bytes: bytes, *, media_type: str) -> OCRPrediction: ...


class OCRWorkerError(RuntimeError):
    error_code = "worker_error"
    retryable = True


class OCRUnavailableError(OCRWorkerError):
    error_code = "engine_unavailable"
    retryable = False


class OCRBindingError(OCRWorkerError):
    error_code = "input_binding_failed"
    retryable = False


class OCRInferenceProcessError(OCRWorkerError):
    error_code = "engine_process_failed"
    retryable = True


class OCRStoppingError(OCRWorkerError):
    error_code = "worker_stopping"
    retryable = True


class OCRLeaseLostError(RuntimeError):
    """leaseを失った推論結果をcache/jobへ書かないための内部制御例外。"""


def _never_stop() -> bool:
    return False


def _paddle_bbox(box: Any) -> list[float]:
    if len(box) == 4 and not isinstance(box[0], (list, tuple)):
        result = [float(value) for value in box]
    else:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        result = [min(xs), min(ys), max(xs), max(ys)]
    if result[2] <= result[0] or result[3] <= result[1]:
        raise ValueError("PaddleOCR returned invalid bbox")
    return result


class PaddleOCREngine:
    """offline cache検証後だけ初期化できる固定CPU PaddleOCR adapter。"""

    engine_profile_hash = profile_hash()
    model_revision = profile_hash()

    def __init__(self, cache_home: Path | None = None):
        availability = paddle_availability(cache_home)
        if not availability.available:
            raise OCRUnavailableError(availability.unavailable_reason or "PaddleOCR unavailable")
        os.environ["PADDLE_PDX_CACHE_HOME"] = availability.cache_home
        os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
        from paddleocr import PaddleOCR
        self._ocr = PaddleOCR(
            device="cpu",
            enable_mkldnn=False,
            text_detection_model_name=PADDLE_CPU_PROFILE.detection_model,
            text_recognition_model_name=PADDLE_CPU_PROFILE.recognition_model,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )

    def predict(self, image_bytes: bytes, *, media_type: str) -> OCRPrediction:
        suffix = ".png" if media_type == "image/png" else ".jpg" if media_type == "image/jpeg" else ".img"
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="sherpa-ocr-", suffix=suffix, delete=False) as stream:
                stream.write(image_bytes)
                temporary_path = Path(stream.name)
            results = list(self._ocr.predict(str(temporary_path)))
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        if len(results) != 1:
            raise ValueError(f"PaddleOCR returned {len(results)} results for one image")
        payload: Any = results[0].json if hasattr(results[0], "json") else results[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        values = payload.get("res", payload)
        texts = values.get("rec_texts") or []
        scores = values.get("rec_scores") or []
        boxes = values.get("rec_boxes") or values.get("rec_polys") or []
        if not (len(texts) == len(scores) == len(boxes)):
            raise ValueError("PaddleOCR result arrays differ in length")
        lines = []
        for index, (text, score, box) in enumerate(zip(texts, scores, boxes, strict=True)):
            # 識別子や空白を勝手に直さず、engine出力の文字列をそのまま保存する。
            raw_text = str(text)
            if not raw_text.strip():
                continue
            lines.append(EngineLine(
                text=raw_text, confidence=round(float(score), 6), bbox=_paddle_bbox(box), line_id=str(index),
            ))
        return OCRPrediction(lines)


def _paddle_inference_child(cache_home: str, request_queue: Any, response_queue: Any) -> None:
    """Paddle runtimeをFastAPI/worker supervisorから隔離する永続child process。"""
    try:
        engine = PaddleOCREngine(Path(cache_home))
    except BaseException as exc:
        response_queue.put({"kind": "startup_error", "error_type": exc.__class__.__name__})
        return
    response_queue.put({"kind": "ready"})
    while True:
        request = request_queue.get()
        if request is None:
            return
        request_id = str(request.get("request_id") or "")
        try:
            prediction = engine.predict(request["image_bytes"], media_type=request["media_type"])
            response_queue.put({"kind": "result", "request_id": request_id, "payload": prediction.to_payload()})
        except MemoryError:
            response_queue.put({"kind": "oom", "request_id": request_id})
        except BaseException as exc:
            # 原本文字列やengine messageをIPC/error logへ搬送しない。分類に必要な型名だけを返す。
            response_queue.put({"kind": "error", "request_id": request_id, "error_type": exc.__class__.__name__})


class PaddleProcessSupervisor:
    """Paddle推論を停止可能なchild processへ閉じ込める1-concurrency supervisor。

    childはmodelを一度だけloadして複数jobで再利用する。timeout、lease喪失、SIGTERM時はchildを
    terminate/killし、hangしたnative runtimeをworker本体から切り離す。
    """

    engine_profile_hash = profile_hash()
    model_revision = profile_hash()

    def __init__(
        self,
        cache_home: Path | None = None,
        *,
        start_method: str = "spawn",
        process_target: Callable[[str, Any, Any], None] = _paddle_inference_child,
    ):
        self.cache_home = str((cache_home or _default_cache_home()).expanduser().resolve())
        self._context = multiprocessing.get_context(start_method)
        self._process_target = process_target
        self._process: Any = None
        self._requests: Any = None
        self._responses: Any = None

    def _start(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        self.close()
        self._requests = self._context.Queue(maxsize=1)
        self._responses = self._context.Queue(maxsize=2)
        self._process = self._context.Process(
            target=self._process_target,
            args=(self.cache_home, self._requests, self._responses),
            name="sherpa-paddle-inference",
            daemon=True,
        )
        self._process.start()

    def _terminate(self) -> None:
        process = self._process
        if process is None:
            return
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
        if process.is_alive():  # native runtimeがSIGTERMを握り潰した場合の最終境界
            process.kill()
            process.join(timeout=3)

    def close(self) -> None:
        process = self._process
        if process is not None and process.is_alive():
            try:
                self._requests.put_nowait(None)
            except (AttributeError, queue.Full):
                pass
            process.join(timeout=3)
        if process is not None and process.is_alive():
            self._terminate()
        for channel in (self._requests, self._responses):
            if channel is not None:
                try:
                    # timeoutでchildをkillした直後、巨大image bytesをpipeへflush中のfeeder threadを
                    # joinすると停止処理自体がhangし得る。未送信IPCは破棄してsupervisorを優先する。
                    channel.cancel_join_thread()
                    channel.close()
                except (AttributeError, ValueError):
                    pass
        self._process = self._requests = self._responses = None

    def predict_monitored(
        self,
        image_bytes: bytes,
        *,
        media_type: str,
        timeout_seconds: float,
        on_tick: Callable[[], None],
        poll_seconds: float = 1.0,
    ) -> OCRPrediction:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("positive Paddle timeout and poll interval are required")
        self._start()
        request_id = uuid.uuid4().hex
        self._requests.put({"request_id": request_id, "image_bytes": image_bytes, "media_type": media_type})
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                on_tick()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("OCR inference exceeded worker timeout")
                try:
                    response = self._responses.get(timeout=min(poll_seconds, remaining))
                except queue.Empty:
                    if self._process is None or not self._process.is_alive():
                        raise OCRInferenceProcessError("Paddle inference child exited without a result")
                    continue
                kind = response.get("kind")
                if kind == "ready":
                    continue
                if kind == "startup_error":
                    raise OCRInferenceProcessError(
                        f"Paddle inference child could not start ({response.get('error_type', 'unknown')})"
                    )
                if response.get("request_id") != request_id:
                    raise OCRInferenceProcessError("Paddle inference response id mismatch")
                if kind == "result":
                    return OCRPrediction.from_payload(response["payload"])
                if kind == "oom":
                    raise MemoryError("Paddle inference child reported OOM")
                raise OCRInferenceProcessError(
                    f"Paddle inference failed ({response.get('error_type', 'unknown')})"
                )
        except BaseException:
            # timeout/lease喪失/stopping時も、推論が裏で継続してCPUや原本bytesを保持しないよう必ず停止する。
            self._terminate()
            self.close()
            raise

    def predict(self, image_bytes: bytes, *, media_type: str) -> OCRPrediction:
        """Protocol互換。製品run_onceはpredict_monitoredを使う。"""
        return self.predict_monitored(
            image_bytes,
            media_type=media_type,
            timeout_seconds=PADDLE_CPU_PROFILE.max_input_seconds,
            on_tick=lambda: None,
        )


@dataclass(frozen=True)
class PreparedOCRInput:
    image_bytes: bytes
    asset_sha256: str
    media_type: str
    pixel_size: list[int]
    input_kind: str
    render_profile: dict[str, Any] | None


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


_SOURCE_HASH_CACHE_LIMIT = 64
_SOURCE_HASH_CACHE: OrderedDict[tuple[str, int, int, int, int, int, str], str] = OrderedDict()
_SOURCE_HASH_CACHE_LOCK = Lock()


def _source_identity(path: Path) -> tuple[Path, tuple[str, int, int, int, int, int]]:
    try:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
    except OSError as exc:
        raise OCRBindingError("source file is unavailable") from exc
    if not resolved.is_file():
        raise OCRBindingError("source path is not a regular file")
    return resolved, (
        str(resolved), int(stat.st_dev), int(stat.st_ino), int(stat.st_size),
        int(stat.st_mtime_ns), int(stat.st_ctime_ns),
    )


def _source_hash_uncached(path: Path, *, on_progress: Callable[[], None] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            if on_progress is not None:
                on_progress()
    return "sha256:" + digest.hexdigest()


def _source_hash(
    path: Path,
    *,
    expected_hash: str | None = None,
    on_progress: Callable[[], None] | None = None,
) -> str:
    """同じread-only原本をjobごとに全量再読しない、有界かつstat拘束済みhash。"""
    resolved, identity = _source_identity(path)
    cache_key = (*identity, expected_hash or "")
    with _SOURCE_HASH_CACHE_LOCK:
        cached = _SOURCE_HASH_CACHE.get(cache_key)
        if cached is not None:
            _SOURCE_HASH_CACHE.move_to_end(cache_key)
    if cached is not None:
        _resolved_after, identity_after = _source_identity(resolved)
        if identity_after == identity:
            return cached
        raise OCRBindingError("source file changed while validating cached hash")

    actual = _source_hash_uncached(resolved, on_progress=on_progress)
    _resolved_after, identity_after = _source_identity(resolved)
    if identity_after != identity:
        raise OCRBindingError("source file changed while hashing")
    with _SOURCE_HASH_CACHE_LOCK:
        _SOURCE_HASH_CACHE[cache_key] = actual
        _SOURCE_HASH_CACHE.move_to_end(cache_key)
        while len(_SOURCE_HASH_CACHE) > _SOURCE_HASH_CACHE_LIMIT:
            _SOURCE_HASH_CACHE.popitem(last=False)
    return actual


def _clear_source_hash_cache() -> None:
    """test/process lifecycle用。通常workerではLRU evictionへ任せる。"""
    with _SOURCE_HASH_CACHE_LOCK:
        _SOURCE_HASH_CACHE.clear()


def _read_bytes(path: Path, *, on_progress: Callable[[], None] | None = None) -> bytes:
    payload = bytearray()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            payload.extend(block)
            if on_progress is not None:
                on_progress()
    return bytes(payload)


def _image_size(raw: bytes) -> list[int]:
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        return [image.width, image.height]


def _safe_asset(asset_root: Path, relative_path: str) -> Path:
    root = asset_root.resolve()
    candidate = root.joinpath(*Path(relative_path).parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise OCRBindingError("asset path escaped generation root") from exc
    current = candidate
    while current != root:
        if current.is_symlink():
            raise OCRBindingError("asset path contains symlink")
        current = current.parent
    if not resolved.is_file():
        raise OCRBindingError("asset is not a file")
    return resolved


_PDF_DOCUMENT_CACHE_LOCK = Lock()
_PDF_DOCUMENT_CACHE_KEY: tuple[str, int, int, int, int, int] | None = None
_PDF_DOCUMENT_CACHE: Any | None = None


def _open_pdf_document(path: Path):
    try:
        import pypdfium2
    except ImportError as exc:
        raise OCRUnavailableError("pypdfium2 is unavailable in OCR worker") from exc
    return pypdfium2.PdfDocument(str(path))


def _clear_pdf_document_cache() -> None:
    global _PDF_DOCUMENT_CACHE, _PDF_DOCUMENT_CACHE_KEY
    with _PDF_DOCUMENT_CACHE_LOCK:
        if _PDF_DOCUMENT_CACHE is not None:
            try:
                _PDF_DOCUMENT_CACHE.close()
            except Exception:
                pass
        _PDF_DOCUMENT_CACHE = None
        _PDF_DOCUMENT_CACHE_KEY = None


atexit.register(_clear_pdf_document_cache)


def _render_pdf_page(source_path: Path, render_profile: dict[str, Any]) -> bytes:
    global _PDF_DOCUMENT_CACHE, _PDF_DOCUMENT_CACHE_KEY
    if render_profile != {**ocr_router.PAGE_RENDER_PROFILE, "page_1_based": render_profile.get("page_1_based")}:
        raise OCRBindingError("unsupported PDF page render profile")
    page_number = render_profile.get("page_1_based")
    if not isinstance(page_number, int) or page_number <= 0:
        raise OCRBindingError("invalid PDF page number")
    resolved, identity = _source_identity(source_path)
    # worker concurrency=1が既定だが、lock内でpage/bitmapまで閉じて将来の並行呼出しにも備える。
    with _PDF_DOCUMENT_CACHE_LOCK:
        if _PDF_DOCUMENT_CACHE_KEY != identity:
            if _PDF_DOCUMENT_CACHE is not None:
                _PDF_DOCUMENT_CACHE.close()
            _PDF_DOCUMENT_CACHE = _open_pdf_document(resolved)
            _PDF_DOCUMENT_CACHE_KEY = identity
        document = _PDF_DOCUMENT_CACHE
        if page_number > len(document):
            raise OCRBindingError("PDF page no longer exists")
        page = document[page_number - 1]
        try:
            bitmap = page.render(scale=float(render_profile["dpi"]) / 72.0)
            try:
                image = bitmap.to_pil().convert("RGB")
                buffer = io.BytesIO()
                image.save(buffer, format="PNG", optimize=False)
                raw = buffer.getvalue()
            finally:
                bitmap.close()
        finally:
            page.close()
        _resolved_after, identity_after = _source_identity(resolved)
        if identity_after != identity:
            _PDF_DOCUMENT_CACHE.close()
            _PDF_DOCUMENT_CACHE = None
            _PDF_DOCUMENT_CACHE_KEY = None
            raise OCRBindingError("PDF source changed while rendering")
        return raw


def prepare_input(
    job: dict[str, Any],
    decision: ocr_router.OCRRouteDecision,
    *,
    source_path: Path,
    asset_root: Path,
    on_progress: Callable[[], None] | None = None,
) -> PreparedOCRInput:
    """source/assetをread-onlyで再hashし、workerへ渡す画素bytesを固定する。"""
    expected_source_hash = str(job["source_content_hash"])
    if _source_hash(
        source_path, expected_hash=expected_source_hash, on_progress=on_progress,
    ) != expected_source_hash:
        raise OCRBindingError("source content hash changed")
    if decision.input_kind == "asset":
        if not decision.asset_rel_path or not decision.asset_sha256 or not decision.media_type:
            raise OCRBindingError("asset route is incomplete")
        raw = _read_bytes(_safe_asset(asset_root, decision.asset_rel_path), on_progress=on_progress)
        if _sha256_bytes(raw) != decision.asset_sha256:
            raise OCRBindingError("asset hash mismatch")
        return PreparedOCRInput(
            image_bytes=raw, asset_sha256=decision.asset_sha256, media_type=decision.media_type,
            pixel_size=decision.pixel_size or _image_size(raw), input_kind="asset", render_profile=None,
        )
    if decision.input_kind == "page_render" and decision.page_render is not None:
        if source_path.suffix.lower() != ".pdf":
            raise OCRBindingError("page render source must be PDF")
        raw = _render_pdf_page(source_path, decision.page_render)
        if on_progress is not None:
            on_progress()
        return PreparedOCRInput(
            image_bytes=raw, asset_sha256=_sha256_bytes(raw), media_type="image/png",
            pixel_size=_image_size(raw), input_kind="page_render", render_profile=decision.page_render,
        )
    raise OCRBindingError("unsupported OCR route input")


def build_observation_set(
    *,
    ir: evidence_ir.EvidenceIR,
    decision: ocr_router.OCRRouteDecision,
    prepared: PreparedOCRInput,
    prediction: OCRPrediction,
    canonical_generation_id: str,
    engine: OCREngine,
) -> ai_observation.AIObservationSet:
    """Paddle行をocr_textの検索可Observation Setへ固定する。

    `use_for_answer`（O1）: 行ごとの実測 confidence が既存の使用可否ルール
    （`ai_observation.MIN_ANSWER_CONFIDENCE`＝VLM も従う同じ閾値）以上なら True にする。
    以前は常に False（rag.md へは出さず `observation_render` の検索専用成果物にだけ載せる設計）
    だったが、OCR も rag.md の「AI観測」レコード（`evidence_render._ai_observation_records`）へ
    統合する今は、この既存ルールを他アームと同じに適用しないと OCR 観測が rag.md に一切出ない
    （新しい閾値は作らない・既存ルールをそのまま OCR にも適用するだけ）。
    """
    payload = prediction.to_payload()
    return ai_observation.build(
        ir=ir, provider="paddleocr", model=(
            f"{PADDLE_CPU_PROFILE.detection_model}+{PADDLE_CPU_PROFILE.recognition_model}"
        ), model_revision=engine.model_revision, execution_mode="local",
        prompt_schema_version=OCR_PROMPT_SCHEMA_VERSION,
        preprocessing_profile=(prepared.render_profile or {}).get("profile", "embedded-asset-original-v1"),
        engine_profile_hash=engine.engine_profile_hash,
        canonical_generation_id=canonical_generation_id,
        raw_response=_canonical(payload),
        inputs=[{
            "input_id": decision.route_input_id,
            "target_evidence_id": decision.target_evidence_id,
            "asset_sha256": prepared.asset_sha256,
            "media_type": prepared.media_type,
            "pixel_size": prepared.pixel_size,
            "input_kind": prepared.input_kind,
            "render_profile": prepared.render_profile,
        }],
        observations=[{
            "input_id": decision.route_input_id,
            "kind": "ocr_text",
            "text": line.text,
            "confidence": line.confidence,
            "pixel_bbox": line.bbox,
            "searchable": True,
            "use_for_answer": bool(line.confidence >= ai_observation.MIN_ANSWER_CONFIDENCE),
            "numeric_verified": False,
            "attributes": {
                "line_id": line.line_id,
                "engine": "paddleocr",
                "engine_profile_hash": engine.engine_profile_hash,
                "text_normalized": False,
            },
        } for line in prediction.observations],
    )


@dataclass(frozen=True)
class WorkerResult:
    status: str
    job_id: int | None = None
    observation_set_hash: str | None = None
    error_code: str | None = None
    cache_hit: bool = False


class _JobLeaseMonitor:
    def __init__(
        self,
        *,
        worker_id: str,
        job_id: int,
        lease_token: str,
        lease_seconds: int,
        interval_seconds: float,
        heartbeat: Callable[[str], None] | None,
        should_stop: Callable[[], bool],
    ):
        if lease_seconds <= 0 or interval_seconds <= 0 or interval_seconds >= lease_seconds:
            raise ValueError("lease monitor interval must be shorter than the positive lease TTL")
        self.worker_id = worker_id
        self.job_id = job_id
        self.lease_token = lease_token
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self.heartbeat = heartbeat
        self.should_stop = should_stop
        self._next_update = 0.0

    def tick(self, *, force: bool = False) -> None:
        if self.should_stop():
            raise OCRStoppingError("OCR worker is stopping")
        now = time.monotonic()
        if not force and now < self._next_update:
            return
        if not ocr_jobs.renew_lease(self.job_id, self.lease_token, lease_seconds=self.lease_seconds):
            raise OCRLeaseLostError("OCR job lease was lost")
        if self.heartbeat is not None:
            self.heartbeat("processing")
        self._next_update = now + self.interval_seconds


def _predict_with_monitor(
    engine: OCREngine,
    prepared: PreparedOCRInput,
    *,
    timeout_seconds: float,
    monitor: _JobLeaseMonitor,
) -> OCRPrediction:
    monitored = getattr(engine, "predict_monitored", None)
    if callable(monitored):
        return monitored(
            prepared.image_bytes,
            media_type=prepared.media_type,
            timeout_seconds=timeout_seconds,
            on_tick=monitor.tick,
        )
    # Fake/custom engines retain the original small synchronous protocol. Production Paddle always uses
    # PaddleProcessSupervisor above, so the elapsed check is not its timeout boundary.
    started = time.monotonic()
    prediction = engine.predict(prepared.image_bytes, media_type=prepared.media_type)
    if time.monotonic() - started > timeout_seconds:
        raise TimeoutError("OCR input exceeded worker timeout")
    return prediction


def _publish_terminal_generation(
    job: dict[str, Any],
    publish_observation: Callable[[dict[str, Any], ai_observation.AIObservationSet | None], None] | None,
    observation_set: ai_observation.AIObservationSet | None,
) -> bool:
    if publish_observation is None:
        return False
    if not ocr_jobs.generation_ready_for_publication(job["world"], job["canonical_generation_id"]):
        return False
    publish_observation(job, observation_set)
    return True


def run_once(
    worker_id: str,
    *,
    engine: OCREngine,
    canonical_is_current: Callable[[str, str], bool],
    load_ir: Callable[[dict[str, Any]], evidence_ir.EvidenceIR],
    resolve_source: Callable[[dict[str, Any]], Path],
    resolve_asset_root: Callable[[dict[str, Any]], Path],
    publish_observation: Callable[[dict[str, Any], ai_observation.AIObservationSet | None], None] | None = None,
    lease_seconds: int = 90,
    lease_renew_interval_seconds: float = 15.0,
    inference_timeout_seconds: float = PADDLE_CPU_PROFILE.max_input_seconds,
    heartbeat: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] = _never_stop,
) -> WorkerResult:
    """queueを1件処理する。Canonical生成・RAG登録の成功状態は変更しない。"""
    if inference_timeout_seconds <= 0 or lease_seconds <= 0:
        raise ValueError("inference timeout and lease TTL must be positive")
    if lease_renew_interval_seconds <= 0 or lease_renew_interval_seconds >= lease_seconds:
        raise ValueError("lease monitor interval must be shorter than the lease TTL")
    job = ocr_jobs.lease_next(worker_id, lease_seconds=lease_seconds)
    if job is None:
        return WorkerResult(status="idle")
    job_id, lease_token = int(job["id"]), str(job["lease_token"])
    monitor = _JobLeaseMonitor(
        worker_id=worker_id,
        job_id=job_id,
        lease_token=lease_token,
        lease_seconds=lease_seconds,
        interval_seconds=lease_renew_interval_seconds,
        heartbeat=heartbeat,
        should_stop=should_stop,
    )
    try:
        monitor.tick(force=True)
        if not canonical_is_current(job["world"], job["canonical_generation_id"]):
            ocr_jobs.mark_stale(job_id, lease_token)
            return WorkerResult(status="stale", job_id=job_id)
        if job["engine_profile_hash"] != engine.engine_profile_hash:
            raise OCRUnavailableError("worker profile does not match queued job")
        decision = ocr_router.OCRRouteDecision(**job["route_input"])
        if decision.status != "selected":
            raise OCRBindingError("leased route is not selected")
        ir = load_ir(job)
        prepared = prepare_input(
            job, decision, source_path=resolve_source(job), asset_root=resolve_asset_root(job),
            on_progress=monitor.tick,
        )
        monitor.tick(force=True)
        cached = ocr_jobs.get_cached_result(job["world"], prepared.asset_sha256, engine.engine_profile_hash)
        cache_hit = cached is not None
        if cached is not None:
            prediction = OCRPrediction.from_payload(cached["result_payload"])
        else:
            prediction = _predict_with_monitor(
                engine, prepared, timeout_seconds=inference_timeout_seconds, monitor=monitor,
            )
            # Leaseを失ったworkerは共有cacheにも推論結果をcommitしない。
            monitor.tick(force=True)
            cached = ocr_jobs.put_cached_result_for_lease(
                job_id,
                lease_token,
                job["world"],
                prepared.asset_sha256,
                engine.engine_profile_hash,
                prediction.to_payload(),
            )
            if cached is None:
                raise OCRLeaseLostError("OCR job lease was lost before cache commit")
            prediction = OCRPrediction.from_payload(cached["result_payload"])
        observation_set = build_observation_set(
            ir=ir, decision=decision, prepared=prepared, prediction=prediction,
            canonical_generation_id=job["canonical_generation_id"], engine=engine,
        )
        if not canonical_is_current(job["world"], job["canonical_generation_id"]):
            ocr_jobs.mark_stale(job_id, lease_token, reason="canonical_generation_changed_after_inference")
            return WorkerResult(status="stale", job_id=job_id, cache_hit=cache_hit)
        monitor.tick(force=True)
        completed = ocr_jobs.complete_job(
            job_id, lease_token, observation_set_hash=observation_set.observation_set_hash,
            result_payload=json.loads(ai_observation.to_json_str(observation_set)),
            cache_hit=cache_hit, observation_count=len(observation_set.observations),
        )
        if completed is None:
            return WorkerResult(status="lease_lost", job_id=job_id, cache_hit=cache_hit)
        try:
            _publish_terminal_generation(completed, publish_observation, observation_set)
        except Exception:
            # OCR結果はDBへ完了済み。別観測pointerの再構築はidle時に再試行できる。
            return WorkerResult(
                status="publish_failed", job_id=job_id,
                observation_set_hash=observation_set.observation_set_hash, error_code="artifact_publish_failed",
                cache_hit=cache_hit,
            )
        return WorkerResult(
            status="succeeded", job_id=job_id, observation_set_hash=observation_set.observation_set_hash,
            cache_hit=cache_hit,
        )
    except OCRLeaseLostError:
        return WorkerResult(status="lease_lost", job_id=job_id)
    except OCRStoppingError as exc:
        ocr_jobs.fail_job(
            job_id, lease_token, error_code=exc.error_code, error_detail=str(exc), retryable=True,
            retry_delay_seconds=0,
        )
        return WorkerResult(status="stopping", job_id=job_id, error_code=exc.error_code)
    except OCRWorkerError as exc:
        failed = ocr_jobs.fail_job(
            job_id, lease_token, error_code=exc.error_code, error_detail=str(exc), retryable=exc.retryable,
        )
        if failed is not None and failed.get("status") == "failed":
            try:
                _publish_terminal_generation(failed, publish_observation, None)
            except Exception:
                pass
        return WorkerResult(status="failed", job_id=job_id, error_code=exc.error_code)
    except TimeoutError as exc:
        failed = ocr_jobs.fail_job(
            job_id, lease_token, error_code="timeout", error_detail=str(exc), retryable=True,
        )
        if failed is not None and failed.get("status") == "failed":
            try:
                _publish_terminal_generation(failed, publish_observation, None)
            except Exception:
                pass
        return WorkerResult(status="failed", job_id=job_id, error_code="timeout")
    except MemoryError as exc:
        failed = ocr_jobs.fail_job(
            job_id, lease_token, error_code="oom", error_detail=str(exc), retryable=False,
        )
        if failed is not None:
            try:
                _publish_terminal_generation(failed, publish_observation, None)
            except Exception:
                pass
        return WorkerResult(status="failed", job_id=job_id, error_code="oom")
    except Exception as exc:
        failed = ocr_jobs.fail_job(
            job_id, lease_token, error_code="engine_failure", error_detail=exc.__class__.__name__, retryable=True,
        )
        if failed is not None and failed.get("status") == "failed":
            try:
                _publish_terminal_generation(failed, publish_observation, None)
            except Exception:
                pass
        return WorkerResult(status="failed", job_id=job_id, error_code="engine_failure")


@dataclass(frozen=True)
class RefreshWorkerResult:
    status: str
    run_id: int | None = None
    manifests_processed: int = 0
    jobs_enqueued: int = 0
    error_code: str | None = None


def _route_manifest_paths(
    generation_root: Path,
    *,
    after: str | None,
    on_directory: Callable[[], None],
):
    """全pathをmemoryへ載せず、決定順でroute manifestをstreamする。"""
    candidate_root = Path(generation_root)
    if candidate_root.is_symlink():
        raise OCRBindingError("Canonical generation root is invalid")
    root = candidate_root.resolve(strict=True)
    if not root.is_dir():
        raise OCRBindingError("Canonical generation root is invalid")
    for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        on_directory()
        current = Path(directory)
        directory_names[:] = sorted(
            name for name in directory_names if not (current / name).is_symlink()
        )
        for name in sorted(file_names):
            if not name.endswith(".ocr_route.json"):
                continue
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise OCRBindingError("OCR route manifest must be a regular non-symlink file")
            relative = path.relative_to(root).as_posix()
            if after is not None and relative <= after:
                continue
            yield relative, path


def run_refresh_once(
    worker_id: str,
    *,
    engine_profile_hash: str,
    canonical_is_current: Callable[[str, str], bool],
    resolve_generation_root: Callable[[str, str], Path],
    lease_seconds: int = 90,
    lease_renew_interval_seconds: float = 15.0,
    heartbeat: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] = _never_stop,
) -> RefreshWorkerResult:
    """1 refresh runをstream展開する。HTTP requestはこの走査を実行しない。"""
    if lease_seconds <= 0 or lease_renew_interval_seconds <= 0 or lease_renew_interval_seconds >= lease_seconds:
        raise ValueError("refresh lease interval must be shorter than the positive lease TTL")
    run = ocr_jobs.lease_refresh_run(worker_id, lease_seconds=lease_seconds)
    if run is None:
        return RefreshWorkerResult(status="idle")
    run_id, lease_token = int(run["id"]), str(run["lease_token"])
    next_update = 0.0

    def tick(*, force: bool = False) -> None:
        nonlocal next_update
        if should_stop():
            raise OCRStoppingError("OCR worker is stopping")
        now = time.monotonic()
        if not force and now < next_update:
            return
        if not ocr_jobs.renew_refresh_run(run_id, lease_token, lease_seconds=lease_seconds):
            raise OCRLeaseLostError("OCR refresh lease was lost")
        if heartbeat is not None:
            heartbeat("processing")
        next_update = now + lease_renew_interval_seconds

    processed = jobs_enqueued = 0
    try:
        tick(force=True)
        world = str(run["world"])
        generation = str(run["canonical_generation_id"])
        if run["engine_profile_hash"] != engine_profile_hash:
            raise OCRUnavailableError("refresh run profile does not match worker")
        if not canonical_is_current(world, generation):
            ocr_jobs.mark_refresh_run_stale(run_id, lease_token)
            return RefreshWorkerResult(status="stale", run_id=run_id)
        root = resolve_generation_root(world, generation)
        cursor = run.get("cursor_rel_path")
        for relative, route_path in _route_manifest_paths(root, after=cursor, on_directory=tick):
            tick()
            source_rel_path = relative[: -len(".ocr_route.json")]
            evidence_path = root.joinpath(*Path(source_rel_path + ".evidence.json").parts)
            if evidence_path.is_symlink() or not evidence_path.is_file():
                raise OCRBindingError(f"Evidence missing for route manifest: {source_rel_path}")
            ir = evidence_ir.from_json_str(evidence_path.read_text(encoding="utf-8"))
            manifest = ocr_router.from_json_str(route_path.read_text(encoding="utf-8"), ir=ir)
            counts = {"selected": 0, "excluded": 0, "failed_binding": 0}
            for decision in manifest.decisions:
                counts[decision.status] += 1
            rows = ocr_jobs.enqueue_manifest_jobs(
                world,
                manifest,
                canonical_generation_id=generation,
                engine_profile_hash=engine_profile_hash,
            )
            if not ocr_jobs.update_refresh_run_progress(
                run_id,
                lease_token,
                cursor_rel_path=relative,
                selected_delta=counts["selected"],
                excluded_delta=counts["excluded"],
                failed_binding_delta=counts["failed_binding"],
                jobs_delta=len(rows),
                lease_seconds=lease_seconds,
            ):
                raise OCRLeaseLostError("OCR refresh lease was lost while committing cursor")
            processed += 1
            jobs_enqueued += len(rows)
            next_update = time.monotonic() + lease_renew_interval_seconds
        tick(force=True)
        if not canonical_is_current(world, generation):
            ocr_jobs.mark_refresh_run_stale(run_id, lease_token)
            return RefreshWorkerResult(
                status="stale", run_id=run_id, manifests_processed=processed, jobs_enqueued=jobs_enqueued,
            )
        completed = ocr_jobs.complete_refresh_run(run_id, lease_token)
        if completed is None:
            return RefreshWorkerResult(
                status="lease_lost", run_id=run_id, manifests_processed=processed, jobs_enqueued=jobs_enqueued,
            )
        return RefreshWorkerResult(
            status="refresh_completed", run_id=run_id,
            manifests_processed=processed, jobs_enqueued=jobs_enqueued,
        )
    except OCRLeaseLostError:
        return RefreshWorkerResult(
            status="lease_lost", run_id=run_id, manifests_processed=processed, jobs_enqueued=jobs_enqueued,
        )
    except OCRStoppingError as exc:
        ocr_jobs.fail_refresh_run(
            run_id, lease_token, error_code=exc.error_code, error_detail=str(exc), retryable=True,
        )
        return RefreshWorkerResult(
            status="stopping", run_id=run_id, manifests_processed=processed,
            jobs_enqueued=jobs_enqueued, error_code=exc.error_code,
        )
    except OCRWorkerError as exc:
        ocr_jobs.fail_refresh_run(
            run_id, lease_token, error_code=exc.error_code, error_detail=str(exc), retryable=exc.retryable,
        )
        return RefreshWorkerResult(
            status="failed", run_id=run_id, manifests_processed=processed,
            jobs_enqueued=jobs_enqueued, error_code=exc.error_code,
        )
    except Exception as exc:
        ocr_jobs.fail_refresh_run(
            run_id, lease_token, error_code="refresh_failed", error_detail=exc.__class__.__name__, retryable=True,
        )
        return RefreshWorkerResult(
            status="failed", run_id=run_id, manifests_processed=processed,
            jobs_enqueued=jobs_enqueued, error_code="refresh_failed",
        )


def garbage_collect_observation_generations(
    observation_root: str | Path,
    *,
    active_canonical_generation_id: str,
    keep_current: int = 2,
    remove_noncurrent: bool = True,
) -> dict[str, int]:
    """不変な観測generationを現行＋直前へ制限する。Canonical成果物には触れない。"""
    if keep_current < 1:
        raise ValueError("at least one observation generation must be retained")
    active = active_canonical_generation_id.strip().lower()
    if len(active) != 64 or any(character not in "0123456789abcdef" for character in active):
        raise ValueError("invalid canonical generation id")
    root = Path(observation_root)
    generations_root = root / observation_render.OBSERVATION_GENERATIONS_NAME
    if not generations_root.exists():
        return {"generations_removed": 0, "canonical_roots_removed": 0}
    if generations_root.is_symlink() or not generations_root.is_dir():
        raise ValueError("observation generations root must be a directory")
    pointer_path = root / observation_render.OBSERVATION_POINTER_NAME
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pointer = {}
    preferred = [
        value for value in (
            pointer.get("observation_generation_id"), pointer.get("previous_observation_generation_id"),
        ) if isinstance(value, str)
    ] if pointer.get("canonical_generation_id") == active else []
    removed = removed_roots = 0
    for canonical_root in sorted(generations_root.iterdir(), key=lambda path: path.name):
        if canonical_root.is_symlink():
            canonical_root.unlink()
            removed_roots += 1
            continue
        if not canonical_root.is_dir():
            continue
        if canonical_root.name != active and remove_noncurrent:
            shutil.rmtree(canonical_root)
            removed_roots += 1
            continue
        children = [
            path for path in canonical_root.iterdir()
            if path.is_dir() and not path.is_symlink() and not path.name.startswith(".staging-")
        ]
        by_newest = sorted(children, key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
        keep: list[str] = []
        for generation_id in preferred + [path.name for path in by_newest]:
            if generation_id not in keep and (canonical_root / generation_id).is_dir():
                keep.append(generation_id)
            if len(keep) >= keep_current:
                break
        for child in canonical_root.iterdir():
            if child.name in keep or child.name.startswith(".staging-"):
                continue
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child)
            removed += 1
        if canonical_root.name != active and not any(canonical_root.iterdir()):
            canonical_root.rmdir()
            removed_roots += 1
    return {"generations_removed": removed, "canonical_roots_removed": removed_roots}


def build_standard_publish_callback(
    *,
    resolve_derived_root: Callable[[str], Path],
    canonical_is_current: Callable[[str, str], bool],
    load_ir: Callable[[dict[str, Any]], evidence_ir.EvidenceIR],
    on_published: Callable[[str, str], None] | None = None,
) -> Callable[[dict[str, Any], ai_observation.AIObservationSet | None], None]:
    """全成功jobをsnapshot化して単一pointerへ公開し、任意の後処理を行うcallbackを返す。

    ``on_published``はworld lock取得後にCanonical generationを再確認してから同じlock内で呼ぶ。
    ESのdelete→create→bulkをrebind/Canonical公開と直列化し、待機中に世代が変わった旧callbackが
    新索引を削除して旧状態を復活させないためである。失敗またはstale時はjobをartifact公開済みにしない。
    snapshot/pointer自体は不変なため、現行世代ならidle時のself-repairが後処理を安全に再試行できる。
    """
    def publish(job: dict[str, Any], _current_set: ai_observation.AIObservationSet | None) -> None:
        world = str(job["world"])
        canonical_generation_id = str(job["canonical_generation_id"])
        snapshot = ocr_jobs.succeeded_results_snapshot(world, canonical_generation_id)

        def records():
            previous_source: str | None = None
            current_ir: evidence_ir.EvidenceIR | None = None
            for row in ocr_jobs.iter_succeeded_results(world, canonical_generation_id):
                source_rel = str(row["source_rel_path"])
                if source_rel != previous_source:
                    current_ir = load_ir(row)
                    previous_source = source_rel
                if current_ir is None:  # pragma: no cover - loop invariant
                    raise RuntimeError("OCR Evidence IR was not loaded")
                payload = row.get("result_payload")
                if not isinstance(payload, dict):
                    raise ValueError("succeeded OCR job has no Observation Set payload")
                observation_set = ai_observation.from_json_str(_canonical(payload), ir=current_ir)
                if observation_set.observation_set_hash != row.get("result_observation_set_hash"):
                    raise ValueError("OCR result hash does not match Observation Set payload")
                yield observation_render.ObservationRecord(
                    source_rel_path=source_rel,
                    ir=current_ir,
                    observation_set=observation_set,
                )

        result = observation_render.publish_snapshot_stream(
            resolve_derived_root(world),
            canonical_generation_id=canonical_generation_id,
            records=records(),
            canonical_is_current=lambda: canonical_is_current(world, canonical_generation_id),
            snapshot_is_current=lambda: (
                ocr_jobs.succeeded_results_snapshot(world, canonical_generation_id) == snapshot
            ),
            publish_guard=lambda: world_lock(world),
        )
        if result.get("status") == "published":
            with world_lock(world):
                # observation pointer公開後、lock待ちの間にrebind/Canonical generation切替が完了し得る。
                # stale callbackはESへ一切触れず、published markerも残さない。
                if not canonical_is_current(world, canonical_generation_id):
                    return
                if on_published is not None:
                    on_published(world, canonical_generation_id)
                garbage_collect_observation_generations(
                    resolve_derived_root(world),
                    active_canonical_generation_id=canonical_generation_id,
                )
                ocr_jobs.mark_snapshot_artifacts_published(world, canonical_generation_id, snapshot)
    return publish


def _safe_world_file(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    relative_path = Path(relative)
    if relative_path.is_absolute() or any(part in {"", ".", ".."} for part in relative_path.parts):
        raise OCRBindingError("world source path must be relative")
    candidate = resolved_root.joinpath(*relative_path.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise OCRBindingError("world source path escaped root") from exc
    current = candidate
    while current != resolved_root:
        if current.is_symlink():
            raise OCRBindingError("world source path contains symlink")
        if resolved_root not in current.parents:
            raise OCRBindingError("world source path escaped root")
        current = current.parent
    if not resolved.is_file():
        raise OCRBindingError("world source path is not a regular non-symlink file")
    return resolved


def _runtime_callbacks():
    """製品worker CLI用のWorld/Canonical resolver。絶対pathをjob payloadへ保存しない。"""
    from . import derived_generation
    from .. import worlds

    # Direct CLI起動でもstart.shのmount検査に依存しない。DB障害を空registryへ縮退せず、
    # writable observation rootがCanonicalまたは全登録World sourceと重なる場合、または明示した
    # read-only source rootが全登録Worldを包含しない場合は起動を拒否する。
    worlds.observation_dir("ocr-worker-validation", validate_registered=True)
    allowed_ocr_root = worlds.validate_ocr_registered_sources()

    def current(world: str, generation_id: str) -> bool:
        return derived_generation.active_generation_id(worlds.derived_dir(world)) == generation_id

    def active_ir_root(job: dict[str, Any]) -> Path:
        derived_root = worlds.derived_dir(job["world"])
        if derived_generation.active_generation_id(derived_root) != job["canonical_generation_id"]:
            raise OCRBindingError("Canonical generation is no longer active")
        return derived_generation.active_ir_dir(derived_root)

    def active_rag_root(job: dict[str, Any]) -> Path:
        derived_root = worlds.derived_dir(job["world"])
        if derived_generation.active_generation_id(derived_root) != job["canonical_generation_id"]:
            raise OCRBindingError("Canonical generation is no longer active")
        return derived_generation.active_rag_dir(derived_root)

    def generation_root(world: str, generation_id: str) -> Path:
        # `.ocr_route.json`/`.evidence.json` はいずれも ir 層（§8.1 三階層）に同居する。
        derived_root = worlds.derived_dir(world)
        if derived_generation.active_generation_id(derived_root) != generation_id:
            raise OCRBindingError("Canonical generation is no longer active")
        return derived_generation.active_ir_dir(derived_root)

    def load_ir(job: dict[str, Any]) -> evidence_ir.EvidenceIR:
        path = active_ir_root(job).joinpath(*Path(job["source_rel_path"] + ".evidence.json").parts)
        return evidence_ir.from_json_str(path.read_text(encoding="utf-8"))

    def resolve_source(job: dict[str, Any]) -> Path:
        root = worlds.world_dir(job["world"])
        if root is None:
            raise OCRBindingError("World source is unavailable")
        # 起動後のregister/rebindやDB job改変に対しても、各jobの実読込直前に再検証する。
        try:
            worlds.validate_ocr_source_root(root, allowed_root=allowed_ocr_root)
        except ValueError as exc:
            raise OCRBindingError("World source is outside the configured OCR root") from exc
        return _safe_world_file(Path(root), job["source_rel_path"])

    def resolve_assets(job: dict[str, Any]) -> Path:
        return active_rag_root(job).joinpath(*Path(job["source_rel_path"] + ".assets").parts)

    def reindex_observations(world: str, canonical_generation_id: str) -> None:
        # Canonical全再索引は行わない。世代の整合性だけを公開直後に再確認する。
        if not current(world, canonical_generation_id):
            raise OCRBindingError("Canonical generation changed before observation reindex")
        # ocr_worker は ES・human_md マーカーの読み書きを一切行わない（隔離 profile では
        # `/derived` が read-only・ES 自体も到達不可のネットワーク構成のため、ここで
        # `es_index`/`.human_md_es_sig` に触れると通常の OCR 公開のたびに必ず失敗する）。
        # O1（2026-09-03）以降、OCR 観測は VLM と合流して rag.md（正本）へ「AI観測」レコードとして
        # 統合される（`office_md._build_observation_set`）——grep の観測専用ツリー直接走査は撤去済み
        # （`grep_tool.grep_search` 参照）。この合流と ES 反映は、この隔離プロセスではなく通常の
        # sync（`worker._refresh_derived_representations` の `.rag_sig` OCR 観測次元＝
        # `office_md.rag_sig_drift`）が次回呼び出し時に「追いつき」として行う。ここでは Canonical
        # 世代の整合性だけを公開直後に再確認し、それ以上は何もしない。

    publisher = build_standard_publish_callback(
        resolve_derived_root=lambda world: Path(worlds.observation_dir(world)),
        canonical_is_current=current,
        load_ir=load_ir,
        on_published=reindex_observations,
    )
    return current, load_ir, resolve_source, resolve_assets, generation_root, publisher


def main(argv: list[str] | None = None) -> int:
    """隔離Compose profileから起動する1-concurrency worker loop。"""
    import argparse

    parser = argparse.ArgumentParser(description="Sherpa offline PaddleOCR observation worker")
    parser.add_argument("--once", action="store_true", help="1件またはidleを処理して終了")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}:{os.getpid()}")
    parser.add_argument("--paddle-cache", type=Path)
    parser.add_argument("--lease-seconds", type=int, default=90)
    parser.add_argument("--heartbeat-seconds", type=float, default=15.0)
    parser.add_argument("--inference-timeout-seconds", type=float, default=PADDLE_CPU_PROFILE.max_input_seconds)
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    if args.lease_seconds <= 0 or not 0 < args.heartbeat_seconds < args.lease_seconds:
        parser.error("--heartbeat-seconds must be positive and shorter than --lease-seconds")
    if not 0 < args.inference_timeout_seconds <= PADDLE_CPU_PROFILE.max_input_seconds:
        parser.error("--inference-timeout-seconds exceeds the fixed profile limit")
    availability = paddle_availability(args.paddle_cache)
    stopping = Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stopping.set()

    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    previous_sigint = signal.signal(signal.SIGINT, request_stop)

    def heartbeat(status: str) -> None:
        ocr_jobs.record_worker_heartbeat(
            args.worker_id,
            engine_profile_hash=availability.engine_profile_hash,
            available=availability.available,
            unavailable_reason=availability.unavailable_reason,
            model_hashes_valid=availability.model_hashes_valid,
            status=status,
            metadata={
                "paddleocr_version": availability.paddleocr_version,
                "paddlepaddle_version": availability.paddlepaddle_version,
                "pypdfium2_version": availability.pypdfium2_version,
                "pillow_version": availability.pillow_version,
                "model_hashes": availability.model_hashes,
                "cache_home": availability.cache_home,
            },
        )

    heartbeat("starting" if availability.available else "unavailable")
    if not availability.available:
        print(_canonical(asdict(availability)), flush=True)
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
        return 2
    engine = PaddleProcessSupervisor(args.paddle_cache)
    current, load_ir, resolve_source, resolve_assets, generation_root, publisher = _runtime_callbacks()
    try:
        while not stopping.is_set():
            result = run_once(
                args.worker_id, engine=engine, canonical_is_current=current, load_ir=load_ir,
                resolve_source=resolve_source, resolve_asset_root=resolve_assets, publish_observation=publisher,
                lease_seconds=args.lease_seconds, lease_renew_interval_seconds=args.heartbeat_seconds,
                inference_timeout_seconds=args.inference_timeout_seconds, heartbeat=heartbeat,
                should_stop=stopping.is_set,
            )
            if not stopping.is_set():
                heartbeat("idle")
            print(_canonical(asdict(result)), flush=True)
            if result.status != "idle" and args.once:
                return 0 if result.status not in {"failed", "publish_failed"} else 1
            if result.status == "idle":
                refresh_result = run_refresh_once(
                    args.worker_id,
                    engine_profile_hash=engine.engine_profile_hash,
                    canonical_is_current=current,
                    resolve_generation_root=generation_root,
                    lease_seconds=args.lease_seconds,
                    lease_renew_interval_seconds=args.heartbeat_seconds,
                    heartbeat=heartbeat,
                    should_stop=stopping.is_set,
                )
                if refresh_result.status != "idle":
                    if not stopping.is_set():
                        heartbeat("idle")
                    print(_canonical(asdict(refresh_result)), flush=True)
                    if args.once:
                        return 0 if refresh_result.status != "failed" else 1
                    continue
                if args.once:
                    return 0
                # OCR自体は成功したがpointer公開だけ失敗した場合やworker再起動後も自己修復する。
                for candidate in ocr_jobs.list_unpublished_generations():
                    if current(candidate["world"], candidate["canonical_generation_id"]):
                        publisher(candidate, None)
                        break
                stopping.wait(args.poll_seconds)
        return 0
    finally:
        engine.close()
        try:
            heartbeat("stopping")
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
            signal.signal(signal.SIGINT, previous_sigint)


if __name__ == "__main__":
    raise SystemExit(main())
