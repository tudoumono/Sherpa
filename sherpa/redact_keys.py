"""秘密鍵ブロック（PEM `PRIVATE KEY`）を、要素をまたいだ状態付きで伏せ字にする。

`KeyBlockRedactor` は「鍵ブロックの内側か」を呼び出し間で持ち越す callable。
呼び出し元は 1 つの原本につき使い捨てのインスタンスを作り、要素を原本の出現順に `redactor(text)` へ渡す
（複数の原本・複数回の呼び出しで使い回さない）。
"""
from __future__ import annotations

import re
from typing import Callable

_UNTERMINATED_PRIVATE_KEY_RE = re.compile(r"-----BEGIN[^-]*PRIVATE KEY(?: BLOCK)?-----")
_PRIVATE_KEY_END_RE = re.compile(r"-----END[^-]*PRIVATE KEY(?: BLOCK)?-----")

# 鍵の本文らしい行（base64 のみの 1 行）。BEGIN/END が窓の外にあって状態を辿れなかったときの最後の砦で、
# 連続 2 行以上・1 行が 40 文字以上・各行の文字種が 12 以上（同じ文字の繰り返しは除く）のとき、
# または 1 行だけでも 60 文字以上で英大文字・英小文字・数字を全部含むときに伏せる（行頭の「行番号: 」と前後の空白・タブは許容）。
# 16 進数だけの行（ハッシュ値の列など）は鍵の本文として扱わない。
_BODY_LINE_RE = re.compile(r"(?:\d+: )?[ \t]*([A-Za-z0-9+/]{16,}={0,2})[ \t]*")
_HEX_ONLY_RE = re.compile(r"[0-9A-Fa-f]+")
_BODY_LONG_MIN = 40
_BODY_SINGLE_MIN = 60
_BODY_DISTINCT_MIN = 12


def _is_body_line(line: str) -> bool:
    m = _BODY_LINE_RE.fullmatch(line)
    return bool(m) and len(set(m.group(1))) >= _BODY_DISTINCT_MIN and not _HEX_ONLY_RE.fullmatch(m.group(1))


def _is_single_body_line(line: str) -> bool:
    m = _BODY_LINE_RE.fullmatch(line)
    body = m.group(1) if m else ""
    return (len(body) >= _BODY_SINGLE_MIN and _is_body_line(line)
            and any(c.isupper() for c in body) and any(c.islower() for c in body)
            and any(c.isdigit() for c in body))


def mask_orphan_key_body(text: str) -> str:
    """BEGIN/END の無い base64 の行（鍵の本文の断片）を行ごと `[REDACTED]` にする。"""
    if not text:
        return text
    lines = text.split("\n")
    out = list(lines)
    i, n = 0, len(lines)
    while i < n:
        if not _is_body_line(lines[i]):
            i += 1
            continue
        j = i
        while j < n and _is_body_line(lines[j]):
            j += 1
        if (j - i >= 2 and any(len(x) >= _BODY_LONG_MIN for x in lines[i:j])) or (
                j - i == 1 and _is_single_body_line(lines[i])):
            for k in range(i, j):
                out[k] = "[REDACTED]"
        i = j
    return "\n".join(out)


def redact_window_lines(lines: list[str], base_clean: Callable[[str], str], in_key_block: bool = False) -> list[str]:
    """窓の行（行番号なしの生行）を伏せる。`in_key_block` は窓の直前までを辿った鍵ブロックの状態。
    窓の途中で始まる／終わる鍵も、状態の持ち越しと base64 連続行の判定で伏せる。"""
    red = KeyBlockRedactor(base_clean, in_key_block=in_key_block)
    return mask_orphan_key_body("\n".join(red(x) for x in lines)).split("\n") if lines else []


class KeyBlockRedactor:
    """`base_clean` を土台に、鍵ブロックの状態を持ち越す。

    BEGIN/END の検出は生文字列に対して行い、鍵ブロックの外側にだけ `base_clean` を適用する。内側は無条件で `[REDACTED]`。
    """
    __slots__ = ("_base_clean", "_in_key_block")

    def __init__(self, base_clean: Callable[[str], str], in_key_block: bool = False):
        self._base_clean = base_clean
        self._in_key_block = in_key_block

    @property
    def in_key_block(self) -> bool:
        return self._in_key_block

    def track(self, s: str) -> None:
        """出力せず、鍵ブロックの内側かだけを進める（窓より前の行を辿る用）。"""
        pos = 0
        while s:
            m = (_PRIVATE_KEY_END_RE if self._in_key_block else _UNTERMINATED_PRIVATE_KEY_RE).search(s, pos)
            if m is None:
                return
            self._in_key_block = not self._in_key_block
            pos = m.end()

    def __call__(self, s: str) -> str:
        if not s:
            return s
        out: list[str] = []
        pos = 0
        n = len(s)
        while pos < n:
            if self._in_key_block:
                m = _PRIVATE_KEY_END_RE.search(s, pos)
                if m is None:
                    out.append("[REDACTED]")
                    pos = n
                    break
                out.append("[REDACTED]")
                pos = m.end()
                self._in_key_block = False
                continue
            m = _UNTERMINATED_PRIVATE_KEY_RE.search(s, pos)
            if m is None:
                out.append(self._base_clean(s[pos:]))
                pos = n
                break
            if m.start() > pos:
                out.append(self._base_clean(s[pos:m.start()]))
            self._in_key_block = True
            pos = m.start()
        return "".join(out)
