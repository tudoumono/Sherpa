"""R3-S1/S2: worker の PG replace / Neo4j load 失敗時の記録＋自己修復（要 Postgres＋Neo4j）。

`sherpa.ingest.worker._run_locked` の PG replace（`store.replace_documents`）・Neo4j load
（`world_neo4j.load_world`）ステップを fault injection で失敗させ、次を固定する:
  (a) 例外は握りつぶされず呼び出し元へ伝播する（API 側可視・従来どおり）。
  (b) `ingest_runs` に **best-effort** で failed（`degraded=True`・`stage="pg_replace"`）が記録される
      （データ起因失敗＝`store.finish_ingest_run` 自体は生きている時）。
  (c) `worlds.last_sig` は**無効化**（`''`）される＝次回 sync が必ず「変更あり」と判定し続け、原因除去後の
      再同期で自己修復する（secRV 再RV・pre-invalidate 設計・2026-07-14。`_run_locked` は取り込み**開始前**
      にガード無しで `store.set_world_sig(world, "")` を呼ぶ＝fail-closed。旧実装は反映失敗のたび
      best-effort ヘルパで**後から**無効化していたが、ソース署名が不変のまま設定（アーム/VLM）だけ drift
      したケースでの恒久失敗（Codex finding #4）に加え、無効化自体が PG 断で失敗すると握り潰される穴も
      残っていた。先出し＋fail-closed でどちらも塞ぐ。`docs/proposals/2026-07-13-横断レビュー対応.md` R3-1
      の自己修復設計を維持しつつ強化）。
  (d) 接続断（記録自体も書けない想定）でも、記録失敗の二次例外に元の例外が隠されない。

Neo4j は community 版のため物理隔離できず実環境と共有（`tests/_world_registry.py` 参照）。
Postgres は `tests/conftest.py` が自動的に隔離 DB（`sherpa_test`）へ差し替える。
"""
from __future__ import annotations

import inspect
import pathlib
import shutil
import tempfile

import psycopg
import pytest
from _world_setup import driver

from sherpa import store, worlds
from sherpa.ingest import worker
from sherpa.store import documents as store_documents
from sherpa.store import ingest as store_ingest

W = "test_pg_replace_fault"


from _corpus_helpers import _mk


def _cleanup(root, drv):
    try:
        worlds.delete(W)
    except Exception:
        pass
    store.delete_world_row(W)
    with drv.session() as s:
        s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=W)
    drv.close()
    shutil.rmtree(root, ignore_errors=True)


def test_pg_replace_failure_data_caused_is_recorded_and_self_heals():
    """データ起因失敗（制約違反等）: failed(degraded,stage=pg_replace) が記録され、last_sig が
    無効化（''）され、原因除去後の次回 sync（自己修復）で正しい sig に確定する。"""
    root = tempfile.mkdtemp()
    _mk(root, "案件A", "FOOPROG")
    drv = driver()
    try:
        reg = worlds.register(W, str(pathlib.Path(root).resolve()))
        assert reg["status"] in ("auto_published", "auto_published_with_flags")
        sig0 = store.get_world(W)["last_sig"]
        assert sig0 is not None
        # 共有 dev DB では ingest_runs が蓄積し list_ingest_runs は 50 件で頭打ち＝件数差分は脆い
        # （Phase7 の教訓）。**新規 run を id 単調増加で識別**する（上限に依存しない・かつ本テストの
        # 失敗 run だと正確に帰属できる）。
        before_max_run_id = max((r["id"] for r in store.list_ingest_runs(W)), default=0)

        _mk(root, "案件A", "BARPROG")                          # 内容変更＝次回 sync が変更検知する

        orig_replace = store.replace_documents

        def _boom(world, rows):
            raise RuntimeError("duplicate key value violates unique constraint (fault injection)")

        store.replace_documents = _boom
        try:
            with pytest.raises(RuntimeError, match="fault injection"):
                worker.sync(W)
        finally:
            store.replace_documents = orig_replace

        assert store.get_world(W)["last_sig"] == ""             # last_sig 無効化（R3-S2・次回 sync を必ず再構築させる番兵）
        latest = store.list_ingest_runs(W)[0]                  # 最新 run
        assert latest["id"] > before_max_run_id               # このテストで新規追加された run（件数上限に非依存）
        assert latest["status"] == "failed"
        snap = latest["extraction_snapshot"]
        assert snap.get("degraded") is True and snap.get("stage") == "pg_replace"

        res2 = worker.sync(W)                                  # 原因除去後の次回 sync＝自己修復
        assert res2["changed"] is True
        assert res2["status"] in ("auto_published", "auto_published_with_flags")
        row2 = store.get_world(W)
        assert row2["last_sig"] != sig0
        names = {d["name"] for d in store.list_documents(W)}
        assert any("BARPROG" in n for n in names)
    finally:
        _cleanup(root, drv)


