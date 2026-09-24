"""`worlds.pin_world_root`（PART-4・TOCTOU 対策）の単体テスト。DB 不要（`store.get_world` を
差し替えて registry の rebind をシミュレートする）。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from pathlib import Path  # noqa: E402

from sherpa import store, worlds  # noqa: E402

_WORLD_ID = "pinroottest"   # 実在しない world_id（fixtures/KB どちらにも無い＝registry 行が唯一の真実源）


def test_pin_world_root_overrides_registry_reads_for_matching_world(monkeypatch, tmp_path):
    """pin されている間は `store.get_world` を再度呼ばず、pin した root をそのまま返す
    （preflight と実行の間の registry rebind との TOCTOU 対策）。"""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    calls = []

    def fake_get_world(world_id):
        calls.append(world_id)
        # 1回目は root_a（初期登録）・2回目以降は root_b（rebind 後）を返す。
        return {"root_path": str(root_b if len(calls) > 1 else root_a)}

    monkeypatch.setattr(store, "get_world", fake_get_world)

    assert worlds.world_dir(_WORLD_ID) == root_a   # pin 無し・初回解決
    with worlds.pin_world_root(_WORLD_ID, root_a):
        # registry が root_b へ rebind された後でも、pin されている間は再解決しない。
        assert worlds.world_dir(_WORLD_ID) == root_a
        assert worlds.world_dir(_WORLD_ID) == root_a
    assert calls == [_WORLD_ID], "pin 中は store.get_world を一度も呼ばないはず"
    # pin を抜けたら通常解決に戻る（＝registry の最新値 root_b が見える）。
    assert worlds.world_dir(_WORLD_ID) == root_b


def test_pin_world_root_scope_limited_to_matching_world_id(monkeypatch, tmp_path):
    """pin は世界単位のスコープ限定——別の world_id の解決には影響しない。"""
    pinned_root = tmp_path / "pinned"
    other_root = tmp_path / "other"
    pinned_root.mkdir()
    other_root.mkdir()
    monkeypatch.setattr(store, "get_world",
                        lambda world_id: {"root_path": str(other_root)} if world_id == "otherworld" else None)

    with worlds.pin_world_root(_WORLD_ID, pinned_root):
        assert worlds.world_dir(_WORLD_ID) == pinned_root
        assert worlds.world_dir("otherworld") == other_root   # 別 world は pin の影響を受けない


def test_pin_world_root_resets_after_context_exit_even_on_exception(monkeypatch, tmp_path):
    root_a = tmp_path / "a"
    root_a.mkdir()
    monkeypatch.setattr(store, "get_world", lambda world_id: None)   # 未登録扱い（fixtures/KB も無し）

    try:
        with worlds.pin_world_root(_WORLD_ID, root_a):
            assert worlds.world_dir(_WORLD_ID) == root_a
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert worlds.world_dir(_WORLD_ID) is None   # pin が正しく解除され、通常解決（未登録＝None）に戻る


def test_pin_world_root_nested_inner_wins_then_restores_outer(tmp_path):
    outer = tmp_path / "outer"
    inner = tmp_path / "inner"
    outer.mkdir()
    inner.mkdir()
    with worlds.pin_world_root(_WORLD_ID, outer):
        assert worlds.world_dir(_WORLD_ID) == outer
        with worlds.pin_world_root(_WORLD_ID, inner):
            assert worlds.world_dir(_WORLD_ID) == inner
        assert worlds.world_dir(_WORLD_ID) == outer
