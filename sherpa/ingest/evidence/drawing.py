"""DrawingML の図形・画像・chart・SmartArt の共通解析。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from .. import drawing_fill, evidence_ir, image_alpha, observation_vocab
from ..metafile_text import child_png_size
from .base import _A, _C, _DGM, _R, _X, _XDR
from .builder import _Builder
from .package import _a1, _content_type, _image_relationship_records, _relationship_records, _relationships, _xml


def _xlsx_sheet_parts(entries: dict[str, bytes]) -> dict[str, str]:
    root = _xml(entries, "xl/workbook.xml")
    if root is None:
        return {}
    rels = _relationships(entries, "xl/workbook.xml")
    out: dict[str, str] = {}
    for sheet in root.findall(f"{_X}sheets/{_X}sheet"):
        name, rel_id = sheet.get("name"), sheet.get(f"{_R}id")
        if name and rel_id in rels:
            out[name] = rels[rel_id]
    return out


def _marker(anchor: ET.Element, tag: str) -> tuple[int, int, int, int] | None:
    node = anchor.find(f"{_XDR}{tag}")
    if node is None:
        return None
    values = []
    for name in ("col", "row", "colOff", "rowOff"):
        child = node.find(f"{_XDR}{name}")
        try:
            values.append(int(child.text or 0) if child is not None else 0)
        except ValueError:
            return None
    return tuple(values)  # type: ignore[return-value]


def _xlsx_anchor(anchor: ET.Element) -> tuple[str | None, dict[str, Any]]:
    start, end = _marker(anchor, "from"), _marker(anchor, "to")
    kind = PurePosixPath(anchor.tag).name.split("}")[-1]
    extension: dict[str, Any] = {"anchor_type": kind, "from_marker": start, "to_marker": end}
    if kind == "twoCellAnchor" and start is not None and end is not None:
        # `to`は次のセル境界を指すため、range終端は0-based marker値をそのまま1-based座標として使う。
        return f"{_a1(start[1] + 1, start[0] + 1)}:{_a1(max(end[1], 1), max(end[0], 1))}", extension
    if kind == "oneCellAnchor" and start is not None:
        return _a1(start[1] + 1, start[0] + 1), extension
    return None, extension


def _xlsx_embedded_anchor(node: ET.Element) -> tuple[str | None, dict[str, Any]]:
    """oleObject/objectPr内のnamespace差を許容してfrom/to cell anchorを読む。"""
    markers: dict[str, tuple[int, int] | None] = {"from": None, "to": None}
    for marker in node.iter():
        local = marker.tag.split("}")[-1]
        if local not in markers:
            continue
        values: dict[str, int] = {}
        for child in list(marker):
            child_local = child.tag.split("}")[-1]
            if child_local not in {"col", "row"}:
                continue
            try:
                values[child_local] = int(child.text or 0)
            except ValueError:
                continue
        if "col" in values and "row" in values:
            markers[local] = (values["col"], values["row"])
    start, end = markers["from"], markers["to"]
    if start is None:
        return None, {"from_cell": None, "to_cell": None}
    start_cell = _a1(start[1] + 1, start[0] + 1)
    end_cell = _a1(end[1] + 1, end[0] + 1) if end is not None else start_cell
    return f"{start_cell}:{end_cell}" if end_cell != start_cell else start_cell, {
        "from_cell": start,
        "to_cell": end,
    }


def _cell_rect(cell_range: str | None) -> list[float] | None:
    if not cell_range:
        return None
    from openpyxl.utils.cell import range_boundaries
    try:
        min_col, min_row, max_col, max_row = range_boundaries(cell_range)
    except ValueError:
        return None
    if not all(isinstance(value, int) for value in (min_col, min_row, max_col, max_row)):
        return None
    # Excel locatorの幾何は列/行境界を0-basedの正規化cell座標で表す。
    return [float(min_col - 1), float(min_row - 1), float(max_col), float(max_row)]


def _area(rect: list[int | float]) -> float:
    return max(0.0, float(rect[2] - rect[0])) * max(0.0, float(rect[3] - rect[1]))


def _drawing_kind(node: ET.Element) -> str | None:
    local = node.tag.split("}")[-1]
    if local == "sp":
        props = node.find(f"{_XDR}nvSpPr/{_XDR}cNvSpPr")
        return "textbox" if props is not None and props.get("txBox") == "1" else "shape"
    return {
        "pic": "picture",
        "cxnSp": "connector",
        "grpSp": "group",
        "graphicFrame": "graphic_frame",
    }.get(local)


def _occlusion_ratio() -> float:
    """覆い判定のしきい値（交差面積 ÷ 対象面積・0.0-1.0）。定義は `office_md._OCCLUSION_RATIO` の 1 箇所。

    pptx と判定をそろえるため値を複製せず、呼び出しのたびに読む。循環 import を避けるため import は関数内で行う。
    """
    from .. import office_md
    return office_md._OCCLUSION_RATIO


def _drawing_front_fill(
    node: ET.Element,
    kind: str,
    *,
    ns: str,
    text: str,
    extension: dict[str, Any],
    entries: dict[str, bytes],
) -> str | None:
    """セル・段落の上に重なる図形・画像の塗りの区分（`observation_vocab.FILL_*`）。図形・テキストボックス・画像以外は None。

    画像は `extension["image_references"]`（`_drawing_asset`）で解決した埋め込み画像の透過の有無で決める（読めない・外部参照は unknown）。
    """
    if kind in ("shape", "textbox"):
        return drawing_fill.shape_fill_kind(
            node.find(f"{ns}spPr"), node.find(f"{ns}style"), has_text=bool(text.strip()))
    if kind != "picture":
        return None
    references = {
        reference.get("relationship_id"): reference
        for reference in extension.get("image_references") or [] if isinstance(reference, dict)
    }

    def image_kind_of(embed: str | None, _link: str | None) -> str:
        reference = references.get(embed) if embed else None
        if reference is None or reference.get("binding_status") != "resolved":
            return observation_vocab.FILL_UNKNOWN
        return image_alpha.image_fill_kind(entries.get(reference.get("media_part")))

    return drawing_fill.picture_fill_kind(node, image_kind_of)


def _drawing_covers_bbox(sp_pr: ET.Element | None, kind: str) -> bool:
    """図形・画像（`kind`）の `spPr` から、覆う範囲を外接矩形と同一視してよいか（`drawing_fill.covers_bounding_box`）。

    xlsx・docx の図形はレイアウトから形状を受け継がないので、形状の指定が無い図形は False・画像は True。
    """
    return drawing_fill.covers_bounding_box(sp_pr, picture=kind == "picture")


def _c_nv_pr(node: ET.Element, prefix: str, kind: str) -> ET.Element | None:
    paths = {
        "textbox": f"{prefix}nvSpPr/{prefix}cNvPr",
        "shape": f"{prefix}nvSpPr/{prefix}cNvPr",
        "picture": f"{prefix}nvPicPr/{prefix}cNvPr",
        "connector": f"{prefix}nvCxnSpPr/{prefix}cNvPr",
        "group": f"{prefix}nvGrpSpPr/{prefix}cNvPr",
        "graphic_frame": f"{prefix}nvGraphicFramePr/{prefix}cNvPr",
    }
    return node.find(paths[kind])


def _chart_payload(entries: dict[str, bytes], chart_part: str) -> tuple[str | None, dict[str, Any]]:
    """chart XMLの文字・参照式・cached値だけを推測せず共通抽出する。"""
    root = _xml(entries, chart_part)
    if root is None:
        return None, {}
    texts = list(dict.fromkeys(
        node.text for node in root.iter(f"{_A}t") if node.text and node.text.strip()
    ))
    references = list(dict.fromkeys(
        node.text for node in root.iter(f"{_C}f") if node.text and node.text.strip()
    ))
    cached_values = [
        node.text for node in root.iter(f"{_C}v") if node.text is not None
    ]
    lines = [
        *(f"グラフ内文字: {value}" for value in texts),
        *(f"参照範囲: {value}" for value in references),
        *(f"キャッシュ値: {value}" for value in cached_values),
    ]
    return ("\n".join(lines) or None), {
        "chart_texts": texts,
        "chart_references": references,
        "chart_cached_values": cached_values,
    }


def _xlsx_chart_payload(entries: dict[str, bytes], chart_part: str) -> tuple[str | None, dict[str, Any]]:
    """旧private名を共通chart抽出へ委譲する。"""
    return _chart_payload(entries, chart_part)


def _add_unparsed_notice(
    builder: _Builder,
    locator: evidence_ir.Locator,
    *,
    detected_kind: str,
    reason_code: str,
    detail: dict[str, Any] | None = None,
) -> None:
    builder.add_coverage(
        "region",
        locator,
        "unsupported",
        detected_kind=detected_kind,
        content_basis="structured",
        reason_code=reason_code,
        detail=detail or {},
    )


def _drawing_asset(
    entries: dict[str, bytes],
    host_part: str,
    node: ET.Element,
    *,
    role: str,
) -> dict[str, Any]:
    """pictureとshape fillの全blip参照を、外部参照も黙って捨てずasset契約へ写す。"""
    relationship_records = _image_relationship_records(entries, host_part)
    references: list[dict[str, Any]] = []
    relationship_ids: list[str] = []
    unresolved: list[str] = []
    external: list[str] = []
    for blip in node.findall(f".//{_A}blip"):
        identifiers = [
            (attribute, relationship_id)
            for attribute in ("embed", "link")
            if isinstance((relationship_id := blip.get(f"{_R}{attribute}")), str) and relationship_id
        ]
        if not identifiers:
            references.append({
                "asset_role": role,
                "relationship_attribute": "missing",
                "binding_status": "relationship_id_missing",
            })
            unresolved.append("missing_relationship_id")
            continue
        for attribute, relationship_id in identifiers:
            if relationship_id not in relationship_ids:
                relationship_ids.append(relationship_id)
            reference: dict[str, Any] = {
                "relationship_id": relationship_id,
                "relationship_attribute": attribute,
                "asset_role": role,
            }
            relationship = relationship_records.get(relationship_id)
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
            if reference not in references:
                references.append(reference)
    if not references:
        return {}
    resolved = [reference for reference in references if reference.get("binding_status") == "resolved"]
    primary = resolved[0] if resolved else references[0]
    extension: dict[str, Any] = {
        **primary,
        "asset_role": role,
        "image_relationship_ids": relationship_ids,
        "image_references": references,
        # extract_assets/routeは参照ごとの全候補を扱う。失敗候補もhash無しのまま残す。
        "assets": references,
    }
    if unresolved:
        extension["unresolved_image_relationship_ids"] = list(dict.fromkeys(unresolved))
    if external:
        extension["external_image_relationship_ids"] = list(dict.fromkeys(external))
    return extension


def _add_raster_reference_coverage(
    builder: _Builder,
    locator: evidence_ir.Locator,
    extension: dict[str, Any],
    *,
    failed_kind: str,
    external_kind: str,
) -> None:
    """解決不能/外部画像参照をCoverage noticeへ安全に搬送する。"""
    references = extension.get("image_references")
    if not isinstance(references, list):
        return
    failed = [
        reference for reference in references
        if isinstance(reference, dict)
        and reference.get("binding_status") in {"relationship_id_missing", "relationship_missing", "target_missing"}
    ]
    external = [
        reference for reference in references
        if isinstance(reference, dict) and reference.get("binding_status") == "external_reference"
    ]
    if failed:
        builder.add_coverage(
            "object",
            locator,
            "failed",
            detected_kind=failed_kind,
            content_basis="pixel_only",
            reason_code="asset_binding_failed",
            detail={
                "relationship_ids": [
                    reference.get("relationship_id") or "missing_relationship_id"
                    for reference in failed
                ],
                "references": failed,
            },
        )
    if external:
        builder.add_coverage(
            "object",
            locator,
            "unsupported",
            detected_kind=external_kind,
            content_basis="pixel_only",
            reason_code="external_image_reference_not_fetched",
            detail={"references": external},
        )


def _smartart_payload(
    entries: dict[str, bytes], host_part: str, node: ET.Element,
) -> tuple[str | None, dict[str, Any], set[str]]:
    rels = _relationships(entries, host_part)
    relationship_ids: dict[str, str] = {}
    targets: dict[str, str] = {}
    for rel_ids in node.findall(f".//{_DGM}relIds"):
        for role in ("dm", "lo", "qs", "cs"):
            relationship_id = rel_ids.get(f"{_R}{role}")
            if not relationship_id:
                continue
            relationship_ids[role] = relationship_id
            target = rels.get(relationship_id)
            if target in entries:
                targets[role] = target
    data_part = targets.get("dm")
    texts = []
    if data_part:
        data = _xml(entries, data_part)
        if data is not None:
            texts = list(dict.fromkeys(
                text.text for text in data.iter(f"{_A}t") if text.text and text.text.strip()
            ))
    value = "\n".join(f"SmartArt内文字: {text}" for text in texts) or None
    return value, {
        "relationship_ids": relationship_ids,
        "diagram_parts": targets,
        "diagram_texts": texts,
        "semantic_graph_status": "unsupported",
    }, {data_part} if data_part else set()


def _ole_metadata(
    entries: dict[str, bytes], host_part: str, relationship_id: str, node: ET.Element,
) -> dict[str, Any]:
    relation = _relationship_records(entries, host_part).get(relationship_id, {})
    embedded_part = relation.get("target")
    extension: dict[str, Any] = {
        "relationship_id": relationship_id,
        "relationship_type": relation.get("type"),
        "embedded_part": embedded_part,
        "prog_id": node.get("ProgID") or node.get("progId"),
        "object_id": node.get("ObjectID") or node.get("spid"),
        "embedded_content_status": "unsupported",
    }
    if embedded_part in entries:
        extension.update({
            "binary_sha256": hashlib.sha256(entries[embedded_part]).hexdigest(),
            "content_type": _content_type(entries, embedded_part),
            "binary_size": len(entries[embedded_part]),
        })
    return extension


def _add_chart_data(
    builder: _Builder,
    entries: dict[str, bytes],
    *,
    chart_part: str,
    parent_id: str,
    object_token: str,
    sheet: str | None = None,
    slide: int | None = None,
) -> None:
    locator = evidence_ir.Locator(
        part=chart_part,
        sheet=sheet,
        slide=slide,
        object_id=f"chart-data:{object_token}",
    )
    if _xml(entries, chart_part) is None:
        builder.add_coverage(
            "object",
            locator,
            "failed",
            detected_kind="chart_data",
            content_basis="structured",
            reason_code="parse_failed",
            detail={"chart_part": chart_part, "error_type": "ParseError"},
        )
        return
    value, extension = _chart_payload(entries, chart_part)
    builder.add_element(
        "chart_data",
        locator,
        parent_id=parent_id,
        order=1,
        value=value,
        extension={"chart_part": chart_part, **extension},
    )
    _add_unparsed_notice(
        builder,
        locator,
        detected_kind="chart_unparsed_attributes",
        reason_code="unsupported_chart_attributes",
        detail={"chart_part": chart_part},
    )


def _add_smartart_data(
    builder: _Builder,
    entries: dict[str, bytes],
    *,
    parent_id: str,
    value: str | None,
    extension: dict[str, Any],
    host_part: str,
    object_token: str,
    sheet: str | None = None,
    slide: int | None = None,
) -> None:
    data_part = extension.get("diagram_parts", {}).get("dm")
    locator = evidence_ir.Locator(
        part=data_part or host_part,
        sheet=sheet,
        slide=slide,
        object_id=f"smartart-data:{object_token}",
    )
    if not data_part:
        builder.add_coverage(
            "object",
            locator,
            "unsupported",
            detected_kind="smartart_data",
            content_basis="structured",
            reason_code="smartart_data_part_missing",
            detail={"host_part": host_part},
        )
        return
    if _xml(entries, data_part) is None:
        builder.add_coverage(
            "object",
            locator,
            "failed",
            detected_kind="smartart_data",
            content_basis="structured",
            reason_code="parse_failed",
            detail={"diagram_data_part": data_part, "error_type": "ParseError"},
        )
        return
    builder.add_element(
        "smartart_data",
        locator,
        parent_id=parent_id,
        order=1,
        value=value,
        extension=extension,
    )
    _add_unparsed_notice(
        builder,
        locator,
        detected_kind="smartart_semantic_graph",
        reason_code="unsupported_smartart_semantics",
        detail={"diagram_data_part": data_part},
    )


def _integer(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _image_pixel_size(data: bytes) -> list[int] | None:
    """任意ライブラリに依存せず、OOXMLで頻出するPNG/GIF/JPEGのピクセル寸法を読む。"""
    if (png_size := child_png_size(data)) is not None:
        return png_size
    if len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        return [int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")]
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            if marker in {0xD8, 0xD9}:
                offset += 2
                continue
            if offset + 4 > len(data):
                break
            length = int.from_bytes(data[offset + 2:offset + 4], "big")
            if length < 2 or offset + 2 + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height = int.from_bytes(data[offset + 5:offset + 7], "big")
                width = int.from_bytes(data[offset + 7:offset + 9], "big")
                return [width, height]
            offset += 2 + length
    return None


def _connection(node: ET.Element | None) -> int | None:
    return _integer(node.get("id")) if node is not None else None


def _prst_geom(node: ET.Element, prefix: str, kind: str) -> str | None:
    """DrawingML の `prstGeom/@prst`（プリセット図形種）を pptx/xlsx 共通で取り出す。未知の値もそのまま保持する。"""
    if kind not in {"shape", "textbox", "connector"}:
        return None
    sp_pr = node.find(f"{prefix}spPr")
    if sp_pr is None:
        return None
    geom = sp_pr.find(f"{_A}prstGeom")
    return geom.get("prst") if geom is not None else None


def _bbox_intersection(a: list[int | float], b: list[int | float]) -> list[int | float] | None:
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return [x0, y0, x1, y1] if x1 > x0 and y1 > y0 else None
