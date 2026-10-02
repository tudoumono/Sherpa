"""WMF/EMF（Windows メタファイル）の描画命令から、文字と埋込ビットマップを決定的に取り出す。

AI も外部の道具も使わない（標準ライブラリ＋Pillow）。文字は描画命令（WMF の TEXTOUT/EXTTEXTOUT、EMF の
EXTTEXTOUTW/A・POLYTEXTOUTW/A、EMF+ の DrawString）に載っている文字列そのもの（原本の値）で、ANSI の文字列は
選択中フォントの文字セットから符号化を決める。ビットマップ（DIB）は PNG へ変換し、既存の OCR 経路へ渡す。
どんな入力でも例外を外へ出さない（失敗は ``reason`` に残す）。走査は記録数・バイト数・出力量で有界。
"""
from __future__ import annotations

import hashlib
import heapq
import io
import json
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

# 抽出規則の版。抽出結果（rag.md の「図の中の文字」・子 PNG）が変わる変更をしたら上げる。
METAFILE_EXTRACT_VERSION = "metafile-extract-v1"

MAX_METAFILE_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 500_000
MAX_TEXT_ITEMS = 2_000
MAX_TEXT_LINES = 400
MAX_TEXT_CHARS = 8_000
MAX_BITMAPS = 32
MAX_BITMAP_PIXELS = 25_000_000
MIN_BITMAP_SIDE = 32
SNIFF_BYTES = 64
CHILD_DIR = "_metafile"
RENDER_NAME = "render.png"                    # 図全体を LibreOffice で描いた PNG（``_metafile/{親hash}/`` の下）
RENDER_STATE_SUFFIX = ".render.json"          # ``_metafile/{親hash}.render.json``: 描画できなかった理由の記録
RENDER_CACHE_DIR = "_metafile_render_cache"    # 派生領域直下: {親sha256}.png（描画）／{親sha256}.failed（失敗の理由）
MAX_RENDER_PIXELS = 25_000_000
MIN_TEXT_CHARS_FOR_SKIP = 20                  # 埋込ビットマップがあり、文字がこれ以上取れた図は描画しない

_WMF_PLACEABLE = b"\xd7\xcd\xc6\x9a"
_EMF_SIGNATURE_OFFSET = 40

# LOGFONT の lfCharSet → Python のコーデック。None は文字を取り出さない（シンボルフォント）。
_CHARSET_CODECS: dict[int, str | None] = {
    0: "cp1252", 2: None, 128: "cp932", 129: "cp949", 130: "johab", 134: "cp936", 136: "cp950",
    161: "cp1253", 162: "cp1254", 163: "cp1258", 177: "cp1255", 178: "cp1256", 186: "cp1257",
    204: "cp1251", 222: "cp874", 238: "cp1250",
}
_FALLBACK_CODECS = ("cp932", "cp1252")

# WMF の関数番号
_WMF_MAX_OBJECTS = 65_535
_WMF_EOF = 0x0000
_WMF_DELETEOBJECT = 0x01F0
_WMF_SELECTOBJECT = 0x012D
_WMF_CREATE_OBJECTS = frozenset({
    0x00F7, 0x01F9, 0x0142, 0x02FA, 0x02FB, 0x02FC, 0x06FF, 0x02FD, 0x06FE,
})
_WMF_CREATEFONTINDIRECT = 0x02FB
_WMF_TEXTOUT = 0x0521
_WMF_EXTTEXTOUT = 0x0A32
_WMF_DIB_OFFSETS = {0x0F43: 22, 0x0B41: 20, 0x0940: 16, 0x0D33: 18}   # パラメータ部の長さ（rdParm 先頭からの DIB 位置）

