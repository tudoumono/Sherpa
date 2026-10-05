"""`GET /ext/v1/doc` の形式固有マジック検証（`sherpa.ext_api`）の単体テスト。DB/app 不要。

legacy Office の CFB ヘッダ健全性（`_legacy_office_header_ok`）、OOXML の bounded EOCD 事前検査
（`_zip_bounded_check`）、UTF-8 厳密判定（`_looks_utf8`）。CFB はヘッダの署名・version・byte order・
sector shift の健全性のみを見る（ディレクトリ列挙はしない）。
"""
from __future__ import annotations

import os
import struct
import zipfile
from contextlib import contextmanager
from io import BytesIO

import pytest

from sherpa import ext_api

_SECTOR_SIZE = 512


def _cfb_header(*, major=3, byte_order=0xFFFE, sector_shift=None) -> bytes:
    header = bytearray(_SECTOR_SIZE)
    header[0:8] = ext_api._OLE2_MAGIC
    struct.pack_into("<HH", header, 24, 0, major)
    struct.pack_into("<H", header, 28, byte_order)
    struct.pack_into("<H", header, 30, sector_shift if sector_shift is not None else (9 if major == 3 else 12))
    return bytes(header)


# ---- _legacy_office_header_ok ----

@pytest.mark.parametrize("data, expected", [
    (_cfb_header(major=3, sector_shift=9), True),
    (_cfb_header(major=4, sector_shift=12), True),
    (_cfb_header(major=3, sector_shift=12), False),   # v3 なのに v4 の sector_shift
    (_cfb_header(byte_order=0x1234), False),
    (_cfb_header(major=99, sector_shift=9), False),
    (ext_api._OLE2_MAGIC + b"\x00" * 10, False),      # 切り詰め
    (b"\x09\x00\x04\x00random pre-ole2 bytes", True),  # OLE2 でない旧形式は拒否しない
    (b"totally unrelated bytes here", True),
], ids=["v3", "v4", "shift-mismatch", "bad-byte-order", "unknown-major", "truncated",
        "pre-ole2-a", "pre-ole2-b"])
def test_legacy_office_header_ok(data, expected):
    assert ext_api._legacy_office_header_ok(data) is expected


# ---- _zip_bounded_check ----

def _minimal_zip_bytes(num_members: int) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(num_members):
            z.writestr(f"f{i}.txt", "x")
    return buf.getvalue()


def _minimal_ooxml_zip_bytes(ext: str = ".docx") -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(ext_api._OOXML_MAIN_PART[ext], "<root/>")
    return buf.getvalue()


@contextmanager
def _open_fd(data: bytes, tmp_path):
    p = tmp_path / "t.zip"
    p.write_bytes(data)
    fd = os.open(p, os.O_RDONLY)
    try:
        yield fd
    finally:
        os.close(fd)


def _zip_ok(data: bytes, tmp_path) -> bool:
    with _open_fd(data, tmp_path) as fd:
        return ext_api._zip_bounded_check(fd, len(data))


def _eocd_idx(data) -> int:
    return bytes(data).rfind(b"PK\x05\x06")


def _cd_offset_of(data: bytes) -> int:
    return struct.unpack_from("<I", data, _eocd_idx(data) + 16)[0]


def _mut_spoofed_total(data):
    idx = _eocd_idx(data)
    assert struct.unpack_from("<H", data, idx + 10)[0] > 2
    struct.pack_into("<H", data, idx + 8, 2)
    struct.pack_into("<H", data, idx + 10, 2)    # 実 entry 数より小さく偽装


def _mut_multi_disk(data):
    struct.pack_into("<H", data, _eocd_idx(data) + 4, 1)


def _mut_cd_boundary(data):
    idx = _eocd_idx(data)
    struct.pack_into("<I", data, idx + 12, struct.unpack_from("<I", data, idx + 12)[0] + 1)


def _mut_entry_disk_start(data):
    struct.pack_into("<H", data, _cd_offset_of(bytes(data)) + 34, 1)


def _mut_zip64_size(offset):
    def mut(data):
        struct.pack_into("<I", data, _cd_offset_of(bytes(data)) + offset, 0xFFFFFFFF)
    return mut


@pytest.mark.parametrize("data, mutate", [
    (_minimal_zip_bytes(10), _mut_spoofed_total),     # EOCD の total_entries だけ偽装
    (_minimal_zip_bytes(2), _mut_multi_disk),
    (_minimal_zip_bytes(2), _mut_cd_boundary),
    (_minimal_zip_bytes(2), _mut_entry_disk_start),
    (_minimal_zip_bytes(2), _mut_zip64_size(20)),     # compressed_size
    (_minimal_zip_bytes(2), _mut_zip64_size(24)),     # uncompressed_size
    (_minimal_zip_bytes(2), _mut_zip64_size(42)),     # local_header_offset
], ids=["spoofed-eocd-count", "multi-disk", "cd-boundary", "entry-disk-start",
        "zip64-csize", "zip64-usize", "zip64-lho"])
