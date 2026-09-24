"""`sherpa.store.worlds` の scan_report 関連 SQL を DB 無しで固定する（ING-2）。

実 Postgres を使わず、`_connect`/`_ensure` を差し替えて発行された SQL 文字列・パラメータだけを
検証する（`tests/unit/test_ocr_jobs_store.py` と同じ流儀のフェイク connection/cursor）。
"""
from __future__ import annotations

from sherpa.store import worlds as worlds_store


class _Cursor:
    def __init__(self, *, one=None, rowcount=1):
        self.one = one
        self.rowcount = rowcount

    def fetchone(self):
        return self.one


class _Connection:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _Cursor(one={"world_id": "w1"})


def test_rebind_bind_invalidate_sig_also_nulls_scan_report(monkeypatch):
    """rebind の無効化 UPDATE は `last_scan_report`/`last_scan_report_at` も NULL にする
    （さもないと新 root の同期が終わるまで旧 root の集計が新 root の件数として表示され続ける）。"""
    conn = _Connection()
    monkeypatch.setattr(worlds_store, "_ensure", lambda: None)
    monkeypatch.setattr(worlds_store, "_connect", lambda: conn)

    worlds_store.rebind_bind_invalidate_sig("w1", "/mnt/c/new-root")

    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert "last_scan_report=NULL" in sql
    assert "last_scan_report_at=NULL" in sql
    assert "last_sig=''" in sql
    assert "last_doc_count=NULL" in sql


def test_set_world_sig_pre_invalidate_does_not_touch_scan_report_column(monkeypatch):
    """sig だけの pre-invalidate（manifest/doc_count/scan_report とも省略）は scan_report 列に触れない
    （既存の「省略した列は更新しない」契約を保つ）。"""
    conn = _Connection()
    monkeypatch.setattr(worlds_store, "_ensure", lambda: None)
    monkeypatch.setattr(worlds_store, "_connect", lambda: conn)

    worlds_store.set_world_sig("w1", "")

    sql, params = conn.calls[0]
    assert "last_scan_report" not in sql
    assert "last_manifest" not in sql
    assert "last_doc_count" not in sql
    assert params[0] == ""                # sig


def test_set_world_sig_success_confirm_folds_scan_report_into_same_update(monkeypatch):
    """成功確定は sig・doc_count・scan_report を1本の UPDATE へまとめる
    （同一トランザクションで一斉に確定する・scan_report だけの別呼び出しは無い）。"""
    conn = _Connection()
    monkeypatch.setattr(worlds_store, "_ensure", lambda: None)
    monkeypatch.setattr(worlds_store, "_connect", lambda: conn)

    report = {"scanned": 3, "indexed": 3}
    worlds_store.set_world_sig("w1", "sig123", manifest={"a": [1, 2, 3]}, doc_count=3, scan_report=report)

    assert len(conn.calls) == 1                            # 1回の UPDATE だけ
    sql, params = conn.calls[0]
    assert "last_sig=%s" in sql
    assert "last_manifest=%s" in sql
    assert "last_doc_count=%s" in sql
    assert "last_scan_report=%s" in sql
    assert "last_scan_report_at=now()" in sql
    assert params[0] == "sig123"


def test_set_world_sig_scan_report_none_leaves_column_untouched(monkeypatch):
    """`scan_report=None`（既定・計算失敗時のフォールバックを含む）なら該当列を UPDATE 文に含めない
    （前回値を意図せず消さない・best-effort の設計どおり）。"""
    conn = _Connection()
    monkeypatch.setattr(worlds_store, "_ensure", lambda: None)
    monkeypatch.setattr(worlds_store, "_connect", lambda: conn)

    worlds_store.set_world_sig("w1", "sig123", manifest={"a": [1]}, doc_count=1)

    sql, params = conn.calls[0]
    assert "last_scan_report" not in sql


def test_get_world_status_row_select_excludes_last_manifest(monkeypatch):
    """status 専用の狭い SELECT は `last_manifest`（world 全ファイル分の JSONB）を
    含まない（`get_world()` の一般用途 SELECT と違い、status はこの列を一切使わない）。"""
    conn = _Connection()
    monkeypatch.setattr(worlds_store, "_ensure", lambda connect_timeout=None: None)
    monkeypatch.setattr(worlds_store, "_connect", lambda **kw: conn)

    worlds_store.get_world_status_row("w1")

    sql, params = conn.calls[0]
    assert "last_manifest" not in sql
    assert "root_path" in sql and "last_scan_report" in sql and "last_scan_report_at" in sql
    assert "last_sig" in sql   # GRA-1: graph_view キャッシュのプローブにも同じ狭い行を使う


def test_set_scan_report_if_unchanged_guards_on_root_and_sig(monkeypatch):
    """recount の書き戻しは binding（root_path）と世代（last_sig 等）が一致する場合だけ
    UPDATE する（`backfill_doc_count` 等と同じ TOCTOU ガードの型）。`last_sig`/タイムスタンプ列は
    NULL のことがあるため `IS NOT DISTINCT FROM` で比較する（素の `=` は NULL 同士を偽と評価
    してしまう）。"""
    conn = _Connection()
    monkeypatch.setattr(worlds_store, "_ensure", lambda: None)
    monkeypatch.setattr(worlds_store, "_connect", lambda: conn)

    worlds_store.set_scan_report_if_unchanged(
        "w1", {"scanned": 1}, expected_root_path="/mnt/c/root", expected_sig="sig1",
        expected_created_at="2026-08-01T00:00:00+00:00", expected_updated_at="2026-08-01T00:00:00+00:00",
        expected_last_synced_at="2026-08-01T00:00:00+00:00", expected_last_scan_report_at=None)

    sql, params = conn.calls[0]
    assert "WHERE" in sql and "root_path=%s" in sql
    assert "last_sig IS NOT DISTINCT FROM %s" in sql
    assert "created_at IS NOT DISTINCT FROM %s" in sql
    assert "updated_at IS NOT DISTINCT FROM %s" in sql
    assert "last_synced_at IS NOT DISTINCT FROM %s" in sql
    assert "last_scan_report_at IS NOT DISTINCT FROM %s" in sql
    assert tuple(params[-6:]) == ("/mnt/c/root", "sig1", "2026-08-01T00:00:00+00:00",
                                  "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00", None)


def test_get_latest_es_run_summary_select_requires_es_key_and_published(monkeypatch):
    """ES 専用の最新反映クエリは `published_at IS NOT NULL` と `extraction_snapshot ? 'es'` の
    両方を条件にする——台帳 replace 失敗で published_snapshot だけ持つ run（ES 未到達）を
    最新反映扱いにしない。"""
    from sherpa.store import ingest as ingest_store
    conn = _Connection()
    monkeypatch.setattr(ingest_store, "_ensure", lambda: None)
    monkeypatch.setattr(ingest_store, "_connect", lambda: conn)

    ingest_store.get_latest_es_run_summary("w1")

    sql, params = conn.calls[0]
    assert "published_at IS NOT NULL" in sql
    assert "extraction_snapshot ? 'es'" in sql
    assert "ORDER BY id DESC LIMIT 1" in sql
