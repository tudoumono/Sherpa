"""利用統計 API（2026-07-02-利用統計とホーム掲示板.md Feature 1・admin 専用）テスト。

- admin ゲート（非 admin → 403、未ログイン → 401）
- 集計値の正しさ（seed した会話/メッセージどおり: ターン数・会話数・lens内訳・personal_turns・監査由来）
- メッセージ本文・会話タイトルが一切含まれない（プライバシー）
- 期間境界（days でフィルタされる）
- 閲覧時に admin.usage_viewed が監査される

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import agentic_search, auth, store
from sherpa.api import app


from _common import _login, _sfx, _try_init


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@usage.local", display_name=f"表示名-{uid}",
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）


def _turn(cid: int, user_text: str, *, lens: str | None, personal: bool = False, assistant_personal: bool | None = None):
    """1ターン（user→assistant）を追加する。assistant_personal 省略時は user と同じ扱い。"""
    store.add_message(cid, "user", user_text, personal=personal)
    store.add_message(cid, "assistant", f"({lens})への回答", lens=lens,
                      personal=personal if assistant_personal is None else assistant_personal)


def _turn_with_sources(cid: int, user_text: str, *, lens: str, sources: list):
    """バッチ3（2026-07-03）: ゼロヒット率テスト用。assistant answer.sources を明示指定する。"""
    store.add_message(cid, "user", user_text)
    store.add_message(cid, "assistant", f"({lens})への回答", lens=lens, answer={"sources": sources})


def test_usage_stats_requires_admin_and_login():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"usgusr{sfx}", f"UsageUser{sfx}"
    _mk_user(uid, pw, role="user")

    anon = TestClient(app, raise_server_exceptions=False)
    r = anon.get("/admin/usage/stats")
    assert r.status_code == 401, r.text

    u = _login(uid, pw)
    r2 = u.get("/admin/usage/stats")
    assert r2.status_code == 403, r2.text


def test_usage_stats_aggregates_seeded_conversations_and_audit():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgadm{sfx}", f"UsageAdmin{sfx}"
    heavy_uid, heavy_pw = f"usgheavy{sfx}", f"UsageHeavy{sfx}"
    light_uid, light_pw = f"usglight{sfx}", f"UsageLight{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(heavy_uid, heavy_pw, role="user")
    _mk_user(light_uid, light_pw, role="user")

    world = f"statsworld{sfx}"
    secret_marker = f"極秘の会話本文マーカー-{sfx}"

    # heavy_uid: 2 会話・3 ターン（impact/qa/troubleshoot 各1）・personal ターン1件。
    c1 = store.create_conversation(user_id=heavy_uid, world=world, title=f"タイトルは非公開-{sfx}")
    _turn(c1["id"], secret_marker + "-1", lens="impact")
    _turn(c1["id"], secret_marker + "-2", lens="qa", personal=True)
    c2 = store.create_conversation(user_id=heavy_uid, world=world)
    _turn(c2["id"], secret_marker + "-3", lens="troubleshoot")

    # light_uid: 1 会話・1 ターン（chat）。
    c3 = store.create_conversation(user_id=light_uid, world=world)
    _turn(c3["id"], secret_marker + "-4", lens="chat")

    # 監査由来（logins x2, downloads x1）は heavy_uid のみ。
    store.audit(heavy_uid, "auth.login", "user", f"user:{heavy_uid}")
    store.audit(heavy_uid, "auth.login", "user", f"user:{heavy_uid}")
    store.audit(heavy_uid, "document.downloaded", "document", "doc:1")

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text
    data = r.json()

    # プライバシー: 本文・タイトルは一切含まれない。
    assert secret_marker not in r.text
    assert f"タイトルは非公開-{sfx}" not in r.text

    users_by_uid = {u["uid"]: u for u in data["users"]}
    assert heavy_uid in users_by_uid and light_uid in users_by_uid

    heavy = users_by_uid[heavy_uid]
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

    light = users_by_uid[light_uid]
    assert light["turns"] == 1
    assert light["conversations"] == 1
    assert light["lens"] == {"impact": 0, "qa": 0, "troubleshoot": 0, "chat": 1}
    assert light["personal_turns"] == 0
    assert light["logins"] == 0

    # ターン数降順（heavy が light より上位）。
    heavy_idx = next(i for i, u in enumerate(data["users"]) if u["uid"] == heavy_uid)
    light_idx = next(i for i, u in enumerate(data["users"]) if u["uid"] == light_uid)
    assert heavy_idx < light_idx

    # 全体合計にも反映されている（他テストの残留データがあり得るため >= で確認）。
    assert data["totals"]["turns"] >= 4
    assert data["totals"]["active_users"] >= 2
    assert data["totals"]["conversations"] >= 3

    # 閲覧が admin.usage_viewed として監査される。
    rows = store.list_audit(actor=admin_uid, action="admin.usage_viewed", limit=5)
    assert rows, "admin.usage_viewed was not recorded"
    assert rows[0]["detail"]["days"] == 30


def test_usage_stats_days_boundary_excludes_stale_messages():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgadm2{sfx}", f"UsageAdmin2{sfx}"
    stale_uid, stale_pw = f"usgstale{sfx}", f"UsageStale{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(stale_uid, stale_pw, role="user")

    conv = store.create_conversation(user_id=stale_uid, world=f"staleworld{sfx}")
    _turn(conv["id"], "古いターン", lens="chat")

    # メッセージを 10 日前に巻き戻す（TTL テストと同じ直接 SQL パターン）。
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at = now() - interval '10 days' "
                  "WHERE conversation_id=%s", (conv["id"],))

    admin = _login(admin_uid, admin_pw)

    r1 = admin.get("/admin/usage/stats?days=1")
    assert r1.status_code == 200, r1.text
    uids_1d = {u["uid"] for u in r1.json()["users"]}
    assert stale_uid not in uids_1d, "10日前のメッセージが days=1 の集計に混入した"

    r30 = admin.get("/admin/usage/stats?days=30")
    assert r30.status_code == 200, r30.text
    uids_30d = {u["uid"] for u in r30.json()["users"]}
    assert stale_uid in uids_30d, "10日前のメッセージが days=30 の集計から漏れた"


def test_usage_stats_days_param_clamped():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgadm3{sfx}", f"UsageAdmin3{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    assert admin.get("/admin/usage/stats?days=0").status_code == 422
    assert admin.get("/admin/usage/stats?days=366").status_code == 422
    assert admin.get("/admin/usage/stats?days=365").status_code == 200


def test_usage_stats_works_in_compat_mode(auth_disabled):
    """SHERPA_AUTH_DISABLED=1（互換モード）: クッキーなしで合成 admin として 200 が返る。"""
    if not _try_init():
        pytest.skip("DB down")
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/admin/usage/stats")
    assert r.status_code == 200, r.text
    assert {"users", "totals", "daily"} <= r.json().keys()


def test_usage_stats_active_days_counts_user_messages_only():
    """RV MEDIUM 対応: active_days は assistant のみの日をカウントしない（role='user' の日のみ）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgadm4{sfx}", f"UsageAdmin4{sfx}"
    uid, pw = f"usgadonly{sfx}", f"UsageAdOnly{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    conv = store.create_conversation(user_id=uid, world=f"adworld{sfx}")
    _turn(conv["id"], "本日のターン", lens="chat")   # user+assistant（今日・1日分）

    # assistant 単独メッセージを「別の日」に付け足す（ユーザーメッセージなし）。
    # 誤って active_days に混入すると 2 日になってしまう。
    solo = store.add_message(conv["id"], "assistant", "システム通知的な単独発言", lens="chat")
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at = now() - interval '1 day' WHERE id=%s", (solo["id"],))

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text
    users_by_uid = {u["uid"]: u for u in r.json()["users"]}
    assert users_by_uid[uid]["active_days"] == 1, "assistant 単独の日が active_days に混入した"


def test_usage_stats_daily_buckets_by_jst_not_utc():
    """RV MEDIUM 対応: daily の日付境界は JST（DB セッション timezone に依存しない）。

    絶対日付をハードコードせず実行時刻から相対計算し、既存データ（共有DB・365日窓）による
    混入を避けるため「挿入前後の差分」で検証する（絶対有無ではなく delta を見る）。
    """
    if not _try_init():
        pytest.skip("DB down")
    from datetime import datetime, timedelta, timezone as _tz
    from zoneinfo import ZoneInfo

    sfx = _sfx()
    admin_uid, admin_pw = f"usgadm5{sfx}", f"UsageAdmin5{sfx}"
    uid, pw = f"usgjst{sfx}", f"UsageJst{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    # 「昨日 16:30 UTC」＝ JST では常に1日進んだ日付になる（UTC+9・日本は DST なし＝季節に依らず常に成立）。
    now_utc = datetime.now(_tz.utc)
    target_utc = (now_utc - timedelta(days=1)).replace(hour=16, minute=30, second=0, microsecond=0)
    target_jst = target_utc.astimezone(ZoneInfo("Asia/Tokyo"))
    utc_date_str = target_utc.date().isoformat()
    jst_date_str = target_jst.date().isoformat()
    assert utc_date_str != jst_date_str   # 前提の健全性確認（暦日をまたいでいること）

    admin = _login(admin_uid, admin_pw)

    def _daily_map():
        r = admin.get("/admin/usage/stats?days=5")
        assert r.status_code == 200, r.text
        return {d["date"]: d["turns"] for d in r.json()["daily"]}

    before = _daily_map()

    conv = store.create_conversation(user_id=uid, world=f"jstworld{sfx}")
    msg = store.add_message(conv["id"], "user", "JST境界テスト")
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at=%s WHERE id=%s", (target_utc, msg["id"]))

    after = _daily_map()

    jst_delta = after.get(jst_date_str, 0) - before.get(jst_date_str, 0)
    utc_delta = after.get(utc_date_str, 0) - before.get(utc_date_str, 0)
    assert jst_delta == 1, f"JST日付({jst_date_str})の delta が +1 でない: {jst_delta}"
    assert utc_delta == 0, f"UTC日付({utc_date_str})に混入している（JST基準になっていない）: {utc_delta}"


# ===== RV ラウンド2 対応 =====

def test_usage_stats_excludes_sanitized_snapshot_messages():
    """MEDIUM: sanitized_snapshot（本文コピー済みの内部成果物）の messages が
    owner の turns/conversations/active_days を水増ししない（origin='own' 限定）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgadm6{sfx}", f"UsageAdmin6{sfx}"
    uid, pw = f"usgsnap{sfx}", f"UsageSnap{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    conv = store.create_conversation(user_id=uid, world=f"snapworld{sfx}")
    _turn(conv["id"], "元会話のターン", lens="chat")

    admin = _login(admin_uid, admin_pw)
    before = admin.get("/admin/usage/stats?days=30").json()
    before_row = next((u for u in before["users"] if u["uid"] == uid), None)
    assert before_row is not None
    turns_before, convs_before = before_row["turns"], before_row["conversations"]

    # snapshot を作る（同じ owner uid・messages がコピーされる＝origin='sanitized_snapshot'）。
    snap = store.create_sanitized_snapshot(uid, conv["id"])
    assert snap is not None

    after = admin.get("/admin/usage/stats?days=30").json()
    after_row = next((u for u in after["users"] if u["uid"] == uid), None)
    assert after_row is not None
    assert after_row["turns"] == turns_before, "snapshot 作成で turns が水増しされた"
    assert after_row["conversations"] == convs_before, "snapshot 作成で conversations が水増しされた"


def test_usage_stats_excludes_users_with_zero_user_turns_in_period():
    """MEDIUM: 期間内に user メッセージが1件も無い（assistant のみ該当した）ユーザーは
    users 一覧にも totals.active_users にも出ない（HAVING で除外）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgadm7{sfx}", f"UsageAdmin7{sfx}"
    uid, pw = f"usgaonly{sfx}", f"UsageAOnly{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    conv = store.create_conversation(user_id=uid, world=f"aonlyworld{sfx}")
    # user メッセージを一切作らず、assistant 単独メッセージだけを追加。
    store.add_message(conv["id"], "assistant", "ユーザー発言のない単独発言", lens="chat")

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text
    data = r.json()
    assert uid not in [u["uid"] for u in data["users"]], \
        "user turn 0（assistant のみ）のユーザーが users 一覧に出ている"


def test_usage_stats_conversations_and_last_active_are_user_message_based():
    """MEDIUM: conversations は user メッセージが1件以上ある会話のみを数え、
    last_active は user メッセージの最終時刻を反映する（assistant 単独発言に引きずられない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgadm8{sfx}", f"UsageAdmin8{sfx}"
    uid, pw = f"usgcla{sfx}", f"UsageCla{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    # conv1: user+assistant（正規のターン）。
    conv1 = store.create_conversation(user_id=uid, world=f"claworld{sfx}")
    user_msg = store.add_message(conv1["id"], "user", "質問")
    store.add_message(conv1["id"], "assistant", "回答", lens="chat")

    # conv2: assistant 単独発言のみ（user メッセージ無し）。conv1 の user 発言よりずっと後の時刻にする。
    conv2 = store.create_conversation(user_id=uid, world=f"claworld{sfx}")
    later_msg = store.add_message(conv2["id"], "assistant", "後から来た単独発言", lens="chat")
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at = now() + interval '1 hour' WHERE id=%s",
                  (later_msg["id"],))

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text
    row = next((u for u in r.json()["users"] if u["uid"] == uid), None)
    assert row is not None

    assert row["conversations"] == 1, \
        f"assistant 単独発言だけの conv2 が conversations にカウントされた: {row['conversations']}"
    # last_active は user メッセージ（conv1）の時刻であるべき（conv2 の未来時刻に引きずられない）。
    # DB 生 datetime（dict_row）と API JSON（isoformat 文字列）を同じ表記（isoformat・秒まで）で比較する。
    assert row["last_active"] is not None
    last_active_str = str(row["last_active"])[:19]
    user_msg_created_str = user_msg["created_at"].isoformat()[:19]
    assert last_active_str == user_msg_created_str, (
        f"last_active が user メッセージ時刻でなく assistant 単独発言の未来時刻に引きずられた: "
        f"{last_active_str} != {user_msg_created_str}"
    )


def test_usage_stats_daily_includes_active_users_per_day():
    """Part2-A: daily に active_users（その日に user メッセージを発したユニーク uid 数）が入る。
    JST・origin='own' 限定で、user 単位の delta を確認する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgadm9{sfx}", f"UsageAdmin9{sfx}"
    u1, p1 = f"usgau1{sfx}", f"UsageAu1{sfx}"
    u2, p2 = f"usgau2{sfx}", f"UsageAu2{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(u1, p1, role="user")
    _mk_user(u2, p2, role="user")

    admin = _login(admin_uid, admin_pw)

    def _daily_active_users_map():
        r = admin.get("/admin/usage/stats?days=5")
        assert r.status_code == 200, r.text
        return {d["date"]: d["active_users"] for d in r.json()["daily"]}

    before = _daily_active_users_map()

    # u1 が2ターン（同じ日・同じユーザーは1人としてカウントされるはず）、u2 が1ターン。
    conv1 = store.create_conversation(user_id=u1, world=f"auworld{sfx}")
    _turn(conv1["id"], "u1のターン1", lens="chat")
    _turn(conv1["id"], "u1のターン2", lens="qa")
    conv2 = store.create_conversation(user_id=u2, world=f"auworld{sfx}")
    _turn(conv2["id"], "u2のターン1", lens="impact")

    after = _daily_active_users_map()

    # JST の「今日」を素直に算出（テスト実行環境の tz に依存しないよう UTC+9 で計算）。
    from datetime import datetime, timedelta, timezone as _tz
    today_jst = (datetime.now(_tz.utc) + timedelta(hours=9)).date().isoformat()

    delta = after.get(today_jst, 0) - before.get(today_jst, 0)
    assert delta == 2, f"本日(JST={today_jst})の active_users delta が u1+u2=2 になっていない: {delta}"


# ===== RV ラウンド3 対応 =====