def test_zip_bounded_check_rejects_mutated_zip(tmp_path, data, mutate):
    data = bytearray(data)
    mutate(data)
    assert _zip_ok(bytes(data), tmp_path) is False


def test_zip_bounded_check_accepts_small_valid_zip(tmp_path):
    assert _zip_ok(_minimal_zip_bytes(3), tmp_path) is True


@pytest.mark.parametrize("data", [
    b"not a zip file at all, no eocd signature present here",
    b"tiny",
], ids=["no-eocd", "undersized"])
def test_zip_bounded_check_rejects_non_zip(tmp_path, data):
    assert _zip_ok(data, tmp_path) is False


def test_zip_bounded_check_rejects_too_many_members(tmp_path, monkeypatch):
    monkeypatch.setattr(ext_api, "_ZIP_MAX_MEMBERS", 2)
    assert _zip_ok(_minimal_zip_bytes(5), tmp_path) is False


def test_zip_bounded_check_rejects_fake_eocd_embedded_in_comment(tmp_path):
    """末尾に全0の偽 EOCD を足すと本物の comment 長の辻褄が合わず、偽物は CD 境界で弾かれる。"""
    data = _minimal_zip_bytes(2) + b"PK\x05\x06" + b"\x00" * 18
    assert _zip_ok(data, tmp_path) is False


def _insert_cd_extra_field(data: bytearray, extra: bytes) -> bytearray:
    """単一 entry の central directory へ extra を挿入し cd_size を整合させる。"""
    cd_off = _cd_offset_of(bytes(data))
    assert struct.unpack_from("<H", data, cd_off + 30)[0] == 0
    n = struct.unpack_from("<H", data, cd_off + 28)[0]
    insert_at = cd_off + ext_api._ZIP_CD_ENTRY_FIXED_SIZE + n
    data[insert_at:insert_at] = extra
    struct.pack_into("<H", data, cd_off + 30, len(extra))
    idx = _eocd_idx(data)
    struct.pack_into("<I", data, idx + 12, struct.unpack_from("<I", data, idx + 12)[0] + len(extra))
    return data


@pytest.mark.parametrize("extra, expected", [
    (struct.pack("<HH", 0x0001, 0), False),                 # ZIP64 拡張情報
    (struct.pack("<HH", 0x5455, 1) + b"\x01", True),        # 拡張タイムスタンプは誤検知しない
], ids=["zip64-extra", "benign-extra"])
def test_zip_bounded_check_cd_entry_extra_field(tmp_path, extra, expected):
    data = _insert_cd_extra_field(bytearray(_minimal_zip_bytes(1)), extra)
    assert _zip_ok(bytes(data), tmp_path) is expected


def _ooxml_with_comment_eocd(ext: str, fake_eocd: bytes) -> bytes:
    real = bytearray(_minimal_ooxml_zip_bytes(ext))
    struct.pack_into("<H", real, _eocd_idx(real) + 20, len(fake_eocd))
    return bytes(real) + fake_eocd


def _zip64_sentinel_eocd() -> bytes:
    e = bytearray(b"PK\x05\x06" + b"\x00" * 18)
    struct.pack_into("<H", e, 8, 0xFFFF)
    struct.pack_into("<H", e, 10, 0xFFFF)
    return bytes(e)


@pytest.mark.parametrize("ext, fake_eocd", [
    (".docx", b"PK\x05\x06" + b"\x00" * 18),   # CD 境界不整合で不採用→左の本物を採用
    (".pptx", _zip64_sentinel_eocd()),          # ZIP64 sentinel で不採用→左の本物を採用
], ids=["cd-boundary-mismatch", "zip64-sentinel"])
def test_zip_bounded_check_accepts_ooxml_with_incidental_eocd_in_comment(tmp_path, ext, fake_eocd):
    data = _ooxml_with_comment_eocd(ext, fake_eocd)
    with _open_fd(data, tmp_path) as fd:
        assert ext_api._zip_bounded_check(fd, len(data)) is True
        assert ext_api._ooxml_magic_ok(fd, ext, len(data)) is True


