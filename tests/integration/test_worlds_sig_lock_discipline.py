"""secRV 再RV round-2（2026-07-14）: last_sig への書き込みは全て world_lock 保持中に行う不変条件（要 PG+Neo4j）。

Codex 再RV（ラウンド2）が検出した並行系の穴のうち、実 DB/Neo4j での相互作用が要る3件を固定する:

  (G) `worker.sync()` 成功時、`store.set_world_sig` の呼び出しは `run()`（内部の `_run_locked` が
      world_lock 保持中に行う pre-invalidate ''＋確定 sig の2回）だけで、`run()` 復帰後（＝lock 解放後）
      に `sync()` 自身のフレームから追加の呼び出しが無いこと（HIGH-1: 後置き書き込みは他プロセスの
      pre-invalidate/削除を有効署名で復活させる番兵復活の穴になる）。
  (H) `worlds.register()` も同型（旧実装は取り込み前スキャン＋`run()` 復帰後のロック外確定を持っていた）。
  (I) `sync()` の「明細未保存だが署名一致」バックフィルは lock 取得後に行に再読して確認する。
      再読と実際の書き込みの間に他 writer が割り込んで署名を無効化していた場合はバックフィルしない
      （上書きしない）。競合が無ければ通常どおりバックフィルする。

Codex 再RV round-3（2026-07-14）が検出した2件（rebind の ABA 恒久不整合＋register のライフサイクル
未ロック）を固定する:

  (M) `worlds.rebind()` 成功時、`store.set_world_sig` の呼び出しは `_run_locked` 内部の2回
      （pre-invalidate ''＋確定 sig）だけで、rebind 自身のフレームからの追加呼び出しが無いこと。
      旧実装は rebind 呼び出し**前**に外側で1回スキャンし、`_run_locked` 復帰後にその外側の
      （場合によっては陳腐化した）署名で上書きしていた（ABA 問題）。rebind 実行中にソースへファイルを
      増やし直後に戻す（ABA の A→B→A）ことで、確定された last_sig が `_run_locked` 内部スキャン（B）
      であって外側の陳腐化した値でないことを固定し、その後 disk が A へ戻った状態で `sync()` が
      正しく `changed=True` を返す（旧実装なら last_sig=A=disk と一致して `unchanged` に化ける状況）
      ことまで pin する。
  (N) `worlds.rebind(..., reflect=False)`（staging のみ）は完了後も `last_sig` が pre-invalidate の
      `''` のまま（MED-2 の再導入防止・確定は `reflect=True` の成功パスのみ）。
  (O) `worlds.register()` の失敗時 cleanup（`store.delete_world_row`）は world_lock 解放**前**に
      起きる。旧実装は行作成〜cleanup が lock の外にあり、失敗 register の cleanup が並行 sync/rebind
      の run と交錯し得た（HIGH-B）。
"""
from __future__ import annotations

import contextlib
import pathlib
import shutil
import tempfile

import pytest
from _world_setup import driver

from sherpa import store, worlds
from sherpa.ingest import worker

W = "test_sig_lock_discipline"


from _corpus_helpers import _mk


def _install_sig_confirm_spies(calls, doc_counts):
    """`store.set_world_sig`（pre-invalidate 専用）と `store.finish_ingest_run_and_confirm_world`
    （ING-3・成功確定は run 完了と同一トランザクションへ統合済み）の両方を計装し、
    `calls`/`doc_counts` へ同じ形（sig／doc_count）で記録する（元の関数はそのまま呼ぶ・
    副作用は変えない）。戻り値は元へ戻す関数（呼び出し元は必ず `finally` で呼ぶこと）。
    """
    orig_set_sig = store.set_world_sig
    orig_confirm = store.finish_ingest_run_and_confirm_world

    def _spy_set_sig(world, sig, manifest=None, doc_count=None, scan_report=None):
        calls.append(sig)
        doc_counts.append(doc_count)
        return orig_set_sig(world, sig, manifest=manifest, doc_count=doc_count, scan_report=scan_report)

    def _spy_confirm(run_id, world, *, status, extraction_snapshot=None, published_snapshot=None,
                     source_doc_ids=None, sig=None, manifest=None, doc_count=None, scan_report=None):
        if sig is not None:
            calls.append(sig)
            doc_counts.append(doc_count)
        return orig_confirm(run_id, world, status=status, extraction_snapshot=extraction_snapshot,
                            published_snapshot=published_snapshot, source_doc_ids=source_doc_ids,
                            sig=sig, manifest=manifest, doc_count=doc_count, scan_report=scan_report)

    store.set_world_sig = _spy_set_sig
    store.finish_ingest_run_and_confirm_world = _spy_confirm

    def _restore():
        store.set_world_sig = orig_set_sig
        store.finish_ingest_run_and_confirm_world = orig_confirm
    return _restore