def test_usage_stats_lens_not_inflated_by_stray_assistant_row():
    """MEDIUM: 既に正規ターンがある会話に assistant 単独行（対応する user メッセージなし）を
    追加しても、lens 内訳・turns が水増しされない（各 user ターンの直後の assistant 行だけを数える）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgadm10{sfx}", f"UsageAdmin10{sfx}"
    uid, pw = f"usgstray{sfx}", f"UsageStray{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    admin = _login(admin_uid, admin_pw)

    conv = store.create_conversation(user_id=uid, world=f"strayworld{sfx}")
    _turn(conv["id"], "正規のターン", lens="impact")

    before = admin.get("/admin/usage/stats?days=30").json()
    before_row = next(u for u in before["users"] if u["uid"] == uid)

    # 正規ターンの後に assistant 単独行を追加（user メッセージを伴わない・2件目以降の assistant 行）。
    store.add_message(conv["id"], "assistant", "対応する質問のない単独発言", lens="qa")
    store.add_message(conv["id"], "assistant", "さらにもう1件", lens="troubleshoot")

    after = admin.get("/admin/usage/stats?days=30").json()
    after_row = next(u for u in after["users"] if u["uid"] == uid)

    assert after_row["turns"] == before_row["turns"], "stray assistant 行で turns が水増しされた"
    assert after_row["lens"] == before_row["lens"], \
        f"stray assistant 行で lens が水増しされた: before={before_row['lens']} after={after_row['lens']}"
    assert after_row["lens"]["qa"] == 0 and after_row["lens"]["troubleshoot"] == 0


def test_usage_stats_lens_only_counts_first_assistant_reply_per_turn():
    """MEDIUM: 1つの user ターンに複数の assistant 行が連続しても、最初の1件だけを lens 内訳に数える。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgadm11{sfx}", f"UsageAdmin11{sfx}"
    uid, pw = f"usgmulti{sfx}", f"UsageMulti{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    admin = _login(admin_uid, admin_pw)

    conv = store.create_conversation(user_id=uid, world=f"multiworld{sfx}")
    store.add_message(conv["id"], "user", "1つの質問")
    store.add_message(conv["id"], "assistant", "最初の返答", lens="impact")
    store.add_message(conv["id"], "assistant", "2件目の返答（同じターン扱いのはず）", lens="qa")

    r = admin.get("/admin/usage/stats?days=30")
    row = next(u for u in r.json()["users"] if u["uid"] == uid)
    assert row["turns"] == 1
    assert row["lens"]["impact"] == 1, "最初の assistant 返答が lens に数えられていない"
    assert row["lens"]["qa"] == 0, "2件目の assistant 返答まで lens に数えられてしまった"


def test_usage_stats_daily_sum_matches_users_and_totals_sum():
    """MEDIUM: daily の合計・users の合計・totals が常に一致する
    （以前はローリング境界とフロント描画範囲がズレて最古日が暗黙に drop されていた）。
    期間境界ぎりぎり（境界の直前=期間外／境界ちょうど=期間内）のメッセージで確認する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgadm12{sfx}", f"UsageAdmin12{sfx}"
    uid, pw = f"usgbound{sfx}", f"UsageBound{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    admin = _login(admin_uid, admin_pw)

    days = 7
    r0 = admin.get(f"/admin/usage/stats?days={days}")
    assert r0.status_code == 200, r0.text
    period = r0.json()["period"]
    assert period["days"] == days
    assert period["start"] <= period["end"]

    conv = store.create_conversation(user_id=uid, world=f"boundworld{sfx}")
    in_msg = store.add_message(conv["id"], "user", "境界ちょうど（期間内のはず）")
    store.add_message(conv["id"], "assistant", "回答", lens="chat")
    out_msg = store.add_message(conv["id"], "user", "境界の直前（期間外のはず）")
    store.add_message(conv["id"], "assistant", "回答2", lens="chat")

    # period.start の JST 00:00:00 ちょうど（期間内）と、その1秒前（期間外）に打刻し直す。
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at = (%s || ' 00:00:00+09:00')::timestamptz "
                  "WHERE id=%s", (period["start"], in_msg["id"]))
        c.execute("UPDATE messages SET created_at = (%s || ' 00:00:00+09:00')::timestamptz - interval '1 second' "
                  "WHERE id=%s", (period["start"], out_msg["id"]))

    r = admin.get(f"/admin/usage/stats?days={days}")
    assert r.status_code == 200, r.text
    data = r.json()

    row = next((u for u in data["users"] if u["uid"] == uid), None)
    assert row is not None, "period.start ちょうどのメッセージが期間内に含まれていない"
    assert row["turns"] == 1, f"境界直前のメッセージが誤って期間内に混入した: turns={row['turns']}"

    sum_daily_turns = sum(d["turns"] for d in data["daily"])
    sum_users_turns = sum(u["turns"] for u in data["users"])
    assert sum_daily_turns == sum_users_turns == data["totals"]["turns"], (
        f"daily合計={sum_daily_turns} / users合計={sum_users_turns} / totals={data['totals']['turns']} "
        "が一致しない（集計期間の境界がズレている）"
    )
    # period.start の日付が daily に現れていること（最古日が暗黙に drop されていないこと）。
    assert period["start"] in [d["date"] for d in data["daily"]], \
        f"period.start({period['start']}) の日が daily から欠落している"


# ===== バッチ3（2026-07-03）: 利用の傾向（提案済み全指標）=====

def test_usage_stats_zero_hit_rate_per_user_and_totals():
    """1. ゼロヒット率: ナレッジ参照オンのターン（lens != 'chat'）のうち assistant answer.sources が
    空の割合。lens='chat'（ナレッジオフ）のターンは knowledge_turns に含めない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgzadm{sfx}", f"UsageZAdm{sfx}"
    uid, pw = f"usgzero{sfx}", f"UsageZero{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    conv = store.create_conversation(user_id=uid, world=f"zeroworld{sfx}")
    _turn_with_sources(conv["id"], "q1", lens="impact", sources=[])                       # ゼロヒット
    _turn_with_sources(conv["id"], "q2", lens="qa", sources=[{"doc_id": "a.md", "span": [1, 2]}])  # ヒット
    _turn_with_sources(conv["id"], "q3", lens="troubleshoot", sources=[])                 # ゼロヒット
    _turn(conv["id"], "q4", lens="chat")                                                  # ナレッジオフ＝対象外

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text
    data = r.json()
    row = next(u for u in data["users"] if u["uid"] == uid)
    assert row["knowledge_turns"] == 3, "lens='chat' のターンが knowledge_turns に混入した"
    assert row["zero_hit_turns"] == 2
    assert row["zero_hit_rate"] == pytest.approx(2 / 3)
    assert data["zero_hit"]["knowledge_turns"] >= 3
    assert data["zero_hit"]["zero_hit_turns"] >= 2
    assert data["zero_hit"]["rate"] is not None


def test_usage_stats_zero_hit_rate_is_none_when_no_knowledge_turns():
    """ナレッジ参照ターンが1件もないユーザーは zero_hit_rate が None（0除算を避ける）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgznadm{sfx}", f"UsageZNAdm{sfx}"
    uid, pw = f"usgznone{sfx}", f"UsageZNone{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    conv = store.create_conversation(user_id=uid, world=f"znoneworld{sfx}")
    _turn(conv["id"], "chat only", lens="chat")

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text
    row = next(u for u in r.json()["users"] if u["uid"] == uid)
    assert row["knowledge_turns"] == 0
    assert row["zero_hit_turns"] == 0
    assert row["zero_hit_rate"] is None


def test_usage_stats_heatmap_buckets_by_jst_weekday_and_hour():
    """2. 時間帯×曜日ヒートマップ: user メッセージ数を JST 曜日(Postgres DOW: 0=日〜6=土)×時間帯で
    集計する。絶対値ではなく挿入前後の delta で確認する（共有DBの既存データを避けるため）。"""
    if not _try_init():
        pytest.skip("DB down")
    from datetime import datetime, timedelta, timezone as _tz
    from zoneinfo import ZoneInfo

    sfx = _sfx()
    admin_uid, admin_pw = f"usghmadm{sfx}", f"UsageHmAdm{sfx}"
    uid, pw = f"usghm{sfx}", f"UsageHm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    now_utc = datetime.now(_tz.utc)
    target_utc = (now_utc - timedelta(days=2)).replace(hour=18, minute=0, second=0, microsecond=0)
    target_jst = target_utc.astimezone(ZoneInfo("Asia/Tokyo"))
    weekday_pg = (target_jst.weekday() + 1) % 7   # Python: 月=0..日=6 → Postgres DOW: 日=0..土=6
    hour = target_jst.hour

    admin = _login(admin_uid, admin_pw)

    def _heatmap_map():
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        return {(h["weekday"], h["hour"]): h["count"] for h in r.json()["heatmap"]}

    before = _heatmap_map()

    conv = store.create_conversation(user_id=uid, world=f"hmworld{sfx}")
    msg = store.add_message(conv["id"], "user", "heatmap test")
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at=%s WHERE id=%s", (target_utc, msg["id"]))

    after = _heatmap_map()
    key = (weekday_pg, hour)
    delta = after.get(key, 0) - before.get(key, 0)
    assert delta == 1, f"JST 曜日={weekday_pg}・時={hour} のセルの delta が +1 でない: {delta}"


def test_usage_stats_worlds_usage_counts_turns_per_world():
    """3. world（フォルダ）別利用量: conversations.version 別ターン数。world が1つでも正直に1行返す。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgwadm{sfx}", f"UsageWAdm{sfx}"
    uid, pw = f"usgworld{sfx}", f"UsageWorld{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    world_a, world_b = f"worldA{sfx}", f"worldB{sfx}"
    conv1 = store.create_conversation(user_id=uid, world=world_a)
    _turn(conv1["id"], "q1", lens="impact")
    _turn(conv1["id"], "q2", lens="qa")
    conv2 = store.create_conversation(user_id=uid, world=world_b)
    _turn(conv2["id"], "q3", lens="chat")

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text
    worlds_map = {w["world"]: w["turns"] for w in r.json()["worlds"]}
    assert worlds_map.get(world_a) == 2
    assert worlds_map.get(world_b) == 1


def test_usage_stats_providers_usage_from_chat_turn_audit_includes_stopped():
    """4. 頭脳別利用比率: 監査 chat.turn の detail.provider を期間集計。stopped ターンも母数に含む。

    RV バッチ3再検証（2026-07-03）MEDIUM対応後: allowlist 外の値は 'unknown' に畳み込まれる
    （store._USAGE_KNOWN_PROVIDERS 参照）ため、ユニークなマーカー文字列では識別できなくなった。
    実在の allowlist 値（'openai'）を使い、delta（挿入前後の差分）で確認する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgpadm{sfx}", f"UsagePAdm{sfx}"
    uid, pw = f"usgprov{sfx}", f"UsageProv{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    admin = _login(admin_uid, admin_pw)

    def _providers_map():
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        return {p["provider"]: p["turns"] for p in r.json()["providers"]}

    before = _providers_map().get("openai", 0)

    store.audit(uid, "chat.turn", "conversation", "conv:1",
               detail={"provider": "openai", "stopped": False}, outcome="success")
    store.audit(uid, "chat.turn", "conversation", "conv:1",
               detail={"provider": "openai", "stopped": True}, outcome="success")   # 停止ターンも母数に含む
    store.audit(uid, "chat.turn", "conversation", "conv:1",
               detail={"provider": "openai", "stopped": False}, outcome="success")

    after = _providers_map().get("openai", 0)
    assert after - before == 3, "stopped ターンが母数から漏れている、または集計が誤り"


def _turn_with_stop_kind(cid, user_text: str, *, lens: str, stop_kind: str | None):
    """STAT-3 S3: assistant answer.stop_kind を明示指定する（`stop_kind=None` は列自体を持たない
    旧データ/未計測経路を模す＝集計側で 'unknown' に畳み込まれることを確認する用）。"""
    store.add_message(cid, "user", user_text)
    answer = {"stop_kind": stop_kind} if stop_kind is not None else {}
    store.add_message(cid, "assistant", f"({lens})への回答", lens=lens, answer=answer)


def test_usage_stats_stop_kinds_distribution_and_unknown_fallback():
    """STAT-3 S3: `messages.answer.stop_kind`（`sherpa/stop_kind.py` の閉じた8値）の分布。
    NULL（未計測経路・過去データ）は 'unknown' に畳み込む。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgskadm{sfx}", f"UsageSkAdm{sfx}"
    uid, pw = f"usgsk{sfx}", f"UsageSk{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    world = f"statsworldsk{sfx}"

    admin = _login(admin_uid, admin_pw)

    def _stop_kinds_map():
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        return {row["stop_kind"]: row["turns"] for row in r.json()["stop_kinds"]}

    before = _stop_kinds_map()

    conv = store.create_conversation(user_id=uid, world=world, title=f"stopkind-{sfx}")
    _turn_with_stop_kind(conv["id"], "q1", lens="qa", stop_kind="completed")
    _turn_with_stop_kind(conv["id"], "q2", lens="qa", stop_kind="budget")
    _turn_with_stop_kind(conv["id"], "q3", lens="qa", stop_kind="timeout")
    _turn_with_stop_kind(conv["id"], "q4", lens="qa", stop_kind=None)   # 未計測経路→ unknown

    after = _stop_kinds_map()
    assert after.get("completed", 0) - before.get("completed", 0) == 1
    assert after.get("budget", 0) - before.get("budget", 0) == 1
    assert after.get("timeout", 0) - before.get("timeout", 0) == 1
    assert after.get("unknown", 0) - before.get("unknown", 0) == 1


def _turn_with_limits(cid, user_text: str, *, lens: str, provider: str | None,
                      limits: dict | None):
    """内部制限「打ち切りの内訳」（`InvestigationState.limits`）テスト用の1ターン。`provider` は
    `answer.usage.provider`（実際の書込は `agents._usage_meta` 経由・ここではテスト用に直書き）・
    `limits=None` は旧行（キー自体が無い）を模す。"""
    store.add_message(cid, "user", user_text)
    answer: dict = {}
    if provider is not None:
        answer["usage"] = {"provider": provider}
    if limits is not None:
        answer["limits"] = limits
    store.add_message(cid, "assistant", f"({lens})への回答", lens=lens, answer=answer)


def test_usage_stats_limits_aggregates_by_provider_and_ignores_legacy_rows_without_key():
    """`answer.limits`（内部制限の打ち切り計測・制限そのものは変えない）を provider 別に集計する。
    キー自体が無い旧行は分母（turns）にだけ数え、各項目は0のまま集計を壊さない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usglmadm{sfx}", f"UsageLmAdm{sfx}"
    uid, pw = f"usglm{sfx}", f"UsageLm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    world = f"statsworldlm{sfx}"

    admin = _login(admin_uid, admin_pw)

    def _limits_map():
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        return {row["provider"]: row for row in r.json()["limits"]["by_provider"]}

    before = _limits_map()

    conv = store.create_conversation(user_id=uid, world=world, title=f"limits-{sfx}")
    _turn_with_limits(conv["id"], "q1", lens="qa", provider="codex", limits={
        "tool_result_clipped": 2, "total_budget_hit": False, "context_compactions": 1,
        "synthesis_truncated": False, "search_truncated": 3, "auto_continues": 1})
    _turn_with_limits(conv["id"], "q2", lens="qa", provider="codex", limits={
        "tool_result_clipped": 0, "total_budget_hit": True, "context_compactions": 0,
        "synthesis_truncated": True, "search_truncated": 0, "auto_continues": 0})
    _turn_with_limits(conv["id"], "q3", lens="qa", provider="codex", limits=None)   # 旧行（キー無し）

    after = _limits_map()
    before_codex = before.get("codex", {})
    after_codex = after["codex"]

    def _delta(key):
        return (after_codex.get(key) or 0) - (before_codex.get(key) or 0)

    assert _delta("turns") == 3                        # 旧行も母数には入る
    assert _delta("tool_result_clipped_turns") == 1     # 1回以上だったターン数（q1のみ）
    assert _delta("tool_result_clipped_total") == 2     # 合計回数
    assert _delta("total_budget_hit_turns") == 1        # bool 系（q2のみ）
    assert _delta("context_compactions_turns") == 1
    assert _delta("context_compactions_total") == 1
    assert _delta("synthesis_truncated_turns") == 1
    assert _delta("search_truncated_turns") == 1
    assert _delta("search_truncated_total") == 3
    assert _delta("auto_continues_turns") == 1
    assert _delta("auto_continues_total") == 1


def test_usage_stats_stop_kind_folds_out_of_vocabulary_values_into_unknown():
    """⑳: allowlist（`stop_kind.STOP_KINDS`）外の非 NULL な文字列も 'unknown' へ畳み込む
    （是正前は NULL しか畳まず、語彙外の値がそのまま9値目として出ていた）。バグ/env 誤設定等で
    書き込まれる状況を模すため、正規の値で保存した行を直接 UPDATE で語彙外の値に書き換える。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgskvadm{sfx}", f"UsageSkvAdm{sfx}"
    uid, pw = f"usgskv{sfx}", f"UsageSkv{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    world = f"statsworldskv{sfx}"

    admin = _login(admin_uid, admin_pw)

    def _stop_kinds_map():
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        return {row["stop_kind"]: row["turns"] for row in r.json()["stop_kinds"]}

    before = _stop_kinds_map()

    conv = store.create_conversation(user_id=uid, world=world, title=f"stopkindvocab-{sfx}")
    store.add_message(conv["id"], "user", "q1")
    reply = store.add_message(conv["id"], "assistant", "(qa)への回答", lens="qa",
                              answer={"stop_kind": "completed"})
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET answer = jsonb_set(answer, '{stop_kind}', "
                  "'\"not_a_real_stop_kind\"') WHERE id=%s", (reply["id"],))

    after = _stop_kinds_map()
    assert after.get("not_a_real_stop_kind", 0) - before.get("not_a_real_stop_kind", 0) == 0, (
        "allowlist 外の stop_kind 文字列がそのまま独立した分類として出てしまった"
    )
    assert after.get("unknown", 0) - before.get("unknown", 0) == 1, (
        "allowlist 外の stop_kind 文字列が unknown に畳み込まれていない"
    )


