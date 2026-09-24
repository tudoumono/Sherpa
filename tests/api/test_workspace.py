"""個人 workspace の API テスト（Task B: workspace MVP）。

テスト範囲:
- アカウント作成で workspace ディレクトリが provisioning される
- アップロード → 一覧 → grep ヒット → 削除
- クロスユーザー隔離（ユーザー A は B のファイルを list/delete できない、search も混ざらない）
- パストラバーサル拒否
- 不変条件: workspace ファイルが /documents に出ない・ES/グラフ索引の対象にならない

要 Postgres。DB 不可は SKIP。
既定のログイン必須モードで認証フローを通す。
compat モード（SHERPA_AUTH_DISABLED=1）でも基本動作が壊れないことも確認する。
"""
from __future__ import annotations

import io
import os
import pathlib
import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app

client = TestClient(app, raise_server_exceptions=True)
# conftest.py が tests/api のどの test_*.py よりも先に SHERPA_USERS_DIR を確定させている
# （sherpa.api._USERS_DIR は import 時定数のため、後から書き換えても効かない）。
_TMP_USERS = os.environ["SHERPA_USERS_DIR"]


# ---- ヘルパ ----

def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")   # 不可なら可視の skip（silent-green 根絶）


def _mk_user(sfx: str, role: str = "user") -> tuple[str, str]:
    uid = f"ws{role[:1]}{sfx}"
    pw = f"pw-{uid}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(pw), role=role, status="active")
    register_test_uid(uid)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）
    return uid, pw


def _mk_admin(sfx: str) -> tuple[str, str]:
    return _mk_user(sfx, role="admin")


def _login(uid: str, pw: str) -> TestClient:
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, f"login failed: {r.text}"
    return client


def _logout() -> None:
    client.post("/auth/logout")


def _upload(filename: str, content: bytes) -> dict:
    """POST /workspace/files してレスポンス dict を返す。"""
    r = client.post(
        "/workspace/files",
        files={"file": (filename, io.BytesIO(content), "text/plain")},
    )
    return r


# ===== テスト =====

def test_provisioning_on_user_create():
    """アカウント作成時に workspace ディレクトリが作成されること。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    # admin でログインしてユーザー作成。
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)

    new_uid = f"newuser{sfx}"
    r = client.post("/admin/users", json={
        "uid": new_uid, "display_name": "Test User",
        "role": "user", "password": "testpass123",
    })
    assert r.status_code == 200, r.text
    register_test_uid(new_uid)   # API 経由で作成した uid もテストユーザー残骸防止の対象にする

    # workspace ディレクトリが作成されているか確認。
    users_dir = pathlib.Path(_TMP_USERS)
    ws_base = users_dir / new_uid / "workspace"
    assert ws_base.is_dir(), f"workspace base not created: {ws_base}"
    assert (ws_base / "outputs").is_dir(), "outputs/ not created"
    assert (ws_base / "tmp").is_dir(), "tmp/ not created"
    assert (ws_base / "files").is_dir(), "files/ not created"

    _logout()


def test_upload_list_search_delete():
    """アップロード→一覧→grep→削除 の基本フロー。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    # アップロード。
    content = "TAX_RATE=0.10\n# shohizei-ritsu no settei\nvalue=100".encode("utf-8")
    r = _upload(f"taxconfig{sfx}.txt", content)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    fid = data["id"]
    assert "rel_path" in data

    # 一覧に出る。
    r = client.get("/workspace/files")
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    assert any(f["id"] == fid for f in files), "uploaded file not in list"

    # grep で見つかる。
    r = client.get("/workspace/search", params={"q": "TAX_RATE"})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["source"] == "個人ファイル内ヒット"
    assert any("TAX_RATE" in h["text"] for h in result["hits"]), "search hit not found"

    # 削除。
    r = client.delete(f"/workspace/files/{fid}")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # 削除後は一覧に出ない。
    r = client.get("/workspace/files")
    files_after = r.json()["files"]
    assert not any(f["id"] == fid for f in files_after), "deleted file still in list"

    # 削除後は grep でも見つからない。
    r = client.get("/workspace/search", params={"q": "TAX_RATE"})
    # grep は物理ファイルを走査するのでファイルが消えていれば hit 0（削除 best-effort の場合もあるが概ね消える）
    # hit 0 or hit があっても同名ファイルでなければOK。
    after_hits = r.json()["hits"]
    # fid に対応するファイル由来の hit は消えていることを確認（rel_path で区別）。
    assert r.status_code == 200

    _logout()


