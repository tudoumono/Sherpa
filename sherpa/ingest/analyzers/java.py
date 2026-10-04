"""Java アナライザ。`public class/interface/enum/record`（ファイル主体）を主体定義（`Module`）とし、同一ファイル内の非 public 型を子定義（`CONTAINS`）として返す。

読み取りは Tree-sitter（`tree-sitter-java`・共通部品 `_ts`）。構文エラーは `Dropped("syntax_error")` で申告し、エラーの外の定義・参照は読み続ける（`collect_defs` が 1 回だけ返す）。

参照（`INVOKES`・細分は `extra["via"]`）:
- `new X(...)`・`X.method(...)`（大文字始まりの修飾子＝クラス名とみなす）→ `call`、`extends`/`implements`。型名は縮めない: `a.b.X` のような package 付きは完全修飾名（`qualified`）、全ての型参照に `type_ref`（共通層が `file_context` の package・import で解決する）を付ける。入れ子の型（`Outer.Inner`）は外側の型 `Outer` へ寄せる。
- フィールド/コンストラクタ引数/メソッド引数の宣言型 → `field_type`（アノテーションに依らず常に抽出）。トップレベル型の直下のメンバーに限る（メソッド本体のローカル変数・入れ子の型のメンバーは対象外）。メソッドの戻り値・`throws`・レコードの成分・ジェネリクスの型引数（入れ子も全段・ワイルドカードの境界も）も `field_type`。型パラメータ自身（`T`）・JDK 頻出型・プリミティブは対象外。
- `import` はエッジにせず、`RefResult.file_context`（`package`・`imports`）として共通層へ渡す。
- 参照の始点（`source_symbol_id`）は、その行を本体に含む型。主体以外の型（同一ファイルの非 public 型）は `(rel_path, 型名)`、主体の型・型の外は省略（ファイルの主体）。同じ行に 2 つ以上の型がかかる行は決められないので主体にし、`Dropped("ambiguous_source_symbol")` を 1 行 1 件残す。

設定キー参照（`ACCESSES`→`Config`・`via=config_key`・`key_kind="property"`）: `getProperty("k")`・`getString("k")`（第 1 引数が文字列リテラル）。キーは識別子形のみ。同じキーは最初の出現行にまとめる。第 1 引数が非リテラルの呼び出しは `Dropped("config_nonliteral")` で申告する。
FW 固有の注釈（Spring の DI・`@Value`・`@Qualifier`・URL マッピング・`getBean`）は FW プラグイン `spring:java`（`spring_java.py`）が足す（docs/21 §3a）。この本体は FW 非依存。

大文字小文字は区別する（`normalize_code_name()` は使わない）。標準で解釈できない構文（入れ子の型）は `dropped`（`nested_type`）に記録する。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import re

from . import _ts
from ._base import Analyzer, DefItem, DefResult, Dropped, FileContext, ImportItem, RefCandidate, RefResult


# 拡張子は本ファイルに閉じて持つ（`static_analysis.py` は COBOL/JCL/コピーブック用）。
JAVA_EXT = frozenset({".java"})

_TYPE_DECL_KINDS = frozenset({"class_declaration", "interface_declaration", "enum_declaration",
                              "record_declaration", "annotation_type_declaration"})
# 型を表す節の種別（`superclass`/`super_interfaces`/`extends_interfaces` の子・型引数の要素）。
_TYPE_NODE_KINDS = frozenset({"type_identifier", "scoped_type_identifier", "generic_type", "array_type",
                              "annotated_type"})
_COMMENT_KINDS = frozenset({"line_comment", "block_comment"})

# `X.method(...)` の修飾子を「クラス名」とみなす形（小文字始まりの修飾子の連なりは package 名として型名に含める。誤りは共通層が未解決に倒す）。
_STATIC_QUALIFIER = re.compile(r"(?:[a-z_$][\w$]*\.)*[A-Z][\w$]*(?:\.[A-Z][\w$]*)*")

# JDK 標準ライブラリの頻出型（ノイズ削減用の小さな既知リスト）。候補にしない。
_JDK_COMMON_TYPES = frozenset({
    "Object", "String", "CharSequence", "Number", "Boolean", "Character", "Byte", "Short",
    "Integer", "Long", "Float", "Double", "Void", "Class", "Enum", "Comparable", "Iterable",
    "Iterator", "Runnable", "Thread", "Throwable", "Exception", "RuntimeException", "Error",
    "List", "ArrayList", "LinkedList", "Map", "HashMap", "LinkedHashMap", "TreeMap",
    "Set", "HashSet", "LinkedHashSet", "TreeSet", "Collection", "Optional", "Stream",
    "Comparator", "BigDecimal", "BigInteger", "Date", "UUID", "Pattern", "Matcher",
})

# 参照にしない JDK の package の接頭辞（import・完全修飾名で JDK の型と分かるもの）。
_JDK_PACKAGE_ROOTS = frozenset({"java", "javax", "jdk"})

# 設定キー参照: 識別子形のキーのみ（ドット/ハイフン区切りを許す）。
_CONFIG_KEY = re.compile(r"[A-Za-z0-9_.\-]+")
# 文字列リテラルの第 1 引数からキーを読むメソッド（呼び出しのレシーバは問わない）。
_PROPERTY_CALLS = ("getProperty", "getString")


# ---- 木の走査 ----

def _walk(node):
    """`node` 以下の名前つきノードを、出現順（先行順）で返す。"""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.named_children))


def _is_top_level(node) -> bool:
    """ファイル直下の型宣言か（構文エラーの領域 `ERROR` に包まれていてもファイル直下とみなす）。"""
    p = node.parent
    while p is not None and p.type == "ERROR":
        p = p.parent
    return p is not None and p.type == "program"


def _type_decls(parsed) -> tuple[list, list]:
    """`(トップレベルの型宣言, 入れ子・ローカルの型宣言)` を出現順で返す。"""
    top: list = []
    nested: list = []
    for n in _walk(parsed.root):
        if n.type in _TYPE_DECL_KINDS:
            (top if _is_top_level(n) else nested).append(n)
    return top, nested


def _name_line(node) -> int:
    name = node.child_by_field_name("name")
    return _ts.start_line(name if name is not None else node)


def _is_public(node) -> bool:
    return any(c.type == "modifiers" and any(m.type == "public" for m in c.children) for c in node.children)


def _primary_index(top: list) -> int:
    """トップレベル型のうちファイルの主体にする型の添字（最初の public 型・無ければ先頭）。`collect_defs` と `extract_refs` で共有する。"""
    return next((i for i, n in enumerate(top) if _is_public(n)), 0)


def _modifiers(node):
    return next((c for c in node.children if c.type == "modifiers"), None)


def _annotations(node) -> list:
    """宣言の修飾子に付いた注釈（`annotation`/`marker_annotation`）を出現順で返す。"""
    mods = _modifiers(node)
    return [c for c in mods.named_children if c.type in ("annotation", "marker_annotation")] if mods else []


def _ann_name(ann, parsed) -> str:
    """注釈名（`@a.b.Name` は最後の要素）。"""
    name = ann.child_by_field_name("name")
    return parsed.text(name).rsplit(".", 1)[-1].strip() if name is not None else ""


def _ann_args(ann) -> list:
    args = ann.child_by_field_name("arguments")
    return [c for c in args.named_children if c.type not in _COMMENT_KINDS] if args is not None else []


def _string_value(parsed, node) -> str | None:
    """通常の文字列リテラルの中身（エスケープは解かない）。リテラルでなければ・text block なら `None`。"""
    if node is None or node.type != "string_literal":
        return None
    t = parsed.text(node)
    return None if t.startswith('"""') else t[1:-1]


