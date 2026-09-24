"""R2a-S3（2026-07-13 横断レビュー対応・SSRF 封じ）behavioural テスト（FastAPI TestClient・要 Postgres）。

受け入れ①②③（docs/proposals/2026-07-13-横断レビュー対応.md R2a）:
  ① allowlist 未登録の URL（内部 RFC1918/169.254・外部ドメインいずれも）は
     PUT /settings・POST /settings/test とも 4xx。
  ② loopback の既定構成は通る。
  ③ admin が `PUT /admin/settings` で登録した allowlist 済み LAN host:port は通る。

grep 網羅・per-sink degrade・正規化 unit は `tests/contract/test_ssrf_allowlist.py`（DB 不要）側。

system_settings は全体（1世界共有）の KV なので、test_system_settings.py と同じく各テスト前後で
全消去する（他ファイルのテストと並行実行されても allowlist が漏れ出さないようにする）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app
from sherpa.ingest import graph_extract


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")


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


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@ssrf.local", display_name=uid,
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def _user_client():
    sfx = _sfx()
    uid, pw = f"ssrfu{sfx}", f"SsrfU{sfx}"
    _mk_user(uid, pw, role="user")
    return _login(uid, pw)


def _admin_client():
    sfx = _sfx()
    uid, pw = f"ssrfa{sfx}", f"SsrfA{sfx}"
    _mk_user(uid, pw, role="admin")
    return _login(uid, pw)


_UNLISTED_LAN = "http://192.168.1.99:11434"           # RFC1918・admin allowlist 未登録
_UNLISTED_LINK_LOCAL = "http://169.254.169.254:11434"  # クラウドメタデータ相当・admin allowlist 未登録
_UNLISTED_EXTERNAL = "http://ollama.example.com:11434"  # 外部ドメイン・admin allowlist 未登録
_LAN_HOST_PORT = "192.168.1.50:11434"
_LAN_URL = f"http://{_LAN_HOST_PORT}"


# ===== PUT /settings（ollama_url の保存境界） =====

def test_put_settings_ollama_url_loopback_default_allowed():
    if not _try_init():
        pytest.skip("DB down")
    c = _user_client()
    for url in ("http://127.0.0.1:11434", "http://localhost:11434"):
        r = c.put("/settings", json={"ollama_url": url})
        assert r.status_code == 200, r.text
        assert r.json()["ollama_url"] == url


@pytest.mark.parametrize("url", [_UNLISTED_LAN, _UNLISTED_LINK_LOCAL, _UNLISTED_EXTERNAL])
def test_put_settings_ollama_url_rejects_unlisted_url(url):
    """内部（RFC1918・169.254 リンクローカル）・外部いずれも allowlist 未登録なら 4xx。"""
    if not _try_init():
        pytest.skip("DB down")
    c = _user_client()
    before = c.get("/settings").json()["ollama_url"]
    r = c.put("/settings", json={"ollama_url": url})
    assert 400 <= r.status_code < 500, r.text
    assert "ollama.example.com" not in r.text and "192.168.1.99" not in r.text and "169.254" not in r.text  # 到達可否の詳細は返さない
    # 拒否時は保存されない（既存値のまま）。
    assert c.get("/settings").json()["ollama_url"] == before


def test_put_settings_ollama_url_allows_admin_allowlisted_lan_host():
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    ar = admin.put("/admin/settings", json={"ollama_allowlist": [_LAN_HOST_PORT]})
    assert ar.status_code == 200, ar.text
    # effective は DB の admin allowlist（登録した host:port を含む）。env 由来の初回シード
    # （`OLLAMA_URL` 等）が同じ一覧に別のホストを一度きり加えていることもあるため、登録した
    # host:port を含むことだけ確認する（env の構成は環境依存・他要素まで断定しない）。
    assert _LAN_HOST_PORT in ar.json()["ollama_allowlist"]["effective"]

    user = _user_client()
    r = user.put("/settings", json={"ollama_url": _LAN_URL})
    assert r.status_code == 200, r.text
    assert user.get("/settings").json()["ollama_url"] == _LAN_URL


def test_put_settings_ollama_url_omitted_port_does_not_alias_explicit_allowlist_entry():
    """RV HIGH #1（2026-07-14）: admin が `192.168.1.50:11434`（明示ポート）を allowlist に
    登録しても、ポート省略の `http://192.168.1.50`（wire port は実際には 80）は許可されない
    （旧実装は省略ポートを無条件で 11434 とみなしていたため誤って一致・許可してしまっていた）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    assert admin.put("/admin/settings", json={"ollama_allowlist": [_LAN_HOST_PORT]}).status_code == 200

    user = _user_client()
    r = user.put("/settings", json={"ollama_url": "http://192.168.1.50"})   # ポート省略
    assert 400 <= r.status_code < 500, r.text


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434/api/tags",   # path（loopback でも拒否）
    "http://127.0.0.1:11434?x=1",        # query
    "http://127.0.0.1:11434#/api/chat",  # fragment
])
def test_put_settings_ollama_url_rejects_path_query_fragment_even_when_host_allowed(url):
    """RV HIGH #2（2026-07-14）: loopback（常に許可される host）であっても、path/query/fragment を
    含む `ollama_url` は拒否する（`ollama_url(base, path)` の base+path 連結で呼び出し側が意図した
    path を上書き/追加できてしまう「任意パス到達」の防止・詳細は sherpa/llm.py docstring）。"""
    if not _try_init():
        pytest.skip("DB down")
    c = _user_client()
    before = c.get("/settings").json()["ollama_url"]
    r = c.put("/settings", json={"ollama_url": url})
    assert 400 <= r.status_code < 500, r.text
    assert c.get("/settings").json()["ollama_url"] == before   # 拒否時は保存されない


