"""入れ替えの途中で原本や設定が変わったときの確認（要 Postgres＋Neo4j＋ES）。子プロセスで本物の worker を動かし、原本の更新時刻は子の `_ingest_crash` の仕掛けで途中に進める。土台は `test_ingest_stop_matrix` と共用する。"""
from __future__ import annotations

import os
import pathlib
import time

import pytest
from _ingest_crash import run_worker_once
from test_ingest_stop_matrix import (  # noqa: F401  (registered_world は fixture)
    W, _change_source, _enqueue_refresh, _rows, _run, _settle_orphan, registered_world)

from sherpa import store
from sherpa.ingest import background, replace_guard, resume, source_guard
from sherpa.store import replace_rows
from sherpa.store.replace_rows import SourceChanged

OK = ("auto_published", "auto_published_with_flags")


def _root() -> pathlib.Path:
    return pathlib.Path(store.get_world(W)["root_path"])


def _new_source(tag: str) -> pathlib.Path:
    return pathlib.Path(_change_source(tag)) / f"extra_{tag}.xlsx"


def _fixed_row():
    """実物の DB に確定済みの行を作り、確かめを付けて返す（今の原本の署名・設定の値を確定する）。"""
    from _replace_row import row_for
    from sherpa.ingest import source_scan
    row = replace_guard.checked(row_for(W))
    sig, manifest = source_scan.world_state(W)
    return replace_guard.fix_sources(row, sig, manifest)


def _reason(row_id: int) -> str | None:
    with store.db._connect() as c:
        return c.execute("SELECT rebuild_reason FROM ingest_replacements WHERE id=%s", (row_id,)).fetchone()["rebuild_reason"]


def test_source_changed_after_the_scan_stops_before_swapping_and_is_not_resumed(registered_world):
    """走査の後で原本が変わると、入れ替えの前に止まり、行に理由が残る。回は中断でなく失敗で終わり、自動では再開しない。次の「更新」は入れ替えきって行を消す。"""
    src = _new_source("c1")
    run_id = _enqueue_refresh(W)
    child = run_worker_once(touch_file=str(src), touch_on="open:.staging")
    assert child.returncode == 0, child.stderr[-2000:]

    assert _run(run_id)["status"] == "failed"
    left = _rows()
    assert len(left) == 1 and left[0]["source_state"] == "fixed" and left[0]["items"].get("es") == "pending"
    assert "原本が変わりました" in _reason(left[0]["id"])
    assert (_run(run_id).get("extraction_snapshot") or {}).get("interrupted") is not True
    assert resume.resume_interrupted() == []

    _settle_orphan()
    run2 = _enqueue_refresh(W)
    assert run_worker_once().returncode == 0
    assert _run(run2)["status"] in OK and _rows() == []


def test_source_restored_with_a_different_update_time_is_not_missed(registered_world):
    """原本を変えて同じ中身に戻しても、更新時刻が違えば見逃さず、入れ替えの前に止まって行に理由が残る。"""
    row = _fixed_row()
    target = _root() / "sample.xlsx"
    original = target.read_bytes()
    time.sleep(0.05)
    target.write_bytes(b"changed")
    time.sleep(0.05)
    target.write_bytes(original)

    with pytest.raises(SourceChanged):
        replace_rows.before_swap(row, W, "ir")
    assert _reason(row.id)


def _set_setting(key: str, value) -> None:
    store.set_system_settings(None, {key: value})
    store.settings._system_settings_cache = None


def test_settings_changed_to_b_and_back_to_a_never_build_with_b(registered_world, monkeypatch):
    """設定を A→B→A と変えても、実行の間は始めに読んだ A に固定され、B のときは入れ替えに入れない。A に戻れば入れ替えに入れる（legacy_backend を含む）。"""
    from sherpa.ingest.arms import legacy_convert
    monkeypatch.setattr(legacy_convert, "soffice_available", lambda: True)
    _set_setting("legacy_backend", "none")
    try:
        with replace_guard.settings_pinned():
            row = _fixed_row()
            _set_setting("legacy_backend", "libreoffice")
            assert store.get_system_settings()["legacy_backend"] == "none"     # 実行の間は A のまま
            with pytest.raises(SourceChanged):
                replace_rows.before_swap(row, W, "md")
            _set_setting("legacy_backend", "none")
            row2 = replace_guard.checked(replace_rows.read_row(row.id))
            replace_rows.before_swap(row2, W, "md")
    finally:
        _set_setting("legacy_backend", None)


def test_source_changed_before_the_es_stage_fails_the_run_instead_of_publishing_with_flags(registered_world):
    """ES の段に入る前に原本が変わると、回は失敗で終わり、auto_published_with_flags にならない。ES は入れ替わらず行が残る。"""
    src = _new_source("c2")
    run_id = _enqueue_refresh(W)
    child = run_worker_once(touch_file=str(src), touch_on="http")
    assert child.returncode == 0, child.stderr[-2000:]

    assert _run(run_id)["status"] == "failed"
    left = _rows()
    assert len(left) == 1 and left[0]["items"].get("es") != "swapped"


def test_rule_only_regeneration_keeps_its_row_when_the_source_changes_during_it(registered_world):
    """規則版への再生成の途中で原本が変わると、行を消す直前の確かめで止まり、行が残る。"""
    src = _root() / "sample.xlsx"
    run_id, _ = background.submit(W, "rag_regenerate_rules", "{}")
    child = run_worker_once(touch_file=str(src), touch_on="http")
    assert child.returncode == 0, child.stderr[-2000:]

    assert _run(run_id)["status"] == "failed"
    assert len(_rows()) == 1 and store.unfinished_state(W) == "whole" and _reason(_rows()[0]["id"])


