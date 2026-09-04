"""鏡モデルのグラフを Neo4j へロード＋**範囲フィルタ付き**影響たどり（MIRROR-MODEL §2-§3・再プラン手順5）。

`world_graph.build_world` が返す **dict ノード/エッジ**（パス同一性・検索スコープ
メタデータ `world_id/top_scope/phase/category/path`）をそのまま Neo4j に MERGE する。
影響 Cypher は **`world_id` ＋ フォルダ prefix（top_scope/phase…）** で絞り、
`scope_prefixes` で「どの階層でも1世界として」たどれる（in-memory の `world_graph.subgraph` と同義）。

**カットオーバー済**: 鏡（world）経路が正＝旧 `@version` 経路（`canonical_id @版`・版物理分離）は退役。
語彙（label/edge）はクローズドなので Cypher 直埋めは安全（allowlist＝NODE_LABELS/EDGE_TYPES）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re

from neo4j import Query
from neo4j.exceptions import Neo4jError

from .. import worlds                                # world root 解決（重要度の解決に使う・I2）
from ..impact_service import CATEGORY                # 種別→結果カテゴリ（再利用・読み取りのみ）
from . import importance                             # 文書の重要度（`_重要度.txt`・I2）
from .model import EDGE_TYPES, NODE_LABELS           # 閉じた語彙（Cypher 直埋めの allowlist・RV Med#1）

_log = logging.getLogger("sherpa")

# 鏡の許容エッジ＝旧オントロジー語彙＋**対応エッジ `CORRESPONDS_TO`**（世代横断の対応・MIRROR §2.3）。
WORLD_EDGE_TYPES = EDGE_TYPES | {"CORRESPONDS_TO"}

# 影響たどりのエッジ＝構造（コード依存）のみ（K13・2026-09-04-グラフのソース正典化.md §4）。
# **対応エッジ `CORRESPONDS_TO` と 添付 `DOCUMENTS`（言及エッジ含む）は辿らない**
# （世代横断の比較・根拠添付＝影響伝播ではない・MIRROR §2.3 / ONTOLOGY §7）。
_IMPACT_REL = "COPIES|CONTAINS|INVOKES|ACCESSES"

WORLD_CONSTRAINTS = [
    "CREATE CONSTRAINT canon IF NOT EXISTS FOR (n:Entity) REQUIRE n.canonical_id IS UNIQUE",
    "CREATE INDEX ent_world IF NOT EXISTS FOR (n:Entity) ON (n.world_id)",
    "CREATE INDEX ent_name IF NOT EXISTS FOR (n:Entity) ON (n.name)",
    # rv-s3-removal: world ごとのスキーマ世代スタンプ（`GRAPH_SCHEMA_ERA`）を持つメタノード。
    # `:Entity` とは別ラベルのため `load_world`/`delete_world` の `DETACH DELETE (n:Entity ...)` の
    # 対象に入らない（明示的に別途 MERGE/DELETE する）。
    "CREATE CONSTRAINT sherpa_meta_world IF NOT EXISTS FOR (m:SherpaMeta) REQUIRE m.world_id IS UNIQUE",
]


def _compute_graph_schema_era() -> str:
    """`GRAPH_SCHEMA_ERA` の合成（sha256 先頭12桁・決定的・rv-s3-removal）。

    材料: コードアナライザの分類契約版（`analyzers.registry.CODE_ANALYZERS_SCHEMA_VERSION`）・
    言及エッジ突合の仕様版（`world_graph.MENTION_SCHEMA_VERSION`）・グラフ語彙
    （`model.NODE_LABELS`/`EDGE_TYPES` のソート済みタプル）。いずれかが変われば合成値も変わる。

    **`ingest.worker._sig`（last_sig・ファイル走査込みの内容署名）とは別物**——last_sig は原本の
    追加・変更のたびに動く「次回 sync で再取り込みが必要（鏡モデルの正常運転）」の署名で、
    こちらはコード側のグラフ内部形式そのものが変わったときだけ動く「**保存済みグラフを
    そのまま読んでよい前提が崩れた**」署名（Codex 案の「last_sig 不一致で 503」は不採用・
    原本変更〜次 sync の署名差は正常運転のため）。

    循環 import 回避のため、材料モジュール（`world_graph`/`analyzers.registry`）は本関数の中で
    遅延 import する（本モジュールを読み込む側から見て、これらは下位層のため実際には循環しない
    が、この慣例に揃える）。呼び出しは module import 時に一度だけ（`GRAPH_SCHEMA_ERA` 定義）。
    """
    from .analyzers.registry import CODE_ANALYZERS_SCHEMA_VERSION
    from .world_graph import MENTION_SCHEMA_VERSION
    material = repr((CODE_ANALYZERS_SCHEMA_VERSION, MENTION_SCHEMA_VERSION,
                     tuple(sorted(NODE_LABELS)), tuple(sorted(EDGE_TYPES))))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


GRAPH_SCHEMA_ERA = _compute_graph_schema_era()

# 検索スコープ（フォルダ prefix）述語。`$prefixes` 空＝全体。
# S3（K9-K11）以降、ノードは全てファイル由来（コード＝Pass1/Pass2・Document＝Pass3 言及元）で
# `path` を必ず持つ——`path` 無し概念ノード（旧 L/REALIZES 由来の Parameter 等）はもう作られない
# ため、`path` prefix 一致だけに簡約する（旧・概念ノード用の scope_path/top_scope 分岐は撤去）。
def _scope_pred(var: str) -> str:
    return (f"(size($prefixes)=0 OR any(pref IN $prefixes WHERE "
            f"{var}.path IS NOT NULL AND ({var}.path=pref OR {var}.path STARTS WITH pref+'/')))")


# secRV 範囲外是正（2026-07-19・影響分析の Neo4j 安全弁＝timeout＋緊急天井・fail-loud＝偽陰性防止・
# LIMIT不使用がユーザー決定）: 直前に `sherpa/lens_service.py`（近傍探索・補助情報）へ同種の安全弁
# （`_run_capped`）を実装済みだが、**本丸の影響分析（`world_impact`/`resolve_world_entity`・本モジュール）は
# lens_service を通らない**ため、ここに同じ道具立て（per-query timeout・ストリーム反復・緊急天井・
# Cypher に LIMIT は入れない＝網羅性維持）を複製する。ただし**縮退の意味は逆**にする:
# lens_service（近傍候補・補助情報）は timeout/天井到達を空リストへ黙って縮退させてよいが、影響分析は
# 「空＝影響なし」と誤読される偽陰性が致命的（部分的な影響一覧は「網羅した」と誤読される）。
# そのため、ここでは timeout・天井到達のどちらも
# `GraphQueryOverloadError` を**必ず raise**し、部分結果や空を黙って返さない（呼び出し側
# `impact_service`/`routers/impact.py`/`chat_service` が受け止めてユーザーへ可視化する）。


class GraphQueryOverloadError(RuntimeError):
    """Neo4j 読み取りクエリが安全弁（timeout／緊急天井）で打ち切られたことを示す（fail-loud）。

    `reason`＝`"timeout"` または `"too_many_rows"`。`world`＝対象 world_id。`rows`＝天井到達時の
    行数上限（timeout の場合は None）。影響分析はこの例外を空リストや部分結果へ握り潰してはならない
    （呼び出し側でユーザーへ「範囲を絞って再実行」等の平文エラーとして可視化する）。
    """

    def __init__(self, reason: str, *, world: str, rows: int | None = None):
        self.reason = reason
        self.world = world
        self.rows = rows
        detail = f" rows>={rows}" if rows is not None else ""
        super().__init__(f"neo4j query overload ({reason}): world={world}{detail}")


# ユーザー向け平文メッセージ（専門用語ゼロ・docs/04-画面の原則.md §5/§6）。呼び出し側
# （routers/impact.py の 503・chat_service のチャット縮退）が同じ文言を共有する（表記ゆれ防止）。
GRAPH_OVERLOAD_USER_MESSAGE = (
    "対象が大きすぎるか、グラフ検索が時間内に終わりませんでした。範囲（フォルダ）を絞って再実行してください。"
)


# rv-s3-removal（Codex RV HIGH・縮小形）: K13 語彙撤去後に**再構築前の旧 Neo4j グラフ**を読むと、
# 「旧 LLM 由来エッジの混入」や「もっともらしい影響なし」を正常応答として返してしまう。
# 「last_sig 不一致で 503」（Codex 案）は不採用——鏡モデルでは原本変更〜次 sync の署名差は
# 正常運転（最後に取り込んだ状態で答えるのが契約）。代わりに、グラフを構築した時の
# スキーマ世代（`GRAPH_SCHEMA_ERA`）を Neo4j 側へ保存し（`load_world`）、現行コードの世代と
# 異なる場合**だけ**（＝コード側のグラフ内部形式が変わったのに再取り込みが済んでいない場合だけ）
# 読み取りを明示エラーにする。
class GraphSchemaEraError(RuntimeError):
    """保存済み world グラフのスキーマ世代が現行コードと不一致（＝旧世代の実データを読んでいる）。

    `world`＝対象 world_id。`stored_era`＝グラフに保存されていた世代（`None`＝世代スタンプ自体が
    無い＝`load_world` が現行コード以前に作った旧世代のグラフ）。`lens`（省略可）＝分かっている
    範囲でのチャットレンズ名（"impact"/"troubleshoot"・呼び出し元が分かる場合のみ添える・
    `check_schema_era` 参照）。`GraphQueryOverloadError` と同じ fail-loud 契約——呼び出し側は
    空/部分結果へ握り潰さず、ユーザーへ「再取り込みが必要」の明示エラーとして可視化すること。
    """

    def __init__(self, world: str, stored_era: str | None, *, lens: str | None = None):
        self.world = world
        self.stored_era = stored_era
        self.lens = lens
        super().__init__(
            f"graph schema era mismatch: world={world} stored={stored_era!r} current={GRAPH_SCHEMA_ERA!r}")


# ユーザー向け平文メッセージ（専門用語ゼロ・docs/04-画面の原則.md §5/§6）。GRAPH_OVERLOAD_USER_MESSAGE
# と同じく呼び出し側（routers/impact.py・routers/graph.py の 503・chat_service のチャット縮退）が共有する。
GRAPH_SCHEMA_ERA_USER_MESSAGE = (
    "この資料フォルダは再取り込みが必要です（内部形式が更新されました）。管理者にご連絡ください。"
)


def check_schema_era(session, world: str, *, lens: str | None = None) -> None:
    """world の保存済みスキーマ世代を確認する（fail-loud・rv-s3-removal）。

    グラフに実データ（`world_id` を持つ `:Entity`）が無い world は対象外——ゲートは
    「**旧世代の実データがある**」場合だけ発動し、未投入 world の既存の空応答/既存エラーは
    変えない。実データがあり、かつ保存世代（`:SherpaMeta{world_id}.schema_era`）が現行
    `GRAPH_SCHEMA_ERA` と不一致（未保存＝スタンプの無い旧世代グラフも含む）なら
    `GraphSchemaEraError` を raise する。

    グラフ読み取りの入口（`world_impact`/`resolve_world_entity`/`lens_service.neo4j_related`/
    `graph_admin.graph_search`）がそれぞれ1回呼ぶ——per-query の追加ラウンドトリップにはなるが
    1クエリで完結させており（count と meta を同一クエリで取得）、20人規模の運用では
    最適化不要（過剰最適化しない・タスク前提）。呼び出し位置は各関数の**主クエリの後**（本関数自体は
    `GraphQueryOverloadError` を扱わない・下記参照）。

    世代プローブ自体が失敗（timeout/接続断等の `Neo4jError`）した場合はゲートを発動せず黙って戻る
    ——それは Neo4j 側の別の問題で、呼び出し元の主クエリが既に fail-loud（`_run_read_capped`）か
    縮退（`lens_service._run_capped`）のどちらかで扱っている。ここで新しい失敗モードを増やさない。
    """
    try:
        rows = session.run(
            "OPTIONAL MATCH (n:Entity {world_id:$w}) "
            "WITH count(n) AS c "
            "OPTIONAL MATCH (m:SherpaMeta {world_id:$w}) "
            "RETURN c, m.schema_era AS era",
            w=world,
        ).data()
    except Neo4jError:
        return
    row = rows[0] if rows else None
    if not row or not row.get("c"):
        return
    era = row.get("era")
    if era != GRAPH_SCHEMA_ERA:
        raise GraphSchemaEraError(world, era, lens=lens)


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """security-limit 系 env の整数解析（`lens_service._env_int`/`agentic_search._env_int` と同一セマンティクス）。

    ここに複製する理由: `lens_service` は本モジュール（`world_neo4j`）から `_scope_pred` を
    import している＝本モジュールは lens_service より**下位**の層。ここで `from ..lens_service
    import _env_int` と import すると循環 import になる（lens_service → world_neo4j → lens_service）。
    関数自体は6行の極小ヘルパーなので、同型のものをここに複製する（セマンティクスは完全に同一）。

    負値/非整数、および範囲 [lo, hi] 外の値は既定へフォールバックする（既定値自体も [lo, hi] へ
    クランプ）。運用者の誤設定で機能を止めない（起動は継続）。
    """
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


# per-query タイムアウト（秒）。既定30・[1,600] にクランプ。lens_service と**同じ env 変数を共用**
# （安全弁のチューニングを1箇所の設定で両モジュールに効かせる）。
_NEO4J_QUERY_TIMEOUT_S = _env_int("SHERPA_NEO4J_QUERY_TIMEOUT_S", 30, 1, 600)
# ストリーム反復の緊急天井（行数）。既定10000・[100,1000000] にクランプ（同上・env 変数も同じ）。
_NEO4J_MAX_ROWS = _env_int("SHERPA_NEO4J_MAX_ROWS", 10000, 100, 1_000_000)
# 影響たどり（world_impact）の既定深さ。範囲外・不正値は既定8へ復帰（[1,64]）。
# `impact_service.run_impact`／`search_service._search_graph` の depth 既定はこの値そのもの
# （call 側から明示 depth が渡されない限りこの既定が効く）。`sherpa.ext_api.ExtSearchReq.depth` の
# 上限（le）は元の外部 API 契約 `12` を後退させない `max(12, IMPACT_MAX_DEPTH)`（env で12を超えて
# 広げたときだけ上限も広がる・下げる方向には動かさない）。
IMPACT_MAX_DEPTH = _env_int("SHERPA_IMPACT_MAX_DEPTH", 8, 1, 64)

# GRAPH-MEM（2026-09-04・机上見積もり=目標規模20〜50万ノード同オーダーのエッジ）: `load_world` の
# ノード/エッジ投入を UNWIND バッチへ分割する行数。既定5000・[1,1000000] にクランプ。
# 根拠: 1行（ノード/エッジ1件）のプロパティは高々数百バイト＝5000行で数百KB〜数MB程度。UNWIND の
# パラメータはドライバ側で Bolt PackStream へ直列化される際に一時的に元の Python オブジェクトと
# 直列化後バイト列が両方メモリに乗る（同量の一時的な二重化）——バッチを割らず全ノード/全エッジを
# 1回の UNWIND に積むと、このピークが world 全体サイズ（数GB級）に比例してしまう。5000は
# ラウンドトリップ回数（小さすぎると遅い）とバッチ内二重化ピーク（大きすぎるとまた大きくなる）の
# 折衷値。世代管理・原子性はバッチ数に依存させない（すぐ下の `load_world` docstring 参照）。
_NEO4J_BATCH_ROWS = _env_int("SHERPA_NEO4J_BATCH_ROWS", 5000, 1, 1_000_000)


def _batched(seq: list, n: int):
    """`seq` を `n` 件ずつのリストへ分割する（最後だけ短い）。バッチ UNWIND の共通ヘルパー。"""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# タイムアウト由来のサーバエラーコードを緩く判定する（専用の例外クラスが無いため・lens_service と同じ
# 判定ロジックの同型複製＝循環 import 回避）。実例:
# `Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration`（クライアント指定タイムアウト）。
_TIMEOUT_CODE_RE = re.compile(r"timedout|timeout", re.IGNORECASE)


def _is_query_timeout(exc: Neo4jError) -> bool:
    """Neo4j サーバエラーが**クエリ/トランザクションのタイムアウト**によるものか判定する。"""
    code = getattr(exc, "code", "") or ""
    return bool(_TIMEOUT_CODE_RE.search(str(code)))


def _run_read_capped(session, cypher: str, *, world: str, **params) -> list[dict]:
    """読み取り専用 Cypher を安全弁つきで実行する（**影響分析向け・fail-loud**）。

    `lens_service._run_capped` と同じ道具立て（`neo4j.Query(cypher, timeout=...)` でper-query
    タイムアウト・`.data()` 一括展開でなくストリーム反復・`_NEO4J_MAX_ROWS` 行の緊急天井・Cypher に
    LIMIT は入れない＝網羅性維持）だが、**縮退が逆**: timeout・天井到達のどちらも空/部分結果へ
    黙って縮退させず `GraphQueryOverloadError` を raise する（呼び出し元が「空＝影響なし」と
    誤読しないよう、必ず呼び出し側にエラーとして伝える）。

    `world` は主にログ/例外メッセージ用途だが、`session.run` へもそのまま渡す（Cypher が `$world` を
    参照する呼び出し（`world_impact`）では実引数として機能し、`$w` 等の別名を使う呼び出し
    （`resolve_world_entity`/`presumed_impact`）では未参照パラメータとして無害に無視される＝Neo4j は
    Cypher が参照しない余分なパラメータをエラーにしない）。

    secRV 範囲外是正 追補（2026-07-19・RV指摘 HIGH-1）: `lens_service._run_capped` と同じ理由で、
    天井到達時は raise する**前**に `result.consume()` を呼び、未消費の Result を残さない（neo4j
    driver 6.2.0 は同じ session で次の `session.run()` を呼ぶと前の未消費 Result の残りを全件
    fetch/buffer するため、raise 後に呼び出し元が同一 session で別クエリを流すと安全弁が逆流する）。
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
    """D3（2026-09-02-RAG表現の全形式展開と文脈保持.md §5.2）: entity/relation の出所
    （`chunk_id`/`locator`/`logical_record_id` を持つ dict のリスト）を Neo4j プロパティへ持てる形に
    直列化する。Neo4j のプロパティ値はプリミティブ/プリミティブ配列限定で、マップの配列は直接
    持てない——リストごと1本の JSON 文字列にする。空/無ければ None（プロパティを立てない）。"""
    if not sources:
        return None
    return json.dumps(sources, ensure_ascii=False)


