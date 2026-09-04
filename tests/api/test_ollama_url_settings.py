"""`ollama_url`（個人の Ollama 接続先）設定の API テスト。

RV是正:
  - item 1: 管理者の中央既定（`system_settings.ollama_url`）が個人未設定の利用者へ実際に効くこと。
    以前は `user_settings.ollama_url` の既定値がハードコード（`http://localhost:11434`）だったため、
    行が無い利用者でも常に個人値が優先扱いとなり、中央既定へ絶対に到達しなかった。
  - item 4: 「管理者の既定を使う」への明示クリア（空文字送信）で既存の個人保存値を消せること。

要 Postgres。DB 不可は SKIP（他の tests/api/test_*settings*.py と同じ流儀）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, keys, store
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
    store.upsert_user(uid, email=f"{uid}@ollamaurl.local", display_name=uid,
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def _admin_client():
    sfx = _sfx()
    uid, pw = f"ourladm{sfx}", f"OurlAdm{sfx}"
    _mk_user(uid, pw, role="admin")
    return _login(uid, pw)


def _clear_system_settings() -> None:
    try:
        with store._connect() as c:
            c.execute("DELETE FROM system_settings")
        store._invalidate_system_settings_cache()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clean_system_settings():
    _clear_system_settings()
    yield
    _clear_system_settings()


def test_fresh_user_ollama_url_is_unset_and_resolves_to_central_default():
    """行が無い（一度も設定を保存していない）利用者は `ollama_url`（生値）が空文字で返り、
    実行時解決（`keys.resolve_ollama_url`）は組み込み既定（localhost）に従う。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"ourlfresh{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    r = c.get("/settings")
    assert r.status_code == 200, r.text
    assert r.json()["ollama_url"] == ""
    assert keys.resolve_ollama_url(store.get_settings(uid)) == keys.DEFAULT_OLLAMA_URL


def test_fresh_user_follows_admin_central_default_not_hardcoded_localhost():
    """重大バグ是正: 管理者が中央既定を localhost 以外（allowlist 登録済みホスト）へ変更すると、
    個人未設定の利用者はその中央既定へ実際に切り替わる（以前は個人側のハードコード既定が常に
    優先され、中央既定へ絶対に到達しなかった）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    r = admin.put("/admin/settings", json={
        "ollama_allowlist": ["10.7.7.7:11434"], "ollama_url": "http://10.7.7.7:11434"})
    assert r.status_code == 200, r.text

    sfx = _sfx()
    uid, pw = f"ourlcentral{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    r2 = c.get("/settings")
    assert r2.status_code == 200, r2.text
    assert r2.json()["ollama_url"] == ""   # 個人は未設定のまま
    assert r2.json()["ollama_url_choice"]["default"] == "http://10.7.7.7:11434"
    assert keys.resolve_ollama_url(store.get_settings(uid)) == "http://10.7.7.7:11434"


def test_saving_unrelated_field_does_not_pin_ollama_url_to_a_stale_value():
    """重大バグ是正: `ollama_url` に触れない PUT（他フィールドだけの保存）を挟んでも、個人の
    `ollama_url` は未設定のまま焼き付かない。以前は行が無い利用者の既定値がハードコードだった
    ため、無関係な保存のたびにその瞬間の（ハードコードの）値が個人設定として確定してしまい、
    後から管理者が中央既定を変えても追従できなくなっていた。"""
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    assert admin.put("/admin/settings", json={
        "ollama_allowlist": ["10.7.7.8:11434"], "ollama_url": "http://10.7.7.8:11434"}).status_code == 200

    sfx = _sfx()
    uid, pw = f"ourlpin{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    assert c.put("/settings", json={"system_prompt": "自分の好み"}).status_code == 200
    assert c.get("/settings").json()["ollama_url"] == ""

    # 管理者が中央既定を変えても、この利用者はまだ個人未設定＝新しい中央既定へ追従する。
    assert admin.put("/admin/settings", json={
        "ollama_allowlist": ["10.7.7.9:11434"], "ollama_url": "http://10.7.7.9:11434"}).status_code == 200
    assert keys.resolve_ollama_url(store.get_settings(uid)) == "http://10.7.7.9:11434"


def test_ollama_url_explicit_empty_string_clears_personal_override():
    """item 4: 空文字を明示送信すると個人保存値をクリアできる（null に変換すると PUT が
    「未指定＝変更しない」と解釈してしまい、一覧外の既存値を選び直しても保存できない不具合の
    往復確認）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"ourlclear{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r1 = c.put("/settings", json={"ollama_url": "http://127.0.0.1:11434"})
    assert r1.status_code == 200, r1.text
    assert c.get("/settings").json()["ollama_url"] == "http://127.0.0.1:11434"

    r2 = c.put("/settings", json={"ollama_url": ""})
    assert r2.status_code == 200, r2.text
    assert r2.json()["ollama_url"] == ""
    assert c.get("/settings").json()["ollama_url"] == ""


