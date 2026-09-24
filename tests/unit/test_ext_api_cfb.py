"""`GET /ext/v1/doc` の形式固有マジック検証（`sherpa.ext_api`）の単体テスト。DB/app 不要。

対象: legacy Office（.doc/.xls/.ppt）の CFB ヘッダ健全性検証（`_legacy_office_header_ok`）、
OOXML の bounded EOCD 事前検査（`_zip_bounded_check`）、UTF-8 厳密判定（`_looks_utf8`）。

legacy Office は CFB ディレクトリ（stream 列挙・形式判別）までは見ない（配信元は登録済み
world＝信頼済みコーパスであり、深い形式判別は脅威モデル過剰という裁定）——ヘッダの
署名・version・byte order・sector shift の健全性のみを見る。
"""
from __future__ import annotations

import os
import struct
import zipfile
from io import BytesIO

from sherpa import ext_api

_SECTOR_SIZE = 512


def _cfb_header(*, magic=None, major=3, byte_order=0xFFFE, sector_shift=None) -> bytes:
    """[MS-CFB] ヘッダ（512バイト）を1つ組み立てる。既定は妥当な v3 ヘッダ。"""
    header = bytearray(_SECTOR_SIZE)
    header[0:8] = ext_api._OLE2_MAGIC if magic is None else magic
    struct.pack_into("<HH", header, 24, 0, major)   # minor, major version
    struct.pack_into("<H", header, 28, byte_order)
    struct.pack_into("<H", header, 30, sector_shift if sector_shift is not None else (9 if major == 3 else 12))
    return bytes(header)


# ---- _legacy_office_header_ok ----

def test_legacy_office_header_ok_accepts_valid_v3_header():
    assert ext_api._legacy_office_header_ok(_cfb_header(major=3, sector_shift=9)) is True


def test_legacy_office_header_ok_accepts_valid_v4_header():
    assert ext_api._legacy_office_header_ok(_cfb_header(major=4, sector_shift=12)) is True


def test_legacy_office_header_ok_rejects_sector_shift_mismatch():
    """major=3（512バイトセクタのはず）なのに sector_shift が v4 の値＝不整合。"""
    assert ext_api._legacy_office_header_ok(_cfb_header(major=3, sector_shift=12)) is False


def test_legacy_office_header_ok_rejects_bad_byte_order():
    assert ext_api._legacy_office_header_ok(_cfb_header(byte_order=0x1234)) is False


def test_legacy_office_header_ok_rejects_unknown_major_version():
    assert ext_api._legacy_office_header_ok(_cfb_header(major=99, sector_shift=9)) is False


def test_legacy_office_header_ok_rejects_truncated_header():
    assert ext_api._legacy_office_header_ok(ext_api._OLE2_MAGIC + b"\x00" * 10) is False


def test_legacy_office_header_ok_pre_ole2_not_rejected():
    """OLE2 マジックですらない（pre-OLE2 の旧形式想定）は拒否しない（doctype 側で既にゲート済み）。"""
    assert ext_api._legacy_office_header_ok(b"\x09\x00\x04\x00random pre-ole2 bytes") is True
    assert ext_api._legacy_office_header_ok(b"totally unrelated bytes here") is True


# ---- _zip_bounded_check（OOXML: EOCD＋central directory の bounded 検証。メンバー名は
# この検証済みの走査から直接得る＝zipfile.ZipFile への二重解析はしない）----

def _minimal_zip_bytes(num_members: int) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(num_members):
            z.writestr(f"f{i}.txt", "x")
    return buf.getvalue()


def _minimal_ooxml_zip_bytes(ext: str = ".docx") -> bytes:
    """`_ooxml_magic_ok()` が要求する必須メンバー（`[Content_Types].xml` ＋形式固有 main part）
    だけを持つ最小 OOXML zip（2 entry）。"""
    main_part = ext_api._OOXML_MAIN_PART[ext]
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(main_part, "<root/>")
    return buf.getvalue()


def _fd_for(data: bytes, tmp_path) -> int:
    p = tmp_path / "t.zip"
    p.write_bytes(data)
    import os
    return os.open(p, os.O_RDONLY)


