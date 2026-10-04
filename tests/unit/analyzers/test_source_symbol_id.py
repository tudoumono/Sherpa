"""参照元の定義キー（`RefCandidate.source_symbol_id`）と `RefResult.file_context` の契約（ANA-14 S2）。

定義ノードがある言語（C・VB・C#・Java の型）は、参照を含む定義の `(rel_path, cid_key)` を出す。`cid_key` は `collect_defs` が返す
children の `DefItem.key` と同じ値。主体の定義・定義の外は省略（`None`＝ファイルの主体）。定義ノードの無い言語（COBOL・JS・SQL）は常に省略。
"""
from __future__ import annotations

from sherpa.ingest.analyzers.c import CAnalyzer
from sherpa.ingest.analyzers.cobol import CobolAnalyzer
from sherpa.ingest.analyzers.csharp import CSharpAnalyzer
from sherpa.ingest.analyzers.java import JavaAnalyzer
from sherpa.ingest.analyzers.js import JsAnalyzer
from sherpa.ingest.analyzers.vb import VbAnalyzer


def _owners(analyzer, text, rel):
    """`{(参照名, via, 行): source_symbol_id}`。"""
    return {(r.name, r.extra.get("via"), r.line): r.source_symbol_id
            for r in analyzer.extract_refs(text, rel).refs}


def _child_keys(analyzer, text, rel):
    return {c.key for c in analyzer.collect_defs(text, rel).children}


C_TEXT = (
    '#include "x.h"\n'                       # 1
    "int helper(void) {\n"                   # 2
    "    return 1;\n"                        # 3
    "}\n"                                    # 4
    "int run(void) {\n"                      # 5
    "    return helper();\n"                 # 6
    "}\n"                                    # 7
    "int seed = init();\n"                   # 8
)


def test_c_call_belongs_to_the_enclosing_function_and_file_scope_to_the_file():
    owners = _owners(CAnalyzer(), C_TEXT, "g/a.c")
    assert owners == {
        ("x.h", "include", 1): None,                       # #include はファイルの主体
        ("helper", "call", 6): ("g/a.c", "a.c.run"),
        ("init", "call", 8): None,                         # 関数の外（ファイル直下）
    }
    assert "a.c.run" in _child_keys(CAnalyzer(), C_TEXT, "g/a.c")


def test_c_same_named_functions_in_two_files_get_distinct_ids():
    a = _owners(CAnalyzer(), C_TEXT, "g/a.c")
    b = _owners(CAnalyzer(), C_TEXT, "g/b.c")
    assert a[("helper", "call", 6)] == ("g/a.c", "a.c.run")
    assert b[("helper", "call", 6)] == ("g/b.c", "b.c.run")


VB_TEXT = (
    "Namespace Acme\n"                       # 1
    "    Public Class Main1\n"               # 2
    "        Inherits Base1\n"               # 3
    "        Public Sub One()\n"             # 4
    "            Dim x As Foo1\n"            # 5
    "        End Sub\n"                      # 6
    "        Public Sub Two()\n"             # 7
    "            Dim y As Foo2\n"            # 8
    "        End Sub\n"                      # 9
    "    End Class\n"                        # 10
    "    Class Side1\n"                      # 11
    "        Inherits Base2\n"               # 12
    "        Public Sub Three()\n"           # 13
    "            Dim z As Foo3\n"            # 14
    "        End Sub\n"                      # 15
    "    End Class\n"                        # 16
    "End Namespace\n"
)


def test_vb_net_ref_belongs_to_procedure_then_non_primary_type_then_primary():
    owners = _owners(VbAnalyzer(), VB_TEXT, "g/Shop.vb")
    assert owners == {
        ("BASE1", "extends", 3): None,                           # 主体の型の直下
        ("FOO1", "field_type", 5): ("g/Shop.vb", "ACME.MAIN1.ONE"),
        ("FOO2", "field_type", 8): ("g/Shop.vb", "ACME.MAIN1.TWO"),
        ("BASE2", "extends", 12): ("g/Shop.vb", "ACME.SIDE1"),   # 主体以外の型（namespace 込みの cid_key）
        ("FOO3", "field_type", 14): ("g/Shop.vb", "ACME.SIDE1.THREE"),
    }
    assert {"ACME.MAIN1.ONE", "ACME.MAIN1.TWO", "ACME.SIDE1", "ACME.SIDE1.THREE"} <= _child_keys(VbAnalyzer(), VB_TEXT, "g/Shop.vb")


