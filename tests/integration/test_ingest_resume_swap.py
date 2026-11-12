"""止まった取り込みの再開が、同じ番号で残りだけを入れ替えることの確認（本物の worker を子プロセスで動かす・要 Postgres＋Neo4j＋ES）。土台は `test_ingest_stop_matrix` と共用する。"""
from __future__ import annotations

import pytest
from _ingest_crash import run_worker_once
from test_ingest_stop_matrix import (  # noqa: F401  (registered_* は fixture)
    W, _add_archive, _change_source, _enqueue_refresh, _es_state, _graph_state, _kill_while_graph_node_is_held, _layer_dirs,
    _layer_numbers, _no_leftover_dirs, _rows, _run, _settle_orphan, _wait_stopped, registered_code_world, registered_world)

from test_ingest_stop_swap import (  # noqa: F401  (new_root は fixture)
    _backup, _doc_stop_state, _mark_failed, _resume_by_update, _stop_reconvert, _stop_rebind, _submit_reconvert, new_root)

from sherpa import store, worlds
from sherpa.ingest import resume
from sherpa.store import db

OK = ("auto_published", "auto_published_with_flags")


def _layer_stamps() -> dict:
    return {k: (d / ".ingest_no").stat().st_mtime_ns for k, d in _layer_dirs().items()}


def _doc_versions() -> list:
    with db._connect() as c:
        return sorted(r["x"] for r in c.execute(
            "SELECT xmin::text AS x FROM documents WHERE kb_id=%s AND version=%s", (db._KB_ID, W)).fetchall())


def _snapshot() -> dict:
    return {"layers": _layer_numbers(), "stamps": _layer_stamps(), "graph": _graph_state(), "es": _es_state(),
            "docs": _doc_versions()}


def _resume() -> int:
    _settle_orphan()
    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in OK and _rows() == []
    return run_id


def test_resume_after_the_graph_switch_stopped_skips_the_derived_layers_and_a_rerun_changes_nothing(registered_code_world):
    """グラフの切り替えの前で止まった再開は、入れ替え済みの派生を作り直さず（番号の印が書き換わらない）同じ番号でグラフから入れ替える。もう一度流しても何も変わらない。"""
    _change_source("r1")
    _kill_while_graph_node_is_held("MATCH (m:SherpaMeta {world_id:$w}) SET m.stop_hold = true", "グラフの切り替え")
    _wait_stopped()
    left = _rows()[0]
    assert left["items"]["ir"] == left["items"]["rag"] == left["items"]["md"] == "cleaned" and left["items"]["graph"] == "swapped"
    stamps = _layer_stamps()

    _resume()

    assert _layer_stamps() == stamps and _layer_numbers() == {k: left["ingest_no"] for k in ("md", "rag", "ir")}
    assert _graph_state()["no"] == left["ingest_no"] and _no_leftover_dirs()
    after = _snapshot()
    _enqueue_refresh(W)
    assert run_worker_once().returncode == 0 and _rows() == []
    assert _snapshot() == after


def test_resume_after_the_es_alias_switch_keeps_the_new_index_and_everything_before_it(registered_code_world):
    """ES の付け替えの後で止まった再開は、新しい索引・グラフの世代・目録・派生を作り直さず、旧索引だけを片付けて同じ番号で終える。"""
    _change_source("r2")
    _enqueue_refresh(W)
    assert run_worker_once(kill_after_http_send="POST /_aliases", kill_on_http_send="DELETE /").returncode == -9
    _wait_stopped()
    left = _rows()[0]
    assert left["items"]["es"] == "swapped" and left["items"]["ledger"] == "cleaned"
    before = _snapshot()
    assert len(before["es"]["physicals"]) == 2

    _resume()

    after = _snapshot()
    assert after["es"]["targets"] == before["es"]["targets"] and after["es"]["physicals"] == before["es"]["targets"]
    assert after["es"]["no"] == left["ingest_no"]
    assert {k: after[k] for k in ("layers", "stamps", "graph", "docs")} == {k: before[k] for k in ("layers", "stamps", "graph", "docs")}


def test_resume_after_the_source_changed_renumbers_and_rebuilds_everything(registered_code_world):
    """止まった後で原本が変わった再開は、新しい番号に置き換えて全部作り直す。もう一度止まっても、残る世代・索引は 2 つまで。"""
    _change_source("r3")
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_http_send="/_bulk").returncode == -9
    _wait_stopped()
    first = _rows()[0]
    _settle_orphan()

    _change_source("r4")
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_http_send="/_bulk").returncode == -9
    _wait_stopped()
    second = _rows()[0]
    assert second["id"] == first["id"] and second["ingest_no"] > first["ingest_no"] and second["source_sig"] != first["source_sig"]
    assert len(_graph_state()["gens"]) <= 2 and len(_es_state()["physicals"]) <= 2

    _resume()

    after = _snapshot()
    assert set(after["layers"].values()) == {second["ingest_no"]} and after["graph"]["no"] == second["ingest_no"]
    assert len(after["graph"]["gens"]) == 1 and len(after["es"]["physicals"]) == 1 and after["es"]["no"] == second["ingest_no"]


def test_stopped_by_low_disk_is_not_resumed_automatically_and_the_row_stays(registered_world):
    """空きの不足で止まった回は起動時に自動で再開せず、行が残るので「途中で止まった」の表示も消えない。"""
    from _replace_row import row_for
    run_id = store.start_ingest_run(W)["id"]
    row_for(W, "", held=False)
    store.finish_ingest_run(run_id, status="failed", extraction_snapshot={
        "interrupted": True, "op": "refresh", "stop_reason": "low_disk"})

    assert resume.resume_interrupted() == []
    assert store.unfinished_state(W) == "whole" and len(_rows()) == 1


