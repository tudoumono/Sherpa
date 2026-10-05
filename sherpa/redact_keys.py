"""秘密鍵ブロック（PEM `PRIVATE KEY`）を、要素をまたいだ状態付きで伏せ字にする。

`KeyBlockRedactor` は「鍵ブロックの内側か」を呼び出し間で持ち越す callable。
呼び出し元は 1 つの原本につき使い捨てのインスタンスを作り、要素を原本の出現順に `redactor(text)` へ渡す
（複数の原本・複数回の呼び出しで使い回さない）。
"""
from __future__ import annotations

import re
from typing import Callable

_UNTERMINATED_PRIVATE_KEY_RE = re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----")
_PRIVATE_KEY_END_RE = re.compile(r"-----END[^-]*PRIVATE KEY-----")


class KeyBlockRedactor:
    """`base_clean` を土台に、鍵ブロックの状態を持ち越す。

    BEGIN/END の検出は生文字列に対して行い、鍵ブロックの外側にだけ `base_clean` を適用する。内側は無条件で `[REDACTED]`。
    """
    __slots__ = ("_base_clean", "_in_key_block")

    def __init__(self, base_clean: Callable[[str], str]):
        self._base_clean = base_clean
        self._in_key_block = False

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
