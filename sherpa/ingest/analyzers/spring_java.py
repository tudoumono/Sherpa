"""FW プラグイン `spring:java`（Java ソースの Spring／TERASOLUNA の注釈）。本体の `JavaAnalyzer`（FW 非依存）の結果へ、注釈から読める FW の意味だけを足す。

足すもの（本体の出力は消さない・docs/21 §3a）:
- 注入: `@Autowired`／`@Inject`／`@Resource` の付いたフィールドの宣言型を `INVOKES(via=inject)`。本体が `via=field_type` で返した同じ型・同じ行の参照を `inject` で重ねる
  （辺の代表の `via` は `VIA_PRIORITY` の順で `inject` が `field_type` より先）。JDK 頻出型などの除外は本体の判断に従う（本体が返さなかった型は足さない）。
- URL キー定義: クラスレベル `@RequestMapping` の prefix（配列は直積）とメソッドレベルのマッピング注釈（`@RequestMapping`／`@GetMapping`／`@PostMapping`／`@PutMapping`／`@DeleteMapping`／`@PatchMapping`）を連結し、
  主体の children（`Config`・`cid_key="key:url:"+パス`・`key_kind="url"`）で返す。
- 設定キー参照（`ACCESSES`→`Config`・`via=config_key`）: `@Value("${k}")`（`${k:default}` の default は捨てる・複数あれば全部）は `key_kind="property"`、
  `getBean("k")`・`@Qualifier`・`@Named`・`@Resource(name=...)` は `"bean"`。キーは識別子形のみ。同じ `(key, key_kind)` は最初の出現行にまとめる。
  SpEL・`@ConfigurationProperties` の prefix・`getBean` の第 1 引数が非リテラルは `Dropped`（`config_spel`／`config_prefix`／`config_nonliteral`）で申告する。
注釈は Tree-sitter のクエリで拾う（`_ts.captures`）。名前は解決しない（参照の候補を返し、解決は共通層）。設計: docs/21-拡張の契約.md §3a・提案書 2026-10-04 段階 2。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from . import _ts
from ._base import (DefItem, DefResult, Dropped, FwPlugin, PluginAmbiguity, PluginDefs, PluginRefs, RefCandidate,
                    RefResult)
from .java import (_COMMENT_KINDS, _CONFIG_KEY, _TYPE_NODE_KINDS, _annotations, _ann_args, _ann_name, _file_context, _members,
                   _pair, _string_value, _type_decls, _type_name, _type_ref_name, assign_source_symbols)

# DI アノテーション（フィールド・セッターの宣言型を `via=inject` にする）。コンストラクタは `Resource` を数えない。
_DI_ANNOTATIONS = frozenset({"Autowired", "Inject", "Resource"})
_DI_CTOR_ANNOTATIONS = frozenset({"Autowired", "Inject"})
# コンポーネント注釈（bean 名の既定・注釈の無い単一コンストラクタの注入の対象）。
_STEREOTYPES = frozenset({"Component", "Service", "Repository", "Controller", "RestController", "Configuration",
                          "ControllerAdvice", "RestControllerAdvice", "Named"})
# 注入先の実装をたどる型の数の上限（循環・巨大な階層の保険）。
_CLOSURE_MAX = 500

# `${key}`／`${key:default}`（デフォルト部分は捨てる）。
_VALUE_PLACEHOLDER = re.compile(r"\$\{(?P<key>[^}:]*)(?::[^}]*)?\}")
# SpEL（`#{...}`）の目印（`Dropped("config_spel")` で申告する）。
_SPEL_MARKER = "#{"
_BEAN_CALL = "getBean"

# URL キー定義側（Spring MVC のマッピング注釈）。
_MAPPING_ANNOTATION_NAMES = frozenset({
    "RequestMapping", "GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping",
})

_ANNOTATION_QUERY = "[(annotation) (marker_annotation)] @ann"
_CALL_QUERY = "(method_invocation) @call"


# ---- 注入（宣言型の `field_type` → `inject`・注入先の実装） ----

@dataclass(frozen=True)
class _BeanFacts:
    """型 1 つの Spring の事実（1 パス目に集め、2 パス目の注入先の決定で引く）。

    `stereotype`＝コンポーネント注釈（`@Component` 系・`@Named`）が付いている・`bean_name`＝明示名（無ければ `None`）・`name_nonliteral`＝明示名の指定があるが文字列リテラルでない・
    `qualifier`＝型に付いた `@Qualifier` の値・`primary`＝`@Primary`・`abstract`＝インターフェース／抽象クラス・
    `supers`＝`extends`／`implements` の `(単純名, 書かれた型名, 行, 型引数の列)`（型引数は `("c", 単純名)`・自分の型パラメータ `("p", 位置)`・照合できない形 `None`・列が空＝型引数なし）・`ntp`＝自分の型パラメータの数。
    """

    stereotype: bool = False
    name_nonliteral: bool = False
    supers: tuple = ()
    ntp: int = 0
    bean_name: str | None = None
    qualifier: str | None = None
    primary: bool = False
    abstract: bool = False


def _decapitalize(name: str) -> str:
    """`java.beans.Introspector.decapitalize`（先頭 2 文字が大文字ならそのまま）。bean 名の既定（クラス名の先頭小文字）。"""
    if len(name) > 1 and name[0].isupper() and name[1].isupper():
        return name
    return name[:1].lower() + name[1:]


def _type_facts(parsed, node) -> _BeanFacts:
    anns = _annotations(node)
    names = [_ann_name(a, parsed) for a in anns]
    stereo = next((a for a in anns if _ann_name(a, parsed) in _STEREOTYPES), None)
    qual = next((a for a in anns if _ann_name(a, parsed) == "Qualifier"), None)
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    abstract = node.type in ("interface_declaration", "annotation_type_declaration") \
        or (mods is not None and any(m.type == "abstract" for m in mods.children))
    nonliteral = False
    if stereo is not None:
        node_v = _value_node(parsed, stereo)
        nonliteral = node_v is not None and _string_value(parsed, node_v) is None
    return _BeanFacts(
        stereotype=stereo is not None, name_nonliteral=nonliteral, supers=_supers(parsed, node), ntp=len(_type_param_list(parsed, node)),
        bean_name=_config_key_value(parsed, stereo, ("value",), bare=True, sole=True) if stereo is not None else None,
        qualifier=_config_key_value(parsed, qual, ("value",), bare=True, sole=True) if qual is not None else None,
        primary="Primary" in names, abstract=abstract)


def _value_node(parsed, ann):
    """注釈の `value`（裸の第 1 引数または `value = ...`）のノード。無ければ `None`。"""
    elems = _ann_args(ann)
    for e in elems:
        pr = _pair(parsed, e)
        if pr is not None:
            if pr[0] == "value":
                return pr[1]
        elif e is elems[0]:
            return e
    return None


def _type_param_list(parsed, node) -> list:
    """型宣言の型パラメータ名（宣言順）。"""
    tp = next((c for c in node.named_children if c.type == "type_parameters"), None)
    out: list = []
    for c in (tp.named_children if tp is not None else []):
        first = next((x for x in c.named_children if x.type == "type_identifier"), None)
        if first is not None:
            out.append(parsed.text(first))
    return out


def _type_arg_names(parsed, generic, tparams: list) -> tuple:
    """`generic_type` の型引数の列。単純名 `("c", 名前)`・`tparams` の型パラメータ `("p", 位置)`・ワイルドカード／入れ子の型引数／配列は `None`（照合できない）。型引数が無ければ空。"""
    targs = next((c for c in generic.named_children if c.type == "type_arguments"), None) if generic.type == "generic_type" else None
    out: list = []
    for a in (targs.named_children if targs is not None else []):
        if a.type in ("type_identifier", "scoped_type_identifier"):
            n = _type_name(parsed, a).rsplit(".", 1)[-1]
            out.append(("p", tparams.index(n)) if n in tparams else ("c", n))
        else:
            out.append(None)
    return tuple(out)


def _supers(parsed, node) -> tuple:
    """型宣言の `extends`／`implements` の `(単純名, 書かれた型名, 行, 型引数の列)`。"""
    tparams = _type_param_list(parsed, node)
    out: list = []
    for c in node.named_children:
        if c.type in ("superclass", "super_interfaces", "extends_interfaces"):
            for x in [y for tl in c.named_children for y in (tl.named_children if tl.type == "type_list" else [tl])]:
                if x.type in _TYPE_NODE_KINDS and (n := _type_name(parsed, x)):
                    out.append((n.rsplit(".", 1)[-1], n, _ts.start_line(x), _type_arg_names(parsed, x, tparams)))
    return tuple(out)


def _declared_type_node(type_node):
    """宣言型ノードから、配列・注釈を外した本体の型ノード（本体が `field_type` を出す位置）。"""
    while type_node is not None and type_node.type in ("array_type", "annotated_type"):
        if type_node.type == "array_type":
            type_node = type_node.child_by_field_name("element")
        else:
            type_node = next((c for c in type_node.named_children if c.type in _TYPE_NODE_KINDS), None)
    return type_node


def _hint(parsed, anns: list, *, resource_name: bool) -> list:
    """注入点に付いた名前の指定 `[(注釈名, 値)]`（`@Qualifier`／`@Named`／`resource_name` のとき `@Resource(name=)`・全部）。値が文字列リテラルでなければ `None`。指定が無ければ空。"""
    out: list = []
    for a in anns:
        n = _ann_name(a, parsed)
        if n in ("Qualifier", "Named"):
            out.append((n, _config_key_value(parsed, a, ("value",), bare=True, sole=True)))
        elif n == "Resource" and resource_name:
            for e in _ann_args(a):
                pr = _pair(parsed, e)
                if pr is not None and pr[0] == "name":      # name 属性があって非リテラルなら「指定あり・値不明」
                    out.append((n, _string_value(parsed, pr[1])))
    return out


def _injection_points(parsed, t) -> list:
    """型 `t` の注入点 `(宣言型のノード, 名前の指定の一覧)` の一覧（出現順）。

    - フィールド: `@Autowired`／`@Inject`／`@Resource`。
    - メソッド（セッター）: 同じ注釈の付いたメソッドの全ての引数。
    - コンストラクタ: `@Autowired`／`@Inject` の付いたもの。無ければ、コンポーネント注釈の付いたクラスで、コンストラクタが 1 つだけ（引数あり）のとき（Spring 4.3 以降）。
      コンストラクタが複数あって注釈が無い型は対象外。
    """
    points: list = []
    members = _members(t)
    for m in members:
        anns = _annotations(m)
        di = {_ann_name(a, parsed) for a in anns} & _DI_ANNOTATIONS
        if m.type in ("field_declaration", "constant_declaration"):
            if di and (tn := _declared_type_node(m.child_by_field_name("type"))) is not None:
                points.append((tn, _hint(parsed, anns, resource_name=True)))
        elif m.type == "method_declaration" and di:
            params = [p for p in _formal_params(m)]
            for p in params:
                tn = _declared_type_node(_param_type(p))
                if tn is not None:
                    points.append((tn, _hint(parsed, _annotations(p), resource_name=False)
                                   or _hint(parsed, anns, resource_name=len(params) == 1)))
    ctors = [m for m in members if m.type == "constructor_declaration"]
    chosen = [c for c in ctors if {_ann_name(a, parsed) for a in _annotations(c)} & _DI_CTOR_ANNOTATIONS]
    if not chosen and len(ctors) == 1 and t.type == "class_declaration" and _type_facts(parsed, t).stereotype:
        chosen = ctors
    for c in chosen:
        for p in _formal_params(c):
            tn = _declared_type_node(_param_type(p))
            if tn is not None:
                points.append((tn, _hint(parsed, _annotations(p), resource_name=False)))
    return points


def _formal_params(node) -> list:
    params = node.child_by_field_name("parameters")
    return [p for p in (params.named_children if params is not None else []) if p.type in ("formal_parameter", "spread_parameter")]


def _param_type(p):
    """引数の型ノード（可変長 `T... xs` は要素の型）。"""
    if p.type == "spread_parameter":
        return next((c for c in p.named_children if c.type in _TYPE_NODE_KINDS), None)
    return p.child_by_field_name("type")


# ---- 設定キー参照 ----

def _config_key_value(parsed, ann, attrs: tuple, bare: bool, sole: bool) -> str | None:
    """注釈の文字列引数（`bare`＝引数が文字列リテラル 1 個・`attrs`＝`name = "..."` の属性名）を返す。`sole` なら引数が 1 個のときだけ。"""
    elems = _ann_args(ann)
    if sole and len(elems) != 1:
        return None
    for e in elems:
        pr = _pair(parsed, e)
        if pr is not None:
            if pr[0] in attrs:
                return _string_value(parsed, pr[1])
        elif bare and e is elems[0]:
            return _string_value(parsed, e)
    return None


def _collect_config_key_refs(parsed) -> tuple:
    """`@Value`/`getBean`/`@Qualifier`/`@Named`/`@Resource(name=...)` から設定キー参照候補を返す（`(refs, dropped)`）。

    - 1 つの `@Value` 文字列に複数の `${key}` があれば全部抽出する。SpEL は `Dropped("config_spel")`。
    - `getBean` の第 1 引数が文字列リテラルでなければ `Dropped("config_nonliteral")`（スニペットは引数全体）。キーが識別子形でないものは黙って除外する。
    - `key_kind`: `@Value` は `"property"`、`getBean`/`@Qualifier`/`@Named`/`@Resource(name=...)` は `"bean"`。
    - 同じ `(key, key_kind)` は最初の出現行のみ 1 回にまとめる。
    - `@ConfigurationProperties` の prefix は接続せず `dropped` に申告する（キーにできない指定も同じ）。
    """
    hits: list = []          # (開始位置, key, key_kind, 行)
    spel: list = []
    nonliteral: list = []
    prefixes: list = []

    for n in _ts.captures(parsed, _ANNOTATION_QUERY).get("ann", []):
        name = _ann_name(n, parsed)
        line, pos = _ts.start_line(n), n.start_byte
        if name == "Value":
            content = _config_key_value(parsed, n, ("value",), bare=True, sole=True)
            if content is None:
                continue
            if _SPEL_MARKER in content:
                snippet = content[2:-1] if content.startswith("#{") and content.endswith("}") else content
                spel.append(Dropped("config_spel", line, snippet[:120]))
                continue
            for pm in _VALUE_PLACEHOLDER.finditer(content):
                key = pm.group("key")
                if _CONFIG_KEY.fullmatch(key):
                    hits.append((pos, key, "property", line))
        elif name in ("Qualifier", "Named"):
            key = _config_key_value(parsed, n, ("value",), bare=True, sole=True)
            if key is not None and _CONFIG_KEY.fullmatch(key):
                hits.append((pos, key, "bean", line))
        elif name == "Resource":
            key = _config_key_value(parsed, n, ("name",), bare=False, sole=False)
            if key is not None and _CONFIG_KEY.fullmatch(key):
                hits.append((pos, key, "bean", line))
        elif name == "ConfigurationProperties":
            prefix = _config_key_value(parsed, n, ("prefix", "value"), bare=True, sole=True)
            if prefix is not None and _CONFIG_KEY.fullmatch(prefix):
                prefixes.append(Dropped("config_prefix", line, prefix))
            elif _ann_args(n):                      # prefix の指定はあるがキーにできない（非リテラル・解釈できない形）＝黙って落とさない
                prefixes.append(Dropped("config_prefix", line, parsed.text(n.child_by_field_name("arguments"))[1:-1].strip()[:120]))

    for n in _ts.captures(parsed, _CALL_QUERY).get("call", []):
        name_node = n.child_by_field_name("name")
        if name_node is None or parsed.text(name_node) != _BEAN_CALL:
            continue
        args = n.child_by_field_name("arguments")
        elems = [c for c in args.named_children if c.type not in _COMMENT_KINDS] if args is not None else []
        if not elems:
            continue
        line, pos = _ts.start_line(name_node), n.start_byte
        key = _string_value(parsed, elems[0])
        if key is None:
            nonliteral.append(Dropped("config_nonliteral", line, parsed.text(args)[1:-1].strip()[:120]))
        elif _CONFIG_KEY.fullmatch(key):
            hits.append((pos, key, "bean", line))

    hits.sort(key=lambda h: h[0])
    refs: list = []
    seen: set = set()
    for _pos, key, key_kind, line in hits:
        if (key, key_kind) in seen:
            continue
        seen.add((key, key_kind))
        refs.append(RefCandidate("ACCESSES", "Config", key, line, extra={"via": "config_key", "key_kind": key_kind}))
    return refs, spel + nonliteral + prefixes


# ---- URL キー定義 ----

def _literal_paths(parsed, node) -> list:
    if node is None:
        return []
    if node.type == "string_literal":
        v = _string_value(parsed, node)
        return [v] if v is not None else []
    if node.type == "element_value_array_initializer":
        return [v for c in node.named_children if (v := _string_value(parsed, c)) is not None]
    return []


def _mapping_paths(parsed, ann) -> list:
    """`@GetMapping`/`@RequestMapping` 等の引数から URL パスのリストを返す（`value=`/`path=` または裸の引数・単一文字列／配列に対応。属性なしは空リスト）。"""
    elems = _ann_args(ann)
    for e in elems:
        pr = _pair(parsed, e)
        if pr is not None and pr[0] in ("value", "path"):
            return _literal_paths(parsed, pr[1])
    if elems and _pair(parsed, elems[0]) is None:
        return _literal_paths(parsed, elems[0])
    return []


def _join_mapping_path(prefix: str, path: str) -> str:
    """クラスレベル prefix とメソッドレベル path を連結する（重複スラッシュを畳む）。"""
    if not path:
        return prefix
    if not prefix:
        return path if path.startswith("/") else f"/{path}"
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def _class_prefixes(parsed, type_node) -> list:
    """型に付いたクラスレベル `@RequestMapping` の prefix 一覧（配列は全件・単一は 1 件・パスを持つ最後の注釈が優先・無ければ `[""]`）。"""
    for ann in reversed(_annotations(type_node)):
        if _ann_name(ann, parsed) == "RequestMapping":
            paths = _mapping_paths(parsed, ann)
            if paths:
                return paths
    return [""]


def _url_config_children(parsed, top: list) -> list:
    """各トップレベル型直下のメソッドのマッピング注釈から URL キー `Config` children を返す（`cid_key="key:url:"+URL`・`key_kind="url"`）。クラスレベル prefix が複数なら各 prefix とメソッドレベルパスの直積を返す。"""
    children: list = []
    for t in top:
        prefixes = _class_prefixes(parsed, t)
        for m in _members(t):
            if m.type != "method_declaration":
                continue
            for ann in _annotations(m):
                if _ann_name(ann, parsed) not in _MAPPING_ANNOTATION_NAMES:
                    continue
                for class_prefix in prefixes:
                    for path in _mapping_paths(parsed, ann) or [""]:
                        full = _join_mapping_path(class_prefix, path)
                        if full:
                            children.append(DefItem(label="Config", name=full,
                                                    line=_ts.start_line(ann), cid_key=f"key:url:{full}",
                                                    extra={"key_kind": "url"}))
    return children


class SpringJavaPlugin(FwPlugin):
    """Java ソースの Spring／TERASOLUNA の注釈（注入・注入先の実装・URL キー・設定キー）。

    注入先の実装を決めるための bean の事実（`_BeanFacts`）は、1 パス目（`collect_defs`）で型ごとに取り込み 1 回ごとの作業領域（`ctx`・`uses_build_context`）へ集め、
    2 パス目（`extract_refs`）で引く。共有のインスタンスには資料フォルダの事実を持たない。
    """

    name = "spring:java"
    languages = frozenset({"java"})
    version = 3
    order = 100
    uses_type_relations = True
    uses_build_context = True

    @staticmethod
    def _record(ctx: dict, parsed, top: list, rel_path: str) -> None:
        ctx[("fc", rel_path)] = _file_context(parsed)
        ctx[rel_path] = {parsed.text(n.child_by_field_name("name")): _type_facts(parsed, n) for n in top}

    @staticmethod
    def _fact(ctx: dict, c) -> _BeanFacts | None:
        return ctx.get(c.path, {}).get(c.name)

    def _bean_names(self, ctx, c) -> set:
        """候補の型を指せる名前: bean 名（明示名・無ければクラス名の先頭小文字・コンポーネント注釈のある型だけ）と型に付いた `@Qualifier` の値。"""
        f = self._fact(ctx, c)
        if f is None:
            return set()
        names = {f.qualifier} if f.qualifier else set()
        if f.stereotype and not f.name_nonliteral:      # 明示名が非リテラルの bean は名前での照合の対象外
            names.add(f.bean_name or _decapitalize(c.name))
        return names

    def collect_defs(self, text: str, rel_path: str, base: DefResult, ctx: dict | None = None) -> PluginDefs:
        parsed = _ts.parse("java", text)
        top, _nested = _type_decls(parsed)
        self._record(ctx if ctx is not None else {}, parsed, top, rel_path)
        return PluginDefs(children=_url_config_children(parsed, top) if top else [])

    def _bind_through(self, ctx, types, c, parent, pbind: tuple):
        """親 `parent`（型引数 `pbind`）を継承・実装する `c` の宣言から、`c` の型パラメータへの型引数を求める。

        戻りは `(c の型引数の列, 照合できない形があったか)`。型引数が食い違えば `None`（その実装は候補にしない）。
        同じ単純名の親が複数あるときは、書かれた型名を `types.subtypes` で解決して `parent` と同じ型の宣言を選ぶ（解決できなければ単純名のまま・複数残れば照合不能）。
        """
        f = self._fact(ctx, c)
        if f is None:
            return (), True
        entries = [x for x in f.supers if x[0] == parent.name]
        if len(entries) > 1:
            kept = []
            for x in entries:
                r = types.subtypes(x[1], c.path, ctx.get(("fc", c.path)), x[2])
                if r.status == "resolved" and r.targets and all(t.qualified != parent.qualified for t in r.targets):
                    continue
                kept.append(x)
            entries = kept
        bind: list = [None] * f.ntp
        if len(entries) != 1 or not entries[0][3] or len(entries[0][3]) != len(pbind):
            return tuple(bind), True
        unknown = False
        for e, b in zip(entries[0][3], pbind):
            if e is None:
                unknown = True
            elif e[0] == "c":
                if b is None:
                    unknown = True
                elif e[1] != b:
                    return None
            else:
                if e[1] < len(bind):
                    bind[e[1]] = b
                unknown = unknown or b is None
        return tuple(bind), unknown

    def _concrete_implementations(self, ctx, types, lookup, dargs) -> tuple:
        """宣言した型の実装（継承・実装の関係をたどった具象の型）の候補 `[(候補, 型引数を照合できなかったか)]`。間にインターフェース／抽象クラスがあってもたどる。途中の宣言が曖昧なら `exact=False`。

        宣言した型に型引数があるとき（`dargs`）は、経路の各段で型パラメータへの型引数の置き換えを伝え、最後に注入点の宣言と照合する（食い違う実装は除く）。
        戻りは `(候補, 探索を上限で打ち切ったか)`。
        """
        found: dict = {}
        seen: set = set()
        root = lookup.targets[0]
        pb0 = None if dargs is None else tuple(d[1] if d is not None and d[0] == "c" else None for d in dargs)
        queue = [(c, root, pb0, False) for c in lookup.subtypes]
        while queue and len(seen) < _CLOSURE_MAX:
            c, parent, pbind, unk = queue.pop(0)
            if (c.path, c.name) in seen:
                continue
            seen.add((c.path, c.name))
            bind = None
            if pbind is not None:
                res = self._bind_through(ctx, types, c, parent, pbind)
                if res is None:
                    continue
                bind, u = res
                unk = unk or u
            f = self._fact(ctx, c)
            if f is not None and f.stereotype and not f.abstract:      # bean の候補はコンポーネント注釈のある具象クラスだけ（XML・`@Bean` の bean は対象外）
                found[(c.path, c.name)] = (c, unk)
            sub = types.subtypes(c.qualified, c.path)
            if sub.status == "resolved":
                queue.extend((replace(x, exact=x.exact and c.exact), c, bind, unk) for x in sub.subtypes)
        return [found[k] for k in sorted(found)], bool(queue)

    def _decide(self, ctx, types, lookup, hint, dargs=None) -> tuple | None:
        """注入先の実装を決める。`("resolved", 候補, 規則)`・`("ambiguous", 候補の列, 理由)`・対象外は `None`。

        決まる根拠は次の順（任意に選ばない）: ①名前の指定（`@Qualifier`／`@Named`／`@Resource(name=)`）が候補の bean 名・型の `@Qualifier` に 1 つだけ一致
        ②`@Primary` が 1 つだけ ③実装が 1 つだけ。どれでも決まらなければ曖昧。宣言した型が具象クラスのとき・実装が無いときは対象外（宣言した型への辺だけ）。
        """
        if lookup.status != "resolved" or not lookup.targets:
            return None
        tfacts = [self._fact(ctx, t) for t in lookup.targets]
        if any(f is None or not f.abstract for f in tfacts):
            return None
        found, truncated = self._concrete_implementations(ctx, types, lookup, dargs)
        cands = [c for c, _u in found]
        if truncated:
            return "ambiguous", cands, "search_limit"
        if not cands:
            return None
        if any(u for _c, u in found):       # 宣言した型の型引数と照合できない実装がある
            return "ambiguous", cands, "generic_unmatched"
        if hint:
            values = {v for _n, v in hint}
            if None in values:
                return "ambiguous", cands, "qualifier_not_literal"
            if len(values) > 1:
                return "ambiguous", cands, "qualifier_conflict"
            (value,) = values
            hit = [c for c in cands if value in self._bean_names(ctx, c)]
            if len(hit) != 1:
                return "ambiguous", hit or cands, "qualifier_multiple" if hit else "qualifier_unmatched"
            win, rule = hit[0], "di_qualifier"
        else:
            primary = [c for c in cands if (f := self._fact(ctx, c)) is not None and f.primary]
            if len(primary) > 1:
                return "ambiguous", primary, "primary_multiple"
            if primary:
                win, rule = primary[0], "di_primary"
            elif len(cands) == 1:
                win, rule = cands[0], "di_single_impl"
            else:
                return "ambiguous", cands, "no_evidence"
        if not win.exact:
            return "ambiguous", cands, "declaration_ambiguous"
        return "resolved", win, rule

    def extract_refs(self, text: str, rel_path: str, base_defs: DefResult, base_refs: RefResult, types,
                     ctx: dict | None = None) -> PluginRefs:
        ctx = ctx if ctx is not None else {}
        parsed = _ts.parse("java", text)
        top, _nested = _type_decls(parsed)
        self._record(ctx, parsed, top, rel_path)
        base = [r for r in base_refs.refs if (r.extra or {}).get("via") == "field_type" and (r.extra or {}).get("type_ref")]
        refs: list = []
        ambiguous: list = []
        tparams = {id(t): _type_param_list(parsed, t) for t in top}
        for t in top:
            for node, hint in _injection_points(parsed, t):
                token = _type_name(parsed, node)
                if not token:
                    continue
                name, line = _type_ref_name(token)[0], _ts.start_line(node)
                for r in (r for r in base if r.name == name and r.line == line):   # 本体が返した同じ型・同じ行の参照を `inject` で重ねる
                    refs.append(RefCandidate(r.edge_type, r.kind, r.name, r.line, extra={**r.extra, "via": "inject"},
                                             source_symbol_id=r.source_symbol_id))
                    dargs = _type_arg_names(parsed, node, tparams[id(t)]) if node.type == "generic_type" else None
                    decided = self._decide(ctx, types, types.subtypes(name, rel_path, base_refs.file_context, line), hint, dargs)
                    if decided is None:
                        continue
                    if decided[0] == "ambiguous":
                        ambiguous.append(PluginAmbiguity("Module", name, line, list(decided[1]), via="inject",
                                                         source_symbol_id=r.source_symbol_id, why=decided[2]))
                        continue
                    win = decided[1]
                    extra = {"via": "inject", "qualified": True, "resolution_rule": decided[2]}
                    if "." not in win.qualified:      # 名前空間の無い型: 完全修飾名の解決（完全一致）で引く
                        extra["absolute"] = True
                    refs.append(RefCandidate("INVOKES", "Module", win.qualified, line, extra=extra,
                                             source_symbol_id=r.source_symbol_id))
        config_refs, dropped = _collect_config_key_refs(parsed)
        # 設定キーの参照だけ、本体と同じ規則で始点を付ける（注入の参照は本体の参照から始点を引き継ぐ）。
        flagged = {d.line for d in base_refs.dropped if d.reason == "ambiguous_source_symbol"}
        dropped.extend(d for d in assign_source_symbols(parsed, top, config_refs, rel_path) if d.line not in flagged)
        refs.extend(config_refs)
        return PluginRefs(refs=refs, dropped=dropped, ambiguous=ambiguous)


FW_PLUGINS = [SpringJavaPlugin()]
