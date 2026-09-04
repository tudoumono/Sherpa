"""外部連携 API（/ext/v1・E1: APIキー基盤＋POST /ext/v1/convert・E2c: POST /ext/v1/search＋openapi）のテスト。

要 Postgres。DB 不可は SKIP（tests/api の既存流儀）。admin キー発行/失効はセッション Cookie 認証
（`/admin/users` 系と同じ）・convert/search/openapi は X-API-Key ヘッダ認証（Cookie ではない）。
search 本体のエンジン分離/RRF融合ロジックは `sherpa/search_service.py` の
`tests/unit/test_search_service.py` で検証済み。ここでは認証・世界/scope検証・監査・
degrade 応答・openapi サブセットの配線を検証する（`sherpa.search_service.search` は monkeypatch で
スタブ化し ES/Neo4j 到達性に依存しない）。
"""
from __future__ import annotations

import io
import json
import os
import time
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, keys, store
from sherpa.api import app

client = TestClient(app, raise_server_exceptions=True)


# ---- ヘルパ ----

def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")   # 不可なら可視の skip（silent-green 根絶）


def _mk_admin(sfx: str) -> tuple[str, str]:
    uid = f"exta{sfx}"
    pw = f"pw-{uid}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(pw), role="admin", status="active")
    register_test_uid(uid)
    return uid, pw


def _mk_user(sfx: str) -> tuple[str, str]:
    uid = f"extu{sfx}"
    pw = f"pw-{uid}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(pw), role="user", status="active")
    register_test_uid(uid)
    return uid, pw


def _login(uid: str, pw: str) -> None:
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, f"login failed: {r.text}"


def _logout() -> None:
    client.post("/auth/logout")


def _issue_key(label: str) -> dict:
    """admin としてログイン済みの前提で発行し、プレーンキーを含む dict を返す。"""
    r = client.post("/ext/v1/admin/keys", json={"label": label})
    assert r.status_code == 200, r.text
    return r.json()


_DOCX_XML = """<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:body>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>タイトル見出し</w:t></w:r></w:p>
  <w:p><w:r><w:t>本文テキストABC</w:t></w:r></w:p>
 </w:body>
</w:document>"""


def _make_docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", _DOCX_XML)
    return buf.getvalue()


def _convert(filename: str, content: bytes, api_key: str | None = None):
    headers = {"X-API-Key": api_key} if api_key else {}
    return client.post(
        "/ext/v1/convert",
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
        headers=headers,
    )


def _search(payload: dict, api_key: str | None = None):
    headers = {"X-API-Key": api_key} if api_key else {}
    return client.post("/ext/v1/search", json=payload, headers=headers)


def _capabilities(api_key: str | None = None):
    headers = {"X-API-Key": api_key} if api_key else {}
    return client.get("/ext/v1/capabilities", headers=headers)


def _doc(world: str, path: str, api_key: str | None = None):
    headers = {"X-API-Key": api_key} if api_key else {}
    return client.get("/ext/v1/doc", params={"world": world, "path": path}, headers=headers)


def _issue_key_scoped(label: str, allowed_worlds) -> dict:
    """admin としてログイン済みの前提で、world スコープ付きで発行する。"""
    r = client.post("/ext/v1/admin/keys", json={"label": label, "allowed_worlds": allowed_worlds})
    assert r.status_code == 200, r.text
    return r.json()


# ===== APIキー基盤 =====

def test_ext_convert_requires_api_key():
    if not _try_init():
        pytest.skip("DB down")
    r = _convert("a.docx", _make_docx_bytes())
    assert r.status_code == 401, r.text


def test_admin_key_create_requires_admin():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)
    r = client.post("/ext/v1/admin/keys", json={"label": "x"})
    assert r.status_code == 403, r.text
    _logout()


def test_admin_key_lifecycle():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)

    # 発行: プレーンキーが sk-ext- で始まる。
    issued = _issue_key(f"lifecycle-{sfx}")
    plain = issued["key"]
    key_id = issued["id"]
    assert plain.startswith("sk-ext-"), issued

    # 一覧: key_prefix のみ（plain キーは出ない）。
    r = client.get("/ext/v1/admin/keys")
    assert r.status_code == 200, r.text
    rows = r.json()["keys"]
    row = next(x for x in rows if x["id"] == key_id)
    assert row["key_prefix"] == issued["key_prefix"]
    for v in row.values():
        if isinstance(v, str):
            assert plain not in v

    _logout()

    # このキーで convert が通る。
    r = _convert("a.docx", _make_docx_bytes(), api_key=plain)
    assert r.status_code == 200, r.text

    # DELETE で失効。
    _login(adm_uid, adm_pw)
    r = client.delete(f"/ext/v1/admin/keys/{key_id}")
    assert r.status_code == 200, r.text
    assert r.json()["revoked_at"]
    _logout()

    # 失効後は convert が 401。
    r = _convert("a.docx", _make_docx_bytes(), api_key=plain)
    assert r.status_code == 401, r.text

    # 再 DELETE は冪等に 200。
    _login(adm_uid, adm_pw)
    r = client.delete(f"/ext/v1/admin/keys/{key_id}")
    assert r.status_code == 200, r.text

    # 未知 id は 404。
    r = client.delete("/ext/v1/admin/keys/999999999")
    assert r.status_code == 404, r.text
    _logout()


def test_key_hash_only_in_db():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"hashonly-{sfx}")
    _logout()

    from sherpa import ext_api
    row = store.api_key_by_hash(ext_api._hash_key(issued["key"]))
    assert row is not None
    assert row["key_hash"] != issued["key"]
    assert issued["key"] not in row["key_hash"]


def test_revoked_key_rejected():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"revoke-{sfx}")
    r = client.delete(f"/ext/v1/admin/keys/{issued['id']}")
    assert r.status_code == 200, r.text
    _logout()

    r = _convert("a.docx", _make_docx_bytes(), api_key=issued["key"])
    assert r.status_code == 401, r.text


# ===== POST /ext/v1/convert =====

def test_convert_docx_roundtrip():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"convert-{sfx}")
    _logout()

    r = _convert("doc.docx", _make_docx_bytes(), api_key=issued["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unsupported"] is False
    assert body["method"] == "ooxml"
    assert "タイトル見出し" in body["md"]
    assert body["filename"] == "doc.docx"
    assert body["size_bytes"] == len(_make_docx_bytes())


def test_convert_rejects_extension():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"ext-{sfx}")
    _logout()

    r = _convert("a.txt", b"hello", api_key=issued["key"])
    assert r.status_code == 422, r.text


def test_convert_size_limit(monkeypatch):
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import ext_api
    monkeypatch.setattr(ext_api, "_CONVERT_MAX_BYTES", 1000)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"size-{sfx}")
    _logout()

    r = _convert("big.docx", b"x" * 2000, api_key=issued["key"])
    assert r.status_code == 413, r.text


def test_convert_zip_bomb_rejected(monkeypatch):
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import ext_api
    monkeypatch.setattr(ext_api, "_ZIP_MAX_UNCOMPRESSED", 100)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"zipbomb-{sfx}")
    _logout()

    # 展開サイズが上限（monkeypatch で 100 バイト）を超える docx。
    big_xml = _DOCX_XML + ("A" * 1000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", big_xml)
    r = _convert("bomb.docx", buf.getvalue(), api_key=issued["key"])
    assert r.status_code == 422, r.text


def test_convert_broken_file_unsupported():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"broken-{sfx}")
    _logout()

    # docx 拡張子だが中身は壊れた zip（BadZipFile）→ 変換は None＝未対応 200。
    r = _convert("broken.docx", b"not a real zip file", api_key=issued["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unsupported"] is True
    assert body["md"] is None
    assert body["reason"] == "conversion_failed"


def test_convert_tmpfile_cleaned(monkeypatch):
    if not _try_init():
        pytest.skip("DB down")
    from pathlib import Path

    from sherpa import ext_api

    created: list[Path] = []
    orig_to_markdown = ext_api.office_md.to_markdown

    def _spy(path):
        created.append(Path(path))
        return orig_to_markdown(path)

    monkeypatch.setattr(ext_api.office_md, "to_markdown", _spy)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"tmpclean-{sfx}")
    _logout()

    r = _convert("clean.docx", _make_docx_bytes(), api_key=issued["key"])
    assert r.status_code == 200, r.text
    assert created, "to_markdown が呼ばれていない（スパイ未到達）"
    for p in created:
        assert not p.exists(), f"一時ファイルが残っている: {p}"


# ===== POST /ext/v1/search（E2c）=====
#
# エンジン分離/RRF融合ロジック自体は tests/unit/test_search_service.py で検証済み。
# ここでは ext_api 層の配線（認証・world/scope 検証・監査・degraded 応答・openapi サブセット）を検証する。

def test_ext_search_requires_api_key():
    if not _try_init():
        pytest.skip("DB down")
    r = _search({"world": "v1", "query": "税"})
    assert r.status_code == 401, r.text


def test_ext_openapi_requires_api_key():
    if not _try_init():
        pytest.skip("DB down")
    r = client.get("/ext/v1/openapi.json")
    assert r.status_code == 401, r.text


def test_search_unknown_world_404():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"noworld-{sfx}")
    _logout()

    r = _search({"world": "no-such-world-xyz", "query": "税"}, api_key=issued["key"])
    assert r.status_code == 404, r.text


def test_search_unknown_scope_422():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"badscope-{sfx}")
    _logout()

    r = _search({"world": "v1", "query": "税", "scope_paths": ["no-such-scope-xyz"]},
                api_key=issued["key"])
    assert r.status_code == 422, r.text


def test_search_scope_validation_oserror_returns_503_and_audits_requested_prefix(monkeypatch):
    """`scope_mod.valid_scope_paths(strict=True)` は OSError を re-raise しうる契約
    （`scope.py` docstring）——search() 本体と同じ try/except に含める（未処理例外として漏らさ
    ない）。あわせて、失敗時の監査行にも要求された scope_paths（`detail["prefix"]`）・
    http_status・outcome・reason が残ることを固定する（検証前に正規化して積む）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import ext_api

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"scopeoserror-{sfx}")
    _logout()
    rid = f"probe-scope-oserror-{sfx}"

    def _boom(world, scope_paths, root=None, strict=False):
        raise OSError("simulated permission error during scope validation")

    monkeypatch.setattr(ext_api.scope_mod, "valid_scope_paths", _boom)
    r = client.post(
        "/ext/v1/search", json={"world": "v1", "query": "税", "scope_paths": ["01_受付"]},
        headers={"X-API-Key": issued["key"], "X-Request-Id": rid})
    assert r.status_code == 503, r.text

    with store._connect() as c:
        rows = c.execute(
            "SELECT outcome, reason, detail FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchall()
    assert len(rows) == 1, f"監査行はちょうど1件のはず（実際 {len(rows)} 件）"
    row = rows[0]
    assert row["detail"]["prefix"] == ["01_受付"]
    assert row["detail"]["http_status"] == 503
    assert row["outcome"] == "error"
    assert row["reason"] == "unavailable"   # `_HTTP_OUTCOME_REASON[503]`


def _assert_single_audit_row_has_world_and_prefix(rid, expected_world, expected_prefix, status_code,
                                                  expected_outcome, expected_reason):
    with store._connect() as c:
        rows = c.execute(
            "SELECT outcome, reason, detail FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchall()
    assert len(rows) == 1, f"監査行はちょうど1件のはず（実際 {len(rows)} 件）"
    row = rows[0]
    detail = row["detail"]
    assert detail["world"] == expected_world
    assert detail["prefix"] == expected_prefix
    assert detail["http_status"] == status_code
    assert row["outcome"] == expected_outcome
    assert row["reason"] == expected_reason


def test_search_scope_exclusion_403_audits_prefix_before_enforcement():
    """scope 外 world（403）でも、prefix は `_enforce_world_scope()` より前に積まれているため
    監査行に残る（後で積むと、403 で失敗したリクエストの監査に prefix が残らない）。未正規化の
    入力（連続スラッシュ・末尾スラッシュ）を与え、監査に残る値が正規化後（`normalize_scope_paths`
    通過後）であることも併せて固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key_scoped(f"scoped403prefix-{sfx}", ["v1"])
    _logout()
    rid = f"probe-search403-prefix-{sfx}"

    r = client.post(
        "/ext/v1/search",
        json={"world": "other-world-xyz", "query": "x", "scope_paths": ["4期//サブ/"]},
        headers={"X-API-Key": issued["key"], "X-Request-Id": rid})
    assert r.status_code == 403, r.text

    _assert_single_audit_row_has_world_and_prefix(
        rid, "other-world-xyz", ["4期//サブ"], 403, "deny", "world_not_allowed")


def test_search_unknown_world_404_audits_prefix_before_resolution():
    """未知の world（404）でも、prefix は `_resolve_world_or_error()` より前に積まれているため
    監査行に残る。未正規化の入力（連続スラッシュ・末尾スラッシュ）を与え、監査に残る値が
    正規化後であることも併せて固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"search404prefix-{sfx}")
    _logout()
    rid = f"probe-search404-prefix-{sfx}"

    r = client.post(
        "/ext/v1/search",
        json={"world": "no-such-world-xyz", "query": "x", "scope_paths": ["4期//サブ/"]},
        headers={"X-API-Key": issued["key"], "X-Request-Id": rid})
    assert r.status_code == 404, r.text

    _assert_single_audit_row_has_world_and_prefix(
        rid, "no-such-world-xyz", ["4期//サブ"], 404, "error", "not_found")


def test_search_registry_unreachable_503_audits_prefix_before_resolution(monkeypatch):
    """registry 到達不可（503）でも、prefix は `_resolve_world_or_error()` より前に積まれている
    ため監査行に残る。未正規化の入力（連続スラッシュ・末尾スラッシュ）を与え、監査に残る値が
    正規化後であることも併せて固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import worlds

    def _boom(world_id, **kw):
        raise worlds.ExternalResolverError("simulated registry outage")

    monkeypatch.setattr(worlds, "resolve_external_world", _boom)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"search503prefix-{sfx}")
    _logout()
    rid = f"probe-search503-prefix-{sfx}"

    r = client.post(
        "/ext/v1/search",
        json={"world": "v1", "query": "x", "scope_paths": ["4期//サブ/"]},
        headers={"X-API-Key": issued["key"], "X-Request-Id": rid})
    assert r.status_code == 503, r.text

    _assert_single_audit_row_has_world_and_prefix(
        rid, "v1", ["4期//サブ"], 503, "error", "unavailable")


