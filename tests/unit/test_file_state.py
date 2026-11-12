"""「ファイルの状態を更新」が、台帳へ増えたファイルだけを足し、なくなった行だけを消し、取り込みの署名と明細を動かさないこと。

DB・資料フォルダは実物（モックは資料フォルダの場所の解決と、取り込み中の判定だけ）。
"""
from __future__ import annotations

import os
import uuid

import pytest

from sherpa import file_state, preview_service, store, tool_dispatch, worlds
from sherpa.ingest import name_rows, worker
from sherpa.store import name_ledger
from tests._replace_row import offline_row

pytestmark = pytest.mark.unit

_FILES = {
    "gen1/design/spec_a.md": "# spec A\n",
    "gen1/src/PROG1.cbl": "       PROGRAM-ID. PROG1.\n",
    "gen1/design/old.md": "# old\n",
}


@pytest.fixture
def world(monkeypatch, tmp_path):
    wid = "filestate_" + uuid.uuid4().hex[:8]
    wd = tmp_path / "world"
    for rel, text in _FILES.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    der, arc = tmp_path / "derived", tmp_path / "archives"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: arc)
    store.upsert_world(wid, str(wd), label="t")
    _ingest(wid)
    yield wid
    store.replace_documents(wid, [], row=offline_row(wid))
    store.delete_world_row(wid)


def _ingest(wid):
    """取り込みが台帳・署名・明細を書いた後と同じ状態にする。"""
    sig, manifest = worker.world_state(wid)
    store.replace_documents(wid, worker._ledger_rows(wid), row=offline_row(wid))
    store.set_world_sig(wid, sig, manifest=manifest, doc_count=len(manifest))


def _put(wid, rel, text="x\n"):
    p = worlds.world_dir(wid) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _update(wid):
    job = file_state._Job(world=wid)
    plan = file_state._begin(wid)
    return file_state._commit(wid, plan, file_state._walk(job, plan))


def _ledger(wid):
    return {r["name"]: r for r in store.list_documents(wid)}


def test_added_file_is_listed_as_partial_including_unconverted_office(world):
    _put(world, "gen1/design/new_spec.md")
    _put(world, "gen1/design/sheet.xlsx", "not really xlsx")
    res = _update(world)
    assert set(res["added"]) == {"gen1/design/new_spec.md", "gen1/design/sheet.xlsx"}
    led = _ledger(world)
    assert led["gen1/design/sheet.xlsx"]["status"] == "partial"
    assert led["gen1/design/sheet.xlsx"]["doctype"] and led["gen1/design/sheet.xlsx"]["branch"] == "office"
    view = {r["name"]: (r, s) for r, s in preview_service._read_ledger_view(world).rows}
    row, status = view["gen1/design/sheet.xlsx"]
    assert preview_service._row_with_state(row, status, {})["label"] == "一部使えます"
    listed, _d, _c, _k = tool_dispatch.run_tool("list_docs", {"state": "partial"}, world, None)
    assert {d["rel_path"] for d in listed["docs"]} == {"gen1/design/new_spec.md", "gen1/design/sheet.xlsx"}
    assert listed["partial"] == 2 and "意味の索引" in listed["partial_notice"]


def test_removed_file_row_disappears_and_unchanged_rows_are_untouched(world):
    before = _ledger(world)
    (worlds.world_dir(world) / "gen1/design/old.md").unlink()
    res = _update(world)
    assert res["removed"] == ["gen1/design/old.md"]
    after = _ledger(world)
    assert set(after) == set(before) - {"gen1/design/old.md"}
    assert all(after[n] == before[n] for n in after)


def test_name_ledger_follows_removed_and_added_files(world):
    rows = worker._ledger_rows(world)
    store.replace_documents(world, rows, names=name_rows.build_rows(rows, []), row=offline_row(world))
    (worlds.world_dir(world) / "gen1/src/PROG1.cbl").unlink()
    _put(world, "gen1/src/PROG2.cbl", "       PROGRAM-ID. PROG2.\n")
    _update(world)
    assert {(r["name"], r["rel_path"]) for r in name_ledger.read_rows(world)} == {("PROG2", "gen1/src/PROG2.cbl")}


