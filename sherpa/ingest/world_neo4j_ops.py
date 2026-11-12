"""旧版へ戻す前にグラフを空にする運用の道具（取り込みが動いていないときに人が流す）。"""
from __future__ import annotations

from . import world_neo4j


def reset_graph_for_downgrade(uri, user, password, worlds=None) -> dict:
    """世代で絞らない旧版へ戻す前に、`:Entity` を小分けに全部消し、`:SherpaMeta`（世代の印）も消す。

    `worlds=None` は全部の資料フォルダ、リストならその `world_id` だけ（テスト用）。
    守ること: 旧版へ戻す前に流す。新旧 2 世代が残ったまま戻すと、旧版の検索に新旧が混ざり、一意制約の作成が重複で失敗する。
    消したあとは、旧版の次の取り込みがグラフを作り直す。戻り値は `{"entities": 削除ノード数, "metas": 削除メタ数}`。
    """
    from neo4j import GraphDatabase
    scope = "" if worlds is None else "WHERE n.world_id IN $worlds "
    meta_scope = "" if worlds is None else "WHERE m.world_id IN $worlds "
    extra = {} if worlds is None else {"worlds": list(worlds)}
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as s:
            entities = 0
            while True:
                def _apply(tx):
                    return tx.run(
                        f"MATCH (n:Entity) {scope}WITH n LIMIT $lim DETACH DELETE n RETURN count(n) AS c",
                        lim=world_neo4j._NEO4J_BATCH_ROWS, **extra,
                    ).single()["c"]
                count = world_neo4j._write_world(s, _apply)
                entities += count
                if count < world_neo4j._NEO4J_BATCH_ROWS:
                    break
            metas = world_neo4j._write_world(s, lambda tx: tx.run(
                f"MATCH (m:SherpaMeta) {meta_scope}DELETE m RETURN count(m) AS c", **extra,
            ).single()["c"])
            return {"entities": entities, "metas": metas}
    finally:
        driver.close()
