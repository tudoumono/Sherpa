"""Codex(OpenAI) 構成の接続先を `sherpa.llm.openai_base_url()`/`openai_endpoint_kind()` 経由で
Azure OpenAI 等へ向けられるようにする一連の変更の単体テスト。

SET-2c（接続先の UI 移管）以降、接続先は env でなく `system_settings`（DB）が唯一の真実源。
このファイルは `sysset` フィクスチャ（可変 dict）で `sherpa.store.get_system_settings` を
差し替え、各テストが辞書へ直接キーを足すことで接続先を制御する。

テスト範囲:
  1. `_openai_compat_provider_lines`（sandbox.py・単体）: 既定/Azure base/api-version/
     bearer・api-key ヘッダモードの行生成。
  2. `_write_codex_authoring_config`（統合）: 接続先が既定(OpenAI)のときは従来どおり
     `model_provider`/`model_providers` を一切書かない（回帰ゼロ）。Azure 等へリダイレクトされて
     いる時だけ行が現れ、TOML として妥当で、api-key モードでもキーの**値**が config に現れない
     （env_http_headers は env 変数名だけを書く）。
  3. web_search: `_web_search_disabled_value`/`_write_codex_authoring_config` は接続先が Azure 等の
     ときは admin 許可・ユーザー設定に関わらず常に無効化する。`_web_search_endpoint_note` は
     「本来 ON だったはずが Azure のせいで OFF」の時だけ理由文言を返す。
  4. `_codex_clean_env`: `openai_api_key` を明示的に渡した時だけ子プロセス env に
     `OPENAI_API_KEY` が乗る（省略時は従来どおり乗らない＝
     test_codex_clean_env_has_no_secrets 等の既存契約と非衝突）。
  5. `_select_provider`（providers/__init__.py）: 既定接続先では無改修（openai_api_key は常に
     None）。Azure 等へリダイレクトされている時だけ、判定を共有する
     `_codex_openai_compat_block_reason` 経由で以下を順に検出して正直に未接続を返す:
     サンドボックス無効（fail-closed）／base URL 不正／実キー未設定／`codex_model` 未設定・
     **既定値のまま**（"gpt-5.5" も未設定扱い）。全部揃えば `CodexProvider` へ `openai_api_key` を
     渡す。Codex(Ollama) 構成は本機能の対象外のまま。「OpenAI 直結」構成（agent=openai）にも
     同じ既定値ガードを入れた（一貫性のため）。

実 Codex CLI・実 Azure/OpenAI は一切呼ばない（config.toml の生成物とその中身だけを検証する）。
"""
from __future__ import annotations

import pathlib

import pytest


@pytest.fixture(autouse=True)
def sysset(monkeypatch):
    """このファイル全体の system_settings 差し替え（可変 dict）。各テストは辞書へ直接キーを
    足して `sherpa.llm` の接続先解決（openai_endpoint_kind/openai_base_url/openai_auth_header/
    openai_api_version）を制御する。既定は個人キー許可のみ（Azure/base URL 判定は A6/A7 とは
    別軸のため・本ファイルは Azure/base URL 判定の検証が目的で、各テストが settings に直接書く
    `openai_api_key` が解決されるよう個人キーの利用を許可しておく）。"""
    state = {"personal_api_keys_allowed": True}
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: dict(state))
    return state


@pytest.fixture(autouse=True)
def _codex_cli_present(monkeypatch):
    """`_select_provider` の codex 分岐は `shutil.which("codex")` の有無を先に見る。本ファイルは
    その後段（キー・デプロイ名・サンドボックス・base URL）を検証するため、開発機に実際に Codex CLI が
    入っているかどうかに関わらず「ある」ことに固定する（CLI 不在の分岐は別テストの対象）。"""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)


def _azure(sysset, **extra) -> None:
    """`sysset` に Azure 接続先（既定）を書き込む（追加キーは `extra` で上書き）。"""
    sysset["openai_endpoint_kind"] = "azure"
    sysset["openai_base_url"] = "https://myres.openai.azure.com/openai/v1"
    sysset.update(extra)


