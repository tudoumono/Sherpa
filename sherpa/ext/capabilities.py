"""GET /ext/v1/capabilities: 資料フォルダごとの機能一覧（discovery）。
設計: docs/design/external-api.md
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from sherpa import codex_jobs_worker, simple_chat, store, worlds
from sherpa.ext.audit import _AuditScope
from sherpa.ext.auth import require_api_key
from sherpa.ext.common import router
from sherpa.ext.request_id import _REQUEST_ID_OPENAPI_HEADER, _validation_error_response, _XRequestIdIn


class ExtCapability(BaseModel):
    configured: bool
    available: bool
    reason: str | None = None


class ExtCapabilities(BaseModel):
    search: ExtCapability
    answer: ExtCapability
    codex_jobs: ExtCapability
    embed: ExtCapability
    convert: ExtCapability


class ExtWorldInfo(BaseModel):
    world: str
    document_count: int | None = None
    last_updated: str | None = None
    capabilities: ExtCapabilities


class ExtCapabilitiesRes(BaseModel):
    worlds: list[ExtWorldInfo]


def _cap(configured: bool, available: bool, reason: str | None = None) -> dict:
    """`reason` は configured/available のどちらかが偽のときだけ付ける。"""
    out: dict = {"configured": configured, "available": available}
    if not (configured and available):
        out["reason"] = reason or ("not_configured" if not configured else "backend_unreachable")
    return out


def _answer_capability() -> dict:
    """`/ext/v1/answer` の既定 AI（管理者設定）が解決でき、接続材料（鍵・接続先）が揃うか。"""
    try:
        ss = store.get_system_settings()
        provider, _model = simple_chat.resolve_model_and_provider(None, ss)
        if provider == "openai":
            simple_chat._connect_openai(ss)
        else:
            simple_chat._connect_ollama(ss)
    except Exception:
        return _cap(False, False, "not_configured")
    return _cap(True, True)


def _capability_set(codex: dict, answer: dict) -> dict:
    """資料フォルダごとの機能一覧（5つ固定）。`available` は管理者の構成（`configured`）と接続の可否で決まる。検索は常に実行可能、変換は管理者設定を持たず、埋め込みは未設定として返す。"""
    return {"search": _cap(True, True), "answer": dict(answer), "codex_jobs": dict(codex),
            "embed": _cap(False, False, "not_configured"), "convert": _cap(True, True)}


_CAPABILITIES_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("入力パラメータが不正な場合"),
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    503: {"description": "world 一覧を確認できませんでした（一時的な障害の可能性があります）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


# `reason` は省略されうる項目（未指定は出力しない）、`document_count`/`last_updated` は明示の null を返す
@router.get("/capabilities", response_model=ExtCapabilitiesRes, response_model_exclude_unset=True,
            responses=_CAPABILITIES_RESPONSES)
def ext_capabilities(request: Request, key: dict = Depends(require_api_key),
                     x_request_id: str | None = _XRequestIdIn):
    """discovery: 資料フォルダごとに `{world, document_count, last_updated, capabilities}` を返す（機能は search/answer/codex_jobs/embed/convert の5つ固定・各 `{configured, available, reason?}`）。

    `key["allowed_worlds"]` が非 None ならそのスコープ内の world だけを返す（スコープ外 world の存在を漏らさない）。
    `document_count` は取り込み成功確定時に記録された事前集計値、`last_updated` も取り込み成功確定の時刻で、未確定（一度も成功同期していない）なら `null`（0 に潰さない）。
    ファイルツリーは走査しない。
    """
    del x_request_id
    with _AuditScope(request, "ext_api.capabilities", "ext_capabilities") as audit:
        try:
            registry_rows = {r["world_id"]: r for r in store.list_worlds_db()}
        except Exception as e:
            raise HTTPException(
                503, "world 一覧を確認できませんでした（一時的な障害の可能性があります）") from e
        try:
            fs_ids = worlds.discover_fs_world_ids_strict()
        except worlds.ExternalResolverError as e:
            raise HTTPException(
                503, "world 一覧を確認できませんでした（一時的な障害の可能性があります）") from e
        ids = sorted(set(registry_rows) | set(fs_ids))
        allowed = key.get("allowed_worlds")
        ids = [w for w in ids if allowed is None or w in allowed]
        out = []
        caps = None  # 機能の判定は1リクエストで1回だけ（資料フォルダごとに繰り返さない）
        for wid in ids:
            row = registry_rows.get(wid)  # 同一スナップショットのみ参照（再照会しない）
            # row が None なら「未登録」として fixtures/dev のみ試す
            try:
                res = worlds.resolve_external_world(wid, registry_row=row)
            except worlds.ExternalResolverError as e:
                if row is not None:
                    # 登録済み world の root に到達できないものを静かに外すと「実在しない」と区別が付かないため、503 にする。fixtures/dev のみの未登録候補（row is None）は静かに外す
                    raise HTTPException(
                        503, "world の実在を確認できませんでした（一時的な障害の可能性があります）") from e
                continue
            if res.status != "ok":
                continue
            doc_count = None
            last_updated = None
            if row and row.get("last_synced_at") and row.get("last_sig"):
                # last_sig が空＝取り込み開始時の pre-invalidate のまま（進行中/未確定）。非空＝成功確定後の署名で、確定済みのときだけ値を報告する
                last_updated = str(row["last_synced_at"])
                doc_count = row.get("last_doc_count")
            if caps is None:
                caps = (_cap(*codex_jobs_worker.capability()), _answer_capability())
            out.append({"world": wid, "document_count": doc_count, "last_updated": last_updated,
                        "capabilities": _capability_set(*caps)})
        audit.detail["result_count"] = len(out)
        return {"worlds": out}