def _pair(parsed, elem) -> tuple[str, object] | None:
    """`name = value` 形の注釈引数を `(name, value ノード)` にする。"""
    if elem.type != "element_value_pair":
        return None
    key = elem.child_by_field_name("key")
    return (parsed.text(key), elem.child_by_field_name("value")) if key is not None else None


def _members(type_node) -> list:
    """型宣言の直下のメンバー（enum は定数の後の本体宣言も含む）。"""
    body = type_node.child_by_field_name("body")
    out: list = []
    for c in (body.named_children if body is not None else []):
        if c.type == "enum_body_declarations":
            out.extend(c.named_children)
        else:
            out.append(c)
    return out


# ---- 型名 ----

def _type_name(parsed, node) -> str | None:
    """型ノードから型名（ジェネリクス・配列の `[]`・注釈を除き、package 修飾は保持する）。型名でなければ `None`。"""
    t = node.type
    if t == "type_identifier":
        return parsed.text(node)
    if t == "scoped_type_identifier":
        parts = [_type_name(parsed, c) for c in node.named_children if c.type in _TYPE_NODE_KINDS]
        return ".".join(p for p in parts if p) or None
    if t == "generic_type":
        base = next((c for c in node.named_children if c.type in ("type_identifier", "scoped_type_identifier")), None)
        return _type_name(parsed, base) if base is not None else None
    if t == "array_type":
        elem = node.child_by_field_name("element")
        return _type_name(parsed, elem) if elem is not None else None
    if t == "annotated_type":
        inner = next((c for c in node.named_children if c.type in _TYPE_NODE_KINDS), None)
        return _type_name(parsed, inner) if inner is not None else None
    return None


