"""`sherpa.ingest.background`（取り込み run の背景実行レジストリ・ING-3）の単体テスト。

DB/Neo4j 不要——`work_fn`/`create_run` はテスト側が渡すダミー関数（`sherpa.chat_turns` の
登録/join テストと同じ流儀）。`report_run_id`/`threading.Event` 待ち合わせ機構は撤去され
（run_id は `create_run()` が呼び出し元の受付処理内で O(1) 確保する契約に変わったため待つ必要が
無い）、代わりに op/fingerprint 一致判定と CAS failed-close のセーフティネットが入った——
`fail_close_if_extracting` は `store` 経由（DB 呼び出し）のため、ここでは `sherpa.store` を
monkeypatch して DB 非依存に保つ。
"""
from __future__ import annotations

import threading
import time

import pytest

from sherpa import store, webhooks
from sherpa.ingest import background


@pytest.fixture(autouse=True)
def _stub_fail_close(monkeypatch):
    """最外周セーフティネット（CAS failed-close）は DB 呼び出しのため常に無害化する
    （個々のテストが明示的に検証したい時だけ上書きする）。"""
    monkeypatch.setattr(store, "fail_close_if_extracting", lambda run_id, reason: False)


def _wait_until_idle(world_id: str, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while background.is_running(world_id) and time.monotonic() < deadline:
        time.sleep(0.01)


def test_start_or_join_first_call_is_not_joined_and_calls_create_run_once():
    released = threading.Event()
    create_calls = []

    def create_run():
        create_calls.append(1)
        return 42

    def work_fn(run_id):
        assert run_id == 42
        released.wait(timeout=2.0)

    try:
        run_id, joined = background.start_or_join("w1", "refresh", "", create_run, work_fn)
        assert run_id == 42
        assert joined is False
        assert create_calls == [1]
    finally:
        released.set()
        _wait_until_idle("w1")


def test_start_or_join_second_call_same_op_and_fingerprint_joins_without_creating_new_run():
    started = threading.Event()
    released = threading.Event()

    def create_run():
        return 7

    def work_fn(run_id):
        started.set()
        released.wait(timeout=2.0)

    try:
        run_id1, joined1 = background.start_or_join("w2", "refresh", "fp-a", create_run, work_fn)
        assert run_id1 == 7 and joined1 is False
        started.wait(timeout=2.0)

        # 実行中に同じ world・同じ op/fingerprint で再度来ても create_run/work_fn は一切呼ばれず、
        # 同じ run_id へ合流する。
        calls = []
        run_id2, joined2 = background.start_or_join(
            "w2", "refresh", "fp-a", lambda: calls.append("create") or 999,
            lambda run_id: calls.append("work"))
        assert run_id2 == 7
        assert joined2 is True
        assert calls == []
    finally:
        released.set()
        _wait_until_idle("w2")


def test_start_or_join_mismatched_op_raises_conflict_error():
    """実行中に**別の操作**が来たら合流せず `ConflictError`（呼び出し側は 409 へ変換する）。
    無関係な実行中 run へ誤って「合流」させない。"""
    started = threading.Event()
    released = threading.Event()

    def work_fn(run_id):
        started.set()
        released.wait(timeout=2.0)

    try:
        background.start_or_join("w6", "refresh", "", lambda: 1, work_fn)
        started.wait(timeout=2.0)

        with pytest.raises(background.ConflictError) as ei:
            background.start_or_join("w6", "extract", "", lambda: 2, lambda run_id: None)
        assert ei.value.existing_op == "refresh"
        assert ei.value.existing_run_id == 1
    finally:
        released.set()
        _wait_until_idle("w6")


def test_start_or_join_mismatched_fingerprint_same_op_raises_conflict_error():
    """同じ操作種別でも payload（fingerprint）が違えば合流せず衝突扱い（例: 別スコープの
    extract が実行中に別スコープの extract が来た）。"""
    started = threading.Event()
    released = threading.Event()

    def work_fn(run_id):
        started.set()
        released.wait(timeout=2.0)

    try:
        background.start_or_join("w7", "extract", "scope-a", lambda: 5, work_fn)
        started.wait(timeout=2.0)

        with pytest.raises(background.ConflictError):
            background.start_or_join("w7", "extract", "scope-b", lambda: 6, lambda run_id: None)
    finally:
        released.set()
        _wait_until_idle("w7")


def test_start_or_join_after_completion_starts_a_new_run():
    run_id1, joined1 = background.start_or_join("w3", "refresh", "", lambda: 1, lambda run_id: None)
    assert run_id1 == 1 and joined1 is False
    _wait_until_idle("w3")
    assert background.is_running("w3") is False

    run_id2, joined2 = background.start_or_join("w3", "refresh", "", lambda: 2, lambda run_id: None)
    assert run_id2 == 2
    assert joined2 is False                     # 完了後の新規呼び出しは合流ではなく新規実行


def test_start_or_join_work_fn_exception_does_not_propagate_and_releases_registry():
    def _boom(run_id):
        raise RuntimeError("boom")

    run_id, joined = background.start_or_join("w4", "refresh", "", lambda: 99, _boom)
    assert run_id == 99 and joined is False
    _wait_until_idle("w4")
    assert background.is_running("w4") is False   # 例外後もレジストリが解放される（次回実行を妨げない）


def test_start_or_join_calls_fail_close_when_work_fn_leaves_run_extracting(monkeypatch):
    """最外周のセーフティネット——`work_fn` が例外で終わり、自分で run を terminal 化
    しなかった場合、`store.fail_close_if_extracting` が呼ばれる（CAS で status='extracting' の
    行だけを failed へ落とす・呼び出し元が既に terminal 化済みなら何もしない）。"""
    calls = []
    monkeypatch.setattr(store, "fail_close_if_extracting",
                        lambda run_id, reason: calls.append((run_id, reason)) or True)

    def _boom(run_id):
        raise RuntimeError("boom")

    background.start_or_join("w8", "refresh", "", lambda: 55, _boom)
    _wait_until_idle("w8")

    assert calls and calls[0][0] == 55


def test_start_or_join_notifies_webhook_when_fail_close_succeeds(monkeypatch):
    """RV是正#5: この CAS 自体が terminal 化（`status='extracting'`→`'failed'`）の成功なので、
    ここでも Webhook 通知する——通知は「terminal 更新が実際に成功した」ことだけを条件にする
    契約どおり、`fail_close_if_extracting` が True（実際に更新した）を返した時だけ呼ばれる。"""
    monkeypatch.setattr(store, "fail_close_if_extracting", lambda run_id, reason: True)
    notified = []
    monkeypatch.setattr(webhooks, "notify_run_terminal",
                        lambda world, run_id, op, status, **kw: notified.append(
                            (world, run_id, op, status)))

    def _boom(run_id):
        raise RuntimeError("boom")

    background.start_or_join("w10", "refresh", "", lambda: 77, _boom)
    _wait_until_idle("w10")

    assert notified == [("w10", 77, "refresh", "failed")]


def test_start_or_join_skips_webhook_notify_when_already_terminal(monkeypatch):
    """CAS が False（呼び出し元が既に自分で terminal 化済み）を返した場合はここでは通知しない
    （その操作自身の terminal 化パスが既に通知済みのはず＝二重通知しない）。"""
    monkeypatch.setattr(store, "fail_close_if_extracting", lambda run_id, reason: False)
    notified = []
    monkeypatch.setattr(webhooks, "notify_run_terminal",
                        lambda *a, **kw: notified.append((a, kw)))

    def _boom(run_id):
        raise RuntimeError("boom")

    background.start_or_join("w11", "refresh", "", lambda: 88, _boom)
    _wait_until_idle("w11")

    assert notified == []


def test_start_or_join_raises_when_not_accepting():
    """lifespan shutdown 後（`stop_accepting()`）は新規実行を受け付けない。"""
    background.stop_accepting()
    try:
        with pytest.raises(background.ShuttingDownError):
            background.start_or_join("w9", "refresh", "", lambda: 1, lambda run_id: None)
    finally:
        background.start_accepting()   # モジュールグローバル＝他テストへ影響しないよう明示的に戻す


def test_start_or_join_extra_keys_registers_alias_and_primary_key_join_matches():
    """`extra_keys` は `world_id` の**置き換えではなく別名の追加**——固定キー（例:
    `world_create` の新規登録仲裁）で受け付けた進行中の run は、`world_id` 単独（World 行出現後
    に別リクエストが通常経路で来た場合を模す）でも同じ run として検出され、同一
    op/fingerprint なら合流し新しい run を作らない。"""
    started = threading.Event()
    released = threading.Event()

    def create_run():
        return 101

    def work_fn(run_id):
        started.set()
        released.wait(timeout=2.0)

    try:
        run_id1, joined1 = background.start_or_join(
            "w11", "register", "fp-x", create_run, work_fn, extra_keys=("__new_world__",))
        assert run_id1 == 101 and joined1 is False
        started.wait(timeout=2.0)
        assert background.is_running("w11") is True
        assert background.is_running("__new_world__") is True

        calls = []
        run_id2, joined2 = background.start_or_join(
            "w11", "register", "fp-x", lambda: calls.append("create") or 999,
            lambda run_id: calls.append("work"))
        assert run_id2 == 101 and joined2 is True
        assert calls == []                      # 別 run を作らない・work_fn も再実行しない
    finally:
        released.set()
        _wait_until_idle("w11")
    assert background.is_running("w11") is False
    assert background.is_running("__new_world__") is False   # 完了で両方のキーから外れる


def test_start_or_join_extra_keys_primary_key_different_op_conflicts_without_new_run():
    """World 行出現後、同じ `world_id` へ**別の操作**（例: delete）が来た場合は合流せず
    `ConflictError`（呼び出し側 409）。`create_run` は呼ばれない＝別 run を作らない。"""
    started = threading.Event()
    released = threading.Event()

    def work_fn(run_id):
        started.set()
        released.wait(timeout=2.0)

    try:
        background.start_or_join("w12", "register", "fp-y", lambda: 202, work_fn,
                                 extra_keys=("__new_world__",))
        started.wait(timeout=2.0)

        create_calls = []
        with pytest.raises(background.ConflictError) as ei:
            background.start_or_join("w12", "delete", "", lambda: create_calls.append(1) or 303,
                                     lambda run_id: None)
        assert ei.value.existing_op == "register"
        assert ei.value.existing_run_id == 202
        assert create_calls == []
    finally:
        released.set()
        _wait_until_idle("w12")


def test_drain_returns_once_registry_is_empty():
    released = threading.Event()

    def work_fn(run_id):
        released.wait(timeout=2.0)

    background.start_or_join("w10", "refresh", "", lambda: 1, work_fn)
    released.set()
    background.drain(timeout=2.0)
    assert background.is_running("w10") is False
