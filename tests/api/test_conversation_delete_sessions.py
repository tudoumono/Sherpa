"""会話削除時の Codex resume セッション実体の削除
（`docs/proposals/2026-09-21-調査台帳を文脈の外に置く.md` §4-3）。

`DELETE /conversations/{cid}` は DB 側の削除（`store.delete_conversation`・soft/hard 問わず）に
加えて `.codex-sessions/{cid}`（Codex ネイティブ resume の永続 CODEX_HOME。将来はここに退避される
`investigation/` 配下の調査台帳も含む）を即時削除する（`sherpa.deps._delete_codex_sessions_for_conversation`）。
以前は DB 行だけが消え、このディレクトリは TTL 掃除（既定30日・`api._sweep_expired_codex_sessions`）
まで残っていた（穴）。

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import logging
import time

import pytest

from _common import _login, _try_init
from _test_users import register_test_uid
from sherpa import auth, deps, store
from sherpa.providers.codex.provider import _conversation_lock


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _mk_user(uid: str, password: str) -> None:
    store.upsert_user(uid, email=f"{uid}@convdel.local", display_name=uid,
                      password_hash=auth.hash_password(password), role="user", status="active")
    register_test_uid(uid)


def _session_dir(uid: str, cid: int):
    """`.codex-sessions/{cid}` の実パス（実行中プロセスが使っている `SHERPA_USERS_DIR` 基準・
    `_delete_codex_sessions_for_conversation` と同じ組み立て方）。"""
    return deps._USERS_DIR.resolve() / uid / "workspace" / ".codex-sessions" / str(cid)


def _mk_session_files(uid: str, cid: int):
    """`.codex-sessions/{cid}/sessions/x.jsonl` と `.codex-sessions/{cid}/investigation/manifest.json`
    を置く（受け入れ条件1の固定物）。"""
    d = _session_dir(uid, cid)
    (d / "sessions").mkdir(parents=True)
    (d / "sessions" / "x.jsonl").write_text('{"marker": "session"}\n')
    (d / "investigation").mkdir(parents=True)
    (d / "investigation" / "manifest.json").write_text('{"marker": "ledger"}\n')
    return d


def test_delete_conversation_removes_codex_sessions_dir():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"convdelu{sfx}", f"ConvDelPw{sfx}"
    _mk_user(uid, pw)
    conv = store.create_conversation(user_id=uid, world="v1", title="請求機能の調査")
    cid = conv["id"]
    d = _mk_session_files(uid, cid)
    assert d.is_dir()

    c = _login(uid, pw)
    r = c.delete(f"/conversations/{cid}")

    assert r.status_code == 200, r.text
    assert not d.exists(), "会話削除で .codex-sessions/{cid} ディレクトリごと消えていない"


def test_delete_conversation_keeps_other_conversation_sessions():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"convdelu2{sfx}", f"ConvDelPw2{sfx}"
    _mk_user(uid, pw)
    conv1 = store.create_conversation(user_id=uid, world="v1", title="会話1")
    conv2 = store.create_conversation(user_id=uid, world="v1", title="会話2")
    cid1, cid2 = conv1["id"], conv2["id"]
    d1 = _mk_session_files(uid, cid1)
    d2 = _mk_session_files(uid, cid2)

    c = _login(uid, pw)
    r = c.delete(f"/conversations/{cid1}")

    assert r.status_code == 200, r.text
    assert not d1.exists(), "削除した会話のセッションディレクトリが残っている"
    assert d2.is_dir(), "別会話のセッションディレクトリまで消してしまった（id取り違え）"


def test_delete_conversation_symlinked_session_dir_unlinks_link_not_target():
    """`.codex-sessions/{cid}` が symlink のときは辿らず——リンク自体だけ unlink し、
    参照先ディレクトリの中身には触れない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"convdelu3{sfx}", f"ConvDelPw3{sfx}"
    _mk_user(uid, pw)
    conv = store.create_conversation(user_id=uid, world="v1", title="会話3")
    cid = conv["id"]

    users_dir = deps._USERS_DIR.resolve()
    sessions_root = users_dir / uid / "workspace" / ".codex-sessions"
    sessions_root.mkdir(parents=True)
    real_target = users_dir / uid / "workspace" / "elsewhere-real-session"
    real_target.mkdir(parents=True)
    (real_target / "marker.txt").write_text("keep me\n")
    link = sessions_root / str(cid)
    link.symlink_to(real_target)

    c = _login(uid, pw)
    r = c.delete(f"/conversations/{cid}")

    assert r.status_code == 200, r.text
    assert not link.exists() and not link.is_symlink(), "symlink 自体が残っている"
    assert real_target.is_dir() and (real_target / "marker.txt").exists(), \
        "symlink のリンク先（参照先ディレクトリの中身）まで削除してしまった"


