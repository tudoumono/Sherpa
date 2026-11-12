"""ES 索引の付け替えが、入れ替えの行を確かめ、取り込みの番号と設定の値をメタに書くこと（要 Postgres・ES）。

文書の材料と埋め込みの送り先だけを差し替える（外部境界）。ES・DB は本物を通す。
"""
from __future__ import annotations

import pytest
from tests._replace_row import row_for

from sherpa import es_index as es
from sherpa.store import db, replace_rows


@pytest.fixture
def env(monkeypatch, tmp_path):
    if not es.available():
        pytest.skip("ES 未起動")
    world = f"es-swap-{tmp_path.name[-8:]}".lower()
    texts = {"a.md": "alpha01 本文"}

    def docs(*a, **k):
        return [{"name": n, "md_path": None, "top_scope": "t", "branch": "office"} for n in texts]

    monkeypatch.setattr(es.corpus_docs, "world_documents", docs)
    monkeypatch.setattr(es.doc_text, "read_world_doc_text", lambda w, d: texts[d["name"]])
    monkeypatch.setattr(es.worlds, "semantic_dir", lambda w: tmp_path / "semantic")
    monkeypatch.setattr(es.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)
    es.delete_world(world, row=row_for(world, kind="delete"))
    yield world, texts
    es._building.pop(world, None)
    es.delete_world(world, row=row_for(world, kind="delete"))
    with db._connect() as c:
        c.execute("DELETE FROM ingest_replacements WHERE kb_id=%s AND world=%s", (db._KB_ID, world))


def _physicals(world):
    return [n for n in es._concrete_indices(world) if n != es._index(world)]


def _lose_lock(row):
    with db._connect() as c:
        c.execute("UPDATE ingest_replacements SET lock_id=lock_id+1 WHERE id=%s", (row.id,))


def test_index_writes_the_row_number_and_settings_sig_and_marks_the_item(env):
    world, _texts = env
    row = row_for(world)

    assert es.index_world(world, content_sig="s1", row=row)["indexed"] == 1

    meta = es._index_meta(world)
    assert meta["ingest_no"] == row.ingest_no and meta["settings_sig"] == replace_rows.settings_sig(row, "es")
    assert es.read_ingest_no(world) == row.ingest_no
    cur = replace_rows.read_row(row.id)
    assert cur.items["es"] == replace_rows.ITEM_CLEANED and "es" not in cur.old_things


def test_second_index_replaces_the_number_and_leaves_one_physical_index(env):
    world, texts = env
    first = row_for(world)
    es.index_world(world, content_sig="s1", row=first)
    old = es._alias_targets(world)
    texts["b.md"] = "beta02 二つ目"
    second = row_for(world)

    es.index_world(world, content_sig="s2", row=second)

    assert es.read_ingest_no(world) == second.ingest_no > first.ingest_no
    assert len(_physicals(world)) == 1 and _physicals(world) != old


def test_old_index_name_stays_in_the_row_when_deleting_it_fails(env, monkeypatch):
    """付け替えの後で旧索引を消せなくても、行は旧索引の名前を持った「入れ替えた」のまま残る（再開が消す）。"""
    world, texts = env
    es.index_world(world, content_sig="s1", row=row_for(world))
    old = es._alias_targets(world)
    texts["b.md"] = "beta02 二つ目"
    row = row_for(world)
    real = es._req

    def req(method, path, *a, **k):
        if method == "DELETE" and path == "/" + old[0]:
            raise OSError("injected")
        return real(method, path, *a, **k)

    monkeypatch.setattr(es, "_req", req)
    es.index_world(world, content_sig="s2", row=row)

    cur = replace_rows.read_row(row.id)
    assert cur.items["es"] == replace_rows.ITEM_SWAPPED and cur.old_things["es"] == old
    assert es._alias_targets(world) != old and old[0] in es._concrete_indices(world)


def test_a_row_of_another_world_is_refused_and_the_old_index_stays_published(env):
    world, texts = env
    es.index_world(world, content_sig="s1", row=row_for(world))
    old = es._alias_targets(world)
    texts["b.md"] = "beta02 二つ目"

    with pytest.raises(ValueError):
        es.index_world(world, content_sig="s2", row=row_for(world + "-other"))

    assert es._alias_targets(world) == old and _physicals(world) == old
    with db._connect() as c:
        c.execute("DELETE FROM ingest_replacements WHERE kb_id=%s AND world=%s", (db._KB_ID, world + "-other"))


def test_a_row_that_lost_its_lock_keeps_the_old_index_published(env):
    world, texts = env
    first = row_for(world)
    es.index_world(world, content_sig="s1", row=first)
    old = es._alias_targets(world)
    texts["b.md"] = "beta02 二つ目"
    lost = row_for(world)
    _lose_lock(lost)

    with pytest.raises(replace_rows.RowLockLost):
        es.index_world(world, content_sig="s2", row=lost)

    assert es._alias_targets(world) == old and _physicals(world) == old
    assert es.read_ingest_no(world) == first.ingest_no
    assert replace_rows.read_row(lost.id).items == {}


def test_delete_world_marks_the_item_and_clears_the_index(env):
    world, _texts = env
    es.index_world(world, content_sig="s1", row=row_for(world))
    drow = row_for(world, kind="delete")

    assert es.delete_world(world, row=drow) is True

    assert es._concrete_indices(world) == []
    assert replace_rows.read_row(drow.id).items["es"] == replace_rows.ITEM_CLEANED


def test_fill_missing_vectors_does_not_write_into_an_index_of_another_number(env, monkeypatch):
    world, _texts = env
    row = row_for(world)
    es.index_world(world, content_sig="s1", row=row)
    es._req("POST", f"/{es._index(world)}/_update_by_query?refresh=true",
            {"script": "ctx._source.vector_status='missing'", "query": {"match_all": {}}})
    assert es.count_missing_vectors(world) == 1
    monkeypatch.setattr(es.embeddings, "cfg", lambda settings=None, **kw: {"provider": "fake", "model": "m", "dim": 3})

    assert es.fill_missing_vectors(world, ingest_no=row.ingest_no + 1)["skipped"] == "ingest_no_changed"
    assert es.count_missing_vectors(world) == 1


def test_mark_failed_keeps_the_row_from_being_dropped(env):
    from sherpa.ingest import replace_guard
    world, _texts = env
    row = row_for(world)
    assert replace_guard.final_check(row) is not None

    replace_rows.mark_failed(row, world, "es", "unavailable")

    cur = replace_rows.read_row(row.id)
    assert cur.items["es"] == replace_rows.ITEM_FAILED and cur.rebuild_reason == "unavailable"
    assert replace_guard.final_check(row) is None
    _lose_lock(row)
    with pytest.raises(replace_rows.RowLockLost):
        replace_rows.mark_failed(row, world, "es", "x")
    with pytest.raises(ValueError):
        replace_rows.mark_failed(row, world + "-x", "es", "x")