# EMF のレコード種別
_EMR_EOF = 14
_EMR_COMMENT = 70
_EMR_SELECTOBJECT = 37
_EMR_DELETEOBJECT = 40
_EMR_EXTCREATEFONTINDIRECTW = 82
_EMR_EXTTEXTOUTA = 83
_EMR_EXTTEXTOUTW = 84
_EMR_POLYTEXTOUTA = 96
_EMR_POLYTEXTOUTW = 97
_EMR_SMALLTEXTOUT = 108
_ETO_NO_RECT = 0x100
_ETO_SMALL_CHARS = 0x200
_EMR_BITMAP_FIELDS = {          # type -> (offBmi の位置)。cbBmi/offBits/cbBits は続く 4 つの DWORD
    76: 84, 77: 84, 78: 84, 79: 96, 80: 48, 81: 48, 114: 84, 116: 84,
}
_EMFPLUS_DRAWSTRING = 0x401C


@dataclass(frozen=True)
class Bitmap:
    png: bytes
    width: int
    height: int
    source_sha256: str


@dataclass
class MetafileContent:
    kind: str | None = None
    lines: list[str] = field(default_factory=list)
    bitmaps: list[Bitmap] = field(default_factory=list)
    reason: str | None = None


@dataclass
class _Item:
    y: int
    x: int
    text: str
    plus: bool = False


def sniff(head: bytes) -> str | None:
    """先頭バイトから ``"wmf"``/``"emf"`` を返す。どちらでもなければ None。"""
    if head.startswith(_WMF_PLACEABLE):
        return "wmf"
    if len(head) >= 6 and head[0:2] in (b"\x01\x00", b"\x02\x00") and head[2:4] == b"\x09\x00" \
            and head[4:6] in (b"\x00\x01", b"\x00\x03"):
        return "wmf"
    if head[:4] == b"\x01\x00\x00\x00" and head[_EMF_SIGNATURE_OFFSET:_EMF_SIGNATURE_OFFSET + 4] == b" EMF":
        return "emf"
    return None


def _decode_ansi(raw: bytes, charset: int | None) -> str | None:
    raw = raw.split(b"\x00", 1)[0]
    if not raw:
        return ""
    if charset in _CHARSET_CODECS:
        codec = _CHARSET_CODECS[charset]
        if codec is None:
            return None
        return raw.decode(codec, errors="replace")
    for codec in _FALLBACK_CODECS:
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1252", errors="replace")


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(text: str) -> str:
    text = _CONTROL_RE.sub("", text).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = text.strip()
    if not text or not text.replace("�", "").strip():
        return ""
    return text


def _i16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<h", data, offset)[0]


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _i32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<i", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def dib_to_bmp(dib: bytes) -> bytes | None:
    """BITMAPINFO＋ビット列（DIB）に BITMAPFILEHEADER を付けた BMP を返す。解釈できなければ None。"""
    if len(dib) < 12:
        return None
    header_size = _u32(dib, 0)
    if header_size == 12:
        bit_count = _u16(dib, 10)
        palette = (1 << bit_count) * 3 if bit_count <= 8 else 0
        offset = 14 + header_size + palette
    elif header_size >= 40 and len(dib) >= 40:
        bit_count = _u16(dib, 14)
        compression = _u32(dib, 16)
        colors_used = _u32(dib, 32)
        palette = (colors_used or (1 << bit_count)) * 4 if bit_count <= 8 else colors_used * 4
        masks = 12 if header_size == 40 and compression == 3 else 16 if header_size == 40 and compression == 6 else 0
        offset = 14 + header_size + masks + palette
    else:
        return None
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, offset) + dib


def _bitmap_from_dib(dib: bytes, out: list[Bitmap], seen: set[str]) -> None:
    if len(out) >= MAX_BITMAPS or len(dib) < 16:
        return
    header_size = _u32(dib, 0)
    if header_size == 12:
        width, height = _u16(dib, 4), _u16(dib, 6)
    elif header_size >= 40 and len(dib) >= 12:
        width, height = _i32(dib, 4), abs(_i32(dib, 8))
    else:
        return
    if width < MIN_BITMAP_SIDE or height < MIN_BITMAP_SIDE or width * height > MAX_BITMAP_PIXELS:
        return
    digest = hashlib.sha256(dib).hexdigest()
    if digest in seen:
        return
    seen.add(digest)
    bmp = dib_to_bmp(dib)
    if bmp is None:
        return
    try:
        from PIL import Image
        with Image.open(io.BytesIO(bmp)) as image:
            image.load()
            rgb = image.convert("RGB")
            buffer = io.BytesIO()
            rgb.save(buffer, format="PNG")
    except Exception:
        return
    out.append(Bitmap(png=buffer.getvalue(), width=width, height=height, source_sha256=digest))