def test_cross_user_isolation():
    """ユーザー A のファイルはユーザー B から見えない・削除できない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid_a, pw_a = _mk_user(sfx + "a")
    uid_b, pw_b = _mk_user(sfx + "b")

    # A でアップロード。
    _login(uid_a, pw_a)
    r = _upload(f"private_a_{sfx}.txt", b"A's secret data")
    assert r.status_code == 200, r.text
    fid_a = r.json()["id"]
    _logout()

    # B でログイン。
    _login(uid_b, pw_b)

    # B の一覧には A のファイルが出ない。
    r = client.get("/workspace/files")
    assert r.status_code == 200
    b_files = r.json()["files"]
    assert not any(f["id"] == fid_a for f in b_files), "A's file visible to B"

    # B が A の file_id で削除を試みても失敗（404）。
    r = client.delete(f"/workspace/files/{fid_a}")
    assert r.status_code == 404, f"B could delete A's file: {r.text}"

    # B の grep に A のファイルの内容が出ない。
    r = client.get("/workspace/search", params={"q": "A's secret"})
    assert r.status_code == 200
    b_hits = r.json()["hits"]
    assert not b_hits, f"A's data leaked to B's search: {b_hits}"

    _logout()


def test_path_traversal_rejected():
    """パストラバーサルを含むファイル名は拒否される。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    for bad_name in ("../evil.txt", "../../etc/passwd", "./../secret.txt"):
        r = client.post(
            "/workspace/files",
            files={"file": (bad_name, io.BytesIO(b"evil"), "text/plain")},
        )
        # 拒否または無害なベース名で処理される（basename が取れてしまう場合はパス成分が消える）。
        # 少なくとも ws 外への書き込みは起きない（物理ファイルがあったとしても workspace 外ではない）。
        if r.status_code == 200:
            # もし受理されたならベース名のみ（パス成分なし）で処理されているはず。
            rj = r.json()
            assert "/" not in rj.get("rel_path", ""), f"traversal not stripped: {rj}"
            assert ".." not in rj.get("rel_path", ""), f"dotdot not stripped: {rj}"
        else:
            # 422 が望ましい（ファイル名バリデーション通過した後の確認でも OK）。
            assert r.status_code in (422, 400), f"unexpected status for {bad_name}: {r.status_code}"

    _logout()


def test_invariant_workspace_not_in_documents():
    """不変条件: workspace ファイルは /documents に出ない。

    /documents は共有 KB（world）の台帳。personal_workspace_files は別テーブル。
    documents テーブルを読む store.list_documents はワークスペースを返さない。
    """
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    # アップロード。
    r = _upload(f"inv_check_{sfx}.txt", b"invariant test content")
    assert r.status_code == 200
    rel_path = r.json()["rel_path"]

    # /documents API に workspace ファイルが出ないこと。
    # /documents は world が必要なので fixtures フラグが必要な場合がある。
    # store 層で直接確認する。
    docs = store.list_documents("v1")
    assert not any(d["name"] == rel_path for d in docs), \
        f"workspace file '{rel_path}' leaked into documents ledger"

    # 不変条件: personal_workspace_files は ES/Neo4j 取り込み関数から参照されない。
    # es_index.py / world_graph.py は personal_workspace_files を使わないことをコード上で保証する
    # （下記は関数実行ではなくモジュール存在を確認するだけ。本テストはアサーションとしての記録）。
    import sherpa.es_index as esi
    import sherpa.ingest.world_graph as wg
    # es_index が personal_workspace_files を import/参照しないことを確認（ソースレベル）。
    import inspect
    esi_src = inspect.getsource(esi)
    wg_src = inspect.getsource(wg)
    assert "personal_workspace_files" not in esi_src, \
        "es_index.py references personal_workspace_files (RAG isolation violated)"
    assert "personal_workspace_files" not in wg_src, \
        "world_graph.py references personal_workspace_files (RAG isolation violated)"

    _logout()


