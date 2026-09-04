"""`sherpa.ext_api._AuditWriter`（監査DB書込み専用の単一 writer スレッド＋bounded queue）の
状態遷移・キャンセル耐性の単体テスト。DB 不要（`_write_pending_audit` を monkeypatch する）。

対象: RUNNING→STOPPING→STOPPED の admission 制御（stop() と submit() の競合防止）、
キャンセル済み Future への set_result/set_exception が writer スレッドを道連れにしないこと、
join タイムアウト時に thread 参照を保持する（二重 writer 防止）こと、`_transition_lock` による
start()/stop() 全体のライフサイクル直列化（並行 start/stop・sentinel 一度きり投入・stop 後の
queue 残留無し）、`_stopped_event`/`_sentinel_put` による世代管理（join timeout 後の
start()/stop() 再試行が新旧スレッドを取り違えない・sentinel を二重投入しない）。
"""
from __future__ import annotations

import threading
import time

import pytest

from sherpa import ext_api


@pytest.fixture
def writer(monkeypatch):
    """DB に触れない `_write_pending_audit` へ差し替えた、独立の `_AuditWriter` インスタンス
    （共有シングルトン `ext_api._audit_writer` には触れない）。"""
    calls = []

    def _fake_write(pending, status_code, duration_ms, method, path, request_id):
        calls.append(request_id)

    monkeypatch.setattr(ext_api, "_write_pending_audit", _fake_write)
    w = ext_api._AuditWriter(maxsize=100)
    w.calls = calls   # type: ignore[attr-defined]
    try:
        yield w
    finally:
        w.stop(drain_timeout=2)


def _submit(w, request_id="rid"):
    return w.submit({"actor": "ext:1", "action": "a", "resource_type": "t"}, 200, 1.0,
                    "GET", "/x", request_id)


