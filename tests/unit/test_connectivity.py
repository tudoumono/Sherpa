"""つながりの確認（`POST /admin/connectivity/check`・`sherpa/connectivity.py`）。外部の境界（HTTP・DB・子プロセス）だけ差し替える。"""
from __future__ import annotations

import io
import json
import subprocess
import urllib.error

import pytest
from fastapi.testclient import TestClient

from sherpa import connectivity, embeddings, health, llm, store
from sherpa.api import app
from sherpa.ingest import worker_status
from sherpa.routers import connectivity_admin

KEY = "sk-test-SECRETKEY-0123456789"
ES_CRED_URL = "http://esuser:espass-secret@localhost:9200"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(connectivity_admin, "_current_user",
                        lambda request: {"uid": "adm", "role": "admin", "status": "active"})
    monkeypatch.setattr(store, "audit", lambda *a, **k: None)
    monkeypatch.setattr(worker_status, "live_workers", lambda: [])   # 既定は取り込みの係が動いていない
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def boundaries(monkeypatch):
    """DB・HTTP・子プロセスの境界。OpenAI への送信は `llm.post_json` の呼び出しとして数える。"""
    sent = []
    monkeypatch.setenv("ES_URL", ES_CRED_URL)
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    sys_s = {"cloud_provider": "openai", "openai_api_key": KEY, "embed_provider": "auto",
             "research_default_provider": "openai"}
    monkeypatch.setattr(store, "get_system_settings", lambda **k: dict(sys_s))
    monkeypatch.setattr(health, "_ping_postgres", lambda: None)
    monkeypatch.setattr(health, "_ping_neo4j", lambda: None)
    monkeypatch.setattr(health, "ollama_in_use", lambda s, rows=None: False)
    monkeypatch.setattr(llm, "assert_openai_io_allowed", lambda: None)

    def fake_urlopen(*a, **k):
        raise urllib.error.URLError(ConnectionRefusedError())
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    def fake_post_json(url, headers, body, timeout=90):
        sent.append(url)
        raise urllib.error.HTTPError(url, 401, "bad", {}, None)
    monkeypatch.setattr(llm, "post_json", fake_post_json)

    runs = []

    def fake_run(cmd, **k):
        runs.append((cmd, k))
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
    monkeypatch.setattr(connectivity.subprocess, "run", fake_run)

    # Codex の起動（子プロセス）の境界。本物の codex は起こさない。
    codex_runs = []
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: "/usr/bin/" + name if name == "codex" else None)
    monkeypatch.setattr(store, "get_settings", lambda uid="admin": {"agent": "simple", "codex_model_provider": "openai"})

    def fake_codex(argv, env, cwd):
        codex_runs.append((argv, env))
        return 1, "401 Unauthorized " + KEY
    monkeypatch.setattr(connectivity, "_run_codex_once", fake_codex)
    return {"sent": sent, "runs": runs, "sys": sys_s, "codex": codex_runs}


def _post(client, paid=False):
    return client.post("/admin/connectivity/check", json={"include_paid": paid})


def test_non_admin_is_403(monkeypatch):
    monkeypatch.setattr(connectivity_admin, "_current_user",
                        lambda request: {"uid": "u", "role": "user", "status": "active"})
    r = TestClient(app, raise_server_exceptions=False).post(
        "/admin/connectivity/check", json={"include_paid": False})
    assert r.status_code == 403


def test_free_run_sends_nothing_to_openai(client, boundaries):
    r = _post(client, paid=False)
    assert r.status_code == 200
    api = {x["id"]: x for x in r.json()["paths"][0]["results"]}
    assert api["embed"]["status"] == "skipped" and api["embed"]["paid"] is True
    assert api["answer_ai"]["status"] == "skipped" and api["answer_ai"]["paid"] is True
    assert api["elasticsearch"]["status"] == "ng" and api["elasticsearch"]["reason"] == "接続を拒否された"
    assert boundaries["sent"] == []


def test_paid_run_sends_and_results_hide_secrets(client, boundaries):
    r = _post(client, paid=True)
    assert r.status_code == 200
    assert boundaries["sent"], "有料の確認は OpenAI へ送る"
    body = r.text
    for secret in (KEY, "espass-secret", "esuser"):
        assert secret not in body
    api = {x["id"]: x for x in r.json()["paths"][0]["results"]}
    assert api["answer_ai"]["status"] == "ng" and "401" in api["answer_ai"]["reason"]
    assert api["elasticsearch"]["host"] == "localhost"


