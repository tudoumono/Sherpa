"""会話管理エンドポイント（`GET /conversations`・`GET /conversations/{cid}`・`DELETE /conversations/{cid}`・`POST /conversations/{cid}/pin`・`PATCH /conversations/{cid}`）と専用モデル（`PinReq`/`RenameReq`）。
`sherpa.api` を import しない。
設計: docs/design/chat.md「会話の保存と継続」
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from sherpa import investigation_record_render, store
from sherpa.deps import _current_user, _delete_codex_sessions_for_conversation
from sherpa.providers.codex.turn_prepare import _conversation_lock
from sherpa.store import investigation_records as store_investigation

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
router = APIRouter()


# GET /conversations には response_model を付けない。応答行は DB 列 `version` を含み、response_model を付けると OpenAPI に `version` が露出して `test_openapi_surface_has_no_version_parameter` に反する。応答形は `sherpa.schemas.ConversationSummary` の TypeAdapter 契約（`tests/api/test_mock_api_contract.py`）で固定する。
@router.get("/conversations", tags=["会話管理"])
def conversations_list(request: Request, q: str | None = None):
    """現在ユーザーの会話一覧（所有＋受領共有）を返す。`q`（trim 後 1〜100 字）を指定するとタイトル・本文検索に絞る（省略時は全件）。"""
    u = _current_user(request)
    if q is None:
        return store.list_conversations(u["uid"])
    q = q.strip()
    if not (1 <= len(q) <= 100):
        raise HTTPException(422, "検索語は1〜100字です")
    return store.search_conversations(u["uid"], q)


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
    """会話を削除する（所有会話・受領ラッパーいずれも可）。
    生きた受領ラッパーがこの会話を参照している場合は論理削除（自分の一覧からは消えるが受領側は引き続き読める）、無ければ物理削除する。どちらも Codex の継続セッションは即時に削除する。
    Codex が実行中の会話は DB に触れず 409 を返す（Codex の実行と同じ会話単位のロックを、DB を変える前に待たずに取る）。
    """
    u = _current_user(request)
    lock = _conversation_lock(cid)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "この会話は調査の実行中のため削除できません。終了してから再度お試しください")
    try:
        if not store.delete_conversation(cid, u["uid"]):
            raise HTTPException(404, "会話が見つかりません")
        _delete_codex_sessions_for_conversation(u["uid"], cid)
    finally:
        lock.release()
    return {"ok": True, "id": cid}


class PinReq(BaseModel):
    pinned: bool = True


@router.post("/conversations/{cid}/pin", tags=["会話管理"])
def conversation_pin(cid: int, req: PinReq, request: Request):
    """会話のピン留め状態を変更する（所有会話・受領ラッパーどちらも可）。"""
    u = _current_user(request)
    # pin は所有会話も受領ラッパーも可（どちらも user_id = current.uid）。
    if not store.set_pinned(cid, req.pinned, u["uid"]):
        raise HTTPException(404, "会話が見つかりません")
    return {"ok": True, "id": cid, "pinned": req.pinned}


class RenameReq(BaseModel):
    title: str


@router.patch("/conversations/{cid}", tags=["会話管理"])
def conversation_rename(cid: int, req: RenameReq, request: Request):
    """会話タイトルを変更する（所有会話のみ・受領共有は403）。"""
    u = _current_user(request)
    # rename は所有会話（origin='own'）のみ。受領共有は拒否する。
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


@router.get("/conversations/{cid}/messages/{message_id}/investigation", tags=["会話管理"])
def conversation_investigation_download(cid: int, message_id: int, request: Request,
                                         format: str = Query("md", pattern="^(md|json)$")):
    """調査の記録（回答ごとの調査台帳）をダウンロードする。
    会話の所有者のみ（他人の会話・論理削除済み・共有の閲覧者はすべて 404）。記録が無いメッセージも 404。
    `format=md`（既定）は人が読む Markdown、`format=json` は保存した manifest/items/coverage/reviews をそのまま返す。ファイル名は固定の一般名（質問文・資料名を含めない）。
    """
    u = _current_user(request)
    if not store.owns_assistant_message(u["uid"], cid, message_id):
        raise HTTPException(404, "記録が見つかりません")
    record = store_investigation.get_investigation_record(message_id)
    if record is None:
        raise HTTPException(404, "記録が見つかりません")
    if format == "json":
        body = {"complete": record["complete"], "truncated": record["truncated"],
                "manifest": record["manifest"], "items": record["items"],
                "coverage": record["coverage"], "reviews": record.get("reviews") or []}
        return Response(content=json.dumps(body, ensure_ascii=False, indent=2),
                        media_type="application/json; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="investigation.json"'})
    md = investigation_record_render.render_markdown(record)
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="investigation.md"'})
