"""GRA-1: `preview_service.graph_view` のプロセス内キャッシュ（world の `last_sig`＋`last_synced_at`
を複合キーにした limit 適用前の全体 view キャッシュ）。`_build`（原本木＋意味層の再構築——Neo4j を
直接読み返す処理ではない）が呼ばれる回数を monkeypatch でカウントして検証する（外部サービス不要）。
"""
from __future__ import annotations

import threading
import time

import pytest

from sherpa import preview_service as ps


def _nodes_edges():
    nodes = [
        {"cid": "n1", "name": "Alpha", "label": "Module", "extraction_method": "static",
         "status": "active", "value": None, "top_scope": None, "path": None},
        {"cid": "n2", "name": "Bravo", "label": "Module", "extraction_method": "static",
         "status": "active", "value": None, "top_scope": None, "path": None},
    ]
    edges = [{"src": "n1", "dst": "n2", "type": "CALLS", "extraction_method": "static", "status": "active"}]
    return nodes, edges, []


def _status(sig, synced_at="t0", root_path="/fake/root"):
    # root_path はダミー（`_resolved_root` は autouse フィクスチャで True 固定＝実際に stat しない）。
    return {"sig": sig, "synced_at": synced_at, "root_path": root_path}


@pytest.fixture(autouse=True)
def _clear_cache():
    ps._GRAPH_VIEW_CACHE.clear()
    yield
    ps._GRAPH_VIEW_CACHE.clear()


@pytest.fixture(autouse=True)
def _resolved_by_default(monkeypatch):
    """既定では root 到達可否の判定をバイパスして True 扱いにする（キャッシュ経路自体の検証に
    ファイルシステムを絡めない）。unresolved を検証するテストは個別に上書きする。"""
    monkeypatch.setattr(ps, "_resolved_root", lambda root_path: True)


def _patch_build(monkeypatch, calls):
    def _build(world):
        calls.append(world)
        return _nodes_edges()
    monkeypatch.setattr(ps, "_build", _build)


def _patch_status(monkeypatch, status_value):
    """`_current_world_status` を固定値で差し替える（呼ばれるたびに同じ dict を返す＝
    構築前後で世代が動かない想定のテスト用）。`**kw` は lock 保持中の再プローブが渡す
    `connect_timeout`/`statement_timeout_ms` を無視するため。"""
    monkeypatch.setattr(ps, "_current_world_status", lambda world, **kw: dict(status_value))


def _patch_status_sequence(monkeypatch, values):
    """`_current_world_status` の呼び出しごとに順番に別の値を返す（世代が動く想定のテスト用）。"""
    it = iter(values)
    monkeypatch.setattr(ps, "_current_world_status", lambda world, **kw: dict(next(it)))


def test_same_generation_uses_cache_build_called_once(monkeypatch):
    calls: list[str] = []
    _patch_build(monkeypatch, calls)
    _patch_status(monkeypatch, _status("sig-a", "t1"))

    r1 = ps.graph_view("w1")
    r2 = ps.graph_view("w1")

    assert len(calls) == 1                       # 2回目は _build を呼ばない
    assert r1["signature"] == r2["signature"]
    assert r1["nodes"] == r2["nodes"]


def test_sig_change_triggers_rebuild(monkeypatch):
    calls: list[str] = []
    _patch_build(monkeypatch, calls)
    status = {"v": _status("sig-a", "t1")}
    monkeypatch.setattr(ps, "_current_world_status", lambda world, **kw: dict(status["v"]))

    ps.graph_view("w1")
    status["v"] = _status("sig-b", "t2")          # sync/rebind 相当で内容と世代の両方が変わる
    ps.graph_view("w1")

    assert len(calls) == 2


