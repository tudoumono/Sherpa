"""保存済みの vlm.provider が不正な値のときは Ollama に丸めず、画像を送らない。未設定は従来どおり Ollama。"""
from __future__ import annotations

import pytest

from sherpa.ingest.arms import vision_arm


@pytest.fixture(autouse=True)
def _no_usable_override(monkeypatch):
    monkeypatch.delenv("SHERPA_VLM_USABLE", raising=False)


@pytest.mark.parametrize("raw", ["bogus", "gemini", 7])
def test_invalid_provider_is_not_rounded_to_ollama(monkeypatch, raw):
    monkeypatch.setattr(vision_arm, "_system_vlm", lambda: {"provider": raw, "cloud_allowed": True})
    assert vision_arm.invalid_vlm_provider() == str(raw)
    with pytest.raises(vision_arm.InvalidVlmProviderConfigError):
        vision_arm.vlm_config(strict=True)
    assert vision_arm.resolve_vlm() is None  # 画像を送らない
    assert vision_arm.vlm_usable() is False
    assert vision_arm.vlm_config()["provider"] == "ollama"  # 管理画面の表示用は従来どおり


@pytest.mark.parametrize("sysv", [{}, {"provider": None}, {"provider": ""}, {"provider": "ollama"}])
def test_unset_provider_stays_ollama(monkeypatch, sysv):
    monkeypatch.setattr(vision_arm, "_system_vlm", lambda: sysv)
    assert vision_arm.invalid_vlm_provider() is None
    assert vision_arm.vlm_config(strict=True)["provider"] == "ollama"
