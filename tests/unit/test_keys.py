"""`sherpa.keys` 単体テスト。

`resolve_api_key`/`resolve_ollama_url`/`personal_keys_allowed`/`selected_cloud_provider` の
解決順序（A6 個人キー許可・A7 クラウドプロバイダ排他選択）を、`store.get_system_settings` を
monkeypatch した hermetic な状態で固定する（unit 共通 fixture の既定 `personal_api_keys_allowed=True`
は各テストで明示的に上書きする＝A6 実際の既定値 false を含めて検証する）。
"""
from __future__ import annotations

import pytest

from sherpa import keys


def _sys(monkeypatch, value):
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: value)


# ---- selected_cloud_provider ----

def test_selected_cloud_provider_defaults_to_openai_when_unset(monkeypatch):
    _sys(monkeypatch, {})
    assert keys.selected_cloud_provider() == "openai"


def test_selected_cloud_provider_normalizes_case_and_whitespace(monkeypatch):
    _sys(monkeypatch, {"cloud_provider": " Gemini "})
    assert keys.selected_cloud_provider() == "gemini"


def test_selected_cloud_provider_falls_back_to_default_for_unknown_value(monkeypatch):
    """壊れた設定（未知の provider 名）で全滅させない＝既定 openai へ倒す。"""
    _sys(monkeypatch, {"cloud_provider": "not-a-real-provider"})
    assert keys.selected_cloud_provider() == "openai"


def test_selected_cloud_provider_accepts_explicit_system_settings_dict():
    """呼び出し側が system_settings を明示的に渡した場合は store を読まない（引数優先）。"""
    assert keys.selected_cloud_provider({"cloud_provider": "bedrock"}) == "bedrock"


def test_selected_cloud_provider_strict_raises_for_unknown_value(monkeypatch):
    """黙ったプロバイダ切替の是正: 非空の不正値（env 誤記・旧データ等）は `strict=True` では
    黙って既定へ倒さない。"""
    _sys(monkeypatch, {"cloud_provider": "not-a-real-provider"})
    with pytest.raises(keys.InvalidCloudProviderConfigError, match="not-a-real-provider"):
        keys.selected_cloud_provider(strict=True)


def test_selected_cloud_provider_strict_allows_unset(monkeypatch):
    """未設定（空）は `strict=True` でも既定へ倒す＝正当な既定でエラーにはしない。"""
    _sys(monkeypatch, {})
    assert keys.selected_cloud_provider(strict=True) == "openai"


def test_selected_cloud_provider_strict_rejects_falsy_non_string(monkeypatch):
    """`False`/`0`/`[]`/`{}` は truthiness で「未設定」に化けず、strict では拒否する
    （黙って既定 openai のキーが返ると、意図しない課金の温床になるため）。"""
    for bad in (False, 0, [], {}):
        _sys(monkeypatch, {"cloud_provider": bad})
        with pytest.raises(keys.InvalidCloudProviderConfigError, match=r"cloud_provider"):
            keys.selected_cloud_provider(strict=True)


def test_selected_cloud_provider_non_strict_falsy_non_string_defaults(monkeypatch):
    """非 strict は従来どおり、非文字列の破損値でも既定 openai へ倒して動き続ける。"""
    for bad in (False, 0, [], {}):
        _sys(monkeypatch, {"cloud_provider": bad})
        assert keys.selected_cloud_provider() == "openai"


# ---- cloud_provider_explicitly_selected（FBK-1・fail-loud の境界判定） ----

def test_cloud_provider_explicitly_selected_false_when_key_absent(monkeypatch):
    """クラウドを一度も選んでいない構成（`cloud_provider` キー自体が無い）は偽——Ollama 専用
    デプロイの auto フォールバックを維持する側。"""
    _sys(monkeypatch, {})
    assert keys.cloud_provider_explicitly_selected() is False


def test_cloud_provider_explicitly_selected_false_when_value_is_none_or_blank(monkeypatch):
    """`None`／空文字／空白のみは「明示選択」に数えない（既定 openai への読み替えと同じ扱い）。"""
    for blank in (None, "", "   "):
        _sys(monkeypatch, {"cloud_provider": blank})
        assert keys.cloud_provider_explicitly_selected() is False


