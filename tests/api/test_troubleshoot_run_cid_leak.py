"""直接 API 経路（`/troubleshoot/run`）の応答が内部専用の Neo4j `cid`（canonical_id）を含まない
ことの契約テスト——`lens_service.run_troubleshoot()` の公開結果は `cid` を持たない（`neighbor_cards`
＝agentic 経路だけが受け取る）。

`/troubleshoot/run`（`sherpa/routers/impact.py::troubleshoot_run`）は `run_troubleshoot` をそのまま
返す薄いエンドポイント。実 Neo4j は使わず `neo4j_session` を monkeypatch する（Neo4j 到達性は
`tests/api/test_impact_overload.py` 等の専用テストが担当・本ファイルの関心は cid 不在のみ）。
"""
from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(auth_disabled):
    from sherpa.api import app
    return TestClient(app, raise_server_exceptions=False)


class _FakeRecord:
    def __init__(self, d):
        self._d = d

    def data(self):
        return dict(self._d)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(_FakeRecord(r) for r in self._rows)

    def consume(self):
        pass


class _FakeSession:
    """`run_troubleshoot` が呼ぶクエリに1種類の行を返す最小スタブ（`tests/unit/test_lens_service.py`
    の `_FakeSession` と同じ最小主義）。"""

    def run(self, query, **params):
        return _FakeResult([{
            "cid": "module:v1:04_運用/order.cob#ORDER-MAIN", "name": "ORDER-MAIN", "label": "Module",
            "em": "static", "status": "active", "path_names": ["ROOT", "ORDER-MAIN"],
            "edges": [{"type": "USES", "doc": "order.md"}], "dist": 1,
        }])


def test_troubleshoot_run_endpoint_response_omits_internal_cid(client, monkeypatch):
    """`/troubleshoot/run` の応答 JSON は `candidates[].cid` を含まない——内部専用の Neo4j
    canonical_id は agentic ツール `graph_neighbors` だけが受け取る契約（公開 API には出さない）。"""
    from sherpa import lens_service
    from sherpa.routers import impact

    @contextlib.contextmanager
    def _fake_session():
        yield _FakeSession()

    monkeypatch.setattr(impact, "neo4j_session", _fake_session)
    monkeypatch.setattr(impact, "validated_scope", lambda world, sp: None)
    monkeypatch.setattr(lens_service, "grep_search", lambda *a, **k: [])

    r = client.post("/troubleshoot/run", json={"symptom": "ORDER-MAIN で ABEND", "world": "v1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["candidates"]                       # 候補が実際に返っていることを前提に確認する
    for c in body["candidates"]:
        assert "cid" not in c
    assert body["candidates"][0]["name"] == "ORDER-MAIN"