def _node_row(n: dict) -> dict:
    """1ノード分の UNWIND 行（`world_id` はバッチ全体で共通なのでクエリの `$world` 側に出す・
    行ごとの重複を避ける。`sources` は JSON 文字列化済み）。"""
    return {
        "cid": n["cid"], "name": n["name"],
        "top": n.get("top_scope"), "phase": n.get("phase"), "cat": n.get("category"),
        "path": n.get("path"), "sp": n.get("scope_path"), "value": n.get("value"),
        "em": n.get("extraction_method", "static"), "status": n.get("status", "active"),
        "analyzer": n.get("analyzer"),
        "sources": _sources_json(n.get("sources")),
        "sources_overflow": n.get("sources_overflow_count", 0),
    }


def _edge_row(e: dict) -> dict:
    """1エッジ分の UNWIND 行。`via` は CODE-2（`RefCandidate.extra["via"]` 由来・属性の追加のみ）。"""
    return {
        "src": e["src"], "dst": e["dst"], "doc": e.get("doc", ""),
        "line": e.get("line", 0), "em": e.get("extraction_method", "static"),
        "status": e.get("status", "active"),
        "source": e.get("source"), "evidence": e.get("evidence"), "rule": e.get("rule"),
        "sources": _sources_json(e.get("sources")),
        "sources_overflow": e.get("sources_overflow_count", 0),
        "via": e.get("via"),
    }


