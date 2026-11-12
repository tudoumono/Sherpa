"""取り込みの途中で止まった・失敗した場面の確認（本物の worker を子プロセスで動かす・要 Postgres＋Neo4j＋ES）。

製品の関数は差し替えない。失敗は `_ingest_crash` の子プロセスで、`.human_md_es_sig` への書き込みだけを失敗させて作る。
"""
from __future__ import annotations

import pathlib
import shutil
import tempfile

import openpyxl
import pytest
from _ingest_crash import run_worker_once
from _world_setup import driver

from sherpa import store, worlds
from sherpa.ingest import background
from sherpa.store import name_ledger

W = "test_stop_matrix"
MARKER = ".human_md_es_sig"


def _make_root() -> str:
    root = tempfile.mkdtemp()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["B1"] = "No", "内容"
    ws["A2"], ws["B2"] = 1, "サンプル内容"
    wb.save(pathlib.Path(root) / "sample.xlsx")
    return root


@pytest.fixture
def registered_world():
    root = _make_root()
    drv = driver()
    try:
        reg = worlds.register(W, str(pathlib.Path(root).resolve()))
        assert reg["status"] in ("auto_published", "auto_published_with_flags")
        yield W
    finally:
        try:
            worlds.delete(W)
        except Exception:
            pass
        store.delete_world_row(W)
        with drv.session() as s:
            s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=W)
        drv.close()
        shutil.rmtree(root, ignore_errors=True)


def _enqueue_refresh(world: str) -> int:
    run_id, _joined = background.submit(world, "refresh", "{}")
    return run_id


def _run(run_id: int) -> dict:
    return next(r for r in store.list_ingest_runs(W) if r["id"] == run_id)


def test_marker_write_failure_in_es_self_heal_fails_the_run(registered_world):
    """ES の自己修復が印を書けなかった回は、成功で終わらず failed で終わる。"""
    marker = worlds.derived_md_dir(W) / MARKER
    assert marker.is_file()
    marker.unlink()

    run_id = _enqueue_refresh(W)
    child = run_worker_once(fail_open_suffix=MARKER)

    assert child.returncode == 0, child.stderr[-2000:]
    run = _run(run_id)
    assert run["status"] == "failed"
    assert not marker.exists()


def test_marker_write_failure_in_derived_refresh_adds_no_extra_failed_run(registered_world):
    """派生物の軽量再生成の中で印が書けなくても、受付の回より新しい failed の回は増えない。"""
    dmd = worlds.derived_md_dir(W)
    (dmd / MARKER).unlink()
    rag_marker = dmd / ".rag_sig"
    rag_marker.write_text(rag_marker.read_text(encoding="utf-8") + ";bumped", encoding="utf-8")

    run_id = _enqueue_refresh(W)
    child = run_worker_once(fail_open_suffix=MARKER)

    assert child.returncode == 0, child.stderr[-2000:]
    runs = store.list_ingest_runs(W)
    assert max(r["id"] for r in runs) == run_id
    assert _run(run_id)["status"] == "failed"


def test_marker_write_failure_in_derived_refresh_is_cleared_when_the_es_rebuild_writes_it(registered_world):
    """軽量再生成の中の印の失敗は、同じ回の後の ES の作り直しが印まで書けたら失敗に残さない。"""
    dmd = worlds.derived_md_dir(W)
    (dmd / MARKER).unlink()
    rag_marker = dmd / ".rag_sig"
    rag_marker.write_text(rag_marker.read_text(encoding="utf-8") + ";bumped", encoding="utf-8")

    run_id = _enqueue_refresh(W)
    child = run_worker_once(fail_open_suffix=MARKER, fail_times=1)

    assert child.returncode == 0, child.stderr[-2000:]
    assert max(r["id"] for r in store.list_ingest_runs(W)) == run_id
    assert _run(run_id)["status"] == "auto_published"
    assert (dmd / MARKER).is_file()


# ---- 入れ替えの行（回ごとの lock と行の一生） ----------------------------------------------------

def _rows(world: str = W) -> list:
    from sherpa.store import db
    with db._connect() as c:
        return c.execute("SELECT id, doc, kind, ingest_no, source_state, source_sig, items, old_things FROM ingest_replacements "
                         "WHERE kb_id=%s AND world=%s ORDER BY doc", (db._KB_ID, world)).fetchall()


def _change_source(tag: str) -> str:
    root = store.get_world(W)["root_path"]
    wb = openpyxl.Workbook()
    wb.active["A1"] = tag
    wb.save(pathlib.Path(root) / f"extra_{tag}.xlsx")
    return root


def _settle_orphan() -> None:
    store.downgrade_orphaned_extracting_runs(W)


