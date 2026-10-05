"""別レンズ probe（薄い・read-only）。取り込み済み world のグラフ・文書・grep を再利用して、業務テーマを別レンズで問う。
設計: docs/design/chat.md「1ターンの流れ」
- トラブルシュート（`troubleshoot`）: 近傍（`INVOKES`/`RELATES_TO`/`DOCUMENTS` 含む）＋運用手順 grep → 原因候補カード。
- 仕様問い合わせ（`qa`）: grep（仕様書）→ 該当節を引用（doc_id＝rel_path＋span）。
厳密な依存影響は impact レンズ（impact_service／world_neo4j）。ここは近傍探索。範囲は world＋`scope_prefixes`（フォルダ prefix）。
"""
from __future__ import annotations

import logging
import re

from neo4j import Query
from neo4j.exceptions import Neo4jError

from . import citations, graph_coverage, scope
from .env_int import env_int
from .graph_coverage import Coverage
from .grep_tool import grep_search
from .impact_service import CATEGORY, plugin_failed_note
from .ingest.world_neo4j import (
    EDGE_SOURCE_FIELDS, EDGE_SOURCES_RETURN_DEFAULT, EDGE_SOURCES_RETURN_MAX, GraphQueryOverloadError, GraphSchemaEraError, _scope_pred,
    check_schema_era, edge_view, limit_edge_sources, read_plugin_failures, read_unresolved)

_log = logging.getLogger("sherpa")

# 近傍たどりのエッジ（実効語彙）。
_RELATED_REL = "COPIES|CONTAINS|INVOKES|ACCESSES|DOCUMENTS|CORRESPONDS_TO"

_ROLE = {"Document": "関連文書", "Module": "実装", "Batch": "ジョブ"}
_ROLE_RANK = {"Document": 1, "Batch": 2, "Module": 3}


# Neo4j の安全弁は timeout と緊急天井。Cypher に LIMIT は入れない（網羅性優先）。
# per-query タイムアウト（秒）。既定 30・[1,600]。
_NEO4J_QUERY_TIMEOUT_S = env_int("SHERPA_NEO4J_QUERY_TIMEOUT_S", 30, 1, 600)
# ストリーム反復の緊急天井（行数）。
_NEO4J_MAX_ROWS = 10000
# トラブルシュート/近傍探索（neo4j_related）の既定深さ（管理画面の基準値が未設定のときに使う）。
TROUBLESHOOT_GRAPH_DEPTH = 4
# 調べる深さ（`depth_profile.scaled_depth`）が加算後に一度だけ適用する絶対上限。
TROUBLESHOOT_GRAPH_DEPTH_ABS_MAX = 16

# タイムアウト由来のサーバエラーコードを緩く判定する（`code` に "timeout"/"timedout" を含むか・大小文字無視）。
_TIMEOUT_CODE_RE = re.compile(r"timedout|timeout", re.IGNORECASE)


def _is_query_timeout(exc: Neo4jError) -> bool:
    """Neo4j サーバエラーがクエリ/トランザクションのタイムアウトによるものか判定する。"""
    code = getattr(exc, "code", "") or ""
    return bool(_TIMEOUT_CODE_RE.search(str(code)))


