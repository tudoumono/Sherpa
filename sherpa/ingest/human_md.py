"""人間が読める `{rel}.md` を、xlsx・docx の document-ir から生成する。

`document_ir.py` の標準構造を、`arms/ooxml_arm` の人間向け MD 生成と共有する。表候補・結合セル・ネスト表位置は
`ooxml/excel.py::regions()` と `arms/ooxml_arm._docx_table_walk` の解決結果をそのまま使う。
pptx/PDF と `.rag.md`／`evidence_render.py`（RAG 側の表現）は対象外。
設計: docs/design/rag.md「人向け MD と RAG 正本の作り分け（マージの実際）」

出力は打切りをしないが、次の安全弁を持つ。
① 走査は `excel.DEFAULT_CAP_CELLS` までで、超過した表・シートには注記を出す。
② 面積が `_MAX_MERGE_DUPLICATE_CELLS` を超える結合セルは値を複製せず、起点セルに1回だけ出して「（結合R×C）」と注記する。
③ `{rel}.md` 1ファイル全体（シートごとにリセットしない）で `_MAX_HUMAN_MD_BYTES` を上限とし、行グループ単位で
   逐次書き出して尽きたら生成を打ち切って注記する。区切り・グループ見出し・注記も実バイトで計上する。
1表が `_MAX_GROUP_CHARS` を超えたら行単位でグループに分けて注記する（③に達しない限り行は落とさない）。
数式・原値・書式の表示は対象外。表候補が0件の xlsx でも `render_xlsx` は非 None を返す（変換未対応と区別するため）。
"""
from __future__ import annotations

import re

from . import document_ir
from .ooxml import excel

# レンダラの版。`office_md._current_human_md_sig()` が抽出器の版と合成し `{rel}.derived.json` に記録する。出力形状を変えたら上げる
HUMAN_MD_RENDERER_VERSION = "human-md-renderer-v6"

# 1グループ（画面に1回に出すパイプ表の塊）あたりの目安上限文字数
_MAX_GROUP_CHARS = 20_000

# 結合セルの値を継続セルへ複製する面積の上限（超える結合は起点セルに1回だけ出す）
_MAX_MERGE_DUPLICATE_CELLS = 200

# 人間向け MD の出力上限（文書全体）。`grep_tool._GREP_FILE_CAP_BYTES` の既定と揃えた固定値で、env には追随させない
_MAX_HUMAN_MD_BYTES = 8 * 1024 * 1024

# シートの可視性注記。`Element.visibility_reason` を、事実だけの平文へ変換する（AI の観測・内部語彙は含めない）
_SHEET_VISIBILITY_NOTES = {
    "hidden_sheet": "（非表示のシートです）",
    "very_hidden": "（完全に非表示のシートです。通常の操作では再表示できません）",
}


def _sheet_visibility_note(reason: str | None) -> str:
    """`reason`（`Element.visibility_reason`）に対応する見出し用の平文注記。対象外の reason は空文字列。"""
    return _SHEET_VISIBILITY_NOTES.get(reason or "", "")


# 打切り理由の注記用に確保する予約量。本文の消費はこの分を除いた枠で行い、注記は `note()` で予約枠から書く
# （最終出力を必ず上限以内に収めるため）
_TRUNCATION_NOTE_RESERVE_BYTES = 1024