# ---- WMF ------------------------------------------------------------------------------------

def _parse_wmf(data: bytes, items: list[_Item], bitmaps: list[Bitmap], seen: set[str]) -> str | None:
    offset = 22 if data.startswith(_WMF_PLACEABLE) else 0
    if len(data) < offset + 18:
        return "truncated"
    object_limit = _u16(data, offset + 10) or _WMF_MAX_OBJECTS      # ヘッダの nObjects（同時に存在できる数）
    offset += _u16(data, offset + 2) * 2
    fonts: dict[int, int] = {}            # オブジェクト番号 -> lfCharSet
    live: set[int] = set()                # 使用中の番号
    freed: list[int] = []                 # 解放済みの番号（最小の空きから再利用する）
    next_slot = 0
    charset: int | None = None
    count = 0
    while True:
        if offset + 6 > len(data):
            return None if offset == len(data) else "truncated"
        size_words, function = struct.unpack_from("<IH", data, offset)
        size = size_words * 2
        if size < 6 or offset + size > len(data):
            return "truncated"
        count += 1
        if count > MAX_RECORDS or len(items) >= MAX_TEXT_ITEMS and len(bitmaps) >= MAX_BITMAPS:
            return "record_limit"
        if function == _WMF_EOF:
            return None
        body = data[offset + 6:offset + size]
        try:
            if function in _WMF_CREATE_OBJECTS:
                if len(live) >= min(object_limit, _WMF_MAX_OBJECTS):
                    return "object_table_limit"
                if freed:
                    slot = heapq.heappop(freed)
                else:
                    slot = next_slot
                    next_slot += 1
                live.add(slot)
                if function == _WMF_CREATEFONTINDIRECT and len(body) >= 14:
                    fonts[slot] = body[13]
            elif function == _WMF_DELETEOBJECT and len(body) >= 2:
                slot = _u16(body, 0)
                if slot in live:
                    live.discard(slot)
                    heapq.heappush(freed, slot)
                fonts.pop(slot, None)
            elif function == _WMF_SELECTOBJECT and len(body) >= 2:
                slot = _u16(body, 0)
                if slot in fonts:
                    charset = fonts[slot]
            elif function == _WMF_TEXTOUT and len(body) >= 2:
                length = _u16(body, 0)
                end = 2 + length
                if end <= len(body):
                    padded = end + (length & 1)
                    y, x = (_i16(body, padded), _i16(body, padded + 2)) if padded + 4 <= len(body) else (0, 0)
                    _add_ansi(items, body[2:end], charset, y, x)
            elif function == _WMF_EXTTEXTOUT and len(body) >= 8:
                y, x, length, options = _i16(body, 0), _i16(body, 2), _u16(body, 4), _u16(body, 6)
                start = 8 + (8 if options & 0x6 else 0)
                if start + length <= len(body):
                    _add_ansi(items, body[start:start + length], charset, y, x)
            elif function in _WMF_DIB_OFFSETS:
                start = _WMF_DIB_OFFSETS[function]
                if len(body) > start:
                    _bitmap_from_dib(body[start:], bitmaps, seen)
        except (struct.error, StopIteration):
            pass
        offset += size


def _add_ansi(items: list[_Item], raw: bytes, charset: int | None, y: int, x: int) -> None:
    if len(items) >= MAX_TEXT_ITEMS:
        return
    decoded = _decode_ansi(raw, charset)
    text = _clean(decoded or "")
    if text:
        items.append(_Item(y=y, x=x, text=text))


# ---- EMF / EMF+ -----------------------------------------------------------------------------

