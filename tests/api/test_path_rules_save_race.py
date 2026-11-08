"""優先・除外の保存は、読み取り・マージ・書き込みを 1 つのトランザクションで行う（別の資料フォルダの規則を同時の保存で消さない）。実 DB が要る。"""
from __future__ import annotations

import threading
import time

import pytest

from sherpa import path_rules as PR


@pytest.fixture(autouse=True)
def _db():
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


@pytest.fixture
def clean_setting():
    from sherpa import store
    store.set_system_settings("admin", {PR.SETTINGS_KEY: None})
    yield
    store.set_system_settings("admin", {PR.SETTINGS_KEY: None})


def _rule(path):
    return PR.validate_entries([{"path": path, "kind": "exclude", "target_type": "folder"}])


def test_concurrent_saves_of_different_worlds_do_not_lose_each_other(clean_setting):
    """別の資料フォルダの同時の保存で、規則が消えない（読み取り・マージ・書き込みが同じトランザクション）。"""
    from sherpa import store
    errs = []

    def save(world, n):
        try:
            for i in range(n):
                PR.save_world("admin", world, _rule(f"p{i}"))
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=save, args=(w, 15)) for w in ("wa", "wb", "wc")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    cur = store._read_system_settings_fresh()[PR.SETTINGS_KEY]
    assert set(cur) == {"wa", "wb", "wc"} and all(cur[w][0]["path"] == "p14" for w in cur)


def test_save_does_not_overwrite_a_change_made_between_read_and_write(clean_setting, monkeypatch):
    """読んだ後・書く前に別の保存が入っても、その資料フォルダの規則を消さない（トランザクションの外で読むと消える）。"""
    from sherpa import store
    PR.save_world("admin", "wa", _rule("a1"))
    real_read, real_modify = store._read_system_settings_fresh, store.modify_system_setting
    started: list = []

    def race():
        if not started:
            t = threading.Thread(target=lambda: PR.save_world("admin", "wb", _rule("b1")))
            started.append(t)
            t.start()
            t.join(timeout=0.5)       # トランザクションの中なら、別の保存はロックで待たされる

    def read_then_race(*a, **k):      # トランザクションの外で読む実装なら、読んだ直後に割り込まれる
        data = real_read(*a, **k)
        race()
        return data

    def modify_then_race(uid, key, mutate):
        return real_modify(uid, key, lambda cur: (race(), mutate(cur))[1])
    monkeypatch.setattr(store, "_read_system_settings_fresh", read_then_race)
    monkeypatch.setattr(store, "modify_system_setting", modify_then_race)
    PR.save_world("admin", "wc", _rule("c1"))
    monkeypatch.setattr(store, "_read_system_settings_fresh", real_read)
    monkeypatch.setattr(store, "modify_system_setting", real_modify)
    started[0].join(timeout=5)
    assert set(real_read()[PR.SETTINGS_KEY]) == {"wa", "wb", "wc"}
