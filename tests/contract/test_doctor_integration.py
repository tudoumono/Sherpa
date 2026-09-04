"""`scripts/doctor_checks.py::run_all()` の統合契約テスト。

`tests/unit/test_doctor_checks.py` は各検査関数を個別にモックして分岐を確認するのに対し、
本ファイルは `run_all()` を実モジュールのまま通し、外部 I/O 境界だけを差し替える:
  - `psycopg.connect`（Postgres）
  - `neo4j.GraphDatabase.driver`（Neo4j）
  - `urllib.request.urlopen`（ES の `_es_get` が直接使う裸の urllib 関数）
  - `sherpa.llm.urlopen_no_redirect`（Ollama の `_probe_ollama_usage` が使う関数。カスタム
    opener を internally 使うため、上の裸の `urllib.request.urlopen` を差し替えても Ollama 側の
    呼び出しは横取りできない＝別々にモックする必要がある）
  - `shutil.which`／`subprocess.run`（Codex CLI）

個別関数のモックでは見えない「実際の呼び出しの繋がり」（読み取り専用 SELECT が本当に DDL を
経由しない経路を通っているか・`llm.ollama_url()` の SSRF 許可判定へ system_settings が正しく
渡っているか・Bedrock SigV4 判定が `sherpa.agents` を実際に経由するか等）を固定する。フェイク
Postgres 接続は SELECT 単文以外（複文・DDL・DML）を即座に拒否し、実行された SQL を記録する
（「読み取り専用」契約をテスト側でも積極的に検証する＝`_FakePgConn` 参照）。

`doctor.sh`（bash エントリポイント）は `bash -n`（構文検査）だけでなく、実サブプロセスとして
起動し、env 読み込み→python 実行という配線が最後まで動くことを確認する。ローカルの未使用ポート
（`127.0.0.1:1`・接続が即座に拒否される）を使い、実ネットワークのタイムアウト待ちを避ける。
`PROBE_CLOUD` の受け渡し配線は、実際の検査ロジックの代わりに子プロセスの環境変数をそのまま
echo するフェイクランナー（`PYTHON_BIN` に指す小さな bash スクリプト）で検証し、業務ロジックの
分岐の複雑さから独立して固定する。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.doctor_checks as doctor_checks
from sherpa import health

ROOT = Path(__file__).resolve().parents[2]
DOCTOR_SH = ROOT / "scripts" / "doctor.sh"


# ---------------------------------------------------------------------------
# 外部 I/O 境界のフェイク
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakePgConn:
    """`psycopg.connect(...)` の戻り値（context manager）。

    読み取り専用契約をテスト側でも積極的に検証する: **単文の SELECT 以外**（`INSERT`／`UPDATE`／
    `DELETE`／`CREATE`／`ALTER`／`DROP`・`;` で連結した複文＝例えば `"SELECT 1; DELETE ..."`）が
    渡されたら即座に例外にする（doctor_checks 側のバグでうっかり書き込み系 SQL を実行しても、
    フェイクが黙って受理して見逃すことがないようにする。先頭一致だけの判定だと複文の2文目以降を
    見逃す＝`;` で分割し文の**個数**も検証する）。実行された全 SQL は `executed_sql` に記録し、
    テストから検証できるようにする。SQL 文字列に含まれるテーブル名で問い合わせを振り分ける
    （本物の SQL パーサではない・テスト用の最小限の判定）。
    """

    def __init__(self, settings_rows, user_rows, fail_on_tables=(), executed_sql=None, executed_params=None):
        self._settings_rows = settings_rows
        self._user_rows = user_rows
        self._fail_on_tables = fail_on_tables
        self.executed_sql = executed_sql if executed_sql is not None else []
        self.executed_params = executed_params if executed_params is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed_sql.append(sql)
        self.executed_params.append(params)
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        if len(statements) != 1 or not statements[0].lower().startswith("select"):
            raise AssertionError(f"読み取り専用契約違反: SELECT 単文以外が実行されました: {sql!r}")
        stripped = statements[0].lower()
        for table in self._fail_on_tables:
            if table in stripped:
                raise RuntimeError(f"permission denied for table {table}")
        if "system_settings" in stripped:
            return _FakeCursor(self._settings_rows)
        if "user_settings" in stripped:
            return _FakeCursor(self._user_rows)
        return _FakeCursor([])   # SELECT 1（ping）等


def _install_fake_pg(monkeypatch, *, settings_rows=(), user_rows=(), fail_on_tables=(), executed_params=None):
    """フェイク Postgres を差し込む。戻り値は実行された全 SQL 文のリスト（呼び出し後に
    参照すると、そのシナリオで実際に発行された SQL を検証できる＝可変リストを共有する）。

    `executed_params`（省略可）: 呼び出し側が渡した可変リストへ、各 `execute()` のバインド
    パラメータ（`sql` と同じ順序）を記録する。パラメータ化された値（Unicode 空白集合等・SQL
    文字列リテラルへ直接埋め込まない値）の中身を検証したいテストだけが使う。
    """
    executed: list[str] = []

    def _fake_connect(*args, **kwargs):
        return _FakePgConn(list(settings_rows), list(user_rows), fail_on_tables, executed, executed_params)
    monkeypatch.setattr("psycopg.connect", _fake_connect)
    return executed


class _FakeNeo4jDriver:
    def __init__(self, fail=False):
        self._fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def verify_connectivity(self):
        if self._fail:
            raise RuntimeError("neo4j unreachable")


def _install_fake_neo4j(monkeypatch, *, fail=False):
    monkeypatch.setattr("neo4j.GraphDatabase.driver", lambda *a, **k: _FakeNeo4jDriver(fail))


class _FakeHttpResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _install_fake_es(monkeypatch, *, root_payload: bytes, plugins_payload: bytes):
    def _fake_urlopen(url, timeout=None):
        if "_cat/plugins" in url:
            return _FakeHttpResponse(plugins_payload)
        return _FakeHttpResponse(root_payload)
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)


_ES_OK_ROOT = json.dumps({"version": {"number": "8.19.20"}}).encode()
_ES_OK_PLUGINS = json.dumps([{"component": "analysis-kuromoji"}]).encode()


def _install_healthy_es(monkeypatch):
    _install_fake_es(monkeypatch, root_payload=_ES_OK_ROOT, plugins_payload=_ES_OK_PLUGINS)


def _install_no_codex(monkeypatch):
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: None)


def _install_ollama_ok(monkeypatch):
    """Ollama の用途別プローブ（`_resolve_ollama_usages`／`_probe_ollama_usage`・接続先やモデル
    解決の詳細に踏み込まないシナリオ用）を常に成功させる（`doctor_checks._probe_ollama_usage` を
    直接モック＝この用途では現行の実装が3引数 `(url, model, sys_s)` を取ることだけ揃える）。"""
    monkeypatch.setattr(doctor_checks, "_probe_ollama_usage", lambda url, model, sys_s: (True, "ok"))


def _install_get_system_settings_spy(monkeypatch) -> list:
    """`sherpa.store.get_system_settings()`（`_ensure()`→DDL を実行しうる高水準 API）が doctor の
    読み取り専用経路から一切呼ばれないことを、**呼び出し回数の記録**で確認する。

    呼び出し元の一部（`sherpa.llm._allowlisted_hosts()` の `except Exception: entries = []` 等）は
    例外を握り潰す設計のため、raise だけに頼ると退行時でも例外がテストまで伝播せず検出できない。
    呼び出しの**記録**は例外が握り潰されるかどうかに関係なく必ず残る（関数本体の先頭で記録して
    から raise するため）ので、呼び出し元は `run_all()` 完了後に `assert calls == []` で明示確認
    すること（引数の値は見ない・呼び出しの有無だけを使う）。"""
    import sherpa.store as sherpa_store
    calls: list = []

    def _spy(*a, **k):
        calls.append((a, k))
        raise AssertionError(
            "get_system_settings()（_ensure()→DDL を実行しうる高水準 API）を呼んではいけない")
    monkeypatch.setattr(sherpa_store, "get_system_settings", _spy)
    return calls


def _fake_effective_agent_from_settings(default: str):
    """`sherpa.agent_constructs.effective_agent` のフェイク: `settings` に明示された `agent` を
    そのまま返し、無ければ `default` を返す（実環境の Codex CLI 有無等に左右されない決定的な
    要否判定シナリオを組むために使う）。"""
    def _fn(settings, **kwargs):
        settings = settings or {}
        return settings.get("agent") or default
    return _fn


# ---------------------------------------------------------------------------
# シナリオ
# ---------------------------------------------------------------------------

def test_run_all_settings_read_failure_is_ng_not_skip(monkeypatch):
    """system_settings の読み取り専用 SELECT が失敗（DDL 権限なし相当）した場合、全体が SKIP
    だらけの exit 0 にならず、明示的な NG が立つ。"""
    _install_fake_pg(monkeypatch, fail_on_tables=("system_settings",))
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    _install_no_codex(monkeypatch)
    _install_ollama_ok(monkeypatch)   # required=True（sys_s 不明で fail-closed）でも通す

    results = doctor_checks.run_all(probe_cloud=False)
    by_id = {r.id: r for r in results}

    assert by_id["postgres"].status == "ok"                       # SELECT 1 自体は通る
    assert by_id["system_settings_read"].status == "ng"           # ここが今回の要点
    assert by_id["user_settings_read"].status == "ok"             # user_settings 自体は読める
    assert by_id["openai_endpoint"].status == "skip"
    assert by_id["selected_provider_key"].status == "skip"
    # fail-closed: sys_s/rows 不明のため codex/ollama は必須扱い＝未導入なら NG（黙って SKIP にしない）。
    assert by_id["codex_cli"].status == "ng"
    assert by_id["llm_ollama"].status == "ng"
    assert any(r.status == "ng" for r in results)                 # exit code が非0になる


def test_run_all_sigv4_bedrock_configuration_is_ok(monkeypatch):
    """中央 Bearer キーが無くても AWS SigV4 の静的な手掛かりがあれば OK。"""
    settings_rows = [
        {"key": "cloud_provider", "value": "bedrock"},
        {"key": "openai_endpoint_seed_version", "value": 1},
    ]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=[])
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    _install_no_codex(monkeypatch)      # codex を経路から外し、この test の関心を bedrock に絞る
    _install_ollama_ok(monkeypatch)     # 同上（ollama required 判定のノイズを避ける）
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    results = doctor_checks.run_all(probe_cloud=False)
    by_id = {r.id: r for r in results}

    assert by_id["system_settings_read"].status == "ok"
    assert by_id["selected_provider_key"].status == "ok"
    assert "bedrock" in by_id["selected_provider_key"].detail
    # PROBE_CLOUD 既定 OFF なので実プローブはしない（課金しない）。
    assert by_id["llm_bedrock"].status == "skip"


def test_run_all_no_marker_azure_env_deployment_unregistered_sends_zero_and_ng(monkeypatch):
    """接続先が未確定（`openai_endpoint_seed_version` 無し＝NO_MARKER）でも、env に妥当な Azure
    候補があれば、`run_all()` はその候補を反映した `system_settings` を `check_selected_
    provider_key`／`check_cloud_llm_probes` へ渡す（`_openai_endpoint_status` 参照）。chat 用途の
    デプロイ名が未登録の構成では、実送信の境界（`complete_json`）を一度も呼ばずに静的検査だけで
    NG になることを `run_all()` の実配線で end-to-end に固定する（送信ゼロ）。"""
    from sherpa import agent_constructs
    from sherpa.ingest import graph_extract
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x.openai.azure.com/openai/v1")
    monkeypatch.delenv("SHERPA_OPENAI_ENDPOINT_KIND", raising=False)
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")

    def _should_not_be_called(*a, **k):
        raise AssertionError("接続先が未確定の構成で実送信してはいけない")
    monkeypatch.setattr(graph_extract, "complete_json", _should_not_be_called)

    settings_rows = [
        {"key": "cloud_provider", "value": "openai"},
        {"key": "openai_api_key", "value": "sk-azure-only-key-1234567890"},
        # openai_endpoint_seed_version 無し＝NO_MARKER
    ]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=[])
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    _install_no_codex(monkeypatch)
    _install_ollama_ok(monkeypatch)

    results = doctor_checks.run_all(probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["openai_endpoint"].status == "ok"   # env 候補自体は妥当（次回起動時に取り込まれる）
    assert by_id["llm_openai"].status == "ng"
    assert "デプロイ名" in by_id["llm_openai"].detail


def test_run_all_db_endpoint_invalid_skips_all_cloud_checks_with_zero_sends(monkeypatch):
    """接続先チェックが `ng`（`DB_ENDPOINT_INVALID`）のときは、`selected_provider_key`／
    `llm_openai`／`llm_gemini`／`llm_bedrock` の送信を伴う確認を一律 SKIP にする（送信ゼロ）。"""
    from sherpa.ingest import graph_extract

    def _should_not_be_called(*a, **k):
        raise AssertionError("接続先が不正な構成で実送信してはいけない")
    monkeypatch.setattr(graph_extract, "complete_json", _should_not_be_called)

    settings_rows = [
        {"key": "cloud_provider", "value": "openai"},
        {"key": "openai_api_key", "value": "sk-real-key-1234567890"},
        {"key": "openai_endpoint_kind", "value": "bogus"},
        {"key": "openai_endpoint_seed_version", "value": 1},
    ]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=[])
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    _install_no_codex(monkeypatch)
    _install_ollama_ok(monkeypatch)

    results = doctor_checks.run_all(probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["openai_endpoint"].status == "ng"
    assert by_id["selected_provider_key"].status == "skip"
    assert by_id["llm_openai"].status == "skip"
    assert by_id["llm_gemini"].status == "skip"
    assert by_id["llm_bedrock"].status == "skip"
    assert by_id["codex_auth"].status == "skip"


def test_run_all_db_endpoint_invalid_skips_codex_azure_real_send(monkeypatch):
    """接続先が `ng`（`DB_ENDPOINT_INVALID`）の構成で、Codex(Azure/custom) を使う利用者がいても
    `check_codex` は生の（未確定・不正な）`system_settings` を使わず、`codex_auth` を SKIP にする
    （送信ゼロ）。Codex 自身の実送信境界（`_run_raw_llm_probe`）が一度も呼ばれないことを end-to-end
    で固定する。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "codex")
    monkeypatch.setattr(agent_constructs, "codex_model_provider", lambda *a, **k: "openai")

    def _should_not_be_called(*a, **k):
        raise AssertionError("接続先が不正な構成で Codex の実送信をしてはいけない")
    monkeypatch.setattr(doctor_checks, "_run_raw_llm_probe", _should_not_be_called)

    settings_rows = [
        {"key": "cloud_provider", "value": "openai"},
        {"key": "openai_api_key", "value": "sk-real-key-1234567890"},
        {"key": "openai_endpoint_kind", "value": "bogus"},
        {"key": "openai_endpoint_seed_version", "value": 1},
    ]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=[])
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(doctor_checks.subprocess, "run",
                        lambda *a, **k: type("P", (), {"returncode": 0, "stdout": "codex-cli 0.144.1\n",
                                                        "stderr": ""})())
    _install_ollama_ok(monkeypatch)

    results = doctor_checks.run_all(probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["openai_endpoint"].status == "ng"
    assert by_id["codex_auth"].status == "skip"


def test_run_all_agent_resolution_indeterminate_ng_on_own_items_not_only_ollama(monkeypatch):
    """`effective_agent()` が例外を投げる構成では、`selected_provider_key`／`llm_openai`／
    Codex の3項目（`codex_cli`／`codex_version`／`codex_auth`）が自身の固定文言 NG を報告する
    （Ollama の要否判定だけに症状が現れて根本原因が誤帰属しないことを end-to-end で固定する）。"""
    from sherpa import agent_constructs

    def _boom(settings, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_constructs, "effective_agent", _boom)

    settings_rows = [
        {"key": "cloud_provider", "value": "openai"},
        {"key": "openai_api_key", "value": "sk-real-key-1234567890"},
        {"key": "openai_endpoint_seed_version", "value": 1},
    ]
    user_rows = [{"agent": "openai", "codex_model_provider": None, "ollama_url": None,
                 "search_helper": "", "has_openai_key": False, "has_gemini_key": False,
                 "has_bedrock_key": False}]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=user_rows)
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: None)

    results = doctor_checks.run_all(probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["selected_provider_key"].status == "ng"
    assert by_id["selected_provider_key"].detail == doctor_checks._AGENT_RESOLUTION_FAILED_DETAIL
    assert by_id["llm_openai"].status == "ng"
    assert by_id["llm_openai"].detail == doctor_checks._AGENT_RESOLUTION_FAILED_DETAIL
    assert by_id["codex_cli"].status == "ng"
    assert by_id["codex_cli"].detail == doctor_checks._AGENT_RESOLUTION_FAILED_DETAIL


def test_run_all_personal_keys_allowed_avoids_false_ng(monkeypatch):
    """`personal_api_keys_allowed=true` かつ有効な利用者の個人キーが
    あれば、中央キー欠落でも NG にしない（`_read_active_user_configs_readonly()` の実クエリが
    `has_openai_key` 等を正しく返すことも含めて end-to-end で確認する）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", _fake_effective_agent_from_settings("openai"))
    settings_rows = [
        {"key": "cloud_provider", "value": "openai"},
        {"key": "personal_api_keys_allowed", "value": True},
        {"key": "openai_endpoint_seed_version", "value": 1},
    ]
    user_rows = [{"agent": "openai", "codex_model_provider": None, "ollama_url": None, "search_helper": "",
                 "has_openai_key": True, "has_gemini_key": False, "has_bedrock_key": False}]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=user_rows)
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    _install_no_codex(monkeypatch)
    _install_ollama_ok(monkeypatch)

    results = doctor_checks.run_all(probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["selected_provider_key"].status == "ok"
    assert "1" in by_id["selected_provider_key"].detail
    assert not any(r.status == "ng" for r in results)


def test_run_all_personal_keys_skip_cloud_probe_instead_of_false_ng(monkeypatch):
    """`PROBE_CLOUD=1` でも、個人キーのみの正常構成では実プローブを試みず SKIP に収束させる
    （doctor は個人キーの値を読まない設計のため、中央キーが無い個人キー運用の実接続確認自体が
    doctor からはできない＝実プローブへ進めて誤 NG にしない）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", _fake_effective_agent_from_settings("openai"))
    settings_rows = [
        {"key": "cloud_provider", "value": "openai"},
        {"key": "personal_api_keys_allowed", "value": True},
        {"key": "openai_endpoint_seed_version", "value": 1},
    ]
    user_rows = [{"agent": "openai", "codex_model_provider": None, "ollama_url": None, "search_helper": "",
                 "has_openai_key": True, "has_gemini_key": False, "has_bedrock_key": False}]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=user_rows)
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    _install_no_codex(monkeypatch)
    _install_ollama_ok(monkeypatch)

    from sherpa.ingest import graph_extract

    def _should_not_be_called(*a, **k):
        raise AssertionError("個人キーのみの構成で実プローブしてはいけない")
    monkeypatch.setattr(graph_extract, "complete_json", _should_not_be_called)

    results = doctor_checks.run_all(probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "skip"
    assert "個人キー" in by_id["llm_openai"].detail
    assert not any(r.status == "ng" for r in results)


@pytest.mark.parametrize("secret_in_message", [
    "sk-abcdefgh1234567890ABCDEFGHIJK",           # 分断なし
    "sk-ab\ncdefgh1234\t567890ABCDEFGHIJK",        # 制御文字（改行・タブ）で分断
    "sk-ab cdefgh1234-567890ABCDEFGHIJK",          # 空白・記号で分断
], ids=["intact", "control-char-split", "space-and-symbol-split"])
def test_run_all_cloud_probe_failure_never_leaks_real_key_end_to_end(monkeypatch, secret_in_message):
    """`run_all()` の実行経路全体を通した end-to-end 確認: `graph_extract._safe_detail` を
    モックで迂回せず、`graph_extract.complete_json`（実際に上流 API へ送信する直前の境界）だけを
    モックする（`_safe_detail` を直接モックすると、doctor 側の実際のマスク処理を経由しない
    false-green になる）。実キーが完全な形・制御文字分割・空白/記号分割のいずれで上流にエコー
    されても、`llm_openai` の detail には一切の自由文（実キーのどんな断片も）が現れず、固定形式の
    安全な分類文字列のみになることを固定する（fail-closed）。
    """
    from sherpa import agent_constructs
    from sherpa.ingest import graph_extract
    monkeypatch.setattr(agent_constructs, "effective_agent", _fake_effective_agent_from_settings("openai"))
    secret = "sk-abcdefgh1234567890ABCDEFGHIJK"
    settings_rows = [
        {"key": "cloud_provider", "value": "openai"},
        {"key": "openai_api_key", "value": secret},
        {"key": "openai_endpoint_seed_version", "value": 1},
    ]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=[])
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    _install_no_codex(monkeypatch)
    _install_ollama_ok(monkeypatch)

    def _boom(system, user, cfg, timeout=None):
        assert cfg["key"] == secret
        raise RuntimeError(f"invalid key: {secret_in_message}")
    monkeypatch.setattr(graph_extract, "complete_json", _boom)

    results = doctor_checks.run_all(probe_cloud=True)
    detail = {r.id: r for r in results}["llm_openai"].detail
    assert detail == "接続に失敗しました: error（RuntimeError）"
    assert "sk-" not in detail
    assert secret not in detail


