"""DOCX の drawing/object inventory。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from .. import drawing_fill, evidence_ir, observation_vocab
from ..ooxml.text import paragraph_separated_text
from .adapt import _apply_cover_state
from .base import _A, _C, _ContentHost, _O, _PIC, _R, _V, _W, _WP, _WPS
from .builder import _Builder
from .drawing import (
    _add_chart_data, _add_raster_reference_coverage, _add_smartart_data, _area, _bbox_intersection, _chart_payload,
    _drawing_asset, _drawing_covers_bbox, _drawing_front_fill, _integer, _occlusion_ratio, _ole_metadata,
    _smartart_payload,
)
from .package import _relationships, _xml
from .vml import _extract_vml_shapes, _vml_geometry


# 位置の決まった浮動図形（`wp:anchor` の位置が `posOffset` で、基準がページ／余白）。同じ基準どうしだけ重なりを比べられる。
_DOCX_ABSOLUTE_H_FRAMES = frozenset({"page", "margin", "leftMargin", "rightMargin"})
_DOCX_ABSOLUTE_V_FRAMES = frozenset({"page", "margin", "topMargin", "bottomMargin"})


def _docx_absolute_frame(anchor: ET.Element) -> tuple[str, str] | None:
    """`wp:anchor` の位置が (横の基準, 縦の基準) の絶対値で決まっていればその組。段落・文字・行などの基準や `wp:align` は None。"""
    if anchor.tag != f"{_WP}anchor":
        return None
    horizontal, vertical = anchor.find(f"{_WP}positionH"), anchor.find(f"{_WP}positionV")
    if horizontal is None or vertical is None:
        return None
    if horizontal.find(f"{_WP}posOffset") is None or vertical.find(f"{_WP}posOffset") is None:
        return None
    frame = (horizontal.get("relativeFrom") or "", vertical.get("relativeFrom") or "")
    if frame[0] in _DOCX_ABSOLUTE_H_FRAMES and frame[1] in _DOCX_ABSOLUTE_V_FRAMES:
        return frame
    return None


def _docx_anchor_pages(root: ET.Element) -> tuple[dict[int, int], bool]:
    """`wp:anchor`（`id(anchor)`）ごとのページ区切りの通し番号と、Word が組版したページ境界の記録があるかを返す。

    本文を入れ子（`w:sdt`・`w:customXml`・`w:smartTag`・表のセルなど）も含めて文書順に 1 回だけ走査し、アンカーの位置と
    区切りの位置を同じ順序で数える。区切りは、Word が最後に組版したときのページ境界（`w:lastRenderedPageBreak`）・
    明示の改ページ（`w:br type=page`）・`w:pageBreakBefore`（その段落の前）・段落の `w:pPr/w:sectPr`（その段落の後）。
    テキストボックスの中の段落はページを区切らない。同じページと確かめられるかは `_docx_same_page` が決める。
    """
    container = root.find(f"{_W}body")
    container = container if container is not None else root
    pages: dict[int, int] = {}
    state = {"segment": 0, "rendered": False}

    def walk(node: ET.Element, in_textbox: bool) -> None:
        tag = node.tag
        if tag == f"{_WP}anchor":
            pages[id(node)] = state["segment"]
        elif not in_textbox and tag == f"{_W}br" and node.get(f"{_W}type") == "page":
            state["segment"] += 1
        elif not in_textbox and tag == f"{_W}lastRenderedPageBreak":
            state["segment"] += 1
            state["rendered"] = True
        paragraph = tag == f"{_W}p" and not in_textbox
        if paragraph and node.find(f"{_W}pPr/{_W}pageBreakBefore") is not None:
            state["segment"] += 1
        inner_textbox = in_textbox or tag == f"{_W}txbxContent"
        for child in node:
            if not (paragraph and child.tag == f"{_W}pPr"):
                walk(child, inner_textbox)
        if paragraph and node.find(f"{_W}pPr/{_W}sectPr") is not None:
            state["segment"] += 1

    for child in container:
        walk(child, False)
    return pages, state["rendered"]


def _docx_same_page(left: int | None, right: int | None, rendered: bool) -> bool:
    """2 つの浮動図形が同じページにあると確かめられるか。Word が組版したページ境界の記録がある文書で、
    2 つのアンカーの間にページ区切りが無いときだけ True（自動の改ページは組版の記録が無ければ原本から分からない）。"""
    return rendered and left is not None and left == right


def _docx_front_fill(anchor: ET.Element, kind: str, text: str, extension: dict[str, Any], entries: dict[str, bytes]) -> str | None:
    """浮動図形の塗りの区分。画像と `wps:wsp` の図形だけ判定し、チャート・SmartArt などは None（covers を作らない）。"""
    if kind == "picture":
        return _drawing_front_fill(anchor, "picture", ns=_PIC, text=text, extension=extension, entries=entries)
    shape_properties = anchor.find(f".//{_WPS}spPr")
    if shape_properties is None:
        return None
    return drawing_fill.shape_fill_kind(
        shape_properties, anchor.find(f".//{_WPS}style"), has_text=bool(text.strip()))


def _docx_content_hosts(entries: dict[str, bytes]) -> list[_ContentHost]:
    """本文と補助本文partを、各part固有のrelationship境界を保って列挙する。"""
    hosts = [
        _ContentHost("word/document.xml", "document_body", "document_body"),
    ]
    kinds = {
        "word/comments.xml": ("comments", "annotation"),
        "word/footnotes.xml": ("footnotes", "reference_note"),
        "word/endnotes.xml": ("endnotes", "reference_note"),
    }
    for part in sorted(entries):
        if part.startswith("word/header") and part.endswith(".xml"):
            hosts.append(_ContentHost(part, "header", "page_repeat"))
        elif part.startswith("word/footer") and part.endswith(".xml"):
            hosts.append(_ContentHost(part, "footer", "page_repeat"))
        elif part in kinds:
            kind, host_visibility = kinds[part]
            hosts.append(_ContentHost(part, kind, host_visibility))
    return hosts


@dataclass
class _DocxHost:
    """1 つの content host を走査する間の状態（足した図形の位置・前後関係・文字を避けない図形）。"""

    builder: _Builder
    entries: dict[str, bytes]
    host: _ContentHost
    order_base: int
    rels: dict[str, str]
    host_metadata: dict[str, Any]
    content_parts: set[str]
    anchor_pages: dict[int, int]
    positioned: list[tuple[str, list[int | float], int]] = field(default_factory=list)
    figures: dict[str, dict[str, Any]] = field(default_factory=dict)
    wrap_none_anchors: list[tuple[str, ET.Element, bool]] = field(default_factory=list)


def _docx_anchor_geometry(anchor: ET.Element) -> tuple[list[int] | None, list[int | float] | None]:
    """anchor / inline の (extent_emu, bbox)。読めなければ None。"""
    extent = anchor.find(f"{_WP}extent")
    x_node = anchor.find(f"{_WP}positionH/{_WP}posOffset")
    y_node = anchor.find(f"{_WP}positionV/{_WP}posOffset")
    try:
        cx, cy = int(extent.get("cx")), int(extent.get("cy"))
        extent_emu: list[int] | None = [cx, cy]
    except (AttributeError, TypeError, ValueError):
        extent_emu = None
    try:
        x, y = int(x_node.text or 0), int(y_node.text or 0)
        if extent_emu is None:
            raise ValueError
        cx, cy = extent_emu
        bbox: list[int | float] | None = [x, y, x + cx, y + cy]
    except (AttributeError, TypeError, ValueError):
        bbox = None
    return extent_emu, bbox


def _docx_anchor_extension(
    state: _DocxHost,
    anchor: ET.Element,
    props: ET.Element | None,
    *,
    kind: str,
    is_picture: bool,
    text: str,
    chart_part: str | None,
    chart_relationship_id: str,
    smartart_extension: dict[str, Any],
    smartart_parts: set[str],
) -> tuple[dict[str, Any], str | None, ET.Element | None]:
    """anchor 1 つの extension を作る。chart / smartart の part は content_parts へ足す。(extension, front_fill, front_properties) を返す。"""
    extension: dict[str, Any] = {
        "anchor_type": anchor.tag.split("}")[-1],
        "name": props.get("name", "") if props is not None else "",
        "description": props.get("descr", "") if props is not None else "",
        "z_order": _integer(anchor.get("relativeHeight")),
        **state.host_metadata,
    }
    alpha = next((_integer(node.get("val")) for node in anchor.findall(f".//{_A}alpha")
                  if _integer(node.get("val")) is not None), None)
    if alpha is not None:
        extension["alpha"] = alpha
    asset_role = "picture_content" if is_picture else "shape_fill"
    extension.update(_drawing_asset(state.entries, state.host.part, anchor, role=asset_role))
    front_fill = _docx_front_fill(anchor, kind, text, extension, state.entries)
    front_properties = anchor.find(f".//{_PIC}spPr") if kind == "picture" else anchor.find(f".//{_WPS}spPr")
    if chart_part in state.entries:
        state.content_parts.add(chart_part)
        _, chart_extension = _chart_payload(state.entries, chart_part)
        extension.update({
            "relationship_id": chart_relationship_id,
            "chart_part": chart_part,
            **chart_extension,
        })
    if kind == "smartart":
        state.content_parts.update(smartart_parts)
        extension.update(smartart_extension)
    return extension, front_fill, front_properties


def _docx_add_anchor(state: _DocxHost, order: int, anchor: ET.Element) -> None:
    """anchor / inline 1 つを要素として足し、重なり判定用に位置と前後関係を state へ残す。"""
    builder, entries, host = state.builder, state.entries, state.host
    props = anchor.find(f"{_WP}docPr")
    object_id = _integer(props.get("id")) if props is not None else None
    extent_emu, bbox = _docx_anchor_geometry(anchor)
    is_picture = anchor.find(f".//{_PIC}pic") is not None
    chart = anchor.find(f".//{_C}chart")
    chart_relationship_id = chart.get(f"{_R}id", "") if chart is not None else ""
    chart_part = state.rels.get(chart_relationship_id)
    smartart_value, smartart_extension, smartart_parts = _smartart_payload(entries, host.part, anchor)
    text = paragraph_separated_text(anchor, _W)
    kind = (
        "picture" if is_picture else
        "chart" if chart_part in entries else
        "smartart" if smartart_extension.get("relationship_ids") else
        "floating_textbox" if text else "floating_object"
    )
    extension, front_fill, front_properties = _docx_anchor_extension(
        state, anchor, props, kind=kind, is_picture=is_picture, text=text, chart_part=chart_part,
        chart_relationship_id=chart_relationship_id, smartart_extension=smartart_extension,
        smartart_parts=smartart_parts)
    host_metadata = state.host_metadata
    locator = evidence_ir.Locator(
        part=host.part,
        object_id=object_id,
        bbox=bbox,
        extension={
            "coordinate_system": "word-page-emu",
            **({"host_object_index": order} if host_metadata else {}),
            **({"extent_emu": extent_emu} if host_metadata and extent_emu is not None else {}),
            **host_metadata,
        },
    )
    element_id = builder.add_element(
        kind,
        locator,
        parent_id=None,
        order=state.order_base + order,
        value=text or None,
        coverage_status="metadata_only" if is_picture else "extracted",
        coverage_reason="image_content_uninterpreted" if is_picture else None,
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
    if chart_part in entries:
        _add_chart_data(
            builder,
            entries,
            chart_part=chart_part,
            parent_id=element_id,
            object_token=str(object_id if object_id is not None else state.order_base + order),
        )
    if kind == "smartart":
        _add_smartart_data(
            builder,
            entries,
            parent_id=element_id,
            value=smartart_value,
            extension=smartart_extension,
            host_part=host.part,
            object_token=str(object_id if object_id is not None else state.order_base + order),
        )
    behind_doc = anchor.get("behindDoc") in {"1", "true"}
    state.figures[element_id] = {
        "frame": _docx_absolute_frame(anchor),
        "page": state.anchor_pages.get(id(anchor)),
        "fill": front_fill,
        "covers_bbox": _drawing_covers_bbox(front_properties, kind),
        # 手前にある順の比較キー: 本文より前面の図形 > 背面の図形、同じなら relativeHeight・出現順が大きいほう。
        "stack": (not behind_doc, extension.get("z_order") or 0, order),
    }
    if anchor.find(f"{_WP}wrapNone") is not None:
        state.wrap_none_anchors.append((element_id, anchor, behind_doc))
    if bbox is not None:
        state.positioned.append((element_id, bbox, extension.get("z_order") or order))


def _docx_ole_objects(state: _DocxHost, root: ET.Element) -> None:
    builder, entries, host, host_metadata = state.builder, state.entries, state.host, state.host_metadata
    vml_shapes = {
        shape.get("id"): shape for shape in root.iter(f"{_V}shape") if shape.get("id")
    }
    for ole_order, ole in enumerate(root.iter(f"{_O}OLEObject"), start=1):
        relationship_id = ole.get(f"{_R}id", "")
        if not relationship_id:
            continue
        shape_id = ole.get("ShapeID") or ole.get("shapeId")
        host_shape = vml_shapes.get(shape_id)
        cell_range = None
        bbox = None
        geometry: dict[str, Any] = {}
        if host_shape is not None:
            cell_range, bbox, geometry, _unparsed = _vml_geometry(host_shape)
        builder.add_element(
            "ole_object",
            evidence_ir.Locator(
                part=host.part,
                cell_range=cell_range,
                object_id=ole.get("ObjectID") or shape_id or relationship_id,
                bbox=bbox,
                extension={
                    "coordinate_system": geometry.get("coordinate_system", "word-flow"),
                    **host_metadata,
                },
            ),
            parent_id=None,
            order=state.order_base + 200_000 + ole_order,
            value=None,
            coverage_status="metadata_only",
            coverage_reason="ole_binary_metadata_only",
            coverage_content_basis="binary_only",
            visibility=host.visibility,
            lifecycle=host.lifecycle,
            extension={
                "shape_id": shape_id,
                **host_metadata,
                **geometry,
                **_ole_metadata(entries, host.part, relationship_id, ole),
            },
        )


def _docx_figure_relations(state: _DocxHost, pages_rendered: bool) -> None:
    """図形どうしの重なり（overlaps）と、同じ基準で比べられるときの遮蔽（covers）。"""
    builder, positioned, figures = state.builder, state.positioned, state.figures
    # 重なり（overlaps）はどの場合も残す。遮蔽（covers）は、位置の決まった図形どうしを同じ基準で比べられるときだけ作る。
    # 位置が段落・文字などの基準で決まる図形や、同じページと確かめられない図形は、遮蔽を判定できない（undetermined）。
    for index, (left_id, left_bbox, left_z) in enumerate(positioned):
        for right_id, right_bbox, right_z in positioned[index + 1:]:
            intersection = _bbox_intersection(left_bbox, right_bbox)
            if intersection is None:
                continue
            source_id, target_id = (left_id, right_id) if left_z >= right_z else (right_id, left_id)
            left_frame, right_frame = figures[left_id]["frame"], figures[right_id]["frame"]
            comparable = (
                left_frame is not None and left_frame == right_frame
                and _docx_same_page(figures[left_id]["page"], figures[right_id]["page"], pages_rendered))
            extra: dict[str, Any] = {} if comparable else {"occlusion": observation_vocab.OCCLUSION_UNDETERMINED}
            builder.add_relation(
                "overlaps",
                source_id,
                target_id,
                evidence_ids=[source_id, target_id],
                extension=observation_vocab.relation_extension(
                    observation_vocab.BASIS_GEOMETRY, observation_vocab.DIRECTION_UNDIRECTED,
                    intersection_bbox=intersection, coordinate_system="word-page-emu", **extra),
            )
            if comparable:
                _docx_add_figure_covers(
                    builder, (left_id, left_bbox), (right_id, right_bbox), figures, intersection)


def _docx_wrap_none_relations(state: _DocxHost, root: ET.Element) -> None:
    builder = state.builder
    paragraph_ids = _docx_anchor_paragraph_ids(builder, root, [anchor for _, anchor, _ in state.wrap_none_anchors])
    for figure_id, anchor, behind_doc in state.wrap_none_anchors:
        paragraph_id = paragraph_ids.get(id(anchor))
        if paragraph_id is None:
            continue
        # 文字を避けない図形は本文の文字に重なり得るが、文字の位置は組版で決まるので覆っているかは判定できない。
        # 背面の図形は文字の下にあるので覆わない。どちらも本文は hidden にしない。
        occlusion = (observation_vocab.OCCLUSION_NOT_OCCLUDING if behind_doc
                     else observation_vocab.OCCLUSION_UNDETERMINED)
        builder.add_relation(
            "overlaps",
            figure_id,
            paragraph_id,
            evidence_ids=[figure_id, paragraph_id],
            extension=observation_vocab.relation_extension(
                observation_vocab.BASIS_OOXML_ANCHOR, observation_vocab.DIRECTION_UNDIRECTED,
                occlusion=occlusion),
        )


def _docx_host_objects(
    builder: _Builder,
    entries: dict[str, bytes],
    host: _ContentHost,
    *,
    order_base: int,
) -> set[str]:
    """1つのWord content host内のDrawingML/VML/OLEを、そのhost自身のrelsで解決する。"""
    content_parts = {host.part}
    root = _xml(entries, host.part)
    if root is None:
        return content_parts
    # 本文の既存locator/element IDは不変にし、追加hostだけに明示的なhost metadataを刻む。
    host_metadata = {} if host.host_kind == "document_body" else host.metadata()
    anchor_pages, pages_rendered = _docx_anchor_pages(root)
    state = _DocxHost(
        builder, entries, host, order_base, _relationships(entries, host.part), host_metadata,
        content_parts, anchor_pages)
    for order, anchor in enumerate(root.findall(f".//{_WP}anchor") + root.findall(f".//{_WP}inline"), start=1):
        _docx_add_anchor(state, order, anchor)

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
    _docx_ole_objects(state, root)
    _docx_figure_relations(state, pages_rendered)
    if state.wrap_none_anchors and host.host_kind == "document_body":
        _docx_wrap_none_relations(state, root)
    return content_parts


def _docx_add_figure_covers(
    builder: _Builder,
    left: tuple[str, list[int | float]],
    right: tuple[str, list[int | float]],
    figures: dict[str, dict[str, Any]],
    intersection: list[int | float],
) -> None:
    """位置の決まった図形どうしの重なりから、前面 → 背面の covers を作り、背面の状態を導く（覆う割合が閾値以上のときだけ）。"""
    (front_id, _front_bbox), (back_id, back_bbox) = sorted(
        (left, right), key=lambda item: figures[item[0]]["stack"], reverse=True)
    front_fill = figures[front_id]["fill"]
    back_area = _area(back_bbox)
    if front_fill is None or not back_area:
        return
    ratio = _area(intersection) / back_area
    if ratio < _occlusion_ratio():
        return
    builder.add_covers(
        front_id, back_id, fill=front_fill, cover_ratio=ratio, coordinate_system="word-page-emu",
        covers_bbox=figures[front_id]["covers_bbox"])
    _apply_cover_state(builder, back_id, file_type="docx")


def _docx_anchor_paragraph_ids(builder: _Builder, root: ET.Element, anchors: list[ET.Element]) -> dict[int, str]:
    """本文直下の段落にある `wp:anchor`（`id(anchor)`）から、その段落の Evidence 要素 ID を引く。表の中・本文が空の段落は含まない。"""
    body = root.find(f"{_W}body")
    if body is None:
        return {}
    by_index: dict[int, str] = {}
    for element in builder.ir.elements:
        if element.type not in ("paragraph", "heading"):
            continue
        source_map = element.locator.extension.get("document_ir_source_map")
        paragraph_index = source_map.get("paragraph_index") if isinstance(source_map, dict) else None
        if isinstance(paragraph_index, int):
            by_index[paragraph_index] = element.element_id
    wanted = {id(anchor) for anchor in anchors}
    found: dict[int, str] = {}
    paragraph_index = -1
    for child in body:
        if child.tag != f"{_W}p":
            continue
        paragraph_index += 1
        if paragraph_index not in by_index:
            continue
        for anchor in child.iter(f"{_WP}anchor"):
            if id(anchor) in wanted:
                found[id(anchor)] = by_index[paragraph_index]
    return found


def _external_vml_host_context(
    entries: dict[str, bytes],
    hosts: list[_ContentHost],
    vml_part: str,
) -> tuple[dict[str, Any], str, str, int | None]:
    owners = [host for host in hosts if vml_part in _relationships(entries, host.part).values()]
    if not owners:
        return (
            {"host_part": vml_part, "host_kind": "detached_vml", "host_visibility": "unknown", "host_lifecycle": "active"},
            "visible",
            "active",
            None,
        )
    primary = owners[0]
    return (
        {**primary.metadata(), "host_parts": [owner.part for owner in owners]},
        primary.visibility,
        primary.lifecycle,
        primary.slide,
    )


def _docx_objects(builder: _Builder, entries: dict[str, bytes]) -> set[str]:
    hosts = _docx_content_hosts(entries)
    content_parts: set[str] = set()
    main = hosts[0]
    content_parts.update(_docx_host_objects(builder, entries, main, order_base=0))

    # 既存本文objectの要素順を変えないため、外部VMLと補助hostは本文objectの後へ追加する。
    for vml_index, vml_part in enumerate(sorted(
        candidate for candidate in entries
        if candidate.startswith("word/") and (candidate.lower().endswith(".vml") or "vmldrawing" in candidate.lower())
    ), start=1):
        vml_root = _xml(entries, vml_part)
        if vml_root is None:
            continue
        content_parts.add(vml_part)
        metadata, visibility, lifecycle, slide = _external_vml_host_context(entries, hosts, vml_part)
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

    for host_index, host in enumerate(hosts[1:], start=1):
        content_parts.update(_docx_host_objects(
            builder,
            entries,
            host,
            order_base=1_000_000 * host_index,
        ))
    return content_parts