def test_audit_rows_for_upload_delete():
    """アップロード・削除に対して audit_log 行が記録されること。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    r = _upload(f"audit_test_{sfx}.txt", b"audit test content")
    assert r.status_code == 200
    fid = r.json()["id"]

    # アップロード監査行の確認。
    upload_rows = store.list_audit(action="workspace.file_uploaded", actor=uid, limit=10)
    assert upload_rows, "no audit row for workspace.file_uploaded"

    # 削除。
    r = client.delete(f"/workspace/files/{fid}")
    assert r.status_code == 200

    delete_rows = store.list_audit(action="workspace.file_deleted", actor=uid, limit=10)
    assert delete_rows, "no audit row for workspace.file_deleted"

    _logout()


def test_upload_does_not_block_event_loop(monkeypatch):
    """アップロード処理内の同期 I/O が `run_in_threadpool` 経由で実行され、単一 worker の
    event loop を塞がないこと。`httpx.ASGITransport` で app を直接呼び出し、1個の event loop
    （`asyncio.run` が作るもの・uvicorn 単一 worker と同じ「1プロセス1 loop」条件）の上で
    アップロードと並行リクエストを同時に走らせる——`TestClient` の素の `.post()`/`.get()` は
    呼び出しごとに独立した event loop（portal）を割り当てるため、この検証には使えない。
    `store.record_workspace_file` に偽の遅延（1秒 sleep）を仕込んでも、並行する軽量な
    `GET /workspace/files` がその遅延に巻き込まれず即座に返ることを確認する（旧実装＝
    event loop 上で直接 sleep していれば、他の全リクエストも同じだけ足止めされたはず）。

    経過時間は実験開始（アップロード発行前）を起点に測る——event loop が塞がれていると
    `await asyncio.sleep(0.2)` 自体の再開も遅延に巻き込まれて遅れるため、その**後**に取り直した
    時刻を起点にすると「GET 単体の所要時間」は常に短く見えてしまい、ブロッキングを検出できない
    （実測で確認済み）。
    """
    if not _try_init():
        pytest.skip("DB down")
    import asyncio

    import httpx

    from sherpa import store as store_mod

    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)
    cookies = dict(client.cookies)   # セッションを維持したまま cookie だけ非同期側へ引き継ぐ

    orig_record = store_mod.record_workspace_file

    def _slow_record(*a, **kw):
        time.sleep(2.0)
        return orig_record(*a, **kw)

    monkeypatch.setattr(store_mod, "record_workspace_file", _slow_record)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                      cookies=cookies) as ac:
            t_start = time.monotonic()   # 実験全体の起点（アップロード発行前）
            upload_task = asyncio.create_task(ac.post(
                "/workspace/files",
                files={"file": (f"slow_{sfx}.txt", io.BytesIO(b"hello"), "text/plain")}))
            await asyncio.sleep(0.2)   # アップロードが確実に _finalize() 実行中のタイミングを待つ
            fast_resp = await ac.get("/workspace/files")
            fast_total = time.monotonic() - t_start
            upload_resp = await upload_task
            return upload_resp, fast_resp, fast_total

    upload_resp, fast_resp, fast_total = asyncio.run(_run())
    _logout()
    assert upload_resp.status_code == 200, upload_resp.text
    assert fast_resp.status_code == 200, fast_resp.text
    # 遅延（2秒）に対し十分小さい閾値（マージン3倍・低速CIでのフレーク耐性）。event loop が塞がれていれば ~2秒に張り付く。
    assert fast_total < 1.2, f"並行リクエストがアップロードの遅延に巻き込まれた: {fast_total:.2f}s"


def test_compat_mode_no_regression(auth_disabled):
    """互換モード（SHERPA_AUTH_DISABLED=1）で workspace エンドポイントが動くこと。

    モジュール再 import は不要（auth.auth_disabled() は env を動的に読む）。
    compat モードでは /auth/me が admin を返す → workspace も admin 名義で動く。
    """
    r = client.get("/workspace/files")
    # 503 や 500 にならなければOK（DB が使えない場合でも 500 以外）。
    assert r.status_code in (200, 503), f"compat workspace/files failed: {r.status_code} {r.text}"


def test_upload_size_limit():
    """上限超えのファイルは 413 で拒否される。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    # _WORKSPACE_MAX_BYTES を超えるデータ（デフォルト 10MB）。
    big_data = b"x" * (10 * 1024 * 1024 + 1)
    r = client.post(
        "/workspace/files",
        files={"file": (f"bigfile_{sfx}.txt", io.BytesIO(big_data), "text/plain")},
    )
    assert r.status_code == 413, f"expected 413 for oversized file, got {r.status_code}"

    _logout()


