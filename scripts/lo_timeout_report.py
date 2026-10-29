"""LibreOffice の前段変換で時間切れになった資料の調べ（`scripts/lo_timeout_report.sh`）。
設計: docs/design/rag.md「アーム一覧」

直近の取り込みの失敗の一覧（`world_failed_docs`）のうち理由が `legacy_conversion_timeout` の資料を集め、
拡張子ごと・原本の大きさの帯ごとの件数と、.xls のシート数・行数の目安を出す。

守ること:
- 読むだけ。DB も原本も書き換えない。
- 資料のファイル名・フォルダ名・パスを出力に出さない（件数と帯だけ）。
"""
from __future__ import annotations

import argparse
import statistics
import struct
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

TIMEOUT_REASON = "legacy_conversion_timeout"

_MB = 1024 * 1024
SIZE_BANDS = (("1MB未満", 1 * _MB), ("1〜10MB", 10 * _MB), ("10〜50MB", 50 * _MB), ("50MB以上", None))
UNKNOWN_BAND = "大きさ不明"
_SHEET_BANDS = (("1枚", 2), ("2〜5枚", 6), ("6〜20枚", 21), ("21枚以上", None))
_ROW_BANDS = (("1万行未満", 10_000), ("1万〜10万行", 100_000), ("10万行以上", None))

_BIFF_BOUNDSHEET = 0x0085
_BIFF_DIMENSIONS = 0x0200


def _band_of(value: int, bands) -> str:
    """`(帯の名前, 上限（含まない）)` の並びから `value` の帯を選ぶ。上限 None は最後の帯。"""
    for name, upper in bands:
        if upper is None or value < upper:
            return name
    return bands[-1][0]


def size_band(size: int | None) -> str:
    """原本のバイト数を帯の名前にする（測れなければ「大きさ不明」）。"""
    return UNKNOWN_BAND if size is None else _band_of(size, SIZE_BANDS)


def scan_biff(stream: bytes) -> tuple[int, int]:
    """BIFF（.xls のブックのストリーム）を先頭から読み、`(シート数, 各シートの使用範囲の行数の合計)` を返す。

    シート数は BOUNDSHEET の数、行数は各シートの DIMENSIONS（最初の行〜最後の行の次）の差の合計。目安であり正確な値ではない。
    """
    sheets = rows = 0
    pos, end = 0, len(stream)
    while pos + 4 <= end:
        rid, size = struct.unpack_from("<HH", stream, pos)
        body = pos + 4
        if rid == _BIFF_BOUNDSHEET:
            sheets += 1
        elif rid == _BIFF_DIMENSIONS and body + size <= end:
            if size >= 14:
                first, last = struct.unpack_from("<II", stream, body)
            elif size >= 8:
                first, last = struct.unpack_from("<HH", stream, body)
            else:
                first = last = 0
            rows += max(0, last - first)
        pos = body + size
    return sheets, rows


def xls_shape(path: Path) -> tuple[int, int] | None:
    """.xls 原本の `(シート数, 行数の目安)`。OLE として読めない・ブックのストリームが無いときは None。"""
    try:
        import olefile
        with olefile.OleFileIO(str(path)) as ole:
            for name in ("Workbook", "Book"):
                if ole.exists(name):
                    return scan_biff(ole.openstream(name).read())
    except Exception:
        return None
    return None


def summarize(items: list[dict]) -> dict:
    """`items`（`{"ext": 拡張子, "size": バイト数 or None, "shape": (シート数, 行数) or None}`）を件数に畳む。名前・パスは持たない。"""
    by_ext: dict[str, dict[str, int]] = {}
    sheets_bands: dict[str, int] = {}
    rows_bands: dict[str, int] = {}
    sheet_counts: list[int] = []
    row_counts: list[int] = []
    xls_unread = 0
    for it in items:
        bands = by_ext.setdefault(it["ext"], {})
        band = size_band(it["size"])
        bands[band] = bands.get(band, 0) + 1
        if it["ext"] != ".xls":
            continue
        if it["shape"] is None:
            xls_unread += 1
            continue
        sheets, rows = it["shape"]
        sheet_counts.append(sheets)
        row_counts.append(rows)
        sb, rb = _band_of(sheets, _SHEET_BANDS), _band_of(rows, _ROW_BANDS)
        sheets_bands[sb] = sheets_bands.get(sb, 0) + 1
        rows_bands[rb] = rows_bands.get(rb, 0) + 1
    return {"total": len(items), "by_ext": by_ext, "xls_unread": xls_unread,
            "sheets_bands": sheets_bands, "rows_bands": rows_bands,
            "sheets_stat": _stat(sheet_counts), "rows_stat": _stat(row_counts)}


