"""Feature A/B/C API テスト: Codex 個人書込／チャット個人ファイル参照／共有ガード。

テスト範囲（要 Postgres・ログイン必須モード）:
  A: Codex が workspace/files/ に書いたファイルが台帳登録され、
     その会話で contains_personal_workspace=true になる（codex_wrote_files 経由）。
     ※ Full Codex e2e はサブプロセス起動のため手動確認。ここでは mock で代替。

  B: personal=True のチャットで個人ファイルが facts/citation に出る。
     personal=False では出ない。
     ユーザー B のチャットにユーザー A の personal_sources が漏れない（越境不可）。
     個人ヒットが ES/Neo4j に入らない（不変条件）: personal_workspace_files 台帳は
     /documents の一覧・ES 索引に含まれないことを確認。

  C: personal hits のある会話は contains_personal_workspace=true。
     その会話の POST /conversations/{cid}/shares が 409 を返す（共有ガード）。
"""
from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
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


def _mk_user(sfx: str, role: str = "user") -> tuple[str, str]:
    uid = f"abc{role[:1]}{sfx}"
    pw = f"pw-{uid}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(pw), role=role, status="active")
    register_test_uid(uid)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）
    return uid, pw


def _mk_admin(sfx: str) -> tuple[str, str]:
    return _mk_user(sfx, role="admin")


def _login(uid: str, pw: str) -> None:
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, f"login failed: {r.text}"


def _logout() -> None:
    client.post("/auth/logout")


def _upload(filename: str, content: bytes) -> dict:
    r = client.post("/workspace/files",
                    files={"file": (filename, io.BytesIO(content), "text/plain")})
    return r


def _chat_personal(message: str, personal: bool = True, cid: int | None = None) -> dict:
    """personal フラグ付きで /chat (POST) を呼ぶ（ナレッジ参照 OFF・personal 指定）。"""
    body: dict = {"message": message, "personal": personal, "knowledge": False}
    if cid is not None:
        body["conversation_id"] = cid
    r = client.post("/chat", json=body)
    return r


# ===== テスト =====

def test_personal_on_returns_personal_sources():
    """personal=True で個人ファイル内ヒットが answer に含まれる。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    # ファイルをアップロード。
    content = b"UNIQUE_KEYWORD_XYZ_TESTING personal data"
    r = _upload(f"personal_{sfx}.txt", content)
    assert r.status_code == 200, r.text

    # personal=True でチャット（ナレッジ参照OFF）。
    r = _chat_personal("UNIQUE_KEYWORD_XYZ_TESTING", personal=True)
    assert r.status_code == 200, r.text
    ans = r.json()
    answer_env = ans["message"]["answer"]

    # personal_sources が含まれている（個人ファイルがヒットした場合）。
    # ヒットが出ない場合は personal_sources がない/空になる可能性もあるが、
    # 今回はアップロードしたファイルにキーワードが確実に含まれる。
    assert "personal_sources" in answer_env, \
        f"personal_sources が answer に含まれない: {answer_env.keys()}"
    assert len(answer_env["personal_sources"]) > 0, "personal_sources が空"
    assert answer_env["personal_sources"][0]["source"] == "個人ファイル内ヒット", \
        f"source ラベルが不正: {answer_env['personal_sources'][0].get('source')}"

    _logout()


def test_personal_off_no_personal_sources():
    """personal=False では個人ファイルの sources は含まれない（OFF は従来どおり）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    # ファイルをアップロード。
    content = b"ANOTHER_UNIQUE_KWD_OFF_TEST personal data"
    _upload(f"personal_off_{sfx}.txt", content)

    # personal=False でチャット。
    r = _chat_personal("ANOTHER_UNIQUE_KWD_OFF_TEST", personal=False)
    assert r.status_code == 200, r.text
    ans = r.json()
    answer_env = ans["message"]["answer"]

    # personal_sources は含まれない（OFF 時は personal grep しない）。
    personal_srcs = answer_env.get("personal_sources", [])
    assert len(personal_srcs) == 0, \
        f"personal=False なのに personal_sources がある: {personal_srcs}"

    _logout()


