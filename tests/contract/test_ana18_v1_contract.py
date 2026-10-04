"""ANA-18 V1（VB.NET のプロジェクト設定 `.vbproj` の RootNamespace・Import を名前の照合に使う）の受入契約。

`fixtures/corpus/ana-v1` の各ケース（最上位フォルダごと）を `build_world` に通し、辺・`flags`・ノードの全件を固定する。
ノードの識別子（cid）は Root Namespace を含まない。`.vbproj` が無いケースは今までと同じ結果になる。
"""
from __future__ import annotations

import pathlib
import shutil

import pytest

from sherpa.ingest import worker, world_graph

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "ana-v1"
TOPS = ("vbroot", "vbfar", "vbimp", "vbnone", "vblegacy", "vbtwo", "vbbad", "vbname",
        "vbclash", "vbdef", "vbshared", "vbprops", "vbutf16", "vbstop", "vbdup", "vbcond", "VbCase", ".")


def _short(cid: str) -> str:
    return cid.split(":", 2)[2]


def _case(world_dir: pathlib.Path):
    nodes, edges, flags = world_graph.build_world(world_dir, "ana-v1")
    out = {}
    for top in TOPS:
        def mine(path, top=top):            # "." は資料フォルダ直下のファイル
            return "/" not in path.split("#")[0] if top == "." else path.startswith(f"{top}/")
        out[top] = (
            {_short(n["cid"]) for n in nodes if mine(_short(n["cid"]))},
            {(e["type"], _short(e["src"]), _short(e["dst"]), e.get("via"), e.get("line"))
             for e in edges if mine(_short(e["src"]))},
            {(f["reason"], f.get("from") or f.get("doc"), f.get("name") or f.get("why"), f.get("line"), f.get("snippet"))
             for f in flags if mine(f.get("from") or f.get("doc") or "")},
        )
    return out


@pytest.fixture(scope="module")
def built():
    return _case(WORLD_DIR)


def _field(src, dst, line=2):
    return ("INVOKES", src, dst, "field_type", line)


def test_root_namespace_qualified_reference_links_and_node_ids_do_not_change(built):
    """SDK 形式: `Acme.Shop.Basket`（Root Namespace つき）でも `Shop.Basket`（無し）でも張れる。cid に Root Namespace は入らない。"""
    nodes, edges, flags = built["vbroot"]
    assert nodes == {"vbroot/Shop/Basket.vb#BASKET", "vbroot/UseRooted.vb#USEROOTED", "vbroot/UseShort.vb#USESHORT"}
    assert edges == {_field("vbroot/UseRooted.vb#USEROOTED", "vbroot/Shop/Basket.vb#BASKET"),
                     _field("vbroot/UseShort.vb#USESHORT", "vbroot/Shop/Basket.vb#BASKET")}
    assert flags == set()


def test_another_projects_type_is_reachable_only_by_its_real_name(built):
    """別のプロジェクトの型は本当の名前（Root Namespace つき）でだけ引ける。自分の Root Namespace を相手の namespace に足した名前（`Other.Shop.Basket`）は引けない。"""
    nodes, edges, flags = built["vbfar"]
    assert edges == {_field("vbfar/A/UseOwn.vb#USEOWN", "vbfar/A/Shop/Basket.vb#BASKET"),
                     _field("vbfar/B/UseForeign.vb#USEFOREIGN", "vbfar/A/Shop/Basket.vb#BASKET")}
    assert flags == {("unresolved_qualifier", "vbfar/B/UseForeign.vb", "OTHER.SHOP.BASKET", 3, None)}


def test_explicit_namespace_never_collides_with_another_projects_root_namespace(built):
    """A（Root=Acme・`Namespace Shop`）と B（Root=Beta・明示の `Namespace Acme.Shop`）: A の `Acme.Shop.Basket` は B へ化けない。B の本当の名前は `Beta.Acme.Shop.Basket`。"""
    nodes, edges, flags = built["vbclash"]
    assert edges == {_field("vbclash/A/Use.vb#USEA", "vbclash/A/Shop/Basket.vb#BASKET"),
                     _field("vbclash/B/Use.vb#USEB", "vbclash/B/Acme/Basket.vb#BASKET"),
                     _field("vbclash/C/Use.vb#USEC", "vbclash/A/Shop/Basket.vb#BASKET")}
    assert flags == set()