def test_pg_replace_connection_failure_propagates_original_error_and_self_heals():
    """接続断（PG 全断想定・`_connect` を例外化）: 記録自体も失敗するが、二次例外に元の接続エラーが
    隠されず伝播する。last_sig は無効化され（`store.set_world_sig` は `documents`/`ingest` とは別の
    `worlds` サブモジュールの `_connect` を使うため、この fault injection の対象外＝patch されない）、
    復旧後の次回 sync で自己修復する。

    RV（2026-07-14）: replace 側と failed-record 側で**別種の例外**を投げる。これにより
    (a) 元の replace 例外（OperationalError）が伝播すること＝失敗記録の best-effort guard が
    二次例外（RuntimeError）で元例外を握り潰していないこと、を実際に pin する
    （同種例外だと guard が無くても素通りで通ってしまい、try/except をピンできない）。

    ING-3: `store.ingest`（`_connect`）は run 開始時の `start_ingest_run`・逐次進捗の
    `update_ingest_run_progress`・完了時の `finish_ingest_run` すべてが使う。ここで壊したいのは
    **`finish_ingest_run`（failed 記録）だけ**——`start_ingest_run`（1回目の呼び出し）は素通し、
    それ以降（進捗更新・failed 記録）だけ故障させる（進捗更新は best-effort で握りつぶされる
    契約のため、実質 `finish_ingest_run` の失敗だけを検証することになる）。
    """
    root = tempfile.mkdtemp()
    _mk(root, "案件B", "BAZPROG")
    drv = driver()
    try:
        reg = worlds.register(W, str(pathlib.Path(root).resolve()))
        assert reg["status"] in ("auto_published", "auto_published_with_flags")
        sig0 = store.get_world(W)["last_sig"]

        _mk(root, "案件B", "QUXPROG")                           # 内容変更＝次回 sync が変更検知する

        def _boom_replace(*a, **k):                             # replace（元の失敗）＝OperationalError
            raise psycopg.OperationalError("replace-fault (primary)")

        orig_doc_connect = store_documents._connect
        orig_ingest_connect = store_ingest._connect

        def _boom_record(*a, **k):                              # failed 記録の書込＝**別種**の RuntimeError
            # `store.ingest` の `_connect` は `start_ingest_run`（run 開始時）／
            # `update_ingest_run_progress`（進捗・best-effort で握りつぶされる）／
            # `finish_ingest_run`（完了時の failed 記録）が共用する——呼び出し元の関数名で
            # `finish_ingest_run` だけを狙い撃ちする（他は素通しして本来の run を成立させる）。
            if inspect.stack()[1].function == "finish_ingest_run":
                raise RuntimeError("record-write-fault (secondary must NOT mask primary)")
            return orig_ingest_connect(*a, **k)

        store_documents._connect = _boom_replace
        store_ingest._connect = _boom_record                    # finish_ingest_run の best-effort 記録を別種例外で失敗させる
        try:
            # 伝播するのは **replace の OperationalError**（record の RuntimeError ではない）。
            with pytest.raises(psycopg.OperationalError, match="replace-fault"):
                worker.sync(W)
        finally:
            store_documents._connect = orig_doc_connect
            store_ingest._connect = orig_ingest_connect

        assert store.get_world(W)["last_sig"] == ""             # last_sig 無効化（R3-S2）

        res2 = worker.sync(W)                                   # 復旧後の次回 sync＝自己修復
        assert res2["changed"] is True
        assert res2["status"] in ("auto_published", "auto_published_with_flags")
        assert store.get_world(W)["last_sig"] != sig0
        names = {d["name"] for d in store.list_documents(W)}
        assert any("QUXPROG" in n for n in names)
    finally:
        _cleanup(root, drv)