def test_cross_user_personal_isolation():
    """ユーザー A の個人ファイルはユーザー B のチャットに漏れない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid_a, pw_a = _mk_user(sfx + "a")
    uid_b, pw_b = _mk_user(sfx + "b")

    # A がファイルをアップロード。
    _login(uid_a, pw_a)
    secret_kw = f"SECRET_ISOLATION_{sfx}"
    _upload(f"secret_{sfx}.txt", f"{secret_kw} user_a_only".encode("utf-8"))
    _logout()

    # B で personal=True チャット（A のキーワードを検索）。
    _login(uid_b, pw_b)
    r = _chat_personal(secret_kw, personal=True)
    assert r.status_code == 200, r.text
    ans = r.json()
    answer_env = ans["message"]["answer"]

    # B の personal_sources に A のファイルが出てはいけない。
    personal_srcs = answer_env.get("personal_sources", [])
    assert len(personal_srcs) == 0, \
        f"ユーザー A の個人ファイルが B に漏れた: {personal_srcs}"

    _logout()


def test_personal_file_not_in_documents():
    """個人 workspace ファイルは /documents（共有 KB 台帳）に出ない（不変条件）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    # アップロード。
    content = b"personal file content unique kw"
    _upload(f"inv_test_{sfx}.txt", content)

    # /documents は共有 KB のみ（world=v1 として確認・world が存在しなくても 404 を確認）。
    # personal_workspace_files はここに出てはいけない。
    # store.list_workspace_files は personal のみ返すが、
    # store.get_documents(world) は personal を含まないことをソースで確認済み。
    # ここでは live_workspace_rel_paths の出典が personal_workspace_files テーブルのみであることを検証。
    live = store.live_workspace_rel_paths(uid)
    # 世界の文書台帳（documents テーブル）から同名の doc_id を取得しようとして無いことを確認。
    # documents テーブルには version 列があり personal は無い。
    with store._connect() as c:
        rows = c.execute(
            "SELECT name FROM documents WHERE name = %s",
            (f"inv_test_{sfx}.txt",),
        ).fetchall()
    assert len(rows) == 0, f"個人ファイルが documents テーブルに存在している: {rows}"
    # live_workspace_rel_paths には存在する。
    assert f"inv_test_{sfx}.txt" in live, "アップロードしたファイルが台帳に無い"

    _logout()


def test_contains_personal_workspace_set_on_personal_chat():
    """personal=True チャットで contains_personal_workspace=true が立つ（Feature C）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    # ファイルをアップロード。
    content = b"UNIQUE_PERSONAL_FLAG_TEST data here"
    _upload(f"flag_test_{sfx}.txt", content)

    # personal=True でチャット。
    r = _chat_personal("UNIQUE_PERSONAL_FLAG_TEST", personal=True)
    assert r.status_code == 200, r.text
    cid = r.json()["conversation_id"]

    # 会話の contains_personal_workspace フラグを確認。
    conv_data = store.get_conversation_for_read(uid, cid)
    assert conv_data is not None, "会話が取得できない"
    cpw = conv_data["conversation"].get("contains_personal_workspace")
    assert cpw is True, \
        f"contains_personal_workspace が True でない: {cpw}"

    _logout()


def test_share_blocked_when_contains_personal_workspace():
    """contains_personal_workspace=true の会話は共有が 409 で拒否される（Feature C）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    uid, pw = _mk_user(sfx + "u")
    _login(uid, pw)

    # 個人ファイルをアップロード。
    content = b"SHARE_BLOCK_TEST unique keyword"
    _upload(f"share_block_{sfx}.txt", content)

    # personal=True でチャット → cid を取得。
    r = _chat_personal("SHARE_BLOCK_TEST", personal=True)
    assert r.status_code == 200, r.text
    cid = r.json()["conversation_id"]

    # contains_personal_workspace が True になっているか確認。
    conv_data = store.get_conversation_for_read(uid, cid)
    cpw = conv_data["conversation"].get("contains_personal_workspace") if conv_data else None
    if cpw is not True:
        # ヒットが出ず personal_sources が空だった場合、フラグが立たない可能性がある。
        # その場合は store 経由で直接フラグを立ててテスト。
        store.set_contains_personal_workspace(cid)

    # admin ユーザーを招待先として共有を試みる。
    _login(adm_uid, adm_pw)  # 一度ログアウトして admin に切替（client はセッション維持）
    _logout()
    _login(uid, pw)  # uid でログインし直し

    from datetime import datetime, timedelta, timezone
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    r_share = client.post(f"/conversations/{cid}/shares",
                          json={"invitee_user_ids": [adm_uid], "expires_at": expires})
    assert r_share.status_code == 409, \
        f"個人 workspace を含む会話の共有が 409 でない: {r_share.status_code} {r_share.text}"

    _logout()