def test_name_ledger_is_not_started_by_a_file_state_update(world):
    (worlds.world_dir(world) / "gen1/src/PROG1.cbl").unlink()
    _put(world, "gen1/src/PROG2.cbl", "       PROGRAM-ID. PROG2.\n")
    _update(world)
    assert not name_ledger.is_built(world) and name_ledger.read_rows(world) == []


def test_changed_file_is_reported_but_ledger_and_signature_stay(world):
    row = store.get_world(world)
    ledger_before = store.list_documents(world)
    stamp_before = store.ledger_stamp(world)
    p = worlds.world_dir(world) / "gen1/design/spec_a.md"
    p.write_text("# spec A changed and longer\n", encoding="utf-8")
    _put(world, "gen1/design/added.md")
    res = _update(world)
    assert res["changed"] == ["gen1/design/spec_a.md"] and res["added"] == ["gen1/design/added.md"]
    assert {r["name"]: r for r in store.list_documents(world) if r["name"] != "gen1/design/added.md"} == \
        {r["name"]: r for r in ledger_before}
    assert store.ledger_stamp(world) != stamp_before
    now = store.get_world(world)
    assert (now["last_sig"], now["last_manifest"], now["last_synced_at"]) == \
        (row["last_sig"], row["last_manifest"], row["last_synced_at"])
    assert worker.world_signature(world) != now["last_sig"]   # 次の「今すぐ更新」は変更を検知する


def test_next_full_ingest_turns_partial_into_ready(world):
    _put(world, "gen1/design/new_spec.md")
    _update(world)
    assert _ledger(world)["gen1/design/new_spec.md"]["status"] == "partial"
    _ingest(world)
    assert _ledger(world)["gen1/design/new_spec.md"]["status"] == "indexed"
    assert _update(world)["added"] == []


def test_archive_expansion_rows_are_not_reported_removed(world, tmp_path):
    _put(world, "pack.zip", "not really zip")
    arc = tmp_path / "archives" / "pack.zip"
    arc.mkdir(parents=True)
    (arc / "inner.md").write_text("# inner\n", encoding="utf-8")
    _ingest(world)
    assert "pack.zip/inner.md" in _ledger(world)
    res = _update(world)
    assert res["removed"] == [] and res["added"] == []
    assert "pack.zip/inner.md" in _ledger(world)


def test_deleted_archive_removes_its_expanded_rows_from_ledger(world, tmp_path):
    _put(world, "gen1/pack.zip", "not really zip")
    arc = tmp_path / "archives" / "gen1" / "pack.zip"
    arc.mkdir(parents=True)
    (arc / "inner.md").write_text("# inner\n", encoding="utf-8")
    _ingest(world)
    assert "gen1/pack.zip/inner.md" in _ledger(world)
    (worlds.world_dir(world) / "gen1/pack.zip").unlink()
    res = _update(world)
    assert "gen1/pack.zip/inner.md" in res["removed"]
    assert "gen1/pack.zip/inner.md" not in _ledger(world)


def test_superseded_job_does_not_write_the_ledger(world):
    _put(world, "gen1/design/new_spec.md")
    old, new = file_state._Job(world=world), file_state._Job(world=world)
    plan = file_state._begin(world)
    file_state._JOBS[world] = new
    try:
        file_state._run(old, plan)
        assert old.state == "conflict" and "gen1/design/new_spec.md" not in _ledger(world)
        file_state._run(new, plan)
        assert new.state == "ready" and "gen1/design/new_spec.md" in _ledger(world)
    finally:
        file_state._JOBS.pop(world, None)


def test_generation_change_before_write_aborts_with_409_and_writes_nothing(world):
    _put(world, "gen1/design/new_spec.md")
    (worlds.world_dir(world) / "gen1/design/old.md").unlink()
    job = file_state._Job(world=world)
    plan = file_state._begin(world)
    found = file_state._walk(job, plan)
    store.set_world_sig(world, "someone-else")          # 取り込みが先に世代を進めた
    before = _ledger(world)
    with pytest.raises(file_state.FileStateError) as e:
        file_state._commit(world, plan, found)
    assert e.value.status == 409
    assert _ledger(world) == before


