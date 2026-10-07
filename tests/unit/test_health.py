"""sherpa.health の単体テスト（外部サービス不要・偽 ping/check に差し替えて検証）。

COMPONENTS / _AI_COMPONENTS / _SEARCH_COMPONENTS を偽関数へ差し替え、snapshot・ai_snapshot・
search_snapshot の集約・TTL/per-uid キャッシュ・並列実行と deadline・秘密/URL の伏せ字を検証する。
"""
from __future__ import annotations

import io
import json
import logging
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
    # impact=none は status に影響しないが components には ok=False と hint が残る
    pytest.param(("codex", "openai", "ollama"), "ok", id="none-impact-stay-ok"),
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
    assert [c[0] for c in health._AI_COMPONENTS] == ["openai", "ollama", "codex"]
    assert all(c[0] not in ("gemini", "bedrock") for c in health.COMPONENTS)


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

    patch_ai({"openai": _fail_key})
    rows = health.ai_snapshot("admin", {"openai_api_key": "sk-should-not-leak"}, force=True)
    row = next(c for c in rows if c["id"] == "openai")
    assert row["ok"] is False
    assert row["hint"]
    assert "sk-should-not-leak" not in row["detail"], "detail にキー値が漏れている"


def _ai_failure_row(patch_ai, caplog, exc_message, comp_id="ollama"):
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

    patch_ai({"openai": _fail_reason})
    row = next(c for c in health.ai_snapshot("admin", {}, force=True) if c["id"] == "openai")
    assert row["ok"] is False
    assert row["detail"] == reason
    assert "エラー（RuntimeError）" not in row["detail"]


def test_ai_snapshot_caches_per_uid_and_force_bypasses(patch_ai):
    calls = {"n": 0}

    def _count(_settings, _system_settings=None):
        calls["n"] += 1

    patch_ai({"openai": _count})
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

    patch_ai({"openai": _hang})
    monkeypatch.setattr(health, "_AI_DEADLINE", 0.2)
    try:
        t0 = time.monotonic()
        rows = health.ai_snapshot("admin", {}, force=True)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"deadline 超過後もブロックしている（{elapsed:.2f}s）"
        row = next(c for c in rows if c["id"] == "openai")
        assert row["ok"] is False
        assert "タイムアウト" in row["detail"]
    finally:
        release.set()


# ===== _ai_check_openai / _ping_openai / _ai_check_codex =====

def test_ai_check_openai_passes_short_explicit_timeout_to_probe(monkeypatch):
    from sherpa.ingest import graph_extract

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    captured = {}

    def fake_probe(cfg, timeout=None):
        captured["timeout"] = timeout
        return True, ""

    monkeypatch.setattr(graph_extract, "_probe", fake_probe)
    health._ai_check_openai({"openai_api_key": "sk-test"})
    assert captured["timeout"] == health._AI_TIMEOUT


def test_ai_check_openai_strict_rejects_invalid_cloud_provider_without_probing(monkeypatch):
    # 不正な cloud_provider のまま実 API probe（課金）を送らない。
    from sherpa.ingest import graph_extract

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True, "cloud_provider": "not-a-real-provider"})
    probe_calls = []
    monkeypatch.setattr(graph_extract, "_probe", lambda cfg, timeout=None: (probe_calls.append(cfg) or (True, "")))
    with pytest.raises(keys.InvalidCloudProviderConfigError, match="not-a-real-provider"):
        health._ai_check_openai({"openai_api_key": "sk-test"})
    assert probe_calls == []


def test_ai_check_openai_does_not_leak_reflected_url_into_health_log(monkeypatch, caplog):
    from sherpa.ingest import graph_extract

    sys_s = {"openai_endpoint_kind": "azure",
             "openai_base_url": "https://myres.openai.azure.com/openai/deployments/my-secret-deploy",
             "openai_api_key": "sk-test"}
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: sys_s)
    fp = io.BytesIO(json.dumps({"error": {"message": (
        "bad request to https://myres.openai.azure.com/openai/deployments/my-secret-deploy/"
        "chat/completions?api-version=2024-01-01")}}).encode("utf-8"))
    exc = urllib.error.HTTPError("https://myres.openai.azure.com/openai/v1/chat/completions",
                                 400, "error", {}, fp)
    monkeypatch.setattr(graph_extract, "complete_json", lambda *a, **k: (_ for _ in ()).throw(exc))

    with caplog.at_level("WARNING", logger="sherpa.health"):
        rows = health.ai_snapshot("admin", {}, force=True)
    row = next(c for c in rows if c["id"] == "openai")
    assert row["ok"] is False
    assert "my-secret-deploy" not in row["detail"]
    logged_text = " ".join(r.getMessage() for r in caplog.records)
    assert "my-secret-deploy" not in logged_text
    assert "api-version" not in logged_text


def _set_central_openai_key(monkeypatch, value):
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"openai_api_key": value} if value is not None else {})


def test_ping_openai_rejects_placeholder_exact_match_not_substring(monkeypatch):
    # プレースホルダ判定は完全一致ベース（"REPLACE_ME" を部分文字列に含むだけの実キーは弾かない）。
    _set_central_openai_key(monkeypatch, "sk-REPLACE_ME")
    with pytest.raises(RuntimeError, match="設定してください"):
        health._ping_openai()

    _set_central_openai_key(monkeypatch, None)
    with pytest.raises(RuntimeError, match="設定してください"):
        health._ping_openai()

    _set_central_openai_key(monkeypatch, "sk-proj-REPLACE_ME_IS_NOT_MY_WHOLE_KEY-abc123")
    health._ping_openai()   # 例外なし＝未設定扱いされていない


