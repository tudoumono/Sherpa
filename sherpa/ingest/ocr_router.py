"""Canonical Evidence から OCR 対象だけを決定的に選ぶ純粋 router。

OCR は実行せず、画像の意味も推定しない。Evidence 上のラスタ要素と hash 照合済み asset inventory だけを入力に、
全候補を ``selected`` / ``excluded`` / ``failed_binding`` へ分類する。
PDF の page render は、利用可能な埋込画像も現行 text layer も無い page だけに限る。
設計: docs/design/rag.md「OCR（非同期・隔離ワーカー）」
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from . import evidence_ir, metafile_text


OCR_ROUTE_SCHEMA_VERSION = "ocr-route-manifest-v1"
OCR_ROUTER_PROFILE = "evidence-raster-router-v6"
OCR_ROUTE_SIG_MARKER = ".ocr_route_sig"
ROUTE_STATUSES = frozenset({"selected", "excluded", "failed_binding"})
RASTER_ELEMENT_TYPES = frozenset({"picture", "image_xobject", "image", "standalone_image", "image_fill"})
RASTER_ASSET_ROLES = frozenset({"picture_content", "shape_fill"})
UNSUPPORTED_IMAGE_FORMAT = "unsupported_image_format"
METAFILE_EXPANDED = "metafile_expanded"
METAFILE_RENDERED = "metafile_rendered"
METAFILE_RENDER_UNAVAILABLE = "metafile_render_unavailable"
METAFILE_RENDER_FAILED = "metafile_render_failed"
METAFILE_RENDER_PENDING = "metafile_render_pending"
METAFILE_TOO_LARGE = "metafile_too_large"
METAFILE_RENDER_UNAVAILABLE_MESSAGE = "未対応（LibreOffice が入っていません）"
PAGE_RENDER_PROFILE: dict[str, Any] = {
    "renderer": "pypdfium2",
    "profile": "pdf-page-render-pypdfium2-200dpi-rgb-png-v1",
    "dpi": 200,
    "color_space": "RGB",
    "format": "png",
    "alpha_background": "#FFFFFF",
}

_SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
_MAGIC_HEAD_BYTES = 64


def raster_media_type(head: bytes) -> str | None:
    """先頭バイトから OCR worker が読めるラスタ形式の media type を返す。読めない形式は None。

    拡張子の推測は使わない。OCR に回してよい形式の判定はここ 1 か所に置く。
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass(frozen=True)
class AssetBinding:
    """generation内のhash照合済みasset。pathはasset rootからの相対に限定する。"""

    asset_sha256: str
    relative_path: str
    media_type: str
    pixel_size: list[int] | None = None
    # 先頭バイトで読めるラスタと確認できたか。None=未判定（media_typeで判断する）。
    raster_readable: bool | None = None
    # WMF/EMF から取り出した埋込ビットマップ（PNG）の親メタファイル hash。通常の画像は None。
    parent_sha256: str | None = None
    # WMF/EMF の全体描画の状態（``pending`` / ``unavailable`` / ``failed:{理由}``）。描画できた・不要だった・対象外は None。
    render_state: str | None = None

    def is_readable_raster(self) -> bool:
        if self.raster_readable is not None:
            return self.raster_readable
        return (self.media_type or "").split(";")[0].strip().lower() in _READABLE_MEDIA_TYPES


_READABLE_MEDIA_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/bmp", "image/tiff", "image/webp",
})


@dataclass(frozen=True)
class OCRRouteDecision:
    route_input_id: str
    target_evidence_id: str
    input_kind: str
    status: str
    reason_code: str
    priority: int
    asset_sha256: str | None = None
    asset_rel_path: str | None = None
    media_type: str | None = None
    pixel_size: list[int] | None = None
    page_render: dict[str, Any] | None = None
    detail: dict[str, Any] | None = None


@dataclass
class OCRRouteManifest:
    schema_version: str
    router_profile: str
    source_rel_path: str
    source_content_hash: str
    decisions: list[OCRRouteDecision]
    route_manifest_hash: str


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ocr_route_sig_value() -> str:
    """OCR route の決定規則を generation へ刻む content-addressed 署名。

    Router 実装を変えたら ``OCR_ROUTER_PROFILE`` を更新し、page render 条件は値そのものを署名へ含める。
    """
    payload = {
        "schema_version": OCR_ROUTE_SCHEMA_VERSION,
        "router_profile": OCR_ROUTER_PROFILE,
        "page_render_profile": PAGE_RENDER_PROFILE,
    }
    return "sha256:" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def ocr_route_sig_drift(derived_md_dir: str | Path) -> bool:
    """公開generationのroute署名が現行profileと異なる、または欠落していればTrue。"""
    marker = Path(derived_md_dir) / OCR_ROUTE_SIG_MARKER
    try:
        return marker.read_text(encoding="utf-8").strip() != ocr_route_sig_value()
    except (OSError, UnicodeError):
        return True


