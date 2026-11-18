"""文書台帳・原本ダウンロードのエンドポイント: `GET /documents/download`（`download_router`）・`GET /documents` と `GET /admin/es/search`（`documents_router`）。
`sherpa.api` を import しない。モジュール名が `sherpa/documents.py` と衝突するため、api.py 側は `from sherpa.routers import documents as documents_routes` と別名で import する。
`doc_download` の fd ベース配信は共有モジュール `sherpa.fd_response`（`sherpa.ext_api` の `/ext/v1/doc` と共用）を使う。
設計: docs/design/data.md「秘匿ファイルの扱い」
"""
from __future__ import annotations

import contextlib
import logging
import mimetypes
import os
import stat
from pathlib import Path

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request

from sherpa import doc_ledger, doc_reach, safe_open, scope_infer, store, worlds
from sherpa import scope as scope_mod
from sherpa.deps import SCOPE_WALK_TIMEOUT_MESSAGE, _DEFAULT_WORLD, _WORLD_PATTERN, _current_user, _require_admin, _resolve_world, validated_scope
from sherpa.fd_response import FdFileResponse, FdOwner, content_disposition
from sherpa.ingest import text_kind

_log = logging.getLogger("sherpa")

_SWAP_BUSY_MESSAGE = "資料フォルダの参照先を切り替えている最中です。少し待ってからもう一度お試しください"

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
download_router = APIRouter()
documents_router = APIRouter()


@contextlib.contextmanager
def _swap_read(world: str):
    """参照先の入れ替え（rebind／削除）と読み取りを排他にする。取り込み中は待たない。上限を超えたら 503。"""
    with contextlib.ExitStack() as st:
        try:
            st.enter_context(store.world_swap_lock_shared(world, timeout_ms=doc_ledger.SWAP_LOCK_WAIT_MS))
        except psycopg.errors.LockNotAvailable:
            raise HTTPException(503, _SWAP_BUSY_MESSAGE)
        yield


def _reject_excluded(world: str, rel: str) -> None:
    """管理者が検索から外している資料は、台帳にあっても配らない（存在を示さず 404）。除外の設定を読めないときは 503。"""
    from sherpa import path_rules
    try:
        hidden = path_rules.rules_for(world).excluded(rel)
    except path_rules.PathRulesUnavailable:
        raise HTTPException(503, path_rules.UNAVAILABLE_MESSAGE)
    if hidden:
        raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")


@download_router.get("/documents/download", tags=["文書"])
def doc_download(request: Request, rel: str = Query(...), world: str = Query(_DEFAULT_WORLD, pattern=_WORLD_PATTERN)):
    """根拠の原本をダウンロードする。doc_id＝rel_path（world root 相対）。
    対象は台帳に載っているファイル、または出典の確かめと同じ条件（`doc_reach.doc_reachable`＝実在・文書種別・取り込みが外すものでない）を満たすファイル。秘匿ファイル・それ以外の名前・root 外は 404（理由は区別しない）。アーカイブ取り込み（zip/tar(.gz)/tgz）の中のファイルは、展開した写しを返す（原本アーカイブそのものではない）。
    ダウンロードは監査に記録され、記録できなければ許可しない。
    """
    u = _current_user(request)
    _reject_excluded(world, rel)
    # world_swap_lock_shared は台帳確認の直前から fd の fstat 完了まで保持し、root は取得後に一度だけ解決する
    with _swap_read(world):
        # ① 台帳に完全一致で載っているか、出典の確かめと同じ条件を満たすか（別名は落ちる）・秘匿名を塞ぐ
        if not (store.document_exists(world, rel) or doc_reach.doc_reachable(rel, world)):
            raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
        if text_kind.is_sensitive_doc_id(rel):
            raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
        root = worlds.world_dir(world)
        if not root:
            raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
        with worlds.pin_world_root(world, root):
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
    取り込み済みの文書台帳から返す。台帳が空の world だけはフォルダを（期限つきで）走査して返し、期限を超えたら 503。
    """
    _current_user(request)  # ログイン必須（auth 有効時）。
    w = _resolve_world(world)
    try:
        docs, total = doc_ledger.public_documents_page(w, limit=limit, offset=offset)
    except psycopg.errors.LockNotAvailable:
        raise HTTPException(503, _SWAP_BUSY_MESSAGE)
    except scope_infer.ScopeWalkDeadlineExceeded:
        raise HTTPException(503, SCOPE_WALK_TIMEOUT_MESSAGE) from None
    body = {"world": w, "documents": docs, "total": total, "has_more": offset + len(docs) < total}
    if limit is not None:
        body["limit"], body["offset"] = limit, offset
    return body  # 資料の一覧・総数には除外を当てない（名前が出るのは許す。除外は検索・読み取り・ダウンロードで拒否する）


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
    hits = []
    es_hits, reason = es_index.search(w, query, scope_paths=sp, k=k, vector=False)
    if reason in es_index._ES_QUERY_FAILURES:
        raise HTTPException(503, "全文検索のサービスを使えません。システム状態を確認してから、もう一度お試しください")
    from sherpa import path_rules
    sp_eff = path_rules.rules_for(w).extend_scope(sp)  # 「範囲の外でも読む」を足した範囲（ES が足したヒットを落とし直さない）
    try:
        valid = documents.existing_rels(w, [h.get("doc_id") for h in es_hits])  # 実在はヒットした doc だけを確かめる（木は歩かない）
    except scope_infer.ScopeWalkDeadlineExceeded:
        raise HTTPException(503, SCOPE_WALK_TIMEOUT_MESSAGE) from None
    for h in es_hits:
        doc = h.get("doc_id")
        if not doc or doc not in valid or not scope_mod.in_scope(doc, sp_eff):
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
