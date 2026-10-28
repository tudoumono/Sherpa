"""`preview_service` の画面を開いたとき（`build_preview`）と「関係を確認」（`build_preview_graph`）の分担。

`build_preview` はグラフを組み立てず、台帳が空のとき（これらのテストの資料フォルダ）だけ資料フォルダを期限つきで歩いて
文書一覧を作る。重要度の解決・診断は世代で使い回さない（走査結果の使い回しは別のテスト）。グラフは `build_preview_graph` が
世代キャッシュ（`_GRAPH_VIEW_CACHE`）を共有して組み立てる。`_build` はスタブせず実際に `world_graph.build_world` まで通す
小さな tmp 資料フォルダを使い、`scope_infer.safe_files` と `_build` の呼び出し回数で「歩く回数」を固定する。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from sherpa import preview_service as ps
from sherpa import scope_infer, worlds
from sherpa.ingest import importance as importance_mod


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _world(monkeypatch, tmp_path) -> Path:
    """`_重要度.txt` 込みの最小 world（`tests/unit/test_importance.py::_world` と同型）。"""
    wd = tmp_path / "world"; wd.mkdir()
    der = tmp_path / "derived"; der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "derived_dir", lambda w: tmp_path / "derived_root")
    monkeypatch.setattr(worlds, "world_label", lambda w: w)   # DB（store.get_world）を経路から外す
    _write(wd / "a.md", "x")
    _write(wd / "_重要度.txt", "*.md: 高")
    return wd


def _status(sig, synced_at="t0", root_path="/fake/root"):
    return {"sig": sig, "synced_at": synced_at, "root_path": root_path}


@pytest.fixture(autouse=True)
def _clear_state():
    ps._GRAPH_VIEW_CACHE.clear()
    importance_mod._CACHE.clear()
    yield
    ps._GRAPH_VIEW_CACHE.clear()
    importance_mod._CACHE.clear()


@pytest.fixture(autouse=True)
def _no_active_run(monkeypatch):
    """取り込み中の run は無い（DB を引かない）。取り込み中を検証するテストは個別に上書きする。"""
    monkeypatch.setattr(ps.store, "get_latest_run_summary", lambda w, **kw: None)


@pytest.fixture(autouse=True)
def _resolved_by_default(monkeypatch):
    """既定では root 到達可否の判定をバイパスして True 扱いにする（`world_dir` 自体を monkeypatch
    済みのため pin の有無は結果に影響しない・GRA-1 のテストと同じ考え方）。"""
    monkeypatch.setattr(ps, "_resolved_root", lambda root_path: True)


def _patch_status(monkeypatch, status_value):
    monkeypatch.setattr(ps, "_current_world_status", lambda world, **kw: dict(status_value))


def _count_safe_files(monkeypatch):
    """`scope_infer.safe_files` の呼び出し回数を数えつつ実体はそのまま動かす（`_build` は
    スタブしない＝`world_graph.build_world` 内部の歩行も対象に含める・finding #3 是正）。"""
    calls = {"n": 0}
    real = scope_infer.safe_files

    def _counting(wd, **kw):
        calls["n"] += 1
        return real(wd, **kw)

    monkeypatch.setattr(scope_infer, "safe_files", _counting)
    return calls


def _count_build(monkeypatch):
    """`preview_service._build` の**実際の**呼び出し回数を数える——実体はそのまま実行する
    forwarding wrapper（スタブ差し替えではない・RV2是正#b3②）。`scope_infer.safe_files` の
    呼び出し回数だけを見るテストは、「毎回グラフを再構築するが、たまたま `files` を使い回して
    歩かない」誤実装でも偽陽性で通ってしまう——`_build` 自体が呼ばれた回数を直接固定することで、
    グラフ構築が実際にスキップされたことを検証する。"""
    calls: list[str] = []
    real = ps._build

    def _counting(world, **kw):
        calls.append(world)
        return real(world, **kw)

    monkeypatch.setattr(ps, "_build", _counting)
    return calls


