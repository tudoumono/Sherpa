"""Codex ジョブ（外部 API の非同期 Codex 実行）の背景ワーカー。永続ジョブ（`store/codex_jobs.py`）を実際に Codex で実行する。
設計: docs/design/codex.md「Codex ジョブ API（外部 API）」／docs/design/interfaces.md「Codex ジョブ・受付」

- Codex の起動方法は新設せず、チャットと同じ実行経路（`agents.get_provider`→`CodexProvider.run(ctx)`）・サンドボックス・壁時計上限を使う。
  会話を DB に永続する `stream_message` は呼ばず、`chat_service` の `_resolve_scope`/`_finalize`/`_sources`/`_is_stopped_terminal` だけ借りる。
- `Ctx.route` は常に `lens="qa"`。`Ctx.dispatch` は通常構成では呼ばれず、MCP 無効の構成でだけツール遮断の envelope を返す保険。
- uid は実在ユーザーと衝突しない技術 uid（`_CODEX_JOB_UID`）を常に渡す。Codex が作ったファイルは
  `data/users/{_CODEX_JOB_UID}/workspace/` に隔離される。
- 同時実行は global 上限をチャットと合算で数える（`chat_turns.try_reserve_external`/`release_external`）。
  claim の前に 1 件ずつ予約し、使わなかった予約は即解放、ジョブ終了時（全経路・`_execute_job` の outer `finally`）に解放する。
  `_running`（job_id→stop_event）は取消通知用で、容量計算には使わない。
"""
from __future__ import annotations

import logging
import threading
import time

from . import agentic_search, chat_service, chat_turns, store, worlds
from . import scope as scope_mod
from .agents import Ctx, CodexProvider, get_provider
from .store import codex_jobs as store_jobs
from .store import investigation_records as store_investigation

_log = logging.getLogger("sherpa")

# 実在ユーザーと衝突しない技術 uid。英数字のみ（`_safe_workspace_authoring` の slug 正規表現を満たす）。
_CODEX_JOB_UID = "ext-codex-job"

_POLL_INTERVAL_S = 2.0
# 期限切れ掃除は poll を間引いて走らせる（2 秒×150≒5 分間隔）。
_EXPIRE_SWEEP_EVERY_N_POLLS = 150

_state_lock = threading.Lock()
_running: dict[str, threading.Event] = {}  # job_id -> stop_event（キャンセル通知用の実行時対応表）
_dispatcher_thread: threading.Thread | None = None
_stop_dispatcher = threading.Event()
_poll_count = 0


def _register_running(job_id: str, stop_event: threading.Event) -> None:
    with _state_lock:
        _running[job_id] = stop_event


def _unregister_running(job_id: str) -> None:
    with _state_lock:
        _running.pop(job_id, None)


def signal_cancel(job_id: str) -> bool:
    """実行中ジョブの `stop_event` を立てる（取消 API から）。見つからなければ False（取消は冪等・呼び出し側は DB の現在状態を返す）。"""
    with _state_lock:
        ev = _running.get(job_id)
    if ev is None:
        return False
    ev.set()
    return True


class CodexUnavailable(RuntimeError):
    """受付時点で Codex が実行可能でない（受付の 503 の根拠）。"""


def _codex_settings() -> dict:
    """ジョブの実行に使う設定。接続先・モデルは管理者設定を使い、頭脳は常に Codex に固定する。"""
    settings = dict(store.get_settings("admin") or {})
    settings["agent"] = "codex"
    return settings


def check_available() -> None:
    """Codex が今実行可能かを確認する（受付の 503 判定）。不可なら `CodexUnavailable`（詳細はログのみ）。
    ① 管理者の既定の頭脳が Codex（`CodexProvider`）へ解決すること。
    ② `health._ai_check_codex`（CLI 導入・ログイン・Ollama 接続先/モデルの存在）が例外を出さないこと。
    `ext_api.py` は `agents`/`health` を import しない契約のため、判定はここに置く。
    """
    from . import health
    settings = _codex_settings()
    sys_settings = store._read_system_settings_fresh()
    provider = get_provider(settings, system_settings=sys_settings)
    if not isinstance(provider, CodexProvider):
        raise CodexUnavailable("既定の構成が Codex ではありません")
    try:
        health._ai_check_codex(settings, sys_settings)
    except Exception as e:
        raise CodexUnavailable(str(e)) from e


