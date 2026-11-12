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
    embeddings._ollama_legacy_urls.clear()
    embeddings._float_only_urls.clear()


def _v1(dim):
    return {"data": [{"index": 0, "embedding": [0.1] * dim}]}


def test_cfg_ollama_overrides_selected_cloud_and_auto_is_unchanged(monkeypatch):
    monkeypatch.setattr(llm, "post_json", lambda *a, **k: _v1(768))
    ec = embeddings.cfg(None, system_settings={**_CLOUD, "embed_provider": "ollama"})
    assert ec["provider"] == "ollama" and ec["model"] == "bge-m3" and ec["dim"] == 768
    assert ec["url"] == "http://localhost:11434"
    for s in (_CLOUD, {**_CLOUD, "embed_provider": "auto"}):
        assert embeddings.cfg(None, system_settings=s)["provider"] == "openai"
    assert embeddings.cfg(None, system_settings={**_CLOUD, "embed_provider": "bogus"}) is None  # 不正値は auto に丸めない


def test_cfg_ollama_unmeasurable_keeps_the_target_without_cloud_fallback(monkeypatch):
    def boom(*a, **k):
        raise OSError("unreachable")
    monkeypatch.setattr(llm, "post_json", boom)
    s = {**_CLOUD, "embed_provider": "ollama",
         "model_catalog": {"ollama": {"embed": {"allowed": ["custom-embed"], "default": "custom-embed"}}}}
    ec = embeddings.cfg(None, system_settings=s)  # 次元を測れなくても、使う接続先とモデルは答える（クラウドへは倒さない）
    assert (ec["provider"], ec["model"], ec["dim"]) == ("ollama", "custom-embed", None)
    assert embeddings.cloud_selected_but_unavailable(system_settings={"embed_provider": "ollama"}) is False  # Ollama は「成立しない」設定ではない


def test_ollama_dim_probe_is_authoritative_and_failure_leaves_dim_unknown_even_for_known_model(monkeypatch):
    s = {"embed_provider": "ollama",
         "model_catalog": {"ollama": {"embed": {"allowed": ["bge-m3"], "default": "bge-m3"}}}}
    monkeypatch.setattr(llm, "post_json", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert embeddings.cfg(None, system_settings=s)["dim"] is None   # 実測できなければ、よく知られたモデルでも次元を決めない
    embeddings._ollama_dim_failed.clear()
    monkeypatch.setattr(llm, "post_json", lambda *a, **k: _v1(4096))
    assert embeddings.cfg(None, system_settings=s)["dim"] == 4096   # 表と違えば実測を正とする
    sent = []

    def fake_post(url, headers, body, timeout):
        sent.append(body)
        return _v1(384)
    monkeypatch.setattr(llm, "post_json", fake_post)
    s["model_catalog"]["ollama"]["embed"] = {"allowed": ["odd-embed:q8"], "default": "odd-embed:q8"}
    assert embeddings.cfg(None, system_settings=s)["dim"] == 384    # 未知モデルは実測
    embeddings.cfg(None, system_settings=s)
    assert len(sent) == 1                                          # 2 回目はキャッシュ


_AUTO_OLLAMA_4096 = {"ollama_url": "http://localhost:11434", "model_catalog": {"ollama": {"embed": {"allowed": ["big-embed"], "default": "big-embed"}}}}


def test_auto_route_ollama_uses_probed_dim_not_fixed_768(monkeypatch):
    monkeypatch.setattr(llm, "post_json", lambda *a, **k: _v1(4096))
    ec = embeddings.cfg(None, system_settings=_AUTO_OLLAMA_4096)
    assert ec["provider"] == "ollama" and ec["model"] == "big-embed" and ec["dim"] == 4096


def test_auto_route_ollama_unmeasurable_keeps_the_target(monkeypatch):
    monkeypatch.setattr(llm, "post_json", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    ec = embeddings.cfg(None, system_settings=_AUTO_OLLAMA_4096)
    assert (ec["provider"], ec["url"], ec["model"], ec["dim"]) == ("ollama", "http://localhost:11434", "big-embed", None)
    assert embeddings.cloud_selected_but_unavailable(system_settings=_AUTO_OLLAMA_4096) is False


def test_auto_route_unconfigured_ollama_is_not_flagged_unavailable():
    assert embeddings.cloud_selected_but_unavailable(system_settings={}) is False


def test_log_target_logs_one_line_without_secrets(caplog):
    ec = {"provider": "ollama", "url": "http://user:pw@localhost:11434/x", "model": "m", "dim": 8}
    with caplog.at_level("INFO", logger="sherpa.embed"):
        assert embeddings.log_target(ec, "w") is ec
    assert [r.getMessage() for r in caplog.records] == [
        "embed 接続先 provider=ollama host=localhost:11434 model=m dim=8（world=w）"]


def test_describe_omits_credentials_and_path():
    line = embeddings.describe({"provider": "ollama", "url": "http://user:pw@host.example:11434/secret/path",
                                "model": "m", "dim": 8})
    assert line == "provider=ollama host=host.example:11434 model=m dim=8"


def test_provider_switch_changes_cache_key_and_triggers_reindex(monkeypatch):
    monkeypatch.setattr(llm, "post_json", lambda *a, **k: _v1(768))
    oa = embeddings.cfg(None, system_settings=_CLOUD)
    ol = embeddings.cfg(None, system_settings={**_CLOUD, "embed_provider": "ollama"})
    assert es_index._chunk_key(oa, "本文") != es_index._chunk_key(ol, "本文")
    meta = {"content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
            "arms_sig": "a", "analyzer_config_sig": "z",
            "chunk_lines": es_index._CHUNK_LINES, "human_md_sig": None, "source_excluded": True,
            "embed_provider": "openai", "embed_model": oa["model"], "dim": oa["dim"],
            "embed_algo": embeddings.EMBEDDING_INPUT_ALGORITHM_ID, "quant": embeddings.ES_VECTOR_QUANT}
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