def test_project_import_resolves_and_no_project_stays_as_before(built):
    """プロジェクトの Import（`Lib.Core`）で型名が引ける。同じ 2 ファイルで `.vbproj` が無ければ今までどおり未解決（Root Namespace つきの参照も未解決）。"""
    assert built["vbimp"][1] == {_field("vbimp/UseWidget.vb#USEWIDGET", "vbimp/Lib/Widget.vb#WIDGET")}
    assert built["vbimp"][2] == set()
    nodes, edges, flags = built["vbnone"]
    assert edges == set()
    assert flags == {("unresolved", "vbnone/UseWidget.vb", "WIDGET", 2, None),
                     ("unresolved_qualifier", "vbnone/UseRooted.vb", "ACME.SHOP.BASKET", 2, None)}


def test_legacy_project_applies_only_to_enumerated_sources(built):
    """旧形式（`<Compile Include>` の列挙・`\\` 区切り）: 列挙に含まれるソースだけがプロジェクトに属する。含まれない `Excluded.vb` は属さず、本当の名前（`Acme.Shop.Basket`）でだけ引く。"""
    nodes, edges, flags = built["vblegacy"]
    assert edges == {_field("vblegacy/Included.vb#INCLUDED", "vblegacy/Shop/Basket.vb#BASKET"),
                     _field("vblegacy/Excluded.vb#EXCLUDED", "vblegacy/Shop/Basket.vb#BASKET")}   # 本当の名前で書けば、含まれないソースからも引ける
    assert flags == set()


def test_two_projects_in_one_folder_are_reported_not_applied(built):
    """同じフォルダに当たり得る `.vbproj` が 2 つ: 当てず、各ソースについて候補を `dropped_syntax(vbproj_ambiguous)` で申告する。"""
    nodes, edges, flags = built["vbtwo"]
    both = "vbtwo/One.vbproj, vbtwo/Two.vbproj"
    assert edges == set()
    assert flags == {("dropped_syntax", "vbtwo/Shop/Basket.vb", "vbproj_ambiguous", 1, both),
                     ("dropped_syntax", "vbtwo/Use.vb", "vbproj_ambiguous", 1, both),
                     ("unresolved_qualifier", "vbtwo/Use.vb", "ACME.SHOP.BASKET", 2, None)}


def test_unreadable_vbproj_is_reported_and_ignored(built):
    """DTD を含む `.vbproj` は読まず（外部実体を解決しない）、`dropped_syntax(vbproj_unreadable)` で申告する。Root Namespace は当たらない。"""
    nodes, edges, flags = built["vbbad"]
    assert edges == set()
    assert flags == {("dropped_syntax", "vbbad/Bad.vbproj", "vbproj_unreadable", 1, "vbproj_dtd"),
                     ("unresolved_qualifier", "vbbad/Use.vb", "ACME.SHOP.BASKET", 2, None)}


def test_missing_root_namespace_defaults_to_the_project_name(built):
    """`RootNamespace` が無い `.vbproj`（SDK 形式・旧形式とも）は MSBuild の既定どおりプロジェクト名（ファイル名から拡張子を除いたもの）が Root Namespace。別のプロジェクトの名前（`Billing.…`）は本当の名前でだけ引ける。"""
    nodes, edges, flags = built["vbname"]
    assert nodes == {"vbname/Sdk/Shop/Basket.vb#BASKET", "vbname/Sdk/UseSdk.vb#USESDK",
                     "vbname/Old/Shop/Basket.vb#BASKET", "vbname/Old/Use.vb#USE"}
    assert edges == {_field("vbname/Sdk/UseSdk.vb#USESDK", "vbname/Sdk/Shop/Basket.vb#BASKET"),
                     _field("vbname/Old/Use.vb#USE", "vbname/Old/Shop/Basket.vb#BASKET"),
                     _field("vbname/Old/Use.vb#USE", "vbname/Sdk/Shop/Basket.vb#BASKET", 3)}
    assert flags == set()


def test_sdk_default_excludes_and_outside_includes_and_semicolons(built):
    """SDK 形式の既定の除外（`obj/**`）のソースは属さない（`Shop.Basket` が引けない）。`;` 区切りの Include を分け、フォルダの外（`..\\Shared`）を指すソースも属する。
    最上位フォルダの外へ出る Include は拒否して申告する。"""
    assert built["vbdef"][1] == {_field("vbdef/Use.vb#USEIN", "vbdef/Shop/Basket.vb#BASKET")}
    assert built["vbdef"][2] == {("unresolved_qualifier", "vbdef/obj/Gen.vb", "SHOP.BASKET", 2, None)}
    nodes, edges, flags = built["vbshared"]
    assert edges == {_field("vbshared/App/Use.vb#USE", "vbshared/Shared/Basket.vb#BASKET")}
    assert flags == {("unresolved_qualifier", "vbshared/Shared/Other.vb", "SHOP.BASKET", 2, None),
                     ("dropped_syntax", "vbshared/App/App.vbproj", "vbproj_include_outside", 1, "..\\..\\Outside\\X.vb")}


