"""`_run_locked` の last_sig 書き込み契約（secRV 再RV round-2・2026-07-14）を DB/Neo4j 無しで pin する。

`tests/unit/test_ingest_worker_flags.py` と同じ流儀（`worker`/`store` 周辺を monkeypatch し外部
サービス非依存）。ここでは呼び出し元（sync/register）を介さず `worker.run`/`_run_locked` 単体の
契約を検証する:

  (J) `world_state()` が `(None, None)`（world 未解決）を返すと、`_build_derived`/`build_world_graph`
      を一切呼ばず（mutation 前に）即 `status=failed` を返し、`store.set_world_sig` は一度も呼ばれない
      （HIGH-2: TOCTOU 窓での番兵無し進行を防ぐ）。
  (K) 成功パスの署名確定（正しい sig への書き戻し）は `ingest_runs` 記録（`store.add_ingest_run`）が
      **成功した後**に行う（MED-1）。記録が失敗すると例外が伝播し、確定は行われない
      （pre-invalidate の `''` だけが残る＝次回 sync が再試行して記録漏れを拾い直す）。
  (L) `reflect=False`（staging）は成功しても署名を確定しない（MED-2）。pre-invalidate の `''` のまま
      残り、以後の `sync(reflect=True)` が必ず本反映で再構築する。
  (M) `_build_derived()` が `error` を返す（派生生成が公開Gateで拒否された不完全な世代）場合、
      graph/台帳/ES 反映へ進まず即 `status=failed` で終了し、正しい sig への確定は行わない
      （pre-invalidate の `''` のまま＝次回 sync が `prev != sig` で必ず再試行する）。
  (N) `build_world_graph()` が blocked flag（受理済みコード文書の実読込失敗等）を返す場合も (M) と
      同様に graph/台帳反映へ進まず即 `status=failed`・sig 未確定のまま終了する。
"""
from __future__ import annotations

import contextlib

import pytest

from sherpa import corpus_docs, es_index, reconcile, store
from sherpa.ingest import world_neo4j, worker


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch):
    """`worker.run` を DB/Neo4j 無しで駆動できるよう周辺を差し替える（happy path 既定）。"""
    monkeypatch.setattr(worker, "world_state", lambda world: ("sig", {"a": [1, 2, 3]}))
    monkeypatch.setattr(worker, "build_world_graph", lambda world: ([], [], []))
    monkeypatch.setattr(worker, "_build_derived",
                        lambda world, **_kw: {"converted": 0, "failed": 0, "unsupported": 0, "by_ext": {}})
    monkeypatch.setattr(worker, "_ledger_rows", lambda world, *, sig: [])
    monkeypatch.setattr(worker, "world_signature", lambda world: "sig")
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})
    monkeypatch.setattr(world_neo4j, "load_world", lambda nodes, edges, world, uri, user, pw: (0, 0))
    monkeypatch.setattr(es_index, "index_world",
                        lambda world, content_sig=None: {"available": True, "indexed": 0, "chunks": 0})
    monkeypatch.setattr(reconcile, "reconcile_derivatives", lambda reflect=True: None)

    @contextlib.contextmanager
    def _noop_lock(world_id):
        yield
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(store, "replace_documents", lambda world, rows: 0)

    captured = {"set_world_sig_calls": [], "set_world_sig_doc_counts": []}

    def _fake_set_world_sig(world, sig, manifest=None, doc_count=None, scan_report=None):
        captured["set_world_sig_calls"].append(sig)
        captured["set_world_sig_doc_counts"].append(doc_count)
    monkeypatch.setattr(store, "set_world_sig", _fake_set_world_sig)

    # ING-2: 成功パス確定に続けて scan_report をキャッシュする（`worker._run_locked` 参照）。
    # `corpus_docs.scan_report` は内部で `worlds.world_dir`（DB 登録行を読む）を呼ぶため、実行すると
    # このテストの架空 world "w" に対して不要な DB 接続を試みてしまう——両方スタブして DB/ネットワーク
    # 不要という本ファイルの前提を保つ。
    monkeypatch.setattr(corpus_docs, "scan_report", lambda world: {})
    monkeypatch.setattr(store, "set_scan_report", lambda world, report: None)

    # ING-3: 開始時 INSERT（`start_ingest_run`）→完了時 UPDATE（`finish_ingest_run`）の2段構成
    # （`add_ingest_run` の単発 INSERT を置換）。`downgrade_orphaned_extracting_runs`/
    # `update_ingest_run_progress` も新規の DB 呼び出しなので合わせてスタブする。
    monkeypatch.setattr(store, "downgrade_orphaned_extracting_runs", lambda world=None: [])
    monkeypatch.setattr(store, "update_ingest_run_progress", lambda run_id, progress: None)

    def _fake_start_ingest_run(world, **kw):
        return {"id": 1, "version": world, "layer": "version", "status": "extracting", "created_at": None}
    monkeypatch.setattr(store, "start_ingest_run", _fake_start_ingest_run)

    def _fake_finish_ingest_run(run_id, **kw):
        return {"id": run_id, **kw}
    monkeypatch.setattr(store, "finish_ingest_run", _fake_finish_ingest_run)

    # ING-3: 成功パスの sig/manifest/doc_count/scan_report 確定は run 完了と同一トランザクション
    # （`finish_ingest_run_and_confirm_world`）へ移った——pre-invalidate（`sig=""`）は変わらず
    # `store.set_world_sig` を直接呼ぶが、最終確定（`sig` が実値）はこちらを経由する。テストの
    # 記録先（`captured["set_world_sig_calls"]`）は両方の呼び出し元から同じ形で集める。
    def _fake_finish_and_confirm(run_id, world, *, status, extraction_snapshot=None,
                                 published_snapshot=None, source_doc_ids=None,
                                 sig=None, manifest=None, doc_count=None, scan_report=None):
        if sig is not None:
            captured["set_world_sig_calls"].append(sig)
            captured["set_world_sig_doc_counts"].append(doc_count)
        return {"id": run_id, "status": status}
    monkeypatch.setattr(store, "finish_ingest_run_and_confirm_world", _fake_finish_and_confirm)
    return captured


