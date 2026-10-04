"""`VbAnalyzer` の単体テスト（アナライザ拡張 §9 波3 レーン C）。

VB.NET（`.vb`）・VB6/VBA エクスポート（`.bas`/`.cls`/`.frm`/`.ctl`）・VBScript（`.vbs`）を
1本のアナライザで扱う契約を、入力ソース片 → (定義・参照・Dropped) の表で固定する。
世界層の解決は `tests/unit/test_world_graph_vb1.py` が別途固定する。
"""
from __future__ import annotations

import pytest

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.vb import VbAnalyzer

A = VbAnalyzer()
ANY = object()


def test_extensions_and_name():
    assert A.extensions == frozenset({".vb", ".bas", ".cls", ".frm", ".ctl", ".vbs"})
    assert A.name == "vb"
    assert A.doctype == "vb"
    assert A.resolves_calls_by_simple_name is True


def test_accepts_all_vb_files_without_content_inspection():
    assert VbAnalyzer.accepts is Analyzer.accepts


def _ns(body, ns="Acme.Order"):
    return f"Namespace {ns}\n\n{body}\nEnd Namespace\n"


# ---- primary（名前・cid_key）----
PRIMARY_CASES = {
    "vbnet_public_class_namespace_qualified": (
        _ns("    Public Class OrderService\n    End Class\n"), "Order/OrderService.vb",
        "ORDERSERVICE", "ACME.ORDER.ORDERSERVICE"),
    "vbnet_without_namespace_no_qualified_prefix": (
        "Public Class Standalone\nEnd Class\n", "Standalone.vb", "STANDALONE", None),
    "vbnet_falls_back_to_first_type": ("Friend Class Helper\nEnd Class\n", "Helper.vb", "HELPER", ANY),
    "second_namespace_type_has_own_prefix": (
        "Namespace A\n\n    Public Class Foo\n    End Class\n\nEnd Namespace\n\n"
        "Namespace B\n\n    Public Class Bar\n    End Class\n\nEnd Namespace\n", "Foo.vb", "FOO", "A.FOO"),
    "names_and_cid_keys_uppercase_normalized": (
        _ns("    Public Class orderService\n        Inherits baseService\n    End Class\n", "acme.order"),
        "OrderService.vb", "ORDERSERVICE", "ACME.ORDER.ORDERSERVICE"),
    "bas_vb_name_attribute": ('Attribute VB_Name = "modUtil"\n\nSub Foo()\nEnd Sub\n', "src/modUtil.bas", "MODUTIL", ANY),
    "cls_vb_name_attribute": ('Attribute VB_Name = "clsOrder"\n\nSub Foo()\nEnd Sub\n', "clsOrder.cls", "CLSORDER", ANY),
    "frm_falls_back_to_begin_vb_form": (
        "VERSION 5.00\nBegin VB.Form Form1\nEnd\n\nSub Foo()\nEnd Sub\n", "Form1.frm", "FORM1", ANY),
    "bas_falls_back_to_file_stem": ("Sub Foo()\nEnd Sub\n", "src/legacy.bas", "LEGACY", ANY),
    "vbs_primary_is_file_stem": ("Sub Foo()\nEnd Sub\n", "script.vbs", "SCRIPT", ANY),
}


@pytest.mark.parametrize("text,path,name,cid_key", PRIMARY_CASES.values(), ids=PRIMARY_CASES)
def test_collect_defs_primary(text, path, name, cid_key):
    p = A.collect_defs(text, path).primary
    assert p.label == "Module" and p.name == name
    if cid_key is not ANY:
        assert p.cid_key == cid_key


def test_vbnet_file_without_any_type_has_no_primary():
    assert A.collect_defs("' just a comment\n", "empty.vb").primary is None