def _emf_text_at(record: bytes, ref_x: int, ref_y: int, chars: int, off_string: int, wide: bool,
                 charset: int | None, items: list[_Item]) -> None:
    if len(items) >= MAX_TEXT_ITEMS or chars <= 0 or off_string < 8:
        return
    length = chars * (2 if wide else 1)
    if off_string + length > len(record):
        return
    raw = record[off_string:off_string + length]
    decoded = raw.decode("utf-16-le", errors="replace") if wide else _decode_ansi(raw, charset)
    text = _clean(decoded or "")
    if text:
        items.append(_Item(y=ref_y, x=ref_x, text=text))


def _parse_emf_plus(record: bytes, items: list[_Item]) -> None:
    """EMR_COMMENT の EMF+ 記録から DrawString を取り出す（EMF+ 以外のコメントは何もしない）。"""
    if len(record) < 16 or record[12:16] != b"EMF+":
        return
    offset = 16
    end = min(len(record), 12 + _u32(record, 8))
    while offset + 12 <= end:
        kind, _flags, size, data_size = struct.unpack_from("<HHII", record, offset)
        if size < 12 or offset + size > len(record):
            break
        if kind == _EMFPLUS_DRAWSTRING and data_size >= 28:
            body = offset + 12
            length = _u32(record, body + 8)
            x, y = struct.unpack_from("<ff", record, body + 12)
            raw_end = body + 28 + length * 2
            if 0 < length <= 4096 and raw_end <= len(record) and len(items) < MAX_TEXT_ITEMS:
                text = _clean(record[body + 28:raw_end].decode("utf-16-le", errors="replace"))
                if text:
                    items.append(_Item(y=int(round(y)), x=int(round(x)), text=text, plus=True))
        offset += size


def _parse_emf(data: bytes, items: list[_Item], bitmaps: list[Bitmap], seen: set[str]) -> str | None:
    offset = 0
    fonts: dict[int, int] = {}
    charset: int | None = None
    count = 0
    while True:
        if offset + 8 > len(data):
            return None if offset == len(data) else "truncated"
        kind, size = struct.unpack_from("<II", data, offset)
        if size < 8 or size % 4 or offset + size > len(data):
            return "truncated"
        count += 1
        if count > MAX_RECORDS:
            return "record_limit"
        record = data[offset:offset + size]
        try:
            if kind == _EMR_EOF:
                return None
            if kind == _EMR_EXTCREATEFONTINDIRECTW and size >= 48:
                fonts[_u32(record, 8)] = record[35]
            elif kind == _EMR_SELECTOBJECT and size >= 12:
                handle = _u32(record, 8)
                if handle & 0x80000000:
                    charset = None
                elif handle in fonts:
                    charset = fonts[handle]
            elif kind == _EMR_DELETEOBJECT and size >= 12:
                fonts.pop(_u32(record, 8), None)
            elif kind in (_EMR_EXTTEXTOUTA, _EMR_EXTTEXTOUTW) and size >= 76:
                _emf_text_at(record, _i32(record, 36), _i32(record, 40), _u32(record, 44), _u32(record, 48),
                             kind == _EMR_EXTTEXTOUTW, charset, items)
            elif kind in (_EMR_POLYTEXTOUTA, _EMR_POLYTEXTOUTW) and size >= 40:
                strings = min(_u32(record, 36), 1024)
                for index in range(strings):
                    base = 40 + index * 40
                    if base + 40 > size:
                        break
                    _emf_text_at(record, _i32(record, base), _i32(record, base + 4), _u32(record, base + 8),
                                 _u32(record, base + 12), kind == _EMR_POLYTEXTOUTW, charset, items)
            elif kind == _EMR_SMALLTEXTOUT and size >= 36:
                options = _u32(record, 20)
                start = 36 if options & _ETO_NO_RECT else 52
                chars = _u32(record, 16)
                small = bool(options & _ETO_SMALL_CHARS)
                if len(items) < MAX_TEXT_ITEMS and 0 < chars <= 65_535 \
                        and start + chars * (1 if small else 2) <= size:
                    raw = record[start:start + chars * (1 if small else 2)]
                    decoded = _decode_ansi(raw, charset) if small else raw.decode("utf-16-le", errors="replace")
                    text = _clean(decoded or "")
                    if text:
                        items.append(_Item(y=_i32(record, 12), x=_i32(record, 8), text=text))
            elif kind == _EMR_COMMENT:
                _parse_emf_plus(record, items)
            elif kind in _EMR_BITMAP_FIELDS and len(bitmaps) < MAX_BITMAPS:
                base = _EMR_BITMAP_FIELDS[kind]
                if size >= base + 16:
                    off_bmi, cb_bmi, off_bits, cb_bits = struct.unpack_from("<IIII", record, base)
                    if (cb_bmi and cb_bits and off_bmi + cb_bmi <= size and off_bits + cb_bits <= size):
                        _bitmap_from_dib(record[off_bmi:off_bmi + cb_bmi] + record[off_bits:off_bits + cb_bits],
                                         bitmaps, seen)
        except (struct.error, IndexError):
            pass
        offset += size


