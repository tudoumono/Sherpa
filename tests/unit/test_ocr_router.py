from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from sherpa.ingest import evidence_ir, ocr_router


SOURCE_HASH = "sha256:" + "a" * 64
ASSET_HASH = "sha256:" + hashlib.sha256(b"image-bytes").hexdigest()


def _element(
    element_id: str,
    element_type: str,
    *,
    page: int = 1,
    value=None,
    extension=None,
    visibility: str = "visible",
):
    return evidence_ir.EvidenceElement(
        element_id=element_id,
        type=element_type,
        parent_id="page-1" if element_id != "page-1" else None,
        order=1,
        value=value,
        locator=evidence_ir.Locator(part=f"page:{page}", page=page),
        coverage_id="coverage-1",
        visibility=visibility,
        extension=extension or {},
    )


def _ir(*elements):
    locator = evidence_ir.Locator(part="page:1")
    coverage_id = evidence_ir.make_coverage_id("part", "page", locator)
    normalized_elements = [replace(element, coverage_id=coverage_id) for element in elements]
    return evidence_ir.EvidenceIR(
        schema_version=evidence_ir.EVIDENCE_IR_SCHEMA_VERSION,
        parser_profile=evidence_ir.EVIDENCE_PARSER_PROFILE,
        source=evidence_ir.EvidenceSource(file_type="pdf", content_hash=SOURCE_HASH),
        elements=normalized_elements,
        coverage=[evidence_ir.CoverageItem(
            coverage_id=coverage_id, scope="part", detected_kind="page", locator=locator, status="extracted",
            content_basis="structured", reason_code="extracted", parser_id="test", detail={},
        )],
    )


def _asset():
    return ocr_router.AssetBinding(
        asset_sha256=ASSET_HASH,
        relative_path=hashlib.sha256(b"image-bytes").hexdigest() + ".png",
        media_type="image/png",
        pixel_size=[20, 10],
    )


def test_all_rasters_are_selected_even_hidden_or_small_and_page_render_is_excluded():
    page = _element("page-1", "page")
    picture = _element(
        "picture-1", "image_fill", extension={"asset_sha256": ASSET_HASH, "pixel_size": [20, 10]},
        visibility="hidden",
    )
    manifest = ocr_router.build_manifest(
        _ir(page, picture), source_rel_path="sub/document.pdf", assets=[_asset()],
    )

    image = next(item for item in manifest.decisions if item.input_kind == "asset")
    page_render = next(item for item in manifest.decisions if item.input_kind == "page_render")
    assert image.status == "selected"
    assert image.priority < 100
    assert page_render.status == "excluded"
    assert page_render.reason_code == "usable_page_image_present"
    assert ocr_router.validation_errors(manifest, ir=_ir(page, picture)) == []


def test_shape_fill_and_every_nested_raster_asset_are_routed():
    second_bytes = b"second-image"
    second_hash = "sha256:" + hashlib.sha256(second_bytes).hexdigest()
    shape = _element(
        "shape-1",
        "shape",
        extension={
            "asset_role": "shape_fill",
            "assets": [
                {"asset_role": "shape_fill", "asset_sha256": ASSET_HASH, "pixel_size": [20, 10]},
                {"asset_role": "shape_fill", "asset_sha256": second_hash, "pixel_size": [30, 15]},
            ],
        },
    )
    second = ocr_router.AssetBinding(
        asset_sha256=second_hash,
        relative_path=hashlib.sha256(second_bytes).hexdigest() + ".png",
        media_type="image/png",
        pixel_size=[30, 15],
    )

    manifest = ocr_router.build_manifest(
        _ir(shape), source_rel_path="sub/design.pptx", assets=[_asset(), second],
    )

    decisions = [item for item in manifest.decisions if item.input_kind == "asset"]
    assert len(decisions) == 2
    assert {item.status for item in decisions} == {"selected"}
    assert {item.asset_sha256 for item in decisions} == {ASSET_HASH, second_hash}
    assert {item.detail["asset_role"] for item in decisions if item.detail} == {"shape_fill"}