def test_aba_sig_returns_to_same_value_without_intervening_get_forces_rebuild(monkeypatch):
    """GRA-1是正#1（ABA）: concepts/extract 等の再実行は成功時に原本由来の同じ last_sig（`A`）へ
    戻りうる（`A→""→A`）。その空文字を挟む窓の間に一度も `graph_view()` が呼ばれなくても、
    `last_synced_at`（pre-invalidate/確定のたびに必ず進む・`set_world_sig` の契約）が複合キーに
    入っていれば、sig だけが元に戻った再構築後の呼び出しはキャッシュを再利用せず必ず再構築する
    （sig 単独の旧実装なら1回のままで恒久ヒットするバグ）。
    """
    calls: list[str] = []
    _patch_build(monkeypatch, calls)
    # 呼び出し元（このテスト）の視点では "" を挟む窓の間に GET が来ない＝sig="A" の2つの異なる
    # 世代（synced_at が違う）だけが見える。1回の graph_view() の miss 経路は「outer pre-check→
    # lock 内 recheck→post-build verify」で _current_world_status を3回呼ぶ（GRA-1是正RV2#2）。
    _patch_status_sequence(monkeypatch, [
        _status("A", "t1"), _status("A", "t1"), _status("A", "t1"),   # 1回目の呼び出し
        _status("A", "t2"), _status("A", "t2"), _status("A", "t2"),   # 2回目: sig は A に戻ったが世代（synced_at）は進んでいる
    ])

    ps.graph_view("w1")
    ps.graph_view("w1")

    assert len(calls) == 2


def test_empty_sig_evicts_existing_entry_before_building(monkeypatch):
    """GRA-1是正#6: 未同期/pre-invalidate 中（`sig` 空）は構築**前**に既存キャッシュを破棄し、
    構築してもキャッシュしない（従来どおりの挙動）。"""
    calls: list[str] = []
    cache_had_entry_at_build_time: list[bool] = []

    def _build(world):
        cache_had_entry_at_build_time.append("w1" in ps._GRAPH_VIEW_CACHE)
        calls.append(world)
        return _nodes_edges()

    monkeypatch.setattr(ps, "_build", _build)

    _patch_status(monkeypatch, _status("sig-a", "t1"))
    ps.graph_view("w1")
    assert "w1" in ps._GRAPH_VIEW_CACHE           # 前提: 一度キャッシュされている

    _patch_status(monkeypatch, _status("", None))
    ps.graph_view("w1")

    assert cache_had_entry_at_build_time == [False, False]   # 2回目の構築時点では既に破棄済み
    assert "w1" not in ps._GRAPH_VIEW_CACHE
    assert len(calls) == 2


def test_limit_variants_share_single_build(monkeypatch):
    """limit 違い（絞り込み→全件の逆順でも）キャッシュは共有され、_build は1回だけ。"""
    calls: list[str] = []
    _patch_build(monkeypatch, calls)
    _patch_status(monkeypatch, _status("sig-a", "t1"))

    limited = ps.graph_view("w1", limit=1)
    full = ps.graph_view("w1", limit=0)

    assert len(calls) == 1
    assert full["total_nodes"] == 2 and full["truncated"] is False
    assert limited["truncated"] is True and len(limited["nodes"]) == 1
    assert full["signature"] == limited["signature"]   # 署名は limit 非依存・呼び出し順にも依存しない


def test_different_worlds_do_not_share_cache(monkeypatch):
    calls: list[str] = []
    _patch_build(monkeypatch, calls)
    _patch_status(monkeypatch, _status("sig-a", "t1"))

    ps.graph_view("w1")
    ps.graph_view("w2")

    assert calls == ["w1", "w2"]


def test_unresolved_root_is_not_cached_and_self_heals(monkeypatch):
    """GRA-1是正#2: 一時的な参照先未解決（root 到達不可）で構築した view は sig が有効でも
    キャッシュしない——解決すれば次の呼び出しで自己修復する（一時失敗を恒久ヒットさせない）。
    """
    calls: list[str] = []
    _patch_build(monkeypatch, calls)
    _patch_status(monkeypatch, _status("sig-a", "t1"))
    monkeypatch.setattr(ps, "_resolved_root", lambda root_path: False)   # 一時的に未解決

    ps.graph_view("w1")
    assert "w1" not in ps._GRAPH_VIEW_CACHE
    ps.graph_view("w1")
    assert len(calls) == 2                        # 未解決の間は毎回再構築（キャッシュされていない証拠）

    monkeypatch.setattr(ps, "_resolved_root", lambda root_path: True)    # 復旧
    ps.graph_view("w1")
    assert "w1" in ps._GRAPH_VIEW_CACHE
    ps.graph_view("w1")
    assert len(calls) == 3                        # 復旧後はキャッシュが効いて4回目は再構築しない


