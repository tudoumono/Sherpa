"""入れ替えの行・行を消す印は、決まった入口でしか手に入らず、成功の記録は消す印を必ず受け取る。"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from sherpa import store, worlds
from sherpa.ingest import archive_extract, derived_swap, ledger_step, office_md, wipe, world_neo4j
from sherpa.store import name_ledger, ocr_jobs, replace_rows


def test_replace_row_cannot_be_built_outside_the_module():
    with pytest.raises(TypeError):
        replace_rows.ReplaceRow(id=1, world="w", doc="", kind="build", ingest_no=1, lock_ns=1, lock_id=1,
                                source_state="collecting", source_sig=None, settings={}, items={},
                                old_things={}, rebuild_reason=None)


def test_drop_token_cannot_be_built_outside_the_module():
    with pytest.raises(TypeError):
        replace_rows.DropToken(1, 1, 1)


@pytest.mark.parametrize("fn", [store.finish_ingest_run_and_confirm_world, store.finish_ingest_run_and_drop_row])
def test_finishing_a_run_requires_drop_without_default(fn):
    p = inspect.signature(fn).parameters["drop"]
    assert p.default is inspect.Parameter.empty and p.kind is inspect.Parameter.KEYWORD_ONLY


def test_replace_aborted_passes_through_broad_except_exception():
    assert not issubclass(replace_rows.ReplaceAborted, Exception)
    assert issubclass(replace_rows.SourceChanged, replace_rows.ReplaceAborted)
    assert issubclass(replace_rows.RowLockLost, replace_rows.ReplaceAborted)


@pytest.mark.parametrize("fn", [
    store.replace_documents, store.replace_document, ledger_step.write_ledger_step, ledger_step.stage_ledger_step,
    ledger_step.switch_ledger_step, store.stage_ledger, store.switch_ledger, store.purge_stale_ledger,
    name_ledger.write_in, name_ledger.clear_in, name_ledger.replace_rows,
    world_neo4j.load_world, world_neo4j.delete_world,
    office_md.build_derived, office_md.derive_one, office_md.publish_one, office_md.restore_reconvert_backups,
    office_md.drop_conversion_cache, office_md.refresh_human_md, office_md.refresh_document_ir,
    office_md.refresh_evidence_ir, office_md.refresh_rag, office_md.refresh_ocr_routes,
    derived_swap._publish_staging, archive_extract.sync_world_archives, archive_extract.extract_archive,
    archive_extract.sync_step, wipe.wipe_locked, ocr_jobs.purge_world, worlds._set_aside_derived])
def test_swap_writers_require_row_without_default(fn):
    p = inspect.signature(fn).parameters["row"]
    assert p.default is inspect.Parameter.empty and p.kind is inspect.Parameter.KEYWORD_ONLY


def test_before_swap_refuses_a_row_of_another_world_and_records_offline():
    from tests._replace_row import offline_row
    row = offline_row("w")
    replace_rows.before_swap(row, "w", "graph", old=["g1"])
    assert row._offline == ["graph"]
    with pytest.raises(ValueError):
        replace_rows.before_swap(row, "other", "graph")
    with pytest.raises(ValueError):
        replace_rows.after_old_removed(row, "other", "graph")


def test_settings_sig_is_stable_for_the_same_settings_and_differs_by_item():
    from tests._replace_row import offline_row
    a = offline_row("w", settings={"graph": {"k": 1, "j": 2}, "es": {"k": 1}})
    b = offline_row("w", settings={"graph": {"j": 2, "k": 1}})
    assert replace_rows.settings_sig(a, "graph") == replace_rows.settings_sig(b, "graph")
    assert replace_rows.settings_sig(a, "graph") != replace_rows.settings_sig(a, "es")


def test_ledger_tables_are_written_only_by_the_store_modules():
    root = pathlib.Path(replace_rows.__file__).resolve().parents[1]
    sql = re.compile(r"(INSERT INTO|DELETE FROM|UPDATE|TRUNCATE)\s+(documents|name_ledger|name_ledger_built)\b")
    allowed = {root / "store" / "documents.py", root / "store" / "name_ledger.py", root / "store" / "ledger_stage.py"}
    outside = [str(p.relative_to(root)) for p in root.rglob("*.py") if p not in allowed and sql.search(p.read_text(encoding="utf-8"))]
    assert outside == []


def test_offline_row_is_not_used_by_the_product_code():
    root = pathlib.Path(replace_rows.__file__).resolve().parents[1]
    users = [str(p.relative_to(root)) for p in root.rglob("*.py")
             if p.name != "replace_rows.py" and re.search(r"\b(offline_row_for_test|row_for_test)\b", p.read_text(encoding="utf-8"))]
    assert users == []


def test_become_delete_keeps_old_things_and_held_row_converts_when_delete_is_asked():
    """消す仕事に変えても古いものの控えは残る。`held_row(kind="delete")` は拾った行も delete に変えて渡す。"""
    import contextlib

    from sherpa.ingest import replace_guard
    from tests._replace_row import offline_row
    row = offline_row("w", old_things={"graph": ["g1"]}, source_state="fixed")
    gone = replace_rows.become_delete(row)
    assert gone.kind == "delete" and gone.source_state == "not_used" and gone.old_things == {"graph": ["g1"]}
    assert replace_rows.become_delete(gone) is gone

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(replace_guard, "row_for_run", lambda world, run_id: row)
        with contextlib.ExitStack() as stack:
            assert stack.enter_context(replace_guard.held_row("w", 1, kind="delete")).kind == "delete"
        assert stack.enter_context(replace_guard.held_row("w", 1)).kind == "build"


def test_check_held_refuses_a_row_of_another_world():
    from tests._replace_row import offline_row
    row = offline_row("w")
    replace_rows.check_held_in(None, row, "w")      # 試験の行は DB を見ない
    with pytest.raises(ValueError):
        replace_rows.check_held_in(None, row, "other")
