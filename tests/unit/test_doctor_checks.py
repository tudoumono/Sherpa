"""`scripts/doctor_checks.py`（`make doctor` の検査本体）の単体テスト。

外部サービス（Postgres/Neo4j/ES/実 API）には一切触れない。ストア疎通は `sherpa.health` の
`_check_one`／`_ai_check_*` を差し替えて OK/NG/SKIP の分岐だけを検証する（`sherpa.health` 自体の
挙動は `tests/unit/test_health.py` が別途固定済み）。接続先設定の妥当性（`check_openai_endpoint`）は
`check_production_openai_probe.probe()`／`sherpa.keys` が system_settings dict だけで完結する
純粋な判定のため、実際にそれらを渡して検証する（モック不要）。必須判定（`_codex_required`／
`_resolve_ollama_usages`／`_cloud_provider_consumed`）は環境の実際の Codex CLI 有無に結果が
左右されないよう、`sherpa.agent_constructs.effective_agent` 等を明示的にモックして決定的にする。
読み取り専用 SELECT（`_fetch_system_settings_readonly`／`_read_active_user_configs_readonly`）は
本ファイルではモックのみ検証し、実際に DB へ触れて DDL を発火させないことは
`tests/contract/test_doctor_integration.py` が別途固定する。
"""
from __future__ import annotations

import json
import logging

import pytest

import scripts.doctor_checks as doctor_checks
from sherpa import health


class _FakeOllamaResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _tags_response(*names: str) -> bytes:
    return json.dumps({"models": [{"name": n} for n in names]}).encode()


# ---------------------------------------------------------------------------
# 0. 共通境界（秘密マスク・制御文字/ANSI除去・長さ制限・ログ経由の伏せ字）
# ---------------------------------------------------------------------------

def test_sanitize_text_masks_known_secret_patterns():
    text = "failed with Authorization: Bearer sk-should-not-leak-123456789012345678"
    out = doctor_checks._sanitize_text(text)
    assert "sk-should-not-leak" not in out


def test_sanitize_text_strips_ansi_and_newlines():
    text = "line1\x1b[31mRED\x1b[0m\nline2\ttabbed"
    out = doctor_checks._sanitize_text(text)
    assert "\x1b" not in out
    assert "\n" not in out
    assert "\t" not in out
    assert "line1" in out and "line2" in out


def test_sanitize_text_strips_ansi_interleaved_inside_secret_before_masking():
    """ANSI エスケープを秘密パターンの途中に挟み込んだ入力でも、除去→マスクの順で正しく伏せる
    （マスク→除去の順だと、除去後に元のトークンが「再結合」して見える不具合を実測で確認済み）。"""
    text = "Authorization: Bearer \x1b[0msk-should-not-leak-123456789012345678"
    out = doctor_checks._sanitize_text(text)
    assert "sk-should-not-leak" not in out


def test_sanitize_text_truncates_long_input():
    out = doctor_checks._sanitize_text("x" * 10000)
    assert len(out) <= doctor_checks._MAX_DETAIL_CHARS + len("…（省略）")




class _FakeReadonlyCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _FakeReadonlyConn:
    """`_connect(**kw)` の呼び出し kwargs を記録するだけの最小フェイク（`sherpa.store.db._connect`
    自体をモックし、`psycopg` レベルまで踏み込まない＝`_connect()` へ渡された `connect_timeout`／
    `options` を検証する専用の軽量フェイク）。"""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return _FakeReadonlyCursor(self._rows)


def test_fetch_system_settings_readonly_sets_statement_timeout(monkeypatch):
    """`health._ping_postgres` と同じ方式・値で `statement_timeout` を設定する（`connect_timeout`
    は接続確立のみをカバーするため、発行した SELECT 自体にも上限を課す）。"""
    from sherpa.store import db as db_mod
    captured = {}

    def _fake_connect(**kw):
        captured.update(kw)
        return _FakeReadonlyConn([])
    monkeypatch.setattr(db_mod, "_connect", _fake_connect)
    doctor_checks._fetch_system_settings_readonly()
    assert captured.get("connect_timeout") == doctor_checks._PG_READONLY_TIMEOUT
    assert captured.get("options") == doctor_checks._PG_READONLY_OPTIONS
    assert "statement_timeout=" in captured["options"]


def test_read_active_user_configs_readonly_sets_statement_timeout(monkeypatch):
    from sherpa.store import db as db_mod
    captured = {}

    def _fake_connect(**kw):
        captured.update(kw)
        return _FakeReadonlyConn([])
    monkeypatch.setattr(db_mod, "_connect", _fake_connect)
    doctor_checks._read_active_user_configs_readonly()
    assert captured.get("connect_timeout") == doctor_checks._PG_READONLY_TIMEOUT
    assert captured.get("options") == doctor_checks._PG_READONLY_OPTIONS


def test_check_result_detail_is_sanitized_on_construction():
    r = doctor_checks.CheckResult("id", "label", "ng", "line1\nline2\x1b[31m")
    assert "\n" not in r.detail
    assert "\x1b" not in r.detail


_GUARDED_CHECK_FIXED_DETAIL = "この検査自体が予期しないエラーで失敗しました（設定を確認してください）"


def test_guarded_check_detail_is_fixed_literal_not_exception_text():
    """`_guarded_check` は捕まえた例外の**種類・メッセージを一切問わず**、常に同じ固定文言を
    返す（自由文を一切出さない・fail-closed）。例外メッセージに実キーらしき値が入っていても
    `CheckResult.detail` には一切現れないことを、部分一致ではなく**完全一致**で固定する
    （「secretという単語が含まれないか」ではなく「detail がまさにこの固定文字列そのものか」を
    確認する方が、将来 `_guarded_check` の実装が変わって別の自由文を混ぜてしまう退行を
    より確実に検出できる）。"""
    secret = "sk-realsecretvalue1234567890ABCDEFGH"

    @doctor_checks._guarded_check("some_check", "何かの検査")
    def _boom(*a, **k):
        raise RuntimeError(f"leaked secret in exception message: {secret}")

    r = _boom()
    assert r.id == "some_check"
    assert r.label == "何かの検査"
    assert r.status == "ng"
    assert r.detail == _GUARDED_CHECK_FIXED_DETAIL
    assert secret not in r.detail


def test_guarded_check_detail_fixed_regardless_of_exception_type():
    """例外の型が変わっても（`TypeError`／`ValueError`／カスタム例外）、固定文言は変わらない。"""
    for exc in (TypeError("bad type"), ValueError("bad value"), KeyError("missing")):
        @doctor_checks._guarded_check("some_check", "何かの検査")
        def _boom(exc=exc):
            raise exc

        r = _boom()
        assert r.detail == _GUARDED_CHECK_FIXED_DETAIL


def test_guarded_check_passes_through_on_success():
    """例外が起きなければラップ前の `CheckResult` をそのまま返す（副作用が無い）。"""
    @doctor_checks._guarded_check("some_check", "何かの検査")
    def _ok(*a, **k):
        return doctor_checks.CheckResult("some_check", "何かの検査", "ok", "問題ありません")

    r = _ok()
    assert r.status == "ok"
    assert r.detail == "問題ありません"


def test_check_codex_azure_compat_unexpected_exception_detail_is_fixed_literal(monkeypatch):
    """`_check_codex_azure_compat`（`@_guarded_check` 適用先の実例）でも、例外メッセージに
    実キーが混入していても `CheckResult.detail` は固定文言のまま（完全一致）で、例外文字列は
    一切現れない。"""
    secret = "sk-realsecretvalue1234567890ABCDEFGH"

    def _boom(s, **k):
        raise TypeError(f"model_catalog broken, leaked key: {secret}")
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", _boom)
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1",
              "openai_api_key": "sk-real-key"}
    r = doctor_checks._check_codex_azure_compat(sys_s, None, required=True, probe_cloud=False)
    assert r.status == "ng"
    assert r.detail == _GUARDED_CHECK_FIXED_DETAIL
    assert secret not in r.detail


def test_log_redaction_replaces_message_from_health_logger(caplog):
    """パターンベースの秘密マスクは URL 構造を伴わない平文（例: DSN のパスワード片）を検出できない
    ため、ログ経由の経路は本文を丸ごと固定文言へ差し替える（実測に基づく設計）。"""
    logger = logging.getLogger("sherpa.health")
    with doctor_checks._log_redaction_active():
        with caplog.at_level(logging.WARNING, logger="sherpa.health"):
            logger.warning("dsn password=my secret password here")
        assert "my secret password" not in caplog.text
        assert "（doctor 実行中のため詳細は省略" in caplog.text


def test_log_redaction_covers_any_sherpa_submodule_logger(caplog):
    """`sherpa.health` 専用ではなく、`sherpa.*` の任意のロガー（例: `sherpa.agent_constructs`）を
    塞ぐ（`Logger.addFilter` はそのロガー自身にしか効かず、個別に登録すると新しいモジュールの
    ログを取りこぼすため、`Logger.callHandlers` 差し替えで一括して塞ぐ設計）。"""
    logger = logging.getLogger("sherpa.agent_constructs")
    with doctor_checks._log_redaction_active():
        with caplog.at_level(logging.WARNING, logger="sherpa.agent_constructs"):
            logger.warning("SHERPA_AGENT=%r raw secret leak test", "some-value")
        assert "raw secret leak test" not in caplog.text
        assert "（doctor 実行中のため詳細は省略" in caplog.text


def test_log_redaction_does_not_affect_non_sherpa_loggers(caplog):
    logger = logging.getLogger("some_other_lib")
    with doctor_checks._log_redaction_active():
        with caplog.at_level(logging.WARNING, logger="some_other_lib"):
            logger.warning("unrelated message stays intact")
        assert "unrelated message stays intact" in caplog.text


def test_log_redaction_covers_anthropic_sdk_debug_logger(caplog):
    """Bedrock 実プローブが使う `anthropic` SDK は import 時に無条件で `setup_logging()` を呼び、
    `ANTHROPIC_LOG=debug`（運用者が設定しうる一般的な SDK デバッグフラグ）が立っていると
    `anthropic`／`httpx` の各ロガーを DEBUG へ引き上げてリクエスト/レスポンス（実キーを含みうる
    ヘッダー等）をそのままログへ出す。`sherpa.*` 専用の差し替えだとこの経路は素通りするため、
    `anthropic._base_client` のような子ロガーも一括して塞げることを確認する。"""
    logger = logging.getLogger("anthropic._base_client")
    with doctor_checks._log_redaction_active():
        with caplog.at_level(logging.DEBUG, logger="anthropic._base_client"):
            logger.debug("HTTP Request: POST ... Authorization: Bearer sk-realsecretvalue")
        assert "sk-realsecretvalue" not in caplog.text
        assert "（doctor 実行中のため詳細は省略" in caplog.text


def test_log_redaction_covers_httpx_debug_logger(caplog):
    """`anthropic` SDK の debug 設定は `httpx`（下請け HTTP クライアント）のロガーも DEBUG へ
    引き上げる（`anthropic/_utils/_logs.py::setup_logging()`）ため、`httpx` 名前空間も対象に
    含める。"""
    logger = logging.getLogger("httpx")
    with doctor_checks._log_redaction_active():
        with caplog.at_level(logging.DEBUG, logger="httpx"):
            logger.debug("request headers include Authorization: Bearer sk-realsecretvalue")
        assert "sk-realsecretvalue" not in caplog.text
        assert "（doctor 実行中のため詳細は省略" in caplog.text


def test_log_redaction_covers_botocore_auth_debug_logger(caplog):
    """`anthropic` の Bedrock 実装は SigV4 署名に `botocore.auth.SigV4Auth` を使う。
    `botocore/auth.py` は DEBUG レベルで `CanonicalRequest`（一時セッショントークン
    `X-Amz-Security-Token` を含みうる生ヘッダー一式）をそのままログへ出す契約のため、
    `botocore`（の子ロガー `botocore.auth`）も対象に含める。"""
    logger = logging.getLogger("botocore.auth")
    with doctor_checks._log_redaction_active():
        with caplog.at_level(logging.DEBUG, logger="botocore.auth"):
            logger.debug("CanonicalRequest:\nX-Amz-Security-Token:sk-realsecretvalue")
        assert "sk-realsecretvalue" not in caplog.text
        assert "（doctor 実行中のため詳細は省略" in caplog.text


def test_log_redaction_works_even_with_a_preexisting_root_handler(caplog):
    """呼び出し元プロセスが既に root ハンドラを設定済みの環境（`logging.lastResort` が使われない
    ケース）でも確実に効くこと（`Logger.callHandlers` を差し替える設計はハンドラの有無に依存
    しない）。"""
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    try:
        logger = logging.getLogger("sherpa.health")
        with doctor_checks._log_redaction_active():
            with caplog.at_level(logging.WARNING, logger="sherpa.health"):
                logger.warning("dsn password=my secret password here (root handler present)")
            assert "my secret password" not in caplog.text
    finally:
        root.removeHandler(handler)


def test_log_redaction_is_scoped_and_reversible():
    """`run_all()` の呼び出し外では差し替えが残らない（他のテスト・プロセスへ副作用を持ち込まない）。"""
    original = logging.Logger.callHandlers
    with doctor_checks._log_redaction_active():
        assert logging.Logger.callHandlers is not original
    assert logging.Logger.callHandlers is original


def test_log_redaction_reentrant_overlapping_activations_restore_once():
    """`run_all()` が重なって呼ばれた場合（多重起動・入れ子）でも、外側の `with` が抜けるまでは
    差し替えを維持し、最も外側が抜けたときだけ元へ戻す（深さカウント方式・`_log_redaction_active()`
    参照）。"""
    original = logging.Logger.callHandlers
    with doctor_checks._log_redaction_active():
        inner_wrapper = logging.Logger.callHandlers
        assert inner_wrapper is not original
        with doctor_checks._log_redaction_active():
            assert logging.Logger.callHandlers is inner_wrapper
        # 内側が抜けても、外側がまだ生きている間は差し替えたまま。
        assert logging.Logger.callHandlers is inner_wrapper
    assert logging.Logger.callHandlers is original


def test_log_redaction_concurrent_threads_restore_correctly():
    """複数スレッドから重なって `_log_redaction_active()` に入っても、ロック＋参照カウントにより
    最後の1つが抜けるまで差し替えを維持し、その後は必ず元へ戻る（再入安全性のスレッド版確認）。"""
    import threading
    import time

    original = logging.Logger.callHandlers
    barrier = threading.Barrier(3)
    errors: list[Exception] = []

    def worker():
        try:
            with doctor_checks._log_redaction_active():
                barrier.wait(timeout=5)
                assert logging.Logger.callHandlers is not original
                time.sleep(0.01)
        except Exception as e:  # pragma: no cover - surfaced via errors list
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors
    assert logging.Logger.callHandlers is original


def test_log_redaction_clears_exc_info_and_stack_info(caplog):
    """トレースバック（`exc_info`／`exc_text`／`stack_info`）は `record.msg`／`record.args` とは
    独立に `Formatter.format()` がレンダリングするため、これらも消去しないと例外メッセージ経由で
    秘密が漏れる。"""
    logger = logging.getLogger("sherpa.health")
    with doctor_checks._log_redaction_active():
        with caplog.at_level(logging.ERROR, logger="sherpa.health"):
            try:
                raise ValueError("dsn password=my secret password here")
            except ValueError:
                logger.exception("failed")
        assert "my secret password" not in caplog.text
        for record in caplog.records:
            assert record.exc_info is None
            assert record.exc_text is None
            assert record.stack_info is None


def test_log_redaction_clears_stack_info_that_was_actually_set(caplog):
    """`logger.exception(...)` は既定では `stack_info` を立てない（`exc_info` のみ）ため、単に
    `record.stack_info is None` を確認するだけでは「元々 None だった」のか「消去した」のか区別が
    付かない。ここでは `stack_info=True` を明示指定して実際に値が入る経路を使い、消去の効果を
    実質的に検証する。"""
    logger = logging.getLogger("sherpa.health")
    with doctor_checks._log_redaction_active():
        with caplog.at_level(logging.WARNING, logger="sherpa.health"):
            logger.warning("dsn password=my secret password here", stack_info=True)
        assert "my secret password" not in caplog.text
        assert caplog.records
        for record in caplog.records:
            assert record.stack_info is None


def test_log_redaction_clears_preexisting_exc_text_on_the_record():
    """`Formatter.format()` は初回整形時に `record.exc_text` へ結果をキャッシュし、同じレコードを
    別のハンドラが再整形する際にそのキャッシュをそのまま使う契約を持つ（`logging` 標準の挙動）。
    ラッパーに渡ってきた時点で既に `exc_text` が埋まっているレコード（キャッシュ経由で先に
    レンダリング済みの場合）でも、`msg`／`args` の差し替えとは独立に必ず消去されることを、
    `logging` の実際の呼び出し経路を介さず `LogRecord` を直接組み立てて確認する。"""
    calls = []

    def fake_original(self, record):
        calls.append(record)

    wrapper = doctor_checks._make_redacting_call_handlers(fake_original)
    logger = logging.getLogger("sherpa.health")
    record = logger.makeRecord("sherpa.health", logging.WARNING, __file__, 0, "msg", (), None)
    record.exc_text = "Traceback: dsn password=my secret password here"
    wrapper(logger, record)
    assert calls == [record]
    assert record.exc_text is None


def test_log_redaction_wrapper_survives_capture_being_cleared_by_a_later_exit():
    """再入・並行安全性の核心: ラッパー関数は呼び出し先（`original`）を自分の**クロージャに固定**
    して持つ（`_make_redacting_call_handlers` 参照）。あるラッパーが呼び出し中の間に、別のスレッド
    （最後の `with _log_redaction_active():` を抜けた側）が復元処理で `_log_redaction_captured_original`
    （グローバルの一時保管場所）を `None` に戻しても、既に実行中のラッパーはそのグローバルを一切
    参照しないため影響を受けない（グローバル変数を実行時に読みに行く実装だと、この状況で
    `TypeError: 'NoneType' object is not callable` になる）。"""
    calls = []

    def fake_original(self, record):
        calls.append((self, record))

    wrapper = doctor_checks._make_redacting_call_handlers(fake_original)
    # 「別スレッドの最終 exit がグローバルの捕捉値を None 化した後」を模す。
    doctor_checks._log_redaction_captured_original = None

    logger = logging.getLogger("sherpa.health")
    record = logger.makeRecord("sherpa.health", logging.WARNING, __file__, 0, "secret msg", (), None)
    wrapper(logger, record)   # NoneType not callable にならないこと
    assert calls == [(logger, record)]


# ---------------------------------------------------------------------------
# 1. ストア疎通
# ---------------------------------------------------------------------------

def test_check_postgres_ok(monkeypatch):
    monkeypatch.setattr(health, "_check_one", lambda *a: {"ok": True, "detail": None, "hint": None})
    r = doctor_checks.check_postgres()
    assert r.status == "ok"


def test_check_postgres_ng(monkeypatch):
    monkeypatch.setattr(health, "_check_one",
                         lambda *a: {"ok": False, "detail": "接続拒否（サービス停止の可能性）（ConnectionRefusedError）",
                                     "hint": "make up"})
    r = doctor_checks.check_postgres()
    assert r.status == "ng"
    assert "connection refused" not in r.detail.lower()   # 生の例外文字列を出さない（health._classify 経由）


def test_check_neo4j_ok(monkeypatch):
    monkeypatch.setattr(health, "_check_one", lambda *a: {"ok": True, "detail": None, "hint": None})
    assert doctor_checks.check_neo4j().status == "ok"


def test_check_neo4j_ng(monkeypatch):
    monkeypatch.setattr(health, "_check_one",
                         lambda *a: {"ok": False, "detail": "タイムアウト（TimeoutError）", "hint": "make up"})
    assert doctor_checks.check_neo4j().status == "ng"


def test_check_es_connect_ok(monkeypatch):
    monkeypatch.setattr(doctor_checks, "_es_get", lambda path, timeout=5.0: {"version": {"number": "8.19.20"}})
    r = doctor_checks.check_es_connect()
    assert r.status == "ok"
    assert "8.19.20" in r.detail


def test_check_es_connect_ng_on_transport_error(monkeypatch):
    def _boom(path, timeout=5.0):
        raise RuntimeError("refused")
    monkeypatch.setattr(doctor_checks, "_es_get", _boom)
    r = doctor_checks.check_es_connect()
    assert r.status == "ng"


def test_check_es_connect_ng_when_response_is_null(monkeypatch):
    """JSON null 応答は「不明でも OK」ではなく NG（診断ツールが安全側で失敗を示す）。"""
    monkeypatch.setattr(doctor_checks, "_es_get", lambda path, timeout=5.0: None)
    r = doctor_checks.check_es_connect()
    assert r.status == "ng"


