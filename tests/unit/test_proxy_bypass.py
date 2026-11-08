"""社内の宛先がプロキシを通らない扱いになる契約（`HTTP_PROXY` あり・`NO_PROXY` 空の環境）。"""
from __future__ import annotations

import os
import urllib.request

import pytest

SS = {"ollama_url": "http://ollama-host:11434", "ollama_allowlist": ["10.1.2.3:11434"]}


@pytest.fixture
def proxied_env(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://user:secret@proxy.corp.example:3128")
    monkeypatch.setenv("http_proxy", "http://user:secret@proxy.corp.example:3128")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("ES_URL", "http://es.corp.example:9200")
    monkeypatch.setenv("NEO4J_URI", "bolt://neo.corp.example:7687")
    monkeypatch.setenv("SHERPA_PG_DSN", "postgresql://u:pw@pg.corp.example:5432/sherpa")


def test_apply_makes_internal_destinations_direct(proxied_env):
    from sherpa import proxy_bypass
    assert urllib.request.proxy_bypass("es.corp.example") is False
    value = proxy_bypass.apply(SS)
    assert os.environ["NO_PROXY"] == os.environ["no_proxy"] == value
    for host in ("es.corp.example", "neo.corp.example", "pg.corp.example",
                 "ollama-host", "10.1.2.3", "localhost", "127.0.0.1", "::1"):
        assert host in value.split(","), host
        assert urllib.request.proxy_bypass(host), host
    assert not urllib.request.proxy_bypass("api.openai.com")


def test_apply_keeps_existing_entries_and_is_idempotent(proxied_env, monkeypatch):
    from sherpa import proxy_bypass
    monkeypatch.setenv("NO_PROXY", ".corp.example,LOCALHOST")
    first = proxy_bypass.apply(SS)
    assert first.split(",")[:2] == [".corp.example", "LOCALHOST"]
    assert [h.lower() for h in first.split(",")].count("localhost") == 1
    assert proxy_bypass.apply(SS) == first


def test_saved_ollama_settings_reach_child_process_env(proxied_env, tmp_path, monkeypatch):
    from sherpa import proxy_bypass, store
    from sherpa.providers.codex import sandbox
    proxy_bypass.apply({"ollama_allowlist": []})
    # 保存済みの設定（DB）を子の起動直前の apply が読み、子に渡す env へ新しい接続先が入る
    monkeypatch.setattr(store, "get_system_settings", lambda: {"ollama_allowlist": ["10.9.9.9:11434"]})
    env = sandbox._codex_clean_env(tmp_path / "ch", tmp_path / "tmp")
    assert "10.9.9.9" in env["NO_PROXY"].split(",") and "10.9.9.9" in env["no_proxy"].split(",")


def test_unreadable_settings_do_not_stop_startup(proxied_env, monkeypatch):
    from sherpa import es_index, proxy_bypass
    monkeypatch.setattr(es_index, "_url", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    value = proxy_bypass.apply(SS)
    assert "neo.corp.example" in value.split(",") and "localhost" in value.split(",")


def test_destination_removed_from_settings_leaves_no_proxy(proxied_env, monkeypatch):
    from sherpa import proxy_bypass
    monkeypatch.setenv("NO_PROXY", ".corp.example")
    proxy_bypass.apply(SS)
    assert "10.1.2.3" in os.environ["NO_PROXY"].split(",")
    value = proxy_bypass.apply({"ollama_allowlist": []})
    assert "10.1.2.3" not in value.split(",") and "ollama-host" not in value.split(",")
    assert ".corp.example" in value.split(",")


def test_external_ollama_hosts_never_become_proxy_exceptions(proxied_env):
    from sherpa import proxy_bypass
    ss = {"ollama_url": "https://api.openai.com:443",
          "ollama_allowlist": ["api.openai.com:443", "8.8.8.8:11434", "gpu.corp.example",
                               "gpu1.local:11434", "172.16.0.5", "169.254.1.1", "[fd00::1]:11434", "intra"]}
    hosts = set(proxy_bypass.apply(ss).split(","))
    assert {"gpu1.local", "172.16.0.5", "169.254.1.1", "intra"} <= hosts
    assert not hosts & {"api.openai.com", "8.8.8.8", "gpu.corp.example"}


def test_mcp_tool_server_start_does_not_read_settings_db(proxied_env, monkeypatch):
    from sherpa import mcp_server, store
    called = []
    monkeypatch.setattr(store, "get_system_settings", lambda: called.append(1) or {})
    src = open(mcp_server.__file__, encoding="utf-8").read()
    block = src[src.index('if __name__ == "__main__":'):]
    exec(compile(block, mcp_server.__file__, "exec"),
         {"__name__": "__main__", "proxy_bypass": mcp_server.proxy_bypass, "serve": lambda: None})
    assert not called
