"""アナライザ拡張の共通層（`world_graph`）契約テスト。

Java/XML に依存しない最小のフェイクアナライザで `registry._ANALYZERS` を monkeypatch し、
共通層だけを検証する（`tests/unit/test_world_graph_edge_extra.py` と同じ流儀）。

golden（`tests/unit/goldens/world_graph_v1.json`・`world_graph_java1.json`）は**意図した増減のときだけ**
次で更新し、差分を目視確認してからコミットする（言及閾値は `_snapshot` 内で既定値 4/200 に固定）:

    SHERPA_USE_FIXTURES=1 PYTHONPATH=. .venv/bin/python -c \
        "import sys; sys.path.insert(0, 'tests/unit'); \
         import test_world_graph_analyzer_expansion_common as t; t._write_goldens()"
"""
from __future__ import annotations

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
    `name`/`extensions` を差し替えれば、複数言語が混在する world も模せる。"""

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


def _build(tmp_path, monkeypatch, defs, refs=None, *, analyzers=None):
    """defs/refs のキー（rel_path）のファイルを持つ world を作り、フェイクで build_world する。
    戻り値は (cid→node, edges, flags)。"""
    refs = refs or {}
    wd = tmp_path / "world"
    wd.mkdir()
    for rel in [*defs, *refs]:
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    monkeypatch.setattr(registry, "_ANALYZERS", analyzers or (_FakeAnalyzer(defs, refs),))
    nodes, edges, flags = world_graph.build_world(wd, "w")
    return {n["cid"]: n for n in nodes}, edges, flags


def _cid(by_cid, **match):
    return next(c for c, n in by_cid.items() if all(n[k] == v for k, v in match.items()))


def _module(name, **kw):
    return DefResult(primary=DefItem(label="Module", name=name, **kw))


def _config(name, children=()):
    return DefResult(primary=DefItem(label="Config", name=name), children=list(children))


def _reasons(flags, *reasons):
    return [f for f in flags if f.get("reason") in reasons]


# --- Config ラベル・cid 規則 ---

def test_config_label_is_accepted_and_gets_the_generic_path_scoped_cid(tmp_path, monkeypatch):
    """`Config` は `unknown_label` に落ちず、cid は一般規則 `config:{world}:{rel}#{name}`。"""
    from sherpa.ingest import model
    assert "Config" in registry.NODE_LABELS and "Config" in model.NODE_LABELS

    by_cid, _edges, flags = _build(tmp_path, monkeypatch, {"pkg/cfg/app.fk": _config("app_config")})
    n = by_cid["config:w:pkg/cfg/app.fk#app_config"]
    assert n["label"] == "Config" and n["path"] == "pkg/cfg/app.fk"
    assert not _reasons(flags, "unknown_label", "unknown_edge_type")


# --- 逆向きエッジ（RefCandidate.reverse） ---

def test_reverse_ref_candidate_flips_edge_direction_to_resolved_target_to_primary(tmp_path, monkeypatch):
    """Mapper XML を模す: Config primary から `reverse=True` の参照を出すと、エッジは
    「解決先（Module）→この Config」の向きで張られる（解決規則自体は変わらない）。"""
    by_cid, edges, flags = _build(
        tmp_path, monkeypatch,
        {"pkg/cfg/mapper.fk": _config("mapper.xml"), "pkg/svc/OrderMapper.fk": _module("OrderMapper")},
        {"pkg/cfg/mapper.fk": RefResult(refs=[
            RefCandidate("INVOKES", "Module", "OrderMapper", 3,
                         extra={"via": "mapper_namespace"}, reverse=True)])})
    e = next(e for e in edges if e["type"] == "INVOKES")
    assert e["src"] == _cid(by_cid, label="Module") and e["dst"] == _cid(by_cid, label="Config")
    assert e["via"] == "mapper_namespace"
    assert flags == []


# --- 完全修飾名の2段解決（qualified） ---

def _qualified_ref(name):
    return RefResult(refs=[RefCandidate("INVOKES", "Module", name, 1,
                                        extra={"via": "bean_class", "qualified": True})])


@pytest.mark.parametrize("defs,ref_from,ref_name,target", [
    # 同名・別 package（cid_key が異なる）は完全一致で一意に解決（単純名の最近傍に落ちない）
    ({"pkg/a/Foo.fk": _module("Foo", cid_key="com.a.Foo"),
      "pkg/b/Foo.fk": _module("Foo", cid_key="com.b.Foo"),
      "pkg/cfg/beans.fk": _config("beans.xml")},
     "pkg/cfg/beans.fk", "com.a.Foo", {"path": "pkg/a/Foo.fk"}),
    # child は cid が cid_key で組み立てられる（cid_key == cid 構築値）ため、登録漏れがあると解決できない
    ({"pkg/Outer.fk": DefResult(primary=DefItem(label="Module", name="Outer"),
                                children=[DefItem(label="Module", name="Inner", cid_key="com.acme.Inner")]),
      "pkg/cfg/beans.fk": _config("beans.xml")},
     "pkg/cfg/beans.fk", "com.acme.Inner", {"label": "Module", "name": "Inner"}),
    # primary も cid_key が name と同じ文字列でも登録される
    ({"pkg/a/Outer.fk": _module("com.acme.Outer", cid_key="com.acme.Outer"),
      "pkg/cfg/beans.fk": _config("beans.xml")},
     "pkg/cfg/beans.fk", "com.acme.Outer", {"path": "pkg/a/Outer.fk"}),
    # 距離差があれば最近傍が一意に選ばれる（ambiguous にならない）
    ({"gen/cfg/other/Foo.fk": _module("Foo", cid_key="com.x.Foo"),
      "gen/x/Foo.fk": _module("Foo", cid_key="com.x.Foo"),
      "gen/cfg/sub/beans.fk": _config("beans.xml")},
     "gen/cfg/sub/beans.fk", "com.x.Foo", {"path": "gen/cfg/other/Foo.fk"}),
])
def test_qualified_reference_resolves_by_exact_name_then_nearest(tmp_path, monkeypatch, defs, ref_from,
                                                                 ref_name, target):
    by_cid, edges, flags = _build(tmp_path, monkeypatch, defs, {ref_from: _qualified_ref(ref_name)})
    e = next(e for e in edges if e["type"] == "INVOKES")
    assert e["dst"] == _cid(by_cid, **target)
    assert "qualified" not in e                        # 解決の指示であってエッジの事実ではない
    assert not _reasons(flags, "unresolved", "qualified_fallback", "ambiguous")


def test_qualified_miss_falls_back_to_simple_name_and_flags_qualified_fallback(tmp_path, monkeypatch):
    """完全一致が無ければ単純名の通常の最近傍へフォールバックし、`qualified_fallback` を記録する
    （黙って倒さない）。"""
    by_cid, edges, flags = _build(
        tmp_path, monkeypatch,
        {"pkg/x/Foo.fk": _module("Foo"), "pkg/cfg/beans.fk": _config("beans.xml")},   # cid_key 無し＝未登録
        {"pkg/cfg/beans.fk": _qualified_ref("com.unknown.Foo")})
    e = next(e for e in edges if e["type"] == "INVOKES")
    assert e["dst"] == _cid(by_cid, path="pkg/x/Foo.fk")
    assert {"reason": "qualified_fallback", "from": "pkg/cfg/beans.fk",
            "kind": "Module", "name": "com.unknown.Foo"} in flags


def test_qualified_equidistant_candidates_are_flagged_ambiguous_not_arbitrarily_resolved(tmp_path, monkeypatch):
    """同一 `cid_key` の2定義が等距離なら任意選択せず `ambiguous` を申告する（単純名へも倒さない）。"""
    _by_cid, edges, flags = _build(
        tmp_path, monkeypatch,
        {"gen/a/Foo.fk": _module("Foo", cid_key="com.x.Foo"),
         "gen/b/Foo.fk": _module("Foo", cid_key="com.x.Foo"),
         "gen/cfg/beans.fk": _config("beans.xml")},
        {"gen/cfg/beans.fk": _qualified_ref("com.x.Foo")})
    assert not [e for e in edges if e["type"] == "INVOKES"]
    assert {"reason": "ambiguous", "from": "gen/cfg/beans.fk", "kind": "Module", "name": "com.x.Foo",
            "line": 1, "via": "bean_class"} in flags
    assert not _reasons(flags, "qualified_fallback")


# --- エッジ集約規則 ---

@pytest.mark.parametrize("candidates,line", [
    ([("call", 1), ("bean_class", 7)], 7),
    ([("bean_class", 9), ("call", 2)], 9),   # 候補の順序に依らない（後勝ちでなく優先順位比較）
])
def test_same_src_type_dst_candidates_collapse_with_fw_specific_via_winning(tmp_path, monkeypatch,
                                                                            candidates, line):
    """同一 (src,type,dst) に汎用 via（`call`）と FW 固有 via（`bean_class`）が来たら 1本へ集約し、
    FW 固有側（`VIA_PRIORITY` で優先）の via/line を採用する。"""
    _by_cid, edges, flags = _build(
        tmp_path, monkeypatch, {"pkg/a.fk": _module("A"), "pkg/b.fk": _module("B")},
        {"pkg/a.fk": RefResult(refs=[RefCandidate("INVOKES", "Module", "B", ln, extra={"via": via})
                                     for via, ln in candidates])})
    invokes = [e for e in edges if e["type"] == "INVOKES"]
    assert len(invokes) == 1
    assert invokes[0]["via"] == "bean_class" and invokes[0]["line"] == line
    assert flags == []


# --- config_key 全件接続 ---

def _config_ref(name, **extra):
    return RefResult(refs=[RefCandidate("ACCESSES", "Config", name, 1, extra={"via": "config_key", **extra})])


def _key_child(name, key_kind=None, line=1):
    return DefItem(label="Config", name=name, line=line, cid_key=f"key:{name}",
                   extra={"key_kind": key_kind} if key_kind else {})


def test_config_key_via_connects_to_every_same_name_config_in_the_same_generation(tmp_path, monkeypatch):
    """`via=config_key` は最近傍/ambiguous 判定を迂回し、同一 top_scope 内の同名 Config キー child
    （environment-dev/prod 相当の複数ファイル）全件へ1本ずつ張る。解決先は常にキー child で、
    ファイル自体を表す primary には張らない。"""
    by_cid, edges, flags = _build(
        tmp_path, monkeypatch,
        {"pkg/App.fk": _module("App"),
         "pkg/dev/db.fk": _config("db.fk", [_key_child("db.url")]),
         "pkg/prod/db.fk": _config("db.fk", [_key_child("db.url")])},
        {"pkg/App.fk": _config_ref("db.url")})
    accesses = [e for e in edges if e["type"] == "ACCESSES"]
    assert {e["dst"] for e in accesses} == {
        _cid(by_cid, path="pkg/dev/db.fk", name="db.url"), _cid(by_cid, path="pkg/prod/db.fk", name="db.url")}
    assert all(e["via"] == "config_key" for e in accesses)
    assert not _reasons(flags, "ambiguous")


def test_config_key_unresolved_when_no_config_matches_in_scope(tmp_path, monkeypatch):
    """同世代に定義が無いケースも黙って落とさず `unresolved` を flags へ記録する。"""
    _by_cid, edges, flags = _build(tmp_path, monkeypatch, {"pkg/App.fk": _module("App")},
                                   {"pkg/App.fk": _config_ref("db.url")})
    assert not [e for e in edges if e["type"] == "ACCESSES"]
    assert {"reason": "unresolved", "from": "pkg/App.fk", "kind": "Config", "name": "db.url",
            "line": 1, "via": "config_key"} in flags


def test_config_key_dst_cid_uses_child_cid_key_not_bare_name_when_it_collides_with_a_primary(
        tmp_path, monkeypatch):
    """properties/YAML のキー child は `cid_key="key:"+裸キー`（primary との自己ループ回避）を持つ。
    primary と同じ裸名の child でも、全件接続は child の cid_key で dst cid を組み立てる
    （裸の name だと primary の cid に誤着地していた）。"""
    by_cid, edges, flags = _build(
        tmp_path, monkeypatch,
        {"pkg/cfg/app.fk": _config("app.properties", [_key_child("app.properties", line=3)]),
         "pkg/App.fk": _module("App")},
        {"pkg/App.fk": _config_ref("app.properties")})
    primary_cid = "config:w:pkg/cfg/app.fk#app.properties"
    child_cid = "config:w:pkg/cfg/app.fk#key:app.properties"
    assert primary_cid in by_cid and child_cid in by_cid

    assert any(e["src"] == primary_cid and e["dst"] == child_cid
               for e in edges if e["type"] == "CONTAINS")   # 自己ループにならない
    accesses = [e for e in edges if e["type"] == "ACCESSES"]
    assert {e["dst"] for e in accesses} == {child_cid}      # child の1本だけ・primary には張らない
    assert all(e["via"] == "config_key" for e in accesses)
    assert not _reasons(flags, "unresolved", "cross_scope")


def test_config_key_kind_scopes_bean_action_property_namespaces_separately(tmp_path, monkeypatch):
    """Config キーは種別（`extra["key_kind"]`）で名前空間を分ける: bean/action/property が同じ裸キー
    `login` を持っていても、`key_kind="bean"` の参照は bean の1本だけへ接続する。"""
    by_cid, edges, flags = _build(
        tmp_path, monkeypatch,
        {"pkg/App.fk": _module("App"),
         "pkg/bean.fk": _config("bean.fk", [_key_child("login", "bean")]),
         "pkg/action.fk": _config("action.fk", [_key_child("login", "action")]),
         "pkg/prop.fk": _config("prop.fk", [_key_child("login", "property")])},
        {"pkg/App.fk": _config_ref("login", key_kind="bean")})
    accesses = [e for e in edges if e["type"] == "ACCESSES"]
    assert {e["dst"] for e in accesses} == {_cid(by_cid, path="pkg/bean.fk", name="login")}
    assert all(e["via"] == "config_key" for e in accesses)
    assert not _reasons(flags, "unresolved", "cross_scope", "ambiguous")


# --- 単純名解決索引（simple_name_defs）は解決を許す言語の同一アナライザ内に限定する ---

def test_c_simple_name_resolution_index_does_not_leak_into_other_languages(tmp_path, monkeypatch):
    """C の関数呼び出し単純名解決は、登録側（C 由来の children だけ）・参照側（C 由来かつ `via=call`
    だけ）の両方を限定する——C の関数定義と Java の未解決参照が同一世代に共存しても、偶然の同名一致で
    誤接続しない。"""
    c_analyzer = _FakeAnalyzer(
        {"gen/worker.c": DefResult(
            primary=DefItem(label="Module", name="worker.c"),
            children=[DefItem(label="Module", name="Worker", cid_key="worker.c.Worker", line=1)])},
        {}, name="c", extensions=frozenset({".c"}))
    java_analyzer = _FakeAnalyzer(
        {"gen/Caller.java": _module("Caller")},
        {"gen/Caller.java": RefResult(refs=[
            RefCandidate("INVOKES", "Module", "Worker", 3, extra={"via": "call"})])},
        name="java", extensions=frozenset({".java"}))
    by_cid, edges, flags = _build(
        tmp_path, monkeypatch, {"gen/worker.c": None, "gen/Caller.java": None},
        analyzers=(c_analyzer, java_analyzer))
    worker_cid = _cid(by_cid, path="gen/worker.c", name="Worker")
    assert not any(e["type"] == "INVOKES" and e["dst"] == worker_cid for e in edges)
    assert {"reason": "unresolved", "from": "gen/Caller.java", "kind": "Module", "name": "Worker",
            "line": 3, "via": "call"} in flags


def test_simple_name_resolution_index_is_scoped_per_analyzer_not_shared_across_languages(
        tmp_path, monkeypatch):
    """`simple_name_defs` は `(analyzer_name, label, name)` で索引する——C・VB のように複数
    アナライザが `resolves_calls_by_simple_name` を持ち同じ単純名 `FOO` が共存しても、VB 側の
    `Call FOO` は VB 由来の定義だけを見て C の `FOO` へは接続しない。"""
    c_analyzer = _FakeAnalyzer(
        {"gen/worker.c": DefResult(
            primary=DefItem(label="Module", name="worker.c"),
            children=[DefItem(label="Module", name="FOO", cid_key="worker.c.FOO", line=1,
                              extra={"c_kind": "definition"})])},
        {}, name="c", extensions=frozenset({".c"}))
    c_analyzer.resolves_calls_by_simple_name = True
    vb_analyzer = _FakeAnalyzer(
        {"gen/main.bas": _module("Main")},
        {"gen/main.bas": RefResult(refs=[RefCandidate("INVOKES", "Module", "FOO", 1, extra={"via": "call"})])},
        name="vb", extensions=frozenset({".bas"}))
    vb_analyzer.resolves_calls_by_simple_name = True
    by_cid, edges, flags = _build(
        tmp_path, monkeypatch, {"gen/worker.c": None, "gen/main.bas": None},
        analyzers=(c_analyzer, vb_analyzer))
    foo_cid = _cid(by_cid, path="gen/worker.c", name="FOO")
    assert not any(e["type"] == "INVOKES" and e["dst"] == foo_cid for e in edges)
    assert {"reason": "unresolved", "from": "gen/main.bas", "kind": "Module", "name": "FOO",
            "line": 1, "via": "call"} in flags


# --- 既存 fixtures 不変（COBOL/JCL/コピーブック・java1） ---

def test_v1_fixture_graph_is_unaffected_by_the_new_common_layer_mechanisms():
    """既存 fixtures（`fixtures/corpus/v1`）は Config/reverse/qualified/config_key を使わないため、
    集約規則を通しても重複の無い既存グラフの edges は不変。"""
    nodes, edges, flags = world_graph.build_world(ROOT / "fixtures" / "corpus" / "v1", "v1_s3a_regress_test")
    assert edges, "COBOL/JCL コーパスなら少なくとも1本はエッジが立つはず"
    keys = [(e["type"], e["src"], e["dst"]) for e in edges]
    assert len(keys) == len(set(keys)), "既存コーパスに (src,type,dst) の重複が無いことの前提確認"


# --- golden 固定: v1/java1 の nodes/edges/flags を丸ごとピン留めする ---
# nodes/flags は完全不変・edges は (src,type,dst) の集合が不変（重複していた組は1本に畳まれる分だけ
# 減り得る）・重複していた組の via/line は VIA_PRIORITY と最初の出現順で決まる。

V1_WORLD_ID = "v1_s3a_regress_test"
JAVA1_WORLD_ID = "java1_test"


def _snapshot(world_dir, world_id: str) -> dict:
    """`build_world` の出力を golden 比較用に正規化する（辞書の内部反復順に依存しない表現）。"""
    nodes, edges, flags = world_graph.build_world(world_dir, world_id)
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
    """v1 の nodes/edges/flags を golden で丸ごとピン留めする。上流限定固定——フォークが
    `.md`/`.cbl` 等を担当する拡張アナライザを登録すると fixture のグラフ構造自体が変わるため。"""
    actual = _snapshot(ROOT / "fixtures" / "corpus" / "v1", V1_WORLD_ID)
    assert actual == _load_golden(V1_GOLDEN)


def test_java1_fixture_graph_matches_golden_snapshot_with_9_aggregated_edges():
    """java1 の nodes/edges/flags を golden で丸ごとピン留めする——エッジ集約後は 13本の Pass2 候補が
    9本（4組の重複が1本ずつに畳まれる）になる。"""
    actual = _snapshot(ROOT / "fixtures" / "corpus" / "java1", JAVA1_WORLD_ID)
    golden = _load_golden(JAVA1_GOLDEN)
    assert len(actual["edges"]) == 9
    assert actual == golden


def test_java1_edge_aggregation_preserves_the_src_type_dst_set(monkeypatch):
    """集約規則（`_aggregate_pass2_edges`）を無効化した「集約前」の生エッジと golden（集約後）を比べ、
    (1) 集約前13本・集約後9本（4組が重複）、(2) 残る `(src,type,dst)` の集合は集約前と完全一致
    （集約は重複を畳むだけで新しい組を作らず既存の組を消さない）ことを検証する。"""
    monkeypatch.setattr(world_graph, "_aggregate_pass2_edges", lambda raw: raw)
    _nodes, before_edges, _flags = world_graph.build_world(
        ROOT / "fixtures" / "corpus" / "java1", JAVA1_WORLD_ID)
    monkeypatch.undo()

    after_edges = _load_golden(JAVA1_GOLDEN)["edges"]
    assert len(before_edges) == 13
    assert len(after_edges) == 9

    before_keys = [(e["type"], e["src"], e["dst"]) for e in before_edges]
    after_keys = [(e["type"], e["src"], e["dst"]) for e in after_edges]
    assert set(before_keys) == set(after_keys), "集約は (src,type,dst) の集合を変えない"

    dup_keys = {k for k, n in Counter(before_keys).items() if n > 1}
    assert len(dup_keys) == 4
    assert dup_keys == set(after_keys) & dup_keys, "畳まれた組は集約後の集合にもそのまま残っている"
