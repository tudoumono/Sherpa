"""XML 設定の FW プラグイン（Spring／MyBatis／Struts）。本体 `xml_config`（汎用の読み取り・設定の種別の判定）の結果へ FW 固有の定義・参照を足す。

適用条件は設定の種別（`XmlConfigAnalyzer.config_kind`）: `xml:spring`＝`spring_beans`・`xml:mybatis`＝`mybatis_mapper`・`xml:struts`＝`struts`。
各プラグインは自分の種別のファイルにだけ適用され、名前は解決せず参照の候補を返すだけ（解決は共通層・`docs/21-拡張の契約.md` §3a）。
同じ入力から同じ `(始点, 型, 終点, via)` の辺が出る（移し替え前の本体の出力と同じ）。

- `xml:spring`（Spring beans と Spring Batch）:
  - 参照: `<bean class>` → `Config -INVOKES(via=bean_class, qualified)-> Module`（完全修飾名が資料フォルダに無ければ辺を張らず未解決）。
    `<bean parent>`・`<property ref>`・`<constructor-arg ref>`・`<batch:tasklet ref>`・`<batch:chunk reader/processor/writer>`・`<batch:job-listener ref>` →
    `Config -ACCESSES(via=config_key, key_kind="bean")-> Config`。`<import resource>`（`<beans>` 直下）→ `Config -INVOKES(via=include)-> Config`
    （宛先は `classpath:` 等を除いたパスの形のまま〔`extra["path_suffix"]`〕で、共通層 `world_graph._resolve_path_suffix` が同一 top_scope 内の末尾一致が 1 件のときだけ接続する）。
    `resource` が無ければ `Dropped("config_import_missing_resource")`、ワイルドカード（`*`/`?`）は `Dropped("config_import_wildcard")`。
    `<context:property-placeholder location>` は `Dropped("config_placeholder_location")`。
  - 定義（キー単位の `Config`・`key_kind`）: `<bean id>`／`<bean name>`（カンマ・空白区切りの別名は各々 1 件）・`<alias>` → `"bean"`。
    `<property name value>` → `X.p`（`"property"`・直接の親 bean の識別子が接頭辞・匿名 bean 配下は返さない）。
    `<batch:job id="X">` → キー X、その直接の子 `<batch:step id="S">` → `X.S`、job 外の step → 裸キー（いずれも `"bean"`。名前空間 URI で判定する）。
- `xml:mybatis`: ルートの `namespace` → `Module -INVOKES(via=mapper_namespace, qualified, reverse)-> Config`。
  `resultType`/`parameterType` の完全修飾名 → `via=mapper_type`（組み込み別名など完全修飾名でない値は `Dropped("mapper_type_alias")`）。
  `<select|insert|update|delete|sql>` の本文のテキストノードを連結し `_sql_scan` で抽出したテーブル名 → `Config -ACCESSES(via=mapper_sql)-> Table`（文ごとに同じテーブルは 1 回）。
  `<include refid>` は展開せず `Dropped("mapper_include")`。定義: 文 id・`<resultMap id>` → `<namespace>.<id>`（`"mapper"`・`namespace` が無ければ返さない）。
- `xml:struts`: `<action class>` → `Config -INVOKES(via=action_class, qualified)-> Module`。定義: `<action name>` → `"action"`・`<constant name>` → `"property"`。

キー単位 children は `DefItem(label="Config", name=<裸キー>, cid_key=f"key:{key_kind}:" + <裸キー>, extra={"config_value": <先頭200文字>, "key_kind": ...})`。
重複キーはプラグインごとに `(key_kind, 裸キー)` で判定し、最初の 1 つを採用して後続は `Dropped("config_duplicate_key")`。
`key_kind` は共通層（`world_graph._link_config_key_all`）が同種別のキーだけを接続候補にする材料。
設計: docs/proposals/2026-10-04-アナライザとグラフの改善.md 段階 2
"""
from __future__ import annotations

import re

from . import _sql_scan
from ._base import DefItem, DefResult, Dropped, FwPlugin, PluginDefs, PluginRefs, RefCandidate, RefResult
from .xml_config import line_for_offset, parse_xml

_XML = frozenset({"xml_config"})

# Spring Batch の名前空間 URI（`batch:`/`b:` 等の綴りに依らず、この URI で判定する）。
_BATCH_NS = "http://www.springframework.org/schema/batch"

