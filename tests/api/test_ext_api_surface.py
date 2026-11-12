"""外部連携 API（/ext/v1）の全経路について、状態コード・本文の形・ヘッダ・監査の記録を1か所で固定する比べる試験。

`sherpa/ext_api.py` を `sherpa/ext/` へ分ける前後で、外へ見える振る舞いが変わらないことの回帰線。
個々の経路の細かい契約は test_ext_api.py・test_ext_codex_jobs.py が持つ。ここは「各経路が1回ずつ通って
何を返し何を監査に残すか」だけを表にして固める。要 Postgres（DB 不可は skip）。
"""
from __future__ import annotations

import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, codex_jobs_worker, ext_api, simple_chat, store
from sherpa.api import app
from sherpa.parts.read import fused_search

client = TestClient(app, raise_server_exceptions=True)

_DOC = "4期/01_標準/消費税法.md"


@pytest.fixture(autouse=True)
def _db():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


@pytest.fixture(autouse=True)
def _audit_writer_running():
    ext_api._audit_writer.start()
    yield


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _issue_key(**body) -> dict:
    uid = f"surf{_sfx()}"
    pw = f"pw-{uid}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(pw), role="admin", status="active")
    register_test_uid(uid)
    assert client.post("/auth/login", json={"username": uid, "password": pw}).status_code == 200
    try:
        r = client.post("/ext/v1/admin/keys", json={"label": f"surf-{_sfx()}", **body})
        assert r.status_code == 200, r.text
        return r.json()
    finally:
        client.post("/auth/logout")


def _docx() -> bytes:
    xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           "<w:body><w:p><w:r><w:t>本文ABC</w:t></w:r></w:p></w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


_HEADERS = ("retry-after", "content-disposition", "x-content-type-options", "cache-control")


def _observe(r, rid: str) -> tuple:
    """応答（状態コード・Content-Type・本文の最上位キー・選んだヘッダ）と、request_id で引いた監査行1件
    （action・resource_type・outcome・reason・severity・http_status・business_outcome と detail のキー）を1行にする。"""
    deadline = time.monotonic() + 5
    rows = []
    while time.monotonic() < deadline:
        with store._connect() as c:
            rows = c.execute(
                "SELECT action, resource_type, resource_id, outcome, reason, severity, detail "
                "FROM audit_log WHERE request_id=%s ORDER BY id", (rid,)).fetchall()
        if rows:
            break
        time.sleep(0.05)
    assert len(rows) == 1, f"監査行はちょうど1件のはず（実際 {len(rows)} 件）"
    a = rows[0]
    ctype = r.headers.get("content-type", "")
    body = None
    if ctype.startswith("application/json"):
        j = r.json()
        body = sorted(j) if isinstance(j, dict) else "list"
    d = a["detail"]
    assert r.headers.get("x-request-id") == rid
    audit = " ".join(str(x) for x in (a["action"], a["resource_type"], a["outcome"], a["reason"],
                                      a["severity"], d.get("http_status"), d.get("business_outcome")))
    return (r.status_code, ctype, body, {h: r.headers[h] for h in _HEADERS if h in r.headers},
            audit, ",".join(sorted(d)))


def _stub_answer(monkeypatch):
    monkeypatch.setattr(simple_chat, "answer", lambda **kw: {
        "answer": "確認しました", "sources": [{"doc_id": _DOC}], "unconfirmed": False,
        "tool_calls": 1, "elapsed_ms": 3})


def _stub_search(monkeypatch):
    monkeypatch.setattr(fused_search, "search", lambda *a, **kw: {
        "hits": [], "engines_used": ["keyword"], "degraded": []})


