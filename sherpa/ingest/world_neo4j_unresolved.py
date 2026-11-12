"""グラフに保存した未解決の参照（どこにも定義の無い名前）を、点検のために名前ごとに集める。読み取り専用。
設計: docs/design/scope.md「期の中の参照の張り方と名前の台帳」
"""
from __future__ import annotations

import json
import logging

from . import name_rows, world_neo4j

_log = logging.getLogger("sherpa")

_CYPHER = (
    "MATCH (m:SherpaMeta {world_id:$w}) WITH m AS gm, m "
    "OPTIONAL MATCH (n:Entity {world_id:$w}) WHERE n.unresolved IS NOT NULL AND " + world_neo4j._generation_pred("n") + " "
    "RETURN m.unresolved_stored AS stored, "
    "  [x IN collect(n) WHERE x IS NOT NULL | {path:x.path, u:x.unresolved, ov:coalesce(x.unresolved_overflow_count,0)}] AS nodes")


def _collect(rows: list) -> dict | None:
    if not (rows and rows[0].get("stored")):
        return None   # 申告を保存していない旧いグラフ
    names: dict = {}
    overflow = 0
    for node in rows[0].get("nodes") or []:
        overflow += int(node.get("ov") or 0)
        for it in json.loads(node.get("u") or "[]"):
            if it.get("reason") == "unresolved" and it.get("kind") in name_rows.PROGRAM_KINDS and it.get("name"):
                names.setdefault(name_rows.normalize_name(it["name"]), []).append(node["path"])
    return {"names": {k: sorted(set(v)) for k, v in names.items()}, "overflow": overflow}


def read_unresolved_names(world: str) -> dict | None:
    """`{names: {正規化した名前: [参照元のパス]}, overflow: 保存の上限で数えなかった参照の件数}` を返す。
    対象は、どこにも定義が無い（理由が `unresolved`）プログラム・部品の参照だけ。グラフを読めない・申告が保存されていないときは None（呼び出し側が断る）。
    """
    from neo4j import GraphDatabase
    env = world_neo4j._env()
    driver = None
    try:
        driver = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
        with driver.session() as s:
            return _collect(world_neo4j._run_read_capped(s, _CYPHER, world=world, w=world))
    except Exception:
        _log.warning("点検: 未解決の参照をグラフから読めませんでした world=%s", world, exc_info=True)
        return None
    finally:
        if driver is not None:
            driver.close()