def _run_capped(session, cypher: str, *, log_world: str, coverage: Coverage | None = None,
                stage: str | None = None, **params) -> list[dict]:
    """読み取り専用 Cypher を安全弁つきで実行する。
    ① `neo4j.Query(cypher, timeout=...)` で per-query タイムアウトを付ける。タイムアウトの `Neo4jError` は `log.warning` を出して空リストへ縮退する（他の `Neo4jError` は再送出）。
    ② 結果カーソルをストリーム反復し、`_NEO4J_MAX_ROWS` 行で打ち切る（`log.warning` を出す・収集済み分は返す）。
    ③ 打ち切りの `break` の前に `result.consume()` を呼ぶ（同じ session の次の `run()` が残りを全件バッファするのを防ぐ）。
    ④ ①②の縮退は、`coverage`（省略可）へ `timeout`／`row_cap`（`stage` つき）として申告する（空・部分結果を「近傍なし」「近傍の全部」と区別させる）。
    Cypher に LIMIT は入れない。
    """
    query = Query(cypher, timeout=_NEO4J_QUERY_TIMEOUT_S)
    try:
        result = session.run(query, **params)
        rows: list[dict] = []
        for i, record in enumerate(result):
            if i >= _NEO4J_MAX_ROWS:
                _log.warning("neo4j 読み取りが緊急天井 %d 行に達したため打ち切り（world=%s）",
                            _NEO4J_MAX_ROWS, log_world)
                result.consume()  # 残り未消費のまま返さない
                if coverage is not None:
                    coverage.add(graph_coverage.KIND_ROW_CAP, stage)
                break
            rows.append(record.data())
        return rows
    except Neo4jError as e:
        if _is_query_timeout(e):
            _log.warning("neo4j クエリがタイムアウト（%ss）のため空へ縮退（world=%s）: %s",
                        _NEO4J_QUERY_TIMEOUT_S, log_world, e)
            if coverage is not None:
                coverage.add(graph_coverage.KIND_TIMEOUT, stage)
            return []
        raise


def emit_result(type_: str, world: str, **payload) -> dict:
    """レンズ共通の出力エンベロープ。type は `impact`／`troubleshoot`／`qa`。"""
    if type_ not in ("impact", "troubleshoot", "qa", "doc_check"):
        raise ValueError(f"unknown result type: {type_}")
    return {"type": type_, "world": world, **payload}


def _truncated_search_note(doc_ids: list) -> str | None:
    """`grep_search(truncated_docs=...)`（cap で打ち切られた文書の doc_id）→ 利用者向け平文の注記 1 件。打切りが無ければ `None`（`notes` キーを作らない）。
    内部語彙（`file_truncated`／cap／バイト）は出さない。
    """
    if not doc_ids:
        return None
    if len(doc_ids) == 1:
        return f"「{doc_ids[0]}」は大きすぎて全体を検索できていません（先頭部分のみ）。"
    shown = "」「".join(doc_ids[:5])
    more = f" ほか{len(doc_ids) - 5}件" if len(doc_ids) > 5 else ""
    return f"次の資料は大きすぎて全体を検索できていません（先頭部分のみ）: 「{shown}」{more}"


def _terms(text: str) -> list[str]:
    """検索語を区切り＋汎用の助詞/問い掛け語で素朴に分割する（テーマ非依存）。"""
    import re
    sep = (r"[\s、。，．・/:：;；,.!?！？「」『』（）()\[\]【】〜~\-]"
           r"|について|という|とは|どう\w*|なに|なん\w*|教えて\w*|でしょうか|ですか"
           r"|から|まで|より|の|は|を|に|が|で|と|も|へ|や|か|ね|よ")
    parts = re.split(rf"(?:{sep})+", text or "")
    return [t for t in (p.strip() for p in parts) if len(t) >= 2]


def _public_grep(world, hits):
    """grep ヒットから API 露出用の根拠だけ残す（物理 `path` は出さない・整形は citations）。
    `text` は `excerpts.display_quote` で人間向け MD の該当節へ引き直す（取れなければ元の grep 本文）。
    """
    from . import excerpts
    out = []
    for h in hits:
        pub = citations.public_grep_hit(h)
        disp = excerpts.display_quote(world, pub["doc_id"], pub["text"], span=pub.get("span"))
        out.append(citations.with_display_text(
            pub, text=disp["quote"], excerpt_source=disp["excerpt_source"],
            locator_hint=disp["locator_hint"]))
    return out


