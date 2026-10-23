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
import uuid

from neo4j import Query
from neo4j.exceptions import Neo4jError

from .. import graph_coverage, worlds                # 未完了の申告・資料フォルダの root 解決（重要度の解決に使う）
from ..env_int import env_int
from ..impact_service import CATEGORY                # 種別→結果カテゴリ
from . import importance, substage                   # 文書の重要度（`_重要度.txt`）・小さな段の進み具合
from .model import EDGE_TYPES, NODE_LABELS           # 閉じた語彙（Cypher 直埋めの allowlist）

_log = logging.getLogger("sherpa")

# 許容エッジ＝標準語彙＋対応エッジ `CORRESPONDS_TO`
WORLD_EDGE_TYPES = EDGE_TYPES | {"CORRESPONDS_TO"}

# 影響たどりで辿るのは構造（コード依存）のエッジだけ。対応エッジ `CORRESPONDS_TO` と添付 `DOCUMENTS`（言及エッジ含む）は辿らない。
# データの流れ `FLOWS_TO` は構造の影響に混ぜず、`world_impact` の第 2 段（`flow_depth`）で候補として別に辿る
_IMPACT_REL = "COPIES|CONTAINS|INVOKES|ACCESSES"
_FLOW_REL = "FLOWS_TO"

# 流れの候補を辿る段数の既定（道具・画面とも既定で出す）
FLOW_DEPTH_DEFAULT = 3

WORLD_CONSTRAINTS = [
    "CREATE CONSTRAINT canon IF NOT EXISTS FOR (n:Entity) REQUIRE n.canonical_id IS UNIQUE",
    "CREATE INDEX ent_world IF NOT EXISTS FOR (n:Entity) ON (n.world_id)",
    "CREATE INDEX ent_name IF NOT EXISTS FOR (n:Entity) ON (n.name)",
    # 資料フォルダごとのスキーマ世代スタンプ（`GRAPH_SCHEMA_ERA`）を持つメタノード。`:Entity` とは別ラベルなので `DETACH DELETE` の対象外（別途 MERGE/DELETE する）
    "CREATE CONSTRAINT sherpa_meta_world IF NOT EXISTS FOR (m:SherpaMeta) REQUIRE m.world_id IS UNIQUE",
]


# 保存形式の版（整数）。ノード・辺の保存する属性の形（`unresolved` 等）を変えたら上げる。`GRAPH_SCHEMA_ERA` の材料で、
# `worker._sig` には含めない（保存形式だけの変更でソースの全件取り込み・ES の再索引を起こさない）。
GRAPH_STORAGE_VERSION = 3

# 影響・近傍の結果へ返す未解決の申告の上限（件）
UNRESOLVED_RETURN_MAX = 50


def _compute_graph_schema_era() -> str:
    """`GRAPH_SCHEMA_ERA` を合成する（sha256 先頭12桁・決定的）。

    材料はコードアナライザの分類契約版・アナライザ登録簿の構成署名・言及エッジ突合の仕様版・保存形式の版（`GRAPH_STORAGE_VERSION`）・グラフ語彙。
    いずれかが変わると値も変わり、再取り込み前の読取ゲート（`check_schema_era`）が古いグラフを止める。
    原本の変更で動く `ingest.worker._sig`（last_sig）とは別物で、保存済みグラフを読んでよい前提が崩れたときだけ動く。
    材料モジュールは循環 import を避けるため関数内で import し、呼び出しは module import 時の1回だけ。
    """
    from .analyzers.registry import CODE_ANALYZERS_SCHEMA_VERSION, config_signature
    from .world_graph import MENTION_SCHEMA_VERSION
    material = repr((CODE_ANALYZERS_SCHEMA_VERSION, config_signature(), MENTION_SCHEMA_VERSION,
                     GRAPH_STORAGE_VERSION, tuple(sorted(NODE_LABELS)), tuple(sorted(EDGE_TYPES))))
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
        "membership_paths": n.get("membership_paths"),
        "path": n.get("path"), "sp": n.get("scope_path"), "value": n.get("value"),
        "status": n.get("status", "active"),
        "analyzer": n.get("analyzer"),
        "sources": _sources_json(n.get("sources")),
        "sources_overflow": n.get("sources_overflow_count", 0),
        "jcl_kind": n.get("jcl_kind"),
        "schema": n.get("schema"), "qualified_name": n.get("qualified_name"),
        "unresolved": _sources_json(n.get("unresolved")),
        "unresolved_names": n.get("unresolved_names"),
        "unresolved_overflow": n.get("unresolved_overflow_count", 0),
    }


def _edge_row(e: dict) -> dict:
    """1エッジ分の UNWIND 行。`via` は `RefCandidate.extra["via"]` 由来。
    根拠は `sources`（JSON 文字列・`{via, doc_id, file, line, rule?, from_def?, locator?, evidence_text?}` の配列）に持ち、上限超過件数は
    `sources_overflow_count`。接続の規則（`rule`）・抜粋（`evidence_text`）も `sources` の中で、辺の単独の `source`・`evidence`・`rule` は書かない。
    """
    return {
        "binding_paths": e.get("binding_paths"),
        "src": e["src"], "dst": e["dst"], "doc": e.get("doc", ""),
        "line": e.get("line", 0),
        "status": e.get("status", "active"),
        "sources": _sources_json(e.get("sources")),
        "sources_overflow": e.get("sources_overflow_count", 0),
        "via": e.get("via"),
    }


# 読み取りが返す辺の根拠（`sources`）の件数。`world_impact` は上限（`EDGE_SOURCES_RETURN_MAX`）まで返し、呼び出し側（`run_impact`・
# 外部 API の `evidence_limit`・近傍）が `EDGE_SOURCES_RETURN_DEFAULT` を既定に `limit_edge_sources` で切る。
EDGE_SOURCES_RETURN_DEFAULT = 3
EDGE_SOURCES_RETURN_MAX = 10