# ---- children（Sub/Function/Property/Type・`<Type>.<Name>`・c_kind）----
# 期待値: {name: (cid_key, c_kind)}（ANY は検査しない）・Dropped の reason 列（None は検査しない）
CHILDREN_CASES = {
    "vbnet_sibling_type_qualified": (
        _ns("    Public Class OrderService\n    End Class\n\n    Friend Class InternalHelper\n    End Class\n"),
        "Order/OrderService.vb", {"INTERNALHELPER": ("ACME.ORDER.INTERNALHELPER", ANY)}, None),
    "vbnet_sub_and_function_qualified_with_type": (
        _ns("    Public Class OrderService\n        Public Sub DoWork()\n        End Sub\n\n"
            "        Public Function Calc() As Integer\n        End Function\n    End Class\n"),
        "Order/OrderService.vb",
        {"DOWORK": ("ORDERSERVICE.DOWORK", "definition"), "CALC": ("ORDERSERVICE.CALC", ANY)}, None),
    "vbnet_auto_property_single_line_not_a_block": (
        _ns("    Public Class OrderService\n        Public Property Total As Integer\n"
            "        Public Sub Next1()\n        End Sub\n    End Class\n"),
        "Order/OrderService.vb", {"TOTAL": (ANY, ANY), "NEXT1": (ANY, ANY)}, None),     # 後続 Sub が property block に飲まれない
    "vbnet_full_property_block_single_child": (
        _ns("    Public Class OrderService\n        Public ReadOnly Property Total As Integer\n            Get\n"
            "                Return 1\n            End Get\n        End Property\n    End Class\n"),
        "Order/OrderService.vb", {"TOTAL": (ANY, ANY)}, None),
    "vbnet_nested_class_dropped_not_child": (
        _ns("    Public Class Outer\n        Private Class Inner\n        End Class\n    End Class\n"),
        "Outer.vb", {}, ["vb_nested_type"]),
    "procedure_inside_nested_class_not_child": (
        _ns("    Public Class Outer\n        Private Class Inner\n            Sub Helper()\n            End Sub\n"
            "        End Class\n    End Class\n"),
        "Outer.vb", {}, ["vb_nested_type"]),
    "interface_members_without_end_block": (
        "Interface IFoo\n    Function First() As Integer\n    Function Second() As Integer\nEnd Interface\n",
        "IFoo.vb", {"FIRST": (ANY, ANY), "SECOND": (ANY, ANY)}, None),
    "mustoverride_members_without_end_block": (
        "Public MustInherit Class Base\n    Public MustOverride Sub First()\n    Public MustOverride Sub Second()\n"
        "End Class\n", "Base.vb", {"FIRST": (ANY, ANY), "SECOND": (ANY, ANY)}, None),
    "vb6_property_get_let_set_merge_into_one": (
        'Attribute VB_Name = "CORDER"\n\nPublic Property Get Item(ByVal Index As Long) As Object\nEnd Property\n\n'
        'Public Property Let Item(ByVal Index As Long, ByVal Value As Object)\nEnd Property\n\n'
        'Public Property Set Item(ByVal Index As Long, ByVal Value As Object)\nEnd Property\n',
        "CORDER.cls", {"ITEM": ("CORDER.ITEM", ANY)}, None),
    "overloaded_sub_single_child": (
        "Class C\n    Overloads Sub F(x As Integer)\n    End Sub\n\n    Overloads Sub F(x As String)\n    End Sub\n"
        "End Class\n", "C.vb", {"F": ("C.F", ANY)}, None),
    "bas_procedures_qualified_with_module_name": (
        'Attribute VB_Name = "modUtil"\n\nSub Foo()\nEnd Sub\n\nFunction Bar() As Integer\nEnd Function\n',
        "modUtil.bas", {"FOO": ("MODUTIL.FOO", ANY), "BAR": ("MODUTIL.BAR", ANY)}, None),
    "declare_function_is_declaration_kind": (
        'Attribute VB_Name = "modWin"\n\nPrivate Declare Function GetTickCount Lib "kernel32" () As Long\n',
        "modWin.bas", {"GETTICKCOUNT": (ANY, "declaration")}, None),
    "removehandler_not_a_rem_comment": (
        'Attribute VB_Name = "m"\n\nSub Foo()\n    RemoveHandler x.Click, AddressOf Bar\nEnd Sub\n',
        "m.bas", {"FOO": (ANY, ANY)}, None),
}


@pytest.mark.parametrize("text,path,children,dropped", CHILDREN_CASES.values(), ids=CHILDREN_CASES)
def test_collect_defs_children(text, path, children, dropped):
    res = A.collect_defs(text, path)
    got = {c.name: c for c in res.children}
    assert set(got) == set(children)
    for name, (cid, c_kind) in children.items():
        assert cid is ANY or got[name].cid_key == cid
        assert c_kind is ANY or got[name].extra["c_kind"] == c_kind
    if dropped is not None:
        assert [d.reason for d in res.dropped] == dropped


# ---- extract_refs ----
# 期待値: (via フィルタ, 名前列)。via == "ACCESSES" は edge_type で絞る。extra は {名前: 部分 dict}。
def _src(body, head="Sub Main()\n"):
    return head + body + "End Sub\n"