def test_ledger_replaced_before_write_aborts_with_409(world):
    _put(world, "gen1/design/new_spec.md")
    job = file_state._Job(world=world)
    plan = file_state._begin(world)
    found = file_state._walk(job, plan)
    store.replace_documents(world, worker._ledger_rows(world), row=offline_row(world))
    with pytest.raises(file_state.FileStateError) as e:
        file_state._commit(world, plan, found)
    assert e.value.status == 409
    assert "gen1/design/new_spec.md" not in _ledger(world) or _ledger(world)["gen1/design/new_spec.md"]["status"] != "partial"


def test_cannot_start_while_ingesting_or_before_first_ingest(world, monkeypatch):
    monkeypatch.setattr(store, "get_latest_run_summary", lambda w, **k: {"status": "extracting"})
    with pytest.raises(file_state.FileStateError) as e:
        file_state._begin(world)
    assert e.value.status == 409
    monkeypatch.undo()
    store.set_world_sig(world, "")
    with pytest.raises(file_state.FileStateError) as e:
        file_state._begin(world)
    assert e.value.status == 409


def test_unreachable_walk_does_not_wipe_the_ledger(world, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: empty)
    job = file_state._Job(world=world)
    plan = file_state._begin(world)
    plan.root = str(empty)
    with pytest.raises(file_state.FileStateError):
        file_state._walk(job, plan)
    assert os.path.isdir(empty) and len(_ledger(world)) == 3


def test_stuck_job_reports_failure_and_never_starts_a_second_thread(world, monkeypatch):
    import time
    started = []
    monkeypatch.setattr(file_state.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: started.append(kw)})())
    stuck = file_state._Job(world=world, started=time.monotonic() - 700)
    file_state._JOBS[world] = stuck
    try:
        for start in (False, True, True):
            res = file_state.request(world, start=start)
            assert res["state"] == "failed" and res["message"] == file_state.MSG_STUCK
        assert file_state._JOBS[world] is stuck and started == []
        stuck.finished = time.monotonic()
        assert file_state.request(world, start=True)["state"] == "scanning"
        assert file_state._JOBS[world] is not stuck and len(started) == 1
    finally:
        file_state._JOBS.pop(world, None)


def test_job_past_deadline_does_not_write_the_ledger_when_it_wakes(world):
    import time
    _put(world, "gen1/design/new_spec.md")
    late = file_state._Job(world=world, started=time.monotonic() - 700)
    file_state._JOBS[world] = late
    plan = file_state._begin(world)
    try:
        file_state._run(late, plan)
        assert late.state == "conflict" and "gen1/design/new_spec.md" not in _ledger(world)
    finally:
        file_state._JOBS.pop(world, None)


def test_partial_notice_says_legacy_office_cannot_be_opened_until_converted(world):
    _put(world, "gen1/design/old_sheet.xls", "not really xls")
    file_state_res = _update(world)
    assert file_state_res["added"] == ["gen1/design/old_sheet.xls"]
    listed = tool_dispatch.run_tool("list_docs", {"state": "partial"}, world, None)[0]
    assert "旧形式" in listed["partial_notice"] and "開けない" in listed["partial_notice"]


def test_new_archive_not_listed_is_counted_as_skipped_new(world):
    _put(world, "gen1/design/bundle.zip", "not really zip")
    res = _update(world)
    assert res["added_total"] == 0 and res["skipped_new_total"] == 1


def test_world_ingested_empty_can_add_its_first_file(world):
    for rel in _FILES:
        (worlds.world_dir(world) / rel).unlink()
    _ingest(world)
    assert store.get_world(world)["last_manifest"] == {}
    _put(world, "gen1/design/first.md")
    assert "gen1/design/first.md" in _update(world)["added"]
