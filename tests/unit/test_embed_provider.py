"""埋め込みの接続先（システム設定 `embed_provider`）の単体テスト。

モックは外部境界（`llm.post_json`）と ES の状態取得だけに置く。
"""
from __future__ import annotations

import pytest

from sherpa import embeddings, es_index, llm

_CLOUD = {"cloud_provider": "openai", "openai_api_key": "sk-test"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    embeddings._ollama_dim_cache.clear()
    embeddings._ollama_dim_failed.clear()


def test_cfg_ollama_overrides_selected_cloud_and_auto_is_unchanged():
    ec = embeddings.cfg(None, system_settings={**_CLOUD, "embed_provider": "ollama"})
    assert ec["provider"] == "ollama" and ec["model"] == "nomic-embed-text" and ec["dim"] == 768
    assert ec["url"] == "http://localhost:11434"
    for s in (_CLOUD, {**_CLOUD, "embed_provider": "auto"}, {**_CLOUD, "embed_provider": "bogus"}):
        assert embeddings.cfg(None, system_settings=s)["provider"] == "openai"


def test_cfg_ollama_unresolvable_is_none_without_cloud_fallback(monkeypatch):
    def boom(*a, **k):
        raise OSError("unreachable")
    monkeypatch.setattr(llm, "post_json", boom)
    s = {**_CLOUD, "embed_provider": "ollama",
         "model_catalog": {"ollama": {"embed": {"allowed": ["custom-embed"], "default": "custom-embed"}}}}
    assert embeddings.cfg(None, system_settings=s) is None
    assert embeddings.cloud_selected_but_unavailable(system_settings=s) is True
    assert embeddings.cloud_selected_but_unavailable(system_settings={"embed_provider": "ollama"}) is True


def test_ollama_dim_known_table_and_probe(monkeypatch):
    s = {"embed_provider": "ollama",
         "model_catalog": {"ollama": {"embed": {"allowed": ["bge-m3"], "default": "bge-m3"}}}}
    assert embeddings.cfg(None, system_settings=s)["dim"] == 1024   # 既知表（通信なし）
    sent = []

    def fake_post(url, headers, body, timeout):
        sent.append(body)
        return {"embeddings": [[0.1] * 384]}
    monkeypatch.setattr(llm, "post_json", fake_post)
    s["model_catalog"]["ollama"]["embed"] = {"allowed": ["odd-embed:q8"], "default": "odd-embed:q8"}
    assert embeddings.cfg(None, system_settings=s)["dim"] == 384    # 未知モデルは実測
    embeddings.cfg(None, system_settings=s)
    assert len(sent) == 1                                          # 2 回目はキャッシュ


def test_provider_switch_changes_cache_key_and_triggers_reindex(monkeypatch):
    oa = embeddings.cfg(None, system_settings=_CLOUD)
    ol = embeddings.cfg(None, system_settings={**_CLOUD, "embed_provider": "ollama"})
    assert es_index._chunk_key(oa, "本文") != es_index._chunk_key(ol, "本文")
    meta = {"content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
            "search_chunk_mode": es_index._search_chunk_mode(), "arms_sig": "a", "analyzer_config_sig": "z",
            "chunk_lines": es_index._CHUNK_LINES, "human_md_sig": None,
            "embed_provider": "openai", "embed_model": oa["model"], "dim": oa["dim"],
            "embed_algo": embeddings.EMBEDDING_INPUT_ALGORITHM_ID}
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "a")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda w: None)
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "z")
    monkeypatch.setattr(es_index, "_index_meta", lambda w: meta)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: oa)
    assert es_index.needs_reindex("w", "c1") is False
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: ol)
    assert es_index.needs_reindex("w", "c1") is True