def _wait_until(condition, timeout=5.0, interval=0.005) -> bool:
    """`condition()` が真になるまで短間隔でポーリングする（固定 `time.sleep()` 1発の
    「たぶんこのくらいで終わっているはず」という当てずっぽうを避ける・遅いマシンでの
    flaky を防ぐ）。`stop()`/`start()` が内部で `_transition_lock` を取った/`thread.join()`
    でブロックし始めた、といった直接 instrument できない内部状態の到達を待つのに使う。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


def test_submit_rejected_after_stop(writer):
    """`stop()` で状態が STOPPED になった後の `submit()` は None を返す（受け付けない）。"""
    writer.start()
    writer.stop(drain_timeout=2)
    assert writer._state == ext_api._WRITER_STOPPED
    assert _submit(writer) is None


def test_stop_drains_items_submitted_before_stop(writer):
    """`stop()` 呼び出し前に投入済みの item は、stop() が完了するまでに確実に処理される
    （sentinel は投入済み item の後ろに入る）。"""
    writer.start()
    futs = [_submit(writer, request_id=f"rid-{i}") for i in range(5)]
    assert all(f is not None for f in futs)
    writer.stop(drain_timeout=5)
    for f in futs:
        assert f.done()
        assert f.exception() is None
    assert sorted(writer.calls) == [f"rid-{i}" for i in range(5)]


def test_cancelled_future_does_not_kill_writer_thread(writer, monkeypatch):
    """投入済み item の Future が処理前にキャンセルされていても（呼び出し側のタイムアウト等）、
    writer スレッドは `InvalidStateError` を吸収して次の item の処理を続ける
    （1件の Future 競合で writer loop ごと終了しない）。"""
    writer.start()
    release = threading.Event()
    entered = threading.Event()

    def _slow_then_normal(pending, status_code, duration_ms, method, path, request_id):
        if request_id == "cancel-me":
            entered.set()
            release.wait(timeout=5)   # writer がこの item を処理し始めてから、外側でキャンセルする猶予
        writer.calls.append(request_id)

    monkeypatch.setattr(ext_api, "_write_pending_audit", _slow_then_normal)

    fut1 = _submit(writer, request_id="cancel-me")
    assert entered.wait(timeout=5), "writer が cancel-me の処理に入らなかった"
    fut1.cancel()      # 呼び出し側は既に諦めた（例: asyncio.wrap_future 側のタイムアウト）ことを模擬
    release.set()      # writer 側の処理を進める（cancel 済み Future への set_result を誘発する）

    fut2 = _submit(writer, request_id="after-cancel")
    assert fut2.result(timeout=5) is None   # writer が生きていて次の item を処理できる
    assert "after-cancel" in writer.calls


def test_start_waits_for_concurrent_stop_to_finish(writer, monkeypatch):
    """stop() の実行中（`thread.join()` でブロック中）に別スレッドが `start()` を呼んでも、
    `_transition_lock` により stop() の完了を待ってから走る——中途半端な状態（二重 thread・
    宙ぶらりんな STOPPING の上書き）を作らない。"""
    writer.start()
    release = threading.Event()
    entered = threading.Event()

    def _blocking_write(pending, status_code, duration_ms, method, path, request_id):
        if request_id == "blocker":
            entered.set()
            release.wait(timeout=10)
        writer.calls.append(request_id)

    monkeypatch.setattr(ext_api, "_write_pending_audit", _blocking_write)
    _submit(writer, request_id="blocker")
    assert entered.wait(timeout=5), "writer がブロッキング処理に入らなかった"

    stop_done = threading.Event()

    def _stop_worker():
        writer.stop(drain_timeout=5)
        stop_done.set()

    t_stop = threading.Thread(target=_stop_worker)
    t_stop.start()
    # `_transition_lock` は stop() が thread.join() でブロックしている間ずっと保持され続ける
    # （release.set() を呼ぶまでこちらから解放しない）——一度 locked() を確認できれば、以降
    # start() が同じ lock を取ろうとすれば必ずブロックされることがタイミングに依らず保証される
    # （sleep で「たぶんブロック中のはず」と当てずっぽうしない）。
    assert _wait_until(lambda: writer._transition_lock.locked()), (
        "stop() が _transition_lock を取得しなかった")

    start_done = threading.Event()

    def _start_worker():
        writer.start()
        start_done.set()

    t_start = threading.Thread(target=_start_worker)
    t_start.start()
    assert not start_done.is_set(), "start() が stop() の完了を待たずに走ってしまっている"
    assert not stop_done.is_set()

    release.set()   # writer の処理を完了させる → stop() の join が成立 → _transition_lock 解放
    t_stop.join(timeout=5)
    t_start.join(timeout=5)
    assert stop_done.is_set() and start_done.is_set()
    assert writer._state == ext_api._WRITER_RUNNING   # start() が後勝ち・最終的に RUNNING で終わる

    fut = _submit(writer, request_id="after-race")
    assert fut.result(timeout=5) is None
    assert "after-race" in writer.calls


def test_concurrent_stop_calls_share_the_same_completion(writer, monkeypatch):
    """複数スレッドが同時に `stop()` を呼んでも sentinel は一度だけ投入され、全ての `stop()`
    呼び出しが同じ完了を待って正常に戻る（`_transition_lock` を最初に取ったスレッドだけが
    実際の停止処理を行い、残りは既に STOPPED になった状態を見て早期 return する）。"""
    writer.start()
    put_calls = []
    orig_put = writer._q.put

    def _tracking_put(item, *a, **kw):
        put_calls.append(item)
        return orig_put(item, *a, **kw)

    monkeypatch.setattr(writer._q, "put", _tracking_put)

    results = []

    def _stop_worker():
        writer.stop(drain_timeout=5)
        results.append(threading.current_thread().name)

    threads = [threading.Thread(target=_stop_worker, name=f"stopper-{i}") for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 5, "全ての stop() 呼び出しが完了しているはず"
    sentinel_puts = [c for c in put_calls if c is None]
    assert len(sentinel_puts) == 1, f"sentinel は一度だけ投入されるはず（実際 {len(sentinel_puts)} 回）"
    assert writer._state == ext_api._WRITER_STOPPED


def test_stop_leaves_no_residual_sentinel_for_next_start(writer):
    """`stop()` 完了後、queue に sentinel が残っていない——残っていると次の `start()` が起動した
    新スレッドがそれを即座に消費してしまい、投入前に writer が終了してしまう。"""
    writer.start()
    writer.stop(drain_timeout=5)
    assert writer._q.qsize() == 0, f"stop() 後に queue へ残留物がある（qsize={writer._q.qsize()}）"

    writer.start()   # 新しい thread で再起動
    fut = _submit(writer, request_id="after-restart")
    assert fut.result(timeout=5) is None, "sentinel 残留により writer が即終了し、新規投入が処理されなかった"
    assert "after-restart" in writer.calls


def test_stop_join_timeout_keeps_thread_reference(writer, monkeypatch):
    """`stop()` の `thread.join(timeout=...)` がタイムアウトした（スレッドがまだ処理中）場合、
    `self._thread` を None にしない——lazy start が「未起動」と誤認して二重 writer を
    起動するのを防ぐ。"""
    writer.start()
    release = threading.Event()
    entered = threading.Event()

    def _blocking_write(pending, status_code, duration_ms, method, path, request_id):
        entered.set()
        release.wait(timeout=10)

    monkeypatch.setattr(ext_api, "_write_pending_audit", _blocking_write)
    try:
        _submit(writer, request_id="blocker")
        assert entered.wait(timeout=5), "writer がブロッキング処理に入らなかった"
        thread_before = writer._thread
        writer.stop(drain_timeout=0.2)   # 処理中なのですぐには終わらない＝join timeout
        assert writer._thread is thread_before, "join timeout で thread 参照が失われている"
        assert writer._state != ext_api._WRITER_STOPPED
    finally:
        release.set()
        if writer._thread is not None:
            writer._thread.join(timeout=5)


def test_join_timeout_then_start_stop_retries_are_deterministic(writer, monkeypatch):
    """`stop()` の `join` がタイムアウトした（旧世代がまだ処理中）直後の `start()`/`stop()`
    再試行が正しく振る舞うことを一続きのシナリオで固定する（世代管理）:

    - join timeout 直後の `start()` 再試行は、旧世代が本当に終わる（`_stopped_event` が
      set される）まで新スレッドを起こさない（同じ queue を新旧スレッドで取り合わせない）。
    - join timeout 直後の `stop()` 再試行は sentinel を二重投入しない（`_sentinel_put`）。
    - 旧世代が実際に終わった後は `start()` が新スレッドを正しく起動し、旧世代の item も
      新世代の item もどちらも処理されている。
    """
    monkeypatch.setattr(ext_api, "_AUDIT_QUEUE_DRAIN_TIMEOUT_S", 0.2)
    writer.start()
    release = threading.Event()
    entered = threading.Event()

    def _blocking_write(pending, status_code, duration_ms, method, path, request_id):
        if request_id == "blocker":
            entered.set()
            release.wait(timeout=10)
        writer.calls.append(request_id)

    monkeypatch.setattr(ext_api, "_write_pending_audit", _blocking_write)
    _submit(writer, request_id="blocker")
    assert entered.wait(timeout=5), "writer がブロッキング処理に入らなかった"

    old_thread = writer._thread
    writer.stop(drain_timeout=0.2)   # blocker がまだブロック中＝join timeout
    assert writer._state == ext_api._WRITER_STOPPING
    assert writer._thread is old_thread, "join timeout で thread 参照が失われている"
    assert writer._sentinel_put is True
    assert not writer._stopped_event.is_set()

    # start() 再試行: 旧世代が終わっていないので（_AUDIT_QUEUE_DRAIN_TIMEOUT_S=0.2s 待った末に）
    # 新スレッドを起こさず諦める。
    writer.start()
    assert writer._thread is old_thread, "旧世代が終わっていないのに新スレッドが起動された"
    assert writer._state == ext_api._WRITER_STOPPING

    # stop() 再試行: sentinel は二重投入しない。
    put_calls = []
    orig_put = writer._q.put

    def _tracking_put(item, *a, **kw):
        put_calls.append(item)
        return orig_put(item, *a, **kw)

    monkeypatch.setattr(writer._q, "put", _tracking_put)
    writer.stop(drain_timeout=0.2)   # 依然ブロック中＝再度 join timeout
    assert put_calls == [], f"sentinel が二重投入されている: {put_calls}"
    assert writer._state == ext_api._WRITER_STOPPING

    release.set()   # 旧世代の処理を完了させる（_run() の finally が _stopped_event.set() する）
    old_thread.join(timeout=5)
    assert writer._stopped_event.is_set()

    # start() 再試行: 旧世代の終了が確認できたので新スレッドを起動する。
    writer.start()
    assert writer._thread is not None and writer._thread is not old_thread
    assert writer._state == ext_api._WRITER_RUNNING

    fut = _submit(writer, request_id="after-generation")
    assert fut.result(timeout=5) is None
    assert "after-generation" in writer.calls
    assert "blocker" in writer.calls   # 旧世代の item も処理済みだった


def test_audit_db_connect_does_not_call_store_ensure_and_passes_timeouts(monkeypatch):
    """`_audit_db_connect()` は `store._ensure()`（unbounded な schema 初期化・advisory lock 待ち・
    DDL 実行に上限時間が無い）を一切呼ばず、`psycopg.connect()` へ接続確立・advisory lock 待ち・
    statement 実行それぞれの bounded timeout を渡す（`_write_pending_audit` から `_ensure()` を
    除去した回帰の防止）。DB 不要（`psycopg.connect` 自体を stub 化する）。"""
    import psycopg

    from sherpa import store

    ensure_calls = []
    monkeypatch.setattr(store, "_ensure", lambda: ensure_calls.append(True))

    connect_calls = []

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_connect(dsn, **kwargs):
        connect_calls.append(kwargs)
        return _FakeConn()

    monkeypatch.setattr(psycopg, "connect", _fake_connect)
    monkeypatch.setattr(ext_api, "_AUDIT_DB_CONNECT_TIMEOUT_S", 7.0)
    monkeypatch.setattr(ext_api, "_AUDIT_DB_LOCK_TIMEOUT_MS", 1234)
    monkeypatch.setattr(ext_api, "_AUDIT_DB_STATEMENT_TIMEOUT_MS", 5678)

    with ext_api._audit_db_connect():
        pass

    assert ensure_calls == [], "_audit_db_connect() が store._ensure()（unbounded）を呼んでいる"
    assert len(connect_calls) == 1
    kwargs = connect_calls[0]
    assert kwargs["connect_timeout"] == 7.0
    assert "lock_timeout=1234" in kwargs["options"]
    assert "statement_timeout=5678" in kwargs["options"]


def test_start_failure_after_join_timeout_self_heals_so_submit_does_not_stay_none_forever(
        writer, monkeypatch):
    """`start()` が旧世代の終了待ちでタイムアウトして False を返しても（旧世代の thread が
    まだブロッキング処理中）、旧世代が実際に終わった時点で自己回復し、以後の submit() が
    永久に None を返し続けることはない——`start()` を明示的に呼び直さなくても、次の submit()
    自身の lazy start が成功する（`_restart_requested` フラグを `_run()` の finally が見て
    state を STOPPED へ確定する）。"""
    monkeypatch.setattr(ext_api, "_AUDIT_QUEUE_DRAIN_TIMEOUT_S", 0.2)
    writer.start()
    release = threading.Event()
    entered_blocker = threading.Event()

    def _blocking_write(pending, status_code, duration_ms, method, path, request_id):
        if request_id == "blocker":
            entered_blocker.set()
            release.wait(timeout=10)
        writer.calls.append(request_id)

    monkeypatch.setattr(ext_api, "_write_pending_audit", _blocking_write)
    _submit(writer, request_id="blocker")
    assert entered_blocker.wait(timeout=5), "writer がブロッキング処理に入らなかった"

    old_thread = writer._thread
    writer.stop(drain_timeout=0.2)   # blocker がまだブロック中＝join timeout
    assert writer._state == ext_api._WRITER_STOPPING

    # start() 再試行: 旧世代が終わっていないので False（_restart_requested が立つ）。
    started = writer.start()
    assert started is False
    assert writer._restart_requested is True
    assert writer._thread is old_thread

    release.set()   # 旧世代の処理を完了させる
    old_thread.join(timeout=5)   # _run() の finally が state を STOPPED へ確定するまで待つ
    assert writer._state == ext_api._WRITER_STOPPED
    assert writer._restart_requested is False

    # start() を明示的に呼び直さなくても、submit() 自身の lazy start が成功する。
    fut = _submit(writer, request_id="after-self-heal")
    assert fut is not None, "自己回復せず submit() が永久に None を返している"
    assert fut.result(timeout=5) is None
    assert "after-self-heal" in writer.calls
    assert "blocker" in writer.calls


def test_write_pending_audit_does_not_call_store_ensure(monkeypatch):
    """`_write_pending_audit()`（`_audit_db_connect()` 単体ではなく、実際の監査書込み1行を
    書く経路全体）を通しても `store._ensure()`（unbounded）が呼ばれないことを固定する
    （`_audit_db_connect()` 単体の契約テストの end-to-end 版）。DB 不要（`psycopg.connect`／
    `store._audit_insert` を stub 化する）。"""
    import psycopg

    from sherpa import store

    ensure_calls = []
    monkeypatch.setattr(store, "_ensure", lambda: ensure_calls.append(True))

    insert_calls = []
    monkeypatch.setattr(store, "_audit_insert", lambda *a, **kw: insert_calls.append((a, kw)))

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(psycopg, "connect", lambda dsn, **kw: _FakeConn())

    pending = {"actor": "ext:1", "action": "ext_api.search", "resource_type": "ext_search",
              "resource_id": "v1", "detail": {}, "reason": None, "severity": None,
              "outcome": None, "business_outcome": "ok"}
    ext_api._write_pending_audit(pending, 200, 1.0, "GET", "/x", "rid-ensure-check")

    assert ensure_calls == [], "_write_pending_audit() が store._ensure()（unbounded）を呼んでいる"
    assert len(insert_calls) == 1


def test_start_locked_rechecks_stopped_event_under_lock_after_timeout_race(writer, monkeypatch):
    """`_start_locked()` の `_stopped_event.wait()` が（本来ならタイムアウトする状況で）
    タイムアウトを返しても、旧世代の `_run()` finally がちょうど同じタイミングで完了して
    いれば（状態確定＋Event set が同一 `_lock` 保持区間で行われる）、`_lock` 下の最終確認で
    それを取りこぼさずに拾って正常に新世代を起動する（lost wake-up 対策）。

    `threading.Barrier` で「start 側の1回目 wait() がタイムアウトを返す直前」と「旧世代の
    finally が `_stopped_event.set()` を呼ぶ直前（＝`_lock` 保持中）」を確定的に同期させる
    ——両者が揃うまで start 側は `_lock` の取得すら試みられないため、`_lock` の相互排他が
    「start 側が最終確認する時点では旧世代の状態確定＋Event set が必ず完了済み」という
    不変条件を機械的に保証する（sleep 依存ではない）。
    """
    writer.start()
    release = threading.Event()
    entered = threading.Event()

    def _blocking_write(pending, status_code, duration_ms, method, path, request_id):
        if request_id == "blocker":
            entered.set()
            release.wait(timeout=10)
        writer.calls.append(request_id)

    monkeypatch.setattr(ext_api, "_write_pending_audit", _blocking_write)
    _submit(writer, request_id="blocker")
    assert entered.wait(timeout=5), "writer がブロッキング処理に入らなかった"

    old_thread = writer._thread
    writer.stop(drain_timeout=0.2)   # blocker がまだブロック中＝join timeout
    assert writer._state == ext_api._WRITER_STOPPING

    barrier = threading.Barrier(2)   # `_stopped_event.wait()` の timeout 相当は monkeypatch で
    # 完全に上書きするため、実タイムアウト値（_AUDIT_QUEUE_DRAIN_TIMEOUT_S）はこのテストの
    # 結果に影響しない。
    orig_wait = writer._stopped_event.wait
    orig_set = writer._stopped_event.set
    wait_calls = {"n": 0}

    def _instrumented_wait(timeout=None):
        wait_calls["n"] += 1
        if wait_calls["n"] == 1:
            barrier.wait(timeout=5)   # 旧世代の finally が set() 直前で揃うのを待つ
            return False              # 1回目はタイムアウトしたことにする（本来の挙動を模す）
        return orig_wait(timeout=timeout)

    def _instrumented_set():
        barrier.wait(timeout=5)   # start 側の1回目 wait() タイムアウトと揃うのを待つ
        orig_set()                # ここで初めて実際に Event を set する（_lock 保持中）

    monkeypatch.setattr(writer._stopped_event, "wait", _instrumented_wait)
    monkeypatch.setattr(writer._stopped_event, "set", _instrumented_set)

    result_holder: dict = {}

    def _start_worker():
        result_holder["ok"] = writer._start_locked()

    t_start = threading.Thread(target=_start_worker)
    t_start.start()

    release.set()   # 旧世代の処理を完了させる → _run() が finally（barrier 待ち）へ向かう
    old_thread.join(timeout=5)
    t_start.join(timeout=5)

    # fixture の finally（`w.stop(drain_timeout=2)`）が新世代に対して実行される前に、
    # instrumented wait/set を確実に元へ戻す（`monkeypatch` の自動 undo は fixture の
    # teardown 順序上 `writer` より後になり、新世代の stop() が instrumented set() を
    # 経由して barrier 待ちのまま壊れる＝関係ない箇所でスレッド例外が漏れる）。
    monkeypatch.setattr(writer._stopped_event, "wait", orig_wait)
    monkeypatch.setattr(writer._stopped_event, "set", orig_set)

    assert result_holder.get("ok") is True, (
        "wait() タイムアウトと旧世代の完了がほぼ同時でも、lock 下の最終確認で拾って"
        "起動に成功するはず")
    assert writer._state == ext_api._WRITER_RUNNING
    assert writer._thread is not None and writer._thread is not old_thread

    fut = _submit(writer, request_id="after-race-recheck")
    assert fut.result(timeout=5) is None
    assert "after-race-recheck" in writer.calls
