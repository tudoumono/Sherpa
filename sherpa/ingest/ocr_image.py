"""OCR の推論へ渡す画像の縮小。上限を超える画像だけを縮め、観測の座標を戻す倍率を持つ。

設計: docs/design/rag.md「OCR（非同期・隔離ワーカー）」
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EngineImage:
    """推論へ渡す画像。縮めたときは、縮め後の座標を向き補正後の原画像の座標へ戻す倍率を持つ。"""
    image_bytes: bytes
    media_type: str
    scale_x: float = 1.0
    scale_y: float = 1.0


def downscale_target(width: int, height: int, profile: Any) -> tuple[int, int] | None:
    """長辺・総画素数のどちらかが上限を超えるとき、縦横比を保った縮め後の大きさ。超えなければ None。"""
    scale = min(
        1.0,
        profile.max_image_side / max(width, height),
        math.sqrt(profile.max_image_pixels / (width * height)),
    )
    if scale >= 1.0:
        return None
    return max(1, math.floor(width * scale)), max(1, math.floor(height * scale))


def downscale_for_engine(image_bytes: bytes, media_type: str, profile: Any) -> EngineImage:
    """上限を超える画像だけ PNG に縮める。上限内・PIL で開けない形式（PDF 等）はそのまま返す。

    縮めた画像は EXIF の向きを反映済み（PaddleOCR が原本を読むときと同じ向き）。
    """
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if downscale_target(image.width, image.height, profile) is None:
                return EngineImage(image_bytes, media_type)
            oriented = ImageOps.exif_transpose(image)
            width, height = oriented.size
            target = downscale_target(width, height, profile)
            if target is None:
                return EngineImage(image_bytes, media_type)
            if oriented.mode not in ("RGB", "L"):
                oriented = oriented.convert("RGB")
            resized = oriented.resize(target, Image.Resampling.LANCZOS)
    except UnidentifiedImageError:
        return EngineImage(image_bytes, media_type)
    buffer = io.BytesIO()
    resized.save(buffer, format="PNG", compress_level=1)
    return EngineImage(buffer.getvalue(), "image/png", width / target[0], height / target[1])
