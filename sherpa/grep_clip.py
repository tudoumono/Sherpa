"""ヒット本文を UTF-8 のバイト上限に収める部品（`grep_tool` から出した）。離れた行は行の位置つきの省略の印でつなぐ。"""
from __future__ import annotations

import re


def _clip_utf8_bytes(s: str, max_bytes: int) -> str:
    """UTF-8 のバイト数が `max_bytes` を超えないよう `s` を切り詰める（マルチバイト文字の境界を壊さない）。"""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")


# ヒット本文の省略の印。離れた行をつなぐときの行の位置（1 始まり）を残し、連続した原文に見せない。
_GAP_MARK_RE = re.compile(r"⋯（(\d+)(?:〜(\d+))?行目を省略）⋯")
_TAIL_MARK_RE = re.compile(r"⋯（(\d+)行目以降を省略）⋯")
_MARK_BYTES_EST = 64  # 省略の印 1 つぶんのバイト数の見積り（実測は 50 未満）
# 節が上限を超えたときに、一致行の前後に残す行数。
_HIT_CONTEXT_LINES = 3


def _gap_mark(first: int, last: int) -> str:
    return f"⋯（{first}〜{last}行目を省略）⋯" if last > first else f"⋯（{first}行目を省略）⋯"


def _tail_mark(first: int) -> str:
    return f"⋯（{first}行目以降を省略）⋯"


def _assemble_lines(kept: list[tuple[int, str]], last_line: int | None) -> str:
    """行番号つきの行（昇順）を、離れた箇所に省略の印を挟んでつなぐ。`last_line` より後ろが残っていれば末尾にも印を付ける。"""
    out: list[str] = []
    prev: int | None = None
    for n, t in kept:
        if prev is not None and n > prev + 1:
            out.append(_gap_mark(prev + 1, n - 1))
        out.append(t)
        prev = n
    if last_line is not None and prev is not None and last_line > prev:
        out.append(_tail_mark(prev + 1))
    return "\n".join(out)


def match_span(line: str, ql: str) -> tuple[int, int] | None:
    """小文字化した `line` 中の `ql`（小文字）の最初の一致を、原文 `line` 上の文字位置 (開始, 終了)（0 始まり・終了は含まない）で返す。
    一致が無い、または小文字化で文字数が変わって原文の位置に戻せないときは None。位置を原文に使う箇所はすべてこれを通す。
    """
    idx = line.lower().find(ql)
    if idx < 0:
        return None
    starts: dict[int, int] = {}
    pos = 0
    for i, c in enumerate(line):
        starts[pos] = i
        pos += len(c.lower())
    starts[pos] = len(line)
    lo, hi = starts.get(idx), starts.get(idx + len(ql))
    return None if lo is None or hi is None else (lo, hi)


def _match_char(line: str, ql: str) -> int | None:
    """`line` 中の `ql`（小文字）の一致位置（原文の文字位置・0 始まり）。戻せないときは None。"""
    span = match_span(line, ql)
    return None if span is None else span[0]


def _clip_line_around(line: str, ql: str, max_bytes: int) -> str:
    """1 行が `max_bytes` を超えるとき、`ql`（小文字）の一致位置を含む前後を残す（切れた側に `…`）。"""
    if len(line.encode("utf-8")) <= max_bytes:
        return line
    span = match_span(line, ql)
    pos, end = span if span is not None else (0, min(len(line), len(ql)))

    def _cut(k: int) -> str:
        a, b = max(0, pos - k), min(len(line), end + k)
        return ("…" if a > 0 else "") + line[a:b] + ("…" if b < len(line) else "")

    lo, hi, best = 0, len(line), _cut(0)
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = _cut(mid)
        if len(cand.encode("utf-8")) <= max_bytes:
            best, lo = cand, mid + 1
        else:
            hi = mid - 1
    return best if len(best.encode("utf-8")) <= max_bytes else _clip_utf8_bytes(best, max_bytes)


