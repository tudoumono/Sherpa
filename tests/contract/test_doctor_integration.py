"""`scripts/doctor_checks.py::run_all()` の統合契約テスト。

`tests/unit/test_doctor_checks.py` が各検査を個別に確かめるのに対し、ここでは `run_all()` を実モジュール
のまま通し、外部 I/O 境界（psycopg・neo4j・ES の urllib・Ollama の `llm.urlopen_no_redirect`・
Codex CLI）だけを差し替える。フェイク Postgres は SELECT 単文以外を即座に拒否して「読み取り専用」契約を
確かめ、`doctor.sh` は実サブプロセスとして起動して配線を確かめる。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.doctor_checks as doctor_checks
from sherpa import agent_constructs, health
from sherpa.ingest import graph_extract

ROOT = Path(__file__).resolve().parents[2]
DOCTOR_SH = ROOT / "scripts" / "doctor.sh"
_SEEDED = {"key": "openai_endpoint_seed_version", "value": 1}


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakePgConn:
    """SELECT 単文以外（DML・DDL・`;` 連結の複文）は即座に AssertionError にし、実行 SQL を記録する。"""

    def __init__(self, settings_rows, user_rows, fail_on_tables=(), executed=None):
        self._settings_rows = settings_rows
        self._user_rows = user_rows
        self._fail_on_tables = fail_on_tables
        self.executed_sql = executed if executed is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed_sql.append(sql)
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        if len(statements) != 1 or not statements[0].lower().startswith("select"):
            raise AssertionError(f"読み取り専用契約違反: SELECT 単文以外が実行されました: {sql!r}")
        low = statements[0].lower()
        for table in self._fail_on_tables:
            if table in low:
                raise RuntimeError(f"permission denied for table {table}")
        if "system_settings" in low:
            return _FakeCursor(self._settings_rows)
        if "user_settings" in low:
            return _FakeCursor(self._user_rows)
        return _FakeCursor([])


class _FakeNeo4jDriver:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def verify_connectivity(self):
        return None


class _FakeHttpResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


_ES_OK_ROOT = json.dumps({"version": {"number": "8.19.20"}}).encode()
_ES_OK_PLUGINS = json.dumps([{"component": "analysis-kuromoji"}]).encode()


def _env(monkeypatch, *, settings=(), users=(), fail_on_tables=(), es_root=_ES_OK_ROOT, codex=None,
         ollama_ok=True):
    """外部 I/O を一括で差し替える。戻り値は実行された SQL のリスト。
    codex: None=未導入 / "ok"=導入済み(version 成功) / "fail"=導入済みだが version が失敗。"""
    executed: list[str] = []
    monkeypatch.setattr("psycopg.connect", lambda *a, **k: _FakePgConn(
        list(settings), list(users), fail_on_tables, executed))
    monkeypatch.setattr("neo4j.GraphDatabase.driver", lambda *a, **k: _FakeNeo4jDriver())
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=None: _FakeHttpResponse(
        _ES_OK_PLUGINS if "_cat/plugins" in url else es_root))
    if codex is None:
        monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: None)
    else:
        class _Proc:
            returncode = 0 if codex == "ok" else 1
            stdout = "codex-cli 0.144.1\n" if codex == "ok" else ""
            stderr = "" if codex == "ok" else "unexpected error"
        monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")
        monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _Proc())
    # 道具の導入状況は DB（OCR ワーカー）を引くため、この統合テストでは境界として空にする。
    monkeypatch.setattr("sherpa.required_tools.snapshot", lambda force=False: [])
    if ollama_ok:
        monkeypatch.setattr(doctor_checks, "_probe_ollama_usage", lambda url, model, s: (True, "ok"))
    return executed


def _agent_from_settings(monkeypatch, default):
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda s, **k: (s or {}).get("agent") or default)


def _no_send(monkeypatch):
    def f(*a, **k):
        raise AssertionError("実送信してはいけない構成で送信された")
    monkeypatch.setattr(graph_extract, "complete_json", f)
    monkeypatch.setattr(doctor_checks, "_run_raw_llm_probe", f)


def _run(probe_cloud=False):
    return {r.id: r for r in doctor_checks.run_all(probe_cloud=probe_cloud)}


def _kv(**kw):
    return [{"key": k, "value": v} for k, v in kw.items()]


def test_run_all_settings_read_failure_is_ng_not_skip(monkeypatch):
    _env(monkeypatch, fail_on_tables=("system_settings",))
    by_id = _run()
    assert by_id["postgres"].status == "ok"
    assert by_id["system_settings_read"].status == "ng"
    assert by_id["user_settings_read"].status == "ok"
    assert by_id["openai_endpoint"].status == "skip"
    assert by_id["selected_provider_key"].status == "skip"
    # sys_s 不明は fail-closed: codex/ollama は必須扱い＝未導入なら NG
    assert by_id["codex_cli"].status == "ng" and by_id["llm_ollama"].status == "ng"


def test_run_all_no_marker_azure_env_sends_nothing_and_is_ng(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x.openai.azure.com/openai/v1")
    monkeypatch.delenv("SHERPA_OPENAI_ENDPOINT_KIND", raising=False)
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    _no_send(monkeypatch)
    _env(monkeypatch, settings=_kv(cloud_provider="openai", openai_api_key="sk-azure-only-key-1234567890"))
    by_id = _run(probe_cloud=True)
    assert by_id["openai_endpoint"].status == "ok"   # env 候補自体は妥当（マーカー無し）
    assert by_id["llm_openai"].status == "ng" and "デプロイ名" in by_id["llm_openai"].detail


def test_run_all_db_endpoint_invalid_skips_every_cloud_send(monkeypatch):
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "codex")
    monkeypatch.setattr(agent_constructs, "codex_model_provider", lambda *a, **k: "openai")
    _no_send(monkeypatch)
    _env(monkeypatch, codex="ok", settings=_kv(
        cloud_provider="openai", openai_api_key="sk-real-key-1234567890",
        openai_endpoint_kind="bogus", openai_endpoint_seed_version=1))
    by_id = _run(probe_cloud=True)
    assert by_id["openai_endpoint"].status == "ng"
    assert [by_id[i].status for i in ("selected_provider_key", "llm_openai", "codex_auth")] == ["skip"] * 3


def test_run_all_agent_resolution_failure_is_reported_on_own_items(monkeypatch):
    def _boom(settings, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_constructs, "effective_agent", _boom)
    _env(monkeypatch, settings=_kv(cloud_provider="openai", openai_api_key="sk-real-key-1234567890",
                                   openai_endpoint_seed_version=1),
         users=[{"agent": "openai", "codex_model_provider": None, "ollama_url": None,
                 "search_helper": "", "has_openai_key": False}])
    by_id = _run(probe_cloud=True)
    for cid in ("selected_provider_key", "llm_openai", "codex_cli"):
        assert by_id[cid].status == "ng"
        assert by_id[cid].detail == doctor_checks._AGENT_RESOLUTION_FAILED_DETAIL


def test_run_all_personal_keys_neither_ng_nor_probed(monkeypatch):
    _agent_from_settings(monkeypatch, "openai")
    _no_send(monkeypatch)
    _env(monkeypatch, settings=_kv(cloud_provider="openai", personal_api_keys_allowed=True,
                                   openai_endpoint_seed_version=1),
         users=[{"agent": "openai", "codex_model_provider": None, "ollama_url": None, "search_helper": "",
                 "has_openai_key": True}])
    by_id = _run(probe_cloud=True)
    assert by_id["selected_provider_key"].status == "ok" and "1" in by_id["selected_provider_key"].detail
    assert by_id["llm_openai"].status == "skip" and "個人キー" in by_id["llm_openai"].detail
    assert not any(r.status == "ng" for r in by_id.values())


@pytest.mark.parametrize("msg", [
    "sk-abcdefgh1234567890ABCDEFGHIJK",
    "sk-ab\ncdefgh1234\t567890ABCDEFGHIJK",
    "sk-ab cdefgh1234-567890ABCDEFGHIJK",
], ids=["intact", "control-split", "space-split"])
def test_run_all_cloud_probe_failure_never_leaks_real_key(monkeypatch, msg):
    _agent_from_settings(monkeypatch, "openai")
    secret = "sk-abcdefgh1234567890ABCDEFGHIJK"
    _env(monkeypatch, settings=_kv(cloud_provider="openai", openai_api_key=secret, openai_endpoint_seed_version=1))

    def _boom(system, user, cfg, timeout=None):
        assert cfg["key"] == secret
        raise RuntimeError(f"invalid key: {msg}")
    monkeypatch.setattr(graph_extract, "complete_json", _boom)
    detail = _run(probe_cloud=True)["llm_openai"].detail
    assert detail == "接続に失敗しました: error（RuntimeError）"


def test_run_all_disabled_agent_is_an_independent_ng(monkeypatch):
    _agent_from_settings(monkeypatch, "openai")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: agent == "gemini")
    _env(monkeypatch, settings=_kv(cloud_provider="openai", openai_api_key="sk-real-key", openai_endpoint_seed_version=1),
         users=[{"agent": "gemini", "codex_model_provider": None, "ollama_url": None, "search_helper": ""}])
    by_id = _run()
    assert by_id["disabled_agent_configs"].status == "ng" and "1" in by_id["disabled_agent_configs"].detail
    assert by_id["selected_provider_key"].status == "ok"


@pytest.mark.parametrize("root", [b"null", b"42", json.dumps({"tagline": "x"}).encode()],
                         ids=["null", "number", "no-version"])
def test_run_all_es_malformed_response_is_ng_without_crashing(monkeypatch, root):
    _env(monkeypatch, settings=[_SEEDED], es_root=root)
    by_id = _run()
    assert by_id["elasticsearch"].status == "ng" and by_id["es_kuromoji"].status == "skip"


def test_run_all_codex_requirement_follows_current_configuration(monkeypatch):
    """Codex を使わない構成では version／ログイン失敗は SKIP。Codex(Ollama) のみなら認証確認自体をしない。"""
    _agent_from_settings(monkeypatch, "ollama")
    _env(monkeypatch, codex="fail", settings=_kv(cloud_provider="openai", openai_api_key="central-openai-key",
                                                  openai_endpoint_seed_version=1))
    monkeypatch.setattr(health, "_ai_check_codex", lambda s, sys_s: (_ for _ in ()).throw(RuntimeError("未ログイン")))
    by_id = _run()
    assert [by_id[i].status for i in ("codex_cli", "codex_version", "codex_auth")] == ["ok", "skip", "skip"]

    _agent_from_settings(monkeypatch, "openai")
    _env(monkeypatch, codex="ok", settings=_kv(cloud_provider="openai", openai_api_key="sk-real-key",
                                                openai_endpoint_seed_version=1),
         users=[{"agent": "codex", "codex_model_provider": "ollama", "ollama_url": None, "search_helper": ""}])

    def _never(*a, **k):
        raise AssertionError("Codex(Ollama) 専用構成では OpenAI 認証確認を呼んではいけない")
    monkeypatch.setattr(health, "_ai_check_codex", _never)
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", _never)
    by_id = _run()
    assert [by_id[i].status for i in ("codex_cli", "codex_version", "codex_auth")] == ["ok", "ok", "skip"]


def _ollama_unreachable(monkeypatch):
    def _unreachable(url, timeout=None):
        raise OSError("Connection refused")
    monkeypatch.setattr("sherpa.llm.urlopen_no_redirect", _unreachable)


def test_run_all_active_user_ollama_usage_makes_it_required(monkeypatch):
    _env(monkeypatch, ollama_ok=False,
         settings=_kv(cloud_provider="openai", openai_api_key="sk-real-key", openai_endpoint_seed_version=1),
         users=[{"agent": "openai", "codex_model_provider": None, "ollama_url": None, "search_helper": ""},
                {"agent": "codex", "codex_model_provider": "ollama", "ollama_url": None, "search_helper": ""}])
    _ollama_unreachable(monkeypatch)
    ollama = [r for r in _run().values() if r.id.startswith("llm_ollama")]
    assert ollama and all(r.status == "ng" for r in ollama)   # 簡易と Codex(Ollama) の用途がいずれも未接続


def test_run_all_stays_read_only_with_real_ollama_probe(monkeypatch):
    """全 SQL は SELECT 単文のみ・`get_system_settings()`（DDL を打ちうる高水準 API）は呼ばない
    （`llm` の許可判定は例外を握り潰すため、呼び出し回数の記録で確かめる）。"""
    import sherpa.store as sherpa_store
    calls: list = []

    def _spy(*a, **k):
        calls.append(1)
        raise AssertionError("get_system_settings() を呼んではいけない")
    monkeypatch.setattr(sherpa_store, "get_system_settings", _spy)
    _agent_from_settings(monkeypatch, "simple")
    executed = _env(monkeypatch, ollama_ok=False,
                    settings=_kv(cloud_provider="openai", openai_endpoint_seed_version=1))
    _ollama_unreachable(monkeypatch)
    assert _run()["llm_ollama"].status == "ng"
    assert calls == []
    low = [s.lower() for s in executed]
    assert any(s.strip() == "select 1" for s in low)
    assert any("system_settings" in s for s in low) and any("user_settings" in s for s in low)
    assert all(s.strip().startswith("select") for s in low), executed


def test_read_active_user_configs_sql_contract(monkeypatch):
    executed = _env(monkeypatch)
    doctor_checks._read_active_user_configs_readonly()
    assert len(executed) == 1
    sql = executed[0].lower()
    assert "join users" in sql and "status" in sql and "active" in sql   # 無効な利用者は除外
    assert "has_openai_key" in sql and "gemini" not in sql and "bedrock" not in sql
    assert "select us.openai_api_key," not in sql   # キーの値そのものは SELECT しない
    # 本番の truthy 判定（NULL／空文字以外は「あり」）と一致させる
    assert "us.openai_api_key is not null and us.openai_api_key <> ''" in sql
    assert "btrim(" not in sql and "= any(" not in sql


def test_run_all_does_not_crash_when_model_catalog_is_broken(monkeypatch):
    from sherpa import model_catalog

    def _boom(*a, **k):
        raise TypeError("壊れた設定")
    monkeypatch.setattr(model_catalog, "resolve_model", _boom)
    _env(monkeypatch, codex="ok", ollama_ok=False, settings=_kv(
        cloud_provider="openai", openai_endpoint_kind="azure", openai_base_url="https://x.openai.azure.com/openai/v1",
        openai_api_key="sk-real-key", openai_endpoint_seed_version=1))
    by_id = _run()   # 例外を投げずに完走する
    assert by_id["codex_auth"].status == "ng" and by_id["llm_ollama"].status == "ng"


def test_fake_pg_conn_rejects_non_select_statements():
    conn = _FakePgConn([], [])
    for sql in ("UPDATE system_settings SET value = '1'", "DELETE FROM user_settings", "CREATE TABLE x (id int)",
                "DROP TABLE user_settings", "INSERT INTO system_settings VALUES (1)",
                "SELECT 1; DELETE FROM user_settings"):
        with pytest.raises(AssertionError):
            conn.execute(sql)


# ---------------------------------------------------------------------------
# doctor.sh（bash エントリポイント）
# ---------------------------------------------------------------------------

def _sh_env(tmp_path, **extra):
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    return {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHON_BIN": sys.executable,
            "SHERPA_ENV_FILE": str(empty), **extra}


def _fake_runner(tmp_path, body):
    p = tmp_path / "fake_python_runner.sh"
    p.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8")
    p.chmod(0o755)
    return p


def _sh(env, timeout=10):
    return subprocess.run([str(DOCTOR_SH)], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=timeout)


def test_doctor_sh_runs_end_to_end_against_unreachable_hosts(tmp_path):
    env_file = tmp_path / "doctor_test.env"
    env_file.write_text("PGHOST=127.0.0.1\nPGPORT=1\nNEO4J_URI=bolt://127.0.0.1:1\nES_URL=http://127.0.0.1:1\n",
                        encoding="utf-8")
    proc = _sh(_sh_env(tmp_path, SHERPA_ENV_FILE=str(env_file)), timeout=30)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "PostgreSQL" in proc.stdout and "NG" in proc.stdout


def test_doctor_sh_passes_probe_cloud_env_to_child(tmp_path):
    runner = _fake_runner(tmp_path, 'echo "PROBE_CLOUD=${PROBE_CLOUD:-<unset>}"')
    for value, expected in ((None, "PROBE_CLOUD=<unset>"), ("1", "PROBE_CLOUD=1")):
        env = _sh_env(tmp_path, PYTHON_BIN=str(runner))
        if value is not None:
            env["PROBE_CLOUD"] = value
        proc = _sh(env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.strip() == expected


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_doctor_sh_errors_when_explicit_env_file_is_unusable(tmp_path, kind):
    target = tmp_path / "does-not-exist.env" if kind == "missing" else tmp_path
    proc = _sh({"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHON_BIN": sys.executable,
                "SHERPA_ENV_FILE": str(target)})
    assert proc.returncode == 2 and "SHERPA_ENV_FILE" in proc.stderr


def test_doctor_sh_proceeds_when_default_env_file_is_absent(tmp_path):
    runner = _fake_runner(tmp_path, 'echo "ran ok"')
    proc = _sh({"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHON_BIN": str(runner)})
    assert proc.returncode == 0 and proc.stdout.strip() == "ran ok"
