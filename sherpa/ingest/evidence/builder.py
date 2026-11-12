"""Evidence IR を組み立てる `_Builder`。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

from dataclasses import replace as _dataclass_replace
from typing import Any

from .. import evidence_ir, observation_vocab
from .base import _element_id, _stable_id


class _Builder:
    def __init__(self, source: evidence_ir.EvidenceSource):
        self.ir = evidence_ir.EvidenceIR(
            schema_version=evidence_ir.EVIDENCE_IR_SCHEMA_VERSION,
            parser_profile=evidence_ir.EVIDENCE_PARSER_PROFILE,
            source=source,
        )
        self._coverage_by_id: dict[str, evidence_ir.CoverageItem] = {}
        self._element_coverage: dict[str, str] = {}
        self._element_index: dict[str, int] = {}
        self._covers_by_target: dict[str, list[evidence_ir.EvidenceRelation]] = {}

    def add_coverage(
        self,
        scope: str,
        locator: evidence_ir.Locator,
        status: str,
        reason: str | None = None,
        *,
        detected_kind: str,
        content_basis: str | None = None,
        reason_code: str | None = None,
        parser_id: str = evidence_ir.EVIDENCE_PARSER_PROFILE,
        detail: dict[str, Any] | None = None,
    ) -> str:
        reason_code = reason_code or reason or ("content_extracted" if status == "extracted" else "unspecified")
        if content_basis is None:
            if status == "intentionally_ignored":
                content_basis = "none"
            elif detected_kind in {"picture", "image_xobject", "image_asset", "package_thumbnail"}:
                content_basis = "pixel_only"
            elif detected_kind in {"text_object", "positioned_text"}:
                content_basis = "text_layer"
            else:
                content_basis = "structured"
        coverage_id = evidence_ir.make_coverage_id(scope, detected_kind, locator)
        item = evidence_ir.CoverageItem(
            coverage_id=coverage_id,
            scope=scope,
            detected_kind=detected_kind,
            locator=locator,
            status=status,
            content_basis=content_basis,
            reason_code=reason_code,
            parser_id=parser_id,
            detail=detail or {},
        )
        existing = self._coverage_by_id.get(coverage_id)
        if existing is not None and existing.status != status:
            raise ValueError(
                f"coverage status conflict: {coverage_id}: {existing.status} != {status}"
            )
        if existing is not None and existing != item:
            raise ValueError(f"coverage classification conflict: {coverage_id}")
        if existing is None:
            self._coverage_by_id[coverage_id] = item
            self.ir.coverage.append(item)
        return coverage_id

    def add_element(
        self,
        element_type: str,
        locator: evidence_ir.Locator,
        *,
        parent_id: str | None,
        order: int,
        value: Any,
        coverage_status: str = "extracted",
        coverage_reason: str | None = None,
        coverage_content_basis: str | None = None,
        coverage_detail: dict[str, Any] | None = None,
        coverage_id: str | None = None,
        visibility: str = "visible",
        lifecycle: str = "active",
        extension: dict[str, Any] | None = None,
    ) -> str:
        element_id = _element_id(element_type, locator, order)
        if coverage_id is None:
            coverage_id = self.add_coverage(
                "object",
                locator,
                coverage_status,
                coverage_reason,
                detected_kind=element_type,
                content_basis=coverage_content_basis,
                detail=coverage_detail,
            )
        self.ir.elements.append(evidence_ir.EvidenceElement(
            element_id=element_id,
            type=element_type,
            parent_id=parent_id,
            order=order,
            value=value,
            locator=locator,
            coverage_id=coverage_id,
            visibility=visibility,
            lifecycle=lifecycle,
            extension=extension or {},
        ))
        self._element_coverage[element_id] = coverage_id
        self._element_index[element_id] = len(self.ir.elements) - 1
        return element_id

    def coverage_for(self, element_id: str) -> str:
        return self._element_coverage[element_id]

    def element(self, element_id: str) -> evidence_ir.EvidenceElement:
        return self.ir.elements[self._element_index[element_id]]

    def set_state(self, element_id: str, *, visibility: str, extension: dict[str, Any]) -> None:
        """既存要素の `visibility` と `extension` を差し替える（要素追加後に幾何関係が判明する遮蔽の反映用）。

        `EvidenceElement` は frozen のため `dataclasses.replace` で差し替え、`value`/`locator`/`coverage_id` は変えない。
        """
        index = self._element_index[element_id]
        self.ir.elements[index] = _dataclass_replace(
            self.ir.elements[index], visibility=visibility, extension=extension)

    def covers_to(self, element_id: str) -> list[evidence_ir.EvidenceRelation]:
        """`element_id` を背面とする covers を、作った順に返す。"""
        return self._covers_by_target.get(element_id, [])

    def add_covers(
        self,
        front_id: str,
        back_id: str,
        *,
        fill: str,
        cover_ratio: float | None,
        coordinate_system: str,
        covers_bbox: bool = True,
    ) -> None:
        """前面 → 背面の `covers`（遮蔽の正本）。背面の状態は `_apply_cover_state` が導く。

        `covers_bbox` は前面の形が外接矩形と同一視できるか。False（ドーナツ・楕円・カスタム形状など）なら、
        不透明な塗りでも occlusion は undetermined になり、extension に `covers_bbox=False` を残す。
        """
        extension: dict[str, Any] = observation_vocab.relation_extension(
            observation_vocab.BASIS_GEOMETRY,
            observation_vocab.DIRECTION_UNDIRECTED,
            fill=fill,
            occlusion=observation_vocab.occlusion_of(fill, covers_bbox),
            coordinate_system=coordinate_system,
        )
        if not covers_bbox:
            extension["covers_bbox"] = False
        if cover_ratio is not None:
            extension["cover_ratio"] = cover_ratio
        self.add_relation(
            observation_vocab.RELATION_COVERS, front_id, back_id,
            evidence_ids=[front_id, back_id], extension=extension)

    def add_relation(
        self,
        relation_type: str,
        source_id: str,
        target_id: str,
        *,
        evidence_ids: list[str],
        confidence: float = 1.0,
        extension: dict[str, Any] | None = None,
    ) -> None:
        relation_id = _stable_id("relation", relation_type, source_id, target_id, extension or {})
        self.ir.relations.append(evidence_ir.EvidenceRelation(
            relation_id=relation_id,
            type=relation_type,
            source_id=source_id,
            target_id=target_id,
            evidence_ids=evidence_ids,
            confidence=confidence,
            extension=extension or {},
        ))
        if relation_type == observation_vocab.RELATION_COVERS:
            self._covers_by_target.setdefault(target_id, []).append(self.ir.relations[-1])
