"""llm.select_provider の優先ロジック単体テスト（A7＝選択中クラウドプロバイダ駆動の auto 解決）。

Bedrock 追加に伴う拡張の検証:
- 既存3プロバイダ（openai/gemini/ollama）の優先順は**不変**（回帰確認）。
- `bedrock` kwarg 未指定の既存呼び出し元（intent_llm/embeddings）は今までどおり動く。
- bedrock は **選択中のクラウドプロバイダ（A7）が bedrock のときだけ** auto でも試す
  （`force_provider` 明示選択の seam は GRAPH-SRC 2026-09-04 で唯一の利用者〔graph_ab.py〕ごと撤去済み）。
"""
from __future__ import annotations

from sherpa import llm


def _factories(calls):
    def openai(key):
        calls.append(("openai", key))
        return {"provider": "openai", "key": key}

    def gemini(key):
        calls.append(("gemini", key))
        return {"provider": "gemini", "key": key}

    def ollama(url):
        calls.append(("ollama", url))
        return {"provider": "ollama", "url": url}

    return openai, gemini, ollama


def _no_env(monkeypatch):
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL"):
        monkeypatch.delenv(k, raising=False)


def test_existing_three_providers_unchanged_without_bedrock_kwarg(monkeypatch):
    """bedrock kwarg 未指定の既存呼び出し（intent_llm/embeddings）は今までどおり動く（後方互換）。"""
    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    calls = []
    openai, gemini, ollama = _factories(calls)
    cfg = llm.select_provider({"openai_api_key": "sk-x"}, openai=openai, gemini=gemini, ollama=ollama)
    assert cfg == {"provider": "openai", "key": "sk-x"} and calls == [("openai", "sk-x")]


def test_auto_priority_openai_then_gemini_unchanged(monkeypatch):
    """A7: auto は「選択中のクラウドプロバイダ（`sherpa.keys.selected_cloud_provider`）」で選ぶ
    （openai/gemini が同時に「選択中」になることは無い＝旧来の「openai→gemini」という2段
    フォールバックは起きない。既定選択は openai）。"""
    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    calls = []
    openai, gemini, ollama = _factories(calls)
    cfg = llm.select_provider({"openai_api_key": "sk-x", "gemini_api_key": "g-x", "ollama_url": "http://x"},
                              openai=openai, gemini=gemini, ollama=ollama)
    assert cfg["provider"] == "openai"                    # 既定選択（openai）のキーがあれば最優先
    calls.clear()
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"personal_api_keys_allowed": True, "cloud_provider": "gemini"})
    cfg = llm.select_provider({"gemini_api_key": "g-x", "ollama_url": "http://x"},
                              openai=openai, gemini=gemini, ollama=ollama)
    assert cfg["provider"] == "gemini"                     # gemini を選択中なら gemini


def test_auto_falls_back_to_ollama_only_when_cloud_never_selected(monkeypatch):
    """クラウドを一度も選んでいない構成（`cloud_provider` の生の保存値が無い＝既定 openai への
    読み替えのみ）では、鍵が何も無くても auto は従来どおり Ollama（`resolve_ollama_url` の
    組み込み既定・localhost）に落ちる（FBK-1 で保存される構成・Ollama 専用デプロイの経路）。"""
    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    calls = []
    openai, gemini, ollama = _factories(calls)
    cfg = llm.select_provider({}, openai=openai, gemini=gemini, ollama=ollama)
    assert cfg == {"provider": "ollama", "url": "http://localhost:11434"}
    assert calls == [("ollama", "http://localhost:11434")]


def test_auto_fails_loud_when_selected_cloud_provider_has_no_key(monkeypatch):
    """FBK-1（2026-09-01・fail-loud）: `cloud_provider`（A7）を明示的に選んでいる場合、そのプロバイダの
    鍵が解決できなくても Ollama へは黙って倒れない（未接続＝None のまま呼び出し元の
    `llm_unavailable`／ベクトル無効等へ委ねる）——選んだクラウド側の障害なのかどうかを
    切り分けられるようにする（ユーザー裁定 2026-09-01）。"""
    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"personal_api_keys_allowed": True, "cloud_provider": "openai"})
    calls = []
    openai, gemini, ollama = _factories(calls)
    cfg = llm.select_provider({"ollama_url": "http://x"}, openai=openai, gemini=gemini, ollama=ollama)
    assert cfg is None
    assert calls == []                                     # ollama factory は一度も呼ばれない（黙って縮退しない）


def test_bedrock_not_tried_in_auto_when_not_selected_cloud_provider(monkeypatch):
    """bedrock は auto の対象だが、選択中のクラウドプロバイダ（A7）でなければ試されない
    （既定は openai・ここでは明示的にそれを確認する。factory 自体が呼ばれないことも確認する）。"""
    _no_env(monkeypatch)
    calls = []
    openai, gemini, ollama = _factories(calls)
    bedrock_calls = []

    def bedrock():
        bedrock_calls.append(True)
        return {"provider": "bedrock"}

    cfg = llm.select_provider({"bedrock_api_key": "bkey"}, openai=openai, gemini=gemini, ollama=ollama,
                              bedrock=bedrock)
    assert cfg == {"provider": "ollama", "url": "http://localhost:11434"} and bedrock_calls == []


