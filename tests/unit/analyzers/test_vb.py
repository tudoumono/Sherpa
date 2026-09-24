"""`VbAnalyzer` の単体テスト（アナライザ拡張 §9 波3 レーン C・ユーザー裁定 2026-09-06）。

VB.NET（`.vb`）・VB6/VBA エクスポート（`.bas`/`.cls`/`.frm`/`.ctl`）・VBScript（`.vbs`）を
1本のアナライザで扱う契約を固定する。世界層（`world_graph.build_world()` 経由の解決）は
`tests/unit/test_world_graph_vb1.py` が別途固定する。
"""
from __future__ import annotations

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.vb import VbAnalyzer

A = VbAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".vb", ".bas", ".cls", ".frm", ".ctl", ".vbs"})
    assert A.name == "vb"
    assert A.doctype == "vb"
    assert A.resolves_calls_by_simple_name is True


def test_accepts_all_vb_files_without_content_inspection():
    assert VbAnalyzer.accepts is Analyzer.accepts


# --- VB.NET: primary/children（Namespace/Class・qualified cid_key）---

def test_vbnet_public_class_is_primary_with_namespace_qualified_cid_key():
    text = "Namespace Acme.Order\n\n    Public Class OrderService\n    End Class\n\nEnd Namespace\n"
    res = A.collect_defs(text, "Order/OrderService.vb")
    assert res.primary.label == "Module" and res.primary.name == "ORDERSERVICE"
    assert res.primary.cid_key == "ACME.ORDER.ORDERSERVICE"


def test_vbnet_without_namespace_has_no_qualified_prefix():
    res = A.collect_defs("Public Class Standalone\nEnd Class\n", "Standalone.vb")
    assert res.primary.name == "STANDALONE"
    assert res.primary.cid_key is None


def test_vbnet_falls_back_to_first_type_when_no_public_type_present():
    res = A.collect_defs("Friend Class Helper\nEnd Class\n", "Helper.vb")
    assert res.primary is not None and res.primary.name == "HELPER"


def test_vbnet_sibling_type_becomes_child_with_qualified_cid_key():
    text = (
        "Namespace Acme.Order\n\n"
        "    Public Class OrderService\n    End Class\n\n"
        "    Friend Class InternalHelper\n    End Class\n\n"
        "End Namespace\n"
    )
    res = A.collect_defs(text, "Order/OrderService.vb")
    siblings = [c for c in res.children if c.name == "INTERNALHELPER"]
    assert len(siblings) == 1
    assert siblings[0].cid_key == "ACME.ORDER.INTERNALHELPER"


def test_vbnet_no_file_without_any_type_has_no_primary():
    res = A.collect_defs("' just a comment\n", "empty.vb")
    assert res.primary is None


# --- VB.NET: Sub/Function/Property children（`<Type>.<Name>`・c_kind）---

def test_vbnet_sub_and_function_become_children_qualified_with_type_name():
    text = (
        "Namespace Acme.Order\n\n"
        "    Public Class OrderService\n"
        "        Public Sub DoWork()\n        End Sub\n\n"
        "        Public Function Calc() As Integer\n        End Function\n"
        "    End Class\n\n"
        "End Namespace\n"
    )
    res = A.collect_defs(text, "Order/OrderService.vb")
    by_name = {c.name: c for c in res.children}
    assert by_name["DOWORK"].cid_key == "ORDERSERVICE.DOWORK"
    assert by_name["CALC"].cid_key == "ORDERSERVICE.CALC"
    assert by_name["DOWORK"].extra["c_kind"] == "definition"


def test_vbnet_auto_implemented_property_is_a_single_line_child_not_a_block():
    text = (
        "Namespace Acme.Order\n\n"
        "    Public Class OrderService\n"
        "        Public Property Total As Integer\n"
        "        Public Sub Next1()\n        End Sub\n"
        "    End Class\n\n"
        "End Namespace\n"
    )
    res = A.collect_defs(text, "Order/OrderService.vb")
    names = [c.name for c in res.children]
    assert "TOTAL" in names and "NEXT1" in names   # 後続の Sub が誤って property block に飲まれない


