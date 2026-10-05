"""ユーザー管理とログインセッション（auth_sessions）。
設計: docs/design/users.md「ログインとセッション」
"""
from __future__ import annotations

import time

from .db import _connect, _ensure

# last_seen_at の更新を token_hash ごとに一定時間間引く（プロセス内キャッシュ・単一 worker 前提）。
_LAST_SEEN_THROTTLE_SEC = 60.0
_last_seen_written_at: dict = {}


def get_user(uid) -> dict | None:
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT uid, email, display_name, role, status, must_change_password "
            "FROM users WHERE uid=%s", (uid,)).fetchone()


def get_user_by_uid(uid) -> dict | None:
    """ログイン用のユーザー行（uid・password_hash を含む＝サーバ内部のみ）。"""
    _ensure()
    with _connect() as c:
        return c.execute("SELECT uid, email, display_name, role, status, must_change_password, password_hash "
                         "FROM users WHERE uid=%s", (uid,)).fetchone()


def get_user_by_email(email) -> dict | None:
    """ログイン用のユーザー行（password_hash を含む＝サーバ内部のみ）。"""
    _ensure()
    with _connect() as c:
        return c.execute("SELECT uid, email, display_name, role, status, must_change_password, password_hash "
                         "FROM users WHERE email=%s", (email,)).fetchone()


def list_users() -> list:
    _ensure()
    with _connect() as c:
        return c.execute("SELECT uid, email, display_name, role, status, must_change_password, last_login_at "
                         "FROM users ORDER BY uid").fetchall()


def suggest_users(query: str, exclude_uid: str, limit: int = 10) -> list:
    """共有ダイアログの入力補完: uid/display_name の部分一致で active ユーザーだけを返す（exclude_uid は除く・返す列は uid/display_name のみ）。
    `query` の `%`/`_`/バックスラッシュはエスケープしてリテラル扱いにする。
    """
    _ensure()
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    with _connect() as c:
        return c.execute(
            "SELECT uid, display_name FROM users "
            "WHERE status='active' AND uid <> %s "
            "AND (uid ILIKE %s ESCAPE '\\' OR display_name ILIKE %s ESCAPE '\\') "
            "ORDER BY uid LIMIT %s",
            (exclude_uid, like, like, limit)).fetchall()


def create_user(uid, email=None, display_name=None, password_hash=None, role="user",
                status="active", must_change_password=True) -> dict | None:
    """ユーザーを新規作成専用で追加する。既存 uid（無効化済み含む）があれば何もせず None を返す（ON CONFLICT DO NOTHING）。
    `must_change_password` は既定 True（初回ログインでパスワード変更を強制する）。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO users (uid, email, display_name, password_hash, role, status, must_change_password) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (uid) DO NOTHING "
            "RETURNING uid, email, display_name, role, status, must_change_password",
            (uid, email, display_name, password_hash, role, status,
             bool(must_change_password))).fetchone()


def create_users_bulk(rows: list) -> list:
    """ユーザーを 1 トランザクションで新規作成する（途中で失敗したら 1 人も作らない）。
    rows は {uid, email, display_name, password_hash, role} の列。全員 must_change_password=TRUE・status=active。
    既存 uid があれば例外（呼び出し側で事前検査する）。作成した行を返す。
    """
    _ensure()
    out = []
    with _connect() as c:
        for r in rows:
            out.append(c.execute(
                "INSERT INTO users (uid, email, display_name, password_hash, role, status, must_change_password) "
                "VALUES (%s,%s,%s,%s,%s,'active',TRUE) "
                "RETURNING uid, email, display_name, role, status, must_change_password",
                (r["uid"], r.get("email"), r.get("display_name"), r["password_hash"], r["role"])).fetchone())
    return out


def upsert_user(uid, email=None, display_name=None, password_hash=None, role="user", status="active",
                must_change_password=None) -> dict:
    """ユーザーを作成/更新する。password_hash/email/display_name/must_change_password は None なら既存値を維持する（新規時の must_change_password は false）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO users (uid, email, display_name, password_hash, role, status, must_change_password) "
            "VALUES (%s,%s,%s,%s,%s,%s,COALESCE(%s,FALSE)) "
            "ON CONFLICT (uid) DO UPDATE SET email=COALESCE(EXCLUDED.email, users.email), "
            "  display_name=COALESCE(EXCLUDED.display_name, users.display_name), "
            "  password_hash=COALESCE(EXCLUDED.password_hash, users.password_hash), "
            "  role=EXCLUDED.role, status=EXCLUDED.status, "
            "  must_change_password=COALESCE(%s, users.must_change_password), "
            "  updated_at=now() "
            "RETURNING uid, email, display_name, role, status, must_change_password",
            (uid, email, display_name, password_hash, role, status,
             must_change_password, must_change_password)).fetchone()


def set_last_login(uid) -> None:
    _ensure()
    with _connect() as c:
        c.execute("UPDATE users SET last_login_at=now() WHERE uid=%s", (uid,))


def create_session(uid, token_hash, expires_at) -> None:
    _ensure()
    with _connect() as c:
        c.execute("INSERT INTO auth_sessions (user_id, token_hash, expires_at) VALUES (%s,%s,%s)",
                  (uid, token_hash, expires_at))


def session_user(token_hash) -> dict | None:
    """有効セッション（未取消・期限内・active）の user 行を返し、`last_seen_at` を更新する（`_LAST_SEEN_THROTTLE_SEC` 未満ならスキップ）。無効なら None。"""
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT u.uid, u.email, u.display_name, u.role, u.status, u.must_change_password "
            "FROM auth_sessions s "
            "JOIN users u ON u.uid=s.user_id "
            "WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>now() AND u.status='active'",
            (token_hash,)).fetchone()
        if row:
            now = time.monotonic()
            last = _last_seen_written_at.get(token_hash)
            if last is None or (now - last) >= _LAST_SEEN_THROTTLE_SEC:
                c.execute("UPDATE auth_sessions SET last_seen_at=now() WHERE token_hash=%s", (token_hash,))
                _last_seen_written_at[token_hash] = now
        return row


def revoke_session(token_hash) -> None:
    _ensure()
    with _connect() as c:
        c.execute("UPDATE auth_sessions SET revoked_at=now() WHERE token_hash=%s AND revoked_at IS NULL",
                  (token_hash,))
    _last_seen_written_at.pop(token_hash, None)  # 取消済みトークンの残骸を掃除する。