def test_same_bytes_at_two_relationships_keep_two_routes_and_share_only_worker_cache():
    shape = _element(
        "shape-duplicate",
        "shape",
        extension={
            "asset_role": "shape_fill",
            "assets": [
                {"asset_role": "shape_fill", "asset_sha256": ASSET_HASH, "relationship_id": "rId1"},
                {"asset_role": "shape_fill", "asset_sha256": ASSET_HASH, "relationship_id": "rId2"},
            ],
        },
    )

    manifest = ocr_router.build_manifest(
        _ir(shape), source_rel_path="sub/design.xlsx", assets=[_asset()],
    )

    decisions = [item for item in manifest.decisions if item.input_kind == "asset"]
    assert len(decisions) == 2
    assert len({item.route_input_id for item in decisions}) == 2
    assert {item.asset_sha256 for item in decisions} == {ASSET_HASH}


def test_page_render_is_selected_only_without_current_text_or_usable_image():
    page = _element("page-1", "page")
    current = _element("text-1", "text_object", value="現行テキスト")
    with_text = ocr_router.build_manifest(_ir(page, current), source_rel_path="scan.pdf", assets=[])
    assert with_text.decisions[0].status == "excluded"
    assert with_text.decisions[0].reason_code == "current_text_layer_present"

    covered = replace(current, visibility="visible", extension={"use_for_current_answer": False})
    without_current = ocr_router.build_manifest(_ir(page, covered), source_rel_path="hybrid.pdf", assets=[])
    assert without_current.decisions[0].status == "selected"
    assert without_current.decisions[0].reason_code == "scan_page_render_fallback"
    assert without_current.decisions[0].page_render == {**ocr_router.PAGE_RENDER_PROFILE, "page_1_based": 1}


def test_missing_or_unbound_asset_is_explicit_failed_binding():
    page = _element("page-1", "page")
    missing_hash = _element("picture-1", "picture")
    missing = ocr_router.build_manifest(_ir(page, missing_hash), source_rel_path="a.pdf", assets=[])
    decision = next(item for item in missing.decisions if item.input_kind == "asset")
    assert decision.status == "failed_binding"
    assert decision.reason_code == "evidence_asset_hash_missing"

    unbound = replace(missing_hash, extension={"asset_sha256": ASSET_HASH})
    manifest = ocr_router.build_manifest(_ir(page, unbound), source_rel_path="a.pdf", assets=[])
    decision = next(item for item in manifest.decisions if item.input_kind == "asset")
    assert decision.status == "failed_binding"
    assert decision.reason_code == "verified_asset_not_found"


def test_asset_inventory_hashes_bytes_and_rejects_symlink(tmp_path):
    asset = tmp_path / "screen.png"
    asset.write_bytes(b"image-bytes")
    inventory = ocr_router.inventory_assets(tmp_path)
    assert inventory == [ocr_router.AssetBinding(
        asset_sha256=ASSET_HASH, relative_path="screen.png", media_type="image/png", pixel_size=None,
    )]

    link = tmp_path / "linked.png"
    try:
        link.symlink_to(asset)
    except OSError:
        pytest.skip("symlink unavailable")
    with pytest.raises(ValueError, match="symlink"):
        ocr_router.inventory_assets(tmp_path)


def test_manifest_round_trip_and_tamper_detection():
    page = _element("page-1", "page")
    manifest = ocr_router.build_manifest(_ir(page), source_rel_path="scan.pdf", assets=[])
    restored = ocr_router.from_json_str(ocr_router.to_json_str(manifest), ir=_ir(page))
    assert restored == manifest
    restored.decisions.append(replace(restored.decisions[0], route_input_id="extra"))
    assert "route_manifest_hash_mismatch" in ocr_router.validation_errors(restored)


def test_route_signature_covers_router_and_page_render_profiles(monkeypatch):
    baseline = ocr_router.ocr_route_sig_value()

    with monkeypatch.context() as patch:
        patch.setattr(ocr_router, "OCR_ROUTER_PROFILE", "evidence-raster-router-v999-test")
        assert ocr_router.ocr_route_sig_value() != baseline

    with monkeypatch.context() as patch:
        patch.setitem(ocr_router.PAGE_RENDER_PROFILE, "dpi", 300)
        assert ocr_router.ocr_route_sig_value() != baseline


def test_route_signature_drift_is_fail_closed_for_missing_or_invalid_marker(tmp_path):
    marker = tmp_path / ocr_router.OCR_ROUTE_SIG_MARKER
    assert ocr_router.ocr_route_sig_drift(tmp_path) is True

    marker.write_text("old-profile\n", encoding="utf-8")
    assert ocr_router.ocr_route_sig_drift(tmp_path) is True

    marker.write_text(ocr_router.ocr_route_sig_value() + "\n", encoding="utf-8")
    assert ocr_router.ocr_route_sig_drift(tmp_path) is False