def test_vbnet_full_property_block_is_a_single_child():
    text = (
        "Namespace Acme.Order\n\n"
        "    Public Class OrderService\n"
        "        Public ReadOnly Property Total As Integer\n"
        "            Get\n"
        "                Return 1\n"
        "            End Get\n"
        "        End Property\n"
        "    End Class\n\n"
        "End Namespace\n"
    )
    res = A.collect_defs(text, "Order/OrderService.vb")
    assert [c.name for c in res.children] == ["TOTAL"]


def test_vbnet_nested_class_is_dropped_not_a_child():
    text = (
        "Namespace Acme.Order\n\n"
        "    Public Class Outer\n"
        "        Private Class Inner\n        End Class\n"
        "    End Class\n\n"
        "End Namespace\n"
    )
    res = A.collect_defs(text, "Outer.vb")
    assert [c.name for c in res.children if c.label == "Module"] == []
    assert [d.reason for d in res.dropped] == ["vb_nested_type"]


def test_procedure_inside_nested_class_is_not_a_child():
    """入れ子型の中の手続きも children にはしない（型自体の `vb_nested_type` Dropped だけ）。"""
    text = (
        "Namespace Acme.Order\n\n"
        "    Public Class Outer\n"
        "        Private Class Inner\n"
        "            Sub Helper()\n            End Sub\n"
        "        End Class\n"
        "    End Class\n\n"
        "End Namespace\n"
    )
    res = A.collect_defs(text, "Outer.vb")
    assert res.children == []
    assert [d.reason for d in res.dropped] == ["vb_nested_type"]


# --- 複数/入れ子の Namespace: それぞれの時点で有効な namespace で修飾する ---

def test_second_namespace_type_gets_its_own_qualified_prefix_not_the_first_namespace():
    text = (
        "Namespace A\n\n    Public Class Foo\n    End Class\n\nEnd Namespace\n\n"
        "Namespace B\n\n    Public Class Bar\n    End Class\n\nEnd Namespace\n"
    )
    res = A.collect_defs(text, "Foo.vb")
    assert res.primary.cid_key == "A.FOO"
    bar = next(c for c in res.children if c.name == "BAR")
    assert bar.cid_key == "B.BAR"


# --- Interface / MustOverride メンバー: 本体を持たないため frame を積まない ---

def test_interface_members_without_end_block_all_become_children():
    text = (
        "Interface IFoo\n"
        "    Function First() As Integer\n"
        "    Function Second() As Integer\n"
        "End Interface\n"
    )
    res = A.collect_defs(text, "IFoo.vb")
    assert {c.name for c in res.children} == {"FIRST", "SECOND"}


def test_mustoverride_members_without_end_block_all_become_children():
    text = (
        "Public MustInherit Class Base\n"
        "    Public MustOverride Sub First()\n"
        "    Public MustOverride Sub Second()\n"
        "End Class\n"
    )
    res = A.collect_defs(text, "Base.vb")
    assert {c.name for c in res.children} == {"FIRST", "SECOND"}


# --- VB6/VBA: Property Get/Let/Set の三つ組を1 child へ集約 ---

def test_vb6_property_get_let_set_triple_merges_into_one_child():
    text = (
        'Attribute VB_Name = "CORDER"\n\n'
        'Public Property Get Item(ByVal Index As Long) As Object\n'
        'End Property\n\n'
        'Public Property Let Item(ByVal Index As Long, ByVal Value As Object)\n'
        'End Property\n\n'
        'Public Property Set Item(ByVal Index As Long, ByVal Value As Object)\n'
        'End Property\n'
    )
    res = A.collect_defs(text, "CORDER.cls")
    items = [c for c in res.children if c.name == "ITEM"]
    assert len(items) == 1
    assert items[0].cid_key == "CORDER.ITEM"


# --- overload の重複 child は definition を declaration より優先して1件に集約 ---

def test_overloaded_sub_with_same_name_is_a_single_child():
    text = (
        "Class C\n"
        "    Overloads Sub F(x As Integer)\n    End Sub\n\n"
        "    Overloads Sub F(x As String)\n    End Sub\n"
        "End Class\n"
    )
    res = A.collect_defs(text, "C.vb")
    fs = [c for c in res.children if c.name == "F"]
    assert len(fs) == 1
    assert fs[0].cid_key == "C.F"