def _type_ref_name(token: str) -> tuple:
    """型トークン（`a.b.Outer.Inner`・`Outer.Inner`・`Type`）から `(名前, 完全修飾か)` を返す。

    最初の大文字始まりの要素までが、その型を宣言するトップレベルの型（入れ子の型は外側の型へ寄せる）。
    それより前に要素があれば package 修飾付きの完全修飾名として保持する（縮めない）。大文字始まりが無ければ全体を完全修飾名とみなす。
    """
    segs = token.split(".")
    for i, seg in enumerate(segs):
        if seg[:1].isupper():
            return ".".join(segs[:i + 1]), i > 0
    return token, "." in token


def _type_ref(name_token: str, line: int, via: str) -> RefCandidate:
    """型名の参照 1 件。完全修飾名は `qualified`、いずれも `type_ref`（参照元ファイルの package・import に従って解決する）。"""
    name, qualified = _type_ref_name(name_token)
    extra = {"via": via, "type_ref": True}
    if qualified:
        extra["qualified"] = True
    return RefCandidate("INVOKES", "Module", name, line, extra=extra)


def _is_candidate_type(token: str, explicit: frozenset = frozenset()) -> bool:
    """宣言型として候補にする名前か（大文字始まりで JDK 頻出型でない）。`explicit`＝プロジェクトの型と分かる単純名（JDK 以外への単一 import・同じファイルで定義した型）は頻出名でも候補にする。"""
    simple = _type_ref_name(token)[0].rsplit(".", 1)[-1]
    return simple[:1].isupper() and (simple not in _JDK_COMMON_TYPES or simple in explicit)


def _type_param_names(parsed, node) -> set:
    """宣言の型パラメータ名（`<T extends X>` の `T`）。参照にしない。"""
    tp = next((c for c in node.named_children if c.type == "type_parameters"), None)
    out: set = set()
    for c in (tp.named_children if tp is not None else []):
        first = next((x for x in c.named_children if x.type == "type_identifier"), None)
        if first is not None:
            out.add(parsed.text(first))
    return out


def _emit_declared_type_refs(parsed, refs: list, type_node, via: str, tparams: frozenset = frozenset(),
                             explicit: frozenset = frozenset()) -> None:
    """宣言型と、そのジェネリクスの型引数（入れ子も全段・ワイルドカードの境界も）を `INVOKES(via=...)` 候補として積む。

    JDK 頻出型・プリミティブ・型パラメータ自身は候補にしない。型引数は常に `field_type`（`inject` への格上げは宣言型本体のみ）。
    """
    if type_node.type == "wildcard":
        for c in type_node.named_children:
            if c.type in _TYPE_NODE_KINDS:
                _emit_declared_type_refs(parsed, refs, c, "field_type", tparams, explicit)
        return
    while type_node.type in ("array_type", "annotated_type"):
        if type_node.type == "array_type":
            elem = type_node.child_by_field_name("element")
        else:
            elem = next((c for c in type_node.named_children if c.type in _TYPE_NODE_KINDS), None)
        if elem is None:
            return
        type_node = elem
    if type_node.type == "wildcard":
        _emit_declared_type_refs(parsed, refs, type_node, "field_type", tparams, explicit)
        return
    name = _type_name(parsed, type_node)
    if name and name not in tparams and _is_candidate_type(name, explicit):
        refs.append(_type_ref(name, _ts.start_line(type_node), via))
    if type_node.type != "generic_type":
        return
    targs = next((c for c in type_node.named_children if c.type == "type_arguments"), None)
    for arg in (targs.named_children if targs is not None else []):
        _emit_declared_type_refs(parsed, refs, arg, "field_type", tparams, explicit)


