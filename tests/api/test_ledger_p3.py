"""文書台帳 受け入れ（鏡モデル・path 基準）: doc_id＝rel_path・走査由来・物理パス非露出。

旧 P3（version 別 basename・layer/scope_path・Postgres seed）は撤去。鏡では world の
フォルダ木を走査し、doc_id＝rel_path＋フォルダ由来の範囲メタを持つ。DL はパス基準（root 限定）。
PG/Neo4j 不要（走査のみ）。
"""
from __future__ import annotations

from _world_setup import SPEC, TAXCALC, TAXCPY

from sherpa import corpus_docs, doc_ledger

V = "v1"


def test_world_documents_are_path_based():
    by = {r["name"]: r for r in doc_ledger.documents_for(V)}
    assert TAXCALC in by and by[TAXCALC]["branch"] == "source"
    assert by[TAXCALC]["top_scope"] == "4期" and by[TAXCALC]["phase"] == "03_開発"
    assert SPEC in by and by[SPEC]["branch"] == "office"
    assert TAXCPY in by and by[TAXCPY]["phase"] == "00_共通"     # 鏡: 共通も普通のフォルダ


def test_public_documents_hide_physical_paths():
    pub = doc_ledger.public_documents(V)
    assert pub and all("original_path" not in d and "md_path" not in d and "path" not in d for d in pub)
    assert any(d["name"] == TAXCALC and d["top_scope"] == "4期" for d in pub)


def test_unknown_world_is_empty():
    assert corpus_docs.world_documents("v2") == []
    assert doc_ledger.documents_for("nope") == []


def test_public_documents_page_hides_sensitive_ledger_rows_and_count():
    """台帳（`store.documents`）に秘匿名（`credentials.xlsx`・`is_sensitive` 導入前に入った行を
    想定）が残っていても、`GET /documents` の一覧・件数の共通の出所である
    `doc_ledger.public_documents_page`（DB 台帳を直接読む高速経路）はそれを出さない
    （`text_kind.is_sensitive_doc_id`・台帳 #85〜#88）。`limit=None`（全件取得）なら件数も
    フィルタ後の実件数と一致する。"""
    from sherpa import store

    w = "docpage-sensitive-test"
    rows = [
        {"name": "credentials.xlsx", "layer": "version", "scope_path": None, "doctype": "表計算",
         "branch": "office", "original_path": None, "md_path": None, "status": "indexed"},
        {"name": "report.xlsx", "layer": "version", "scope_path": None, "doctype": "表計算",
         "branch": "office", "original_path": None, "md_path": None, "status": "indexed"},
    ]
    store.replace_documents(w, rows)
    try:
        docs, total = doc_ledger.public_documents_page(w, limit=None)
        names = [d["name"] for d in docs]
        assert "credentials.xlsx" not in names
        assert names == ["report.xlsx"]
        assert total == 1
    finally:
        store.replace_documents(w, [])


def test_dl_is_path_based():
    """原本DL はパス基準: 実在ソースは DL 可／実在しない設計書 rel・トラバーサルは None。"""
    assert doc_ledger.original_path(TAXCALC, V) is not None
    assert doc_ledger.original_path(TAXCPY, V) is not None
    assert doc_ledger.original_path("4期/02_設計/01_基本設計/未作成_NOEXIST.md", V) is None  # 実在しない
    assert doc_ledger.original_path("../etc/passwd", V) is None                            # トラバーサル拒否


def test_documents_endpoint(auth_disabled):
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    r = c.get("/documents", params={"world": V})
    assert r.status_code == 200, r.text
    docs = r.json()["documents"]
    assert any(x["name"] == TAXCALC for x in docs)
    assert all("original_path" not in x and "md_path" not in x for x in docs)   # パス非露出


