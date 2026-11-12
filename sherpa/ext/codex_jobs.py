"""Codex ジョブ（非同期）: 受付・状態照会・結果取得・取消の4経路。実行本体は `sherpa/store/codex_jobs.py`／`sherpa/codex_jobs_worker.py`。
設計: docs/design/external-api.md
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from sherpa import codex_jobs_worker, path_rules, scope_infer, store
from sherpa import scope as scope_mod
from sherpa.ext.audit import _AuditScope
from sherpa.ext.auth import _enforce_world_scope, require_api_key
from sherpa.ext.common import ExtSourceItem, _resolve_world_or_error, router
from sherpa.ext.request_id import _REQUEST_ID_OPENAPI_HEADER, _validation_error_response, _XRequestIdIn
from sherpa.store import codex_jobs as store_jobs

_log = logging.getLogger("sherpa")

# ==== Codex ジョブ（非同期・`C-EXT-CODEXJOB-*`）====
# 受付（`POST /codex/jobs`）→状態照会（`GET /codex/jobs/{id}`）→結果取得（`GET /codex/jobs/{id}/result`）→取消（`POST /codex/jobs/{id}/cancel`）の4段。
# 実行本体は `sherpa/store/codex_jobs.py`／`sherpa/codex_jobs_worker.py`（本ファイルは `sherpa.agents`/`sherpa.chat_service` を import しないため、実行可否の判定も `codex_jobs_worker.check_available()` へ委譲する）。
# 「資料フォルダスコープ外は403」が通用するのは受付だけ。状態照会・結果取得・取消は、`job_id` が存在しない場合も鍵の scope 外の場合も 404（ジョブの存在を漏らさない）


def _codex_job_visible(job: dict | None, key: dict) -> bool:
    """このジョブをこの鍵から見てよいか。ジョブは受け付けた鍵（key_id）に属し、別の鍵からは見えない。照会の時点で鍵の `allowed_worlds` から外れていれば見えない。"""
    if job is None:
        return False
    if job["key_id"] != key["key_id"]:
        return False
    allowed = key.get("allowed_worlds")
    return allowed is None or job["world"] in allowed


def _codex_job_not_found() -> HTTPException:
    return HTTPException(404, "ジョブが見つかりません")


def _iso(dt) -> str | None:
    return None if dt is None else str(dt)


_CODEX_JOB_RETRY_AFTER = {"queued": "30", "running": "60"}  # 終了状態は付けない


class ExtCodexJobSubmitReq(BaseModel):
    # 契約外の項目は黙って捨てず 422
    model_config = ConfigDict(extra="forbid")
    world: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=1000)
    scope_paths: list[str] = Field(default_factory=list)  # フォルダ prefix
    # チャットと同じ調べる深さ（`depth_profile.DEPTH_PROFILES`）。省略時 "standard"。不正値は自動 422
    depth: Literal["quick", "standard", "deep", "max"] | None = None
    # 終了通知。`true` は鍵に通知先が登録済みのときだけ受け付ける（送れない通知を受け付けない）
    webhook: bool = False


class ExtCodexJobSubmitRes(BaseModel):
    job_id: str
    status: Literal["queued"]


_CODEX_JOB_SUBMIT_RESPONSES = {
    202: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    403: {"description": "このキーはこの world へのアクセスを許可されていません（world スコープ外）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "資料フォルダ（world）が見つかりません", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("webhook=true なのに鍵に通知先が登録されていない場合、または不明な範囲（scope_paths）が指定された場合"),
    429: {"description": "レート制限を超過しました、または待ち列が上限（全体・鍵ごと）に達しています（`Retry-After` 秒後に再試行）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    503: {"description": "Codex が今実行できません（CLI未接続・未設定・サンドボックス不可等）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


@router.post("/codex/jobs", status_code=202, response_model=ExtCodexJobSubmitRes,
            responses=_CODEX_JOB_SUBMIT_RESPONSES)
def ext_codex_job_submit(req: ExtCodexJobSubmitReq, request: Request,
                         key: dict = Depends(require_api_key),
                         x_request_id: str | None = _XRequestIdIn):
    """Codex ジョブの受付。共有 KB のみ・個人 workspace は対象外。"""
    del x_request_id
    with _AuditScope(request, "ext_api.codex_job_submit", "ext_codex_job") as audit:
        audit.resource_id = req.world
        audit.detail.update({"world": req.world, "query": req.query[:200],
                             "depth": req.depth or "standard"})
        if req.webhook and store.get_api_key_webhook(key["key_id"]) is None:
            raise HTTPException(
                422, "この鍵には通知先（Webhook）が登録されていないため、webhook=true は指定できません")
        sp = scope_mod.normalize_scope_paths(req.scope_paths)
        audit.detail["prefix"] = sp
        _enforce_world_scope(request, key, req.world)  # scope 外は世界の存在有無を明かさず先に 403
        root = _resolve_world_or_error(req.world)
        try:
            if not scope_mod.valid_scope_paths(req.world, req.scope_paths, root=root, strict=True):
                raise HTTPException(422, "不明な範囲（scope_paths）が指定されました")
        except scope_infer.ScopeWalkDeadlineExceeded:
            raise HTTPException(503, "資料フォルダの一覧を時間内に取得できませんでした。取り込みが終わってから、もう一度お試しください") from None
        except OSError as e:
            raise HTTPException(
                503, "資料フォルダの走査中にエラーが発生しました（一時的な障害の可能性があります）") from e
        try:
            codex_jobs_worker.check_available()
        except codex_jobs_worker.CodexUnavailable as e:
            _log.warning("ext_api codex job submit: Codex unavailable: %s", e)
            raise HTTPException(503, "Codex が今実行できません（管理者に接続状況の確認を依頼してください）") from None
        depth = req.depth or "standard"
        job_id = store_jobs.new_job_id()
        try:
            row = store_jobs.insert_job(job_id=job_id, key_id=key["key_id"], world=req.world,
                                        query=req.query, scope_paths=sp, depth=depth,
                                        webhook=req.webhook)
        except store_jobs.QueueFull as e:
            audit.pending["reason"] = "queue_full"
            audit.detail["queue_scope"] = e.scope
            raise HTTPException(429, "待ち列がいっぱいです。しばらくしてからもう一度お試しください",
                                headers={"Retry-After": _CODEX_JOB_RETRY_AFTER["queued"]}) from None
        audit.resource_id = row["id"]
        return {"job_id": row["id"], "status": row["status"]}


# ---- GET /codex/jobs/{job_id}（状態照会）----

class ExtCodexJobStatusRes(BaseModel):
    job_id: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    expires_at: str | None = None
    completion: str | None = None  # 終端のとき: complete／partial（途中までの回答）／failed
    stop_kind: str | None = None  # 終了理由（閉じた語彙）


_CODEX_JOB_STATUS_RESPONSES = {
    200: {"headers": {**_REQUEST_ID_OPENAPI_HEADER,
                      "Retry-After": {"schema": {"type": "string"},
                                      "description": "次に見に来るまでの目安秒数（queued/running のみ）"}}},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "ジョブが見つかりません（別の鍵のジョブ・scope外・存在しない、のいずれも同じ404）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


@router.get("/codex/jobs/{job_id}", response_model=ExtCodexJobStatusRes,
           response_model_exclude_none=True, responses=_CODEX_JOB_STATUS_RESPONSES)
def ext_codex_job_status(job_id: str, request: Request, response: Response,
                         key: dict = Depends(require_api_key),
                         x_request_id: str | None = _XRequestIdIn):
    """Codex ジョブの状態照会。"""
    del x_request_id
    with _AuditScope(request, "ext_api.codex_job_status", "ext_codex_job") as audit:
        audit.resource_id = job_id
        job = store_jobs.get_job(job_id)
        if not _codex_job_visible(job, key):
            raise _codex_job_not_found()
        job = store_jobs.expire_job_if_due(job_id, store_jobs.fail_if_overdue(job))
        audit.detail.update({"world": job["world"], "status": job["status"]})
        retry_after = _CODEX_JOB_RETRY_AFTER.get(job["status"])
        if retry_after is not None:
            response.headers["Retry-After"] = retry_after
        return {"job_id": job["id"], "status": job["status"], "created_at": _iso(job["created_at"]),
                "started_at": _iso(job["started_at"]), "finished_at": _iso(job["finished_at"]),
                "expires_at": _iso(job["expires_at"]), "completion": job.get("completion"),
                "stop_kind": job.get("stop_kind")}


# ---- GET /codex/jobs/{job_id}/result（結果取得）----

class ExtCodexJobUnconfirmedItem(BaseModel):
    """調査で確認できなかった項目。`reason` は確認できなかった理由の定型文、または null。"""
    item: str
    reason: str | None = None


class ExtInvestigationRecord(BaseModel):
    """調査の記録。`manifest`（`question_kind`/`created_at`/`items`）・`items`/`coverage`（id→値の dict）・`reviews`（調査の途中の見直しの配列）。資料名は運ばない。"""
    complete: bool
    truncated: bool
    manifest: dict | None = None
    items: dict = {}
    coverage: dict = {}
    reviews: list = []


class ExtCodexJobResultRes(BaseModel):
    answer: str
    sources: list[ExtSourceItem]  # 出典の共通スキーマ（`ExtSourceItem`・`C-EXT-ANSWER-01` と同一）
    unconfirmed_items: list[ExtCodexJobUnconfirmedItem]
    elapsed_ms: int
    completion: str | None = None  # complete／partial（途中までの回答。不足があり得る）
    stop_kind: str | None = None  # 終了理由（閉じた語彙）
    investigation: ExtInvestigationRecord | None = None  # 記録が無ければ省く（response_model_exclude_none）


_CODEX_JOB_RESULT_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "ジョブが見つかりません（別の鍵のジョブ・scope外・存在しない、のいずれも同じ404）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    409: {"description": "まだ完了していない、または失敗/取消済みです（`status`／失敗時のみ `error_code`）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    410: {"description": "保存期間（7日）を過ぎ、結果は消去済みです", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


@router.get("/codex/jobs/{job_id}/result", response_model=ExtCodexJobResultRes,
            response_model_exclude_none=True, responses=_CODEX_JOB_RESULT_RESPONSES)
def ext_codex_job_result(job_id: str, request: Request, key: dict = Depends(require_api_key),
                         x_request_id: str | None = _XRequestIdIn):
    """Codex ジョブの結果取得。`completed` のときだけ200（409/410 は本文形が異なるため dict をそのまま返す）。"""
    del x_request_id
    with _AuditScope(request, "ext_api.codex_job_result", "ext_codex_job") as audit:
        audit.resource_id = job_id
        job = store_jobs.get_job(job_id)
        if not _codex_job_visible(job, key):
            raise _codex_job_not_found()
        job = store_jobs.expire_job_if_due(job_id, store_jobs.fail_if_overdue(job))
        audit.detail.update({"world": job["world"], "status": job["status"]})
        status = job["status"]
        if status == "expired":
            return JSONResponse(status_code=410, content={"status": "expired"})
        if status != "completed":
            body = {"status": status}
            if status == "failed" and job.get("error_code"):
                body["error_code"] = job["error_code"]
            body.update({k: job[k] for k in ("completion", "stop_kind") if job.get(k)})
            # 契約どおりの形（`{"status", "error_code"?}`）をそのまま返すため `JSONResponse` を直接使う（`HTTPException` だと `{"detail": body}` に包まれる）
            audit.business_outcome = "failed"
            return JSONResponse(status_code=409, content=body)
        return path_rules.filter_output({"answer": job.get("answer") or "", "sources": list(job.get("sources") or []),
                                         "unconfirmed_items": list(job.get("unconfirmed_items") or []), "elapsed_ms": job.get("elapsed_ms") or 0, "completion": job.get("completion"),
                                         "stop_kind": job.get("stop_kind"), "investigation": job.get("investigation")}, job.get("world") or "")


# ---- POST /codex/jobs/{job_id}/cancel（取消）----

class ExtCodexJobCancelRes(BaseModel):
    status: str


_CODEX_JOB_CANCEL_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "ジョブが見つかりません（別の鍵のジョブ・scope外・存在しない、のいずれも同じ404）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


@router.post("/codex/jobs/{job_id}/cancel", response_model=ExtCodexJobCancelRes,
            responses=_CODEX_JOB_CANCEL_RESPONSES)
def ext_codex_job_cancel(job_id: str, request: Request, key: dict = Depends(require_api_key),
                         x_request_id: str | None = _XRequestIdIn):
    """Codex ジョブの取消。冪等。`queued` は即 `cancelled`、`running` は先に DB で `cancelled` を原子的に確定してから停止を通知する。
    終端状態（completed/failed/cancelled/expired）への要求は何もせず今の状態を返す。
    取消と完了が競合したときは、先に DB 行を `running` から動かした方が勝つ（取消したのに結果が返る・完了したのに cancelled と返る、のどちらも起こさない）。
    """
    del x_request_id
    with _AuditScope(request, "ext_api.codex_job_cancel", "ext_codex_job") as audit:
        audit.resource_id = job_id
        job = store_jobs.get_job(job_id)
        if not _codex_job_visible(job, key):
            raise _codex_job_not_found()
        job = store_jobs.fail_if_overdue(job)
        audit.detail.update({"world": job["world"], "status_before": job["status"]})
        if job["status"] == "queued":
            if store_jobs.cancel_if_queued(job_id):
                return {"status": "cancelled"}
            # 競合でこの一瞬の間に running 等へ進んだ場合は下の再読込へ進む
            job = store_jobs.get_job(job_id) or job
        if job["status"] == "running":
            # DB 側の確定が先。ワーカーの `mark_completed`/`mark_failed` と同じ `WHERE status='running'` 条件で競い、勝った側だけが行を書き換える
            if store_jobs.mark_cancelled(job_id, from_status="running"):
                codex_jobs_worker.signal_cancel(job_id)  # 確定後に停止を通知（best-effort）
            job = store_jobs.get_job(job_id) or job
        job = store_jobs.expire_job_if_due(job_id, job)
        audit.detail["status_after"] = job["status"]
        return {"status": job["status"]}
