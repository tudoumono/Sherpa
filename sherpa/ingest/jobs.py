"""取り込みワーカーが実行する操作の一覧（操作名＋JSON の引数 → 実処理）。

Web アプリは `background.submit` で操作名と引数を `ingest_runs` に積むだけで、ワーカーが `run_job` で同じ関数を呼ぶ。
操作ごとの `payload` のキー: register＝`path`/`label`/`world_id`/`root`、rebind＝`path`/`label`、reconvert＝`rel`/`uid`、
delete＝`uid`、refresh／rerun／rag_regenerate_rules＝なし。
設計: docs/design/operations.md「取り込みワーカーと版の照合」
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from .. import store, webhooks, world_admin_service, worlds
from . import worker as ingest_worker

_log = logging.getLogger("sherpa")


def _register(world: str, payload: dict, run_id: int) -> None:
    # `world_id`・`root` は受付時に確定した値を使い、受付の判断と実際に登録される値を一致させる。
    world_admin_service.register_or_rerun(payload["path"], label=payload.get("label"), world_id=world,
                                          root=payload["root"], run_id=run_id)


def _refresh(world: str, payload: dict, run_id: int) -> None:
    world_admin_service.refresh(world, run_id=run_id)


def _rebind(world: str, payload: dict, run_id: int) -> None:
    world_admin_service.rebind(world, payload["path"], label=payload.get("label"), run_id=run_id)


def _rerun(world: str, payload: dict, run_id: int) -> None:
    ingest_worker.rerun(world, run_id=run_id)


def _rag_regenerate_rules(wid: str, payload: dict, run_id: int) -> None:
    """LLM 成形キャッシュの一掃・rag.md の規則版への作り直し・ES 反映を `ingest_worker.regenerate_rag_rule_only` へ委譲する（`store.world_lock` は同関数が確保し、`_run_locked` は経由しない）。"""
    if not store.get_world(wid):
        store.finish_ingest_run(run_id, status="failed", extraction_snapshot={
            "flags": [{"doc": None, "action": "blocked", "reason": "world_not_found"}]})
        return
    if not worlds.world_dir(wid):
        store.finish_ingest_run(run_id, status="failed", extraction_snapshot={
            "flags": [{"doc": None, "action": "blocked", "reason": "world_dir_unreachable"}]})
        return
    try:
        result = ingest_worker.regenerate_rag_rule_only(wid)
    except Exception as e:
        store.finish_ingest_run(run_id, status="failed", extraction_snapshot={
            "flags": [{"doc": None, "action": "blocked",
                      "reason": f"unexpected_error:{e.__class__.__name__}"}]})
        _log.warning("規則版への再生成が想定外の例外で失敗しました: world=%s", wid, exc_info=True)
        return
    if result.get("status") == "ok":
        store.finish_ingest_run(run_id, status="auto_published", extraction_snapshot={
            "docs": result.get("rag_generated", 0)})
    else:
        store.finish_ingest_run(run_id, status="failed", extraction_snapshot={
            "flags": [{"doc": None, "action": "blocked", "reason": str(result.get("status"))}],
            "docs": result.get("rag_generated", 0)})


def _audit_reconvert(uid: str | None, action: str, wid: str, rel: str, *, outcome: str = "success") -> None:
    """reconvert の pre/post 監査（best-effort・actor/world/rel/結果を記録）。"""
    try:
        store.audit(uid, action, "world", f"world:{wid}",
                    detail={"world": wid, "rel": rel}, outcome=outcome)
    except Exception:
        _log.warning("reconvert 監査ログの記録に失敗しました（best-effort）: action=%s world=%s rel=%s",
                     action, wid, rel, exc_info=True)


def _reconvert(wid: str, payload: dict, run_id: int) -> None:
    uid, rel = payload.get("uid"), payload["rel"]
    _audit_reconvert(uid, "world.reconvert_requested", wid, rel)
    outcome = "failure"
    try:
        res = ingest_worker.reconvert(wid, rel, run_id=run_id)
        outcome = "failure" if res["status"] == "failed" else "success"
    finally:
        _audit_reconvert(uid, "world.reconverted", wid, rel, outcome=outcome)


def _notify_delete_terminal(wid: str, run_id: int, status: str) -> None:
    """削除 run の terminal 化を Webhook 通知する（best-effort・削除自体の成否には影響させない）。`status` は `ingest_runs.status` と同じ語彙（"auto_published"|"failed"）。
    呼び出し元は run の terminal 化（`finish_ingest_run*`）が実際に成功した場合だけ呼ぶ（通知内容と DB の状態を食い違わせない）。
    """
    try:
        webhooks.notify_run_terminal(wid, run_id, "delete", status)
    except Exception:
        _log.warning("Webhook 通知の起動に失敗しました（削除自体は継続）: world=%s", wid, exc_info=True)


def _delete(wid: str, payload: dict, run_id: int) -> None:
    """派生物 wipe＋レジストリ削除。world_lock は `world_admin_service.delete`→`worlds.delete` が内部で 1 回だけ取得する（ここで重ねて取らない）。
    fail-closed: グラフ削除に失敗したら例外になり world 行は残す。成功時は world 行 DELETE と run 完了 UPDATE が同一トランザクションで確定する。
    失敗時はここで `finish_ingest_run` により run を理由付きで閉じる。post-event 監査（`world.deleted`）は実処理の成否が分かるここで行う。
    """
    try:
        store.update_ingest_run_progress(run_id, {
            "stage": "deleting", "stage_label": ingest_worker.STAGE_LABELS["deleting"],
            "done": None, "total": None,
            "updated_at": datetime.now(timezone.utc).isoformat()})
    except Exception:
        _log.warning("進捗の記録に失敗しました（削除自体は継続）: world=%s stage=deleting", wid, exc_info=True)
    uid = payload.get("uid")

    def _fail(reason: str, exc: Exception, log_message: str) -> None:
        # 通知は terminal 更新が実際に成功したときだけ行う（失敗なら run 行は extracting のままで、failed を通知してはいけない）。
        finished = False
        try:
            store.finish_ingest_run(
                run_id, status="failed",
                extraction_snapshot={"flags": [{"doc": None, "action": "blocked", "reason": reason}]})
            finished = True
        except Exception:
            _log.warning("削除失敗の記録自体に失敗しました（best-effort）: world=%s", wid, exc_info=True)
        try:
            store.audit(uid, "world.deleted", "world", f"world:{wid}",
                        outcome="failure", severity="critical", reason=exc.__class__.__name__)
        except Exception:
            pass
        _log.warning(log_message, wid, exc_info=True)
        if finished:
            _notify_delete_terminal(wid, run_id, "failed")

    try:
        world_admin_service.delete(wid, run_id=run_id)
    except world_admin_service.WorldAdminError as exc:  # グラフ削除失敗＝fail-closed（行は残す）
        _fail(f"delete_failed:{exc.__class__.__name__}", exc,
              "背景削除がグラフ削除失敗で中止しました（fail-closed・行は保持）: world=%s")
        return
    except Exception as e:
        _fail(f"unexpected_error:{e.__class__.__name__}", e, "背景削除が想定外の例外で失敗しました: world=%s")
        return
    try:
        store.audit(uid, "world.deleted", "world", f"world:{wid}", outcome="success", severity="critical")
    except Exception:
        pass
    _notify_delete_terminal(wid, run_id, "auto_published")


OPS: dict[str, Callable[[str, dict, int], None]] = {
    "register": _register,
    "refresh": _refresh,
    "rebind": _rebind,
    "rerun": _rerun,
    "rag_regenerate_rules": _rag_regenerate_rules,
    "reconvert": _reconvert,
    "delete": _delete,
}


def run_job(op: str | None, world: str, payload: dict | None, run_id: int) -> None:
    """積まれた依頼 1 件を実行する。未知の操作名は例外（呼び出し側の最外周が run を failed にする）。"""
    fn = OPS.get(op or "")
    if fn is None:
        raise ValueError(f"未対応の取り込み操作です: {op}")
    fn(world, payload or {}, run_id)