def _decoy_eocd(comment_len: int) -> bytes:
    """disk_number≠0 の安価に棄却される偽 EOCD。comment_len は EOF までの残りバイト数。"""
    eocd = bytearray(ext_api._ZIP_EOCD_SIZE)
    eocd[0:4] = ext_api._ZIP_EOCD_SIG
    struct.pack_into("<H", eocd, 4, 1)
    struct.pack_into("<H", eocd, 20, comment_len)
    return bytes(eocd)


def test_ooxml_magic_ok_accepts_file_with_9_cheap_reject_decoy_eocds_in_comment(tmp_path):
    """安価に棄却される偽候補は候補数上限（8）を消費しない＝9 個あっても左の本物へ到達する。"""
    n_decoys = 9
    assert n_decoys > ext_api._ZIP_MAX_EOCD_CANDIDATES
    blob = b""
    for _ in range(n_decoys):
        blob = _decoy_eocd(len(blob)) + blob
    data = _ooxml_with_comment_eocd(".xlsx", blob)
    with _open_fd(data, tmp_path) as fd:
        assert ext_api._ooxml_magic_ok(fd, ".xlsx", len(data)) is True


# ---- _ZipScanBudget ----

def test_zip_scan_budget_limits():
    b = ext_api._ZipScanBudget()
    for _ in range(ext_api._ZIP_MAX_EOCD_CANDIDATES):
        assert b.note_candidate() is True
    assert b.exceeded is False
    assert b.note_candidate() is False and b.exceeded is True
    assert b.note_candidate() is False   # 超過後は False のまま

    b = ext_api._ZipScanBudget()
    b.entries_walked = ext_api._ZIP_MAX_TOTAL_ENTRIES_WALKED
    assert b.note_entry() is False and b.exceeded is True

    b = ext_api._ZipScanBudget()
    assert b.note_read(ext_api._ZIP_MAX_TOTAL_BYTES_READ) is True   # ちょうど上限は超過ではない
    assert b.exceeded is False
    assert b.note_read(1) is False and b.exceeded is True


def _fake_eocd_cheap_pass(idx: int, *, cd_size: int = 0) -> bytes:
    """安価な事前チェック（disk・ZIP64・cd_offset+cd_size==idx）を通過する偽 EOCD。"""
    eocd = bytearray(ext_api._ZIP_EOCD_SIZE)
    struct.pack_into("<II", eocd, 12, cd_size, idx - cd_size)
    return bytes(eocd)


def _cheap_reject_eocd() -> bytes:
    eocd = bytearray(ext_api._ZIP_EOCD_SIZE)
    struct.pack_into("<H", eocd, 4, 1)
    return bytes(eocd)


@pytest.fixture
def walk_and_pread(monkeypatch):
    """実 walker / 実 os.pread を素通しで呼びつつ呼び出しを記録する。"""
    walk_calls, pread_calls = [], []
    orig_walk = ext_api._zip_count_central_directory_entries
    orig_pread = os.pread

    def _walk(fd, cd_offset, cd_size, budget):
        walk_calls.append((cd_offset, cd_size))
        return orig_walk(fd, cd_offset, cd_size, budget)

    def _pread(fd_, n, offset):
        pread_calls.append((n, offset))
        return orig_pread(fd_, n, offset)

    monkeypatch.setattr(ext_api, "_zip_count_central_directory_entries", _walk)
    monkeypatch.setattr(ext_api.os, "pread", _pread)
    return walk_calls, pread_calls


def test_zip_bounded_check_caps_total_candidates_tried(monkeypatch, tmp_path, walk_and_pread):
    """安価に弾ける偽候補は候補枠を消費せず、CD 走査まで届く候補はちょうど上限回で打ち切られる。"""
    walk_calls, pread_calls = walk_and_pread
    base = 1000
    cheap = [(9000 + i, _cheap_reject_eocd()) for i in range(ext_api._ZIP_MAX_EOCD_CANDIDATES + 12)]
    real_shaped = [(base + i, _fake_eocd_cheap_pass(base + i, cd_size=ext_api._ZIP_CD_ENTRY_FIXED_SIZE))
                   for i in range(ext_api._ZIP_MAX_EOCD_CANDIDATES + 5)]
    fake = cheap + real_shaped
    monkeypatch.setattr(ext_api, "_iter_eocd_candidates", lambda tail: iter(fake))

    assert _zip_ok(b"x" * 2000, tmp_path) is False
    assert len(walk_calls) == ext_api._ZIP_MAX_EOCD_CANDIDATES
    # tail の 1 回＋各候補の header pread 1 回ずつ
    assert len(pread_calls) == 1 + ext_api._ZIP_MAX_EOCD_CANDIDATES


