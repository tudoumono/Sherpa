#!/usr/bin/env python3
"""実 Neo4j で v1 world を投入し、影響の golden を再現するか検証する（鏡モデル・要 `make up`）。

`world_graph.build_world` → `world_neo4j.load_world` → `run_world_impact` を通し、構造的な影響
レンズの golden（TAX-RATE まわり・骨格到達集合）と番人（顧客マスタ側への誤到達）を確認する。
テストのオラクル（正解）なのでコード名を含む。
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
ENTRY = "TAX-RATE"
GOLDEN = {"TAX-CPY", "TAXCALC", "BILLGEN", "SALESUP", "NIGHTLY"}
FORBIDDEN = {"CUSTMNT", "CUSTOMER-CPY"}


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
            names = {i["name"] for i in run_impact(s, ENTRY, V)["items"]}
    finally:
        driver.close()

    ok = True
    if not (GOLDEN <= names):
        ok = False
        print(f"FAIL impact({ENTRY}) 不足:", sorted(GOLDEN - names))
    if names & FORBIDDEN:
        ok = False
        print(f"FAIL impact({ENTRY}) に番人が混入:", sorted(names & FORBIDDEN))
    if ok:
        print("PASS: 実 Neo4j の world 影響が golden を再現")
        print(f"  impact({ENTRY}) =", sorted(names))
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