def test_search_degraded_all_engines_down(monkeypatch):
    """ES/Neo4j 不可はエンジン単位の degraded（200）で返る（黙ってすり替えない・D3）。"""
    if not _try_init():
        pytest.skip("DB down")
    from neo4j.exceptions import ServiceUnavailable

    from sherpa import es_index, search_service

    monkeypatch.setattr(es_index, "available", lambda: False)

    class _RaiseCtx:
        def __enter__(self):
            raise ServiceUnavailable("down")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(search_service, "_neo4j_session", lambda: _RaiseCtx())

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"degrade-{sfx}")
    _logout()

    r = _search({"world": "v1", "query": "税", "engines": ["keyword", "vector", "graph"]},
                api_key=issued["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hits"] == []
    assert body["engines_used"] == []
    reasons = {d["engine"]: d["reason"] for d in body["degraded"]}
    assert reasons == {"keyword": "es_unavailable", "vector": "es_unavailable",
                       "graph": "neo4j_unavailable"}


def test_search_filters_nonexistent_docs(monkeypatch):
    """R3-S2: 削除直後の窓で ES 索引に古いまま残る doc_id は返さない
    （`documents.world_rel_set` の実在フィルタ・`chat_service._es_hits` と同型・search_service 側に集約）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import documents, search_service

    def _fake_keyword(world, query, sp, k, settings, layer=None):
        return ([{"key": "real.md", "doc_id": "real.md", "path": "real.md", "line": 1,
                  "snippet": "s", "engine_score": 1.0, "judgement": None, "paths": None},
                 {"key": "deleted.md", "doc_id": "deleted.md", "path": "deleted.md", "line": 1,
                  "snippet": "s", "engine_score": 1.0, "judgement": None, "paths": None}], None)

    monkeypatch.setattr(search_service, "_search_keyword", _fake_keyword)
    monkeypatch.setattr(documents, "world_rel_set",
                        lambda world=None, root=None, strict=False, **kw: {"real.md"})

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"realfilter-{sfx}")
    _logout()

    r = _search({"world": "v1", "query": "x", "engines": ["keyword"]}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {h["doc_id"] for h in body["hits"]}
    assert ids == {"real.md"}
    assert "deleted.md" not in ids
    assert body["engines_used"] == ["keyword"]   # フィルタで件数が減っても degrade 扱いにはしない


def test_search_audit_written():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"audit-{sfx}")
    _logout()
    rid = f"probe-search-audit-{sfx}"

    r = client.post(
        "/ext/v1/search", json={"world": "v1", "query": "税計算", "engines": ["keyword"]},
        headers={"X-API-Key": issued["key"], "X-Request-Id": rid})
    assert r.status_code == 200, r.text

    with store._connect() as c:
        row = c.execute(
            "SELECT actor_user_id, action, resource_type, resource_id, detail FROM audit_log "
            "WHERE request_id=%s", (rid,)
        ).fetchone()
    assert row is not None
    assert row["actor_user_id"] == f"ext:{issued['id']}"
    assert row["resource_type"] == "ext_search"
    assert row["resource_id"] == "v1"
    assert row["detail"]["query"] == "税計算"
    assert row["detail"]["engines"] == ["keyword"]


def test_ext_openapi_subset():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"openapi-{sfx}")
    _logout()

    r = client.get("/ext/v1/openapi.json", headers={"X-API-Key": issued["key"]})
    assert r.status_code == 200, r.text
    doc = r.json()

    assert set(doc["paths"].keys()) == {
        "/ext/v1/convert", "/ext/v1/search", "/ext/v1/capabilities", "/ext/v1/doc",
        "/ext/v1/research"}
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


# ===== search: depth パラメータ =====

def test_search_depth_passthrough(monkeypatch):
    """`ExtSearchReq.depth` が `search_service.search(depth=...)` まで素通しされる。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import search_service

    captured = {}

    def _fake_search(world, query, engines=None, k=10, scope_paths=None, weights=None,
                     settings=None, depth=8, root=None, strict=False, layer=None):
        captured["depth"] = depth
        return {"hits": [], "engines_used": [], "degraded": []}

    monkeypatch.setattr(search_service, "search", _fake_search)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"depth-{sfx}")
    _logout()

    r = _search({"world": "v1", "query": "x", "depth": 3}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    assert captured["depth"] == 3

    r = _search({"world": "v1", "query": "x"}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    assert captured["depth"] == 8   # 既定（従来の固定値と同じ）


def test_search_depth_out_of_range_422(monkeypatch):
    """`depth` の上限（le）は元の外部 API 契約 12 を後退させない（`SHERPA_IMPACT_MAX_DEPTH`
    未設定/12未満でも `max(12, IMPACT_MAX_DEPTH)`＝env 未設定時は従来どおり 12 のまま）。
    0/13 は従来どおり範囲外、9〜12 は従来どおり受理されることの両方を固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import search_service

    monkeypatch.setattr(search_service, "search",
                        lambda *a, **kw: {"hits": [], "engines_used": [], "degraded": []})

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"depthrange-{sfx}")
    _logout()

    for bad in (0, 13):
        r = _search({"world": "v1", "query": "x", "depth": bad}, api_key=issued["key"])
        assert r.status_code == 422, r.text

    for ok in (9, 10, 11, 12):
        r = _search({"world": "v1", "query": "x", "depth": ok}, api_key=issued["key"])
        assert r.status_code == 200, r.text


def test_search_depth_reaches_run_impact(monkeypatch):
    """`search_service.search` 自体は monkeypatch せず、`_search_graph`→`run_impact` の実配線を
    通して depth の最終到達値を確認する（`test_search_depth_passthrough` は search() 止まり）。"""
    if not _try_init():
        pytest.skip("DB down")
    from contextlib import contextmanager

    from sherpa import search_service

    captured = {}

    def _fake_run_impact(session, term, world, aliasmap=None, scope_prefixes=None, depth=8, **kw):
        captured["depth"] = depth
        return {"items": [], "presumed": []}

    @contextmanager
    def _fake_session():
        yield object()

    monkeypatch.setattr(search_service, "_neo4j_session", _fake_session)
    monkeypatch.setattr(search_service, "run_impact", _fake_run_impact)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"depthreach-{sfx}")
    _logout()

    r = _search({"world": "v1", "query": "x", "engines": ["graph"], "depth": 5}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    assert captured["depth"] == 5


# ===== 探す対象（層フィルタ・調べ方ブロック §3.4）=====

def test_search_layer_passthrough(monkeypatch):
    """`ExtSearchReq.layer` が `search_service.search(layer=...)` まで素通しされる（既定 both）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import search_service

    captured = {}

    def _fake_search(world, query, engines=None, k=10, scope_paths=None, weights=None,
                     settings=None, depth=8, root=None, strict=False, layer=None):
        captured["layer"] = layer
        return {"hits": [], "engines_used": [], "degraded": []}

    monkeypatch.setattr(search_service, "search", _fake_search)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"layer-{sfx}")
    _logout()

    r = _search({"world": "v1", "query": "x", "layer": "code"}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    assert captured["layer"] == "code"

    r = _search({"world": "v1", "query": "x"}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    assert captured["layer"] == "both"   # 既定


def test_search_layer_invalid_value_422():
    """不正な layer 値は 422（Literal 制約）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"layerbad-{sfx}")
    _logout()

    r = _search({"world": "v1", "query": "x", "layer": "bogus"}, api_key=issued["key"])
    assert r.status_code == 422, r.text


# ===== GET /ext/v1/capabilities（discovery）=====

def test_capabilities_requires_api_key():
    if not _try_init():
        pytest.skip("DB down")
    r = _capabilities()
    assert r.status_code == 401, r.text


def test_capabilities_lists_worlds_and_features():
    """`v1`（fixtures 直下・DB 未登録）は一覧に載る。fixtures はテスト経路で worker の成功同期を
    通っていないため `document_count`/`last_updated` は未確定＝null（正しい「不明」表示——
    確定値の取得は `test_capabilities_document_count_reflects_confirmed_sync` 側で検証）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"caps-{sfx}")
    _logout()

    r = _capabilities(api_key=issued["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {w["id"] for w in body["worlds"]}
    assert "v1" in ids
    assert set(body["features"]) == {
        "convert", "search:keyword", "search:vector", "search:graph", "doc", "research"}


def test_capabilities_document_count_reflects_confirmed_sync(monkeypatch):
    """取り込みが成功確定済み（`last_sig` が非空）の world は `document_count`/`last_updated` が
    実値で返る（`worlds.last_doc_count`・`worker._run_locked` の成功パスで書き込まれる事前集計値・
    ここではホットパス走査をしない）。"""
    if not _try_init():
        pytest.skip("DB down")
    from datetime import datetime, timezone
    from pathlib import Path

    from sherpa import store as store_mod

    fake_row = {"world_id": "confirmed-world", "root_path": str(Path("fixtures/corpus/v1").resolve()),
               "last_synced_at": datetime.now(timezone.utc), "last_sig": "abc123",
               "last_doc_count": 42}
    monkeypatch.setattr(store_mod, "list_worlds_db", lambda: [fake_row])

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"capsconfirmed-{sfx}")
    _logout()

    r = _capabilities(api_key=issued["key"])
    assert r.status_code == 200, r.text
    world = next(w for w in r.json()["worlds"] if w["id"] == "confirmed-world")
    assert world["document_count"] == 42
    assert world["last_updated"] is not None


def test_capabilities_does_not_fabricate_v1_when_nothing_exists(monkeypatch):
    """world が1件も無い場合、`list_worlds()` の UI 向け `["v1"] フォールバックは使わない
    （`store.list_worlds_db()`＋`discover_fs_world_ids_strict()` を使う＝実在しない world を
    実在するように返さない）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import store as store_mod, worlds

    monkeypatch.setattr(store_mod, "list_worlds_db", lambda: [])
    monkeypatch.setattr(worlds, "discover_fs_world_ids_strict", lambda: [])

    def _must_not_be_called():
        raise AssertionError("list_worlds()（UI 向け [\"v1\"] フォールバック付き）を使ってはいけない")

    monkeypatch.setattr(worlds, "list_worlds", _must_not_be_called)
    monkeypatch.setattr(worlds, "discover_world_ids", _must_not_be_called)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"nofakev1-{sfx}")
    _logout()

    r = _capabilities(api_key=issued["key"])
    assert r.status_code == 200, r.text
    assert r.json()["worlds"] == []


def test_capabilities_last_updated_null_when_not_confirmed(tmp_path, monkeypatch):
    """取り込み開始時の pre-invalidate 書き込み（`last_sig=""`）のままの world は、
    `last_synced_at` があっても `last_updated=null`・`document_count=null`
    （進行中/未確定を成功確定と誤表示しない・`worlds.last_doc_count` も同じゲートで隠す）。"""
    if not _try_init():
        pytest.skip("DB down")
    from datetime import datetime, timezone

    from sherpa import store as store_mod

    fake_row = {"world_id": "pending-world", "root_path": str(tmp_path),
               "last_synced_at": datetime.now(timezone.utc), "last_sig": "", "last_doc_count": 7}
    monkeypatch.setattr(store_mod, "list_worlds_db", lambda: [fake_row])

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"unconfirmed-{sfx}")
    _logout()

    r = _capabilities(api_key=issued["key"])
    assert r.status_code == 200, r.text
    world = next(w for w in r.json()["worlds"] if w["id"] == "pending-world")
    assert world["last_updated"] is None
    assert world["document_count"] is None


def test_capabilities_registry_snapshot_error_returns_503(monkeypatch):
    """`store.list_worlds_db()`（唯一の registry スナップショット取得点）が例外を送出した場合は
    503（未捕捉例外による 500 にしない）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import store as store_mod

    def _boom():
        raise RuntimeError("simulated db outage")

    monkeypatch.setattr(store_mod, "list_worlds_db", _boom)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"caps503-{sfx}")
    _logout()

    r = _capabilities(api_key=issued["key"])
    assert r.status_code == 503, r.text


# ===== GET /ext/v1/doc（原本取得）=====

def test_doc_requires_api_key():
    if not _try_init():
        pytest.skip("DB down")
    r = _doc("v1", "4期/01_標準/消費税法.md")
    assert r.status_code == 401, r.text


def test_doc_download_roundtrip():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"doc-{sfx}")
    _logout()

    r = _doc("v1", "4期/01_標準/消費税法.md", api_key=issued["key"])
    assert r.status_code == 200, r.text
    assert r.content
    from pathlib import Path
    original = Path("fixtures/corpus/v1/4期/01_標準/消費税法.md").read_bytes()
    assert r.content == original
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-store"
    # 非ASCIIファイル名は RFC 5987 形式（filename*=utf-8''<percent-encoded>）で返る。
    from urllib.parse import quote
    assert r.headers["content-disposition"] == f"attachment; filename*=utf-8''{quote('消費税法.md')}"
    assert r.headers["content-length"] == str(len(original))


def test_doc_unknown_path_404():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"docnf-{sfx}")
    _logout()

    r = _doc("v1", "no/such/file.md", api_key=issued["key"])
    assert r.status_code == 404, r.text