def test_usage_stats_stopped_turns_counts_chat_turn_audit_with_stopped_true():
    """STAT-3 S3: `stopped_turns` は利用者の明示停止（`chat.turn` 監査の `detail.stopped=true`）の
    件数——停止ターンは assistant を保存しないため `stop_kinds` の分布には現れない（別集計）。
    `stopped_turns` は `conversations` と JOIN する（⑰）ため、実在する会話に対する監査を使う。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgstadm{sfx}", f"UsageStAdm{sfx}"
    uid, pw = f"usgst{sfx}", f"UsageSt{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    conv = store.create_conversation(user_id=uid, world=f"stworld{sfx}")

    admin = _login(admin_uid, admin_pw)

    def _stopped_turns():
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        return r.json()["stopped_turns"]

    before = _stopped_turns()
    store.audit(uid, "chat.turn", "conversation", f"conv:{conv['id']}",
               detail={"stopped": True}, outcome="success")
    store.audit(uid, "chat.turn", "conversation", f"conv:{conv['id']}",
               detail={"stopped": False}, outcome="success")
    after = _stopped_turns()
    assert after - before == 1


def test_usage_stats_stopped_turns_excludes_deleted_conversation():
    """⑰: `stopped_turns` は `stop_kinds`/`turns` と同じ母集団（`conversations` と JOIN し
    `deleted_at IS NULL AND origin='own'`）を使う——会話を削除すると、その会話の停止ターンは
    `stopped_turns` から外れる（`audit_log` 行自体は監査ログとして残る契約と対照的）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgstdadm{sfx}", f"UsageStdAdm{sfx}"
    uid, pw = f"usgstd{sfx}", f"UsageStd{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    admin = _login(admin_uid, admin_pw)

    def _stopped_turns():
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        return r.json()["stopped_turns"]

    before = _stopped_turns()

    conv = store.create_conversation(user_id=uid, world=f"stdworld{sfx}")
    store.audit(uid, "chat.turn", "conversation", f"conv:{conv['id']}",
               detail={"stopped": True}, outcome="success")

    with_stop = _stopped_turns()
    assert with_stop - before == 1, "実在する会話の停止ターンが数えられていない"

    assert store.delete_conversation(conv["id"], user_id=uid)

    after_delete = _stopped_turns()
    assert after_delete - before == 0, "削除済み会話の停止ターンが stopped_turns に数え続けられている"


def test_usage_stats_stopped_turns_do_not_appear_in_stop_kinds_distribution():
    """明示停止（assistant 未保存・`chat_service.py` の stopped 分岐を模す）は `stop_kinds` の
    分布に現れず、`stopped_turns` 側だけで数えられる（二重計上しない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgsknoadm{sfx}", f"UsageSknoAdm{sfx}"
    uid, pw = f"usgskno{sfx}", f"UsageSkno{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    world = f"stopkindnoworld{sfx}"

    admin = _login(admin_uid, admin_pw)

    def _stop_kinds_total():
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        return sum(row["turns"] for row in r.json()["stop_kinds"])

    def _stopped_turns():
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        return r.json()["stopped_turns"]

    before_total = _stop_kinds_total()
    before_stopped = _stopped_turns()

    # 明示停止したターン: user メッセージのみ保存し assistant は保存しない（stopped 分岐の実際の形）。
    conv = store.create_conversation(user_id=uid, world=world)
    store.add_message(conv["id"], "user", "止めて")
    store.audit(uid, "chat.turn", "conversation", f"conv:{conv['id']}",
               detail={"stopped": True}, outcome="success")

    after_total = _stop_kinds_total()
    after_stopped = _stopped_turns()

    assert after_total - before_total == 0, "assistant 未保存の停止ターンが stop_kinds の分布に混入した"
    assert after_stopped - before_stopped == 1, "停止ターンが stopped_turns に数えられていない"


def test_usage_stats_turns_and_stop_kinds_agree_across_period_boundary():
    """CR-1 ⑪: 期間境界を跨ぐターン（user 行が期間内・assistant 行が期間外＝数分/数時間かかる
    ターンが日境界を越えるケース）でも `turns` と `stop_kinds` が同じ側（今回の期間）に計上される。
    是正前は `stop_kinds` が assistant 行自身の created_at を境界に使っており、この turn が
    turns には数えられるのに stop_kinds には現れない食い違いが起きていた。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgxbadm{sfx}", f"UsageXbAdm{sfx}"
    uid, pw = f"usgxb{sfx}", f"UsageXb{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    admin = _login(admin_uid, admin_pw)

    days = 7
    r0 = admin.get(f"/admin/usage/stats?days={days}")
    assert r0.status_code == 200, r0.text
    period = r0.json()["period"]

    def _stop_kinds_map():
        r = admin.get(f"/admin/usage/stats?days={days}")
        assert r.status_code == 200, r.text
        return {row["stop_kind"]: row["turns"] for row in r.json()["stop_kinds"]}

    def _uid_turns():
        r = admin.get(f"/admin/usage/stats?days={days}")
        assert r.status_code == 200, r.text
        row = next((u for u in r.json()["users"] if u["uid"] == uid), None)
        return row["turns"] if row else 0

    before_turns = _uid_turns()
    before_stop_kinds = _stop_kinds_map()

    conv = store.create_conversation(user_id=uid, world=f"xboundworld{sfx}")
    user_msg = store.add_message(conv["id"], "user", "境界を跨ぐターン")
    assistant_msg = store.add_message(conv["id"], "assistant", "回答", lens="qa",
                                      answer={"stop_kind": "completed"})

    # user 行＝period.end（今回の期間の最終暦日）の JST 23:59:00＝期間内。
    # assistant 行＝period.end の翌日（=期間の排他的上限のさらに1時間後）＝期間外
    # （このターンだけで数時間かかり日境界を越えた状況を再現）。
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at = (%s || ' 23:59:00+09:00')::timestamptz "
                  "WHERE id=%s", (period["end"], user_msg["id"]))
        c.execute("UPDATE messages SET created_at = (%s || ' 00:00:00+09:00')::timestamptz "
                  "+ interval '1 day 1 hour' WHERE id=%s", (period["end"], assistant_msg["id"]))

    after_turns = _uid_turns()
    after_stop_kinds = _stop_kinds_map()

    assert after_turns - before_turns == 1, "user 行が期間内なのに turns に数えられていない"
    assert after_stop_kinds.get("completed", 0) - before_stop_kinds.get("completed", 0) == 1, (
        "assistant 行が期間外（日境界を跨いだ）ため stop_kinds から漏れている＝turns と食い違う"
    )


def test_usage_stats_stopped_turns_use_user_message_time_not_audit_write_time_for_boundary():
    """CR-1 ⑪是正（Codex 節 C3）: `stopped_turns` は監査行自体の `created_at` ではなく、
    `detail.message_id_user`（`_audit_chat_turn` が常に書く）で結合した user 発言の created_at
    （`turns`/`stop_kinds` と同じ `turn_created_at` 境界）で期間を絞る。

    期間開始の直前に始まったターン（user 発言 = start_ts - 1秒 = 期間外）が、監査への書き込みが
    期間開始の直後にずれ込んだ（= start_ts + 1秒・API/DB のわずかな遅延を模す）だけで
    「期間内の停止」に誤って数えられないことを固定する（是正前は監査の created_at だけを見ており、
    この食い違いが起きていた）。"""
    if not _try_init():
        pytest.skip("DB down")
    from datetime import timedelta

    sfx = _sfx()
    admin_uid, admin_pw = f"usgstbadm{sfx}", f"UsageStbAdm{sfx}"
    uid, pw = f"usgstb{sfx}", f"UsageStb{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    admin = _login(admin_uid, admin_pw)

    days = 30
    start_ts, _start_date, _end_date, _end_exclusive_ts = store._usage_period_bounds(days)

    def _stopped_turns():
        r = admin.get(f"/admin/usage/stats?days={days}")
        assert r.status_code == 200, r.text
        return r.json()["stopped_turns"]

    before = _stopped_turns()

    conv = store.create_conversation(user_id=uid, world=f"stbworld{sfx}")
    msg = store.add_message(conv["id"], "user", "止めて（期間開始の直前に始まったターン）")
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at = %s WHERE id=%s",
                  (start_ts - timedelta(seconds=1), msg["id"]))

    store.audit(uid, "chat.turn", "conversation", f"conv:{conv['id']}",
               detail={"stopped": True, "message_id_user": msg["id"]}, outcome="success")
    with psycopg.connect(store._dsn()) as c:
        c.execute(
            "UPDATE audit_log SET created_at = %s WHERE id = ("
            "  SELECT id FROM audit_log WHERE actor_user_id=%s AND action='chat.turn' "
            "  ORDER BY id DESC LIMIT 1)",
            (start_ts + timedelta(seconds=1), uid),
        )

    after = _stopped_turns()
    assert after - before == 0, (
        "期間開始の直前に始まったターンが、監査行の書き込み時刻（期間開始の直後）だけで"
        "stopped_turns に数えられている（turn_created_at 境界と食い違う）"
    )


def test_usage_stats_downloads_total_and_daily_from_audit():
    """6. 原本DL数: document.downloaded の期間合計＋日別内訳。delta で確認する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgdadm{sfx}", f"UsageDAdm{sfx}"
    uid, pw = f"usgdl{sfx}", f"UsageDl{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    admin = _login(admin_uid, admin_pw)
    before = admin.get("/admin/usage/stats?days=30").json()["downloads"]["total"]

    store.audit(uid, "document.downloaded", "document", "doc:1", outcome="success")
    store.audit(uid, "document.downloaded", "document", "doc:2", outcome="success")

    after_data = admin.get("/admin/usage/stats?days=30").json()
    after_total = after_data["downloads"]["total"]
    assert after_total - before == 2
    sum_daily = sum(d["count"] for d in after_data["downloads"]["daily"])
    assert sum_daily == after_total, "downloads.daily の合計が downloads.total と一致しない"


def test_usage_stats_retention_field_present_with_expected_shape():
    """5. 定着指標: API 応答に retention.weekly / retention.revisit_rate が存在する（共有 dev DB の
    既存データに引きずられるため厳密な値の検証は store._compute_retention の単体テストで行う）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgradm{sfx}", f"UsageRAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    r = admin.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text
    retention = r.json()["retention"]
    assert "weekly" in retention and "revisit_rate" in retention
    assert isinstance(retention["weekly"], list)
    for w in retention["weekly"]:
        assert {"week_start", "active_users"} <= w.keys()


# ===== Codex RV「バッチ3再検証」5件（MEDIUM3/LOW2・2026-07-03）=====

def test_usage_stats_zero_hit_handles_missing_null_and_non_array_sources_without_500():
    """1./5. RV MEDIUM+LOW: answer が欠落(NULL)・sources が JSON null・sources が非配列（想定外データ）
    のいずれでも 500 にならず、全てゼロヒットとして数えられる
    （素朴な `COALESCE(jsonb_array_length(...), 0)` は非配列で例外になっていた・修正後の回帰テスト）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgzeadm{sfx}", f"UsageZeAdm{sfx}"
    uid, pw = f"usgzedge{sfx}", f"UsageZEdge{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    conv = store.create_conversation(user_id=uid, world=f"zedgeworld{sfx}")
    _turn(conv["id"], "answer 自体が無い", lens="impact")                            # answer=NULL
    _turn_with_sources(conv["id"], "sourcesがJSON null", lens="qa", sources=None)     # {"sources": null}
    _turn_with_sources(conv["id"], "sourcesが非配列", lens="troubleshoot", sources="not-an-array")  # 非配列

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text   # 500 にならないことが最重要の確認
    row = next(u for u in r.json()["users"] if u["uid"] == uid)
    assert row["knowledge_turns"] == 3
    assert row["zero_hit_turns"] == 3, "answer欠落/JSON null/非配列のいずれかがゼロヒット判定から漏れた"


def test_usage_stats_heatmap_jst_midnight_boundary_buckets_correctly():
    """2./5. RV LOW: JST ちょうど 00:00:00 のメッセージは hour=0 のバケットに入り、前日23時台には
    混入しない（日付変換の境界確認）。"""
    if not _try_init():
        pytest.skip("DB down")
    from datetime import datetime, timedelta, timezone as _tz
    from zoneinfo import ZoneInfo

    sfx = _sfx()
    admin_uid, admin_pw = f"usghmbadm{sfx}", f"UsageHmbAdm{sfx}"
    uid, pw = f"usghmb{sfx}", f"UsageHmb{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")

    jst = ZoneInfo("Asia/Tokyo")
    today_jst_date = datetime.now(_tz.utc).astimezone(jst).date()
    midnight_jst = datetime(today_jst_date.year, today_jst_date.month, today_jst_date.day, 0, 0, 0, tzinfo=jst)
    weekday_pg = (midnight_jst.weekday() + 1) % 7   # Python: 月=0..日=6 → Postgres DOW: 日=0..土=6

    admin = _login(admin_uid, admin_pw)

    def _heatmap_map():
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        return {(h["weekday"], h["hour"]): h["count"] for h in r.json()["heatmap"]}

    before = _heatmap_map()

    conv = store.create_conversation(user_id=uid, world=f"hmbworld{sfx}")
    msg = store.add_message(conv["id"], "user", "midnight boundary test")
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at=%s WHERE id=%s", (midnight_jst, msg["id"]))

    after = _heatmap_map()
    key_hour0 = (weekday_pg, 0)
    key_hour23_prev_day = ((weekday_pg - 1) % 7, 23)
    delta_hour0 = after.get(key_hour0, 0) - before.get(key_hour0, 0)
    delta_hour23 = after.get(key_hour23_prev_day, 0) - before.get(key_hour23_prev_day, 0)
    assert delta_hour0 == 1, f"JST 00:00:00 ちょうどが hour=0 バケットに入っていない: {delta_hour0}"
    assert delta_hour23 == 0, f"JST 00:00:00 ちょうどが前日23時台に混入した: {delta_hour23}"


def test_usage_stats_providers_usage_folds_multiple_unknown_values_into_single_bucket():
    """4./5. RV MEDIUM+LOW: allowlist 外の**異なる**不正値（複数）や provider キー自体の欠落が
    まとめて1つの 'unknown' 行に集約される（別行のまま残らない＝畳み込み漏れの直接確認）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgpuadm{sfx}", f"UsagePuAdm{sfx}"
    uid, pw = f"usgpu{sfx}", f"UsagePu{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    admin = _login(admin_uid, admin_pw)

    def _providers_rows():
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        return r.json()["providers"]

    before_unknown = next((p["turns"] for p in _providers_rows() if p["provider"] == "unknown"), 0)

    store.audit(uid, "chat.turn", "conversation", "conv:1", detail={"provider": f"bogus-a-{sfx}"}, outcome="success")
    store.audit(uid, "chat.turn", "conversation", "conv:1", detail={"provider": f"bogus-b-{sfx}"}, outcome="success")
    store.audit(uid, "chat.turn", "conversation", "conv:1", detail={}, outcome="success")   # provider キー自体無し＝NULL

    after_rows = _providers_rows()
    unknown_rows = [p for p in after_rows if p["provider"] == "unknown"]
    assert len(unknown_rows) == 1, "unknown が複数行に分かれている（畳み込み漏れ）"
    assert unknown_rows[0]["turns"] - before_unknown == 3