def test_ollama_url_choice_prefers_https_current_over_synthesized_http_allowlist_entry():
    """重大バグ是正（RV 2巡目）: 個人の保存値が `https://` で、同じ host:port が admin allowlist
    にも host:port のみで登録されている場合、合成された `http://` エントリが先に採用されて
    https を上書きしない（scheme が分かっている実 URL を allowlist からの合成より先に追加する）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    assert admin.put("/admin/settings", json={"ollama_allowlist": ["ollama.lan:8443"]}).status_code == 200

    sfx = _sfx()
    uid, pw = f"ourlhttps{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    r = c.put("/settings", json={"ollama_url": "https://ollama.lan:8443"})
    assert r.status_code == 200, r.text

    choice = c.get("/settings").json()["ollama_url_choice"]
    assert "https://ollama.lan:8443" in choice["allowed"]
    assert "http://ollama.lan:8443" not in choice["allowed"]   # 合成 http が https を上書きしない


def test_ollama_url_choice_personal_https_replaces_central_http_at_same_host_port():
    """重大バグ是正（RV 3巡目 #8）: 中央既定が `http://host:port`、個人の現在値が同じ host:port の
    `https://` の場合、以前は先勝ち（中央）で個人の https が allowed からも legacy からも消えて
    しまい、実際には有効な現在値が UI で「一覧外」と誤表示されていた。https が同一 host:port の
    http を置換することを固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    assert admin.put("/admin/settings", json={
        "ollama_allowlist": ["ollama.lan:9443"], "ollama_url": "http://ollama.lan:9443"}).status_code == 200

    sfx = _sfx()
    uid, pw = f"ourlhttps2{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    r = c.put("/settings", json={"ollama_url": "https://ollama.lan:9443"})
    assert r.status_code == 200, r.text

    choice = c.get("/settings").json()["ollama_url_choice"]
    assert "https://ollama.lan:9443" in choice["allowed"]
    assert "http://ollama.lan:9443" not in choice["allowed"]
    assert choice["legacy"] is None   # 有効な現在値なので legacy 扱いにもならない


def test_ollama_url_choice_reports_unauthorized_current_as_legacy_not_allowed():
    """重大バグ是正（RV 2巡目）: 許可されなくなった（admin allowlist から削除された）現在の個人
    保存値は `allowed`（選んでも保存できる値の一覧）へ混ぜず、`legacy` として別枠で返す。"""
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    assert admin.put("/admin/settings", json={"ollama_allowlist": ["10.9.9.9:11434"]}).status_code == 200

    sfx = _sfx()
    uid, pw = f"ourllegacy{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    assert c.put("/settings", json={"ollama_url": "http://10.9.9.9:11434"}).status_code == 200

    # admin が後から allowlist からそのホストを削除する（中央既定は別ホストのまま）。
    assert admin.put("/admin/settings", json={"ollama_allowlist": []}).status_code == 200

    choice = c.get("/settings").json()["ollama_url_choice"]
    assert "http://10.9.9.9:11434" not in choice["allowed"]
    assert choice["legacy"] == "http://10.9.9.9:11434"


def test_ollama_url_choice_handles_ipv6_current_value():
    """IPv6 の現在値（角括弧付き）が allowed の完全 URL として round-trip すること。"""
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    assert admin.put("/admin/settings", json={"ollama_allowlist": ["[2001:db8::1]:11434"]}).status_code == 200

    sfx = _sfx()
    uid, pw = f"ourlipv6{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    r = c.put("/settings", json={"ollama_url": "http://[2001:db8::1]:11434"})
    assert r.status_code == 200, r.text

    choice = c.get("/settings").json()["ollama_url_choice"]
    assert "http://[2001:db8::1]:11434" in choice["allowed"]
    assert choice["legacy"] is None
