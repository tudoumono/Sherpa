"""`world_graph.build_world()` 経由の Java 統合テスト（CODE-1d＝新言語1つでの手順検証・
docs/proposals/2026-08-29-コード解析層のコンポーネント化.md §4.2 (b)(c)）。

`fixtures/corpus/java1`（2パッケージ・相互参照あり・新規作成）を実際に `build_world()` へ通し、
共通層（`sherpa/ingest/world_graph.py`・無改修）の同一 top_scope 内最近傍解決・`ambiguous_reference`
flag・語彙バリデーションが Java でもそのまま働くことを固定する。
"""
from __future__ import annotations

import pathlib

from sherpa.ingest import world_graph

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "java1"
WORLD_ID = "java1_test"


def _build():
    return world_graph.build_world(WORLD_DIR, WORLD_ID)


def _node_keys(nodes):
    """(label, name, path) の集合——同名ノード（Helper が2パッケージに存在）を path で区別する。"""
    return {(n["label"], n["name"], n["path"]) for n in nodes}


def _edge_keys(nodes, edges):
    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}
    return {(e["type"], by_cid[e["src"]], by_cid[e["dst"]]) for e in edges}


def test_public_types_become_primary_module_nodes():
    nodes, _edges, _flags = _build()
    keys = _node_keys(nodes)
    assert ("Module", "TaxCalculator", "com/acme/tax/TaxCalculator.java") in keys
    assert ("Module", "Taxable", "com/acme/tax/Taxable.java") in keys
    assert ("Module", "AbstractCalculator", "com/acme/tax/AbstractCalculator.java") in keys
    assert ("Module", "InvoiceService", "com/acme/billing/InvoiceService.java") in keys
    assert ("Module", "Main", "com/acme/Main.java") in keys


def test_non_public_sibling_becomes_child_module_with_contains_edge():
    nodes, edges, _flags = _build()
    assert ("Module", "RoundingHelper", "com/acme/tax/TaxCalculator.java") in _node_keys(nodes)
    assert ("CONTAINS",
           ("Module", "TaxCalculator", "com/acme/tax/TaxCalculator.java"),
           ("Module", "RoundingHelper", "com/acme/tax/TaxCalculator.java")) in _edge_keys(nodes, edges)


def test_extends_and_implements_resolve_to_invokes_edges():
    nodes, edges, _flags = _build()
    ek = _edge_keys(nodes, edges)
    src = ("Module", "TaxCalculator", "com/acme/tax/TaxCalculator.java")
    assert ("INVOKES", src, ("Module", "AbstractCalculator", "com/acme/tax/AbstractCalculator.java")) in ek
    assert ("INVOKES", src, ("Module", "Taxable", "com/acme/tax/Taxable.java")) in ek


def test_cross_package_new_and_static_call_resolve_via_nearest_neighbor():
    """別パッケージ（com.acme.billing）から `new TaxCalculator()`／`TaxCalculator.staticRate()` で
    参照した `com.acme.tax.TaxCalculator` が、同一 top_scope（`com`）内で一意に解決される
    （共通層 `world_graph._resolve_nearest()`・無改修）。"""
    nodes, edges, _flags = _build()
    ek = _edge_keys(nodes, edges)
    src = ("Module", "InvoiceService", "com/acme/billing/InvoiceService.java")
    dst = ("Module", "TaxCalculator", "com/acme/tax/TaxCalculator.java")
    assert ("INVOKES", src, dst) in ek


def test_same_named_helper_in_two_packages_is_flagged_ambiguous_not_arbitrarily_resolved():
    """`com/acme/Main.java` は `com/acme/tax/util/Helper.java` と `com/acme/billing/util/Helper.java`
    の両方から等距離——共通層は任意解決せず `ambiguous_reference`(`ambiguous`) flag を立てる
    （docs/03-鏡モデル.md §2.2・§4.2 (c) の否定的テスト）。"""
    nodes, edges, flags = _build()
    ek = _edge_keys(nodes, edges)
    src = ("Module", "Main", "com/acme/Main.java")
    tax_helper = ("Module", "Helper", "com/acme/tax/util/Helper.java")
    billing_helper = ("Module", "Helper", "com/acme/billing/util/Helper.java")
    assert ("INVOKES", src, tax_helper) not in ek           # 任意解決していない
    assert ("INVOKES", src, billing_helper) not in ek
    assert {"reason": "ambiguous", "from": "com/acme/Main.java", "kind": "Module", "name": "Helper"} in flags


def test_reference_to_non_public_sibling_type_now_resolves_via_children_index():
    """CODE-2 是正（JAVA-1 残課題#3）: `children` も共通層の `defs` 解決索引へ登録するようになり、
    同一ファイル内からの非 public 兄弟型への参照（`new RoundingHelper()`・フィールド宣言型）が
    誤検出なく解決される——`unresolved` ではなく `INVOKES` エッジが張られる。"""
    nodes, edges, flags = _build()
    ek = _edge_keys(nodes, edges)
    src = ("Module", "TaxCalculator", "com/acme/tax/TaxCalculator.java")
    dst = ("Module", "RoundingHelper", "com/acme/tax/TaxCalculator.java")
    assert ("INVOKES", src, dst) in ek
    assert {"reason": "unresolved", "from": "com/acme/tax/TaxCalculator.java",
            "kind": "Module", "name": "RoundingHelper"} not in flags


