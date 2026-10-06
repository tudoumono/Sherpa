"""ANA-14 S2（呼び出し元を定義の単位で持つ）の受入契約。

比較対象は S2 着手前の `build_world()` 出力（`goldens/ana14_baseline_pre_s2.json`・fixtures/corpus/* と fixtures/mirror の各 world）。
差分は「辺の始点が、ファイルの主体から参照を含む定義ノードへ移る」行だけであることを確かめる。
"""
from __future__ import annotations

import json
import pathlib
from collections import Counter, defaultdict, deque

import pytest

from sherpa.ingest import world_graph
from sherpa.ingest.analyzers.java import JavaAnalyzer
from sherpa.ingest.analyzers._base import RefResult

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASELINE = json.loads((ROOT / "tests" / "contract" / "goldens" / "ana14_baseline_pre_s2.json")
                      .read_text(encoding="utf-8"))["worlds"]


# S3 で、namespace の中の VB.NET の手続きの定義キーが `<Namespace>.<型>.<手続き>` になった（同じファイルの別 namespace の同名の型・手続きを区別するため）。
# S2 着手前の出力の cid を、同じ定義の新しい cid へ読み替える（`module:<資料フォルダ>:<パス>#<キー>` の `#` の後だけ）。
S3_VB_PROC_KEY_RENAMES = {
    "vb/Shop.vb": {"CART.ADD": "ACME.SHOP.CART.ADD", "CART.RECALC": "ACME.SHOP.CART.RECALC",
                   "HELPER.ASSIST": "ACME.SHOP.HELPER.ASSIST"},
    "net/Api/ApiClient.vb": {"APICLIENT.CONNECT": "ACME.API.APICLIENT.CONNECT", "APICLIENT.LOAD": "ACME.API.APICLIENT.LOAD"},
    "net/Order/OrderService.vb": {f"ORDERSERVICE.{k}": f"ACME.ORDER.ORDERSERVICE.{k}"
                                  for k in ("LOGSTART", "NEW", "PROCESS")},
}


def _s3_rename(value):
    if isinstance(value, str) and value.startswith("module:") and "#" in value:
        head, key = value.rsplit("#", 1)
        path = head.split(":", 2)[2]
        return f"{head}#{S3_VB_PROC_KEY_RENAMES.get(path, {}).get(key, key)}"
    if isinstance(value, list):
        return [_s3_rename(v) for v in value]
    if isinstance(value, dict):
        return {_s3_rename(k): _s3_rename(v) for k, v in value.items()}
    return value


BASELINE = _s3_rename(BASELINE)
S2_WORLD = "ana-s2"
S2_DIR = ROOT / "fixtures" / "corpus" / S2_WORLD


def _world_dir(name: str) -> pathlib.Path:
    return ROOT / "fixtures" / "mirror" if name == "mirror" else ROOT / "fixtures" / "corpus" / name


def _build(name: str):
    return world_graph.build_world(_world_dir(name), name)


def _path(cid: str) -> str:
    """cid（`label:world:rel#key`）→ 所属ファイルの rel_path。"""
    return cid.split(":", 2)[2].rsplit("#", 1)[0]


def _short(cid: str) -> str:
    return cid.split(":", 2)[2]