class _OutputBudget:
    """出力バイト数の予算を追跡する。`render_xlsx`／`render_docx` が文書全体で1つ作り、全体で共有する。

    本文用の枠（`remaining`）と打切り注記用の予約枠は分けてあり、`consume()` は前者、`note()` は後者だけを減らす。
    """
    def __init__(self, limit: int | None = None):
        # 既定値は呼び出し時に読む（引数の既定値に束縛しない）
        total = _MAX_HUMAN_MD_BYTES if limit is None else limit
        # limit が極端に小さい場合は予約を半分までに抑える
        reserve = min(_TRUNCATION_NOTE_RESERVE_BYTES, total // 2)
        self.remaining = total - reserve
        self._note_remaining = reserve
        self.truncated = False

    def consume(self, text: str) -> bool:
        """`text` を書き出してよければ予算を消費して True。予算切れなら False を返し `truncated` を立てる。

        区切り（`"\n\n"`＝2バイト）を各ブロックに +2 で見積もる（過小評価にならない側）。"""
        if self.truncated:
            return False
        size = len(text.encode("utf-8")) + 2
        if size > self.remaining:
            self.truncated = True
            return False
        self.remaining -= size
        return True

    def note(self, text: str) -> str:
        """予約枠から打切り注記を書き出す（複数回呼べ、呼ぶたびに予約枠を減らす）。

        本文枠が尽きたあとでも注記を残すため、`consume()` は経由しない。枠が尽きたら空文字列を返す。
        枠を超える注記は UTF-8 境界を壊さないよう切り詰める。
        """
        if self._note_remaining <= 0:
            return ""
        encoded = text.encode("utf-8")
        if len(encoded) > self._note_remaining:
            encoded = encoded[: self._note_remaining]
            while encoded and (encoded[-1] & 0xC0) == 0x80:  # UTF-8 継続バイトの途中で切らない
                encoded = encoded[:-1]
        self._note_remaining -= len(encoded)
        return encoded.decode("utf-8", errors="ignore")


def _column_letters(column: int) -> str:
    letters = ""
    while column > 0:
        column, rest = divmod(column - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _figure_text_block(figures: list | None, *, with_cell: bool = False) -> str:
    """WMF/EMF 図の描画命令にある文字（元の値）。AI の観測ではなく原本の値。"""
    parts = []
    for figure in figures or []:
        lines = [line for line in figure.lines if line.strip()]
        if not lines:
            continue
        where = ""
        if with_cell and figure.anchor is not None:
            where = f"（{_column_letters(figure.anchor[1])}{figure.anchor[0]}付近）"
        parts.append("図の中の文字（元の値）" + where + "\n" + "\n".join("- " + line for line in lines))
    return "\n\n".join(parts)


def _table_min_row(table: document_ir.Element) -> int | None:
    match = re.match(r"[A-Za-z]+(\d+)", str(table.source_map.get("range", "")))
    return int(match.group(1)) if match else None


def render_xlsx(ir: document_ir.DocumentIR, figure_texts: dict[str, list] | None = None) -> str | None:
    """xlsx の document-ir から人間向け MD を生成する。`ir` にシートが無ければ None（表候補が0件でも None にしない）。

    ① シートごとに `## シート「{名前}」` を出す。
    ② 各シートの下に、`regions()` が検出した表候補ごとに `### {セル範囲}` の小見出し＋パイプ表を並べる。
    見出し・注記・表・区切りのすべてが `_MAX_HUMAN_MD_BYTES` の予算を文書全体で1つ共有する。
    予算切れ以降は残りのシートも省略し、末尾に1回だけ注記する（注記は予約枠から書く）。
    """
    sheets = [e for e in ir.elements if e.type == "sheet"]
    if not sheets:
        return None
    budget = _OutputBudget()
    out: list[str] = []
    omitted_sheets = 0
    for si, sheet in enumerate(sheets):
        if budget.truncated:
            omitted_sheets = len(sheets) - si
            break
        sheet_parts: list[str] = []
        heading = f"## シート「{sheet.source_map.get('sheet', '')}」" + _sheet_visibility_note(sheet.visibility_reason)
        if budget.consume(heading):
            sheet_parts.append(heading)
        if sheet.source_map.get("truncated"):
            note = (f"（注記: このシートは走査上限（{excel.DEFAULT_CAP_CELLS:,}セル）を超えるため、"
                     "一部の内容を省略しました）")
            if budget.consume(note):
                sheet_parts.append(note)
        picture_count = sheet.source_map.get("picture_count")
        if picture_count:
            # 画像は存在（枚数）だけを述べる。内容の解釈・OCR/VLM 観測は載せない
            note = f"（画像が{picture_count}枚あります。内容は原本で確認してください）"
            if budget.consume(note):
                sheet_parts.append(note)
        tables = sorted(
            (e for e in ir.elements if e.type == "table" and e.parent_id == sheet.element_id),
            key=lambda e: e.order)
        # 図の文字は、アンカーのセルより上から始まる最後の表の直後へ（該当なし・位置不明の図はシート先頭）
        figure_slots: dict[int, list] = {}
        for figure in (figure_texts or {}).get(sheet.source_map.get("sheet", ""), []):
            slot = -1
            if figure.anchor is not None:
                for ti, table in enumerate(tables):
                    min_row = _table_min_row(table)
                    if min_row is not None and min_row <= figure.anchor[0]:
                        slot = ti
            figure_slots.setdefault(slot, []).append(figure)
        top_block = _figure_text_block(figure_slots.get(-1), with_cell=True)
        if top_block and budget.consume(top_block):
            sheet_parts.append(top_block)
        if not tables:
            note = "（このシートには値のあるセルが見つかりませんでした）"
            if budget.consume(note):
                sheet_parts.append(note)
        else:
            for ti, table in enumerate(tables):
                if budget.truncated:
                    break
                rendered = _render_xlsx_table(table, budget)
                if rendered:
                    sheet_parts.append(rendered)
                elif budget.truncated:
                    break
                block = _figure_text_block(figure_slots.get(ti), with_cell=True)
                if block and budget.consume(block):
                    sheet_parts.append(block)
        if sheet_parts:
            out.append("\n\n".join(sheet_parts))
        elif budget.truncated:
            omitted_sheets = len(sheets) - si   # 見出しも入らなかったシートから丸ごと省略
            break
    if budget.truncated:
        mib = _MAX_HUMAN_MD_BYTES // (1024 * 1024)
        note = f"（注記: 出力上限（{mib}MiB）に達したため、以降の内容を省略しました"
        note += f"・未表示のシート {omitted_sheets} 件）" if omitted_sheets else "）"
        out.append(budget.note(note))
    return "\n\n".join(out)


def _render_xlsx_table(table: document_ir.Element, budget: "_OutputBudget") -> str:
    if budget.truncated:
        return ""
    sm = table.source_map
    parts: list[str] = []
    heading = f"### {sm.get('range', '')}"
    if not budget.consume(heading):
        return ""
    parts.append(heading)
    for note in _table_notes(sm):
        if not budget.consume(note):
            break
        parts.append(note)
    grid = _render_cells_grid(table.cells or [], budget)
    if grid:
        parts.append(grid)
    return "\n\n".join(p for p in parts if p)


def _count_descendant_elements(element_id: str | None, by_parent: dict) -> int:
    """`element_id` の子孫要素数（自分自身は含まない・再帰）。省略した部分木の件数を過小報告しないために使う。"""
    total = 0
    for child in by_parent.get(element_id, []):
        total += 1 + _count_descendant_elements(child.element_id, by_parent)
    return total


def _count_elements_with_descendants(elements, by_parent: dict) -> int:
    """`elements`（それぞれ自分自身）＋各々の子孫を合算した要素数。着手しなかった部分木を省略件数に計上するのに使う。"""
    return sum(1 + _count_descendant_elements(e.element_id, by_parent) for e in elements)


def render_docx(ir: document_ir.DocumentIR, figure_texts: list | None = None) -> str | None:
    """docx の document-ir から人間向け MD を生成する。本文が1件も無ければ None。

    見出し・段落・表を原本の出現順で並べる。表は `_docx_table_walk` が解決した結合をそのまま展開したパイプ表にし、
    ネスト表は直後に小見出し付きで続ける。見出し・段落・表のすべてが文書全体で `_MAX_HUMAN_MD_BYTES` を共有し、
    予算切れ以降は出力せず末尾に「（以降 N 要素を省略）」と注記する（N にはネスト表内の省略も含める）。
    `ir.picture_count` があれば文書冒頭に画像枚数の事実だけを1回出す。
    `figure_texts`（WMF/EMF 図の文字）は、その図を含む本文直下の段落・表の直後へ出し、位置不明の図は本文の先頭にまとめる。
    """
    by_parent: dict[str | None, list[document_ir.Element]] = {}
    for e in ir.elements:
        if e.type in ("heading", "paragraph", "table"):
            by_parent.setdefault(e.parent_id, []).append(e)
    top = sorted(by_parent.get(None, []), key=lambda e: e.order)
    if not top:
        return None
    budget = _OutputBudget()
    out: list[str] = []
    if ir.picture_count:
        # 画像は存在（枚数）だけを述べる。内容の解釈・OCR/VLM 観測は載せない
        note = f"（画像が{ir.picture_count}枚あります。内容は原本で確認してください）"
        if budget.consume(note):
            out.append(note)
    # 図の文字は、その図を含む段落・表の直後へ。位置不明の図は先頭
    figure_slots: dict[int, list] = {}
    for figure in figure_texts or []:
        slot = -1
        if figure.anchor is not None:
            paragraph_limit, table_limit = figure.anchor
            for index, element in enumerate(top):
                if element.type == "table":
                    position = element.source_map.get("table_index")
                    before = position is not None and position < table_limit
                else:
                    position = element.source_map.get("paragraph_index")
                    before = position is not None and position < paragraph_limit
                if before:
                    slot = index
        figure_slots.setdefault(slot, []).append(figure)
    figure_block = _figure_text_block(figure_slots.get(-1))
    if figure_block and budget.consume(figure_block):
        out.append(figure_block)
    omitted = [0]                                     # 再帰全体で共有する省略カウンタ
    for i, e in enumerate(top):
        if budget.truncated:
            omitted[0] += _count_elements_with_descendants(top[i:], by_parent)
            break
        rendered = _render_docx_element(e, by_parent, budget, omitted)
        if rendered:
            out.append(rendered)
        block = _figure_text_block(figure_slots.get(i))
        if block and budget.consume(block):
            out.append(block)
        # rendered が空でも `_render_docx_element` が自己申告済みなので二重に数えない
    if budget.truncated:
        out.append(budget.note(f"（以降 {omitted[0]} 要素を省略）"))
    return "\n\n".join(out) if out else None


def _render_docx_element(e: document_ir.Element, by_parent: dict, budget: "_OutputBudget",
                          omitted: list[int]) -> str:
    """自己申告契約: 予算切れで自分の出力が空になる場合は、自分の分（表は未着手の子孫も含む）を `omitted[0]` へ加算してから `""` を返す。

    呼び出し元は戻り値が空でも二重に加算せず、着手しなかった兄弟（と子孫）だけを `_count_elements_with_descendants` で計上する。
    """
    if budget.truncated:
        return ""
    if e.type == "heading":
        level = max(1, min(6, e.source_map.get("level") or 1))
        text = "#" * level + " " + (e.text or "")
        if budget.consume(text):
            return text
        omitted[0] += 1
        return ""
    if e.type == "paragraph":
        text = e.text or ""
        if not text:
            return ""
        if budget.consume(text):
            return text
        omitted[0] += 1
        return ""
    parts: list[str] = []
    for note in _table_notes(e.source_map):
        if not budget.consume(note):
            # 表自身と未着手の子孫（グリッド・ネスト表）を丸ごと省略する
            omitted[0] += 1 + _count_descendant_elements(e.element_id, by_parent)
            return ""
        parts.append(note)
    grid = _render_cells_grid(e.cells or [], budget)
    if grid:
        parts.append(grid)
    nested_list = sorted(by_parent.get(e.element_id, []), key=lambda c: c.order)
    for j, nested in enumerate(nested_list):
        if budget.truncated:
            omitted[0] += _count_elements_with_descendants(nested_list[j:], by_parent)
            break
        host_row = nested.source_map.get("host_row")
        host_col = nested.source_map.get("host_column")
        heading = f"#### ネスト表（{host_row}行{host_col}列）"
        if not budget.consume(heading):
            omitted[0] += _count_elements_with_descendants(nested_list[j:], by_parent)
            break
        nested_text = _render_docx_element(nested, by_parent, budget, omitted)
        if nested_text:
            parts.append(heading)
            parts.append(nested_text)
        # nested_text が空でも nested が自己申告済み
    if not parts and budget.truncated:
        omitted[0] += 1     # 表自体は何も出せなかった
    return "\n\n".join(p for p in parts if p)


# `Element.source_map` の異常系フラグ → 注記文の対応表（xlsx・docx 共通）
_FLAG_NOTES = {
    "docx_column_span_clamped": "結合/列の指定が異常に大きかったため、表示上の範囲を制限しました",
    "docx_row_span_clamped": "縦結合の指定が表の行数を超えていたため、範囲を制限しました",
    "docx_vmerge_text_merged": "結合セルの継続セルに本文があったため、結合の先頭セルへ統合しました",
    "docx_column_overflow_dropped": "表の列数が上限（63列）を超えたため、超過分の列を省略しました",
}


def _table_notes(sm: dict) -> list[str]:
    notes = []
    if sm.get("truncated"):
        notes.append("（注記: 走査上限に達したため、この表の続きが省略されている可能性があります）")
    if sm.get("split_budget_exhausted"):
        notes.append("（注記: 隣接する表と癒着している可能性があります）")
    for flag in sm.get("flags", []):
        text = _FLAG_NOTES.get(flag)
        if text:
            notes.append(f"（注記: {text}）")
    return notes


def _render_cells_grid(cells: list[document_ir.Cell], budget: "_OutputBudget") -> str:
    """位置付きセル配列（結合の起点のみ・row_span/column_span 付き）をパイプ表にする。

    ① 結合は面積が `_MAX_MERGE_DUPLICATE_CELLS` 以下のときだけ値を継続セルへ複製し、超えるときは起点セルに1回だけ「（結合R×C）」付きで出す。
    ② 行を1行ずつ生成して `_MAX_GROUP_CHARS` のグループへ振り分ける（密な二次元配列は作らず、結合の範囲は `active` で追う）。
    ③ グループごとに見出し＋本体を1単位として予算判定し、入らなかったグループは `omitted_groups` へ数えて生成を打ち切る。
    起点が持たない座標は空セルで埋める（表示上の穴埋めで値の欠落ではない）。
    """
    if not cells:
        return ""
    if budget.truncated:
        return ""
    min_row = min(c.row for c in cells)
    max_row = max(c.row + c.row_span - 1 for c in cells)
    min_col = min(c.column for c in cells)
    max_col = max(c.column + c.column_span - 1 for c in cells)

    by_start_row: dict[int, list[document_ir.Cell]] = {}
    for c in cells:
        by_start_row.setdefault(c.row, []).append(c)

    active: dict[int, tuple[str, int]] = {}    # column -> (text, end_row)
    shown: list[tuple[str, str]] = []          # 収まった (見出し, 本体) の並び
    group_index = 0                            # 逐次のグループ番号（1始まり）
    total_rows_shown = 0
    omitted_groups = 0
    current: list[str] = []
    current_chars = 0
    any_oversized = False
    stopped_early = False

    def _flush(rows: list[str]) -> bool:
        nonlocal group_index, total_rows_shown, omitted_groups
        group_index += 1
        body = "\n".join(rows)
        header = f"#### グループ{group_index}"
        if not budget.consume(f"{header}\n\n{body}"):     # 見出し＋本体を1単位で判定
            omitted_groups += 1
            return False
        shown.append((header, body))
        total_rows_shown += len(rows)
        return True

    for r in range(min_row, max_row + 1):
        for c in by_start_row.get(r, ()):
            end_row = r + c.row_span - 1
            area = c.row_span * c.column_span
            if area <= _MAX_MERGE_DUPLICATE_CELLS:
                text = _normalize_cell_text(c.text)
                for col in range(c.column, c.column + c.column_span):
                    active[col] = (text, end_row)
            else:
                # 面積の大きい結合は複製せず起点セルにだけ値と注記を出す。終了行を `r` にして後続の行へ繰り返さない
                text = f"{_normalize_cell_text(c.text)}（結合{c.row_span}×{c.column_span}）"
                active[c.column] = (text, r)
        row_str = "| " + " | ".join(
            active.get(col, ("", 0))[0] for col in range(min_col, max_col + 1)) + " |"
        row_chars = len(row_str) + 1
        if row_chars > _MAX_GROUP_CHARS:
            any_oversized = True                # 1行だけの超過も分割注記の対象にする
        if current and current_chars + row_chars > _MAX_GROUP_CHARS:
            if not _flush(current):
                stopped_early = True
                break
            current, current_chars = [], 0
        current.append(row_str)
        current_chars += row_chars
        for col_key in [k for k, (_, er) in active.items() if er <= r]:
            del active[col_key]
    else:
        if current:
            if not _flush(current):
                stopped_early = True
    return _render_groups(budget, shown, any_oversized, stopped_early,
                          total_rows_shown=total_rows_shown, omitted_groups=omitted_groups)


def _normalize_cell_text(text: str | None) -> str:
    """パイプ表1セル分として安全な1行文字列にする（改行は `<br>`・`|` はエスケープ）。値は失わない。"""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    return t.replace("|", "\\|")


def _render_groups(budget: "_OutputBudget", shown: list[tuple[str, str]], any_oversized: bool,
                    stopped_early: bool, *, total_rows_shown: int, omitted_groups: int) -> str:
    """`_render_cells_grid` が確定した `(見出し, 本体)` の並びを最終的なパイプ表テキストにする。

    `shown` は消費済みなので再消費しない。複数グループのときだけ各グループの見出しと「Nグループに分割して表示します」を添える。
    通常時の要約/警告注記は `consume()` で計上し、`stopped_early` 時の打切り注記は `budget.note()`（予約枠）から書く。
    """
    def _try_add(text: str) -> str:
        if stopped_early:
            return budget.note(text)
        return text if budget.consume(text) else ""

    if not shown:
        if not stopped_early:
            return ""
        return budget.note("（注記: 出力上限に達したため、この表は表示できませんでした）")

    if len(shown) == 1 and not any_oversized and not stopped_early and omitted_groups == 0:
        return shown[0][1]                                  # 唯一・打切りなし＝素の本体

    parts: list[str] = []
    multi = len(shown) > 1 or omitted_groups > 0
    if multi:
        summary = (f"（注記: 表が大きいため複数グループに分割して表示します。ここまでの "
                   f"{total_rows_shown} 行）" if stopped_early else
                   f"（注記: 表が大きいため {len(shown)} グループに分割して表示します。"
                   f"全 {total_rows_shown} 行）")
        note = _try_add(summary)
        if note:
            parts.append(note)
        for header, body in shown:
            parts.append(header)
            parts.append(body)
    else:
        if any_oversized:
            note = _try_add("（注記: この表には非常に大きい行が含まれるため、表示が崩れる可能性があります）")
            if note:
                parts.append(note)
        parts.append(shown[0][1])                            # 見出し無し（1グループのみ）
    if stopped_early:
        note = (budget.note(f"（注記: 出力上限に達したため、以降 {omitted_groups} グループを省略しました）")
                if omitted_groups else
                budget.note("（注記: 出力上限に達したため、この表の続きを省略しました）"))
        if note:
            parts.append(note)
    return "\n\n".join(parts)