def test_usage_stats_excludes_future_timestamped_rows_beyond_period_end():
    """3. RV MEDIUM: created_at が「明日」（JST）以降の行は period の上限（end_exclusive_ts）で
    除外される（daily・downloads の両方で確認・クロックスキュー/テスト由来行の混入防止）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgfutadm{sfx}", f"UsageFutAdm{sfx}"
    uid, pw = f"usgfut{sfx}", f"UsageFut{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    admin = _login(admin_uid, admin_pw)

    def _totals():
        d = admin.get("/admin/usage/stats?days=30").json()
        return sum(x["turns"] for x in d["daily"]), d["downloads"]["total"]

    before_daily_total, before_dl_total = _totals()

    conv = store.create_conversation(user_id=uid, world=f"futworld{sfx}")
    msg = store.add_message(conv["id"], "user", "未来の投稿")
    store.audit(uid, "document.downloaded", "document", "doc:1", outcome="success")
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at = now() + interval '3 days' WHERE id=%s", (msg["id"],))
        c.execute(
            "UPDATE audit_log SET created_at = now() + interval '3 days' WHERE id = ("
            "  SELECT id FROM audit_log WHERE actor_user_id=%s AND action='document.downloaded' "
            "  ORDER BY id DESC LIMIT 1)",
            (uid,),
        )

    after_daily_total, after_dl_total = _totals()
    assert after_daily_total == before_daily_total, "未来時刻のメッセージが daily 集計に混入した"
    assert after_dl_total == before_dl_total, "未来時刻の監査行が downloads 集計に混入した"


def test_usage_stats_retention_respects_period_narrow_window_partial_week():
    """5. RV LOW: 期間（days）を週の途中で区切った時、retention.weekly は期間内の日だけを反映する
    （期間外＝昨日の活動まで拾い上げない＝partial week の正直な集計）。"""
    if not _try_init():
        pytest.skip("DB down")
    from datetime import datetime, timedelta, timezone as _tz

    sfx = _sfx()
    admin_uid, admin_pw = f"usgpwadm{sfx}", f"UsagePwAdm{sfx}"
    today_uid, today_pw = f"usgpwtoday{sfx}", f"UsagePwToday{sfx}"
    yest_uid, yest_pw = f"usgpwyest{sfx}", f"UsagePwYest{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(today_uid, today_pw, role="user")
    _mk_user(yest_uid, yest_pw, role="user")
    admin = _login(admin_uid, admin_pw)

    today_jst = (datetime.now(_tz.utc) + timedelta(hours=9)).date()

    def _today_week_active_users(days):
        r = admin.get(f"/admin/usage/stats?days={days}")
        assert r.status_code == 200, r.text
        for w in r.json()["retention"]["weekly"]:
            ws = datetime.fromisoformat(w["week_start"]).date()
            if ws <= today_jst <= ws + timedelta(days=6):
                return w["active_users"]
        return 0

    before_narrow = _today_week_active_users(1)

    conv_today = store.create_conversation(user_id=today_uid, world=f"pwworld{sfx}")
    store.add_message(conv_today["id"], "user", "今日の発言")
    conv_yest = store.create_conversation(user_id=yest_uid, world=f"pwworld{sfx}")
    msg_yest = store.add_message(conv_yest["id"], "user", "昨日の発言")
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at = now() - interval '1 day' WHERE id=%s", (msg_yest["id"],))

    after_narrow = _today_week_active_users(1)   # days=1＝「今日」だけの期間
    assert after_narrow - before_narrow == 1, (
        "days=1（今日だけ）の期間なのに、昨日分の活動が同じ週の active_users に混入した"
        "（period の上限/下限が retention のクエリに正しく効いていない）"
    )


# ===== PERF-1（台帳#17）: usage_stats の期間絞り込み =====

# `_USAGE_TURN_CTE` の「会話単位フィルタ」導入前（PERF-1着手前）の `numbered` 定義を凍結したコピー。
# 期間フィルタを一切持たない＝全 messages/conversations を無条件で window 関数にかける、これまで
# 本番で動いていた挙動そのもの。以降のテストではこれを「挙動オラクル」（実装の変更点＝会話単位
# フィルタの有無に関わらず出力が一致すべき基準）として使う。実装（`store._USAGE_TURN_CTE`）とは
# 独立に維持する固定コピーであり、本番コードの変更に追従して書き換えるものではない。
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
    """PERF-1: 会話単位フィルタ（`store._USAGE_TURN_CTE` 直前のコメント参照）の厳密同値性を、
    id の採番順と created_at の単調性が崩れる破壊的シナリオ込みで確認する。

    ID順で「期間内user（返信なし）→期間外user→期間内assistant」のように created_at が id 順と
    逆転する並びだと、行単位で `m.created_at >= start_ts` を足す実装では期間外 user 行だけが
    取り除かれて `turn_no` の累積カウントが後続行でずれ、本来ペアの無かった期間内 user 行に
    別ターンの assistant 応答が誤結合し得る。会話単位フィルタは対象会話の行を一切間引かないため
    この問題が原理的に起きない――このテストはそれを実データで裏づける。

    `_USAGE_TURN_CTE` が使われる全6箇所（users・worlds・週次retention元・token by_model・
    token by_user・token daily）の出力が `_FROZEN_ORACLE_TURN_CTE`（期間フィルタなしの基準実装）
    と完全一致することを確認する（zero-hit は users 側の knowledge_turns/zero_hit_turns 列に
    同居しているため users の比較に含まれる）。比較は同一 REPEATABLE READ トランザクション内で
    行い、共有 dev DB の並行書込み（別レーンのテスト実行）によるノイズを遮断する
    （両クエリが同一スナップショットを見る）。
    """
    if not _try_init():
        pytest.skip("DB down")
    from datetime import timedelta

    sfx = _sfx()
    uid = f"usgcorr{sfx}"
    _mk_user(uid, f"UsageCorr{sfx}")
    world = f"corrworld{sfx}"

    days = 7
    start_ts, _start_date, _end_date, end_exclusive_ts = store._usage_period_bounds(days)

    # ID 順で「期間内user（返信なし）→期間外user→期間内assistant」。
    # created_at を ID 順と逆転させる（id2 の created_at を id1/id3 より古くする＝単調性を崩す）。
    conv = store.create_conversation(user_id=uid, world=world)
    msg1 = store.add_message(conv["id"], "user", "期間内・本来は返信なし")
    msg2 = store.add_message(conv["id"], "user", "期間外（created_atがidより古い＝単調性崩れ）")
    msg3 = store.add_message(conv["id"], "assistant", "本来はmsg2への返信のはず", lens="impact",
                              answer={"sources": [{"doc_id": "a.md"}],
                                      "usage": {"provider": "openai", "model": f"gpt-corr-{sfx}",
                                                "input_tokens": 10, "cached_input_tokens": 1,
                                                "output_tokens": 20, "reasoning_output_tokens": 0}})
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at = %s WHERE id=%s",
                  (start_ts + timedelta(seconds=1), msg1["id"]))
        c.execute("UPDATE messages SET created_at = %s WHERE id=%s",
                  (start_ts - timedelta(days=1), msg2["id"]))
        c.execute("UPDATE messages SET created_at = %s WHERE id=%s",
                  (start_ts + timedelta(seconds=2), msg3["id"]))

    # ゼロヒット・token 系にも変化を持たせる通常ターン。
    conv2 = store.create_conversation(user_id=uid, world=world)
    _turn_with_sources(conv2["id"], "通常ターン(ヒットあり)", lens="qa", sources=[{"doc_id": "b.md"}])
    store.add_message(conv2["id"], "user", "usage計測ターン")
    store.add_message(conv2["id"], "assistant", "usage返信", lens="chat",
                       answer={"usage": {"provider": "openai", "model": f"gpt-corr-{sfx}",
                                         "input_tokens": 5, "cached_input_tokens": 0,
                                         "output_tokens": 7, "reasoning_output_tokens": 0}})

    def _canon(rows):
        """行の集合を順序無視・リスト列（worlds等）も正規化した比較可能な set に変換する。"""
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

    from psycopg import IsolationLevel
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
    """PERF-1: `_USAGE_TURN_CTE` の基点走査は「期間内にメッセージを1件でも持つ会話」単位で絞る
    （会話内の行は一切間引かない＝厳密同値・store._USAGE_TURN_CTE のコメント参照）。境界を跨ぐ
    ケース（user発言が期間の直前＝期間外・その assistant 返信が期間の直後）でも、孤立した
    assistant 行が期間内の別ターンの lens に誤って合流しないことを確認する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgpfadm{sfx}", f"UsagePfAdm{sfx}"
    uid, pw = f"usgpf{sfx}", f"UsagePf{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    admin = _login(admin_uid, admin_pw)

    days = 7
    period = admin.get(f"/admin/usage/stats?days={days}").json()["period"]

    conv = store.create_conversation(user_id=uid, world=f"pfworld{sfx}")
    # 期間の直前に発言した「古いターン」（期間外＝出力から除外されるべき）。
    stale_user = store.add_message(conv["id"], "user", "期間直前の質問")
    # その assistant 返信が境界を跨いで期間直後に生成された想定（絞り込み後も残る孤立行）。
    stale_reply = store.add_message(conv["id"], "assistant", "遅れて生成された返信", lens="qa")
    # 期間内の正規ターン。
    fresh_user = store.add_message(conv["id"], "user", "期間内の質問")
    fresh_reply = store.add_message(conv["id"], "assistant", "正しい返信", lens="impact")

    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at = (%s || ' 00:00:00+09:00')::timestamptz - interval '1 second' "
                  "WHERE id=%s", (period["start"], stale_user["id"]))
        c.execute("UPDATE messages SET created_at = (%s || ' 00:00:00+09:00')::timestamptz + interval '1 second' "
                  "WHERE id=%s", (period["start"], stale_reply["id"]))
        c.execute("UPDATE messages SET created_at = (%s || ' 00:00:00+09:00')::timestamptz + interval '2 second' "
                  "WHERE id=%s", (period["start"], fresh_user["id"]))
        c.execute("UPDATE messages SET created_at = (%s || ' 00:00:00+09:00')::timestamptz + interval '3 second' "
                  "WHERE id=%s", (period["start"], fresh_reply["id"]))

    r = admin.get(f"/admin/usage/stats?days={days}")
    assert r.status_code == 200, r.text
    row = next(u for u in r.json()["users"] if u["uid"] == uid)
    # 期間内のターンは1件（古いターンの user 発言は期間外なので出力に出ない）。
    assert row["turns"] == 1, f"古いターンが誤って期間内に混入した: turns={row['turns']}"
    # 孤立した assistant 返信（qa）が期間内ターンの lens に混入していれば qa=1 になる。
    assert row["lens"] == {"impact": 1, "qa": 0, "troubleshoot": 0, "chat": 0}, (
        f"境界を跨いだ孤立 assistant 返信が期間内ターンの lens に混入した: {row['lens']}"
    )


def test_usage_stats_conversation_level_filter_reduces_windowagg_input_rows():
    """PERF-1 受け入れ条件（契約の範囲は store._USAGE_TURN_CTE 直前の「契約の範囲」コメント
    参照）: 会話単位フィルタ（`touched` への明示 JOIN）が、`numbered` の turn_no 累積カウント
    計算（WindowAgg）へ**投入される行数**を、全 messages N 行から「期間内に触れた会話」T 行へ
    削減することを EXPLAIN で確認する。

    messages 全体に対する線形の物理読取（Seq/Index Scan でテーブル全体を辿ること）自体が
    無くなることは主張しない（Postgres がどのプランを選ぶかはデータ分布次第）。よってこのテストは
    Seq Scan の有無や特定の索引ノードの存在を断言しない（既定 GUC のまま・特定プラン形状を
    前提にしない）。断言するのは WindowAgg ノードの実測行数（Actual Rows）の**差分**のみ:
    新クエリの WindowAgg 行数は旧クエリ（`_FROZEN_ORACLE_TURN_CTE`＝期間フィルタ無しの基準実装）
    の WindowAgg 行数より、このテストが投入した「一切期間に触れない会話」の行数（`_EXCLUDED_DUMMY_MESSAGES`）
    分以上少ないこと。

    相対比率（旧の何%以下）や絶対マージン（自テスト行数+固定値以下）は使わない: 共有 dev DB の
    既存データ量（B=既存メッセージ総数・R=既存の touched 会話分メッセージ数、どちらも他レーンの
    並行実行で変動する）によっては、旧=B+_EXCLUDED_DUMMY_MESSAGES+own、新=R+own という関係から
    B≈R のとき相対比率・絶対マージンのどちらも偽陽性で壊れうる（実測で確認済み）。一方
    旧-新 = (B-R) + _EXCLUDED_DUMMY_MESSAGES は B≧R（R は B の部分集合）より常に
    `_EXCLUDED_DUMMY_MESSAGES` 以上になる＝共有 dev DB の既存データ量に依存しない不変式。

    旧新の比較は同一 REPEATABLE READ トランザクション内・既定 GUC のまま実行し、共有 dev DB の
    並行書込み（別レーンのテスト実行）がどちらか一方にだけ影響してノイズになるのを遮断する。"""
    if not _try_init():
        pytest.skip("DB down")

    sfx = _sfx()
    uid = f"usgexpl{sfx}"
    _mk_user(uid, f"UsageExpl{sfx}")
    world = f"explworld{sfx}"

    # 古い・一切期間に触れない会話を数千行規模でバルク投入する（1件ずつの store.add_message() は
    # 遅いため raw SQL で一括 INSERT）。会話は期間内へ触れる会話より**先に**（＝小さい
    # conversation_id で）作る＝実運用の「古い会話＝小さい ID」の並びを模す。共有 dev DB を
    # 汚さないよう、テスト終了後に conversations の delete でカスケード削除し、
    # dead tuple・古い統計を残さないよう VACUUM ANALYZE を実行する。
    _OLD_TURNS = 5000
    _EXCLUDED_DUMMY_MESSAGES = _OLD_TURNS * 2   # 新クエリの WindowAgg には一切投入されないはずの行数
    dummy_conv = store.create_conversation(user_id=uid, world=world)   # 先に作る＝小さい conversation_id
    touched_conv = None
    try:
        with psycopg.connect(store._dsn()) as c:
            c.execute(
                "INSERT INTO messages (conversation_id, role, content, lens, created_at) "
                "SELECT %s, CASE WHEN i %% 2 = 0 THEN 'user' ELSE 'assistant' END, 'x', 'chat', "
                "  now() - interval '400 days' "
                "FROM generate_series(1, %s) AS i",
                (dummy_conv["id"], _EXCLUDED_DUMMY_MESSAGES),
            )
            c.execute("ANALYZE messages")

        # ダミーより後（＝大きい conversation_id）に、期間内へ実際に触れる会話を作る。
        touched_conv = store.create_conversation(user_id=uid, world=world)
        store.add_message(touched_conv["id"], "user", "期間内の質問")
        store.add_message(touched_conv["id"], "assistant", "期間内の返信", lens="chat")

        days = 1
        start_ts, _start_date, _end_date, end_exclusive_ts = store._usage_period_bounds(days)
        tail_sql = "SELECT COUNT(*) AS n FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s"

        def _find_nodes(plan_root, predicate):
            found = []

            def walk(node):
                if isinstance(node, dict):
                    if predicate(node):
                        found.append(node)
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for item in node:
                        walk(item)

            walk(plan_root)
            return found

        def _is_windowagg(node) -> bool:
            return node.get("Node Type") == "WindowAgg"

        from psycopg import IsolationLevel
        with store._connect() as c:
            c.isolation_level = IsolationLevel.REPEATABLE_READ
            # 既定 GUC のまま（特定のプラン形状を強制しない）。
            old_plan = c.execute(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + _FROZEN_ORACLE_TURN_CTE + " " + tail_sql,
                (start_ts, end_exclusive_ts),
            ).fetchone()["QUERY PLAN"]
            new_plan = c.execute(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + store._USAGE_TURN_CTE + " " + tail_sql,
                (start_ts, end_exclusive_ts, start_ts, end_exclusive_ts),
            ).fetchone()["QUERY PLAN"]
            c.rollback()

        old_wa_nodes = _find_nodes(old_plan[0]["Plan"], _is_windowagg)
        new_wa_nodes = _find_nodes(new_plan[0]["Plan"], _is_windowagg)
        assert len(old_wa_nodes) == 1, f"旧クエリの WindowAgg ノードが1個でない: {old_wa_nodes}"
        assert len(new_wa_nodes) == 1, f"新クエリの WindowAgg ノードが1個でない: {new_wa_nodes}"
        old_wa_rows = old_wa_nodes[0]["Actual Rows"]
        new_wa_rows = new_wa_nodes[0]["Actual Rows"]

        # 旧-新 は「一切期間に触れない会話として除外されるはずの行数」以上になるはず
        # （共有 dev DB の既存データ量に依存しない不変式・docstring 参照）。
        assert old_wa_rows - new_wa_rows >= _EXCLUDED_DUMMY_MESSAGES, (
            f"新クエリの WindowAgg 投入行数の削減が、自テストが除外対象として投入したダミー行数"
            f"（{_EXCLUDED_DUMMY_MESSAGES}）に満たない: 旧={old_wa_rows} 新={new_wa_rows} "
            f"差分={old_wa_rows - new_wa_rows}"
        )
    finally:
        ids_to_delete = [dummy_conv["id"]] + ([touched_conv["id"]] if touched_conv is not None else [])
        with psycopg.connect(store._dsn()) as c:
            c.execute("DELETE FROM conversations WHERE id = ANY(%s)", (ids_to_delete,))
        # VACUUM はトランザクションブロック内で実行できないため autocommit 接続を使う。
        # dead tuple・古い統計（ANALYZE で書き換えた分布）を後続テストに残さない。
        with psycopg.connect(store._dsn(), autocommit=True) as c:
            c.execute("VACUUM ANALYZE messages")


