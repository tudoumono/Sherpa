"""フォルダの置き方の点検: `GET /worlds/{wid}/folder-check`（管理者のみ・読み取りだけ）。
名前の一覧は取り込みの台帳から出し（資料フォルダは歩かない）、変更一覧だけ資料フォルダから読む。
`sherpa.api` を import しない。
設計: docs/design/scope.md「資料フォルダの登録・解決・付け替え（registry）」
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import Response

from sherpa import doc_ledger, documents, store, worlds
from sherpa.deps import _WORLD_PATTERN, _current_user, _require_admin
from sherpa.fd_response import content_disposition
from sherpa.ingest.period_text import READ_LIMIT_BYTES, read_text_via  # noqa: F401
from sherpa.ingest.check import inspect_folder, to_json, to_markdown

_log = logging.getLogger("sherpa")

NO_LEDGER_MESSAGE = "取り込みが済んでいないため点検できません。先に取り込んでください。"

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
router = APIRouter()


def read_text_of(world: str):
    """変更一覧を読む関数（`period_text.read_text_via`）。"""
    return read_text_via(lambda rel: documents.resolve(rel, world))


def _ledger_at(world: str):
    try:
        at = store.read_ledger_for_listing(world)["ledger_at"]
    except Exception:
        _log.warning("folder-check: 台帳の時点を読めませんでした world=%s", world, exc_info=True)
        return None
    return at.isoformat() if at else None


@router.get("/worlds/{wid}/folder-check", tags=["資料フォルダ(World)管理"])
def folder_check(request: Request, wid: str = Path(..., pattern=_WORLD_PATTERN),
                 format: str = Query("json", pattern="^(json|md)$")):
    """資料フォルダの置き方が、期ごとにフォルダを分ける置き方の決まりに合うかを点検する。`format=md` は Markdown のダウンロード。
    次の取り込み直しの版で使う置き方の確認で、今の取り込みの動きは変わらない。
    名前は最後の取り込みの時点の台帳から読む（`ledger_at`）。台帳が無いときは歩かず 409。
    """
    _require_admin(_current_user(request))
    if not worlds.world_dir(wid):
        raise HTTPException(404, "資料フォルダ（world）が見つかりません（登録済みフォルダを指定してください）")
    rels = doc_ledger.ledger_rels(wid)
    if rels is None:
        raise HTTPException(409, NO_LEDGER_MESSAGE)
    name = worlds.world_label(wid)
    report = inspect_folder(rels, read_text_of(wid))
    if format == "md":
        return Response(to_markdown(report, name), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": content_disposition(f"{name}-点検.md")})
    out = to_json(report, name)
    ledger_at = _ledger_at(wid)
    if ledger_at:
        out["ledger_at"] = ledger_at
    return out
