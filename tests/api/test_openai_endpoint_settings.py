"""GET /settings の接続先表示フィールド。

`sherpa/routers/system.py::_public_settings` が返す `openai_endpoint_kind`（openai/azure/custom）と
`openai_base_url_host`（ホスト名のみ・キー/パスは含めない）を検証する。接続先の判定そのものは
`sherpa/llm.py::openai_base_url` / `openai_endpoint_kind` の担当。

`_openai_endpoint_kind`/`_openai_base_url_host`（system.py）は `sherpa.llm` の関数を直接呼ぶ契約
（存在しなければ `AttributeError` になる＝欠落を隠す防御を挟まない）。本ファイルはその直接呼びと、
通常の実行時例外（DB/env の想定外値等）への耐性の両方を確認する。

要 Postgres。DB 不可は SKIP（他の tests/api/test_*settings*.py と同じ流儀）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, llm, store
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
    store.upsert_user(uid, email=f"{uid}@openaiendpoint.local", display_name=uid,
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def _admin_client() -> TestClient:
    sfx = _sfx()
    uid, pw = f"oaiepadm{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw, role="admin")
    return _login(uid, pw)


def test_llm_endpoint_helpers_exist_and_are_called_directly():
    """LOW-1（2026-08-18 Codex RV）: `llm.openai_endpoint_kind`/`llm.openai_base_url` は S1 実装が
    着地済み＝存在することを固定する（`getattr(..., None)` の欠落防御は撤去済みなので、これらが
    リネーム/削除されると `_openai_endpoint_kind`/`_openai_base_url_host`（system.py）は
    `AttributeError` で即座に気付ける必要がある＝防御で隠さないことの保険）。"""
    assert callable(getattr(llm, "openai_endpoint_kind", None)), "llm.openai_endpoint_kind が無い"
    assert callable(getattr(llm, "openai_base_url", None)), "llm.openai_base_url が無い"

    from sherpa.routers import system as system_router
    assert system_router._openai_endpoint_kind() in ("openai", "azure", "custom")
    assert isinstance(system_router._openai_base_url_host(), str)


def test_default_env_reports_openai_kind_via_real_get_settings():
    """S1 実装が着地した現状で、`OPENAI_BASE_URL` 未設定の既定環境では GET /settings が
    "openai"（画面には注記を出さない種別）を返すこと（helper 経由の配線そのものの回帰）。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-a{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r = c.get("/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["openai_endpoint_kind"] == "openai"
    assert "openai_base_url_host" in body


def test_reflects_azure_endpoint_kind_and_host_only(monkeypatch):
    """`llm.openai_endpoint_kind`/`openai_base_url` が azure を返すとき、ホスト名だけを返す
    （パスは含めない）。保存済みの値は `_openai_base_url_host` が表示前に
    `llm.assert_openai_base_url_allowed` で再検証するため、クエリを含む値は「不正」（別テスト
    `test_display_shows_fixed_label_when_saved_value_has_query_string` 参照）として扱われる＝
    ここでは検証を通る（クエリなし・パスのみの）値で host 抽出そのものを固定する。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-b{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: "azure", raising=False)
    monkeypatch.setattr(
        llm, "openai_base_url",
        lambda system_settings=None: "https://my-resource.openai.azure.com/openai/v1",
        raising=False,
    )

    r = c.get("/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["openai_endpoint_kind"] == "azure"
    assert body["openai_base_url_host"] == "my-resource.openai.azure.com"
    assert "openai/v1" not in body["openai_base_url_host"]


def test_display_shows_fixed_label_when_saved_value_has_query_string(monkeypatch):
    """`llm.assert_openai_base_url_allowed` はクエリ/フラグメント付きの base_url を不正とする
    （`openai_url()` の単純連結で path がクエリの後ろへ紛れ込むため）。保存時検証の強化より前に
    こうした値が保存されていた場合、表示前の再検証で弾かれ固定文字列を返す（クエリの中身が
    生のまま画面へ出ることはない）。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-q{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: "azure", raising=False)
    monkeypatch.setattr(
        llm, "openai_base_url",
        lambda system_settings=None: "https://my-resource.openai.azure.com/openai/v1/?api-version=2024-10-21",
        raising=False,
    )

    r = c.get("/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["openai_base_url_host"] == "(不正な保存値)"
    assert "api-version" not in body["openai_base_url_host"]


def test_display_falls_back_to_fixed_label_for_invalid_legacy_saved_base_url(monkeypatch):
    """保存時検証（`llm.assert_openai_base_url_allowed`）の強化より前に保存された不正値
    （バックスラッシュ混入等）は、`urlsplit` がこれを構造区切りとして扱わずそのまま `hostname` に
    含めてしまうため、再検証なしでは内部パスの断片が生のまま画面へ出る。再検証して不合格なら
    固定文字列へ倒す。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-f{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: "azure", raising=False)
    monkeypatch.setattr(
        llm, "openai_base_url",
        lambda system_settings=None: "https://host.example\\internal\\secret",
        raising=False,
    )

    r = c.get("/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["openai_base_url_host"] == "(不正な保存値)"
    assert "internal" not in body["openai_base_url_host"]
    assert "secret" not in body["openai_base_url_host"]


def test_reflects_custom_endpoint_kind(monkeypatch):
    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-c{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: "custom", raising=False)
    monkeypatch.setattr(llm, "openai_base_url",
                         lambda system_settings=None: "https://gateway.internal.example.com/v1", raising=False)

    r = c.get("/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["openai_endpoint_kind"] == "custom"
    assert body["openai_base_url_host"] == "gateway.internal.example.com"


def test_endpoint_helper_exception_falls_back_safely(monkeypatch):
    """`llm.openai_base_url()` が例外を投げても GET /settings は 500 にならず、空文字へ倒れる。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-d{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    def _boom(system_settings=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: "azure", raising=False)
    monkeypatch.setattr(llm, "openai_base_url", _boom, raising=False)

    r = c.get("/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["openai_endpoint_kind"] == "azure"
    assert body["openai_base_url_host"] == ""


# ===== POST /settings/test の codex 分岐: 接続先が Azure 等のときは _select_provider と同じ判定
# （`_codex_openai_compat_block_reason`）を共有する。`codex login status`（auth.json のログイン状態）
# だけを見ると、Azure 等では実際には未接続（_UnwiredProvider）になる構成でも接続テストだけ ok=True を
# 返しうるため、判定を一本化してこの食い違いを防ぐ。=====


def test_settings_test_codex_default_endpoint_still_checks_login_status(monkeypatch):
    """既定接続先（openai）では従来どおり `codex login status` を見る（回帰ゼロ）。"""
    import shutil
    import subprocess

    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-e{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    monkeypatch.delenv("SHERPA_CODEX_SANDBOX", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "logged in", "stderr": ""})())

    r = c.post("/settings/test", json={"provider": "codex"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["detail"] == "接続OK"


def test_settings_test_codex_azure_missing_key_reports_reason_without_login_status(monkeypatch):
    """接続先が Azure 等で実キー未設定なら、`codex login status` を呼ばず理由を返す。"""
    import shutil
    import subprocess

    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-f{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    def _boom(*a, **k):
        raise AssertionError("codex login status を呼ぶべきではない（Azure 判定で先に弾かれるはず）")

    monkeypatch.delenv("SHERPA_CODEX_SANDBOX", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: "azure", raising=False)

    r = c.post("/settings/test", json={"provider": "codex", "codex_model": "my-deployment"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "キー" in body["detail"]


def test_settings_test_codex_azure_fully_configured_skips_login_status(monkeypatch):
    """接続先が Azure 等で実キー・デプロイ名が揃っていれば、`codex login status` は問わず
    （＝Azure 等は auth.json でなく env のキーで接続するため）、代わりに実際に1回だけ接続して
    確認する。HTTP 層は `graph_extract.complete_json`（本番のテストシーム）を差し替えて固定し、
    実 Azure API は一切呼ばない。"""
    import shutil
    import subprocess

    from sherpa.ingest import graph_extract

    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-g{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    def _boom(*a, **k):
        raise AssertionError("codex login status を呼ぶべきではない（Azure 構成は判定不要）")

    monkeypatch.delenv("SHERPA_CODEX_SANDBOX", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: "azure", raising=False)
    monkeypatch.setattr(graph_extract, "complete_json",
                         lambda system, user, cfg, **kw: '{"ok":true}')
    # `_codex_openai_compat_block_reason` は共有の `sherpa.keys.resolve_api_key` を通る
    # ため、POST 本文の入力中キーも personal_api_keys_allowed（既定 false）のゲートを通る。
    # モデル名は個人上書きが無い＝カタログに実デプロイ名を設定して判定#5（組み込み既定のまま
    # なら拒否）を通す（`codex_model` を本文で送っても無視される＝この経路では使えない）。
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True,
        "model_catalog": {"codex": {"codex": {"allowed": ["my-deployment"], "default": "my-deployment"}}}})

    r = c.post("/settings/test", json={"provider": "codex", "openai_api_key": "sk-azure-key"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "codex login" in body["detail"]


def test_settings_test_codex_azure_real_connection_failure_is_not_reported_ok(monkeypatch):
    """形式確認（サンドボックス有効・base URL 妥当・実キー・非既定デプロイ名）を通っても、実接続が
    失敗（401 等）すれば `ok=False` を返す（実接続なしに『接続OK』と表示しない）。
    失敗理由にキーが混入しないことも確認する。"""
    import shutil
    import subprocess
    import urllib.error

    from sherpa.ingest import graph_extract

    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-i{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    def _boom(*a, **k):
        raise AssertionError("codex login status を呼ぶべきではない（Azure 構成は判定不要）")

    def _fake_complete_json(system, user, cfg, **kw):
        raise urllib.error.HTTPError("https://myres.openai.azure.com/openai/v1/chat/completions",
                                     401, "Unauthorized", {}, None)

    monkeypatch.delenv("SHERPA_CODEX_SANDBOX", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: "azure", raising=False)
    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)
    # `_codex_openai_compat_block_reason` は共有の `sherpa.keys.resolve_api_key` を通る
    # ため、POST 本文の入力中キーも personal_api_keys_allowed（既定 false）のゲートを通る。
    # モデル名は個人上書きが無い＝カタログに実デプロイ名を設定して判定#5（組み込み既定のまま
    # なら拒否）を通す（`codex_model` を本文で送っても無視される＝この経路では使えない）。
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True,
        "model_catalog": {"codex": {"codex": {"allowed": ["my-deployment"], "default": "my-deployment"}}}})

    r = c.post("/settings/test", json={"provider": "codex", "openai_api_key": "sk-azure-bad-key"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "401" in body["detail"]
    assert "sk-azure-bad-key" not in body["detail"]


def test_settings_test_codex_ollama_construct_ignores_azure_env(monkeypatch):
    """Codex(Ollama) 構成（`codex_model_provider="ollama"`）は Azure 判定の対象外＝従来どおり
    `codex login status` を見る（`_select_provider` の同種分岐と一貫）。"""
    import shutil
    import subprocess

    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-h{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    put = c.put("/settings", json={"codex_model_provider": "ollama"})
    assert put.status_code == 200, put.text

    monkeypatch.delenv("SHERPA_CODEX_SANDBOX", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "logged in", "stderr": ""})())
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: "azure", raising=False)

    r = c.post("/settings/test", json={"provider": "codex"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["detail"] == "接続OK"


def test_settings_test_codex_ollama_unaffected_by_corrupted_openai_endpoint_kind(monkeypatch):
    """Codex(Ollama) 構成は `openai_endpoint_kind()` の解決結果と無関係（`_select_provider` と
    同じ順序＝Ollama 分岐を先に見る）。保存済み中央設定の `openai_endpoint_kind`/`openai_base_url`
    が型破損（JSONB の非文字列値）していて `openai_endpoint_kind()` が `ValueError` を送出する
    状態でも、Codex(Ollama) 利用時は先に Ollama 分岐で確定するためこの呼び出し自体が発生せず、
    接続テストは通常どおり `codex login status` を見て `ok=True` を返す（先に
    `openai_endpoint_kind()` を評価してしまう実装だと、無関係な破損設定のせいで
    false negative になっていた）。"""
    import shutil
    import subprocess

    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-j{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    put = c.put("/settings", json={"codex_model_provider": "ollama"})
    assert put.status_code == 200, put.text

    monkeypatch.delenv("SHERPA_CODEX_SANDBOX", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "logged in", "stderr": ""})())

    def _boom(system_settings=None):
        raise ValueError("接続先設定（openai_endpoint_kind）の保存値が不正です（文字列ではありません）")
    monkeypatch.setattr(llm, "openai_endpoint_kind", _boom, raising=False)

    r = c.post("/settings/test", json={"provider": "codex"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["detail"] == "接続OK"


def test_settings_test_codex_ollama_sandbox_disabled_reports_fail_closed(monkeypatch):
    """Codex(Ollama) 構成で `SHERPA_CODEX_SANDBOX=0`（緊急避難経路）のとき、接続テストは
    `codex login status` を見る前に fail-closed で `ok=False` を返す（`_select_provider` の
    実行時判定と同じ理由を共有する・保存前は緑なのに実行時だけ honest failure になる不整合を防ぐ）。
    `codex login status` 自体は一度も呼ばれない（sandbox 判定が先に確定させる）。"""
    import shutil
    import subprocess

    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-k{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    put = c.put("/settings", json={"codex_model_provider": "ollama"})
    assert put.status_code == 200, put.text

    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    login_calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: (login_calls.append(a) or
                         type("R", (), {"returncode": 0, "stdout": "logged in", "stderr": ""})()))

    r = c.post("/settings/test", json={"provider": "codex"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "サンドボックス" in body["detail"]
    assert login_calls == []   # login status を見る前に fail-closed で確定する


def test_settings_test_codex_azure_branch_shares_one_system_settings_snapshot(monkeypatch):
    """`POST /settings/test`（provider=codex・接続先が Azure 等）は入口で `sys_s` を1回だけ取得し、
    `_codex_openai_compat_block_reason` 内部のキー・モデル解決（Azure 判定）と、その後の実キー
    再解決の両方へそれを渡す契約になっている（`_select_provider` と同じ二重呼び出し経路を
    settings_test 側でも共有する）。個別に読み直すと、接続テストの判定中に admin 設定が
    変わった場合に判定が新旧混在しうる。

    リクエスト本文に `openai_api_key`（入力中の未保存キー）を含めると、`explicit_openai_api_key`
    override と `probe_settings["openai_api_key"]` の両方が短絡し、`keys.resolve_api_key` が
    実際には一度も呼ばれないまま「呼ばれた」ように見えてしまう false green があった。ここでは
    中央キーを snapshot 側に置き本文からは省く＝`resolve_api_key` が実際に実行される経路を通し、
    `resolve_api_key`／`resolve_model` それぞれについて個別に呼び出しの有無と受け取った
    system_settings を確認する（flat なリストの非空確認だけだと、片方が呼ばれなくても
    もう片方の呼び出しに埋もれて検出できない）。"""
    import shutil
    import subprocess

    from sherpa import keys as _keys_mod, model_catalog as _model_catalog_mod
    from sherpa.ingest import graph_extract

    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-j{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    def _boom(*a, **k):
        raise AssertionError("codex login status を呼ぶべきではない（Azure 構成は判定不要）")

    monkeypatch.delenv("SHERPA_CODEX_SANDBOX", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: "azure", raising=False)
    monkeypatch.setattr(graph_extract, "complete_json", lambda system, user, cfg, **kw: '{"ok":true}')

    read_calls = []
    # モデル名は個人上書きが無い＝カタログに実デプロイ名を設定して判定#5（組み込み既定のまま
    # なら拒否）を通す（`codex_model` を本文で送っても無視される＝この経路では使えない）。
    sentinel = {"personal_api_keys_allowed": True, "cloud_provider": "openai",
                "openai_api_key": "sk-central-azure",
                "model_catalog": {"codex": {"codex": {"allowed": ["my-deployment"], "default": "my-deployment"}}}}

    def _spy_get_system_settings():
        read_calls.append(1)
        return sentinel

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy_get_system_settings)

    seen_by_name: dict[str, list] = {}

    def _spy(name, real):
        def _wrapped(*a, **kw):
            seen_by_name.setdefault(name, []).append(kw.get("system_settings"))
            return real(*a, **kw)
        return _wrapped

    monkeypatch.setattr(_keys_mod, "resolve_api_key", _spy("resolve_api_key", _keys_mod.resolve_api_key))
    monkeypatch.setattr(_model_catalog_mod, "resolve_model", _spy("resolve_model", _model_catalog_mod.resolve_model))

    # 本文には openai_api_key を含めない（中央キーの解決経路を実際に通すため）。
    r = c.post("/settings/test", json={"provider": "codex"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"
    required = {"resolve_api_key", "resolve_model"}
    missing = required - set(seen_by_name)
    assert not missing, f"呼ばれなかったヘルパー: {missing}"
    for name, snaps in seen_by_name.items():
        assert snaps, f"{name} が記録されたが呼び出しが空だった（診断ロジック不整合）"
        assert all(snap is sentinel for snap in snaps), \
            f"{name} が settings_test と異なる system_settings オブジェクトを受け取った"


# ===== 一般ユーザーの POST /settings/test は接続先 override を受け付けない =====

def test_settings_test_openai_ignores_endpoint_override_from_general_user(monkeypatch):
    """一般ユーザー（非 admin）が `openai_base_url` 等を本文に含めても、`TestReq` はこれらの
    フィールドをもう宣言していない（pydantic の既定＝未知フィールドは無視）ため、実際の probe は
    保存済みの system_settings のまま行われる（任意の HTTPS 宛先へ中央キーを送信できるSSRFの穴を
    閉じたことの回帰固定）。"""
    from sherpa.ingest import graph_extract

    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-k{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)   # 一般ユーザー（admin ではない）
    c = _login(uid, pw)

    captured = {}

    def _fake_complete_json(system, user, cfg, **kw):
        captured["cfg"] = cfg
        return '{"ok":true}'

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "cloud_provider": "openai", "openai_api_key": "sk-central-real",
        # 接続先は本家のまま（override を試みる攻撃者が任意の値を指定しても、これ以外は使われない）。
    })

    r = c.post("/settings/test", json={
        "provider": "openai",
        "openai_endpoint_kind": "custom",
        "openai_base_url": "https://evil.example.com/v1",
        "openai_auth_header": "api-key",
    })
    assert r.status_code == 200, r.text

    override = captured["cfg"].get("openai_endpoint_override")
    resolved_url = llm.openai_url("chat/completions", system_settings=override)
    assert resolved_url == "https://api.openai.com/v1/chat/completions", \
        f"一般ユーザーが指定した接続先が使われてしまっている: {resolved_url}"


# ===== admin 専用 POST /admin/settings/openai-endpoint-test =====

def test_admin_openai_endpoint_test_requires_admin():
    """一般ユーザーは 403（admin 専用エンドポイントへ分離済み）。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"oaiep-l{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r = c.post("/admin/settings/openai-endpoint-test", json={
        "provider": "openai", "openai_endpoint_kind": "custom",
        "openai_base_url": "https://evil.example.com/v1"})
    assert r.status_code == 403, r.text


def test_admin_openai_endpoint_test_rejects_invalid_base_url_before_probing(monkeypatch):
    """不正な base URL（http:// や userinfo 付き）は 422 で拒否し、`_probe` を一切呼ばない
    （PUT /admin/settings と同じ検証を通信前に共有・SSRF 対策の多層防御）。"""
    from sherpa.ingest import graph_extract

    _try_init()
    c = _admin_client()

    def _boom(*a, **k):
        raise AssertionError("不正な入力なのに complete_json が呼ばれた（通信してしまっている）")

    monkeypatch.setattr(graph_extract, "complete_json", _boom)

    for base_url in ("http://evil.example.com/v1", "https://user:pass@evil.example.com/v1"):
        r = c.post("/admin/settings/openai-endpoint-test", json={
            "provider": "openai", "openai_endpoint_kind": "custom", "openai_base_url": base_url})
        assert r.status_code == 422, r.text


def test_admin_openai_endpoint_test_rejects_non_openai_kind_without_base_url():
    _try_init()
    c = _admin_client()
    r = c.post("/admin/settings/openai-endpoint-test", json={
        "provider": "openai", "openai_endpoint_kind": "azure"})
    assert r.status_code == 422, r.text


def test_admin_openai_endpoint_test_uses_central_key_and_model_not_personal(monkeypatch):
    """A6（個人キー許可）が有効でも、admin 本人の個人キー・個人モデルではなく
    常に中央キー・中央カタログ既定で試す（`/settings/test` の流用をやめた効果の直接確認）。
    `keys.resolve_api_key`/`model_catalog.resolve_model` に渡す `user_settings` が常に `None`
    （個人設定を一切参照しない）であることを直接固定する（実際に個人キーを保存する round-trip は
    A6 の書込み直前再検証（`store.update_settings` の advisory lock 付きチェック）が実 DB の
    `personal_api_keys_allowed` 行を必要とし、共有テスト DB を変更せずに構成しづらいため、
    呼び出し引数を spy する形で固定する）。"""
    from sherpa import keys as _keys_mod, model_catalog as _model_catalog_mod
    from sherpa.ingest import graph_extract

    _try_init()
    c = _admin_client()

    captured = {}

    def _fake_complete_json(system, user, cfg, **kw):
        captured["cfg"] = cfg
        return '{"ok":true}'

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "cloud_provider": "openai", "personal_api_keys_allowed": True,
        "openai_api_key": "sk-central-real",
        "model_catalog": {"openai": {"chat": {"allowed": ["gpt-central-deploy"],
                                              "default": "gpt-central-deploy"}}},
    })
    seen_user_settings = []
    real_resolve_api_key = _keys_mod.resolve_api_key
    real_resolve_model = _model_catalog_mod.resolve_model

    def _spy_resolve_api_key(provider, user_settings, **kw):
        seen_user_settings.append(user_settings)
        return real_resolve_api_key(provider, user_settings, **kw)

    def _spy_resolve_model(provider, usage, user_settings, **kw):
        seen_user_settings.append(user_settings)
        return real_resolve_model(provider, usage, user_settings, **kw)

    monkeypatch.setattr(_keys_mod, "resolve_api_key", _spy_resolve_api_key)
    monkeypatch.setattr(_model_catalog_mod, "resolve_model", _spy_resolve_model)

    r = c.post("/admin/settings/openai-endpoint-test", json={"provider": "openai"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["model"] == "gpt-central-deploy"
    assert captured["cfg"]["key"] == "sk-central-real"
    assert seen_user_settings, "resolve_api_key/resolve_model が呼ばれなかった"
    assert all(us is None for us in seen_user_settings), \
        f"個人設定（user_settings）が参照されている: {seen_user_settings}"


def test_admin_openai_endpoint_test_input_key_overrides_saved_central_key(monkeypatch):
    """保存前の入力中の中央キー（`openai_api_key`）が指定されていれば、それを優先して試す
    （保存済みキーが壊れていても、入力中の新しいキーで試せる）。"""
    from sherpa.ingest import graph_extract

    _try_init()
    c = _admin_client()

    captured = {}

    def _fake_complete_json(system, user, cfg, **kw):
        captured["cfg"] = cfg
        return '{"ok":true}'

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "cloud_provider": "openai", "openai_api_key": "sk-old-saved-key"})

    r = c.post("/admin/settings/openai-endpoint-test", json={
        "provider": "openai", "openai_api_key": "sk-input-in-progress"})
    assert r.status_code == 200, r.text
    assert captured["cfg"]["key"] == "sk-input-in-progress"


def test_admin_openai_endpoint_test_codex_branch_shares_pending_snapshot(monkeypatch):
    """provider=codex も、入力中の接続先 override を同じ pending スナップショットで
    Azure 判定（`_codex_openai_compat_block_reason`）へ渡す。モデルは常に中央カタログ既定で解決する
    （`codex_model` はリクエストで受け付けない）。"""
    from sherpa.ingest import graph_extract

    _try_init()
    c = _admin_client()

    def _fake_complete_json(system, user, cfg, **kw):
        return '{"ok":true}'

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "cloud_provider": "openai", "personal_api_keys_allowed": True,
        # admin がカタログ既定を実際のデプロイ名へ変更済み（接続テスト経由の codex_model 入力は
        # もう受け付けない＝カタログを介さずにモデル名を通す経路は無くなった）。
        "model_catalog": {"codex": {"codex": {"allowed": ["my-azure-deployment"],
                                              "default": "my-azure-deployment"}}}})

    r = c.post("/admin/settings/openai-endpoint-test", json={
        "provider": "codex",
        "openai_endpoint_kind": "azure",
        "openai_base_url": "https://myres.openai.azure.com/openai/v1",
        "openai_api_key": "sk-azure-central-key",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "codex"
    assert body["model"] == "my-azure-deployment"
    assert body["ok"] is True


def test_admin_openai_endpoint_test_codex_ignores_codex_model_field(monkeypatch):
    """`codex_model` をリクエストに送っても無視される（カタログ既定のまま
    解決される・カタログ外モデル名を接続テスト経由で通す経路が無いことの回帰固定）。カタログ既定が
    組み込み既定（gpt-5.5）のままだと Azure 判定がブロックする＝probe まで到達しない。"""
    from sherpa.ingest import graph_extract

    _try_init()
    c = _admin_client()

    called = []

    def _fake_complete_json(system, user, cfg, **kw):
        called.append(cfg)
        return '{"ok":true}'

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "cloud_provider": "openai", "personal_api_keys_allowed": True})   # カタログ既定は変更なし（gpt-5.5）

    r = c.post("/admin/settings/openai-endpoint-test", json={
        "provider": "codex",
        "openai_endpoint_kind": "azure",
        "openai_base_url": "https://myres.openai.azure.com/openai/v1",
        "openai_api_key": "sk-azure-central-key",
        "codex_model": "arbitrary-uncataloged-name",   # pydantic の extra="ignore" で黙って捨てられる
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "codex"
    assert body["model"] == "gpt-5.5"          # カタログ外の入力値は反映されない
    assert body["ok"] is False                 # gpt-5.5 のまま＝Azure 判定がブロック
    assert "デプロイ名" in body["detail"]
    assert called == []                        # ブロックされ probe（complete_json）は一切呼ばれない


def test_admin_openai_endpoint_test_reports_missing_central_key(monkeypatch):
    _try_init()
    c = _admin_client()
    # 実 DB の状態（共有テスト DB に他テストが残した中央キー等）に依存しないよう明示的に空にする。
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"cloud_provider": "openai"})
    r = c.post("/admin/settings/openai-endpoint-test", json={"provider": "openai"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "管理者が" in body["detail"]


def test_admin_openai_endpoint_test_rejects_invalid_cloud_provider_without_probing(monkeypatch):
    """`cloud_provider`（A7）が非空の不正値のとき、非 strict の寛容キー解決で黙って既定
    openai 扱いのキーを実送信しない（課金を伴う接続テストは送信前に strict 検証する）。
    `req.openai_api_key` を明示指定しない経路（保存済みキー解決に頼る）でだけ効く
    （短絡評価で明示キーがあれば resolve_api_key 自体を呼ばないため）。"""
    from sherpa.ingest import graph_extract

    _try_init()
    c = _admin_client()
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"cloud_provider": "not-a-real-provider", "openai_api_key": "sk-central"})

    def _boom(*a, **k):
        raise AssertionError("不正な cloud_provider なのに実送信（complete_json）してしまった")

    monkeypatch.setattr(graph_extract, "complete_json", _boom)
    monkeypatch.setattr(graph_extract, "_probe", lambda *a, **k: _boom())

    r = c.post("/admin/settings/openai-endpoint-test", json={"provider": "openai"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "cloud_provider" in body["detail"]


def _assert_deny_audit_for_invalid_base_url(row: dict) -> None:
    """保存済み `openai_base_url` の再検証が不合格だった場合の監査行の形を厳密に固定する
    （`outcome`/`reason`/`severity`・監査 host が固定文字列であること）。"""
    assert row["outcome"] == "deny"
    assert row["reason"] == "invalid_base_url"
    assert row["severity"] == "warning"
    assert row["detail"]["host"] == "(不正な保存値)"


def test_admin_openai_endpoint_test_rejects_invalid_inherited_saved_base_url(monkeypatch):
    """`openai_base_url` を省略した接続テスト（`PUT`/`_validate_openai_base_url` を経由していない
    保存済み値をそのまま継承する経路）で、その保存済み値が不正（バックスラッシュ混入等）だった
    場合でも、使用直前に再検証して 422 で拒否し、probe（`complete_json`）は一切呼ばない。
    監査行の host にも生の内容を残さず固定文字列に畳む。"""
    from sherpa.ingest import graph_extract

    _try_init()
    c = _admin_client()

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "openai_endpoint_kind": "azure",
        "openai_base_url": "https://host.example\\internal\\secret"})

    def _boom(*a, **k):
        raise AssertionError("不正な保存値なのに complete_json が呼ばれた（通信してしまっている）")

    monkeypatch.setattr(graph_extract, "complete_json", _boom)

    r = c.post("/admin/settings/openai-endpoint-test", json={"provider": "openai"})
    assert r.status_code == 422, r.text

    rows = store.list_audit(action="openai_endpoint.tested", limit=5)
    assert rows, "接続テストの監査行が記録されていない（fail-closed で probe 前に記録する契約）"
    row = rows[0]
    assert "internal" not in row["detail"]["host"]
    assert "secret" not in row["detail"]["host"]
    _assert_deny_audit_for_invalid_base_url(row)


def test_admin_openai_endpoint_test_rejects_non_string_inherited_saved_base_url(monkeypatch):
    """保存済みの `system_settings.openai_base_url` が非文字列（list/dict/int 等・JSONB は型を
    強制しないため理論上あり得る）の場合、`assert_openai_endpoint_consistent`（`.strip()` は
    文字列を前提にしている）の呼び出しで 500 になり監査も残らない、という穴の回帰を固定する。
    型に関わらず実効値の再検証を経て 422 + deny 監査（固定文字列 host）へ倒れることを確認する。

    `{}`/`[]`/`0`/`False` のような **falsy** な非文字列も含める: `str(value or "")` のような
    素朴な falsy 潰しで早期整合チェックを素通りさせると、falsy な非文字列は `or ""` で
    「欠落」に潰れて監査を書く前の 422（`assert_openai_endpoint_consistent` の「base_url 必須」
    エラー）で止まり、deny 監査が残らない（この抜け穴は `llm.openai_base_url()` の同種の
    falsy 潰しと対になっており、接続テストを経ずに直接送信されるパスでは本家 OpenAI への
    誤送信を引き起こす実害と同根）。"""
    from sherpa.ingest import graph_extract

    _try_init()
    c = _admin_client()

    def _boom(*a, **k):
        raise AssertionError("非文字列の保存値なのに complete_json が呼ばれた（通信してしまっている）")

    monkeypatch.setattr(graph_extract, "complete_json", _boom)

    for bad_value in (["https://host.example"], {"nested": "value"}, 12345, {}, [], 0, False):
        monkeypatch.setattr("sherpa.store.get_system_settings", lambda bv=bad_value: {
            "openai_endpoint_kind": "azure", "openai_base_url": bv})

        # ループの前回反復が書いた古い監査行を「今回も書かれた」と誤認しないよう、直前の
        # 最新行 id を基準に**新しい行が実際に増えたこと**を確認する（stale row による false
        # green を避ける）。
        before_rows = store.list_audit(action="openai_endpoint.tested", limit=1)
        before_id = before_rows[0]["id"] if before_rows else None

        r = c.post("/admin/settings/openai-endpoint-test", json={"provider": "openai"})
        assert r.status_code == 422, f"{bad_value!r}: {r.text}"

        rows = store.list_audit(action="openai_endpoint.tested", limit=5)
        assert rows, f"{bad_value!r}: 監査行が記録されていない"
        assert rows[0]["id"] != before_id, f"{bad_value!r}: 新しい監査行が書かれていない（stale row）"
        _assert_deny_audit_for_invalid_base_url(rows[0])


@pytest.mark.parametrize("saved_kind", ["openai", None])
def test_admin_openai_endpoint_test_rejects_falsy_non_string_base_url_when_kind_openai_or_unset(
        monkeypatch, saved_kind):
    """実害の回帰固定: 保存済み `openai_endpoint_kind` が `"openai"`（明示）または未設定
    （`None`＝推定に委ねる）の場合でも、`openai_base_url` が falsy な非文字列（`{}`/`[]`/`0`/
    `False`）なら型検査より先に本家既定へ進む早期 return（`llm.openai_base_url()`/`llm.
    openai_endpoint_kind()` の kind=openai 分岐）で検査を素通りさせない契約（`llm.py` の型検査は
    判定分岐より**先**に行う）。kind=openai／未設定のどちらでも 422 + deny 監査（固定 host）へ
    倒れ、probe（`complete_json`）は一切呼ばれないことを固定する。"""
    from sherpa.ingest import graph_extract

    _try_init()
    c = _admin_client()

    def _boom(*a, **k):
        raise AssertionError("非文字列の保存値なのに complete_json が呼ばれた（通信してしまっている）")

    monkeypatch.setattr(graph_extract, "complete_json", _boom)

    for bad_value in ({}, [], 0, False):
        saved = {"openai_base_url": bad_value}
        if saved_kind is not None:
            saved["openai_endpoint_kind"] = saved_kind
        monkeypatch.setattr("sherpa.store.get_system_settings", lambda sv=saved: dict(sv))

        before_rows = store.list_audit(action="openai_endpoint.tested", limit=1)
        before_id = before_rows[0]["id"] if before_rows else None

        r = c.post("/admin/settings/openai-endpoint-test", json={"provider": "openai"})
        assert r.status_code == 422, f"kind={saved_kind!r} base={bad_value!r}: {r.text}"

        rows = store.list_audit(action="openai_endpoint.tested", limit=5)
        assert rows, f"kind={saved_kind!r} base={bad_value!r}: 監査行が記録されていない"
        assert rows[0]["id"] != before_id, (
            f"kind={saved_kind!r} base={bad_value!r}: 新しい監査行が書かれていない（stale row）")
        _assert_deny_audit_for_invalid_base_url(rows[0])


def test_admin_settings_view_does_not_crash_on_falsy_non_string_saved_base_url(monkeypatch):
    """`llm.openai_base_url()` は非文字列の保存値を `ValueError` で拒否する契約
    （実害の回帰修正）。`GET /admin/settings`（`_admin_settings_view`）はこの `effective.base_url`
    を生成する際に同じ関数を呼ぶため、対策なしではこの型検証強化が admin 設定画面自体のクラッシュ
    （500）という新たな副作用を生む。表示は落とさず固定文字列へ倒すことを固定する。"""
    for bad_value in ({}, [], 0, False, ["https://host.example"]):
        monkeypatch.setattr("sherpa.store.get_system_settings", lambda bv=bad_value: {
            "openai_endpoint_kind": "azure", "openai_base_url": bv})
        c = _admin_client()
        r = c.get("/admin/settings")
        assert r.status_code == 200, f"{bad_value!r}: {r.text}"
        body = r.json()
        assert body["openai_endpoint"]["effective"]["base_url"] == "(不正な保存値)"
        assert body["openai_endpoint"]["configured"]["base_url"] == bad_value
