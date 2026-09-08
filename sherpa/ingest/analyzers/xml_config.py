"""XML 設定アナライザ（アナライザ拡張 S3・A7 案A＋A2/A8＝Spring/MyBatis/Struts/TERASOLUNA 5.x）。

`.xml` 拡張子は**全件受理**する（`accepts()` は既定のまま上書きしない）——設定 XML と判定できた
ファイルだけファイル自体を主体定義（`Config`）にし、判定できないファイル（Maven pom・web.xml 等）は
primary なし＋`Dropped("xml_not_config", ...)` で通す（拒否すると台帳・grep・ES から消える・
docs/proposals/2026-09-05-アナライザ拡張.md §6 参照）。壊れた XML は `Dropped("xml_parse_error", ...)`。

ルート要素のローカル名で FW を判定する（`beans`/`mapper`/`struts` の根判定は namespace URI を
問わない）。Spring Batch（`<job>`/`<step>`/`<tasklet>`/`<chunk>`/`<job-listener>`）だけは
namespace URI（`http://www.springframework.org/schema/batch`）で判定する——`batch:`/`b:` 等の
プレフィックス綴りに依らず、逆に無名前空間の同名要素を誤って Batch 扱いしない（§4(c) RV 是正）。

- `beans`（Spring・TERASOLUNA 5.x は同じ `<beans>` を使うため区別しない）: `<bean class="...">`
  → `Config -INVOKES(via=bean_class, qualified)-> Module`。
- `mapper`（MyBatis）: ルートの `namespace` 属性 → `Module -INVOKES(via=mapper_namespace,
  qualified, reverse)-> Config`（A8＝Mapper インターフェース側から見た依存として逆向きに張る）。
  `<select/insert/update/delete resultType="..."/parameterType="...">` のうち `.` を含む
  識別子形の値だけ → `Config -INVOKES(via=mapper_type, qualified)-> Module`。非空だが完全修飾名
  でない値（MyBatis 組み込み別名 `int`/`string`/`map`/`hashmap` 等も含む）は
  `Dropped("mapper_type_alias", line, value)` として申告する（推測接続はしない・黙って捨てない）。
  `<select|insert|update|delete|sql>` の本文（子要素 `<if>`/`<where>`/`<foreach>`/`<include>` は
  展開せず、要素配下のテキストノードを出現順に連結する）から `FROM`/`JOIN`/`INSERT INTO`/
  `UPDATE`/`DELETE FROM`/`MERGE INTO` 直後のテーブル名を `_sql_scan` で抽出し、
  `Config -ACCESSES(via=mapper_sql)-> Table` として返す（S4'・波2持ち越し「Module→Config→Table
  の2段」契約——参照側 src はファイル primary に固定されている現行契約のため、`Table` 起点の
  incoming 探索は `Module -[mapper_namespace]-> Config -[mapper_sql]-> Table` の2ホップで
  Mapper 利用プログラムまで届く）。同一文中に同じ Table が複数回出現しても文ごとに1回だけ返す。
  動的プレースホルダ（`#{...}`/`${...}`）は `_sql_scan` が除外する。`<include refid="...">` は
  実体展開せず `Dropped("mapper_include", line, refid)` として申告する（旧 `Dropped("mapper_sql", ...)`＝
  文数だけの申告は本抽出に置き換わったため撤去——Table が world に無ければ共通層の unresolved flag に
  落ちる）。
- `struts`（Struts）: `<action class="...">` → `Config -INVOKES(via=action_class, qualified)-> Module`。
- Spring Batch（`beans` 直下・アナライザ拡張 波3 レーン B）: `<batch:job id="X">`（namespace URI
  判定・prefix/default namespace の綴りは問わない）→ キー X の `Config` children（既存 bean と
  同じ契約）。`<batch:step id="S">`（`X` の直接の子要素）→ キー `X.S`。job の外（`beans` 直下）の
  `<batch:step id="s">` は裸キー `s`。`<batch:tasklet ref="bean">`／`<batch:chunk reader="r"
  processor="p" writer="w">`／`<batch:job-listener ref="bean">` →
  `Config -ACCESSES(via=config_key, key_kind="bean")-> Config`（bean id への参照・src はファイル
  primary 固定・§4(b) 追補と同じ「参照は primary から」契約）。

**キー単位 children（S3'・A7 案B）**: primary（ファイル単位 `Config`）に加え、FW ごとのキーを
`DefItem(label="Config", name=<裸キー>, cid_key=f"key:{key_kind}:" + <裸キー>,
extra={"config_value": <先頭200文字>, "key_kind": <下記>})` として children に返す
（properties/yaml_config/java(URL)/shell(env) と同じ契約・`cid_key` の `"key:"` 接頭辞は primary と
の cid 名前空間分離のため、続く `key_kind` は同一ファイル内で異なる種別が同じ裸キーを持つ場合の
cid 衝突回避のため＝波3 統合 RV）。`key_kind`（RV 裁定・Config キーの名前空間分離）はキーの種別を
表す値で、共通層（`world_graph._link_config_key_all`）が `(key_kind, 名前)` で同種別のキーだけを
候補にする材料——`beans`/Spring Batch 系（bean id/name/alias・job/step）は `"bean"`、`<property>`
の `X.p` は `"property"`、`mapper` の文 id/`resultMap` は `"mapper"`、`struts` の `action`/`constant`
は `action`/`property`。

- `beans`: `<bean id="X">`／`<bean name="X">`（`name` はカンマ/空白区切りの複数エイリアスを
  各々1つの children として返す）→ キー X（値は `class` 属性・`key_kind="bean"`）。
  `<property name="p" value="v">`（属性値の直接形のみ・入れ子 `<value>` 要素は対象外）は、直接の親
  `<bean>` の識別子（`id` があれば `id`、無ければ `name` 分解後の最初のエイリアス）を接頭辞にした
  `X.p` というキーで1つだけ返す（`key_kind="property"`・`id`/`name` のどちらも無い匿名 bean 配下の
  `<property>` は接頭辞になる識別子が無いため children にしない）。`<property ref="b">`（値の代わりに
  他 bean を参照する形）／`<constructor-arg ref="b">` は children にせず
  `Config -ACCESSES(via=config_key, key_kind="bean")-> Config`（参照先 bean id・src はファイル
  primary 固定）として返す（黙って落とさない・RV 裁定）。`<context:property-placeholder
  location="…">` は外部プロパティファイルの参照であり children 化せず
  `Dropped("config_placeholder_location", line, location)` として申告するだけ（properties
  アナライザが当該ファイル自体を別途読む前提・§4(b) 追補）。`<alias name="A" alias="B">` は
  キー B（値は A・`key_kind="bean"`）。
- `mapper`: `<select|insert|update|delete|sql id="…">` の文 id → キー `<namespace>.<id>`（値は
  空・`key_kind="mapper"`）。`<resultMap id="R">` → キー `<namespace>.R`。ルートに `namespace`
  属性が無ければ接頭辞を組み立てられないため children を返さない。
- `struts`: `<action name="login">` → キー `login`（値は `class`・`key_kind="action"`）。
  `<constant name="k" value="v">` → キー k（`key_kind="property"`）。`<package name="p">` は
  children にしない（`action`/`constant` の収集自体は入れ子の深さを問わない・package 配下でも拾う）。

同一ファイル内の重複キーは `(key_kind, 裸キー)` の組で判定する（波3 統合 RV＝`key_kind` が違えば
名前空間が別なので同じ裸キーでも重複扱いしない——例: `<constant name="login">`（`key_kind="property"`）
と `<action name="login">`（`key_kind="action"`）は同一ファイルに両方残る）。同一 `(key_kind, 裸キー)`
の組が複数あれば最初の1つを採用し、後続は `Dropped("config_duplicate_key", line, key)` として申告する
（properties/yaml_config は単一 `key_kind` しか持たないため黙って後続を捨てる既存契約のままで変えない・
XML だけ申告する——本スライスの受け入れ条件どおり）。

`mapper_sql` の `KNOWN_VIA`/`VIA_PRIORITY`（`_base.py`）への追加は本アナライザの変更と別コミット
（S5b の cobol.py 差し替えと合わせて統合）で行う——それまでは共通層が `unknown_via` flag を記録し
`via` 属性だけを落とす（エッジ自体は張る）。

標準ライブラリ `xml.parsers.expat` の1パス push パーサで抽出と行番号（`CurrentLineNumber`）を
同時に取る——生テキストを別途検索して行番号を後付けする経路（コメント/CDATA 内の同名タグに
惑わされ、かつ改行の再カウントで要素数に対して二次的コストになる）を持たない。namespace 処理を
有効化し（`ParserCreate(namespace_separator=" ")`）、タグ名を `"URI local"`（無名前空間なら
`local` のみ）で受け取る（`_split_ns`）——`beans`/`mapper`/`struts` の根判定・bean/property/alias
等はローカル名だけで判定する（namespace URI/prefix を問わない・§4(c)）が、Spring Batch の
`job`/`step`/`tasklet`/`chunk`/`job-listener` だけは URI が Spring Batch のものと一致する場合に
限る。外部実体は `SetParamEntityParsing(XML_PARAM_ENTITY_PARSING_NEVER)` で
パラメータ実体展開を無効化し、`ExternalEntityRefHandler` が常に拒否（`0`）を返すことで一般外部実体の
展開も許さない（DTD 由来の XXE は対象外＝旧 `ElementTree.iterparse` と同水準の安全性を維持）。
"""
from __future__ import annotations