def _run_cases(monkeypatch) -> dict:
    _stub_answer(monkeypatch)
    _stub_search(monkeypatch)
    monkeypatch.setattr(codex_jobs_worker, "check_available", lambda: None)
    key = _issue_key()["key"]
    scoped = _issue_key(allowed_worlds=["v1"])["key"]
    sfx = _sfx()
    out: dict[str, tuple] = {}

    def call(name, method, url, key_=key, **kw):
        rid = f"surf-{name}-{sfx}"
        headers = {"X-Request-Id": rid}
        if key_:
            headers["X-API-Key"] = key_
        r = client.request(method, url, headers=headers, **kw)
        out[name] = _observe(r, rid)
        return r

    files = {"file": ("a.docx", io.BytesIO(_docx()), "application/octet-stream")}
    call("convert_ok", "POST", "/ext/v1/convert", files=files)
    call("convert_no_key", "POST", "/ext/v1/convert", key_=None,
         files={"file": ("a.docx", io.BytesIO(_docx()), "application/octet-stream")})
    call("convert_bad_ext", "POST", "/ext/v1/convert",
         files={"file": ("a.txt", io.BytesIO(b"x"), "application/octet-stream")})

    call("search_ok", "POST", "/ext/v1/search", json={"world": "v1", "query": "税"})
    call("search_no_key", "POST", "/ext/v1/search", key_=None, json={"world": "v1", "query": "税"})
    call("search_out_of_scope", "POST", "/ext/v1/search", key_=scoped,
         json={"world": "other-world-xyz", "query": "税"})
    call("search_unknown_scope_path", "POST", "/ext/v1/search",
         json={"world": "v1", "query": "税", "scope_paths": ["no/such/dir"]})
    call("search_validation", "POST", "/ext/v1/search", json={"world": "v1"})

    call("answer_ok", "POST", "/ext/v1/answer", json={"world": "v1", "query": "税"})
    call("answer_no_key", "POST", "/ext/v1/answer", key_=None, json={"world": "v1", "query": "税"})
    call("answer_out_of_scope", "POST", "/ext/v1/answer", key_=scoped,
         json={"world": "other-world-xyz", "query": "税"})
    call("answer_extra_field", "POST", "/ext/v1/answer", json={"world": "v1", "query": "税", "model": "x"})

    call("capabilities_ok", "GET", "/ext/v1/capabilities")
    call("capabilities_scoped", "GET", "/ext/v1/capabilities", key_=scoped)
    call("capabilities_no_key", "GET", "/ext/v1/capabilities", key_=None)

    call("doc_ok", "GET", "/ext/v1/doc", params={"world": "v1", "path": _DOC})
    call("doc_no_key", "GET", "/ext/v1/doc", key_=None, params={"world": "v1", "path": _DOC})
    call("doc_out_of_scope", "GET", "/ext/v1/doc", key_=scoped,
         params={"world": "other-world-xyz", "path": _DOC})
    call("doc_not_found", "GET", "/ext/v1/doc", params={"world": "v1", "path": "no/such/file.md"})

    call("openapi_ok", "GET", "/ext/v1/openapi.json")
    call("openapi_no_key", "GET", "/ext/v1/openapi.json", key_=None)

    r = call("job_submit_ok", "POST", "/ext/v1/codex/jobs", json={"world": "v1", "query": "税"})
    job_id = r.json()["job_id"]
    call("job_submit_no_key", "POST", "/ext/v1/codex/jobs", key_=None, json={"world": "v1", "query": "税"})
    call("job_submit_out_of_scope", "POST", "/ext/v1/codex/jobs", key_=scoped,
         json={"world": "other-world-xyz", "query": "税"})
    call("job_status_queued", "GET", f"/ext/v1/codex/jobs/{job_id}")
    call("job_status_not_found", "GET", "/ext/v1/codex/jobs/job-no-such")
    call("job_status_other_key", "GET", f"/ext/v1/codex/jobs/{job_id}", key_=scoped)
    call("job_result_queued_409", "GET", f"/ext/v1/codex/jobs/{job_id}/result")
    call("job_result_not_found", "GET", "/ext/v1/codex/jobs/job-no-such/result")
    call("job_cancel_queued", "POST", f"/ext/v1/codex/jobs/{job_id}/cancel")
    call("job_cancel_again", "POST", f"/ext/v1/codex/jobs/{job_id}/cancel")
    call("job_cancel_not_found", "POST", "/ext/v1/codex/jobs/job-no-such/cancel")
    call("job_result_cancelled_409", "GET", f"/ext/v1/codex/jobs/{job_id}/result")
    return out