def test_preview_does_not_build_graph_and_graph_walks_exactly_once(monkeypatch, tmp_path):
    """`build_preview` は台帳が空のとき文書一覧のために 1 回だけ歩き、グラフは組み立てない。グラフは `build_preview_graph` が
    実 `world_graph.build_world` まで通して 1 回だけ歩く。"""
    _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("sig-a", "t1"))
    build_calls = _count_build(monkeypatch)
    calls = _count_safe_files(monkeypatch)

    out = ps.build_preview("wtest")

    assert calls["n"] == 1
    assert build_calls == []
    assert [d["name"] for d in out["documents"]] == ["a.md"]
    assert out["documents"][0]["importance"] == "高"
    assert out["counts"]["documents"] == 1
    assert out["importance_diagnostics"] == []
    assert out["entities"] == [] and out["relations"] == [] and out["issues"] == []
    assert "wtest" not in ps._GRAPH_VIEW_CACHE

    calls["n"] = 0
    graph = ps.build_preview_graph("wtest")

    assert calls["n"] == 1 and len(build_calls) == 1
    assert graph["graph_note"] is None
    assert "wtest" in ps._GRAPH_VIEW_CACHE               # グラフ部分は GRA-1 キャッシュへ公開された


def test_graph_cache_hit_skips_graph_build_and_walk(monkeypatch, tmp_path):
    """世代不変の 2 回目の `build_preview_graph` はグラフ構築（`_build`）も資料フォルダの走査もしない。
    `_build` 自体の呼び出し回数を forwarding wrapper で直接数える（`safe_files` の回数だけでは、毎回再構築するが
    `files` を使い回して歩かない誤実装を見逃すため）。"""
    _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("sig-a", "t1"))
    build_calls = _count_build(monkeypatch)
    calls = _count_safe_files(monkeypatch)

    r1 = ps.build_preview_graph("wtest")
    calls["n"] = 0
    r2 = ps.build_preview_graph("wtest")

    assert len(build_calls) == 1
    assert calls["n"] == 0
    assert r1["entities"] == r2["entities"]
    assert r1["issues"] == r2["issues"]


def test_graph_bundle_and_build_preview_graph_share_graph_cache(monkeypatch, tmp_path):
    """`_get_graph_bundle()` が先に world を構築していれば、同じ世代の `build_preview_graph()` はグラフを
    再構築しない（同じ `_GRAPH_VIEW_CACHE` を共有する証拠）。"""
    _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("sig-a", "t1"))
    build_calls = _count_build(monkeypatch)

    bundle = ps._get_graph_bundle("wtest")
    calls = _count_safe_files(monkeypatch)
    pv = ps.build_preview_graph("wtest")

    assert len(build_calls) == 1                # 合計で1回だけ
    assert calls["n"] == 0
    assert len(bundle["raw_nodes"]) == pv["counts"]["entities"]


def test_importance_edit_without_resync_is_reflected_next_call(monkeypatch, tmp_path):
    """台帳が空のとき、重要度（`_重要度.txt` の内容）は sig（世代）が変わらなくても次回呼び出しへ
    即座に反映される（文書一覧・重要度の解決は世代のキャッシュから読まない）。"""
    wd = _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("sig-a", "t1"))   # sig は両呼び出しで同一（sync していない想定）

    out1 = ps.build_preview("wtest")
    assert out1["documents"][0]["importance"] == "高"

    _write(wd / "_重要度.txt", "*.md: 低")                # sync を経ずに直接編集
    out2 = ps.build_preview("wtest")

    assert out2["documents"][0]["importance"] == "低"     # 次回 sync を待たず反映される


def test_documents_and_importance_are_never_served_from_a_cache(monkeypatch, tmp_path):
    """台帳が空のとき、`doc_ledger.preview_documents` は世代不変でも毎回実際に呼ばれる
    （一時的な障害結果が公開キャッシュへ焼き付いて次回まで残らない）。"""
    _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("sig-a", "t1"))
    calls: list[str] = []
    orig = ps.doc_ledger.preview_documents

    def _counting(*a, **kw):
        calls.append("call")
        return orig(*a, **kw)

    monkeypatch.setattr(ps.doc_ledger, "preview_documents", _counting)

    ps.build_preview("wtest")
    ps.build_preview("wtest")

    assert len(calls) == 2                     # 2回とも実際に呼ばれている（外側キャッシュに阻まれない）


