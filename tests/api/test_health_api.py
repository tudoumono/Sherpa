"""GET /health/summary ・ GET /admin/health のゲートテスト（バックエンド健全性の表示）。

health.COMPONENTS を偽 ping に差し替えてから実行する（実ストアへ ping させず高速化・安定化）。
要 Postgres。DB 不可は graceful SKIP（_try_init が False を返す・test_auth_gates.py の流儀を踏襲）。
"""
from __future__ import annotations

import time

import pytest

from _test_users import register_test_uid

# 既定（conftest）の実認証モードで import。
IMPORT_ERROR: Exception | None = None
try:
    from fastapi.testclient import TestClient

    from sherpa import auth, health, store
    from sherpa.api import app
    from sherpa.store import db as store_db
except Exception as e:  # pragma: no cover
    IMPORT_ERROR = e
    TestClient = None  # type: ignore[assignment]
    auth = None  # type: ignore[assignment]
    health = None  # type: ignore[assignment]
    store = None  # type: ignore[assignment]
    store_db = None  # type: ignore[assignment]
    app = None  # type: ignore[assignment]


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    """DB を初期化できるか確認。失敗した場合は SKIP 判定用に False を返す。"""
    if IMPORT_ERROR is not None:
        pytest.skip(f"infra down: {IMPORT_ERROR}")
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"infra down: {e}")   # 不可なら可視の skip（silent-green 根絶）


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _mk_user(uid: str, password: str, *, role: str = "user") -> None:
    store.upsert_user(
        uid,
        email=f"{uid}@health.local",
        display_name=uid.upper(),
        password_hash=auth.hash_password(password),
        role=role,
        status="active",
    )
    register_test_uid(uid)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）


