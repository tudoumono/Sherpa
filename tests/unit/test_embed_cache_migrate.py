"""埋め込みキャッシュの 1 次元 1 バイトの BLOB 化と、古い形式（0.19.5 の float32・0.19.4 以前の JSON）からの写し替え（外部境界は差し替えない・tmp_path の SQLite だけ）。"""
from __future__ import annotations

import json
import sqlite3
from array import array

import pytest

from sherpa import embed_cache_migrate as mig
from sherpa import embeddings, es_index

pytestmark = pytest.mark.unit

DIM = 8


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(es_index.worlds, "semantic_dir", lambda w: tmp_path / w / "semantic")
    return "mw"


def _vec(i: int) -> list:
    return [(i + 1) * 0.1234567890123 + j * 0.01 for j in range(DIM)]


def _seed_legacy(world: str, n: int, extra_bad: bool = False) -> dict:
    old = es_index._embed_cache_legacy_path(world)
    old.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(old))
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, vec TEXT NOT NULL)")
    vecs = {f"k{i}": _vec(i) for i in range(n)}
    conn.executemany("INSERT INTO kv VALUES (?, ?)", [(k, json.dumps(v)) for k, v in vecs.items()])
    if extra_bad:
        conn.execute("INSERT INTO kv VALUES ('bad', 'not json')")
    conn.commit()
    conn.close()
    return vecs


def _seed_f32(world: str, n: int) -> dict:
    p = es_index._embed_cache_f32_path(world)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE kv2 (key TEXT PRIMARY KEY, vec BLOB NOT NULL)")
    vecs = {f"f{i}": _vec(i) for i in range(n)}
    conn.executemany("INSERT INTO kv2 VALUES (?, ?)", [(k, embeddings.vector_to_bytes(v)) for k, v in vecs.items()])
    conn.execute("INSERT INTO kv2 VALUES ('short', ?)", (b"\x00" * 6,))
    conn.commit()
    conn.close()
    return vecs


def test_migrated_cache_hits_return_quantized_values(world):
    vecs = _seed_legacy(world, 5000)
    before = es_index._embed_cache_lookup_batch(world, list(vecs), DIM)   # 写し替え前（JSON を読んで縮める）
    r = mig.migrate_world(world)
    assert r["status"] == "migrated" and r["count"] == 5000
    assert not es_index._embed_cache_legacy_path(world).exists()
    assert es_index._embed_cache_db_path(world) == es_index._embed_cache_i8_path(world)
    got = es_index._embed_cache_lookup_batch(world, list(vecs), DIM)
    assert set(got) == set(vecs) and got == before
    for k, v in vecs.items():
        assert got[k] == embeddings.quantize_vector(array("f", v)) and got[k].typecode == "b"


def test_f32_cache_from_0195_is_migrated_to_the_same_bytes(world):
    vecs = _seed_f32(world, 3000)
    before = es_index._embed_cache_lookup_batch(world, list(vecs), DIM)   # 写し替え前（float32 を読んで縮める）
    r = mig.migrate_world(world)
    assert r["status"] == "migrated" and r["count"] == 3000 and r["skipped"] == 1
    assert not es_index._embed_cache_f32_path(world).exists()
    got = es_index._embed_cache_lookup_batch(world, list(vecs), DIM)
    assert got == before and set(got) == set(vecs)
    i8 = sqlite3.connect(str(es_index._embed_cache_i8_path(world)))
    assert all(len(b) == DIM for (b,) in i8.execute("SELECT vec FROM kv3"))


def test_bad_row_is_dropped_not_fatal(world):
    vecs = _seed_legacy(world, 3, extra_bad=True)
    r = mig.migrate_world(world)
    assert r["status"] == "migrated" and r["skipped"] == 1
    assert set(es_index._embed_cache_lookup_batch(world, list(vecs) + ["bad"], DIM)) == set(vecs)


def test_stop_midway_keeps_old_file_and_removes_tmp_then_retry_works(world):
    vecs = _seed_legacy(world, 5000)
    old = es_index._embed_cache_legacy_path(world)
    before = old.read_bytes()
    calls = []

    def stop():
        calls.append(1)
        return len(calls) > 1  # 1 回目の区切りは通し、2 回目で止める

    r = mig.migrate_world(world, should_stop=stop)
    assert r["status"] == "stopped"
    assert old.read_bytes() == before
    assert not list(old.parent.glob("*.tmp*")) and not es_index._embed_cache_i8_path(world).exists()
    assert set(es_index._embed_cache_lookup_batch(world, list(vecs), DIM)) == set(vecs)  # 古い方でまだ読める
    assert mig.migrate_world(world)["status"] == "migrated"


