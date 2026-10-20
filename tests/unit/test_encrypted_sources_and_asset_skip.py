"""パスワード付きファイルの見分け・知らせ・使い回しと、PDF の画像 1 枚の不一致で文書全体を捨てないことの回帰テスト。

実害の再現に限る。PDF・OLE（旧形式／暗号化 OOXML）は合成したバイト列で作り、soffice は起動回数を数える偽物に差し替える。
設計: docs/design/rag.md「サイズガードと文字コード・対象外」
"""
from __future__ import annotations

import io
import logging
import stat
import struct
import zlib
from pathlib import Path

import pytest

from sherpa import log_setup
from sherpa.ingest import encryption, evidence_spike, failure_reasons, legacy_provenance, office_md
from sherpa.ingest.arms import legacy_convert

_BODY = "BODYTEXT_ALPHA_9137"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_ARMS", "ooxml,pdf_text")
    monkeypatch.setenv("SHERPA_MD_WORKERS", "1")
    monkeypatch.delenv(office_md._OCR_ENABLED_ENV, raising=False)


def _pdf_with_image(path: Path, text: str = _BODY) -> Path:
    """本文 1 行と画像 1 枚を持つ 1 ページの PDF を直接組む。"""
    objs: list[bytes] = [b"", b""]

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pixels = zlib.compress(bytes(range(64)))
    img = add(b"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceGray /BitsPerComponent 8 "
              b"/Filter /FlateDecode /Length %d >>\nstream\n" % len(pixels) + pixels + b"\nendstream")
    data = f"BT /F1 12 Tf 20 150 Td ({text}) Tj ET\nq 60 0 0 60 100 20 cm /Im1 Do Q".encode("latin-1")
    content = add(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")
    page = add(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents %d 0 R "
                "/Resources << /Font << /F1 %d 0 R >> /XObject << /Im1 %d 0 R >> >> >>" % (content, font, img)).encode())
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = ("<< /Type /Pages /Kids [%d 0 R] /Count 1 >>" % page).encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def _encrypt_pdf(path: Path, *, user_password: str) -> None:
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter(clone_from=PdfReader(str(path)))
    writer.encrypt(user_password, owner_password="owner-pw-for-test")
    buf = io.BytesIO()
    writer.write(buf)
    path.write_bytes(buf.getvalue())


