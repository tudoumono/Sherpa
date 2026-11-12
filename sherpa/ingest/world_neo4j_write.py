"""グラフの保存世代の書き込みと削除（`load_world`・`delete_world` が使う）。"""
from __future__ import annotations

from . import world_neo4j


def _delete_inactive_generations(session, world_id: str, *, keep_generation: str | None) -> int:
    """`keep_generation` 以外の保存世代を小分けに削除する。None は旧形式を残して作りかけだけ消す。"""
    deleted = 0
    while True:
        def _apply(tx):
            return tx.run(
                "MATCH (n:Entity {world_id:$w}) "
                "WHERE ($keep IS NULL AND n.graph_generation IS NOT NULL) "
                "   OR ($keep IS NOT NULL AND (n.graph_generation IS NULL OR n.graph_generation<>$keep)) "
                "WITH n LIMIT $lim DETACH DELETE n RETURN count(n) AS c",
                w=world_id, keep=keep_generation, lim=world_neo4j._NEO4J_BATCH_ROWS,
            ).single()["c"]
        count = world_neo4j._write_world(session, _apply)
        deleted += count
        if count < world_neo4j._NEO4J_BATCH_ROWS:
            return deleted


def _write_generation(s, world_id: str, generation: str, nodes_by_label: dict, edges_by_type: dict, step) -> None:
    """新しい世代のノードと辺を `_NEO4J_BATCH_ROWS` 件ずつ別 tx で書く（まだ公開しない）。"""
    loaded = 0
    for label, items in nodes_by_label.items():
        node_cypher = (
            "UNWIND $rows AS row "
            "MERGE (x:Entity {storage_id: row.storage_id}) "
            f"SET x:`{label}`, x.canonical_id=row.cid, x.name=row.name, x.world_id=$world, "
            "x.graph_generation=row.generation, "
            "x.top_scope=row.top, x.phase=row.phase, x.category=row.cat, x.path=row.path, "
            "x.scope_path=row.sp, x.value=row.value, x.membership_paths=row.membership_paths, "
            "x.status=row.status, x.analyzer=row.analyzer, x.sources=row.sources, "
            "x.sources_overflow_count=row.sources_overflow, x.jcl_kind=row.jcl_kind, "
            "x.schema=row.schema, x.qualified_name=row.qualified_name, "
            "x.unresolved=row.unresolved, x.unresolved_names=row.unresolved_names, "
            "x.unresolved_overflow_count=row.unresolved_overflow, x.period=row.period, x.period_place=row.period_place"
        )
        for batch in world_neo4j._batched(items, world_neo4j._NEO4J_BATCH_ROWS):
            rows = [world_neo4j._node_row(n, generation, world_id) for n in batch]
            world_neo4j._write_world(s, lambda tx, q=node_cypher, r=rows: tx.run(q, rows=r, world=world_id))
            loaded += len(batch)
            step.tick(loaded)
    for etype, items in edges_by_type.items():
        edge_cypher = (
            "UNWIND $rows AS row "
            "MATCH (a:Entity {world_id:$world, graph_generation:row.generation, canonical_id:row.src}), "
            "      (b:Entity {world_id:$world, graph_generation:row.generation, canonical_id:row.dst}) "
            f"MERGE (a)-[r:`{etype}`]->(b) "
            "SET r.world_id=$world, r.graph_generation=row.generation, r.doc=row.doc, r.line=row.line, "
            "r.status=row.status, r.sources=row.sources, r.sources_overflow_count=row.sources_overflow, "
            "r.via=row.via, r.binding_paths=row.binding_paths, r.confirm=row.confirm, r.assumption=row.assumption"
        )
        for batch in world_neo4j._batched(items, world_neo4j._NEO4J_BATCH_ROWS):
            rows = [world_neo4j._edge_row(e, generation) for e in batch]
            world_neo4j._write_world(s, lambda tx, q=edge_cypher, r=rows: tx.run(q, rows=r, world=world_id))
            loaded += len(batch)
            step.tick(loaded)


def _delete_world_entities(session, world_id: str) -> int:
    """資料フォルダの全保存世代を小分けに削除する。"""
    deleted = 0
    while True:
        def _apply(tx):
            return tx.run(
                "MATCH (n:Entity {world_id:$w}) WITH n LIMIT $lim "
                "DETACH DELETE n RETURN count(n) AS c",
                w=world_id, lim=world_neo4j._NEO4J_BATCH_ROWS,
            ).single()["c"]
        count = world_neo4j._write_world(session, _apply)
        deleted += count
        if count < world_neo4j._NEO4J_BATCH_ROWS:
            return deleted


def read_swap_state(world_id: str) -> dict | None:
    """公開中のグラフのメタ `{ingest_no, settings_sig, node_count, edge_count}`（メタが無ければ None）。Neo4j に届かなければ例外にする。"""
    from neo4j import GraphDatabase
    env = world_neo4j._env()
    driver = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
    try:
        with driver.session() as s:
            rec = s.run("MATCH (m:SherpaMeta {world_id:$w}) RETURN m.ingest_no AS no, m.settings_sig AS sig, "
                        "m.node_count AS nc, m.edge_count AS ec", w=world_id).single()
    finally:
        driver.close()
    return None if rec is None else {"ingest_no": int(rec["no"] or 0), "settings_sig": rec["sig"],
                                      "node_count": rec["nc"], "edge_count": rec["ec"]}


def drop_inactive(world_id: str) -> None:
    """公開中でない保存世代（前に止まった入れ替えの旧世代・作りかけ）を消す。公開中の世代には触れない。"""
    from neo4j import GraphDatabase
    env = world_neo4j._env()
    driver = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
    try:
        with driver.session() as s:
            active = s.run("MATCH (m:SherpaMeta {world_id:$w}) RETURN m.active_generation AS g", w=world_id).single()
            _delete_inactive_generations(s, world_id, keep_generation=active["g"] if active else None)
    finally:
        driver.close()
