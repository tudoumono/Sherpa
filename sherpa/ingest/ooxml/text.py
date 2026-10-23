"""図形・テキストボックス内の文字を段落ごとに区切って取り出す共通ヘルパ（Word の `w:`／DrawingML の `a:` で共通）。"""
from __future__ import annotations


def paragraph_separated_text(root, ns: str) -> str:
    """`root` 配下の文字（`ns` 名前空間の `t`）を、段落（同 `p`）ごとに改行で区切って返す。空の段落は数えない。

    守ること: 段落をまたいで値を連結しない（「10」「20」の 2 段落を「1020」にしない）。
    """
    marks: list[str] = []
    for node in root.iter():
        if node.tag == f"{ns}p":
            marks.append("\n")
        elif node.tag == f"{ns}t":
            marks.append(node.text or "")
    return "\n".join(part for part in "".join(marks).split("\n") if part)