# ===== 1. _openai_compat_provider_lines（行生成・単体） =====

def test_openai_compat_provider_lines_bearer_no_api_version():
    from sherpa.providers.codex.sandbox import _openai_compat_provider_lines

    lines = _openai_compat_provider_lines(
        "https://myres.openai.azure.com/openai/v1", api_version=None, auth_header="bearer")
    txt = "\n".join(lines)
    assert 'model_provider = "sherpa-openai-compat"' in txt
    assert "[model_providers.sherpa-openai-compat]" in txt
    assert 'base_url = "https://myres.openai.azure.com/openai/v1"' in txt
    assert 'env_key = "OPENAI_API_KEY"' in txt
    assert 'wire_api = "responses"' in txt
    # 予約語 `openai`/`ollama`/`lmstudio` は使わない（Codex は組込み provider の上書きを拒否する）。
    assert "sherpa-openai-compat" != "openai"
    assert "query_params" not in txt, "api_version 無しなのに query_params が出ている"
    assert "http_headers" not in txt, "bearer モードで余計なヘッダ行が出ている"


def test_openai_compat_provider_lines_with_api_version():
    from sherpa.providers.codex.sandbox import _openai_compat_provider_lines

    lines = _openai_compat_provider_lines(
        "https://myres.openai.azure.com/openai/v1", api_version="2025-04-01-preview", auth_header="bearer")
    txt = "\n".join(lines)
    assert 'query_params = { "api-version" = "2025-04-01-preview" }' in txt


def test_openai_compat_provider_lines_api_key_mode_uses_env_http_headers_not_literal():
    from sherpa.providers.codex.sandbox import _openai_compat_provider_lines

    lines = _openai_compat_provider_lines(
        "https://myres.openai.azure.com/openai/v1", api_version=None, auth_header="api-key")
    txt = "\n".join(lines)
    assert 'env_http_headers = { "api-key" = "OPENAI_API_KEY" }' in txt
    # `http_headers`（静的値・キーの値そのものを書く形）は使わない。
    assert "http_headers = {" not in txt or "env_http_headers" in txt
    assert not any(ln.startswith("http_headers") for ln in lines), \
        "静的 http_headers（キー値 literal）を書いてしまっている"


def test_openai_compat_provider_lines_toml_string_escaping():
    """base_url に TOML の特殊文字が混じっても `_toml_str` でエスケープされ、壊れた TOML にならない。"""
    from sherpa.providers.codex.sandbox import _openai_compat_provider_lines

    lines = _openai_compat_provider_lines(
        'https://evil".openai.azure.com/v1', api_version=None, auth_header="bearer")
    txt = "\n".join(lines)
    assert '\\"' in txt, "ダブルクオートがエスケープされていない"
    try:
        import tomllib
        tomllib.loads("model = \"x\"\n" + txt)
    except ModuleNotFoundError:
        pass


# ===== 2. _write_codex_authoring_config（統合） =====

def _config_text(tmp_path, **kw) -> str:
    from sherpa.providers.codex.sandbox import _write_codex_authoring_config
    ch = tmp_path / "ch"
    _write_codex_authoring_config(ch, ["/kb"], "low", False, "test", None, **kw)
    return (ch / "config.toml").read_text(encoding="utf-8")


def test_default_endpoint_writes_no_model_provider_lines(tmp_path):
    """接続先が既定（OpenAI 本家・system_settings 未設定）のときは従来どおり何も書かない（回帰ゼロ）。"""
    txt = _config_text(tmp_path)
    assert "model_provider" not in txt
    assert "model_providers" not in txt
    assert "sherpa-openai-compat" not in txt


