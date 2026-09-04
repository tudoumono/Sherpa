"""既存データ移行の受け入れ（要 PG+Neo4j+ES）。

旧コード（I1 導入前）が `_重要度.txt` を通常文書として documents 台帳に書き、意味層まで
LLM 抽出していた world は、専用の移行機構ではなく**標準の「署名不一致→全再構築」経路**で
自動的に是正される: 重要度機能のスキーマ版（`ingest.importance.IMPORTANCE_SCHEMA_VERSION`）を
world 署名の材料に畳み込み、ES マッピング版（`es_index.ES_MAPPING_VERSION`）も上げてある
ため、I1 導入後の初回 sync は必ず署名不一致として検知され、通常の内容変更と同じ full
rebuild が走る。`build_world` は制御ファイルを除外済みなので、rebuild するだけで台帳・
Neo4j から旧世代のエントリが消える（`l_extract.json`/per-doc 抽出キャッシュに残る旧
`_重要度.txt` 由来のエントリは、`_load_semantic` が `valid_docs`〔制御ファイル除外済み〕で
絞るため Neo4j には載らない＝キャッシュ自体は触らない）。ES 側のマッピング版不一致検知は
`needs_reindex` の既存の仕組みで別経路（世界の署名とは独立）に効くため、別テストで固定する。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import tempfile

import pytest
from _world_registry import register_test_world

from sherpa import es_index, store, worlds
from sherpa.ingest import worker, world_neo4j

WORLD = "importance_schema_version_rebuild_test"
CONTROL_DOC = "4期/_重要度.txt"
CBL_DOC = "4期/src/TAXRATE.cpy"
FAKE_RULE_CID = f"document:{WORLD}:4期#偽ルール"


def _driver():
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "sherpa_dev")),
    )


def _cleanup_world(wid):
    """`store.delete_world_row` は worlds レジストリ行しか消さない（documents 台帳・派生
    derived_dir の削除は呼出側の責務）。テストごとに一意な wid を使うため、次回の別テスト
    実行での自然な上書きに頼れず、明示的に両方を後始末する。
    """
    try:
        store.replace_documents(wid, [])
    except Exception:
        pass
    shutil.rmtree(worlds.derived_dir(wid), ignore_errors=True)
    store.delete_world_row(wid)


def test_legacy_world_first_sync_clears_control_file_from_ledger_and_neo4j():
    """旧行あり world（documents 台帳に `_重要度.txt` が通常文書として残り、Neo4j にも
    その来歴を持つ意味層ノードが残っている）の初回 sync で、台帳・Neo4j から制御ファイルの
    痕跡が消える（標準の署名不一致→全再構築・migration 専用経路は無い）。ES 側の検証は
    別テスト（`test_es_mapping_version_bump_triggers_reindex_on_next_sync`・
    `test_es_unavailable_during_sync_is_reindexed_after_recovery`）が担う——ここでは
    documents 台帳・Neo4j だけを固定する。
    """
    root = pathlib.Path(tempfile.mkdtemp())
    wid = WORLD
    try:
        src_dir = root / "4期" / "src"
        src_dir.mkdir(parents=True)
        (src_dir / "TAXRATE.cpy").write_text(
            "       01 TAX-RATE      PIC 9(2)V9 VALUE 10.0.\n", encoding="utf-8")
        (root / "4期" / "_重要度.txt").write_text("*.cpy: 高\n", encoding="utf-8")

        store.upsert_world(wid, str(root.resolve()))
        register_test_world(wid)   # セッション終了時にまとめて削除（backstop）

        # 旧コード（I1 導入前）が確定させていた状態を再現する: 署名はスキーマ版込みの現行計算とは
        # 別の（古い）値で確定させ「まだこの world は現行スキーマで一度も rebuild されていない」を
        # 偽装し、documents 台帳・Neo4j 両方に実際の汚染データを投入する。
        _, manifest = worker.world_state(wid)
        store.set_world_sig(wid, "legacy-pre-schema-bump-sig", manifest=manifest, doc_count=2)
        store.replace_documents(wid, [
            {"name": CBL_DOC, "layer": "version", "scope_path": "4期", "doctype": "copybook",
             "branch": "source", "original_path": None, "md_path": None, "status": "indexed"},
            {"name": CONTROL_DOC, "layer": "version", "scope_path": "4期", "doctype": "テキスト",
             "branch": "office", "original_path": None, "md_path": None, "status": "indexed"},
        ])
        env = world_neo4j._env()
        world_neo4j.load_world(
            [{"cid": FAKE_RULE_CID, "label": "Document", "name": "偽ルール", "world_id": wid,
             "top_scope": "4期", "phase": None, "category": None, "path": None, "scope_path": None,
             "value": None, "extraction_method": "llm", "status": "active"}],
            [], wid, env["uri"], env["user"], env["pw"])

        res = worker.sync(wid)

        assert res["changed"] is True, res
        names = {r["name"] for r in store.list_documents(wid)}
        assert CONTROL_DOC not in names, "重要度設定ファイルの行が台帳に残っている"
        assert CBL_DOC in names, "正規の文書まで消えている"

        drv = _driver()
        try:
            with drv.session() as s:
                fake_nodes = list(s.run(
                    "MATCH (n:Entity {world_id:$w, name:'偽ルール'}) RETURN count(n) AS c", w=wid))
                assert fake_nodes[0]["c"] == 0, "旧世代の意味層ノードが rebuild 後も残っている"
        finally:
            drv.close()
    finally:
        _cleanup_world(wid)
        shutil.rmtree(root, ignore_errors=True)


def test_importance_schema_version_bump_repopulates_null_columns_on_next_sync(monkeypatch):
    """RV2是正#a1: `ingest/worker.py::_ledger_rows` は importance/importance_reason/
    importance_source を ingest 時に materialize するようになったが、`IMPORTANCE_SCHEMA_VERSION`
    を上げていなければ、旧 v1 署名で確定済みの既存 world は通常の `sync` が unchanged 経路に
    入り `_ledger_rows` を再実行しない——旧行は3列とも `NULL` のまま固定される。

    旧コード（`IMPORTANCE_SCHEMA_VERSION=1`）が実際に確定させていた `last_sig` を
    一時的に version=1 へ戻して計算することで再現し、importance 列 3 つを持たない台帳行
    （旧 `_ledger_rows` の出力を模す）を投入する。現行版（2）での `sync` が署名不一致を検知して
    rebuild し、importance が materialize されることを固定する。
    """
    root = pathlib.Path(tempfile.mkdtemp())
    wid = WORLD + "_importance_schema_bump"
    try:
        (root / "a.cpy").write_text("       01 TAX-RATE      PIC 9(2)V9 VALUE 10.0.\n", encoding="utf-8")
        (root / "_重要度.txt").write_text("*.cpy: 高\n", encoding="utf-8")
        store.upsert_world(wid, str(root.resolve()))
        register_test_world(wid)

        # 旧 v1 スキーマで確定していた last_sig を再現する（当時のコードが実際に書き込んで
        # いた値そのもの＝IMPORTANCE_SCHEMA_VERSION を一時的に 1 へ戻して計算する）。
        from sherpa.ingest import importance as importance_mod

        monkeypatch.setattr(importance_mod, "IMPORTANCE_SCHEMA_VERSION", 1)
        legacy_sig, manifest = worker.world_state(wid)
        monkeypatch.undo()   # 現行の IMPORTANCE_SCHEMA_VERSION（2）へ戻す

        store.set_world_sig(wid, legacy_sig, manifest=manifest, doc_count=1)
        # 旧コード（importance 3列を materialize しない `_ledger_rows`）が書いていた状態を再現する。
        store.replace_documents(wid, [
            {"name": "a.cpy", "layer": "version", "scope_path": None, "doctype": "copybook",
             "branch": "source", "original_path": None, "md_path": None, "status": "indexed"},
        ])

        res = worker.sync(wid)

        assert res["changed"] is True, "旧v1署名は現行v2署名と不一致のはず（版が上がっていない場合の回帰）"
        rows = {r["name"]: r for r in store.list_documents(wid)}
        assert rows["a.cpy"]["importance"] == "高", "次回 sync で importance が再構築されているはず"
    finally:
        _cleanup_world(wid)
        shutil.rmtree(root, ignore_errors=True)


def test_documents_endpoint_reflects_importance_from_real_ingest(auth_disabled):
    """RV2是正#b3③: `tests/api/test_ledger_p3.py::test_documents_endpoint_uses_ledger_when_populated`
    は importance 3列を手投入しており、`_ledger_rows`（ingest 時の実解決・materialize）と
    既存デプロイの移行（旧行 importance=NULL → 次回 sync で再構築）を検証していなかった。

    ここでは実 ingest（`worker.sync`→`_run_locked`→`_ledger_rows`）を1回実行し、その結果が
    `GET /documents` のAPI応答（`doc_ledger.public_documents_page` の台帳高速経路）にも
    そのまま現れることを、手投入なしで固定する——`_ledger_rows` の実解決から API 応答までの
    結線全体を通す。
    """
    from fastapi.testclient import TestClient
    from sherpa.api import app

    root = pathlib.Path(tempfile.mkdtemp())
    wid = WORLD + "_api_real_ingest"
    try:
        (root / "a.cpy").write_text("       01 TAX-RATE      PIC 9(2)V9 VALUE 10.0.\n", encoding="utf-8")
        (root / "b.cpy").write_text("       01 OTHER-ITEM    PIC 9(2)V9 VALUE 20.0.\n", encoding="utf-8")
        (root / "_重要度.txt").write_text("*.cpy: 高  # 一次資料\n", encoding="utf-8")
        store.upsert_world(wid, str(root.resolve()))
        register_test_world(wid)

        res = worker.sync(wid)
        assert res["changed"] is True, res

        c = TestClient(app)
        body = c.get("/documents", params={"world": wid}).json()
        by_name = {d["name"]: d for d in body["documents"]}
        assert by_name["a.cpy"]["importance"] == "高"
        assert by_name["a.cpy"]["importance_reason"] == "一次資料"
        assert by_name["b.cpy"]["importance"] == "高"
    finally:
        _cleanup_world(wid)
        shutil.rmtree(root, ignore_errors=True)


def test_es_mapping_version_bump_triggers_reindex_on_next_sync(monkeypatch):
    """ES 側に旧マッピング版（"3"）で索引済みのメタが残っている world は、ソース内容・
    world 署名が不変（`sync` は unchanged 経路）でも、`needs_reindex` が版不一致を検知して
    現行のマッピング版（`es_index.ES_MAPPING_VERSION`）に再索引される（重要度機能のスキーマ版
    導入に伴う `ES_MAPPING_VERSION` "3"→"4" の実地検証・世界の署名とは独立した経路）。
    ES 未到達の環境ではこのテスト自体を検証できないためスキップする。
    """
    if not es_index.available():
        pytest.skip("ES unavailable")
    root = pathlib.Path(tempfile.mkdtemp())
    wid = WORLD + "_es_mapping_bump"
    try:
        (root / "a.md").write_text("消費税率は10%。", encoding="utf-8")
        store.upsert_world(wid, str(root.resolve()))
        register_test_world(wid)

        sig, manifest = worker.world_state(wid)
        store.set_world_sig(wid, sig, manifest=manifest, doc_count=1)
        store.replace_documents(wid, [
            {"name": "a.md", "layer": "version", "scope_path": None, "doctype": "設計書",
             "branch": "source", "original_path": None, "md_path": None, "status": "indexed"},
        ])

        # ES へは旧マッピング版（"3"）で索引済みの状態を作る。
        monkeypatch.setattr(es_index, "ES_MAPPING_VERSION", "3")
        setup_result = es_index.index_world(wid, content_sig=sig)
        assert setup_result.get("available") is True and not setup_result.get("error"), \
            f"前提の索引セットアップ自体が失敗している: {setup_result}"
        meta_before = es_index._index_meta(wid)
        assert meta_before.get("mapping_version") == "3", "前提: 旧マッピング版で索引できていない"
        monkeypatch.undo()   # 現行の ES_MAPPING_VERSION に戻す

        res = worker.sync(wid)

        assert res["changed"] is False, "ソース内容・署名は不変（ES 側だけが古い）"
        meta_after = es_index._index_meta(wid)
        assert meta_after.get("mapping_version") == es_index.ES_MAPPING_VERSION, \
            "sync 後は現行のマッピング版に更新されているはず"
    finally:
        _cleanup_world(wid)
        shutil.rmtree(root, ignore_errors=True)


def test_es_unavailable_during_sync_is_reindexed_after_recovery(monkeypatch):
    """sync 実行中に ES が到達不能でも取り込み自体（台帳・グラフ）は成功する（ES は
    best-effort）。ES 復旧後は `needs_reindex` が索引欠落を検知したままなので、次回 sync で
    恒久的な取りこぼしにならず再索引される。ES 未到達の環境ではこのテストの後半（復旧確認）を
    検証できないためスキップする。
    """
    root = pathlib.Path(tempfile.mkdtemp())
    wid = WORLD + "_es_recovery"
    try:
        (root / "a.md").write_text("消費税率は10%。", encoding="utf-8")
        store.upsert_world(wid, str(root.resolve()))
        register_test_world(wid)

        monkeypatch.setattr(es_index, "index_world",
                            lambda *a, **kw: {"available": False, "indexed": 0, "chunks": 0})
        res1 = worker.sync(wid)
        assert res1["changed"] is True, res1   # 初回 rebuild は ES の可否と独立に成功する

        monkeypatch.undo()   # ES 復旧を模す（実 index_world に戻す）
        if not es_index.available():
            pytest.skip("ES unavailable")

        assert es_index.needs_reindex(wid, worker.world_signature(wid)) is True, \
            "ES 未到達だった間の索引欠落がまだ検知されているはず"

        res2 = worker.sync(wid)   # 内容・署名は不変だが ES の欠落を検知して再索引されるはず

        assert res2["changed"] is False, res2
        meta = es_index._index_meta(wid)
        assert meta.get("mapping_version") == es_index.ES_MAPPING_VERSION, \
            "復旧後の sync で再索引され、現行のマッピング版になっているはず"
    finally:
        _cleanup_world(wid)
        shutil.rmtree(root, ignore_errors=True)
