"""人間が読める `{rel}.md` を、xlsx・docx の document-ir から生成する。

`document_ir.py` の標準構造を、`arms/ooxml_arm` の人間向け MD 生成と共有する。表候補・結合セル・ネスト表位置は
`ooxml/excel.py::regions()` と `arms/ooxml_arm._docx_table_walk` の解決結果をそのまま使う。
pptx と `.rag.md`／`evidence_render.py`（RAG 側の表現）は対象外。PDF は Evidence IR から `render_pdf` で組む。
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
from collections.abc import Callable

from . import document_ir, observation_vocab
from .ooxml import excel

# レンダラの版。`office_md._current_human_md_sig()` が抽出器の版と合成し `{rel}.derived.json` に記録する。出力形状を変えたら上げる
HUMAN_MD_RENDERER_VERSION = "human-md-renderer-v10"

# PDF の人向け MD（`render_pdf`）の版。`office_md._current_pdf_human_md_sig()` が PDF の変換キャッシュの鍵と再生成の判定に使う。出力形状を変えたら上げる
PDF_HUMAN_MD_VERSION = "pdf-human-md-v3"

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


def render_xlsx(ir: document_ir.DocumentIR, figure_texts: dict[str, list] | None = None,
                notes: EvidenceNotes | None = None) -> str | None:
    """xlsx の document-ir から人間向け MD を生成する。`ir` にシートが無ければ None（表候補が0件でも None にしない）。

    ① シートごとに `## シート「{名前}」` を出す。
    ② 各シートの下に、`regions()` が検出した表候補ごとに `### {セル範囲}` の小見出し＋パイプ表を並べる。
    `notes`（Evidence IR 由来）があれば、状態・関係・未解析／未対応の通知を、そのセルを含む表の直後・図形のアンカーの位置に「※」の行で足す。
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
        note_slots = xlsx_sheet_notes(notes, sheet.source_map.get("sheet", ""), tables) if notes is not None else {}
        _append_note_blocks(note_slots.get(-1, []), sheet_parts, budget)
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
                _append_note_blocks(note_slots.get(ti, []), sheet_parts, budget)
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


def _append_note_blocks(blocks: list[str], parts: list[str], budget: _OutputBudget) -> None:
    """注記の行を、予算が許す限り `parts` へ追記する（予算切れの打切り注記は呼び出し側の全体の注記に任せる）。"""
    for block in blocks:
        if not budget.consume(block):
            return
        parts.append(block)


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


