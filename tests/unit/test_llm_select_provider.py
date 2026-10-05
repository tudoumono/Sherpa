"""llm.select_provider の優先ロジック単体テスト（A7＝クラウドプロバイダ駆動の auto 解決）。

プロバイダは openai（Azure OpenAI を含む）と ollama だけ。
- openai のキーが解決できれば openai。
- クラウドを一度も選んでいない構成だけ ollama へ自動フォールバックする。
- `cloud_provider` を明示選択済みでキーが解決できないときは ollama へ倒さず None（fail-loud）。
- 閉じたプロバイダ（gemini/bedrock）が保存されたままでも「未選択」として扱う。
"""
from __future__ import annotations

from sherpa import llm


def _factories(calls):
    def openai(key):
        calls.append(("openai", key))
        return {"provider": "openai", "key": key}

    def ollama(url):
        calls.append(("ollama", url))
        return {"provider": "ollama", "url": url}

    return openai, ollama


def _no_env(monkeypatch):
    for k in ("OPENAI_API_KEY", "OLLAMA_URL"):
        monkeypatch.delenv(k, raising=False)


def test_openai_wins_when_key_resolves(monkeypatch):
    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    calls = []
    openai, ollama = _factories(calls)
    cfg = llm.select_provider({"openai_api_key": "sk-x", "ollama_url": "http://x"}, openai=openai, ollama=ollama)
    assert cfg == {"provider": "openai", "key": "sk-x"} and calls == [("openai", "sk-x")]


def test_auto_falls_back_to_ollama_only_when_cloud_never_selected(monkeypatch):
    """クラウドを一度も選んでいない構成（`cloud_provider` の生の保存値が無い＝既定 openai への
    読み替えのみ）では、鍵が何も無くても auto は従来どおり Ollama（`resolve_ollama_url` の
    組み込み既定・localhost）に落ちる（FBK-1 で保存される構成・Ollama 専用デプロイの経路）。"""
    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    calls = []
    openai, ollama = _factories(calls)
    cfg = llm.select_provider({}, openai=openai, ollama=ollama)
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
    openai, ollama = _factories(calls)
    cfg = llm.select_provider({"ollama_url": "http://x"}, openai=openai, ollama=ollama)
    assert cfg is None
    assert calls == []                                     # ollama factory は一度も呼ばれない（黙って縮退しない）


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
    openai, ollama = _factories(calls)
    with pytest.raises(keys.InvalidCloudProviderConfigError):
        llm.select_provider({}, openai=openai, ollama=ollama, strict=True)
    calls.clear()
    cfg = llm.select_provider({}, openai=openai, ollama=ollama)
    assert cfg == {"provider": "openai", "key": "sk-x"}
