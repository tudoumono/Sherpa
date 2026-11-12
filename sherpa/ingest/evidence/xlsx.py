"""XLSX の drawing/object inventory。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from .. import evidence_ir, observation_vocab
from ..ooxml.text import paragraph_separated_text
from .adapt import _apply_cover_state
from .base import _A, _C, _R, _X, _XDR
from .builder import _Builder
from .drawing import (
    _add_chart_data, _add_raster_reference_coverage, _add_smartart_data, _area, _bbox_intersection, _c_nv_pr,
    _cell_rect, _connection, _drawing_asset, _drawing_covers_bbox, _drawing_front_fill, _drawing_kind, _integer,
    _occlusion_ratio, _ole_metadata, _prst_geom, _smartart_payload, _xlsx_anchor, _xlsx_chart_payload,
    _xlsx_embedded_anchor, _xlsx_sheet_parts,
)
from .package import _relationships, _xml
from .vml import _extract_vml_shapes


_GEOMETRY_WIDE_ROWS = 4096  # これより多くの行にまたがるセル範囲は行ごとの索引に入れず、いつも候補にする


class _SheetGeometry:
    """1 シートの cell・table 要素の矩形と、行ごとの索引（図形との幾何交差の候補を絞る）。

    要素の並び（`builder.ir.elements` の順）をそのまま保ち、候補も元の並びの順で返す。
    守ること: 作った後に同じシートの cell・table 要素が足されたら作り直す（`_xlsx_sheet_geometry`）。
    """

    def __init__(self, builder: _Builder, sheet_name: str) -> None:
        self.built_len = len(builder.ir.elements)
        self.cells: list[tuple[str, list[float]]] = []     # (element_id, rect)
        self.tables_by_rect: dict[tuple[float, ...], list[tuple[str, list[float]]]] = {}
        self._rows: dict[int, list[int]] = {}
        self._wide: list[int] = []
        for element in builder.ir.elements:
            if element.locator.sheet != sheet_name or element.type not in ("cell", "table"):
                continue
            rect = _cell_rect(element.locator.cell_range)
            if rect is None:
                continue
            if element.type == "table":
                self.tables_by_rect.setdefault(tuple(rect), []).append((element.element_id, rect))
                continue
            pos = len(self.cells)
            self.cells.append((element.element_id, rect))
            lo, hi = int(rect[1]), int(rect[3])
            if hi - lo > _GEOMETRY_WIDE_ROWS:
                self._wide.append(pos)
            else:
                for row in range(lo, hi):
                    self._rows.setdefault(row, []).append(pos)

    def cells_near(self, rect: list[float]) -> list[tuple[str, list[float]]]:
        """`rect` と行が重なりうる cell を、元の並びの順で返す（交差の判定は呼び出し側）。"""
        lo, hi = int(rect[1]), int(rect[3])
        if hi - lo > _GEOMETRY_WIDE_ROWS:
            return self.cells
        hits = set(self._wide)
        for row in range(lo, hi):
            hits.update(self._rows.get(row, ()))
        return [self.cells[pos] for pos in sorted(hits)]


def _xlsx_sheet_geometry(builder: _Builder, cache: dict[str, _SheetGeometry], sheet_name: str) -> _SheetGeometry:
    """`sheet_name` の `_SheetGeometry` を返す。前回作った後に同じシートの cell・table 要素が足されていれば作り直す。"""
    geometry = cache.get(sheet_name)
    if geometry is not None:
        added = builder.ir.elements[geometry.built_len:]
        if not any(e.locator.sheet == sheet_name and e.type in ("cell", "table") for e in added):
            geometry.built_len = len(builder.ir.elements)
            return geometry
    geometry = _SheetGeometry(builder, sheet_name)
    cache[sheet_name] = geometry
    return geometry


def _add_connector_relation(
    builder: _Builder,
    connector_id: str,
    start_object_id: int | None,
    end_object_id: int | None,
    ids_by_object: dict[int, str],
) -> None:
    """コネクタの始点・終点の指定から `connects_to` を 1 本作る（xlsx・pptx 共通）。

    両端が原本の図形に解決できれば 始点 → 終点。片端だけなら、解決できた図形とコネクタを結び
    （`ends=source_only` は 始点 → コネクタ、`target_only` は コネクタ → 終点）、解決できなかった側の原本の図形 ID を
    読めれば `unresolved_object_id` に残す。両端とも解決できなければ関係は作らない。
    """
    start_id, end_id = ids_by_object.get(start_object_id), ids_by_object.get(end_object_id)
    if start_id and end_id:
        source_id, target_id, ends, extra = start_id, end_id, observation_vocab.ENDS_BOTH, {}
        evidence_ids = [start_id, connector_id, end_id]
    elif start_id or end_id:
        if start_id:
            source_id, target_id, ends = start_id, connector_id, observation_vocab.ENDS_SOURCE_ONLY
            unresolved_object_id = end_object_id
        else:
            source_id, target_id, ends = connector_id, end_id, observation_vocab.ENDS_TARGET_ONLY
            unresolved_object_id = start_object_id
        extra = {"unresolved_object_id": unresolved_object_id} if unresolved_object_id is not None else {}
        evidence_ids = [source_id, target_id]
    else:
        return
    builder.add_relation(
        "connects_to",
        source_id,
        target_id,
        evidence_ids=evidence_ids,
        extension=observation_vocab.relation_extension(
            observation_vocab.BASIS_OOXML_CONNECTOR,
            observation_vocab.DIRECTION_ENDPOINT_ORDER_ONLY,
            ends,
            connector_element_id=connector_id, directed=True, **extra),
    )


@dataclass
class _XlsxDrawing:
    """1 つの drawing part の走査状態。anchor ごとに z_order・anchor_ext・cell_range・source_rect を差し替えて使う。"""

    builder: _Builder
    entries: dict[str, bytes]
    geometry_cache: dict[str, _SheetGeometry]
    sheet_name: str
    drawing_part: str
    drawing_rels: dict[str, str]
    content_parts: set[str]
    # コネクタの relation 化は pptx（`_pptx_objects.walk`）と同じ二段パス: drawing part 全体の object_id を先に集めてから解決する
    ids_by_object: dict[int, str] = field(default_factory=dict)
    pending_connectors: list[tuple[str, int | None, int | None]] = field(default_factory=list)
    z_order: int = 0
    anchor_ext: dict[str, Any] = field(default_factory=dict)
    cell_range: str | None = None
    source_rect: list[float] | None = None


def _xlsx_node_extension(
    ctx: _XlsxDrawing,
    node: ET.Element,
    kind: str,
    props: ET.Element | None,
) -> tuple[str, dict[str, Any], str | None, str | None, dict[str, Any]]:
    """図形 1 つの extension を作る。graphic_frame は chart / smartart に読み替え、(kind, extension, chart_part, smartart_value, smartart_extension) を返す。"""
    extension: dict[str, Any] = {
        **ctx.anchor_ext,
        "z_order": ctx.z_order,
        "name": props.get("name", "") if props is not None else "",
        "description": props.get("descr", "") if props is not None else "",
    }
    if (prst := _prst_geom(node, _XDR, kind)) is not None:
        extension["prst"] = prst
    chart_part = None
    chart_extension: dict[str, Any] = {}
    smartart_value = None
    smartart_extension: dict[str, Any] = {}
    smartart_parts: set[str] = set()
    if kind == "graphic_frame":
        chart = node.find(f".//{_C}chart")
        relationship_id = chart.get(f"{_R}id", "") if chart is not None else ""
        candidate = ctx.drawing_rels.get(relationship_id)
        if candidate in ctx.entries:
            kind = "chart"
            chart_part = candidate
            ctx.content_parts.add(candidate)
            _, chart_extension = _xlsx_chart_payload(ctx.entries, candidate)
            extension.update({
                "relationship_id": relationship_id,
                "chart_part": candidate,
                **chart_extension,
            })
        else:
            smartart_value, smartart_extension, smartart_parts = _smartart_payload(
                ctx.entries, ctx.drawing_part, node,
            )
            if smartart_extension.get("relationship_ids"):
                kind = "smartart"
                ctx.content_parts.update(smartart_parts)
                extension.update(smartart_extension)
    asset_role = "picture_content" if kind == "picture" else "shape_fill"
    extension.update(_drawing_asset(ctx.entries, ctx.drawing_part, node, role=asset_role))
    if kind == "connector":
        extension["start_object_id"] = _connection(node.find(f".//{_A}stCxn"))
        extension["end_object_id"] = _connection(node.find(f".//{_A}endCxn"))
    return kind, extension, chart_part, smartart_value, smartart_extension


def _xlsx_add_node_data(
    ctx: _XlsxDrawing,
    kind: str,
    element_id: str,
    object_token: str,
    chart_part: str | None,
    smartart_value: str | None,
    smartart_extension: dict[str, Any],
) -> None:
    """chart / smartart の中身（系列・ノード）を子要素として足す。"""
    if chart_part is not None:
        _add_chart_data(
            ctx.builder,
            ctx.entries,
            chart_part=chart_part,
            parent_id=element_id,
            object_token=object_token,
            sheet=ctx.sheet_name,
        )
    if kind == "smartart":
        _add_smartart_data(
            ctx.builder,
            ctx.entries,
            parent_id=element_id,
            value=smartart_value,
            extension=smartart_extension,
            host_part=ctx.drawing_part,
            object_token=object_token,
            sheet=ctx.sheet_name,
        )


def _xlsx_geometry_relations(
    ctx: _XlsxDrawing,
    node: ET.Element,
    kind: str,
    element_id: str,
    text: str,
    extension: dict[str, Any],
) -> None:
    """drawing と抽出済み cell region の幾何交差を overlaps に、前面の塗りが閾値以上覆うセルを covers にする。"""
    source_rect = ctx.source_rect
    # 状態は確定せず、drawing と抽出済み cell region の幾何交差だけを relation にする。
    # グループ子孫（order_base != 0）は呼び出し側で対象外: 子の cell_range は親 anchor の継承（近似）で、
    # その矩形で overlaps/覆いを主張すると事実でない幾何を relation にしてしまう。
    geometry = _xlsx_sheet_geometry(ctx.builder, ctx.geometry_cache, ctx.sheet_name)
    near_cells = geometry.cells_near(source_rect)
    targets = geometry.tables_by_rect.get(tuple(source_rect)) or near_cells
    for target_id, target_rect in targets:
        intersection = _bbox_intersection(source_rect, target_rect)
        if intersection is None:
            continue
        source_area, target_area = _area(source_rect), _area(target_rect)
        intersection_area = _area(intersection)
        ctx.builder.add_relation(
            "overlaps",
            element_id,
            target_id,
            evidence_ids=[element_id, target_id],
            extension=observation_vocab.relation_extension(
                observation_vocab.BASIS_GEOMETRY,
                observation_vocab.DIRECTION_UNDIRECTED,
                coordinate_system="normalized-cell",
                intersection_bbox=intersection,
                intersection_area=intersection_area,
                source_overlap_ratio=intersection_area / source_area if source_area else 0.0,
                target_overlap_ratio=intersection_area / target_area if target_area else 0.0,
            ),
        )
    # 前面の図形/画像がセルを閾値以上覆うなら、塗りの区分つきで前面 → セルの covers を作り、
    # セルの状態（hidden・理由・occluded_by）はそこから導く（`_field_piece` は cell 自身の `visibility` を見る）。
    # hidden になるのは不透明の塗りのときだけ（`_apply_cover_state`）。
    front_fill = _drawing_front_fill(
        node, kind, ns=_XDR, text=text, extension=extension, entries=ctx.entries)
    if front_fill is not None:
        for target_id, target_rect in near_cells:
            intersection = _bbox_intersection(source_rect, target_rect)
            if intersection is None:
                continue
            target_area = _area(target_rect)
            ratio = _area(intersection) / target_area if target_area else 0.0
            if ratio >= _occlusion_ratio():
                ctx.builder.add_covers(
                    element_id, target_id, fill=front_fill,
                    cover_ratio=ratio, coordinate_system="normalized-cell",
                    covers_bbox=_drawing_covers_bbox(node.find(f"{_XDR}spPr"), kind))
                _apply_cover_state(ctx.builder, target_id, file_type="xlsx")


def _xlsx_walk(ctx: _XlsxDrawing, nodes: list[ET.Element], parent_id: str | None, order_base: int) -> None:
    """`xdr:grpSp` の子を個別要素として辿る（`_pptx_objects.walk` と同じ再帰形）。

    グループの中身を連結文字列へ潰さず、ノード名で検索できるようにする。xlsx の drawing 座標系は anchor 単位でしか
    セル範囲を持たないため、group 子孫の `cell_range` は親 anchor のものをそのまま継承する。
    """
    local_index = 0
    for node in nodes:
        kind = _drawing_kind(node)
        if kind is None:
            continue
        local_index += 1
        order = ctx.z_order if order_base == 0 else order_base + local_index
        props = _c_nv_pr(node, _XDR, kind)
        object_id = _integer(props.get("id")) if props is not None else None
        text = paragraph_separated_text(node, _A)
        kind, extension, chart_part, smartart_value, smartart_extension = _xlsx_node_extension(ctx, node, kind, props)
        locator = evidence_ir.Locator(
            part=ctx.drawing_part,
            sheet=ctx.sheet_name,
            cell_range=ctx.cell_range,
            object_id=object_id,
            extension={"coordinate_system": "xlsx-cell-anchor"},
        )
        element_id = ctx.builder.add_element(
            kind,
            locator,
            parent_id=parent_id,
            order=order,
            value=text or None,
            coverage_status="metadata_only" if kind == "picture" else "extracted",
            coverage_reason="image_content_uninterpreted" if kind == "picture" else None,
            extension=extension,
        )
        if object_id is not None:
            ctx.ids_by_object[object_id] = element_id
        if kind == "connector":
            ctx.pending_connectors.append(
                (element_id, extension.get("start_object_id"), extension.get("end_object_id")))
        _add_raster_reference_coverage(
            ctx.builder,
            locator,
            extension,
            failed_kind="drawing_image_asset",
            external_kind="drawing_external_image_reference",
        )
        _xlsx_add_node_data(
            ctx, kind, element_id, str(object_id if object_id is not None else order),
            chart_part, smartart_value, smartart_extension)
        if ctx.source_rect is not None and order_base == 0:
            _xlsx_geometry_relations(ctx, node, kind, element_id, text, extension)
        if kind == "group":
            child_nodes = [child for child in list(node) if _drawing_kind(child) is not None]
            _xlsx_walk(ctx, child_nodes, element_id, order * 1000)


def _xlsx_drawing_objects(ctx: _XlsxDrawing, drawing: ET.Element, sheet_id: str | None) -> None:
    for z_order, anchor in enumerate(list(drawing), start=1):
        ctx.z_order = z_order
        ctx.cell_range, ctx.anchor_ext = _xlsx_anchor(anchor)
        ctx.source_rect = _cell_rect(ctx.cell_range)
        _xlsx_walk(ctx, list(anchor), sheet_id, 0)
    for connector_id, start_object_id, end_object_id in ctx.pending_connectors:
        _add_connector_relation(ctx.builder, connector_id, start_object_id, end_object_id, ctx.ids_by_object)


def _xlsx_ole_objects(
    builder: _Builder,
    entries: dict[str, bytes],
    root: ET.Element,
    sheet_part: str,
    sheet_name: str,
    sheet_id: str | None,
) -> None:
    for ole_order, ole in enumerate(root.findall(f".//{_X}oleObject"), start=1):
        relationship_id = ole.get(f"{_R}id", "")
        if not relationship_id:
            continue
        cell_range, anchor_extension = _xlsx_embedded_anchor(ole)
        extension = {
            **anchor_extension,
            **_ole_metadata(entries, sheet_part, relationship_id, ole),
            "shape_id": ole.get("shapeId"),
        }
        builder.add_element(
            "ole_object",
            evidence_ir.Locator(
                part=sheet_part,
                sheet=sheet_name,
                cell_range=cell_range,
                object_id=ole.get("shapeId") or relationship_id,
                extension={"coordinate_system": "xlsx-cell-anchor"},
            ),
            parent_id=sheet_id,
            order=100_000 + ole_order,
            value=None,
            coverage_status="metadata_only",
            coverage_reason="ole_binary_metadata_only",
            coverage_content_basis="binary_only",
            extension=extension,
        )


def _xlsx_vml_objects(
    builder: _Builder,
    entries: dict[str, bytes],
    sheet_rels: dict[str, str],
    sheet_name: str,
    sheet_id: str | None,
    content_parts: set[str],
) -> None:
    for vml_part in sorted({
        target for target in sheet_rels.values()
        if target in entries and (target.lower().endswith(".vml") or "vmldrawing" in target.lower())
    }):
        vml_root = _xml(entries, vml_part)
        if vml_root is None:
            continue
        content_parts.add(vml_part)
        _extract_vml_shapes(
            builder,
            entries,
            vml_root,
            part=vml_part,
            parent_id=sheet_id,
            sheet=sheet_name,
            order_base=200_000,
        )


def _xlsx_objects(builder: _Builder, entries: dict[str, bytes]) -> set[str]:
    geometry_cache: dict[str, _SheetGeometry] = {}
    content_parts = {"xl/workbook.xml"}
    content_parts.update(part for part in ("xl/sharedStrings.xml", "xl/styles.xml") if part in entries)
    sheet_ids = {
        element.locator.sheet: element.element_id
        for element in builder.ir.elements
        if element.type == "sheet" and element.locator.sheet
    }
    for sheet_name, sheet_part in _xlsx_sheet_parts(entries).items():
        content_parts.add(sheet_part)
        root = _xml(entries, sheet_part)
        if root is None:
            continue
        sheet_rels = _relationships(entries, sheet_part)
        content_parts.update(
            target for target in sheet_rels.values()
            if target in entries and target.startswith("xl/comments") and target.endswith(".xml")
        )
        sheet_id = sheet_ids.get(sheet_name)
        for drawing_ref in root.findall(f"{_X}drawing"):
            drawing_part = sheet_rels.get(drawing_ref.get(f"{_R}id", ""))
            drawing = _xml(entries, drawing_part or "")
            if drawing is None or drawing_part is None:
                continue
            content_parts.add(drawing_part)
            ctx = _XlsxDrawing(
                builder, entries, geometry_cache, sheet_name, drawing_part,
                _relationships(entries, drawing_part), content_parts)
            _xlsx_drawing_objects(ctx, drawing, sheet_id)
        _xlsx_ole_objects(builder, entries, root, sheet_part, sheet_name, sheet_id)
        _xlsx_vml_objects(builder, entries, sheet_rels, sheet_name, sheet_id, content_parts)
    return content_parts
