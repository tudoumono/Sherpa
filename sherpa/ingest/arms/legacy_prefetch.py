"""旧形式（.doc/.xls/.ppt）の先変換。MD 化の本体ループが旧形式へ着く前に、別スレッドから soffice をまとめて・並列に起動して OOXML への変換を済ませ、旧形式キャッシュ（`legacy_convert.ensure_ooxml` と同じ置き場・同じ鍵）へ入れておく。

本体ループは今まで通り `ensure_ooxml` を呼び、キャッシュに当たって先へ進む。先変換が失敗・タイムアウト・出力欠落したものはキャッシュへ入れず放置する（本体ループが 1 件ずつ変換し、原因の資料の失敗の理由・ログもそこで出る）。バッチ全体の失敗・時間切れ・異常終了・メモリの上限超えは、バッチの件数と理由を警告 1 行で残す。
バッチを始める前に空きメモリを確かめ、下限を下回る間は短く待つ。最大まで待っても空かなければ並列を 1 つ減らして進める（本体ループの 1 件変換は待たない）。
対象は libreoffice バックエンドだけ。1 バッチ＝同じ拡張子の最大 `SHERPA_LEGACY_BATCH` 件を 1 回の soffice 起動で変換し、`SHERPA_LEGACY_WORKERS` 本のスレッドがそれぞれ専用プロファイルでバッチを処理する（0 で先変換を行わない）。
設計: docs/design/rag.md「アーム一覧」
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from pathlib import Path

from sherpa.env_int import env_int

from .. import mem_guard
from . import legacy_convert as lc

_log = logging.getLogger("sherpa.convert.libreoffice")

_DEFAULT_BATCH = 16
_MAX_BATCH = 256
_MAX_WORKERS = 8
_BATCH_TIMEOUT_CAP_SEC = 600.0   # バッチ全体のタイムアウト上限（件数 × 1 件分と比べて小さい方）
_FAILURE_LABELS = {"timeout": "時間切れ", "memory": "メモリの上限を超えた", "error": "異常終了"}


def batch_size() -> int:
    """1 回の soffice 起動で変換する件数。env `SHERPA_LEGACY_BATCH`（1〜256）。未設定・不正値は 16。"""
    return env_int("SHERPA_LEGACY_BATCH", _DEFAULT_BATCH, 1, _MAX_BATCH)


def worker_count() -> int:
    """先変換の並列数。env `SHERPA_LEGACY_WORKERS`（0〜8・0 で先変換しない）。未設定・不正値は MD 化の先読みと同じ本数。"""
    from .. import md_prefetch
    return env_int("SHERPA_LEGACY_WORKERS", md_prefetch.worker_count(), 0, _MAX_WORKERS)


class _Entry:
    __slots__ = ("rel", "src", "ext", "key", "cache_path", "key_path", "state")

    def __init__(self, rel: str, src: Path, ext: str, key: str, cache_path: Path, key_path: Path):
        self.rel, self.src, self.ext, self.key = rel, src, ext, key
        self.cache_path, self.key_path = cache_path, key_path
        self.state = "queued"        # queued → running → done ／ queued → claimed（本体ループが先に取った）


class LegacyPrefetcher:
    """`items`（`(原本, rel)` を本体ループの順に）の先変換を始める。`settle(rel)` は本体ループがその rel を処理する直前に呼ぶ。

    ① 変換済み（キャッシュが原本と一致）のものは対象から外す ② 先頭から同じ拡張子のバッチを切り出し、スレッドが専用プロファイルで変換する
    ③ 成功分は `ensure_ooxml` と同じ鍵でキャッシュへ書く。同じ rel を 2 度変換しない（`settle` は変換中なら完了まで待ち、未着手なら本体ループへ譲る）。
    """

    def __init__(self, items: list[tuple[Path, str]], cache_root, *, workers: int, batch: int):
        self._cond = threading.Condition()
        self._tracker = lc.SofficeTracker()
        self._batch = max(1, batch)
        self._bin = lc._soffice_bin()
        self._entries: dict[str, _Entry] = {}
        self._pending: dict[str, _Entry] = {}
        self._closed = False
        self.planned = 0
        self.stored = 0
        self._workers = workers
        for src, rel in items:
            entry = self._plan(Path(src), rel, Path(cache_root))
            if entry is not None:
                self._entries[rel] = self._pending[rel] = entry
        self.planned = len(self._entries)
        self._threads: list[threading.Thread] = []
        self._allowed = self._alive = workers          # 進んでよい並列数・動いているスレッド数
        if self._bin and self._pending:
            if workers > 1 and not mem_guard.wait_for_free_memory("旧形式の先変換"):
                workers -= 1
                _log.warning("メモリの空きが戻らないため、旧形式の先変換の並列を %d に減らして進めます", workers)
            self._workers = self._allowed = workers
            firsts = [self._take_batch() for _ in range(min(workers, -(-len(self._pending) // self._batch)))]
            self._alive = len(firsts)
            for first in firsts:        # 先頭のバッチは開始前に確保する（本体ループが先頭を自分で変換してしまわないように）
                t = threading.Thread(target=self._run, args=(first,), name="legacy-prefetch", daemon=True)
                self._threads.append(t)
                t.start()

    @staticmethod
    def _plan(src: Path, rel: str, cache_root: Path) -> _Entry | None:
        ext = src.suffix.lower()
        target_ext = lc.LEGACY_EXT_MAP.get(ext)
        if target_ext is None:
            return None
        try:
            key = lc._source_key(src)
        except OSError:
            return None
        cache_path = cache_root / (rel + target_ext)
        key_path = Path(str(cache_path) + ".key")
        if lc.cache_valid(cache_path, key_path, key):
            return None
        return _Entry(rel, src, ext, key, cache_path, key_path)

    def __enter__(self) -> "LegacyPrefetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def settle(self, rel: str) -> None:
        """`rel` が変換中なら終わるまで待つ。未着手なら先変換の対象から外す（本体ループが自分で変換する）。"""
        with self._cond:
            entry = self._entries.get(rel)
            if entry is None:
                return
            if entry.state == "queued":
                self._pending.pop(rel, None)
                entry.state = "claimed"
                return
            while entry.state == "running" and not self._closed:
                self._cond.wait(timeout=1.0)

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
            self._cond.notify_all()
        self._tracker.kill_all()
        for t in self._threads:
            t.join(timeout=30)
        _log.info("旧形式の先変換を使いました: %d/%d件（バッチ %d・並列 %d）",
                  self.stored, self.planned, self._batch, self._workers)

    def _may_continue(self) -> bool:
        """次のバッチへ進んでよいか。空きメモリを待ち、空かなければ並列を 1 つ減らす（減らした分のスレッドは、この判定で終わる）。"""
        with self._cond:
            if self._closed:
                return True
            if self._alive > self._allowed:
                self._alive -= 1
                return False
            single = self._allowed <= 1
        if single or mem_guard.wait_for_free_memory("旧形式の先変換"):
            return True
        with self._cond:
            if self._allowed > 1:
                self._allowed -= 1
                _log.warning("メモリの空きが戻らないため、旧形式の先変換の並列を %d に減らして進めます", self._allowed)
            if self._alive > self._allowed:
                self._alive -= 1
                return False
        return True

    def _take_batch(self) -> list[_Entry]:
        with self._cond:
            if self._closed or not self._pending:
                return []
            first = next(iter(self._pending.values()))
            taken = [e for e in self._pending.values() if e.ext == first.ext][:self._batch]
            for e in taken:
                del self._pending[e.rel]
                e.state = "running"
            return taken

    def _finish(self, entries: list[_Entry]) -> None:
        with self._cond:
            for e in entries:
                e.state = "done"
            self._cond.notify_all()

    def _run(self, batch: list[_Entry]) -> None:
        while batch:
            try:
                self._process(batch)
            except Exception as e:
                _log.warning("旧形式の先変換のバッチが失敗しました（%s %d件・理由: %s）。本体が 1 件ずつ変換し、原因の資料をログに残します",
                             batch[0].ext, len(batch), e.__class__.__name__, exc_info=True)
            finally:
                self._finish(batch)
            if not self._may_continue():
                return
            batch = self._take_batch()

    def _process(self, batch: list[_Entry]) -> None:
        """バッチを変換し、出力が揃ったものから順にキャッシュへ書いて完了にする。揃わなかったものは放置する。"""
        outputs = self._convert(batch)
        for i, entry in enumerate(batch):
            data = outputs.get(i)
            if data is not None and lc.store_cache(entry.cache_path, entry.key_path, data, entry.key):
                with self._cond:
                    self.stored += 1
            self._finish([entry])

    def _convert(self, batch: list[_Entry]) -> dict[int, bytes]:
        """バッチを 1 回の soffice 起動で変換して `{バッチ内の番号: OOXML のバイト列}`。原本は触らず、一時 dir に置いたコピーだけを渡す（LibreOffice はシンボリックリンクを辿って原本の横にロックファイルを作る）。"""
        ext = batch[0].ext
        target_ext = lc.LEGACY_EXT_MAP[ext]
        work = tempfile.mkdtemp(prefix="sherpa-lo-batch-")
        profile = tempfile.mkdtemp(prefix="sherpa-lo-profile-")
        outdir = tempfile.mkdtemp(prefix="sherpa-lo-out-")
        try:
            links: dict[int, Path] = {}
            for i, entry in enumerate(batch):
                link = Path(work) / f"{i}{ext}"
                try:
                    shutil.copyfile(entry.src, link)
                except OSError:
                    continue
                links[i] = link
            if not links:
                return {}
            timeout = min(lc._timeout_sec() * len(links), max(lc._timeout_sec(), _BATCH_TIMEOUT_CAP_SEC))
            label = f"{ext} {len(links)}件のバッチ"
            if ext == ".xls":
                lc._convert_xls_keep_values_batch(self._bin, work, profile, outdir, label, timeout, self._tracker)
                done = {i for i in links if (Path(outdir) / f"{i}.ok").is_file()}
            else:
                cmd = [self._bin, "--headless", "--convert-to", target_ext.lstrip("."), "--outdir", outdir,
                       f"-env:UserInstallation={Path(profile).as_uri()}", *map(str, links.values())]
                done = set(links) if lc._run_soffice(cmd, label, timeout=timeout, quiet=True,
                                                    tracker=self._tracker) else set()
            failure = lc.take_soffice_failure()
            if failure is not None:
                _log.warning("旧形式の先変換のバッチが失敗しました（%s（理由: %s））。本体が 1 件ずつ変換し、原因の資料をログに残します",
                             label, _FAILURE_LABELS.get(failure, failure))
            out: dict[int, bytes] = {}
            for i in done:
                path = Path(outdir) / f"{i}{target_ext}"
                try:
                    if path.is_file():
                        out[i] = path.read_bytes()
                except OSError:
                    continue
            return out
        finally:
            for d in (work, profile, outdir):
                shutil.rmtree(d, ignore_errors=True)


def start(items: list[tuple[Path, str]], cache_root) -> LegacyPrefetcher | None:
    """libreoffice バックエンドで、先変換する旧形式が 1 件以上あり、並列数が 1 以上のときだけ始める（それ以外は None）。"""
    if not items or worker_count() < 1:
        return None
    if lc.legacy_backend_name() != "libreoffice" or not lc.soffice_available():
        return None
    pf = LegacyPrefetcher(items, cache_root, workers=worker_count(), batch=batch_size())
    return pf if pf.planned else None
