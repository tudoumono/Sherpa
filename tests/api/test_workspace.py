"""個人 workspace API の契約（アップロード・一覧・grep・削除・DL・他人隔離・RAG 非索引）。要 Postgres。"""
from __future__ import annotations

import asyncio
import inspect
import io
import os
import pathlib
import re
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import sherpa.api as api_mod
import sherpa.es_index as esi
import sherpa.ingest.world_graph as wg
from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app

client = TestClient(app, raise_server_exceptions=True)
# conftest.py が SHERPA_USERS_DIR を先に確定させている（sherpa.api._USERS_DIR は import 時定数）。
_TMP_USERS = os.environ["SHERPA_USERS_DIR"]


@pytest.fixture(autouse=True)
def _db():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")
    yield
    client.post("/auth/logout")


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _mk_user(sfx: str, role: str = "user") -> tuple[str, str]:
    uid = f"ws{role[:1]}{sfx}"
    pw = f"pw-{uid}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(pw), role=role, status="active")
    register_test_uid(uid)
    return uid, pw


def _login(uid: str, pw: str) -> TestClient:
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, f"login failed: {r.text}"
    return client


def _new_login(sfx: str | None = None) -> tuple[str, str]:
    uid, pw = _mk_user(sfx or _sfx())
    _login(uid, pw)
    return uid, pw


def _upload(filename: str, content: bytes, ctype: str = "text/plain"):
    return client.post("/workspace/files", files={"file": (filename, io.BytesIO(content), ctype)})


def _upload_ok(filename: str, content: bytes) -> dict:
    r = _upload(filename, content)
    assert r.status_code == 200, r.text
    return r.json()


def _search_hits(q: str) -> list[dict]:
    r = client.get("/workspace/search", params={"q": q})
    assert r.status_code == 200, r.text
    return r.json()["hits"]


def _files_dir(uid: str) -> pathlib.Path:
    return pathlib.Path(_TMP_USERS) / uid / "workspace" / "files"


def test_provisioning_on_user_create():
    """アカウント作成時に workspace の outputs/tmp/files が作られる。"""
    sfx = _sfx()
    adm_uid, adm_pw = _mk_user(sfx, role="admin")
    _login(adm_uid, adm_pw)
    new_uid = f"newuser{sfx}"
    r = client.post("/admin/users", json={"uid": new_uid, "display_name": "Test User",
                                          "role": "user", "password": "testpass123"})
    assert r.status_code == 200, r.text
    register_test_uid(new_uid)
    ws_base = pathlib.Path(_TMP_USERS) / new_uid / "workspace"
    assert ws_base.is_dir()
    for sub in ("outputs", "tmp", "files"):
        assert (ws_base / sub).is_dir(), f"{sub}/ not created"


def test_upload_list_search_delete():
    sfx = _sfx()
    _new_login(sfx)
    data = _upload_ok(f"taxconfig{sfx}.txt", "TAX_RATE=0.10\n# shohizei-ritsu no settei\nvalue=100".encode())
    assert data["ok"] is True
    assert "rel_path" in data
    fid = data["id"]

    r = client.get("/workspace/files")
    assert r.status_code == 200, r.text
    assert any(f["id"] == fid for f in r.json()["files"])

    r = client.get("/workspace/search", params={"q": "TAX_RATE"})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "個人ファイル内ヒット"
    assert any("TAX_RATE" in h["text"] for h in r.json()["hits"])

    r = client.delete(f"/workspace/files/{fid}")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert not any(f["id"] == fid for f in client.get("/workspace/files").json()["files"])


def test_cross_user_isolation():
    """A のファイルは B から list/delete/grep/download できない（404）。"""
    sfx = _sfx()
    uid_a, pw_a = _mk_user(sfx + "a")
    uid_b, pw_b = _mk_user(sfx + "b")
    _login(uid_a, pw_a)
    fid_a = _upload_ok(f"private_a_{sfx}.txt", b"A's secret data")["id"]
    client.post("/auth/logout")

    _login(uid_b, pw_b)
    r = client.get("/workspace/files")
    assert r.status_code == 200
    assert not any(f["id"] == fid_a for f in r.json()["files"])
    r = client.delete(f"/workspace/files/{fid_a}")
    assert r.status_code == 404, r.text
    assert not _search_hits("A's secret")
    r = client.get(f"/workspace/files/{fid_a}/download")
    assert r.status_code == 404, r.text


