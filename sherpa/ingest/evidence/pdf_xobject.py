"""PDF の画像 XObject の列挙と変換行列。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from .. import evidence_ir
from .base import PDF_ADAPTER_VERSION, PDF_XOBJECT_MAX_DEPTH, PDF_XOBJECT_MAX_RESOLUTIONS
from .builder import _Builder
from .drawing import _bbox_intersection


def _matrix_multiply(left: list[float], right: list[float]) -> list[float]:
    a, b, c, d, e, f = left
    g, h, i, j, k, l = right
    return [a * g + c * h, b * g + d * h, a * i + c * j, b * i + d * j,
            a * k + c * l + e, b * k + d * l + f]


def _matrix_bbox(matrix: list[float]) -> list[float]:
    a, b, c, d, e, f = matrix
    points = [(e, f), (a + e, b + f), (c + e, d + f), (a + c + e, b + d + f)]
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def _matrix_rect_bbox(matrix: list[float], rect: list[float]) -> list[float]:
    """任意のPDF矩形をmatrixでuser spaceへ写したaxis-aligned bboxを返す。"""
    left, bottom, right, top = rect
    points = [
        _pdf_point(matrix, left, bottom),
        _pdf_point(matrix, right, bottom),
        _pdf_point(matrix, right, top),
        _pdf_point(matrix, left, top),
    ]
    return [
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    ]


def _pdf_point(matrix: list[float], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def _pdf_image_payload(xobject: Any, object_name: str) -> tuple[str, bytes, Any]:
    """既に安全に解決したImage XObjectをpypdfの固定decoderで通常画像bytesへ変換する。"""
    from pypdf.generic._image_xobject import _xobj_to_image

    extension, image_bytes, image = _xobj_to_image(xobject)
    return f"{object_name.lstrip('/')}{extension}", image_bytes, image


def _pdf_xobject_at_path(page: Any, xobject_path: list[str]) -> Any:
    """Evidenceへ固定したresource pathだけを辿る。page全XObjectの再列挙は行わない。"""

    def dereference(value: Any) -> Any:
        return value.get_object() if hasattr(value, "get_object") else value

    resources = dereference(page.get("/Resources"))
    current = None
    for index, raw_name in enumerate(xobject_path):
        pdf_name = "/" + str(raw_name).lstrip("/")
        xobjects = dereference(resources.get("/XObject")) if resources is not None else None
        reference = xobjects.get(pdf_name) if xobjects is not None else None
        if reference is None:
            raise KeyError(pdf_name)
        current = dereference(reference)
        if index < len(xobject_path) - 1:
            nested_resources = current.get("/Resources")
            if nested_resources is not None:
                resources = dereference(nested_resources)
    if current is None or str(current.get("/Subtype", "")) != "/Image":
        raise KeyError("resource path does not end at an Image XObject")
    return current


class _XObjectWalker:
    """page/Formの``Do``を再帰して Image XObject の spec を集める。解決件数は page 全体で数える。"""

    def __init__(self, builder: _Builder, reader: Any, page_number: int) -> None:
        self.builder = builder
        self.reader = reader
        self.page_number = page_number
        self.image_specs: list[dict[str, Any]] = []
        self.resolution_count = 0

    @staticmethod
    def dereference(value: Any) -> Any:
        return value.get_object() if hasattr(value, "get_object") else value

    @staticmethod
    def object_token(reference: Any, resolved: Any) -> tuple[Any, ...]:
        indirect = reference if hasattr(reference, "idnum") else getattr(resolved, "indirect_reference", None)
        if indirect is not None and hasattr(indirect, "idnum"):
            return ("indirect", int(indirect.idnum), int(getattr(indirect, "generation", 0)))
        return ("direct", id(resolved))

    def path_locator(
        self,
        path: tuple[str, ...],
        operator_path: tuple[int, ...],
        *,
        bbox: list[float] | None = None,
        suffix: str = "",
    ) -> evidence_ir.Locator:
        clean_path = [name.lstrip("/") for name in path]
        joined = "/".join(clean_path)
        return evidence_ir.Locator(
            part=f"page:{self.page_number}/xobject:{joined}{suffix}",
            page=self.page_number,
            object_id=f"{joined}{suffix}",
            bbox=bbox,
            extension={
                "coordinate_system": "pdf-user-space",
                "xobject_path": clean_path,
                "operator_path": list(operator_path),
            },
        )

    def add_issue(
        self,
        path: tuple[str, ...],
        operator_path: tuple[int, ...],
        *,
        status: str,
        detected_kind: str,
        content_basis: str,
        reason_code: str,
        bbox: list[float] | None,
        detail: dict[str, Any] | None = None,
        suffix: str = "",
    ) -> None:
        self.builder.add_coverage(
            "object",
            self.path_locator(path, operator_path, bbox=bbox, suffix=suffix),
            status,
            detected_kind=detected_kind,
            content_basis=content_basis,
            reason_code=reason_code,
            parser_id=PDF_ADAPTER_VERSION,
            detail={
                "xobject_path": [name.lstrip("/") for name in path],
                "operator_path": list(operator_path),
                **(detail or {}),
            },
        )

    def walk(
        self,
        current_operations: list[tuple[list[Any], bytes]],
        current_resources: Any,
        *,
        initial_ctm: list[float],
        path_prefix: tuple[str, ...],
        operator_prefix: tuple[int, ...],
        active_forms: frozenset[tuple[Any, ...]],
        form_depth: int,
        clip_bbox: list[float] | None,
    ) -> None:
        stack: list[list[float]] = []
        ctm = list(initial_ctm)
        for operator_index, (operands, operator) in enumerate(current_operations):
            if operator == b"q":
                stack.append(list(ctm))
                continue
            if operator == b"Q":
                ctm = stack.pop() if stack else list(initial_ctm)
                continue
            if operator == b"cm" and len(operands) == 6:
                try:
                    ctm = _matrix_multiply(ctm, [float(value) for value in operands])
                except (TypeError, ValueError):
                    pass
                continue
            if operator != b"Do" or not operands:
                continue
            pdf_name = str(operands[0])
            if not pdf_name.startswith("/"):
                pdf_name = "/" + pdf_name
            self.do(
                pdf_name,
                ctm,
                current_resources,
                path=(*path_prefix, pdf_name),
                operator_path=(*operator_prefix, operator_index),
                active_forms=active_forms,
                form_depth=form_depth,
                clip_bbox=clip_bbox,
            )

    def resolve(
        self,
        current_resources: Any,
        pdf_name: str,
        path: tuple[str, ...],
        operator_path: tuple[int, ...],
        reference_bbox: list[float],
    ) -> tuple[Any, Any, str] | None:
        """resource 辞書から (reference, xobject, Subtype) を解く。解けなければ coverage に積んで None。"""
        try:
            resource_dict = self.dereference(current_resources)
            xobjects = self.dereference(resource_dict.get("/XObject")) if resource_dict is not None else None
            reference = xobjects.get(pdf_name) if xobjects is not None else None
        except Exception as exc:
            self.add_issue(
                path,
                operator_path,
                status="failed",
                detected_kind="pdf_xobject_reference",
                # Do演算子は表示内容への参照であり、resource辞書を解決できなくても
                # content-bearing である。none にするとcoverage noticeが描画されず
                # 未解決の表示内容がsilent-dropする。
                content_basis="binary_only",
                reason_code="xobject_resource_resolution_failed",
                bbox=reference_bbox,
                detail={"error_type": exc.__class__.__name__},
            )
            return None
        if reference is None:
            self.add_issue(
                path,
                operator_path,
                status="unsupported",
                detected_kind="pdf_xobject_reference",
                content_basis="binary_only",
                reason_code="xobject_resource_missing",
                bbox=reference_bbox,
            )
            return None
        try:
            xobject = self.dereference(reference)
            subtype = str(xobject.get("/Subtype", ""))
        except Exception as exc:
            self.add_issue(
                path,
                operator_path,
                status="failed",
                detected_kind="pdf_xobject",
                content_basis="binary_only",
                reason_code="xobject_parse_failed",
                bbox=reference_bbox,
                detail={"error_type": exc.__class__.__name__},
            )
            return None
        return reference, xobject, subtype

    def do(
        self,
        pdf_name: str,
        ctm: list[float],
        current_resources: Any,
        *,
        path: tuple[str, ...],
        operator_path: tuple[int, ...],
        active_forms: frozenset[tuple[Any, ...]],
        form_depth: int,
        clip_bbox: list[float] | None,
    ) -> None:
        reference_bbox = _matrix_bbox(ctm)
        if self.resolution_count >= PDF_XOBJECT_MAX_RESOLUTIONS:
            self.add_issue(
                path,
                operator_path,
                status="unsupported",
                detected_kind="pdf_xobject_reference",
                # 上限によりSubtype未解決でも、Do参照は表示内容を指す。
                # ``none``にするとrendererがnoticeを出さず、内容が静かに落ちる。
                content_basis="binary_only",
                reason_code="xobject_resource_limit_exceeded",
                bbox=reference_bbox,
                detail={"max_resolutions": PDF_XOBJECT_MAX_RESOLUTIONS},
            )
            return
        self.resolution_count += 1
        resolved = self.resolve(current_resources, pdf_name, path, operator_path, reference_bbox)
        if resolved is None:
            return
        reference, xobject, subtype = resolved
        if subtype == "/Image":
            self.add_image(xobject, ctm, path, operator_path, reference_bbox, clip_bbox)
            return
        if subtype != "/Form":
            self.add_issue(
                path,
                operator_path,
                status="unsupported",
                detected_kind="pdf_xobject",
                content_basis="binary_only",
                reason_code="xobject_subtype_unsupported",
                bbox=reference_bbox,
                detail={"subtype": subtype or None},
            )
            return
        self.enter_form(
            reference, xobject, ctm, current_resources, path, operator_path, reference_bbox,
            active_forms=active_forms, form_depth=form_depth, clip_bbox=clip_bbox)

    def add_image(
        self,
        xobject: Any,
        ctm: list[float],
        path: tuple[str, ...],
        operator_path: tuple[int, ...],
        reference_bbox: list[float],
        clip_bbox: list[float] | None,
    ) -> None:
        try:
            image_name, image_bytes, decoded_image = _pdf_image_payload(xobject, path[-1])
            pixel_size = list(decoded_image.size)
            source_stream = xobject.get_data()
        except Exception as exc:
            self.add_issue(
                path,
                operator_path,
                status="failed",
                detected_kind="image_xobject",
                content_basis="pixel_only",
                reason_code="image_decode_failed",
                bbox=reference_bbox,
                detail={"error_type": exc.__class__.__name__},
            )
            return
        asset_extension = Path(image_name).suffix.lower() or ".bin"
        raw_filters = xobject.get("/Filter")
        source_filters = (
            [str(item) for item in raw_filters]
            if isinstance(raw_filters, (list, tuple))
            else ([str(raw_filters)] if raw_filters is not None else [])
        )
        visible_bbox = _bbox_intersection(reference_bbox, clip_bbox) if clip_bbox is not None else reference_bbox
        self.image_specs.append({
            "name": "/".join(name.lstrip("/") for name in path),
            "bbox": reference_bbox,
            "effective_bbox": visible_bbox,
            "extension": {
                "operator_index": operator_path[0],
                "operator_path": list(operator_path),
                "matrix": list(ctm),
                "xobject_path": [name.lstrip("/") for name in path],
                "asset_sha256": hashlib.sha256(image_bytes).hexdigest(),
                "source_stream_sha256": hashlib.sha256(source_stream).hexdigest(),
                "source_stream_basis": "pypdf_decoded_stream",
                "source_filters": source_filters,
                "pixel_size": pixel_size,
                "image_name": image_name,
                "asset_extension": asset_extension,
                "media_type": mimetypes.guess_type(image_name)[0] or "application/octet-stream",
                **({"visible_bbox": visible_bbox} if visible_bbox != reference_bbox else {}),
            },
        })

    def form_geometry(
        self,
        xobject: Any,
        ctm: list[float],
        path: tuple[str, ...],
        operator_path: tuple[int, ...],
        reference_bbox: list[float],
        clip_bbox: list[float] | None,
    ) -> tuple[list[float], list[float], list[float] | None] | None:
        """Form の (変換行列, bbox, 子へ渡す clip)。Matrix が読めなければ coverage に積んで None。"""
        try:
            raw_matrix = xobject.get("/Matrix")
            form_matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0] if raw_matrix is None else [float(value) for value in raw_matrix]
            if len(form_matrix) != 6:
                raise ValueError("Form Matrix must contain 6 numbers")
        except (TypeError, ValueError) as exc:
            self.add_issue(
                path,
                operator_path,
                status="failed",
                detected_kind="pdf_form_xobject",
                content_basis="structured",
                reason_code="form_matrix_parse_failed",
                bbox=reference_bbox,
                detail={"error_type": exc.__class__.__name__},
            )
            return None
        form_ctm = _matrix_multiply(ctm, form_matrix)
        form_bbox = reference_bbox
        child_clip = clip_bbox
        raw_form_bbox = xobject.get("/BBox")
        if raw_form_bbox is not None:
            try:
                rect = [float(value) for value in raw_form_bbox]
                if len(rect) != 4:
                    raise ValueError("Form BBox must contain 4 numbers")
                form_bbox = _matrix_rect_bbox(form_ctm, rect)
                child_clip = _bbox_intersection(clip_bbox, form_bbox) if clip_bbox is not None else form_bbox
            except (TypeError, ValueError) as exc:
                self.add_issue(
                    path,
                    operator_path,
                    status="unsupported",
                    detected_kind="pdf_form_bbox",
                    content_basis="structured",
                    reason_code="form_bbox_parse_failed",
                    bbox=reference_bbox,
                    detail={"error_type": exc.__class__.__name__},
                    suffix="/bbox",
                )
        return form_ctm, form_bbox, child_clip

    def enter_form(
        self,
        reference: Any,
        xobject: Any,
        ctm: list[float],
        current_resources: Any,
        path: tuple[str, ...],
        operator_path: tuple[int, ...],
        reference_bbox: list[float],
        *,
        active_forms: frozenset[tuple[Any, ...]],
        form_depth: int,
        clip_bbox: list[float] | None,
    ) -> None:
        """Form の content stream を深さ・循環の境界つきで再帰する。"""
        from pypdf.generic import ContentStream

        geometry = self.form_geometry(xobject, ctm, path, operator_path, reference_bbox, clip_bbox)
        if geometry is None:
            return
        form_ctm, form_bbox, child_clip = geometry
        next_depth = form_depth + 1
        if next_depth > PDF_XOBJECT_MAX_DEPTH:
            self.add_issue(
                path,
                operator_path,
                status="unsupported",
                detected_kind="pdf_form_xobject",
                content_basis="structured",
                reason_code="xobject_recursion_depth_exceeded",
                bbox=form_bbox,
                detail={"max_depth": PDF_XOBJECT_MAX_DEPTH},
            )
            return
        token = self.object_token(reference, xobject)
        if token in active_forms:
            self.add_issue(
                path,
                operator_path,
                status="failed",
                detected_kind="pdf_form_xobject",
                content_basis="structured",
                reason_code="xobject_cycle_detected",
                bbox=form_bbox,
            )
            return
        try:
            form_operations = list(ContentStream(xobject, self.reader).operations)
            raw_resources = xobject.get("/Resources")
            form_resources = current_resources if raw_resources is None else self.dereference(raw_resources)
        except Exception as exc:
            self.add_issue(
                path,
                operator_path,
                status="failed",
                detected_kind="pdf_form_xobject",
                content_basis="structured",
                reason_code="form_content_stream_parse_failed",
                bbox=form_bbox,
                detail={"error_type": exc.__class__.__name__},
            )
            return
        self.builder.add_coverage(
            "object",
            self.path_locator(path, operator_path, bbox=form_bbox),
            "extracted",
            detected_kind="pdf_form_xobject",
            content_basis="structured",
            reason_code="content_extracted",
            parser_id=PDF_ADAPTER_VERSION,
            detail={
                "xobject_path": [name.lstrip("/") for name in path],
                "operator_path": list(operator_path),
            },
        )
        self.walk(
            form_operations,
            form_resources,
            initial_ctm=form_ctm,
            path_prefix=path,
            operator_prefix=operator_path,
            active_forms=active_forms | {token},
            form_depth=next_depth,
            clip_bbox=child_clip,
        )


def _pdf_xobject_specs(
    builder: _Builder,
    *,
    reader: Any,
    page_number: int,
    page_bbox: list[float],
    operations: list[tuple[list[Any], bytes]],
    resources: Any,
) -> list[dict[str, Any]]:
    """page/Formの``Do``を再帰し、実際に描画されたImage XObjectだけを列挙する。

    ``page.images``の平坦なfile nameは、異なるForm内で同名の画像を区別できず、Form自体を画像と
    誤認する。そこでresource pathを保持したままSubtypeを判定し、Form Matrixと各content streamの
    graphics stateを合成する。深さ・循環・解決件数の境界はCoverageへ明示し、黙って打ち切らない。
    """
    walker = _XObjectWalker(builder, reader, page_number)
    walker.walk(
        operations,
        resources,
        initial_ctm=[1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
        path_prefix=(),
        operator_prefix=(),
        active_forms=frozenset(),
        form_depth=0,
        clip_bbox=page_bbox,
    )
    return walker.image_specs
