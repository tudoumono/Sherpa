"""単体PNG/JPEGをOCRなしでCanonical Evidenceへ変換するadapter。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import evidence_ir


RASTER_ADAPTER_VERSION = "raster-evidence-adapter-v1"
SUPPORTED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _image_metadata(path: Path) -> dict[str, Any]:
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
        image_format = (image.format or "").upper()
        exif = image.getexif()
        orientation = exif.get(274) if exif else None
    with Image.open(path) as image:
        image.verify()
    if image_format == "PNG":
        media_type, asset_extension = "image/png", ".png"
    elif image_format in {"JPEG", "JPG"}:
        media_type, asset_extension = "image/jpeg", ".jpg"
    else:
        raise ValueError(f"unsupported raster format: {image_format or 'unknown'}")
    return {
        "media_type": media_type,
        "asset_extension": asset_extension,
        "pixel_size": [int(width), int(height)],
        "exif_orientation": int(orientation) if isinstance(orientation, int) else None,
    }


def extract(path: str | Path) -> evidence_ir.EvidenceIR:
    """画像の存在、全画像bbox、hash、MIME、向きだけを抽出する。画像内容は解釈しない。"""
    source = Path(path)
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"unsupported raster Evidence input: {source.suffix.lower()}")
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    metadata = _image_metadata(source)
    locator = evidence_ir.Locator(
        part="standalone-image",
        object_id="image:1",
        bbox=[0, 0, metadata["pixel_size"][0], metadata["pixel_size"][1]],
        extension={"coordinate_system": "pixel", "source_extension": source.suffix.lower()},
    )
    coverage_id = evidence_ir.make_coverage_id("object", "standalone_raster_image", locator)
    coverage = evidence_ir.CoverageItem(
        coverage_id=coverage_id,
        scope="object",
        detected_kind="standalone_raster_image",
        locator=locator,
        status="metadata_only",
        content_basis="pixel_only",
        reason_code="image_content_uninterpreted",
        parser_id=RASTER_ADAPTER_VERSION,
        detail={"media_type": metadata["media_type"], "pixel_size": metadata["pixel_size"]},
    )
    extension = {
        "name": source.name,
        "media_part": "standalone" + metadata["asset_extension"],
        "asset_sha256": digest,
        **metadata,
    }
    element = evidence_ir.EvidenceElement(
        element_id=_stable_id("evidence", "picture", digest, locator.part, locator.object_id),
        type="picture",
        parent_id=None,
        order=1,
        value=None,
        locator=locator,
        coverage_id=coverage_id,
        extension=extension,
    )
    ir = evidence_ir.EvidenceIR(
        schema_version=evidence_ir.EVIDENCE_IR_SCHEMA_VERSION,
        parser_profile=evidence_ir.EVIDENCE_PARSER_PROFILE,
        source=evidence_ir.EvidenceSource(
            file_type=source.suffix.lower().lstrip("."),
            content_hash="sha256:" + digest,
        ),
        elements=[element],
        coverage=[coverage],
    )
    errors = evidence_ir.validation_errors(ir)
    if errors:
        raise ValueError("invalid raster Evidence IR: " + ",".join(errors))
    return ir


def extract_assets(path: str | Path, ir: evidence_ir.EvidenceIR, destination: str | Path) -> list[Path]:
    """原画像をEvidence内hashと再照合し、content-addressed assetとして1回だけ保存する。"""
    source = Path(path)
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    pictures = [element for element in ir.elements if element.type == "picture"]
    if len(pictures) != 1 or pictures[0].extension.get("asset_sha256") != digest:
        raise ValueError("raster asset inventory mismatch")
    suffix = pictures[0].extension.get("asset_extension")
    if not isinstance(suffix, str) or suffix not in {".png", ".jpg"}:
        raise ValueError("invalid raster asset extension")
    target_dir = Path(destination)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{digest}{suffix}"
    target.write_bytes(data)
    return [target]
