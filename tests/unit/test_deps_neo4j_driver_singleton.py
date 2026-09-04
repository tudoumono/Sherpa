"""`sherpa.deps` の Neo4j driver シングルトン化（性能台帳#17 QW2・DB 不要）。

以前は `neo4j_session()` の呼び出しごとに `GraphDatabase.driver()` を新規生成し `finally` で
即 `close()` していた——driver 自体が内部にコネクションプールを持つため、リクエスト毎に
作り直すとハンドシェイクを毎回支払っていた。ここでは `_driver()` がプロセス内シングルトンに
なったこと、接続先（世代キー）が変わった場合だけ作り直して旧 driver を close すること、
`neo4j_session()` は session の open/close のみで driver 自体は閉じないこと、
`close_neo4j_driver()` が明示的に閉じてシングルトンをリセットし、以後は新しい driver が
遅延生成されること、を偽 driver で固定する。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_neo4j_driver_singleton():
    """本ファイルのテストがプロセス内シングルトン `deps._neo4j_driver` を汚染しないようにする。"""
    from sherpa import deps

    saved_drv = deps._neo4j_driver
    saved_key = deps._neo4j_driver_key
    deps._neo4j_driver = None
    deps._neo4j_driver_key = None
    try:
        yield
    finally:
        if deps._neo4j_driver is not None and deps._neo4j_driver is not saved_drv:
            try:
                deps._neo4j_driver.close()
            except Exception:
                pass
        deps._neo4j_driver = saved_drv
        deps._neo4j_driver_key = saved_key


class _FakeSession:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


class _FakeDriver:
    def __init__(self, uri, auth, **kw):
        self.uri = uri
        self.auth = auth
        self.kw = kw
        self.closed = False

    def close(self):
        self.closed = True

    def session(self):
        return _FakeSession()


def _install_fake_driver_factory(monkeypatch):
    import neo4j as neo4j_mod

    created: list[_FakeDriver] = []

    def fake_driver(uri, auth, **kw):
        d = _FakeDriver(uri, auth, **kw)
        created.append(d)
        return d

    monkeypatch.setattr(neo4j_mod.GraphDatabase, "driver", fake_driver)
    return created


def test_driver_is_singleton_when_config_unchanged(monkeypatch):
    from sherpa import deps

    created = _install_fake_driver_factory(monkeypatch)
    monkeypatch.setattr(deps, "_neo4j_driver_config", lambda: ("bolt://x", "u", "p"))

    d1 = deps._driver()
    d2 = deps._driver()
    d3 = deps._driver()

    assert d1 is d2 is d3
    assert len(created) == 1


def test_driver_recreated_and_old_closed_on_config_change(monkeypatch):
    """世代キー（uri/user/password）が変わったら作り直し、旧 driver は close する。"""
    from sherpa import deps

    created = _install_fake_driver_factory(monkeypatch)
    configs = iter([("bolt://a", "u1", "p1"), ("bolt://b", "u2", "p2")])
    monkeypatch.setattr(deps, "_neo4j_driver_config", lambda: next(configs))

    d1 = deps._driver()
    d2 = deps._driver()

    assert d1 is not d2
    assert d1.closed is True
    assert d2.closed is False
    assert len(created) == 2
    assert d2.uri == "bolt://b"
    assert d2.auth == ("u2", "p2")


def test_neo4j_session_does_not_close_driver_between_uses(monkeypatch):
    """以前は `neo4j_session()` の `finally` で毎回 `drv.close()` していた——プロセス内シングルトン化
    後は session だけを開閉し、driver（内部コネクションプール）はリクエスト間で使い回す。"""
    from sherpa import deps

    _install_fake_driver_factory(monkeypatch)
    monkeypatch.setattr(deps, "_neo4j_driver_config", lambda: ("bolt://x", "u", "p"))

    with deps.neo4j_session():
        pass
    drv_after_first = deps._neo4j_driver
    assert drv_after_first is not None
    assert drv_after_first.closed is False

    with deps.neo4j_session():
        pass
    assert deps._neo4j_driver is drv_after_first   # 同一 driver を再利用（作り直していない）
    assert drv_after_first.closed is False


def test_close_neo4j_driver_resets_singleton_and_is_idempotent(monkeypatch):
    from sherpa import deps

    _install_fake_driver_factory(monkeypatch)
    monkeypatch.setattr(deps, "_neo4j_driver_config", lambda: ("bolt://x", "u", "p"))

    d1 = deps._driver()
    deps.close_neo4j_driver()
    assert d1.closed is True
    assert deps._neo4j_driver is None
    assert deps._neo4j_driver_key is None

    deps.close_neo4j_driver()   # 未生成状態での二重呼び出しは安全（何もしない）
    assert d1.closed is True

    d2 = deps._driver()   # 明示クローズ後は新しい driver を遅延生成する
    assert d2 is not d1
    assert d2.closed is False
