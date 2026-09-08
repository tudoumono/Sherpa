"""同時実行上限（`chat_turns.MAX_TURNS_PER_USER`／`MAX_TURNS_GLOBAL`）の env 解析、および
管理画面（`system_settings`）が優先する実効値解決 `chat_turns.effective_limits()` の検証。

範囲外・非整数は既定へ戻す（黙って無制限や 0 にしない）。定数は import 時に決まるため、
解析関数 `_env_int` を直接検証する（`_ai_env_isolation` が両 env を隔離する前提）。
`effective_limits()` は `store.get_system_settings()` を monkeypatch して DB 値あり／なし／不正／
DB 不達／`None` 返り値の5パターンを検証する（`tests/unit/test_agentic_search.py` の BUDGET-1 段
（settings > env）テストと同型）。加えて、`effective_limits()`（DB 読み取りを伴いうる）が
`chat_turns.start_turn` の `_REGISTRY_LOCK` 取得前に解決されること（ロック保持中に DB を
読まないこと）をスレッドで固定する。
"""
from __future__ import annotations

import os
import threading

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import chat_turns as CT  # noqa: E402
from sherpa import store  # noqa: E402


def test_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("SHERPA_CHAT_MAX_TURNS_PER_USER", raising=False)
    monkeypatch.delenv("SHERPA_CHAT_MAX_TURNS_GLOBAL", raising=False)
    assert CT._env_int("SHERPA_CHAT_MAX_TURNS_PER_USER", 2, 1, 16) == 2
    assert CT._env_int("SHERPA_CHAT_MAX_TURNS_GLOBAL", 8, 1, 64) == 8


def test_valid_values_are_used(monkeypatch):
    monkeypatch.setenv("SHERPA_CHAT_MAX_TURNS_PER_USER", "4")
    monkeypatch.setenv("SHERPA_CHAT_MAX_TURNS_GLOBAL", "20")
    assert CT._env_int("SHERPA_CHAT_MAX_TURNS_PER_USER", 2, 1, 16) == 4
    assert CT._env_int("SHERPA_CHAT_MAX_TURNS_GLOBAL", 8, 1, 64) == 20


def test_out_of_range_and_garbage_fall_back(monkeypatch):
    monkeypatch.setenv("SHERPA_CHAT_MAX_TURNS_PER_USER", "0")       # 0＝受付不能は許さない
    monkeypatch.setenv("SHERPA_CHAT_MAX_TURNS_GLOBAL", "abc")
    assert CT._env_int("SHERPA_CHAT_MAX_TURNS_PER_USER", 2, 1, 16) == 2
    assert CT._env_int("SHERPA_CHAT_MAX_TURNS_GLOBAL", 8, 1, 64) == 8
    monkeypatch.setenv("SHERPA_CHAT_MAX_TURNS_GLOBAL", "999")       # 上限超過も既定へ
    assert CT._env_int("SHERPA_CHAT_MAX_TURNS_GLOBAL", 8, 1, 64) == 8


def test_module_constants_are_within_range():
    assert 1 <= CT.MAX_TURNS_PER_USER <= 16
    assert 1 <= CT.MAX_TURNS_GLOBAL <= 64


# ===== effective_limits(): system_settings（管理画面）> env の2段解決 =====

def test_effective_limits_falls_back_to_module_constants_when_settings_unset(monkeypatch):
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})
    assert CT.effective_limits() == (CT.MAX_TURNS_PER_USER, CT.MAX_TURNS_GLOBAL)


def test_effective_limits_settings_overrides_module_constants(monkeypatch):
    monkeypatch.setattr(CT, "MAX_TURNS_PER_USER", 2)
    monkeypatch.setattr(CT, "MAX_TURNS_GLOBAL", 8)
    monkeypatch.setattr(store, "get_system_settings",
                        lambda **kw: {"chat_max_turns_per_user": 5, "chat_max_turns_global": 30})
    assert CT.effective_limits() == (5, 30)