def test_documents_endpoint_paginates_fallback_walk(auth_disabled):
    """S工事②: 台帳が空（未登録の dev fixture world）でも既存の実走査へフォールバックし、
    `total`/`has_more` は後方互換の追加フィールドとして正しく計算される。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    full = c.get("/documents", params={"world": V}).json()
    total = full["total"]
    assert total == len(full["documents"]) and full["has_more"] is False

    r = c.get("/documents", params={"world": V, "limit": 1, "offset": 0})
    body = r.json()
    assert r.status_code == 200 and len(body["documents"]) == 1
    assert body["total"] == total
    assert body["has_more"] == (total > 1)


def test_documents_endpoint_uses_ledger_when_populated(auth_disabled):
    """S工事②/RV1是正#2: 台帳（`store.documents`）に行があれば、フォルダを歩かず狭い SELECT
    だけでページング応答する（`store.count_documents`/`store.list_documents_page`）。importance
    が台帳に materialize 済みならそのまま通し（無ければ3キーとも付けない・§2 truth table）。

    ここでは DB 行→API 応答のマッピング（`doc_ledger._ledger_row_to_public` 経由）だけを対象に
    importance 3列を手投入する（DB 不要な範囲に留める・api マーカーのテスト）。ingest 時の実解決
    （`ingest/worker.py::_ledger_rows`）から API 応答までの結線全体は
    `tests/integration/test_importance_migration_rebuild.py::
    test_documents_endpoint_reflects_importance_from_real_ingest`（要 PG+Neo4j+ES・RV2是正#b3③）
    が実 ingest 経由で固定する。"""
    from fastapi.testclient import TestClient
    from sherpa import store
    from sherpa.api import app

    w = "docpage-ledger-test"
    rows = [{"name": f"4期/03_開発/01_ソース/F{i:03d}.cbl", "layer": "version",
             "scope_path": "4期", "doctype": "COBOL", "branch": "source",
             "original_path": None, "md_path": None, "status": "indexed"}
            for i in range(5)]
    rows[0]["importance"] = "高"
    rows[0]["importance_reason"] = "一次資料"
    rows[0]["importance_source"] = "4期/_重要度.txt:1行目"
    store.replace_documents(w, rows)
    try:
        c = TestClient(app)
        r1 = c.get("/documents", params={"world": w, "limit": 2, "offset": 0})
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        assert body1["total"] == 5 and body1["has_more"] is True
        assert len(body1["documents"]) == 2
        d0 = body1["documents"][0]
        assert d0["top_scope"] == "4期" and d0["phase"] == "03_開発" and d0["category"] == "01_ソース"
        assert d0["status"] == "ready"                     # 台帳の "indexed" → 表示語彙 "ready"
        assert d0["importance"] == "高"                     # ingest 時に materialize 済みならそのまま通す
        assert d0["importance_reason"] == "一次資料"
        assert d0["importance_source"] == "4期/_重要度.txt:1行目"
        d1 = body1["documents"][1]
        assert "importance" not in d1                       # materialize されていない行は3キーとも付けない

        r2 = c.get("/documents", params={"world": w, "limit": 2, "offset": 4})
        body2 = r2.json()
        assert len(body2["documents"]) == 1 and body2["has_more"] is False
    finally:
        store.replace_documents(w, [])


def test_documents_endpoint_no_limit_returns_all_and_omits_paging_keys(auth_disabled):
    """RV1是正#1/RV2是正#b3①: `limit`/`offset` 無指定は後方互換の全件返却——応答に
    `limit`/`offset` キー自体を含めない（旧クライアントが読まないフィールドを増やすだけに
    留める）。**旧既定 200 件の打切りでも検出できない偽陽性を避けるため 201 件以上**（旧実装の
    暗黙上限を確実に超える件数）で検証する——3件だけの旧テストは打切りが復活しても通ってしまう。
    """
    from fastapi.testclient import TestClient
    from sherpa import store
    from sherpa.api import app

    n = 205
    w = "docpage-nolimit-test"
    rows = [{"name": f"f{i:04d}.md", "layer": "version", "scope_path": None, "doctype": "設計書",
             "branch": "office", "original_path": None, "md_path": None, "status": "indexed"}
            for i in range(n)]
    store.replace_documents(w, rows)
    try:
        c = TestClient(app)
        body = c.get("/documents", params={"world": w}).json()
        assert len(body["documents"]) == n            # 旧既定 200 件で黙って切られていない
        assert body["total"] == n and body["has_more"] is False
        assert "limit" not in body and "offset" not in body
    finally:
        store.replace_documents(w, [])


def test_documents_endpoint_explicit_limit_includes_effective_values(auth_disabled):
    """RV1是正#1: `limit` を明示指定した時だけ、応答に実効 `limit`/`offset` を含める。"""
    from fastapi.testclient import TestClient
    from sherpa import store
    from sherpa.api import app

    w = "docpage-explicit-limit-test"
    rows = [{"name": f"f{i:03d}.md", "layer": "version", "scope_path": None, "doctype": "設計書",
             "branch": "office", "original_path": None, "md_path": None, "status": "indexed"}
            for i in range(3)]
    store.replace_documents(w, rows)
    try:
        c = TestClient(app)
        body = c.get("/documents", params={"world": w, "limit": 2, "offset": 1}).json()
        assert body["limit"] == 2 and body["offset"] == 1
        assert len(body["documents"]) == 2 and body["total"] == 3
    finally:
        store.replace_documents(w, [])