def test_run_all_gemini_selected_openai_personal_key_does_not_skip_codex_azure(monkeypatch):
    """A7: `cloud_provider=gemini`（システム選択）の環境で、有効な利用者に残存する openai の
    個人キーは、`keys.resolve_api_key("openai", ...)` の A7 ゲートにより誰にも解決できない。
    Codex(Azure/custom) 認証確認がこの残存キーを「個人キー運用中」と誤認して SKIP へ倒すと、
    実際には中央キーが無く Codex(OpenAI 互換) が起動できない構成を見逃す（end-to-end 確認）。
    サンドボックス・URL 妥当性等の判定内部は `_codex_openai_compat_block_reason` を直接固定して
    切り離し、この確認の対象（A7 ゲート）だけに絞る。キー実在を強制した2回目の呼び出しでは
    `None`（他に不備は無い）を返すようにし、`_check_codex_azure_compat` が実際に
    `_personal_key_holder_count`（A7 ゲート）まで進むことを保証する。
    """
    from sherpa import keys
    no_central_key_reason = f"{keys.NO_CENTRAL_KEY_MESSAGE}（Azure 等の接続先の認証にも使います）"

    def _block_reason(s, *, explicit_openai_api_key=None, **k):
        if explicit_openai_api_key:
            return None
        return no_central_key_reason
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", _block_reason)

    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", _fake_effective_agent_from_settings("codex"))
    monkeypatch.setattr(agent_constructs, "codex_model_provider", lambda *a, **k: "openai")
    settings_rows = [
        {"key": "cloud_provider", "value": "gemini"},
        {"key": "personal_api_keys_allowed", "value": True},
        {"key": "openai_endpoint_kind", "value": "azure"},
        {"key": "openai_base_url", "value": "https://x.openai.azure.com/openai/v1"},
        {"key": "openai_endpoint_seed_version", "value": 1},
    ]
    # この利用者は openai の個人キーを保存済みだが、システム選択は gemini ＝この openai 個人キーは
    # A7 ゲートにより誰も解決できない（実際には Codex の OpenAI 認証は未解決のまま）。
    user_rows = [{"agent": "codex", "codex_model_provider": "openai", "ollama_url": None, "search_helper": "",
                 "has_openai_key": True, "has_gemini_key": False, "has_bedrock_key": False}]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=user_rows)
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")

    class _Proc:
        returncode = 0
        stdout = "codex-cli 0.144.1\n"
        stderr = ""
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _Proc())
    _install_ollama_ok(monkeypatch)

    results = doctor_checks.run_all(probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["codex_auth"].status == "ng"
    assert "個人キー" not in by_id["codex_auth"].detail


def test_run_all_disabled_extra_agent_is_reported_as_independent_ng(monkeypatch):
    """有効な利用者が gemini を選んでいるが `SHERPA_EXTRA_AGENTS` に含まれず現在の環境では無効な
    場合、`disabled_agent_configs` が独立の NG として報告される。無効化された頭脳は
    `_select_provider` がキー解決より先に `_DisabledProvider` へ差し替えるため、`selected_provider_key`
    側では「消費していない」扱いになり、キーの有無とは別の理由で報告されることを end-to-end で
    確認する。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", _fake_effective_agent_from_settings("openai"))
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: agent == "gemini")
    settings_rows = [
        {"key": "cloud_provider", "value": "openai"},
        {"key": "openai_api_key", "value": "sk-real-key"},
        {"key": "openai_endpoint_seed_version", "value": 1},
    ]
    user_rows = [{"agent": "gemini", "codex_model_provider": None, "ollama_url": None, "search_helper": ""}]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=user_rows)
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    _install_no_codex(monkeypatch)
    _install_ollama_ok(monkeypatch)

    results = doctor_checks.run_all(probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["disabled_agent_configs"].status == "ng"
    assert "1" in by_id["disabled_agent_configs"].detail
    assert by_id["selected_provider_key"].status == "ok"   # openai 自体は中央キーがあり ok


@pytest.mark.parametrize("root_payload,plugins_payload,label", [
    (b"null", _ES_OK_PLUGINS, "root-is-null"),
    (b"42", _ES_OK_PLUGINS, "root-is-number"),
    (json.dumps({"tagline": "You Know, for Search"}).encode(), _ES_OK_PLUGINS, "version-number-missing"),
])
def test_run_all_es_malformed_response_is_ng_without_crashing(monkeypatch, root_payload, plugins_payload, label):
    """ES の応答が想定外の形（JSON null／数値／version.number 欠落）でも `run_all()` が例外を
    送出せず、必ず NG の CheckResult を返す。"""
    _install_fake_pg(monkeypatch, settings_rows=[{"key": "openai_endpoint_seed_version", "value": 1}])
    _install_fake_neo4j(monkeypatch)
    _install_fake_es(monkeypatch, root_payload=root_payload, plugins_payload=plugins_payload)
    _install_no_codex(monkeypatch)
    _install_ollama_ok(monkeypatch)

    results = doctor_checks.run_all(probe_cloud=False)   # 例外を投げずに完走することが前提
    by_id = {r.id: r for r in results}

    assert by_id["elasticsearch"].status == "ng", label
    assert by_id["es_kuromoji"].status == "skip", label   # ES 未確認のため kuromoji は確認できない


def test_run_all_optional_codex_failure_is_skip_not_ng(monkeypatch):
    """現在の構成（システム既定＋有効な利用者の保存設定）が Codex を使わない場合、
    `codex --version`／ログイン確認の失敗は NG でなく SKIP（情報表示）。

    `agent_constructs.effective_agent` を明示的にフェイクし、システム既定（設定なし利用者）が
    "ollama" になる構成を決定的に作る（実環境の Codex CLI 有無に結果が左右されないようにする）。
    """
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", _fake_effective_agent_from_settings("ollama"))
    settings_rows = [
        {"key": "cloud_provider", "value": "bedrock"},
        {"key": "bedrock_api_key", "value": "central-bedrock-key"},
        {"key": "openai_endpoint_seed_version", "value": 1},
    ]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=[])
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    _install_ollama_ok(monkeypatch)

    # codex CLI は「見つかるが」バージョン取得・ログイン確認が失敗する構成。
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")

    class _FailingProc:
        returncode = 1
        stdout = ""
        stderr = "unexpected error"
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _FailingProc())

    def _codex_auth_boom(settings, sys_s):
        raise RuntimeError("未ログイン")
    monkeypatch.setattr(health, "_ai_check_codex", _codex_auth_boom)

    results = doctor_checks.run_all(probe_cloud=False)
    by_id = {r.id: r for r in results}

    assert by_id["codex_cli"].status == "ok"          # CLI 自体は見つかる
    assert by_id["codex_version"].status == "skip"
    assert by_id["codex_auth"].status == "skip"


def test_run_all_codex_ollama_user_does_not_require_openai_auth(monkeypatch):
    """有効な利用者が Codex(Ollama) 構成のみで
    あれば、Codex CLI 自体は必須（`codex_cli`／`codex_version` は確認する）だが、OpenAI/Azure の
    認証確認（`health._ai_check_codex`）は一切呼ばれない。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", _fake_effective_agent_from_settings("openai"))
    settings_rows = [
        {"key": "cloud_provider", "value": "openai"},
        {"key": "openai_api_key", "value": "sk-real-key"},
        {"key": "openai_endpoint_seed_version", "value": 1},
    ]
    user_rows = [{"agent": "codex", "codex_model_provider": "ollama", "ollama_url": None, "search_helper": ""}]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=user_rows)
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    _install_ollama_ok(monkeypatch)

    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")

    class _Proc:
        returncode = 0
        stdout = "codex-cli 0.144.1\n"
        stderr = ""
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _Proc())

    def _should_not_be_called(*a, **k):
        raise AssertionError("Codex(Ollama) 専用構成では OpenAI 認証確認を呼んではいけない")
    monkeypatch.setattr(health, "_ai_check_codex", _should_not_be_called)
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", _should_not_be_called)

    results = doctor_checks.run_all(probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["codex_cli"].status == "ok"
    assert by_id["codex_version"].status == "ok"
    assert by_id["codex_auth"].status == "skip"


def test_run_all_active_user_ollama_usage_makes_it_required(monkeypatch):
    """user_settings を read-only で走査し、システム既定に無関係でも有効な利用者がいれば
    ollama を必須扱いにする（`run_all()` を通した end-to-end 確認）。Ollama への実際の疎通は
    現行の実装が使う真の I/O 境界（`sherpa.llm.urlopen_no_redirect`）を差し替えて検証する
    （`health._ai_check_ollama` はもう `check_ollama_probes` の実装経路ではない）。"""
    settings_rows = [
        {"key": "cloud_provider", "value": "openai"},
        {"key": "openai_api_key", "value": "sk-real-key"},
        {"key": "openai_endpoint_seed_version", "value": 1},
    ]
    # システム既定は openai だが、1人だけ ollama を明示選択している。
    user_rows = [{"agent": "openai", "codex_model_provider": None, "ollama_url": None, "search_helper": ""},
                 {"agent": "ollama", "codex_model_provider": None, "ollama_url": None, "search_helper": ""}]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=user_rows)
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    _install_no_codex(monkeypatch)

    def _ollama_unreachable(url, timeout=None):
        raise OSError("Connection refused")
    monkeypatch.setattr("sherpa.llm.urlopen_no_redirect", _ollama_unreachable)

    results = doctor_checks.run_all(probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["llm_ollama"].status == "ng"   # 誰も使っていなければ SKIP のはずが、1人使っている＝NG


def test_run_all_ollama_probe_stays_read_only_when_actually_probed(monkeypatch):
    """`_probe_ollama_usage` の実装（`llm.ollama_url()` の SSRF 許可判定を含む）をモックせずそのまま
    通し、system_settings の受け渡しが `sherpa.store.get_system_settings()`（`_ensure()`→DDL を
    実行しうる高水準 API）へ迂回していないことを確認する。

    `sherpa.llm._allowlisted_hosts()` は `except Exception: entries = []`（DB 不達時の fail-closed
    設計）を持つため、退行時に `get_system_settings()` が投げる例外はそこで握り潰され、`llm_ollama`
    の ok/ng だけでは退行の有無を区別できない。`_install_get_system_settings_spy()` で呼び出し回数を
    記録し、`run_all()` 完了後に `calls == []` を明示確認する（例外の伝播やキャッシュ状態に依存
    しない）。SQL の記録（`executed`）は、想定される SELECT が実際に発行されたことを確認する
    副次的なチェックとして残す（他の項目が発行する SQL 件数が変わっても壊れないよう、総件数の
    完全一致には固定しない）。
    """
    get_system_settings_calls = _install_get_system_settings_spy(monkeypatch)

    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", _fake_effective_agent_from_settings("ollama"))
    settings_rows = [{"key": "cloud_provider", "value": "openai"},
                     {"key": "openai_endpoint_seed_version", "value": 1}]
    executed = _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=[])
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    _install_no_codex(monkeypatch)

    def _ollama_unreachable(url, timeout=None):
        raise OSError("Connection refused")
    monkeypatch.setattr("sherpa.llm.urlopen_no_redirect", _ollama_unreachable)

    results = doctor_checks.run_all(probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["llm_ollama"].status == "ng"
    assert get_system_settings_calls == [], get_system_settings_calls

    sql_texts = [sql.lower() for sql in executed]
    assert any(sql.strip() == "select 1" for sql in sql_texts), executed
    assert any("system_settings" in sql for sql in sql_texts), executed
    assert any("user_settings" in sql for sql in sql_texts), executed
    assert all(sql.strip().startswith("select") for sql in sql_texts), executed


def test_read_active_user_configs_sql_filters_to_active_status(monkeypatch):
    """`_read_active_user_configs_readonly()` が実際に発行する SQL 文そのものに、無効化済み
    （`users.status != 'active'`）利用者を除外する `JOIN users ... WHERE u.status = 'active'`
    が含まれることを確認する（要否判定コード側のロジックではなく、実際の問い合わせ文を検証する）。
    """
    executed = _install_fake_pg(monkeypatch, user_rows=[])
    doctor_checks._read_active_user_configs_readonly()
    assert len(executed) == 1
    sql_lower = executed[0].lower()
    assert "user_settings" in sql_lower
    assert "join users" in sql_lower
    assert "status" in sql_lower and "active" in sql_lower


def test_read_active_user_configs_sql_does_not_select_raw_key_values(monkeypatch):
    """個人キーの値そのものは SELECT しない（`IS NOT NULL AND <> ''` の
    真偽値へ SQL 側で畳んだ列だけを見る）。"""
    executed = _install_fake_pg(monkeypatch, user_rows=[])
    doctor_checks._read_active_user_configs_readonly()
    sql_lower = executed[0].lower()
    assert "has_openai_key" in sql_lower
    assert "has_gemini_key" in sql_lower
    assert "has_bedrock_key" in sql_lower
    # 列名そのもの（例: "us.openai_api_key"）を SELECT リストへ生で載せていないこと（IS NOT NULL の
    # 判定式の中に現れるのは許すが、値としてそのまま返す形は禁止）。
    assert "select us.openai_api_key," not in sql_lower
    assert "select us.openai_api_key ," not in sql_lower


def test_read_active_user_configs_sql_has_key_matches_production_truthy(monkeypatch):
    """`has_{provider}_key` は本番の truthy 判定（`sherpa.keys.resolve_api_key()` の
    `if personal: return personal`）と一致させる: `NULL`／空文字列以外は一律「あり」として畳む
    （プレースホルダ・空白のみの値を除外する `btrim()`／`= ANY(プレースホルダ配列)` は使わない＝
    本番が実際に送信を試みて必ず失敗する構成を doctor だけが「未使用」と誤認する穴を作らない）。"""
    executed = _install_fake_pg(monkeypatch, user_rows=[])
    doctor_checks._read_active_user_configs_readonly()
    sql_lower = executed[0].lower()
    for field in ("openai_api_key", "gemini_api_key", "bedrock_api_key"):
        assert f"us.{field} is not null and us.{field} <> ''" in sql_lower
    assert "btrim(" not in sql_lower
    assert "= any(" not in sql_lower



def test_run_all_only_executes_select_statements(monkeypatch):
    """読み取り専用契約の end-to-end 確認: `run_all()` 1回の実行で発行される全 SQL が
    `SELECT` 単文のみであること（`_FakePgConn` 自体も非 SELECT を即座に拒否するが、ここでは
    実際に収集した文を積極的に検証し、単なる「拒否されなかった」以上の保証にする）。あわせて
    `store.get_system_settings()`（`_ensure()`→DDL を実行しうる高水準 API）が一切呼ばれないことを
    `_install_get_system_settings_spy()` の呼び出し回数記録で確認する（呼び出し元の一部が例外を
    握り潰す設計でも、記録自体は例外の伝播に左右されない）。"""
    get_system_settings_calls = _install_get_system_settings_spy(monkeypatch)

    settings_rows = [{"key": "cloud_provider", "value": "openai"},
                     {"key": "openai_api_key", "value": "sk-real-key"},
                     {"key": "openai_endpoint_seed_version", "value": 1}]
    executed = _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=[])
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    _install_no_codex(monkeypatch)
    _install_ollama_ok(monkeypatch)

    doctor_checks.run_all(probe_cloud=False)

    assert executed, "SQL が1件も発行されていない（フェイクの配線を確認）"
    assert all(sql.strip().lower().startswith("select") for sql in executed), executed
    assert get_system_settings_calls == [], get_system_settings_calls