# S3（名前の解決を言語の規則に従わせる）で変わった行。S2 の比較対象（S2 着手前の出力）に対する差分として明示する。
# 未解決の申告の行 `[reason, from, kind, name, line]`: 消えた行（解決できるようになった・理由が `unresolved_qualifier` へ変わった）と、足された行。
S3_UNRESOLVED_REMOVED = {
    "ana-s2": [["unresolved", "cs/Two.cs", "Module", "Beta", 5],
               ["unresolved", "vb/Shop.vb", "Module", "HELPER", 5]],
    "batch1": [["unresolved", "gen1/batch-context.xml", "Module", "PayrollTasklet", 8]],
    "cs1": [["unresolved", "Acme/Misc/BoxHost.cs", "Module", "Box", 5]],
    "java1": [["ambiguous", "com/acme/Main.java", "Module", "Helper", 9]],
}
S3_UNRESOLVED_ADDED = {
    "batch1": [["unresolved_qualifier", "gen1/batch-context.xml", "Module", "com.acme.PayrollTasklet", 8]],
    "cs1": [["unresolved_qualifier", "Acme/Misc/BoxHost.cs", "Module", "B.Box", 5]],
    "java1": [["unresolved", "com/acme/Main.java", "Module", "Helper", 9]],
}
# S3 で新しく張られる辺（同じファイル・同じ namespace の型への参照が、完全修飾名で引けるようになった）。
S3_NEW_EDGES = {
    ("cs/Two.cs#Alpha", "INVOKES", "cs/Two.cs#Acme.App.Beta", "field_type", 5),
    ("vb/Shop.vb#ACME.SHOP.CART.ADD", "INVOKES", "vb/Shop.vb#ACME.SHOP.HELPER", "field_type", 5),
}


@pytest.fixture(scope="module", params=sorted(BASELINE))
def built(request):
    nodes, edges, flags = _build(request.param)
    return request.param, nodes, edges, flags


def test_node_cids_and_unresolved_reports_are_unchanged(built):
    """既存の定義ノードの cid は変わらない。未解決・曖昧の申告も、S3 の差分（`S3_UNRESOLVED_*`）を除いて変わらない（辞書化した `(reason, from, kind, name, line)`）。"""
    name, nodes, _edges, flags = built
    base = BASELINE[name]
    assert sorted(n["cid"] for n in nodes) == sorted(base["nodes"])
    unresolved = sorted(([f["reason"], f.get("from"), f.get("kind"), f.get("name"), f.get("line")]
                         for f in flags if f["reason"] in ("unresolved", "ambiguous", "cross_scope", "unresolved_qualifier")),
                        key=lambda r: [str(x) for x in r])
    expected = [r for r in base["unresolved"] if r not in S3_UNRESOLVED_REMOVED.get(name, [])]
    expected += S3_UNRESOLVED_ADDED.get(name, [])
    assert unresolved == sorted(expected, key=lambda r: [str(x) for x in r])


_IMPACT = {"COPIES", "CONTAINS", "INVOKES", "ACCESSES"}


def _affected_paths(edges, depth: int = 10) -> dict:
    """終点ノード → 影響ノード（構造の辺を逆向きに深さ `depth` まで）の所属ファイル集合。`world_neo4j.world_impact` と同じ辿り方の純関数。"""
    callers = defaultdict(set)
    for e in edges:
        if e["type"] in _IMPACT:
            callers[e["dst"]].add(e["src"])
    out = {}
    for start in {e["dst"] for e in edges if e["type"] in _IMPACT}:
        seen, frontier = {start}, {start}
        for _ in range(depth):
            frontier = {s for c in frontier for s in callers.get(c, ())} - seen
            seen |= frontier
        seen.discard(start)
        out[start] = sorted({_path(c) for c in seen})
    return out


# 影響たどりで新しく届くようになったファイル（呼ばれた関数の先の依存まで、呼び出し元の関数から辿れるようになった分）。
S2_AFFECTED_GAINS = {
    "ana-s2": {"c3/bar.c#bar.c.bar": ["c3/main.c"]},
    "vb1": {f"vb6/schema.sql#{k}": ["vb6/frmMain.frm", "vb6/modMain.bas"]
            for k in ("ORDERS", "ORDERS.ID", "ORDERS.STATUS")},
}


