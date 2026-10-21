"""取り込み専用ワーカー（`python -m sherpa.ingest.ingest_worker`）。

Web アプリは取り込みを動かさず、`ingest_runs` に queued の依頼（操作名＋引数）を積むだけにする。
このプロセスが自分と同じ版の依頼を 1 件拾って実行し、**1 件ごとにプロセスを終了する**（Python は空いたメモリを
OS へ返しにくいため、重い処理のたびにプロセスごと捨てて全部戻す）。起動し直すのは見張りのスクリプト
（`scripts/run-ingest-worker.sh`・systemd の `sherpa-ingest.service`）。
① 起動 → ② 持ち主の居ない extracting の run を中断へ格下げ → ③ 中断した取り込みの再開依頼 →
③' 旧形式の埋め込みキャッシュの写し替え（依頼を拾う前。待っている依頼では止めず、停止要求でだけ止める）→
④ 待機（短い間隔で確認・版の違う依頼しか無ければ waiting_version。依頼が無いあいだは旧形式の埋め込みキャッシュを 1 資料フォルダずつ新形式へ写し替える）→
⑤ 1 件拾って実行 → LLM 成形の後追い → 埋め込みキャッシュの写し替え → ⑥ 終了。
停止要求（SIGTERM）は処理中の 1 件の区切りで止まり、次の起動で続きから再開する。
設計: docs/design/operations.md「取り込みワーカーと版の照合」
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

from .. import app_version, store, webhooks
from ..env_int import env_int
from ..store import workers as store_workers
from . import background, jobs, worker_state

_log = logging.getLogger("sherpa")

POLL_SEC = 2.0                 # 待機中に依頼を確かめる間隔（env `SHERPA_INGEST_POLL_SEC`）
RESUME_RECHECK_SEC = 30.0      # 起動の直後に間に合わなかった中断 run を拾い直す時刻
METAFILE_FIRST_DELAY_SEC = 60.0
METAFILE_INTERVAL_SEC = 300.0
FOLLOWUP_CHECK_SEC = 2.0       # LLM 成形の途中で「待っている依頼があるか」を確かめる間隔

_terminate = threading.Event()   # SIGTERM／SIGINT で立つ


class _Beat:
    """心拍（`ingest_workers` と状態ファイル）。`BEAT_SEC` ごと、と状態が変わったときに書く。"""

    def __init__(self, worker_id: str, version: str | None):
        self.worker_id = worker_id
        self.version = version
        self._lock = threading.Lock()
        self._state = "idle"
        self._run_id: int | None = None
        self._world: str | None = None
        self._label: str | None = None     # run 以外の処理（LLM 成形）を状態ファイルへ出すときの表示名
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sherpa-ingest-worker-beat")

    def start(self) -> None:
        self.write()
        self._thread.start()

    def stop(self, state: str) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        with self._lock:
            self._state, self._run_id, self._world, self._label = state, None, None, None
        self.write()
        worker_state.clear_active()

    def set_state(self, state: str, *, run_id: int | None = None, world: str | None = None,
                  label: str | None = None) -> None:
        with self._lock:
            changed = (state, run_id, world, label) != (self._state, self._run_id, self._world, self._label)
            self._state, self._run_id, self._world, self._label = state, run_id, world, label
        if changed:
            self.write()

    def write(self) -> None:
        with self._lock:
            state, run_id, world, label = self._state, self._run_id, self._world, self._label
        rel, since = background.current_file()
        rel = label or rel
        try:
            store_workers.beat(self.worker_id, role="ingest", app_version=self.version, state=state,
                               run_id=run_id, current_file=rel, pid=os.getpid())
        except Exception:
            _log.debug("取り込みワーカーの心拍の記録に失敗しました（best-effort）", exc_info=True)
        if world is not None:
            worker_state.write_active(world, since or time.time(), rel)
        else:
            worker_state.clear_active()

    def _loop(self) -> None:
        while not self._stop.wait(worker_state.BEAT_SEC):
            self.write()


def run_claimed(row: dict) -> None:
    """拾った依頼 1 件を実行する。想定外の例外はここで捕まえ、終端に届かなかった run は failed へ落とす（最外周のセーフティネット）。"""
    run_id, world, op = row["id"], row["version"], row.get("op")
    background.bind_run(run_id)
    try:
        jobs.run_job(op, world, row.get("payload"), run_id)
    except Exception:
        _log.warning("取り込みの実行が未捕捉の例外で終了しました: world=%s op=%s run_id=%s",
                     world, op, run_id, exc_info=True)
    finally:
        # 操作が terminal 化しなかった行（status='extracting' のまま）だけ failed へ落とす
        try:
            if store.fail_close_if_extracting(
                    run_id, reason="background_worker_exited_without_terminal_status"):
                _log.warning("取り込みが run を terminal 化せずに終了したため failed へ格下げしました: "
                             "world=%s op=%s run_id=%s", world, op, run_id)
                try:
                    webhooks.notify_run_terminal(world, run_id, op or "", "failed")
                except Exception:
                    _log.warning("Webhook 通知の起動に失敗しました（best-effort）: world=%s run_id=%s",
                                 world, run_id, exc_info=True)
        except Exception:
            _log.warning("最外周の failed 格下げ自体に失敗しました（best-effort）: world=%s run_id=%s",
                         world, run_id, exc_info=True)
        background.unbind_run()


def process_one(worker_id: str, version: str | None, beat: _Beat | None = None) -> bool:
    """自分と同じ版の依頼を 1 件拾って実行する。拾えなければ False。（`main` の 1 回分。テストもこれを直接呼ぶ）"""
    row = store.claim_queued_run(worker_id, version)
    if row is None:
        return False
    if beat is not None:
        beat.set_state("running", run_id=row["id"], world=row["version"])
    run_claimed(row)
    return True


def recover_orphans() -> list[int]:
    """持ち主のワーカーが居ない（異常終了した）`extracting` の run を中断へ格下げする。格下げした run の id を返す。"""
    downgraded: list[int] = []
    for claimant in store.list_claimants_of_extracting_runs():
        if store_workers.is_dead(claimant):
            downgraded += store.downgrade_orphaned_extracting_runs(claimed_by=claimant)
    if downgraded:
        _log.warning("異常終了したワーカーの取り込み run を中断へ格下げしました: ids=%s", downgraded)
    return downgraded


def _run_followups(version: str | None, beat: _Beat) -> None:
    """run の後に、LLM 成形（`worker.sync()` が登録したもの）を行う。待っている依頼があるか停止要求が出たら、次の文書へ進まず止める。"""
    from . import llm_render
    from . import worker as ingest_worker

    last_check = [0.0]
    waiting = [False]

    def _should_stop() -> bool:
        if _terminate.is_set():
            return True
        now = time.monotonic()
        if now - last_check[0] >= FOLLOWUP_CHECK_SEC:
            last_check[0] = now
            try:
                waiting[0] = store.count_queued_runs(version)[0] > 0
            except Exception:
                waiting[0] = False
        return waiting[0]

    for world in llm_render.take_followups():
        if _should_stop():
            break
        beat.set_state("running", world=world, label="AI整形")
        try:
            with background.followup_scope():
                ingest_worker.llm_render_pass(world, should_stop=_should_stop)
        except background.LowDiskStop as e:
            _log.warning("LLM 成形の後追いをディスクの空き不足で止めました（自動では繰り返しません。空きを作って「更新」を押してください）: world=%s %s", world, e)
        except Exception:
            _log.warning("LLM 成形の後追いが失敗しました: world=%s", world, exc_info=True)


def _migrate_embed_caches(version: str | None, beat: _Beat, tried: set, *, yield_to_queue: bool = True) -> bool:
    """旧形式の埋め込みキャッシュを 1 資料フォルダずつ新形式へ写し替える。待っている依頼が来るか（`yield_to_queue`）停止要求が出たら、次の区切りで止める。写し替えた（メモリを返すためプロセスを終える合図）なら True。見送り・失敗は False。"""
    from .. import embed_cache_migrate

    last_check = [0.0]
    waiting = [False]

    def _should_stop() -> bool:
        if _terminate.is_set():
            return True
        if not yield_to_queue:
            return False
        now = time.monotonic()
        if now - last_check[0] >= FOLLOWUP_CHECK_SEC:
            last_check[0] = now
            try:
                waiting[0] = store.count_queued_runs(version)[0] > 0
            except Exception:
                waiting[0] = False
        return waiting[0]

    def _on_world(world: str) -> None:
        tried.add(world)
        beat.set_state("running", world=world, label="埋め込みキャッシュの写し替え")

    try:
        embed_cache_migrate.sweep_document_json(should_stop=_should_stop, skip=tried)
        results = embed_cache_migrate.migrate_pending(should_stop=_should_stop, skip=tried, on_world=_on_world)
    except Exception:
        _log.warning("埋め込みキャッシュの写し替えに失敗しました（古いファイルはそのまま使えます）", exc_info=True)
        return False
    return any(r.get("status") in ("migrated", "cleaned") for r in results)


def _metafile_pass() -> bool:
    """図全体の描画待ちを進める。実際に描いた（または失敗した）なら True（メモリを返すためプロセスを終える合図）。"""
    from . import metafile_render
    try:
        result = metafile_render.run_pass()
    except Exception:
        _log.warning("図全体の描画 pass に失敗しました（次回に再試行）", exc_info=True)
        return False
    if result.get("rendered") or result.get("failed"):
        _log.info("図全体の描画 pass: %s", result)
        return True
    return False


def _resume() -> None:
    from . import resume
    try:
        resume.resume_interrupted()
    except Exception:
        _log.warning("中断した取り込みの再開依頼に失敗しました（best-effort）", exc_info=True)


def _wait_and_process(worker_id: str, version: str | None, beat: _Beat) -> bool:
    """依頼を待ち、拾えたら実行する。実行した（プロセスを終える）なら True、停止要求で抜けたら False。"""
    poll = float(env_int("SHERPA_INGEST_POLL_SEC", int(POLL_SEC), 1, 60))
    started = time.monotonic()
    resume_at = started + RESUME_RECHECK_SEC
    meta_at = started + METAFILE_FIRST_DELAY_SEC
    migrated: set = set()   # このプロセスで写し替えを試した資料フォルダ（見送り・失敗を待機のたびに繰り返さない）
    # 依頼を拾う前に写し替える（取り込みの間に旧形式のキャッシュへ書き足して膨らませない）。
    if _migrate_embed_caches(version, beat, migrated, yield_to_queue=False):
        return True
    while not _terminate.is_set():
        try:
            if process_one(worker_id, version, beat):
                _run_followups(version, beat)
                _migrate_embed_caches(version, beat, migrated)
                return True
            _same, other = store.count_queued_runs(version)
            if not other and _migrate_embed_caches(version, beat, migrated):
                return True
            beat.set_state("waiting_version" if other else "idle")
        except Exception:
            _log.warning("取り込みの依頼の確認に失敗しました（DB 不達の可能性・待機を続けます）", exc_info=True)
        now = time.monotonic()
        if now >= resume_at:
            resume_at = float("inf")
            _resume()
        if now >= meta_at:
            if _metafile_pass():
                return True
            meta_at = time.monotonic() + METAFILE_INTERVAL_SEC
        _terminate.wait(poll)
    return False


def _on_signal(signum, _frame) -> None:
    _terminate.set()
    background.request_stop()


def main(argv: list[str] | None = None) -> int:
    from .. import log_setup
    try:
        log_setup.configure_logging(rotate=False, owner="ingest")
    except Exception as e:
        _log.warning("サブシステム別ログの設定に失敗しました（続行します）: %s", e)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    worker_id = worker_state.new_worker_id("ingest")
    version = app_version.current()
    store.init_schema()
    liveness = store_workers.hold_liveness_lock(worker_id)
    beat = _Beat(worker_id, version)
    beat.start()
    _log.info("取り込みワーカーを開始しました: worker=%s 版=%s pid=%s", worker_id, version, os.getpid())
    try:
        store_workers.purge_old()
        recover_orphans()
        _resume()
        _wait_and_process(worker_id, version, beat)
    finally:
        webhooks.flush(timeout=60)
        beat.stop("stopping" if _terminate.is_set() else "idle")
        try:
            liveness.close()
        except Exception:
            pass
        try:
            from sherpa.store import db as store_db
            store_db.close_pg_pool()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