def test_delete_conversation_rmtree_failure_is_fail_open(caplog):
    """削除に失敗しても（読み取り専用等）会話削除 API 自体は成功で返す（fail-open）。

    内部関数の置換ではなく、実際に rmtree が失敗する状況を作る（`.codex-sessions` 自体を
    書込不可にし、配下の `{cid}` を rmdir できなくする）——モックは外部境界に限る
    （`docs/20-開発ハーネス.md` のテスト規範）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"convdelu4{sfx}", f"ConvDelPw4{sfx}"
    _mk_user(uid, pw)
    conv = store.create_conversation(user_id=uid, world="v1", title="会話4")
    cid = conv["id"]
    d = _mk_session_files(uid, cid)
    sessions_root = d.parent
    original_mode = sessions_root.stat().st_mode

    sessions_root.chmod(0o500)   # 書込不可＝配下の {cid} エントリを削除（rmdir）できなくする
    try:
        c = _login(uid, pw)
        with caplog.at_level(logging.WARNING, logger="sherpa"):
            r = c.delete(f"/conversations/{cid}")
    finally:
        sessions_root.chmod(original_mode)   # 後始末（他テスト・cleanup を巻き込まない）

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True, "rmtree 失敗時も会話削除 API は成功を返すべき（fail-open）"
    if not d.exists():
        pytest.skip("この実行環境では権限チェックが効かず rmtree が成功した（root 実行等）")
    assert d.is_dir(), "rmtree 失敗時は .codex-sessions/{cid} 自体（配下は空）が残るはず"
    assert "delete_codex_sessions_for_conversation" in caplog.text, "失敗時の warning ログが出ていない"


def test_delete_conversation_blocked_while_codex_execution_lock_held():
    """実行中（`CodexProvider._conversation_lock` を保持中）の削除は 409・DB もファイルも
    変更しない。ロック解放後の削除は 200 で両方消える（provider.py の実行系列化ロックと同じものを
    削除エンドポイントが DB 変更前に取る・RV指摘の是正）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"convdelu5{sfx}", f"ConvDelPw5{sfx}"
    _mk_user(uid, pw)
    conv = store.create_conversation(user_id=uid, world="v1", title="会話5")
    cid = conv["id"]
    d = _mk_session_files(uid, cid)

    lock = _conversation_lock(cid)
    assert lock.acquire(blocking=False), "テスト前提: ロックを取得できるはず"
    c = _login(uid, pw)
    try:
        r = c.delete(f"/conversations/{cid}")
        assert r.status_code == 409, r.text
        assert store.owns_conversation(uid, cid) is True, "実行中は DB 行を変更してはいけない"
        assert d.is_dir(), "実行中はセッションディレクトリを変更してはいけない"
    finally:
        lock.release()

    r2 = c.delete(f"/conversations/{cid}")
    assert r2.status_code == 200, r2.text
    assert store.owns_conversation(uid, cid) is False, "解放後の削除で DB 行が消えていない"
    assert not d.exists(), "解放後の削除でセッションディレクトリが消えていない"