# Cypher の経路の辺 `e` から根拠の欄を取り出す射影（影響・近傍が共有する。`edge_view` が読む）
EDGE_SOURCE_FIELDS = "via:e.via, sources:e.sources, sources_overflow:e.sources_overflow_count"


def edge_view(raw: dict, limit: int = EDGE_SOURCES_RETURN_MAX) -> dict:
    """Cypher で取り出した辺（`type`・`from`・`to`・`doc`・`line` と `EDGE_SOURCE_FIELDS`）→ 返却の辺。

    `via` は辺の関係の種類。`sources`（先頭 `limit` 件）・`sources_overflow_count`（返さなかった根拠の件数＝保存の上限超過を含む）・
    `rule`（接続を決めた解決規則）は根拠のある辺だけに付く。`rule` は先頭の根拠から読む（旧欄の辺 `rule` は書かないので読み替えで足りる）。
    """
    out = {k: raw[k] for k in ("type", "from", "to", "doc", "line") if k in raw}
    if raw.get("via"):
        out["via"] = raw["via"]
    try:
        srcs = json.loads(raw["sources"]) if raw.get("sources") else []
    except (TypeError, ValueError):
        srcs = []
    if srcs:
        if srcs[0].get("rule"):
            out["rule"] = srcs[0]["rule"]
        out["sources"] = srcs[:limit]
        out["sources_overflow_count"] = int(raw.get("sources_overflow") or 0) + max(0, len(srcs) - limit)
    return out


def limit_edge_sources(edges: list, limit: int) -> list:
    """辺（`edge_view` の形）の `sources` を先頭 `limit` 件（0〜`EDGE_SOURCES_RETURN_MAX` に収める）に切り、切った分を
    `sources_overflow_count` に足す（元は変えない）。"""
    limit = max(0, min(int(limit), EDGE_SOURCES_RETURN_MAX))
    out = []
    for e in edges:
        srcs = e.get("sources")
        if srcs is None or len(srcs) <= limit:
            out.append(e)
            continue
        out.append({**e, "sources": srcs[:limit],
                    "sources_overflow_count": int(e.get("sources_overflow_count") or 0) + len(srcs) - limit})
    return out


# グラフの書き込みのメモリ不足（Neo4j の 1 回の書き込みの上限）を失敗の理由に添える文言。変数名は .env の設定名（scripts/lib/mem_budget.sh）
MEMORY_POOL_ERROR = "MemoryPoolOutOfMemoryError"
MEMORY_POOL_HINT = ("グラフの書き込みのメモリが足りません（Neo4j の 1 回の書き込みの上限）。"
                    ".env の SHERPA_NEO4J_HEAP_FIXED（Neo4j のヒープ）か SHERPA_NEO4J_TX_MEMORY_MAX（1 回の書き込みの上限）を上げて make restart してください")


def is_memory_pool_error(exc: BaseException) -> bool:
    """Neo4j の 1 回の書き込みがメモリの上限に当たった失敗か（`Neo.TransientError.General.MemoryPoolOutOfMemoryError`）。"""
    return MEMORY_POOL_ERROR in str(getattr(exc, "code", "") or "") or MEMORY_POOL_ERROR in str(exc)


_WRITE_ATTEMPTS = 2


def _write_world(session, apply):
    """`apply(tx)` を明示のトランザクションで書いて確定する。一時的な失敗は 1 回だけやり直す。

    守ること: 1 回の書き込みのメモリの上限（MemoryPoolOutOfMemoryError）に当たったら、やり直さずにそのまま失敗にする。
    """
    for attempt in range(1, _WRITE_ATTEMPTS + 1):
        try:
            with session.begin_transaction() as tx:
                result = apply(tx)
                tx.commit()
                return result
        except Exception as exc:
            retryable = callable(getattr(exc, "is_retryable", None)) and exc.is_retryable()
            if is_memory_pool_error(exc) or not retryable or attempt == _WRITE_ATTEMPTS:
                raise