def test_cloud_provider_explicitly_selected_true_even_when_value_equals_default(monkeypatch):
    """既定と同じ文字列（"openai"）でも、生の保存値があれば明示選択扱い（admin が実際に PUT した
    値かどうかは値の中身では判定できない＝raw の有無だけを見る）。"""
    _sys(monkeypatch, {"cloud_provider": "openai"})
    assert keys.cloud_provider_explicitly_selected() is True


def test_cloud_provider_explicitly_selected_true_for_other_explicit_values(monkeypatch):
    for value in ("gemini", "bedrock", "not-a-real-provider"):
        _sys(monkeypatch, {"cloud_provider": value})
        assert keys.cloud_provider_explicitly_selected() is True


def test_cloud_provider_explicitly_selected_true_for_non_string_garbage(monkeypatch):
    """非文字列の破損値（`False`/`0`/`[]`/`{}`）も「何か保存されている」扱い（値の妥当性検証は
    `selected_cloud_provider(strict=...)` 側の責務・ここでは fail-loud 境界の判定のみ行う）。"""
    for bad in (False, 0, [], {}):
        _sys(monkeypatch, {"cloud_provider": bad})
        assert keys.cloud_provider_explicitly_selected() is True


# ---- personal_keys_allowed ----

def test_personal_keys_allowed_defaults_false(monkeypatch):
    _sys(monkeypatch, {})
    assert keys.personal_keys_allowed() is False


def test_personal_keys_allowed_true_when_set(monkeypatch):
    _sys(monkeypatch, {"personal_api_keys_allowed": True})
    assert keys.personal_keys_allowed() is True


# ---- resolve_api_key: 基本の解決順序 ----

def test_resolve_api_key_none_when_nothing_configured(monkeypatch):
    _sys(monkeypatch, {})
    assert keys.resolve_api_key("openai", {}) is None
    assert keys.resolve_api_key("openai", None) is None


def test_resolve_api_key_uses_central_when_personal_disabled(monkeypatch):
    """A6 既定（personal_api_keys_allowed=false）: 個人キーがあっても無視し、中央のキーを使う。"""
    _sys(monkeypatch, {"openai_api_key": "central-key"})
    assert keys.resolve_api_key("openai", {"openai_api_key": "personal-key"}) == "central-key"


def test_resolve_api_key_ignores_personal_when_disabled_and_no_central(monkeypatch):
    """個人キーはあるが許可 OFF・中央キーも無い＝None（黙って個人キーへは倒れない）。"""
    _sys(monkeypatch, {})
    assert keys.resolve_api_key("openai", {"openai_api_key": "personal-key"}) is None


def test_resolve_api_key_prefers_personal_over_central_when_allowed(monkeypatch):
    """A6 ON: 個人キーがあれば中央より優先する。"""
    _sys(monkeypatch, {"personal_api_keys_allowed": True, "openai_api_key": "central-key"})
    assert keys.resolve_api_key("openai", {"openai_api_key": "personal-key"}) == "personal-key"


def test_resolve_api_key_falls_back_to_central_when_allowed_but_no_personal_value(monkeypatch):
    """A6 ON でも、このユーザーが個人キーを入れていなければ中央のキーを使う。"""
    _sys(monkeypatch, {"personal_api_keys_allowed": True, "openai_api_key": "central-key"})
    assert keys.resolve_api_key("openai", {}) == "central-key"


def test_resolve_api_key_empty_string_personal_value_does_not_win(monkeypatch):
    """個人キーが空文字（未設定相当）なら中央へフォールバックする。"""
    _sys(monkeypatch, {"personal_api_keys_allowed": True, "openai_api_key": "central-key"})
    assert keys.resolve_api_key("openai", {"openai_api_key": ""}) == "central-key"


def test_resolve_api_key_unknown_provider_raises():
    import pytest
    with pytest.raises(ValueError):
        keys.resolve_api_key("anthropic-direct", {})


# ---- resolve_api_key: A7（クラウドプロバイダ排他選択） ----