def test_successful_refresh_leaves_no_row(registered_world):
    """「更新」が成功すれば、拾った回が作った行は同じ回の終わりに消える。"""
    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in ("auto_published", "auto_published_with_flags")
    assert _rows() == []


def test_kill_while_collecting_keeps_row_and_resume_inherits_number(registered_world):
    """原本を集めている間に止まった行は collecting で残り、再開は同じ番号のまま走査からやり直して、成功で行が消える。"""
    root = _change_source("a")
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_scan_of=root).returncode == -9
    first = _rows()
    assert len(first) == 1 and first[0]["source_state"] == "collecting" and first[0]["doc"] == ""
    _settle_orphan()

    _enqueue_refresh(W)
    assert run_worker_once(kill_on_scan_of=root).returncode == -9
    again = _rows()
    assert len(again) == 1 and again[0]["ingest_no"] == first[0]["ingest_no"]
    _settle_orphan()

    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in ("auto_published", "auto_published_with_flags")
    assert _rows() == []


def test_kill_during_es_bulk_leaves_fixed_row_and_same_signature_refresh_replaces_everything(registered_world):
    """ES の書き込みの途中で止まった行は確定済みで残る。署名が同じ「更新」も変更なしで済まさず、入れ替えてから行を消す。"""
    _change_source("b")
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_http_send="/_bulk").returncode == -9
    left = _rows()
    assert len(left) == 1 and left[0]["source_state"] == "fixed" and left[0]["source_sig"]
    assert set(left[0]["items"]) == {"archives", "ir", "rag", "md", "graph", "ledger", "es"}
    _settle_orphan()
    store.set_world_sig(W, left[0]["source_sig"])      # 原本が行の署名のままなら、署名だけでは「変更なし」に見える状態

    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in ("auto_published", "auto_published_with_flags")
    assert _rows() == []
    assert store.get_world(W)["last_sig"] == left[0]["source_sig"]


def test_delete_over_a_stopped_build_row_removes_all_rows(registered_world):
    """止まった build の行が残る資料フォルダを消すと、行は delete に変わり、world の行と全部の行が消える。"""
    from _ingest_queue import drain_ingest_queue
    _change_source("c")
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_http_send="/_bulk").returncode == -9
    assert [r["kind"] for r in _rows()] == ["build"]
    _settle_orphan()
    store.set_world_sig(W, _rows()[0]["source_sig"])

    from sherpa.ingest.background import submit
    submit(W, "delete", "{}", {"uid": None})
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert store.get_world(W) is None
    assert _rows() == []
    assert drain_ingest_queue() == 0


def test_whole_run_replaces_leftover_single_doc_row(registered_world):
    """1 文書の行が残る資料フォルダで全体の取り込みを始めると、走査の確定で全体の行にまとまり、行は 2 つにならない。"""
    from _replace_row import row_for
    row_for(W, "sub/a.xlsx")
    _change_source("d")
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_http_send="/_bulk").returncode == -9
    left = _rows()
    assert [(r["doc"], r["source_state"]) for r in left] == [("", "fixed")]


@pytest.mark.parametrize("stop", ["scan", "es_bulk"])
def test_stopped_run_is_reported_before_a_new_worker_starts(registered_world, stop):
    """止まった直後（新しい worker の起動前）でも、状態は「途中で止まった」を返し、実行中のままで操作が止まらない。"""
    from sherpa.routers import worlds as api
    root = _change_source(f"s_{stop}")
    _enqueue_refresh(W)
    kill = {"kill_on_scan_of": root} if stop == "scan" else {"kill_on_http_send": "/_bulk"}
    assert run_worker_once(**kill).returncode == -9

    state = api._run_state(W)
    summary = api._ingest_summary(W, store.get_world(W), state)

    assert summary["last_run_status"] == "failed" and summary["ingest_active"] is False
    assert "取り込みの途中で止まったため、一部の検索が古い内容のままです" in summary["last_failure_message"]
    assert {k for k, a in summary["actions"].items() if not a["allowed"]} == {"rag_regenerate_rules", "reconvert"}


def test_refused_reconvert_leaves_no_row(registered_world):
    """何も入れ替えないうちに断った再変換は、その回が作った行を回の終わりと同じ記録で消す。"""
    from sherpa.ingest.background import submit
    run_id, _ = submit(W, "reconvert", '{"rel": "missing.xlsx"}', {"rel": "missing.xlsx", "uid": None})

    child = run_worker_once()

    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] == "failed"
    assert _rows() == []


def test_refused_reconvert_keeps_a_row_inherited_from_a_stopped_run(registered_world):
    """止まった回から受け継いだ行は、断った再変換では消えない。"""
    from sherpa.ingest.background import submit
    _change_source("e")
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_http_send="/_bulk").returncode == -9
    _settle_orphan()
    left = _rows()

    submit(W, "reconvert", '{"rel": "missing.xlsx"}', {"rel": "missing.xlsx", "uid": None})
    assert run_worker_once().returncode == 0

    assert [(r["id"], r["source_state"]) for r in _rows()] == [(left[0]["id"], "fixed")]