def resolve_anchor(session, text, world, scope_prefixes=None, coverage: Coverage | None = None):
    """症状文 → グラフ上のアンカー `[(cid, name)]`（入力に現れるノード名で同定・範囲内）。"""
    rows = _run_capped(
        session,
        f"MATCH (n:Entity {{world_id:$w}}) WHERE {_scope_pred('n')} "
        "RETURN DISTINCT n.canonical_id AS cid, n.name AS name",
        log_world=world, w=world, prefixes=list(scope_prefixes or []),
        coverage=coverage, stage=graph_coverage.STAGE_ANCHOR,
    )
    low = (text or "").lower()
    return [(r["cid"], r["name"]) for r in rows
            if r["name"] and len(r["name"]) >= 2 and r["name"].lower() in low]


def neo4j_related(session, anchors, world, scope_prefixes=None, depth=TROUBLESHOOT_GRAPH_DEPTH, include_deprecated=False,
                  coverage: Coverage | None = None, sources_limit: int = EDGE_SOURCES_RETURN_DEFAULT):
    """アンカー近傍（厳密な影響ではない）。各近傍への最短経路を代表に返す（範囲内・無向探索・全エッジ型）。
    `edges` は各辺を `{type, from, to, doc, line, via, rule, sources, sources_overflow_count}` で返す（`from`/`to` はグラフ上の実際の向き・
    `via`・`rule`・`sources` は根拠のある辺だけ。`sources` は先頭 `sources_limit` 件で、切った分は `sources_overflow_count` に足す）。
    主クエリの後に `check_schema_era` を呼ぶ（旧世代の実データは `GraphSchemaEraError`）。`anchors` が空でも呼ぶ。
    時間切れ・行数の天井は `coverage`（省略可）へ申告する。深さの上限の先は判定しない（固定深さの近傍探索）。
    """
    def _plugin_failures():
        if coverage is not None:
            graph_coverage.attach_plugin_failures(
                coverage, lambda: read_plugin_failures(session, world), graph_coverage.STAGE_NEIGHBORS)

    if not anchors:
        check_schema_era(session, world, lens="troubleshoot")
        _plugin_failures()
        return []
    cy = (
        "MATCH (a:Entity) WHERE a.canonical_id IN $anchors "
        f"MATCH p=(a)-[r:{_RELATED_REL}*1..%(d)d]-(nb:Entity) "
        f"WHERE nb.world_id=$world AND nb.canonical_id <> a.canonical_id "
        f"  AND all(n IN nodes(p) WHERE n.world_id=$world AND {_scope_pred('n')}) "  # 経路全体を範囲内に（scope 外を経由させない）
        "  AND ($incl OR all(n IN nodes(p) WHERE coalesce(n.status,'active')='active')) "
        "  AND ($incl OR all(e IN relationships(p) WHERE coalesce(e.status,'active')='active')) "
        "WITH nb, p ORDER BY length(p) "
        "WITH nb, head(collect(p)) AS path "
        "RETURN nb.canonical_id AS cid, nb.name AS name, "
        "  [l IN labels(nb) WHERE l<>'Entity'][0] AS label, "
        "  coalesce(nb.status,'active') AS status, "
        "  [n IN nodes(path) | n.name] AS path_names, "
        f"  [e IN relationships(path) | {{type:type(e), from:startNode(e).name, to:endNode(e).name, doc:e.doc, line:e.line, {EDGE_SOURCE_FIELDS}}}] AS edges, "
        "  length(path) AS dist"
    ) % {"d": int(depth)}
    out = []
    rows = _run_capped(session, cy, log_world=world, anchors=list(anchors), world=world,
                       prefixes=list(scope_prefixes or []), incl=include_deprecated,
                       coverage=coverage, stage=graph_coverage.STAGE_NEIGHBORS)
    check_schema_era(session, world, lens="troubleshoot")
    _plugin_failures()
    for r in rows:
        out.append({
            "cid": r["cid"], "name": r["name"], "label": r["label"],
            "category": CATEGORY.get(r["label"], r["label"]),
            "status": r["status"],
            "path": r["path_names"], "distance": r["dist"],
            "edges": limit_edge_sources([edge_view(e) for e in r["edges"]], sources_limit),
        })
    return out