def test_documents_endpoint_boundary_limit_and_offset_reject_422(auth_disabled):
    """RV1是正#7: 境界表——`limit=0`/負/1001・負 `offset` は 422（`Query(ge=1,le=1000)`/`ge=0`）。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    for params in ({"world": V, "limit": 0}, {"world": V, "limit": -1}, {"world": V, "limit": 1001},
                  {"world": V, "limit": 1, "offset": -1}):
        r = c.get("/documents", params=params)
        assert r.status_code == 422, (params, r.text)


def test_documents_endpoint_offset_past_total_is_empty_not_error(auth_disabled):
    """RV1是正#7: `offset>=total` は空の `documents`＋`has_more=false`（エラーにしない）。"""
    from fastapi.testclient import TestClient
    from sherpa import store
    from sherpa.api import app

    w = "docpage-offset-overflow-test"
    store.replace_documents(w, [{"name": "a.md", "layer": "version", "scope_path": None,
                                 "doctype": "設計書", "branch": "office", "original_path": None,
                                 "md_path": None, "status": "indexed"}])
    try:
        c = TestClient(app)
        body = c.get("/documents", params={"world": w, "limit": 10, "offset": 100}).json()
        assert body["documents"] == [] and body["total"] == 1 and body["has_more"] is False
    finally:
        store.replace_documents(w, [])


