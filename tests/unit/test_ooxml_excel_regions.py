"""Excel（.xlsx）表候補抽出（`sherpa/ingest/ooxml/excel.py::regions()` 系）の単体テスト。

`regions()` は値グリッド・結合セル情報・背景色情報を受け取る純関数のため、多くのケースは openpyxl を
経由せず素の Python データ構造で検証する。`filled_cells()`（openpyxl ワークシート消費層）と実ファイルを通した
経路（`ooxml_arm._build_xlsx_ir`）は最小限の openpyxl ワークブックで別途検証する。
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import time
import zipfile

import openpyxl
import pytest
from openpyxl.styles import Font, GradientFill, PatternFill
from openpyxl.styles.colors import Color

from sherpa.ingest import office_md
from sherpa.ingest.arms import ooxml_arm
from sherpa.ingest.ooxml import excel

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_EVAL_XLSX_DIR = _ROOT / "fixtures" / "eval" / "excel_ja" / "inputs"


def _grid(rows: int, cols: int, values: dict[tuple[int, int], object]) -> list[list]:
    """1-based座標→値の疎な dict から `regions()` 向けの `ws_values` グリッド（0-based）を作る。"""
    g: list[list] = [[None] * cols for _ in range(rows)]
    for (r, c), v in values.items():
        g[r - 1][c - 1] = v
    return g


def _union(out) -> set:
    cells: set = set()
    for rg in out:
        cells |= rg.cells
    return cells


def _timed(fn, limit=5.0):
    started = time.monotonic()
    out = fn()
    elapsed = time.monotonic() - started
    assert elapsed < limit, f"遅い（実測 {elapsed:.2f}s）"
    return out


def _fill(color) -> PatternFill:
    return PatternFill(fill_type="solid", fgColor=color)


def _comb(n_fingers: int, r0: int = 0, c0: int = 0, finger_rows=(2, 3, 4)) -> dict:
    """1本の背骨（行1全列）から多数の「指」が垂れ下がる、非矩形性の強い連結成分。"""
    width = n_fingers * 2 - 1
    values = {(r0 + 1, c0 + c): "spine" for c in range(1, width + 1)}
    for i in range(n_fingers):
        for r in finger_rows:
            values[(r0 + r, c0 + 1 + i * 2)] = f"finger{i}"
    return values


def _saved(wb, tmp_path, name="a.xlsx"):
    p = tmp_path / name
    wb.save(p)
    return p


# ---- 背景色・結合セルによる占有化と橋渡し ----
M13 = {(1, c): {"anchor": (1, 1), "row_span": 1, "column_span": 3} for c in (1, 2, 3)}
M12 = {(1, c): {"anchor": (1, 1), "row_span": 1, "column_span": 2} for c in (1, 2)}
# id -> (grid, kwargs, 期待される (range, value_cell_count|None, cells|None) の列)
BRIDGE_CASES = {
    "no_bridge_stays_split": (_grid(1, 3, {(1, 1): "A", (1, 3): "B"}), dict(cap_rows=1, cap_cols=3),
                              [("A1:A1", None, None), ("C1:C1", None, None)]),
    "background_fill_bridges_adjacent_value_cells": (
        _grid(1, 3, {(1, 1): "A", (1, 3): "B"}), dict(cap_rows=1, cap_cols=3, filled={(1, 2)}),
        [("A1:C1", 2, frozenset({(1, 1), (1, 2), (1, 3)}))]),          # 橋渡しセル自体は値を持たないため数えない
    "filled_beyond_cap_ignored": (_grid(1, 3, {(1, 1): "A", (1, 3): "B"}), dict(cap_rows=1, cap_cols=2, filled={(1, 2)}),
                                  [("A1:B1", None, None)]),            # (1,3) は走査対象外・cap 内の塗りだけが占有
    "merge_without_info_splits_header_and_data": (
        _grid(2, 3, {(1, 1): "見出し", (2, 1): "a", (2, 2): "b", (2, 3): "c"}), dict(cap_rows=2, cap_cols=3),
        [("A1:A1", None, None), ("A2:C2", None, None)]),                # 結合情報が無ければ従来どおり分裂（既知の限界）
    "merge_continuation_bridges_header_and_data": (
        _grid(2, 3, {(1, 1): "見出し", (2, 1): "a", (2, 2): "b", (2, 3): "c"}), dict(cap_rows=2, cap_cols=3, merged=M13),
        [("A1:C2", 4, frozenset({(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3)}))]),   # 継続セル2つは数えない
    "fully_blank_merge_creates_no_region": (_grid(2, 2, {}), dict(cap_rows=2, cap_cols=2, merged=M12), []),
    "merge_with_fill_on_continuation_occupies_whole_span": (
        _grid(1, 2, {}), dict(cap_rows=1, cap_cols=2, merged=M12, filled={(1, 2)}),
        [("A1:B1", 0, frozenset({(1, 1), (1, 2)}))]),
    "blank_row_separator_keeps_tables_separate": (
        _grid(3, 2, {(1, 1): "a", (1, 2): "b", (3, 1): "c", (3, 2): "d"}), dict(cap_rows=3, cap_cols=2),
        [("A1:B1", None, None), ("A3:B3", None, None)]),
}


@pytest.mark.parametrize("grid,kwargs,expected", BRIDGE_CASES.values(), ids=BRIDGE_CASES)
def test_regions_bridging(grid, kwargs, expected):
    out = excel.regions(grid, **kwargs)
    assert sorted(rg.range for rg in out) == sorted(e[0] for e in expected)
    by_range = {rg.range: rg for rg in out}
    for rng, vcc, cells in expected:
        assert vcc is None or by_range[rng].value_cell_count == vcc
        assert cells is None or by_range[rng].cells == cells


# ---- ヒストグラム法の最大矩形反復抽出（隣接表・L字の癒着解消）----

def test_split_avoids_phantom_blank_cells_in_l_shaped_cluster():
    """表 A（5行×3列）と表 B（3行×3列）が列境界で接触しても、分割後はどの領域の外接矩形も非占有セルを含まず、
    セルの総数が変わらない（完全性）。"""
    values = {(r, c): f"A{r}{c}" for r in range(1, 6) for c in range(1, 4)}
    values.update({(r, c): f"B{r}{c}" for r in range(1, 4) for c in range(4, 7)})
    out = excel.regions(_grid(5, 6, values), cap_rows=5, cap_cols=6)
    assert len(out) >= 2                                              # 1つの外接矩形のままでは無い
    for rg in out:
        for r in range(rg.min_row, rg.max_row + 1):
            for c in range(rg.min_col, rg.max_col + 1):
                assert (r, c) in values, f"region {rg.range} が非占有セル ({r},{c}) を巻き込んでいる"
    assert _union(out) == set(values)


def test_split_is_deterministic_across_repeated_calls():
    grid = _grid(6, 6, {(r, c): "v" for r in range(1, 7) for c in range(1, 7) if (r + c) % 3 != 0})
    first = excel.regions(grid, cap_rows=6, cap_cols=6)
    second = excel.regions(grid, cap_rows=6, cap_cols=6)
    assert first == second and [rg.range for rg in first] == [rg.range for rg in second]


def test_split_cap_falls_back_without_losing_cells():
    """反復上限（`_MAX_RECT_SPLITS`）に達しても完全性が保たれ、実行時間が有界である。"""
    values = _comb(50)
    out = _timed(lambda: excel.regions(_grid(4, 99, values), cap_rows=4, cap_cols=99))
    assert _union(out) == set(values)


# ---- 表候補スコア（付与のみ・抑制はしない）----

def test_score_tapers_for_small_regions_and_saturates_for_dense_ones():
    rg = excel.regions(_grid(1, 1, {(1, 1): "x"}), cap_rows=1, cap_cols=1)[0]
    assert rg.value_cell_count == 1 and rg.density == 1.0
    assert rg.score == pytest.approx(min(1.0, 1 / excel._SCORE_MIN_CELLS))
    assert rg.score < 1.0                                              # 最小非空セル数未満は割り引く
    rg2 = excel.regions(_grid(3, 3, {(r, c): "x" for r in range(1, 4) for c in range(1, 4)}), cap_rows=3, cap_cols=3)[0]
    assert rg2.value_cell_count == 9 and rg2.density == 1.0
    assert rg2.score == pytest.approx(1.0)


def test_score_reflects_sparse_density_when_component_too_large_to_decompose():
    """外接矩形面積が `_MAX_RECT_DECOMPOSE_CELLS` を超える連結成分は分割せず単一の外接矩形のまま返すため、密度が 1.0 未満のまま観測できる。"""
    rows = 100
    cols = excel._MAX_RECT_DECOMPOSE_CELLS // rows + 1                # 面積が上限を必ず超えるよう動的に決める
    values = {(1, c): "h" for c in range(1, cols + 1)}
    values.update({(r, 1): "v" for r in range(1, rows + 1)})
    out = _timed(lambda: excel.regions(_grid(rows, cols, values), cap_rows=rows, cap_cols=cols))
    assert len(out) == 1
    rg = out[0]
    assert (rg.min_row, rg.max_row, rg.min_col, rg.max_col) == (1, rows, 1, cols)
    assert rg.value_cell_count == len(values) and 0.0 < rg.density < 1.0
    assert rg.score == pytest.approx(rg.density)


def test_huge_dense_single_table_bypasses_decomposition_and_stays_one_region():
    rows, cols = 2000, 30                                              # 面積 60,000 > _MAX_RECT_DECOMPOSE_CELLS
    values = {(r, c): "v" for r in range(1, rows + 1) for c in range(1, cols + 1)}
    out = _timed(lambda: excel.regions(_grid(rows, cols, values), cap_rows=rows, cap_cols=cols))
    assert len(out) == 1 and out[0].value_cell_count == len(values)
    assert out[0].score == pytest.approx(1.0)


def test_max_rect_decompose_cells_raised_above_old_value():
    assert excel._MAX_RECT_DECOMPOSE_CELLS >= 20_000                   # 5,000 へ戻す変更を検知する


# ---- シート全体の分割予算・領域数予算 ----

def test_sheet_wide_split_budget_bounds_many_comb_components():
    """独立した櫛形連結成分（50本指）が100個ある場合、シート全体の予算（`_MAX_RECT_SPLITS_PER_SHEET`/
    `_MAX_REGIONS_PER_SHEET`）が働いて高速に完了し、完全性が保たれる（予算なしでは 5,100 Region）。"""
    n_side, comb_rows, comb_width = 10, 4, 99
    values: dict = {}
    for bi in range(n_side):
        for bj in range(n_side):
            values.update(_comb(50, bi * (comb_rows + 1), bj * (comb_width + 1)))
    rows, cols = n_side * (comb_rows + 1), n_side * (comb_width + 1)
    out = _timed(lambda: excel.regions(_grid(rows, cols, values), cap_rows=rows, cap_cols=cols))
    assert _union(out) == set(values)
    assert len(out) < 1000, f"シート全体の分割予算が働いていない可能性（Region数={len(out)}）"


def test_two_row_comb_exceeds_region_budget_and_is_capped():
    """2行の櫛形で指が500本あると、フォールバック断片だけで領域数が膨らむ（上限なしでは501領域）。上限以内に収まり、
    予算切れの証跡があり、完全性が保たれる。"""
    values = _comb(500, finger_rows=(2,))
    width = 999
    out = _timed(lambda: excel.regions(_grid(2, width, values), cap_rows=2, cap_cols=width))
    assert len(out) <= excel._MAX_REGIONS_PER_SHEET
    assert any(rg.split_budget_exhausted for rg in out)
    assert _union(out) == set(values)


def test_checkerboard_many_isolated_components_all_stay_unsplit():
    """孤立した1セル連結成分が500個ある場合、各成分は最低1件の Region を出す（silent-drop ゼロ）契約により
    `_MAX_REGIONS_PER_SHEET`（256）を超えて500 Region になる。すべて未分割で完全性が保たれる。"""
    cols = 500
    values: dict = {}
    for c in range(1, cols + 1):
        if c % 2 == 1:
            values[(1, c)] = "a"
        else:
            values[(2, c)] = "b"
    out = _timed(lambda: excel.regions(_grid(2, cols, values), cap_rows=2, cap_cols=cols))
    assert len(out) == 500 and all(len(rg.cells) == 1 for rg in out)
    assert _union(out) == set(values)


def test_few_components_total_stays_within_region_budget():
    """連結成分が少なければ、断片化しうる1成分（500本指の櫛）が予算を独占せず合計が `_MAX_REGIONS_PER_SHEET` 以内。"""
    values = _comb(500, finger_rows=(2,))
    gap_col = 999 + 10
    for k in range(9):
        values[(4, gap_col + k * 3)] = f"iso{k}"
    rows, cols = 5, gap_col + 9 * 3 + 5
    out = excel.regions(_grid(rows, cols, values), cap_rows=rows, cap_cols=cols)
    assert len(out) <= excel._MAX_REGIONS_PER_SHEET
    assert _union(out) == set(values)


def test_leading_l_shape_component_folds_to_single_bbox_within_budget():
    """256 成分（先頭 L 字 3 セル＋孤立 255）では各成分の予算が1件ずつ。L 字は自然な分割に2件要るため分割を諦めて
    1つの外接矩形に畳み、合計が257に膨らまない。"""
    values: dict = {(1, 1): "a", (1, 2): "b", (2, 2): "c"}
    for i in range(255):
        values[(4, 10 + i * 2)] = f"iso{i}"
    rows, cols = 4, 10 + 255 * 2 + 2
    out = excel.regions(_grid(rows, cols, values), cap_rows=rows, cap_cols=cols)
    assert len(out) == 256
    l_shape = [rg for rg in out if len(rg.cells) == 3]
    assert len(l_shape) == 1 and l_shape[0].split_budget_exhausted is True
    assert _union(out) == set(values)


# ---- 背景色（filled_cells）: 白/自動/テーマ白の除外・条件付き書式は対象外 ----

def test_filled_cells_detects_pattern_fill_and_respects_caps():
    ws = openpyxl.Workbook().active
    ws["A1"] = "x"
    ws["B2"].fill = _fill("FFFF00")
    ws["E5"].fill = _fill("00FF00")
    assert excel.filled_cells(ws, cap_rows=10, cap_cols=10) == {(2, 2), (5, 5)}
    assert excel.filled_cells(ws, cap_rows=3, cap_cols=3) == {(2, 2)}      # cap 外の (5,5) は対象外


# 1セル（A1/B2）の塗り → 占有されるか
FILL_CASES = {
    "solid_white": (_fill("FFFFFFFF"), False),
    "solid_color": (_fill("FFFF0000"), True),
    "no_fill": (None, False),
    "gradient_is_occupied_without_crash": (GradientFill(stop=(Color(rgb="FFFF0000"), Color(rgb="FF00FF00"))), True),
    "gradient_all_white_stops": (GradientFill(stop=(Color(rgb="FFFFFFFF"), Color(auto=True))), False),
    "indexed_white_1": (_fill(Color(indexed=1)), False),
    "indexed_white_9_duplicate": (_fill(Color(indexed=9)), False),
    "theme_white_negative_tint_darkens": (_fill(Color(theme=0, tint=-0.5)), True),
    "indexed_white_negative_tint": (_fill(Color(indexed=1, tint=-0.5)), True),
    "rgb_white_negative_tint": (_fill(Color(rgb="FFFFFFFF", tint=-0.5)), True),
}


@pytest.mark.parametrize("fill,occupied", FILL_CASES.values(), ids=FILL_CASES)
def test_fill_boundary(fill, occupied):
    ws = openpyxl.Workbook().active
    ws["A1"] = "左"
    ws["C1"] = "右"
    if fill is not None:
        ws["B1"].fill = fill
    assert excel.filled_cells(ws, cap_rows=5, cap_cols=5) == ({(1, 2)} if occupied else set())
    if occupied:                                                          # 橋渡しセルとして使うと分裂しない
        out = excel.regions(_grid(1, 3, {(1, 1): "左", (1, 3): "右"}), cap_rows=1, cap_cols=3,
                            filled=excel.filled_cells(ws, cap_rows=1, cap_cols=3))
        assert len(out) == 1


def _theme_ws(tmp_path, n=1):
    """テーマ色（`theme=0`）で塗った n セル（A1..）を保存・再読込したワークシート（loaded_theme は実ファイル由来）。"""
    wb = openpyxl.Workbook()
    for i in range(n):
        wb.active.cell(row=1, column=i + 1).fill = _fill(Color(theme=0))
    return openpyxl.load_workbook(_saved(wb, tmp_path))


def test_theme_white_not_occupied_but_unresolvable_theme_is_occupied(tmp_path):
    """標準テーマの白は非占有。閉じタグ欠落の壊れた XML・`lastClr` が `garbageFFFFFF` のような不正値は
    解決不能＝占有側に倒れる（末尾一致だけで白と誤受理しない）。"""
    assert excel.filled_cells(_theme_ws(tmp_path).active, cap_rows=5, cap_cols=5) == set()

    reloaded = _theme_ws(tmp_path)
    good = reloaded.loaded_theme.decode("utf-8")
    assert "<a:lt1>" in good and "</a:lt1>" in good
    reloaded.loaded_theme = good.replace("</a:lt1>", "").encode("utf-8")
    with pytest.raises(Exception):
        excel.ET.fromstring(reloaded.loaded_theme)                        # 壊れている前提の確認
    assert excel.filled_cells(reloaded.active, cap_rows=5, cap_cols=5) == {(1, 1)}

    reloaded = _theme_ws(tmp_path)
    good = reloaded.loaded_theme.decode("utf-8")
    assert 'lastClr="FFFFFF"' in good
    reloaded.loaded_theme = good.replace('lastClr="FFFFFF"', 'lastClr="garbageFFFFFF"').encode("utf-8")
    assert excel.filled_cells(reloaded.active, cap_rows=5, cap_cols=5) == {(1, 1)}


def test_custom_indexed_and_theme_palette_resolved_as_occupied(tmp_path):
    """標準では白（indexed 1・9／theme 背景1）でも、ワークブックがカスタム定義で赤へ上書きしていれば占有として扱う。"""
    from openpyxl.writer.theme import theme_xml
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"].fill = _fill(Color(indexed=1))
    ws["A2"].fill = _fill(Color(indexed=9))
    ws["A3"].fill = _fill(Color(theme=0))
    colors = list(wb._colors)
    colors[1] = colors[9] = "00FF0000"
    wb._colors = colors
    wb.loaded_theme = re.sub(r"<a:lt1>.*?</a:lt1>", '<a:lt1><a:srgbClr val="FF0000"/></a:lt1>', theme_xml,
                             flags=re.S).encode("utf-8")
    reloaded = openpyxl.load_workbook(_saved(wb, tmp_path)).active
    assert excel.filled_cells(reloaded, cap_rows=5, cap_cols=5) == {(1, 1), (2, 1), (3, 1)}


def test_theme_resolution_is_cached_per_workbook(tmp_path, monkeypatch):
    """`loaded_theme` のパース（`_resolve_theme_lt1_rgb`）は、ワークブック内に多数のテーマ色セルがあっても1回だけ
    （`filled_cells()` の入口の `ColorResolver` がキャッシュする）。"""
    ws = _theme_ws(tmp_path, n=50).active
    calls = []
    original = excel._resolve_theme_lt1_rgb
    monkeypatch.setattr(excel, "_resolve_theme_lt1_rgb", lambda wb_arg: calls.append(1) or original(wb_arg))
    assert excel.filled_cells(ws, cap_rows=5, cap_cols=51) == set()
    assert len(calls) == 1


def test_color_resolver_reused_across_sheets_via_build_xlsx_ir(tmp_path, monkeypatch):
    """複数シートでも `ColorResolver` はワークブックあたり1つで使い回し、テーマ XML のパースは1回だけ。"""
    wb = openpyxl.Workbook()
    wb.active.title = "シート1"
    wb.active["A1"].fill = _fill(Color(theme=0))
    wb.create_sheet("シート2")["A1"].fill = _fill(Color(theme=0))
    p = _saved(wb, tmp_path)
    calls = []
    original = excel._resolve_theme_lt1_rgb
    monkeypatch.setattr(excel, "_resolve_theme_lt1_rgb", lambda wb_arg: calls.append(1) or original(wb_arg))
    assert ooxml_arm._build_xlsx_ir(p) is not None
    assert len(calls) == 1


def test_is_white_hex_rejects_malformed_values():
    """文字列全体の形式（6桁 or ARGB8桁の16進）を厳密に検証する（`endswith("FFFFFF")` だけだと `garbageFFFFFF` を誤受理）。"""
    f = excel._is_white_hex
    assert f("FFFFFF") is True and f("ffffff") is True and f("00FFFFFF") is True
    for bad in ("garbageFFFFFF", "GGFFFFFF", "FFFF", None, 123456):
        assert f(bad) is False


# ---- end-to-end: ooxml_arm._build_xlsx_ir 経由の配線確認 ----

def _tables(doc):
    return [e for e in doc.elements if e.type == "table"]


def test_build_xlsx_ir_wires_background_fill_bridge_and_score(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "シート1"
    ws["A1"], ws["C1"] = "左", "右"
    ws["B1"].fill = _fill("FFFF00")                                       # 値なしの橋渡しセル
    doc = ooxml_arm._build_xlsx_ir(_saved(wb, tmp_path))
    assert doc is not None
    tables = _tables(doc)
    assert len(tables) == 1                                               # filled 配線: 背景色の橋渡しで1領域
    assert isinstance(tables[0].source_map.get("score"), float)
    texts = {(c.row, c.column): c.text for c in tables[0].cells}
    assert texts[(1, 1)] == "左" and texts[(1, 3)] == "右"


def test_build_xlsx_ir_wires_merge_continuation_bridge(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "シート1"
    ws["A1"] = "見出し"
    ws.merge_cells("A1:C1")
    ws["A2"], ws["B2"], ws["C2"] = "a", "b", "c"
    doc = ooxml_arm._build_xlsx_ir(_saved(wb, tmp_path))
    assert doc is not None and len(_tables(doc)) == 1                     # merged 配線: 継続セルの橋渡しで1領域


def test_split_budget_exhausted_propagates_to_table_source_map(tmp_path):
    """予算切れフォールバックの `table:N` だけに `split_budget_exhausted: true` が付き、通常の `table:N` にはキー自体が無い
    （`truncated` と同じ「False の時はキーを書かない」規約）。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "シート1"
    for c in range(1, 1000):
        ws.cell(row=1, column=c, value="s")
    for i in range(500):
        ws.cell(row=2, column=1 + i * 2, value=f"f{i}")
    for r, c, v in ((10, 1, "x"), (10, 2, "y"), (11, 1, "z"), (11, 2, "w")):      # 離れた小さな2x2ブロック＝通常側
        ws.cell(row=r, column=c, value=v)
    doc = ooxml_arm._build_xlsx_ir(_saved(wb, tmp_path))
    assert doc is not None
    tables = _tables(doc)
    assert len(tables) <= excel._MAX_REGIONS_PER_SHEET
    assert any(t.source_map.get("split_budget_exhausted") is True for t in tables)
    assert any("split_budget_exhausted" not in t.source_map for t in tables)
    assert all(t.source_map.get("split_budget_exhausted", True) is True for t in tables)


