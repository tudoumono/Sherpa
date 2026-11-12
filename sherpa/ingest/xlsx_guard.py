"""xlsx を openpyxl で開く前の大きさ検査: シート XML を流し読みして、実際のセル数と結合セルの範囲の面積を数える。
設計: docs/design/rag.md「人向け MD と RAG 正本の作り分け（マージの実際）」
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_REF_RE = re.compile(r"^\$?([A-Za-z]+)\$?(\d+)$")
_MAX_DEPTH = 64


def _col_num(col: str) -> int:
    n = 0
    for c in col.upper():
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n


def merge_area(ref: str) -> int:
    """結合範囲 `A1:CV1000` の面積（行×列）。読めない形は 0。"""
    parts = ref.strip().split(":")
    if len(parts) not in (1, 2):
        return 0
    pts = []
    for part in parts:
        m = _REF_RE.match(part)
        if not m:
            return 0
        pts.append((_col_num(m.group(1)), int(m.group(2))))
    if len(pts) == 1:
        return 1
    (c1, r1), (c2, r2) = pts
    return (abs(c2 - c1) + 1) * (abs(r2 - r1) + 1)


def cell_load(rp: Path, cap: int) -> int | None:
    """全シートの「実セル数＋結合範囲とハイパーリンクの範囲の面積」の和。自己申告の dimension には頼らない。`cap` を超えた時点で打ち切る。
    壊れた zip・シート皆無は None。XML の壊れたシートは飛ばし、ほかのシートは数える。
    """
    try:
        with zipfile.ZipFile(rp) as zf:
            names = [n for n in zf.namelist() if n.startswith("xl/worksheets/") and n.lower().endswith(".xml")]
            if not names:
                return None
            total = 0
            for name in names:
                try:
                    total = _sheet_load(zf, name, total, cap)
                except ET.ParseError:
                    continue   # 壊れたシートは openpyxl 側で失敗する。数えた分は持ち越す
                if total > cap:
                    return total
            return total
    except (OSError, zipfile.BadZipFile):
        return None


def _sheet_load(zf: zipfile.ZipFile, name: str, total: int, cap: int) -> int:
    """1 シートの実セル数・結合範囲・ハイパーリンクの範囲の面積を `total` に足す。深すぎる入れ子と `cap` 超えは `cap + 1` 以上を返す。"""
    with zf.open(name) as fh:
        stack: list[ET.Element] = []
        for event, elem in ET.iterparse(fh, events=("start", "end")):
            if event == "end":
                stack.pop()
                if stack:
                    stack[-1].clear()   # 読み終えた子を捨ててメモリを一定に保つ
                continue
            stack.append(elem)
            if len(stack) > _MAX_DEPTH:
                return cap + 1
            local = elem.tag.rsplit("}", 1)[-1]   # 属性は開いた時点で読む（子が閉じると親の属性は消える）
            if local == "c":
                total += 1
            elif local in ("mergeCell", "hyperlink"):   # openpyxl はどちらも範囲の全セルを作る
                total += merge_area(elem.get("ref", ""))
            if total > cap:
                return total
    return total