def _stat(values: list[int]) -> tuple[int, int] | None:
    return (max(values), int(statistics.median(values))) if values else None


def render(summary: dict, worlds: int) -> str:
    """件数の表。拡張子と帯の名前以外は出さない。"""
    lines = ["LibreOffice 前段変換の時間切れ（直近の取り込みの記録）",
             f"時間切れ: {summary['total']} 件（資料フォルダ {worlds} 個を調べた）"]
    if not summary["total"]:
        return "\n".join(lines + ["  時間切れの資料はありません"]) + "\n"
    names = [n for n, _ in SIZE_BANDS] + [UNKNOWN_BAND]
    lines += ["", "[拡張子 × 原本の大きさ]", "  " + "".join(f"{n:>12}" for n in ["拡張子"] + names + ["合計"])]
    for ext in sorted(summary["by_ext"]):
        counts = summary["by_ext"][ext]
        lines.append("  " + f"{ext:>12}" + "".join(f"{counts.get(n, 0):>12}" for n in names) + f"{sum(counts.values()):>12}")
    if ".xls" in summary["by_ext"]:
        lines += ["", "[.xls のシート数・行数の目安（原本の BIFF から数えた概算）]"]
        lines.append("  シート数: " + " / ".join(f"{n} {summary['sheets_bands'].get(n, 0)}件" for n, _ in _SHEET_BANDS))
        lines.append("  行数（各シートの使用範囲の合計）: "
                     + " / ".join(f"{n} {summary['rows_bands'].get(n, 0)}件" for n, _ in _ROW_BANDS))
        if summary["sheets_stat"]:
            lines.append(f"  最大・中央値: シート数 {summary['sheets_stat'][0]}・{summary['sheets_stat'][1]}、"
                         f"行数 {summary['rows_stat'][0]:,}・{summary['rows_stat'][1]:,}")
        if summary["xls_unread"]:
            lines.append(f"  読めなかった .xls: {summary['xls_unread']} 件（原本が無い・壊れている・暗号化など）")
    return "\n".join(lines) + "\n"


def collect(world_ids: list[str], failed_docs, world_dir) -> list[dict]:
    """時間切れの資料ごとに拡張子・大きさ・（.xls は）形を測る。`failed_docs(world)` は失敗の一覧、`world_dir(world)` は資料フォルダの実パス。"""
    items = []
    for world in world_ids:
        root = world_dir(world)
        for row in failed_docs(world):
            if row["reason"] != TIMEOUT_REASON:
                continue
            ext = Path(row["rel"]).suffix.lower() or "(なし)"
            path = Path(root) / row["rel"] if root is not None else None
            try:
                size = path.stat().st_size if path is not None else None
            except OSError:
                size = None
            shape = xls_shape(path) if ext == ".xls" and size is not None else None
            items.append({"ext": ext, "size": size, "shape": shape})
    return items


def main(argv: list[str] | None = None, *, stdout=None, failed_docs=None, world_dir=None, world_ids=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    parser = argparse.ArgumentParser(description="LibreOffice の前段変換で時間切れになった資料の調べ")
    parser.add_argument("--world", default="", help="資料フォルダ名。省略で全部")
    args = parser.parse_args(argv)
    if failed_docs is None or world_dir is None or world_ids is None:
        from sherpa import store, worlds
        failed_docs, world_dir = store.list_failed_docs, worlds.world_dir
        world_ids = worlds.discover_world_ids
    ids = [args.world.strip()] if args.world.strip() else list(world_ids())
    try:
        items = collect(ids, failed_docs, world_dir)
    except Exception as e:
        print(f"調べられません（失敗の一覧を読めません: {e.__class__.__name__}）", file=sys.stderr)
        return 3
    out.write(render(summarize(items), len(ids)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