def test_sig_none_fails_immediately_without_mutation(monkeypatch, _stub_pipeline):
    """(J) world 未解決＝ mutation 前に即 failed。set_world_sig は一度も呼ばれない。"""
    monkeypatch.setattr(worker, "world_state", lambda world: (None, None))
    build_calls = {"derived": 0, "graph": 0}
    monkeypatch.setattr(worker, "_build_derived",
                        lambda world, **_kw: build_calls.__setitem__("derived", build_calls["derived"] + 1))
    monkeypatch.setattr(worker, "build_world_graph",
                        lambda world: build_calls.__setitem__("graph", build_calls["graph"] + 1))

    res = worker.run("w")

    assert res["status"] == "failed"
    assert res["flags"] == [{"doc": None, "action": "blocked", "reason": "world_unresolved"}]
    assert build_calls == {"derived": 0, "graph": 0}          # 反映/派生生成に一切触れていない
    assert _stub_pipeline["set_world_sig_calls"] == []        # 無効化も確定も呼ばれない（書けるかも不明・触らない）


def test_success_path_confirms_sig_only_after_record_succeeds(_stub_pipeline):
    """(K・happy path) 正常時は pre-invalidate('')→記録→確定(sig) の順で `set_world_sig` が2回呼ばれる。

    確定呼び出しの `doc_count` は `world_state` スタブの manifest
    `{"a": [1, 2, 3]}` から doctype 対応原本件数を数えた値（"a" は拡張子無し＝対象外＝0）。
    """
    res = worker.run("w")

    assert res["status"] in ("auto_published", "auto_published_with_flags")
    assert _stub_pipeline["set_world_sig_calls"] == ["", "sig"]
    assert _stub_pipeline["set_world_sig_doc_counts"] == [None, 0]