def _cls(body):
    return f"Class A\n    Sub Foo()\n        {body}\n    End Sub\nEnd Class\n"


REFS_CASES = {
    "inherits_is_extends": (_ns("    Public Class A\n        Inherits Base\n    End Class\n", "N"), "A.vb",
                            "extends", ["BASE"], None),
    "implements_is_extends": (_ns("    Public Class A\n        Implements IFoo\n    End Class\n", "N"), "A.vb",
                              "extends", ["IFOO"], None),
    "new_expression_call": (_cls("Dim x = New Helper()"), "A.vb", "call", ["HELPER"], None),
    "dim_as_new_call": (_cls("Dim x As New Helper"), "A.vb", "call", ["HELPER"], None),
    "qualified_new_sets_qualified": (_cls("Dim x = New Acme.Data.Repo()"), "A.vb", "call", ["ACME.DATA.REPO"],
                                     {"ACME.DATA.REPO": {"qualified": True}}),
    "dim_as_custom_type_field_type": (_cls("Dim r As Repo"), "A.vb", "field_type", ["REPO"], None),
    "builtin_types_excluded": (
        "Class A\n" + "".join(f"    Dim v{i} As {t}\n" for i, t in enumerate(
            "String Integer Boolean Long Object Date Double Decimal Byte Char Short Single Variant".split()))
        + "End Class\n", "A.vb", "field_type", [], None),
    "with_events_field_type": ("Class A\n    Private WithEvents Btn As CommandButton\nEnd Class\n", "A.vb",
                               "field_type", ["COMMANDBUTTON"], None),
    "function_return_and_parameter_types": (
        "Class A\n    Public Function Convert(src As Source) As Target\n    End Function\nEnd Class\n", "A.vb",
        "field_type", ["SOURCE", "TARGET"], None),
    "call_keyword_without_parens": (_src("    Call LoadOrders\n"), "modMain.bas", "call", ["LOADORDERS"], None),
    "bare_call_with_args": (_src('    LogMessage "hi"\n'), "modMain.bas", "call", ["LOGMESSAGE"], None),
    "paren_call": (_src("    DoWork(1, 2)\n"), "modMain.bas", "call", ["DOWORK"], None),
    "qualified_paren_call": (_src("    modData.LoadOrders(1)\n"), "modMain.bas", "call", ["MODDATA.LOADORDERS"],
                             {"MODDATA.LOADORDERS": {"qualified": True}}),
    "assignment_not_a_call": (_src("    total = 5\n"), "modMain.bas", "call", [], None),
    "dim_declaration_not_a_call": (_src("    Dim total As Long\n"), "modMain.bas", "call", [], None),
    "frm_property_value_line_not_a_call": ('Begin VB.Form Form1\n   Caption   =   "Main"\nEnd\n', "Form1.frm",
                                           "call", [], None),
    "nested_beginproperty_block_not_a_call": (
        'Begin VB.Form Form1\n   BeginProperty Font\n      Name = "Arial"\n   EndProperty\nEnd\n', "Form1.frm",
        "call", [], None),
    "code_after_design_section_scanned": (
        'Begin VB.Form Form1\n   Caption   =   "Main"\nEnd\nAttribute VB_Name = "frmMain"\n\n'
        'Private Sub cmdOK_Click()\n    Call RealTarget\nEnd Sub\n', "Form1.frm", "call", ["REALTARGET"], None),
    "single_line_if_then_bare_call": (_src("    If flag Then Foo\n"), "m.bas", "call", ["FOO"], None),
    "colon_separated_statements": (_src("    Foo: Bar\n"), "m.bas", "call", ["FOO", "BAR"], None),
    "builtin_bare_statements_excluded": (_src('    MsgBox "x"\n    Debug.Print x\n'), "m.bas", "call", [], None),
    "self_file_call_not_excluded": (
        'Attribute VB_Name = "modUtil"\n\nSub Main()\n    Helper\nEnd Sub\n\nSub Helper()\nEnd Sub\n', "modUtil.bas",
        "call", ["HELPER"], None),
    "single_quote_comment_ignored": (_src("    ' Call FakeTarget\n    Call RealTarget\n"), "m.bas", "call",
                                     ["REALTARGET"], None),
    "rem_comment_ignored": (_src("    REM Call FakeTarget\n    Call RealTarget\n"), "m.bas", "call", ["REALTARGET"], None),
    "escaped_double_quote_in_string": (
        _src('    msg = "she said ""hi""" & vbCrLf\n    Call RealTarget\n'), "m.bas", "call", ["REALTARGET"], None),
    # SQL 文字列（文字列リテラル／連結＋行継続）→ ACCESSES via=vba_sql
    "sql_string_literal": ('Function Load()\n    sql = "SELECT * FROM ORDERS"\nEnd Function\n', "m.bas",
                           "ACCESSES", ["ORDERS"], {"ORDERS": {"via": "vba_sql"}}),
    "sql_concatenated_with_variable": (
        'Function Load(id)\n    sql = "SELECT * FROM ORDERS WHERE ID = " & id\nEnd Function\n', "m.bas",
        "ACCESSES", ["ORDERS"], None),
    "sql_line_continuation_joined": (
        'Function Load(id)\n    sql = "SELECT * " & _\n          "FROM ORDERS WHERE ID = " & id\nEnd Function\n',
        "m.bas", "ACCESSES", ["ORDERS"], None),
    "sql_table_adjacent_placeholder_discarded": (
        'Function Load(suffix)\n    sql = "SELECT * FROM ORD" & suffix\nEnd Function\n', "m.bas", "ACCESSES", [], None),
    "sql_table_fully_placeholder_unresolved": (
        'Function Load(tbl)\n    sql = "SELECT * FROM " & tbl\nEnd Function\n', "m.bas", "ACCESSES", [], None),
    "non_sql_string_no_accesses": (_src('    msg = "hello world"\n'), "m.bas", "ACCESSES", [], None),
}


