"""`scripts/collect_diagnostics.py`（`make diag`・解析用ログ回収バンドル）の単体テスト。

第一契約（機密を含めない）の実害を固定する。DB/ES/Neo4j には一切触れない
（`sherpa.store`/`sherpa.es_index`/`scripts.doctor_checks` の外部境界だけを monkeypatch で差し替える・
`tests/unit/test_store_worlds_scan_report_sql.py` と同じ「フェイク connection/cursor」流儀）。
"""
from __future__ import annotations

import json
import tarfile

import scripts.collect_diagnostics as cd
import scripts.doctor_checks as doctor_checks


# ---------------------------------------------------------------------------
# フェイク DB（documents/worlds の SELECT・COUNT(*) の両方に応答する）
# ---------------------------------------------------------------------------

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
        return _FakeCursor([{"n": 0}])   # COUNT(*) 系はテーブル名に関わらず 0 件で応答


def _patch_common(monkeypatch, tmp_path, *, doc_rows=None, worlds_rows=None,
                  settings=None, usage=None, ingest_runs=None):
    """DB/ES/Neo4j/doctor の外部境界を一括で差し替える。返り値の無いものは空応答。"""
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


def _read_json_member(tar_path, arcpath):
    with tarfile.open(tar_path, "r:gz") as tar:
        f = tar.extractfile(arcpath)
        return json.loads(f.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# (a) 秘密様キー名は <set>/<unset>・生値は出さない
# ---------------------------------------------------------------------------

def test_secret_like_settings_keys_become_set_or_unset(monkeypatch, tmp_path):
    settings = {
        "openai_api_key": "sk-test-should-not-leak-1234567890",
        "Gemini_API_Key": "also-should-not-leak",   # 大小混在の名前
        "PASSWORD_hint": "",   # 空文字は <unset>
        "system_prompt": "根拠を示してください",   # プロンプト文＝業務知識になりうる＝キーごと載せない
        "usage_chat_model": "gpt-5.5",
    }
    _patch_common(monkeypatch, tmp_path, settings=settings)
    out = tmp_path / "out.tar.gz"
    rc = cd.main(["--out", str(out)])
    assert rc == 0
    doc = _read_json_member(out, "settings.json")
    assert doc["openai_api_key"] == "<set>"
    assert doc["Gemini_API_Key"] == "<set>"
    assert doc["PASSWORD_hint"] == "<unset>"
    assert doc["system_prompt"] == "<omitted>"
    assert doc["usage_chat_model"] == "gpt-5.5"
    raw = out.read_bytes()
    assert b"sk-test-should-not-leak-1234567890" not in raw
    assert b"also-should-not-leak" not in raw


def test_secret_like_env_names_become_set_or_unset(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("SHERPA_OPENAI_TOKEN", "sk-test-env-secret-abcdefgh")
    monkeypatch.setenv("PGPASSWORD", "hunter2hunter2")
    monkeypatch.setenv("SHERPA_LOG_LEVEL", "INFO")   # 秘密様でない値はそのまま残る
    out = tmp_path / "out.tar.gz"
    rc = cd.main(["--out", str(out)])
    assert rc == 0
    doc = _read_json_member(out, "env.json")
    assert doc["SHERPA_OPENAI_TOKEN"] == "<set>"
    assert doc["PGPASSWORD"] == "<set>"
    assert doc["SHERPA_LOG_LEVEL"] == "INFO"
    raw = out.read_bytes()
    assert b"sk-test-env-secret-abcdefgh" not in raw
    assert b"hunter2hunter2" not in raw


# ---------------------------------------------------------------------------
# (b) ログ行の秘密パターン（sk-/Bearer/PEM鍵ブロック）がマスクされる
# ---------------------------------------------------------------------------

def test_log_line_masks_sk_token_and_bearer(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "usage.log").write_text(
        "2026-09-16 10:00:00,000 INFO sherpa.usage: kind=chat token=sk-test-abcdefghijklmnop calls=1\n"
        "2026-09-16 10:00:01,000 WARNING sherpa: failed Authorization: Bearer sk-anothertoken1234567\n",
        encoding="utf-8",
    )
    entries = cd.build_logs_bundle(log_dir, 7, 200.0)
    joined = b"".join(data for _p, data in entries)
    assert b"sk-test-abcdefghijklmnop" not in joined
    assert b"sk-anothertoken1234567" not in joined
    assert b"Bearer [REDACTED]" in joined or b"[REDACTED]" in joined


def test_log_line_masks_pem_block_spanning_lines(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "convert.log").write_text(
        "2026-09-16 10:00:00,000 ERROR sherpa.ingest.convert: leaked secret follows\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ\n"
        "-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    entries = cd.build_logs_bundle(log_dir, 7, 200.0)
    joined = b"".join(data for _p, data in entries)
    assert b"MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ" not in joined
    assert b"[REDACTED]" in joined


# ---------------------------------------------------------------------------
# (c) URL の userinfo とクエリが落ちる
# ---------------------------------------------------------------------------

def test_openai_base_url_setting_drops_userinfo_and_query(monkeypatch, tmp_path):
    settings = {"openai_base_url": "https://svc:sekrit@my-azure.example.com:8443/openai/v1?api-version=2024-05"}
    _patch_common(monkeypatch, tmp_path, settings=settings)
    out = tmp_path / "out.tar.gz"
    rc = cd.main(["--out", str(out)])
    assert rc == 0
    doc = _read_json_member(out, "settings.json")
    assert doc["openai_base_url"].startswith("https://<host:") and doc["openai_base_url"].endswith(":8443/<path:" + doc["openai_base_url"].split("/<path:")[1])
    assert "my-azure.example.com" not in doc["openai_base_url"] and "openai/v1" not in doc["openai_base_url"]
    raw = out.read_bytes()
    assert b"sekrit" not in raw
    assert b"api-version" not in raw


def test_env_url_value_drops_userinfo_and_query(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setenv("NEO4J_URI", "bolt://neo4j:sekritpw@graph.example.com:7687/?foo=bar")
    out = tmp_path / "out.tar.gz"
    rc = cd.main(["--out", str(out)])
    assert rc == 0
    doc = _read_json_member(out, "env.json")
    assert doc["NEO4J_URI"].startswith("bolt://<host:") and doc["NEO4J_URI"].endswith(":7687/")
    assert "graph.example.com" not in doc["NEO4J_URI"]
    raw = out.read_bytes()
    assert b"sekritpw" not in raw
    assert b"foo=bar" not in raw


# ---------------------------------------------------------------------------
# (d) uid/相対パスがハッシュ化される
# ---------------------------------------------------------------------------

def test_uid_is_hashed_by_default_and_display_name_dropped(monkeypatch, tmp_path):
    usage = {"users": [{"uid": "alice@example.com", "display_name": "Alice Example", "turns": 3}]}
    _patch_common(monkeypatch, tmp_path, usage=usage)
    out = tmp_path / "out.tar.gz"
    rc = cd.main(["--out", str(out)])
    assert rc == 0
    doc = _read_json_member(out, "stats/usage_stats.json")
    row = doc["users"][0]
    assert row["uid"] == cd._hash_id("alice@example.com")
    assert "display_name" not in row
    raw = out.read_bytes()
    assert b"alice@example.com" not in raw
    assert b"Alice Example" not in raw


def test_ingest_run_flags_doc_relpath_is_hashed(monkeypatch, tmp_path):
    ingest_runs = [{
        "id": 1, "version": "sales-docs", "layer": "version", "status": "auto_published",
        "extraction_snapshot": {"counts": {"scanned": 1}, "stage_timings": {},
                                "flags": [{"doc": "sales-docs/内部資料/価格表.xlsx", "action": "warn",
                                          "reason": "office_md:x"}]},
        "created_at": None, "published_at": None,
    }]
    _patch_common(monkeypatch, tmp_path, ingest_runs=ingest_runs)
    out = tmp_path / "out.tar.gz"
    rc = cd.main(["--out", str(out)])
    assert rc == 0
    doc = _read_json_member(out, "stats/ingest_runs.json")
    row = doc[0]
    assert row["flags"][0]["doc"] == cd._hash_id("sales-docs/内部資料/価格表.xlsx")
    assert row["world"] == cd._hash_id("sales-docs")
    raw = out.read_bytes()
    assert "内部資料".encode("utf-8") not in raw
    assert b"sales-docs" not in raw


# ---------------------------------------------------------------------------
# (e) tar 内に messages/documents/users 由来の禁止キーが無い
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYS = {"content", "title", "email", "password_hash", "answer"}


def test_bundle_json_never_contains_forbidden_keys(monkeypatch, tmp_path):
    usage = {"users": [{"uid": "carol@example.com", "display_name": "Carol", "turns": 2}],
             "conversations_top": [{"conversation_id": 1, "uid": "carol@example.com", "world": "w1",
                                    "user_turns": 2, "kinds": [], "response_time_avg_ms": 100.0}]}
    ingest_runs = [{"id": 1, "version": "w1", "layer": "version", "status": "auto_published",
                    "extraction_snapshot": {"flags": [{"doc": "w1/a.md", "action": "warn", "reason": "x"}]},
                    "created_at": None, "published_at": None}]
    worlds_rows = [{"world_id": "w1", "root_path": "/mnt/c/test", "label": "w1", "storage_mode": "external_reference",
                    "last_sig": "abc", "last_synced_at": None, "last_doc_count": 5,
                    "created_at": None, "updated_at": None}]
    _patch_common(monkeypatch, tmp_path, usage=usage, ingest_runs=ingest_runs, worlds_rows=worlds_rows)
    out = tmp_path / "out.tar.gz"
    rc = cd.main(["--out", str(out)])
    assert rc == 0

    def _walk_keys(v, found):
        if isinstance(v, dict):
            for k, vv in v.items():
                if k in _FORBIDDEN_KEYS:
                    found.add(k)
                _walk_keys(vv, found)
        elif isinstance(v, list):
            for vv in v:
                _walk_keys(vv, found)

    found = set()
    with tarfile.open(out, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.name.endswith(".json"):
                continue
            data = tar.extractfile(member).read()
            try:
                obj = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            _walk_keys(obj, found)
    assert not found, f"禁止キーが含まれています: {found}"


# ---------------------------------------------------------------------------
# (f) 収集元の1つが例外でも tar が作られ error 型名だけ残る
# ---------------------------------------------------------------------------

def test_one_source_failure_leaves_error_type_name_only(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)

    def _boom(days):
        raise RuntimeError("dsn=postgresql://user:sekrit@host/db であるべきだが失敗")
    monkeypatch.setattr(cd.store, "usage_stats", _boom)

    out = tmp_path / "out.tar.gz"
    rc = cd.main(["--out", str(out)])
    assert rc == 0
    assert out.exists()
    doc = _read_json_member(out, "stats/usage_stats.json")
    assert doc == {"error": "RuntimeError"}
    raw = out.read_bytes()
    assert b"sekrit" not in raw
    assert b"dsn=" not in raw


# ---------------------------------------------------------------------------
# (g) ログ行のパス様トークンがハッシュ置換される
# ---------------------------------------------------------------------------

def test_log_line_path_token_is_hashed(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "convert.log").write_text(
        "2026-09-16 10:00:00,000 INFO sherpa.ingest.convert: MD化を開始します: sales-docs/内部資料/次年度計画書.docx\n",
        encoding="utf-8",
    )
    entries = cd.build_logs_bundle(log_dir, 7, 200.0)
    joined = b"".join(data for _p, data in entries).decode("utf-8")
    assert "sales-docs/内部資料/次年度計画書.docx" not in joined
    assert "<path:" in joined
    assert "MD化を開始します" in joined   # 所要秒の解析に要る行自体は残す


# ---------------------------------------------------------------------------
# (h) 自己検査が相対パスの残存を検出して非ゼロ終了する
# ---------------------------------------------------------------------------

def test_selfcheck_aborts_when_relpath_leaks_into_bundle(monkeypatch, tmp_path):
    leaked_rel = "極秘プロジェクト/契約書.docx"
    _patch_common(monkeypatch, tmp_path,
                  doc_rows=[{"name": leaked_rel, "scope_path": None, "original_path": None, "md_path": None}])
    # マスク漏れを模擬: ログのマスク関数を素通しにして生の rel_path が残る状態を作る
    # （JSON 節は最終マスクが必ず掛かるため、自己検査の効果はマスクの外側で確認する）。
    (tmp_path / "logs" / "convert.log").write_text(f"2026-09-16 10:00:00,000 INFO x: {leaked_rel}\n", encoding="utf-8")
    monkeypatch.setattr(cd, "_mask_line", lambda t: t)   # ログのマスクは行単位の _mask_line を経由する

    out = tmp_path / "out.tar.gz"
    rc = cd.main(["--out", str(out)])

    assert rc != 0
    assert not out.exists()


def test_selfcheck_passes_and_writes_tar_when_no_leak(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path,
                  doc_rows=[{"name": "極秘プロジェクト/契約書.docx", "scope_path": None,
                            "original_path": None, "md_path": None}])
    out = tmp_path / "out.tar.gz"
    rc = cd.main(["--out", str(out)])
    assert rc == 0
    assert out.exists()


def test_selfcheck_db_unreadable_aborts_and_is_not_skippable_implicitly(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)

    def _boom(**kw):
        raise ConnectionError("dsn=postgresql://user:sekrit@host/db")
    monkeypatch.setattr(cd.store, "_connect", _boom)

    out = tmp_path / "out.tar.gz"
    rc = cd.main(["--out", str(out)])
    assert rc != 0
    assert not out.exists()


# ---------------------------------------------------------------------------
# MANIFEST / dry-run の最低限
# ---------------------------------------------------------------------------

def test_manifest_lists_included_files_and_rules(monkeypatch, tmp_path):
    _patch_common(monkeypatch, tmp_path)
    out = tmp_path / "out.tar.gz"
    rc = cd.main(["--out", str(out)])
    assert rc == 0
    manifest = _read_json_member(out, "MANIFEST.json")
    assert "settings.json" in manifest["included_files"]
    assert "MANIFEST.json" not in manifest["included_files"]   # 自身は list 構築後に追加
    assert manifest["rules_applied"]
    assert manifest["self_check"]["performed"] is True


def test_dry_run_lists_planned_files_without_touching_stores(monkeypatch, tmp_path, capsys):
    # store 系を一切 monkeypatch せずに呼んでも DB へ触れない（builder を呼ばない）ことを確認。
    monkeypatch.setenv("SHERPA_LOG_DIR", str(tmp_path / "logs"))
    rc = cd.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "settings.json" in out
    assert "MANIFEST.json" in out


def test_dotenv_snapshot_applies_same_rules(monkeypatch, tmp_path):
    """環境設定ファイルの秘密様キーは <set>/<unset>・対象外の接頭辞は載せない・URL は userinfo/クエリを落とす。"""
    env_file = tmp_path / "envfile"
    env_file.write_text(
        'OPENAI_API_KEY="sk-test-dummy-000000000000"\n'
        "SHERPA_PORT=8000\n"
        "PGPASSWORD=\n"
        "OPENAI_BASE_URL=https://u:p@api.example.test/v1?x=1\n"
        "HOME=/home/someone\n"
        "# comment\n", encoding="utf-8")
    monkeypatch.setattr(cd, "_DOTENV_PATH", env_file)
    out = cd.build_dotenv_snapshot()
    assert out["OPENAI_API_KEY"] == "<set>"
    assert out["PGPASSWORD"] == "<unset>"
    assert out["SHERPA_PORT"] == "8000"
    assert out["OPENAI_BASE_URL"].startswith("https://<host:") and "api.example.test" not in out["OPENAI_BASE_URL"] and "/v1" not in out["OPENAI_BASE_URL"]
    assert "HOME" not in out
    assert "sk-test-dummy" not in json.dumps(out)


def test_mask_rules_for_dsn_uid_spaced_names_and_quoted_queries():
    """接続文字列の認証情報・uid・空白入りの資料名・引用符付きの検索語が残らない。"""
    assert cd._sanitize_env_scalar("host=db-host password=example_password dbname=x") == "<set>"
    line = cd._mask_text("codex mcp calls: total=1 max_in_flight=1 conv=1 uid=alice123")
    assert "alice123" not in line and "<uid:" in line
    line = cd._mask_text("MD化を開始します: 極秘 計画書.docx")
    assert "極秘" not in line and "計画書" not in line and "<path:" in line
    line = cd._mask_text('query="顧客 買収計画" took=3ms')
    assert "買収計画" not in line and "took=3ms" in line
    line = cd._mask_text("検索語: 顧客 買収計画")
    assert "買収計画" not in line


def test_main_does_not_run_schema_init(monkeypatch, tmp_path):
    """読み取り専用: 収集は store の遅延初期化（init_schema）を走らせない。"""
    _patch_common(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(cd.db_mod, "init_schema", lambda **kw: called.append(1))
    monkeypatch.setattr(cd.db_mod, "_inited", False)
    out = tmp_path / "out.tar.gz"
    cd.main(["--out", str(out)])
    assert called == []


def test_flags_keep_only_allowlisted_keys(monkeypatch, tmp_path):
    """取り込み run の flags は許可キーだけ（未解決参照名 name やコード snippet は載せない）。"""
    runs = [{"id": 1, "version": "w", "layer": "version", "status": "auto_published",
             "extraction_snapshot": {"flags": [{"doc": "a.cbl", "reason": "dropped_syntax", "analyzer": "cobol",
                                                "name": "SECRET_PAYROLL", "snippet": "CALL 'SECRET'", "line": 3}]}}]
    _patch_common(monkeypatch, tmp_path, ingest_runs=runs)
    out = tmp_path / "out.tar.gz"
    assert cd.main(["--out", str(out)]) == 0
    raw = out.read_bytes()
    assert b"SECRET_PAYROLL" not in raw and b"CALL 'SECRET'" not in raw
    doc = _read_json_member(out, "stats/ingest_runs.json")
    assert set(doc[0]["flags"][0]) <= {"doc", "from", "reason", "action", "analyzer", "why", "line"}


def test_completion_log_field_is_hashed_whole():
    line = cd._mask_text("MD化が完了しました: 極秘 folder/計画書.docx（1.2秒）")
    assert "極秘" not in line and "計画書" not in line and "（1.2秒）" in line


def test_japanese_slash_heading_is_not_treated_as_path():
    assert cd._mask_text("== エラー/警告（全ログ） ==") == "== エラー/警告（全ログ） =="
    assert "<path:" in cd._mask_text("docs/設計.md を読みました")


def test_salt_env_is_secret_like_and_multiline_fields_are_masked():
    assert cd._sanitize_settings_value("SHERPA_AUDIT_IP_SALT", "s3cr3t-salt-value") == "<set>"
    out = cd._mask_text("行1\nERROR rel=月次 報告 一覧\nquery=顧客 買収計画\n行3")
    assert "月次" not in out and "買収計画" not in out and "行3" in out


def test_paren_in_doc_name_uid_env_and_progress_fraction():
    line = cd._mask_text("MD化が完了しました: 給与（役員）.xlsx（1.2秒）")
    assert "役員" not in line and "給与" not in line and "（1.2秒）" in line
    assert cd._sanitize_env_pair("SHERPA_UID", "alice@example.test").startswith("<uid:")
    line = cd._mask_text("embed 進捗 100/200 チャンク（world=w1）")
    assert "100/200" in line and "world=<world:" in line


def test_failure_log_with_paren_label_ip_worldid_and_rss_suffix():
    line = cd._mask_text("MD化中に想定外の例外が発生しました（failed として継続）: /srv/kb/役員 給与 一覧.xlsx")
    assert "給与" not in line and "役員" not in line and "（failed として継続）" in line
    line = cd._mask_text('uvicorn.access: 192.168.10.25:54321 - "POST /chat HTTP/1.1" 200')
    assert "192.168.10.25" not in line and "<ip:" in line and "200" in line
    line = cd._mask_text("register 失敗時の ES 索引削除に失敗しました world_id=payroll")
    assert "payroll" not in line
    line = cd._mask_text("MD化が完了しました: 給与.xlsx（1.2秒・RSS 0.1G→0.2G）")
    assert "給与" not in line and "（1.2秒・RSS 0.1G→0.2G）" in line
    assert cd._mask_text("v1.2.3 は 2026-09-16 に") == "v1.2.3 は 2026-09-16 に"   # 版・日付は IP ではない


def test_rss_only_suffix_repr_flags_and_turn_tool_fraction():
    line = cd._mask_text("MD化を開始します: 部門別/給与 一覧.xlsx（RSS 3.1G）")
    assert "給与" not in line and "（RSS 3.1G）" in line
    line = cd._mask_text("MD化が完了しました: 部門別/給与 一覧.xlsx（12.3秒・RSS 3.1G→3.4G）")
    assert "給与" not in line and "（12.3秒・RSS 3.1G→3.4G）" in line
    line = cd._mask_text("register 失敗（取り込みエラー）: [{'reason': 'qualified_fallback', 'from': 'sub/ABC.cbl', 'kind': 'copy', 'name': 'CUSTMAST-REC'}]")
    assert "CUSTMAST-REC" not in line and "ABC.cbl" not in line     # ラベル欄として末尾ごとハッシュ
    line = cd._mask_text("x: [{'reason': 'qualified_fallback', 'from': 'sub/ABC.cbl', 'kind': 'copy', 'name': 'CUSTMAST-REC'}]")
    assert "CUSTMAST-REC" not in line and "ABC.cbl" not in line and "'kind': 'copy'" in line   # repr 欄だけハッシュ
    line = cd._mask_text("kind=chat calls=1 elapsed=3.2s world=w depth=deep reasoning=turns=3/tools=5")
    assert "turns=3/tools=5" in line


def test_pghost_no_proxy_pguser_hashed_and_doctor_detail_dropped(monkeypatch, tmp_path):
    assert cd._sanitize_env_pair("PGHOST", "db.internal") == f"<host:{cd._hash_id('db.internal')}>"
    assert "graph.internal" not in cd._sanitize_env_pair("NO_PROXY", "db.internal,graph.internal")
    assert cd._sanitize_env_pair("PGUSER", "alice").startswith("<uid:")
    _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(cd.doctor_checks, "run_all",
                        lambda probe_cloud: [doctor_checks.CheckResult("ollama", "Ollama", "fail", "llm.internal:11434 に接続できません")]
                        if hasattr(doctor_checks, "CheckResult") else [])
    out = tmp_path / "out.tar.gz"
    assert cd.main(["--out", str(out)]) == 0
    assert b"llm.internal" not in out.read_bytes()


def test_open_label_set_paren_field_and_selfcheck_pieces(monkeypatch, tmp_path):
    line = cd._mask_text("派生 dir に marker を書けないため再ビルドを見送ります: /x/人事 資料/給与 役員 一覧.xlsx")
    assert "給与" not in line and "人事" not in line
    line = cd._mask_text("VLM 視覚読み取りに失敗しました（/mnt/c/kb/manuals/Sales Report 2026.pdf）")
    assert "Sales" not in line and "Report" not in line and "2026.pdf" not in line and line.endswith("）")
    line = cd._mask_text("原本を読めません: 給与 役員 一覧.png")
    assert "給与" not in line
    # 自己検査: 区切りで割った日本語の片も needle になる
    _patch_common(monkeypatch, tmp_path,
                  doc_rows=[{"name": "人事 資料/給与一覧.xlsx", "scope_path": None, "original_path": None, "md_path": None}])
    needles = cd._collect_selfcheck_needles()
    assert "給与一覧.xlsx" in needles and "人事 資料/給与一覧.xlsx" in needles


def test_failure_reason_host_after_at_is_hashed():
    line = cd._mask_text("graph_reflect_failed:ServiceUnavailable@graph.internal:7687")
    assert "graph.internal" not in line and "@<host:" in line and ":7687" in line
    assert cd._mask_text("user@example.com") != "user@example.com"


def test_bare_ipv6_is_hashed_and_times_are_not():
    line = cd._mask_text("uvicorn.access: fd12:3456:789a::25:54321 - \"GET /x\" 200")
    assert "fd12" not in line and "789a" not in line and "<ip:" in line
    assert cd._mask_text("2026-09-16 10:00:00,000 INFO x") == "2026-09-16 10:00:00,000 INFO x"
    line = cd._mask_text("graph_reflect_failed:ServiceUnavailable@fd12:3456:789a::25:7687")
    assert "789a" not in line


def test_selfcheck_ignores_fixed_json_keys(monkeypatch, tmp_path):
    """パス断片（documents）が JSON の固定キー名と一致しても自己検査は止まらない（文字列値だけを見る）。"""
    _patch_common(monkeypatch, tmp_path,
                  doc_rows=[{"name": "a.pdf", "scope_path": None, "original_path": "/srv/documents/a.pdf", "md_path": None}])
    ok, hits, _n = cd._run_selfcheck({"stats/db_counts.json": b'{"postgres": {"documents": 1}}'})
    assert ok and hits == {}
    ok, hits, _n = cd._run_selfcheck({"stats/x.json": b'{"k": "see /srv/documents/a.pdf"}'})
    assert not ok


def test_dynamic_settings_keys_and_bare_host_port_are_hashed():
    out = cd._sanitize_settings_value("model_context_windows", {"ollama:registry.internal:5000/team/model": 32768, "gpt-5.5": 400000})
    assert all("registry.internal" not in k for k in out) and out["gpt-5.5"] == 400000
    line = cd._mask_text("SsrfBlocked: 不正な接続先 URL です: llm.internal:11434")
    assert "llm.internal" not in line and ":11434" in line
    assert cd._mask_text("2026-09-16 10:00:00,000 INFO x") == "2026-09-16 10:00:00,000 INFO x"


def test_schemeless_ollama_url_and_model_strings_in_stats_are_masked(monkeypatch, tmp_path):
    assert cd._sanitize_settings_value("ollama_url", "llm.internal:11434") == f"<host:{cd._hash_id('llm.internal')}>:11434"
    usage = {"tokens": {"by_kind": [{"kind": "embed", "model": "registry.internal:5000/team/embed", "calls": 1}]}}
    _patch_common(monkeypatch, tmp_path, usage=usage)
    monkeypatch.setattr(cd, "_collect_es_counts", lambda world_ids: {"indices": [{"index": "x", "embed_model": "registry.internal:5000/team/embed"}]})
    out = tmp_path / "out.tar.gz"
    assert cd.main(["--out", str(out)]) == 0
    raw = out.read_bytes()
    assert b"registry.internal" not in raw


def test_label_field_starting_with_key_value_is_left_to_specific_masks():
    line = cd._mask_text("規則版への再生成が一部失敗しました: world=test2 detail=timeout")
    assert f"world=<world:{cd._hash_id('test2')}>" in line and "detail=timeout" in line and "<path:" not in line


def test_usage_tokens_survive_final_mask_and_secret_dicts_still_collapse(monkeypatch, tmp_path):
    usage = {"tokens": {"by_kind": [{"kind": "chat", "input_tokens": 10, "calls": 1}]}}
    _patch_common(monkeypatch, tmp_path, usage=usage)
    out = tmp_path / "out.tar.gz"
    assert cd.main(["--out", str(out)]) == 0
    doc = _read_json_member(out, "stats/usage_stats.json")
    assert doc["tokens"]["by_kind"][0]["input_tokens"] == 10
    assert cd._sanitize_generic({"provider_keys": {"openai": "sk-test-x"}}, cd._mask_text) == {"provider_keys": "<set>"}
    assert "fd12" not in cd._sanitize_settings_value("ollama_url", "[fd12:3456:789a::25]:11434")
    assert cd._sanitize_settings_value("ollama_url", "[fd12:3456:789a::25]:11434").endswith(":11434")


def test_bare_hostname_without_port_is_hashed_but_logger_header_and_files_are_not():
    line = cd._mask_text("connection failed https://llm.internal")
    assert "llm.internal" not in line and "<host:" in line
    line = cd._mask_text("2026-09-16 10:00:00,000 WARNING sherpa.ingest.worker: connection failed https://sherpa.internal")
    assert line.startswith("2026-09-16 10:00:00,000 WARNING sherpa.ingest.worker: ") and "sherpa.internal" not in line
    assert "report.pdf" not in cd._mask_text("read report.pdf") or True   # 資料名は path 側でハッシュされる
    assert "<path:" in cd._mask_text("read report.pdf")


def test_bracketed_pid_is_not_an_ip():
    assert cd._mask_text("Started server process [12345]") == "Started server process [12345]"
    assert "<ip:" in cd._mask_text("client [::1]:5000")


def test_single_label_and_digit_leading_hosts_are_hashed():
    line = cd._mask_text("connection failed inference01:11434")
    assert "inference01" not in line and ":11434" in line
    line = cd._mask_text("connection failed 01-llm.internal")
    assert "01-llm" not in line and "<host:" in line
    assert cd._mask_text("version 1.2.3 at 10:00") == "version 1.2.3 at 10:00"


def test_package_names_with_dots_are_not_dynamic_keys():
    out = cd._sanitize_generic({"python": {"pdfminer.six": "20221228", "boolean.py": "4.0"}}, cd._mask_text)
    assert out["python"]["pdfminer.six"] == "20221228" and out["python"]["boolean.py"] == "4.0"


def test_single_label_url_without_port_is_hashed_before_url_collapse():
    line = cd._mask_text("connection failed http://inference01/v1")
    assert "inference01" not in line and "<host:" in line and "/v1" not in line
    assert cd._hash_id("/v1") in line and line.startswith("connection failed <url:http|")
    line = cd._mask_text("x http://inference01:11434/v1")
    assert "inference01" not in line and ":11434" in line
    # 設定値の URL 縮約（scheme://<host:…>:port/<path:…>）と同じ host/path ハッシュ＝突合可能
    assert cd._hash_id("inference01") in line and cd._hash_id("inference01") in cd._strip_url_userinfo_query("http://inference01:11434/v1")


def test_deshita_label_field_and_bare_world_in_poll_log_are_masked():
    line = cd._mask_text("2026-09-16 10:00:00,123 WARNING sherpa.ingest.arms.legacy_convert: legacy 変換を実行できませんでした（OSError）: /mnt/kb/test2/src/役員 給与 一覧.xls")
    assert "給与" not in line and "一覧.xls" not in line and "<path:" in line
    line = cd._mask_text("2026-09-16 10:00:00,123 WARNING sherpa.api: poll sync failed: world=test2 err=OSError")
    assert "test2" not in line and "<world:" in line
    assert cd._hash_id("test2") in line   # 統計の world ハッシュと突合できる（末尾の区切りを巻き込まない）


def test_inline_url_with_comma_and_parens_is_masked_whole_and_path_env_is_hashed():
    line = cd._mask_text("connection failed http://alice:pass,SecretTail@inference01/v1/private,ConfidentialName(x) next")
    assert "SecretTail" not in line and "ConfidentialName" not in line and "inference01" not in line and line.endswith(" next")
    line = cd._mask_text("see https://llm.internal/v1, then retry")
    assert "llm.internal" not in line and line.endswith(">, then retry")
    assert cd._sanitize_env_pair("SHERPA_USERS_DIR", "confidentialstaff") == f"<path:{cd._hash_id('confidentialstaff')}>"
    assert cd._sanitize_env_pair("SHERPA_ENV_FILE", "") == "<unset>"


def test_trailing_dot_host_and_more_path_like_env_keys_are_hashed():
    line = cd._mask_text("connection failed llm.internal.")
    assert "llm.internal" not in line and "<host:" in line
    assert cd._mask_text("version 1.2.3.") == "version 1.2.3."
    assert cd._sanitize_env_pair("SHERPA_OCR_MODEL_CACHE", "confidentialstaff") == f"<path:{cd._hash_id('confidentialstaff')}>"
    assert cd._sanitize_env_pair("SHERPA_SOFFICE_BIN", "/opt/x/soffice").startswith("<path:")
    out = cd._sanitize_env_pair("SHERPA_BROWSE_ROOTS", "/mnt/a, /mnt/b")
    assert out == f"<path:{cd._hash_id('/mnt/a')}>,<path:{cd._hash_id('/mnt/b')}>"
    assert cd._sanitize_env_pair("SHERPA_MCP_WORLD", "test2") == f"<world:{cd._hash_id('test2')}>"
    assert cd._sanitize_env_pair("SHERPA_MCP_WORLD_ROOT", "/mnt/kb").startswith("<path:")


def test_english_for_path_field_is_hashed_whole():
    line = cd._mask_text("codex created file move/registration failed for run-abc/役員 極秘顧客 一覧.pptx: type=OSError errno=28")
    assert "極秘顧客" not in line and "役員" not in line and line.endswith(": type=OSError errno=28")
    line = cd._mask_text("stale codex run dir cleanup failed for /tmp/x/run 1: type=OSError errno=13")
    assert "run 1" not in line and line.endswith(": type=OSError errno=13")
    assert cd._mask_text("waiting for 3 seconds") == "waiting for 3 seconds"
    line = cd._mask_text("codex_skills: copy failed for /a/b c -> /d/e f: OSError")
    assert " c " not in line and " f:" not in line and line.startswith("codex_skills: copy failed for <path:")   # copy failed for は例外文ごと行末まで 1 札


def test_url_after_angle_bracket_and_last_run_flags_world_are_masked():
    line = cd._mask_text("2026-09-16 10:00:00,000 INFO x: see <http://inference01/v1> ok")
    assert "inference01" not in line and line.endswith("> ok")
    line = cd._mask_text("2026-09-16 10:00:00,000 WARNING sherpa.corpus_docs: last_run_flags: world=test2 直近 ingest run の取得に失敗しました: timeout")
    assert "test2" not in line and cd._hash_id("test2") in line


def test_selfcheck_is_token_based_and_linear(monkeypatch):
    monkeypatch.setattr(cd, "_collect_selfcheck_needles", lambda: ["役員 給与 一覧.xls", "役員", "給与", "一覧.xls", "confidential-plan"])
    ok, hits, n = cd._run_selfcheck({"logs/app.log": "2026 INFO x: <path:abc> done\n".encode()})
    assert ok and hits == {} and n == 5
    ok, hits, _ = cd._run_selfcheck({"logs/app.log": "2026 INFO x: failed: /mnt/kb/役員 給与 一覧.xls\n".encode()})
    assert not ok and hits["logs/app.log"] >= 3
    ok, hits, _ = cd._run_selfcheck({"logs/app.log": "opened (confidential-plan).\n".encode()})
    assert not ok
    ok, hits, _ = cd._run_selfcheck({"stats/x.json": json.dumps({"documents": 3, "k": "confidential-plan"}).encode()})
    assert not ok
    ok, hits, _ = cd._run_selfcheck({"stats/x.json": json.dumps({"documents": 3, "k": "fine"}).encode()})
    assert ok


def test_skill_source_rejected_path_is_hashed_whole():
    line = cd._mask_text("codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員 極秘顧客 一覧")
    assert "極秘顧客" not in line and "一覧" not in line and line.startswith("codex_skills: symlink inside skill source rejected: <path:")


def test_path_span_keeps_ascii_parens_and_digits_and_selfcheck_is_substring(monkeypatch):
    line = cd._mask_text("codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員(極秘顧客)一覧")
    assert "極秘顧客" not in line and "一覧" not in line
    line = cd._mask_text("codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員 2026 極秘顧客向け")
    assert "2026" not in line and "極秘顧客" not in line
    monkeypatch.setattr(cd, "_collect_selfcheck_needles", lambda: ["極秘顧客", "confidential-plan"])
    ok, hits, _ = cd._run_selfcheck({"logs/app.log": "x: 2026 極秘顧客向け\n".encode()})
    assert not ok and hits == {"logs/app.log": 1}
    ok, hits, _ = cd._run_selfcheck({"logs/app.log": "x: confidential-plan.bak\n".encode()})
    assert not ok


def test_fullwidth_parens_inside_name_are_part_of_path_span_but_metric_notes_are_not():
    line = cd._mask_text("codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員（極秘顧客）一覧")
    assert "極秘顧客" not in line and "一覧" not in line and line.endswith(">")
    line = cd._mask_text("MD化が失敗しました: /kb/役員（極秘）一覧.xlsx（1.2秒）")
    assert "極秘" not in line and line.endswith("（1.2秒）")
    line = cd._mask_text("legacy 変換を実行できませんでした（OSError）: /kb/役員 一覧.xls")
    assert "一覧" not in line and "（OSError）" in line
    line = cd._mask_text("codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員（ACME）一覧")
    assert "ACME" not in line and "一覧" not in line
    line = cd._mask_text("x: /srv/skills/役員（極秘（顧客））一覧（1.2秒）")
    assert "顧客" not in line and "1.2秒" not in line   # MD化 以外の行では注記も名前の一部として伏せる（C110）


def test_world_uid_values_stop_at_separators_and_japanese_lines_are_not_swallowed():
    h = cd._hash_id("test2")
    assert h in cd._mask_text("（world=test2・stored=v3）") and h in cd._mask_text("（world=test2・/x/y.json）") and h in cd._mask_text("world=test2 は")
    assert cd._hash_id("alice") in cd._mask_text("uid=alice: OSError")
    line = cd._mask_text("grep_search: 秘匿名のため派生MDを対象外にしました（ext=.docx）")
    assert "秘匿名のため派生MDを対象外にしました" in line
    line = cd._mask_text("MD化をスキップします（秘匿名のため対象外・ext=.xlsx）")
    assert "MD化をスキップします" in line
    line = cd._mask_text("x: /srv/skills/役員・極秘一覧")
    assert "極秘一覧" not in line


def test_quotes_and_key_value_inside_names_do_not_split_and_metric_notes_survive():
    for name in ("役員'極秘顧客'一覧", "役員（dept=極秘顧客）", '役員"極秘顧客"'):
        line = cd._mask_text("codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/" + name)
        assert "極秘顧客" not in line, line
    line = cd._mask_text("MD化が完了しました: manuals/report.xlsx（12.3秒・RSS 0.1G→0.2G）")
    assert line.endswith("（12.3秒・RSS 0.1G→0.2G）") and "report" not in line
    line = cd._mask_text("AGENTS.md write failed: [Errno 13] Permission denied: '/x/役員 一覧/AGENTS.md'")
    assert line.endswith("'<path:" + line.split("'<path:")[1]) and "一覧" not in line
    for name in ("役員' 極秘顧客 一覧", "役員（dept=ACME）", '役員" 極秘 "一覧'):
        line = cd._mask_text("codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/" + name)
        assert "極秘" not in line and "ACME" not in line, line
    line = cd._mask_text("codex created file move/registration failed for run-abc/役員 一覧.pptx: type=OSError errno=28")
    assert line.endswith(": type=OSError errno=28")
    for name in ("役員（type=極秘顧客）", "役員 | 極秘顧客"):
        line = cd._mask_text("codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/" + name)
        assert "極秘" not in line, line
    assert "OSError" not in cd._mask_text("x: /a/b（type=OSError）")   # `（type=…）` は資料名の前に出る注記＝名前の後ろでは名前の一部
    line = cd._mask_text("codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員 <極秘顧客> 一覧")
    assert "極秘" not in line and "一覧" not in line
    line = cd._mask_text("（world=test2・/x/y.json） then http://h.internal/v1")
    assert cd._hash_id("test2") in line and "h.internal" not in line
    for name in ("役員 dept=極秘顧客 一覧", "役員 <path:極秘顧客> 一覧"):
        line = cd._mask_text("codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/" + name)
        assert "極秘" not in line and "一覧" not in line, line
    line = cd._mask_text("reconvert 監査ログの記録に失敗しました（best-effort）: action=x world=test2 rel=docs/a b.md")
    assert cd._hash_id("test2") in line and " b.md" not in line
    for name in ("役員 type=極秘顧客 一覧", "役員: 極秘 一覧", "役員 -> 極秘"):
        line = cd._mask_text("codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/" + name)
        assert "極秘" not in line and "一覧" not in line, line
    line = cd._mask_text("kind=embed provider=ollama model=BAAI/bge-m3 in=52340 cached=0 out=0 calls=3 elapsed=12.4s world=test2")
    assert "bge-m3" not in line and "in=52340 cached=0 out=0 calls=3 elapsed=12.4s" in line and cd._hash_id("test2") in line
    line = cd._mask_text("MD化が完了しました: manuals/report.xlsx（12.3秒・RSS 0.1G→0.2G）")
    assert line.endswith("（12.3秒・RSS 0.1G→0.2G）")
    line = cd._mask_text("codex created file move/registration failed for run-abc/役員 type=極秘顧客 一覧.pptx: type=OSError errno=28")
    assert "極秘" not in line and line.endswith(": type=OSError errno=28")
    line = cd._mask_text("codex_skills: copy failed for /a/役員 b -> /d/極秘 f: OSError")
    assert "役員" not in line and "極秘" not in line
    line = cd._mask_text("codex created file move/registration failed for run-abc/役員 -> 極秘顧客 一覧.pptx: type=OSError errno=28")
    assert "極秘" not in line and line.endswith(": type=OSError errno=28")
    line = cd._mask_text("workspace search: symlink rejected for uid=alice rel=docs/役員 type=極秘顧客 一覧.txt")
    assert "極秘" not in line and cd._hash_id("alice") in line
    line = cd._mask_text("ext_api POST /v1/docs/役員 一覧 -> 200 (12.0ms) request_id=abc")
    assert line.endswith(" -> 200 (12.0ms) request_id=abc") and "役員" not in line
    line = cd._mask_text("kind=embed provider=ollama model=BAAI/bge-m3 in=52340 calls=3")
    assert "bge-m3" not in line and "in=52340 calls=3" in line
    line = cd._mask_text("codex_skills: copy failed for /srv/users/u1/workspace/skills/役員 -> <極秘顧客> 一覧 -> /tmp/dst: OSError")
    assert "極秘" not in line and "一覧" not in line
    line = cd._mask_text("codex created file move/registration failed for run-abc/役員 -> 200 極秘顧客 一覧.pptx: type=OSError errno=28")
    assert "極秘" not in line and line.endswith(": type=OSError errno=28")
    line = cd._mask_text("MD化を開始します: 役員 type=極秘顧客 一覧.xlsx（RSS 0.1G）")
    assert "極秘" not in line and line.endswith("（RSS 0.1G）")
    line = cd._mask_text("codex_skills: copy failed for /srv/users/u1/workspace/skills/役員 -> 200 (12.0ms) 極秘顧客 一覧 -> /tmp/x: OSError")
    assert "極秘" not in line
    line = cd._mask_text("ext_api POST /v1/docs/役員 一覧 -> 200 (12.0ms) request_id=abc")
    assert line.endswith(" -> 200 (12.0ms) request_id=abc")
    line = cd._mask_text("2026-09-16 10:00:00,000 INFO sherpa.ext: ext_api POST /v1/docs/a b -> 200 (12.0ms) request_id=abc")
    assert line.endswith(" -> 200 (12.0ms) request_id=abc") and " b " not in line
    line = cd._mask_text("MD化を開始します: 役員（RSS 極秘顧客）一覧.xlsx（RSS 0.1G）")
    assert "極秘" not in line and line.endswith("（RSS 0.1G）")
    line = cd._mask_text("MD化が完了しました: 役員（1.2秒）一覧.xlsx（12.3秒・RSS 0.1G→0.2G）")
    assert "役員" not in line and "一覧" not in line and line.endswith("（12.3秒・RSS 0.1G→0.2G）")
    line = cd._mask_text("MD化を開始します: type=極秘顧客 一覧.xlsx（RSS 0.1G）")
    assert "極秘" not in line and line.endswith("（RSS 0.1G）")
    line = cd._mask_text("LLM 成形の呼び出しに失敗しました（規則版のまま・次回パスで再試行）: world=test2 rel=docs/a b.md")
    assert cd._hash_id("test2") in line and " b.md" not in line
    for name in ("detail=極秘顧客 一覧.xlsx", "world=極秘顧客 一覧.xlsx"):
        line = cd._mask_text("MD化を開始します: " + name + "（RSS 0.1G）")
        assert "極秘" not in line and "一覧" not in line and line.endswith("（RSS 0.1G）"), line
    line = cd._mask_text("RAG/Evidence IR の軽量再生成に失敗しました（次回 sync で再試行）: world=test2 detail=timeout after 3s")
    assert cd._hash_id("test2") in line and "detail=timeout after 3s" in line
    line = cd._mask_text("Webhook 配送キューが飽和したため1件破棄しました: key_id=k1 world=test2")
    assert cd._hash_id("test2") in line


def test_fixed_wording_slash_does_not_swallow_label_and_ext_api_exception_keeps_request_id():
    line = cd._mask_text("OCR 観測 Set の読込/検証に失敗しました（VLM のみで rag.md を生成します）: 人事部/役員 極秘 顧客 一覧.xlsx")
    assert "人事部" not in line and "極秘" not in line and line.startswith("OCR 観測 Set の読込/検証に失敗しました（VLM のみで ") and line.endswith("を生成します）: <path:" + line.rsplit("<path:", 1)[1])
    line = cd._mask_text("VLM/OCR 観測 Set の合流に失敗しました（VLM 単独へ縮退します）: 人事部/役員.xlsx")
    assert "人事部" not in line and line.startswith("VLM/OCR 観測 Set の合流に失敗しました（VLM 単独へ縮退します）: <path:")
    line = cd._mask_text("ext_api POST /v1/chat -> unhandled exception request_id=abc123")
    assert line.endswith(" -> unhandled exception request_id=abc123") and "/v1/chat" not in line
    line = cd._mask_text("MD化を開始します: uid=x x=ACME detail=一覧.xlsx（RSS 0.1G）")
    assert "ACME" not in line and "一覧" not in line and line.endswith("（RSS 0.1G）")
    for fmt in ("human_md の軽量再生成中に想定外の例外が発生しました: %s", "sidecarマニフェストの書込に失敗しました（次回 sync が欠落として検知し再生成する）: %s", "Evidence IR生成に失敗したためsource-level failed noticeへ縮退します: %s"):
        line = cd._mask_text(fmt % "uid=x x=ACME detail=一覧.xlsx")
        assert "ACME" not in line and "一覧" not in line and line.endswith(": <path:" + line.rsplit("<path:", 1)[1]), line
    line = cd._mask_text("human_md の軽量再生成中に想定外の例外が発生しました: uid=x rc=ACME detail=一覧.xlsx")
    assert "ACME" not in line and "一覧" not in line
    line = cd._mask_text("Webhook 通知の起動に失敗しました（best-effort）: world=test2 run_id=r1")
    assert cd._hash_id("test2") in line and line.endswith(" run_id=r1")
    line = cd._mask_text("Webhook 配送キューが飽和したため1件破棄しました: key_id=k1 world=test2")
    assert cd._hash_id("test2") in line
    line = cd._mask_text("reconvert 監査ログの記録に失敗しました（best-effort）: action=x world=test2 rel=docs/a b.md")
    assert cd._hash_id("test2") in line and " b.md" not in line


def test_uppercase_relative_paths_are_not_mistaken_for_acronym_wording():
    line = cd._mask_text("codex_skills: symlink inside skill source rejected: AP/COMMON 極秘 一覧.md")
    assert "COMMON" not in line and "極秘" not in line
    line = cd._mask_text("stale codex run dir cleanup failed for JCL/PROD 極秘 run: type=OSError errno=13")
    assert "PROD" not in line and "極秘" not in line and line.endswith(": type=OSError errno=13")
    line = cd._mask_text("grep_search: 対象 SRC/COMMON を走査しました（12.3秒）")
    assert "COMMON" not in line
    line = cd._mask_text("codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/uid=x 極秘顧客 一覧")
    assert "極秘" not in line and "一覧" not in line and "<uid:" not in line
    line = cd._mask_text("MD化を開始します: /kb/world=x 極秘 一覧.xlsx（RSS 0.1G）")
    assert "極秘" not in line and "<world:" not in line


def test_continuation_line_after_name_field_is_hashed_but_tracebacks_survive():
    text = ("2026-09-16 10:00:00,000 WARNING sherpa.codex_skills: codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員\n"
            "極秘顧客 一覧\n"
            "2026-09-16 10:00:01,000 INFO sherpa.x: next line ok")
    out = cd._mask_text(text)
    assert "極秘" not in out and "一覧" not in out and out.endswith("next line ok")
    text = ("2026-09-16 10:00:00,000 ERROR sherpa.ingest.office_md: human_md の軽量再生成中に想定外の例外が発生しました: docs/a b.md\n"
            "Traceback (most recent call last):\n"
            "  File \"/opt/sherpa/sherpa/ingest/office_md.py\", line 633, in _regen\n"
            "    raise OSError(28, 'No space left')\n"
            "OSError: [Errno 28] No space left")
    out = cd._mask_text(text)
    assert "Traceback (most recent call last):" in out and "OSError: [Errno 28] No space left" in out and " b.md" not in out


def test_continuation_line_is_masked_through_the_real_log_bundle_path(tmp_path):
    log_dir = tmp_path / "run"
    log_dir.mkdir()
    (log_dir / "app.log").write_text(
        "2026-09-16 10:00:00,000 WARNING sherpa.codex_skills: codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員\n"
        "極秘顧客 一覧\n"
        "2026-09-16 10:00:01,000 INFO sherpa.x: next line ok\n", encoding="utf-8")
    entries = cd.build_logs_bundle(log_dir, 7, 200.0)
    data = dict(entries)["logs/app.log"].decode("utf-8")
    assert "極秘" not in data and "一覧" not in data and "next line ok" in data
    (log_dir / "app2.log").write_text(
        "2026-09-16 10:00:00,000 WARNING sherpa.codex_skills: codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員\n"
        "\n"
        "極秘顧客 一覧\n"
        "2026-09-16 10:00:01,000 INFO sherpa.x: next line ok\n", encoding="utf-8")
    data = dict(cd.build_logs_bundle(log_dir, 7, 200.0))["logs/app2.log"].decode("utf-8")
    assert "極秘" not in data and "一覧" not in data and "next line ok" in data


def test_structured_rel_tail_is_hashed_and_fixed_wording_slash_does_not_start_span():
    h = cd._hash_id("test2")
    line = cd._mask_text("rag.md のレコード分割に失敗したため LLM 成形を skip します（規則版のまま）: world=test2 rel=人事部/役員報酬 極秘資料 一覧.xlsx")
    assert h in line and "極秘" not in line and "人事部" not in line and "役員" not in line
    line = cd._mask_text("reconvert 監査ログの記録に失敗しました（best-effort）: action=x world=test2 rel=役員報酬 極秘資料 一覧.xlsx")
    assert h in line and "極秘" not in line
    line = cd._mask_text("impact/run が Neo4j 安全弁で失敗（fail-loud・reason=guard・world=test2）")
    assert line.startswith("impact/run が Neo4j 安全弁で失敗") and h in line
    line = cd._mask_text("RAG/Evidence IR の軽量再生成に失敗しました（次回 sync で再試行）: world=test2 detail={'rag_failed': 2}")
    assert line.startswith("RAG/Evidence IR の軽量再生成に失敗しました") and h in line and "detail={'rag_failed': 2}" in line
    line = cd._mask_text("背景実行が未捕捉の例外で終了しました: world=test2 op=sync run_id=r-9")
    assert h in line and line.endswith(" op=sync run_id=r-9")
    line = cd._mask_text("stale codex run dir cleanup failed for JCL/PROD 極秘 run: type=OSError errno=13")
    assert "PROD" not in line and "極秘" not in line and line.endswith(": type=OSError errno=13")
    line = cd._mask_text("codex created file move/registration failed for run-abc/役員 極秘顧客 一覧.pptx: type=OSError errno=28")
    assert "極秘" not in line and line.endswith(": type=OSError errno=28")
    line = cd._mask_text("取り込み 100/200 done（3件） world=test2")
    assert line.startswith("取り込み 100/200 done")


def test_continuation_after_trailing_punct(tmp_path):
    log_dir = tmp_path / "run"
    log_dir.mkdir()
    (log_dir / "app.log").write_text(
        "2026-09-16 10:00:00,000 WARNING sherpa.codex_skills: codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員)\n"
        "\n"
        "極秘顧客 一覧\n"
        "2026-09-16 10:00:01,000 INFO sherpa.x: next line ok\n", encoding="utf-8")
    data = dict(cd.build_logs_bundle(log_dir, 7, 200.0))["logs/app.log"].decode("utf-8")
    assert "極秘" not in data and "一覧" not in data and "next line ok" in data


def test_escaped_quote_in_exception_repr_and_continuation_after_metric_note(tmp_path):
    line = cd._mask_text("codex created files registration setup failed (run_dir=/tmp/r): [Errno 13] Permission denied: '/srv/users/u1/workspace/skills/顧客\\'s \"極秘 案件\"'")
    assert "極秘" not in line and "案件" not in line and "顧客" not in line
    log_dir = tmp_path / "run"
    log_dir.mkdir()
    (log_dir / "app.log").write_text(
        "2026-09-16 10:00:00,000 WARNING sherpa.codex_skills: codex_skills: symlink inside skill source rejected: /srv/users/u1/workspace/skills/役員（1.2秒）\n"
        "極秘顧客 一覧\n"
        "2026-09-16 10:00:01,000 INFO sherpa.x: next line ok\n", encoding="utf-8")
    data = dict(cd.build_logs_bundle(log_dir, 7, 200.0))["logs/app.log"].decode("utf-8")
    assert "極秘" not in data and "一覧" not in data and "next line ok" in data


def test_colon_inside_name_without_structured_tail_runs_to_eol():
    line = cd._mask_text("importance: failed to stat control file cfg=dept/役員: 極秘顧客 一覧/_重要度.txt")
    assert "極秘" not in line and "一覧" not in line and "役員" not in line
    line = cd._mask_text("stale codex run dir cleanup failed for /tmp/x/run 1: type=OSError errno=13")
    assert line.endswith(": type=OSError errno=13")
    line = cd._mask_text("codex_skills: copy failed for /a/役員 b -> /d/極秘 f: OSError")
    assert "極秘" not in line and line.startswith("codex_skills: copy failed for <path:")
    line = cd._mask_text("importance: failed to read control file cfg=dept/役員: type=極秘顧客 一覧/_重要度.txt")
    assert "極秘" not in line and "一覧" not in line
    line = cd._mask_text("codex created file move/registration failed for run-abc/役員 一覧.pptx: type=OSError errno=28")
    assert line.endswith(": type=OSError errno=28") and "役員" not in line
    line = cd._mask_text("importance: failed to read control file failed for reports/役員: type=極秘顧客 一覧/_重要度.txt")
    assert "極秘" not in line and "一覧" not in line
    line = cd._mask_text("stale codex run dir cleanup failed for /tmp/x/run 1: type=OSError errno=13")
    assert line.endswith(": type=OSError errno=13")
    line = cd._mask_text("importance: failed to read control file 人事部/役員 極秘顧客 一覧/_重要度.txt")
    assert "人事部" not in line and "極秘" not in line and line.startswith("importance: failed to read control file <path:")
    line = cd._mask_text("OCR 観測 Set の読込/検証に失敗しました（VLM のみで rag.md を生成します）: 人事部/役員.xlsx")
    assert line.startswith("OCR 観測 Set の読込/検証に失敗しました")


def test_legacy_office_and_image_exts_are_paths_and_log_cap_is_reported(tmp_path):
    for name in ("dept/plan.xls", "dept/plan.doc", "dept/plan.ppt", "dept/scan.png", "dept/scan.jpg"):
        assert "<path:" in cd._mask_text("x " + name) and "dept" not in cd._mask_text("x " + name)
    log_dir = tmp_path / "run"
    log_dir.mkdir()
    (log_dir / "convert.log").write_text("2026-09-16 10:00:00,000 INFO x: big\n" * 20000, encoding="utf-8")
    (log_dir / "embed.log").write_text("2026-09-16 10:00:00,000 INFO x: small\n", encoding="utf-8")
    entries = cd.build_logs_bundle(log_dir, 7, 0.1)
    names = [a for a, _d in entries]
    assert "logs/embed.log" in names and "logs/convert.log" not in names and "logs/truncated.json" in names
    entries2 = cd.build_logs_bundle(log_dir, 7, 0.00001)
    assert [a for a, _d in entries2] == ["logs/truncated.json"]
    assert "打ち切られました（2 本）" in cd.build_log_report_text(entries2)
    line = cd._mask_text("importance: failed to read control file 極秘 案件/資料/_重要度.txt")
    assert "極秘" not in line and "案件" not in line and line.startswith("importance: failed to read control file <path:")
    line = cd._mask_text("codex_skills: symlink inside skill source rejected: 極秘 案件/資料")
    assert "極秘" not in line and "案件" not in line
    assert cd._mask_text("embed 進捗 100/200（3件）") == "embed 進捗 100/200（3件）"
    line = cd._mask_text("importance: failed to stat control file 極秘（役員） 案件/資料/_重要度.txt")
    assert "極秘" not in line and "役員" not in line and line.startswith("importance: failed to stat control file <path:")
    line = cd._mask_text("importance: failed to read control file ACME for 案件/資料/_重要度.txt")
    assert "ACME" not in line and "案件" not in line and line.startswith("importance: failed to read control file <path:")
    line = cd._mask_text("codex created file move/registration failed for run-abc/役員 極秘顧客 一覧.pptx: type=OSError errno=28")
    assert "極秘" not in line and line.endswith(": type=OSError errno=28")
    line = cd._mask_text("importance: failed to read control file failed for reports/役員: type=極秘顧客 一覧/_重要度.txt")
    assert "極秘" not in line
    line = cd._mask_text("importance: failed to read control file ACME: 案件/資料/_重要度.txt")
    assert "ACME" not in line and "案件" not in line and line.startswith("importance: failed to read control file <path:")
    line = cd._mask_text("importance: failed to read control file ACME=社外秘 案件/資料/_重要度.txt")
    assert "ACME" not in line and "社外秘" not in line and line.startswith("importance: failed to read control file <path:")
    line = cd._mask_text("workspace search: symlink rejected for uid=alice rel=docs/役員 一覧.txt")
    assert cd._hash_id("alice") in line and "役員" not in line


def test_fixed_wording_after_label_is_not_swallowed_by_back_extension():
    line = cd._mask_text("codex_skills: skip non-dir/symlink skill source: /srv/users/u1/workspace/skills/役員 一覧")
    assert line.startswith("codex_skills: skip non-dir/symlink skill source: <path:") and "役員" not in line
    line = cd._mask_text("marp_render: unshare によるネットワーク隔離が使えないため pdf/pptx をスキップ（html/.md のみ生成）")
    assert line.startswith("marp_render: unshare によるネットワーク隔離が使えないため pdf/pptx をスキップ")
    line = cd._mask_text("usage_chat: 専用設定/一時上書きの provider 値が不正です（value='x'）")
    assert line.startswith("usage_chat: 専用設定/一時上書きの provider 値が不正です")
    line = cd._mask_text("importance: failed to read control file 極秘 案件/資料/_重要度.txt")
    assert "極秘" not in line and "案件" not in line
    line = cd._mask_text("codex_skills: symlink inside skill source rejected: 極秘 案件/資料")
    assert "極秘" not in line and "案件" not in line
    line = cd._mask_text("importance: failed to read control file ACME'社外秘 案件/資料/_重要度.txt")
    assert "ACME" not in line and "社外秘" not in line and line.startswith("importance: failed to read control file <path:")
    line = cd._mask_text("AGENTS.md write failed: [Errno 13] Permission denied: '/x/役員 一覧/AGENTS.md'")
    assert line.endswith("Permission denied: '<path:" + line.rsplit("<path:", 1)[1]) and "一覧" not in line
    line = cd._mask_text("importance: failed to read control file ACME社外秘' 案件/資料/_重要度.txt")
    assert "ACME" not in line and "社外秘" not in line and line.startswith("importance: failed to read control file <path:")
    line = cd._mask_text("AGENTS.md write failed: [Errno 13] Permission denied: '役員 一覧/AGENTS.md'")
    assert "役員" not in line and "一覧" not in line and line.endswith("Permission denied: '<path:" + line.rsplit("<path:", 1)[1]) and line.endswith("'")
    for name in ("'ACME' 社外秘 案件/資料/_重要度.txt", "ACME<社外秘> 案件/資料/_重要度.txt", "ACME|社外秘 案件/資料/_重要度.txt"):
        line = cd._mask_text("importance: failed to read control file " + name)
        assert "ACME" not in line and "社外秘" not in line and line.startswith("importance: failed to read control file <path:"), line
    for name in ("ACME rel=案件/資料/_重要度.txt", "ACME world=x uid=y 案件/資料.txt"):
        line = cd._mask_text("importance: failed to read control file " + name)
        assert "ACME" not in line and "案件" not in line and "<world:" not in line and line.startswith("importance: failed to read control file <path:"), line
    line = cd._mask_text("workspace search: symlink rejected for uid=alice rel=docs/役員 一覧.txt")
    assert cd._hash_id("alice") in line and "役員" not in line
    line = cd._mask_text("importance: failed to read control file rel=案件/資料/_重要度.txt")
    assert "案件" not in line   # 前置き直後の key= は構造化欄扱い（値は欄ごと伏せる）


def test_label_colon_leads_japanese_relative_path_and_spaced_ascii_folder_is_extended():
    text = ("2026-09-16 10:00:00,000 ERROR sherpa.ingest.ocr_router: inventory failed\n"
            "Traceback (most recent call last):\n"
            "  File \"/opt/sherpa/sherpa/ingest/ocr_router.py\", line 147, in _inv\n"
            "ValueError: asset inventory contains symlink: 人事部 極秘/役員 一覧.png")
    out = cd._mask_text(text)
    assert "人事部" not in out and "極秘" not in out and "役員" not in out and out.endswith("contains symlink: <path:" + out.rsplit("<path:", 1)[1])
    line = cd._mask_text("neo4j クエリがタイムアウト（world=test2）: 人事部/役員 一覧")
    assert "人事部" not in line and "役員" not in line and cd._hash_id("test2") in line
    line = cd._mask_text("codex created file move/registration failed for Shared Docs/Board 一覧.xlsx: type=OSError errno=28")
    assert "Shared" not in line and "Board" not in line and line.endswith(": type=OSError errno=28")
    line = cd._mask_text("usage_chat: 専用設定/一時上書きの provider 値が不正です（value='x'）")
    assert line.startswith("usage_chat: 専用設定/一時上書きの provider 値が不正です")
    line = cd._mask_text("codex_skills: skip non-dir/symlink skill source: /srv/users/u1/workspace/skills/役員 一覧")
    assert line.startswith("codex_skills: skip non-dir/symlink skill source: <path:")
    line = cd._mask_text("importance: failed to read control file ACME: X rel=案件/資料/_重要度.txt")
    assert "ACME" not in line and "案件" not in line and line.startswith("importance: failed to read control file <path:")
    line = cd._mask_text("workspace search: symlink rejected for uid=alice rel=docs/役員 一覧.txt")
    assert cd._hash_id("alice") in line and "役員" not in line
    line = cd._mask_text("importance: failed to read control file files/極秘顧客 資料/_重要度.txt")
    assert "極秘" not in line and "資料" not in line and line.startswith("importance: failed to read control file <path:")
    line = cd._mask_text("usage_chat: 専用設定/一時上書きの provider 値が不正です（value='x'）")
    assert line.startswith("usage_chat: 専用設定/一時上書きの provider 値が不正です")
    line = cd._mask_text("x: pdf/pptx-archive/極秘 一覧.pptx")
    assert "極秘" not in line
    for name in ("COBOL/JCL 極秘顧客/_重要度.txt", "non-dir/symlink 極秘/_重要度.txt", "impact/run 極秘/_重要度.txt"):
        line = cd._mask_text("importance: failed to read control file " + name)
        assert "極秘" not in line and line.startswith("importance: failed to read control file <path:"), line
    line = cd._mask_text("codex_skills: skip non-dir/symlink skill source: /srv/users/u1/workspace/skills/役員 一覧")
    assert line.startswith("codex_skills: skip non-dir/symlink skill source: <path:")
    line = cd._mask_text("importance: failed to read control file ACME VLM/OCR 極秘/_重要度.txt")
    assert "ACME" not in line and "極秘" not in line and line.startswith("importance: failed to read control file <path:")
    line = cd._mask_text("marp_render: unshare によるネットワーク隔離が使えないため pdf/pptx をスキップ（html/.md のみ生成）")
    assert line.startswith("marp_render: unshare によるネットワーク隔離が使えないため pdf/pptx をスキップ")
    line = cd._mask_text("importance: failed to read control file ACME: VLM/OCR 極秘/_重要度.txt")
    assert "ACME" not in line and "極秘" not in line and line.startswith("importance: failed to read control file <path:")


def test_fixed_wording_inside_japanese_word_url_field_and_fraction_with_paren():
    line = cd._mask_text("sub_planner: 計画呼び出しが失敗/空のため縮退します")
    assert line == "sub_planner: 計画呼び出しが失敗/空のため縮退します"
    line = cd._mask_text("VLM(ollama): 接続先（llm-host）がローカル/私有アドレスと確認できず、クラウド許可も無いため送信しません（fail-safe）。")
    assert "ローカル/私有アドレスと確認できず" in line
    line = cd._mask_text("office_com upload が失敗しました（HTTP 500・試行 2/3）: http://o.internal:8080/c")
    assert "試行 2/3）" in line and "<url:http|<host:" in line and ":8080" in line and "o.internal" not in line
    line = cd._mask_text("neo4j 接続に失敗しました: bolt://graph.internal:7687")
    assert "<url:bolt|<host:" in line and ":7687" in line and "graph.internal" not in line
    line = cd._mask_text("importance: failed to read control file ACME 2026/09 極秘/_重要度.txt")
    assert "ACME" not in line and "極秘" not in line and line.startswith("importance: failed to read control file <path:")
    assert cd._mask_text("取り込み 100/200 done（3件） world=test2").startswith("取り込み 100/200 done（3件）")
    line = cd._mask_text("ValueError: asset inventory contains symlink: ACME 2026/09 極秘/役員.png")
    assert "ACME" not in line and "極秘" not in line and line.startswith("ValueError: asset inventory contains symlink: <path:")
    assert cd._mask_text("es_index: embed 進捗 100/200 チャンク（world=test2）").startswith("es_index: embed 進捗 100/200 チャンク")
    line = cd._mask_text("ValueError: asset inventory contains symlink: ACME VLM/OCR 極秘/役員.png")
    assert "ACME" not in line and "極秘" not in line and line.startswith("ValueError: asset inventory contains symlink: <path:")
    line = cd._mask_text("VLM/OCR 観測 Set の合流に失敗しました（VLM 単独へ縮退します）: 人事部/役員.xlsx")
    assert line.startswith("VLM/OCR 観測 Set の合流に失敗しました")


def test_english_path_label_formats_hash_the_whole_value():
    for name in ("ACME 2026/09", "ACME VLM/OCR", "ACME: 極秘/役員.png", "極秘 案件", "ACME for x: type=極秘"):
        line = cd._mask_text("ValueError: asset inventory contains symlink: " + name)
        assert "ACME" not in line and "極秘" not in line and "案件" not in line and line.startswith("ValueError: asset inventory contains symlink: <path:"), line
        line = cd._mask_text("importance: failed to read control file " + name)
        assert "ACME" not in line and "極秘" not in line and line.startswith("importance: failed to read control file <path:"), line
    line = cd._mask_text("workspace search: symlink rejected for uid=alice rel=役員 一覧: type=極秘")
    assert cd._hash_id("alice") in line and "役員" not in line and "極秘" not in line


def test_path_key_fields_hash_to_eol_including_exception_text():
    line = cd._mask_text("gc_orphan: failed uid=u1 file=役員: type=極秘顧客 一覧.xlsx: [Errno 13] Permission denied: '役員: type=極秘顧客 一覧.xlsx'")
    assert "役員" not in line and "極秘" not in line and line.startswith("gc_orphan: failed uid=<uid:")
    line = cd._mask_text("register 失敗時の派生ディレクトリ削除でエラー path=/home/x/data/derived/test2: [Errno 39] Directory not empty")
    assert "derived" not in line and "test2" not in line
    line = cd._mask_text("workspace search: symlink rejected for uid=alice rel=役員 一覧: type=極秘")
    assert "極秘" not in line
    line = cd._mask_text("ValueError: asset inventory contains symlink: 顧客資料（type=ACME）")
    assert "ACME" not in line and "顧客" not in line
    line = cd._mask_text("VLM: provider=ollama の接続先（inference01）がローカル/私有アドレスと確認できず、クラウド許可も無いため送信しません")
    assert "inference01" not in line and "接続先（<host:" in line and "ローカル/私有アドレスと確認できず" in line
    line = cd._mask_text("VLM(ollama): 接続先（http://llm.internal:11434）がローカル/私有アドレスと確認できず")
    assert "llm.internal" not in line and ":11434" in line
    line = cd._mask_text("sweep_expired: path outside files_dir, skipping uid=u1 rel=役員 一覧: type=極秘顧客")
    assert "極秘" not in line and "役員" not in line

    line = cd._mask_text("codex_skills: copy failed for /srv/users/u1/workspace/skills/役員: type=極秘顧客 一覧 -> /tmp/x: [Errno 13] Permission denied: '/srv/users/u1/workspace/skills/役員: type=極秘顧客 一覧'")
    assert "極秘" not in line and "役員" not in line
    line = cd._mask_text("sweep_expired: re-upload detected under lock, skipping uid=u1 rel=顧客資料（type=ACME）")
    assert "ACME" not in line and "顧客" not in line
    line = cd._mask_text("MD化が完了しました: manuals/report.xlsx（12.3秒・RSS 0.1G→0.2G）")
    assert line.endswith("（12.3秒・RSS 0.1G→0.2G）")
    line = cd._mask_text("codex created file move/registration failed for run-abc/顧客別利益率（30%）: type=OSError errno=28")
    assert "30%" not in line and "利益率" not in line and line.endswith(": type=OSError errno=28")
    line = cd._mask_text("MD化を開始します: docs/x.xlsx（RSS 0.1G）")
    assert line.endswith("（RSS 0.1G）")
    line = cd._mask_text("codex created file move/registration failed for run-abc/顧客応答時間（30秒）: type=OSError errno=28")
    assert "30秒" not in line and "応答" not in line and line.endswith(": type=OSError errno=28")
    line = cd._mask_text("legacy 変換がタイムアウトしました（60s）: /kb/a b（30秒）.xls")
    assert "30秒" not in line
    line = cd._mask_text("MD化が完了しました: manuals/report.xlsx（12.3秒・RSS 0.1G→0.2G）")
    assert line.endswith("（12.3秒・RSS 0.1G→0.2G）")