import re
import xml.parsers.expat as expat
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import _sql_scan
from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

XML_CONFIG_EXT = frozenset({".xml"})

# ルート要素のローカル名でだけ判定する（namespace URI/prefix は無視・§4(c)）。
_CONFIG_ROOTS = frozenset({"beans", "mapper", "struts"})

# `<sql>` は再利用可能な SQL 断片定義（`<include refid="...">` から参照される）——本文の
# テーブル抽出対象は SQL 文タグと同じ扱いにする（S4'）。
_SQL_STMT_TAGS = ("select", "insert", "update", "delete", "sql")

# `resultType`/`parameterType` を「クラス名らしい識別子」に絞る（`int`/`string`/`map` 等の
# エイリアスは弾く・§4(c)）。
_IDENTIFIER_WITH_DOT = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+$")


# Spring Batch の名前空間 URI（`batch:`/`b:` 等のプレフィックス名や default namespace の綴りに
# 依らず、この URI で判定する・§4(c) RV 是正）。
_BATCH_NS = "http://www.springframework.org/schema/batch"


def _split_ns(tag: str) -> tuple:
    """expat の namespace 処理（`namespace_separator=" "`）が返す `"URI local"`（名前空間あり）
    または `"local"`（無名前空間）からローカル名と URI を取り出す（`(URI またはNone, local)`）。"""
    if " " in tag:
        uri, local = tag.rsplit(" ", 1)
        return uri, local
    return None, tag