def test_bedrock_tried_in_auto_when_selected_and_factory_provided(monkeypatch):
    """bedrock が選択中のクラウドプロバイダ（A7）なら auto でも試す（factory を渡した消費者のみ）。"""
    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"personal_api_keys_allowed": True, "cloud_provider": "bedrock"})
    calls = []
    openai, gemini, ollama = _factories(calls)
    bedrock_calls = []

    def bedrock():
        bedrock_calls.append(True)
        return {"provider": "bedrock"}

    cfg = llm.select_provider({}, openai=openai, gemini=gemini, ollama=ollama, bedrock=bedrock)
    assert cfg == {"provider": "bedrock"} and bedrock_calls == [True]


def test_bedrock_selected_but_unsupported_consumer_fails_loud(monkeypatch):
    """FBK-1（fail-loud）: bedrock factory を渡さない消費者（未対応）は、bedrock 選択中でも
    auto は Ollama へ落ちず None（bedrock は既定に含まれないため「選択中」は常に admin の明示選択）。"""
    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"personal_api_keys_allowed": True, "cloud_provider": "bedrock"})
    calls = []
    openai, gemini, ollama = _factories(calls)
    cfg = llm.select_provider({}, openai=openai, gemini=gemini, ollama=ollama)   # bedrock 未指定
    assert cfg is None
    assert calls == []


def test_bedrock_selected_but_auth_unresolved_fails_loud(monkeypatch):
    """FBK-1（fail-loud）: bedrock 選択中で factory はあるが認証未解決（None を返す）なら
    Ollama へは倒さず None のまま返す。"""
    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"personal_api_keys_allowed": True, "cloud_provider": "bedrock"})
    calls = []
    openai, gemini, ollama = _factories(calls)
    cfg = llm.select_provider({}, openai=openai, gemini=gemini, ollama=ollama, bedrock=lambda: None)
    assert cfg is None
    assert calls == []


def test_resolve_provider_selection_returns_explicit_value_unchanged(monkeypatch):
    """`resolve_provider_selection` は `pick_provider_selector` が明示的なプロバイダ名を選んだ
    場合、`resolve_auto_provider` を呼ばずそのまま返す（sherpa/routers/system.py の
    `_effective_provider_for_field` が保存時検証で使う経路・RV 4巡目 #10）。"""
    def _boom(*_a, **_k):
        raise AssertionError("明示選択時は resolve_auto_provider を呼んではいけない")

    monkeypatch.setattr(llm, "resolve_auto_provider", _boom)
    assert llm.resolve_provider_selection("", "gemini") == "gemini"
    assert llm.resolve_provider_selection("openai", "gemini") == "openai"


def test_resolve_provider_selection_falls_back_to_resolve_auto_provider_for_auto(monkeypatch):
    """明示 `"auto"`／全て空欄で auto へ落ちた場合は `resolve_auto_provider` の結果を返す。"""
    calls = []

    def _fake_resolve_auto(settings, *, bedrock_capable=False, system_settings=None):
        calls.append((settings, bedrock_capable))
        return "ollama"

    monkeypatch.setattr(llm, "resolve_auto_provider", _fake_resolve_auto)
    assert llm.resolve_provider_selection("auto", "gemini", settings={"x": 1}) == "ollama"
    assert calls == [({"x": 1}, False)]
    calls.clear()
    assert llm.resolve_provider_selection("", "", settings={"x": 1}) == "ollama"
    assert calls == [({"x": 1}, False)]


def test_select_provider_strict_propagates_invalid_cloud_provider(monkeypatch):
    """課金プロバイダ解決の実行時呼び出し元（グラフ抽出・埋め込み・intent 分類等）は
    `strict=True` を渡す＝`cloud_provider` が非空の不正値のとき、黙って既定（openai）へ倒れた
    キーで実送信せず `InvalidCloudProviderConfigError` を伝播する。`strict=False`（既定）は
    従来どおり openai へ倒れて解決される。"""
    import pytest

    from sherpa import keys

    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True, "cloud_provider": "not-a-real-provider",
        "openai_api_key": "sk-x"})
    calls = []
    openai, gemini, ollama = _factories(calls)
    with pytest.raises(keys.InvalidCloudProviderConfigError):
        llm.select_provider({}, openai=openai, gemini=gemini, ollama=ollama, strict=True)
    calls.clear()
    cfg = llm.select_provider({}, openai=openai, gemini=gemini, ollama=ollama)
    assert cfg == {"provider": "openai", "key": "sk-x"}
