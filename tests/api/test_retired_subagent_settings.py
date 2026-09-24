"""旧サブエージェント方式（個人設定 `sub_profile`/`sub_planner`・管理設定 `subagent_profiles`）の
撤去に対する負テスト。`test_bedrock_settings.py` の `bedrock_region` 撤去テストと同型:
「送っても保存されない・応答に出ない」に加え、「旧デプロイ由来のレガシー DB 値がテーブルに残って
いても、機能フラグ `SHERPA_SUBAGENTS_ENABLED=1` を立てても、実行時に一切復活しない」ことまで
固定する（撤去はコードの受理側だけでなく実行時参照からも消えている、という契約の直接確認）。

要 Postgres。DB 不可は SKIP（他の tests/api/test_*settings*.py と同じ流儀）。`system_settings`
（`subagent_profiles`／`personal_api_keys_allowed`）は全ユーザー共有の1行KVのため、退避
（`_snapshot_system_settings`）→復元（`_restore_system_settings`）で他レーン・他テストの値を
壊さないようにするが、この退避/復元自体はロックを取らない＝並行実行時の TOCTOU までは埋めない
（他の tests/api/test_system_settings.py 等と同じく、pytest を1本ずつ直列実行する既存の運用を
前提にする）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import agents, auth, store
from sherpa.api import app


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@retiredsub.local", display_name=uid,
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def _admin_client():
    sfx = _sfx()
    uid, pw = f"rsadm{sfx}", f"RsAdm{sfx}"
    _mk_user(uid, pw, role="admin")
    return _login(uid, pw), uid


def _snapshot_system_settings(keys: list[str]) -> dict:
    """指定キーの現在値を退避する（無ければ None）。他レーン／他テストがそのキーに正当な値を
    持っていても、このファイルの一時操作で上書き・削除したままにしない（順序依存の除去）。"""
    with store._connect() as c:
        return {k: (c.execute("SELECT value FROM system_settings WHERE key=%s", (k,)).fetchone() or {}).get("value")
                for k in keys}


def _restore_system_settings(snapshot: dict) -> None:
    """`_snapshot_system_settings` が退避した値へ戻す（None だった＝未設定なら削除・
    それ以外は元の値へ upsert）。"""
    from psycopg.types.json import Json
    with store._connect() as c:
        for k, v in snapshot.items():
            if v is None:
                c.execute("DELETE FROM system_settings WHERE key=%s", (k,))
            else:
                c.execute(
                    "INSERT INTO system_settings (key, value, updated_by) VALUES (%s, %s, 'test') "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                    (k, Json(v)))
    store._invalidate_system_settings_cache()


@pytest.mark.parametrize("field,value", [
    ("sub_profile", "worker"),
    ("sub_planner", "auto"),
])
def test_personal_field_is_retired_and_not_stored(field, value):
    """PUT /settings に含めても 200・保存されない（`SettingsReq` に無い＝未知フィールドとして
    黙って無視される）。`store.get_settings()`／GET /settings のどちらにもキー自体が現れない
    （既定値へのフォールバックではなく、フィールドそのものが撤去済み）。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"rsf{field[-4:]}{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r = c.put("/settings", json={field: value})
    assert r.status_code == 200, r.text
    assert field not in r.json()
    assert field not in store.get_settings(uid)
    assert field not in c.get("/settings").json()