def test_child_runs_with_mcp_env(client, boundaries, monkeypatch):
    from sherpa.providers.codex import mcp
    seen = {}

    def fake_env(world, scope_paths, *a, **k):
        seen["args"] = (world, scope_paths)
        return {"ES_URL": "http://es.example:9200", "SHERPA_MCP_WORLD": world}
    monkeypatch.setattr(mcp, "_mcp_env", fake_env)
    r = _post(client, paid=True)
    assert r.status_code == 200
    (cmd, kw), = boundaries["runs"]
    assert cmd[1:] == ["-m", "sherpa.connectivity", "--json", "--paid"]
    assert kw["env"]["ES_URL"] == "http://es.example:9200"
    assert seen["args"][1] is None
    assert kw["timeout"] == connectivity.CHILD_TIMEOUT_S == 60.0


def test_child_timeout_is_incomplete(client, boundaries, monkeypatch):
    def boom(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, 60)
    monkeypatch.setattr(connectivity.subprocess, "run", boom)
    r = _post(client)
    mcp_rows = r.json()["paths"][1]["results"]
    assert mcp_rows and all(x["status"] == "incomplete" for x in mcp_rows)
    assert "postgres" not in {x["id"] for x in mcp_rows}


def test_second_concurrent_run_is_409(client, boundaries):
    assert connectivity_admin._running.acquire(blocking=False)
    try:
        assert _post(client).status_code == 409
    finally:
        connectivity_admin._running.release()
    assert _post(client).status_code == 200


def _embed_ollama_row(monkeypatch, fake_post):
    monkeypatch.setattr(store, "get_system_settings", lambda **k: {})
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    monkeypatch.setattr(embeddings, "cfg", lambda *a, **k: {
        "provider": "ollama", "url": "http://localhost:11434", "model": "m", "dim": 8})
    monkeypatch.setattr(llm, "post_json", fake_post)
    return {x["id"]: x for x in connectivity.run_checks(True)}["embed"]


def test_embed_dimension_mismatch_is_ng(monkeypatch):
    """応答の次元が設定と違うと、実際の `embed()` を通って次元不一致が理由になる。"""
    row = _embed_ollama_row(monkeypatch, lambda url, headers, body, timeout=90: {"embeddings": [[0.5] * 4]})
    assert row["status"] == "ng" and "次元が合わない（設定 8・応答 4）" in row["reason"]
    assert row["route"] == "直接"


def test_embed_http_401_is_auth_failure(monkeypatch):
    """HTTP 401 は認証の失敗が理由になる（再送しない）。"""
    def fake_post(url, headers, body, timeout=90):
        raise urllib.error.HTTPError(url, 401, "bad", {}, None)
    row = _embed_ollama_row(monkeypatch, fake_post)
    assert row["status"] == "ng" and "認証に失敗（401）" in row["reason"]


def test_embed_failure_log_keeps_the_error_kind_from_the_response_body(monkeypatch, caplog):
    """受け皿が無い通常の経路でも、失敗のログに応答の本文の分類が残る。"""
    payload = json.dumps({"error": {"type": "insufficient_quota", "code": "insufficient_quota"}}).encode()

    def fake_post(url, headers, body, timeout=90):
        raise urllib.error.HTTPError(url, 400, "bad", {}, io.BytesIO(payload))
    monkeypatch.setattr(llm, "post_json", fake_post)
    with caplog.at_level("WARNING", logger=embeddings._log.name):
        assert embeddings.embed(["a"], {"provider": "ollama", "url": "http://localhost:11434", "model": "m", "dim": 8}) is None
    assert "error_type=insufficient_quota error_code=insufficient_quota" in caplog.text


