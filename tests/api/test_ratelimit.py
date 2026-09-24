"""レート制限（監査台帳 2026-07-10-監査対応台帳.md #4）のテスト。

対象:
  1. ログイン失敗バックオフ（同一 uid の連続5失敗→60秒429・成功でリセット）。
  2. ext_api キー単位のレート制限（60req/分・キー間は独立）。

`sherpa.ratelimit` はプロセス内グローバル状態を持つため、各テストは専用の uid/key を使い、
かつテスト前後で `_reset_for_tests()` を呼んで漏れを防ぐ（tests/api の既存流儀通り DB down は SKIP）。
実時間は消費しない（`time.time` を monkeypatch）。
"""
from __future__ import annotations

import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, ratelimit, store
from sherpa.api import app

client = TestClient(app, raise_server_exceptions=False)


from _common import _sfx, _try_init


def _mk_user(uid: str, pw: str) -> None:
    store.upsert_user(uid, email=f"{uid}@rl.local", display_name=uid,
                       password_hash=auth.hash_password(pw), role="user", status="active")
    register_test_uid(uid)


def _mk_admin(uid: str, pw: str) -> None:
    store.upsert_user(uid, email=f"{uid}@rl.local", display_name=uid,
                       password_hash=auth.hash_password(pw), role="admin", status="active")
    register_test_uid(uid)


@pytest.fixture(autouse=True)
def _clean_ratelimit_state():
    ratelimit._reset_for_tests()
    yield
    ratelimit._reset_for_tests()


# ===== ログイン失敗バックオフ =====

def test_login_lockout_after_5_failures_then_correct_password_still_429():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"rla{sfx}", f"Rla{sfx}pw"
    _mk_user(uid, pw)

    # 4回失敗してもまだ通常の401（ロックアウトはしない）。
    for _ in range(4):
        r = client.post("/auth/login", json={"username": uid, "password": "wrong"})
        assert r.status_code == 401, r.text

    # 5回目の失敗でロックアウト開始。
    r = client.post("/auth/login", json={"username": uid, "password": "wrong"})
    assert r.status_code == 401, r.text

    # 6回目（正しいパスワードでも）429。
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 429, r.text
    assert "Retry-After" in r.headers


def test_login_lockout_expires_after_60_seconds_without_real_sleep():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"rlb{sfx}", f"Rlb{sfx}pw"
    _mk_user(uid, pw)

    for _ in range(5):
        client.post("/auth/login", json={"username": uid, "password": "wrong"})

    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 429, r.text

    # 実sleepせず、`time.time` を未来にずらしてロックアウト期限切れを検証する
    # （`ratelimit.check_login_lockout` は `locked_until - now` の比較のみ）。
    real_time = time.time
    try:
        ratelimit.time.time = lambda: real_time() + ratelimit.LOGIN_LOCKOUT_SECONDS + 1
        r = client.post("/auth/login", json={"username": uid, "password": pw})
        assert r.status_code == 200, r.text
    finally:
        ratelimit.time.time = real_time


def test_login_success_resets_counter():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"rlc{sfx}", f"Rlc{sfx}pw"
    _mk_user(uid, pw)

    for _ in range(4):
        r = client.post("/auth/login", json={"username": uid, "password": "wrong"})
        assert r.status_code == 401, r.text

    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, r.text

    # リセット後、また失敗してもすぐには429にならない（カウンタが0から再開している）。
    for _ in range(4):
        r = client.post("/auth/login", json={"username": uid, "password": "wrong"})
        assert r.status_code == 401, r.text


# ===== ext_api キー単位のレート制限 =====

def _make_docx_bytes() -> bytes:
    xml = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>rate limit test</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def _issue_key(label: str) -> dict:
    r = client.post("/ext/v1/admin/keys", json={"label": label})
    assert r.status_code == 200, r.text
    return r.json()


def _convert(api_key: str) -> "object":
    return client.post(
        "/ext/v1/convert",
        files={"file": ("a.docx", io.BytesIO(_make_docx_bytes()), "application/octet-stream")},
        headers={"X-API-Key": api_key},
    )


def test_ext_api_rate_limit_within_and_over_limit_independent_keys():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = f"rld{sfx}", f"Rld{sfx}pw"
    _mk_admin(adm_uid, adm_pw)
    r = client.post("/auth/login", json={"username": adm_uid, "password": adm_pw})
    assert r.status_code == 200, r.text

    key_a = _issue_key(f"rl-a-{sfx}")["key"]
    key_b = _issue_key(f"rl-b-{sfx}")["key"]
    client.post("/auth/logout")

    # しきい値を小さく monkeypatch して実際のリクエスト回数を抑える。
    orig_limit = ratelimit.EXT_API_RATE_LIMIT_PER_MINUTE
    try:
        ratelimit.EXT_API_RATE_LIMIT_PER_MINUTE = 3

        # key_a: 上限内は200が続く。
        for _ in range(3):
            r = _convert(key_a)
            assert r.status_code == 200, r.text

        # key_a: 4回目で429。
        r = _convert(key_a)
        assert r.status_code == 429, r.text
        assert "Retry-After" in r.headers

        # key_b は key_a の影響を受けず200のまま。
        r = _convert(key_b)
        assert r.status_code == 200, r.text
    finally:
        ratelimit.EXT_API_RATE_LIMIT_PER_MINUTE = orig_limit