def test_ai_check_openai_rejects_placeholder_without_calling_probe(monkeypatch):
    from sherpa.ingest import graph_extract

    called = {"n": 0}

    def _should_not_be_called(cfg, timeout=None):
        called["n"] += 1
        return True, ""

    monkeypatch.setattr(graph_extract, "_probe", _should_not_be_called)
    with pytest.raises(RuntimeError, match="設定してください"):
        health._ai_check_openai({"openai_api_key": "sk-REPLACE_ME"})
    assert called["n"] == 0, "プレースホルダなのに _probe（実API呼び出し）まで進んでいる"


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
    health._ai_check_codex({}, {})

    monkeypatch.setattr(health.keys, "resolve_api_key",
                        lambda provider, settings, system_settings=None, **kw: None)
    with pytest.raises(RuntimeError, match=keys.NO_CENTRAL_KEY_MESSAGE[:12]):
        health._ai_check_codex({}, {})


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
    health._ai_check_codex({}, {})
    assert calls, "直結構成では login status を確認する"


def test_ping_openai_hint_points_to_admin_not_env():
    hint = next(c[4] for c in health.COMPONENTS if c[0] == "openai")
    assert ".env" not in hint
    assert keys.NO_CENTRAL_KEY_MESSAGE in hint


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
        ("es_search", "ES検索（実クエリ）", "none", _hang, "h"),
        ("graph_search", "グラフ検索（実クエリ）", "none", lambda _w: "ヒットあり", "h"),
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


# ===== 対象外（_NotApplicable）: 未設定のプロバイダを WARNING にしない =====

@pytest.mark.parametrize("settings, ok, warns", [
    pytest.param({}, True, False, id="ollama-unconfigured-not-applicable"),
    pytest.param({"ollama_url": "http://127.0.0.1:11434"}, False, True, id="ollama-configured-still-warns"),
])
def test_ollama_connection_refused(monkeypatch, caplog, settings, ok, warns):
    monkeypatch.setattr(store, "get_system_settings", lambda: settings)

    def _boom(*a, **kw):
        raise OSError("Connection refused")

    monkeypatch.setattr(llm, "urlopen_no_redirect", _boom)
    with caplog.at_level(logging.DEBUG, logger="sherpa.health"):
        out = health._check_one("ollama", "o", "none", health._ping_ollama, "hint")
    assert out["ok"] is ok
    if ok:
        assert "対象外" in out["detail"]
    assert bool([r for r in caplog.records if r.levelno >= logging.WARNING]) is warns


_AZURE_ONLY = {"cloud_provider": "openai", "research_default_provider": "openai", "embed_provider": "auto"}


def test_ollama_not_in_use_skips_ping_and_logs_nothing(monkeypatch, caplog):
    monkeypatch.setattr(store, "get_system_settings", lambda: _AZURE_ONLY)
    monkeypatch.setattr(health, "_active_user_codex_rows", lambda: [])
    monkeypatch.setattr("sherpa.ingest.arms.enabled_arm_names", lambda: ["ooxml"])

    def _must_not_call(*a, **kw):
        raise AssertionError("Ollama を問い合わせてはいけない")

    monkeypatch.setattr(llm, "urlopen_no_redirect", _must_not_call)
    with caplog.at_level(logging.DEBUG, logger="sherpa.health"):
        out = health._check_one("ollama", "o", "none", health._ping_ollama, "hint")
    assert out["ok"] is True and "使っていません" in out["detail"]
    assert not caplog.records


@pytest.mark.parametrize("sys_s, rows", [
    pytest.param({**_AZURE_ONLY, "embed_provider": "ollama"}, [], id="embed-ollama"),
    pytest.param(_AZURE_ONLY, [{"agent": "codex", "codex_model_provider": "ollama"}], id="codex-oss-user"),
    pytest.param({**_AZURE_ONLY, "research_default_provider": "ollama", "ollama_url": "http://h:11434"}, [],
                 id="simple-ollama"),
    pytest.param({**_AZURE_ONLY, "research_default_provider": "ollama"}, [], id="simple-ollama-without-url"),
])
def test_ollama_in_use_when_any_purpose_uses_it(monkeypatch, sys_s, rows):
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    monkeypatch.setattr("sherpa.ingest.arms.enabled_arm_names", lambda: ["ooxml"])
    assert health.ollama_in_use(sys_s, rows) is True


# ===== 埋め込みモデル未取得の検出 =====

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


_OLLAMA_EMBED = {"provider": "ollama", "url": "http://127.0.0.1:11434", "model": "nomic-embed-text", "dim": 768}


@pytest.mark.parametrize("tags, embed_cfg, ok", [
    pytest.param(("qwen2.5:latest",), _OLLAMA_EMBED, False, id="embed-model-missing"),
    pytest.param(("nomic-embed-text:latest",), _OLLAMA_EMBED, True, id="embed-model-present-with-latest"),
    pytest.param(("qwen2.5:latest",), None, True, id="embed-not-ollama-unaffected"),
])
def test_ollama_ping_embed_model_check(monkeypatch, tags, embed_cfg, ok):
    monkeypatch.setattr(store, "get_system_settings", lambda: {"ollama_url": "http://127.0.0.1:11434"})
    monkeypatch.setattr(llm, "urlopen_no_redirect",
                        lambda url, timeout=None: _FakeOllamaTagsResponse(_tags_payload(*tags)))
    monkeypatch.setattr(embeddings, "cfg", lambda *a, **k: embed_cfg)
    out = health._check_one("ollama", "o", "none", health._ping_ollama, "hint")
    assert out["ok"] is ok
    if not ok:
        assert "nomic-embed-text" in out["detail"]
        assert "ollama pull nomic-embed-text" in out["detail"]


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