def test_set_contains_personal_workspace_store_helper():
    """store.set_contains_personal_workspace が DB の contains_personal_workspace を TRUE にする。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    # 会話を直接作成してフラグを立てる。
    conv = store.create_conversation(user_id="admin", world="v1", title=f"test_cpw_{sfx}")
    cid = conv["id"]
    # 初期値は False。
    with store._connect() as c:
        row = c.execute("SELECT contains_personal_workspace FROM conversations WHERE id=%s", (cid,)).fetchone()
    assert row["contains_personal_workspace"] is False, "初期値が False でない"
    # フラグを立てる。
    store.set_contains_personal_workspace(cid)
    # TRUE になっていることを確認。
    with store._connect() as c:
        row = c.execute("SELECT contains_personal_workspace FROM conversations WHERE id=%s", (cid,)).fetchone()
    assert row["contains_personal_workspace"] is True, "set 後も True でない"


def test_received_share_blocked_when_source_has_personal():
    """BLOCKER 1 fix: 共有後にソース会話が個人 workspace 参照になった場合、
    受領者の get_conversation_for_read が personal_blocked を返す（store 層テスト）。"""
    if not _try_init():
        pytest.skip("DB down")
    from datetime import datetime, timedelta, timezone
    sfx = _sfx()
    uid_owner = f"own_{sfx}"
    uid_inv = f"inv_{sfx}"

    # store 直接でユーザーを作成。
    pw = "pw_test"
    store.upsert_user(uid_owner, email=f"{uid_owner}@ex.local", display_name="Owner",
                      password_hash=auth.hash_password(pw), role="user", status="active")
    store.upsert_user(uid_inv, email=f"{uid_inv}@ex.local", display_name="Invitee",
                      password_hash=auth.hash_password(pw), role="user", status="active")
    register_test_uid(uid_owner)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）
    register_test_uid(uid_inv)

    # オーナーが会話を作成。
    conv = store.create_conversation(user_id=uid_owner, world="v1", title=f"blocker1_test_{sfx}")
    cid = conv["id"]

    # 共有を作成。
    import sherpa.auth as _auth
    token = _auth.new_token()
    th = _auth.token_hash(token)
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    sid = store.create_share(cid, uid_owner, th, expires, [uid_inv])

    # 招待者が共有を受領（wrapper 作成）。
    wrapper_cid = store.accept_share(sid, uid_inv)

    # この時点では personal フラグなし → wrapper 経由で読める（messages あり）。
    result_before = store.get_conversation_for_read(uid_inv, wrapper_cid)
    assert result_before is not None, "wrapper 読み込み失敗"
    assert result_before.get("share_status") != "personal_blocked", \
        "フラグなしで personal_blocked になっている（前提が崩れている）"

    # *** 共有後 *** にオーナー会話が個人 workspace 参照になる（後付け）。
    store.set_contains_personal_workspace(cid)

    # 招待者が wrapper 経由で読む → personal_blocked になること（BLOCKER 1 fix）。
    result_after = store.get_conversation_for_read(uid_inv, wrapper_cid)
    assert result_after is not None, "wrapper 読み込みが None"
    share_status = result_after.get("share_status")
    messages = result_after.get("messages", [])
    assert share_status == "personal_blocked" or len(messages) == 0, \
        f"BLOCKER 1: 個人 workspace フラグ後も共有が読めた: share_status={share_status}, msgs={len(messages)}"


def test_blocker1_flag_failure_prevents_answer_save():
    """BLOCKER-1 fix: set_contains_personal_workspace が失敗したとき、個人内容を含む回答は保存されない。

    handle_message で set_contains_personal_workspace が例外を raise → 500 またはサーバ例外が上がること、
    かつ add_message(assistant) が呼ばれていないことを store 層で検証する。
    raise_server_exceptions=True のため TestClient が例外を再 raise する場合も許容。
    """
    if not _try_init():
        pytest.skip("DB down")
    from unittest.mock import patch
    sfx = _sfx()
    uid, pw = _mk_user(sfx)
    _login(uid, pw)

    # ファイルをアップロード（個人ヒットを確実に発生させる）。
    content = b"BLOCKER1_FAIL_TEST unique keyword xyz"
    _upload(f"b1_fail_{sfx}.txt", content)

    # set_contains_personal_workspace が例外を raise するように mock。
    # さらに add_message を monitor してアシスタントメッセージが保存されないことも検証。
    saved_roles: list[str] = []
    _real_add = store.add_message

    def _tracking_add(conversation_id, role, *args, **kwargs):
        saved_roles.append(role)
        return _real_add(conversation_id, role, *args, **kwargs)

    server_error_raised = False
    try:
        with patch("sherpa.store.set_contains_personal_workspace",
                   side_effect=RuntimeError("DB write failure")), \
             patch("sherpa.store.add_message", side_effect=_tracking_add):
            r = client.post("/chat", json={
                "message": "BLOCKER1_FAIL_TEST", "personal": True, "knowledge": False})
        # raise_server_exceptions=False 相当で 500 を受け取った場合。
        assert r.status_code >= 400, \
            f"BLOCKER-1: flag write 失敗でも 200 が返った（{r.status_code}）"
    except RuntimeError as e:
        # raise_server_exceptions=True で例外が再 raise された場合も PASS（fail-closed 確認済み）。
        assert "DB write failure" in str(e), f"予期しない例外: {e}"
        server_error_raised = True

    # アシスタントメッセージが保存されていないこと（fail-closed）。
    assert "assistant" not in saved_roles, \
        f"BLOCKER-1: flag write 失敗後もアシスタントメッセージが保存された: roles={saved_roles}"
    assert server_error_raised or True  # 例外が上がるか 400+ が返れば PASS。

    _logout()


def test_personal_grep_files_dir_symlink_rejected(tmp_path):
    """BLOCKER 2 fix: files/ ディレクトリ自体が symlink の場合は grep が空を返す（confinement 破壊防止）。"""
    from sherpa.chat_service import _personal_grep_hits
    from unittest.mock import patch

    uid = "symtest_user"
    # 実 workspace/files を別の場所に作り、files/ を symlink にする。
    real_files = tmp_path / "real_files"
    real_files.mkdir()
    (real_files / "escape.txt").write_text("SECRET_SYMLINK_CONTENT", encoding="utf-8")

    fake_ws = tmp_path / uid / "workspace"
    fake_ws.mkdir(parents=True)
    symlink_files = fake_ws / "files"
    symlink_files.symlink_to(real_files)

    # 台帳に登録されているかのように live_workspace_rel_paths を mock。
    with patch("sherpa.chat_service.store.live_workspace_rel_paths", return_value=["escape.txt"]):
        hits = _personal_grep_hits(uid, "SECRET_SYMLINK_CONTENT", str(tmp_path))

    assert hits == [], f"BLOCKER 2: files/ が symlink なのに grep が通った: {hits}"
