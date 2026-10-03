"""API 共有の依存ヘルパ（認証・world 解決・scope 検証・個人 workspace・Neo4j driver・フォルダ選択ルート）。`sherpa.api` は import しない。"""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import Field

from sherpa import auth, store, worlds
from sherpa import scope as scope_mod

_log = logging.getLogger("sherpa")

# セッション Cookie 名。
_COOKIE = "sherpa_session"
# 個人 workspace のルート。env 読みはここだけ。
_USERS_DIR = Path(os.environ.get("SHERPA_USERS_DIR", "data/users"))
# world 識別子は英数字＋限定記号（`/`・`..` 不可）。
_WORLD_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
# API 既定 world。
_DEFAULT_WORLD = worlds.default_world()
# API パラメータ用の world Field。
_WorldField = Field(default=None, pattern=_WORLD_PATTERN)


def _synthetic_admin() -> dict:
    """SHERPA_AUTH_DISABLED=1 時の合成 admin ユーザー。"""
    return {"uid": "admin", "email": None, "display_name": "Admin", "role": "admin",
            "status": "active", "must_change_password": False}


def _current_user(request: Request, *, allow_password_change: bool = False) -> dict:
    """cookie → session_user。既定でログイン必須。

    `SHERPA_AUTH_DISABLED=1` の明示時だけ合成 admin を返す互換モード。
    must_change_password が残るユーザーは、変更APIと /auth/me / logout 以外を使えない。
    """
    if auth.auth_disabled():
        return _synthetic_admin()
    token = request.cookies.get(_COOKIE)
    if not token:
        raise HTTPException(401, "ログインが必要です")
    user = store.session_user(auth.token_hash(token))
    if not user:
        raise HTTPException(401, "セッションが無効です（ログインし直してください）")
    if user.get("must_change_password") and not allow_password_change:
        raise HTTPException(403, "初回ログイン後のパスワード変更が必要です")
    return user


def _require_admin(user: dict) -> dict:
    """admin ロール必須。403 を raise する。"""
    if user.get("role") != "admin":
        raise HTTPException(403, "管理者権限が必要です")
    return user


def _check_scope(world: str, scope_paths):
    if not scope_mod.valid_scope_paths(world, scope_paths):
        raise HTTPException(422, "不明な範囲（scope_paths）が指定されました")


def _require_world(world: str):
    """world が解決できない（未登録・参照元不在・fixtures フラグ無し）なら 404。未知の world_id で Neo4j を直読みさせない。"""
    if not worlds.world_dir(world):
        raise HTTPException(404, "資料フォルダ（world）が見つかりません（登録済みフォルダを指定してください）")


def validated_scope(world: str, scope_paths) -> list:
    """world 検証＋scope 検証＋正規化。HTTPException は握りつぶさない。返り値は正規化済み list。"""
    _require_world(world)
    _check_scope(world, scope_paths)
    return scope_mod.normalize_scope_paths(scope_paths)


def _resolve_world(world: str | None) -> str:
    """API の world 解決。`world` 指定を使い、省略時は既定 world。"""
    return world or _DEFAULT_WORLD


def ensure_workspace(uid: str) -> Path:
    """個人 workspace ディレクトリを冪等作成して返す。
    uid は slug 制約でパス注入不可。無効化ユーザーの workspace は消さない。
    """
    # SHERPA_USERS_DIR/{uid}/workspace 配下のみ。共有 KB には触れない。
    base = _USERS_DIR.resolve() / uid / "workspace"
    (base / "outputs").mkdir(parents=True, exist_ok=True)
    (base / "tmp").mkdir(parents=True, exist_ok=True)
    (base / "files").mkdir(parents=True, exist_ok=True)
    return base


def _remove_codex_session_dir(target: Path, base: Path) -> None:
    """`target`（`.codex-sessions/{cid}` 配下の 1 件）を `base` への confinement を再確認して削除する。
    confinement 崩壊は ValueError、rmtree 失敗は OSError。symlink の扱いは呼び出し側が決める。
    """
    target.resolve().relative_to(base.resolve())
    shutil.rmtree(target)


def _delete_codex_sessions_for_conversation(uid: str, cid) -> None:
    """会話削除に伴い、その会話の Codex resume セッション（`workspace/.codex-sessions/{cid}`）を即時削除する。
    共有の soft delete 中でも削除する。fail-open（例外は投げずログ 1 行）。symlink はリンク自体だけ unlink する。
    """
    try:
        udir = _USERS_DIR.resolve() / uid
        sessions_root = udir / "workspace" / ".codex-sessions"
        if sessions_root.is_symlink() or not sessions_root.is_dir():
            return
        sessions_root.resolve().relative_to(udir.resolve())
        target = sessions_root / str(cid)
        if target.is_symlink():
            target.unlink()
            return
        if not target.is_dir():
            return
        _remove_codex_session_dir(target, sessions_root)
    except Exception as e:
        _log.warning("delete_codex_sessions_for_conversation: failed uid=%s cid=%s: %s", uid, cid, e)


