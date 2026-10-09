"""Excel（.xlsx）の生 OOXML 抽出層。`arms/ooxml_arm._build_xlsx_ir` が消費する純関数群で、MD が表示しない構造（非表示シート/行/列・名前付き範囲・コメント・ハイパーリンク・外部ブック参照・取り消し線・画像の存在）と連続領域 `regions()` を取り出す。`regions()` は `office_md._xlsx_md`（`human_md.render_xlsx` 経由）とも共有する。

決定的な純関数（走査・辞書・リスト順はソートで固定）。壊れた/欠落したパートは例外を投げず空へ縮退する。
`load_two` の値用ロード（`wb_values`）は `read_only=False` を使う（結合セル・非表示行列・ハイパーリンク・コメントの取得に通常ロードが必要なため）。数式用ロード（`wb_formula`）は `read_only=True`。巨大シートは `regions()`／`formulas()`／`cell_hyperlinks()`／`cell_comments()` の行・列上限と `DEFAULT_CAP_CELLS`（総セル予算）で頭打ちにする。
「表・連続領域・設定欄」の意味分類はしない（孤立セルも小さな `Region` として出す）。意味分類は検索用表現生成層の責務。
設計: docs/design/rag.md「アーム一覧」
"""
from __future__ import annotations

import re
import threading
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .rels import load_relationships, resolve_target

_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
# シートに画像が何枚あるかの存在だけを得るための最小限のネームスペース（図形解析は `evidence_spike.py` の `_xlsx_objects` が担う）。
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_SML = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XDR = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"

# 巨大シートの安全弁。行・列上限は Excel の実上限（行 1,048,576・列 16,384）で、加えて総走査セル数 `DEFAULT_CAP_CELLS`（5,000,000）で頭打ちにする（列数が多いシートほど `effective_cap_rows` が行数側を絞る）。`regions()` は有効上限を呼び出し側から受け取る。`formulas`/`cell_hyperlinks`/`cell_comments` は `DEFAULT_CAP_ROWS`/`DEFAULT_CAP_COLS` を呼び出し時にモジュール属性として読む（`monkeypatch.setattr(excel, ...)` で差し替えられる）。
DEFAULT_CAP_ROWS = 1_048_576
DEFAULT_CAP_COLS = 16_384
DEFAULT_CAP_CELLS = 5_000_000