def test_neo4j_load_failure_invalidates_last_sig_and_self_heals():
    """Codex finding #4: Neo4j load（`world_neo4j.load_world`）失敗経路も last_sig を無効化する。

    このパスは PG replace 失敗と違い**例外を伝播しない**（`_run_locked` 内で catch して
    `status="failed"` を返すだけ）。`worker.sync` はそれをそのまま返す（raise しない）。"""
    wid = W + "_neo4j"
    root = tempfile.mkdtemp()
    _mk(root, "案件N", "NEOFOO")
    drv = driver()
    try:
        reg = worlds.register(wid, str(pathlib.Path(root).resolve()))
        assert reg["status"] in ("auto_published", "auto_published_with_flags")
        sig0 = store.get_world(wid)["last_sig"]
        assert sig0

        _mk(root, "案件N", "NEOBAR")                            # 内容変更＝次回 sync が変更検知する

        from sherpa.ingest import world_neo4j
        orig_load = world_neo4j.load_world

        def _boom(*a, **k):
            raise RuntimeError("neo4j load fault injection")

        world_neo4j.load_world = _boom
        try:
            res = worker.sync(wid)                              # 例外は伝播しない＝status="failed" を返すだけ
        finally:
            world_neo4j.load_world = orig_load

        assert res["status"] == "failed"
        assert store.get_world(wid)["last_sig"] == ""           # last_sig 無効化（旧実装は非更新のまま＝sig0 残存）

        res2 = worker.sync(wid)                                 # 復旧後の次回 sync＝自己修復
        assert res2["changed"] is True
        assert res2["status"] in ("auto_published", "auto_published_with_flags")
        assert store.get_world(wid)["last_sig"] != ""
        names = {d["name"] for d in store.list_documents(wid)}
        assert any("NEOBAR" in n for n in names)
    finally:
        try:
            worlds.delete(wid)
        except Exception:
            pass
        store.delete_world_row(wid)
        with drv.session() as s:
            s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=wid)
        drv.close()
        shutil.rmtree(root, ignore_errors=True)


def test_unchanged_source_with_arm_drift_and_failed_run_forces_rebuild_next_sync():
    """Codex finding #4 の核心再現: ソース内容は**不変**のまま、アーム構成 drift（`.arms_sig`
    マーカー不一致）で sync が再ビルドに入り、その最中に反映が失敗した場合。

    `_build_derived`（`office_md.build_derived`）は失敗より**先に**派生 `.arms_sig` マーカーを
    現構成へ書き直してしまう。旧実装は失敗時に last_sig を触らないため、次回 sync は
    `prev == sig`（内容不変）**かつ** `_derived_stale() == False`（marker はもう drift していない）で
    `unchanged` 扱いになり、失敗した反映（Neo4j/PG が旧のまま）が恒久的に直らなかった。
    pre-invalidate 設計（secRV 再RV・2026-07-14）は `_run_locked` 冒頭（`_build_derived` 呼び出しより前）で
    内容の変化に依存せず last_sig を番兵（''）にするので、このケースでも次回 sync は必ず再構築する。"""
    wid = W + "_arm_drift"
    root = tempfile.mkdtemp()
    _mk(root, "案件C", "DRIFTPROG")
    drv = driver()
    try:
        reg = worlds.register(wid, str(pathlib.Path(root).resolve()))
        assert reg["status"] in ("auto_published", "auto_published_with_flags")
        sig0 = store.get_world(wid)["last_sig"]
        assert sig0

        marker = worlds.derived_md_dir(wid) / ".arms_sig"       # 派生ビルド時の有効アーム構成マーカー
        assert marker.is_file(), "register で派生MDビルドが marker を書くはず"
        marker.write_text("arms=bogus;pdf=none;legacy=none;md=none;vlm=none", encoding="utf-8")  # drift を演出

        orig_replace = store.replace_documents

        def _boom(world, rows):
            raise RuntimeError("fault injection (arm-drift rebuild fails)")

        store.replace_documents = _boom
        try:
            with pytest.raises(RuntimeError, match="fault injection"):
                worker.sync(wid)                                # ソース不変・drift のみ検知して再構築 → 失敗
        finally:
            store.replace_documents = orig_replace

        assert store.get_world(wid)["last_sig"] == ""           # last_sig 無効化（旧実装は sig0 のまま残り穴になった）
        # _build_derived は失敗前に marker を現構成へ更新済み＝drift はもう検知されない（バグの前提を確認）
        from sherpa.ingest import office_md
        assert office_md.arms_sig_drift(worlds.derived_md_dir(wid)) is False

        res2 = worker.sync(wid)                                 # ソースは相変わらず不変。旧実装だと unchanged のまま直らなかった
        assert res2["changed"] is True, res2
        assert res2["status"] in ("auto_published", "auto_published_with_flags")
        assert store.get_world(wid)["last_sig"] == sig0          # 内容が不変なので正しい sig（=sig0）に復帰
    finally:
        try:
            worlds.delete(wid)
        except Exception:
            pass
        store.delete_world_row(wid)
        with drv.session() as s:
            s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=wid)
        drv.close()
        shutil.rmtree(root, ignore_errors=True)


