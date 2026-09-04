"""ING-2 `GET /worlds/{wid}/status`・`POST /worlds/{wid}/recount`・ING-1 `POST /worlds/{wid}/reconvert`
の API 層テスト。

401/403（認可ゲートの有無）は `tests/api/test_authz_matrix.py`（POLICY 表に両ルートを追加済み）が
全ルート横断で担保するため、ここでは 404/503/200 の業務ロジック（world 未登録・参照元不達・
対象ファイル不在・成功時の応答形）に絞る。`auth_disabled` fixture（合成 admin・ログイン不要）で
高速に検証し、Neo4j/実ファイルツリーは monkeypatch で置き換える（DB 依存を最小化）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(auth_disabled):
    from sherpa.api import app
    return TestClient(app, raise_server_exceptions=False)


def _stub_ingest_summary(monkeypatch):
    """`_ingest_summary` の周辺（最新 run／反映済み run の狭い SELECT）を DB 不要に固定する。
    `_ingest_summary` はもう `graph_view`/`es_index.count` を呼ばない。"""
    from sherpa import store
    monkeypatch.setattr(store, "get_latest_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: None)


# ===================================================================================
# POST /worlds/{wid}/recount
# ===================================================================================

def test_recount_unknown_world_returns_404(client, monkeypatch):
    from sherpa import store
    monkeypatch.setattr(store, "get_world", lambda wid: None)
    r = client.post("/worlds/w1/recount")
    assert r.status_code == 404, r.text


def test_recount_unreachable_root_returns_503(client, monkeypatch):
    from sherpa import store, worlds
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: None)
    r = client.post("/worlds/w1/recount")
    assert r.status_code == 503, r.text


_RECOUNT_GEN_ROW = {"root_path": None, "last_sig": "sig1", "created_at": "2026-08-01T00:00:00+00:00",
                    "updated_at": "2026-08-01T00:00:00+00:00", "last_synced_at": "2026-08-01T00:00:00+00:00",
                    "last_scan_report_at": "2026-08-15T00:00:00+00:00"}


def test_recount_success_writes_cache_and_returns_summary(client, monkeypatch, tmp_path):
    """再集計後の応答（`_ingest_summary`）は `store.get_world` を**再読み**するため、
    fake store は書込を実際に反映するミュータブルな行にする（読み書きの往復を素通りさせない）。
    走査は排他ロックの外（`worlds.pin_world_root` 経由）で行う——ここではその境界を
    直接検証せず、書き戻し（`set_scan_report_if_unchanged`）へ渡る binding/世代の値を確認する。"""
    from sherpa import corpus_docs, store, worlds
    _stub_ingest_summary(monkeypatch)
    row = {**_RECOUNT_GEN_ROW, "world_id": "w1", "root_path": str(tmp_path),
          "last_scan_report": None}
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
    # 読み取り時点の binding/世代（sig・created_at・updated_at・last_synced_at・last_scan_report_at）
    # をそのまま再確認に使う。
    assert calls == [(str(tmp_path), "sig1", _RECOUNT_GEN_ROW["created_at"], _RECOUNT_GEN_ROW["updated_at"],
                      _RECOUNT_GEN_ROW["last_synced_at"], _RECOUNT_GEN_ROW["last_scan_report_at"])]


def test_recount_store_write_failure_returns_503(client, monkeypatch, tmp_path):
    from sherpa import corpus_docs, store, worlds
    monkeypatch.setattr(store, "get_world", lambda wid: {
        **_RECOUNT_GEN_ROW, "world_id": wid, "root_path": str(tmp_path)})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: tmp_path)
    monkeypatch.setattr(corpus_docs, "scan_report", lambda wid: corpus_docs.empty_scan_report())

    def _boom(wid, report, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "set_scan_report_if_unchanged", _boom)
    r = client.post("/worlds/w1/recount")
    assert r.status_code == 503, r.text


def test_recount_binding_or_generation_changed_returns_409(client, monkeypatch, tmp_path):
    """走査中に他の sync/rebind/delete が割り込み、書き戻し時に binding（root_path）／
    世代（last_sig 等）が読み取り時点と食い違ったら 409 で終える（再試行しない・古い走査結果を
    新しい世代へ誤って結び付けない）。"""
    from sherpa import corpus_docs, store, worlds
    monkeypatch.setattr(store, "get_world", lambda wid: {
        **_RECOUNT_GEN_ROW, "world_id": wid, "root_path": str(tmp_path)})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: tmp_path)
    monkeypatch.setattr(corpus_docs, "scan_report", lambda wid: corpus_docs.empty_scan_report())
    monkeypatch.setattr(store, "set_scan_report_if_unchanged", lambda *a, **kw: False)
    r = client.post("/worlds/w1/recount")
    assert r.status_code == 409, r.text


def test_recount_aba_sig_reverted_to_same_value_is_still_detected_via_timestamps(client, monkeypatch, tmp_path):
    """ABA 対策: 走査中に別の sync が pre-invalidate→同じ内容へ再確定すると `last_sig` だけは
    読み取り時点と一致してしまうが、その sync は必ず `last_synced_at`（と場合により
    `last_scan_report_at`）を更新している。CAS 条件にこれらのタイムスタンプ列も含めることで、
    `last_sig` が一致していても不一致（409）として検知できることを固定する。"""
    from sherpa import corpus_docs, store, worlds
    monkeypatch.setattr(store, "get_world", lambda wid: {
        **_RECOUNT_GEN_ROW, "world_id": wid, "root_path": str(tmp_path)})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: tmp_path)
    monkeypatch.setattr(corpus_docs, "scan_report", lambda wid: corpus_docs.empty_scan_report())

    def _fake_cas(wid, report, *, expected_root_path, expected_sig, expected_created_at,
                 expected_updated_at, expected_last_synced_at, expected_last_scan_report_at):
        # 実 DB の CAS を模す: last_sig は一致するが last_synced_at が既に動いている（ABA）。
        current_last_synced_at = "2026-08-20T00:00:00+00:00"      # 呼び出し元の読み取り時点より後
        return expected_last_synced_at == current_last_synced_at
    monkeypatch.setattr(store, "set_scan_report_if_unchanged", _fake_cas)
    r = client.post("/worlds/w1/recount")
    assert r.status_code == 409, r.text


def test_recount_root_replaced_during_scan_returns_503_without_saving(client, monkeypatch, tmp_path):
    """走査の直前・直後で root を lstat し、同一ディレクトリ実体（st_dev/st_ino 一致）でなければ
    503 とし、走査結果を保存しない（binding/世代の CAS だけでは、同じパスへ別ディレクトリが
    再作成されたケースを検知できない）。

    実ファイルシステムで rmdir→mkdir しても環境（tmpfs 等）次第で inode が再利用され偽陰性に
    なりうるため、`Path.lstat` を対象パスだけへ scope した monkeypatch で決定的に再現する
    （無関係な `Path.lstat` 呼び出しは実装へ委譲・対象外パスまで壊さない）。
    """
    import os
    from pathlib import Path as PathCls

    from sherpa import corpus_docs, store, worlds
    _stub_ingest_summary(monkeypatch)
    monkeypatch.setattr(store, "get_world", lambda wid: {
        **_RECOUNT_GEN_ROW, "world_id": wid, "root_path": str(tmp_path)})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: tmp_path)
    monkeypatch.setattr(corpus_docs, "scan_report", lambda wid: corpus_docs.empty_scan_report())

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

    save_calls = []
    monkeypatch.setattr(store, "set_scan_report_if_unchanged",
                        lambda *a, **kw: save_calls.append(1) or True)
    r = client.post("/worlds/w1/recount")
    assert r.status_code == 503, r.text
    assert save_calls == []                 # 消失/置換を検知したら保存経路には一切到達しない


def test_recount_root_vanishes_before_scan_returns_503_without_saving(client, monkeypatch, tmp_path):
    """走査開始前に root 自体が消えていれば（`lstat` が失敗）503 とし、保存しない。"""
    from sherpa import corpus_docs, store, worlds
    missing = tmp_path / "vanished"     # 実際には存在しないパス
    monkeypatch.setattr(store, "get_world", lambda wid: {
        **_RECOUNT_GEN_ROW, "world_id": wid, "root_path": str(missing)})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: missing)   # 存在確認は済ませたことにする

    def _must_not_scan(wid):
        raise AssertionError("root が無いのに走査してはいけない")
    monkeypatch.setattr(corpus_docs, "scan_report", _must_not_scan)
    save_calls = []
    monkeypatch.setattr(store, "set_scan_report_if_unchanged",
                        lambda *a, **kw: save_calls.append(1) or True)
    r = client.post("/worlds/w1/recount")
    assert r.status_code == 503, r.text
    assert save_calls == []


def test_recount_root_is_a_regular_file_returns_503_without_saving(client, monkeypatch, tmp_path):
    """root_path が通常ファイル（symlink 置換等）に化けている場合、その状態が走査の前後で
    変わらなければ st_dev/st_ino の同一性比較だけでは検知できない（同じ実体のまま一致する）。
    pre/post どちらの `lstat` 結果にも `stat.S_ISDIR` を適用して拒否する（この場合は pre 側で
    即座に検知し、走査自体を一切行わない）。"""
    from sherpa import corpus_docs, store, worlds
    f = tmp_path / "not_a_directory"
    f.write_text("x")
    monkeypatch.setattr(store, "get_world", lambda wid: {
        **_RECOUNT_GEN_ROW, "world_id": wid, "root_path": str(f)})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: f)   # 存在確認は済ませたことにする

    def _must_not_scan(wid):
        raise AssertionError("root がディレクトリでないのに走査してはいけない")
    monkeypatch.setattr(corpus_docs, "scan_report", _must_not_scan)
    save_calls = []
    monkeypatch.setattr(store, "set_scan_report_if_unchanged",
                        lambda *a, **kw: save_calls.append(1) or True)
    r = client.post("/worlds/w1/recount")
    assert r.status_code == 503, r.text
    assert save_calls == []


# ===================================================================================
# POST /worlds/{wid}/reconvert
# ===================================================================================

def test_reconvert_unknown_world_returns_404(client, monkeypatch):
    from sherpa import store
    monkeypatch.setattr(store, "get_world", lambda wid: None)
    r = client.post("/worlds/w1/reconvert", json={"rel": "a.doc"})
    assert r.status_code == 404, r.text


def test_reconvert_unreachable_root_returns_503(client, monkeypatch):
    from sherpa import store, worlds
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: None)
    r = client.post("/worlds/w1/reconvert", json={"rel": "a.doc"})
    assert r.status_code == 503, r.text


def test_reconvert_unknown_rel_returns_404(client, monkeypatch, tmp_path):
    from sherpa import doc_ledger, store, worlds
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: tmp_path)
    monkeypatch.setattr(doc_ledger, "original_path", lambda rel, wid: None)
    r = client.post("/worlds/w1/reconvert", json={"rel": "missing.doc"})
    assert r.status_code == 404, r.text


def test_reconvert_success_drops_cache_and_runs_directly(client, monkeypatch, tmp_path):
    """`sync(force=True)` ではなく `_run_locked` を直接1回実行する（二重走査を避ける）。"""
    from sherpa import doc_ledger, store, worlds
    from sherpa.ingest import worker as ingest_worker
    from sherpa.ingest.arms import legacy_convert
    _stub_ingest_summary(monkeypatch)
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: tmp_path)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda wid: tmp_path / "derived" / "md")
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"x")
    monkeypatch.setattr(doc_ledger, "original_path", lambda rel, wid: src if rel == "旧資料.doc" else None)

    dropped = {}
    monkeypatch.setattr(legacy_convert, "drop_cache_entry",
                        lambda cache_root, rel: dropped.setdefault("rel", rel) or True)

    run_calls = []

    def _fake_run_locked(wid, *, reflect, created_by, scan_root, op="sync"):
        # `op`（PART-6・Webhook 通知の情報用途のみ）: reconvert は "refresh" を渡す
        # （`sherpa/routers/worlds.py::world_reconvert` 参照）。
        run_calls.append({"wid": wid, "reflect": reflect, "created_by": created_by,
                          "scan_root": scan_root, "op": op})
        return {"world": wid, "status": "auto_published", "ledger": 3, "flags": [],
               "nodes": 0, "edges": 0, "run": {"id": 1}}
    monkeypatch.setattr(ingest_worker, "_run_locked", _fake_run_locked)

    audits = []
    monkeypatch.setattr(store, "audit", lambda *a, **kw: audits.append((a, kw)))

    r = client.post("/worlds/w1/reconvert", json={"rel": "旧資料.doc"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["world_id"] == "w1" and body["rel"] == "旧資料.doc"
    assert body["changed"] is True and body["status"] == "auto_published"
    assert dropped["rel"] == "旧資料.doc"
    assert run_calls == [{"wid": "w1", "reflect": True, "created_by": "admin", "scan_root": None,
                          "op": "refresh"}]
    # pre/post 監査（actor・world・rel・結果）が両方記録される。
    actions = [a[1] for a, kw in audits]
    assert actions == ["world.reconvert_requested", "world.reconverted"]
    assert audits[1][1]["outcome"] == "success"
    assert audits[1][1]["detail"] == {"world": "w1", "rel": "旧資料.doc"}


def test_reconvert_run_failed_returns_503_and_audits_failure(client, monkeypatch, tmp_path):
    from sherpa import doc_ledger, store, worlds
    from sherpa.ingest import worker as ingest_worker
    from sherpa.ingest.arms import legacy_convert
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: tmp_path)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda wid: tmp_path / "derived" / "md")
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"x")
    monkeypatch.setattr(doc_ledger, "original_path", lambda rel, wid: src)
    monkeypatch.setattr(legacy_convert, "drop_cache_entry", lambda cache_root, rel: True)
    monkeypatch.setattr(ingest_worker, "_run_locked",
                        lambda wid, **kw: {"world": wid, "status": "failed", "ledger": 0,
                                          "flags": [{"reason": "graph_reflect_failed"}],
                                          "nodes": 0, "edges": 0, "run": {"id": 1}})
    audits = []
    monkeypatch.setattr(store, "audit", lambda *a, **kw: audits.append((a, kw)))

    r = client.post("/worlds/w1/reconvert", json={"rel": "旧資料.doc"})
    assert r.status_code == 503, r.text
    actions_outcomes = [(a[1], kw.get("outcome")) for a, kw in audits]
    assert actions_outcomes == [("world.reconvert_requested", "success"), ("world.reconverted", "failure")]


def test_reconvert_cache_drop_failure_returns_503_before_running(client, monkeypatch, tmp_path):
    """旧形式キャッシュの削除に失敗したら sync 前に 503 で止める
    （安定して壊れたファイルを「再変換した」ことにしない）。`_run_locked` は一切呼ばれない。"""
    from sherpa import doc_ledger, store, worlds
    from sherpa.ingest import worker as ingest_worker
    from sherpa.ingest.arms import legacy_convert
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: tmp_path)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda wid: tmp_path / "derived" / "md")
    src = tmp_path / "旧資料.doc"
    src.write_bytes(b"x")
    monkeypatch.setattr(doc_ledger, "original_path", lambda rel, wid: src)
    monkeypatch.setattr(legacy_convert, "drop_cache_entry", lambda cache_root, rel: False)

    run_calls = []
    monkeypatch.setattr(ingest_worker, "_run_locked", lambda wid, **kw: run_calls.append(wid))
    audits = []
    monkeypatch.setattr(store, "audit", lambda *a, **kw: audits.append((a, kw)))

    r = client.post("/worlds/w1/reconvert", json={"rel": "旧資料.doc"})
    assert r.status_code == 503, r.text
    assert run_calls == []
    actions_outcomes = [(a[1], kw.get("outcome")) for a, kw in audits]
    assert actions_outcomes == [("world.reconvert_requested", "success"), ("world.reconverted", "failure")]


def test_reconvert_non_legacy_ext_skips_cache_drop(client, monkeypatch, tmp_path):
    """legacy 拡張子（.doc/.xls/.ppt）でないファイルはキャッシュ削除を試みない。"""
    from sherpa import doc_ledger, store, worlds
    from sherpa.ingest import worker as ingest_worker
    from sherpa.ingest.arms import legacy_convert
    _stub_ingest_summary(monkeypatch)
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: tmp_path)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda wid: tmp_path / "derived" / "md")
    src = tmp_path / "新資料.docx"
    src.write_bytes(b"x")
    monkeypatch.setattr(doc_ledger, "original_path", lambda rel, wid: src)

    drop_calls = []
    monkeypatch.setattr(legacy_convert, "drop_cache_entry", lambda cache_root, rel: drop_calls.append(rel) or True)
    monkeypatch.setattr(ingest_worker, "_run_locked",
                        lambda wid, **kw: {"world": wid, "status": "auto_published", "ledger": 1,
                                          "flags": [], "nodes": 0, "edges": 0, "run": {"id": 1}})
    monkeypatch.setattr(store, "audit", lambda *a, **kw: None)

    r = client.post("/worlds/w1/reconvert", json={"rel": "新資料.docx"})
    assert r.status_code == 200, r.text
    assert drop_calls == []


# ===================================================================================
# GET /worlds/{wid}/status（定数時間契約）
# ===================================================================================

def test_status_unknown_world_returns_404(client, monkeypatch):
    from sherpa import store
    monkeypatch.setattr(store, "get_world_status_row", lambda wid: None)
    r = client.get("/worlds/w1/status")
    assert r.status_code == 404, r.text


def test_status_unreachable_root_returns_503(client, monkeypatch):
    """`stat(follow_symlinks=False)` 1回で到達確認する（`worlds.world_dir` は呼ばない）。"""
    from sherpa import store, worlds
    monkeypatch.setattr(store, "get_world_status_row", lambda wid: {
        "world_id": wid, "root_path": "/no/such/path", "label": None, "last_synced_at": None,
        "last_scan_report": None, "last_scan_report_at": None})

    def _must_not_call(*a, **kw):
        raise AssertionError("world_status は worlds.world_dir を呼んではいけない")
    monkeypatch.setattr(worlds, "world_dir", _must_not_call)
    r = client.get("/worlds/w1/status")
    assert r.status_code == 503, r.text


def test_status_root_is_a_regular_file_not_directory_returns_503(client, monkeypatch, tmp_path):
    """`root_path` が通常ファイル（登録後に symlink/ファイルへ置換された想定）でも、
    stat 自体は成功しうる——同じ stat 結果に `stat.S_ISDIR` を適用して拒否する（追加 I/O 無し）。"""
    from sherpa import store
    f = tmp_path / "not_a_directory"
    f.write_text("x")
    monkeypatch.setattr(store, "get_world_status_row", lambda wid: {
        "world_id": wid, "root_path": str(f), "label": None, "last_synced_at": None,
        "last_scan_report": None, "last_scan_report_at": None})
    r = client.get("/worlds/w1/status")
    assert r.status_code == 503, r.text


def test_status_db_get_world_failure_returns_503_not_zeroes(client, monkeypatch):
    """`store.get_world_status_row` 自体が失敗したら 503（全ゼロへ縮退しない）。"""
    from sherpa import store

    def _boom(wid):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "get_world_status_row", _boom)
    r = client.get("/worlds/w1/status")
    assert r.status_code == 503, r.text


def test_status_success_does_not_walk_or_query_live_graph_es(client, monkeypatch, tmp_path):
    """成功パスは `corpus_docs.scan_report`／`es_index.count` を一切呼ばない（worlds.world_dir も
    呼ばない・stat のみ）。`store.get_world`（`last_manifest` まで持つ重い SELECT）も呼ばない——
    status 専用の狭い `get_world_status_row` だけを使う。`graph_view` を呼ばない契約は
    `sherpa.routers.worlds` がこのシンボルを import しなくなったこと自体で構造的に保証される
    （ING-3・confirm/disable の応答末尾にあった graph_view 再構築は非同期化で撤去済み）。"""
    from sherpa import corpus_docs, es_index, store

    def _must_not_call(name):
        def _boom(*a, **kw):
            raise AssertionError(f"world_status は {name} を呼んではいけない")
        return _boom
    monkeypatch.setattr(corpus_docs, "scan_report", _must_not_call("corpus_docs.scan_report"))
    monkeypatch.setattr(es_index, "count", _must_not_call("es_index.count"))
    monkeypatch.setattr(store, "get_world", _must_not_call("store.get_world"))
    cached = {**corpus_docs.empty_scan_report(), "scanned": 5, "indexed": 5}
    monkeypatch.setattr(store, "get_world_status_row", lambda wid: {
        "world_id": wid, "root_path": str(tmp_path), "label": "テスト", "last_synced_at": None,
        "last_scan_report": cached, "last_scan_report_at": "2026-09-01T03:12:00+00:00"})
    monkeypatch.setattr(store, "get_latest_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: {
        "published_snapshot": {"nodes": 3, "edges": 2}})
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: {
        "extraction_snapshot": {"es": {"available": True, "error": None, "chunks": 6}}})

    r = client.get("/worlds/w1/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scanned"] == 5 and body["indexed"] == 5
    assert body["counts_as_of"] == "2026-09-01T03:12:00+00:00"
    assert body["graph_nodes"] == 3 and body["graph_edges"] == 2
    assert body["es_chunks"] == 6


def test_status_es_chunks_is_none_when_available_false_or_error_set(client, monkeypatch, tmp_path):
    """ES は `available is True` かつ `error` 無しの時だけ chunks を件数として見せる
    （delete_failed で旧索引が残ったまま「0件」を返す・bulk_errors が投入予定件数を実成功件数と
    偽って見せる、のどちらも避ける——不明な時は None＝UI「不明」表示）。"""
    from sherpa import store
    monkeypatch.setattr(store, "get_world_status_row", lambda wid: {
        "world_id": wid, "root_path": str(tmp_path), "label": None, "last_synced_at": None,
        "last_scan_report": None, "last_scan_report_at": None})
    monkeypatch.setattr(store, "get_latest_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: {
        "published_snapshot": {"nodes": 1, "edges": 1}})
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: {
        "extraction_snapshot": {"es": {"available": False, "error": None, "chunks": 6}}})
    r = client.get("/worlds/w1/status")
    assert r.status_code == 200, r.text
    assert r.json()["es_chunks"] is None

    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: {
        "extraction_snapshot": {"es": {"available": True, "error": "bulk_failed", "chunks": 6}}})
    r2 = client.get("/worlds/w1/status")
    assert r2.status_code == 200, r2.text
    assert r2.json()["es_chunks"] is None


def test_status_es_chunks_survive_a_newer_pg_replace_failed_run(client, monkeypatch, tmp_path):
    """台帳（PG）replace 失敗の run は Neo4j へは反映済み（graph 用の最新反映 run になる）でも
    ES 段には未到達——別クエリ（`get_latest_es_run_summary`）に分離したことで、その後も
    実際に ES へ触れた古い run の件数がそのまま表示され続ける（新しい run に隠されない）。"""
    from sherpa import store
    monkeypatch.setattr(store, "get_world_status_row", lambda wid: {
        "world_id": wid, "root_path": str(tmp_path), "label": None, "last_synced_at": None,
        "last_scan_report": None, "last_scan_report_at": None})
    monkeypatch.setattr(store, "get_latest_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: {
        # 新しい pg_replace 失敗 run: nodes/edges は新世代だが extraction_snapshot に es は無い。
        "published_snapshot": {"nodes": 20, "edges": 15}})
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: {
        # より古い、実際に ES まで到達した run。
        "extraction_snapshot": {"es": {"available": True, "error": None, "chunks": 34}}})
    r = client.get("/worlds/w1/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["graph_nodes"] == 20 and body["graph_edges"] == 15
    assert body["es_chunks"] == 34


def test_status_flags_are_capped_with_total_and_truncated(client, monkeypatch, tmp_path):
    """`extraction_snapshot.flags` は無制限に増えうるため、status は
    `_STATUS_FLAGS_LIMIT` 件で打ち切り、`last_run_flags_total`/`last_run_flags_truncated` で
    打切りの有無を明示する（`last_run_warnings`/`last_run_blocked` もこの打切り後の分だけ）。"""
    from sherpa.routers import worlds as worlds_router
    from sherpa import store
    monkeypatch.setattr(store, "get_world_status_row", lambda wid: {
        "world_id": wid, "root_path": str(tmp_path), "label": None, "last_synced_at": None,
        "last_scan_report": None, "last_scan_report_at": None})
    many_flags = [{"doc": None, "action": "warn", "reason": f"warn_{i}"}
                 for i in range(worlds_router._STATUS_FLAGS_LIMIT + 5)]
    monkeypatch.setattr(store, "get_latest_run_summary", lambda wid: {
        "status": "auto_published_with_flags", "extraction_snapshot": {"flags": many_flags}})
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: None)
    r = client.get("/worlds/w1/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["last_run_flags_total"] == worlds_router._STATUS_FLAGS_LIMIT + 5
    assert body["last_run_flags_truncated"] is True
    assert len(body["last_run_warnings"]) == worlds_router._STATUS_FLAGS_LIMIT