def test_unchanged_branch_keeps_its_row_when_the_source_changes_during_it(registered_world):
    """変更なしの更新の途中で原本が変わると、行を消す直前の確かめで止まり、行が残る。"""
    src = _root() / "sample.xlsx"
    run_id = _enqueue_refresh(W)
    child = run_worker_once(touch_file=str(src), touch_on="http")
    assert child.returncode == 0, child.stderr[-2000:]

    assert _run(run_id)["status"] == "failed"
    assert len(_rows()) == 1 and store.unfinished_state(W) == "whole" and _reason(_rows()[0]["id"])


def test_single_document_redo_keeps_its_row_when_the_source_changes_right_after_the_es_step(registered_world):
    """1 文書の入れ直しの ES の反映の直後に原本が変わると、行を消す直前の確かめで止まり、その文書の行が残る。"""
    import psycopg
    from _ingest_crash import start_worker_once
    from test_ingest_stop_swap import REL, _mark_failed, _submit_reconvert, _wait_blocked_by_pg_trigger
    from sherpa.store import db
    _mark_failed()
    holder = psycopg.connect(db._dsn(), autocommit=True)
    holder.execute("SELECT pg_advisory_lock(7102, 9)")
    with db._connect() as c:
        c.execute("CREATE OR REPLACE FUNCTION stop_swap_wait() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                  "PERFORM pg_advisory_lock(7102, 9); PERFORM pg_advisory_unlock(7102, 9); RETURN OLD; END $$")
        c.execute("CREATE TRIGGER stop_swap_trigger BEFORE DELETE ON world_failed_docs FOR EACH ROW "
                  f"WHEN (OLD.rel = '{REL}') EXECUTE FUNCTION stop_swap_wait()")
    proc = None
    try:
        run_id = _submit_reconvert()
        proc = start_worker_once()
        _wait_blocked_by_pg_trigger(proc, "world_failed_docs", "DELETE")
        target = _root() / REL
        st = target.stat()
        os.utime(target, (st.st_atime, st.st_mtime + 100))
        holder.execute("SELECT pg_advisory_unlock(7102, 9)")
        proc.wait(timeout=120)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        holder.close()
        with db._connect() as c:
            c.execute("DROP TRIGGER IF EXISTS stop_swap_trigger ON world_failed_docs")
            c.execute("DROP FUNCTION IF EXISTS stop_swap_wait()")

    assert _run(run_id)["status"] == "failed"
    left = _rows()
    assert len(left) == 1 and left[0]["doc"] == REL and left[0]["items"]["es"] == "cleaned"
    assert store.unfinished_state(W) == "doc" and _reason(left[0]["id"])


def test_file_added_after_the_scan_is_not_missed_by_the_checks(registered_world):
    """取り込みの冒頭の列挙を使い回している間に足されたファイルも、入れ替えの前の確かめが見逃さない。"""
    from _replace_row import row_for
    from sherpa import scope_infer
    from sherpa.ingest import source_scan
    with scope_infer.walk_reuse_scope():
        row = replace_guard.checked(row_for(W))
        state = source_scan.world_state(W)
        scope_infer.reuse_walk(state.scan.walk)
        replace_guard.fix_sources(row, state[0], state[1])
        _new_source("late")
        with pytest.raises(SourceChanged):
            replace_rows.before_swap(row, W, "ir")
        assert _reason(row.id)


def test_document_inside_an_archive_is_compared_with_its_archive_not_rejected(registered_world):
    """アーカイブの中の文書の入れ直しは、その文書が入っているアーカイブの原本の明細と比べる（原本が変わっていなければ止めない・アーカイブが変われば止める）。"""
    import zipfile

    from _replace_row import row_for
    rel = "pack.zip/inner/doc.txt"
    arc = _root() / "pack.zip"
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("inner/doc.txt", "x")
    st = arc.stat()
    row = replace_guard.checked(row_for(W, rel))
    replace_rows.fix_sources(row, "sig", {"pack.zip": [st.st_mtime_ns, st.st_ctime_ns, st.st_size]}, {}, targets=("ir",))

    replace_rows.before_swap(row, W, "ir")

    os.utime(arc, (st.st_atime, st.st_mtime + 100))
    row2 = replace_guard.checked(replace_rows.read_row(row.id))
    with pytest.raises(SourceChanged):
        replace_rows.before_swap(row2, W, "rag")


def test_numbers_written_on_made_things_carry_the_settings_confirmed_on_the_row(registered_world):
    """作ったものに書く設定の値は、行に確定した設定の値と同じ（確定前の行を渡し続けて空の設定のハッシュを書かない）。"""
    from sherpa import worlds
    from sherpa.ingest import derived_numbers
    _change_source("n1")
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_http_send="/_bulk").returncode == -9
    left = replace_rows.read_row(_rows()[0]["id"])
    assert left.settings.get("rag")
    for layer in ("ir", "rag", "md"):
        rec = derived_numbers.read(derived_numbers.layer_dir(worlds.derived_md_dir(W), layer))
        assert rec == {"ingest_no": left.ingest_no, "settings_sig": replace_rows.settings_sig(left, layer)}
