"""点検の名前の項目: ファイル名と中に書かれた名前が違うソース・どこにも定義の無い名前（入力は名前の台帳の行と未解決の申告）。
行は取り込みと同じ `name_rows.build_rows` で作る。未解決の申告の読み取り（Neo4j）は保存形式の行を直接渡して確かめる。
"""
from __future__ import annotations

import json

import pytest

from sherpa.ingest import name_rows, world_neo4j_unresolved
from sherpa.ingest.check import GOOD, WARN, inspect_folder, to_json, to_markdown

pytestmark = pytest.mark.unit

_DOCS = [{"name": f"gen1/src/{n}.cbl", "branch": "source"} for n in ("ZZAPROG1", "ZZBPROG1", "ZZCPROG1")]


def _node(label, name, path):
    return {"analyzer": "cobol", "label": label, "name": name, "path": path}


_NODES = [_node("Module", "ZZAPROG0", "gen1/src/ZZAPROG1.cbl"), _node("Module", "ZZBPROG1", "gen1/src/ZZBPROG1.cbl"),
          _node("Module", "ZZCPROG1", "gen1/src/ZZCPROG1.cbl"),
          {**_node("Module", "SUBA", "gen1/src/ZZAPROG1.cbl"), "line": 9}]


def _rows():
    return name_rows.build_rows(_DOCS, _NODES)


def _names(rep):
    return [it for it in rep.items if it.rule == "名前"]


def test_mismatched_source_is_counted_with_examples():
    rep = inspect_folder([d["name"] for d in _DOCS], {}.get, names=_rows(), unresolved={"names": {}, "overflow": 0})
    warn = [it for it in _names(rep) if it.result == WARN]
    assert len(warn) == 1 and warn[0].count == 1
    assert warn[0].examples == ["gen1/src/ZZAPROG1.cbl（中に書かれた名前 ZZAPROG0）"]
    assert [it.result for it in _names(rep) if "どこにも定義の無い" in it.what] == [GOOD]


def test_no_mismatch_is_good_and_extension_names_are_not_mismatch():
    rows = name_rows.build_rows([{"name": "a/main.c", "branch": "source"}], [_node("Module", "main.c", "a/main.c")])
    rep = inspect_folder(["a/main.c"], {}.get, names=rows, unresolved={"names": {}, "overflow": 0})
    assert [(it.result, it.count) for it in _names(rep)] == [(GOOD, 0), (GOOD, 0)]


def test_unresolved_names_show_near_candidate_and_file_name_hint():
    unresolved = {"names": {"ZZBPROG0": ["gen1/src/ZZCPROG1.cbl"], "ZZCPROG1": ["gen1/src/ZZAPROG1.cbl"]}, "overflow": 2}
    rep = inspect_folder([d["name"] for d in _DOCS], {}.get, names=_rows(), unresolved=unresolved)
    item = next(it for it in _names(rep) if "どこにも定義の無い" in it.what)
    assert item.result == WARN and item.count == 2 and "上限で数えなかった参照が 2 件" in item.what
    assert item.examples == ["ZZBPROG0（参照元 gen1/src/ZZCPROG1.cbl・番号だけが違う近い名前（確認していない候補）: ZZBPROG1）",
                             "ZZCPROG1（参照元 gen1/src/ZZAPROG1.cbl・ファイル名では見つかる: gen1/src/ZZCPROG1.cbl）"]


def test_unreadable_graph_and_missing_ledger_are_stated_not_hidden():
    rep = inspect_folder([d["name"] for d in _DOCS], {}.get, names=_rows(), unresolved=None)
    assert any("未解決の参照を読めなかった" in t for t in rep.not_counted)
    assert len([it for it in _names(rep)]) == 1
    rep = inspect_folder([d["name"] for d in _DOCS], {}.get)
    assert not _names(rep) and any("名前の台帳がまだ無い" in t for t in rep.not_counted)


def test_built_ledger_with_no_names_is_checked_not_reported_as_missing():
    """名前が 0 件でも作り終えた台帳は「台帳がまだ無い」と断らず、名前の点検を行う。"""
    rep = inspect_folder([d["name"] for d in _DOCS], {}.get, names=[], unresolved={"names": {}, "overflow": 0})
    assert not any("名前の台帳がまだ無い" in t for t in rep.not_counted)


def test_report_outputs_carry_the_name_items():
    rep = inspect_folder([d["name"] for d in _DOCS], {}.get, names=_rows(), unresolved={"names": {}, "overflow": 0})
    md = to_markdown(rep, "r")
    assert "## 名前 ファイル名と中に書かれた名前" in md and "ZZAPROG0" in md
    out = to_json(rep, "r")
    assert out["summary"]["名前"][WARN] == 1 and any(i["rule"] == "名前" for i in out["items"])
    json.dumps(out, ensure_ascii=False)


def test_unresolved_names_are_read_from_saved_declarations():
    saved = [{"path": "gen1/src/ZZCPROG1.cbl", "ov": 3, "u": json.dumps([
        {"reason": "unresolved", "kind": "Module", "name": "zzbprog0", "line": 5},
        {"reason": "ambiguous", "kind": "Module", "name": "AMBIG", "line": 6},
        {"reason": "unresolved", "kind": "Dataset", "name": "SOME.DATA", "line": 7}])}]
    got = world_neo4j_unresolved._collect([{"stored": True, "nodes": saved}])
    assert got == {"names": {"ZZBPROG0": ["gen1/src/ZZCPROG1.cbl"]}, "overflow": 3}
    assert world_neo4j_unresolved._collect([{"stored": None, "nodes": saved}]) is None
    assert world_neo4j_unresolved._collect([]) is None
