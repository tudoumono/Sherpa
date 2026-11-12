"""番号を持たない古い形の資料フォルダから全体の取り込みを 1 回流して、番号 0 の行が残らず、検索・名前の台帳・取り込みの画面が変わらず動くことの確認
（本物の worker を子プロセスで動かす・要 Postgres＋Neo4j＋ES）。土台は `test_ingest_stop_matrix` と共用する。

古い形は、取り込み済みの資料フォルダの DB・派生物・グラフ・ES の番号を直接外して作る（番号の列は 0・`.ingest_no` と `archive_manifest.json` の番号は無し・
グラフのメタと ES の `_meta` の番号は無し・入れ替えの行は無し）。
"""
from __future__ import annotations

import json

import pytest
from _ingest_crash import run_worker_once
from _world_setup import driver
from test_ingest_stop_matrix import (  # noqa: F401  (registered_* は fixture)
    W, _add_archive, _change_source, _enqueue_refresh, _graph_state, _layer_dirs, _layer_numbers, _ledger_numbers, _manifest,
    _rows, _run, registered_code_world, registered_world)

from sherpa import es_index, store, worlds
from sherpa.ingest import derived_numbers
from sherpa.store import db, name_ledger

OK = ("auto_published", "auto_published_with_flags")


def _make_legacy() -> None:
    """取り込み済みの資料フォルダの全部の番号を外して、番号が付く前の形にする。"""
    with db._connect() as c:
        c.execute("DELETE FROM ingest_replacements WHERE kb_id=%s AND world=%s", (db._KB_ID, W))
        c.execute("UPDATE documents SET ingest_no=0, doc_ingest_no=0 WHERE kb_id=%s AND version=%s", (db._KB_ID, W))
        c.execute("UPDATE name_ledger SET ingest_no=0 WHERE world=%s", (W,))
        c.execute("UPDATE name_ledger_built SET ingest_no=0 WHERE world=%s", (W,))
        c.execute("UPDATE worlds SET shown_ingest_no=0 WHERE kb_id=%s AND world_id=%s", (db._KB_ID, W))
    for d in _layer_dirs().values():
        (d / derived_numbers.INGEST_NO_FILE).unlink()
    manifest = _manifest()
    manifest.pop("ingest_no", None)
    manifest.pop("settings_sig", None)
    worlds.archive_manifest_path(W).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    drv = driver()
    try:
        with drv.session() as s:
            s.run("MATCH (m:SherpaMeta {world_id:$w}) REMOVE m.ingest_no, m.settings_sig", w=W).consume()
    finally:
        drv.close()
    meta = dict(es_index._index_meta(W))
    meta.pop("ingest_no", None)
    meta.pop("settings_sig", None)
    es_index._req("PUT", f"/{es_index._index(W)}/_mapping", {"_meta": meta})


def _numbers() -> dict:
    """番号を書く全部の場所の、今の番号。"""
    return {"ledger": _ledger_numbers(), "layers": _layer_numbers(), "graph": _graph_state()["no"],
            "es": es_index.read_ingest_no(W), "archives": _manifest().get("ingest_no"), "rows": _rows()}


def _reads() -> dict:
    """検索・名前の引き当て・目録・取り込みの画面が返す、資料フォルダの今の姿。"""
    from sherpa.routers import worlds as api
    names = name_ledger.read_rows(W)
    summary = api._ingest_summary(W, store.get_world(W), api._run_state(W))
    return {"documents": sorted(store.list_document_names(W)),
            "names": sorted((r["name"], r["rel_path"]) for r in names),
            "lookup": {r["name"]: sorted(x["rel_path"] for x in name_ledger.lookup(W, r["name"])) for r in names},
            "hits": sorted(h["doc_id"] for h in es_index.search(W, "サンプル内容", k=20)[0]),
            "built": name_ledger.is_built(W), "es_count": es_index.count(W),
            "summary": {k: summary[k] for k in ("last_run_status", "ingest_active", "last_failure_message")}}


@pytest.fixture
def legacy_world(registered_code_world):
    """アーカイブも持つ取り込み済みの資料フォルダを、古い形に直したもの。"""
    _add_archive("L")
    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in OK and _manifest()["ingest_no"] > 0
    _make_legacy()
    yield W


def test_the_old_shape_is_read_as_number_zero_and_nothing_is_reported_as_stopped(legacy_world):
    """番号の無い形は、番号 0 として今までどおり読める。入れ替えの行が無いので「途中で止まった」とは出ない。"""
    now = _numbers()
    assert now["ledger"] == {"docs": [0], "names": [0], "built": 0, "shown": 0}
    assert now["layers"] == {"md": None, "rag": None, "ir": None} and now["graph"] is None and now["es"] == 0
    assert now["archives"] is None and now["rows"] == []
    reads = _reads()
    assert reads["documents"] and reads["names"] and reads["built"] and reads["hits"] and reads["es_count"]
    assert store.unfinished_state(W) is None


def test_a_whole_ingest_over_the_old_shape_leaves_no_number_zero_and_reads_stay_the_same(legacy_world):
    """番号の無い形に新しいファイルを足して全体の取り込みを流すと、全部が同じ新しい番号になり、番号 0 の行・断片が残らない。読み取りは足した分以外変わらない。"""
    before = _reads()
    _change_source("L1")
    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in OK

    now = _numbers()
    no = now["ledger"]["shown"]
    assert no > 0 and now["rows"] == []
    assert now["ledger"] == {"docs": [no], "names": [no], "built": no, "shown": no}
    assert set(now["layers"].values()) == {no} and now["graph"] == no and now["es"] == no and now["archives"] == no
    assert len(_graph_state()["gens"]) == 1 and len(es_index._concrete_indices(W)) == 1
    after = _reads()
    assert after["built"] and after["summary"]["last_run_status"] in OK and not after["summary"]["ingest_active"]
    assert set(before["documents"]) < set(after["documents"]) and len(after["documents"]) == len(before["documents"]) + 1
    assert set(before["names"]) <= set(after["names"])
    assert all(after["lookup"][n] == p for n, p in before["lookup"].items())
    assert set(before["hits"]) <= set(after["hits"])
    assert after["es_count"] > before["es_count"]


def test_an_update_with_nothing_changed_over_the_old_shape_keeps_reading_and_leaves_no_row(legacy_world):
    """原本が変わらない「更新」は、番号の無い形を読み続けたまま行を残さない（番号は次に全体の取り込みが流れたときに付く）。"""
    before = _reads()
    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in OK
    assert _rows() == []
    assert store.unfinished_state(W) is None
    after = _reads()
    assert {k: v for k, v in after.items() if k != "summary"} == {k: v for k, v in before.items() if k != "summary"}