def test_path_traversal_rejected():
    """パストラバーサルのファイル名は拒否、受理されてもパス成分は残らない。"""
    _new_login()
    for bad_name in ("../evil.txt", "../../etc/passwd", "./../secret.txt"):
        r = _upload(bad_name, b"evil")
        if r.status_code == 200:
            rel = r.json().get("rel_path", "")
            assert "/" not in rel and ".." not in rel, r.json()
        else:
            assert r.status_code in (422, 400), f"{bad_name}: {r.status_code}"


def test_invariant_workspace_not_documents_nor_rag_indexed():
    """workspace は /documents 台帳に出ず、ES/グラフ索引・workspace_search からも RAG 側を参照しない。"""
    sfx = _sfx()
    _new_login(sfx)
    rel_path = _upload_ok(f"inv_check_{sfx}.txt", b"invariant test content")["rel_path"]
    assert not any(d["name"] == rel_path for d in store.list_documents("v1"))

    for mod in (esi, wg):
        assert "personal_workspace_files" not in inspect.getsource(mod), mod.__name__

    code_only = "\n".join(ln for ln in inspect.getsource(api_mod.workspace_search).splitlines()
                          if not ln.lstrip().startswith("#"))
    assert not re.search(r"\b_TEXT_EXT\b", code_only)
    assert "es_index" not in code_only
    assert "world_graph" not in code_only


def test_audit_rows_for_upload_download_delete():
    sfx = _sfx()
    uid, _ = _new_login(sfx)
    fid = _upload_ok(f"audit_test_{sfx}.txt", b"audit test content")["id"]
    assert store.list_audit(action="workspace.file_uploaded", actor=uid, limit=10)
    assert client.get(f"/workspace/files/{fid}/download").status_code == 200
    assert store.list_audit(action="workspace.file_downloaded", actor=uid, limit=10)
    assert client.delete(f"/workspace/files/{fid}").status_code == 200
    assert store.list_audit(action="workspace.file_deleted", actor=uid, limit=10)


def test_upload_does_not_block_event_loop(monkeypatch):
    """アップロード内の同期 I/O が threadpool 経由で、単一 worker の event loop を塞がない。
    1 つの loop 上（httpx.ASGITransport）で遅延 2 秒のアップロードと軽量 GET を並行させ、GET が
    巻き込まれないこと。経過は実験開始（アップロード発行前）を起点に測る（loop が塞がれると
    sleep の再開自体が遅れるため、後から取り直した時刻では検出できない）。"""
    sfx = _sfx()
    _new_login(sfx)
    cookies = dict(client.cookies)
    orig_record = store.record_workspace_file

    def _slow_record(*a, **kw):
        time.sleep(2.0)
        return orig_record(*a, **kw)

    monkeypatch.setattr(store, "record_workspace_file", _slow_record)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies=cookies) as ac:
            t_start = time.monotonic()
            upload_task = asyncio.create_task(ac.post(
                "/workspace/files",
                files={"file": (f"slow_{sfx}.txt", io.BytesIO(b"hello"), "text/plain")}))
            await asyncio.sleep(0.2)
            fast_resp = await ac.get("/workspace/files")
            fast_total = time.monotonic() - t_start
            return await upload_task, fast_resp, fast_total

    upload_resp, fast_resp, fast_total = asyncio.run(_run())
    assert upload_resp.status_code == 200, upload_resp.text
    assert fast_resp.status_code == 200, fast_resp.text
    assert fast_total < 1.2, f"並行リクエストがアップロードの遅延に巻き込まれた: {fast_total:.2f}s"


def test_compat_mode_no_regression(auth_disabled):
    r = client.get("/workspace/files")
    assert r.status_code in (200, 503), f"{r.status_code} {r.text}"


def test_upload_size_limit():
    _new_login()
    r = _upload("bigfile.txt", b"x" * (10 * 1024 * 1024 + 1))
    assert r.status_code == 413, r.status_code


