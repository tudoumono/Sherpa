"""取り込みオーケストレーション 受け入れ（鏡モデル・即反映ライブ鏡）。

worker が world を1回スキャン→台帳(走査)＋world グラフを作り、`ingest_runs` に記録する。
グラフ構築は PG/Neo4j 不要（`build_world_graph`）。run/endpoint は要 Postgres（無ければ skip）・
反映は要 Neo4j（reflect=False なら不要）。
"""
from __future__ import annotations

import pytest

import _world_setup  # noqa: F401  (sets cwd/fixtures env)
from _world_registry import register_test_world
from _world_setup import TEST_WORLD_ID
from sherpa import store
from sherpa.ingest import worker

# 実 world と衝突しないテスト専用 world_id（2026-07-03 インシデント対応の v1→pytest-v1 移行）。
# 本ファイルは worker.run(reflect=True) で**共有 Neo4j に書く**ため、固定 'v1' のままだと
# 同名の実 world を破壊し得る＋セッション終了掃除（_world_registry）の対象外になる。
V = TEST_WORLD_ID
register_test_world(V)


def _pg_up():
    try:
        store.list_documents(V)
        return True
    except Exception:
        return False


def test_build_world_graph_is_clean():
    """world グラフ構築（骨格＋言及エッジ・S3 で意味層/REALIZES 撤去済み）が曖昧なく解決する（PG/Neo4j 不要）。"""
    nodes, edges, flags = worker.build_world_graph(V)
    assert nodes and edges and flags == []
    assert any(n["label"] == "Module" and n["name"] == "TAXCALC" for n in nodes)


def test_unknown_world_blocks():
    nodes, edges, flags = worker.build_world_graph("nope")
    assert nodes == [] and edges == []
    assert any(f.get("reason") == "world_unresolved" and f.get("action") == "blocked" for f in flags)


def test_run_writes_ledger_and_records_run():
    """worker.run（reflect=False）が台帳を書き、run を記録（staging のみ＝published_at なし）。"""
    if not _pg_up():
        pytest.skip("PG 未起動")
    res = worker.run(V, reflect=False)
    assert res["ledger"] > 0 and res["status"] == "extracting"
    assert store.list_documents(V)                       # 台帳が書かれた（doc_id＝rel_path）
    names = {r["name"] for r in store.list_documents(V)}
    assert "4期/03_開発/01_ソース/TAXCALC.cbl" in names
    runs = store.list_ingest_runs(V)
    assert runs and runs[0]["status"] == "extracting" and runs[0]["version"] == V   # 行キー version は DB 列名由来（不変）
    assert runs[0]["published_at"] is None


def test_list_document_worlds_includes_worlds_with_ledger_rows():
    """バッチ2・3番（2026-07-03）: `store.list_document_worlds()` は documents に行がある world を
    重複無く返す（reconcile の孤児判定が使う一覧取得・実 Postgres で確認）。"""
    if not _pg_up():
        pytest.skip("PG 未起動")
    worker.run(V, reflect=False)                          # V="v1" の台帳行を確定させる
    worlds_present = store.list_document_worlds()
    assert V in worlds_present
    assert len(worlds_present) == len(set(worlds_present))   # 重複無し


def test_run_reflects_to_neo4j_when_available():
    """reflect=True で world をクリーン rebuild（Neo4j 無ければ failed・台帳は確定）。"""
    if not _pg_up():
        pytest.skip("PG 未起動")
    res = worker.run(V)
    assert res["status"] in ("auto_published", "auto_published_with_flags", "failed")
    assert res["ledger"] > 0                              # 反映可否によらず台帳は確定


def test_rerun_endpoint_and_runs(auth_disabled):
    """ING-3: `/ingest/rerun` は即受付（run_id・joined）。完了状況は `/ingest/runs` の
    該当 run（`run_id` で照合）をポーリングして確認する（`GET /worlds/{wid}/status` は
    `worlds` テーブルへの登録〔root_path 付き〕が要るが、本ファイルの world は
    `worker.run()` 直呼びのみで作られ登録されていないため使えない）。"""
    import time

    try:
        from fastapi.testclient import TestClient
        from sherpa.api import app
        c = TestClient(app)
        r = c.post("/ingest/rerun", json={"world": V})
        if r.status_code != 202:
            pytest.skip("infra down")
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    body = r.json()
    assert body["ok"] is True and body["world_id"] == V
    assert body["run_id"] is not None

    deadline = time.monotonic() + 60.0
    run = None
    while time.monotonic() < deadline:
        runs = c.get("/ingest/runs", params={"world": V}).json()["runs"]
        run = next((x for x in runs if x["id"] == body["run_id"]), None)
        if run is not None and run["status"] != "extracting":
            break
        time.sleep(0.2)
    assert run is not None, f"取り込みが完了しませんでした: run_id={body['run_id']}"
    assert run["status"] in ("auto_published", "auto_published_with_flags", "failed")
    assert run.get("source_doc_ids")                      # 反映可否によらず台帳は確定（ledger > 0 相当）

    assert c.post("/ingest/rerun", json={"world": "../bad"}).status_code == 422
    runs = c.get("/ingest/runs", params={"world": V}).json()["runs"]
    assert runs and "original_path" not in runs[0]         # 物理パスを出さない