def test_usage_stats_conversation_turns_and_resume_rate():
    """S4（2026-09-11-利用統計の拡充.md T4）: 会話セッション統計。

    会話3件（user ターン数 1・2・5）を作り、conversation_turns の avg/median/max/p90 と
    resume_rate（user ターン2回以上×codex_session_id 設定済み÷user ターン2回以上）が
    期待どおりに算出されることを確認する（分布の定義は store._compute_conversation_turn_stats・
    tests/unit/test_usage_conversation_turns.py と同じ）。
    """
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgadm6{sfx}", f"UsageAdmin6{sfx}"
    uid, pw = f"usgconv{sfx}", f"UsageConv{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    world = f"convworld{sfx}"

    # 1 ターンの会話（resume_rate の分母には入らない）。
    c1 = store.create_conversation(user_id=uid, world=world)
    _turn(c1["id"], "1ターン目", lens="chat")

    # 2 ターンの会話・codex_session_id あり（resume_rate の分子に入る）。
    c2 = store.create_conversation(user_id=uid, world=world)
    _turn(c2["id"], "2ターン会話-1", lens="chat")
    _turn(c2["id"], "2ターン会話-2", lens="chat")
    store.set_session_id(c2["id"], f"sess-{sfx}")

    # 5 ターンの会話・codex_session_id なし（分母に入るが分子には入らない）。
    c3 = store.create_conversation(user_id=uid, world=world)
    for i in range(5):
        _turn(c3["id"], f"5ターン会話-{i}", lens="chat")

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text
    data = r.json()

    ct = data["conversation_turns"]
    assert ct["max"] >= 5, "この3会話のうち最大ターン数5が反映されていない"
    # avg/median/resume_rate は共有 dev DB の残留会話込みの全体集計のため厳密な期待値を固定できない
    # （他テストが並行して同じ DB に会話を作るため・厳密な avg/median/max/p90/resume_rate の分子分母は
    # DB 抜きで固定できる tests/unit/test_usage_conversation_turns.py が担当する）。ここでは配線
    # （3会話が集計に反映され、応答の形が壊れていないこと）だけを確認する。
    assert ct["avg"] is not None and ct["median"] is not None and ct["p90"] is not None
    assert data["resume_rate"] is not None

    # 期間外（10日前）に巻き戻した専用会話が days=1 の `users` 集計に混入しないことの配線確認
    # （`conversation_turns`/`resume_rate` の期間境界は共有 DB では固定できないため
    # tests/unit/test_usage_conversation_turns.py の純粋関数側で担保する）。
    admin_uid2, admin_pw2 = f"usgadm7{sfx}", f"UsageAdmin7{sfx}"
    _mk_user(admin_uid2, admin_pw2, role="admin")
    stale_uid, stale_pw = f"usgconvstale{sfx}", f"UsageConvStale{sfx}"
    _mk_user(stale_uid, stale_pw, role="user")
    c4 = store.create_conversation(user_id=stale_uid, world=world)
    _turn(c4["id"], "期間外1ターン目", lens="chat")
    _turn(c4["id"], "期間外2ターン目", lens="chat")
    store.set_session_id(c4["id"], f"sess-stale-{sfx}")
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at = now() - interval '10 days' "
                  "WHERE conversation_id=%s", (c4["id"],))

    admin2 = _login(admin_uid2, admin_pw2)
    r_short = admin2.get("/admin/usage/stats?days=1")
    assert r_short.status_code == 200, r_short.text
    users_short = {u["uid"] for u in r_short.json()["users"]}
    assert stale_uid not in users_short, "10日前の会話が days=1 の集計対象に混入した"


def _delete_usage_events_by_model(models: list) -> None:
    """このテストが仕込んだ usage_events 行を model 名で回収する（unique 名なので他テストと
    衝突しない・usage_events は users への FK が無く `_test_users.cleanup_users` の対象外・
    tests/api/test_usage_events.py の同名ヘルパーと同型）。"""
    try:
        with psycopg.connect(store._dsn()) as c:
            c.execute("DELETE FROM usage_events WHERE model = ANY(%s)", (models,))
    except Exception:
        pass


def test_usage_stats_by_user_kind_splits_rows_and_matches_by_kind_totals():
    """STAT-4 U1（2026-09-12-利用統計の拡充2.md §2/§3）: `tokens.by_user_kind` はユーザー別 ×
    用途別（kind）に行が分かれ、同一 kind の合計は `tokens.by_kind` の当該行と一致する。

    2ユーザーが別々の kind（chat-sub と intent）を使う——unique な model 名で他テスト/共有 dev DB の
    既存行と衝突しないようにする（tests/api/test_usage_events.py の by_kind テストと同じ手法）。
    """
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgukadm{sfx}", f"UsageUkAdm{sfx}"
    uid_a, pw_a = f"usguka{sfx}", f"UsageUkA{sfx}"
    uid_b, pw_b = f"usgukb{sfx}", f"UsageUkB{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid_a, pw_a, role="user")
    _mk_user(uid_b, pw_b, role="user")
    world = f"ukworld{sfx}"
    m_a = f"test-model-uk-chatsub-{sfx}"
    m_b = f"test-model-uk-intent-{sfx}"
    models = [m_a, m_b]
    try:
        store.add_usage_event(kind="chat-sub", provider="openai", model=m_a,
                              input_tokens=100, cached_input_tokens=10, output_tokens=20,
                              reasoning_output_tokens=0, calls=2, user_id=uid_a, world=world)
        store.add_usage_event(kind="intent", provider="openai", model=m_b,
                              input_tokens=30, cached_input_tokens=0, output_tokens=5,
                              reasoning_output_tokens=0, calls=1, user_id=uid_b, world=world)

        admin = _login(admin_uid, admin_pw)
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        tokens = r.json()["tokens"]

        by_kind_chatsub = next(row for row in tokens["by_kind"]
                               if row["kind"] == "chat-sub" and row["model"] == m_a)
        by_kind_intent = next(row for row in tokens["by_kind"]
                              if row["kind"] == "intent" and row["model"] == m_b)

        by_user_kind = {(row["uid"], row["kind"]): row for row in tokens["by_user_kind"]}
        row_a = by_user_kind[(uid_a, "chat-sub")]
        row_b = by_user_kind[(uid_b, "intent")]

        # 行が分かれる: uid_a は chat-sub にしか出ず、uid_b は intent にしか出ない。
        assert (uid_a, "intent") not in by_user_kind
        assert (uid_b, "chat-sub") not in by_user_kind

        # 合計が by_kind と一致する（unique model 名で他行と分離済みのため厳密一致で固定できる）。
        for col in ("calls", "input", "cached_input", "output", "reasoning_output"):
            assert row_a[col] == by_kind_chatsub[col], f"chat-sub の{col}が by_kind と不一致"
            assert row_b[col] == by_kind_intent[col], f"intent の{col}が by_kind と不一致"
        assert row_a["calls"] == 2 and row_a["input"] == 100
        assert row_b["calls"] == 1 and row_b["input"] == 30
    finally:
        _delete_usage_events_by_model(models)


def test_usage_stats_response_time_distribution_excludes_rows_without_duration():
    """STAT-4 U1: `response_time.by_provider` は `answer->>'duration_ms'` を持つ assistant 行だけを
    対象に avg/median/p90/max/n を計算し、duration の無い行（利用者停止・実行中・想定外データ）は
    除外する。provider 名を本テスト専用の unique 値にすることで、共有 dev DB の既存行と混ざらず
    厳密な期待値で固定できる（token 系の unique model 名と同じ分離手法）。
    """
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgrtadm{sfx}", f"UsageRtAdm{sfx}"
    uid, pw = f"usgrt{sfx}", f"UsageRt{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    world = f"rtworld{sfx}"
    prov_a = f"rt-prov-a-{sfx}"
    prov_b = f"rt-prov-b-{sfx}"

    conv = store.create_conversation(user_id=uid, world=world)
    store.add_message(conv["id"], "assistant", "回答1", lens="chat",
                       answer={"usage": {"provider": prov_a}, "duration_ms": 1000})
    store.add_message(conv["id"], "assistant", "回答2", lens="chat",
                       answer={"usage": {"provider": prov_a}, "duration_ms": 3000})
    store.add_message(conv["id"], "assistant", "回答3", lens="chat",
                       answer={"usage": {"provider": prov_b}, "duration_ms": 5000})
    # duration_ms の無い行（利用者停止/実行中相当）: 除外されるはず。
    store.add_message(conv["id"], "assistant", "回答4(duration無し)", lens="chat",
                       answer={"usage": {"provider": prov_a}})

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text
    by_provider = {row["provider"]: row for row in r.json()["response_time"]["by_provider"]}

    row_a = by_provider[prov_a]
    assert row_a == {"provider": prov_a, "avg": 2000.0, "median": 2000.0, "max": 3000,
                     "p90": 3000.0, "n": 2}, "duration の無い行が混入した、または分布計算が不一致"

    row_b = by_provider[prov_b]
    assert row_b == {"provider": prov_b, "avg": 5000.0, "median": 5000.0, "max": 5000,
                     "p90": 5000.0, "n": 1}


def test_usage_stats_conversations_top_splits_by_conversation_and_excludes_null_conversation_id():
    """2026-09-12-利用統計の拡充2.md §2 (b): `conversations_top` の各行の
    `kinds` は会話ごとに分かれ、`conversation_id` が NULL の usage_events 行（列追加前の過去データ・
    遡及なし）はどの会話にも混ざらない。unique な model 名で他テスト/共有 dev DB の既存行と
    衝突しないようにする（`by_user_kind` テストと同じ手法）。
    """
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgctadm{sfx}", f"UsageCtAdm{sfx}"
    uid, pw = f"usgct{sfx}", f"UsageCt{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    world = f"ctworld{sfx}"
    m_hi = f"test-model-ct-hi-{sfx}"
    m_lo = f"test-model-ct-lo-{sfx}"
    m_null = f"test-model-ct-null-{sfx}"
    models = [m_hi, m_lo, m_null]

    conv_hi = store.create_conversation(user_id=uid, world=world)
    _turn(conv_hi["id"], "会話1-1ターン目", lens="chat")
    conv_lo = store.create_conversation(user_id=uid, world=world)
    _turn(conv_lo["id"], "会話2-1ターン目", lens="chat")

    try:
        # 会話ごとに分かれた chat-sub usage_events。conv_hi の方がトークン合計が大きい。
        store.add_usage_event(kind="chat-sub", provider="openai", model=m_hi,
                              input_tokens=10 ** 12, cached_input_tokens=0, output_tokens=5 * 10 ** 11,
                              reasoning_output_tokens=0, calls=2, user_id=uid, world=world,
                              conversation_id=conv_hi["id"])
        store.add_usage_event(kind="chat-sub", provider="openai", model=m_lo,
                              input_tokens=10 ** 11, cached_input_tokens=0, output_tokens=5 * 10 ** 10,
                              reasoning_output_tokens=0, calls=1, user_id=uid, world=world,
                              conversation_id=conv_lo["id"])
        # conversation_id が NULL の行（遡及なし・過去データ相当）はどちらの会話にも混ざらない。
        store.add_usage_event(kind="chat-sub", provider="openai", model=m_null,
                              input_tokens=99999, cached_input_tokens=0, output_tokens=99999,
                              reasoning_output_tokens=0, calls=9, user_id=uid, world=world,
                              conversation_id=None)

        admin = _login(admin_uid, admin_pw)
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        conversations_top = r.json()["conversations_top"]

        by_cid = {row["conversation_id"]: row for row in conversations_top}
        assert conv_hi["id"] in by_cid
        assert conv_lo["id"] in by_cid

        row_hi = by_cid[conv_hi["id"]]
        row_lo = by_cid[conv_lo["id"]]

        kinds_hi = {k["kind"]: k for k in row_hi["kinds"]}
        kinds_lo = {k["kind"]: k for k in row_lo["kinds"]}
        assert kinds_hi["chat-sub"]["calls"] == 2
        assert kinds_hi["chat-sub"]["input"] == 10 ** 12
        assert kinds_hi["chat-sub"]["output"] == 5 * 10 ** 11
        assert kinds_lo["chat-sub"]["calls"] == 1
        assert kinds_lo["chat-sub"]["input"] == 10 ** 11
        assert kinds_lo["chat-sub"]["output"] == 5 * 10 ** 10
        # NULL 行（m_null・巨大なトークン数）がどちらの kinds にも現れない
        # ＝conversation_id が NULL の行が混入していないことの確認。
        for k in row_hi["kinds"] + row_lo["kinds"]:
            assert k["input"] != 99999 and k["output"] != 99999

        assert row_hi["user_turns"] == 1
        assert row_lo["user_turns"] == 1
        assert row_hi["uid"] == uid and row_lo["uid"] == uid
        assert row_hi["world"] == world and row_lo["world"] == world

        # トークン合計（input+output）降順: conv_hi（1.5×10^12）が conv_lo（1.5×10^11）より先に出る。
        idx_hi = next(i for i, row in enumerate(conversations_top) if row["conversation_id"] == conv_hi["id"])
        idx_lo = next(i for i, row in enumerate(conversations_top) if row["conversation_id"] == conv_lo["id"])
        assert idx_hi < idx_lo
    finally:
        _delete_usage_events_by_model(models)