def test_stale_worker_cannot_finish_a_run_closed_as_dead(registered_world):
    """lock を失って別に failed で閉じられた回は、古い worker が成功で閉じ直せない。資料フォルダの確定も行の削除もされない。"""
    from _replace_row import row_for
    from sherpa.store.replace_rows import RowLockLost, make_drop

    before = store.get_world(W)["last_sig"]
    run_id = store.start_ingest_run(W)["id"]
    row = row_for(W, "", lock_id=run_id)
    assert store.fail_close_if_extracting(run_id, reason="lock_lost", interrupted=True) is True

    with pytest.raises(RowLockLost):
        store.finish_ingest_run_and_drop_row(run_id, drop=make_drop(row), status="auto_published")
    with pytest.raises(RowLockLost):
        store.finish_ingest_run_and_confirm_world(run_id, W, status="auto_published", sig="stale-sig",
                                                  drop=make_drop(row))
    assert store.finish_ingest_run(run_id, status="auto_published") is None

    assert _run(run_id)["status"] == "failed"
    assert store.get_world(W)["last_sig"] == before
    assert [r["id"] for r in _rows()] == [row.id]


def test_unexpected_error_before_scan_leaves_no_row(registered_world):
    """走査より前の想定外の例外で終わった回は、その回が作った未変更の行を失敗の記録と同じ記録で消す。"""
    run_id, _ = background.submit(W, "no_such_op", "{}")

    child = run_worker_once()

    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] == "failed"
    assert _rows() == []


# ---- 回の外の経路（規則版・LLM での書き換え・孤児の掃除・回を拾わない呼び出し） ----------------------------

def test_run_without_a_claimed_run_leaves_no_row(registered_world):
    """回を拾わずに呼ぶ取り込み（登録）も、成功すれば自分が持った行を残さない。"""
    assert _rows() == []


def test_kill_after_rule_only_regeneration_leaves_fixed_row_and_update_replaces_everything(registered_world):
    """規則版への再生成が書き換えた後（ES の書き込みの瞬間）に止まった行は確定済みで残り、「途中で止まった」が出る。「更新」が入れ替えて行を消す。"""
    from sherpa.routers import worlds as api
    background.submit(W, "rag_regenerate_rules", "{}")
    assert run_worker_once(kill_on_http_send="/_bulk").returncode == -9

    left = _rows()
    assert len(left) == 1 and left[0]["source_state"] == "fixed" and left[0]["source_sig"]
    assert set(left[0]["items"]) == {"rag", "graph", "es"}
    summary = api._ingest_summary(W, store.get_world(W), api._run_state(W))
    assert "取り込みの途中で止まったため" in summary["last_failure_message"]
    _settle_orphan()

    refresh_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(refresh_id)["status"] in ("auto_published", "auto_published_with_flags")
    assert _rows() == []


def test_rule_only_regeneration_success_leaves_no_row(registered_world):
    run_id, _ = background.submit(W, "rag_regenerate_rules", "{}")
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in ("auto_published", "auto_published_with_flags")
    assert _rows() == []


def test_rewrite_is_skipped_and_keeps_every_row_while_a_row_remains(registered_world):
    """止まった行が残る間は、書き換えを見送り、行を消さない（LLM が有効でも無効でも）。"""
    from _replace_row import row_for
    from sherpa.ingest import llm_render, worker
    left = row_for(W)

    def _must_not_run(*a, **kw):
        raise AssertionError("行が残っている間は書き換えない")

    cfg = {"provider": "p", "model": "m"}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(llm_render, "run_world_pass", _must_not_run)
        for prepared in (cfg, None):
            mp.setattr(llm_render, "prepare", lambda world, settings=None, _c=prepared: _c)
            worker.llm_render_pass(W)
            assert [r["id"] for r in _rows()] == [left.id]


def test_rewrite_holds_its_own_row_while_running_and_drops_it_when_done(registered_world):
    """書き換えの間は自分の lock と確定済みの行を持ち（止まったとは見えない）、終われば自分の行だけを消す。"""
    from sherpa.ingest import llm_render, replace_guard, worker
    seen = {}

    class _Result:
        changed_rels: list = []

    def _pass(world, *, cfg, row, should_stop=None):
        seen["rows"] = _rows()
        seen["unfinished"] = store.unfinished_state(W)
        return _Result()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(llm_render, "prepare", lambda world, settings=None: {"provider": "p", "model": "m"})
        mp.setattr(llm_render, "run_world_pass", _pass)
        worker.llm_render_pass(W)

    assert [(r["source_state"], sorted(r["items"])) for r in seen["rows"]] == [("fixed", ["es", "graph", "rag"])]
    assert seen["unfinished"] is None
    assert _rows() == []
    assert replace_guard.row_for_run(W, 0) is None


