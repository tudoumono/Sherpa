"""`scripts/lo_timeout_report.py`: 時間切れの資料を拡張子×大きさの帯で数え、ファイル名・資料フォルダ名・パスを出力に出さない。"""
from __future__ import annotations

import importlib.util
import io
import struct
import sys
from pathlib import Path

import pytest

from sherpa.ingest.arms import legacy_convert

_SPEC = importlib.util.spec_from_file_location(
    "lo_timeout_report", Path(__file__).resolve().parents[2] / "scripts" / "lo_timeout_report.py")
REPORT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = REPORT
_SPEC.loader.exec_module(REPORT)

_MB = 1024 * 1024


def _record(rid: int, body: bytes) -> bytes:
    return struct.pack("<HH", rid, len(body)) + body


def _sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


def test_size_band_boundaries():
    assert REPORT.size_band(0) == "1MB未満" and REPORT.size_band(_MB - 1) == "1MB未満"
    assert REPORT.size_band(_MB) == "1〜10MB" and REPORT.size_band(10 * _MB - 1) == "1〜10MB"
    assert REPORT.size_band(10 * _MB) == "10〜50MB" and REPORT.size_band(50 * _MB - 1) == "10〜50MB"
    assert REPORT.size_band(50 * _MB) == "50MB以上"
    assert REPORT.size_band(None) == "大きさ不明"


def test_scan_biff_counts_sheets_and_rows_from_records():
    stream = (_record(0x0809, b"\x00" * 16)
              + _record(0x0085, b"\x00" * 8) + _record(0x0085, b"\x00" * 8)
              + _record(0x0200, struct.pack("<IIHH", 0, 100, 0, 5) + b"\x00\x00")
              + _record(0x0200, struct.pack("<IIHH", 10, 20010, 0, 5) + b"\x00\x00"))
    assert REPORT.scan_biff(stream) == (2, 20100)
    assert REPORT.scan_biff(b"") == (0, 0)
    assert REPORT.scan_biff(b"\x85\x00\xff\xff") == (1, 0)           # 長さが残りを超える記録でも落ちない


@pytest.mark.skipif(not legacy_convert.soffice_available(), reason="soffice が無い")
def test_xls_shape_reads_a_real_xls(tmp_path):
    import subprocess

    import xlsxwriter

    src = tmp_path / "src.xlsx"
    wb = xlsxwriter.Workbook(src)
    for name in ("a", "b"):
        sheet = wb.add_worksheet(name)
        for r in range(30):
            sheet.write_number(r, 0, r)
    wb.close()
    subprocess.run([legacy_convert._soffice_bin(), "--headless", "--convert-to", "xls", "--outdir", str(tmp_path / "o"),
                    f"-env:UserInstallation={(tmp_path / 'p').as_uri()}", str(src)],
                   check=True, capture_output=True, timeout=120)
    assert REPORT.xls_shape(tmp_path / "o" / "src.xls") == (2, 60)
    assert REPORT.xls_shape(tmp_path / "src.xlsx") is None            # OLE ではない


def test_report_counts_by_extension_and_band_without_any_name(tmp_path):
    root = tmp_path / "secret-world-dir"
    files = {"機密/極秘設計書.xls": 60 * _MB, "機密/別の台帳.xls": 5 * _MB, "機密/議事録.doc": 200_000,
             "機密/ok失敗.ppt": 2 * _MB}
    for rel, size in files.items():
        _sparse(root / rel, size)
    rows = [{"rel": rel, "reason": REPORT.TIMEOUT_REASON} for rel in files]
    rows.append({"rel": "機密/消えた.xls", "reason": REPORT.TIMEOUT_REASON})          # 原本が無い＝大きさ不明
    rows.append({"rel": "機密/別の失敗.doc", "reason": "legacy_conversion_failed"})   # 時間切れ以外は数えない
    out = io.StringIO()
    code = REPORT.main(["--world", "機密プロジェクト"], stdout=out,
                       failed_docs=lambda w: rows if w == "機密プロジェクト" else [],
                       world_dir=lambda w: root, world_ids=lambda: [])
    text = out.getvalue()
    assert code == 0
    assert "時間切れ: 5 件" in text
    by_ext = REPORT.summarize(REPORT.collect(["w"], lambda w: rows, lambda w: root))["by_ext"]
    assert by_ext == {".xls": {"50MB以上": 1, "1〜10MB": 1, "大きさ不明": 1},
                      ".doc": {"1MB未満": 1}, ".ppt": {"1〜10MB": 1}}
    assert "読めなかった .xls: 3 件" in text                         # 中身が OLE ではない 2 件＋原本が無い 1 件
    for forbidden in ("機密", "極秘", "設計書", "台帳", "議事録", "secret-world-dir", "ok失敗", str(tmp_path)):
        assert forbidden not in text


def test_report_with_no_timeouts_says_so():
    out = io.StringIO()
    code = REPORT.main([], stdout=out, failed_docs=lambda w: [{"rel": "a.doc", "reason": "legacy_conversion_failed"}],
                       world_dir=lambda w: None, world_ids=lambda: ["w1", "w2"])
    assert code == 0 and "時間切れ: 0 件（資料フォルダ 2 個を調べた）" in out.getvalue()
    assert "時間切れの資料はありません" in out.getvalue()