# ---- ING-1: 静かな部分抽出の検知（宣言行数 vs 実際に値が入っていた行数）----

def _sheet_source_map(doc):
    sheets = [e for e in doc.elements if e.type == "sheet"]
    assert len(sheets) == 1
    return sheets[0].source_map


def _styled_far_row_xlsx(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "シート1"
    ws["A1"] = "唯一の値"
    ws.cell(row=150, column=1).font = Font(bold=True)      # 値なし・スタイルだけの遠い行（宣言行数を伸ばす）
    return _saved(wb, tmp_path)


def _dense_xlsx(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "シート1"
    for r in range(1, 11):
        ws.cell(row=r, column=1, value=f"row{r}")
    return _saved(wb, tmp_path)


def test_build_xlsx_ir_flags_partial_extraction_when_declared_rows_far_exceed_extracted(tmp_path):
    sm = _sheet_source_map(ooxml_arm._build_xlsx_ir(_styled_far_row_xlsx(tmp_path)))
    assert sm.get("truncated") is not True                                # cap 打切り（自己申告）ではない
    assert sm.get("partial_extraction_suspected") is True
    assert sm.get("declared_rows") == 150 and sm.get("extracted_rows") == 1


def test_build_xlsx_ir_does_not_flag_normal_dense_sheet(tmp_path):
    assert "partial_extraction_suspected" not in _sheet_source_map(ooxml_arm._build_xlsx_ir(_dense_xlsx(tmp_path)))


def test_build_xlsx_ir_does_not_flag_when_cap_truncation_already_self_declared(tmp_path, monkeypatch):
    """予算打切り（`truncated` の自己申告）は正常なので、宣言/抽出の比率が閾値を満たしても部分抽出の疑いに計上しない。"""
    monkeypatch.setattr(excel, "sheet_truncated", lambda *a, **kw: True)
    sm = _sheet_source_map(ooxml_arm._build_xlsx_ir(_styled_far_row_xlsx(tmp_path)))
    assert sm.get("truncated") is True and "partial_extraction_suspected" not in sm


def test_build_xlsx_ir_does_not_flag_sparse_but_genuine_real_fixture():
    """`JPX-007.xlsx`（宣言501行・非空は A1 と 501 行目のみ）は最終非空行が宣言終端に達しているため疑いに計上しない
    （単純な行数比だけだと 2/501 を誤検知する）。"""
    p = _EVAL_XLSX_DIR / "JPX-007.xlsx"
    if not p.is_file():
        pytest.skip("実 fixture が無い環境")
    doc = ooxml_arm._build_xlsx_ir(p)
    assert doc is not None
    sheets = [e for e in doc.elements if e.type == "sheet" and e.source_map.get("sheet") == "境界"]
    assert len(sheets) == 1
    assert sheets[0].source_map.get("truncated") is not True
    assert "partial_extraction_suspected" not in sheets[0].source_map


# ---- 実ファイルでの回帰（silent-drop ゼロ）----

def _small_real_xlsx_fixtures(max_bytes: int = 60_000) -> list[pathlib.Path]:
    if not _EVAL_XLSX_DIR.is_dir():
        return []
    return sorted(p for p in _EVAL_XLSX_DIR.glob("*.xlsx") if p.stat().st_size <= max_bytes)


def _expected_nonblank_cells(path: pathlib.Path) -> dict[tuple[str, int, int], str]:
    """`_build_xlsx_ir` と独立に、ファイルの非空セルを直接読んで期待値を作る（オラクル）。"""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    try:
        out: dict[tuple[str, int, int], str] = {}
        for ws in wb.worksheets:
            cap_rows = excel.effective_cap_rows(ws.max_column)
            max_row = min(ws.max_row or 1, cap_rows + 1)
            max_col = min(ws.max_column or 1, excel.DEFAULT_CAP_COLS + 1)
            for r, row in enumerate(
                    ws.iter_rows(min_row=1, max_row=max_row, min_col=1, max_col=max_col, values_only=True), start=1):
                if r > cap_rows:
                    continue
                for c, v in enumerate(row, start=1):
                    if c <= excel.DEFAULT_CAP_COLS and v is not None and str(v).strip() != "":
                        out[(ws.title, r, c)] = str(v)
        return out
    finally:
        wb.close()


@pytest.mark.parametrize("path", _small_real_xlsx_fixtures(), ids=lambda p: p.name)
def test_real_fixture_no_silent_drop(path):
    """シート上の非空セルが1つ残らずいずれかの table 要素へ現れる（占有判定/分割を変えても取りこぼしが増えない）。"""
    doc = ooxml_arm._build_xlsx_ir(path)
    assert doc is not None
    actual = {(el.source_map["sheet"], cell.row, cell.column): cell.text
              for el in doc.elements if el.type == "table" for cell in el.cells or [] if cell.text}
    expected = _expected_nonblank_cells(path)
    missing = set(expected) - set(actual)
    assert not missing, f"{path.name}: 取りこぼしたセル {sorted(missing)[:10]}"
    mismatched = {k for k in expected if k in actual and actual[k] != expected[k]}
    assert not mismatched, f"{path.name}: 値不一致 {sorted(mismatched)[:10]}"


def test_jpx021_large_component_no_longer_degrades_to_single_bbox():
    """JPX-021.xlsx「統合設計」シートの2連結成分（面積6,760・5,096）は旧上限（5,000）では単一外接矩形へ縮退していた。
    上限引き上げ後は複数矩形へ分割され `split_budget_exhausted` が消える。"""
    path = _EVAL_XLSX_DIR / "JPX-021.xlsx"
    if not path.is_file():
        pytest.skip(f"fixture が無い環境: {path}")
    doc = _timed(lambda: ooxml_arm._build_xlsx_ir(path))
    assert doc is not None
    sheet = next(e for e in doc.elements if e.type == "sheet" and e.source_map.get("sheet") == "統合設計")
    tables = [e for e in doc.elements if e.type == "table" and e.parent_id == sheet.element_id]
    exhausted = [t for t in tables if t.source_map.get("split_budget_exhausted")]
    assert exhausted == [], f"縮退が残っている: {[t.source_map.get('range') for t in exhausted]}"
    assert len(tables) > 27, "旧上限では27 table だった（縮退2件込み）——分割後は増えるはず"


# ---- XLSX_EXTRACTOR_VERSION が document_ir/evidence_ir の署名に含まれる契約 ----

def _sig_component(sig: str, key: str) -> str | None:
    for part in sig.split(";"):
        if part.startswith(key + "="):
            return part[len(key) + 1:]
    return None


def test_xlsx_extractor_version_included_in_document_and_evidence_sig():
    assert _sig_component(office_md._current_document_ir_sig(), "xlsx") == ooxml_arm.XLSX_EXTRACTOR_VERSION
    assert _sig_component(office_md._current_evidence_ir_sig(), "xlsx") == ooxml_arm.XLSX_EXTRACTOR_VERSION


def test_upgrade_from_v1_signature_regenerates_v2_region_structure(tmp_path):
    """v1（旧 XLSX_EXTRACTOR_VERSION）構築済みの world を模し、drift 判定→refresh 関数の直接呼び出しで Document/Evidence IR が
    現行版へ更新される**成果物そのもの**を検証する。マーカーだけ書き換わって中身が古いままの取り違えを検出するため
    `.document.json` を壊してから refresh し、v2 固有の領域構造（隣接2表の分割・`score`）が再生成されることを確認する。"""
    world = tmp_path / "world"
    world.mkdir()
    derived = tmp_path / "derived"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "シート1"
    for r in range(1, 6):                                    # 隣接する2表（列境界で接触・癒着）
        for c in range(1, 4):
            ws.cell(row=r, column=c, value=f"A{r}{c}")
    for r in range(1, 4):
        for c in range(4, 7):
            ws.cell(row=r, column=c, value=f"B{r}{c}")
    wb.save(world / "a.xlsx")

    office_md.build_derived(world, derived)
    doc_path = derived.parent / "ir" / "a.xlsx.document.json"
    evidence_path = derived.parent / "ir" / "a.xlsx.evidence.json"
    original_doc_json = doc_path.read_text(encoding="utf-8")
    original_evidence_json = evidence_path.read_bytes()
    v2_tables = [e for e in json.loads(original_doc_json)["elements"] if e["type"] == "table"]
    assert len(v2_tables) == 2 and all("score" in t["source_map"] for t in v2_tables)

    doc_marker = derived / office_md._DOCUMENT_IR_SIG_MARKER
    evidence_marker = derived / office_md._EVIDENCE_IR_SIG_MARKER
    current_doc_sig = doc_marker.read_text(encoding="utf-8")
    current_evidence_sig = evidence_marker.read_text(encoding="utf-8")
    xlsx_component = f"xlsx={ooxml_arm.XLSX_EXTRACTOR_VERSION}"
    assert xlsx_component in current_doc_sig and f"{xlsx_component};" in current_evidence_sig
    doc_marker.write_text(current_doc_sig.replace(xlsx_component, "xlsx=xlsx-ooxml-v1"), encoding="utf-8")
    evidence_marker.write_text(current_evidence_sig.replace(f"{xlsx_component};", "xlsx=xlsx-ooxml-v1;"), encoding="utf-8")
    doc_path.write_text('{"corrupted": "v1-era placeholder"}', encoding="utf-8")
    evidence_path.write_text('{"corrupted": "v1-era placeholder"}', encoding="utf-8")
    assert office_md.document_ir_sig_drift(derived) is True and office_md.evidence_ir_sig_drift(derived) is True

    assert office_md.refresh_document_ir(world, derived)["document_ir_failed"] == 0
    assert office_md.refresh_evidence_ir(world, derived)["evidence_ir_failed"] == 0
    assert office_md.document_ir_sig_drift(derived) is False and office_md.evidence_ir_sig_drift(derived) is False
    assert doc_marker.read_text(encoding="utf-8") == current_doc_sig
    assert evidence_marker.read_text(encoding="utf-8") == current_evidence_sig
    regenerated = doc_path.read_text(encoding="utf-8")
    assert regenerated == original_doc_json                      # 壊した内容ではなく v2 の中身が復元される
    regen_tables = [e for e in json.loads(regenerated)["elements"] if e["type"] == "table"]
    assert len(regen_tables) == 2 and all("score" in t["source_map"] for t in regen_tables)
    assert evidence_path.read_bytes() == original_evidence_json


# ---- 結合セルの異常・巨大宣言 ----

def test_merge_non_anchor_raw_value_is_dropped_without_crash(tmp_path):
    """結合範囲（A1:B1）の非anchor（B1）だけに raw XML で値がある異常 OOXML は、openpyxl の通常ロードで値が失われる
    （既知の限界）。クラッシュせず、anchor（塗りあり）の範囲が領域として出力される。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "シート1"
    ws.merge_cells("A1:B1")
    ws["A1"].fill = _fill("FFFF0000")
    src = _saved(wb, tmp_path)
    needle = '<row r="1"><c r="A1" s="1" t="n"></c></row>'
    with zipfile.ZipFile(src) as zf:
        contents = {n: zf.read(n) for n in zf.namelist()}
    sheet_xml = contents["xl/worksheets/sheet1.xml"].decode("utf-8")
    assert needle in sheet_xml
    contents["xl/worksheets/sheet1.xml"] = sheet_xml.replace(
        needle, needle[:-len("</row>")] + '<c r="B1" t="inlineStr"><is><t>異常値</t></is></c></row>').encode("utf-8")
    malformed = tmp_path / "b.xlsx"
    with zipfile.ZipFile(malformed, "w") as zf:
        for name, data in contents.items():
            zf.writestr(name, data)

    assert openpyxl.load_workbook(malformed).active["B1"].value is None      # 非anchorの値は破棄される（受容記録）
    doc = ooxml_arm._build_xlsx_ir(malformed)
    assert doc is not None
    assert any(t.source_map.get("range") == "A1:B1" for t in _tables(doc))


def _inject_declared_merge(ws, range_str: str):
    """`ws.merge_cells()`（結合範囲内の全セルを MergedCell へ変換して極端に遅い）を経由せず、宣言だけの結合を安価に注入する。"""
    from openpyxl.worksheet.merge import MergedCellRange
    ws.merged_cells.ranges.add(MergedCellRange(ws, range_str))


def test_clip_merge_enumeration_bounds_caps_area_not_just_each_axis():
    """各軸のクリップだけでなく面積（積）も `DEFAULT_CAP_CELLS` 以内へ追加でクリップする。"""
    max_row, max_col = excel._clip_merge_enumeration_bounds(1, 1, 1_048_576, 16_384, cap_rows=1_000_000, cap_cols=16_384)
    assert max_row * max_col <= excel.DEFAULT_CAP_CELLS
    assert max_col == 16_384                                     # 列側は cap_cols のまま


def test_huge_declared_merge_is_clipped_before_enumeration_bounded_time():
    """A1:XFD1048576（Excel 絶対上限）の宣言でも、座標展開前に cap でクリップされ現実的な時間・件数で完走する
    （`merged_map()` と `filled_cells()` の `anchor_of` 事前構築の両方）。宣言どおりの span は保持する。"""
    ws = openpyxl.Workbook().active
    ws["A1"] = "x"
    _inject_declared_merge(ws, "A1:XFD1048576")
    merges = _timed(lambda: excel.merged_map(ws, cap_rows=100, cap_cols=100), limit=2.0)
    assert len(merges) == 100 * 100
    assert merges[(1, 1)]["row_span"] == 1_048_576 and merges[(1, 1)]["column_span"] == 16_384
    assert (101, 1) not in merges and (1, 101) not in merges
    _timed(lambda: excel.filled_cells(ws, cap_rows=100, cap_cols=100), limit=2.0)


# ---- 画像の存在（枚数）検出（`picture_counts_by_sheet`・人間向けMD注記用）----

def test_picture_counts_by_sheet_real_fixture():
    """DEP-XLSX-MARKERS.xlsx は「対象」シートに画像1枚のみ。0枚のシートはキー自体を持たない。"""
    path = _ROOT / "fixtures" / "eval" / "deprecation_markers" / "inputs" / "DEP-XLSX-MARKERS.xlsx"
    if not path.is_file():
        pytest.skip(f"fixture が無い環境: {path}")
    with zipfile.ZipFile(path) as z:
        assert excel.picture_counts_by_sheet(z) == {"対象": 1}


def test_picture_counts_by_sheet_no_drawing_part_returns_empty(tmp_path):
    wb = openpyxl.Workbook()
    wb.active["A1"] = "x"
    with zipfile.ZipFile(_saved(wb, tmp_path, "no_image.xlsx")) as z:
        assert excel.picture_counts_by_sheet(z) == {}


def test_picture_counts_by_sheet_counts_pictures_inside_group(tmp_path):
    """`xdr:grpSp`（グループ化図形）の中の画像も数える（トップレベル anchor だけでなく子孫まで）。
    openpyxl はグループ化図形の書き込み API を持たないため drawing part を直接注入する。"""
    wb = openpyxl.Workbook()
    wb.active["A1"] = "x"
    p = _saved(wb, tmp_path)
    pic = ('<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{i}" name="Image {i}"/><xdr:cNvPicPr/></xdr:nvPicPr>'
           '<xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill><xdr:spPr/></xdr:pic>')
    drawing_xml = (
        '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<xdr:oneCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff>'
        '<xdr:row>0</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:ext cx="1" cy="1"/>'
        '<xdr:grpSp><xdr:nvGrpSpPr><xdr:cNvPr id="1" name="Group 1"/><xdr:cNvGrpSpPr/></xdr:nvGrpSpPr><xdr:grpSpPr/>'
        + pic.format(i=2) + pic.format(i=3) + '</xdr:grpSp><xdr:clientData/></xdr:oneCellAnchor></xdr:wsDr>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{kind}" '
            'Target="{target}"/></Relationships>')

    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(p) as z:
        z.extractall(unpacked)
    files = {"xl/drawings/drawing1.xml": drawing_xml,
             "xl/drawings/_rels/drawing1.xml.rels": rels.format(kind="image", target="../media/image1.png"),
             "xl/worksheets/_rels/sheet1.xml.rels": rels.format(kind="drawing", target="../drawings/drawing1.xml")}
    for name, text in files.items():
        (unpacked / name).parent.mkdir(parents=True, exist_ok=True)
        (unpacked / name).write_text(text, encoding="utf-8")
    (unpacked / "xl" / "media").mkdir(parents=True, exist_ok=True)
    (unpacked / "xl" / "media" / "image1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    sheet1 = unpacked / "xl" / "worksheets" / "sheet1.xml"
    sheet1.write_text(sheet1.read_text(encoding="utf-8").replace(
        "</worksheet>", '<drawing xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                        'r:id="rId1"/></worksheet>'), encoding="utf-8")

    repacked = tmp_path / "grouped_with_image.xlsx"
    with zipfile.ZipFile(repacked, "w", zipfile.ZIP_DEFLATED) as zw:
        for f in unpacked.rglob("*"):
            if f.is_file():
                zw.write(f, f.relative_to(unpacked))
    shutil.rmtree(unpacked)
    with zipfile.ZipFile(repacked) as z:
        assert excel.picture_counts_by_sheet(z) == {"Sheet": 2}