def render_docx(ir: document_ir.DocumentIR, figure_texts: list | None = None,
                notes: DocxNotes | None = None) -> str | None:
    """docx の document-ir から人間向け MD を生成する。本文・テキストボックス・画像・図の文字・出すべき注記のどれも無ければ None。

    見出し・段落・表を原本の出現順で並べる。表は `_docx_table_walk` が解決した結合をそのまま展開したパイプ表にし、
    ネスト表は直後に小見出し付きで続ける。テキストボックスは、ホストの見出し・段落、または表（ネスト表ならそのネスト表）の
    直後に「（テキストボックス）本文」の別の行として要素ごとに出す（表のセルの中なら位置を添える）。
    見出し・段落・表・テキストボックスのすべてが文書全体で `_MAX_HUMAN_MD_BYTES` を共有し、
    予算切れ以降は出力せず末尾に「（以降 N 要素を省略）」と注記する（N にはネスト表内・テキストボックスの省略も含める）。
    `ir.picture_count` があれば文書冒頭に画像枚数の事実だけを1回出す。
    `notes`（Evidence IR 由来）があれば、図形・画像・OLE などの状態・関係・未解析／未対応の通知を、その図形がある本文の要素の直後に「※」の行で足す。
    `figure_texts`（WMF/EMF 図の文字）は、その図を含む本文直下の段落・表の直後へ出し、位置不明の図は本文の先頭にまとめる。
    本文の無い段落にあるテキストボックス（ホストが無い）は、直前の本文要素（`source_map.after_body_order`）の直後
    （文書の先頭なら本文の先頭）へ出し、同じ位置の図の文字より先に置く。
    """
    by_parent: dict[str | None, list[document_ir.Element]] = {}
    for e in ir.elements:
        if e.type in ("heading", "paragraph", "table") or (e.type == "textbox" and e.parent_id is not None):
            by_parent.setdefault(e.parent_id, []).append(e)
    top = sorted(by_parent.get(None, []), key=lambda e: e.order)
    orphans = [e for e in ir.elements if e.type == "textbox" and e.parent_id is None]
    if not top and not orphans and not ir.picture_count and not figure_texts and notes is None:
        return None            # 本当に何も無い文書。画像・図の文字・出すべき注記があれば空とは扱わない
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
    # ホストの無いテキストボックスは、直前の本文要素（`after_body_order`＝その要素の order）の直後へ（0・不明は先頭）
    orphan_slots: dict[int, list[document_ir.Element]] = {}
    for box in orphans:
        after = box.source_map.get("after_body_order")
        slot = -1
        for index, element in enumerate(top):
            if after is not None and element.order <= after:
                slot = index
        orphan_slots.setdefault(slot, []).append(box)
    # 注記は「それを含む本文の子の位置以下で最後の本文要素」の直後へ（無ければ先頭）
    note_slots: dict[int, list[str]] = {}
    if notes is not None:
        body_positions = [notes.body_index(e) for e in top]
        for position, block in notes.entries:
            slot = -1
            for index, body_position in enumerate(body_positions):
                if body_position is not None and body_position <= position:
                    slot = index
            note_slots.setdefault(slot, []).append(block)
    omitted = [0]                                     # 再帰全体で共有する省略カウンタ
    _append_textbox_lines(orphan_slots.get(-1, []), out, budget, omitted)
    _append_note_blocks(note_slots.get(-1, []), out, budget)
    figure_block = _figure_text_block(figure_slots.get(-1))
    if figure_block and budget.consume(figure_block):
        out.append(figure_block)
    for i, e in enumerate(top):
        if budget.truncated:
            omitted[0] += _count_elements_with_descendants(top[i:], by_parent)
            omitted[0] += sum(len(orphan_slots.get(k, [])) for k in range(i, len(top)))
            break
        rendered = _render_docx_element(e, by_parent, budget, omitted)
        if rendered:
            out.append(rendered)
        _append_textbox_lines(orphan_slots.get(i, []), out, budget, omitted)
        _append_note_blocks(note_slots.get(i, []), out, budget)
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
    children = by_parent.get(e.element_id, [])
    boxes = [c for c in children if c.type == "textbox"]   # 要素の並び（＝原本の出現順）のまま使う
    if e.type in ("heading", "paragraph"):
        if e.type == "heading":
            level = max(1, min(6, e.source_map.get("level") or 1))
            text = "#" * level + " " + (e.text or "")
        else:
            text = e.text or ""
        lines: list[str] = []
        if text:
            if not budget.consume(text):
                omitted[0] += 1 + len(boxes)
                return ""
            lines.append(text)
        _append_textbox_lines(boxes, lines, budget, omitted)
        return "\n\n".join(lines)
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
    _append_textbox_lines(boxes, parts, budget, omitted)
    nested_list = sorted((c for c in children if c.type == "table"), key=lambda c: c.order)
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


def _textbox_line(box: document_ir.Element) -> str:
    """テキストボックス1つを、短い前置き付きの1ブロックにする。表のセルの中なら行・列を添える。"""
    row, column = box.source_map.get("row"), box.source_map.get("column")
    where = f"・{row}行{column}列" if row is not None and column is not None else ""
    return f"（テキストボックス{where}）{box.text or ''}"