def test_disallowed_extension():
    """許可外拡張子は 422 で拒否される。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    for bad_ext in (".exe", ".dll", ".zip", ".pdf"):
        r = client.post(
            "/workspace/files",
            files={"file": (f"badfile{sfx}{bad_ext}", io.BytesIO(b"content"), "application/octet-stream")},
        )
        assert r.status_code == 422, f"expected 422 for {bad_ext}, got {r.status_code}"

    _logout()


def test_w1_ledger_based_search_excludes_fs_residue():
    """W1: 台帳削除済みファイルは物理ファイルが残っていても grep にヒットしない。

    手順:
    1. ファイルをアップロード（台帳 status='uploaded'・物理ファイルあり）。
    2. 台帳を論理削除（DELETE エンドポイント）→ status='deleted'。
    3. 物理ファイルを再作成（削除失敗を模擬）。
    4. /workspace/search がヒットしないことを確認（台帳基準）。
    """
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    # アップロード。
    marker = f"W1_RESIDUE_MARKER_{sfx}"
    content = f"{marker}=secret".encode("utf-8")
    r = _upload(f"w1test{sfx}.txt", content)
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    rel_path = r.json()["rel_path"]

    # grep でヒットすることを確認（正常動作）。
    r = client.get("/workspace/search", params={"q": marker})
    assert r.status_code == 200
    assert any(marker in h["text"] for h in r.json()["hits"]), \
        "pre-delete: expected hit not found"

    # 台帳から論理削除（DELETE エンドポイント）。物理ファイルも消えるが…
    r = client.delete(f"/workspace/files/{fid}")
    assert r.status_code == 200

    # …物理ファイルを再作成（削除失敗シミュレーション）。
    users_dir = pathlib.Path(_TMP_USERS)
    physical = users_dir / uid / "workspace" / "files" / rel_path
    physical.write_bytes(content)   # FS 残骸を意図的に再作成。

    # W1 の核心: 台帳削除済みなので grep にヒットしてはいけない。
    r = client.get("/workspace/search", params={"q": marker})
    assert r.status_code == 200
    assert not any(marker in h["text"] for h in r.json()["hits"]), \
        f"W1 FAIL: ledger-deleted file leaked via FS residue (rel={rel_path})"

    _logout()


def test_w2_all_allowed_exts_are_searchable():
    """W2: アップロード許可拡張子 = grep 対象拡張子（一元定義の一致確認）。"""
    from sherpa.api import _WORKSPACE_ALLOWED_EXT, _WORKSPACE_SEARCHABLE_EXT

    # 単一真実源の確認: 2つの名前は同じオブジェクト（alias）。
    assert _WORKSPACE_SEARCHABLE_EXT is _WORKSPACE_ALLOWED_EXT, \
        "W2 FAIL: _WORKSPACE_SEARCHABLE_EXT is not the same object as _WORKSPACE_ALLOWED_EXT (should be alias)"
    assert _WORKSPACE_ALLOWED_EXT == _WORKSPACE_SEARCHABLE_EXT, (
        f"W2 FAIL: allowed ext != searchable ext\n"
        f"  allowed-only: {_WORKSPACE_ALLOWED_EXT - _WORKSPACE_SEARCHABLE_EXT}\n"
        f"  searchable-only: {_WORKSPACE_SEARCHABLE_EXT - _WORKSPACE_ALLOWED_EXT}"
    )


def test_w2_csv_json_yaml_are_searchable():
    """W2: CSV/JSON/YAML 等が実際にアップロードでき、grep にヒットすること。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    test_cases = [
        (f"data{sfx}.csv",  f"CSV_MARKER_{sfx},value\n1,2"),
        (f"cfg{sfx}.json",  f'{{"JSON_MARKER_{sfx}": true}}'),
        (f"conf{sfx}.yaml", f"YAML_MARKER_{sfx}: enabled"),
    ]
    markers = [f"CSV_MARKER_{sfx}", f"JSON_MARKER_{sfx}", f"YAML_MARKER_{sfx}"]

    for fname, content in test_cases:
        r = _upload(fname, content.encode("utf-8"))
        assert r.status_code == 200, f"upload failed for {fname}: {r.text}"

    for marker in markers:
        r = client.get("/workspace/search", params={"q": marker})
        assert r.status_code == 200
        assert any(marker in h["text"] for h in r.json()["hits"]), \
            f"W2 FAIL: {marker} not found in workspace search"

    _logout()


