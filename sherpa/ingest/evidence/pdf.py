"""PDF のページごとの object inventory。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import evidence_ir
from .base import PDF_ADAPTER_VERSION
from .builder import _Builder
from .drawing import _bbox_intersection
from .pdf_layout import (
    _pdf_axis_segments, _pdf_cell_slot, _pdf_display_mapper, _pdf_map_bbox, _pdf_merged_cells, _pdf_packed_cells,
    _pdf_row_lines, _pdf_table_grids, _pdf_text_groups, _pdf_vertical_rules, _pdfium_close, _pdfium_open,
    _pdfium_page_text,
)
from .pdf_xobject import _pdf_xobject_specs


def _pdf_objects(builder: _Builder, path: Path) -> None:
    """PDFをEvidence IRへ。位置付き文字はpdfium、使えないページだけpypdfで取る。"""
    document = _pdfium_open(path)
    try:
        _pdf_page_objects(builder, path, document)
    finally:
        _pdfium_close(document)


def _pdf_info_title(reader: Any) -> str | None:
    """PDF の文書情報（Info 辞書）の Title の原値。無い・読めない・空なら None。"""
    try:
        title = (reader.metadata or {}).get("/Title")
    except Exception:
        return None
    return str(title) if title is not None and str(title).strip() else None


def _pdf_packed_rows(
    builder: _Builder,
    page_id: str,
    page_number: int,
    text_space: str,
    text_groups: list[dict[str, Any]],
    grid: tuple[Any, ...],
    grid_members: set[int],
    table_index: int,
) -> None:
    """列を分ける縦罫線が無い格子（枠と横罫線だけの表）。セルに押し込まず、行の塊の文字として残す。"""
    ys = grid[1]
    for row in range(1, len(ys)):
        band = [text_groups[i] for i in sorted(grid_members)
                if (slot := _pdf_cell_slot(text_groups[i], grid)) is not None and slot[0] == row]
        if not band:
            continue
        lines = _pdf_row_lines(band)
        band_bbox = [min(float(g["bbox"][0]) for g in band), min(float(g["bbox"][1]) for g in band),
                     max(float(g["bbox"][2]) for g in band), max(float(g["bbox"][3]) for g in band)]
        builder.add_element(
            "positioned_text",
            evidence_ir.Locator(
                part=f"page:{page_number}/contents",
                page=page_number,
                object_id=f"text-rows:{table_index}:{row}",
                bbox=band_bbox,
                extension={"coordinate_system": text_space},
            ),
            parent_id=page_id, order=2000 + len(text_groups) + table_index * 1000 + row,
            value="\n".join(" ".join(g["text"] for g in line) for line in lines),
            extension={"font_size": max(float(g["font_size"]) for g in band),
                       "position_confidence": 1.0, "row_lines": len(lines)},
        )


def _pdf_add_grid(
    builder: _Builder,
    page_id: str,
    page_number: int,
    text_space: str,
    text_groups: list[dict[str, Any]],
    grid: tuple[Any, ...],
    table_index: int,
    assigned: set[int],
) -> None:
    """罫線の格子 1 つを表（table と cell）にする。格子に入った文字群の番号は assigned へ足す。"""
    xs, ys = grid[0], grid[1]
    anchor_of, spans = _pdf_merged_cells(grid)
    table_groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    grid_members: set[int] = set()
    for group_index, group in enumerate(text_groups):
        if (slot := _pdf_cell_slot(group, grid)) is not None:
            table_groups.setdefault(anchor_of[slot], []).append(group)
            grid_members.add(group_index)
    if not table_groups:
        return
    if _pdf_packed_cells(table_groups):
        _pdf_packed_rows(builder, page_id, page_number, text_space, text_groups, grid, grid_members, table_index)
        assigned.update(grid_members)
        return
    assigned.update(grid_members)
    table_bbox = [xs[0], ys[-1], xs[-1], ys[0]]
    title_candidates = [
        group for index, group in enumerate(text_groups)
        if index not in assigned and float(group["bbox"][1]) >= ys[0]
    ]
    title = min(title_candidates, key=lambda item: float(item["bbox"][1]) - ys[0])["text"] \
        if title_candidates else f"PDF表{table_index}"
    table_locator = evidence_ir.Locator(
        part=f"page:{page_number}/contents",
        page=page_number,
        object_id=f"table:{table_index}",
        bbox=table_bbox,
        extension={"coordinate_system": text_space},
    )
    table_id = builder.add_element(
        "table", table_locator, parent_id=page_id, order=1000 + table_index, value=None,
        extension={"name": title, "column_boundaries": xs, "row_boundaries": ys,
                   "geometry_source": "vector_grid"},
    )
    table_coverage = builder.coverage_for(table_id)
    for (row, column), groups_in_cell in sorted(table_groups.items()):
        row_span, column_span = spans[(row, column)]
        value = " ".join(group["text"] for group in sorted(
            groups_in_cell, key=lambda item: (-item.get("baseline_y", item["bbox"][1]), item["bbox"][0])))
        cell_locator = evidence_ir.Locator(
            part=f"page:{page_number}/contents",
            page=page_number,
            object_id=f"table:{table_index}/cell:{row}:{column}",
            bbox=[xs[column - 1], ys[row + row_span - 1], xs[column + column_span - 1], ys[row - 1]],
            extension={"row": row, "column": column, "row_span": row_span, "column_span": column_span,
                       "coordinate_system": text_space},
        )
        builder.add_element(
            "cell", cell_locator, parent_id=table_id, order=row * 1000 + column, value=value,
            coverage_id=table_coverage,
        )


def _pdf_add_tables_and_text(
    builder: _Builder,
    page_id: str,
    page_number: int,
    text_space: str,
    text_groups: list[dict[str, Any]],
    segments: list[tuple[float, float, float, float]],
) -> None:
    """罫線の格子ごとの表と、どの表にも入らない文字群（positioned_text）を足す。"""
    grids = _pdf_table_grids(segments)
    assigned: set[int] = set()
    for table_index, grid in enumerate(grids, start=1):
        _pdf_add_grid(builder, page_id, page_number, text_space, text_groups, grid, table_index, assigned)
    for group_index, group in enumerate(text_groups):
        if group_index in assigned:
            continue
        locator = evidence_ir.Locator(
            part=f"page:{page_number}/contents",
            page=page_number,
            object_id=f"text:{group['group_index']}",
            bbox=group["bbox"],
            extension={"coordinate_system": text_space, "text_group_index": group["group_index"]},
        )
        builder.add_element(
            "positioned_text", locator, parent_id=page_id, order=2000 + group_index,
            value=group["text"], extension={"font_size": group["font_size"], "position_confidence": 1.0},
        )


def _pdf_add_images(
    builder: _Builder,
    page_id: str,
    page_number: int,
    image_specs: list[dict[str, Any]],
    text_id: str | None,
    text_indexes: list[int],
) -> None:
    """Image XObject を要素にし、文字層より後に描かれたものは painted_after、ページ全面を覆うものは covers / covered_by を張る。"""
    for image_order, spec in enumerate(image_specs, start=1):
        raw_name = spec["name"]
        image_bbox = spec["bbox"]
        image_id = builder.add_element(
            "image_xobject",
            evidence_ir.Locator(
                part=f"page:{page_number}/xobject:{raw_name}",
                page=page_number,
                object_id=raw_name,
                bbox=image_bbox,
                extension={
                    "coordinate_system": "pdf-user-space",
                    "xobject_path": spec["extension"]["xobject_path"],
                    "operator_path": spec["extension"]["operator_path"],
                },
            ),
            parent_id=page_id,
            order=image_order,
            value=None,
            coverage_status="metadata_only",
            coverage_reason="image_content_uninterpreted",
            extension=spec["extension"],
        )
        operator_index = spec["extension"]["operator_index"]
        if text_id is not None and text_indexes and max(text_indexes) < operator_index:
            builder.add_relation(
                "painted_after",
                text_id,
                image_id,
                evidence_ids=[text_id, image_id],
                extension={"operator_order": [max(text_indexes), operator_index]},
            )
            if spec["page_overlap_ratio"] >= 0.95:
                builder.add_relation(
                    "covers",
                    image_id,
                    text_id,
                    evidence_ids=[image_id, text_id],
                    extension={"page_overlap_ratio": spec["page_overlap_ratio"]},
                )
                builder.add_relation(
                    "covered_by",
                    text_id,
                    image_id,
                    evidence_ids=[text_id, image_id],
                    extension={"page_overlap_ratio": spec["page_overlap_ratio"], "use_for_current_answer": False},
                )


def _pdf_page_text(
    page: Any,
    page_number: int,
    pdfium_document: Any,
    operations: list[tuple[list[Any], bytes]],
    mapper: Any,
    text_bbox: list[float],
) -> tuple[list[int], list[tuple[float, float, float, float]], list[dict[str, Any]], str]:
    """(文字演算子の位置, 罫線の線分, 文字群, ページの文字)。文字は pdfium、取れなければ pypdf。"""
    text_indexes = [index for index, (_args, operator) in enumerate(operations)
                    if operator in {b"Tj", b"TJ", b"'", b'"'}]
    segments = _pdf_axis_segments(operations)
    if mapper is not None:
        segments = [(*mapper(seg[0], seg[1]), *mapper(seg[2], seg[3])) for seg in segments]
    text_groups: list[dict[str, Any]] = []
    page_text = ""
    if text_indexes:
        pdfium_text = (_pdfium_page_text(pdfium_document, page_number - 1, _pdf_vertical_rules(segments), mapper)
                       if pdfium_document is not None else None)
        if pdfium_text is not None:
            text_groups, page_text = pdfium_text
        else:
            text_groups = _pdf_text_groups(page, operations, text_bbox, positioned=mapper is None)
            page_text = page.extract_text() or ""
    return text_indexes, segments, text_groups, page_text


def _pdf_covering_specs(
    page_bbox: list[float],
    image_specs: list[dict[str, Any]],
    text_indexes: list[int],
) -> list[dict[str, Any]]:
    """各画像にページとの重なり率を付け、文字より後に描かれたページ全面（95%以上）の画像を返す。"""
    page_area = max(0.0, page_bbox[2] - page_bbox[0]) * max(0.0, page_bbox[3] - page_bbox[1])
    covering_specs = []
    for spec in image_specs:
        intersection = _bbox_intersection(page_bbox, spec.get("effective_bbox") or spec["bbox"])
        overlap_area = 0.0 if intersection is None else \
            (intersection[2] - intersection[0]) * (intersection[3] - intersection[1])
        ratio = overlap_area / page_area if page_area else 0.0
        spec["page_overlap_ratio"] = ratio
        if text_indexes and max(text_indexes) < spec["extension"]["operator_index"] and ratio >= 0.95:
            covering_specs.append(spec)
    return covering_specs


def _pdf_add_text_layer(
    builder: _Builder,
    page_id: str,
    page_number: int,
    text_bbox: list[float],
    text_space: str,
    text_indexes: list[int],
    page_text: str,
    covered: bool,
) -> str:
    text_locator = evidence_ir.Locator(
        part=f"page:{page_number}/contents",
        page=page_number,
        object_id="text-layer",
        bbox=text_bbox,
        extension={"operator_indexes": text_indexes, "coordinate_system": text_space},
    )
    return builder.add_element(
        "text_object",
        text_locator,
        parent_id=page_id,
        order=1,
        value=page_text,
        visibility="hidden" if covered else "visible",
        extension={
            "layer": "text",
            "visual_currentness": "unknown",
            "aggregate": True,
            **({"visibility_reason": "covered_by_full_page_image", "use_for_current_answer": False}
               if covered else {}),
        },
    )


def _pdf_add_page(
    builder: _Builder,
    reader: Any,
    pdfium_document: Any,
    page_number: int,
    page: Any,
    document_title: str | None,
) -> None:
    from pypdf.generic import ContentStream

    media = page.mediabox
    page_bbox = [float(media.left), float(media.bottom), float(media.right), float(media.top)]
    mapper = _pdf_display_mapper(page)       # /Rotate 付きページ: 文字・罫線・表は表示される向きの座標で扱う
    text_bbox = _pdf_map_bbox(mapper, page_bbox) if mapper is not None else page_bbox
    text_space = "pdf-display-space" if mapper is not None else "pdf-user-space"
    page_locator = evidence_ir.Locator(part=f"page:{page_number}", page=page_number, bbox=text_bbox,
                                       extension={"coordinate_system": text_space})
    page_id = builder.add_element(
        "page", page_locator, parent_id=None, order=page_number, value=None,
        extension={"document_info_title": document_title} if page_number == 1 and document_title else None)
    builder.add_coverage(
        "part",
        page_locator,
        "extracted",
        detected_kind="pdf_page",
        content_basis="structured",
        reason_code="content_extracted",
        parser_id=PDF_ADAPTER_VERSION,
    )
    try:
        content = ContentStream(page.get_contents(), reader)
        operations = content.operations
    except Exception as exc:
        failure_locator = evidence_ir.Locator(part=f"page:{page_number}/contents", page=page_number)
        builder.add_coverage(
            "region",
            failure_locator,
            "failed",
            detected_kind="pdf_content_stream",
            content_basis="structured",
            reason_code="content_stream_parse_failed",
            parser_id=PDF_ADAPTER_VERSION,
            detail={"error_type": exc.__class__.__name__},
        )
        return

    text_indexes, segments, text_groups, page_text = _pdf_page_text(
        page, page_number, pdfium_document, operations, mapper, text_bbox)
    image_specs = _pdf_xobject_specs(
        builder,
        reader=reader,
        page_number=page_number,
        page_bbox=page_bbox,
        operations=operations,
        resources=page.get("/Resources"),
    )
    covering_specs = _pdf_covering_specs(page_bbox, image_specs, text_indexes)
    text_id = None
    if text_indexes:
        text_id = _pdf_add_text_layer(
            builder, page_id, page_number, text_bbox, text_space, text_indexes, page_text, bool(covering_specs))
    if not covering_specs and text_groups and all(item.get("position_confidence") == 1.0 for item in text_groups):
        _pdf_add_tables_and_text(builder, page_id, page_number, text_space, text_groups, segments)
    _pdf_add_images(builder, page_id, page_number, image_specs, text_id, text_indexes)


def _pdf_page_objects(builder: _Builder, path: Path, pdfium_document: Any) -> None:
    from pypdf import PdfReader

    reader = PdfReader(path)
    document_title = _pdf_info_title(reader)
    for page_number, page in enumerate(reader.pages, start=1):
        _pdf_add_page(builder, reader, pdfium_document, page_number, page, document_title)