def test_documents_endpoint_select_failure_propagates_not_silent_empty(auth_disabled, monkeypatch):
    """RV1是正#7: 台帳 SELECT 失敗はフォールバック（実走査/空応答）せず、明示的に伝播させる
    （silent degradation なしの家風・`store.count_documents` が例外を出したら 200 の偽装をしない）。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app

    def _boom(world, **kw):
        raise RuntimeError("db down")

    import sherpa.doc_ledger as doc_ledger_mod
    monkeypatch.setattr(doc_ledger_mod.store, "count_and_list_documents", _boom)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/documents", params={"world": V})
    assert r.status_code == 500


def test_documents_endpoint_does_not_wait_for_ingest_lock(auth_disabled):
    """取り込み（`store.world_lock` を最初から最後まで保持する）の最中でも、`GET /documents` は待たずに返る。"""
    import threading

    from fastapi.testclient import TestClient
    from sherpa import store
    from sherpa.api import app

    w = "docpage-ingestlock-test"
    store.replace_documents(w, [{"name": "a.md", "layer": "version", "scope_path": None,
                                 "doctype": "設計書", "branch": "office", "original_path": None,
                                 "md_path": None, "status": "indexed"}])
    entered, release = threading.Event(), threading.Event()

    def _hold_ingest():
        with store.world_lock(w):
            entered.set()
            release.wait(timeout=10)

    t = threading.Thread(target=_hold_ingest)
    t.start()
    try:
        assert entered.wait(timeout=5)
        r = TestClient(app).get("/documents", params={"world": w, "limit": 10})
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 1
    finally:
        release.set()
        t.join(timeout=10)
        store.replace_documents(w, [])


def test_documents_endpoint_returns_503_while_swapping(auth_disabled, monkeypatch):
    """rebind／削除が参照先を入れ替えている間、`GET /documents` は上限まで待って 503 を返す（無期限に待たない）。"""
    import threading
    import time

    from fastapi.testclient import TestClient
    from sherpa import doc_ledger as doc_ledger_mod
    from sherpa import store
    from sherpa.api import app

    monkeypatch.setattr(doc_ledger_mod, "SWAP_LOCK_WAIT_MS", 300)
    w = "docpage-swaplock-test"
    entered, release = threading.Event(), threading.Event()

    def _hold_swap():
        with store.world_swap_lock(w):
            entered.set()
            release.wait(timeout=10)

    t = threading.Thread(target=_hold_swap)
    t.start()
    try:
        assert entered.wait(timeout=5)
        t0 = time.monotonic()
        r = TestClient(app).get("/documents", params={"world": w, "limit": 10})
        assert r.status_code == 503, r.text
        assert "切り替えている最中" in r.json()["detail"]
        assert time.monotonic() - t0 < 5
    finally:
        release.set()
        t.join(timeout=10)


def test_documents_endpoint_ledger_queries_all_run_inside_swap_lock_scope(auth_disabled, monkeypatch):
    """台帳の読み取り（COUNT とページ）と blocked 突き合わせが、どちらも入れ替えロック（共有）を保持している間に呼ばれる。
    ロックは差し替えず、実際の advisory lock を深度追跡のラッパーで包む。"""
    import sherpa.doc_ledger as doc_ledger_mod
    from fastapi.testclient import TestClient
    from sherpa import corpus_docs as corpus_docs_mod
    from sherpa import store
    from sherpa.api import app

    real_lock = store.world_swap_lock_shared
    depth = {"n": 0}
    calls_inside: list[tuple[str, int]] = []

    class _DepthTrackingLock:
        def __init__(self, world, **kw):
            self._cm = real_lock(world, **kw)

        def __enter__(self):
            self._cm.__enter__()
            depth["n"] += 1
            return self

        def __exit__(self, *exc):
            depth["n"] -= 1
            return self._cm.__exit__(*exc)

    monkeypatch.setattr(doc_ledger_mod.store, "world_swap_lock_shared",
                        lambda world, **kw: _DepthTrackingLock(world, **kw))

    orig_read = doc_ledger_mod.store.count_and_list_documents
    orig_blocked = corpus_docs_mod.last_run_blocked_docs

    def _read(world, **kw):
        calls_inside.append(("read", depth["n"]))
        return orig_read(world, **kw)

    def _blocked(world, **kw):
        calls_inside.append(("blocked", depth["n"]))
        return orig_blocked(world, **kw)

    monkeypatch.setattr(doc_ledger_mod.store, "count_and_list_documents", _read)
    monkeypatch.setattr(corpus_docs_mod, "last_run_blocked_docs", _blocked)

    w = "docpage-lockdepth-test"
    store.replace_documents(w, [{"name": "a.md", "layer": "version", "scope_path": None,
                                 "doctype": "設計書", "branch": "office", "original_path": None,
                                 "md_path": None, "status": "indexed"}])
    try:
        r = TestClient(app).get("/documents", params={"world": w, "limit": 10})
        assert r.status_code == 200, r.text
    finally:
        store.replace_documents(w, [])

    assert calls_inside == [("read", 1), ("blocked", 1)], (
        f"台帳の読み取り／blocked 突き合わせが入れ替えロック（深度1）の外で呼ばれている: {calls_inside}")


def test_count_and_list_documents_reads_one_snapshot():
    """COUNT とページの間に取り込みが台帳を丸ごと入れ替えても、総件数とページは同じ世代になる。"""
    from sherpa import store

    w = "docpage-snapshot-test"
    row = lambda n: {"name": n, "layer": "version", "scope_path": None, "doctype": "設計書",
                     "branch": "office", "original_path": None, "md_path": None, "status": "indexed"}
    store.replace_documents(w, [row("a.md"), row("b.md")])
    try:
        from sherpa.store import documents as sd
        orig = sd._connect

        class _Conn:
            """COUNT の直後（ページ取得の前）に別接続で台帳を入れ替える。"""
            def __init__(self, c):
                self._c, self._swapped = c, False

            def __enter__(self):
                self._c.__enter__()
                return self

            def __exit__(self, *a):
                return self._c.__exit__(*a)

            def execute(self, sql, *a, **k):
                cur = self._c.execute(sql, *a, **k)
                if "COUNT(*)" in sql and not self._swapped:
                    self._swapped = True
                    cur = cur  # 結果は呼び出し側が先に fetch する
                    rows = cur.fetchall()
                    store.replace_documents(w, [row("x.md"), row("y.md"), row("z.md")])

                    class _R:
                        def fetchone(self_inner):
                            return rows[0]
                    return _R()
                return cur

        sd._connect = lambda: _Conn(orig())
        try:
            total, rows = sd.count_and_list_documents(w, limit=10)
        finally:
            sd._connect = orig
        assert total == 2 and [r["name"] for r in rows] == ["a.md", "b.md"]
    finally:
        store.replace_documents(w, [])


def test_documents_endpoint_ledger_fast_path_reconciles_blocked_and_unknown(auth_disabled, monkeypatch):
    """RV1是正#2/RV2是正#b2: 台帳高速経路も直近 run の blocked flag（DB のみ・フォルダを歩かない）で
    `unreadable`/`unknown` を突き合わせる——`documents_for()`（実走査版）と同じ粒度を保つ。
    `unknown` フォールバック（直近 run 自体が確認できない）は `documents_for()` と同じく
    `branch=="source"` の行だけが対象——Office/画像/Markdown（分類時点で内容確認済み）は
    誤って `unknown` にしない（b2 是正: 以前は台帳側が全 `status=="indexed"` 行を対象にしており
    Office 行まで `unknown` になっていた）。"""
    from fastapi.testclient import TestClient
    from sherpa import corpus_docs as corpus_docs_mod
    from sherpa import store
    from sherpa.api import app

    w = "docpage-ledger-blocked-test"
    rows = [{"name": "a.cbl", "layer": "version", "scope_path": None, "doctype": "COBOL",
             "branch": "source", "original_path": None, "md_path": None, "status": "indexed"},
            {"name": "b.cbl", "layer": "version", "scope_path": None, "doctype": "COBOL",
             "branch": "source", "original_path": None, "md_path": None, "status": "indexed"},
            {"name": "c.docx", "layer": "version", "scope_path": None, "doctype": "設計書",
             "branch": "office", "original_path": None, "md_path": None, "status": "indexed"}]
    store.replace_documents(w, rows)
    try:
        c = TestClient(app)

        # 直近 run が a.cbl を blocked と検出していれば、台帳側の "indexed" も "unreadable" へ倒す。
        monkeypatch.setattr(corpus_docs_mod, "last_run_blocked_docs", lambda world, deadline=None: {"a.cbl": "read_error"})
        body = c.get("/documents", params={"world": w}).json()
        by_name = {d["name"]: d for d in body["documents"]}
        assert by_name["a.cbl"]["status"] == "unreadable"
        assert by_name["b.cbl"]["status"] == "ready"
        assert by_name["c.docx"]["status"] == "ready"

        # 直近 run 自体が確認できなかった（None）＝fail-closed で "indexed" を "unknown" へ倒す
        # ——ただし branch=="source" の行だけ（b2是正）。
        monkeypatch.setattr(corpus_docs_mod, "last_run_blocked_docs", lambda world, deadline=None: None)
        body2 = c.get("/documents", params={"world": w}).json()
        by_name2 = {d["name"]: d for d in body2["documents"]}
        assert by_name2["a.cbl"]["status"] == "unknown"
        assert by_name2["b.cbl"]["status"] == "unknown"
        assert by_name2["c.docx"]["status"] == "ready"   # Office は誤って unknown にしない（b2是正の核）
    finally:
        store.replace_documents(w, [])
