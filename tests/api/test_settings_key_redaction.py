"""キー保護境界の契約テスト（R2b・docs/proposals/2026-07-13-横断レビュー対応.md）。

方針(b)（平文維持・監査台帳 docs/proposals/2026-07-10-監査対応台帳.md #2）が依拠する「保護境界」
＝API 応答からキー値が絶対に漏れないことを、設定系エンドポイントの応答 JSON を**再帰走査**して担保する。

対象キーは `SettingsReq`（sherpa/routers/system.py）のフィールドから `*_api_key` サフィックスで
動的に検出する（openai/gemini/bedrock を決め打ちしない）＝将来の設定項目追加にもマスク規律が
自動的に追従することの担保（監査台帳 :147 の懸念）。

検証する2点:
  (a) 保存した生キー文字列が応答のどこにも（部分一致でも）現れない。
  (b) `*_api_key` サフィックスのキーが応答に生値のまま出ない（出るなら `*_key_set` の bool のみ）。

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app
from sherpa.routers.system import SettingsReq

# SettingsReq のフィールドから "*_api_key" を動的検出（openai/gemini/bedrock を決め打ちしない）。
_KEY_FIELDS = [name for name in SettingsReq.model_fields if name.endswith("_api_key")]


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _mk_user(uid: str, password: str) -> None:
    store.upsert_user(uid, email=f"{uid}@keyredact.local", display_name=uid,
                      password_hash=auth.hash_password(password), role="user", status="active")
    register_test_uid(uid)


@pytest.fixture
def _personal_keys_allowed_in_db():
    """`store.update_settings()` の個人キー書込みは、A6（`personal_api_keys_allowed`）を実 DB から
    直接（`sherpa.store.get_system_settings` の monkeypatch を経由せず）再確認する（advisory lock
    付き）。個人キーを実際に PUT するテストのために実 DB にも `personal_api_keys_allowed=True` を
    書き、テスト終了後は元の値（行の有無まで含む）へ復元する（他テストと共有する実 DB の状態を
    汚さない）。DB 不可はテスト側の `_try_init()` が別途 skip する。"""
    try:
        store.init_schema()
        with store._connect() as c:
            prev_row = c.execute(
                "SELECT value FROM system_settings WHERE key='personal_api_keys_allowed'").fetchone()
    except Exception:
        yield
        return
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": True})
    try:
        yield
    finally:
        store.set_system_settings(
            "admin-uid", {"personal_api_keys_allowed": bool(prev_row["value"]) if prev_row else None})


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def _iter_leaves(obj):
    """dict/list を再帰的に辿り、全ての葉値（プリミティブ）を列挙する。"""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_leaves(v)
    else:
        yield obj


def _iter_dict_keys(obj):
    """dict/list を再帰的に辿り、全ての dict キー（と対応する値）を列挙する。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _iter_dict_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dict_keys(v)


def _assert_body_never_leaks(body: dict, raw_values: list[str], where: str) -> None:
    # (a) 保存した生キー文字列が応答のどこにも（部分一致でも）現れない。
    for leaf in _iter_leaves(body):
        if not isinstance(leaf, str):
            continue
        for raw in raw_values:
            assert raw not in leaf, f"{where}: 生キー値が応答の文字列に含まれている（leaf={leaf!r}）"
    # (b) "*_api_key" サフィックスのキーは応答に存在しないか、存在してもブールのみ（生値ではない）。
    for k, v in _iter_dict_keys(body):
        if k.endswith("_api_key"):
            assert isinstance(v, bool), (
                f"{where}: '{k}' が bool でない値（{v!r}）で応答に出ている"
                "（*_api_key サフィックスは *_key_set ブールのみを許す契約）")


def test_settings_endpoints_never_leak_raw_keys_and_mask_future_fields(monkeypatch, _personal_keys_allowed_in_db):
    """SettingsReq の *_api_key 全フィールドを保存し、PUT 応答・GET /settings・GET /config を
    再帰走査してキー値の非露出とマスク規律を確認する（監査台帳 #2 :147 の懸念に対する回帰防止）。

    個人キーの保存/使用には `personal_api_keys_allowed`
    （既定 false）が要る。また `*_key_set` は「今この設定で実際に使えるキーがあるか」
    （`sherpa.keys.resolve_api_key`・A7 排他込み）を返すため、3種のキーを同時保存しても
    `*_key_set` が同時に true になるのは選択中のクラウドプロバイダ（`cloud_provider`）1つだけ。
    漏洩しないことの確認（全フィールドまとめて1回の PUT/GET）は従来どおり行い、
    `*_key_set` は各プロバイダを順に選択し直して個別に確認する。
    """
    if not _try_init():
        pytest.skip("DB down")
    assert _KEY_FIELDS, "SettingsReq に *_api_key フィールドが1つも無い（検出ロジックの前提が崩れている）"
    sfx = _sfx()
    uid, pw = f"keyred{sfx}", f"KeyRed{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    raw_values = [f"{field}-secret-{sfx}" for field in _KEY_FIELDS]
    payload = dict(zip(_KEY_FIELDS, raw_values))

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})

    r = c.put("/settings", json=payload)
    assert r.status_code == 200, r.text
    _assert_body_never_leaks(r.json(), raw_values, "PUT /settings")

    s = c.get("/settings")
    assert s.status_code == 200, s.text
    body = s.json()
    _assert_body_never_leaks(body, raw_values, "GET /settings")
    # 各キーの set フラグが true になることを、そのプロバイダを選択中（A7）にして確認する
    # （マスクした結果が空でないことの担保・非選択プロバイダは false になるのが正しい新契約）。
    for field in _KEY_FIELDS:
        provider = field.rsplit("_api_key", 1)[0]
        flag_name = provider + "_key_set"
        monkeypatch.setattr(
            "sherpa.store.get_system_settings",
            lambda p=provider: {"personal_api_keys_allowed": True, "cloud_provider": p})
        body = c.get("/settings").json()
        assert body.get(flag_name) is True, (
            f"{flag_name} が true になっていない（{field} 保存後・cloud_provider={provider}）")

    cfg = c.get("/config")
    assert cfg.status_code == 200, cfg.text
    _assert_body_never_leaks(cfg.json(), raw_values, "GET /config")


def test_settings_key_fields_are_write_only_from_the_start():
    """未設定（保存前）でも `*_api_key` フィールドは応答に一切現れない（bool の `*_key_set` のみ）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"keyredz{sfx}", f"KeyRedZ{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    s = c.get("/settings")
    assert s.status_code == 200, s.text
    body = s.json()
    for k, v in _iter_dict_keys(body):
        if k.endswith("_api_key"):
            assert isinstance(v, bool), f"'{k}' が生値で応答に出ている（未設定状態でも bool のみのはず）"
    for field in _KEY_FIELDS:
        flag_name = field.rsplit("_api_key", 1)[0] + "_key_set"
        assert body.get(flag_name) is False, f"{flag_name} は未設定なら false のはず"
