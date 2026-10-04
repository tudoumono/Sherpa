"""ANA-14 S3（名前の解決を言語の規則に従わせる）の受入契約。

`fixtures/corpus/ana-s3` の各ケース（最上位フォルダごと）を `build_world` に通し、解決順の分岐ごとの否定ケース
（別 package・別 namespace の同名へ張らない・ワイルドカードで複数一致なら曖昧・static は型名を持ち込まない・完全修飾名が無ければ短名へ倒さない）
を、辺と未解決の申告の両方で固定する（受入条件 A・E）。
"""
from __future__ import annotations

import json
import pathlib
import shutil

import pytest

from sherpa.ingest import world_graph

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "ana-s3"


def _short(cid: str) -> str:
    return cid.split(":", 2)[2]


def _case(world_dir: pathlib.Path, top: str):
    """最上位フォルダ `top` のケースの全種類の辺と `flags` の全件。辺は `(型, 始点, 終点, via, 行)`、申告は `(理由, 参照元, 名前, 行)`。"""
    _nodes, edges, flags = world_graph.build_world(world_dir, "ana-s3")
    got_edges = {(e["type"], _short(e["src"]), _short(e["dst"]), e.get("via"), e.get("line"))
                 for e in edges if _short(e["src"]).startswith(f"{top}/")}
    got_flags = {(f["reason"], f.get("from") or f.get("doc"), f.get("name"), f.get("line"))
                 for f in flags if (f.get("from") or f.get("doc") or "").startswith(f"{top}/")}
    return got_edges, got_flags


def _inv(edges: set) -> set:
    return {("INVOKES",) + e for e in edges}


# 構造の包含（CONTAINS）の辺。ケースごとの期待に含める（全種類の辺を比べるため）。
CONTAINS = {
    "jfq": set(), "jimp": set(), "jgen": set(),
    "csgl": {("CONTAINS", "csgl/G.cs#B", "csgl/G.cs#UseB", None, 8)},
    "csraw": {("CONTAINS", "csraw/Holder.cs#Holder", "csraw/Holder.cs#Raw.After", None, 14)},
    "csus": {("CONTAINS", "csus/Two.cs#First", "csus/Two.cs#N2.Second", None, 13)},
    "vbgl": {("CONTAINS", "vbgl/G.vb#B", "vbgl/G.vb#USEB", None, 8)},
    "vbdp": {("CONTAINS", "vbdp/Two.vb#C", "vbdp/Two.vb#N1.C.A", None, 4),
             ("CONTAINS", "vbdp/Two.vb#C", "vbdp/Two.vb#N2.C", None, 13),
             ("CONTAINS", "vbdp/Two.vb#C", "vbdp/Two.vb#N2.C.A", None, 14)},
    "gl2": set(), "vbgn": set(), "csgn": set(), "csgn2": set(),
    "vbg2": {("CONTAINS", "vbg2/T.vb#T", "vbg2/T.vb#N1.I", None, 6)},
    "cs": {("CONTAINS", "cs/AliasNs/Own.cs#Target", "cs/AliasNs/Own.cs#AliasNs.Use", None, 10),
           ("CONTAINS", "cs/Multi/Two.cs#First", "cs/Multi/Two.cs#Ns2.Second", None, 11)},
    "vb": {("CONTAINS", "vb/AliasNs.vb#TARGET", "vb/AliasNs.vb#ALIASNS.USE", None, 9),
           ("CONTAINS", "vb/Lib/Target.vb#TARGET", "vb/Lib/Target.vb#LIB.DUP", None, 6),
           ("CONTAINS", "vb/Multi/Two.vb#FIRST", "vb/Multi/Two.vb#NS2.SECOND", None, 11)},
    "cinc": {("CONTAINS", "cinc/a/inc/there.h#there.h", "cinc/a/inc/there.h#there.h.there", None, 1),
             ("CONTAINS", "cinc/a/main.c#main.c", "cinc/a/main.c#main.c.main", None, 6),
             ("CONTAINS", "cinc/b/missing.h#missing.h", "cinc/b/missing.h#missing.h.missing", None, 1),
             ("CONTAINS", "cinc/b/plain.h#plain.h", "cinc/b/plain.h#plain.h.plain", None, 1)},
    "xmlq": {("CONTAINS", "xmlq/ctx/Mapper.xml#Mapper.xml", "xmlq/ctx/Mapper.xml#key:mapper:com.acme.Svc.one", None, 3),
             ("CONTAINS", "xmlq/ctx/beans.xml#beans.xml", "xmlq/ctx/beans.xml#key:bean:ghost", None, 4),
             ("CONTAINS", "xmlq/ctx/beans.xml#beans.xml", "xmlq/ctx/beans.xml#key:bean:svc", None, 3)},
}