def _hex_digest(value: str) -> str:
    match = _SHA256_RE.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError("invalid sha256")
    return match.group(1)


def _tagged_digest(value: str) -> str:
    return "sha256:" + _hex_digest(value)


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path must be a non-traversing relative path")
    return path.as_posix()


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256(_canonical(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _child_parent_hash(relative: str) -> str | None:
    """``_metafile/{親のsha256}/{連番}.png`` の形なら親の hash（``sha256:`` 付き）を返す。"""
    parts = PurePosixPath(relative).parts
    if len(parts) == 3 and parts[0] == metafile_text.CHILD_DIR and re.fullmatch(r"[0-9a-f]{64}", parts[1]):
        return "sha256:" + parts[1]
    return None


def inventory_assets(root: str | Path) -> list[AssetBinding]:
    """asset directory を読み、bytes hash を権威として安全な inventory を返す。

    symlink は受理しない。同一 hash が複数名で存在する場合は辞書順で最初の path だけを採用する。
    メタファイルから取り出した子 PNG は親ごとに別の資産として数える。
    """
    asset_root = Path(root)
    if not asset_root.is_dir() or asset_root.is_symlink():
        return []
    by_key: dict[tuple[str, str], AssetBinding] = {}
    for path in sorted(asset_root.rglob("*"), key=lambda item: item.relative_to(asset_root).as_posix()):
        if path.is_symlink():
            raise ValueError(f"asset inventory contains symlink: {path.relative_to(asset_root).as_posix()}")
        if not path.is_file():
            continue
        if path.name.endswith(metafile_text.RENDER_STATE_SUFFIX) and path.parent.name == metafile_text.CHILD_DIR:
            continue                       # 全体描画の記録（資産ではない）
        digest = _file_sha256(path)
        relative = _safe_relative(path.relative_to(asset_root).as_posix())
        with path.open("rb") as stream:
            head = stream.read(_MAGIC_HEAD_BYTES)
        sniffed = raster_media_type(head[:16])
        media_type = sniffed or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parent = _child_parent_hash(relative)
        render_state = None
        if parent is None and sniffed is None and metafile_text.sniff(head) is not None:
            render_state = metafile_text.read_render_state(asset_root, digest)
        binding = AssetBinding(
            asset_sha256=f"sha256:{digest}", relative_path=relative, media_type=media_type,
            pixel_size=metafile_text.child_png_size(head) if parent else None,
            raster_readable=sniffed is not None, parent_sha256=parent, render_state=render_state,
        )
        by_key.setdefault((digest, parent or ""), binding)
    return [by_key[key] for key in sorted(by_key)]


def _manifest_payload(manifest: OCRRouteManifest) -> dict[str, Any]:
    payload = asdict(manifest)
    payload.pop("route_manifest_hash", None)
    return payload


def content_hash(manifest: OCRRouteManifest) -> str:
    return "sha256:" + hashlib.sha256(_canonical(_manifest_payload(manifest)).encode("utf-8")).hexdigest()


def _priority(element: evidence_ir.EvidenceElement) -> int:
    priority = 100
    size = element.extension.get("pixel_size")
    if isinstance(size, list) and len(size) == 2 and all(isinstance(value, int) for value in size) \
            and min(size) < 32:
        priority -= 30
    if element.visibility == "hidden":
        priority -= 20
    return max(1, priority)


def _has_current_text(ir: evidence_ir.EvidenceIR, page: int) -> bool:
    for element in ir.elements:
        if element.locator.page != page or element.type not in {"text_object", "positioned_text", "cell"}:
            continue
        if (element.visibility != "hidden"
                and element.extension.get("use_for_current_answer") is not False
                and isinstance(element.value, str) and element.value.strip()):
            return True
    return False


def _raster_candidates(element: evidence_ir.EvidenceElement) -> list[dict[str, Any]]:
    """Evidence 要素に拘束された OCR 対象ラスタを列挙する。

    OOXML の画像 fill は要素 type を保ったまま ``asset_role=shape_fill`` で表すため、type と role を直交して判定する。
    複数 blip を持つ要素も全 asset を個別 route へ残す。
    """
    extension = element.extension
    raw_candidates = extension.get("assets")
    candidates = raw_candidates if isinstance(raw_candidates, list) and raw_candidates else [extension]
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(candidates):
        if not isinstance(raw, dict):
            continue
        role = raw.get("asset_role") or extension.get("asset_role")
        if element.type not in RASTER_ELEMENT_TYPES and role not in RASTER_ASSET_ROLES:
            continue
        candidate = dict(raw)
        candidate.setdefault("asset_role", role)
        candidate["asset_index"] = index
        result.append(candidate)
    # Raster type自体が検知済みなら、hash欠落もfailed_bindingとして明示分類する。
    if not result and element.type in RASTER_ELEMENT_TYPES:
        result.append({"asset_index": 0, "asset_role": extension.get("asset_role")})
    return result


def _render_outcome(binding: AssetBinding) -> tuple[str, dict[str, Any]]:
    """メタファイルの全体描画の状態を（理由コード, detail）にする。状態が無ければ読めない画像形式。"""
    state = binding.render_state
    if not state or state == "rendered_unapplied":     # 描画済み（反映の仕上げ待ち）は状態なしと同じ
        return UNSUPPORTED_IMAGE_FORMAT, {}
    if state == "too_large":                           # 描画しない図は、LibreOffice の有無より大きさの理由を残す
        return METAFILE_TOO_LARGE, {"message": "大きすぎるため読み取りの対象外（32 MiB を超える図）"}
    if state == "unavailable":
        return METAFILE_RENDER_UNAVAILABLE, {"message": METAFILE_RENDER_UNAVAILABLE_MESSAGE}
    if state == "pending":
        return METAFILE_RENDER_PENDING, {"message": "図全体の描画待ち（バックグラウンドで順に描画します）"}
    if state:
        return METAFILE_RENDER_FAILED, {"message": "未対応（図全体の描画に失敗しました）",
                                        "render_failure": state.partition(":")[2]}
    return UNSUPPORTED_IMAGE_FORMAT, {}


def render_unavailable(decision: OCRRouteDecision) -> bool:
    """この入力（親のメタファイル）が「LibreOffice が無くて全体描画できない」状態か（DIB の有無によらない）。"""
    return (decision.detail or {}).get("message") == METAFILE_RENDER_UNAVAILABLE_MESSAGE


def build_manifest(
    ir: evidence_ir.EvidenceIR,
    *,
    source_rel_path: str,
    assets: Iterable[AssetBinding],
) -> OCRRouteManifest:
    """Evidenceと検証済みasset bindingから副作用なしでroute manifestを作る。"""
    source_rel = _safe_relative(source_rel_path)
    bindings: dict[str, AssetBinding] = {}
    children: dict[str, dict[str, AssetBinding]] = {}
    for binding in assets:
        digest = _hex_digest(binding.asset_sha256)
        relative = _safe_relative(binding.relative_path)
        parent = _tagged_digest(binding.parent_sha256) if binding.parent_sha256 else None
        normalized = AssetBinding(
            asset_sha256=f"sha256:{digest}", relative_path=relative, media_type=binding.media_type,
            pixel_size=binding.pixel_size, raster_readable=binding.raster_readable, parent_sha256=parent,
            render_state=binding.render_state,
        )
        if parent is not None:
            siblings = children.setdefault(_hex_digest(parent), {})
            existing_child = siblings.get(digest)
            if existing_child is None or normalized.relative_path < existing_child.relative_path:
                siblings[digest] = normalized
            continue
        existing = bindings.get(digest)
        if existing is None or normalized.relative_path < existing.relative_path:
            bindings[digest] = normalized

    decisions: list[OCRRouteDecision] = []
    selected_images_by_page: set[int] = set()
    raster_elements = sorted(
        (item for item in ir.elements if _raster_candidates(item)),
        key=lambda item: (item.locator.part, item.order, item.element_id),
    )
    for element in raster_elements:
        for candidate in _raster_candidates(element):
            raw_hash = candidate.get("asset_sha256")
            asset_index = candidate.get("asset_index", 0)
            asset_role = candidate.get("asset_role")
            route_id = _stable_id(
                "ocr-input", ir.source.content_hash, element.element_id, "asset", raw_hash, asset_index,
            )
            detail = {
                "element_type": element.type,
                "asset_role": asset_role,
                "asset_index": asset_index,
            }
            for key in (
                "relationship_id", "relationship_attribute", "relationship_type", "target_mode",
                "external_target_sha256", "binding_status", "vml_element",
            ):
                value = candidate.get(key)
                if value not in (None, ""):
                    detail[key] = value
            if not isinstance(raw_hash, str) or _SHA256_RE.fullmatch(raw_hash.strip().lower()) is None:
                reason = (
                    "external_asset_not_fetched"
                    if candidate.get("binding_status") == "external_reference"
                    else "evidence_asset_hash_missing"
                )
                decisions.append(OCRRouteDecision(
                    route_input_id=route_id, target_evidence_id=element.element_id, input_kind="asset",
                    status="failed_binding", reason_code=reason, priority=_priority(element),
                    detail=detail,
                ))
                continue
            digest = _hex_digest(raw_hash)
            binding = bindings.get(digest)
            if binding is None:
                decisions.append(OCRRouteDecision(
                    route_input_id=route_id, target_evidence_id=element.element_id, input_kind="asset",
                    status="failed_binding", reason_code="verified_asset_not_found", priority=_priority(element),
                    asset_sha256=f"sha256:{digest}", detail=detail,
                ))
                continue
            if not binding.is_readable_raster():
                embedded = [children[digest][key] for key in sorted(children.get(digest, {}))]
                if embedded:
                    # メタファイル自体は OCR へ回さず、中のビットマップ（子 PNG）を入力に選ぶ（親は「展開済み」）。
                    # 全体描画が済んでいない場合は、その状態を親の detail に残す。
                    _, render_extra = _render_outcome(binding)
                    decisions.append(OCRRouteDecision(
                        route_input_id=route_id, target_evidence_id=element.element_id, input_kind="asset",
                        status="excluded", reason_code=METAFILE_EXPANDED, priority=0,
                        asset_sha256=binding.asset_sha256, asset_rel_path=binding.relative_path,
                        media_type=binding.media_type,
                        detail={**detail, "child_asset_sha256s": [item.asset_sha256 for item in embedded],
                                **render_extra},
                    ))
                    for child in embedded:
                        child_detail = {
                            **detail,
                            "parent_route_input_id": route_id,
                            "parent_asset_sha256": binding.asset_sha256,
                            "parent_asset_rel_path": binding.relative_path,
                        }
                        decisions.append(OCRRouteDecision(
                            route_input_id=_stable_id(
                                "ocr-input", ir.source.content_hash, element.element_id, "asset",
                                child.asset_sha256, asset_index, "metafile_child", binding.asset_sha256,
                            ),
                            target_evidence_id=element.element_id, input_kind="asset",
                            status="selected",
                            reason_code=(
                                METAFILE_RENDERED if PurePosixPath(child.relative_path).name == metafile_text.RENDER_NAME
                                else "metafile_embedded_bitmap"),
                            priority=_priority(element), asset_sha256=child.asset_sha256,
                            asset_rel_path=child.relative_path, media_type=child.media_type,
                            pixel_size=child.pixel_size, detail=child_detail,
                        ))
                        if isinstance(element.locator.page, int):
                            selected_images_by_page.add(element.locator.page)
                    continue
                reason, extra = _render_outcome(binding)
                decisions.append(OCRRouteDecision(
                    route_input_id=route_id, target_evidence_id=element.element_id, input_kind="asset",
                    status="excluded", reason_code=reason, priority=0,
                    asset_sha256=binding.asset_sha256, asset_rel_path=binding.relative_path,
                    media_type=binding.media_type, detail={**detail, **extra},
                ))
                continue
            pixel_size = candidate.get("pixel_size")
            if not (isinstance(pixel_size, list) and len(pixel_size) == 2):
                pixel_size = binding.pixel_size
            decisions.append(OCRRouteDecision(
                route_input_id=route_id, target_evidence_id=element.element_id, input_kind="asset",
                status="selected", reason_code="evidence_raster_asset", priority=_priority(element),
                asset_sha256=binding.asset_sha256, asset_rel_path=binding.relative_path,
                media_type=binding.media_type, pixel_size=pixel_size, detail=detail,
            ))
            if isinstance(element.locator.page, int):
                selected_images_by_page.add(element.locator.page)

    # PDF page renderは常に候補として分類する。選択は画像/text layerのどちらも利用不能なpageだけ。
    pages = sorted(
        (item for item in ir.elements if item.type == "page" and isinstance(item.locator.page, int)),
        key=lambda item: (item.locator.page or 0, item.element_id),
    )
    for page_element in pages:
        page = int(page_element.locator.page or 0)
        route_id = _stable_id("ocr-input", ir.source.content_hash, page_element.element_id, "page_render")
        render_profile = {**PAGE_RENDER_PROFILE, "page_1_based": page}
        if page in selected_images_by_page:
            status, reason = "excluded", "usable_page_image_present"
        elif _has_current_text(ir, page):
            status, reason = "excluded", "current_text_layer_present"
        else:
            status, reason = "selected", "scan_page_render_fallback"
        decisions.append(OCRRouteDecision(
            route_input_id=route_id, target_evidence_id=page_element.element_id, input_kind="page_render",
            status=status, reason_code=reason, priority=90 if status == "selected" else 0,
            media_type="image/png", page_render=render_profile,
            detail={"page": page, "source_rel_path": source_rel},
        ))

    decisions.sort(key=lambda item: (item.target_evidence_id, item.input_kind, item.route_input_id))
    manifest = OCRRouteManifest(
        schema_version=OCR_ROUTE_SCHEMA_VERSION,
        router_profile=OCR_ROUTER_PROFILE,
        source_rel_path=source_rel,
        source_content_hash=_tagged_digest(ir.source.content_hash),
        decisions=decisions,
        route_manifest_hash="",
    )
    manifest.route_manifest_hash = content_hash(manifest)
    errors = validation_errors(manifest, ir=ir)
    if errors:
        raise ValueError("invalid OCR route manifest: " + ",".join(errors))
    return manifest


def validation_errors(manifest: OCRRouteManifest, *, ir: evidence_ir.EvidenceIR | None = None) -> list[str]:
    errors: list[str] = []
    if manifest.schema_version != OCR_ROUTE_SCHEMA_VERSION:
        errors.append("schema_version")
    if manifest.router_profile != OCR_ROUTER_PROFILE:
        errors.append("router_profile")
    try:
        _safe_relative(manifest.source_rel_path)
        _hex_digest(manifest.source_content_hash)
    except ValueError:
        errors.append("source_identity")
    ids = [item.route_input_id for item in manifest.decisions]
    if len(ids) != len(set(ids)):
        errors.append("duplicate_route_input_id")
    evidence_ids = {item.element_id for item in ir.elements} if ir is not None else None
    for item in manifest.decisions:
        if item.status not in ROUTE_STATUSES:
            errors.append(f"route_status:{item.route_input_id}")
        if item.input_kind not in {"asset", "page_render"}:
            errors.append(f"input_kind:{item.route_input_id}")
        if evidence_ids is not None and item.target_evidence_id not in evidence_ids:
            errors.append(f"target_missing:{item.route_input_id}")
        if item.status == "selected" and item.input_kind == "asset":
            try:
                _hex_digest(item.asset_sha256 or "")
                _safe_relative(item.asset_rel_path or "")
            except ValueError:
                errors.append(f"asset_binding:{item.route_input_id}")
        if item.status == "selected" and item.input_kind == "page_render" and (
                item.page_render is None or item.page_render.get("profile") != PAGE_RENDER_PROFILE["profile"]):
            errors.append(f"page_render:{item.route_input_id}")
        if item.priority < 0:
            errors.append(f"priority:{item.route_input_id}")
    if _SHA256_RE.fullmatch(manifest.route_manifest_hash) is None:
        errors.append("route_manifest_hash")
    elif content_hash(manifest) != manifest.route_manifest_hash:
        errors.append("route_manifest_hash_mismatch")
    if ir is not None and _tagged_digest(ir.source.content_hash) != manifest.source_content_hash:
        errors.append("source_content_hash_mismatch")
    return sorted(set(errors))


def to_json_str(manifest: OCRRouteManifest) -> str:
    errors = validation_errors(manifest)
    if errors:
        raise ValueError("invalid OCR route manifest: " + ",".join(errors))
    return _canonical(asdict(manifest)) + "\n"


def from_json_str(raw: str, *, ir: evidence_ir.EvidenceIR | None = None) -> OCRRouteManifest:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("OCR route manifest must be an object")
    try:
        manifest = OCRRouteManifest(
            schema_version=payload["schema_version"], router_profile=payload["router_profile"],
            source_rel_path=payload["source_rel_path"], source_content_hash=payload["source_content_hash"],
            decisions=[OCRRouteDecision(**item) for item in payload.get("decisions", [])],
            route_manifest_hash=payload["route_manifest_hash"],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid OCR route manifest shape") from exc
    errors = validation_errors(manifest, ir=ir)
    if errors:
        raise ValueError("invalid OCR route manifest: " + ",".join(errors))
    return manifest


def write_json_atomic(path: str | Path, manifest: OCRRouteManifest) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            stream.write(to_json_str(manifest))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target