def test_resolve_api_key_a7_blocks_non_selected_provider_even_with_keys_present(monkeypatch):
    """gemini の中央/個人キーが両方あっても、選択中プロバイダが openai なら None（保存キーは温存・不使用）。"""
    _sys(monkeypatch, {"personal_api_keys_allowed": True, "cloud_provider": "openai",
                       "gemini_api_key": "central-gemini"})
    assert keys.resolve_api_key("gemini", {"gemini_api_key": "personal-gemini"}) is None


def test_resolve_api_key_a7_allows_selected_provider(monkeypatch):
    _sys(monkeypatch, {"personal_api_keys_allowed": True, "cloud_provider": "gemini",
                       "gemini_api_key": "central-gemini"})
    assert keys.resolve_api_key("gemini", {}) == "central-gemini"


def test_resolve_api_key_a7_switching_provider_does_not_delete_other_keys(monkeypatch):
    """非選択プロバイダのキーは system_settings にそのまま残っている想定＝resolve は None を返すだけ
    （削除はしない・呼び出し側は sysset に触れていない）。"""
    sysset = {"personal_api_keys_allowed": True, "cloud_provider": "bedrock",
              "openai_api_key": "still-here-but-unused", "bedrock_api_key": "bedrock-key"}
    _sys(monkeypatch, sysset)
    assert keys.resolve_api_key("openai", {}) is None
    assert keys.resolve_api_key("bedrock", {}) == "bedrock-key"
    assert sysset["openai_api_key"] == "still-here-but-unused"   # 温存されたまま（消えていない）


def test_resolve_api_key_strict_raises_for_invalid_cloud_provider(monkeypatch):
    """課金プロバイダ解決に至る経路（グラフ抽出・埋め込み・intent 分類・VLM 等）は `strict=True`
    を渡す＝`cloud_provider` が非空の不正値のとき、黙って既定（openai）へ倒れたキーで実送信しない。
    `strict=False`（既定）は表示/診断など読み取り専用の呼び出しを壊れた設定でも動かし続ける。"""
    _sys(monkeypatch, {"personal_api_keys_allowed": True, "cloud_provider": "not-a-real-provider",
                       "openai_api_key": "central-openai"})
    with pytest.raises(keys.InvalidCloudProviderConfigError, match="not-a-real-provider"):
        keys.resolve_api_key("openai", {}, strict=True)
    # 非strict（既定）は従来どおり openai へ倒れた上で解決される（表示/診断は動き続ける）。
    assert keys.resolve_api_key("openai", {}) == "central-openai"


# ---- resolve_ollama_url: A7 排他対象外・常時併用 ----

def test_resolve_ollama_url_builtin_default_when_nothing_set(monkeypatch):
    _sys(monkeypatch, {})
    assert keys.resolve_ollama_url({}) == "http://localhost:11434"
    assert keys.resolve_ollama_url(None) == "http://localhost:11434"


def test_resolve_ollama_url_central_overrides_builtin_default(monkeypatch):
    _sys(monkeypatch, {"ollama_url": "http://central-ollama:11434"})
    assert keys.resolve_ollama_url({}) == "http://central-ollama:11434"


def test_resolve_ollama_url_personal_overrides_central(monkeypatch):
    _sys(monkeypatch, {"ollama_url": "http://central-ollama:11434"})
    assert keys.resolve_ollama_url({"ollama_url": "http://personal-ollama:11434"}) == "http://personal-ollama:11434"


def test_resolve_ollama_url_not_gated_by_a7_cloud_provider_selection(monkeypatch):
    """クラウドプロバイダが gemini/bedrock を選んでいても Ollama は常に使える（排他対象外）。"""
    _sys(monkeypatch, {"cloud_provider": "gemini", "ollama_url": "http://central-ollama:11434"})
    assert keys.resolve_ollama_url({}) == "http://central-ollama:11434"


# ---- honest failure メッセージ ----

def test_no_central_key_message_is_non_empty_and_does_not_mention_env():
    assert keys.NO_CENTRAL_KEY_MESSAGE
    assert "env" not in keys.NO_CENTRAL_KEY_MESSAGE.lower()