def _dotted(parsed, node) -> str | None:
    """式が `a.b.C` のような名前だけの連なりならそのドット区切りの文字列（深い連なりでも再帰しない）。"""
    parts: list = []
    while node.type == "field_access":
        fld = node.child_by_field_name("field")
        obj = node.child_by_field_name("object")
        if fld is None or fld.type != "identifier" or obj is None:
            return None
        parts.append(parsed.text(fld))
        node = obj
    if node.type != "identifier":
        return None
    parts.append(parsed.text(node))
    return ".".join(reversed(parts))


# ---- 宣言型の参照 ----

def _param_type_refs(parsed, refs: list, params, tparams: frozenset, explicit: frozenset) -> None:
    for prm in (params.named_children if params is not None else []):
        if prm.type == "formal_parameter":
            tn = prm.child_by_field_name("type")
        elif prm.type == "spread_parameter":
            tn = next((c for c in prm.named_children if c.type in _TYPE_NODE_KINDS), None)
        else:
            continue
        if tn is not None:
            _emit_declared_type_refs(parsed, refs, tn, "field_type", tparams, explicit)


def _declared_type_refs(parsed, top: list) -> list:
    """トップレベル型の直下の宣言に現れる型を参照候補として抽出する。

    フィールド・定数の型、メソッド・コンストラクタの引数型、メソッドの戻り値型、`throws` の例外型、レコードの成分の型。
    いずれも `via=field_type`（注釈には依らない。DI の注入は FW プラグインが別に足す）。
    """
    refs: list = []
    explicit = frozenset({i.name.rsplit(".", 1)[-1] for i in _file_context(parsed).imports
                          if i.kind == "single" and not i.static and i.name.split(".", 1)[0] not in _JDK_PACKAGE_ROOTS}
                         | {parsed.text(n.child_by_field_name("name")) for n in top})
    for t in top:
        outer_tp = _type_param_names(parsed, t)
        if t.type == "record_declaration":
            _param_type_refs(parsed, refs, t.child_by_field_name("parameters"), frozenset(outer_tp), explicit)
        for m in _members(t):
            if m.type in ("field_declaration", "constant_declaration"):
                tn = m.child_by_field_name("type")
                if tn is None:
                    continue
                _emit_declared_type_refs(parsed, refs, tn, "field_type", frozenset(outer_tp), explicit)
            elif m.type in ("method_declaration", "constructor_declaration"):
                tparams = frozenset(outer_tp | _type_param_names(parsed, m))
                ret = m.child_by_field_name("type") if m.type == "method_declaration" else None
                if ret is not None:
                    _emit_declared_type_refs(parsed, refs, ret, "field_type", tparams)
                _param_type_refs(parsed, refs, m.child_by_field_name("parameters"), tparams, explicit)
                for th in (c for c in m.named_children if c.type == "throws"):
                    for tn in th.named_children:
                        if tn.type in _TYPE_NODE_KINDS:
                            _emit_declared_type_refs(parsed, refs, tn, "field_type", tparams, explicit)
    return refs


def _header_type_refs(parsed, top: list) -> list:
    """トップレベル型の `extends`/`implements` 節の型を参照候補にする（型パラメータの境界 `<T extends X>` は対象外・ジェネリクスの型引数は辿らない）。"""
    refs: list = []
    for t in top:
        line = _name_line(t)
        for c in t.named_children:
            if c.type == "superclass":
                via, nodes = "extends", c.named_children
            elif c.type == "extends_interfaces":
                via, nodes = "extends", [x for tl in c.named_children for x in (tl.named_children if tl.type == "type_list" else [tl])]
            elif c.type == "super_interfaces":
                via, nodes = "implements", [x for tl in c.named_children for x in (tl.named_children if tl.type == "type_list" else [tl])]
            else:
                continue
            for n in nodes:
                name = _type_name(parsed, n) if n.type in _TYPE_NODE_KINDS else None
                if name:
                    refs.append(_type_ref(name, line, via))
    return refs