def _cleanup(wid, roots, drv):
    try:
        worlds.delete(wid)
    except Exception:
        pass
    store.delete_world_row(wid)
    with drv.session() as s:
        s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=wid)
    drv.close()
    for r in roots:
        shutil.rmtree(r, ignore_errors=True)


def test_sync_success_set_world_sig_calls_only_from_run_no_post_write():
    """(G) sync 復帰後にロック外からの追加 set_world_sig が無いこと（呼び出し記録で pin）。"""
    a = tempfile.mkdtemp()
    _mk(a, "案件X", "SYNCG1")
    wid = W + "_sync_no_postwrite"
    drv = driver()
    try:
        worlds.register(wid, str(pathlib.Path(a).resolve()))
        _mk(a, "案件X", "SYNCG2")                              # 内容変更＝sync が変更検知して run() を呼ぶ

        calls = []
        doc_counts = []
        restore = _install_sig_confirm_spies(calls, doc_counts)
        try:
            res = worker.sync(wid)
        finally:
            restore()

        assert res["changed"] is True
        assert res["status"] in ("auto_published", "auto_published_with_flags")
        # pre-invalidate('')→確定(sig) の2回だけ（sync フレームからの追加呼び出しが無い）
        assert calls == ["", store.get_world(wid)["last_sig"]]
        assert calls[1] != ""
        # 確定呼び出しの doc_count は SYNCG1/SYNCG2 の .cbl 2件（doctype対応原本）。
        assert doc_counts == [None, 2]
        assert store.get_world(wid)["last_doc_count"] == 2
    finally:
        _cleanup(wid, [a], drv)


def test_register_success_set_world_sig_calls_only_from_run_no_post_write():
    """(H) register も同型: run() 内部の2回だけで、register 自身のフレームからの追加呼び出しが無い。"""
    a = tempfile.mkdtemp()
    _mk(a, "案件X", "REGH1")
    wid = W + "_register_no_postwrite"
    drv = driver()
    try:
        calls = []
        doc_counts = []
        restore = _install_sig_confirm_spies(calls, doc_counts)
        try:
            res = worlds.register(wid, str(pathlib.Path(a).resolve()))
        finally:
            restore()

        assert res["status"] in ("auto_published", "auto_published_with_flags")
        assert calls == ["", store.get_world(wid)["last_sig"]]
        assert calls[1] != ""
        # REGH1.cbl 1件（doctype対応原本）。
        assert doc_counts == [None, 1]
        assert store.get_world(wid)["last_doc_count"] == 1
    finally:
        _cleanup(wid, [a], drv)


def test_sync_backfill_applies_when_uncontended():
    """(I・正常時) 明細未保存＋署名一致で競合が無ければ、lock 内の再読を経て通常どおりバックフィルされる。"""
    a = tempfile.mkdtemp()
    _mk(a, "案件X", "BFI1")
    wid = W + "_backfill_ok"
    drv = driver()
    try:
        worlds.register(wid, str(pathlib.Path(a).resolve()))
        sig0 = store.get_world(wid)["last_sig"]
        assert sig0
        with store._connect() as cc:                          # 明細だけ未保存（旧データ相当）を模擬
            cc.execute("UPDATE worlds SET last_manifest=NULL WHERE world_id=%s", (wid,))
        assert store.get_world(wid)["last_manifest"] is None

        res = worker.sync(wid)

        assert res["changed"] is False
        row = store.get_world(wid)
        assert row["last_sig"] == sig0
        assert row["last_manifest"]                            # バックフィルされた
    finally:
        _cleanup(wid, [a], drv)