def test_orphan_cleanup_skips_a_cleanup_in_progress_and_ignores_other_rows(registered_world):
    """孤児の文書台帳の掃除は、他が掃除中なら飛ばし、止まった掃除の行は引き継いで消し、別の資料フォルダの行では例外にならない。"""
    from _replace_row import offline_row, row_for
    from sherpa import reconcile
    from sherpa.store import replace_rows
    orphan = "test_stop_matrix_orphan"
    other = row_for(W, held=False)
    store.replace_documents(orphan, [{"name": "a.txt", "layer": "version", "status": "indexed"}], row=offline_row(orphan))
    valid = set(store.list_document_worlds()) - {orphan}

    keep_derived = [reconcile._derived_parent()]   # 派生物の置き場は触らない（実環境と共有）
    failed: list = []
    with replace_rows.begin_cleanup(orphan) as held:
        assert held is not None
        assert reconcile._reconcile_worlds(valid, keep_derived, False, failed)["documents"] == []
        assert orphan in store.list_document_worlds()

    row_for(orphan, kind="cleanup", held=False)
    assert reconcile._reconcile_worlds(valid, keep_derived, False, failed)["documents"] == [orphan]
    assert failed == []
    assert orphan not in store.list_document_worlds()
    assert [r["id"] for r in _rows()] == [other.id]
    assert _rows(orphan) == []


# ---- 目録と名前の台帳 ---------------------------------------------------------------------------

_STOP_LOCK = (7101, 9)


def _ledger_numbers() -> dict:
    from sherpa.store import db
    with db._connect() as c:
        docs = c.execute("SELECT DISTINCT ingest_no FROM documents WHERE kb_id=%s AND version=%s",
                         (db._KB_ID, W)).fetchall()
        names = c.execute("SELECT DISTINCT ingest_no FROM name_ledger WHERE world=%s", (W,)).fetchall()
        built = c.execute("SELECT ingest_no FROM name_ledger_built WHERE world=%s", (W,)).fetchone()
        shown = c.execute("SELECT shown_ingest_no FROM worlds WHERE kb_id=%s AND world_id=%s",
                          (db._KB_ID, W)).fetchone()
    return {"docs": sorted(r["ingest_no"] for r in docs), "names": sorted(r["ingest_no"] for r in names),
            "built": built and built["ingest_no"], "shown": shown["shown_ingest_no"]}


def test_kill_inside_ledger_swap_keeps_row_and_leaves_both_ledgers_untouched(registered_world):
    """目録の DELETE で止めて SIGKILL → 行は確定済みのまま残り、目録・名前の台帳・見せている番号は古いまま食い違わない。再開は同じ番号で入れ替える。"""
    import time

    import psycopg
    from _ingest_crash import start_worker_once
    from sherpa.store import db
    _change_source("g")
    before = _ledger_numbers()
    assert len(before["docs"]) == 1 and before["docs"] == [before["shown"]] and before["built"] == before["shown"]

    holder = psycopg.connect(db._dsn(), autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s, %s)", _STOP_LOCK)
    with db._connect() as c:
        c.execute("CREATE OR REPLACE FUNCTION stop_matrix_wait() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                  f"PERFORM pg_advisory_lock({_STOP_LOCK[0]}, {_STOP_LOCK[1]}); "
                  f"PERFORM pg_advisory_unlock({_STOP_LOCK[0]}, {_STOP_LOCK[1]}); RETURN NULL; END $$")
        c.execute("CREATE TRIGGER stop_matrix_documents_delete BEFORE DELETE ON documents "
                  "FOR EACH STATEMENT EXECUTE FUNCTION stop_matrix_wait()")
    proc = None
    try:
        _enqueue_refresh(W)
        proc = start_worker_once()
        deadline = time.monotonic() + 120
        while True:
            with db._connect() as c:
                waiting = c.execute("SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted "
                                    "AND classid=%s AND objid=%s", _STOP_LOCK).fetchone()
            if waiting:
                break
            assert proc.poll() is None, proc.communicate()[1][-2000:]
            assert time.monotonic() < deadline, "目録の DELETE に達しませんでした"
            time.sleep(0.2)
        proc.kill()
        proc.wait(timeout=30)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        holder.execute("SELECT pg_advisory_unlock(%s, %s)", _STOP_LOCK)
        holder.close()
        with db._connect() as c:
            c.execute("DROP TRIGGER IF EXISTS stop_matrix_documents_delete ON documents")
            c.execute("DROP FUNCTION IF EXISTS stop_matrix_wait()")

    deadline = time.monotonic() + 30
    while store.unfinished_state(W) != "whole" and time.monotonic() < deadline:
        time.sleep(0.2)
    left = _rows()
    assert store.unfinished_state(W) == "whole"
    assert len(left) == 1 and left[0]["source_state"] == "fixed" and left[0]["items"]["ledger"] == "pending"
    assert _ledger_numbers() == before
    _settle_orphan()

    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in ("auto_published", "auto_published_with_flags")
    assert _rows() == []
    after = _ledger_numbers()
    assert after["docs"] == [left[0]["ingest_no"]] and after["names"] in ([], [left[0]["ingest_no"]])
    assert after["shown"] == after["built"] == left[0]["ingest_no"]


