"""単一 world（資料フォルダ）契約の受け入れ（鏡モデル・要 Neo4j＋PG）。

標準MVPは登録元フォルダを**全体で1本**に固定する（決定 2026-08-15）。ここでは:
2本目の登録は拒否される → 削除すれば別フォルダを登録できる（入れ替え） → 入れ替え後は
グラフ/台帳/チャット選択肢が新しいフォルダだけを指す、を固定する。

旧仕様（複数 world 同時登録・サブシステムごとに別 world）の回帰ガードは撤去済み。
"""
from __future__ import annotations

import pathlib
import shutil
import tempfile

import pytest
from _world_setup import driver

from sherpa import store, worlds

W1 = "test_multi_world_a"
W2 = "test_multi_world_b"


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """このファイルはログインせず直接叩く前提（compat モード）。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


from _corpus_helpers import _mk


def _names(drv, wid):
    with drv.session() as s:
        return sorted(r["n"] for r in s.run(
            "MATCH (x:Entity {world_id:$w}) RETURN x.name AS n", w=wid))


def test_second_world_rejected_and_swap_after_delete():
    from fastapi.testclient import TestClient
    from sherpa.api import app
    a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
    _mk(a, "サブシステムA", "FOOPROG")
    _mk(b, "サブシステムB", "BARPROG")
    drv = driver()
    c = TestClient(app)
    try:
        # 1本目は登録できる
        ra = worlds.register(W1, str(pathlib.Path(a).resolve()))
        assert ra["status"] in ("auto_published", "auto_published_with_flags")

        # 2本目（別 world_id・別参照元）は拒否＝登録元フォルダは全体で1本
        with pytest.raises(worlds.WorldConflict):
            worlds.register(W2, str(pathlib.Path(b).resolve()))
        # list_worlds() は fixtures 由来の world も含むため、対象2件の有無だけを見る
        assert W1 in set(worlds.list_worlds()) and W2 not in set(worlds.list_worlds())
        assert _names(drv, W2) == [] and store.list_documents(W2) == []

        # 一覧・チャット選択肢とも1本目だけ
        opts = c.get("/world-options").json()["worlds"]
        assert W1 in set(opts) and W2 not in set(opts)
        assert {d["name"] for d in store.list_documents(W1)} == {"サブシステムA/03_開発/01_ソース/FOOPROG.cbl"}

        # 削除すれば別フォルダへ入れ替えできる
        worlds.delete(W1)
        assert W1 not in set(worlds.list_worlds()) and _names(drv, W1) == []
        rb = worlds.register(W2, str(pathlib.Path(b).resolve()))
        assert rb["status"] in ("auto_published", "auto_published_with_flags")

        # 入れ替え後は新しいフォルダだけを指す（旧 world の残骸が混ざらない）
        assert W2 in set(worlds.list_worlds()) and W1 not in set(worlds.list_worlds())
        assert _names(drv, W2) == ["BARPROG"] and _names(drv, W1) == []
        assert {d["name"] for d in store.list_documents(W2)} == {"サブシステムB/03_開発/01_ソース/BARPROG.cbl"}
        assert store.list_documents(W1) == []
        sc = c.get(f"/scopes?world={W2}").json()
        assert sc.get("world") == W2
    finally:
        for w in (W1, W2):
            try:
                worlds.delete(w)
            except Exception:
                pass
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)
