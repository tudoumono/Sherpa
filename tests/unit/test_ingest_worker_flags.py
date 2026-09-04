"""取り込み成功に見える半壊状態の可視化（監査#5・Phase 2-a）の単体テスト。

`_run_locked`（reflect=True 経路）で ES 索引・reconcile が失敗しても例外を握りつぶさず
`extra_flags` の warn として `auto_published_with_flags` に倒れることを検証する。外部サービス
（Postgres/Neo4j/ES）は monkeypatch で差し替え、DB/ネットワーク不要（tests/unit の慣行）。
"""
from __future__ import annotations

import contextlib

import pytest

from sherpa import corpus_docs, es_index, reconcile, store
from sherpa.ingest import world_neo4j, worker


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch):
    """`worker.run` を DB/Neo4j 無しで駆動できるよう周辺を差し替える（happy path 既定）。

    secRV 再RV round-2（HIGH-2）: `_run_locked` は `world_state()` が `sig=None` を返すと
    mutation 前に即 failed で終了するようになった。このファイルの世界 "w" は実ディレクトリに
    解決しない架空 world id なので、`world_state` を固定 sig にスタブして従来どおり
    happy path（ES/reconcile の flag 化）を検証できるようにする。あわせて pre-invalidate/確定の
    `store.set_world_sig` も DB 無しで記録だけするようスタブする（`tests/unit` は外部サービス不要）。
    """
    monkeypatch.setattr(worker, "world_state", lambda world: ("sig", {"a": [1, 2, 3]}))
    monkeypatch.setattr(worker, "build_world_graph", lambda world: ([], [], []))
    monkeypatch.setattr(worker, "_build_derived",
                        lambda world, **_kw: {"converted": 0, "failed": 0, "unsupported": 0, "by_ext": {}})
    monkeypatch.setattr(worker, "_ledger_rows", lambda world, *, sig: [])
    monkeypatch.setattr(worker, "world_signature", lambda world: "sig")
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})
    monkeypatch.setattr(world_neo4j, "load_world", lambda nodes, edges, world, uri, user, pw: (0, 0))

    @contextlib.contextmanager
    def _noop_lock(world_id):
        yield
    monkeypatch.setattr(store, "world_lock", _noop_lock)

    captured = {"replace_documents_calls": 0, "set_world_sig_calls": []}

    def _fake_replace_documents(world, rows):
        captured["replace_documents_calls"] += 1
        return 3
    monkeypatch.setattr(store, "replace_documents", _fake_replace_documents)

    def _fake_set_world_sig(world, sig, manifest=None, doc_count=None, scan_report=None):
        captured["set_world_sig_calls"].append(sig)
        captured["set_world_sig_doc_counts"] = captured.get("set_world_sig_doc_counts", []) + [doc_count]
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
        captured["extraction_snapshot"] = kw.get("extraction_snapshot")
        captured["status"] = kw.get("status")
        return {"id": run_id, **kw}
    monkeypatch.setattr(store, "finish_ingest_run", _fake_finish_ingest_run)

    # ING-3: 成功パスの run 完了＋sig/doc_count 確定は同一トランザクション
    # （`finish_ingest_run_and_confirm_world`）へ移った——`extraction_snapshot`/`status` の記録先も
    # `set_world_sig_calls`/`set_world_sig_doc_counts` の記録先も、この関数からのものを合流させる。
    def _fake_finish_and_confirm(run_id, world, *, status, extraction_snapshot=None,
                                 published_snapshot=None, source_doc_ids=None,
                                 sig=None, manifest=None, doc_count=None, scan_report=None):
        captured["extraction_snapshot"] = extraction_snapshot
        captured["status"] = status
        if sig is not None:
            captured["set_world_sig_calls"].append(sig)
            captured["set_world_sig_doc_counts"] = captured.get("set_world_sig_doc_counts", []) + [doc_count]
        return {"id": run_id, "status": status}
    monkeypatch.setattr(store, "finish_ingest_run_and_confirm_world", _fake_finish_and_confirm)
    return captured


def test_es_index_failure_becomes_warn_flag(_stub_pipeline, monkeypatch):
    """ES 索引失敗は握りつぶさず warn flag 化＋status は auto_published_with_flags に倒れる（監査#5）。"""
    def _boom(world, content_sig=None):
        raise RuntimeError("es down")
    monkeypatch.setattr(es_index, "index_world", _boom)
    monkeypatch.setattr(reconcile, "reconcile_derivatives", lambda reflect=True: None)

    res = worker.run("w")

    assert res["status"] == "auto_published_with_flags"
    reasons = [f.get("reason") for f in res["flags"]]
    # 失敗理由に接続先（host:port）を含める（閉域実機 2026-08-18: 例示ホスト名のまま有効化した事故が画面で追えなかった）
    matched = [r for r in reasons if r and r.startswith("es_index_failed:RuntimeError@")]
    assert matched and ":" in matched[0].split("@", 1)[1], reasons
    snap = _stub_pipeline["extraction_snapshot"]
    assert any((f.get("reason") or "").startswith("es_index_failed:RuntimeError@") for f in snap["flags"])
    assert _stub_pipeline["replace_documents_calls"] == 1     # 台帳書込はグラフ成功後に行われている（ES失敗の影響を受けない）


def test_reconcile_failure_becomes_warn_flag(_stub_pipeline, monkeypatch):
    """reconcile 失敗も同様に握りつぶさず warn flag 化される（監査#5）。"""
    monkeypatch.setattr(es_index, "index_world",
                        lambda world, content_sig=None: {"available": True, "indexed": 1, "chunks": 1})

    def _boom(reflect=True):
        raise RuntimeError("reconcile down")
    monkeypatch.setattr(reconcile, "reconcile_derivatives", _boom)

    res = worker.run("w")

    assert res["status"] == "auto_published_with_flags"
    reasons = [f.get("reason") for f in res["flags"]]
    assert "reconcile_failed:RuntimeError" in reasons