def test_azure_endpoint_writes_model_provider_lines(tmp_path, sysset):
    _azure(sysset)
    txt = _config_text(tmp_path)
    assert 'model_provider = "sherpa-openai-compat"' in txt
    assert "[model_providers.sherpa-openai-compat]" in txt
    # 末尾スラッシュは `openai_base_url()` 側で落とされる。
    assert 'base_url = "https://myres.openai.azure.com/openai/v1"' in txt
    assert 'wire_api = "responses"' in txt
    try:
        import tomllib
        tomllib.loads(txt)
    except ModuleNotFoundError:
        pass


def test_azure_endpoint_with_api_version_and_api_key_header(tmp_path, sysset):
    _azure(sysset, openai_auth_header="api-key", openai_api_version="2024-10-21")
    txt = _config_text(tmp_path)
    assert 'query_params = { "api-version" = "2024-10-21" }' in txt
    assert 'env_http_headers = { "api-key" = "OPENAI_API_KEY" }' in txt
    # キーの値そのもの（テストでは未設定なので、そもそも「値」が config に literal で出ないことを、
    # 変数名だけが書かれていることで確認する＝実キーを渡しても中身は変わらない設計）。
    assert "OPENAI_API_KEY" in txt
    for line in txt.splitlines():
        if line.strip().startswith(("query_params", "env_http_headers", "base_url", "env_key")):
            assert "sk-" not in line, f"キーらしき文字列が config に出ている: {line!r}"


def test_ollama_construct_ignores_azure_settings(tmp_path, sysset):
    """Codex(Ollama) 構成（`ollama_base_url` あり）は接続先設定と無関係＝従来どおり ollama 行だけ。"""
    _azure(sysset)
    from sherpa.providers.codex.sandbox import _write_codex_authoring_config
    ch = tmp_path / "ch"
    _write_codex_authoring_config(ch, ["/kb"], "low", False, "test", None,
                                  ollama_base_url="http://127.0.0.1:11500/")
    txt = (ch / "config.toml").read_text(encoding="utf-8")
    assert 'model_provider = "sherpa-ollama"' in txt
    assert "sherpa-openai-compat" not in txt


# ===== 多層防御: sandbox.py 側でも base URL を検証する =====

def test_openai_compat_base_url_validates_and_raises_for_invalid_url(sysset):
    """`_openai_compat_base_url()` 自体が base URL を検証する（`_select_provider` の判定を迂回する
    経路があっても、不正な URL が config.toml に書かれてキーが誤った宛先へ渡らない・多層防御）。"""
    from sherpa.providers.codex.sandbox import _openai_compat_base_url

    _azure(sysset, openai_base_url="http://myres.openai.azure.com/openai/v1")
    with pytest.raises(ValueError):
        _openai_compat_base_url()


def test_write_codex_authoring_config_raises_for_invalid_base_url(tmp_path, sysset):
    """`_write_codex_authoring_config` を直接呼ぶ（`_select_provider` の判定を経由しない）場合でも、
    不正な base URL は `ValueError` で拒否される（呼び出し元 `provider.py` の broad except に乗って
    安全に degrade する設計・ここでは例外がそこまで伝播することそのものを確認する）。"""
    from sherpa.providers.codex.sandbox import _write_codex_authoring_config

    _azure(sysset, openai_base_url="http://myres.openai.azure.com/openai/v1")
    ch = tmp_path / "ch"
    with pytest.raises(ValueError):
        _write_codex_authoring_config(ch, ["/kb"], "low", False, "test", None)