def _count_build_world(monkeypatch):
    """グラフ構築の本体（`world_graph.build_world`）の呼び出し回数を数える（実体はそのまま実行する）。"""
    from sherpa.ingest import world_graph
    calls: list[str] = []
    real = world_graph.build_world

    def _counting(*a, **kw):
        calls.append("call")
        return real(*a, **kw)

    monkeypatch.setattr(world_graph, "build_world", _counting)
    return calls


def test_completed_world_builds_graph_and_reports_no_note(monkeypatch, tmp_path):
    """一度取り込みが完了した資料フォルダ（`sig` あり・実行中の run なし）はグラフを組み立てて要素・件数を返す。"""
    _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("sig-a", "t1"))
    build_world_calls = _count_build_world(monkeypatch)

    out = ps.build_preview_graph("wtest")

    assert len(build_world_calls) == 1
    assert out["graph_note"] is None
    assert out["counts"]["entities"] == len(out["entities"])      # 組み立てたグラフからの件数（記録からの None ではない）
    assert out["counts"]["deprecated"] == 0 and out["counts"]["hidden"] == 0


@pytest.mark.parametrize("status", ["queued", "extracting"])
def test_running_ingest_does_not_build_graph_and_counts_come_from_records(monkeypatch, tmp_path, status):
    """取り込み中（最新の run が queued／extracting）は `build_world` を呼ばない。一覧は文書一覧を出し、
    グラフは要素・関係を空にして理由を返す。件数は最後に完了した取り込みの記録から出す。"""
    _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("sig-a", "t1"))
    monkeypatch.setattr(ps.store, "get_latest_run_summary", lambda w, **kw: {"id": 7, "status": status})
    monkeypatch.setattr(ps.store, "get_latest_published_run_summary",
                        lambda w: {"published_snapshot": {"nodes": 120, "edges": 340}, "created_at": None})
    build_world_calls = _count_build_world(monkeypatch)

    out = ps.build_preview("wtest")
    graph = ps.build_preview_graph("wtest")

    assert build_world_calls == []
    assert out["graph_note"] and "取り込み中" in out["graph_note"]
    assert graph["graph_note"] == out["graph_note"]
    assert out["entities"] == [] and out["relations"] == [] and out["issues"] == []
    assert graph["entities"] == [] and graph["relations"] == [] and graph["issues"] == []
    assert out["counts"] == {"entities": 120, "relations": 340, "deprecated": None, "hidden": None, "documents": 1}
    assert graph["counts"]["entities"] == 120 and graph["counts"]["relations"] == 340
    assert [d["name"] for d in out["documents"]] == ["a.md"]
    assert "wtest" not in ps._GRAPH_VIEW_CACHE


def test_never_completed_world_does_not_build_graph(monkeypatch, tmp_path):
    """一度も取り込みが完了していない資料フォルダ（`sig` 空）は、実行中の run が無くても `build_world` を
    呼ばない。記録も無いので件数は不明（None）で、文書一覧は出す。"""
    _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("", None))
    monkeypatch.setattr(ps.store, "get_latest_published_run_summary", lambda w: None)
    build_world_calls = _count_build_world(monkeypatch)

    out = ps.build_preview("wtest")
    graph = ps.build_preview_graph("wtest")

    assert build_world_calls == []
    assert out["graph_note"] and "完了していない" in out["graph_note"]
    assert graph["graph_note"] == out["graph_note"]
    assert out["entities"] == [] and out["relations"] == []
    assert graph["entities"] == [] and graph["relations"] == []
    assert out["counts"] == {"entities": None, "relations": None, "deprecated": None, "hidden": None, "documents": 1}
    assert [d["name"] for d in out["documents"]] == ["a.md"]
    assert "wtest" not in ps._GRAPH_VIEW_CACHE


