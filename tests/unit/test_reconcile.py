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


def test_es_orphan_indices_lists_only_orphans_by_alias_name(monkeypatch):
    """`sherpa-kb-*` のうち、現行 world の索引名（世代付きの物理索引を含む）に無いものだけを、エイリアス名で返す。"""
    from sherpa import es_index
    keep = es_index._index("aa")                       # valid world の現行名
    orphans = [es_index._index("bb"), "sherpa-kb-legacy"]
    monkeypatch.setattr(es_index, "list_kb_indices",
                        lambda: [keep, keep + "-g123", *orphans, orphans[0] + "-g9"])
    assert es_index.orphan_indices({"aa"}) == sorted(orphans)


def test_reconcile_skips_when_registry_unavailable(monkeypatch):
    """**最重要 fail-safe①**: レジストリ(PG)が取れない時は何も消さない（全消し事故の構造的防止）。

    `store.list_worlds_db`/`es_index.orphan_indices` を丸ごとスタブに差し替え＝実ストアには一切触れない
    ので、テスト用 DB 分離の安全ガード（`SHERPA_TEST_DB_ISOLATED`・2026-07-03 インシデント対応）は
    このテストには関係ない。monkeypatch で明示的に外し、`reconcile_derivatives()` 自体の
    fail-safe① ロジックを検証する。
    """
    monkeypatch.delenv("SHERPA_TEST_DB_ISOLATED", raising=False)
    from sherpa import es_index, reconcile, store
    touched, o_db, o_es = [], store.list_worlds_db, es_index.orphan_indices

    def boom(*a, **k):
        raise RuntimeError("registry down")

    store.list_worlds_db = boom
    es_index.orphan_indices = lambda v: touched.append(v) or []
    try:
        res = reconcile.reconcile_derivatives()
        assert res == {"skipped": "registry_unavailable"}
        assert touched == []                            # ES reconcile を一切呼ばない＝削除ゼロ
    finally:
        store.list_worlds_db, es_index.orphan_indices = o_db, o_es


def test_reconcile_skips_when_local_enumeration_uncertain(monkeypatch):
    """**fail-safe②**: ローカル列挙が OSError なら valid 部分集合を避けて全停止（RV High）。

    実ストアに触れないスタブのみ使う＝テスト用 DB 分離の安全ガードは対象外（monkeypatch で外す。
    上の test_reconcile_skips_when_registry_unavailable と同じ理由）。
    """
    monkeypatch.delenv("SHERPA_TEST_DB_ISOLATED", raising=False)
    from sherpa import es_index, reconcile, store
    touched = []
    o_db, o_local, o_es = store.list_worlds_db, reconcile._local_world_ids, es_index.orphan_indices
    store.list_worlds_db = lambda *a, **k: [{"world_id": "x"}]

    def boom():
        raise OSError("perm denied")

    reconcile._local_world_ids = boom
    es_index.orphan_indices = lambda v: touched.append(v) or []
    try:
        res = reconcile.reconcile_derivatives()
        assert res == {"skipped": "local_uncertain"}
        assert touched == []                            # 削除に進まない
    finally:
        store.list_worlds_db, reconcile._local_world_ids, es_index.orphan_indices = o_db, o_local, o_es


def test_reconcile_derived_removes_only_orphan_dirs():
    from sherpa import reconcile
    with tempfile.TemporaryDirectory() as td:
        o = _set_derived(td)
        for w in ("keepw", "orphanw"):
            (pathlib.Path(td) / w / "md").mkdir(parents=True)
        try:
            assert reconcile._derived_orphans({"keepw"}, True) == ["orphanw"]
            reconcile._remove_derived("orphanw")
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
            derived_ok = not reconcile._overlaps(reconcile._derived_parent(), pathlib.Path(td))   # parent==source root
            assert derived_ok is False and reconcile._derived_orphans(set(), derived_ok) == []
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
            assert reconcile._derived_orphans(set(), True) == ["orphanw"]
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


