"""PDF の位置付き文字（PDF の 1 行＝1 要素）を、同じページの読み順で 300〜400 字の塊にまとめる計画を立てる。

要素は書き換えない。どの要素を同じチャンクにするか（と、そのチャンクに添える直前の見出し）を返すだけで、
本文・出典・識別子の組み立ては `evidence_render` が行う。LLM は使わない。
設計: docs/design/rag.md「チャンク化の考え方」

手順:
  ① 同じ高さに並ぶ断片を 1 つの「見た目の行」にする。
  ② 本文の文字サイズより大きい行を見出し、ページ端の行を欄外（ヘッダ・フッタ）と判定する。
  ③ 見出し・段組の移り・大きな空きでは必ず切り、段落の切れ目は目安の長さに達していれば切る。
     欄外はページごとに 1 つの塊にする。
  ④ 上限（400 字）を超えるときは、段落の切れ目、文末、行の順に切る位置を選ぶ。

守ること: ページを越えてまとめない。見出しとその直後の行は離さない。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from itertools import pairwise
from statistics import median

from . import evidence_ir

PDF_CHUNK_TARGET_CHARS = 300
PDF_CHUNK_MAX_CHARS = 400
_MIN_PART_CHARS = 120
_MIN_TAIL_CHARS = 80
_HEADING_RATIO = 1.12
_HEADING_MAX_CHARS = 80
_MARGIN_RATIO = 0.05
_SAME_ROW_RATIO = 0.4
_PITCH_RATIO = 1.2
_INDENT_RATIO = 0.8
_SHORT_LAST_LINE_RATIO = 3.0
_COLUMN_JUMP_RATIO = 0.5
_BLOCK_GAP_RATIO = 2.5
_SENTENCE_END = ("。", "！", "？", ".", "!", "?")
_BULLET_RE = re.compile(r"^[・•●○■□◆◇▪▶\-–—*]\s*\S")
_TITLE_RATIO = 1.2
_TITLE_MAX_CHARS = 80
_TITLE_MAX_LINES = 2
_INFO_TITLE_MAX_CHARS = 120
_PLACEHOLDER_TITLE_RE = re.compile(
    r"^(untitled|no title|無題|タイトルなし|microsoft (word|excel|powerpoint) - .*|.*\.(docx?|xlsx?|pptx?|pdf|txt|rtf))$",
    re.IGNORECASE)
_DOC_NUMBER_RE = re.compile(
    r"(?:文書番号|管理番号|資料番号|規程番号|文書\s*No\.?|Doc(?:ument)?\s*No\.?)\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9\-_/.]{1,30})",
    re.IGNORECASE)
_SECTION_NUMBER_RE = re.compile(
    r"^(第\s*[0-9０-９一二三四五六七八九十百]+\s*[章節条表図部編項]|[0-9０-９]+\s*[.．、)）]|[(（][0-9０-９]+[)）]|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ])")
_VERSION_RE = re.compile(r"(?:^|\s)版\s*[:：]\s*(第\s*[0-9０-９]+\s*版|[0-9]+(?:\.[0-9]+)*)")


@dataclass
class _Line:
    members: list[evidence_ir.EvidenceElement]
    text: str
    left: float
    right: float
    top: float
    bottom: float
    size: float
    kind: str = "body"
    para_start: bool = False


@dataclass(frozen=True)
class PdfChunkGroup:
    """同じチャンクにする要素（読み順）と、チャンクの先頭が見出しでないときに添える直前の見出し。"""
    elements: list[evidence_ir.EvidenceElement]
    texts: list[str]
    heading: str | None = None


def _bbox(element: evidence_ir.EvidenceElement) -> list[float]:
    box = element.locator.bbox or [0.0, 0.0, 0.0, 0.0]
    return [float(value) for value in box]


def _text(element: evidence_ir.EvidenceElement) -> str:
    return element.value if isinstance(element.value, str) else ""


def _size(element: evidence_ir.EvidenceElement, box: list[float]) -> float:
    value = element.extension.get("font_size")
    height = max(box[3] - box[1], 1.0)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0.5 * height:
        return float(value)
    return height


def _join(left: str, right: str, gap: float, size: float) -> str:
    """同じ行の断片をつなぐ。間が詰まっている、または境目が両方とも非 ASCII なら空白を入れない。"""
    if gap <= 0.25 * size or (left and right and not left[-1].isascii() and not right[0].isascii()):
        return left + right
    return left + " " + right


def _visual_lines(elements: list[evidence_ir.EvidenceElement]) -> list[_Line]:
    lines: list[_Line] = []
    for element in sorted(elements, key=lambda item: item.order):
        box = _bbox(element)
        size = _size(element, box)
        text = _text(element)
        last = lines[-1] if lines else None
        if last is not None:
            center = (box[1] + box[3]) / 2
            last_center = (last.top + last.bottom) / 2
            if (abs(center - last_center) <= _SAME_ROW_RATIO * max(size, last.size)
                    and box[0] >= last.right - 0.3 * size):
                last.text = _join(last.text, text, box[0] - last.right, size)
                last.members.append(element)
                last.right = max(last.right, box[2])
                last.top = max(last.top, box[3])
                last.bottom = min(last.bottom, box[1])
                last.size = max(last.size, size)
                continue
        lines.append(_Line([element], text, box[0], box[2], box[3], box[1], size))
    return lines


def _body_size(lines: list[_Line]) -> float:
    """本文の文字サイズ（文字数で重みを付けた中央値）。"""
    weighted = sorted((line.size, len(line.text)) for line in lines if line.text)
    total = sum(weight for _, weight in weighted)
    seen = 0
    for size, weight in weighted:
        seen += weight
        if seen * 2 >= total:
            return size
    return 0.0


def _classify(lines: list[_Line], body_size: float, page_box: list[float] | None) -> None:
    for line in lines:
        if page_box is not None:
            height = max(page_box[3] - page_box[1], 1.0)
            if line.bottom <= page_box[1] + _MARGIN_RATIO * height:
                line.kind = "margin_bottom"
                continue
            if line.top >= page_box[3] - _MARGIN_RATIO * height:
                line.kind = "margin_top"
                continue
        if body_size > 0 and line.size >= body_size * _HEADING_RATIO and len(line.text) <= _HEADING_MAX_CHARS:
            line.kind = "heading"


def _mark_paragraphs(lines: list[_Line]) -> None:
    body = [line for line in lines if line.kind == "body"]
    pitches = [a.top - b.top for a, b in pairwise(body) if a.top - b.top > 0]
    typical = median(pitches) if pitches else 0.0
    rights = sorted(line.right for line in body)
    right_edge = rights[int(len(rights) * 0.9)] if rights else 0.0
    previous: _Line | None = None
    for line in lines:
        if previous is not None and previous.kind == "body" and line.kind == "body":
            size = max(previous.size, line.size)
            pitch = previous.top - line.top
            ended = previous.text.rstrip().endswith(_SENTENCE_END)
            indented = line.left - previous.left >= _INDENT_RATIO * size
            short_last = previous.right < right_edge - _SHORT_LAST_LINE_RATIO * size
            line.para_start = (
                (typical > 0 and pitch >= max(_PITCH_RATIO * typical, typical + 0.3 * size))
                or (ended and (indented or short_last))
                or bool(_BULLET_RE.match(line.text))
            )
        previous = line


def _hard_break(previous: _Line, line: _Line) -> bool:
    margin = {"margin_top", "margin_bottom"}
    if previous.kind in margin or line.kind in margin:
        if previous.kind != line.kind:
            return True
    elif line.kind == "heading" and previous.kind != "heading":
        return True
    return _jumps(previous, line)


def _jumps(previous: _Line, line: _Line) -> bool:
    """段組・図の文字などで読み順が上へ戻る、または大きく空く。"""
    size = max(previous.size, line.size)
    if line.top > previous.top + _COLUMN_JUMP_RATIO * size:
        return True
    return previous.bottom - line.top > _BLOCK_GAP_RATIO * size


def _length(lines: list[_Line]) -> int:
    return sum(len(line.text) for line in lines) + max(len(lines) - 1, 0)


def _split_index(lines: list[_Line]) -> int:
    """上限を超えたとき、先頭から何行までを 1 つ目の塊にするか。0 なら切れない（見出しの直後しか候補が無い）。

    末尾の行（今加えた行）は含めない位置で切る。段落の切れ目、文末、それ以外の順に、できるだけ後ろを選ぶ。
    """
    allowed = [k for k in range(len(lines) - 1, 0, -1) if lines[k - 1].kind != "heading"]
    long_enough = [k for k in allowed if _length(lines[:k]) >= _MIN_PART_CHARS]
    for k in long_enough:
        if lines[k].para_start:
            return k
    for k in long_enough:
        if lines[k - 1].text.rstrip().endswith(_SENTENCE_END):
            return k
    return allowed[0] if allowed else 0


def _chunk_lines(lines: list[_Line]) -> list[list[_Line]]:
    chunks: list[tuple[list[_Line], bool]] = []   # (行, 直前の切れ目が段落・長さによる切れ目か)
    current: list[_Line] = []
    soft = False
    for line in lines:
        if current:
            previous = current[-1]
            if _hard_break(previous, line) and not (
                    _jumps(previous, line) and previous.kind == line.kind == "body"
                    and _length(current) < _MIN_TAIL_CHARS):
                chunks.append((current, soft))
                current, soft = [], False
            elif line.para_start and line.kind == "body" and _length(current) >= PDF_CHUNK_TARGET_CHARS:
                chunks.append((current, soft))
                current, soft = [], True
        current.append(line)
        while len(current) > 1 and _length(current) > PDF_CHUNK_MAX_CHARS:
            cut = _split_index(current)
            if cut == 0:
                break
            chunks.append((current[:cut], soft))
            current, soft = current[cut:], True
    if current:
        chunks.append((current, soft))
    merged: list[tuple[list[_Line], bool]] = []
    for chunk, is_soft in chunks:
        if (merged and is_soft and _length(chunk) < _MIN_TAIL_CHARS
                and _length(merged[-1][0]) + 1 + _length(chunk) <= PDF_CHUNK_MAX_CHARS):
            merged[-1] = (merged[-1][0] + chunk, merged[-1][1])
        else:
            merged.append((chunk, is_soft))
    return [chunk for chunk, _ in merged]


def _row_text_chunks(
    elements: list[evidence_ir.EvidenceElement],
) -> list[list[evidence_ir.EvidenceElement]]:
    """行ごとのテキストに倒した表（`row_lines`）。行のまとまりは分けず、同じ表の連続する行を上限まで 1 つにする。"""
    chunks: list[list[evidence_ir.EvidenceElement]] = []
    current: list[evidence_ir.EvidenceElement] = []
    length = 0
    for element in sorted(elements, key=lambda item: item.order):
        table = str(element.locator.object_id or "").rsplit(":", 1)[0]
        size = len(_text(element))
        if current and (str(current[-1].locator.object_id or "").rsplit(":", 1)[0] != table
                        or length + 1 + size > PDF_CHUNK_MAX_CHARS):
            chunks.append(current)
            current, length = [], 0
        length += size + (1 if current else 0)
        current.append(element)
    if current:
        chunks.append(current)
    return chunks


def plan(
    elements: list[evidence_ir.EvidenceElement],
    page_boxes: dict[int, list[float]],
    *,
    max_span_chars: int,
) -> list[PdfChunkGroup]:
    """まとめる対象の `positioned_text`（呼び出し側が選んだもの）から、チャンクごとの要素の組を返す。

    1 行が `max_span_chars` を超える要素は対象から外す（従来の長文分割に任せる）。
    """
    by_page: dict[int, list[evidence_ir.EvidenceElement]] = {}
    for element in elements:
        if element.locator.page is not None and _text(element).strip():
            by_page.setdefault(element.locator.page, []).append(element)
    pages: dict[int, list[_Line]] = {}
    rows: dict[int, list[evidence_ir.EvidenceElement]] = {}
    for page, items in by_page.items():
        rows[page] = [item for item in items
                      if item.extension.get("row_lines") and len(_text(item)) <= max_span_chars]
        lines = [line for line in _visual_lines([item for item in items if not item.extension.get("row_lines")])
                 if len(line.text) <= max_span_chars]
        pages[page] = lines
    all_lines = [line for lines in pages.values() for line in lines]
    body_size = _body_size([line for line in all_lines if line.text])
    groups: list[PdfChunkGroup] = []
    heading: str | None = None
    for page in sorted(set(pages) | set(rows)):
        lines = pages.get(page, [])
        _classify(lines, body_size, page_boxes.get(page))
        _mark_paragraphs(lines)
        headings_above = [line for line in lines if line.kind == "heading"]
        page_groups: list[PdfChunkGroup] = []
        margin: list[_Line] = []
        for chunk in _chunk_lines(lines):
            first = chunk[0]
            if first.kind.startswith("margin"):
                margin.extend(chunk)
                continue
            members = [member for line in chunk for member in line.members]
            texts = [line.text for line in chunk]
            carried = None if first.kind == "heading" else heading
            page_groups.append(PdfChunkGroup(members, texts, carried))
            heading_lines = [line for line in chunk if line.kind == "heading"]
            if heading_lines:
                heading = heading_lines[-1].text[:_HEADING_MAX_CHARS]
        if margin:
            page_groups.append(PdfChunkGroup(
                [member for line in margin for member in line.members], [line.text for line in margin]))
        for chunk_elements in _row_text_chunks(rows.get(page, [])):
            top = max(_bbox(item)[3] for item in chunk_elements)
            above = [line for line in headings_above if line.bottom >= top]
            context = above[-1].text[:_HEADING_MAX_CHARS] if above else None
            page_groups.append(PdfChunkGroup(chunk_elements, [_text(item) for item in chunk_elements], context))
        groups.extend(page_groups)
    return groups


@dataclass(frozen=True)
class PdfDocumentContext:
    """原本から確かめられた文書の題名・文書番号・版（無いものは None）。"""
    title: str | None = None
    number: str | None = None
    version: str | None = None


def comparable_text(text: str) -> str:
    """突き合わせ用（NFKC・空白除去）。"""
    return "".join(unicodedata.normalize("NFKC", text).split())


def _clean_info_title(raw: str | None) -> str | None:
    title = " ".join(str(raw or "").split())
    if not title or len(title) > _INFO_TITLE_MAX_CHARS or _PLACEHOLDER_TITLE_RE.match(title):
        return None
    return title


def _largest_heading(lines: list[_Line], body_size: float, later_max: float,
                     page_box: list[float] | None) -> str | None:
    """1 ページ目で最も大きい文字の行（本文の 1.2 倍以上・2 ページ目以降のどの行より大きい・ページの上半分・
    連続する 2 行まで・80 字まで・章や表の番号で始まらない）。同じ大きさの行が離れて複数あれば None。"""
    body = [line for line in lines if line.kind not in {"margin_top", "margin_bottom"} and line.text.strip()]
    if not body or body_size <= 0:
        return None
    top = max(line.size for line in body)
    if top < body_size * _TITLE_RATIO or top <= later_max:
        return None
    picked = [index for index, line in enumerate(body) if line.size >= top - 0.01]
    if len(picked) > _TITLE_MAX_LINES or picked != list(range(picked[0], picked[0] + len(picked))):
        return None
    text = " ".join(body[index].text.strip() for index in picked)
    if (len(text) > _TITLE_MAX_CHARS or not any(ch.isalpha() for ch in text)
            or any(ch in text for ch in "\r\n\t") or _SECTION_NUMBER_RE.match(text)):
        return None
    if page_box is not None and body[picked[0]].bottom < (page_box[1] + page_box[3]) / 2:
        return None
    return text


def _single(values: list[str]) -> str | None:
    distinct = list(dict.fromkeys(values))
    return distinct[0] if len(distinct) == 1 else None


def document_context(
    elements: list[evidence_ir.EvidenceElement],
    page_boxes: dict[int, list[float]],
    info_title: str | None,
) -> PdfDocumentContext:
    """文書全体に当てはまる題名・文書番号・版を、1 ページ目の文字と文書情報の Title から決める。

    題名: 文書情報の Title が 1 ページ目の文字に現れればそれを使う。そうでなければ 1 ページ目で最も大きい文字の行
    （文書のほかのページのどの行より大きいもの）を、Title が無いか Title に含まれるときだけ使う。それ以外は付けない。
    文書番号・版: 1 ページ目の「文書番号:」「版:」などの名前付きの値だけを使い、値が 2 つ以上に割れたら付けない。
    守ること: ファイル名からは決めない。章・節の見出しは使わない。
    """
    texts = [element for element in elements if element.locator.page is not None and _text(element).strip()
             and not element.extension.get("row_lines")]
    all_lines = _visual_lines(texts)
    body_size = _body_size([line for line in all_lines if line.text])
    first = _visual_lines([element for element in texts if element.locator.page == 1])
    if not first:
        return PdfDocumentContext()
    _classify(first, body_size, page_boxes.get(1))
    page_text = comparable_text("".join(line.text for line in first))
    info = _clean_info_title(info_title)
    later = [line.size for line in all_lines if line.members[0].locator.page != 1 and line.text.strip()]
    heading = _largest_heading(first, body_size, max(later, default=0.0), page_boxes.get(1))
    title = None
    if info and comparable_text(info) in page_text:
        title = info
    elif heading and (not info or comparable_text(heading) in comparable_text(info)):
        title = heading
    number = _single([match.group(1) for line in first for match in _DOC_NUMBER_RE.finditer(line.text)])
    version = _single([comparable_text(match.group(1)) for line in first for match in _VERSION_RE.finditer(line.text)])
    return PdfDocumentContext(title, number, version)


def eligible(element: evidence_ir.EvidenceElement) -> bool:
    """まとめの対象にしてよい要素（可視・有効な位置付き文字）。"""
    return (element.type == "positioned_text" and element.visibility == "visible"
            and element.lifecycle == "active" and bool(_text(element).strip()))

