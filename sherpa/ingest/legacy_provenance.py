"""旧Office前段変換のoriginal/normalized来歴を分離して保持する。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import evidence_ir


LEGACY_PROVENANCE_ADAPTER_VERSION = "legacy-provenance-adapter-v2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def build(original: str | Path, normalized: str | Path, notes: list[str]) -> dict[str, Any]:
    original_path, normalized_path = Path(original), Path(normalized)
    note_values: dict[str, str] = {}
    for note in notes:
        if isinstance(note, str) and "=" in note:
            key, value = note.split("=", 1)
            if key and value:
                note_values[key] = value
    backend = note_values.get("legacy_backend", "unknown")
    backend_version = {
        key: note_values[key]
        for key in ("soffice", "office_com_versions")
        if key in note_values
    }
    return {
        "original_content_hash": _sha256(original_path),
        "original_file_type": original_path.suffix.lower().lstrip("."),
        "normalized_content_hash": _sha256(normalized_path),
        "normalized_file_type": normalized_path.suffix.lower().lstrip("."),
        "backend": backend,
        "backend_version": backend_version,
        "fidelity_status": "not_verified",
        "locator_basis": "normalized_artifact",
    }


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def apply_to_evidence(ir: evidence_ir.EvidenceIR, provenance: dict[str, Any]) -> None:
    """正本の identity を原本へ戻し、locator が変換後のものであることを明示する。"""
    ir.source = evidence_ir.EvidenceSource(
        file_type=provenance["original_file_type"],
        content_hash=provenance["original_content_hash"],
    )
    # locator は変換後 OOXML 上の座標。ID 算出後に locator は変えず、basis は extension/detail に足す
    ir.elements = [replace(
        element,
        extension={**element.extension, "locator_basis": "normalized_artifact"},
    ) for element in ir.elements]
    ir.coverage = [replace(
        item,
        detail={**item.detail, "locator_basis": "normalized_artifact"},
    ) for item in ir.coverage]

    locator = evidence_ir.Locator(
        part="normalized-artifact",
        object_id="conversion-provenance",
        extension={"locator_basis": "normalized_artifact"},
    )
    coverage_id = evidence_ir.make_coverage_id("document", "legacy_conversion_provenance", locator)
    ir.coverage.append(evidence_ir.CoverageItem(
        coverage_id=coverage_id,
        scope="document",
        detected_kind="legacy_conversion_provenance",
        locator=locator,
        status="extracted",
        content_basis="structured",
        reason_code="conversion_provenance_recorded",
        parser_id=LEGACY_PROVENANCE_ADAPTER_VERSION,
        detail={"locator_basis": "normalized_artifact"},
    ))
    ir.elements.append(evidence_ir.EvidenceElement(
        element_id=_stable_id("evidence", "conversion_provenance", provenance),
        type="conversion_provenance",
        parent_id=None,
        order=0,
        value=provenance,
        locator=locator,
        coverage_id=coverage_id,
        extension={"locator_basis": "normalized_artifact"},
    ))
    errors = evidence_ir.validation_errors(ir)
    if errors:
        raise ValueError("invalid legacy Evidence provenance: " + ",".join(errors))


def build_unavailable_evidence(
    original: str | Path,
    *,
    status: str,
    reason_code: str,
    detected_kind: str = "legacy_office_binary",
    object_id: str = "legacy-office-source",
    detail: dict[str, Any] | None = None,
) -> evidence_ir.EvidenceIR:
    """内容抽出前に止まった原本を、原本 hash/type と binary_only の coverage だけの Evidence として残す。"""
    if status not in {"unsupported", "failed"}:
        raise ValueError("legacy unavailable Evidence status must be unsupported or failed")
    source = Path(original)
    locator = evidence_ir.Locator(
        part="source-file",
        object_id=object_id,
        extension={"locator_basis": "source_file"},
    )
    coverage_id = evidence_ir.make_coverage_id("document", detected_kind, locator)
    ir = evidence_ir.EvidenceIR(
        schema_version=evidence_ir.EVIDENCE_IR_SCHEMA_VERSION,
        parser_profile=evidence_ir.EVIDENCE_PARSER_PROFILE,
        source=evidence_ir.EvidenceSource(
            file_type=source.suffix.lower().lstrip("."),
            content_hash=_sha256(source),
        ),
        coverage=[evidence_ir.CoverageItem(
            coverage_id=coverage_id,
            scope="document",
            detected_kind=detected_kind,
            locator=locator,
            status=status,
            content_basis="binary_only",
            reason_code=reason_code,
            parser_id=LEGACY_PROVENANCE_ADAPTER_VERSION,
            detail={
                "locator_basis": "source_file",
                "original_file_type": source.suffix.lower().lstrip("."),
                **(detail or {}),
            },
        )],
    )
    errors = evidence_ir.validation_errors(ir)
    if errors:
        raise ValueError("invalid unavailable legacy Evidence: " + ",".join(errors))
    return ir
