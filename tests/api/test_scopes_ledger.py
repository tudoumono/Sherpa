"""範囲の選択肢（`GET /scopes`）・範囲の検証・文書一覧・管理者の ES 検索が、取り込みの台帳から答え、資料フォルダを歩かないこと。
台帳が無いときだけ期限つきで歩き、期限を超えたら 503 で「時間内に取得できない」を返す。
台帳の書き込みは取り込みと同じ `worker._ledger_rows`、DB は実物（差し替えは資料フォルダの場所の解決と ES 検索の戻りだけ）。
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from sherpa import deps, es_index, scope_infer, store, worlds
from sherpa.api import app
from sherpa.ingest import worker

_FILES = {
    "gen1/design/spec_a.md": "# spec A\n",
    "gen1/src/PROG1.cbl": "       PROGRAM-ID. PROG1.\n",
    "gen1/src/lib/Util.java": "class Util {}\n",
    "gen2/design/spec_a.md": "# spec A v2\n",
    "loose.md": "# loose\n",
}


@pytest.fixture
def world(monkeypatch, tmp_path, auth_disabled):
    wid = "scopesapi_" + uuid.uuid4().hex[:8]
    wd, arch, der = tmp_path / "world", tmp_path / "arch", tmp_path / "derived"
    for rel, text in _FILES.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    arch.mkdir()
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: arch)
    yield wid
    store.replace_documents(wid, [])


def _forbid_walk(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("台帳があるのに資料フォルダを歩いた")
    monkeypatch.setattr(scope_infer, "safe_files", boom)
    monkeypatch.setattr(os, "scandir", boom)


def test_scopes_endpoint_from_ledger_returns_same_tree_without_walking(world, monkeypatch):
    c = TestClient(app, raise_server_exceptions=False)
    walked = c.get("/scopes", params={"world": world})            # 台帳が無い＝歩いて答える
    assert walked.status_code == 200, walked.text
    store.replace_documents(world, worker._ledger_rows(world))
    with monkeypatch.context() as mp:
        _forbid_walk(mp)
        from_ledger = c.get("/scopes", params={"world": world})
    assert from_ledger.status_code == 200, from_ledger.text
    assert from_ledger.json() == walked.json()
    assert {s["path"] for s in from_ledger.json()["scopes"]} >= {"gen1", "gen1/src/lib", "gen2/design"}


def test_scopes_endpoint_without_ledger_times_out_with_503(world, monkeypatch):
    monkeypatch.setattr(scope_infer, "USER_OP_WALK_SECONDS", -1.0)
    r = TestClient(app, raise_server_exceptions=False).get("/scopes", params={"world": world})
    assert r.status_code == 503
    assert "時間内に取得できません" in r.json()["detail"]


def test_scope_validation_uses_ledger_and_times_out_without_it(world, monkeypatch):
    store.replace_documents(world, worker._ledger_rows(world))
    with monkeypatch.context() as mp:
        _forbid_walk(mp)
        assert deps.validated_scope(world, ["gen1/src"]) == ["gen1/src"]
        with pytest.raises(HTTPException) as e:
            deps.validated_scope(world, ["gen1/nothing"])
        assert e.value.status_code == 422
    store.replace_documents(world, [])
    monkeypatch.setattr(scope_infer, "USER_OP_WALK_SECONDS", -1.0)
    with pytest.raises(HTTPException) as e:
        deps.validated_scope(world, ["gen1/src"])
    assert e.value.status_code == 503


def test_documents_endpoint_without_ledger_times_out_with_503(world, monkeypatch):
    monkeypatch.setattr(scope_infer, "USER_OP_WALK_SECONDS", -1.0)
    r = TestClient(app, raise_server_exceptions=False).get("/documents", params={"world": world})
    assert r.status_code == 503
    assert "時間内に取得できません" in r.json()["detail"]


def test_admin_es_search_checks_hits_without_walking(world, monkeypatch):
    store.replace_documents(world, worker._ledger_rows(world))
    monkeypatch.setattr(es_index, "search", lambda w, q, scope_paths=None, k=20, settings=None, vector=True: (
        [{"doc_id": "gen1/design/spec_a.md", "line": 1, "text": "hit", "score": 1.0, "ext": ".md"},
         {"doc_id": "gone.md", "line": 1, "text": "stale", "score": 1.0, "ext": ".md"}], None))
    with monkeypatch.context() as mp:
        _forbid_walk(mp)
        r = TestClient(app, raise_server_exceptions=False).get(
            "/admin/es/search", params={"world": world, "query": "hit"})
    assert r.status_code == 200, r.text
    assert [h["doc_id"] for h in r.json()["hits"]] == ["gen1/design/spec_a.md"]
