"""`sherpa.ext_api` の request_id ログ束縛（`_RequestIdLogFilter`）の単体テスト。実 DB には
触れない（`sherpa.lifespan.lifespan()` の起動処理はここで monkeypatch により無効化する）。

`logging.setLogRecordFactory()`（プロセス全体・永続的な副作用を持ち、再import/reload で多重化
しうる・`extra={"request_id": ...}` と衝突しうる）から `logging.Filter` を handler 側へ付与する
方式へ置き換えた。ここでは handler 側付与の要点——logger 名を問わず子ロガー
（`logging.getLogger(__name__)` で作るもの）にも効くこと・既存の `record.request_id` 属性を
上書きしないこと・複数回の付与でも filter が重複登録されないこと——を固定する。

`_attach_request_id_filter()` は import 時に一度走るが、ASGI サーバーが root へ自身の handler を
import より後に追加する場合は取りこぼす（`sherpa.lifespan.lifespan()` の起動処理で再度呼ぶことで
拾う・実装側の契約）。ここでは import 時点より後に追加した handler を、テストが直接
`_attach_request_id_filter()` を呼び直すのではなく、実際に `lifespan()` を駆動して拾わせる
（起動処理本体は `tests/api/test_lifespan.py` と同じ要領で no-op に差し替え、DB/外部 I/O には
触れない）——さもないと「lifespan 側の再適用が抜けても気付けない」テストになってしまう。
"""
from __future__ import annotations

import asyncio
import io
import logging
import threading

import pytest

from sherpa import api, ext_api, store
from sherpa.ingest import background
from sherpa.lifespan import lifespan as _lifespan_cm


def _drive_lifespan_startup_and_shutdown(monkeypatch) -> None:
    """`sherpa.lifespan.lifespan()` を実際に1周（起動〜shutdown）駆動する。request_id filter の
    再適用（検証対象）以外の起動処理は全て no-op に差し替え、実 DB/外部 I/O には触れない
    （差し替え対象は `tests/api/test_lifespan.py::test_lifespan_runs_startup_steps_in_order` と同じ
    起動ステップ一覧）。

    shutdown 側は `sherpa.ingest.background.stop_accepting()`（ING-3）を実際に呼ぶ——
    `_accepting` はプロセス寿命のモジュールグローバルのため、`monkeypatch` で明示的に元へ戻す
    （でなければ本ファイルの後に実行される他の全テストが `ShuttingDownError` で壊れる）。
    """
    monkeypatch.setattr(background, "_accepting", True)
    monkeypatch.setattr(store, "init_schema", lambda: None)
    for name in (
        "_seed_settings_from_env", "_purge_personal_keys_if_disabled_on_startup",
        "_warn_change_me_placeholders", "_warn_default_admin_password", "_auth_bootstrap_on_startup", "_warn_fixtures",
        "_warn_test_db_isolated", "_warn_codex_sandbox_disabled", "_warn_multi_worker_chat_turns",
        "_warn_browse_roots_missing", "_start_poller", "_reconcile_orphans",
        "_sweep_expired_on_startup",
    ):
        monkeypatch.setattr(api, name, lambda: None)

    async def _drive():
        cm = _lifespan_cm(None)
        await cm.__aenter__()
        await cm.__aexit__(None, None, None)

    asyncio.run(_drive())


