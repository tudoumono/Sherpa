"""検証済み fd 1本から配信する HTTP レスポンス（documents/ext_api 両ルータで共有）。

`safe_open.py` と対になる配信側の部品。検証を終えた fd を最後まで使い、パスを再解決しない。
Range/検証ヘッダ（`Accept-Ranges`/`Last-Modified`/`ETag`/`If-Range`・単一 Range の 206・範囲外の 416）を実装する
（複数 Range は対象外）。切断監視と同期読み取りの退避は Starlette の `StreamingResponse` に委譲する。
fastapi/starlette 以外の sherpa モジュールは import しない葉ノード。
"""
from __future__ import annotations

import os
from email.utils import formatdate
from hashlib import md5
from urllib.parse import quote

from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool

_CHUNK = 262144  # 256KiB


class FdOwner:
    """fd の所有権を1箇所に集約する（冪等 close）。"""

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
    """非ASCIIファイル名は `filename*=utf-8''...`（RFC 5987 形式）。"""
    q = quote(filename)
    if q != filename:
        return f"{disposition_type}; filename*=utf-8''{q}"
    return f'{disposition_type}; filename="{filename}"'


def _parse_single_range(value: str, size: int):
    """`Range` ヘッダ値を単一範囲だけ解釈する。

    ヘッダ無し・構文不正・複数範囲は `None`（呼び出し元は 200）、範囲が実サイズの外は `"unsatisfiable"`（416）、
    それ以外は半開区間 `(start, end)`（`size` にクランプ済み）。
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

    `owner` の所有権を引き継ぎ、正常終了・早期切断・例外のいずれでも確実に close する。`mtime`/`size` は検証時の `os.fstat()` 結果を渡す
    （このクラスは stat/open をしない）。
    """

    def __init__(self, owner: FdOwner, size: int, mtime: float, *, media_type: str | None,
                headers: dict, status_code: int = 200, background=None):
        self._owner = owner
        self._size = size
        super().__init__((), status_code=status_code, headers=headers,
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
        # If-Range が現在の ETag/Last-Modified と一致しなければ Range を無視して全量 200 にする
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
        # 1 read ごとにスレッドへ退避し、遅い SMB/NFS 越しでもイベントループを止めない
        self.body_iterator = iterate_in_threadpool(self._iter(start, end))
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._owner.close()