def test_exception_midway_keeps_old_file_and_removes_tmp(world, monkeypatch):
    _seed_legacy(world, 5000)
    old = es_index._embed_cache_legacy_path(world)
    n = []
    real = embeddings.quantize_vector

    def boom(v):
        n.append(1)
        if len(n) > 2500:
            raise RuntimeError("disk")
        return real(v)

    monkeypatch.setattr(embeddings, "quantize_vector", boom)
    with pytest.raises(RuntimeError):
        mig.migrate_world(world)
    assert old.exists() and not list(old.parent.glob("*.tmp*")) and not es_index._embed_cache_i8_path(world).exists()


def test_not_enough_disk_skips_migration(world, monkeypatch):
    _seed_legacy(world, 10)
    monkeypatch.setattr(mig.shutil, "disk_usage", lambda p: type("U", (), {"free": 0})())
    r = mig.migrate_world(world)
    assert r["status"] == "skipped" and r["reason"] == "no_space"
    assert es_index._embed_cache_legacy_path(world).exists() and not es_index._embed_cache_i8_path(world).exists()


def test_new_file_wins_and_legacy_is_not_written(world):
    _seed_legacy(world, 3)
    es_index._embed_cache_write_batch(world, {"fresh": _vec(99)})
    # 旧形式しか無いあいだは旧形式へ書く
    assert not es_index._embed_cache_i8_path(world).exists()
    assert mig.migrate_world(world)["status"] == "migrated"
    # 古いファイルが残っていても（rename 後・unlink 前の停電など）新しい方だけを使う
    old = es_index._embed_cache_legacy_path(world)
    conn = sqlite3.connect(str(old))
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, vec TEXT NOT NULL)")
    conn.execute("INSERT INTO kv VALUES ('stale', ?)", (json.dumps(_vec(1)),))
    conn.commit()
    conn.close()
    assert es_index._embed_cache_db_path(world) == es_index._embed_cache_i8_path(world)
    es_index._embed_cache_write_batch(world, {"new1": _vec(7)})
    assert set(es_index._embed_cache_lookup_batch(world, ["stale", "new1", "fresh"], DIM)) == {"new1", "fresh"}
    assert sqlite3.connect(str(old)).execute("SELECT COUNT(*) FROM kv").fetchone()[0] == 1  # 古い方へは書いていない
    assert mig.migrate_world(world)["status"] == "cleaned" and not old.exists()


def test_wrong_length_blob_is_miss(world):
    es_index._embed_cache_write_batch(world, {"a": _vec(1)})
    conn = sqlite3.connect(str(es_index._embed_cache_i8_path(world)))
    conn.execute("INSERT INTO kv3 VALUES ('short', ?)", (b"\x01" * 7,))
    conn.execute("INSERT INTO kv3 VALUES ('zero', ?)", (b"\x00" * DIM,))
    conn.commit()
    conn.close()
    got = es_index._embed_cache_lookup_batch(world, ["a", "short", "zero"], DIM)
    assert set(got) == {"a"}
    assert es_index._embed_cache_lookup_batch(world, ["a"], DIM + 1) == {}


def test_drop_removes_only_legacy(world):
    _seed_legacy(world, 3)
    new = es_index._embed_cache_i8_path(world)
    tmp = new.with_name(new.name + ".tmp")
    tmp.write_bytes(b"x" * 10)                                       # 写し替えの途中で強制終了された書きかけ
    r = mig.drop_legacy(world)
    assert r["status"] == "dropped" and not es_index._embed_cache_legacy_path(world).exists()
    assert not tmp.exists()
    assert es_index._embed_cache_lookup_batch(world, ["k0"], DIM) == {}


def test_prune_and_delete_keys_work_on_new_format(world):
    es_index._embed_cache_write_batch(world, {f"k{i}": _vec(i) for i in range(10)})
    assert es_index._embed_cache_delete_keys(world, ["k0", "k1"]) == 2
    es_index._prune_embed_cache(world, {"k5"})
    assert set(es_index._embed_cache_lookup_batch(world, [f"k{i}" for i in range(10)], DIM)) == {"k5"}