@pytest.fixture
def _temp_root_handler(monkeypatch):
    """root logger へ一時的な StreamHandler を、モジュール import 時点より**後**に付ける
    （＝ASGI サーバーが root へ handler を足すのが import より後になる状況を模す）。
    filter を届けるのは実際に駆動した `lifespan()` の起動処理そのもの（テスト後は確実に外す）。"""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(request_id)s|%(name)s|%(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        _drive_lifespan_startup_and_shutdown(monkeypatch)
        yield buf
    finally:
        root.removeHandler(handler)


def test_filter_applies_to_child_logger_via_root_handler(_temp_root_handler):
    """`logging.getLogger(__name__)` 系の子ロガー（例: `sherpa.ingest.office_md`）が出す
    レコードにも request_id が乗る（handler 側付与＝logger 名を問わない）。"""
    child = logging.getLogger("sherpa.ingest.office_md")
    child.setLevel(logging.INFO)
    tok = ext_api._request_id_ctx.set("probe-child-logger")
    try:
        child.info("hello from child logger")
    finally:
        ext_api._request_id_ctx.reset(tok)

    out = _temp_root_handler.getvalue()
    assert "probe-child-logger|sherpa.ingest.office_md|hello from child logger" in out


def test_filter_does_not_overwrite_explicit_extra_request_id(_temp_root_handler):
    """呼び出し側が `extra={"request_id": ...}` を明示した場合、filter はそれを上書きしない
    （`setLogRecordFactory` 方式で起きていた属性衝突/KeyError の心配が無いことの確認）。"""
    logger = logging.getLogger("sherpa.some.other.module")
    logger.setLevel(logging.INFO)
    tok = ext_api._request_id_ctx.set("ambient-request-id")
    try:
        logger.info("explicit wins", extra={"request_id": "explicit-request-id"})
    finally:
        ext_api._request_id_ctx.reset(tok)

    out = _temp_root_handler.getvalue()
    assert "explicit-request-id|sherpa.some.other.module|explicit wins" in out
    assert "ambient-request-id" not in out


def test_filter_falls_back_to_dash_outside_request_context(_temp_root_handler):
    """ContextVar 未設定（ext_api の処理外）では "-" になる（フォーマッタが %(request_id)s を
    参照しても KeyError にならないよう常に属性を持たせる）。"""
    assert ext_api._request_id_ctx.get() is None
    logger = logging.getLogger("sherpa.no.request.context")
    logger.setLevel(logging.INFO)
    logger.info("outside request")

    out = _temp_root_handler.getvalue()
    assert "-|sherpa.no.request.context|outside request" in out


def test_attach_request_id_filter_is_idempotent():
    """同じ filter インスタンスを同じ target（handler）へ複数回付与しても重複登録されない
    （プロセス起動時に一度呼ばれる `_attach_request_id_filter()` が、モジュール再 import 等で
    再度呼ばれても filter が多重化しないことの固定）。付与対象は handler 単位（logger 自身へは
    付与しない・`_RequestIdLogFilter` の docstring 参照）なので `logging.lastResort` で見る。"""
    ext_api._attach_request_id_filter()
    ext_api._attach_request_id_filter()
    ext_api._attach_request_id_filter()
    assert logging.lastResort.filters.count(ext_api._request_id_log_filter) == 1


class _FakeLoggingModule:
    """`logging` モジュールの一部属性だけ差し替える薄い proxy。指定した属性は隠す
    （`AttributeError`）／上書きし、それ以外は実物の `logging` モジュールへ委譲する。

    `ext_api._logging_module_lock()` が参照する名前は `ext_api` モジュール自身の名前空間に
    束縛された `logging`（`import logging` で作られる参照）であり、`sys.modules["logging"]`
    そのものとは別物——`monkeypatch.setattr(ext_api, "logging", fake)` は `ext_api` から見える
    参照だけを差し替え、stdlib 側（`logging.Handler.close()`/`removeHandler()`・pytest 自身の
    ロギング処理等、モジュール内部から直接 `_acquireLock()` を呼ぶコード）が使う実物の
    `logging` モジュールには一切触れない。実際に `monkeypatch.delattr(logging, "_acquireLock")`
    のように実モジュールから属性を削除すると、stdlib 内部の別の場所が同じ名前を直接参照して
    いるため `NameError` でプロセス全体のロギングが壊れる（実測して確認済み・この proxy 方式で
    回避する）。
    """

    def __init__(self, *, hide=(), overrides=None):
        self._hide = set(hide)
        self._overrides = overrides or {}

    def __getattr__(self, name):
        if name in self._hide:
            raise AttributeError(name)
        if name in self._overrides:
            return self._overrides[name]
        return getattr(logging, name)


def test_logging_module_lock_uses_acquire_release_functions_when_present(monkeypatch):
    """`logging._acquireLock`/`_releaseLock`（Python 3.12 以前）が存在する場合はそれらを
    呼ぶ（`with` に入る前に acquire・出た後に release）。"""
    calls: list[str] = []
    fake = _FakeLoggingModule(overrides={
        "_acquireLock": lambda: calls.append("acquire"),
        "_releaseLock": lambda: calls.append("release"),
    })
    monkeypatch.setattr(ext_api, "logging", fake)
    with ext_api._logging_module_lock():
        assert calls == ["acquire"]
    assert calls == ["acquire", "release"]


def test_logging_module_lock_falls_back_to_lock_object_when_functions_absent(monkeypatch):
    """`logging._acquireLock`/`_releaseLock` が無い（Python 3.13+ で撤去された想定）場合は
    `logging._lock`（RLock 本体）を直接 `with` で使う——import 時点でこの分岐判定自体が
    例外にならないことも併せて確認する（`getattr(..., None)` による feature-detect であり、
    属性アクセスで即座に落ちない）。"""
    entered: list[str] = []

    class _FakeLock:
        def __enter__(self):
            entered.append("enter")
            return self

        def __exit__(self, *exc):
            entered.append("exit")
            return False

    fake = _FakeLoggingModule(hide={"_acquireLock", "_releaseLock"},
                              overrides={"_lock": _FakeLock()})
    monkeypatch.setattr(ext_api, "logging", fake)
    assert not hasattr(fake, "_acquireLock")
    assert not hasattr(fake, "_releaseLock")

    with ext_api._logging_module_lock():
        assert entered == ["enter"]
    assert entered == ["enter", "exit"]


@pytest.fixture
def _temp_named_handler_no_propagate(monkeypatch):
    """`"sherpa."` 配下の named logger に、root へは伝播しない（`propagate=False`）専用 handler を
    付ける——モジュール import 時点より後（＝lifespan 起動処理より前）に作られた状況を模す。
    filter を届けるのは実際に駆動した `lifespan()` の起動処理そのもの（テスト後は確実に外す）。"""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(request_id)s|%(name)s|%(message)s"))
    logger = logging.getLogger("sherpa.dedicated.worker")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        _drive_lifespan_startup_and_shutdown(monkeypatch)
        yield buf, logger
    finally:
        logger.removeHandler(handler)
        logger.propagate = True   # 他テストへ影響しないよう既定へ戻す


def test_filter_reaches_named_logger_with_own_handler_and_no_propagate(_temp_named_handler_no_propagate):
    """`propagate=False` の named logger（root へ伝播しない・独自 handler を持つ）にも、
    その logger 自身の handler へ filter が付与されて request_id が乗る（root の handlers だけ
    でなく `"sherpa"` 自身と配下の作成済み logger の handlers も列挙する）。"""
    buf, logger = _temp_named_handler_no_propagate
    tok = ext_api._request_id_ctx.set("probe-no-propagate")
    try:
        logger.info("hello from dedicated worker logger")
    finally:
        ext_api._request_id_ctx.reset(tok)

    out = buf.getvalue()
    assert "probe-no-propagate|sherpa.dedicated.worker|hello from dedicated worker logger" in out


def _snapshot_logger_dict_entries(names):
    """`logging.Logger.manager.loggerDict` の指定キー群の現在値をスナップショットする
    （`_logging_module_lock()` 保持下で呼ぶこと）。`PlaceHolder` は参照だけ保持しても内部
    `loggerMap` が以後の `getLogger()` で in-place に書き換えられるため、`loggerMap` も
    別 dict としてコピーしておく（復元時に「後から追加された子」を確実に取り除くため）。
    """
    snap = {}
    for name in names:
        obj = logging.Logger.manager.loggerDict.get(name)
        if isinstance(obj, logging.PlaceHolder):
            snap[name] = (obj, dict(obj.loggerMap))
        else:
            snap[name] = (obj, None)   # 未存在（None）または実 Logger（変更されない・参照で十分）
    return snap


def _restore_logger_dict_entries(snapshot):
    """`_snapshot_logger_dict_entries()` が撮った状態へ厳密に戻す（`_logging_module_lock()`
    保持下で呼ぶこと）。テストが新規に作った `PlaceHolder`（未存在だったキー）は削除し、
    既存の `PlaceHolder` は同一オブジェクトを残しつつ `loggerMap` だけテスト前の内容に戻す
    （実 Logger だった場合は元の参照をそのまま戻す＝ミューテーションされないため十分）。
    """
    for name, (obj, logger_map_copy) in snapshot.items():
        if obj is None:
            logging.Logger.manager.loggerDict.pop(name, None)
        elif isinstance(obj, logging.PlaceHolder):
            obj.loggerMap = logger_map_copy
            logging.Logger.manager.loggerDict[name] = obj
        else:
            logging.Logger.manager.loggerDict[name] = obj


def test_attach_request_id_filter_survives_concurrent_getlogger_calls():
    """`_attach_request_id_filter()` が `manager.loggerDict` を走査している間に、別スレッドが
    `logging.getLogger()` で新しい named logger を作っても（loggerDict へ挿入しても）
    `RuntimeError: dictionary changed size during iteration` を起こさない
    （`_logging_module_lock()` でスナップショットを取ってから走査する）。

    生成する logger 数は固定（50件）し、`threading.Barrier` で両スレッドの開始タイミングを
    揃えて確実に競合させる（`stop` フラグでの無制限ループにしない）。

    後始末: 葉の50 logger だけでなく、`logging.Logger.manager._fixupParents()` が新規に
    作る祖先 `PlaceHolder`（"sherpa.concurrent"／"sherpa.concurrent.probe" ——"sherpa" 自体は
    このモジュールの import 時点で実 Logger として既に存在するため対象外）も、テスト開始前に
    `_logging_module_lock()` 下でスナップショットを取り、終了後に同じロック下で元の状態
    （祖先が元々無ければ削除・元々 `PlaceHolder` だったなら `loggerMap` だけ巻き戻す）へ
    復元する——葉だけ pop するとこれらの `PlaceHolder` が loggerDict に残り続け、その
    `loggerMap` にも削除済みの Logger インスタンスへの参照が残ったままになる（他テストへの
    残骸）。
    """
    n_loggers = 50
    prefix = "sherpa.concurrent.probe"
    names = [f"{prefix}.{i}" for i in range(n_loggers)]
    ancestor_names = ["sherpa.concurrent", prefix]   # _fixupParents が新規に作りうる祖先
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    with ext_api._logging_module_lock():
        ancestor_snapshot = _snapshot_logger_dict_entries(ancestor_names)

    def _spawn_loggers():
        barrier.wait(timeout=5)
        for name in names:
            logging.getLogger(name)

    t = threading.Thread(target=_spawn_loggers)
    t.start()
    try:
        barrier.wait(timeout=5)   # attach 側もここで足並みを揃えてから走査を始める
        for _ in range(200):
            try:
                ext_api._attach_request_id_filter()
            except RuntimeError as e:
                errors.append(e)
    finally:
        t.join(timeout=5)
        assert not t.is_alive(), "logger 生成スレッドが join 後もまだ生存している"
        with ext_api._logging_module_lock():
            for name in names:
                logging.Logger.manager.loggerDict.pop(name, None)   # 葉の後始末
            _restore_logger_dict_entries(ancestor_snapshot)          # 祖先 PlaceHolder の後始末

    assert errors == [], f"並行 getLogger() 実行中に例外: {errors}"