def test_unfinished_ingest_after_completed_one_says_last_ingest_not_finished(monkeypatch, tmp_path):
    """署名が空でも、完了した取り込みの記録があれば「一度も完了していない」とは言わず、前回の取り込みが最後まで終わっていない旨を返す（組み立てない動きは同じ・件数は記録から）。"""
    _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("", None))
    monkeypatch.setattr(ps.store, "get_latest_published_run_summary",
                        lambda w: {"published_snapshot": {"nodes": 120, "edges": 340}, "created_at": None})
    build_world_calls = _count_build_world(monkeypatch)

    out = ps.build_preview("wtest")
    graph = ps.build_preview_graph("wtest")

    assert build_world_calls == []
    assert graph["graph_note"] == ps._NOTE_LAST_UNFINISHED
    assert out["graph_note"] == ps._NOTE_LAST_UNFINISHED
    assert "最後まで終わっていない" in out["graph_note"] and "完了していない" not in out["graph_note"]
    assert out["counts"]["entities"] == 120 and out["counts"]["relations"] == 340


def test_run_registered_after_hold_check_does_not_build_graph(monkeypatch, tmp_path):
    """最初の判定のあと、キャッシュが無いままロックを取るまでの間に取り込みが queued になっても、`build_world` を呼ばず理由を返す。"""
    _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("sig-a", "t1"))
    latest = [None]
    monkeypatch.setattr(ps.store, "get_latest_run_summary", lambda w, **kw: latest[0])
    real_get = ps._get_graph_bundle

    def _run_appears_then_get(world):
        latest[0] = {"id": 8, "status": "queued"}
        return real_get(world)

    monkeypatch.setattr(ps, "_get_graph_bundle", _run_appears_then_get)
    build_world_calls = _count_build_world(monkeypatch)

    out = ps.build_preview_graph("wtest")

    assert build_world_calls == []
    assert out["graph_note"] == ps._NOTE_RUNNING
    assert out["entities"] == [] and out["relations"] == []
    assert "wtest" not in ps._GRAPH_VIEW_CACHE


def test_aba_sig_returns_to_same_value_forces_rebuild_for_preview_graph_too(monkeypatch, tmp_path):
    """ABA（GRA-1是正#1）は共有キャッシュのため `build_preview_graph` にも及ぶ: `A→""→A` の往復後は
    `synced_at` が変わっているため、sig だけ見た旧実装と違い必ず再構築する。"""
    _world(monkeypatch, tmp_path)
    status = {"v": _status("A", "t1")}
    monkeypatch.setattr(ps, "_current_world_status", lambda world, **kw: dict(status["v"]))

    ps.build_preview_graph("wtest")
    assert ps._GRAPH_VIEW_CACHE["wtest"]["synced_at"] == "t1"
    status["v"] = _status("A", "t2")          # 空文字を挟んだ後、sig は元に戻ったが世代は進んだ想定
    ps.build_preview_graph("wtest")

    assert ps._GRAPH_VIEW_CACHE["wtest"]["synced_at"] == "t2"


def test_delete_race_with_in_flight_preview_build_does_not_resurrect_cache(monkeypatch, tmp_path):
    """delete 競合（GRA-1是正RV2#3 と同型）: `build_preview_graph` 経由の in-flight 構築
    （`_GRAPH_VIEW_LOCK` 保持中）の最中に world が削除されても、delete の pop は構築の公開後に
    直列実行され、最終的にキャッシュへ残らない——`need_files=True` の新しい構築経路でも既存の
    ロック規律が保たれることの確認。"""
    _world(monkeypatch, tmp_path)
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
    _patch_status(monkeypatch, _status("sig-a", "t1"))

    entered_build = threading.Event()
    release_build = threading.Event()
    orig_build = ps._build

    def _slow_build(world, **kw):
        entered_build.set()
        release_build.wait(timeout=5)
        return orig_build(world, **kw)

    monkeypatch.setattr(ps, "_build", _slow_build)

    t_pv = threading.Thread(target=lambda: ps.build_preview_graph("wtest"))
    t_pv.start()
    assert entered_build.wait(timeout=5)

    t_del = threading.Thread(target=lambda: worlds_mod.delete("wtest", reflect=False))
    t_del.start()
    time.sleep(0.1)                            # delete が pop 側で lock 待ちになる時間を作る
    assert "wtest" not in ps._GRAPH_VIEW_CACHE  # build 側はまだ release 待ちで未公開

    release_build.set()
    t_pv.join(timeout=5)
    t_del.join(timeout=5)

    assert "wtest" not in ps._GRAPH_VIEW_CACHE  # 公開されても delete の pop が直列に後始末する