class _Cards(list):
    """`_troubleshoot_cards` が返すカードの `list`。`coverage`（`Coverage`）に、この読み取りで見つかった打ち切りを持つ。"""

    coverage: Coverage
    unresolved: dict | None = None   # 起点の名前に一致する未解決の参照（`neighbor_cards*` だけが付ける・`world_neo4j.read_unresolved` の形）

    def __init__(self, items, coverage: Coverage):
        super().__init__(items)
        self.coverage = coverage


def _troubleshoot_cards(session, symptom, world, depth=TROUBLESHOOT_GRAPH_DEPTH, include_deprecated=False, scope_paths=None,
                        sources_limit: int = EDGE_SOURCES_RETURN_DEFAULT):
    """症状 → 原因候補カード（内部専用・`cid` 付き）。`run_troubleshoot`（`cid` 除去）と `neighbor_cards`（agentic `graph_neighbors`・`cid` 保持）が共有する。
    戻り値を直接 API・会話保存・共有・JSON 書き出しへ渡さない（内部専用フィールドが漏れる）。
    戻り値は `(anchor_names, cards, truncated_docs)`。`truncated_docs` は `run_troubleshoot` が平文の注記にする。
    `cards` は `_Cards`（`.coverage`＝起点の解決・近傍の取得・文書の探索で見つかった打ち切り）。
    """
    sp = scope.normalize_scope_paths(scope_paths) or None
    coverage = Coverage()
    pairs = resolve_anchor(session, symptom, world, sp, coverage=coverage)
    anchors = [c for c, _n in pairs]
    anchor_names = {n for _c, n in pairs}
    related = neo4j_related(session, anchors, world, sp, depth, include_deprecated, coverage=coverage,
                            sources_limit=sources_limit)

    grep_by_doc: dict[str, list] = {}
    truncated_docs: list = []
    for nm in anchor_names:
        for h in grep_search(nm, world, scope_paths=sp, truncated_docs=truncated_docs):
            grep_by_doc.setdefault(h["doc_id"], []).append(h)

    cards: list[dict] = []
    covered_docs: set[str] = set()
    for nb in related:
        if nb["label"] == "DataItem":  # 末端の項目は粒度が細かすぎる（影響レンズで見る）
            continue
        card_docs = {e.get("doc") for e in nb["edges"] if e.get("doc")}  # この候補の来歴 rel_path
        gh = [h for d in card_docs for h in grep_by_doc.get(d, [])]  # grep 根拠は rel_path で結合
        covered_docs |= {d for d in card_docs if grep_by_doc.get(d)}
        cards.append({
            "name": nb["name"], "label": nb["label"], "category": nb["category"],
            "role": _ROLE.get(nb["label"], "近傍"), "distance": nb["distance"], "path": nb["path"],
            "source": "both" if gh else "graph",
            "evidence": {"edges": nb["edges"], "grep": _public_grep(world, gh)},
            "cid": nb["cid"],  # 内部専用: Neo4j canonical_id
                                # 同名 label/name の別ノードを区別する機械キー。公開経路（`run_troubleshoot`）は返す前に除去する。
        })
    for doc_id, gh in grep_by_doc.items():  # グラフに無いが grep だけで出た文書（運用手順など）
        if doc_id in covered_docs:
            continue
        cards.append({
            "name": doc_id, "label": "Document", "category": CATEGORY["Document"],
            "role": _ROLE["Document"], "distance": None, "path": [],
            "source": "grep", "evidence": {"edges": [], "grep": _public_grep(world, gh)},
        })

    cards.sort(key=lambda c: (_ROLE_RANK.get(c["label"], 9),
                              c["distance"] if c["distance"] is not None else 99, c["name"]))
    cards = scope.filter_items(cards, sp)
    if truncated_docs:
        coverage.add(graph_coverage.KIND_DOC_SEARCH_TRUNCATED, graph_coverage.STAGE_DOCS)
    return anchor_names, _Cards(cards, coverage), truncated_docs