def test_kill_while_removing_the_old_ledger_numbers_keeps_the_new_ledger_shown_and_resume_finishes_the_removal(registered_world):
    """切り替えの後、古い番号の目録の行を消す途中で SIGKILL → 新しい番号が見え続け、古い番号の行は残る。行は残り、再開が古い番号を消し切る。"""
    import time

    import psycopg
    from _ingest_crash import start_worker_once
    from sherpa.store import db
    _change_source("p")
    before = _ledger_numbers()
    old_no = before["shown"]

    holder = psycopg.connect(db._dsn(), autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s, %s)", _STOP_LOCK)
    with db._connect() as c:
        c.execute("CREATE OR REPLACE FUNCTION stop_matrix_wait() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                  f"PERFORM pg_advisory_lock({_STOP_LOCK[0]}, {_STOP_LOCK[1]}); "
                  f"PERFORM pg_advisory_unlock({_STOP_LOCK[0]}, {_STOP_LOCK[1]}); RETURN OLD; END $$")
        c.execute("CREATE TRIGGER stop_matrix_documents_purge BEFORE DELETE ON documents "
                  f"FOR EACH ROW WHEN (OLD.ingest_no = {int(old_no)}) EXECUTE FUNCTION stop_matrix_wait()")
    proc = None
    try:
        _enqueue_refresh(W)
        proc = start_worker_once()
        deadline = time.monotonic() + 120
        while True:
            with db._connect() as c:
                waiting = c.execute("SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted "
                                    "AND classid=%s AND objid=%s", _STOP_LOCK).fetchone()
            if waiting:
                break
            assert proc.poll() is None, proc.communicate()[1][-2000:]
            assert time.monotonic() < deadline, "古い番号の目録の行を消す所に達しませんでした"
            time.sleep(0.2)
        proc.kill()
        proc.wait(timeout=30)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        holder.execute("SELECT pg_advisory_unlock(%s, %s)", _STOP_LOCK)
        holder.close()
        with db._connect() as c:
            c.execute("DROP TRIGGER IF EXISTS stop_matrix_documents_purge ON documents")
            c.execute("DROP FUNCTION IF EXISTS stop_matrix_wait()")

    _wait_stopped()
    left = _rows()
    mid = _ledger_numbers()
    assert len(left) == 1 and mid["shown"] == left[0]["ingest_no"] != old_no
    assert mid["docs"] == sorted([old_no, left[0]["ingest_no"]]) and name_ledger.is_built(W)
    _settle_orphan()

    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in ("auto_published", "auto_published_with_flags")
    assert _rows() == []
    after = _ledger_numbers()
    assert after["docs"] == [left[0]["ingest_no"]] and after["shown"] == left[0]["ingest_no"]


# ---- グラフ ---------------------------------------------------------------------------------------

@pytest.fixture
def registered_code_world(registered_world):
    """ノードが 2 つできるソースを持つ登録済みの資料フォルダ（切り替えと旧世代の削除が実際に動く）。"""
    root = pathlib.Path(store.get_world(W)["root_path"])
    (root / "code").mkdir()
    (root / "code" / "A.java").write_text("public class A { void f() { new B().g(); } }\n", encoding="utf-8")
    (root / "code" / "B.java").write_text("public class B { void g() {} }\n", encoding="utf-8")
    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in ("auto_published", "auto_published_with_flags")
    assert len(_graph_state()["gens"]) == 1 and None not in _graph_state()["gens"]
    yield W

def _graph_state() -> dict:
    drv = driver()
    try:
        with drv.session() as s:
            meta = s.run("MATCH (m:SherpaMeta {world_id:$w}) RETURN m.active_generation AS gen, m.ingest_no AS no",
                         w=W).single()
            gens = {r["g"] for r in s.run("MATCH (n:Entity {world_id:$w}) RETURN DISTINCT n.graph_generation AS g", w=W)}
    finally:
        drv.close()
    return {"gen": meta["gen"], "no": meta["no"], "gens": gens}


