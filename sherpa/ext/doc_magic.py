"""GET /ext/v1/doc の形式検証: 拡張子ごとの先頭バイト・OOXML の EOCD/central directory の bounded 検査・legacy Office の CFB ヘッダ。
設計: docs/design/external-api.md
"""
from __future__ import annotations

import os
import struct

from sherpa.ext import zipsafe
from sherpa.ingest import office_md

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # 旧バイナリ Office（OLE2/CFB）共通シグネチャ


# ---- マジック検証 ----
# - legacy Office（.doc/.xls/.ppt）は CFB ヘッダの健全性のみ確認する（stream 列挙・形式判別・入れ子解析はしない）。Content-Type は application/octet-stream 固定。
# - OOXML は EOCD（末尾の central directory 終端レコード）＋central directory 自体を bounded に検証し（メンバ数上限・ZIP64・multi-disk・境界整合を含む）、
#   メンバー名もその検証済みの走査から直接得る（`zipfile.ZipFile` に central directory を再解析させない）

_IMAGE_MAGIC_BY_EXT = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".bmp": (b"BM",),
    # TIFF: classic（version 42）と BigTIFF（version 43）の両方を受理する
    ".tif": (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"),
    ".tiff": (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"),
}

_OOXML_MAIN_PART = {
    ".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}

_ZIP_EOCD_SIG = b"PK\x05\x06"
_ZIP_EOCD_SIZE = 22
_ZIP_EOCD_MAX_COMMENT = 65535  # ZIP スペック上の comment 長の上限（2 バイトフィールド）
_ZIP64_SENTINEL = 0xFFFF  # EOCD の 16bit entry 数フィールドがこの値＝ZIP64（別レコード）
_ZIP64_SENTINEL32 = 0xFFFFFFFF  # EOCD の 32bit cd_size/cd_offset がこの値＝同上
_ZIP_CD_ENTRY_SIG = b"PK\x01\x02"  # central directory file header の署名
_ZIP_CD_ENTRY_FIXED_SIZE = 46  # 可変長フィールド（filename/extra/comment）より前の固定部

# 複数 EOCD 候補を右から左へ試す際の合算上限。候補数・全候補合算の entry 数・pread バイト数のいずれかを超えたら、それ以上候補を試さずアーカイブ全体を拒否する（偽 EOCD を並べた DoS 対策）
_ZIP_MAX_EOCD_CANDIDATES = 8
_ZIP_MAX_TOTAL_ENTRIES_WALKED = 20_000
_ZIP_MAX_TOTAL_BYTES_READ = 8 * 1024 * 1024  # 8MiB


class _ZipScanBudget:
    """複数の EOCD 候補を試す際の合算走査量（候補数・entry 数・pread バイト数）を数える実測カウンタ。

    いずれかの上限を超えると `exceeded=True` になり、以後の `note_*()` は全て False を返す（呼び出し元は候補の構造不正による次候補への進行とは区別して全体を拒否する）。
    """

    __slots__ = ("candidates_tried", "entries_walked", "bytes_read", "exceeded")

    def __init__(self) -> None:
        self.candidates_tried = 0
        self.entries_walked = 0
        self.bytes_read = 0
        self.exceeded = False

    def note_candidate(self) -> bool:
        self.candidates_tried += 1
        if self.candidates_tried > _ZIP_MAX_EOCD_CANDIDATES:
            self.exceeded = True
        return not self.exceeded

    def note_entry(self) -> bool:
        self.entries_walked += 1
        if self.entries_walked > _ZIP_MAX_TOTAL_ENTRIES_WALKED:
            self.exceeded = True
        return not self.exceeded

    def note_read(self, n: int) -> bool:
        self.bytes_read += n
        if self.bytes_read > _ZIP_MAX_TOTAL_BYTES_READ:
            self.exceeded = True
        return not self.exceeded


def _iter_eocd_candidates(tail: bytes):
    """`tail`（ファイル末尾の bounded read）の中の `PK\x05\x06` 出現を右から左へ `(idx, eocd)` として yield する。

    申告された comment_len が実際に EOF まで一致する（EOCD として形が成立しうる）候補だけを yield する。central directory との整合は検証せず、
    呼び出し元（`_zip_bounded_check_names`）が候補ごとに同一の parser で最後まで検証して最初に通過したものを採用する。
    """
    end = len(tail)
    while True:
        idx = tail.rfind(_ZIP_EOCD_SIG, 0, end)
        if idx == -1:
            return
        if len(tail) - idx >= _ZIP_EOCD_SIZE:
            eocd = tail[idx:idx + _ZIP_EOCD_SIZE]
            comment_len = struct.unpack_from("<H", eocd, 20)[0]
            if idx + _ZIP_EOCD_SIZE + comment_len == len(tail):
                yield idx, eocd
        end = idx


_ZIP_CD_DISK_NUMBER_OFFSET = 34  # disk number where file starts（2バイト）
_ZIP_CD_COMPRESSED_SIZE_OFFSET = 20  # compressed size（4バイト）
_ZIP_CD_UNCOMPRESSED_SIZE_OFFSET = 24  # uncompressed size（4バイト）
_ZIP_CD_LOCAL_HEADER_OFFSET_OFFSET = 42  # local header offset（4バイト）
_ZIP64_EXTRA_TAG = 0x0001  # extra field 内の ZIP64 拡張情報サブレコードの tag


def _cd_entry_extra_has_zip64(extra: bytes) -> bool:
    """central directory entry の "extra" フィールドに ZIP64 拡張情報（tag `0x0001`）が含まれるか。サブレコード列が壊れている場合も True（安全側＝拒否）。"""
    pos, n = 0, len(extra)
    while pos + 4 <= n:
        tag, size = struct.unpack_from("<HH", extra, pos)
        if tag == _ZIP64_EXTRA_TAG:
            return True
        pos += 4 + size
    return pos != n


def _zip_count_central_directory_entries(
    fd: int, cd_offset: int, cd_size: int, budget: _ZipScanBudget
) -> tuple[int, frozenset[bytes]] | None:
    """central directory（`cd_offset` から `cd_size` バイト）を1件ずつ上限付き exact-read で逐次走査し、実際の entry 数と各 entry のファイル名（生バイト列）を返す。

    `budget` は複数候補にまたがって共有する合算カウンタ。`note_entry()` は各 entry のループ先頭（pread の前）で、pread ごとに `note_read()` を呼び、
    合算上限を超えたら即座に None を返す（呼び出し元が `budget.exceeded` を見て他の候補も試さない）。
    次のいずれかで None（拒否）: signature 不一致・可変長フィールドの合計が `cd_size` と不整合・`_ZIP_MAX_MEMBERS` 超過／`disk number start != 0`／
    `compressed_size`/`uncompressed_size`/`local_header_offset` のいずれかが `0xFFFFFFFF`（ZIP64 sentinel）／extra に ZIP64 拡張情報／合算走査量が上限超過。
    """
    if cd_size == 0:
        return 0, frozenset()
    pos = 0
    count = 0
    names: set[bytes] = set()
    while pos < cd_size:
        if not budget.note_entry():
            return None  # 合算 entry 数上限に既に達している＝この entry の pread は行わない
        if cd_size - pos < _ZIP_CD_ENTRY_FIXED_SIZE:
            return None
        if not budget.note_read(_ZIP_CD_ENTRY_FIXED_SIZE):
            return None
        header = os.pread(fd, _ZIP_CD_ENTRY_FIXED_SIZE, cd_offset + pos)
        if len(header) != _ZIP_CD_ENTRY_FIXED_SIZE or header[:4] != _ZIP_CD_ENTRY_SIG:
            return None
        compressed_size = struct.unpack_from("<I", header, _ZIP_CD_COMPRESSED_SIZE_OFFSET)[0]
        uncompressed_size = struct.unpack_from("<I", header, _ZIP_CD_UNCOMPRESSED_SIZE_OFFSET)[0]
        disk_number_start = struct.unpack_from("<H", header, _ZIP_CD_DISK_NUMBER_OFFSET)[0]
        local_header_offset = struct.unpack_from("<I", header, _ZIP_CD_LOCAL_HEADER_OFFSET_OFFSET)[0]
        n, m, k = struct.unpack_from("<HHH", header, 28)
        if disk_number_start != 0:
            return None  # multi-disk archive は拒否
        if _ZIP64_SENTINEL32 in (compressed_size, uncompressed_size, local_header_offset):
            return None  # ZIP64 sentinel（実値は extra フィールド）
        if cd_size - pos - _ZIP_CD_ENTRY_FIXED_SIZE < n + m + k:
            return None  # 可変長フィールドの合計が cd_size をはみ出す
        if n > 0:
            if not budget.note_read(n):
                return None
            fname = os.pread(fd, n, cd_offset + pos + _ZIP_CD_ENTRY_FIXED_SIZE)
            if len(fname) != n:
                return None
            names.add(fname)
        if m > 0:
            if not budget.note_read(m):
                return None
            extra = os.pread(fd, m, cd_offset + pos + _ZIP_CD_ENTRY_FIXED_SIZE + n)
            if len(extra) != m or _cd_entry_extra_has_zip64(extra):
                return None
        pos += _ZIP_CD_ENTRY_FIXED_SIZE + n + m + k
        count += 1
        if count > zipsafe._ZIP_MAX_MEMBERS:
            return None
    return (count, frozenset(names)) if pos == cd_size else None


def _zip_bounded_check_names(fd: int, size: int) -> frozenset[bytes] | None:
    """EOCD の候補を右から左へ順に試し、同一 parser で EOCD の全フィールドと central directory 自体（`_ZIP_MAX_MEMBERS` 超・ZIP64・multi-disk・`cd_offset+cd_size` の境界・実 entry 数の一致）を完全に検証できた最初の候補を採用し、そのメンバー名の集合を返す（どの候補も通過しなければ None＝アーカイブ全体を拒否）。

    rightmost が central directory と整合しない場合も、全体拒否せず次の候補（さらに左）を試す。EOCD の自己申告件数は信用せず、central directory を走査して実 entry 数を確定してから突き合わせる。
    `budget` は central directory walker を呼ぶ直前（EOCD フィールドの定数時間チェックを通過した候補）にだけ候補数を課金し、合算上限を超えたら即座にアーカイブ全体を拒否する。
    """
    if size < _ZIP_EOCD_SIZE:
        return None
    tail_size = min(size, _ZIP_EOCD_SIZE + _ZIP_EOCD_MAX_COMMENT)
    tail = os.pread(fd, tail_size, size - tail_size)
    budget = _ZipScanBudget()
    for idx, eocd in _iter_eocd_candidates(tail):
        eocd_abs_offset = (size - tail_size) + idx
        disk_number, disk_with_cd, entries_this_disk, total_entries = struct.unpack_from(
            "<HHHH", eocd, 4)
        cd_size, cd_offset = struct.unpack_from("<II", eocd, 12)
        if disk_number != 0 or disk_with_cd != 0 or entries_this_disk != total_entries:
            continue  # multi-disk は不採用
        if (total_entries == _ZIP64_SENTINEL or cd_size == _ZIP64_SENTINEL32
                or cd_offset == _ZIP64_SENTINEL32):
            continue  # ZIP64 sentinel（実値は別レコード）
        if total_entries > zipsafe._ZIP_MAX_MEMBERS:
            continue
        if cd_offset + cd_size != eocd_abs_offset:
            continue  # central directory は EOCD の直前で終わっているはず（prepended data 等は不採用）
        if not budget.note_candidate():
            return None  # 候補数の合算上限を超過＝アーカイブ全体を拒否
        walked = _zip_count_central_directory_entries(fd, cd_offset, cd_size, budget)
        if walked is None:
            if budget.exceeded:
                return None  # entry 数/バイト数の合算上限を超過
            continue
        actual_count, names = walked
        if actual_count == total_entries:
            return names
    return None


def _zip_bounded_check(fd: int, size: int) -> bool:
    """`_zip_bounded_check_names` の合否のみを返す薄いラッパー。"""
    return _zip_bounded_check_names(fd, size) is not None


def _ooxml_magic_ok(fd: int, ext: str, size: int) -> bool:
    """OOXML（.docx/.xlsx/.pptx）: EOCD と central directory の bounded 検査を通過し、検証済みの走査で得たメンバー名が `[Content_Types].xml` と形式固有 main part を含むか。
    `zipfile.ZipFile` に central directory を再解析させない。fd は `os.pread` のみで動かす。
    """
    main_part = _OOXML_MAIN_PART.get(ext)
    if main_part is None:
        return False
    names = _zip_bounded_check_names(fd, size)
    if names is None:
        return False
    return b"[Content_Types].xml" in names and main_part.encode("ascii") in names


def _legacy_office_header_ok(data: bytes) -> bool:
    """.doc/.xls/.ppt: CFB（[MS-CFB]）ヘッダの署名＋version/byte order/sector shift の健全性だけを見る。OLE2 でない場合は旧形式とみなし拒否しない（拡張子で既にゲート済み）。"""
    if data[:8] != _OLE2_MAGIC:
        return True
    if len(data) < 512:
        return False  # 512 バイトヘッダ未満＝壊れている
    try:
        minor, major = struct.unpack_from("<HH", data, 24)
        byte_order = struct.unpack_from("<H", data, 28)[0]
        sector_shift = struct.unpack_from("<H", data, 30)[0]
    except struct.error:
        return False
    del minor
    if byte_order != 0xFFFE:
        return False
    if major == 3:
        return sector_shift == 9  # 512 バイトセクタ
    if major == 4:
        return sector_shift == 12  # 4096 バイトセクタ
    return False


def _doc_magic_ok(fd: int, ext: str, header: bytes, size: int) -> bool:
    """拡張子ごとに形式固有の検証を行う（バイナリ形式のみ。text 系はマジック無しのため対象外）。"""
    if ext in office_md.PDF_EXT:
        return header.startswith(b"%PDF-")
    if ext in office_md.CONVERTIBLE_EXT:
        return _ooxml_magic_ok(fd, ext, size)
    if ext in office_md.LEGACY_OFFICE_EXT:
        return _legacy_office_header_ok(os.pread(fd, 512, 0))
    if ext in office_md.IMAGE_EXT:
        sigs = _IMAGE_MAGIC_BY_EXT.get(ext)
        return bool(sigs) and any(header.startswith(s) for s in sigs)
    return True


def _looks_utf8(data: bytes) -> bool:
    """厳密な UTF-8 検証（incremental decoder・全文）。ファイル全体が上限（64KiB）以下のときだけ使う（打ち切った途中経過だと末尾の不完全な多バイト列を不正判定する）。"""
    import codecs
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        decoder.decode(data, final=True)
    except UnicodeDecodeError:
        return False
    return True
