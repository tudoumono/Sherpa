"""Excel セルの原値・型付き値・数式・表示値を決定的に抽出する。

OOXML の ``<v>`` を原値の権威、openpyxl の型変換結果を型付き値の権威として分け、
対応できる主要書式だけを表示する。複雑な条件・会計・ロケール・和暦等は近似せず
``display_status=unsupported`` にする。LLM・外部通信・再計算は行わない。
設計: docs/design/rag.md「アームの選択方式（実装済みは「最初に受理した1本」）」
"""
from __future__ import annotations

import posixpath
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any

from .gc_relax import relaxed_gc
from .ooxml import excel as _ooxml_excel
from xml.etree import ElementTree as ET


EXCEL_DISPLAY_PROFILE = "excel-display-linux-v1"

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DOC_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_CELL_REF_RE = re.compile(r"^[A-Z]{1,3}[1-9][0-9]*$")
_NUMERIC_TOKEN_RE = re.compile(r"[0#?]")


@dataclass(frozen=True)
class DisplayResult:
    value: str | None
    source: str
    status: str
    reason: str | None


def _number_or_text(value: str | None) -> Any:
    if value in (None, ""):
        return None
    assert value is not None
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def _json_scalar(value: Any) -> Any:
    """Python 固有型を決定的な JSON 値へ変換する。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat(timespec="seconds")
    if isinstance(value, timedelta):
        return value.total_seconds()
    return str(value)


def _typed_value(value: Any) -> dict[str, Any]:
    if value is None:
        kind = "blank"
    elif isinstance(value, bool):
        kind = "boolean"
    elif isinstance(value, datetime):
        kind = "datetime"
    elif isinstance(value, date):
        kind = "date"
    elif isinstance(value, time):
        kind = "time"
    elif isinstance(value, timedelta):
        kind = "duration_seconds"
    elif isinstance(value, int):
        kind = "integer"
    elif isinstance(value, float):
        kind = "number"
    elif isinstance(value, str):
        kind = "string"
    else:
        kind = value.__class__.__name__.lower()
    return {"type": kind, "value": _json_scalar(value)}


def _resolve_part(base_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def _sheet_parts(archive: zipfile.ZipFile) -> dict[str, str]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    by_id = {
        rel.get("Id", ""): _resolve_part("xl/workbook.xml", rel.get("Target", ""))
        for rel in rels.findall(f"{_REL}Relationship")
        if rel.get("Id") and rel.get("Target")
    }
    out: dict[str, str] = {}
    sheets = workbook.find(f"{_MAIN}sheets")
    for sheet in list(sheets) if sheets is not None else []:
        name = sheet.get("name")
        part = by_id.get(sheet.get(_DOC_REL_ID, ""))
        if name and part:
            out[name] = part
    return out


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(node.text or "" for node in item.iter(f"{_MAIN}t"))
            for item in root.findall(f"{_MAIN}si")]


def _raw_from_cell(cell: ET.Element, shared: list[str]) -> Any:
    cell_type = cell.get("t", "n")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_MAIN}t"))
    value_node = cell.find(f"{_MAIN}v")
    raw = value_node.text if value_node is not None else None
    if cell_type == "s" and raw is not None:
        try:
            index = int(raw)
        except ValueError:
            return None
        return shared[index] if 0 <= index < len(shared) else None
    if cell_type == "b":
        return raw == "1"
    if cell_type in {"str", "e", "d"}:
        return raw
    return _number_or_text(raw)


_CELL_TAG = f"{_MAIN}c"
_FORMULA_TAG = f"{_MAIN}f"
_VALUE_TAG = f"{_MAIN}v"


def _raw_cells(
    path: Path,
    targets: dict[str, set[str]],
    unaddressed: set[str] | None = None,
) -> dict[tuple[str, str], tuple[Any, str | None, bool]]:
    """対象座標だけを worksheet XML から stream 抽出する。値は ``(原値, 数式, キャッシュ有無)``。

    `unaddressed` を渡すと、座標（`r`）の無いセルを持つシート名をそこへ足す（そのセルは座標で引けない）。
    """
    out: dict[tuple[str, str], tuple[Any, str | None, bool]] = {}
    with zipfile.ZipFile(path) as archive:
        parts = _sheet_parts(archive)
        shared = _shared_strings(archive)
        for sheet_name in sorted(targets):
            wanted = targets[sheet_name]
            part = parts.get(sheet_name)
            if not wanted or not part:
                continue
            with archive.open(part) as stream:
                for _event, cell in ET.iterparse(stream, events=("end",)):
                    if cell.tag != _CELL_TAG:
                        continue
                    coordinate = cell.get("r", "").upper()
                    if not coordinate and unaddressed is not None:
                        unaddressed.add(sheet_name)
                    if coordinate in wanted:
                        formula_node = cell.find(_FORMULA_TAG)
                        value_node = cell.find(_VALUE_TAG)
                        formula = None if formula_node is None else f"={formula_node.text or ''}"
                        out[(sheet_name, coordinate)] = (
                            _raw_from_cell(cell, shared),
                            formula,
                            value_node is not None and value_node.text not in (None, ""),
                        )
                    cell.clear()
    return out


def _split_sections(code: str) -> list[str]:
    sections: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in code:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.extend(("\\",))
            escaped = True
        elif char == '"':
            quoted = not quoted
            current.append(char)
        elif char == ";" and not quoted:
            sections.append("".join(current))
            current = []
        else:
            current.append(char)
    sections.append("".join(current))
    return sections


def _unquote_literal(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            end = text.find('"', index + 1)
            if end < 0:
                return ""
            out.append(text[index + 1:end])
            index = end + 1
        elif char == "\\" and index + 1 < len(text):
            out.append(text[index + 1])
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _format_decimal(value: int | float, code: str) -> str | None:
    if any(marker in code for marker in ("[", "]", "_", "*", "?")):
        return None
    if re.search(r"[Ee][+-]?0", code) or "/" in code:
        return None
    placeholders = [match.start() for match in _NUMERIC_TOKEN_RE.finditer(code)]
    if not placeholders:
        return None
    first, last = placeholders[0], placeholders[-1]
    prefix = _unquote_literal(code[:first])
    tail = code[last + 1:]
    scale_commas = len(tail) - len(tail.lstrip(","))
    suffix = _unquote_literal(tail[scale_commas:])
    core = code[first:last + 1]
    percent_count = code.count("%")
    try:
        number = Decimal(str(value)) * (Decimal(100) ** percent_count) / (Decimal(1000) ** scale_commas)
    except InvalidOperation:
        return None

    decimal_part = core.split(".", 1)[1] if "." in core else ""
    decimal_placeholders = "".join(char for char in decimal_part if char in "0#")
    decimals = len(decimal_placeholders)
    quantum = Decimal(1).scaleb(-decimals)
    number = number.quantize(quantum, rounding=ROUND_HALF_UP)
    grouped = "," in core.split(".", 1)[0]
    rendered = format(number, f",.{decimals}f" if grouped else f".{decimals}f")
    if "#" in decimal_placeholders and "." in rendered:
        mandatory = decimal_placeholders.count("0")
        integer, fraction = rendered.split(".", 1)
        fraction = fraction.rstrip("0")
        fraction += "0" * max(0, mandatory - len(fraction))
        rendered = integer + (("." + fraction) if fraction else "")

    integer_pattern = core.split(".", 1)[0].replace(",", "")
    mandatory_integer = integer_pattern.count("0")
    sign = "-" if rendered.startswith("-") else ""
    unsigned = rendered[1:] if sign else rendered
    integer, dot, fraction = unsigned.partition(".")
    plain_integer = integer.replace(",", "")
    if mandatory_integer > len(plain_integer):
        plain_integer = plain_integer.zfill(mandatory_integer)
        integer = f"{int(plain_integer):,}" if grouped else plain_integer
    rendered = sign + integer + (dot + fraction if dot else "")
    return prefix + rendered + suffix


@lru_cache(maxsize=512)
def _date_tokens(code: str) -> list[tuple[str, bool]] | None:
    """(text, is_token) 列へ分解する。quote/escape は literal に確定する。"""
    out: list[tuple[str, bool]] = []
    index = 0
    while index < len(code):
        char = code[index]
        if char == '"':
            end = code.find('"', index + 1)
            if end < 0:
                return None
            out.append((code[index + 1:end], False))
            index = end + 1
            continue
        if char == "\\" and index + 1 < len(code):
            out.append((code[index + 1], False))
            index += 2
            continue
        if code[index:index + 5].casefold() == "am/pm":
            out.append((code[index:index + 5], True))
            index += 5
            continue
        if char.casefold() in "ymdhs":
            end = index + 1
            while end < len(code) and code[end].casefold() == char.casefold():
                end += 1
            out.append((code[index:end], True))
            index = end
            continue
        out.append((char, False))
        index += 1
    return out


def _format_date(value: date | datetime | time, code: str) -> str | None:
    if any(marker in code for marker in ("[", "]", "_", "*")) or re.search(r"[ge]+", code, re.I):
        return None
    tokens = _date_tokens(code)
    if tokens is None:
        return None
    dt = value
    year = getattr(dt, "year", 0)
    month = getattr(dt, "month", 0)
    day = getattr(dt, "day", 0)
    hour = getattr(dt, "hour", 0)
    minute = getattr(dt, "minute", 0)
    second = getattr(dt, "second", 0)
    has_ampm = any(is_token and token.casefold() == "am/pm" for token, is_token in tokens)
    rendered: list[str] = []
    for index, (token, is_token) in enumerate(tokens):
        if not is_token:
            rendered.append(token)
            continue
        lower = token.casefold()
        if lower == "am/pm":
            rendered.append("AM" if hour < 12 else "PM")
        elif lower[0] == "y":
            rendered.append(f"{year % 100:02d}" if len(lower) <= 2 else f"{year:04d}")
        elif lower[0] == "d":
            rendered.append(f"{day:02d}" if len(lower) >= 2 else str(day))
        elif lower[0] == "h":
            display_hour = hour % 12 or 12 if has_ampm else hour
            rendered.append(f"{display_hour:02d}" if len(lower) >= 2 else str(display_hour))
        elif lower[0] == "s":
            rendered.append(f"{second:02d}" if len(lower) >= 2 else str(second))
        elif lower[0] == "m":
            previous = tokens[index - 1][0] if index else ""
            following = tokens[index + 1][0] if index + 1 < len(tokens) else ""
            is_minute = previous == ":" or following == ":" or any(
                is_t and text.casefold().startswith("h") for text, is_t in tokens[:index])
            value_m = minute if is_minute else month
            rendered.append(f"{value_m:02d}" if len(lower) >= 2 else str(value_m))
    return "".join(rendered)


@lru_cache(maxsize=512)
def _format_code(number_format: str | None) -> tuple[str, str]:
    """表示形式を ``("multi_section", "")``・``("general", "")``・``("code", 書式コード)`` に分類する。"""
    sections = _split_sections(number_format or "General")
    if len(sections) != 1:
        return "multi_section", ""
    code = sections[0].strip() or "General"
    if code.casefold() == "general":
        return "general", ""
    return "code", code


def format_display(value: Any, number_format: str, *, formula_cache_missing: bool = False) -> DisplayResult:
    """主要な Excel 書式だけを表示し、未対応は推測しない。"""
    if formula_cache_missing:
        return DisplayResult(None, EXCEL_DISPLAY_PROFILE, "unsupported", "formula_cache_missing")
    if value is None:
        return DisplayResult("", "raw_value", "rendered", None)
    if isinstance(value, str):
        return DisplayResult(value, "raw_value", "rendered", None)
    if isinstance(value, bool):
        return DisplayResult("TRUE" if value else "FALSE", EXCEL_DISPLAY_PROFILE, "rendered", None)

    kind, code = _format_code(number_format)
    if kind == "multi_section":
        return DisplayResult(None, EXCEL_DISPLAY_PROFILE, "unsupported", "multi_section_format")
    if kind == "general":
        return DisplayResult(str(_json_scalar(value)), "raw_value", "rendered", None)
    if isinstance(value, (datetime, date, time)):
        rendered = _format_date(value, code)
        reason = None if rendered is not None else "complex_date_format"
        return DisplayResult(rendered, EXCEL_DISPLAY_PROFILE, "rendered" if rendered is not None else "unsupported", reason)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        rendered = _format_decimal(value, code)
        reason = None if rendered is not None else "complex_number_format"
        return DisplayResult(rendered, EXCEL_DISPLAY_PROFILE, "rendered" if rendered is not None else "unsupported", reason)
    return DisplayResult(None, EXCEL_DISPLAY_PROFILE, "unsupported", "unsupported_value_type")


def _formula_book_values(
    source: Path,
    wanted: dict[str, set[tuple[int, int]]],
) -> dict[tuple[str, int, int], Any]:
    """数式ブック（data_only=False・読み取り専用）から、指定セルの値（数式の復元結果）だけを読む。"""
    import openpyxl

    found: dict[tuple[str, int, int], Any] = {}
    workbook = openpyxl.load_workbook(source, data_only=False, read_only=True, keep_links=False)
    try:
        for sheet_name, positions in wanted.items():
            if sheet_name not in workbook.sheetnames:
                continue
            first_row = min(row for row, _ in positions)
            last_row = max(row for row, _ in positions)
            last_column = max(column for _, column in positions)
            by_row: dict[int, list[int]] = {}
            for row, column in positions:
                by_row.setdefault(row, []).append(column)
            for offset, values in enumerate(
                    workbook[sheet_name].iter_rows(
                        min_row=first_row, max_row=last_row, min_col=1, max_col=last_column, values_only=True)):
                columns = by_row.get(first_row + offset)
                if not columns:
                    continue
                for column in columns:
                    found[(sheet_name, first_row + offset, column)] = values[column - 1] if column <= len(values) else None
    finally:
        workbook.close()
    return found


_COORDINATE_RE = re.compile(r"([A-Z]+)([0-9]+)")
_COLUMN_INDEX: dict[str, int] = {}


def _cell_position(coordinate: str) -> tuple[int, int]:
    """検証済みの A1 形式の座標を (行, 列) にする。"""
    match = _COORDINATE_RE.fullmatch(coordinate)
    assert match is not None
    letters = match.group(1)
    column = _COLUMN_INDEX.get(letters)
    if column is None:
        from openpyxl.utils import column_index_from_string
        column = _COLUMN_INDEX[letters] = column_index_from_string(letters)
    return int(match.group(2)), column


def _iter_cell_metadata(
    path: str | Path, targets: dict[str, set[str]],
) -> Iterator[tuple[tuple[str, str], dict[str, Any]]]:
    """`extract_cell_metadata` と同じ内容を、セルごとに（シート名・座標の昇順で）返す。全セル分の辞書を持たずに済む。"""
    source = Path(path)
    normalized_targets = {
        sheet: {coordinate.upper() for coordinate in coordinates if _CELL_REF_RE.fullmatch(coordinate.upper())}
        for sheet, coordinates in targets.items()
    }
    unaddressed: set[str] = set()
    raw = _raw_cells(source, normalized_targets, unaddressed)

    wb_values = _ooxml_excel.load_workbook_fast(source, data_only=True, read_only=False, keep_links=False)
    try:
        # ① 式の復元に数式ブックが要るセル（共有数式の従属セル・座標の無いセルを持つシートで XML に見つからないセル）
        needs_formula_value: dict[str, set[tuple[int, int]]] = {}
        for sheet_name, coordinates in normalized_targets.items():
            if sheet_name not in wb_values.sheetnames:
                continue
            for coordinate in coordinates:
                entry = raw.get((sheet_name, coordinate))
                if (entry is not None and entry[1] == "=") or (entry is None and sheet_name in unaddressed):
                    needs_formula_value.setdefault(sheet_name, set()).add(_cell_position(coordinate))
        formula_values = _formula_book_values(source, needs_formula_value) if needs_formula_value else {}

        # ② セルごとに、書式と表示値を決める
        for sheet_name in sorted(normalized_targets):
            if sheet_name not in wb_values.sheetnames:
                continue
            values_sheet = wb_values[sheet_name]
            for coordinate in sorted(normalized_targets[sheet_name]):
                key = (sheet_name, coordinate)
                row, column = _cell_position(coordinate)
                values_cell = values_sheet.cell(row=row, column=column)
                entry = raw.get(key)
                formula = None if entry is None else entry[1]
                if entry is not None and formula != "=":
                    formula_cell_value = values_cell.value
                else:
                    formula_cell_value = formula_values.get((sheet_name, row, column), values_cell.value)
                # shared formula の follower は XML 直読だと式が単独の ``=`` になるため、openpyxl が復元した式で補う
                if (formula in {None, "="} and isinstance(formula_cell_value, str)
                        and formula_cell_value.startswith("=")):
                    formula = formula_cell_value
                has_cache = bool(entry is not None and entry[2]) if formula else False
                raw_value = _json_scalar(formula_cell_value) if entry is None else entry[0]
                number_format = values_cell.number_format or "General"
                semantic_value = values_cell.value if formula else formula_cell_value
                calculation_status = "cached" if formula and has_cache else "cache_missing" if formula else "not_formula"
                display = format_display(
                    semantic_value,
                    number_format,
                    formula_cache_missing=bool(formula and not has_cache),
                )
                yield key, {
                    "raw_value": raw_value,
                    "typed_value": _typed_value(semantic_value),
                    "formula": formula,
                    "cached_value": _json_scalar(values_cell.value) if formula and has_cache else None,
                    "calculation_status": calculation_status,
                    "style_id": int(values_cell.style_id),
                    "number_format": number_format,
                    "display_value": display.value,
                    "display_source": display.source,
                    "display_status": display.status,
                    "display_reason": display.reason,
                }
    finally:
        wb_values.close()


def extract_cell_metadata(path: str | Path, targets: dict[str, set[str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """対象セルの 10 項目契約を返す。targets は ``{sheet: {A1, ...}}``。

    ① OOXML から原値・数式を読む ② 値ブック（data_only=True）を 1 冊だけ読み、書式・型付き値・表示値を決める。
    数式ブック（data_only=False）が要るのは、OOXML 直読で式が空の共有数式の従属セルと、座標の無いセルを持つシートの未解決セルだけで、
    その値だけを読み取り専用で読む（式の無いセルの値は 2 冊で同じ）。
    """
    return dict(_iter_cell_metadata(path, targets))


@relaxed_gc
def enrich_evidence(ir, path: str | Path) -> None:
    """XLSX Evidence の cell/formula extension へ表示契約を in-place で追加する。"""
    targets: dict[str, set[str]] = {}
    elements = []
    for element in ir.elements:
        if element.type not in {"cell", "formula"} or not element.locator.sheet or not element.locator.cell_range:
            continue
        coordinate = element.locator.cell_range.upper()
        if ":" in coordinate or not _CELL_REF_RE.fullmatch(coordinate):
            continue
        targets.setdefault(element.locator.sheet, set()).add(coordinate)
        elements.append((element, coordinate))
    if not elements:
        return
    missing = {
        "raw_value": None,
        "typed_value": {"type": "blank", "value": None},
        "formula": None,
        "cached_value": None,
        "calculation_status": "not_formula",
        "style_id": 0,
        "number_format": "General",
        "display_value": None,
        "display_source": EXCEL_DISPLAY_PROFILE,
        "display_status": "unsupported",
        "display_reason": "cell_not_found",
    }
    waiting: dict[tuple[str, str], list] = {}
    for element, coordinate in elements:
        waiting.setdefault((element.locator.sheet, coordinate), []).append(element)
    del elements
    for key, metadata in _iter_cell_metadata(path, targets):
        for element in waiting.pop(key, ()):
            element.extension.update(metadata)
    for waiting_elements in waiting.values():                    # 値ブックに見つからなかったセル
        for element in waiting_elements:
            element.extension.update(missing)