def test_doc_rejects_unsupported_doctype():
    """`semantic/l_extract.json` は doctype 対応種別外＝404（原本DLの対象外）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"docext-{sfx}")
    _logout()

    r = _doc("v1", "semantic/l_extract.json", api_key=issued["key"])
    assert r.status_code == 404, r.text


def test_doc_traversal_rejected():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"doctrav-{sfx}")
    _logout()

    r = _doc("v1", "../../../../etc/passwd.md", api_key=issued["key"])
    assert r.status_code == 404, r.text


def test_doc_unknown_world_404():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"docworld-{sfx}")
    _logout()

    r = _doc("no-such-world-xyz", "a.md", api_key=issued["key"])
    assert r.status_code == 404, r.text


def test_doc_size_limit(monkeypatch):
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import ext_api
    monkeypatch.setattr(ext_api, "_DOC_MAX_BYTES", 10)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"docsize-{sfx}")
    _logout()

    r = _doc("v1", "4期/01_標準/消費税法.md", api_key=issued["key"])
    assert r.status_code == 413, r.text


# ===== GET /ext/v1/doc: symlink 差し替え耐性・マジック検証（低レベル安全性） =====
#
# `worlds.world_dir` を任意の一時ディレクトリへ monkeypatch し、`safe_open` の実 O_NOFOLLOW walk を
# 実ファイルシステム上で検証する（tests/unit/test_ext2_evidence.py と同じ monkeypatch 流儀）。

def _mk_doc_key(sfx: str) -> str:
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"docfd-{sfx}")
    _logout()
    return issued["key"]


def _mock_external_world(monkeypatch, root):
    """`worlds.resolve_external_world`（ext_doc/ext_search が使う strict resolver）を任意の
    一時ディレクトリへ差し替える（低レベル安全性テスト用・registry/fixtures 解決を経由しない）。
    """
    from sherpa import worlds
    monkeypatch.setattr(worlds, "resolve_external_world",
                        lambda w, **kw: worlds.ExternalWorldResolution("ok", root))


def test_doc_rejects_symlink_in_path(tmp_path, monkeypatch):
    """world root 配下の中間要素が symlink だと、実ファイルが world 外にあっても 404
    （`safe_open.open_file_nofollow_walk` は中間ディレクトリも O_NOFOLLOW で辿る）。"""
    if not _try_init():
        pytest.skip("DB down")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("外部の内容", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "evil_link").symlink_to(outside, target_is_directory=True)
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "evil_link/secret.md", api_key=key)
    assert r.status_code == 404, r.text


def test_doc_rejects_symlink_file_itself(tmp_path, monkeypatch):
    """対象ファイル自体が symlink（world 外の実ファイルを指す）でも 404。"""
    if not _try_init():
        pytest.skip("DB down")

    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.md"
    target.write_text("外部の内容", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link.md").symlink_to(target)
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "link.md", api_key=key)
    assert r.status_code == 404, r.text


def test_doc_rejects_magic_mismatch(tmp_path, monkeypatch):
    """拡張子は `.pdf` だが実バイト列が PDF マジック（`%PDF-`）でない＝拡張子偽装は 415。"""
    if not _try_init():
        pytest.skip("DB down")

    root = tmp_path / "root"
    root.mkdir()
    (root / "fake.pdf").write_bytes(b"this is not a pdf file at all")
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "fake.pdf", api_key=key)
    assert r.status_code == 415, r.text


def test_doc_accepts_real_magic(tmp_path, monkeypatch):
    """マジックが一致する場合は正常に配信される（誤検知していないことの対照）。"""
    if not _try_init():
        pytest.skip("DB down")

    root = tmp_path / "root"
    root.mkdir()
    content = b"%PDF-1.4\n%dummy pdf content\n"
    (root / "real.pdf").write_bytes(content)
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "real.pdf", api_key=key)
    assert r.status_code == 200, r.text
    assert r.content == content
    assert r.headers["content-type"] == "application/pdf"


def test_doc_rejects_importance_control_file(tmp_path, monkeypatch):
    """`_重要度.txt`（文書の重要度設定ファイル自体）は原本DLの対象外＝404。

    `corpus_docs.status_document_doctype()` が `_重要度.txt` を None 扱いにする（§5 の除外契約）
    ため、`documents.resolve()` を経由しないこの経路（`ext_doc` は直接 `safe_open` で配信する）
    でも一貫して弾かれることを固定する。
    """
    if not _try_init():
        pytest.skip("DB down")

    root = tmp_path / "root"
    root.mkdir()
    (root / "_重要度.txt").write_text("*.md: 高\n", encoding="utf-8")
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "_重要度.txt", api_key=key)
    assert r.status_code == 404, r.text


def test_doc_rejects_ooxml_content_types_missing(tmp_path, monkeypatch):
    """docx を騙る空/不正な zip（`[Content_Types].xml` も main part も無い）は 415
    （cross-format 検証・空 ZIP のケースを兼ねる）。"""
    if not _try_init():
        pytest.skip("DB down")
    import zipfile as zf

    root = tmp_path / "root"
    root.mkdir()
    buf_path = root / "empty.docx"
    with zf.ZipFile(buf_path, "w"):
        pass   # 空の zip（PK\x05\x06 の EOCD のみ）＝有効な zip だが中身が無い
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "empty.docx", api_key=key)
    assert r.status_code == 415, r.text


def test_doc_rejects_cross_format_ooxml(tmp_path, monkeypatch):
    """実体は xlsx（`xl/workbook.xml` を持つ）だが `.docx` として要求＝main part 不一致で 415。"""
    if not _try_init():
        pytest.skip("DB down")
    import zipfile as zf

    root = tmp_path / "root"
    root.mkdir()
    p = root / "mislabeled.docx"
    with zf.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<workbook/>")   # xlsx の main part（docx のではない）
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "mislabeled.docx", api_key=key)
    assert r.status_code == 415, r.text


def test_doc_accepts_valid_ooxml(tmp_path, monkeypatch):
    """`[Content_Types].xml` と形式固有 main part を持つ正しい docx は 200（誤検知していない対照）。"""
    if not _try_init():
        pytest.skip("DB down")
    import zipfile as zf

    root = tmp_path / "root"
    root.mkdir()
    p = root / "real.docx"
    with zf.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<document/>")
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "real.docx", api_key=key)
    assert r.status_code == 200, r.text


def test_doc_rejects_cross_format_image(tmp_path, monkeypatch):
    """PNG の実バイト列を `.jpg` として要求＝拡張子別 signature 不一致で 415
    （旧実装は「画像系ならどれかの signature が一致すればOK」だったため素通りしていた）。"""
    if not _try_init():
        pytest.skip("DB down")

    root = tmp_path / "root"
    root.mkdir()
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    (root / "fake.jpg").write_bytes(png_bytes)
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "fake.jpg", api_key=key)
    assert r.status_code == 415, r.text


def test_doc_accepts_pre_ole2_legacy_xls(tmp_path, monkeypatch):
    """OLE2/CFB ではない .xls（pre-OLE2 の旧形式を模した任意バイト列）は拒否しない
    （doctype ゲート済みのため。CFB ヘッダ健全性チェックは OLE2 マジックがある場合のみ働く）。"""
    if not _try_init():
        pytest.skip("DB down")

    root = tmp_path / "root"
    root.mkdir()
    (root / "legacy.xls").write_bytes(b"\x09\x00\x04\x00not really biff but not ole2 either")
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "legacy.xls", api_key=key)
    assert r.status_code == 200, r.text


def _cfb_header_bytes(*, major: int = 3, sector_shift: int = 9) -> bytes:
    """妥当な [MS-CFB] ヘッダ（512バイト）を組み立てる。legacy Office（.doc/.xls/.ppt）の検証は
    ヘッダの署名・version・byte order・sector shift の健全性のみを見る（stream 列挙・形式判別は
    しない——配信元は登録済み world＝信頼済みコーパスであり、深い形式判別は脅威モデル過剰という
    裁定。パーサ単体の網羅的なテストは tests/unit/test_ext_api_cfb.py 側）。
    """
    import struct

    from sherpa import ext_api

    header = bytearray(512)
    header[0:8] = ext_api._OLE2_MAGIC
    struct.pack_into("<HH", header, 24, 0, major)
    struct.pack_into("<H", header, 28, 0xFFFE)
    struct.pack_into("<H", header, 30, sector_shift)
    return bytes(header)


def test_doc_accepts_valid_cfb_doc(tmp_path, monkeypatch):
    """妥当な CFB ヘッダを持つ実体は `.doc` として 200・Content-Type は application/octet-stream 固定
    （legacy Office は形式判別しない裁定・nosniff 済みのため MIME 混同の実害は無い）。"""
    if not _try_init():
        pytest.skip("DB down")

    data = _cfb_header_bytes() + b"\x00" * 512
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.doc").write_bytes(data)
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "real.doc", api_key=key)
    assert r.status_code == 200, r.text
    assert r.content == data
    assert r.headers["content-type"] == "application/octet-stream"


def test_doc_rejects_malformed_cfb_header(tmp_path, monkeypatch):
    """OLE2 マジックはあるが sector_shift が major version と不整合な壊れヘッダは 415。"""
    if not _try_init():
        pytest.skip("DB down")

    data = _cfb_header_bytes(major=3, sector_shift=12) + b"\x00" * 512   # v3 なのに v4 の shift
    root = tmp_path / "root"
    root.mkdir()
    (root / "fake.xls").write_bytes(data)
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "fake.xls", api_key=key)
    assert r.status_code == 415, r.text


def test_doc_charset_dropped_for_invalid_utf8(tmp_path, monkeypatch):
    """`.txt` の内容が有効な UTF-8 でなければ、Content-Type から charset=utf-8 を外す
    （実際のエンコーディングを確認せず utf-8 と偽らない）。"""
    if not _try_init():
        pytest.skip("DB down")

    root = tmp_path / "root"
    root.mkdir()
    (root / "sjis.txt").write_bytes("こんにちは".encode("shift_jis"))
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "sjis.txt", api_key=key)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "text/plain"   # charset 宣言なし


def test_doc_charset_kept_for_valid_utf8(tmp_path, monkeypatch):
    """`.txt` が実際に UTF-8 なら charset=utf-8 のまま（誤検知していないことの対照）。"""
    if not _try_init():
        pytest.skip("DB down")

    root = tmp_path / "root"
    root.mkdir()
    (root / "utf8.txt").write_text("こんにちは", encoding="utf-8")
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "utf8.txt", api_key=key)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "text/plain; charset=utf-8"


# ===== API キーの world スコープ =====

def test_scoped_key_allows_in_scope_world():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key_scoped(f"scoped-ok-{sfx}", ["v1"])
    _logout()

    r = _search({"world": "v1", "query": "税", "engines": ["keyword"]}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    r = _doc("v1", "4期/01_標準/消費税法.md", api_key=issued["key"])
    assert r.status_code == 200, r.text


def test_scoped_key_rejects_out_of_scope_world():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key_scoped(f"scoped-403-{sfx}", ["v1"])
    _logout()

    r = _search({"world": "other-world-xyz", "query": "x"}, api_key=issued["key"])
    assert r.status_code == 403, r.text
    r = _doc("other-world-xyz", "a.md", api_key=issued["key"])
    assert r.status_code == 403, r.text


def test_scoped_key_capabilities_filters_worlds():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key_scoped(f"scoped-caps-{sfx}", ["v1"])
    _logout()

    r = _capabilities(api_key=issued["key"])
    assert r.status_code == 200, r.text
    ids = {w["id"] for w in r.json()["worlds"]}
    assert ids == {"v1"}


def test_unscoped_key_allows_any_world():
    """既存キー（allowed_worlds 未指定=null）は従来どおり全 world にアクセスできる（後方互換）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"unscoped-{sfx}")
    _logout()
    assert issued["allowed_worlds"] is None

    r = _search({"world": "v1", "query": "税", "engines": ["keyword"]}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    r = _search({"world": "some-other-world-abc", "query": "x"}, api_key=issued["key"])
    assert r.status_code == 404, r.text   # スコープではなく世界不在の 404（後方互換）


def test_key_create_rejects_invalid_world_identifier():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    r = client.post("/ext/v1/admin/keys",
                    json={"label": f"badworld-{sfx}", "allowed_worlds": ["not a valid id!"]})
    assert r.status_code == 422, r.text
    _logout()


def test_key_list_includes_allowed_worlds():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key_scoped(f"listscope-{sfx}", ["v1"])
    r = client.get("/ext/v1/admin/keys")
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["keys"] if x["id"] == issued["id"])
    assert row["allowed_worlds"] == ["v1"]
    _logout()


def test_key_create_rejects_unknown_world():
    """形式は正しいが実在しない world は 422（`_known_world_ids_or_503` の実在検証）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    r = client.post("/ext/v1/admin/keys",
                    json={"label": f"unknownworld-{sfx}", "allowed_worlds": ["nonexistent-world-zzz"]})
    assert r.status_code == 422, r.text
    _logout()


# ===== expires_at・daily_quota・利用者自己発行 =====

def test_key_create_with_expires_at_and_daily_quota_round_trip():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    r = client.post("/ext/v1/admin/keys",
                    json={"label": f"exq-{sfx}", "expires_at": "2099-01-01T00:00:00+00:00",
                          "daily_quota": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["expires_at"] is not None
    assert body["daily_quota"] == 5

    listed = client.get("/ext/v1/admin/keys").json()["keys"]
    row = next(x for x in listed if x["id"] == body["id"])
    assert row["expires_at"] is not None
    assert row["daily_quota"] == 5
    assert row["call_count"] == 0
    _logout()


def test_key_create_rejects_invalid_daily_quota():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    r = client.post("/ext/v1/admin/keys", json={"label": f"badq-{sfx}", "daily_quota": 0})
    assert r.status_code == 422, r.text
    _logout()


def test_key_create_daily_quota_rejects_bool_string_and_over_limit():
    """`daily_quota` は StrictInt: bool・数字文字列は型検証で 422（暗黙変換しない）。
    1,000,000 を超える値も 422（DB の CHECK 制約と同じ上限）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    try:
        r = client.post("/ext/v1/admin/keys", json={"label": f"boolq-{sfx}", "daily_quota": True})
        assert r.status_code == 422, r.text
        r = client.post("/ext/v1/admin/keys", json={"label": f"strq-{sfx}", "daily_quota": "10"})
        assert r.status_code == 422, r.text
        r = client.post("/ext/v1/admin/keys", json={"label": f"overq-{sfx}", "daily_quota": 1_000_001})
        assert r.status_code == 422, r.text
        # 上限ちょうどは許可される。
        r = client.post("/ext/v1/admin/keys", json={"label": f"maxq-{sfx}", "daily_quota": 1_000_000})
        assert r.status_code == 200, r.text
        assert r.json()["daily_quota"] == 1_000_000
    finally:
        _logout()


def test_self_key_create_daily_quota_rejects_bool_string_and_over_limit():
    """自己発行側でも `daily_quota` は StrictInt（管理者発行側と同じ境界カバレッジ）。
    管理者の許可上限自体を1,000,000へ引き上げた上で、型検証（bool/文字列）と
    _DAILY_QUOTA_MAX 超過がいずれも422になることを確認する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    assert client.put("/admin/settings", json={"user_api_keys_allowed": True,
                                                "user_api_keys_daily_quota_default": 1_000_000}
                     ).status_code == 200
    _logout()
    try:
        uid, pw = _mk_user(sfx)
        _login(uid, pw)
        r = client.post("/ext/v1/keys", json={"label": f"selfboolq-{sfx}", "daily_quota": True})
        assert r.status_code == 422, r.text
        r = client.post("/ext/v1/keys", json={"label": f"selfstrq-{sfx}", "daily_quota": "10"})
        assert r.status_code == 422, r.text
        r = client.post("/ext/v1/keys", json={"label": f"selfoverq-{sfx}", "daily_quota": 1_000_001})
        assert r.status_code == 422, r.text
        # 上限ちょうど（かつ管理者の許可上限内）は許可される。
        r = client.post("/ext/v1/keys", json={"label": f"selfmaxq-{sfx}", "daily_quota": 1_000_000})
        assert r.status_code == 200, r.text
        assert r.json()["daily_quota"] == 1_000_000
        _logout()
    finally:
        _login(adm_uid, adm_pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None,
                                            "user_api_keys_daily_quota_default": None})
        _logout()


def test_key_create_rejects_past_expiry():
    """管理者発行・利用者自己発行のいずれも、過去日時の有効期限は422（発行直後から使えない
    キーを作らせない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    try:
        r = client.post("/ext/v1/admin/keys",
                        json={"label": f"pastexp-{sfx}", "expires_at": "2000-01-01T00:00:00+00:00"})
        assert r.status_code == 422, r.text

        assert client.put("/admin/settings", json={"user_api_keys_allowed": True}).status_code == 200
    finally:
        _logout()
    try:
        uid, pw = _mk_user(sfx)
        _login(uid, pw)
        r = client.post("/ext/v1/keys",
                        json={"label": f"pastexpself-{sfx}", "expires_at": "2000-01-01T00:00:00+00:00"})
        assert r.status_code == 422, r.text
        _logout()
    finally:
        _login(adm_uid, adm_pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None})
        _logout()


def test_key_create_client_op_id_round_trips_to_list_and_create_response():
    """`client_op_id`（発行 UI の相関トークン・UUID 形式）は発行応答・一覧の両方に反映される
    （POST 応答が失われた場合の照合用・秘密ではない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    op_id = str(uuid.uuid4())
    r = client.post("/ext/v1/admin/keys", json={"label": f"opid-{sfx}", "client_op_id": op_id})
    assert r.status_code == 200, r.text
    assert r.json()["client_op_id"] == op_id
    listed = client.get("/ext/v1/admin/keys").json()["keys"]
    row = next(x for x in listed if x["id"] == r.json()["id"])
    assert row["client_op_id"] == op_id
    _logout()


def test_self_key_create_client_op_id_round_trips_to_list_and_create_response():
    """自己発行側でも `client_op_id` が発行応答・本人一覧の両方に反映される
    （管理者発行側と同じ境界カバレッジ）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    assert client.put("/admin/settings", json={"user_api_keys_allowed": True}).status_code == 200
    _logout()
    try:
        uid, pw = _mk_user(sfx)
        _login(uid, pw)
        op_id = str(uuid.uuid4())
        r = client.post("/ext/v1/keys", json={"label": f"selfopid-{sfx}", "client_op_id": op_id})
        assert r.status_code == 200, r.text
        assert r.json()["client_op_id"] == op_id
        listed = client.get("/ext/v1/keys").json()["keys"]
        row = next(x for x in listed if x["id"] == r.json()["id"])
        assert row["client_op_id"] == op_id
        _logout()
    finally:
        _login(adm_uid, adm_pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None})
        _logout()


def test_key_create_client_op_id_rejects_non_uuid_format():
    """`client_op_id` は UUID 形式（8-4-4-4-12）以外は422（管理者発行・自己発行の両方）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    try:
        for bad in ("not-a-uuid", f"op-{sfx}-abc123", "12345678-1234-1234-1234", ""):
            r = client.post("/ext/v1/admin/keys",
                            json={"label": f"badcop-{sfx}", "client_op_id": bad})
            assert r.status_code == 422, (bad, r.text)

        assert client.put("/admin/settings", json={"user_api_keys_allowed": True}).status_code == 200
    finally:
        _logout()
    try:
        uid, pw = _mk_user(sfx)
        _login(uid, pw)
        r = client.post("/ext/v1/keys", json={"label": f"selfbadcop-{sfx}", "client_op_id": "xyz"})
        assert r.status_code == 422, r.text
        _logout()
    finally:
        _login(adm_uid, adm_pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None})
        _logout()


def test_key_create_client_op_id_conflict_returns_409():
    """同じ `client_op_id` で2回発行しようとすると2回目は409（一意制約違反の変換）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    op_id = str(uuid.uuid4())
    r1 = client.post("/ext/v1/admin/keys", json={"label": f"dup1-{sfx}", "client_op_id": op_id})
    assert r1.status_code == 200, r1.text
    r2 = client.post("/ext/v1/admin/keys", json={"label": f"dup2-{sfx}", "client_op_id": op_id})
    assert r2.status_code == 409, r2.text
    _logout()