def test_record_failure_leaves_sig_unconfirmed(monkeypatch, _stub_pipeline):
    """(K) `ingest_runs` 記録（成功パスは `finish_ingest_run_and_confirm_world`）が失敗すると
    例外が伝播し、正しい sig への確定は行われない（MED-1・記録と sig 確定は同一トランザクション
    のため、この関数の失敗は sig 確定自体も道連れにする）。"""
    def _boom_finish_and_confirm(run_id, world, **kw):
        raise RuntimeError("add_ingest_run fault (record failure)")
    monkeypatch.setattr(store, "finish_ingest_run_and_confirm_world", _boom_finish_and_confirm)

    with pytest.raises(RuntimeError, match="record failure"):
        worker.run("w")

    # pre-invalidate（''）だけが書かれ、記録失敗のため確定（2回目・sig）は呼ばれていない
    assert _stub_pipeline["set_world_sig_calls"] == [""]


def test_pg_replace_failure_marks_exception_as_already_recorded(monkeypatch, _stub_pipeline):
    """台帳更新（`store.replace_documents`）失敗時は理由付きで `ingest_runs` に記録した
    うえで、元の例外に `_sherpa_ingest_run_recorded` 属性を付けて re-raise する——
    呼び出し元（`routers/worlds.py::_run_worker_or_503` 等）が同じ失敗を汎用な理由で
    二重記録しないための目印（1回の取り込みで ingest_runs に2件残る事故を防ぐ）。

    run 自体は `failed` のままでも、`published_snapshot` には Neo4j へ実際に反映済みの
    nodes/edges を記録する——台帳 replace の失敗は Neo4j 側の巻き戻しを伴わないため、
    `get_latest_published_run_summary` が返す「今実際に Neo4j にある内容」を、この run が新世代の
    まま止めてしまわないようにする。
    """
    monkeypatch.setattr(world_neo4j, "load_world", lambda nodes, edges, world, uri, user, pw: (4, 6))

    def _boom_replace(world, rows):
        raise RuntimeError("pg_replace fault")
    monkeypatch.setattr(store, "replace_documents", _boom_replace)

    recorded = []

    def _fake_finish_ingest_run(run_id, **kw):
        recorded.append(kw)
        return {"id": run_id, **kw}
    monkeypatch.setattr(store, "finish_ingest_run", _fake_finish_ingest_run)

    with pytest.raises(RuntimeError, match="pg_replace fault") as ei:
        worker.run("w")

    assert getattr(ei.value, "_sherpa_ingest_run_recorded", False) is True
    assert recorded and recorded[0]["status"] == "failed"
    assert recorded[0]["extraction_snapshot"]["stage"] == "pg_replace"
    assert recorded[0]["published_snapshot"] == {"nodes": 4, "edges": 6}


def test_reflect_false_does_not_confirm_sig(_stub_pipeline):
    """(L) `reflect=False`（staging）は成功しても署名を確定しない（MED-2）。pre-invalidate のままにする。"""
    res = worker.run("w", reflect=False)

    assert res["status"] == "extracting"
    assert _stub_pipeline["set_world_sig_calls"] == [""]      # 確定（sig）は呼ばれない＝ '' のまま残る


def test_build_derived_error_blocks_downstream_and_leaves_sig_unconfirmed(monkeypatch, _stub_pipeline):
    """(M) 派生生成が公開Gateで拒否された（`error`付き）場合、graph構築・台帳・ES反映へ進まず、
    正しいsigへの確定も行わない（旧派生content(A)を基に新sig(B)を確定してしまう事故を防ぐ）。"""
    monkeypatch.setattr(worker, "_build_derived",
                        lambda world, **_kw: {"error": "derived_incomplete:unhandled_failed=1",
                                              "unhandled_failed": 1,
                                              "unhandled_failures": [
                                                  {"doc": "broken.xlsx",
                                                   "reason": "unhandled_exception:RuntimeError"}]})
    graph_calls = []
    monkeypatch.setattr(worker, "build_world_graph", lambda world: graph_calls.append(world) or ([], [], []))
    replace_calls = []
    monkeypatch.setattr(store, "replace_documents",
                        lambda world, rows: replace_calls.append(rows) or 0)

    res = worker.run("w")

    assert res["status"] == "failed"
    # per-file の詳細（rel/reason）はもう flags へ複製しない（`failed_files`
    # ＝`_failed_files_summary` が単一の出所・200件上限で集約する）。flags には集約 warn だけが残る。
    assert res["flags"] == [{"doc": None, "action": "warn", "reason": "office_md:derived_incomplete:unhandled_failed=1"}]
    # 情報は失われていない——`failed_files`（capped 集約）に broken.xlsx が残る。
    assert res["run"]["extraction_snapshot"]["failed_files"]["items"] == [
        {"doc": "broken.xlsx", "stage": "unhandled", "reason": "other", "detail": "unhandled_exception:RuntimeError"}]
    assert graph_calls == []                                  # graph構築へ進んでいない
    assert replace_calls == []                                # 台帳更新へも進んでいない
    assert _stub_pipeline["set_world_sig_calls"] == [""]       # pre-invalidateのまま＝確定していない