def test_admin_allowlist_ipv6_roundtrips_and_allows(monkeypatch):
    """RV Medium（2026-07-14）: 非 loopback IPv6 の allowlist 登録が round-trip する。
    `[2001:db8::1]:11434` を登録 → 保存も角括弧付きで正規化 → `_allowlisted_hosts()` の再パースと
    一致 → 同 URL の PUT /settings が通る（旧実装は角括弧無しで保存され再パース不能＝正当な IPv6 を
    許可できなかった）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    ar = admin.put("/admin/settings", json={"ollama_allowlist": ["[2001:db8::1]:11434"]})
    assert ar.status_code == 200, ar.text
    stored = ar.json()["ollama_allowlist"]["configured"]
    assert "[2001:db8::1]:11434" in stored                    # 角括弧付きで round-trip
    user = _user_client()
    r = user.put("/settings", json={"ollama_url": "http://[2001:db8::1]:11434/"})
    assert r.status_code == 200, r.text                       # 登録済み IPv6 は許可される


@pytest.mark.parametrize("junk", [
    "127.0.0.1@evil.com:11434",   # userinfo（別ホストに丸められる誤登録の誘発）
    "192.168.1.5:11434/path",     # path
    "192.168.1.5:11434?q=1",      # query
    "http://192.168.1.5:11434",   # scheme 混入
    "192.168.1.5 :11434",         # 空白
])
def test_admin_allowlist_rejects_junk_entries(junk):
    """RV Low（2026-07-14）: userinfo/path/query/scheme/空白を含むエントリは 422（admin の誤登録防止）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    r = admin.put("/admin/settings", json={"ollama_allowlist": [junk]})
    assert r.status_code == 422, r.text


# ===== POST /settings/test（provider=ollama の probe 境界） =====