def test_effective_limits_settings_out_of_range_falls_back(monkeypatch):
    """範囲外（1〜16／1〜64 外）・非整数の保存値は「不正な保存値」として env 既定へ倒す
    （fail-safe・PUT 側の Field(ge,le) を通常はすり抜けないが、DB 直接編集等の破損値でも
    落ちないことを固定する）。"""
    monkeypatch.setattr(CT, "MAX_TURNS_PER_USER", 2)
    monkeypatch.setattr(CT, "MAX_TURNS_GLOBAL", 8)
    for bad in (0, -1, 17, "not-an-int"):
        monkeypatch.setattr(store, "get_system_settings", lambda bad=bad, **kw: {"chat_max_turns_per_user": bad})
        assert CT.effective_limits() == (2, 8), bad
    for bad in (0, -1, 65, "not-an-int"):
        monkeypatch.setattr(store, "get_system_settings", lambda bad=bad, **kw: {"chat_max_turns_global": bad})
        assert CT.effective_limits() == (2, 8), bad


def test_effective_limits_settings_read_failure_falls_back(monkeypatch):
    """DB 不達（`store.get_system_settings()` が例外）でもターン受付の上限解決自体は落ちない
    （`depth_profile.effective_base` と同じ fail-open）。"""
    def _boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(CT, "MAX_TURNS_PER_USER", 2)
    monkeypatch.setattr(CT, "MAX_TURNS_GLOBAL", 8)
    monkeypatch.setattr(store, "get_system_settings", _boom)
    assert CT.effective_limits() == (2, 8)


def test_effective_limits_settings_none_return_falls_back(monkeypatch):
    """`store.get_system_settings()` は契約上 `None` を返すこともあり得る（例外を送出せず
    そのまま返る場合）——`sysset.get(...)` が `AttributeError` で落ちずに env 既定へ倒すことを
    固定する（`depth_profile.effective_base` の `(system_settings or {}).get(...)` と同じ
    fail-open）。"""
    monkeypatch.setattr(CT, "MAX_TURNS_PER_USER", 2)
    monkeypatch.setattr(CT, "MAX_TURNS_GLOBAL", 8)
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: None)
    assert CT.effective_limits() == (2, 8)


# ===== start_turn: _REGISTRY_LOCK 保持中に DB を読まない（呼び出し順序）=====

def test_start_turn_resolves_effective_limits_before_acquiring_registry_lock(monkeypatch):
    """`effective_limits()`（キャッシュミス時に DB へ読みに行きうる）は `start_turn` が
    `_REGISTRY_LOCK` を取得する**前**に解決される契約——DB 読み取りが詰まっている間も
    `_REGISTRY_LOCK` は空いたままで、同じロックを取る `get_turn`/`list_running`/`stop_turn` を
    待たせない（`conversation_factory()` を lock の外で呼ぶのと同じ「DB I/O をロック保持中に
    行わない」原則の一部）。`store.get_system_settings` を意図的に足止めし、その間に
    `_REGISTRY_LOCK` を非ブロッキングで取得できることを確認する。
    """
    entered = threading.Event()
    release = threading.Event()
    holder: dict = {}

    def _blocking_get_system_settings(**kw):
        entered.set()
        release.wait(timeout=5)
        return {}

    monkeypatch.setattr(store, "get_system_settings", _blocking_get_system_settings)

    def _noop_run(stop_event, emit):
        pass

    def _call():
        holder["rec"] = CT.start_turn(uid="lock-order-check", conversation_factory=lambda: 999,
                                      run_fn_factory=lambda cid: _noop_run)

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    try:
        assert entered.wait(timeout=2), "DB 読み取り（store.get_system_settings）に入っていない"
        got = CT._REGISTRY_LOCK.acquire(blocking=False)
        try:
            assert got, "_REGISTRY_LOCK が DB 読み取り中に保持されている（ロック取得前に解決すべき）"
        finally:
            if got:
                CT._REGISTRY_LOCK.release()
    finally:
        release.set()
        t.join(timeout=5)
    rec = holder.get("rec")
    if rec is not None:
        with CT._REGISTRY_LOCK:
            CT._REGISTRY.pop(rec.turn_id, None)   # テストの後始末（レジストリを汚染しない）