def test_check_es_connect_ng_when_response_is_a_number(monkeypatch):
    """`.get()` が無い型（int）が返ってきても例外を外へ漏らさず CheckResult を返す。"""
    monkeypatch.setattr(doctor_checks, "_es_get", lambda path, timeout=5.0: 42)
    r = doctor_checks.check_es_connect()
    assert r.status == "ng"


def test_check_es_connect_ng_when_version_number_missing(monkeypatch):
    monkeypatch.setattr(doctor_checks, "_es_get", lambda path, timeout=5.0: {"tagline": "You Know, for Search"})
    r = doctor_checks.check_es_connect()
    assert r.status == "ng"


def test_check_es_kuromoji_skips_when_es_unreachable():
    r = doctor_checks.check_es_kuromoji(es_ok=False)
    assert r.status == "skip"


def test_check_es_kuromoji_ok_when_plugin_present(monkeypatch):
    monkeypatch.setattr(doctor_checks, "_es_get",
                         lambda path, timeout=5.0: [{"component": "analysis-kuromoji"}])
    r = doctor_checks.check_es_kuromoji(es_ok=True)
    assert r.status == "ok"


def test_check_es_kuromoji_ng_when_plugin_absent(monkeypatch):
    monkeypatch.setattr(doctor_checks, "_es_get", lambda path, timeout=5.0: [{"component": "analysis-icu"}])
    r = doctor_checks.check_es_kuromoji(es_ok=True)
    assert r.status == "ng"


def test_check_es_kuromoji_ng_on_substring_only_match(monkeypatch):
    """完全一致で判定する（部分一致だと無関係な将来のプラグイン名にも誤って反応しうる）。"""
    monkeypatch.setattr(doctor_checks, "_es_get",
                         lambda path, timeout=5.0: [{"component": "not-analysis-kuromoji-really"}])
    r = doctor_checks.check_es_kuromoji(es_ok=True)
    assert r.status == "ng"


def test_check_es_kuromoji_ng_when_response_not_a_list(monkeypatch):
    monkeypatch.setattr(doctor_checks, "_es_get", lambda path, timeout=5.0: {})
    r = doctor_checks.check_es_kuromoji(es_ok=True)
    assert r.status == "ng"


def test_check_es_kuromoji_ng_when_plugin_list_unreadable(monkeypatch):
    def _boom(path, timeout=5.0):
        raise RuntimeError("boom")
    monkeypatch.setattr(doctor_checks, "_es_get", _boom)
    r = doctor_checks.check_es_kuromoji(es_ok=True)
    assert r.status == "ng"


# ---------------------------------------------------------------------------
# 2. 設定の妥当性（読み取り専用 SELECT・env 候補・Bedrock SigV4・不要キー要求の抑制・個人キー）
# ---------------------------------------------------------------------------

def test_load_system_settings_skips_when_pg_down():
    check, sys_s = doctor_checks._load_system_settings(False)
    assert check.status == "skip"
    assert sys_s is None


def test_load_system_settings_ok(monkeypatch):
    monkeypatch.setattr(doctor_checks, "_fetch_system_settings_readonly",
                         lambda: {"cloud_provider": "openai"})
    check, sys_s = doctor_checks._load_system_settings(True)
    assert check.status == "ok"
    assert sys_s == {"cloud_provider": "openai"}


def test_load_system_settings_ng_on_read_failure_not_skip(monkeypatch):
    """読み取り専用 SELECT 自体の失敗は SKIP でなく NG にする（DDL 権限が無い構成で全項目 SKIP・
    exit 0 になる穴を塞ぐ）。"""
    def _boom():
        raise RuntimeError("permission denied for table system_settings")
    monkeypatch.setattr(doctor_checks, "_fetch_system_settings_readonly", _boom)
    check, sys_s = doctor_checks._load_system_settings(True)
    assert check.status == "ng"
    assert sys_s is None
    assert "permission denied" not in check.detail.lower()   # health._classify 経由で丸められる


def test_load_active_user_configs_skips_when_pg_down():
    check, rows = doctor_checks._load_active_user_configs(False)
    assert check.status == "skip"
    assert rows is None


def test_load_active_user_configs_ok(monkeypatch):
    monkeypatch.setattr(doctor_checks, "_read_active_user_configs_readonly",
                         lambda: [{"agent": "openai", "codex_model_provider": None,
                                  "ollama_url": None, "search_helper": "",
                                  "has_openai_key": False, "has_gemini_key": False,
                                  "has_bedrock_key": False}])
    check, rows = doctor_checks._load_active_user_configs(True)
    assert check.status == "ok"
    assert rows is not None and len(rows) == 1


def test_load_active_user_configs_ng_on_read_failure(monkeypatch):
    def _boom():
        raise RuntimeError("permission denied for table user_settings")
    monkeypatch.setattr(doctor_checks, "_read_active_user_configs_readonly", _boom)
    check, rows = doctor_checks._load_active_user_configs(True)
    assert check.status == "ng"
    assert rows is None


def test_check_openai_endpoint_skips_without_settings():
    assert doctor_checks.check_openai_endpoint(None).status == "skip"


def test_check_openai_endpoint_ok_before_first_boot():
    r = doctor_checks.check_openai_endpoint({})
    assert r.status == "ok"


def test_check_openai_endpoint_ng_before_first_boot_when_env_candidate_invalid(monkeypatch):
    """NO_MARKER（初回シード前）でも env 候補（次回起動時に取り込まれる値）自体の妥当性を検証する
    （Azure/custom の base_url 欠落・危険な URL・未知 kind を見逃さない）。"""
    monkeypatch.setenv("SHERPA_OPENAI_ENDPOINT_KIND", "azure")   # base_url 無しの azure は不整合
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    r = doctor_checks.check_openai_endpoint({})
    assert r.status == "ng"


def test_check_openai_endpoint_ok_when_seeded():
    r = doctor_checks.check_openai_endpoint({"openai_endpoint_seed_version": 1})
    assert r.status == "ok"


def test_check_openai_endpoint_ng_when_kind_invalid():
    r = doctor_checks.check_openai_endpoint(
        {"openai_endpoint_seed_version": 1, "openai_endpoint_kind": "bogus"})
    assert r.status == "ng"


def test_openai_endpoint_status_merges_valid_env_candidate_when_not_yet_seeded(monkeypatch):
    """`sys_s` に起動時シードのマーカーがまだ無く（NO_MARKER）、env に妥当な Azure 候補がある
    場合、`effective_sys_s` はその候補を重ねたコピーになる（実際に起動すればこの値になる＝以後の
    チェックはこの値を使う）。元の `sys_s` は書き換えない。"""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x.openai.azure.com/openai/v1")
    monkeypatch.delenv("SHERPA_OPENAI_ENDPOINT_KIND", raising=False)
    sys_s = {"openai_api_key": "sk-azure-key-1234567890"}
    r = doctor_checks._openai_endpoint_status(sys_s)
    assert r["status"] == "ok"
    assert r["effective_sys_s"]["openai_base_url"] == "https://x.openai.azure.com/openai/v1"
    assert r["effective_sys_s"] is not sys_s
    assert "openai_base_url" not in sys_s


def test_openai_endpoint_status_db_value_wins_over_env_candidate_when_key_already_exists(monkeypatch):
    """本番のシード（`store.seed_system_settings_once`）は既存行を絶対に上書きしない（行単位の
    `WHERE NOT EXISTS`）。`sys_s` に既に `openai_endpoint_kind`／`openai_base_url` の行がある
    場合、env が別の値を要求してもそちらは無視し、DB（`sys_s`）の値をそのまま使う。"""
    monkeypatch.setenv("SHERPA_OPENAI_ENDPOINT_KIND", "azure")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://y.openai.azure.com/openai/v1")
    sys_s = {"openai_endpoint_kind": "custom", "openai_base_url": "https://custom.example.com/v1"}
    r = doctor_checks._openai_endpoint_status(sys_s)
    assert r["status"] == "ok"
    assert r["effective_sys_s"]["openai_endpoint_kind"] == "custom"
    assert r["effective_sys_s"]["openai_base_url"] == "https://custom.example.com/v1"


def test_openai_endpoint_status_fills_only_missing_keys_from_env_candidate(monkeypatch):
    """DB に無いキーだけを env 候補で補完する。DB に既にある `openai_base_url` はそのまま
    （host のサフィックスから azure と推定される・`SHERPA_OPENAI_ENDPOINT_KIND` は未指定なので
    候補のクロス検証には掛からない）、DB にも候補にも無い `openai_endpoint_kind` は補完されず
    未設定のまま（推定に委ねる・本番のシードと同じ挙動）。"""
    monkeypatch.delenv("SHERPA_OPENAI_ENDPOINT_KIND", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("SHERPA_OPENAI_AUTH_HEADER", "api-key")
    sys_s = {"openai_base_url": "https://existing.openai.azure.com/openai/v1"}
    r = doctor_checks._openai_endpoint_status(sys_s)
    assert r["status"] == "ok"
    assert r["effective_sys_s"]["openai_base_url"] == "https://existing.openai.azure.com/openai/v1"
    assert r["effective_sys_s"]["openai_auth_header"] == "api-key"   # env 候補で補完
    assert "openai_endpoint_kind" not in r["effective_sys_s"]


def test_openai_endpoint_status_ng_when_merged_combo_invalid(monkeypatch):
    """DB に `openai_endpoint_kind=azure` の行だけがあり `openai_base_url` の行が無い場合、
    env 候補が base_url を含まなければ合成後も不整合のまま＝初回シード済みと同じ検証
    （`validate_endpoint_settings`）へ通して NG にする（候補単体の検証では見えない組合せ）。"""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("SHERPA_OPENAI_ENDPOINT_KIND", raising=False)
    monkeypatch.setenv("SHERPA_OPENAI_AUTH_HEADER", "api-key")
    sys_s = {"openai_endpoint_kind": "azure"}
    r = doctor_checks._openai_endpoint_status(sys_s)
    assert r["status"] == "ng"
    assert r["effective_sys_s"] is None


def test_openai_endpoint_status_ng_with_none_effective_when_env_candidate_invalid(monkeypatch):
    monkeypatch.setenv("SHERPA_OPENAI_ENDPOINT_KIND", "azure")   # base_url 無しの azure は不整合
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    r = doctor_checks._openai_endpoint_status({})
    assert r["status"] == "ng"
    assert r["effective_sys_s"] is None


def test_openai_endpoint_status_ng_with_none_effective_when_db_endpoint_invalid():
    sys_s = {"openai_endpoint_seed_version": 1, "openai_endpoint_kind": "bogus"}
    r = doctor_checks._openai_endpoint_status(sys_s)
    assert r["status"] == "ng"
    assert r["effective_sys_s"] is None


def test_openai_endpoint_status_ok_returns_same_sys_s_object_when_already_seeded():
    """初回シード済み（DB が唯一の真実源）なら `sys_s` をそのまま返す（コピーすら作らない）。"""
    sys_s = {"openai_endpoint_seed_version": 1}
    r = doctor_checks._openai_endpoint_status(sys_s)
    assert r["status"] == "ok"
    assert r["effective_sys_s"] is sys_s


def test_openai_endpoint_status_skip_and_none_effective_when_sys_s_none():
    r = doctor_checks._openai_endpoint_status(None)
    assert r["status"] == "skip"
    assert r["effective_sys_s"] is None


def test_cloud_llm_probes_zero_sends_when_no_marker_and_azure_env_deployment_unregistered(monkeypatch):
    """接続先チェックが是正前の穴（NO_MARKER＋Azure env で `sys_s` に反映しないまま chat/embed
    プローブへ渡すと、Azure 専用のキーが `api.openai.com` へ誤って送られる）を再現する構成
    （env は Azure・実キーがあり・chat のデプロイ名は未登録）で、`_openai_endpoint_status` が
    計算した `effective_sys_s` を `check_cloud_llm_probes` へ渡すと、実送信の境界
    （`complete_json`／`llm.openai_post_json`）を一度も呼ばずに静的検査だけで NG になることを
    固定する（送信ゼロ）。"""
    from sherpa import agent_constructs, llm
    from sherpa.ingest import graph_extract
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x.openai.azure.com/openai/v1")
    monkeypatch.delenv("SHERPA_OPENAI_ENDPOINT_KIND", raising=False)
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")

    def _should_not_be_called(*a, **k):
        raise AssertionError("接続先が未確定/不正な構成で実送信してはいけない")
    monkeypatch.setattr(graph_extract, "complete_json", _should_not_be_called)
    monkeypatch.setattr(llm, "openai_post_json", _should_not_be_called)

    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-azure-only-key-1234567890"}
    endpoint_status = doctor_checks._openai_endpoint_status(sys_s)
    results = doctor_checks.check_cloud_llm_probes(endpoint_status["effective_sys_s"], [], probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "ng"
    assert "デプロイ名" in by_id["llm_openai"].detail


def test_cloud_llm_probes_zero_sends_when_endpoint_check_ng(monkeypatch):
    """`_openai_endpoint_status` が `ng`（`effective_sys_s=None`）を返す構成では、`run_all()` と
    同じ配線（`None` をそのまま `check_cloud_llm_probes` へ渡す）で全プロバイダが SKIP になり、
    実送信の境界を一度も呼ばない。"""
    from sherpa.ingest import graph_extract

    def _should_not_be_called(*a, **k):
        raise AssertionError("接続先が不正な構成で実送信してはいけない")
    monkeypatch.setattr(graph_extract, "complete_json", _should_not_be_called)

    sys_s = {"openai_endpoint_seed_version": 1, "openai_endpoint_kind": "bogus", "cloud_provider": "openai",
             "openai_api_key": "sk-real-key-1234567890"}
    endpoint_status = doctor_checks._openai_endpoint_status(sys_s)
    assert endpoint_status["effective_sys_s"] is None
    results = doctor_checks.check_cloud_llm_probes(endpoint_status["effective_sys_s"], [], probe_cloud=True)
    assert {r.status for r in results} == {"skip"}


def test_check_selected_provider_key_skips_without_settings():
    assert doctor_checks.check_selected_provider_key(None, None).status == "skip"


def test_agent_resolution_indeterminate_true_when_default_effective_agent_raises(monkeypatch):
    from sherpa import agent_constructs

    def _boom(settings, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_constructs, "effective_agent", _boom)
    assert doctor_checks._agent_resolution_indeterminate({}, []) is True


def test_agent_resolution_indeterminate_true_when_per_row_effective_agent_raises(monkeypatch):
    from sherpa import agent_constructs

    def _maybe_boom(settings, **k):
        if settings is None:
            return "ollama"
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_constructs, "effective_agent", _maybe_boom)
    rows = [{"agent": "openai", "codex_model_provider": None}]
    assert doctor_checks._agent_resolution_indeterminate({}, rows) is True


def test_agent_resolution_indeterminate_false_when_resolvable(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    rows = [{"agent": "openai", "codex_model_provider": None}]
    assert doctor_checks._agent_resolution_indeterminate({}, rows) is False


def test_agent_resolution_indeterminate_false_when_rows_none(monkeypatch):
    """`rows is None` はこの関数の対象外（`user_settings_read` 側が別途 NG を報告する契約）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    assert doctor_checks._agent_resolution_indeterminate({}, None) is False


def test_check_selected_provider_key_ng_when_agent_resolution_indeterminate_even_with_real_key(monkeypatch):
    """`effective_agent()` が例外を投げる構成では、実在する中央キーの有無に関わらず本項目自身を
    固定文言の NG にする（`_central_auth_available` が先に `ok` を返してしまうと、判定不能だった
    こと自体が握り潰される＝誤帰属の是正）。"""
    from sherpa import agent_constructs

    def _boom(settings, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_constructs, "effective_agent", _boom)
    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-real-key-1234567890"}
    r = doctor_checks.check_selected_provider_key(sys_s, [])
    assert r.status == "ng"
    assert r.detail == doctor_checks._AGENT_RESOLUTION_FAILED_DETAIL


def test_check_selected_provider_key_ok_when_present_even_if_not_currently_consumed(monkeypatch):
    """有効な認証情報が実際にあれば、現在のどの構成が消費するかに関わらず `ok`（有効な設定を
    「使っていないかもしれないから」隠さない）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-real-key"}
    assert doctor_checks.check_selected_provider_key(sys_s, []).status == "ok"


def test_check_selected_provider_key_ng_when_missing_and_consumed(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    sys_s = {"cloud_provider": "openai"}
    rows = [{"agent": "openai", "codex_model_provider": None, "search_helper": ""}]
    assert doctor_checks.check_selected_provider_key(sys_s, rows).status == "ng"


def test_check_selected_provider_key_ng_when_placeholder_and_consumed(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-REPLACE_ME"}
    rows = [{"agent": "openai", "codex_model_provider": None, "search_helper": ""}]
    assert doctor_checks.check_selected_provider_key(sys_s, rows).status == "ng"


def test_check_selected_provider_key_ng_via_second_path_truthy_placeholder_when_chat_not_consumed(monkeypatch):
    """chat/Codex の誰も openai を使っていなくても（全員 ollama）、第2経路（intent/render/embed・
    `sherpa.llm.resolve_auto_provider()`）は truthy 判定のみで自動選択する＝本番はプレースホルダの
    ままでも実際に送信を試みて必ず失敗する。「消費判定」（truthy）は「認証有効性」
    （`is_real_api_key`）とは別物＝この構成は「消費されている」が「認証は無効」なので NG になる
    （本番は実際に壊れたキーで送信し続けているのに doctor が見逃してはいけない）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-REPLACE_ME"}
    rows = [{"agent": "ollama", "codex_model_provider": None, "search_helper": "",
            "has_openai_key": False, "has_gemini_key": False, "has_bedrock_key": False}]
    assert doctor_checks.check_selected_provider_key(sys_s, rows).status == "ng"


def test_check_selected_provider_key_ng_via_second_path_whitespace_key_when_chat_not_consumed(monkeypatch):
    """空白のみのキーも truthy（本番の `resolve_auto_provider` は `bool(...)` しか見ない）ため、
    上のテストと同様に「消費されている」扱いになる。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    sys_s = {"cloud_provider": "gemini", "gemini_api_key": "   "}
    rows = [{"agent": "ollama", "codex_model_provider": None, "search_helper": "",
            "has_openai_key": False, "has_gemini_key": False, "has_bedrock_key": False}]
    assert doctor_checks.check_selected_provider_key(sys_s, rows).status == "ng"


def test_check_selected_provider_key_skip_when_missing_and_not_consumed(monkeypatch):
    """ollama_only／codex_ollama のみの構成で、`cloud_provider` を一度も選んでいない（生の
    保存値が無い＝既定 openai への読み替えのみ）場合は、そのキー欠落を要求しない
    （exit 1 の誤検知を避ける）。RV1（FBK-1・2026-09-01）: `cloud_provider` に生の保存値が
    ある場合はこの skip の対象外になる（下の `test_check_selected_provider_key_ng_when_raw_
    selected_even_if_not_consumed_by_chat` 参照）——本テストは「一度も選んでいない」場合の
    回帰確認に絞る。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    monkeypatch.setattr(agent_constructs, "codex_model_provider", lambda *a, **k: "ollama")
    sys_s = {}   # cloud_provider の生の保存値なし＝一度も選んでいない
    rows = [{"agent": "ollama", "codex_model_provider": None, "search_helper": ""},
            {"agent": "codex", "codex_model_provider": "ollama", "search_helper": ""}]
    r = doctor_checks.check_selected_provider_key(sys_s, rows)
    assert r.status == "skip"


def test_check_selected_provider_key_ng_when_raw_selected_even_if_not_consumed_by_chat(monkeypatch):
    """RV1（FBK-1・境界回帰#7・2026-09-01）: `cloud_provider` に生の保存値がある（＝admin が
    明示選択済み）なら、チャット/Codex の実効頭脳が Ollama で「今は」消費していなくても NG。
    fail-loud（`llm.resolve_auto_provider`）の下では、intent/render/embed（第2経路）は
    明示選択済みのクラウドで解決を試みてキー不足で失敗するだけで Ollama へは倒れないため、
    「使われていません」という skip は復旧診断として不十分（上の skip テストとの対比）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    monkeypatch.setattr(agent_constructs, "codex_model_provider", lambda *a, **k: "ollama")
    sys_s = {"cloud_provider": "openai"}   # 生の保存値あり＝明示選択済み（キー未設定）
    rows = [{"agent": "ollama", "codex_model_provider": None, "search_helper": "",
            "has_openai_key": False, "has_gemini_key": False, "has_bedrock_key": False},
            {"agent": "codex", "codex_model_provider": "ollama", "search_helper": "",
            "has_openai_key": False, "has_gemini_key": False, "has_bedrock_key": False}]
    r = doctor_checks.check_selected_provider_key(sys_s, rows)
    assert r.status == "ng"
    assert "openai" in r.detail


def test_check_selected_provider_key_consumed_via_azure_codex_backing_when_effective(monkeypatch):
    """`selected == "openai"` で、その構成の実効頭脳が実際に `"codex"` であり、Codex の接続先が
    Azure/custom（`codex_model_provider` が `ollama` でない）なら、`resolve_api_key("openai", ...)`
    を必ず使う＝消費している扱いにする。"""
    from sherpa import agent_constructs, llm
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "codex")
    monkeypatch.setattr(agent_constructs, "codex_model_provider", lambda *a, **k: "openai")
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda *a, **k: "azure")
    sys_s = {"cloud_provider": "openai"}
    r = doctor_checks.check_selected_provider_key(sys_s, [])
    assert r.status == "ng"


def test_check_selected_provider_key_stale_codex_model_provider_not_consumed_when_not_actually_codex(monkeypatch):
    """実効頭脳が Codex でないのに、残存する Azure/custom 向けの
    `codex_model_provider` 設定だけを根拠に「openai を消費している」扱いにしない
    （`agent_constructs.codex_model_provider(None)` が "openai" のままでも、実際の実効頭脳が
    "ollama" 等へフォールバックしていれば Codex 経由の OpenAI/Azure I/O は発生しない）。

    RV1（FBK-1・2026-09-01）: `cloud_provider` の生の保存値は無し（`sys_s = {}`）にする——
    この区別は `check_selected_provider_key` 自体の外側の判定（生の保存値があれば無条件で ng）に
    委ねているため、本テストが確認したい `_cloud_provider_consumed` の精度（false positive の
    回避）を検証するには raw を立てず「既定値 openai への読み替え」の経路のまま通す必要がある。"""
    from sherpa import agent_constructs, llm
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    monkeypatch.setattr(agent_constructs, "codex_model_provider", lambda *a, **k: "openai")
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda *a, **k: "azure")
    sys_s = {}   # cloud_provider の生の保存値なし（既定 openai への読み替えのみ）
    r = doctor_checks.check_selected_provider_key(sys_s, [])
    assert r.status == "skip"


def test_check_selected_provider_key_search_helper_alone_not_consumed_when_main_agent_not_openai(monkeypatch):
    """`search_helper` は主頭脳が openai のときだけ実際に配線される
    （`sherpa/providers/__init__.py::get_provider` 参照）。主頭脳が openai でない利用者の
    `search_helper == "openai"` 列だけを根拠に消費扱いにしない。

    RV1（FBK-1・2026-09-01）: 上のテストと同じ理由で `cloud_provider` の生の保存値は無しにする。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    sys_s = {}   # cloud_provider の生の保存値なし（既定 openai への読み替えのみ）
    rows = [{"agent": "ollama", "codex_model_provider": None, "search_helper": "openai"}]
    r = doctor_checks.check_selected_provider_key(sys_s, rows)
    assert r.status == "skip"


def test_check_selected_provider_key_fail_closed_when_rows_unavailable(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    sys_s = {"cloud_provider": "openai"}
    r = doctor_checks.check_selected_provider_key(sys_s, None)
    assert r.status == "ng"


def test_check_selected_provider_key_follows_a7_exclusive_selection(monkeypatch):
    """A7: 選択されていないプロバイダにキーが残っていても、選択中プロバイダの列を見る。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent",
                         lambda settings, **k: (settings or {}).get("agent") or "ollama")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: False)   # gemini が有効な環境
    sys_s = {"cloud_provider": "gemini", "openai_api_key": "sk-real-key"}
    rows = [{"agent": "gemini", "codex_model_provider": None, "search_helper": ""}]
    assert doctor_checks.check_selected_provider_key(sys_s, rows).status == "ng"


