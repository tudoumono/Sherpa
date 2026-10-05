"""宣言的なルールファイル（TOML）から FW プラグインを作る。コードを書かずに足せるのは**単純な抽出**まで。

1 つのルールファイル `rules_<名前>.toml`（拡張アナライザと同じ置き場所 `sherpa/ingest/analyzers/`）＝ 1 つの FW プラグイン（登録名 `rules:<名前>`）。
判断（DI の注入先の決定・Mapper と SQL の対応づけなど）はルールに書けず、プラグインのコード（`FwPlugin` のサブクラス）で書く。
プラグインは名前を解決せず、参照の候補を返すだけ（解決は共通層・`docs/21-拡張の契約.md` §3a）。

ファイルの形:
    name = "acme"                 # 登録名 rules:acme・ファイル名は rules_acme.toml
    version = 1                   # プラグインの版（1 以上）
    order = 200                   # 小さいほど先（既定 100）
    languages = ["java", "xml_config"]
    config_kinds = ["spring_beans"]   # 省略可。非空なら languages は ["xml_config"] だけ（Java は設定の種別を持たない）

    [[rule]]
    id = "acme-api"               # ファイル内で一意
    version = 1                   # ルールの版
    language = "java"             # languages の 1 つ
    query = '(method_declaration (modifiers (annotation name: (identifier) @n (#eq? @n "AcmeApi") arguments: (annotation_argument_list (string_literal) @value))))'
    emit = "def"                  # def＝キー単位の Config 定義（key_kind 必須）／ref＝参照の候補
    key_kind = "url"

    [[rule]]
    id = "acme-service"
    language = "xml_config"
    element = "service"           # 要素のローカル名（namespace = "URI" で名前空間も指定可・root = "beans" でルート要素も指定可）
    attribute = "class"
    emit = "ref"
    edge_type = "INVOKES"         # EDGE_TYPES・kind は NODE_LABELS・via は KNOWN_VIA の中だけ
    kind = "Module"
    via = "bean_class"
    name_form = "qualified"       # plain（既定）／qualified／type_ref

Java のクエリは捕捉 `@value`（文字列リテラル・必須）と任意の `@at`（行を取るノード・既定は `@value`）を使う。
署名の材料（`signature_extra`）＝ルールファイルの内容のハッシュ・ルール ID と版の並び。ルールの 1 行を変えると、`version` を上げ忘れても署名が変わる。
"""
from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import _ts
from ._base import KNOWN_VIA, DefItem, DefResult, Dropped, FwPlugin, PluginDefs, PluginRefs, RefCandidate, RefResult
from .xml_config import CONFIG_KINDS, parse_xml

RULE_FILE_PREFIX = "rules"

# `Config` のキー定義・参照の `key_kind`（`world_graph` の `via=config_key` 索引の閉じた集合）。
KEY_KINDS = frozenset({"property", "bean", "action", "mapper", "url", "env"})
# ルールが出せる参照の `(edge_type, kind)` の組み合わせ（`via` ごと・docs/05 §2 の辺と既存のアナライザが出している組み合わせ）。ここに無い組み合わせは登録時に失敗させる。
# `mention`（資料の言及）・`mapper_namespace`（向きを反転して張る）・`config_value`・`include`（相対パスの厳密な解決が要る）は単純な抽出では出せないので含めない。
ALLOWED_REFS = {
    **{v: frozenset({("INVOKES", "Module")}) for v in (
        "call", "extends", "implements", "field_type", "inject", "import", "bean_class", "mapper_type", "action_class",
        "cics_xctl", "cics_link")},
    "copy": frozenset({("COPIES", "Copybook")}),
    "config_key": frozenset({("ACCESSES", "Config")}),
    **{v: frozenset({("ACCESSES", "Table")}) for v in ("exec_sql", "mapper_sql", "vba_sql")},
    **{v: frozenset({("INVOKES", "Batch")}) for v in ("exec_proc", "include_member")},
}
NAME_FORMS = frozenset({"plain", "qualified", "type_ref"})
RULE_LANGUAGES = frozenset({"java", "xml_config"})

_VALUE_CAPTURE = re.compile(r"\(string_literal\)\s*@value\b")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_FILE_KEYS = frozenset({"name", "version", "order", "languages", "config_kinds", "rule"})
_COMMON_KEYS = frozenset({"id", "version", "language", "emit", "key_kind", "edge_type", "kind", "via", "name_form"})
_LANG_KEYS = {"java": frozenset({"query"}), "xml_config": frozenset({"element", "namespace", "attribute", "root"})}