_GRAPH_LIMIT_NOTES = {
    (graph_coverage.KIND_TIMEOUT, graph_coverage.STAGE_ANCHOR):
        "症状に含まれる名前を関係グラフから探す検索が時間内に終わらず、起点を調べきれていません"
        "（結果が空・一部のみの可能性があります）。範囲（フォルダ）を絞って再実行してください。",
    (graph_coverage.KIND_TIMEOUT, graph_coverage.STAGE_NEIGHBORS):
        "関係グラフのつながりを調べる検索が時間内に終わらず、調べきれていません"
        "（結果が空・一部のみの可能性があります）。範囲（フォルダ）を絞って再実行してください。",
    (graph_coverage.KIND_ROW_CAP, graph_coverage.STAGE_ANCHOR):
        "症状に含まれる名前を関係グラフから探す検索が件数の上限に達し、起点の一部しか調べられていません。"
        "範囲（フォルダ）を絞って再実行してください。",
    (graph_coverage.KIND_ROW_CAP, graph_coverage.STAGE_NEIGHBORS):
        "関係グラフのつながりを調べる検索が件数の上限に達し、一部しか調べられていません。"
        "範囲（フォルダ）を絞って再実行してください。",
}


def graph_limit_notes(coverage: Coverage) -> list[str]:
    """`coverage` の `timeout`／`row_cap`／`plugin_failed` → 利用者向け平文の注記（段階ごとに 1 件・`plugin_failed` は全プラグインで 1 件）。文書探索の打ち切りは `_truncated_search_note` が別に出す。"""
    notes = [_GRAPH_LIMIT_NOTES[(lim["kind"], lim.get("stage"))] for lim in coverage.limits
             if (lim["kind"], lim.get("stage")) in _GRAPH_LIMIT_NOTES]
    plugin_note = plugin_failed_note({"limits": coverage.limits})
    return notes + [plugin_note] if plugin_note else notes


def run_troubleshoot(session, symptom, world, depth=TROUBLESHOOT_GRAPH_DEPTH, include_deprecated=False, scope_paths=None):
    """症状 → 原因候補カード（近傍グラフ＋運用手順 grep・根拠つき）。範囲で grep と候補を絞る。
    公開経路のため内部専用 `cid` は含まない（`cid` 付きは `neighbor_cards`）。
    `coverage`（`graph_coverage` の欄）で、時間切れ（空）・行数の天井（部分結果）・文書探索の打ち切りを「近傍なし」と区別して返す。
    打ち切りがあれば `notes`（平文の注記）を添える。
    """
    anchor_names, cards, truncated_docs = _troubleshoot_cards(session, symptom, world, depth, include_deprecated, scope_paths)
    public_cards = [{k: v for k, v in c.items() if k != "cid"} for c in cards]
    coverage = getattr(cards, "coverage", None) or Coverage()
    result = emit_result("troubleshoot", world, symptom=symptom,
                         anchors=sorted(anchor_names), candidates=public_cards,
                         coverage=coverage.as_dict())
    notes = [n for n in (_truncated_search_note(truncated_docs), *graph_limit_notes(coverage)) if n]
    if notes:
        result["notes"] = notes
    return result


def _attach_unresolved(session, cards, world, anchor_names, scope_prefixes) -> None:
    """起点（アンカー）の名前に一致する未解決の参照を `cards.unresolved` に付ける（名前は小文字で比べる＝近傍の起点の解決と同じ）。

    起点の解決・近傍の取得が時間切れ・件数の天井だったときは付けない（起点が不完全で、返す一覧を全部と読ませないため）。
    この読み取り自体の時間切れ・天井は近傍の取得の打ち切りとして申告する。
    """
    if cards.coverage.has(graph_coverage.KIND_TIMEOUT, graph_coverage.KIND_ROW_CAP):
        return
    try:
        cards.unresolved = read_unresolved(session, world, sorted(anchor_names), scope_prefixes, fold_case=True)
    except GraphQueryOverloadError as e:
        cards.coverage.add(graph_coverage.kind_of_overload(e.reason), graph_coverage.STAGE_NEIGHBORS)