def load_world(nodes, edges, world_id, uri, user, password, plugin_failures=None, unparsed_sources=None):
    """資料フォルダのグラフ（dict）を Neo4j へクリーン rebuild する（削除→全ロードを1つの write tx で行う）。

    ① schema コマンドはデータ tx と混ぜられないので先に流す。
    ② 語彙を閉じた集合で検証し、ラベル/エッジ型ごとにまとめる。
    ③ 1つの write tx の中で、当該 `world_id` を削除して `_NEO4J_BATCH_ROWS` 件ずつ UNWIND バッチで再ロードする
       （途中で失敗すれば tx 全体がロールバックし旧グラフが残る。複数 tx にしない＝半分削除・半分再構築を並行の影響検索に見せないため）。
    ④ 同じ tx の最後に `GRAPH_SCHEMA_ERA` と実物件数（`:SherpaMeta{world_id}`）を刻む。
    `sources`（ノードは出所リスト・辺は根拠リスト）は JSON 文字列として `x.sources`/`r.sources` に、上限超過件数は `sources_overflow_count` に載せる。
    未解決の申告は主体のノードの `unresolved`（JSON 文字列）・`unresolved_names`・`unresolved_overflow_count` に載せ、
    メタの `unresolved_stored` を立てる（`read_unresolved` が「申告の保存が無い」と「0 件」を区別する）。
    FW プラグインの失敗（`world_graph.plugin_failures_from_flags`）はメタの `plugin_failures`（JSON 文字列・失敗なしは `[]`）に同じ tx で保存する（`read_plugin_failures`）。
    グラフに入れなかったソースのファイル数（`world_graph.unparsed_sources_from_flags`）はメタの `unparsed_sources`（JSON 文字列）に同じ tx で保存する（`read_unparsed_sources`）。
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
    step = substage.begin("neo4j_load", "Neo4j に反映しています", total=len(nodes) + len(edges))
    loaded_ok = False
    try:
        with driver.session() as s:
            for c in WORLD_CONSTRAINTS:
                s.run(c)
            def _apply(tx):
                loaded = 0                                   # やり直しのときは最初から数え直す
                tx.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=world_id)
                for label, items in nodes_by_label.items():
                    node_cypher = (
                        "UNWIND $rows AS row "
                        f"MERGE (x:Entity {{canonical_id: row.cid}}) "
                        f"SET x:`{label}`, x.name=row.name, x.world_id=$world, "
                        f"x.top_scope=row.top, x.phase=row.phase, x.category=row.cat, x.path=row.path, "
                        f"x.scope_path=row.sp, x.value=row.value, x.membership_paths=row.membership_paths, "
                        f"x.status=row.status, x.analyzer=row.analyzer, x.sources=row.sources, "
                        f"x.sources_overflow_count=row.sources_overflow, x.jcl_kind=row.jcl_kind, "
                        f"x.schema=row.schema, x.qualified_name=row.qualified_name, "
                        f"x.unresolved=row.unresolved, x.unresolved_names=row.unresolved_names, "
                        f"x.unresolved_overflow_count=row.unresolved_overflow"
                    )
                    for batch in _batched(items, _NEO4J_BATCH_ROWS):
                        rows = [_node_row(n) for n in batch]
                        tx.run(node_cypher, rows=rows, world=world_id)
                        loaded += len(batch)
                        step.tick(loaded)
                for etype, items in edges_by_type.items():
                    edge_cypher = (
                        "UNWIND $rows AS row "
                        "MATCH (a:Entity {canonical_id: row.src}), (b:Entity {canonical_id: row.dst}) "
                        f"MERGE (a)-[r:`{etype}`]->(b) "
                        "SET r.world_id=$world, r.doc=row.doc, r.line=row.line, "
                        "r.status=row.status, "
                        "r.sources=row.sources, r.sources_overflow_count=row.sources_overflow, "
                        "r.via=row.via, r.binding_paths=row.binding_paths"
                    )
                    for batch in _batched(items, _NEO4J_BATCH_ROWS):
                        rows = [_edge_row(e) for e in batch]
                        tx.run(edge_cypher, rows=rows, world=world_id)
                        loaded += len(batch)
                        step.tick(loaded)
                step.tick(loaded)
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
                    "SET m.schema_era=$era, m.node_count=$nc, m.edge_count=$ec, m.unresolved_stored=true, "
                    "m.plugin_failures=$pf, m.unparsed_sources=$us, m.generation=$gen",
                    w=world_id, era=GRAPH_SCHEMA_ERA, nc=node_count, ec=edge_count, gen=uuid.uuid4().hex[:16],
                    pf=json.dumps(list(plugin_failures or []), ensure_ascii=False),
                    us=json.dumps(dict(unparsed_sources or {}), ensure_ascii=False))
                return len(nodes), len(edges)
            result = _write_world(s, _apply)
            loaded_ok = True
            return result
    finally:
        driver.close()
        step.end(failed=not loaded_ok)


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


# 1 段ずつ広げる影響たどり（BFS）。Neo4j には「frontier から逆向きに 1 段」だけを問い、訪問済み集合と最初に届いた辺（親）は Sherpa 側で持つ。
# 1 回の問い合わせに渡す frontier の id 数。行数の天井に当たったら半分に割って投げ直す
_IMPACT_FRONTIER_CHUNK = 300


def _impact_edge_where(mv: str) -> str:
    """影響たどりの 1 段で、広げた先のノード `mv` と辺 `e` に効かせる条件（資料フォルダ・範囲・status）。"""
    return (
        f"{mv}.world_id=$world AND {_scope_pred(mv)} "
        f"  AND ($incl OR coalesce({mv}.status,'active')='active') "
        "  AND ($incl OR coalesce(e.status,'active')='active') "
    )


def _impact_start_nodes(session, start_cids, world_id, params) -> dict[str, dict]:
    """起点のうち、資料フォルダ・範囲・status を満たすものの `cid → {name, path}`（満たさない起点からは何も届かない）。"""
    rows = _run_read_capped(
        session,
        "MATCH (s:Entity) WHERE s.canonical_id IN $starts AND s.world_id=$world "
        f"  AND {_scope_pred('s')} AND ($incl OR coalesce(s.status,'active')='active') "
        "RETURN s.canonical_id AS cid, s.name AS name, s.path AS path",
        world=world_id, starts=list(start_cids), **params)
    return {r["cid"]: {"name": r["name"], "path": r["path"]} for r in rows}


def _flow_path_pred(path: str) -> str:
    """流れの所属・DD 結合元のパスを範囲と秘匿条件で絞る。"""
    scope = (f"(size($prefixes)=0 OR any(pref IN $prefixes WHERE "
             f"{path}=pref OR {path} STARTS WITH pref+'/'))")
    return f"({path} IS NOT NULL AND {scope} AND {_not_sensitive_path('', path=path)})"


def _flow_node_path(var: str) -> str:
    """流れのノードの範囲内の代表パス（Dataset は全 DD 参照元から選ぶ）。"""
    return (f"head([flow_path IN CASE WHEN 'Dataset' IN labels({var}) THEN coalesce({var}.membership_paths,[]) "
            f"ELSE [{var}.path] END WHERE {_flow_path_pred('flow_path')}])")


def _flow_edge_pred(edge: str = "e") -> str:
    """ファイル受け渡しの DD 結合元が範囲内・非秘匿である条件。"""
    return (f"(NOT {edge}.via IN ['read_file','write_file'] OR "
            f"any(binding_path IN coalesce({edge}.binding_paths,[]) WHERE {_flow_path_pred('binding_path')}))")


def _safe_flow_edge(edge: dict, prefixes) -> dict:
    """流れの候補の経路根拠を範囲・非秘匿条件で絞る。"""
    from ..graph_tools import _route_doc, _safe_sources
    edge = dict(edge)
    try:
        sources = edge.get("sources") or []
        if not isinstance(sources, list):
            sources = json.loads(sources)
    except (TypeError, ValueError):
        sources = []
    sources = _safe_sources(sources, prefixes)
    edge["sources"] = _sources_json(sources)
    if prefixes:
        edge["sources_overflow"] = 0
    if not _route_doc(edge.get("doc"), prefixes):
        edge["doc"] = sources[0]["doc_id"] if sources else None
        edge["line"] = sources[0].get("line") if sources else None
    return edge


def _impact_expand_rows(session, frontier, world_id, params, sink: list, *, seen=None, exists_only=False,
                        rel: str = _IMPACT_REL, direction: str = "in") -> None:
    """`frontier` の各ノードから辺 `rel` でつながる 1 段先のノードを `sink` に集める（frontier は `_IMPACT_FRONTIER_CHUNK` 件ずつ分けて問う）。
    `direction` が `in` なら frontier へ辺を向ける側（依存元）、`out` なら frontier から辺が出る先。
    行数の天井に当たった塊は半分に割って投げ直し、1 件でも天井なら送出する。送出の時点までに `sink` へ入った行は残る。
    `exists_only` は、`seen` に無いノードが 1 つでも見つかれば打ち切る（深さの先の存在確認）。
    """
    flow = rel == _FLOW_REL
    path = _flow_node_path('m') if flow else "m.path"
    if exists_only:
        tail = "RETURN 1 AS x LIMIT 1"
        guard = "AND NOT m.canonical_id IN $seen "
    else:
        tail = (
            "RETURN m.canonical_id AS cid, m.name AS name, [l IN labels(m) WHERE l<>'Entity'][0] AS label, "
            f"  coalesce(m.status,'active') AS status, {path} AS dpath, m.top_scope AS top, m.analyzer AS analyzer, "
            f"  n.canonical_id AS to_cid, {{type:type(e), doc:e.doc, line:e.line, {EDGE_SOURCE_FIELDS}}} AS edge")
        guard = ""
    pattern = f"(m:Entity)-[e:{rel}]->(n:Entity)" if direction == "in" else f"(n:Entity)-[e:{rel}]->(m:Entity)"
    where = _impact_edge_where('m')
    if flow:
        where = (
            f"m.world_id=$world AND {path} IS NOT NULL AND {_flow_node_path('n')} IS NOT NULL "
            "AND ($incl OR coalesce(m.status,'active')='active') "
            "AND ($incl OR coalesce(e.status,'active')='active') "
            f"AND {_flow_edge_pred()} ")
    cypher = (
        f"MATCH {pattern} WHERE n.canonical_id IN $frontier {guard}"
        f"  AND {where} " + tail)
    extra = {"seen": list(seen)} if exists_only else {}
    if flow:
        from . import text_kind
        extra.update(s_exact=sorted(text_kind._SENSITIVE_NAME_EXACT), s_pre=list(text_kind._SENSITIVE_NAME_PREFIXES),
                     s_ext=sorted(text_kind.SENSITIVE_EXT))
    todo = list(_batched(list(frontier), _IMPACT_FRONTIER_CHUNK))
    while todo:
        ids = todo.pop(0)
        try:
            rows = _run_read_capped(session, cypher, world=world_id, frontier=ids, **params, **extra)
        except GraphQueryOverloadError as e:
            if e.reason == "too_many_rows" and len(ids) > 1:
                todo[:0] = [ids[:len(ids) // 2], ids[len(ids) // 2:]]
                continue
            raise
        sink.extend(rows)
        if exists_only and rows:
            return


def _impact_row_key(r: dict):
    e = r["edge"]
    return (r["cid"], r["to_cid"], str(e.get("type")), str(e.get("doc")), e.get("line") or 0)


def _flow_stage(session, seeds, visited: set, world_id, params, flow_depth: int, *, parent: dict, meta: dict,
                order: list, flow_cids: set) -> tuple:
    """第 2 段のたどり（データの流れの候補）。構造の到達集合 `seeds` を種に、`flow_depth` 段だけ広げる。

    1 段目は種からの `FLOWS_TO` の出辺。2 段目以降は、前の段で流れによって届いたノードからの `FLOWS_TO` の出辺と `_IMPACT_REL` の入辺
    （流れで届いた読み手を実行するジョブなど）。`visited` は構造の段から引き継ぎ、初めて届いたノードだけを `parent`／`meta`／`order`／`flow_cids` へ足す。
    戻りは `(最後に届いたノード, 過負荷)`。過負荷のときは、それまでに届いた分を残して打ち切る。
    """
    frontier = sorted(seeds)
    overload: GraphQueryOverloadError | None = None
    for level in range(flow_depth):
        if not frontier:
            break
        rows: list[dict] = []
        try:
            _impact_expand_rows(session, frontier, world_id, params, rows, rel=_FLOW_REL, direction="out")
            if level > 0:
                _impact_expand_rows(session, frontier, world_id, params, rows)
        except GraphQueryOverloadError as e:
            overload = e
        reached: list[str] = []
        for r in sorted(rows, key=_impact_row_key):
            cid = r["cid"]
            if cid in visited:
                continue
            visited.add(cid)
            parent[cid] = (r["to_cid"], r["edge"])
            meta[cid] = r
            order.append(cid)
            flow_cids.add(cid)
            reached.append(cid)
        frontier = reached
        if overload is not None:
            break
    return frontier, overload


def world_impact(session, start_cids, world_id, scope_prefixes=None, depth=IMPACT_MAX_DEPTH,
                 include_deprecated=False, info: dict | None = None, detail: bool = False, flow_depth: int = 0):
    """範囲フィルタ付きで影響ノードを 1 段ずつ広げて引き、構造化 item を返す（`impact_service.neo4j_impact` の資料フォルダ版）。

    start・affected・経路の全ノードが `world_id` と `scope_prefixes` 内。骨格エッジ（`_IMPACT_REL`＝COPIES/CONTAINS/INVOKES/ACCESSES）
    だけを辿る決定的な構造たどりで、全件を同格に扱う。
    ① 起点を確かめる ② 段 k ごとに frontier から逆向きに 1 段だけ問い、初めて届いたノードに最初の辺（親）を記録する
    ③ `check_schema_era`（旧世代の実データがあれば `GraphSchemaEraError`） ④ 深さの先に未到達のノードが残るかを 1 回だけ問う。
    代表経路は親をたどって復元する（段ごとに広げるので最初の到達が最短）。起点どうしが互いに届くときと、起点へ戻る循環は、起点も影響先に数える。
    `info`（省略可）を渡すと、深さの先に影響先が残るかを `info["depth_truncated"]`（真偽・判定できなければ None と `depth_check_limit`）へ入れる。
    段の問い合わせが timeout・行数の天井に当たったときは、届いた分を返し `info["overload"]`（`GraphQueryOverloadError`）を立てる
    （`info` が無いときは `GraphQueryOverloadError` をそのまま送出する）。
    `detail`（既定 False）が真のとき、各 item に影響先の `canonical_id`・代表経路のノード識別子 `trace_cids`・
    代表経路の全辺 `edges`（`type/doc/line`・`trace` と同じ並び＝影響先から起点へ）を足す（`graph_impact` 用）。
    `flow_depth`（既定 0）が正のとき、構造の後に第 2 段（`_flow_stage`）でデータの流れの候補を `flow_depth` 段まで広げる。各 item に
    `origin`（`structure`／`flow`）を足し、`info["flow_depth_truncated"]`（真偽・判定できなければ None と `flow_depth_check_limit`）と
    第 2 段の過負荷 `info["flow_overload"]` を立てる。構造の段が過負荷なら第 2 段は走らせない。0 のときは戻りも `info` も変わらない。
    """
    params = {"incl": include_deprecated, "prefixes": list(scope_prefixes or [])}
    d = int(depth)
    starts = _impact_start_nodes(session, start_cids, world_id, params)
    visited = set(starts)                       # 展開済み（起点を含む）
    frontier = sorted(starts)
    parent: dict[str, tuple[str, dict]] = {}    # 起点以外の影響先 → (広げた元ノード, その辺)
    start_hit: dict[str, tuple[str, dict]] = {}  # 起点に戻って届いた起点 → (広げた元ノード, その辺)
    meta: dict[str, dict] = {}
    meta_hit: dict[str, dict] = {}
    order: list[str] = []
    overload: GraphQueryOverloadError | None = None
    for _level in range(d):
        if not frontier:
            break
        rows: list[dict] = []
        try:
            _impact_expand_rows(session, frontier, world_id, params, rows)
        except GraphQueryOverloadError as e:
            overload = e
        reached: list[str] = []
        for r in sorted(rows, key=_impact_row_key):
            cid = r["cid"]
            if cid in starts:
                if cid not in start_hit:
                    start_hit[cid] = (r["to_cid"], r["edge"])
                    meta_hit[cid] = r
                    order.append(cid)
            elif cid not in visited:
                visited.add(cid)
                parent[cid] = (r["to_cid"], r["edge"])
                meta[cid] = r
                order.append(cid)
                reached.append(cid)
        frontier = reached
        if overload is not None:
            break
    if overload is not None and info is None:
        raise overload
    struct_order, struct_frontier = list(order), frontier
    flow_cids: set[str] = set()
    flow_overload: GraphQueryOverloadError | None = None
    flow_frontier: list[str] = []
    if flow_depth > 0 and overload is None:
        flow_frontier, flow_overload = _flow_stage(
            session, list(visited), visited, world_id, params, int(flow_depth),
            parent=parent, meta=meta, order=order, flow_cids=flow_cids)
        if flow_overload is not None and info is None:
            raise flow_overload

    def chain(cid):
        names, cids, paths, edges = [], [], [], []
        step = parent.get(cid) or start_hit.get(cid)
        while True:
            node = meta.get(cid)
            names.append(node["name"] if node else starts[cid]["name"])
            cids.append(cid)
            paths.append(node["dpath"] if node else starts[cid]["path"])
            if step is None:
                return names, cids, paths, edges
            edges.append(step[1])
            cid = step[0]
            step = parent.get(cid)

    items = []
    for cid in order:
        names, cids, paths, edges = chain(cid)
        r = meta.get(cid) or meta_hit[cid]
        items.append({
            "name": r["name"],
            "label": r["label"],
            "category": CATEGORY.get(r["label"], r["label"]),
            "status": r["status"],
            "analyzer": r["analyzer"],                                # 担当アナライザの来歴（コード以外は None）
            "top_scope": r["top"], "path": r["dpath"],                # 所属（範囲）
            "trace": names,                                           # なぜ影響するか（ノード名列）
            "evidence": [e for e in map(edge_view, edges) if e.get("doc")],   # 根拠（辺ごとの doc・line・via・rule・sources）
        })
        if cid in flow_cids:
            edges = [_safe_flow_edge(e, scope_prefixes) for e in edges]
            items[-1]["evidence"] = [e for e in map(edge_view, edges) if e.get("doc")]
        if flow_depth > 0:
            items[-1]["origin"] = "flow" if cid in flow_cids else "structure"
        if detail:
            items[-1].update({"canonical_id": cid, "trace_cids": cids, "trace_paths": paths, "edges": edges})
    check_schema_era(session, world_id, lens="impact")
    if info is not None:
        if overload is not None:
            _log.warning("影響たどりが途中で過負荷のため届いた分だけ返す（world=%s・%s）", world_id, overload.reason)
            info["overload"] = overload
            info["depth_truncated"] = None
            info["depth_check_limit"] = graph_coverage.kind_of_overload(overload.reason)
        else:
            # frontier が空なら、その先も無い。判定の問い合わせの過負荷は本体の結果と分ける（判定だけ不明＝None と過負荷の理由を渡す）
            try:
                info["depth_truncated"] = False
                if d >= 1 and struct_frontier:
                    probe: list[dict] = []
                    _impact_expand_rows(session, struct_frontier, world_id, params, probe,
                                        seen=set(struct_order), exists_only=True)
                    info["depth_truncated"] = bool(probe)
            except GraphQueryOverloadError as e:
                _log.warning("深さの先の判定が過負荷のため不明として返す（world=%s・%s）", world_id, e.reason)
                info["depth_truncated"] = None
                info["depth_check_limit"] = graph_coverage.kind_of_overload(e.reason)
        if flow_depth > 0:
            _flow_truncation(session, info, world_id, params, visited, flow_frontier, overload, flow_overload)
    _attach_importance(items, world_id)
    return items


def _flow_truncation(session, info: dict, world_id, params, visited: set, flow_frontier: list,
                     struct_overload, flow_overload) -> None:
    """第 2 段の打ち切りを `info` に入れる。構造の段か第 2 段が過負荷なら不明（None）、そうでなければ先に未到達のノードが残るかを 1 回だけ問う。"""
    over = struct_overload or flow_overload
    if over is not None:
        info["flow_depth_truncated"] = None
        info["flow_depth_check_limit"] = graph_coverage.kind_of_overload(over.reason)
        if flow_overload is not None:
            info["flow_overload"] = flow_overload
        return
    info["flow_depth_truncated"] = False
    if not flow_frontier:
        return
    try:
        probe: list[dict] = []
        _impact_expand_rows(session, flow_frontier, world_id, params, probe, seen=visited, exists_only=True,
                            rel=_FLOW_REL, direction="out")
        if not probe:
            _impact_expand_rows(session, flow_frontier, world_id, params, probe, seen=visited, exists_only=True)
        info["flow_depth_truncated"] = bool(probe)
    except GraphQueryOverloadError as e:
        _log.warning("流れの先の判定が過負荷のため不明として返す（world=%s・%s）", world_id, e.reason)
        info["flow_depth_truncated"] = None
        info["flow_depth_check_limit"] = graph_coverage.kind_of_overload(e.reason)


def read_plugin_failures(session, world_id) -> list:
    """その資料フォルダのグラフを作ったときに失敗した FW プラグイン（`[{plugin, files, why}]`・保存が無い旧グラフ・失敗なしは空）。範囲（フォルダ）では絞らない。"""
    rows = _run_read_capped(
        session, "MATCH (m:SherpaMeta {world_id:$w}) RETURN m.plugin_failures AS pf",
        world=world_id, w=world_id)
    if not rows or not rows[0].get("pf"):
        return []
    return json.loads(rows[0]["pf"])


def read_generation(session, world_id) -> str | None:
    """そのグラフの取り込みの世代（`load_world` が刻む `generation`）。作り直すたびに変わる。印のない旧グラフは件数から作った印、グラフが無ければ None。"""
    rows = _run_read_capped(
        session, "MATCH (m:SherpaMeta {world_id:$w}) "
                 "RETURN m.generation AS g, m.node_count AS nc, m.edge_count AS ec, m.schema_era AS era",
        world=world_id, w=world_id)
    if not rows:
        return None
    r = rows[0]
    return str(r["g"]) if r.get("g") else f"legacy-{r.get('era')}-{r.get('nc')}-{r.get('ec')}"


def read_unparsed_sources(session, world_id) -> dict | None:
    """そのグラフを作ったときにグラフへ入れなかったソースのファイル数 `{syntax, size_exceeded}`。保存が無い旧グラフは None（不明・申告しない）。範囲（フォルダ）では絞らない。"""
    rows = _run_read_capped(
        session, "MATCH (m:SherpaMeta {world_id:$w}) RETURN m.unparsed_sources AS us",
        world=world_id, w=world_id)
    if not rows or not rows[0].get("us"):
        return None
    info = json.loads(rows[0]["us"])
    return info if isinstance(info, dict) else None


def read_unresolved(session, world_id, names, scope_prefixes=None, *, fold_case=False) -> dict:
    """起点の名前に一致する未解決の参照（保存済みの申告）を `{available, items[], omitted}` で返す。

    `available`＝そのグラフが申告を保存しているか（`:SherpaMeta.unresolved_stored`・無い旧グラフは False）。
    `items` は最大 `UNRESOLVED_RETURN_MAX` 件（`path`・`line`・`reason`・`kind`・`name`・`via`・`from_def`・曖昧のとき `candidates`）。
    `omitted`＝返さなかった件数（返却の上限で切った分＋一致したファイルが保存の上限〔1 ファイル 50 件〕で落とした件数）。
    返却の上限より先のノードは件数だけ数え（各 1 件以上として足す）、このため `omitted` は「少なくとも」の数になりうる。
    範囲は他の読み取りと同じフォルダ prefix の述語（参照を書いたファイルの `path`）。
    `fold_case`＝名前を大文字小文字無視で比べる（近傍の起点の解決と同じ規則）。影響の起点は完全一致（`resolve_world_entity` と同じ）。
    一致したノードは Cypher 側で `path` 順に集約し、先頭 `UNRESOLVED_RETURN_MAX` ノードだけを取り出す。
    """
    names = [n for n in dict.fromkeys(names or []) if n]
    if fold_case:
        names = list(dict.fromkeys(n.lower() for n in names))
    key = "toLower(x)" if fold_case else "x"
    rows = _run_read_capped(
        session,
        "MATCH (m:SherpaMeta {world_id:$w}) "
        "OPTIONAL MATCH (n:Entity {world_id:$w}) WHERE n.unresolved_names IS NOT NULL "
        f"  AND any(x IN n.unresolved_names WHERE {key} IN $names) "
        f"  AND {_scope_pred('n')} "
        "WITH m, n ORDER BY n.path "
        "WITH m, collect(n) AS ns "
        "RETURN m.unresolved_stored AS stored, size(ns) AS node_total, "
        "  reduce(o=0, x IN ns | o + coalesce(x.unresolved_overflow_count,0)) AS ov_total, "
        "  [x IN ns[0..$cap] | {path:x.path, u:x.unresolved}] AS head",
        world=world_id, w=world_id, names=names, prefixes=list(scope_prefixes or []), cap=UNRESOLVED_RETURN_MAX)
    if not (rows and rows[0].get("stored")):
        return {"available": False, "items": [], "omitted": 0}
    row = rows[0]
    wanted = set(names)

    def _match(nm: str) -> bool:
        tail = nm.rsplit(".", 1)[-1]
        return (nm.lower() in wanted or tail.lower() in wanted) if fold_case else (nm in wanted or tail in wanted)

    items: list = []
    for h in row.get("head") or []:
        for it in json.loads(h["u"] or "[]"):
            if _match(it["name"]):
                items.append({"path": h["path"], **it})
    extra_nodes = max(0, int(row.get("node_total") or 0) - UNRESOLVED_RETURN_MAX)
    omitted = int(row.get("ov_total") or 0) + extra_nodes + max(0, len(items) - UNRESOLVED_RETURN_MAX)
    return {"available": True, "items": items[:UNRESOLVED_RETURN_MAX], "omitted": omitted}


def _not_sensitive_path(var: str, *, path: str | None = None) -> str:
    """ノード `var` の所属パスの末尾のファイル名が秘匿名（`text_kind.is_sensitive` と同じ規則）でない、の Cypher 述語（`$s_exact`・`$s_pre`・`$s_ext` を使う）。"""
    fl = f"toLower(last(split(coalesce({path or var + '.path'},''),'/')))"
    return (f"NOT ({fl} IN $s_exact OR any(p IN $s_pre WHERE {fl} STARTS WITH p) "
            f"OR any(e IN $s_ext WHERE {fl} ENDS WITH e))")


def list_world_candidates(session, name, world_id, scope_prefixes=None, *, kind=None, path_part=None,
                          include_deprecated=False, limit: int | None = None,
                          exclude_documents: bool = False) -> list[dict]:
    """起点の候補（`graph_resolve` 用）。名前（表示名・修飾名の部分一致・大文字小文字無視）・種別（ラベル）・所属パスの一部で絞る。
    同名の複数候補はまとめず全て返す（並びは名前の完全一致 → 名前 → パス）。`match` は `exact`（名前または修飾名が一致）／`partial`。
    範囲（`scope_prefixes`）・status は影響たどり（`world_impact`）と同じ述語。主クエリの後に `check_schema_era` を呼ぶ。
    秘匿名のファイルに属するノードは返さない。`limit` を渡すと Cypher 側で上位 `limit` 件だけ取る（並びも Cypher で決める＝呼び出し側は
    `limit+1` を渡して超過の有無を見る）。`exclude_documents` が真なら資料（`Document`）を除く。
    過負荷は `GraphQueryOverloadError`（空・部分結果にしない）。
    """
    from . import text_kind
    term = (name or "").strip()
    rows = _run_read_capped(
        session,
        "MATCH (n:Entity {world_id:$w}) "
        "WHERE ($incl OR coalesce(n.status,'active')='active') "
        f"  AND {_scope_pred('n')} "
        "  AND ($name = '' OR toLower(n.name) CONTAINS toLower($name) "
        "       OR toLower(coalesce(n.qualified_name,'')) CONTAINS toLower($name)) "
        "  AND ($kind = '' OR $kind IN labels(n)) "
        "  AND ($pp = '' OR toLower(coalesce(n.path,'')) CONTAINS toLower($pp)) "
        "  AND ($docs_ok OR NOT 'Document' IN labels(n)) "
        f"  AND {_not_sensitive_path('n')} "
        "RETURN n.canonical_id AS cid, [l IN labels(n) WHERE l<>'Entity'][0] AS label, "
        "  n.name AS name, n.path AS path, n.qualified_name AS qname, "
        "  coalesce(n.status,'active') AS status "
        "ORDER BY CASE WHEN toLower(n.name)=toLower($name) "
        "  OR toLower(coalesce(n.qualified_name,''))=toLower($name) THEN 0 ELSE 1 END, "
        "  toLower(n.name), coalesce(n.path,''), n.canonical_id"
        + (" LIMIT $lim" if limit is not None else ""),
        world=world_id, w=world_id, name=term, kind=kind or "", pp=(path_part or "").strip(),
        incl=include_deprecated, prefixes=list(scope_prefixes or []), docs_ok=not exclude_documents,
        s_exact=sorted(text_kind._SENSITIVE_NAME_EXACT), s_pre=list(text_kind._SENSITIVE_NAME_PREFIXES),
        s_ext=sorted(text_kind.SENSITIVE_EXT), lim=limit,
    )
    check_schema_era(session, world_id)
    low = term.lower()
    out = []
    for r in rows:
        exact = bool(low) and low in ((r["name"] or "").lower(), (r["qname"] or "").lower())
        out.append({"canonical_id": r["cid"], "label": r["label"], "name": r["name"], "path": r["path"],
                    "qualified_name": r["qname"], "status": r["status"], "match": "exact" if exact else "partial"})
    out.sort(key=lambda c: (c["match"] != "exact", (c["name"] or "").lower(), c["path"] or "", c["canonical_id"]))
    return out


def get_world_entities(session, cids, world_id, scope_prefixes=None, include_deprecated=False) -> list[dict]:
    """識別子（canonical_id）でノードを引く（資料フォルダ・範囲・status の述語は影響たどりと同じ）。無いものは返らない。
    主クエリの後に `check_schema_era` を呼ぶ。
    """
    rows = _run_read_capped(
        session,
        "MATCH (n:Entity {world_id:$w}) WHERE n.canonical_id IN $cids "
        "  AND ($incl OR coalesce(n.status,'active')='active') "
        f"  AND {_scope_pred('n')} "
        "RETURN n.canonical_id AS cid, [l IN labels(n) WHERE l<>'Entity'][0] AS label, "
        "  n.name AS name, n.path AS path, n.qualified_name AS qname",
        world=world_id, w=world_id, cids=list(cids), incl=include_deprecated,
        prefixes=list(scope_prefixes or []),
    )
    check_schema_era(session, world_id)
    return [{"canonical_id": r["cid"], "label": r["label"], "name": r["name"], "path": r["path"],
             "qualified_name": r["qname"]} for r in rows]


def world_related_documents(session, cids, world_id, scope_prefixes=None, include_deprecated=False,
                            limit: int | None = None) -> list[dict]:
    """`cids` のノードを `DOCUMENTS`（言及を含む）で指す資料を返す（影響には数えず、関連文書として別に返す）。
    資料の所属が範囲内のものだけ。秘匿名のファイルに属する資料・対象は返さない。並びは資料のパス → 対象の識別子。
    `limit` を渡すと Cypher 側で上位 `limit` 件だけ取る（呼び出し側は上限＋1 を渡して超過の有無を見る）。
    """
    from . import text_kind
    rows = _run_read_capped(
        session,
        "MATCH (d:Entity {world_id:$w})-[r:DOCUMENTS]->(t:Entity {world_id:$w}) WHERE t.canonical_id IN $cids "
        "  AND ($incl OR (coalesce(d.status,'active')='active' AND coalesce(t.status,'active')='active')) "
        f"  AND {_scope_pred('d')} "
        f"  AND {_not_sensitive_path('d')} AND {_not_sensitive_path('t')} "
        "RETURN d.canonical_id AS cid, d.name AS name, d.path AS path, "
        "  t.canonical_id AS tcid, t.name AS tname, t.path AS tpath, r.via AS via, r.line AS line "
        "ORDER BY coalesce(d.path,''), t.canonical_id"
        + (" LIMIT $lim" if limit is not None else ""),
        world=world_id, w=world_id, cids=list(cids), incl=include_deprecated,
        prefixes=list(scope_prefixes or []), lim=limit,
        s_exact=sorted(text_kind._SENSITIVE_NAME_EXACT), s_pre=list(text_kind._SENSITIVE_NAME_PREFIXES),
        s_ext=sorted(text_kind.SENSITIVE_EXT),
    )
    rows.sort(key=lambda r: (r["path"] or "", r["tcid"]))
    return [{"canonical_id": r["cid"], "name": r["name"], "path": r["path"], "via": r["via"], "line": r["line"],
             "target": {"canonical_id": r["tcid"], "name": r["tname"]}, "_target_path": r["tpath"]}
            for r in rows]


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
                     depth=IMPACT_MAX_DEPTH, include_deprecated=False, flow_depth=0):
    """`resolve_world_entity` → `world_impact` → 構造化結果（emit_result 形・範囲つき）を返す。

    `flow_depth` が正のとき、構造の影響 `items` とは別に、データの流れで届いた候補を `candidates` に返す（`coverage.flow` に段数と打ち切り）。
    第 2 段の過負荷は構造の結果を捨てずに `coverage.limits`（stage `flow`）で申告する。
    """
    starts = resolve_world_entity(session, term, world_id, scope_prefixes, include_deprecated)
    info: dict = {}
    items = world_impact(session, [s["canonical_id"] for s in starts], world_id,
                         scope_prefixes, depth, include_deprecated, info=info,
                         **({"flow_depth": flow_depth} if flow_depth > 0 else {}))
    if info.get("overload") is not None:
        raise info["overload"]   # 画面側は届いた分だけを「影響なし」と読ませない（範囲を絞って再実行を促す）
    candidates = [i for i in items if i.get("origin") == "flow"]
    if flow_depth > 0:
        items = [i for i in items if i.get("origin") != "flow"]
        for i in items + candidates:
            i.pop("origin", None)
    truncated = info.get("depth_truncated", False)  # None＝判定できなかった（不明）
    coverage = graph_coverage.Coverage()
    if truncated is None:
        coverage.add(info["depth_check_limit"], graph_coverage.STAGE_IMPACT)
    elif truncated:
        coverage.add(graph_coverage.KIND_DEPTH, graph_coverage.STAGE_IMPACT)
    flow_cov = None
    if flow_depth > 0:
        flow_truncated = info.get("flow_depth_truncated", False)
        if flow_truncated is None:
            coverage.add(info["flow_depth_check_limit"], graph_coverage.STAGE_FLOW)
        elif flow_truncated:
            coverage.add(graph_coverage.KIND_DEPTH, graph_coverage.STAGE_FLOW)
        flow_cov = {"requested": int(flow_depth), "truncated": flow_truncated}
    graph_coverage.attach_plugin_failures(
        coverage, lambda: read_plugin_failures(session, world_id), graph_coverage.STAGE_IMPACT,
        read_unparsed=lambda: read_unparsed_sources(session, world_id))
    # 第1ソートキーは重要度（`高`>`中`/未設定>`低`）。`_重要度.txt` の無い資料フォルダは全 item の rank が揃い、category・name だけの順になる
    def _order(x):
        return (-importance.RANK.get(x.get("importance"), importance.RANK_UNSET), x["category"], x["name"])
    items.sort(key=_order)
    out = {"type": "impact", "world_id": world_id, "scope_prefixes": list(scope_prefixes or []),
           "start": term, "include_deprecated": include_deprecated, "starts": starts, "items": items,
           "unresolved": read_unresolved(session, world_id, [term], scope_prefixes),
           "coverage": coverage.as_dict(depth={"requested": int(depth), "truncated": truncated}, flow=flow_cov)}
    if flow_depth > 0:
        candidates.sort(key=_order)
        out["candidates"] = candidates
    return out


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
