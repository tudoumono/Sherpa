"""`extract` / `extract_assets`（Evidence IR の入口）。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from typing import Any

from ... import json_io
from .. import document_ir, evidence_ir
from ..gc_relax import relaxed_gc
from .adapt import _adapt_document_ir, _annotate_state_basis
from .base import _source
from .builder import _Builder
from .docx import _docx_objects
from .package import _package_coverage, _package_entries, _package_metadata
from .pdf import _pdf_objects
from .pdf_xobject import _pdf_image_payload, _pdf_xobject_at_path
from .pptx import _pptx_objects
from .xlsx import _xlsx_objects


def _legacy_ir(path: Path) -> document_ir.DocumentIR | None:
    from ..arms import ooxml_arm

    builders = {
        ".docx": ooxml_arm._build_docx_ir,
        ".pptx": ooxml_arm._build_pptx_ir,
        ".xlsx": ooxml_arm._build_xlsx_ir,
    }
    builder = builders.get(path.suffix.lower())
    return builder(path) if builder is not None else None


@relaxed_gc
def extract(
    path: str | Path,
    *,
    legacy_ir: document_ir.DocumentIR | None = None,
    consume_legacy: bool = False,
) -> evidence_ir.EvidenceIR:
    """XLSX/DOCX/PPTX/PDFを共通Evidence IRへ通す。外部通信・LLMは使用しない。"""
    source_path = Path(path)
    suffix = source_path.suffix.lower()
    if suffix not in {".xlsx", ".docx", ".pptx", ".pdf"}:
        raise ValueError(f"unsupported Evidence IR spike input: {suffix}")
    builder = _Builder(_source(source_path))
    if suffix == ".pdf":
        _pdf_objects(builder, source_path)
    else:
        _adapt_document_ir(
            builder, legacy_ir if legacy_ir is not None else _legacy_ir(source_path), consume_legacy=consume_legacy)
        entries = _package_entries(source_path)
        package_metadata = _package_metadata(entries)
        if suffix == ".xlsx":
            content_parts = _xlsx_objects(builder, entries)
        elif suffix == ".docx":
            content_parts = _docx_objects(builder, entries)
        else:
            content_parts = _pptx_objects(builder, entries)
        content_parts.update(package_metadata)
        _package_coverage(builder, entries, content_parts, part_details=package_metadata)
        _annotate_state_basis(builder.ir)
    errors = evidence_ir.validation_errors(builder.ir)
    if errors:
        raise ValueError("invalid Evidence IR spike: " + ",".join(errors))
    return builder.ir


def extract_assets(
    path: str | Path,
    ir: evidence_ir.EvidenceIR,
    destination: str | Path,
    *,
    skipped: list[dict[str, Any]] | None = None,
) -> list[Path]:
    """OOXML/PDF内のEvidence画像をcontent hash名でgeneration stageへ取り出す。

    Evidence IRに記録済みの``media_part``だけを対象とし、書込前にbytesのSHA-256を再照合する。
    destinationは非公開stage内を呼び出し側が指定するため、ここでは同名hash assetを決定的に上書きする。
    PDFはpypdfが列挙したpage image bytesを同じhashで再照合する。
    PDFで取り出せない・照合が合わない画像は、その1枚だけ外して``skipped``へ``{page, name, reason}``を積む
    （文書全体は止めない）。
    """
    source_path = Path(path)
    if source_path.suffix.lower() not in {".xlsx", ".docx", ".pptx", ".pdf"}:
        return []
    if source_path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        requested = [
            element for element in ir.elements
            if element.type == "image_xobject" and isinstance(element.extension.get("asset_sha256"), str)
        ]
        if not requested:
            return []
        target_dir = Path(destination)
        target_dir.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}
        reader = PdfReader(source_path)
        for element in requested:
            expected_digest = str(element.extension["asset_sha256"])
            page_number = element.locator.page
            xobject_path = element.extension.get("xobject_path")
            reason = None
            raw = b""
            if not isinstance(page_number, int) or not isinstance(xobject_path, list) or not xobject_path:
                reason = "locator_incomplete"
            else:
                try:
                    xobject = _pdf_xobject_at_path(reader.pages[page_number - 1], xobject_path)
                    _image_name, raw, _decoded_image = _pdf_image_payload(xobject, xobject_path[-1])
                except (IndexError, KeyError):
                    reason = "locator_unresolved"
                except Exception:
                    reason = "decode_failed"
            if reason is None and hashlib.sha256(raw).hexdigest() != expected_digest:
                reason = "hash_mismatch"
            if reason is not None:
                if skipped is None:
                    raise ValueError(f"PDF asset {reason}")
                skipped.append({
                    "page": page_number,
                    "name": "/".join(str(name) for name in xobject_path) if isinstance(xobject_path, list) else "",
                    "reason": reason,
                })
                continue
            suffix = element.extension.get("asset_extension", ".bin")
            suffix = suffix if isinstance(suffix, str) and suffix.startswith(".") else ".bin"
            target = target_dir / f"{expected_digest}{suffix}"
            if expected_digest not in written:
                json_io.write_bytes_atomic(target, raw)
                written[expected_digest] = target
        return [written[digest] for digest in sorted(written)]
    requested: dict[str, str] = {}
    for element in ir.elements:
        candidates = element.extension.get("assets")
        if not isinstance(candidates, list):
            candidates = [element.extension]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            media_part = candidate.get("media_part")
            digest = candidate.get("asset_sha256")
            if isinstance(media_part, str) and isinstance(digest, str):
                requested[media_part] = digest
    if not requested:
        return []
    target_dir = Path(destination)
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(source_path) as package:
        for media_part, expected_digest in sorted(requested.items()):
            raw = package.read(media_part)
            actual_digest = hashlib.sha256(raw).hexdigest()
            if actual_digest != expected_digest:
                raise ValueError(f"asset hash mismatch: {media_part}")
            suffix = Path(media_part).suffix.lower() or ".bin"
            target = target_dir / f"{actual_digest}{suffix}"
            json_io.write_bytes_atomic(target, raw)
            written.append(target)
    return written
