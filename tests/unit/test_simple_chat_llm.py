"""`sherpa/simple_chat.py` の既定 AI 解決（`resolve_model_and_provider`/`default_research_provider`/
`_connect_openai`）の単体テスト（DB 不要）。

2026-10-01: `POST /ext/v1/research`（PART-4・2026-09-30 廃止）の実装モジュール
（旧 `sherpa/research_service.py`）から、簡易チャット（`/ext/v1/answer`）がそのまま流用している
既定 AI の解決ロジックだけを移設した。調査ループ本体（`run_research`）・Evidence Packet の組み立て・
デッドライン/ロック周りの契約は移設対象外（呼び出し元が無くなったため削除済み）——本ファイルは
移設した契約だけを固定する。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

import pytest  # noqa: E402

import sherpa.simple_chat as SC  # noqa: E402


def test_resolve_model_and_provider_default_and_explicit_model_selection():
    """model/provider 両方省略時は ollama（組み込み既定モデル）。model 指定・provider 省略時は
    カタログが一致する provider を自動選択する。"""
    provider, model = SC.resolve_model_and_provider(None, {})
    assert (provider, model) == ("ollama", "qwen2.5")

    provider, model = SC.resolve_model_and_provider("gpt-5.4-mini", {})
    assert (provider, model) == ("openai", "gpt-5.4-mini")


def test_resolve_model_and_provider_rejects_unknown_model_or_provider():
    """許可リスト外の model・provider はいずれも `ModelNotAllowed`（呼び出し元は400にする）。"""
    with pytest.raises(SC.ModelNotAllowed):
        SC.resolve_model_and_provider("not-a-real-model", {})
    with pytest.raises(SC.ModelNotAllowed) as exc_info:
        SC.resolve_model_and_provider(None, {}, provider="gemini")
    assert "gemini" in str(exc_info.value)
    with pytest.raises(SC.ModelNotAllowed) as exc_info:
        SC.resolve_model_and_provider("not-a-real-model", {}, provider="openai")
    assert "openai" in str(exc_info.value)


def test_resolve_model_and_provider_catalog_default_empty_raises_provider_unavailable():
    """管理者がカタログを明示設定（allowed はあるが default 空欄）した場合、組み込み既定
    （qwen2.5）が allowed に含まれなければ黙って使わず `ProviderUnavailable` にする。カタログ
    未設定（組み込み既定のまま）の従来環境は壊れない（設定不備の検出が正常系を巻き込まない）。"""
    sys_s = {"model_catalog": {"ollama": {"subsearch": {"allowed": ["custom-local"], "default": ""}}}}
    with pytest.raises(SC.ProviderUnavailable):
        SC.resolve_model_and_provider(None, sys_s)
    provider, model = SC.resolve_model_and_provider(None, {"model_catalog": {}})
    assert (provider, model) == ("ollama", "qwen2.5")


def test_default_research_provider_corrupted_value_handling():
    """`default_research_provider`: 未設定/None/空文字のみ組み込み既定 "ollama"・有効値はそのまま。
    破損値（`RESEARCH_PROVIDERS` に無い・非文字列）は黙ってフォールバックせず `ValueError`。
    `resolve_model_and_provider` はこの `ValueError` を捕捉して `ProviderUnavailable` へ変換する
    （PUT 側は既に 422 で拒否済み・保存後に何らかの経路で壊れた値への防波堤）。"""
    assert SC.default_research_provider({}) == "ollama"
    assert SC.default_research_provider({"research_default_provider": None}) == "ollama"
    assert SC.default_research_provider({"research_default_provider": ""}) == "ollama"
    assert SC.default_research_provider({"research_default_provider": "openai"}) == "openai"
    for bad in (["openai"], {"x": 1}, 42, False, "gemini", "OLLAMA"):
        with pytest.raises(ValueError):
            SC.default_research_provider({"research_default_provider": bad})
    with pytest.raises(SC.ProviderUnavailable):
        SC.resolve_model_and_provider(None, {"research_default_provider": "gemini"})


def test_research_providers_is_public_constant():
    """他モジュール（system_extras.py 等）が private 名を参照しないで済むよう、
    provider 集合は公開定数 `RESEARCH_PROVIDERS`（`_` 始まりでない）として提供する。"""
    assert SC.RESEARCH_PROVIDERS == frozenset({"ollama", "openai"})


def test_connect_openai_preflight_rejects_unsendable_configs(monkeypatch):
    """送信前 fail-closed preflight（`_post` は一切呼ばれない）: プレースホルダキー／不正な
    cloud_provider／Azure 等で用途別（subsearch）デプロイ名が未解決、のいずれも
    `ProviderUnavailable`（呼び出しゼロ）。"""
    import sherpa.agentic_search as A

    calls = []
    monkeypatch.setattr(A, "_post", lambda *a, **kw: calls.append(1))

    with pytest.raises(SC.ProviderUnavailable) as exc_info:
        SC._connect_openai({"openai_api_key": "sk-REPLACE_ME"})
    assert SC.keys.NO_CENTRAL_KEY_MESSAGE in str(exc_info.value)

    with pytest.raises(SC.ProviderUnavailable) as exc_info:
        SC._connect_openai({"cloud_provider": "not-a-real-provider",
                            "openai_api_key": "sk-real-key-value-for-unit-test"})
    msg = str(exc_info.value)
    assert "設定が正しくありません" in msg and "クラウド（OpenAI）" in msg
    assert "not-a-real-provider" not in msg   # 生の設定値は外部応答に出さない

    sys_s = {"openai_api_key": "sk-real-key-value-for-unit-test",
            "openai_endpoint_kind": "azure",
            "openai_base_url": "https://example.openai.azure.com/openai/v1"}
    with pytest.raises(SC.ProviderUnavailable) as exc_info:
        SC._connect_openai(sys_s)
    assert "デプロイ名" in str(exc_info.value)

    # chat 用途にだけデプロイ名を設定し subsearch 用途を未設定のままにした場合も拒否する
    # （本モジュールが実際に送信するのは subsearch 用のモデルであり、chat セルだけを見る
    # 誤判定なら通ってしまう）。
    sys_s_chat_only = {**sys_s, "model_catalog": {"openai": {"chat": {
        "allowed": ["my-chat-deployment"], "default": "my-chat-deployment"}}}}
    with pytest.raises(SC.ProviderUnavailable) as exc_info:
        SC._connect_openai(sys_s_chat_only)
    assert "デプロイ名" in str(exc_info.value)

    assert not calls