def test_neo4j_orphan_worlds_lists_non_valid_world_ids():
    import neo4j

    from sherpa.ingest import world_neo4j

    class FakeResult:
        def __init__(self, rows): self._rows = rows
        def data(self): return self._rows

    class FakeSession:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def run(self, cy, **kw):
            assert "DISTINCT" in cy and "DELETE" not in cy      # 一覧を読むだけ（消すのは delete_world）
            return FakeResult([{"w": "keepw"}, {"w": "orphanw"}])

    class FakeDriver:
        def session(self): return FakeSession()
        def close(self): pass

    o_drv = neo4j.GraphDatabase.driver
    neo4j.GraphDatabase.driver = lambda *a, **k: FakeDriver()
    try:
        assert world_neo4j.orphan_worlds({"keepw"}, "bolt://x", "u", "p") == ["orphanw"]
    finally:
        neo4j.GraphDatabase.driver = o_drv


def _offline_cleanup(monkeypatch):
    """掃除の行の入口（DB）を手元の行に差し替える。ES への問い合わせも止める。掃除した world の順と、行を消した world を返す。"""
    import contextlib

    from sherpa import es_index
    from sherpa.store import replace_rows
    from tests._replace_row import offline_row
    began, dropped = [], []

    @contextlib.contextmanager
    def _begin(world):
        began.append(world)
        yield offline_row(world, kind="cleanup")

    monkeypatch.setattr(replace_rows, "begin_cleanup", _begin)
    monkeypatch.setattr(replace_rows, "drop_own", lambda drop: dropped.append(drop.id))
    monkeypatch.setattr(replace_rows, "stale_cleanup_worlds", lambda: [])
    monkeypatch.setattr(es_index, "delete_world", lambda w, **kw: True)
    return began, dropped


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
    began, dropped = _offline_cleanup(monkeypatch)
    o_db, o_local, o_es = store.list_worlds_db, reconcile._local_world_ids, es_index.orphan_indices
    o_del = es_index.delete_orphan
    o_ldw, o_rd = store.list_document_worlds, store.replace_documents
    store.list_worlds_db = lambda *a, **k: [{"world_id": "keepw"}]
    reconcile._local_world_ids = lambda: {"localw"}                # fixtures/kb 由来をスタブ
    es_index.orphan_indices = lambda valid: (seen.update(valid=set(valid)) or ["sherpa-kb-gone"])  # 実ESに触れない
    es_index.delete_orphan = lambda alias, **kw: None
    store.list_document_worlds = lambda: ["keepw", "localw", "orphanw"]
    cleared_docs = []
    store.replace_documents = lambda w, rows, **_kw: cleared_docs.append(w) or 0
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
            store.list_worlds_db, reconcile._local_world_ids, es_index.orphan_indices = o_db, o_local, o_es
            store.list_document_worlds, store.replace_documents = o_ldw, o_rd
            _restore_derived(o)


# ---- バッチ2・3番（2026-07-03）: documents 台帳（PG）は従来 reconcile の対象外だった（実際に
# 本番 dev で v1（fixtures 由来）残骸が Postgres documents/Neo4j に残っていた事象を受けての拡張）。

def test_documents_reconcile_deletes_only_orphan_worlds(monkeypatch, tmp_path):
    """documents 台帳のうち valid に無い world だけ、掃除の行を持って `replace_documents(world, [])` で消す。"""
    from sherpa import reconcile, store
    began, dropped = _offline_cleanup(monkeypatch)
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path))   # 実際の派生物の置き場には触れない
    cleared = []
    monkeypatch.setattr(store, "list_document_worlds", lambda: ["keepw", "orphanw"])
    monkeypatch.setattr(store, "replace_documents", lambda w, rows, **_kw: cleared.append(w) or 0)
    failed: list = []
    out = reconcile._reconcile_worlds({"keepw"}, [], False, failed)
    assert out["documents"] == ["orphanw"] and failed == []
    assert cleared == ["orphanw"] and began == ["orphanw"] and len(dropped) == 1


def test_documents_reconcile_skips_on_list_failure_without_raising():
    """一覧取得（DB）に失敗しても reconcile 全体を落とさない（best-effort・fail-safe）。"""
    from sherpa import reconcile, store
    o_list = store.list_document_worlds

    def boom():
        raise RuntimeError("db down")

    store.list_document_worlds = boom
    failed: list = []
    try:
        assert reconcile._documents_orphans({"keepw"}, failed) == [] and failed == ["documents"]
    finally:
        store.list_document_worlds = o_list