def test_static_call_heuristic_flags_unresolved_jdk_reference_rather_than_guessing():
    """`Math.round(...)`（JDK 標準ライブラリ）も静的呼び出しヒューリスティックに拾われるが、
    コーパスに `Math` の定義は無いため `unresolved` になる（誤って解決しない）。"""
    _nodes, _edges, flags = _build()
    assert {"reason": "unresolved", "from": "com/acme/tax/TaxCalculator.java",
            "kind": "Module", "name": "Math"} in flags


def test_analyzer_provenance_is_recorded_on_java_nodes():
    """来歴（誰が解析したか）が来歴として必ず記録される（§7 裁定2）。"""
    nodes, _edges, _flags = _build()
    tax_calc = next(n for n in nodes if n["path"] == "com/acme/tax/TaxCalculator.java"
                    and n["name"] == "TaxCalculator")
    assert tax_calc["analyzer"] == "java"


def test_no_unknown_label_or_edge_type_flags():
    """docs/05 のクローズド語彙のみを使う——Java アナライザが語彙外のラベル/エッジ型を出していない。"""
    _nodes, _edges, flags = _build()
    reasons = {f["reason"] for f in flags}
    assert "unknown_label" not in reasons and "unknown_edge_type" not in reasons


def test_no_unknown_via_flags():
    """CODE-2: Java アナライザが積む `via` はすべて `KNOWN_VIA` の既知値のみ（`unknown_via` 無し）。"""
    _nodes, _edges, flags = _build()
    assert "unknown_via" not in {f["reason"] for f in flags}


# --- JAVA-2（宣言型参照の一般抽出・`fixtures/corpus/java1/declrefs`）---

def test_field_and_parameter_declared_types_resolve_with_via_field_type():
    nodes, edges, _flags = _build()
    ek = _edge_keys(nodes, edges)
    worker = ("Module", "Worker", "declrefs/Worker.java")
    engine = ("Module", "Engine", "declrefs/Engine.java")
    tax_calc = ("Module", "TaxCalc", "declrefs/TaxCalc.java")
    assert ("INVOKES", worker, engine) in ek       # フィールド宣言型／コンストラクタ引数
    assert ("INVOKES", worker, tax_calc) in ek     # メソッド引数／ジェネリクス型引数(1段)
    via = {(e["src"], e["dst"], e.get("via")) for e in edges if e["type"] == "INVOKES"}
    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}
    worker_cid = next(cid for cid, k in by_cid.items() if k == worker)
    engine_cid = next(cid for cid, k in by_cid.items() if k == engine)
    assert (worker_cid, engine_cid, "field_type") in via


def test_jdk_common_types_do_not_appear_as_unresolved_or_resolved_references():
    """`String`/`List` は JDK 頻出型としてそもそも候補にしない——ノイズ（unresolved flag）も
    誤ったエッジも生まない。"""
    nodes, edges, flags = _build()
    worker_flags = {f.get("name") for f in flags if f.get("from") == "declrefs/Worker.java"}
    assert "String" not in worker_flags and "List" not in worker_flags
    by_cid = {n["cid"]: n["name"] for n in nodes}
    worker_edge_targets = {by_cid.get(e["dst"]) for e in edges if e.get("doc") == "declrefs/Worker.java"}
    assert "String" not in worker_edge_targets and "List" not in worker_edge_targets


def test_framework_independent_di_fixtures_produce_the_same_invokes_edge_set_with_different_via():
    """受け入れ条件: Spring 風（`@Autowired` 付き）とプレーン Java（アノテーション無し・同じ
    型宣言）の両フィクスチャで**同一の INVOKES エッジ集合**（via だけ inject/field_type と
    異なる）が立つ——フレームワーク非依存の実証（裁定2026-09-03）。"""
    nodes, edges, flags = _build()
    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}

    def _via_edges(top_scope):
        out = set()
        for e in edges:
            src, dst = by_cid.get(e["src"]), by_cid.get(e["dst"])
            if src and src[2].startswith(top_scope + "/") and e["type"] == "INVOKES":
                out.add((src[0], src[1], dst[0], dst[1], e.get("via")))
        return out

    spring = _via_edges("spring_di")
    plain = _via_edges("plain_di")
    assert spring == {("Module", "Service", "Module", "Engine", "inject")}
    assert plain == {("Module", "Service", "Module", "Engine", "field_type")}
    # via を無視すれば同一のエッジ集合（型宣言の依存事実は DI アノテーションの有無に依らない）。
    assert {t[:4] for t in spring} == {t[:4] for t in plain}
    assert not any(f.get("from", "").startswith(("spring_di/", "plain_di/")) for f in flags)
