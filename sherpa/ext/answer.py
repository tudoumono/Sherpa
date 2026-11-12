"""POST /ext/v1/answer: 簡易チャットの同期応答（`sherpa/simple_chat.py` を呼ぶだけ）。
設計: docs/design/external-api.md
"""
from __future__ import annotations

import time

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from sherpa import path_rules, scope_infer, simple_chat, worlds
from sherpa import scope as scope_mod
from sherpa.ext.audit import _AuditScope
from sherpa.ext.auth import _enforce_world_scope, require_api_key
from sherpa.ext.common import ExtSourceItem, _resolve_world_or_error, router
from sherpa.ext.request_id import _REQUEST_ID_OPENAPI_HEADER, _validation_error_response, _XRequestIdIn

_ANSWER_TIMEOUT_S = 120  # 回答全体の絶対期限（秒）


class ExtAnswerReq(BaseModel):
    # 契約外の項目（model・provider 等）は黙って捨てず 422（指定が効いたと誤解させない）
    model_config = ConfigDict(extra="forbid")
    world: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=1000)
    scope_paths: list[str] = Field(default_factory=list)  # フォルダ prefix


class ExtAnswerRes(BaseModel):
    answer: str
    sources: list[ExtSourceItem]
    unconfirmed: bool
    unconfirmed_reason: str | None = None
    tool_calls: int
    elapsed_ms: int


_ANSWER_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    403: {"description": "このキーはこの world へのアクセスを許可されていません（world スコープ外）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "資料フォルダ（world）が見つかりません", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("不明な範囲（scope_paths）が指定された場合"),
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    503: {"description": "既定の AI が使えません（未接続・未設定。フォールバックはしません）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    504: {"description": f"回答が制限時間（{_ANSWER_TIMEOUT_S}秒）内に完了しませんでした",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


@router.post("/answer", response_model=ExtAnswerRes, response_model_exclude_none=True, responses=_ANSWER_RESPONSES)
def ext_answer(req: ExtAnswerReq, request: Request, key: dict = Depends(require_api_key),
              x_request_id: str | None = _XRequestIdIn):
    """簡易チャットの同期応答。共有 KB のみ・個人 workspace は対象外。

    回答全体に 120 秒の絶対期限があり、world の解決・scope_paths の検証・回答の生成がその同じ期限を共有する（超えると 504）。
    `model`/`provider` は入力に持たず、管理者設定の既定 AI を使う。
    """
    del x_request_id
    with _AuditScope(request, "ext_api.answer", "ext_answer") as audit:
        audit.resource_id = req.world
        audit.detail.update({"world": req.world, "query": req.query[:200]})
        sp = scope_mod.normalize_scope_paths(req.scope_paths)
        audit.detail["prefix"] = sp
        deadline = time.monotonic() + _ANSWER_TIMEOUT_S

        def _remaining() -> float:
            return deadline - time.monotonic()

        def _deadline_exceeded() -> HTTPException:
            return HTTPException(
                504, f"回答が制限時間（{_ANSWER_TIMEOUT_S}秒）内に完了しませんでした")

        _enforce_world_scope(request, key, req.world)  # scope 外は世界の存在有無を明かさず先に 403
        root = _resolve_world_or_error(
            req.world, connect_timeout=max(0.001, _remaining()),
            statement_timeout_ms=max(1, int(_remaining() * 1000)))
        if _remaining() <= 0:
            raise _deadline_exceeded()
        try:
            if not scope_mod.valid_scope_paths(req.world, req.scope_paths, root=root, strict=True,
                                               deadline=deadline):
                if _remaining() <= 0:
                    raise _deadline_exceeded()
                raise HTTPException(422, "不明な範囲（scope_paths）が指定されました")
        except scope_infer.ScopeWalkDeadlineExceeded:
            raise _deadline_exceeded() from None
        except OSError as e:
            if _remaining() <= 0:
                raise _deadline_exceeded() from e
            raise HTTPException(
                503, "資料フォルダの走査中にエラーが発生しました（一時的な障害の可能性があります）") from e
        if _remaining() <= 0:
            raise _deadline_exceeded()
        try:
            # 入口で1回だけ解決した root に固定する（道具の中で registry を引き直さない）
            with worlds.pin_world_root(req.world, root):
                result = simple_chat.answer(
                    world=req.world, query=req.query, scope_paths=req.scope_paths,
                    key_id=key["key_id"], absolute_deadline=deadline)
        except simple_chat.AnswerTimeout as e:
            raise HTTPException(504, str(e)) from None
        except simple_chat.LLMUnavailable as e:
            audit.detail["reason"] = str(e)
            raise HTTPException(503, str(e)) from None
        audit.detail.update({"tool_calls": result["tool_calls"], "unconfirmed": result["unconfirmed"],
                             "result_count": len(result["sources"])})
        return path_rules.filter_output(result, req.world)
