"""PPTX の slide/master/layout の object inventory。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from .. import evidence_ir, observation_vocab
from ..ooxml import powerpoint
from .adapt import _apply_cover_state, _bbox_cover_ratio
from .base import _A, _C, _ContentHost, _P, _R
from .builder import _Builder
from .docx import _external_vml_host_context
from .drawing import (
    _add_chart_data, _add_raster_reference_coverage, _add_smartart_data, _bbox_intersection, _c_nv_pr,
    _chart_payload, _connection, _drawing_asset, _integer, _ole_metadata, _prst_geom, _smartart_payload,
)
from .package import _relationships, _xml
from .vml import _extract_vml_shapes
from .xlsx import _add_connector_relation


def _ppt_props(node: ET.Element, kind: str) -> ET.Element | None:
    return _c_nv_pr(node, _P, kind)


def _ppt_kind(node: ET.Element) -> str | None:
    local = node.tag.split("}")[-1]
    return {"sp": "shape", "pic": "picture", "grpSp": "group", "cxnSp": "connector",
            "graphicFrame": "graphic_frame"}.get(local)


def _raw_xfrm(node: ET.Element, kind: str) -> tuple[float, float, float, float] | None:
    if kind == "group":
        xfrm = node.find(f"{_P}grpSpPr/{_A}xfrm")
    elif kind in {"graphic_frame", "table"}:
        xfrm = node.find(f"{_P}xfrm")
    else:
        props_name = "spPr"
        xfrm = node.find(f"{_P}{props_name}/{_A}xfrm")
    if xfrm is None:
        return None
    off, ext = xfrm.find(f"{_A}off"), xfrm.find(f"{_A}ext")
    try:
        return float(off.get("x")), float(off.get("y")), float(ext.get("cx")), float(ext.get("cy"))
    except (AttributeError, TypeError, ValueError):
        return None


def _transform_bbox(
    raw: tuple[float, float, float, float] | None,
    transform: tuple[float, float, float, float],
) -> list[int | float] | None:
    if raw is None:
        return None
    sx, sy, tx, ty = transform
    x, y, cx, cy = raw
    values = [sx * x + tx, sy * y + ty, sx * (x + cx) + tx, sy * (y + cy) + ty]
    return [int(value) if value.is_integer() else value for value in values]


def _child_transform(
    node: ET.Element,
    parent: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    xfrm = node.find(f"{_P}grpSpPr/{_A}xfrm")
    if xfrm is None:
        return parent
    off, ext, child_off, child_ext = (
        xfrm.find(f"{_A}off"),
        xfrm.find(f"{_A}ext"),
        xfrm.find(f"{_A}chOff"),
        xfrm.find(f"{_A}chExt"),
    )
    try:
        sx0, sy0, tx0, ty0 = parent
        scale_x = float(ext.get("cx")) / float(child_ext.get("cx"))
        scale_y = float(ext.get("cy")) / float(child_ext.get("cy"))
        offset_x = float(off.get("x")) - float(child_off.get("x")) * scale_x
        offset_y = float(off.get("y")) - float(child_off.get("y")) * scale_y
        return sx0 * scale_x, sy0 * scale_y, sx0 * offset_x + tx0, sy0 * offset_y + ty0
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return parent


def _ppt_text(node: ET.Element) -> str:
    """DrawingMLの段落境界を保って文字列化する。run境界は連結し、段落間だけLFにする。取り消し線のrunは`powerpoint.stated_text`が括る。"""
    paragraphs = [powerpoint.stated_text(paragraph) for paragraph in node.findall(f".//{_A}p")]
    return "\n".join(text for text in paragraphs if text)


def _ppt_alpha(node: ET.Element) -> int | None:
    return next(
        (_integer(alpha.get("val")) for alpha in node.findall(f".//{_A}alpha")
         if _integer(alpha.get("val")) is not None),
        None,
    )


def _ppt_table_cells(
    builder: _Builder,
    table_node: ET.Element,
    *,
    table_id: str,
    table_locator: evidence_ir.Locator,
    visibility: str,
    coverage_id: str,
) -> None:
    """PPTXの``a:tbl``をrow/column/span付きcellへ展開する。

    ``hMerge``/``vMerge`` continuationは値を持つ独立cellにせず、先頭cellのspanで表す。壊れた表で
    continuationに文字が入っている場合だけ、欠落防止のため通常cellとして残す。
    """
    table = table_node.find(f".//{_A}tbl")
    if table is None:
        return
    order = 0
    for row_index, row in enumerate(table.findall(f"{_A}tr"), start=1):
        column = 1
        for cell in row.findall(f"{_A}tc"):
            text = _ppt_text(cell)
            horizontal_merge = cell.get("hMerge") in {"1", "true"}
            vertical_merge = cell.get("vMerge") in {"1", "true"}
            try:
                column_span = max(1, int(cell.get("gridSpan", "1")))
            except ValueError:
                column_span = 1
            try:
                row_span = max(1, int(cell.get("rowSpan", "1")))
            except ValueError:
                row_span = 1
            if (horizontal_merge or vertical_merge) and not text:
                # span起点が既に消費した論理列なので、continuation自体ではcolumnを進めない。
                continue
            order += 1
            cell_locator = evidence_ir.Locator(
                part=table_locator.part,
                slide=table_locator.slide,
                object_id=f"{table_locator.object_id}/cell:{row_index}:{column}",
                extension={
                    "row": row_index,
                    "column": column,
                    "row_span": row_span,
                    "column_span": column_span,
                    "coordinate_system": "pptx-table-grid",
                },
            )
            builder.add_element(
                "cell",
                cell_locator,
                parent_id=table_id,
                order=order,
                value=text,
                coverage_id=coverage_id,
                visibility=visibility,
                extension={"role": "unknown"},
            )
            column += column_span


def _ppt_template_elements(builder: _Builder, entries: dict[str, bytes]) -> set[str]:
    """slide/master/layout/notes masterを検査済みpartにし、実在するtemplate文字も保持する。"""
    prefixes = ("ppt/slideLayouts/", "ppt/slideMasters/", "ppt/notesMasters/")
    content_parts: set[str] = set()
    order = 0
    for part in sorted(
        candidate for candidate in entries
        if candidate.startswith(prefixes) and candidate.endswith(".xml")
    ):
        root = _xml(entries, part)
        if root is None:
            continue
        content_parts.add(part)
        texts = list(dict.fromkeys(
            node.text for node in root.iter(f"{_A}t") if node.text and node.text.strip()
        ))
        for text_index, text in enumerate(texts, start=1):
            order += 1
            builder.add_element(
                "presentation_template_text",
                evidence_ir.Locator(part=part, object_id=f"text:{text_index}"),
                parent_id=None,
                order=order,
                value=text,
                extension={"template_part": part},
            )
    return content_parts


def _ppt_auxiliary_hosts(
    entries: dict[str, bytes],
    slide_parts: list[str],
    hidden_slides: set[int],
) -> list[_ContentHost]:
    """master/layout/notes系partを、対応slideと用途を失わずcontent host化する。"""
    notes_to_slide: dict[str, int] = {}
    for slide_number, slide_part in enumerate(slide_parts, start=1):
        for target in _relationships(entries, slide_part).values():
            if target.startswith("ppt/notesSlides/notesSlide") and target.endswith(".xml"):
                notes_to_slide.setdefault(target, slide_number)

    hosts: list[_ContentHost] = []
    prefixes = (
        ("ppt/slideMasters/slideMaster", "slide_master", "template_inherited", "template_definition"),
        ("ppt/slideLayouts/slideLayout", "slide_layout", "template_inherited", "template_definition"),
        ("ppt/notesMasters/notesMaster", "notes_master", "notes_template", "template_definition"),
        ("ppt/notesSlides/notesSlide", "notes_slide", "notes_only", "active"),
    )
    for part in sorted(entries):
        if not part.endswith(".xml"):
            continue
        descriptor = next((item for item in prefixes if part.startswith(item[0])), None)
        if descriptor is None:
            continue
        _prefix, host_kind, host_visibility, host_lifecycle = descriptor
        slide = notes_to_slide.get(part) if host_kind == "notes_slide" else None
        visibility = "hidden" if slide in hidden_slides else "visible"
        hosts.append(_ContentHost(
            part,
            host_kind,
            host_visibility,
            host_lifecycle=host_lifecycle,
            slide=slide,
            visibility=visibility,
        ))
    return hosts


def _add_auxiliary_raster(
    builder: _Builder,
    entries: dict[str, bytes],
    host: _ContentHost,
    host_metadata: dict[str, Any],
    node: ET.Element,
    raw_kind: str,
    has_blip: bool,
    *,
    sequence: int,
    local_order: int,
    paint_order: int,
    transform: tuple[float, float, float, float],
    order_base: int,
) -> None:
    """非slide hostの画像1件をElementと参照のcoverageとして足す。"""
    props = _ppt_props(node, raw_kind)
    object_id = _integer(props.get("id")) if props is not None else None
    asset_role = "picture_content" if raw_kind == "picture" else "shape_fill"
    asset_extension = _drawing_asset(entries, host.part, node, role=asset_role)
    if has_blip and not asset_extension:
        # unresolved shape fillもrouterでfailed_bindingへ搬送できるよう役割だけは保持する。
        asset_extension = {"asset_role": asset_role}
    extension: dict[str, Any] = {
        "name": props.get("name", "") if props is not None else "",
        "description": props.get("descr", "") if props is not None else "",
        "z_order": local_order,
        "paint_order": paint_order,
        **host_metadata,
        **asset_extension,
    }
    if (alpha := _ppt_alpha(node)) is not None:
        extension["alpha"] = alpha
    locator = evidence_ir.Locator(
        part=host.part,
        slide=host.slide,
        object_id=object_id,
        bbox=_transform_bbox(_raw_xfrm(node, raw_kind), transform),
        extension={
            "coordinate_system": "pptx-slide-emu",
            "host_object_index": sequence,
            **host_metadata,
        },
    )
    builder.add_element(
        raw_kind,
        locator,
        parent_id=None,
        order=order_base + sequence,
        # template/notes文字は既存template/Document IR要素が保持する。画像要素へ重複転記しない。
        value=None,
        coverage_status="metadata_only" if raw_kind == "picture" else "extracted",
        coverage_reason="image_content_uninterpreted" if raw_kind == "picture" else None,
        visibility=host.visibility,
        lifecycle=host.lifecycle,
        extension=extension,
    )
    _add_raster_reference_coverage(
        builder,
        locator,
        extension,
        failed_kind="drawing_image_asset",
        external_kind="drawing_external_image_reference",
    )


def _ppt_auxiliary_rasters(
    builder: _Builder,
    entries: dict[str, bytes],
    host: _ContentHost,
    *,
    order_base: int,
) -> set[str]:
    """非slide hostのpicture/画像fillをhost固有relsでEvidenceへ運ぶ。"""
    root = _xml(entries, host.part)
    if root is None:
        return {host.part}
    sp_tree = root.find(f"{_P}cSld/{_P}spTree")
    if sp_tree is None:
        return {host.part}
    host_metadata = host.metadata()
    sequence = 0

    def walk(
        nodes: list[ET.Element],
        transform=(1.0, 1.0, 0.0, 0.0),
        paint_base: int = 0,
    ) -> None:
        nonlocal sequence
        local_order = 0
        for node in nodes:
            raw_kind = _ppt_kind(node)
            if raw_kind is None:
                continue
            local_order += 1
            paint_order = local_order * 1_000_000 if paint_base == 0 else paint_base + local_order
            if raw_kind == "group":
                child_nodes = [child for child in list(node) if _ppt_kind(child) is not None]
                walk(child_nodes, _child_transform(node, transform), paint_order)
                continue
            if raw_kind not in {"picture", "shape"}:
                continue
            has_blip = node.find(f".//{_A}blip") is not None
            if raw_kind == "shape" and not has_blip:
                continue
            sequence += 1
            _add_auxiliary_raster(
                builder, entries, host, host_metadata, node, raw_kind, has_blip,
                sequence=sequence, local_order=local_order, paint_order=paint_order,
                transform=transform, order_base=order_base,
            )

    walk(list(sp_tree))
    _extract_vml_shapes(
        builder,
        entries,
        root,
        part=host.part,
        slide=host.slide,
        order_base=order_base + 100_000,
        visibility=host.visibility,
        lifecycle=host.lifecycle,
        host_metadata=host_metadata,
    )
    return {host.part}


def _pptx_add_covers(
    builder: _Builder,
    top_level_ids: dict[int, str],
    id_counts: Counter,
    twin_by_native: dict[str, evidence_ir.EvidenceElement],
) -> None:
    """DocumentIR の前面の候補（fronts・covered_by_text）から前面 → 背面の covers を作り、背面の状態を導く。

    前面は原本の図形 ID（cNvPr/@id）で原本の図形に解決する。塗りの区分は DocumentIR 側の判定のまま（ここでは変えない）。
    背面が hidden になるのは occlusion=confirmed の covers があるときだけ（`_apply_cover_state`）。
    """
    native_by_twin = {twin.element_id: native_id for native_id, twin in twin_by_native.items()}

    def front_of(element_id: str) -> evidence_ir.EvidenceElement:
        return twin_by_native.get(element_id) or builder.element(element_id)

    for back_id, twin in twin_by_native.items():
        source_map = twin.locator.extension.get("document_ir_source_map", {})
        if not isinstance(source_map, dict):
            source_map = {}
        fronts: list[tuple[str, str, bool]] = []  # (前面の element_id, 塗りの区分, 形が外接矩形と同一視できるか)
        for candidate in source_map.get("fronts") or []:
            shape_id = candidate.get("shape_id") if isinstance(candidate, dict) else None
            front_id = top_level_ids.get(shape_id) if isinstance(shape_id, int) and id_counts[shape_id] == 1 else None
            if front_id is not None and isinstance(candidate.get("fill"), str):
                fronts.append((front_id, candidate["fill"], candidate.get("covers_bbox", True) is not False))
        covered_by_text = twin.extension.get("covered_by_text")
        if isinstance(covered_by_text, dict):
            front_id = native_by_twin.get(covered_by_text.get("element_id"))
            if front_id is not None and front_id not in {existing for existing, _, _ in fronts}:
                fronts.append((front_id, observation_vocab.FILL_NONE_WITH_TEXT, True))
        for front_id, fill, covers_bbox in fronts:
            ratio = _bbox_cover_ratio(builder.element(back_id).locator.bbox, builder.element(front_id).locator.bbox)
            builder.add_covers(
                front_id, back_id, fill=fill, cover_ratio=ratio, coordinate_system="pptx-slide-emu", covers_bbox=covers_bbox)
        if fronts:
            _apply_cover_state(builder, back_id, file_type="pptx", front_of=front_of)


@dataclass
class _PptxSlide:
    """1 枚の slide を走査する間の状態（足した図形の位置・原本の図形 ID への対応・コネクタの解決待ち）。"""

    builder: _Builder
    entries: dict[str, bytes]
    part: str
    slide_number: int
    slide_element_id: str | None
    slide_rels: dict[str, str]
    content_parts: set[str]
    hidden_slides: set[int]
    adapted_by_shape: dict[tuple[int, int], evidence_ir.EvidenceElement]
    top_level_id_counts: Counter
    pending_connectors: list[tuple[str, int | None, int | None]] = field(default_factory=list)
    ids_by_object: dict[int, str] = field(default_factory=dict)
    positioned: list[tuple[str, list[int | float], int]] = field(default_factory=list)
    top_level_ids: dict[int, str] = field(default_factory=dict)
    twin_by_native: dict[str, evidence_ir.EvidenceElement] = field(default_factory=dict)


@dataclass
class _PptxNode:
    """図形 1 つが何として読めるか（表・chart・smartart・OLE への読み替え結果）。"""

    frame_kind: str
    kind: str
    chart_part: str | None = None
    chart_relationship_id: str = ""
    smartart_value: str | None = None
    smartart_extension: dict[str, Any] = field(default_factory=dict)
    smartart_parts: set[str] = field(default_factory=set)
    ole_node: ET.Element | None = None
    ole_relationship_id: str = ""


def _pptx_resolve_node(ctx: _PptxSlide, node: ET.Element, raw_kind: str) -> _PptxNode:
    frame_kind = "table" if raw_kind == "graphic_frame" and node.find(f".//{_A}tbl") is not None else raw_kind
    resolved = _PptxNode(frame_kind=frame_kind, kind=frame_kind)
    if raw_kind == "graphic_frame" and frame_kind != "table":
        chart = node.find(f".//{_C}chart")
        resolved.chart_relationship_id = chart.get(f"{_R}id", "") if chart is not None else ""
        resolved.chart_part = ctx.slide_rels.get(resolved.chart_relationship_id)
        if resolved.chart_part in ctx.entries:
            resolved.kind = "chart"
        else:
            resolved.smartart_value, resolved.smartart_extension, resolved.smartart_parts = _smartart_payload(
                ctx.entries, ctx.part, node)
            if resolved.smartart_extension.get("relationship_ids"):
                resolved.kind = "smartart"
            else:
                resolved.ole_node = node.find(f".//{_P}oleObj")
                resolved.ole_relationship_id = (
                    resolved.ole_node.get(f"{_R}id", "") if resolved.ole_node is not None else "")
                if resolved.ole_relationship_id and ctx.slide_rels.get(resolved.ole_relationship_id) in ctx.entries:
                    resolved.kind = "ole_object"
    return resolved


def _pptx_node_extension(
    ctx: _PptxSlide,
    node: ET.Element,
    resolved: _PptxNode,
    props: ET.Element | None,
    *,
    local_order: int,
    paint_order: int,
) -> dict[str, Any]:
    """図形 1 つの extension。chart / smartart の part は content_parts へ足す。"""
    kind = resolved.kind
    entries = ctx.entries
    extension: dict[str, Any] = {
        "name": props.get("name", "") if props is not None else "",
        "description": props.get("descr", "") if props is not None else "",
        "z_order": local_order,
        "paint_order": paint_order,
    }
    if (prst := _prst_geom(node, _P, kind)) is not None:
        extension["prst"] = prst
    if (alpha := _ppt_alpha(node)) is not None:
        extension["alpha"] = alpha
    asset_role = (
        "picture_content" if kind == "picture" else
        "ole_preview" if kind == "ole_object" else
        "shape_fill"
    )
    extension.update(_drawing_asset(entries, ctx.part, node, role=asset_role))
    if resolved.chart_part in entries:
        ctx.content_parts.add(resolved.chart_part)
        _, chart_extension = _chart_payload(entries, resolved.chart_part)
        extension.update({
            "relationship_id": resolved.chart_relationship_id,
            "chart_part": resolved.chart_part,
            **chart_extension,
        })
    if kind == "smartart":
        ctx.content_parts.update(resolved.smartart_parts)
        extension.update(resolved.smartart_extension)
    if kind == "ole_object" and resolved.ole_node is not None:
        extension.update(_ole_metadata(entries, ctx.part, resolved.ole_relationship_id, resolved.ole_node))
    if kind == "connector":
        extension.update({
            "start_object_id": _connection(node.find(f".//{_A}stCxn")),
            "end_object_id": _connection(node.find(f".//{_A}endCxn")),
        })
    return extension


def _pptx_add_node_data(
    ctx: _PptxSlide,
    node: ET.Element,
    resolved: _PptxNode,
    element_id: str,
    locator: evidence_ir.Locator,
    visibility: str,
    object_token: str,
) -> None:
    """表の cell・chart・smartart の中身を子要素として足す。"""
    builder, entries = ctx.builder, ctx.entries
    if resolved.kind == "table":
        _ppt_table_cells(
            builder,
            node,
            table_id=element_id,
            table_locator=locator,
            visibility=visibility,
            coverage_id=builder.coverage_for(element_id),
        )
    if resolved.chart_part in entries:
        _add_chart_data(
            builder,
            entries,
            chart_part=resolved.chart_part,
            parent_id=element_id,
            object_token=object_token,
            slide=ctx.slide_number,
        )
    if resolved.kind == "smartart":
        _add_smartart_data(
            builder,
            entries,
            parent_id=element_id,
            value=resolved.smartart_value,
            extension=resolved.smartart_extension,
            host_part=ctx.part,
            object_token=object_token,
            slide=ctx.slide_number,
        )


def _pptx_walk(
    ctx: _PptxSlide,
    nodes: list[ET.Element],
    parent_id: str | None,
    transform=(1.0, 1.0, 0.0, 0.0),
    paint_base: int = 0,
) -> None:
    builder = ctx.builder
    local_order = 0
    for node in nodes:
        raw_kind = _ppt_kind(node)
        if raw_kind is None:
            continue
        local_order += 1
        resolved = _pptx_resolve_node(ctx, node, raw_kind)
        kind = resolved.kind
        props = (
            node.find(f"{_P}nvGraphicFramePr/{_P}cNvPr")
            if raw_kind == "graphic_frame"
            else _ppt_props(node, raw_kind)
        )
        object_id = _integer(props.get("id")) if props is not None else None
        bbox = _transform_bbox(_raw_xfrm(node, resolved.frame_kind), transform)
        text = _ppt_text(node)
        # top-levelのzを大きい桁、group子をその内側の小さい桁に置き、次のtop-levelより前に保つ。
        paint_order = local_order * 1_000_000 if paint_base == 0 else paint_base + local_order
        extension = _pptx_node_extension(
            ctx, node, resolved, props, local_order=local_order, paint_order=paint_order)
        adapted = (
            ctx.adapted_by_shape.get((ctx.slide_number, object_id))
            if parent_id is None and object_id is not None and ctx.top_level_id_counts[object_id] == 1 else None
        )
        # 遮蔽・前面文字による重なり（理由 occluded・covered_by_text）は、ここでは写さず covers から導く。
        adapted_reason = adapted.extension.get("visibility_reason") if adapted is not None else None
        derived_from_covers = adapted_reason in observation_vocab.COVER_DERIVED_REASONS
        hidden = ctx.slide_number in ctx.hidden_slides
        visibility = "hidden" if hidden else (
            adapted.visibility if adapted is not None and not derived_from_covers else "visible"
        )
        reason = (
            "hidden_slide_inherited" if hidden else
            None if derived_from_covers else adapted_reason
        )
        if reason:
            extension["visibility_reason"] = reason
        locator = evidence_ir.Locator(
            part=ctx.part,
            slide=ctx.slide_number,
            object_id=object_id,
            bbox=bbox,
            extension={"coordinate_system": "pptx-slide-emu"},
        )
        element_id = builder.add_element(
            kind,
            locator,
            parent_id=parent_id or ctx.slide_element_id,
            order=local_order,
            value=None if kind in {"table", "ole_object"} else (text or None),
            coverage_status="metadata_only" if kind in {"picture", "ole_object"} else "extracted",
            coverage_reason=(
                "image_content_uninterpreted" if kind == "picture" else
                "ole_binary_metadata_only" if kind == "ole_object" else None
            ),
            coverage_content_basis="binary_only" if kind == "ole_object" else None,
            visibility=visibility,
            extension=extension,
        )
        _add_raster_reference_coverage(
            builder,
            locator,
            extension,
            failed_kind="drawing_image_asset",
            external_kind="drawing_external_image_reference",
        )
        if object_id is not None:
            ctx.ids_by_object[object_id] = element_id
            if parent_id is None:
                ctx.top_level_ids[object_id] = element_id
        if adapted is not None:
            ctx.twin_by_native[element_id] = adapted
        if bbox is not None:
            ctx.positioned.append((element_id, bbox, paint_order))
        _pptx_add_node_data(
            ctx, node, resolved, element_id, locator, visibility,
            str(object_id if object_id is not None else local_order))
        if kind == "connector":
            ctx.pending_connectors.append(
                (element_id, extension.get("start_object_id"), extension.get("end_object_id")))
        if kind == "group":
            child_nodes = [child for child in list(node) if _ppt_kind(child) is not None]
            _pptx_walk(ctx, child_nodes, element_id, _child_transform(node, transform), paint_order)


def _pptx_overlaps(builder: _Builder, positioned: list[tuple[str, list[int | float], int]]) -> None:
    elements_by_id = {element.element_id: element for element in builder.ir.elements}
    for index, (left_id, left_bbox, left_z) in enumerate(positioned):
        for right_id, right_bbox, right_z in positioned[index + 1:]:
            left_element, right_element = elements_by_id.get(left_id), elements_by_id.get(right_id)
            if left_element is None or right_element is None:
                continue
            # groupは子の包含bboxであり、業務上のoverlayではない。子との重複relationを水増ししない。
            if left_element.type == "group" or right_element.type == "group":
                continue
            intersection = _bbox_intersection(left_bbox, right_bbox)
            if intersection is None:
                continue
            source_id, target_id = (left_id, right_id) if left_z >= right_z else (right_id, left_id)
            builder.add_relation(
                "overlaps",
                source_id,
                target_id,
                evidence_ids=[source_id, target_id],
                extension=observation_vocab.relation_extension(
                    observation_vocab.BASIS_GEOMETRY, observation_vocab.DIRECTION_UNDIRECTED,
                    intersection_bbox=intersection, coordinate_system="pptx-slide-emu"),
            )


def _pptx_slide_objects(
    builder: _Builder,
    entries: dict[str, bytes],
    part: str,
    slide_number: int,
    *,
    slide_element_id: str | None,
    content_parts: set[str],
    hidden_slides: set[int],
    adapted_by_shape: dict[tuple[int, int], evidence_ir.EvidenceElement],
) -> None:
    root = _xml(entries, part)
    if root is None:
        return
    sp_tree = root.find(f"{_P}cSld/{_P}spTree")
    if sp_tree is None:
        return
    top_level_id_counts = Counter(
        _integer(props.get("id")) if props is not None else None
        for node in sp_tree
        for raw_kind in [_ppt_kind(node)] if raw_kind is not None
        for props in [_ppt_props(node, raw_kind)]
    )
    ctx = _PptxSlide(
        builder, entries, part, slide_number, slide_element_id, _relationships(entries, part),
        content_parts, hidden_slides, adapted_by_shape, top_level_id_counts)
    _pptx_walk(ctx, list(sp_tree), None)
    _pptx_add_covers(builder, ctx.top_level_ids, top_level_id_counts, ctx.twin_by_native)
    for connector_id, start_object_id, end_object_id in ctx.pending_connectors:
        _add_connector_relation(builder, connector_id, start_object_id, end_object_id, ctx.ids_by_object)
    _pptx_overlaps(builder, ctx.positioned)


def _pptx_slide_parts(entries: dict[str, bytes]) -> list[str]:
    """presentation.xmlのsldIdLstとrelationshipを辿り、ファイル名順ではなく表示順を使う。"""
    presentation = _xml(entries, "ppt/presentation.xml")
    rels = _relationships(entries, "ppt/presentation.xml")
    slide_parts: list[str] = []
    if presentation is not None:
        for slide_id in presentation.findall(f"{_P}sldIdLst/{_P}sldId"):
            part = rels.get(slide_id.get(f"{_R}id", ""))
            if part:
                slide_parts.append(part)
    if not slide_parts:
        slide_parts = sorted(part for part in entries if part.startswith("ppt/slides/slide") and part.endswith(".xml"))
    return slide_parts


def _pptx_adapted_by_shape(builder: _Builder) -> dict[tuple[int, int], evidence_ir.EvidenceElement]:
    """DocumentIR 由来の shape を、スライドと原本の図形 ID（cNvPr/@id）で原本の図形に対応づける。同じ ID が複数あれば対応づけない。"""
    adapted_candidates: dict[tuple[int, int], list[evidence_ir.EvidenceElement]] = {}
    for element in builder.ir.elements:
        source_map = element.locator.extension.get("document_ir_source_map", {})
        slide = element.locator.slide
        shape_id = source_map.get("shape_id") if isinstance(source_map, dict) else None
        if (element.extension.get("origin") == "document-ir-v2-adapter"
                and element.type == "shape" and isinstance(slide, int) and isinstance(shape_id, int)):
            adapted_candidates.setdefault((slide, shape_id), []).append(element)
    return {key: items[0] for key, items in adapted_candidates.items() if len(items) == 1}


def _pptx_vml_objects(
    builder: _Builder,
    entries: dict[str, bytes],
    drawing_hosts: list[_ContentHost],
    content_parts: set[str],
) -> None:
    for vml_index, vml_part in enumerate(sorted(
        candidate for candidate in entries
        if candidate.startswith("ppt/") and (candidate.lower().endswith(".vml") or "vmldrawing" in candidate.lower())
    ), start=1):
        vml_root = _xml(entries, vml_part)
        if vml_root is None:
            continue
        content_parts.add(vml_part)
        metadata, visibility, lifecycle, slide = _external_vml_host_context(entries, drawing_hosts, vml_part)
        _extract_vml_shapes(
            builder,
            entries,
            vml_root,
            part=vml_part,
            slide=slide,
            order_base=300_000 + (vml_index - 1) * 10_000,
            visibility=visibility,
            lifecycle=lifecycle,
            host_metadata=metadata,
        )


def _pptx_objects(builder: _Builder, entries: dict[str, bytes]) -> set[str]:
    content_parts = {"ppt/presentation.xml"}
    content_parts.update(_ppt_template_elements(builder, entries))
    content_parts.update(
        part for part in entries if part.startswith("ppt/notesSlides/notesSlide") and part.endswith(".xml")
    )
    slide_parts = _pptx_slide_parts(entries)
    slide_ids = {
        element.locator.slide: element.element_id
        for element in builder.ir.elements
        if element.type == "slide" and element.locator.slide is not None
    }
    hidden_slides = {
        element.locator.slide
        for element in builder.ir.elements
        if element.type == "slide" and element.visibility == "hidden" and element.locator.slide is not None
    }
    auxiliary_hosts = _ppt_auxiliary_hosts(entries, slide_parts, hidden_slides)
    drawing_hosts = [
        _ContentHost(
            part,
            "slide",
            "slide_canvas",
            slide=slide_number,
            visibility="hidden" if slide_number in hidden_slides else "visible",
        )
        for slide_number, part in enumerate(slide_parts, start=1)
    ] + auxiliary_hosts
    adapted_by_shape = _pptx_adapted_by_shape(builder)
    for slide_number, part in enumerate(slide_parts, start=1):
        content_parts.add(part)
        _pptx_slide_objects(
            builder, entries, part, slide_number, slide_element_id=slide_ids.get(slide_number),
            content_parts=content_parts, hidden_slides=hidden_slides, adapted_by_shape=adapted_by_shape)
    _pptx_vml_objects(builder, entries, drawing_hosts, content_parts)
    for host_index, host in enumerate(auxiliary_hosts, start=1):
        content_parts.update(_ppt_auxiliary_rasters(
            builder,
            entries,
            host,
            order_base=10_000_000 + host_index * 1_000_000,
        ))
    return content_parts