def test_root_namespace_from_directory_build_props_and_unevaluated_values(built):
    """`.vbproj` に無ければ上の `Directory.Build.props` の `RootNamespace`。`$(…)` を含む値と `Condition` つきの要素は当てはめず申告する（Root は当てない）。空の `RootNamespace` は Root 無し（プロジェクト名にしない）。MSBuild の `<Import Project>`（標準の `Microsoft.*` を除く）は評価せず申告する。"""
    nodes, edges, flags = built["vbprops"]
    assert edges == {_field("vbprops/App/Use.vb#USEA", "vbprops/App/Shop/Basket.vb#BASKET"),
                     _field("vbprops/Dyn/Use.vb#USED", "vbprops/Dyn/Shop/Basket.vb#BASKET"),
                     _field("vbprops/Empty/Use.vb#USEE", "vbprops/Empty/Shop/Basket.vb#BASKET")}
    assert flags == {("dropped_syntax", "vbprops/Empty/Empty.vbproj", "vbproj_unevaluated", 1, "Import Project: Common.props"),
                     ("unresolved_qualifier", "vbprops/Empty/Use.vb", "EMPTY.SHOP.BASKET", 3, None),
                     ("dropped_syntax", "vbprops/Dyn/Dyn.vbproj", "vbproj_unevaluated", 1, "RootNamespace: $(Prefix).Dyn"),
                     ("dropped_syntax", "vbprops/Dyn/Dyn.vbproj", "vbproj_unevaluated", 1, "Import: Lib.Core")}


def test_utf16_vbproj_is_read(built):
    """UTF-16（BOM つき）の `.vbproj` も読む。"""
    assert built["vbutf16"][1] == {_field("vbutf16/Use.vb#USE", "vbutf16/Shop/Basket.vb#BASKET")}
    assert built["vbutf16"][2] == set()


def test_unreadable_inner_vbproj_stops_the_search(built):
    """内側の `.vbproj` が読めなければ、外側のプロジェクト（Root=Acme）へ倒さず、内側のソースはどのプロジェクトにも属さない。"""
    nodes, edges, flags = built["vbstop"]
    assert edges == set()
    assert flags == {("dropped_syntax", "vbstop/Inner/Inner.vbproj", "vbproj_unreadable", 1, "vbproj_xml: no element found: line 2, column 0"),
                     ("unresolved_qualifier", "vbstop/Inner/Use.vb", "SHOP.BASKET", 2, None)}


def test_vbproj_at_the_world_root_applies_to_root_level_sources(built):
    """資料フォルダ直下の `.vbproj` は直下のソースに当たる（最上位フォルダの中のソースには当てない）。"""
    nodes, edges, flags = built["."]
    assert nodes == {"RootBasket.vb#BASKET", "RootUse.vb#ROOTUSE"}
    assert edges == {_field("RootUse.vb#ROOTUSE", "RootBasket.vb#BASKET")}
    assert flags == set()


def test_same_qualified_name_in_two_projects_prefers_the_referencing_projects_own(built):
    """同じ Root Namespace の 2 プロジェクトに同じ完全修飾名がある: 近さではなく同じプロジェクトの定義を先にする（`P1/Use.vb` は遠い自分の `Basket` へ張る）。"""
    nodes, edges, flags = built["vbdup"]
    assert edges == {_field("vbdup/P1/Use.vb#USEP1", "vbdup/P1/a/b/c/Basket.vb#BASKET"),
                     _field("vbdup/P1/Sub/Use.vb#USEP2", "vbdup/P1/Sub/Basket.vb#BASKET")}
    assert flags == set()


def test_unevaluated_membership_stops_the_project_from_applying(built):
    """ソースを含むかの判定に評価できない値（`EnableDefaultCompileItems=$(…)`）が関わるプロジェクトは当てはめず、申告だけ残す（Root Namespace 付きの名前は引けない）。"""
    nodes, edges, flags = built["vbcond"]
    assert edges == set()
    assert flags == {("dropped_syntax", "vbcond/App.vbproj", "vbproj_unevaluated", 1, "EnableDefaultCompileItems: $(Defaults)"),
                     ("unresolved_qualifier", "vbcond/Use.vb", "ACME.SHOP.BASKET", 2, None)}