@pytest.mark.parametrize("bad_base_url", [{}, [], 0, False])
def test_write_codex_authoring_config_raises_for_falsy_non_string_base_url_instead_of_degrading(
        tmp_path, sysset, bad_base_url):
    """実害の回帰固定: 保存済み `openai_base_url` が `{}`/`[]`/`0`/`False` のような falsy な
    非文字列の場合、`llm.openai_base_url()` の素朴な falsy 潰しに乗って本家 OpenAI 既定 URL へ
    黙って縮退し、config.toml が本家向けに（web_search 制限なども Azure 判定を外れたまま）
    書かれてしまってはならない（kind=azure なのに Azure 向けの資格情報が本家へ渡る事故になる）。
    `_openai_compat_base_url`/`_write_codex_authoring_config` の両方が `ValueError` で拒否し、
    本家向けの config.toml が書かれないことを固定する。"""
    from sherpa.providers.codex.sandbox import _openai_compat_base_url, _write_codex_authoring_config

    _azure(sysset, openai_base_url=bad_base_url)
    with pytest.raises(ValueError):
        _openai_compat_base_url()
    ch = tmp_path / "ch"
    with pytest.raises(ValueError):
        _write_codex_authoring_config(ch, ["/kb"], "low", False, "test", None)


# ===== 3. web_search の強制 OFF =====

def test_web_search_disabled_value_forces_off_for_non_openai_endpoint():
    """WEB-1: 管理者許可は system_settings.web_search_allowed（env はもう見ない）。"""
    from sherpa.providers.codex.sandbox import _web_search_disabled_value

    allowed = {"web_search_allowed": True}
    # endpoint_kind 省略（既定 "openai"）なら従来どおり admin+user 両方 True で有効化される。
    assert _web_search_disabled_value(True, system_settings=allowed) is None
    # 明示 "openai" も同じ。
    assert _web_search_disabled_value(True, "openai", allowed) is None
    # Azure/custom は admin 許可・ユーザー希望に関わらず常に disabled。
    assert _web_search_disabled_value(True, "azure", allowed) == "disabled"
    assert _web_search_disabled_value(True, "custom", allowed) == "disabled"
    assert _web_search_disabled_value(False, "azure", allowed) == "disabled"


def test_config_web_search_forced_off_when_azure_even_if_admin_and_user_allow(tmp_path, sysset):
    _azure(sysset)
    sysset["web_search_allowed"] = True   # 管理者許可あり（system_settings・sysset 経由で DB モック）
    txt = _config_text(tmp_path, web_search_enabled=True)
    assert 'web_search = "disabled"' in txt, \
        "admin許可+ユーザー希望でも Azure 接続時は web_search が有効化されてしまっている"


def test_web_search_endpoint_note_only_when_would_have_been_enabled():
    """WEB-1: 管理者許可は system_settings.web_search_allowed（env はもう見ない）。"""
    from sherpa.providers.codex.sandbox import _web_search_endpoint_note

    allowed = {"web_search_allowed": True}
    # 既定接続先なら Azure が理由という説明は不要。
    assert _web_search_endpoint_note(True, "openai", allowed) is None
    # Azure だが admin 未許可／ユーザー未希望 → そもそも既定で OFF なので注記不要。
    assert _web_search_endpoint_note(True, "azure") is None
    assert _web_search_endpoint_note(False, "azure", allowed) is None
    # admin 許可＋ユーザー希望が揃って初めて「Azure のせいで OFF」の注記が出る。
    note = _web_search_endpoint_note(True, "azure", allowed)
    assert note and "Azure" in note


# ===== 4. _codex_clean_env の openai_api_key 注入 =====

def test_codex_clean_env_injects_key_only_when_explicitly_passed(tmp_path, monkeypatch):
    from sherpa.providers.codex.sandbox import _codex_clean_env

    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak-by-default")
    env_default = _codex_clean_env(tmp_path / "ch", tmp_path / "auth", tmp_path / "tmp")
    assert "OPENAI_API_KEY" not in env_default, "既定(引数省略)で OPENAI_API_KEY が漏れている"

    env_azure = _codex_clean_env(tmp_path / "ch2", tmp_path / "auth2", tmp_path / "tmp2",
                                 openai_api_key="sk-azure-key-value")
    assert env_azure["OPENAI_API_KEY"] == "sk-azure-key-value"

    env_empty = _codex_clean_env(tmp_path / "ch3", tmp_path / "auth3", tmp_path / "tmp3",
                                 openai_api_key="")
    assert "OPENAI_API_KEY" not in env_empty, "空文字は未設定として扱われるべき"