def _wait_neo4j_blocked(drv, proc, what: str) -> None:
    """子プロセスの書き込みが Neo4j のノードの lock を待つ状態になるまで待つ。"""
    import time
    deadline = time.monotonic() + 120
    while True:
        with drv.session() as s:
            blocked = s.run("SHOW TRANSACTIONS YIELD status WHERE status STARTS WITH 'Blocked' RETURN count(*) AS c").single()["c"]
        if blocked:
            return
        assert proc.poll() is None, proc.communicate()[1][-2000:]
        assert time.monotonic() < deadline, f"{what}に達しませんでした"
        time.sleep(0.2)


def _kill_while_graph_node_is_held(hold_cypher: str, what: str) -> dict:
    """親が `hold_cypher` で Neo4j のノードを未確定の tx で持ち、子の worker がそれを待っている間に SIGKILL する。止める前のグラフの状態を返す。"""
    from _ingest_crash import start_worker_once
    drv = driver()
    session = drv.session()
    tx = session.begin_transaction()
    proc = None
    try:
        tx.run(hold_cypher, w=W).consume()
        _enqueue_refresh(W)
        proc = start_worker_once()
        _wait_neo4j_blocked(drv, proc, what)
        proc.kill()
        proc.wait(timeout=30)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        tx.rollback()
        tx.close()
        session.close()
        drv.close()
    return _graph_state()


def _wait_stopped() -> None:
    import time
    deadline = time.monotonic() + 30
    while store.unfinished_state(W) != "whole" and time.monotonic() < deadline:
        time.sleep(0.2)
    assert store.unfinished_state(W) == "whole"


def _resume_and_check(left: dict, old_gens: set, kept_gen: str | None = None) -> None:
    """再開で行が消え、同じ番号のまま 1 世代だけが残る。`kept_gen` があれば、入れ替え済みのその世代を作り直さずに残す。"""
    _settle_orphan()
    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in ("auto_published", "auto_published_with_flags")
    assert _rows() == []
    after = _graph_state()
    assert after["no"] == left["ingest_no"] and len(after["gens"]) == 1
    assert after["gen"] == kept_gen if kept_gen else after["gen"] not in old_gens


def test_kill_just_before_the_graph_switch_keeps_old_generation_active(registered_code_world):
    """切り替えの tx が :SherpaMeta の lock を待つ間に SIGKILL → 旧世代が見え続け、作りかけが 1 つ増えるだけ（2 世代まで）。行は「入れ替えた」と旧世代の名前を持つ。再開は同じ番号で入れ替える。"""
    _change_source("m")
    before = _graph_state()
    assert before["gens"] == {before["gen"]}

    state = _kill_while_graph_node_is_held(
        "MATCH (m:SherpaMeta {world_id:$w}) SET m.stop_hold = true", "グラフの切り替え")

    _wait_stopped()
    left = _rows()
    assert len(left) == 1 and left[0]["source_state"] == "fixed"
    assert left[0]["items"]["graph"] == "swapped" and left[0]["old_things"]["graph"] == [before["gen"]]
    assert state["gen"] == before["gen"] and state["no"] == before["no"]
    assert len(state["gens"]) == 2
    _resume_and_check(left[0], {before["gen"], *state["gens"]})


def test_kill_in_the_graph_switch_leaves_the_ledger_draft_and_keeps_the_old_ledger_shown(registered_code_world):
    """グラフの切り替えで止まっても、目録と名前の台帳は下書きまで書けていて、見せているのは古い番号のまま。再開は同じ番号の下書きを切り替える。"""
    _change_source("d")
    before = _ledger_numbers()
    _kill_while_graph_node_is_held("MATCH (m:SherpaMeta {world_id:$w}) SET m.stop_hold = true", "グラフの切り替え")
    _wait_stopped()
    left = _rows()
    mid = _ledger_numbers()
    assert mid["shown"] == before["shown"] and name_ledger.is_built(W) and name_ledger.built_at(W)
    assert mid["docs"] == sorted([before["shown"], left[0]["ingest_no"]]) and left[0]["items"]["ledger"] == "pending"
    _settle_orphan()
    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _rows() == []
    after = _ledger_numbers()
    assert after["docs"] == [left[0]["ingest_no"]] == [after["shown"]] and after["built"] == after["shown"]


