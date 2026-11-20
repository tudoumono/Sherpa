"""sherpa.health の単体テスト（外部サービス不要・偽 ping/check に差し替えて検証）。

COMPONENTS / _AI_COMPONENTS / _SEARCH_COMPONENTS を偽関数へ差し替え、snapshot・ai_snapshot・
search_snapshot の集約・TTL/per-uid キャッシュ・並列実行と deadline・秘密/URL の伏せ字を検証する。
"""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.error

import pytest

from sherpa import embeddings, health, keys, llm, store

_ORIGINAL_COMPONENTS = health.COMPONENTS
_ORIGINAL_AI_COMPONENTS = health._AI_COMPONENTS
_ORIGINAL_SEARCH_COMPONENTS = health._SEARCH_COMPONENTS


def _ok():
    return None


def _fail():
    raise RuntimeError("boom")


def _swap(original, fn_for_id):
    return [(cid, label, impact, fn_for_id(cid, fn), hint) for cid, label, impact, fn, hint in original]


@pytest.fixture
def patch_components():
    """COMPONENTS の ping を差し替える（id -> ping。未指定は成功）。TTL キャッシュもリセット。"""
    def _patch(pings: dict | None = None):
        pings = pings or {}
        health.COMPONENTS = _swap(_ORIGINAL_COMPONENTS, lambda cid, _fn: pings.get(cid, _ok))
        health._cache = {"at": 0.0, "data": None}

    yield _patch
    health.COMPONENTS = _ORIGINAL_COMPONENTS
    health._cache = {"at": 0.0, "data": None}


def _failing(*ids):
    return {i: _fail for i in ids}


@pytest.fixture
def patch_ai():
    """_AI_COMPONENTS の check を差し替える（id -> check(settings, system_settings)。未指定は成功）。"""
    def _patch(checks: dict):
        def _noop(_settings, _system_settings=None):
            return None
        health._AI_COMPONENTS = _swap(_ORIGINAL_AI_COMPONENTS, lambda cid, _fn: checks.get(cid, _noop))
        health._ai_cache = {}

    yield _patch
    health._AI_COMPONENTS = _ORIGINAL_AI_COMPONENTS
    health._ai_cache = {}


@pytest.fixture
def patch_search(monkeypatch):
    """_SEARCH_COMPONENTS の probe を差し替える（全行へ同じ probe・対象 world を固定）。"""
    def _patch(probe=None, world="test", components=None):
        if components is not None:
            health._SEARCH_COMPONENTS = components
        else:
            health._SEARCH_COMPONENTS = _swap(_ORIGINAL_SEARCH_COMPONENTS, lambda cid, _fn: probe)
        monkeypatch.setattr(health, "_search_probe_world", lambda: world)
        health._search_cache = {}

    yield _patch
    health._SEARCH_COMPONENTS = _ORIGINAL_SEARCH_COMPONENTS
    health._search_cache = {}


# ===== snapshot / summary =====

@pytest.mark.parametrize("failing, status", [
    pytest.param((), "ok", id="all-success"),
    pytest.param(("elasticsearch",), "degraded", id="elasticsearch-degraded"),
    pytest.param(("postgres",), "down", id="postgres-down"),
])
def test_snapshot_status_and_failed_components(patch_components, failing, status):
    patch_components(_failing(*failing))
    s = health.snapshot(force=True)
    assert s["status"] == status
    failed = {c["id"]: c for c in s["components"] if not c["ok"]}
    assert set(failed) == set(failing)
    for c in failed.values():
        assert c["hint"]
    for c in s["components"]:
        if c["ok"]:
            assert "hint" not in c


def test_cache_ttl_and_force_refresh(patch_components):
    patch_components()
    s1 = health.snapshot(force=True)
    assert health.snapshot(force=False) is s1, "TTL 内は force=False で同一（キャッシュ）結果を返す"

    # COMPONENTS の変更だけではキャッシュは無効化されない（キャッシュをリセットしないよう直接差し替える）
    health.COMPONENTS = _swap(_ORIGINAL_COMPONENTS, lambda cid, _fn: _fail if cid == "postgres" else _ok)
    assert health.snapshot(force=False) is s1

    s4 = health.snapshot(force=True)
    assert s4 is not s1
    assert s4["status"] == "down"