def test_run_locked_pre_invalidate_failure_is_fail_closed_before_build_derived():
    """secRV 再RV #4（fail-closed）: `_run_locked` 冒頭の pre-invalidate
    （`store.set_world_sig(world, "")`）が失敗（PG 断）したら、`_build_derived`
    （`office_md.build_derived`＝派生 `.arms_sig` マーカー先行更新）に**到達せず**例外がそのまま伝播する。

    PG に無効化すら書けない状態で反映を始めない＝まだ何も壊れていない時点で失敗が可視化される
    （registry 行の last_sig は旧のまま無傷）。"""
    wid = W + "_pre_invalidate_fail"
    root = tempfile.mkdtemp()
    _mk(root, "案件P", "PREFOO")
    drv = driver()
    try:
        reg = worlds.register(wid, str(pathlib.Path(root).resolve()))
        assert reg["status"] in ("auto_published", "auto_published_with_flags")
        sig0 = store.get_world(wid)["last_sig"]
        assert sig0

        _mk(root, "案件P", "PREBAR")                            # 内容変更＝次回 sync が変更検知する

        from sherpa.ingest import office_md
        orig_build_derived = office_md.build_derived
        build_calls = {"n": 0}

        def _track_build_derived(*a, **k):
            build_calls["n"] += 1
            return orig_build_derived(*a, **k)

        orig_set_sig = store.set_world_sig

        def _boom_set_sig(world, sig, manifest=None):
            raise psycopg.OperationalError("set_world_sig fault (pre-invalidate, PG断)")

        office_md.build_derived = _track_build_derived
        store.set_world_sig = _boom_set_sig
        try:
            with pytest.raises(psycopg.OperationalError, match="pre-invalidate"):
                worker.sync(wid)
        finally:
            office_md.build_derived = orig_build_derived
            store.set_world_sig = orig_set_sig

        assert build_calls["n"] == 0                            # _build_derived（マーカー更新）に未到達
        assert store.get_world(wid)["last_sig"] == sig0          # 無効化が失敗＝last_sig は旧のまま（何も壊れていない）

        res2 = worker.sync(wid)                                 # 復旧後の次回 sync＝内容変更を正常に検知・反映
        assert res2["changed"] is True
        assert res2["status"] in ("auto_published", "auto_published_with_flags")
        names = {d["name"] for d in store.list_documents(wid)}
        assert any("PREBAR" in n for n in names)
    finally:
        try:
            worlds.delete(wid)
        except Exception:
            pass
        store.delete_world_row(wid)
        with drv.session() as s:
            s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=wid)
        drv.close()
        shutil.rmtree(root, ignore_errors=True)


def test_run_direct_call_confirms_last_sig_on_success():
    """secRV 再RV（成功パス）: 直接 `worker.run()` を呼ぶ経路（`worlds.register()`/`worker.sync()` の
    自前 `set_world_sig` を経由しない最小経路）でも、成功時に last_sig が pre-scan 署名へ確定する
    （新挙動＝改善）。旧実装は `run()` 単体では last_sig を一切更新せず、呼び出し元
    （`sync()`/`register()`）の成功時上書きに依存していた（`routers/worlds.py` の direct `run()`/
    `rerun()` 呼び出しは従来 sig 非更新のままだった）。"""
    wid = W + "_direct_run_confirm"
    root = tempfile.mkdtemp()
    _mk(root, "案件R", "RUNFOO")
    drv = driver()
    try:
        store.upsert_world(wid, str(pathlib.Path(root).resolve()))   # register() を経由しない最小 bind
        assert store.get_world(wid)["last_sig"] is None              # まだ何も取り込んでいない

        r = worker.run(wid, created_by="admin")                      # 直接 run()（register/sync の自前 set は経由しない）
        assert r["status"] in ("auto_published", "auto_published_with_flags")

        row = store.get_world(wid)
        expected_sig = worker.world_signature(wid)
        assert row["last_sig"] == expected_sig
        assert row["last_sig"] != ""
    finally:
        try:
            worlds.delete(wid)
        except Exception:
            pass
        store.delete_world_row(wid)
        with drv.session() as s:
            s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=wid)
        drv.close()
        shutil.rmtree(root, ignore_errors=True)