@pytest.mark.parametrize("bad_ext", [".exe", ".dll", ".zip", ".pdf"])
def test_disallowed_extension(bad_ext):
    _new_login()
    r = _upload(f"badfile{bad_ext}", b"content", "application/octet-stream")
    assert r.status_code == 422, r.status_code


def test_ledger_based_search_excludes_fs_residue():
    """台帳削除済みファイルは物理ファイルが残っていても grep にヒットしない。"""
    sfx = _sfx()
    uid, _ = _new_login(sfx)
    marker = f"W1_RESIDUE_MARKER_{sfx}"
    content = f"{marker}=secret".encode()
    data = _upload_ok(f"w1test{sfx}.txt", content)
    assert any(marker in h["text"] for h in _search_hits(marker))

    assert client.delete(f"/workspace/files/{data['id']}").status_code == 200
    (_files_dir(uid) / data["rel_path"]).write_bytes(content)   # 削除失敗を模した FS 残骸
    assert not any(marker in h["text"] for h in _search_hits(marker))


def test_allowed_exts_are_the_searchable_exts():
    from sherpa.api import _WORKSPACE_ALLOWED_EXT, _WORKSPACE_SEARCHABLE_EXT
    assert _WORKSPACE_SEARCHABLE_EXT is _WORKSPACE_ALLOWED_EXT
    assert _WORKSPACE_ALLOWED_EXT == _WORKSPACE_SEARCHABLE_EXT


@pytest.mark.parametrize("ext, tmpl", [
    ("csv", "CSV_MARKER_{s},value\n1,2"),
    ("json", '{{"JSON_MARKER_{s}": true}}'),
    ("yaml", "YAML_MARKER_{s}: enabled"),
])
def test_csv_json_yaml_are_uploadable_and_searchable(ext, tmpl):
    sfx = _sfx()
    _new_login(sfx)
    _upload_ok(f"data{sfx}.{ext}", tmpl.format(s=sfx).encode())
    marker = f"{ext.upper()}_MARKER_{sfx}"
    assert any(marker in h["text"] for h in _search_hits(marker))


def test_download_roundtrip_then_404_after_delete_and_for_unknown_id():
    sfx = _sfx()
    _new_login(sfx)
    content = f"DOWNLOAD_MARKER_{sfx}".encode()
    data = _upload_ok(f"dl_test_{sfx}.txt", content)
    r = client.get(f"/workspace/files/{data['id']}/download")
    assert r.status_code == 200, r.text
    assert r.content == content
    assert data["rel_path"] in (r.headers.get("content-disposition") or "")

    assert client.delete(f"/workspace/files/{data['id']}").status_code == 200
    assert client.get(f"/workspace/files/{data['id']}/download").status_code == 404
    assert client.get("/workspace/files/999999999/download").status_code == 404


def test_download_symlinked_ledger_path_rejected():
    """台帳 rel_path の実体が symlink なら fail-closed で 404（resolve 後ではなく未解決パスで検査する穴の回帰）。"""
    sfx = _sfx()
    uid, _ = _new_login(sfx)
    r1 = _upload_ok(f"target_{sfx}.txt", b"secret-target")
    r2 = _upload_ok(f"link_{sfx}.txt", b"placeholder")
    link_path = _files_dir(uid) / r2["rel_path"]
    link_path.unlink()
    link_path.symlink_to(_files_dir(uid) / r1["rel_path"])
    r = client.get(f"/workspace/files/{r2['id']}/download")
    assert r.status_code == 404, r.status_code


def test_download_symlinked_files_dir_rejected():
    """files/ ディレクトリ自体が symlink のときも fail-closed で 404。"""
    sfx = _sfx()
    uid, _ = _new_login(sfx)
    fid = _upload_ok(f"parent_{sfx}.txt", b"parent-symlink-check")["id"]
    ws = pathlib.Path(_TMP_USERS) / uid / "workspace"
    real_files, moved = ws / "files", ws / "files_real"
    real_files.rename(moved)
    (ws / "files").symlink_to(moved)
    try:
        resp = client.get(f"/workspace/files/{fid}/download")
        assert resp.status_code == 404, resp.status_code
    finally:
        (ws / "files").unlink()
        moved.rename(real_files)