def test_summary_returns_only_status_and_checked_at(patch_components):
    patch_components()
    s = health.summary(force=True)
    assert set(s.keys()) == {"status", "checked_at"}
    assert s["status"] == "ok"
    assert isinstance(s["checked_at"], str)


def test_detail_does_not_leak_raw_exception_text(patch_components):
    # detail には `_classify()` の短い分類だけを入れ、DSN 等の生の例外文字列を出さない。
    def _fail_with_dsn():
        raise RuntimeError("postgresql://user:secretpw@host/db connection failed")

    patch_components({"postgres": _fail_with_dsn})
    pg = next(c for c in health.snapshot(force=True)["components"] if c["id"] == "postgres")
    assert pg["ok"] is False
    assert "secretpw" not in pg["detail"]
    assert "エラー" in pg["detail"]
    assert pg["hint"]


# ===== ai_snapshot =====

def test_ai_components_cover_only_supported_providers():
    assert [c[0] for c in health._AI_COMPONENTS] == ["answer_ai", "codex", "embed"]
    assert [c[0] for c in health.COMPONENTS] == ["postgres", "neo4j", "elasticsearch"]


def test_ai_snapshot_passes_per_user_settings_to_each_check(patch_ai):
    received = {}

    def _record(name):
        def _check(settings, system_settings=None):
            received[name] = settings
        return _check

    patch_ai({c[0]: _record(c[0]) for c in health._AI_COMPONENTS})
    sentinel = {"openai_api_key": "sk-test-sentinel"}
    rows = health.ai_snapshot("admin", sentinel, force=True)
    assert all(c["ok"] for c in rows)
    for name, settings in received.items():
        assert settings is sentinel, f"{name} に渡された settings が呼出元と別物になっている"


def test_ai_snapshot_failure_recorded_with_hint_but_no_secret_leak(patch_ai):
    def _fail_key(settings, system_settings=None):
        raise RuntimeError(f"401 unauthorized for key={settings.get('openai_api_key')}")

    patch_ai({"answer_ai": _fail_key})
    rows = health.ai_snapshot("admin", {"openai_api_key": "sk-should-not-leak"}, force=True)
    row = next(c for c in rows if c["id"] == "answer_ai")
    assert row["ok"] is False
    assert row["hint"]
    assert "sk-should-not-leak" not in row["detail"], "detail にキー値が漏れている"


def _ai_failure_row(patch_ai, caplog, exc_message, comp_id="answer_ai"):
    def _fail_msg(settings, system_settings=None):
        raise RuntimeError(exc_message)

    patch_ai({comp_id: _fail_msg})
    with caplog.at_level("WARNING", logger="sherpa.health"):
        rows = health.ai_snapshot("admin", {}, force=True)
    return (next(c for c in rows if c["id"] == comp_id),
            " ".join(r.getMessage() for r in caplog.records))


def test_ai_snapshot_redacts_unexpected_exception_with_url_userinfo_query_and_fragment(patch_ai, caplog):
    # `_safe_detail` を経由しない経路の例外でも、detail とログの両方で URL の userinfo/query/fragment を伏せる。
    row, logged = _ai_failure_row(
        patch_ai, caplog,
        "connect to https://user:s3cr3t@internal-gw.example.com:8443/path?token=leak-me#frag failed")
    assert row["ok"] is False
    for leaked in ("s3cr3t", "user:", "token=leak-me", "leak-me", "frag"):
        assert leaked not in row["detail"], f"{leaked!r} が detail に漏れている: {row['detail']!r}"
        assert leaked not in logged, f"{leaked!r} がログに漏れている: {logged!r}"
    assert "internal-gw.example.com:8443" in row["detail"]   # host[:port] は残る


@pytest.mark.parametrize("dsn, leaked_fragments", [
    ("postgresql://admin:db-secret@db.internal/app", ("admin", "db-secret", "app")),
    ("redis://user:pass@cache.internal:6379/0", ("user", "pass", "6379")),
    ("bolt://neo4j:s3cr3t@graph.internal:7687", ("neo4j", "s3cr3t", "7687")),
])
def test_ai_snapshot_redacts_dsn_style_exceptions_in_detail_and_log(patch_ai, caplog, dsn, leaked_fragments):
    # http/https 以外の scheme は host 縮約せず丸ごと [URL] にする。
    row, logged = _ai_failure_row(patch_ai, caplog, f"connect failed: {dsn} timeout")
    assert row["ok"] is False
    for leaked in leaked_fragments:
        assert leaked not in row["detail"], f"{leaked!r} が detail に漏れている: {row['detail']!r}"
        assert leaked not in logged, f"{leaked!r} がログに漏れている: {logged!r}"
    assert row["detail"] == "connect failed: [URL] timeout"