def test_kill_while_removing_the_old_generation_leaves_both_until_resume(registered_code_world):
    """切り替えの後で旧世代を消す途中に SIGKILL → 新世代が見え、旧世代が残る（2 世代まで）。行は旧世代の名前を持ったまま。再開が旧世代を片付ける。"""
    _change_source("n")
    before = _graph_state()

    state = _kill_while_graph_node_is_held(
        "MATCH (n:Entity {world_id:$w}) WITH n LIMIT 1 SET n.stop_hold = true", "旧世代の削除")

    _wait_stopped()
    left = _rows()
    assert len(left) == 1 and left[0]["items"]["graph"] == "swapped"
    assert left[0]["old_things"]["graph"] == [before["gen"]]
    assert state["gen"] != before["gen"] and state["no"] == left[0]["ingest_no"]
    assert state["gens"] == {before["gen"], state["gen"]}
    _resume_and_check(left[0], {before["gen"], state["gen"]}, kept_gen=state["gen"])


# ---- ES ---------------------------------------------------------------------------------------

def _es_state() -> dict:
    from sherpa import es_index
    return {"targets": es_index._alias_targets(W), "physicals": [n for n in es_index._concrete_indices(W) or []
                                                                 if n != es_index._index(W)],
            "no": es_index.read_ingest_no(W)}


def _kill_in_es_swap(tag: str, **kill) -> tuple[dict, dict]:
    """ES の付け替えの途中で子の worker を止める。止める前の ES の状態と、止めた後の行を返す。"""
    _change_source(tag)
    before = _es_state()
    assert len(before["physicals"]) == 1
    _enqueue_refresh(W)
    assert run_worker_once(**kill).returncode == -9
    _wait_stopped()
    left = _rows()
    assert len(left) == 1 and left[0]["source_state"] == "fixed" and left[0]["items"]["es"] == "swapped"
    assert left[0]["old_things"]["es"] == before["physicals"]
    return before, left[0]


def _resume_es_and_check(left: dict) -> None:
    _settle_orphan()
    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in ("auto_published", "auto_published_with_flags")
    assert _rows() == []
    after = _es_state()
    assert after["no"] == left["ingest_no"] and len(after["physicals"]) == 1 and after["targets"] == after["physicals"]


def test_kill_before_the_alias_switch_keeps_old_index_published_and_resume_replaces_it(registered_world):
    """`/_aliases` を送る瞬間に SIGKILL → 旧索引が見え続け、作りかけが 1 つ増えるだけ。行は旧索引の名前を持つ。再開は同じ番号で入れ替える。"""
    before, left = _kill_in_es_swap("e1", kill_on_http_send="POST /_aliases")
    state = _es_state()
    assert state["targets"] == before["targets"] and state["no"] == before["no"]
    assert len(state["physicals"]) == 2
    _resume_es_and_check(left)


def test_kill_after_the_alias_switch_before_it_is_confirmed_leaves_both_until_resume(registered_world):
    """付け替えの直後・引き直しの前に SIGKILL → 新索引が見え、旧索引が残る。再開が旧索引を片付ける。"""
    before, left = _kill_in_es_swap("e2", kill_after_http_send="POST /_aliases", kill_on_http_send="GET /_alias/")
    state = _es_state()
    assert state["targets"] != before["targets"] and len(state["physicals"]) == 2
    _resume_es_and_check(left)


def test_kill_before_deleting_the_old_index_leaves_both_until_resume(registered_world):
    """付け替えの後・旧索引を消す前に SIGKILL → 新索引が見え、旧索引が残る。行は旧索引の名前を持ったまま。再開が片付ける。"""
    before, left = _kill_in_es_swap("e3", kill_after_http_send="POST /_aliases", kill_on_http_send="DELETE /")
    state = _es_state()
    assert state["targets"] != before["targets"] and set(before["physicals"]) <= set(state["physicals"])
    _resume_es_and_check(left)


def test_unreachable_es_keeps_the_row_as_stopped_and_update_after_recovery_clears_it(registered_world):
    """ES に届かない回は成功の記録のまま、行を残して「途中で止まった」を出す。ES が戻った後の「更新」で行が消える。"""
    from sherpa.routers import worlds as api
    _change_source("e4")
    run_id = _enqueue_refresh(W)
    child = run_worker_once(extra_env={"ES_URL": "http://127.0.0.1:9"})
    assert child.returncode == 0, child.stderr[-2000:]
    left = _rows()
    assert len(left) == 1 and left[0]["items"]["es"] == "failed" and left[0]["source_state"] == "fixed"
    assert store.unfinished_state(W) == "whole"
    summary = api._ingest_summary(W, store.get_world(W), api._run_state(W))
    assert "取り込みの途中で止まったため" in summary["last_failure_message"] or _run(run_id)["status"] != "failed"

    _resume_es_and_check(left[0])


# ---- 派生とアーカイブ -------------------------------------------------------------------------

def _layer_dirs() -> dict:
    md = worlds.derived_md_dir(W)
    return {"md": md, "rag": md.with_name("rag"), "ir": md.with_name("ir")}