def _append_textbox_lines(boxes: list[document_ir.Element], parts: list[str], budget: "_OutputBudget",
                          omitted: list[int]) -> None:
    """`boxes` を出現順に `parts` へ追記する。予算切れになったら、残りの件数を `omitted[0]` へ加算して止める。"""
    for k, box in enumerate(boxes):
        line = _textbox_line(box)
        if not budget.consume(line):
            omitted[0] += len(boxes) - k
            return
        parts.append(line)


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
    ③ グループごとに見出し＋本体を1単位として予算判定し、入らなかった時点で生成を打ち切る。省略した行数（全行数－表示した行数）を注記に出す。
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
    omitted_rows = (max_row - min_row + 1) - total_rows_shown if stopped_early else 0
    return _render_groups(budget, shown, any_oversized, stopped_early,
                          total_rows_shown=total_rows_shown, omitted_groups=omitted_groups,
                          omitted_rows=omitted_rows)


def _normalize_cell_text(text: str | None) -> str:
    """パイプ表1セル分として安全な1行文字列にする（改行は `<br>`・`|` はエスケープ）。値は失わない。"""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    return t.replace("|", "\\|")


def _render_groups(budget: "_OutputBudget", shown: list[tuple[str, str]], any_oversized: bool,
                    stopped_early: bool, *, total_rows_shown: int, omitted_groups: int,
                    omitted_rows: int = 0) -> str:
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
        return budget.note(f"（注記: 出力上限に達したため、この表（{omitted_rows} 行）は表示できませんでした）"
                           if omitted_rows > 0 else
                           "（注記: 出力上限に達したため、この表は表示できませんでした）")

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
        note = (budget.note(f"（注記: 出力上限に達したため、以降の {omitted_rows} 行を省略しました）")
                if omitted_rows > 0 else
                budget.note("（注記: 出力上限に達したため、この表の続きを省略しました）"))
        if note:
            parts.append(note)
    return "\n\n".join(parts)


# 同じ行とみなす縦位置の差（行頭の文字の高さに対する比）と、その下限（PDF のポイント）
_PDF_ROW_TOLERANCE_RATIO = 0.3
_PDF_ROW_TOLERANCE_MIN = 1.0


def render_pdf(evidence, page_body: Callable[[int], str]) -> str | None:
    """PDF の Evidence IR → 人向け Markdown（`## ページ N` ごと）。ページが 1 つも出なければ None。

    ① ページ内の表（`table`＋`cell`）と表の外の位置付き文字（`positioned_text`）を、上から下・同じ高さなら左から右に並べる（表は上端の位置）。
    ② 表は Markdown の表（1 行目が見出し・結合セルは左上だけ値）、同じ高さの文字は 1 行にまとめる。
    ③ 位置付き文字も表も無いページは `page_body(ページ番号)`（pdfium のページ本文を整形したもの）をそのまま出す。
    設計: docs/design/rag.md「人向け MD と RAG 正本の作り分け（マージの実際）」
    """
    texts: dict[str, list] = {}
    tables: dict[str, list] = {}
    cells: dict[str, list] = {}
    pages = []
    for e in evidence.elements:
        if e.visibility != "visible":
            continue
        if e.type == "page":
            pages.append(e)
        elif e.type == "positioned_text" and e.locator.bbox and isinstance(e.value, str) and e.value.strip():
            texts.setdefault(e.parent_id, []).append(e)
        elif e.type == "table" and e.locator.bbox:
            tables.setdefault(e.parent_id, []).append(e)
        elif e.type == "cell":
            cells.setdefault(e.parent_id, []).append(e)
    out = []
    for page in sorted(pages, key=lambda p: (p.locator.page or 0, p.order)):
        number = page.locator.page or 0
        blocks = _pdf_page_blocks(texts.get(page.element_id, []), tables.get(page.element_id, []), cells)
        if not blocks:
            body = page_body(number)
            blocks = [body] if body else []
        if blocks:
            out.append(f"## ページ {number}\n\n" + "\n\n".join(blocks))
    return "\n\n".join(out) if out else None


