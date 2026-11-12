"""ヒット本文の位置づけと切り詰め（`run_tool` の es_search／ripgrep_search が使う）。
設計: docs/design/interfaces.md「読み取り部品（境界としての切り出し済み・手順②）」
"""
from __future__ import annotations

from ... import grep_tool


def _clip_utf8_bytes(s: str, max_bytes: int) -> str:
    """UTF-8 エンコード後のバイト数が `max_bytes` を超えないよう `s` を切り詰める（マルチバイト文字の境界で壊れた文字が残らないよう `errors="ignore"` で再デコードする）。"""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")


def _match_position(text: str, q: str, base_line: int | None) -> tuple[int, int] | None:
    """`text` の中で `q` が最初に一致する (行, 行内の文字位置 0 始まり)。行は `base_line`（本文の先頭行）からの番号で、先頭行が不明なら本文内の 1 始まり。一致しなければ None。"""
    ql = q.lower()
    if not ql:
        return None
    for i, t in enumerate(text.split("\n")):
        if ql in t.lower():
            span = grep_tool.match_span(t, ql)
            return None if span is None else ((base_line if isinstance(base_line, int) else 1) + i, span[0])
    return None


def _clip_es_hit_text(text: str, q: str, base_line: int | None, max_bytes: int) -> tuple[str, tuple[int, int] | None]:
    """es_search のヒット本文を `max_bytes` に収める。語が本文にあれば一致した所の周辺を残し、無ければ（意味検索のヒット）先頭から切る。切ったときは一致の (行, 行内の文字位置) も返す（呼び出し側は行の目安だけを使う）。"""
    if len(text.encode("utf-8")) <= max_bytes:
        return text, None
    pos = _match_position(text, q, base_line)
    if pos is None:
        return _clip_utf8_bytes(text, max_bytes), None
    clipped = grep_tool.clip_hit_text(text, q, pos[0], max_bytes,
                                      span_start=base_line if isinstance(base_line, int) else 1)
    return clipped, pos
