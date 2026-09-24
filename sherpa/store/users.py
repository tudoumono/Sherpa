"""ユーザー管理・セッション（docs/proposals/2026-07-01-認証と共有の提案.md MVP）。

`sherpa/store/__init__.py` から純移動（フェーズ4 S8）。ロジックは一切変更していない。
セッション（auth_sessions）はテーブルが users と対になっているため、計画どおり本モジュールに同居させる。
facade（`sherpa.store`）の属性として呼び出し側（api.py・deps.py・routers/*・chat_service.py 等）から
参照される。tests/api/test_auth_api.py・test_health_api.py・tests/api/test_workspace_ttl.py が
`store.get_user`/`store.session_user` を直接 monkeypatch するが、これは facade 属性への実行時アクセス
（`import sherpa.store as store; store.get_user(...)`）であり、settings.py の `_audit_insert` の
ようなパッケージ内部からの相互呼び出しではないため、追加の実行時解決の仕組みは不要
（呼び出し側は毎回 `store.X` を属性参照する＝patch がそのまま効く）。
"""
from __future__ import annotations

import os
import time

from .db import _connect, _ensure

# QW4（性能台帳・scratchpad/perf-triage-ledger.md §4）: 全ページ共通ポーリング（状態ドット・
# 背景ターン通知等）は毎回 `_current_user`→`session_user` を通るため、素直に毎回 UPDATE すると
# ポーリング回数だけ PG 書込が発生する。`last_seen_at` は「だいたいの最終アクセス時刻」で足りる
# 用途（表示・監査補助）のため、token_hash 単位で前回書込から一定時間内はスキップする
# （プロセス内キャッシュ・単一 worker 前提＝複数 worker 化する場合は共有ストアへの置き換えが必要）。
_LAST_SEEN_THROTTLE_SEC = float(os.environ.get("SHERPA_LAST_SEEN_THROTTLE_SEC", "60"))
_last_seen_written_at: dict = {}   # token_hash -> time.monotonic() の前回書込時刻


def get_user(uid) -> dict | None:
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT uid, email, display_name, role, status, must_change_password "
            "FROM users WHERE uid=%s", (uid,)).fetchone()


def get_user_by_uid(uid) -> dict | None:
    """ログイン用（uid＝ユーザー名・password_hash 含む＝サーバ内部のみ）。"""
    _ensure()
    with _connect() as c:
        return c.execute("SELECT uid, email, display_name, role, status, must_change_password, password_hash "
                         "FROM users WHERE uid=%s", (uid,)).fetchone()


def get_user_by_email(email) -> dict | None:
    """ログイン用（password_hash 含む＝サーバ内部のみ）。"""
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
    """共有ダイアログの入力補完（バッチ2・5番・2026-07-03）: uid/display_name の部分一致で
    **active** ユーザーのみを返す（無効化ユーザーへ共有できても意味が無い＋一覧に出す必要が無い）。
    自分自身（exclude_uid）は除外。返す列は uid/display_name のみ（email・role 等は不要な情報を渡さない）。

    RV MEDIUM（2026-07-03再検証）: `query` は ILIKE パターンへそのまま埋め込むため、`%`/`_`（ILIKE の
    ワイルドカード）がエスケープされていないと `q=%` で全 active ユーザーに一致してしまい、
    「空クエリで全件化しない」意図（api.py の `users_suggest` が空文字は空配列を返す設計）を
    ユーザー入力の `%` 一文字で迂回できてしまう。バックスラッシュをエスケープ文字にして
    `%`/`_`/バックスラッシュ自体をリテラル扱いにしてから `%…%`（意図した前後方一致ワイルドカード）で包む。
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
    """ユーザーを**新規作成専用**で追加する（RV MEDIUM「バッチ2」4番・2026-07-03）。

    既存 uid（無効化済み含む）が既にあれば **何もせず None を返す**（`ON CONFLICT (uid) DO NOTHING`）。
    `POST /admin/users`（作成 API）が `upsert_user` を使っていたため、既存 uid で「作成」すると
    黙って上書き（パスワード/権限が置き換わる）してしまっていた事故の修正版。更新系
    （パスワード変更・PATCH /admin/users/{uid}）は従来どおり `upsert_user` を使い続けてよい
    （そちらは「既存を明示的に更新する」意図なので上書きが正しい）。
    uid の大小文字は既存の `uid TEXT UNIQUE` 制約と同じ**大小文字区別**のまま（変更しない）。
    `must_change_password` は既定 True＝管理者が付けた初期パスワードは本人しか知らない状態に
    到達していないため、初回ログインで変更を強制する（合成ユーザー等の例外だけ False を明示）。
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


def upsert_user(uid, email=None, display_name=None, password_hash=None, role="user", status="active",
                must_change_password=None) -> dict:
    """ユーザー作成/更新。

    password_hash/email/display_name/must_change_password は None なら既存維持（書込専用）。
    新規作成時の must_change_password は既定 false。
    """
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
    """有効セッションの user 行（未取消・期限内・active のみ）。`last_seen_at` を更新
    （前回書込から `_LAST_SEEN_THROTTLE_SEC` 未満ならスキップ＝間引く）。無効は None。"""
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
    _last_seen_written_at.pop(token_hash, None)   # 取消済みトークンの残骸でキャッシュが無限成長しないよう掃除
