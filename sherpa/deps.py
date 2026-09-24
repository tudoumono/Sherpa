"""api.py 共有依存ヘルパ（フェーズ3スライス1・2・3・4・5・8・純移動）。

`_current_user` / `_require_admin` / `validated_scope`（および validated_scope 専用の
private ヘルパ `_require_world` / `_check_scope`）に加え、`ensure_workspace` /
`_USERS_DIR` / `_validate_new_password`（スライス2・admin_users router との共有ヘルパ）、
`_client_ip_hash`（スライス3・shares router と sherpa/routers/auth.py の共有ヘルパ）、
`_resolve_world` / `_DEFAULT_WORLD` / `_WORLD_PATTERN` / `_WorldField`
（スライス4・conversations/documents router と api.py 残留の複数ハンドラが共有）、
`neo4j_session` / `_driver`（スライス5・impact/graph router と api.py 残留のチャット系
ハンドラが共有する Neo4j セッションの open/close ラッパー）、`_browse_roots` / `_under_roots`
（スライス6・worlds router の fs_list/_resolve_root と api.py 残留の _warn_browse_roots_missing
〔lifespan 起動処理〕が共有するフォルダ選択許可ルートのヘルパ）、`_ensure_initial_admin`
（スライス8・sherpa/routers/auth.py の auth_login と api.py 残留の _auth_bootstrap_on_startup
〔lifespan 起動処理〕が共有する初期 admin 冪等作成ヘルパ）
を api.py から抽出する。
このモジュールは `sherpa.api` を import しない（循環回避）。api.py 側の再エクスポートは
repo 内に現存参照が残る名前だけの**選択的維持**（例: ensure_workspace）。`_current_user`・
`_require_admin` 等は router 側が deps を直接 import する（api.py の注記どおり api 経由の
互換参照は非目標。`api._current_user` 等の現存参照ゼロは 2026-07-13 に grep 確認済み）。
"""
from __future__ import annotations

import atexit
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import Field

from sherpa import auth, store, worlds
from sherpa import scope as scope_mod

_log = logging.getLogger("sherpa")

# セッション Cookie 名（sherpa/routers/auth.py の `_set_session_cookie`/`_clear_session_cookie` 等と共有）。
_COOKIE = "sherpa_session"
# 個人 workspace のルート（uid は slug 制約でパス注入不可）。env 読みはここ1箇所のみ
# （api.py は再エクスポートで参照する・二重読みしない）。
_USERS_DIR = Path(os.environ.get("SHERPA_USERS_DIR", "data/users"))
# world 識別子は英数字＋限定記号（`/`・`..` 不可＝パストラバーサル防止）。
_WORLD_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
# API 既定 world（リテラル "v1" の単一の真実源＝worlds.default_world）。
_DEFAULT_WORLD = worlds.default_world()
# API パラメータ用の world Field（語彙統一フェーズ2・第2段で旧 `version` の受理は終了済み）。
_WorldField = Field(default=None, pattern=_WORLD_PATTERN)


def _synthetic_admin() -> dict:
    """SHERPA_AUTH_DISABLED=1 時に返す合成 admin ユーザー（DB 無し・既存テスト互換）。"""
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
    """world が解決できない（未登録・参照元不在・fixtures フラグ無し）なら 404。

    未知/不在の world_id で Neo4j を world_id 直読みさせない＝**本番で残存 fixture グラフ等を参照しない**担保（RV High）。
    既定 world='v1' で来ても、本番（フラグ無し・data/kb/v1 不在）は world_dir が None＝ここで弾く。
    """
    if not worlds.world_dir(world):
        raise HTTPException(404, "資料フォルダ（world）が見つかりません（登録済みフォルダを指定してください）")


def validated_scope(world: str, scope_paths) -> list:
    """world 検証＋scope 検証＋正規化を1本に（rv B2）。HTTPException は呼び元へ素通し（握りつぶさない）。返り値＝正規化 list。"""
    _require_world(world)
    _check_scope(world, scope_paths)
    return scope_mod.normalize_scope_paths(scope_paths)


# 語彙統一（フェーズ2・第2段・2026-07-13）: API パラメータは `world` のみ。旧 `version` の互換受理
# （第1段・deprecation 警告つき）は終了済み。DB 列 `version` は不変（スコープ外）。
def _resolve_world(world: str | None) -> str:
    """API の world 解決: `world` 指定を使う／省略時は既定 world。

    鏡モデル（MIRROR-MODEL）は「版」概念を撤去済み＝API 表面は `world` のみ（旧 `version` は
    第2段で受理終了・未宣言パラメータとして黙って無視される＝FastAPI/Pydantic の既定動作）。
    """
    return world or _DEFAULT_WORLD


def ensure_workspace(uid: str) -> Path:
    """個人 workspace ディレクトリを冪等作成して返す。
    uid は slug 制約で `/`・`..` を持てない（api.py の `_UID_PATTERN`）→ パス注入不可。
    無効化ユーザーの workspace は消さない（実装計画 §1 決定）。
    """
    # 不変条件: SHERPA_USERS_DIR/{uid}/workspace 配下のみ。共有 KB には触れない。
    base = _USERS_DIR.resolve() / uid / "workspace"
    (base / "outputs").mkdir(parents=True, exist_ok=True)
    (base / "tmp").mkdir(parents=True, exist_ok=True)
    (base / "files").mkdir(parents=True, exist_ok=True)
    return base


