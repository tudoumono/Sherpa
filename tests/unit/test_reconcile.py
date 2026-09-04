"""派生物の孤児 自動掃除（reconcile）— 不可視の自己修復＋**fail-safe**（ES/Neo4j/DB 不要・stub）。"""
from __future__ import annotations

import os
import pathlib
import tempfile


def _set_derived(td):
    o = os.environ.get("SHERPA_DERIVED_DIR")
    os.environ["SHERPA_DERIVED_DIR"] = td
    return o


def _restore_derived(o):
    if o is None:
        os.environ.pop("SHERPA_DERIVED_DIR", None)
    else:
        os.environ["SHERPA_DERIVED_DIR"] = o


def test_es_reconcile_deletes_only_orphans():
    """`sherpa-kb-*` のうち、現行 world の索引名に無いものだけ削除（valid は消さない）。"""
    from sherpa import es_index
    keep = es_index._index("aa")                       # valid world の現行名
    orphans = [es_index._index("bb"), "sherpa-kb-legacy"]
    deletes, o_list, o_req = [], es_index.list_kb_indices, es_index._req
    es_index.list_kb_indices = lambda: [keep, *orphans]
    es_index._req = lambda method, path, *a, **k: (deletes.append(path) if method == "DELETE" else None) or {}
    try:
        deleted = es_index.reconcile({"aa"})
        assert set(deleted) == set(orphans) and keep not in deleted
        assert all(p.lstrip("/") in orphans for p in deletes)   # keep は DELETE しない
    finally:
        es_index.list_kb_indices, es_index._req = o_list, o_req


def test_reconcile_skips_when_registry_unavailable(monkeypatch):
    """**最重要 fail-safe①**: レジストリ(PG)が取れない時は何も消さない（全消し事故の構造的防止）。

    `store.list_worlds_db`/`es_index.reconcile` を丸ごとスタブに差し替え＝実ストアには一切触れない
    ので、テスト用 DB 分離の安全ガード（`SHERPA_TEST_DB_ISOLATED`・2026-07-03 インシデント対応）は
    このテストには関係ない。monkeypatch で明示的に外し、`reconcile_derivatives()` 自体の
    fail-safe① ロジックを検証する。
    """
    monkeypatch.delenv("SHERPA_TEST_DB_ISOLATED", raising=False)
    from sherpa import es_index, reconcile, store
    touched, o_db, o_es = [], store.list_worlds_db, es_index.reconcile

    def boom(*a, **k):
        raise RuntimeError("registry down")

    store.list_worlds_db = boom
    es_index.reconcile = lambda v: touched.append(v) or []
    try:
        res = reconcile.reconcile_derivatives()
        assert res == {"skipped": "registry_unavailable"}
        assert touched == []                            # ES reconcile を一切呼ばない＝削除ゼロ
    finally:
        store.list_worlds_db, es_index.reconcile = o_db, o_es


def test_reconcile_skips_when_local_enumeration_uncertain(monkeypatch):
    """**fail-safe②**: ローカル列挙が OSError なら valid 部分集合を避けて全停止（RV High）。

    実ストアに触れないスタブのみ使う＝テスト用 DB 分離の安全ガードは対象外（monkeypatch で外す。
    上の test_reconcile_skips_when_registry_unavailable と同じ理由）。
    """
    monkeypatch.delenv("SHERPA_TEST_DB_ISOLATED", raising=False)
    from sherpa import es_index, reconcile, store
    touched = []
    o_db, o_local, o_es = store.list_worlds_db, reconcile._local_world_ids, es_index.reconcile
    store.list_worlds_db = lambda *a, **k: [{"world_id": "x"}]

    def boom():
        raise OSError("perm denied")

    reconcile._local_world_ids = boom
    es_index.reconcile = lambda v: touched.append(v) or []
    try:
        res = reconcile.reconcile_derivatives()
        assert res == {"skipped": "local_uncertain"}
        assert touched == []                            # 削除に進まない
    finally:
        store.list_worlds_db, reconcile._local_world_ids, es_index.reconcile = o_db, o_local, o_es