def test_edges_differ_from_baseline_only_by_start_node_within_the_same_file(built):
    """旧辺 `(src,type,dst,via,line)` ごとに、始点だけが同じファイルの定義ノードへ移った辺が新側にある。
    辺は件数込みで比べる（二重の辺を作らない・新側にだけある辺は ana-s2 の表にある 1 本だけ）。影響たどりの `affected.path` 集合は新旧で減らず、増えるのは `S2_AFFECTED_GAINS` の分だけ。"""
    name, _nodes, edges, _flags = built
    # 言及はトップフォルダの中だけに張る（docs/03-鏡モデル.md §2.4）。基準に残るトップフォルダをまたぐ言及は比べない。
    old = Counter(tuple(e) for e in BASELINE[name]["edges"]
                  if not (e[3] == "mention" and _path(e[0]).split("/")[0] != _path(e[2]).split("/")[0]))
    new_list = [(e["src"], e["type"], e["dst"], e.get("via"), e.get("line")) for e in edges]
    new_list = [e for e in new_list if (_short(e[0]), e[1], _short(e[2]), e[3], e[4]) not in S3_NEW_EDGES]
    new = Counter(new_list)
    assert max(new.values()) == 1, [k for k, v in new.items() if v > 1]
    assert max(old.values()) == 1
    new_by_rest = defaultdict(set)
    for src, etype, dst, via, line in new:
        new_by_rest[(etype, dst, via, line)].add(_path(src))
    for src, etype, dst, via, line in old:
        assert _path(src) in new_by_rest[(etype, dst, via, line)], (src, etype, dst, via, line)
    to_file = lambda c: Counter((_path(s), t, _path(d), v, ln) for s, t, d, v, ln in c)  # noqa: E731
    extra = to_file(new) - to_file(old)
    assert not (to_file(old) - to_file(new))
    assert sum(extra.values()) == (len(S2_NEW_EDGES) if name == S2_WORLD else 0), extra
    new_aff, old_aff = _affected_paths(edges), BASELINE[name]["affected_paths"]
    assert all(set(paths) <= set(new_aff.get(cid, ())) for cid, paths in old_aff.items())   # 影響は減らない
    gains = {_short(cid): sorted(set(new_aff[cid]) - set(old_aff.get(cid, ())))
             for cid in new_aff if set(new_aff[cid]) - set(old_aff.get(cid, ()))}
    assert gains == S2_AFFECTED_GAINS.get(name, {})


# S2 の言語別 fixture（fixtures/corpus/ana-s2）での旧辺 → 新しい始点の対応表。
# キー: 旧辺 (始点, 型, 終点, via, 行)・値: 新しい始点（定義ノード）。表に無い旧辺は始点が変わらない（ファイルの主体）。
S2_START_MOVES = {
    ("c3/foo.c#foo.c", "INVOKES", "c3/bar.c#bar.c.bar", "call", 4): "c3/foo.c#foo.c.foo",
    ("c3/main.c#main.c", "INVOKES", "c3/foo.c#foo.c.foo", "call", 4): "c3/main.c#main.c.main",
    ("csame/pair.c#pair.c", "INVOKES", "csame/pair.c#pair.c.ping", "call", 3): "csame/pair.c#pair.c.pong",
    ("csame/pair.c#pair.c", "INVOKES", "csame/pair.c#pair.c.pong", "call", 9): "csame/pair.c#pair.c.ping",
    ("cs/Two.cs#Alpha", "INVOKES", "cs/Gamma.cs#Gamma", "call", 10): "cs/Two.cs#Acme.App.Beta",
    ("java/app/Main.java#Main", "INVOKES", "java/app/Other.java#Other", "field_type", 10):
        "java/app/Main.java#Side",
    ("vb/Legacy.bas#LEGACY", "INVOKES", "vb/Legacy.bas#LEGACY.FINISH", "call", 3): "vb/Legacy.bas#LEGACY.START",
    ("vb/Legacy.bas#LEGACY", "INVOKES", "vb/Price.vb#PRICE", "field_type", 6): "vb/Legacy.bas#LEGACY.FINISH",
    ("vb/Shop.vb#CART", "INVOKES", "vb/Price.vb#PRICE", "field_type", 9): "vb/Shop.vb#ACME.SHOP.CART.RECALC",
    ("vb/Shop.vb#CART", "INVOKES", "vb/Shop.vb#ACME.SHOP.CART.RECALC", "call", 6): "vb/Shop.vb#ACME.SHOP.CART.ADD",
}
# 新しく見えるようになった定義間の辺（旧では同じ `(始点ファイル, 型, 終点)` の 1 本に畳まれて見えなかった）。
S2_NEW_EDGES = {
    ("vb/Shop.vb#ACME.SHOP.HELPER.ASSIST", "INVOKES", "vb/Price.vb#PRICE", "field_type", 15),
}


