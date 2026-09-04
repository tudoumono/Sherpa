"""非同期処理の完了/要対応の通知（NOTIFY-1・2026-09-02-RAG表現の全形式展開と文脈保持.md 追加起票）。

新しいイベント基盤は作らない——既存のイベント源を**読み出す**だけ:
  a) 取り込み run の完了/失敗（`ingest_runs`・`store.get_latest_run_summary`）。全利用者に見せる。
  b) LLM 成形パスの完了（`usage_events` の `kind="rag_render"`・M1 metering）。admin のみ。
     LLM 呼び出し自体の失敗は `format_document` が握って規則版へ静かに縮退する設計（L5・§8.6-4）のため
     `usage_events` に残らない——ここでは「完了（成形が実際に走った）」の通知のみ扱う（失敗の通知は無い）。
  c) OCR ジョブ群の完了＝rag.md への反映待ち（`ocr_jobs` の状態集計＋`.rag_sig` drift）。admin のみ。
     隔離 OCR worker は `ocr-internal`（`internal: true`）ネットワークのみに属し本体 API へ到達できず、
     `/derived` も read-only（`docker-compose.yml`）——sync を起こせるのは本体プロセス側だけ。
     このモジュールが「反映待ち」を検知した際に、既存の `POST /worlds/{wid}/refresh` と同じ
     `sherpa.ingest.background` 多重起動抑止（world 単位・同一 op/fingerprint は合流）に乗せて
     軽量 sync を1回だけ予約する（`_trigger_ocr_catchup`）——新しい常時ポーリングは追加しない。

旧・グラフ drift 通知（`kind="graph_stale"`・D1c）は意味層フル抽出の撤去（GRAPH-SRC・2026-09-04・
K9-K11）に伴い供給源ごと撤去済み（復活させない）。

既読管理はしない（最小版）: 呼ぶたびに現在の状態から毎回組み立てて返す。admin 限定イベント
（b/c）は `list_notifications(is_admin=...)` が呼び出し側の役割で絞る。
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

# `POST /worlds/{wid}/refresh` の受付処理（`routers/worlds.py::world_refresh`）／`api.py` の
# folder poller と同じ固定 fingerprint（`_fingerprint({})` の結果）。同じ op="refresh" に揃えることで、
# 既に手動更新やポーリング更新が進行中ならそちらへ無害に合流する（多重予約しない）。
_REFRESH_FP = "{}"
_LLM_RENDER_EVENT_LIMIT = 200   # usage_events(kind=rag_render) から遡って読む直近件数（軽量な固定上限）


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _world_labels() -> dict[str, str]:
    return {row["world_id"]: (row.get("label") or row["world_id"]) for row in store.list_worlds_db()}


def _item(*, kind: str, world: str, world_label: str, status: str, message: str, at,
         admin_only: bool, action: dict | None = None) -> dict:
    at_iso = _iso(at) or datetime.now(timezone.utc).isoformat()
    return {
        "id": f"{kind}:{world}:{at_iso}",
        "kind": kind,
        "world": world,
        "world_label": world_label,
        "status": status,          # "done" | "failed" | "warn"
        "message": message,
        "created_at": at_iso,
        "admin_only": admin_only,
        "action": action,
    }


# ---- a) 取り込み run の完了/失敗（全利用者） -----------------------------------------------------

def _ingest_run_notifications(labels: dict[str, str]) -> list[dict]:
    items = []
    for wid, label in labels.items():
        run = store.get_latest_run_summary(wid)
        if not run or run.get("status") == "extracting":
            continue                                  # 実行中・run 未存在は通知しない
        status = str(run.get("status") or "")
        failed = "failed" in status
        message = (f"「{label}」の取り込みに失敗しました。取り込み状況をご確認ください。" if failed
                  else f"「{label}」の取り込みが完了しました。")
        items.append(_item(
            kind="ingest_run", world=wid, world_label=label,
            status="failed" if failed else "done", message=message,
            at=run.get("created_at"), admin_only=False))
    return items


# ---- b) LLM 成形パスの完了（admin のみ） ----------------------------------------------------------

def _llm_render_notifications(labels: dict[str, str]) -> list[dict]:
    latest: dict[str, dict] = {}
    for row in usage_events.list_recent_events("rag_render", limit=_LLM_RENDER_EVENT_LIMIT):
        wid = row.get("world")
        if wid not in labels or wid in latest:            # world 単位で最新の1件だけ・削除済み world は除外
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


# ---- c) OCR ジョブ群の完了＝反映待ち（admin のみ・自動追いつき1回予約込み） -----------------------

def _trigger_ocr_catchup(world_id: str) -> None:
    """OCR 完了の反映待ちを検知した world の軽量 sync を1回だけ予約する（best-effort）。

    `sherpa.ingest.background.start_or_join` の world 単位多重起動抑止（同一 op/fingerprint は
    合流）にそのまま乗る——既に手動更新/ポーリング更新が進行中ならそちらへ合流し、新しいスレッドは
    起動しない。他の操作（extract/delete 等）が進行中なら `ConflictError`＝今回は静かに諦める
    （次に反映待ちが検知された時点で再試行すれば足りる・poller の folder loop と同じ諦め方）。
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
            continue                                       # 公開中の OCR 観測が無い＝対象外
        summary = ocr_jobs.status_summary(wid)
        if summary["targets"] == 0 or summary["pending"] > 0:
            continue                                        # OCR 対象なし、または実行中
        dmd = worlds.derived_md_dir(wid)
        if not office_md.rag_sig_drift(dmd, world=wid):
            continue                                        # 既に rag.md へ反映済み
        _trigger_ocr_catchup(wid)                            # 自動追いつき（O1・§8.1一本化）を1回だけ予約
        items.append(_item(
            kind="ocr_pending", world=wid, world_label=label, status="warn",
            message=f"「{label}」の画像文字認識（OCR）が完了しました。反映には更新が必要です。",
            at=summary.get("updated_at"), admin_only=True,
            action={"label": "更新する", "method": "POST",
                   "path": f"/worlds/{wid}/refresh", "confirm": False}))
    return items


def list_notifications(*, is_admin: bool) -> list[dict]:
    """ホーム画面向けの通知一覧（新しい順）。`is_admin=False` は a) のみ返す。"""
    labels = _world_labels()
    items = _ingest_run_notifications(labels)
    if is_admin:
        items += _llm_render_notifications(labels)
        items += _ocr_notifications(labels)
    items.sort(key=lambda it: it["created_at"], reverse=True)
    return items