def test_sync_backfill_skips_when_concurrent_invalidate_races_lock_reread():
    """(I・競合時) lock 取得〜再読の間に他 writer が無効化（''）していたら、バックフィルは行われず
    署名を上書きしない（TOCTOU 対策）。"""
    a = tempfile.mkdtemp()
    _mk(a, "案件X", "BFI2")
    wid = W + "_backfill_race"
    drv = driver()
    try:
        worlds.register(wid, str(pathlib.Path(a).resolve()))
        with store._connect() as cc:                          # 明細だけ未保存（旧データ相当）を模擬
            cc.execute("UPDATE worlds SET last_manifest=NULL WHERE world_id=%s", (wid,))
        assert store.get_world(wid)["last_manifest"] is None

        orig_lock = store.world_lock

        @contextlib.contextmanager
        def _racing_lock(world_id):
            with orig_lock(world_id):
                store.set_world_sig(world_id, "")              # lock 取得直後に別 writer が無効化したことを模擬
                yield
        store.world_lock = _racing_lock
        try:
            res = worker.sync(wid)                              # 無変更のはずが、再読で競合を検出してバックフィル skip
        finally:
            store.world_lock = orig_lock

        assert res["changed"] is False
        row = store.get_world(wid)
        assert row["last_sig"] == ""                             # 上書きしていない＝他 writer の無効化のまま
        assert row["last_manifest"] is None                      # バックフィルされていない
    finally:
        _cleanup(wid, [a], drv)


def test_rebind_aba_final_sig_matches_run_locked_scan_and_sync_self_heals():
    """(M) secRV round-3 HIGH-A pin: rebind 成功時の `set_world_sig` 呼び出しは `_run_locked` 内部の
    2回（pre-invalidate ''＋確定 sig）だけで、rebind フレームからの追加確定が無い。

    ABA を模擬する: rebind 実行中（`_run_locked` 呼び出しの直前〜直後）だけ新 root にファイルを増やし
    （状態 B）、`_run_locked` 復帰直後に消して元の内容（状態 A）へ戻す。旧実装は rebind 呼び出し**前**に
    外側で1回スキャン（状態 A の署名）し、`_run_locked` 復帰後にその陳腐化した外側署名で上書きしていた
    ため、この状況では「last_sig=A（disk と一致）だがグラフは B のまま」という恒久不整合になり、続く
    `sync()` が誤って `unchanged` と判定していた。修正後は last_sig が `_run_locked` 内部スキャン（B）
    に一致するため、disk が A へ戻った後の `sync()` は正しく `changed=True` を返し、グラフを A へ
    自己修復する。
    """
    a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
    _mk(a, "案件X", "ABAFOO")
    _mk(b, "案件Y", "ABABAR")
    wid = W + "_rebind_aba"
    drv = driver()
    try:
        worlds.register(wid, str(pathlib.Path(a).resolve()))

        orig_run_locked = worker._run_locked
        extra = pathlib.Path(b) / "案件Y" / "03_開発" / "01_ソース" / "TRANSIENT.cbl"

        calls = []
        doc_counts = []
        restore = _install_sig_confirm_spies(calls, doc_counts)

        def _wrapped_run_locked(*aa, **kk):
            extra.parent.mkdir(parents=True, exist_ok=True)   # ABA の "B"（rebind 実行中だけ増える）
            extra.write_text(
                "       IDENTIFICATION DIVISION.\n"
                "       PROGRAM-ID. TRANSIENT.\n"
                "       PROCEDURE DIVISION.\n           STOP RUN.\n", encoding="utf-8")
            try:
                return orig_run_locked(*aa, **kk)
            finally:
                extra.unlink(missing_ok=True)                  # rebind 完了直後に "A" へ戻す

        worker._run_locked = _wrapped_run_locked
        try:
            worlds.rebind(wid, str(pathlib.Path(b).resolve()))
        finally:
            worker._run_locked = orig_run_locked
            restore()

        assert calls == ["", store.get_world(wid)["last_sig"]]   # rebind フレームからの追加確定が無い
        final_sig = calls[-1]
        assert final_sig != ""
        # 確定時点（_run_locked 内部スキャン＝状態B）は ABABAR.cbl+TRANSIENT.cbl の2件。
        assert doc_counts == [None, 2]
        assert store.get_world(wid)["last_doc_count"] == 2

        # last_sig は "B"（TRANSIENT を含む・_run_locked 内部スキャン）に一致する。旧実装なら外側の
        # "A" スキャン（TRANSIENT 無し）で上書きされ、この時点で disk（既に A へ戻っている）と一致
        # してしまっていた。
        current_disk_sig, _ = worker.world_state(wid)
        assert final_sig != current_disk_sig

        r = worker.sync(wid)                                     # disk=A・last_sig=B なので必ず changed
        assert r["changed"] is True
        assert r["status"] in ("auto_published", "auto_published_with_flags")
        assert store.get_world(wid)["last_sig"] == current_disk_sig   # 自己修復で A の署名へ収束
    finally:
        _cleanup(wid, [a, b], drv)