# ===== 5. _select_provider の配線・正直なエラー =====

def test_select_provider_default_endpoint_unaffected():
    """既定接続先では openai_api_key を一切解決しない（None のまま）＝在来の Codex(OpenAI) 無改修。"""
    from sherpa.providers import _select_provider

    p = _select_provider({"agent": "codex", "codex_model_provider": "openai"})
    assert p.__class__.__name__ == "CodexProvider", f"想定外のクラス: {p.__class__.__name__}"
    assert p._openai_api_key is None
    assert p._ollama_base_url is None


def test_select_provider_azure_without_key_is_unwired(sysset):
    from sherpa.providers import _UnwiredProvider, _select_provider

    _azure(sysset)
    p = _select_provider({"agent": "codex", "codex_model_provider": "openai",
                          "codex_model": "my-azure-deployment"})
    assert isinstance(p, _UnwiredProvider)
    assert "キー" in p.howto


def test_select_provider_azure_without_model_is_unwired(sysset):
    from sherpa.providers import _UnwiredProvider, _select_provider

    _azure(sysset)
    p = _select_provider({"agent": "codex", "codex_model_provider": "openai",
                          "openai_api_key": "sk-real-azure-key"})
    assert isinstance(p, _UnwiredProvider), "codex_model 未設定なのに CodexProvider が組み立てられた"
    assert "デプロイ名" in p.howto


def test_select_provider_azure_with_key_and_model_wires_codex_provider(sysset):
    """モデル名は個人設定でなく管理者のカタログ（codex/codex）から解決される。"""
    from sherpa.providers import _select_provider

    _azure(sysset, model_catalog={"codex": {"codex": {"allowed": ["my-deployment"],
                                                       "default": "my-deployment"}}})
    p = _select_provider({"agent": "codex", "codex_model_provider": "openai",
                          "openai_api_key": "sk-real-azure-key"})
    assert p.__class__.__name__ == "CodexProvider"
    assert p._openai_api_key == "sk-real-azure-key"
    assert p._ollama_base_url is None
    assert p.model == "my-deployment"


def test_select_provider_azure_invalid_cloud_provider_is_unwired_not_key_leak(sysset):
    """codex は A7（cloud_provider）の対象外だが、Azure 等リダイレクト時は central/personal の
    openai_api_key を実際に Codex 子プロセスへ渡す。`cloud_provider` が非空の不正値
    （env 誤記・旧データ等）のときは黙って既定（openai）へ倒れたキーを渡さず honest failure
    にする。"""
    from sherpa.providers import _UnwiredProvider, _select_provider

    _azure(sysset, cloud_provider="not-a-real-provider",
          model_catalog={"codex": {"codex": {"allowed": ["my-deployment"],
                                             "default": "my-deployment"}}})
    p = _select_provider({"agent": "codex", "codex_model_provider": "openai",
                          "openai_api_key": "sk-should-not-leak"})
    assert isinstance(p, _UnwiredProvider)
    assert "not-a-real-provider" in p.howto


def test_select_provider_narrows_exception_catch_to_invalid_model_name_only(monkeypatch):
    """`_select_provider` の `except model_catalog.InvalidModelNameError` は、この型だけを狭く
    捕捉して `_UnwiredProvider`（正直な「モデル名が不正」表示）に化ける。`SHERPA_CODEX_TIMEOUT`
    のような無関係な env 値が壊れて別の `ValueError` が起きた場合は、この except に拾われず
    そのまま伝播する。"""
    from sherpa.providers import _select_provider

    monkeypatch.setenv("SHERPA_CODEX_TIMEOUT", "not-a-number")
    with pytest.raises(ValueError):
        _select_provider({"agent": "codex", "codex_model_provider": "openai",
                          "codex_model": "gpt-5.4-mini"})


