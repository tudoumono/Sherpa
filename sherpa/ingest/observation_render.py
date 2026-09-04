"""隔離OCR workerが書くOCR補助観測Setを、Canonical RAG成果物とは別のgenerationへ永続化する。

O1（2026-09-03・§8.1一本化）で検索用途の描画（Markdown/chunkJSONL）は撤去した——OCR観測は
VLMと合流してrag.md（正本）へ「AI観測」レコードとして統合される経路（`office_md._build_observation_set`）
に一本化されており、grepもこの別木を直接走査しない（`grep_tool.grep_search`参照）。
このmoduleが今も持つのは、`office_md._load_ocr_observation_sets`がその合流のために読む
`{rel}.ai_observations.jsonl`（Observation Set本体）の永続化・世代管理・pointer切替だけである。
"""
from __future__ import annotations

import hashlib
import heapq
import json
import os
import shutil
import tempfile
import uuid
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, ContextManager, Iterable, TextIO

from . import ai_observation, evidence_ir


OBSERVATION_RENDERER_VERSION = "ocr-observation-renderer-v1"
OBSERVATION_GENERATIONS_NAME = "md-observation-generations"
OBSERVATION_POINTER_NAME = "md-observations.current.json"
OBSERVATION_POINTER_SCHEMA = "sherpa-observation-pointer-v1"
OBSERVATION_GENERATION_MANIFEST = ".observation-generation.json"
OBSERVATION_GENERATION_SCHEMA = "sherpa-observation-generation-v1"

# immutable generationを毎文書のES row生成で全bytes再hashしないための検証cache。
# keyのgenerationに対し、全artifactのpath/size/mtime/ctime/inode台帳が同一な間だけ再利用する。
_VERIFIED_ARTIFACT_LEDGERS: dict[tuple[str, str], str] = {}
_VERIFICATION_CACHE_LIMIT = 64
_ARTIFACT_SORT_CHUNK_SIZE = 2048
_ARTIFACT_SORT_MERGE_FAN_IN = 32


@dataclass(frozen=True)
class ObservationArtifactPaths:
    generation_root: Path
    observation_sets_jsonl: Path


@dataclass(frozen=True)
class ObservationDocument:
    source_rel_path: str
    ir: evidence_ir.EvidenceIR
    observation_sets: list[ai_observation.AIObservationSet]


