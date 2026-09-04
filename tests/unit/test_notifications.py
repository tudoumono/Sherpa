"""NOTIFY-1: 非同期処理の完了/要対応通知（`sherpa/notifications.py`）を DB/ES/Neo4j 無しで pin する。

3種の通知源（取り込み run・LLM 成形・OCR 反映待ち）を個別に固定し、admin 限定の絞り込み・
OCR 反映待ち検知時の自動追いつき（軽量 sync を1回だけ予約）を検証する。旧・グラフ drift 通知
（`graph_stale`）は GRAPH-SRC（2026-09-04）で供給源ごと撤去済み。
"""
from __future__ import annotations

from sherpa import notifications, world_admin_service
from sherpa.ingest import background as ingest_background
from sherpa.ingest import office_md
from sherpa.store import ocr_jobs, usage_events


def _label(world_id="w1", label="資料フォルダ1"):
    return {world_id: label}


# ---- _world_labels: world_id の生露出防止（VOCAB-1）-----------------------------------------------

def test_world_labels_falls_back_to_placeholder_when_unnamed(monkeypatch):
    """ラベル未設定（空）・ID と同値（実質未設定）のいずれも、world_id を生で見せず
    固定プレースホルダに丸める（利用者向け通知に内部識別子を出さない）。"""
    monkeypatch.setattr(notifications.store, "list_worlds_db", lambda: [
        {"world_id": "w1", "label": "資料フォルダ1"},   # 正式ラベルあり→そのまま
        {"world_id": "w2", "label": ""},                # ラベル空→プレースホルダ
        {"world_id": "w3", "label": None},               # ラベル未設定（None）→プレースホルダ
        {"world_id": "w4", "label": "w4"},               # ラベル＝ID（実質未設定）→プレースホルダ
    ])
    labels = notifications._world_labels()
    assert labels["w1"] == "資料フォルダ1"
    assert labels["w2"] == notifications._UNNAMED_WORLD_LABEL
    assert labels["w3"] == notifications._UNNAMED_WORLD_LABEL
    assert labels["w4"] == notifications._UNNAMED_WORLD_LABEL
    assert "w2" not in labels["w2"] and "w4" not in labels["w4"]   # world_id が文言に生で出ない


# ---- a) 取り込み run の完了/失敗（全利用者） -----------------------------------------------------

def test_ingest_run_success_notification(monkeypatch):
    monkeypatch.setattr(notifications.store, "get_latest_run_summary",
                        lambda wid: {"status": "auto_published", "extraction_snapshot": {},
                                     "created_at": "2026-09-03T01:00:00+00:00"})
    items = notifications._ingest_run_notifications(_label())
    assert len(items) == 1
    assert items[0]["status"] == "done"
    assert items[0]["admin_only"] is False
    assert "取り込みが完了しました" in items[0]["message"]


def test_ingest_run_failed_notification(monkeypatch):
    monkeypatch.setattr(notifications.store, "get_latest_run_summary",
                        lambda wid: {"status": "failed", "extraction_snapshot": {},
                                     "created_at": "2026-09-03T01:00:00+00:00"})
    items = notifications._ingest_run_notifications(_label())
    assert items[0]["status"] == "failed"
    assert "失敗しました" in items[0]["message"]


def test_ingest_run_extracting_is_not_a_notification(monkeypatch):
    monkeypatch.setattr(notifications.store, "get_latest_run_summary",
                        lambda wid: {"status": "extracting", "extraction_snapshot": {}})
    assert notifications._ingest_run_notifications(_label()) == []


def test_ingest_run_missing_run_is_skipped(monkeypatch):
    monkeypatch.setattr(notifications.store, "get_latest_run_summary", lambda wid: None)
    assert notifications._ingest_run_notifications(_label()) == []


# ---- b) LLM 成形パス完了（admin のみ） -------------------------------------------------------------

def test_llm_render_notification_keeps_latest_per_world(monkeypatch):
    rows = [
        {"ts": "2026-09-03T02:00:00+00:00", "world": "w1", "calls": 3},
        {"ts": "2026-09-03T01:00:00+00:00", "world": "w1", "calls": 1},   # 同じ world の古い方は無視
    ]
    monkeypatch.setattr(usage_events, "list_recent_events", lambda kind, limit=200: rows)
    items = notifications._llm_render_notifications(_label())
    assert len(items) == 1
    assert items[0]["created_at"] == "2026-09-03T02:00:00+00:00"
    assert items[0]["admin_only"] is True


def test_llm_render_notification_skips_unregistered_world(monkeypatch):
    monkeypatch.setattr(usage_events, "list_recent_events",
                        lambda kind, limit=200: [{"ts": "2026-09-03T02:00:00+00:00", "world": "gone"}])
    assert notifications._llm_render_notifications(_label()) == []


# ---- c) OCR 反映待ち（admin のみ・自動追いつき） ----------------------------------------------------

def _stub_ocr_ready(monkeypatch, *, drift=True, pending=0, targets=1):
    monkeypatch.setattr(office_md, "ocr_enabled", lambda: True)
    monkeypatch.setattr(notifications.worlds, "observation_current_dir", lambda wid: "/observations/w1/gen")
    monkeypatch.setattr(notifications.worlds, "derived_md_dir", lambda wid: "/derived/w1/md")
    monkeypatch.setattr(ocr_jobs, "status_summary",
                        lambda wid: {"targets": targets, "pending": pending,
                                     "updated_at": "2026-09-03T03:00:00+00:00"})
    monkeypatch.setattr(office_md, "rag_sig_drift", lambda dmd, world=None: drift)


