"""MD化の重い純粋計算（アーム変換・Evidence 抽出）をファイル単位で別プロセスへ先読みする。

集計とファイル書込は親の直列ループのまま、子は変換結果を返すだけ（DB・system_settings・ネットワークは使わない）。
先読みの対象・窓の広さ・失敗時に親の直列経路へ戻す扱いはこのモジュールが持つ。
設計: docs/design/rag.md「人向け MD と RAG 正本の作り分け（マージの実際）」
"""
from __future__ import annotations

import logging
import multiprocessing
import os
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

PREFETCH_ARMS = ("ooxml", "pdf_text")
_MAX_WORKERS = 8
_WINDOW_PER_WORKER = 2


def worker_count() -> int:
    """先読みのワーカー数。env `SHERPA_MD_WORKERS`（1〜8）。未設定・不正値は `min(4, max(1, CPU 数 - 1))`。"""
    default = min(4, max(1, (os.cpu_count() or 1) - 1))
    raw = os.environ.get("SHERPA_MD_WORKERS", "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    return n if 1 <= n <= _MAX_WORKERS else default


@dataclass(frozen=True)
class Job:
    """先読み 1 件（原本 rel・原本の絶対パス・担当アーム名・拡張子）。"""
    rel: str
    path: str
    arm_name: str
    ext: str


@dataclass
class Prefetched:
    """子の計算結果。`result.document` は常に None（実体は `document_json`／`evidence` に畳んで返す）。

    `has_document`＝アームが Document IR を作れたか。`partial`＝`_check_partial_extraction` の追記分。
    `evidence`＝Evidence を作る拡張子のときだけ（xlsx は `excel_display` 適用済み）。
    """
    arm_name: str
    result: object
    has_document: bool
    document_json: str | None
    partial: list[dict]
    evidence: object | None


def compute(path_str: str, arm_name: str, ext: str, rel: str) -> Prefetched:
    """子プロセスで動く。① アームで変換 ② 消費前の Document IR から document.json・部分抽出の疑いを作る ③ Evidence を抽出する。"""
    from . import document_ir, evidence_spike, excel_display, office_md
    from .arms import ooxml_arm, pdf_text_arm

    arm = {"ooxml": ooxml_arm.OoxmlArm, "pdf_text": pdf_text_arm.PdfTextArm}[arm_name]()
    path = Path(path_str)
    result = arm.convert(path)
    if result is None:
        return Prefetched(arm_name, None, False, None, [], None)
    document = result.document
    result.document = None
    if result.md is None:
        return Prefetched(arm_name, result, document is not None, None, [], None)
    partial: list[dict] = []
    office_md._check_partial_extraction(path, result.md, rel, document, partial)
    document_json = None
    if ext in ooxml_arm._IR_EXTS and document is not None:
        document.doc_id = rel
        document.source.path = rel
        document_json = document_ir.to_json_str(document)
    evidence = None
    if ext in office_md.EVIDENCE_EXT and office_md._evidence_arm_selected(ext, arm_name):
        evidence = evidence_spike.extract(path, legacy_ir=document, consume_legacy=document is not None)
        if path.suffix.lower() == ".xlsx":
            excel_display.enrich_evidence(evidence, path)
    return Prefetched(arm_name, result, document is not None, document_json, partial, evidence)


class Prefetcher:
    """`jobs` の順に先読みし、`take(rel)` で同じ順に結果を渡す。未取得（提出済み＋完了待ち）は ワーカー数×2 件まで。

    子の例外・プロセス死・想定外の形は `take` が None を返す＝呼び出し側が直列経路で計算し直す。
    """

    def __init__(self, jobs: list[Job], workers: int):
        self._queue: deque[Job] = deque(jobs)
        self._inflight: deque[tuple[Job, Future]] = deque()
        self._known = {j.rel for j in jobs}
        self._window = workers * _WINDOW_PER_WORKER
        self._pool: ProcessPoolExecutor | None = ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
        self._pump()

    def __enter__(self) -> "Prefetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _pump(self) -> None:
        while self._pool is not None and self._queue and len(self._inflight) < self._window:
            job = self._queue.popleft()
            try:
                fut = self._pool.submit(compute, job.path, job.arm_name, job.ext, job.rel)
            except Exception:
                _log.warning("MD化の先読みを提出できませんでした（直列で処理します）", exc_info=True)
                self._queue.clear()
                return
            self._inflight.append((job, fut))

    def take(self, rel: str) -> Prefetched | None:
        """`rel` の先読み結果。対象外・失敗は None。`rel` より前の未取得分（ループが飛ばしたファイル）は捨てる。"""
        if rel not in self._known:
            return None
        while self._inflight and self._inflight[0][0].rel != rel:
            skipped, stale = self._inflight.popleft()
            stale.cancel()
            self._known.discard(skipped.rel)
        if not self._inflight:
            while self._queue and self._queue[0].rel != rel:
                self._known.discard(self._queue.popleft().rel)
            self._pump()
        if not self._inflight or self._inflight[0][0].rel != rel:
            self._known.discard(rel)
            return None
        job, fut = self._inflight.popleft()
        self._known.discard(rel)
        self._pump()
        try:
            got = fut.result()
        except Exception as exc:
            _log.warning("MD化の先読みに失敗しました（直列で計算し直します）: %s", exc.__class__.__name__)
            return None
        if not isinstance(got, Prefetched) or got.arm_name != job.arm_name:
            _log.warning("MD化の先読み結果の形が想定外でした（直列で計算し直します）")
            return None
        return got

    def close(self) -> None:
        pool, self._pool = self._pool, None
        self._queue.clear()
        self._inflight.clear()
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