def test_check_selected_provider_key_bedrock_ng_without_any_credentials(monkeypatch, tmp_path):
    from sherpa import agent_constructs
    from sherpa.providers import bedrock as bedrock_mod
    for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_AWS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bedrock_mod.Path, "home", classmethod(lambda cls: tmp_path))   # ~/.aws/credentials 無し
    monkeypatch.setattr(agent_constructs, "effective_agent",
                         lambda settings, **k: (settings or {}).get("agent") or "ollama")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: False)   # bedrock が有効な環境
    sys_s = {"cloud_provider": "bedrock"}
    rows = [{"agent": "bedrock", "codex_model_provider": None, "search_helper": ""}]
    assert doctor_checks.check_selected_provider_key(sys_s, rows).status == "ng"


def test_check_selected_provider_key_bedrock_ok_with_central_key():
    sys_s = {"cloud_provider": "bedrock", "bedrock_api_key": "central-key"}
    assert doctor_checks.check_selected_provider_key(sys_s, []).status == "ok"


def test_check_selected_provider_key_bedrock_ok_with_sigv4_env(monkeypatch):
    """中央キーが無くても AWS_ACCESS_KEY_ID（SigV4）があれば正当な構成として OK にする
    （providers/bedrock.py::_bedrock_auth_available 準拠）。"""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")
    sys_s = {"cloud_provider": "bedrock"}
    assert doctor_checks.check_selected_provider_key(sys_s, []).status == "ok"


def test_check_selected_provider_key_bedrock_ok_with_aws_profile_env(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "default")
    sys_s = {"cloud_provider": "bedrock"}
    assert doctor_checks.check_selected_provider_key(sys_s, []).status == "ok"


def test_check_selected_provider_key_bedrock_ok_with_credentials_file(monkeypatch, tmp_path):
    from sherpa.providers import bedrock as bedrock_mod
    cred_dir = tmp_path / ".aws"
    cred_dir.mkdir()
    (cred_dir / "credentials").write_text("[default]\n", encoding="utf-8")
    monkeypatch.setattr(bedrock_mod.Path, "home", classmethod(lambda cls: tmp_path))
    sys_s = {"cloud_provider": "bedrock"}
    assert doctor_checks.check_selected_provider_key(sys_s, []).status == "ok"


def test_check_selected_provider_key_ok_via_personal_keys_when_central_missing(monkeypatch):
    """`personal_api_keys_allowed=true` かつ有効な利用者の個人キーが
    あれば、中央キー欠落でも NG にしない（値は読まず、件数だけの情報表示で `ok`）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    sys_s = {"cloud_provider": "openai", "personal_api_keys_allowed": True}
    rows = [{"agent": "openai", "codex_model_provider": None, "search_helper": "",
            "has_openai_key": True, "has_gemini_key": False, "has_bedrock_key": False},
            {"agent": "openai", "codex_model_provider": None, "search_helper": "",
            "has_openai_key": False, "has_gemini_key": False, "has_bedrock_key": False}]
    r = doctor_checks.check_selected_provider_key(sys_s, rows)
    assert r.status == "ok"
    assert "1" in r.detail
    assert "sk-" not in r.detail   # 値は出さない


def test_check_selected_provider_key_ng_when_personal_keys_not_allowed(monkeypatch):
    """A6 が無効（既定）なら、有効な利用者に個人キーがあっても中央キー欠落は救済しない。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    sys_s = {"cloud_provider": "openai", "personal_api_keys_allowed": False}
    rows = [{"agent": "openai", "codex_model_provider": None, "search_helper": "",
            "has_openai_key": True, "has_gemini_key": False, "has_bedrock_key": False}]
    r = doctor_checks.check_selected_provider_key(sys_s, rows)
    assert r.status == "ng"


def test_check_selected_provider_key_personal_keys_field_is_per_provider(monkeypatch):
    """選択中プロバイダに対応する列（`has_<provider>_key`）だけを見る（他プロバイダの個人キーで
    誤って救済しない）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    sys_s = {"cloud_provider": "openai", "personal_api_keys_allowed": True}
    rows = [{"agent": "openai", "codex_model_provider": None, "search_helper": "",
            "has_openai_key": False, "has_gemini_key": True, "has_bedrock_key": True}]
    r = doctor_checks.check_selected_provider_key(sys_s, rows)
    assert r.status == "ng"


def test_check_selected_provider_key_personal_keys_bedrock_field(monkeypatch, tmp_path):
    """中央キー・SigV4 のどちらも無い状態で、個人キー経由の `ok` を確実に検証する（`Path.home()`
    を隔離しないと、実行環境に実際の `~/.aws/credentials` があった場合に中央側の SigV4 判定で
    先に `ok` が決まってしまい、この検証対象＝個人キー経路を実際には通らないことがある）。"""
    from sherpa import agent_constructs
    from sherpa.providers import bedrock as bedrock_mod
    for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bedrock_mod.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "bedrock")
    sys_s = {"cloud_provider": "bedrock", "personal_api_keys_allowed": True}
    rows = [{"agent": "bedrock", "codex_model_provider": None, "search_helper": "",
            "has_openai_key": False, "has_gemini_key": False, "has_bedrock_key": True}]
    r = doctor_checks.check_selected_provider_key(sys_s, rows)
    assert r.status == "ok"


def test_as_key_str_passes_through_strings_and_rejects_non_strings():
    assert doctor_checks._as_key_str("sk-real-key") == "sk-real-key"
    assert doctor_checks._as_key_str(None) is None
    for bad in (42, True, 3.14, {"a": 1}, [1, 2], object()):
        assert doctor_checks._as_key_str(bad) is None


def test_central_auth_available_openai_true_with_real_key():
    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-real-key"}
    assert doctor_checks._central_auth_available("openai", sys_s) is True


def test_central_auth_available_false_not_crash_when_key_is_non_string():
    """`openai_api_key`（JSONB）が数値・オブジェクト等の非文字列だと、`is_real_api_key()`
    （`.strip()` を呼ぶ契約）が `AttributeError` を投げ `make doctor` 全体を未捕捉の traceback で
    中断させうる。`_as_key_str()` による正規化で「無効キー」として安全に `False` を返す。"""
    for bad in (42, {"unexpected": "object"}, ["a", "list"], True):
        sys_s = {"cloud_provider": "openai", "openai_api_key": bad}
        assert doctor_checks._central_auth_available("openai", sys_s) is False


def test_central_auth_available_openai_false_with_placeholder():
    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-REPLACE_ME"}
    assert doctor_checks._central_auth_available("openai", sys_s) is False


def test_central_auth_available_bedrock_true_with_sigv4_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")
    assert doctor_checks._central_auth_available("bedrock", {"cloud_provider": "bedrock"}) is True


def test_central_auth_available_bedrock_false_with_placeholder_central_key(monkeypatch, tmp_path):
    """`_bedrock_auth_available` は単純な truthy 判定しかしないため、プレースホルダ文字列
    （`sk-REPLACE_ME` 等）が central key に入っていても「キーあり」として通してしまう。
    `_central_auth_available` は `is_real_api_key()` で弾いてから渡すこと。

    `Path.home()` をテスト用の空の一時ディレクトリへ固定する（実行環境（開発機）に実際の
    `~/.aws/credentials` が存在すると、そちらを拾って意図せず `ok` になり、環境依存で
    結果が変わる非決定的なテストになる）。"""
    from sherpa.providers import bedrock as bedrock_mod
    for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bedrock_mod.Path, "home", classmethod(lambda cls: tmp_path))
    sys_s = {"cloud_provider": "bedrock", "bedrock_api_key": "sk-REPLACE_ME"}
    assert doctor_checks._central_auth_available("bedrock", sys_s) is False


def test_personal_key_holder_count_zero_when_not_allowed():
    sys_s = {"personal_api_keys_allowed": False}
    rows = [{"has_openai_key": True}]
    assert doctor_checks._personal_key_holder_count("openai", sys_s, rows) == 0


def test_personal_key_holder_count_zero_when_rows_none():
    sys_s = {"personal_api_keys_allowed": True}
    assert doctor_checks._personal_key_holder_count("openai", sys_s, None) == 0


def test_personal_key_holder_count_counts_matching_provider_field():
    sys_s = {"personal_api_keys_allowed": True}
    rows = [{"has_openai_key": True}, {"has_openai_key": False}, {"has_openai_key": True}]
    assert doctor_checks._personal_key_holder_count("openai", sys_s, rows) == 2


def test_personal_key_holder_count_zero_when_provider_not_currently_selected():
    """A7: `keys.resolve_api_key(provider, ...)` は現在の `cloud_provider`（システム選択）と
    `provider` が一致しない限り常に `None` を返す（保存済みの個人キーがあっても解決されない）。
    `cloud_provider=gemini` の構成に残る openai の個人キーは、選択が gemini である限り誰にも
    解決できないため、"openai" の保有者数としては数えない。"""
    sys_s = {"personal_api_keys_allowed": True, "cloud_provider": "gemini"}
    rows = [{"has_openai_key": True}]
    assert doctor_checks._personal_key_holder_count("openai", sys_s, rows) == 0


# ---------------------------------------------------------------------------
# 2b. LLM プローブ失敗の fail-closed 分類（_classify_llm_probe_failure）
# ---------------------------------------------------------------------------

def test_classify_llm_probe_failure_never_embeds_dynamic_class_name():
    """`type(e).__name__` は動的に決まる識別子であり、動的生成クラスであれば理論上任意の文字列を
    運びうる（例: 例外クラス名に実キーの断片が紛れ込むような、悪意ある／異常なライブラリ実装）。
    分類は固定の許可リスト（`_KNOWN_EXC_TYPE_LABELS`）へ照合するため、既知の基底クラス
    （ここでは `RuntimeError`）を継承していれば動的なクラス名がどうであれ固定ラベルに丸め込まれ、
    クラス名の文字列そのものは detail に一切現れない。"""
    secret_like = "sk-SUPERSECRET1234567890ABCDEFGH"
    DynamicCls = type(f"Leak_{secret_like}", (RuntimeError,), {})
    detail = doctor_checks._classify_llm_probe_failure(DynamicCls("boom"))
    assert secret_like not in detail
    assert detail == "error（RuntimeError）"


def test_classify_llm_probe_failure_rejects_non_int_status():
    """`status_code`／`code` 属性が `int` 型でない（文字列等）場合は HTTP ステータスとして
    採用しない（`_safe_http_status` の型検査）。文字列に実キーが紛れ込んでいても detail には
    現れない。"""
    class FakeStatusError(Exception):
        status_code = "sk-not-an-int-1234567890"
    detail = doctor_checks._classify_llm_probe_failure(FakeStatusError("boom"))
    assert "sk-" not in detail
    assert detail == "error（UnknownError）"


def test_classify_llm_probe_failure_survives_attribute_getter_exception():
    """`status_code`／`code` 相当の属性が `property` の getter として実装されていて、アクセス時に
    例外を投げても、分類処理自体は落ちずに固定の `"error"` を返す（属性アクセスは全て `try` の
    内側で行う）。"""
    class BoomStatusError(Exception):
        @property
        def status_code(self):
            raise RuntimeError("getter exploded")
    detail = doctor_checks._classify_llm_probe_failure(BoomStatusError("boom"))
    assert detail == "error"


def test_classify_llm_probe_failure_known_types_and_status_codes():
    """既知の型・妥当な HTTP ステータスは正しく分類される（回帰確認）。"""
    import socket
    import urllib.error
    assert doctor_checks._classify_llm_probe_failure(
        urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, None)) == "auth status=401（HTTPError）"
    assert doctor_checks._classify_llm_probe_failure(
        urllib.error.HTTPError("https://x", 500, "Internal", {}, None)) == "http_5xx status=500（HTTPError）"
    assert doctor_checks._classify_llm_probe_failure(socket.gaierror("boom")) == "dns（DNSError）"
    assert doctor_checks._classify_llm_probe_failure(TimeoutError()) == "timeout（TimeoutError）"


# ---------------------------------------------------------------------------
# 3. LLM 最小プローブ（クラウド）
# ---------------------------------------------------------------------------

def test_cloud_llm_probes_skip_without_settings():
    results = doctor_checks.check_cloud_llm_probes(None, None, probe_cloud=True)
    assert {r.status for r in results} == {"skip"}


def test_cloud_llm_probes_ng_when_agent_resolution_indeterminate_even_with_real_key(monkeypatch):
    """選択中プロバイダについて `effective_agent()` が例外を投げる構成では、実キーの有無や
    PROBE_CLOUD の設定に関わらず本項目自身を固定文言の NG にする（誤帰属の是正・非選択の
    プロバイダは従来どおり「選択中ではない」で SKIP のまま）。"""
    from sherpa import agent_constructs

    def _boom(settings, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_constructs, "effective_agent", _boom)
    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-real-key-1234567890"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "ng"
    assert by_id["llm_openai"].detail == doctor_checks._AGENT_RESOLUTION_FAILED_DETAIL
    assert by_id["llm_gemini"].status == "skip"
    assert by_id["llm_bedrock"].status == "skip"


def test_cloud_llm_probes_skip_when_billing_gate_closed():
    sys_s = {"cloud_provider": "openai"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "skip"
    assert "PROBE_CLOUD" in by_id["llm_openai"].detail
    assert by_id["llm_gemini"].status == "skip"
    assert by_id["llm_bedrock"].status == "skip"


def test_cloud_llm_probes_openai_direct_azure_missing_deployment_is_ng_without_probe_cloud(monkeypatch):
    """「OpenAI 直結」構成（Codex を介さず `agent=="openai"`）で接続先が Azure/custom なのに
    chat 用途のデプロイ名が未登録（組み込み既定 gpt-5.5 のまま）の場合、本番は
    `_select_provider` が `_UnwiredProvider`（実行時に必ず失敗）へ倒す。この不備はネットワーク
    I/O を伴わない静的検査（`model_catalog.resolve_model()` のローカル解決）だけで検出できるため、
    `PROBE_CLOUD`（課金を伴う実プローブのゲート）に関わらず NG として検出する（`complete_json` は
    一度も呼ばれない＝この検査自体もネットワークへ出ない）。`effective_agent()` が実際に
    `"openai"` を返す構成でのみこの静的検査が適用される（`_agent_actually_used` 参照）ため、
    ここでは明示的に `"openai"` へ固定する。"""
    from sherpa import agent_constructs
    from sherpa.ingest import graph_extract
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")

    def _should_not_be_called(system, user, cfg, timeout=None):
        raise AssertionError("静的検査のはずなのに complete_json（実送信）が呼ばれた")
    monkeypatch.setattr(graph_extract, "complete_json", _should_not_be_called)

    sys_s = {"cloud_provider": "openai", "openai_endpoint_kind": "azure",
             "openai_base_url": "https://x.openai.azure.com/openai/v1", "openai_api_key": "sk-real-key"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "ng"
    assert "デプロイ名" in by_id["llm_openai"].detail


def test_cloud_llm_probes_openai_direct_default_endpoint_unaffected_by_deployment_check():
    """接続先が既定(openai)なら「デプロイ名」静的検査の対象外＝これまで通り PROBE_CLOUD が
    閉じていれば SKIP のまま（回帰確認）。"""
    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-real-key"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "skip"


def test_cloud_llm_probes_openai_direct_azure_with_registered_deployment_not_flagged(monkeypatch):
    """実際に消費されている用途（ここでは実キーもあるため chat に加えて第2経路の
    intent/render/embed も消費される）**すべて**のデプロイ名が実際に登録されていれば、静的検査は
    NG にしない（PROBE_CLOUD が閉じていれば通常どおり SKIP のまま）。`effective_agent()` を
    明示的に `"openai"` に固定し、静的検査自体が実行される（`_agent_actually_used` を通過する）
    ことを保証したうえで「登録済みなら NG にならない」分岐を検証する。"""
    from sherpa import agent_constructs, model_catalog
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")

    def _fake_resolve(provider, purpose, s, **k):
        return f"my-{purpose}-deployment-name" if provider == "openai" else "unused"
    monkeypatch.setattr(model_catalog, "resolve_model", _fake_resolve)

    sys_s = {"cloud_provider": "openai", "openai_endpoint_kind": "azure",
             "openai_base_url": "https://x.openai.azure.com/openai/v1", "openai_api_key": "sk-real-key"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "skip"


def test_agent_actually_used_fail_closed_when_default_effective_agent_raises(monkeypatch):
    """`effective_agent()` がシステム既定側で例外を投げても、黙って「一致しない（＝使われて
    いない）」に丸めず fail-closed（使われている扱い）にする（docstring の契約どおり）。"""
    from sherpa import agent_constructs

    def _boom(settings, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_constructs, "effective_agent", _boom)
    assert doctor_checks._agent_actually_used("openai", {}, []) is True


def test_agent_actually_used_fail_closed_when_per_row_effective_agent_raises(monkeypatch):
    """システム既定は判定できても（対象と一致しない）、有効な利用者の1人の `effective_agent()`
    が例外を投げれば、その行を黙って「使っていない」に丸めず fail-closed にする。"""
    from sherpa import agent_constructs

    def _maybe_boom(settings, **k):
        if settings is None:
            return "ollama"
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_constructs, "effective_agent", _maybe_boom)
    rows = [{"agent": "openai", "codex_model_provider": None}]
    assert doctor_checks._agent_actually_used("openai", {}, rows) is True


def test_agent_actually_used_false_when_genuinely_not_used(monkeypatch):
    """判定不能ではなく、単に対象と一致しない場合は引き続き `False`（fail-closed の乱用にしない）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    assert doctor_checks._agent_actually_used("openai", {}, []) is False


