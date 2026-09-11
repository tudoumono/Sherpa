"""秘密鍵ブロック（PEM `PRIVATE KEY`）を、要素をまたいだ状態付きで伏せ字にする共有モジュール。

`agentic_search._redact`（正規表現ベースの伏せ字）は BEGIN/END が同一の1文字列内で対に
ならないとマッチしない。原本読取ツール（`doc_readers`）が返す段落・セル・スライド・ページ等は
1要素＝1文字列に分かれているため、鍵ブロックが要素をまたぐ（例: BEGIN が段落、鍵本文が次の表の
セル、END がさらに次の段落）と、要素ごとに独立した伏せ字では鍵本文が素通りする。

`KeyBlockRedactor` は「鍵ブロックの内側にいるか」を呼び出し間で持ち越す callable にすることで、
呼び出し元が**原本の出現順**に1要素ずつ渡せば、この順序をまたいだ鍵ブロックも見失わない。

呼び出し元の契約: 1回のツール呼び出し（1つの原本を1回読む）につき使い捨てのインスタンスを
作り、その原本の要素を**原本の出現順**（`doc_readers` 各関数のツール別ロジックが辿る並び）に
`redactor(text)` で処理する。複数の原本・複数回の呼び出しを跨いで同じインスタンスを使い回さない。
"""
from __future__ import annotations

import re
from typing import Callable

_UNTERMINATED_PRIVATE_KEY_RE = re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----")
_PRIVATE_KEY_END_RE = re.compile(r"-----END[^-]*PRIVATE KEY-----")


class KeyBlockRedactor:
    """`base_clean`（例: `agentic_search._redact`）を土台に、鍵ブロックの状態を持ち越す callable。

    BEGIN/END の検出は**生文字列**（`base_clean` を掛ける前）に対して行う——`base_clean` の
    kv 秘密パターン（`key: value` 形の一般名を伏せる）は `\\s*` が改行もまたぐため、
    `"secret:\\n-----BEGIN..."` のように鍵の直前にラベルが付くと BEGIN の文字列そのものを
    巻き込んで消してしまい、以後 BEGIN マーカーを検出できず状態が立たない（結果として鍵本文が
    伏せられずに漏れる）。そのため鍵ブロックの外側（BEGIN より前・END より後）にだけ
    `base_clean` を適用し、鍵ブロックの内側は常に無条件で `[REDACTED]` に置き換える。
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
                    out.append("[REDACTED]")   # END がまだ来ない→残り全部を伏せて状態を持ち越す
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
            pos = m.start()   # 次の反復で in_key_block 分岐が BEGIN 込みで伏せる
        return "".join(out)