class NeighborCardsFailure(list):
    """`neighbor_cards` が障害を捕捉したときだけ返す空 `list` のサブクラス。`error_code` 属性で通常の空リストと区別する。"""

    def __init__(self, error_code: str):
        super().__init__()
        self.error_code = error_code


def neighbor_cards(world, term, scope_paths=None) -> list:
    """関係グラフの近傍カード（原因候補）を返す。agentic ツール `graph_neighbors` 専用で、自前で Neo4j セッションを開いて `_troubleshoot_cards` を呼ぶ（`cid` 付き）。
    戻り値のカードは `.coverage`（`Coverage`）を持つ（時間切れ・行数の天井・文書探索の打ち切り）。
    Neo4j 不可・未解決は `[]`。捕捉した障害は `NeighborCardsFailure`（`error_code`）で返す:
    接続系（`DriverError`/`TransientError`）は `"graph_unavailable"`（回復可能）、それ以外は `"graph_internal_error"`（回復不可）。型名だけログに残す。
    `GraphSchemaEraError` だけは再送出する（呼び出し元が `graph_reingest_required` に変換）。
    """
    if not (term or "").strip():
        return []
    driver = None
    try:
        from neo4j import GraphDatabase

        from .ingest import world_neo4j
        env = world_neo4j._env()
        driver = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
        with driver.session() as s:
            # 3 件目（truncated_docs）は無視（agentic 経路は `ripgrep_search` が別途申告する）。
            anchor_names, cards, _truncated_docs = _troubleshoot_cards(
                s, term, world, scope_paths=scope_paths, sources_limit=EDGE_SOURCES_RETURN_MAX)
            _attach_unresolved(s, cards, world, anchor_names, scope.normalize_scope_paths(scope_paths) or None)
        return cards
    except GraphSchemaEraError:
        raise
    except Exception as exc:
        from neo4j.exceptions import ConfigurationError, DriverError, TransientError
        # `ConfigurationError` は `DriverError` のサブクラスだが回復不可として扱う。
        recoverable = not isinstance(exc, ConfigurationError) and isinstance(exc, (DriverError, TransientError))
        _log.warning("lens_service: neighbor_cards 取得に失敗（回復%s）: %s errno=%s",
                    "可" if recoverable else "不可", type(exc).__name__, getattr(exc, "errno", None))
        return NeighborCardsFailure("graph_unavailable" if recoverable else "graph_internal_error")
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass


def _resolve_anchor_by_name(session, term, world, scope_prefixes=None, coverage: Coverage | None = None):
    """起点をグラフから名前の一致で直接引く（素の Codex モード専用）。完全一致が 0 件のときだけ大文字小文字無視の一致を試す。範囲の述語は `resolve_anchor` と同じ。"""
    sp = list(scope_prefixes or [])
    rows = _run_capped(
        session,
        f"MATCH (n:Entity {{world_id:$w}}) WHERE {_scope_pred('n')} AND n.name = $term "
        "RETURN DISTINCT n.canonical_id AS cid, n.name AS name",
        log_world=world, w=world, prefixes=sp, term=term,
        coverage=coverage, stage=graph_coverage.STAGE_ANCHOR,
    )
    if not rows:
        rows = _run_capped(
            session,
            f"MATCH (n:Entity {{world_id:$w}}) WHERE {_scope_pred('n')} AND toLower(n.name) = toLower($term) "
            "RETURN DISTINCT n.canonical_id AS cid, n.name AS name",
            log_world=world, w=world, prefixes=sp, term=term,
            coverage=coverage, stage=graph_coverage.STAGE_ANCHOR,
        )
    return [(r["cid"], r["name"]) for r in rows]