def _login(uid: str, password: str) -> TestClient:
    c = _client(raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, f"login failed for {uid}: {r.status_code} {r.text}"
    return c


def _ok():
    return None


def _ok_ai(_settings, _system_settings=None):
    return None


def _ok_search(_world_id):
    return "テスト用固定応答"


def _patch_components() -> tuple[list, list, list]:
    """実ストアへ ping させないよう COMPONENTS を偽 ping（全成功）に差し替える。戻り値は元の値。

    UI フィードバック4（2026-07-03）: /admin/health は COMPONENTS（状態ドット用の軽量チェック）に
    加えて health._AI_COMPONENTS（管理者本人の設定で実接続確認する深いチェック）も呼ぶため、
    こちらも偽 check（全成功）に差し替える（さもないと未設定のテスト用 admin では実際に AI へ
    到達しようとして失敗し、テストが実ネットワーク状態に依存してしまう）。
    health._SEARCH_COMPONENTS（ES/グラフの実クエリ検索テスト）も同じ理由で偽 probe（全成功）に
    差し替える。
    差し替え時に全キャッシュもリセットする（前のテストの TTL キャッシュを持ち越さないため）。
    """
    original = health.COMPONENTS
    health.COMPONENTS = [
        (comp_id, label, impact, _ok, hint)
        for comp_id, label, impact, _ping, hint in original
    ]
    health._cache = {"at": 0.0, "data": None}
    original_ai = health._AI_COMPONENTS
    health._AI_COMPONENTS = [
        (comp_id, label, impact, _ok_ai, hint)
        for comp_id, label, impact, _check, hint in original_ai
    ]
    health._ai_cache = {}
    original_search = health._SEARCH_COMPONENTS
    health._SEARCH_COMPONENTS = [
        (comp_id, label, impact, _ok_search, hint)
        for comp_id, label, impact, _probe, hint in original_search
    ]
    health._search_cache = {}
    return original, original_ai, original_search


def _restore_components(original) -> None:
    original_sync, original_ai, original_search = original
    health.COMPONENTS = original_sync
    health._cache = {"at": 0.0, "data": None}
    health._AI_COMPONENTS = original_ai
    health._ai_cache = {}
    health._SEARCH_COMPONENTS = original_search
    health._search_cache = {}


def test_health_endpoints_require_login():
    """auth 有効時: セッションなしで /health/summary・/admin/health が 401 を返す。"""
    if not _try_init():
        pytest.skip("infra down")
    original = _patch_components()
    try:
        c = _client(raise_server_exceptions=False)
        r1 = c.get("/health/summary")
        assert r1.status_code == 401, f"expected 401, got {r1.status_code}: {r1.text}"
        r2 = c.get("/admin/health")
        assert r2.status_code == 401, f"expected 401, got {r2.status_code}: {r2.text}"
    finally:
        _restore_components(original)


def test_health_summary_200_and_admin_health_403_for_user():
    """auth 有効時: 一般ユーザーは /health/summary が 200・/admin/health が 403。"""
    if not _try_init():
        pytest.skip("infra down")
    original = _patch_components()
    try:
        sfx = _sfx()
        uid, pw = f"healthusr{sfx}", f"healthusr-pw-{sfx}"
        _mk_user(uid, pw, role="user")
        c = _login(uid, pw)

        r1 = c.get("/health/summary")
        assert r1.status_code == 200, f"expected 200, got {r1.status_code}: {r1.text}"
        data = r1.json()
        assert set(data.keys()) == {"status", "checked_at"}

        r2 = c.get("/admin/health")
        assert r2.status_code == 403, f"expected 403 for non-admin, got {r2.status_code}: {r2.text}"
    finally:
        _restore_components(original)


def test_admin_health_200_with_components_for_admin():
    """auth 有効時: admin は /admin/health が 200 で components が入っている。"""
    if not _try_init():
        pytest.skip("infra down")
    original = _patch_components()
    try:
        sfx = _sfx()
        uid, pw = f"healthadm{sfx}", f"healthadm-pw-{sfx}"
        _mk_user(uid, pw, role="admin")
        c = _login(uid, pw)

        r = c.get("/admin/health?refresh=1")
        assert r.status_code == 200, f"expected 200 for admin, got {r.status_code}: {r.text}"
        data = r.json()
        assert data["status"] == "ok"
        assert data["components"], "components が空です"
        assert all(comp["ok"] for comp in data["components"])
    finally:
        _restore_components(original)


def test_admin_health_merges_ai_snapshot_without_duplicates_and_includes_gemini():
    """UI フィードバック4（2026-07-03）: /admin/health の openai/gemini/bedrock/ollama/codex は
    health.ai_snapshot（管理者本人の設定での実接続確認）の結果に差し替わる（COMPONENTS 側の軽量
    チェックと重複表示されない）。gemini も新たに含まれる（旧実装は含んでいなかった）。"""
    if not _try_init():
        pytest.skip("infra down")
    original = _patch_components()
    try:
        sfx = _sfx()
        uid, pw = f"healthai{sfx}", f"healthai-pw-{sfx}"
        _mk_user(uid, pw, role="admin")
        c = _login(uid, pw)

        r = c.get("/admin/health?refresh=1")
        assert r.status_code == 200, r.text
        data = r.json()
        ids = [comp["id"] for comp in data["components"]]
        assert len(ids) == len(set(ids)), f"component id が重複している: {ids}"
        assert "gemini" in ids, "gemini が components に含まれていない"
        for expected in ("postgres", "neo4j", "elasticsearch", "openai", "bedrock", "ollama", "codex"):
            assert expected in ids, f"{expected} が components から消えている"
    finally:
        _restore_components(original)


def test_admin_health_includes_search_probe_rows():
    """AI との切り分け用に ES/グラフの実クエリ検索テストが components へ2行追加されている
    （`health.search_snapshot`）。"""
    if not _try_init():
        pytest.skip("infra down")
    original = _patch_components()
    try:
        sfx = _sfx()
        uid, pw = f"healthsrch{sfx}", f"healthsrch-pw-{sfx}"
        _mk_user(uid, pw, role="admin")
        c = _login(uid, pw)

        r = c.get("/admin/health?refresh=1")
        assert r.status_code == 200, r.text
        data = r.json()
        by_id = {comp["id"]: comp for comp in data["components"]}
        assert "es_search" in by_id, "es_search が components から消えている"
        assert "graph_search" in by_id, "graph_search が components から消えている"
        assert by_id["es_search"]["label"] == "ES検索（実クエリ）"
        assert by_id["graph_search"]["label"] == "グラフ検索（実クエリ）"
        assert by_id["es_search"]["ok"] is True
        assert by_id["graph_search"]["ok"] is True
    finally:
        _restore_components(original)


def test_health_endpoints_when_session_store_unreachable():
    """認証DB（session_user）到達不可時: /health/summary は 200 で status=down、
    /admin/health は 503（admin 確認ができない以上、詳細は返さない＝情報漏えい防止）。

    ログインで cookie を得てから session_user を壊す（順序が逆だとログイン自体が
    失敗する）。finally で必ず元の session_user に戻す。
    """
    if not _try_init():
        pytest.skip("infra down")
    original = _patch_components()
    try:
        sfx = _sfx()
        uid, pw = f"healthdown{sfx}", f"healthdown-pw-{sfx}"
        _mk_user(uid, pw, role="admin")
        c = _login(uid, pw)

        original_session_user = store.session_user

        def _broken_session_user(token_hash):
            raise RuntimeError("postgresql://user:secretpw@host/db connection failed")

        store.session_user = _broken_session_user
        try:
            r1 = c.get("/health/summary")
            assert r1.status_code == 200, f"expected 200, got {r1.status_code}: {r1.text}"
            data = r1.json()
            assert set(data.keys()) == {"status", "checked_at"}
            assert data["status"] == "down"

            r2 = c.get("/admin/health")
            assert r2.status_code == 503, f"expected 503, got {r2.status_code}: {r2.text}"
        finally:
            store.session_user = original_session_user
    finally:
        _restore_components(original)


# ---- R5: /healthz の schema readiness 連動（2026-07-13-横断レビュー対応.md §3）----
# `store._inited`（facade 経由の re-export）は import 時点のスナップショットで db.py 側の
# 以後の更新には追随しない（`sherpa/store/__init__.py` docstring 参照）ため、readiness を
# 実際に動かすには `sherpa.store.db._inited`（本体のモジュール global）を直接差し替える。


def test_healthz_503_when_schema_not_ready_and_recovers():
    """schema 未適用（`store.schema_ready()` が False）かつ `init_schema` が例外を投げる間は
    503＋`{"ok": False, "detail": "schema not ready"}`。復元後は 200＋`{"ok": True}` に戻る。"""
    if not _try_init():
        pytest.skip("infra down")
    original_inited = store_db._inited
    original_init_schema = store.init_schema
    try:
        store_db._inited = False

        def _boom():
            raise RuntimeError("db unreachable")

        store.init_schema = _boom
        try:
            c = _client(raise_server_exceptions=False)
            r = c.get("/healthz")
            assert r.status_code == 503, f"expected 503, got {r.status_code}: {r.text}"
            assert r.json() == {"ok": False, "detail": "schema not ready"}
        finally:
            store.init_schema = original_init_schema
    finally:
        store_db._inited = original_inited

    # 復元後: 本物の init_schema は実 DB に到達できるため成功し、schema_ready()=True に戻る。
    store_db._inited = False
    try:
        c = _client(raise_server_exceptions=False)
        r = c.get("/healthz")
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
        assert r.json() == {"ok": True}
    finally:
        store_db._inited = original_inited