@pytest.fixture(scope="module")
def built():
    return {top: _case(WORLD_DIR, top) for top in ("jfq", "jimp", "jgen", "cs", "csraw", "csus", "csgl", "vb", "vbgl", "vbdp", "gl2", "vbgn", "csgn", "csgn2", "vbg2", "cinc", "xmlq")}


def test_java_fully_qualified_name_is_kept_and_never_falls_back_to_the_short_name(built):
    """`new approved.Helper()`・`approved.Helper.go()`: `approved` はどこにも無く、別 package の `Helper` へ張らず未解決。実在する `lib.Target` へは張る。"""
    edges, flags = built["jfq"]
    assert edges == _inv({("jfq/app/Main.java#Main", "jfq/lib/Target.java#Target", "call", 6)}) | CONTAINS["jfq"]
    assert flags == {("unresolved_qualifier", "jfq/app/Main.java", "approved.Helper", 5),
                     ("unresolved_qualifier", "jfq/app/Main.java", "approved.Helper", 7)}


def test_java_resolution_follows_import_then_package_then_wildcard(built):
    """単一型 import は同じ package の同名・近くの同名より優先／同じ package は宣言された package で決め、フォルダの近さでは選ばない／
    ワイルドカードで一意なら張る／static import は型の名前を持ち込まない／どれにも無ければ未解決／ワイルドカード 2 つで同名が両方にあれば曖昧。"""
    edges, flags = built["jimp"]
    assert edges == _inv({
        ("jimp/app/Main.java#Main", "jimp/lib/Target.java#Target", "call", 9),       # import lib.Target（同 package の app.Target ではない）
        ("jimp/app/Main.java#Main", "jimp/far/a/b/Local.java#Local", "call", 10),   # package app の Local（近くの package near ではない）
        ("jimp/app/Main.java#Main", "jimp/w1/Thing.java#Thing", "call", 11),        # import w1.*
    }) | CONTAINS["jimp"]
    assert flags == {
        ("unresolved", "jimp/app/Main.java", "Missing", 12),
        ("unresolved", "jimp/app/Main.java", "Statics", 13),     # `import static lib.Statics.make` は型 Statics を持ち込まない
        ("unresolved", "jimp/app/Main.java", "Absent", 14),
        ("ambiguous", "jimp/app/Wild.java", "Dup", 7),           # import w1.* と import w2.* の両方に Dup
    }