def test_codex_required_fail_closed_when_default_effective_agent_raises(monkeypatch):
    """`_codex_required` の docstring は「読み取れない場合は fail-closed」と約束しているが、
    `effective_agent()` がシステム既定側で例外を投げるケースも同じ契約に含める（黙って
    「codex を使っていない」に丸めると `needs_openai_auth` が偽になり、委譲先の認証確認
    （`check_codex`／`_check_codex_auth`）自体が SKIP されてしまう）。"""
    from sherpa import agent_constructs

    def _boom(settings, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_constructs, "effective_agent", _boom)
    required, needs_openai_auth, _note = doctor_checks._codex_required({}, [])
    assert required is True
    assert needs_openai_auth is True


def test_codex_required_fail_closed_when_per_row_effective_agent_raises(monkeypatch):
    from sherpa import agent_constructs

    def _maybe_boom(settings, **k):
        if settings is None:
            return "ollama"
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_constructs, "effective_agent", _maybe_boom)
    rows = [{"agent": "codex", "codex_model_provider": "ollama"}]
    required, needs_openai_auth, _note = doctor_checks._codex_required({}, rows)
    assert required is True
    assert needs_openai_auth is True


def test_openai_azure_deployment_reason_matches_select_provider_contract():
    """`_openai_azure_deployment_reason` は `sherpa/providers/__init__.py::_select_provider`
    の openai 分岐（Azure/custom かつ既定モデルのまま→`_UnwiredProvider`）と同じ判定を行う
    （重複実装だが判定条件は一致させる契約・回帰確認）。"""
    sys_s_default = {"openai_api_key": "sk-real-key"}
    assert doctor_checks._openai_azure_deployment_reason("chat", sys_s_default) is None

    sys_s_azure_unregistered = {"openai_endpoint_kind": "azure",
                                 "openai_base_url": "https://x.openai.azure.com/openai/v1"}
    reason = doctor_checks._openai_azure_deployment_reason("chat", sys_s_azure_unregistered)
    assert reason is not None and "デプロイ名" in reason


def test_openai_azure_deployment_reason_none_when_endpoint_is_default():
    """接続先が既定(openai)ならどの用途でもこのガードの対象外。実際に呼び出し元
    （`check_cloud_llm_probes`）が渡すのは "chat"／"embed" の2つ（docstring 参照）。"""
    sys_s = {"openai_api_key": "sk-real-key"}
    for purpose in ("chat", "embed"):
        assert doctor_checks._openai_azure_deployment_reason(purpose, sys_s) is None


def test_openai_azure_deployment_reason_checks_embed_independently_of_chat(monkeypatch):
    """`purpose` ごとに独立したカタログセルを見る（embed はカタログのみで編集する専用欄）。
    chat 用途のデプロイ名が登録されていなくても、embed 用途が登録されていれば embed は
    問題なしと判定する（逆も同様）。"""
    from sherpa import model_catalog
    orig_resolve = model_catalog.resolve_model

    def _fake_resolve(provider, purpose, s, **k):
        if provider == "openai" and purpose == "embed":
            return "my-embed-deployment"
        return orig_resolve(provider, purpose, s, **k)
    monkeypatch.setattr(model_catalog, "resolve_model", _fake_resolve)

    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1"}
    reason = doctor_checks._openai_azure_deployment_reason("chat", sys_s)
    assert reason is not None and "デプロイ名" in reason and "チャット" in reason
    assert doctor_checks._openai_azure_deployment_reason("embed", sys_s) is None


def test_openai_azure_deployment_reason_survives_broken_model_catalog(monkeypatch):
    """`model_catalog.resolve_model()` が壊れた設定で例外を投げても、この関数の外へは伝播せず
    （`run_all()` 全体を巻き込まない）、NG 扱いの理由文字列を返す。"""
    from sherpa import model_catalog

    def _boom(*a, **k):
        raise TypeError("model_catalog.openai.chat.allowed=1 のような壊れた設定")
    monkeypatch.setattr(model_catalog, "resolve_model", _boom)
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1"}
    reason = doctor_checks._openai_azure_deployment_reason("chat", sys_s)
    assert reason is not None


def test_cloud_llm_probes_openai_azure_deployment_check_skipped_when_only_codex_azure_configured(monkeypatch):
    """本番の `_select_provider` の openai 直結分岐は `agent == "openai"` のときにしかこの検査を
    通らない（`sherpa/providers/__init__.py` 参照）。Codex(Azure/custom) のみを使う構成
    （`effective_agent()` は常に `"codex"`・"openai" が直接の実効頭脳になることは無い）では、
    "chat" 用途の静的デプロイ名検査自体が本番で一度も評価されない分岐なので、doctor も NG に
    しない。Codex 自身の Azure デプロイ名確認は別の既存チェック（`codex_auth`）が担当する。
    PROBE_CLOUD を閉じたまま呼び、静的検査（billing 非依存）の結果だけを見る（この構成は "chat"
    用途が Codex 経由で間接消費される＝`_chat_or_codex_consumes` 参照・実プローブの対象からは
    `codex_auth` との二重送信を避けるため除外されるため、`PROBE_CLOUD=1` でも本テストの主眼
    である「静的デプロイ名検査は誤って NG にしない」点の確認には billing ゲートで止める方が
    直接的）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "codex")
    sys_s = {"cloud_provider": "openai", "openai_endpoint_kind": "azure",
             "openai_base_url": "https://x.openai.azure.com/openai/v1",
             "codex_model_provider": "ollama"}   # Codex は Ollama 実行＝openai を間接消費しない
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "skip"


def test_cloud_llm_probes_openai_azure_deployment_check_skipped_when_only_ollama_configured():
    """Ollama のみの構成（`effective_agent()` は常に `"ollama"`・第2経路も未消費）でも同様に対象外。"""
    sys_s = {"cloud_provider": "openai", "openai_endpoint_kind": "azure",
             "openai_base_url": "https://x.openai.azure.com/openai/v1"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "skip"


def test_cloud_llm_probes_openai_azure_deployment_check_checks_active_user_rows_too(monkeypatch):
    """システム既定の実効頭脳が `"openai"` でなくても、有効な利用者の誰か1人の実効頭脳が
    `"openai"` なら "chat" 用途の静的検査が対象になる（`_agent_actually_used` が rows も走査
    する）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent",
                        lambda settings, **k: (settings or {}).get("agent") or "codex")
    sys_s = {"cloud_provider": "openai", "openai_endpoint_kind": "azure",
             "openai_base_url": "https://x.openai.azure.com/openai/v1"}
    rows = [{"agent": "openai", "codex_model_provider": None}]
    results = doctor_checks.check_cloud_llm_probes(sys_s, rows, probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "ng"
    assert "デプロイ名" in by_id["llm_openai"].detail


def test_cloud_llm_probes_intent_render_deployment_not_statically_checked(monkeypatch):
    """"intent"／"render" の解決モデルがたまたま組み込み既定と一致していても、本番にはこの
    「デプロイ名が組み込み既定のままなら NG」という判定を裏付ける実行時ガードが無い
    （`_select_provider` の `_UnwiredProvider` 分岐は "openai"（chat 直結）専用）ため、doctor も
    静的には NG にしない。実プローブ（`complete_json`）で実際の接続を確認する。"""
    from sherpa import agent_constructs
    from sherpa.ingest import graph_extract
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")   # chat は未消費
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")   # embed の静的検査を対象から外し intent/render に絞る
    calls = []

    def _fake_complete_json(system, user, cfg, timeout=None):
        calls.append(cfg)
        return '{"ok":true}'
    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)

    sys_s = {"cloud_provider": "openai", "openai_endpoint_kind": "azure",
             "openai_base_url": "https://x.openai.azure.com/openai/v1",
             "openai_api_key": "sk-real-key-1234567890"}   # intent/render のみ第2経路で消費
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "ok"   # 静的 NG にならず、実プローブ（fake）が成功する
    assert len(calls) == 2   # intent + render


def test_cloud_llm_probes_no_double_probe_when_codex_indirect_chat_and_second_path_consumed(monkeypatch):
    """Codex(Azure/custom) の間接消費（"chat"）に加えて第2経路（intent/render）も消費される
    構成で、`complete_json` が "chat" 用途で二重に呼ばれない（Codex 自身の接続確認は
    `_check_codex_azure_compat` が別途担当する）ことを固定する。"""
    from sherpa import agent_constructs, llm
    from sherpa.ingest import graph_extract
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "codex")
    monkeypatch.setattr(agent_constructs, "codex_model_provider", lambda *a, **k: "openai")
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda *a, **k: "openai")   # 既定接続先のまま
    calls = []

    def _fake_complete_json(system, user, cfg, timeout=None):
        calls.append(cfg.get("model"))
        return '{"ok":true}'
    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)

    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-real-key-1234567890"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "ok"
    assert len(calls) == 2   # intent + render のみ（"chat" は Codex 経由の間接消費のため対象外）


def test_cloud_llm_probes_only_probes_selected_provider_when_gate_open(monkeypatch):
    """openai／gemini は `graph_extract.complete_json` を共有する（`cfg["provider"]` で分岐）ため、
    非選択プロバイダが呼ばれていないことは `cfg["provider"]` の実際の値で確認する。第2経路
    （intent／render）も含め全用途が同じ選択中プロバイダで消費されるため（`sys_s` に実キーが
    あり `effective_agent()` も openai）、chat 用途以外（intent/render）の `complete_json`
    呼び出しも同様に非選択プロバイダでは呼ばれないことを確認する（embed は実プローブを持たない
    ため対象外）。"""
    from sherpa import agent_constructs
    from sherpa.ingest import graph_extract
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")

    def _complete_json(system, user, cfg, timeout=None):
        if cfg["provider"] != "openai":
            raise AssertionError(f"非選択プロバイダは probe してはいけない: {cfg['provider']!r}")
        return '{"ok":true}'
    monkeypatch.setattr(graph_extract, "complete_json", _complete_json)

    def _should_not_be_called(*a, **k):
        raise AssertionError("非選択プロバイダは probe してはいけない")
    monkeypatch.setattr(health, "_ai_check_bedrock", _should_not_be_called)

    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-real-key"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "ok"
    assert by_id["llm_gemini"].status == "skip"
    assert by_id["llm_bedrock"].status == "skip"


def test_cloud_llm_probes_ng_on_probe_failure(monkeypatch):
    from sherpa import agent_constructs
    from sherpa.ingest import graph_extract
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")

    def _boom(system, user, cfg, timeout=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(graph_extract, "complete_json", _boom)
    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-real-key"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "ng"


@pytest.mark.parametrize("secret_in_message", [
    "sk-abcdefgh1234567890ABCDEFGHIJK",           # 分断なし（完全なキーがそのまま含まれる場合）
    "sk-ab\ncdefgh1234\t567890ABCDEFGHIJK",        # 制御文字（改行・タブ）で分断
    "sk-ab cdefgh1234-567890ABCDEFGHIJK",          # 空白・記号（非印字制御文字ではない）で分断
], ids=["intact", "control-char-split", "space-and-symbol-split"])
def test_cloud_llm_probes_openai_failure_detail_never_contains_free_text(monkeypatch, secret_in_message):
    """`check_cloud_llm_probes` の失敗理由は、上流の生の例外メッセージ（実キーがどんな区切り文字で
    分断されて含まれていても、あるいは分断されていなくても）を一切出力しない（fail-closed）。
    ここでは `graph_extract.complete_json` を直接モックし、`_probe`／`_safe_detail` を経由しない
    実経路で確認する（先行する `_safe_detail` をモックで迂回すると、実際のマスク漏れを検出できない
    false-green になるため、それより手前の境界をモックする）。"""
    from sherpa import agent_constructs
    from sherpa.ingest import graph_extract
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    secret = "sk-abcdefgh1234567890ABCDEFGHIJK"

    def _boom(system, user, cfg, timeout=None):
        assert cfg["key"] == secret
        raise RuntimeError(f"invalid key: {secret_in_message}")
    monkeypatch.setattr(graph_extract, "complete_json", _boom)
    sys_s = {"cloud_provider": "openai", "openai_api_key": secret}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=True)
    detail = {r.id: r for r in results}["llm_openai"].detail
    assert detail == "接続に失敗しました: error（RuntimeError）"
    assert "sk-" not in detail
    assert secret not in detail


