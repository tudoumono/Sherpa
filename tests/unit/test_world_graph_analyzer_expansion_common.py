"""アナライザ拡張 S3a（docs/proposals/2026-09-05-アナライザ拡張.md §4(b)/(c)/(c)'/(f)・A6/A8/A9）の
共通層契約テスト。設定ファイルアナライザ本体（S3b）は未実装のため、既存の流儀
（`tests/unit/test_world_graph_edge_extra.py` と同じ・Java/XML に依存しない最小のフェイクアナライザで
`registry._ANALYZERS` を monkeypatch）で共通層（`world_graph`）だけを検証する。

golden（`tests/unit/goldens/world_graph_v1.json`・`world_graph_java1.json`）の更新手順
（**意図した増減のときだけ**実行し、差分を目視確認してからコミットする・`test_agents_surface.py` と
同じ流儀・言及閾値は `_snapshot` 内で既定値 4/200 に固定される）:

    SHERPA_USE_FIXTURES=1 PYTHONPATH=. .venv/bin/python -c \
        "import sys; sys.path.insert(0, 'tests/unit'); \
         import test_world_graph_analyzer_expansion_common as t; t._write_goldens()"
"""
from __future__ import annotations

import os

import json
import pathlib
from collections import Counter

import pytest

from sherpa.ingest import world_graph
from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers._base import (Analyzer, DefItem, DefResult,
                                           RefCandidate, RefResult)

ROOT = pathlib.Path(__file__).resolve().parents[2]
GOLDEN_DIR = pathlib.Path(__file__).resolve().parent / "goldens"
V1_GOLDEN = GOLDEN_DIR / "world_graph_v1.json"
JAVA1_GOLDEN = GOLDEN_DIR / "world_graph_java1.json"


class _FakeAnalyzer(Analyzer):
    """`collect_defs`/`extract_refs` の戻り値を rel_path ごとに差し替えられる最小フェイク。

    `name`/`extensions` は既定（`"fake"`/`.fk`）から差し替え可能——複数言語が混在する world を
    模す回帰テスト（例: C 由来 children の解決索引が他言語の unresolved 参照へ漏れないこと）で、
    別々の `analyzer.name`（例: `"c"`/`"java"`）を持つ複数インスタンスを同時に registry へ積むため。
    """

    name = "fake"
    extensions = frozenset({".fk"})
    doctype = "fake"

    def __init__(self, defs_by_rel: dict, refs_by_rel: dict, *, name: str | None = None,
                 extensions: frozenset | None = None):
        self._defs = defs_by_rel
        self._refs = refs_by_rel
        if name is not None:
            self.name = name
        if extensions is not None:
            self.extensions = extensions

    def collect_defs(self, text, rel_path):
        return self._defs.get(rel_path, DefResult())

    def extract_refs(self, text, rel_path):
        return self._refs.get(rel_path, RefResult())


def _world(tmp_path, files: dict):
    wd = tmp_path / "world"
    wd.mkdir()
    for rel, content in files.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return wd


def _by_cid(nodes):
    return {n["cid"]: n for n in nodes}


# --- item 1: Config ラベル・cid 規則（A6） ---

def test_config_label_is_accepted_and_gets_the_generic_path_scoped_cid():
    """`Config` は既に `registry.NODE_LABELS`/`model.NODE_LABELS` にある（A6）——`unknown_label`
    に落ちず、cid は既存ラベルと同じ一般規則 `config:{world}:{rel}#{name}`（S3b 側の
    `name` 導出規則はここでは問わない・共通層は任意の name を汎用に扱えることだけを確認する）。"""
    assert "Config" in registry.NODE_LABELS
    from sherpa.ingest import model
    assert "Config" in model.NODE_LABELS


def test_config_primary_node_lands_with_generic_cid_rule(tmp_path, monkeypatch):
    wd = _world(tmp_path, {"pkg/cfg/app.fk": "x"})
    defs = {"pkg/cfg/app.fk": DefResult(primary=DefItem(label="Config", name="app_config"))}
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, {}),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    cid = "config:w:pkg/cfg/app.fk#app_config"
    assert cid in _by_cid(nodes)
    n = _by_cid(nodes)[cid]
    assert n["label"] == "Config" and n["path"] == "pkg/cfg/app.fk"
    assert not [f for f in flags if f.get("reason") in ("unknown_label", "unknown_edge_type")]


# --- item 2: A8 逆向きエッジ（RefCandidate.reverse） ---