@dataclass
class _ScanResult:
    root_name: str | None = None
    error: expat.ExpatError | None = None
    # [(class属性値(またはNone), id属性値(またはNone), name属性値(またはNone), line, properties), ...]
    # `properties`＝この `<bean>` の直接の子 `<property name="p" value="v">` から
    # `[(name属性値, value属性値, line), ...]`（属性値の直接形のみ・S3' children 用）。
    beans: list = field(default_factory=list)
    property_placeholders: list = field(default_factory=list)  # [(location属性値(またはNone), line), ...]
    aliases: list = field(default_factory=list)             # [(name属性値, alias属性値, line), ...]
    mapper_namespace: tuple | None = None                   # (namespace属性値(またはNone), line)
    # [(id属性値(またはNone), resultType属性値(またはNone), parameterType属性値(またはNone),
    #   開始line, body_frags), ...]。`body_frags`＝`[(開始line, テキスト), ...]`（要素配下の
    # テキストノードを出現順に連結する材料・子要素は展開しないが、その配下のテキストノードは
    # 通過して集める・S4'）。
    sql_stmts: list = field(default_factory=list)
    result_maps: list = field(default_factory=list)         # [(id属性値(またはNone), line), ...]
    mapper_includes: list = field(default_factory=list)     # [(refid属性値(またはNone), line), ...]
    actions: list = field(default_factory=list)             # [(class属性値(またはNone), name属性値(またはNone), line), ...]
    constants: list = field(default_factory=list)           # [(name属性値(またはNone), value属性値(またはNone), line), ...]
    # Spring Batch（アナライザ拡張 波3 レーン B）: [(id属性値(またはNone), line), ...]。
    batch_jobs: list = field(default_factory=list)
    # [(job_id, step_id, line), ...]（`job_id` は直接の親 `<batch:job id=...>` の id・無ければ登録しない）。
    batch_steps: list = field(default_factory=list)
    # job の外（`<beans>` 直下＝depth==2）の `<batch:step id="s">`——裸キー `s` として登録する
    # （RV 是正）。[(step_id, line), ...]。
    batch_top_level_steps: list = field(default_factory=list)
    # `<property ref="b">`／`<constructor-arg ref="b">`／Spring Batch の `<batch:tasklet ref="b">`・
    # `<batch:chunk reader="r" processor="p" writer="w">`・`<batch:job-listener ref="b">` —— いずれも
    # 他 bean id への参照（children にはせず `Config -ACCESSES(via=config_key, key_kind="bean")->
    # Config` として返す・RV 裁定）。[(参照先 bean id, line), ...]。
    bean_ref_targets: list = field(default_factory=list)