def neighbor_cards_graph_only(world, term, scope_paths=None) -> list:
    """`neighbor_cards` のグラフだけ版（素の Codex モード）。grep はせず、起点は `_resolve_anchor_by_name`、近傍は `neo4j_related`。
    `evidence.grep` は常に空・`source` は常に `"graph"`。戻り値・失敗の扱いは `neighbor_cards` と同じ（`GraphSchemaEraError` は再送出）。
    """
    if not (term or "").strip():
        return []
    driver = None
    try:
        from neo4j import GraphDatabase

        from .ingest import world_neo4j
        env = world_neo4j._env()
        driver = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
        sp = scope.normalize_scope_paths(scope_paths) or None
        with driver.session() as s:
            coverage = Coverage()
            pairs = _resolve_anchor_by_name(s, term, world, sp, coverage=coverage)
            anchors = [c for c, _n in pairs]
            related = neo4j_related(s, anchors, world, sp, coverage=coverage, sources_limit=EDGE_SOURCES_RETURN_MAX)
            probe = _Cards([], coverage)
            _attach_unresolved(s, probe, world, [n for _c, n in pairs], sp)
            unresolved = probe.unresolved
        cards: list[dict] = []
        for nb in related:
            if nb["label"] == "DataItem":  # 末端の項目は粒度が細かすぎる（影響レンズで見る）
                continue
            cards.append({
                "name": nb["name"], "label": nb["label"], "category": nb["category"],
                "role": _ROLE.get(nb["label"], "近傍"), "distance": nb["distance"], "path": nb["path"],
                "source": "graph",
                "evidence": {"edges": nb["edges"], "grep": []},
                "cid": nb["cid"],
            })
        cards.sort(key=lambda c: (_ROLE_RANK.get(c["label"], 9),
                                  c["distance"] if c["distance"] is not None else 99, c["name"]))
        result = _Cards(scope.filter_items(cards, sp), coverage)
        result.unresolved = unresolved
        return result
    except GraphSchemaEraError:
        raise
    except Exception as exc:
        from neo4j.exceptions import ConfigurationError, DriverError, TransientError
        recoverable = not isinstance(exc, ConfigurationError) and isinstance(exc, (DriverError, TransientError))
        _log.warning("lens_service: neighbor_cards_graph_only 取得に失敗（回復%s）: %s errno=%s",
                    "可" if recoverable else "不可", type(exc).__name__, getattr(exc, "errno", None))
        return NeighborCardsFailure("graph_unavailable" if recoverable else "graph_internal_error")
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass


def run_qa(question, world, max_hits=20, scope_paths=None, layer=None):
    """検索語 → 仕様書の該当節を引用（doc_id＝rel_path＋span）。範囲（フォルダ prefix）で引用源を絞る。
    `layer`（省略可・`None`＝`"both"`）は探す対象で、`grep_search` へそのまま渡す。
    打ち切られた文書があれば `notes`（平文の注記 1 件）を添える。
    """
    sp = scope.normalize_scope_paths(scope_paths) or None
    truncated_docs: list = []
    hits = grep_search(question, world, max_hits=max_hits, scope_paths=sp, layer=layer,
                       truncated_docs=truncated_docs)
    if not hits:  # 自然文 → 語に分割し具体的な語から試す
        for t in sorted(_terms(question), key=len, reverse=True):
            found, seen = [], set()
            for h in grep_search(t, world, max_hits=max_hits, scope_paths=sp, layer=layer,
                                 truncated_docs=truncated_docs):
                key = (h["doc_id"], tuple(h["span"]))
                if key not in seen:
                    seen.add(key)
                    found.append(h)
            if found:
                hits = found
                break
    # quote を人間向け MD の該当節へ引き直す（match/ext は維持）。取れなければ grep 本文のまま。
    from . import excerpts
    cites = []
    for h in hits:
        c = citations.from_grep_hit(h)
        disp = excerpts.display_quote(world, h["doc_id"], c["quote"], span=h.get("span"))
        cites.append(citations.with_display_excerpt(
            c, quote=disp["quote"], excerpt_source=disp["excerpt_source"],
            locator_hint=disp["locator_hint"]))
    result = emit_result("qa", world, question=question,
                         answered=bool(cites), citations=cites)
    note = _truncated_search_note(truncated_docs)
    if note:
        result["notes"] = [note]
    return result