def test_zip_bounded_check_accepts_small_valid_zip(tmp_path):
    data = _minimal_zip_bytes(3)
    fd = _fd_for(data, tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is True
    finally:
        import os
        os.close(fd)


def test_zip_bounded_check_rejects_too_many_members(tmp_path, monkeypatch):
    monkeypatch.setattr(ext_api, "_ZIP_MAX_MEMBERS", 2)
    data = _minimal_zip_bytes(5)
    fd = _fd_for(data, tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is False
    finally:
        import os
        os.close(fd)


def test_zip_bounded_check_rejects_no_eocd(tmp_path):
    data = b"not a zip file at all, no eocd signature present here"
    fd = _fd_for(data, tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is False
    finally:
        import os
        os.close(fd)


def test_zip_bounded_check_rejects_undersized_file(tmp_path):
    data = b"tiny"
    fd = _fd_for(data, tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is False
    finally:
        import os
        os.close(fd)


def test_zip_bounded_check_rejects_spoofed_eocd_member_count(tmp_path):
    """EOCD の自己申告 `total_entries` だけ小さく偽装し、central directory 自体には多くの
    entry を残す攻撃を拒否する（`zipfile.ZipFile` は `cd_size` 分を実走査するため、EOCD の
    件数だけを検査しても素通りしてしまっていた の本丸）。"""
    data = bytearray(_minimal_zip_bytes(10))   # 2固定 + 10 = 12 entries（honest な EOCD 上は12）
    idx = data.rfind(b"PK\x05\x06")
    real_total = struct.unpack_from("<H", data, idx + 10)[0]
    assert real_total > 2   # 前提: 実entry数はEOCDの偽装先（2）より多い
    struct.pack_into("<H", data, idx + 8, 2)    # entries_this_disk（disk一致チェックを通すため）
    struct.pack_into("<H", data, idx + 10, 2)   # total_entries を偽って2件だけと申告
    fd = _fd_for(bytes(data), tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is False
    finally:
        import os
        os.close(fd)


def test_zip_bounded_check_accepts_valid_ooxml_with_incidental_eocd_signature_cd_boundary_mismatch(
        tmp_path):
    """rightmost の EOCD 候補（本物の comment 内に偶然含まれる `PK\x05\x06` という4バイト列）
    の comment_len が EOF に一致し**完全検証まで進んでも**、central directory との整合
    （`cd_offset+cd_size` の境界）で不採用になるだけで、それだけでアーカイブ全体を拒否せず、
    次の候補（さらに左＝本物の EOCD）を試して受理する。`_ooxml_magic_ok()` まで通ることを
    固定する（必須 OOXML メンバー `[Content_Types].xml`／main part 入り）。

    以前は rightmost 一本化のみで「候補が1つでも central directory と整合しなければ即座に
    全体拒否」しており、comment に偶然 `PK\x05\x06` を含むだけの正当な ZIP まで誤って
    拒否していた。`_ooxml_magic_ok()` は既に `zipfile.ZipFile` への二重解析を撤去済み
    （メンバー名は検証済みの central directory 走査から直接得る）ため、rightmost 以外の
    候補を採用しても別 parser との食い違いは生じない——「候補として認めるか」
    （`_iter_eocd_candidates`）と「その候補を採用するか」（`_zip_bounded_check_names`）を
    同じ parser・同じ検証基準（EOF 長・CD 境界・全 entry 走査）で行うことが安全の根拠。"""
    real = bytearray(_minimal_ooxml_zip_bytes(".docx"))
    idx_real = real.rfind(b"PK\x05\x06")
    # 偽 EOCD: 全フィールド0（comment_len=0 も含む）＝ファイル末尾ぴったりに置けば
    # 「comment が EOF に届く」形としては成立する（disk_number==entries_this_disk==0 も
    # 通る）が、cd_offset=0/cd_size=0 は自分自身の位置（末尾）と一致しない＝境界不整合で
    # 完全検証の途中（cd_offset+cd_size 境界チェック）で不採用になる。
    fake_eocd = b"PK\x05\x06" + b"\x00" * 18
    # 本物の EOCD の comment_len を「これから追記する偽 EOCD の分ちょうど」に更新する
    # （実際にその分のバイトを comment として追記するので偽装ではなく本物の comment の中身）。
    struct.pack_into("<H", real, idx_real + 20, len(fake_eocd))
    data = bytes(real) + fake_eocd
    fd = _fd_for(data, tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is True
        assert ext_api._ooxml_magic_ok(fd, ".docx", len(data)) is True
    finally:
        os.close(fd)


def test_zip_bounded_check_accepts_valid_ooxml_with_incidental_eocd_signature_zip64_sentinel(
        tmp_path):
    """rightmost の EOCD 候補の comment_len が EOF に一致し完全検証まで進んでも、ZIP64
    sentinel（`total_entries`/`entries_this_disk` が `0xFFFF`）で不採用になるだけで、それだけで
    アーカイブ全体を拒否せず、次の候補（さらに左＝本物の EOCD）を試して受理する（cd_offset/
    cd_size 境界不整合で不採用になる上のテストとは別の構造チェックを通る経路）。
    `_ooxml_magic_ok()` まで通ることを固定する。"""
    real = bytearray(_minimal_ooxml_zip_bytes(".pptx"))
    idx_real = real.rfind(b"PK\x05\x06")
    fake_eocd = bytearray(b"PK\x05\x06" + b"\x00" * 18)
    struct.pack_into("<H", fake_eocd, 8, 0xFFFF)    # entries_this_disk
    struct.pack_into("<H", fake_eocd, 10, 0xFFFF)   # total_entries（ZIP64 sentinel）
    fake_eocd = bytes(fake_eocd)
    struct.pack_into("<H", real, idx_real + 20, len(fake_eocd))
    data = bytes(real) + fake_eocd
    fd = _fd_for(data, tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is True
        assert ext_api._ooxml_magic_ok(fd, ".pptx", len(data)) is True
    finally:
        os.close(fd)


def _decoy_eocd(comment_len: int) -> bytes:
    """cheap-reject（disk_number≠0）な偽 EOCD（22バイト）。`comment_len` は呼び出し側が
    「このEOCDから実際のEOFまでの残りバイト数」を正確に渡す必要がある——でなければ
    `_iter_eocd_candidates` の comment_len==EOF 一致チェックで弾かれ、そもそも候補として
    yield されない（このテストの主眼＝候補として実際に拾われた上で定数時間チェックで
    即 continue されることを検証する、が成立しなくなる）。"""
    eocd = bytearray(ext_api._ZIP_EOCD_SIZE)
    eocd[0:4] = ext_api._ZIP_EOCD_SIG
    struct.pack_into("<H", eocd, 4, 1)    # disk_number=1（single-disk チェックで即 continue）
    struct.pack_into("<H", eocd, 20, comment_len)
    return bytes(eocd)


def test_ooxml_magic_ok_accepts_real_file_with_9_cheap_reject_decoy_eocds_in_comment(tmp_path):
    """実 OOXML zip の comment に、cheap-reject（disk_number≠0）な偽 EOCD を `_ZIP_MAX_
    EOCD_CANDIDATES`（8）を超える9件、実際に埋め込んだ結合正例。`_iter_eocd_candidates`／
    `_zip_bounded_check_names`／`_zip_count_central_directory_entries` はいずれも差し替えず
    （モックなし）、end-to-end で `_ooxml_magic_ok()` が True を返すことを固定する。

    各偽 EOCD は自分の位置から見た「EOF までの残りバイト数」を正しく `comment_len` に
    入れているため、`_iter_eocd_candidates` に実際に候補として拾われ、
    `_zip_bounded_check_names` の定数時間チェック（disk_number）で即 continue される——
    `note_candidate()` は定数時間チェック通過後にのみ課金するため、8個を超える cheap-reject
    候補があっても候補数の合算枠を一切消費せず、さらに左にある本物の EOCD まで実際に
    到達できる。もし `note_candidate()` が定数時間チェックより前に戻っていれば、9個目の
    偽候補で候補数上限（8）に達し本物の EOCD へ到達できず、このテストは False で失敗する。
    """
    n_decoys = 9
    assert n_decoys > ext_api._ZIP_MAX_EOCD_CANDIDATES

    real = bytearray(_minimal_ooxml_zip_bytes(".xlsx"))
    idx_real = real.rfind(b"PK\x05\x06")

    # EOF に近い（右）decoy から順に組み立て、各 decoy の comment_len を「自分より右側の
    # 総バイト数」に正確に合わせる。
    decoy_blob = b""
    for _ in range(n_decoys):
        decoy_blob = _decoy_eocd(len(decoy_blob)) + decoy_blob

    struct.pack_into("<H", real, idx_real + 20, len(decoy_blob))   # 本物の comment_len を更新
    data = bytes(real) + decoy_blob

    fd = _fd_for(data, tmp_path)
    try:
        assert ext_api._ooxml_magic_ok(fd, ".xlsx", len(data)) is True
    finally:
        os.close(fd)


def test_zip_bounded_check_rejects_multi_disk(tmp_path):
    """disk_number/disk_with_cd が 0 でない（複数ディスクアーカイブ）は拒否する。"""
    data = bytearray(_minimal_zip_bytes(2))
    idx = data.rfind(b"PK\x05\x06")
    struct.pack_into("<H", data, idx + 4, 1)   # disk_number != 0
    fd = _fd_for(bytes(data), tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is False
    finally:
        os.close(fd)


def test_zip_bounded_check_rejects_cd_boundary_mismatch(tmp_path):
    """`cd_offset + cd_size` が EOCD の直前で終わっていない（prepended data 等）は拒否する。"""
    data = bytearray(_minimal_zip_bytes(2))
    idx = data.rfind(b"PK\x05\x06")
    cd_size = struct.unpack_from("<I", data, idx + 12)[0]
    struct.pack_into("<I", data, idx + 12, cd_size + 1)   # cd_size をわずかに水増しして境界を崩す
    fd = _fd_for(bytes(data), tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is False
    finally:
        os.close(fd)


def test_zip_bounded_check_rejects_fake_eocd_embedded_in_comment(tmp_path):
    """comment 内に偽の EOCD 署名（`PK\\x05\\x06`）を埋め込んでも、辻褄（comment_len が実際に
    EOF まで届くか）が合わない候補は無視して本物の EOCD を探す（`bytes.rfind()` を無条件で
    信用しない）。ここでは偽物が「本物」として通ってしまわないことを固定する
    （偽物の cd_offset/cd_size は 0 のため、境界検証で確実に弾かれる）。"""
    real = _minimal_zip_bytes(2)
    fake_eocd = b"PK\x05\x06" + b"\x00" * 18   # 全フィールド0＝total_entries=0・comment_len=0
    fd_data = real + fake_eocd   # 本物のEOCDの直後（＝本物のcommentの中）に偽物を追記
    fd = _fd_for(fd_data, tmp_path)
    try:
        # 偽物は「ファイル末尾から数えて comment_len=0 バイトで EOF に届く」という辻褄だけは
        # 満たすため _iter_eocd_candidates には拾われうるが、cd_offset=0/cd_size=0 は偽物自身の
        # 位置と一致しない（境界検証で弾かれる）ため、全体として受理されない
        # （本物の EOCD は偽物が追記された分だけ comment 長の辻褄が合わなくなり候補にすら
        # ならない＝フォールバック先も無い）。
        assert ext_api._zip_bounded_check(fd, len(fd_data)) is False
    finally:
        os.close(fd)


def _cd_offset_of(data: bytes) -> int:
    """EOCD の cd_offset フィールドから、最初の central directory entry の絶対オフセットを読む。"""
    idx = data.rfind(b"PK\x05\x06")
    return struct.unpack_from("<I", data, idx + 16)[0]


def test_zip_bounded_check_rejects_cd_entry_disk_number_start(tmp_path):
    """central directory **entry 単位**の disk number start != 0 は拒否する（EOCD レベルの
    disk_number だけでなく entry 単位でも multi-disk を検出する）。"""
    data = bytearray(_minimal_zip_bytes(2))
    cd_off = _cd_offset_of(bytes(data))
    struct.pack_into("<H", data, cd_off + 34, 1)   # 最初の entry の disk number start != 0
    fd = _fd_for(bytes(data), tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is False
    finally:
        os.close(fd)


def test_zip_bounded_check_rejects_cd_entry_zip64_size_sentinels(tmp_path):
    """central directory entry の compressed_size/uncompressed_size/local_header_offset が
    `0xFFFFFFFF`（ZIP64 sentinel・実値は extra フィールド側）なら拒否する。"""
    for field_offset, label in ((20, "compressed_size"), (24, "uncompressed_size"),
                                (42, "local_header_offset")):
        data = bytearray(_minimal_zip_bytes(2))
        cd_off = _cd_offset_of(bytes(data))
        struct.pack_into("<I", data, cd_off + field_offset, 0xFFFFFFFF)
        fd = _fd_for(bytes(data), tmp_path)
        try:
            assert ext_api._zip_bounded_check(fd, len(data)) is False, label
        finally:
            os.close(fd)


def _insert_cd_extra_field(data: bytearray, extra: bytes) -> bytearray:
    """単一 entry のみの central directory へ extra フィールドを挿入し、EOCD の cd_size を
    整合させる（呼び出し元は `_minimal_zip_bytes(1)` を渡すこと＝挿入位置の後ろに他 entry が
    無く、シフト計算が単純になる）。
    """
    cd_off = _cd_offset_of(bytes(data))
    m_before = struct.unpack_from("<H", data, cd_off + 30)[0]
    assert m_before == 0, "前提: writestr は extra フィールドを付けない"
    n = struct.unpack_from("<H", data, cd_off + 28)[0]
    insert_at = cd_off + ext_api._ZIP_CD_ENTRY_FIXED_SIZE + n
    data[insert_at:insert_at] = extra
    struct.pack_into("<H", data, cd_off + 30, len(extra))   # extra field length (m) を更新
    new_idx = bytes(data).rfind(b"PK\x05\x06")
    cd_size = struct.unpack_from("<I", data, new_idx + 12)[0]
    struct.pack_into("<I", data, new_idx + 12, cd_size + len(extra))   # cd_size を挿入分だけ増やす
    return data



def test_zip_bounded_check_rejects_cd_entry_zip64_extra_tag(tmp_path):
    """central directory entry の extra フィールドに ZIP64 拡張情報（tag `0x0001`）が含まれる
    場合は拒否する（単一 entry のみのアーカイブで extra フィールドを直接組み立てる）。"""
    data = bytearray(_minimal_zip_bytes(1))
    zip64_extra = struct.pack("<HH", 0x0001, 0)   # tag=0x0001（ZIP64）, size=0
    data = _insert_cd_extra_field(data, zip64_extra)
    fd = _fd_for(bytes(data), tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is False
    finally:
        os.close(fd)


def test_zip_bounded_check_accepts_benign_cd_entry_extra_field(tmp_path):
    """ZIP64 以外の extra フィールド（例: 拡張タイムスタンプ tag `0x5455`）は拒否しない
    （誤検知していないことの対照）。"""
    data = bytearray(_minimal_zip_bytes(1))
    benign_extra = struct.pack("<HH", 0x5455, 1) + b"\x01"   # tag=0x5455, size=1, data=1byte
    data = _insert_cd_extra_field(data, benign_extra)
    fd = _fd_for(bytes(data), tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is True
    finally:
        os.close(fd)


# ---- _ZipScanBudget（複数 EOCD 候補にまたがる合算走査量の上限）----

def test_zip_scan_budget_note_candidate_exceeds_after_limit():
    b = ext_api._ZipScanBudget()
    for _ in range(ext_api._ZIP_MAX_EOCD_CANDIDATES):
        assert b.note_candidate() is True
    assert b.exceeded is False
    assert b.note_candidate() is False   # 上限+1回目で超過
    assert b.exceeded is True
    assert b.note_candidate() is False   # 超過後はずっと False


def test_zip_scan_budget_note_entry_exceeds_after_limit():
    b = ext_api._ZipScanBudget()
    b.entries_walked = ext_api._ZIP_MAX_TOTAL_ENTRIES_WALKED
    assert b.note_entry() is False
    assert b.exceeded is True


def test_zip_scan_budget_note_read_exceeds_after_limit():
    b = ext_api._ZipScanBudget()
    assert b.note_read(ext_api._ZIP_MAX_TOTAL_BYTES_READ) is True   # ちょうど上限は超過ではない
    assert b.exceeded is False
    assert b.note_read(1) is False   # 1バイトでも超えたら超過
    assert b.exceeded is True


def _fake_eocd_cheap_pass(idx: int, *, cd_size: int = 0) -> bytes:
    """`idx` をそのまま `cd_offset` に置き、`cd_size` はデフォルト0にすることで、
    `_zip_bounded_check_names` の安価な事前チェック（disk 番号・ZIP64 sentinel・
    `cd_offset+cd_size == eocd_abs_offset`）を必ず通過する偽 EOCD を組み立てる
    （`eocd_abs_offset == idx` になるテスト条件下で `cd_offset+cd_size == idx` を
    機械的に満たす）。cheap-check を通過させること自体がこのヘルパーの目的であり、
    以降 central directory walker まで実際に到達させたいテストで使う。"""
    eocd = bytearray(ext_api._ZIP_EOCD_SIZE)
    struct.pack_into("<II", eocd, 12, cd_size, idx - cd_size)
    return bytes(eocd)


def _cheap_reject_eocd() -> bytes:
    """disk_number を非0にし、`_zip_bounded_check_names` の最初の定数時間チェック
    （`disk_number != 0` 判定）だけで即 `continue` される安価な偽候補を組み立てる
    （central directory walker には一切到達しない＝`note_candidate()` を消費してはいけない）。"""
    eocd = bytearray(ext_api._ZIP_EOCD_SIZE)
    struct.pack_into("<H", eocd, 4, 1)   # disk_number = 1（single-disk ではない＝即 continue）
    return bytes(eocd)


def test_zip_bounded_check_caps_total_candidates_tried(monkeypatch, tmp_path):
    """EOCD 候補が合算候補数上限（`_ZIP_MAX_EOCD_CANDIDATES`）を超えて存在しても、それ以上は
    central directory 走査を試さずアーカイブ全体を拒否する（多数の偽候補を comment 内に並べて
    何度も CD 走査させる DoS への対策）。**安価な定数時間チェックでその場で弾ける偽候補を
    8個より多く先頭に並べても**（このテストの主眼——それらは `note_candidate()` を消費しては
    いけない）、その後ろの「cheap-check は通過するが central directory の実データが不正」な
    偽候補が**実際に central directory walker（実 os.pread）まで到達**し、ちょうど
    `_ZIP_MAX_EOCD_CANDIDATES` 回だけ試して打ち切られることを固定する（壁時計ではなく実際の
    呼び出し回数）。"""
    n_candidates = ext_api._ZIP_MAX_EOCD_CANDIDATES + 5
    base = 1000   # cd_offset(=idx-46) が常に非負になるよう、十分大きい idx から始める
    n_cheap_reject = ext_api._ZIP_MAX_EOCD_CANDIDATES + 12   # 候補数上限より多い「安価な偽候補」
    cheap_reject_candidates = [(9000 + i, _cheap_reject_eocd()) for i in range(n_cheap_reject)]
    real_shaped_candidates = [
        (base + i, _fake_eocd_cheap_pass(base + i, cd_size=ext_api._ZIP_CD_ENTRY_FIXED_SIZE))
        for i in range(n_candidates)
    ]
    fake_candidates = cheap_reject_candidates + real_shaped_candidates
    monkeypatch.setattr(ext_api, "_iter_eocd_candidates", lambda tail: iter(fake_candidates))

    walk_calls = []
    orig_walk = ext_api._zip_count_central_directory_entries

    def _counting_walk(fd, cd_offset, cd_size, budget):
        walk_calls.append((cd_offset, cd_size))
        return orig_walk(fd, cd_offset, cd_size, budget)   # 実 walker（実 pread）を素通しで呼ぶ

    monkeypatch.setattr(ext_api, "_zip_count_central_directory_entries", _counting_walk)

    pread_calls = []
    orig_pread = os.pread

    def _counting_pread(fd_, n, offset):
        pread_calls.append((n, offset))
        return orig_pread(fd_, n, offset)

    monkeypatch.setattr(ext_api.os, "pread", _counting_pread)

    data = b"x" * 2000   # cd_offset+46（最大 966+46=1012）が収まる十分なサイズ。中身は "x" 埋め
    fd = _fd_for(data, tmp_path)                      # ＝central directory 署名と一致しない
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is False
    finally:
        os.close(fd)
    assert len(walk_calls) == ext_api._ZIP_MAX_EOCD_CANDIDATES, (
        f"候補数の合算上限ちょうどで central directory 走査が打ち切られるはず（呼び出し "
        f"{len(walk_calls)} 回）")
    # tail の1回＋各候補の header pread 1回ずつ（署名不一致で即失敗＝各候補1回のみ）。
    assert len(pread_calls) == 1 + ext_api._ZIP_MAX_EOCD_CANDIDATES, (
        f"実際の pread 回数が期待と異なる（安価な棄却は候補数枠を消費しないはず）: {pread_calls}")


def test_zip_bounded_check_caps_total_entries_walked_across_candidates(monkeypatch, tmp_path):
    """1候補あたりの entry 数は上限（`_ZIP_MAX_MEMBERS`）未満でも、複数候補**合算**の走査
    entry 数が `_ZIP_MAX_TOTAL_ENTRIES_WALKED` を超えたら、それ以上候補を試さずアーカイブ
    全体を拒否する（1候補ずつは正常に見える偽候補を並べて合算コストを膨らませる DoS への
    対策）。候補は cheap-check を必ず通過する形（`cd_offset=idx`）で用意し、壁時計ではなく
    実際の呼び出し回数で固定する。"""
    per_candidate = ext_api._ZIP_MAX_TOTAL_ENTRIES_WALKED // 2 + 1   # 2候補で合算上限を超える量
    fake_candidates = [(i, _fake_eocd_cheap_pass(i)) for i in range(5)]
    monkeypatch.setattr(ext_api, "_iter_eocd_candidates", lambda tail: iter(fake_candidates))

    walk_calls = []

    def _fake_walk(fd, cd_offset, cd_size, budget):
        walk_calls.append(1)
        for _ in range(per_candidate):
            if not budget.note_entry():
                return None
        return None   # 律儀に消費しきっても構造的には不正（主眼は途中で打ち切ること）

    monkeypatch.setattr(ext_api, "_zip_count_central_directory_entries", _fake_walk)

    data = b"x" * 100
    fd = _fd_for(data, tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is False
    finally:
        os.close(fd)
    assert len(walk_calls) == 2, (
        f"entry 数の合算上限を超えた時点（2候補目の途中）で打ち切られるはず（呼び出し "
        f"{len(walk_calls)} 回）")


def _real_cd_entry() -> bytes:
    """central directory file header の最小妥当形（46バイト固定部のみ・filename/extra 無し）。
    署名以外は全ゼロ（compressed/uncompressed size=0・disk_number_start=0・n=m=k=0）で、
    `_zip_count_central_directory_entries` の構造検証を素通しする実バイト列。"""
    entry = bytearray(ext_api._ZIP_CD_ENTRY_FIXED_SIZE)
    entry[0:4] = ext_api._ZIP_CD_ENTRY_SIG
    return bytes(entry)


def _real_eocd_for(cd_offset: int, cd_size: int, total_entries: int) -> bytes:
    """single-disk・非ZIP64の実 EOCD（22バイト）を組み立てる（`_zip_bounded_check_names` の
    定数時間チェックを通す最小限のフィールドのみ設定）。"""
    eocd = bytearray(ext_api._ZIP_EOCD_SIZE)
    struct.pack_into("<HHHH", eocd, 4, 0, 0, total_entries, total_entries)
    struct.pack_into("<II", eocd, 12, cd_size, cd_offset)
    return bytes(eocd)


def test_zip_bounded_check_shares_byte_budget_across_candidates(monkeypatch, tmp_path):
    """`_ZIP_MAX_TOTAL_BYTES_READ`（pread バイト数の合算上限）は候補ごとにリセットされず、
    複数候補にまたがって共有される——モックした walker ではなく、実 central directory
    walker（`_zip_count_central_directory_entries`）に実バイト列を実際に `os.pread` させて
    検証する。walker 呼び出し回数・実際の pread 呼び出し（引数含む）・要求バイト数の合計を
    壁時計ではなく完全一致で固定する。

    候補A: 実 entry を2件（46byte×2=92byte）central directory に置くが、EOCD の
    `total_entries` をわざと不一致（999）にする——構造的には最後まで完走する（walker は
    None を返さない）が採用条件（実 entry 数と EOCD 自己申告の一致）を満たさず次候補へ進む。
    候補B: 候補Aで既に92byte消費済みの状態で1件目の entry header を読もうとし、
    （budget 上限をこのテスト専用に100byteへ絞ることで）header の pread が発生する**前**に
    `note_read()` が False を返して打ち切られる——候補間で bytes_read が引き継がれている
    ことの直接証拠になる。
    """
    entry_bytes = _real_cd_entry()
    entry_size = len(entry_bytes)

    cd_a_offset, cd_a = 1000, entry_bytes * 2
    cd_b_offset, cd_b = 2000, entry_bytes * 1

    monkeypatch.setattr(ext_api, "_ZIP_MAX_TOTAL_BYTES_READ", entry_size * 2 + 8)  # =100

    data = bytearray(b"\x00" * 3000)
    data[cd_a_offset:cd_a_offset + len(cd_a)] = cd_a
    data[cd_b_offset:cd_b_offset + len(cd_b)] = cd_b
    data = bytes(data)

    fake_candidates = [
        (cd_a_offset + len(cd_a), _real_eocd_for(cd_a_offset, len(cd_a), 999)),   # 採用されない罠
        (cd_b_offset + len(cd_b), _real_eocd_for(cd_b_offset, len(cd_b), 1)),     # budget 尽きて未到達
    ]
    monkeypatch.setattr(ext_api, "_iter_eocd_candidates", lambda tail: iter(fake_candidates))

    walk_calls = []
    orig_walk = ext_api._zip_count_central_directory_entries

    def _counting_walk(fd, cd_offset, cd_size, budget):
        walk_calls.append((cd_offset, cd_size))
        return orig_walk(fd, cd_offset, cd_size, budget)   # 実 walker（実 pread）を素通しで呼ぶ

    monkeypatch.setattr(ext_api, "_zip_count_central_directory_entries", _counting_walk)

    pread_calls = []
    orig_pread = os.pread

    def _counting_pread(fd_, n, offset):
        pread_calls.append((n, offset))
        return orig_pread(fd_, n, offset)

    monkeypatch.setattr(ext_api.os, "pread", _counting_pread)

    fd = _fd_for(data, tmp_path)
    try:
        assert ext_api._zip_bounded_check(fd, len(data)) is False
    finally:
        os.close(fd)

    assert walk_calls == [(cd_a_offset, len(cd_a)), (cd_b_offset, len(cd_b))], (
        f"候補Aと候補Bの2回だけ walker が呼ばれるはず（呼び出し: {walk_calls}）")

    tail_size = min(len(data), ext_api._ZIP_EOCD_SIZE + ext_api._ZIP_EOCD_MAX_COMMENT)
    expected_pread_calls = [
        (tail_size, len(data) - tail_size),          # tail 読み取り（EOCD 候補探索用）
        (entry_size, cd_a_offset),                    # 候補A entry1（成功）
        (entry_size, cd_a_offset + entry_size),        # 候補A entry2（成功）
        # 候補Bの1件目は note_read() が pread 前に False を返すため、header の pread 自体が
        # 発生しない（budget が候補間で共有されている直接証拠）。
    ]
    assert pread_calls == expected_pread_calls, (
        f"実際の pread 呼び出し列が期待と異なる: {pread_calls}")
    total_requested_bytes = sum(n for n, _ in pread_calls)
    assert total_requested_bytes == tail_size + entry_size * 2, (
        f"要求バイト数の合計が期待と異なる: {total_requested_bytes}")


def test_zip_count_central_directory_entries_skips_pread_when_entry_budget_already_exhausted(
        monkeypatch, tmp_path):
    """entry 数の合算上限に既に達している状態で central directory 走査を呼ぶと、次の entry の
    header pread すら一切行わずに即座に打ち切る（`note_entry()` をループ先頭で呼ぶことの直接
    検証＝20,001件目の pread が発生しないことをカウンタで固定する）。"""
    data = b"x" * 1000
    fd = _fd_for(data, tmp_path)
    try:
        budget = ext_api._ZipScanBudget()
        budget.entries_walked = ext_api._ZIP_MAX_TOTAL_ENTRIES_WALKED   # 既に上限ちょうど

        pread_calls = []
        orig_pread = os.pread

        def _counting_pread(fd_, n, offset):
            pread_calls.append((n, offset))
            return orig_pread(fd_, n, offset)

        monkeypatch.setattr(ext_api.os, "pread", _counting_pread)

        result = ext_api._zip_count_central_directory_entries(
            fd, 0, ext_api._ZIP_CD_ENTRY_FIXED_SIZE, budget)
        assert result is None
        assert pread_calls == [], (
            f"entry 数の合算上限に既に達しているのに header の pread が発生している"
            f"（20,001件目相当）: {pread_calls}")
        assert budget.exceeded is True
    finally:
        os.close(fd)


# ---- _looks_utf8（incremental decoder・全文・打ち切り無し）----

def test_looks_utf8_accepts_plain_ascii():
    assert ext_api._looks_utf8(b"hello world") is True


def test_looks_utf8_accepts_multibyte_japanese():
    assert ext_api._looks_utf8("こんにちは".encode("utf-8")) is True


def test_looks_utf8_rejects_invalid_trailing_byte():
    """`b"ok\\xff"`: 末尾の不正バイトを trim ベースの旧実装は見逃していた（回帰ケース）。"""
    assert ext_api._looks_utf8(b"ok\xff") is False


def test_looks_utf8_rejects_shift_jis_bytes():
    assert ext_api._looks_utf8("こんにちは".encode("shift_jis")) is False


def test_looks_utf8_rejects_truncated_multibyte_sequence():
    assert ext_api._looks_utf8(b"ok" + "あ".encode("utf-8")[:1]) is False