def test_key_create_client_op_id_uppercase_input_normalizes_and_second_attempt_conflicts():
    """`client_op_id` を大文字で送っても、発行応答は正準小文字形に正規化される。同じ UUID を
    別の大小文字表記で2回目に送っても409（UUID互換の回帰契約・大小文字迂回を防ぐ）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    op_id_lower = str(uuid.uuid4())
    r1 = client.post("/ext/v1/admin/keys",
                     json={"label": f"upnorm-{sfx}", "client_op_id": op_id_lower.upper()})
    assert r1.status_code == 200, r1.text
    assert r1.json()["client_op_id"] == op_id_lower   # 応答は常に正準小文字形。

    r2 = client.post("/ext/v1/admin/keys",
                     json={"label": f"upnorm2-{sfx}", "client_op_id": op_id_lower})
    assert r2.status_code == 409, r2.text
    _logout()


def test_key_recover_matches_directly_seeded_uppercase_legacy_row_via_lowercase_query():
    """アプリの正規化を経由しない直接 SQL で大文字のまま保存された旧行（移行前のデータを
    模す）でも、回復 API へ小文字で照会すれば一致する（`lower()` 照合の回帰契約）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    op_id_upper = str(uuid.uuid4()).upper()
    with store._connect() as c:
        key_id = c.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, label, created_by, client_op_id) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (f"hash-legacyup-{sfx}", f"pfxlgu{sfx}"[:12], "legacy", adm_uid, op_id_upper),
        ).fetchone()["id"]

    rec = client.post("/ext/v1/admin/keys/recover", json={"client_op_id": op_id_upper.lower()})
    assert rec.status_code == 200, rec.text
    assert rec.json()["found"] is True
    assert rec.json()["id"] == key_id
    _logout()


def test_key_recover_scoped_to_self_actor_does_not_reach_other_owners_key():
    """回復専用エンドポイントは認証主体自身が発行操作した行にしか一致しない。B が A の
    `client_op_id` で回復を試みても A のキーは無傷（別所有者衝突の反転テスト・API 経由）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    assert client.put("/admin/settings", json={"user_api_keys_allowed": True}).status_code == 200
    _logout()
    op_id = str(uuid.uuid4())
    try:
        uid_a, pw_a = _mk_user(f"{sfx}a")
        _login(uid_a, pw_a)
        created = client.post("/ext/v1/keys", json={"label": f"recA-{sfx}", "client_op_id": op_id})
        assert created.status_code == 200, created.text
        key_id = created.json()["id"]
        _logout()

        uid_b, pw_b = _mk_user(f"{sfx}b")
        _login(uid_b, pw_b)
        # B が A の client_op_id で回復を試みても、見つからない（found: false）。
        rec_b = client.post("/ext/v1/keys/recover", json={"client_op_id": op_id})
        assert rec_b.status_code == 200, rec_b.text
        assert rec_b.json()["found"] is False
        _logout()

        _login(uid_a, pw_a)
        listed = client.get("/ext/v1/keys").json()["keys"]
        row = next(x for x in listed if x["id"] == key_id)
        assert row["revoked_at"] is None   # A のキーは無傷のまま
        # A 自身の回復は正しく効く。
        rec_a = client.post("/ext/v1/keys/recover", json={"client_op_id": op_id})
        assert rec_a.status_code == 200, rec_a.text
        assert rec_a.json()["found"] is True
        assert rec_a.json()["id"] == key_id
        _logout()
    finally:
        _login(adm_uid, adm_pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None})
        _logout()


def test_key_recover_admin_self_success_and_different_admin_denied():
    """admin 自身の回復は成功し、別の admin が同じ `client_op_id` で回復を試みても見つからない
    （admin 側の反転テスト・`created_by` で厳密に絞り込む）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm1_uid, adm1_pw = _mk_admin(f"{sfx}1")
    adm2_uid, adm2_pw = _mk_admin(f"{sfx}2")
    op_id = str(uuid.uuid4())

    _login(adm1_uid, adm1_pw)
    created = client.post("/ext/v1/admin/keys", json={"label": f"rec1-{sfx}", "client_op_id": op_id})
    assert created.status_code == 200, created.text
    key_id = created.json()["id"]
    _logout()

    _login(adm2_uid, adm2_pw)
    rec_other = client.post("/ext/v1/admin/keys/recover", json={"client_op_id": op_id})
    assert rec_other.status_code == 200, rec_other.text
    assert rec_other.json()["found"] is False
    _logout()

    _login(adm1_uid, adm1_pw)
    listed = client.get("/ext/v1/admin/keys").json()["keys"]
    row = next(x for x in listed if x["id"] == key_id)
    assert row["revoked_at"] is None   # 別 admin の回復では無傷のまま

    rec_self = client.post("/ext/v1/admin/keys/recover", json={"client_op_id": op_id})
    assert rec_self.status_code == 200, rec_self.text
    assert rec_self.json()["found"] is True
    assert rec_self.json()["id"] == key_id
    _logout()


def test_key_recover_admin_and_self_rows_do_not_cross_match():
    """admin 発行の回復エンドポイントは自己発行キー（`owner_uid` 非NULL）に一致せず、
    自己発行の回復エンドポイントも admin 発行キー（`owner_uid IS NULL`）に一致しない
    （`client_op_id` はグローバルに一意のため同じ値は使えない——別々の値で、行の種別
    そのものが交差しないことを確認する）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    assert client.put("/admin/settings", json={"user_api_keys_allowed": True}).status_code == 200
    admin_op_id = str(uuid.uuid4())
    created_admin = client.post("/ext/v1/admin/keys",
                                json={"label": f"crossA-{sfx}", "client_op_id": admin_op_id})
    assert created_admin.status_code == 200, created_admin.text
    _logout()
    try:
        uid, pw = _mk_user(sfx)
        _login(uid, pw)
        self_op_id = str(uuid.uuid4())
        created_self = client.post("/ext/v1/keys",
                                   json={"label": f"crossB-{sfx}", "client_op_id": self_op_id})
        assert created_self.status_code == 200, created_self.text

        # 自己発行の回復エンドポイントで admin 発行キーの client_op_id を渡しても一致しない。
        rec_self_for_admin_row = client.post("/ext/v1/keys/recover",
                                             json={"client_op_id": admin_op_id})
        assert rec_self_for_admin_row.status_code == 200, rec_self_for_admin_row.text
        assert rec_self_for_admin_row.json()["found"] is False
        _logout()

        _login(adm_uid, adm_pw)
        # admin 発行の回復エンドポイントで自己発行キーの client_op_id を渡しても一致しない。
        rec_admin_for_self_row = client.post("/ext/v1/admin/keys/recover",
                                             json={"client_op_id": self_op_id})
        assert rec_admin_for_self_row.status_code == 200, rec_admin_for_self_row.text
        assert rec_admin_for_self_row.json()["found"] is False

        # どちらの行も無傷のまま。
        admin_listed = client.get("/ext/v1/admin/keys").json()["keys"]
        admin_row = next(x for x in admin_listed if x["id"] == created_admin.json()["id"])
        assert admin_row["revoked_at"] is None
        self_row = next(x for x in admin_listed if x["id"] == created_self.json()["id"])
        assert self_row["revoked_at"] is None
        _logout()
    finally:
        _login(adm_uid, adm_pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None})
        _logout()


def test_self_key_create_client_op_id_conflict_returns_409():
    """自己発行側でも、同じ `client_op_id` で2回目の発行を試みると409（管理者発行側と同型）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    assert client.put("/admin/settings", json={"user_api_keys_allowed": True}).status_code == 200
    _logout()
    try:
        uid, pw = _mk_user(sfx)
        _login(uid, pw)
        op_id = str(uuid.uuid4())
        r1 = client.post("/ext/v1/keys", json={"label": f"selfdup1-{sfx}", "client_op_id": op_id})
        assert r1.status_code == 200, r1.text
        r2 = client.post("/ext/v1/keys", json={"label": f"selfdup2-{sfx}", "client_op_id": op_id})
        assert r2.status_code == 409, r2.text
        _logout()
    finally:
        _login(adm_uid, adm_pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None})
        _logout()


def test_self_key_recover_forbidden_when_disabled():
    """`user_api_keys_allowed` が OFF のときは自己発行の回復エンドポイントも403
    （create/list/revoke と同じ4ルート共通のゲートに、回復も揃っている）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)
    r = client.post("/ext/v1/keys/recover", json={"client_op_id": str(uuid.uuid4())})
    assert r.status_code == 403, r.text
    _logout()


def test_key_recover_creates_audit_row():
    """回復の試行は監査ログに残る（`ext_api.key_recover_attempted`・成功時は resource_id が
    失効したキーの id）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    op_id = str(uuid.uuid4())
    created = client.post("/ext/v1/admin/keys", json={"label": f"audrec-{sfx}", "client_op_id": op_id})
    assert created.status_code == 200, created.text
    key_id = created.json()["id"]

    rec = client.post("/ext/v1/admin/keys/recover", json={"client_op_id": op_id})
    assert rec.status_code == 200, rec.text
    assert rec.json()["found"] is True
    _logout()

    rows = store.list_audit(actor=adm_uid, action="ext_api.key_recover_attempted", limit=10)
    matching = [r for r in rows if r["resource_id"] == str(key_id)]
    assert matching, f"回復の監査行が見つからない: {rows}"


def test_key_create_client_op_id_conflict_after_revoke_still_409():
    """失効済みキーの `client_op_id` でも一意制約は有効（`revoked_at` の有無に関わらず一意・
    失効済みキーの操作トークンを使い回して2重発行できてしまわないようにする）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    op_id = str(uuid.uuid4())
    r1 = client.post("/ext/v1/admin/keys", json={"label": f"revdup1-{sfx}", "client_op_id": op_id})
    assert r1.status_code == 200, r1.text
    key_id = r1.json()["id"]

    revoked = client.delete(f"/ext/v1/admin/keys/{key_id}")
    assert revoked.status_code == 200, revoked.text

    r2 = client.post("/ext/v1/admin/keys", json={"label": f"revdup2-{sfx}", "client_op_id": op_id})
    assert r2.status_code == 409, r2.text
    _logout()


def test_check_constraint_rejects_out_of_range_daily_quota_at_db_level():
    """DB の CHECK 制約（`api_keys_daily_quota_range`）は、アプリ層のバリデーションを迂回した
    直接 SQL でも範囲外の `daily_quota` を拒否する（最後の砦）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    with store._connect() as c:
        with pytest.raises(Exception) as exc_info:
            c.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, label, created_by, daily_quota) "
                "VALUES (%s,%s,%s,%s,%s)",
                (f"hash-chk-{sfx}", f"pfxchk{sfx}"[:12], f"chk-{sfx}", "admin", 2_000_000))
    assert "api_keys_daily_quota_range" in str(exc_info.value) or "check constraint" in str(exc_info.value).lower()


def test_self_key_create_forbidden_when_disabled():
    """既定 OFF（user_api_keys_allowed 未設定）では利用者は自己発行できない。
    一覧・失効・回復も同様に拒否する（4ルート共通のゲート）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)
    try:
        r = client.post("/ext/v1/keys", json={"label": f"self-{sfx}"})
        assert r.status_code == 403, r.text
        r = client.get("/ext/v1/keys")
        assert r.status_code == 403, r.text
        r = client.delete("/ext/v1/keys/999999999")
        assert r.status_code == 403, r.text
        r = client.post("/ext/v1/keys/recover", json={"client_op_id": str(uuid.uuid4())})
        assert r.status_code == 403, r.text
    finally:
        _logout()


def test_self_key_create_list_revoke_when_enabled():
    """許可時は自己発行→一覧→本人失効ができ、他人からは見えない（IDOR無し）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    r = client.put("/admin/settings", json={"user_api_keys_allowed": True})
    assert r.status_code == 200, r.text
    _logout()
    try:
        uid, pw = _mk_user(sfx)
        other_uid, other_pw = _mk_user(f"o{sfx}")

        _login(uid, pw)
        r = client.post("/ext/v1/keys", json={"label": f"self-{sfx}"})
        assert r.status_code == 200, r.text
        created = r.json()
        assert created["key"].startswith("sk-ext-")

        listed = client.get("/ext/v1/keys").json()["keys"]
        assert {row["id"] for row in listed} == {created["id"]}
        assert listed[0]["allowed_worlds"] is None   # 現状「全員全 world」＝⊆ の結果は全許可のまま
        _logout()

        # 他人には見えない・失効もできない（404＝所有権の有無を外に出さない）。
        _login(other_uid, other_pw)
        assert client.get("/ext/v1/keys").json()["keys"] == []
        r = client.delete(f"/ext/v1/keys/{created['id']}")
        assert r.status_code == 404, r.text
        _logout()

        # 本人は失効できる。
        _login(uid, pw)
        r = client.delete(f"/ext/v1/keys/{created['id']}")
        assert r.status_code == 200, r.text
        assert r.json()["revoked_at"] is not None
        _logout()

        # admin は全キー（利用者発行分も含む）を見え、失効もできる。
        _login(adm_uid, adm_pw)
        admin_listed = client.get("/ext/v1/admin/keys").json()["keys"]
        admin_row = next(x for x in admin_listed if x["id"] == created["id"])
        assert admin_row["owner_uid"] == uid
        _logout()
    finally:
        _login(adm_uid, adm_pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None})
        _logout()


def test_self_key_create_daily_quota_is_admin_controlled():
    """自己発行キーの daily_quota は管理者統制: 未指定は既定を適用し（空欄=無制限を許さない）、
    上限を超える指定は422。管理者が既定/上限を明示設定していれば、その値が適用される。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    r = client.put("/admin/settings", json={"user_api_keys_allowed": True,
                                            "user_api_keys_daily_quota_default": 5})
    assert r.status_code == 200, r.text
    assert r.json()["ext_keys"]["daily_quota_default"]["effective"] == 5
    _logout()
    try:
        uid, pw = _mk_user(sfx)
        _login(uid, pw)
        # 未指定＝管理者の既定（5）が適用される（空欄で無制限にはならない）。
        r = client.post("/ext/v1/keys", json={"label": f"quotadef-{sfx}"})
        assert r.status_code == 200, r.text
        assert r.json()["daily_quota"] == 5

        # 上限（5）以下の指定は許可される。
        r = client.post("/ext/v1/keys", json={"label": f"quotaok-{sfx}", "daily_quota": 3})
        assert r.status_code == 200, r.text
        assert r.json()["daily_quota"] == 3

        # 上限超過は422（管理者の許可なく無制限/大容量キーを作れない）。
        r = client.post("/ext/v1/keys", json={"label": f"quotaover-{sfx}", "daily_quota": 6})
        assert r.status_code == 422, r.text
        _logout()
    finally:
        _login(adm_uid, adm_pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None,
                                            "user_api_keys_daily_quota_default": None})
        _logout()


def test_self_key_create_daily_quota_fallback_default_when_admin_unset():
    """管理者が既定/上限を一度も設定していなくても、組み込みのフォールバック既定が適用される
    （self-issued キーが常にクォータを持つことの保証）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    assert client.put("/admin/settings", json={"user_api_keys_allowed": True}).status_code == 200
    _logout()
    try:
        uid, pw = _mk_user(sfx)
        _login(uid, pw)
        r = client.post("/ext/v1/keys", json={"label": f"quotafallback-{sfx}"})
        assert r.status_code == 200, r.text
        assert r.json()["daily_quota"] == store.SELF_ISSUED_DAILY_QUOTA_DEFAULT_FALLBACK
        _logout()
    finally:
        _login(adm_uid, adm_pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None})
        _logout()


