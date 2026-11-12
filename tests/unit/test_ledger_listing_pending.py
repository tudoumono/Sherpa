"""名前で種類が決まらない台帳の行も、資料フォルダを歩かずに台帳から返すこと。

台帳の時点ごとに分類を取っておき、2 回目はファイルに触れない。台帳の書き込みは取り込みと同じ `worker._ledger_rows`、DB は実物。
"""
from __future__ import annotations

import os
import uuid

import pytest

from sherpa import corpus_docs, doc_ledger, scope_infer, search_cache, store, worlds
from sherpa.ingest import worker
from tests._replace_row import offline_row

pytestmark = pytest.mark.unit

_AMBIGUOUS = "gen1/web/page.html"
_FILES = {
    "gen1/design/spec_a.md": "# spec A\n",
    "gen1/src/PROG1.cbl": "       PROGRAM-ID. PROG1.\n",
    _AMBIGUOUS: "<html><body>hello</body></html>\n",
}


@pytest.fixture
def world(monkeypatch, tmp_path):
    wid = "ledgerpend_" + uuid.uuid4().hex[:8]
    wd = tmp_path / "world"
    for rel, text in _FILES.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "derived_dir", lambda w: der)
    # 取り込み直後の更新時刻は「確定前」として保存対象外になるため、試験ではファイルを古くしておく
    for rel in _FILES:
        os.utime(wd / rel, (1_000_000_000, 1_000_000_000))
    yield wid
    store.replace_documents(wid, [], row=offline_row(wid))
    with store._connect() as c:
        c.execute("DELETE FROM ingest_runs WHERE version=%s", (wid,))


def _ingest_with_blank_doctype(wid):
    """名前で決まらない行（doctype／branch が空）を持つ台帳を作る。"""
    run = store.start_ingest_run(wid)
    rows = worker._ledger_rows(wid)
    for r in rows:
        if r["name"] == _AMBIGUOUS:
            r["doctype"], r["branch"] = None, None
    store.replace_documents(wid, rows, row=offline_row(wid))
    store.finish_ingest_run(run["id"], status="auto_published", extraction_snapshot={"flags": []},
                            published_snapshot={"nodes": 1, "edges": 1})


def _walked(wid):
    return {r["name"]: r for r in doc_ledger.documents_for(wid)}


def test_blank_row_is_listed_from_ledger_with_same_doctype_and_branch_as_walk(world):
    assert corpus_docs.classify_by_name(_AMBIGUOUS) is None
    _ingest_with_blank_doctype(world)
    listing = doc_ledger.listing_documents(world)
    assert listing.ledger_at is not None
    got = {r["name"]: r for r in listing.rows}
    want = _walked(world)[_AMBIGUOUS]
    assert (got[_AMBIGUOUS]["doctype"], got[_AMBIGUOUS]["branch"]) == (want["doctype"], want["branch"])
    assert got[_AMBIGUOUS]["doctype"] is not None


def test_second_listing_does_not_touch_the_file(world, tmp_path):
    _ingest_with_blank_doctype(world)
    first = doc_ledger.listing_documents(world)
    (tmp_path / "world" / _AMBIGUOUS).unlink()
    second = doc_ledger.listing_documents(world)
    assert second.ledger_at == first.ledger_at
    assert second.rows == first.rows


def test_ledger_time_change_reads_again(world, tmp_path):
    _ingest_with_blank_doctype(world)
    first = doc_ledger.listing_documents(world)
    _ingest_with_blank_doctype(world)
    (tmp_path / "world" / _AMBIGUOUS).unlink()
    second = doc_ledger.listing_documents(world)
    assert first.ledger_at is not None and second.ledger_at is not None
    assert _AMBIGUOUS in {r["name"] for r in first.rows} and _AMBIGUOUS not in {r["name"] for r in second.rows}


def test_missing_file_is_dropped_without_walking(world, tmp_path):
    """取り込みの後に消えたファイルは、歩いたときと同じく一覧に出さず、ほかの行は台帳から返す。"""
    _ingest_with_blank_doctype(world)
    (tmp_path / "world" / _AMBIGUOUS).unlink()
    got = doc_ledger.listing_documents(world)
    assert got.ledger_at is not None
    assert {r["name"] for r in got.rows} == set(_walked(world))


def test_deadline_exceeded_raises_walk_deadline(world):
    _ingest_with_blank_doctype(world)
    with pytest.raises(scope_infer.ScopeWalkDeadlineExceeded):
        doc_ledger.listing_documents(world, deadline=0.0)


def test_without_cache_dir_it_still_works_by_reading_each_time(world, monkeypatch):
    monkeypatch.setattr(search_cache, "_db_path", lambda w: None)
    _ingest_with_blank_doctype(world)
    listing = doc_ledger.listing_documents(world)
    assert listing.ledger_at is not None
    assert {r["name"]: r["doctype"] for r in listing.rows}[_AMBIGUOUS] is not None


def test_missing_row_is_remembered_for_the_same_ledger_time(world, tmp_path):
    """同じ台帳の時点では、消えていた行を見に行き直さない（戻しても出ない）。"""
    _ingest_with_blank_doctype(world)
    p = tmp_path / "world" / _AMBIGUOUS
    body = p.read_text(encoding="utf-8")
    p.unlink()
    first = doc_ledger.listing_documents(world)
    p.write_text(body, encoding="utf-8")
    second = doc_ledger.listing_documents(world)
    assert _AMBIGUOUS not in {r["name"] for r in first.rows} and second.rows == first.rows


def test_classifying_pending_rows_keeps_other_classify_cache_rows(world):
    """名前で決まらない行だけを分類しても、資料フォルダ全体の分類のキャッシュを消さない。"""
    import sqlite3
    doc_ledger.documents_for(world)                     # 全体を歩いて分類のキャッシュを作る
    db = search_cache._db_path(world)

    def rows():
        with sqlite3.connect(db) as c:
            return {r[0] for r in c.execute("SELECT rel FROM classify")}
    before = rows()
    _ingest_with_blank_doctype(world)
    doc_ledger.listing_documents(world)
    assert before and before <= rows()