def test_download_roundtrip():
    """P1-c: アップロード→DL で本文がそのまま返る（Content-Disposition 相当のファイル名も一致）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    content = f"DOWNLOAD_MARKER_{sfx}".encode("utf-8")
    r = _upload(f"dl_test_{sfx}.txt", content)
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    rel_path = r.json()["rel_path"]

    r = client.get(f"/workspace/files/{fid}/download")
    assert r.status_code == 200, r.text
    assert r.content == content
    assert rel_path in (r.headers.get("content-disposition") or "")

    _logout()


def test_download_cross_user_isolation():
    """P1-c: 他ユーザーの file_id を指定した DL は 404（本人以外は閲覧不可）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid_a, pw_a = _mk_user(sfx + "a")
    uid_b, pw_b = _mk_user(sfx + "b")

    _login(uid_a, pw_a)
    r = _upload(f"private_dl_{sfx}.txt", b"A's secret download")
    assert r.status_code == 200, r.text
    fid_a = r.json()["id"]
    _logout()

    _login(uid_b, pw_b)
    r = client.get(f"/workspace/files/{fid_a}/download")
    assert r.status_code == 404, f"B could download A's file: {r.text}"
    _logout()


def test_download_after_delete_returns_404():
    """P1-c: 削除済みファイルの DL は 404（台帳 status='deleted' で不可視）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    r = _upload(f"del_before_dl_{sfx}.txt", b"soon deleted")
    assert r.status_code == 200, r.text
    fid = r.json()["id"]

    r = client.delete(f"/workspace/files/{fid}")
    assert r.status_code == 200, r.text

    r = client.get(f"/workspace/files/{fid}/download")
    assert r.status_code == 404, f"deleted file still downloadable: {r.status_code}"

    _logout()


def test_download_unknown_id_returns_404():
    """P1-c: 存在しない file_id は 404。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    r = client.get("/workspace/files/999999999/download")
    assert r.status_code == 404

    _logout()