def capability() -> tuple[bool, bool, str | None]:
    """discovery 用の `(configured, available, reason)`。`check_available` と同じ判定を、閉じた語彙の理由に畳んで返す。"""
    import shutil
    from . import health
    try:
        settings = _codex_settings()
        sys_settings = store._read_system_settings_fresh()
        if not isinstance(get_provider(settings, system_settings=sys_settings), CodexProvider):
            return False, False, "not_configured"
    except Exception:
        return False, False, "not_configured"
    if not shutil.which("codex"):
        return True, False, "codex_cli_missing"
    try:
        health._ai_check_codex(settings, sys_settings)
    except Exception:
        return True, False, "backend_unreachable"
    return True, True, None


def _max_global() -> int:
    """チャットの同時実行上限（`chat_turns.effective_limits()` の global 値）。"""
    _per_user, glob = chat_turns.effective_limits()
    return glob


def _qa_route(msg: str) -> dict:
    return {"lens": "qa", "input": msg, "reason": "ext_codex_job", "confident": True}


def _qa_dispatch_fallback(lens: str, _inp: str) -> dict:
    """MCP 無効時の保険（通常構成では呼ばれない）。"""
    return agentic_search.tools_blocked_env(lens)


def _execute_job(job: dict, stop_event: threading.Event) -> None:
    """1 ジョブを実行し、終了状態を DB へ書く（`queued`→`running` は呼び出し元の `claim_queued_jobs()` が済み）。
    例外は内部で捕捉して `codex_failed` で終端化し、呼び出し元へ伝播させない。
    `dispatch_cycle` が確保した外部実行枠は、全終了経路で outer `finally` が解放する。
    """
    job_id = job["id"]
    t0 = time.monotonic()
    try:
        current = store_jobs.get_job(job_id)
        if not current or current.get("status") != "running":
            # claim 後・停止の登録前に取り消された。Codex を起動しない。
            return
        try:
            res = worlds.resolve_external_world(job["world"])
        except worlds.ExternalResolverError as e:
            _log.warning("codex job: world resolver 到達不可 job_id=%s: %s", job_id, e)
            store_jobs.mark_failed(job_id, error_code="codex_failed")
            return
        if res.status != "ok":
            _log.warning("codex job: world が見つかりません job_id=%s world=%s", job_id, job["world"])
            store_jobs.mark_failed(job_id, error_code="codex_failed")
            return
        root = res.path
        with worlds.pin_world_root(job["world"], root):
            try:
                if not scope_mod.valid_scope_paths(job["world"], job["scope_paths"], root=root, strict=True):
                    store_jobs.mark_failed(job_id, error_code="codex_failed")
                    return
            except OSError:
                store_jobs.mark_failed(job_id, error_code="codex_failed")
                return
            scope_meta = chat_service._resolve_scope(
                job["query"], job["world"], job["scope_paths"], depth_profile=job["depth"])
            settings = _codex_settings()
            sys_settings = store._read_system_settings_fresh()
            provider = get_provider(settings, system_settings=sys_settings)
            if not isinstance(provider, CodexProvider):
                # 受付〜実行の間に管理者が構成を変えた場合の多層防御。
                _log.warning("codex job: 既定の構成が Codex ではありません job_id=%s", job_id)
                store_jobs.mark_failed(job_id, error_code="codex_failed")
                return
            ctx = Ctx(message=job["query"], world=job["world"], route=_qa_route,
                      dispatch=_qa_dispatch_fallback, knowledge=True, scope_meta=scope_meta,
                      make_sources=lambda docs: chat_service._sources(docs, job["world"]),
                      uid=_CODEX_JOB_UID, stop_event=stop_event,
                      tools_availability=agentic_search.tool_availability())
            result = None
            try:
                for ev in provider.run(ctx):
                    if stop_event.is_set() and not chat_service._is_stopped_terminal(ev):
                        continue
                    if isinstance(ev, dict) and ev.get("type") == "_result":
                        result = ev
                        break
            except Exception:
                _log.exception("codex job: provider.run が例外を送出しました job_id=%s", job_id)
                if stop_event.is_set():
                    store_jobs.mark_cancelled(job_id)
                else:
                    store_jobs.mark_failed(job_id, error_code="codex_failed")
                return
            if stop_event.is_set():
                # 取消要求による終了は、部分的な回答があっても「取消」として扱う（取消ジョブに結果は持たせない）。
                store_jobs.mark_cancelled(job_id)
                return
            if result is None:
                store_jobs.mark_failed(job_id, error_code="codex_failed")
                return
            env = chat_service._finalize(result["env"], result["decision"], job["query"])
            elapsed_ms = round((time.monotonic() - t0) * 1000)
            sources = [{"doc_id": s["doc_id"]} for s in (env.get("sources") or []) if s.get("doc_id")]
            unconfirmed_items = list((env.get("investigation") or {}).get("unconfirmed_items") or [])
            # ジョブの結果にも調査の記録を載せる（チャット経路と同じ切り詰めルール・ジョブと一緒に期限切れで消える）。
            _investigation_record = result.get("investigation_record")
            investigation = None
            if _investigation_record is not None:
                _manifest, _items, _coverage, _reviews, _truncated = store_investigation.trim_to_budget(
                    _investigation_record.get("manifest"),
                    _investigation_record.get("items") or {},
                    _investigation_record.get("coverage") or {},
                    _investigation_record.get("reviews") or [])
                investigation = {"complete": bool(_investigation_record.get("complete")),
                                 "truncated": _truncated,
                                 "manifest": _manifest,
                                 "items": _items, "coverage": _coverage, "reviews": _reviews}
            store_jobs.mark_completed(job_id, answer=env.get("headline") or "", sources=sources,
                                      unconfirmed_items=unconfirmed_items, elapsed_ms=elapsed_ms,
                                      investigation=investigation)
    except Exception:
        _log.exception("codex job: 予期しない例外で終端化します job_id=%s", job_id)
        try:
            store_jobs.mark_failed(job_id, error_code="codex_failed")
        except Exception:
            _log.exception("codex job: failed への終端化自体にも失敗しました job_id=%s", job_id)
    finally:
        _unregister_running(job_id)
        chat_turns.release_external()  # dispatch_cycle が確保した枠を返す（全終了経路共通）