def test_select_provider_ollama_construct_ignores_azure_settings(sysset):
    """Codex(Ollama) 構成は Azure 判定の対象外＝openai_api_key は解決しない（従来どおり）。"""
    from sherpa.providers import _select_provider

    _azure(sysset)
    p = _select_provider({"agent": "codex", "codex_model_provider": "ollama",
                          "ollama_url": "http://localhost:11434"})
    assert p.__class__.__name__ == "CodexProvider"
    assert p._ollama_base_url == "http://localhost:11434"
    assert p._openai_api_key is None


def test_select_provider_ollama_construct_sandbox_disabled_is_fail_closed(monkeypatch):
    """`SHERPA_CODEX_SANDBOX=0`（緊急避難経路）は独自 model_provider の config.toml 書込に対応
    していない（`-c` 引数のみ）ため、Codex(Ollama) 構成のままこの経路で実行すると Codex CLI が
    既定の `openai` provider（auth.json 経由）へ黙って接続し、利用者が選んだローカル AI ではなく
    OpenAI へ意図せず課金される。サンドボックス無効時は honest failure（`_UnwiredProvider`）を
    返し、Codex を起動しない（黙って別の課金プロバイダへ倒さない）。"""
    from sherpa.providers import _UnwiredProvider, _select_provider

    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    p = _select_provider({"agent": "codex", "codex_model_provider": "ollama",
                          "ollama_url": "http://localhost:11434"})
    assert isinstance(p, _UnwiredProvider)
    assert "サンドボックス" in p.howto
    assert "OpenAI" in p.howto   # 原因（黙って OpenAI へ繋がりうること）を利用者に伝える


def test_select_provider_ollama_construct_sandbox_enabled_explicit_still_wires(monkeypatch):
    """回帰確認: サンドボックスが明示的に有効（既定と同じ）なら従来どおり Codex(Ollama) が組み立て
    られる。"""
    from sherpa.providers import _select_provider

    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "1")
    p = _select_provider({"agent": "codex", "codex_model_provider": "ollama",
                          "ollama_url": "http://localhost:11434"})
    assert p.__class__.__name__ == "CodexProvider"
    assert p._ollama_base_url == "http://localhost:11434"


def test_select_provider_invalid_codex_model_provider_is_unwired():
    """`codex_model_provider` が非空の不正値（env 誤記・旧データ等）のとき、`_select_provider` は
    黙って openai へ倒さず honest failure（`_UnwiredProvider`）を返す（黙ったプロバイダ切替の是正）。
    """
    from sherpa.providers import _UnwiredProvider, _select_provider

    p = _select_provider({"agent": "codex", "codex_model_provider": "anthropic"})
    assert isinstance(p, _UnwiredProvider)
    assert "anthropic" in p.howto


def test_select_provider_azure_default_model_name_is_unwired(sysset):
    """`codex_model` が既定値（"gpt-5.5"）のままだと、空チェックだけを通ってしまい黙って 404 になる
    （デプロイ名が要るのに OpenAI のモデル名を送る）。既定値のままも未設定として扱う。"""
    from sherpa.providers import _UnwiredProvider, _select_provider

    _azure(sysset)
    p = _select_provider({"agent": "codex", "codex_model_provider": "openai",
                          "openai_api_key": "sk-real-azure-key", "codex_model": "gpt-5.5"})
    assert isinstance(p, _UnwiredProvider), "codex_model が既定値のままなのに CodexProvider が組み立てられた"
    assert "デプロイ名" in p.howto


def test_select_provider_azure_invalid_base_url_is_unwired(sysset):
    """base URL 自体が不正（http かつ非ループバック）なら、実キー・モデルが揃っていても未接続を
    返す（config.toml に不正な URL が書かれてキーが渡ることを防ぐ）。"""
    from sherpa.providers import _UnwiredProvider, _select_provider

    _azure(sysset, openai_base_url="http://myres.openai.azure.com/openai/v1")
    p = _select_provider({"agent": "codex", "codex_model_provider": "openai",
                          "openai_api_key": "sk-real-azure-key", "codex_model": "my-deployment"})
    assert isinstance(p, _UnwiredProvider)
    assert "接続先" in p.howto