def test_documents_reconcile_individual_failure_does_not_stop_others(monkeypatch, tmp_path):
    """1件の削除失敗が他の world の削除を止めず、失敗した world の掃除の行は残す（次回に続きから消す）。"""
    from sherpa import reconcile, store
    began, dropped = _offline_cleanup(monkeypatch)
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path))   # 実際の派生物の置き場には触れない
    monkeypatch.setattr(store, "list_document_worlds", lambda: ["orphan1", "orphan2"])

    def flaky(w, rows, **_kw):
        if w == "orphan1":
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr(store, "replace_documents", flaky)
    failed: list = []
    out = reconcile._reconcile_worlds(set(), [], False, failed)
    assert out["documents"] == ["orphan2"] and failed == ["documents:orphan1"]
    assert began == ["orphan1", "orphan2"] and len(dropped) == 1      # orphan1 の行は消さない


def test_stale_cleanup_row_without_leftovers_is_cleared_by_the_next_reconcile(monkeypatch, tmp_path):
    """何も残っていない world の止まった掃除の行も、次の reconcile が拾って消す（valid の world は拾わない）。"""
    from sherpa import reconcile, store
    from sherpa.store import replace_rows
    began, dropped = _offline_cleanup(monkeypatch)
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path))   # 実際の派生物の置き場には触れない
    monkeypatch.setattr(store, "list_document_worlds", lambda: [])
    monkeypatch.setattr(store, "replace_documents", lambda w, rows, **_kw: 0)
    monkeypatch.setattr(replace_rows, "stale_cleanup_worlds", lambda: ["gone", "keepw"])
    failed: list = []
    out = reconcile._reconcile_worlds({"keepw"}, [], False, failed)
    assert began == ["gone"] and len(dropped) == 1 and failed == []
    assert out == {"neo4j": [], "documents": [], "derived": []}


def test_stale_es_cleanup_row_is_resumed_as_es_and_never_as_a_world(monkeypatch, tmp_path):
    """ES 用の掃除の行は資料フォルダの掃除に拾われない。ES の掃除は、まだ孤児の索引なら続きから消し、もう無い索引なら行だけ消す。"""
    from sherpa import es_index, reconcile, store
    from sherpa.store import replace_rows
    began, dropped = _offline_cleanup(monkeypatch)
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path))
    prefix = replace_rows.ES_CLEANUP_PREFIX
    monkeypatch.setattr(replace_rows, "stale_cleanup_worlds", lambda: [prefix + "sherpa-kb-a", prefix + "sherpa-kb-b"])
    monkeypatch.setattr(store, "list_document_worlds", lambda: [])
    failed: list = []
    reconcile._reconcile_worlds(set(), [], False, failed)
    assert began == [] and dropped == []                      # 資料フォルダの流れは ES の行に触れない

    deleted_orphans = []
    monkeypatch.setattr(es_index, "orphan_indices", lambda valid: ["sherpa-kb-a"])
    monkeypatch.setattr(es_index, "delete_orphan", lambda alias, **kw: deleted_orphans.append(alias))
    assert reconcile._reconcile_es(set(), failed) == ["sherpa-kb-a"]
    assert began == [prefix + "sherpa-kb-a", prefix + "sherpa-kb-b"] and len(dropped) == 2
    assert deleted_orphans == ["sherpa-kb-a"] and failed == []   # b は索引が無いので消さず、行だけ消す


def test_stale_es_cleanup_row_is_kept_while_es_is_unreachable(monkeypatch):
    """ES に届かない間は孤児の一覧を空とみなさず、止まった ES の掃除の行を消さない。"""
    from sherpa import es_index, reconcile
    from sherpa.store import replace_rows
    began, dropped = _offline_cleanup(monkeypatch)
    monkeypatch.setattr(replace_rows, "stale_cleanup_worlds", lambda: [replace_rows.ES_CLEANUP_PREFIX + "sherpa-kb-a"])

    def _down(*_a, **_kw):
        raise ConnectionRefusedError("es down")
    monkeypatch.setattr(es_index, "_req", _down)
    failed: list = []
    assert reconcile._reconcile_es(set(), failed) == []
    assert began == [] and dropped == [] and failed == ["es"]