def test_ai_snapshot_shows_detailed_reason_instead_of_generic_classification(patch_ai):
    # AI 各行は `_classify()` の汎用分類ではなく、秘密を含まない具体的な理由文字列をそのまま表示する。
    reason = ("OpenAI 接続先の設定が未確定のため停止しています"
              "（env の設定を修正して再起動してください）: kind が不正です")

    def _fail_reason(settings, system_settings=None):
        raise RuntimeError(reason)

    patch_ai({"answer_ai": _fail_reason})
    row = next(c for c in health.ai_snapshot("admin", {}, force=True) if c["id"] == "answer_ai")
    assert row["ok"] is False
    assert row["detail"] == reason
    assert "エラー（RuntimeError）" not in row["detail"]


def test_ai_snapshot_caches_per_uid_and_force_bypasses(patch_ai):
    calls = {"n": 0}

    def _count(_settings, _system_settings=None):
        calls["n"] += 1

    patch_ai({"answer_ai": _count})
    health.ai_snapshot("admin", {}, force=True)
    assert calls["n"] == 1
    health.ai_snapshot("admin", {}, force=False)         # キャッシュ内
    assert calls["n"] == 1
    health.ai_snapshot("other-admin", {}, force=False)   # 別 uid は独立
    assert calls["n"] == 2
    health.ai_snapshot("admin", {}, force=True)          # force は常に再実行
    assert calls["n"] == 3


def test_ai_snapshot_runs_probes_in_parallel_not_sequentially(patch_ai):
    def _slow(_settings, _system_settings=None):
        time.sleep(0.3)

    patch_ai({c[0]: _slow for c in health._AI_COMPONENTS})
    t0 = time.monotonic()
    rows = health.ai_snapshot("admin", {}, force=True)
    elapsed = time.monotonic() - t0
    assert all(c["ok"] for c in rows)
    assert elapsed < 1.0, f"並列実行されていない疑い（{elapsed:.2f}s）"


def test_ai_snapshot_marks_probe_exceeding_deadline_as_timeout(patch_ai, monkeypatch):
    release = threading.Event()

    def _hang(_settings, _system_settings=None):
        release.wait(timeout=5)

    patch_ai({"answer_ai": _hang})
    monkeypatch.setattr(health, "_AI_DEADLINE", 0.2)
    try:
        t0 = time.monotonic()
        rows = health.ai_snapshot("admin", {}, force=True)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"deadline 超過後もブロックしている（{elapsed:.2f}s）"
        row = next(c for c in rows if c["id"] == "answer_ai")
        assert row["ok"] is False
        assert "タイムアウト" in row["detail"]
    finally:
        release.set()


# ===== 用途別の軽い確認（回答の AI・埋め込み・Codex） =====

_OPENAI = {"codex_model_provider": "openai"}


