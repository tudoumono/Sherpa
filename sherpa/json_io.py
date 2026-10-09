"""JSON 入出力の小ユーティリティ。書き込みは tmp→os.replace のアトミック置換。"""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import uuid
import zlib
from pathlib import Path

_GZIP_MAGIC = b"\x1f\x8b"
_GZIP_LEVEL = 6
# 壊れた・途中で切れた gzip を読んだときに（OSError 以外で）出る例外。
GZIP_READ_ERRORS = (EOFError, zlib.error)


def is_gzip_file(path) -> bool:
    """先頭 2 バイトが gzip の印（1f 8b）か。不在・読めないは False。"""
    try:
        with open(path, "rb") as f:
            return f.read(2) == _GZIP_MAGIC
    except OSError:
        return False


def open_text_maybe_gzip(path, *, errors: str = "strict"):
    """テキストとして開く（gzip でも非圧縮でも同じ使い方・`with` で閉じる・1 行ずつ読める）。
    設計: docs/design/rag.md「派生物」。"""
    raw = open(path, "rb")
    try:
        head = raw.read(2)
        raw.seek(0)
        stream = gzip.GzipFile(fileobj=raw, mode="rb") if head == _GZIP_MAGIC else raw
        return io.TextIOWrapper(stream, encoding="utf-8", errors=errors)
    except BaseException:
        raw.close()
        raise


def read_text_maybe_gzip(path, *, errors: str = "strict") -> str:
    """gzip でも非圧縮でも展開後の本文を返す（壊れた gzip は OSError）。"""
    try:
        with open_text_maybe_gzip(path, errors=errors) as f:
            return f.read()
    except GZIP_READ_ERRORS as exc:
        raise OSError(f"gzip が壊れています: {exc}") from exc


def expanded_size_exceeds(path, cap_bytes: int) -> bool:
    """展開後の大きさが `cap_bytes` を超えるか。gzip は展開しながら数え、超えた時点で打ち切る。OSError は送出する。"""
    if not is_gzip_file(path):
        return Path(path).stat().st_size > cap_bytes
    total = 0
    try:
        with gzip.open(path, "rb") as f:
            while True:
                block = f.read(1024 * 1024)
                if not block:
                    return False
                total += len(block)
                if total > cap_bytes:
                    return True
    except GZIP_READ_ERRORS as exc:
        raise OSError(f"gzip が壊れています: {exc}") from exc


@contextlib.contextmanager
def atomic_gzip_text_writer(path):
    """gzip テキストをアトミックに書く（一時ファイルへ書いて fsync→`os.replace`）。
    守ること: gzip ヘッダの時刻とファイル名は固定（同じ中身は同じバイト列）・その場上書きしない（ハードリンクの共有元を壊さない）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as raw:
            gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=_GZIP_LEVEL, mtime=0)
            text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
            try:
                yield text
                text.flush()
            finally:
                text.close()
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_json(path, default=None):
    """JSON を安全に読む（gzip も可）。無い/壊れ/IO エラーは `default` を返す（呼び出し側のフォールバックに使う）。"""
    try:
        p = Path(path)
        return json.loads(read_text_maybe_gzip(p)) if p.is_file() else default
    except (OSError, ValueError):
        return default


def write_json_atomic(path, data, *, indent=None, ensure_ascii=False) -> Path:
    """JSON をアトミックに書く（親 dir は自動作成。`indent` 省略時はコンパクト）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # tmp 名は一意にする（同一ファイルへの同時書き込みで tmp を奪い合わない）
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=ensure_ascii, indent=indent), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return p


def write_text_atomic(path, text: str) -> Path:
    """任意のテキストをアトミックに書く（`write_json_atomic` と同じ流儀。直列化済みの文字列用）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return p


def write_bytes_atomic(path, data: bytes) -> Path:
    """バイト列をアトミックに書く（新しい一時ファイルへ書いて差し替える＝ハードリンクで共有された元ファイルの中身は変わらない）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return p