# --- VB6/VBA: primary 名の解決（VB_Name／Begin VB.Form／ファイル名ステム）---

def test_bas_primary_name_from_vb_name_attribute():
    res = A.collect_defs('Attribute VB_Name = "modUtil"\n\nSub Foo()\nEnd Sub\n', "src/modUtil.bas")
    assert res.primary.name == "MODUTIL"


def test_cls_primary_name_from_vb_name_attribute():
    res = A.collect_defs('Attribute VB_Name = "clsOrder"\n\nSub Foo()\nEnd Sub\n', "clsOrder.cls")
    assert res.primary.name == "CLSORDER"


def test_frm_primary_name_falls_back_to_begin_vb_form_when_vb_name_missing():
    text = "VERSION 5.00\nBegin VB.Form Form1\nEnd\n\nSub Foo()\nEnd Sub\n"
    res = A.collect_defs(text, "Form1.frm")
    assert res.primary.name == "FORM1"


def test_bas_primary_name_falls_back_to_file_stem_when_vb_name_missing():
    res = A.collect_defs("Sub Foo()\nEnd Sub\n", "src/legacy.bas")
    assert res.primary.name == "LEGACY"


def test_vbs_primary_name_is_file_stem():
    res = A.collect_defs("Sub Foo()\nEnd Sub\n", "script.vbs")
    assert res.primary.name == "SCRIPT"


# --- VB6/VBA: Sub/Function/Property children（`<primary名>.<Name>`）---

def test_bas_procedures_become_children_qualified_with_module_name():
    text = 'Attribute VB_Name = "modUtil"\n\nSub Foo()\nEnd Sub\n\nFunction Bar() As Integer\nEnd Function\n'
    res = A.collect_defs(text, "modUtil.bas")
    by_name = {c.name: c for c in res.children}
    assert by_name["FOO"].cid_key == "MODUTIL.FOO"
    assert by_name["BAR"].cid_key == "MODUTIL.BAR"


def test_declare_function_is_a_declaration_kind_child():
    text = ('Attribute VB_Name = "modWin"\n\n'
           'Private Declare Function GetTickCount Lib "kernel32" () As Long\n')
    res = A.collect_defs(text, "modWin.bas")
    assert len(res.children) == 1
    assert res.children[0].name == "GETTICKCOUNT"
    assert res.children[0].extra["c_kind"] == "declaration"


# --- 参照: Inherits/Implements → via=extends ---

def test_inherits_is_via_extends():
    text = "Namespace N\n\n    Public Class A\n        Inherits Base\n    End Class\n\nEnd Namespace\n"
    res = A.extract_refs(text, "A.vb")
    extends = [r for r in res.refs if r.extra.get("via") == "extends"]
    assert [(r.name) for r in extends] == ["BASE"]


def test_implements_is_also_via_extends():
    text = "Namespace N\n\n    Public Class A\n        Implements IFoo\n    End Class\n\nEnd Namespace\n"
    res = A.extract_refs(text, "A.vb")
    extends = [r for r in res.refs if r.extra.get("via") == "extends"]
    assert [(r.name) for r in extends] == ["IFOO"]
    assert not any(r.extra.get("via") == "implements" for r in res.refs)


# --- 参照: New（インスタンス化）→ via=call ---

def test_new_expression_is_via_call():
    text = "Class A\n    Sub Foo()\n        Dim x = New Helper()\n    End Sub\nEnd Class\n"
    res = A.extract_refs(text, "A.vb")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert any(r.name == "HELPER" for r in calls)


def test_dim_as_new_form_is_also_via_call():
    text = "Class A\n    Sub Foo()\n        Dim x As New Helper\n    End Sub\nEnd Class\n"
    res = A.extract_refs(text, "A.vb")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert any(r.name == "HELPER" for r in calls)


def test_qualified_new_expression_sets_qualified_extra():
    text = "Class A\n    Sub Foo()\n        Dim x = New Acme.Data.Repo()\n    End Sub\nEnd Class\n"
    res = A.extract_refs(text, "A.vb")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert calls[0].name == "ACME.DATA.REPO"
    assert calls[0].extra.get("qualified") is True