def load_world(nodes, edges, world_id, uri, user, password):
    """world グラフ（dict）を Neo4j に**クリーン rebuild**（当該 world を削除→全ロードを1 write tx で原子化）。

    削除と再ロードを同一 tx にして、途中失敗で live グラフを空/部分にしない（neo4j_load.reload と同方針・RV High）。
    schema コマンドはデータ tx と混ぜられないので先に流す。

    rv-s3-removal: 同じ tx の最後に `GRAPH_SCHEMA_ERA`（`:SherpaMeta{world_id}.schema_era`）を
    刻む——グラフを構築した時のスキーマ世代を保存し、読み取り側（`check_schema_era`）が現行
    コードの世代と比較できるようにする（旧世代のまま再取り込みされていない world を fail-loud
    で検知するため）。

    GRAPH-MEM（2026-09-04）: ノード/エッジの投入は `_NEO4J_BATCH_ROWS` 件ずつの **UNWIND バッチ**へ
    分割して送る（ラベル/エッジ型は Cypher に直埋め＝1バッチ=1ラベル or 1エッジ型・許容語彙は事前検証済み）。
    **原子性は変えない**——バッチはすべて同一の明示 write tx（`s.execute_write(_apply)`）の中で送るので、
    途中のバッチが例外を投げれば tx 全体がロールバックされ（neo4j driver の managed transaction の挙動）、
    旧グラフがそのまま残る（本レーンで検討した代替案＝複数 write tx ＋完了マーカーは不採用。世代管理
    （`last_sig`）は Neo4j 反映が成功したか否かでしか判定しておらず、反映途中を
    クエリ側が読める設計ではない——複数 tx にすると、途中経過（半分削除・半分再構築）が
    並行の影響検索から**直接見えてしまう**窓ができ、fail-loud 前提を壊す。1 tx のままバッチ送信は
    その窓を作らずに、ドライバ側のパラメータ直列化ピーク（バッチ1回分＝最大 `_NEO4J_BATCH_ROWS` 行）
    だけを削る。バッチ送信後は次の反復でリスト参照を再代入する（前バッチの行リストは即 GC 対象）。

    `sources`（D3・`world_graph._add_source` が蓄積した出所リスト）は JSON 文字列として
    `x.sources`/`r.sources` に、上限超過件数は `x.sources_overflow_count`/`r.sources_overflow_count`
    にそのまま乗せる（属性の追加のみ・ノード/エッジ種別は増やさない）。
    """
    from neo4j import GraphDatabase  # 遅延 import（解析だけなら不要）

    # label/edge type は Cypher に直埋めするので、書込 tx の前に**閉じた語彙で検証**（fail-closed・RV Med#1）。
    bad_n = {n["label"] for n in nodes if n["label"] not in NODE_LABELS}
    bad_e = {e["type"] for e in edges if e["type"] not in WORLD_EDGE_TYPES}
    if bad_n or bad_e:
        raise ValueError(f"未知の語彙はロードしない: labels={sorted(bad_n)} edges={sorted(bad_e)}")

    # ラベル/エッジ型ごとにグルーピング（Cypher の `SET x:\`Label\`` はラベル名をパラメータ化できない
    # ＝グループ単位でしか UNWIND バッチにできない）。ここでの分割は参照のリストなので中身のコピーはしない。
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
                        f"x.scope_path=row.sp, x.value=row.value, x.extraction_method=row.em, "
                        f"x.status=row.status, x.analyzer=row.analyzer, x.sources=row.sources, "
                        f"x.sources_overflow_count=row.sources_overflow"
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
                        "r.extraction_method=row.em, r.status=row.status, "
                        "r.source=row.source, r.evidence=row.evidence, r.rule=row.rule, "
                        "r.sources=row.sources, r.sources_overflow_count=row.sources_overflow, "
                        "r.via=row.via"
                    )
                    for batch in _batched(items, _NEO4J_BATCH_ROWS):
                        rows = [_edge_row(e) for e in batch]
                        tx.run(edge_cypher, rows=rows, world=world_id)
                # rv-s3-removal: このロードが作った world グラフのスキーマ世代を刻む
                # （同一 tx＝rebuild と不可分。`:Entity` とは別ラベルなので上の DETACH DELETE の
                # 対象に入らない——MERGE で世代を上書きするだけでよい）。
                tx.run("MERGE (m:SherpaMeta {world_id:$w}) SET m.schema_era=$era",
                      w=world_id, era=GRAPH_SCHEMA_ERA)
                return len(nodes), len(edges)
            return s.execute_write(_apply)
    finally:
        driver.close()


