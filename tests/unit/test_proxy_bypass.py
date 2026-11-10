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


def test_registered_ollama_hosts_all_become_proxy_exceptions(proxied_env):
    from sherpa import proxy_bypass
    ss = {"ollama_url": "https://gpu.vendor.example:443",
          "ollama_allowlist": ["gpu.corp.example", "8.8.8.8:11434", "gpu1.local:11434", "172.16.0.5", "intra"]}
    hosts = set(proxy_bypass.apply(ss).split(","))
    assert {"gpu.vendor.example", "gpu.corp.example", "8.8.8.8", "gpu1.local", "172.16.0.5", "intra"} <= hosts
    for host in ("gpu.corp.example:11434", "8.8.8.8:11434"):
        assert urllib.request.proxy_bypass(host), host


def test_wildcard_and_suffix_forms_never_reach_proxy_exceptions(proxied_env):
    from sherpa import proxy_bypass
    ss = {"ollama_allowlist": ["*:11434", ".corp.example:11434", "a b:11434", "gpu.corp.example:11434"]}
    parts = proxy_bypass.apply(ss).split(",")
    assert "*" not in parts and not [p for p in parts if p.startswith(".")]
    assert "gpu.corp.example" in parts


def test_underscore_and_trailing_dot_hosts_are_added(proxied_env, monkeypatch):
    from sherpa import proxy_bypass
    monkeypatch.setenv("ES_URL", "http://es_node.corp.example.:9200")
    value = proxy_bypass.apply({"ollama_allowlist": ["gpu_1.corp.example:11434"]})
    parts = value.split(",")
    assert "gpu_1.corp.example" in parts and "es_node.corp.example." in parts
    assert urllib.request.proxy_bypass("gpu_1.corp.example:11434")
    assert urllib.request.proxy_bypass("es_node.corp.example.:9200")


def test_zone_id_with_wildcard_never_reaches_proxy_exceptions(proxied_env):
    from sherpa import proxy_bypass
    ss = {"ollama_allowlist": ["[fe80::1%a,.example.com,*]:11434", "[fe80::2%eth0]:11434"]}
    parts = proxy_bypass.apply(ss).split(",")
    assert "*" not in parts and not [p for p in parts if p.startswith(".") or "%" in p]


def test_ipv6_literals_match_urllib_bracket_form(proxied_env):
    from sherpa import proxy_bypass
    proxy_bypass.apply({"ollama_allowlist": ["[fd00::1]:11434"]})
    assert urllib.request.proxy_bypass("[::1]:11434")
    assert urllib.request.proxy_bypass("[fd00::1]:11434")


def test_ingest_worker_applies_proxy_bypass_per_claimed_run(monkeypatch):
    from sherpa import proxy_bypass, store
    from sherpa.ingest import background, ingest_worker, jobs
    calls = []
    monkeypatch.setattr(proxy_bypass, "apply", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(background, "bind_run", lambda run_id: None)
    monkeypatch.setattr(jobs, "run_job", lambda op, world, payload, run_id: None)
    monkeypatch.setattr(store, "fail_close_if_extracting", lambda run_id, reason=None: False)
    for i in (1, 2):
        ingest_worker.run_claimed({"id": i, "version": "w", "op": "sync", "payload": None})
    assert len(calls) == 2


def test_mcp_tool_server_start_does_not_read_settings_db(proxied_env, monkeypatch):
    from sherpa import mcp_server, store
    called = []
    monkeypatch.setattr(store, "get_system_settings", lambda: called.append(1) or {})
    src = open(mcp_server.__file__, encoding="utf-8").read()
    block = src[src.index('if __name__ == "__main__":'):]
    exec(compile(block, mcp_server.__file__, "exec"),
         {"__name__": "__main__", "proxy_bypass": mcp_server.proxy_bypass, "serve": lambda: None})
    assert not called
