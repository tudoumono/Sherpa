"""透過の有無を作り分けた最小の PNG/JPEG と、xlsx の埋め込み画像の差し替え（実環境の資料は使わない）。"""
from __future__ import annotations

import pathlib
import struct
import zipfile
import zlib


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def _predict(filter_type: int, left: int, up: int, upper_left: int) -> int:
    if filter_type == 1:
        return left
    if filter_type == 2:
        return up
    if filter_type == 3:
        return (left + up) >> 1
    if filter_type == 4:
        estimate = left + up - upper_left
        distances = (abs(estimate - left), abs(estimate - up), abs(estimate - upper_left))
        return (left, up, upper_left)[distances.index(min(distances))]
    return 0


def png(
    *, color_type: int, width: int = 8, height: int = 8, trns: bool = False,
    bit_depth: int = 8, filter_type: int = 0, transparent_pixels: int = 0, transparent_last: int = 0, interlace: int = 0,
) -> bytes:
    """画素の値が場所で変わる PNG。color_type は 0（グレー）・2（RGB）・3（パレット）・4（グレー+アルファ）・6（RGBA）。

    アルファ付きの形式は、先頭から `transparent_pixels` 個と末尾の `transparent_last` 個の画素だけアルファを 0 にし、残りは不透明（最大値）にする。
    `filter_type`（0〜4）は全行に同じフィルタを掛ける（復元の確認用）。
    """
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    sample_bytes = bit_depth // 8 if bit_depth >= 8 else 1
    bytes_per_pixel = channels * sample_bytes
    rows: list[bytes] = []
    pixel_index = 0
    for y in range(height):
        line = bytearray()
        for x in range(width):
            samples = [(x * 7 + y * 13 + c * 29) % 256 for c in range(channels)]
            if color_type in (4, 6):
                samples[-1] = 0 if pixel_index < transparent_pixels or pixel_index >= width * height - transparent_last else 255
            for value in samples:
                line.extend(bytes([value]) * sample_bytes)
            pixel_index += 1
        rows.append(bytes(line))
    scanlines = bytearray()
    previous = bytes(len(rows[0]))
    for line in rows:
        filtered = bytearray()
        for i, value in enumerate(line):
            left = line[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
            upper_left = previous[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
            filtered.append((value - _predict(filter_type, left, previous[i], upper_left)) & 255)
        scanlines.append(filter_type)
        scanlines.extend(filtered)
        previous = line
    parts = [b"\x89PNG\r\n\x1a\n",
             _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace))]
    if color_type == 3:
        parts.append(_chunk(b"PLTE", b"\xc8\x1e\x1e" * 256))
    if trns:
        parts.append(_chunk(b"tRNS", b"\x00" if color_type == 3 else b"\x00\x00\x00\x00\x00\x00"))
    parts.append(_chunk(b"IDAT", zlib.compress(bytes(scanlines))))
    parts.append(_chunk(b"IEND", b""))
    return b"".join(parts)


def opaque_png() -> bytes:
    """アルファの無い RGB の PNG。"""
    return png(color_type=2)


def transparent_png() -> bytes:
    """透過を含む画素のある RGBA の PNG（スタンプの角が透明な画像と同じ性質）。"""
    return png(color_type=6, transparent_pixels=3)


def opaque_rgba_png() -> bytes:
    """アルファ付きだが全画素が不透明な PNG。"""
    return png(color_type=6)


def jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"


def replace_media(source: pathlib.Path, target: pathlib.Path, data: bytes, *, folder: str = "xl/media/") -> pathlib.Path:
    """`source` の zip をコピーし、`folder` 配下の画像をすべて `data` に差し替えて `target` に書く。"""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as dst:
        for info in src.infolist():
            body = src.read(info.filename)
            if info.filename.startswith(folder) and info.filename.lower().endswith((".png", ".jpg", ".jpeg")):
                body = data
            dst.writestr(info, body)
    return target