def test_unreadable_code_file_blocks_downstream_and_leaves_sig_unconfirmed(monkeypatch, _stub_pipeline):
    """受理済みコード文書の実読込失敗（`world_graph.build_world` が返す blocked flag）は
    graph反映・台帳書込へ進まず即 failed で終了し、正しい sig への確定も行わない（fail-closed・
    部分グラフを確定しない）。`corpus_docs.classify_document` の既定 accepts の短絡では検知
    できない失敗を、実際に読み込む Pass1（`build_world_graph`）の結果から worker が受け取って
    run 全体の失敗として扱う。復旧後の次回 sync は sig 不一致で必ず全再構築される。
    """
    monkeypatch.setattr(worker, "build_world_graph",
                        lambda world: ([], [], [{"doc": "案件A/BADPROG.cbl",
                                                 "reason": "unreadable_code_file", "action": "blocked"}]))
    replace_calls = []
    monkeypatch.setattr(store, "replace_documents",
                        lambda world, rows: replace_calls.append(rows) or 0)

    res = worker.run("w")

    assert res["status"] == "failed"
    assert res["flags"] == [{"doc": "案件A/BADPROG.cbl", "reason": "unreadable_code_file", "action": "blocked"}]
    assert replace_calls == []                                # 台帳更新へ進んでいない（部分グラフを確定しない）
    assert _stub_pipeline["set_world_sig_calls"] == [""]       # pre-invalidateのまま＝確定していない


def test_build_derived_error_then_next_sync_retries(monkeypatch, _stub_pipeline):
    """(M) 1回目のsyncでbuild_derivedが失敗してsigが未確定のままだと、2回目のsyncは
    `prev != sig` により必ず全再構築（run）へ入り直す（既存派生A・更新原本Bのシナリオ）。"""
    db_row = {"last_sig": "sig-A", "last_manifest": {"a": [1, 2, 3]}, "last_doc_count": 1}
    monkeypatch.setattr(store, "get_world", lambda world: dict(db_row))

    def _fake_set_world_sig(world, sig, manifest=None, doc_count=None, scan_report=None):
        _stub_pipeline["set_world_sig_calls"].append(sig)
        db_row["last_sig"] = sig                              # 実際のDB行の更新を模擬する
    monkeypatch.setattr(store, "set_world_sig", _fake_set_world_sig)

    # ING-3: 成功確定は `finish_ingest_run_and_confirm_world` 経由（`set_world_sig` 単体は
    # pre-invalidate 専用に残る）——ここでも db_row への反映を模擬する。
    def _fake_finish_and_confirm(run_id, world, *, status, extraction_snapshot=None,
                                 published_snapshot=None, source_doc_ids=None,
                                 sig=None, manifest=None, doc_count=None, scan_report=None):
        if sig is not None:
            _stub_pipeline["set_world_sig_calls"].append(sig)
            db_row["last_sig"] = sig
        return {"id": run_id, "status": status}
    monkeypatch.setattr(store, "finish_ingest_run_and_confirm_world", _fake_finish_and_confirm)
    monkeypatch.setattr(worker, "_derived_stale", lambda world: False)
    monkeypatch.setattr(worker, "world_state", lambda world: ("sig-B", {"a": [1, 2, 3]}))  # 原本はBへ更新済み

    build_derived_calls = []

    def _failing_build_derived(world, **_kw):
        build_derived_calls.append(_kw)
        return {"error": "derived_incomplete:unhandled_failed=1", "unhandled_failed": 1, "unhandled_failures": []}
    monkeypatch.setattr(worker, "_build_derived", _failing_build_derived)

    res1 = worker.sync("w")
    assert res1["status"] == "failed" and res1["changed"] is True
    assert db_row["last_sig"] == ""                           # pre-invalidateのまま（Bへ確定していない）
    assert len(build_derived_calls) == 1

    # 2回目のsync: last_sig("")と実際の原本sig("sig-B")が不一致＝分岐⓪で必ずrun()が再試行される。
    def _succeeding_build_derived(world, **_kw):
        build_derived_calls.append(_kw)
        return {"converted": 1, "failed": 0, "unsupported": 0, "by_ext": {}}
    monkeypatch.setattr(worker, "_build_derived", _succeeding_build_derived)

    res2 = worker.sync("w")
    assert len(build_derived_calls) == 2                      # 再試行された
    assert res2["status"] in ("auto_published", "auto_published_with_flags")
    assert db_row["last_sig"] == "sig-B"                      # 今度は正しく確定する