def test_admin_subagent_profiles_field_is_retired_and_not_stored():
    """PUT /admin/settings に `subagent_profiles` を含めても 200・保存されない
    （`SystemSettingsReq` に無い＝未知フィールドとして黙って無視される）。GET /admin/settings の
    応答にもキー自体が現れず、system_settings テーブルにも行が作られない。

    このテスト自身が起点を「未設定」へ明示的に揃えてから確認する（他レーン・過去の実行が
    たまたま残した行の有無に依存すると、PUT の副作用ではない要因で偽の失敗/成功になる）。"""
    _try_init()
    admin, _ = _admin_client()
    snapshot = _snapshot_system_settings(["subagent_profiles"])
    try:
        with store._connect() as c:
            c.execute("DELETE FROM system_settings WHERE key='subagent_profiles'")
        store._invalidate_system_settings_cache()

        r = admin.put("/admin/settings", json={
            "subagent_profiles": [{"id": "x", "name": "x", "provider": "ollama", "model": "",
                                   "tools": ["ripgrep_search"], "enabled": True}]})
        assert r.status_code == 200, r.text
        assert "subagent_profiles" not in r.json()
        assert "subagent_profiles" not in admin.get("/admin/settings").json()

        with store._connect() as c:
            row = c.execute(
                "SELECT 1 FROM system_settings WHERE key='subagent_profiles'").fetchone()
        assert row is None, "未設定の起点から、この PUT が subagent_profiles 行を作った"
    finally:
        _restore_system_settings(snapshot)