def test_cloud_llm_probes_skip_with_personal_key_message_when_central_missing(monkeypatch):
    """中央キーが無くても個人キー保有者がいれば実プローブを試みず SKIP に収束させる
    （個人キーの値を doctor が読まない設計のため、doctor 自身では実接続できない）。"""
    from sherpa.ingest import graph_extract

    def _should_not_be_called(*a, **k):
        raise AssertionError("個人キーのみの構成で実プローブしてはいけない")
    monkeypatch.setattr(graph_extract, "complete_json", _should_not_be_called)
    sys_s = {"cloud_provider": "openai", "personal_api_keys_allowed": True}
    rows = [{"agent": "openai", "codex_model_provider": None, "search_helper": "",
            "has_openai_key": True, "has_gemini_key": False, "has_bedrock_key": False}]
    results = doctor_checks.check_cloud_llm_probes(sys_s, rows, probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "skip"
    assert "個人キー" in by_id["llm_openai"].detail


def test_cloud_llm_probes_still_ng_when_no_central_and_no_personal_keys(monkeypatch):
    """中央キーも個人キーも無ければ、送信前ガード（`is_real_api_key`）で弾かれて NG になる
    （`complete_json` は一度も呼ばれない＝壊れたキーで実際に上流へ送信することはない）。"""
    from sherpa.ingest import graph_extract
    calls = []

    def _should_not_be_called(system, user, cfg, timeout=None):
        calls.append(cfg)
        raise RuntimeError("boom")
    monkeypatch.setattr(graph_extract, "complete_json", _should_not_be_called)
    sys_s = {"cloud_provider": "openai", "personal_api_keys_allowed": True}
    rows = [{"agent": "openai", "codex_model_provider": None, "search_helper": "",
            "has_openai_key": False, "has_gemini_key": False, "has_bedrock_key": False}]
    results = doctor_checks.check_cloud_llm_probes(sys_s, rows, probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "ng"
    assert calls == [], "実キーが無いのに complete_json（実送信）が呼ばれた"


def test_cloud_llm_probes_probes_cloud_provider_with_real_key_even_when_chat_uses_ollama(monkeypatch):
    """SHERPA_AGENT=ollama（＝有効な利用者・システム既定のいずれも chat/Codex の実効頭脳は
    `ollama`）＋ `cloud_provider=openai`（A7 選択）＋中央キーは実在、という構成でも、`openai` は
    `sherpa.llm.select_provider()`／`resolve_auto_provider()` を経由する**chat/Codex とは独立**の
    経路（`sherpa/intent_llm.py::_cfg`＝意図分類・`sherpa/ingest/graph_extract.py::available`＝
    グラフ抽出・`sherpa/embeddings.py::cfg`＝埋め込み）から chat の設定と無関係に自動選択され
    続ける（A7 選択＋実キーがあれば ollama より優先）。したがって `_consumed_llm_purposes` は
    "chat" を含まず（chat/Codex は誰も openai を使っていない）"intent"／"render"／"embed" の
    3用途を「消費されている」と判定する。intent/render は実際に `complete_json` でプローブする
    が、embed は実プローブを持たない（静的検査のみ・item3 裁定）ため、静的検査（デフォルト
    接続先なので問題なし）を通過した時点でネットワーク送信は一切発生しない。"""
    from sherpa import agent_constructs
    from sherpa.ingest import graph_extract
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    chat_style_calls = []

    def _fake_complete_json(system, user, cfg, timeout=None):
        chat_style_calls.append(cfg)
        return '{"ok":true}'
    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)

    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-real-key-1234567890"}
    rows = [{"agent": "ollama", "codex_model_provider": None, "search_helper": "",
            "has_openai_key": False, "has_gemini_key": False, "has_bedrock_key": False}]
    results = doctor_checks.check_cloud_llm_probes(sys_s, rows, probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "ok"
    assert {cfg["provider"] for cfg in chat_style_calls} == {"openai"}
    assert len(chat_style_calls) == 2   # intent + render（chat・embed は含まれない）


def test_cloud_llm_probes_skips_truly_unused_cloud_provider_with_no_real_key(monkeypatch):
    """`cloud_provider=openai` が既定値のまま残っているだけで、中央キーも個人キーも無く
    （`_central_auth_available` が偽）、chat/Codex の誰も明示的に `openai` を選んでいない
    （`_actually_routes_to`／`_codex_consumes_openai` も偽）構成では、`resolve_auto_provider()`
    自身も openai を解決できず（キーが無いので auto 解決は ollama へ落ちる）実際には一切消費
    されない。この場合だけ課金を伴う実送信を避けて SKIP にする。"""
    from sherpa import agent_constructs
    from sherpa.ingest import graph_extract
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    calls = []

    def _should_not_be_called(system, user, cfg, timeout=None):
        calls.append(cfg)
        raise AssertionError("未使用の cloud_provider へ実送信してはいけない")
    monkeypatch.setattr(graph_extract, "complete_json", _should_not_be_called)
    sys_s = {"cloud_provider": "openai"}   # 中央キー無し
    rows = [{"agent": "ollama", "codex_model_provider": None, "search_helper": "",
            "has_openai_key": False, "has_gemini_key": False, "has_bedrock_key": False}]
    results = doctor_checks.check_cloud_llm_probes(sys_s, rows, probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_openai"].status == "skip"
    assert "使われていません" in by_id["llm_openai"].detail
    assert calls == [], "未使用の cloud_provider へ complete_json（実送信）が呼ばれた"


def test_cloud_provider_consumed_true_via_intent_classification_path(monkeypatch):
    """`sherpa/intent_llm.py::_cfg` は、`cloud_provider` の A7 選択＋実キーがあれば chat/Codex の
    設定と無関係に openai を選ぶ＝「消費されている」扱いになることを固定する
    （chat/Codex の実効頭脳が誰も openai でなくても揺るがない）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-real-key-1234567890"}
    assert doctor_checks._cloud_provider_consumed("openai", sys_s, []) is True


def test_cloud_provider_consumed_false_via_graph_extraction_path_when_bedrock_not_used_by_chat(
        monkeypatch, tmp_path):
    """`sherpa/ingest/graph_extract.py::available` は `llm.select_provider(..., bedrock=B, ...)`
    経由の auto 解決に Bedrock も含める（`intent`／`embed` は含めない）が、`_second_path_purposes`
    は Bedrock をこの第2経路の自動検出対象に含めない（openai／gemini のみが対象・挙動は変更しない）。
    したがって、chat/Codex の誰も Bedrock を使っていない（全員 ollama）構成では、SigV4 の
    手掛かりだけがある（中央キー無し）としても「消費されている」扱いにしない。"""
    from sherpa import agent_constructs
    from sherpa.providers import bedrock as bedrock_mod
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    monkeypatch.setattr(bedrock_mod.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    sys_s = {"cloud_provider": "bedrock"}   # 中央キー無し・SigV4 のみ
    assert doctor_checks._cloud_provider_consumed("bedrock", sys_s, []) is False


def test_second_path_purposes_excludes_bedrock(monkeypatch):
    """第2経路（intent／render／embed）の自動検出対象は openai／gemini のみ（Bedrock は対象外・
    上のテスト参照）。"""
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)   # 他テストモジュールの import 時設定から隔離
    assert doctor_checks._second_path_purposes("bedrock") == ()
    assert doctor_checks._second_path_purposes("ollama") == ()
    assert set(doctor_checks._second_path_purposes("openai")) == {"intent", "render", "embed"}
    assert set(doctor_checks._second_path_purposes("gemini")) == {"intent", "render", "embed"}


def test_second_path_purposes_excludes_embed_when_disabled_by_env(monkeypatch):
    """`SHERPA_DISABLE_EMBED`（`sherpa/embeddings.py::cfg` のキルスイッチ）が設定されている環境
    では embed は実際には一切自動解決されない＝対象から除く（intent／render はキルスイッチの
    対象外なので引き続き含める）。"""
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    assert set(doctor_checks._second_path_purposes("openai")) == {"intent", "render"}


def test_second_path_truthy_true_via_central_key(monkeypatch):
    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-real-key-1234567890"}
    assert doctor_checks._second_path_truthy("openai", sys_s, []) is True


def test_second_path_truthy_false_when_nothing_present():
    assert doctor_checks._second_path_truthy("openai", {"cloud_provider": "openai"}, []) is False


def test_second_path_truthy_true_via_personal_key_when_central_missing(monkeypatch):
    """本番はリクエストを行った利用者自身の `user_settings` を渡して `resolve_api_key()` を呼ぶ
    （`intent_llm.py::_cfg` 等は per-user 呼び出し）ため、中央キーが無くても A6（個人 API キー
    許可）が有効で有効な利用者の誰かが個人キーを保存済みなら、その利用者の操作では実際に消費
    される扱いにする（`has_openai_key` の真偽列だけを見る・値は読まない）。"""
    sys_s = {"cloud_provider": "openai", "personal_api_keys_allowed": True}
    rows = [{"agent": "ollama", "has_openai_key": False},
            {"agent": "ollama", "has_openai_key": True}]
    assert doctor_checks._second_path_truthy("openai", sys_s, rows) is True


def test_second_path_truthy_false_when_personal_keys_not_allowed(monkeypatch):
    sys_s = {"cloud_provider": "openai", "personal_api_keys_allowed": False}
    rows = [{"agent": "ollama", "has_openai_key": True}]
    assert doctor_checks._second_path_truthy("openai", sys_s, rows) is False


def test_second_path_truthy_false_when_a7_selects_different_provider(monkeypatch):
    """A6 が有効で個人キー保有者がいても、A7 選択が別プロバイダなら `resolve_api_key()` の A7
    ゲートにより常に `None`（保存キーは温存されるだけで解決されない）。"""
    sys_s = {"cloud_provider": "gemini", "personal_api_keys_allowed": True}
    rows = [{"agent": "ollama", "has_openai_key": True}]
    assert doctor_checks._second_path_truthy("openai", sys_s, rows) is False


def test_second_path_truthy_fail_closed_when_rows_none():
    """`rows` 不明時は「0人」ではなく fail-closed（消費している扱い）にする
    （`_personal_key_holder_count` とは向きが逆＝あちらは中央検査 SKIP への緩和判定、こちらは
    そもそも消費されているかどうかの判定）。"""
    sys_s = {"cloud_provider": "openai", "personal_api_keys_allowed": True}
    assert doctor_checks._second_path_truthy("openai", sys_s, None) is True


def test_cloud_provider_consumed_true_via_embeddings_path(monkeypatch):
    """`sherpa/embeddings.py::cfg` も `llm.select_provider(settings, openai=O, gemini=G, ollama=L,
    system_settings=sys_s)`（`bedrock` 未指定＝bedrock 非対応）で auto 解決する。A7 選択＋実キーが
    あれば chat/Codex の設定と無関係に openai/gemini を選ぶ＝「消費されている」扱いになることを
    固定する（gemini でも同様に確認）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    sys_s = {"cloud_provider": "gemini", "gemini_api_key": "real-gemini-key-1234567890"}
    assert doctor_checks._cloud_provider_consumed("gemini", sys_s, []) is True


def test_cloud_llm_probes_bedrock_placeholder_key_never_reaches_sdk_send(monkeypatch, tmp_path):
    """Bedrock は `health._ai_check_bedrock` を直接呼ぶため `_run_raw_llm_probe` の送信前ガードを
    経由しない。`health._ai_check_bedrock` 内部の認証ゲート（`_bedrock_auth_available`）は単純な
    truthy 判定のみでプレースホルダ（`sk-REPLACE_ME`）を弾かない契約のため、ガード無しだと
    プレースホルダのまま実際の SDK 送信（`messages.create()`）まで到達しうる。中央認証が
    使えない・個人キーも無いと判定できた時点で `health._ai_check_bedrock` 自体を呼ばず、
    送信ゼロで NG にすることを固定する（`Path.home()` を隔離し実行環境の `~/.aws/credentials`
    に依存しない）。`effective_agent()` を明示的に `"bedrock"` に固定し、`_cloud_provider_consumed`
    による SKIP に巻き込まれないようにする。"""
    from sherpa import agent_constructs, health
    from sherpa.providers import bedrock as bedrock_mod
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "bedrock")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: False)   # bedrock が有効な環境
    for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bedrock_mod.Path, "home", classmethod(lambda cls: tmp_path))

    def _should_not_be_called(*a, **k):
        raise AssertionError("プレースホルダキーのまま health._ai_check_bedrock を呼んではいけない")
    monkeypatch.setattr(health, "_ai_check_bedrock", _should_not_be_called)

    sys_s = {"cloud_provider": "bedrock", "bedrock_api_key": "sk-REPLACE_ME"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_bedrock"].status == "ng"
    assert by_id["llm_bedrock"].detail == "接続に失敗しました: auth（MissingApiKey）"


def test_cloud_llm_probes_bedrock_no_key_no_sigv4_never_reaches_sdk_send(monkeypatch, tmp_path):
    """中央キー自体が未設定（`None`）で SigV4 の手掛かりも無い場合も、同様に
    `health._ai_check_bedrock` を呼ばず送信ゼロで NG にする。"""
    from sherpa import agent_constructs, health
    from sherpa.providers import bedrock as bedrock_mod
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "bedrock")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: False)   # bedrock が有効な環境
    for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bedrock_mod.Path, "home", classmethod(lambda cls: tmp_path))

    def _should_not_be_called(*a, **k):
        raise AssertionError("認証手掛かりが無いのに health._ai_check_bedrock を呼んではいけない")
    monkeypatch.setattr(health, "_ai_check_bedrock", _should_not_be_called)

    sys_s = {"cloud_provider": "bedrock"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_bedrock"].status == "ng"


def test_cloud_llm_probes_bedrock_sigv4_present_still_probes(monkeypatch, tmp_path):
    """SigV4 の手掛かり（env）が実在すれば、これまで通り `health._ai_check_bedrock` を呼んで
    実プローブする（fail-closed ガードが正当な構成まで巻き込んで SKIP/NG にしないことの回帰）。"""
    from sherpa import agent_constructs, health
    from sherpa.providers import bedrock as bedrock_mod
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "bedrock")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: False)   # bedrock が有効な環境
    monkeypatch.setattr(bedrock_mod.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(health, "_ai_check_bedrock", lambda settings, sys_s, **k: None)

    sys_s = {"cloud_provider": "bedrock"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_bedrock"].status == "ok"


def test_cloud_llm_probes_bedrock_passes_max_retries_zero(monkeypatch, tmp_path):
    """doctor の Bedrock プローブは `max_retries=0` を明示する（SDK 既定のリトライで実 HTTP
    送信回数が呼び出し回数と食い違わないように）。"""
    from sherpa import agent_constructs, health
    from sherpa.providers import bedrock as bedrock_mod
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "bedrock")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: False)
    monkeypatch.setattr(bedrock_mod.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    captured = {}

    def _fake_check(settings, system_settings=None, *, max_retries=None):
        captured["max_retries"] = max_retries
    monkeypatch.setattr(health, "_ai_check_bedrock", _fake_check)

    sys_s = {"cloud_provider": "bedrock"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_bedrock"].status == "ok"
    assert captured["max_retries"] == 0


def test_cloud_llm_probes_bedrock_does_not_check_per_user_model(monkeypatch, tmp_path):
    """利用者別 `bedrock_model` 上書き・個人キーは doctor では検査しない（各利用者が設定画面の
    接続テストで確認する）。システム既定のみ1回だけ疎通確認し、`rows` の `bedrock_model` は
    一切参照しない。"""
    from sherpa import agent_constructs, health
    from sherpa.providers import bedrock as bedrock_mod
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "bedrock")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: False)
    monkeypatch.setattr(bedrock_mod.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    calls = []

    def _fake_check(settings, system_settings=None, **k):
        calls.append(settings.get("bedrock_model"))
    monkeypatch.setattr(health, "_ai_check_bedrock", _fake_check)

    sys_s = {"cloud_provider": "bedrock"}
    rows = [{"bedrock_model": "user-override-model"}, {"bedrock_model": "another-model"}]
    results = doctor_checks.check_cloud_llm_probes(sys_s, rows, probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert len(calls) == 1   # システム既定のみ・利用者ごとには増えない
    assert calls == [None]   # 利用者の bedrock_model は一切参照しない
    assert "llm_bedrock_1" not in by_id   # 複数行に分かれない
    assert by_id["llm_bedrock"].status == "ok"
    assert "利用者別モデル" in by_id["llm_bedrock"].detail


def test_sanitized_sys_s_for_bedrock_probe_clears_placeholder_key():
    sys_s = {"cloud_provider": "bedrock", "bedrock_api_key": "sk-REPLACE_ME"}
    sanitized = doctor_checks._sanitized_sys_s_for_bedrock_probe(sys_s)
    assert sanitized["bedrock_api_key"] is None
    assert sys_s["bedrock_api_key"] == "sk-REPLACE_ME"   # 元の dict は書き換えない


def test_sanitized_sys_s_for_bedrock_probe_keeps_real_key():
    sys_s = {"cloud_provider": "bedrock", "bedrock_api_key": "sk-real-key-1234567890"}
    sanitized = doctor_checks._sanitized_sys_s_for_bedrock_probe(sys_s)
    assert sanitized is sys_s   # 実キーなら書き換えない（コピーすら作らない）


def test_sanitized_sys_s_for_bedrock_probe_not_crash_when_key_is_non_string():
    """`bedrock_api_key`（JSONB）が非文字列でも `is_real_api_key()` の `AttributeError` を起こさず、
    無効キーとして扱ってサニタイズする（`_as_key_str()` 経由）。"""
    for bad in (42, {"unexpected": "object"}, ["a", "list"]):
        sys_s = {"cloud_provider": "bedrock", "bedrock_api_key": bad}
        sanitized = doctor_checks._sanitized_sys_s_for_bedrock_probe(sys_s)
        assert sanitized["bedrock_api_key"] is None


def test_run_raw_llm_probe_non_string_key_treated_as_missing(monkeypatch):
    """`resolve_api_key()` の解決結果が非文字列（JSONB の型不正）でも、`is_real_api_key()` の
    `AttributeError` で `_run_raw_llm_probe` 自体が壊れず、`_MissingApiKeyError`（送信ゼロ）へ
    正しく倒れる。"""
    from sherpa import keys
    from sherpa.ingest import graph_extract
    calls = []

    def _should_not_be_called(system, user, cfg, timeout=None):
        calls.append(cfg)
        raise AssertionError("非文字列キーのまま complete_json が呼ばれた")
    monkeypatch.setattr(graph_extract, "complete_json", _should_not_be_called)
    monkeypatch.setattr(keys, "resolve_api_key", lambda provider, s, **k: {"unexpected": "object"})
    e = doctor_checks._run_raw_llm_probe("openai", {"cloud_provider": "openai"})
    assert isinstance(e, doctor_checks._MissingApiKeyError)
    assert calls == []


def test_cloud_llm_probes_bedrock_placeholder_with_valid_sigv4_does_not_leak_placeholder_to_probe(
        monkeypatch, tmp_path):
    """中央キーがプレースホルダ（`sk-REPLACE_ME`）でも有効な SigV4 の手掛かりがあれば、doctor 自身
    の判定（`_central_auth_available`）は `ok`（実プローブへ進む）と判定する。この構成で
    `health._ai_check_bedrock` へプレースホルダをそのまま渡すと、`_ai_check_bedrock` 内部の
    `keys.resolve_api_key()` 再解決がプレースホルダを「キーあり」として拾い、
    `BedrockProvider(..., api_key="sk-REPLACE_ME")` を組み立ててしまう（Anthropic SDK は明示
    キーがあれば SigV4 へ進まない＝有効な SigV4 が設定されていてもプレースホルダのまま実際に
    送信してしまう）。`health._ai_check_bedrock` に渡る `system_settings` の `bedrock_api_key` が
    `None`（サニタイズ済み）になっていることを固定する。"""
    from sherpa import agent_constructs, health
    from sherpa.providers import bedrock as bedrock_mod
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "bedrock")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: False)
    monkeypatch.setattr(bedrock_mod.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    captured = {}

    def _fake_ai_check_bedrock(settings, system_settings=None, **k):
        captured["bedrock_api_key"] = system_settings.get("bedrock_api_key")
    monkeypatch.setattr(health, "_ai_check_bedrock", _fake_ai_check_bedrock)

    sys_s = {"cloud_provider": "bedrock", "bedrock_api_key": "sk-REPLACE_ME"}
    results = doctor_checks.check_cloud_llm_probes(sys_s, [], probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["llm_bedrock"].status == "ok"
    assert captured["bedrock_api_key"] is None


def test_run_raw_llm_probe_missing_key_sends_zero_http_requests(monkeypatch):
    """`keys.resolve_api_key()` の解決結果が `is_real_api_key()` を満たさない（`None`／空文字／
    プレースホルダ）場合、`graph_extract.complete_json`（実際に上流へ送信する境界）を一度も呼ばず
    `_MissingApiKeyError` を返す（`Authorization: Bearer None` のような壊れたリクエストを実際に
    送らない・fail-closed の送信前ガード）。"""
    from sherpa import keys
    from sherpa.ingest import graph_extract
    calls = []

    def _should_not_be_called(system, user, cfg, timeout=None):
        calls.append(cfg)
        raise AssertionError("実キーが無いのに complete_json が呼ばれた")
    monkeypatch.setattr(graph_extract, "complete_json", _should_not_be_called)
    for fake_key in (None, "", "sk-REPLACE_ME"):
        monkeypatch.setattr(keys, "resolve_api_key", lambda provider, s, **k: fake_key)
        e = doctor_checks._run_raw_llm_probe("openai", {"cloud_provider": "openai"})
        assert isinstance(e, doctor_checks._MissingApiKeyError), fake_key
    assert calls == []


def test_run_raw_llm_probe_preparation_error_does_not_propagate(monkeypatch):
    """キー解決・モデル解決・cfg 組み立て（準備処理）で例外が起きても、`_run_raw_llm_probe` の
    外へは伝播せず、例外オブジェクトとして返る（壊れた `model_catalog` 等で `run_all()` 全体が
    未捕捉の traceback で中断することを防ぐ）。"""
    from sherpa import model_catalog

    def _boom(*a, **k):
        raise RuntimeError("model_catalog is broken")
    monkeypatch.setattr(model_catalog, "resolve_model", _boom)
    e = doctor_checks._run_raw_llm_probe("openai", {"cloud_provider": "openai", "openai_api_key": "sk-real-key"})
    assert isinstance(e, RuntimeError)
    assert doctor_checks._classify_llm_probe_failure(e) == "error（RuntimeError）"


def test_embed_static_check_none_when_model_resolves_and_default_endpoint():
    sys_s = {"cloud_provider": "openai"}
    assert doctor_checks._embed_static_check("openai", sys_s) is None
    assert doctor_checks._embed_static_check("gemini", {"cloud_provider": "gemini"}) is None


def test_embed_static_check_reports_broken_catalog(monkeypatch):
    from sherpa import model_catalog

    def _boom(*a, **k):
        raise TypeError("model_catalog.openai.embed.allowed=1 のような壊れた設定")
    monkeypatch.setattr(model_catalog, "resolve_model", _boom)
    reason = doctor_checks._embed_static_check("openai", {})
    assert reason is not None


def test_embed_static_check_ng_when_azure_and_deployment_unregistered():
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1"}
    reason = doctor_checks._embed_static_check("openai", sys_s)
    assert reason is not None and "デプロイ名" in reason and "埋め込み" in reason


def test_embed_static_check_ok_when_azure_and_deployment_registered(monkeypatch):
    from sherpa import model_catalog
    monkeypatch.setattr(model_catalog, "resolve_model",
                        lambda provider, purpose, s, **k: "my-embed-deployment" if purpose == "embed" else "x")
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1"}
    assert doctor_checks._embed_static_check("openai", sys_s) is None


def test_embed_static_check_gemini_has_no_azure_equivalent():
    """Gemini には Azure のような別接続先の概念が無いため、モデルが解決できれば常に None。"""
    assert doctor_checks._embed_static_check("gemini", {"cloud_provider": "gemini"}) is None


# ---------------------------------------------------------------------------
# 3b. Codex の必須判定（有効な利用者の保存設定のみを走査・OpenAI 認証の要否も判定）
# ---------------------------------------------------------------------------

def test_codex_required_fail_closed_when_settings_unavailable():
    required, needs_openai_auth, note = doctor_checks._codex_required(None, None)
    assert required is True
    assert needs_openai_auth is True


def test_codex_required_fail_closed_when_rows_unavailable():
    required, needs_openai_auth, note = doctor_checks._codex_required({}, None)
    assert required is True
    assert needs_openai_auth is True


def test_codex_required_from_system_default_only(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "codex")
    monkeypatch.setattr(agent_constructs, "codex_model_provider", lambda *a, **k: "openai")
    required, needs_openai_auth, note = doctor_checks._codex_required({}, [])
    assert required is True
    assert needs_openai_auth is True
    assert "0" in note


def test_codex_required_detects_any_active_user_row(monkeypatch):
    """`agent` は per-user 設定。システム既定だけでなく有効な利用者の保存設定のいずれかが
    該当すれば必須とする。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent",
                         lambda settings, **k: (settings or {}).get("agent") or "openai")
    monkeypatch.setattr(agent_constructs, "codex_model_provider",
                         lambda settings, **k: (settings or {}).get("codex_model_provider") or "openai")
    rows = [{"agent": "openai", "codex_model_provider": None},
            {"agent": "codex", "codex_model_provider": "openai"}]
    required, needs_openai_auth, note = doctor_checks._codex_required({}, rows)
    assert required is True
    assert needs_openai_auth is True
    assert "2" in note


def test_codex_required_false_when_nobody_uses_it(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent",
                         lambda settings, **k: (settings or {}).get("agent") or "ollama")
    rows = [{"agent": "ollama", "codex_model_provider": None},
            {"agent": "openai", "codex_model_provider": None}]
    required, needs_openai_auth, note = doctor_checks._codex_required({}, rows)
    assert required is False
    assert needs_openai_auth is False


def test_codex_required_true_but_openai_auth_not_needed_when_only_ollama_backing(monkeypatch):
    """Codex を使う構成が全て
    `codex_model_provider == "ollama"` なら、Codex 自体は必須（CLI は要る）だが OpenAI/Azure の
    認証確認は不要。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent",
                         lambda settings, **k: (settings or {}).get("agent") or "ollama")
    monkeypatch.setattr(agent_constructs, "codex_model_provider",
                         lambda settings, **k: (settings or {}).get("codex_model_provider") or "openai")
    rows = [{"agent": "codex", "codex_model_provider": "ollama"}]
    required, needs_openai_auth, note = doctor_checks._codex_required({}, rows)
    assert required is True
    assert needs_openai_auth is False


def test_codex_required_openai_auth_needed_when_mixed_backing(monkeypatch):
    """Ollama backing の利用者と OpenAI/Azure backing の利用者が混在する場合は、後者がいる限り
    OpenAI 側の認証確認も必要。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent",
                         lambda settings, **k: (settings or {}).get("agent") or "ollama")
    monkeypatch.setattr(agent_constructs, "codex_model_provider",
                         lambda settings, **k: (settings or {}).get("codex_model_provider") or "openai")
    rows = [{"agent": "codex", "codex_model_provider": "ollama"},
            {"agent": "codex", "codex_model_provider": "openai"}]
    required, needs_openai_auth, note = doctor_checks._codex_required({}, rows)
    assert required is True
    assert needs_openai_auth is True


def test_codex_required_does_not_expose_user_ids(monkeypatch):
    """人数は出してよいが個々の user_id は出力に使わない。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    rows = [{"user_id": "alice", "agent": "openai", "codex_model_provider": None}]
    _required, _needs, note = doctor_checks._codex_required({}, rows)
    assert "alice" not in note


# ---------------------------------------------------------------------------
# 3c. Ollama の用途別プローブ（実効URL・用途・モデル単位）
# ---------------------------------------------------------------------------

def test_resolve_ollama_usages_none_when_settings_unavailable():
    assert doctor_checks._resolve_ollama_usages(None, []) is None
    assert doctor_checks._resolve_ollama_usages({}, None) is None


def test_resolve_ollama_usages_none_when_model_catalog_raises(monkeypatch):
    """壊れた設定（例: `model_catalog.openai.chat.allowed=1` のような型不正）で
    `model_catalog.resolve_model()` が例外を投げても、`_resolve_ollama_usages` の外へは
    伝播せず（`run_all()` 全体を巻き込まない）、既存の「判定できない」契約（`None`）に乗せる。"""
    from sherpa import model_catalog

    def _boom(*a, **k):
        raise TypeError("model_catalog.openai.chat.allowed=1 のような壊れた設定")
    monkeypatch.setattr(model_catalog, "resolve_model", _boom)
    assert doctor_checks._resolve_ollama_usages({}, []) is None


def test_resolve_ollama_usages_none_when_per_user_ollama_url_is_non_string(monkeypatch):
    """`user_settings.ollama_url`（JSONB）が文字列以外（オブジェクト等・破損した設定・移行時の
    型変換ミス等）だと、`(url, model)` を辞書キーとして使う集約処理が `TypeError`（ハッシュ不能）
    を投げうる。`_add()` が型検証してこれを吸収し、関数全体を安全側で `None`（NG 扱い）に倒す
    ことを固定する（黙って無視して SKIP 相当にしない）。"""
    from sherpa import agent_constructs, model_catalog
    monkeypatch.setattr(agent_constructs, "effective_agent",
                        lambda settings, **k: (settings or {}).get("agent") or "codex")
    monkeypatch.setattr(model_catalog, "resolve_model", lambda provider, usage, *a, **k: "chat-model")
    rows = [{"agent": "ollama", "codex_model_provider": None,
            "ollama_url": {"unexpected": "object"}, "search_helper": ""}]
    assert doctor_checks._resolve_ollama_usages({}, rows) is None


def test_resolve_ollama_usages_none_when_system_default_ollama_url_is_non_string(monkeypatch):
    """システム既定側（`user_settings` を経由しない `sys_s.get("ollama_url")`）が文字列以外でも
    同様に `None` へ倒す（`try/except Exception: pass` で握り潰して黙って SKIP にしない）。"""
    from sherpa import agent_constructs, keys, model_catalog
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    monkeypatch.setattr(keys, "resolve_ollama_url", lambda settings, **k: ["not", "a", "string"])
    monkeypatch.setattr(model_catalog, "resolve_model", lambda provider, usage, *a, **k: "chat-model")
    assert doctor_checks._resolve_ollama_usages({}, []) is None


def test_resolve_ollama_usages_none_when_resolve_ollama_url_raises(monkeypatch):
    """`keys.resolve_ollama_url()` 自体が想定外の例外を投げても（型不正な入力での内部エラー等）、
    `run_all()` を巻き込まず `None`（NG 扱い）に倒す。"""
    from sherpa import agent_constructs, keys, model_catalog
    monkeypatch.setattr(agent_constructs, "effective_agent",
                        lambda settings, **k: (settings or {}).get("agent") or "codex")
    monkeypatch.setattr(model_catalog, "resolve_model", lambda provider, usage, *a, **k: "chat-model")

    def _boom(*a, **k):
        raise TypeError("ollama_url 解決中の想定外エラー")
    monkeypatch.setattr(keys, "resolve_ollama_url", _boom)
    rows = [{"agent": "ollama", "codex_model_provider": None, "ollama_url": None, "search_helper": ""}]
    assert doctor_checks._resolve_ollama_usages({}, rows) is None


def test_resolve_ollama_usages_none_when_system_default_resolve_ollama_url_raises(monkeypatch):
    """システム既定の実効頭脳が ollama のとき、`keys.resolve_ollama_url()` 自体が想定外の例外
    （TypeError 等）を投げても、黙って「システム既定は ollama を使っていない」に丸めず
    `None`（判定不能→NG 扱い）へ倒す。"""
    from sherpa import agent_constructs, keys, model_catalog
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    monkeypatch.setattr(model_catalog, "resolve_model", lambda provider, usage, *a, **k: "chat-model")

    def _boom(*a, **k):
        raise TypeError("ollama_url 解決中の想定外エラー")
    monkeypatch.setattr(keys, "resolve_ollama_url", _boom)
    assert doctor_checks._resolve_ollama_usages({}, []) is None


def test_resolve_ollama_usages_none_when_per_row_effective_agent_raises(monkeypatch):
    """利用者行側で `agent_constructs.effective_agent()` が例外を投げても、その行を黙って
    「使っていない」に丸めず、`None`（判定不能→NG）へ倒す。"""
    from sherpa import agent_constructs, keys, model_catalog
    monkeypatch.setattr(keys, "resolve_ollama_url", lambda settings, **k: "http://localhost:11434")
    monkeypatch.setattr(model_catalog, "resolve_model", lambda provider, usage, *a, **k: "chat-model")

    def _boom(settings, **k):
        if settings is None:
            return "openai"   # システム既定側は問題なく解決できる
        raise TypeError("effective_agent 解決中の想定外エラー")
    monkeypatch.setattr(agent_constructs, "effective_agent", _boom)
    rows = [{"agent": "ollama", "codex_model_provider": None, "ollama_url": None, "search_helper": ""}]
    assert doctor_checks._resolve_ollama_usages({}, rows) is None


def test_resolve_ollama_usages_empty_when_nobody_uses_ollama(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    rows = [{"agent": "openai", "codex_model_provider": None, "ollama_url": None, "search_helper": ""}]
    assert doctor_checks._resolve_ollama_usages({}, rows) == []


def test_resolve_ollama_usages_system_default_chat(monkeypatch):
    from sherpa import agent_constructs, keys, model_catalog
    monkeypatch.setattr(agent_constructs, "effective_agent",
                         lambda settings, **k: "ollama" if not settings else None)
    monkeypatch.setattr(keys, "resolve_ollama_url", lambda settings, **k: "http://localhost:11434")
    monkeypatch.setattr(model_catalog, "resolve_model", lambda provider, usage, *a, **k: f"{provider}-{usage}-model")
    usages = doctor_checks._resolve_ollama_usages({}, [])
    assert len(usages) == 1
    assert usages[0]["url"] == "http://localhost:11434"
    assert usages[0]["model"] == "ollama-chat-model"
    assert "システム既定" in usages[0]["purposes"][0]


def test_resolve_ollama_usages_per_user_override_url(monkeypatch):
    from sherpa import agent_constructs, keys, model_catalog
    monkeypatch.setattr(agent_constructs, "effective_agent",
                         lambda settings, **k: (settings or {}).get("agent") or "openai")
    monkeypatch.setattr(keys, "resolve_ollama_url",
                         lambda settings, **k: (settings or {}).get("ollama_url") or "http://localhost:11434")
    monkeypatch.setattr(model_catalog, "resolve_model", lambda provider, usage, *a, **k: "chat-model")
    rows = [{"agent": "ollama", "codex_model_provider": None,
            "ollama_url": "http://personal-ollama:11434", "search_helper": ""}]
    usages = doctor_checks._resolve_ollama_usages({}, rows)
    assert len(usages) == 1
    assert usages[0]["url"] == "http://personal-ollama:11434"


def test_resolve_ollama_usages_codex_ollama_backing_uses_codex_model(monkeypatch):
    from sherpa import agent_constructs, keys, model_catalog
    monkeypatch.setattr(agent_constructs, "effective_agent",
                         lambda settings, **k: (settings or {}).get("agent") or "openai")
    monkeypatch.setattr(keys, "resolve_ollama_url", lambda settings, **k: "http://localhost:11434")
    monkeypatch.setattr(model_catalog, "resolve_model", lambda provider, usage, *a, **k: f"{provider}-{usage}")
    rows = [{"agent": "codex", "codex_model_provider": "ollama", "ollama_url": None, "search_helper": ""}]
    usages = doctor_checks._resolve_ollama_usages({}, rows)
    assert len(usages) == 1
    assert usages[0]["model"] == "codex-codex"
    assert "Codex" in usages[0]["purposes"][0]


def test_resolve_ollama_usages_search_helper_when_main_agent_is_openai(monkeypatch):
    from sherpa import agent_constructs, search_helper
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    monkeypatch.setattr(search_helper, "resolve",
                         lambda settings, **k: {"provider": "ollama", "url": "http://localhost:11434",
                                               "model": "qwen2.5"})
    rows = [{"agent": "openai", "codex_model_provider": None, "ollama_url": None, "search_helper": "ollama"}]
    usages = doctor_checks._resolve_ollama_usages({}, rows)
    assert len(usages) == 1
    assert usages[0]["model"] == "qwen2.5"
    assert "検索ヘルパー" in usages[0]["purposes"][0]


def test_resolve_ollama_usages_search_helper_ignored_when_main_agent_not_openai(monkeypatch):
    """検索ヘルパーは主頭脳が openai のときだけ実際に配線される
    （`sherpa/providers/__init__.py::get_provider` 参照）。主頭脳が codex/ollama の利用者の
    `search_helper` 列は runtime で一切評価されないため、ここでも解決を試みない。"""
    from sherpa import agent_constructs, search_helper
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "codex")

    def _should_not_be_called(*a, **k):
        raise AssertionError("主頭脳が openai でないのに search_helper.resolve を呼んではいけない")
    monkeypatch.setattr(search_helper, "resolve", _should_not_be_called)
    rows = [{"agent": "codex", "codex_model_provider": "openai", "ollama_url": None, "search_helper": "ollama"}]
    usages = doctor_checks._resolve_ollama_usages({}, rows)
    assert usages == []


def test_resolve_ollama_usages_none_when_search_helper_resolve_raises(monkeypatch):
    """`search_helper.resolve()` は本番では例外を捕捉しない（呼び出し元が壊れた設定をそのまま
    検出する契約）。この行が「検索ヘルパーは使っていない」に丸められて黙って SKIP に落ちないよう、
    `None`（判定不能→NG 扱い）へ倒す。"""
    from sherpa import agent_constructs, search_helper
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")

    def _boom(*a, **k):
        raise RuntimeError("search_helper 解決中の想定外エラー")
    monkeypatch.setattr(search_helper, "resolve", _boom)
    rows = [{"agent": "openai", "codex_model_provider": None, "ollama_url": None, "search_helper": "ollama"}]
    assert doctor_checks._resolve_ollama_usages({}, rows) is None


def test_resolve_ollama_usages_dedupes_same_url_and_model(monkeypatch):
    from sherpa import agent_constructs, keys, model_catalog
    monkeypatch.setattr(agent_constructs, "effective_agent",
                         lambda settings, **k: (settings or {}).get("agent") or "openai")
    monkeypatch.setattr(keys, "resolve_ollama_url", lambda settings, **k: "http://localhost:11434")
    monkeypatch.setattr(model_catalog, "resolve_model", lambda provider, usage, *a, **k: "chat-model")
    rows = [{"agent": "ollama", "codex_model_provider": None, "ollama_url": None, "search_helper": ""},
            {"agent": "ollama", "codex_model_provider": None, "ollama_url": None, "search_helper": ""}]
    usages = doctor_checks._resolve_ollama_usages({}, rows)
    assert len(usages) == 1
    assert len(usages[0]["purposes"]) == 1   # 用途ラベルも重複しない


# ---------------------------------------------------------------------------
# 3d. Ollama 用途プローブ本体（実効URL・厳密タグ一致・不正応答の耐性）
# ---------------------------------------------------------------------------

def test_probe_ollama_usage_ok_exact_tag_match(monkeypatch):
    from sherpa import llm
    monkeypatch.setattr(llm, "urlopen_no_redirect",
                         lambda url, timeout=None: _FakeOllamaResponse(_tags_response("qwen2.5:latest")))
    ok, detail = doctor_checks._probe_ollama_usage("http://localhost:11434", "qwen2.5", {})
    assert ok is True
    assert "qwen2.5:latest" in detail


def test_probe_ollama_usage_false_ok_regression_only_other_tag_pulled(monkeypatch):
    """設定は `qwen2.5`（暗黙 `:latest`）だが実際に pull 済みなのは `qwen2.5:7b` だけ、という構成は
    実行時に解決できない（Ollama は無指定なら `:latest` を要求する）ため NG（タグを無視した名前
    だけの一致は false OK になる）。"""
    from sherpa import llm
    monkeypatch.setattr(llm, "urlopen_no_redirect",
                         lambda url, timeout=None: _FakeOllamaResponse(_tags_response("qwen2.5:7b")))
    ok, detail = doctor_checks._probe_ollama_usage("http://localhost:11434", "qwen2.5", {})
    assert ok is False
    assert "qwen2.5:latest" in detail
    assert "qwen2.5:7b" in detail   # 「モデル名一致・タグ違い」の案内に別タグを列挙する


def test_probe_ollama_usage_ng_when_no_matching_model_name_at_all(monkeypatch):
    from sherpa import llm
    monkeypatch.setattr(llm, "urlopen_no_redirect",
                         lambda url, timeout=None: _FakeOllamaResponse(_tags_response("llama3:latest")))
    ok, detail = doctor_checks._probe_ollama_usage("http://localhost:11434", "qwen2.5", {})
    assert ok is False
    assert "pull" in detail


def test_probe_ollama_usage_explicit_tag_requires_exact_match(monkeypatch):
    """設定側が明示タグ（`qwen2.5:7b`）を指定している場合は、そのタグそのものが無いと NG
    （`:latest` への暗黙読み替えは行わない）。"""
    from sherpa import llm
    monkeypatch.setattr(llm, "urlopen_no_redirect",
                         lambda url, timeout=None: _FakeOllamaResponse(_tags_response("qwen2.5:latest")))
    ok, detail = doctor_checks._probe_ollama_usage("http://localhost:11434", "qwen2.5:7b", {})
    assert ok is False
    assert "qwen2.5:7b" in detail


def test_probe_ollama_usage_passes_system_settings_to_ollama_url(monkeypatch):
    """`llm.ollama_url()` へ読み取り専用 SELECT 済みの system_settings を明示的に渡す（省略すると
    SSRF 許可判定が `sherpa.store.get_system_settings()`＝DDL を発火しうる高水準 API を自分で
    読みに行ってしまう）。"""
    from sherpa import llm
    captured = {}

    def _fake_ollama_url(base, path, *, extra_allowed=None, system_settings=None):
        captured["system_settings"] = system_settings
        return base.rstrip("/") + path
    monkeypatch.setattr(llm, "ollama_url", _fake_ollama_url)
    sentinel = {"cloud_provider": "openai"}
    monkeypatch.setattr(llm, "urlopen_no_redirect",
                         lambda url, timeout=None: _FakeOllamaResponse(_tags_response("qwen2.5:latest")))
    doctor_checks._probe_ollama_usage("http://localhost:11434", "qwen2.5", sentinel)
    assert captured["system_settings"] is sentinel


@pytest.mark.parametrize("payload", [
    b"null",
    b"42",
    b'{"models": 1}',
    b'{"models": "not-a-list"}',
    b'{"models": [1, 2, 3]}',
    b"not even json",
])
def test_probe_ollama_usage_ng_without_crashing_on_malformed_response(monkeypatch, payload):
    """`/api/tags` の応答が想定外の形（JSON でない・`models` が配列でない・数値等）でも、
    TypeError 等を外へ漏らさず必ず `(False, ...)` を返す。"""
    from sherpa import llm
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda url, timeout=None: _FakeOllamaResponse(payload))
    ok, detail = doctor_checks._probe_ollama_usage("http://localhost:11434", "qwen2.5", {})
    assert ok is False
    assert isinstance(detail, str) and detail