def _ensure_initial_admin(ip_hash: str | None = None, user_agent: str | None = None) -> dict | None:
    """初期 admin を冪等作成する。認証無効モードでは DB に触らない。既存 admin はパスワードが空のときだけ初期パスを設定する。"""
    if auth.auth_disabled():
        return None
    pw = auth.initial_admin_password()
    source = "env" if os.environ.get("SHERPA_ADMIN_PASSWORD") else "default"
    admin = store.get_user_by_uid("admin")
    if admin and admin.get("password_hash"):
        return admin
    ph = auth.hash_password(pw)
    row = store.upsert_user(
        "admin",
        email="admin@sherpa.local",
        display_name="Administrator",
        password_hash=ph,
        role="admin",
        status="active",
        must_change_password=True,
    )
    try:
        ensure_workspace("admin")
    except Exception as ws_err:
        _log.warning("workspace provisioning failed for initial admin: %s", ws_err)
    try:
        action = "admin.initial_created" if not admin else "admin.initial_password_set"
        store.audit(
            "system:bootstrap", action, "user", "user:admin",
            detail={"password_source": source, "must_change_password": True},
            outcome="success", severity="critical",
            ip_hash=ip_hash, user_agent=user_agent,
        )
    except Exception:
        _log.critical("audit write failed for initial admin bootstrap")
    return store.get_user_by_uid("admin") or row


def _validate_new_password(uid: str, current_password: str, new_password: str,
                           confirm_password: str) -> str | None:
    """パスワード変更の最小要件。問題がなければ None。"""
    new = new_password or ""
    if new != (confirm_password or ""):
        return "新しいパスワードと確認入力が一致しません"
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in new):
        return "パスワードは半角英数字・記号のみを使ってください（全角文字・空白は使えません）"
    if len(new) < 8:
        return "新しいパスワードは8文字以上にしてください"
    lower = new.lower()
    if new == (current_password or ""):
        return "現在のパスワードとは別のものにしてください"
    if new == auth.initial_admin_password():
        return "初期パスワードと同じものは使えません"
    if "password" in lower or "admin" in lower:
        return "admin や password を含むパスワードは使えません"
    uid_l = (uid or "").lower()
    if len(uid_l) >= 3 and uid_l in lower:
        return "ユーザー名を含むパスワードは使えません"
    return None


def _client_ip_hash(request: Request) -> str | None:
    """IP を HMAC-SHA256 で hash する（生 IP は保存しない）。"""
    import hashlib
    ip = request.client.host if request.client else None
    if not ip:
        return None
    salt = os.environ.get("SHERPA_AUDIT_IP_SALT", "")
    return hashlib.sha256((salt + ip).encode()).hexdigest()


# フォルダ選択（サーバ側エクスプローラー）の許可ルート。

def _browse_roots() -> list:
    """フォルダ選択で辿れるルート（既定 `/mnt:/srv:/home:/Users`・`SHERPA_BROWSE_ROOTS` で `:` 区切り設定）。
    `/` は既定にしない（登録＝共有 KB への公開）。空セグメントは除外し、全部空なら既定へ戻す。
    フォルダ閲覧・登録パス検証の封じ込め境界でもある。
    """
    env = os.environ.get("SHERPA_BROWSE_ROOTS")
    segments = [p for p in env.split(":") if p] if env else []
    return [Path(p) for p in (segments or ["/mnt", "/srv", "/home", "/Users"])]


def _under_roots(p: Path, roots) -> bool:
    try:
        rp = p.resolve()
    except Exception:
        return False
    for r in roots:
        try:
            rr = r.resolve()
            if rp == rr or rp.is_relative_to(rr):
                return True
        except Exception:
            continue
    return False


def _neo4j_driver_config() -> tuple[str, str, str]:
    from sherpa.ingest.world_neo4j import default_neo4j_uri  # 接続先の既定は world_neo4j に一本化
    return (
        default_neo4j_uri(),
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", "sherpa_dev"),
    )


# driver はプロセス内シングルトン。接続先タプルが変わったら作り直す（スレッドセーフ）。
_neo4j_driver_lock = threading.Lock()
_neo4j_driver = None
_neo4j_driver_key: tuple[str, str, str] | None = None


def _driver():
    """プロセス内シングルトンの Neo4j driver（スレッドセーフ・接続先が変わったら作り直す）。接続は `.session()` 使用時まで遅延する。"""
    global _neo4j_driver, _neo4j_driver_key
    key = _neo4j_driver_config()
    with _neo4j_driver_lock:
        if _neo4j_driver is not None and _neo4j_driver_key == key:
            return _neo4j_driver
        stale = _neo4j_driver
        from neo4j import GraphDatabase
        drv = GraphDatabase.driver(
            key[0], auth=(key[1], key[2]),
            notifications_min_severity="OFF",   # 未使用エッジ型の警告などを抑止
        )
        _neo4j_driver = drv
        _neo4j_driver_key = key
    if stale is not None:
        try:
            stale.close()
        except Exception:
            pass
    # ロック内で確定したローカル参照を返す（shutdown との競合で None を返さない）。
    return drv


def close_neo4j_driver() -> None:
    """Neo4j driver を閉じる（`lifespan` の shutdown から）。未生成なら何もしない・多重呼び出し可。"""
    global _neo4j_driver, _neo4j_driver_key
    with _neo4j_driver_lock:
        drv, _neo4j_driver, _neo4j_driver_key = _neo4j_driver, None, None
    if drv is not None:
        try:
            drv.close()
        except Exception:
            _log.warning("Neo4j driver のクローズに失敗しました（プロセス終了時のベストエフォート）", exc_info=True)


atexit.register(close_neo4j_driver)


@contextmanager
def neo4j_session():
    """Neo4j セッションの open/close。streaming は generator の内側で使うこと（iteration 前に session が閉じる）。
    driver は共有し、session だけ毎回 open/close する。
    """
    drv = _driver()
    with drv.session() as s:
        yield s