def test_ocr_pending_notification_triggers_catchup_once(monkeypatch):
    _stub_ocr_ready(monkeypatch)
    calls = []
    monkeypatch.setattr(ingest_background, "start_or_join",
                        lambda world_id, op, fp, create_run, work_fn: calls.append(
                            (world_id, op, fp)) or (1, False))
    items = notifications._ocr_notifications(_label())
    assert len(items) == 1
    assert items[0]["admin_only"] is True
    assert items[0]["action"]["path"] == "/worlds/w1/refresh"
    assert calls == [("w1", "refresh", "{}")]   # folder poller / 手動更新と同じ op/fingerprint に合流できる


def test_ocr_pending_skipped_when_not_drifted(monkeypatch):
    _stub_ocr_ready(monkeypatch, drift=False)
    calls = []
    monkeypatch.setattr(ingest_background, "start_or_join",
                        lambda *a, **kw: calls.append(1))
    assert notifications._ocr_notifications(_label()) == []
    assert calls == []                          # 反映済みなら sync を予約しない


def test_ocr_pending_skipped_while_jobs_running(monkeypatch):
    _stub_ocr_ready(monkeypatch, pending=2)
    assert notifications._ocr_notifications(_label()) == []


def test_ocr_pending_skipped_when_no_observation_published(monkeypatch):
    monkeypatch.setattr(office_md, "ocr_enabled", lambda: True)
    monkeypatch.setattr(notifications.worlds, "observation_current_dir", lambda wid: None)
    assert notifications._ocr_notifications(_label()) == []


def test_ocr_notifications_short_circuit_when_ocr_disabled(monkeypatch):
    monkeypatch.setattr(office_md, "ocr_enabled", lambda: False)
    called = []
    monkeypatch.setattr(notifications.worlds, "observation_current_dir",
                        lambda wid: called.append(wid) or None)
    assert notifications._ocr_notifications(_label()) == []
    assert called == []                          # 無効時は world を一切走査しない


def test_ocr_catchup_conflict_error_is_swallowed(monkeypatch):
    """反映待ちを検知しても、他の操作が進行中（ConflictError）なら静かに諦める
    （通知自体は返す・次回検知時に再試行すれば足りる）。"""
    _stub_ocr_ready(monkeypatch)

    def _raise(*a, **kw):
        raise ingest_background.ConflictError("w1", "extract", 9)
    monkeypatch.setattr(ingest_background, "start_or_join", _raise)
    items = notifications._ocr_notifications(_label())
    assert len(items) == 1                       # 例外は外へ伝播しない


def test_ocr_catchup_uses_world_admin_service_refresh(monkeypatch):
    """予約された work_fn が既存の `POST /worlds/{wid}/refresh` と同じ実処理へ委譲することを固定する。"""
    _stub_ocr_ready(monkeypatch)
    captured = {}

    def _start_or_join(world_id, op, fp, create_run, work_fn):
        captured["work_fn"] = work_fn
        return 1, False
    monkeypatch.setattr(ingest_background, "start_or_join", _start_or_join)
    refresh_calls = []
    monkeypatch.setattr(world_admin_service, "refresh",
                        lambda wid, run_id=None: refresh_calls.append((wid, run_id)))

    notifications._ocr_notifications(_label())
    captured["work_fn"](77)
    assert refresh_calls == [("w1", 77)]


# ---- 全体の組み立て（役割による絞り込み・並び順） ---------------------------------------------------

def test_list_notifications_hides_admin_only_from_non_admin(monkeypatch):
    monkeypatch.setattr(notifications, "_world_labels", lambda: _label())
    monkeypatch.setattr(notifications.store, "get_latest_run_summary",
                        lambda wid: {"status": "auto_published", "extraction_snapshot": {},
                                     "created_at": "2026-09-03T01:00:00+00:00"})
    monkeypatch.setattr(usage_events, "list_recent_events",
                        lambda kind, limit=200: [{"ts": "2026-09-03T02:00:00+00:00", "world": "w1"}])
    monkeypatch.setattr(office_md, "ocr_enabled", lambda: False)

    non_admin = notifications.list_notifications(is_admin=False)
    admin = notifications.list_notifications(is_admin=True)

    assert [it["kind"] for it in non_admin] == ["ingest_run"]
    assert {it["kind"] for it in admin} == {"ingest_run", "llm_render"}


def test_list_notifications_sorts_newest_first(monkeypatch):
    monkeypatch.setattr(notifications, "_world_labels", lambda: {"w1": "A", "w2": "B"})

    def _run(wid):
        ts = "2026-09-03T01:00:00+00:00" if wid == "w1" else "2026-09-03T05:00:00+00:00"
        return {"status": "auto_published", "extraction_snapshot": {}, "created_at": ts}
    monkeypatch.setattr(notifications.store, "get_latest_run_summary", _run)
    monkeypatch.setattr(usage_events, "list_recent_events", lambda kind, limit=200: [])
    monkeypatch.setattr(office_md, "ocr_enabled", lambda: False)

    items = notifications.list_notifications(is_admin=False)
    assert [it["world"] for it in items] == ["w2", "w1"]