def test_probe_ollama_usage_does_not_embed_raw_exception_text(monkeypatch):
    """接続失敗・URL 解析不能時の理由は `health._classify()` による安全な分類のみで構成し、生の
    例外文字列（`SsrfBlocked`／`ValueError` 等が不正な URL 自体を repr で含みうる）をそのまま
    埋め込まない（パターンベースの秘密マスクでは検出できない値の再連結を避ける）。"""
    from sherpa import llm

    def _boom(url, timeout=None):
        raise RuntimeError("http://user:hunter2@localhost:11434/api/tags failed")
    monkeypatch.setattr(llm, "urlopen_no_redirect", _boom)
    ok, detail = doctor_checks._probe_ollama_usage("http://localhost:11434", "qwen2.5", {})
    assert ok is False
    assert "hunter2" not in detail
    assert "localhost:11434" in detail   # 匿名化表示（host[:port]）自体は残る


def test_probe_ollama_usage_anonymizes_display_target(monkeypatch):
    from sherpa import llm

    def _boom(url, timeout=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(llm, "urlopen_no_redirect", _boom)
    ok, detail = doctor_checks._probe_ollama_usage("http://user:hunter2@localhost:11434", "qwen2.5", {})
    assert ok is False
    assert "hunter2" not in detail


def test_probe_ollama_usage_url_display_resolution_does_not_crash_on_non_string_url(monkeypatch):
    """`ollama_url` に非文字列（設定破損等）が入っていても、表示用の解析
    （`llm._redact_url_for_error`）で例外を起こしてレポート全体を落とさない。"""
    from sherpa import llm

    def _boom(url, timeout=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(llm, "urlopen_no_redirect", _boom)
    ok, detail = doctor_checks._probe_ollama_usage(123, "qwen2.5", {})
    assert ok is False
    assert isinstance(detail, str) and detail


_HOST = doctor_checks._OLLAMA_DEFAULT_HOST
_NS = doctor_checks._OLLAMA_DEFAULT_NAMESPACE


@pytest.mark.parametrize("ref,expected", [
    ("qwen2.5", (_HOST, _NS, "qwen2.5", "latest")),
    ("qwen2.5:7b", (_HOST, _NS, "qwen2.5", "7b")),
    ("library/qwen2.5", (_HOST, _NS, "qwen2.5", "latest")),
    ("library/qwen2.5:7b", (_HOST, _NS, "qwen2.5", "7b")),
    ("myorg/qwen2.5:7b", (_HOST, "myorg", "qwen2.5", "7b")),
    # 既定レジストリ・既定名前空間は明示されていても省略と等価（3要素そろえた完全修飾）。
    ("registry.ollama.ai/library/qwen2.5", (_HOST, _NS, "qwen2.5", "latest")),
    ("registry.ollama.ai/library/qwen2.5:7b", (_HOST, _NS, "qwen2.5", "7b")),
    # スラッシュ1個（namespace/model）は host を指定できない＝先頭要素がドメイン風でも
    # 「既定 host を省略した custom host」ではなく「namespace が registry.ollama.ai という
    # 別モデル」を意味する……はずだが、namespace の構成文字として `.` は無効（Ollama 公式
    # grammar は namespace に `.` を認めない）ため、この参照自体が不正（`None`）。
    ("registry.ollama.ai/qwen2.5", None),
    # custom host は host[:port]/namespace/model の3要素そろえて初めて成立する。
    ("registry.example.com:5000/myorg/qwen2.5", ("registry.example.com:5000", "myorg", "qwen2.5", "latest")),
    ("registry.example.com:5000/myorg/qwen2.5:7b", ("registry.example.com:5000", "myorg", "qwen2.5", "7b")),
    # 2要素で先頭がドメイン風でも custom host とは解釈しない（namespace 扱い）だが、
    # namespace の構成文字として `:` は無効（`:` は host のポート区切り・タグ区切り専有）＝
    # 2要素形では host を指定できないため、この参照は不正（`None`）。誤って受理すると
    # 「見つからない＝pull すれば取得できる」という誤案内につながる。
    ("registry.example.com:5000/qwen2.5", None),
    # scheme 付きの完全修飾（3要素）も有効（scheme は host 部分の前置修飾として無視する）。
    ("https://registry.example.com/myorg/qwen2.5:7b", ("registry.example.com", "myorg", "qwen2.5", "7b")),
    # scheme 付きで3要素そろっていない参照は不正（scheme 無しの短縮形と同一視しない＝
    # 実行時に送信されるのは正規化前の生の参照文字列そのものであり、doctor が「一致」と
    # 誤判定すると false-green になる）。
    ("http://qwen2.5", None),
    ("https://library/qwen2.5", None),
    ("http://myorg/qwen2.5", None),
    # 大小文字は区別しない。
    ("QWEN2.5:LATEST", (_HOST, _NS, "qwen2.5", "latest")),
    ("Registry.Ollama.AI/Library/Qwen2.5", (_HOST, _NS, "qwen2.5", "latest")),
    # 明示的な ":" の直後にタグが無い参照は不正（暗黙の :latest 補完とは別物）。
    ("qwen2.5:", None),
    # 4要素以上（スラッシュ3個以上）は不正。
    ("a/b/c/d", None),
    # 文字種違反: namespace/model/tag の構成文字として `:` は無効（host のポート区切り・
    # タグ区切りとしてのみ有効・かつ3要素形でしか host を指定できない）。2要素形の先頭要素は
    # 常に namespace 扱いのため、`:` を含む時点で不正。
    ("my:org/qwen2.5", None),
    # namespace の構成文字として `.` は無効（model/tag は許可・namespace だけ不許可）。
    ("my.org/qwen2.5", None),
    # 空白・記号（許可文字集合外）は namespace/model/tag に使えない。
    ("my org/qwen2.5", None),
    ("myorg/qwen2.5:tag with space", None),
    ("myorg/qwen2.5!", None),
    # 先頭の `_` ・末尾の `_`／`-`（model/tag は `.` も）は許可される（Ollama 公式 grammar）。
    ("_myorg/qwen2.5", (_HOST, "_myorg", "qwen2.5", "latest")),
    ("myorg_/qwen2.5", (_HOST, "myorg_", "qwen2.5", "latest")),
    ("myorg-/qwen2.5", (_HOST, "myorg-", "qwen2.5", "latest")),
    ("myorg/_qwen2.5", (_HOST, "myorg", "_qwen2.5", "latest")),
    ("myorg/qwen2.5_", (_HOST, "myorg", "qwen2.5_", "latest")),
    ("myorg/qwen2.5:_7b", (_HOST, "myorg", "qwen2.5", "_7b")),
    # 長さ上限: namespace/model/tag は各80文字（`_OLLAMA_NAME_PART_MAX_LEN`）。
    ("a" * 81, None),
    ("a" * 80, (_HOST, _NS, "a" * 80, "latest")),      # 上限ちょうどは許容（境界値）。
    ("myorg/" + "a" * 81, None),
    ("myorg/" + "a" * 80, (_HOST, "myorg", "a" * 80, "latest")),
])
def test_normalize_ollama_ref(ref, expected):
    assert doctor_checks._normalize_ollama_ref(ref) == expected


def test_normalize_ollama_ref_host_max_length_boundary():
    """host の長さ上限は350文字（`_OLLAMA_HOST_MAX_LEN`・Ollama 公式 grammar）。"""
    host_350 = "h" * 346 + ".com"
    host_351 = "h" * 347 + ".com"
    assert len(host_350) == 350 and len(host_351) == 351
    assert doctor_checks._normalize_ollama_ref(f"{host_350}/myorg/qwen2.5") is not None
    assert doctor_checks._normalize_ollama_ref(f"{host_351}/myorg/qwen2.5") is None


@pytest.mark.parametrize("ref,expected", [
    # host の `_` は先頭・内部・末尾いずれの位置でも許可（Ollama 公式 grammar）。
    ("_registry/myorg/model", ("_registry", "myorg", "model", "latest")),
    ("registry_/myorg/model", ("registry_", "myorg", "model", "latest")),
    ("my_registry/myorg/model", ("my_registry", "myorg", "model", "latest")),
    # host の末尾 `-`／`.` も許可。
    ("registry-/myorg/model", ("registry-", "myorg", "model", "latest")),
    ("registry./myorg/model", ("registry.", "myorg", "model", "latest")),
])
def test_normalize_ollama_ref_host_allows_underscore_and_trailing_symbols(ref, expected):
    assert doctor_checks._normalize_ollama_ref(ref) == expected


@pytest.mark.parametrize("ref", [
    "-registry/myorg/model",       # host 先頭の `-` は不可
    ".registry/myorg/model",       # host 先頭の `.` は不可
    "registry/-myorg/model",       # namespace 先頭の `-` は不可
    "registry/myorg/-model",       # model 先頭の `-` は不可
    "registry/myorg/.model",       # model 先頭の `.` は不可
    "registry/myorg/model:-tag",   # tag 先頭の `-` は不可
    "registry/myorg/model:.tag",   # tag 先頭の `.` は不可
])
def test_normalize_ollama_ref_rejects_leading_hyphen_or_dot(ref):
    """各 part の先頭文字は英数字または `_` のみ（Ollama 公式 grammar）。先頭の `-`／`.` は
    内部/末尾では許可される記号だが、先頭に限っては不正とする。"""
    assert doctor_checks._normalize_ollama_ref(ref) is None


@pytest.mark.parametrize("ref,expected", [
    # host は先頭以外なら `:` を自由に含められる（数字 port に限定しない・Ollama 公式 grammar）。
    ("host:abc/myorg/model", ("host:abc", "myorg", "model", "latest")),
    ("host:5000:extra/myorg/model", ("host:5000:extra", "myorg", "model", "latest")),
])
def test_normalize_ollama_ref_host_colon_not_limited_to_numeric_port(ref, expected):
    assert doctor_checks._normalize_ollama_ref(ref) == expected


def test_normalize_ollama_ref_rejects_trailing_newline():
    """`match()` ＋ 末尾 `$` は対象文字列の末尾に改行が1つ付いていてもマッチしてしまう
    （Python 正規表現の `$` は「文字列末尾」だけでなく「末尾の改行の直前」にもマッチする仕様）。
    `fullmatch()` に切り替えたことで、末尾に改行が紛れ込んだ値を正しく拒否する。"""
    assert doctor_checks._normalize_ollama_ref("qwen2.5\n") is None
    assert doctor_checks._normalize_ollama_ref("myorg\n/qwen2.5") is None
    assert doctor_checks._normalize_ollama_ref("myorg/qwen2.5\n") is None
    assert doctor_checks._normalize_ollama_ref("myorg/qwen2.5:7b\n") is None
    # 改行が無ければ引き続き有効（回帰確認）。
    assert doctor_checks._normalize_ollama_ref("qwen2.5") is not None


def test_normalize_ollama_ref_scheme_qualified_equals_bare_form():
    """scheme 付き完全修飾参照は、scheme 無しの同じ参照と同一の正規形へ畳み込まれる。"""
    a = doctor_checks._normalize_ollama_ref("https://registry.example.com/myorg/qwen2.5:7b")
    b = doctor_checks._normalize_ollama_ref("registry.example.com/myorg/qwen2.5:7b")
    assert a is not None and a == b


def test_normalize_ollama_ref_scheme_qualified_short_form_is_rejected_not_equated():
    """`http://qwen2.5`／`https://library/qwen2.5` を、scheme 無しの短縮形（`qwen2.5`）と同一視
    しない。実行時は正規化前の生の参照文字列がそのまま Codex/Ollama クライアントへ渡るため、
    doctor がここで「一致」と判定しても実際には解決できない参照を誤って OK にする false-green
    になる（scheme 検出時は host/namespace/model の3要素必須という Ollama 公式 parser の
    grammar に従う）。"""
    assert doctor_checks._normalize_ollama_ref("http://qwen2.5") is None
    assert doctor_checks._normalize_ollama_ref("https://library/qwen2.5") is None
    # 参考: scheme 無しなら同じ短縮形は有効（不正化の対象は「scheme 付きなのに短縮形」の組合せ）。
    assert doctor_checks._normalize_ollama_ref("qwen2.5") is not None
    assert doctor_checks._normalize_ollama_ref("library/qwen2.5") is not None


def test_normalize_ollama_ref_default_host_without_namespace_is_distinct_model():
    """`otherns/qwen2.5`（スラッシュ1個）は「既定 host を省略した参照」ではなく
    namespace が `otherns` という別のモデルを指す＝裸の `qwen2.5` とは一致しない。"""
    a = doctor_checks._normalize_ollama_ref("otherns/qwen2.5")
    b = doctor_checks._normalize_ollama_ref("qwen2.5")
    assert a is not None and b is not None and a != b


def test_normalize_ollama_ref_namespace_with_dot_looking_like_host_is_rejected():
    """`registry.ollama.ai/qwen2.5`（スラッシュ1個）の先頭要素はドメイン風に見えても常に
    namespace 扱いになる（host を指定できるのは3要素形のみ）が、namespace の構成文字として
    `.` は無効（Ollama 公式 grammar は namespace に `.` を認めない・model/tag とは異なる文字
    集合）＝この参照は不正（`None`）。"""
    assert doctor_checks._normalize_ollama_ref("registry.ollama.ai/qwen2.5") is None


def test_probe_ollama_usage_library_prefix_matches_bare_repo_name(monkeypatch):
    """Ollama が `library/qwen2.5` として返し、設定側は `qwen2.5` の場合（またはその逆）でも、
    `library/` 名前空間は省略と等価として一致させる（生文字列比較だと false NG になるケース）。"""
    from sherpa import llm
    monkeypatch.setattr(llm, "urlopen_no_redirect",
                         lambda url, timeout=None: _FakeOllamaResponse(_tags_response("library/qwen2.5:latest")))
    ok, detail = doctor_checks._probe_ollama_usage("http://localhost:11434", "qwen2.5", {})
    assert ok is True


def test_probe_ollama_usage_default_registry_host_matches_bare_repo_name(monkeypatch):
    """既定レジストリ（`registry.ollama.ai`）を明示した参照も省略形と同一視する。"""
    from sherpa import llm
    monkeypatch.setattr(
        llm, "urlopen_no_redirect",
        lambda url, timeout=None: _FakeOllamaResponse(_tags_response("registry.ollama.ai/library/qwen2.5:latest")))
    ok, detail = doctor_checks._probe_ollama_usage("http://localhost:11434", "qwen2.5", {})
    assert ok is True


def test_probe_ollama_usage_case_insensitive_match(monkeypatch):
    from sherpa import llm
    monkeypatch.setattr(llm, "urlopen_no_redirect",
                         lambda url, timeout=None: _FakeOllamaResponse(_tags_response("QWen2.5:LATEST")))
    ok, detail = doctor_checks._probe_ollama_usage("http://localhost:11434", "qwen2.5", {})
    assert ok is True


def test_probe_ollama_usage_ng_when_configured_model_ref_is_invalid(monkeypatch):
    """明示的な `:` の後にタグが無い設定値（例 `"qwen2.5:"`）は、暗黙の `:latest` 解決とは別物の
    不正な参照として NG にする（接続先が生きていて目的のタグが実在しても「確認できた」としない）。"""
    from sherpa import llm
    monkeypatch.setattr(llm, "urlopen_no_redirect",
                         lambda url, timeout=None: _FakeOllamaResponse(_tags_response("qwen2.5:latest")))
    ok, detail = doctor_checks._probe_ollama_usage("http://localhost:11434", "qwen2.5:", {})
    assert ok is False
    assert "不正" in detail


def test_probe_ollama_usage_invalid_charset_ref_reports_invalid_not_pull_needed(monkeypatch):
    """文字種違反（namespace に `:` を含む等）で不正な参照は「不正です」と正しく案内し、
    「見つかりません（ollama pull が必要な可能性）」という**誤った**案内をしない
    （`_normalize_ollama_ref` は要素数だけでなく文字種も検証する）。接続先は
    生きていて、たまたま無関係なモデルが1つ pull 済みという状況でも区別できることを確認する。"""
    from sherpa import llm
    monkeypatch.setattr(llm, "urlopen_no_redirect",
                         lambda url, timeout=None: _FakeOllamaResponse(_tags_response("qwen2.5:latest")))
    ok, detail = doctor_checks._probe_ollama_usage(
        "http://localhost:11434", "registry.example.com:5000/qwen2.5", {})
    assert ok is False
    assert "不正" in detail
    assert "pull" not in detail


def test_check_ollama_probes_ng_when_usages_undeterminable():
    results = doctor_checks.check_ollama_probes(None, None)
    assert len(results) == 1
    assert results[0].status == "ng"


def test_check_ollama_probes_skip_when_unused(monkeypatch):
    monkeypatch.setattr(doctor_checks, "_resolve_ollama_usages", lambda *a, **k: [])
    results = doctor_checks.check_ollama_probes({}, [])
    assert len(results) == 1
    assert results[0].status == "skip"


def test_check_ollama_probes_one_item_per_usage(monkeypatch):
    monkeypatch.setattr(doctor_checks, "_resolve_ollama_usages", lambda *a, **k: [
        {"url": "http://a:11434", "model": "m1", "purposes": ["用途A"]},
        {"url": "http://b:11434", "model": "m2", "purposes": ["用途B"]},
    ])
    monkeypatch.setattr(doctor_checks, "_probe_ollama_usage", lambda url, model, sys_s: (True, "ok"))
    results = doctor_checks.check_ollama_probes({}, [])
    assert len(results) == 2
    assert len({r.id for r in results}) == 2   # id が重複しない


def test_check_ollama_probes_passes_sys_s_through(monkeypatch):
    monkeypatch.setattr(doctor_checks, "_resolve_ollama_usages", lambda *a, **k: [
        {"url": "http://a:11434", "model": "m1", "purposes": ["用途A"]},
    ])
    captured = {}

    def _fake_probe(url, model, sys_s):
        captured["sys_s"] = sys_s
        return True, "ok"
    monkeypatch.setattr(doctor_checks, "_probe_ollama_usage", _fake_probe)
    sentinel = {"cloud_provider": "openai"}
    doctor_checks.check_ollama_probes(sentinel, [])
    assert captured["sys_s"] is sentinel


# ---------------------------------------------------------------------------
# 4. Codex 経路
# ---------------------------------------------------------------------------

def test_check_codex_all_ng_when_indeterminate_even_if_cli_present_and_required_true(monkeypatch):
    """`indeterminate=True` の間は、CLI が実際に導入・ログイン済み（`required`／
    `needs_openai_auth` が `True` のまま通常判定へ進めば `ok` になりうる構成）でも、3項目全てを
    固定文言の NG にする（`_codex_required` の fail-closed な真偽値だけに頼ると、判定不能
    だったこと自体が握り潰されて `ok` に見えてしまう＝誤帰属の是正）。"""
    class _Proc:
        returncode = 0
        stdout = "codex-cli 0.144.1\n"
        stderr = ""
    from sherpa import health
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(health, "_ai_check_codex", lambda settings, sys_s: None)
    results = doctor_checks.check_codex({}, [], required=True, needs_openai_auth=True,
                                        note="", probe_cloud=False, indeterminate=True)
    by_id = {r.id: r for r in results}
    assert by_id["codex_cli"].status == "ng"
    assert by_id["codex_version"].status == "ng"
    assert by_id["codex_auth"].status == "ng"
    for cid in ("codex_cli", "codex_version", "codex_auth"):
        assert by_id[cid].detail == doctor_checks._AGENT_RESOLUTION_FAILED_DETAIL


def test_run_all_codex_indeterminate_does_not_misattribute_to_ollama_check(monkeypatch):
    """`effective_agent()` が例外を投げる構成では、Codex 側の3項目が自身の固定文言 NG を
    報告する（Ollama の要否判定（`_resolve_ollama_usages`）が独自に NG を出す既存の経路だけに
    症状が現れて誤帰属することがない）。"""
    from sherpa import agent_constructs

    def _boom(settings, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_constructs, "effective_agent", _boom)
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: None)   # CLI 有無は本テストの関心外
    sys_s = {"cloud_provider": "openai"}
    rows = [{"agent": "openai", "codex_model_provider": None}]
    codex_required, codex_needs_openai_auth, codex_note = doctor_checks._codex_required(sys_s, rows)
    codex_indeterminate = doctor_checks._agent_resolution_indeterminate(sys_s, rows)
    assert codex_indeterminate is True
    results = doctor_checks.check_codex(sys_s, rows, codex_required, codex_needs_openai_auth,
                                        codex_note, probe_cloud=False, indeterminate=codex_indeterminate)
    by_id = {r.id: r for r in results}
    assert by_id["codex_cli"].status == "ng"
    assert by_id["codex_cli"].detail == doctor_checks._AGENT_RESOLUTION_FAILED_DETAIL
    assert by_id["codex_version"].status == "ng"
    assert by_id["codex_auth"].status == "ng"


def test_codex_cli_missing_and_not_required_is_skip(monkeypatch):
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: None)
    results = doctor_checks.check_codex(None, None, required=False, needs_openai_auth=False,
                                        note="", probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["codex_cli"].status == "skip"
    assert by_id["codex_version"].status == "skip"
    assert by_id["codex_auth"].status == "skip"


def test_codex_cli_missing_and_required_is_ng(monkeypatch):
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: None)
    results = doctor_checks.check_codex({"agent": "codex"}, [], required=True, needs_openai_auth=True,
                                        note="", probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["codex_cli"].status == "ng"


def test_codex_found_version_and_auth_ok_default_openai(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = "codex-cli 0.144.1\n"
        stderr = ""
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(health, "_ai_check_codex", lambda settings, sys_s: None)
    results = doctor_checks.check_codex({}, None, required=False, needs_openai_auth=True,
                                        note="", probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["codex_cli"].status == "ok"
    assert by_id["codex_version"].status == "ok"
    assert "0.144.1" in by_id["codex_version"].detail
    assert by_id["codex_auth"].status == "ok"


def test_codex_version_ng_on_nonzero_exit_when_required(monkeypatch):
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "unknown flag"
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(health, "_ai_check_codex", lambda settings, sys_s: None)
    results = doctor_checks.check_codex({"agent": "codex"}, [], required=True, needs_openai_auth=True,
                                        note="", probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["codex_version"].status == "ng"


def test_codex_version_skip_on_nonzero_exit_when_not_required(monkeypatch):
    """任意構成（required=False）の `--version` 失敗は失敗ではなく情報表示（SKIP）。"""
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "unknown flag"
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(health, "_ai_check_codex", lambda settings, sys_s: None)
    results = doctor_checks.check_codex(None, None, required=False, needs_openai_auth=False,
                                        note="", probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["codex_version"].status == "skip"


def test_codex_auth_skipped_when_only_ollama_backing(monkeypatch):
    """`needs_openai_auth=False` なら、
    OpenAI/Azure 側の判定部品（`_codex_openai_compat_block_reason`／`health._ai_check_codex`）を
    一切呼ばず `skip` にする（CLI 存在・バージョンは `required` に従い引き続き確認する）。"""
    class _Proc:
        returncode = 0
        stdout = "codex-cli 0.144.1\n"
        stderr = ""

    def _should_not_be_called(*a, **k):
        raise AssertionError("needs_openai_auth=False では OpenAI 側の判定を呼んではいけない")
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(health, "_ai_check_codex", _should_not_be_called)
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", _should_not_be_called)
    results = doctor_checks.check_codex({"agent": "codex", "codex_model_provider": "ollama"}, [],
                                        required=True, needs_openai_auth=False, note="", probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["codex_cli"].status == "ok"
    assert by_id["codex_version"].status == "ok"
    assert by_id["codex_auth"].status == "skip"


def test_codex_auth_default_openai_ng_when_required_and_login_fails(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = "codex-cli 0.144.1\n"
        stderr = ""

    def _boom(settings, sys_s):
        raise RuntimeError("未ログイン")
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(health, "_ai_check_codex", _boom)
    results = doctor_checks.check_codex({"agent": "codex"}, [], required=True, needs_openai_auth=True,
                                        note="", probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["codex_auth"].status == "ng"


def test_codex_auth_default_openai_skip_when_login_fails_but_not_required(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = "codex-cli 0.144.1\n"
        stderr = ""

    def _boom(settings, sys_s):
        raise RuntimeError("未ログイン")
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(health, "_ai_check_codex", _boom)
    results = doctor_checks.check_codex({}, None, required=False, needs_openai_auth=True,
                                        note="", probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert by_id["codex_auth"].status == "skip"


def test_codex_auth_masks_secret_in_exception_text(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = "codex-cli 0.144.1\n"
        stderr = ""

    def _boom(settings, sys_s):
        raise RuntimeError("Authorization: Bearer sk-should-not-leak-123456789012345678")
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(health, "_ai_check_codex", _boom)
    results = doctor_checks.check_codex({"agent": "codex"}, [], required=True, needs_openai_auth=True,
                                        note="", probe_cloud=False)
    by_id = {r.id: r for r in results}
    assert "sk-should-not-leak" not in by_id["codex_auth"].detail


def test_codex_auth_azure_backing_uses_compat_block_reason(monkeypatch):
    """接続先種別が openai 以外（Azure/custom）なら `_codex_openai_compat_block_reason` を使う
    （`codex login status` は auth.json 経由のため Azure/custom には的外れ）。"""
    def _should_not_be_called(*a, **k):
        raise AssertionError("Azure backing では codex login status を呼んではいけない")
    monkeypatch.setattr(health, "_ai_check_codex", _should_not_be_called)
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason",
                        lambda s, **k: "デプロイ名が未設定です")
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1",
              "openai_api_key": "sk-real-key"}
    r = doctor_checks._check_codex_auth(sys_s, None, required=True, note="", probe_cloud=False)
    assert r.status == "ng"
    assert "デプロイ名" in r.detail


def test_codex_auth_azure_backing_ok_when_not_blocked_and_probe_cloud_off(monkeypatch):
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", lambda s, **k: None)
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1",
              "openai_api_key": "sk-real-key"}
    r = doctor_checks._check_codex_auth(sys_s, None, required=True, note="", probe_cloud=False)
    assert r.status == "skip"   # 設定形式は妥当・実接続は PROBE_CLOUD ゲート待ち


def test_check_codex_azure_compat_unexpected_exception_becomes_ng_not_traceback(monkeypatch):
    """`_codex_openai_compat_block_reason`（内部で `model_catalog.resolve_model()` を呼ぶ）が
    壊れた設定で未知の例外（`TypeError` 等）を投げても、`_check_codex_azure_compat` の外へは
    伝播せず（`@_guarded_check` デコレータ・`run_all()` 全体を巻き込まない）、単独の NG に
    変換される。"""
    def _boom(s, **k):
        raise TypeError("model_catalog.openai.chat.allowed=1 のような壊れた設定")
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", _boom)
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1",
              "openai_api_key": "sk-real-key"}
    r = doctor_checks._check_codex_azure_compat(sys_s, None, required=True, probe_cloud=False)
    assert r.status == "ng"
    assert r.id == "codex_auth"


def test_codex_auth_azure_backing_real_probe_when_probe_cloud_on(monkeypatch):
    from sherpa.ingest import graph_extract
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", lambda s, **k: None)
    monkeypatch.setattr(graph_extract, "complete_json", lambda system, user, cfg, timeout=None: '{"ok":true}')
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1",
              "openai_api_key": "sk-real-key"}
    r = doctor_checks._check_codex_auth(sys_s, None, required=True, note="", probe_cloud=True)
    assert r.status == "ok"


def test_codex_auth_azure_backing_real_probe_passes_doctor_timeout(monkeypatch):
    """診断ツールの応答性のため、抽出用の既定タイムアウト（90s）ではなく doctor 専用の短い
    タイムアウト（`_CODEX_TIMEOUT`）を明示で渡す（接続先が無応答でも1項目で全体をブロックしない・
    `health._AI_TIMEOUT` を使う他の LLM プローブと同じ考え方）。"""
    from sherpa.ingest import graph_extract
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", lambda s, **k: None)
    seen = {}

    def _fake_complete_json(system, user, cfg, timeout=None):
        seen["timeout"] = timeout
        return '{"ok":true}'
    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1",
              "openai_api_key": "sk-real-key"}
    doctor_checks._check_codex_auth(sys_s, None, required=True, note="", probe_cloud=True)
    assert seen["timeout"] == doctor_checks._CODEX_TIMEOUT


@pytest.mark.parametrize("secret_in_message", [
    "sk-abcdefgh1234567890ABCDEFGHIJK",           # 分断なし
    "sk-ab\ncdefgh1234\t567890ABCDEFGHIJK",        # 制御文字（改行・タブ）で分断
    "sk-ab cdefgh1234-567890ABCDEFGHIJK",          # 空白・記号で分断
], ids=["intact", "control-char-split", "space-and-symbol-split"])
def test_codex_auth_azure_backing_real_probe_ng_detail_never_contains_free_text(monkeypatch, secret_in_message):
    """Codex(Azure/custom) 実プローブの失敗理由も、`check_cloud_llm_probes` と同じ fail-closed
    契約（自由文を一切出さない）に従う。`graph_extract.complete_json` を
    直接モックし、`_probe`／`_safe_detail` を経由しない実経路で確認する。"""
    from sherpa.ingest import graph_extract
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", lambda s, **k: None)
    secret = "sk-abcdefgh1234567890ABCDEFGHIJK"

    def _boom(system, user, cfg, timeout=None):
        assert cfg["key"] == secret
        raise RuntimeError(f"invalid key: {secret_in_message}")
    monkeypatch.setattr(graph_extract, "complete_json", _boom)
    monkeypatch.setattr("sherpa.keys.resolve_api_key", lambda provider, s, **k: secret)
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1",
              "openai_api_key": secret}
    r = doctor_checks._check_codex_auth(sys_s, None, required=True, note="", probe_cloud=True)
    assert r.status == "ng"
    assert r.detail == "接続に失敗しました: error（RuntimeError）"
    assert "sk-" not in r.detail
    assert secret not in r.detail
    assert secret[:5] not in r.detail
    assert secret not in r.detail


def test_codex_auth_azure_backing_blocked_is_skip_when_not_required(monkeypatch):
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", lambda s, **k: "キー未設定")
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1",
              "openai_api_key": "sk-real-key"}
    r = doctor_checks._check_codex_auth(sys_s, None, required=False, note="", probe_cloud=False)
    assert r.status == "skip"


def test_codex_auth_invalid_endpoint_kind_is_ng(monkeypatch):
    sys_s = {"openai_endpoint_kind": "bogus"}
    r = doctor_checks._check_codex_auth(sys_s, None, required=True, note="", probe_cloud=False)
    assert r.status == "ng"


def test_check_codex_skips_openai_auth_with_zero_sends_when_sys_s_none(monkeypatch):
    """`sys_s`（`run_all` の `llm_sys_s`）が `None`（接続先が未確定／不正・または system_settings
    読み取り不能）の間は、`codex_auth`（OpenAI/Azure 側の認証確認・Azure/custom 分岐は実送信も
    ありうる）を一切試みず SKIP にする（送信ゼロ）。CLI 存在・バージョン確認自体は `sys_s` と
    無関係なので影響しない。"""
    class _Proc:
        returncode = 0
        stdout = "codex-cli 0.144.1\n"
        stderr = ""
    from sherpa import providers as providers_mod
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(doctor_checks.subprocess, "run", lambda *a, **k: _Proc())

    def _should_not_be_called(*a, **k):
        raise AssertionError("接続先未確定のまま Codex の OpenAI/Azure 認証確認をしてはいけない")
    monkeypatch.setattr(providers_mod, "_codex_openai_compat_block_reason", _should_not_be_called)
    monkeypatch.setattr(doctor_checks, "_run_raw_llm_probe", _should_not_be_called)

    results = doctor_checks.check_codex(None, [], required=True, needs_openai_auth=True,
                                        note="", probe_cloud=True)
    by_id = {r.id: r for r in results}
    assert by_id["codex_cli"].status == "ok"
    assert by_id["codex_version"].status == "ok"
    assert by_id["codex_auth"].status == "skip"


def test_run_all_check_codex_receives_gated_sys_s_not_raw(monkeypatch):
    """`run_all()` は `check_codex()` へ生の `sys_s` ではなく `_openai_endpoint_status()` が
    確定した実効値（`llm_sys_s`）を渡す（`check_selected_provider_key`／`check_cloud_llm_probes`
    と同じゲート）。接続先が NG（`DB_ENDPOINT_INVALID`）の構成で、Codex(Azure/custom) の実送信
    （`_run_raw_llm_probe`）が一度も呼ばれないことを確認する。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "codex")
    monkeypatch.setattr(agent_constructs, "codex_model_provider", lambda *a, **k: "openai")

    def _should_not_be_called(*a, **k):
        raise AssertionError("接続先が不正な構成で Codex の実送信をしてはいけない")
    monkeypatch.setattr(doctor_checks, "_run_raw_llm_probe", _should_not_be_called)
    monkeypatch.setattr(doctor_checks.shutil, "which", lambda name: None)   # CLI 有無は本テストの関心外

    sys_s = {"cloud_provider": "openai", "openai_endpoint_kind": "bogus",
             "openai_endpoint_seed_version": 1, "openai_api_key": "sk-real-key-1234567890"}
    r = doctor_checks._openai_endpoint_status(sys_s)
    assert r["status"] == "ng"
    results = doctor_checks.check_codex(r["effective_sys_s"], [], required=True,
                                        needs_openai_auth=True, note="", probe_cloud=True)
    by_id = {r2.id: r2 for r2 in results}
    assert by_id["codex_auth"].status == "skip"


def test_codex_auth_azure_backing_skip_with_personal_key_when_central_key_missing(monkeypatch):
    """A6: 中央キーさえあれば通る構成（＝キー実在チェックだけを強制的に通した2回目の呼び出しが
    `None` を返す）で、かつ有効な利用者の誰かが個人キーを保存済みなら SKIP に読み替える（doctor は
    個人キーの値を確認できないため実際に動くかどうかは doctor 側からは判定できない）。"""
    from sherpa import keys
    no_central_key_reason = f"{keys.NO_CENTRAL_KEY_MESSAGE}（Azure 等の接続先の認証にも使います）"

    def _block_reason(s, *, explicit_openai_api_key=None, **k):
        if explicit_openai_api_key:
            return None   # キーさえあれば他の不備は無い
        return no_central_key_reason
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", _block_reason)
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1",
              "personal_api_keys_allowed": True}
    rows = [{"agent": "codex", "codex_model_provider": "openai", "search_helper": "",
            "has_openai_key": True, "has_gemini_key": False, "has_bedrock_key": False}]
    r = doctor_checks._check_codex_auth(sys_s, rows, required=True, note="", probe_cloud=True)
    assert r.status == "skip"
    assert "個人キー" in r.detail


def test_codex_auth_azure_backing_deployment_name_missing_not_bypassed_by_personal_keys(monkeypatch):
    """中央キーが無い**うえに**デプロイ名も未設定（カタログ既定 gpt-5.5 のまま）の構成では、
    `_codex_openai_compat_block_reason` は早い者勝ちで「キー未設定」しか返さない。個人キー救済を
    この理由だけで判定すると、実際にはキーがあっても解決しないデプロイ名未設定を見逃して誤って
    SKIP にしてしまう。キー実在を強制した再呼び出しでデプロイ名の不備が残ることを検出し、NG の
    まま出す。"""
    deployment_reason = "管理画面の「使えるモデル」で Codex に接続先（Azure 等）のデプロイ名を登録してください"

    def _block_reason(s, *, explicit_openai_api_key=None, **k):
        from sherpa import keys
        if not explicit_openai_api_key:
            return f"{keys.NO_CENTRAL_KEY_MESSAGE}（Azure 等の接続先の認証にも使います）"
        return deployment_reason   # キーがあってもデプロイ名未設定は残る
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason", _block_reason)
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1",
              "personal_api_keys_allowed": True}
    rows = [{"agent": "codex", "codex_model_provider": "openai", "search_helper": "",
            "has_openai_key": True, "has_gemini_key": False, "has_bedrock_key": False}]
    r = doctor_checks._check_codex_auth(sys_s, rows, required=True, note="", probe_cloud=True)
    assert r.status == "ng"
    assert "デプロイ名" in r.detail
    assert "個人キー" not in r.detail


def test_codex_auth_azure_backing_calls_block_reason_before_skip_decision(monkeypatch):
    """SKIP 判定は `_codex_openai_compat_block_reason` を呼んだ**後**にしか行わない
    （呼ぶ前に個人キー保有だけで即 SKIP へ倒すと、サンドボックス無効・URL不正・デプロイ名未設定
    といったキーとは無関係な静的検査を丸ごと迂回してしまう）。"""
    called = []
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason",
                        lambda s, **k: called.append(1) or None)
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1",
              "personal_api_keys_allowed": True, "openai_api_key": "sk-real-key"}
    rows = [{"agent": "codex", "codex_model_provider": "openai", "search_helper": "",
            "has_openai_key": True, "has_gemini_key": False, "has_bedrock_key": False}]
    doctor_checks._check_codex_auth(sys_s, rows, required=True, note="", probe_cloud=False)
    assert called == [1]


def test_codex_auth_azure_backing_sandbox_disabled_not_bypassed_by_personal_keys(monkeypatch):
    """個人キー運用中でも、サンドボックス無効等のキーと無関係な設定不備は NG のまま出る
    （「キー未設定」以外の理由は個人キー救済の対象外）。"""
    monkeypatch.setattr("sherpa.providers._codex_openai_compat_block_reason",
                        lambda s, **k: "Azure OpenAI 等の接続先は Codex サンドボックス有効時のみ対応です")
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": "https://x.openai.azure.com/openai/v1",
              "personal_api_keys_allowed": True}
    rows = [{"agent": "codex", "codex_model_provider": "openai", "search_helper": "",
            "has_openai_key": True, "has_gemini_key": False, "has_bedrock_key": False}]
    r = doctor_checks._check_codex_auth(sys_s, rows, required=True, note="", probe_cloud=True)
    assert r.status == "ng"
    assert "サンドボックス" in r.detail


# ---------------------------------------------------------------------------
# 不要なクラウド消費判定（`_cloud_provider_consumed` 単体）
# ---------------------------------------------------------------------------

def test_cloud_provider_consumed_true_for_system_default(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    assert doctor_checks._cloud_provider_consumed("openai", {}, []) is True


def test_cloud_provider_consumed_false_when_only_ollama(monkeypatch):
    from sherpa import agent_constructs, llm
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    monkeypatch.setattr(agent_constructs, "codex_model_provider", lambda *a, **k: "ollama")
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda *a, **k: "openai")
    rows = [{"agent": "ollama", "codex_model_provider": None, "search_helper": ""},
            {"agent": "codex", "codex_model_provider": "ollama", "search_helper": ""}]
    assert doctor_checks._cloud_provider_consumed("openai", {}, rows) is False


def test_cloud_provider_consumed_fail_closed_when_rows_none(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    assert doctor_checks._cloud_provider_consumed("openai", {}, None) is True


def test_cloud_provider_consumed_ignores_stale_codex_model_provider_when_not_codex(monkeypatch):
    """実効頭脳が codex でなければ、`codex_model_provider` の残存値だけで
    「openai を消費している」扱いにしない。"""
    from sherpa import agent_constructs, llm
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    monkeypatch.setattr(agent_constructs, "codex_model_provider", lambda *a, **k: "openai")
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda *a, **k: "azure")
    assert doctor_checks._cloud_provider_consumed("openai", {}, []) is False


def test_cloud_provider_consumed_true_when_actually_codex_with_azure_backing(monkeypatch):
    from sherpa import agent_constructs, llm
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "codex")
    monkeypatch.setattr(agent_constructs, "codex_model_provider", lambda *a, **k: "openai")
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda *a, **k: "azure")
    assert doctor_checks._cloud_provider_consumed("openai", {}, []) is True


def test_cloud_provider_consumed_false_when_selected_agent_is_runtime_blocked(monkeypatch):
    """`effective_agent()` が gemini/bedrock を返しても、`runtime_blocked()` が真
    （`SHERPA_EXTRA_AGENTS` 未設定で現在の環境では無効）なら実行時は `_DisabledProvider` に
    差し替わりキーは一切参照されない。「保存されているが無効」は「selected を消費している」と
    区別する（理由は `_disabled_agent_configs` が別途 NG 報告する）。"""
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "gemini")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: True)
    assert doctor_checks._cloud_provider_consumed("gemini", {}, []) is False


def test_cloud_provider_consumed_true_when_selected_agent_is_runtime_enabled(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "gemini")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: False)
    assert doctor_checks._cloud_provider_consumed("gemini", {}, []) is True


def test_cloud_provider_consumed_fail_closed_when_default_effective_agent_raises(monkeypatch):
    """システム既定（`rows=[]`＝有効な利用者なし）の `effective_agent()` が例外を投げても、
    黙って「一致しない（＝未消費）」に丸めず fail-closed（消費している扱い）にする
    （判定不能を「使っていない」に丸めると、設定解決が壊れているだけの環境を doctor が
    誤ってスキップしてしまう）。"""
    from sherpa import agent_constructs

    def _boom(settings, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_constructs, "effective_agent", _boom)
    assert doctor_checks._cloud_provider_consumed("openai", {}, []) is True


def test_cloud_provider_consumed_fail_closed_when_per_row_effective_agent_raises(monkeypatch):
    """システム既定は判定できても（openai ではない）、有効な利用者の1人の `effective_agent()` が
    例外を投げれば、その行を黙って「使っていない」に丸めず fail-closed にする。"""
    from sherpa import agent_constructs

    def _maybe_boom(settings, **k):
        if settings is None:
            return "ollama"
        raise RuntimeError("boom")
    monkeypatch.setattr(agent_constructs, "effective_agent", _maybe_boom)
    rows = [{"agent": "openai", "codex_model_provider": None}]
    assert doctor_checks._cloud_provider_consumed("openai", {}, rows) is True


# ---------------------------------------------------------------------------
# 実際に消費されている用途一覧（`_consumed_llm_purposes`）
# ---------------------------------------------------------------------------

def test_consumed_llm_purposes_empty_when_nothing_used(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    assert doctor_checks._consumed_llm_purposes("openai", {}, []) == []


def test_consumed_llm_purposes_chat_only_when_agent_matches_but_no_key(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    assert doctor_checks._consumed_llm_purposes("openai", {}, []) == ["chat"]


def test_consumed_llm_purposes_second_path_only_when_key_present_but_chat_uses_ollama(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)   # 他テストモジュールの import 時設定から隔離
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-real-key-1234567890"}
    assert doctor_checks._consumed_llm_purposes("openai", sys_s, []) == ["intent", "render", "embed"]


def test_consumed_llm_purposes_chat_and_second_path_combined(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)   # 他テストモジュールの import 時設定から隔離
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    sys_s = {"cloud_provider": "openai", "openai_api_key": "sk-real-key-1234567890"}
    assert doctor_checks._consumed_llm_purposes("openai", sys_s, []) == ["chat", "intent", "render", "embed"]


def test_consumed_llm_purposes_second_path_excludes_bedrock(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    sys_s = {"cloud_provider": "bedrock", "bedrock_api_key": "sk-real-key-1234567890"}
    assert doctor_checks._consumed_llm_purposes("bedrock", sys_s, []) == []


def test_consumed_llm_purposes_second_path_truthy_not_is_real_api_key(monkeypatch):
    """truthy だが `is_real_api_key()` は満たさない値（プレースホルダ・空白）でも、本番の
    `resolve_auto_provider()` と同じ truthy 判定で「消費されている」扱いにする（認証有効性の
    判定は呼び出し元＝`check_selected_provider_key`／`check_cloud_llm_probes` の責務）。"""
    from sherpa import agent_constructs
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)   # 他テストモジュールの import 時設定から隔離
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "ollama")
    for placeholder in ("sk-REPLACE_ME", "   "):
        sys_s = {"cloud_provider": "gemini", "gemini_api_key": placeholder}
        assert doctor_checks._consumed_llm_purposes("gemini", sys_s, []) == ["intent", "render", "embed"]


# ---------------------------------------------------------------------------
# 無効化された頭脳構成（gemini/bedrock が SHERPA_EXTRA_AGENTS 外）
# ---------------------------------------------------------------------------

def test_disabled_agent_configs_skip_when_settings_unavailable():
    r = doctor_checks._disabled_agent_configs(None, None)
    assert r.status == "skip"


def test_disabled_agent_configs_ok_when_none_blocked(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent", lambda *a, **k: "openai")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: False)
    r = doctor_checks._disabled_agent_configs({}, [{"agent": "openai", "codex_model_provider": None}])
    assert r.status == "ok"


def test_disabled_agent_configs_ng_counts_system_default_and_active_users(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent",
                         lambda settings, **k: (settings or {}).get("agent") or "gemini")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: agent in ("gemini", "bedrock"))
    rows = [{"agent": "gemini", "codex_model_provider": None},
            {"agent": "bedrock", "codex_model_provider": None},
            {"agent": "openai", "codex_model_provider": None}]
    r = doctor_checks._disabled_agent_configs({}, rows)
    assert r.status == "ng"
    assert "3" in r.detail   # システム既定(gemini・フォールバック) + 有効利用者2件


def test_disabled_agent_configs_does_not_expose_user_ids(monkeypatch):
    from sherpa import agent_constructs
    monkeypatch.setattr(agent_constructs, "effective_agent",
                         lambda settings, **k: (settings or {}).get("agent") or "openai")
    monkeypatch.setattr(agent_constructs, "runtime_blocked", lambda agent: agent == "gemini")
    rows = [{"agent": "gemini", "codex_model_provider": None, "user_id": "user-should-not-leak"}]
    r = doctor_checks._disabled_agent_configs({}, rows)
    assert "user-should-not-leak" not in r.detail


# ---------------------------------------------------------------------------
# PROBE_CLOUD env ゲート・レポート整形・終了コード
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("0", False), ("no", False),
    ("1", True), ("true", True), ("YES", True), ("on", True),
])
def test_probe_cloud_enabled_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PROBE_CLOUD", raising=False)
    else:
        monkeypatch.setenv("PROBE_CLOUD", value)
    assert doctor_checks.probe_cloud_enabled() is expected


def test_format_report_counts_by_status():
    results = [
        doctor_checks.CheckResult("a", "A", "ok", "d"),
        doctor_checks.CheckResult("b", "B", "ng", "d"),
        doctor_checks.CheckResult("c", "C", "skip", "d"),
    ]
    out = doctor_checks.format_report(results)
    assert "OK=1 NG=1 SKIP=1" in out


def test_main_exit_code_nonzero_on_ng(monkeypatch):
    monkeypatch.setattr(doctor_checks, "run_all",
                         lambda **k: [doctor_checks.CheckResult("a", "A", "ng", "d")])
    assert doctor_checks.main([]) == 1


def test_main_exit_code_zero_when_all_ok_or_skip(monkeypatch):
    monkeypatch.setattr(doctor_checks, "run_all",
                         lambda **k: [doctor_checks.CheckResult("a", "A", "ok", "d"),
                                      doctor_checks.CheckResult("b", "B", "skip", "d")])
    assert doctor_checks.main([]) == 0