# ---- 公開 API -------------------------------------------------------------------------------

def _order_lines(items: list[_Item]) -> list[str]:
    """描画順を保って重複を除き、同じ y の文字を x 順に並べた行にする（上から下）。

    EMF+ の DrawString が含まれる（デュアル）場合、同じ文字列を持つ EMF 側の描画は二重にしない。
    """
    plus_texts = {item.text for item in items if item.plus}
    seen: set[tuple[int, int, str]] = set()
    rows: dict[int, list[_Item]] = {}
    for item in items:
        if not item.plus and item.text in plus_texts:
            continue
        key = (item.y, item.x, item.text)
        if key in seen:
            continue
        seen.add(key)
        rows.setdefault(item.y, []).append(item)
    lines: list[str] = []
    for y in sorted(rows):
        lines.append(" ".join(item.text for item in sorted(rows[y], key=lambda it: it.x)))
    return lines


def _cap_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    total = 0
    for line in lines[:MAX_TEXT_LINES]:
        if total + len(line) > MAX_TEXT_CHARS:
            line = line[: max(0, MAX_TEXT_CHARS - total)]
            if line:
                result.append(line)
            break
        result.append(line)
        total += len(line)
    return result


def extract(data: bytes) -> MetafileContent:
    """WMF/EMF の bytes から文字行と埋込ビットマップ（PNG）を取り出す。例外は外へ出さない。"""
    kind = sniff(data[:SNIFF_BYTES])
    result = MetafileContent(kind=kind)
    if kind is None:
        result.reason = "not_metafile"
        return result
    if len(data) > MAX_METAFILE_BYTES:
        result.reason = "too_large"
        return result
    items: list[_Item] = []
    bitmaps: list[Bitmap] = []
    try:
        reason = (_parse_wmf if kind == "wmf" else _parse_emf)(data, items, bitmaps, set())
    except Exception as exc:                      # 壊れた入力で取り込みを止めない
        reason = f"parse_error:{exc.__class__.__name__}"
    result.lines = _cap_lines(_order_lines(items))
    result.bitmaps = bitmaps
    result.reason = reason
    return result


