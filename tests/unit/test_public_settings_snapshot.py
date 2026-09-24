"""system_settings の「1リクエスト1スナップショット」契約を検証する（`_public_settings`・
`effective_agent`・`_select_provider` の3経路）。

`keys.resolve_api_key`（3プロバイダ分）・`keys.selected_cloud_provider`・
`agent_constructs.construct_id`（内部の A7 判定）・`agent_constructs.available_constructs`・
`_effective_provider_for_field`（intent_model の auto 解決）・`_ollama_url_choice`（中央既定 URL）・
`model_catalog.get_catalog()`（モデルカタログ本体）・`llm._allowlisted_hosts()`（Ollama 許可
リスト）・`effective_agent()`（agent 未設定時の `default_agent()` 解決チェーン・A7 判定と警告
ログ用のプロバイダ名解決）は、呼び出し側が既に読んだスナップショットを渡せばそれを使い、省略時
だけ自分で読む契約になっている。各経路の入口が1回だけ `store.get_system_settings()` を読み、
以後の全ヘルパーへ同じオブジェクトを渡すことをここで固定する（個別に読み直すと、応答を
組み立てている途中で admin 更新が挟まった場合に新旧の値が混ざった1レスポンスになりうる・
再読込ヘルパーを丸ごと stub 化したテストではこの回帰を検出できない）。

単一の flat リストへ全スパイを積んで `len(...) >= N` とだけ確認する方式は、`get_catalog` 単独で
何度も記録されるため、**別のヘルパー経路が消えても** `len` の閾値を割らず通ってしまう false
green がある。ここではヘルパー名ごとに呼ばれたことを個別に assert する。

`_web_search_admin_allowed`（WEB-1・system_settings.web_search_allowed を読む）も
`_openai_endpoint_kind`／`_openai_base_url_host` と同じくスコープ内（`_public_settings` のスナップ
ショットを受け取る契約）のため、丸ごと stub 化はしない——helper 自体を丸ごと差し替えるテストは、
helper が再び独自に `store.get_system_settings()` を読み直す退行を検出できない false green に
なるため、実体を spy でラップして経路を通す。
"""
from __future__ import annotations

from sherpa.routers import system as system_router


def _base_user_settings(agent: str | None = "openai") -> dict:
    return {
        "agent": agent, "codex_reasoning": "low", "codex_model": "gpt-5.5",
        "openai_model": "gpt-5.5", "ollama_url": "http://localhost:11434", "ollama_model": "qwen2.5",
        "gemini_model": "gemini-2.5-flash", "bedrock_model": None,
    }


def _spy(name, real, extract_system_settings, seen_by_name):
    """呼び出しごとに `seen_by_name[name]` へ受け取った system_settings を積む monkeypatch ラッパー。"""
    def _wrapped(*args, **kwargs):
        seen_by_name.setdefault(name, []).append(extract_system_settings(args, kwargs))
        return real(*args, **kwargs)
    return _wrapped


_KW_OR_POS0 = lambda a, kw: kw.get("system_settings", a[0] if a else None)   # noqa: E731 - テスト専用の短い抽出規則