def test_vb6_procedure_refs_use_the_file_prefixed_key():
    text = ('Attribute VB_Name = "Legacy"\n'
            "Public Sub Start()\n"
            "    Call Finish\n"
            "End Sub\n"
            "Public Sub Finish()\n"
            "End Sub\n")
    assert _owners(VbAnalyzer(), text, "g/Legacy.bas") == {("FINISH", "call", 3): ("g/Legacy.bas", "LEGACY.START")}
    assert "LEGACY.START" in _child_keys(VbAnalyzer(), text, "g/Legacy.bas")


CS_TEXT = (
    "using System;\n"                         # 1
    "using Acme.Lib;\n"                      # 2
    "using Al = Acme.Real;\n"                # 3
    "using static Acme.Util;\n"              # 4
    "namespace Acme.App;\n"                  # 5
    "\n"                                     # 6
    "public class Alpha\n"                   # 7
    "{\n"                                    # 8
    "    private Beta b;\n"                  # 9
    "}\n"                                    # 10
    "\n"                                     # 11
    "class Gamma\n"                          # 12
    "{\n"                                    # 13
    "    private Delta d = new Delta();\n"   # 14
    "}\n"                                    # 15
)


def test_csharp_ref_belongs_to_its_type_and_non_primary_type_uses_namespaced_key():
    owners = _owners(CSharpAnalyzer(), CS_TEXT, "g/Two.cs")
    assert owners == {
        ("Beta", "field_type", 9): None,
        ("Delta", "field_type", 14): ("g/Two.cs", "Acme.App.Gamma"),
        ("Delta", "call", 14): ("g/Two.cs", "Acme.App.Gamma"),
    }
    assert "Acme.App.Gamma" in _child_keys(CSharpAnalyzer(), CS_TEXT, "g/Two.cs")


def test_csharp_file_context_carries_namespace_and_using_directives():
    ctx = CSharpAnalyzer().extract_refs(CS_TEXT, "g/Two.cs").file_context
    assert ctx.package == "Acme.App"
    assert [(i.kind, i.name, i.alias, i.static, i.line) for i in ctx.imports] == [
        ("wildcard", "System", None, False, 1),
        ("wildcard", "Acme.Lib", None, False, 2),
        ("alias", "Acme.Real", "Al", False, 3),
        ("single", "Acme.Util", None, True, 4),
    ]


JAVA_TEXT = (
    "package app;\n"                          # 1
    "\n"                                     # 2
    "import lib.Target;\n"                   # 3
    "\n"                                     # 4
    "public class Main {\n"                  # 5
    "    private Target t = new Target();\n" # 6
    "}\n"                                    # 7
    "\n"                                     # 8
    "class Side {\n"                         # 9
    "    private Other o;\n"                 # 10
    "}\n"                                    # 11
)


def test_java_ref_belongs_to_its_type_and_non_primary_type_uses_the_type_name():
    owners = _owners(JavaAnalyzer(), JAVA_TEXT, "g/app/Main.java")
    assert owners == {
        ("Target", "field_type", 6): None,
        ("Target", "call", 6): None,
        ("Other", "field_type", 10): ("g/app/Main.java", "Side"),
    }
    assert "Side" in _child_keys(JavaAnalyzer(), JAVA_TEXT, "g/app/Main.java")


def test_languages_without_definition_nodes_never_set_a_source_symbol_id():
    cobol = ("       PROGRAM-ID. ORDER-MAIN.\n       PROCEDURE DIVISION.\n"
             "           CALL 'ORDER-SUB'.\n")
    for analyzer, text, rel in ((CobolAnalyzer(), cobol, "m.cbl"),
                                (JsAnalyzer(), "import x from './b.js';\n", "a.js")):
        res = analyzer.extract_refs(text, rel)
        assert res.refs, analyzer.name
        assert all(r.source_symbol_id is None for r in res.refs), analyzer.name
        assert res.file_context is None, analyzer.name