def test_usage_stats_conversations_top_kinds_ordered_by_usage_desc_not_by_name():
    """STAT-4 C2是正: `conversations_top` の各行の `kinds` は用途名のアルファベット順ではなく
    input+output 降順（同値は kind 名）で並ぶ——バイト予算超過時の間引き（先頭優先）で、最も
    重い用途が落ちないようにするため。`chat`（軽量）と `embed`（重量）を同じ会話へ仕込み、
    名前順なら `chat` が先（'c' < 'e'）だが、使用量順では `embed` が先に出ることを固定する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgckadm{sfx}", f"UsageCkAdm{sfx}"
    uid, pw = f"usgck{sfx}", f"UsageCk{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    world = f"ckworld{sfx}"
    m_embed = f"test-model-ck-embed-{sfx}"

    conv = store.create_conversation(user_id=uid, world=world)
    # chat: 軽量（1ターン・小さいトークン量）。
    store.add_message(conv["id"], "user", "会話-1ターン目")
    store.add_message(conv["id"], "assistant", "(chat)への回答", lens="chat",
                      answer={"usage": {"provider": "openai", "model": f"gpt-ck-{sfx}",
                                        "input_tokens": 1, "cached_input_tokens": 0,
                                        "output_tokens": 1, "reasoning_output_tokens": 0}})
    try:
        # embed: 重量（chat よりはるかに大きいトークン量）。'embed' > 'chat' はアルファベット順では
        # 後（e > c）だが、使用量では embed が圧倒的に大きい。
        store.add_usage_event(kind="embed", provider="openai", model=m_embed,
                              input_tokens=10 ** 9, cached_input_tokens=0, output_tokens=10 ** 9,
                              reasoning_output_tokens=0, calls=1, user_id=uid, world=world,
                              conversation_id=conv["id"])

        admin = _login(admin_uid, admin_pw)
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        conversations_top = r.json()["conversations_top"]
        row = next(x for x in conversations_top if x["conversation_id"] == conv["id"])
        kind_names = [k["kind"] for k in row["kinds"]]
        assert kind_names == ["embed", "chat"], (
            f"kinds の並びが使用量降順になっていない（名前順のままなら chat が先に出る）: {kind_names}"
        )
    finally:
        _delete_usage_events_by_model([m_embed])


def test_usage_stats_conversations_top_truncates_to_20_ordered_desc_by_tokens():
    """2026-09-12-利用統計の拡充2.md §2 (b): 上位20件で打ち切り、トークン合計（usage_events の
    input+output）降順で並ぶ。

    25会話に単調増加する巨大トークン量（他テスト/共有 dev DB の既存データを圧倒する桁）を仕込み、
    返る20件が「トークン量の大きい方から20件（=25件中、最小5件は除外）」の順であることを固定する。
    """
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgcttadm{sfx}", f"UsageCttAdm{sfx}"
    uid, pw = f"usgctt{sfx}", f"UsageCtt{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    world = f"cttworld{sfx}"
    n = 25
    models: list[str] = []
    conv_ids_asc: list[int] = []   # index 0 = 最小トークン, index n-1 = 最大トークン
    try:
        for i in range(n):
            conv = store.create_conversation(user_id=uid, world=world)
            _turn(conv["id"], f"会話{i}-1ターン目", lens="chat")
            model = f"test-model-cttop-{i}-{sfx}"
            models.append(model)
            # 他データ（既存の共有 dev DB のトークン量は高々数万〜数十万）を圧倒する桁で、
            # かつ i に応じ厳密に単調増加させる。
            tokens = 10**12 + i * 10**9
            store.add_usage_event(kind="chat-sub", provider="openai", model=model,
                                  input_tokens=tokens, cached_input_tokens=0, output_tokens=0,
                                  reasoning_output_tokens=0, calls=1, user_id=uid, world=world,
                                  conversation_id=conv["id"])
            conv_ids_asc.append(conv["id"])

        expected_top20_cids = list(reversed(conv_ids_asc))[:20]   # i=24..5（トークン量の大きい順）

        admin = _login(admin_uid, admin_pw)
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        conversations_top = r.json()["conversations_top"]
        assert len(conversations_top) == 20, "上位20件で打ち切られていない"

        returned_cids = [row["conversation_id"] for row in conversations_top]
        assert returned_cids == expected_top20_cids, "並び順（トークン合計降順）または打ち切り対象が一致しない"
    finally:
        _delete_usage_events_by_model(models)


def test_usage_stats_response_time_excludes_clarify_cards():
    """確認カード（lens='clarify'・回答前の一時停止）は `duration_ms` を持つが回答時間の母集団に
    入れない（`response_time` と `conversations_top.avg_response_time_ms` の両方）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgrtcadm{sfx}", f"UsageRtcAdm{sfx}"
    uid, pw = f"usgrtc{sfx}", f"UsageRtc{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    world = f"rtcworld{sfx}"
    prov = f"rtc-prov-{sfx}"
    conv = store.create_conversation(user_id=uid, world=world)
    _turn(conv["id"], "質問1", lens="chat")
    store.add_message(conv["id"], "assistant", "確認カード", lens="clarify",
                      answer={"lens": "clarify", "question": {"text": "どれ？"}, "duration_ms": 50})
    # 確認カードへの返事（新しい user ターン）→ 本回答（実運用と同じ並び）
    store.add_message(conv["id"], "user", "A のほう", lens="chat")
    store.add_message(conv["id"], "assistant", "回答", lens="chat",
                      # 共有 dev DB の他会話（上位 20 件テストの 10^12 級）より上に来る桁で入れる
                      answer={"usage": {"provider": prov, "input_tokens": 10 ** 14, "output_tokens": 5},
                              "duration_ms": 4000})
    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text
    body = r.json()
    by_provider = {row["provider"]: row for row in body["response_time"]["by_provider"]}
    assert prov in by_provider and by_provider[prov]["n"] == 1 and by_provider[prov]["max"] == 4000
    assert "unknown" not in by_provider or all(
        row["max"] != 50 for row in body["response_time"]["by_provider"] if row["provider"] == "unknown")
    rows = [c for c in body["conversations_top"] if c["conversation_id"] == conv["id"]]
    assert rows and rows[0]["response_time_avg_ms"] == 4000.0


# ===================================================================================
# docs/proposals/2026-09-12-利用統計の拡充2.md §3b: 利用統計チャットの調査ツールが
# 使う絞り込み付き集計関数（`sherpa/store/usage.py`）。エンドポイントを持たないため
# `store.usage_*` を直接呼ぶ（本節のみ HTTP を経由しない）。
# ===================================================================================

def test_usage_by_user_filters_uid_and_clamps_days_above_365():
    """`usage_by_user(days=7, uid=X)` は X の行だけを返し、days=400 は365にクランプされる。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid_a, uid_b = f"u4bua{sfx}", f"u4bub{sfx}"
    _mk_user(uid_a, f"U4buaPw{sfx}")
    _mk_user(uid_b, f"U4bubPw{sfx}")
    world = f"u4buworld{sfx}"
    m_a = f"test-model-u4bu-a-{sfx}"
    m_b = f"test-model-u4bu-b-{sfx}"
    try:
        store.add_usage_event(kind="intent", provider="openai", model=m_a, input_tokens=10,
                              output_tokens=5, calls=1, user_id=uid_a, world=world)
        store.add_usage_event(kind="intent", provider="openai", model=m_b, input_tokens=20,
                              output_tokens=6, calls=1, user_id=uid_b, world=world)

        out = store.usage_by_user(days=400, uid=uid_a)
        assert out["period"]["days"] == 365, "days=400 は365へクランプされるはず"
        assert out["uid"] == uid_a
        uids_seen = {r["uid"] for r in out["rows"]}
        assert uids_seen == {uid_a}, "uid で絞り込んだのに他ユーザーの行が混入した"
        assert all("display_name" not in r for r in out["rows"]), "ツールの戻り値に display_name を含めない"
        row = next(r for r in out["rows"] if r["kind"] == "intent")
        assert row["calls"] == 1 and row["input"] == 10
    finally:
        _delete_usage_events_by_model([m_a, m_b])


def test_usage_by_user_filters_kind():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"u4bk{sfx}"
    _mk_user(uid, f"U4bkPw{sfx}")
    world = f"u4bkworld{sfx}"
    m_intent = f"test-model-u4bk-intent-{sfx}"
    m_embed = f"test-model-u4bk-embed-{sfx}"
    try:
        store.add_usage_event(kind="intent", provider="openai", model=m_intent, input_tokens=1,
                              calls=1, user_id=uid, world=world)
        store.add_usage_event(kind="embed", provider="openai", model=m_embed, input_tokens=2,
                              calls=1, user_id=uid, world=world)
        out = store.usage_by_user(days=30, uid=uid, kind="embed")
        assert out["kind"] == "embed"
        assert {r["kind"] for r in out["rows"]} == {"embed"}
    finally:
        _delete_usage_events_by_model([m_intent, m_embed])


def test_usage_conversations_limit_clamps_to_50():
    """`usage_conversations(limit=100)` は50件に丸まる（`conversations_top` の上位20件打ち切り
    テストと同じ手法＝unique な model 名 55件をトークン量で単調増加させ、返るのが上位50件だけで
    あることを固定する）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"u4cvl{sfx}"
    _mk_user(uid, f"U4cvlPw{sfx}")
    world = f"u4cvlworld{sfx}"
    n = 55
    models: list[str] = []
    conv_ids_asc: list[int] = []
    try:
        for i in range(n):
            conv = store.create_conversation(user_id=uid, world=world)
            _turn(conv["id"], f"質問{i}", lens="chat")
            model = f"test-model-u4cvl-{i}-{sfx}"
            models.append(model)
            store.add_usage_event(kind="chat-sub", provider="openai", model=model,
                                  input_tokens=10**12 + i * 10**9, output_tokens=0, calls=1,
                                  user_id=uid, world=world, conversation_id=conv["id"])
            conv_ids_asc.append(conv["id"])
        expected_top50 = list(reversed(conv_ids_asc))[:50]

        out = store.usage_conversations(days=30, uid=uid, limit=100)
        assert len(out["conversations"]) == 50, "limit=100 は50件に丸まるはず"
        returned_cids = [c["conversation_id"] for c in out["conversations"]]
        assert returned_cids == expected_top50
    finally:
        _delete_usage_events_by_model(models)


def test_usage_conversations_uid_filters_own_conversations_only():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid_a, uid_b = f"u4cva{sfx}", f"u4cvb{sfx}"
    _mk_user(uid_a, f"U4cvaPw{sfx}")
    _mk_user(uid_b, f"U4cvbPw{sfx}")
    world = f"u4cvworld{sfx}"
    conv_a = store.create_conversation(user_id=uid_a, world=world)
    _turn(conv_a["id"], "Aの質問", lens="chat")
    conv_b = store.create_conversation(user_id=uid_b, world=world)
    _turn(conv_b["id"], "Bの質問", lens="chat")

    out = store.usage_conversations(days=30, uid=uid_a)
    cids = {c["conversation_id"] for c in out["conversations"]}
    assert conv_a["id"] in cids
    assert conv_b["id"] not in cids


def test_usage_conversation_detail_missing_id_returns_error_without_body_or_title_keys():
    """存在しない会話 id は本文/タイトルのキーを一切持たない error 辞書を返す。"""
    if not _try_init():
        pytest.skip("DB down")
    out = store.usage_conversation_detail(999_999_999)
    assert out == {"error": "指定した会話が見つかりません"}
    assert "answer" not in out and "title" not in out and "content" not in out


def test_usage_conversation_detail_non_integer_id_returns_error():
    if not _try_init():
        pytest.skip("DB down")
    out = store.usage_conversation_detail("not-an-int")
    assert out == {"error": "conversation_id は整数で指定してください"}


def test_usage_conversation_detail_returns_turns_kinds_and_response_time_series():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"u4cd{sfx}"
    _mk_user(uid, f"U4cdPw{sfx}")
    world = f"u4cdworld{sfx}"
    m = f"test-model-u4cd-{sfx}"
    conv = store.create_conversation(user_id=uid, world=world)
    store.add_message(conv["id"], "user", "質問1")
    store.add_message(conv["id"], "assistant", "回答1", lens="chat",
                      answer={"usage": {"provider": "openai"}, "duration_ms": 1500})
    try:
        store.add_usage_event(kind="intent", provider="openai", model=m, input_tokens=7,
                              calls=1, user_id=uid, world=world, conversation_id=conv["id"])
        out = store.usage_conversation_detail(conv["id"])
        assert out["conversation_id"] == conv["id"]
        assert out["user_turns"] == 1
        kinds = {k["kind"] for k in out["kinds"]}
        assert kinds == {"chat", "intent"}
        assert out["response_time_series"] == [{"turn": 1, "duration_ms": 1500, "provider": "openai"}]
        assert "answer" not in out and "title" not in out
    finally:
        _delete_usage_events_by_model([m])


def test_usage_overview_excludes_display_name():
    """`usage_overview` は `usage_stats()` と同じ材料の射影だが display_name は含めない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"u4ov{sfx}"
    _mk_user(uid, f"U4ovPw{sfx}")
    world = f"u4ovworld{sfx}"
    conv = store.create_conversation(user_id=uid, world=world)
    _turn(conv["id"], "質問1", lens="chat")
    out = store.usage_overview(days=30)
    assert out["period"]["days"] == 30
    assert all("display_name" not in r for r in out["users"])
    assert all("display_name" not in r for r in out["tokens"]["by_user"])
    assert all("display_name" not in r for r in out["tokens"]["by_user_kind"])
    assert all("display_name" not in c for c in out["conversations_top"])


def test_usage_overview_users_last_active_is_json_native_and_serializable():
    """RV是正 #12: `users[].last_active` は DB の `datetime` のまま返さず、`json.dumps`
    （`default` なし）で直列化できる JSON ネイティブ型（isoformat 文字列）にする——直列化に失敗すると
    ツール結果の予算判定（`agentic_search._result_byte_size`）が「測定不能＝特大」扱いになり、
    利用統計チャットが空回答/502 になっていた（`docs/rv/2026-09-12-利用統計の拡充2.md` U4 #12）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"u4json{sfx}"
    _mk_user(uid, f"U4jsonPw{sfx}")
    world = f"u4jsonworld{sfx}"
    conv = store.create_conversation(user_id=uid, world=world)
    _turn(conv["id"], "質問1", lens="chat")
    out = store.usage_overview(days=30)
    assert any(u["uid"] == uid for u in out["users"])
    text = json.dumps(out, ensure_ascii=False)   # default= を渡さない＝ネイティブ型のみで通ること
    assert json.loads(text) == out
    target = next(u for u in out["users"] if u["uid"] == uid)
    assert isinstance(target["last_active"], str)


def test_usage_response_time_filters_by_provider():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"u4rt{sfx}"
    _mk_user(uid, f"U4rtPw{sfx}")
    world = f"u4rtworld{sfx}"
    prov = f"u4rt-prov-{sfx}"
    conv = store.create_conversation(user_id=uid, world=world)
    store.add_message(conv["id"], "assistant", "回答", lens="chat",
                      answer={"usage": {"provider": prov}, "duration_ms": 2000})
    out = store.usage_response_time(days=30, provider=prov)
    assert out["provider"] == prov
    assert out["n"] == 1 and out["max"] == 2000


def test_usage_daily_returns_series_for_each_metric():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"u4dl{sfx}"
    _mk_user(uid, f"U4dlPw{sfx}")
    world = f"u4dlworld{sfx}"
    conv = store.create_conversation(user_id=uid, world=world)
    _turn(conv["id"], "質問1", lens="chat")
    for metric in ("turns", "tokens", "response_time"):
        out = store.usage_daily(days=30, metric=metric)
        assert out["metric"] == metric
        assert isinstance(out["series"], list)


def test_usage_daily_invalid_metric_falls_back_to_turns():
    if not _try_init():
        pytest.skip("DB down")
    out = store.usage_daily(days=30, metric="bogus")
    assert out["metric"] == "turns"


def test_usage_stop_kinds_filters_by_uid():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid_a, uid_b = f"u4ska{sfx}", f"u4skb{sfx}"
    _mk_user(uid_a, f"U4skaPw{sfx}")
    _mk_user(uid_b, f"U4skbPw{sfx}")
    world = f"u4skworld{sfx}"
    conv_a = store.create_conversation(user_id=uid_a, world=world)
    store.add_message(conv_a["id"], "user", "質問A")
    store.add_message(conv_a["id"], "assistant", "回答A", answer={"stop_kind": "completed"})
    conv_b = store.create_conversation(user_id=uid_b, world=world)
    store.add_message(conv_b["id"], "user", "質問B")
    store.add_message(conv_b["id"], "assistant", "回答B", answer={"stop_kind": "completed"})

    out = store.usage_stop_kinds(days=30, uid=uid_a)
    assert out["uid"] == uid_a
    total = sum(r["turns"] for r in out["stop_kinds"])
    assert total == 1, "uid で絞り込んだのに他ユーザーのターンが混入した"


# ===== 巡別記録（chat-round）の集計・期間指定（from/to）・品質採点の入口 =====

def _add_chat_round(cid: int, *, provider: str, model: str, round_no: int,
                    citations_delta: int, confirmed: int, inferred: int, unknown: int,
                    reason_codes: dict, world: str, uid: str, ts=None,
                    input_tokens: int | None = None, output_tokens: int | None = None) -> None:
    """1巡分の `chat-round` イベントを仕込む（巡ループが実際に書く meta 形と同じキー）。"""
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


def _delete_quality_runs(run_ids: list) -> None:
    try:
        with psycopg.connect(store._dsn()) as c:
            c.execute("DELETE FROM depth_quality_runs WHERE run_id = ANY(%s)", (run_ids,))
    except Exception:
        pass


