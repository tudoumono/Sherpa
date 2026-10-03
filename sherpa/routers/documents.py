"""文書台帳・原本ダウンロードのエンドポイント: `GET /documents/download`（`download_router`）・`GET /documents` と `GET /admin/es/search`（`documents_router`）。
`sherpa.api` を import しない。モジュール名が `sherpa/documents.py` と衝突するため、api.py 側は `from sherpa.routers import documents as documents_routes` と別名で import する。
`doc_download` の fd ベース配信は共有モジュール `sherpa.fd_response`（`sherpa.ext_api` の `/ext/v1/doc` と共用）を使う。
設計: docs/design/data.md「秘匿ファイルの扱い」
"""
from __future__ import annotations

import logging
import mimetypes
import os
import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from sherpa import doc_ledger, safe_open, store, worlds
from sherpa import scope as scope_mod
from sherpa.deps import _DEFAULT_WORLD, _WORLD_PATTERN, _current_user, _require_admin, _resolve_world, validated_scope
from sherpa.fd_response import FdFileResponse, FdOwner, content_disposition
from sherpa.ingest import text_kind
from sherpa.store.db import world_lock_shared

_log = logging.getLogger("sherpa")

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
download_router = APIRouter()
documents_router = APIRouter()


@download_router.get("/documents/download", tags=["文書"])
def doc_download(request: Request, rel: str = Query(...), world: str = Query(_DEFAULT_WORLD, pattern=_WORLD_PATTERN)):
    """根拠の原本をダウンロードする。doc_id＝rel_path（world root 相対）。
    台帳に載っているファイルだけが対象で、秘匿ファイル・台帳に無い名前・root 外は 404。アーカイブ取り込み（zip/tar(.gz)/tgz）の中のファイルは、展開した写しを返す（原本アーカイブそのものではない）。
    ダウンロードは監査に記録され、記録できなければ許可しない。
    """
    u = _current_user(request)
    # world_lock_shared は台帳確認の直前から fd の fstat 完了まで保持し、root は取得後に一度だけ解決する
    with world_lock_shared(world):
        # ① 台帳に完全一致で載っているか確認する（別名は落ちる）・秘匿名を塞ぐ
        if not store.document_exists(world, rel):
            raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
        if text_kind.is_sensitive_doc_id(rel):
            raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
        root = worlds.world_dir(world)
        if not root:
            raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
        with worlds.pin_world_root(world, root):
            # ② 実体を root からの lstat 降下で確認する（symlink 拒否・封じ込め）
            # ② 実体を root からの lstat 降下で確認する（symlink 拒否・封じ込め）
            p = doc_ledger.original_path(rel, world)
            if not p:
                raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
            anchor = root
            archives_root = worlds.archives_dir(world)
            if archives_root.is_dir():
                try:
                    if p.resolve().is_relative_to(archives_root.resolve()):
                        anchor = archives_root
                except OSError:
                    pass
            # ③ 同じ fd だけを fstat から配信まで使う（検証した Path は使わない）
            try:
                fd = safe_open.open_file_nofollow_walk(anchor, tuple(rel.split("/")))
            except OSError:
                raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
                size_bytes, mtime = st.st_size, st.st_mtime
            except Exception:  # 配信前の失敗（HTTPException を含む）はここで fd を閉じる。
                os.close(fd)
                raise
    try:
        store.audit(u["uid"], "document.downloaded", "document", f"{world}:{rel}",
                    detail={"world": world, "rel": rel, "download_type": "original"},
                    outcome="success")
    except Exception:
        # fail-closed: 監査できないダウンロードは許可しない。
        _log.critical("audit write failed for document.downloaded – blocking download")
        os.close(fd)
        raise HTTPException(500, "ダウンロード処理中にエラーが発生しました")
    media_type = mimetypes.guess_type(rel)[0] or "application/octet-stream"
    headers = {"Content-Disposition": content_disposition(Path(rel).name)}
    return FdFileResponse(FdOwner(fd), size_bytes, mtime, media_type=media_type, headers=headers)


@documents_router.get("/documents", tags=["文書"])
def documents_list(request: Request, world: str | None = Query(None, pattern=_WORLD_PATTERN),
                   limit: int | None = Query(None, ge=1, le=1000), offset: int = Query(0, ge=0)):
    """文書台帳（doc_id＝rel_path＋フォルダ由来の範囲メタ）を返す。物理パスは出さない。
    ページング（`limit`/`offset`・上限 1000）は明示指定したときだけ有効で、省略時は全件を返す。応答の `world`/`documents` はそのままで、`total`/`has_more` を常に追加し、`limit`/`offset` を指定したときだけその実効値も追加する。
    取り込み済みの文書台帳から返す。台帳が空の world だけはフォルダを走査して返す。
    """
    _current_user(request)  # ログイン必須（auth 有効時）。
    w = _resolve_world(world)
    docs, total = doc_ledger.public_documents_page(w, limit=limit, offset=offset)
    body = {"world": w, "documents": docs, "total": total, "has_more": offset + len(docs) < total}
    if limit is not None:
        body["limit"], body["offset"] = limit, offset
    return body


@documents_router.get("/admin/es/search", tags=["文書"])
def _admin_es_search_endpoint(request: Request,
                               world: str | None = Query(None, pattern=_WORLD_PATTERN),
                               query: str = Query(..., min_length=1),
                               scope_paths: list[str] = Query(default_factory=list),
                               k: int = Query(20, ge=1, le=50)):
    """管理者向けの ES 検索（読み取り専用）。クエリ param `world` は他の API と同じ取込ディレクトリ（world id）。"""
    from sherpa import documents, es_index
    _require_admin(_current_user(request))
    w = _resolve_world(world)
    sp = validated_scope(w, scope_paths) or None
    valid = documents.world_rel_set(w)
    hits = []
    # `es_index.search()` は (hits, reason) のタプルを返す。この読み取り専用検索には degraded 報告が無いため BM25 失敗（es_query_failed）の reason は捨てる（構造化された degraded 集計が要る呼び出し元は `fused_search._search_keyword()`）。
    es_hits, _reason = es_index.search(w, query, scope_paths=sp, k=k, vector=False)
    for h in es_hits:
        doc = h.get("doc_id")
        if not doc or doc not in valid or not scope_mod.in_scope(doc, sp):
            continue
        # 秘匿名（credentials.xlsx 等）の本文は、索引済みでも管理者検索で返さない。
        if text_kind.is_sensitive_doc_id(doc):
            continue
        hit = {"doc_id": doc, "line": h.get("line"), "snippet": h.get("text", ""),
               "score": h.get("score"), "ext": h.get("ext")}
        # 抽出来歴（索引済み）を表示用にそのまま渡す（無ければ付けない）。
        for key in ("extraction_method", "confidence", "has_conflicts"):
            if h.get(key) is not None:
                hit[key] = h[key]
        hits.append(hit)
    return {"world": w, "query": query, "scope_paths": sp or [], "hits": hits}