def test_resume_after_a_rebind_stopped_in_the_new_root_skips_what_was_swapped(registered_code_world, new_root):
    """付け替えの新 root の取り込みの途中（ES）で止まった再開は、新 root で入れ替え済みの派生・グラフ・目録を作り直さない。"""
    _stop_rebind(new_root, kill_on_http_send="/_bulk")
    left = _rows()[0]
    stamps, graph = _layer_stamps(), _graph_state()
    assert left["items"]["graph"] == "cleaned" and graph["no"] == left["ingest_no"]

    _resume_by_update(left, new_root)

    assert _layer_stamps() == stamps and _graph_state()["gen"] == graph["gen"] and _graph_state()["no"] == left["ingest_no"]
    assert not _backup().exists()


@pytest.mark.parametrize("stop", ["before_publish", "mid_publish", "in_graph", "after_graph", "after_es"])
def test_single_document_redo_resumes_with_the_same_number_and_a_rerun_changes_nothing(registered_world, stop):
    """1 文書の入れ直しの再開は、止まった行と同じ番号でその文書を最初からやり直す。入れ替わったグラフの番号が行の番号と一致し、もう一度「更新」しても何も変わらない。"""
    _mark_failed()
    _stop_reconvert(stop)
    left = _doc_stop_state()
    _settle_orphan()
    if stop == "mid_publish":
        store.clear_failed_docs(W)

    _submit_reconvert()
    assert run_worker_once().returncode == 0 and _rows() == []

    assert _graph_state()["no"] == left["ingest_no"]
    after = _snapshot()
    _enqueue_refresh(W)
    assert run_worker_once().returncode == 0 and _rows() == []
    assert _snapshot() == after


def test_resume_skips_the_archive_extraction_that_was_done(registered_world):
    """アーカイブの展開が済んでから止まった再開は、展開し直さず（manifest が書き換わらない）、同じ番号で残りを入れ替える。"""
    rel = _add_archive("k1")
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_http_send="/_bulk").returncode == -9
    _wait_stopped()
    left = _rows()[0]
    manifest = worlds.archive_manifest_path(W)
    assert left["items"]["archives"] == "cleaned" and manifest.is_file()
    stamp = manifest.stat().st_mtime_ns

    _resume()

    assert manifest.stat().st_mtime_ns == stamp and (worlds.archives_dir(W) / rel / "inner.txt").is_file()


def test_a_row_is_kept_until_every_item_is_cleaned(registered_world):
    """行を消してよいのは、全項目が「古いものを消した」になってから。まだ・入れ替えただけ・失敗の項目が 1 つでもあれば残す。"""
    from _replace_row import row_for
    from sherpa.ingest import replace_guard, source_scan
    from sherpa.store import replace_rows
    row = replace_guard.checked(row_for(W))
    sig, manifest = source_scan.world_state(W)
    row = replace_guard.fix_sources(row, sig, manifest, targets=("ir", "es"))
    assert replace_guard.final_check(row) is None

    replace_rows.settle_item(row, W, "ir")
    assert replace_guard.final_check(row) is None
    replace_rows.mark_failed(row, W, "es", "x")
    assert replace_guard.final_check(row) is None
    replace_rows.settle_item(row, W, "es")
    assert replace_guard.final_check(row) is not None


def test_skipped_derived_build_gives_back_the_failed_documents_saved_for_that_number(registered_world):
    """派生を飛ばす再開は、その番号の派生で作れなかった文書（古い出力を引き継いだものを含む）を行から戻す。控えが無ければ飛ばさず作り直す。"""
    from _replace_row import row_for
    from sherpa.ingest import derived_numbers, replace_guard, source_scan, swap_resume
    from sherpa.store import replace_rows
    row = replace_guard.checked(row_for(W))
    sig, manifest = source_scan.world_state(W)
    row = replace_guard.fix_sources(row, sig, manifest, targets=("ir", "rag", "md"))
    for layer in ("ir", "rag", "md"):
        derived_numbers.write(derived_numbers.layer_dir(worlds.derived_md_dir(W), layer), row, layer)
        replace_rows.before_swap(row, W, layer)
    assert swap_resume.derived_if_done(row, W) is None   # 失敗の一覧の控えが無ければ飛ばさない

    swap_resume.note_derived(row, {"rag_failures": [{"doc": "kept.xlsx", "reason": "rag_failed"}]})
    done = swap_resume.derived_if_done(row, W)

    assert done["rag_failures"] == [{"doc": "kept.xlsx", "reason": "rag_failed"}]


def test_saved_failed_documents_are_dropped_before_the_derived_build_starts_again(registered_world):
    """派生を作り直すことになったら、その前に古い失敗の一覧の控えを外す（作り直しの公開が一部失敗しても、古い控えで一覧を置き換えない）。"""
    from _replace_row import row_for
    from sherpa.ingest import derived_numbers, replace_guard, source_scan, swap_resume
    from sherpa.store import replace_rows
    row = replace_guard.checked(row_for(W))
    sig, manifest = source_scan.world_state(W)
    row = replace_guard.fix_sources(row, sig, manifest, targets=("ir", "rag", "md"))
    for layer in ("ir", "rag"):   # md 層は入れ替わっていない
        derived_numbers.write(derived_numbers.layer_dir(worlds.derived_md_dir(W), layer), row, layer)
        replace_rows.before_swap(row, W, layer)
    swap_resume.note_derived(row, {"rag_failures": []})
    assert replace_rows.FAILED_DOCS_KEY in replace_rows.read_row(row.id).settings

    assert swap_resume.derived_if_done(row, W) is None

    assert replace_rows.FAILED_DOCS_KEY not in replace_rows.read_row(row.id).settings
