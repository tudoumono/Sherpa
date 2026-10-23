"""MD化の重い純粋計算（アーム変換・Evidence 抽出）をファイル単位で別プロセスへ先読みする。

集計とファイル書込は親の直列ループのまま、子は変換結果を返すだけ（DB・system_settings・ネットワークは使わない）。
先読みの対象・窓の広さ・失敗時に親の直列経路へ戻す扱いはこのモジュールが持つ。
設計: docs/design/rag.md「人向け MD と RAG 正本の作り分け（マージの実際）」
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import shutil
import sys
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

from ..env_int import env_int
from . import child_registry, mem_guard, resource_meter

_log = logging.getLogger("sherpa.ingest.convert")   # 変換ログ（make logs convert）と同じ名前

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


def plan_threads() -> int:
    """先読みの対象の確認（キャッシュ照合・入口ガード・重さ）を並列にするスレッド数。env `SHERPA_MD_PLAN_THREADS`（1〜32・1 で直列）。未設定・不正値は 8。"""
    return env_int("SHERPA_MD_PLAN_THREADS", 8, 1, 32)


@dataclass(frozen=True)
class Job:
    """先読み 1 件（原本 rel・原本の絶対パス・担当アーム名・拡張子）。

    `render`＝子で rag.md の描画まで行う（親が判定済み。VLM の補足観測が起きない・xlsx の Office 表示補完が無効のとき）。
    `assets_dir`＝子が素材を書く `dr_rag/(rel+".assets")`、`obs_dir`＝公開済み OCR 観測の置き場（親が解決済み）。
    `size`＝計画で測った原本のサイズ（大きいものを先に出す並びに使う。None なら `Prefetcher` が測る）。
    """
    rel: str
    path: str
    arm_name: str
    ext: str
    render: bool = False
    assets_dir: str | None = None
    obs_dir: str | None = None
    size: int | None = None


@dataclass
class Rendered:
    """子が描画まで済ませた結果。親は中身をそのままファイルへ書く（素材は子が `assets_dir` へ書き済み）。"""
    evidence_gz: bytes
    markdown: str
    chunks_gz: bytes
    figure_drops: dict
    assets_dir: str
    context_gz: bytes


@dataclass
class Prefetched:
    """子の計算結果。`result.document` は常に None（実体は `evidence` に畳んで返す）。

    `has_document`＝アームが Document IR を作れたか。`partial`＝`_check_partial_extraction` の追記分。
    `evidence`＝Evidence を作る拡張子のときだけ（xlsx は `excel_display` 適用済み）。描画まで済んだとき（`rendered`）は None。
    `max_rss_kb`＝子プロセスのここまでの最大メモリ（KB）。
    `source_stat`＝子が原本を読む直前の `source_stat()`。`guard_ok`＝子での入口ガードの再判定が通ったか（False なら変換していない）。
    """
    arm_name: str
    result: object
    has_document: bool
    partial: list[dict]
    evidence: object | None
    source_stat: tuple | None = None
    guard_ok: bool = True
    rendered: Rendered | None = None
    max_rss_kb: int = 0


def source_stat(path) -> tuple | None:
    """原本の (st_size, st_mtime_ns, st_ctime_ns)。変換キャッシュのキーと同じ材料。取れなければ None。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _max_rss_kb() -> int:
    """この子プロセスの最大 RSS（KB）。取れなければ 0。"""
    try:
        import resource
        rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return rss // 1024 if sys.platform == "darwin" else rss   # macOS はバイト・Linux は KB
    except Exception:
        return 0


def compute(path_str: str, arm_name: str, ext: str, rel: str, render: bool = False,
            assets_dir: str | None = None, obs_dir: str | None = None) -> Prefetched:
    """子プロセスで動く。結果に子の最大メモリを載せる（`_compute`）。"""
    got = _compute(path_str, arm_name, ext, rel, render, assets_dir, obs_dir)
    got.max_rss_kb = _max_rss_kb()
    return got