def test_pending_migration_skips_world_held_by_another_run(world, monkeypatch):
    from sherpa import store
    _seed_legacy(world, 3)
    monkeypatch.setattr(mig.worlds, "discover_world_ids", lambda: [world])
    with store.world_lock(world):  # 取り込みが同じ資料フォルダを処理している間
        res = mig.migrate_pending()
    assert [r["status"] for r in res] == ["skipped"] and res[0]["reason"] == "busy"
    assert es_index._embed_cache_legacy_path(world).exists()
    assert [r["status"] for r in mig.migrate_pending()] == ["migrated"]


def test_worker_migrates_before_picking_up_a_run(monkeypatch):
    """再起動したワーカーは、中断した取り込みを拾う前に旧形式のキャッシュを写し替える（取り込みの間に旧形式へ書き足さない）。"""
    from sherpa.ingest import ingest_worker
    order = []

    def fake_migrate(version, beat, tried, *, yield_to_queue=True):
        order.append(("migrate", yield_to_queue))
        return False

    def fake_process(worker_id, version, beat):
        order.append(("process",))
        ingest_worker._terminate.set()
        return False

    monkeypatch.setattr(ingest_worker, "_migrate_embed_caches", fake_migrate)
    monkeypatch.setattr(ingest_worker, "process_one", fake_process)
    monkeypatch.setattr(ingest_worker.store, "count_queued_runs", lambda version: (0, 0))
    ingest_worker._terminate.clear()

    class _B:
        def set_state(self, *a, **k):
            pass
    try:
        ingest_worker._wait_and_process("w1", None, _B())
    finally:
        ingest_worker._terminate.clear()
    assert order[0] == ("migrate", False) and ("process",) in order


def test_sweep_document_json_removes_per_world_once_and_skips_busy(monkeypatch, tmp_path):
    import contextlib
    import psycopg
    from sherpa import store, worlds
    from sherpa.ingest import office_md
    monkeypatch.setattr(worlds, "discover_world_ids", lambda: ["a", "busy"])
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: tmp_path / w / "md")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path / "src" / w)
    for w in ("a", "busy"):
        (tmp_path / w / "ir").mkdir(parents=True)
        (tmp_path / w / "ir" / "x.document.json").write_text("{}", encoding="utf-8")

    @contextlib.contextmanager
    def lock(w, timeout_ms=None):
        if w == "busy":
            raise psycopg.errors.LockNotAvailable()
        yield

    monkeypatch.setattr(store, "world_lock", lock)
    tried: set = set()
    res = mig.sweep_document_json(skip=tried)
    assert [(r["world"], r["count"]) for r in res] == [("a", 1)]
    assert not (tmp_path / "a" / "ir" / "x.document.json").exists()
    assert (tmp_path / "busy" / "ir" / "x.document.json").exists()      # ロックを取れない資料フォルダは触らない
    assert mig.sweep_document_json(skip=tried) == []                    # 同じプロセスでは繰り返さない


def test_sweep_document_json_leaves_marker_and_does_not_rescan(monkeypatch, tmp_path):
    from sherpa import worlds
    monkeypatch.setattr(worlds, "discover_world_ids", lambda: ["a"])
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: tmp_path / w / "md")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path / "src" / w)
    ir = tmp_path / "a" / "ir"
    ir.mkdir(parents=True)
    assert [r["count"] for r in mig.sweep_document_json()] == []        # 0 件でも印は置く
    assert (tmp_path / "a" / mig._DOCUMENT_JSON_SWEPT).exists()
    (ir / "x.document.json").write_text("{}", encoding="utf-8")
    assert mig.sweep_document_json() == [] and (ir / "x.document.json").exists()   # 別プロセスでも走査し直さない
    assert [r["count"] for r in mig.sweep_document_json(force=True)] == [1]       # 手で流すときは印があっても走らせる


def test_sweep_document_json_does_not_touch_files_when_derived_overlaps_source(monkeypatch, tmp_path):
    """派生物の置き場が登録ディレクトリ（読み取り専用）と重なっていれば、何も消さず印も置かない。"""
    from sherpa import worlds
    src = tmp_path / "kb" / "a"
    monkeypatch.setattr(worlds, "discover_world_ids", lambda: ["a"])
    monkeypatch.setattr(worlds, "world_dir", lambda w: src)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: src / "derived" / "md")   # 原本の中に派生物がある
    ir = src / "derived" / "ir"
    ir.mkdir(parents=True)
    (ir / "x.document.json").write_text("{}", encoding="utf-8")
    assert mig.sweep_document_json(force=True) == []
    assert (ir / "x.document.json").exists() and not (src / "derived" / mig._DOCUMENT_JSON_SWEPT).exists()