def test_public_settings_shares_one_system_settings_snapshot(monkeypatch):
    """`agent="openai"`（保存済みの具体的クラウド系 agent・A7 判定が実際に発火する経路）で
    `_public_settings` を呼び、スコープ内の全ヘルパーが同じスナップショットを受け取り、
    `store.get_system_settings()` が1回だけ呼ばれることを固定する。"""
    sentinel = {"cloud_provider": "openai", "personal_api_keys_allowed": False}
    read_calls = []

    def _spy_get_system_settings():
        read_calls.append(1)
        return sentinel

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy_get_system_settings)

    # スコープ外のヘルパー（system_settings を扱わない箇所）は無い——`available_constructs`／
    # `_effective_provider_for_field`／`_ollama_url_choice`／
    # `construct_id`(→`effective_agent`→`agent_requires_unselected_cloud`)／
    # `model_catalog.field_choice_info`（→`get_catalog`）／`llm._allowlisted_hosts`／
    # `_openai_endpoint_kind`／`_openai_base_url_host`／`_web_search_admin_allowed` は
    # stub せず実経路を通す（これらこそが「同じスナップショットを受け取るか」の検証対象）。

    seen_by_name: dict[str, list] = {}

    monkeypatch.setattr(
        system_router.keys, "resolve_api_key",
        _spy("resolve_api_key", system_router.keys.resolve_api_key,
             lambda a, kw: kw.get("system_settings", a[2] if len(a) > 2 else None), seen_by_name))
    monkeypatch.setattr(
        system_router.keys, "selected_cloud_provider",
        _spy("selected_cloud_provider", system_router.keys.selected_cloud_provider, _KW_OR_POS0, seen_by_name))
    monkeypatch.setattr(
        system_router.keys, "personal_keys_allowed",
        _spy("personal_keys_allowed", system_router.keys.personal_keys_allowed, _KW_OR_POS0, seen_by_name))
    monkeypatch.setattr(
        system_router.keys, "resolve_ollama_url",
        _spy("resolve_ollama_url", system_router.keys.resolve_ollama_url,
             lambda a, kw: kw.get("system_settings", a[1] if len(a) > 1 else None), seen_by_name))
    monkeypatch.setattr(
        system_router.model_catalog, "get_catalog",
        _spy("get_catalog", system_router.model_catalog.get_catalog, _KW_OR_POS0, seen_by_name))
    monkeypatch.setattr(
        system_router.llm, "_allowlisted_hosts",
        _spy("_allowlisted_hosts", system_router.llm._allowlisted_hosts, _KW_OR_POS0, seen_by_name))
    monkeypatch.setattr(
        system_router, "_openai_endpoint_kind",
        _spy("_openai_endpoint_kind", system_router._openai_endpoint_kind, _KW_OR_POS0, seen_by_name))
    monkeypatch.setattr(
        system_router, "_openai_base_url_host",
        _spy("_openai_base_url_host", system_router._openai_base_url_host, _KW_OR_POS0, seen_by_name))
    monkeypatch.setattr(
        system_router, "_web_search_admin_allowed",
        _spy("_web_search_admin_allowed", system_router._web_search_admin_allowed, _KW_OR_POS0, seen_by_name))

    system_router._public_settings(_base_user_settings("openai"))

    # `store.get_system_settings()` は `_public_settings` 自身が1回だけ読む
    # （`_openai_endpoint_kind`／`_openai_base_url_host`／`_web_search_admin_allowed` が渡された
    # スナップショットを使わず独自に読み直すと、ここが2回以上に増える＝混在の再発をこの回数で
    # 検出する）。
    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"
    # ヘルパー名ごとの必須集合を確認する（flat な件数の閾値だけだと、特定のヘルパー経路が丸ごと
    # 呼ばれなくなっても他ヘルパーの多重呼び出しに埋もれて検出できない）。
    # `resolve_ollama_url` は必須集合に含めない: FBK-1（fail-loud・2026-09-01）以降、
    # `cloud_provider` を明示選択（この sentinel は "openai"）していて鍵が無い場合、
    # `resolve_auto_provider` は Ollama へ倒す前に打ち切るため、intent_model の auto 解決は
    # `resolve_ollama_url` まで到達しない（呼ばれてもスナップショット一致は下の全件ループで確認する）。
    required = {"resolve_api_key", "selected_cloud_provider", "personal_keys_allowed",
               "get_catalog", "_allowlisted_hosts",
               "_openai_endpoint_kind", "_openai_base_url_host", "_web_search_admin_allowed"}
    missing = required - set(seen_by_name)
    assert not missing, f"呼ばれなかったヘルパー: {missing}"
    for name, snaps in seen_by_name.items():
        assert snaps, f"{name} が記録されたが呼び出しが空だった（診断ロジック不整合）"
        assert all(snap is sentinel for snap in snaps), \
            f"{name} が _public_settings と異なる system_settings オブジェクトを受け取った"