def _ensure_initial_admin(ip_hash: str | None = None, user_agent: str | None = None) -> dict | None:
    """初期 admin を冪等作成する。

    認証無効モードでは DB に触らない。既存 admin のパスワードが空の場合だけ初期パスを設定する。
    既にパスワードがある admin は、display_name などを上書きしない。
    """
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
    """IP を HMAC-SHA256 で hash（生 IP は保存しない）。salt なし MVP = 単純 SHA-256。"""
    import hashlib
    ip = request.client.host if request.client else None
    if not ip:
        return None
    salt = os.environ.get("SHERPA_AUDIT_IP_SALT", "")
    return hashlib.sha256((salt + ip).encode()).hexdigest()


# ===== フォルダ選択（サーバ側エクスプローラー・/mnt 等の許可ルート配下に限定）=====
# スライス6・worlds router（fs_list/_resolve_root）と api.py 残留の _warn_browse_roots_missing
# （lifespan 起動処理）が共用するため deps.py へ移動。

def _browse_roots() -> list:
    """フォルダ選択で辿れるルート（既定 `/mnt:/srv:/home`）。`SHERPA_BROWSE_ROOTS`（`:`区切り）で設定可。

    既定は実データの置き場の定番3箇所（/mnt=Windowsドライブ・SMBマウント、/srv=サーバ配置、
    /home=ユーザー領域）に限る（2026-09-04 裁定）。`/` を既定にしない——登録＝共有KBへの公開
    （鏡モデル）であり、/etc 等のシステム領域を誤登録すると全利用者へ晒される。roots は
    フォルダ閲覧/登録パス検証の封じ込め境界でもある（secRV）。

    空セグメント（例 `SHERPA_BROWSE_ROOTS=/srv/data:` の末尾など）は除外する。
    `Path("")` は cwd 扱いになり、警告抑止や許可ルートへの意図しない cwd 混入を招くため。
    全セグメントが空なら既定にフォールバックする（未設定時と同じ扱い）。
    """
    env = os.environ.get("SHERPA_BROWSE_ROOTS")
    segments = [p for p in env.split(":") if p] if env else []
    return [Path(p) for p in (segments or ["/mnt", "/srv", "/home"])]


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
    from sherpa.ingest.world_neo4j import default_neo4j_uri   # 接続先の既定は world_neo4j に一本化
    return (
        default_neo4j_uri(),
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", "sherpa_dev"),
    )


# QW2（性能台帳#17）: 以前は `neo4j_session()` の呼び出しごとに driver を新規生成し `finally` で
# 即 close していた——driver 自体が内部にコネクションプールを持つため、リクエスト毎に作り直すと
# ハンドシェイクを毎回支払っていた（1ターンで複数回呼ばれる・ポーリングでも呼ばれる）。
# プロセス内シングルトンへ差し替える。接続先（URI/user/password）は env 由来で通常は起動後
# 変わらないが、**世代キー**（このタプル自体）が変わったら安全に作り直す（テストの monkeypatch
# や将来の設定変更に追随する・スレッドセーフ）。
_neo4j_driver_lock = threading.Lock()
_neo4j_driver = None
_neo4j_driver_key: tuple[str, str, str] | None = None


def _driver():
    """プロセス内シングルトンの Neo4j driver（スレッドセーフ・世代キー切替）。

    `GraphDatabase.driver(...)` はこの呼び出し自体では実際に接続しない（lazy＝`verify_connectivity()`
    を呼ばない限り TCP は張らない）——差し替え前の「呼び出しごとに新規 driver」と同じく、
    接続の生成自体は `.session()` 使用時まで遅延する。健全性確認（`verify_connectivity`）は
    `health.py`/`agentic_search.py` が別途持つ独立の一時 driver で行っており（本関数は関与しない・
    変更なし）、ここでは「driver オブジェクト＝内部コネクションプールをリクエスト間で使い回す」
    ことだけを行う。
    """
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
    # RV代替 M3: ロック解放後にグローバルを再読みすると、shutdown（`close_neo4j_driver()`）と
    # 競合した場合に None を返しうる（別スレッドが `_neo4j_driver=None` へ戻した後にここへ
    # 到達する余地があるため）。ロック内で確定させたローカル参照を返す。
    return drv


def close_neo4j_driver() -> None:
    """Neo4j driver を閉じる（`sherpa.lifespan` の shutdown から呼ぶ想定・`atexit` にも保険登録済み）。

    未生成なら何もしない。多重呼び出しは安全（`neo4j.Driver.close()` は冪等）。呼び出し後に
    `_driver()` が再度呼ばれれば新しい driver を遅延生成する。
    """
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
    """Neo4j セッションの open/close を1本化（rv B2）。**streaming は generator の内側で使う**こと
    （wrapper に generator を返させると iteration 前に session が閉じる・Codex 指摘）。

    driver 自体はプロセス内シングルトン（`_driver()`・QW2）——session だけを毎回 open/close する。
    以前の「毎回 driver ごと close」はしない（他の同時実行中セッションを巻き添えで壊すため）。
    """
    drv = _driver()
    with drv.session() as s:
        yield s