# Spring の `<import resource>` が使うリソースローダのプレフィックス（他の未知プレフィックスはそのまま扱う）。
_IMPORT_PREFIX_RE = re.compile(r"^(classpath\*?:|file:)")

# `<sql>` は再利用可能な SQL 断片定義。本文は SQL 文タグと同じくテーブル抽出の対象にする。
_SQL_STMT_TAGS = ("select", "insert", "update", "delete", "sql")

# `resultType`/`parameterType` を「クラス名らしい識別子」に絞る（`int`/`string`/`map` 等のエイリアスは弾く）。
_IDENTIFIER_WITH_DOT = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+$")

_NAME_SPLIT = re.compile(r"[,\s]+")


def _import_target(resource: str) -> str | None:
    """`<import resource>` の値からプレフィックスを除いた**パスの形のまま**を取り出す（解決は共通層が末尾一致で一意に決まるときだけ行う）。ワイルドカードを含む値は `None`。"""
    value = _IMPORT_PREFIX_RE.sub("", resource.strip(), count=1)
    if "*" in value or "?" in value:
        return None
    return value.strip("/") or None


def _split_bean_names(name_attr: str | None) -> list:
    """`<bean name="a, b">` のカンマ/空白区切りの複数エイリアスを分解する。"""
    if not name_attr:
        return []
    return [n for n in _NAME_SPLIT.split(name_attr.strip()) if n]


class _ChildCollector:
    """キー単位の `Config` children を `(key_kind, 裸キー)` で重複排除しながら集める。"""

    def __init__(self) -> None:
        self.children: list = []
        self.dropped: list = []
        self._seen: set = set()

    def add(self, key: str | None, value: str, line: int, key_kind: str) -> None:
        if not key:
            return
        dup_key = (key_kind, key)
        if dup_key in self._seen:
            self.dropped.append(Dropped("config_duplicate_key", line, key))
            return
        self._seen.add(dup_key)
        self.children.append(DefItem(label="Config", name=key, line=line, cid_key=f"key:{key_kind}:{key}",
                                     extra={"config_value": (value or "")[:200], "key_kind": key_kind}))


def _root(text: str):
    """本体と同じ木（構文エラーなら `None`＝種別が `None` になり適用されないので通常は起きない）。"""
    doc = parse_xml(text)
    return None if doc.error is not None else doc.root