def test_run_all_does_not_crash_when_model_catalog_is_broken(monkeypatch):
    """`run_all()` の end-to-end 確認: `model_catalog.resolve_model()`（Azure/custom のデプロイ名
    静的検査・Ollama 用途解決・Codex(Azure) 検査がいずれも内部で呼ぶ）が壊れた設定
    （例: `model_catalog.openai.chat.allowed=1` のような型不正）で未知の例外を投げても、
    `run_all()` 全体が未捕捉の traceback で中断せず、影響を受けた個々のチェックが単独の NG に
    収まることを確認する（1項目の設定不備で診断ツール全体が壊れないことを固定する）。"""
    from sherpa import model_catalog

    def _boom(*a, **k):
        raise TypeError("model_catalog.openai.chat.allowed=1 のような壊れた設定")
    monkeypatch.setattr(model_catalog, "resolve_model", _boom)

    settings_rows = [{"key": "cloud_provider", "value": "openai"},
                     {"key": "openai_endpoint_kind", "value": "azure"},
                     {"key": "openai_base_url", "value": "https://x.openai.azure.com/openai/v1"},
                     {"key": "openai_api_key", "value": "sk-real-key"},
                     {"key": "openai_endpoint_seed_version", "value": 1}]
    _install_fake_pg(monkeypatch, settings_rows=settings_rows, user_rows=[])
    _install_fake_neo4j(monkeypatch)
    _install_healthy_es(monkeypatch)
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")

    class _Proc:
        returncode = 0
        stdout = "codex-cli 0.144.1\n"
        stderr = ""
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _Proc())

    results = doctor_checks.run_all(probe_cloud=False)   # 例外を投げずに完走することが前提
    by_id = {r.id: r for r in results}
    assert by_id["codex_auth"].status == "ng"
    assert by_id["llm_ollama"].status == "ng"