_PDF_NO_TEXT_NOTICE = ("（文字の層が無いスキャン PDF のため、ここには本文がありません。本文は画像の読み取り（OCR）で検索できます"
                       "（読み取りが終わるまでは検索に出ません）。内容は原本で確認してください）")


def render_pdf_without_text(evidence) -> str:
    """文字の層が無いスキャン PDF の人向け MD。冒頭に知らせ、続けて `## ページ N` と「（画像だけのページです）」をページ数分出す。

    OCR の進み具合に左右されない決定的な出力（読み取り結果は rag.md 側に入る）。
    """
    pages = sorted((e for e in evidence.elements if e.type == "page" and e.visibility == "visible"),
                   key=lambda p: (p.locator.page or 0, p.order))
    parts = [_PDF_NO_TEXT_NOTICE]
    parts.extend(f"## ページ {page.locator.page or 0}\n\n（画像だけのページです）" for page in pages)
    return "\n\n".join(parts)


def _pdf_page_blocks(texts: list, tables: list, cells: dict[str, list]) -> list[str]:
    """1 ページ分の本文ブロック（表 1 つ・または連続する文字の行の塊が 1 ブロック）を上から下の順で返す。"""
    items = []     # (縦位置, 左端, 並び順, 種別, 要素, 高さ)
    for index, e in enumerate(texts):
        left, bottom, _right, top = e.locator.bbox
        height = max(top - bottom, 1.0)
        items.append((bottom + 0.2 * height, left, index, "text", e, height))     # 基線の推定（bbox の下端から文字高の 2 割上）
    for index, e in enumerate(tables):
        items.append((e.locator.bbox[3], e.locator.bbox[0], len(texts) + index, "table", e, 0.0))
    items.sort(key=lambda it: (-it[0], it[1], it[2]))
    blocks: list[str] = []
    rows: list[list] = []        # 文字の行（行ごとに [(左端, 並び順, 文字)]）
    row_top = row_tolerance = 0.0

    def flush_rows() -> None:
        nonlocal rows
        if rows:
            blocks.append("\n".join(
                " ".join(t for _l, _i, t in sorted(row)) for row in rows))
            rows = []

    for y, left, index, kind, e, height in items:
        if kind == "table":
            flush_rows()
            blocks.append(_pdf_table_md(cells.get(e.element_id, [])))
            continue
        if e.extension.get("row_lines"):         # 縦罫線の無い表の行の塊: 行ごとの改行を残す
            flush_rows()
            blocks.append(e.value.replace("\r\n", "\n").replace("\r", "\n").strip())
            continue
        value = " ".join(e.value.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()
        if rows and row_top - y <= row_tolerance:
            rows[-1].append((left, index, value))
        else:
            rows.append([(left, index, value)])
            row_top = y
            row_tolerance = max(_PDF_ROW_TOLERANCE_MIN, _PDF_ROW_TOLERANCE_RATIO * height)
    flush_rows()
    return [b for b in blocks if b]


def _pdf_table_md(cells: list) -> str:
    """表のセル（`extension.row/column/row_span/column_span`・1 始まり）を Markdown の表にする。1 行目が見出し。"""
    if not cells:
        return ""
    n_rows = max(c.locator.extension["row"] + c.locator.extension.get("row_span", 1) - 1 for c in cells)
    n_cols = max(c.locator.extension["column"] + c.locator.extension.get("column_span", 1) - 1 for c in cells)
    grid = [[""] * n_cols for _ in range(n_rows)]
    for c in cells:
        grid[c.locator.extension["row"] - 1][c.locator.extension["column"] - 1] = _normalize_cell_text(
            c.value if isinstance(c.value, str) else None)
    lines = ["| " + " | ".join(row) + " |" for row in grid]
    lines.insert(1, "| " + " | ".join("---" for _ in range(n_cols)) + " |")
    return "\n".join(lines)


# ---- Evidence IR 由来の注記（状態・関係・未解析／未対応の通知） ----
# 設計: docs/proposals/2026-10-22-Office観測の共通の扱い.md §1.6
# 対象の直後に「※ 対象: 行」の形で短く書く。文言は `observation_vocab` の対応表から引く（RAG と同じ語）。
# 原本の内部 ID や `visibility_reason` の生の値は出さず、スライド・シート・順番・位置の言葉で対象を区別する。

NOTE_PREFIX = "※ "
_NOTE_MAX_CELLS = observation_vocab.MAX_FRONT_ITEMS

_KIND_LABELS = {
    "shape": "図形", "picture": "画像", "connector": "コネクタ", "group": "グループ", "ole_object": "埋め込みオブジェクト",
    "chart": "グラフ", "smartart": "SmartArt", "graphic_frame": "図表", "vml_shape": "図形",
    "floating_object": "図形", "floating_textbox": "テキストボックス",
}
# 注記の対象にする図形・画像などの種別（段落・セル・表・見出しなどの本文は含めない）
OBJECT_TYPES = frozenset(_KIND_LABELS)

_POSITION_WORDS = {
    (0, 0): "左上付近", (1, 0): "上部付近", (2, 0): "右上付近",
    (0, 1): "左側付近", (1, 1): "中央付近", (2, 1): "右側付近",
    (0, 2): "左下付近", (1, 2): "下部付近", (2, 2): "右下付近",
}


def _position_words(bbox, size) -> str:
    """図形の中心がスライドのどのあたりか（3×3 の区画）。大きさ・位置が分からなければ空文字列。"""
    if not bbox or not size or not size[0] or not size[1]:
        return ""
    cx = (bbox[0] + bbox[2]) / 2 / size[0]
    cy = (bbox[1] + bbox[3]) / 2 / size[1]
    return _POSITION_WORDS[(min(2, max(0, int(cx * 3))), min(2, max(0, int(cy * 3))))]


class EvidenceNotes:
    """Evidence IR の要素ごとの注記（状態・関係・通知）を、人用 Markdown の平文の短い文で作る。

    ① 対象の参照は人の言葉（スライド・シート・順番・位置）で作る。② 関係は `observation_vocab.relation_note_lines` で
    両端に分け、`relation_block` で行にする。③ 文は `observation_vocab.HUMAN_WORDING` から、RAG と同じ観測の鍵で引く。
    `slide_size` は pptx の位置の言葉に使う（EMU）。`inline=True`（pptx）は注記が対象の文字の直後に出るので主語を「この図形」にし、
    それ以外は対象の位置を主語に添える。セルは表の直後にまとめて書くので、主語は `_SUBJECT_SLOT` の差し込み口にする。
    """

    def __init__(self, evidence, slide_size: tuple[int, int] | None = None, inline: bool = False):
        self.evidence = evidence
        self.slide_size = slide_size
        self.inline = inline
        self.elements = {e.element_id: e for e in evidence.elements}
        self.ordinals: dict[str, int] = {}
        seen: dict[str, int] = {}
        for e in sorted(evidence.elements, key=lambda item: item.order):
            if e.type in OBJECT_TYPES:
                seen[e.type] = seen.get(e.type, 0) + 1
                self.ordinals[e.element_id] = seen[e.type]
        self._entries = observation_vocab.relation_note_lines(
            evidence.relations, self._ref_or_none, human=True, subject_of=self._subject_of)
        self._coverage = {c.coverage_id: c for c in evidence.coverage}
        hosts: dict[tuple, list] = {}
        for e in evidence.elements:
            if (e.type in OBJECT_TYPES and e.extension.get("origin") != "document-ir-v2-adapter"
                    and e.locator.object_id is not None):
                hosts.setdefault((e.locator.slide, e.locator.sheet, str(e.locator.object_id)), []).append(e)
        self._unsupported: dict[str, list[str]] = {}
        for item in evidence.coverage:
            if item.status not in ("unsupported", "failed") or item.scope == "part" or item.locator.object_id is None:
                continue
            token = str(item.locator.object_id).split(":", 1)[-1]
            host = self._host_for(hosts.get((item.locator.slide, item.locator.sheet, token), []), item)
            if host is None:
                continue
            line = observation_vocab.notice_line(
                item.status, item.reason_code, item.detected_kind, self._real_name(host) or self.kind(host), human=True)
            if line:
                self._unsupported.setdefault(host.element_id, []).append(self._place_notice(host, line))

    @staticmethod
    def _host_for(candidates: list, item):
        """未対応の部品の通知を載せる図形。同じ ID が別の部品（本文とヘッダーなど）にもあるときは、部品が一致するものだけ。"""
        if len(candidates) <= 1:
            return candidates[0] if candidates else None
        part = item.locator.part
        matched = [c for c in candidates if part in (c.locator.part, c.extension.get("chart_part"))
                   or part in (c.extension.get("diagram_parts") or {}).values()]
        return matched[0] if len(matched) == 1 else None

    def kind(self, element) -> str:
        return _KIND_LABELS.get(element.type, "図形")

    def _real_name(self, element) -> str:
        return str(element.extension.get("name") or "").strip() or str(element.extension.get("prog_id") or "").strip()

    def _z_label(self, element) -> str:
        parent = self.elements.get(element.parent_id or "")
        z = element.extension.get("z_order") or element.order
        if parent is not None and parent.type == "group":
            return f"{parent.extension.get('z_order') or parent.order} 番目の図形の中の {z} 番目の図形"
        return f"{z} 番目の図形"

    def where(self, element) -> str:
        """対象の位置の言葉（スライド・シート・順番）。原本の内部 ID は含めない。"""
        locator = element.locator
        if locator.slide is not None and locator.sheet is None:
            where = f"スライド {locator.slide} の {self._z_label(element)}"
            position = _position_words(locator.bbox, self.slide_size)
            return where + (f"・{position}" if position else "")
        if locator.sheet:
            return f"シート「{locator.sheet}」" + (f"のセル範囲 {locator.cell_range}" if locator.cell_range else "")
        ordinal = self.ordinals.get(element.element_id)
        return f"文書内で {ordinal} 番目の{self.kind(element)}" if ordinal else ""

    def _ref_or_none(self, element_id: str) -> str | None:
        element = self.elements.get(element_id)
        return self.ref(element) if element is not None else None

    def ref(self, element) -> str:
        """関係の相手を指す参照。例: 「覆いの図形」（スライド 1 の 3 番目の図形・中央付近）。"""
        if element.type == "cell":
            return f"セル {element.locator.cell_range}" if element.locator.cell_range else "セル"
        name = self._real_name(element)
        label = f"「{name}」" if name else self.kind(element)
        where = self.where(element)
        return f"{label}（{where}）" if where else label

    def _subject_of(self, element_id: str) -> str:
        element = self.elements.get(element_id)
        if element is None:
            return ""
        return self.subject(element)

    def subject(self, element) -> str:
        """注記の主語。pptx は対象の直後に出るので「この図形」、セルはまとめて書くので差し込み口、ほかは種類・名前・位置。"""
        if element.type == "cell":
            return _SUBJECT_SLOT
        if self.inline:
            return f"この{self.kind(element)}"
        name = self._real_name(element)
        where = self.where(element)
        label = f"{self.kind(element)}「{name}」" if name else self.kind(element)
        return f"{label}（{where}）" if where else label

    def _place_notice(self, element, line: str) -> str:
        """通知は「この…」で始まる。表の後ろにまとめて出る xlsx は、先に対象の位置を添える（pptx・docx は対象の直後に出る）。"""
        where = self.where(element) if element.locator.sheet else ""
        return f"{where}にある{line.removeprefix('この')}" if where else line

    def lines(self, element) -> list[str]:
        """要素の状態・関係・通知の文（`※` なし）。何も無ければ空。"""
        out: list[str] = []
        subject = self.subject(element)
        reason = element.extension.get("visibility_reason")
        label = observation_vocab.VISIBILITY_REASON_LABELS.get(reason) if isinstance(reason, str) else None
        # 覆いによる非表示は、覆っている相手を書く関係の文（覆われていると判定）が同じことを言うので、状態の文は重ねない
        if (element.visibility == "hidden" and label and reason not in observation_vocab.REASONS_ABOVE_COVERS
                and reason not in observation_vocab.COVER_DERIVED_REASONS):
            out.append(observation_vocab.visibility_line(reason, label, human=True, subject=subject))
        if element.type not in ("strike_text", "hidden_text", "deleted_text") and observation_vocab.is_whole_strike(
                element.extension):
            out.append(observation_vocab.strike_line(human=True, subject=subject))
        entries = self._entries.get(element.element_id)
        if entries:
            out.extend(observation_vocab.relation_block(entries, human=True))
        item = self._coverage.get(element.coverage_id)
        if item is not None:
            line = observation_vocab.notice_line(
                item.status, item.reason_code, item.detected_kind, self._real_name(element) or self.kind(element),
                human=True)
            if line:
                out.append(self._place_notice(element, line))
        out.extend(self._unsupported.get(element.element_id, []))
        return list(dict.fromkeys(out))

    def block(self, element) -> str:
        """要素の注記を「※」の行にする。注記が無ければ空文字列。"""
        return "\n".join(f"{NOTE_PREFIX}{line}" for line in self.lines(element))


_SUBJECT_SLOT = "\x00subject\x00"


def _cell_note_blocks(notes: EvidenceNotes, cells: list) -> list[str]:
    """注記のあるセルを、同じ注記ごとに「セル A1、B2は…」の文へまとめる（並べるセルは最大 `_NOTE_MAX_CELLS` 件）。"""
    groups: dict[tuple[str, ...], list[str]] = {}
    for cell in cells:
        lines = tuple(notes.lines(cell))
        if lines:
            groups.setdefault(lines, []).append(cell.locator.cell_range or "")
    out: list[str] = []
    for lines, ranges in groups.items():
        subject = "セル " + "、".join(ranges[:_NOTE_MAX_CELLS])
        if len(ranges) > _NOTE_MAX_CELLS:
            subject += f" ほか {len(ranges) - _NOTE_MAX_CELLS} 件"
        out.append("\n".join(f"{NOTE_PREFIX}{line.replace(_SUBJECT_SLOT, subject)}" for line in lines))
    return out


_RANGE_RE = re.compile(r"^([A-Za-z]+)(\d+)(?::([A-Za-z]+)(\d+))?$")


def _cell_rc(text: str) -> tuple[int, int] | None:
    match = _RANGE_RE.match(text or "")
    if not match:
        return None
    column = 0
    for ch in match.group(1).upper():
        column = column * 26 + ord(ch) - 64
    return int(match.group(2)), column


def _range_bounds(text: str) -> tuple[int, int, int, int] | None:
    first = _cell_rc(text.split(":")[0])
    last = _cell_rc(text.split(":")[-1])
    if first is None or last is None:
        return None
    return first[0], first[1], last[0], last[1]


def xlsx_sheet_notes(notes: EvidenceNotes, sheet_name: str, tables: list) -> dict[int, list[str]]:
    """シートの注記を「表の添字 → 注記の行」にする（`-1` はシートの先頭＝どの表にも属さない位置）。

    セルの注記はそのセルを含む表の直後、図形・画像などはアンカーの行より上から始まる最後の表の直後（図の文字と同じ置き方）。
    """
    bounds = [_range_bounds(str(t.source_map.get("range", ""))) for t in tables]
    cells_by_table: dict[int, list] = {}
    slots: dict[int, list[str]] = {}
    for e in notes.evidence.elements:
        if e.locator.sheet != sheet_name:
            continue
        if e.type == "cell":
            rc = _cell_rc(e.locator.cell_range or "")
            if rc is None:
                continue
            for ti, b in enumerate(bounds):
                if b is not None and b[0] <= rc[0] <= b[2] and b[1] <= rc[1] <= b[3]:
                    cells_by_table.setdefault(ti, []).append(e)
                    break
        elif e.type in OBJECT_TYPES:
            block = notes.block(e)
            if not block:
                continue
            first = _cell_rc((e.locator.cell_range or "").split(":")[0])
            slot = -1
            for ti, b in enumerate(bounds):
                if first is not None and b is not None and b[0] <= first[0]:
                    slot = ti
            slots.setdefault(slot, []).append(block)
    for ti, cells in cells_by_table.items():
        slots.setdefault(ti, []).extend(_cell_note_blocks(notes, cells))
    return slots


class DocxNotes:
    """docx の注記。各注記は「本文の並びのどこに置くか」（本文の子の位置）を持つ。"""

    def __init__(self, entries: list[tuple[int, str]], paragraph_to_body: list[int], table_to_body: list[int]):
        self.entries = entries
        self.paragraph_to_body = paragraph_to_body
        self.table_to_body = table_to_body

    def body_index(self, element: document_ir.Element) -> int | None:
        """本文の要素（見出し・段落・表）の、本文の子の位置。"""
        sm = element.source_map
        if element.type == "table":
            index, table = sm.get("table_index"), self.table_to_body
        else:
            index, table = sm.get("paragraph_index"), self.paragraph_to_body
        return table[index] if isinstance(index, int) and 0 <= index < len(table) else None


def docx_notes(document_xml: bytes, notes: EvidenceNotes, part: str = "word/document.xml") -> DocxNotes | None:
    """docx の図形・画像・OLE などの注記を、本文の子（段落・表）の位置へ結び付ける。注記が無ければ None。

    ① 図形の ID（`docPr`・VML の `shape`・`OLEObject`）を、それを含む本文の子の位置に引く。② 注記を持つ Evidence の
    図形を、`part`（`document_xml` の部品）と ID で引き当てる。ヘッダーなど別の部品の図形は、同じ ID でも本文に書かない。
    引き当てられない図形は書かない（位置の分からない注記を文書の先頭や末尾に置かない）。
    """
    from xml.etree import ElementTree as ET

    root = ET.fromstring(document_xml)
    body = next((el for el in root if el.tag.endswith("}body")), None)
    if body is None:
        return None
    id_to_body: dict[str, int] = {}
    paragraph_to_body: list[int] = []
    table_to_body: list[int] = []
    for position, child in enumerate(body):
        local = child.tag.rsplit("}", 1)[-1]
        if local == "p":
            paragraph_to_body.append(position)
        elif local == "tbl":
            table_to_body.append(position)
        for node in child.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag in ("docPr", "shape"):
                keys = [node.get("id")]
            elif tag == "OLEObject":
                keys = [node.get("ObjectID"), node.get("ShapeID")]
            else:
                continue
            for key in keys:
                if key:
                    id_to_body.setdefault(str(key), position)
    entries: list[tuple[int, str]] = []
    for e in notes.evidence.elements:
        if e.type not in OBJECT_TYPES or e.locator.object_id is None or e.locator.part != part:
            continue
        position = id_to_body.get(str(e.locator.object_id))
        if position is None:
            continue
        block = notes.block(e)
        if block:
            entries.append((position, block))
    return DocxNotes(entries, paragraph_to_body, table_to_body) if entries else None
