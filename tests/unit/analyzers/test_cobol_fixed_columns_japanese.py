"""SRH-04: decoded Japanese must not shift COBOL's fixed-column boundary."""
import pytest

from sherpa.ingest.analyzers.cobol import CobolAnalyzer
from sherpa.ingest.analyzers.copybook import CopybookAnalyzer
from sherpa.ingest.static_analysis import _normalize_logical_lines


def _record(code, suffix="", prefix="000100 "):
    body = prefix + code
    return body + " " * (72 - len(body.encode("cp932"))) + suffix


@pytest.mark.parametrize("encoding", ["cp932", "utf-8"])
@pytest.mark.parametrize("literal", ["日本語機能名" * 3, "ｶﾅ" * 8, "αβγ①㈱" * 3, "日本ｶﾅ①" * 4])
def test_identification_area_does_not_create_call_references(encoding, literal):
    text = "\n".join([
        "       PROGRAM-ID. MAIN.",
        _record(f"DISPLAY '{literal}'.", "CALL 'X'."),
        "           CALL 'REAL'.",
    ])
    text = text.encode(encoding).decode(encoding)
    result = CobolAnalyzer().extract_refs(text, "MAIN.cbl")
    assert [(ref.name, ref.line) for ref in result.refs] == [("REAL", 3)]
    assert result.dropped == []


def test_japanese_literal_continuation_preserves_text_and_physical_boundaries():
    first = _record("DISPLAY '日本語", "CALL 'X'.")
    continuation = _record("    'の続き'.", "COPY FALSE.", prefix="000200-")
    text = "\n".join([first, "000150*日本語コメント", continuation])
    entries, dropped = _normalize_logical_lines(text, free_format=False)
    first_code = "DISPLAY '日本語" + " " * (72 - len("000100 DISPLAY '日本語".encode("cp932")))
    second_code = "の続き'." + " " * (72 - len("000200-    'の続き'.".encode("cp932")))
    assert entries == [(first_code + second_code, 1, (0, len(first_code)))]
    assert dropped == []


def test_multibyte_sequence_area_keeps_indicator_and_code_columns():
    text = "\n".join([
        "連番一*COPY FALSE.",
        "連番二 PROGRAM-ID. MAIN.",
        "連番三     CALL 'REAL'.",
    ])
    analyzer = CobolAnalyzer()
    assert analyzer.collect_defs(text, "MAIN.cbl").primary.name == "MAIN"
    assert [(ref.name, ref.line) for ref in analyzer.extract_refs(text, "MAIN.cbl").refs] == [("REAL", 3)]


def test_copybook_value_in_identification_area_is_not_a_definition_value():
    text = "\n".join([
        "       01 RECORD-A.",
        _record("    05 LABEL-A PIC X VALUE '日本語機能名日本語機能名'.", "VALUE 999."),
    ])
    result = CopybookAnalyzer().collect_defs(text, "RECORD-A.cpy")
    label = next(child for child in result.children if child.name == "LABEL-A")
    assert label.value is None
    assert label.line == 2


def test_fixed_columns_preserve_unicode_outside_cp932_as_one_column():
    code = "DISPLAY '日本😀'."
    line = "       " + code
    line += " " * (72 - len(line.encode("cp932", errors="replace")))
    entries, _ = _normalize_logical_lines(line + "COPY FALSE.", free_format=False)
    assert entries == [(line[7:], 1, (0,))]


@pytest.mark.parametrize("free_format", [True, False])
def test_free_and_column_one_styles_preserve_japanese_text(free_format):
    text = "IDENTIFICATION DIVISION.\n" + _record("DISPLAY '日本語機能名'.", "CALL 'REAL'.")
    entries, _ = _normalize_logical_lines(text, free_format=free_format)
    assert entries == [(line, number, (0,)) for number, line in enumerate(text.splitlines(), 1)]