def test_csharp_resolution_follows_namespace_then_using(built):
    """自分の namespace（親へさかのぼる）が using より先／using N は N の型に限る／`using static` は型の名前を持ち込まない／using 2 つに同名があれば曖昧。"""
    edges, flags = built["cs"]
    assert edges == _inv({
        ("cs/Acme/App/Main.cs#Main", "cs/Lib/Target.cs#Target", "field_type", 9),        # using Lib（近くの Main.Ns.Target ではない）
        ("cs/Acme/App/Main.cs#Main", "cs/Acme/Sibling.cs#Sibling", "field_type", 10),    # 親の namespace Acme
        ("cs/Acme/App/Main.cs#Main", "cs/Acme/App/Own.cs#Own", "field_type", 14),        # 自分の namespace が using Lib の Own より先
        ("cs/Alias/Use.cs#Use", "cs/Lib/Target.cs#Target", "field_type", 7),             # `using A = Lib;` の `A.Target`（別名を接頭辞として展開）
        ("cs/Alias/Use.cs#Use", "cs/LibG/Gtarget.cs#Gtarget", "field_type", 8),          # 別ファイルの `global using LibG;`（同じ最上位フォルダの全ファイルへ効く）
        # 別名は完全修飾名として展開する（今の namespace `AliasNs` の同名 Target ではなく Lib.Target・ドットの無い別名 B = GlobalT も展開）
        ("cs/AliasNs/Own.cs#AliasNs.Use", "cs/Lib/Target.cs#Target", "field_type", 12),
        ("cs/AliasNs/Own.cs#AliasNs.Use", "cs/GlobalT.cs#GlobalT", "field_type", 13),
        # 1 ファイルの複数の namespace ブロック: 参照ごとに自分を囲む namespace で解決し、始点も型ごと
        ("cs/Multi/Two.cs#First", "cs/Ns1/Shared.cs#Shared", "field_type", 5),
        ("cs/Multi/Two.cs#Ns2.Second", "cs/Ns2/Shared.cs#Shared", "field_type", 13),
    }) | CONTAINS["cs"]
    assert flags == {
        ("unresolved", "cs/Acme/App/Main.cs", "Hidden", 11),
        ("ambiguous", "cs/Acme/App/Main.cs", "Dup", 12),
        ("unresolved", "cs/Acme/App/Main.cs", "Statics", 13),
    }


def test_vb_resolution_follows_namespace_then_imports(built):
    """VB.NET も C# と同じ（`Imports N` は N の型・完全修飾の型名は子の型にも届く）。"""
    edges, flags = built["vb"]
    assert edges == _inv({
        ("vb/Acme/Main.vb#MAIN", "vb/Lib/Target.vb#TARGET", "field_type", 7),            # Imports Lib
        ("vb/Acme/Main.vb#MAIN", "vb/Acme/Sibling.vb#SIBLING", "field_type", 8),         # 親の Namespace Acme
        ("vb/Acme/Main.vb#MAIN", "vb/Lib/Target.vb#LIB.DUP", "field_type", 11),          # 完全修飾 Lib.Dup（Target.vb の 2 つ目の型）
        ("vb/Alias/Use.vb#USE", "vb/Lib/Target.vb#TARGET", "field_type", 6),              # `Imports Al = Lib` の `Al.Target`
        ("vb/AliasNs.vb#ALIASNS.USE", "vb/Lib/Target.vb#TARGET", "field_type", 10),       # 別名は今の Namespace の同名 Target より先に完全修飾名として展開
        ("vb/AliasNs.vb#ALIASNS.USE", "vb/Elsewhere/Hidden.vb#HIDDEN", "field_type", 11), # ドットの無い別名
        ("vb/Multi/Two.vb#FIRST", "vb/Ns1/Shared.vb#SHARED", "field_type", 4),            # 複数の Namespace ブロックは参照ごとに
        ("vb/Multi/Two.vb#NS2.SECOND", "vb/Ns2/Shared.vb#SHARED", "field_type", 12),
    }) | CONTAINS["vb"]
    assert flags == {
        ("unresolved", "vb/Acme/Main.vb", "HIDDEN", 9),
        ("ambiguous", "vb/Acme/Main.vb", "DUP", 10),
    }


def test_c_include_with_a_relative_path_is_exact_or_unresolved(built):
    """`#include "inc/missing.h"` は相対パスに無ければ別フォルダの同名へ倒さず未解決／実在すれば張る／パス区切りの無い `"plain.h"` は今までどおり／`<…>` は対象外。"""
    edges, flags = built["cinc"]
    assert edges == _inv({
        ("cinc/a/main.c#main.c", "cinc/a/inc/there.h#there.h", "include", 2),
        ("cinc/a/main.c#main.c", "cinc/b/plain.h#plain.h", "include", 3),
    }) | CONTAINS["cinc"]
    assert flags == {("unresolved_qualifier", "cinc/a/main.c", "inc/missing.h", 1)}      # `{` が次の行にある `int main(void)` は定義（子定義 main）で、呼び出しではない