# --- 参照: 宣言型（Dim/引数/戻り値/WithEvents）→ via=field_type・組み込み型は除外 ---

def test_dim_as_custom_type_is_field_type_reference():
    text = "Class A\n    Sub Foo()\n        Dim r As Repo\n    End Sub\nEnd Class\n"
    res = A.extract_refs(text, "A.vb")
    field_refs = [r for r in res.refs if r.extra.get("via") == "field_type"]
    assert [r.name for r in field_refs] == ["REPO"]


def test_builtin_types_are_excluded_from_field_type_references():
    text = ("Class A\n"
           "    Dim a As String\n    Dim b As Integer\n    Dim c As Boolean\n"
           "    Dim d As Long\n    Dim e As Object\n    Dim f As Date\n"
           "    Dim g As Double\n    Dim h As Decimal\n    Dim i As Byte\n"
           "    Dim j As Char\n    Dim k As Short\n    Dim l As Single\n"
           "    Dim m As Variant\n"
           "End Class\n")
    res = A.extract_refs(text, "A.vb")
    field_refs = [r for r in res.refs if r.extra.get("via") == "field_type"]
    assert field_refs == []


def test_with_events_declaration_is_field_type_reference():
    text = "Class A\n    Private WithEvents Btn As CommandButton\nEnd Class\n"
    res = A.extract_refs(text, "A.vb")
    field_refs = [r for r in res.refs if r.extra.get("via") == "field_type"]
    assert [r.name for r in field_refs] == ["COMMANDBUTTON"]


def test_function_return_and_parameter_types_are_field_type_references():
    text = "Class A\n    Public Function Convert(src As Source) As Target\n    End Function\nEnd Class\n"
    res = A.extract_refs(text, "A.vb")
    field_refs = {r.name for r in res.refs if r.extra.get("via") == "field_type"}
    assert field_refs == {"SOURCE", "TARGET"}


# --- 参照: 手続き呼び出し（Call/括弧/括弧なし）→ via=call（単純名）---

def test_call_with_keyword_and_no_parens_is_via_call():
    res = A.extract_refs("Sub Main()\n    Call LoadOrders\nEnd Sub\n", "modMain.bas")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [r.name for r in calls] == ["LOADORDERS"]


def test_bare_call_with_args_and_no_parens_is_via_call():
    res = A.extract_refs('Sub Main()\n    LogMessage "hi"\nEnd Sub\n', "modMain.bas")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [r.name for r in calls] == ["LOGMESSAGE"]


def test_paren_call_is_via_call():
    res = A.extract_refs("Sub Main()\n    DoWork(1, 2)\nEnd Sub\n", "modMain.bas")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [r.name for r in calls] == ["DOWORK"]


def test_qualified_paren_call_sets_qualified_extra():
    res = A.extract_refs("Sub Main()\n    modData.LoadOrders(1)\nEnd Sub\n", "modMain.bas")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert calls[0].name == "MODDATA.LOADORDERS"
    assert calls[0].extra.get("qualified") is True


def test_assignment_statement_is_not_mistaken_for_a_bare_call():
    res = A.extract_refs("Sub Main()\n    total = 5\nEnd Sub\n", "modMain.bas")
    assert [r for r in res.refs if r.extra.get("via") == "call"] == []


def test_dim_declaration_is_not_mistaken_for_a_bare_call():
    res = A.extract_refs("Sub Main()\n    Dim total As Long\nEnd Sub\n", "modMain.bas")
    assert [r for r in res.refs if r.extra.get("via") == "call"] == []


def test_property_designer_value_line_is_not_mistaken_for_a_bare_call():
    """`.frm` の Begin/End プロパティ列（`Caption = "Main"`）を誤って呼び出しと判定しない。"""
    res = A.extract_refs('Begin VB.Form Form1\n   Caption   =   "Main"\nEnd\n', "Form1.frm")
    assert [r for r in res.refs if r.extra.get("via") == "call"] == []


def test_nested_beginproperty_designer_block_is_not_mistaken_for_a_bare_call():
    """入れ子の `BeginProperty ... EndProperty`（対応する Begin/End 全体）も読み飛ばす。"""
    text = ('Begin VB.Form Form1\n'
           '   BeginProperty Font\n'
           '      Name = "Arial"\n'
           '   EndProperty\n'
           'End\n')
    res = A.extract_refs(text, "Form1.frm")
    assert [r for r in res.refs if r.extra.get("via") == "call"] == []