def test_ext_api_rate_limit_real_threshold_60_allowed_61st_denied():
    """実定数（`EXT_API_RATE_LIMIT_PER_MINUTE`=60）そのものでの境界検証（監査台帳 LOW-5）。

    上の独立性テストはしきい値を3に monkeypatch して確認しているが、監査意図は「実運用の定数
    60req/分で60件は許可・61件目で429になる」ことの直接検証。HTTP経由（convert）だと60回分の
    ドキュメント変換処理で重くなるため、`ratelimit.check_ext_api_rate_limit()` を直接60/61回呼ぶ
    （エンドポイント側の呼び出し方は他のテストで既に確認済み・ここはしきい値ロジックの境界のみ検証）。
    実 sleep はしない。
    """
    assert ratelimit.EXT_API_RATE_LIMIT_PER_MINUTE == 60, "実定数が変わっていないか確認"
    key_id = 999_000_001   # 実キーを介さない専用のダミー key_id（他テストの key_id と衝突しない値）
    for i in range(60):
        assert ratelimit.check_ext_api_rate_limit(key_id) is None, f"{i+1}件目は許可されるはず"
    remaining = ratelimit.check_ext_api_rate_limit(key_id)
    assert remaining is not None, "61件目は拒否されるはず"
    assert remaining > 0


# ===== ext_api キー単位の日次クォータ =====

def test_ext_api_daily_quota_unlimited_when_none():
    """quota=None は常に許可・記録もしない（既存キーは全て None＝無制限のまま）。"""
    key_id = 999_000_002
    for _ in range(1000):
        assert ratelimit.check_ext_api_daily_quota(key_id, None) is None


def test_ext_api_daily_quota_within_and_over_limit_independent_keys():
    key_id_a, key_id_b = 999_000_003, 999_000_004
    for i in range(3):
        assert ratelimit.check_ext_api_daily_quota(key_id_a, 3) is None, f"{i+1}件目は許可されるはず"
    remaining = ratelimit.check_ext_api_daily_quota(key_id_a, 3)
    assert remaining is not None and remaining > 0

    # key_b は key_a と独立（別キー扱い）。
    assert ratelimit.check_ext_api_daily_quota(key_id_b, 3) is None


def test_ext_api_daily_quota_resets_after_24h_without_real_sleep():
    key_id = 999_000_005
    assert ratelimit.check_ext_api_daily_quota(key_id, 1) is None
    assert ratelimit.check_ext_api_daily_quota(key_id, 1) is not None   # 2件目は拒否

    real_time = time.time
    try:
        ratelimit.time.time = lambda: real_time() + ratelimit.EXT_API_DAILY_QUOTA_WINDOW_SECONDS + 1
        assert ratelimit.check_ext_api_daily_quota(key_id, 1) is None, "窓リセット後は再び許可されるはず"
    finally:
        ratelimit.time.time = real_time


# ===== 有効期限・日次クォータの HTTP 経由の検証（401/429・監査記録） =====

def test_ext_api_key_expired_returns_401():
    """発行時は未来日時しか指定できない（`_validate_future_expiry`）ため、ここでは
    「発行時点では有効だったが、その後 expires_at を過ぎた」状態を模す＝発行後に DB の
    `expires_at` を直接過去へ書き換える（実際の経過時間の代わり）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = f"rle{sfx}", f"Rle{sfx}pw"
    _mk_admin(adm_uid, adm_pw)
    r = client.post("/auth/login", json={"username": adm_uid, "password": adm_pw})
    assert r.status_code == 200, r.text
    r = client.post("/ext/v1/admin/keys",
                    json={"label": f"exp-{sfx}", "expires_at": "2099-01-01T00:00:00+00:00"})
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    key_id = r.json()["id"]
    assert r.json()["expires_at"] is not None
    client.post("/auth/logout")

    with store._connect() as c:
        c.execute("UPDATE api_keys SET expires_at = now() - interval '1 day' WHERE id=%s", (key_id,))

    r = _convert(key)
    assert r.status_code == 401, r.text

    # 監査: ちょうど1行・reason=expired・秘密（プレーンキー本体）を含まない。
    rows = store.list_audit(actor=f"ext:{key_id}", action="ext_api.auth_failed", limit=10)
    matching = [row for row in rows if row["reason"] == "expired"]
    assert len(matching) == 1, matching
    assert key not in str(matching[0]["detail"])


def test_ext_api_key_daily_quota_exceeded_returns_429():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = f"rlf{sfx}", f"Rlf{sfx}pw"
    _mk_admin(adm_uid, adm_pw)
    r = client.post("/auth/login", json={"username": adm_uid, "password": adm_pw})
    assert r.status_code == 200, r.text
    r = client.post("/ext/v1/admin/keys", json={"label": f"quota-{sfx}", "daily_quota": 1})
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    key_id = r.json()["id"]
    assert r.json()["daily_quota"] == 1
    client.post("/auth/logout")

    r = _convert(key)
    assert r.status_code == 200, r.text
    r = _convert(key)
    assert r.status_code == 429, r.text
    assert "Retry-After" in r.headers
    # 429 応答本文の文言（固定窓・初回呼び出し起点であることが伝わる表現）を固定する。
    assert "最初の呼び出しから24時間ごとの枠" in r.json()["detail"]

    # 監査: 429（daily_quota_exceeded）はちょうど1行・秘密（プレーンキー本体）を含まない。
    rows = store.list_audit(actor=f"ext:{key_id}", action="ext_api.rate_limited", limit=10)
    matching = [row for row in rows if row["reason"] == "daily_quota_exceeded"]
    assert len(matching) == 1, matching
    assert key not in str(matching[0]["detail"])