def _layer_numbers() -> dict:
    from sherpa.ingest import derived_numbers
    return {k: (derived_numbers.read(d) or {}).get("ingest_no") for k, d in _layer_dirs().items()}


def _no_leftover_dirs() -> bool:
    return not [p for d in _layer_dirs().values() for p in (d.with_name(d.name + ".retired"), d.with_name(d.name + ".staging"))
                if p.exists()]


def _resume_derived_and_check(left: dict) -> None:
    _settle_orphan()
    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in ("auto_published", "auto_published_with_flags")
    assert _rows() == []
    assert _layer_numbers() == {"md": left["ingest_no"], "rag": left["ingest_no"], "ir": left["ingest_no"]}
    assert _no_leftover_dirs() and any(_layer_dirs()["md"].iterdir())


def test_kill_between_derived_layers_keeps_the_swapped_layer_and_resume_swaps_the_rest(registered_world):
    """ir 層を差し替えた後・rag 層の退避の前に SIGKILL → ir は新しい番号、rag と md は古いまま。行は rag の旧公開分の名前を持つ。再開が全層を同じ番号にそろえる。"""
    _change_source("d1")
    old = _layer_numbers()
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_rename="/rag|/rag.retired").returncode == -9
    _wait_stopped()
    left = _rows()
    assert len(left) == 1 and left[0]["source_state"] == "fixed"
    assert left[0]["items"]["ir"] == "cleaned" and left[0]["items"]["rag"] == "swapped"
    assert left[0]["old_things"]["rag"] == [str(_layer_dirs()["rag"]) + ".retired"]
    now = _layer_numbers()
    assert now["ir"] == left[0]["ingest_no"] and now["rag"] == old["rag"] and now["md"] == old["md"]
    _resume_derived_and_check(left[0])


def test_kill_between_the_two_renames_of_a_layer_leaves_the_old_one_retired_and_resume_restores_then_swaps(registered_world):
    """md 層の退避の後・新しい層の改名の前に SIGKILL → 公開中の md が無く退避だけが残る。再開が退避を戻してから差し替え、全層が同じ番号になる。"""
    _change_source("d2")
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_rename=".staging|/md").returncode == -9
    _wait_stopped()
    left = _rows()
    assert len(left) == 1 and left[0]["items"]["md"] == "swapped" and left[0]["items"]["ir"] == "cleaned"
    md = _layer_dirs()["md"]
    assert not md.exists() and md.with_name("md.retired").is_dir()
    _resume_derived_and_check(left[0])


def _add_archive(tag: str) -> str:
    import zipfile
    root = store.get_world(W)["root_path"]
    with zipfile.ZipFile(pathlib.Path(root) / f"pack_{tag}.zip", "w") as zf:
        zf.writestr("inner.txt", f"本文 {tag}\n")
    return f"pack_{tag}.zip"


def _manifest() -> dict:
    import json
    return json.loads(worlds.archive_manifest_path(W).read_text(encoding="utf-8"))


def test_kill_at_the_archive_staging_rename_keeps_the_row_and_resume_extracts_again(registered_world):
    """アーカイブの展開物の改名の直前に SIGKILL → 展開先は無く、行は「入れ替えた」と退避先の名前を持つ。再開が展開し直し、manifest に行の番号が入る。"""
    rel = _add_archive("a1")
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_rename=f"{rel}.staging|/{rel}").returncode == -9
    _wait_stopped()
    left = _rows()
    assert len(left) == 1 and left[0]["items"]["archives"] == "swapped" and left[0]["old_things"]["archives"]
    assert not (worlds.archives_dir(W) / rel).exists()
    _settle_orphan()
    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in ("auto_published", "auto_published_with_flags")
    assert _rows() == []
    assert (worlds.archives_dir(W) / rel / "inner.txt").is_file()
    assert _manifest()["ingest_no"] == left[0]["ingest_no"] and rel in _manifest()


def test_failure_of_the_whole_archive_extraction_keeps_the_row_and_update_extracts_again(registered_world):
    """展開全体の例外（manifest を書けない）でも回は続き、行は「失敗」で残る。次の「更新」で展開し直して行が消える。"""
    rel = _add_archive("a2")
    _enqueue_refresh(W)
    child = run_worker_once(fail_open_containing="archive_manifest.json", fail_times=1)
    assert child.returncode == 0, child.stderr[-2000:]
    left = _rows()
    assert len(left) == 1 and left[0]["items"]["archives"] == "failed" and left[0]["source_state"] == "fixed"
    assert store.unfinished_state(W) == "whole"
    assert not worlds.archive_manifest_path(W).exists()

    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in ("auto_published", "auto_published_with_flags")
    assert _rows() == []
    assert (worlds.archives_dir(W) / rel / "inner.txt").is_file()
    assert _manifest()["ingest_no"] == left[0]["ingest_no"]