# ---- 設定キー参照 ----

def _collect_config_key_refs(parsed) -> tuple:
    """`getProperty`/`getString` の呼び出しから設定キー参照候補を返す（`(refs, dropped)`・`key_kind="property"`）。

    - 第 1 引数が文字列リテラルでない呼び出しは `Dropped("config_nonliteral")`（スニペットは引数全体）。キーが識別子形でないものは黙って除外する。
    - 同じキーは最初の出現行のみ 1 回にまとめる。
    """
    hits: list = []          # (開始位置, key, 行)
    nonliteral = {name: [] for name in _PROPERTY_CALLS}

    for n in _ts.captures(parsed, "(method_invocation) @call").get("call", []):
        name_node = n.child_by_field_name("name")
        mname = parsed.text(name_node) if name_node is not None else ""
        if mname not in nonliteral:
            continue
        args = n.child_by_field_name("arguments")
        elems = [c for c in args.named_children if c.type not in _COMMENT_KINDS] if args is not None else []
        if not elems:
            continue
        line, pos = _ts.start_line(name_node), n.start_byte
        key = _string_value(parsed, elems[0])
        if key is None:
            nonliteral[mname].append(Dropped("config_nonliteral", line, parsed.text(args)[1:-1].strip()[:120]))
        elif _CONFIG_KEY.fullmatch(key):
            hits.append((pos, key, line))

    hits.sort(key=lambda h: h[0])
    refs: list = []
    seen: set = set()
    for _pos, key, line in hits:
        if key in seen:
            continue
        seen.add(key)
        refs.append(RefCandidate("ACCESSES", "Config", key, line, extra={"via": "config_key", "key_kind": "property"}))

    return refs, [d for name in _PROPERTY_CALLS for d in nonliteral[name]]


# ---- package・import ----

def _scoped_name(parsed, node) -> str:
    """`package`・`import` の修飾名を、子の識別子をつないで作る（名前の途中のコメント・空白は含めない）。"""
    if node.type == "identifier":
        return parsed.text(node)
    return ".".join(_scoped_name(parsed, c) for c in node.named_children if c.type in ("identifier", "scoped_identifier"))


def _file_context(parsed) -> FileContext:
    package = None
    imports: list = []
    for c in parsed.root.named_children:
        if c.type == "package_declaration":
            name = next((x for x in c.named_children if x.type in ("identifier", "scoped_identifier")), None)
            if name is not None and package is None:
                package = _scoped_name(parsed, name)
        elif c.type == "import_declaration":
            name = next((x for x in c.named_children if x.type in ("identifier", "scoped_identifier")), None)
            if name is None:
                continue
            kinds = {x.type for x in c.children}
            imports.append(ImportItem(kind="wildcard" if "asterisk" in kinds else "single",
                                      name=_scoped_name(parsed, name), static="static" in kinds,
                                      line=_ts.start_line(name)))
    return FileContext(package=package, imports=imports)


def assign_source_symbols(parsed, top: list, refs: list, rel_path: str) -> list:
    """参照の始点（`source_symbol_id`）を、その行を本体に含む型にする（主体以外の型だけ。主体・型の外は省略＝ファイルの主体）。

    同じ行に 2 つ以上の型がかかる行は決められないので主体にし、`Dropped("ambiguous_source_symbol")` を 1 行 1 件返す。FW プラグインが足す参照も同じ規則で始点を付ける。
    """
    primary_idx = _primary_index(top)
    spans = [(i, parsed.text(n.child_by_field_name("name")), _ts.start_line(n), _ts.end_line(n))
             for i, n in enumerate(top)]
    dropped: list = []
    ambiguous_lines: set = set()
    for ref in refs:
        hits = [(i, name) for i, name, start, end in spans if start <= ref.line <= end]
        if len(hits) > 1:
            if ref.line not in ambiguous_lines:
                ambiguous_lines.add(ref.line)
                dropped.append(Dropped("ambiguous_source_symbol", ref.line, ", ".join(n for _i, n in hits)))
        elif hits and hits[0][0] != primary_idx:
            ref.source_symbol_id = (rel_path, hits[0][1])
    return dropped


