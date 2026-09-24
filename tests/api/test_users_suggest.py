"""GET /users/suggest（バッチ2・5番・2026-07-03）: 共有ダイアログの入力補完。

ログイン必須・active ユーザーのみ・uid/表示名の部分一致・limit 10・自分自身は除外・
返すのは uid と display_name のみ。DB 不可は graceful SKIP（既存ファイルの流儀）。
"""
from __future__ import annotations

import time

import pytest

IMPORT_ERROR: Exception | None = None
try:
    from fastapi.testclient import TestClient

    from sherpa import auth, store
    from sherpa.api import app
except Exception as e:  # pragma: no cover
    IMPORT_ERROR = e
    TestClient = None  # type: ignore[assignment]


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    if IMPORT_ERROR is not None:
        pytest.skip(f"infra down: {IMPORT_ERROR}")
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"infra down: {e}")


def _mk_user(uid: str, password: str, *, display_name: str | None = None, status: str = "active") -> None:
    from _test_users import register_test_uid
    store.upsert_user(uid, email=f"{uid}@suggest.local", display_name=display_name or uid.upper(),
                      password_hash=auth.hash_password(password), role="user", status=status)
    register_test_uid(uid)


def _login(uid: str, password: str) -> "TestClient":
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return c


def test_users_suggest_requires_login():
    if not _try_init():
        pytest.skip("infra down")
    anon = TestClient(app, raise_server_exceptions=False)
    r = anon.get("/users/suggest", params={"q": "a"})
    assert r.status_code == 401


def test_users_suggest_empty_query_returns_empty_list():
    """空クエリは全ユーザー一覧化を避けるため空配列（typo で全件が漏れないように）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"usg-empty{sfx}", f"UsgEmpty{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    r = c.get("/users/suggest", params={"q": ""})
    assert r.status_code == 200
    assert r.json() == {"users": []}


def test_users_suggest_escapes_percent_and_underscore_wildcards():
    """RV MEDIUM（2026-07-03再検証）: `q=%`/`q=_` は ILIKE ワイルドカードとしてではなく
    リテラル文字として扱われる（`%` が全 active ユーザーに一致し「空クエリで全件化しない」
    意図をユーザー入力1文字で迂回できてしまう問題の修正・回帰テスト）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    me_uid, me_pw = f"usgwc{sfx}", f"UsgWc{sfx}"
    _mk_user(me_uid, me_pw)
    other_uid = f"usgwcother{sfx}"
    _mk_user(other_uid, f"UsgWcP{sfx}", display_name="ワイルドカード対象")
    c = _login(me_uid, me_pw)

    # 候補が実在し、通常の部分一致では見つかることの確認（このあとの「一致無し」に意味を持たせる）。
    r_normal = c.get("/users/suggest", params={"q": other_uid})
    assert r_normal.status_code == 200
    assert any(u["uid"] == other_uid for u in r_normal.json()["users"])

    # "%" はワイルドカードでなくリテラル文字扱い＝どの uid/display_name にも含まれないため一致なし。
    r_percent = c.get("/users/suggest", params={"q": "%"})
    assert r_percent.status_code == 200
    assert r_percent.json() == {"users": []}, "q='%' が全 active ユーザーに一致してしまっている"

    # "_" も同様にリテラル扱い。
    r_underscore = c.get("/users/suggest", params={"q": "_"})
    assert r_underscore.status_code == 200
    assert r_underscore.json() == {"users": []}, "q='_' が意図せず1文字ワイルドカードとして一致している"


def test_users_suggest_matches_uid_and_display_name_excludes_self_and_disabled():
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    me_uid, me_pw = f"usgme{sfx}", f"UsgMe{sfx}"
    _mk_user(me_uid, me_pw, display_name="検索者")
    match_by_uid = f"usgtanaka{sfx}"
    _mk_user(match_by_uid, f"UsgP1{sfx}", display_name="山田太郎")
    match_by_name = f"usgyamada{sfx}"
    _mk_user(match_by_name, f"UsgP2{sfx}", display_name="田中花子")
    disabled_uid = f"usgtandisabled{sfx}"
    _mk_user(disabled_uid, f"UsgP3{sfx}", display_name="田中無効", status="disabled")
    unrelated_uid = f"usgnomatch{sfx}"
    _mk_user(unrelated_uid, f"UsgP4{sfx}", display_name="無関係太郎")

    c = _login(me_uid, me_pw)

    # "tan"（uid部分一致）と "田中"（表示名部分一致）の両方が拾われることを個別に確認。
    r_uid = c.get("/users/suggest", params={"q": f"usgtanaka{sfx}"[:10]})
    assert r_uid.status_code == 200
    uids_by_uid_match = {u["uid"] for u in r_uid.json()["users"]}
    assert match_by_uid in uids_by_uid_match
    assert disabled_uid not in uids_by_uid_match, "無効化ユーザーが含まれている"

    r_name = c.get("/users/suggest", params={"q": "田中"})
    assert r_name.status_code == 200
    result_name = r_name.json()["users"]
    uids_by_name = {u["uid"] for u in result_name}
    assert match_by_name in uids_by_name
    assert disabled_uid not in uids_by_name, "無効化ユーザー（表示名も一致）が含まれている"
    assert me_uid not in uids_by_name

    # 返す列は uid/display_name のみ（email 等は含まない）。
    row = next(u for u in result_name if u["uid"] == match_by_name)
    assert set(row.keys()) == {"uid", "display_name"}


def test_users_suggest_excludes_self_even_when_query_matches_own_uid():
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"usgself{sfx}", f"UsgSelf{sfx}"
    _mk_user(uid, pw, display_name="自分自身")
    c = _login(uid, pw)
    r = c.get("/users/suggest", params={"q": uid})
    assert r.status_code == 200
    assert r.json() == {"users": []}, "自分自身が候補に含まれている"


def test_users_suggest_limits_to_10():
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    me_uid, me_pw = f"usglimme{sfx}", f"UsgLimMe{sfx}"
    _mk_user(me_uid, me_pw)
    for i in range(12):
        _mk_user(f"usglim{sfx}n{i:02d}", f"UsgLimP{i:02d}{sfx}", display_name=f"候補{i:02d}")
    c = _login(me_uid, me_pw)
    r = c.get("/users/suggest", params={"q": f"usglim{sfx}"})
    assert r.status_code == 200
    assert len(r.json()["users"]) == 10
