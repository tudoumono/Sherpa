"""OCR の推論へ渡す画像の縮小: 上限を超える画像だけ縮め、観測の座標は元の画像の座標で返す。
設計: docs/design/rag.md「OCR（非同期・隔離ワーカー）」
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sherpa.ingest import ai_observation, ocr_image, ocr_worker
from tests.unit.test_ocr_worker import GENERATION_ID, SOURCE, FakeEngine, _limit_profile, _picture_route


def _png_bytes(width, height, *, exif_orientation=None):
    import io
    from PIL import Image
    buffer = io.BytesIO()
    image = Image.new("RGB", (width, height), (255, 255, 255))
    if exif_orientation is None:
        image.save(buffer, format="PNG")
    else:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def _image_size_of(raw):
    import io
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        return image.size


class _FakePaddle:
    """PaddleOCR 本体の代役。渡された画像の大きさを記録し、縮め後の座標で 1 行返す。"""

    def __init__(self, box_in_engine_frame):
        self.seen_sizes = []
        self.box = box_in_engine_frame

    def predict(self, path):
        self.seen_sizes.append(_image_size_of(Path(path).read_bytes()))
        return [{"res": {"rec_texts": ["本文"], "rec_scores": [0.9], "rec_boxes": [self.box]}}]


class _FileEngine:
    """`PaddleOCREngine.predict_file` に、バイト列を一時ファイルへ置いて渡す。"""

    def __init__(self, fake):
        self._engine = object.__new__(ocr_worker.PaddleOCREngine)
        self._engine._ocr = fake

    def predict(self, image_bytes, *, media_type):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ("input" + ocr_worker._paddle_input_suffix(media_type))
            path.write_bytes(image_bytes)
            return self._engine.predict_file(path, media_type=media_type)


def _engine_with(fake):
    return _FileEngine(fake)


@pytest.mark.parametrize("size", [
    pytest.param((4299, 3036), id="long-side-over-limit"),
    pytest.param((900, 900), id="pixels-over-limit-only"),
])
def test_oversized_image_is_downscaled_keeping_aspect_ratio(size):
    scaled = ocr_image.downscale_for_engine(_png_bytes(*size), "image/jpeg", _limit_profile())

    width, height = _image_size_of(scaled.image_bytes)
    assert scaled.media_type == "image/png"
    assert max(width, height) <= 1000 and width * height <= 500_000
    assert abs(width / height - size[0] / size[1]) < 0.01
    assert scaled.scale_x == pytest.approx(size[0] / width) and scaled.scale_y == pytest.approx(size[1] / height)


def test_image_within_limits_and_non_image_input_are_passed_through_untouched():
    small = _png_bytes(400, 300)
    assert ocr_image.downscale_for_engine(small, "image/png", _limit_profile()) == \
        ocr_image.EngineImage(small, "image/png")
    pdf = b"%PDF-1.7 not an image"
    assert ocr_image.downscale_for_engine(pdf, "application/pdf", _limit_profile()) == \
        ocr_image.EngineImage(pdf, "application/pdf")


def test_downscale_applies_exif_orientation_like_the_engine_would_read_the_original():
    raw = _png_bytes(3000, 1500, exif_orientation=6)   # 90 度回して表示＝向き補正後は縦長 1500x3000
    scaled = ocr_image.downscale_for_engine(raw, "image/jpeg", _limit_profile())

    width, height = _image_size_of(scaled.image_bytes)
    assert height > width
    assert (width * scaled.scale_x, height * scaled.scale_y) == pytest.approx((1500, 3000), abs=1e-6)


def test_engine_reads_downscaled_image_and_returns_bbox_in_original_coordinates(monkeypatch):
    monkeypatch.setattr(ocr_worker, "PADDLE_CPU_PROFILE", _limit_profile())
    fake = _FakePaddle([100, 50, 300, 150])   # 縮め後(1000x500)の座標

    prediction = _engine_with(fake).predict(_png_bytes(4000, 2000), media_type="image/png")

    assert fake.seen_sizes == [(1000, 500)]
    assert prediction.observations[0].bbox == pytest.approx([400, 200, 1200, 600])


def test_engine_passes_small_image_and_bbox_through_unchanged(monkeypatch):
    monkeypatch.setattr(ocr_worker, "PADDLE_CPU_PROFILE", _limit_profile())
    fake = _FakePaddle([10, 20, 30, 40])

    prediction = _engine_with(fake).predict(_png_bytes(400, 300), media_type="image/png")

    assert fake.seen_sizes == [(400, 300)]
    assert prediction.observations[0].bbox == [10.0, 20.0, 30.0, 40.0]


def test_downscaled_bbox_fits_the_original_pixel_size_recorded_in_the_observation_set(monkeypatch, tmp_path):
    ir, asset_root, decision, job = _picture_route(tmp_path)
    prepared = ocr_worker.prepare_input(job, decision, source_path=SOURCE, asset_root=asset_root)
    width, height = prepared.pixel_size
    profile = _limit_profile(side=max(8, max(width, height) // 2), pixels=100_000_000)
    monkeypatch.setattr(ocr_worker, "PADDLE_CPU_PROFILE", profile)
    scaled_width, scaled_height = _image_size_of(
        ocr_image.downscale_for_engine(prepared.image_bytes, prepared.media_type, profile).image_bytes)
    assert (scaled_width, scaled_height) != (width, height)
    fake = _FakePaddle([1, 1, scaled_width - 1, scaled_height - 1])   # 縮め後の画像のほぼ全面

    prediction = _engine_with(fake).predict(prepared.image_bytes, media_type=prepared.media_type)
    observation_set = ocr_worker.build_observation_set(
        ir=ir, decision=decision, prepared=prepared, prediction=prediction,
        canonical_generation_id=GENERATION_ID, engine=FakeEngine(),
    )

    x1, y1, x2, y2 = observation_set.observations[0].pixel_bbox
    assert 0 < x1 < x2 <= width and 0 < y1 < y2 <= height
    assert x2 > scaled_width and y2 > scaled_height   # 縮め後の座標のままではない
    assert ai_observation.validation_errors(observation_set, ir=ir) == []


def _rotated_asset_input(tmp_path, raw):
    """`_picture_route` の画像を差し替えた入力（原画像は向きタグ付き JPEG・router の記録は向き補正前の大きさ）。"""
    import dataclasses
    _ir, asset_root, decision, job = _picture_route(tmp_path)
    (asset_root / decision.asset_rel_path).write_bytes(raw)
    width, height = _image_size_of(raw)
    decision = dataclasses.replace(
        decision, asset_sha256="sha256:" + hashlib.sha256(raw).hexdigest(), media_type="image/jpeg",
        pixel_size=[width, height],
    )
    return asset_root, decision, job


def test_rotated_image_bbox_fits_pixel_size_recorded_for_the_observation(monkeypatch, tmp_path):
    raw = _png_bytes(3000, 1500, exif_orientation=6)   # 向き補正後は縦長 1500x3000
    asset_root, decision, job = _rotated_asset_input(tmp_path, raw)
    prepared = ocr_worker.prepare_input(job, decision, source_path=SOURCE, asset_root=asset_root)
    monkeypatch.setattr(ocr_worker, "PADDLE_CPU_PROFILE", _limit_profile(side=1000, pixels=100_000_000))
    fake = _FakePaddle([1, 1, 499, 999])   # 縮め後(500x1000)の画像のほぼ全面

    prediction = _engine_with(fake).predict(prepared.image_bytes, media_type=prepared.media_type)

    assert prepared.pixel_size == [1500, 3000]
    width, height = prepared.pixel_size
    x1, y1, x2, y2 = prediction.observations[0].bbox
    assert 0 < x1 < x2 <= width and 0 < y1 < y2 <= height


def test_unscaled_rotated_image_pixel_size_is_also_oriented(tmp_path):
    raw = _png_bytes(400, 300, exif_orientation=6)   # 縮めない画像でも同じ座標系（向き補正後 300x400）
    asset_root, decision, job = _rotated_asset_input(tmp_path, raw)

    prepared = ocr_worker.prepare_input(job, decision, source_path=SOURCE, asset_root=asset_root)

    assert prepared.pixel_size == [300, 400]
