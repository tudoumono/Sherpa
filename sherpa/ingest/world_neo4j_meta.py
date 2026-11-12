"""グラフを作ったときに `:SherpaMeta` へ刻んだ値（失敗したプラグイン・ビルドの報告・世代・グラフに入れなかったソース）の読み取りと、実物との照合。読み取り専用。"""
from __future__ import annotations

import json

from . import world_neo4j


def read_plugin_failures(session, world_id) -> list:
    """その資料フォルダのグラフを作ったときに失敗した FW プラグイン（`[{plugin, files, why}]`・保存が無い旧グラフ・失敗なしは空）。範囲（フォルダ）では絞らない。"""
    rows = world_neo4j._run_read_capped(
        session, "MATCH (m:SherpaMeta {world_id:$w}) RETURN m.plugin_failures AS pf",
        world=world_id, w=world_id)
    if not rows or not rows[0].get("pf"):
        return []
    return json.loads(rows[0]["pf"])


def read_build_report(world_id) -> dict | None:
    """グラフを作ったときのビルドの定義の読み取りの報告。何も読まなかったグラフは空、グラフが無い・保存が無いときは None。接続の失敗は例外のまま伝える。"""
    from neo4j import GraphDatabase
    env = world_neo4j._env()
    driver = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]), connection_timeout=5)
    try:
        with driver.session() as s:
            rows = s.run("MATCH (m:SherpaMeta {world_id:$w}) RETURN m.build_report AS br", w=world_id).data()
    finally:
        driver.close()
    if not rows or rows[0].get("br") is None:
        return None
    info = json.loads(rows[0]["br"])
    return info if isinstance(info, dict) else None


def read_build_marks_sig(world_id) -> str | None:
    """グラフを作ったときに当てはめた印の値。グラフ（メタ）が無ければ None、印を当てはめた読み取りが無ければ空。接続の失敗は例外のまま伝える。"""
    from neo4j import GraphDatabase
    env = world_neo4j._env()
    driver = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]), connection_timeout=5)
    try:
        with driver.session() as s:
            rows = s.run("MATCH (m:SherpaMeta {world_id:$w}) RETURN m.build_marks_sig AS bms", w=world_id).data()
    finally:
        driver.close()
    return None if not rows else (rows[0].get("bms") or "")


def read_generation(session, world_id) -> str | None:
    """そのグラフの取り込みの世代（`load_world` が刻む `generation`）。作り直すたびに変わる。印のない旧グラフは件数から作った印、グラフが無ければ None。"""
    rows = world_neo4j._run_read_capped(
        session, "MATCH (m:SherpaMeta {world_id:$w}) "
                 "RETURN coalesce(m.generation,m.active_generation) AS g, "
                 "m.node_count AS nc, m.edge_count AS ec, m.schema_era AS era",
        world=world_id, w=world_id)
    if not rows:
        return None
    r = rows[0]
    return str(r["g"]) if r.get("g") else f"legacy-{r.get('era')}-{r.get('nc')}-{r.get('ec')}"


def read_ingest_no(session, world_id) -> int:
    """そのグラフを作ったときの取り込みの番号（`load_world` が刻む `ingest_no`）。番号が付く前のグラフ・グラフが無いときは 0。"""
    rows = world_neo4j._run_read_capped(
        session, "MATCH (m:SherpaMeta {world_id:$w}) RETURN m.ingest_no AS n", world=world_id, w=world_id)
    return int(rows[0]["n"] or 0) if rows else 0


def read_unparsed_sources(session, world_id) -> dict | None:
    """そのグラフを作ったときにグラフへ入れなかったソースのファイル数 `{syntax, size_exceeded}`。保存が無い旧グラフは None（不明・申告しない）。範囲（フォルダ）では絞らない。"""
    rows = world_neo4j._run_read_capped(
        session, "MATCH (m:SherpaMeta {world_id:$w}) RETURN m.unparsed_sources AS us",
        world=world_id, w=world_id)
    if not rows or not rows[0].get("us"):
        return None
    info = json.loads(rows[0]["us"])
    return info if isinstance(info, dict) else None


def check_graph_counts(world_id, uri, user, password, build_marks_sig: str = "") -> str | None:
    """保存済み `:SherpaMeta` の世代・件数スタンプを実際のグラフ内容と照合する（`load_world` が刻む値と対）。

    作り直しが要る理由コードを返す。整合していれば `None`。
    - `"no_stamp"`: `node_count`/`edge_count` が無い（`:SherpaMeta` 自体が無い場合を含む）
    - `"era_mismatch"`: 保存済み `schema_era` が現行 `world_neo4j.GRAPH_SCHEMA_ERA` と不一致
    - `"count_mismatch"`: スタンプ済み件数と実物（`:Entity{world_id}` の数・その資料フォルダのノードから出る `r.world_id` 一致リレーションの数）が違う
    - `"build_marks_changed"`: ビルドの定義を読む場所の印が、グラフを作ったときの印と違う（`build_marks_sig`）
    ドライバは自前で開閉し、接続/クエリ例外はそのまま呼び出し元へ伝播する。
    """
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as s:
            rows = s.run(
                "MATCH (m:SherpaMeta {world_id:$w}) "
                "RETURN m.schema_era AS era, m.node_count AS nc, m.edge_count AS ec, "
                "m.active_generation AS generation, m.build_marks_sig AS bms",
                w=world_id,
            ).data()
            meta = rows[0] if rows else None
            if not meta or meta.get("nc") is None or meta.get("ec") is None:
                return "no_stamp"
            if meta.get("era") != world_neo4j.GRAPH_SCHEMA_ERA:
                return "era_mismatch"
            generation = meta.get("generation")
            actual_nc = s.run(
                "MATCH (n:Entity {world_id:$w}) "
                "WHERE ($g IS NULL AND n.graph_generation IS NULL) OR n.graph_generation=$g "
                "RETURN count(n) AS c", w=world_id, g=generation
            ).single()["c"]
            actual_ec = s.run(
                "MATCH (a:Entity {world_id:$w})-[r]->() WHERE r.world_id=$w "
                "AND (($g IS NULL AND a.graph_generation IS NULL AND r.graph_generation IS NULL) "
                "OR (a.graph_generation=$g AND r.graph_generation=$g)) RETURN count(r) AS c",
                w=world_id, g=generation,
            ).single()["c"]
            if actual_nc != meta["nc"] or actual_ec != meta["ec"]:
                return "count_mismatch"
            if (meta.get("bms") or "") != (build_marks_sig or ""):
                return "build_marks_changed"
            return None
    finally:
        driver.close()