@dataclass(frozen=True)
class ObservationRecord:
    """DBから1行ずつ渡せる、1資料・1 OCR routeの有界な公開単位。"""

    source_rel_path: str
    ir: evidence_ir.EvidenceIR
    observation_set: ai_observation.AIObservationSet


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def observation_generation_id(observation_sets: list[ai_observation.AIObservationSet]) -> str:
    """文書単位の別観測generation ID。Set追加時は別directoryとなり既存成果物を不変に保つ。"""
    if not observation_sets:
        raise ValueError("at least one AI Observation Set is required")
    payload = {
        "renderer": OBSERVATION_RENDERER_VERSION,
        "canonical_generation_id": observation_sets[0].canonical_generation_id,
        "observation_set_hashes": sorted(item.observation_set_hash for item in observation_sets),
    }
    if any(item.canonical_generation_id != payload["canonical_generation_id"] for item in observation_sets):
        raise ValueError("mixed Canonical generations")
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def artifact_paths(
    derived_root: str | Path,
    *,
    canonical_generation_id: str,
    observation_generation_id: str,
    source_rel_path: str,
) -> ObservationArtifactPaths:
    """Canonical外の不変な別観測generationに置く、Observation Set本体のpath契約。"""
    for value in (canonical_generation_id, observation_generation_id):
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("invalid generation id")
    rel = PurePosixPath(source_rel_path.replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError("source_rel_path must be relative")
    generation_root = Path(derived_root) / OBSERVATION_GENERATIONS_NAME / canonical_generation_id / observation_generation_id
    base = generation_root.joinpath(*rel.parts)
    return ObservationArtifactPaths(
        generation_root=generation_root,
        observation_sets_jsonl=Path(str(base) + ".ai_observations.jsonl"),
    )


def _write_atomic(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def write_bundle_atomic(
    paths: ObservationArtifactPaths,
    observation_sets: list[ai_observation.AIObservationSet],
) -> ObservationArtifactPaths:
    """同じ別generation directoryへSet JSONLを原子書込する。"""
    ordered = sorted(observation_sets, key=lambda item: item.observation_set_hash)
    set_lines = "".join(ai_observation.to_json_str(item) for item in ordered)
    _write_atomic(paths.observation_sets_jsonl, set_lines)
    return paths


def _walk_tree_files(root: Path):
    """Walk files without retaining a directory or World-wide entry list."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("observation generation root must be a non-symlink directory")
    stack = [os.scandir(root)]
    try:
        while stack:
            try:
                entry = next(stack[-1])
            except StopIteration:
                stack.pop().close()
                continue
            if entry.is_symlink():
                raise ValueError("observation generation contains symlink")
            if entry.is_dir(follow_symlinks=False):
                stack.append(os.scandir(entry.path))
            elif entry.is_file(follow_symlinks=False):
                yield Path(entry.path)
    finally:
        for iterator in stack:
            iterator.close()


def _write_path_run(path: Path, values: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        for value in sorted(values):
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def _iter_path_run(stream: TextIO):
    for line in stream:
        value = json.loads(line)
        if not isinstance(value, str):  # pragma: no cover - private run writer invariant
            raise ValueError("invalid observation artifact sort run")
        yield value


def _merge_path_runs(inputs: list[Path], output: Path) -> None:
    """Merge a fixed-size group; fan-in bounds open files and heap memory."""
    with ExitStack() as stack:
        streams = [stack.enter_context(path.open("r", encoding="utf-8")) for path in inputs]
        with output.open("w", encoding="utf-8", newline="") as target:
            for value in heapq.merge(*(_iter_path_run(stream) for stream in streams)):
                target.write(json.dumps(value, ensure_ascii=False) + "\n")


def _iter_tree_files_sorted(root: Path):
    """Yield files in stable relative-path order with bounded RAM.

    Small generations sort one fixed-size chunk in memory.  Larger generations
    spill sorted path runs to a private temporary directory and merge them with
    fixed fan-in.  This preserves the former global relative-path ordering (and
    therefore artifact digests) without ``sorted(root.rglob('*'))``.  Temporary
    data contains relative paths only and is removed when iteration ends.
    """
    # Spill beside the generation rather than into the OCR container's bounded
    # /tmp tmpfs.  The ``.staging-`` prefix makes concurrent observation GC
    # ignore the private merge workspace; cleanup remains automatic.
    with tempfile.TemporaryDirectory(
        prefix=".staging-artifact-sort-", dir=root.parent,
    ) as temporary_value:
        temporary = Path(temporary_value)
        chunk: list[str] = []
        run_count = 0
        prefix = "pass-0000"
        for path in _walk_tree_files(root):
            if len(chunk) == _ARTIFACT_SORT_CHUNK_SIZE:
                _write_path_run(temporary / f"{prefix}-{run_count:012d}.jsonl", chunk)
                run_count += 1
                chunk = []
            chunk.append(path.relative_to(root).as_posix())

        if run_count == 0:
            for relative in sorted(chunk):
                yield root.joinpath(*PurePosixPath(relative).parts)
            return
        if chunk:
            _write_path_run(temporary / f"{prefix}-{run_count:012d}.jsonl", chunk)
            run_count += 1

        pass_index = 1
        while run_count > 1:
            next_prefix = f"pass-{pass_index:04d}"
            next_count = 0
            for start in range(0, run_count, _ARTIFACT_SORT_MERGE_FAN_IN):
                stop = min(start + _ARTIFACT_SORT_MERGE_FAN_IN, run_count)
                inputs = [temporary / f"{prefix}-{index:012d}.jsonl" for index in range(start, stop)]
                output = temporary / f"{next_prefix}-{next_count:012d}.jsonl"
                _merge_path_runs(inputs, output)
                for path in inputs:
                    path.unlink()
                next_count += 1
            prefix = next_prefix
            run_count = next_count
            pass_index += 1

        final = temporary / f"{prefix}-000000000000.jsonl"
        with final.open("r", encoding="utf-8") as stream:
            for relative in _iter_path_run(stream):
                yield root.joinpath(*PurePosixPath(relative).parts)


def _artifact_digest(root: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    count = total_bytes = 0
    for path in _iter_tree_files_sorted(root):
        if path.is_symlink():
            raise ValueError("observation generation contains symlink")
        if not path.is_file() or path.name == OBSERVATION_GENERATION_MANIFEST:
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        count += 1
        total_bytes += size
    return digest.hexdigest(), count, total_bytes


def _artifact_stat_ledger(root: Path) -> tuple[str, int, int]:
    """再hash省略の可否だけを判定する軽量台帳。metadata変化時は必ず実bytes検証へ戻す。"""
    digest = hashlib.sha256()
    count = total_bytes = 0
    for path in _iter_tree_files_sorted(root):
        if path.is_symlink():
            raise ValueError("observation generation contains symlink")
        if not path.is_file() or path.name == OBSERVATION_GENERATION_MANIFEST:
            continue
        stat = path.stat()
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        for value in (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino):
            digest.update(int(value).to_bytes(16, "big", signed=False))
        count += 1
        total_bytes += stat.st_size
    return digest.hexdigest(), count, total_bytes


def _artifacts_match_manifest(root: Path, manifest: dict[str, Any]) -> bool:
    """artifact実体をmanifestへ照合する。安定台帳の検証済みcacheだけbytes再読を省略する。"""
    expected_digest = manifest.get("artifact_sha256")
    expected_count = manifest.get("artifact_count")
    expected_bytes = manifest.get("artifact_bytes")
    if (not isinstance(expected_digest, str) or len(expected_digest) != 64
            or not isinstance(expected_count, int) or expected_count < 0
            or not isinstance(expected_bytes, int) or expected_bytes < 0):
        return False
    try:
        ledger_before, count, total_bytes = _artifact_stat_ledger(root)
    except (OSError, ValueError):
        return False
    if (count, total_bytes) != (expected_count, expected_bytes):
        return False
    cache_key = (str(root.resolve()), expected_digest)
    if _VERIFIED_ARTIFACT_LEDGERS.get(cache_key) == ledger_before:
        return True
    try:
        actual_digest, actual_count, actual_bytes = _artifact_digest(root)
        ledger_after, stable_count, stable_bytes = _artifact_stat_ledger(root)
    except (OSError, ValueError):
        return False
    if ledger_before != ledger_after:  # 検証中に書き換わったgenerationは公開扱いにしない
        return False
    if ((actual_digest, actual_count, actual_bytes) != (expected_digest, expected_count, expected_bytes)
            or (stable_count, stable_bytes) != (expected_count, expected_bytes)):
        return False
    if len(_VERIFIED_ARTIFACT_LEDGERS) >= _VERIFICATION_CACHE_LIMIT:
        _VERIFIED_ARTIFACT_LEDGERS.pop(next(iter(_VERIFIED_ARTIFACT_LEDGERS)))
    _VERIFIED_ARTIFACT_LEDGERS[cache_key] = ledger_after
    return True


def _pointer_payload(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema") != OBSERVATION_POINTER_SCHEMA:
        return None
    return value


def _valid_generation_id(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def active_observation_dir(
    derived_root: str | Path,
    *,
    canonical_generation_id: str | None = None,
) -> Path | None:
    """pointerが指定Canonicalと一致する場合だけ、ESが読む唯一の別観測directoryを返す。"""
    root = Path(derived_root)
    pointer = _pointer_payload(root / OBSERVATION_POINTER_NAME)
    if pointer is None:
        return None
    if canonical_generation_id is not None and pointer.get("canonical_generation_id") != canonical_generation_id:
        return None
    canonical = pointer.get("canonical_generation_id")
    observation = pointer.get("observation_generation_id")
    if not _valid_generation_id(canonical) or not _valid_generation_id(observation):
        return None
    target = root / OBSERVATION_GENERATIONS_NAME / canonical / observation
    if target.is_symlink() or not target.is_dir():
        return None
    try:
        manifest = json.loads((target / OBSERVATION_GENERATION_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (not isinstance(manifest, dict) or manifest.get("schema") != OBSERVATION_GENERATION_SCHEMA
            or manifest.get("canonical_generation_id") != canonical
            or manifest.get("observation_generation_id") != observation
            or manifest.get("artifact_sha256") != pointer.get("artifact_sha256")
            or manifest.get("artifact_count") != pointer.get("artifact_count")
            or manifest.get("artifact_bytes") != pointer.get("artifact_bytes")
            or not _artifacts_match_manifest(target, manifest)):
        return None
    return target


def _relative_source_path(value: str) -> str:
    rel = PurePosixPath(value.replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError("source_rel_path must be relative")
    return rel.as_posix()


def _flush_file(stream: TextIO) -> None:
    stream.flush()
    os.fsync(stream.fileno())


class _StreamingObservationBundle:
    """1資料のSetを1件ずつstageの`.ai_observations.jsonl`へ書き、資料全体のlistを保持しない。"""

    def __init__(self, stage: Path, record: ObservationRecord, *, canonical_generation_id: str):
        self.source_rel_path = _relative_source_path(record.source_rel_path)
        self.canonical_generation_id = canonical_generation_id
        self.source_content_hash = record.ir.source.content_hash
        base = stage.joinpath(*PurePosixPath(self.source_rel_path).parts)
        base.parent.mkdir(parents=True, exist_ok=True)
        self.paths = ObservationArtifactPaths(
            generation_root=stage,
            observation_sets_jsonl=Path(str(base) + ".ai_observations.jsonl"),
        )
        self._sets = self.paths.observation_sets_jsonl.open("w", encoding="utf-8", newline="")
        self._previous_hash: str | None = None
        self._count = 0
        self._closed = False

    def add(self, record: ObservationRecord) -> None:
        source_rel_path = _relative_source_path(record.source_rel_path)
        if source_rel_path != self.source_rel_path:
            raise ValueError("observation record belongs to another document")
        observation_set = record.observation_set
        if (observation_set.canonical_generation_id != self.canonical_generation_id
                or observation_set.source_content_hash != self.source_content_hash
                or record.ir.source.content_hash != self.source_content_hash):
            raise ValueError("observation record binding changed within one document")
        if self._previous_hash is not None and observation_set.observation_set_hash <= self._previous_hash:
            raise ValueError("observation records must be unique and ordered by observation_set_hash")
        errors = ai_observation.validation_errors(observation_set, ir=record.ir)
        if errors:
            raise ValueError("invalid AI Observation Set for publication: " + ",".join(errors))
        self._sets.write(ai_observation.to_json_str(observation_set))
        self._previous_hash = observation_set.observation_set_hash
        self._count += 1

    def finish(self) -> None:
        if self._closed:
            return
        _flush_file(self._sets)
        self._sets.close()
        self._closed = True

    def abort(self) -> None:
        if self._closed:
            return
        try:
            self._sets.close()
        except OSError:
            pass
        self._closed = True


def publish_snapshot_stream(
    derived_root: str | Path,
    *,
    canonical_generation_id: str,
    records: Iterable[ObservationRecord],
    canonical_is_current: Callable[[], bool],
    snapshot_is_current: Callable[[], bool] | None = None,
    publish_guard: Callable[[], ContextManager[Any]] | None = None,
) -> dict[str, Any]:
    """順序済みSetを逐次書込みし、最後に小さいpointerだけを切り替える。

    呼出し側は``(source_rel_path, observation_set_hash)``順で渡す。保持するのは1 OCR job分の
    Setだけで、World全体や1資料全体の観測をlist化しない。
    """
    if len(canonical_generation_id) != 64 or any(ch not in "0123456789abcdef" for ch in canonical_generation_id):
        raise ValueError("invalid canonical generation id")
    if not canonical_is_current():
        return {"status": "stale", "canonical_generation_id": canonical_generation_id}
    root = Path(derived_root)
    parent = root / OBSERVATION_GENERATIONS_NAME / canonical_generation_id
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise ValueError("observation generation root must not be a symlink")
    stage = parent / f".staging-{os.getpid()}-{uuid.uuid4().hex}"
    stage.mkdir()
    bundle: _StreamingObservationBundle | None = None
    previous_source: str | None = None
    try:
        for record in records:
            source_rel_path = _relative_source_path(record.source_rel_path)
            if previous_source is not None and source_rel_path < previous_source:
                raise ValueError("observation records must be ordered by source_rel_path")
            if bundle is None or source_rel_path != previous_source:
                if bundle is not None:
                    bundle.finish()
                bundle = _StreamingObservationBundle(
                    stage, record, canonical_generation_id=canonical_generation_id,
                )
            bundle.add(record)
            previous_source = source_rel_path
        if bundle is not None:
            bundle.finish()
            bundle = None

        artifact_sha256, artifact_count, artifact_bytes = _artifact_digest(stage)
        observation_generation_id = hashlib.sha256(_canonical({
            "canonical_generation_id": canonical_generation_id,
            "artifact_sha256": artifact_sha256,
            "renderer": OBSERVATION_RENDERER_VERSION,
        }).encode("utf-8")).hexdigest()
        manifest = {
            "schema": OBSERVATION_GENERATION_SCHEMA,
            "canonical_generation_id": canonical_generation_id,
            "observation_generation_id": observation_generation_id,
            "artifact_sha256": artifact_sha256,
            "artifact_count": artifact_count,
            "artifact_bytes": artifact_bytes,
            "renderer": OBSERVATION_RENDERER_VERSION,
        }
        _write_atomic(stage / OBSERVATION_GENERATION_MANIFEST, _canonical(manifest) + "\n")
        guard = publish_guard() if publish_guard is not None else nullcontext()
        with guard:
            if not canonical_is_current():
                shutil.rmtree(stage, ignore_errors=True)
                return {"status": "stale", "canonical_generation_id": canonical_generation_id}
            if snapshot_is_current is not None and not snapshot_is_current():
                shutil.rmtree(stage, ignore_errors=True)
                return {"status": "superseded", "canonical_generation_id": canonical_generation_id}
            target = parent / observation_generation_id
            if target.exists():
                existing_digest, existing_count, existing_bytes = _artifact_digest(target)
                if (existing_digest, existing_count, existing_bytes) != (artifact_sha256, artifact_count, artifact_bytes):
                    raise ValueError("existing observation generation content mismatch")
                shutil.rmtree(stage, ignore_errors=True)
            else:
                os.replace(stage, target)
            previous = _pointer_payload(root / OBSERVATION_POINTER_NAME)
            pointer = {
                "schema": OBSERVATION_POINTER_SCHEMA,
                "canonical_generation_id": canonical_generation_id,
                "observation_generation_id": observation_generation_id,
                "previous_observation_generation_id": (
                    previous.get("observation_generation_id") if isinstance(previous, dict) else None
                ),
                "artifact_sha256": artifact_sha256,
                "artifact_count": artifact_count,
                "artifact_bytes": artifact_bytes,
            }
            _write_atomic(root / OBSERVATION_POINTER_NAME, _canonical(pointer) + "\n")
        return {"status": "published", **pointer}
    except BaseException:
        if bundle is not None:
            bundle.abort()
        shutil.rmtree(stage, ignore_errors=True)
        raise


def publish_snapshot(
    derived_root: str | Path,
    *,
    canonical_generation_id: str,
    documents: list[ObservationDocument],
    canonical_is_current: Callable[[], bool],
    snapshot_is_current: Callable[[], bool] | None = None,
    publish_guard: Callable[[], ContextManager[Any]] | None = None,
) -> dict[str, Any]:
    """従来の文書list API。製品workerは``publish_snapshot_stream``を使用する。"""

    def records() -> Iterable[ObservationRecord]:
        previous_source: str | None = None
        for document in sorted(documents, key=lambda item: _relative_source_path(item.source_rel_path)):
            source_rel_path = _relative_source_path(document.source_rel_path)
            if source_rel_path == previous_source:
                raise ValueError("duplicate observation document path")
            previous_source = source_rel_path
            if not document.observation_sets:
                raise ValueError("at least one AI Observation Set is required")
            for observation_set in sorted(document.observation_sets, key=lambda item: item.observation_set_hash):
                yield ObservationRecord(
                    source_rel_path=source_rel_path,
                    ir=document.ir,
                    observation_set=observation_set,
                )

    return publish_snapshot_stream(
        derived_root,
        canonical_generation_id=canonical_generation_id,
        records=records(),
        canonical_is_current=canonical_is_current,
        snapshot_is_current=snapshot_is_current,
        publish_guard=publish_guard,
    )