def _compute(path_str: str, arm_name: str, ext: str, rel: str, render: bool = False,
             assets_dir: str | None = None, obs_dir: str | None = None) -> Prefetched:
    """子プロセスで動く。① 原本の stat を取り入口ガードを判定し直す ② アームで変換 ③ 消費前の Document IR から部分抽出の疑いを作る
    ④ Evidence を抽出する（pdf_text は人向け MD を組むため②の直後に抽出し `result.md` を差し替える）⑤ `render` のときは素材の書出し・描画まで行い、親が書く中身を返す（失敗したら素材を消して Evidence までで返す）。"""
    from . import evidence_spike, excel_display, office_md
    from .arms import ooxml_arm, pdf_text_arm

    arm = {"ooxml": ooxml_arm.OoxmlArm, "pdf_text": pdf_text_arm.PdfTextArm}[arm_name]()
    path = Path(path_str)
    stat = source_stat(path)
    if office_md._precheck_target(path, ext, {ext})[0] != "ok":      # ガードに掛かる原本は子で開かない
        return Prefetched(arm_name, None, False, [], None, stat, guard_ok=False)
    result = arm.convert(path)
    if result is None:
        return Prefetched(arm_name, None, False, [], None, stat)
    document = result.document
    result.document = None
    if result.md is None:
        return Prefetched(arm_name, result, document is not None, [], None, stat)
    evidence = None
    if ext == ".pdf" and arm_name == "pdf_text":                     # 人向け MD は Evidence から組むので、先に抽出して差し替える
        evidence = evidence_spike.extract(path)
        office_md._apply_pdf_human_md(result, evidence, path)
    partial: list[dict] = []
    office_md._check_partial_extraction(path, result.md, rel, document, partial)
    if ext in ooxml_arm._IR_EXTS and document is not None:
        document.doc_id = rel
        document.source.path = rel
    if evidence is None and ext in office_md.EVIDENCE_EXT and office_md._evidence_arm_selected(ext, arm_name):
        evidence = evidence_spike.extract(path, legacy_ir=document, consume_legacy=document is not None)
        if path.suffix.lower() == ".xlsx":
            excel_display.enrich_evidence(evidence, path)
    rendered = None
    if render and evidence is not None and assets_dir is not None:
        try:
            rendered = office_md._render_evidence_for_prefetch(
                path, rel, evidence, Path(assets_dir), Path(obs_dir) if obs_dir else None)
        except Exception:
            _log.warning("MD化の先読みでの描画に失敗しました（親が計算し直します）: %s", rel, exc_info=True)
            shutil.rmtree(assets_dir, ignore_errors=True)
    if rendered is not None:
        evidence = None
    return Prefetched(arm_name, result, document is not None, partial, evidence, stat,
                      rendered=rendered)