def delete_world(world_id, uri, user, password) -> int:
    """その world の全ノード（と接続辺）を削除（rebind/delete の wipe・`world_id` 単位）。

    `SherpaMeta`（スキーマ世代スタンプ・rv-s3-removal）も同じクエリで合わせて削除する——`:Entity`
    とは別ラベルのため素の `MATCH (n:Entity ...)` の対象には入らず、放置すると同じ world_id を
    後で再利用したとき古い世代スタンプが残る。戻り値は削除ノード総数（Entity＋あれば SherpaMeta
    1件・呼び出し元は件数を厳密検証しない集計値としてのみ使う）。
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
    """**孤児グラフの自動掃除**: グラフに残る world_id のうち、登録 world に無いものを `DETACH DELETE`。

    `valid_worlds`＝確実に取得できた登録 world id 集合（fail-safe は呼出側）。返り値＝削除した world_id 一覧。
    Neo4j 不可は []（best-effort・個別失敗は次回に再試行）。
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
                    # rv-s3-removal: `SherpaMeta`（スキーマ世代スタンプ）も同じクエリで一緒に消す
                    # （`delete_world` と同じ理由・別ラベルなので `n:Entity` 単独の MATCH には入らない）。
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
    """起点語 → 起点 canonical_id 群（名前一致・範囲内）。

    impact_service.resolve_entity の world 版。`scope_prefixes` で起点も範囲に絞る（範囲外の同名は起点にしない）。
    業務語→コードの橋渡し（旧 REALIZES）は撤去済み（K10）——業務語の入口はクエリ時のエージェントが
    文書を grep してコード名を発見する経路に委ねる（§2）。

    rv-s3-removal: `check_schema_era` を呼ぶ（旧世代の実データがある world は `GraphSchemaEraError`
    で fail-loud）。主クエリの**後**に呼ぶ——主クエリ自体の安全弁（`_run_read_capped` の
    timeout/緊急天井＝`GraphQueryOverloadError`）を先に効かせるため（結果を返す直前の最終ゲート）。
    """
    prefixes = list(scope_prefixes or [])
    rows = _run_read_capped(
        session,
        "MATCH (n:Entity {world_id:$w}) WHERE n.name=$name "
        "  AND ($incl OR coalesce(n.status,'active')='active') "
        f"  AND {_scope_pred('n')} "
        "RETURN n.canonical_id AS cid, [l IN labels(n) WHERE l<>'Entity'][0] AS label, n.name AS name",
        world=world_id, w=world_id, name=term, incl=include_deprecated, prefixes=prefixes,
    )
    check_schema_era(session, world_id, lens="impact")
    return [{"canonical_id": r["cid"], "label": r["label"], "name": r["name"]} for r in rows]


