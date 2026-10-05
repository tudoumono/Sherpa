"""`scripts/collect_diagnostics.py`（`make diag`・解析用ログ回収バンドル）の単体テスト。

第一契約（機密を含めない）の実害を固定する。DB/ES/Neo4j には触れない（外部境界だけを差し替える）。
ログ行の伏せ字は `_MASK_CASES` の表（入力・消えるべき語・残るべき語・先頭／末尾）で確かめ、
バンドル全体の伏せ字と「伏せ忘れの自己検査（漏れたら止まる）」は個別のテストで確かめる。
"""
from __future__ import annotations

import json
import re
import tarfile

import pytest

import scripts.collect_diagnostics as cd
import scripts.doctor_checks as doctor_checks


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, doc_rows=None):
        self.doc_rows = doc_rows or []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        if "FROM documents" in sql:
            return _FakeCursor(self.doc_rows)
        return _FakeCursor([{"n": 0}])


def _patch_common(monkeypatch, tmp_path, *, doc_rows=None, worlds_rows=None, settings=None, usage=None,
                  ingest_runs=None):
    monkeypatch.setattr(cd.store, "_ensure", lambda: None)
    monkeypatch.setattr(cd.store, "_connect", lambda: _FakeConn(doc_rows))
    monkeypatch.setattr(cd.store, "list_worlds_db", lambda: worlds_rows or [])
    monkeypatch.setattr(cd.store, "get_system_settings", lambda: settings or {})
    monkeypatch.setattr(cd.store, "usage_stats", lambda days: usage if usage is not None else {})
    monkeypatch.setattr(cd.store, "list_ingest_runs", lambda limit=50: ingest_runs or [])
    monkeypatch.setattr(cd.doctor_checks, "run_all", lambda probe_cloud: [])
    monkeypatch.setattr(cd, "_collect_es_counts", lambda world_ids: {"status": "unavailable", "error": "OSError"})
    monkeypatch.setattr(cd, "_collect_neo4j_counts", lambda world_ids: {"status": "unavailable", "error": "OSError"})
    monkeypatch.setenv("SHERPA_LOG_DIR", str(tmp_path / "logs"))
    (tmp_path / "logs").mkdir(exist_ok=True)


def _bundle(monkeypatch, tmp_path, **kw):
    _patch_common(monkeypatch, tmp_path, **kw)
    out = tmp_path / "out.tar.gz"
    assert cd.main(["--out", str(out)]) == 0
    return out


def _member(tar_path, arcpath):
    with tarfile.open(tar_path, "r:gz") as tar:
        return json.loads(tar.extractfile(arcpath).read().decode("utf-8"))


def _log_bundle(tmp_path, text, name="app.log"):
    log_dir = tmp_path / "run"
    log_dir.mkdir(exist_ok=True)
    (log_dir / name).write_text(text, encoding="utf-8")
    return dict(cd.build_logs_bundle(log_dir, 7, 200.0))


# ---------------------------------------------------------------------------
# バンドル全体の伏せ字
# ---------------------------------------------------------------------------

def test_secret_like_settings_and_env_become_set_or_unset(monkeypatch, tmp_path):
    settings = {
        "openai_api_key": "sk-test-should-not-leak-1234567890",
        "Gemini_API_Key": "also-should-not-leak",
        "PASSWORD_hint": "",   # 空文字は <unset>
        "system_prompt": "根拠を示してください",   # プロンプト文は業務知識になりうる＝キーごと載せない
        "usage_chat_model": "gpt-5.5",
        "openai_base_url": "https://svc:sekrit@my-azure.example.com:8443/openai/v1?api-version=2024-05",
    }
    monkeypatch.setenv("SHERPA_OPENAI_TOKEN", "sk-test-env-secret-abcdefgh")
    monkeypatch.setenv("PGPASSWORD", "hunter2hunter2")
    monkeypatch.setenv("SHERPA_LOG_LEVEL", "INFO")
    monkeypatch.setenv("NEO4J_URI", "bolt://neo4j:sekritpw@graph.example.com:7687/?foo=bar")
    out = _bundle(monkeypatch, tmp_path, settings=settings)
    doc = _member(out, "settings.json")
    assert (doc["openai_api_key"], doc["Gemini_API_Key"], doc["PASSWORD_hint"], doc["system_prompt"]) == \
        ("<set>", "<set>", "<unset>", "<omitted>")
    assert doc["usage_chat_model"] == "gpt-5.5"
    url = doc["openai_base_url"]
    assert url.startswith("https://<host:") and ":8443/<path:" in url
    assert "my-azure.example.com" not in url and "openai/v1" not in url
    env = _member(out, "env.json")
    assert (env["SHERPA_OPENAI_TOKEN"], env["PGPASSWORD"], env["SHERPA_LOG_LEVEL"]) == ("<set>", "<set>", "INFO")
    assert env["NEO4J_URI"].startswith("bolt://<host:") and env["NEO4J_URI"].endswith(":7687/")
    raw = out.read_bytes()
    for leaked in (b"sk-test-should-not-leak", b"also-should-not-leak", b"sk-test-env-secret", b"hunter2hunter2",
                   b"sekrit", b"api-version", b"graph.example.com", b"foo=bar"):
        assert leaked not in raw


def test_log_bundle_masks_secret_patterns_and_pem_block(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "usage.log").write_text(
        "2026-09-16 10:00:00,000 INFO sherpa.usage: kind=chat token=sk-test-abcdefghijklmnop calls=1\n"
        "2026-09-16 10:00:01,000 WARNING sherpa: failed Authorization: Bearer sk-anothertoken1234567\n",
        encoding="utf-8")
    (log_dir / "convert.log").write_text(
        "2026-09-16 10:00:00,000 ERROR sherpa.ingest.convert: leaked secret follows\n"
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ\n-----END PRIVATE KEY-----\n",
        encoding="utf-8")
    joined = b"".join(data for _p, data in cd.build_logs_bundle(log_dir, 7, 200.0))
    for leaked in (b"sk-test-abcdefghijklmnop", b"sk-anothertoken1234567", b"MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ"):
        assert leaked not in joined
    assert b"[REDACTED]" in joined


