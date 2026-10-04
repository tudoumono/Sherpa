"""利用統計 API（admin 専用）の契約テスト（要 Postgres）。集計値・本文を返さない・期間境界（JST）・母集団の揃え。
共有 DB の既存行と混ざらないよう、挿入前後の差分か専用の unique 名で確かめる。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app

from _common import _login, _sfx, _try_init

JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture(autouse=True)
def _db_up():
    _try_init()


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@usage.local", display_name=f"表示名-{uid}",
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def _mk(tag: str, role: str = "user") -> str:
    uid = f"{tag}{_sfx()}"
    _mk_user(uid, f"Pw{uid}", role=role)
    return uid


def _admin() -> TestClient:
    uid = _mk("usgadm", "admin")
    return _login(uid, f"Pw{uid}")


def _stats(admin: TestClient, days: int = 30) -> dict:
    r = admin.get(f"/admin/usage/stats?days={days}")
    assert r.status_code == 200, r.text
    return r.json()


def _urow(data: dict, uid: str) -> dict:
    return next(u for u in data["users"] if u["uid"] == uid)


def _sql(sql: str, *params) -> None:
    with psycopg.connect(store._dsn()) as c:
        c.execute(sql, params)


def _set_created(msg_id: int, ts) -> None:
    _sql("UPDATE messages SET created_at=%s WHERE id=%s", ts, msg_id)


def _turn(cid: int, user_text: str, *, lens: str | None, personal: bool = False):
    store.add_message(cid, "user", user_text, personal=personal)
    store.add_message(cid, "assistant", f"({lens})への回答", lens=lens, answer={}, personal=personal)


def _turn_with_answer(cid: int, user_text: str, *, lens: str, answer: dict) -> None:
    store.add_message(cid, "user", user_text)
    store.add_message(cid, "assistant", f"({lens})への回答", lens=lens, answer=answer)


def _turn_with_sources(cid, user_text: str, *, lens: str, sources):
    _turn_with_answer(cid, user_text, lens=lens, answer={"sources": sources})


def _turn_with_stop_kind(cid, user_text: str, *, lens: str, stop_kind: str | None):
    """stop_kind=None は列自体を持たない旧データ/未計測経路（集計側で 'unknown' に畳まれる）。"""
    _turn_with_answer(cid, user_text, lens=lens, answer={"stop_kind": stop_kind} if stop_kind is not None else {})


def _turn_with_limits(cid, user_text: str, *, lens: str, provider: str | None, limits: dict | None):
    """limits=None は旧行（キー自体が無い）。"""
    answer = {**({"usage": {"provider": provider}} if provider is not None else {}),
              **({"limits": limits} if limits is not None else {})}
    _turn_with_answer(cid, user_text, lens=lens, answer=answer)


def _delete_rows(table: str, column: str, values: list) -> None:
    try:
        with psycopg.connect(store._dsn()) as c:
            c.execute(f"DELETE FROM {table} WHERE {column} = ANY(%s)", (values,))
    except Exception:
        pass


def _delete_usage_events_by_model(models: list) -> None:
    _delete_rows("usage_events", "model", models)


def _delete_quality_runs(run_ids: list) -> None:
    _delete_rows("depth_quality_runs", "run_id", run_ids)


def _usage_event(kind: str, model: str, uid: str, world: str, *, input_tokens: int, output_tokens: int,
                 calls: int = 1, cid: int | None = None, cached: int = 0) -> None:
    store.add_usage_event(kind=kind, provider="openai", model=model, input_tokens=input_tokens,
                          cached_input_tokens=cached, output_tokens=output_tokens,
                          reasoning_output_tokens=0, calls=calls, user_id=uid, world=world,
                          conversation_id=cid)


def test_usage_stats_requires_admin_and_login():
    uid = _mk("usgusr")
    assert TestClient(app, raise_server_exceptions=False).get("/admin/usage/stats").status_code == 401
    assert _login(uid, f"Pw{uid}").get("/admin/usage/stats").status_code == 403


def test_usage_stats_works_in_compat_mode(auth_disabled):
    r = TestClient(app, raise_server_exceptions=False).get("/admin/usage/stats")
    assert r.status_code == 200, r.text
    assert {"users", "totals", "daily"} <= r.json().keys()


def test_usage_stats_aggregates_seeded_conversations_and_audit():
    """ユーザー別の turns/conversations/lens/personal/worlds/監査由来・world 別ターン数・ターン数降順・本文タイトルを
    返さない・閲覧の監査。sanitized_snapshot（内部コピー）は turns/conversations を水増ししない。"""
    admin_uid = _mk("usgadm", "admin")
    heavy_uid, light_uid = _mk("usgheavy"), _mk("usglight")
    sfx = _sfx()
    world, world_b = f"statsworld{sfx}", f"statsworldb{sfx}"
    secret_marker = f"極秘の会話本文マーカー-{sfx}"

    c1 = store.create_conversation(user_id=heavy_uid, world=world, title=f"タイトルは非公開-{sfx}")
    _turn(c1["id"], secret_marker + "-1", lens="impact")
    _turn(c1["id"], secret_marker + "-2", lens="qa", personal=True)
    c2 = store.create_conversation(user_id=heavy_uid, world=world)
    _turn(c2["id"], secret_marker + "-3", lens="troubleshoot")
    c3 = store.create_conversation(user_id=light_uid, world=world_b)
    _turn(c3["id"], secret_marker + "-4", lens="chat")

    store.audit(heavy_uid, "auth.login", "user", f"user:{heavy_uid}")
    store.audit(heavy_uid, "auth.login", "user", f"user:{heavy_uid}")
    store.audit(heavy_uid, "document.downloaded", "document", "doc:1")

    admin = _login(admin_uid, f"Pw{admin_uid}")
    r = admin.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text
    data = r.json()
    assert secret_marker not in r.text
    assert f"タイトルは非公開-{sfx}" not in r.text

    heavy, light = _urow(data, heavy_uid), _urow(data, light_uid)
    assert heavy["turns"] == 3
    assert heavy["conversations"] == 2
    assert heavy["lens"] == {"impact": 1, "qa": 1, "troubleshoot": 1, "chat": 0}
    assert heavy["personal_turns"] == 1
    assert heavy["worlds"] == [world]
    assert heavy["logins"] == 2
    assert heavy["downloads"] == 1
    assert heavy["uploads"] == 0
    assert heavy["shares"] == 0
    assert heavy["display_name"] == f"表示名-{heavy_uid}"
    assert light["turns"] == 1
    assert light["conversations"] == 1
    assert light["lens"] == {"impact": 0, "qa": 0, "troubleshoot": 0, "chat": 1}
    assert light["personal_turns"] == 0
    assert light["worlds"] == [world_b]
    assert light["logins"] == 0
    worlds_map = {w["world"]: w["turns"] for w in data["worlds"]}
    assert worlds_map.get(world) == 3 and worlds_map.get(world_b) == 1

    uids = [u["uid"] for u in data["users"]]
    assert uids.index(heavy_uid) < uids.index(light_uid)   # ターン数降順

    # 共有 DB の残留データがあり得るため >=
    assert data["totals"]["turns"] >= 4
    assert data["totals"]["active_users"] >= 2
    assert data["totals"]["conversations"] >= 3

    rows = store.list_audit(actor=admin_uid, action="admin.usage_viewed", limit=5)
    assert rows, "admin.usage_viewed was not recorded"
    assert rows[0]["detail"]["days"] == 30

    assert store.create_sanitized_snapshot(heavy_uid, c1["id"]) is not None
    after = _urow(_stats(admin), heavy_uid)
    assert after["turns"] == 3, "snapshot 作成で turns が水増しされた"
    assert after["conversations"] == 2, "snapshot 作成で conversations が水増しされた"


def test_usage_stats_assistant_only_rows_do_not_count_as_activity():
    """active_days・users 一覧・conversations・last_active・turns・lens は user メッセージ基準
    （assistant 単独行・2件目以降の assistant 行で水増しされない）。"""
    admin = _admin()
    sfx = _sfx()
    uid_days, uid_aonly, uid_cla, uid_stray = (_mk("usgadonly"), _mk("usgaonly"), _mk("usgcla"),
                                                _mk("usgstray"))

    conv = store.create_conversation(user_id=uid_days, world=f"adworld{sfx}")
    _turn(conv["id"], "本日のターン", lens="chat")
    solo = store.add_message(conv["id"], "assistant", "システム通知的な単独発言", lens="chat")
    _sql("UPDATE messages SET created_at = now() - interval '1 day' WHERE id=%s", solo["id"])

    conv = store.create_conversation(user_id=uid_aonly, world=f"aonlyworld{sfx}")
    store.add_message(conv["id"], "assistant", "ユーザー発言のない単独発言", lens="chat")

    conv1 = store.create_conversation(user_id=uid_cla, world=f"claworld{sfx}")
    user_msg = store.add_message(conv1["id"], "user", "質問")
    store.add_message(conv1["id"], "assistant", "回答", lens="chat")
    conv2 = store.create_conversation(user_id=uid_cla, world=f"claworld{sfx}")
    later_msg = store.add_message(conv2["id"], "assistant", "後から来た単独発言", lens="chat")
    _sql("UPDATE messages SET created_at = now() + interval '1 hour' WHERE id=%s", later_msg["id"])

    conv = store.create_conversation(user_id=uid_stray, world=f"strayworld{sfx}")
    store.add_message(conv["id"], "user", "1つの質問")
    store.add_message(conv["id"], "assistant", "最初の返答", lens="impact", answer={})
    store.add_message(conv["id"], "assistant", "対応する質問のない単独発言", lens="qa")
    store.add_message(conv["id"], "assistant", "さらにもう1件", lens="troubleshoot")

    data = _stats(admin)
    assert _urow(data, uid_days)["active_days"] == 1, "assistant 単独の日が active_days に混入した"
    assert uid_aonly not in [u["uid"] for u in data["users"]], "assistant のみのユーザーが users に出た"

    row = _urow(data, uid_cla)
    assert row["conversations"] == 1, f"assistant 単独の conv2 が数えられた: {row['conversations']}"
    assert row["last_active"] is not None
    assert str(row["last_active"])[:19] == user_msg["created_at"].isoformat()[:19], \
        "last_active が assistant 単独発言の未来時刻に引きずられた"

    row = _urow(data, uid_stray)
    assert row["turns"] == 1
    assert row["lens"] == {"impact": 1, "qa": 0, "troubleshoot": 0, "chat": 0}, row["lens"]


def test_usage_stats_daily_buckets_by_jst_not_utc():
    """daily の日付境界は JST（DB の timezone に依存しない）。挿入前後の差分で見る。"""
    admin, uid = _admin(), _mk("usgjst")
    # 昨日 16:30 UTC＝JST では常に翌日（日本は DST なし）
    target_utc = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=16, minute=30, second=0, microsecond=0)
    utc_date, jst_date = target_utc.date().isoformat(), target_utc.astimezone(JST).date().isoformat()
    assert utc_date != jst_date

    def _daily():
        return {d["date"]: d["turns"] for d in _stats(admin, 5)["daily"]}

    before = _daily()
    conv = store.create_conversation(user_id=uid, world=f"jstworld{_sfx()}")
    _set_created(store.add_message(conv["id"], "user", "JST境界テスト")["id"], target_utc)
    after = _daily()

    assert after.get(jst_date, 0) - before.get(jst_date, 0) == 1
    assert after.get(utc_date, 0) - before.get(utc_date, 0) == 0, "UTC 日付に混入（JST 基準でない）"


def test_usage_stats_daily_includes_active_users_per_day():
    """daily.active_users＝その日に user メッセージを発したユニーク uid 数（JST・origin='own'）。"""
    admin, u1, u2 = _admin(), _mk("usgau1"), _mk("usgau2")

    def _daily():
        return {d["date"]: d["active_users"] for d in _stats(admin, 5)["daily"]}

    before = _daily()
    sfx = _sfx()
    conv1 = store.create_conversation(user_id=u1, world=f"auworld{sfx}")
    _turn(conv1["id"], "u1のターン1", lens="chat")
    _turn(conv1["id"], "u1のターン2", lens="qa")   # 同じ日・同じユーザーは1人
    conv2 = store.create_conversation(user_id=u2, world=f"auworld{sfx}")
    _turn(conv2["id"], "u2のターン1", lens="impact")
    after = _daily()

    today_jst = (datetime.now(timezone.utc) + timedelta(hours=9)).date().isoformat()
    assert after.get(today_jst, 0) - before.get(today_jst, 0) == 2


def test_usage_stats_daily_sum_matches_users_and_totals_sum():
    """期間境界ぎりぎり（直前=期間外／ちょうど=期間内）で daily 合計・users 合計・totals が一致する。"""
    admin, uid = _admin(), _mk("usgbound")
    days = 7
    period = _stats(admin, days)["period"]
    assert period["days"] == days
    assert period["start"] <= period["end"]

    conv = store.create_conversation(user_id=uid, world=f"boundworld{_sfx()}")
    in_msg = store.add_message(conv["id"], "user", "境界ちょうど（期間内のはず）")
    store.add_message(conv["id"], "assistant", "回答", lens="chat")
    out_msg = store.add_message(conv["id"], "user", "境界の直前（期間外のはず）")
    store.add_message(conv["id"], "assistant", "回答2", lens="chat")
    _sql("UPDATE messages SET created_at = (%s || ' 00:00:00+09:00')::timestamptz WHERE id=%s",
         period["start"], in_msg["id"])
    _sql("UPDATE messages SET created_at = (%s || ' 00:00:00+09:00')::timestamptz - interval '1 second' "
         "WHERE id=%s", period["start"], out_msg["id"])

    data = _stats(admin, days)
    row = next((u for u in data["users"] if u["uid"] == uid), None)
    assert row is not None, "period.start ちょうどのメッセージが期間内に含まれていない"
    assert row["turns"] == 1, f"境界直前のメッセージが期間内に混入した: turns={row['turns']}"

    sum_daily = sum(d["turns"] for d in data["daily"])
    sum_users = sum(u["turns"] for u in data["users"])
    assert sum_daily == sum_users == data["totals"]["turns"], (sum_daily, sum_users, data["totals"]["turns"])
    assert period["start"] in [d["date"] for d in data["daily"]], "最古日が daily から欠落している"


def test_usage_stats_zero_hit_rate():
    """ゼロヒット＝ナレッジ参照ターン（lens != 'chat'）のうち answer.sources が空。chat は対象外・
    ナレッジ参照が無ければ rate=None。answer 欠落/sources が JSON null/非配列でも 500 にならずゼロヒット。"""
    admin = _admin()
    uid, uid_none, uid_edge = _mk("usgzero"), _mk("usgznone"), _mk("usgzedge")
    sfx = _sfx()

    conv = store.create_conversation(user_id=uid, world=f"zeroworld{sfx}")
    _turn_with_sources(conv["id"], "q1", lens="impact", sources=[])
    _turn_with_sources(conv["id"], "q2", lens="qa", sources=[{"doc_id": "a.md", "span": [1, 2]}])
    _turn_with_sources(conv["id"], "q3", lens="troubleshoot", sources=[])
    _turn(conv["id"], "q4", lens="chat")
    conv = store.create_conversation(user_id=uid_none, world=f"znoneworld{sfx}")
    _turn(conv["id"], "chat only", lens="chat")
    conv = store.create_conversation(user_id=uid_edge, world=f"zedgeworld{sfx}")
    _turn(conv["id"], "answer 自体が無い", lens="impact")
    _turn_with_sources(conv["id"], "sourcesがJSON null", lens="qa", sources=None)
    _turn_with_sources(conv["id"], "sourcesが非配列", lens="troubleshoot", sources="not-an-array")

    data = _stats(admin)
    row = _urow(data, uid)
    assert row["knowledge_turns"] == 3, "lens='chat' が knowledge_turns に混入した"
    assert row["zero_hit_turns"] == 2
    assert row["zero_hit_rate"] == pytest.approx(2 / 3)
    assert data["zero_hit"]["knowledge_turns"] >= 3
    assert data["zero_hit"]["zero_hit_turns"] >= 2
    assert data["zero_hit"]["rate"] is not None

    row = _urow(data, uid_none)
    assert row["knowledge_turns"] == 0
    assert row["zero_hit_turns"] == 0
    assert row["zero_hit_rate"] is None

    row = _urow(data, uid_edge)
    assert row["knowledge_turns"] == 3
    assert row["zero_hit_turns"] == 3, "answer欠落/JSON null/非配列のいずれかがゼロヒット判定から漏れた"


def _utc_18h_two_days_ago():
    return (datetime.now(timezone.utc) - timedelta(days=2)).replace(hour=18, minute=0, second=0, microsecond=0)


def _jst_midnight_today():
    d = datetime.now(timezone.utc).astimezone(JST).date()
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=JST)


@pytest.mark.parametrize("make_target, absent_prev_day_23", [
    (_utc_18h_two_days_ago, False),
    (_jst_midnight_today, True),   # JST 00:00:00 ちょうどは hour=0 に入り、前日23時台には混入しない
])
def test_usage_stats_heatmap_buckets_by_jst_weekday_and_hour(make_target, absent_prev_day_23):
    """ヒートマップは JST 曜日(Postgres DOW: 0=日〜6=土)×時間帯の user メッセージ数（挿入前後の差分）。"""
    admin, uid = _admin(), _mk("usghm")
    target = make_target()
    target_jst = target.astimezone(JST)
    weekday_pg = (target_jst.weekday() + 1) % 7   # Python 月=0..日=6 → Postgres 日=0..土=6
    key = (weekday_pg, target_jst.hour)
    prev_key = ((weekday_pg - 1) % 7, 23)

    def _heatmap():
        return {(h["weekday"], h["hour"]): h["count"] for h in _stats(admin)["heatmap"]}

    before = _heatmap()
    conv = store.create_conversation(user_id=uid, world=f"hmworld{_sfx()}")
    _set_created(store.add_message(conv["id"], "user", "heatmap test")["id"], target)
    after = _heatmap()

    assert after.get(key, 0) - before.get(key, 0) == 1, f"セル {key} の delta が +1 でない"
    if absent_prev_day_23:
        assert after.get(prev_key, 0) - before.get(prev_key, 0) == 0, "前日23時台に混入した"


def test_usage_stats_providers_usage_from_chat_turn_audit():
    """監査 chat.turn の detail.provider を期間集計（stopped も母数）。allowlist 外・キー欠落は
    複数あっても 1 つの 'unknown' 行に畳み込む。"""
    admin, uid = _admin(), _mk("usgprov")
    sfx = _sfx()

    def _rows():
        return _stats(admin)["providers"]

    def _turns(rows, name):
        return next((p["turns"] for p in rows if p["provider"] == name), 0)

    before = _rows()
    for stopped in (False, True, False):
        store.audit(uid, "chat.turn", "conversation", "conv:1",
                    detail={"provider": "openai", "stopped": stopped}, outcome="success")
    store.audit(uid, "chat.turn", "conversation", "conv:1", detail={"provider": f"bogus-a-{sfx}"}, outcome="success")
    store.audit(uid, "chat.turn", "conversation", "conv:1", detail={"provider": f"bogus-b-{sfx}"}, outcome="success")
    store.audit(uid, "chat.turn", "conversation", "conv:1", detail={}, outcome="success")   # provider キー無し
    after = _rows()

    assert _turns(after, "openai") - _turns(before, "openai") == 3, "stopped が母数から漏れた、または集計誤り"
    assert len([p for p in after if p["provider"] == "unknown"]) == 1, "unknown が複数行に分かれた"
    assert _turns(after, "unknown") - _turns(before, "unknown") == 3


def test_usage_stats_stop_kinds_distribution_and_unknown_fallback():
    """messages.answer.stop_kind の分布。NULL（未計測）と allowlist 外の文字列は 'unknown' に畳む。"""
    admin, uid = _admin(), _mk("usgsk")

    def _stop_kinds():
        return {row["stop_kind"]: row["turns"] for row in _stats(admin)["stop_kinds"]}

    before = _stop_kinds()
    conv = store.create_conversation(user_id=uid, world=f"statsworldsk{_sfx()}")
    _turn_with_stop_kind(conv["id"], "q1", lens="qa", stop_kind="completed")
    _turn_with_stop_kind(conv["id"], "q2", lens="qa", stop_kind="budget")
    _turn_with_stop_kind(conv["id"], "q3", lens="qa", stop_kind="timeout")
    _turn_with_stop_kind(conv["id"], "q4", lens="qa", stop_kind=None)
    store.add_message(conv["id"], "user", "q5")
    reply = store.add_message(conv["id"], "assistant", "(qa)への回答", lens="qa", answer={"stop_kind": "completed"})
    _sql("UPDATE turn_metrics SET stop_kind='not_a_real_stop_kind' WHERE message_id=%s", reply["id"])
    after = _stop_kinds()

    assert after.get("completed", 0) - before.get("completed", 0) == 1
    assert after.get("budget", 0) - before.get("budget", 0) == 1
    assert after.get("timeout", 0) - before.get("timeout", 0) == 1
    assert after.get("unknown", 0) - before.get("unknown", 0) == 2   # NULL と語彙外
    assert after.get("not_a_real_stop_kind", 0) - before.get("not_a_real_stop_kind", 0) == 0, \
        "allowlist 外の stop_kind がそのまま独立した分類として出た"


def test_usage_stats_limits_aggregates_by_provider_and_ignores_legacy_rows_without_key():
    """answer.limits を provider 別に集計。キー無しの旧行は分母（turns）にだけ数え、各項目は0のまま。"""
    admin, uid = _admin(), _mk("usglm")

    def _limits():
        return {row["provider"]: row for row in _stats(admin)["limits"]["by_provider"]}

    before = _limits().get("codex", {})
    conv = store.create_conversation(user_id=uid, world=f"statsworldlm{_sfx()}", title="limits")
    _turn_with_limits(conv["id"], "q1", lens="qa", provider="codex", limits={
        "tool_result_clipped": 2, "total_budget_hit": False, "context_compactions": 1,
        "synthesis_truncated": False, "search_truncated": 3, "auto_continues": 1})
    _turn_with_limits(conv["id"], "q2", lens="qa", provider="codex", limits={
        "tool_result_clipped": 0, "total_budget_hit": True, "context_compactions": 0,
        "synthesis_truncated": True, "search_truncated": 0, "auto_continues": 0,
        "depth_escalated": True})
    _turn_with_limits(conv["id"], "q3", lens="qa", provider="codex", limits=None)
    _turn_with_limits(conv["id"], "q4", lens="impact", provider="codex", limits={
        "backend_unavailable_fulltext": True, "backend_unavailable_graph": True,
        "graph_reingest_required": True})
    after = _limits()["codex"]

    def _delta(key):
        return (after.get(key) or 0) - (before.get(key) or 0)

    assert _delta("turns") == 4                         # 旧行も母数には入る
    assert _delta("tool_result_clipped_turns") == 1
    assert _delta("tool_result_clipped_total") == 2
    assert _delta("total_budget_hit_turns") == 1
    assert after["context_compactions_turns"] is None
    assert after["context_compactions_total"] is None
    assert after["synthesis_truncated_turns"] is None
    assert after["depth_escalated_turns"] is None
    assert _delta("search_truncated_turns") == 1
    assert _delta("search_truncated_total") == 3
    assert _delta("auto_continues_turns") == 1
    assert _delta("auto_continues_total") == 1
    assert _delta("backend_unavailable_fulltext_turns") == 1
    assert _delta("backend_unavailable_graph_turns") == 1
    assert _delta("graph_reingest_required_turns") == 1


def test_usage_stats_stopped_turns():
    """stopped_turns＝chat.turn 監査の detail.stopped=true（実在する会話のみ・stop_kinds には現れない別集計）。
    会話を削除すると外れる（stop_kinds/turns と同じ deleted_at IS NULL AND origin='own' の母集団）。"""
    admin, uid = _admin(), _mk("usgst")

    def _stopped():
        return _stats(admin)["stopped_turns"]

    def _stop_kinds_total():
        return sum(row["turns"] for row in _stats(admin)["stop_kinds"])

    before, before_total = _stopped(), _stop_kinds_total()
    conv = store.create_conversation(user_id=uid, world=f"stworld{_sfx()}")
    store.add_message(conv["id"], "user", "止めて")   # assistant は保存しない（stopped 分岐の実形）
    store.audit(uid, "chat.turn", "conversation", f"conv:{conv['id']}", detail={"stopped": True}, outcome="success")
    store.audit(uid, "chat.turn", "conversation", f"conv:{conv['id']}", detail={"stopped": False}, outcome="success")

    assert _stopped() - before == 1, "実在する会話の停止ターンが数えられていない"
    assert _stop_kinds_total() - before_total == 0, "停止ターンが stop_kinds の分布に混入した"
    assert store.delete_conversation(conv["id"], user_id=uid)
    assert _stopped() - before == 0, "削除済み会話の停止ターンが数え続けられている"

    # 期間は監査行の created_at でなく detail.message_id_user の user 発言時刻で絞る（期間開始の直前に始まった
    # ターンの監査書込みが開始直後にずれ込んでも期間内に数えない）。
    start_ts = store._usage_period_bounds(30)[0]
    conv = store.create_conversation(user_id=uid, world=f"stbworld{_sfx()}")
    msg = store.add_message(conv["id"], "user", "止めて（期間開始の直前に始まったターン）")
    _set_created(msg["id"], start_ts - timedelta(seconds=1))
    store.audit(uid, "chat.turn", "conversation", f"conv:{conv['id']}",
                detail={"stopped": True, "message_id_user": msg["id"]}, outcome="success")
    _sql("UPDATE audit_log SET created_at = %s WHERE id = ("
         "  SELECT id FROM audit_log WHERE actor_user_id=%s AND action='chat.turn' "
         "  ORDER BY id DESC LIMIT 1)", start_ts + timedelta(seconds=1), uid)
    assert _stopped() - before == 0, "監査の書込み時刻だけで期間内の停止に数えられた"


def test_usage_stats_turns_and_stop_kinds_agree_across_period_boundary():
    """user 行が期間内・assistant 行が期間外のターンでも turns と stop_kinds は同じ側（期間内）に計上される。"""
    admin, uid = _admin(), _mk("usgxb")
    days = 7
    period = _stats(admin, days)["period"]

    def _uid_turns():
        row = next((u for u in _stats(admin, days)["users"] if u["uid"] == uid), None)
        return row["turns"] if row else 0

    def _completed():
        return {r["stop_kind"]: r["turns"] for r in _stats(admin, days)["stop_kinds"]}.get("completed", 0)

    before_turns, before_completed = _uid_turns(), _completed()
    conv = store.create_conversation(user_id=uid, world=f"xboundworld{_sfx()}")
    user_msg = store.add_message(conv["id"], "user", "境界を跨ぐターン")
    assistant_msg = store.add_message(conv["id"], "assistant", "回答", lens="qa", answer={"stop_kind": "completed"})
    # user＝period.end の JST 23:59（期間内）／assistant＝期間の排他的上限のさらに1時間後（期間外）
    _sql("UPDATE messages SET created_at = (%s || ' 23:59:00+09:00')::timestamptz WHERE id=%s",
         period["end"], user_msg["id"])
    _sql("UPDATE messages SET created_at = (%s || ' 00:00:00+09:00')::timestamptz "
         "+ interval '1 day 1 hour' WHERE id=%s", period["end"], assistant_msg["id"])

    assert _uid_turns() - before_turns == 1, "user 行が期間内なのに turns に数えられていない"
    assert _completed() - before_completed == 1, "assistant 行が期間外のため stop_kinds から漏れた（turns と食い違い）"


def test_usage_stats_downloads_total_and_daily_and_future_rows_excluded():
    """原本DL数＝document.downloaded の期間合計＋日別。created_at が明日以降の行は period 上限で除外（daily・downloads）。"""
    admin, uid = _admin(), _mk("usgdl")
    before = _stats(admin)
    before_daily = sum(x["turns"] for x in before["daily"])
    store.audit(uid, "document.downloaded", "document", "doc:1", outcome="success")
    store.audit(uid, "document.downloaded", "document", "doc:2", outcome="success")

    dl = _stats(admin)["downloads"]
    assert dl["total"] - before["downloads"]["total"] == 2
    assert sum(d["count"] for d in dl["daily"]) == dl["total"], "downloads.daily の合計が total と一致しない"

    conv = store.create_conversation(user_id=uid, world=f"futworld{_sfx()}")
    msg = store.add_message(conv["id"], "user", "未来の投稿")
    store.audit(uid, "document.downloaded", "document", "doc:3", outcome="success")
    _sql("UPDATE messages SET created_at = now() + interval '3 days' WHERE id=%s", msg["id"])
    _sql("UPDATE audit_log SET created_at = now() + interval '3 days' WHERE id = ("
         "  SELECT id FROM audit_log WHERE actor_user_id=%s AND action='document.downloaded' "
         "  ORDER BY id DESC LIMIT 1)", uid)

    after = _stats(admin)
    assert sum(x["turns"] for x in after["daily"]) == before_daily, "未来時刻のメッセージが daily に混入した"
    assert after["downloads"]["total"] == dl["total"], "未来時刻の監査行が downloads に混入した"


def test_usage_stats_retention_shape_and_period_narrow_window_partial_week():
    """retention.weekly / revisit_rate が存在する。days=1（今日だけ）の weekly は期間内の日だけを反映
    （昨日の活動を拾わない）。"""
    admin, today_uid, yest_uid = _admin(), _mk("usgpwtoday"), _mk("usgpwyest")
    retention = _stats(admin)["retention"]
    assert "weekly" in retention and "revisit_rate" in retention
    assert isinstance(retention["weekly"], list)
    for w in retention["weekly"]:
        assert {"week_start", "active_users"} <= w.keys()

    today_jst = (datetime.now(timezone.utc) + timedelta(hours=9)).date()

    def _week_active_users():
        for w in _stats(admin, 1)["retention"]["weekly"]:
            ws = datetime.fromisoformat(w["week_start"]).date()
            if ws <= today_jst <= ws + timedelta(days=6):
                return w["active_users"]
        return 0

    before = _week_active_users()
    sfx = _sfx()
    conv_today = store.create_conversation(user_id=today_uid, world=f"pwworld{sfx}")
    store.add_message(conv_today["id"], "user", "今日の発言")
    conv_yest = store.create_conversation(user_id=yest_uid, world=f"pwworld{sfx}")
    msg_yest = store.add_message(conv_yest["id"], "user", "昨日の発言")
    _sql("UPDATE messages SET created_at = now() - interval '1 day' WHERE id=%s", msg_yest["id"])

    assert _week_active_users() - before == 1, "昨日分の活動が同じ週の active_users に混入した"


# 会話単位フィルタ導入前の `numbered` 定義を凍結したコピー（期間フィルタなしの挙動オラクル）。
# 実装（`store._USAGE_TURN_CTE`）とは独立に維持し、本番コードの変更に追従して書き換えない。
_FROZEN_ORACLE_TURN_CTE = (
    "WITH numbered AS ("
    "  SELECT m.id, m.conversation_id, m.role, m.lens, m.personal, m.answer, m.created_at, "
    "    c.user_id, c.version, "
    "    SUM(CASE WHEN m.role='user' THEN 1 ELSE 0 END) "
    "      OVER (PARTITION BY m.conversation_id ORDER BY m.id "
    "            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS turn_no "
    "  FROM messages m JOIN conversations c ON c.id = m.conversation_id "
    "  WHERE c.deleted_at IS NULL AND c.origin='own' "
    "), assistant_replies AS ("
    "  SELECT DISTINCT ON (conversation_id, turn_no) conversation_id, turn_no, lens, answer "
    "  FROM numbered WHERE role='assistant' AND turn_no > 0 "
    "  ORDER BY conversation_id, turn_no, id "
    "), turns AS ("
    "  SELECT n.user_id, n.version, n.conversation_id, n.created_at AS turn_created_at, "
    "    n.personal AS user_personal, ar.lens, ar.answer "
    "  FROM numbered n LEFT JOIN assistant_replies ar "
    "    ON ar.conversation_id = n.conversation_id AND ar.turn_no = n.turn_no "
    "  WHERE n.role='user' "
    ")"
)


def test_usage_stats_conversation_level_filter_matches_frozen_oracle_across_all_shapes():
    """会話単位フィルタの出力が、created_at が id 順と逆転する並び（期間内user→期間外user→期間内assistant）
    を含めて、期間フィルタ無しのオラクルと `_USAGE_TURN_CTE` を使う全6集計（users・worlds・weekly・
    token by_model/by_user/daily）で完全一致する。比較は同一 REPEATABLE READ トランザクション内。"""
    from psycopg import IsolationLevel

    sfx = _sfx()
    uid = _mk("usgcorr")
    world = f"corrworld{sfx}"
    start_ts, _start_date, _end_date, end_exclusive_ts = store._usage_period_bounds(7)

    conv = store.create_conversation(user_id=uid, world=world)
    msg1 = store.add_message(conv["id"], "user", "期間内・本来は返信なし")
    msg2 = store.add_message(conv["id"], "user", "期間外（created_atがidより古い＝単調性崩れ）")
    msg3 = store.add_message(conv["id"], "assistant", "本来はmsg2への返信のはず", lens="impact",
                             answer={"sources": [{"doc_id": "a.md"}],
                                     "usage": {"provider": "openai", "model": f"gpt-corr-{sfx}",
                                               "input_tokens": 10, "cached_input_tokens": 1,
                                               "output_tokens": 20, "reasoning_output_tokens": 0}})
    _set_created(msg1["id"], start_ts + timedelta(seconds=1))
    _set_created(msg2["id"], start_ts - timedelta(days=1))
    _set_created(msg3["id"], start_ts + timedelta(seconds=2))

    conv2 = store.create_conversation(user_id=uid, world=world)
    _turn_with_sources(conv2["id"], "通常ターン(ヒットあり)", lens="qa", sources=[{"doc_id": "b.md"}])
    store.add_message(conv2["id"], "user", "usage計測ターン")
    store.add_message(conv2["id"], "assistant", "usage返信", lens="chat",
                      answer={"usage": {"provider": "openai", "model": f"gpt-corr-{sfx}",
                                        "input_tokens": 5, "cached_input_tokens": 0,
                                        "output_tokens": 7, "reasoning_output_tokens": 0}})

    def _canon(rows):
        out = set()
        for r in rows:
            items = []
            for k, v in dict(r).items():
                if isinstance(v, list):
                    v = tuple(sorted(v))
                items.append((k, v))
            out.add(tuple(sorted(items, key=lambda kv: kv[0])))
        return out

    shapes = [
        ("users", "SELECT user_id AS uid, "
         "  COUNT(*) AS turns, "
         "  COUNT(DISTINCT conversation_id) AS conversations, "
         "  COUNT(DISTINCT (turn_created_at AT TIME ZONE 'Asia/Tokyo')::date) AS active_days, "
         "  MAX(turn_created_at) AS last_active, "
         "  COUNT(*) FILTER (WHERE lens='impact') AS lens_impact, "
         "  COUNT(*) FILTER (WHERE lens='qa') AS lens_qa, "
         "  COUNT(*) FILTER (WHERE lens='troubleshoot') AS lens_troubleshoot, "
         "  COUNT(*) FILTER (WHERE lens='chat') AS lens_chat, "
         "  COUNT(*) FILTER (WHERE user_personal) AS personal_turns, "
         "  ARRAY_REMOVE(ARRAY_AGG(DISTINCT version), NULL) AS worlds, "
         "  COUNT(*) FILTER (WHERE lens IS NOT NULL AND lens != 'chat') AS knowledge_turns, "
         "  COUNT(*) FILTER (WHERE lens IS NOT NULL AND lens != 'chat' AND "
         "    CASE WHEN jsonb_typeof(answer->'sources')='array' "
         "         THEN jsonb_array_length(answer->'sources') ELSE 0 END = 0) AS zero_hit_turns "
         "FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s GROUP BY user_id",
         (start_ts, end_exclusive_ts)),
        ("worlds", "SELECT version AS world, COUNT(*) AS turns FROM turns "
         "WHERE turn_created_at >= %s AND turn_created_at < %s AND version IS NOT NULL GROUP BY version",
         (start_ts, end_exclusive_ts)),
        ("weekly", "SELECT DISTINCT user_id AS uid, "
         "  date_trunc('week', turn_created_at AT TIME ZONE 'Asia/Tokyo')::date AS week_start "
         "FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s",
         (start_ts, end_exclusive_ts)),
        ("token_by_model", "SELECT answer->'usage'->>'provider' AS provider, answer->'usage'->>'model' AS model, "
         + store._usage_token_sum_cols() + " FROM turns "
         "WHERE turn_created_at >= %s AND turn_created_at < %s" + store._USAGE_TOKEN_WHERE +
         "GROUP BY provider, model",
         (start_ts, end_exclusive_ts)),
        ("token_by_user", "SELECT user_id AS uid, " + store._usage_token_sum_cols() + " FROM turns "
         "WHERE turn_created_at >= %s AND turn_created_at < %s" + store._USAGE_TOKEN_WHERE +
         "GROUP BY user_id",
         (start_ts, end_exclusive_ts)),
        ("token_daily", "SELECT (turn_created_at AT TIME ZONE 'Asia/Tokyo')::date AS date, "
         f"SUM({store._usage_tok('input_tokens')}) AS input, SUM({store._usage_tok('output_tokens')}) AS output "
         "FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s" + store._USAGE_TOKEN_WHERE +
         "GROUP BY date",
         (start_ts, end_exclusive_ts)),
    ]

    mismatches = []
    with store._connect() as c:
        c.isolation_level = IsolationLevel.REPEATABLE_READ
        for name, tail_sql, tail_params in shapes:
            oracle_rows = c.execute(_FROZEN_ORACLE_TURN_CTE + " " + tail_sql, tail_params).fetchall()
            new_rows = c.execute(store._USAGE_TURN_CTE + " " + tail_sql,
                                 (start_ts, end_exclusive_ts) + tail_params).fetchall()
            if _canon(oracle_rows) != _canon(new_rows):
                mismatches.append((name, oracle_rows, new_rows))
        c.rollback()

    assert not mismatches, "オラクル（期間フィルタ無し）と会話単位フィルタの出力が不一致: " + "; ".join(
        f"{n}: oracle={o} new={w}" for n, o, w in mismatches
    )


def test_usage_stats_period_prefilter_does_not_leak_orphan_reply_across_boundary():
    """user発言が期間の直前（期間外）・その assistant 返信が期間の直後でも、孤立した返信が
    期間内の別ターンの lens に合流しない。"""
    admin, uid = _admin(), _mk("usgpf")
    period = _stats(admin, 7)["period"]

    conv = store.create_conversation(user_id=uid, world=f"pfworld{_sfx()}")
    stale_user = store.add_message(conv["id"], "user", "期間直前の質問")
    stale_reply = store.add_message(conv["id"], "assistant", "遅れて生成された返信", lens="qa", answer={})
    fresh_user = store.add_message(conv["id"], "user", "期間内の質問")
    fresh_reply = store.add_message(conv["id"], "assistant", "正しい返信", lens="impact", answer={})
    for msg, offset in ((stale_user, "- interval '1 second'"), (stale_reply, "+ interval '1 second'"),
                        (fresh_user, "+ interval '2 second'"), (fresh_reply, "+ interval '3 second'")):
        _sql("UPDATE messages SET created_at = (%s || ' 00:00:00+09:00')::timestamptz " + offset +
             " WHERE id=%s", period["start"], msg["id"])

    row = _urow(_stats(admin, 7), uid)
    assert row["turns"] == 1, f"古いターンが期間内に混入した: turns={row['turns']}"
    assert row["lens"] == {"impact": 1, "qa": 0, "troubleshoot": 0, "chat": 0}, \
        f"孤立 assistant 返信が期間内ターンの lens に混入した: {row['lens']}"


def test_usage_stats_conversation_level_filter_reduces_windowagg_input_rows():
    """会話単位フィルタ（`touched` への JOIN）が WindowAgg へ投入する行数を EXPLAIN ANALYZE で確認する。
    断言は WindowAgg の Actual Rows の差分のみ: 旧(`_FROZEN_ORACLE_TURN_CTE`)-新 は、期間に一切触れない
    ダミー会話の行数以上（旧-新=(B-R)+ダミー行数 ≥ ダミー行数 は共有 DB の既存量に依存しない不変式）。
    比較は同一 REPEATABLE READ・既定 GUC。"""
    from psycopg import IsolationLevel

    uid = _mk("usgexpl")
    world = f"explworld{_sfx()}"
    old_turns = 5000
    excluded_dummy_messages = old_turns * 2
    # 期間に触れない古い会話は小さい conversation_id（実運用の並び）。終了後はカスケード削除＋VACUUM ANALYZE。
    dummy_conv = store.create_conversation(user_id=uid, world=world)
    touched_conv = None
    try:
        with psycopg.connect(store._dsn()) as c:
            c.execute(
                "INSERT INTO messages (conversation_id, role, content, lens, created_at) "
                "SELECT %s, CASE WHEN i %% 2 = 0 THEN 'user' ELSE 'assistant' END, 'x', 'chat', "
                "  now() - interval '400 days' "
                "FROM generate_series(1, %s) AS i",
                (dummy_conv["id"], excluded_dummy_messages),
            )
            c.execute("ANALYZE messages")

        touched_conv = store.create_conversation(user_id=uid, world=world)
        store.add_message(touched_conv["id"], "user", "期間内の質問")
        store.add_message(touched_conv["id"], "assistant", "期間内の返信", lens="chat")

        start_ts, _start_date, _end_date, end_exclusive_ts = store._usage_period_bounds(1)
        tail_sql = "SELECT COUNT(*) AS n FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s"

        def _find_nodes(node, predicate, found=None):
            found = [] if found is None else found
            if isinstance(node, dict):
                if predicate(node):
                    found.append(node)
                for v in node.values():
                    _find_nodes(v, predicate, found)
            elif isinstance(node, list):
                for item in node:
                    _find_nodes(item, predicate, found)
            return found

        with store._connect() as c:
            c.isolation_level = IsolationLevel.REPEATABLE_READ
            old_plan = c.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + _FROZEN_ORACLE_TURN_CTE + " " + tail_sql,
                                 (start_ts, end_exclusive_ts)).fetchone()["QUERY PLAN"]
            new_plan = c.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + store._USAGE_TURN_CTE + " " + tail_sql,
                                 (start_ts, end_exclusive_ts, start_ts, end_exclusive_ts)).fetchone()["QUERY PLAN"]
            c.rollback()

        old_wa = _find_nodes(old_plan[0]["Plan"], lambda n: n.get("Node Type") == "WindowAgg")
        new_wa = _find_nodes(new_plan[0]["Plan"], lambda n: n.get("Node Type") == "WindowAgg")
        assert len(old_wa) == 1, f"旧クエリの WindowAgg ノードが1個でない: {old_wa}"
        assert len(new_wa) == 1, f"新クエリの WindowAgg ノードが1個でない: {new_wa}"
        old_rows, new_rows = old_wa[0]["Actual Rows"], new_wa[0]["Actual Rows"]
        assert old_rows - new_rows >= excluded_dummy_messages, (
            f"WindowAgg 投入行数の削減がダミー行数（{excluded_dummy_messages}）に満たない: "
            f"旧={old_rows} 新={new_rows} 差分={old_rows - new_rows}"
        )
    finally:
        ids_to_delete = [dummy_conv["id"]] + ([touched_conv["id"]] if touched_conv is not None else [])
        with psycopg.connect(store._dsn()) as c:
            c.execute("DELETE FROM conversations WHERE id = ANY(%s)", (ids_to_delete,))
        with psycopg.connect(store._dsn(), autocommit=True) as c:   # VACUUM はトランザクション外
            c.execute("VACUUM ANALYZE messages")


def test_usage_stats_conversation_turns_and_resume_rate():
    """会話3件（user ターン数 1・2・5・2 ターン会話のみ codex_session_id あり）が conversation_turns と
    resume_rate に反映される配線確認（厳密な avg/median/p90/resume_rate は共有 DB では固定できず
    tests/unit/test_usage_conversation_turns.py が担当）。"""
    admin, uid = _admin(), _mk("usgconv")
    world = f"convworld{_sfx()}"
    c1 = store.create_conversation(user_id=uid, world=world)
    _turn(c1["id"], "1ターン目", lens="chat")
    c2 = store.create_conversation(user_id=uid, world=world)
    _turn(c2["id"], "2ターン会話-1", lens="chat")
    _turn(c2["id"], "2ターン会話-2", lens="chat")
    store.set_session_id(c2["id"], f"sess-{_sfx()}")
    c3 = store.create_conversation(user_id=uid, world=world)
    for i in range(5):
        _turn(c3["id"], f"5ターン会話-{i}", lens="chat")

    data = _stats(admin)
    ct = data["conversation_turns"]
    assert ct["max"] >= 5, "最大ターン数5が反映されていない"
    assert ct["avg"] is not None and ct["median"] is not None and ct["p90"] is not None
    assert data["resume_rate"] is not None


def test_usage_stats_by_user_kind_splits_rows_and_matches_by_kind_totals():
    """tokens.by_user_kind はユーザー別×用途別に行が分かれ、同一 kind の合計は by_kind の当該行と一致する。"""
    admin, uid_a, uid_b = _admin(), _mk("usguka"), _mk("usgukb")
    sfx = _sfx()
    world = f"ukworld{sfx}"
    m_a, m_b = f"test-model-uk-chatsub-{sfx}", f"test-model-uk-intent-{sfx}"
    try:
        _usage_event("chat-sub", m_a, uid_a, world, input_tokens=100, output_tokens=20, calls=2, cached=10)
        _usage_event("intent", m_b, uid_b, world, input_tokens=30, output_tokens=5)

        tokens = _stats(admin)["tokens"]
        by_kind_a = next(r for r in tokens["by_kind"] if r["kind"] == "chat-sub" and r["model"] == m_a)
        by_kind_b = next(r for r in tokens["by_kind"] if r["kind"] == "intent" and r["model"] == m_b)
        by_user_kind = {(r["uid"], r["kind"]): r for r in tokens["by_user_kind"]}
        row_a, row_b = by_user_kind[(uid_a, "chat-sub")], by_user_kind[(uid_b, "intent")]

        assert (uid_a, "intent") not in by_user_kind
        assert (uid_b, "chat-sub") not in by_user_kind
        for col in ("calls", "input", "cached_input", "output", "reasoning_output"):
            assert row_a[col] == by_kind_a[col], f"chat-sub の{col}が by_kind と不一致"
            assert row_b[col] == by_kind_b[col], f"intent の{col}が by_kind と不一致"
        assert row_a["calls"] == 2 and row_a["input"] == 100
        assert row_b["calls"] == 1 and row_b["input"] == 30
    finally:
        _delete_usage_events_by_model([m_a, m_b])


def test_usage_stats_response_time_distribution_excludes_rows_without_duration_and_clarify_cards():
    """response_time.by_provider は duration_ms を持つ assistant 行だけで avg/median/p90/max/n を出す。
    確認カード（lens='clarify'）は duration_ms を持っても response_time と
    conversations_top.response_time_avg_ms の母集団に入れない。"""
    admin, uid, uid_c = _admin(), _mk("usgrt"), _mk("usgrtc")
    sfx = _sfx()
    prov_a, prov_b, prov_c = f"rt-prov-a-{sfx}", f"rt-prov-b-{sfx}", f"rtc-prov-{sfx}"
    conv = store.create_conversation(user_id=uid, world=f"rtworld{sfx}")
    for text, prov, ans in (("回答1", prov_a, {"duration_ms": 1000}), ("回答2", prov_a, {"duration_ms": 3000}),
                            ("回答3", prov_b, {"duration_ms": 5000}),
                            ("回答4(duration無し)", prov_a, {})):
        store.add_message(conv["id"], "assistant", text, lens="chat",
                          answer={"usage": {"provider": prov}, **ans})

    conv_c = store.create_conversation(user_id=uid_c, world=f"rtcworld{sfx}")
    _turn(conv_c["id"], "質問1", lens="chat")
    store.add_message(conv_c["id"], "assistant", "確認カード", lens="clarify",
                      answer={"lens": "clarify", "question": {"text": "どれ？"}, "duration_ms": 50})
    store.add_message(conv_c["id"], "user", "A のほう", lens="chat")
    store.add_message(conv_c["id"], "assistant", "回答", lens="chat",
                      # 共有 DB の他会話（上位20件テストの 10^12 級）より上に来る桁
                      answer={"usage": {"provider": prov_c, "input_tokens": 10 ** 14, "output_tokens": 5},
                              "duration_ms": 4000})

    body = _stats(admin)
    by_provider = {row["provider"]: row for row in body["response_time"]["by_provider"]}
    assert by_provider[prov_a] == {"provider": prov_a, "avg": 2000.0, "median": 2000.0, "max": 3000,
                                   "p90": 3000.0, "n": 2}, "duration の無い行が混入、または分布計算が不一致"
    assert by_provider[prov_b] == {"provider": prov_b, "avg": 5000.0, "median": 5000.0, "max": 5000,
                                   "p90": 5000.0, "n": 1}
    assert prov_c in by_provider and by_provider[prov_c]["n"] == 1 and by_provider[prov_c]["max"] == 4000
    assert "unknown" not in by_provider or all(
        row["max"] != 50 for row in body["response_time"]["by_provider"] if row["provider"] == "unknown")
    rows = [c for c in body["conversations_top"] if c["conversation_id"] == conv_c["id"]]
    assert rows and rows[0]["response_time_avg_ms"] == 4000.0


def test_usage_stats_conversations_top_splits_by_conversation_and_excludes_null_conversation_id():
    """conversations_top の kinds は会話ごとに分かれ、conversation_id が NULL の usage_events 行は
    どの会話にも混ざらない。トークン合計（input+output）降順。kinds 内は input+output 降順
    （名前順なら chat<embed だが、使用量順では embed が先）。"""
    admin, uid = _admin(), _mk("usgct")
    sfx = _sfx()
    world = f"ctworld{sfx}"
    m_hi, m_lo, m_null, m_embed = (f"test-model-ct-{k}-{sfx}" for k in ("hi", "lo", "null", "embed"))
    conv_hi = store.create_conversation(user_id=uid, world=world)
    _turn(conv_hi["id"], "会話1-1ターン目", lens="chat")
    conv_lo = store.create_conversation(user_id=uid, world=world)
    _turn(conv_lo["id"], "会話2-1ターン目", lens="chat")
    conv_ck = store.create_conversation(user_id=uid, world=world)
    store.add_message(conv_ck["id"], "user", "会話3-1ターン目")
    store.add_message(conv_ck["id"], "assistant", "(chat)への回答", lens="chat",
                      answer={"usage": {"provider": "openai", "model": f"gpt-ck-{sfx}", "input_tokens": 1,
                                        "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0}})
    try:
        _usage_event("embed", m_embed, uid, world, input_tokens=10 ** 9, output_tokens=10 ** 9, cid=conv_ck["id"])
        _usage_event("chat-sub", m_hi, uid, world, input_tokens=10 ** 12, output_tokens=5 * 10 ** 11,
                     calls=2, cid=conv_hi["id"])
        _usage_event("chat-sub", m_lo, uid, world, input_tokens=10 ** 11, output_tokens=5 * 10 ** 10,
                     calls=1, cid=conv_lo["id"])
        _usage_event("chat-sub", m_null, uid, world, input_tokens=99999, output_tokens=99999,
                     calls=9, cid=None)

        top = _stats(admin)["conversations_top"]
        by_cid = {row["conversation_id"]: row for row in top}
        row_hi, row_lo = by_cid[conv_hi["id"]], by_cid[conv_lo["id"]]
        kinds_hi = {k["kind"]: k for k in row_hi["kinds"]}
        kinds_lo = {k["kind"]: k for k in row_lo["kinds"]}
        assert kinds_hi["chat-sub"]["calls"] == 2
        assert kinds_hi["chat-sub"]["input"] == 10 ** 12
        assert kinds_hi["chat-sub"]["output"] == 5 * 10 ** 11
        assert kinds_lo["chat-sub"]["calls"] == 1
        assert kinds_lo["chat-sub"]["input"] == 10 ** 11
        assert kinds_lo["chat-sub"]["output"] == 5 * 10 ** 10
        for k in row_hi["kinds"] + row_lo["kinds"]:
            assert k["input"] != 99999 and k["output"] != 99999   # NULL 行が混入していない
        assert row_hi["user_turns"] == 1
        assert row_lo["user_turns"] == 1
        assert row_hi["uid"] == uid and row_lo["uid"] == uid
        assert row_hi["world"] == world and row_lo["world"] == world
        cids = [row["conversation_id"] for row in top]
        assert cids.index(conv_hi["id"]) < cids.index(conv_lo["id"])
        assert [k["kind"] for k in by_cid[conv_ck["id"]]["kinds"]] == ["embed", "chat"]
    finally:
        _delete_usage_events_by_model([m_hi, m_lo, m_null, m_embed])


def test_usage_stats_conversations_top_truncates_to_20_ordered_desc_by_tokens():
    """25会話（単調増加の巨大トークン量）から上位20件・トークン合計降順（最小5件は除外）。"""
    admin, uid = _admin(), _mk("usgctt")
    sfx = _sfx()
    world = f"cttworld{sfx}"
    n = 25
    models: list[str] = []
    conv_ids_asc: list[int] = []   # index 0 = 最小トークン
    try:
        for i in range(n):
            conv = store.create_conversation(user_id=uid, world=world)
            _turn(conv["id"], f"会話{i}-1ターン目", lens="chat")
            models.append(f"test-model-cttop-{i}-{sfx}")
            _usage_event("chat-sub", models[-1], uid, world, input_tokens=10**12 + i * 10**9,
                         output_tokens=0, cid=conv["id"])
            conv_ids_asc.append(conv["id"])

        top = _stats(admin)["conversations_top"]
        assert len(top) == 20, "上位20件で打ち切られていない"
        assert [row["conversation_id"] for row in top] == list(reversed(conv_ids_asc))[:20], \
            "並び順（トークン合計降順）または打ち切り対象が一致しない"
    finally:
        _delete_usage_events_by_model(models)


def _add_chat_round(cid: int, *, provider: str, model: str, round_no: int,
                    citations_delta: int, confirmed: int, inferred: int, unknown: int,
                    reason_codes: dict, world: str, uid: str, ts=None,
                    input_tokens: int | None = None, output_tokens: int | None = None) -> None:
    """1巡分の `chat-round` イベント（巡ループが書く meta と同じキー）。"""
    store.add_usage_event(
        kind="chat-round", provider=provider, model=model, calls=1,
        input_tokens=10 * round_no if input_tokens is None else input_tokens,
        output_tokens=5 * round_no if output_tokens is None else output_tokens,
        user_id=uid, world=world, conversation_id=cid, ts=ts,
        meta={"round": round_no, "verdict": "insufficient", "missing": "", "stop": "",
              "lens": "qa", "citations_delta": citations_delta,
              "limits": {}, "claims": {"confirmed": confirmed, "inferred": inferred,
                                       "unknown": unknown, "reason_codes": reason_codes},
              "roles": {}})


def _round(cid: int, **kw) -> None:
    _add_chat_round(cid, **{"citations_delta": 1, "confirmed": 0, "inferred": 0, "unknown": 0,
                            "reason_codes": {}, **kw})


def _by_dp(data: dict) -> dict:
    return {(r["depth_profile"], r["provider"]): r for r in data["rounds"]["by_depth_provider"]}


def test_usage_stats_rounds_depth_provider_distribution_and_reason_codes():
    """chat-round から深さ×経路別の巡数・活動量、最終回答の data.claims から不明理由コードを集計する。
    既存集計（tokens.by_kind/by_user_kind・conversations_top・response_time）は chat-round で変わらない。
    本文（質問/回答）は応答に出ない。"""
    admin, uid = _admin(), _mk("usgrd")
    sfx = _sfx()
    world, provider, model = f"statsworldrd{sfx}", f"testprovrd{sfx}", f"test-model-round-{sfx}"
    secret_q, secret_a = f"質問本文-秘密{sfx}", f"回答本文-非公開{sfx}"

    before = _stats(admin)
    before_by_kind_keys = {(row["kind"], row["model"]) for row in before["tokens"]["by_kind"]}
    before_conv_n = len(before["conversations_top"])
    before_rt_n = before["response_time"]["overall"]["n"] or 0

    conv = store.create_conversation(user_id=uid, world=world, title=f"rounds-{sfx}")
    cid = conv["id"]
    store.add_message(cid, "user", secret_q)
    _add_chat_round(cid, provider=provider, model=model, round_no=1, citations_delta=2,
                    confirmed=1, inferred=0, unknown=1, reason_codes={"budget": 1}, world=world, uid=uid)
    _add_chat_round(cid, provider=provider, model=model, round_no=2, citations_delta=3,
                    confirmed=2, inferred=0, unknown=0, reason_codes={}, world=world, uid=uid)
    store.add_message(
        cid, "assistant", secret_a, lens="qa",
        answer={"usage": {"provider": provider, "depth_profile": "deep",
                          "input_tokens": 100, "output_tokens": 50},
                "duration_ms": 1234, "sources": [],
                "data": {"claims": [
                    {"id": "c1", "status": "confirmed", "text": "x", "evidence_refs": [],
                     "reason": "", "reason_code": ""},
                    {"id": "c2", "status": "unknown", "text": "y", "evidence_refs": [],
                     "reason": "", "reason_code": "budget"},
                ]}})
    try:
        after = _stats(admin)
        after_by_kind_keys = {(row["kind"], row["model"]) for row in after["tokens"]["by_kind"]}
        assert ("chat-round", model) not in after_by_kind_keys, "chat-round が課金集計に混入した"
        assert "chat-round" not in {kind for kind, _ in (after_by_kind_keys - before_by_kind_keys)}
        assert all(row["kind"] != "chat-round" for row in after["tokens"]["by_user_kind"])
        assert len(after["conversations_top"]) >= before_conv_n
        conv_entry = next(c for c in after["conversations_top"] if c["conversation_id"] == cid)
        assert all(k["kind"] != "chat-round" for k in conv_entry["kinds"])
        assert (after["response_time"]["overall"]["n"] or 0) >= before_rt_n + 1

        rounds = after["rounds"]
        row = _by_dp(after)[("deep", provider)]
        assert row["rounds"] == 2
        assert row["citations_delta_total"] == 5
        assert row["input_tokens"] == 10 + 20 and row["output_tokens"] == 5 + 10
        assert row["claims"] == {"confirmed": 3, "inferred": 0, "unknown": 1}
        assert row["reason_codes"] == {"budget": 1}
        dist = {(r["depth_profile"], r["provider"], r["rounds_reached"]): r["turns"]
                for r in rounds["round_distribution"]}
        assert dist[("deep", provider, 2)] == 1
        final_codes = {(r["depth_profile"], r["provider"], r["reason_code"]): r["claims"]
                       for r in rounds["reason_codes"]["final"]}
        assert final_codes[("deep", provider, "budget")] == 1
        round_codes = {(r["depth_profile"], r["provider"], r["reason_code"]): r["claims"]
                       for r in rounds["reason_codes"]["rounds"]}
        assert round_codes[("deep", provider, "budget")] == 1

        body_text = json.dumps(after, ensure_ascii=False)
        assert secret_q not in body_text and secret_a not in body_text
    finally:
        _delete_usage_events_by_model([model])


def test_usage_stats_rounds_period_follows_owning_turn_not_round_ts():
    """巡別記録の期間は「巡が属する user ターン」の created_at 基準——巡イベント自身の ts が期間内でも
    ターンが期間外なら数えない。"""
    admin, uid = _admin(), _mk("usgprd")
    sfx = _sfx()
    world, provider, model = f"prdworld{sfx}", f"prdprov{sfx}", f"prd-model-{sfx}"
    conv = store.create_conversation(user_id=uid, world=world, title=f"prd-{sfx}")
    cid = conv["id"]
    store.add_message(cid, "user", "10日前の質問")
    _round(cid, provider=provider, model=model, round_no=1, world=world, uid=uid)
    store.add_message(cid, "assistant", "10日前の回答", lens="qa",
                      answer={"usage": {"provider": provider, "depth_profile": "deep"}, "sources": []})
    # user だけ10日前へ（assistant は「今」のまま＝対応付け LATERAL の assistant.created_at >= 巡の ts を保つ）
    _sql("UPDATE messages SET created_at = now() - interval '10 days' WHERE conversation_id=%s AND role='user'", cid)
    try:
        assert ("deep", provider) not in _by_dp(_stats(admin, 1)), "所属ターンが期間外なのに巡の ts で混入した"
        by_dp_30 = _by_dp(_stats(admin, 30))
        assert ("deep", provider) in by_dp_30, "所属ターンが期間内（30日）なのに巡が漏れた"
        assert by_dp_30[("deep", provider)]["rounds"] == 1
    finally:
        _delete_usage_events_by_model([model])


def test_usage_stats_rounds_do_not_bind_to_next_turns_assistant():
    """巡→assistant の対応は「巡の ts 以降・次の user メッセージより前」に限る。assistant 未保存（停止等）の
    ターンの巡が次ターンの assistant へ誤結合されず、unmatched_rounds に落ちる。"""
    admin, uid = _admin(), _mk("usgunm")
    sfx = _sfx()
    world, provider, model = f"unmworld{sfx}", f"unmprov{sfx}", f"unm-model-{sfx}"
    conv = store.create_conversation(user_id=uid, world=world, title=f"unm-{sfx}")
    cid = conv["id"]
    store.add_message(cid, "user", "turn1（停止・回答未保存）")
    _round(cid, provider=provider, model=model, round_no=1, world=world, uid=uid)
    store.add_message(cid, "user", "turn2")
    store.add_message(cid, "assistant", "turn2 回答", lens="qa",
                      answer={"usage": {"provider": provider, "depth_profile": "max"}, "sources": []})
    try:
        out = _stats(admin)
        dist_max = [r for r in out["rounds"]["round_distribution"]
                    if r["depth_profile"] == "max" and r["provider"] == provider]
        assert dist_max == [], "turn1 の巡が turn2 の assistant（depth=max）へ誤結合された"
        assert ("max", provider) not in _by_dp(out)
        assert out["rounds"]["unmatched_rounds"] >= 1
    finally:
        _delete_usage_events_by_model([model])


def test_usage_stats_from_to_round_event_may_cross_upper_bound():
    """巡集計の基準は所属 user 発言の created_at——巡イベント自身の ts が `to` を越えても、
    user 発言が期間内なら数える。"""
    admin, uid = _admin(), _mk("usgxbr")
    sfx = _sfx()
    world, provider, model = f"xbworld{sfx}", f"xbprov{sfx}", f"xb-model-{sfx}"
    base = datetime.now(JST).replace(hour=6, minute=0, second=0, microsecond=0)
    switch = base + timedelta(hours=6)

    conv = store.create_conversation(user_id=uid, world=world, title=f"xb-{sfx}")
    cid = conv["id"]
    store.add_message(cid, "user", "境界直前の質問")
    _add_chat_round(cid, provider=provider, model=model, round_no=1, citations_delta=1,
                    confirmed=1, inferred=0, unknown=0, reason_codes={}, world=world, uid=uid,
                    ts=switch + timedelta(minutes=5), input_tokens=7, output_tokens=1)
    store.add_message(cid, "assistant", "境界直後の回答", lens="qa",
                      answer={"usage": {"provider": provider, "depth_profile": "deep"}, "sources": []})
    _sql("UPDATE messages SET created_at=%s WHERE conversation_id=%s AND role='user'",
         switch - timedelta(minutes=1), cid)
    _sql("UPDATE messages SET created_at=%s WHERE conversation_id=%s AND role='assistant'",
         switch + timedelta(minutes=6), cid)
    try:
        r = admin.get("/admin/usage/stats", params={"from": base.isoformat(), "to": switch.isoformat()})
        assert r.status_code == 200, r.text
        row = _by_dp(r.json())[("deep", provider)]
        assert row["rounds"] == 1, "巡イベントの ts が to を越えたら所属 user 発言が期間内でも数えられなくなった"
        assert row["input_tokens"] == 7
    finally:
        _delete_usage_events_by_model([model])


@pytest.mark.parametrize("params", [
    {"days": 0}, {"days": 366},
    {"days": 7, "from": "2026-09-18T00:00:00+09:00", "to": "2026-09-19T00:00:00+09:00"},
    {"from": "2026-09-18T00:00:00+09:00"},
    {"to": "2026-09-19T00:00:00+09:00"},
    {"from": "2026-09-18T00:00:00", "to": "2026-09-19T00:00:00+09:00"},
    {"from": "2026-09-19T00:00:00+09:00", "to": "2026-09-18T00:00:00+09:00"},
    {"from": "2026-01-01T00:00:00+09:00", "to": "2027-01-02T00:00:00+09:00"},
])
def test_usage_stats_period_params_rejected(params):
    """days は 1〜365・days と from/to の併用不可・from/to は両方必須・オフセット必須・from<to・最大365日。"""
    assert _admin().get("/admin/usage/stats", params=params).status_code == 422


def test_usage_stats_period_params_accepted_default_period_and_days_boundary():
    admin, uid = _admin(), _mk("usgstale")
    conv = store.create_conversation(user_id=uid, world=f"staleworld{_sfx()}")
    _turn(conv["id"], "古いターン", lens="chat")
    _sql("UPDATE messages SET created_at = now() - interval '10 days' WHERE conversation_id=%s", conv["id"])
    assert uid not in {u["uid"] for u in _stats(admin, 1)["users"]}, "10日前のメッセージが days=1 に混入した"
    assert uid in {u["uid"] for u in _stats(admin, 30)["users"]}, "10日前のメッセージが days=30 から漏れた"

    assert admin.get("/admin/usage/stats?days=365").status_code == 200
    r = admin.get("/admin/usage/stats")   # days 省略＝30日・JST 暦日。実際に使った境界も返る
    assert r.status_code == 200, r.text
    period = r.json()["period"]
    b_start, b_start_date, b_end_date, b_end = store._usage_period_bounds(30)
    assert period["days"] == 30
    assert period["start"] == b_start_date.isoformat()
    assert period["end"] == b_end_date.isoformat()
    assert period["from"] == b_start.isoformat() and period["to"] == b_end.isoformat()


def test_admin_usage_quality_run_records_condition_and_executed_period():
    """condition（閉集合）別に集計され rounds=0 も受け付ける。母集団は実行期間（executed_from/to）が
    照会期間に完全に含まれるランだけ（登録時刻ではない）。"""
    admin = _admin()
    sfx = _sfx()
    base = datetime.now(JST).replace(hour=6, minute=0, second=0, microsecond=0)
    switch = base + timedelta(hours=6)
    end = base + timedelta(hours=12)
    run_main, run_deep, run_out = f"qr-main-{sfx}", f"qr-deep-{sfx}", f"qr-out-{sfx}"

    def _post(run_id, condition, rounds, ef, et, **extra):
        return admin.post("/admin/usage/quality-runs", json={
            "rounds": rounds, "condition": condition, "run_id": run_id,
            "executed_from": ef.isoformat(), "executed_to": et.isoformat(), **extra})

    def _by_cond(**kw):
        return {(r["condition"], r["rounds"]): r for r in store.depth_quality_stats(**kw)["by_rounds"]}

    try:
        r1 = _post(run_main, "main", 0, base, switch, correct=4, wrong_assertion=1)
        assert r1.status_code == 200, r1.text
        assert r1.json()["inserted"] is True
        r2 = _post(run_deep, "depth2-quick", 0, base, switch, correct=5)   # 同じ rounds=0 でも条件が違えば別行
        assert r2.status_code == 200, r2.text
        r3 = _post(run_out, "depth2-deep", 3, switch, end + timedelta(hours=1), correct=1)   # 期間はみ出し
        assert r3.status_code == 200, r3.text

        by_cond = _by_cond(time_from=base.isoformat(), time_to=end.isoformat())
        assert by_cond[("main", 0)]["correct"] == 4
        assert by_cond[("main", 0)]["wrong_assertion"] == 1
        assert by_cond[("depth2-quick", 0)]["correct"] == 5
        assert ("depth2-deep", 3) not in by_cond, "実行期間が照会期間を超えるランが集計に入った"

        edge = _by_cond(time_from=base.isoformat(), time_to=switch.isoformat())   # executed_to == to は含む
        assert edge[("main", 0)]["runs"] == 1
        late = _by_cond(time_from=(base + timedelta(minutes=1)).isoformat(), time_to=end.isoformat())
        assert ("main", 0) not in late   # 開始が下限より前は含まない
    finally:
        _delete_quality_runs([run_main, run_deep, run_out])


_QR_OK = {"rounds": 1, "condition": "depth2-deep",
          "executed_from": "2026-09-18T00:00:00+09:00", "executed_to": "2026-09-19T00:00:00+09:00"}


@pytest.mark.parametrize("mutate", [
    lambda b: {**b, "condition": "main-ish"},
    lambda b: {k: v for k, v in b.items() if k != "condition"},
    lambda b: {k: v for k, v in b.items() if k != "executed_from"},
    lambda b: {**b, "executed_from": "2026-09-18T00:00:00"},
    lambda b: {**b, "executed_from": b["executed_to"], "executed_to": b["executed_from"]},
    lambda b: {**b, "executed_from": "2026-01-01T00:00:00+09:00", "executed_to": "2027-01-02T00:00:00+09:00"},
    lambda b: {**b, "rounds": -1},
])
def test_admin_usage_quality_run_rejects_invalid_condition_and_period(mutate):
    """condition は閉集合のみ・実行期間はオフセット必須／from<to／最大365日・rounds は非負。"""
    assert _admin().post("/admin/usage/quality-runs", json=mutate(_QR_OK)).status_code == 422


def test_admin_usage_quality_run_duplicate_run_id_is_idempotent():
    """同じ run_id の再送は2行目を作らず inserted=False（run_id の値は返さない）。"""
    admin = _admin()
    run_id = f"qr-dup-{_sfx()}"
    body = {**_QR_OK, "rounds": 3, "correct": 2, "wrong_assertion": 0, "missing": 1, "regressed": 0,
            "unrated": 0, "run_id": run_id}
    try:
        r1 = admin.post("/admin/usage/quality-runs", json=body)
        assert r1.status_code == 200, r1.text
        assert r1.json()["inserted"] is True
        assert "run_id" not in r1.json()
        r2 = admin.post("/admin/usage/quality-runs", json=body)
        assert r2.status_code == 200, r2.text
        assert r2.json()["inserted"] is False
        with psycopg.connect(store._dsn()) as c:
            row = c.execute("SELECT COUNT(*) AS n FROM depth_quality_runs WHERE run_id=%s", (run_id,)).fetchone()
        assert row[0] == 1, "同じ run_id の再送が2行目を作った（二重計上）"
    finally:
        _delete_quality_runs([run_id])


def test_admin_usage_quality_run_insert_and_audit_are_atomic():
    """監査 INSERT が失敗すれば depth_quality_runs 側の行もロールバックされる。"""
    admin = _admin()
    run_id = f"qr-atomic-{_sfx()}"

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    orig = store._audit_insert
    store._audit_insert = _boom
    try:
        r = admin.post("/admin/usage/quality-runs", json={**_QR_OK, "correct": 1, "run_id": run_id})
        assert r.status_code == 500, r.text
    finally:
        store._audit_insert = orig
    with psycopg.connect(store._dsn()) as c:
        row = c.execute("SELECT COUNT(*) AS n FROM depth_quality_runs WHERE run_id=%s", (run_id,)).fetchone()
    assert row[0] == 0, "監査ログ書込み失敗後も行が残った（非アトミック）"


@pytest.mark.parametrize("literal", ["Infinity", "NaN"])
def test_admin_usage_quality_run_rejects_non_finite_cost(literal):
    """cost_usd の inf/nan は保存前に 422（保存されると SUM(cost_usd) が壊れて利用統計が 500 になる）。"""
    admin = _admin()
    head = ('{"rounds": 1, "condition": "main", "executed_from": "2026-09-18T00:00:00+09:00", '
            '"executed_to": "2026-09-19T00:00:00+09:00", "cost_usd": ')
    r = admin.post("/admin/usage/quality-runs", content=(head + literal + "}").encode(),
                   headers={"content-type": "application/json"})
    assert r.status_code == 422, r.text
    assert admin.get("/admin/usage/stats?days=1").status_code == 200   # 利用統計は壊れない
