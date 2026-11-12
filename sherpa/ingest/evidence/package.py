"""OOXML package の関係・メタデータ・未解釈 part の被覆。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from .. import evidence_ir
from ..ooxml.rels import load_relationships, resolve_target
from .base import _CT
from .builder import _Builder


_COLUMN_LETTERS: dict[int, str] = {}


def _column_letter(column: int) -> str:
    letters = _COLUMN_LETTERS.get(column)
    if letters is None:
        from openpyxl.utils import get_column_letter
        letters = _COLUMN_LETTERS[column] = get_column_letter(column)
    return letters


def _a1(row: int, column: int) -> str:
    return f"{_column_letter(column)}{row}"


def _relationships(entries: dict[str, bytes], part: str) -> dict[str, str]:
    return {
        rel.id: resolve_target(part, rel.target)
        for rel in load_relationships(entries.__getitem__, part)
        if rel.id and rel.target and rel.mode != "External"
    }


def _relationship_records(entries: dict[str, bytes], part: str) -> dict[str, dict[str, str]]:
    """relationshipのtargetだけでなくtypeも失わずに返す。外部targetは原本内assetとして扱わない。"""
    return {
        rel.id: {"target": resolve_target(part, rel.target), "type": rel.type or ""}
        for rel in load_relationships(entries.__getitem__, part)
        if rel.id and rel.target and rel.mode != "External"
    }


def _image_relationship_records(entries: dict[str, bytes], part: str) -> dict[str, dict[str, str]]:
    """画像relationshipを、外部targetの生値を保持せずに列挙する。"""
    records: dict[str, dict[str, str]] = {}
    for rel in load_relationships(entries.__getitem__, part):
        if not rel.id or not rel.target:
            continue
        target_mode = rel.mode if rel.mode is not None else "Internal"
        record = {
            "relationship_type": rel.type or "",
            "target_mode": target_mode,
        }
        if target_mode.casefold() == "external":
            record["external_target_sha256"] = "sha256:" + hashlib.sha256(rel.target.encode("utf-8")).hexdigest()
        else:
            record["media_part"] = resolve_target(part, rel.target)
        records[rel.id] = record
    return records


def _content_type(entries: dict[str, bytes], part: str) -> str | None:
    root = _xml(entries, "[Content_Types].xml")
    if root is None:
        return None
    overrides = {
        node.get("PartName", "").lstrip("/"): node.get("ContentType", "")
        for node in root.findall(f"{_CT}Override")
    }
    if part in overrides:
        return overrides[part] or None
    defaults = {
        node.get("Extension", "").lower(): node.get("ContentType", "")
        for node in root.findall(f"{_CT}Default")
    }
    return defaults.get(PurePosixPath(part).suffix.lower().lstrip(".")) or None


def _package_entries(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist() if not info.is_dir()}


_DOCUMENT_PROPERTY_NAMES = frozenset({
    "AppVersion", "Application", "Characters", "CharactersWithSpaces", "Company", "DocSecurity",
    "HiddenSlides", "HyperlinksChanged", "Lines", "LinksUpToDate", "Manager", "MMClips", "Notes",
    "Pages", "Paragraphs", "PresentationFormat", "ScaleCrop", "SharedDoc", "Slides", "Template",
    "TotalTime", "Words", "category", "contentStatus", "created", "creator", "description",
    "identifier", "keywords", "language", "lastModifiedBy", "lastPrinted", "modified", "revision",
    "subject", "title", "version",
})


_DOCUMENT_PROPERTY_CANONICAL_NAMES = {
    "Application": "application",
    "Company": "company",
    "creator": "author",
}


def _package_metadata(entries: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    """標準docPropsの原値を検索本文へ水増しせずpart単位のcoverage metadataへ写す。"""
    metadata: dict[str, dict[str, Any]] = {}
    for part in ("docProps/core.xml", "docProps/app.xml"):
        root = _xml(entries, part)
        if root is None:
            continue
        properties: list[dict[str, Any]] = []
        for node in root.iter():
            name = node.tag.split("}")[-1]
            if name not in _DOCUMENT_PROPERTY_NAMES or node.text is None:
                continue
            value = node.text
            if not value.strip():
                continue
            properties.append({
                "ordinal": len(properties) + 1,
                "xml_tag": node.tag,
                "native_property_name": name,
                "property_name": _DOCUMENT_PROPERTY_CANONICAL_NAMES.get(name, name),
                "raw_value": value,
            })
        metadata[part] = {
            "metadata_kind": "standard_document_properties",
            "part_sha256": hashlib.sha256(entries[part]).hexdigest(),
            "part_size": len(entries[part]),
            "properties": properties,
        }
    return metadata


_PACKAGE_CLASSIFIER_ID = "ooxml-package-classifier-v2"
_IMAGE_PART_SUFFIXES = frozenset({
    ".apng", ".avif", ".bmp", ".emf", ".gif", ".heic", ".jpeg", ".jpg", ".png",
    ".svg", ".tif", ".tiff", ".webp", ".wmf",
})


_PACKAGE_SUPPORT_EXACT = frozenset({
    "[Content_Types].xml",
    "xl/calcChain.xml",
    "xl/styles.xml",
    "word/fontTable.xml",
    "word/numbering.xml",
    "word/settings.xml",
    "word/styles.xml",
    "word/stylesWithEffects.xml",
    "word/webSettings.xml",
    "ppt/presProps.xml",
    "ppt/tableStyles.xml",
    "ppt/viewProps.xml",
})


_PACKAGE_SUPPORT_PREFIXES = (
    "ppt/printersettings/",
    "xl/printersettings/",
    "xl/theme/",
    "word/theme/",
    "ppt/theme/",
)


def _is_xml_part(part: str) -> bool:
    lower = part.lower()
    return lower.endswith((".xml", ".rels", ".vml"))


def _package_part_kind(part: str) -> str:
    """pathだけから、抽出成否とは独立したpart種別を決定する。"""
    lower = part.lower()
    suffix = PurePosixPath(lower).suffix
    if lower == "[content_types].xml":
        return "package_content_types"
    if lower.endswith(".rels"):
        return "package_relationships"
    if lower.startswith("docprops/"):
        return "package_thumbnail" if suffix in _IMAGE_PART_SUFFIXES else "package_metadata"
    if "/media/" in lower:
        return "image_asset" if suffix in _IMAGE_PART_SUFFIXES else "binary_asset"
    if "/diagrams/" in lower:
        return "smartart_part"
    if "/charts/" in lower:
        return "chart_part"
    if "/embeddings/" in lower or "oleobject" in lower:
        return "ole_embedding"
    if lower.endswith(".vml") or "vmldrawing" in lower:
        return "vml_part"
    if lower.startswith("customxml/"):
        return "custom_xml_part"
    if part in _PACKAGE_SUPPORT_EXACT or lower.startswith(_PACKAGE_SUPPORT_PREFIXES):
        return "package_support_part"
    return "xml_content_part" if _is_xml_part(part) else "binary_content_part"


def _is_empty_office_custom_xml(entries: dict[str, bytes], part: str) -> bool:
    if PurePosixPath(part).name.lower().startswith("itemprops"):
        return True
    root = _xml(entries, part)
    if root is None or root.tag.split("}")[-1] != "Sources":
        return False
    return not any(child.tag.split("}")[-1] == "Source" for child in root)


def _package_coverage(
    builder: _Builder,
    entries: dict[str, bytes],
    content_parts: set[str],
    *,
    part_details: dict[str, dict[str, Any]] | None = None,
) -> None:
    """ZIP内の全partを狭いallowlistとcontent-bearing分類へ振り分ける。

    ``content_parts``または要素locatorで実際に扱ったpartだけをextractedとする。未知partや
    SmartArt/OLE/VML等を補助part扱いにせず、明示的なunsupportedとして残す。
    """
    represented_parts = {element.locator.part for element in builder.ir.elements}
    extracted_parts = content_parts | represented_parts
    for part in sorted(entries):
        locator = evidence_ir.Locator(part=part)
        detected_kind = _package_part_kind(part)
        common = {
            "detected_kind": detected_kind,
            "parser_id": _PACKAGE_CLASSIFIER_ID,
            "detail": {"part": part, **(part_details or {}).get(part, {})},
        }
        if _is_xml_part(part):
            try:
                ET.fromstring(entries[part])
            except ET.ParseError as exc:
                builder.add_coverage(
                    "part",
                    locator,
                    "failed",
                    detected_kind=detected_kind,
                    content_basis="structured",
                    reason_code="parse_failed",
                    parser_id=_PACKAGE_CLASSIFIER_ID,
                    detail={"part": part, "error_type": exc.__class__.__name__},
                )
                continue
        if part in extracted_parts:
            builder.add_coverage(
                "part", locator, "extracted", content_basis="structured",
                reason_code="content_extracted", **common,
            )
        elif detected_kind == "image_asset":
            builder.add_coverage(
                "part", locator, "metadata_only", content_basis="pixel_only",
                reason_code="binary_asset_content_uninterpreted", **common,
            )
        elif detected_kind == "package_thumbnail":
            builder.add_coverage(
                "part", locator, "intentionally_ignored", content_basis="none",
                reason_code="package_preview_thumbnail", **common,
            )
        elif detected_kind == "binary_asset":
            builder.add_coverage(
                "part", locator, "metadata_only", content_basis="binary_only",
                reason_code="binary_asset_content_uninterpreted", **common,
            )
        elif detected_kind == "package_metadata":
            builder.add_coverage(
                "part", locator, "metadata_only", content_basis="structured",
                reason_code="package_metadata_uninterpreted", **common,
            )
        elif detected_kind == "custom_xml_part" and _is_empty_office_custom_xml(entries, part):
            builder.add_coverage(
                "part", locator, "intentionally_ignored", content_basis="none",
                reason_code="empty_office_custom_xml_support", **common,
            )
        elif detected_kind in {"package_content_types", "package_relationships", "package_support_part"}:
            builder.add_coverage(
                "part", locator, "intentionally_ignored", content_basis="none",
                reason_code="package_support_part", **common,
            )
        else:
            reason_code = {
                "smartart_part": "unsupported_smartart",
                "chart_part": "unsupported_chart",
                "ole_embedding": "unsupported_ole",
                "vml_part": "unsupported_vml",
                "custom_xml_part": "unsupported_custom_xml",
                "xml_content_part": "unsupported_content_part",
                "binary_content_part": "unsupported_binary_part",
            }[detected_kind]
            builder.add_coverage(
                "part",
                locator,
                "unsupported",
                content_basis="binary_only" if detected_kind in {"ole_embedding", "binary_content_part"} else "structured",
                reason_code=reason_code,
                **common,
            )


def _xml(entries: dict[str, bytes], part: str) -> ET.Element | None:
    try:
        return ET.fromstring(entries[part])
    except (KeyError, ET.ParseError):
        return None