def test_rebind_reflect_false_leaves_sig_uncommitted():
    """(N) secRV round-3: `rebind(reflect=False)`（staging のみ）は完了後も last_sig を確定しない
    （MED-2 の再導入防止・`_run_locked` の成功パスは `reflect=True` の時だけ確定する）。"""
    a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
    _mk(a, "案件X", "STGFOO")
    _mk(b, "案件Y", "STGBAR")
    wid = W + "_rebind_staging"
    drv = driver()
    try:
        worlds.register(wid, str(pathlib.Path(a).resolve()))
        assert store.get_world(wid)["last_sig"]

        worlds.rebind(wid, str(pathlib.Path(b).resolve()), reflect=False)

        row = store.get_world(wid)
        assert row["last_sig"] == ""                              # 確定しない（pre-invalidate のまま）
        assert worlds.world_dir(wid) == pathlib.Path(b).resolve()  # bind は新 root へ切り替わる
    finally:
        _cleanup(wid, [a, b], drv)


def test_register_failure_cleanup_row_delete_happens_inside_lock():
    """(O) secRV round-3 HIGH-B pin: register が失敗した時、行削除（`store.delete_world_row`）は
    world_lock 解放**前**に起きる（呼び出し順を記録して固定）。旧実装は行作成（`upsert_world`）〜
    失敗時 cleanup が lock の外にあり、失敗 register の cleanup が並行 sync/rebind の run と
    交錯し得た（並行 sync 側は自分の `set_world_sig` を UPDATE 0行のまま成功扱いしてしまう）。"""
    a = tempfile.mkdtemp()
    _mk(a, "案件X", "LOCKORDFOO")
    wid = W + "_register_lock_order"
    drv = driver()
    try:
        orig_run_locked = worker._run_locked
        orig_world_lock = store.world_lock
        orig_delete_row = store.delete_world_row

        events = []

        @contextlib.contextmanager
        def _traced_lock(world_id):
            with orig_world_lock(world_id):
                events.append("lock_acquired")
                try:
                    yield
                finally:
                    events.append("lock_released")

        def _fail_run_locked(*aa, **kk):                          # 実際の取り込みは走らせず即 failed
            return {"world": aa[0] if aa else kk.get("world"), "status": "failed",
                    "ledger": 0, "nodes": 0, "edges": 0, "flags": [{"reason": "boom"}], "run": None}

        def _traced_delete(world_id):
            events.append("delete_world_row")
            return orig_delete_row(world_id)

        store.world_lock = _traced_lock
        worker._run_locked = _fail_run_locked
        store.delete_world_row = _traced_delete
        try:
            with pytest.raises(RuntimeError, match="register 失敗"):
                worlds.register(wid, str(pathlib.Path(a).resolve()))
        finally:
            store.world_lock = orig_world_lock
            worker._run_locked = orig_run_locked
            store.delete_world_row = orig_delete_row

        assert events == ["lock_acquired", "delete_world_row", "lock_released"]
        assert store.get_world(wid) is None                       # 失敗 cleanup で行は削除済み
    finally:
        _cleanup(wid, [a], drv)