def effective_cap_rows(sheet_max_column: int | None) -> int:
    """列数に応じた行走査上限（総セル予算を超えない範囲で、狭い長大表を欠落させない）。"""
    columns = max(1, min(sheet_max_column or 1, DEFAULT_CAP_COLS))
    return max(1, min(DEFAULT_CAP_ROWS, DEFAULT_CAP_CELLS // columns))


@dataclass
class Region:
    """連続領域（非空セルの4連結成分を、隣接表の癒着解消のため最大矩形へ分割したもの）の外接矩形（`regions()` の戻り値要素）。

    `min_row`/`max_row`/`min_col`/`max_col` は1-based の絶対シート座標。`range` は A1形式（例 `"A1:C10"`）。`truncated` は cap（行/列の走査上限）に到達した領域だけ True。
    """
    min_row: int
    max_row: int
    min_col: int
    max_col: int
    range: str
    truncated: bool = False
    # この領域が実際に所有するセル座標（外接矩形どうしが重なっても重複出力しないための正本）。値を持たない占有セル（背景色付き・結合セルの継続セル）も含みうる（`value_cell_count` は値を持つセルだけを数える）。
    cells: frozenset = frozenset()
    # 表候補スコアの元になった値セル数・密度（`_region_score`）。抑制はせず判断材料として保持する（閾値判断は消費側の責務）。
    value_cell_count: int = 0
    density: float = 0.0
    score: float = 0.0
    # True の場合、この外接矩形はヒストグラム法の抽出結果ではなく、面積上限（`_MAX_RECT_DECOMPOSE_CELLS`）またはシート全体の予算（`_MAX_RECT_SPLITS_PER_SHEET`/`_MAX_REGIONS_PER_SHEET`）に達して単純な外接矩形へ縮退したもの。bbox の内部に非占有セルを含みうる（所有座標の正本は `cells`）。
    split_budget_exhausted: bool = False


# 表候補スコア算出時、値セル数がこの件数に達するまでは密度を按分して割り引く（孤立した1〜数セルが高評価にならないようにする緩やかな補正）。
_SCORE_MIN_CELLS = 4

# ヒストグラム法による最大矩形の反復抽出（`_split_component`）の計算量安全弁。連結成分の外接矩形面積がこれを超える場合は分割せず単一の外接矩形のまま返す。
_MAX_RECT_DECOMPOSE_CELLS = 20_000
# 1連結成分あたりの最大分割反復回数。到達後の残りのセルは4連結成分ごとの外接矩形へまとめて出力する（セルは失われない）。
_MAX_RECT_SPLITS = 32

# シート全体での安全弁（`_MAX_RECT_DECOMPOSE_CELLS`/`_MAX_RECT_SPLITS` は連結成分1つあたりの上限）。`regions()` は連結成分を処理するたびにこの2つの予算を消費し、使い切った以降の成分は分割せず単一の外接矩形にする（セルは失われない）。
_MAX_RECT_SPLITS_PER_SHEET = 64
_MAX_REGIONS_PER_SHEET = 256


def _region_score(value_cell_count: int, area: int) -> tuple[float, float]:
    """`(density, score)` を返す（`Region` と `expand_regions_for_merges` の再計算で共有する算出ロジック）。"""
    density = value_cell_count / area if area else 0.0
    taper = min(1.0, value_cell_count / _SCORE_MIN_CELLS) if _SCORE_MIN_CELLS else 1.0
    return density, density * taper


def _connected_subcomponents(cells: set[tuple[int, int]]) -> list[set[tuple[int, int]]]:
    """`cells`（1-based 座標集合）を4連結成分（上下左右のみ）に分割する（`(min_row, min_col)` が最小の座標から見つかる順・決定的）。"""
    visited: set[tuple[int, int]] = set()
    out: list[set[tuple[int, int]]] = []
    for start in sorted(cells):
        if start in visited:
            continue
        stack = [start]
        visited.add(start)
        component: set[tuple[int, int]] = {start}
        while stack:
            cr, cc = stack.pop()
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (cr + dr, cc + dc)
                if nb in cells and nb not in visited:
                    visited.add(nb)
                    component.add(nb)
                    stack.append(nb)
        out.append(component)
    return out


def _extract_max_rectangle(occ: set[tuple[int, int]], min_row: int, max_row: int,
                           min_col: int, max_col: int) -> tuple[int, int, int, int]:
    """`occ` のうち `[min_row,max_row]×[min_col,max_col]` 範囲内で、全セルが `occ` に含まれる最大面積の矩形を1つ `(top, left, bottom, right)` で返す（ヒストグラム法・モノトニックスタック）。

    同面積の解が複数あれば `(top, left)` が最小の物を選ぶ（決定的）。`occ` はこの範囲内に必ず1セル以上を含む前提。
    """
    n_cols = max_col - min_col + 1
    heights = [0] * n_cols
    best_area = 0
    best: tuple[int, int, int, int] | None = None
    for r in range(min_row, max_row + 1):
        for ci in range(n_cols):
            heights[ci] = heights[ci] + 1 if (r, min_col + ci) in occ else 0
        stack: list[tuple[int, int]] = []  # (開始列index, 高さ)
        for ci in range(n_cols + 1):
            h = heights[ci] if ci < n_cols else 0
            start = ci
            while stack and stack[-1][1] >= h:
                s, sh = stack.pop()
                area = sh * (ci - s)
                candidate = (r - sh + 1, min_col + s, r, min_col + ci - 1)
                if area > 0 and (area > best_area
                                 or (area == best_area and candidate[:2] < best[:2])):
                    best_area, best = area, candidate
                start = s
            stack.append((start, h))
    assert best is not None
    return best


def _split_component(component: set[tuple[int, int]], min_row: int, max_row: int,
                     min_col: int, max_col: int, *,
                     max_splits: int, max_regions: int
                     ) -> tuple[list[tuple[frozenset, int, int, int, int, bool]], int]:
    """1つの4連結成分をヒストグラム法の最大矩形反復抽出で分割し、`([(所有セル集合, min_row, min_col, max_row, max_col, split_budget_exhausted), ...], 実際に使った反復回数)` を返す。呼び出し側で `(min_row, min_col)` 順に並べ直すこと。

    `max_splits` は反復回数上限、`max_regions` はこの呼び出しが出力してよい領域数の上限（どちらも `regions()` がシート全体の予算の残量から渡す）。
    ① `max_splits` に従って自然な分割（反復抽出し、打ち切った残りは4連結成分ごとの断片）を最後まで計算する。② その件数が `max_regions` に収まれば採用する。③ 収まらなければ分割せず、成分全体を1つの外接矩形に畳む（予算を超えない）。外接矩形面積が `_MAX_RECT_DECOMPOSE_CELLS` を超える成分、予算が0の場合も単一の外接矩形のまま返す。
    どの経路でも `cells` の和集合は元の成分全体と一致する。`split_budget_exhausted` は上限到達のフォールバックで生成された領域かを示す。
    """
    area = (max_row - min_row + 1) * (max_col - min_col + 1)
    if area > _MAX_RECT_DECOMPOSE_CELLS or max_splits <= 0 or max_regions <= 0:
        return [(frozenset(component), min_row, min_col, max_row, max_col, True)], 0

    remaining = set(component)
    natural: list[tuple[frozenset, int, int, int, int, bool]] = []
    splits = 0
    while remaining and splits < max_splits:
        r_min = min(r for r, _ in remaining)
        r_max = max(r for r, _ in remaining)
        c_min = min(c for _, c in remaining)
        c_max = max(c for _, c in remaining)
        top, left, bottom, right = _extract_max_rectangle(remaining, r_min, r_max, c_min, c_max)
        rect_cells = frozenset((r, c) for r in range(top, bottom + 1) for c in range(left, right + 1))
        natural.append((rect_cells, top, left, bottom, right, False))
        remaining -= rect_cells
        splits += 1
    if remaining:  # 反復回数上限で打ち切った残りを断片ごとに
        for sub in _connected_subcomponents(remaining):
            sr = [r for r, _ in sub]
            sc = [c for _, c in sub]
            natural.append((frozenset(sub), min(sr), min(sc), max(sr), max(sc), True))

    if len(natural) <= max_regions:
        return natural, splits
    # 自然な分割結果が予算に収まらない: 成分全体を1つの外接矩形へ畳む（保証された1件のみ使用）。反復に使った splits はシート全体の予算から差し引くため、そのまま返す。
    return [(frozenset(component), min_row, min_col, max_row, max_col, True)], splits


_FAST_MERGE = threading.local()


def _format_anchor_only(mcr) -> None:
    """`MergedCellRange.format` のうち、左上のセル（anchor）への処理だけを元と同じ順で行う（縁のほかのセルへの罫線・保護の書き写しは省く）。"""
    import copy

    from openpyxl.styles.borders import Border
    start = mcr.start_cell
    on_edge = {"top": True, "left": True, "right": mcr.max_col == mcr.min_col, "bottom": mcr.max_row == mcr.min_row}
    for name in ("top", "left", "right", "bottom"):
        side = getattr(start.border, name)
        if side and side.style is None:
            continue
        if on_edge[name]:
            start.border += Border(**{name: side})
    if start.protection is not None:
        start.protection = copy.copy(start.protection)


def _install_fast_merge_format() -> None:
    """`MergedCellRange.format` を、このスレッドが `load_workbook_fast` の中にいるときだけ `_format_anchor_only` へ切り替える形に包む（1 回だけ）。"""
    from openpyxl.worksheet.merge import MergedCellRange
    orig = MergedCellRange.format
    if getattr(orig, "_sherpa_fast_merge", False):
        return

    def format(self):
        if getattr(_FAST_MERGE, "on", False):
            return _format_anchor_only(self)
        return orig(self)
    format._sherpa_fast_merge = True
    MergedCellRange.format = format


def load_workbook_fast(p, **kw):
    """`openpyxl.load_workbook` と同じ。読み込みの間だけ、結合範囲の縁の（左上以外の）セルへ罫線・保護を写す処理を省く。
    抽出は左上以外の結合セルの書式を読まない（値・数式・書式・結合の範囲・左上のセルの style_id は変わらない）。結合の多い表で読み込みの時間の大半を占める。
    守ること: 省くのはこのスレッドの読み込みの間だけ（ほかのスレッド・書き出し用のブックは元の処理のまま）。
    """
    import openpyxl
    _install_fast_merge_format()
    prev = getattr(_FAST_MERGE, "on", False)
    _FAST_MERGE.on = True
    try:
        return openpyxl.load_workbook(p, **kw)
    finally:
        _FAST_MERGE.on = prev


def load_two(p):
    """openpyxl で `p` を2回ロードする（`(wb_values, wb_formula)`）。

    `wb_values`＝`data_only=True`（キャッシュ済み計算値）・`read_only=False`（結合セル/非表示行列/ハイパーリンク/コメントの取得に通常ロードが必要）。`wb_formula`＝`data_only=False`（数式文字列）・`read_only=True`（ストリーミング。`formulas()` が `iter_rows()` 等しか使わないため）。呼び出し側は使用後に両方を `close()` すること。
    """
    import openpyxl
    wb_values = load_workbook_fast(p, data_only=True, read_only=False)
    wb_formula = openpyxl.load_workbook(p, data_only=False, read_only=True)
    return wb_values, wb_formula


def sheet_states(wb) -> list[dict]:
    """ワークブック内の全シートをブック内順で `{"name": <タイトル>, "state": "visible"|"hidden"|"veryHidden"}` のリストにする。"""
    return [{"name": ws.title, "state": ws.sheet_state} for ws in wb.worksheets]


def _range_str(min_r: int, min_c: int, max_r: int, max_c: int) -> str:
    from openpyxl.utils import get_column_letter
    return f"{get_column_letter(min_c)}{min_r}:{get_column_letter(max_c)}{max_r}"


def regions(ws_values: list[list], cap_rows: int, cap_cols: int, *,
           merged: dict[tuple[int, int], dict] | None = None,
           filled: set[tuple[int, int]] | None = None) -> list["Region"]:
    """非空セルの4連結成分（上下左右のみ）を、隣接する複数表の癒着を解消するため最大矩形へ分割し、`Region` のリストで返す。

    `ws_values` は呼び出し側が cap+1 まで有界化した値グリッド（行のリストのリスト・`None`＝空セル）。`_build_xlsx_ir` が `ws.max_row`/`ws.max_column` と `cap_rows+1`/`cap_cols+1` の小さい方まで読んで渡す（本関数は grid の総量を制限しない）。
    `merged`（`merged_map()` の戻り値）・`filled`（背景色付きセルの1-based座標集合・`filled_cells()`）は省略可。値を持たなくても占有として扱う:
    - 背景色: `filled` の座標は占有マスにする。
    - 結合セル: 結合範囲内のいずれかのセルが占有済みなら範囲の全セルを占有にする。範囲内が完全に空かつ無地なら占有にしない。

    ① `ws_values` を `cap_rows`×`cap_cols` に切り詰めて占有マスを求める。② 行優先で未訪問の占有マスから BFS して4連結成分を確定する。③ 各成分を `_split_component` で分割する（1成分から複数の `Region` が生まれうる）。分割コストはシート全体で `_MAX_RECT_SPLITS_PER_SHEET`（反復回数）・`_MAX_REGIONS_PER_SHEET`（出力数）の予算を共有する。

    `Region` 数の契約: `len(regions(...)) <= max(連結成分数, _MAX_REGIONS_PER_SHEET)`。各成分は最低1件の `Region` を出す（silent-drop ゼロ）ので連結成分数を下回らない。連結成分数が予算未満なら合計は予算を超えない。予算を使い切った成分は単一の外接矩形にし `split_budget_exhausted=True` にする。
    `Region.value_cell_count`/`density`/`score` は値セル数・密度・表候補スコア（`_region_score`）。スコアによる抑制はしない。
    `truncated`: `ws_values` の行数が `cap_rows` を超える、または列数が `cap_cols` を超える行がある場合に、cap 境界に接する領域だけ True。領域どうしの外接矩形が重なる稀なケースでも重複排除はしない。
    戻り値は `(min_row, min_col)` 昇順。同じ入力に対し常に同じ順序・分割結果を返す。
    """
    total_rows = len(ws_values)
    row_trunc = total_rows > cap_rows
    rows = ws_values[:cap_rows]
    col_trunc = any(len(r) > cap_cols for r in rows)

    value_occupied: set[tuple[int, int]] = set()
    for r, row in enumerate(rows, start=1):
        for c, v in enumerate(row[:cap_cols], start=1):
            if v is not None and str(v).strip() != "":
                value_occupied.add((r, c))

    occupied: set[tuple[int, int]] = set(value_occupied)
    if filled:
        occupied.update((r, c) for (r, c) in filled if r <= cap_rows and c <= cap_cols)
    if merged:
        spans: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for coord, info in merged.items():
            r, c = coord
            if r > cap_rows or c > cap_cols:
                continue
            spans.setdefault(info["anchor"], []).append(coord)
        for span_cells in spans.values():
            if any(cell in occupied for cell in span_cells):
                occupied.update(span_cells)

    components = list(_connected_subcomponents(occupied))
    # 各連結成分に保証の1件を割り当て、余りの予算（`_MAX_REGIONS_PER_SHEET` と連結成分数の差分）だけを早く処理された成分から順に分け合う。連結成分数が予算を超える場合は合計は連結成分数のまま。
    extra_region_budget = max(0, _MAX_REGIONS_PER_SHEET - len(components))

    out: list[Region] = []
    splits_budget = _MAX_RECT_SPLITS_PER_SHEET
    for component in components:
        comp_min_r = min(r for r, _ in component)
        comp_max_r = max(r for r, _ in component)
        comp_min_c = min(c for _, c in component)
        comp_max_c = max(c for _, c in component)
        effective_max_splits = min(_MAX_RECT_SPLITS, splits_budget) if splits_budget > 0 else 0
        effective_max_regions = 1 + extra_region_budget  # 保証1件 + 残りの共有予算
        sub_regions, used = _split_component(
            component, comp_min_r, comp_max_r, comp_min_c, comp_max_c,
            max_splits=effective_max_splits, max_regions=effective_max_regions)
        splits_budget -= used
        extra_region_budget -= max(0, len(sub_regions) - 1)  # この成分が消費した「追加分」だけ減らす
        for cells, top, left, bottom, right, budget_exhausted in sub_regions:
            touches_cap = (row_trunc and bottom == cap_rows) or (col_trunc and right == cap_cols)
            value_count = len(cells & value_occupied)
            area = (bottom - top + 1) * (right - left + 1)
            density, score = _region_score(value_count, area)
            out.append(Region(min_row=top, max_row=bottom, min_col=left, max_col=right,
                              range=_range_str(top, left, bottom, right), truncated=touches_cap,
                              cells=cells, value_cell_count=value_count, density=density, score=score,
                              split_budget_exhausted=budget_exhausted))
    out.sort(key=lambda rg: (rg.min_row, rg.min_col))
    return out


def sheet_truncated(ws_values: list[list], cap_rows: int, cap_cols: int,
                    sheet_max_row: int | None = None, sheet_max_col: int | None = None) -> bool:
    """走査がシート全域をカバーしていない（cap で打ち切った）可能性があるかを返す。

    保守的に定義する: シートの申告範囲（`max_row`/`max_column`）が cap を超えていれば True（過大申告なら偽陽性を許容）。加えて、読み込んだ番兵グリッド（cap+1 まで）内に非空セルがあれば dimension が過小申告でも True。`regions()` の `truncated` と合わせ、cap の外側で完結する領域が無警告で消えないようにする。
    """
    if sheet_max_row is not None and sheet_max_row > cap_rows:
        return True
    if sheet_max_col is not None and sheet_max_col > cap_cols:
        return True
    for r, row in enumerate(ws_values, start=1):
        for c, v in enumerate(row, start=1):
            if (r > cap_rows or c > cap_cols) and v is not None and str(v).strip() != "":
                return True
    return False


def expand_regions_for_merges(region_list: list["Region"], merged: dict[tuple[int, int], dict],
                              cap_rows: int, cap_cols: int) -> list["Region"]:
    """各領域の外接矩形を、所有セル中の結合 anchor の span まで広げた Region リストを返す。

    anchor の継続セルは非占有で bbox に入らず、`source_map.range` が実セル範囲より狭くなるのを防ぐ。拡張は cap でクランプし、クランプが起きた領域は `truncated=True` にする。`cells` は変えない。`density`/`score` は `value_cell_count` と新しい面積から再計算する。
    """
    out: list[Region] = []
    for rg in region_list:
        max_r, max_c, clamped = rg.max_row, rg.max_col, False
        for coord in rg.cells:
            info = merged.get(coord)
            if info is None or info["anchor"] != coord:
                continue
            span_r = coord[0] + info["row_span"] - 1
            span_c = coord[1] + info["column_span"] - 1
            if span_r > cap_rows:
                span_r, clamped = cap_rows, True
            if span_c > cap_cols:
                span_c, clamped = cap_cols, True
            max_r, max_c = max(max_r, span_r), max(max_c, span_c)
        if (max_r, max_c) == (rg.max_row, rg.max_col):
            out.append(rg)
        else:
            area = (max_r - rg.min_row + 1) * (max_c - rg.min_col + 1)
            density, score = _region_score(rg.value_cell_count, area)
            out.append(Region(min_row=rg.min_row, max_row=max_r, min_col=rg.min_col, max_col=max_c,
                              range=_range_str(rg.min_row, rg.min_col, max_r, max_c),
                              truncated=rg.truncated or clamped, cells=rg.cells,
                              value_cell_count=rg.value_cell_count, density=density, score=score,
                              split_budget_exhausted=rg.split_budget_exhausted))
    return out


def _clip_merge_enumeration_bounds(min_row: int, min_col: int, max_row: int, max_col: int,
                                   cap_rows: int, cap_cols: int) -> tuple[int, int]:
    """結合範囲 `[min_row,max_row]×[min_col,max_col]` を座標展開する前に、`cap_rows`×`cap_cols`、かつ総面積 `DEFAULT_CAP_CELLS` 以内へクリップした `(max_row, max_col)` を返す（`min_row`/`min_col` は変えない）。

    結合範囲は宣言上 Excel の絶対上限（A1:XFD1048576）まで指定できるため、展開前に行わないとメモリ/時間が破綻する。面積も追加でクリップし、行方向を優先して削る。`row_span`/`column_span` はクリップしない（`expand_regions_for_merges()` が cap と突き合わせて別途クランプする）。
    """
    max_row = min(max_row, cap_rows)
    max_col = min(max_col, cap_cols)
    area = (max_row - min_row + 1) * (max_col - min_col + 1)
    if area > DEFAULT_CAP_CELLS:
        width = max_col - min_col + 1
        max_row = min_row + max(1, DEFAULT_CAP_CELLS // width) - 1
    return max_row, max_col


def merged_map(ws, cap_rows: int, cap_cols: int) -> dict[tuple[int, int], dict]:
    """結合セル範囲を座標展開した辞書 `{(row, col): {"anchor": (ar, ac), "row_span", "column_span"}}`。

    範囲内の全座標（anchor 自身も含む）をキーにする。キーが無ければ通常セル、`info["anchor"] == (row, col)` なら anchor、それ以外は非anchor の継続セル（`cells` に出さない）。
    展開は `cap_rows`/`cap_cols` でクリップしてから行う（`_clip_merge_enumeration_bounds`）。値の `row_span`/`column_span` は宣言どおりのまま返す。`ws.merged_cells.ranges` を `(min_row, min_col)` 順にソートして展開する（決定的）。
    既知の限界: 結合範囲の非anchorセルだけに値がある異常な OOXML は、`load_two()` の通常ロードの時点で openpyxl が値を破棄する。クラッシュはしない。
    """
    out: dict[tuple[int, int], dict] = {}
    for mc in sorted(ws.merged_cells.ranges, key=lambda m: (m.min_row, m.min_col)):
        anchor = (mc.min_row, mc.min_col)
        row_span = mc.max_row - mc.min_row + 1
        col_span = mc.max_col - mc.min_col + 1
        clip_max_row, clip_max_col = _clip_merge_enumeration_bounds(
            mc.min_row, mc.min_col, mc.max_row, mc.max_col, cap_rows, cap_cols)
        for r in range(mc.min_row, clip_max_row + 1):
            for c in range(mc.min_col, clip_max_col + 1):
                out[(r, c)] = {"anchor": anchor, "row_span": row_span, "column_span": col_span}
    return out


_DRAWINGML_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


_HEX6_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
_HEX8_RE = re.compile(r"^[0-9A-Fa-f]{8}$")


def _is_white_hex(value) -> bool:
    """`value`（RGB6桁または ARGB8桁の16進文字列）が厳密に白（`FFFFFF`）と確認できるか。

    文字列全体の形式を検証してから比較する（`endswith("FFFFFF")` だけでは `"garbageFFFFFF"` を白と誤受理する）。6桁はそのまま、8桁はアルファを除いた末尾6桁を比較する。どちらの形式でもない場合は False（占有側に倒す）。
    """
    if not isinstance(value, str):
        return False
    if _HEX6_RE.fullmatch(value):
        return value.upper() == "FFFFFF"
    if _HEX8_RE.fullmatch(value):
        return value[2:].upper() == "FFFFFF"
    return False


def _resolve_indexed_rgb(wb, indexed: int) -> str | None:
    """`wb`（openpyxl `Workbook`）のインデックスパレット（`wb._colors`）から `indexed` が指す RGB 文字列を取得する。`wb` が無い・パレットが無い・インデックスが範囲外なら `None`（呼び出し側は占有として扱う）。"""
    colors = getattr(wb, "_colors", None) if wb is not None else None
    if not colors or not (0 <= indexed < len(colors)):
        return None
    return colors[indexed]


def _resolve_theme_lt1_rgb(wb) -> str | None:
    """`wb.loaded_theme`（テーマ part の生 XML バイト列）から背景1（`lt1`）の RGB を取り出す。`xml.etree` でパースする。直接色（`a:srgbClr`）・システム色（`a:sysClr` の `lastClr`）に対応する。`wb` が無い・テーマ未ロード・パース失敗・`lt1` が見つからない場合は `None`（呼び出し側は占有として扱う）。"""
    theme_bytes = getattr(wb, "loaded_theme", None) if wb is not None else None
    if not theme_bytes:
        return None
    try:
        root = ET.fromstring(theme_bytes)
    except ET.ParseError:
        return None
    lt1 = root.find(f".//{_DRAWINGML_NS}lt1")
    if lt1 is None:
        return None
    srgb = lt1.find(f"{_DRAWINGML_NS}srgbClr")
    if srgb is not None:
        return srgb.get("val")
    sys_clr = lt1.find(f"{_DRAWINGML_NS}sysClr")
    if sys_clr is not None:
        return sys_clr.get("lastClr")
    return None


class ColorResolver:
    """1ワークブック分の色解決コンテキスト。複数シートを走査する呼び出し側はワークブックあたり1つだけ構築し、シートごとの `filled_cells()` に明示的に渡して使い回すこと（省略すると呼び出しごとに新規構築される）。

    `wb.loaded_theme` のパースは初回だけ実行してキャッシュする。インデックスパレットはコストが小さいためキャッシュしない。
    """
    def __init__(self, wb):
        self._wb = wb
        self._lt1_rgb: str | None = None
        self._lt1_resolved = False

    def indexed_rgb(self, indexed: int) -> str | None:
        return _resolve_indexed_rgb(self._wb, indexed)

    def theme_lt1_rgb(self) -> str | None:
        if not self._lt1_resolved:
            self._lt1_rgb = _resolve_theme_lt1_rgb(self._wb)
            self._lt1_resolved = True
        return self._lt1_rgb


def _is_white_color(color, resolver: "ColorResolver") -> bool:
    """openpyxl の `Color`（`None` も可）が「白／自動＝背景色として占有扱いしない色」とみなせるか。`resolver`（`ColorResolver`）でインデックスパレット・テーマの実際の値を引く。

    判定順序:
    1. 自動色（`auto`）→ 無条件に白（tint より先に判定する）。
    2. tint が負（暗色化）→ 白ではない。
    3. RGB が白（`_is_white_hex`）。
    4. インデックスパレット（`indexed`）が `resolver` のパレットで白に解決する。
    5. テーマの背景1（`theme == 0`）が `resolver` のテーマ定義で白に解決する。
    `None`・解決不能・形式不正・どれにも一致しない場合は False（白と断定せず占有側に倒す＝表の分裂を避ける）。
    """
    if color is None:
        return False
    # openpyxl の `Color.auto`/`.theme`/`.indexed` は未設定だと `None` ではなく Typed 記述子オブジェクト自身を返す。truthy 判定や `isinstance` なしの同値比較は誤判定しうるので、`is True`/`isinstance(..., int)`/`== 値` で判定する（`tint` は未設定でも `float` の `0.0`）。
    if getattr(color, "auto", None) is True:
        return True
    tint = getattr(color, "tint", 0.0)
    if isinstance(tint, (int, float)) and tint < 0:
        return False
    indexed = getattr(color, "indexed", None)
    if isinstance(indexed, int):
        return _is_white_hex(resolver.indexed_rgb(indexed))
    if getattr(color, "theme", None) == 0:
        return _is_white_hex(resolver.theme_lt1_rgb())
    return _is_white_hex(getattr(color, "rgb", None))


def _fill_is_colored(fill, resolver: "ColorResolver") -> bool:
    """`fill`（`PatternFill`/`GradientFill`/未知の型）が「値が無くても占有とみなすべき背景色」を持つか。`resolver` は `_is_white_color` の色解決に使う。

    - 塗りなし（`patternType` が `None`/`"none"`）は対象外。単色塗り（`"solid"`）は前景色（`fgColor`）が白なら対象外。単色以外のパターン塗りは一律で占有対象とする。
    - `GradientFill` は `patternType` を持たないため `getattr` で判定する。各ストップの色がすべて白でない限り占有対象とする。
    - 条件付き書式は対象外（セル自身のスタイル定義だけを見る）。
    """
    if fill is None:
        return False
    pattern_type = getattr(fill, "patternType", None)
    if pattern_type not in (None, "none"):
        if pattern_type == "solid" and _is_white_color(getattr(fill, "fgColor", None), resolver):
            return False
        return True
    stops = getattr(fill, "stop", None)  # GradientFill のみが持つ（PatternFill には無い）
    if stops:
        return any(not _is_white_color(getattr(stop, "color", None), resolver) for stop in stops)
    return False


def filled_cells(ws, cap_rows: int, cap_cols: int, *, resolver: "ColorResolver | None" = None
                 ) -> set[tuple[int, int]]:
    """背景色（単色/縞模様パターン塗り・グラデーション塗り）が設定されているセルの1-based座標集合を返す（`regions()` の `filled` 引数用・`cap_rows`×`cap_cols` 以内・色の判定は `_fill_is_colored`）。白判定はセルが属するワークブック固有のパレット・テーマ定義を `resolver`（`ColorResolver`）で行う。

    複数シートのワークブックでは、呼び出し側が `ColorResolver(wb)` を1つだけ構築して全シートの呼び出しに渡すこと（省略するとシートの数だけテーマのパースが繰り返される）。
    結合範囲の非anchorセル（`MergedCell`）は自身の `fill` を持たないため、anchor セルの `fill` を見る。`anchor_of` の座標展開は `merged_map()` と同じ `_clip_merge_enumeration_bounds` でクリップしてから行う。
    """
    from openpyxl.cell.cell import MergedCell

    if resolver is None:
        resolver = ColorResolver(ws.parent)
    anchor_of: dict[tuple[int, int], tuple[int, int]] = {}
    for mc in ws.merged_cells.ranges:
        anchor = (mc.min_row, mc.min_col)
        clip_max_row, clip_max_col = _clip_merge_enumeration_bounds(
            mc.min_row, mc.min_col, mc.max_row, mc.max_col, cap_rows, cap_cols)
        for r in range(mc.min_row, clip_max_row + 1):
            for c in range(mc.min_col, clip_max_col + 1):
                anchor_of[(r, c)] = anchor

    max_row = min(ws.max_row or 1, cap_rows)
    max_col = min(ws.max_column or 1, cap_cols)
    out: set[tuple[int, int]] = set()
    for row in ws.iter_rows(min_row=1, max_row=max_row, min_col=1, max_col=max_col):
        for cell in row:
            fill = cell.fill
            if isinstance(cell, MergedCell):
                anchor = anchor_of.get((cell.row, cell.column))
                if anchor is not None:
                    fill = ws.cell(row=anchor[0], column=anchor[1]).fill
            if _fill_is_colored(fill, resolver):
                out.add((cell.row, cell.column))
    return out


def hidden_rows(ws) -> list[int]:
    """非表示行の1-based行番号（昇順）。`ws.row_dimensions` は明示設定された行だけを持つため、`hidden` が真の行だけを抽出する。"""
    return sorted(r for r, dim in ws.row_dimensions.items() if dim.hidden)


def hidden_cols(ws) -> list[str]:
    """非表示列の列文字（例 `"D"`・列インデックス昇順）。グループ化された非表示（`B:D` のように範囲を1エントリで表す）は範囲を全列へ展開する。"""
    from openpyxl.utils import column_index_from_string, get_column_letter
    idxs: set[int] = set()
    for key, dim in ws.column_dimensions.items():
        if not dim.hidden:
            continue
        lo = dim.min or column_index_from_string(key)
        hi = dim.max or lo
        idxs.update(range(lo, hi + 1))
    return [get_column_letter(i) for i in sorted(idxs)]


def formulas(ws_formula, ws_values) -> list[dict]:
    """`ws_formula`（`data_only=False`）内の数式セル（`=` で始まる値）を走査し、`ws_values`（`data_only=True`）の同座標と突き合わせて `{"cell", "row", "column", "formula", "has_cached"}` のリストを `(row, column)` 昇順で返す。

    `has_cached`＝`ws_values` 側の同座標の値が `None` でないか（未計算式／キャッシュ破棄済みは `None`）。走査範囲は行・列上限と `DEFAULT_CAP_CELLS` の総セル予算（呼び出し時にモジュール属性を読む）で `min(ws.max_row, effective_cap+1)` に有界化する。
    """
    max_row = min(ws_formula.max_row or 1, effective_cap_rows(ws_formula.max_column) + 1)
    max_col = min(ws_formula.max_column or 1, DEFAULT_CAP_COLS + 1)
    out: list[dict] = []
    for row in ws_formula.iter_rows(min_row=1, max_row=max_row, min_col=1, max_col=max_col):
        for cell in row:
            v = cell.value
            if isinstance(v, str) and v.startswith("="):
                cached = ws_values.cell(row=cell.row, column=cell.column).value
                out.append({"cell": cell.coordinate, "row": cell.row, "column": cell.column,
                           "formula": v, "has_cached": cached is not None})
    out.sort(key=lambda e: (e["row"], e["column"]))
    return out


def defined_names(wb) -> list[dict]:
    """名前付き範囲（ブック全体＝global／シート限定＝local）を `{"name", "value": <参照先文字列>, "scope": "workbook" | <シート名>}` のリストで返す。

    順序: ブック全体スコープ（名前昇順）、続けてシート限定スコープをブック内シート順に、各シート内は名前昇順。
    """
    out: list[dict] = []
    for name in sorted(wb.defined_names):
        dn = wb.defined_names[name]
        out.append({"name": name, "value": dn.value or "", "scope": "workbook"})
    for ws in wb.worksheets:
        for name in sorted(ws.defined_names):
            dn = ws.defined_names[name]
            out.append({"name": name, "value": dn.value or "", "scope": ws.title})
    return out


def cell_hyperlinks(ws) -> list[dict]:
    """`ws` 内のセル単位ハイパーリンクを `{"cell", "row", "column", "target", "text"}` のリストで `(row, column)` 昇順で返す。

    `ws` は `data_only=True` でロードした値ワークシートを渡すこと（`text` にキャッシュ済み計算値を使うため）。`target` は外部 URL（`Hyperlink.target`）優先、無ければ `"#" + location`。どちらも無ければ省略する。走査範囲は `formulas()` と同じ cap 契約。
    """
    max_row = min(ws.max_row or 1, effective_cap_rows(ws.max_column) + 1)
    max_col = min(ws.max_column or 1, DEFAULT_CAP_COLS + 1)
    out: list[dict] = []
    for row in ws.iter_rows(min_row=1, max_row=max_row, min_col=1, max_col=max_col):
        for cell in row:
            h = cell.hyperlink
            if h is None:
                continue
            target = h.target or (("#" + h.location) if h.location else None)
            if not target:
                continue
            v = cell.value
            out.append({"cell": cell.coordinate, "row": cell.row, "column": cell.column,
                       "target": target, "text": "" if v is None else str(v)})
    out.sort(key=lambda e: (e["row"], e["column"]))
    return out


def cell_comments(ws) -> list[dict]:
    """`ws` 内のセルコメントを `{"cell", "row", "column", "text", "author"}` のリストで `(row, column)` 昇順で返す。本文が空のコメントは出さない。走査範囲は `formulas()` と同じ cap 契約。"""
    max_row = min(ws.max_row or 1, effective_cap_rows(ws.max_column) + 1)
    max_col = min(ws.max_column or 1, DEFAULT_CAP_COLS + 1)
    out: list[dict] = []
    for row in ws.iter_rows(min_row=1, max_row=max_row, min_col=1, max_col=max_col):
        for cell in row:
            c = cell.comment
            if c is None:
                continue
            text = (c.text or "").strip()
            if not text:
                continue
            out.append({"cell": cell.coordinate, "row": cell.row, "column": cell.column,
                       "text": text, "author": c.author or ""})
    out.sort(key=lambda e: (e["row"], e["column"]))
    return out


def strike_cells(ws) -> list[dict]:
    """取り消し線（`cell.font.strike`）が設定されたセルを `{"cell", "row", "column", "text"}` のリストで `(row, column)` 昇順で返す。

    値が無いセル（`cell.value is None`）は出さない。結合範囲の非anchorセルは値を持たないので自然に除外される。走査範囲は `formulas()` と同じ cap 契約。
    """
    max_row = min(ws.max_row or 1, effective_cap_rows(ws.max_column) + 1)
    max_col = min(ws.max_column or 1, DEFAULT_CAP_COLS + 1)
    out: list[dict] = []
    for row in ws.iter_rows(min_row=1, max_row=max_row, min_col=1, max_col=max_col):
        for cell in row:
            font = cell.font
            if font is None or not font.strike:
                continue
            v = cell.value
            if v is None:
                continue
            out.append({"cell": cell.coordinate, "row": cell.row, "column": cell.column, "text": str(v)})
    out.sort(key=lambda e: (e["row"], e["column"]))
    return out


_EXTERNAL_LINK_RELS_RE = re.compile(r"xl/externalLinks/_rels/[^/]+\.rels")


def external_link_targets(zf: zipfile.ZipFile) -> list[str]:
    """zip 内 `xl/externalLinks/_rels/*.rels` の `Target` 属性値一覧（ソート済み・重複も保持）。外部ブック参照の存在を示す来歴情報。パート欠落は空リスト。壊れた rels は無視して続行する。"""
    out: list[str] = []
    names = sorted(n for n in zf.namelist() if _EXTERNAL_LINK_RELS_RE.fullmatch(n))
    for n in names:
        try:
            root = ET.fromstring(zf.read(n))
        except (KeyError, ET.ParseError):
            continue
        for r in root.iter(f"{_RELS}Relationship"):
            target = r.get("Target")
            if target:
                out.append(target)
    return sorted(out)


def _load_rels(zf: zipfile.ZipFile, part: str) -> dict[str, str]:
    """`part` に対応する `_rels/*.rels` から `{Id: 解決済み絶対パートパス}` を返す（`TargetMode="External"` は除外）。パート・rels の欠落/破損は空 dict。"""
    return {
        rel.id: resolve_target(part, rel.target)
        for rel in load_relationships(zf.read, part)
        if rel.id and rel.target and rel.mode != "External"
    }


def picture_counts_by_sheet(zf: zipfile.ZipFile) -> dict[str, int]:
    """ブック内の各シート名 → そのシートの drawing part に含まれる画像（`xdr:pic`）の枚数。

    画像が0枚のシートはキーを持たない。壊れた/欠落したパートはそのシート分だけ黙って除外する。図形（`xdr:sp`）・チャート・SmartArt は数えない。
    """
    try:
        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
    except (KeyError, ET.ParseError):
        return {}
    wb_rels = _load_rels(zf, "xl/workbook.xml")
    out: dict[str, int] = {}
    for sheet in wb_root.findall(f"{_SML}sheets/{_SML}sheet"):
        name, rid = sheet.get("name"), sheet.get(f"{_R}id")
        sheet_part = wb_rels.get(rid) if rid else None
        if not name or not sheet_part:
            continue
        try:
            sheet_root = ET.fromstring(zf.read(sheet_part))
        except (KeyError, ET.ParseError):
            continue
        drawing_ref = sheet_root.find(f"{_SML}drawing")
        if drawing_ref is None:
            continue
        drawing_rid = drawing_ref.get(f"{_R}id")
        drawing_part = _load_rels(zf, sheet_part).get(drawing_rid) if drawing_rid else None
        if not drawing_part:
            continue
        try:
            drawing_root = ET.fromstring(zf.read(drawing_part))
        except (KeyError, ET.ParseError):
            continue
        # `xdr:pic` は anchor（`oneCellAnchor`/`twoCellAnchor`）の子孫で、グループ化図形の中ではさらに深いため、`.iter()` で木全体から数える。
        count = sum(1 for _ in drawing_root.iter(f"{_XDR}pic"))
        if count:
            out[name] = count
    return out
