"""ANA-14 S4（COBOL・Copybook・JS・Shell・SQL の誤抽出と取りこぼし）の受入契約。

`fixtures/corpus/ana-s4` を `build_world()` に通し、グラフ上の辺と申告（`flags`）で確かめる。
"""
from __future__ import annotations

import pathlib

import pytest

from sherpa.ingest import world_graph

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "ana-s4"


@pytest.fixture(scope="module")
def built():
    nodes, edges, flags = world_graph.build_world(WORLD_DIR, "ana-s4")
    path_of = {n["cid"]: n["path"] for n in nodes}
    name_of = {n["cid"]: n["name"] for n in nodes}
    return nodes, edges, flags, path_of, name_of


def _file_edges(built, etype):
    _nodes, edges, _flags, path_of, _names = built
    return {(path_of[e["src"]], path_of[e["dst"]]) for e in edges if e["type"] == etype}


def _dropped(flags, why, src=None):
    return [f for f in flags if f.get("reason") == "dropped_syntax" and f.get("why") == why
            and (src is None or f.get("from") == src)]


def test_cobol_fake_call_in_literal_is_not_an_edge_but_call_operand_is(built):
    calls = _file_edges(built, "INVOKES")
    assert ("cobol/MAINPG.cbl", "cobol/REALPG.cbl") in calls
    assert ("cobol/MAINPG.cbl", "cobol/GHOST.cbl") not in calls


def test_nested_copy_reaches_the_using_program_from_the_innermost_copybook(built):
    copies = _file_edges(built, "COPIES")
    assert ("cobol/OUTER.cpy", "cobol/INNER.cpy") in copies
    assert ("cobol/MAINPG.cbl", "cobol/OUTER.cpy") in copies
    # INNER から逆向きに COPIES をたどると MAINPG に届く
    reached, frontier = {"cobol/INNER.cpy"}, ["cobol/INNER.cpy"]
    while frontier:
        cur = frontier.pop()
        for src, dst in copies:
            if dst == cur and src not in reached:
                reached.add(src)
                frontier.append(src)
    assert "cobol/MAINPG.cbl" in reached


def test_static_cursor_reads_the_table_and_expression_from_is_not_a_table(built):
    _nodes, _edges, flags, _p, _n = built
    _nodes, edges, _f, path_of, name_of = built
    accessed = {name_of[e["dst"]] for e in edges if e["type"] == "ACCESSES" and path_of[e["src"]] == "cobol/MAINPG.cbl"}
    assert accessed == {"CARD", "CUSTOMER"}  # CARD は静的カーソルだけが読む表
    # 属性つきで準備済みの文の名前を指すカーソル（C2）だけが動的として申告される
    dynamic = _dropped(flags, "exec_sql_dynamic")
    assert len(dynamic) == 1 and "SCROLL" in dynamic[0]["snippet"]
    assert not [f for f in flags if f.get("name") == "CREATED_AT"]


def test_js_string_code_is_skipped_and_reported_while_api_arguments_remain(built):
    _nodes, _edges, flags, _p, _n = built
    _nodes, edges, _f, path_of, _n = built
    includes = sorted(path_of[e["dst"]] for e in edges if e["type"] == "INVOKES" and path_of[e["src"]] == "js/a.js")
    assert includes == ["js/b.js", "js/c.js"]  # 文字列の中の fake.js・/api/fake は辺にも未解決にもならない
    assert not [f for f in flags if f.get("name") in ("/api/fake", "fake.js")]
    assert len(_dropped(flags, "js_string_code", "js/a.js")) == 1
    assert any(f.get("name") == "/api/z" for f in flags)  # fetch の第 1 引数は今までどおり未解決として出る
    assert [f for f in flags if f.get("name") == "/api/z"][0]["line"] == 4


def test_quoted_heredoc_body_is_skipped_and_reported(built):
    _nodes, _edges, flags, _p, _n = built
    calls = {d for s, d in _file_edges(built, "INVOKES") if s == "sh/r.sh"}
    assert calls == {"sh/other.sh"}
    assert len(_dropped(flags, "shell_heredoc", "sh/r.sh")) == 1


def test_indented_terminator_does_not_end_a_plain_heredoc(built):
    calls = {d for s, d in _file_edges(built, "INVOKES") if s == "sh/r2.sh"}
    assert calls == {"sh/other.sh"}


def test_template_expression_dependency_is_kept_and_template_text_is_skipped(built):
    includes = {d for s, d in _file_edges(built, "INVOKES") if s == "js/t.js"}
    assert includes == {"js/b.js", "js/c.js", "js/d.js"}  # `${}` の中・複数行 import・`require (` を含む


def test_self_copy_is_reported_and_mutual_copy_cycle_builds_without_looping(built):
    _nodes, _edges, flags, _p, _n = built
    copies = _file_edges(built, "COPIES")
    assert ("cobol/SELF.cpy", "cobol/SELF.cpy") not in copies
    assert len(_dropped(flags, "copy_self_reference")) == 1
    # 相互の COPY は事実なので辺を残す（探索側は訪問済みで止まる）
    assert {("cobol/CYCA.cpy", "cobol/CYCB.cpy"), ("cobol/CYCB.cpy", "cobol/CYCA.cpy")} <= copies


def test_xhr_open_second_argument_is_kept_when_method_is_a_variable(built):
    _nodes, _edges, flags, _p, _n = built
    assert [f for f in flags if f.get("name") == "/api/open" and f.get("from") == "js/o.js"]