def test_explicit_import_target_does_not_change_when_same_named_classes_are_added_or_moved(tmp_path):
    """明示 import の接続先は、同名のクラスを足してもフォルダを動かしても変わらない。"""
    moved = tmp_path / "ana-s3"
    shutil.copytree(WORLD_DIR, moved)
    (moved / "jimp" / "app" / "x").mkdir()
    shutil.move(str(moved / "jimp" / "app" / "Target.java"), str(moved / "jimp" / "app" / "x" / "Target.java"))
    (moved / "jimp" / "app" / "Target.java").write_text("package misc;\n\npublic class Target {\n}\n", encoding="utf-8")
    edges, _flags = _case(moved, "jimp")
    assert ("INVOKES", "jimp/app/Main.java#Main", "jimp/lib/Target.java#Target", "call", 9) in edges
    assert not [e for e in edges if e[4] == 9 and e[2] != "jimp/lib/Target.java#Target"]


def test_fully_qualified_names_in_xml_jsp_and_shell_are_exact_or_unresolved(built):
    """完全修飾名が実在すれば張り（Spring の bean class・MyBatis の namespace・JSP の useBean・`java` 起動の FQCN）、
    無くて同じ短名（別 package の `Ghost`）だけがあるときは辺を張らず `unresolved_qualifier` を申告する。"""
    edges, flags = built["xmlq"]
    assert edges == _inv({
        ("xmlq/ctx/beans.xml#beans.xml", "xmlq/src/Svc.java#Svc", "bean_class", 3),
        ("xmlq/src/Svc.java#Svc", "xmlq/ctx/Mapper.xml#Mapper.xml", "mapper_namespace", 2),
        ("xmlq/web/page.jsp#page.jsp", "xmlq/src/Svc.java#Svc", "bean_class", 1),
        ("xmlq/bin/run.sh#run.sh", "xmlq/src/Svc.java#Svc", "call", 2),
    }) | CONTAINS["xmlq"]
    assert flags == {
        ("unresolved", "xmlq/ctx/Mapper.xml", "DUAL", 3),                   # SQL 本文の表名（定義なし）
        ("unresolved_qualifier", "xmlq/ctx/beans.xml", "com.missing.Ghost", 4),
        ("unresolved_qualifier", "xmlq/ctx/Mapper.xml", "com.missing.Ghost", 3),
        ("unresolved_qualifier", "xmlq/web/page.jsp", "com.missing.Ghost", 2),
        ("unresolved_qualifier", "xmlq/bin/run.sh", "com.missing.Ghost", 3),
    }


def test_generic_bounds_do_not_swallow_the_real_extends_clause(built):
    """`class A<T extends q.B> extends q.Super`・`interface I<T extends q.B> extends q.J`: 型パラメータの境界と継承節を取り違えず、継承の辺を張る。"""
    edges, flags = built["jgen"]
    assert edges == _inv({("jgen/p/A.java#A", "jgen/q/Super.java#Super", "extends", 3),
                          ("jgen/p/I.java#I", "jgen/q/J.java#J", "extends", 3)}) | CONTAINS["jgen"]
    assert flags == set()


def test_csharp_raw_strings_and_preprocessor_lines_do_not_hide_later_types(built):
    """raw string（内側に引用符や `{ class Fake {} }`・補間付きの raw string）と `#region {` の括弧を数えず、後ろの型と辺を落とさない。"""
    edges, flags = built["csraw"]
    assert edges == _inv({("csraw/Holder.cs#Holder", "csraw/Dep.cs#Dep", "field_type", 10),
                          ("csraw/Holder.cs#Raw.After", "csraw/Dep.cs#Dep", "field_type", 16)}) | CONTAINS["csraw"]
    assert flags == set()