def test_both_succeed_stays_auto_published(_stub_pipeline, monkeypatch):
    """ES/reconcile が両方成功すれば flags は空のまま status は auto_published（既定の happy path 回帰）。

    成功確定の `set_world_sig` 呼び出しは `manifest`（`world_state` スタブが返す
    `{"a": [1, 2, 3]}`）から doctype 対応原本件数を数えた `doc_count` を渡す。`"a"` は拡張子を
    持たないため doctype 対象外＝確定値は 0（pre-invalidate 呼び出しは doc_count=None のまま）。
    """
    monkeypatch.setattr(es_index, "index_world",
                        lambda world, content_sig=None: {"available": True, "indexed": 1, "chunks": 1})
    monkeypatch.setattr(reconcile, "reconcile_derivatives", lambda reflect=True: None)

    res = worker.run("w")

    assert res["status"] == "auto_published"
    assert _stub_pipeline["set_world_sig_doc_counts"] == [None, 0]
    assert res["flags"] == []


def test_es_index_error_dict_becomes_warn_flag(_stub_pipeline, monkeypatch):
    """index_world は主要な失敗（delete/create/bulk）を例外でなく error dict で返す＝それも warn 化する（監査#5 本丸）。"""
    monkeypatch.setattr(es_index, "index_world",
                        lambda world, content_sig=None: {"available": True, "indexed": 0, "chunks": 0,
                                                          "error": "bulk_failed"})
    monkeypatch.setattr(reconcile, "reconcile_derivatives", lambda reflect=True: None)

    res = worker.run("w")

    assert res["status"] == "auto_published_with_flags"
    reasons = [f.get("reason") for f in res["flags"]]
    assert "es_index_failed:bulk_failed" in reasons


def test_es_index_unavailable_does_not_warn(_stub_pipeline, monkeypatch):
    """ES 未起動/未導入（`available=False`・error キー無し）は意図的 no-op＝warn しない（誤警報回帰ガード）。"""
    monkeypatch.setattr(es_index, "index_world",
                        lambda world, content_sig=None: {"available": False, "indexed": 0, "chunks": 0})
    monkeypatch.setattr(reconcile, "reconcile_derivatives", lambda reflect=True: None)

    res = worker.run("w")

    assert res["status"] == "auto_published"
    assert res["flags"] == []


def test_human_md_es_confirm_failure_becomes_warn_flag_not_silent_success(
        _stub_pipeline, monkeypatch, tmp_path):
    """`office_md.confirm_human_md_es_sig` が False（render 側の drift 残り・マーカー書込失敗）を
    返しても、その事実を捨てず warn flag 化する——`auto_published`（無警告の成功）に倒さない。"""
    from sherpa import worlds
    from sherpa.ingest import office_md

    wd = tmp_path / "world"; wd.mkdir()
    dmd = tmp_path / "derived"; dmd.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda world: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world: dmd)
    monkeypatch.setattr(es_index, "index_world",
                        lambda world, content_sig=None: {"available": True, "indexed": 1, "chunks": 1})
    monkeypatch.setattr(reconcile, "reconcile_derivatives", lambda reflect=True: None)
    monkeypatch.setattr(office_md, "confirm_human_md_es_sig", lambda wd, dmd: False)

    res = worker.run("w")

    assert res["status"] == "auto_published_with_flags"
    reasons = [f.get("reason") for f in res["flags"]]
    assert "human_md_es_sig_marker_confirm_failed" in reasons


def test_human_md_es_meta_confirm_failure_becomes_warn_flag(_stub_pipeline, monkeypatch, tmp_path):
    """マーカー確定（`confirm_human_md_es_sig`）は成功しても、続く ES 自身の `_meta` 書き直し
    （`es_index.confirm_human_md_meta`）が失敗したら、それも warn flag 化する（meta が
    古いまま残ると次回 sync が無駄な再索引を繰り返し続けるため、黙って見逃さない）。"""
    from sherpa import worlds
    from sherpa.ingest import office_md

    wd = tmp_path / "world"; wd.mkdir()
    dmd = tmp_path / "derived"; dmd.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda world: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world: dmd)
    monkeypatch.setattr(es_index, "index_world",
                        lambda world, content_sig=None: {"available": True, "indexed": 1, "chunks": 1})
    monkeypatch.setattr(reconcile, "reconcile_derivatives", lambda reflect=True: None)
    monkeypatch.setattr(office_md, "confirm_human_md_es_sig", lambda wd, dmd: True)
    monkeypatch.setattr(es_index, "confirm_human_md_meta", lambda world: False)

    res = worker.run("w")

    assert res["status"] == "auto_published_with_flags"
    reasons = [f.get("reason") for f in res["flags"]]
    assert "human_md_es_meta_confirm_failed" in reasons


def test_target_of_strips_credentials_and_keeps_host_port():
    """失敗理由に含める接続先は host:port のみ（userinfo/パスは出さない・不正値は "?"）。"""
    assert worker._target_of("bolt://neo4j:secret@graph.example.local:7687") == "graph.example.local:7687"
    assert worker._target_of("http://localhost:9200/") == "localhost:9200"
    assert worker._target_of("http://search.local") == "search.local"
    assert worker._target_of("") == "?"
    assert worker._target_of("bolt://") == "?"
    assert "secret" not in worker._target_of("bolt://u:secret@h:1")
