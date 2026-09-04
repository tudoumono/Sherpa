"""RV1是正（2026-09-01・finding #3/#4）: `preview_service.build_preview` の走査一本化＋
GRA-1 キャッシュ共有。

finding #3 是正: 旧テストは `_build` をスタブ化しており、`_build`（→`world_graph_service.
build_effective_world`→`world_graph.build_world`）自身が行う実際の全木走査を消して false green に
していた。ここでは `_build` をスタブせず、実際に `world_graph.build_world` まで通す小さな実
fixture world（tmp_path）を使い、`scope_infer.safe_files`（モジュール属性を直接 monkeypatch・
`world_graph.py`/`preview_service.py` どちらの import 経路からも同じオブジェクトを差し替える）の
呼び出し回数で「files が `_build` まで貫通し、合計1回に統合されたか」を検証する。

finding #4 是正: `build_preview` は `graph_view` と**同じ** `_GRAPH_VIEW_CACHE`（GRA-1）を
共有するが、それはグラフ部分（entities/relations/issues/counts.entities 等）だけ——文書一覧・
重要度解決・重要度診断は世代（`last_sig`）よりも細かい失効契約（制御ファイルの内容 hash・直近
run の DB 状態）を持つため、**一切キャッシュしない**（毎回フレッシュに計算する）。ここでは
`_重要度.txt` を sync を経ずに直接編集し、次回呼び出しへ即座に反映されることを実際に確認する
（外側キャッシュに阻まれて次回 sync まで固定されない、という finding #4 の直接反証）。
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


def test_cache_miss_walks_world_graph_and_documents_exactly_once(monkeypatch, tmp_path):
    """finding #3: `_build`（実 `world_graph.build_world`）をスタブせず、cache miss の
    `build_preview()` 1回で `scope_infer.safe_files` が実際に1回しか呼ばれないことを検証する
    （files が `_build` まで貫通している証拠。以前は `_build` 内部で1回・preview 側でもう1回の
    計2回だった）。"""
    _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("sig-a", "t1"))
    calls = _count_safe_files(monkeypatch)

    out = ps.build_preview("wtest")

    assert calls["n"] == 1
    assert [d["name"] for d in out["documents"]] == ["a.md"]
    assert out["documents"][0]["importance"] == "高"
    assert out["counts"]["documents"] == 1
    assert out["importance_diagnostics"] == []
    assert "wtest" in ps._GRAPH_VIEW_CACHE               # グラフ部分は GRA-1 キャッシュへ公開された


def test_cache_hit_skips_graph_build_but_still_walks_once_for_documents(monkeypatch, tmp_path):
    """世代不変の2回目呼び出しはグラフ構築（`_build`）をスキップするが、文書一覧・重要度・診断は
    キャッシュしない設計（finding #4）のため、そのぶんだけ `scope_infer.safe_files` を1回歩く
    （0回にはしない）。RV2是正#b3②: `_build` 自体の呼び出し回数を forwarding wrapper で直接
    数える——`safe_files` の呼び出し回数だけを見ると、「毎回グラフを再構築するが `files` を
    使い回して歩かない」誤実装でも偽陽性で通ってしまう。"""
    _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("sig-a", "t1"))
    build_calls = _count_build(monkeypatch)
    calls = _count_safe_files(monkeypatch)

    r1 = ps.build_preview("wtest")
    calls["n"] = 0
    r2 = ps.build_preview("wtest")

    assert len(build_calls) == 1                # グラフ構築（_build）自体は1回だけ＝2回目は再構築しない
    assert calls["n"] == 1                       # グラフは再構築しないが文書一覧のため1回歩く
    assert r1["entities"] == r2["entities"]
    assert r1["issues"] == r2["issues"]


def test_graph_view_and_build_preview_share_graph_cache(monkeypatch, tmp_path):
    """`graph_view()` が先に world を構築していれば、同じ世代の `build_preview()` はグラフを
    再構築しない（同じ `_GRAPH_VIEW_CACHE` を共有する証拠・finding #4 是正の核）。RV2是正#b3②:
    `_build` の呼び出し回数を forwarding wrapper で直接数え、`graph_view()`+`build_preview()`
    2回の合計で1回だけ（＝2回目の `build_preview()` はグラフを再構築しない）ことを固定する。"""
    _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("sig-a", "t1"))
    build_calls = _count_build(monkeypatch)

    gv = ps.graph_view("wtest")
    calls = _count_safe_files(monkeypatch)
    pv = ps.build_preview("wtest")

    assert len(build_calls) == 1                # graph_view+build_preview 合計で1回だけ
    assert calls["n"] == 1                       # 文書一覧のためだけの1回（グラフは共有キャッシュから）
    assert gv["counts"]["entities"] == pv["counts"]["entities"]


def test_importance_edit_without_resync_is_reflected_next_call(monkeypatch, tmp_path):
    """finding #4: 重要度（`_重要度.txt` の内容）は sig（世代）が変わらなくても次回呼び出しへ
    即座に反映される——外側の `_GRAPH_VIEW_CACHE` を共有していても、文書一覧・重要度はそこから
    一切読まない（毎回フレッシュに計算する）ことの直接証明。"""
    wd = _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("sig-a", "t1"))   # sig は両呼び出しで同一（sync していない想定）

    out1 = ps.build_preview("wtest")
    assert out1["documents"][0]["importance"] == "高"

    _write(wd / "_重要度.txt", "*.md: 低")                # sync を経ずに直接編集
    out2 = ps.build_preview("wtest")

    assert out2["documents"][0]["importance"] == "低"     # 次回 sync を待たず反映される
    assert "wtest" in ps._GRAPH_VIEW_CACHE                 # グラフ部分は依然キャッシュ済み（再構築なし）


def test_documents_and_importance_are_never_served_from_a_cache(monkeypatch, tmp_path):
    """finding #4: `doc_ledger.preview_documents` は世代不変でも毎回実際に呼ばれる（外側の
    bundle キャッシュから読まれることはない）——一時的な障害結果が公開キャッシュへ焼き付いて
    次回まで残る、という懸念への直接証明。"""
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


def test_unregistered_world_not_cached(monkeypatch, tmp_path):
    """未登録 world（`sig` 空・dev fixture 等）はキャッシュキーを持てないため毎回計算する。"""
    _world(monkeypatch, tmp_path)
    _patch_status(monkeypatch, _status("", None))

    ps.build_preview("wtest")

    assert "wtest" not in ps._GRAPH_VIEW_CACHE


def test_aba_sig_returns_to_same_value_forces_rebuild_for_preview_too(monkeypatch, tmp_path):
    """ABA（GRA-1是正#1）は共有キャッシュのため `build_preview` にも及ぶ: `A→""→A` の往復後は
    `synced_at` が変わっているため、sig だけ見た旧実装と違い必ず再構築する。"""
    _world(monkeypatch, tmp_path)
    status = {"v": _status("A", "t1")}
    monkeypatch.setattr(ps, "_current_world_status", lambda world, **kw: dict(status["v"]))

    ps.build_preview("wtest")
    assert ps._GRAPH_VIEW_CACHE["wtest"]["synced_at"] == "t1"
    status["v"] = _status("A", "t2")          # 空文字を挟んだ後、sig は元に戻ったが世代は進んだ想定
    ps.build_preview("wtest")

    assert ps._GRAPH_VIEW_CACHE["wtest"]["synced_at"] == "t2"


def test_delete_race_with_in_flight_preview_build_does_not_resurrect_cache(monkeypatch, tmp_path):
    """delete 競合（GRA-1是正RV2#3 と同型）: `build_preview` 経由の in-flight 構築
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

    t_pv = threading.Thread(target=lambda: ps.build_preview("wtest"))
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
