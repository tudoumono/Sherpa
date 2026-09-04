"""利用統計 API（2026-07-02-利用統計とホーム掲示板.md Feature 1・admin 専用）テスト。

- admin ゲート（非 admin → 403、未ログイン → 401）
- 集計値の正しさ（seed した会話/メッセージどおり: ターン数・会話数・lens内訳・personal_turns・監査由来）
- メッセージ本文・会話タイトルが一切含まれない（プライバシー）
- 期間境界（days でフィルタされる）
- 閲覧時に admin.usage_viewed が監査される

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import time

import psycopg
import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
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
