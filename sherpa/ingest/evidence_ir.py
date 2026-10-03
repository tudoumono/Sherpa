"""Canonical Evidence IR の最小共通契約。

原本要素を自然文へ畳み込まず、値・原本位置・親子・関係・coverage を分けて保持する。
XLSX/DOCX/PPTX/PDF で共通の field だけを core に置き、形式固有の anchor・座標系は
``Locator.extension``/``EvidenceElement.extension`` へ名前空間を保って残す。
資料フォルダ名や絶対パスは含めず、同じ原本 bytes と parser profile から同じ byte 列を生成する。
設計: docs/design/rag.md「OCR（非同期・隔離ワーカー）」
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


EVIDENCE_IR_SCHEMA_VERSION = "evidence-ir-v1alpha2"
EVIDENCE_PARSER_PROFILE = "evidence-spike-v4"
COVERAGE_STATUSES = frozenset({
    "extracted",
    "metadata_only",
    "intentionally_ignored",
    "unsupported",
    "failed",
})
CONTENT_BASES = frozenset({
    "structured",
    "text_layer",
    "pixel_only",
    "binary_only",
    "none",
})

# ``intentionally_ignored`` は検索表現から意図的に外す唯一の coverage 状態。
# 許可する (検知種別, 理由) をスキーマ側でも固定し、内容を持つ part を補助扱いに偽装させない。
INTENTIONALLY_IGNORED_CONTRACT = frozenset({
    ("package_content_types", "package_support_part"),
    ("package_relationships", "package_support_part"),
    ("package_support_part", "package_support_part"),
    ("package_thumbnail", "package_preview_thumbnail"),
    ("custom_xml_part", "empty_office_custom_xml_support"),
})


@dataclass(frozen=True)
class EvidenceSource:
    """パス同一性から分離した原本 content の識別情報。"""

    file_type: str
    content_hash: str


@dataclass(frozen=True)
class Locator:
    """原本へ機械的に戻るための共通 locator と形式別 extension。"""

    part: str
    page: int | None = None
    slide: int | None = None
    sheet: str | None = None
    cell_range: str | None = None
    object_id: str | int | None = None
    bbox: list[int | float] | None = None
    extension: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CoverageItem:
    """検知した part/object/region を黙って落とさないための処理結果。"""

    coverage_id: str
    scope: str
    detected_kind: str
    locator: Locator
    status: str
    content_basis: str
    reason_code: str
    parser_id: str
    detail: dict[str, Any]

    @property
    def reason(self) -> str:
        """理由は reason_code が正。"""
        return self.reason_code


@dataclass(frozen=True)
class EvidenceElement:
    """原本から決定的に得た 1 要素。意味上の断定は relation/view 側で扱う。"""

    element_id: str
    type: str
    parent_id: str | None
    order: int
    value: Any
    locator: Locator
    coverage_id: str
    visibility: str = "visible"
    lifecycle: str = "active"
    extension: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceRelation:
    """要素間の観測可能な関係。推測した自然文は入れない。"""

    relation_id: str
    type: str
    source_id: str
    target_id: str
    evidence_ids: list[str]
    confidence: float
    extension: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvidenceIR:
    schema_version: str
    parser_profile: str
    source: EvidenceSource
    elements: list[EvidenceElement] = field(default_factory=list)
    relations: list[EvidenceRelation] = field(default_factory=list)
    coverage: list[CoverageItem] = field(default_factory=list)


def validation_errors(ir: EvidenceIR) -> list[str]:
    """参照整合性と coverage 全分類を検査する。"""
    errors: list[str] = []
    if ir.schema_version != EVIDENCE_IR_SCHEMA_VERSION:
        errors.append("schema_version")
    if not ir.parser_profile:
        errors.append("parser_profile")
    if not ir.source.content_hash.startswith("sha256:"):
        errors.append("source.content_hash")

    coverage_ids = [item.coverage_id for item in ir.coverage]
    if len(coverage_ids) != len(set(coverage_ids)):
        errors.append("duplicate_coverage_id")
        statuses_by_id: dict[str, set[str]] = {}
        for item in ir.coverage:
            statuses_by_id.setdefault(item.coverage_id, set()).add(item.status)
        if any(len(statuses) > 1 for statuses in statuses_by_id.values()):
            errors.append("coverage_status_conflict")
    coverage_by_id = {item.coverage_id: item for item in ir.coverage}
    for item in ir.coverage:
        if item.coverage_id != make_coverage_id(item.scope, item.detected_kind, item.locator):
            errors.append(f"coverage_id:{item.coverage_id}")
        if item.status not in COVERAGE_STATUSES:
            errors.append(f"coverage_status:{item.coverage_id}")
        if item.content_basis not in CONTENT_BASES:
            errors.append(f"coverage_content_basis:{item.coverage_id}")
        if not item.detected_kind:
            errors.append(f"coverage_detected_kind:{item.coverage_id}")
        if not item.reason_code:
            errors.append(f"coverage_reason_code:{item.coverage_id}")
        if not item.parser_id:
            errors.append(f"coverage_parser_id:{item.coverage_id}")
        if not isinstance(item.detail, dict):
            errors.append(f"coverage_detail:{item.coverage_id}")
        if not item.locator.part:
            errors.append(f"coverage_locator:{item.coverage_id}")
        if item.status == "intentionally_ignored":
            if item.content_basis != "none":
                errors.append(f"coverage_ignored_basis:{item.coverage_id}")
            if (item.detected_kind, item.reason_code) not in INTENTIONALLY_IGNORED_CONTRACT:
                errors.append(f"coverage_ignored_not_allowlisted:{item.coverage_id}")

    element_ids = [element.element_id for element in ir.elements]
    if len(element_ids) != len(set(element_ids)):
        errors.append("duplicate_element_id")
    element_set = set(element_ids)
    for element in ir.elements:
        if element.parent_id is not None and element.parent_id not in element_set:
            errors.append(f"parent_missing:{element.element_id}")
        if element.coverage_id not in coverage_by_id:
            errors.append(f"coverage_missing:{element.element_id}")
        if not element.locator.part:
            errors.append(f"element_locator:{element.element_id}")

    relation_ids = [relation.relation_id for relation in ir.relations]
    if len(relation_ids) != len(set(relation_ids)):
        errors.append("duplicate_relation_id")
    for relation in ir.relations:
        if relation.source_id not in element_set or relation.target_id not in element_set:
            errors.append(f"relation_endpoint:{relation.relation_id}")
        if not 0.0 <= relation.confidence <= 1.0:
            errors.append(f"relation_confidence:{relation.relation_id}")
        if not relation.evidence_ids or any(evidence_id not in element_set for evidence_id in relation.evidence_ids):
            errors.append(f"relation_evidence:{relation.relation_id}")
    return sorted(set(errors))


def to_json_str(ir: EvidenceIR) -> str:
    """検証済み IR を決定的 JSON へ直列化する。"""
    errors = validation_errors(ir)
    if errors:
        raise ValueError("invalid Evidence IR: " + ",".join(errors))
    return "".join(_json_parts(ir))


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_coverage_id(scope: str, detected_kind: str, locator: Locator) -> str:
    """scope・検知種別・原本位置から coverage ID を決定生成する。"""
    payload = "\n".join(_compact(value) for value in (scope, detected_kind, asdict(locator)))
    return "coverage:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _array_parts(values):
    yield "["
    for index, value in enumerate(values):
        if index:
            yield ","
        yield _compact(asdict(value))
    yield "]"


def _json_parts(ir: EvidenceIR):
    """sort_keys 相当の root 順で、巨大配列を全 dict 化せず決定的 JSON 片へ展開する。"""
    yield '{"coverage":'
    yield from _array_parts(ir.coverage)
    yield ',"elements":'
    yield from _array_parts(ir.elements)
    yield ',"parser_profile":'
    yield _compact(ir.parser_profile)
    yield ',"relations":'
    yield from _array_parts(ir.relations)
    yield ',"schema_version":'
    yield _compact(ir.schema_version)
    yield ',"source":'
    yield _compact(asdict(ir.source))
    yield "}\n"


def write_json_atomic(path: str | Path, ir: EvidenceIR) -> Path:
    """Evidence IR を同一ディレクトリの tmp へ stream して原子置換する。"""
    errors = validation_errors(ir)
    if errors:
        raise ValueError("invalid Evidence IR: " + ",".join(errors))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            stream.writelines(_json_parts(ir))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def from_json_str(raw: str) -> EvidenceIR:
    """決定的 JSON を型付き Evidence IR へ戻し、参照整合性も再検証する。"""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Evidence IR must be a JSON object")
    try:
        source_raw = payload["source"]
        ir = EvidenceIR(
            schema_version=payload["schema_version"],
            parser_profile=payload["parser_profile"],
            source=EvidenceSource(**source_raw),
            elements=[EvidenceElement(
                **{**item, "locator": Locator(**item["locator"])}) for item in payload.get("elements", [])],
            relations=[EvidenceRelation(**item) for item in payload.get("relations", [])],
            coverage=[CoverageItem(
                **{**item, "locator": Locator(**item["locator"])}) for item in payload.get("coverage", [])],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid Evidence IR shape") from exc
    errors = validation_errors(ir)
    if errors:
        raise ValueError("invalid Evidence IR: " + ",".join(errors))
    return ir
