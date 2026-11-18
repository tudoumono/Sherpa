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
