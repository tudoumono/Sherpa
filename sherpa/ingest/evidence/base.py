"""Evidence IR の共通部品（名前空間・版・ID・原本情報）。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .. import evidence_ir


_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_PIC = "{http://schemas.openxmlformats.org/drawingml/2006/picture}"
_WPS = "{http://schemas.microsoft.com/office/word/2010/wordprocessingShape}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XDR = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
_C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
_DGM = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}"
_V = "{urn:schemas-microsoft-com:vml}"
_O = "{urn:schemas-microsoft-com:office:office}"
_CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"


# 各形式の adapter 版。Evidence の中身が変わる変更をしたら上げる。
XLSX_ADAPTER_VERSION = "xlsx-evidence-adapter-v7"
DOCX_ADAPTER_VERSION = "docx-evidence-adapter-v6"
PPTX_ADAPTER_VERSION = "pptx-evidence-adapter-v6"
PDF_ADAPTER_VERSION = "pdf-evidence-adapter-v5"
PDF_XOBJECT_MAX_DEPTH = 16
PDF_XOBJECT_MAX_RESOLUTIONS = 4096


@dataclass(frozen=True)
class _ContentHost:
    """OOXML内で独立したrelationshipを持つcontent-bearing XML part。"""

    part: str
    host_kind: str
    host_visibility: str
    host_lifecycle: str = "active"
    slide: int | None = None
    visibility: str = "visible"
    lifecycle: str = "active"

    def metadata(self) -> dict[str, Any]:
        return {
            "host_part": self.part,
            "host_kind": self.host_kind,
            "host_visibility": self.host_visibility,
            "host_lifecycle": self.host_lifecycle,
        }


def _canonical(value: Any) -> str:
    return evidence_ir.compact_json(value)


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = "\n".join(_canonical(part) for part in parts)
    return f"{prefix}:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


_json_str = json.encoder.encode_basestring


def _scalar_json(value: Any) -> str | None:
    """None・真偽・整数・文字列だけを JSON の断片にする（それ以外は None＝直列化器に任せる）。"""
    kind = type(value)
    if value is None:
        return "null"
    if kind is str:
        return _json_str(value)
    if kind is int:
        return int.__repr__(value)
    if kind is bool:
        return "true" if value else "false"
    return None


def _locator_json(locator: evidence_ir.Locator) -> str | None:
    """`_canonical(locator_payload(locator))` と同じ文字列を、直列化器を通さずに作る。作れない形（座標・入れ子の値など）は None。"""
    if locator.bbox is not None:
        return None
    part, sheet, cell_range = locator.part, locator.sheet, locator.cell_range
    object_id, page, slide = locator.object_id, locator.page, locator.slide
    if (type(part) is str and (sheet is None or type(sheet) is str) and (cell_range is None or type(cell_range) is str)
            and object_id is None and page is None and slide is None):
        part_json = _json_str(part)                                  # xlsx のセルの原本位置の形
        sheet_json = "null" if sheet is None else _json_str(sheet)
        cell_json = "null" if cell_range is None else _json_str(cell_range)
        object_json = page_json = slide_json = "null"
    else:
        fields = [_scalar_json(value) for value in (part, sheet, cell_range, object_id, page, slide)]
        if None in fields:
            return None
        part_json, sheet_json, cell_json, object_json, page_json, slide_json = fields
    extension = locator.extension
    items: list[str] = []
    for key in sorted(extension):
        value = extension[key]
        if type(key) is not str:
            return None
        if type(value) is int:
            items.append(f"{_json_str(key)}:{value}")
            continue
        value_json = _scalar_json(value)
        if value_json is None:
            return None
        items.append(f"{_json_str(key)}:{value_json}")
    return (
        f'{{"bbox":null,"cell_range":{cell_json},"extension":{{{",".join(items)}}},'
        f'"object_id":{object_json},"page":{page_json},"part":{part_json},'
        f'"sheet":{sheet_json},"slide":{slide_json}}}'
    )


def _element_id(element_type: str, locator: evidence_ir.Locator, order: int) -> str:
    located = _locator_json(locator)
    if located is not None and type(order) is int and type(element_type) is str:
        payload = f'{_json_str(element_type)}\n{located}\n{int.__repr__(order)}'
        return "evidence:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    try:
        return _stable_id("evidence", element_type, evidence_ir.locator_payload(locator), order)
    except TypeError:                                # 入れ子の dataclass など、dict への展開が要る値は asdict で変換する
        return _stable_id("evidence", element_type, asdict(locator), order)


def _source(path: Path) -> evidence_ir.EvidenceSource:
    return evidence_ir.EvidenceSource(
        file_type=path.suffix.lower().lstrip("."),
        content_hash="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
    )