def test_generation_moved_during_build_is_not_published(monkeypatch):
    """公開直前の再確認（GRA-1是正#2）: 構築中に世代（sig/synced_at）が進んでいたら公開しない。
    outer pre-check・lock 内 recheck は一致（＝構築へ進む）、構築後の post-verify だけが
    別世代を返す（＝構築の**最中**に世代が動いたことを模擬・GRA-1是正RV2#2 で recheck が
    増えたため3回のうち最後だけをずらす）。"""
    calls: list[str] = []
    _patch_build(monkeypatch, calls)
    _patch_status_sequence(monkeypatch, [_status("A", "t1"), _status("A", "t1"), _status("B", "t2")])

    ps.graph_view("w1")

    assert "w1" not in ps._GRAPH_VIEW_CACHE


def test_concurrent_misses_are_single_flighted(monkeypatch):
    """GRA-1是正#5: 並行20要求の同時 miss は1本の `threading.Lock` で直列化され、`_build` は
    1回だけ呼ばれる（並行 stampede を防ぐ）。"""
    calls: list[str] = []

    def _build(world):
        calls.append(world)
        time.sleep(0.05)          # 他スレッドがロック待ちで並ぶ時間を作る
        return _nodes_edges()

    monkeypatch.setattr(ps, "_build", _build)
    _patch_status(monkeypatch, _status("sig-a", "t1"))

    results: list[dict] = []
    results_lock = threading.Lock()

    def _call():
        r = ps.graph_view("w1")
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=_call) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(calls) == 1
    assert len(results) == 20
    sig0 = results[0]["signature"]
    assert all(r["signature"] == sig0 for r in results)


def test_delete_evicts_cache_entry(monkeypatch):
    """GRA-1是正#6: world 削除成功時にキャッシュ entry を破棄する（`worlds.delete`）。"""
    ps._GRAPH_VIEW_CACHE["w1"] = {"sig": "sig-a", "synced_at": "t1", "out_nodes": [], "out_edges": [],
                                  "counts": {}, "total_nodes": 0, "total_edges": 0, "signature": "x"}
    from sherpa import store, worlds as worlds_mod
    from sherpa.ingest import worker as worker_mod

    class _FakeLock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(store, "world_lock", lambda world_id, **kw: _FakeLock())
    monkeypatch.setattr(store, "delete_world_row", lambda world_id: True)
    monkeypatch.setattr(worker_mod, "_wipe_locked", lambda world_id, reflect=True: None)

    ok = worlds_mod.delete("w1", reflect=False)

    assert ok is True
    assert "w1" not in ps._GRAPH_VIEW_CACHE


def test_current_world_status_reads_store_row(monkeypatch):
    monkeypatch.setattr(ps.store, "get_world_status_row",
                        lambda w, **kw: {"last_sig": "abc", "last_synced_at": "t1", "root_path": "/r"})
    assert ps._current_world_status("w1") == {"sig": "abc", "synced_at": "t1", "root_path": "/r"}


def test_current_world_status_missing_row_returns_empty_generation(monkeypatch):
    """未登録 world（dev fixture 等）は行が無い——異常ではなく世代なし（sig=""）として扱う。"""
    monkeypatch.setattr(ps.store, "get_world_status_row", lambda w, **kw: None)
    assert ps._current_world_status("w1") == {"sig": "", "synced_at": None, "root_path": None}


