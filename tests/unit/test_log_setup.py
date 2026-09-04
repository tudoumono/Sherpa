"""sherpa/log_setup.py（LOG-2・サブシステム別ログ分離・2026-09-03）の受け入れテスト。

対象: `configure_logging()` が LibreOffice 変換／MD 変換（取り込み）／LLM 埋め込みの3系統へ専用
ファイルハンドラを付けること、WARNING 以上は run ログ（"sherpa" ロガー）にも残ること、INFO 以下は
専用ファイルのみに書かれること、起動時退避（`rotate_and_prune`）が保持数超過分だけを古い順に
削除すること。`configure_logging()` は既定で pytest 実行中（`SHERPA_TEST_DB_ISOLATED`）は
実ファイル操作をしない——本ファイルのテストは `force=True` で明示的にその既定を迂回する。
"""
from __future__ import annotations

import io
import logging
import time

import pytest

from sherpa import log_setup


@pytest.fixture(autouse=True)
def _isolated_log_state():
    """各テストの前後でこのモジュールが付けた handler・`_configured` ガードを完全に外す
    （pytest プロセス内でロガーはシングルトンのため、テスト間の handler 蓄積を防ぐ）。"""
    log_setup._reset_state_for_tests()
    yield
    log_setup._reset_state_for_tests()


def _own_handlers(logger_name: str) -> list[logging.Handler]:
    return [h for h in logging.getLogger(logger_name).handlers if getattr(h, log_setup._HANDLER_MARK, False)]


# ---- configure_logging(): pytest 既定ガード ----

def test_configure_logging_is_noop_during_pytest_by_default(tmp_path, monkeypatch):
    """SHERPA_TEST_DB_ISOLATED が立つ pytest 実行中は、force なしの呼び出しで実ファイル/handler を作らない
    （テスト実行のたびに data/run/*.log を散らかさない・実行中サーバのログを誤って退避しないため）。"""
    monkeypatch.setenv("SHERPA_TEST_DB_ISOLATED", "1")
    log_setup.configure_logging(log_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []
    for logger_name, _ in log_setup._SUBSYSTEM_LOGGERS.values():
        assert _own_handlers(logger_name) == []
    assert _own_handlers("sherpa") == []


# ---- configure_logging(force=True): 実際の配線 ----

def test_configure_logging_creates_one_file_per_subsystem(tmp_path):
    log_setup.configure_logging(force=True, log_dir=tmp_path)
    for _name, (_logger_name, filename) in log_setup._SUBSYSTEM_LOGGERS.items():
        assert (tmp_path / filename).exists()


def test_usage_subsystem_is_registered(tmp_path):
    """LOG-UX（2026-09-04）: `sherpa.usage` ロガー（`metering.record()` が使う）が usage.log へ配線される
    （registry に1行足すだけで他の受け入れテスト群が自動的に検証する契約・本テストは名前を明示で固定）。"""
    assert log_setup._SUBSYSTEM_LOGGERS["usage"] == ("sherpa.usage", "usage.log")
    log_setup.configure_logging(force=True, log_dir=tmp_path)
    assert (tmp_path / "usage.log").exists()
    logging.getLogger("sherpa.usage").info("kind=embed provider=openai model=m in=1 cached=0 out=0 calls=1")
    for h in logging.getLogger("sherpa.usage").handlers:
        h.flush()
    assert "kind=embed" in (tmp_path / "usage.log").read_text(encoding="utf-8")


def test_info_goes_to_subsystem_file_only_warning_also_reaches_run_logger(tmp_path, monkeypatch):
    """INFO は専用ファイルのみ／WARNING 以上は専用ファイル＋run ログ（"sherpa" ロガー）の両方に残る
    （専用ファイルを見ないと障害に気づけない、という新しい無音を作らない、の実測）。"""
    fake_stderr = io.StringIO()
    monkeypatch.setattr(log_setup.sys, "stderr", fake_stderr)
    log_setup.configure_logging(force=True, log_dir=tmp_path)

    embed_logger_name, embed_filename = log_setup._SUBSYSTEM_LOGGERS["embed"]
    logger = logging.getLogger(embed_logger_name)
    logger.info("詳細情報（run ログには出ない想定）")
    logger.warning("障害の疑いあり（run ログにも出る想定）")
    for h in logger.handlers:
        h.flush()
    for h in logging.getLogger("sherpa").handlers:
        h.flush()

    file_content = (tmp_path / embed_filename).read_text(encoding="utf-8")
    assert "詳細情報" in file_content
    assert "障害の疑いあり" in file_content

    run_output = fake_stderr.getvalue()
    assert "障害の疑いあり" in run_output
    assert "詳細情報" not in run_output


def test_run_logger_handler_level_is_warning(tmp_path):
    log_setup.configure_logging(force=True, log_dir=tmp_path)
    handlers = _own_handlers("sherpa")
    assert len(handlers) == 1
    assert handlers[0].level == logging.WARNING


def test_subsystem_loggers_are_children_of_sherpa_namespace(tmp_path):
    """"sherpa" 配下の子ロガーであること＝ WARNING+ が自然に run ログへ伝播する前提（propagate 既定 True）。"""
    log_setup.configure_logging(force=True, log_dir=tmp_path)
    for _name, (logger_name, _filename) in log_setup._SUBSYSTEM_LOGGERS.items():
        assert logger_name == "sherpa" or logger_name.startswith("sherpa.")
        assert logging.getLogger(logger_name).propagate is True


def test_double_registration_guard_does_not_accumulate_handlers(tmp_path):
    """同一プロセスで configure_logging(force=True) を2回呼んでも handler は各1個のまま
    （二重登録ガード）。2回目は新しい log_dir を向く（テスト間の実際の切替を兼ねて検証）。"""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    log_setup.configure_logging(force=True, log_dir=dir_a)
    log_setup.configure_logging(force=True, log_dir=dir_b)
    for _name, (logger_name, filename) in log_setup._SUBSYSTEM_LOGGERS.items():
        assert len(_own_handlers(logger_name)) == 1
        assert (dir_b / filename).exists()
    assert len(_own_handlers("sherpa")) == 1


def test_process_guard_default_call_configures_only_once(tmp_path, monkeypatch):
    """force なしの呼び出しは `_configured` が立った後は何もしない（プロセスにつき1回だけ効く）。"""
    monkeypatch.delenv("SHERPA_TEST_DB_ISOLATED", raising=False)
    log_setup.configure_logging(log_dir=tmp_path)
    assert list(tmp_path.iterdir()) != []
    dir_b = tmp_path / "b"
    log_setup.configure_logging(log_dir=dir_b)   # 既に _configured=True なので無視される
    assert not dir_b.exists()


# ---- rotate_and_prune(): 起動時退避 ----

def test_rotate_and_prune_archives_nonempty_and_leaves_empty_alone(tmp_path):
    path = tmp_path / "embed.log"
    path.write_text("前回の内容\n", encoding="utf-8")
    log_setup.rotate_and_prune(path, keep=10)
    assert path.read_text(encoding="utf-8") == ""
    archives = [p for p in tmp_path.iterdir() if p.name != "embed.log"]
    assert len(archives) == 1
    assert archives[0].read_text(encoding="utf-8") == "前回の内容\n"

    # 空/不在のログは退避しない。
    path2 = tmp_path / "convert.log"
    log_setup.rotate_and_prune(path2, keep=10)
    assert path2.exists() and path2.read_text(encoding="utf-8") == ""
    assert [p for p in tmp_path.iterdir() if p.name not in ("embed.log", "convert.log")] == archives


def test_rotate_and_prune_keeps_only_newest_n_and_ignores_unrelated_files(tmp_path):
    path = tmp_path / "libreoffice.log"
    unrelated = tmp_path / "libreoffice-notes.log"
    unrelated.write_text("触らないで", encoding="utf-8")

    for i in range(4):
        path.write_text(f"run {i}\n", encoding="utf-8")
        log_setup.rotate_and_prune(path, keep=2)
        time.sleep(1.1)   # タイムスタンプ（秒精度）衝突回避

    archives = sorted(p for p in tmp_path.iterdir() if p.name not in ("libreoffice.log", "libreoffice-notes.log"))
    assert len(archives) == 2
    contents = sorted(p.read_text(encoding="utf-8") for p in archives)
    assert contents == ["run 2\n", "run 3\n"]
    assert unrelated.read_text(encoding="utf-8") == "触らないで"


# ===== access ログの監視ノイズ除去（実利用フィードバック 2026-09-03） =====

def _access_record(path, status, args_override=None):
    import logging
    r = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                          '%s - "%s %s HTTP/%s" %d', args_override if args_override is not None
                          else ("127.0.0.1:1", "GET", path, "1.1", status), None)
    return r


