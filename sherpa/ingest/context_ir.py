"""Evidence IRから検索用の文書・領域・record文脈を決定的に組み立てる。

Evidence IRは原値と原本位置の証拠であり、このmoduleはそれを変更しない。XLSXでは横方向の
結合見出しから論理領域を分け、DOCXでは見出し階層と表境界、PPTXではslideとgraphicFrame境界を保つ。各形式で領域ごとのheader row、
header path、業務record keyを推定する。曖昧な小表はheaderを断定せず``coordinate_fallback``へ縮退する。

全cellを別objectへ複製すると巨大表でmemoryを再び増幅するため、Context IRが保持するのは領域定義と
row単位のrecord metadataだけである。field本体はrendererがEvidence elementを参照する。
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import evidence_ir


CONTEXT_IR_SCHEMA_VERSION = "context-ir-v1alpha2"
CONTEXT_ANALYZER_VERSION = "xlsx-context-rules-v4"
DOCX_CONTEXT_ANALYZER_VERSION = "docx-context-rules-v1"
PPTX_CONTEXT_ANALYZER_VERSION = "pptx-context-rules-v1"
PDF_CONTEXT_ANALYZER_VERSION = "pdf-context-rules-v1"
IDENTIFIER_ROLE_ANALYZER_VERSION = "identifier-role-rules-v1"
IDENTIFIER_METADATA_SCHEMA_VERSION = "identifier-metadata-v1"
IDENTIFIER_MAX_CHARS = 256
# Elasticsearchのnested object数とretrieval時の監査量を、原本サイズに依存せず有界にする。
# 値を変える場合はRAG signature/index contractも変わるため、既存世代へ混在させない。
IDENTIFIER_MAX_MENTIONS_PER_CHUNK = 128
_KEY_WORDS = ("id", "コード", "番号", "連番", "キー", "no", "ｎｏ")
_ID_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_.:/-]{3,}$")
_NUMBER_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
_IDENTIFIER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9Ａ-Ｚａ-ｚ０-９_＿])"
    r"(?:[A-Za-z0-9Ａ-Ｚａ-ｚ０-９]"
    r"[A-Za-z0-9Ａ-Ｚａ-ｚ０-９_.:/\-．／－：＿]{1,}"
    r"[A-Za-z0-9Ａ-Ｚａ-ｚ０-９])"
    r"(?![A-Za-z0-9Ａ-Ｚａ-ｚ０-９_＿])"
)
_REFERENCE_LABEL_MARKERS = (
    "呼び出し先", "呼出先", "真遷移先", "偽遷移先", "遷移先", "遷移元", "発生段落", "参照先", "参照元",
    "リンク先", "接続先", "転送先", "依存先", "参照", "親id", "子id", "関連id",
)
_INLINE_REFERENCE_MARKER_RE = re.compile(
    r"(?:^|[\s、,;\uff1b。．.!?！？(\[【])"
    r"(?P<marker>真遷移先|偽遷移先|遷移先|発生段落|参照先|参照|呼び出し先|呼出先|真|偽)"
    r"\s*[:：=＝]\s*",
)
_IDENTITY_LABEL_RE = re.compile(
    r"^(?:段落|処理|エラー|画面|項目|レコード|テーブル|機能|業務|手続|帳票|api|ジョブ|バッチ|メッセージ|トランザクション)"
    r"(?:id|識別子)$"
)
_CONTINUATION_MAX_ROW_GAP = 3


@dataclass(frozen=True)
class RecordKey:
    label: str
    value: str
    evidence_id: str


@dataclass(frozen=True)
class IdentifierMention:
    """原本上の識別子出現と、その根拠付き役割。

    ``value`` は原値の部分文字列を一切変更しない。``normalized_value`` は候補検索専用であり、
    回答支持の完全一致判定には使ってはならない。明示的なfield labelやinline markerのない
    出現は推測でidentityへ上げず``unclassified``とする。
    """

    value: str
    normalized_value: str
    role: str
    role_basis: str
    field_label: str
    evidence_id: str
    locator: dict[str, Any]
    text_span: dict[str, int]


@dataclass(frozen=True)
class IdentifierMentionBatch:
    """有界に保持した識別子出現と、保持しなかった件数を分離する。"""

    mentions: tuple[IdentifierMention, ...]
    mention_count: int
    overflow_count: int

    @property
    def complete(self) -> bool:
        return self.overflow_count == 0


@dataclass(frozen=True)
class ContextRecord:
    record_id: str
    region_id: str
    row: int
    keys: tuple[RecordKey, ...]
    identifiers: tuple[str, ...]
    identifier_mentions: tuple[IdentifierMention, ...]
    confidence: float
    identifier_metadata_complete: bool = True
    identifier_mention_count: int = 0
    identifier_mention_overflow_count: int = 0
    identifier_mention_limit: int = IDENTIFIER_MAX_MENTIONS_PER_CHUNK


@dataclass(frozen=True)
class ContextRegion:
    region_id: str
    table_id: str
    sheet: str | None
    start_column: int
    end_column: int
    start_row: int
    end_row: int
    title: str | None
    header_row: int | None
    header_paths: tuple[tuple[int, tuple[str, ...]], ...]
    mode: str
    confidence: float
    part: str | None = None
    table_object_id: str | int | None = None
    section_path: tuple[str, ...] = ()
    slide: int | None = None
    page: int | None = None

    def header_for(self, column: int) -> tuple[str, ...]:
        return dict(self.header_paths).get(column, ())


@dataclass
class ContextIR:
    schema_version: str
    analyzer_version: str
    source_hash: str
    source_name: str
    document_titles: dict[str, str] = field(default_factory=dict)
    regions: list[ContextRegion] = field(default_factory=list)
    records: list[ContextRecord] = field(default_factory=list)
    related_records: dict[str, tuple[str, ...]] = field(default_factory=dict)
    element_sections: dict[str, tuple[str, ...]] = field(default_factory=dict)
    element_identifier_mentions: dict[str, tuple[IdentifierMention, ...]] = field(default_factory=dict)

    def records_by_region_row(self) -> dict[tuple[str, int], ContextRecord]:
        return {(record.region_id, record.row): record for record in self.records}


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def normalize_identifier_candidate(value: str) -> str:
    """識別子候補検索用のみにNFKC/casefoldする。原値の支持判定には使わない。"""
    return unicodedata.normalize("NFKC", value).casefold()


def _identifier_locator(locator: evidence_ir.Locator) -> dict[str, Any]:
    """extension全体を複製せず、原本へ戻るための決定的locatorだけを保持する。"""
    out: dict[str, Any] = {"part": locator.part}
    for key in ("sheet", "slide", "page", "cell_range", "object_id", "bbox"):
        value = getattr(locator, key)
        if value is not None:
            out[key] = value
    return out


def _inline_reference_marker(text_before: str) -> str | None:
    """次の候補を含む、有界なinline参照listの明示markerを返す。"""
    for match in reversed(list(_INLINE_REFERENCE_MARKER_RE.finditer(text_before))):
        tail = text_before[match.end():]
        if not tail:
            return match.group("marker")
        cursor = 0
        candidate_count = 0
        valid = True
        for candidate in _IDENTIFIER_TOKEN_RE.finditer(tail):
            if re.fullmatch(r"[\s,，、]*", tail[cursor:candidate.start()]) is None:
                valid = False
                break
            candidate_count += 1
            cursor = candidate.end()
        if (
            valid
            and candidate_count > 0
            and re.fullmatch(r"[\s,，、]*", tail[cursor:]) is not None
        ):
            return match.group("marker")
    return None


def _identifier_role(field_label: str, text_before: str, *, allow_record_identity: bool) -> tuple[str, str]:
    normalized_label = normalize_identifier_candidate(field_label).strip()
    compact_label = re.sub(r"\s+", "", normalized_label)
    for marker in _REFERENCE_LABEL_MARKERS:
        if marker in compact_label:
            return "reference", f"{IDENTIFIER_ROLE_ANALYZER_VERSION}:field_reference:{marker}"

    # occurrence直前の明示markerは、周囲のidentity field labelより強い根拠である。
    inline_marker = _inline_reference_marker(text_before)
    if inline_marker is not None:
        return "reference", f"{IDENTIFIER_ROLE_ANALYZER_VERSION}:inline_reference_marker:{inline_marker}"

    leaf_label = re.split(r"\s*>\s*|[/／・]", normalized_label)[-1].strip()
    leaf_label = re.sub(r"\s+", "", leaf_label)
    identity_label = leaf_label in {"id", "識別子"} or _IDENTITY_LABEL_RE.fullmatch(leaf_label)
    if identity_label and allow_record_identity:
        return "record_identity", f"{IDENTIFIER_ROLE_ANALYZER_VERSION}:record_key_identity:{leaf_label}"

    if identity_label:
        return "unclassified", f"{IDENTIFIER_ROLE_ANALYZER_VERSION}:identity_label_not_record_key"
    if not normalized_label:
        return "unclassified", f"{IDENTIFIER_ROLE_ANALYZER_VERSION}:missing_field_label"
    return "unclassified", f"{IDENTIFIER_ROLE_ANALYZER_VERSION}:unsupported_field_label"


def identifier_mentions_with_metadata(
    value: Any,
    *,
    field_label: str,
    evidence_id: str,
    locator: evidence_ir.Locator | dict[str, Any],
    text_offset: int = 0,
    allow_record_identity: bool = False,
    match_start: int | None = None,
    match_end: int | None = None,
    max_mentions: int = IDENTIFIER_MAX_MENTIONS_PER_CHUNK,
) -> IdentifierMentionBatch:
    """原値文字列から識別子候補を抽出し、明示根拠だけで役割を付ける。

    数字もseparatorも無い通常の英単語は候補にしない。一方、``P-0005``、``E-X023-01``、
    ``ACCOUNT_NO``のような業務識別子は原表記とoffsetを保って抽出する。候補数は最後まで数えるが、
    objectとして保持するのは``max_mentions``件までとし、超過を明示する。

    ``match_start``/``match_end``を指定した場合もtoken照合は原値全体で行い、候補の開始位置で
    対象chunkを決める。これにより巨大cellの分割境界をまたぐ識別子を失わない。
    """
    if isinstance(max_mentions, bool) or not isinstance(max_mentions, int) or max_mentions < 0:
        raise ValueError("max_mentions must be a non-negative integer")
    # ``_text``のstripはheader判定には便利だが、原値内offsetをずらす。ここでは文字列原値をそのまま使う。
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    range_start = 0 if match_start is None else max(0, match_start)
    range_end = len(text) if match_end is None else min(len(text), max(range_start, match_end))
    compact_locator = locator if isinstance(locator, dict) else _identifier_locator(locator)
    mentions: list[IdentifierMention] = []
    mention_count = 0
    for match in _IDENTIFIER_TOKEN_RE.finditer(text):
        if match.start() < range_start or match.start() >= range_end:
            continue
        candidate = match.group(0)
        if len(candidate) > IDENTIFIER_MAX_CHARS:
            continue
        normalized_candidate = normalize_identifier_candidate(candidate)
        if not any(char.isalpha() for char in candidate):
            continue
        if not (any(char.isdigit() for char in candidate) or any(char in "_-/:" for char in normalized_candidate)):
            continue
        mention_count += 1
        if len(mentions) >= max_mentions:
            continue
        if "/" in normalized_candidate:
            # slashはidentifier内部文字と列挙separatorを区別できないためidentityへ昇格しない。
            role = "unclassified"
            role_basis = f"{IDENTIFIER_ROLE_ANALYZER_VERSION}:ambiguous_slash_compound"
        else:
            role, role_basis = _identifier_role(
                field_label, text[:match.start()], allow_record_identity=allow_record_identity,
            )
        mentions.append(IdentifierMention(
            value=candidate,
            normalized_value=normalized_candidate,
            role=role,
            role_basis=role_basis,
            field_label=field_label,
            evidence_id=evidence_id,
            locator=dict(compact_locator),
            text_span={"start": text_offset + match.start(), "end": text_offset + match.end()},
        ))
    return IdentifierMentionBatch(
        mentions=tuple(mentions),
        mention_count=mention_count,
        overflow_count=max(0, mention_count - len(mentions)),
    )


def identifier_mentions(
    value: Any,
    *,
    field_label: str,
    evidence_id: str,
    locator: evidence_ir.Locator | dict[str, Any],
    text_offset: int = 0,
    allow_record_identity: bool = False,
) -> tuple[IdentifierMention, ...]:
    """互換用の有界tuple API。完全性が必要な索引生成はmetadata APIを使う。"""
    return identifier_mentions_with_metadata(
        value,
        field_label=field_label,
        evidence_id=evidence_id,
        locator=locator,
        text_offset=text_offset,
        allow_record_identity=allow_record_identity,
    ).mentions


def _dedupe_identifier_mentions(items: list[IdentifierMention]) -> tuple[IdentifierMention, ...]:
    seen: set[str] = set()
    unique: list[IdentifierMention] = []
    for item in items:
        key = json.dumps({
            "value": item.value,
            "normalized_value": item.normalized_value,
            "role": item.role,
            "role_basis": item.role_basis,
            "field_label": item.field_label,
            "evidence_id": item.evidence_id,
            "locator": item.locator,
            "text_span": item.text_span,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return tuple(unique)


def _data_like_count(values: list[str]) -> int:
    return sum(bool(_ID_RE.fullmatch(value) or _NUMBER_RE.fullmatch(value)) for value in values)


def _row_column(cell: evidence_ir.EvidenceElement) -> tuple[int | None, int | None]:
    row = cell.locator.extension.get("row")
    column = cell.locator.extension.get("column")
    return (row if isinstance(row, int) else None, column if isinstance(column, int) else None)


def _span(cell: evidence_ir.EvidenceElement, name: str) -> int:
    value = cell.locator.extension.get(name, 1)
    return value if isinstance(value, int) and value > 0 else 1


def _sheet_titles(cells: list[evidence_ir.EvidenceElement]) -> dict[str, str]:
    candidates: dict[str, list[tuple[int, int, int, str]]] = defaultdict(list)
    for cell in cells:
        row, column = _row_column(cell)
        value = _text(cell.value)
        sheet = cell.locator.sheet
        width = _span(cell, "column_span")
        if sheet and row is not None and column is not None and value and width >= 2:
            candidates[sheet].append((row, -width, column, value))
    return {
        sheet: min(values)[3]
        for sheet, values in candidates.items()
    }


def _docx_outline(ir: evidence_ir.EvidenceIR) -> tuple[str | None, dict[str, tuple[str, ...]]]:
    """DOCX bodyの物理順から、見出し階層と各要素の所属節を作る。

    DrawingMLを後段で追加したobjectはbody orderと同じ座標軸を持たない。それを無理に文章の間へ
    差し込まず、現行Document IRからadaptした本文要素だけでoutlineを確定する。
    """
    body = [
        element for element in ir.elements
        if element.parent_id is None
        and element.extension.get("origin") == "document-ir-v2-adapter"
        and element.type in {"heading", "paragraph", "table"}
    ]
    stack: list[str] = []
    by_element: dict[str, tuple[str, ...]] = {}
    document_title: str | None = None
    for element in sorted(body, key=lambda item: item.order):
        if element.type == "heading" and (text := _text(element.value)):
            source_map = element.locator.extension.get("document_ir_source_map", {})
            raw_level = source_map.get("level", 1) if isinstance(source_map, dict) else 1
            level = raw_level if isinstance(raw_level, int) and raw_level > 0 else 1
            stack = stack[:level - 1]
            while len(stack) < level - 1:
                stack.append("")
            stack.append(text)
            if document_title is None:
                document_title = text
        by_element[element.element_id] = tuple(item for item in stack if item)
    return document_title, by_element


def _pptx_titles(ir: evidence_ir.EvidenceIR) -> dict[str, str]:
    """各slideのタイトルshapeを、原本上端の位置と名称から決定的に選ぶ。"""
    candidates: dict[int, list[tuple[float, float, int, str]]] = defaultdict(list)
    for element in ir.elements:
        if element.type != "shape" or element.extension.get("origin") == "document-ir-v2-adapter":
            continue
        slide = element.locator.slide
        text = _text(element.value)
        if slide is None or not text:
            continue
        bbox = element.locator.bbox or [0, 0, 0, 0]
        name = _text(element.extension.get("name"))
        name_priority = 0 if "タイトル" in name else 1
        candidates[slide].append((name_priority, float(bbox[1]), element.order, text))
    return {
        f"slide:{slide}": min(values)[3]
        for slide, values in candidates.items()
    }


def _pdf_titles(ir: evidence_ir.EvidenceIR) -> dict[str, str]:
    """各page上端のpositioned textを文書タイトル候補にする。"""
    candidates: dict[int, list[tuple[float, float, int, str]]] = defaultdict(list)
    for element in ir.elements:
        if element.type != "positioned_text" or element.locator.page is None:
            continue
        text = _text(element.value)
        if not text:
            continue
        bbox = element.locator.bbox or [0, 0, 0, 0]
        candidates[element.locator.page].append((-float(bbox[3]), float(bbox[0]), element.order, text))
    return {f"page:{page}": min(values)[3] for page, values in candidates.items()}


def _region_specs(
    table_id: str,
    cells: list[evidence_ir.EvidenceElement],
) -> list[tuple[int, int, int | None, str | None]]:
    positions = [(_row_column(cell), cell) for cell in cells if _row_column(cell)[0] is not None]
    min_row = min(position[0] for position, _ in positions)
    max_row = max(position[0] for position, _ in positions)
    min_column = min(position[1] for position, _ in positions)
    max_column = max(position[1] + _span(cell, "column_span") - 1 for position, cell in positions)
    width = max_column - min_column + 1
    anchors_by_row: dict[int, list[evidence_ir.EvidenceElement]] = defaultdict(list)
    for (row, column), cell in positions:
        if row is None or column is None or row > min_row + 4 or not _text(cell.value):
            continue
        if _span(cell, "column_span") >= 2:
            anchors_by_row[row].append(cell)
    title_row = None
    anchors: list[evidence_ir.EvidenceElement] = []
    for row in sorted(anchors_by_row):
        row_anchors = sorted(anchors_by_row[row], key=lambda cell: _row_column(cell)[1] or 0)
        covered = sum(_span(cell, "column_span") for cell in row_anchors)
        if len(row_anchors) >= 2 or covered >= max(2, int(width * 0.6)):
            title_row = row
            anchors = row_anchors
            break
    if not anchors:
        return [(min_column, max_column, None, None)]
    specs: list[tuple[int, int, int | None, str | None]] = []
    for index, anchor in enumerate(anchors):
        _, anchor_column = _row_column(anchor)
        if anchor_column is None:
            continue
        start = min_column if index == 0 else anchor_column
        end = (anchors[index + 1].locator.extension.get("column", max_column + 1) - 1
               if index + 1 < len(anchors) else max_column)
        specs.append((start, end, title_row, _text(anchor.value)))
    return specs or [(min_column, max_column, None, None)]


def _cells_in_region(
    cells: list[evidence_ir.EvidenceElement],
    start_column: int,
    end_column: int,
) -> list[evidence_ir.EvidenceElement]:
    return [
        cell for cell in cells
        if (column := _row_column(cell)[1]) is not None and start_column <= column <= end_column
    ]


def _header_row(
    cells: list[evidence_ir.EvidenceElement],
    start_column: int,
    end_column: int,
    title_row: int | None,
    max_row: int,
) -> tuple[int | None, float]:
    min_row = min(row for cell in cells if (row := _row_column(cell)[0]) is not None)
    start = (title_row + 1) if title_row is not None else min_row
    stop = min(max_row - 1, start + 4)
    candidates: list[tuple[int, float, int]] = []
    for row in range(start, stop + 1):
        row_cells = [cell for cell in cells if _row_column(cell)[0] == row and _text(cell.value)]
        if not row_cells:
            continue
        # 縦結合labelで文章を束ねる小表をheader表と誤認しない。
        if any(_span(cell, "row_span") > 1 for cell in row_cells):
            continue
        texts = [_text(cell.value) for cell in row_cells]
        if any(len(text) > 40 or text.endswith(("。", "！", "？")) or "\n" in text for text in texts):
            continue
        later = any(
            (_row_column(cell)[0] or 0) > row and _text(cell.value)
            for cell in cells
        )
        if not later:
            continue
        nonempty = len(row_cells)
        width = end_column - start_column + 1
        if nonempty < min(2, width):
            continue
        string_ratio = sum(isinstance(cell.value, str) for cell in row_cells) / nonempty
        density = nonempty / width
        score = density * 0.75 + string_ratio * 0.25
        candidates.append((nonempty, score, -row))
    if not candidates:
        return None, 0.0
    nonempty, score, neg_row = max(candidates)
    confidence = min(1.0, score * (1.0 if nonempty >= 2 else 0.7))
    if confidence < 0.55:
        return None, 0.0
    return -neg_row, round(confidence, 3)


def _covering_value(cells: list[evidence_ir.EvidenceElement], row: int, column: int) -> str | None:
    candidates = []
    for cell in cells:
        cell_row, cell_column = _row_column(cell)
        if cell_row != row or cell_column is None or not _text(cell.value):
            continue
        if cell_column <= column < cell_column + _span(cell, "column_span"):
            candidates.append((cell_column, _text(cell.value)))
    return min(candidates)[1] if candidates else None


def _header_paths(
    cells: list[evidence_ir.EvidenceElement],
    start_column: int,
    end_column: int,
    title_row: int | None,
    header_row: int,
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    paths: list[tuple[int, tuple[str, ...]]] = []
    for column in range(start_column, end_column + 1):
        values: list[str] = []
        if title_row is not None:
            for row in range(title_row + 1, header_row):
                value = _covering_value(cells, row, column)
                # 中間行は、複数列を束ねる見出しだけをheader pathにする。個別のkey/value metadataは除外。
                if value:
                    owner = next((cell for cell in cells if _row_column(cell)[0] == row
                                  and _row_column(cell)[1] is not None
                                  and (_row_column(cell)[1] or 0) <= column
                                  < (_row_column(cell)[1] or 0) + _span(cell, "column_span")
                                  and _text(cell.value) == value), None)
                    if owner is not None and _span(owner, "column_span") >= 2:
                        values.append(value)
        direct = _covering_value(cells, header_row, column)
        if direct:
            values.append(direct)
        paths.append((column, tuple(dict.fromkeys(values))))
    return tuple(paths)


def _key_score(label: str, value: str, column: int) -> tuple[int, int]:
    normalized = label.casefold()
    keyword = max((100 - index * 10 for index, word in enumerate(_KEY_WORDS) if word in normalized), default=0)
    id_bonus = 60 if _ID_RE.fullmatch(value) else 0
    return keyword + id_bonus, -column


def _record_for(
    source_hash: str,
    region: ContextRegion,
    row: int,
    row_cells: list[evidence_ir.EvidenceElement],
) -> ContextRecord:
    candidates: list[tuple[tuple[int, int], RecordKey]] = []
    identifiers: list[str] = []
    for cell in sorted(row_cells, key=lambda item: _row_column(item)[1] or 0):
        _, column = _row_column(cell)
        if column is None:
            continue
        value = _text(cell.value)
        if not value:
            continue
        header = " > ".join(region.header_for(column))
        if _ID_RE.fullmatch(value):
            identifiers.append(value)
        score = _key_score(header, value, column)
        if score[0] > 0:
            candidates.append((score, RecordKey(header or "識別子", value, cell.element_id)))
    candidates.sort(key=lambda item: item[0], reverse=True)
    keys: list[RecordKey] = []
    seen_values: set[str] = set()
    for _, key in candidates:
        if key.value in seen_values:
            continue
        seen_values.add(key.value)
        keys.append(key)
        if len(keys) == 2:
            break
    key_evidence_ids = {key.evidence_id for key in keys}
    mentions: list[IdentifierMention] = []
    mention_count = 0
    for cell in sorted(row_cells, key=lambda item: _row_column(item)[1] or 0):
        column = _row_column(cell)[1]
        if column is None:
            continue
        batch = identifier_mentions_with_metadata(
            cell.value,
            field_label=" > ".join(region.header_for(column)),
            evidence_id=cell.element_id,
            locator=cell.locator,
            allow_record_identity=cell.element_id in key_evidence_ids,
            max_mentions=max(0, IDENTIFIER_MAX_MENTIONS_PER_CHUNK - len(mentions)),
        )
        mention_count += batch.mention_count
        mentions.extend(batch.mentions)
    overflow_count = max(0, mention_count - len(mentions))
    record_id = _stable_id("context-record", source_hash, region.region_id, row)
    return ContextRecord(
        record_id=record_id,
        region_id=region.region_id,
        row=row,
        keys=tuple(keys),
        identifiers=tuple(dict.fromkeys(identifiers)),
        identifier_mentions=tuple(mentions),
        confidence=region.confidence if keys else round(region.confidence * 0.7, 3),
        identifier_metadata_complete=overflow_count == 0,
        identifier_mention_count=mention_count,
        identifier_mention_overflow_count=overflow_count,
    )


def _continuation_predecessor(
    regions: list[ContextRegion],
    cells: list[evidence_ir.EvidenceElement],
    *,
    cells_by_table: dict[str, list[evidence_ir.EvidenceElement]] | None = None,
    sheet: str | None,
    start_column: int,
    end_column: int,
    min_row: int,
    title: str | None,
    detected_header_row: int | None,
) -> ContextRegion | None:
    """空白行で別tableへ分断された、見出しを再掲しない縦続き領域を見つける。

    単に同じ列位置というだけでは別表を誤結合するため、現在segmentの先頭行がheaderとして検出され、かつ
    数値/業務IDを2つ以上含む「data rowらしい」ことに加え、直前行からの連番とID参照の双方が連続する場合
    だけ直前header regionを継承する。独立した根拠が揃わない曖昧な領域は、誤ったheaderを付けずfallbackする。
    """
    if title is not None or detected_header_row != min_row:
        return None
    first_row_values = [
        _text(cell.value)
        for cell in cells
        if _row_column(cell)[0] == min_row and _text(cell.value)
    ]
    data_like = _data_like_count(first_row_values)
    if data_like < 2:
        return None
    candidates = [
        region for region in regions
        if region.sheet == sheet
        and region.mode == "header_record"
        and region.title is not None
        and region.start_column == start_column
        and region.end_column >= end_column
        and 0 < min_row - region.end_row <= _CONTINUATION_MAX_ROW_GAP
    ]
    if not candidates or not cells_by_table:
        return None
    predecessor = max(candidates, key=lambda region: region.end_row)
    previous_cells = cells_by_table.get(predecessor.table_id, [])
    previous_values = {
        column: _text(cell.value)
        for cell in previous_cells
        if _row_column(cell)[0] == predecessor.end_row
        and (column := _row_column(cell)[1]) is not None
        and start_column <= column <= end_column
        and _text(cell.value)
    }
    current_values = {
        column: _text(cell.value)
        for cell in cells
        if _row_column(cell)[0] == min_row
        and (column := _row_column(cell)[1]) is not None
        and start_column <= column <= end_column
        and _text(cell.value)
    }

    ordinal_continues = any(
        previous.lstrip("+-").isdigit()
        and current.lstrip("+-").isdigit()
        and int(current) == int(previous) + 1
        for column, current in current_values.items()
        if (previous := previous_values.get(column)) is not None
    )
    previous_identifiers = {value for value in previous_values.values() if _ID_RE.fullmatch(value)}
    current_identifiers = {value for value in current_values.values() if _ID_RE.fullmatch(value)}
    identifier_bridge = bool(previous_identifiers & current_identifiers)
    return predecessor if ordinal_continues and identifier_bridge else None


def build(ir: evidence_ir.EvidenceIR, *, source_name: str) -> ContextIR:
    """XLSX/DOCX/PPTX Evidence IRから小さなContext IRを作る。他形式は文書contextだけを返す。"""
    # 旧Officeは前段変換後のlocatorを持つ。原本identity（xls/doc/ppt）は保ったまま、context規則だけ
    # normalized OOXML形式へ合わせる。変換来歴とlocator_basisはEvidence側に明示される。
    source_type = {"xls": "xlsx", "doc": "docx", "ppt": "pptx"}.get(
        ir.source.file_type, ir.source.file_type)
    cells = [element for element in ir.elements if element.type == "cell"]
    docx_title, docx_outline = _docx_outline(ir) if source_type == "docx" else (None, {})
    document_titles = _sheet_titles(cells)
    if docx_title:
        document_titles[""] = docx_title
    if source_type == "pptx":
        document_titles.update(_pptx_titles(ir))
    if source_type == "pdf":
        document_titles.update(_pdf_titles(ir))
    analyzer_version = {
        "docx": DOCX_CONTEXT_ANALYZER_VERSION,
        "pptx": PPTX_CONTEXT_ANALYZER_VERSION,
        "pdf": PDF_CONTEXT_ANALYZER_VERSION,
    }.get(source_type, CONTEXT_ANALYZER_VERSION)
    context = ContextIR(
        schema_version=CONTEXT_IR_SCHEMA_VERSION,
        analyzer_version=analyzer_version,
        source_hash=ir.source.content_hash,
        source_name=Path(source_name).name,
        document_titles=document_titles,
        element_sections=docx_outline,
    )
    if source_type not in {"xlsx", "docx", "pptx", "pdf"}:
        context.element_identifier_mentions = {
            element.element_id: mentions
            for element in ir.elements
            if (mentions := identifier_mentions(
                element.value,
                field_label="",
                evidence_id=element.element_id,
                locator=element.locator,
            ))
        }
        return context
    by_parent: dict[str, list[evidence_ir.EvidenceElement]] = defaultdict(list)
    for cell in cells:
        if cell.parent_id:
            by_parent[cell.parent_id].append(cell)
    tables = {
        element.element_id: element
        for element in ir.elements
        if element.type == "table"
    }

    def _table_outline(table_id: str) -> tuple[str, ...]:
        seen: set[str] = set()
        current = tables.get(table_id)
        while current is not None and current.element_id not in seen:
            seen.add(current.element_id)
            if current.element_id in docx_outline:
                return docx_outline[current.element_id]
            current = tables.get(current.parent_id or "")
        return ()

    for table_id, table_cells in sorted(by_parent.items(), key=lambda item: min(cell.order for cell in item[1])):
        table = tables.get(table_id)
        rows = [row for cell in table_cells if (row := _row_column(cell)[0]) is not None]
        if not rows:
            continue
        min_row, max_row = min(rows), max(rows)
        for start_column, end_column, title_row, title in _region_specs(table_id, table_cells):
            if source_type in {"pptx", "pdf"} and title is None and table is not None:
                title = _text(table.extension.get("name")) or None
            region_cells = _cells_in_region(table_cells, start_column, end_column)
            header_row, confidence = _header_row(
                region_cells, start_column, end_column, title_row, max_row)
            sheet = region_cells[0].locator.sheet if region_cells else None
            predecessor = (
                _continuation_predecessor(
                    context.regions,
                    region_cells,
                    cells_by_table=by_parent,
                    sheet=sheet,
                    start_column=start_column,
                    end_column=end_column,
                    min_row=min_row,
                    title=title,
                    detected_header_row=header_row,
                )
                if source_type == "xlsx" else None
            )
            if predecessor is not None:
                title = predecessor.title
                header_row = predecessor.header_row
                header_paths = tuple(
                    (column, path)
                    for column, path in predecessor.header_paths
                    if start_column <= column <= end_column
                )
                confidence = min(predecessor.confidence, 0.9)
            else:
                first_row_values = [
                    _text(cell.value)
                    for cell in region_cells
                    if _row_column(cell)[0] == min_row and _text(cell.value)
                ]
                if title is None and header_row == min_row and _data_like_count(first_row_values) >= 2:
                    # 先頭data rowをheaderと誤認した可能性が高い。継続根拠もtitleもない場合は、値を
                    # column labelへ昇格させず座標fallbackにする。
                    header_row = None
                    confidence = 0.0
                header_paths = (
                    _header_paths(region_cells, start_column, end_column, title_row, header_row)
                    if header_row is not None else ()
                )
            mode = "header_record" if header_row is not None else "coordinate_fallback"
            region_id = _stable_id(
                "context-region", ir.source.content_hash, table_id, start_column, end_column, title_row, title)
            region = ContextRegion(
                region_id=region_id,
                table_id=table_id,
                sheet=sheet,
                start_column=start_column,
                end_column=end_column,
                start_row=min_row,
                end_row=max_row,
                title=title,
                header_row=header_row,
                header_paths=header_paths,
                mode=mode,
                confidence=confidence,
                part=table.locator.part if table is not None else None,
                table_object_id=table.locator.object_id if table is not None else None,
                section_path=_table_outline(table_id),
                slide=table.locator.slide if table is not None else None,
                page=table.locator.page if table is not None else None,
            )
            context.regions.append(region)
            data_start = header_row + 1 if header_row is not None else min_row
            by_row: dict[int, list[evidence_ir.EvidenceElement]] = defaultdict(list)
            for cell in region_cells:
                row, _ = _row_column(cell)
                if row is not None and row >= data_start and _text(cell.value):
                    by_row[row].append(cell)
            for row, row_cells in sorted(by_row.items()):
                context.records.append(_record_for(ir.source.content_hash, region, row, row_cells))

    region_by_id = {region.region_id: region for region in context.regions}
    by_identifier: dict[str, list[ContextRecord]] = defaultdict(list)
    for record in context.records:
        for identifier in record.identifiers:
            by_identifier[identifier].append(record)
    related: dict[str, set[str]] = defaultdict(set)
    for records in by_identifier.values():
        if len(records) < 2 or len(records) > 20:
            continue
        # v1は同じ原本行に並ぶ別論理領域だけを確定linkにする。繰返しコードや遷移先による
        # 別行linkは候補が増えやすいため、graph/LLMへ渡さず安全側に倒す。
        for record in records:
            source_region = region_by_id[record.region_id]
            for target in records:
                target_region = region_by_id[target.region_id]
                if (record.record_id != target.record_id and record.row == target.row
                        and source_region.table_id == target_region.table_id
                        and record.region_id != target.region_id):
                    related[record.record_id].add(target.record_id)
    context.related_records = {
        record_id: tuple(sorted(record_ids))
        for record_id, record_ids in sorted(related.items())
    }
    mentions_by_element: dict[str, list[IdentifierMention]] = defaultdict(list)
    for record in context.records:
        for mention in record.identifier_mentions:
            mentions_by_element[mention.evidence_id].append(mention)
    for element in ir.elements:
        if element.element_id not in mentions_by_element:
            mentions_by_element[element.element_id].extend(identifier_mentions(
                element.value,
                field_label="",
                evidence_id=element.element_id,
                locator=element.locator,
            ))
    context.element_identifier_mentions = {
        evidence_id: _dedupe_identifier_mentions(items)
        for evidence_id, items in sorted(mentions_by_element.items())
        if items
    }
    return context