def test_legacy_db_values_and_feature_env_do_not_resurrect_sub_candidates(monkeypatch):
    """旧デプロイ由来のレガシー値（管理設定 `subagent_profiles`・個人設定
    `sub_profile`/`sub_planner` の物理列）がテーブルに残っていて、かつ旧機能フラグ
    `SHERPA_SUBAGENTS_ENABLED=1` を立てても、`get_provider()` はもう配線を持たない
    （`Provider._sub_candidates` は常に None のまま＝env ゲートを迂回した「隠れた復活」が無い
    ことの直接確認・`test_bedrock_settings.py::test_bedrock_settings_region_field_is_retired_and_not_stored`
    と同型）。env は `monkeypatch.setenv`（テスト終了時に自動で元の状態へ戻る＝他レーンの値を
    無条件削除しない）を使う。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"rslegacy{sfx}", f"RsLegacy{sfx}"
    _mk_user(uid, pw)

    snapshot = _snapshot_system_settings(["subagent_profiles", "personal_api_keys_allowed"])
    monkeypatch.setenv("SHERPA_SUBAGENTS_ENABLED", "1")
    try:
        # 管理設定側を先に用意する: 旧デプロイの admin 定義プロファイルが system_settings に
        # 残っている状態の再現、および `personal_api_keys_allowed=True`（`get_provider` が実際に
        # provider_id=="openai" の分岐＝旧 `_sub_candidates` 解決ブロックがあった箇所へ入るための
        # 前提・個人キー書込みにも要る＝先に立てておかないと下の update_settings が A6 で弾かれる）。
        from psycopg.types.json import Json
        with store._connect() as c:
            c.execute(
                "INSERT INTO system_settings (key, value, updated_by) VALUES (%s, %s, 'test') "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                ("subagent_profiles", Json([{"id": "legacy", "name": "レガシー", "provider": "ollama",
                                            "model": "", "tools": ["ripgrep_search"], "enabled": True}])))
            c.execute(
                "INSERT INTO system_settings (key, value, updated_by) VALUES (%s, %s, 'test') "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                ("personal_api_keys_allowed", Json(True)))
        store._invalidate_system_settings_cache()

        # 個人設定側: user_settings 行を実体化してから、API 経由ではなく列へ直接レガシー値を書く
        # （`store.update_settings` はこの2列を `_SETTINGS_FIELDS` に持たないため素通りする＝
        # ここでは「昔のデプロイで書かれた値が今も列に残っている」状態を直接再現する）。
        store.update_settings(uid, agent="openai", openai_api_key="sk-legacy")
        with store._connect() as c:
            c.execute("UPDATE user_settings SET sub_profile=%s, sub_planner=%s WHERE user_id=%s",
                      ("legacy-worker", "auto", uid))

        settings = store.get_settings(uid)
        assert "sub_profile" not in settings and "sub_planner" not in settings

        provider = agents.get_provider(settings)
        assert provider.provider_id == "openai", (
            "前提が崩れている（_UnwiredProvider 等に落ちて openai 分岐を検証できていない）")
        assert provider._sub_candidates is None, (
            "レガシー値＋SHERPA_SUBAGENTS_ENABLED=1 でも _sub_candidates は None のまま"
            "（get_provider から subagent_profiles 配線が完全に消えている）")
        assert provider._sub is None, "検索アシスタント未設定のまま _sub が解決されている"
    finally:
        _restore_system_settings(snapshot)


def _legacy_style_wiring_would_activate(user_settings: dict, sys_settings: dict) -> bool:
    """撤去前の `get_provider` が `_sub_candidates`／`_sub` を配線していた条件の最小再現
    （下の変異検知テストの健全性確認専用・本物の `subagent_profiles.py` は削除済みで復活させない）。
    `sub_profile`（単一選択）が system 設定 `subagent_profiles` の enabled 定義に一致するか、
    `sub_planner=='auto'`（複数候補）で enabled 定義が1件以上あれば、旧ロジックは何かを配線して
    いた。"""
    profiles = {p["id"]: p for p in (sys_settings.get("subagent_profiles") or []) if p.get("enabled", True)}
    if user_settings.get("sub_profile") in profiles:
        return True
    if user_settings.get("sub_planner") == "auto" and profiles:
        return True
    return False


def test_flag_and_legacy_keys_together_do_not_wire_sub_candidates(monkeypatch):
    """検知力の直接確認: `store.get_settings()` はもう `sub_profile`/`sub_planner` 列を SELECT
    しないため、前テストのように実データから作った辞書だけでは「旧配線
    （env `SHERPA_SUBAGENTS_ENABLED=1` ＋ system 設定 `subagent_profiles` ＋ user 設定
    `sub_profile`/`sub_planner`）が将来のリグレッションで復活していないか」を検知できない
    （辞書に鍵自体が無いので、復活していても再現しようがない）。ここでは旧方式が実際に活性化して
    いた本来の前提を丸ごと再現し、それでも `_sub_candidates`／`_sub` が配線されないことを固定する。

    健全性チェック: `_legacy_style_wiring_would_activate`（削除済みロジックの最小再現）で、
    まさにこの入力が旧方式なら何かを配線していたはずのシナリオであることを先に確認する
    （でなければ「注入しても何も起きない」のが当然という無意味な負テストになってしまう）。"""
    monkeypatch.setenv("SHERPA_SUBAGENTS_ENABLED", "1")
    sys_settings = {
        "personal_api_keys_allowed": True,
        "subagent_profiles": [{"id": "worker", "name": "実働", "provider": "ollama", "model": "",
                               "tools": ["ripgrep_search"], "enabled": True}],
    }
    # WEB-1: `get_provider()` の1ターン唯一の読取点は `store._read_system_settings_fresh()`
    # （共有キャッシュを介さない生の読取・TOCTOU 対策）——`get_system_settings` ではない。
    monkeypatch.setattr("sherpa.store._read_system_settings_fresh", lambda: sys_settings)
    settings = {
        "agent": "openai", "openai_api_key": "sk-x", "ollama_url": "http://localhost:11434",
        # 旧配線が読んでいたキーを直接注入する（現行コードはこれらのキー名を一切参照しない）。
        "sub_profile": "worker", "sub_planner": "auto",
    }

    assert _legacy_style_wiring_would_activate(settings, sys_settings), (
        "この入力設定は旧ロジックの最小再現でも何も配線しない＝検知テストとして無意味な"
        "シナリオになっている（テスト自体の前提が壊れている）")

    provider = agents.get_provider(settings)
    assert provider.provider_id == "openai", (
        "前提が崩れている（_UnwiredProvider 等に落ちて openai 分岐を検証できていない）")
    assert provider._sub_candidates is None, (
        "env フラグ＋system 設定＋user 設定を旧方式が活性化していた前提どおり揃えても "
        "_sub_candidates が配線された＝get_provider に旧配線が復活している")
    assert provider._sub is None, "search_helper 未設定なのに _sub が解決されている"