class RulesError(ValueError):
    """ルールファイルの読み込み・検証の失敗（登録側が `FwPluginError` にする）。"""


@dataclass(frozen=True)
class Rule:
    id: str
    version: int
    language: str
    emit: str
    key_kind: str | None = None
    edge_type: str | None = None
    kind: str | None = None
    via: str | None = None
    name_form: str = "plain"
    query: str | None = None
    element: str | None = None
    namespace: str | None = None
    attribute: str | None = None
    root: str | None = None


def _int(v, what: str, minimum: int | None = None) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or (minimum is not None and v < minimum):
        raise RulesError(f"{what} は{f' {minimum} 以上の' if minimum is not None else ''}整数にしてください（実際: {v!r}）")
    return v


def _str(v, what: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise RulesError(f"{what} は非空の文字列にしてください（実際: {v!r}）")
    return v


def _parse_rule(raw, idx: int, languages: frozenset, node_labels, edge_types) -> Rule:
    if not isinstance(raw, dict):
        raise RulesError(f"rule[{idx}] は表（[[rule]]）にしてください")
    rid = raw.get("id")
    where = f"rule[{idx}]" if not isinstance(rid, str) else f"rule {rid!r}"
    for k in ("language", "emit", "key_kind", "edge_type", "kind", "via", "name_form"):   # 語彙の値は文字列だけ（配列などは照合の前に拒否）
        if k in raw and not isinstance(raw[k], str):
            raise RulesError(f"{where}: {k} は文字列にしてください（実際: {raw[k]!r}）")
    lang = raw.get("language")
    if lang not in RULE_LANGUAGES:
        raise RulesError(f"{where}: language は {sorted(RULE_LANGUAGES)} のどれかにしてください（実際: {lang!r}）")
    unknown = sorted(set(raw) - _COMMON_KEYS - _LANG_KEYS[lang])
    if unknown:
        raise RulesError(f"{where}: 未知のキー {unknown}（language={lang} で使えるのは {sorted(_COMMON_KEYS | _LANG_KEYS[lang])}）")
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        raise RulesError(f"{where}: id は英数字・_・.・- の文字列にしてください（実際: {rid!r}）")
    if lang not in languages:
        raise RulesError(f"{where}: language {lang!r} がファイルの languages {sorted(languages)} にありません")
    version = _int(raw.get("version"), f"{where}: version", 1)
    emit = raw.get("emit")
    if emit not in ("def", "ref"):
        raise RulesError(f"{where}: emit は 'def' か 'ref' にしてください（実際: {emit!r}）")
    key_kind = raw.get("key_kind")
    if key_kind is not None and key_kind not in KEY_KINDS:
        raise RulesError(f"{where}: key_kind {key_kind!r} は閉じた語彙 {sorted(KEY_KINDS)} の外です")
    name_form = raw.get("name_form", "plain")
    if name_form not in NAME_FORMS:
        raise RulesError(f"{where}: name_form は {sorted(NAME_FORMS)} のどれかにしてください（実際: {name_form!r}）")
    edge_type = kind = via = None
    if emit == "def":
        stray = sorted(k for k in ("edge_type", "kind", "via", "name_form") if k in raw)
        if key_kind is None:
            raise RulesError(f"{where}: emit='def' には key_kind が要ります（キー単位の Config 定義）")
        if stray:
            raise RulesError(f"{where}: emit='def' では {stray} は使えません")
    else:
        edge_type, kind, via = raw.get("edge_type"), raw.get("kind"), raw.get("via")
        if edge_type not in edge_types:
            raise RulesError(f"{where}: edge_type {edge_type!r} は閉じた語彙 {sorted(edge_types)} の外です")
        if kind not in node_labels:
            raise RulesError(f"{where}: kind {kind!r} は閉じた語彙 {sorted(node_labels)} の外です")
        if via not in KNOWN_VIA:
            raise RulesError(f"{where}: via {via!r} は閉じた語彙（KNOWN_VIA）の外です")
        if (edge_type, kind) not in ALLOWED_REFS.get(via, frozenset()):
            ok = sorted(ALLOWED_REFS.get(via, ()))
            raise RulesError(f"{where}: via={via!r} で出せる (edge_type, kind) は {ok} です（実際: {(edge_type, kind)}）")
        if (key_kind is not None) and kind != "Config":
            raise RulesError(f"{where}: key_kind は kind='Config' の参照だけに付けられます")
        if via == "config_key" and key_kind is None:
            raise RulesError(f"{where}: via='config_key' には key_kind が要ります")
    fields: dict = {}
    if lang == "java":
        query = _str(raw.get("query"), f"{where}: query")
        try:
            q = _ts.query("java", query)
        except _ts.TreeSitterUnavailable:
            # Tree-sitter の無い実行系（sherpa を読むだけのスクリプト）では構文の検査を取り込みの入口へ回す
            # （`build_world` が `_ts.require()` で失敗させる・verify-extension は依存の入った実行系で走る）。
            q = None
        except Exception as e:   # tree_sitter.QueryError ほか（構文の誤り）
            raise RulesError(f"{where}: Tree-sitter のクエリを読めません（{type(e).__name__}: {' '.join(str(e).split())[:200]}）") from e
        names = {q.capture_name(i) for i in range(q.capture_count)} if q is not None else (
            {"value"} if re.search(r"@value\b", query) else set())
        if "value" not in names:
            raise RulesError(f"{where}: クエリに捕捉 @value（文字列リテラル）がありません")
        if len(_VALUE_CAPTURE.findall(query)) != len(re.findall(r"@value\b", query)):
            raise RulesError(f"{where}: @value は (string_literal) @value の形（文字列リテラルの捕捉）だけにしてください")
        fields["query"] = query
    else:
        fields["element"] = _str(raw.get("element"), f"{where}: element")
        fields["attribute"] = _str(raw.get("attribute"), f"{where}: attribute")
        for k in ("namespace", "root"):
            if k in raw:
                fields[k] = _str(raw[k], f"{where}: {k}")
    return Rule(id=rid, version=version, language=lang, emit=emit, key_kind=key_kind, edge_type=edge_type,
                kind=kind, via=via, name_form=name_form, **fields)


def _literal(parsed, node) -> str | None:
    """通常の文字列リテラルの中身（エスケープは解かない）。リテラルでなければ・text block なら `None`。"""
    if node is None or node.type != "string_literal":
        return None
    t = parsed.text(node)
    return None if t.startswith('"""') else t[1:-1]


def _ref(rule: Rule, name: str, line: int) -> RefCandidate:
    extra: dict = {"via": rule.via}
    if rule.key_kind is not None:
        extra["key_kind"] = rule.key_kind
    if rule.name_form != "plain":
        extra[rule.name_form] = True
    return RefCandidate(rule.edge_type, rule.kind, name, line, extra)


def _def(rule: Rule, name: str, line: int) -> DefItem:
    return DefItem(label="Config", name=name, line=line, cid_key=f"key:{rule.key_kind}:{name}",
                   extra={"key_kind": rule.key_kind})


class RulesPlugin(FwPlugin):
    """ルールファイルから作る FW プラグインの共通の実装（登録するのは `load_rules_plugin` が返す派生クラスのインスタンス）。"""

    rules: tuple = ()

    def _emit(self, text: str, emit: str) -> tuple[list, list]:
        out: list = []
        dropped: list = []
        rules = [r for r in self.rules if r.emit == emit]
        java = [r for r in rules if r.language == "java"]
        xml = [r for r in rules if r.language == "xml_config"]
        make = _def if emit == "def" else _ref
        if java:
            parsed = _ts.parse("java", text)
            for r in java:
                for caps in _ts.matches(parsed, r.query):
                    vnode = (caps.get("value") or [None])[0]
                    if vnode is None:
                        continue
                    anchor = (caps.get("at") or [vnode])[0]
                    line = _ts.start_line(anchor)
                    value = _literal(parsed, vnode)
                    if not value or not value.strip():
                        dropped.append(Dropped(f"rule_nonliteral:{r.id}", line, parsed.text(vnode)[:80]))
                        continue
                    out.append(make(r, value.strip(), line))
        if xml:
            doc = parse_xml(text)
            if doc.error is None and doc.root is not None:
                for r in xml:
                    if r.root is not None and doc.root.local != r.root:
                        continue
                    for el in doc.root.walk():
                        if el.local != r.element or (r.namespace is not None and el.ns != r.namespace):
                            continue
                        value = (el.attrs.get(r.attribute) or "").strip()
                        if not value:
                            dropped.append(Dropped(f"rule_missing_attribute:{r.id}", el.line, f"<{el.local} {r.attribute}>"))
                            continue
                        out.append(make(r, value, el.line))
        return out, dropped

    def collect_defs(self, text: str, rel_path: str, base: DefResult) -> PluginDefs:
        items, dropped = self._emit(text, "def")
        return PluginDefs(children=items, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str, base_defs: DefResult, base_refs: RefResult, types) -> PluginRefs:
        items, dropped = self._emit(text, "ref")
        return PluginRefs(refs=items, dropped=dropped)


def load_rules_plugin(path: Path, *, node_labels, edge_types, known_analyzer_names) -> FwPlugin:
    """ルールファイルを読み、検証して FW プラグイン（登録名 `rules:<name>`）を返す。失敗は `RulesError`（黙って読み飛ばさない）。"""
    stem = path.stem
    if path.is_symlink():
        raise RulesError("シンボリックリンクは使えません（置き場所の外を読めてしまうため）")
    try:
        raw_bytes = path.read_bytes()
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise RulesError(f"TOML を読めません（{type(e).__name__}: {e}）") from e
    unknown = sorted(set(data) - _FILE_KEYS)
    if unknown:
        raise RulesError(f"未知のキー {unknown}（使えるのは {sorted(_FILE_KEYS)}）")
    name = data.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise RulesError(f"name は英小文字・数字・_ の文字列にしてください（実際: {name!r}）")
    if stem != f"{RULE_FILE_PREFIX}_{name}":
        raise RulesError(f"ファイル名は '{RULE_FILE_PREFIX}_{name}.toml' にしてください（name={name!r}）")
    version = _int(data.get("version"), "version", 1)
    order = _int(data.get("order", 100), "order")
    langs = data.get("languages")
    if not isinstance(langs, list) or not langs or not all(isinstance(x, str) and x for x in langs):
        raise RulesError("languages は対象アナライザ名の非空の配列にしてください")
    unknown_langs = sorted(set(langs) - set(known_analyzer_names))
    if unknown_langs:
        raise RulesError(f"languages に未登録のアナライザ名があります: {unknown_langs}")
    unsupported = sorted(set(langs) - RULE_LANGUAGES)
    if unsupported:
        raise RulesError(f"languages {unsupported} はルールファイルでは扱えません（使えるのは {sorted(RULE_LANGUAGES)}）")
    kinds = data.get("config_kinds", [])
    if not isinstance(kinds, list) or not all(isinstance(x, str) and x for x in kinds):
        raise RulesError("config_kinds は文字列の配列にしてください")
    if kinds:
        if set(langs) != {"xml_config"}:
            raise RulesError("config_kinds を指定できるのは languages が ['xml_config'] だけのときです（Java は設定の種別を持たず、適用されなくなるため）")
        bad = sorted(set(kinds) - CONFIG_KINDS)
        if bad:
            raise RulesError(f"config_kinds {bad} は設定の種別の閉じた集合 {sorted(CONFIG_KINDS)} の外です")
    raw_rules = data.get("rule")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RulesError("[[rule]] が 1 件もありません")
    try:
        rules = tuple(_parse_rule(r, i, frozenset(langs), node_labels, edge_types) for i, r in enumerate(raw_rules))
    except RulesError:
        raise
    except (TypeError, ValueError, AttributeError) as e:   # 想定外の型の値は明示の失敗にする
        raise RulesError(f"ルールの値の型が不正です（{type(e).__name__}: {' '.join(str(e).split())[:200]}）") from e
    ids = [r.id for r in rules]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise RulesError(f"rule の id が重複しています: {dup}")
    used = {r.language for r in rules}
    if used != set(langs):
        raise RulesError(f"languages {sorted(set(langs) - used)} に対応するルールがありません")
    material = ("sha256:" + hashlib.sha256(raw_bytes).hexdigest(), tuple((r.id, r.version) for r in rules))
    cls = type(f"RulesPlugin_{name}", (RulesPlugin,), {
        "name": f"{RULE_FILE_PREFIX}:{name}", "version": version, "order": order,
        "languages": frozenset(langs), "config_kinds": frozenset(kinds), "rules": rules,
        "signature_extra": material,
    })
    return cls()
