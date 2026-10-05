"""Tree-sitter 共通部品 `_ts`（ANA-19 T0）: 文法の読み込み・行番号・構文エラーの申告。"""
from __future__ import annotations

import threading

import pytest

from sherpa.ingest.analyzers import _ts
from sherpa.ingest.analyzers._base import Dropped

_VALID = {
    "java": "class A { void f() {} }\n",
    "c_sharp": "class A { void F() {} }\n",
    "c": "int f(void) { return 0; }\n",
    "javascript": "function f() { return 1; }\n",
    "bash": "echo hi\n",
    "css": "a { color: red; }\n",
    "embedded_template": "<% x %> y\n",
}


def test_every_grammar_loads_and_parses_valid_source_without_errors():
    _ts.require()
    for lang, src in _VALID.items():
        assert _ts.syntax_errors(_ts.parse(lang, src)) == [], lang


def test_missing_grammar_fails_loudly(monkeypatch):
    monkeypatch.setitem(_ts._GRAMMARS, "ghost", ("tree_sitter_no_such_grammar", "language"))
    with pytest.raises(_ts.TreeSitterUnavailable):
        _ts.language("ghost")


def test_line_numbers_are_one_based_and_survive_crlf_and_multibyte():
    p = _ts.parse("java", "class A {\r\n  // 日本語\r\n  void f() {}\r\n}\r\n")
    method = _ts.captures(p, "(method_declaration) @m")["m"][0]
    assert (_ts.start_line(method), _ts.end_line(method)) == (3, 3)
    assert p.text(method) == "void f() {}"


def test_syntax_error_is_reported_once_per_line_with_snippet():
    p = _ts.parse("java", "class A {\n  void f( { int x = ;\n}\n")
    errs = _ts.syntax_errors(p)
    assert errs and all(isinstance(e, Dropped) and e.reason == "syntax_error" for e in errs)
    lines = [e.line for e in errs]
    assert lines == sorted(set(lines)) and 2 in lines
    assert all(e.snippet and len(e.snippet) <= _ts.SNIPPET_MAX for e in errs)


def test_parser_is_per_thread():
    ids = []
    t = threading.Thread(target=lambda: ids.append(id(_ts._parser("c"))))
    t.start()
    t.join()
    assert ids[0] != id(_ts._parser("c")) and _ts._parser("c") is _ts._parser("c")


def test_bytes_input_with_bom_and_invalid_bytes():
    p = _ts.parse("java", b"\xef\xbb\xbfclass A { // \xff\n int x = ;}")
    assert [(e.line, e.snippet) for e in _ts.syntax_errors(p)] == [(2, "=")]
    comment = _ts.captures(p, "(line_comment) @c")["c"][0]
    assert "\ufffd" in p.text(comment) and _ts.start_line(comment) == 1


def test_missing_inside_error_on_another_line_is_reported():
    errs = _ts.syntax_errors(_ts.parse("java", "class A {\n  void f( {\n  int x = ;\n  foo(\n}\n"))
    assert [(e.line, e.snippet) for e in errs][:2] == [(1, "class A {"), (2, "missing )")]


def test_whole_file_error():
    p = _ts.parse("java", "}}} (((\n;;; )))\n")
    assert [e.line for e in _ts.syntax_errors(p)] == [1, 2]


def test_line_numbers_of_many_nodes_in_a_long_file_do_not_corrupt_memory():
    """`node.start_point.row`（連鎖の属性参照）は行が 256 を超えると解放後使用で GC 時に落ちる（tree-sitter 0.26）。添字で取る。"""
    import gc

    src = "".join(f"A{i}=1\n" for i in range(600))
    for _ in range(3):
        p = _ts.parse("bash", src)
        lines = [_ts.start_line(n) for n in _ts.captures(p, "(variable_assignment) @a")["a"]]
        assert sorted(lines) == list(range(1, 601))
        del p
        gc.collect()


def test_syntax_errors_are_capped_with_an_omitted_count():
    p = _ts.parse("bash", "".join("echo $(( 0$p & 2 ))\n" for _ in range(50)))
    errs = _ts.syntax_errors(p)
    assert len(errs) == _ts.SYNTAX_ERROR_MAX + 1
    assert errs[-1].reason == "syntax_error" and errs[-1].snippet.startswith("ほか ")


def test_captures_are_returned_in_document_order():
    src = "".join(f"f{i}();\n" for i in range(40))
    lines = [_ts.start_line(n) for n in _ts.captures(_ts.parse("c", src), "(call_expression) @c")["c"]]
    assert lines == list(range(1, 41))