def read_asset_content(path: Path) -> MetafileContent | None:
    """資産ファイルが WMF/EMF なら抽出結果を返す（そうでなければ None）。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            head = stream.read(SNIFF_BYTES)
            if sniff(head) is None:
                return None
            if size > MAX_METAFILE_BYTES:
                return MetafileContent(kind=sniff(head), reason="too_large")
            data = head + stream.read()
    except OSError:
        return None
    return extract(data)


def _render_wanted(content: MetafileContent) -> bool:
    """全体描画をするか。埋込ビットマップが無い図、または文字がほとんど取れなかった図だけ描く。

    ビットマップがあり文字も十分取れた図は、描画しても同じ内容の OCR が重なるだけなので描かない。
    """
    if not content.bitmaps:
        return True
    return sum(len(line) for line in content.lines) < MIN_TEXT_CHARS_FOR_SKIP


def _valid_render(png: bytes) -> bool:
    """出力が本物の PNG で、画素数が上限内かを確かめる。"""
    size = child_png_size(png[:32])
    if size is None or size[0] < 1 or size[1] < 1 or size[0] * size[1] > MAX_RENDER_PIXELS:
        return False
    try:
        from PIL import Image
        with Image.open(io.BytesIO(png)) as image:
            image.verify()
    except Exception:
        return False
    return True


def _state_path(root: Path, parent_hex: str) -> Path:
    return root / CHILD_DIR / f"{parent_hex}{RENDER_STATE_SUFFIX}"


def read_render_state_record(root: Path, parent_hex: str) -> dict | None:
    try:
        value = json.loads(_state_path(root, parent_hex).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and isinstance(value.get("state"), str) else None


def read_render_state(root: Path, parent_hex: str) -> str | None:
    """描画の状態（``pending`` / ``unavailable`` / ``failed:{理由}``）。記録が無ければ None。"""
    record = read_render_state_record(root, parent_hex)
    return record["state"] if record else None


def _write_state(root: Path, parent_hex: str, state: str | None, file: str | None = None) -> None:
    path = _state_path(root, parent_hex)
    try:
        if state is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"state": state, "file": file}), encoding="utf-8")
    except OSError:
        pass


def render_cache_dir(assets_dir: str | Path) -> Path:
    """描画 PNG を親 sha256 で共有する置き場（派生領域の直下・``rag`` 層の兄弟。資料フォルダ側には作らない）。"""
    root = Path(assets_dir)
    for ancestor in root.parents:
        if ancestor.name in ("rag", "rag.staging", "rag.retired"):
            return ancestor.parent / RENDER_CACHE_DIR
    return root.parent / RENDER_CACHE_DIR


def cached_render(cache_dir: Path, parent_hex: str) -> bytes | None:
    try:
        png = (cache_dir / f"{parent_hex}.png").read_bytes()
    except OSError:
        return None
    return png if _valid_render(png) else None


def cached_failure(cache_dir: Path, parent_hex: str) -> str | None:
    try:
        return (cache_dir / f"{parent_hex}.failed").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def clear_render_state(root: Path, parent_hex: str) -> None:
    _write_state(root, parent_hex, None)


def _materialize_render(root: Path, name: str, kind: str, parent_hex: str, *, keep_state: bool = False) -> int:
    """図全体の描画 PNG を ``_metafile/{親hash}/render.png`` へ置く。LibreOffice はここでは呼ばない。

    共有キャッシュ（親 hash）に描画があればそれを写す。無ければ LibreOffice の有無で ``unavailable``／
    ``pending`` を記録し、描画はバックグラウンド（``metafile_render``）が行う。過去に失敗した図は
    ``failed:{理由}`` のまま再試行しない。
    """
    target = root / CHILD_DIR / parent_hex / RENDER_NAME
    if target.is_file():
        return 0
    cache = render_cache_dir(root)
    png = cached_render(cache, parent_hex)
    if png is not None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(png)
        except OSError:
            return 0
        # keep_state: バックグラウンドの反映では、ルート書き直しと OCR job の enqueue が済むまで
        # 再試行できる状態（rendered_unapplied）を残す（呼び出し元が成功後に消す）。
        _write_state(root, parent_hex, "rendered_unapplied" if keep_state else None, name)
        return 1
    failure = cached_failure(cache, parent_hex)
    if failure is not None:
        _write_state(root, parent_hex, f"failed:{failure}")
        return 0
    from .arms import legacy_convert

    _write_state(root, parent_hex, "pending" if legacy_convert.soffice_available() else "unavailable", name)
    return 0


def materialize_children(assets_dir: str | Path, *, keep_state: bool = False) -> int:
    """``{rel}.assets/`` 内の WMF/EMF から埋込ビットマップを PNG にして ``_metafile/{親hash}/`` へ書く。

    親（メタファイル）の hash 名ディレクトリの下に連番＋PNG hash で置くため、再実行しても同じ名前になる。
    全体の描画が要る図（``_render_wanted``）は、共有キャッシュから写すか、状態（``pending``／``unavailable``）
    だけを記録する（LibreOffice は呼ばない）。書くのは派生物の assets 内だけ（登録した資料フォルダには
    触れない）。失敗は握りつぶして次へ進む。
    """
    root = Path(assets_dir)
    if not root.is_dir():
        return 0
    written = 0
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        content = read_asset_content(path)
        if content is None or content.kind is None:
            continue
        try:
            parent_hex = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        target_dir = root / CHILD_DIR / parent_hex
        try:
            if content.bitmaps:
                target_dir.mkdir(parents=True, exist_ok=True)
            for index, bitmap in enumerate(content.bitmaps, start=1):
                name = f"{index:02d}-{hashlib.sha256(bitmap.png).hexdigest()[:16]}.png"
                target = target_dir / name
                if not target.is_file():
                    target.write_bytes(bitmap.png)
                    written += 1
        except OSError:
            continue
        if content.reason == "too_large":
            _write_state(root, parent_hex, "too_large", path.name)    # 読まない理由をルートに残す（描画もしない）
        elif _render_wanted(content):
            try:
                written += _materialize_render(root, path.name, content.kind, parent_hex, keep_state=keep_state)
            except Exception:
                pass
    return written


# ---- Office パッケージ内の図の位置（人間向け MD 用）-----------------------------------------------

_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_SML_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _rels(archive: zipfile.ZipFile, part: str) -> dict[str, str]:
    base = PurePosixPath(part)
    rels_name = (base.parent / "_rels" / (base.name + ".rels")).as_posix()
    try:
        root = ET.fromstring(archive.read(rels_name))
    except (KeyError, ET.ParseError):
        return {}
    out: dict[str, str] = {}
    for rel in root.iter(f"{_REL_NS}Relationship"):
        rel_id, target = rel.get("Id"), rel.get("Target")
        if not rel_id or not target or rel.get("TargetMode") == "External":
            continue
        resolved = PurePosixPath(target.lstrip("/")) if target.startswith("/") else base.parent / target
        parts: list[str] = []
        for piece in resolved.parts:
            if piece == "..":
                if parts:
                    parts.pop()
            elif piece != ".":
                parts.append(piece)
        out[rel_id] = "/".join(parts)
    return out


def _part_lines(archive: zipfile.ZipFile, media_part: str, cache: dict[str, list[str]]) -> list[str]:
    if media_part in cache:
        return cache[media_part]
    lines: list[str] = []
    try:
        info = archive.getinfo(media_part)
        if info.file_size <= MAX_METAFILE_BYTES:
            data = archive.read(media_part)
            if sniff(data[:SNIFF_BYTES]) is not None:
                lines = extract(data).lines
    except (KeyError, OSError, zipfile.BadZipFile):
        lines = []
    cache[media_part] = lines
    return lines


@dataclass(frozen=True)
class PlacedFigure:
    """Office 文書内の、文字を持つメタファイル図 1 つ。

    ``anchor``: docx は (段落数の上限, 表数の上限)＝「本文の段落 index < 上限、表 index < 上限」の要素までが
    この図より前。xlsx は (行, 列)（1 始まりのアンカーセル）。位置を決められない図は None。
    """

    lines: list[str]
    anchor: tuple[int, int] | None = None


_R_ATTRS = (f"{_R_NS}embed", f"{_R_NS}id", f"{_R_NS}pict")


def _media_in(element: ET.Element, rels: dict[str, str]) -> list[str]:
    """``element`` 以下が出現順に参照するメディアパート（重複を含む）。"""
    found: list[str] = []
    for node in element.iter():
        for name, value in node.attrib.items():
            if name in _R_ATTRS and value in rels and "/media/" in rels[value]:
                found.append(rels[value])
    return found


def docx_figure_texts(path: str | Path) -> list[PlacedFigure]:
    """docx 本文に出現する順の、文字を持つメタファイル図（本文の直下の段落・表ごとに位置を付ける）。"""
    w_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    cache: dict[str, list[str]] = {}
    out: list[PlacedFigure] = []
    try:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
            rels = _rels(archive, "word/document.xml")
            body = root.find(f"{w_ns}body")
            if body is None:
                return []
            paragraphs = tables = 0
            for child in body:
                if child.tag == f"{w_ns}p":
                    anchor = (paragraphs + 1, tables)
                    paragraphs += 1
                elif child.tag == f"{w_ns}tbl":
                    anchor = (paragraphs, tables + 1)
                    tables += 1
                else:
                    continue
                for part in _media_in(child, rels):
                    lines = _part_lines(archive, part, cache)
                    if lines:
                        out.append(PlacedFigure(lines=lines, anchor=anchor))
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile, ValueError):
        return []
    return out


def pptx_slide_figures(path: str | Path, slide_part: str, cache: dict[str, list[str]] | None = None
                       ) -> dict[int, list[list[str]]]:
    """スライドの ``p:spTree`` 直下の子の位置 → その子（図・グループ等）が持つメタファイル図の文字行。"""
    p_ns = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
    cache = {} if cache is None else cache
    out: dict[int, list[list[str]]] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read(slide_part))
            rels = _rels(archive, slide_part)
            tree = root.find(f"{p_ns}cSld/{p_ns}spTree")
            if tree is None:
                return {}
            for index, child in enumerate(tree):
                for part in _media_in(child, rels):
                    lines = _part_lines(archive, part, cache)
                    if lines:
                        out.setdefault(index, []).append(lines)
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile, ValueError):
        return {}
    return out


_XDR_NS = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"


def _drawing_figures(archive: zipfile.ZipFile, drawing_part: str, cache: dict[str, list[str]]) -> list[PlacedFigure]:
    try:
        root = ET.fromstring(archive.read(drawing_part))
    except (KeyError, ET.ParseError):
        return []
    rels = _rels(archive, drawing_part)
    out: list[PlacedFigure] = []
    for anchor_el in root:
        origin = anchor_el.find(f"{_XDR_NS}from")
        anchor = None
        if origin is not None:
            try:
                anchor = (int(origin.findtext(f"{_XDR_NS}row", "")) + 1, int(origin.findtext(f"{_XDR_NS}col", "")) + 1)
            except ValueError:
                anchor = None
        for part in _media_in(anchor_el, rels):
            lines = _part_lines(archive, part, cache)
            if lines:
                out.append(PlacedFigure(lines=lines, anchor=anchor))
    return out


def xlsx_figure_texts(path: str | Path) -> dict[str, list[PlacedFigure]]:
    """シート名 → そのシートの図（drawing のアンカー順）のうち、文字を持つメタファイル図。"""
    cache: dict[str, list[str]] = {}
    out: dict[str, list[PlacedFigure]] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            except (KeyError, ET.ParseError):
                return {}
            workbook_rels = _rels(archive, "xl/workbook.xml")
            for sheet in workbook.findall(f"{_SML_NS}sheets/{_SML_NS}sheet"):
                name, rel_id = sheet.get("name"), sheet.get(f"{_R_NS}id")
                sheet_part = workbook_rels.get(rel_id or "")
                if not name or not sheet_part:
                    continue
                try:
                    sheet_root = ET.fromstring(archive.read(sheet_part))
                except (KeyError, ET.ParseError):
                    continue
                drawing = sheet_root.find(f"{_SML_NS}drawing")
                drawing_part = _rels(archive, sheet_part).get(drawing.get(f"{_R_NS}id") or "") \
                    if drawing is not None else None
                if not drawing_part:
                    continue
                figures = _drawing_figures(archive, drawing_part, cache)
                if figures:
                    out[name] = figures
    except (OSError, zipfile.BadZipFile, ValueError):
        return {}
    return out


def child_png_size(head: bytes) -> list[int] | None:
    """PNG の IHDR から [幅, 高さ] を読む（読めなければ None）。"""
    if len(head) >= 24 and head.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height = struct.unpack(">II", head[16:24])
        return [width, height]
    return None