def test_build_derived_publish_rename_failure_leaves_sig_unconfirmed_and_retries(monkeypatch, _stub_pipeline):
    """後半 rename（staging→target）失敗（`office_md.build_derived` が `derived_publish_failed:*`
    を返す経路・派生ディレクトリ自体は `_publish_staging` のロールバックで旧内容のまま残る＝
    `tests/unit/test_office_md.py::test_publish_failure_after_retire_rolls_back_old_derived_content`
    で確認済み）でも、(M) と同じく sig は確定せず、次回 sync が必ず再試行することを worker 側で
    確認する。"""
    db_row = {"last_sig": "sig-A", "last_manifest": {"a": [1, 2, 3]}, "last_doc_count": 1}
    monkeypatch.setattr(store, "get_world", lambda world: dict(db_row))

    def _fake_set_world_sig(world, sig, manifest=None, doc_count=None, scan_report=None):
        _stub_pipeline["set_world_sig_calls"].append(sig)
        db_row["last_sig"] = sig
    monkeypatch.setattr(store, "set_world_sig", _fake_set_world_sig)

    def _fake_finish_and_confirm(run_id, world, *, status, extraction_snapshot=None,
                                 published_snapshot=None, source_doc_ids=None,
                                 sig=None, manifest=None, doc_count=None, scan_report=None):
        if sig is not None:
            _stub_pipeline["set_world_sig_calls"].append(sig)
            db_row["last_sig"] = sig
        return {"id": run_id, "status": status}
    monkeypatch.setattr(store, "finish_ingest_run_and_confirm_world", _fake_finish_and_confirm)
    monkeypatch.setattr(worker, "_derived_stale", lambda world: False)
    monkeypatch.setattr(worker, "world_state", lambda world: ("sig-B", {"a": [1, 2, 3]}))

    build_derived_calls = []

    def _failing_build_derived(world, **_kw):
        build_derived_calls.append(_kw)
        return {"error": "derived_publish_failed:OSError", "unhandled_failures": []}
    monkeypatch.setattr(worker, "_build_derived", _failing_build_derived)

    res1 = worker.sync("w")
    assert res1["status"] == "failed" and res1["changed"] is True
    assert db_row["last_sig"] == ""                           # pre-invalidateのまま＝Bへ確定しない
    assert len(build_derived_calls) == 1

    def _succeeding_build_derived(world, **_kw):
        build_derived_calls.append(_kw)
        return {"converted": 1, "failed": 0, "unsupported": 0, "by_ext": {}}
    monkeypatch.setattr(worker, "_build_derived", _succeeding_build_derived)

    res2 = worker.sync("w")
    assert len(build_derived_calls) == 2                      # 必ず再試行される
    assert res2["status"] in ("auto_published", "auto_published_with_flags")
    assert db_row["last_sig"] == "sig-B"