class Prefetcher:
    """原本の大きい上位 ワーカー数 件を最初に提出し、残りは `jobs` の順に提出する。`take(rel)` は親のループ順（`jobs` の順）に結果を渡す。

    先に出した大きいものを除く提出済みで未取得（計算中＋完了待ち）は ワーカー数×2 件まで（未取得の合計は ワーカー数×3 件まで）。
    `take` した rel が未提出なら窓に関係なく先に提出する（超過は 1 件まで）。
    子の例外・プロセス死・想定外の形は `take` が None を返す＝呼び出し側が直列経路で計算し直す。
    子に渡す前に空きメモリを確かめる。下限を下回る間は最初の提出で短く待ち、最大まで待っても空かなければ同時に走らせる子を 1 つ減らして進める。`take` の途中では待たず、その回は提出しない（親が待たされない）。
    """

    def __init__(self, jobs: list[Job], workers: int):
        self._order = {j.rel: i for i, j in enumerate(jobs)}
        sizes = {j.rel: j.size if j.size is not None else (source_stat(j.path) or (0,))[0] for j in jobs}
        largest = sorted(jobs, key=lambda j: (-sizes[j.rel], j.rel))[:workers]
        self._early = {j.rel for j in largest}               # 最初に提出する大きいもの（窓の外に数える）
        self._queue: deque[Job] = deque(largest + [j for j in jobs if j.rel not in self._early])
        self._inflight: dict[str, tuple[Job, Future]] = {}
        self._known = {j.rel for j in jobs}
        self._window = workers * _WINDOW_PER_WORKER
        self._planned = len(jobs)
        self._workers = workers
        self._parallel = workers                           # 同時に走らせてよい子の数（空きメモリが戻らないと減らす）
        self.used = 0
        self.rendered = 0
        self.max_rss_kb = 0                                # 子の最大メモリ（全子の最大値）
        self._leftover_dirs: list[str] = []                # 使われなかった子が書いた素材（終了時に消す）
        self._unclaimed: dict[str, str] = {}               # 結果を取り出したが親がまだ描画結果を使っていない rel の素材
        self._closed = False
        self._pool: ProcessPoolExecutor | None = ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
            initializer=child_registry.guard_parent, initargs=(os.getpid(),))
        self._pump(wait=True)

    def __enter__(self) -> "Prefetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _submit(self, job: Job) -> bool:
        try:
            fut = self._pool.submit(compute, job.path, job.arm_name, job.ext, job.rel,
                                    job.render, job.assets_dir, job.obs_dir)
        except Exception:
            _log.warning("MD化の先読みを提出できませんでした（直列で処理します）", exc_info=True)
            self._queue.clear()
            return False
        self._inflight[job.rel] = (job, fut)
        return True

    def _memory_ready(self, wait: bool) -> bool:
        """子に渡してよいか。空きが下限未満なら、`wait` のとき待つ（最大まで待っても空かなければ並列を 1 つ減らして True）、待たないときは False。並列 1 では待たない。"""
        if self._parallel <= 1 or mem_guard.memory_ok():
            return True
        if not wait:
            return False
        with resource_meter.waiting(resource_meter.RESOURCE, task="md_prefetch_memory"):
            freed = mem_guard.wait_for_free_memory("MD化の先読み")
        if not freed:
            self._parallel -= 1
            _log.warning("メモリの空きが戻らないため、MD化の先読みの並列を %d に減らして進めます", self._parallel)
        return True

    def _running(self) -> int:
        return sum(1 for _job, f in self._inflight.values() if not f.done())

    def _pump(self, wait: bool = False) -> None:
        while self._pool is not None and self._queue:
            job = self._queue[0]
            if job.rel not in self._early and self._windowed_inflight() >= self._window:
                return
            if self._parallel < self._workers and self._running() >= self._parallel:
                return
            if not self._memory_ready(wait):
                return
            self._queue.popleft()
            if not self._submit(job):
                return

    def _windowed_inflight(self) -> int:
        """窓に数える未取得の件数（最初に出した大きいものを除く）。"""
        return sum(1 for r in self._inflight if r not in self._early)

    def _drop_before(self, rel: str) -> None:
        """ループ順で `rel` より前の未取得分（ループが飛ばしたファイル）を捨てる。"""
        limit = self._order[rel]
        for r in [r for r in self._inflight if self._order[r] < limit]:
            skipped, stale = self._inflight.pop(r)
            stale.cancel()
            self._known.discard(r)
            if skipped.assets_dir:
                self._leftover_dirs.append(skipped.assets_dir)
        while self._queue and self._order[self._queue[0].rel] < limit:   # 未提出の列は名前順（大きいものは最初に提出済み）
            self._known.discard(self._queue.popleft().rel)

    def take(self, rel: str, path) -> Prefetched | None:
        """`rel` の先読み結果。対象外・失敗・子のガード不通過・子が読んだ後に原本が変わったものは None。

        ループ順で `rel` より前の未取得分（ループが飛ばしたファイル）は捨てる。
        """
        if rel not in self._known:
            return None
        self._drop_before(rel)
        if rel not in self._inflight:
            queued = self._queue.popleft() if self._queue and self._queue[0].rel == rel else None
            if queued is None or self._pool is None or not self._submit(queued):
                self._known.discard(rel)
                return None
        job, fut = self._inflight.pop(rel)
        self._known.discard(rel)
        self._pump()
        try:
            if fut.done():
                got = fut.result()
            else:
                with resource_meter.waiting(resource_meter.WORKING, task="md_prefetch_result"):
                    got = fut.result()
        except Exception as exc:
            _log.warning("MD化の先読みに失敗しました（直列で計算し直します）: %s", exc.__class__.__name__)
            if isinstance(exc, BrokenProcessPool):
                self._stop_broken_pool()
            self._discard_assets(job)
            return None
        if not isinstance(got, Prefetched) or got.arm_name != job.arm_name:
            _log.warning("MD化の先読み結果の形が想定外でした（直列で計算し直します）")
            self._discard_assets(job)
            return None
        self.max_rss_kb = max(self.max_rss_kb, got.max_rss_kb)
        if not got.guard_ok or got.source_stat is None or got.source_stat != source_stat(path):
            self._discard_assets(job)
            return None                                   # 子が読んだ後に原本が変わった＝古い中身を新しいキャッシュキーで残さない
        self.used += 1
        if got.rendered is not None:
            self.rendered += 1
            self._unclaimed[rel] = got.rendered.assets_dir
        return got

    def claim(self, rel: str) -> None:
        """親が `rel` の描画結果を使う（素材をそのまま残す）。使わなかった分は `close` が消す。"""
        self._unclaimed.pop(rel, None)

    @staticmethod
    def _discard_assets(job: Job) -> None:
        """結果を使わない rel の素材を消す（親が計算し直す前に、子の途中までの書込みを残さない）。"""
        if job.assets_dir:
            shutil.rmtree(job.assets_dir, ignore_errors=True)

    def _stop_broken_pool(self) -> None:
        """壊れたプールの子が全て止まるのを待ってから、未取得分の素材を消す（以降は全件を親が直列で計算する）。"""
        pool, self._pool = self._pool, None
        self._queue.clear()
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        for job, _f in self._inflight.values():
            self._discard_assets(job)
        self._inflight.clear()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pool, self._pool = self._pool, None
        self._queue.clear()
        self._leftover_dirs.extend(j.assets_dir for j, _f in self._inflight.values() if j.assets_dir)
        self._inflight.clear()
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        for leftover in (*self._leftover_dirs, *self._unclaimed.values()):
            shutil.rmtree(leftover, ignore_errors=True)
        self._leftover_dirs.clear()
        self._unclaimed.clear()
        _log.info("MD化の先読みを使いました: %d/%d件（うち描画まで子で済んだ件数 %d）・子の最大メモリ %dMB",
                  self.used, self._planned, self.rendered, self.max_rss_kb // 1024)