def test_s2_fixture_start_node_moves_match_the_fixed_table():
    nodes, edges, _flags = _build(S2_WORLD)
    old = {tuple(e) for e in BASELINE[S2_WORLD]["edges"]}
    expected = set()
    for src, etype, dst, via, line in old:
        s, d = _short(src), _short(dst)
        moved = S2_START_MOVES.get((s, etype, d, via, line))
        expected.add((moved or s, etype, d, via, line))
    expected |= S2_NEW_EDGES | S3_NEW_EDGES
    actual = {(_short(e["src"]), e["type"], _short(e["dst"]), e.get("via"), e.get("line")) for e in edges}
    assert actual == expected


def _reaches_backwards(edges, start: str, goal: str) -> bool:
    """INVOKES を終点から始点へさかのぼって `start` から `goal` へ着くか。"""
    callers = defaultdict(set)
    for e in edges:
        if e["type"] == "INVOKES":
            callers[_short(e["dst"])].add(_short(e["src"]))
    seen, queue = {start}, deque([start])
    while queue:
        cur = queue.popleft()
        if cur == goal:
            return True
        for nxt in callers[cur] - seen:
            seen.add(nxt)
            queue.append(nxt)
    return False


def test_function_chain_is_traceable_from_bar_back_to_main():
    """`main()→foo()→bar()`: `foo()→bar()` の辺があり、`bar()` から `main()` までさかのぼれる。"""
    _nodes, edges, _flags = _build(S2_WORLD)
    pairs = {(_short(e["src"]), _short(e["dst"])) for e in edges if e["type"] == "INVOKES"}
    assert ("c3/foo.c#foo.c.foo", "c3/bar.c#bar.c.bar") in pairs
    assert _reaches_backwards(edges, "c3/bar.c#bar.c.bar", "c3/main.c#main.c.main")


def test_sibling_definitions_do_not_collapse_into_the_representative_definition():
    """同一ファイルの別の型・手続きが、互いの（主体の）代表定義に化けない。"""
    _nodes, edges, _flags = _build(S2_WORLD)
    pairs = {(_short(e["src"]), _short(e["dst"])) for e in edges if e["type"] == "INVOKES"}
    assert ("java/app/Main.java#Side", "java/app/Other.java#Other") in pairs
    assert ("java/app/Main.java#Main", "java/app/Other.java#Other") not in pairs
    assert ("cs/Two.cs#Acme.App.Beta", "cs/Gamma.cs#Gamma") in pairs
    assert ("cs/Two.cs#Alpha", "cs/Gamma.cs#Gamma") not in pairs
    assert ("vb/Shop.vb#ACME.SHOP.HELPER.ASSIST", "vb/Price.vb#PRICE") in pairs
    assert ("csame/pair.c#pair.c.ping", "csame/pair.c#pair.c.pong") in pairs
    assert ("csame/pair.c#pair.c.pong", "csame/pair.c#pair.c.ping") in pairs


def test_no_source_symbol_contract_flags_on_the_fixture_worlds(built):
    _name, _nodes, _edges, flags = built
    assert not [f for f in flags if f["reason"] in ("file_context_missing", "unknown_source_symbol")]


def test_missing_file_context_from_a_requiring_analyzer_is_reported(monkeypatch):
    """`requires_file_context` のアナライザが `file_context` を返さなければ、Pass 2 が読んで `file_context_missing` を申告する（届いていることの確認の裏）。"""
    java_dir = S2_DIR / "java"
    _n, _e, flags = world_graph.build_world(java_dir, "ctx")
    assert not [f for f in flags if f["reason"] == "file_context_missing"]

    original = JavaAnalyzer.extract_refs

    def without_context(self, text, rel_path):
        res = original(self, text, rel_path)
        return RefResult(refs=res.refs, dropped=res.dropped)

    monkeypatch.setattr(JavaAnalyzer, "extract_refs", without_context)
    _n, _e, flags = world_graph.build_world(java_dir, "ctx")
    missing = sorted(f["from"] for f in flags if f["reason"] == "file_context_missing")
    assert missing == ["app/Main.java", "app/Other.java", "lib/Target.java"]


