"""辺の根拠（sources）の集約・COPY の循環・未解決の申告の上限・取り込み記録（flags）からの取り出し。

設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

from collections import defaultdict

from ..analyzers import registry as analyzer_registry
from .periods_select import merge_confirm
from .state import WorldBuild


EDGE_SOURCES_MAX = 20     # 1 本の辺の `sources`（根拠）の上限。超過は `sources_overflow_count`

# 辺の根拠 `sources[].rule`（接続を決めた解決規則の名前）の閉じた一覧。解決器（`_link`）と言及の突合がここの名前だけを付ける。
EDGE_RULES = frozenset({
    "alias",                 # 別名（`using A = …`・`Imports A = …`）
    "single_import",         # 単一型の import
    "same_package",          # 同じ package／enclosing namespace
    "wildcard",              # ワイルドカード import（`using N;`・`Imports N`）
    "qualified_name",        # 完全修飾名の一致
    "path_exact",            # `#include "相対パス"` の相対パスの完全一致
    "path_suffix",           # classpath 相対のパス末尾の一致
    "config_key_all",        # 同名の設定キー全件
    "schema_exact",          # schema を書いた参照が同じ schema の表に一致
    "schema_unqualified",    # schema を書いた参照が schema の記載が無い定義に一致
    "table_name",            # schema を書かない参照の表名の一致
    "nearest_name",          # 同じ最上位フォルダの最近傍の同名
    "di_qualifier",          # DI の注入先の実装: 名前の指定（`@Qualifier`／`@Named`／`@Resource(name=)`）が bean 名に一致
    "di_primary",            # DI の注入先の実装: `@Primary` の付いた実装
    "di_single_impl",        # DI の注入先の実装: 実装が 1 つだけ
    "copy_path_setting",     # 最近傍で決まらない COPY を、資料フォルダの設定の `copy_paths` の先頭から試して一意に決めた
    "path_alias_setting",    # 資料フォルダの設定の `path_aliases` で別名を prefix に置き換えた実在の相対パスに一致
    "name_rule_setting",     # 資料フォルダの設定の `name_rules` で参照の名前を置き換えた名前の定義に一致
    "dictionary_match",      # 資料の本文と定義名の辞書突合（言及）
    "dd_name_match",         # COBOL の `ASSIGN TO` の DD 名と、JCL の `DD` 文の DSN の一致
    "copy_item_name",        # 手続き部の名前が、COPY で取り込んだコピーブックの項目名に一致
    "build_definition",      # ビルドの定義に書かれた実行名と材料の組
    "undefined_table",       # 定義が資料フォルダに無い表（トップフォルダごとの Table のノード）
})


def _edge_source(e: dict) -> dict:
    """Pass 2 の候補 1 件 → 辺の根拠 1 件（`{via?, doc_id, file, line, rule?, from_def?}`）。辺が `via` を持たない（COBOL の COPY など）ときは `via` も持たない。"""
    src = {"doc_id": e["doc"], "file": e["doc"], "line": e["line"]}
    if e.get("via"):
        src = {"via": e["via"], **src}
    if e.get("_rule"):
        src["rule"] = e["_rule"]
    if e.get("_from_def") is not None:
        src["from_def"] = e["_from_def"]
    if e.get("_evidence_text"):
        src["evidence_text"] = e["_evidence_text"]
    return src


def _source_dedupe_key(src: dict) -> tuple:
    fd = src.get("from_def")
    return (src["doc_id"], src["line"], src.get("via"), src.get("rule"),
            (fd["file"], fd["key"]) if fd else None, src.get("locator"))


def _source_place_key(src: dict) -> tuple:
    """根拠の位置（`via` を除く）。"""
    fd = src.get("from_def")
    return (src["doc_id"], src["line"], src.get("rule"), (fd["file"], fd["key"]) if fd else None, src.get("locator"))


def _source_sort_key(src: dict) -> tuple:
    fd = src.get("from_def")
    return (analyzer_registry.via_priority_rank(src.get("via")), src["doc_id"], src["line"],
            src.get("rule") or "", (fd["file"], fd["key"] or "") if fd else ("", ""), src.get("locator") or "")


def cap_sources(sources: list, first: dict | None = None) -> tuple:
    """根拠を重複排除 `(doc_id, line, via, rule, from_def, locator)` し、`via` の優先順位→文書→行で並べて上限で切る。戻りは `(根拠, 超過件数)`。
    `first`（省略可）は辺の代表の根拠で、並びの先頭に置く（残りだけを並べる）。
    """
    seen: set = set()
    uniq = []
    for src in sources:
        k = _source_dedupe_key(src)
        if k not in seen:
            seen.add(k)
            uniq.append(src)
    # 注入（`inject`）は同じ位置の宣言型（`field_type`）の格上げ: 同じ位置に `inject` の根拠があれば `field_type` の根拠は畳む。
    injected = {_source_place_key(u) for u in uniq if u.get("via") == "inject"}
    if injected:
        uniq = [u for u in uniq if not (u.get("via") == "field_type" and _source_place_key(u) in injected)]
    uniq.sort(key=_source_sort_key)
    if first is not None:
        fk = _source_dedupe_key(first)
        uniq = [u for u in uniq if _source_dedupe_key(u) == fk] + [u for u in uniq if _source_dedupe_key(u) != fk]
    return uniq[:EDGE_SOURCES_MAX], max(0, len(uniq) - EDGE_SOURCES_MAX)


def aggregate_pass2_edges(raw_edges: list) -> list:
    """同一 `(src, type, dst)` の複数候補を 1 本へ集約し、候補の根拠を `sources` に並べる。

    代表（`doc`・`line`・`via` と `include_path` などの追加属性の持ち主）は、`via_priority_rank` が最小の候補（同順位は先着）。
    `sources` の先頭の件が代表の根拠で、残りは `cap_sources`（重複排除・並び・上限 20 件）。`(src,type,dst)` の初出順を保って返す。
    """
    groups: dict = {}
    order: list = []
    for e in raw_edges:
        key = (e["src"], e["type"], e["dst"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)
    out = []
    for key in order:
        cands = groups[key]
        rep = cands[0]
        for e in cands[1:]:
            if analyzer_registry.via_priority_rank(e.get("via")) < analyzer_registry.via_priority_rank(rep.get("via")):
                rep = e
        sources, overflow = cap_sources([_edge_source(e) for e in cands], first=_edge_source(rep))
        edge = {k: v for k, v in rep.items() if not k.startswith("_")}
        edge["sources"] = sources
        edge.update(merge_confirm(cands))
        if any(e.get("_binding_path") for e in cands):
            edge["binding_paths"] = sorted({e["_binding_path"] for e in cands if e.get("_binding_path")})
        if overflow:
            edge["sources_overflow_count"] = overflow
        out.append(edge)
    return out


UNRESOLVED_PER_FILE_MAX = 50       # 1 ファイルの未解決の申告を Neo4j のノードへ保存する上限（超過は `unresolved_overflow_count`）
NO_PRIMARY_LINES_MAX = 20          # `no_primary_definition` の `lines` の上限（超過は `lines_omitted`）


def is_unresolved_flag(f: dict, ref_rel: str) -> bool:
    """`_link` が積んだ未解決系の申告（理由を列挙せず形で判定する＝`from`・`kind`・`name`・`line` を持つ）。"""
    return f.get("from") == ref_rel and "kind" in f and "name" in f and "line" in f


def find_copy_cycles(nodes: dict, edges: list) -> list:
    """COPIES の辺（ファイル単位に畳む）から循環（強連結成分・2 パス以上）を検出し、含まれるパスの昇順の列を返す。"""
    graph: dict = defaultdict(set)
    for e in edges:
        if e.get("type") != "COPIES":
            continue
        a, b = nodes.get(e["src"]), nodes.get(e["dst"])
        if a is None or b is None or a["path"] == b["path"]:
            continue
        graph[a["path"]].add(b["path"])
        graph.setdefault(b["path"], set())
    index: dict = {}
    low: dict = {}
    on_stack: set = set()
    stack: list = []
    out: list = []
    counter = 0
    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(graph[w]))))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
    return sorted(out)


def plugin_failures_from_flags(flags) -> list:
    """`build_world` の `flags` から FW プラグインの失敗（`[{plugin, files, why}]`・プラグイン名順）を取り出す（`world_neo4j.load_world` が同じ世代で保存する）。"""
    return sorted(({"plugin": f["plugin"], "files": f.get("files"), "why": f.get("why")}
                   for f in flags if f.get("reason") == "plugin_failed" and f.get("plugin")),
                  key=lambda x: x["plugin"])


def unparsed_sources_from_flags(flags) -> dict:
    """`build_world` の `flags` から、グラフに入れなかったソースのファイル数 `{syntax, size_exceeded}` を取り出す（`world_neo4j.load_world` が同じ世代で保存する）。
    `syntax`＝落とした構文があり主体のノードも作れず、ファイルごとグラフに入らなかったもの（`build_world` が出す `source_not_registered`）、
    `size_exceeded`＝大きすぎて読まなかったファイル（`dropped_syntax` の `why`）。いずれも `from` で重複を除く。
    主体のノードを作った上で一部の構文だけ落とした申告（動的 URL・構文エラーの一部など）は数えない。取り込みの状態の通知も同じ関数を使う。
    """
    syntax: set = set()
    oversize: set = set()
    for f in flags:
        if f.get("reason") == "source_not_registered":
            syntax.add(f.get("from"))
        elif f.get("reason") == "dropped_syntax" and f.get("why") == "size_exceeded":
            oversize.add(f.get("from"))
    return {"syntax": len(syntax), "size_exceeded": len(oversize)}


BUILD_REPORT_REASON = "build_report"


def pop_build_report(flags: list) -> tuple:
    """`build_world` の `flags` から、ビルドの定義の読み取りの報告を取り出す。`(報告・無ければ空, 残りの flags)` を返す（報告は取り込みの注意に数えない）。"""
    report: dict = {}
    rest: list = []
    for f in flags:
        if f.get("reason") == BUILD_REPORT_REASON:
            report = f.get("report") or {}
        else:
            rest.append(f)
    return report, rest


def finalize(b: WorldBuild) -> None:
    """③ 解決済みの候補を 1 本へ集約して辺を確定し、COPY の循環・プラグインの失敗・未解決の申告を取り込み記録とノードへ載せる。"""
    # 同一 (src,type,dst) の複数候補を 1 本へ集約してから確定する（全 Pass2 エッジに適用・nodes/flags は不変）
    b.edges.extend(aggregate_pass2_edges(b.link_edges))

    # COPY のファイルをまたぐ循環（辺は残す）。取り込み記録（flags）だけに出す＝保存先のノードが 1 つに決まらない
    for cycle in find_copy_cycles(b.nodes, b.edges):
        b.flags.append({"reason": "copy_cycle", "paths": cycle})

    for pname in sorted(b.plugin_failed):                 # FW プラグインの失敗（グラフはそのプラグインの分だけ欠ける）
        rec = b.plugin_failed[pname]
        b.flags.append({"reason": "plugin_failed", "action": "warn", "plugin": pname, "from": rec["from"],
                        "files": len(rec["files"]), "why": rec["why"]})
    _attach_unresolved(b)


def _attach_unresolved(b: WorldBuild) -> None:
    """未解決の申告を、参照を書いたファイルの主体ノードへ載せる（`world_neo4j.load_world` が同じ tx で保存する）。"""
    for rel, items in b.unresolved_by_rel.items():
        items.sort(key=lambda it: (it["line"], it["reason"], it["kind"], it["name"]))
        node = b.nodes[b.unresolved_subject[rel]]
        # 上限で切るとき、プロジェクトの中に候補がありうる理由（ambiguous・cross_scope・unresolved_qualifier ほか）を先に残し、素の unresolved を後にする
        kept = (sorted(items, key=lambda it: (it["reason"] == "unresolved", it["reason"], it["line"], it["kind"], it["name"]))
                [:UNRESOLVED_PER_FILE_MAX] if len(items) > UNRESOLVED_PER_FILE_MAX else items)
        node["unresolved"] = sorted(kept, key=lambda it: (it["line"], it["reason"], it["kind"], it["name"]))
        node["unresolved_names"] = sorted({nm for it in items
                                           for nm in (it["name"], it["name"].rsplit(".", 1)[-1])})
        if len(items) > UNRESOLVED_PER_FILE_MAX:
            node["unresolved_overflow_count"] = len(items) - UNRESOLVED_PER_FILE_MAX