def test_usage_stats_rounds_depth_provider_distribution_and_reason_codes():
    """chat-round（巡別記録）から深さ×経路別の巡数分布・活動量、最終回答の `data.claims` から
    不明理由コードの分布を集計する。既存集計（tokens.by_kind/by_user_kind・conversations_top・
    response_time）は chat-round 行の有無で変わらない（正本と二重に足さない）。
    本文（質問/回答）は応答のどこにも出ない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgrdadm{sfx}", f"UsgRdAdm{sfx}"
    uid, pw = f"usgrd{sfx}", f"UsgRd{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    world = f"statsworldrd{sfx}"
    provider = f"testprovrd{sfx}"
    model = f"test-model-round-{sfx}"
    secret_q = f"質問本文-秘密{sfx}"
    secret_a = f"回答本文-非公開{sfx}"

    admin = _login(admin_uid, admin_pw)

    def _get_stats():
        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        return r.json()

    before = _get_stats()
    before_by_kind_keys = {(row["kind"], row["model"]) for row in before["tokens"]["by_kind"]}
    before_conv_n = len(before["conversations_top"])
    before_rt_n = before["response_time"]["overall"]["n"] or 0

    conv = store.create_conversation(user_id=uid, world=world, title=f"rounds-{sfx}")
    cid = conv["id"]
    store.add_message(cid, "user", secret_q)
    _add_chat_round(cid, provider=provider, model=model, round_no=1, citations_delta=2,
                    confirmed=1, inferred=0, unknown=1, reason_codes={"budget": 1},
                    world=world, uid=uid)
    _add_chat_round(cid, provider=provider, model=model, round_no=2, citations_delta=3,
                    confirmed=2, inferred=0, unknown=0, reason_codes={},
                    world=world, uid=uid)
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
        after = _get_stats()
        after_by_kind_keys = {(row["kind"], row["model"]) for row in after["tokens"]["by_kind"]}
        assert ("chat-round", model) not in after_by_kind_keys, "chat-round が課金集計に混入した"
        # chat-round 由来の kind 名（'chat-round'）はどの by_kind 行にも現れない
        # （新しく増えた行があるとすれば自会話の 'chat' 行だけ・二重計上の対象外）。
        new_kind_names = {kind for kind, _ in (after_by_kind_keys - before_by_kind_keys)}
        assert "chat-round" not in new_kind_names
        assert all(row["kind"] != "chat-round" for row in after["tokens"]["by_user_kind"])

        # conversations_top/response_time は「新しい会話が1件増える」以外の形が変わらないこと
        # （chat-round 由来の kind が conv["kinds"] に紛れ込まない）だけを確認する。
        assert len(after["conversations_top"]) >= before_conv_n
        conv_entry = next(c for c in after["conversations_top"] if c["conversation_id"] == cid)
        assert all(k["kind"] != "chat-round" for k in conv_entry["kinds"])
        assert (after["response_time"]["overall"]["n"] or 0) >= before_rt_n + 1

        rounds = after["rounds"]
        by_dp = {(r["depth_profile"], r["provider"]): r for r in rounds["by_depth_provider"]}
        row = by_dp[("deep", provider)]
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

        # 本文（質問/回答）は集計レスポンスのどこにも出ない。
        body_text = json.dumps(after, ensure_ascii=False)
        assert secret_q not in body_text and secret_a not in body_text
    finally:
        _delete_usage_events_by_model([model])


def test_usage_depth_rounds_tool_omits_body():
    """`usage_depth_rounds`（管理者向け利用統計チャットの調査ツール）は深さ×経路の巡数分布と
    不明理由コードの分布だけを返し、本文（質問/回答/資料名）を一切含まない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"u4drt{sfx}"
    _mk_user(uid, f"U4drtPw{sfx}")
    world = f"u4drtworld{sfx}"
    provider = f"u4drt-prov-{sfx}"
    model = f"u4drt-model-{sfx}"
    secret_q, secret_a = f"秘密の質問{sfx}", f"秘密の回答{sfx}"

    conv = store.create_conversation(user_id=uid, world=world)
    cid = conv["id"]
    store.add_message(cid, "user", secret_q)
    _add_chat_round(cid, provider=provider, model=model, round_no=1, citations_delta=1,
                    confirmed=0, inferred=0, unknown=1, reason_codes={"conflict": 1},
                    world=world, uid=uid)
    store.add_message(cid, "assistant", secret_a, lens="qa",
                      answer={"usage": {"provider": provider, "depth_profile": "max"},
                              "sources": [],
                              "data": {"claims": [
                                  {"id": "c1", "status": "unknown", "text": "y",
                                   "evidence_refs": [], "reason": "", "reason_code": "conflict"},
                              ]}})
    try:
        out = store.usage_depth_rounds(days=30)
        body_text = json.dumps(out, ensure_ascii=False)
        assert secret_q not in body_text and secret_a not in body_text
        dist = {(r["depth_profile"], r["provider"], r["rounds_reached"]): r["turns"]
                for r in out["round_distribution"]}
        assert dist[("max", provider, 1)] == 1
        round_codes = {(r["depth_profile"], r["provider"], r["reason_code"]): r["claims"]
                      for r in out["reason_codes"]["rounds"]}
        assert round_codes[("max", provider, "conflict")] == 1
        final_codes = {(r["depth_profile"], r["provider"], r["reason_code"]): r["claims"]
                       for r in out["reason_codes"]["final"]}
        assert final_codes[("max", provider, "conflict")] == 1
    finally:
        _delete_usage_events_by_model([model])


def test_usage_stats_rounds_period_follows_owning_turn_not_round_ts():
    """巡別記録の期間判定は「巡が属する user ターン」の created_at を使う——巡イベント自身の
    `ts`（記録時刻）が期間内でも、そのターン（user/assistant）が期間外なら数えない
    （最終回答由来の集計と母集団を揃える。`turns`/`conv_turn_rows` 等、他の集計と同じ境界）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgrdprd{sfx}", f"UsgRdPrd{sfx}"
    uid, pw = f"usgprd{sfx}", f"UsgPrd{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    world = f"prdworld{sfx}"
    provider = f"prdprov{sfx}"
    model = f"prd-model-{sfx}"

    conv = store.create_conversation(user_id=uid, world=world, title=f"prd-{sfx}")
    cid = conv["id"]
    store.add_message(cid, "user", "10日前の質問")
    _add_chat_round(cid, provider=provider, model=model, round_no=1, citations_delta=1,
                    confirmed=0, inferred=0, unknown=0, reason_codes={}, world=world, uid=uid)
    store.add_message(cid, "assistant", "10日前の回答", lens="qa",
                      answer={"usage": {"provider": provider, "depth_profile": "deep"}, "sources": []})
    # user メッセージだけを10日前に巻き戻す（assistant はそのまま「今」＝巡の ts 以降を保つ——
    # 対応付け LATERAL の「assistant.created_at >= 巡の ts」を壊さない）。chat-round イベント
    # 自身の ts は「今」のまま（period 判定が「巡が属する user ターン」を見ているかを確認する
    # ための不整合を作る）。
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at = now() - interval '10 days' "
                  "WHERE conversation_id=%s AND role='user'", (cid,))

    admin = _login(admin_uid, admin_pw)
    try:
        out1d = admin.get("/admin/usage/stats?days=1").json()
        by_dp_1d = {(r["depth_profile"], r["provider"]): r for r in out1d["rounds"]["by_depth_provider"]}
        assert ("deep", provider) not in by_dp_1d, (
            "所属ターンが期間外なのに巡イベント自身の ts（今）で days=1 の集計に混入した")

        out30d = admin.get("/admin/usage/stats?days=30").json()
        by_dp_30d = {(r["depth_profile"], r["provider"]): r for r in out30d["rounds"]["by_depth_provider"]}
        assert ("deep", provider) in by_dp_30d, "所属ターンが期間内（30日）なのに巡が集計から漏れた"
        assert by_dp_30d[("deep", provider)]["rounds"] == 1
    finally:
        _delete_usage_events_by_model([model])


def test_usage_stats_rounds_do_not_bind_to_next_turns_assistant():
    """巡→assistant の対応付けは「巡の ts 以降・かつ同会話の次の user メッセージより前」に
    限定する。このターンの assistant が未保存（利用者の停止等）でも、次ターンの assistant へ
    誤って結合されて水増しされてはいけない——対応が見つからない巡は unmatched_rounds に落ちる。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgrdunm{sfx}", f"UsgRdUnm{sfx}"
    uid, pw = f"usgunm{sfx}", f"UsgUnm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, pw, role="user")
    world = f"unmworld{sfx}"
    provider = f"unmprov{sfx}"
    model = f"unm-model-{sfx}"

    conv = store.create_conversation(user_id=uid, world=world, title=f"unm-{sfx}")
    cid = conv["id"]
    store.add_message(cid, "user", "turn1（停止・回答未保存）")
    _add_chat_round(cid, provider=provider, model=model, round_no=1, citations_delta=1,
                    confirmed=0, inferred=0, unknown=0, reason_codes={}, world=world, uid=uid)
    # turn1 は利用者の停止等で assistant を保存しないまま turn2 が始まる。
    store.add_message(cid, "user", "turn2")
    store.add_message(cid, "assistant", "turn2 回答", lens="qa",
                      answer={"usage": {"provider": provider, "depth_profile": "max"}, "sources": []})

    admin = _login(admin_uid, admin_pw)
    try:
        out = admin.get("/admin/usage/stats?days=30").json()
        rounds = out["rounds"]
        dist_max = [r for r in rounds["round_distribution"]
                    if r["depth_profile"] == "max" and r["provider"] == provider]
        assert dist_max == [], "turn1 の巡が turn2 の assistant（depth=max）へ誤結合された"
        by_dp = {(r["depth_profile"], r["provider"]): r for r in rounds["by_depth_provider"]}
        assert ("max", provider) not in by_dp
        assert rounds["unmatched_rounds"] >= 1
    finally:
        _delete_usage_events_by_model([model])


# ----- 期間指定（from/to）-----

def _seed_round_turn(cid: int, *, at, provider: str, model: str, world: str, uid: str,
                     input_tokens: int, text: str) -> None:
    """user ターン（`at`）→ 巡（`at`+1分）→ assistant（`at`+2分）を明示時刻で仕込む。

    期間境界のテストは「所属する user ターンの created_at」で判定される（`_round_rows_query`）
    ため、user メッセージの created_at を直接 UPDATE して境界ちょうどに置く。
    """
    store.add_message(cid, "user", text)
    _add_chat_round(cid, provider=provider, model=model, round_no=1, citations_delta=1,
                    confirmed=1, inferred=0, unknown=0, reason_codes={}, world=world, uid=uid,
                    ts=at + timedelta(minutes=1), input_tokens=input_tokens, output_tokens=1)
    store.add_message(cid, "assistant", f"{text}への回答", lens="qa",
                      answer={"usage": {"provider": provider, "depth_profile": "deep"},
                              "sources": []})
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at=%s WHERE id=("
                  "  SELECT id FROM messages WHERE conversation_id=%s AND role='user' "
                  "  ORDER BY id DESC LIMIT 1)", (at, cid))
        c.execute("UPDATE messages SET created_at=%s WHERE id=("
                  "  SELECT id FROM messages WHERE conversation_id=%s AND role='assistant' "
                  "  ORDER BY id DESC LIMIT 1)", (at + timedelta(minutes=2), cid))


def test_usage_stats_from_to_is_half_open_and_matches_tool():
    """同じ JST 日の中で from/to を切り替えると、下限は含み上限は含まない（半開区間）。
    同じ期間なら `GET /admin/usage/stats` の `rounds` と `usage_depth_rounds` ツールの集計が一致し、
    等価な UTC 表記でも同じ結果になる。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgftadm{sfx}", f"UsgFtAdm{sfx}"
    uid = f"usgft{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, f"UsgFt{sfx}")
    world = f"ftworld{sfx}"
    provider = f"ftprov{sfx}"
    model = f"ft-model-{sfx}"

    jst = timezone(timedelta(hours=9))
    base = datetime.now(jst).replace(hour=6, minute=0, second=0, microsecond=0)
    switch = base + timedelta(hours=6)       # 同じ JST 日の「切替時刻」
    end = base + timedelta(hours=12)

    conv = store.create_conversation(user_id=uid, world=world, title=f"ft-{sfx}")
    cid = conv["id"]
    _seed_round_turn(cid, at=base, provider=provider, model=model, world=world, uid=uid,
                     input_tokens=10, text="切替前の質問")
    _seed_round_turn(cid, at=switch, provider=provider, model=model, world=world, uid=uid,
                     input_tokens=20, text="切替後の質問")

    admin = _login(admin_uid, admin_pw)

    def _api_rounds(f, t):
        r = admin.get("/admin/usage/stats", params={"from": f.isoformat(), "to": t.isoformat()})
        assert r.status_code == 200, r.text
        return r.json()

    try:
        # [base, switch): 下限ちょうどの記録を含み、上限ちょうどの記録は含まない。
        before = _api_rounds(base, switch)
        by_dp = {(r["depth_profile"], r["provider"]): r for r in before["rounds"]["by_depth_provider"]}
        assert by_dp[("deep", provider)]["rounds"] == 1
        assert by_dp[("deep", provider)]["input_tokens"] == 10
        assert before["period"]["from"] == base.isoformat()
        assert before["period"]["to"] == switch.isoformat()

        # [switch, end): 上限ちょうどで切られた側がこちらに入る。
        after = _api_rounds(switch, end)
        by_dp_after = {(r["depth_profile"], r["provider"]): r
                       for r in after["rounds"]["by_depth_provider"]}
        assert by_dp_after[("deep", provider)]["rounds"] == 1
        assert by_dp_after[("deep", provider)]["input_tokens"] == 20

        # API とツールは同じ期間・同じ集計（U4 ツールが返す2表は API の rounds と一致する）。
        tool = store.usage_depth_rounds(time_from=base.isoformat(), time_to=switch.isoformat())
        assert tool["round_distribution"] == before["rounds"]["round_distribution"]
        assert tool["reason_codes"] == before["rounds"]["reason_codes"]
        assert tool["unmatched_rounds"] == before["rounds"]["unmatched_rounds"]
        assert tool["period"]["from"] == base.isoformat()

        # 等価な UTC 表記でも同じ結果（オフセットを解釈している）。
        utc_same = _api_rounds(base.astimezone(timezone.utc), switch.astimezone(timezone.utc))
        assert utc_same["rounds"]["by_depth_provider"] == before["rounds"]["by_depth_provider"]

        # 他の調査ツール（agentic_search.run_tool 経由）も同じ規則で同じ期間を読む。
        def _tool(name, f, t, **extra):
            out, _docs, _cites, _cards = agentic_search.run_tool(
                name, {"from": f.isoformat(), "to": t.isoformat(), **extra}, "v1", None)
            return out

        # usage_conversations: 会話は同じでも user ターン数が期間で変わる（2ターン→1ターン）。
        conv_wide = _tool("usage_conversations", base, end)
        conv_narrow = _tool("usage_conversations", base, switch)
        wide_turns = next(c["user_turns"] for c in conv_wide["conversations"]
                          if c["conversation_id"] == cid)
        narrow_turns = next(c["user_turns"] for c in conv_narrow["conversations"]
                            if c["conversation_id"] == cid)
        assert wide_turns == 2 and narrow_turns == 1
        assert conv_narrow["period"]["from"] == base.isoformat()
        assert conv_narrow["period"]["to"] == switch.isoformat()

        # usage_by_user: chat ターン由来の件数も同じ境界で変わる。
        by_user_wide = _tool("usage_by_user", base, end, uid=uid, kind="chat")
        by_user_narrow = _tool("usage_by_user", base, switch, uid=uid, kind="chat")
        assert sum(r["calls"] for r in by_user_wide["rows"]) == 2
        assert sum(r["calls"] for r in by_user_narrow["rows"]) == 1

        # usage_stop_kinds: 同じ母集団（answer あり・clarify 除外）を同じ境界で数える。
        sk_wide = _tool("usage_stop_kinds", base, end, uid=uid)
        sk_narrow = _tool("usage_stop_kinds", base, switch, uid=uid)
        assert sum(r["turns"] for r in sk_wide["stop_kinds"]) == 2
        assert sum(r["turns"] for r in sk_narrow["stop_kinds"]) == 1

        # 不正な期間（オフセットなし・days との併用）はどのツールでも error で返る。
        for name in ("usage_overview", "usage_by_user", "usage_conversations",
                     "usage_stop_kinds", "usage_depth_rounds"):
            bad, _d, _c, _k = agentic_search.run_tool(
                name, {"from": "2026-09-18T00:00:00", "to": "2026-09-19T00:00:00"}, "v1", None)
            assert "error" in bad, name
            both, _d, _c, _k = agentic_search.run_tool(
                name, {"days": 7, "from": base.isoformat(), "to": switch.isoformat()}, "v1", None)
            assert "error" in both, name
    finally:
        _delete_usage_events_by_model([model])


def test_usage_stats_from_to_round_event_may_cross_upper_bound():
    """巡集計の基準は「所属する user 発言の created_at」——巡イベント自身の `ts` が `to` を
    越えていても、user 発言が期間内なら数える（`usage_events` 由来の集計が `ts` を基準にするのとは
    別の基準。最終回答由来の集計と母集団を揃えるため）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgxbadm{sfx}", f"UsgXbAdm{sfx}"
    uid = f"usgxb{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, f"UsgXb{sfx}")
    world = f"xbworld{sfx}"
    provider = f"xbprov{sfx}"
    model = f"xb-model-{sfx}"

    jst = timezone(timedelta(hours=9))
    base = datetime.now(jst).replace(hour=6, minute=0, second=0, microsecond=0)
    switch = base + timedelta(hours=6)

    conv = store.create_conversation(user_id=uid, world=world, title=f"xb-{sfx}")
    cid = conv["id"]
    # user 発言は境界の1分前（期間内）・巡イベントと assistant は境界の後（期間外の時刻）。
    store.add_message(cid, "user", "境界直前の質問")
    _add_chat_round(cid, provider=provider, model=model, round_no=1, citations_delta=1,
                    confirmed=1, inferred=0, unknown=0, reason_codes={}, world=world, uid=uid,
                    ts=switch + timedelta(minutes=5), input_tokens=7, output_tokens=1)
    store.add_message(cid, "assistant", "境界直後の回答", lens="qa",
                      answer={"usage": {"provider": provider, "depth_profile": "deep"},
                              "sources": []})
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at=%s WHERE conversation_id=%s AND role='user'",
                  (switch - timedelta(minutes=1), cid))
        c.execute("UPDATE messages SET created_at=%s WHERE conversation_id=%s AND role='assistant'",
                  (switch + timedelta(minutes=6), cid))

    admin = _login(admin_uid, admin_pw)
    try:
        r = admin.get("/admin/usage/stats",
                      params={"from": base.isoformat(), "to": switch.isoformat()})
        assert r.status_code == 200, r.text
        by_dp = {(x["depth_profile"], x["provider"]): x
                 for x in r.json()["rounds"]["by_depth_provider"]}
        assert by_dp[("deep", provider)]["rounds"] == 1, (
            "巡イベントの ts が to を越えたら、所属 user 発言が期間内なのに数えられなくなった")
        assert by_dp[("deep", provider)]["input_tokens"] == 7
    finally:
        _delete_usage_events_by_model([model])