def test_route_table_is_stable():
    """ルーターの経路（メソッドとパス）とその並び。OpenAPI の並びに効く。"""
    table = [(sorted(r.methods), r.path) for r in ext_api.router.routes]
    assert table == EXPECTED_ROUTES


def test_every_route_status_headers_body_and_audit_are_stable(monkeypatch):
    observed = _run_cases(monkeypatch)
    assert observed == EXPECTED_CASES


EXPECTED_ROUTES = [
    (['POST'], '/ext/v1/convert'),
    (['POST'], '/ext/v1/search'),
    (['POST'], '/ext/v1/answer'),
    (['POST'], '/ext/v1/codex/jobs'),
    (['GET'], '/ext/v1/codex/jobs/{job_id}'),
    (['GET'], '/ext/v1/codex/jobs/{job_id}/result'),
    (['POST'], '/ext/v1/codex/jobs/{job_id}/cancel'),
    (['GET'], '/ext/v1/capabilities'),
    (['GET'], '/ext/v1/doc'),
    (['GET'], '/ext/v1/openapi.json'),
]

# 名前: (状態, Content-Type, 本文の最上位キー, 選んだヘッダ, 監査の要約, 監査 detail のキー)
EXPECTED_CASES = {
    'answer_extra_field':
        (422, 'application/json', ['detail'], {}, 'ext_api.answer ext_answer error validation_error info 422 failed', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'answer_no_key':
        (401, 'application/json', ['detail'], {}, 'ext_api.auth_failed api_key deny missing_or_malformed warning 401 denied', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'answer_ok':
        (200, 'application/json', ['answer', 'elapsed_ms', 'sources', 'tool_calls', 'unconfirmed'], {}, 'ext_api.answer ext_answer success None info 200 ok', 'business_outcome,duration_ms,http_status,method,path,prefix,query,result_count,tool_calls,unconfirmed,world'),
    'answer_out_of_scope':
        (403, 'application/json', ['detail'], {}, 'ext_api.auth_failed api_key deny world_not_allowed warning 403 denied', 'business_outcome,duration_ms,http_status,method,path,prefix,query,result_count,world'),
    'capabilities_no_key':
        (401, 'application/json', ['detail'], {}, 'ext_api.auth_failed api_key deny missing_or_malformed warning 401 denied', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'capabilities_ok':
        (200, 'application/json', ['worlds'], {}, 'ext_api.capabilities ext_capabilities success None info 200 ok', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'capabilities_scoped':
        (200, 'application/json', ['worlds'], {}, 'ext_api.capabilities ext_capabilities success None info 200 ok', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'convert_bad_ext':
        (422, 'application/json', ['detail'], {}, 'ext_api.convert ext_convert error validation_error info 422 failed', 'business_outcome,duration_ms,ext,filename,http_status,method,path,result_count'),
    'convert_no_key':
        (401, 'application/json', ['detail'], {}, 'ext_api.auth_failed api_key deny missing_or_malformed warning 401 denied', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'convert_ok':
        (200, 'application/json', ['filename', 'md', 'method', 'size_bytes', 'unsupported'], {}, 'ext_api.convert ext_convert success None info 200 ok', 'business_outcome,duration_ms,ext,filename,http_status,method,ok,path,result_count,size_bytes'),
    'doc_no_key':
        (401, 'application/json', ['detail'], {}, 'ext_api.auth_failed api_key deny missing_or_malformed warning 401 denied', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'doc_not_found':
        (404, 'application/json', ['detail'], {}, 'ext_api.doc ext_doc error not_found info 404 failed', 'business_outcome,duration_ms,http_status,method,path,result_count,world'),
    'doc_ok':
        (200, 'text/markdown; charset=utf-8', None, {'cache-control': 'private, no-store', 'content-disposition': "attachment; filename*=utf-8''%E6%B6%88%E8%B2%BB%E7%A8%8E%E6%B3%95.md", 'x-content-type-options': 'nosniff'}, 'ext_api.doc ext_doc success None info 200 ok', 'business_outcome,duration_ms,http_status,method,path,result_count,size_bytes,world'),
    'doc_out_of_scope':
        (403, 'application/json', ['detail'], {}, 'ext_api.auth_failed api_key deny world_not_allowed warning 403 denied', 'business_outcome,duration_ms,http_status,method,path,result_count,world'),
    'job_cancel_again':
        (200, 'application/json', ['status'], {}, 'ext_api.codex_job_cancel ext_codex_job success None info 200 ok', 'business_outcome,duration_ms,http_status,method,path,result_count,status_after,status_before,world'),
    'job_cancel_not_found':
        (404, 'application/json', ['detail'], {}, 'ext_api.codex_job_cancel ext_codex_job error not_found info 404 failed', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'job_cancel_queued':
        (200, 'application/json', ['status'], {}, 'ext_api.codex_job_cancel ext_codex_job success None info 200 ok', 'business_outcome,duration_ms,http_status,method,path,result_count,status_before,world'),
    'job_result_cancelled_409':
        (409, 'application/json', ['status'], {}, 'ext_api.codex_job_result ext_codex_job error error info 409 failed', 'business_outcome,duration_ms,http_status,method,path,result_count,status,world'),
    'job_result_not_found':
        (404, 'application/json', ['detail'], {}, 'ext_api.codex_job_result ext_codex_job error not_found info 404 failed', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'job_result_queued_409':
        (409, 'application/json', ['status'], {}, 'ext_api.codex_job_result ext_codex_job error error info 409 failed', 'business_outcome,duration_ms,http_status,method,path,result_count,status,world'),
    'job_status_not_found':
        (404, 'application/json', ['detail'], {}, 'ext_api.codex_job_status ext_codex_job error not_found info 404 failed', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'job_status_other_key':
        (404, 'application/json', ['detail'], {}, 'ext_api.codex_job_status ext_codex_job error not_found info 404 failed', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'job_status_queued':
        (200, 'application/json', ['created_at', 'job_id', 'status'], {'retry-after': '30'}, 'ext_api.codex_job_status ext_codex_job success None info 200 ok', 'business_outcome,duration_ms,http_status,method,path,result_count,status,world'),
    'job_submit_no_key':
        (401, 'application/json', ['detail'], {}, 'ext_api.auth_failed api_key deny missing_or_malformed warning 401 denied', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'job_submit_ok':
        (202, 'application/json', ['job_id', 'status'], {}, 'ext_api.codex_job_submit ext_codex_job success None info 202 ok', 'business_outcome,depth,duration_ms,http_status,method,path,prefix,query,result_count,world'),
    'job_submit_out_of_scope':
        (403, 'application/json', ['detail'], {}, 'ext_api.auth_failed api_key deny world_not_allowed warning 403 denied', 'business_outcome,depth,duration_ms,http_status,method,path,prefix,query,result_count,world'),
    'openapi_no_key':
        (401, 'application/json', ['detail'], {}, 'ext_api.auth_failed api_key deny missing_or_malformed warning 401 denied', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'openapi_ok':
        (200, 'application/json', ['components', 'info', 'openapi', 'paths', 'security'], {}, 'ext_api.openapi ext_openapi success None info 200 ok', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'search_no_key':
        (401, 'application/json', ['detail'], {}, 'ext_api.auth_failed api_key deny missing_or_malformed warning 401 denied', 'business_outcome,duration_ms,http_status,method,path,result_count'),
    'search_ok':
        (200, 'application/json', ['degraded', 'engines_used', 'hits', 'query', 'world'], {}, 'ext_api.search ext_search success None info 200 ok', 'business_outcome,degraded,depth,duration_ms,engines,http_status,k,layer,method,path,prefix,query,result_count,world'),
    'search_out_of_scope':
        (403, 'application/json', ['detail'], {}, 'ext_api.auth_failed api_key deny world_not_allowed warning 403 denied', 'business_outcome,depth,duration_ms,engines,http_status,k,layer,method,path,prefix,query,result_count,world'),
    'search_unknown_scope_path':
        (422, 'application/json', ['detail'], {}, 'ext_api.search ext_search error validation_error info 422 failed', 'business_outcome,depth,duration_ms,engines,http_status,k,layer,method,path,prefix,query,result_count,world'),
    'search_validation':
        (422, 'application/json', ['detail'], {}, 'ext_api.search ext_search error validation_error info 422 failed', 'business_outcome,duration_ms,http_status,method,path,result_count'),
}