def world_impact(session, start_cids, world_id, scope_prefixes=None, depth=IMPACT_MAX_DEPTH,
                 include_deprecated=False):
    """正準 Cypher（範囲フィルタ付き）で影響ノードを引き、構造化 item を返す（impact_service.neo4j_impact の world 版）。

    範囲：start・affected・**経路の全ノード**が `world_id` ＋ `scope_prefixes` 内（in-graph の `subgraph` 同義）。
    骨格エッジ（`_IMPACT_REL`＝COPIES/CONTAINS/INVOKES/ACCESSES）のみを辿る決定的な構造たどり——
    K12（2026-09-04-グラフのソース正典化.md §4）: 「確実/要確認」の二重クエリ・判定表示は
    機構ごと撤去（全件同格）。1本の Cypher で足りる（旧 all-static 判定クエリは撤去）。

    rv-s3-removal: `check_schema_era` を呼ぶ（`resolve_world_entity` と同じゲート・呼び出し元が
    別でも二重に守る）。`resolve_world_entity` と同じ理由で主クエリの**後**に呼ぶ。
    """
    # "world" キーは params に含めない＝ `_run_read_capped` の専用キーワード引数（world=world_id）から
    # `session.run` へ渡す（衝突回避・関数 docstring 参照）。
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
    """items へ `importance`/`importance_reason`/`importance_mixed` を条件付きで付与する（I2・J3・
    rv-oom-resume item7・2026-09-05）。

    候補＝各 item の `path`（自身の所属文書）∪ `evidence[].doc`（根拠の来歴文書）。**未設定
    （`_重要度.txt` の解決が無い候補）は「中」と同格の順位**（`importance.RANK_UNSET`・
    `importance.rank_of` 参照＝`grep_tool` のヒット優先順位と同じスケール）として最高位計算
    （どの候補が勝つか）にも `importance_mixed` の判定にも含める——旧実装は未解決の候補を
    候補集合から単純に除外していたため、「自身は無印（中相当）だが根拠の中にたまたま `低`
    指定の文書が1つ混じっている」ような item が、無印候補の存在を無視して誤って `低` 表示に
    なる/ならないが候補の順序に左右される穴があった。

    ただし**最高位に達した候補の中に実際の `Resolution`（`_重要度.txt` で明示解決された候補）が
    1つも無ければ、`importance`/`importance_reason`/`importance_mixed` のいずれも付けない**——
    未設定候補が「たまたま」最高位を取っただけなら、表示できる実在の判定根拠が無いため
    （§2 truth table＝「無ければ無い」を、集合全体の最高位でも守る）。最高位に複数の実 Resolution
    が同着する場合は `path` 優先→`evidence` 出現順（候補の元の並び順）で先頭のものを採る
    （同じ rank の Resolution は `value` が必然的に同一——`RANK` は値からの一対一写像——なので
    表示値自体は候補の選び方に左右されない）。

    `_重要度.txt` の無い world（`resolve_for_world` が空 dict）は即座に戻り、items は無改変
    （受け入れ条件＝影響一覧の出力完全不変）。
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
    """resolve_world_entity → world_impact → 構造化結果（emit_result 形・範囲つき）。"""
    starts = resolve_world_entity(session, term, world_id, scope_prefixes, include_deprecated)
    items = world_impact(session, [s["canonical_id"] for s in starts], world_id,
                         scope_prefixes, depth, include_deprecated)
    # I2（J3）: 第1ソートキーは重要度（`高`>`中`/未設定>`低`・`world_impact` が付けた `importance`
    # 表示値から導く・§K12 で「確実/要確認」の第1キーが消えた後継）。`_重要度.txt` の無い world は
    # 全 item の rank が揃う（`importance.RANK_UNSET`）ため、旧ソート（category, name のみ）と
    # 完全に同じ順序になる（受け入れ条件）。
    items.sort(key=lambda x: (-importance.RANK.get(x.get("importance"), importance.RANK_UNSET),
                              x["category"], x["name"]))
    return {"type": "impact", "world_id": world_id, "scope_prefixes": list(scope_prefixes or []),
            "start": term, "include_deprecated": include_deprecated, "starts": starts, "items": items}


def default_neo4j_uri() -> str:
    """Neo4j 接続 URI。NEO4J_URI の明示（別ホスト向け）が最優先、無ければ compose の公開ポート変数
    SHERPA_NEO4J_BOLT_PORT（docker-compose.yml と共用の 1 変数）に追随する（2026-08-18・ポートは 1 か所で決める）。"""
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