def test_deeply_nested_and_invalid_encoding_vbproj_are_reported_not_fatal(tmp_path):
    """入れ子の深い XML・不正な UTF-8 の `.vbproj` で取り込み全体を落とさない（不正な文字コードは `vbproj_unreadable`）。"""
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "App.vbproj").write_text("<Project>" + "<A>" * 20000 + "</A>" * 20000 + "</Project>")
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "App.vbproj").write_bytes(b"<Project><PropertyGroup><RootNamespace>Ac\xffme</RootNamespace></PropertyGroup></Project>")
    flags = world_graph.build_world(tmp_path, "t")[2]
    got = {(f["from"], f["why"]) for f in flags if f.get("analyzer") == "vb"}
    assert ("bad/App.vbproj", "vbproj_unreadable") in got


def _flat(world_dir):
    nodes, edges, flags = world_graph.build_world(world_dir, "ana-v1")
    return ({(e["type"], _short(e["src"]), _short(e["dst"]), e.get("via"), e.get("line")) for e in edges},
            {(f["reason"], f.get("from") or f.get("doc"), f.get("name") or f.get("why"), f.get("line"), f.get("snippet"))
             for f in flags})


def test_root_level_props_and_conditional_root_namespace():
    """資料フォルダの root の `Directory.Build.props` も入れ子のプロジェクトの `RootNamespace` になる。`RootNamespace` の定義に条件つきのものが 1 つでもあれば未評価（Root は当てない・申告）。"""
    edges, flags = _flat(ROOT / "fixtures" / "corpus" / "ana-v1b")
    assert edges == {_field("nested/Use.vb#USE", "nested/Shop/Basket.vb#BASKET")}
    assert flags == {("dropped_syntax", "condroot/App.vbproj", "vbproj_unevaluated", 1, "RootNamespace: Other"),
                     ("unresolved_qualifier", "condroot/Use.vb", "ACME.SHOP.BASKET", 2, None)}


def test_props_without_any_vbproj_are_not_read_or_reported():
    """`.vbproj` の無い資料フォルダの `Directory.Build.props` は読まず、申告も出さない。"""
    edges, flags = _flat(ROOT / "fixtures" / "corpus" / "ana-v1c")
    assert edges == set()
    assert flags == {("unresolved_qualifier", "orphan/Use.vb", "SHOP.BASKET", 2, None)}


def test_nearest_folders_project_decides_and_link_includes_only_apply_without_one(built):
    """大文字の最上位フォルダでも `Compile Include`（`..\\`）を外部扱いにしない。最も近いフォルダの `.vbproj` が `Remove` したソースは、兄弟プロジェクトのリンクの Include では属さない（`Beta.Shop.Basket` は引けない）。"""
    nodes, edges, flags = built["VbCase"]
    assert edges == set()
    assert flags == {("unresolved_qualifier", "VbCase/Link/Use.vb", "BETA.SHOP.BASKET", 2, None)}


def test_vbproj_change_changes_the_world_signature_and_the_graph(tmp_path):
    """`.vbproj` は資料フォルダの 1 ファイルとして署名（`worker._sig`）の材料になる＝内容を変えると署名が変わって取り込み直しになり、結果も変わる。"""
    shutil.copytree(WORLD_DIR / "vbroot", tmp_path / "vbroot")
    before_sig = worker.world_signature_of_root(tmp_path)
    before = {(e["type"], _short(e["src"]), _short(e["dst"])) for e in world_graph.build_world(tmp_path, "t")[1]}
    assert ("INVOKES", "vbroot/UseRooted.vb#USEROOTED", "vbroot/Shop/Basket.vb#BASKET") in before
    proj = tmp_path / "vbroot" / "App.vbproj"
    proj.write_text(proj.read_text().replace("Acme", "Beta"))
    assert worker.world_signature_of_root(tmp_path) != before_sig
    after = {(e["type"], _short(e["src"]), _short(e["dst"])) for e in world_graph.build_world(tmp_path, "t")[1]}
    assert ("INVOKES", "vbroot/UseRooted.vb#USEROOTED", "vbroot/Shop/Basket.vb#BASKET") not in after
