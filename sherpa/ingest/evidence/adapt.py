"""Document IR を Evidence IR へ写し、覆い・状態の根拠を付ける。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from .. import document_ir, evidence_ir, observation_vocab
from .base import _element_id
from .builder import _Builder
from .drawing import _area, _bbox_intersection
from .package import _a1, _column_letter


def _apply_cover_state(
    builder: _Builder,
    back_id: str,
    *,
    file_type: str,
    front_of: Callable[[str], evidence_ir.EvidenceElement] | None = None,
) -> None:
    """背面の要素の visibility・visibility_reason・occluded_by・covered_by_text を、その要素へ向かう covers から導く。

    遮蔽の正本は covers で、背面の状態は xlsx・pptx ともこの関数だけが書く。
    ① occlusion=confirmed の covers があれば、最後に作った（最も手前の）前面で hidden にし、前面を occluded_by に残す
    ② なければ、文字を持つ前面（none_with_text）を covered_by_text に残す（visibility は変えない）。
    すでに別の理由（原本の非表示の指定・スライド範囲外など遮蔽由来でないもの）を持つ要素は、可視性と理由を保つ。`front_of` は前面の要素の引き当て（既定は builder の要素）。
    """
    covers = builder.covers_to(back_id)
    if not covers:
        return
    lookup = front_of or builder.element
    back = builder.element(back_id)
    extension = dict(back.extension)
    visibility = back.visibility
    confirmed = [c for c in covers if c.extension.get("occlusion") == observation_vocab.OCCLUSION_CONFIRMED]
    if confirmed:
        front = lookup(confirmed[-1].source_id)
        occluded_by: dict[str, Any] = {
            "kind": front.type, "element_id": front.element_id, "z_order": front.extension.get("z_order")}
        if front.value:
            occluded_by["text"] = front.value
        elif front.extension.get("name"):
            occluded_by["name"] = front.extension["name"]
        extension["occluded_by"] = occluded_by
        # 原本の直接の指定（非表示の行・列・スライドなど）の理由は、覆いで置き換えない。覆いは occluded_by と covers に残す。
        existing_reason = extension.get("visibility_reason")
        if not existing_reason or existing_reason in observation_vocab.COVER_DERIVED_REASONS:
            visibility = "hidden"
            extension["visibility_reason"] = observation_vocab.occlusion_reason(file_type, front.type)
    else:
        text_cover = next(
            (c for c in covers if c.extension.get("fill") == observation_vocab.FILL_NONE_WITH_TEXT), None)
        if text_cover is None:
            return
        front = lookup(text_cover.source_id)
        extension["covered_by_text"] = {"element_id": front.element_id, "text": front.value}
    builder.set_state(back_id, visibility=visibility, extension=extension)


def _bbox_cover_ratio(back_bbox: Any, front_bbox: Any) -> float | None:
    """背面の面積のうち前面と重なる割合。どちらかの bbox が無い・重ならないときは None。"""
    if not back_bbox or not front_bbox:
        return None
    intersection = _bbox_intersection(list(back_bbox), list(front_bbox))
    back_area = _area(list(back_bbox))
    if intersection is None or not back_area:
        return None
    return _area(intersection) / back_area


def _annotate_state_basis(ir: evidence_ir.EvidenceIR) -> None:
    """可視性の理由と要素全体の書式の状態に、由来（原本の直接の指定 / 幾何の判定）を `extension.state_basis` で付ける。"""
    for element in ir.elements:
        extension = element.extension
        basis: dict[str, str] = {}
        visibility_basis = observation_vocab.state_basis_for(extension.get("visibility_reason"))
        if visibility_basis is not None:
            basis["visibility"] = visibility_basis
        if extension.get("format_state"):
            basis["format"] = observation_vocab.STATE_BASIS_OOXML_DIRECT
        if basis:
            extension["state_basis"] = basis


def _part_for_element(file_type: str, element: document_ir.Element, slide_parts: dict[int, str]) -> str:
    source_map = element.source_map
    if file_type == "docx":
        fixed_parts = {
            "comment": "word/comments.xml",
            "footnote": "word/footnotes.xml",
            "endnote": "word/endnotes.xml",
        }
        if element.type in fixed_parts:
            return fixed_parts[element.type]
        part = source_map.get("part")
        if part:
            return part if "/" in part else f"word/{part}"
        return "word/document.xml"
    if file_type == "pptx":
        return source_map.get("part") or slide_parts.get(source_map.get("slide"), "ppt/presentation.xml")
    if file_type == "xlsx":
        return "xl/workbook.xml"
    return f"{file_type}:document"


def _locator_for_legacy_element(
    file_type: str,
    element: document_ir.Element,
    slide_parts: dict[int, str],
) -> evidence_ir.Locator:
    sm = element.source_map
    cell_range = sm.get("range") or sm.get("cell")
    return evidence_ir.Locator(
        part=_part_for_element(file_type, element, slide_parts),
        slide=sm.get("slide") if isinstance(sm.get("slide"), int) else None,
        sheet=sm.get("sheet") if isinstance(sm.get("sheet"), str) else None,
        cell_range=cell_range if isinstance(cell_range, str) else None,
        object_id=element.element_id,
        bbox=_bounds_to_bbox(sm.get("bounds")),
        extension={"document_ir_source_map": sm},
    )


def _bounds_to_bbox(bounds: Any) -> list[int | float] | None:
    if not isinstance(bounds, list) or len(bounds) != 4 or not all(isinstance(item, (int, float)) for item in bounds):
        return None
    x, y, cx, cy = bounds
    return [x, y, x + cx, y + cy]


def _legacy_slide_parts(legacy: document_ir.DocumentIR) -> dict[int, str]:
    return {
        element.source_map["slide"]: element.source_map["part"]
        for element in legacy.elements
        if element.type == "slide"
        and isinstance(element.source_map.get("slide"), int)
        and isinstance(element.source_map.get("part"), str)
    }


def _legacy_extension(
    element: document_ir.Element,
    legacy_by_id: dict[str, document_ir.Element],
    ids: dict[str, str],
) -> dict[str, Any]:
    """現行 IR の要素 1 つ分の Evidence extension（由来・抽出情報・前面テキスト・書式の状態・浮動図形）。"""
    extension: dict[str, Any] = {
        "origin": "document-ir-v2-adapter",
        "extraction": asdict(element.extraction),
        "visibility_reason": element.visibility_reason,
    }
    # pptx の前面テキストによる重ね（`covered_by_text`）は `source_map` に前面要素の内部 ID しか無いため、
    # 前面要素を解決して Evidence element_id と前面テキストをこの要素の extension へ直接持たせる（意味の断定はしない）。
    covered_by_text_id = element.source_map.get("covered_by_text")
    if isinstance(covered_by_text_id, str):
        front_legacy = legacy_by_id.get(covered_by_text_id)
        front_evidence_id = ids.get(covered_by_text_id)
        if front_legacy is not None and front_evidence_id is not None:
            extension["covered_by_text"] = {"element_id": front_evidence_id, "text": front_legacy.text}
    # 要素全体の取り消し線などの書式の状態は可視性と分けて持つ。
    format_state = element.source_map.get("format_state")
    if isinstance(format_state, dict) and format_state:
        extension["format_state"] = dict(format_state)
    # docx の浮動図形と段落の関連付けの事実（幾何断定なし）を Evidence extension へ運ぶ。
    # `_docx_host_objects` の EMU 座標 `overlaps` とは別物なのでキー名（"floating_anchors"）で区別する。
    floating_anchors = element.source_map.get("floating_anchors")
    if isinstance(floating_anchors, list) and floating_anchors:
        extension["floating_anchors"] = floating_anchors
    return extension


def _adapt_cells(
    builder: _Builder,
    element: document_ir.Element,
    locator: evidence_ir.Locator,
    element_id: str,
    parent_coverage_id: str,
    file_type: str,
    xlsx_strike_cells: set[tuple[str | None, str | None]],
) -> None:
    """表要素の cell を独立 element として足す。xlsx は非表示行・列と取り消し線を cell 自身へ反映する。"""
    # xlsx はこの表要素の source_map が表の範囲に絞った非表示行/列を持つため、cell ごとに判定する集合へ先に変換する
    hidden_rows = set(element.source_map.get("hidden_rows") or []) if file_type == "xlsx" else set()
    hidden_columns = set(element.source_map.get("hidden_columns") or []) if file_type == "xlsx" else set()
    for cell_order, cell in enumerate(element.cells or [], start=1):
        cell_range = _a1(cell.row, cell.column) if file_type == "xlsx" else None
        cell_locator = evidence_ir.Locator(
            part=locator.part,
            sheet=locator.sheet,
            cell_range=cell_range,
            # XLSXはsheet+cell_rangeで一意。DOCX等はcell_rangeが無いためobject IDを残す。
            object_id=(
                f"{element.element_id}/cell:{cell.row}:{cell.column}"
                if file_type != "xlsx" else None
            ),
            extension={
                "row": cell.row,
                "column": cell.column,
                "row_span": cell.row_span,
                "column_span": cell.column_span,
            },
        )
        cell_visibility = element.visibility
        cell_extension: dict[str, Any] = {"role": cell.role}
        if file_type == "xlsx":
            # 可視性: 非表示行 → 非表示列。取り消し線は本文が読めるので可視性を変えず、書式の状態として別に持つ。
            if cell.row in hidden_rows:
                cell_visibility = "hidden"
                cell_extension["visibility_reason"] = "hidden_row"
            elif _column_letter(cell.column) in hidden_columns:
                cell_visibility = "hidden"
                cell_extension["visibility_reason"] = "hidden_column"
            if (locator.sheet, cell_range) in xlsx_strike_cells:
                cell_extension["format_state"] = observation_vocab.whole_strike_state()
        builder.add_element(
            "cell",
            cell_locator,
            parent_id=element_id,
            order=cell_order,
            value=cell.text,
            coverage_id=parent_coverage_id,
            visibility=cell_visibility,
            lifecycle=element.status,
            extension=cell_extension,
        )
        # 親子所属はparent_idで一意に表せるため、cellごとの冗長belongs_to relationは作らない。
        # 30万cell級でrelation/coverageを二重に持つメモリ増幅を避ける。


def _adapt_document_ir(
    builder: _Builder,
    legacy: document_ir.DocumentIR | None,
    *,
    consume_legacy: bool = False,
) -> None:
    """現行 IR を共通 core へ写す。cell は独立 element とし、parent_id で table 所属を明示する。

    ``consume_legacy`` は通常取り込みの巨大 XLSX 用。既存 IR 成果物と chunk を書き終えた後に限って使い、
    table ごとの cell を Evidence へ移し終えた時点で元 list を空にしてピークメモリを抑える。
    xlsx の cell は、親 `table:N` の `source_map` の `hidden_rows`/`hidden_columns` と `strike_text` 要素を突き合わせ、
    cell 自身の `visibility`/`extension["visibility_reason"]`（非表示行・列）と `extension["format_state"]`（取り消し線）へ直接反映する。
    """
    if legacy is None:
        return
    file_type = legacy.source.file_type
    slide_parts = _legacy_slide_parts(legacy)
    ids: dict[str, str] = {}
    legacy_by_id: dict[str, document_ir.Element] = {}
    # parentは現行IRで必ず先に現れるが、将来の並び替えに依存しないよう2段でIDを確定する。
    for element in legacy.elements:
        locator = _locator_for_legacy_element(file_type, element, slide_parts)
        ids[element.element_id] = _element_id(element.type, locator, element.order)
        legacy_by_id[element.element_id] = element

    # xlsx の取り消し線（独立の `strike_text` 要素）を対応する cell 要素へも反映するため、`(sheet, cell)` の集合を先に作る
    xlsx_strike_cells: set[tuple[str | None, str | None]] = (
        {(el.source_map.get("sheet"), el.source_map.get("cell"))
         for el in legacy.elements if el.type == "strike_text"}
        if file_type == "xlsx" else set()
    )

    for element in legacy.elements:
        locator = _locator_for_legacy_element(file_type, element, slide_parts)
        truncated = bool(element.source_map.get("truncated"))
        extension = _legacy_extension(element, legacy_by_id, ids)
        element_id = builder.add_element(
            element.type,
            locator,
            parent_id=ids.get(element.parent_id) if element.parent_id else None,
            order=element.order,
            value=element.text,
            coverage_status="unsupported" if truncated else "extracted",
            coverage_reason="scan_cap_reached" if truncated else None,
            visibility=element.visibility,
            lifecycle=element.status,
            extension=extension,
        )
        if element_id != ids[element.element_id]:
            raise ValueError("legacy element ID calculation drift")
        parent_coverage_id = builder.coverage_for(element_id)
        _adapt_cells(builder, element, locator, element_id, parent_coverage_id, file_type, xlsx_strike_cells)
        if consume_legacy and element.cells:
            element.cells.clear()
