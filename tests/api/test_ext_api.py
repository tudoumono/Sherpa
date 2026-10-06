"""外部連携 API（/ext/v1）のテスト。要 Postgres（DB 不可は skip）。

admin 操作はセッション Cookie 認証、convert/search/answer/capabilities/doc/openapi は X-API-Key 認証。
融合ロジックは tests/unit/test_fused_search.py、fd 所有権は tests/unit/test_fd_response.py、
CFB/ZIP の検証部品は tests/unit/test_ext_api_cfb.py。ここでは認証・world/scope・監査・配線を検証する。
"""
from __future__ import annotations

import asyncio
import errno
import io
import json
import os
import shutil
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import agentic_search, auth, documents, es_index, ext_api, store, worlds
from sherpa.api import app
from sherpa.parts.read import fused_search

client = TestClient(app, raise_server_exceptions=True)

_TAX_DOC = "4期/01_標準/消費税法.md"
_ANSWER_REAL_DOC = "4期/04_運用/障害記録.md"   # fixtures/corpus/v1 実在ファイル
_ADMIN_KEYS = "/ext/v1/admin/keys"
_SELF_KEYS = "/ext/v1/keys"


@pytest.fixture(autouse=True)
def _db():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")   # 不可なら可視の skip


@pytest.fixture(autouse=True)
def _audit_writer_running():
    """このファイルは lifespan 無しの TestClient を使う。先行テストが lifespan を閉じると監査 writer が
    明示停止のまま残り監査行が書かれないため、各テストの前に起動し直す。"""
    ext_api._audit_writer.start()
    yield


# ---- ヘルパ ----

def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _mk_account(prefix: str, role: str, sfx: str) -> tuple[str, str]:
    uid = f"{prefix}{sfx}"
    pw = f"pw-{uid}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(pw), role=role, status="active")
    register_test_uid(uid)
    return uid, pw


def _mk_admin(sfx: str) -> tuple[str, str]:
    return _mk_account("exta", "admin", sfx)


def _mk_user(sfx: str) -> tuple[str, str]:
    return _mk_account("extu", "user", sfx)


def _login(uid: str, pw: str) -> None:
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, f"login failed: {r.text}"


def _logout() -> None:
    client.post("/auth/logout")


@contextmanager
def _as_admin():
    """admin としてログインした状態で with 本体を実行し、終了時にログアウトする。"""
    uid, pw = _mk_admin(_sfx())
    _login(uid, pw)
    try:
        yield uid
    finally:
        _logout()


def _key(label: str, **body) -> dict:
    """admin 発行（world スコープ等は body で指定）。プレーンキーを含む応答 dict を返す。"""
    with _as_admin():
        r = client.post(_ADMIN_KEYS, json={"label": f"{label}-{_sfx()}", **body})
        assert r.status_code == 200, r.text
        return r.json()


@contextmanager
def _self_issue_enabled(**settings):
    """利用者の自己発行を許可し、終了時に未設定へ戻す。admin 資格と設定 PUT 応答を渡す。"""
    uid, pw = _mk_admin(_sfx())
    _login(uid, pw)
    put = client.put("/admin/settings", json={"user_api_keys_allowed": True, **settings})
    assert put.status_code == 200, put.text
    _logout()
    try:
        yield SimpleNamespace(uid=uid, pw=pw, put=put)
    finally:
        _logout()
        _login(uid, pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None,
                                            **{k: None for k in settings}})
        _logout()


@contextmanager
def _issuer(who: str, **settings):
    """who="admin": admin ログイン済みで発行ルートを返す。who="self": 自己発行を許可し、利用者
    ログイン済みで自己発行ルートを返す。"""
    if who == "admin":
        with _as_admin():
            yield _ADMIN_KEYS
        return
    with _self_issue_enabled(**settings):
        _login(*_mk_user(_sfx()))
        try:
            yield _SELF_KEYS
        finally:
            _logout()


def _h(key: str | None = None, rid: str | None = None) -> dict:
    h = {}
    if key:
        h["X-API-Key"] = key
    if rid:
        h["X-Request-Id"] = rid
    return h


_DOCX_XML = """<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:body>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>タイトル見出し</w:t></w:r></w:p>
  <w:p><w:r><w:t>本文テキストABC</w:t></w:r></w:p>
 </w:body>
</w:document>"""


def _zip_bytes(members) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in members:
            z.writestr(name, content)
    return buf.getvalue()


def _make_docx_bytes() -> bytes:
    return _zip_bytes([("word/document.xml", _DOCX_XML)])


def _convert(filename, content, key=None, rid=None):
    return client.post("/ext/v1/convert",
                       files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
                       headers=_h(key, rid))


def _search(payload, key=None, rid=None):
    return client.post("/ext/v1/search", json=payload, headers=_h(key, rid))


def _answer(payload, key=None):
    return client.post("/ext/v1/answer", json=payload, headers=_h(key))


def _capabilities(key=None, rid=None):
    return client.get("/ext/v1/capabilities", headers=_h(key, rid))


def _doc(world, path, key=None):
    return client.get("/ext/v1/doc", params={"world": world, "path": path}, headers=_h(key))


def _audit_rows(rid, cols="outcome, reason, detail"):
    with store._connect() as c:
        return c.execute(f"SELECT {cols} FROM audit_log WHERE request_id=%s ORDER BY id",
                         (rid,)).fetchall()


def _audit_one(rid, cols="outcome, reason, detail"):
    rows = _audit_rows(rid, cols)
    assert len(rows) == 1, f"監査行はちょうど1件のはず（実際 {len(rows)} 件）"
    return rows[0]


def _raise(exc):
    def _f(*a, **kw):
        raise exc
    return _f


# ===== APIキー基盤 =====

@pytest.mark.parametrize("call", [
    lambda: _convert("a.docx", _make_docx_bytes()),
    lambda: _search({"world": "v1", "query": "税"}),
    lambda: _capabilities(),
    lambda: _doc("v1", _TAX_DOC),
    lambda: client.get("/ext/v1/openapi.json"),
], ids=["convert", "search", "capabilities", "doc", "openapi"])
def test_ext_endpoints_require_api_key(call):
    assert call().status_code == 401


def test_admin_key_create_requires_admin():
    _login(*_mk_user(_sfx()))
    try:
        assert client.post(_ADMIN_KEYS, json={"label": "x"}).status_code == 403
    finally:
        _logout()


def test_admin_key_lifecycle():
    adm = _mk_admin(_sfx())
    _login(*adm)
    issued = client.post(_ADMIN_KEYS, json={"label": f"lifecycle-{_sfx()}"}).json()
    plain, key_id = issued["key"], issued["id"]
    assert plain.startswith("sk-ext-"), issued

    # 一覧は key_prefix のみ（プレーンキーは出ない）。
    r = client.get(_ADMIN_KEYS)
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["keys"] if x["id"] == key_id)
    assert row["key_prefix"] == issued["key_prefix"]
    assert all(plain not in v for v in row.values() if isinstance(v, str))
    _logout()

    assert _convert("a.docx", _make_docx_bytes(), plain).status_code == 200

    _login(*adm)
    r = client.delete(f"{_ADMIN_KEYS}/{key_id}")
    assert r.status_code == 200, r.text
    assert r.json()["revoked_at"]
    _logout()

    assert _convert("a.docx", _make_docx_bytes(), plain).status_code == 401   # 失効後

    _login(*adm)
    assert client.delete(f"{_ADMIN_KEYS}/{key_id}").status_code == 200   # 再 DELETE は冪等
    assert client.delete(f"{_ADMIN_KEYS}/999999999").status_code == 404
    _logout()


def test_key_hash_only_in_db():
    issued = _key("hashonly")
    row = store.api_key_by_hash(ext_api._hash_key(issued["key"]))
    assert row is not None
    assert row["key_hash"] != issued["key"]
    assert issued["key"] not in row["key_hash"]


# ===== POST /ext/v1/convert =====