def test_select_provider_azure_sandbox_disabled_is_fail_closed(sysset, monkeypatch):
    """`SHERPA_CODEX_SANDBOX=0`（fallback 経路）は Azure 等への接続先リダイレクトに未対応
    （独自 model_provider を書けない）。サンドボックス無効時は、実キー・デプロイ名が揃っていても
    常に未接続を返す（fail-closed・fallback argv へ同等設定を渡す案は採らない・
    docs/08-実行権限と隔離.md §11 参照）。"""
    from sherpa.providers import _UnwiredProvider, _select_provider

    _azure(sysset)
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    p = _select_provider({"agent": "codex", "codex_model_provider": "openai",
                          "openai_api_key": "sk-real-azure-key", "codex_model": "my-deployment"})
    assert isinstance(p, _UnwiredProvider)
    assert "サンドボックス" in p.howto


def test_select_provider_azure_sandbox_enabled_explicit_still_wires(sysset, monkeypatch):
    """回帰確認: サンドボックスが明示的に有効（既定と同じ）なら従来どおり組み立てられる。"""
    from sherpa.providers import _select_provider

    _azure(sysset, model_catalog={"codex": {"codex": {"allowed": ["my-deployment"],
                                                       "default": "my-deployment"}}})
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "1")
    p = _select_provider({"agent": "codex", "codex_model_provider": "openai",
                          "openai_api_key": "sk-real-azure-key"})
    assert p.__class__.__name__ == "CodexProvider"
    assert p._openai_api_key == "sk-real-azure-key"


# ===== OpenAI 直結（Codex を介さない agent=openai）にも同じ既定値ガードを入れた =====

def test_select_provider_openai_direct_ignores_personal_model_uses_catalog_default():
    """個人設定の `openai_model` はもう読まれない＝管理者のカタログ既定（openai/chat）が使われる。
    個人値とカタログ既定をわざと異なる値にする＝個人値参照が復活したらこのテストが落ちる。"""
    from sherpa.providers import _select_provider

    p = _select_provider({"agent": "openai", "openai_api_key": "sk-real-key",
                          "openai_model": "personal-value-should-be-ignored"})
    assert p.__class__.__name__ == "OpenAIProvider"
    assert p.model == "gpt-5.5"   # 組み込み既定（個人値ではない）


def test_select_provider_openai_direct_azure_default_model_is_unwired(sysset):
    """「OpenAI 直結」構成でも、接続先が Azure 等で `openai_model` が既定値（"gpt-5.5"）のままなら
    未接続を返す（Codex(OpenAI 互換) 構成と一貫させる）。"""
    from sherpa.providers import _UnwiredProvider, _select_provider

    _azure(sysset)
    p = _select_provider({"agent": "openai", "openai_api_key": "sk-real-azure-key",
                          "openai_model": "gpt-5.5"})
    assert isinstance(p, _UnwiredProvider), "openai_model が既定値のままなのに OpenAIProvider が組み立てられた"
    assert "デプロイ名" in p.howto


def test_select_provider_openai_direct_azure_with_deployment_name_wires_provider(sysset):
    """デプロイ名（管理者のカタログ・openai/chat の既定値と異なる値）が設定されていれば
    従来どおり組み立てられる。"""
    from sherpa.providers import _select_provider

    _azure(sysset, model_catalog={"openai": {"chat": {"allowed": ["my-embed-chat-deployment"],
                                                       "default": "my-embed-chat-deployment"}}})
    p = _select_provider({"agent": "openai", "openai_api_key": "sk-real-azure-key"})
    assert p.__class__.__name__ == "OpenAIProvider"
    assert p.model == "my-embed-chat-deployment"