def test_ai_check_codex_uses_key_auth_on_azure_endpoint(monkeypatch):
    # Azure/互換接続先は env_key 認証＝`codex login status`（subprocess）を呼ばない。
    import subprocess

    monkeypatch.setattr(health.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("sherpa.llm.openai_endpoint_kind", lambda s=None: "azure")

    def _no_subprocess(*a, **kw):
        raise AssertionError("codex login status を呼んではいけない（env_key 認証構成）")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    monkeypatch.setattr(health.keys, "resolve_api_key",
                        lambda provider, settings, system_settings=None, **kw: "sk-test")
    health._ai_check_codex(_OPENAI, {})

    monkeypatch.setattr(health.keys, "resolve_api_key",
                        lambda provider, settings, system_settings=None, **kw: None)
    with pytest.raises(RuntimeError, match=keys.NO_CENTRAL_KEY_MESSAGE[:12]):
        health._ai_check_codex(_OPENAI, {})


def test_ai_check_codex_direct_openai_still_uses_login_status(monkeypatch):
    import subprocess

    monkeypatch.setattr(health.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("sherpa.llm.openai_endpoint_kind", lambda s=None: "openai")
    calls = []

    class _R:
        returncode = 0
        stdout = "Logged in"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (calls.append(a), _R())[1])
    health._ai_check_codex(_OPENAI, {})
    assert calls, "直結構成では login status を確認する"


# ===== ES/グラフ 実クエリ検索プローブ =====

def test_search_probe_no_world():
    assert health._search_probe_es(None) == health._NO_WORLD_DETAIL
    assert health._search_probe_graph(None) == health._NO_WORLD_DETAIL


@pytest.mark.parametrize("response, expected", [
    ({"hits": {"hits": [{"_id": "1"}]}}, "ヒットあり"),
    ({"hits": {"hits": []}}, "索引が空です"),
])
def test_search_probe_es_hit_and_empty(monkeypatch, response, expected):
    monkeypatch.setattr("sherpa.es_index._req", lambda *a, **k: response)
    assert health._search_probe_es("test") == expected


def test_search_probe_es_missing_index_404_reports_empty_not_failure(monkeypatch):
    def _raise_404(*a, **k):
        raise urllib.error.HTTPError("http://es/x/_search", 404, "not found", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr("sherpa.es_index._req", _raise_404)
    assert health._search_probe_es("test") == "索引が空です（未取り込み）"


def test_search_probe_es_connection_failure_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("sherpa.es_index._req", _boom)
    with pytest.raises(RuntimeError):
        health._search_probe_es("test")


class _FakeGraphDriver:
    """neo4j.GraphDatabase.driver の戻り値の最小の偽物（session().run().single() が row を返す）。"""

    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def session(self):
        row = self._row

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def run(self, query, **params):
                class _Result:
                    def single(self):
                        return row
                return _Result()

        return _Session()


def _patch_neo4j(monkeypatch, driver):
    import neo4j

    monkeypatch.setattr("sherpa.ingest.world_neo4j._env",
                        lambda: {"uri": "bolt://localhost:7687", "user": "neo4j", "pw": "x"})
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", driver)


@pytest.mark.parametrize("row, expected", [
    ({"n": "x"}, "ヒットあり"),
    (None, "該当データが空です（未取り込み）"),
])
def test_search_probe_graph_hit_and_empty(monkeypatch, row, expected):
    _patch_neo4j(monkeypatch, lambda *a, **k: _FakeGraphDriver(row))
    assert health._search_probe_graph("test") == expected


def test_search_probe_graph_connection_failure_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    _patch_neo4j(monkeypatch, _boom)
    with pytest.raises(RuntimeError):
        health._search_probe_graph("test")


# ===== search_snapshot =====

def test_search_snapshot_no_world_shows_no_target_and_ok(monkeypatch):
    monkeypatch.setattr("sherpa.store.list_worlds_db", lambda: [])
    health._search_cache = {}
    rows = health.search_snapshot("admin", force=True)
    assert len(rows) == 2
    for row in rows:
        assert row["ok"] is True
        assert row["detail"] == health._NO_WORLD_DETAIL
        assert "hint" not in row


def test_search_snapshot_passes_same_world_to_both_probes(patch_search):
    seen = []

    def _probe(world_id):
        seen.append(world_id)
        return "ヒットあり"

    patch_search(_probe, world="world-x")
    health.search_snapshot("admin", force=True)
    assert seen == ["world-x", "world-x"]


def test_search_snapshot_caches_per_uid_and_force_bypasses(patch_search):
    calls = {"n": 0}

    def _probe(_world_id):
        calls["n"] += 1
        return "ヒットあり"

    patch_search(_probe)
    health.search_snapshot("admin", force=True)
    assert calls["n"] == 2   # es_search + graph_search
    health.search_snapshot("admin", force=False)
    assert calls["n"] == 2
    health.search_snapshot("other-admin", force=False)
    assert calls["n"] == 4
    health.search_snapshot("admin", force=True)
    assert calls["n"] == 6


def test_search_snapshot_failure_recorded_with_hint(patch_search):
    def _fail_probe(_world_id):
        raise RuntimeError("boom")

    patch_search(_fail_probe)
    rows = health.search_snapshot("admin", force=True)
    assert all(not r["ok"] and r["hint"] for r in rows)


def test_search_snapshot_registry_failure_reported_as_failure_not_no_target(monkeypatch):
    # レジストリ読取失敗は「登録なし」に丸めず、両行 ok=False＋分類 detail（生の DSN は出さない）。
    def _boom():
        raise RuntimeError("postgresql://user:secretpw@host/db connection failed")

    monkeypatch.setattr("sherpa.store.list_worlds_db", _boom)
    health._search_cache = {}
    rows = health.search_snapshot("admin", force=True)
    assert len(rows) == 2
    for row in rows:
        assert row["ok"] is False
        assert row["detail"] != health._NO_WORLD_DETAIL
        assert "エラー" in row["detail"]
        assert "secretpw" not in row["detail"]
        assert row["hint"]


def test_search_snapshot_runs_probes_in_parallel_not_sequentially(patch_search):
    def _slow(_world_id):
        time.sleep(0.3)
        return "ヒットあり"

    patch_search(_slow)
    t0 = time.monotonic()
    rows = health.search_snapshot("admin", force=True)
    elapsed = time.monotonic() - t0
    assert all(r["ok"] for r in rows)
    assert elapsed < 0.6, f"並列実行されていない疑い（{elapsed:.2f}s）"


def test_search_snapshot_marks_probe_exceeding_deadline_as_timeout(patch_search, monkeypatch):
    release = threading.Event()

    def _hang(_world_id):
        release.wait(timeout=5)
        return "ヒットあり"

    patch_search(components=[
        ("es_search", "全文検索（1 件読めるか）", "none", _hang, "h"),
        ("graph_search", "つながりの検索（1 件読めるか）", "none", lambda _w: "ヒットあり", "h"),
    ])
    monkeypatch.setattr(health, "_SEARCH_DEADLINE", 0.2)
    try:
        t0 = time.monotonic()
        rows = health.search_snapshot("admin", force=True)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"deadline 超過後もブロックしている（{elapsed:.2f}s）"
        es_row = next(r for r in rows if r["id"] == "es_search")
        assert es_row["ok"] is False
        assert "タイムアウト" in es_row["detail"]
    finally:
        release.set()


# ===== 用途別の接続先（他の用途の接続先を見に行かない） =====

class _FakeOllamaTagsResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _tags_payload(*names: str) -> bytes:
    return json.dumps({"models": [{"name": n} for n in names]}).encode()


_CATALOG = {"model_catalog": {
    "ollama": {"subsearch": {"allowed": ["chat-sample:8b"], "default": "chat-sample:8b"},
               "embed": {"allowed": ["embed-sample"], "default": "embed-sample"}},
    "codex": {"codex": {"allowed": ["code-sample:20b"], "default": "code-sample:20b"}},
}}
_OLLAMA_ALL = {**_CATALOG, "cloud_provider": "ollama", "research_default_provider": "ollama",
               "codex_default_model_provider": "ollama", "embed_provider": "ollama",
               "ollama_url": "http://127.0.0.1:11434", "ollama_embed_url": "http://127.0.0.1:11435"}


@pytest.fixture
def ollama_servers(monkeypatch):
    """Ollama の外部境界（urlopen_no_redirect）だけを差し替える。`served` の port -> 取得済みモデル名（None は落ちている）。"""
    served = {"11434": ("chat-sample:8b", "code-sample:20b"), "11435": ("embed-sample:latest",)}
    hits = []

    def _open(url, timeout=None):
        hits.append(url)
        names = served[url.split(":")[2].split("/")[0]]
        if names is None:
            raise ConnectionRefusedError("down")
        return _FakeOllamaTagsResponse(_tags_payload(*names))

    monkeypatch.setattr(llm, "urlopen_no_redirect", _open)
    monkeypatch.setattr(health.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    monkeypatch.setattr(health, "_ai_cache", {})
    return served, hits


def _rows(sys_s, settings=None):
    orig = store.get_system_settings
    store.get_system_settings = lambda *a, **k: sys_s
    try:
        return {r["id"]: r for r in health.ai_snapshot("admin", settings or {}, force=True)}
    finally:
        store.get_system_settings = orig


def test_only_embed_row_fails_when_only_the_embed_server_is_down(ollama_servers):
    served, _hits = ollama_servers
    served["11435"] = None
    rows = _rows(_OLLAMA_ALL, {"codex_model_provider": "ollama"})
    assert rows["answer_ai"]["ok"] is True
    assert rows["codex"]["ok"] is True
    assert rows["embed"]["ok"] is False and "埋め込み用の Ollama に届きません" in rows["embed"]["detail"]


def test_embed_row_reports_missing_embed_model_on_the_embed_server(ollama_servers):
    served, _hits = ollama_servers
    served["11435"] = ("other-sample:latest",)
    rows = _rows(_OLLAMA_ALL, {"codex_model_provider": "ollama"})
    assert rows["embed"]["ok"] is False and "ollama pull embed-sample" in rows["embed"]["detail"]
    assert rows["answer_ai"]["ok"] is True


def test_chat_on_openai_does_not_look_at_the_central_ollama_when_only_embed_uses_ollama(ollama_servers, monkeypatch):
    import subprocess
    _served, hits = ollama_servers
    sys_s = {**_CATALOG, "cloud_provider": "openai", "research_default_provider": "openai", "openai_api_key": "sk-sample-key",
             "codex_default_model_provider": "openai", "embed_provider": "ollama",
             "ollama_url": "http://127.0.0.1:11434", "ollama_embed_url": "http://127.0.0.1:11435"}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    rows = _rows(sys_s)
    assert all(r["ok"] for r in rows.values()), rows
    assert hits and all(":11435/" in u for u in hits), "チャット用（中央）の Ollama の接続先を見に行っている"


def test_light_check_neither_sends_to_ai_nor_writes(ollama_servers, monkeypatch):
    from sherpa import agentic_search, es_index
    from sherpa.ingest import graph_extract

    def _forbidden(*a, **k):
        raise AssertionError("軽い確認が送信・書き込みをしている")

    monkeypatch.setattr(graph_extract, "_probe", _forbidden)
    monkeypatch.setattr(graph_extract, "complete_json", _forbidden)
    monkeypatch.setattr(agentic_search, "_post", _forbidden)
    monkeypatch.setattr(es_index, "_req", _forbidden)
    monkeypatch.setattr(llm, "post_json", _forbidden)
    monkeypatch.setattr(embeddings, "embed_with_failure", _forbidden)
    rows = _rows(_OLLAMA_ALL, {"codex_model_provider": "ollama"})
    assert all(r["ok"] for r in rows.values()), rows


def test_embed_row_not_in_use_when_disabled(ollama_servers, monkeypatch):
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    rows = _rows(_OLLAMA_ALL, {"codex_model_provider": "ollama"})
    assert rows["embed"]["ok"] is True and "使っていません" in rows["embed"]["detail"]


def test_answer_row_openai_needs_a_real_key(monkeypatch):
    base = {"cloud_provider": "openai", "research_default_provider": "openai", "model_catalog": {
        "openai": {"subsearch": {"allowed": ["chat-sample"], "default": "chat-sample"}}}}
    with pytest.raises(RuntimeError, match=keys.NO_CENTRAL_KEY_MESSAGE[:12]):
        health._ai_check_answer({}, {**base, "openai_api_key": "sk-REPLACE_ME"})
    health._ai_check_answer({}, {**base, "openai_api_key": "sk-sample-key"})


def test_ai_check_codex_ollama_requires_codex_model_pulled(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda _: "/usr/bin/codex")
    cat = {"model_catalog": {"codex": {"codex": {"allowed": ["gpt-oss:20b"], "default": "gpt-oss:20b"}}}}

    def _tags(names):
        return lambda *a, **k: _FakeOllamaTagsResponse(_tags_payload(*names))

    monkeypatch.setattr("sherpa.llm.urlopen_no_redirect", _tags(["nomic-embed-text:latest"]))
    with pytest.raises(RuntimeError, match="gpt-oss:20b"):
        health._ai_check_codex({"codex_model_provider": "ollama"}, cat)
    monkeypatch.setattr("sherpa.llm.urlopen_no_redirect", _tags(["gpt-oss:20b"]))
    health._ai_check_codex({"codex_model_provider": "ollama"}, cat)