def test_fake_pg_conn_rejects_non_select_statements():
    """フィクスチャ自体のメタテスト: `_FakePgConn` は SELECT 以外・複文を即座に拒否する
    （このフェイクが将来これらを静かに受理する退行を検出する）。"""
    conn = _FakePgConn([], [])
    for sql in ("UPDATE system_settings SET value = '1'", "DELETE FROM user_settings",
                "CREATE TABLE x (id int)", "ALTER TABLE users ADD COLUMN y text",
                "DROP TABLE user_settings", "INSERT INTO system_settings VALUES (1)",
                "SELECT 1; DELETE FROM user_settings", "SELECT 1; SELECT 2"):
        with pytest.raises(AssertionError):
            conn.execute(sql)


# ---------------------------------------------------------------------------
# doctor.sh（bash エントリポイント）の実行経路
# ---------------------------------------------------------------------------

def _doctor_sh_env(tmp_path, **extra: str) -> dict[str, str]:
    """`doctor.sh` サブプロセス用の最小 env。`PYTHON_BIN` にこのテストを実行しているのと同じ
    インタプリタ（`sys.executable`）を明示することで、`.venv` を持たない worktree でも
    `psycopg`/`neo4j` 等の依存が確実に見える状態で起動する（`doctor.sh` は `PYTHON_BIN` が
    未設定の場合のみ `.venv`/`python3` を自動検出する）。`SHERPA_ENV_FILE` は既定で
    **実在する空ファイル**を指す（`doctor.sh` は明示指定されたパスが存在しないと今はエラー
    終了するため、単に存在しないパス名を使う旧来のやり方は使えない＝リポジトリ直下に実 `.env`
    があってもテストへ混入させないための空ファイルにする）。"""
    empty_env_file = tmp_path / "empty.env"
    if not empty_env_file.exists():
        empty_env_file.write_text("", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHON_BIN": sys.executable,
           "SHERPA_ENV_FILE": str(empty_env_file)}
    env.update(extra)
    return env