def test_route_matches_urllib_for_no_proxy_with_port(monkeypatch):
    """`NO_PROXY` にポート付きで書いた宛先は、urllib と同じく直接と判定する。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "api.example:8443")
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert connectivity.route_for("https://api.example:8443/v1") == "直接"
    assert connectivity.route_for("https://api.example:9443/v1") == "プロキシ経由"
    # 資格情報付きの URL は、urllib が資格情報ごと例外に照らす（当たらずプロキシへ行く）のと同じに判定する
    assert connectivity.route_for("https://user:pw@api.example:8443/v1") == "プロキシ経由"


@pytest.mark.parametrize("paid", [False, True])
def test_answer_check_follows_simple_answer_to_ollama(client, boundaries, paid):
    """簡易の回答が Ollama を選ぶ構成では、OpenAI のキーがあっても回答の AI の確認は Ollama へ（無料で）送り、OpenAI へは送らない。"""
    boundaries["sys"]["research_default_provider"] = "ollama"
    r = _post(client, paid=paid)
    assert r.status_code == 200
    api = {x["id"]: x for x in r.json()["paths"][0]["results"]}
    assert api["answer_ai"]["paid"] is False and api["answer_ai"]["status"] != "skipped"
    assert any(u.endswith("/api/chat") for u in boundaries["sent"])
    assert not any("chat/completions" in u for u in boundaries["sent"])


def test_answer_check_names_models_without_tool_support(client, boundaries, monkeypatch):
    """道具に対応しないモデル（400 の本文で分かる）は、その旨の決まった言葉で出し、本文は出さない。"""
    import io
    boundaries["sys"]["research_default_provider"] = "ollama"

    def no_tools(url, headers, body, timeout=90):
        raise urllib.error.HTTPError(url, 400, "bad", {}, io.BytesIO(b'{"error":"zz-model does not support tools"}'))
    monkeypatch.setattr(llm, "post_json", no_tools)
    r = _post(client)
    api = {x["id"]: x for x in r.json()["paths"][0]["results"]}
    assert api["answer_ai"]["status"] == "ng" and "道具" in api["answer_ai"]["reason"]
    assert "zz-model" not in r.text


def test_answer_check_sends_the_same_first_round_as_simple_answer(client, boundaries, monkeypatch):
    """回答の AI の確認は、簡易の回答の 1 往復目と同じ system・道具・上限時間で送る。"""
    from sherpa import agentic_search, simple_chat
    boundaries["sys"]["research_default_provider"] = "ollama"
    seen = []
    monkeypatch.setattr(agentic_search, "_post", lambda url, headers, body, timeout=90: seen.append((body, timeout)) or {})
    monkeypatch.setattr(agentic_search, "tool_availability", lambda force=False: {"grep": True, "fulltext": True, "graph": True})
    r = _post(client)
    assert r.status_code == 200
    (body, timeout), = seen
    assert body["messages"][0] == {"role": "system", "content": simple_chat._SYSTEM_PROMPT}
    assert body["tools"] == simple_chat._build_tools({"grep": True, "fulltext": True, "graph": True})
    assert timeout == simple_chat._MAX_PER_CALL_TIMEOUT_S


def test_child_path_does_not_run_answer_check(monkeypatch):
    """簡易の回答は API のプロセスで動くので、道具の子の結果に回答の AI の確認は出さない。"""
    monkeypatch.setattr(store, "get_system_settings", lambda **k: {})
    monkeypatch.setattr(connectivity, "_CHECKS", tuple(
        (cid, label, paid, lambda ctx, p: {"route": None}) for cid, label, paid, _fn in connectivity._CHECKS))
    rows = connectivity.run_checks(False, path="mcp")
    assert {r["id"] for r in rows} == {"elasticsearch", "neo4j", "ollama", "embed"}


@pytest.mark.parametrize("resp", [[], "text", {"choices": ["x"]}, {"message": "x"}, {"choices": []}, {"message": None}, {}])
def test_answer_check_fails_when_the_reply_cannot_be_read_like_simple_answer(client, boundaries, monkeypatch, resp):
    """応答の形が簡易の回答で読めないものなら、つながっていても「つながる」と出さない。"""
    from sherpa import agentic_search
    boundaries["sys"]["research_default_provider"] = "ollama"
    monkeypatch.setattr(agentic_search, "_post", lambda url, headers, body, timeout=90: resp)
    api = {x["id"]: x for x in _post(client).json()["paths"][0]["results"]}
    assert api["answer_ai"]["status"] == "ng" and "応答の形" in api["answer_ai"]["reason"]


def test_response_has_four_paths_and_free_run_sends_nothing_from_codex(client, boundaries):
    """経路は 4 つ。無料の実行では OpenAI 系の Codex を起動しない（有料の印だけ出す）。"""
    r = _post(client, paid=False)
    paths = {p["path"]: p for p in r.json()["paths"]}
    assert list(paths) == ["api", "mcp", "ingest", "codex"]
    assert paths["ingest"]["label"] == "取り込みの係"
    (codex,) = paths["codex"]["results"]
    assert codex["id"] == "codex" and codex["status"] == "skipped" and codex["paid"] is True
    assert boundaries["codex"] == [] and boundaries["sent"] == []
    assert {x["id"] for x in paths["ingest"]["results"]} == {"postgres", "elasticsearch", "neo4j", "ollama", "embed"}
    assert all(x["status"] == "incomplete" and "動いていない" in x["reason"] for x in paths["ingest"]["results"])


def test_paid_codex_failure_reason_is_fixed_wording_without_output(client, boundaries):
    """Codex の出力（鍵を含む）は判定にだけ使い、結果には決まった言葉だけを出す。起動した子の env に DB・ES の資格情報は無い。"""
    r = _post(client, paid=True)
    (codex,) = {p["path"]: p for p in r.json()["paths"]}["codex"]["results"]
    assert codex["status"] == "ng" and codex["reason"] == "認証に失敗（401/403）" and codex["paid"] is True
    assert KEY not in r.text and "espass-secret" not in r.text
    (argv, env), = boundaries["codex"]
    assert argv[:3] == ["codex", "exec", "--json"] and argv[-1] == "-" and "--ephemeral" in argv
    assert "ES_URL" not in env and "OPENAI_API_KEY" not in env


def test_codex_timeout_and_ollama_is_free(client, boundaries, monkeypatch):
    """Ollama で動かす Codex は無料で流す。上限を超えたら「時間切れ」。"""
    from sherpa import keys
    monkeypatch.setattr(store, "get_settings", lambda uid="admin": {"agent": "codex", "codex_model_provider": "ollama"})
    monkeypatch.setattr(connectivity, "_run_codex_once", lambda argv, env, cwd: (None, ""))
    from sherpa.providers import _select_provider  # noqa: F401
    monkeypatch.setattr(keys, "resolve_ollama_url", lambda *a, **k: "http://localhost:11434")
    monkeypatch.setattr(llm, "assert_ollama_url_allowed", lambda *a, **k: None)
    from sherpa import model_catalog
    monkeypatch.setattr(model_catalog, "resolve_model", lambda *a, **k: "gpt-oss:20b")
    (codex,) = {p["path"]: p for p in _post(client, paid=False).json()["paths"]}["codex"]["results"]
    assert codex["paid"] is False and codex["status"] == "ng" and codex["reason"].startswith("時間切れ")
    assert codex["host"] == "localhost"


def test_codex_on_openai_shows_the_builtin_endpoint_host(client, boundaries):
    """OpenAI 本家で動かす Codex も、宛先（組み込みの接続先のホスト）と経路を出す。"""
    (codex,) = {p["path"]: p for p in _post(client, paid=False).json()["paths"]}["codex"]["results"]
    assert codex["host"] == "api.openai.com" and codex["route"] in ("直接", "プロキシ経由")


@pytest.mark.parametrize("resp", [{"choices": [{"message": {"content": ""}}]}, {"message": {"content": "   "}}, {"message": {}}])
def test_answer_check_fails_on_an_empty_reply(client, boundaries, monkeypatch, resp):
    """道具の呼び出しも本文も無い返事は、簡易の回答も空になるので「つながる」と出さない。"""
    from sherpa import agentic_search
    boundaries["sys"]["research_default_provider"] = "ollama"
    monkeypatch.setattr(agentic_search, "_post", lambda url, headers, body, timeout=90: resp)
    api = {x["id"]: x for x in _post(client).json()["paths"][0]["results"]}
    assert api["answer_ai"]["status"] == "ng" and "空" in api["answer_ai"]["reason"]


@pytest.mark.parametrize("text, expect", [
    ("Error: thread/start failed: fs sandbox helper failed with status exit status: 71: sandbox-exec: execvp() of "
     "'/zz/bin/codex' failed: Operation not permitted", "サンドボックス"),
    ('{"type":"turn.failed","error":{"message":"The \'zz-model\' model is not supported when using Codex with a ZZ account."}}',
     "使えない"),
    ("bwrap: Failed RTM_NEWADDR: Operation not permitted", "つながらない"),  # 名前空間の問題で、導入先の許可の問題ではない
    ("Error: fs sandbox helper failed with status exit status: 1: bwrap: Failed RTM_NEWADDR: Operation not permitted",
     "つながらない"),
])
def test_codex_failure_reason_names_real_codex_messages(text, expect):
    """本物の Codex が出す、サンドボックスが自身を起動できない・モデルがその使い方では使えない、の文言を決まった理由に振り分ける。"""
    assert expect in connectivity._codex_failure_reason(text)