def test_csharp_using_inside_a_namespace_block_applies_only_to_that_block(built):
    """`namespace N1 { using LibU; … } namespace N2 { … }`: N1 の参照は LibU の型へ張り、N2 には効かず未解決。"""
    edges, flags = built["csus"]
    assert edges == _inv({("csus/Two.cs#First", "csus/LibU/Tu.cs#Tu", "field_type", 7)}) | CONTAINS["csus"]
    assert flags == {("unresolved", "csus/Two.cs", "Tu", 15)}


def test_types_outside_any_namespace_do_not_resolve_in_the_first_namespace(built):
    """`namespace A { class B }` と、namespace の外の型の `B` 参照: グローバルの参照は `A.B` へ張らず未解決（C#・VB.NET）。"""
    assert built["csgl"] == (CONTAINS["csgl"], {("unresolved", "csgl/G.cs", "B", 10)})
    assert built["vbgl"] == (CONTAINS["vbgl"], {("unresolved", "vbgl/G.vb", "B", 9)})


# 各ケースの `flags` の全件（dict 全体）。
FLAGS = {
    "csgn2": [
    ],
    "vbg2": [
    ],
    "vbgn": [
    ],
    "csgn": [
    ],
    "vbdp": [
    ],
    "gl2": [
        {"reason": "unresolved", "from": "gl2/UseJ.java", "kind": "Module", "name": "LibType", "line": 4, "via": "field_type", "from_def": {"file": "gl2/UseJ.java", "key": None}},
    ],
    "jfq": [
        {"reason": "unresolved_qualifier", "from": "jfq/app/Main.java", "kind": "Module", "name": "approved.Helper", "line": 5, "via": "call", "from_def": {"file": "jfq/app/Main.java", "key": None}},
        {"reason": "unresolved_qualifier", "from": "jfq/app/Main.java", "kind": "Module", "name": "approved.Helper", "line": 7, "via": "call", "from_def": {"file": "jfq/app/Main.java", "key": None}},
    ],
    "jimp": [
        {"reason": "unresolved", "from": "jimp/app/Main.java", "kind": "Module", "name": "Missing", "line": 12, "via": "call", "from_def": {"file": "jimp/app/Main.java", "key": None}},
        {"reason": "unresolved", "from": "jimp/app/Main.java", "kind": "Module", "name": "Statics", "line": 13, "via": "call", "from_def": {"file": "jimp/app/Main.java", "key": None}},
        {"reason": "unresolved", "from": "jimp/app/Main.java", "kind": "Module", "name": "Absent", "line": 14, "via": "call", "from_def": {"file": "jimp/app/Main.java", "key": None}},
        {"reason": "ambiguous", "from": "jimp/app/Wild.java", "kind": "Module", "name": "Dup", "line": 7, "via": "field_type", "from_def": {"file": "jimp/app/Wild.java", "key": None}, "candidates": 2},
    ],
    "jgen": [
    ],
    "cs": [
        {"reason": "unresolved", "from": "cs/Acme/App/Main.cs", "kind": "Module", "name": "Hidden", "line": 11, "via": "field_type", "from_def": {"file": "cs/Acme/App/Main.cs", "key": None}},
        {"reason": "ambiguous", "from": "cs/Acme/App/Main.cs", "kind": "Module", "name": "Dup", "line": 12, "via": "field_type", "from_def": {"file": "cs/Acme/App/Main.cs", "key": None}, "candidates": 2},
        {"reason": "unresolved", "from": "cs/Acme/App/Main.cs", "kind": "Module", "name": "Statics", "line": 13, "via": "field_type", "from_def": {"file": "cs/Acme/App/Main.cs", "key": None}},
    ],
    "csraw": [
    ],
    "csus": [
        {"reason": "unresolved", "from": "csus/Two.cs", "kind": "Module", "name": "Tu", "line": 15, "via": "field_type", "from_def": {"file": "csus/Two.cs", "key": "N2.Second"}},
    ],
    "csgl": [
        {"reason": "unresolved", "from": "csgl/G.cs", "kind": "Module", "name": "B", "line": 10, "via": "field_type", "from_def": {"file": "csgl/G.cs", "key": "UseB"}},
    ],
    "vb": [
        {"reason": "ambiguous", "from": "vb/Acme/Main.vb", "kind": "Module", "name": "DUP", "line": 10, "via": "field_type", "from_def": {"file": "vb/Acme/Main.vb", "key": None}, "candidates": None},
        {"reason": "unresolved", "from": "vb/Acme/Main.vb", "kind": "Module", "name": "HIDDEN", "line": 9, "via": "field_type", "from_def": {"file": "vb/Acme/Main.vb", "key": None}},
    ],
    "vbgl": [
        {"reason": "unresolved", "from": "vbgl/G.vb", "kind": "Module", "name": "B", "line": 9, "via": "field_type", "from_def": {"file": "vbgl/G.vb", "key": "USEB"}},
    ],
    "cinc": [
        {"reason": "unresolved_qualifier", "from": "cinc/a/main.c", "kind": "Module", "name": "inc/missing.h", "line": 1, "via": "include", "from_def": {"file": "cinc/a/main.c", "key": None}},
    ],
    "xmlq": [
        {"reason": "unresolved_qualifier", "from": "xmlq/bin/run.sh", "kind": "Module", "name": "com.missing.Ghost", "line": 3, "via": "call", "from_def": {"file": "xmlq/bin/run.sh", "key": None}},
        {"reason": "unresolved_qualifier", "from": "xmlq/ctx/Mapper.xml", "kind": "Module", "name": "com.missing.Ghost", "line": 3, "via": "mapper_type", "from_def": {"file": "xmlq/ctx/Mapper.xml", "key": None}},
        {"reason": "unresolved", "from": "xmlq/ctx/Mapper.xml", "kind": "Table", "name": "DUAL", "line": 3, "via": "mapper_sql", "from_def": {"file": "xmlq/ctx/Mapper.xml", "key": None}},
        {"reason": "unresolved_qualifier", "from": "xmlq/ctx/beans.xml", "kind": "Module", "name": "com.missing.Ghost", "line": 4, "via": "bean_class", "from_def": {"file": "xmlq/ctx/beans.xml", "key": None}},
        {"reason": "unresolved_qualifier", "from": "xmlq/web/page.jsp", "kind": "Module", "name": "com.missing.Ghost", "line": 2, "via": "bean_class", "from_def": {"file": "xmlq/web/page.jsp", "key": None}},
    ],
}