def test_self_issued_key_audit_detail_has_owner_uid_on_success_401_429_and_fallback():
    """自己発行キーの `detail.owner_uid` が、行を特定できる全ての監査経路
    （成功・401・429・フォールバック）で付与される（actor は `ext:{key_id}` のまま）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    assert client.put("/admin/settings", json={"user_api_keys_allowed": True}).status_code == 200
    _logout()
    uid, pw = _mk_user(sfx)
    try:
        _login(uid, pw)
        r = client.post("/ext/v1/keys", json={"label": f"ownaudit-{sfx}", "daily_quota": 1})
        assert r.status_code == 200, r.text
        key, key_id = r.json()["key"], r.json()["id"]
        _logout()

        # 成功（200）。
        r = _convert("a.docx", _make_docx_bytes(), api_key=key)
        assert r.status_code == 200, r.text

        # 429（daily_quota=1 を使い切っているので2回目は拒否）。
        r = _convert("a.docx", _make_docx_bytes(), api_key=key)
        assert r.status_code == 429, r.text

        # 401（所有者を無効化）。
        store.upsert_user(uid, role="user", status="disabled")
        r = _convert("a.docx", _make_docx_bytes(), api_key=key)
        assert r.status_code == 401, r.text
        store.upsert_user(uid, role="user", status="active")

        # フォールバック（malformed body・require_api_key 自体が実行されない終了経路）。
        rid_fb = f"probe-ownaudit-fallback-{sfx}"
        r = client.post("/ext/v1/search", content=b"{not valid json",
                        headers={"X-API-Key": key, "Content-Type": "application/json",
                                "X-Request-Id": rid_fb})
        assert r.status_code == 422, r.text

        with store._connect() as c:
            rows = c.execute(
                "SELECT outcome, reason, detail FROM audit_log WHERE actor_user_id=%s "
                "ORDER BY id ASC", (f"ext:{key_id}",)
            ).fetchall()

        success_row = next((r for r in rows if r["outcome"] == "success"), None)
        assert success_row is not None, f"成功行が見つからない: {rows}"
        assert success_row["detail"].get("owner_uid") == uid

        quota_row = next((r for r in rows if r["reason"] == "daily_quota_exceeded"), None)
        assert quota_row is not None, f"429（daily_quota_exceeded）行が見つからない: {rows}"
        assert quota_row["detail"].get("owner_uid") == uid

        inactive_row = next((r for r in rows if r["reason"] == "owner_inactive"), None)
        assert inactive_row is not None, f"401（owner_inactive）行が見つからない: {rows}"
        assert inactive_row["detail"].get("owner_uid") == uid

        fallback_row = next((r for r in rows if r["reason"] == "request_incomplete"), None)
        assert fallback_row is not None, f"フォールバック行（request_incomplete）が見つからない: {rows}"
        assert fallback_row["detail"].get("owner_uid") == uid
    finally:
        store.upsert_user(uid, role="user", status="active")
        _login(adm_uid, adm_pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None})
        _logout()


def test_self_issued_key_owner_disabled_returns_401():
    """自己発行キーの所有者が無効化（disabled）されたら、キー自体は失効していなくても
    以後の呼び出しは401になる（Cookie セッションのアカウント停止確認と同じ扱い・IDOR/権限
    迂回の防止）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    assert client.put("/admin/settings", json={"user_api_keys_allowed": True}).status_code == 200
    _logout()
    try:
        uid, pw = _mk_user(sfx)
        _login(uid, pw)
        r = client.post("/ext/v1/keys", json={"label": f"ownerdis-{sfx}"})
        assert r.status_code == 200, r.text
        key = r.json()["key"]
        _logout()

        r = _convert("a.docx", _make_docx_bytes(), api_key=key)
        assert r.status_code == 200, r.text

        store.upsert_user(uid, role="user", status="disabled")
        r = _convert("a.docx", _make_docx_bytes(), api_key=key)
        assert r.status_code == 401, r.text
    finally:
        store.upsert_user(uid, role="user", status="active")
        _login(adm_uid, adm_pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None})
        _logout()


def test_self_key_create_disallowed_after_toggle_off_revokes_existing():
    """OFF に戻すと利用者発行キーは一括失効し、以後は認証時にも fail-safe で締め出される
    （A6 と同型・PUT /admin/settings の一括失効＋`_verify_key_sync` の二重チェック）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    assert client.put("/admin/settings", json={"user_api_keys_allowed": True}).status_code == 200
    _logout()

    uid, pw = _mk_user(sfx)
    _login(uid, pw)
    r = client.post("/ext/v1/keys", json={"label": f"toggle-{sfx}"})
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    _logout()

    r = _convert("a.docx", _make_docx_bytes(), api_key=key)
    assert r.status_code == 200, r.text

    _login(adm_uid, adm_pw)
    r = client.put("/admin/settings", json={"user_api_keys_allowed": False})
    assert r.status_code == 200, r.text
    assert r.json()["ext_keys"]["user_api_keys_allowed"] is False
    _logout()

    # 一括失効された鍵は以後 401（revoked と同じ扱い）。
    r = _convert("a.docx", _make_docx_bytes(), api_key=key)
    assert r.status_code == 401, r.text


def test_key_create_failure_audits_requested_label_and_allowed_worlds():
    """キー発行が 422（未知の world）で失敗しても、監査行には要求された `label`/`allowed_worlds`
    （検証前の入力）が残る——検証成功後にしか `pending["detail"]` へ積まないと、失敗した
    発行の監査行は入力が空のままになる。status/outcome/reason の三項目も併せて固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    rid = f"probe-keycreate-fail-{sfx}"
    label = f"failaudit-{sfx}"
    r = client.post(
        "/ext/v1/admin/keys",
        json={"label": label, "allowed_worlds": ["nonexistent-world-zzz"]},
        headers={"X-Request-Id": rid})
    assert r.status_code == 422, r.text
    _logout()

    with store._connect() as c:
        rows = c.execute(
            "SELECT outcome, reason, detail FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchall()
    assert len(rows) == 1, f"監査行はちょうど1件のはず（実際 {len(rows)} 件）"
    row = rows[0]
    detail = row["detail"]
    assert detail["label"] == label
    assert detail["allowed_worlds"] == ["nonexistent-world-zzz"]
    assert detail["http_status"] == 422
    assert row["outcome"] == "error"
    assert row["reason"] == "validation_error"   # `_HTTP_OUTCOME_REASON[422]`


def test_key_create_unhandled_exception_audits_status_outcome_reason(monkeypatch):
    """キー発行が（実在する world で検証を通過した後の）未処理例外で 500 になっても、
    `ExtRequestMiddleware` が自前で 500 応答を組み立てて監査する（`test_unhandled_exception_
    gets_request_id_and_is_audited` の search 版と対になる key-issuance 版）。500 は
    `_HTTP_OUTCOME_REASON` に無いため reason は既定の "error" になることを固定する。"""
    if not _try_init():
        pytest.skip("DB down")

    def _boom(*a, **kw):
        raise RuntimeError("simulated db outage during key insert")

    monkeypatch.setattr(store, "insert_api_key", _boom)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    rid = f"probe-keycreate-500-{sfx}"
    r = client.post(
        "/ext/v1/admin/keys",
        json={"label": f"boom500-{sfx}", "allowed_worlds": ["v1"]},
        headers={"X-Request-Id": rid})
    assert r.status_code == 500, r.text
    assert r.headers["X-Request-Id"] == rid
    _logout()

    with store._connect() as c:
        rows = c.execute(
            "SELECT outcome, reason, detail FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchall()
    assert len(rows) == 1, f"監査行はちょうど1件のはず（実際 {len(rows)} 件）"
    row = rows[0]
    assert row["detail"]["http_status"] == 500
    assert row["outcome"] == "error"
    assert row["reason"] == "error"   # `_HTTP_OUTCOME_REASON` に 500 は無い＝既定値


def test_key_create_malformed_json_body_returns_clean_422_without_stray_audit_row():
    """`/ext/v1/admin/keys`（Cookie 認証の admin ルート）へ malformed JSON を送ると、body parse
    自体が `start_audit()` 実行前に失敗するため `request.state.audit_pending` は一度も作られない。
    `_fallback_audit_pending()` は5つの X-API-Key ルート限定（`_ACTION_BY_PATH`）で admin 系は
    意図的にスコープ外（docstring 明記）——クライアントへは綺麗な 422（X-Request-Id 付き）を
    返しつつ、迷子の監査行を残さないことを固定する（三項目 assert が成立しないのはこの経路が
    そもそも監査行を持たないため、という前提を明示する）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    rid = f"probe-keycreate-malformed-{sfx}"
    r = client.post(
        "/ext/v1/admin/keys", content=b"{not valid json at all",
        headers={"Content-Type": "application/json", "X-Request-Id": rid})
    assert r.status_code == 422, r.text
    assert r.headers["X-Request-Id"] == rid
    _logout()

    with store._connect() as c:
        rows = c.execute(
            "SELECT id FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchall()
    assert rows == [], (
        "admin 系ルートの malformed JSON はフォールバック監査の対象外＝監査行は書かれないはず"
        f"（実際 {len(rows)} 件）")


def test_key_create_two_world_scope(tmp_path):
    """2つの**実在** world をスコープに持てる（`store.upsert_world` で本物の registry 行を作る・
    `worlds.resolve_external_world` はモックしない＝実際の scope/存在検証経路をそのまま通す）。
    スコープに含めた未知（実在しない）world は key 発行時点で 422（`_validate_allowed_worlds_or_error`
    が個別に strict resolve するため）。"""
    if not _try_init():
        pytest.skip("DB down")

    sfx = _sfx()
    second_world = f"v2real{sfx}"
    root2 = tmp_path / "root2"
    root2.mkdir()
    (root2 / "note.md").write_text("第二世界の資料です", encoding="utf-8")
    store.upsert_world(second_world, str(root2))
    try:
        adm_uid, adm_pw = _mk_admin(sfx)
        _login(adm_uid, adm_pw)
        issued = _issue_key_scoped(f"twoworld-{sfx}", ["v1", second_world])
        assert issued["allowed_worlds"] == ["v1", second_world]

        r = client.post("/ext/v1/admin/keys",
                        json={"label": f"badscope-{sfx}",
                              "allowed_worlds": [second_world, "nonexistent-world-zzz"]})
        assert r.status_code == 422, r.text
        _logout()

        r = _search({"world": "v1", "query": "税", "engines": ["keyword"]}, api_key=issued["key"])
        assert r.status_code == 200, r.text
        r = _search({"world": second_world, "query": "資料", "engines": ["keyword"]},
                   api_key=issued["key"])
        assert r.status_code == 200, r.text
        r = _doc(second_world, "note.md", api_key=issued["key"])
        assert r.status_code == 200, r.text
        assert "第二世界" in r.text
    finally:
        # `store.upsert_world` は共有テスト DB に本物の registry 行を作る（実 scope 検証経路を
        # 通すため・モックしない裁定）。片付けないと `worlds.register()`（「全体で1本だけ」制約）を
        # 使う他テスト（例: tests/integration/test_worlds_sig_lock_discipline.py）を毎回壊す
        # （実際に踏んだ回帰 周辺の確認で発覚）。
        store.delete_world_row(second_world)


def test_key_create_empty_allowed_worlds_denies_all():
    """`allowed_worlds: []`（意図的な空配列）はどの world にもアクセスできない（deny-all）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key_scoped(f"denyall-{sfx}", [])
    assert issued["allowed_worlds"] == []
    _logout()

    r = _search({"world": "v1", "query": "x"}, api_key=issued["key"])
    assert r.status_code == 403, r.text
    r = _doc("v1", "4期/01_標準/消費税法.md", api_key=issued["key"])
    assert r.status_code == 403, r.text
    r = _capabilities(api_key=issued["key"])
    assert r.status_code == 200, r.text
    assert r.json()["worlds"] == []


# ===== X-Request-Id =====

def test_request_id_echoed_and_generated():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"reqid-{sfx}")
    _logout()

    # クライアント指定値はそのまま応答ヘッダへ返る。
    r = client.get("/ext/v1/capabilities",
                   headers={"X-API-Key": issued["key"], "X-Request-Id": "probe-req-1"})
    assert r.status_code == 200, r.text
    assert r.headers["X-Request-Id"] == "probe-req-1"

    # 未指定なら採番され、応答ヘッダに載る。
    r2 = client.get("/ext/v1/capabilities", headers={"X-API-Key": issued["key"]})
    assert r2.status_code == 200, r2.text
    assert r2.headers.get("X-Request-Id")

    # 401 応答にもヘッダが付く（キー不正でも request_id は解決される）。
    r3 = client.get("/ext/v1/capabilities", headers={"X-Request-Id": "probe-req-401"})
    assert r3.status_code == 401, r3.text
    assert r3.headers["X-Request-Id"] == "probe-req-401"


def test_request_id_recorded_in_audit():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"reqidaudit-{sfx}")
    _logout()
    # request_id は sfx で一意化する（固定リテラルだと、共有 sherpa_test DB に残る過去実行や
    # 並行実行中の他テストの同名行と衝突しうる）。
    rid = f"probe-audit-req-{sfx}"

    r = client.get("/ext/v1/capabilities",
                   headers={"X-API-Key": issued["key"], "X-Request-Id": rid})
    assert r.status_code == 200, r.text

    with store._connect() as c:
        row = c.execute(
            "SELECT request_id FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchone()
    assert row is not None
    assert row["request_id"] == rid


def test_request_id_present_on_representative_error_statuses(monkeypatch):
    """403/404/413/422/429 の代表的な失敗応答にも X-Request-Id が付く
    （`ExtRequestMiddleware` が routing/validation の外側で一元的に付与するため、
    自動バリデーション由来・ドメインエラー由来のどちらの応答でも漏れない）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import ext_api

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    scoped = _issue_key_scoped(f"reqid403-{sfx}", ["v1"])
    plain = _issue_key(f"reqidplain-{sfx}")
    _logout()

    # 403: world スコープ外
    r = client.post("/ext/v1/search", json={"world": "other-world-xyz", "query": "x"},
                    headers={"X-API-Key": scoped["key"], "X-Request-Id": "probe-403"})
    assert r.status_code == 403, r.text
    assert r.headers["X-Request-Id"] == "probe-403"

    # 404: 世界不在
    r = client.post("/ext/v1/search", json={"world": "no-such-world-xyz", "query": "x"},
                    headers={"X-API-Key": plain["key"], "X-Request-Id": "probe-404"})
    assert r.status_code == 404, r.text
    assert r.headers["X-Request-Id"] == "probe-404"

    # 422: 不明な scope_paths（FastAPI/pydantic の自動検証ではなくハンドラ内の 422）
    r = client.post("/ext/v1/search",
                    json={"world": "v1", "query": "x", "scope_paths": ["no-such-scope-xyz"]},
                    headers={"X-API-Key": plain["key"], "X-Request-Id": "probe-422"})
    assert r.status_code == 422, r.text
    assert r.headers["X-Request-Id"] == "probe-422"

    # 413: 変換サイズ上限
    monkeypatch.setattr(ext_api, "_CONVERT_MAX_BYTES", 10)
    r = client.post(
        "/ext/v1/convert",
        files={"file": ("big.docx", io.BytesIO(b"x" * 100), "application/octet-stream")},
        headers={"X-API-Key": plain["key"], "X-Request-Id": "probe-413"})
    assert r.status_code == 413, r.text
    assert r.headers["X-Request-Id"] == "probe-413"

    # 429: レート制限
    monkeypatch.setattr(ext_api.ratelimit, "check_ext_api_rate_limit", lambda key_id: 3)
    r = client.get("/ext/v1/capabilities",
                   headers={"X-API-Key": plain["key"], "X-Request-Id": "probe-429"})
    assert r.status_code == 429, r.text
    assert r.headers["X-Request-Id"] == "probe-429"


# ===== request-level 監査（ExtRequestMiddleware・自動422/未処理例外を含む全終了経路）=====

def test_auto_validation_422_is_audited():
    """自動バリデーション422（handler 本体が一度も実行されない終了経路）も監査される
    （`require_api_key` が action/actor を仮置きし、`ExtRequestMiddleware` が実ステータスで書く）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"auto422-{sfx}")
    _logout()
    rid = f"probe-auto422-{sfx}"

    r = client.post("/ext/v1/search", json={"world": "v1", "query": "x", "k": 999},
                    headers={"X-API-Key": issued["key"], "X-Request-Id": rid})
    assert r.status_code == 422, r.text

    with store._connect() as c:
        row = c.execute(
            "SELECT outcome, reason, detail FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchone()
    assert row is not None
    assert row["outcome"] == "error"
    assert row["reason"] == "validation_error"
    assert row["detail"]["http_status"] == 422


def test_unhandled_exception_gets_request_id_and_is_audited(monkeypatch):
    """handler 内の未処理例外でも X-Request-Id が付き、500 として監査される
    （`ExtRequestMiddleware` が自前で 500 応答を組み立てる・再送出はしない）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import search_service

    def _boom(*a, **kw):
        raise RuntimeError("simulated bug")

    monkeypatch.setattr(search_service, "search", _boom)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"unhandled500-{sfx}")
    _logout()
    rid = f"probe-500-{sfx}"

    r = client.post("/ext/v1/search", json={"world": "v1", "query": "x"},
                    headers={"X-API-Key": issued["key"], "X-Request-Id": rid})
    assert r.status_code == 500, r.text
    assert r.headers["X-Request-Id"] == rid

    with store._connect() as c:
        row = c.execute(
            "SELECT detail FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchone()
    assert row is not None
    assert row["detail"]["http_status"] == 500


def test_audit_row_has_all_fields_exactly_once():
    """成功応答は監査行がちょうど1件だけ・全項目（http_status/duration_ms/result_count/
    method/path/world/prefix/business_outcome）を含む。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"auditfields-{sfx}")
    _logout()

    r = _search({"world": "v1", "query": "税", "engines": ["keyword"]}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    req_id = r.headers["X-Request-Id"]

    with store._connect() as c:
        rows = c.execute(
            "SELECT outcome, detail FROM audit_log WHERE action='ext_api.search' AND request_id=%s",
            (req_id,)
        ).fetchall()
    assert len(rows) == 1, f"監査行はちょうど1件のはず（実際 {len(rows)} 件）"
    detail = rows[0]["detail"]
    for key in ("http_status", "duration_ms", "result_count", "method", "path", "world", "prefix",
               "business_outcome"):
        assert key in detail, f"detail に {key} が無い: {detail}"
    assert detail["http_status"] == 200
    assert detail["method"] == "POST"
    assert detail["path"] == "/ext/v1/search"
    assert rows[0]["outcome"] == "success"


def test_convert_business_outcome_failed_on_conversion_failure():
    """変換失敗（HTTP レベルは200・`unsupported: true`）は `business_outcome=failed` で記録する
    （HTTP の outcome=success とは別列の業務結果）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"convfail-{sfx}")
    _logout()
    rid = f"probe-convfail-{sfx}"

    r = client.post(
        "/ext/v1/convert",
        files={"file": ("broken.docx", io.BytesIO(b"not a real zip file"), "application/octet-stream")},
        headers={"X-API-Key": issued["key"], "X-Request-Id": rid})
    assert r.status_code == 200, r.text
    assert r.json()["unsupported"] is True

    with store._connect() as c:
        row = c.execute(
            "SELECT outcome, detail FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchone()
    assert row is not None
    assert row["outcome"] == "success"
    assert row["detail"]["business_outcome"] == "failed"


# ===== 外部 API 専用 strict world resolver（registry/root 不達は 503）=====

def test_search_registry_unreachable_returns_503(monkeypatch):
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import worlds

    def _boom(world_id, **kw):
        raise worlds.ExternalResolverError("simulated registry outage")

    monkeypatch.setattr(worlds, "resolve_external_world", _boom)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"search503-{sfx}")
    _logout()

    r = _search({"world": "v1", "query": "x"}, api_key=issued["key"])
    assert r.status_code == 503, r.text


def test_doc_registry_unreachable_returns_503(monkeypatch):
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import worlds

    def _boom(world_id, **kw):
        raise worlds.ExternalResolverError("simulated registry outage")

    monkeypatch.setattr(worlds, "resolve_external_world", _boom)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"doc503-{sfx}")
    _logout()

    r = _doc("v1", "a.md", api_key=issued["key"])
    assert r.status_code == 503, r.text


def test_capabilities_registry_unreachable_returns_503(monkeypatch):
    """fixtures/dev KB のファイルシステム列挙（`discover_fs_world_ids_strict`）自体が失敗した
    場合も 503（registry スナップショット取得の失敗とは別経路・どちらも同じ 503 契約）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import worlds

    def _boom():
        raise worlds.ExternalResolverError("simulated fs enumeration outage")

    monkeypatch.setattr(worlds, "discover_fs_world_ids_strict", _boom)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"capsregistry503-{sfx}")
    _logout()

    r = _capabilities(api_key=issued["key"])
    assert r.status_code == 503, r.text


