"""資料フォルダのグラフを Neo4j へロードし、範囲フィルタ付きで影響をたどる。

`world_graph.build_world` が返す dict のノード/エッジ（パス同一性・検索スコープ用メタデータ
`world_id/top_scope/phase/category/path`）をそのまま Neo4j へ MERGE する。
影響 Cypher は `world_id` とフォルダ prefix（`scope_prefixes`）で絞り、どの階層でも1つの資料フォルダとしてたどる。
語彙（label/edge）は閉じているため Cypher へ直埋めする（allowlist＝`NODE_LABELS`/`EDGE_TYPES`）。
設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re

from neo4j import Query
from neo4j.exceptions import Neo4jError

from .. import worlds                                # 資料フォルダの root 解決（重要度の解決に使う）
from ..env_int import env_int
from ..impact_service import CATEGORY                # 種別→結果カテゴリ
from . import importance                             # 文書の重要度（`_重要度.txt`）
from .model import EDGE_TYPES, NODE_LABELS           # 閉じた語彙（Cypher 直埋めの allowlist）

_log = logging.getLogger("sherpa")

# 許容エッジ＝標準語彙＋対応エッジ `CORRESPONDS_TO`
WORLD_EDGE_TYPES = EDGE_TYPES | {"CORRESPONDS_TO"}

# 影響たどりで辿るのは構造（コード依存）のエッジだけ。対応エッジ `CORRESPONDS_TO` と添付 `DOCUMENTS`（言及エッジ含む）は辿らない
_IMPACT_REL = "COPIES|CONTAINS|INVOKES|ACCESSES"

WORLD_CONSTRAINTS = [
    "CREATE CONSTRAINT canon IF NOT EXISTS FOR (n:Entity) REQUIRE n.canonical_id IS UNIQUE",
    "CREATE INDEX ent_world IF NOT EXISTS FOR (n:Entity) ON (n.world_id)",
    "CREATE INDEX ent_name IF NOT EXISTS FOR (n:Entity) ON (n.name)",
    # 資料フォルダごとのスキーマ世代スタンプ（`GRAPH_SCHEMA_ERA`）を持つメタノード。`:Entity` とは別ラベルなので `DETACH DELETE` の対象外（別途 MERGE/DELETE する）
    "CREATE CONSTRAINT sherpa_meta_world IF NOT EXISTS FOR (m:SherpaMeta) REQUIRE m.world_id IS UNIQUE",
]


def _compute_graph_schema_era() -> str:
    """`GRAPH_SCHEMA_ERA` を合成する（sha256 先頭12桁・決定的）。

    材料はコードアナライザの分類契約版・アナライザ登録簿の構成署名・言及エッジ突合の仕様版・グラフ語彙。
    いずれかが変わると値も変わり、再取り込み前の読取ゲート（`check_schema_era`）が古いグラフを止める。
    原本の変更で動く `ingest.worker._sig`（last_sig）とは別物で、保存済みグラフを読んでよい前提が崩れたときだけ動く。
    材料モジュールは循環 import を避けるため関数内で import し、呼び出しは module import 時の1回だけ。
    """
    from .analyzers.registry import CODE_ANALYZERS_SCHEMA_VERSION, config_signature
    from .world_graph import MENTION_SCHEMA_VERSION
    material = repr((CODE_ANALYZERS_SCHEMA_VERSION, config_signature(), MENTION_SCHEMA_VERSION,
                     tuple(sorted(NODE_LABELS)), tuple(sorted(EDGE_TYPES))))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


GRAPH_SCHEMA_ERA = _compute_graph_schema_era()

# 検索スコープ（フォルダ prefix）述語。`$prefixes` が空なら全体。ノードは全て `path` を持つので `path` の prefix 一致だけで判定する
def _scope_pred(var: str) -> str:
    return (f"(size($prefixes)=0 OR any(pref IN $prefixes WHERE "
            f"{var}.path IS NOT NULL AND ({var}.path=pref OR {var}.path STARTS WITH pref+'/')))")


# 影響分析の Neo4j 安全弁: per-query timeout・ストリーム反復・緊急天井（Cypher に LIMIT は入れない）。
# timeout・天井到達のどちらも `GraphQueryOverloadError` を必ず raise し、部分結果や空を黙って返さない
# （空は「影響なし」と誤読されるため。呼び出し側の `impact_service`/`routers/impact.py`/`chat_service` が利用者へ見せる）


class GraphQueryOverloadError(RuntimeError):
    """Neo4j 読み取りクエリが安全弁（timeout／緊急天井）で打ち切られたことを示す。

    `reason` は `"timeout"` または `"too_many_rows"`。`world` は対象 world_id、`rows` は天井到達時の行数上限（timeout は None）。
    影響分析はこの例外を空や部分結果へ握り潰さず、利用者へ「範囲を絞って再実行」と伝える。
    """

    def __init__(self, reason: str, *, world: str, rows: int | None = None):
        self.reason = reason
        self.world = world
        self.rows = rows
        detail = f" rows>={rows}" if rows is not None else ""
        super().__init__(f"neo4j query overload ({reason}): world={world}{detail}")


# 利用者向けの平文メッセージ（`routers/impact.py` の 503 と `chat_service` が共有する）
GRAPH_OVERLOAD_USER_MESSAGE = (
    "対象が大きすぎるか、グラフ検索が時間内に終わりませんでした。範囲（フォルダ）を絞って再実行してください。"
)


# グラフを構築した時のスキーマ世代（`GRAPH_SCHEMA_ERA`）を Neo4j 側へ保存し（`load_world`）、現行コードの世代と異なる場合だけ
# （内部形式が変わったのに再取り込みが済んでいない場合だけ）読み取りを明示エラーにする。last_sig の不一致は正常運転なので使わない
class GraphSchemaEraError(RuntimeError):
    """保存済みグラフのスキーマ世代が現行コードと不一致（旧世代の実データを読んでいる）ことを示す。

    `stored_era` は保存されていた世代（`None` は世代スタンプのない旧グラフ）。`lens` は分かる範囲でのチャットレンズ名（"impact"/"troubleshoot"）。
    `GraphQueryOverloadError` と同じく、呼び出し側は空や部分結果にせず「再取り込みが必要」と利用者へ伝える。
    """

    def __init__(self, world: str, stored_era: str | None, *, lens: str | None = None):
        self.world = world
        self.stored_era = stored_era
        self.lens = lens
        super().__init__(
            f"graph schema era mismatch: world={world} stored={stored_era!r} current={GRAPH_SCHEMA_ERA!r}")


# 利用者向けの平文メッセージ（`routers/impact.py`・`routers/graph.py` の 503 と `chat_service` が共有する）
GRAPH_SCHEMA_ERA_USER_MESSAGE = (
    "この資料フォルダの検索用データが古い内部形式のままです（内部形式が更新されました）。"
    "管理者に『今すぐ更新』を依頼してください。"
)


def check_schema_era(session, world: str, *, lens: str | None = None) -> None:
    """資料フォルダの保存済みスキーマ世代を確認する（不一致なら `GraphSchemaEraError`）。

    実データ（`world_id` を持つ `:Entity`）のない資料フォルダは対象外。実データがあり、保存世代
    （`:SherpaMeta{world_id}.schema_era`・未保存を含む）が現行 `GRAPH_SCHEMA_ERA` と違うときだけ raise する。
    グラフ読み取りの入口（`world_impact`/`resolve_world_entity`/`lens_service.neo4j_related`/`graph_admin.graph_search`）が
    主クエリの後に1回ずつ呼ぶ。存在確認は `LIMIT 1` で打ち切る。
    世代プローブが `Neo4jError` で失敗したときは警告ログを残して re-raise する（黙って戻ると旧世代の検知が無効になる）。
    """
    try:
        rows = session.run(
            "OPTIONAL MATCH (n:Entity {world_id:$w}) WITH n LIMIT 1 "
            "WITH count(n) AS c "
            "OPTIONAL MATCH (m:SherpaMeta {world_id:$w}) "
            "RETURN c, m.schema_era AS era",
            w=world,
        ).data()
    except Neo4jError:
        _log.warning("check_schema_era: 世代プローブが失敗しました（world=%s lens=%s）",
                    world, lens, exc_info=True)
        raise
    row = rows[0] if rows else None
    if not row or not row.get("c"):
        return
    era = row.get("era")
    if era != GRAPH_SCHEMA_ERA:
        raise GraphSchemaEraError(world, era, lens=lens)


def world_graph_is_empty(session, world: str) -> bool:
    """資料フォルダに `:Entity` が1件も無い（未構築）か。`Neo4jError` はそのまま送出する。"""
    rows = session.run(
        "OPTIONAL MATCH (n:Entity {world_id:$w}) WITH n LIMIT 1 RETURN count(n) AS c",
        w=world,
    ).data()
    row = rows[0] if rows else None
    return not (row and row.get("c"))


# per-query タイムアウト（秒）。既定30・[1,600]。`lens_service` と同じ env 変数を共用する
_NEO4J_QUERY_TIMEOUT_S = env_int("SHERPA_NEO4J_QUERY_TIMEOUT_S", 30, 1, 600)
# ストリーム反復の緊急天井（行数）
_NEO4J_MAX_ROWS = 10000
# 影響たどり（`world_impact`）の既定深さ。`impact_service.run_impact`／`fused_search._search_graph` の既定でもある
IMPACT_MAX_DEPTH = 10

# `load_world` のノード/エッジ投入を UNWIND バッチへ分ける行数。
# 1回の UNWIND に全件を積むとドライバの直列化ピークが全体サイズに比例するため分ける（原子性はバッチ数に依存しない）
_NEO4J_BATCH_ROWS = 5000


def _batched(seq: list, n: int):
    """`seq` を `n` 件ずつのリストへ分割する（最後だけ短い）。バッチ UNWIND の共通ヘルパー。"""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# タイムアウト由来のサーバエラーコードを緩く判定する（`lens_service` と同じ判定。例: `Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration`）
_TIMEOUT_CODE_RE = re.compile(r"timedout|timeout", re.IGNORECASE)


def _is_query_timeout(exc: Neo4jError) -> bool:
    """Neo4j サーバエラーが**クエリ/トランザクションのタイムアウト**によるものか判定する。"""
    code = getattr(exc, "code", "") or ""
    return bool(_TIMEOUT_CODE_RE.search(str(code)))


def _run_read_capped(session, cypher: str, *, world: str, **params) -> list[dict]:
    """読み取り専用 Cypher を安全弁つきで実行する（影響分析向け）。

    `neo4j.Query(cypher, timeout=...)` の per-query タイムアウト・ストリーム反復・`_NEO4J_MAX_ROWS` の緊急天井で実行する
    （Cypher に LIMIT は入れない）。timeout・天井到達のどちらも空/部分結果にせず `GraphQueryOverloadError` を raise する。
    `world` はログ/例外用で `session.run` へも渡す（参照しない Cypher では無視される）。
    天井到達時は raise の前に `result.consume()` を呼び、未消費の Result を残さない（同じ session の次クエリで全件バッファされるため）。
    """
    query = Query(cypher, timeout=_NEO4J_QUERY_TIMEOUT_S)
    try:
        result = session.run(query, world=world, **params)
        rows: list[dict] = []
        for i, record in enumerate(result):
            if i >= _NEO4J_MAX_ROWS:
                _log.warning("neo4j 読み取りが緊急天井 %d 行に達したため打ち切り（fail-loud・world=%s）",
                            _NEO4J_MAX_ROWS, world)
                result.consume()   # raise 前に残り未消費分を破棄（次クエリでの全件バッファ逆流を防ぐ）
                raise GraphQueryOverloadError("too_many_rows", world=world, rows=_NEO4J_MAX_ROWS)
            rows.append(record.data())
        return rows
    except Neo4jError as e:
        if _is_query_timeout(e):
            _log.warning("neo4j クエリがタイムアウト（%ss・fail-loud・world=%s）: %s",
                        _NEO4J_QUERY_TIMEOUT_S, world, e)
            raise GraphQueryOverloadError("timeout", world=world) from e
        raise


def _sources_json(sources) -> str | None:
    """entity/relation の出所（`chunk_id`/`locator`/`logical_record_id` を持つ dict のリスト）を、
    Neo4j のプロパティに持てる1本の JSON 文字列にする。空/無ければ None（プロパティを立てない）。"""
    if not sources:
        return None
    return json.dumps(sources, ensure_ascii=False)


def _node_row(n: dict) -> dict:
    """1ノード分の UNWIND 行（`world_id` はクエリの `$world` 側に出す。`sources` は JSON 文字列化済み）。

    `jcl_kind` は JCL の `Batch` 種別（`job`/`proc`/`include`）で、JCL 以外では `None`。
    """
    return {
        "cid": n["cid"], "name": n["name"],
        "top": n.get("top_scope"), "phase": n.get("phase"), "cat": n.get("category"),
        "path": n.get("path"), "sp": n.get("scope_path"), "value": n.get("value"),
        "status": n.get("status", "active"),
        "analyzer": n.get("analyzer"),
        "sources": _sources_json(n.get("sources")),
        "sources_overflow": n.get("sources_overflow_count", 0),
        "jcl_kind": n.get("jcl_kind"),
    }


def _edge_row(e: dict) -> dict:
    """1エッジ分の UNWIND 行。`via` は `RefCandidate.extra["via"]` 由来。"""
    return {
        "src": e["src"], "dst": e["dst"], "doc": e.get("doc", ""),
        "line": e.get("line", 0),
        "status": e.get("status", "active"),
        "source": e.get("source"), "evidence": e.get("evidence"), "rule": e.get("rule"),
        "sources": _sources_json(e.get("sources")),
        "sources_overflow": e.get("sources_overflow_count", 0),
        "via": e.get("via"),
    }


def load_world(nodes, edges, world_id, uri, user, password):
    """資料フォルダのグラフ（dict）を Neo4j へクリーン rebuild する（削除→全ロードを1つの write tx で行う）。

    ① schema コマンドはデータ tx と混ぜられないので先に流す。
    ② 語彙を閉じた集合で検証し、ラベル/エッジ型ごとにまとめる。
    ③ 1つの write tx の中で、当該 `world_id` を削除して `_NEO4J_BATCH_ROWS` 件ずつ UNWIND バッチで再ロードする
       （途中で失敗すれば tx 全体がロールバックし旧グラフが残る。複数 tx にしない＝半分削除・半分再構築を並行の影響検索に見せないため）。
    ④ 同じ tx の最後に `GRAPH_SCHEMA_ERA` と実物件数（`:SherpaMeta{world_id}`）を刻む。
    `sources`（出所リスト）は JSON 文字列として `x.sources`/`r.sources` に、上限超過件数は `sources_overflow_count` に載せる。
    """
    from neo4j import GraphDatabase  # 遅延 import（解析だけなら不要）

    # label/edge type は Cypher に直埋めするため、書込 tx の前に閉じた語彙で検証する
    bad_n = {n["label"] for n in nodes if n["label"] not in NODE_LABELS}
    bad_e = {e["type"] for e in edges if e["type"] not in WORLD_EDGE_TYPES}
    if bad_n or bad_e:
        raise ValueError(f"未知の語彙はロードしない: labels={sorted(bad_n)} edges={sorted(bad_e)}")

    # ラベル/エッジ型ごとにまとめる（ラベル名は Cypher でパラメータ化できないためグループ単位で UNWIND する）
    nodes_by_label: dict[str, list] = {}
    for n in nodes:
        nodes_by_label.setdefault(n["label"], []).append(n)
    edges_by_type: dict[str, list] = {}
    for e in edges:
        edges_by_type.setdefault(e["type"], []).append(e)

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as s:
            for c in WORLD_CONSTRAINTS:
                s.run(c)
            def _apply(tx):
                tx.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=world_id)
                for label, items in nodes_by_label.items():
                    node_cypher = (
                        "UNWIND $rows AS row "
                        f"MERGE (x:Entity {{canonical_id: row.cid}}) "
                        f"SET x:`{label}`, x.name=row.name, x.world_id=$world, "
                        f"x.top_scope=row.top, x.phase=row.phase, x.category=row.cat, x.path=row.path, "
                        f"x.scope_path=row.sp, x.value=row.value, "
                        f"x.status=row.status, x.analyzer=row.analyzer, x.sources=row.sources, "
                        f"x.sources_overflow_count=row.sources_overflow, x.jcl_kind=row.jcl_kind"
                    )
                    for batch in _batched(items, _NEO4J_BATCH_ROWS):
                        rows = [_node_row(n) for n in batch]
                        tx.run(node_cypher, rows=rows, world=world_id)
                for etype, items in edges_by_type.items():
                    edge_cypher = (
                        "UNWIND $rows AS row "
                        "MATCH (a:Entity {canonical_id: row.src}), (b:Entity {canonical_id: row.dst}) "
                        f"MERGE (a)-[r:`{etype}`]->(b) "
                        "SET r.world_id=$world, r.doc=row.doc, r.line=row.line, "
                        "r.status=row.status, "
                        "r.source=row.source, r.evidence=row.evidence, r.rule=row.rule, "
                        "r.sources=row.sources, r.sources_overflow_count=row.sources_overflow, "
                        "r.via=row.via"
                    )
                    for batch in _batched(items, _NEO4J_BATCH_ROWS):
                        rows = [_edge_row(e) for e in batch]
                        tx.run(edge_cypher, rows=rows, world=world_id)
                # 世代スタンプと実物件数を同一 tx で刻む（投入後の実物を数える＝`check_graph_counts` と同じ数え方）
                node_count = tx.run(
                    "MATCH (n:Entity {world_id:$w}) RETURN count(n) AS c", w=world_id
                ).single()["c"]
                edge_count = tx.run(
                    "MATCH (a:Entity {world_id:$w})-[r]->() WHERE r.world_id=$w RETURN count(r) AS c",
                    w=world_id,
                ).single()["c"]
                tx.run(
                    "MERGE (m:SherpaMeta {world_id:$w}) "
                    "SET m.schema_era=$era, m.node_count=$nc, m.edge_count=$ec",
                    w=world_id, era=GRAPH_SCHEMA_ERA, nc=node_count, ec=edge_count)
                return len(nodes), len(edges)
            return s.execute_write(_apply)
    finally:
        driver.close()


def check_graph_counts(world_id, uri, user, password) -> str | None:
    """保存済み `:SherpaMeta` の世代・件数スタンプを実際のグラフ内容と照合する（`load_world` が刻む値と対）。

    作り直しが要る理由コードを返す。整合していれば `None`。
    - `"no_stamp"`: `node_count`/`edge_count` が無い（`:SherpaMeta` 自体が無い場合を含む）
    - `"era_mismatch"`: 保存済み `schema_era` が現行 `GRAPH_SCHEMA_ERA` と不一致
    - `"count_mismatch"`: スタンプ済み件数と実物（`:Entity{world_id}` の数・その資料フォルダのノードから出る `r.world_id` 一致リレーションの数）が違う
    ドライバは自前で開閉し、接続/クエリ例外はそのまま呼び出し元へ伝播する。
    """
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as s:
            rows = s.run(
                "MATCH (m:SherpaMeta {world_id:$w}) "
                "RETURN m.schema_era AS era, m.node_count AS nc, m.edge_count AS ec",
                w=world_id,
            ).data()
            meta = rows[0] if rows else None
            if not meta or meta.get("nc") is None or meta.get("ec") is None:
                return "no_stamp"
            if meta.get("era") != GRAPH_SCHEMA_ERA:
                return "era_mismatch"
            actual_nc = s.run(
                "MATCH (n:Entity {world_id:$w}) RETURN count(n) AS c", w=world_id
            ).single()["c"]
            actual_ec = s.run(
                "MATCH (a:Entity {world_id:$w})-[r]->() WHERE r.world_id=$w RETURN count(r) AS c",
                w=world_id,
            ).single()["c"]
            if actual_nc != meta["nc"] or actual_ec != meta["ec"]:
                return "count_mismatch"
            return None
    finally:
        driver.close()


def delete_world(world_id, uri, user, password) -> int:
    """資料フォルダの全ノード（と接続辺）を削除する（rebind/delete の wipe・`world_id` 単位）。

    `SherpaMeta`（スキーマ世代スタンプ）も同じクエリで削除する。戻り値は削除ノード総数（集計値）。
    """
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as s:
            r = s.run(
                "MATCH (n) WHERE n.world_id=$w AND (n:Entity OR n:SherpaMeta) "
                "DETACH DELETE n RETURN count(n) AS n",
                w=world_id)
            return r.single()["n"]
    finally:
        driver.close()


def reconcile(valid_worlds, uri, user, password) -> list:
    """孤児グラフの自動掃除: グラフに残る world_id のうち、登録された資料フォルダに無いものを `DETACH DELETE` する。

    `valid_worlds` は確実に取得できた登録 world id の集合。戻り値は削除した world_id の一覧。Neo4j 不可は `[]`（個別の失敗は次回に再試行）。
    """
    keep = set(valid_worlds or [])
    from neo4j import GraphDatabase
    deleted = []
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
    except Exception:
        return []
    try:
        with driver.session() as s:
            present = [r["w"] for r in s.run(
                "MATCH (n:Entity) WHERE n.world_id IS NOT NULL RETURN DISTINCT n.world_id AS w").data()]
            for wid in present:
                if wid in keep:
                    continue
                try:
                    # `SherpaMeta` も同じクエリで消す（`delete_world` と同じ）
                    s.run("MATCH (n) WHERE n.world_id=$w AND (n:Entity OR n:SherpaMeta) "
                         "DETACH DELETE n", w=wid)
                    deleted.append(wid)
                except Exception:
                    pass
    except Exception:
        return deleted
    finally:
        driver.close()
    return deleted


def resolve_world_entity(session, term, world_id, scope_prefixes=None,
                         include_deprecated=False):
    """起点語 → 起点 canonical_id 群（名前一致・範囲内）を返す。`impact_service.resolve_entity` の資料フォルダ版。

    `scope_prefixes` で起点も範囲に絞る（範囲外の同名は起点にしない）。業務語の入口はクエリ時のエージェントが文書を grep して発見する。
    `check_schema_era` は呼ばない（唯一の呼び出し元 `run_world_impact` の `world_impact` が1回だけ確認する。単独で呼ぶ場合は呼び出し側でゲートを足す）。
    """
    prefixes = list(scope_prefixes or [])
    rows = _run_read_capped(
        session,
        "MATCH (n:Entity {world_id:$w}) WHERE n.name=$name "
        "  AND ($incl OR coalesce(n.status,'active')='active') "
        f"  AND {_scope_pred('n')} "
        "RETURN n.canonical_id AS cid, [l IN labels(n) WHERE l<>'Entity'][0] AS label, "
        "  n.name AS name, n.path AS path",
        world=world_id, w=world_id, name=term, incl=include_deprecated, prefixes=prefixes,
    )
    # `path`: 同名の起点候補を呼び出し側が区別できるようにする
    return [{"canonical_id": r["cid"], "label": r["label"], "name": r["name"], "path": r["path"]}
           for r in rows]


def world_impact(session, start_cids, world_id, scope_prefixes=None, depth=IMPACT_MAX_DEPTH,
                 include_deprecated=False):
    """範囲フィルタ付きの Cypher で影響ノードを引き、構造化 item を返す（`impact_service.neo4j_impact` の資料フォルダ版）。

    start・affected・経路の全ノードが `world_id` と `scope_prefixes` 内。骨格エッジ（`_IMPACT_REL`＝COPIES/CONTAINS/INVOKES/ACCESSES）
    だけを辿る決定的な構造たどりで、全件を同格に扱う。
    主クエリの後に `check_schema_era` を呼ぶ（旧世代の実データがあれば `GraphSchemaEraError`）。主クエリ自体の安全弁を先に効かせるため。
    """
    # "world" キーは params に含めず、`_run_read_capped` の専用キーワード引数（world=world_id）から `session.run` へ渡す
    params = {"starts": list(start_cids),
              "incl": include_deprecated, "prefixes": list(scope_prefixes or [])}
    d = int(depth)
    sp_n = _scope_pred("n")

    impact_cypher = (
        "MATCH (start:Entity) WHERE start.canonical_id IN $starts "
        f"MATCH p=(affected:Entity)-[r:{_IMPACT_REL}*1..%(d)d]->(start) "
        "WHERE affected.world_id=$world "
        f"  AND all(n IN nodes(p) WHERE n.world_id=$world AND {sp_n}) "
        "  AND ($incl OR coalesce(affected.status,'active')='active') "
        "  AND ($incl OR all(n IN nodes(p) WHERE coalesce(n.status,'active')='active')) "
        "  AND ($incl OR all(e IN relationships(p) WHERE coalesce(e.status,'active')='active')) "
        "WITH affected, p ORDER BY length(p) "        # 代表経路＝最短（trace/evidence 用）
        "WITH affected, head(collect(p)) AS path "
        "RETURN affected.canonical_id AS cid, affected.name AS name, "
        "  [l IN labels(affected) WHERE l<>'Entity'][0] AS label, "
        "  coalesce(affected.status,'active') AS status, "
        "  affected.path AS dpath, affected.top_scope AS top, affected.analyzer AS analyzer, "
        "  [n IN nodes(path) | n.name] AS path_names, "
        "  [e IN relationships(path) | {type:type(e), doc:e.doc, line:e.line}] AS edges"
    ) % {"d": d}

    items = []
    for r in _run_read_capped(session, impact_cypher, world=world_id, **params):
        items.append({
            "name": r["name"],
            "label": r["label"],
            "category": CATEGORY.get(r["label"], r["label"]),
            "status": r["status"],
            "analyzer": r["analyzer"],                                # 担当アナライザの来歴（コード以外は None）
            "top_scope": r["top"], "path": r["dpath"],                # 所属（範囲）
            "trace": r["path_names"],                                 # なぜ影響するか（ノード名列）
            "evidence": [e for e in r["edges"] if e.get("doc")],      # 根拠(doc=rel_path/line)
        })
    check_schema_era(session, world_id, lens="impact")
    _attach_importance(items, world_id)
    return items


def _attach_importance(items: list, world_id: str) -> None:
    """items へ `importance`/`importance_reason`/`importance_mixed` を条件付きで付与する。

    候補は各 item の `path`（自身の所属文書）と `evidence[].doc`（根拠の来歴文書）。
    未設定の候補は「中」と同格の順位（`importance.RANK_UNSET`・`importance.rank_of`）として最高位の計算と `importance_mixed` の判定に含める。
    最高位の候補に実際の `Resolution`（`_重要度.txt` で明示解決されたもの）が1つも無ければ、3つとも付けない。
    最高位に複数の実 Resolution が同着するときは `path` 優先→`evidence` の出現順で先頭を採る。
    `_重要度.txt` の無い資料フォルダ（`resolve_for_world` が空 dict）は items を変更しない。
    """
    wd = worlds.world_dir(world_id)
    res_map = importance.resolve_for_world(world_id, root=wd) if wd else {}
    if not res_map:
        return
    for it in items:
        candidates = [c for c in ([it.get("path")] + [e.get("doc") for e in (it.get("evidence") or [])]) if c]
        if not candidates:
            continue
        pairs = [res_map.get(c) for c in candidates]              # None＝未解決（中相当）
        best_rank = max(importance.rank_of(res) for res in pairs)
        winners = [res for res in pairs if res is not None and importance.rank_of(res) == best_rank]
        if not winners:
            continue                                              # 最高位が全て未設定＝表示しない
        best = winners[0]
        it["importance"] = best.value
        if best.reason:
            it["importance_reason"] = best.reason
        effective_values = {(res.value if res is not None else "中") for res in pairs}
        if len(effective_values) > 1:
            it["importance_mixed"] = True


def run_world_impact(session, term, world_id, scope_prefixes=None,
                     depth=IMPACT_MAX_DEPTH, include_deprecated=False):
    """`resolve_world_entity` → `world_impact` → 構造化結果（emit_result 形・範囲つき）を返す。"""
    starts = resolve_world_entity(session, term, world_id, scope_prefixes, include_deprecated)
    items = world_impact(session, [s["canonical_id"] for s in starts], world_id,
                         scope_prefixes, depth, include_deprecated)
    # 第1ソートキーは重要度（`高`>`中`/未設定>`低`）。`_重要度.txt` の無い資料フォルダは全 item の rank が揃い、category・name だけの順になる
    items.sort(key=lambda x: (-importance.RANK.get(x.get("importance"), importance.RANK_UNSET),
                              x["category"], x["name"]))
    return {"type": "impact", "world_id": world_id, "scope_prefixes": list(scope_prefixes or []),
            "start": term, "include_deprecated": include_deprecated, "starts": starts, "items": items}


def default_neo4j_uri() -> str:
    """Neo4j 接続 URI。`NEO4J_URI` の明示が最優先、無ければ compose の公開ポート変数 `SHERPA_NEO4J_BOLT_PORT` に追随する。"""
    uri = os.environ.get("NEO4J_URI")
    if uri:
        return uri
    return f"bolt://localhost:{os.environ.get('SHERPA_NEO4J_BOLT_PORT') or '7687'}"


def _env():
    return dict(
        uri=default_neo4j_uri(),
        user=os.environ.get("NEO4J_USER", "neo4j"),
        pw=os.environ.get("NEO4J_PASSWORD", "sherpa_dev"),
    )