def test_doctor_sh_runs_end_to_end_against_unreachable_hosts(tmp_path):
    """`bash -n`（構文検査のみ）でなく、実サブプロセスとして起動し、env 読み込み→python 実行の
    配線が最後まで動くことを確認する。ローカルの未使用ポート（127.0.0.1:1・接続即座に拒否）を
    使い、実ネットワークのタイムアウト待ちを避ける。"""
    env_file = tmp_path / "doctor_test.env"
    env_file.write_text(
        "\n".join([
            "PGHOST=127.0.0.1",
            "PGPORT=1",
            "NEO4J_URI=bolt://127.0.0.1:1",
            "ES_URL=http://127.0.0.1:1",
        ]) + "\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [str(DOCTOR_SH)],
        cwd=str(ROOT),
        env=_doctor_sh_env(tmp_path, SHERPA_ENV_FILE=str(env_file)),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr   # PostgreSQL 到達不可のため NG が立つ
    assert "PostgreSQL" in proc.stdout
    assert "NG" in proc.stdout


def test_doctor_sh_passes_probe_cloud_env_through_to_child_process(tmp_path):
    """`PROBE_CLOUD` が `doctor.sh` から実際に子プロセスの環境変数として渡ることを、業務ロジックの
    分岐から独立して固定する。`PYTHON_BIN` を実際の検査ロジック（`doctor_checks.py`）の代わりに
    自分の環境変数をそのまま出力するだけの偽ランナーに差し替え、フラグ無し＝子プロセスで未設定・
    フラグあり＝子プロセスで設定済み、の両方を1テストで確認する（`doctor.sh` は
    `exec "$PYTHON_BIN" "$ROOT/scripts/doctor_checks.py" "$@"` で常に `doctor_checks.py` を
    第一引数として渡すが、偽ランナーはそれを無視して env だけを観測する）。"""
    fake_python = tmp_path / "fake_python_runner.sh"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        'echo "PROBE_CLOUD=${PROBE_CLOUD:-<unset>}"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    def _run(probe_cloud_value: str | None) -> str:
        env = _doctor_sh_env(tmp_path, PYTHON_BIN=str(fake_python))
        if probe_cloud_value is not None:
            env["PROBE_CLOUD"] = probe_cloud_value
        proc = subprocess.run([str(DOCTOR_SH)], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout.strip()

    assert _run(None) == "PROBE_CLOUD=<unset>"
    assert _run("1") == "PROBE_CLOUD=1"


def test_doctor_sh_errors_when_explicit_env_file_does_not_exist(tmp_path):
    """SHERPA_ENV_FILE を明示指定したのにそのパスが存在しない場合、黙って続行せずエラー終了する
    （沈黙した誤設定が的外れな NG として現れ、原因調査を誤らせることを防ぐ）。"""
    missing = tmp_path / "does-not-exist.env"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHON_BIN": sys.executable,
           "SHERPA_ENV_FILE": str(missing)}
    proc = subprocess.run([str(DOCTOR_SH)], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 2
    assert "SHERPA_ENV_FILE" in proc.stderr
    assert str(missing) in proc.stderr


def test_doctor_sh_errors_when_explicit_env_file_is_a_directory(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHON_BIN": sys.executable,
           "SHERPA_ENV_FILE": str(tmp_path)}
    proc = subprocess.run([str(DOCTOR_SH)], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 2
    assert "SHERPA_ENV_FILE" in proc.stderr


def test_doctor_sh_proceeds_when_default_env_file_is_absent(tmp_path):
    """既定値（`SHERPA_ENV_FILE` 未指定時の `$ROOT/.env`）が存在しないのは正常として許容する
    （`SHERPA_ENV_FILE` を明示的に unset のまま起動する）。"""
    fake_python = tmp_path / "fake_python_runner.sh"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        'echo "ran ok"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHON_BIN": str(fake_python)}
    proc = subprocess.run([str(DOCTOR_SH)], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "ran ok"
