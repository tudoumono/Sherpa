"""XML 設定アナライザ。設定 XML（Spring beans／MyBatis mapper／Struts／Spring Batch）をファイル自体の主体定義（`Config`）と、キー単位の `Config` children、クラス・bean・テーブルへの参照候補に分解する。

`.xml` は全件受理する。設定 XML と判定できたファイルだけ primary を持ち、判定できないもの（pom・web.xml 等）は primary なし＋`Dropped("xml_not_config")`。壊れた XML は `Dropped("xml_parse_error")`。
FW はルート要素のローカル名で判定する（namespace URI は問わない）。Spring Batch の要素だけは namespace URI で判定する。

参照:
- `beans`: `<bean class>` → `Config -INVOKES(via=bean_class, qualified)-> Module`（完全修飾名が資料フォルダに無ければ辺を張らず未解決）。`<bean parent>`・`<property ref>`・`<constructor-arg ref>` → `Config -ACCESSES(via=config_key, key_kind="bean")-> Config`。`<context:property-placeholder location>` は `Dropped("config_placeholder_location")`。
- `<import resource>`（`<beans>` 直下）→ `Config -INVOKES(via=include)-> Config`。宛先は `classpath:` 等のプレフィックスを除いたパスの形のまま（`extra["path_suffix"]`）で、共通層（`world_graph._resolve_path_suffix`）が同一 top_scope 内の末尾一致が1件のときだけ接続する（0件は `config_import_unresolved`、複数件は `config_import_ambiguous` の flag）。`resource` が無い場合は `Dropped("config_import_missing_resource")`、ワイルドカード（`*`/`?`）は `Dropped("config_import_wildcard")`。
- `mapper`: ルートの `namespace` → `Module -INVOKES(via=mapper_namespace, qualified, reverse)-> Config`。`resultType`/`parameterType` の完全修飾名 → `via=mapper_type`、組み込み別名など完全修飾名でない値は `Dropped("mapper_type_alias")`。`<select|insert|update|delete|sql>` の本文のテキストノードを連結し、`_sql_scan` で抽出したテーブル名を `Config -ACCESSES(via=mapper_sql)-> Table`（文ごとに同じテーブルは1回）。`<include refid>` は展開せず `Dropped("mapper_include")`。
- `struts`: `<action class>` → `Config -INVOKES(via=action_class, qualified)-> Module`。
- Spring Batch: `<batch:tasklet ref>`・`<batch:chunk reader/processor/writer>`・`<batch:job-listener ref>` → `ACCESSES(via=config_key, key_kind="bean")`。

キー単位 children: `DefItem(label="Config", name=<裸キー>, cid_key=f"key:{key_kind}:" + <裸キー>, extra={"config_value": <先頭200文字>, "key_kind": ...})`。`key_kind` は共通層（`world_graph._link_config_key_all`）が同種別のキーだけを接続候補にする材料。
- `beans`: `<bean id>`／`<bean name>`（カンマ/空白区切りの別名は各々1件）・`<alias>` → `"bean"`。`<property name value>` → `X.p`（`"property"`・直接の親 bean の識別子が接頭辞・匿名 bean 配下は返さない）。
- `mapper`: 文 id・`<resultMap id>` → `<namespace>.<id>`（`"mapper"`・`namespace` が無ければ返さない）。
- `struts`: `<action name>` → `"action"`、`<constant name>` → `"property"`。
- Spring Batch: `<batch:job id="X">` → キー X、その直接の子 `<batch:step id="S">` → `X.S`、job 外の step → 裸キー（いずれも `"bean"`）。
重複キーは `(key_kind, 裸キー)` で判定し、最初の1つを採用して後続は `Dropped("config_duplicate_key")`。

標準ライブラリ `xml.parsers.expat` の1パス push パーサで抽出と行番号を同時に取る（`ParserCreate(namespace_separator=" ")`・タグ名は `"URI local"`）。外部実体は展開しない（パラメータ実体展開を無効化し、外部実体参照ハンドラは常に拒否する）。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import re
import xml.parsers.expat as expat
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import _sql_scan
from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

XML_CONFIG_EXT = frozenset({".xml"})

# ルート要素のローカル名でだけ判定する（namespace URI/prefix は無視）。
_CONFIG_ROOTS = frozenset({"beans", "mapper", "struts"})

# `<sql>` は再利用可能な SQL 断片定義。本文は SQL 文タグと同じくテーブル抽出の対象にする。
_SQL_STMT_TAGS = ("select", "insert", "update", "delete", "sql")

# `resultType`/`parameterType` を「クラス名らしい識別子」に絞る（`int`/`string`/`map` 等のエイリアスは弾く）。
_IDENTIFIER_WITH_DOT = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+$")


# Spring Batch の名前空間 URI（`batch:`/`b:` 等の綴りに依らず、この URI で判定する）。
_BATCH_NS = "http://www.springframework.org/schema/batch"

# Spring の `<import resource>` が使うリソースローダのプレフィックス（他の未知プレフィックスはそのまま最終パスセグメントの抽出に進む）。
_IMPORT_PREFIX_RE = re.compile(r"^(classpath\*?:|file:)")


def _import_target(resource: str) -> str | None:
    """`<import resource>` の値からプレフィックスを除いた**パスの形のまま**を取り出す（ファイル名だけに縮めない。解決は共通層が末尾一致で一意に決まるときだけ行う）。

    Ant 風ワイルドカード（`*`/`?`）を含む値は一意に特定できないため `None`（呼び出し側が `Dropped` で申告する）。
    """
    value = resource.strip()
    value = _IMPORT_PREFIX_RE.sub("", value, count=1)
    if "*" in value or "?" in value:
        return None
    value = value.strip("/")
    if not value:
        return None
    return value


def _split_ns(tag: str) -> tuple:
    """expat が返す `"URI local"`（名前空間あり）または `"local"`（無名前空間）から `(URI または None, local)` を取り出す。"""
    if " " in tag:
        uri, local = tag.rsplit(" ", 1)
        return uri, local
    return None, tag


@dataclass
class _ScanResult:
    root_name: str | None = None
    error: expat.ExpatError | None = None
    # [(class属性値, id属性値, name属性値, line, properties), ...]。`properties`＝直接の子 `<property name value>` の `[(name, value, line), ...]`（属性値の直接形のみ）。
    beans: list = field(default_factory=list)
    property_placeholders: list = field(default_factory=list)
    aliases: list = field(default_factory=list)
    mapper_namespace: tuple | None = None
    # [(id, resultType, parameterType, 開始line, body_frags), ...]。`body_frags`＝`[(開始line, テキスト), ...]`（要素配下のテキストノードを出現順に集める。子要素は展開しない）。
    sql_stmts: list = field(default_factory=list)
    result_maps: list = field(default_factory=list)
    mapper_includes: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    constants: list = field(default_factory=list)
    # Spring Batch: [(id属性値(またはNone), line), ...]。
    batch_jobs: list = field(default_factory=list)
    # [(job_id, step_id, line), ...]（`job_id` は直接の親 `<batch:job>` の id・無ければ登録しない）。
    batch_steps: list = field(default_factory=list)
    # job の外（`<beans>` 直下＝depth==2）の `<batch:step id="s">` は裸キー `s` として登録する。[(step_id, line), ...]。
    batch_top_level_steps: list = field(default_factory=list)
    # `<property ref>`／`<constructor-arg ref>`／Spring Batch の `tasklet ref`・`chunk reader/processor/writer`・`job-listener ref` は他 bean id への参照（children にはせず `ACCESSES(via=config_key, key_kind="bean")` として返す）。[(参照先 bean id, line), ...]。
    bean_ref_targets: list = field(default_factory=list)
    # `<import resource>`（`<beans>` 直下のみ）: [(resource属性値, line), ...]。
    imports: list = field(default_factory=list)


def _scan(text: str) -> _ScanResult:
    """`xml.parsers.expat` で1回線形走査し、FW 判定に必要な最小限の情報を行番号付きで集める（要素ツリーは構築しない）。"""
    result = _ScanResult()
    depth = 0
    buffer_active = False
    buffer_start_depth = 0
    buffer_frags: list = []
    current_stmt: tuple | None = None
    # 直近の `<bean>` を辿るスタック（`[(depth, bean_entry), ...]`）。直接の子 `<property>` をその bean の properties リストへ紐付ける（リストは共有参照なので直接 append する）。
    bean_stack: list = []
    # Spring Batch の `<batch:job id>` を辿るスタック（`[(depth, job_id), ...]`）。直接の子 `<batch:step>` を `X.S` として紐付ける。
    job_stack: list = []
    # namespace 処理を有効化する（タグ名を `URI local` 形式で受け取り、Spring Batch の URI 検証に使う）。
    parser = expat.ParserCreate(namespace_separator=" ")
    # 外部実体は展開しない（パラメータ実体展開を無効化し、一般外部実体の参照ハンドラは常に拒否する）。
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.ExternalEntityRefHandler = lambda context, base, system_id, public_id: 0

    def _start(name, attrs):
        nonlocal depth, buffer_active, buffer_start_depth, buffer_frags, current_stmt
        depth += 1
        ns_uri, local = _split_ns(name)
        is_batch = ns_uri == _BATCH_NS
        line = parser.CurrentLineNumber
        if depth == 1:
            result.root_name = local
            if local == "mapper":
                result.mapper_namespace = (attrs.get("namespace"), line)
            return
        if result.root_name == "beans":
            if local == "bean":
                bean_entry = (attrs.get("class"), attrs.get("id"), attrs.get("name"), line, [])
                result.beans.append(bean_entry)
                bean_stack.append((depth, bean_entry))
                parent_attr = attrs.get("parent")
                if parent_attr:
                    result.bean_ref_targets.append((parent_attr, line))
            elif local == "import" and depth == 2:
                result.imports.append((attrs.get("resource"), line))
            elif local == "property" and bean_stack and bean_stack[-1][0] == depth - 1:
                prop_name, prop_value, prop_ref = attrs.get("name"), attrs.get("value"), attrs.get("ref")
                if prop_name is not None and prop_value is not None:
                    bean_stack[-1][1][4].append((prop_name, prop_value, line))
                elif prop_ref is not None:
                    result.bean_ref_targets.append((prop_ref, line))
            elif local == "constructor-arg" and bean_stack and bean_stack[-1][0] == depth - 1:
                ctor_ref = attrs.get("ref")
                if ctor_ref is not None:
                    result.bean_ref_targets.append((ctor_ref, line))
            elif local == "alias":
                result.aliases.append((attrs.get("name"), attrs.get("alias"), line))
            elif local == "property-placeholder":
                result.property_placeholders.append((attrs.get("location"), line))
            elif is_batch and local == "job":
                id_attr = attrs.get("id")
                result.batch_jobs.append((id_attr, line))
                if id_attr:
                    job_stack.append((depth, id_attr))
            elif is_batch and local == "step":
                step_id = attrs.get("id")
                if job_stack and job_stack[-1][0] == depth - 1:
                    if step_id:
                        result.batch_steps.append((job_stack[-1][1], step_id, line))
                elif depth == 2 and step_id:  # job 外のトップレベル step（`<beans>` 直下）
                    result.batch_top_level_steps.append((step_id, line))
            elif is_batch and local == "tasklet":
                tasklet_ref = attrs.get("ref")
                if tasklet_ref:
                    result.bean_ref_targets.append((tasklet_ref, line))
            elif is_batch and local == "chunk":
                for attr_name in ("reader", "processor", "writer"):
                    val = attrs.get(attr_name)
                    if val:
                        result.bean_ref_targets.append((val, line))
            elif is_batch and local == "job-listener":
                listener_ref = attrs.get("ref")
                if listener_ref:
                    result.bean_ref_targets.append((listener_ref, line))
        elif result.root_name == "mapper":
            if local in _SQL_STMT_TAGS and not buffer_active:
                buffer_active = True
                buffer_start_depth = depth
                buffer_frags = []
                current_stmt = (attrs.get("id"), attrs.get("resultType"), attrs.get("parameterType"), line)
            elif local == "include" and buffer_active:
                result.mapper_includes.append((attrs.get("refid"), line))
            elif local == "resultMap":
                result.result_maps.append((attrs.get("id"), line))
        elif result.root_name == "struts":
            if local == "action":
                result.actions.append((attrs.get("class"), attrs.get("name"), line))
            elif local == "constant":
                result.constants.append((attrs.get("name"), attrs.get("value"), line))

    def _end(name):
        nonlocal depth, buffer_active, buffer_frags, current_stmt
        if buffer_active and depth == buffer_start_depth:
            stmt_id, result_type, param_type, line = current_stmt
            result.sql_stmts.append((stmt_id, result_type, param_type, line, buffer_frags))
            buffer_active = False
            buffer_frags = []
            current_stmt = None
        if bean_stack and bean_stack[-1][0] == depth:
            bean_stack.pop()
        if job_stack and job_stack[-1][0] == depth:
            job_stack.pop()
        depth -= 1

    def _chars(data):
        if buffer_active:
            buffer_frags.append((parser.CurrentLineNumber, data))

    parser.StartElementHandler = _start
    parser.EndElementHandler = _end
    parser.CharacterDataHandler = _chars
    try:
        parser.Parse(text, True)
    except expat.ExpatError as exc:
        result.error = exc
    return result


def _line_for_offset(frags: list, offset: int) -> int:
    """`frags`（`(開始line, テキスト)` の列）から、連結後テキスト中の `offset` 位置が属する物理行番号を返す。"""
    pos = 0
    for start_line, frag_text in frags:
        end = pos + len(frag_text)
        if offset < end:
            return start_line + frag_text[:offset - pos].count("\n")
        pos = end
    if frags:
        start_line, frag_text = frags[-1]
        return start_line + frag_text.count("\n")
    return 1


_NAME_SPLIT = re.compile(r"[,\s]+")


def _split_bean_names(name_attr: str | None) -> list:
    """`<bean name="a, b">` のカンマ/空白区切りの複数エイリアスを分解する。"""
    if not name_attr:
        return []
    return [n for n in _NAME_SPLIT.split(name_attr.strip()) if n]


class XmlConfigAnalyzer(Analyzer):
    """設定 XML（Spring beans／MyBatis mapper／Struts）→ `Config`（primary）＋キー単位 `Config` children。設定でない XML は primary なし＋`Dropped("xml_not_config")`。"""

    name = "xml_config"
    extensions = XML_CONFIG_EXT
    doctype = "xml_config"
    # 解析結果が変わる変更をしたときに上げる（`registry.config_signature()` の材料。上げると既存 world が全再構築される）。
    version = 3

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        scan = _scan(text)
        if scan.error is not None:
            line = getattr(scan.error, "lineno", None) or 1
            return DefResult(dropped=[Dropped("xml_parse_error", line, str(scan.error)[:120])])
        if scan.root_name is None or scan.root_name not in _CONFIG_ROOTS:
            return DefResult(dropped=[Dropped("xml_not_config", 1, scan.root_name or "")])
        name = PurePosixPath(rel_path).name
        primary = DefItem(label="Config", name=name)

        children: list = []
        dropped: list = []
        seen: set = set()

        def _add_child(key: str | None, value: str, line: int, key_kind: str) -> None:
            if not key:
                return
            dup_key = (key_kind, key)
            if dup_key in seen:
                dropped.append(Dropped("config_duplicate_key", line, key))
                return
            seen.add(dup_key)
            children.append(DefItem(label="Config", name=key, line=line,
                                    cid_key=f"key:{key_kind}:{key}",
                                    extra={"config_value": (value or "")[:200], "key_kind": key_kind}))

        if scan.root_name == "beans":
            for cls, id_attr, name_attr, line, properties in scan.beans:
                identities = ([id_attr] if id_attr else []) + _split_bean_names(name_attr)
                for ident in identities:
                    _add_child(ident, cls or "", line, "bean")
                primary_ident = identities[0] if identities else None
                if primary_ident:
                    for prop_name, prop_value, prop_line in properties:
                        _add_child(f"{primary_ident}.{prop_name}", prop_value, prop_line, "property")
            for location, line in scan.property_placeholders:
                dropped.append(Dropped("config_placeholder_location", line, location or ""))
            for name_attr, alias_attr, line in scan.aliases:
                _add_child(alias_attr, name_attr or "", line, "bean")
            for job_id, line in scan.batch_jobs:
                _add_child(job_id, "", line, "bean")
            for job_id, step_id, line in scan.batch_steps:
                if job_id:
                    _add_child(f"{job_id}.{step_id}", "", line, "bean")
            for step_id, line in scan.batch_top_level_steps:
                _add_child(step_id, "", line, "bean")

        elif scan.root_name == "mapper":
            namespace = scan.mapper_namespace[0] if scan.mapper_namespace else None
            if namespace:
                for stmt_id, _result_type, _param_type, line, _body_frags in scan.sql_stmts:
                    if stmt_id:
                        _add_child(f"{namespace}.{stmt_id}", "", line, "mapper")
                for result_map_id, line in scan.result_maps:
                    if result_map_id:
                        _add_child(f"{namespace}.{result_map_id}", "", line, "mapper")

        elif scan.root_name == "struts":
            for cls, name_attr, line in scan.actions:
                _add_child(name_attr, cls or "", line, "action")
            for name_attr, value_attr, line in scan.constants:
                _add_child(name_attr, value_attr or "", line, "property")

        return DefResult(primary=primary, children=children, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        scan = _scan(text)
        if scan.error is not None or scan.root_name not in _CONFIG_ROOTS:
            return RefResult()

        refs: list = []
        dropped: list = []

        if scan.root_name == "beans":
            for cls, _id_attr, _name_attr, line, _properties in scan.beans:
                if cls:
                    refs.append(RefCandidate("INVOKES", "Module", cls, line,
                                             extra={"via": "bean_class", "qualified": True}))
            for ref_target, line in scan.bean_ref_targets:
                refs.append(RefCandidate("ACCESSES", "Config", ref_target, line,
                                         extra={"via": "config_key", "key_kind": "bean"}))
            for resource, line in scan.imports:
                if not resource:
                    dropped.append(Dropped("config_import_missing_resource", line, ""))
                    continue
                target = _import_target(resource)
                if target is None:
                    dropped.append(Dropped("config_import_wildcard", line, resource))
                    continue
                refs.append(RefCandidate("INVOKES", "Config", target, line,
                                         extra={"via": "include", "path_suffix": True}))

        elif scan.root_name == "mapper":
            if scan.mapper_namespace and scan.mapper_namespace[0]:
                namespace, line = scan.mapper_namespace
                refs.append(RefCandidate("INVOKES", "Module", namespace, line,
                                         extra={"via": "mapper_namespace", "qualified": True},
                                         reverse=True))
            for _stmt_id, result_type, param_type, line, body_frags in scan.sql_stmts:
                for val in (result_type, param_type):
                    if not val:
                        continue
                    if _IDENTIFIER_WITH_DOT.match(val):
                        refs.append(RefCandidate("INVOKES", "Module", val, line,
                                                 extra={"via": "mapper_type", "qualified": True}))
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
                    dropped.append(Dropped("mapper_sql_dynamic_table", _line_for_offset(body_frags, offset),
                                           sanitized[offset:offset + 80].strip()))
                seen_names: set = set()
                for name, offset in _sql_scan.table_refs(sanitized):
                    if name in seen_names:  # 同一文中の同じ Table は1回だけ申告する
                        continue
                    seen_names.add(name)
                    table_line = _line_for_offset(body_frags, offset)
                    refs.append(RefCandidate("ACCESSES", "Table", name, table_line,
                                             extra={"via": "mapper_sql"}))

            for refid, line in scan.mapper_includes:
                dropped.append(Dropped("mapper_include", line, refid or ""))

        elif scan.root_name == "struts":
            for cls, _name_attr, line in scan.actions:
                if cls:
                    refs.append(RefCandidate("INVOKES", "Module", cls, line,
                                             extra={"via": "action_class", "qualified": True}))

        return RefResult(refs=refs, dropped=dropped)