VB_IMPORTS_TEXT = (
    "Imports Lib\n"                          # 1
    "Imports Al = Lib.Real\n"                # 2
    "Imports System.Text\n"                  # 3
    "\n"
    "Namespace Acme.App\n"                   # 5
    "    Public Class Main\n"
    "    End Class\n"
    "End Namespace\n"
)


def test_vb_net_file_context_carries_namespace_and_imports_and_vb6_has_none():
    ctx = VbAnalyzer().extract_refs(VB_IMPORTS_TEXT, "g/Main.vb").file_context
    assert ctx.package == "ACME.APP"
    assert [(i.kind, i.name, i.alias, i.line) for i in ctx.imports] == [
        ("wildcard", "LIB", None, 1), ("alias", "LIB.REAL", "AL", 2), ("wildcard", "SYSTEM.TEXT", None, 3)]
    assert VbAnalyzer().extract_refs('Attribute VB_Name = "Mod1"\nSub A()\nEnd Sub\n', "g/Mod1.bas").file_context is None


def test_vb_write_only_property_body_belongs_to_the_property():
    """`Get` を持たない（`Set` だけの）`Property` の本体の参照も、その Property が始点。"""
    text = ("Public Class Box\n"                         # 1
            "    Public WriteOnly Property Value As Integer\n"  # 2
            "        Set(ByVal v As Integer)\n"           # 3
            "            Store()\n"                      # 4
            "        End Set\n"                          # 5
            "    End Property\n"                         # 6
            "    Public Sub After()\n"                   # 7
            "        Other()\n"                          # 8
            "    End Sub\n"                              # 9
            "End Class\n")
    owners = _owners(VbAnalyzer(), text, "g/Box.vb")
    assert owners[("STORE", "call", 4)] == ("g/Box.vb", "BOX.VALUE")
    assert owners[("OTHER", "call", 8)] == ("g/Box.vb", "BOX.AFTER")


def _ambiguous(analyzer, text, rel):
    return [(d.line, d.snippet) for d in analyzer.extract_refs(text, rel).dropped
            if d.reason == "ambiguous_source_symbol"]


def test_c_line_shared_by_two_definitions_is_ambiguous_and_goes_to_the_primary():
    text = ("int f(void) { return 0; } int g(void) { return h(); }\n"   # 1: 2 つの定義が同じ行
            "int k(void) {\n"
            "    return h();\n"
            "}\n")
    assert _owners(CAnalyzer(), text, "g/a.c") == {("g", "call", 1): None, ("h", "call", 1): None,
                                                   ("h", "call", 3): ("g/a.c", "a.c.k")}
    assert _ambiguous(CAnalyzer(), text, "g/a.c") == [(1, "a.c.f")]


def test_java_and_csharp_line_shared_by_two_types_is_ambiguous_and_goes_to_the_primary():
    java = "public class A { void a() { new X(); } } class B { void b() { new Y(); } }\n"
    assert _owners(JavaAnalyzer(), java, "A.java") == {("X", "call", 1): None, ("Y", "call", 1): None}
    assert _ambiguous(JavaAnalyzer(), java, "A.java") == [(1, "A, B")]
    cs = "namespace N;\npublic class A { void a() { new X(); } } class B { void b() { new Y(); } }\n"
    assert _owners(CSharpAnalyzer(), cs, "A.cs") == {("X", "call", 2): None, ("Y", "call", 2): None}
    assert _ambiguous(CSharpAnalyzer(), cs, "A.cs") == [(2, "N.A, N.B")]


def test_csharp_partial_types_with_the_same_name_are_one_definition():
    text = ("namespace N;\n"
            "public partial class A\n{\n    private X x;\n}\n"
            "public partial class A\n{\n    private Y y;\n}\n"
            "class B\n{\n    private Z z;\n}\n")
    res = CSharpAnalyzer().collect_defs(text, "A.cs")
    assert res.primary.key == "N.A" and [c.key for c in res.children] == ["N.B"]      # N.A を child に二重登録しない
    assert _owners(CSharpAnalyzer(), text, "A.cs") == {
        ("X", "field_type", 4): None, ("Y", "field_type", 8): None, ("Z", "field_type", 12): ("A.cs", "N.B")}