def test_uid_and_relpath_are_hashed_and_names_dropped(monkeypatch, tmp_path):
    usage = {"users": [{"uid": "alice@example.com", "display_name": "Alice Example", "turns": 3}]}
    runs = [{"id": 1, "version": "sales-docs", "layer": "version", "status": "auto_published",
             "extraction_snapshot": {"counts": {"scanned": 1}, "stage_timings": {},
                                     "flags": [{"doc": "sales-docs/内部資料/価格表.xlsx", "action": "warn",
                                                "reason": "office_md:x"}]},
             "created_at": None, "published_at": None}]
    out = _bundle(monkeypatch, tmp_path, usage=usage, ingest_runs=runs)
    row = _member(out, "stats/usage_stats.json")["users"][0]
    assert row["uid"] == cd._hash_id("alice@example.com") and "display_name" not in row
    run = _member(out, "stats/ingest_runs.json")[0]
    assert run["flags"][0]["doc"] == cd._hash_id("sales-docs/内部資料/価格表.xlsx")
    assert run["world"] == cd._hash_id("sales-docs")
    raw = out.read_bytes()
    for leaked in (b"alice@example.com", b"Alice Example", "内部資料".encode(), b"sales-docs"):
        assert leaked not in raw


_FORBIDDEN_KEYS = {"content", "title", "email", "password_hash", "answer"}


def test_bundle_json_never_contains_forbidden_keys(monkeypatch, tmp_path):
    usage = {"users": [{"uid": "carol@example.com", "display_name": "Carol", "turns": 2}],
             "conversations_top": [{"conversation_id": 1, "uid": "carol@example.com", "world": "w1",
                                    "user_turns": 2, "kinds": [], "response_time_avg_ms": 100.0}]}
    runs = [{"id": 1, "version": "w1", "layer": "version", "status": "auto_published",
             "extraction_snapshot": {"flags": [{"doc": "w1/a.md", "action": "warn", "reason": "x"}]},
             "created_at": None, "published_at": None}]
    worlds = [{"world_id": "w1", "root_path": "/mnt/c/test", "label": "w1", "storage_mode": "external_reference",
               "last_sig": "abc", "last_synced_at": None, "last_doc_count": 5, "created_at": None, "updated_at": None}]
    out = _bundle(monkeypatch, tmp_path, usage=usage, ingest_runs=runs, worlds_rows=worlds)
    found: set[str] = set()

    def walk(v):
        if isinstance(v, dict):
            for k, vv in v.items():
                if k in _FORBIDDEN_KEYS:
                    found.add(k)
                walk(vv)
        elif isinstance(v, list):
            for vv in v:
                walk(vv)
    with tarfile.open(out, "r:gz") as tar:
        for m in tar.getmembers():
            if m.name.endswith(".json"):
                try:
                    walk(json.loads(tar.extractfile(m).read().decode("utf-8")))
                except json.JSONDecodeError:
                    continue
    assert not found, f"禁止キーが含まれています: {found}"


def test_flags_keep_only_allowlisted_keys_and_usage_numbers_survive(monkeypatch, tmp_path):
    runs = [{"id": 1, "version": "w", "layer": "version", "status": "auto_published",
             "extraction_snapshot": {"flags": [{"doc": "a.cbl", "reason": "dropped_syntax", "analyzer": "cobol",
                                                "name": "SECRET_PAYROLL", "snippet": "CALL 'SECRET'", "line": 3}]}}]
    usage = {"tokens": {"by_kind": [{"kind": "embed", "model": "registry.internal:5000/team/embed",
                                     "input_tokens": 10, "calls": 1}]}}
    _patch_common(monkeypatch, tmp_path, ingest_runs=runs, usage=usage)
    monkeypatch.setattr(cd, "_collect_es_counts", lambda w: {"indices": [{"index": "x", "embed_model": "registry.internal:5000/team/embed"}]})
    out = tmp_path / "out.tar.gz"
    assert cd.main(["--out", str(out)]) == 0
    raw = out.read_bytes()
    assert b"SECRET_PAYROLL" not in raw and b"CALL 'SECRET'" not in raw and b"registry.internal" not in raw
    assert set(_member(out, "stats/ingest_runs.json")[0]["flags"][0]) <= {"doc", "from", "reason", "action", "analyzer", "why", "line"}
    assert _member(out, "stats/usage_stats.json")["tokens"]["by_kind"][0]["input_tokens"] == 10


def test_one_source_failure_leaves_error_type_name_only(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)

    def _boom(days):
        raise RuntimeError("dsn=postgresql://user:sekrit@host/db であるべきだが失敗")
    monkeypatch.setattr(cd.store, "usage_stats", _boom)
    out = tmp_path / "out.tar.gz"
    assert cd.main(["--out", str(out)]) == 0
    assert _member(out, "stats/usage_stats.json") == {"error": "RuntimeError"}
    raw = out.read_bytes()
    assert b"sekrit" not in raw and b"dsn=" not in raw