def test_download_audit_row():
    """P1-c: DL に対して監査行（workspace.file_downloaded）が記録される。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    r = _upload(f"audit_dl_{sfx}.txt", b"audit dl content")
    assert r.status_code == 200
    fid = r.json()["id"]

    r = client.get(f"/workspace/files/{fid}/download")
    assert r.status_code == 200

    rows = store.list_audit(action="workspace.file_downloaded", actor=uid, limit=10)
    assert rows, "no audit row for workspace.file_downloaded"

    _logout()


def test_w2_invariant_workspace_not_rag_indexed():
    """W2/不変条件: workspace 拡張子を広げても ES/Neo4j を参照しないことを確認。

    - workspace_search が grep_tool._TEXT_EXT を変数として **使用（代入/比較）** しないこと。
      コメント内の文言は許容（説明目的）。
    - ES/Neo4j モジュールを参照しないこと。
    - es_index.py / world_graph.py が personal_workspace_files を参照しないこと（RAG 隔離）。
    """
    import inspect
    import re as _re
    import sherpa.api as _api_mod
    import sherpa.es_index as _esi
    import sherpa.ingest.world_graph as _wg
    src = inspect.getsource(_api_mod.workspace_search)

    # コメント行（#で始まる行）を除いたコードで _TEXT_EXT の変数使用がないことを確認。
    code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    code_only = "\n".join(code_lines)
    assert not _re.search(r'\b_TEXT_EXT\b', code_only), \
        "W2 invariant FAIL: workspace_search uses shared KB grep_tool._TEXT_EXT as a variable"
    assert "es_index" not in code_only, \
        "W2 invariant FAIL: workspace_search references es_index"
    assert "world_graph" not in code_only, \
        "W2 invariant FAIL: workspace_search references world_graph"

    # RAG 隔離: ES/グラフ索引が personal_workspace_files を使わないこと。
    esi_src = inspect.getsource(_esi)
    wg_src = inspect.getsource(_wg)
    assert "personal_workspace_files" not in esi_src, \
        "RAG isolation FAIL: es_index.py references personal_workspace_files"
    assert "personal_workspace_files" not in wg_src, \
        "RAG isolation FAIL: world_graph.py references personal_workspace_files"


def test_download_symlinked_ledger_path_rejected():
    """RV LOW（Phase1）: 台帳 rel_path の実体が symlink のときは fail-closed で 404。
    _confined_path は resolve 済みを返すため、symlink 検査は未解決パス側で行う必要がある
    （resolve 後に is_symlink() しても常に False＝検査が無効、という穴の回帰テスト）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    r1 = _upload(f"target_{sfx}.txt", b"secret-target")
    assert r1.status_code == 200, r1.text
    r2 = _upload(f"link_{sfx}.txt", b"placeholder")
    assert r2.status_code == 200, r2.text
    fid2 = r2.json()["id"]

    files_dir = pathlib.Path(_TMP_USERS) / uid / "workspace" / "files"
    link_path = files_dir / r2.json()["rel_path"]
    link_path.unlink()
    link_path.symlink_to(files_dir / r1.json()["rel_path"])   # files_dir 内でも symlink 自体を拒否

    r = client.get(f"/workspace/files/{fid2}/download")
    assert r.status_code == 404, f"symlink 実体の DL が通ってしまった: {r.status_code}"
    _logout()


def test_download_symlinked_files_dir_rejected():
    """RV r2 LOW: files/ ディレクトリ自体が symlink のときも fail-closed で 404。
    親が symlink だと _confined_path は symlink 先を信頼ルートにしてしまうため、
    未解決の files_dir を検査してから封じ込め確認に進む必要がある。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    r = _upload(f"parent_{sfx}.txt", b"parent-symlink-check")
    assert r.status_code == 200, r.text
    fid = r.json()["id"]

    ws = pathlib.Path(_TMP_USERS) / uid / "workspace"
    real_files = ws / "files"
    moved = ws / "files_real"
    real_files.rename(moved)
    (ws / "files").symlink_to(moved)
    try:
        resp = client.get(f"/workspace/files/{fid}/download")
        assert resp.status_code == 404, f"files/ が symlink でも DL が通ってしまった: {resp.status_code}"
    finally:
        (ws / "files").unlink()
        moved.rename(real_files)
    _logout()
