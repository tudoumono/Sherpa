"""会話管理エンドポイント（フェーズ3スライス4・純移動）。

`GET /conversations`・`GET /conversations/{cid}`・`DELETE /conversations/{cid}`・
`POST /conversations/{cid}/pin`・`PATCH /conversations/{cid}`＋専用モデル（`PinReq`/`RenameReq`）
を api.py から抽出する。ロジックは変更しない（コード移動のみ）。ルート表 golden の定義順を保つため、
api.py 側は削除したブロックの元位置に `app.include_router(conversations.router)` を1回だけ置く。

このモジュールは `sherpa.api` を import しない（循環回避）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from sherpa import store
from sherpa.deps import _current_user

# router に tags を持たせない: 各エンドポイントの `tags=["会話管理"]` と結合されて
# 二重化してしまう（ルート表 golden 不一致の原因）ため、tags 指定は各デコレータ側のみに残す
# （system.py:42-44 と同じパターン）。
router = APIRouter()


# GET /conversations には response_model を付与しない: 応答行（store.list_conversations）は DB 列
# `version`（歴史的名称・世代/世界 ID の実体・語彙統一のスコープ外＝DB 不変）をそのまま含み、
# response_model を付与すると OpenAPI スキーマに `version` プロパティが露出する。
# `tests/api/test_world_param_compat.py::test_openapi_surface_has_no_version_parameter` が
# 「退役した version 概念を API surface に再宣言しない」契約を pin しているため、このルートは
# `sherpa.schemas.ConversationSummary` を TypeAdapter 契約のみで固定する
# （`tests/api/test_response_schemas.py` 参照・response_model 非付与）。
@router.get("/conversations", tags=["会話管理"])
def conversations_list(request: Request):
    """現在ユーザーの会話一覧（所有＋受領共有）を返す。"""
    u = _current_user(request)
    return store.list_conversations(u["uid"])


@router.get("/conversations/{cid}", tags=["会話管理"])
def conversation_get(cid: int, request: Request):
    """会話1件の詳細（メッセージ履歴込み）を返す。他人の会話は404。"""
    u = _current_user(request)
    conv = store.get_conversation_for_read(u["uid"], cid)
    if not conv:
        raise HTTPException(404, "会話が見つかりません")
    return conv


@router.delete("/conversations/{cid}", tags=["会話管理"])
def conversation_delete(cid: int, request: Request):
    """会話を削除（所有会話・受領ラッパーいずれも削除可）。

    生きた受領ラッパーがこの会話を参照している場合は soft-delete（自分の一覧からは消えるが
    受領側は引き続き読める）、参照が無ければ物理削除（store.delete_conversation 参照）。
    """
    u = _current_user(request)
    if not store.delete_conversation(cid, u["uid"]):
        raise HTTPException(404, "会話が見つかりません")
    return {"ok": True, "id": cid}


class PinReq(BaseModel):
    pinned: bool = True


@router.post("/conversations/{cid}/pin", tags=["会話管理"])
def conversation_pin(cid: int, req: PinReq, request: Request):
    """会話のピン留め状態を変更（所有会話・受領ラッパーどちらも可）。"""
    u = _current_user(request)
    # pin は所有会話も受領ラッパーも可（どちらも user_id = current.uid）。
    if not store.set_pinned(cid, req.pinned, u["uid"]):
        raise HTTPException(404, "会話が見つかりません")
    return {"ok": True, "id": cid, "pinned": req.pinned}


class RenameReq(BaseModel):
    title: str


@router.patch("/conversations/{cid}", tags=["会話管理"])
def conversation_rename(cid: int, req: RenameReq, request: Request):
    """会話タイトルを変更（所有会話のみ・受領共有は403）。"""
    u = _current_user(request)
    # rename は所有会話（origin='own'）のみ。受領共有は拒否。
    if not store.owns_conversation(u["uid"], cid):
        # 受領共有かどうか確認して 403/404 を区別する。
        conv = store.get_conversation_for_read(u["uid"], cid)
        if conv and conv["conversation"].get("origin") == "received_share":
            raise HTTPException(403, "共有された会話のタイトルは変更できません")
        raise HTTPException(404, "会話が見つかりません")
    title = (req.title or "").strip()[:120]
    if not title:
        raise HTTPException(422, "タイトルが空です")
    if not store.rename_conversation(cid, title, u["uid"]):
        raise HTTPException(404, "会話が見つかりません")
    return {"ok": True, "id": cid, "title": title}