def test_convert_docx_roundtrip():
    r = _convert("doc.docx", _make_docx_bytes(), _key("convert")["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unsupported"] is False
    assert body["method"] == "ooxml"
    assert "タイトル見出し" in body["md"]
    assert body["filename"] == "doc.docx"
    assert body["size_bytes"] == len(_make_docx_bytes())


@pytest.mark.parametrize("name, content, patch, status", [
    ("a.txt", b"hello", {}, 422),
    ("big.docx", b"x" * 2000, {"_CONVERT_MAX_BYTES": 1000}, 413),
    ("bomb.docx", _zip_bytes([("word/document.xml", _DOCX_XML + "A" * 1000)]),
     {"_ZIP_MAX_UNCOMPRESSED": 100}, 422),
], ids=["extension", "size_limit", "zip_bomb"])
def test_convert_rejects(monkeypatch, name, content, patch, status):
    for k, v in patch.items():
        monkeypatch.setattr(ext_api, k, v)
    r = _convert(name, content, _key("convrej")["key"])
    assert r.status_code == status, r.text


def test_convert_broken_file_is_unsupported_200_and_audited_as_failed_business_outcome():
    """壊れた zip は変換 None＝HTTP 200・`unsupported: true`。監査は outcome=success のまま
    business_outcome=failed を別列で記録する。"""
    rid = f"probe-convfail-{_sfx()}"
    r = _convert("broken.docx", b"not a real zip file", _key("broken")["key"], rid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unsupported"] is True
    assert body["md"] is None
    assert body["reason"] == "conversion_failed"

    row = _audit_one(rid)
    assert row["outcome"] == "success"
    assert row["detail"]["business_outcome"] == "failed"


def test_convert_tmpfile_cleaned(monkeypatch):
    created: list[Path] = []
    orig_to_markdown = ext_api.office_md.to_markdown

    def _spy(path):
        created.append(Path(path))
        return orig_to_markdown(path)

    monkeypatch.setattr(ext_api.office_md, "to_markdown", _spy)
    r = _convert("clean.docx", _make_docx_bytes(), _key("tmpclean")["key"])
    assert r.status_code == 200, r.text
    assert created, "to_markdown が呼ばれていない（スパイ未到達）"
    for p in created:
        assert not p.exists(), f"一時ファイルが残っている: {p}"


# ===== POST /ext/v1/search =====

def test_search_unknown_scope_422():
    r = _search({"world": "v1", "query": "税", "scope_paths": ["no-such-scope-xyz"]},
                _key("badscope")["key"])
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("patch, allowed, world, scope_in, scope_audited, status, outcome, reason", [
    # scope 検証の OSError は未処理例外にせず 503。要求された scope は検証前に正規化して監査に積む。
    ("scope_oserror", None, "v1", ["01_受付"], ["01_受付"], 503, "error", "unavailable"),
    # prefix は `_enforce_world_scope()`／`_resolve_world_or_error()`／registry 解決より前に積まれ、
    # 失敗リクエストの監査にも残る（未正規化入力は正規化後の値で残る）。
    ("none", ["v1"], "other-world-xyz", ["4期//サブ/"], ["4期//サブ"], 403, "deny", "world_not_allowed"),
    ("none", None, "no-such-world-xyz", ["4期//サブ/"], ["4期//サブ"], 404, "error", "not_found"),
    ("registry_down", None, "v1", ["4期//サブ/"], ["4期//サブ"], 503, "error", "unavailable"),
], ids=["scope_oserror_503", "scope_exclusion_403", "unknown_world_404", "registry_unreachable_503"])
def test_search_failure_audits_normalized_prefix(monkeypatch, patch, allowed, world, scope_in,
                                                 scope_audited, status, outcome, reason):
    if patch == "scope_oserror":
        monkeypatch.setattr(ext_api.scope_mod, "valid_scope_paths", _raise(
            OSError("simulated permission error during scope validation")))
    elif patch == "registry_down":
        monkeypatch.setattr(worlds, "resolve_external_world", _raise(
            worlds.ExternalResolverError("simulated registry outage")))
    extra = {} if allowed is None else {"allowed_worlds": allowed}
    key = _key("auditprefix", **extra)["key"]
    rid = f"probe-search-fail-{_sfx()}"

    r = _search({"world": world, "query": "x", "scope_paths": scope_in}, key, rid)
    assert r.status_code == status, r.text

    row = _audit_one(rid)
    assert row["detail"]["world"] == world
    assert row["detail"]["prefix"] == scope_audited
    assert row["detail"]["http_status"] == status
    assert row["outcome"] == outcome
    assert row["reason"] == reason


@pytest.mark.parametrize("target, attr, exc, call", [
    (worlds, "resolve_external_world", worlds.ExternalResolverError("simulated registry outage"),
     lambda k: _search({"world": "v1", "query": "x"}, k)),
    (worlds, "resolve_external_world", worlds.ExternalResolverError("simulated registry outage"),
     lambda k: _doc("v1", "a.md", k)),
    # fixtures/dev KB の FS 列挙の失敗と registry スナップショット取得の失敗は別経路・同じ 503。
    (worlds, "discover_fs_world_ids_strict", worlds.ExternalResolverError("simulated fs outage"),
     lambda k: _capabilities(k)),
    (store, "list_worlds_db", RuntimeError("simulated db outage"),
     lambda k: _capabilities(k)),
], ids=["search", "doc", "capabilities_fs", "capabilities_snapshot"])
def test_registry_unreachable_returns_503(monkeypatch, target, attr, exc, call):
    monkeypatch.setattr(target, attr, _raise(exc))
    assert call(_key("reg503")["key"]).status_code == 503


def test_search_registered_root_unreachable_returns_503(tmp_path):
    """登録済みだが参照先ディレクトリが無い（マウント外れ等）＝503（未登録の 404 とは区別）。"""
    wid = f"realbroken{_sfx()}"
    root = tmp_path / "root"
    root.mkdir()
    (root / "note.md").write_text("x", encoding="utf-8")
    store.upsert_world(wid, str(root))
    try:
        shutil.rmtree(root)
        r = _search({"world": wid, "query": "x"}, _key("rootunreach")["key"])
        assert r.status_code == 503, r.text
    finally:
        store.delete_world_row(wid)


def test_search_degraded_all_engines_down(monkeypatch):
    """ES/Neo4j 不可はエンジン単位の degraded（200）で返る（黙ってすり替えない）。"""
    from neo4j.exceptions import ServiceUnavailable

    monkeypatch.setattr(es_index, "available", lambda: False)

    class _RaiseCtx:
        def __enter__(self):
            raise ServiceUnavailable("down")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(fused_search, "_neo4j_session", lambda: _RaiseCtx())

    r = _search({"world": "v1", "query": "税", "engines": ["keyword", "vector", "graph"]},
                _key("degrade")["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hits"] == []
    assert body["engines_used"] == []
    assert {d["engine"]: d["reason"] for d in body["degraded"]} == {
        "keyword": "es_unavailable", "vector": "es_unavailable", "graph": "neo4j_unavailable"}


def test_search_filters_nonexistent_docs(monkeypatch):
    """削除直後の窓で ES 索引に古いまま残る doc_id は返さない（`documents.world_rel_set` の実在フィルタ）。"""
    def _hit(rel):
        return {"key": rel, "doc_id": rel, "path": rel, "line": 1, "snippet": "s",
                "engine_score": 1.0, "judgement": None, "paths": None}

    monkeypatch.setattr(fused_search, "_search_keyword",
                        lambda world, query, sp, k, settings, layer=None: (
                            [_hit("real.md"), _hit("deleted.md")], None))
    monkeypatch.setattr(documents, "world_rel_set",
                        lambda world=None, root=None, strict=False, **kw: {"real.md"})

    r = _search({"world": "v1", "query": "x", "engines": ["keyword"]}, _key("realfilter")["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert {h["doc_id"] for h in body["hits"]} == {"real.md"}
    assert body["engines_used"] == ["keyword"]   # フィルタで減っても degrade 扱いにしない


def test_search_audit_written():
    issued = _key("audit")
    rid = f"probe-search-audit-{_sfx()}"
    r = _search({"world": "v1", "query": "税計算", "engines": ["keyword"]}, issued["key"], rid)
    assert r.status_code == 200, r.text

    row = _audit_one(rid, "actor_user_id, action, resource_type, resource_id, detail")
    assert row["actor_user_id"] == f"ext:{issued['id']}"
    assert row["resource_type"] == "ext_search"
    assert row["resource_id"] == "v1"
    assert row["detail"]["query"] == "税計算"
    assert row["detail"]["engines"] == ["keyword"]


# ===== search: coverage（未完了・打ち切りの申告）・graph_origin・degraded.detail・include_presumed =====

def _stub_engines(monkeypatch, graph_result, keyword_docs=()):
    """keyword は固定ヒット、graph は `run_impact` 相当の結果を実の `_graph_hits` で変換する（ES/Neo4j は使わない）。"""
    monkeypatch.setattr(documents, "world_rel_set", lambda world=None, root=None, strict=False, **kw: _AllDocs())
    monkeypatch.setattr(fused_search, "_search_keyword",
                        lambda world, query, sp, k, settings, layer=None: ([
                            {"key": d, "doc_id": d, "path": d, "line": 1, "snippet": "s",
                             "engine_score": 1.0, "judgement": None, "paths": None} for d in keyword_docs], None))
    seen = []

    def _graph(world, query, sp, k, depth=10, **kw):
        seen.append(kw)
        return fused_search._graph_hits(graph_result, k), None

    monkeypatch.setattr(fused_search, "_search_graph", _graph)
    return seen


class _AllDocs:
    def __contains__(self, item):
        return True


_GRAPH_RESULT = {
    "items": [{"name": f"S{i}", "label": "Module", "category": "ソース", "path": f"s{i}.cbl",
               "trace": [], "evidence": []} for i in range(3)],
    "presumed": [],
    "coverage": {"complete": False, "limits": [{"kind": "depth", "stage": "impact"}], "omitted": None,
                 "depth": {"requested": 4, "truncated": True}},
}


def test_search_coverage_shape_and_graph_origin(monkeypatch):
    _stub_engines(monkeypatch, _GRAPH_RESULT, keyword_docs=["s0.cbl", "k.md"])
    r = _search({"world": "v1", "query": "x", "engines": ["keyword", "graph"], "k": 2}, _key("cov")["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["coverage"] == {
        "keyword": {"complete": True, "requested_k": 2, "returned": 2, "omitted": None, "limits": []},
        "graph": {"complete": False, "requested_k": 2, "returned": 2, "omitted": 1,
                  "limits": [{"kind": "depth"}, {"kind": "result_cap"}],
                  "depth": {"requested": 4, "truncated": True}, "structural_count": 3, "presumed_count": 0},
        "fused": {"requested_k": 2, "returned": 2, "omitted_by_cut": 1},
    }
    by = {h["doc_id"]: h for h in body["hits"]}
    assert by["s0.cbl"]["graph_origin"] == ["structure"] and set(by["s0.cbl"]["sources"]) == {"keyword", "graph"}
    assert "graph_origin" not in by["k.md"]
    assert all("detail" not in d for d in body["degraded"])


def test_search_overload_is_degraded_detail_without_coverage(monkeypatch):
    from sherpa.ingest.world_neo4j import GraphQueryOverloadError

    monkeypatch.setattr(documents, "world_rel_set", lambda world=None, root=None, strict=False, **kw: _AllDocs())

    @contextmanager
    def _session():
        yield object()

    monkeypatch.setattr(fused_search, "_neo4j_session", _session)
    monkeypatch.setattr(fused_search, "run_impact", _raise(GraphQueryOverloadError("timeout", world="v1")))
    r = _search({"world": "v1", "query": "x", "engines": ["graph"]}, _key("ovl")["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["degraded"] == [{"engine": "graph", "reason": "graph_query_failed", "detail": "timeout"}]
    assert "graph" not in body["coverage"] and body["engines_used"] == []


def test_search_include_presumed_default_unchanged_and_false_audited(monkeypatch):
    seen = _stub_engines(monkeypatch, {"items": [], "presumed": []})
    key = _key("presumed")["key"]
    rid = f"probe-presumed-default-{_sfx()}"
    assert _search({"world": "v1", "query": "x", "engines": ["graph"]}, key, rid).status_code == 200
    assert seen == [{}]  # 既定は今までの呼び方のまま
    detail = _audit_one(rid, "detail")["detail"]
    assert {"world", "query", "engines", "k", "depth", "layer", "prefix", "result_count", "degraded"} <= set(detail)
    assert "include_presumed" not in detail and "coverage_complete" not in detail  # 既定の要求の詳細は今と同じ

    rid = f"probe-presumed-false-{_sfx()}"
    assert _search({"world": "v1", "query": "x", "engines": ["graph"], "include_presumed": False},
                   key, rid).status_code == 200
    assert seen[-1] == {"include_presumed": False}
    assert _audit_one(rid, "detail")["detail"]["include_presumed"] is False


def test_search_audit_keeps_coverage_complete_only_when_incomplete(monkeypatch):
    _stub_engines(monkeypatch, _GRAPH_RESULT)
    rid = f"probe-cov-audit-{_sfx()}"
    assert _search({"world": "v1", "query": "x", "engines": ["graph"]}, _key("covaudit")["key"], rid).status_code == 200
    assert _audit_one(rid, "detail")["detail"]["coverage_complete"] == {"graph": False}


_SRC = {"doc_id": "s0.cbl", "file": "s0.cbl", "via": "call", "from_def": {"file": "s0.cbl", "key": None}}


def test_search_graph_edges_carry_via_rule_sources_and_paths_omitted(monkeypatch):
    """経路の辺に `via`・`rule`・`sources`・`sources_omitted`、5 本で切った経路に `paths_omitted`（根拠のない辺は今の形のまま）。"""
    edge = {"type": "INVOKES", "doc": "s0.cbl", "line": 3, "via": "call", "rule": "single_import",
            "sources": [{**_SRC, "line": 3, "rule": "single_import"}, {**_SRC, "line": 8, "rule": "single_import"}],
            "sources_overflow_count": 5}
    plain = {"type": "COPIES", "doc": "s0.cbl", "line": 1}
    result = {"items": [{"name": "S", "label": "Module", "category": "ソース", "path": "s0.cbl",
                         "trace": ["A", "B", "C"], "evidence": [edge, plain]} for _ in range(7)]
              + [{"name": "T", "label": "Module", "category": "ソース", "path": "t.cbl", "trace": [], "evidence": []}],
              "presumed": []}
    _stub_engines(monkeypatch, result)
    r = _search({"world": "v1", "query": "x", "engines": ["graph"], "k": 10}, _key("edges")["key"])
    assert r.status_code == 200, r.text
    by = {h["doc_id"]: h for h in r.json()["hits"]}
    assert len(by["s0.cbl"]["paths"]) == 5 and by["s0.cbl"]["paths_omitted"] == 2
    e0, e1 = by["s0.cbl"]["paths"][0]["edges"]
    assert e0 == {"type": "INVOKES", "doc": "s0.cbl", "line": 3, "via": "call", "rule": "single_import",
                  "sources": [{**_SRC, "line": 3, "rule": "single_import"}, {**_SRC, "line": 8, "rule": "single_import"}],
                  "sources_omitted": 5}
    assert e1 == plain
    assert "paths_omitted" not in by["t.cbl"]


def test_search_evidence_limit_range_passthrough_and_audit(monkeypatch):
    seen = _stub_engines(monkeypatch, {"items": [], "presumed": []})
    key = _key("evlimit")["key"]
    for bad in (-1, 11):
        assert _search({"world": "v1", "query": "x", "engines": ["graph"], "evidence_limit": bad}, key).status_code == 422
    rid = f"probe-evlimit-default-{_sfx()}"
    assert _search({"world": "v1", "query": "x", "engines": ["graph"]}, key, rid).status_code == 200
    assert seen == [{}] and "evidence_limit" not in _audit_one(rid, "detail")["detail"]  # 既定の要求は今と同じ
    rid = f"probe-evlimit-7-{_sfx()}"
    assert _search({"world": "v1", "query": "x", "engines": ["graph"], "evidence_limit": 7}, key, rid).status_code == 200
    assert seen[-1] == {"evidence_limit": 7} and _audit_one(rid, "detail")["detail"]["evidence_limit"] == 7
    assert _search({"world": "v1", "query": "x", "engines": ["graph"], "evidence_limit": 0}, key).status_code == 200


def test_ext_openapi_subset():
    r = client.get("/ext/v1/openapi.json", headers=_h(_key("openapi")["key"]))
    assert r.status_code == 200, r.text
    doc = r.json()

    assert set(doc["paths"].keys()) == {
        "/ext/v1/convert", "/ext/v1/search", "/ext/v1/capabilities", "/ext/v1/doc",
        "/ext/v1/answer", "/ext/v1/codex/jobs", "/ext/v1/codex/jobs/{job_id}",
        "/ext/v1/codex/jobs/{job_id}/result", "/ext/v1/codex/jobs/{job_id}/cancel"}
    assert not any(p.startswith("/ext/v1/admin") for p in doc["paths"])

    def _collect_refs(obj, out: set) -> None:
        if isinstance(obj, dict):
            ref = obj.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                out.add(ref.rsplit("/", 1)[1])
            for v in obj.values():
                _collect_refs(v, out)
        elif isinstance(obj, list):
            for v in obj:
                _collect_refs(v, out)

    refs: set = set()
    _collect_refs(doc["paths"], refs)
    schemas = doc["components"]["schemas"]
    assert refs, "search/convert のパスから schema 参照が1つも見つからない"
    assert refs <= set(schemas.keys()), f"dangling $ref: {refs - set(schemas.keys())}"

    assert doc["security"] == [{"ApiKeyAuth": []}]
    assert doc["components"]["securitySchemes"]["ApiKeyAuth"] == {
        "type": "apiKey", "in": "header", "name": "X-API-Key"}


# ===== search: depth・layer =====

def test_search_depth_and_layer_passthrough(monkeypatch):
    """`depth`/`layer` が `fused_search.search` まで素通しされ、省略時は既定（depth=10・layer=both）。"""
    captured = {}

    def _fake_search(world, query, engines=None, k=10, scope_paths=None, weights=None,
                     settings=None, depth=8, root=None, strict=False, layer=None):
        captured.update(depth=depth, layer=layer)
        return {"hits": [], "engines_used": [], "degraded": []}

    monkeypatch.setattr(fused_search, "search", _fake_search)
    key = _key("passthru")["key"]

    assert _search({"world": "v1", "query": "x", "depth": 3, "layer": "code"}, key).status_code == 200
    assert captured == {"depth": 3, "layer": "code"}

    assert _search({"world": "v1", "query": "x"}, key).status_code == 200
    assert captured["depth"] == fused_search.IMPACT_MAX_DEPTH == 10
    assert captured["layer"] == "both"


def test_search_depth_range_and_invalid_layer_422(monkeypatch):
    """`depth` の上限は外部 API 契約 12 を後退させない（0/13 は範囲外・9〜12 は受理）。不正 layer は 422。"""
    monkeypatch.setattr(fused_search, "search",
                        lambda *a, **kw: {"hits": [], "engines_used": [], "degraded": []})
    key = _key("depthrange")["key"]

    for extra in ({"depth": 0}, {"depth": 13}, {"layer": "bogus"}):
        r = _search({"world": "v1", "query": "x", **extra}, key)
        assert r.status_code == 422, (extra, r.text)
    for ok in (9, 10, 11, 12):
        r = _search({"world": "v1", "query": "x", "depth": ok}, key)
        assert r.status_code == 200, (ok, r.text)


def test_search_depth_reaches_run_impact(monkeypatch):
    """`fused_search.search` は差し替えず、`_search_graph`→`run_impact` の実配線で depth の最終到達値を見る。"""
    captured = {}

    def _fake_run_impact(session, term, world, aliasmap=None, scope_prefixes=None, depth=8, **kw):
        captured["depth"] = depth
        return {"items": [], "presumed": []}

    @contextmanager
    def _fake_session():
        yield object()

    monkeypatch.setattr(fused_search, "_neo4j_session", _fake_session)
    monkeypatch.setattr(fused_search, "run_impact", _fake_run_impact)

    r = _search({"world": "v1", "query": "x", "engines": ["graph"], "depth": 5}, _key("depthreach")["key"])
    assert r.status_code == 200, r.text
    assert captured["depth"] == 5


# ===== GET /ext/v1/capabilities =====

def test_capabilities_lists_worlds_and_capabilities():
    """`v1`（fixtures 直下・DB 未登録）は載るが、成功同期を通っていないため document_count/last_updated は null。"""
    r = _capabilities(_key("caps")["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"worlds"}
    w = next(w for w in body["worlds"] if w["world"] == "v1")
    assert set(w) == {"world", "document_count", "last_updated", "capabilities"}
    assert set(w["capabilities"]) == {"search", "answer", "codex_jobs", "embed", "convert"}
    for cap in w["capabilities"].values():
        assert isinstance(cap["configured"], bool) and isinstance(cap["available"], bool)
        # reason は configured/available のどちらかが偽のときだけ付く
        assert ("reason" in cap) == (not (cap["configured"] and cap["available"]))


def test_capabilities_distinguishes_configured_from_runnable(monkeypatch):
    """Codex は構成済みでも CLI が無ければ「設定済み・今は実行不可」（閉じた語彙の理由付き）。"""
    from sherpa import codex_jobs_worker
    monkeypatch.setattr(codex_jobs_worker, "capability", lambda: (True, False, "codex_cli_missing"))
    r = _capabilities(_key("capsrun")["key"])
    assert r.status_code == 200, r.text
    w = next(w for w in r.json()["worlds"] if w["world"] == "v1")
    assert w["capabilities"]["codex_jobs"] == {
        "configured": True, "available": False, "reason": "codex_cli_missing"}
    assert w["capabilities"]["convert"] == {"configured": True, "available": True}


@pytest.mark.parametrize("wid, sig, count, confirmed", [
    ("confirmed-world", "abc123", 42, True),
    ("pending-world", "", 7, False),   # 取り込み開始時の pre-invalidate（last_sig=""）＝未確定
], ids=["confirmed", "unconfirmed"])
def test_capabilities_document_count_only_when_sync_confirmed(monkeypatch, tmp_path, wid, sig, count,
                                                              confirmed):
    root = Path("fixtures/corpus/v1").resolve() if confirmed else tmp_path
    fake_row = {"world_id": wid, "root_path": str(root), "last_synced_at": datetime.now(timezone.utc),
                "last_sig": sig, "last_doc_count": count}
    monkeypatch.setattr(store, "list_worlds_db", lambda: [fake_row])

    r = _capabilities(_key("capsconfirm")["key"])
    assert r.status_code == 200, r.text
    world = next(w for w in r.json()["worlds"] if w["world"] == wid)
    if confirmed:
        assert world["document_count"] == count
        assert world["last_updated"] is not None
    else:
        assert world["document_count"] is None
        assert world["last_updated"] is None


def test_capabilities_does_not_fabricate_v1_when_nothing_exists(monkeypatch):
    """world が 1 件も無いとき、UI 向けの ["v1"] フォールバック（`list_worlds`）は使わず空を返す。"""
    monkeypatch.setattr(store, "list_worlds_db", lambda: [])
    monkeypatch.setattr(worlds, "discover_fs_world_ids_strict", lambda: [])

    def _must_not_be_called():
        raise AssertionError("list_worlds()（UI 向け [\"v1\"] フォールバック付き）を使ってはいけない")

    monkeypatch.setattr(worlds, "list_worlds", _must_not_be_called)
    monkeypatch.setattr(worlds, "discover_world_ids", _must_not_be_called)

    r = _capabilities(_key("nofakev1")["key"])
    assert r.status_code == 200, r.text
    assert r.json()["worlds"] == []


# ===== GET /ext/v1/doc =====

def test_doc_download_roundtrip():
    r = _doc("v1", _TAX_DOC, _key("doc")["key"])
    assert r.status_code == 200, r.text
    original = Path("fixtures/corpus/v1", _TAX_DOC).read_bytes()
    assert r.content
    assert r.content == original
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-store"
    # 非 ASCII ファイル名は RFC 5987 形式で返る。
    assert r.headers["content-disposition"] == f"attachment; filename*=utf-8''{quote('消費税法.md')}"
    assert r.headers["content-length"] == str(len(original))


@pytest.mark.parametrize("world, path", [
    ("v1", "no/such/file.md"),
    ("v1", "semantic/l_extract.json"),          # doctype 対応種別外
    ("v1", "../../../../etc/passwd.md"),        # traversal
    ("no-such-world-xyz", "a.md"),
], ids=["unknown_path", "unsupported_doctype", "traversal", "unknown_world"])
def test_doc_not_found_404(world, path):
    assert _doc(world, path, _key("docnf")["key"]).status_code == 404


def test_doc_size_limit(monkeypatch):
    monkeypatch.setattr(ext_api, "_DOC_MAX_BYTES", 10)
    assert _doc("v1", _TAX_DOC, _key("docsize")["key"]).status_code == 413


# ===== GET /ext/v1/doc: symlink 差し替え耐性・マジック検証（低レベル安全性） =====
#
# `worlds.resolve_external_world` を任意の一時ディレクトリへ差し替え、`safe_open` の実 O_NOFOLLOW walk を
# 実ファイルシステム上で検証する。

def _mock_external_world(monkeypatch, root, *, world_dir=False):
    """外部 API 専用の strict resolver を一時ディレクトリへ向ける。`world_dir=True` は内容判定
    （`status_document_doctype`→`worlds.world_dir`）も同じ root へ向ける（未登録拡張子の内容判定用）。"""
    monkeypatch.setattr(worlds, "resolve_external_world",
                        lambda w, **kw: worlds.ExternalWorldResolution("ok", root))
    if world_dir:
        monkeypatch.setattr(worlds, "world_dir", lambda w: root)


def _file(name, data):
    def setup(root, outside):
        (root / name).write_bytes(data)
        return data
    return setup


def _zip_file(name, members):
    def setup(root, outside):
        (root / name).write_bytes(_zip_bytes(members))
    return setup


def _symlinked_dir(root, outside):
    (outside / "secret.md").write_text("外部の内容", encoding="utf-8")
    (root / "evil_link").symlink_to(outside, target_is_directory=True)


def _symlinked_file(root, outside):
    target = outside / "secret.md"
    target.write_text("外部の内容", encoding="utf-8")
    (root / "link.md").symlink_to(target)


def _cfb_header_bytes(*, major: int = 3, sector_shift: int = 9) -> bytes:
    import struct
    header = bytearray(512)
    header[0:8] = ext_api._OLE2_MAGIC
    struct.pack_into("<HH", header, 24, 0, major)
    struct.pack_into("<H", header, 28, 0xFFFE)
    struct.pack_into("<H", header, 30, sector_shift)
    return bytes(header)


_PDF = b"%PDF-1.4\n%dummy pdf content\n"
_UTF8_TEXT = "こんにちは"

# (setup, 要求パス, 期待ステータス, 期待 content-type（None は見ない）, world_dir も差し替え, ext_api 定数の上書き)
_DOC_CASES = {
    # symlink: 中間ディレクトリでも対象ファイル自体でも、world 外へ出る経路は 404
    "symlink_in_path": (_symlinked_dir, "evil_link/secret.md", 404, None, False, {}),
    "symlink_file_itself": (_symlinked_file, "link.md", 404, None, False, {}),
    # マジック検証（拡張子偽装は 415・一致は配信）
    "magic_mismatch_pdf": (_file("fake.pdf", b"this is not a pdf file at all"), "fake.pdf", 415, None,
                           False, {}),
    "real_pdf": (_file("real.pdf", _PDF), "real.pdf", 200, "application/pdf", False, {}),
    "cross_format_image": (_file("fake.jpg", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32), "fake.jpg", 415,
                           None, False, {}),
    "bigtiff": (_file("big.tif", b"II+\x00" + b"\x08\x00\x00\x00" + b"\x00" * 32), "big.tif", 200,
                None, False, {}),
    "ooxml_empty_zip": (_zip_file("empty.docx", []), "empty.docx", 415, None, False, {}),
    "ooxml_cross_format": (_zip_file("mislabeled.docx", [("[Content_Types].xml", "<Types/>"),
                                                         ("xl/workbook.xml", "<workbook/>")]),
                           "mislabeled.docx", 415, None, False, {}),
    "ooxml_valid": (_zip_file("real.docx", [("[Content_Types].xml", "<Types/>"),
                                            ("word/document.xml", "<document/>")]),
                    "real.docx", 200, None, False, {}),
    "ooxml_too_many_members": (_zip_file("many.docx", [("[Content_Types].xml", "<Types/>"),
                                                       ("word/document.xml", "<document/>")]
                                         + [(f"extra/{i}.txt", "x") for i in range(10)]),
                               "many.docx", 415, None, False, {"_ZIP_MAX_MEMBERS": 3}),
    # legacy Office: OLE2 でない旧形式は拒否せず、壊れた CFB ヘッダは 415
    "pre_ole2_xls": (_file("legacy.xls", b"\x09\x00\x04\x00not really biff but not ole2 either"),
                     "legacy.xls", 200, None, False, {}),
    "valid_cfb_doc": (_file("real.doc", _cfb_header_bytes() + b"\x00" * 512), "real.doc", 200,
                      "application/octet-stream", False, {}),
    "malformed_cfb_xls": (_file("fake.xls", _cfb_header_bytes(major=3, sector_shift=12) + b"\x00" * 512),
                          "fake.xls", 415, None, False, {}),
    # テキストの charset: UTF-8 でなければ宣言を外し、検証上限超過も「未検証」として宣言しない
    "charset_invalid_utf8": (_file("sjis.txt", _UTF8_TEXT.encode("shift_jis")), "sjis.txt", 200,
                             "text/plain", False, {}),
    "charset_valid_utf8": (_file("utf8.txt", _UTF8_TEXT.encode("utf-8")), "utf8.txt", 200,
                           "text/plain; charset=utf-8", False, {}),
    "charset_over_validation_cap": (_file("big.txt", ("あ" * 200).encode("utf-8")), "big.txt", 200,
                                    "text/plain", False, {"_UTF8_VALIDATE_CAP": 100}),
    # 対象外: 重要度設定ファイル・内容判定で対象外のバイナリ・秘匿名は 404、読めるテキストは未登録拡張子でも配信
    "importance_control_file": (_file("_重要度.txt", "*.md: 高\n".encode("utf-8")), "_重要度.txt", 404,
                                None, False, {}),
    "unregistered_ext_text": (_file("app.zzz", b"readable plain text content\n"), "app.zzz", 200,
                              None, True, {}),
    "unregistered_ext_binary": (_file("blob.bin", b"\x00\x01\x02binary\xff\xfe" * 10), "blob.bin", 404,
                                None, True, {}),
    "sensitive_name": (_file(".env", b"API_KEY=secret\n"), ".env", 404, None, True, {}),
}


@pytest.mark.parametrize("case", list(_DOC_CASES))
def test_doc_serving_safety(tmp_path, monkeypatch, case):
    setup, rel, status, ctype, world_dir, patch = _DOC_CASES[case]
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    for k, v in patch.items():
        monkeypatch.setattr(ext_api, k, v)
    content = setup(root, outside)
    _mock_external_world(monkeypatch, root, world_dir=world_dir)

    r = _doc("docsafety", rel, _key("docfd")["key"])
    assert r.status_code == status, r.text
    if status == 200 and content is not None:
        assert r.content == content
    if ctype is not None:
        assert r.headers["content-type"] == ctype


def test_doc_rejects_dot_segment_path_tricks_matching_content_check(tmp_path, monkeypatch):
    """`./`・`.//`・`a/./b`・末尾 `/.`（`%2e` 含む）は、配信側（`_doc_path_segments`）と内容判定側
    （`status_document_doctype`→`resolve_path`）のどちらも同じ正規化（`valid_rel_parts`）で拒否する
    ——食い違うと内容判定は「読み取り不可」で通り、配信側だけが実ファイルを返す（秘密鍵の漏洩経路）。
    実ファイルで再現する（モックでは穴が再現できない）。秘密鍵の中身は出さずステータスだけ見る。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "blob.zzz").write_text(
        "-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 64 + "\n-----END RSA PRIVATE KEY-----\n",
        encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "note.zzz").write_text("readable plain text\n", encoding="utf-8")   # トリック無しなら読める
    _mock_external_world(monkeypatch, root, world_dir=True)
    key = _key("docdot")["key"]

    for tricky in ("./blob.zzz", ".//blob.zzz", "sub/./note.zzz", "note.zzz/.", "sub/note.zzz/."):
        r = client.get("/ext/v1/doc", params={"world": "docsafety", "path": tricky}, headers=_h(key))
        assert r.status_code == 404, tricky
    # URL エンコード表現が HTTP 層でデコードされた後も拒否される。
    for qs in ("path=%2eblob.zzz", "path=sub%2f%2enote.zzz"):
        r = client.get(f"/ext/v1/doc?world=docsafety&{qs}", headers=_h(key))
        assert r.status_code == 404, r.text

    r = _doc("docsafety", "sub/note.zzz", key)   # トリック無しは取得できる（回帰）
    assert r.status_code == 200, r.text
    assert r.content == b"readable plain text\n"


def _build_deep_tree_with_leaves(root, num_segments: int, seg_len: int, leaves: dict) -> dict:
    """PATH_MAX を超える深いディレクトリ木を dir_fd 相対の syscall だけで構築し（累積パス文字列は
    作らない＝配信側 `safe_open.open_file_nofollow_walk` と同じ経路）、`leaves`（{名前: バイト列}）を
    最深ディレクトリへ書く。戻り値は {名前: world root からの相対 rel}。"""
    segs = ["あ" * seg_len] * num_segments
    dir_fd = os.open(str(root), os.O_DIRECTORY)
    try:
        for seg in segs:
            try:
                os.mkdir(seg, dir_fd=dir_fd)
            except FileExistsError:
                pass
            new_fd = os.open(seg, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = new_fd
        for name, content in leaves.items():
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600, dir_fd=dir_fd)
            try:
                os.write(fd, content)
            finally:
                os.close(fd)
    finally:
        os.close(dir_fd)
    prefix = "/".join(segs)
    return {name: f"{prefix}/{name}" for name in leaves}


def test_doc_rejects_path_too_long_for_content_check_even_though_serving_would_succeed(
        tmp_path, monkeypatch):
    """内容判定は累積パスを `os.lstat` するためパスが長すぎると判定不能（ENAMETOOLONG）になるが、
    配信側は dir_fd 相対で同じファイルを開ける。判定不能を「対象外ではない」に丸めると判定できなかった
    ファイルが配信される（fail-open）。PEM 秘密鍵ヘッダ入りと読めるテキストの両方（同じ長さ）で、
    判定できない以上は内容に関わらず拒否することを固定する（`verify_doc_exists` にも同じ fail-closed）。"""
    root = tmp_path / "root"
    root.mkdir()
    leaves = _build_deep_tree_with_leaves(root, 18, 80, {
        "secret.zzz": b"-----BEGIN RSA PRIVATE KEY-----\n" + b"A" * 64
                      + b"\n-----END RSA PRIVATE KEY-----\n",
        "plain.zzz": b"readable plain text\n",
    })
    with pytest.raises(OSError) as ei:
        root.joinpath(*(["あ" * 80] * 18), "secret.zzz").stat()
    assert ei.value.errno == errno.ENAMETOOLONG, f"想定外の errno（この環境の PATH_MAX 設定を確認）: {ei.value}"

    _mock_external_world(monkeypatch, root, world_dir=True)
    key = _key("docdeep")["key"]
    for rel in leaves.values():
        r = client.get("/ext/v1/doc", params={"world": "docsafety", "path": rel}, headers=_h(key))
        assert r.status_code == 404, rel

    (root / "normal.zzz").write_bytes(b"ok\n")   # 通常の長さの正常ファイルは取得できる（回帰）
    r = client.get("/ext/v1/doc", params={"world": "docsafety", "path": "normal.zzz"}, headers=_h(key))
    assert r.status_code == 200, r.text
    assert r.content == b"ok\n"

    for rel in leaves.values():
        assert agentic_search.verify_doc_exists(rel, "docsafety") is False, rel
    assert agentic_search.verify_doc_exists("normal.zzz", "docsafety") is True


# ===== API キーの world スコープ =====

def test_scoped_key_enforces_world_scope_across_endpoints():
    issued = _key("scoped", allowed_worlds=["v1"])
    key = issued["key"]
    with _as_admin():   # 一覧にも allowed_worlds が出る
        row = next(x for x in client.get(_ADMIN_KEYS).json()["keys"] if x["id"] == issued["id"])
        assert row["allowed_worlds"] == ["v1"]

    assert _search({"world": "v1", "query": "税", "engines": ["keyword"]}, key).status_code == 200
    assert _doc("v1", _TAX_DOC, key).status_code == 200
    for r in (_search({"world": "other-world-xyz", "query": "x"}, key),
              _doc("other-world-xyz", "a.md", key),
              _answer({"world": "other-world-xyz", "query": "x"}, key)):
        assert r.status_code == 403, r.text
    r = _capabilities(key)
    assert r.status_code == 200, r.text
    assert {w["world"] for w in r.json()["worlds"]} == {"v1"}


def test_unscoped_key_allows_any_world():
    """既存キー（allowed_worlds=null）は従来どおり全 world にアクセスできる（後方互換）。"""
    issued = _key("unscoped")
    assert issued["allowed_worlds"] is None
    assert _search({"world": "v1", "query": "税", "engines": ["keyword"]}, issued["key"]).status_code == 200
    r = _search({"world": "some-other-world-abc", "query": "x"}, issued["key"])
    assert r.status_code == 404, r.text   # スコープではなく世界不在の 404


def test_key_create_empty_allowed_worlds_denies_all():
    issued = _key("denyall", allowed_worlds=[])
    assert issued["allowed_worlds"] == []
    assert _search({"world": "v1", "query": "x"}, issued["key"]).status_code == 403
    assert _doc("v1", _TAX_DOC, issued["key"]).status_code == 403
    r = _capabilities(issued["key"])
    assert r.status_code == 200, r.text
    assert r.json()["worlds"] == []


@pytest.mark.parametrize("worlds_in", [["not a valid id!"], ["nonexistent-world-zzz"]],
                         ids=["invalid_identifier", "unknown_world"])
def test_key_create_rejects_invalid_or_unknown_world(worlds_in):
    with _as_admin():
        r = client.post(_ADMIN_KEYS, json={"label": f"badworld-{_sfx()}", "allowed_worlds": worlds_in})
        assert r.status_code == 422, r.text


def test_key_create_two_world_scope(tmp_path):
    """2 つの実在 world をスコープに持てる（本物の registry 行・resolver はモックしない）。
    スコープに未知の world を含めると発行時点で 422。"""
    second_world = f"v2real{_sfx()}"
    root2 = tmp_path / "root2"
    root2.mkdir()
    (root2 / "note.md").write_text("第二世界の資料です", encoding="utf-8")
    store.upsert_world(second_world, str(root2))
    try:
        issued = _key("twoworld", allowed_worlds=["v1", second_world])
        assert issued["allowed_worlds"] == ["v1", second_world]
        with _as_admin():
            r = client.post(_ADMIN_KEYS, json={"label": f"badscope-{_sfx()}",
                                               "allowed_worlds": [second_world, "nonexistent-world-zzz"]})
            assert r.status_code == 422, r.text

        key = issued["key"]
        assert _search({"world": "v1", "query": "税", "engines": ["keyword"]}, key).status_code == 200
        assert _search({"world": second_world, "query": "資料", "engines": ["keyword"]},
                       key).status_code == 200
        r = _doc(second_world, "note.md", key)
        assert r.status_code == 200, r.text
        assert "第二世界" in r.text
    finally:
        # 本物の registry 行は共有テスト DB に残ると `worlds.register()`（全体で 1 本だけ）を使う他テストを壊す。
        store.delete_world_row(second_world)


# ===== expires_at・daily_quota・client_op_id・利用者自己発行 =====

def test_key_create_with_expires_at_and_daily_quota_round_trip():
    with _as_admin():
        r = client.post(_ADMIN_KEYS, json={"label": f"exq-{_sfx()}",
                                           "expires_at": "2099-01-01T00:00:00+00:00", "daily_quota": 5})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["expires_at"] is not None
        assert body["daily_quota"] == 5
        row = next(x for x in client.get(_ADMIN_KEYS).json()["keys"] if x["id"] == body["id"])
        assert row["expires_at"] is not None
        assert row["daily_quota"] == 5
        assert row["call_count"] == 0


@pytest.mark.parametrize("who", ["admin", "self"])
def test_key_create_daily_quota_is_strict_int_with_max(who):
    """`daily_quota` は StrictInt: bool・数字文字列・1,000,000 超は 422（DB の CHECK と同じ上限）で、
    上限ちょうどは許可される。自己発行は管理者の許可上限を 1,000,000 へ上げた上で確認する。"""
    bad = ([0] if who == "admin" else []) + [True, "10", 1_000_001]
    with _issuer(who, user_api_keys_daily_quota_default=1_000_000) as route:
        for v in bad:
            r = client.post(route, json={"label": f"badq-{_sfx()}", "daily_quota": v})
            assert r.status_code == 422, (v, r.text)
        r = client.post(route, json={"label": f"maxq-{_sfx()}", "daily_quota": 1_000_000})
        assert r.status_code == 200, r.text
        assert r.json()["daily_quota"] == 1_000_000


@pytest.mark.parametrize("who", ["admin", "self"])
def test_key_create_rejects_past_expiry(who):
    with _issuer(who) as route:
        r = client.post(route, json={"label": f"pastexp-{_sfx()}", "expires_at": "2000-01-01T00:00:00+00:00"})
        assert r.status_code == 422, r.text


@pytest.mark.parametrize("who", ["admin", "self"])
def test_key_create_client_op_id_round_trips_to_list_and_create_response(who):
    with _issuer(who) as route:
        op_id = str(uuid.uuid4())
        r = client.post(route, json={"label": f"opid-{_sfx()}", "client_op_id": op_id})
        assert r.status_code == 200, r.text
        assert r.json()["client_op_id"] == op_id
        row = next(x for x in client.get(route).json()["keys"] if x["id"] == r.json()["id"])
        assert row["client_op_id"] == op_id


@pytest.mark.parametrize("who, bad_values", [
    ("admin", ["not-a-uuid", "op-abc123", "12345678-1234-1234-1234", ""]),
    ("self", ["xyz"]),
])
def test_key_create_client_op_id_rejects_non_uuid_format(who, bad_values):
    with _issuer(who) as route:
        for bad in bad_values:
            r = client.post(route, json={"label": f"badcop-{_sfx()}", "client_op_id": bad})
            assert r.status_code == 422, (bad, r.text)


@pytest.mark.parametrize("who", ["admin", "self"])
def test_key_create_client_op_id_conflict_returns_409(who):
    with _issuer(who) as route:
        op_id = str(uuid.uuid4())
        assert client.post(route, json={"label": f"dup1-{_sfx()}", "client_op_id": op_id}).status_code == 200
        r = client.post(route, json={"label": f"dup2-{_sfx()}", "client_op_id": op_id})
        assert r.status_code == 409, r.text


def test_key_create_client_op_id_normalizes_case_and_conflicts_even_after_revoke():
    """大文字で送っても応答は正準小文字形。別の大小文字表記の同じ UUID は 409（大小文字迂回を防ぐ）で、
    失効済みキーの client_op_id でも一意制約は有効。"""
    with _as_admin():
        op_lower = str(uuid.uuid4())
        r1 = client.post(_ADMIN_KEYS, json={"label": f"upnorm-{_sfx()}", "client_op_id": op_lower.upper()})
        assert r1.status_code == 200, r1.text
        assert r1.json()["client_op_id"] == op_lower

        r2 = client.post(_ADMIN_KEYS, json={"label": f"upnorm2-{_sfx()}", "client_op_id": op_lower})
        assert r2.status_code == 409, r2.text

        assert client.delete(f"{_ADMIN_KEYS}/{r1.json()['id']}").status_code == 200
        r3 = client.post(_ADMIN_KEYS, json={"label": f"revdup-{_sfx()}", "client_op_id": op_lower})
        assert r3.status_code == 409, r3.text


def test_key_recover_matches_directly_seeded_uppercase_legacy_row_via_lowercase_query():
    """正規化を経由せず大文字のまま保存された旧行でも、回復 API へ小文字で照会すれば一致する。"""
    with _as_admin() as adm_uid:
        op_id_upper = str(uuid.uuid4()).upper()
        sfx = _sfx()
        with store._connect() as c:
            key_id = c.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, label, created_by, client_op_id) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (f"hash-legacyup-{sfx}", f"pfxlgu{sfx}"[:12], "legacy", adm_uid, op_id_upper),
            ).fetchone()["id"]
        rec = client.post(f"{_ADMIN_KEYS}/recover", json={"client_op_id": op_id_upper.lower()})
        assert rec.status_code == 200, rec.text
        assert rec.json()["found"] is True
        assert rec.json()["id"] == key_id


def _recover(route, op_id):
    r = client.post(f"{route}/recover", json={"client_op_id": op_id})
    assert r.status_code == 200, r.text
    return r.json()


def _list_row(route, key_id):
    return next(x for x in client.get(route).json()["keys"] if x["id"] == key_id)


def test_key_recover_scoped_to_self_actor_does_not_reach_other_owners_key():
    """回復は認証主体自身が発行操作した行にしか一致しない。B が A の client_op_id で試みても
    A のキーは無傷（別所有者衝突の反転テスト）。"""
    op_id = str(uuid.uuid4())
    sfx = _sfx()
    with _self_issue_enabled():
        uid_a, pw_a = _mk_user(f"{sfx}a")
        _login(uid_a, pw_a)
        created = client.post(_SELF_KEYS, json={"label": f"recA-{sfx}", "client_op_id": op_id})
        assert created.status_code == 200, created.text
        key_id = created.json()["id"]
        _logout()

        _login(*_mk_user(f"{sfx}b"))
        assert _recover(_SELF_KEYS, op_id)["found"] is False
        _logout()

        _login(uid_a, pw_a)
        assert _list_row(_SELF_KEYS, key_id)["revoked_at"] is None   # A のキーは無傷
        rec_a = _recover(_SELF_KEYS, op_id)
        assert rec_a["found"] is True
        assert rec_a["id"] == key_id
        _logout()


def test_key_recover_admin_self_success_and_different_admin_denied():
    """admin 自身の回復は成功し、別の admin が同じ client_op_id で試みても見つからない（`created_by` で厳密に絞る）。"""
    sfx = _sfx()
    adm1 = _mk_admin(f"{sfx}1")
    adm2 = _mk_admin(f"{sfx}2")
    op_id = str(uuid.uuid4())

    _login(*adm1)
    created = client.post(_ADMIN_KEYS, json={"label": f"rec1-{sfx}", "client_op_id": op_id})
    assert created.status_code == 200, created.text
    key_id = created.json()["id"]
    _logout()

    _login(*adm2)
    assert _recover(_ADMIN_KEYS, op_id)["found"] is False
    _logout()

    _login(*adm1)
    assert _list_row(_ADMIN_KEYS, key_id)["revoked_at"] is None   # 別 admin の回復では無傷
    rec_self = _recover(_ADMIN_KEYS, op_id)
    assert rec_self["found"] is True
    assert rec_self["id"] == key_id
    _logout()


def test_key_recover_admin_and_self_rows_do_not_cross_match():
    """admin 回復は自己発行キー（owner_uid 非 NULL）に、自己発行の回復は admin 発行キーに一致しない
    （client_op_id はグローバル一意のため別々の値で、行の種別が交差しないことを見る）。"""
    admin_op_id, self_op_id = str(uuid.uuid4()), str(uuid.uuid4())
    sfx = _sfx()
    with _self_issue_enabled() as adm:
        _login(adm.uid, adm.pw)
        created_admin = client.post(_ADMIN_KEYS, json={"label": f"crossA-{sfx}", "client_op_id": admin_op_id})
        assert created_admin.status_code == 200, created_admin.text
        _logout()

        _login(*_mk_user(sfx))
        created_self = client.post(_SELF_KEYS, json={"label": f"crossB-{sfx}", "client_op_id": self_op_id})
        assert created_self.status_code == 200, created_self.text
        assert _recover(_SELF_KEYS, admin_op_id)["found"] is False
        _logout()

        _login(adm.uid, adm.pw)
        assert _recover(_ADMIN_KEYS, self_op_id)["found"] is False
        assert _list_row(_ADMIN_KEYS, created_admin.json()["id"])["revoked_at"] is None
        assert _list_row(_ADMIN_KEYS, created_self.json()["id"])["revoked_at"] is None
        _logout()


def test_key_recover_creates_audit_row():
    """回復の試行は監査ログに残る（`ext_api.key_recover_attempted`・resource_id は失効したキーの id）。"""
    with _as_admin() as adm_uid:
        op_id = str(uuid.uuid4())
        created = client.post(_ADMIN_KEYS, json={"label": f"audrec-{_sfx()}", "client_op_id": op_id})
        assert created.status_code == 200, created.text
        assert _recover(_ADMIN_KEYS, op_id)["found"] is True

    rows = store.list_audit(actor=adm_uid, action="ext_api.key_recover_attempted", limit=10)
    assert [r for r in rows if r["resource_id"] == str(created.json()["id"])], f"回復の監査行が無い: {rows}"


def test_check_constraint_rejects_out_of_range_daily_quota_at_db_level():
    """アプリ層のバリデーションを迂回した直接 SQL でも、DB の CHECK 制約が範囲外の daily_quota を拒否する。"""
    sfx = _sfx()
    with store._connect() as c:
        with pytest.raises(Exception) as exc_info:
            c.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, label, created_by, daily_quota) "
                "VALUES (%s,%s,%s,%s,%s)",
                (f"hash-chk-{sfx}", f"pfxchk{sfx}"[:12], f"chk-{sfx}", "admin", 2_000_000))
    assert ("api_keys_daily_quota_range" in str(exc_info.value)
            or "check constraint" in str(exc_info.value).lower())


def test_self_key_forbidden_when_disabled():
    """既定 OFF では利用者は自己発行できず、一覧・失効・回復も同じゲートで 403。"""
    _login(*_mk_user(_sfx()))
    try:
        assert client.post(_SELF_KEYS, json={"label": f"self-{_sfx()}"}).status_code == 403
        assert client.get(_SELF_KEYS).status_code == 403
        assert client.delete(f"{_SELF_KEYS}/999999999").status_code == 403
        assert client.post(f"{_SELF_KEYS}/recover", json={"client_op_id": str(uuid.uuid4())}).status_code == 403
    finally:
        _logout()


def test_self_key_create_list_revoke_when_enabled():
    """許可時は自己発行→一覧→本人失効ができ、他人からは見えない・失効もできない（IDOR 無し）。"""
    sfx = _sfx()
    with _self_issue_enabled() as adm:
        uid, pw = _mk_user(sfx)
        other_uid, other_pw = _mk_user(f"o{sfx}")

        _login(uid, pw)
        r = client.post(_SELF_KEYS, json={"label": f"self-{sfx}"})
        assert r.status_code == 200, r.text
        created = r.json()
        assert created["key"].startswith("sk-ext-")
        listed = client.get(_SELF_KEYS).json()["keys"]
        assert {row["id"] for row in listed} == {created["id"]}
        assert listed[0]["allowed_worlds"] is None   # 現状「全員全 world」
        _logout()

        _login(other_uid, other_pw)
        assert client.get(_SELF_KEYS).json()["keys"] == []
        assert client.delete(f"{_SELF_KEYS}/{created['id']}").status_code == 404   # 所有権の有無を外に出さない
        _logout()

        _login(uid, pw)
        r = client.delete(f"{_SELF_KEYS}/{created['id']}")
        assert r.status_code == 200, r.text
        assert r.json()["revoked_at"] is not None
        _logout()

        _login(adm.uid, adm.pw)   # admin は利用者発行分も見える
        assert _list_row(_ADMIN_KEYS, created["id"])["owner_uid"] == uid
        _logout()


def test_self_key_create_daily_quota_is_admin_controlled():
    """自己発行キーの daily_quota は管理者統制: 未指定は既定を適用（空欄で無制限にならない）・上限超過は 422。"""
    with _self_issue_enabled(user_api_keys_daily_quota_default=5) as adm:
        assert adm.put.json()["ext_keys"]["daily_quota_default"]["effective"] == 5
        _login(*_mk_user(_sfx()))
        r = client.post(_SELF_KEYS, json={"label": f"quotadef-{_sfx()}"})
        assert r.status_code == 200, r.text
        assert r.json()["daily_quota"] == 5

        r = client.post(_SELF_KEYS, json={"label": f"quotaok-{_sfx()}", "daily_quota": 3})
        assert r.status_code == 200, r.text
        assert r.json()["daily_quota"] == 3

        r = client.post(_SELF_KEYS, json={"label": f"quotaover-{_sfx()}", "daily_quota": 6})
        assert r.status_code == 422, r.text
        _logout()


def test_self_key_create_daily_quota_fallback_default_when_admin_unset():
    """管理者が既定/上限を設定していなくても組み込みのフォールバック既定が適用される。"""
    with _self_issue_enabled():
        _login(*_mk_user(_sfx()))
        r = client.post(_SELF_KEYS, json={"label": f"quotafallback-{_sfx()}"})
        assert r.status_code == 200, r.text
        assert r.json()["daily_quota"] == store.SELF_ISSUED_DAILY_QUOTA_DEFAULT_FALLBACK
        _logout()


def test_self_issued_key_audit_detail_has_owner_uid_on_success_401_429_and_fallback():
    """自己発行キーの `detail.owner_uid` が、行を特定できる全ての監査経路（成功・429・所有者無効の 401・
    フォールバック）で付与される（actor は `ext:{key_id}` のまま）。"""
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    with _self_issue_enabled():
        try:
            _login(uid, pw)
            r = client.post(_SELF_KEYS, json={"label": f"ownaudit-{sfx}", "daily_quota": 1})
            assert r.status_code == 200, r.text
            key, key_id = r.json()["key"], r.json()["id"]
            _logout()

            assert _convert("a.docx", _make_docx_bytes(), key).status_code == 200
            assert _convert("a.docx", _make_docx_bytes(), key).status_code == 429   # daily_quota=1 を使い切り
            store.upsert_user(uid, role="user", status="disabled")
            assert _convert("a.docx", _make_docx_bytes(), key).status_code == 401   # 所有者無効
            store.upsert_user(uid, role="user", status="active")
            # require_api_key 自体が実行されない終了経路（malformed body）。
            r = client.post("/ext/v1/search", content=b"{not valid json",
                            headers={**_h(key, f"probe-ownaudit-fallback-{sfx}"),
                                     "Content-Type": "application/json"})
            assert r.status_code == 422, r.text

            with store._connect() as c:
                rows = c.execute(
                    "SELECT outcome, reason, detail FROM audit_log WHERE actor_user_id=%s ORDER BY id ASC",
                    (f"ext:{key_id}",)).fetchall()
            for label, match in (("success", lambda r: r["outcome"] == "success"),
                                 ("daily_quota_exceeded", lambda r: r["reason"] == "daily_quota_exceeded"),
                                 ("owner_inactive", lambda r: r["reason"] == "owner_inactive"),
                                 ("request_incomplete", lambda r: r["reason"] == "request_incomplete")):
                row = next((r for r in rows if match(r)), None)
                assert row is not None, f"{label} の監査行が見つからない: {rows}"
                assert row["detail"].get("owner_uid") == uid, label
        finally:
            store.upsert_user(uid, role="user", status="active")


def test_self_key_create_disallowed_after_toggle_off_revokes_existing():
    """OFF に戻すと利用者発行キーは一括失効し、以後の認証でも締め出される（PUT /admin/settings の一括失効＋
    `_verify_key_sync` の二重チェック）。"""
    with _self_issue_enabled() as adm:
        _login(*_mk_user(_sfx()))
        r = client.post(_SELF_KEYS, json={"label": f"toggle-{_sfx()}"})
        assert r.status_code == 200, r.text
        key = r.json()["key"]
        _logout()
        assert _convert("a.docx", _make_docx_bytes(), key).status_code == 200

        _login(adm.uid, adm.pw)
        r = client.put("/admin/settings", json={"user_api_keys_allowed": False})
        assert r.status_code == 200, r.text
        assert r.json()["ext_keys"]["user_api_keys_allowed"] is False
        _logout()
        assert _convert("a.docx", _make_docx_bytes(), key).status_code == 401


# ===== キー発行の監査（失敗・未処理例外・malformed body） =====

def test_key_create_failure_audits_requested_label_and_allowed_worlds():
    """発行が 422（未知の world）で失敗しても、監査行には検証前の入力（label/allowed_worlds）が残る。"""
    rid = f"probe-keycreate-fail-{_sfx()}"
    label = f"failaudit-{_sfx()}"
    with _as_admin():
        r = client.post(_ADMIN_KEYS, json={"label": label, "allowed_worlds": ["nonexistent-world-zzz"]},
                        headers=_h(rid=rid))
        assert r.status_code == 422, r.text

    row = _audit_one(rid)
    assert row["detail"]["label"] == label
    assert row["detail"]["allowed_worlds"] == ["nonexistent-world-zzz"]
    assert row["detail"]["http_status"] == 422
    assert row["outcome"] == "error"
    assert row["reason"] == "validation_error"


def test_key_create_unhandled_exception_audits_status_outcome_reason(monkeypatch):
    """検証通過後の未処理例外で 500 になっても `ExtRequestMiddleware` が 500 を組み立てて監査する。
    500 は `_HTTP_OUTCOME_REASON` に無いため reason は既定の "error"。"""
    monkeypatch.setattr(store, "insert_api_key", _raise(RuntimeError("simulated db outage during key insert")))
    rid = f"probe-keycreate-500-{_sfx()}"
    with _as_admin():
        r = client.post(_ADMIN_KEYS, json={"label": f"boom500-{_sfx()}", "allowed_worlds": ["v1"]},
                        headers=_h(rid=rid))
        assert r.status_code == 500, r.text
        assert r.headers["X-Request-Id"] == rid

    row = _audit_one(rid)
    assert row["detail"]["http_status"] == 500
    assert row["outcome"] == "error"
    assert row["reason"] == "error"


def test_key_create_malformed_json_body_returns_clean_422_without_stray_audit_row():
    """admin ルート（Cookie 認証）の malformed JSON は `start_audit()` 前に失敗し、フォールバック監査
    （X-API-Key の 5 ルート限定）の対象外＝X-Request-Id 付きの綺麗な 422 で、迷子の監査行を残さない。"""
    rid = f"probe-keycreate-malformed-{_sfx()}"
    with _as_admin():
        r = client.post(_ADMIN_KEYS, content=b"{not valid json at all",
                        headers={"Content-Type": "application/json", "X-Request-Id": rid})
        assert r.status_code == 422, r.text
        assert r.headers["X-Request-Id"] == rid
    assert _audit_rows(rid, "id") == []


def test_admin_key_routes_are_audited():
    """管理系 3 ルート（発行/一覧/失効）も同じ request-level 監査へ統合され、プレーンキーは detail に出ない。"""
    sfx = _sfx()
    rid_create, rid_list, rid_revoke = (f"probe-admin-create-{sfx}", f"probe-admin-list-{sfx}",
                                        f"probe-admin-revoke-{sfx}")
    with _as_admin() as adm_uid:
        r = client.post(_ADMIN_KEYS, json={"label": f"adminaudit-{sfx}"}, headers=_h(rid=rid_create))
        assert r.status_code == 200, r.text
        key_id, plain_key = r.json()["id"], r.json()["key"]
        assert client.get(_ADMIN_KEYS, headers=_h(rid=rid_list)).status_code == 200
        r = client.delete(f"{_ADMIN_KEYS}/{key_id}", headers=_h(rid=rid_revoke))
        assert r.status_code == 200, r.text

    with store._connect() as c:
        rows = {row["request_id"]: row for row in c.execute(
            "SELECT request_id, action, actor_user_id, detail FROM audit_log WHERE request_id = ANY(%s)",
            ([rid_create, rid_list, rid_revoke],)).fetchall()}
    assert set(rows) == {rid_create, rid_list, rid_revoke}
    assert rows[rid_create]["action"] == "ext_api.key_created"
    assert rows[rid_create]["actor_user_id"] == adm_uid
    assert rows[rid_list]["action"] == "ext_api.key_listed"
    assert rows[rid_revoke]["action"] == "ext_api.key_revoked"
    for rid, row in rows.items():
        assert plain_key not in json.dumps(row["detail"]), (
            f"監査行 {rid}（{row['action']}）の detail にプレーンキー本体が含まれている: {row['detail']}")


# ===== X-Request-Id =====

def test_request_id_echoed_generated_and_recorded_in_audit():
    key = _key("reqid")["key"]
    r = _capabilities(key, "probe-req-1")
    assert r.status_code == 200, r.text
    assert r.headers["X-Request-Id"] == "probe-req-1"

    r2 = _capabilities(key)   # 未指定なら採番される
    assert r2.status_code == 200, r2.text
    assert r2.headers.get("X-Request-Id")

    r3 = _capabilities(rid="probe-req-401")   # 401 にも付く（キー不正でも request_id は解決される）
    assert r3.status_code == 401, r3.text
    assert r3.headers["X-Request-Id"] == "probe-req-401"

    rid = f"probe-audit-req-{_sfx()}"   # 固定リテラルだと共有 DB の過去実行行と衝突しうる
    assert _capabilities(key, rid).status_code == 200
    assert _audit_one(rid, "request_id")["request_id"] == rid


def test_request_id_present_on_representative_error_statuses(monkeypatch):
    """403/404/422/413/429 の代表的な失敗応答にも X-Request-Id が付く（`ExtRequestMiddleware` が一元付与）。"""
    scoped = _key("reqid403", allowed_worlds=["v1"])["key"]
    plain = _key("reqidplain")["key"]

    cases = [
        (403, _search({"world": "other-world-xyz", "query": "x"}, scoped, "probe-403")),
        (404, _search({"world": "no-such-world-xyz", "query": "x"}, plain, "probe-404")),
        (422, _search({"world": "v1", "query": "x", "scope_paths": ["no-such-scope-xyz"]}, plain,
                           "probe-422")),
    ]
    monkeypatch.setattr(ext_api, "_CONVERT_MAX_BYTES", 10)
    cases.append((413, _convert("big.docx", b"x" * 100, plain, "probe-413")))
    monkeypatch.setattr(ext_api.ratelimit, "check_ext_api_rate_limit", lambda key_id: 3)
    cases.append((429, _capabilities(plain, "probe-429")))
    for status, r in cases:
        assert r.status_code == status, r.text
        assert r.headers["X-Request-Id"] == f"probe-{status}"


def test_request_id_appears_on_application_log_records(caplog):
    """束縛された request_id が共通ロガー（"sherpa"）のアプリログの `record.request_id` にも乗る。"""
    import logging
    key = _key("logreqid")["key"]
    with caplog.at_level(logging.INFO, logger="sherpa"):
        assert _capabilities(key, "probe-log-reqid").status_code == 200
    assert [rec for rec in caplog.records if getattr(rec, "request_id", None) == "probe-log-reqid"], (
        f"request_id='probe-log-reqid' を持つログレコードが無い: "
        f"{[(rec.name, rec.getMessage()) for rec in caplog.records]}")


# ===== request-level 監査（ExtRequestMiddleware・自動422/未処理例外を含む全終了経路）=====

def test_auto_validation_422_is_audited():
    """handler 本体が実行されない自動バリデーション 422 も、仮置きの action/actor と実ステータスで監査される。"""
    rid = f"probe-auto422-{_sfx()}"
    r = _search({"world": "v1", "query": "x", "k": 999}, _key("auto422")["key"], rid)
    assert r.status_code == 422, r.text
    row = _audit_rows(rid)[0]
    assert row["outcome"] == "error"
    assert row["reason"] == "validation_error"
    assert row["detail"]["http_status"] == 422


def test_unhandled_exception_gets_request_id_and_is_audited(monkeypatch):
    monkeypatch.setattr(fused_search, "search", _raise(RuntimeError("simulated bug")))
    rid = f"probe-500-{_sfx()}"
    r = _search({"world": "v1", "query": "x"}, _key("unhandled500")["key"], rid)
    assert r.status_code == 500, r.text
    assert r.headers["X-Request-Id"] == rid
    assert _audit_rows(rid, "detail")[0]["detail"]["http_status"] == 500


def test_audit_row_has_all_fields_exactly_once():
    r = _search({"world": "v1", "query": "税", "engines": ["keyword"]}, _key("auditfields")["key"])
    assert r.status_code == 200, r.text
    with store._connect() as c:
        rows = c.execute("SELECT outcome, detail FROM audit_log WHERE action='ext_api.search' "
                         "AND request_id=%s", (r.headers["X-Request-Id"],)).fetchall()
    assert len(rows) == 1, f"監査行はちょうど1件のはず（実際 {len(rows)} 件）"
    detail = rows[0]["detail"]
    for field in ("http_status", "duration_ms", "result_count", "method", "path", "world", "prefix",
                  "business_outcome"):
        assert field in detail, f"detail に {field} が無い: {detail}"
    assert detail["http_status"] == 200
    assert detail["method"] == "POST"
    assert detail["path"] == "/ext/v1/search"
    assert rows[0]["outcome"] == "success"


@pytest.mark.parametrize("api_key, expected_actor", [("issued", "issued"), ("sk-ext-totally-bogus-key-xyz", "ext:unknown")],
                         ids=["valid_key", "unknown_key"])
def test_malformed_json_body_is_still_audited(api_key, expected_actor):
    """本文が JSON として解析できず `require_api_key` 前に失敗しても、フォールバックが X-API-Key から
    identity を解決して監査行を 1 件書く（無効なキーなら actor は "ext:unknown"）。"""
    issued = _key("malformed")
    if api_key == "issued":
        api_key, expected_actor = issued["key"], f"ext:{issued['id']}"
    rid = f"probe-malformed-{_sfx()}"
    r = client.post("/ext/v1/search", content=b"{not valid json at all",
                    headers={**_h(api_key, rid), "Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    assert r.headers["X-Request-Id"] == rid

    row = _audit_rows(rid, "actor_user_id, outcome, reason, detail")[-1]
    assert row["actor_user_id"] == expected_actor
    if expected_actor != "ext:unknown":
        assert row["detail"]["business_outcome"] == "failed"


def _minimal_asgi_http_scope(path: str, headers: list) -> dict:
    """手組みの ASGI HTTP scope。`query_string`/`scheme`/`server`/`client`/`root_path` を省くと、応答開始前に
    例外が起きた経路で Starlette の `ServerErrorMiddleware` が KeyError を起こすことがある。"""
    return {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
            "query_string": b"", "scheme": "http", "root_path": "",
            "server": ("testserver", 80), "client": ("testclient", 12345), "headers": headers}


def _bare_ext_app():
    """`ExtRequestMiddleware` 未装着の裸の FastAPI app（`sherpa.api.app` は装着済みで、直接包むと二重に呼ばれ
    監査行が複数書かれる）。middleware 単体テスト専用。"""
    from fastapi import FastAPI
    bare = FastAPI()
    bare.include_router(ext_api.router)
    return bare


def _run_through_middleware(api_key: str, rid: str, send, expect_exc):
    """`/ext/v1/capabilities` を `ExtRequestMiddleware` へ直接流し、`send` が投げた例外を返す。"""
    async def _run():
        scope = _minimal_asgi_http_scope(
            "/ext/v1/capabilities", [(b"x-api-key", api_key.encode()), (b"x-request-id", rid.encode())])

        async def _receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        with pytest.raises(expect_exc) as ei:
            await ext_api.ExtRequestMiddleware(_bare_ext_app())(scope, _receive, send)
        return ei.value

    return asyncio.run(_run())


def _assert_failed_delivery_audit(rid, reason, http_status):
    row = _audit_one(rid)
    assert row["outcome"] == "error", row
    assert row["reason"] == reason
    assert row["detail"]["business_outcome"] == "failed"
    assert row["detail"]["http_status"] == http_status


def test_cancelled_request_still_writes_audit_and_resets_contextvar():
    """応答完了前の `CancelledError` でも、監査書込と ContextVar reset は `asyncio.shield()` で完了する。
    status は 0 のまま success 扱いにならない。"""
    rid = f"probe-cancel-{_sfx()}"

    async def _send(message):
        if message["type"] == "http.response.start":
            raise asyncio.CancelledError()

    _run_through_middleware(_key("cancel")["key"], rid, _send, asyncio.CancelledError)
    assert ext_api._request_id_ctx.get() is None, "ContextVar がリクエスト後にリセットされていない"
    _assert_failed_delivery_audit(rid, "cancelled", 0)


def test_send_start_failure_with_self_generated_500_also_failing_is_audited():
    """`http.response.start` の送信が失敗し、自己生成 500 の再送も失敗しても、例外を握り潰さず
    outcome=error・reason=delivery_failed で監査し、最初の送信例外を再送出する。"""
    rid = f"probe-sendfail-{_sfx()}"
    raised: list[Exception] = []

    async def _send(message):
        exc = ConnectionResetError("simulated early disconnect (every send fails)")
        raised.append(exc)
        raise exc

    err = _run_through_middleware(_key("sendfail")["key"], rid, _send, ConnectionResetError)
    assert len(raised) >= 2, f"send が 2 回以上（start 失敗＋自己生成500の再送失敗）呼ばれるはず（実際 {len(raised)} 回）"
    assert err is raised[0], "再送出された例外が最初の送信失敗そのものではない（後続の例外にすり替わっている）"
    _assert_failed_delivery_audit(rid, "delivery_failed", 0)


def test_send_body_failure_after_start_succeeds_is_audited_as_delivery_failed():
    """`http.response.start` 成功後の body 送信失敗（早期切断）は、観測できた status（200）を残したまま
    "success" に誤記録せず delivery_failed で監査する。"""
    rid = f"probe-bodyfail-{_sfx()}"

    async def _send(message):
        if message["type"] == "http.response.body":
            raise ConnectionResetError("simulated disconnect mid-body")

    _run_through_middleware(_key("bodyfail")["key"], rid, _send, ConnectionResetError)
    _assert_failed_delivery_audit(rid, "delivery_failed", 200)


def test_audit_write_does_not_block_event_loop(monkeypatch):
    """監査 DB 書込は専用 writer スレッドへ逃がされ、書込みが遅延しても同じ event loop の他コルーチンは進む。"""
    orig_write = ext_api._write_pending_audit
    release = threading.Event()

    def _slow_write(pending, *a, **kw):
        release.wait(timeout=5)
        return orig_write(pending, *a, **kw)

    monkeypatch.setattr(ext_api, "_write_pending_audit", _slow_write)
    rid = f"probe-evloop-{_sfx()}"

    async def _run():
        pending = ext_api._init_audit_pending("ext_api.search", "ext_search", "ext:evloop-test")
        write_task = asyncio.ensure_future(
            ext_api._write_pending_audit_async(pending, 200, 1.0, "GET", "/x", rid))
        await asyncio.sleep(0.1)   # 書込みが release.wait() で止まっている間に…

        progressed = {"v": False}

        async def _other_coro():
            await asyncio.sleep(0.05)
            progressed["v"] = True

        await asyncio.wait_for(_other_coro(), timeout=2)   # …別コルーチンが進む
        assert progressed["v"] is True
        assert not write_task.done(), "監査書込みがまだ release 待ちのはず"
        release.set()
        await asyncio.wait_for(write_task, timeout=5)

    asyncio.run(_run())
    assert _audit_one(rid, "actor_user_id")["actor_user_id"] == "ext:evloop-test"


# ===== POST /ext/v1/answer（簡易チャットの同期応答・C-EXT-ANSWER-01）=====
#
# LLM は外部境界のため HTTP 層（`agentic_search._post`）で偽装し、ES/グラフを不可にして提示ツールを
# ripgrep_search/read_around に絞る。

def _install_answer_post(monkeypatch, seq):
    monkeypatch.setattr(agentic_search.es_index, "available", lambda: False)
    monkeypatch.setattr(agentic_search, "_graph_available", lambda: False)
    monkeypatch.setattr(agentic_search, "_tools_availability_cache", {"at": 0.0, "data": None})
    monkeypatch.setattr(agentic_search, "_post", lambda url, headers, body, timeout=90: seq.pop(0))


def _tool_call_round(call_id, name, arguments):
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": call_id, "function": {"name": name, "arguments": arguments}}]}}]}


def test_ext_answer_sources_only_real_touched_verified_docs(monkeypatch):
    """出典は、この回答中に実際に道具で触れ、かつ実在確認できた doc_id だけ。"""
    _install_answer_post(monkeypatch, [
        # 0 件ヒットのダミー語（read_around だけが唯一の「触れた doc_id」になる）。
        _tool_call_round("c1", "ripgrep_search", '{"query":"該当なしのダミー語xyz99"}'),
        _tool_call_round("c2", "read_around", f'{{"doc_id":"{_ANSWER_REAL_DOC}","line":1}}'),
        {"choices": [{"message": {"content": "障害の記録を確認しました。"}, "finish_reason": "stop"}]},
    ])
    r = _answer({"world": "v1", "query": "税率改定の障害は？"}, _key("answerok")["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "障害の記録を確認しました。"
    assert body["tool_calls"] == 2
    assert body["unconfirmed"] is False
    assert "unconfirmed_reason" not in body   # 確かめられた回答では添えない
    assert [s["doc_id"] for s in body["sources"]] == [_ANSWER_REAL_DOC]


def test_ext_answer_unconfirmed_when_no_verified_sources(monkeypatch):
    """道具を一度も使わず回答した場合、出典 0 件のまま黙って消さず `unconfirmed=True` を明示する。"""
    _install_answer_post(monkeypatch, [
        {"choices": [{"message": {"content": "資料からは確認できませんでした。"}, "finish_reason": "stop"}]}])
    r = _answer({"world": "v1", "query": "存在しない話題"}, _key("answernosrc")["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sources"] == []
    assert body["tool_calls"] == 0
    assert body["unconfirmed"] is True
    assert body["unconfirmed_reason"] is not None


def test_ext_answer_round_trip_and_tool_call_limits_not_exceeded(monkeypatch):
    """往復は最大 3 回・1 往復あたりの道具呼び出しは内部上限 4 回。毎回 5 件要求し一度も回答を終えなくても
    実行は 3×4=12 件で頭打ちになり、往復が尽きれば `unconfirmed=True`（黙って打ち切らない）。"""
    def _round_with_5_calls(n):
        return {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": f"r{n}-{i}", "function": {"name": "ripgrep_search",
             "arguments": '{"query":"該当なしのダミー語xyz"}'}} for i in range(5)]}}]}

    _install_answer_post(monkeypatch, [_round_with_5_calls(1), _round_with_5_calls(2), _round_with_5_calls(3)])
    r = _answer({"world": "v1", "query": "限界まで道具を呼ぶ"}, _key("answerlimits")["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool_calls"] == 12
    assert body["unconfirmed"] is True
    assert body["unconfirmed_reason"].startswith("round_trip_limit")


def test_ext_answer_llm_unavailable_returns_503_no_fallback(monkeypatch):
    """既定 AI が未接続（中央キー未設定）なら 503——黙って別プロバイダへフォールバックしない。"""
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"research_default_provider": "openai"})
    r = _answer({"world": "v1", "query": "x"}, _key("answernokey")["key"])
    assert r.status_code == 503, r.text