def test_search_registered_root_unreachable_returns_503(tmp_path):
    """レジストリには登録されているが参照先ディレクトリが無い（マウント外れ等）＝503
    （未登録＝404 とは区別する）。`store.get_world`/`resolve_external_world` はモックせず、
    実際に登録した world の root を削除して実装経由で確認する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    wid = f"realbroken{sfx}"
    root = tmp_path / "root"
    root.mkdir()
    (root / "note.md").write_text("x", encoding="utf-8")
    store.upsert_world(wid, str(root))
    try:
        import shutil
        shutil.rmtree(root)   # 登録後にマウント外れ/削除を模擬（resolve_external_world は実処理で辿る）

        adm_uid, adm_pw = _mk_admin(sfx)
        _login(adm_uid, adm_pw)
        issued = _issue_key(f"rootunreach-{sfx}")
        _logout()

        r = _search({"world": wid, "query": "x"}, api_key=issued["key"])
        assert r.status_code == 503, r.text
    finally:
        store.delete_world_row(wid)


# fd 所有権（`sherpa.fd_response.FdOwner`/`FdFileResponse` の ASGI __call__ 全体を try/finally）
# ＝documents/ext_api 両ルータの共有部品。単体テストは tests/unit/test_fd_response.py（DB 不要）
# へ集約済み（本ファイルからは重複するため撤去）。

# ===== malformed body・キャンセル・並行監査・admin監査統合・UTF8/ZIP境界・request_id⇔ログ =====

def test_malformed_json_body_is_still_audited():
    """本文が JSON としてすら解析できない（`RequestValidationError` が `require_api_key` 実行前に
    発生する）場合でも、`ExtRequestMiddleware` のフォールバック（`_fallback_audit_pending`）が
    ヘッダの X-API-Key から identity を解決し、監査行を1件書く（audit_pending が最後まで
    None のまま=無記録、という抜け穴を塞ぐ）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"malformed-{sfx}")
    _logout()
    # request_id は sfx で一意化する（固定リテラルだと、共有 sherpa_test DB に残る過去実行の
    # 同名行と衝突しうる）。
    rid = f"probe-malformed-{sfx}"

    r = client.post("/ext/v1/search", content=b"{not valid json at all",
                    headers={"X-API-Key": issued["key"], "Content-Type": "application/json",
                            "X-Request-Id": rid})
    assert r.status_code == 422, r.text
    assert r.headers["X-Request-Id"] == rid

    with store._connect() as c:
        row = c.execute(
            "SELECT actor_user_id, outcome, reason, detail FROM audit_log "
            "WHERE request_id=%s ORDER BY id DESC LIMIT 1", (rid,)
        ).fetchone()
    assert row is not None, "malformed body でも監査行が書かれているはず"
    assert row["actor_user_id"] == f"ext:{issued['id']}"
    assert row["detail"]["business_outcome"] == "failed"


def test_malformed_json_body_unknown_key_records_unknown_actor():
    """malformed body＋無効な X-API-Key でも監査は書かれる（actor は特定できないため
    "ext:unknown"）——フォールバック経路自体が例外で落ちない対照。"""
    if not _try_init():
        pytest.skip("DB down")
    # request_id は sfx で一意化する（固定リテラルだと、共有 sherpa_test DB に残る過去実行の
    # 同名行と衝突しうる）。
    rid = f"probe-malformed-unknown-{_sfx()}"
    r = client.post("/ext/v1/search", content=b"{not valid json",
                    headers={"X-API-Key": "sk-ext-totally-bogus-key-xyz",
                            "Content-Type": "application/json",
                            "X-Request-Id": rid})
    assert r.status_code == 422, r.text

    with store._connect() as c:
        row = c.execute(
            "SELECT actor_user_id, detail FROM audit_log WHERE request_id=%s "
            "ORDER BY id DESC LIMIT 1", (rid,)
        ).fetchone()
    assert row is not None
    assert row["actor_user_id"] == "ext:unknown"


def _minimal_asgi_http_scope(path: str, headers: list) -> dict:
    """手組みの ASGI HTTP scope（middleware 単体テスト用）。`query_string`/`scheme`/`server`/
    `client`/`root_path` を省くと、応答開始前に例外が起きた経路で Starlette 自身の
    `ServerErrorMiddleware` が内部的に `Request(scope).query_params` 等へアクセスして
    `KeyError` を起こすことがある（実測）——最小限だが完全な scope を1箇所にまとめる。"""
    return {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
            "query_string": b"", "scheme": "http", "root_path": "",
            "server": ("testserver", 80), "client": ("testclient", 12345),
            "headers": headers}


def _bare_ext_app():
    """`ExtRequestMiddleware` を装着していない裸の FastAPI app（`ext_api.router` だけを含む・
    `request.app` 経由の依存解決は効く）。`sherpa.api.app` は `app.add_middleware()` で
    このミドルウェアを**既に装着済み**のため、`ExtRequestMiddleware(app)` のように直接包むと
    二重に呼ばれてしまう（監査行が複数書かれる・実際に踏んだ回帰）。middleware 単体テスト専用。
    """
    from fastapi import FastAPI

    from sherpa import ext_api
    bare = FastAPI()
    bare.include_router(ext_api.router)
    return bare


def test_cancelled_request_still_writes_audit_and_resets_contextvar():
    """`asyncio.CancelledError` が応答完了前に起きても、監査書込と ContextVar reset は
    `asyncio.shield()` で保護されて完了する（キャンセルシールド）。"""
    if not _try_init():
        pytest.skip("DB down")
    import asyncio

    from sherpa import ext_api

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"cancel-{sfx}")
    _logout()
    rid = f"probe-cancel-{sfx}"   # sfx で一意化（固定リテラルだと共有 sherpa_test DB の過去実行行と衝突する）

    async def _run():
        scope = _minimal_asgi_http_scope(
            "/ext/v1/capabilities",
            [(b"x-api-key", issued["key"].encode()), (b"x-request-id", rid.encode())])

        async def _receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        sent = []

        async def _send(message):
            sent.append(message)
            if message["type"] == "http.response.start":
                raise asyncio.CancelledError()

        mw = ext_api.ExtRequestMiddleware(_bare_ext_app())   # 二重装着を避ける（helper docstring 参照）
        with pytest.raises(asyncio.CancelledError):
            await mw(scope, _receive, _send)

    asyncio.run(_run())

    assert ext_api._request_id_ctx.get() is None, "ContextVar がリクエスト後にリセットされていない"
    with store._connect() as c:
        rows = c.execute(
            "SELECT outcome, reason, detail FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchall()
    assert len(rows) == 1, f"監査行はちょうど1件のはず（実際 {len(rows)} 件）"
    row = rows[0]
    # status_holder["code"] は None のまま（http.response.start の送信自体が CancelledError で
    # 失敗している）＝ status=0 のまま success 扱いになっていないことを固定する。
    assert row["outcome"] == "error", row
    assert row["reason"] == "cancelled"
    assert row["detail"]["business_outcome"] == "failed"
    assert row["detail"]["http_status"] == 0


def test_send_start_failure_with_self_generated_500_also_failing_is_audited(monkeypatch):
    """`http.response.start` の送信自体が失敗し（早期切断相当）、その後の自己生成500の再送も
    同じ理由で失敗するケースでも、例外を握り潰さず・status=0のままsuccess扱いにもならず、
    outcome=error・reason=delivery_failedで監査され、最初の送信例外が再送出される
    （自己生成500を含む全 send を同一追跡関数に通す）。"""
    if not _try_init():
        pytest.skip("DB down")
    import asyncio

    from sherpa import ext_api

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"sendfail-{sfx}")
    _logout()
    rid = f"probe-sendfail-{sfx}"

    async def _run():
        scope = _minimal_asgi_http_scope(
            "/ext/v1/capabilities",
            [(b"x-api-key", issued["key"].encode()), (b"x-request-id", rid.encode())])

        async def _receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        raised: list[Exception] = []   # send() が実際に投げた例外オブジェクトを呼び出し順に記録する

        async def _send(message):
            exc = ConnectionResetError("simulated early disconnect (every send fails)")
            raised.append(exc)
            raise exc

        mw = ext_api.ExtRequestMiddleware(_bare_ext_app())   # 二重装着を避ける（helper docstring 参照）
        with pytest.raises(ConnectionResetError) as exc_info:
            await mw(scope, _receive, _send)
        assert len(raised) >= 2, (
            f"send が2回以上（start 失敗＋自己生成500の再送失敗）呼ばれているはず（実際 {len(raised)} 回）")
        assert exc_info.value is raised[0], (
            "再送出された例外が最初の送信失敗そのものではない（同一性不一致）——自己生成500の"
            "再送で発生した後続の例外にすり替わっている可能性がある")

    asyncio.run(_run())

    with store._connect() as c:
        rows = c.execute(
            "SELECT outcome, reason, detail FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchall()
    assert len(rows) == 1, f"監査行はちょうど1件のはず（実際 {len(rows)} 件）"
    row = rows[0]
    assert row["outcome"] == "error", row
    assert row["reason"] == "delivery_failed"
    assert row["detail"]["business_outcome"] == "failed"
    assert row["detail"]["http_status"] == 0


def test_send_body_failure_after_start_succeeds_is_audited_as_delivery_failed():
    """`http.response.start` は成功したが、その後の `http.response.body` 送信で失敗する
    （早期切断の典型パターン）場合も、実際に観測できた status（200 等）を "success" に
    誤記録せず、outcome=error・reason=delivery_failedで監査される。"""
    if not _try_init():
        pytest.skip("DB down")
    import asyncio

    from sherpa import ext_api

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"bodyfail-{sfx}")
    _logout()
    rid = f"probe-bodyfail-{sfx}"

    async def _run():
        scope = _minimal_asgi_http_scope(
            "/ext/v1/capabilities",
            [(b"x-api-key", issued["key"].encode()), (b"x-request-id", rid.encode())])

        async def _receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(message):
            if message["type"] == "http.response.body":
                raise ConnectionResetError("simulated disconnect mid-body")

        mw = ext_api.ExtRequestMiddleware(_bare_ext_app())   # 二重装着を避ける（helper docstring 参照）
        with pytest.raises(ConnectionResetError):
            await mw(scope, _receive, _send)

    asyncio.run(_run())

    with store._connect() as c:
        rows = c.execute(
            "SELECT outcome, reason, detail FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchall()
    assert len(rows) == 1, f"監査行はちょうど1件のはず（実際 {len(rows)} 件）"
    row = rows[0]
    assert row["outcome"] == "error", row
    assert row["reason"] == "delivery_failed"
    assert row["detail"]["business_outcome"] == "failed"
    assert row["detail"]["http_status"] == 200, "start は成功済みなので観測できた実ステータスは残る"


def test_audit_write_does_not_block_event_loop(monkeypatch):
    """監査 DB 書込を専用 writer スレッドへ逃がしているため、1件の書込みが人為的に遅延しても
    同じ event loop 上の**他のコルーチン**は並行して進む（単一 writer スレッドである
    以上、監査書込み**同士**は queue で直列化される——書込み中の別リクエストの応答完了まで
    同じだけ待たされうる——が、それは「event loop がブロックされる」こととは別物。ここでは
    event loop レベルで直接検証する（`_write_pending_audit_async` を await している間、
    同じ event loop の他のコルーチンが進めることを確認する）。"""
    if not _try_init():
        pytest.skip("DB down")
    import asyncio
    import threading

    from sherpa import ext_api

    orig_write = ext_api._write_pending_audit
    release = threading.Event()

    def _slow_write(pending, *a, **kw):
        release.wait(timeout=5)
        return orig_write(pending, *a, **kw)

    monkeypatch.setattr(ext_api, "_write_pending_audit", _slow_write)
    # request_id は sfx で一意化する（固定リテラルだと共有 sherpa_test DB に残る過去実行の
    # 同名行を拾ってしまう）。
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

        await asyncio.wait_for(_other_coro(), timeout=2)   # …同じ event loop の別コルーチンが進む
        assert progressed["v"] is True
        assert not write_task.done(), "監査書込みがまだ release 待ちのはず（早期完了は想定外）"

        release.set()
        await asyncio.wait_for(write_task, timeout=5)

    asyncio.run(_run())

    with store._connect() as c:
        row = c.execute(
            "SELECT actor_user_id FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchone()
    assert row is not None and row["actor_user_id"] == "ext:evloop-test"


def test_admin_key_routes_are_audited():
    """管理系3ルート（発行/一覧/失効）も `ext_api.start_audit()` 経由で同じ request-level 監査へ
    統合されている。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    # request_id は sfx で一意化する（固定リテラルだと、共有 sherpa_test DB に残る過去実行の
    # 同名行を拾って actor が食い違う偽陽性/陰性が起きうる・実際に踏んだ回帰）。
    rid_create, rid_list, rid_revoke = (f"probe-admin-create-{sfx}", f"probe-admin-list-{sfx}",
                                        f"probe-admin-revoke-{sfx}")

    r = client.post("/ext/v1/admin/keys", json={"label": f"adminaudit-{sfx}"},
                    headers={"X-Request-Id": rid_create})
    assert r.status_code == 200, r.text
    key_id = r.json()["id"]
    plain_key = r.json()["key"]   # プレーンキーはこの応答で1度だけ返る

    r = client.get("/ext/v1/admin/keys", headers={"X-Request-Id": rid_list})
    assert r.status_code == 200, r.text

    r = client.delete(f"/ext/v1/admin/keys/{key_id}", headers={"X-Request-Id": rid_revoke})
    assert r.status_code == 200, r.text
    _logout()

    with store._connect() as c:
        rows = {row["request_id"]: row for row in c.execute(
            "SELECT request_id, action, actor_user_id, detail FROM audit_log "
            "WHERE request_id = ANY(%s)",
            ([rid_create, rid_list, rid_revoke],)
        ).fetchall()}
    assert set(rows) == {rid_create, rid_list, rid_revoke}
    assert rows[rid_create]["action"] == "ext_api.key_created"
    assert rows[rid_create]["actor_user_id"] == adm_uid
    assert rows[rid_list]["action"] == "ext_api.key_listed"
    assert rows[rid_revoke]["action"] == "ext_api.key_revoked"
    # プレーンキー本体は3ルートいずれの監査 detail にも一切現れない（key_prefix のみ記録する契約）。
    for rid, row in rows.items():
        assert plain_key not in json.dumps(row["detail"]), (
            f"監査行 {rid}（{row['action']}）の detail にプレーンキー本体が含まれている: {row['detail']}")


def test_doc_charset_skipped_when_file_exceeds_validation_cap(tmp_path, monkeypatch):
    """`.txt` が有効な UTF-8 でも、ファイル全体が検証上限（64KiB）を超える場合は打ち切って
    charset を宣言しない（「未検証」を utf-8 と偽らない方針）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import ext_api
    monkeypatch.setattr(ext_api, "_UTF8_VALIDATE_CAP", 100)

    root = tmp_path / "root"
    root.mkdir()
    (root / "big.txt").write_text("あ" * 200, encoding="utf-8")   # 全体は有効な UTF-8・上限だけ超える
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "big.txt", api_key=key)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "text/plain"   # charset 宣言なし（検証していないため）


def test_doc_accepts_bigtiff_magic(tmp_path, monkeypatch):
    """BigTIFF（version 43）のシグネチャも受理する（classic TIFF の version 42 だけでなく）。"""
    if not _try_init():
        pytest.skip("DB down")

    root = tmp_path / "root"
    root.mkdir()
    content = b"II+\x00" + b"\x08\x00\x00\x00" + b"\x00" * 32   # BigTIFF header（little-endian）
    (root / "big.tif").write_bytes(content)
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "big.tif", api_key=key)
    assert r.status_code == 200, r.text
    assert r.content == content