def test_public_settings_shares_one_system_settings_snapshot_with_unset_agent(monkeypatch):
    """`agent` 未設定（`None`／空文字＝通常のユーザーで最も一般的な経路）で `_public_settings` を
    呼び、`agent_constructs.default_agent()` が同じスナップショットを受け取り、
    `store.get_system_settings()` が1回だけ呼ばれることを固定する（固定済みの
    `agent="openai"` テストだけでは、この未設定経路の再読込漏れを検出できない）。"""
    sentinel = {"cloud_provider": "openai", "personal_api_keys_allowed": False}
    read_calls = []

    def _spy_get_system_settings():
        read_calls.append(1)
        return sentinel

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy_get_system_settings)

    seen_by_name: dict[str, list] = {}
    monkeypatch.setattr(
        system_router.agent_constructs, "default_agent",
        _spy("default_agent", system_router.agent_constructs.default_agent, _KW_OR_POS0, seen_by_name))
    monkeypatch.setattr(
        system_router, "_openai_endpoint_kind",
        _spy("_openai_endpoint_kind", system_router._openai_endpoint_kind, _KW_OR_POS0, seen_by_name))
    monkeypatch.setattr(
        system_router, "_openai_base_url_host",
        _spy("_openai_base_url_host", system_router._openai_base_url_host, _KW_OR_POS0, seen_by_name))
    monkeypatch.setattr(
        system_router, "_web_search_admin_allowed",
        _spy("_web_search_admin_allowed", system_router._web_search_admin_allowed, _KW_OR_POS0, seen_by_name))

    system_router._public_settings(_base_user_settings(None))

    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"
    assert "default_agent" in seen_by_name, "default_agent が呼ばれなかった"
    assert all(snap is sentinel for snap in seen_by_name["default_agent"]), \
        "default_agent が _public_settings と異なる system_settings オブジェクトを受け取った"
    for name in ("_openai_endpoint_kind", "_openai_base_url_host", "_web_search_admin_allowed"):
        assert name in seen_by_name, f"{name} が呼ばれなかった"
        assert all(snap is sentinel for snap in seen_by_name[name]), \
            f"{name} が _public_settings と異なる system_settings オブジェクトを受け取った"


def test_effective_agent_unset_reads_once_via_default_agent_chain(monkeypatch):
    """`agent` 未設定（`None`／空文字＝最も一般的な経路）のとき、`effective_agent` が一度だけ
    materialize したスナップショットが `default_agent()` の解決チェーン全体（
    `_auto_default_agent`→`_codex_auth_available`）に渡ることを固定する
    （`store.get_system_settings()` は1回だけ）。codex CLI が PATH にある環境を模し、
    `_codex_auth_available()` 自体（`shutil.which` の短絡で素通りしない経路）を実際に通す。
    `_codex_auth_available` を spy して実際に呼ばれたこと・受け取った引数を直接確認し
    （呼ばれずに済んでしまう false green を防ぐ）、結果も `enabled_agents()` 所属という弱い
    assert ではなく期待する具体値（"ollama"）で固定する。"""
    from sherpa import agent_constructs

    monkeypatch.delenv("SHERPA_AGENT", raising=False)   # 明示指定なし＝自動選択の経路を確定させる

    read_calls = []
    # openai の実キーが無い環境を模す＝ codex CLI はあるが認証も無い（_codex_auth_available が
    # False）→ 次点の openai キーも無い→ ollama へフォールバックする経路。
    sentinel = {"cloud_provider": "openai"}

    def _spy_get_system_settings():
        read_calls.append(1)
        return sentinel

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy_get_system_settings)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setenv("CODEX_HOME", "/nonexistent-codex-home")   # auth.json 不在を確定させる

    auth_calls: list = []
    real_codex_auth_available = agent_constructs._codex_auth_available

    def _spy_codex_auth_available(system_settings=None):
        auth_calls.append(system_settings)
        return real_codex_auth_available(system_settings)

    monkeypatch.setattr(agent_constructs, "_codex_auth_available", _spy_codex_auth_available)

    result = agent_constructs.effective_agent(None)

    assert result == "ollama", f"自動選択の結果が想定と異なる: {result!r}"
    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"
    assert len(auth_calls) == 1, (
        "_codex_auth_available の呼び出し回数が想定と異なる"
        "（shutil.which の短絡で素通りした場合は0回になりうる）: "
        f"{auth_calls!r}")
    assert auth_calls[0] is sentinel, \
        "_codex_auth_available が effective_agent と異なる system_settings オブジェクトを受け取った"


