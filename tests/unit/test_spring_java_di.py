"""FW プラグイン `spring:java` の DI の拾い方（ANA-15 P2・提案書 2026-10-04 段階 2 の「Spring の DI で拾う形」）。

`fixtures/corpus/ana-p2`（架空の名前）を `build_world` に通し、注入の辺が宣言した型のまま残ること、実装への辺が決まる根拠
（名前の指定・`@Primary`・唯一の実装）があるときだけ張られること、決まらないときは辺を張らず候補つきで申告されること、
注釈の無いコンストラクタの注入が対象になる条件を固定する。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sherpa.ingest import world_graph

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def world():
    return world_graph.build_world(ROOT / "fixtures" / "corpus" / "ana-p2", "ana_p2")


def _inject(edges, src):
    """`src`（クラス名）から出る `via=inject` の辺 → `{終点のクラス名: 根拠の規則の集合}`。"""
    out: dict = {}
    for e in edges:
        if e["type"] == "INVOKES" and e.get("via") == "inject" and e["src"].endswith(f"#{src}"):
            out[e["dst"].rsplit("#", 1)[-1]] = {s.get("rule") for s in e["sources"] if s.get("via") == "inject"}
    return out


def test_injection_keeps_declared_types_and_links_implementations_only_with_a_basis(world):
    _nodes, edges, _flags = world
    got = _inject(edges, "ShipmentService")
    # 宣言した型（インターフェース）への辺は注入の種類（フィールド・セッター・コンストラクタ）を問わず残る
    for declared in ("Notifier", "Printer", "Archiver", "Storage", "Sink"):
        assert declared in got
    # 実装への辺は、決まる根拠があるものだけ（根拠の名前は辺の根拠の rule に残る）
    assert got["LaserPrinter"] == {"di_qualifier"}       # コンストラクタ引数の @Qualifier("laser")＝明示名
    assert got["DotPrinter"] == {"di_qualifier"}         # @Resource(name = "dotPrinter")＝クラス名の先頭小文字
    assert got["AuditSink"] == {"di_qualifier"}          # @Inject @Named("auditSink")
    assert got["ZipArchiver"] == {"di_primary"}
    assert got["MailNotifier"] == {"di_single_impl"}     # 注釈の無い単一コンストラクタ（コンポーネント注釈あり）
    # 決まらない（実装が 2 つで根拠なし）・名前の指定の無い他の実装には辺を張らない
    assert not {"DiskStorage", "CloudStorage", "TarArchiver", "NullSink"} & set(got)


def test_undecidable_implementation_is_reported_with_candidates_and_no_edge(world):
    nodes, _edges, flags = world
    amb = [f for f in flags if f["reason"] == "ambiguous" and f.get("via") == "inject" and f["from"].endswith("ShipmentService.java")]
    assert [(f["from"], f["name"], f["candidates"], f["why"]) for f in amb] == [
        ("shop/di/ShipmentService.java", "Storage", 2, "no_evidence")]        # セッター注入・実装が 2 つ
    svc = next(n for n in nodes if n["path"] == "shop/di/ShipmentService.java" and n["label"] == "Module")
    item = next(u for u in svc["unresolved"] if u["via"] == "inject")
    assert item["candidate_paths"] == ["shop/di/CloudStorage.java", "shop/di/DiskStorage.java"]


def test_unannotated_constructor_injection_applies_only_to_a_single_constructor_of_a_component(world):
    _nodes, edges, _flags = world
    assert _inject(edges, "TwoConstructors") == {} and _inject(edges, "PlainHolder") == {}   # 複数・コンポーネントでない
    via = {e["dst"].rsplit("#", 1)[-1]: e.get("via") for e in edges
           if e["type"] == "INVOKES" and e["src"].endswith(("#TwoConstructors", "#PlainHolder"))}
    assert via == {"Notifier": "field_type"}                                               # 宣言型の参照は本体のまま


def test_unknown_resolution_rule_from_a_plugin_is_flagged_and_the_edge_keeps_the_common_rule(tmp_path, monkeypatch):
    from sherpa.ingest.analyzers import registry
    from sherpa.ingest.analyzers._base import FwPlugin, PluginRefs, RefCandidate

    class _P(FwPlugin):
        name, languages, version, order = "p:rule", frozenset({"java"}), 1, 10

        def extract_refs(self, text, rel_path, base_defs, base_refs, types):
            if not rel_path.endswith("A.java"):
                return PluginRefs()
            return PluginRefs(refs=[RefCandidate("INVOKES", "Module", "app.B", 2, {"via": "inject", "qualified": True,
                                                                                  "resolution_rule": "bogus"})])

    monkeypatch.setattr(registry, "FW_PLUGINS", (_P(),))
    d = tmp_path / "app"
    d.mkdir()
    (d / "A.java").write_text("package app;\npublic class A {}\n", encoding="utf-8")
    (d / "B.java").write_text("package app;\npublic class B {}\n", encoding="utf-8")
    _n, edges, flags = world_graph.build_world(tmp_path, "w", files=[(d / "A.java", "app/A.java"), (d / "B.java", "app/B.java")])
    edge = next(e for e in edges if e["type"] == "INVOKES")
    assert [s["rule"] for s in edge["sources"]] == ["qualified_name"] and "resolution_rule" not in edge
    assert [f for f in flags if f["reason"] == "unknown_rule"] == [
        {"reason": "unknown_rule", "analyzer": "java", "from": "app/A.java", "rule": "bogus"}]


def _java_files(root):
    return sorted(p for p in root.rglob("*.java"))


def _build_dir(root, wid):
    files = [(p, p.relative_to(root).as_posix()) for p in _java_files(root)]
    return world_graph.build_world(root, wid, files=files)


def test_bean_facts_do_not_leak_between_interleaved_builds_of_different_folders(tmp_path, monkeypatch):
    import shutil
    from sherpa.ingest.analyzers import spring_java
    src = ROOT / "fixtures" / "corpus" / "ana-p2"
    a, b = tmp_path / "a", tmp_path / "b"
    shutil.copytree(src, a)
    shutil.copytree(src, b)
    zip_b = b / "shop/di/ZipArchiver.java"
    zip_b.write_text(zip_b.read_text(encoding="utf-8").replace("@Primary\n", ""), encoding="utf-8")   # B には @Primary が無い
    n_a, state = len(_java_files(a)), {"calls": 0, "nested": None}
    orig = spring_java.SpringJavaPlugin.collect_defs

    def spy(self, text, rel_path, base, ctx=None):
        out = orig(self, text, rel_path, base, ctx)
        state["calls"] += 1
        if state["calls"] == n_a:               # A の 1 パス目の最後で、別の資料フォルダ B を丸ごと取り込む
            state["nested"] = _build_dir(b, "b")
        return out

    monkeypatch.setattr(spring_java.SpringJavaPlugin, "collect_defs", spy)
    _n, edges_a, _f = _build_dir(a, "a")
    assert _inject(edges_a, "ShipmentService")["ZipArchiver"] == {"di_primary"}          # A は A の @Primary で決まる
    _n, edges_b, flags_b = state["nested"]
    assert "ZipArchiver" not in _inject(edges_b, "ShipmentService")                       # B には根拠が無い
    assert any(f["reason"] == "ambiguous" and f["name"] == "Archiver" for f in flags_b)


def _ambiguity(tmp_path, ctl_body):
    d = tmp_path / "app"
    d.mkdir()
    (d / "Svc.java").write_text("package app;\npublic interface Svc {}\n", encoding="utf-8")
    for n in ("A", "B"):
        (d / f"{n}.java").write_text(f"package app;\n@org.springframework.stereotype.Component\npublic class {n} implements Svc {{}}\n", encoding="utf-8")
    (d / "Ctl.java").write_text("package app;\npublic class Ctl {\n" + ctl_body + "\n}\n", encoding="utf-8")
    nodes, edges, flags = _build_dir(d, "w")
    return edges, [f for f in flags if f["reason"] == "ambiguous" and f.get("via") == "inject"]


def test_conflicting_name_hints_are_ambiguous_and_link_no_implementation(tmp_path):
    edges, amb = _ambiguity(tmp_path, '  @Autowired @Qualifier("a") @Named("b")\n  Svc svc;')
    assert [f["why"] for f in amb] == ["qualifier_conflict"]
    assert _inject(edges, "Ctl").keys() == {"Svc"}


def test_implementation_search_limit_is_reported_and_links_no_implementation(monkeypatch):
    from sherpa.ingest.analyzers import spring_java
    monkeypatch.setattr(spring_java, "_CLOSURE_MAX", 1)
    _nodes, edges, flags = world_graph.build_world(ROOT / "fixtures" / "corpus" / "ana-p2", "ana_p2")
    assert {"LaserPrinter", "DotPrinter"}.isdisjoint(_inject(edges, "ShipmentService"))
    assert any(f["reason"] == "ambiguous" and f["name"] == "Printer" and f["why"] == "search_limit" for f in flags)


def test_configuration_properties_prefix_that_cannot_be_a_key_is_reported():
    from sherpa.ingest.analyzers.java import JavaAnalyzer
    from sherpa.ingest.analyzers._base import TypeLookup, TypeRelations
    from sherpa.ingest.analyzers.spring_java import SpringJavaPlugin

    class _T(TypeRelations):
        def subtypes(self, *a, **k):
            return TypeLookup(status="unresolved")

    text = '@ConfigurationProperties(prefix = PREFIX)\npublic class P {}\n'
    a = JavaAnalyzer()
    out = SpringJavaPlugin().extract_refs(text, "P.java", a.collect_defs(text, "P.java"), a.extract_refs(text, "P.java"), _T())
    assert [(d.reason, d.snippet) for d in out.dropped] == [("config_prefix", "prefix = PREFIX")]


def test_only_component_annotated_classes_are_implementation_candidates(world):
    _nodes, edges, _flags = world
    got = _inject(edges, "WidgetUser")
    assert got["SpringMeter"] == {"di_single_impl"} and "PlainMeter" not in got      # 注釈の無い実装は候補にしない


def test_generic_type_arguments_select_the_matching_implementation(world):
    _nodes, edges, flags = world
    got = _inject(edges, "WidgetUser")
    assert got["UserRepo"] == {"di_single_impl"} and "OrderRepo" not in got            # Repo<User> は Repo<Order> の実装へ張らない
    amb = [f for f in flags if f["reason"] == "ambiguous" and f["from"] == "shop/di/WidgetUser.java"]
    assert [(f["name"], f["line"], f["why"]) for f in amb] == [("Repo", 16, "generic_unmatched")]      # Repo<?> は照合不能


def _small_world(tmp_path, files):
    d = tmp_path / "app"
    d.mkdir(parents=True)
    for n, body in files.items():
        (d / f"{n}.java").write_text("package app;\n" + body, encoding="utf-8")
    _n, edges, flags = _build_dir(d, "w")
    return edges, [f for f in flags if f["reason"] == "ambiguous" and f.get("via") == "inject"]


def test_non_literal_resource_name_and_non_literal_bean_name_do_not_decide(tmp_path):
    comp = "@org.springframework.stereotype.Component"
    edges, amb = _small_world(tmp_path, {
        "Svc": "public interface Svc {}\n", "A": f"{comp}\npublic class A implements Svc {{}}\n",
        "Ctl": "public class Ctl {\n  @Resource(name = NAMES.X)\n  Svc svc;\n}\n"})
    assert [f["why"] for f in amb] == ["qualifier_not_literal"] and _inject(edges, "Ctl").keys() == {"Svc"}
    edges, amb = _small_world(tmp_path / "x", {
        "Svc": "public interface Svc {}\n", "A": f"{comp}(NAME)\npublic class A implements Svc {{}}\n",
        "Ctl": 'public class Ctl {\n  @Autowired @Qualifier("a")\n  Svc svc;\n}\n'})
    assert [f["why"] for f in amb] == ["qualifier_unmatched"] and _inject(edges, "Ctl").keys() == {"Svc"}   # 既定の名前 a を足さない


def test_plugin_context_written_by_a_failing_call_does_not_reach_later_files():
    from sherpa.ingest.analyzers import registry
    from sherpa.ingest.analyzers._base import DefResult, FwPlugin, PluginDefs

    class _P(FwPlugin):
        name, languages, version, order, uses_build_context = "p:ctx", frozenset({"java"}), 1, 10, True

        def collect_defs(self, text, rel_path, base, ctx=None):
            ctx[rel_path] = 1
            if rel_path == "bad.java":
                raise RuntimeError("boom")
            return PluginDefs()

    build: dict = {}
    for rel in ("a.java", "bad.java", "c.java"):
        registry.apply_fw_defs([_P()], "", rel, DefResult(), build)
    assert build["p:ctx"] == {"a.java": 1, "c.java": 1}


def test_generic_arguments_are_carried_through_intermediate_abstract_types(world):
    _nodes, edges, _flags = world
    got = _inject(edges, "ChainUser")
    assert got["ItemRepo"] == {"di_single_impl"} and {"GadgetRepo", "UserRepo", "OrderRepo"}.isdisjoint(got)   # Repo<Item> ← BaseRepo<T> ← ItemRepo


def test_varargs_setter_parameter_is_an_injection_point(world):
    _nodes, edges, _flags = world
    assert _inject(edges, "ChainUser")["SpringMeter"] == {"di_single_impl"}


def test_same_simple_name_parents_from_different_packages_are_told_apart(tmp_path):
    comp = "@org.springframework.stereotype.Component"
    d = tmp_path / "w"
    for rel, body in {
        "a/Repo.java": "package a;\npublic interface Repo<T> {}\n",
        "b/Repo.java": "package b;\npublic interface Repo<T> {}\n",
        "m/X.java": "package m;\npublic class X {}\n", "m/Y.java": "package m;\npublic class Y {}\n",
        "m/C.java": f"package m;\n{comp}\npublic class C implements b.Repo<Y>, a.Repo<X> {{}}\n",
        "m/Ctl.java": "package m;\npublic class Ctl {\n  @Autowired\n  a.Repo<X> r;\n}\n",
    }.items():
        (d / "g" / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / "g" / rel).write_text(body, encoding="utf-8")
    _n, edges, flags = _build_dir(d, "w")
    assert "C" in _inject(edges, "Ctl") and not [f for f in flags if f["reason"] == "ambiguous"]