def test_usage_stats_from_to_validation_and_days_exclusivity():
    """`days` の明示指定と `from`/`to` の併用は 422。`from`/`to` は両方必須・オフセット必須・
    `from < to`・最大365日。`days` 省略時の既定（30日・JST 暦日）は従来どおり。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgfvadm{sfx}", f"UsgFvAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    f, t = "2026-09-18T00:00:00+09:00", "2026-09-19T00:00:00+09:00"
    assert admin.get("/admin/usage/stats", params={"days": 7, "from": f, "to": t}).status_code == 422
    assert admin.get("/admin/usage/stats", params={"from": f}).status_code == 422
    assert admin.get("/admin/usage/stats", params={"to": t}).status_code == 422
    assert admin.get("/admin/usage/stats",
                     params={"from": "2026-09-18T00:00:00", "to": t}).status_code == 422
    assert admin.get("/admin/usage/stats", params={"from": t, "to": f}).status_code == 422
    assert admin.get("/admin/usage/stats",
                     params={"from": "2026-01-01T00:00:00+09:00",
                             "to": "2027-01-02T00:00:00+09:00"}).status_code == 422

    # 既定（days 省略）は従来どおり30日・JST 暦日。実際に使った境界も返る。
    r = admin.get("/admin/usage/stats")
    assert r.status_code == 200, r.text
    period = r.json()["period"]
    b_start, b_start_date, b_end_date, b_end = store._usage_period_bounds(30)
    assert period["days"] == 30
    assert period["start"] == b_start_date.isoformat()
    assert period["end"] == b_end_date.isoformat()
    assert period["from"] == b_start.isoformat() and period["to"] == b_end.isoformat()


def test_usage_overview_forwards_from_to_to_stats_and_quality_runs():
    """`usage_overview` → `usage_stats` → `depth_quality_stats` の委譲で from/to が落ちない
    （末端の SQL が同じ期間で照会する＝`period` が指定どおりに返る）。"""
    if not _try_init():
        pytest.skip("DB down")
    f, t = "2026-09-18T03:00:00+09:00", "2026-09-18T18:00:00+09:00"
    overview = store.usage_overview(time_from=f, time_to=t)
    assert overview["period"]["from"] == f and overview["period"]["to"] == t
    stats = store.usage_stats(time_from=f, time_to=t)
    assert stats["period"]["from"] == f and stats["period"]["to"] == t
    assert stats["quality_runs"]["period"]["from"] == f
    assert stats["quality_runs"]["period"]["to"] == t


# ----- 品質採点の入口（POST /admin/usage/quality-runs）-----

def test_admin_usage_quality_run_records_condition_and_executed_period():
    """`condition`（閉集合）別に集計され、`rounds=0`（見直しを回さない条件）も受け付ける。
    母集団は実行期間（`executed_from`/`executed_to`）が照会期間に完全に含まれるランだけ
    （登録時刻ではない＝後日登録しても元の実行期間で取れる）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgqcadm{sfx}", f"UsgQcAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)

    jst = timezone(timedelta(hours=9))
    base = datetime.now(jst).replace(hour=6, minute=0, second=0, microsecond=0)
    switch = base + timedelta(hours=6)
    end = base + timedelta(hours=12)
    run_main, run_deep, run_out = f"qr-main-{sfx}", f"qr-deep-{sfx}", f"qr-out-{sfx}"

    def _post(run_id, condition, rounds, ef, et, **extra):
        body = {"rounds": rounds, "condition": condition, "run_id": run_id,
                "executed_from": ef.isoformat(), "executed_to": et.isoformat(), **extra}
        return admin.post("/admin/usage/quality-runs", json=body)

    try:
        r1 = _post(run_main, "main", 0, base, switch, correct=4, wrong_assertion=1)
        assert r1.status_code == 200, r1.text
        assert r1.json()["inserted"] is True
        # 同じ rounds=0 でも条件が違えば別行に分かれる。
        r2 = _post(run_deep, "depth2-standard", 0, base, switch, correct=5)
        assert r2.status_code == 200, r2.text
        # 実行期間が照会期間からはみ出すランは母集団に入らない。
        r3 = _post(run_out, "depth2-deep", 3, switch, end + timedelta(hours=1), correct=1)
        assert r3.status_code == 200, r3.text

        out = store.depth_quality_stats(time_from=base.isoformat(), time_to=end.isoformat())
        by_cond = {(r["condition"], r["rounds"]): r for r in out["by_rounds"]}
        assert by_cond[("main", 0)]["correct"] == 4
        assert by_cond[("main", 0)]["wrong_assertion"] == 1
        assert by_cond[("depth2-standard", 0)]["correct"] == 5
        assert ("depth2-deep", 3) not in by_cond, "実行期間が照会期間を超えるランが集計に入った"

        # 実行期間の終端が照会の上限ちょうど（executed_to == to）のランは含む。
        out_edge = store.depth_quality_stats(time_from=base.isoformat(), time_to=switch.isoformat())
        by_cond_edge = {(r["condition"], r["rounds"]): r for r in out_edge["by_rounds"]}
        assert by_cond_edge[("main", 0)]["runs"] == 1

        # 開始が照会の下限より前のランは含まない。
        out_late = store.depth_quality_stats(
            time_from=(base + timedelta(minutes=1)).isoformat(), time_to=end.isoformat())
        assert ("main", 0) not in {(r["condition"], r["rounds"]) for r in out_late["by_rounds"]}
    finally:
        _delete_quality_runs([run_main, run_deep, run_out])


def test_usage_depth_rounds_includes_quality_by_condition():
    """`usage_depth_rounds` は品質採点の条件別件数を `quality.by_rounds` として返す
    （`usage_overview` 側のキーは `quality_runs`）。通常の利用記録（巡）を固定したまま、
    品質採点だけが包含／非包含になる2期間で返却件数が変わる。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgqdadm{sfx}", f"UsgQdAdm{sfx}"
    uid = f"usgqd{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(uid, f"UsgQd{sfx}")
    world = f"qdworld{sfx}"
    provider = f"qdprov{sfx}"
    model = f"qd-model-{sfx}"
    run_id = f"qr-tool-{sfx}"

    jst = timezone(timedelta(hours=9))
    base = datetime.now(jst).replace(hour=6, minute=0, second=0, microsecond=0)
    mid = base + timedelta(hours=3)
    end = base + timedelta(hours=6)

    conv = store.create_conversation(user_id=uid, world=world, title=f"qd-{sfx}")
    cid = conv["id"]
    # 巡の記録は両方の期間に入る位置（base+1時間）に1件だけ置く＝差が出るのは品質採点だけ。
    _seed_round_turn(cid, at=base + timedelta(hours=1), provider=provider, model=model,
                     world=world, uid=uid, input_tokens=11, text="固定の質問")

    admin = _login(admin_uid, admin_pw)
    try:
        # 実行期間 [mid, end) の採点ラン——[base, end) には含まれるが [base, mid) には含まれない。
        r = admin.post("/admin/usage/quality-runs",
                       json={"rounds": 0, "condition": "main", "run_id": run_id, "correct": 3,
                             "executed_from": mid.isoformat(), "executed_to": end.isoformat()})
        assert r.status_code == 200, r.text

        wide = store.usage_depth_rounds(time_from=base.isoformat(), time_to=end.isoformat())
        narrow = store.usage_depth_rounds(time_from=base.isoformat(), time_to=mid.isoformat())
        # 巡の集計は両方で同じ（母集団を固定した）。
        assert wide["round_distribution"] == narrow["round_distribution"]
        wide_runs = {(x["condition"], x["rounds"]): x for x in wide["quality"]["by_rounds"]}
        narrow_runs = {(x["condition"], x["rounds"]): x for x in narrow["quality"]["by_rounds"]}
        assert wide_runs[("main", 0)]["correct"] == 3
        assert ("main", 0) not in narrow_runs, "実行期間が照会期間からはみ出すランが含まれた"

        # 利用統計チャットが読む概要（usage_overview）にも同じ品質採点が載る。
        ov = store.usage_overview(time_from=base.isoformat(), time_to=end.isoformat())
        ov_runs = {(x["condition"], x["rounds"]): x for x in ov["quality_runs"]["by_rounds"]}
        assert ov_runs[("main", 0)]["correct"] == 3
    finally:
        _delete_usage_events_by_model([model])
        _delete_quality_runs([run_id])


def test_admin_usage_quality_run_rejects_invalid_condition_and_period():
    """`condition` は閉集合のみ・実行期間はオフセット必須／`from < to`／最大365日。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgqvadm{sfx}", f"UsgQvAdm{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)
    ok = {"rounds": 1, "condition": "depth2-deep",
          "executed_from": "2026-09-18T00:00:00+09:00", "executed_to": "2026-09-19T00:00:00+09:00"}

    assert admin.post("/admin/usage/quality-runs",
                      json={**ok, "condition": "main-ish"}).status_code == 422
    assert admin.post("/admin/usage/quality-runs",
                      json={k: v for k, v in ok.items() if k != "condition"}).status_code == 422
    assert admin.post("/admin/usage/quality-runs",
                      json={k: v for k, v in ok.items() if k != "executed_from"}).status_code == 422
    assert admin.post("/admin/usage/quality-runs",
                      json={**ok, "executed_from": "2026-09-18T00:00:00"}).status_code == 422
    assert admin.post("/admin/usage/quality-runs",
                      json={**ok, "executed_from": ok["executed_to"],
                            "executed_to": ok["executed_from"]}).status_code == 422
    assert admin.post("/admin/usage/quality-runs",
                      json={**ok, "executed_from": "2026-01-01T00:00:00+09:00",
                            "executed_to": "2027-01-02T00:00:00+09:00"}).status_code == 422
    assert admin.post("/admin/usage/quality-runs",
                      json={**ok, "rounds": -1}).status_code == 422


def test_admin_usage_quality_run_duplicate_run_id_is_idempotent():
    """同じ `run_id` で `POST /admin/usage/quality-runs` を再送しても2行目を作らない
    （監査ログ書込み失敗後のリトライでの二重計上対策・登録と監査は同一トランザクション）。
    衝突時は `inserted=False` で新規登録が無かったことを伝える（`run_id` の値は返さない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgq68{sfx}", f"UsgQ68{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)
    run_id = f"qr-dup-{sfx}"
    body = {"rounds": 3, "condition": "depth2-deep", "correct": 2, "wrong_assertion": 0,
            "missing": 1, "regressed": 0, "unrated": 0, "run_id": run_id,
            "executed_from": "2026-09-18T00:00:00+09:00",
            "executed_to": "2026-09-19T00:00:00+09:00"}
    try:
        r1 = admin.post("/admin/usage/quality-runs", json=body)
        assert r1.status_code == 200, r1.text
        assert r1.json()["inserted"] is True
        assert "run_id" not in r1.json()
        r2 = admin.post("/admin/usage/quality-runs", json=body)
        assert r2.status_code == 200, r2.text
        assert r2.json()["inserted"] is False, "衝突時も inserted=True のまま（新規登録が無かったことが伝わらない）"
        with psycopg.connect(store._dsn()) as c:
            row = c.execute("SELECT COUNT(*) AS n FROM depth_quality_runs WHERE run_id=%s",
                            (run_id,)).fetchone()
        assert row[0] == 1, "同じ run_id の再送が2行目を作った（二重計上）"
    finally:
        _delete_quality_runs([run_id])


def test_admin_usage_quality_run_insert_and_audit_are_atomic():
    """登録（INSERT）が監査ログと同一トランザクションであること——監査 INSERT が失敗すれば
    `depth_quality_runs` 側の行もロールバックされる（片方だけ残らない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgq68b{sfx}", f"UsgQ68B{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)
    run_id = f"qr-atomic-{sfx}"

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    orig = store._audit_insert
    store._audit_insert = _boom
    try:
        r = admin.post("/admin/usage/quality-runs",
                       json={"rounds": 1, "condition": "depth2-deep", "correct": 1,
                             "run_id": run_id,
                             "executed_from": "2026-09-18T00:00:00+09:00",
                             "executed_to": "2026-09-19T00:00:00+09:00"})
        assert r.status_code == 500, r.text
    finally:
        store._audit_insert = orig
    with psycopg.connect(store._dsn()) as c:
        row = c.execute("SELECT COUNT(*) AS n FROM depth_quality_runs WHERE run_id=%s",
                        (run_id,)).fetchone()
    assert row[0] == 0, "監査ログ書込み失敗後も depth_quality_runs に行が残った（非アトミック）"


def test_admin_usage_quality_run_rejects_non_finite_cost():
    """`cost_usd` に inf/nan を登録できると、以後の利用統計（`SUM(cost_usd)`）が壊れて 500 に
    なる——router 側が `math.isfinite` で保存前に 422 で拒否する（schemas 側に pydantic 制約は
    付けない・422 ハンドラの 500 化を避けるため）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"usgq69{sfx}", f"UsgQ69{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    admin = _login(admin_uid, admin_pw)
    head = ('{"rounds": 1, "condition": "main", '
            '"executed_from": "2026-09-18T00:00:00+09:00", '
            '"executed_to": "2026-09-19T00:00:00+09:00", "cost_usd": ')

    r_inf = admin.post("/admin/usage/quality-runs",
                       content=(head + "Infinity}").encode(),
                       headers={"content-type": "application/json"})
    assert r_inf.status_code == 422, r_inf.text
    r_nan = admin.post("/admin/usage/quality-runs",
                       content=(head + "NaN}").encode(),
                       headers={"content-type": "application/json"})
    assert r_nan.status_code == 422, r_nan.text

    # 拒否されているので利用統計は壊れない。
    r_stats = admin.get("/admin/usage/stats?days=1")
    assert r_stats.status_code == 200, r_stats.text