def test_code_after_design_section_is_scanned_normally():
    """デザイナ部を抜けた後（`Attribute`／手続き本体）は通常どおり参照走査する。"""
    text = ('Begin VB.Form Form1\n'
           '   Caption   =   "Main"\n'
           'End\n'
           'Attribute VB_Name = "frmMain"\n\n'
           'Private Sub cmdOK_Click()\n'
           '    Call RealTarget\n'
           'End Sub\n')
    res = A.extract_refs(text, "Form1.frm")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [r.name for r in calls] == ["REALTARGET"]


def test_if_then_bare_call_on_single_line_is_via_call():
    """単一行 `If 条件 Then <文>` の実行部も括弧なし呼び出しとして走査する。"""
    res = A.extract_refs("Sub Main()\n    If flag Then Foo\nEnd Sub\n", "m.bas")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [r.name for r in calls] == ["FOO"]


def test_colon_separated_statements_both_become_calls():
    res = A.extract_refs("Sub Main()\n    Foo: Bar\nEnd Sub\n", "m.bas")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [r.name for r in calls] == ["FOO", "BAR"]


def test_builtin_bare_statements_are_excluded_from_call_references():
    text = 'Sub Main()\n    MsgBox "x"\n    Debug.Print x\nEnd Sub\n'
    res = A.extract_refs(text, "m.bas")
    assert [r for r in res.refs if r.extra.get("via") == "call"] == []


def test_self_file_call_is_not_excluded():
    """自ファイル内の手続き呼び出しも除外しない（primary/children の cid は常に異なる・C と同じ）。"""
    text = 'Attribute VB_Name = "modUtil"\n\nSub Main()\n    Helper\nEnd Sub\n\nSub Helper()\nEnd Sub\n'
    res = A.extract_refs(text, "modUtil.bas")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [r.name for r in calls] == ["HELPER"]


# --- late-bound / dynamic call の Dropped 化 ---

def test_createobject_is_dropped_as_vb_late_bound_with_progid_snippet():
    res = A.extract_refs('Sub Main()\n    Set c = CreateObject("ADODB.Connection")\nEnd Sub\n', "m.bas")
    assert [(d.reason, d.snippet) for d in res.dropped] == [("vb_late_bound", "ADODB.Connection")]
    assert res.refs == [] or all(r.extra.get("via") != "call" for r in res.refs)


def test_getobject_is_dropped_as_vb_late_bound():
    res = A.extract_refs('Sub Main()\n    Set x = GetObject(, "Excel.Application")\nEnd Sub\n', "m.bas")
    late = [d for d in res.dropped if d.reason == "vb_late_bound"]
    assert len(late) == 1


def test_callbyname_is_dropped_as_vb_dynamic_call():
    res = A.extract_refs('Sub Main()\n    CallByName obj, "Foo", VbMethod\nEnd Sub\n', "m.bas")
    assert [d.reason for d in res.dropped] == ["vb_dynamic_call"]


def test_application_run_bare_call_is_dropped_as_vb_dynamic_call():
    res = A.extract_refs('Sub Main()\n    Application.Run "MyMacro"\nEnd Sub\n', "m.bas")
    assert [d.reason for d in res.dropped] == ["vb_dynamic_call"]
    assert not any(r.extra.get("via") == "call" for r in res.refs)


# --- SQL 文字列（文字列リテラル／連結＋行継続）→ ACCESSES via=vba_sql ---

def test_single_sql_string_literal_produces_accesses_table_ref():
    text = 'Function Load()\n    sql = "SELECT * FROM ORDERS"\nEnd Function\n'
    res = A.extract_refs(text, "m.bas")
    sql_refs = [r for r in res.refs if r.edge_type == "ACCESSES"]
    assert [(r.kind, r.name, r.extra) for r in sql_refs] == [("Table", "ORDERS", {"via": "vba_sql"})]


