"""Excel の範囲読み（`xlsx_range`）の、行・列を削った後の範囲の付け直し。
設計: docs/design/interfaces.md「読み取り部品（境界としての切り出し済み・手順②）」
"""
from __future__ import annotations


def _xlsx_refresh_next(r: dict, orig: dict) -> dict:
    """行・列を削った `r` の `range` に合わせて `next_range`／`next_cols_range` を付け直す（要求した広がりは元の結果の `range` と `next_*` から戻す）。"""
    from openpyxl.utils.cell import range_boundaries
    from ... import doc_readers
    try:
        c0, r0, c1, r1 = range_boundaries(r["range"])
        _oc0, _or0, req_c, req_r = range_boundaries(orig["range"])
        for key in ("next_range", "next_cols_range"):
            if orig.get(key):
                _a, _b, nc, nr = range_boundaries(orig[key])
                req_c, req_r = max(req_c, nc), max(req_r, nr)
    except Exception:
        return r
    out = {k: v for k, v in r.items() if k not in ("next_range", "next_cols_range")}
    out.update(doc_readers.xlsx_next_ranges(c0, r0, c1, r1, req_c, req_r))
    return out


def _range_right_col(a1_range: str | None, fallback: int) -> int:
    """A1 レンジの右端の絶対列番号。解析できなければ `fallback`。"""
    from openpyxl.utils.cell import range_boundaries
    try:
        return range_boundaries(a1_range)[2]
    except Exception:
        return fallback