# 各ケースのノードの cid（`label:world:path#key` の `label:world:` を除いたもの）。
NODES = {
    "csgn2": ["csgn2/App/G.cs#G", "csgn2/App/Lib/T.cs#T", "csgn2/App/U.cs#U", "csgn2/GlobalG.cs#G", "csgn2/Lib/T.cs#T"],
    "vbg2": ["vbg2/Root.vb#R", "vbg2/T.vb#N1.I", "vbg2/T.vb#T", "vbg2/Use.vb#USE"],
    "vbgn": ["vbgn/A.vb#T", "vbgn/B.vb#U", "vbgn/Use.vb#USE"],
    "csgn": ["csgn/Lib/T.cs#T", "csgn/Use.cs#Use"],
    "vbdp": ["vbdp/T1.vb#T1", "vbdp/T2.vb#T2", "vbdp/Two.vb#C", "vbdp/Two.vb#N1.C.A", "vbdp/Two.vb#N2.C", "vbdp/Two.vb#N2.C.A"],
    "gl2": ["gl2/LibX/LibType.cs#LibType", "gl2/UseC.cs#UseC", "gl2/UseJ.java#UseJ"],
    "jfq": ["jfq/app/Main.java#Main", "jfq/lib/Target.java#Target", "jfq/other/Helper.java#Helper"],
    "jimp": ["jimp/app/Local.java#Local", "jimp/app/Main.java#Main", "jimp/app/Target.java#Target", "jimp/app/Wild.java#Wild", "jimp/far/a/b/Local.java#Local", "jimp/lib/Statics.java#Statics", "jimp/lib/Target.java#Target", "jimp/w1/Dup.java#Dup", "jimp/w1/Thing.java#Thing", "jimp/w2/Dup.java#Dup"],
    "jgen": ["jgen/p/A.java#A", "jgen/p/I.java#I", "jgen/q/B.java#B", "jgen/q/J.java#J", "jgen/q/Super.java#Super"],
    "cs": ["cs/Acme/App/Main.cs#Main", "cs/Acme/App/Own.cs#Own", "cs/Acme/Sibling.cs#Sibling", "cs/Alias/Use.cs#Use", "cs/AliasNs/Own.cs#AliasNs.Use", "cs/AliasNs/Own.cs#Target", "cs/Elsewhere/Hidden.cs#Hidden", "cs/GlobalT.cs#GlobalT", "cs/Lib/Dup.cs#Dup", "cs/Lib/Own.cs#Own", "cs/Lib/Target.cs#Target", "cs/Lib2/Dup.cs#Dup", "cs/LibG/Gtarget.cs#Gtarget", "cs/LibS/Statics.cs#Statics", "cs/Main/Target.cs#Target", "cs/Multi/Two.cs#First", "cs/Multi/Two.cs#Ns2.Second", "cs/Ns1/Shared.cs#Shared", "cs/Ns2/Shared.cs#Shared"],
    "csraw": ["csraw/Dep.cs#Dep", "csraw/Holder.cs#Holder", "csraw/Holder.cs#Raw.After"],
    "csus": ["csus/LibU/Tu.cs#Tu", "csus/Two.cs#First", "csus/Two.cs#N2.Second"],
    "csgl": ["csgl/G.cs#B", "csgl/G.cs#UseB"],
    "vb": ["vb/Acme/Main.vb#MAIN", "vb/Acme/Sibling.vb#SIBLING", "vb/Alias/Use.vb#USE", "vb/AliasNs.vb#ALIASNS.USE", "vb/AliasNs.vb#TARGET", "vb/Elsewhere/Hidden.vb#HIDDEN", "vb/Lib/Target.vb#LIB.DUP", "vb/Lib/Target.vb#TARGET", "vb/Lib2/Dup.vb#DUP", "vb/Multi/Two.vb#FIRST", "vb/Multi/Two.vb#NS2.SECOND", "vb/Ns1/Shared.vb#SHARED", "vb/Ns2/Shared.vb#SHARED"],
    "vbgl": ["vbgl/G.vb#B", "vbgl/G.vb#USEB"],
    "cinc": ["cinc/a/inc/there.h#there.h", "cinc/a/inc/there.h#there.h.there", "cinc/a/main.c#main.c", "cinc/a/main.c#main.c.main", "cinc/b/missing.h#missing.h", "cinc/b/missing.h#missing.h.missing", "cinc/b/plain.h#plain.h", "cinc/b/plain.h#plain.h.plain"],
    "xmlq": ["xmlq/bin/run.sh#run.sh", "xmlq/ctx/Mapper.xml#Mapper.xml", "xmlq/ctx/Mapper.xml#key:mapper:com.acme.Svc.one", "xmlq/ctx/beans.xml#beans.xml", "xmlq/ctx/beans.xml#key:bean:ghost", "xmlq/ctx/beans.xml#key:bean:svc", "xmlq/other/Ghost.java#Ghost", "xmlq/src/Svc.java#Svc", "xmlq/web/page.jsp#page.jsp"],
}


