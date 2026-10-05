"""非同期処理の完了/要対応の通知。既存のイベント源を読み出すだけで、既読管理はしない（呼ぶたびに現在の状態から組み立てる）。

  a) 取り込み run の完了/失敗（`ingest_runs`）。全利用者に見せる。
  b) LLM 成形パスの完了（`usage_events` の `kind="rag_render"`）。admin のみ（失敗は通知しない）。
  c) OCR ジョブ群の完了＝rag.md への反映待ち（`ocr_jobs` の状態集計＋`.rag_sig` drift）。admin のみ。
     反映待ちを検知したら `sherpa.ingest.background` の多重起動抑止に乗せて軽量 sync を1回だけ予約する（`_trigger_ocr_catchup`）。
  d) 自分が所有する共有の期限が近い通知。所有者のみ。
admin 限定イベントは `list_notifications(is_admin=...)` が絞る。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import store, world_admin_service, worlds
from .ingest import background as ingest_background
from .ingest import office_md
from .ingest import worker as ingest_worker
from .store import ocr_jobs, usage_events

_log = logging.getLogger("sherpa")

# `POST /worlds/{wid}/refresh` と同じ固定 fingerprint（進行中の更新へ合流させ、多重予約しない）
_REFRESH_FP = "{}"
_LLM_RENDER_EVENT_LIMIT = 200  # usage_events(kind=rag_render) から遡って読む直近件数


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


_UNNAMED_WORLD_LABEL = "名称未設定の資料フォルダ"


def _world_labels() -> dict[str, str]:
    # world_id を生で見せない。ラベル未設定（空/null）のときだけ平文のプレースホルダに丸める
    out = {}
    for row in store.list_worlds_db():
        wid = row["world_id"]
        label = row.get("label") or ""
        out[wid] = label if label else _UNNAMED_WORLD_LABEL
    return out


def _item(*, kind: str, world: str, world_label: str, status: str, message: str, at,
         admin_only: bool, action: dict | None = None, link: str | None = None) -> dict:
    at_iso = _iso(at) or datetime.now(timezone.utc).isoformat()
    return {
        "id": f"{kind}:{world}:{at_iso}",
        "kind": kind,
        "world": world,
        "world_label": world_label,
        "status": status,
        "message": message,
        "created_at": at_iso,
        "admin_only": admin_only,
        "action": action,
        "link": link,
    }


# ---- a) 取り込み run の完了/失敗（全利用者） ----

def _ingest_run_notifications(labels: dict[str, str]) -> list[dict]:
    items = []
    for wid, label in labels.items():
        run = store.get_latest_run_summary(wid)
        if not run or run.get("status") == "extracting":
            continue
        status = str(run.get("status") or "")
        failed = "failed" in status
        message = (f"「{label}」の取り込みに失敗しました。取り込み状況をご確認ください。" if failed
                  else f"「{label}」の取り込みが完了しました。")
        items.append(_item(
            kind="ingest_run", world=wid, world_label=label,
            status="failed" if failed else "done", message=message,
            at=run.get("created_at"), admin_only=False))
    return items


# ---- b) LLM 成形パスの完了（admin のみ） ----

def _llm_render_notifications(labels: dict[str, str]) -> list[dict]:
    latest: dict[str, dict] = {}
    for row in usage_events.list_recent_events("rag_render", limit=_LLM_RENDER_EVENT_LIMIT):
        wid = row.get("world")
        if wid not in labels or wid in latest:  # world 単位で最新の1件だけ・削除済み world は除外
            continue
        latest[wid] = row
    items = []
    for wid, row in latest.items():
        label = labels[wid]
        items.append(_item(
            kind="llm_render", world=wid, world_label=label, status="done",
            message=f"「{label}」の文書のAI整形が完了しました。",
            at=row.get("ts"), admin_only=True))
    return items


# ---- c) OCR ジョブ群の完了＝反映待ち（admin のみ・自動追いつき1回予約込み） ----

def _trigger_ocr_catchup(world_id: str) -> None:
    """OCR 完了の反映待ちを検知した world の軽量 sync を1回だけ予約する（best-effort）。

    `background.start_or_join` の world 単位多重起動抑止に乗る。他の操作が進行中（`ConflictError`）なら静かに諦める。
    """
    def _create_run() -> int:
        row = store.start_ingest_run(
            world_id, scan_root=None, created_by="admin",
            progress={"stage": "accepted", "stage_label": ingest_worker.STAGE_LABELS["accepted"],
                     "done": None, "total": None, "updated_at": datetime.now(timezone.utc).isoformat()})
        return row["id"]

    try:
        ingest_background.start_or_join(
            world_id, "refresh", _REFRESH_FP, _create_run,
            lambda run_id: world_admin_service.refresh(world_id, run_id=run_id))
    except (ingest_background.ConflictError, ingest_background.ShuttingDownError):
        pass
    except Exception:
        _log.warning(
            "OCR完了の反映（軽量sync）予約に失敗しました（次回検知時に再試行）: world=%s",
            world_id, exc_info=True)


def _ocr_notifications(labels: dict[str, str]) -> list[dict]:
    if not office_md.ocr_enabled():
        return []
    items = []
    for wid, label in labels.items():
        if worlds.observation_current_dir(wid) is None:
            continue
        summary = ocr_jobs.status_summary(wid)
        if summary["targets"] == 0 or summary["pending"] > 0:
            continue
        dmd = worlds.derived_md_dir(wid)
        if not office_md.rag_sig_drift(dmd, world=wid):
            continue
        _trigger_ocr_catchup(wid)  # 自動追いつきを1回だけ予約
        items.append(_item(
            kind="ocr_pending", world=wid, world_label=label, status="warn",
            message=f"「{label}」の画像文字認識（OCR）が完了しました。反映には更新が必要です。",
            at=summary.get("updated_at"), admin_only=True,
            action={"label": "更新する", "method": "POST",
                   "path": f"/worlds/{wid}/refresh", "confirm": False}))
    return items


_SHARE_LABEL = "会話の共有"


def _share_expiry_notifications(uid: str) -> list[dict]:
    """d) 自分が所有する共有の期限が近い（7 日以内）通知。所有者本人にだけ出す。"""
    now = datetime.now(timezone.utc)
    items = []
    for r in store.list_expiring_shares_for_owner(uid):
        exp = r["expires_at"]
        days = max(1, -(-int((exp - now).total_seconds()) // 86400))  # 切り上げ（残り 0 日表示を避ける）
        title = r.get("title") or "会話"
        items.append(_item(
            kind="share_expiring", world=str(r["share_id"]), world_label=_SHARE_LABEL, status="warn",
            message=f"共有『{title}』の期限が{days}日後（{exp.astimezone().strftime('%Y-%m-%d')}）に切れます。延長できます。",
            at=now, admin_only=False,
            link=f"/ui/chat.html?conv={r['conversation_id']}&share=1"))
    return items


def list_notifications(*, is_admin: bool, uid: str | None = None) -> list[dict]:
    """ホーム画面向けの通知一覧（新しい順）。`is_admin=False` は a)（＋uid 指定時は d）のみ返す。"""
    labels = _world_labels()
    items = _ingest_run_notifications(labels)
    if uid:
        items += _share_expiry_notifications(uid)
    if is_admin:
        items += _llm_render_notifications(labels)
        items += _ocr_notifications(labels)
    items.sort(key=lambda it: it["created_at"], reverse=True)
    return items