def test_doc_rejects_ooxml_with_too_many_zip_members(tmp_path, monkeypatch):
    """EOCD のメンバ数が上限を超える OOXML は `_zip_bounded_check` で拒否する（`zipfile.ZipFile`
    への解析委譲は無い＝メンバー名も検証済みの central directory 走査から直接得る・
    legacy-Office/OOXML 検証方針: bounded EOCD 検査）。"""
    if not _try_init():
        pytest.skip("DB down")
    import zipfile as zf

    from sherpa import ext_api
    monkeypatch.setattr(ext_api, "_ZIP_MAX_MEMBERS", 3)

    root = tmp_path / "root"
    root.mkdir()
    p = root / "many.docx"
    with zf.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<document/>")
        for i in range(10):
            z.writestr(f"extra/{i}.txt", "x")
    _mock_external_world(monkeypatch, root)

    key = _mk_doc_key(_sfx())
    r = _doc("docsafety", "many.docx", api_key=key)
    assert r.status_code == 415, r.text


def test_request_id_appears_on_application_log_records(caplog):
    """`ExtRequestMiddleware` が束縛した request_id が、共通ロガー（"sherpa"）経由で出す
    アプリログの `record.request_id` にも乗る（`_RequestIdLogFilter`）。"""
    if not _try_init():
        pytest.skip("DB down")
    import logging as logging_mod

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"logreqid-{sfx}")
    _logout()

    with caplog.at_level(logging_mod.INFO, logger="sherpa"):
        r = client.get("/ext/v1/capabilities",
                       headers={"X-API-Key": issued["key"], "X-Request-Id": "probe-log-reqid"})
    assert r.status_code == 200, r.text
    matching = [rec for rec in caplog.records
               if getattr(rec, "request_id", None) == "probe-log-reqid"]
    assert matching, ("request_id='probe-log-reqid' を持つログレコードが見つからない: "
                      f"{[(rec.name, rec.getMessage()) for rec in caplog.records]}")


# ===== POST /ext/v1/research（PART-4: AI 下調べ検索）=====
#
# agentic search 本体（ツール実行・Evidence Packet 組み立て）は tests/unit/test_ext2_evidence.py で
# 既に検証済み（`_dedupe_citations_and_evidence`/`_evidence_packet_evidence` 等）。ここでは ext_api 層
# の配線（認証・world/scope 検証・監査・model 許容値検証・プロバイダ未接続の honest failure・利用統計
# 記録）を検証する。LLM は `agentic_search._post` を差し替えて呼ぶ（実 Ollama/OpenAI 到達不要・
# `test_ext2_evidence.py` と同じ手法）。

_RESEARCH_REAL_DOC = "4期/04_運用/障害記録.md"   # fixtures/corpus/v1 実在ファイル（test_ext2_evidence.py と同一）


def test_normalize_evidence_spans_folds_none_element_to_none():
    """RV12 是正の固定: `source_span` の要素に `None` が1つでも含まれていれば全体を `None` へ畳む
    （`ExtEvidenceItem.source_span: list[int] | None` は要素として `None` を許さないため）。
    有効な整数 span・既に `None` の span は変更しない。"""
    from sherpa import ext_api

    evidence = [
        {"evidence_id": "ev-1", "source_span": [None, None]},
        {"evidence_id": "ev-2", "source_span": [5, None]},
        {"evidence_id": "ev-3", "source_span": [3, 6]},
        {"evidence_id": "ev-4", "source_span": None},
        {"evidence_id": "ev-5"},   # キー自体が無いケースも壊さない
    ]
    ext_api._normalize_evidence_spans(evidence)
    assert evidence[0]["source_span"] is None
    assert evidence[1]["source_span"] is None
    assert evidence[2]["source_span"] == [3, 6]
    assert evidence[3]["source_span"] is None
    assert evidence[4].get("source_span") is None


def _research(payload: dict, api_key: str | None = None):
    headers = {"X-API-Key": api_key} if api_key else {}
    return client.post("/ext/v1/research", json=payload, headers=headers)


def _install_agentic_post(monkeypatch, seq):
    """`sherpa.agentic_search._post` を固定応答列に差し替える（tests/unit/test_ext2_evidence.py と同じ手法）。"""
    from sherpa import agentic_search as A
    monkeypatch.setattr(A, "es_index", A.es_index)
    monkeypatch.setattr(A.es_index, "available", lambda: False)
    monkeypatch.setattr(A, "_graph_available", lambda: False)
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))


_RESEARCH_SUCCESS_SEQ = [
    {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "ripgrep_search",
         "arguments": '{"query":"税率改定に伴う障害の記録"}'}}]}}]},
    {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c2", "function": {"name": "read_around",
         "arguments": f'{{"doc_id":"{_RESEARCH_REAL_DOC}","line":1}}'}}]}}]},
    # finish_reason=stop（自然完了 allowlist）が無いと帰属呼び出し（次項）自体が省略される
    # （`agentic_search._is_natural_completion` 参照）。
    {"choices": [{"message": {"content": "税率改定に伴う障害です。"}, "finish_reason": "stop"}]},
    # attribution 呼び出し（EV-0）: `submit_attribution` の tool 強制呼び出しへの応答形
    # （`agentic_search.attribute_openai_style` 参照・プレーンな JSON content ではない）。
    # 1回目は `openai_style` 自身が内部で行う帰属（重複排除**前**の添字・research_service は
    # この結果を使わない）。2回目は `research_service.run_research` が最終重複排除の**後**に
    # やり直す帰属（Evidence Packet の `used` へ実際に反映されるのはこちら）。
    {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c3", "function": {"name": "submit_attribution", "arguments": '{"used":["ev-1"]}'}}]}}]},
    {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c4", "function": {"name": "submit_attribution", "arguments": '{"used":["ev-1"]}'}}]}}]},
]


def test_ext_research_requires_api_key():
    if not _try_init():
        pytest.skip("DB down")
    r = _research({"world": "v1", "query": "税率改定の障害は？"})
    assert r.status_code == 401, r.text


def test_ext_research_unknown_world_404():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"research404-{sfx}")
    _logout()

    r = _research({"world": "no-such-world-xyz", "query": "x"}, api_key=issued["key"])
    assert r.status_code == 404, r.text


def test_ext_research_unknown_scope_422():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"research422-{sfx}")
    _logout()

    r = _research({"world": "v1", "query": "x", "scope_paths": ["no-such-scope-xyz"]},
                  api_key=issued["key"])
    assert r.status_code == 422, r.text


def test_ext_research_param_limits_rejected_with_422():
    """反復上限/件数上限/タイムアウトは Pydantic Field 制約（`/ext/v1/search` の k/depth/weights と
    同じ自動バリデーション経路）——範囲外は 422（LLM を一切呼ばない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchlimits-{sfx}")
    _logout()

    for bad in ({"max_iterations": 13}, {"max_iterations": 0}, {"max_results": 0},
               {"max_results": 51}, {"timeout_s": 4}, {"timeout_s": 181}):
        r = _research({"world": "v1", "query": "x", **bad}, api_key=issued["key"])
        assert r.status_code == 422, f"{bad}: {r.text}"


def test_ext_research_world_scope_403():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key_scoped(f"research403-{sfx}", ["v1"])
    _logout()

    r = _research({"world": "other-world-xyz", "query": "x"}, api_key=issued["key"])
    assert r.status_code == 403, r.text


def test_ext_research_model_not_allowed_400():
    """許可リスト外の model は 400（LLM を一切呼ばない・ネットワーク到達不要）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchbadmodel-{sfx}")
    _logout()

    r = _research({"world": "v1", "query": "x", "model": "not-a-real-model"}, api_key=issued["key"])
    assert r.status_code == 400, r.text


def test_ext_research_openai_unavailable_returns_503_no_fallback(monkeypatch):
    """model が openai 側カタログに一致する値でも、openai_api_key 未設定なら 503（黙って ollama へ
    フォールバックしない）。"""
    if not _try_init():
        pytest.skip("DB down")
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})   # cloud_provider 未選択＝鍵解決は常に None

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchnokey-{sfx}")
    _logout()

    r = _research({"world": "v1", "query": "x", "model": "gpt-5.4-mini"}, api_key=issued["key"])
    assert r.status_code == 503, r.text