def _canon(flag: dict) -> str:
    return json.dumps(flag, sort_keys=True, ensure_ascii=False)


@pytest.mark.parametrize("top", sorted(FLAGS))
def test_every_flag_and_node_of_each_case_is_pinned_in_full(top):
    """`flags` は dict 全体（理由・参照元・種別・名前・行・via・追加の属性）、ノードは cid の集合を完全一致で固定する
    （`dropped_syntax`・`file_context_missing`・未知の申告・予期しないノードも落とす）。"""
    nodes, _edges, flags = world_graph.build_world(WORLD_DIR, "ana-s3")
    got_flags = sorted(_canon(f) for f in flags if (f.get("from") or f.get("doc") or "").startswith(f"{top}/"))
    assert got_flags == sorted(_canon(f) for f in FLAGS[top])
    assert sorted(_short(n["cid"]) for n in nodes if n["path"].startswith(f"{top}/")) == NODES[top]


def test_vb_procedures_with_the_same_name_in_different_namespaces_keep_their_own_start(built):
    """1 つの .vb の `N1.C.A` と `N2.C.A`: 手続きのキーは Namespace 込みで別の定義になり、それぞれの参照の始点がそれぞれの手続きになる。"""
    edges, flags = built["vbdp"]
    assert edges == _inv({("vbdp/Two.vb#N1.C.A", "vbdp/T1.vb#T1", "field_type", 5),
                          ("vbdp/Two.vb#N2.C.A", "vbdp/T2.vb#T2", "field_type", 15)}) | CONTAINS["vbdp"]
    assert flags == set()


