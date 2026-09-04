"""POST /settings/test（接続テスト）のモデル名解決を検証する。

重大バグ是正: 以前は空選択時に `model_catalog` ではなく旧ハードコード既定
（"gpt-5.5"／"gemini-2.5-flash"／"qwen2.5"）を使っていたため、管理者がカタログ既定を実際の
デプロイ名（Azure 等）へ変えていても、接続テストだけは古い既定のまま失敗し得た（実行時に使われる
モデルとテストで確認するモデルがずれる）。`model_catalog.resolve_model` に統一する。

セキュリティ是正: `TestReq` にモデル名欄（openai_model/gemini_model/ollama_model/codex_model）は
無い（`/settings/test` はログイン済みなら誰でも呼べる＝管理者確認もレート制限も無いため、任意の
モデル名を受け取ると一般ユーザーが実 probe（外部 API への実リクエスト）へ任意の値を到達させられて
しまう）。リクエスト本文にこれらのキーを含めても pydantic の `extra="ignore"` で黙って無視され、
probe は常に管理者のカタログ解決値で実行される（Bedrock だけ実在確認済みモデルの専用機構
（`bedrock_model`）があり例外）。

要 Postgres。DB 不可は SKIP（他の tests/api/test_*settings*.py と同じ流儀）。ネットワーク I/O が
発生しない分岐だけを使う（codex は CLI 不在で早期 return・openai/gemini は鍵未設定で早期 return）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
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
    store.upsert_user(uid, email=f"{uid}@testresolve.local", display_name=uid,
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def _admin_client():
    sfx = _sfx()
    uid, pw = f"strsladm{sfx}", f"StrslAdm{sfx}"
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
def _clean_system_settings(monkeypatch):
    _clear_system_settings()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    yield
    _clear_system_settings()


def test_settings_test_ignores_stale_personal_model_column_uses_catalog_default():
    """個人設定に保存済みの（カタログ導入以前の自由入力時代の）モデル名が残っていても、
    `/settings/test` は一切読まずカタログ既定のみで解決する（個人上書きは撤去済み）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"strslclr{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    # 保存済みの（カタログ外）旧値を直接仕込む（PUT /settings 経由だとカタログ検証で弾かれるため
    # store を直接使う＝旧・自由入力時代に保存された値を模す）。
    store.update_settings(uid, openai_model="stale-legacy-model")

    resp = c.post("/settings/test", json={"provider": "openai"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == "gpt-5.5"   # カタログ既定（組み込み既定）へ解決される
    assert resp.json()["model"] != "stale-legacy-model"


def test_settings_test_rejects_unknown_body_fields_silently_openai_model():
    """`TestReq` に openai_model 欄は無い。リクエスト本文に含めても pydantic の `extra="ignore"`
    で黙って無視され、任意のモデル名が実 probe へ到達しない（バリデーションエラーにもならない＝
    既存フロントの互換動作）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"strslinj{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    resp = c.post("/settings/test", json={"provider": "openai", "openai_model": "attacker-chosen-model"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == "gpt-5.5"
    assert resp.json()["model"] != "attacker-chosen-model"


def test_settings_test_codex_empty_model_uses_catalog_default_not_hardcoded():
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    r = admin.put("/admin/settings", json={
        "model_catalog": {"codex": {"codex": {"allowed": ["custom-codex-deploy"],
                                              "default": "custom-codex-deploy"}}}})
    assert r.status_code == 200, r.text

    sfx = _sfx()
    uid, pw = f"strslcx{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    resp = c.post("/settings/test", json={"provider": "codex"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == "custom-codex-deploy"


def test_settings_test_codex_injected_model_ignored_uses_catalog_default():
    """`codex_model` は `TestReq` に無い欄＝送っても無視され、常にカタログ解決値で probe される
    （一般ユーザーが任意のモデル名を実 probe へ到達させられないことの回帰）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    assert admin.put("/admin/settings", json={
        "model_catalog": {"codex": {"codex": {"allowed": ["custom-codex-deploy"],
                                              "default": "custom-codex-deploy"}}}}).status_code == 200

    sfx = _sfx()
    uid, pw = f"strslcx2{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    resp = c.post("/settings/test", json={"provider": "codex", "codex_model": "attacker-chosen-model"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == "custom-codex-deploy"
    assert resp.json()["model"] != "attacker-chosen-model"


def test_settings_test_codex_invalid_catalog_model_grammar_rejected_before_login_status_check(monkeypatch):
    """`codex login status` はモデル名を見ない（ログイン状態だけを見る）ため、文法として不正な
    モデル名（`CodexProvider` が実行時に拒否する値）でも CLI さえログイン済みなら ok=True を
    返しかねない。共通文法（`CODEX_MODEL_NAME_RE`）を subprocess 呼び出しより前に確認し、不正な
    非空値は ok=False とする（CLI の有無やログイン状態を問わない）。

    モデル名は個人上書きが無い＝常に管理者カタログの解決値。この文法チェックが実際に効くのは
    `PUT /admin/settings`（`model_catalog.validate_catalog` で通常は弾かれる）を迂回してカタログに
    直接不正な値が入った場合の多層防御であり、ここでは store を直接操作してその状況を再現する
    （一般ユーザーがリクエスト本文でモデル名を注入する経路は無い＝そちらは他テストで確認済み）。

    CLI が「無い」テスト環境では、文法検証コードを丸ごと削除しても `shutil.which("codex")`
    早期 return が偶然 ok=False を返し、文法検証自体が効いているのか区別できない false green
    だった。ここでは CLI が「ある」・ログインも「成功する」健全な状態を偽装し、`subprocess.run`
    自体を失敗させない（文法検証が無ければ ok=True になるはず）。その上で ok=False かつ detail が
    文法エラーであること（CLI 不在メッセージではないこと）を固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None)

    class _FakeCompletedProcess:
        returncode = 0
        stdout = "Logged in using ChatGPT"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _FakeCompletedProcess())

    # `PUT /admin/settings` の検証（`model_catalog.validate_catalog`）を迂回して不正な文法の
    # モデル名をカタログへ直接仕込む（管理者の正規の保存経路では起こり得ない状態を再現する）。
    store.set_system_settings("system", {"model_catalog": {"codex": {"codex": {
        "allowed": ["bad model name!"], "default": "bad model name!"}}}})

    sfx = _sfx()
    uid, pw = f"strslcxbad{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    resp = c.post("/settings/test", json={"provider": "codex"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["model"] == "bad model name!"
    assert "モデル名の形式が不正です" in body["detail"]
    assert "CLI" not in body["detail"]   # CLI 不在の早期 return とは違う理由で拒否されている


def test_settings_test_openai_empty_model_uses_catalog_default_not_hardcoded():
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    r = admin.put("/admin/settings", json={
        "model_catalog": {"openai": {"chat": {"allowed": ["custom-openai-deploy"],
                                              "default": "custom-openai-deploy"}}}})
    assert r.status_code == 200, r.text

    sfx = _sfx()
    uid, pw = f"strslo{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    resp = c.post("/settings/test", json={"provider": "openai"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False   # 鍵未設定のため早期 return（ネットワーク I/O なし）
    assert body["model"] == "custom-openai-deploy"


def test_settings_test_openai_injected_model_ignored_uses_catalog_default():
    """`openai_model` はリクエスト本文に含めても `TestReq` に欄が無く無視される（一般ユーザーが
    任意のモデル名で実 probe に到達させられないことの回帰）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    r = admin.put("/admin/settings", json={
        "model_catalog": {"openai": {"chat": {"allowed": ["custom-openai-deploy"],
                                              "default": "custom-openai-deploy"}}}})
    assert r.status_code == 200, r.text

    sfx = _sfx()
    uid, pw = f"strsloinj{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    resp = c.post("/settings/test", json={"provider": "openai", "openai_model": "attacker-chosen-model"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == "custom-openai-deploy"
    assert body["model"] != "attacker-chosen-model"


def test_settings_test_gemini_empty_model_uses_catalog_default_not_hardcoded():
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    r = admin.put("/admin/settings", json={
        "model_catalog": {"gemini": {"chat": {"allowed": ["custom-gemini-deploy"],
                                              "default": "custom-gemini-deploy"}}}})
    assert r.status_code == 200, r.text

    sfx = _sfx()
    uid, pw = f"strslg{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    resp = c.post("/settings/test", json={"provider": "gemini"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["model"] == "custom-gemini-deploy"


def test_settings_test_gemini_injected_model_ignored_uses_catalog_default():
    """`gemini_model` はリクエスト本文に含めても `TestReq` に欄が無く無視される。"""
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    r = admin.put("/admin/settings", json={
        "model_catalog": {"gemini": {"chat": {"allowed": ["custom-gemini-deploy"],
                                              "default": "custom-gemini-deploy"}}}})
    assert r.status_code == 200, r.text

    sfx = _sfx()
    uid, pw = f"strslginj{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    resp = c.post("/settings/test", json={"provider": "gemini", "gemini_model": "attacker-chosen-model"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == "custom-gemini-deploy"
    assert body["model"] != "attacker-chosen-model"


def test_settings_test_ollama_injected_model_ignored_uses_catalog_default():
    """`ollama_model` はリクエスト本文に含めても `TestReq` に欄が無く無視される
    （`ollama_url` は個人設定として残る欄のため引き続き受け付ける＝モデル名だけが対象）。"""
    if not _try_init():
        pytest.skip("DB down")
    admin = _admin_client()
    r = admin.put("/admin/settings", json={
        "model_catalog": {"ollama": {"chat": {"allowed": ["custom-ollama-deploy"],
                                              "default": "custom-ollama-deploy"}}}})
    assert r.status_code == 200, r.text

    sfx = _sfx()
    uid, pw = f"strslolinj{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    resp = c.post("/settings/test", json={"provider": "ollama", "ollama_model": "attacker-chosen-model",
                                          "ollama_url": "http://127.0.0.1:11434"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == "custom-ollama-deploy"
    assert body["model"] != "attacker-chosen-model"


@pytest.mark.parametrize("provider", ["openai", "gemini", "bedrock"])
def test_settings_test_rejects_invalid_cloud_provider_without_probing(monkeypatch, provider):
    """`cloud_provider`（A7）が非空の不正値のとき、非 strict の寛容キー解決で黙って既定
    openai 扱いのキーで実送信しない（課金を伴う接続テストは送信前に strict 検証する）。
    openai/gemini/bedrock いずれも `keys.resolve_api_key(..., strict=True)` を経由し、
    `InvalidCloudProviderConfigError` を honest failure（`ok: False`）へ変換する。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa.ingest import graph_extract

    def _boom(*a, **k):
        raise AssertionError(f"不正な cloud_provider なのに {provider} へ実送信してしまった")

    monkeypatch.setattr(graph_extract, "_probe", _boom)
    monkeypatch.setattr("sherpa.agents.BedrockProvider.probe", lambda self: _boom())
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"cloud_provider": "not-a-real-provider"})
    sfx = _sfx()
    uid, pw = f"strslinv{sfx}{provider[:2]}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    resp = c.post("/settings/test", json={"provider": provider})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["provider"] == provider
    assert "cloud_provider" in body["detail"]
