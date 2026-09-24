"""「廃止」運用の構造マーカーを Office 原本から直接走査する（sherpa/ 非依存）。

xlsx/docx/pptx を zip+XML として直接読み、次のマーカーが実在するかどうかの**一次事実**を報告する:

- xlsx: セルを覆う図形・画像（drawing anchor）／取り消し線（font.strike）／非表示シート／
  非表示行・列
- docx: 取り消し線（``w:strike``/``w:dstrike``）／隠し文字（``w:vanish``）／
  変更履歴削除（``w:del``）
- pptx: 非表示スライド（``p:sld/@show="0"``）／スライド内シェイプの位置・塗り・テキスト有無
  （前面図形による覆い・スライド外配置の判定材料）

``sherpa/`` の取り込みパイプライン（``evidence_spike.py``・``arms/ooxml_arm.py``。L3 レーンが並行して
書き換え中）には一切依存しない。ここで扱うのは「原本にその表現が存在するか」という一次事実のみで、
sherpa 側がそれを Evidence IR にどう取り込むかの検証は ``tests/unit/test_deprecation_marker_acceptance.py``
の役割（責務分離）。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import openpyxl

_NS_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"

_OFFICE_SUFFIXES = (".xlsx", ".docx", ".pptx")


def _qn(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def scan_xlsx(path: Path) -> dict[str, Any]:
    """xlsx 1ファイルの構造マーカーを報告する。値を持たない項目はキーごと省略する。"""
    result: dict[str, Any] = {
        "strikethrough_cells": [],
        "hidden_sheets": [],
        "very_hidden_sheets": [],
        "hidden_rows": {},
        "hidden_columns": {},
        "drawing_anchors": [],
    }
    wb = openpyxl.load_workbook(path)
    try:
        for ws in wb.worksheets:
            if ws.sheet_state == "hidden":
                result["hidden_sheets"].append(ws.title)
            elif ws.sheet_state == "veryHidden":
                result["very_hidden_sheets"].append(ws.title)
            hidden_rows = sorted(r for r, dim in ws.row_dimensions.items() if dim.hidden)
            if hidden_rows:
                result["hidden_rows"][ws.title] = hidden_rows
            hidden_cols = sorted(letter for letter, dim in ws.column_dimensions.items() if dim.hidden)
            if hidden_cols:
                result["hidden_columns"][ws.title] = hidden_cols
            for row in ws.iter_rows():
                for cell in row:
                    if cell.value is not None and cell.font and cell.font.strike:
                        result["strikethrough_cells"].append(f"{ws.title}!{cell.coordinate}")
    finally:
        wb.close()

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for name in sorted(n for n in names if n.startswith("xl/drawings/drawing") and n.endswith(".xml")):
            root = ET.fromstring(z.read(name))
            for anchor in list(root):
                kind = anchor.tag.split("}")[-1]
                if kind not in ("twoCellAnchor", "oneCellAnchor"):
                    continue
                has_pic = anchor.find(_qn(_NS_XDR, "pic")) is not None
                has_sp = anchor.find(_qn(_NS_XDR, "sp")) is not None
                if not (has_pic or has_sp):
                    continue  # コネクタ・グループ・チャート等はここでは対象外（覆い判定に使わない）
                frm = anchor.find(_qn(_NS_XDR, "from"))
                from_cell = None
                if frm is not None:
                    col, row = frm.find(_qn(_NS_XDR, "col")), frm.find(_qn(_NS_XDR, "row"))
                    if col is not None and row is not None:
                        from_cell = [int(col.text or 0), int(row.text or 0)]
                text = "".join(t.text or "" for t in anchor.findall(f".//{_qn(_NS_A, 't')}")) if has_sp else ""
                result["drawing_anchors"].append({
                    "part": name,
                    "kind": "picture" if has_pic else "shape",
                    "from_cell_zero_based": from_cell,
                    "text": text,
                })
    return {k: v for k, v in result.items() if v}


def scan_docx(path: Path) -> dict[str, Any]:
    """docx 1ファイルの構造マーカーを報告する。ヘッダー/フッターは対象外（本文のみ）。"""
    result: dict[str, Any] = {"strike_runs": [], "double_strike_runs": [], "hidden_runs": [], "deleted_runs": []}
    with zipfile.ZipFile(path) as z:
        if "word/document.xml" not in z.namelist():
            return {}
        root = ET.fromstring(z.read("word/document.xml"))
        for r in root.iter(_qn(_NS_W, "r")):
            r_pr = r.find(_qn(_NS_W, "rPr"))
            text = "".join(t.text or "" for t in r.findall(_qn(_NS_W, "t")))
            if r_pr is None or not text:
                continue
            if r_pr.find(_qn(_NS_W, "strike")) is not None:
                result["strike_runs"].append(text)
            if r_pr.find(_qn(_NS_W, "dstrike")) is not None:
                result["double_strike_runs"].append(text)
            if r_pr.find(_qn(_NS_W, "vanish")) is not None:
                result["hidden_runs"].append(text)
        for d in root.iter(_qn(_NS_W, "del")):
            text = "".join(t.text or "" for t in d.iter(_qn(_NS_W, "delText")))
            if text:
                result["deleted_runs"].append(text)
    return {k: v for k, v in result.items() if v}


def scan_pptx(path: Path) -> dict[str, Any]:
    """pptx 1ファイルの構造マーカーを報告する（非表示スライド／シェイプの位置・塗り・テキスト有無）。"""
    result: dict[str, Any] = {"hidden_slides": [], "shapes_by_slide": {}}
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        slide_parts = sorted(n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
        for part in slide_parts:
            root = ET.fromstring(z.read(part))
            if root.get("show") == "0":
                result["hidden_slides"].append(part)
            sptree = root.find(f".//{_qn(_NS_P, 'cSld')}/{_qn(_NS_P, 'spTree')}")
            if sptree is None:
                continue
            shapes = []
            for sp in sptree.findall(_qn(_NS_P, "sp")):
                text = "".join(t.text or "" for t in sp.findall(f".//{_qn(_NS_A, 't')}"))
                xfrm = sp.find(f".//{_qn(_NS_A, 'xfrm')}")
                bbox = None
                if xfrm is not None:
                    off, ext = xfrm.find(_qn(_NS_A, "off")), xfrm.find(_qn(_NS_A, "ext"))
                    if off is not None and ext is not None:
                        x0, y0 = int(off.get("x", 0)), int(off.get("y", 0))
                        bbox = [x0, y0, x0 + int(ext.get("cx", 0)), y0 + int(ext.get("cy", 0))]
                shapes.append({
                    "text": text,
                    "bbox_emu": bbox,
                    "has_text": bool(text),
                    "has_solid_fill": sp.find(f".//{_qn(_NS_A, 'solidFill')}") is not None,
                })
            if shapes:
                result["shapes_by_slide"][part] = shapes
    return {k: v for k, v in result.items() if v}


def scan_file(path: Path) -> dict[str, Any] | None:
    """拡張子から適切な scan_* を呼ぶ。対象外拡張子は None。"""
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return scan_xlsx(path)
    if suffix == ".docx":
        return scan_docx(path)
    if suffix == ".pptx":
        return scan_pptx(path)
    return None


def build_inventory(roots: list[Path], repo_root: Path) -> dict[str, Any]:
    """``roots`` 配下の xlsx/docx/pptx を全走査し、``{repo相対パス: 所見}`` を決定的な順序で返す。

    所見が空（マーカーが1つも無い）ファイルはキーごと省略する（ノイズを避ける）。
    """
    inventory: dict[str, Any] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _OFFICE_SUFFIXES:
                continue
            findings = scan_file(path)
            if findings:
                inventory[path.relative_to(repo_root).as_posix()] = findings
    return inventory