def test_global_using_applies_only_to_files_of_the_same_language(built):
    """C# の `global using LibX;` は同じ最上位フォルダの C# には効くが、Java の同名の型参照（`LibType`）には効かず未解決。"""
    edges, flags = built["gl2"]
    assert edges == _inv({("gl2/UseC.cs#UseC", "gl2/LibX/LibType.cs#LibType", "field_type", 5)})
    assert flags == {("unresolved", "gl2/UseJ.java", "LibType", 4)}


def test_global_qualifier_is_dropped_in_vb_and_csharp(built):
    """VB の `Namespace Global`（入れ子も）・`Namespace Global.N1`・`Global.N1.U` の `Global.` と、C# の `global::Lib.T` の `global::` は外して、同じ名前の定義へ張る。"""
    assert built["vbgn"][0] == _inv({("vbgn/Use.vb#USE", "vbgn/A.vb#T", "field_type", 4),
                                     ("vbgn/Use.vb#USE", "vbgn/B.vb#U", "field_type", 5)})
    assert built["csgn"][0] == _inv({("csgn/Use.cs#Use", "csgn/Lib/T.cs#T", "field_type", 5)})
    assert built["vbgn"][1] == built["csgn"][1] == set()


def test_csharp_global_qualifier_resolves_as_an_absolute_name(built):
    """`namespace App.Lib { T }`・`namespace Lib { T }` があるとき、`namespace App` の `global::Lib.T` は `App.Lib.T` ではなく `Lib.T`。
    単独の `global::G` も、`App.G` ではなくグローバルの `G` へ張る。"""
    assert built["csgn2"][0] == _inv({("csgn2/App/U.cs#U", "csgn2/Lib/T.cs#T", "field_type", 5),
                                      ("csgn2/App/U.cs#U", "csgn2/GlobalG.cs#G", "field_type", 6)})
    assert built["csgn2"][1] == set()


def test_vb_global_in_imports_alias_inherits_implements_and_bare_global_namespace(built):
    """`Imports A = Global.N1`＋`A.T`・`Inherits Global.N1.T`・`Implements Global.N1.I` は `N1` の型へ、`Namespace Global`（単独＝ルート）の型 `R` は名前だけで引ける。"""
    assert built["vbg2"][0] == _inv({("vbg2/Use.vb#USE", "vbg2/T.vb#T", "extends", 6),
                                     ("vbg2/Use.vb#USE", "vbg2/T.vb#N1.I", "extends", 7),
                                     ("vbg2/Use.vb#USE", "vbg2/Root.vb#R", "field_type", 9)}) | CONTAINS["vbg2"]
    assert built["vbg2"][1] == set()