def test_doctor_detail_is_dropped_and_dotenv_snapshot_applies_same_rules(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(cd.doctor_checks, "run_all", lambda probe_cloud: [
        doctor_checks.CheckResult("ollama", "Ollama", "fail", "llm.internal:11434 に接続できません")])
    out = tmp_path / "out.tar.gz"
    assert cd.main(["--out", str(out)]) == 0
    assert b"llm.internal" not in out.read_bytes()
    env_file = tmp_path / "envfile"
    env_file.write_text('OPENAI_API_KEY="sk-test-dummy-000000000000"\nSHERPA_PORT=8000\nPGPASSWORD=\n'
                        "OPENAI_BASE_URL=https://u:p@api.example.test/v1?x=1\nHOME=/home/someone\n# comment\n",
                        encoding="utf-8")
    monkeypatch.setattr(cd, "_DOTENV_PATH", env_file)
    snap = cd.build_dotenv_snapshot()
    assert (snap["OPENAI_API_KEY"], snap["PGPASSWORD"], snap["SHERPA_PORT"]) == ("<set>", "<unset>", "8000")
    assert snap["OPENAI_BASE_URL"].startswith("https://<host:") and "api.example.test" not in snap["OPENAI_BASE_URL"]
    assert "HOME" not in snap and "sk-test-dummy" not in json.dumps(snap)


def test_manifest_dry_run_and_no_schema_init(monkeypatch, tmp_path, capsys):
    called = []
    monkeypatch.setattr(cd.db_mod, "init_schema", lambda **kw: called.append(1))
    monkeypatch.setattr(cd.db_mod, "_inited", False)
    out = _bundle(monkeypatch, tmp_path)
    manifest = _member(out, "MANIFEST.json")
    assert "settings.json" in manifest["included_files"] and "MANIFEST.json" not in manifest["included_files"]
    assert manifest["rules_applied"] and manifest["self_check"]["performed"] is True
    assert called == []   # 読み取り専用: store の遅延初期化を走らせない
    assert cd.main(["--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "settings.json" in printed and "MANIFEST.json" in printed


# ---------------------------------------------------------------------------
# 伏せ忘れの自己検査（漏れたら止まる）
# ---------------------------------------------------------------------------

def test_selfcheck_aborts_when_relpath_leaks_and_passes_when_clean(monkeypatch, tmp_path):
    leaked = "極秘プロジェクト/契約書.docx"
    doc_rows = [{"name": leaked, "scope_path": None, "original_path": None, "md_path": None}]
    _patch_common(monkeypatch, tmp_path, doc_rows=doc_rows)
    out = tmp_path / "out.tar.gz"
    assert cd.main(["--out", str(out)]) == 0 and out.exists()   # 漏れが無ければ書き出す
    out.unlink()
    (tmp_path / "logs" / "convert.log").write_text(f"2026-09-16 10:00:00,000 INFO x: {leaked}\n", encoding="utf-8")
    monkeypatch.setattr(cd, "_mask_line", lambda t: t)   # ログのマスク漏れを模擬
    assert cd.main(["--out", str(out)]) != 0
    assert not out.exists()


def test_selfcheck_aborts_when_db_is_unreadable(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)

    def _boom(**kw):
        raise ConnectionError("dsn=postgresql://user:sekrit@host/db")
    monkeypatch.setattr(cd.store, "_connect", _boom)
    out = tmp_path / "out.tar.gz"
    assert cd.main(["--out", str(out)]) != 0 and not out.exists()


def test_selfcheck_ignores_fixed_json_keys_and_collects_split_needles(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path, doc_rows=[{"name": "a.pdf", "scope_path": None,
                                                   "original_path": "/srv/documents/a.pdf", "md_path": None}])
    ok, hits, _n = cd._run_selfcheck({"stats/db_counts.json": b'{"postgres": {"documents": 1}}'})
    assert ok and hits == {}   # 固定のキー名は文字列値ではないので止まらない
    ok, _h, _n = cd._run_selfcheck({"stats/x.json": b'{"k": "see /srv/documents/a.pdf"}'})
    assert not ok
    _patch_common(monkeypatch, tmp_path, doc_rows=[{"name": "人事 資料/給与一覧.xlsx", "scope_path": None,
                                                   "original_path": None, "md_path": None}])
    needles = cd._collect_selfcheck_needles()
    assert "給与一覧.xlsx" in needles and "人事 資料/給与一覧.xlsx" in needles


def test_selfcheck_is_token_based_and_substring_aware(monkeypatch):
    monkeypatch.setattr(cd, "_collect_selfcheck_needles",
                        lambda: ["役員 給与 一覧.xls", "役員", "給与", "一覧.xls", "confidential-plan"])
    ok, hits, n = cd._run_selfcheck({"logs/app.log": "2026 INFO x: <path:abc> done\n".encode()})
    assert ok and hits == {} and n == 5
    ok, hits, _ = cd._run_selfcheck({"logs/app.log": "2026 INFO x: failed: /mnt/kb/役員 給与 一覧.xls\n".encode()})
    assert not ok and hits["logs/app.log"] >= 3
    assert not cd._run_selfcheck({"logs/app.log": "opened (confidential-plan).\n".encode()})[0]
    assert not cd._run_selfcheck({"stats/x.json": json.dumps({"documents": 3, "k": "confidential-plan"}).encode()})[0]
    assert cd._run_selfcheck({"stats/x.json": json.dumps({"documents": 3, "k": "fine"}).encode()})[0]
    monkeypatch.setattr(cd, "_collect_selfcheck_needles", lambda: ["極秘顧客", "confidential-plan"])
    ok, hits, _ = cd._run_selfcheck({"logs/app.log": "x: 2026 極秘顧客向け\n".encode()})
    assert not ok and hits == {"logs/app.log": 1}
    assert not cd._run_selfcheck({"logs/app.log": "x: confidential-plan.bak\n".encode()})[0]


# ---------------------------------------------------------------------------
# 設定値・環境変数・統計値の個別規則
# ---------------------------------------------------------------------------

def test_env_and_settings_value_sanitizers():
    h = cd._hash_id
    assert cd._sanitize_env_scalar("host=db-host password=example_password dbname=x") == "<set>"
    assert cd._sanitize_settings_value("SHERPA_AUDIT_IP_SALT", "s3cr3t-salt-value") == "<set>"
    assert cd._sanitize_env_pair("SHERPA_UID", "alice@example.test").startswith("<uid:")
    assert cd._sanitize_env_pair("PGUSER", "alice").startswith("<uid:")
    assert cd._sanitize_env_pair("PGHOST", "db.internal") == f"<host:{h('db.internal')}>"
    assert "graph.internal" not in cd._sanitize_env_pair("NO_PROXY", "db.internal,graph.internal")
    for key in ("SHERPA_USERS_DIR", "SHERPA_OCR_MODEL_CACHE"):
        assert cd._sanitize_env_pair(key, "confidentialstaff") == f"<path:{h('confidentialstaff')}>"
    assert cd._sanitize_env_pair("SHERPA_ENV_FILE", "") == "<unset>"
    assert cd._sanitize_env_pair("SHERPA_SOFFICE_BIN", "/opt/x/soffice").startswith("<path:")
    assert cd._sanitize_env_pair("SHERPA_MCP_WORLD_ROOT", "/mnt/kb").startswith("<path:")
    assert cd._sanitize_env_pair("SHERPA_BROWSE_ROOTS", "/mnt/a, /mnt/b") == f"<path:{h('/mnt/a')}>,<path:{h('/mnt/b')}>"
    assert cd._sanitize_env_pair("SHERPA_MCP_WORLD", "test2") == f"<world:{h('test2')}>"
    assert cd._sanitize_settings_value("ollama_url", "llm.internal:11434") == f"<host:{h('llm.internal')}>:11434"
    v6 = cd._sanitize_settings_value("ollama_url", "[fd12:3456:789a::25]:11434")
    assert "fd12" not in v6 and v6.endswith(":11434")
    windows = cd._sanitize_settings_value("model_context_windows",
                                          {"ollama:registry.internal:5000/team/model": 32768, "gpt-5.5": 400000})
    assert all("registry.internal" not in k for k in windows) and windows["gpt-5.5"] == 400000
    assert cd._sanitize_generic({"provider_keys": {"openai": "sk-test-x"}}, cd._mask_text) == {"provider_keys": "<set>"}
    pk = cd._sanitize_generic({"python": {"pdfminer.six": "20221228", "boolean.py": "4.0"}}, cd._mask_text)
    assert pk["python"] == {"pdfminer.six": "20221228", "boolean.py": "4.0"}   # パッケージ名は動的キーではない
    assert h("inference01") in cd._strip_url_userinfo_query("http://inference01:11434/v1")


# ---------------------------------------------------------------------------
# ログ行の伏せ字（表）
# ---------------------------------------------------------------------------

def _c(text, gone=(), keep=(), start=None, end=None):
    return (text, tuple(gone), tuple(keep), start, end)


_SKILL = "codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/"
_IMP = "importance: failed to read control file "
_MD = "MD化を開始します: "


_MASK_CASES = [
    _c('codex mcp calls: total=1 max_in_flight=1 conv=1 uid=alice123', gone=('alice123',), keep=('<uid:',)),
    _c('MD化を開始します: 極秘 計画書.docx', gone=('極秘', '計画書'), keep=('<path:',)),
    _c('query="顧客 買収計画" took=3ms', gone=('買収計画',), keep=('took=3ms',)),
    _c('検索語: 顧客 買収計画', gone=('買収計画',)),
    _c('MD化が完了しました: 極秘 folder/計画書.docx（1.2秒）', gone=('極秘', '計画書'), keep=('（1.2秒）',)),
    _c('行1\nERROR rel=月次 報告 一覧\nquery=顧客 買収計画\n行3', gone=('月次', '買収計画'), keep=('行3',)),
    _c('MD化が完了しました: 給与（役員）.xlsx（1.2秒）', gone=('役員', '給与'), keep=('（1.2秒）',)),
    _c('embed 進捗 100/200 チャンク（world=w1）', keep=('100/200', 'world=<world:')),
    _c('MD化中に想定外の例外が発生しました（failed として継続）: /srv/kb/役員 給与 一覧.xlsx', gone=('役員', '給与'), keep=('（failed として継続）',)),
    _c('uvicorn.access: 192.168.10.25:54321 - "POST /chat HTTP/1.1" 200', gone=('192.168.10.25',), keep=('200', '<ip:')),
    _c('register 失敗時の ES 索引削除に失敗しました world_id=payroll', gone=('payroll',)),
    _c('MD化が完了しました: 給与.xlsx（1.2秒・RSS 0.1G→0.2G）', gone=('給与',), keep=('（1.2秒・RSS 0.1G→0.2G）',)),
    _c('MD化を開始します: 部門別/給与 一覧.xlsx（RSS 3.1G）', gone=('給与',), keep=('（RSS 3.1G）',)),
    _c('MD化が完了しました: 部門別/給与 一覧.xlsx（12.3秒・RSS 3.1G→3.4G）', gone=('給与',), keep=('（12.3秒・RSS 3.1G→3.4G）',)),
    _c("register 失敗（取り込みエラー）: [{'reason': 'qualified_fallback', 'from': 'sub/ABC.cbl', 'kind': 'copy', 'name': 'CUSTMAST-REC'}]", gone=('ABC.cbl', 'CUSTMAST-REC')),
    _c("x: [{'reason': 'qualified_fallback', 'from': 'sub/ABC.cbl', 'kind': 'copy', 'name': 'CUSTMAST-REC'}]", gone=('ABC.cbl', 'CUSTMAST-REC'), keep=("'kind': 'copy'",)),
    _c('kind=chat calls=1 elapsed=3.2s world=w depth=deep reasoning=turns=3/tools=5', keep=('turns=3/tools=5',)),
    _c('派生 dir に marker を書けないため再ビルドを見送ります: /x/人事 資料/給与 役員 一覧.xlsx', gone=('人事', '給与')),
    _c('VLM 視覚読み取りに失敗しました（/mnt/c/kb/manuals/Sales Report 2026.pdf）', gone=('Sales', 'Report', '2026.pdf'), end='）'),
    _c('原本を読めません: 給与 役員 一覧.png', gone=('給与',)),
    _c('graph_reflect_failed:ServiceUnavailable@graph.internal:7687', gone=('graph.internal',), keep=(':7687', '@<host:')),
    _c('uvicorn.access: fd12:3456:789a::25:54321 - "GET /x" 200', gone=('789a', 'fd12'), keep=('<ip:',)),
    _c('graph_reflect_failed:ServiceUnavailable@fd12:3456:789a::25:7687', gone=('789a',)),
    _c('SsrfBlocked: 不正な接続先 URL です: llm.internal:11434', gone=('llm.internal',), keep=(':11434',)),
    _c('規則版への再生成が一部失敗しました: world=test2 detail=timeout', gone=('<path:',), keep=('detail=timeout', 'world=<world:{h:test2}>')),
    _c('connection failed https://llm.internal', gone=('llm.internal',), keep=('<host:',)),
    _c('2026-09-16 10:00:00,000 WARNING sherpa.ingest.worker: connection failed https://sherpa.internal', gone=('sherpa.internal',), start='2026-09-16 10:00:00,000 WARNING sherpa.ingest.worker: '),
    _c('connection failed inference01:11434', gone=('inference01',), keep=(':11434',)),
    _c('connection failed 01-llm.internal', gone=('01-llm',), keep=('<host:',)),
    _c('connection failed http://inference01/v1', gone=('/v1', 'inference01'), keep=('<host:', '{h:/v1}'), start='connection failed <url:http|'),
    _c('x http://inference01:11434/v1', gone=('inference01',), keep=(':11434', '{h:inference01}')),
    _c('2026-09-16 10:00:00,123 WARNING sherpa.ingest.arms.legacy_convert: legacy 変換を実行できませんでした（OSError）: /mnt/kb/test2/src/役員 給与 一覧.xls', gone=('給与', '一覧.xls'), keep=('<path:',)),
    _c('2026-09-16 10:00:00,123 WARNING sherpa.api: poll sync failed: world=test2 err=OSError', gone=('test2',), keep=('<world:', '{h:test2}')),
    _c('connection failed http://alice:pass,SecretTail@inference01/v1/private,ConfidentialName(x) next', gone=('SecretTail', 'inference01', 'ConfidentialName'), end=' next'),
    _c('see https://llm.internal/v1, then retry', gone=('llm.internal',), end='>, then retry'),
    _c('connection failed llm.internal.', gone=('llm.internal',), keep=('<host:',)),
    _c('codex created file move/registration failed for run-abc/役員 極秘顧客 一覧.pptx: type=OSError errno=28', gone=('役員', '極秘', '極秘顧客'), end=': type=OSError errno=28'),
    _c('stale codex run dir cleanup failed for /tmp/x/run 1: type=OSError errno=13', gone=('run 1',), end=': type=OSError errno=13'),
    _c('codex_skills: copy failed for /a/b c -> /d/e f: OSError', gone=(' c ', ' f:'), start='codex_skills: copy failed for <path:'),
    _c('2026-09-16 10:00:00,000 INFO x: see <http://inference01/v1> ok', gone=('inference01',), end='> ok'),
    _c('2026-09-16 10:00:00,000 WARNING sherpa.corpus_docs: last_run_flags: world=test2 直近 ingest run の取得に失敗しました: timeout', gone=('test2',), keep=('{h:test2}',)),
    _c('codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員 極秘顧客 一覧', gone=('一覧', '極秘顧客'), start='codex_skills: symlink inside skill source rejected: <path:'),
    _c('codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員(極秘顧客)一覧', gone=('一覧', '極秘顧客')),
    _c('codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員 2026 極秘顧客向け', gone=('2026', '極秘顧客')),
    _c('codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員（極秘顧客）一覧', gone=('一覧', '極秘顧客'), end='>'),
    _c('MD化が失敗しました: /kb/役員（極秘）一覧.xlsx（1.2秒）', gone=('極秘',), end='（1.2秒）'),
    _c('legacy 変換を実行できませんでした（OSError）: /kb/役員 一覧.xls', gone=('一覧',), keep=('（OSError）',)),
    _c('codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員（ACME）一覧', gone=('一覧', 'ACME')),
    _c('x: /srv/skills/役員（極秘（顧客））一覧（1.2秒）', gone=('顧客', '1.2秒')),
    _c('grep_search: 秘匿名のため派生MDを対象外にしました（ext=.docx）', keep=('秘匿名のため派生MDを対象外にしました',)),
    _c('MD化をスキップします（秘匿名のため対象外・ext=.xlsx）', keep=('MD化をスキップします',)),
    _c('x: /srv/skills/役員・極秘一覧', gone=('極秘一覧',)),
    _c('MD化が完了しました: manuals/report.xlsx（12.3秒・RSS 0.1G→0.2G）', gone=('report',), end='（12.3秒・RSS 0.1G→0.2G）'),
    _c("AGENTS.md write failed: [Errno 13] Permission denied: '/x/役員 一覧/AGENTS.md'", gone=('一覧',), keep=("Permission denied: '<path:",)),
    _c('codex created file move/registration failed for run-abc/役員 一覧.pptx: type=OSError errno=28', gone=('役員',), end=': type=OSError errno=28'),
    _c('codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員 <極秘顧客> 一覧', gone=('一覧', '極秘')),
    _c('（world=test2・/x/y.json） then http://h.internal/v1', gone=('h.internal',), keep=('{h:test2}',)),
    _c('reconvert 監査ログの記録に失敗しました（best-effort）: action=x world=test2 rel=docs/a b.md', gone=(' b.md',), keep=('{h:test2}',)),
    _c('kind=embed provider=ollama model=BAAI/bge-m3 in=52340 cached=0 out=0 calls=3 elapsed=12.4s world=test2', gone=('bge-m3',), keep=('in=52340 cached=0 out=0 calls=3 elapsed=12.4s', '{h:test2}')),
    _c('codex created file move/registration failed for run-abc/役員 type=極秘顧客 一覧.pptx: type=OSError errno=28', gone=('極秘',), end=': type=OSError errno=28'),
    _c('codex_skills: copy failed for /a/役員 b -> /d/極秘 f: OSError', gone=('役員', '極秘'), start='codex_skills: copy failed for <path:'),
    _c('codex created file move/registration failed for run-abc/役員 -> 極秘顧客 一覧.pptx: type=OSError errno=28', gone=('極秘',), end=': type=OSError errno=28'),
    _c('workspace search: symlink rejected for uid=alice rel=docs/役員 type=極秘顧客 一覧.txt', gone=('極秘',), keep=('{h:alice}',)),
    _c('ext_api POST /v1/docs/役員 一覧 -> 200 (12.0ms) request_id=abc', gone=('役員',), end=' -> 200 (12.0ms) request_id=abc'),
    _c('kind=embed provider=ollama model=BAAI/bge-m3 in=52340 calls=3', gone=('bge-m3',), keep=('in=52340 calls=3',)),
    _c('codex_skills: copy failed for /srv/users/u1/workspace/skills/役員 -> <極秘顧客> 一覧 -> /tmp/dst: OSError', gone=('一覧', '極秘')),
    _c('codex created file move/registration failed for run-abc/役員 -> 200 極秘顧客 一覧.pptx: type=OSError errno=28', gone=('極秘',), end=': type=OSError errno=28'),
    _c('MD化を開始します: 役員 type=極秘顧客 一覧.xlsx（RSS 0.1G）', gone=('極秘',), end='（RSS 0.1G）'),
    _c('codex_skills: copy failed for /srv/users/u1/workspace/skills/役員 -> 200 (12.0ms) 極秘顧客 一覧 -> /tmp/x: OSError', gone=('極秘',)),
    _c('2026-09-16 10:00:00,000 INFO sherpa.ext: ext_api POST /v1/docs/a b -> 200 (12.0ms) request_id=abc', gone=(' b ',), end=' -> 200 (12.0ms) request_id=abc'),
    _c('MD化を開始します: 役員（RSS 極秘顧客）一覧.xlsx（RSS 0.1G）', gone=('極秘',), end='（RSS 0.1G）'),
    _c('MD化が完了しました: 役員（1.2秒）一覧.xlsx（12.3秒・RSS 0.1G→0.2G）', gone=('一覧', '役員'), end='（12.3秒・RSS 0.1G→0.2G）'),
    _c('MD化を開始します: type=極秘顧客 一覧.xlsx（RSS 0.1G）', gone=('極秘',), end='（RSS 0.1G）'),
    _c('LLM 成形の呼び出しに失敗しました（規則版のまま・次回パスで再試行）: world=test2 rel=docs/a b.md', gone=(' b.md',), keep=('{h:test2}',)),
    _c('RAG/Evidence IR の軽量再生成に失敗しました（次回 sync で再試行）: world=test2 detail=timeout after 3s', keep=('detail=timeout after 3s', '{h:test2}')),
    _c('Webhook 配送キューが飽和したため1件破棄しました: key_id=k1 world=test2', keep=('{h:test2}',)),
    _c('OCR 観測 Set の読込/検証に失敗しました（VLM のみで rag.md を生成します）: 人事部/役員 極秘 顧客 一覧.xlsx', gone=('極秘', '人事部'), keep=('を生成します）: <path:',), start='OCR 観測 Set の読込/検証に失敗しました（VLM のみで '),
    _c('VLM/OCR 観測 Set の合流に失敗しました（VLM 単独へ縮退します）: 人事部/役員.xlsx', gone=('人事部',), start='VLM/OCR 観測 Set の合流に失敗しました（VLM 単独へ縮退します）: <path:'),
    _c('ext_api POST /v1/chat -> unhandled exception request_id=abc123', gone=('/v1/chat',), end=' -> unhandled exception request_id=abc123'),
    _c('MD化を開始します: uid=x x=ACME detail=一覧.xlsx（RSS 0.1G）', gone=('一覧', 'ACME'), end='（RSS 0.1G）'),
    _c('human_md の軽量再生成中に想定外の例外が発生しました: uid=x rc=ACME detail=一覧.xlsx', gone=('一覧', 'ACME')),
    _c('Webhook 通知の起動に失敗しました（best-effort）: world=test2 run_id=r1', keep=('{h:test2}',), end=' run_id=r1'),
    _c('codex_skills: symlink inside skill source rejected: AP/COMMON 極秘 一覧.md', gone=('極秘', 'COMMON')),
    _c('stale codex run dir cleanup failed for JCL/PROD 極秘 run: type=OSError errno=13', gone=('極秘', 'PROD'), end=': type=OSError errno=13'),
    _c('grep_search: 対象 SRC/COMMON を走査しました（12.3秒）', gone=('COMMON',)),
    _c('codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/uid=x 極秘顧客 一覧', gone=('一覧', '極秘', '<uid:')),
    _c('MD化を開始します: /kb/world=x 極秘 一覧.xlsx（RSS 0.1G）', gone=('極秘', '<world:')),
    _c('rag.md のレコード分割に失敗したため LLM 成形を skip します（規則版のまま）: world=test2 rel=人事部/役員報酬 極秘資料 一覧.xlsx', gone=('役員', '極秘', '人事部'), keep=('{h:test2}',)),
    _c('reconvert 監査ログの記録に失敗しました（best-effort）: action=x world=test2 rel=役員報酬 極秘資料 一覧.xlsx', gone=('極秘',), keep=('{h:test2}',)),
    _c('impact/run が Neo4j 安全弁で失敗（fail-loud・reason=guard・world=test2）', keep=('{h:test2}',), start='impact/run が Neo4j 安全弁で失敗'),
    _c("RAG/Evidence IR の軽量再生成に失敗しました（次回 sync で再試行）: world=test2 detail={'rag_failed': 2}", keep=("detail={'rag_failed': 2}", '{h:test2}'), start='RAG/Evidence IR の軽量再生成に失敗しました'),
    _c('背景実行が未捕捉の例外で終了しました: world=test2 op=sync run_id=r-9', keep=('{h:test2}',), end=' op=sync run_id=r-9'),
    _c('取り込み 100/200 done（3件） world=test2', start='取り込み 100/200 done'),
    _c('codex created files registration setup failed (run_dir=/tmp/r): [Errno 13] Permission denied: \'/srv/users/u1/workspace/skills/顧客\\\'s "極秘 案件"\'', gone=('案件', '極秘', '顧客')),
    _c('importance: failed to stat control file cfg=dept/役員: 極秘顧客 一覧/_重要度.txt', gone=('一覧', '役員', '極秘')),
    _c('importance: failed to read control file cfg=dept/役員: type=極秘顧客 一覧/_重要度.txt', gone=('一覧', '極秘')),
    _c('importance: failed to read control file failed for reports/役員: type=極秘顧客 一覧/_重要度.txt', gone=('一覧', '極秘')),
    _c('importance: failed to read control file 人事部/役員 極秘顧客 一覧/_重要度.txt', gone=('極秘', '人事部'), start='importance: failed to read control file <path:'),
    _c('OCR 観測 Set の読込/検証に失敗しました（VLM のみで rag.md を生成します）: 人事部/役員.xlsx', start='OCR 観測 Set の読込/検証に失敗しました'),
    _c('importance: failed to read control file 極秘 案件/資料/_重要度.txt', gone=('案件', '極秘'), start='importance: failed to read control file <path:'),
    _c('codex_skills: symlink inside skill source rejected: 極秘 案件/資料', gone=('案件', '極秘')),
    _c('importance: failed to stat control file 極秘（役員） 案件/資料/_重要度.txt', gone=('役員', '極秘'), start='importance: failed to stat control file <path:'),
    _c('importance: failed to read control file ACME for 案件/資料/_重要度.txt', gone=('案件', 'ACME'), start='importance: failed to read control file <path:'),
    _c('importance: failed to read control file ACME: 案件/資料/_重要度.txt', gone=('案件', 'ACME'), start='importance: failed to read control file <path:'),
    _c('importance: failed to read control file ACME=社外秘 案件/資料/_重要度.txt', gone=('社外秘', 'ACME'), start='importance: failed to read control file <path:'),
    _c('workspace search: symlink rejected for uid=alice rel=docs/役員 一覧.txt', gone=('役員',), keep=('{h:alice}',)),
    _c('codex_skills: skip non-dir/symlink skill source: /srv/users/u1/workspace/skills/役員 一覧', gone=('役員',), start='codex_skills: skip non-dir/symlink skill source: <path:'),
    _c('marp_render: unshare によるネットワーク隔離が使えないため pdf/pptx をスキップ（html/.md のみ生成）', start='marp_render: unshare によるネットワーク隔離が使えないため pdf/pptx をスキップ'),
    _c("usage_chat: 専用設定/一時上書きの provider 値が不正です（value='x'）", start='usage_chat: 専用設定/一時上書きの provider 値が不正です'),
    _c("importance: failed to read control file ACME'社外秘 案件/資料/_重要度.txt", gone=('社外秘', 'ACME'), start='importance: failed to read control file <path:'),
    _c("importance: failed to read control file ACME社外秘' 案件/資料/_重要度.txt", gone=('社外秘', 'ACME'), start='importance: failed to read control file <path:'),
    _c("AGENTS.md write failed: [Errno 13] Permission denied: '役員 一覧/AGENTS.md'", gone=('一覧', '役員'), keep=("Permission denied: '<path:",), end="'"),
    _c('importance: failed to read control file rel=案件/資料/_重要度.txt', gone=('案件',)),
    _c('neo4j クエリがタイムアウト（world=test2）: 人事部/役員 一覧', gone=('役員', '人事部'), keep=('{h:test2}',)),
    _c('codex created file move/registration failed for Shared Docs/Board 一覧.xlsx: type=OSError errno=28', gone=('Board', 'Shared'), end=': type=OSError errno=28'),
    _c('importance: failed to read control file ACME: X rel=案件/資料/_重要度.txt', gone=('案件', 'ACME'), start='importance: failed to read control file <path:'),
    _c('importance: failed to read control file files/極秘顧客 資料/_重要度.txt', gone=('極秘', '資料'), start='importance: failed to read control file <path:'),
    _c('x: pdf/pptx-archive/極秘 一覧.pptx', gone=('極秘',)),
    _c('importance: failed to read control file ACME VLM/OCR 極秘/_重要度.txt', gone=('極秘', 'ACME'), start='importance: failed to read control file <path:'),
    _c('importance: failed to read control file ACME: VLM/OCR 極秘/_重要度.txt', gone=('極秘', 'ACME'), start='importance: failed to read control file <path:'),
    _c('sub_planner: 計画呼び出しが失敗/空のため縮退します'),
    _c('VLM(ollama): 接続先（llm-host）がローカル/私有アドレスと確認できず、クラウド許可も無いため送信しません（fail-safe）。', keep=('ローカル/私有アドレスと確認できず',)),
    _c('office_com upload が失敗しました（HTTP 500・試行 2/3）: http://o.internal:8080/c', gone=('o.internal',), keep=(':8080', '<url:http|<host:', '試行 2/3）')),
    _c('neo4j 接続に失敗しました: bolt://graph.internal:7687', gone=('graph.internal',), keep=(':7687', '<url:bolt|<host:')),
    _c('importance: failed to read control file ACME 2026/09 極秘/_重要度.txt', gone=('極秘', 'ACME'), start='importance: failed to read control file <path:'),
    _c('ValueError: asset inventory contains symlink: ACME 2026/09 極秘/役員.png', gone=('極秘', 'ACME'), start='ValueError: asset inventory contains symlink: <path:'),
    _c('ValueError: asset inventory contains symlink: ACME VLM/OCR 極秘/役員.png', gone=('極秘', 'ACME'), start='ValueError: asset inventory contains symlink: <path:'),
    _c('workspace search: symlink rejected for uid=alice rel=役員 一覧: type=極秘', gone=('役員', '極秘'), keep=('{h:alice}',)),
    _c("gc_orphan: failed uid=u1 file=役員: type=極秘顧客 一覧.xlsx: [Errno 13] Permission denied: '役員: type=極秘顧客 一覧.xlsx'", gone=('役員', '極秘'), start='gc_orphan: failed uid=<uid:'),
    _c('register 失敗時の派生ディレクトリ削除でエラー path=/home/x/data/derived/test2: [Errno 39] Directory not empty', gone=('test2', 'derived')),
    _c('ValueError: asset inventory contains symlink: 顧客資料（type=ACME）', gone=('顧客', 'ACME')),
    _c('VLM: provider=ollama の接続先（inference01）がローカル/私有アドレスと確認できず、クラウド許可も無いため送信しません', gone=('inference01',), keep=('ローカル/私有アドレスと確認できず', '接続先（<host:')),
    _c('VLM(ollama): 接続先（http://llm.internal:11434）がローカル/私有アドレスと確認できず', gone=('llm.internal',), keep=(':11434',)),
    _c('sweep_expired: path outside files_dir, skipping uid=u1 rel=役員 一覧: type=極秘顧客', gone=('役員', '極秘')),
    _c("codex_skills: copy failed for /srv/users/u1/workspace/skills/役員: type=極秘顧客 一覧 -> /tmp/x: [Errno 13] Permission denied: '/srv/users/u1/workspace/skills/役員: type=極秘顧客 一覧'", gone=('役員', '極秘')),
    _c('sweep_expired: re-upload detected under lock, skipping uid=u1 rel=顧客資料（type=ACME）', gone=('顧客', 'ACME')),
    _c('codex created file move/registration failed for run-abc/顧客別利益率（30%）: type=OSError errno=28', gone=('30%', '利益率'), end=': type=OSError errno=28'),
    _c('MD化を開始します: docs/x.xlsx（RSS 0.1G）', end='（RSS 0.1G）'),
    _c('codex created file move/registration failed for run-abc/顧客応答時間（30秒）: type=OSError errno=28', gone=('応答', '30秒'), end=': type=OSError errno=28'),
    _c('legacy 変換がタイムアウトしました（60s）: /kb/a b（30秒）.xls', gone=('30秒',)),

    # 名前の中の引用符・区切り・キー=値は欄を割らず、まるごと伏せる
    *[_c(_SKILL + n, gone=("極秘顧客",)) for n in ("役員'極秘顧客'一覧", "役員（dept=極秘顧客）", '役員"極秘顧客"')],
    *[_c(_SKILL + n, gone=("極秘", "ACME")) for n in ("役員' 極秘顧客 一覧", "役員（dept=ACME）", '役員" 極秘 "一覧',
                                                      "役員（type=極秘顧客）", "役員 | 極秘顧客")],
    *[_c(_SKILL + n, gone=("極秘", "一覧")) for n in ("役員 dept=極秘顧客 一覧", "役員 <path:極秘顧客> 一覧",
                                                      "役員 type=極秘顧客 一覧", "役員: 極秘 一覧", "役員 -> 極秘")],
    _c("x: /a/b（type=OSError）", gone=("OSError",)),
    *[_c(_MD + n + "（RSS 0.1G）", gone=("極秘", "一覧"), end="（RSS 0.1G）") for n in ("detail=極秘顧客 一覧.xlsx", "world=極秘顧客 一覧.xlsx")],
    *[_c(f % "uid=x x=ACME detail=一覧.xlsx", gone=("ACME", "一覧"), keep=(": <path:",)) for f in (
        "human_md の軽量再生成中に想定外の例外が発生しました: %s",
        "sidecarマニフェストの書込に失敗しました（次回 sync が欠落として検知し再生成する）: %s",
        "Evidence IR生成に失敗したためsource-level failed noticeへ縮退します: %s")],
    *[_c("x " + n, gone=("dept",), keep=("<path:",)) for n in ("dept/plan.xls", "dept/plan.doc", "dept/plan.ppt", "dept/scan.png", "dept/scan.jpg")],
    *[_c(_IMP + n, gone=("ACME", "社外秘"), start=_IMP + "<path:") for n in ("'ACME' 社外秘 案件/資料/_重要度.txt", "ACME<社外秘> 案件/資料/_重要度.txt", "ACME|社外秘 案件/資料/_重要度.txt")],
    *[_c(_IMP + n, gone=("ACME", "案件"), start=_IMP + "<path:") for n in ("ACME rel=案件/資料/_重要度.txt", "ACME world=x uid=y 案件/資料.txt")],
    *[_c(_IMP + n, gone=("極秘",), start=_IMP + "<path:") for n in ("COBOL/JCL 極秘顧客/_重要度.txt", "non-dir/symlink 極秘/_重要度.txt", "impact/run 極秘/_重要度.txt")],
    *[_c(p + n, gone=("ACME", "極秘", "案件"), start=p + "<path:") for p in ("ValueError: asset inventory contains symlink: ", _IMP)
      for n in ("ACME 2026/09", "ACME VLM/OCR", "ACME: 極秘/役員.png", "極秘 案件", "ACME for x: type=極秘")],
    _c("codex created files registration setup failed (run_dir=/tmp/r): [Errno 13] Permission denied: '/srv/users/u1/workspace/skills/顧客\\'s \"極秘 案件\"'", gone=("極秘", "案件", "顧客")),
    # 構造化されていない・変わってはいけない行
    *[_c(s, keep=(s,), start=s, end=s) for s in (
        "== エラー/警告（全ログ） ==", "v1.2.3 は 2026-09-16 に", "2026-09-16 10:00:00,000 INFO x", "Started server process [12345]",
        "version 1.2.3 at 10:00", "version 1.2.3.", "waiting for 3 seconds", "embed 進捗 100/200（3件）",
        "sub_planner: 計画呼び出しが失敗/空のため縮退します")],
    _c("docs/設計.md を読みました", keep=("<path:",)),
    _c("read report.pdf", keep=("<path:",)),
    _c("client [::1]:5000", keep=("<ip:",)),
    _c("取り込み 100/200 done（3件） world=test2", start="取り込み 100/200 done（3件）"),
    _c("es_index: embed 進捗 100/200 チャンク（world=test2）", start="es_index: embed 進捗 100/200 チャンク"),
    *[_c(s, keep=("{h:test2}",)) for s in ("（world=test2・stored=v3）", "（world=test2・/x/y.json）", "world=test2 は")],
    _c("uid=alice: OSError", keep=("{h:alice}",)),
]


@pytest.mark.parametrize("text,gone,keep,start,end", _MASK_CASES)
def test_mask_text(text, gone, keep, start, end):
    def expand(s):
        return re.sub(r"\{h:([^}]*)\}", lambda m: cd._hash_id(m.group(1)), s)
    line = cd._mask_text(text)
    assert not [g for g in gone if g in line], line
    assert all(expand(k) in line for k in keep), line
    if start is not None:
        assert line.startswith(expand(start)), line
    if end is not None:
        assert line.endswith(expand(end)), line


def test_mask_text_changes_email_and_keeps_inline_structure():
    assert cd._mask_text("user@example.com") != "user@example.com"


@pytest.mark.parametrize("text", [
    "2026-09-16 10:00:00,000 WARNING sherpa.codex_skills: " + _SKILL + "役員\n極秘顧客 一覧\n2026-09-16 10:00:01,000 INFO sherpa.x: next line ok",
    "2026-09-16 10:00:00,000 WARNING sherpa.codex_skills: " + _SKILL + "役員\n\n極秘顧客 一覧\n2026-09-16 10:00:01,000 INFO sherpa.x: next line ok",
    "2026-09-16 10:00:00,000 WARNING sherpa.codex_skills: " + _SKILL + "役員)\n\n極秘顧客 一覧\n2026-09-16 10:00:01,000 INFO sherpa.x: next line ok",
    "2026-09-16 10:00:00,000 WARNING sherpa.codex_skills: " + _SKILL + "役員（1.2秒）\n極秘顧客 一覧\n2026-09-16 10:00:01,000 INFO sherpa.x: next line ok",
], ids=["next-line", "blank-between", "trailing-punct", "metric-note"])
def test_continuation_line_after_name_field_is_masked_through_log_bundle(tmp_path, text):
    data = _log_bundle(tmp_path, text + "\n")["logs/app.log"].decode("utf-8")
    assert "極秘" not in data and "一覧" not in data and "next line ok" in data
    assert "極秘" not in cd._mask_text(text) and cd._mask_text(text).endswith("next line ok")


def test_traceback_continuation_survives_while_label_and_symlink_path_are_masked():
    out = cd._mask_text(
        "2026-09-16 10:00:00,000 ERROR sherpa.ingest.office_md: human_md の軽量再生成中に想定外の例外が発生しました: docs/a b.md\n"
        "Traceback (most recent call last):\n"
        "  File \"/opt/sherpa/sherpa/ingest/office_md.py\", line 633, in _regen\n"
        "    raise OSError(28, 'No space left')\nOSError: [Errno 28] No space left")
    assert "Traceback (most recent call last):" in out and "OSError: [Errno 28] No space left" in out and " b.md" not in out
    out = cd._mask_text(
        "2026-09-16 10:00:00,000 ERROR sherpa.ingest.ocr_router: inventory failed\n"
        "Traceback (most recent call last):\n"
        "  File \"/opt/sherpa/sherpa/ingest/ocr_router.py\", line 147, in _inv\n"
        "ValueError: asset inventory contains symlink: 人事部 極秘/役員 一覧.png")
    assert not {"人事部", "極秘", "役員"} & set(re.findall(r"人事部|極秘|役員", out))
    assert "contains symlink: <path:" in out


def test_path_token_in_log_line_is_hashed_but_wording_stays(tmp_path):
    data = _log_bundle(tmp_path, "2026-09-16 10:00:00,000 INFO sherpa.ingest.convert: MD化を開始します: sales-docs/内部資料/次年度計画書.docx\n",
                       "convert.log")["logs/convert.log"].decode("utf-8")
    assert "内部資料" not in data and "<path:" in data and "MD化を開始します" in data


def test_log_cap_is_reported(tmp_path):
    log_dir = tmp_path / "run"
    log_dir.mkdir()
    (log_dir / "convert.log").write_text("2026-09-16 10:00:00,000 INFO x: big\n" * 20000, encoding="utf-8")
    (log_dir / "embed.log").write_text("2026-09-16 10:00:00,000 INFO x: small\n", encoding="utf-8")
    names = [a for a, _d in cd.build_logs_bundle(log_dir, 7, 0.1)]
    assert "logs/embed.log" in names and "logs/convert.log" not in names and "logs/truncated.json" in names
    entries = cd.build_logs_bundle(log_dir, 7, 0.00001)
    assert [a for a, _d in entries] == ["logs/truncated.json"]
    assert "打ち切られました（2 本）" in cd.build_log_report_text(entries)
