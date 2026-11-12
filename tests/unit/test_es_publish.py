"""ES 索引の公開（エイリアスの付け替え）と作り直しの前の空き確認の単体テスト。
ES は `_req` を差し替えた小さな模擬（物理索引とエイリアスの表）。公開中の索引を失わない順番だけを見る。
"""
from __future__ import annotations

import urllib.error

import pytest

from sherpa import es_index, es_publish
from sherpa.ingest import background
from tests._replace_row import offline_row

GB = 1024 ** 3


class FakeEs:
    def __init__(self, indices: dict, alias_to: str | None, free: int = 100 * GB):
        self.sizes = dict(indices)          # 物理索引名 -> 大きさ（バイト）
        self.alias_to = alias_to
        self.free = free
        self.log: list = []
        self.lose_response_on_aliases = False
        self.unacknowledged_aliases = False   # 2xx で `acknowledged:false`（付け替えは効いていない）

    def deletes(self) -> list:
        return [p for m, p in self.log if m == "DELETE"]

    def req(self, method, path, body=None, **kw):
        self.log.append((method, path))
        alias = es_index._index("w")
        if method == "GET" and path == f"/_alias/{alias}":
            if not self.alias_to:
                raise urllib.error.HTTPError(path, 404, "nf", {}, None)
            return {self.alias_to: {"aliases": {alias: {}}}}
        if method == "GET" and path.startswith("/_cat/indices/") and "*" in path:
            return [{"index": n} for n in sorted(self.sizes)]
        if method == "GET" and path.startswith("/_cat/indices/"):
            return [{"index": n, "store.size": str(s)} for n, s in self.sizes.items() if f"/{n}" in path or f",{n}" in path]
        if method == "GET" and path == "/_nodes/stats/fs":
            return {"nodes": {"n1": {"fs": {"total": {"available_in_bytes": self.free}}}}}
        if method == "POST" and path == "/_aliases":
            if self.unacknowledged_aliases:
                return {"acknowledged": False}
            for act in body["actions"]:
                if "add" in act:
                    self.alias_to = act["add"]["index"]
                if "remove" in act and self.alias_to == act["remove"]["index"]:
                    self.alias_to = None
            if self.lose_response_on_aliases:
                raise TimeoutError("response lost")
            return {"acknowledged": True}
        if method == "DELETE":
            name = path.lstrip("/")
            if name not in self.sizes:
                raise urllib.error.HTTPError(path, 404, "nf", {}, None)
            del self.sizes[name]
            return {"acknowledged": True}
        return {}


@pytest.fixture
def fake(monkeypatch):
    old = es_index._index("w") + "-g1"
    es = FakeEs({old: 10 * GB}, old, free=15 * GB)
    monkeypatch.setattr(es_index, "_req", es.req)
    monkeypatch.setattr(es_publish, "_CONFIRM_WAIT_SEC", 0)
    es_index._building.pop("w", None)
    yield es
    es_index._building.pop("w", None)


def test_shortage_stops_without_touching_the_published_index(fake):
    """空きが旧索引の 2 倍に満たなくても、旧索引を先に消さない。足りない量を返して止める。"""
    old = next(iter(fake.sizes))
    short = es_publish.rebuild_space_shortfall("w")
    assert short == {"free_bytes": 15 * GB, "need_bytes": 20 * GB, "shortfall_bytes": 5 * GB}
    assert fake.deletes() == [] and fake.alias_to == old and old in fake.sizes


def test_shortage_in_a_background_run_stops_with_low_disk(fake, monkeypatch):
    monkeypatch.setattr(background, "in_run", lambda: True)
    with pytest.raises(background.LowDiskStop):
        es_publish.rebuild_space_shortfall("w")
    assert fake.deletes() == []


def test_enough_space_is_not_a_shortage(fake):
    fake.free = 20 * GB
    assert es_publish.rebuild_space_shortfall("w") is None


def test_lost_alias_response_keeps_the_new_published_index(fake):
    """付け替えは効いたが応答だけ届かなかったとき、公開された新しい索引を消さない（旧索引は消える）。"""
    old = next(iter(fake.sizes))
    new = es_index._index("w") + "-g2"
    fake.sizes[new] = GB
    es_index._building["w"] = new
    fake.lose_response_on_aliases = True
    assert es_index._activate_index("w", new, row=offline_row("w")) == es_publish.ACTIVATED
    es_index._discard_building("w")
    assert fake.alias_to == new and new in fake.sizes and old not in fake.sizes
    assert fake.deletes() == ["/" + old]


def test_unacknowledged_swap_keeps_the_old_published_index(fake):
    """付け替えの応答が 2xx でも効いていなければ（`acknowledged:false`）、公開中の古い索引を消さない。"""
    old = next(iter(fake.sizes))
    new = es_index._index("w") + "-g2"
    fake.sizes[new] = GB
    fake.unacknowledged_aliases = True
    assert es_index._activate_index("w", new, row=offline_row("w")) == es_publish.NOT_ACTIVATED
    assert old in fake.sizes and fake.alias_to == old
    assert ("DELETE", "/" + old) not in fake.log


def test_alias_unconfirmable_keeps_both_generations(fake, monkeypatch):
    """付け替えが効いたか確かめられないときは、新旧どちらの索引も消さない。"""
    old = next(iter(fake.sizes))
    new = es_index._index("w") + "-g2"
    fake.sizes[new] = GB
    es_index._building["w"] = new
    fake.lose_response_on_aliases = True
    calls = {"n": 0}
    real = es_index._alias_targets

    def targets(world):
        calls["n"] += 1
        return real(world) if calls["n"] == 1 else None      # 付け替え前だけ読め、その後は確かめられない

    monkeypatch.setattr(es_index, "_alias_targets", targets)
    assert es_index._activate_index("w", new, row=offline_row("w")) == es_publish.UNKNOWN
    assert fake.deletes() == [] and {old, new} <= set(fake.sizes)
    es_index._discard_building("w")                       # 作りかけとして覚えていない＝後始末でも消えない
    assert fake.deletes() == []


def test_rejected_swap_leaves_the_old_index_and_discards_only_the_new(fake, monkeypatch):
    old = next(iter(fake.sizes))
    new = es_index._index("w") + "-g2"
    fake.sizes[new] = GB
    es_index._building["w"] = new

    def refuse(method, path, body=None, **kw):
        if method == "POST" and path == "/_aliases":
            fake.log.append((method, path))
            raise urllib.error.HTTPError(path, 400, "bad", {}, None)
        return FakeEs.req(fake, method, path, body, **kw)

    monkeypatch.setattr(es_index, "_req", refuse)
    assert es_index._activate_index("w", new, row=offline_row("w")) == es_publish.NOT_ACTIVATED
    es_index._discard_building("w")
    assert fake.alias_to == old and fake.deletes() == ["/" + new]


def test_discard_never_deletes_the_published_index(fake):
    """作りかけとして覚えている名前が実際は公開中でも、消さない。"""
    old = next(iter(fake.sizes))
    es_index._building["w"] = old
    es_index._discard_building("w")
    assert fake.deletes() == [] and old in fake.sizes


@pytest.mark.parametrize("esr,failed", [
    ({"available": True, "indexed": 1}, False),
    ({"available": False, "indexed": 0}, True),
    ({"available": None, "error": "OSError@h"}, True),
    ({"available": True, "error": "bulk_errors"}, True),
])
def test_es_failure_marks_the_row_item_failed(esr, failed):
    from sherpa.ingest import es_reflect
    row = offline_row("w")
    es_reflect.keep_row_on_es_failure("w", row, esr)
    assert row._offline == (["failed:es"] if failed else [])
