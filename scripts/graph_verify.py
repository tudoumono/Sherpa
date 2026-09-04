#!/usr/bin/env python3
"""実 Neo4j で v1 world を投入し、影響の golden を再現するか検証する（鏡モデル・要 `make up`）。

`world_graph.build_world` → `world_neo4j.load_world` → `run_world_impact` を通し、影響レンズの
golden（消費税率まわり）と precision 番人を確認する。テストのオラクル（正解）なのでテーマ名を含む。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sherpa import worlds                                          # noqa: E402
from sherpa.impact_service import run_impact                       # noqa: E402
from sherpa.ingest import world_graph, world_neo4j                 # noqa: E402

V = "v1"
GOLDEN = {"TAX-RATE", "TAXCALC", "BILLGEN", "SALESUP", "NIGHTLY",
          "請求機能", "売上計上機能", "見積機能", "消費税計算ルール", "請求書", "納品書", "売上日報"}
FORBIDDEN = {"顧客マスタ機能", "消費税法", "税率改定障害記録", "CUSTMNT"}


def main():
    try:
        from neo4j import GraphDatabase
    except ModuleNotFoundError:
        sys.exit("neo4j ドライバ未導入: pip install -r requirements.txt")

    wd = worlds.world_dir(V)
    nodes, edges, flags = world_graph.build_world(wd, V)
    env = world_neo4j._env()
    n, m = world_neo4j.load_world(nodes, edges, V, env["uri"], env["user"], env["pw"])
    print(f"loaded nodes={n} edges={m} into {env['uri']} (world={V}) flags={flags}")

    driver = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]),
                                  notifications_min_severity="OFF")
    try:
        with driver.session() as s:
            tax = {i["name"] for i in run_impact(s, "消費税率", V)["items"]}
    finally:
        driver.close()

    ok = True
    if not (GOLDEN <= tax):
        ok = False
        print("FAIL impact(消費税率) 不足:", sorted(GOLDEN - tax))
    if tax & FORBIDDEN:
        ok = False
        print("FAIL impact(消費税率) に番人が混入:", sorted(tax & FORBIDDEN))
    if ok:
        print("PASS: 実 Neo4j の world 影響が golden を再現")
        print("  impact(消費税率) =", sorted(tax))
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
