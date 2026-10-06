"""`GET /worlds/{wid}/status`・`POST /worlds/{wid}/recount`・`POST /worlds/{wid}/reconvert` の API 層テスト。

401/403（認可ゲート）は `tests/api/test_authz_matrix.py` が全ルート横断で担保するため、ここでは
404/503/200 の業務ロジック（world 未登録・参照元不達・対象ファイル不在・成功時の応答形）に絞る。
`auth_disabled` fixture（合成 admin・ログイン不要）で検証し、Neo4j/実ファイルツリーは monkeypatch で
置き換える。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _no_es_attempts(monkeypatch):
    from sherpa import store
    monkeypatch.setattr(store, "get_recent_es_attempts", lambda wid, limit=200: [])


@pytest.fixture
def client(auth_disabled):
    from sherpa.api import app
    return TestClient(app, raise_server_exceptions=False)


def _stub_ingest_summary(monkeypatch, *, latest=None, published=None, es=None):
    """`_ingest_summary` の周辺（最新 run／反映済み run／ES run の狭い SELECT）を DB 不要に固定する。"""
    from sherpa import store
    monkeypatch.setattr(store, "get_latest_run_summary", lambda wid: latest)
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: published)
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: es)


class _Audits(list):
    """`store.audit` の呼び出し記録（action, outcome）。"""

    def pairs(self):
        return [(a[1], kw.get("outcome")) for a, kw in self]


def _stub_audit(monkeypatch) -> _Audits:
    from sherpa import store
    audits = _Audits()
    monkeypatch.setattr(store, "audit", lambda *a, **kw: audits.append((a, kw)))
    return audits


# ===================================================================================
# POST /worlds/{wid}/recount, /reconvert 共通の受付
# ===================================================================================

_ENDPOINTS = [("/worlds/w1/recount", None), ("/worlds/w1/reconvert", {"rel": "a.doc"})]


@pytest.mark.parametrize("path,body", _ENDPOINTS)
def test_unknown_world_returns_404(client, monkeypatch, path, body):
    from sherpa import store
    monkeypatch.setattr(store, "get_world", lambda wid: None)
    assert client.post(path, json=body).status_code == 404


@pytest.mark.parametrize("path,body", _ENDPOINTS)
def test_unreachable_root_returns_503(client, monkeypatch, path, body):
    from sherpa import store, worlds
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: None)
    assert client.post(path, json=body).status_code == 503


# ===================================================================================
# POST /worlds/{wid}/recount
# ===================================================================================

_RECOUNT_GEN_ROW = {"root_path": None, "last_sig": "sig1", "created_at": "2026-08-01T00:00:00+00:00",
                    "updated_at": "2026-08-01T00:00:00+00:00", "last_synced_at": "2026-08-01T00:00:00+00:00",
                    "last_scan_report_at": "2026-08-15T00:00:00+00:00"}


def _recount_env(monkeypatch, root, *, scan=None, cas=None):
    """root を持つ world を registry に載せ、走査と書き戻し（CAS）を差し替える。"""
    from sherpa import corpus_docs, store, worlds
    monkeypatch.setattr(store, "get_world", lambda wid: {**_RECOUNT_GEN_ROW, "world_id": wid,
                                                         "root_path": str(root)})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: root)
    monkeypatch.setattr(corpus_docs, "scan_report", scan or (lambda wid: corpus_docs.empty_scan_report()))
    if cas is not None:
        monkeypatch.setattr(store, "set_scan_report_if_unchanged", cas)


def _must_not_scan(wid):
    raise AssertionError("root が無い／ディレクトリでないのに走査してはいけない")


def test_recount_success_writes_cache_and_returns_summary(client, monkeypatch, tmp_path):
    """再集計後の応答（`_ingest_summary`）は `store.get_world` を再読みするため、fake store は書込を
    実際に反映するミュータブルな行にする。書き戻し（`set_scan_report_if_unchanged`）へ渡る
    binding/世代の値（読み取り時点の root_path・sig・created_at・updated_at・last_synced_at・
    last_scan_report_at）を確認する。"""
    from sherpa import corpus_docs, store, worlds
    _stub_ingest_summary(monkeypatch)
    row = {**_RECOUNT_GEN_ROW, "world_id": "w1", "root_path": str(tmp_path), "last_scan_report": None}
    monkeypatch.setattr(store, "get_world", lambda wid: row)
    monkeypatch.setattr(worlds, "world_dir", lambda wid: tmp_path)
    fresh_report = {**corpus_docs.empty_scan_report(), "scanned": 7, "indexed": 7}
    monkeypatch.setattr(corpus_docs, "scan_report", lambda wid: fresh_report)

    calls = []

    def _set_scan_report_if_unchanged(wid, report, *, expected_root_path, expected_sig,
                                      expected_created_at, expected_updated_at,
                                      expected_last_synced_at, expected_last_scan_report_at):
        calls.append((expected_root_path, expected_sig, expected_created_at, expected_updated_at,
                      expected_last_synced_at, expected_last_scan_report_at))
        row["last_scan_report"] = report
        row["last_scan_report_at"] = "2026-09-01T03:12:00+00:00"
        return True
    monkeypatch.setattr(store, "set_scan_report_if_unchanged", _set_scan_report_if_unchanged)

    r = client.post("/worlds/w1/recount")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["world_id"] == "w1"
    assert body["scanned"] == 7 and body["indexed"] == 7
    assert body["counts_as_of"] == "2026-09-01T03:12:00+00:00"
    assert calls == [(str(tmp_path), "sig1", _RECOUNT_GEN_ROW["created_at"], _RECOUNT_GEN_ROW["updated_at"],
                      _RECOUNT_GEN_ROW["last_synced_at"], _RECOUNT_GEN_ROW["last_scan_report_at"])]


def _cas_raises(wid, report, **kw):
    raise RuntimeError("db down")


def _cas_aba(wid, report, *, expected_root_path, expected_sig, expected_created_at,
             expected_updated_at, expected_last_synced_at, expected_last_scan_report_at):
    # 実 DB の CAS を模す: last_sig は一致するが last_synced_at が既に動いている（ABA）。
    return expected_last_synced_at == "2026-08-20T00:00:00+00:00"


@pytest.mark.parametrize("cas,status", [
    (_cas_raises, 503),                         # 書き込み失敗
    (lambda *a, **kw: False, 409),              # 走査中に sync/rebind/delete が割り込み binding/世代が食い違う
    # ABA: 走査中に別 sync が pre-invalidate→同じ内容へ再確定すると last_sig だけは一致するが
    # last_synced_at は動いている＝タイムスタンプ列も CAS 条件に含めて検知する
    (_cas_aba, 409),
])
def test_recount_write_failure_or_changed_binding_or_generation(client, monkeypatch, tmp_path, cas, status):
    """409 は再試行せず終える（古い走査結果を新しい世代へ誤って結び付けない）。"""
    _recount_env(monkeypatch, tmp_path, cas=cas)
    assert client.post("/worlds/w1/recount").status_code == status


def test_recount_root_replaced_during_scan_returns_503_without_saving(client, monkeypatch, tmp_path):
    """走査の直前・直後で root を lstat し、同一ディレクトリ実体（st_dev/st_ino 一致）でなければ 503 とし
    走査結果を保存しない（同じパスへ別ディレクトリが再作成されたケースは CAS だけでは検知できない）。
    tmpfs 等では inode が再利用され得るため、`Path.lstat` を対象パスだけへ scope した monkeypatch で
    決定的に再現する（無関係なパスは実装へ委譲）。"""
    import os
    from pathlib import Path as PathCls

    save_calls = []
    _stub_ingest_summary(monkeypatch)
    _recount_env(monkeypatch, tmp_path, cas=lambda *a, **kw: save_calls.append(1) or True)

    target = str(tmp_path)
    real_lstat = PathCls.lstat
    calls = {"n": 0}

    def _fake_lstat(self, *, follow_symlinks=True):
        if str(self) != target:
            return real_lstat(self)
        calls["n"] += 1
        base = real_lstat(self)
        if calls["n"] == 1:                 # 走査前（正常）
            return base
        # 走査後: st_ino が異なる別ディレクトリへ置換された想定。
        return os.stat_result((base.st_mode, base.st_ino + 1, base.st_dev, base.st_nlink,
                               base.st_uid, base.st_gid, base.st_size,
                               int(base.st_atime), int(base.st_mtime), int(base.st_ctime)))
    monkeypatch.setattr(PathCls, "lstat", _fake_lstat)

    r = client.post("/worlds/w1/recount")
    assert r.status_code == 503, r.text
    assert save_calls == []                 # 消失/置換を検知したら保存経路には一切到達しない


@pytest.mark.parametrize("kind", ["vanished", "regular_file"])
def test_recount_root_vanished_or_not_a_directory_returns_503_without_scan_or_save(
        client, monkeypatch, tmp_path, kind):
    """走査開始前に root が消えている（`lstat` 失敗）／通常ファイル（symlink 置換等）に化けている場合は、
    走査も保存もせず 503。通常ファイルは st_dev/st_ino の同一性比較では検知できないため、
    `stat.S_ISDIR` で pre 側で拒否する。"""
    root = tmp_path / "vanished"
    if kind == "regular_file":
        root = tmp_path / "not_a_directory"
        root.write_text("x")
    save_calls = []
    _recount_env(monkeypatch, root, scan=_must_not_scan, cas=lambda *a, **kw: save_calls.append(1) or True)
    r = client.post("/worlds/w1/recount")
    assert r.status_code == 503, r.text
    assert save_calls == []


# ===================================================================================
# POST /worlds/{wid}/reconvert
# ===================================================================================

def _reconvert_env(monkeypatch, tmp_path, *, name="旧資料.doc", original=True, drop=lambda cache_root, rel: True,
                   run_locked=None):
    """world・原本・旧形式キャッシュ削除・取り込み本体を差し替える。"""
    from sherpa import doc_ledger, store, worlds
    from sherpa.ingest import worker as ingest_worker
    from sherpa.ingest.arms import legacy_convert
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: tmp_path)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda wid: tmp_path / "derived" / "md")
    src = tmp_path / name
    src.write_bytes(b"x")
    monkeypatch.setattr(doc_ledger, "original_path", lambda rel, wid: src if original and rel == name else None)
    monkeypatch.setattr(legacy_convert, "drop_cache_entry", drop)
    if run_locked is not None:
        monkeypatch.setattr(ingest_worker, "_run_locked", run_locked)


def _published(wid, **kw):
    return {"world": wid, "status": "auto_published", "ledger": 1, "flags": [], "nodes": 0, "edges": 0,
            "run": {"id": 1}}


def test_reconvert_unknown_rel_returns_404(client, monkeypatch, tmp_path):
    _reconvert_env(monkeypatch, tmp_path, original=False)
    assert client.post("/worlds/w1/reconvert", json={"rel": "missing.doc"}).status_code == 404


def test_reconvert_success_drops_cache_and_runs_directly(client, monkeypatch, tmp_path):
    """`sync(force=True)` ではなく `_run_locked` を直接1回実行する（二重走査を避ける）。"""
    _stub_ingest_summary(monkeypatch)
    dropped = {}
    run_calls = []

    def _fake_run_locked(wid, *, reflect, created_by, scan_root, op="sync"):
        # `op`（Webhook 通知の情報用途のみ）: reconvert は "refresh" を渡す。
        run_calls.append({"wid": wid, "reflect": reflect, "created_by": created_by,
                          "scan_root": scan_root, "op": op})
        return {**_published(wid), "ledger": 3}

    _reconvert_env(monkeypatch, tmp_path, run_locked=_fake_run_locked,
                   drop=lambda cache_root, rel: dropped.setdefault("rel", rel) or True)
    audits = _stub_audit(monkeypatch)

    r = client.post("/worlds/w1/reconvert", json={"rel": "旧資料.doc"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["world_id"] == "w1" and body["rel"] == "旧資料.doc"
    assert body["changed"] is True and body["status"] == "auto_published"
    assert dropped["rel"] == "旧資料.doc"
    assert run_calls == [{"wid": "w1", "reflect": True, "created_by": "admin", "scan_root": None,
                          "op": "refresh"}]
    # pre/post 監査（actor・world・rel・結果）が両方記録される。
    assert audits.pairs() == [("world.reconvert_requested", "success"), ("world.reconverted", "success")]
    assert audits[1][1]["detail"] == {"world": "w1", "rel": "旧資料.doc"}


@pytest.mark.parametrize("case", ["run_failed", "cache_drop_failed"])
def test_reconvert_failure_returns_503_and_audits_failure(client, monkeypatch, tmp_path, case):
    """取り込み run が failed／旧形式キャッシュの削除に失敗したら 503 で、失敗を監査する。
    キャッシュ削除の失敗は sync 前に止める（安定して壊れたファイルを「再変換した」ことにしない・
    `_run_locked` は一切呼ばれない）。"""
    run_calls = []

    def _run_failed(wid, **kw):
        run_calls.append(wid)
        return {**_published(wid), "status": "failed", "ledger": 0, "flags": [{"reason": "graph_reflect_failed"}]}

    _reconvert_env(monkeypatch, tmp_path, run_locked=_run_failed,
                   drop=(lambda cache_root, rel: False) if case == "cache_drop_failed"
                   else (lambda cache_root, rel: True))
    audits = _stub_audit(monkeypatch)

    r = client.post("/worlds/w1/reconvert", json={"rel": "旧資料.doc"})
    assert r.status_code == 503, r.text
    assert run_calls == ([] if case == "cache_drop_failed" else ["w1"])
    assert audits.pairs() == [("world.reconvert_requested", "success"), ("world.reconverted", "failure")]


def test_reconvert_non_legacy_ext_skips_cache_drop(client, monkeypatch, tmp_path):
    """legacy 拡張子（.doc/.xls/.ppt）でないファイルはキャッシュ削除を試みない。"""
    _stub_ingest_summary(monkeypatch)
    drop_calls = []
    _reconvert_env(monkeypatch, tmp_path, name="新資料.docx", run_locked=lambda wid, **kw: _published(wid),
                   drop=lambda cache_root, rel: drop_calls.append(rel) or True)
    _stub_audit(monkeypatch)

    r = client.post("/worlds/w1/reconvert", json={"rel": "新資料.docx"})
    assert r.status_code == 200, r.text
    assert drop_calls == []


# ===================================================================================
# GET /worlds/{wid}/status（定数時間契約）
# ===================================================================================

def _status_row(root, *, label=None, last_scan_report=None, last_scan_report_at=None):
    return lambda wid: {"world_id": wid, "root_path": str(root), "label": label, "last_synced_at": None,
                        "last_scan_report": last_scan_report, "last_scan_report_at": last_scan_report_at}


def _get_status(client, monkeypatch, root, *, label=None, report=None, report_at=None, **summary):
    """status 専用の狭い SELECT（`get_world_status_row`）と周辺 run を固定して GET する。"""
    from sherpa import store
    monkeypatch.setattr(store, "get_world_status_row",
                        _status_row(root, label=label, last_scan_report=report, last_scan_report_at=report_at))
    _stub_ingest_summary(monkeypatch, **summary)
    return client.get("/worlds/w1/status")


def test_status_unknown_world_returns_404(client, monkeypatch):
    from sherpa import store
    monkeypatch.setattr(store, "get_world_status_row", lambda wid: None)
    assert client.get("/worlds/w1/status").status_code == 404


@pytest.mark.parametrize("case", ["missing_root", "regular_file", "db_failure"])
def test_status_unreachable_root_or_db_failure_returns_503(client, monkeypatch, tmp_path, case):
    """到達確認は `stat(follow_symlinks=False)` 1回（`worlds.world_dir` は呼ばない）。通常ファイルに
    化けた root は同じ stat 結果に `S_ISDIR` を適用して拒否する（追加 I/O 無し）。行の取得自体の失敗は
    全ゼロへ縮退せず 503。"""
    from sherpa import store, worlds

    def _must_not_call(*a, **kw):
        raise AssertionError("world_status は worlds.world_dir を呼んではいけない")
    monkeypatch.setattr(worlds, "world_dir", _must_not_call)

    if case == "db_failure":
        def _boom(wid):
            raise RuntimeError("db down")
        monkeypatch.setattr(store, "get_world_status_row", _boom)
    else:
        root = "/no/such/path"
        if case == "regular_file":
            root = tmp_path / "not_a_directory"
            root.write_text("x")
        monkeypatch.setattr(store, "get_world_status_row", _status_row(root))
    assert client.get("/worlds/w1/status").status_code == 503


def test_status_success_does_not_walk_or_query_live_graph_es(client, monkeypatch, tmp_path):
    """成功パスは `corpus_docs.scan_report`／`es_index.count`／`store.get_world`（`last_manifest` まで
    持つ重い SELECT）を一切呼ばない（`worlds.world_dir` も呼ばない・stat のみ）。"""
    from sherpa import corpus_docs, es_index, store

    def _must_not_call(name):
        def _boom(*a, **kw):
            raise AssertionError(f"world_status は {name} を呼んではいけない")
        return _boom
    monkeypatch.setattr(corpus_docs, "scan_report", _must_not_call("corpus_docs.scan_report"))
    monkeypatch.setattr(es_index, "count", _must_not_call("es_index.count"))
    monkeypatch.setattr(store, "get_world", _must_not_call("store.get_world"))
    cached = {**corpus_docs.empty_scan_report(), "scanned": 5, "indexed": 5}

    r = _get_status(client, monkeypatch, tmp_path, label="テスト", report=cached,
                    report_at="2026-09-01T03:12:00+00:00",
                    published={"published_snapshot": {"nodes": 3, "edges": 2}},
                    es={"extraction_snapshot": {"es": {"available": True, "error": None, "chunks": 6}}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scanned"] == 5 and body["indexed"] == 5
    assert body["counts_as_of"] == "2026-09-01T03:12:00+00:00"
    assert body["graph_nodes"] == 3 and body["graph_edges"] == 2
    assert body["es_chunks"] == 6


def test_status_legacy_scan_report_is_filled_and_counts_as_of_is_unknown_until_full_report(
        client, monkeypatch, tmp_path):
    """`sensitive_excluded`/`unreachable_as_text`/`unreachable_as_text_by_ext` を追加する前に保存された
    旧形式の `last_scan_report` でも 500 にならず、欠落は `empty_scan_report()` の既定値（0/空）で補う。
    `counts_as_of` は補完時に `None`（未集計）へ倒す（古い時刻を伴う実測0件に見せない・`scanned`/`indexed`
    は実測値のまま）。完全な `last_scan_report` が保存された後は、その時刻がそのまま返る。"""
    from sherpa import corpus_docs
    legacy_cached = {"scanned": 5, "indexed": 5, "by_doctype": {}, "office_md": 0,
                     "skipped_office": 0, "office_failed": 0, "skipped_other": 0, "skipped_ext": {},
                     "analyzer_declined": 0, "analyzer_declined_as_document": 0, "unreadable": 0,
                     "document_count": 5}
    r = _get_status(client, monkeypatch, tmp_path, label="テスト", report=legacy_cached,
                    report_at="2026-01-01T00:00:00+00:00")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scanned"] == 5 and body["indexed"] == 5
    assert body["sensitive_excluded"] == 0
    assert body["unreachable_as_text"] == 0
    assert body["unreachable_as_text_by_ext"] == {}
    assert body["counts_as_of"] is None

    complete = {**corpus_docs.empty_scan_report(), "scanned": 5, "indexed": 5}
    r2 = _get_status(client, monkeypatch, tmp_path, label="テスト", report=complete,
                     report_at="2026-02-01T00:00:00+00:00")
    assert r2.status_code == 200, r2.text
    assert r2.json()["counts_as_of"] == "2026-02-01T00:00:00+00:00"


@pytest.mark.parametrize("es_state", [
    {"available": False, "error": None, "chunks": 6},
    {"available": True, "error": "bulk_failed", "chunks": 6},
])
def test_status_es_chunks_is_none_when_available_false_or_error_set(client, monkeypatch, tmp_path, es_state):
    """ES は `available is True` かつ `error` 無しの時だけ chunks を件数として見せる（delete_failed で
    旧索引が残ったまま「0件」を返す・bulk_errors が投入予定件数を実成功件数と偽る、のどちらも避ける。
    不明な時は None＝UI「不明」表示）。"""
    r = _get_status(client, monkeypatch, tmp_path, published={"published_snapshot": {"nodes": 1, "edges": 1}},
                    es={"extraction_snapshot": {"es": es_state}})
    assert r.status_code == 200, r.text
    assert r.json()["es_chunks"] is None


def test_status_es_state_distinguishes_failed_unavailable_reflecting(client, monkeypatch, tmp_path):
    """`es_state`: 失敗した ES run（未反映の run も含む）は failed・旧索引が残ると言えるのは旧索引に触れる前の
    失敗で、かつ過去に成功した記録があるときだけ。接続不可は unavailable・ES の段の実行中だけ reflecting。"""
    from sherpa import store
    run = {"v": None}
    monkeypatch.setattr(store, "get_world_status_row", _status_row(tmp_path))
    monkeypatch.setattr(store, "get_latest_run_summary", lambda wid: run["v"])
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: None)
    ok = {"available": True, "error": None, "chunks": 3}

    def es(*attempts):
        monkeypatch.setattr(store, "get_recent_es_attempts", lambda wid, limit=200: list(attempts))
        return client.get("/worlds/w1/status").json()

    emb = {"available": True, "error": "embedding_cloud_unavailable", "chunks": 0}
    b = es({"available": True, "error": "bulk_errors", "chunks": 0}, ok)
    assert (b["es_state"], b["es_index_kept"]) == ("failed", False) and b["es_error"]
    assert es(emb, ok)["es_index_kept"] is True
    assert "クラウド" not in es(emb, ok)["es_error"]
    assert es(emb)["es_index_kept"] is False                      # 初回: 旧索引の記録なし
    assert es(emb, {"available": True, "error": None, "chunks": 0})["es_index_kept"] is False
    assert es({"available": True, "error": "delete_failed", "chunks": 0}, ok)["es_index_kept"] is False
    bulk = {"available": True, "error": "bulk_failed", "chunks": 0}
    assert es(emb, bulk, ok)["es_index_kept"] is False            # 成功の後に索引を消す失敗がある
    assert es(emb, emb, ok)["es_index_kept"] is True              # 索引に触れない失敗は読み飛ばす
    assert es(emb, *([emb] * 50), ok)["es_index_kept"] is True    # 窓の奥の成功も拾う（limit は store 側）
    assert es({"available": False, "error": None})["es_state"] == "unavailable"
    assert es(ok)["es_state"] == "ok"
    def prog(stage):
        return {"status": "extracting", "extraction_snapshot": None, "progress": {
            "stage": stage, "stage_label": "x", "done": None, "total": None,
            "updated_at": "2026-01-01T00:00:00+00:00"}}
    run["v"] = prog("graph_build")
    assert es(emb, ok)["es_state"] == "failed"                    # ES 以外の段の実行中は直前の記録
    run["v"] = prog("es_index")
    assert es(emb, ok)["es_state"] == "reflecting"


def test_status_es_chunks_survive_a_newer_pg_replace_failed_run(client, monkeypatch, tmp_path):
    """台帳（PG）replace 失敗の run は Neo4j へは反映済みでも ES 段には未到達——ES 用の別クエリ
    （`get_latest_es_run_summary`）に分離したことで、実際に ES へ触れた古い run の件数が新しい run に
    隠されず表示され続ける。"""
    r = _get_status(
        client, monkeypatch, tmp_path,
        published={"published_snapshot": {"nodes": 20, "edges": 15}},   # 新しい pg_replace 失敗 run（es 無し）
        es={"extraction_snapshot": {"es": {"available": True, "error": None, "chunks": 34}}})   # 古い・ES 到達 run
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["graph_nodes"] == 20 and body["graph_edges"] == 15
    assert body["es_chunks"] == 34


def test_status_flags_are_capped_with_total_and_truncated(client, monkeypatch, tmp_path):
    """`extraction_snapshot.flags` は `_STATUS_FLAGS_LIMIT` 件で打ち切り、`last_run_flags_total`/
    `last_run_flags_truncated` で打切りの有無を明示する（`last_run_warnings` もこの打切り後の分だけ）。"""
    from sherpa.routers import worlds as worlds_router
    limit = worlds_router._STATUS_FLAGS_LIMIT
    many_flags = [{"doc": None, "action": "warn", "reason": f"warn_{i}"} for i in range(limit + 5)]
    r = _get_status(client, monkeypatch, tmp_path,
                    latest={"status": "auto_published_with_flags", "extraction_snapshot": {"flags": many_flags}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["last_run_flags_total"] == limit + 5
    assert body["last_run_flags_truncated"] is True
    assert len(body["last_run_warnings"]) == limit
