"""保存済みの embed_provider が不正な値のときは auto に丸めず、埋め込みを止める。未設定は従来どおり auto。"""
from __future__ import annotations

import pytest

from sherpa import embeddings


@pytest.fixture(autouse=True)
def _embed_enabled(monkeypatch):
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)


@pytest.mark.parametrize("raw", ["bogus", "OLLAMA", " ollama", 7])
def test_invalid_value_is_not_rounded_to_auto(raw):
    sys_s = {"embed_provider": raw, "cloud_provider": "openai", "openai_api_key": "sk-test"}
    assert embeddings.invalid_embed_provider(sys_s) == str(raw)
    with pytest.raises(embeddings.InvalidEmbedProviderConfigError):
        embeddings.effective_embed_provider(sys_s, strict=True)
    assert embeddings.cfg(None, system_settings=sys_s) is None  # OpenAI を選ばない
    assert embeddings.cloud_selected_but_unavailable(sys_s) is True  # 取り込みも検索も「接続できない」と明示する


@pytest.mark.parametrize("sys_s", [{}, {"embed_provider": None}, {"embed_provider": ""}, {"embed_provider": "  "},
                                   {"embed_provider": "auto"}])
def test_unset_value_stays_auto(sys_s):
    assert embeddings.invalid_embed_provider(sys_s) is None
    assert embeddings.effective_embed_provider(sys_s, strict=True) == "auto"
