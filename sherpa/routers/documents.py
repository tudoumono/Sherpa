"""文書台帳・原本ダウンロードエンドポイント（フェーズ3スライス4・純移動）。

`GET /documents/download`（`download_router`）・`GET /documents` と `GET /admin/es/search`
（`documents_router`）を api.py から抽出する。ロジックは変更しない（コード移動のみ）。
ルート表 golden の定義順を保つため、api.py 側は削除したブロックの元位置にそれぞれ
`app.include_router(documents_routes.download_router)`（`/ingest/preview` より前・元の
`/documents/download` の位置）／`app.include_router(documents_routes.documents_router)`
（`/ingest/preview` の直後・`/world-options` より前）を置く（`sherpa/routers/system.py` の
複数 router 分離パターンと同じ）。

このモジュールは `sherpa.api` を import しない（循環回避）。モジュール basename が既存の
`sherpa/documents.py`（世界単位の文書一覧ユーティリティ）と衝突するため、api.py 側は
`from sherpa.routers import documents as documents_routes` のように別名 import し、
api.py モジュールレベルに裸の `documents` 名を作らない。

`doc_download` の fd ベース配信（検証〜配信を同一 fd にする TOCTOU 対策・Range/ETag/
Last-Modified 対応）は中立の共有モジュール `sherpa.fd_response`（stdlib＋fastapi/starlette
のみに依存する葉ノード）を `sherpa.ext_api`（`/ext/v1/doc`）と共用する——`documents`→`ext_api`
という向きの import 依存を持たないため、どちらのルータが先に読み込まれても循環にならない。
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
from sherpa.store.db import world_lock_shared

_log = logging.getLogger("sherpa")

# router に tags を持たせない: 各エンドポイントの `tags=["文書"]` と結合されて
# 二重化してしまう（ルート表 golden 不一致の原因）ため、tags 指定は各デコレータ側のみに残す
# （system.py:42-44 と同じパターン）。
download_router = APIRouter()
documents_router = APIRouter()


@download_router.get("/documents/download", tags=["文書"])
def doc_download(request: Request, rel: str = Query(...), world: str = Query(_DEFAULT_WORLD, pattern=_WORLD_PATTERN)):
    """根拠の原本DL（R6）。doc_id＝**rel_path**（world root 相対）をパス基準で解決（root 確認）。

    検証は3段、`world_lock_shared`（読み取り専用の共有ロック・rebind/delete の排他ロックと
    直列化）を**台帳確認の直前から fd の fstat 完了まで**保持したまま行う——ロックを取った
    **後**に一度だけ `worlds.world_dir()` で root を解決し（`pin_world_root` で固定）、以降の
    台帳整合の検証と safe_open の両方に**同じ** root を渡す（世代が違う root を跨いで検証・
    配信してしまう＝rebind と競合する窓を無くす）。
    (1) 文書台帳（`store.document_exists`）に `rel` が**完全一致**で載っているかを索引付き1行
    SELECT で確認する——台帳の name は safe_files 由来の正準表記なので、大文字小文字違い・
    8.3 短縮名等の別名や、列挙不能なディレクトリ内の（生成物にしか現れない）ファイル名はここで
    落ちる（`sherpa.documents.resolve` 自体は filesystem のみで完結し DB を要らないため、この
    確認は DB が常に使える本エンドポイント側で行う）。台帳は取り込み成功時に書かれる
    （`ingest/worker.py`）ため、一度も取り込みが完了していない world は常に 404。
    (2) `doc_ledger.original_path`（root からの直接 lstat 降下・symlink 拒否・封じ込め）で実体を
    確認する。(3) 検証と実配信を別操作にしない：検証済みの `rel` を改めて
    `safe_open.open_file_nofollow_walk`（`/ext/v1/doc` と同じ実装）で O_NOFOLLOW walk して
    得た fd 1本だけを fstat〜配信まで使う——検証で得た `Path` をそのまま `FileResponse` に渡すと、
    検証〜実際の open の間隔で中間ディレクトリが symlink に差し替えられた場合に追跡してしまう
    （TOCTOU）。fstat 完了後（fd 自体は inode を掴んでおり以後のパス変化と無関係）にロックを
    解放してから監査・配信に進む——配信（大きなファイルだと時間がかかる）の間 rebind/delete を
    ブロックし続けない。
    """
    u = _current_user(request)
    with world_lock_shared(world):
        if not store.document_exists(world, rel):
            raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
        root = worlds.world_dir(world)
        if not root:
            raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
        with worlds.pin_world_root(world, root):
            p = doc_ledger.original_path(rel, world)
            if not p:
                raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
            try:
                fd = safe_open.open_file_nofollow_walk(root, tuple(rel.split("/")))
            except OSError:
                raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise HTTPException(404, "原本が見つかりません（パス不一致／未実在）")
                size_bytes, mtime = st.st_size, st.st_mtime
            except Exception:   # HTTPException も含め、配信前の失敗はここで fd を閉じる
                os.close(fd)
                raise
    try:
        store.audit(u["uid"], "document.downloaded", "document", f"{world}:{rel}",
                    detail={"world": world, "rel": rel, "download_type": "original"},
                    outcome="success")
    except Exception:
        # fail-closed: 監査できない DL は許可しない（2026-07-01-監査ログ強化.md §4）
        _log.critical("audit write failed for document.downloaded – blocking download")
        os.close(fd)
        raise HTTPException(500, "ダウンロード処理中にエラーが発生しました")
    media_type = mimetypes.guess_type(rel)[0] or "application/octet-stream"
    headers = {"Content-Disposition": content_disposition(Path(rel).name)}
    return FdFileResponse(FdOwner(fd), size_bytes, mtime, media_type=media_type, headers=headers)


@documents_router.get("/documents", tags=["文書"])
def documents_list(request: Request, world: str | None = Query(None, pattern=_WORLD_PATTERN),
                   limit: int | None = Query(None, ge=1, le=1000), offset: int = Query(0, ge=0)):
    """文書台帳（doc_id＝rel_path＋フォルダ由来の範囲メタ）。**物理パスは出さない**。

    ページング（`limit`/`offset`・上限 1000・`sherpa/routers/graph.py` の近傍検索と同じ上限値）は
    **明示指定時のみ**有効にする（RV1是正#1）——`limit`/`offset` を省略した従来どおりの呼び出しは
    **全件**を返す（既定 200 件で黙って切らない＝旧クライアント互換を壊さない）。応答は後方互換
    （既存の `world`/`documents` フィールドはそのまま）で `total`/`has_more` を常に追加し、
    `limit`/`offset` を指定した時だけその実効値も追加する（無指定時は省略）。

    定数時間化（S工事②・2026-09-01）: 台帳（`store.documents`）に行があれば、そこへの狭い
    SELECT（`doc_ledger.public_documents_page` 参照）だけで応答する——2TB 級 world でもフォルダを
    歩かない。台帳が空（未登録の dev fixture 等）の world だけ、既存の実走査へフォールバックする
    （後方互換）。
    """
    _current_user(request)   # ログイン必須（auth 有効時）
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
    """管理者向け ES 検索（read-only・認証付き）。

    クエリ param は他の API（`/documents`・`/graph`・`/chat`）と同じ `world`（取込ディレクトリ＝world id）。
    """
    from sherpa import documents, es_index
    _require_admin(_current_user(request))
    w = _resolve_world(world)
    sp = validated_scope(w, scope_paths) or None
    valid = documents.world_rel_set(w)
    hits = []
    # `es_index.search()` は (hits, reason) タプル（RV2）。BM25 実クエリ失敗（es_query_failed）も
    # ありうるが、この管理者向け read-only 検索には degraded 報告の仕組みが無いため意図的に捨てる
    # （構造化された degraded 集計が要る呼び出し元は `search_service._search_keyword()` 参照）。
    es_hits, _reason = es_index.search(w, query, scope_paths=sp, k=k, vector=False)
    for h in es_hits:
        doc = h.get("doc_id")
        if not doc or doc not in valid or not scope_mod.in_scope(doc, sp):
            continue
        hit = {"doc_id": doc, "line": h.get("line"), "snippet": h.get("text", ""),
               "score": h.get("score"), "ext": h.get("ext")}
        # 抽出来歴（A4・索引済み）を表示用に passthrough（無ければ付けない＝従来どおり・S2）。
        for key in ("extraction_method", "confidence", "has_conflicts"):
            if h.get(key) is not None:
                hit[key] = h[key]
        hits.append(hit)
    return {"world": w, "query": query, "scope_paths": sp or [], "hits": hits}
