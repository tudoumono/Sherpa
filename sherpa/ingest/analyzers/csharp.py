"""C# アナライザ（本体のみ・FW なし）。`class`/`interface`/`struct`/`enum`/`record` を主体定義（`Module`）とし、同一ファイル内の非 public 型を子定義（`CONTAINS`）として返す。`namespace X;` と `namespace X { ... }` の両方に対応し、`cid_key` は `Namespace.Type`。

参照（`INVOKES`）:
- base list の全エントリを `via=extends`（`enum X : int` は対象外・型引数は取らない）。
- トップレベル型の直下のフィールド/プロパティ/イベント/インデクサの宣言型・メソッド/演算子/デリゲートの戻り値型・コンストラクタ/メソッド/演算子/デリゲートの引数型を `via=field_type`（型引数は入れ子も辿る）、`new X(...)` を `via=call`（型引数・キャスト・`typeof`・`as` の型も同じ入れ子の抽出）。戻り値型・プロパティ/イベント/インデクサの型・レコード（クラス）の主コンストラクタ引数も読む。メソッド本体の中の宣言（ローカル関数を含む）は取らない。
- `using X;`・`using Alias = N.T;` は参照にしない。全ての型参照に `type_ref`（共通層が `file_context` の namespace・using・別名で解決する）を付ける。`global::X` の絶対指定は `absolute`。型パラメータ名（`T` など）は参照にしない。
- `namespace` と `using`（通常・`static`・エイリアス・`global`）は `RefResult.file_context` として共通層へ渡す。1 ファイルに namespace ブロックが複数あれば、型ごとにその型を囲む namespace を `cid_key`・`source_symbol_id`・`file_context.namespaces`（参照の行から引く）に使う。namespace ブロックの中の `using` は `ImportItem.scope` にそのブロックの範囲を持つ。
- 参照の始点（`source_symbol_id`）は、その行を本体に含む型。主体以外の型は `(rel_path, "<namespace>.<型名>")`（`cid_key`）、主体の型・型の外は省略（ファイルの主体）。同じ行に複数の型がかかる行は決められないので主体にし、`Dropped("ambiguous_source_symbol")`。メソッド単位の始点は持たない。
- `partial class` は `Dropped("cs_partial")`、入れ子の型は `Dropped("nested_type")`、構文エラーの領域は `Dropped("syntax_error")` で申告する。
読み取りは Tree-sitter（tree-sitter-c-sharp・`_ts`）。プリプロセッサの `#if` 内・構文エラーの領域内の宣言も、木に現れた範囲で読む。大文字小文字は区別する。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import _ts
from ._base import Analyzer, DefItem, DefResult, Dropped, FileContext, ImportItem, RefCandidate, RefResult


CSHARP_EXT = frozenset({".cs"})

_TYPE_NODES = frozenset({
    "class_declaration", "interface_declaration", "struct_declaration", "enum_declaration",
    "record_declaration", "record_struct_declaration",
})
_TYPE_KEYWORDS = frozenset({"class", "interface", "struct", "enum", "record"})
# 宣言の並びをそのまま包むノード（`#if` の枝・構文エラーの領域）。中の宣言を読み続ける。
_TRANSPARENT = frozenset({"preproc_if", "preproc_else", "preproc_elif", "ERROR"})

_JDK_LIKE_COMMON_TYPES = frozenset({
    "object", "Object", "string", "String", "bool", "Boolean", "byte", "Byte", "sbyte",
    "short", "Int16", "int", "Int32", "long", "Int64", "float", "Single", "double", "Double",
    "decimal", "Decimal", "char", "Char", "void", "var", "dynamic", "Task", "Action", "Func",
    "List", "IList", "IEnumerable", "ICollection", "Dictionary", "IDictionary", "Nullable",
    "DateTime", "TimeSpan", "Guid", "Exception", "Type",
})

_SNIPPET_MAX = 120


def _named(node) -> list:
    return [c for c in node.named_children if c.type != "comment"]


def _members(node):
    """`node` の子を宣言の並びとして順に返す（`#if` の枝・構文エラーの領域は中身を展開する）。"""
    for ch in node.children:
        if ch.type in _TRANSPARENT:
            yield from _members(ch)
        else:
            yield ch


def _line_text(lines: list, line: int) -> str:
    return lines[line - 1].strip()[:_SNIPPET_MAX] if 0 < line <= len(lines) else ""


def _type_name(parsed: _ts.Parsed, n):
    """型ノードを `(名前, global:: の絶対指定か)` にする。ジェネリクスの型引数は含めない。型名にならないもの（`int`・タプル等）は `(None, False)`。"""
    t = n.type
    if t == "identifier":
        return parsed.text(n).lstrip("@"), False
    if t == "generic_name":
        kids = _named(n)
        return (parsed.text(kids[0]).lstrip("@"), False) if kids else (None, False)
    if t == "alias_qualified_name":
        kids = _named(n)
        if len(kids) < 2:
            return None, False
        name, _ = _type_name(parsed, kids[-1])
        return name, parsed.text(kids[0]) == "global"
    if t == "qualified_name":
        kids = _named(n)
        if len(kids) < 2:
            return None, False
        head, absolute = _type_name(parsed, kids[0])
        tail, _ = _type_name(parsed, kids[-1])
        if head is None or tail is None:
            return None, False
        return f"{head}.{tail}", absolute
    return None, False


def _type_args(n):
    """型ノードの中の型引数（`generic_name` の `type_argument_list`）を左から順に返す（`A<B>.C<D>` は B と D）。"""
    if n.type == "generic_name":
        for ch in n.children:
            if ch.type == "type_argument_list":
                yield from _named(ch)
    elif n.type in ("qualified_name", "alias_qualified_name"):
        for ch in _named(n):
            yield from _type_args(ch)
    elif n.type == "tuple_type":
        for el in _named(n):
            tn = el.child_by_field_name("type")
            if tn is not None:
                yield tn


def _unwrap(n):
    """`Foo?`・`Foo[]`・`Foo*` の外側を外して中の型ノードにする。"""
    while n is not None and n.type in ("nullable_type", "array_type", "pointer_type", "ref_type"):
        kids = _named(n)
        n = kids[0] if kids else None
    return n


# 型パラメータを宣言できるノード。`type_parameter_list` は本体（`{ … }`・`=>`）より前に並ぶ。
_GENERIC_OWNERS = _TYPE_NODES | {"method_declaration", "local_function_statement", "delegate_declaration"}
_BODY_NODES = frozenset({"declaration_list", "block", "arrow_expression_clause", "enum_member_declaration_list"})


def _type_params(parsed: _ts.Parsed, node) -> frozenset:
    """`node` を囲む型・メソッドが宣言した型パラメータ名。"""
    names: set = set()
    cur = node
    while cur is not None:
        if cur.type in _GENERIC_OWNERS:
            for ch in cur.children:
                if ch.type in _BODY_NODES:
                    break
                if ch.type == "type_parameter_list":
                    for tp in _named(ch):
                        ident = next((c for c in _named(tp) if c.type == "identifier"), None)
                        if ident is not None:
                            names.add(parsed.text(ident))
        cur = cur.parent
    return frozenset(names)


def _emit(refs: list, name: str, absolute: bool, line: int, via: str) -> None:
    """`INVOKES` 候補を 1 件積む。`.` を含む完全修飾名は `qualified` 参照にする（単純名へ落とさない）。別名（`using A = …`）は共通層が `file_context` の別名で展開する。"""
    extra = {"via": via, "type_ref": True}
    if absolute:                                       # `global::X.Y`＝今の namespace の下を探さず完全修飾名として解決する
        extra["absolute"] = True
    if "." in name:
        extra["qualified"] = True
    refs.append(RefCandidate("INVOKES", "Module", name, line, extra=extra))


def _emit_declared(parsed: _ts.Parsed, refs: list, type_node, tparams: frozenset) -> None:
    """宣言型（と型引数）を `INVOKES(via=field_type)` 候補として積む。共通型・小文字始まり（`var`/プリミティブ）・型パラメータは除く。"""
    n = _unwrap(type_node)
    if n is None:
        return
    name, absolute = _type_name(parsed, n)
    if name is not None:
        simple = name.rsplit(".", 1)[-1]
        if simple[:1].isupper() and simple not in _JDK_LIKE_COMMON_TYPES and simple not in tparams:
            _emit(refs, name, absolute, _ts.start_line(n), "field_type")
    for arg in _type_args(n):
        _emit_declared(parsed, refs, arg, tparams)


@dataclass
class _Decl:
    node: object
    name: str
    line: int          # 宣言キーワード（`partial` が直前にあればそれ）の行
    end: int           # 本体の閉じ括弧の行（本体が無ければ宣言の終わりの行）
    public: bool
    partial: bool
    ns: str | None
    start: int = 0     # 属性リスト・修飾子を含む宣言の開始行（参照の始点・namespace の範囲に使う）

    @property
    def key(self) -> str:
        return f"{self.ns}.{self.name}" if self.ns else self.name


def _decl_line(node) -> int:
    kw = next((c for c in node.children if c.type in _TYPE_KEYWORDS), None)
    if kw is None:
        return _ts.start_line(node)
    prev = kw.prev_sibling
    return _ts.start_line(prev if prev is not None and prev.type == "modifier" and prev.text == b"partial" else kw)


def _make_decl(parsed: _ts.Parsed, node, ns) -> _Decl | None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    mods = {parsed.text(c) for c in node.children if c.type == "modifier"}
    body = node.child_by_field_name("body")
    return _Decl(node, parsed.text(name_node), _decl_line(node), _ts.end_line(body if body is not None else node),
                 "public" in mods, "partial" in mods, ns, _ts.start_line(node))


@dataclass
class _Scan:
    top: list          # `_Decl`（namespace 直下の型・出現順）
    nested: list       # `_Decl`（入れ子の型）
    imports: list      # `ImportItem`


def _using(parsed: _ts.Parsed, node, scope) -> ImportItem | None:
    kids = _named(node)
    if not kids:
        return None
    alias_node = node.child_by_field_name("name")
    target = kids[-1]
    name, _abs = _type_name(parsed, target)
    if name is None:
        name = re.sub(r"\s+", "", parsed.text(target))
    static = any(c.type == "static" for c in node.children)
    return ImportItem(kind=("alias" if alias_node is not None else "single" if static else "wildcard"), name=name,
                      alias=parsed.text(alias_node) if alias_node is not None else None, static=static,
                      line=_ts.start_line(target), is_global=any(c.type == "global" for c in node.children), scope=scope)


def _nested_decls(parsed: _ts.Parsed, node, out: list) -> None:
    body = node.child_by_field_name("body")
    for ch in _members(body) if body is not None else ():
        if ch.type in _TYPE_NODES:
            d = _make_decl(parsed, ch, None)
            if d is not None:
                out.append(d)
            _nested_decls(parsed, ch, out)


def _scan(parsed: _ts.Parsed) -> _Scan:
    file_ns = next((re.sub(r"\s+", "", parsed.text(c.child_by_field_name("name")))
                    for c in _members(parsed.root)
                    if c.type == "file_scoped_namespace_declaration" and c.child_by_field_name("name") is not None), None)
    scan = _Scan([], [], [])

    def visit(container, block_ns, scope) -> None:
        for ch in _members(container):
            if ch.type == "using_directive":
                item = _using(parsed, ch, scope)
                if item is not None:
                    scan.imports.append(item)
            elif ch.type == "namespace_declaration":
                nm, body = ch.child_by_field_name("name"), ch.child_by_field_name("body")
                if nm is None or body is None:
                    continue
                part = re.sub(r"\s+", "", parsed.text(nm))
                visit(body, f"{block_ns}.{part}" if block_ns else part, (_ts.start_line(body), _ts.end_line(body)))
            elif ch.type in _TYPE_NODES:
                d = _make_decl(parsed, ch, block_ns if block_ns is not None else file_ns)
                if d is not None:
                    scan.top.append(d)
                    _nested_decls(parsed, ch, scan.nested)

    visit(parsed.root, None, None)
    return scan


def _primary_index(top: list) -> int:
    """トップレベル型のうちファイルの主体にする型の添字（最初の public 型・無ければ先頭）。`collect_defs` と `extract_refs` で共有する。"""
    return next((i for i, d in enumerate(top) if d.public), 0)


def _base_refs(parsed: _ts.Parsed, d: _Decl, refs: list) -> None:
    if d.node.type == "enum_declaration":
        return                                         # `enum X : int` の `:` は underlying type であり base list ではない
    for bl in (c for c in d.node.children if c.type == "base_list"):
        for entry in _named(bl):
            if entry.type == "primary_constructor_base_type":
                kids = _named(entry)
                entry = kids[0] if kids else entry
            name, absolute = _type_name(parsed, entry)
            if name is not None:
                _emit(refs, name, absolute, _ts.start_line(entry), "extends")
            for arg in _type_args(entry):                  # `I<A<B>>` の型引数も型の使用
                _emit_declared(parsed, refs, arg, _type_params(parsed, entry))


def _param_type_nodes(params):
    for i, p in enumerate(params.children if params is not None else ()):
        if p.type == "parameter":
            tn = p.child_by_field_name("type")
        else:
            tn = p if params.field_name_for_child(i) == "type" else None   # `params T[] x` は `parameter` に包まれない
        if tn is not None:
            yield tn


def _member_type_nodes(parsed: _ts.Parsed, member):
    """型宣言そのもの（レコード・クラスの主コンストラクタ）とトップレベル型の直下のメンバーから、宣言型として読む型ノードを順に返す。
    戻り値型・プロパティ/イベント/インデクサの型・引数型を含む。"""
    t = member.type
    if t in _TYPE_NODES:
        yield from _param_type_nodes(next((c for c in member.children if c.type == "parameter_list"), None))
    elif t in ("field_declaration", "event_field_declaration"):
        vd = next((c for c in member.children if c.type == "variable_declaration"), None)
        tn = vd.child_by_field_name("type") if vd is not None else None
        if tn is not None:
            yield tn
    elif t in ("property_declaration", "event_declaration", "indexer_declaration"):
        tn = member.child_by_field_name("type")
        if tn is not None:
            yield tn
        yield from _param_type_nodes(next((c for c in member.children if c.type == "bracketed_parameter_list"), None))
    elif t in ("method_declaration", "constructor_declaration", "delegate_declaration", "operator_declaration",
               "conversion_operator_declaration"):
        tn = member.child_by_field_name("returns" if t == "method_declaration" else "type")
        if tn is not None:
            yield tn
        yield from _param_type_nodes(member.child_by_field_name("parameters"))


class CSharpAnalyzer(Analyzer):
    """`class/interface/struct/enum/record`（public＝primary・他は children）→ `Module`。
    継承/実装（`: Base, IFoo`・全件 `via=extends`）・宣言型（`via=field_type`）・`new X(...)`
    （`via=call`）→ `INVOKES` 候補。"""

    name = "csharp"
    extensions = CSHARP_EXT
    doctype = "csharp"
    requires_file_context = True
    resolves_parent_namespaces = True
    version = 4

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        parsed = _ts.parse("c_sharp", text)
        scan = _scan(parsed)
        lines = text.split("\n")
        dropped = [Dropped("nested_type", d.line, _line_text(lines, d.line)) for d in scan.nested]
        top = scan.top
        if not top:
            return DefResult(dropped=dropped + _ts.syntax_errors(parsed))

        # `partial class` は解決せず `Dropped` で申告するだけ（ファイルの主体定義は通常どおり作る）。
        dropped += [Dropped("cs_partial", d.line, _line_text(lines, d.line)) for d in top if d.partial]

        primary_idx = _primary_index(top)
        primary_d = top[primary_idx]
        primary = DefItem(label="Module", name=primary_d.name, cid_key=primary_d.key)
        # 非 primary 型にも、その型を囲む namespace 込みの `cid_key` を設定する（別 namespace の同名型と区別し、完全修飾参照を一意に解決するため）。
        # 同じキーの型（`partial` の同名）は 1 つのノード（primary・既出の child と同じ cid を二重に作らない）。
        children = []
        seen_keys = {primary_d.key}
        for i, d in enumerate(top):
            if i == primary_idx or d.key in seen_keys:
                continue
            seen_keys.add(d.key)
            children.append(DefItem(label="Module", name=d.name, cid_key=d.key, line=d.line))

        return DefResult(primary=primary, children=children, dropped=dropped + _ts.syntax_errors(parsed))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        parsed = _ts.parse("c_sharp", text)
        scan = _scan(parsed)
        top = scan.top
        refs: list = []

        for d in top:
            _base_refs(parsed, d, refs)

        creations = _ts.captures(parsed, "(object_creation_expression type: (_) @t)").get("t", [])
        for n in sorted(creations, key=lambda n: n.start_byte):   # 捕捉の列は出現順とは限らない
            name, absolute = _type_name(parsed, n)
            if name is not None and name.rsplit(".", 1)[-1] not in _type_params(parsed, n):
                _emit(refs, name, absolute, _ts.start_line(n.parent), "call")
            for arg in _type_args(n):
                _emit_declared(parsed, refs, arg, _type_params(parsed, n))

        for pattern in ("(cast_expression type: (_) @t)", "(typeof_expression type: (_) @t)", "(as_expression right: (_) @t)"):
            for n in sorted(_ts.captures(parsed, pattern).get("t", []), key=lambda n: n.start_byte):
                _emit_declared(parsed, refs, n, _type_params(parsed, n))

        for d in top:
            body = d.node.child_by_field_name("body")
            for member in [d.node, *(_members(body) if body is not None else ())]:
                tparams = None
                for tn in _member_type_nodes(parsed, member):
                    if tparams is None:
                        tparams = _type_params(parsed, member)
                    _emit_declared(parsed, refs, tn, tparams)

        # 始点＝その行を本体に含む型（主体以外の型だけ。主体・型の外は省略＝ファイルの主体）。キーは `collect_defs` の children の `cid_key`。
        # 同じキーの型（`partial` の同名）は 1 つの定義。同じ行に 2 つ以上の型がかかる行は決められないので主体にし、
        # `Dropped("ambiguous_source_symbol")` を 1 行 1 件残す。
        primary_idx = _primary_index(top)
        spans = [(d.key, d.start, d.end) for d in top]
        primary_key = spans[primary_idx][0] if spans else None
        ambiguous_dropped: list = []
        ambiguous_lines: set = set()
        for ref in refs:
            hits = [key for key, start, end in spans if start <= ref.line <= end]
            if len(set(hits)) > 1:
                if ref.line not in ambiguous_lines:
                    ambiguous_lines.add(ref.line)
                    ambiguous_dropped.append(Dropped("ambiguous_source_symbol", ref.line, ", ".join(hits)))
            elif hits and hits[0] != primary_key:
                ref.source_symbol_id = (rel_path, hits[0])

        # 参照ごとの namespace は、その行を含む型（本体の範囲）の namespace。`using X;` は参照にせず `file_context` として渡す。
        namespaces = [(d.start, d.end, d.ns) for d in top]
        package = top[primary_idx].ns if top else None
        return RefResult(refs=refs, dropped=ambiguous_dropped,
                         file_context=FileContext(package=package, namespaces=namespaces, imports=scan.imports))

    def global_imports(self, text: str, rel_path: str) -> list:
        return [i for i in _scan(_ts.parse("c_sharp", text)).imports if i.is_global]