def test_concatenated_sql_string_with_variable_placeholder_produces_accesses_table_ref():
    text = 'Function Load(id)\n    sql = "SELECT * FROM ORDERS WHERE ID = " & id\nEnd Function\n'
    res = A.extract_refs(text, "m.bas")
    sql_refs = [r for r in res.refs if r.edge_type == "ACCESSES"]
    assert [r.name for r in sql_refs] == ["ORDERS"]


def test_line_continuation_joins_concatenated_sql_string_across_physical_lines():
    text = ('Function Load(id)\n'
           '    sql = "SELECT * " & _\n'
           '          "FROM ORDERS WHERE ID = " & id\n'
           'End Function\n')
    res = A.extract_refs(text, "m.bas")
    sql_refs = [r for r in res.refs if r.edge_type == "ACCESSES"]
    assert [r.name for r in sql_refs] == ["ORDERS"]
    assert sql_refs[0].line == 2                          # 論理行の開始物理行を報告する


def test_sql_table_identifier_with_adjacent_placeholder_is_dropped_as_dynamic_table():
    """テーブル識別子の途中に動的な置換（`?`）が隣接する候補（`ORD` & suffix）は破棄する。"""
    text = 'Function Load(suffix)\n    sql = "SELECT * FROM ORD" & suffix\nEnd Function\n'
    res = A.extract_refs(text, "m.bas")
    assert [r for r in res.refs if r.edge_type == "ACCESSES"] == []
    dyn = [d for d in res.dropped if d.reason == "vba_sql_dynamic_table"]
    assert len(dyn) == 1


def test_sql_table_fully_replaced_by_placeholder_remains_unresolved():
    """`"FROM " & tbl`（識別子全体が `?` に置き換わる形）は現状どおり未解決のまま（Dropped もしない）。"""
    text = 'Function Load(tbl)\n    sql = "SELECT * FROM " & tbl\nEnd Function\n'
    res = A.extract_refs(text, "m.bas")
    assert [r for r in res.refs if r.edge_type == "ACCESSES"] == []
    assert [d for d in res.dropped if d.reason == "vba_sql_dynamic_table"] == []


def test_non_sql_string_literal_produces_no_accesses_ref():
    res = A.extract_refs('Sub Main()\n    msg = "hello world"\nEnd Sub\n', "m.bas")
    assert [r for r in res.refs if r.edge_type == "ACCESSES"] == []


# --- 大文字小文字の正規化（COBOL と同じ規則）---

def test_names_and_qualified_cid_keys_are_uppercase_normalized():
    text = "Namespace acme.order\n\n    Public Class orderService\n        Inherits baseService\n    End Class\n\nEnd Namespace\n"
    res = A.collect_defs(text, "OrderService.vb")
    assert res.primary.name == "ORDERSERVICE"
    assert res.primary.cid_key == "ACME.ORDER.ORDERSERVICE"
    refs = A.extract_refs(text, "OrderService.vb")
    assert refs.refs[0].name == "BASESERVICE"


# --- コメント（`'`・文頭の REM）・文字列リテラル（`""` エスケープ）---

def test_single_quote_comment_is_ignored():
    res = A.extract_refs("Sub Main()\n    ' Call FakeTarget\n    Call RealTarget\nEnd Sub\n", "m.bas")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [r.name for r in calls] == ["REALTARGET"]


def test_statement_leading_rem_comment_is_ignored():
    res = A.extract_refs("Sub Main()\n    REM Call FakeTarget\n    Call RealTarget\nEnd Sub\n", "m.bas")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [r.name for r in calls] == ["REALTARGET"]


def test_removehandler_keyword_is_not_mistaken_for_a_rem_comment():
    """`RemoveHandler` は `REM` 単語境界チェックにより誤ってコメント化されない。"""
    res = A.collect_defs('Attribute VB_Name = "m"\n\nSub Foo()\n    RemoveHandler x.Click, AddressOf Bar\nEnd Sub\n', "m.bas")
    assert [c.name for c in res.children] == ["FOO"]


def test_escaped_double_quote_inside_string_literal_does_not_break_scanning():
    text = 'Sub Main()\n    msg = "she said ""hi""" & vbCrLf\n    Call RealTarget\nEnd Sub\n'
    res = A.extract_refs(text, "m.bas")
    calls = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [r.name for r in calls] == ["REALTARGET"]
