"""PDF の罫線・表の格子・pdfium の文字取得。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .pdf_xobject import _matrix_multiply, _pdf_point


_PDF_PAINT_OPERATORS = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}
_PDF_RULE_MERGE_TOLERANCE = 2.0          # 同じ軸でこの距離以内に並ぶ罫線の境界は1本とみなす（pt）
_PDF_RULE_COVER_TOLERANCE = 1.5          # セル境界を罫線が覆うかの端の許容（pt）
_PDF_GROUP_GAP_RATIO = 0.6               # 同じ行の隣り合う矩形を1語群にまとめる間隔の上限（文字高さ比）
_PDF_GROUP_LINE_RATIO = 0.3              # 同じ行とみなす下端のずれの上限（文字高さ比）
_PDF_PACKED_CELL_GROUPS = 3              # 1セルの同じ行にこの数以上の語群が並ぶ格子は、列の罫線が足りない（表として信頼しない）。箱1つ・見出し+本文の2段の囲みは対象外
_PDFIUM_LOCK = threading.Lock()          # pdfiumはスレッド安全でないため、呼び出しをこのロックで直列化する


def _pdf_axis_segments(operations: list[tuple[list[Any], bytes]]) -> list[tuple[float, float, float, float]]:
    """content streamの直交線分をuser spaceへ写す。描画演算子で確定した経路だけを採り、曲線や斜線は表として扱わない。"""
    stack: list[list[float]] = []
    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    current: tuple[float, float] | None = None
    pending: list[tuple[float, float, float, float]] = []
    segments: list[tuple[float, float, float, float]] = []
    for operands, operator in operations:
        if operator == b"q":
            stack.append(list(ctm))
        elif operator == b"Q":
            ctm = stack.pop() if stack else [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        elif operator == b"cm" and len(operands) == 6:
            try:
                ctm = _matrix_multiply(ctm, [float(value) for value in operands])
            except (TypeError, ValueError):
                continue
        elif operator == b"m" and len(operands) >= 2:
            try:
                current = _pdf_point(ctm, float(operands[0]), float(operands[1]))
            except (TypeError, ValueError):
                current = None
        elif operator == b"l" and len(operands) >= 2 and current is not None:
            try:
                target = _pdf_point(ctm, float(operands[0]), float(operands[1]))
            except (TypeError, ValueError):
                continue
            if abs(current[0] - target[0]) <= 0.5 or abs(current[1] - target[1]) <= 0.5:
                pending.append((*current, *target))
            current = target
        elif operator == b"re" and len(operands) >= 4:
            try:
                x, y, width, height = (float(value) for value in operands[:4])
            except (TypeError, ValueError):
                continue
            corners = [_pdf_point(ctm, x, y), _pdf_point(ctm, x + width, y),
                       _pdf_point(ctm, x + width, y + height), _pdf_point(ctm, x, y + height)]
            for left, right in zip(corners, corners[1:] + corners[:1], strict=True):
                if abs(left[0] - right[0]) <= 0.5 or abs(left[1] - right[1]) <= 0.5:
                    pending.append((*left, *right))
        elif operator in _PDF_PAINT_OPERATORS:
            segments.extend(pending)
            pending = []
            current = None
        elif operator == b"n":
            pending = []
            current = None
    return segments


def _pdf_merge_close(values: list[float]) -> list[float]:
    """近接する境界座標を決定的に1本へまとめる（昇順・代表値は平均）。"""
    ordered = sorted({round(value, 3) for value in values})
    clusters: list[list[float]] = []
    for value in ordered:
        if clusters and value - clusters[-1][-1] <= _PDF_RULE_MERGE_TOLERANCE:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [round(sum(cluster) / len(cluster), 3) for cluster in clusters]


def _pdf_table_grids(
    segments: list[tuple[float, float, float, float]],
) -> list[tuple[list[float], list[float], list[tuple[float, float, float]], list[tuple[float, float, float]]]]:
    """交差する縦横線のconnected componentから表gridを得る。独立した上下・左右の表を混ぜない。

    返す各gridは (列境界xs昇順, 行境界ys降順, 縦線[(x, 下, 上)], 横線[(y, 左, 右)])。
    """
    axis = [
        segment for segment in segments
        if (abs(segment[0] - segment[2]) <= 0.5 and abs(segment[1] - segment[3]) >= 5)
        or (abs(segment[1] - segment[3]) <= 0.5 and abs(segment[0] - segment[2]) >= 5)
    ]
    if not axis:
        return []
    adjacency: list[set[int]] = [set() for _ in axis]
    vertical = [i for i, item in enumerate(axis) if abs(item[0] - item[2]) <= 0.5]
    horizontal = [i for i, item in enumerate(axis) if abs(item[1] - item[3]) <= 0.5]
    for vertical_index in vertical:
        vx = axis[vertical_index][0]
        vlow, vhigh = sorted((axis[vertical_index][1], axis[vertical_index][3]))
        for horizontal_index in horizontal:
            hy = axis[horizontal_index][1]
            hlow, hhigh = sorted((axis[horizontal_index][0], axis[horizontal_index][2]))
            if hlow - 0.75 <= vx <= hhigh + 0.75 and vlow - 0.75 <= hy <= vhigh + 0.75:
                adjacency[vertical_index].add(horizontal_index)
                adjacency[horizontal_index].add(vertical_index)
    seen: set[int] = set()
    grids: list[tuple[list[float], list[float], list[tuple[float, float, float]], list[tuple[float, float, float]]]] = []
    vertical_set = set(vertical)
    for start in range(len(axis)):
        if start in seen or not adjacency[start]:
            continue
        component: set[int] = set()
        pending = [start]
        while pending:
            index = pending.pop()
            if index in component:
                continue
            component.add(index)
            pending.extend(adjacency[index] - component)
        seen.update(component)
        xs = _pdf_merge_close([axis[index][0] for index in component if index in vertical_set])
        ys = sorted(_pdf_merge_close([axis[index][1] for index in component if index not in vertical_set]), reverse=True)
        if len(xs) >= 2 and len(ys) >= 2:
            vlines = [(axis[index][0], *sorted((axis[index][1], axis[index][3])))
                      for index in component if index in vertical_set]
            hlines = [(axis[index][1], *sorted((axis[index][0], axis[index][2])))
                      for index in component if index not in vertical_set]
            grids.append((xs, ys, vlines, hlines))
    return sorted(grids, key=lambda grid: (-grid[1][0], grid[0][0]))


def _pdf_rule_covers(lines: list[tuple[float, float, float]], position: float, low: float, high: float) -> bool:
    """position付近の罫線の和が、区間 [low, high] を（許容つきで）覆うか。"""
    intervals = sorted((lo, hi) for at, lo, hi in lines if abs(at - position) <= _PDF_RULE_MERGE_TOLERANCE)
    reach = low
    for lo, hi in intervals:
        if lo > reach + _PDF_RULE_COVER_TOLERANCE:
            break
        reach = max(reach, hi)
    return reach >= high - _PDF_RULE_COVER_TOLERANCE


def _pdf_merged_cells(
    grid: tuple[list[float], list[float], list[tuple[float, float, float]], list[tuple[float, float, float]]],
) -> tuple[dict[tuple[int, int], tuple[int, int]], dict[tuple[int, int], tuple[int, int]]]:
    """罫線が覆っていないセル境界の両側を結合する。長方形になる結合だけ採る。

    返り値は ({各セル: 結合の左上セル}, {左上セル: (row_span, column_span)})。
    """
    xs, ys, vlines, hlines = grid
    rows, columns = len(ys) - 1, len(xs) - 1
    parent = {(r, c): (r, c) for r in range(1, rows + 1) for c in range(1, columns + 1)}

    def find(cell: tuple[int, int]) -> tuple[int, int]:
        while parent[cell] != cell:
            parent[cell] = parent[parent[cell]]
            cell = parent[cell]
        return cell

    def union(a: tuple[int, int], b: tuple[int, int]) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)

    for r in range(1, rows + 1):
        for c in range(1, columns + 1):
            if c < columns and not _pdf_rule_covers(vlines, xs[c], ys[r], ys[r - 1]):
                union((r, c), (r, c + 1))
            if r < rows and not _pdf_rule_covers(hlines, ys[r], xs[c - 1], xs[c]):
                union((r, c), (r + 1, c))
    members: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for cell in parent:
        members.setdefault(find(cell), []).append(cell)
    anchor_of = {cell: cell for cell in parent}
    spans: dict[tuple[int, int], tuple[int, int]] = {cell: (1, 1) for cell in parent}
    for anchor, cells in members.items():
        if len(cells) == 1:
            continue
        row_lo, row_hi = min(r for r, _ in cells), max(r for r, _ in cells)
        col_lo, col_hi = min(c for _, c in cells), max(c for _, c in cells)
        if (row_hi - row_lo + 1) * (col_hi - col_lo + 1) != len(cells):
            continue
        for cell in cells:
            anchor_of[cell] = anchor
        spans[anchor] = (row_hi - row_lo + 1, col_hi - col_lo + 1)
    return anchor_of, spans


def _pdf_display_mapper(page: Any):
    """/Rotate 付きページの user space 座標を、表示される向き（左下原点・y 上向き）へ写す関数。回転なしは None。"""
    try:
        rotate = int(getattr(page, "rotation", 0) or 0) % 360
        media = page.mediabox
        x0, y0, x1, y1 = float(media.left), float(media.bottom), float(media.right), float(media.top)
    except Exception:
        return None
    if rotate == 90:
        return lambda x, y: (y - y0, x1 - x)
    if rotate == 180:
        return lambda x, y: (x1 - x, y1 - y)
    if rotate == 270:
        return lambda x, y: (y1 - y, x - x0)
    return None


def _pdf_map_bbox(mapper, bbox: list[float]) -> list[float]:
    (ax, ay), (bx, by) = mapper(bbox[0], bbox[1]), mapper(bbox[2], bbox[3])
    return [round(min(ax, bx), 3), round(min(ay, by), 3), round(max(ax, bx), 3), round(max(ay, by), 3)]


def _pdf_text_groups(
    page: Any, operations: list[tuple[list[Any], bytes]], page_bbox: list[float], *, positioned: bool = True,
) -> list[dict[str, Any]]:
    """pypdfのdecoded textとlayout座標を安全に突き合わせる。ずれたPDFはpage textへ縮退する。

    `positioned=False`（/Rotate 付きページ）は座標を信用せず、最初からpage textへ縮退する。
    """
    decoded: list[tuple[str, float, float]] = []

    def visitor(text: str, _cm: list[float], tm: list[float], _font: Any, _size: float) -> None:
        value = text.strip()
        if value:
            decoded.append((value, float(tm[4]), float(tm[5])))

    page_text = page.extract_text(visitor_text=visitor) or ""
    try:
        from pypdf._text_extraction._layout_mode._fixed_width_page import text_show_operations
        groups = [item for item in text_show_operations(iter(operations), page._layout_mode_fonts())
                  if str(item.get("text", "")).strip()]
    except Exception:
        groups = []
    if not positioned or len(groups) != len(decoded):
        return ([{"text": page_text.strip(), "bbox": page_bbox, "position_confidence": 0.0}]
                if page_text.strip() else [])
    offsets = [tm_x - float(group["tx"]) for (_text, tm_x, _tm_y), group in zip(decoded, groups, strict=True) if tm_x]
    x_offset = sorted(offsets)[len(offsets) // 2] if offsets else 0.0
    result: list[dict[str, Any]] = []
    for index, ((text, _tm_x, _tm_y), group) in enumerate(zip(decoded, groups, strict=True), start=1):
        left = float(group["tx"]) + x_offset
        right = float(group["displaced_tx"]) + x_offset
        baseline = float(group["ty"])
        height = max(float(group["font_height"]), float(group["font_size"]), 1.0)
        result.append({
            "text": text,
            "bbox": [round(left, 3), round(baseline - height * 0.25, 3), round(right, 3), round(baseline + height, 3)],
            "baseline_y": round(baseline, 3),
            "font_size": float(group["font_size"]),
            "group_index": index,
            "position_confidence": 1.0,
        })
    return result


def _pdfium_open(path: Path) -> Any:
    """pdfiumでPDFを開く。使えない・開けない場合はNone（呼び出し側はpypdf経路へ縮退する）。"""
    try:
        import pypdfium2 as pdfium
        with _PDFIUM_LOCK:
            return pdfium.PdfDocument(str(path))
    except Exception:
        return None


def _pdfium_close(document: Any) -> None:
    if document is None:
        return
    try:
        with _PDFIUM_LOCK:
            document.close()
    except Exception:
        pass


def _pdfium_page_text(
    document: Any, page_index: int, vertical_rules: list[tuple[float, float, float]] = (), mapper=None,
) -> tuple[list[dict[str, Any]], str] | None:
    """pdfiumのtext pageから位置付きtext groupsとページ全文を得る。失敗・文字なしはNone。

    矩形は行ごとに取れる。同じ行で間隔が小さい矩形（同じセル内の語の分割）だけ1グループにまとめ、
    間に縦罫線（`vertical_rules`＝[(x, 下, 上)]）がある矩形はまとめない。
    `mapper` を渡すと（/Rotate 付きページ）矩形・基線を表示される向きへ写してから行にまとめる。
    """
    try:
        import ctypes

        import pypdfium2.raw as pdfium_c
        with _PDFIUM_LOCK:
            page = document[page_index]
            try:
                textpage = page.get_textpage()
                try:
                    rects: list[dict[str, Any]] = []
                    for index in range(textpage.count_rects()):
                        left, bottom, right, top = textpage.get_rect(index)
                        text = textpage.get_text_bounded(left, bottom, right, top)
                        if not text.strip():
                            continue
                        char_index = textpage.get_index(left + 0.5, (bottom + top) / 2, 1.0, 1.0)
                        origin: tuple[float, float] | None = None
                        size = 0.0
                        if char_index is not None and char_index >= 0:
                            origin_x, origin_y = ctypes.c_double(), ctypes.c_double()
                            if pdfium_c.FPDFText_GetCharOrigin(textpage.raw, char_index, origin_x, origin_y):
                                origin = (origin_x.value, origin_y.value)
                            size = pdfium_c.FPDFText_GetFontSize(textpage.raw, char_index)
                        if mapper is not None:
                            left, bottom, right, top = _pdf_map_bbox(mapper, [left, bottom, right, top])
                            if origin is not None:
                                origin = mapper(*origin)
                        height = max(top - bottom, 1.0)
                        baseline = bottom + height * 0.2
                        font_size = height
                        if origin is not None and bottom - 1.0 <= origin[1] <= top + 1.0:
                            baseline = origin[1]
                        if size > 0:
                            font_size = size
                        rects.append({"text": text, "left": left, "bottom": bottom, "right": right, "top": top,
                                      "baseline": baseline, "font_size": font_size})
                    full_text = textpage.get_text_range().replace("\r\n", "\n").replace("\r", "\n")
                finally:
                    textpage.close()
            finally:
                page.close()
    except Exception:
        return None
    lines: list[dict[str, Any]] = []
    for rect in rects:
        last = lines[-1] if lines else None
        if last is not None:
            height = max(last["top"] - last["bottom"], 1.0)
            gap = rect["left"] - last["right"]
            if (abs(rect["bottom"] - last["bottom"]) <= height * _PDF_GROUP_LINE_RATIO
                    and -height * _PDF_GROUP_LINE_RATIO <= gap <= height * _PDF_GROUP_GAP_RATIO
                    and not _pdf_rule_between(vertical_rules, last, rect)):
                last["text"] += rect["text"]
                last["right"] = max(last["right"], rect["right"])
                last["top"] = max(last["top"], rect["top"])
                last["bottom"] = min(last["bottom"], rect["bottom"])
                continue
        lines.append(dict(rect))
    groups = [
        {
            "text": item["text"].strip(),
            "bbox": [round(item["left"], 3), round(item["bottom"], 3), round(item["right"], 3), round(item["top"], 3)],
            "baseline_y": round(item["baseline"], 3),
            "font_size": float(item["font_size"]),
            "position_confidence": 1.0,
        }
        for item in lines if item["text"].strip()
    ]
    for index, group in enumerate(groups, start=1):
        group["group_index"] = index
    return (groups, full_text) if groups else None


def _pdf_vertical_rules(segments: list[tuple[float, float, float, float]]) -> list[tuple[float, float, float]]:
    """線分のうち縦の罫線を [(x, 下, 上)] で返す（格子検出と同じ長さの下限）。"""
    return [(segment[0], *sorted((segment[1], segment[3]))) for segment in segments
            if abs(segment[0] - segment[2]) <= 0.5 and abs(segment[1] - segment[3]) >= 5]


def _pdf_rule_between(vertical_rules, left: dict[str, Any], right: dict[str, Any]) -> bool:
    """2つの矩形の間に、その行の高さに掛かる縦罫線があるか。"""
    bottom, top = min(left["bottom"], right["bottom"]), max(left["top"], right["top"])
    return any(left["right"] - 1.0 <= x <= right["left"] + 1.0 and low < top and high > bottom
               for x, low, high in vertical_rules)


def _pdf_packed_cells(table_groups: dict[tuple[int, int], list[dict[str, Any]]]) -> bool:
    """1セルの同じ行に語群が _PDF_PACKED_CELL_GROUPS 以上並ぶか（縦罫線が無く複数の列が1セルに入った格子）。"""
    for groups in table_groups.values():
        by_line: list[list[dict[str, Any]]] = []
        for group in sorted(groups, key=lambda item: -item.get("baseline_y", item["bbox"][1])):
            height = max(float(group["bbox"][3]) - float(group["bbox"][1]), 1.0)
            if by_line and abs(by_line[-1][0].get("baseline_y", 0.0) - group.get("baseline_y", 0.0)) \
                    <= max(1.0, _PDF_GROUP_LINE_RATIO * height):
                by_line[-1].append(group)
            else:
                by_line.append([group])
        if any(len(line) >= _PDF_PACKED_CELL_GROUPS for line in by_line):
            return True
    return False


def _pdf_row_lines(groups: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """語群を上から下・同じ行は左から右にそろえた行のリストにする。"""
    lines: list[list[dict[str, Any]]] = []
    for group in sorted(groups, key=lambda item: -item.get("baseline_y", item["bbox"][1])):
        height = max(float(group["bbox"][3]) - float(group["bbox"][1]), 1.0)
        if lines and abs(lines[-1][0].get("baseline_y", 0.0) - group.get("baseline_y", 0.0)) \
                <= max(1.0, _PDF_GROUP_LINE_RATIO * height):
            lines[-1].append(group)
        else:
            lines.append([group])
    return [sorted(line, key=lambda item: float(item["bbox"][0])) for line in lines]


def _pdf_cell_slot(group: dict[str, Any], grid: tuple[Any, ...]) -> tuple[int, int] | None:
    xs, ys = grid[0], grid[1]
    bbox = group["bbox"]
    x = float(bbox[0])
    y = float(group.get("baseline_y", (bbox[1] + bbox[3]) / 2))
    column = next((index for index, (left, right) in enumerate(zip(xs, xs[1:], strict=False), start=1)
                   if left - 0.75 <= x < right), None)
    row = next((index for index, (top, bottom) in enumerate(zip(ys, ys[1:], strict=False), start=1)
                if bottom - 0.75 <= y <= top + 0.75), None)
    return (row, column) if row is not None and column is not None else None