def test_ext_research_unknown_provider_422():
    """PART-4a: `provider` は ollama/openai の2択のみ（pydantic Literal・LLM を一切呼ばない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchbadprovider-{sfx}")
    _logout()

    r = _research({"world": "v1", "query": "x", "provider": "gemini"}, api_key=issued["key"])
    assert r.status_code == 422, r.text


def test_ext_research_provider_openai_without_key_returns_503_fixed_message(monkeypatch):
    """PART-4a: `provider=openai` を明示指定し、中央キー未設定なら 503＋固定文言
    （`keys.NO_CENTRAL_KEY_MESSAGE`・黙って ollama へフォールバックしない）。"""
    if not _try_init():
        pytest.skip("DB down")
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})   # openai_api_key 未設定

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchprovidernokey-{sfx}")
    _logout()

    r = _research({"world": "v1", "query": "x", "provider": "openai"}, api_key=issued["key"])
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == keys.NO_CENTRAL_KEY_MESSAGE


def test_ext_research_provider_openai_routes_to_openai_when_model_omitted(monkeypatch):
    """PART-4a: `model` 省略・`provider=openai` 明示指定時は、管理者設定の既定（ollama）ではなく
    openai 側カタログの既定モデルを使って openai 経路を呼ぶ（provider_used=="openai" で固定）。"""
    if not _try_init():
        pytest.skip("DB down")
    monkeypatch.setattr(store, "get_system_settings",
                        lambda **kw: {"openai_api_key": "sk-fake-test-key-for-provider-test"})
    _install_agentic_post(monkeypatch, list(_RESEARCH_SUCCESS_SEQ))

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchproveropenai-{sfx}")
    _logout()

    r = _research({"world": "v1", "query": "税率改定の障害は？", "provider": "openai"},
                  api_key=issued["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider_used"] == "openai"
    assert body["model_used"] == "gpt-5.4-mini"


def test_ext_research_provider_omitted_uses_admin_default_provider_setting(monkeypatch):
    """リクエストの `provider` を省略した場合、管理者設定 `research_default_provider`
    （既定 ollama）がハードコード既定より優先される——ここでは "openai" に設定した状態を模し、
    明示指定なしでも openai 経路が呼ばれることを確認する。"""
    if not _try_init():
        pytest.skip("DB down")
    monkeypatch.setattr(store, "get_system_settings",
                        lambda **kw: {"openai_api_key": "sk-fake-test-key-for-provider-test",
                                     "research_default_provider": "openai"})
    _install_agentic_post(monkeypatch, list(_RESEARCH_SUCCESS_SEQ))

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchdefaultopenai-{sfx}")
    _logout()

    r = _research({"world": "v1", "query": "税率改定の障害は？"}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider_used"] == "openai"
    assert body["model_used"] == "gpt-5.4-mini"


def test_ext_research_ollama_default_success_with_evidence(monkeypatch):
    """model 省略＝既定 Ollama。Evidence Packet（Committed Evidence）付きで 200 を返す。"""
    if not _try_init():
        pytest.skip("DB down")
    _install_agentic_post(monkeypatch, list(_RESEARCH_SUCCESS_SEQ))

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchok-{sfx}")
    _logout()

    r = _research({"world": "v1", "query": "税率改定の障害は？"}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["world"] == "v1"
    assert body["provider_used"] == "ollama"
    assert body["model_used"] == "qwen2.5"
    # iterations（可視ステップ数=ツール呼び出し2回）と llm_calls（課金相当=ツール2回+最終合成+
    # 帰属呼び出し1回〔内部のみ・単一 citation で ev-N 採番がずれないため研究サービス側の再帰属は
    # 発行されない・RV12 是正で二重発行を解消〕の計4回）は一致しない値であることを固定する。
    assert body["iterations"] == 2
    assert body["llm_calls"] == 4
    assert body["answer"]
    packet = body["evidence_packet"]
    assert packet["investigation_status"] == "sufficient"
    assert packet["evidence"], "Evidence Packet に evidence が1件も無い"
    ev = packet["evidence"][0]
    assert ev["source_path"] == _RESEARCH_REAL_DOC
    assert ev["evidence_id"] == "ev-1"


def test_ext_research_es_search_hit_without_line_number_normalizes_span_to_none(monkeypatch):
    """RV12 是正の固定: 行番号を持たない ES/RAG ヒット（`span=[None, None]`）が Evidence Packet の
    `source_span` へそのまま転記されると、応答モデル（`ExtEvidenceItem.source_span: list[int] |
    None`）の Pydantic 検証で 500 になる——API 境界（`ext_api._normalize_evidence_spans`）で
    `None` へ正規化し、200 で返すことを固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import agentic_search as A

    monkeypatch.setattr(A, "es_index", A.es_index)
    monkeypatch.setattr(A.es_index, "available", lambda: True)
    monkeypatch.setattr(A, "_graph_available", lambda: False)

    # `es_search`（rag_chunks 由来で行番号を持たない想定）が実在 doc への citation を
    # `span=[None, None]` で返すケースを再現する（`agentic_search.run_tool` の es_search 分岐が
    # `[h.get("line"), h.get("line")]` を組む際、`line` 欠落だとこの形になる）。
    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": []}, {_RESEARCH_REAL_DOC},
               [{"doc_id": _RESEARCH_REAL_DOC, "span": [None, None], "quote": "本文", "ext": ".md"}], [])

    monkeypatch.setattr(A, "run_tool", fake_run_tool)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "es_search", "arguments": '{"query":"x"}'}}]}}]},
        {"choices": [{"message": {"content": "見つかりました。"}, "finish_reason": "stop"}]},
    ]
    monkeypatch.setattr(A, "_post", lambda url, headers, body, timeout=90: seq.pop(0))

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchspan-{sfx}")
    _logout()

    r = _research({"world": "v1", "query": "x"}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    packet = r.json()["evidence_packet"]
    assert packet["evidence"], "Evidence Packet に evidence が1件も無い"
    for ev in packet["evidence"]:
        assert ev["source_span"] is None or all(isinstance(x, int) for x in ev["source_span"])


def test_ext_research_max_results_caps_evidence_count(monkeypatch):
    """`max_results` は Evidence Packet の `evidence` 件数上限として働く。"""
    if not _try_init():
        pytest.skip("DB down")
    _install_agentic_post(monkeypatch, list(_RESEARCH_SUCCESS_SEQ))

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchcap-{sfx}")
    _logout()

    r = _research({"world": "v1", "query": "税率改定の障害は？", "max_results": 1},
                  api_key=issued["key"])
    assert r.status_code == 200, r.text
    assert len(r.json()["evidence_packet"]["evidence"]) <= 1


def test_ext_research_audits_model_used_and_ev_ids(monkeypatch):
    """監査行に model_used/provider_used/iterations と ev-* の一覧が残る（§8.3/§8.4）。"""
    if not _try_init():
        pytest.skip("DB down")
    _install_agentic_post(monkeypatch, list(_RESEARCH_SUCCESS_SEQ))

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchaudit-{sfx}")
    _logout()
    rid = f"probe-research-audit-{sfx}"

    r = client.post(
        "/ext/v1/research", json={"world": "v1", "query": "税率改定の障害は？"},
        headers={"X-API-Key": issued["key"], "X-Request-Id": rid})
    assert r.status_code == 200, r.text

    with store._connect() as c:
        row = c.execute(
            "SELECT actor_user_id, resource_type, detail FROM audit_log WHERE request_id=%s", (rid,)
        ).fetchone()
    assert row is not None
    assert row["actor_user_id"] == f"ext:{issued['id']}"
    assert row["resource_type"] == "ext_research"
    assert row["detail"]["model_used"] == "qwen2.5"
    assert row["detail"]["provider_used"] == "ollama"
    assert row["detail"]["llm_calls"] == 4
    assert row["detail"]["ev_ids"] == ["ev-1"]


def test_ext_research_usage_metering_records_per_key(monkeypatch):
    """利用量の記録は常時ON（TOGGLE-RM・2026-09-03）: キー別 usage_events へ記録される
    （§8.3・kind='research'）。"""
    if not _try_init():
        pytest.skip("DB down")
    _install_agentic_post(monkeypatch, list(_RESEARCH_SUCCESS_SEQ))

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchusage-{sfx}")
    _logout()

    r = _research({"world": "v1", "query": "税率改定の障害は？"}, api_key=issued["key"])
    assert r.status_code == 200, r.text

    with store._connect() as c:
        rows = c.execute(
            "SELECT provider, model, calls FROM usage_events WHERE kind='research' AND user_id=%s",
            (f"ext:{issued['id']}",)
        ).fetchall()
    assert rows, "usage_events に research kind の行が記録されていない"
    assert rows[-1]["provider"] == "ollama"
    assert rows[-1]["model"] == "qwen2.5"
    assert rows[-1]["calls"] == 4   # llm_calls と一致する実測値（ツール2回+最終合成+帰属呼び出し1回）


def test_ext_research_pins_resolved_root(monkeypatch):
    """`research_service.run_research` は `ext_api._resolve_world_or_error`（preflight・ロック**前**の
    値）を使わず、共有ロック（`world_lock_shared`）を保持した状態で自前に `worlds.
    resolve_external_world` を（再）解決してから `worlds.pin_world_root` で固定することを配線レベルで
    固定する（TOCTOU 対策の実地確認。`tests/unit/test_worlds_pin_root.py` は pin 機構自体の契約、
    `tests/integration/test_world_lock_shared_semantics.py` はロックの相互排他そのものを検証——
    こちらは実際にその2つが呼ばれることを確認する）。"""
    if not _try_init():
        pytest.skip("DB down")
    _install_agentic_post(monkeypatch, list(_RESEARCH_SUCCESS_SEQ))
    from sherpa import worlds

    seen = {}
    real_pin = worlds.pin_world_root
    real_resolve = worlds.resolve_external_world

    def spy_pin(world_id, root):
        seen["world_id"] = world_id
        seen["root"] = root
        return real_pin(world_id, root)

    def spy_resolve(world_id, **kw):
        seen["resolve_called"] = seen.get("resolve_called", 0) + 1
        return real_resolve(world_id, **kw)

    monkeypatch.setattr(worlds, "pin_world_root", spy_pin)
    monkeypatch.setattr(worlds, "resolve_external_world", spy_resolve)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchpin-{sfx}")
    _logout()

    r = _research({"world": "v1", "query": "税率改定の障害は？"}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    assert seen["world_id"] == "v1"
    assert seen["root"] == worlds.world_dir("v1")
    assert seen["resolve_called"] >= 1, "研究実行経路が自前で world を再解決していない"


def test_ext_research_overall_timeout_returns_504(monkeypatch):
    """`timeout_s`（リクエスト全体のデッドライン）超過は 504（黙った空 200 にしない）。

    実時間を待たない: `research_service.threading.Timer` を即時発火する fake に差し替え、
    `stop_event` がループ冒頭（初回 `_post` の前）で立った状態を作る——`agentic_search.openai_style`
    は `stop_event` が立っていれば `final` を yield せず終了する契約（既存挙動・agentic_search.py
    docstring）ため、LLM 呼び出し自体が一度も発行されない decisive なタイムアウト再現になる。
    """
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import research_service

    class _ImmediateTimer:
        def __init__(self, interval, function):
            self._function = function

        def start(self):
            self._function()   # デッドライン到達を即座に模擬（実待機なし）

        def cancel(self):
            pass

    monkeypatch.setattr(research_service.threading, "Timer", _ImmediateTimer)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchtimeout-{sfx}")
    _logout()
    rid = f"probe-research-timeout-{sfx}"

    r = client.post(
        "/ext/v1/research", json={"world": "v1", "query": "税率改定の障害は？", "timeout_s": 5},
        headers={"X-API-Key": issued["key"], "X-Request-Id": rid})
    assert r.status_code == 504, r.text
    assert "制限時間" in r.json()["detail"]

    with store._connect() as c:
        row = c.execute(
            "SELECT reason FROM audit_log WHERE request_id=%s", (rid,)).fetchone()
    assert row is not None
    assert row["reason"] == "timeout"   # `_HTTP_OUTCOME_REASON[504]`


def test_ext_research_preflight_exceeding_deadline_returns_504_not_422(monkeypatch):
    """RV5 是正の固定: scope_paths 走査（preflight）自体がリクエスト全体の共有デッドラインを
    使い切るほど遅い場合、走査結果が「不明な範囲」であっても 422 ではなく 504 を返す——preflight
    と `run_research` が同じ絶対期限（ハンドラ入口で確定）を共有する契約
    （`ext_api.ext_research`/`research_service.run_research` docstring 参照）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import ext_api

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchpreflightdl-{sfx}")
    _logout()

    clock = {"t": 0.0}
    monkeypatch.setattr(ext_api.time, "monotonic", lambda: clock["t"])

    def _slow_invalid_scope(*a, **kw):
        clock["t"] = 1000.0   # scope 走査がデッドラインを丸ごと使い切ったことにする
        return False

    monkeypatch.setattr(ext_api.scope_mod, "valid_scope_paths", _slow_invalid_scope)

    r = _research({"world": "v1", "query": "x", "scope_paths": ["no-such-scope-xyz"],
                  "timeout_s": 5}, api_key=issued["key"])
    assert r.status_code == 504, r.text


def test_ext_research_preflight_elapsed_time_shares_absolute_deadline_with_run_research(monkeypatch):
    """RV6 是正の固定: `run_research` へは preflight 消費後の `timeout_s`（残り秒数）を再計算して
    渡すのではなく、ハンドラ入口で確定した絶対期限（`absolute_deadline`）そのものを渡す——
    別々に `time.monotonic()` を起点に変換し直すと、整数秒への切り上げ＋変換〜呼び出しに実際に
    かかる僅かな時間の両方が積み重なり、元の期限を最大約1秒超えてから 200 を返しうる（RV6・
    旧実装は `timeout_s=max(1, math.ceil(_remaining()))` を渡し直していた＝RV5 時点の教訓）。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import citations, ext_api, research_service

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchbudget-{sfx}")
    _logout()

    clock = {"t": 0.0}
    monkeypatch.setattr(ext_api.time, "monotonic", lambda: clock["t"])

    real_resolve = ext_api._resolve_world_or_error

    def _slow_resolve(world, **kw):
        clock["t"] = 12.0   # world 解決（preflight）に12秒かかったことにする
        return real_resolve(world)

    monkeypatch.setattr(ext_api, "_resolve_world_or_error", _slow_resolve)

    captured: dict = {}

    def _fake_run_research(**kw):
        captured.update(kw)
        return {"world": kw["world"], "query": kw["query"], "answer": "",
                "evidence_packet": citations.build_evidence_packet(
                    task_id="t", investigation_status="insufficient"),
                "model_used": "qwen2.5", "provider_used": "ollama",
                "iterations": 0, "llm_calls": 0, "used_ev_ids": []}

    monkeypatch.setattr(research_service, "run_research", _fake_run_research)

    r = _research({"world": "v1", "query": "x", "timeout_s": 30}, api_key=issued["key"])
    assert r.status_code == 200, r.text
    # 元の timeout_s（30）はそのまま渡る（メッセージ表示用・切り詰めない）。
    assert captured["timeout_s"] == 30
    # 絶対期限はハンドラ入口の時刻（0.0）+30 のまま——preflight が12秒使っても「期限」という
    # 固定点自体は動かない（動くのは run_research 内部が見る「残り」だけ）。
    assert captured["absolute_deadline"] == 30.0


def test_ext_research_slow_world_resolver_404_becomes_504_when_deadline_exceeded(monkeypatch):
    """RV6 是正の固定: world resolver（`worlds.resolve_external_world`）自体が長引いた末に
    「未登録」（404 相当）で失敗した場合でも、その時点で既にリクエスト全体のデッドラインを
    超えていれば 404 ではなく 504 を返す——`_resolve_world_or_error` 自身は期限を見ずに直接
    404/503 を送出するだけなので、呼び出し元（`ext_research`）が resolver の失敗を捕捉して
    判定する契約を固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import ext_api, worlds

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchslow404-{sfx}")
    _logout()

    clock = {"t": 0.0}
    monkeypatch.setattr(ext_api.time, "monotonic", lambda: clock["t"])

    def _slow_not_found(world_id, **kw):
        clock["t"] = 1000.0   # world 解決自体がデッドラインを丸ごと使い切ったことにする
        return worlds.ExternalWorldResolution("not_found", None)

    monkeypatch.setattr(ext_api.worlds, "resolve_external_world", _slow_not_found)

    r = _research({"world": "no-such-world-xyz", "query": "x", "timeout_s": 5},
                  api_key=issued["key"])
    assert r.status_code == 504, r.text


def test_ext_research_slow_world_resolver_503_becomes_504_when_deadline_exceeded(monkeypatch):
    """RV6 是正の固定: 上と同じ契約だが、resolver が registry 到達不可（`ExternalResolverError`
    →503相当）で失敗する場合。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import ext_api, worlds

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchslow503-{sfx}")
    _logout()

    clock = {"t": 0.0}
    monkeypatch.setattr(ext_api.time, "monotonic", lambda: clock["t"])

    def _slow_unreachable(world_id, **kw):
        clock["t"] = 1000.0
        raise worlds.ExternalResolverError("simulated registry unreachable")

    monkeypatch.setattr(ext_api.worlds, "resolve_external_world", _slow_unreachable)

    r = _research({"world": "v1", "query": "x", "timeout_s": 5}, api_key=issued["key"])
    assert r.status_code == 504, r.text


def test_ext_research_fast_world_resolver_404_stays_404_when_deadline_not_exceeded():
    """対照実験: resolver が期限内に速く失敗した場合は、これまでどおり素の 404 のまま
    （デッドライン優先の再分類は「期限を超えた場合だけ」に限定されることの固定）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchfast404-{sfx}")
    _logout()

    r = _research({"world": "no-such-world-xyz", "query": "x", "timeout_s": 30},
                  api_key=issued["key"])
    assert r.status_code == 404, r.text


def test_ext_research_scope_walk_deadline_exceeded_becomes_504(monkeypatch):
    """RV6 是正の固定: scope_paths の木走査自体（`scope_infer.safe_files` の `deadline` 引数）が
    デッドラインを超えて中断した場合（`scope_infer.ScopeWalkDeadlineExceeded`）、422/503 ではなく
    504 を返す。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import ext_api, scope_infer

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchscopewalkdl-{sfx}")
    _logout()

    def _boom_walk(*a, **kw):
        raise scope_infer.ScopeWalkDeadlineExceeded("simulated deadline mid-walk")

    monkeypatch.setattr(ext_api.scope_mod, "valid_scope_paths", _boom_walk)

    r = _research({"world": "v1", "query": "x", "scope_paths": ["4期"], "timeout_s": 5},
                  api_key=issued["key"])
    assert r.status_code == 504, r.text


def test_ext_research_records_partial_cost_and_audit_on_mid_failure(monkeypatch):
    """途中で LLM 呼び出しが失敗しても、それまでの llm_calls 分は metering に記録され、
    監査 detail にも解決済み model_used/provider_used/llm_calls が残る。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import agentic_search as A

    monkeypatch.setattr(A, "es_index", A.es_index)
    monkeypatch.setattr(A.es_index, "available", lambda: False)
    monkeypatch.setattr(A, "_graph_available", lambda: False)
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAXCALC"}'}}]}}]},
    ]

    def failing_post(url, headers, body, timeout=90):
        if seq:
            return seq.pop(0)
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(A, "_post", failing_post)

    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchfail-{sfx}")
    _logout()
    rid = f"probe-research-fail-{sfx}"

    r = client.post(
        "/ext/v1/research", json={"world": "v1", "query": "TAXCALCの仕様は？"},
        headers={"X-API-Key": issued["key"], "X-Request-Id": rid})
    assert r.status_code == 503, r.text

    with store._connect() as c:
        audit_row = c.execute(
            "SELECT detail FROM audit_log WHERE request_id=%s", (rid,)).fetchone()
        usage_rows = c.execute(
            "SELECT provider, model, calls FROM usage_events WHERE kind='research' AND user_id=%s",
            (f"ext:{issued['id']}",)).fetchall()
    assert audit_row is not None
    assert audit_row["detail"]["model_used"] == "qwen2.5"
    assert audit_row["detail"]["provider_used"] == "ollama"
    # 成功1回（ripgrep_search）+失敗1回分の送信——ただし `ConnectionError` は `OSError` の一種
    # として `agentic_search._retryable_post_error` の再試行対象に入るため、`_send` が同一
    # プロバイダ内で最大 `_POST_RETRY_ATTEMPTS`（2）回まで再試行する（初回+再試行2回=3回試行）。
    # 実際に発行を試みた回数を数える契約（`llm_calls`/usage_events 双方）のため、失敗した
    # 再試行分もすべて計上される＝1（成功）+3（失敗側の全試行）=4。
    assert audit_row["detail"]["llm_calls"] == 4
    assert usage_rows, "失敗時も usage_events へ記録される"
    assert usage_rows[-1]["calls"] == 4


def test_ext_research_openapi_subset_includes_research():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = _issue_key(f"researchopenapi-{sfx}")
    _logout()

    r = client.get("/ext/v1/openapi.json", headers={"X-API-Key": issued["key"]})
    assert r.status_code == 200, r.text
    assert "/ext/v1/research" in r.json()["paths"]