def _select(res, via):
    return [r for r in res.refs if (r.edge_type == "ACCESSES" if via == "ACCESSES" else r.extra.get("via") == via)]


@pytest.mark.parametrize("text,path,via,names,extras", REFS_CASES.values(), ids=REFS_CASES)
def test_extract_refs(text, path, via, names, extras):
    got = _select(A.extract_refs(text, path), via)
    assert [r.name for r in got] == names
    for r in got:
        for k, v in (extras or {}).get(r.name, {}).items():
            assert r.extra.get(k) == v


def test_implements_is_not_via_implements():
    res = A.extract_refs(_ns("    Public Class A\n        Implements IFoo\n    End Class\n", "N"), "A.vb")
    assert not any(r.extra.get("via") == "implements" for r in res.refs)


def test_uppercase_normalized_inherits_name():
    text = _ns("    Public Class orderService\n        Inherits baseService\n    End Class\n", "acme.order")
    assert A.extract_refs(text, "OrderService.vb").refs[0].name == "BASESERVICE"


def test_sql_line_continuation_reports_logical_line_start():
    text = ('Function Load(id)\n    sql = "SELECT * " & _\n          "FROM ORDERS WHERE ID = " & id\nEnd Function\n')
    assert [r.line for r in _select(A.extract_refs(text, "m.bas"), "ACCESSES")] == [2]


# ---- late-bound / dynamic call は Dropped ----
@pytest.mark.parametrize("body,dropped", [
    ('    Set c = CreateObject("ADODB.Connection")\n', [("vb_late_bound", "ADODB.Connection")]),
    ('    Set x = GetObject(, "Excel.Application")\n', [("vb_late_bound", None)]),
    ('    CallByName obj, "Foo", VbMethod\n', [("vb_dynamic_call", None)]),
    ('    Application.Run "MyMacro"\n', [("vb_dynamic_call", None)]),
], ids=["createobject", "getobject", "callbyname", "application_run"])
def test_late_bound_and_dynamic_calls_are_dropped_not_resolved(body, dropped):
    res = A.extract_refs(_src(body), "m.bas")
    assert [(d.reason, d.snippet) if s else (d.reason, None) for d, (_, s) in zip(res.dropped, dropped)] == dropped
    assert len(res.dropped) == len(dropped)
    assert not any(r.extra.get("via") == "call" for r in res.refs)


def test_sql_table_adjacent_placeholder_dropped_as_dynamic_table_but_fully_replaced_is_not():
    adjacent = A.extract_refs('Function Load(suffix)\n    sql = "SELECT * FROM ORD" & suffix\nEnd Function\n', "m.bas")
    assert len([d for d in adjacent.dropped if d.reason == "vba_sql_dynamic_table"]) == 1
    full = A.extract_refs('Function Load(tbl)\n    sql = "SELECT * FROM " & tbl\nEnd Function\n', "m.bas")
    assert [d for d in full.dropped if d.reason == "vba_sql_dynamic_table"] == []