def test_effective_agent_unset_with_explicit_env_agent_still_applies_a7(monkeypatch):
    """`SHERPA_AGENT=openai`（明示 env）でも、選択中のクラウドプロバイダが一致しなければ
    `ollama` へフォールバックすることを固定する。`default_agent()` は明示 env 値をそのまま
    通すため（A7 を見ない）、その結果へ改めて A7 判定を適用する必要がある
    （`store.get_system_settings()` は1回だけ）。"""
    from sherpa import agent_constructs

    read_calls = []
    sentinel = {"cloud_provider": "gemini"}   # openai を選択中でない

    def _spy_get_system_settings():
        read_calls.append(1)
        return sentinel

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy_get_system_settings)
    monkeypatch.setenv("SHERPA_AGENT", "openai")
    agent_constructs._warned_unavailable_cloud_agent.clear()

    result = agent_constructs.effective_agent(None)

    assert result == "ollama"
    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"


def test_effective_agent_explicit_noncloud_agent_never_reads_system_settings(monkeypatch):
    """明示的な非クラウド系 agent（codex/ollama）は A7 判定の対象外のため、`effective_agent` は
    `store.get_system_settings()` を一切呼ばない（クラウド系 agent の未設定分岐だけが
    materialize する・非クラウド系はこの読取自体を経由しない最適化）。"""
    from sherpa import agent_constructs

    read_calls = []

    def _boom():
        read_calls.append(1)
        raise AssertionError("非クラウド系 agent で system_settings が読まれた")

    monkeypatch.setattr("sherpa.store.get_system_settings", _boom)

    assert agent_constructs.effective_agent({"agent": "codex"}) == "codex"
    assert agent_constructs.effective_agent({"agent": "ollama"}) == "ollama"
    assert read_calls == []


def test_effective_agent_materializes_snapshot_once_when_omitted(monkeypatch):
    """`effective_agent(settings)`（`system_settings` 省略）が保存済みの**具体的クラウド系
    agent**（A7 不一致）で ollama へフォールバックする際、`agent_requires_unselected_cloud` と
    警告ログ用の `selected_cloud_provider` の両方が、関数内部で一度だけ materialize した同じ
    スナップショットを使うことを固定する（`store.get_system_settings()` は1回だけ・別々に
    読み直すと途中で admin 設定が変わった場合に「判定は旧値・警告表示は新値」という食い違いが
    起こりうる）。"""
    from sherpa import agent_constructs

    read_calls = []
    sentinel = {"cloud_provider": "gemini"}   # openai を選択中でない＝A7 不一致を発生させる

    def _spy_get_system_settings():
        read_calls.append(1)
        return sentinel

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy_get_system_settings)
    agent_constructs._warned_unavailable_cloud_agent.clear()   # プロセス内1回だけの警告抑制をリセット

    result = agent_constructs.effective_agent({"agent": "openai"})

    assert result == "ollama"   # A7 不一致＝ollama へフォールバック
    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"