def test_reconcile_derived_removes_only_orphan_dirs():
    from sherpa import reconcile
    with tempfile.TemporaryDirectory() as td:
        o = _set_derived(td)
        for w in ("keepw", "orphanw"):
            (pathlib.Path(td) / w / "md").mkdir(parents=True)
        try:
            deleted = reconcile._reconcile_derived({"keepw"}, [])   # source_roots 空＝overlap 無し
            assert deleted == ["orphanw"]
            assert (pathlib.Path(td) / "keepw").is_dir()
            assert not (pathlib.Path(td) / "orphanw").exists()
        finally:
            _restore_derived(o)


def test_reconcile_derived_skips_when_parent_overlaps_source_root():
    """**BLOCKER 対策**: 派生 parent がソース root と重なる誤設定なら**何も消さない**（原本を消さない）。"""
    from sherpa import reconcile
    with tempfile.TemporaryDirectory() as td:
        o = _set_derived(td)
        (pathlib.Path(td) / "orphanw" / "md").mkdir(parents=True)
        try:
            deleted = reconcile._reconcile_derived(set(), [pathlib.Path(td)])   # parent==source root
            assert deleted == []
            assert (pathlib.Path(td) / "orphanw").is_dir()         # 消えていない（fail-closed）
        finally:
            _restore_derived(o)


def test_reconcile_derived_ignores_dotdirs_and_rebind_backup():
    """`.{world}.rebind-bak` 等の `.` 始まり退避dir は valid_world=False＝掃除対象外（rebind を壊さない）。"""
    from sherpa import reconcile
    with tempfile.TemporaryDirectory() as td:
        o = _set_derived(td)
        (pathlib.Path(td) / "orphanw" / "md").mkdir(parents=True)
        (pathlib.Path(td) / ".keepw.rebind-bak" / "md").mkdir(parents=True)
        try:
            deleted = reconcile._reconcile_derived(set(), [])
            assert deleted == ["orphanw"]
            assert (pathlib.Path(td) / ".keepw.rebind-bak").is_dir()   # 退避は無傷
        finally:
            _restore_derived(o)


def test_local_world_ids_propagates_real_oserror_but_skips_missing():
    """**fail-safe③**: 不在(FileNotFound)は skip、Permission/IO 等の OSError は伝播（部分 valid を作らない・RV High）。"""
    import os as _os

    from sherpa import reconcile
    o_scandir = _os.scandir

    _os.scandir = lambda p: (_ for _ in ()).throw(FileNotFoundError("missing"))
    try:
        assert reconcile._local_world_ids() == set()    # 不在は静かに空（伝播しない）
    finally:
        _os.scandir = o_scandir

    _os.scandir = lambda p: (_ for _ in ()).throw(PermissionError("denied"))
    try:
        raised = False
        try:
            reconcile._local_world_ids()
        except OSError:
            raised = True
        assert raised                                   # 不確実は伝播＝reconcile が止まる
    finally:
        _os.scandir = o_scandir


def test_neo4j_reconcile_deletes_non_valid_world_ids():
    import neo4j

    from sherpa.ingest import world_neo4j
    deleted = []

    class FakeResult:
        def __init__(self, rows): self._rows = rows
        def data(self): return self._rows

    class FakeSession:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def run(self, cy, **kw):
            if "DISTINCT" in cy:
                return FakeResult([{"w": "keepw"}, {"w": "orphanw"}])
            if "DETACH DELETE" in cy:
                deleted.append(kw["w"])
            return FakeResult([])

    class FakeDriver:
        def session(self): return FakeSession()
        def close(self): pass

    o_drv = neo4j.GraphDatabase.driver
    neo4j.GraphDatabase.driver = lambda *a, **k: FakeDriver()
    try:
        out = world_neo4j.reconcile({"keepw"}, "bolt://x", "u", "p")
        assert out == ["orphanw"] and deleted == ["orphanw"]
    finally:
        neo4j.GraphDatabase.driver = o_drv


