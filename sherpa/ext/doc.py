"""GET /ext/v1/doc: 根拠の原本DL（symlink 差し替え耐性の open 1本で検証から配信まで行う）。
設計: docs/design/external-api.md
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from sherpa import corpus_docs, deps, safe_open, worlds
from sherpa.ext.audit import _AuditScope
from sherpa.ext.auth import _enforce_world_scope, require_api_key
from sherpa.ext.common import _resolve_world_or_error, router
from sherpa.ext.doc_magic import _doc_magic_ok, _looks_utf8
from sherpa.ext.request_id import _REQUEST_ID_OPENAPI_HEADER, _validation_error_response, _XRequestIdIn
from sherpa.fd_response import FdFileResponse, FdOwner, content_disposition
from sherpa.ingest.analyzers import registry as _analyzer_registry

_DOC_MAX_BYTES = int(os.environ.get("SHERPA_EXT_DOC_MAX_BYTES", str(50 * 1024 * 1024)))  # 50MiB


# ソース原文（コード）の拡張子はアナライザ登録簿が真実源
_UTF8_DECLARE_EXT = {".md", ".markdown", ".txt"} | _analyzer_registry.registered_extensions()
_UTF8_VALIDATE_CAP = 65536  # charset=utf-8 の宣言判定はファイル全体がこの上限以下のときだけ行う

# 拡張子→固定 Content-Type。legacy Office は含めず application/octet-stream 固定（CFB の中身は判別しない）。ソース原文はすべて text/plain
_DOC_CONTENT_TYPE = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
    **{ext: "text/plain" for ext in _analyzer_registry.registered_extensions()},
}

_DOC_RESPONSES = {
    200: {
        "description": "原本ファイル（バイナリ・Content-Type は拡張子ごとの固定値。legacy Office"
                       "〔.doc/.xls/.ppt〕は application/octet-stream 固定で、中身からの形式判別はしない）",
        # 実際に返しうる全 Content-Type を列挙する（`_DOC_CONTENT_TYPE` が真実源）。legacy Office・辞書に無い拡張子は application/octet-stream
        "content": {ct: {"schema": {"type": "string", "format": "binary"}}
                   for ct in sorted({*_DOC_CONTENT_TYPE.values(), "application/octet-stream"})},
        "headers": {
            **_REQUEST_ID_OPENAPI_HEADER,
            "Content-Disposition": {"schema": {"type": "string"}},
        },
    },
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    403: {"description": "このキーはこの world へのアクセスを許可されていません（world スコープ外）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "文書が見つかりません（存在しない／範囲外／対応していない種別）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    413: {"description": "ファイルサイズが上限（既定50MiB）を超えています",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    415: {"description": "ファイルの内容が拡張子と一致しません", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("world/path の形式が不正な場合"),
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    503: {"description": "資料フォルダの参照先を確認できませんでした（一時的な障害）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


def _content_type_for(ext: str, fd: int, size: int) -> str:
    """text 系（charset=utf-8 を宣言する種別）はファイル全体が上限以下の場合のみ実データを検証し、妥当でなければ charset 宣言を外す。上限超過（打ち切り）は検証していないため charset を宣言しない。"""
    base = _DOC_CONTENT_TYPE.get(ext, "application/octet-stream")
    if ext in _UTF8_DECLARE_EXT and size <= _UTF8_VALIDATE_CAP:
        sample = os.pread(fd, size, 0)
        if _looks_utf8(sample):
            return f"{base}; charset=utf-8"
    return base


def _doc_path_segments(path: str) -> tuple | None:
    """`path`（query）→ 検証済み POSIX セグメント列。

    `corpus_docs.status_document_reachable` と同じ正規化（`world_graph.valid_rel_parts`＝絶対パス・`\\`・NUL・空/`.`/`..` 要素の拒否）を共有する
    （判定側と配信側で検証条件が分かれると秘匿ファイル・未対応種別の漏洩経路になる）。配信可否は `status_document_reachable` の `True` 確定だけで判定し、判定不能（`None`）は拒否する（fail-closed）。
    """
    from sherpa.ingest.world_graph import valid_rel_parts
    return valid_rel_parts(path)


@router.get("/doc", response_class=StreamingResponse, responses=_DOC_RESPONSES)
def ext_doc(request: Request, world: str = Query(..., min_length=1, max_length=100),
           path: str = Query(..., min_length=1, max_length=4096),
           key: dict = Depends(require_api_key), x_request_id: str | None = _XRequestIdIn):
    """根拠の原本DL。

    symlink を拒否し、world の root の外は開かない。対応する種別で、先頭バイトが拡張子と整合するファイルだけを返す。
    検証から配信までは同じ開いたファイルから読み、途中でパスを解決し直さない。
    取り込んだアーカイブ（zip/tar(.gz)/tgz）の中のファイルは、展開した写しを返す。
    既定で 50MiB を超えるファイルは 413。
    """
    del x_request_id
    with _AuditScope(request, "ext_api.doc", "ext_doc") as audit:
        audit.resource_id = world
        audit.detail.update({"world": world, "path": path})
        _enforce_world_scope(request, key, world)  # scope 外は世界の存在有無を明かさず先に 403
        ext = Path(path).suffix.lower()
        deps.reject_excluded(world, path, "文書が見つかりません（パス不一致／未実在）")  # 管理者が検索から外している資料は配らない
        # `allow_content_sniff=True`（単発の解決のため軽量テキスト枠の第2段も内容を読んで判定する）。`status_document_reachable` が `True` のときだけ配信し、
        # 判定不能（`None`）は拒否する（fail-closed。配信側の open 経路は判定側と長さ制約が異なるため）
        if corpus_docs.status_document_reachable(path, world, allow_content_sniff=True) is not True:
            raise HTTPException(404, "対応していない種別、または文書が見つかりません")
        parts = _doc_path_segments(path)
        if parts is None:
            raise HTTPException(404, "文書が見つかりません（パス不一致／未実在）")
        root = _resolve_world_or_error(world)
        try:
            fd = safe_open.open_file_nofollow_walk(root, parts)
        except OSError:
            # アーカイブ取り込みの中のファイルは原本ツリーに実在しないため、`documents.resolve` と同じ優先順位（原本→展開先）で展開先（`worlds.archives_dir`）を anchor にした O_NOFOLLOW walk を再試行する
            archives_root = worlds.archives_dir(world)
            if not archives_root.is_dir():
                raise HTTPException(404, "文書が見つかりません（パス不一致／未実在）")
            try:
                fd = safe_open.open_file_nofollow_walk(archives_root, parts)
            except OSError:
                raise HTTPException(404, "文書が見つかりません（パス不一致／未実在）")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise HTTPException(404, "文書が見つかりません（パス不一致／未実在）")
            size_bytes = st.st_size
            if size_bytes > _DOC_MAX_BYTES:
                raise HTTPException(
                    413, f"ファイルサイズが上限（{_DOC_MAX_BYTES // 1024 // 1024}MiB）を超えています")
            header = os.pread(fd, 16, 0)
            if not _doc_magic_ok(fd, ext, header, size_bytes):
                raise HTTPException(415, "ファイルの内容が拡張子と一致しません")
            media_type = _content_type_for(ext, fd, size_bytes)
        except Exception:  # HTTPException も含め、検証失敗時は配信前なので fd をここで閉じる
            os.close(fd)
            raise
        audit.detail["size_bytes"] = size_bytes
        headers = {
            # Content-Type はここで確定させ media_type=None で渡す（starlette が `text/` 始まりに自動で charset を足し、`_content_type_for` の「charset を宣言しない」判断を上書きするのを避ける）
            "Content-Type": media_type,
            "Content-Disposition": content_disposition(Path(path).name),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        return FdFileResponse(FdOwner(fd), size_bytes, st.st_mtime, media_type=None, headers=headers)