def test_zip_bounded_check_caps_total_entries_walked_across_candidates(monkeypatch, tmp_path):
    """1 候補ずつは上限未満でも合算 entry 数が `_ZIP_MAX_TOTAL_ENTRIES_WALKED` を超えたら打ち切る。"""
    per_candidate = ext_api._ZIP_MAX_TOTAL_ENTRIES_WALKED // 2 + 1
    fake = [(i, _fake_eocd_cheap_pass(i)) for i in range(5)]
    monkeypatch.setattr(ext_api, "_iter_eocd_candidates", lambda tail: iter(fake))
    walk_calls = []

    def _fake_walk(fd, cd_offset, cd_size, budget):
        walk_calls.append(1)
        for _ in range(per_candidate):
            if not budget.note_entry():
                return None
        return None

    monkeypatch.setattr(ext_api, "_zip_count_central_directory_entries", _fake_walk)
    assert _zip_ok(b"x" * 100, tmp_path) is False
    assert len(walk_calls) == 2


def _real_cd_entry() -> bytes:
    entry = bytearray(ext_api._ZIP_CD_ENTRY_FIXED_SIZE)
    entry[0:4] = ext_api._ZIP_CD_ENTRY_SIG
    return bytes(entry)


def _real_eocd_for(cd_offset: int, cd_size: int, total_entries: int) -> bytes:
    eocd = bytearray(ext_api._ZIP_EOCD_SIZE)
    struct.pack_into("<HHHH", eocd, 4, 0, 0, total_entries, total_entries)
    struct.pack_into("<II", eocd, 12, cd_size, cd_offset)
    return bytes(eocd)


def test_zip_bounded_check_shares_byte_budget_across_candidates(monkeypatch, tmp_path, walk_and_pread):
    """pread バイト数の合算上限は候補間で共有される。候補 A（total 不一致の罠）で消費済みの
    ため候補 B は header の pread が発生する前に打ち切られる。"""
    walk_calls, pread_calls = walk_and_pread
    entry_bytes = _real_cd_entry()
    entry_size = len(entry_bytes)
    cd_a_offset, cd_a = 1000, entry_bytes * 2
    cd_b_offset, cd_b = 2000, entry_bytes
    monkeypatch.setattr(ext_api, "_ZIP_MAX_TOTAL_BYTES_READ", entry_size * 2 + 8)

    data = bytearray(b"\x00" * 3000)
    data[cd_a_offset:cd_a_offset + len(cd_a)] = cd_a
    data[cd_b_offset:cd_b_offset + len(cd_b)] = cd_b
    fake = [
        (cd_a_offset + len(cd_a), _real_eocd_for(cd_a_offset, len(cd_a), 999)),
        (cd_b_offset + len(cd_b), _real_eocd_for(cd_b_offset, len(cd_b), 1)),
    ]
    monkeypatch.setattr(ext_api, "_iter_eocd_candidates", lambda tail: iter(fake))

    assert _zip_ok(bytes(data), tmp_path) is False
    assert walk_calls == [(cd_a_offset, len(cd_a)), (cd_b_offset, len(cd_b))]
    tail_size = min(len(data), ext_api._ZIP_EOCD_SIZE + ext_api._ZIP_EOCD_MAX_COMMENT)
    assert pread_calls == [
        (tail_size, len(data) - tail_size),
        (entry_size, cd_a_offset),
        (entry_size, cd_a_offset + entry_size),
    ]


def test_zip_count_central_directory_entries_skips_pread_when_entry_budget_exhausted(
        tmp_path, walk_and_pread):
    """entry 合算上限に既に達していれば header の pread を一切行わず打ち切る。"""
    _, pread_calls = walk_and_pread
    budget = ext_api._ZipScanBudget()
    budget.entries_walked = ext_api._ZIP_MAX_TOTAL_ENTRIES_WALKED
    with _open_fd(b"x" * 1000, tmp_path) as fd:
        result = ext_api._zip_count_central_directory_entries(
            fd, 0, ext_api._ZIP_CD_ENTRY_FIXED_SIZE, budget)
    assert result is None
    assert pread_calls == []
    assert budget.exceeded is True


# ---- _looks_utf8 ----

@pytest.mark.parametrize("data, expected", [
    (b"hello world", True),
    ("こんにちは".encode("utf-8"), True),
    (b"ok\xff", False),                                   # 末尾の不正バイト（旧実装は見逃し）
    ("こんにちは".encode("shift_jis"), False),
    (b"ok" + "あ".encode("utf-8")[:1], False),            # 切れたマルチバイト
], ids=["ascii", "japanese", "invalid-trailing", "shift-jis", "truncated-multibyte"])
def test_looks_utf8(data, expected):
    assert ext_api._looks_utf8(data) is expected