def _scan(text: str) -> _ScanResult:
    """`xml.parsers.expat` で1回線形走査し、FW 判定に必要な最小限の情報を行番号付きで集める。

    push パーサ（イベント通知のみ・要素ツリーを構築しない）のため、巨大な設定 XML でも
    メモリに全木を保持しない。
    """
    result = _ScanResult()
    depth = 0
    buffer_active = False
    buffer_start_depth = 0
    buffer_frags: list = []
    current_stmt: tuple | None = None                      # (id, resultType, parameterType, line)
    # 直近の `<bean>` を辿るスタック（`[(depth, bean_entry), ...]`）——直接の子 `<property>` を
    # その `<bean>` の properties リストへ紐付けるために使う（S3'）。`bean_entry` の5番目の要素
    # （properties リスト）はタプル自体が不変でも中身は共有参照なので直接 append で書き換わる。
    bean_stack: list = []
    # Spring Batch の `<batch:job id="X">` を辿るスタック（`[(depth, job_id), ...]`）——直接の子
    # `<batch:step id="S">` を `X.S` として紐付けるために使う（bean_stack と同じ流儀）。
    job_stack: list = []
    # namespace 処理を有効化する（`URI local` 形式でタグ名を受け取る・Spring Batch 判定の
    # URI 検証に使う——`prefix:local` の文字列剥がしだけでは無名前空間の `<job>` も Batch と
    # 誤認してしまうため・§4(c) RV 是正）。
    parser = expat.ParserCreate(namespace_separator=" ")
    # 外部実体（DTD 由来の XXE）は一切展開しない——パラメータ実体展開を無効化し、
    # 一般外部実体の参照ハンドラは常に拒否（0）を返す。
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
                elif depth == 2:                # job 外のトップレベル step（`<beans>` 直下）
                    if step_id:
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
        if bean_stack and bean_stack[-1][0] == depth:      # 閉じタグは自身の depth（減算前）と一致
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
    """`frags`（`(開始line, テキスト)` の列・要素配下のテキストノードを出現順に連結した想定）から、
    連結後テキスト中の `offset` 位置が属する物理行番号を返す。各断片は expat が返した実際の
    改行を保持しているため、断片内で追加の改行を数えるだけで済む（生テキストの再検索はしない）。"""
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
    """`<bean name="a, b">` のカンマ/空白区切りの複数エイリアスを分解する（Spring の別名記法）。"""
    if not name_attr:
        return []
    return [n for n in _NAME_SPLIT.split(name_attr.strip()) if n]


class XmlConfigAnalyzer(Analyzer):
    """設定 XML（Spring beans／MyBatis mapper／Struts）→ `Config`（primary）＋キー単位 `Config`
    children（S3'）。設定でない XML は primary なし＋`Dropped("xml_not_config")`（拡張子内を
    全件受理・§6）。"""

    name = "xml_config"
    extensions = XML_CONFIG_EXT
    doctype = "xml_config"

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
            dup_key = (key_kind, key)                    # 名前空間（key_kind）ごとに重複を判定
            if dup_key in seen:                           # 同一ファイル内・同一 key_kind の重複は最初の1つ
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
                        # 非空だが完全修飾名でない値（MyBatis 組み込み別名 `int`/`string`/`map` 等含む）は
                        # 推測接続せず、解析対象外として申告する（黙って捨てない・§4(c)）。
                        dropped.append(Dropped("mapper_type_alias", line, val))

                if not body_frags:
                    continue
                combined = "".join(frag_text for _ln, frag_text in body_frags)
                # MyBatis（MySQL 方言想定）は `#` 行コメントも有効——DDL/EXEC SQL の DB2/COBOL
                # 方言（`#` が識別子文字）とは切り分ける。`#{...}` は動的プレースホルダのため除外。
                sanitized = _sql_scan.sanitize(combined, hash_line_comments=True)
                seen_names: set = set()
                for name, offset in _sql_scan.table_refs(sanitized):
                    if name in seen_names:                # 同一文中の同じ Table は1回だけ申告する
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
