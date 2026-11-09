"""期の読み取り（`periods.read_periods`）に渡す、変更一覧を読む関数の作り方。点検（folder-check）が使う。"""
from __future__ import annotations

import os

from .. import text_encoding
from . import text_kind

READ_LIMIT_BYTES = 2 * 1024 * 1024


def read_text_via(resolve):
    """rel → 原本 Path（無ければ None）を返す `resolve` から、変更一覧を読む関数を作る。
    2MB 以下のファイルを UTF-8（BOM 付き可）→ CP932 → EUC-JP の順に判定して読み、読めなければ None。秘匿名は読まない。"""
    def read_text(rel: str):
        if text_kind.is_sensitive_doc_id(rel):
            return None
        p = resolve(rel)
        if p is None:
            return None
        try:
            if os.path.getsize(p) > READ_LIMIT_BYTES:
                return None
            raw = p.read_bytes()
            return raw.decode(text_encoding.detect_bytes(raw, complete=True))
        except (OSError, UnicodeDecodeError):
            return None
    return read_text
