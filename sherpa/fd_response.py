"""検証済み fd 1本から配信する HTTP レスポンス（documents/ext_api 両ルータで共有）。

`sherpa/safe_open.py`（symlink 差し替え耐性 open）と対になる配信側の共有部品——検証
（realpath/doctype/マジック等）を終えた fd をそのまま最後まで使い、パスを再解決しない
（検証〜配信間の TOCTOU を避ける）。fastapi/starlette 以外の sherpa モジュールは import しない
（documents ルータ・ext_api ルータのどちらからも一方向に import される中立の葉ノード——
`sherpa.ext_api` を `sherpa.routers.documents` から import する/しないという向きの問題を
本モジュールの新設で解消する）。

`starlette.responses.FileResponse` 相当の Range/検証ヘッダ（`Accept-Ranges`/`Last-Modified`/
`ETag`/`If-Range`・単一 Range の 206・範囲外の 416）を実装するが、複数 Range
（`multipart/byteranges`）は対象外（現状の利用箇所はいずれも単一 Range の再開DLだけを必要と
する最小実装）。`StreamingResponse` を継承することで、クライアント切断の監視
（`receive()` の `http.disconnect`・`send` が例外を投げない ASGI transport でも打ち切る）と
同期読み取りのイベントループ退避（`iterate_in_threadpool`＝1 read ごとに
`anyio.to_thread.run_sync`）は Starlette 本体の実装にそのまま委譲する——本モジュールが独自に
持つのは Range/If-Range の解釈と検証ヘッダの付与、fd の所有権管理だけ。
"""
from __future__ import annotations

import os
from email.utils import formatdate
from hashlib import md5
from urllib.parse import quote

from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool

_CHUNK = 262144   # 256KiB


class FdOwner:
    """fd の所有権を1箇所に集約する（冪等 close）。close は複数回呼ばれても安全
    （応答の正常終了・早期切断・例外のいずれの経路からも呼ばれ得るため）。
    """

    __slots__ = ("_fd", "_closed")

    def __init__(self, fd: int):
        self._fd = fd
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._fd)

    def pread(self, size: int, offset: int) -> bytes:
        return os.pread(self._fd, size, offset)


def content_disposition(filename: str, *, disposition_type: str = "attachment") -> str:
    """非ASCIIファイル名は `filename*=utf-8''...`（starlette.FileResponse と同じ RFC 5987 形式）。"""
    q = quote(filename)
    if q != filename:
        return f"{disposition_type}; filename*=utf-8''{q}"
    return f'{disposition_type}; filename="{filename}"'


def _parse_single_range(value: str, size: int):
    """`Range` ヘッダ値を単一範囲だけ解釈する。

    戻り値: ヘッダが無い/構文不正/複数範囲（`,` を含む）は `None`（呼び出し元は単純配信 200 に
    フォールバックする——複数範囲の multipart 応答は本モジュールでは実装しない）。範囲が実在
    サイズの外（`start` が負/サイズ以上）は文字列 `"unsatisfiable"`（呼び出し元は 416）。
    それ以外は半開区間 `(start, end)`（`end` は exclusive・`size` にクランプ済み）。
    """
    if not value.lower().startswith("bytes="):
        return None
    spec = value[len("bytes="):].strip()
    if not spec or "," in spec or "-" not in spec:
        return None
    start_s, _, end_s = spec.partition("-")
    start_s, end_s = start_s.strip(), end_s.strip()
    try:
        if start_s:
            start = int(start_s)
            end = int(end_s) + 1 if end_s else size
        elif end_s:
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start = max(size - suffix, 0)
            end = size
        else:
            return None
    except ValueError:
        return None
    if start < 0 or start >= size:
        return "unsatisfiable"
    if start > end:
        return None
    return start, min(end, size)


class FdFileResponse(StreamingResponse):
    """検証済み fd 1本から配信する（Range/If-Range/ETag/Last-Modified 対応）。

    `owner`（`FdOwner`）の所有権を引き継ぐ——`__call__` が正常終了・クライアント早期切断・
    例外のいずれでも try/finally で確実に close する。`mtime`/`size` は呼び出し元が検証時に
    取得済みの `os.fstat()` 結果からそのまま渡す（このクラス自身は一切 stat/open をしない＝
    検証〜配信間で対象が変わりようがない）。
    """

    def __init__(self, owner: FdOwner, size: int, mtime: float, *, media_type: str | None,
                headers: dict, status_code: int = 200, background=None):
        self._owner = owner
        self._size = size
        super().__init__((), status_code=status_code, headers=headers,   # body_iterator は __call__ で差し替える
                         media_type=media_type, background=background)
        self.headers.setdefault("accept-ranges", "bytes")
        self.headers["content-length"] = str(size)
        self.headers["last-modified"] = formatdate(mtime, usegmt=True)
        etag_base = f"{mtime}-{size}"
        self.headers["etag"] = f'"{md5(etag_base.encode(), usedforsecurity=False).hexdigest()}"'

    def _iter(self, start: int, end: int):
        pos = start
        while pos < end:
            chunk = self._owner.pread(min(_CHUNK, end - pos), pos)
            if not chunk:
                break
            pos += len(chunk)
            yield chunk

    async def __call__(self, scope, receive, send) -> None:
        range_header = None
        if_range = None
        for k, v in scope.get("headers", []):
            if k == b"range":
                range_header = v.decode("latin-1")
            elif k == b"if-range":
                if_range = v.decode("latin-1")
        # If-Range が現在の ETag/Last-Modified と一致しない（＝配信直前に検証した内容から更新
        # されている恐れがある）場合は Range を無視して全量 200 にする——一致しないまま Range を
        # 適用すると、更新前後の断片を連結した壊れたファイルをクライアントが再構成してしまう。
        if range_header is not None and if_range is not None:
            if if_range != self.headers["etag"] and if_range != self.headers["last-modified"]:
                range_header = None
        start, end = 0, self._size
        if range_header is not None:
            parsed = _parse_single_range(range_header, self._size)
            if parsed == "unsatisfiable":
                try:
                    headers = [(b"content-range", f"bytes */{self._size}".encode("latin-1"))]
                    await send({"type": "http.response.start", "status": 416, "headers": headers})
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                finally:
                    self._owner.close()
                return
            if parsed is not None:
                start, end = parsed
                self.status_code = 206
                extra = [(b"content-range", f"bytes {start}-{end - 1}/{self._size}".encode("latin-1")),
                        (b"content-length", str(end - start).encode("latin-1"))]
                self.raw_headers = [(k, v) for k, v in self.raw_headers
                                    if k not in (b"content-length",)] + extra
        # `iterate_in_threadpool`（starlette.concurrency）が 1 read ごとに
        # `anyio.to_thread.run_sync` で実行する——遅い SMB/NFS 越しの読み取りでイベントループ
        # （同一 worker の他リクエスト）を止めない。切断監視（`receive()` の `http.disconnect`）
        # も `StreamingResponse.__call__`（継承元）がそのまま行う。
        self.body_iterator = iterate_in_threadpool(self._iter(start, end))
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._owner.close()