def test_reverse_ref_candidate_flips_edge_direction_to_resolved_target_to_primary(tmp_path, monkeypatch):
    """MyBatis の `<mapper namespace="...">` を模す: Config（Mapper XML）の primary から
    `reverse=True` の参照を出すと、エッジは「解決先（Module）→この Config」の向きで張られる
    （src/dst が入れ替わるだけで解決規則自体は変わらない・A8）。"""
    wd = _world(tmp_path, {"pkg/cfg/mapper.fk": "x", "pkg/svc/OrderMapper.fk": "y"})
    defs = {
        "pkg/cfg/mapper.fk": DefResult(primary=DefItem(label="Config", name="mapper.xml")),
        "pkg/svc/OrderMapper.fk": DefResult(primary=DefItem(label="Module", name="OrderMapper")),
    }
    refs = {
        "pkg/cfg/mapper.fk": RefResult(refs=[
            RefCandidate("INVOKES", "Module", "OrderMapper", 3,
                        extra={"via": "mapper_namespace"}, reverse=True),
        ]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    by_cid = _by_cid(nodes)
    module_cid = next(c for c, n in by_cid.items() if n["label"] == "Module")
    config_cid = next(c for c, n in by_cid.items() if n["label"] == "Config")
    e = next(e for e in edges if e["type"] == "INVOKES")
    assert e["src"] == module_cid and e["dst"] == config_cid   # 解決先(Module)→primary(Config)
    assert e["via"] == "mapper_namespace"
    assert flags == []


# --- item 3: RV2-4 完全修飾名の2段解決 ---

def test_qualified_exact_match_picks_the_right_package_not_the_nearest_simple_name(tmp_path, monkeypatch):
    """同名・別 package（`cid_key` が異なる）2型が同一世代に存在するとき、`extra={"qualified": True}`
    付きの参照は完全修飾名の完全一致で一意に解決する——単純名の最近傍（曖昧/別ファイル）には落ちない。"""
    wd = _world(tmp_path, {
        "pkg/a/Foo.fk": "x", "pkg/b/Foo.fk": "y", "pkg/cfg/beans.fk": "z",
    })
    defs = {
        "pkg/a/Foo.fk": DefResult(primary=DefItem(label="Module", name="Foo", cid_key="com.a.Foo")),
        "pkg/b/Foo.fk": DefResult(primary=DefItem(label="Module", name="Foo", cid_key="com.b.Foo")),
        "pkg/cfg/beans.fk": DefResult(primary=DefItem(label="Config", name="beans.xml")),
    }
    refs = {
        "pkg/cfg/beans.fk": RefResult(refs=[
            RefCandidate("INVOKES", "Module", "com.a.Foo", 1,
                        extra={"via": "bean_class", "qualified": True}),
        ]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    by_cid = _by_cid(nodes)
    a_foo = next(c for c, n in by_cid.items() if n["path"] == "pkg/a/Foo.fk")
    b_foo = next(c for c, n in by_cid.items() if n["path"] == "pkg/b/Foo.fk")
    e = next(e for e in edges if e["type"] == "INVOKES")
    assert e["dst"] == a_foo
    assert e["dst"] != b_foo
    assert "qualified" not in e                        # 解決の指示であってエッジの事実ではない
    assert not [f for f in flags if f.get("reason") == "qualified_fallback"]
    assert not [f for f in flags if f.get("reason") == "ambiguous"]


def test_qualified_miss_falls_back_to_simple_name_and_flags_qualified_fallback(tmp_path, monkeypatch):
    """完全修飾名の完全一致が見つからないときは最後のセグメント（単純名）で通常の最近傍へ
    フォールバックし、`qualified_fallback` を flags へ記録する（黙って倒さない・RV2-4）。"""
    wd = _world(tmp_path, {"pkg/x/Foo.fk": "x", "pkg/cfg/beans.fk": "z"})
    defs = {
        "pkg/x/Foo.fk": DefResult(primary=DefItem(label="Module", name="Foo")),   # cid_key 無し＝qualified_defs 未登録
        "pkg/cfg/beans.fk": DefResult(primary=DefItem(label="Config", name="beans.xml")),
    }
    refs = {
        "pkg/cfg/beans.fk": RefResult(refs=[
            RefCandidate("INVOKES", "Module", "com.unknown.Foo", 1,
                        extra={"via": "bean_class", "qualified": True}),
        ]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    by_cid = _by_cid(nodes)
    foo_cid = next(c for c, n in by_cid.items() if n["path"] == "pkg/x/Foo.fk")
    e = next(e for e in edges if e["type"] == "INVOKES")
    assert e["dst"] == foo_cid
    assert {"reason": "qualified_fallback", "from": "pkg/cfg/beans.fk",
            "kind": "Module", "name": "com.unknown.Foo"} in flags


# --- item 4: §4(f) エッジ集約規則 ---

def test_same_src_type_dst_candidates_collapse_with_fw_specific_via_winning(tmp_path, monkeypatch):
    """同一 (src,type,dst) に汎用 via（`call`）と FW 固有 via（`bean_class`）の両方が来たら、
    1本へ集約し FW 固有側（`analyzer_registry.VIA_PRIORITY` で優先）の via/line を採用する。"""
    wd = _world(tmp_path, {"pkg/a.fk": "x", "pkg/b.fk": "y"})
    defs = {
        "pkg/a.fk": DefResult(primary=DefItem(label="Module", name="A")),
        "pkg/b.fk": DefResult(primary=DefItem(label="Module", name="B")),
    }
    refs = {
        "pkg/a.fk": RefResult(refs=[
            RefCandidate("INVOKES", "Module", "B", 1, extra={"via": "call"}),
            RefCandidate("INVOKES", "Module", "B", 7, extra={"via": "bean_class"}),
        ]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    invokes = [e for e in edges if e["type"] == "INVOKES"]
    assert len(invokes) == 1                            # 集約されて1本
    assert invokes[0]["via"] == "bean_class"
    assert invokes[0]["line"] == 7                       # 採用した via の出現行
    assert flags == []


def test_aggregation_result_is_independent_of_candidate_order(tmp_path, monkeypatch):
    """集約結果はエンコード順（どちらの候補が先に来るか）に依らない——FW 固有 via が後から
    来ても優先される（優先順位比較であって「後勝ち」ではないことの確認）。"""
    wd = _world(tmp_path, {"pkg/c.fk": "x", "pkg/d.fk": "y"})
    defs = {
        "pkg/c.fk": DefResult(primary=DefItem(label="Module", name="C")),
        "pkg/d.fk": DefResult(primary=DefItem(label="Module", name="D")),
    }
    refs = {
        "pkg/c.fk": RefResult(refs=[
            RefCandidate("INVOKES", "Module", "D", 9, extra={"via": "bean_class"}),
            RefCandidate("INVOKES", "Module", "D", 2, extra={"via": "call"}),
        ]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    invokes = [e for e in edges if e["type"] == "INVOKES"]
    assert len(invokes) == 1
    assert invokes[0]["via"] == "bean_class"
    assert invokes[0]["line"] == 9


# --- item 6: A9 config_key 全件接続 ---

def test_config_key_via_connects_to_every_same_name_config_in_the_same_generation(tmp_path, monkeypatch):
    """`via=config_key` は通常の最近傍/ambiguous 判定を迂回し、同一 top_scope 内の同名 `Config`
    キー child（environment-dev/prod 相当の複数ファイル）全件へ1本ずつ張る（A9・RV2-5）。
    config_key の解決先は常に config キー child（`cid_key="key:"+裸キー`・properties/YAML の
    実際の返し方＝S3'）——ファイル自体を表す primary には張らない（専用索引）。"""
    wd = _world(tmp_path, {
        "pkg/App.fk": "x", "pkg/dev/db.fk": "y", "pkg/prod/db.fk": "z",
    })
    defs = {
        "pkg/App.fk": DefResult(primary=DefItem(label="Module", name="App")),
        "pkg/dev/db.fk": DefResult(
            primary=DefItem(label="Config", name="db.fk"),
            children=[DefItem(label="Config", name="db.url", line=1, cid_key="key:db.url")]),
        "pkg/prod/db.fk": DefResult(
            primary=DefItem(label="Config", name="db.fk"),
            children=[DefItem(label="Config", name="db.url", line=1, cid_key="key:db.url")]),
    }
    refs = {
        "pkg/App.fk": RefResult(refs=[
            RefCandidate("ACCESSES", "Config", "db.url", 1, extra={"via": "config_key"}),
        ]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    by_cid = _by_cid(nodes)
    dev_cid = next(c for c, n in by_cid.items()
                   if n["path"] == "pkg/dev/db.fk" and n["name"] == "db.url")
    prod_cid = next(c for c, n in by_cid.items()
                    if n["path"] == "pkg/prod/db.fk" and n["name"] == "db.url")
    accesses = [e for e in edges if e["type"] == "ACCESSES"]
    assert {e["dst"] for e in accesses} == {dev_cid, prod_cid}
    assert all(e["via"] == "config_key" for e in accesses)
    assert not [f for f in flags if f.get("reason") == "ambiguous"]


def test_config_key_unresolved_when_no_config_matches_in_scope(tmp_path, monkeypatch):
    """A9 の特例でも「同世代に定義が無い」ケースは黙って落とさない——`unresolved`/`cross_scope`
    を flags へ記録する（従来の解決不能時と同じ規律）。"""
    wd = _world(tmp_path, {"pkg/App.fk": "x"})
    defs = {"pkg/App.fk": DefResult(primary=DefItem(label="Module", name="App"))}
    refs = {
        "pkg/App.fk": RefResult(refs=[
            RefCandidate("ACCESSES", "Config", "db.url", 1, extra={"via": "config_key"}),
        ]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    assert not [e for e in edges if e["type"] == "ACCESSES"]
    assert {"reason": "unresolved", "from": "pkg/App.fk", "kind": "Config", "name": "db.url",
            "line": 1, "via": "config_key"} in flags


def test_config_key_dst_cid_uses_child_cid_key_not_bare_name_when_it_collides_with_a_primary(
        tmp_path, monkeypatch):
    """RV波1是正: properties/YAML のキー child は `cid_key="key:"+裸キー`（primary との自己ループ
    回避）を持つ。primary と同じ裸名を持つ config キー child でも、`config_key` の全件接続は
    child の cid_key で dst cid を組み立てる——裸の `name` で組み立てると child の実際の cid
    （`config:{world}:{rel}#key:app.properties`）と一致せず、primary の cid
    （`config:{world}:{rel}#app.properties`）に誤って着地していた。"""
    wd = _world(tmp_path, {"pkg/cfg/app.fk": "x", "pkg/App.fk": "y"})
    defs = {
        "pkg/cfg/app.fk": DefResult(
            primary=DefItem(label="Config", name="app.properties"),
            children=[DefItem(label="Config", name="app.properties", line=3,
                              cid_key="key:app.properties")],
        ),
        "pkg/App.fk": DefResult(primary=DefItem(label="Module", name="App")),
    }
    refs = {
        "pkg/App.fk": RefResult(refs=[
            RefCandidate("ACCESSES", "Config", "app.properties", 1, extra={"via": "config_key"}),
        ]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    by_cid = _by_cid(nodes)
    primary_cid = "config:w:pkg/cfg/app.fk#app.properties"
    child_cid = "config:w:pkg/cfg/app.fk#key:app.properties"
    assert primary_cid in by_cid and child_cid in by_cid
    assert primary_cid != child_cid

    contains = [e for e in edges if e["type"] == "CONTAINS"]
    assert any(e["src"] == primary_cid and e["dst"] == child_cid for e in contains)   # 自己ループにならない

    accesses = [e for e in edges if e["type"] == "ACCESSES"]
    assert {e["dst"] for e in accesses} == {child_cid}             # child の1本だけ・primary には張らない
    assert all(e["via"] == "config_key" for e in accesses)
    assert not [f for f in flags if f.get("reason") in ("unresolved", "cross_scope")]


# --- Config キーの名前空間分離（key_kind・裁定2026-09-06）---

def test_config_key_kind_scopes_bean_action_property_namespaces_separately(tmp_path, monkeypatch):
    """Config キーは種別（`extra["key_kind"]`）で名前空間を分ける（裁定2026-09-06）: 同一世代に
    bean/action/property の3種類が同じ裸キー `login` を持っていても、`key_kind="bean"` の参照
    （`@Qualifier("login")` 相当）は bean の1本だけへ接続する——action/property には張らない。
    参照側/定義側どちらかに `key_kind` が無ければ `None` 同士だけ一致する既存の後方互換は
    このテストでは問わない（別テストで A9 全件接続として既に固定済み）。"""
    wd = _world(tmp_path, {
        "pkg/App.fk": "x", "pkg/bean.fk": "b", "pkg/action.fk": "a", "pkg/prop.fk": "p",
    })
    defs = {
        "pkg/App.fk": DefResult(primary=DefItem(label="Module", name="App")),
        "pkg/bean.fk": DefResult(
            primary=DefItem(label="Config", name="bean.fk"),
            children=[DefItem(label="Config", name="login", line=1, cid_key="key:login",
                              extra={"key_kind": "bean"})]),
        "pkg/action.fk": DefResult(
            primary=DefItem(label="Config", name="action.fk"),
            children=[DefItem(label="Config", name="login", line=1, cid_key="key:login",
                              extra={"key_kind": "action"})]),
        "pkg/prop.fk": DefResult(
            primary=DefItem(label="Config", name="prop.fk"),
            children=[DefItem(label="Config", name="login", line=1, cid_key="key:login",
                              extra={"key_kind": "property"})]),
    }
    refs = {
        "pkg/App.fk": RefResult(refs=[
            RefCandidate("ACCESSES", "Config", "login", 1,
                        extra={"via": "config_key", "key_kind": "bean"}),
        ]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    by_cid = _by_cid(nodes)
    bean_cid = next(c for c, n in by_cid.items() if n["path"] == "pkg/bean.fk" and n["name"] == "login")
    accesses = [e for e in edges if e["type"] == "ACCESSES"]
    assert {e["dst"] for e in accesses} == {bean_cid}
    assert all(e["via"] == "config_key" for e in accesses)
    assert not [f for f in flags if f.get("reason") in ("unresolved", "cross_scope", "ambiguous")]


# --- qualified 索引の登録漏れ是正: cid_key == 実際の cid 構築値でも登録する ---

def test_qualified_index_registers_child_even_when_cid_key_equals_the_cid_construction_value(tmp_path, monkeypatch):
    """child は cid が `.key`（＝`cid_key`）で組み立てられるため `cid_key == 実際の cid 構築値` が
    常に成立する——これまではこのケースで `qualified_defs` への登録自体が抜けており、`com.acme.Inner`
    という完全修飾名の参照が単純名 `Inner` へのフォールバックも噛み合わず解決できなかった。
    child `name=Inner, cid_key=com.acme.Inner` を完全修飾名 `com.acme.Inner` で一意に解決できることを
    固定する。"""
    wd = _world(tmp_path, {"pkg/Outer.fk": "x", "pkg/cfg/beans.fk": "z"})
    defs = {
        "pkg/Outer.fk": DefResult(
            primary=DefItem(label="Module", name="Outer"),
            children=[DefItem(label="Module", name="Inner", cid_key="com.acme.Inner")],
        ),
        "pkg/cfg/beans.fk": DefResult(primary=DefItem(label="Config", name="beans.xml")),
    }
    refs = {
        "pkg/cfg/beans.fk": RefResult(refs=[
            RefCandidate("INVOKES", "Module", "com.acme.Inner", 1,
                        extra={"via": "bean_class", "qualified": True}),
        ]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    by_cid = _by_cid(nodes)
    inner_cid = next(c for c, n in by_cid.items() if n["label"] == "Module" and n["name"] == "Inner")
    e = next(e for e in edges if e["type"] == "INVOKES")
    assert e["dst"] == inner_cid
    assert not [f for f in flags if f.get("reason") in ("unresolved", "qualified_fallback", "ambiguous")]


def test_qualified_index_registers_primary_even_when_cid_key_equals_name(tmp_path, monkeypatch):
    """primary も同じ穴を持つ——`cid_key` が `.name` と文字列として同じ値（ドット込みの名前を
    アナライザがそのまま `name`/`cid_key` 両方に入れるケース）でも登録され、完全修飾名で解決できる。"""
    wd = _world(tmp_path, {"pkg/a/Outer.fk": "x", "pkg/cfg/beans.fk": "z"})
    defs = {
        "pkg/a/Outer.fk": DefResult(primary=DefItem(label="Module", name="com.acme.Outer",
                                                    cid_key="com.acme.Outer")),
        "pkg/cfg/beans.fk": DefResult(primary=DefItem(label="Config", name="beans.xml")),
    }
    refs = {
        "pkg/cfg/beans.fk": RefResult(refs=[
            RefCandidate("INVOKES", "Module", "com.acme.Outer", 1,
                        extra={"via": "bean_class", "qualified": True}),
        ]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    by_cid = _by_cid(nodes)
    outer_cid = next(c for c, n in by_cid.items() if n["path"] == "pkg/a/Outer.fk")
    e = next(e for e in edges if e["type"] == "INVOKES")
    assert e["dst"] == outer_cid
    assert not [f for f in flags if f.get("reason") in ("unresolved", "qualified_fallback", "ambiguous")]


# --- qualified 解決の等距離: 同一 top_scope で複数候補が等距離なら ambiguous ---

def test_qualified_equidistant_candidates_are_flagged_ambiguous_not_arbitrarily_resolved(tmp_path, monkeypatch):
    """同一 `cid_key` を持つ2つの定義が参照元から等距離のとき、`_resolve_qualified` は
    `_resolve_nearest` と同じ規律で任意選択せず `ambiguous` を申告する（単純名へのフォールバックにも
    倒さない）。"""
    wd = _world(tmp_path, {
        "gen/a/Foo.fk": "x", "gen/b/Foo.fk": "y", "gen/cfg/beans.fk": "z",
    })
    defs = {
        "gen/a/Foo.fk": DefResult(primary=DefItem(label="Module", name="Foo", cid_key="com.x.Foo")),
        "gen/b/Foo.fk": DefResult(primary=DefItem(label="Module", name="Foo", cid_key="com.x.Foo")),
        "gen/cfg/beans.fk": DefResult(primary=DefItem(label="Config", name="beans.xml")),
    }
    refs = {
        "gen/cfg/beans.fk": RefResult(refs=[
            RefCandidate("INVOKES", "Module", "com.x.Foo", 1,
                        extra={"via": "bean_class", "qualified": True}),
        ]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    assert not [e for e in edges if e["type"] == "INVOKES"]
    assert {"reason": "ambiguous", "from": "gen/cfg/beans.fk", "kind": "Module", "name": "com.x.Foo",
            "line": 1, "via": "bean_class"} in flags
    assert not [f for f in flags if f.get("reason") == "qualified_fallback"]


def test_qualified_candidates_with_a_distance_difference_pick_the_nearest(tmp_path, monkeypatch):
    """等距離ではなく距離差があるときは通常どおり最近傍が一意に選ばれる（ambiguous にならない）。"""
    wd = _world(tmp_path, {
        "gen/cfg/other/Foo.fk": "x", "gen/x/Foo.fk": "y", "gen/cfg/sub/beans.fk": "z",
    })
    defs = {
        "gen/cfg/other/Foo.fk": DefResult(primary=DefItem(label="Module", name="Foo", cid_key="com.x.Foo")),
        "gen/x/Foo.fk": DefResult(primary=DefItem(label="Module", name="Foo", cid_key="com.x.Foo")),
        "gen/cfg/sub/beans.fk": DefResult(primary=DefItem(label="Config", name="beans.xml")),
    }
    refs = {
        "gen/cfg/sub/beans.fk": RefResult(refs=[
            RefCandidate("INVOKES", "Module", "com.x.Foo", 1,
                        extra={"via": "bean_class", "qualified": True}),
        ]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    by_cid = _by_cid(nodes)
    nearer = next(c for c, n in by_cid.items() if n["path"] == "gen/cfg/other/Foo.fk")
    farther = next(c for c, n in by_cid.items() if n["path"] == "gen/x/Foo.fk")
    e = next(e for e in edges if e["type"] == "INVOKES")
    assert e["dst"] == nearer
    assert e["dst"] != farther
    assert not [f for f in flags if f.get("reason") in ("ambiguous", "qualified_fallback", "unresolved")]


# --- C の単純名解決索引（simple_name_defs）は C アナライザ由来の参照だけに限定する ---

def test_c_simple_name_resolution_index_does_not_leak_into_other_languages(tmp_path, monkeypatch):
    """`simple_name_defs`（C の関数呼び出し単純名解決・§9）は登録側（C アナライザ由来の children
    だけ）・参照側（C アナライザ由来かつ `via=call` の参照だけ）の両方を限定する——同一世代に
    C の関数定義（`worker.c` の `Worker`）と Java の未解決参照（`Caller.java` の `new Worker()`）が
    共存しても、Java 側の参照が偶然の同名一致で C の関数へ誤接続してはならない。"""
    wd = _world(tmp_path, {"gen/worker.c": "x", "gen/Caller.java": "y"})
    c_defs = {
        "gen/worker.c": DefResult(
            primary=DefItem(label="Module", name="worker.c"),
            children=[DefItem(label="Module", name="Worker", cid_key="worker.c.Worker", line=1)],
        ),
    }
    java_defs = {
        "gen/Caller.java": DefResult(primary=DefItem(label="Module", name="Caller")),
    }
    java_refs = {
        "gen/Caller.java": RefResult(refs=[
            RefCandidate("INVOKES", "Module", "Worker", 3, extra={"via": "call"}),
        ]),
    }
    c_analyzer = _FakeAnalyzer(c_defs, {}, name="c", extensions=frozenset({".c"}))
    java_analyzer = _FakeAnalyzer(java_defs, java_refs, name="java", extensions=frozenset({".java"}))
    monkeypatch.setattr(registry, "_ANALYZERS", (c_analyzer, java_analyzer))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    by_cid = _by_cid(nodes)
    worker_cid = next(c for c, n in by_cid.items() if n["path"] == "gen/worker.c" and n["name"] == "Worker")
    assert not any(e["type"] == "INVOKES" and e["dst"] == worker_cid for e in edges)
    assert {"reason": "unresolved", "from": "gen/Caller.java", "kind": "Module", "name": "Worker",
            "line": 3, "via": "call"} in flags


def test_simple_name_resolution_index_is_scoped_per_analyzer_not_shared_across_languages(
        tmp_path, monkeypatch):
    """`simple_name_defs` は `(analyzer_name, label, name)` で索引する——
    C・VB のように複数アナライザが `resolves_calls_by_simple_name` を持つ場合でも、同じ単純名
    `FOO` が同一 top_scope に共存すると、登録側の限定（アナライザ由来）だけでは異なる言語間の
    誤接続を防げない（両方とも登録される）。索引キーに `analyzer_name` を含めることで、VB 側の
    `Call FOO` は VB 由来の定義だけを見て C の `FOO` へは接続しない。"""
    wd = _world(tmp_path, {"gen/worker.c": "x", "gen/main.bas": "y"})
    c_defs = {
        "gen/worker.c": DefResult(
            primary=DefItem(label="Module", name="worker.c"),
            children=[DefItem(label="Module", name="FOO", cid_key="worker.c.FOO", line=1,
                              extra={"c_kind": "definition"})],
        ),
    }
    vb_defs = {
        "gen/main.bas": DefResult(primary=DefItem(label="Module", name="Main")),
    }
    vb_refs = {
        "gen/main.bas": RefResult(refs=[
            RefCandidate("INVOKES", "Module", "FOO", 1, extra={"via": "call"}),
        ]),
    }
    c_analyzer = _FakeAnalyzer(c_defs, {}, name="c", extensions=frozenset({".c"}))
    c_analyzer.resolves_calls_by_simple_name = True
    vb_analyzer = _FakeAnalyzer(vb_defs, vb_refs, name="vb", extensions=frozenset({".bas"}))
    vb_analyzer.resolves_calls_by_simple_name = True
    monkeypatch.setattr(registry, "_ANALYZERS", (c_analyzer, vb_analyzer))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    by_cid = _by_cid(nodes)
    foo_cid = next(c for c, n in by_cid.items() if n["path"] == "gen/worker.c" and n["name"] == "FOO")
    assert not any(e["type"] == "INVOKES" and e["dst"] == foo_cid for e in edges)
    assert {"reason": "unresolved", "from": "gen/main.bas", "kind": "Module", "name": "FOO",
            "line": 1, "via": "call"} in flags


# --- 既存 fixtures 不変（COBOL/JCL/コピーブック・byte 単位で挙動不変・§8） ---

def test_v1_fixture_graph_is_unaffected_by_the_new_common_layer_mechanisms():
    """既存 fixtures（`fixtures/corpus/v1`）は Config/reverse/qualified/config_key のいずれも
    使わないため、集約規則（§4(f)）を通しても edges の中身は不変——集約は同一 (src,type,dst) が
    複数あるときだけ効くため、重複の無い既存グラフには影響しない。"""
    wd = ROOT / "fixtures" / "corpus" / "v1"
    nodes, edges, flags = world_graph.build_world(wd, "v1_s3a_regress_test")
    assert edges, "COBOL/JCL コーパスなら少なくとも1本はエッジが立つはず"
    keys = [(e["type"], e["src"], e["dst"]) for e in edges]
    assert len(keys) == len(set(keys)), "既存コーパスに (src,type,dst) の重複が無いことの前提確認"


# --- golden 固定: v1/java1 の nodes/edges/flags を丸ごとピン留めする（§8 受け入れ条件の書き換え） ---
#
# 受け入れ条件は「既存 fixtures が byte 単位で不変」から、§4(f) のエッジ集約規則を全 Pass2 エッジへ
# 適用する前提の下で「nodes/flags は完全不変・edges は (src,type,dst) の集合が不変（重複していた組は
# 1本に畳まれる分だけ本数が減り得る）・重複していた組の via/line だけ VIA_PRIORITY と最初の出現順で
# 決まる」に読み替えた（提案書 §8）。golden はこの読み替え後の実際の出力をそのまま固定する。

V1_WORLD_ID = "v1_s3a_regress_test"
JAVA1_WORLD_ID = "java1_test"


def _snapshot(world_dir, world_id: str) -> dict:
    """`build_world` の出力を golden 比較用に正規化する（nodes は cid・edges は (type,src,dst)・
    flags は JSON 文字列でソートし、辞書の内部反復順に依存しない安定した表現にする）。"""
    # 言及エッジ（Pass3）の閾値は env で動くため、golden の基準値（既定値）に固定して比較・再生成の
    # 条件を揃える（環境の設定次第で偽失敗し、その環境で再生成すると誤った golden が確定してしまう）。
    pinned = {"SHERPA_MENTION_MIN_LEN": "4", "SHERPA_MENTION_MAX_PER_DOC": "200"}
    saved = {k: os.environ.get(k) for k in pinned}
    os.environ.update(pinned)
    try:
        nodes, edges, flags = world_graph.build_world(world_dir, world_id)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return {
        "nodes": sorted(nodes, key=lambda n: n["cid"]),
        "edges": sorted(edges, key=lambda e: (e["type"], e["src"], e["dst"])),
        "flags": sorted(flags, key=lambda f: json.dumps(f, sort_keys=True, ensure_ascii=False)),
    }


def _load_golden(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_goldens() -> None:
    """現状の v1/java1 グラフから golden を書き出す（更新手順は module docstring 参照）。"""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    v1 = _snapshot(ROOT / "fixtures" / "corpus" / "v1", V1_WORLD_ID)
    java1 = _snapshot(ROOT / "fixtures" / "corpus" / "java1", JAVA1_WORLD_ID)
    V1_GOLDEN.write_text(json.dumps(v1, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    JAVA1_GOLDEN.write_text(json.dumps(java1, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@pytest.mark.usefixtures("upstream_only_registry")
def test_v1_fixture_graph_matches_golden_snapshot():
    """v1（COBOL/JCL/コピーブック）の nodes/edges/flags を golden で丸ごとピン留めする。

    上流限定固定（`upstream_only_registry`）——フォークが `.md`/`.cbl` 等を担当する拡張アナライザを
    登録すると実 fixture（`fixtures/corpus/v1`）のグラフ構造自体が変わり golden と一致しなくなる
    ため（開発ハーネス S4・敵対 RV 是正）。"""
    actual = _snapshot(ROOT / "fixtures" / "corpus" / "v1", V1_WORLD_ID)
    assert actual == _load_golden(V1_GOLDEN)


def test_java1_fixture_graph_matches_golden_snapshot_with_9_aggregated_edges():
    """java1 の nodes/edges/flags を golden で丸ごとピン留めする——エッジ集約（§4(f)）適用後は
    13本の Pass2 候補が9本（4組の重複が1本ずつに畳まれる）になる（下記
    `test_java1_edge_aggregation_preserves_the_src_type_dst_set` で集合不変を別途検証する）。"""
    actual = _snapshot(ROOT / "fixtures" / "corpus" / "java1", JAVA1_WORLD_ID)
    golden = _load_golden(JAVA1_GOLDEN)
    assert len(actual["edges"]) == 9
    assert actual == golden


def test_java1_edge_aggregation_preserves_the_src_type_dst_set(monkeypatch):
    """§8 受け入れ条件の核心: 集約規則（`_aggregate_pass2_edges`）を無効化した「集約前」の生エッジと、
    通常どおり有効化した「集約後」の golden エッジを比べ、(1) 集約前は13本・集約後は9本
    （4組が重複していた）、(2) 集約後に残る `(src,type,dst)` の集合は集約前の集合と完全に一致する
    （集約は重複を1本へ畳むだけで、新しい組を作ったり既存の組を消したりしない）ことを検証する。"""
    monkeypatch.setattr(world_graph, "_aggregate_pass2_edges", lambda raw: raw)
    _nodes, before_edges, _flags = world_graph.build_world(
        ROOT / "fixtures" / "corpus" / "java1", JAVA1_WORLD_ID)
    monkeypatch.undo()

    after = _load_golden(JAVA1_GOLDEN)
    after_edges = after["edges"]

    assert len(before_edges) == 13
    assert len(after_edges) == 9

    before_keys = [(e["type"], e["src"], e["dst"]) for e in before_edges]
    after_keys = [(e["type"], e["src"], e["dst"]) for e in after_edges]
    assert set(before_keys) == set(after_keys), "集約は (src,type,dst) の集合を変えない"

    # 集約前に重複していた（＝集約後に1本へ畳まれた）4組——line/via だけが変わり得る組。
    dup_keys = {k for k, n in Counter(before_keys).items() if n > 1}
    assert len(dup_keys) == 4
    assert dup_keys == set(after_keys) & dup_keys, "畳まれた組は集約後の集合にもそのまま残っている"