class SpringXmlPlugin(FwPlugin):
    """Spring の beans 設定（Spring Batch を含む）。"""

    name = "xml:spring"
    languages = _XML
    config_kinds = frozenset({"spring_beans"})
    version = 1

    def collect_defs(self, text: str, rel_path: str, base: DefResult) -> PluginDefs:
        root = _root(text)
        out = _ChildCollector()
        if root is None:
            return PluginDefs()
        beans, aliases, placeholders = [], [], []
        jobs, steps, top_steps = [], [], []
        for el in root.walk():
            if el.depth < 2:
                continue
            line, local = el.line, el.local
            is_batch = el.ns == _BATCH_NS
            if local == "bean":
                beans.append(el)
            elif local == "alias":
                aliases.append((el.attrs.get("name"), el.attrs.get("alias"), line))
            elif local == "property-placeholder":
                placeholders.append((el.attrs.get("location"), line))
            elif is_batch and local == "job":
                jobs.append((el.attrs.get("id"), line))
            elif is_batch and local == "step":
                step_id = el.attrs.get("id")
                parent = el.parent
                if parent is not None and parent.ns == _BATCH_NS and parent.local == "job" and parent.attrs.get("id"):
                    if step_id:
                        steps.append((parent.attrs["id"], step_id, line))
                elif el.depth == 2 and step_id:
                    top_steps.append((step_id, line))
        for el in beans:
            identities = ([el.attrs.get("id")] if el.attrs.get("id") else []) + _split_bean_names(el.attrs.get("name"))
            for ident in identities:
                out.add(ident, el.attrs.get("class") or "", el.line, "bean")
            if identities:
                for child in el.content:
                    if getattr(child, "local", None) != "property":
                        continue
                    pname, pvalue = child.attrs.get("name"), child.attrs.get("value")
                    if pname is not None and pvalue is not None:
                        out.add(f"{identities[0]}.{pname}", pvalue, child.line, "property")
        for location, line in placeholders:
            out.dropped.append(Dropped("config_placeholder_location", line, location or ""))
        for name_attr, alias_attr, line in aliases:
            out.add(alias_attr, name_attr or "", line, "bean")
        for job_id, line in jobs:
            out.add(job_id, "", line, "bean")
        for job_id, step_id, line in steps:
            out.add(f"{job_id}.{step_id}", "", line, "bean")
        for step_id, line in top_steps:
            out.add(step_id, "", line, "bean")
        return PluginDefs(children=out.children, dropped=out.dropped)

    def extract_refs(self, text: str, rel_path: str, base_defs, base_refs: RefResult, types) -> PluginRefs:
        root = _root(text)
        if root is None:
            return PluginRefs()
        class_refs, bean_refs, import_refs, dropped = [], [], [], []

        def _bean_ref(target, line):
            bean_refs.append(RefCandidate("ACCESSES", "Config", target, line, extra={"via": "config_key", "key_kind": "bean"}))

        for el in root.walk():
            if el.depth < 2:
                continue
            local, attrs, line = el.local, el.attrs, el.line
            is_batch = el.ns == _BATCH_NS
            parent_is_bean = el.parent is not None and el.parent.local == "bean" and el.parent.depth >= 2
            if local == "bean":
                if attrs.get("class"):
                    class_refs.append(RefCandidate("INVOKES", "Module", attrs["class"], line,
                                                   extra={"via": "bean_class", "qualified": True}))
                if attrs.get("parent"):
                    _bean_ref(attrs["parent"], line)
            elif local == "import" and el.depth == 2:
                resource = attrs.get("resource")
                if not resource:
                    dropped.append(Dropped("config_import_missing_resource", line, ""))
                    continue
                target = _import_target(resource)
                if target is None:
                    dropped.append(Dropped("config_import_wildcard", line, resource))
                    continue
                import_refs.append(RefCandidate("INVOKES", "Config", target, line,
                                                extra={"via": "include", "path_suffix": True}))
            elif local == "property" and parent_is_bean:
                if not (attrs.get("name") is not None and attrs.get("value") is not None) and attrs.get("ref") is not None:
                    _bean_ref(attrs["ref"], line)
            elif local == "constructor-arg" and parent_is_bean:
                if attrs.get("ref") is not None:
                    _bean_ref(attrs["ref"], line)
            elif is_batch and local == "tasklet":
                if attrs.get("ref"):
                    _bean_ref(attrs["ref"], line)
            elif is_batch and local == "chunk":
                for attr_name in ("reader", "processor", "writer"):
                    if attrs.get(attr_name):
                        _bean_ref(attrs[attr_name], line)
            elif is_batch and local == "job-listener":
                if attrs.get("ref"):
                    _bean_ref(attrs["ref"], line)
        # 出力の並びは「bean の class → bean 参照 → import」（移し替え前と同じ）
        return PluginRefs(refs=class_refs + bean_refs + import_refs, dropped=dropped)