def test_select_provider_execution_path_shares_one_snapshot(monkeypatch):
    """実行経路（`providers/__init__.py::_select_provider`）でも入口の `sys_s` が各分岐の
    モデル解決（`model_catalog.resolve_model`）・キー解決（`keys.resolve_api_key`）へ一貫して
    渡ることを固定する（`store.get_system_settings()` は1回だけ）。openai 分岐（キー解決＋
    モデル解決の両方を通る）で検証する。"""
    from sherpa.providers import _select_provider

    read_calls = []
    sentinel = {"cloud_provider": "openai", "openai_api_key": "sk-central"}

    def _spy_get_system_settings():
        read_calls.append(1)
        return sentinel

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy_get_system_settings)

    seen_by_name: dict[str, list] = {}
    from sherpa import keys as _keys_mod, model_catalog as _model_catalog_mod

    monkeypatch.setattr(
        _keys_mod, "resolve_api_key",
        _spy("resolve_api_key", _keys_mod.resolve_api_key,
             lambda a, kw: kw.get("system_settings", a[2] if len(a) > 2 else None), seen_by_name))
    monkeypatch.setattr(
        _model_catalog_mod, "resolve_model",
        _spy("resolve_model", _model_catalog_mod.resolve_model,
             lambda a, kw: kw.get("system_settings"), seen_by_name))

    provider = _select_provider({"agent": "openai"})

    assert provider.__class__.__name__ in ("OpenAIProvider", "_UnwiredProvider")
    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"
    required = {"resolve_api_key", "resolve_model"}
    missing = required - set(seen_by_name)
    assert not missing, f"呼ばれなかったヘルパー: {missing}"
    for name, snaps in seen_by_name.items():
        assert all(snap is sentinel for snap in snaps), \
            f"{name} が _select_provider と異なる system_settings オブジェクトを受け取った"


def test_select_provider_codex_azure_branch_shares_one_snapshot(monkeypatch):
    """`_select_provider` の Codex(Azure 等) 分岐でも、入口の `sys_s` が一貫して渡ることを固定する。
    この分岐は `_codex_openai_compat_block_reason`（キー解決・モデル解決の両方を内部で行う）を
    経由したあと、`_select_provider` 自身が再度キー・モデルを解決する二重の呼び出し経路を持つため、
    openai 直結分岐（上のテスト）とは別に固定する（`_codex_openai_compat_block_reason` 内部の
    モデル解決だけ system_settings が未配線だった穴の回帰防止）。"""
    from sherpa import providers as providers_mod
    from sherpa.providers import _select_provider

    monkeypatch.setattr(providers_mod.shutil, "which",
                        lambda name: "/usr/bin/codex" if name == "codex" else None)

    read_calls = []
    # 既定(api.openai.com)以外の接続先（sentinel 自体に含める＝Azure 等の分岐へ入れる）。
    # model_catalog（codex/codex）に組み込み既定と異なるデプロイ名を設定する＝個人設定の
    # `codex_model` はもう読まれないため、Azure 判定を通過させるにはカタログ側で用意する必要がある。
    sentinel = {"cloud_provider": "openai", "openai_api_key": "sk-central",
                "openai_endpoint_kind": "azure",
                "openai_base_url": "https://myres.openai.azure.com/openai/v1",
                "model_catalog": {"codex": {"codex": {"allowed": ["my-azure-deployment"],
                                                      "default": "my-azure-deployment"}}}}

    def _spy_get_system_settings():
        read_calls.append(1)
        return sentinel

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy_get_system_settings)

    seen_by_name: dict[str, list] = {}
    from sherpa import keys as _keys_mod, model_catalog as _model_catalog_mod

    monkeypatch.setattr(
        _keys_mod, "resolve_api_key",
        _spy("resolve_api_key", _keys_mod.resolve_api_key,
             lambda a, kw: kw.get("system_settings", a[2] if len(a) > 2 else None), seen_by_name))
    monkeypatch.setattr(
        _model_catalog_mod, "resolve_model",
        _spy("resolve_model", _model_catalog_mod.resolve_model,
             lambda a, kw: kw.get("system_settings"), seen_by_name))

    # モデル名は管理者のカタログ（sentinel 側で用意した非既定デプロイ名）から解決される＝
    # `_codex_openai_compat_block_reason` の「既定のまま Azure へ切替＝拒否」判定を通過する。
    provider = _select_provider({"agent": "codex", "codex_model_provider": "openai"})

    assert provider is not None
    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"
    required = {"resolve_api_key", "resolve_model"}
    missing = required - set(seen_by_name)
    assert not missing, f"呼ばれなかったヘルパー: {missing}"
    for name, snaps in seen_by_name.items():
        assert snaps, f"{name} が一度も呼ばれなかった"
        assert all(snap is sentinel for snap in snaps), \
            f"{name} が _select_provider と異なる system_settings オブジェクトを受け取った"