def test_unknown_source_symbol_falls_back_to_the_file_primary_and_is_reported(monkeypatch):
    """存在しない定義キーを指した参照は、始点をファイルの主体へ倒し、`unknown_source_symbol` を申告する（辺は落とさない）。"""
    java_dir = S2_DIR / "java"
    original = JavaAnalyzer.extract_refs

    def bogus(self, text, rel_path):
        res = original(self, text, rel_path)
        for ref in res.refs:
            ref.source_symbol_id = (rel_path, "NoSuchType")
        return res

    monkeypatch.setattr(JavaAnalyzer, "extract_refs", bogus)
    nodes, edges, flags = world_graph.build_world(java_dir, "bogus")
    unknown = sorted((f["from"], f["key"], f["line"]) for f in flags if f["reason"] == "unknown_source_symbol")
    assert unknown == [("app/Main.java", "NoSuchType", 6), ("app/Main.java", "NoSuchType", 6),
                       ("app/Main.java", "NoSuchType", 10)]
    invokes = sorted((_short(e["src"]), _short(e["dst"]), e.get("via"), e.get("line"))
                     for e in edges if e["type"] == "INVOKES")
    # `lib/` は別の最上位フォルダ（世代）なので Target へは張らない。Other だけが、主体 Main を始点に張られる。
    assert invokes == [("app/Main.java#Main", "app/Other.java#Other", "field_type", 10)]


# vb1（namespace の中の VB.NET と VB6）の旧辺 → 新しい始点の対応表。辺の比較は始点 cid・終点 cid・via・行まで厳密に行う
# （手続きの取り違えを、ファイル単位の集合比較で見逃さない）。表に無い辺は始点が変わらない。
S2_VB1_START_MOVES = {
    ("net/Api/ApiClient.vb#APICLIENT", "INVOKES", "net/Api/Api.cs#API", "call", 6): "net/Api/ApiClient.vb#ACME.API.APICLIENT.CONNECT",
    ("net/Api/ApiClient.vb#APICLIENT", "INVOKES", "net/Order/OrderService.vb#ORDERSERVICE", "call", 10):
        "net/Api/ApiClient.vb#ACME.API.APICLIENT.LOAD",
    ("net/Order/OrderService.vb#ORDERSERVICE", "INVOKES", "net/Order/OrderService.vb#ACME.ORDER.ORDERSERVICE.LOGSTART", "call", 13):
        "net/Order/OrderService.vb#ACME.ORDER.ORDERSERVICE.PROCESS",
    ("vb6/frmMain.frm#FRMMAIN", "INVOKES", "vb6/modData.bas#MODDATA.LOADORDERS", "call", 12): "vb6/frmMain.frm#FRMMAIN.CMDOK_CLICK",
    ("vb6/modData.bas#MODDATA", "ACCESSES", "vb6/schema.sql#ORDERS", "vba_sql", 5): "vb6/modData.bas#MODDATA.LOADORDERS",
    ("vb6/modMain.bas#MODMAIN", "INVOKES", "vb6/modData.bas#MODDATA.LOADORDERS", "call", 5): "vb6/modMain.bas#MODMAIN.MAIN",
    ("vb6/modMain.bas#MODMAIN", "INVOKES", "vb6/modMain.bas#MODMAIN.LOGMESSAGE", "call", 4): "vb6/modMain.bas#MODMAIN.MAIN",
}


def test_vb1_edges_match_the_fixed_start_table_exactly():
    _nodes, edges, _flags = _build("vb1")

    def short(c):
        return c.split(":", 2)[2]
    expected = set()
    for src, etype, dst, via, line in BASELINE["vb1"]["edges"]:
        key = (short(src), etype, short(dst), via, line)
        expected.add((S2_VB1_START_MOVES.get(key, key[0]), etype, short(dst), via, line))
    actual = {(short(e["src"]), e["type"], short(e["dst"]), e.get("via"), e.get("line")) for e in edges}
    assert actual == expected
