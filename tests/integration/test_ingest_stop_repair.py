"""原本が変わらない「更新」の自己修復（ES・グラフ）と、派生の軽量な再生成の途中で止まった場面の確認（本物の worker を子プロセスで動かす・要 Postgres＋Neo4j＋ES）。
土台は `test_ingest_stop_matrix` と共用する。止まった後は「途中で止まった」と出て、次の「更新」が入れ替えきって行が消える。
"""
from __future__ import annotations

from _ingest_crash import run_worker_once
from _world_setup import driver
from test_ingest_stop_matrix import (  # noqa: F401  (registered_* は fixture)
    W, _enqueue_refresh, _graph_state, _kill_while_graph_node_is_held, _layer_dirs, _layer_numbers, _rows, _run,
    _settle_orphan, _wait_stopped, registered_code_world, registered_world)

from sherpa import es_index, store

OK = ("auto_published", "auto_published_with_flags")


def _resume() -> None:
    _settle_orphan()
    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in OK and _rows() == []
    assert store.unfinished_state(W) is None


def test_kill_in_the_es_self_repair_of_an_unchanged_update_is_reported_and_the_next_update_finishes_it(registered_world):
    """原本が変わらない「更新」が ES を張り直す途中で止まると「途中で止まった」と出て、次の「更新」が張り直しきって行が消える。"""
    meta = dict(es_index._index_meta(W))
    meta["mapping_version"] = "stale"
    es_index._req("PUT", f"/{es_index._index(W)}/_mapping", {"_meta": meta})
    assert es_index.needs_reindex(W, meta["content_sig"])
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_http_send="/_bulk").returncode == -9
    _wait_stopped()
    assert len(_rows()) == 1

    _resume()

    assert not es_index.needs_reindex(W, es_index._index_meta(W)["content_sig"]) and es_index.read_ingest_no(W) > 0


def test_kill_in_the_graph_self_repair_of_an_unchanged_update_is_reported_and_the_next_update_finishes_it(registered_code_world):
    """原本が変わらない「更新」がグラフを作り直す途中（切り替え）で止まると「途中で止まった」と出て、次の「更新」が入れ替えきる。世代は 1 つに戻る。"""
    drv = driver()
    try:
        with drv.session() as s:
            s.run("MATCH (m:SherpaMeta {world_id:$w}) SET m.node_count = m.node_count + 5", w=W).consume()
    finally:
        drv.close()
    _kill_while_graph_node_is_held("MATCH (m:SherpaMeta {world_id:$w}) SET m.stop_hold = true", "グラフの切り替え")
    _wait_stopped()
    left = _rows()
    assert len(left) == 1

    _resume()

    state = _graph_state()
    assert len(state["gens"]) == 1 and state["no"] == left[0]["ingest_no"]


def test_kill_after_the_light_derived_refresh_is_reported_and_the_next_update_finishes_it(registered_world):
    """派生の軽量な再生成（検索用の MD の署名ずれ）が済んだ後、ES の張り直しの途中で止まると「途中で止まった」と出て、次の「更新」が入れ替えきる。"""
    marker = _layer_dirs()["md"] / ".rag_sig"
    marker.write_text(marker.read_text(encoding="utf-8") + ";bumped", encoding="utf-8")
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_http_send="/_bulk").returncode == -9
    _wait_stopped()
    left = _rows()
    assert len(left) == 1 and left[0]["items"].get("rag") == "cleaned"

    _resume()

    assert _layer_numbers()["rag"] == left[0]["ingest_no"]