class JavaAnalyzer(Analyzer):
    """`public class/interface/enum/record` → `Module`（primary）。同一ファイル内の非 public 型
    → `Module`（children・`CONTAINS`）。`new`/静的呼び出し/`extends`/`implements` → `INVOKES` 候補。
    """

    name = "java"
    extensions = JAVA_EXT
    doctype = "java"
    requires_file_context = True
    version = 5

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        parsed = _ts.parse("java", text)
        lines_raw = text.splitlines()
        top, nested = _type_decls(parsed)
        dropped = [Dropped("nested_type", _name_line(n),
                           (lines_raw[_name_line(n) - 1].strip()[:120] if _name_line(n) - 1 < len(lines_raw) else ""))
                   for n in nested]
        dropped.extend(_ts.syntax_errors(parsed))
        if not top:
            return DefResult(dropped=dropped)

        # primary＝最初の public 型。public が無ければ最初の型宣言を primary にする（ノードを黙って消さない）。
        primary_idx = _primary_index(top)
        primary_name = parsed.text(top[primary_idx].child_by_field_name("name"))
        package = _file_context(parsed).package
        qualified = f"{package}.{primary_name}" if package else primary_name

        extra = {}
        if package:
            extra["qualified_name"] = qualified  # ノードの属性（cid_key と同じ値）
        primary = DefItem(label="Module", name=primary_name, cid_key=qualified, extra=extra)

        # 非 public 型は cid を変えず、解決用の完全修飾名（`qualified`）だけを持つ（同じ package からの参照を完全修飾名で引くため）。
        children = []
        for i, n in enumerate(top):
            if i == primary_idx:
                continue
            nm = parsed.text(n.child_by_field_name("name"))
            children.append(DefItem(label="Module", name=nm, line=_name_line(n),
                                    qualified=f"{package}.{nm}" if package else nm))

        return DefResult(primary=primary, children=children, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        parsed = _ts.parse("java", text)
        top, _nested = _type_decls(parsed)
        refs: list = _header_type_refs(parsed, top)

        for n in _walk(parsed.root):
            if n.type == "object_creation_expression":
                tn = n.child_by_field_name("type")
                name = _type_name(parsed, tn) if tn is not None else None
                if name:
                    kw = next((c for c in n.children if c.type == "new"), n)
                    refs.append(_type_ref(name, _ts.start_line(kw), "call"))
            elif n.type == "method_invocation":
                obj = n.child_by_field_name("object")
                name = _dotted(parsed, obj) if obj is not None else None
                if name and _STATIC_QUALIFIER.fullmatch(name):
                    refs.append(_type_ref(name, _ts.start_line(obj), "call"))

        refs.extend(_declared_type_refs(parsed, top))
        config_refs, config_dropped = _collect_config_key_refs(parsed)
        refs.extend(config_refs)

        # JDK の型（完全修飾名が `java.`・`javax.`・`jdk.` の下、または単一 import がその下）は参照にしない。
        ctx = _file_context(parsed)
        jdk_imported = {i.name.rsplit(".", 1)[-1] for i in ctx.imports
                        if i.kind == "single" and not i.static and i.name.split(".", 1)[0] in _JDK_PACKAGE_ROOTS}
        refs = [r for r in refs
                if not r.extra.get("type_ref")
                or not (r.name.split(".", 1)[0] in _JDK_PACKAGE_ROOTS and "." in r.name
                        or "." not in r.name and r.name in jdk_imported)]

        ambiguous_dropped = assign_source_symbols(parsed, top, refs, rel_path)

        # nested type・syntax_error は collect_defs 側で記録済み（二重記録しない）。
        return RefResult(refs=refs, dropped=config_dropped + ambiguous_dropped, file_context=ctx)
