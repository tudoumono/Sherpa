"""埋め込み画像のバイト列から、画像に透過があるかを塗りの区分で返す純関数。

PNG は透過を含む画素が 1 つでもあれば transparent_png、全画素が不透明なら opaque。画素ごとの重なりは計算しない。
外部通信・Pillow・numpy は使わず、標準ライブラリだけで読む（アルファ値の列だけを復元する）。
設計: docs/proposals/2026-10-22-Office観測の共通の扱い.md §1.4
"""
from __future__ import annotations

import struct
import zlib
from itertools import accumulate
from operator import add

from .observation_vocab import FILL_OPAQUE, FILL_TRANSPARENT_PNG, FILL_UNKNOWN

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_COLOR_GRAY_ALPHA = 4
_COLOR_RGBA = 6

# 透過を含むかを画素で確かめる画像の上限。超える画像は読まずに unknown にする。
MAX_PIXELS = 40_000_000
MAX_RAW_BYTES = 64 * 1024 * 1024  # 展開後（フィルタ種別の 1 バイトを含む）の大きさ


def image_fill_kind(data: bytes | None) -> str:
    """画像のバイト列から塗りの区分を返す。

    PNG は、tRNS があるか、アルファ値が 255 未満の画素があれば `transparent_png`。アルファの無い PNG と、アルファが全画素 255 の PNG は `opaque`。
    JPEG は `opaque`。読めない・壊れている・上限を超える・上記以外の形式は `unknown`。
    """
    if not data:
        return FILL_UNKNOWN
    if data.startswith(_PNG_SIGNATURE):
        return _png_fill_kind(data)
    if data.startswith(_JPEG_SIGNATURE):
        return FILL_OPAQUE
    return FILL_UNKNOWN


def _png_fill_kind(data: bytes) -> str:
    """PNG のチャンクを歩く。アルファ付きの形式は IDAT を集め、アルファの列を復元して全画素の不透明を確かめる。"""
    position = len(_PNG_SIGNATURE)
    header: tuple[int, int, int, int, int] | None = None  # 幅・高さ・ビット深度・カラータイプ・インターレース
    idat: list[bytes] = []
    while position + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        body_start = position + 8
        if body_start + length > len(data):
            return FILL_UNKNOWN
        body = data[body_start:body_start + length]
        if header is None:
            if kind != b"IHDR" or length < 13:
                return FILL_UNKNOWN
            header = struct.unpack(">IIBBxxB", body[:13])
        elif kind == b"tRNS":
            return FILL_TRANSPARENT_PNG
        elif kind == b"IDAT":
            if header[3] not in (_COLOR_GRAY_ALPHA, _COLOR_RGBA):
                return FILL_OPAQUE
            idat.append(body)
        elif kind == b"IEND":
            break
        position = body_start + length + 4  # 本体の後ろの CRC を読み飛ばす
    if header is None or not idat:
        return FILL_UNKNOWN
    return _alpha_png_fill_kind(header, b"".join(idat))


def _alpha_png_fill_kind(header: tuple[int, int, int, int, int], compressed: bytes) -> str:
    width, height, bit_depth, color_type, interlace = header
    if interlace != 0 or bit_depth not in (8, 16) or width <= 0 or height <= 0:
        return FILL_UNKNOWN
    channels = 2 if color_type == _COLOR_GRAY_ALPHA else 4
    bytes_per_pixel = channels * bit_depth // 8
    stride = width * bytes_per_pixel
    expected = height * (1 + stride)
    if width * height > MAX_PIXELS or expected > MAX_RAW_BYTES:
        return FILL_UNKNOWN
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(compressed, expected)
    except zlib.error:
        return FILL_UNKNOWN
    if len(raw) != expected:
        return FILL_UNKNOWN
    alpha_lanes = list(range(bytes_per_pixel - bit_depth // 8, bytes_per_pixel))
    opaque = _alpha_lanes_all_opaque(raw, width, height, bytes_per_pixel, alpha_lanes)
    if opaque is None:
        return FILL_UNKNOWN
    return FILL_OPAQUE if opaque else FILL_TRANSPARENT_PNG


def _alpha_lanes_all_opaque(raw: bytes, width: int, height: int, bytes_per_pixel: int, lanes: list[int]) -> bool | None:
    """アルファの各バイトの列（PNG のフィルタは列ごとに独立）を復元し、全画素が不透明なら True。1 画素でも透過があれば即 False。不正なフィルタは None。"""
    stride = width * bytes_per_pixel
    previous = [bytes(width) for _ in lanes]
    position = 0
    for _ in range(height):
        filter_type = raw[position]
        row = raw[position + 1:position + 1 + stride]
        position += 1 + stride
        for index, lane in enumerate(lanes):
            current = _unfilter(filter_type, row[lane::bytes_per_pixel], previous[index])
            if current is None:
                return None
            if current.count(255) != width:
                return False
            previous[index] = current
    return True


def _unfilter(filter_type: int, line: bytes, above: bytes) -> bytes | None:
    """1 本のバイト列（隣のバイトは 1 つ前）に対する PNG のフィルタの復元。"""
    if filter_type == 0:
        return line
    if filter_type == 1:
        return bytes(map((255).__and__, accumulate(line)))
    if filter_type == 2:
        return bytes(map((255).__and__, map(add, line, above)))
    if filter_type not in (3, 4):
        return None
    out = bytearray(len(line))
    left = upper_left = 0
    for i, value in enumerate(line):
        up = above[i]
        if filter_type == 3:
            left = (value + ((left + up) >> 1)) & 255
        else:
            estimate = left + up - upper_left
            distance_left, distance_up, distance_upper_left = abs(estimate - left), abs(estimate - up), abs(estimate - upper_left)
            if distance_left <= distance_up and distance_left <= distance_upper_left:
                predictor = left
            elif distance_up <= distance_upper_left:
                predictor = up
            else:
                predictor = upper_left
            left = (value + predictor) & 255
        out[i] = left
        upper_left = up
    return bytes(out)
