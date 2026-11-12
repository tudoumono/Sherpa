"""VML 図形の解析。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

import hashlib
from typing import Any
from xml.etree import ElementTree as ET

from .. import evidence_ir
from ..ooxml import word
from .base import _O, _R, _V, _W
from .builder import _Builder
from .drawing import _add_raster_reference_coverage, _add_unparsed_notice, _image_pixel_size, _integer
from .package import _a1, _image_relationship_records


def _vml_length(value: str | None) -> float | None:
    if not value:
        return None
    lowered = value.strip().lower()
    for suffix in ("pt", "px", "in", "cm", "mm"):
        if lowered.endswith(suffix):
            lowered = lowered[:-len(suffix)]
            break
    try:
        return float(lowered)
    except ValueError:
        return None


def _vml_geometry(shape: ET.Element) -> tuple[str | None, list[float] | None, dict[str, Any], bool]:
    style_text = shape.get("style", "")
    style = {
        name.strip().lower(): value.strip()
        for declaration in style_text.split(";") if ":" in declaration
        for name, value in [declaration.split(":", 1)]
    }
    left = _vml_length(style.get("margin-left") or style.get("left"))
    top = _vml_length(style.get("margin-top") or style.get("top"))
    width = _vml_length(style.get("width"))
    height = _vml_length(style.get("height"))
    bbox = [left, top, left + width, top + height] if None not in {left, top, width, height} else None
    anchor_values: list[int] | None = None
    row = column = None
    for child in shape.iter():
        local = child.tag.split("}")[-1]
        if local == "Anchor" and child.text:
            try:
                values = [int(value.strip()) for value in child.text.split(",")]
            except ValueError:
                values = []
            if len(values) >= 8:
                anchor_values = values[:8]
        elif local == "Row" and child.text:
            row = _integer(child.text)
        elif local == "Column" and child.text:
            column = _integer(child.text)
    cell_range = None
    if anchor_values is not None:
        start_col, start_row, end_col, end_row = (
            anchor_values[0], anchor_values[2], anchor_values[4], anchor_values[6]
        )
        cell_range = f"{_a1(start_row + 1, start_col + 1)}:{_a1(end_row + 1, end_col + 1)}"
    elif row is not None and column is not None:
        cell_range = _a1(row + 1, column + 1)
    extension = {
        "style": style_text,
        "style_fields": style,
        "anchor": anchor_values,
        "coordinate_system": "vml-style" if bbox is not None else "xlsx-vml-anchor" if cell_range else "unknown",
    }
    unparsed_geometry = bbox is None and cell_range is None or any(
        shape.get(attribute) is not None for attribute in ("path", "coordsize", "coordorigin")
    )
    return cell_range, bbox, extension, unparsed_geometry


_VML_OBJECT_TAGS = frozenset({
    f"{_V}shape",
    f"{_V}group",
    f"{_V}rect",
    f"{_V}roundrect",
    f"{_V}oval",
    f"{_V}line",
    f"{_V}polyline",
    f"{_V}curve",
    f"{_V}arc",
    f"{_V}image",
})


def _owned_vml_nodes(shape: ET.Element):
    """shape/groupに属するノードを原文順で返す。子VML objectの内容は子側で別に抽出する。"""
    yield shape
    for child in shape:
        if child.tag in _VML_OBJECT_TAGS:
            continue
        yield from _owned_vml_nodes(child)


def _vml_texts(shape: ET.Element) -> tuple[list[str], list[str]]:
    """VML textboxのWordprocessingML文字とWordArt textpathを原文順で保持する。

    段落の本文は取り消し線・非表示のrunを状態つきで書く（`ooxml.word.stated_text`）。
    """
    texts: list[str] = []
    sources: list[str] = []
    owned = list(_owned_vml_nodes(shape))
    consumed_text_nodes: set[int] = set()
    for node in owned:
        value = None
        source = None
        if node.tag == f"{_W}p":
            runs = [child for child in _owned_vml_nodes(node) if child.tag == f"{_W}t"]
            value = word.stated_text(node)
            consumed_text_nodes.update(id(child) for child in runs)
            source = "w:p/w:t"
        elif node.tag == f"{_W}t" and id(node) not in consumed_text_nodes:
            value, source = node.text, "w:t"
        elif node.tag == f"{_V}textpath":
            value, source = node.get("string"), "v:textpath@string"
        if not isinstance(value, str) or not value.strip():
            continue
        texts.append(value)
        sources.append(source or "unknown")
    return texts, sources


def _vml_assets(entries: dict[str, bytes], part: str, shape: ET.Element) -> dict[str, Any]:
    """VML shape の画像参照を DrawingML と同じ asset 契約へ写す。

    画像は ``v:imagedata`` のほか ``v:fill`` に保持される原本がある。relationship 属性は ``r:id`` または ``o:relid`` で、
    どちらも part 自身の relationship から解決する。参照単位を維持し、同じ bytes を複数箇所が参照しても統合しない。
    """
    relationships = _image_relationship_records(entries, part)
    references: list[dict[str, Any]] = []
    relationship_ids: list[str] = []
    unresolved: list[str] = []
    external: list[str] = []
    for node in _owned_vml_nodes(shape):
        local_name = node.tag.split("}")[-1]
        if node.tag not in {f"{_V}imagedata", f"{_V}fill", f"{_V}image"}:
            continue
        identifier = next((
            (attribute, value)
            for attribute, key in (
                ("id", f"{_R}id"),
                ("embed", f"{_R}embed"),
                ("link", f"{_R}link"),
                ("relid", f"{_O}relid"),
                ("relid", "relid"),
            )
            if isinstance((value := node.get(key)), str) and value
        ), None)
        relationship_id = identifier[1] if identifier is not None else None
        role = "picture_content" if node.tag == f"{_V}image" else "shape_fill"
        if relationship_id is None:
            # 単色/gradientだけのv:fillは画像assetではない。imagedata、またはsrcを持つfillだけを
            # content-bearingな未解決画像として分類する。
            if node.tag in {f"{_V}imagedata", f"{_V}image"} or node.get("src"):
                unresolved.append(f"{local_name}:missing_relationship_id")
                references.append({
                    "asset_role": role,
                    "relationship_attribute": "missing",
                    "binding_status": "relationship_id_missing",
                    "vml_element": local_name,
                })
            continue
        relationship_ids.append(relationship_id)
        reference: dict[str, Any] = {
            "relationship_id": relationship_id,
            "relationship_attribute": identifier[0],
            "asset_role": role,
            "vml_element": local_name,
        }
        relationship = relationships.get(relationship_id)
        if relationship is None:
            reference["binding_status"] = "relationship_missing"
            unresolved.append(relationship_id)
        elif relationship.get("target_mode", "Internal").casefold() == "external":
            reference.update(relationship)
            reference["binding_status"] = "external_reference"
            external.append(relationship_id)
        else:
            media_part = relationship.get("media_part")
            reference.update(relationship)
            if media_part not in entries:
                reference["binding_status"] = "target_missing"
                unresolved.append(relationship_id)
            else:
                media = entries[media_part]
                reference.update({
                    "asset_sha256": hashlib.sha256(media).hexdigest(),
                    "binding_status": "resolved",
                })
                if (pixel_size := _image_pixel_size(media)) is not None:
                    reference["pixel_size"] = pixel_size
        references.append(reference)
    extension: dict[str, Any] = {"image_references": references, "assets": references} if references else {}
    if relationship_ids:
        extension["image_relationship_ids"] = relationship_ids
    if unresolved:
        extension["unresolved_image_relationship_ids"] = unresolved
    if external:
        extension["external_image_relationship_ids"] = external
    if references:
        resolved = [reference for reference in references if reference.get("binding_status") == "resolved"]
        extension.update(resolved[0] if resolved else references[0])
    return extension


def _extract_vml_shapes(
    builder: _Builder,
    entries: dict[str, bytes],
    root: ET.Element,
    *,
    part: str,
    parent_id: str | None = None,
    sheet: str | None = None,
    slide: int | None = None,
    order_base: int = 0,
    visibility: str = "visible",
    lifecycle: str = "active",
    host_metadata: dict[str, Any] | None = None,
) -> None:
    shapes = (node for node in root.iter() if node.tag in _VML_OBJECT_TAGS)
    for index, shape in enumerate(shapes, start=1):
        primitive = shape.tag.split("}")[-1]
        object_id = shape.get("id") or f"vml:{primitive}:{index}"
        cell_range, bbox, geometry, unparsed_geometry = _vml_geometry(shape)
        asset_extension = _vml_assets(entries, part, shape)
        texts, text_sources = _vml_texts(shape)
        locator = evidence_ir.Locator(
            part=part,
            slide=slide,
            sheet=sheet,
            cell_range=cell_range,
            object_id=object_id,
            bbox=bbox,
            extension={
                "coordinate_system": geometry["coordinate_system"],
                **(host_metadata or {}),
            },
        )
        builder.add_element(
            "vml_shape",
            locator,
            parent_id=parent_id,
            order=order_base + index,
            value="\n".join(texts) or None,
            visibility=visibility,
            lifecycle=lifecycle,
            extension={
                **geometry,
                "vml_primitive": primitive,
                "shape_type": shape.get("type"),
                "title": shape.get("title"),
                "text_sources": text_sources,
                **(host_metadata or {}),
                **asset_extension,
            },
        )
        _add_raster_reference_coverage(
            builder,
            locator,
            asset_extension,
            failed_kind="vml_image_asset",
            external_kind="vml_external_image_reference",
        )
        if unparsed_geometry:
            _add_unparsed_notice(
                builder,
                locator,
                detected_kind="vml_unparsed_geometry",
                reason_code="unsupported_vml_geometry",
                detail={"shape_id": object_id},
            )