def test_reconcile_protects_local_and_removes_orphan_when_registry_ok(monkeypatch):
    """registry が取れた時は valid=registry∪local で掃除。**local（fixtures/kb）world は保護**・registry world も保護。

    store/es_index の全関数をスタブ差し替え＋派生物は一時ディレクトリ＝実ストアに一切触れない。
    テスト用 DB 分離の安全ガード（`SHERPA_TEST_DB_ISOLATED`）はこのテストには関係ないので外す
    （2026-07-03 インシデント対応: 実 Neo4j/ES に触れる経路だけを止めるためのガードで、完全に
    モック化されたこのテストの対象外）。
    """
    monkeypatch.delenv("SHERPA_TEST_DB_ISOLATED", raising=False)
    from sherpa import es_index, reconcile, store
    seen = {}
    o_db, o_local, o_es = store.list_worlds_db, reconcile._local_world_ids, es_index.reconcile
    o_ldw, o_rd = store.list_document_worlds, store.replace_documents
    store.list_worlds_db = lambda *a, **k: [{"world_id": "keepw"}]
    reconcile._local_world_ids = lambda: {"localw"}                # fixtures/kb 由来をスタブ
    es_index.reconcile = lambda valid: (seen.update(valid=set(valid)) or ["sherpa-kb-gone"])  # 実ESに触れない
    store.list_document_worlds = lambda: ["keepw", "localw", "orphanw"]
    cleared_docs = []
    store.replace_documents = lambda w, rows: cleared_docs.append(w) or 0
    with tempfile.TemporaryDirectory() as td:
        o = _set_derived(td)
        for w in ("keepw", "localw", "orphanw"):
            (pathlib.Path(td) / w / "md").mkdir(parents=True)
        try:
            res = reconcile.reconcile_derivatives(reflect=False)   # Neo4j はスキップ
            assert "skipped" not in res
            assert res["es"] == ["sherpa-kb-gone"]
            assert seen["valid"] == {"keepw", "localw"}            # registry∪local
            assert res["derived"] == ["orphanw"]                   # local/registry world は残す
            assert res["documents"] == ["orphanw"]                 # documents 台帳も同じ valid で掃除（バッチ2・3番）
            assert cleared_docs == ["orphanw"]
            assert (pathlib.Path(td) / "keepw").is_dir() and (pathlib.Path(td) / "localw").is_dir()
        finally:
            store.list_worlds_db, reconcile._local_world_ids, es_index.reconcile = o_db, o_local, o_es
            store.list_document_worlds, store.replace_documents = o_ldw, o_rd
            _restore_derived(o)


# ---- バッチ2・3番（2026-07-03）: documents 台帳（PG）は従来 reconcile の対象外だった（実際に
# 本番 dev で v1（fixtures 由来）残骸が Postgres documents/Neo4j に残っていた事象を受けての拡張）。

def test_documents_reconcile_deletes_only_orphan_worlds():
    """documents 台帳のうち valid に無い world だけ削除する（`replace_documents(world, [])` を再利用）。"""
    from sherpa import reconcile, store
    o_list, o_replace = store.list_document_worlds, store.replace_documents
    cleared = []
    store.list_document_worlds = lambda: ["keepw", "orphanw"]
    store.replace_documents = lambda w, rows: cleared.append(w) or 0
    try:
        deleted = reconcile._reconcile_documents({"keepw"})
        assert deleted == ["orphanw"]
        assert cleared == ["orphanw"]
    finally:
        store.list_document_worlds, store.replace_documents = o_list, o_replace


def test_documents_reconcile_skips_on_list_failure_without_raising():
    """一覧取得（DB）に失敗しても reconcile 全体を落とさない（best-effort・fail-safe）。"""
    from sherpa import reconcile, store
    o_list = store.list_document_worlds

    def boom():
        raise RuntimeError("db down")

    store.list_document_worlds = boom
    try:
        assert reconcile._reconcile_documents({"keepw"}) == []
    finally:
        store.list_document_worlds = o_list


def test_documents_reconcile_individual_failure_does_not_stop_others():
    """1件の削除失敗が他の world の削除を止めない（best-effort・次回リコンサイルで再試行）。"""
    from sherpa import reconcile, store
    o_list, o_replace = store.list_document_worlds, store.replace_documents
    store.list_document_worlds = lambda: ["orphan1", "orphan2"]

    def flaky(w, rows):
        if w == "orphan1":
            raise RuntimeError("boom")
        return 0

    store.replace_documents = flaky
    try:
        deleted = reconcile._reconcile_documents(set())
        assert deleted == ["orphan2"]
    finally:
        store.list_document_worlds, store.replace_documents = o_list, o_replace
