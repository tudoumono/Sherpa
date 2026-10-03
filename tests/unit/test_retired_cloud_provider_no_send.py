"""廃止済みの cloud_provider（gemini/bedrock）が保存されたままのとき、有効な OpenAI キーがあっても、
AI へ送信する経路（本文・要求を外部へ出す解決点）がすべて送信前にブロックされることを、HTTP 境界を
塞いだ状態で一括して確認する。新しい送信経路を足したときは、ここへ1行足す。"""
from __future__ import annotations

import contextlib
import urllib.request

import pytest

from sherpa import embeddings, intent_llm, keys, llm, simple_chat
from sherpa.ingest import graph_extract, llm_render
from sherpa.ingest.arms import vision_arm


def _block_http(monkeypatch):
    sent = []

    def _no(*a, **k):
        sent.append(a)
        raise AssertionError("HTTP 送信が行われた")

    for target, name in ((llm, "post_json"), (llm, "urlopen_no_redirect"), (graph_extract, "complete_json")):
        monkeypatch.setattr(target, name, _no)
    monkeypatch.setattr(urllib.request, "urlopen", _no)
    return sent


def _expect(strict):
    """strict は例外、非 strict は例外なし（どちらも送信用の構成・鍵は返さない）。"""
    return pytest.raises(keys.InvalidCloudProviderConfigError) if strict else contextlib.nullcontext()


@pytest.mark.parametrize("retired", ["gemini", "bedrock"])
def test_every_sending_resolution_blocks_for_retired_cloud_provider(monkeypatch, retired):
    sys_s = {"cloud_provider": retired, "openai_api_key": "sk-real-looking-key",
             "personal_api_keys_allowed": False}
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: sys_s)
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    sent = _block_http(monkeypatch)

    def O(key):
        return {"provider": "openai", "key": key}

    def L(url):
        return {"provider": "ollama", "url": url}

    # (a) 送信用の構成・鍵を作る解決点: strict は例外、非 strict も構成・鍵を返さない。
    for strict in (True, False):
        with _expect(strict):
            assert llm.select_provider({}, openai=O, ollama=L, strict=strict) is None
        with _expect(strict):
            assert llm.resolve_auto_provider({}, strict=strict) is None
        with _expect(strict):
            assert keys.resolve_api_key("openai", {}, strict=strict) is None
        with _expect(strict):
            assert graph_extract.available({}, strict=strict, usage="render") is None
    # (a) 個別の呼び出し元（成形・埋め込み・intent・画像）。
    assert llm_render.available({}) is None
    assert embeddings.cfg({}, system_settings=sys_s) is None
    assert intent_llm._cfg({}, system_settings=sys_s) is None
    assert vision_arm._openai_key_with_reason(sys_s) == (None, True)
    # (a) 簡易チャット（外部 API）の OpenAI 接続準備。
    with pytest.raises(simple_chat.ProviderUnavailable):
        simple_chat._connect_openai(sys_s)
    # (a) Codex(OpenAI): auth.json 認証で keys.py を通らないため、プロセス境界（Popen）で確認する。
    import shutil
    import subprocess

    from sherpa.providers import _UnwiredProvider, _select_provider

    def _no_process(*a, **k):
        sent.append(a)
        raise AssertionError("Codex プロセスが起動された")
    monkeypatch.setattr(subprocess, "Popen", _no_process)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    provider = _select_provider({"agent": "codex", "codex_model_provider": "openai"}, sys_s)
    assert isinstance(provider, _UnwiredProvider)
    assert sent == []