def test_settings_test_ollama_loopback_default_allowed(monkeypatch):
    """既定（loopback）は probe まで進む（probe 自体は fake で実 I/O を避ける）。"""
    if not _try_init():
        pytest.skip("DB down")
    monkeypatch.setattr(graph_extract, "_probe", lambda cfg: (True, ""))
    c = _user_client()
    r = c.post("/settings/test", json={"provider": "ollama", "ollama_url": "http://127.0.0.1:11434"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


@pytest.mark.parametrize("url", [_UNLISTED_LAN, _UNLISTED_LINK_LOCAL, _UNLISTED_EXTERNAL])
def test_settings_test_ollama_rejects_unlisted_url_without_probing(monkeypatch, url):
    """allowlist 未登録なら probe 自体を1回も呼ばずに 4xx（到達可否の詳細を返す到達オラクルにしない）。"""
    if not _try_init():
        pytest.skip("DB down")

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("allowlist 未登録の接続先なのに probe が呼ばれた（到達オラクルになっている）")
    monkeypatch.setattr(graph_extract, "_probe", _fail_if_called)

    c = _user_client()
    r = c.post("/settings/test", json={"provider": "ollama", "ollama_url": url})
    assert 400 <= r.status_code < 500, r.text


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434/api/tags",   # path（loopback でも拒否）
    "http://127.0.0.1:11434?x=1",        # query
    "http://127.0.0.1:11434#/api/chat",  # fragment
])
def test_settings_test_ollama_rejects_path_query_fragment_without_probing(monkeypatch, url):
    """RV HIGH #2（2026-07-14）: loopback であっても path/query/fragment を含む ollama_url は
    probe 自体を1回も呼ばずに 4xx（`test_put_settings_ollama_url_rejects_path_query_fragment_...`
    と対の書込境界テスト）。"""
    if not _try_init():
        pytest.skip("DB down")

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("path/query/fragment 混入の接続先なのに probe が呼ばれた")
    monkeypatch.setattr(graph_extract, "_probe", _fail_if_called)

    c = _user_client()
    r = c.post("/settings/test", json={"provider": "ollama", "ollama_url": url})
    assert 400 <= r.status_code < 500, r.text


def test_settings_test_ollama_omitted_port_does_not_alias_explicit_allowlist_entry(monkeypatch):
    """RV HIGH #1（2026-07-14）: admin allowlist の明示ポートエントリに、ポート省略の接続先は
    一致しない（`test_put_settings_ollama_url_omitted_port_does_not_alias_...` と対の probe 境界）。"""
    if not _try_init():
        pytest.skip("DB down")

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("ポート省略の接続先なのに probe が呼ばれた（wire-port 不一致のはず）")
    monkeypatch.setattr(graph_extract, "_probe", _fail_if_called)

    admin = _admin_client()
    assert admin.put("/admin/settings", json={"ollama_allowlist": [_LAN_HOST_PORT]}).status_code == 200

    user = _user_client()
    r = user.post("/settings/test", json={"provider": "ollama", "ollama_url": "http://192.168.1.50"})
    assert 400 <= r.status_code < 500, r.text


def test_settings_test_ollama_allows_admin_allowlisted_lan_host(monkeypatch):
    if not _try_init():
        pytest.skip("DB down")
    monkeypatch.setattr(graph_extract, "_probe", lambda cfg: (True, ""))
    admin = _admin_client()
    assert admin.put("/admin/settings", json={"ollama_allowlist": [_LAN_HOST_PORT]}).status_code == 200

    user = _user_client()
    r = user.post("/settings/test", json={"provider": "ollama", "ollama_url": _LAN_URL})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


# ===== POST /settings/test（provider=ollama の probe 失敗詳細の丸め・到達オラクル低減） =====

def test_settings_test_ollama_probe_failure_detail_is_rounded(monkeypatch):
    """Connection refused 等の詳細は丸められる（内部ホスト/ポートの生死判別に使えるオラクルを塞ぐ）。"""
    if not _try_init():
        pytest.skip("DB down")
    monkeypatch.setattr(graph_extract, "_probe", lambda cfg: (False, "Connection refused（サービス停止の可能性）"))
    c = _user_client()
    r = c.post("/settings/test", json={"provider": "ollama", "ollama_url": "http://127.0.0.1:11434"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False
    assert "Connection refused" not in d["detail"]


def test_settings_test_ollama_probe_auth_failure_detail_is_not_rounded(monkeypatch):
    """401 相当の認証失敗だけは詳細を残す（`_round_ollama_probe_detail` の例外）。"""
    if not _try_init():
        pytest.skip("DB down")
    monkeypatch.setattr(graph_extract, "_probe", lambda cfg: (False, "401 unauthorized: bad token"))
    c = _user_client()
    r = c.post("/settings/test", json={"provider": "ollama", "ollama_url": "http://127.0.0.1:11434"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False
    assert d["detail"].startswith("401")
