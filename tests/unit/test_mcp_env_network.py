"""MCP サブプロセスへ渡す外への経路の設定（プロキシ・社内 CA）の契約テスト。"""
from __future__ import annotations


def test_mcp_env_passes_proxy_and_ca_like_codex_itself(monkeypatch):
    """MCP サブプロセス（質問の埋め込みで外へ出る）にも、Codex 本体と同じプロキシ・社内 CA の設定を渡す。未設定・空は渡さない。"""
    from sherpa.providers.codex import mcp
    for k in mcp.NETWORK_PASSTHROUGH_ENV:
        monkeypatch.delenv(k, raising=False)
    env = mcp._mcp_env("v1", None)
    assert not any(k in env for k in mcp.NETWORK_PASSTHROUGH_ENV if k not in ("NO_PROXY", "no_proxy"))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:8080")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/certs/corp.pem")
    monkeypatch.setenv("HTTP_PROXY", "")
    env = mcp._mcp_env("v1", None)
    assert env["HTTPS_PROXY"] == "http://proxy.internal:8080" and {"localhost", "127.0.0.1"} <= set(env["no_proxy"].split(","))
    assert env["SSL_CERT_FILE"] == "/etc/ssl/certs/corp.pem" and "HTTP_PROXY" not in env


def test_proxy_url_with_credentials_never_appears_in_codex_argv(monkeypatch):
    """認証情報入りのプロキシ URL は Codex の argv に出さず、名前だけ `env_vars` で渡す（値は起動 env から受け継ぐ）。"""
    from sherpa.providers.codex import mcp
    secret = "http://user:topsecret@proxy.internal:8080"
    monkeypatch.setenv("HTTP_PROXY", secret)
    monkeypatch.setenv("https_proxy", secret)
    args = mcp._mcp_config_args("v1", None)
    assert not any("topsecret" in a for a in args)
    env_vars = next(a for a in args if a.startswith("mcp_servers.sherpa.env_vars"))
    assert '"HTTP_PROXY"' in env_vars and '"https_proxy"' in env_vars
    env_arg = next(a for a in args if a.startswith("mcp_servers.sherpa.env ="))
    assert "HTTP_PROXY" not in env_arg and "NO_PROXY" not in env_arg
    assert mcp._mcp_env("v1", None)["HTTP_PROXY"] == secret  # 子の env（sandbox の config.toml・起動 env）には届く


def test_db_secrets_never_appear_in_codex_argv(monkeypatch):
    """DB のパスワードと、認証情報を含み得る接続先 URL は Codex の argv に出さず、名前だけ `env_vars` で渡す。"""
    from sherpa.providers.codex import mcp
    monkeypatch.setenv("NEO4J_PASSWORD", "pw-topsecret")
    monkeypatch.setenv("NEO4J_URI", "bolt://neo:uri-topsecret@db.internal:7687")
    monkeypatch.setenv("ES_URL", "http://es:es-topsecret@es.internal:9200")
    args = mcp._mcp_config_args("v1", None)
    assert not any("topsecret" in a for a in args)
    env_vars = next(a for a in args if a.startswith("mcp_servers.sherpa.env_vars"))
    for k in ("NEO4J_PASSWORD", "NEO4J_URI", "ES_URL"):
        assert f'"{k}"' in env_vars
    env_arg = next(a for a in args if a.startswith("mcp_servers.sherpa.env ="))
    assert not any(k in env_arg for k in ("NEO4J_PASSWORD", "NEO4J_URI", "ES_URL"))
    env = mcp._mcp_env("v1", None)
    assert env["NEO4J_PASSWORD"] == "pw-topsecret" and "uri-topsecret" in env["NEO4J_URI"] and "es-topsecret" in env["ES_URL"]


def test_shell_env_policy_keeps_proxy_and_api_key_out_of_model_shell(tmp_path, monkeypatch):
    """認証つきプロキシ URL と接続用 API キーは Codex 本体の env にあっても、モデルの実行シェルの env には継承させない。"""
    import tomllib
    from sherpa.providers.codex import mcp, sandbox
    monkeypatch.setenv("HTTPS_PROXY", "http://user:topsecret@proxy.internal:8080")
    home = tmp_path / "ch"
    home.mkdir()
    sandbox._write_codex_authoring_config(home, ["/kb"], "low", False, "test", None)
    policy = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))["shell_environment_policy"]
    assert policy.get("inherit") in ("core", "none")
    assert 'shell_environment_policy.inherit = "core"' in mcp._mcp_config_args("v1", None)


def test_mcp_env_forwards_tool_deadline_and_search_embed_timeout(monkeypatch):
    """道具の待ち時間（道具 1 回の期限・検索の埋め込みの期限）の設定は、Codex 経由の道具にも同じ値で届く。未設定・空は渡さない。"""
    from sherpa.providers.codex import mcp
    monkeypatch.setenv("SHERPA_MCP_TOOL_DEADLINE_SEC", "240")
    monkeypatch.setenv("SHERPA_SEARCH_EMBED_TIMEOUT_SEC", "60")
    env = mcp._mcp_env("v1", None)
    assert env["SHERPA_MCP_TOOL_DEADLINE_SEC"] == "240" and env["SHERPA_SEARCH_EMBED_TIMEOUT_SEC"] == "60"
    monkeypatch.setenv("SHERPA_MCP_TOOL_DEADLINE_SEC", "")
    monkeypatch.delenv("SHERPA_SEARCH_EMBED_TIMEOUT_SEC")
    env = mcp._mcp_env("v1", None)
    assert "SHERPA_MCP_TOOL_DEADLINE_SEC" not in env and "SHERPA_SEARCH_EMBED_TIMEOUT_SEC" not in env


def test_embed_tuning_settings_reach_mcp_child_via_snapshot(monkeypatch):
    """管理画面の埋め込みの待ち時間・件数は、設定の写しで道具の子プロセスに同じ値で届く。"""
    import json
    from sherpa import embeddings, store
    from sherpa.providers.codex import mcp
    monkeypatch.setattr(store, "get_system_settings", lambda: {"embed_request_timeout_s": 120, "embed_batch_size": 20})
    monkeypatch.setattr(store, "get_settings", lambda: {})
    snap = json.loads(mcp._mcp_env("v1", None)[store.CHILD_SNAPSHOT_ENV])["system"]
    assert embeddings.effective_embed_request_timeout(snap) == 120 and embeddings.effective_embed_batch_size(snap) == 20