def dispatch_cycle() -> None:
    """claim できる分だけ `queued` ジョブを `running` にし、ジョブごとに専用スレッドを起こす（副作用は DB 状態遷移とスレッド起動のみ）。
    ① claim の前に、件数分の外部実行枠を `chat_turns.try_reserve_external()` で 1 件ずつ予約する。
    ② claim できた件数が予約より少なければ、余りを即解放する。
    ③ 各ジョブの予約は `_execute_job` の outer `finally` が解放する。
    """
    max_global = _max_global()
    reserved = 0
    while reserved < max_global and chat_turns.try_reserve_external(max_global):
        reserved += 1
    if reserved == 0:
        return
    try:
        jobs = store_jobs.claim_queued_jobs(reserved)
    except Exception:
        for _ in range(reserved):
            chat_turns.release_external()  # claim が失敗したら予約をすべて返す
        raise
    for _ in range(reserved - len(jobs)):
        chat_turns.release_external()  # claim できなかった分の予約を即解放
    for job in jobs:
        job_id = job["id"]
        stop_event = threading.Event()
        # スレッド起動前に登録する（起動直後の取消を `signal_cancel` が見つけられるように）。起動失敗時は下の except で解除する。
        _register_running(job_id, stop_event)
        try:
            threading.Thread(target=_execute_job, args=(job, stop_event), daemon=True,
                             name=f"codex-job-{job_id[:8]}").start()
        except Exception:
            _unregister_running(job_id)
            chat_turns.release_external()  # スレッドが起動しなかった＝_execute_job の finally は走らない
            _log.exception("codex job: スレッド起動に失敗しました job_id=%s", job_id)
            store_jobs.mark_failed(job_id, error_code="codex_failed")


def _run_loop() -> None:
    global _poll_count
    while not _stop_dispatcher.is_set():
        try:
            dispatch_cycle()
            _poll_count += 1
            if _poll_count % _EXPIRE_SWEEP_EVERY_N_POLLS == 0:
                store_jobs.expire_due_jobs()
        except Exception:
            _log.exception("codex jobs dispatcher: 1周期の処理に失敗しました")
        _stop_dispatcher.wait(_POLL_INTERVAL_S)


def start() -> None:
    """起動時に呼ぶ（`lifespan`）。前回プロセスが `running` のまま残したジョブを `failed(interrupted)` にし、ポーリングの背景スレッドを起こす。"""
    try:
        ids = store_jobs.recover_interrupted_on_startup()
        if ids:
            _log.warning("codex jobs: 起動時に running のまま残っていたジョブを "
                        "failed(interrupted) にしました: %s", ids)
    except Exception:
        _log.exception("codex jobs: 起動時の running ジョブ回収に失敗しました（DB 不達の可能性）")
    _stop_dispatcher.clear()
    global _dispatcher_thread
    _dispatcher_thread = threading.Thread(target=_run_loop, daemon=True, name="codex-jobs-dispatcher")
    _dispatcher_thread.start()


def stop(timeout: float = 5.0) -> None:
    """新規の claim を止める（shutdown）。実行中のジョブスレッドは daemon で、待ち切らない。"""
    _stop_dispatcher.set()
    t = _dispatcher_thread
    if t is not None:
        t.join(timeout=timeout)