def test_codex_openai_compat_block_reason_materializes_snapshot_once_when_omitted(monkeypatch):
    """`_codex_openai_compat_block_reason(s, system_settings=None)`（呼び出し側がスナップショットを
    渡さない単独呼び出し）は、内部で1回だけ `store.get_system_settings()` を読み、キー解決・モデル
    解決の両方へ同じオブジェクトを渡す。省略値のまま両方の呼び出し先へ横流しすると、各呼び出し先が
    個別に読み直し、この1回の判定の中で admin 更新が挟まった場合にキーとモデルが新旧混在しうる。"""
    from sherpa.providers import _codex_openai_compat_block_reason

    monkeypatch.delenv("SHERPA_CODEX_SANDBOX", raising=False)

    read_calls = []
    # 既定(api.openai.com)以外の接続先（sentinel 自体に含める＝Azure 等の分岐へ入れる）。
    # model_catalog（codex/codex）に組み込み既定と異なるデプロイ名を設定し、resolve_model が
    # 実際に呼ばれた上で「既定のまま」拒否分岐に落ちないようにする（個人設定の `codex_model` は
    # もう読まれない）。
    sentinel = {"cloud_provider": "openai", "openai_api_key": "sk-central",
                "openai_endpoint_kind": "azure",
                "openai_base_url": "https://myres.openai.azure.com/openai/v1",
                "model_catalog": {"codex": {"codex": {"allowed": ["my-azure-deployment"],
                                                      "default": "my-azure-deployment"}}}}

    def _spy_get_system_settings():
        read_calls.append(1)
        return sentinel

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy_get_system_settings)

    seen_by_name: dict[str, list] = {}
    from sherpa import keys as _keys_mod, model_catalog as _model_catalog_mod

    monkeypatch.setattr(
        _keys_mod, "resolve_api_key",
        _spy("resolve_api_key", _keys_mod.resolve_api_key,
             lambda a, kw: kw.get("system_settings", a[2] if len(a) > 2 else None), seen_by_name))
    monkeypatch.setattr(
        _model_catalog_mod, "resolve_model",
        _spy("resolve_model", _model_catalog_mod.resolve_model,
             lambda a, kw: kw.get("system_settings"), seen_by_name))

    reason = _codex_openai_compat_block_reason({}, system_settings=None)

    assert reason is None, f"想定外の拒否理由: {reason!r}"
    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"
    required = {"resolve_api_key", "resolve_model"}
    missing = required - set(seen_by_name)
    assert not missing, f"呼ばれなかったヘルパー: {missing}"
    for name, snaps in seen_by_name.items():
        assert snaps, f"{name} が一度も呼ばれなかった"
        assert all(snap is sentinel for snap in snaps), \
            f"{name} が異なる system_settings オブジェクトを受け取った（内部で複数回読み直した）"