def _cfb(streams: dict[str, bytes]) -> bytes:
    """ルート直下の流れだけを持つ最小の OLE（CFB v3）を組む。各流れは 4096 バイト以上に埋めて通常セクタに置く。"""
    names = sorted(streams, key=lambda n: (len(n), n.upper()))
    padded = {n: streams[n].ljust(max(4096, (len(streams[n]) + 511) // 512 * 512), b"\0") for n in names}
    fat = [0xFFFFFFFD, 0xFFFFFFFE]            # 0=FAT 自身・1=ディレクトリ（1 セクタ）
    body = b""
    starts = {}
    for n in names:
        starts[n] = len(fat)
        count = len(padded[n]) // 512
        fat.extend(range(len(fat) + 1, len(fat) + count))
        fat.append(0xFFFFFFFE)
        body += padded[n]
    assert len(fat) <= 128 and len(names) <= 3
    fat_sector = struct.pack("<128I", *(fat + [0xFFFFFFFF] * (128 - len(fat))))

    def entry(name: str, kind: int, left: int, right: int, child: int, start: int, size: int) -> bytes:
        raw = name.encode("utf-16-le") + b"\0\0"
        return (raw.ljust(64, b"\0") + struct.pack("<HBB", len(raw) if name else 0, kind, 1)
                + struct.pack("<III", left, right, child) + b"\0" * 16 + struct.pack("<I", 0) + b"\0" * 16
                + struct.pack("<IQ", start, size))

    nostream = 0xFFFFFFFF
    entries = [entry("Root Entry", 5, nostream, nostream, 1 if names else nostream, 0xFFFFFFFE, 0)]
    for i, n in enumerate(names):
        right = i + 2 if i + 1 < len(names) else nostream
        entries.append(entry(n, 2, nostream, right, nostream, starts[n], len(padded[n])))
    while len(entries) < 4:
        entries.append(entry("", 0, nostream, nostream, nostream, 0, 0))
    directory = b"".join(entries)
    difat = [0] + [0xFFFFFFFF] * 108
    header = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6)
              + b"\0" * 6 + struct.pack("<IIIIIIIIII", 0, 1, 1, 0, 4096, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0, 0)[:36]
              + struct.pack("<109I", *difat))
    return header.ljust(512, b"\0") + fat_sector + directory + body


def _biff(*records: tuple[int, bytes]) -> bytes:
    return b"".join(struct.pack("<HH", rid, len(data)) + data for rid, data in records)


_BOF = (0x0809, b"\0\x06\x05\0" + b"\0" * 12)
_EOF = (0x000A, b"")


def _encrypted_samples() -> dict[str, bytes]:
    word_fib = bytearray(32)
    word_fib[0:2] = b"\xec\xa5"
    struct.pack_into("<H", word_fib, 0x0A, 0x0100)
    return {
        "enc.docx": _cfb({"EncryptionInfo": b"\x04\0\x04\0", "EncryptedPackage": b"x" * 64}),
        "enc.doc": _cfb({"WordDocument": bytes(word_fib)}),
        "enc.xls": _cfb({"Workbook": _biff(_BOF, (0x002F, b"\x01\0\x01\0\x01\0"), _EOF)}),
        "enc.ppt": _cfb({"Current User": struct.pack("<IIII", 0, 0, 0, 0xF3D1C4DF),
                         "PowerPoint Document": b"\0" * 32}),
    }


def _plain_samples() -> dict[str, bytes]:
    word_fib = bytearray(32)
    word_fib[0:2] = b"\xec\xa5"
    return {
        "plain.doc": _cfb({"WordDocument": bytes(word_fib)}),
        "plain.xls": _cfb({"Workbook": _biff(_BOF, (0x00E1, b"\xb0\x04"), _EOF)}),
        "plain.ppt": _cfb({"Current User": struct.pack("<IIII", 0, 0, 0, 0xE391C05F),
                           "PowerPoint Document": b"\0" * 32}),
    }


def _build(src: Path, derived: Path) -> dict:
    return office_md.build_derived(src, derived)


def _rows(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("sherpa")]


# ---- 見分け ----

@pytest.mark.parametrize("name", ["enc.docx", "enc.doc", "enc.xls", "enc.ppt"])
def test_encrypted_office_files_are_recognized(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(_encrypted_samples()[name])
    assert encryption.is_password_protected(p) is True


@pytest.mark.parametrize("name", ["plain.doc", "plain.xls", "plain.ppt"])
def test_unencrypted_legacy_files_are_not_flagged(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(_plain_samples()[name])
    assert encryption.is_password_protected(p) is False


def test_unreadable_or_unknown_files_are_not_flagged(tmp_path):
    broken = tmp_path / "broken.xls"
    broken.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\xff" * 40)
    assert encryption.is_password_protected(broken) is False
    assert encryption.is_password_protected(tmp_path / "missing.pdf") is False


def test_pdf_with_only_owner_password_is_not_flagged_but_user_password_is(tmp_path):
    owner_only = _pdf_with_image(tmp_path / "owner.pdf")
    _encrypt_pdf(owner_only, user_password="")
    user_pw = _pdf_with_image(tmp_path / "user.pdf")
    _encrypt_pdf(user_pw, user_password="secret")
    assert encryption.is_password_protected(owner_only) is False
    assert encryption.is_password_protected(user_pw) is True


# ---- PDF: 所有者パスワードだけなら普通に変換する ----

def test_owner_password_only_pdf_is_converted_normally(tmp_path, caplog):
    src = tmp_path / "src"
    pdf = _pdf_with_image(src / "owner.pdf")
    _encrypt_pdf(pdf, user_password="")
    caplog.set_level(logging.INFO)
    rep = _build(src, tmp_path / "out" / "md")
    assert rep["failed"] == 0 and rep["converted"] == 1 and rep["conversion_failures"] == []
    assert _BODY in (tmp_path / "out" / "md" / "owner.pdf.md").read_text(encoding="utf-8")
    assert not [r for r in _rows(caplog) if "パスワード" in r.getMessage()]


# ---- PDF: 利用者パスワード付きは 1 行の知らせ・理由は password_protected ----

def test_user_password_pdf_gives_one_plain_line_and_password_protected_reason(tmp_path, caplog):
    src = tmp_path / "src"
    pdf = _pdf_with_image(src / "docs" / "locked.pdf")
    _encrypt_pdf(pdf, user_password="secret")
    caplog.set_level(logging.INFO)
    rep = _build(src, tmp_path / "out" / "md")

    assert rep["conversion_failures"] == [{"doc": "docs/locked.pdf", "reason": "password_protected"}]
    assert failure_reasons.classify("password_protected") == "password_protected"
    assert rep["failed"] == 1 and rep["converted"] == 0
    lines = [r for r in _rows(caplog) if "パスワード付き" in r.getMessage()]
    assert len(lines) == 1 and "docs/locked.pdf" in lines[0].getMessage()
    assert lines[0].levelno <= logging.WARNING
    assert all(r.exc_info is None for r in _rows(caplog))
    assert (tmp_path / "out" / "md" / "docs" / "locked.pdf.md").is_file()      # 資料名だけは検索に出る
    summary = [r.getMessage() for r in _rows(caplog) if r.getMessage().startswith("MD化が終わりました")]
    assert len(summary) == 1 and "失敗 1 件（パスワード保護／暗号化 1）" in summary[0]


# ---- 暗号化 OOXML・旧形式: soffice を起動しない ----

_FAKE_SOFFICE = """#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "LibreOffice 7.5.0.0 fake"; exit 0; fi
echo "$@" >> "$FAKE_LAUNCHES"
exit 1
"""


def _fake_soffice(tmp_path, monkeypatch) -> Path:
    script = tmp_path / "fake_soffice.sh"
    script.write_text(_FAKE_SOFFICE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    launches = tmp_path / "launches.txt"
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(script))
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "libreoffice")
    monkeypatch.setenv("SHERPA_LEGACY_WORKERS", "2")
    monkeypatch.setenv("FAKE_LAUNCHES", str(launches))
    monkeypatch.delenv("SHERPA_MCP_ARMS", raising=False)
    legacy_convert._version_cache.clear()
    return launches


def test_encrypted_ooxml_and_legacy_files_are_reported_without_starting_soffice(tmp_path, monkeypatch, caplog):
    launches = _fake_soffice(tmp_path, monkeypatch)
    src = tmp_path / "src"
    src.mkdir()
    for name, data in _encrypted_samples().items():
        (src / name).write_bytes(data)
    caplog.set_level(logging.INFO)
    rep = _build(src, tmp_path / "out" / "md")

    assert sorted((f["doc"], f["reason"]) for f in rep["conversion_failures"]) == [
        ("enc.doc", "password_protected"), ("enc.docx", "password_protected"),
        ("enc.ppt", "password_protected"), ("enc.xls", "password_protected")]
    assert rep["legacy_conversion_failures"] == [] and rep["failed"] == 4
    assert not launches.exists()                                              # LibreOffice を待たない・起動もしない
    lines = [r for r in _rows(caplog) if "パスワード付き" in r.getMessage()]
    assert len(lines) == 4 and all(r.exc_info is None for r in _rows(caplog))


def test_password_protected_legacy_file_is_not_sent_to_prefetch(tmp_path, monkeypatch):
    _fake_soffice(tmp_path, monkeypatch)
    src = tmp_path / "src"
    src.mkdir()
    (src / "enc.xls").write_bytes(_encrypted_samples()["enc.xls"])
    (src / "plain.xls").write_bytes(_plain_samples()["plain.xls"])
    conv = office_md.convertible_exts()
    items = office_md._legacy_prefetch_items(
        [(src / "enc.xls", "enc.xls"), (src / "plain.xls", "plain.xls")],
        candidate=office_md.OFFICE_EXT, conv=conv | {".xls"}, cache_hit=lambda rp, rel: None)
    assert [rel for _rp, rel in items] == ["plain.xls"]


# ---- 二回目の更新で変換し直さない ----

def test_password_notice_is_reused_until_the_original_changes(tmp_path, monkeypatch):
    src = tmp_path / "src"
    pdf = _pdf_with_image(src / "locked.pdf")
    _encrypt_pdf(pdf, user_password="secret")
    derived = tmp_path / "out" / "md"
    made = []
    real = legacy_provenance.build_unavailable_evidence

    def counting(*args, **kwargs):
        made.append(kwargs.get("reason_code"))
        return real(*args, **kwargs)

    monkeypatch.setattr(legacy_provenance, "build_unavailable_evidence", counting)

    first = _build(src, derived)
    second = _build(src, derived)
    assert made == ["password_protected"]                                     # 2 回目は知らせを作り直さない
    for rep in (first, second):
        assert rep["conversion_failures"] == [{"doc": "locked.pdf", "reason": "password_protected"}]
        assert rep["failed"] == 1
    assert (derived / "locked.pdf.md").is_file() and (derived / "locked.pdf.md.meta.json").is_file()

    _pdf_with_image(src / "locked.pdf", text="UNLOCKED_BODY_5521")             # パスワードを外して置き換えた
    third = _build(src, derived)
    assert third["failed"] == 0 and third["converted"] == 1 and third["conversion_failures"] == []
    assert "UNLOCKED_BODY_5521" in (derived / "locked.pdf.md").read_text(encoding="utf-8")


# ---- 二重出力しない ----

def test_conversion_warning_appears_once_across_convert_log_and_run_log(tmp_path, monkeypatch, capsys):
    log_setup._reset_state_for_tests()
    try:
        log_setup.configure_logging(force=True, log_dir=tmp_path / "logs", owner="ingest", rotate=False)
        src = tmp_path / "src"
        src.mkdir()
        (src / "big.xlsx").write_bytes(b"PK\x03\x04" + b"0" * 64)
        monkeypatch.setattr(office_md, "_office_size_exceeded", lambda rp, ext: True)
        _build(src, tmp_path / "out" / "md")
        for h in logging.getLogger("sherpa.ingest.convert").handlers + logging.getLogger("sherpa").handlers:
            h.flush()
        convert_log = (tmp_path / "logs" / "convert.log").read_text(encoding="utf-8")
        run_log = capsys.readouterr().err
        assert convert_log.count("ファイルが大きすぎるため変換しません") == 1
        assert "ファイルが大きすぎるため変換しません" not in run_log
    finally:
        log_setup._reset_state_for_tests()


# ---- PDF の画像 1 枚の不一致 ----

def test_image_hash_mismatch_drops_only_that_image_and_keeps_the_body(tmp_path, monkeypatch, caplog):
    src = tmp_path / "src"
    _pdf_with_image(src / "sub" / "photo.pdf")
    real = evidence_spike._pdf_image_payload

    def changed_on_reextract(xobject, name):
        import sys
        image_name, raw, image = real(xobject, name)
        if sys._getframe(1).f_code.co_name == "extract_assets":
            raw = b"\0" + raw                                                  # 取り直した bytes が控えと食い違う
        return image_name, raw, image

    monkeypatch.setattr(evidence_spike, "_pdf_image_payload", changed_on_reextract)
    caplog.set_level(logging.INFO)
    derived = tmp_path / "out" / "md"
    rep = _build(src, derived)

    assert rep["failed"] == 0 and rep["rag_failed"] == 0 and rep["evidence_ir_failed"] == 0
    assert rep["converted"] == 1 and rep["conversion_failures"] == []
    rag = derived.parent / "rag" / "sub" / "photo.pdf.rag.md"
    assert _BODY in rag.read_text(encoding="utf-8")                            # 本文は検索用の表現に入る
    assets = derived.parent / "rag" / "sub" / "photo.pdf.assets"
    assert not assets.exists() or not any(assets.iterdir())                   # 食い違った画像だけが素材から外れる
    notes = [r for r in _rows(caplog) if "画像を取り出せなかった" in r.getMessage()]
    assert len(notes) == 1 and "sub/photo.pdf" in notes[0].getMessage()
    assert not [r for r in _rows(caplog) if "縮退" in r.getMessage()]
    assert all(r.exc_info is None for r in _rows(caplog))                      # Traceback を出さない


def test_extract_assets_raises_without_a_skip_list_so_other_callers_keep_their_contract(tmp_path):
    pdf = _pdf_with_image(tmp_path / "a.pdf")
    ir = evidence_spike.extract(pdf)
    for element in ir.elements:
        if element.type == "image_xobject":
            element.extension["asset_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        evidence_spike.extract_assets(pdf, ir, tmp_path / "assets")
    skipped: list = []
    assert evidence_spike.extract_assets(pdf, ir, tmp_path / "assets2", skipped=skipped) == []
    assert [s["reason"] for s in skipped] == ["hash_mismatch"] and skipped[0]["page"] == 1


def test_summary_line_counts_cache_restored_and_failures():
    line = office_md._convert_summary_line(
        10, 8, 5, 2, 0, [{"doc": "a.xlsx", "reason": "size_exceeded"}, {"doc": "b.pdf", "reason": "password_protected"}], [])
    assert line == ("MD化が終わりました: 対象 10 件・変換 8 件（うちキャッシュから 5 件）"
                    "・失敗 2 件（サイズ超過 1・パスワード保護／暗号化 1）・対象外 0 件")