def test_current_world_status_does_not_swallow_store_exceptions(monkeypatch):
    """GRA-1是正#3: store 例外は握り潰さない（silent degradation なし・router 側がログ付き503へ
    変換する契約——`sherpa/routers/graph.py::graph_get` 参照）。"""
    def _boom(w, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(ps.store, "get_world_status_row", _boom)

    with pytest.raises(RuntimeError):
        ps._current_world_status("w1")
    with pytest.raises(RuntimeError):
        ps.graph_view("w1")


def test_current_world_status_forwards_timeout_kwargs(monkeypatch):
    """GRA-1是正RV2#2: `connect_timeout`/`statement_timeout_ms` は `store.get_world_status_row`
    へそのまま転送される（`get_world()` と同じ既存 timeout 機構の再利用）。"""
    captured = {}

    def _fake(w, **kw):
        captured.update(kw)
        return {"last_sig": "abc", "last_synced_at": "t1", "root_path": "/r"}

    monkeypatch.setattr(ps.store, "get_world_status_row", _fake)
    ps._current_world_status("w1", connect_timeout=5, statement_timeout_ms=5000)
    assert captured == {"connect_timeout": 5, "statement_timeout_ms": 5000}


def test_post_probe_failure_does_not_repeat_heavy_build_across_waiters(monkeypatch):
    """GRA-1是正RV2#2: lock 取得直後の再プローブ（pre-build recheck・post-build verify とも
    `connect_timeout`/`statement_timeout_ms` 付きで呼ばれる同じ `_current_world_status`）が
    最初の1回だけ成功し以降失敗し続けても、待機列の後続スレッドは自分自身の再プローブの失敗で
    即座に諦め、`_build`（重い構築）を繰り返さない——3並行でも `_build` は1回だけ。
    """
    calls: list[str] = []
    _patch_build(monkeypatch, calls)

    count_lock = threading.Lock()
    state = {"n": 0}

    def _status_fn(world, **kw):
        if not kw:
            return _status("sig-a", "t1")          # fast-path（lock 外）の pre-check は kwargs 無し＝常に成功
        with count_lock:
            state["n"] += 1
            n = state["n"]
        if n == 1:
            return _status("sig-a", "t1")           # lock 内の最初の呼び出し（pre-build recheck）だけ成功
        raise RuntimeError("db down")                # 以降（post-verify・他スレッドの recheck）は失敗

    monkeypatch.setattr(ps, "_current_world_status", _status_fn)

    results: list[dict] = []
    errors: list[Exception] = []
    out_lock = threading.Lock()

    def _call():
        try:
            r = ps.graph_view("w1")
            with out_lock:
                results.append(r)
        except Exception as e:
            with out_lock:
                errors.append(e)

    threads = [threading.Thread(target=_call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(calls) == 1                     # _build は最初の1回だけ（残り2スレッドは build に到達しない）
    assert len(errors) == 3                     # 全員が最終的に失敗する（DB 完全ダウンを模擬）が build は1回のみ
    assert "w1" not in ps._GRAPH_VIEW_CACHE      # post-verify 失敗のため何も公開されていない


def test_delete_pop_is_serialized_with_publish_via_graph_view_lock(monkeypatch):
    """GRA-1是正RV2#3: `worlds.delete()` の pop は `_GRAPH_VIEW_LOCK` で build の代入と直列化
    される——delete の直後に旧 view が再挿入されて残ることを防ぐ。"""
    from sherpa import store, worlds as worlds_mod
    from sherpa.ingest import worker as worker_mod

    class _FakeLock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(store, "world_lock", lambda world_id, **kw: _FakeLock())
    monkeypatch.setattr(store, "delete_world_row", lambda world_id: True)
    monkeypatch.setattr(worker_mod, "_wipe_locked", lambda world_id, reflect=True: None)

    entered_publish = threading.Event()
    release_publish = threading.Event()
    order: list[str] = []

    def _slow_publish():
        with ps._GRAPH_VIEW_LOCK:
            entered_publish.set()
            release_publish.wait(timeout=5)
            ps._GRAPH_VIEW_CACHE["w1"] = {"sig": "sig-a", "synced_at": "t1", "out_nodes": [], "out_edges": [],
                                          "counts": {}, "total_nodes": 0, "total_edges": 0, "signature": "x"}
            order.append("publish")

    t_pub = threading.Thread(target=_slow_publish)
    t_pub.start()
    assert entered_publish.wait(timeout=5)

    def _delete():
        worlds_mod.delete("w1", reflect=False)
        order.append("delete")

    t_del = threading.Thread(target=_delete)
    t_del.start()
    time.sleep(0.1)                            # delete が pop 側で lock 待ちになる時間を作る
    assert "w1" not in ps._GRAPH_VIEW_CACHE     # publish 側はまだ release 待ちでキャッシュ未挿入

    release_publish.set()
    t_pub.join(timeout=5)
    t_del.join(timeout=5)

    assert order == ["publish", "delete"]        # delete の pop は publish の完了後に実行された
    assert "w1" not in ps._GRAPH_VIEW_CACHE      # delete が publish 後の entry を正しく破棄した