class MyBatisXmlPlugin(FwPlugin):
    """MyBatis の mapper 設定（namespace・文 id・型・SQL のテーブル）。"""

    name = "xml:mybatis"
    languages = _XML
    config_kinds = frozenset({"mybatis_mapper"})
    version = 1

    @staticmethod
    def _scan(root):
        """`(namespace と行, [(id, resultType, parameterType, 開始行, 本文の断片)], [(resultMap の id, 行)], [(include の refid, 行)])`。

        文タグは入れ子の文の中では数えない（外側の文の本文に含める）。`<include>` は文の中だけ数える。
        """
        stmts, result_maps, includes = [], [], []
        namespace = (root.attrs.get("namespace"), root.line)

        stack = [(c, False) for c in reversed(root.content) if hasattr(c, "local")]
        while stack:
            el, in_stmt = stack.pop()
            inner = in_stmt
            if el.local in _SQL_STMT_TAGS and not in_stmt:
                stmts.append((el.attrs.get("id"), el.attrs.get("resultType"), el.attrs.get("parameterType"),
                              el.line, el.text_frags()))
                inner = True
            elif el.local == "include" and in_stmt:
                includes.append((el.attrs.get("refid"), el.line))
            elif el.local == "resultMap":
                result_maps.append((el.attrs.get("id"), el.line))
            stack.extend((c, inner) for c in reversed(el.content) if hasattr(c, "local"))
        return namespace, stmts, result_maps, includes

    def collect_defs(self, text: str, rel_path: str, base: DefResult) -> PluginDefs:
        root = _root(text)
        if root is None:
            return PluginDefs()
        out = _ChildCollector()
        (namespace, _ns_line), stmts, result_maps, _includes = self._scan(root)
        if namespace:
            for stmt_id, _rt, _pt, line, _frags in stmts:
                if stmt_id:
                    out.add(f"{namespace}.{stmt_id}", "", line, "mapper")
            for result_map_id, line in result_maps:
                if result_map_id:
                    out.add(f"{namespace}.{result_map_id}", "", line, "mapper")
        return PluginDefs(children=out.children, dropped=out.dropped)

    def extract_refs(self, text: str, rel_path: str, base_defs, base_refs: RefResult, types) -> PluginRefs:
        root = _root(text)
        if root is None:
            return PluginRefs()
        refs: list = []
        dropped: list = []
        (namespace, ns_line), stmts, _result_maps, includes = self._scan(root)
        if namespace:
            refs.append(RefCandidate("INVOKES", "Module", namespace, ns_line,
                                     extra={"via": "mapper_namespace", "qualified": True}, reverse=True))
        for _stmt_id, result_type, param_type, line, body_frags in stmts:
            for val in (result_type, param_type):
                if not val:
                    continue
                if _IDENTIFIER_WITH_DOT.match(val):
                    refs.append(RefCandidate("INVOKES", "Module", val, line, extra={"via": "mapper_type", "qualified": True}))
                else:
                    # 非空だが完全修飾名でない値（MyBatis 組み込み別名 `int`/`string`/`map` 等）は推測接続せず、解析対象外として申告する。
                    dropped.append(Dropped("mapper_type_alias", line, val))
            if not body_frags:
                continue
            combined = "".join(frag_text for _ln, frag_text in body_frags)
            # MyBatis は `#` 行コメントも有効（DDL/EXEC SQL の DB2/COBOL 方言とは切り分ける）。`#{...}` は動的プレースホルダのため除外する。
            sanitized = _sql_scan.sanitize(combined, hash_line_comments=True)
            for offset in _sql_scan.dynamic_table_offsets(sanitized):
                # `${...}` を含む表名は実行時に決まる＝解決せず申告する。
                dropped.append(Dropped("mapper_sql_dynamic_table", line_for_offset(body_frags, offset),
                                       sanitized[offset:offset + 80].strip()))
            seen_names: set = set()
            for name, offset in _sql_scan.table_refs(sanitized):
                if name in seen_names:  # 同一文中の同じ Table は 1 回だけ申告する
                    continue
                seen_names.add(name)
                refs.append(RefCandidate("ACCESSES", "Table", name, line_for_offset(body_frags, offset),
                                         extra={"via": "mapper_sql"}))
        for refid, line in includes:
            dropped.append(Dropped("mapper_include", line, refid or ""))
        return PluginRefs(refs=refs, dropped=dropped)


class StrutsXmlPlugin(FwPlugin):
    """Struts の設定（action の対応・constant）。"""

    name = "xml:struts"
    languages = _XML
    config_kinds = frozenset({"struts"})
    version = 1

    def collect_defs(self, text: str, rel_path: str, base: DefResult) -> PluginDefs:
        root = _root(text)
        if root is None:
            return PluginDefs()
        out = _ChildCollector()
        actions, constants = [], []
        for el in root.walk():
            if el.depth < 2:
                continue
            if el.local == "action":
                actions.append(el)
            elif el.local == "constant":
                constants.append(el)
        for el in actions:
            out.add(el.attrs.get("name"), el.attrs.get("class") or "", el.line, "action")
        for el in constants:
            out.add(el.attrs.get("name"), el.attrs.get("value") or "", el.line, "property")
        return PluginDefs(children=out.children, dropped=out.dropped)

    def extract_refs(self, text: str, rel_path: str, base_defs, base_refs: RefResult, types) -> PluginRefs:
        root = _root(text)
        if root is None:
            return PluginRefs()
        refs = [RefCandidate("INVOKES", "Module", el.attrs["class"], el.line,
                             extra={"via": "action_class", "qualified": True})
                for el in root.walk() if el.depth >= 2 and el.local == "action" and el.attrs.get("class")]
        return PluginRefs(refs=refs)


FW_PLUGINS = [SpringXmlPlugin(), MyBatisXmlPlugin(), StrutsXmlPlugin()]