def clip_hit_text(text: str, q: str, hit_line: int | None, max_bytes: int, *, span_start: int | None = None) -> str:
    """ヒット本文 `text` を `max_bytes` に収める。収まるならそのまま返す。
    収まらないときは「見出し行（先頭が `#`）＋一致行の前後」を残し、離れた箇所は行の位置つきの省略の印でつなぐ（先頭から切って一致行を落とさない）。
    一致行が見つからない・行の位置が不明なときは先頭から切る。`hit_line` はヒットの行（1 始まり）、`span_start` は本文の先頭行の行番号（分かるときだけ）。
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    ql = q.lower()
    raw = text.split("\n")
    if not ql or hit_line is None:
        return _clip_utf8_bytes(text, max_bytes)
    # ① 省略の印を取り除き、残りの行に行番号を振る（印の範囲から番号を引き継ぐ）
    kinds: list = []
    for t in raw:
        g = _GAP_MARK_RE.fullmatch(t)
        tm = _TAIL_MARK_RE.fullmatch(t)
        kinds.append(("gap", int(g.group(1)), int(g.group(2) or g.group(1))) if g
                     else ("tail", int(tm.group(1))) if tm else None)
    real = [i for i, k in enumerate(kinds) if k is None]
    m = None
    if span_start is not None and not any(kinds):
        idx = hit_line - span_start
        if 0 <= idx < len(raw) and ql in raw[idx].lower():
            m = idx
    if m is None:
        m = next((i for i in real if ql in raw[i].lower()), None)
    if m is None:
        return _clip_utf8_bytes(text, max_bytes)
    num: dict[int, int] = {m: hit_line}
    n = hit_line
    for i in range(m + 1, len(raw)):
        k = kinds[i]
        if k is None:
            n += 1
            num[i] = n
        elif k[0] == "gap":
            n = k[2]
    n = hit_line
    for i in range(m - 1, -1, -1):
        k = kinds[i]
        if k is None:
            n -= 1
            num[i] = n
        elif k[0] == "gap":
            n = k[1]
    ents = [(num[i], raw[i]) for i in real]
    tail_marks = [k[1] for k in kinds if k and k[0] == "tail"]
    total_last = (tail_marks[-1] - 1) if tail_marks else ents[-1][0]
    mi = real.index(m)
    nb = [len(t.encode("utf-8")) + 1 for _n, t in ents]
    has_heading = mi > 0 and ents[0][1].lstrip().startswith("#")

    def _cost(lo: int, hi: int) -> int:
        c = sum(nb[lo:hi + 1])
        c += sum(_MARK_BYTES_EST for j in range(lo, hi) if ents[j + 1][0] > ents[j][0] + 1)
        if has_heading and lo > 0:
            c += nb[0] + (_MARK_BYTES_EST if ents[lo][0] > ents[0][0] + 1 else 0)
        if ents[hi][0] < total_last:
            c += _MARK_BYTES_EST
        return c

    lo = hi = mi
    if _cost(lo, hi) > max_bytes:
        # 一致行だけでも収まらない: 見出しは予算の 1/4 まで、一致行は一致位置の前後に切る
        head = _clip_utf8_bytes(ents[0][1], max_bytes // 4) if has_heading else None
        marks = _MARK_BYTES_EST * ((1 if has_heading and ents[mi][0] > ents[0][0] + 1 else 0)
                                   + (1 if ents[mi][0] < total_last else 0))
        room = max_bytes - marks - (len(head.encode("utf-8")) + 1 if head is not None else 0) - 1
        if room < 64:
            head, room = None, max_bytes - marks - 1
        kept = ([(ents[0][0], head)] if head is not None else []) + [(ents[mi][0], _clip_line_around(ents[mi][1], ql, max(1, room)))]
        return _clip_utf8_bytes(_assemble_lines(kept, total_last), max_bytes)
    while True:
        grew = False
        if lo > 0 and _cost(lo - 1, hi) <= max_bytes:
            lo -= 1
            grew = True
        if hi < len(ents) - 1 and _cost(lo, hi + 1) <= max_bytes:
            hi += 1
            grew = True
        if not grew:
            break
    kept = ([ents[0]] if has_heading and lo > 0 else []) + ents[lo:hi + 1]
    return _clip_utf8_bytes(_assemble_lines(kept, total_last), max_bytes)