def test_access_filter_drops_healthz_success():
    from sherpa.log_setup import _AccessLogNoiseFilter
    f = _AccessLogNoiseFilter()
    assert f.filter(_access_record("/healthz", 200)) is False
    assert f.filter(_access_record("/notifications", 200)) is False
    assert f.filter(_access_record("/healthz?x=1", 200)) is False


def test_access_filter_keeps_errors_and_other_paths():
    from sherpa.log_setup import _AccessLogNoiseFilter
    f = _AccessLogNoiseFilter()
    assert f.filter(_access_record("/healthz", 503)) is True     # 監視パスでもエラーは残す
    assert f.filter(_access_record("/chat/turns", 200)) is True
    assert f.filter(_access_record("/worlds/w/refresh", 200)) is True


def test_access_filter_fail_open_on_unexpected_args():
    from sherpa.log_setup import _AccessLogNoiseFilter
    f = _AccessLogNoiseFilter()
    assert f.filter(_access_record("/healthz", 200, args_override=("only",))) is True


def test_configure_attaches_access_filter_once(tmp_path):
    import logging
    from sherpa import log_setup
    log_setup.configure_logging(force=True, log_dir=tmp_path)
    log_setup.configure_logging(force=True, log_dir=tmp_path)
    flt = [f for f in logging.getLogger("uvicorn.access").filters
           if isinstance(f, log_setup._AccessLogNoiseFilter)]
    assert len(flt) == 1


def test_configure_logging_adds_timestamps_to_uvicorn_handlers(tmp_path):
    """uvicorn アクセス行に時刻が無い（実利用フィードバック 2026-09-04）: configure_logging が
    uvicorn/uvicorn.access/uvicorn.error の既存ハンドラへ時刻付きフォーマッタを差し替える。"""
    import logging

    from sherpa import log_setup
    lg = logging.getLogger("uvicorn.access")
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))   # uvicorn 既定相当（時刻なし）
    lg.addHandler(h)
    try:
        log_setup.configure_logging(force=True, log_dir=tmp_path)
        assert "asctime" in (h.formatter._fmt or "")
    finally:
        lg.removeHandler(h)
        log_setup._reset_state_for_tests()
