"""共有フォーク（SH-1・引き継いで質問）と再共有（SH-2・スナップショット更新）テスト
（docs/proposals/2026-08-23-共有フォーク.md）。

`test_sanitized_share.py`/`test_auth_sharing.py` と同じ流儀: store 層の関数を直接呼んで
データ・例外契約を固定し、HTTP ステータス（403/404/409）の対応づけはルータ経由（TestClient）で
確認する。要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from _common import _login, _try_init
from _test_users import register_test_uid
from sherpa import auth, store


def _future(days=7):
    return datetime.now(timezone.utc) + timedelta(days=days)


def _past(days=1):
    return datetime.now(timezone.utc) - timedelta(days=days)


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _mk_users(sfx: str, *names: str):
    uids = [f"{n}{sfx}" for n in names]
    for u in uids:
        store.upsert_user(u, email=f"{u}@ex.local", display_name=u.upper(),
                          password_hash=auth.hash_password("Fork-Refresh-Pw!9"), role="user")
        register_test_uid(u)
    return uids


def _mk_share(cid, owner, invitee, *, expires_at=None, sfx=""):
    th = hashlib.sha256(f"tok-{sfx}-{cid}-{invitee}".encode()).hexdigest()
    return store.create_share(cid, owner, th, expires_at if expires_at is not None else _future(), [invitee])


# ===================================================================================
# SH-1: フォーク（store 層）
# ===================================================================================

def test_fork_copies_visible_form_and_marks_own():
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fko", "fki")

    conv = store.create_conversation(user_id=owner, world="v1", title="フォーク元会話")
    cid = conv["id"]
    store.add_message(cid, "user", "TAXCALC の影響は？")
    store.add_message(cid, "assistant", "TAXCALC に影響します",
                      route={"lens": "impact", "path": ["a"]}, trace=[{"type": "node"}],
                      answer={"headline": "TAXCALC に影響します", "lens": "impact",
                              "sources": [{"doc_id": "kb1", "source": "KB"}]})

    sid = _mk_share(cid, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)

    new_cid = store.fork_received_share(invitee, wid)

    # フォーク先は invitee 自身の書込可会話。
    assert store.owns_conversation(invitee, new_cid) is True
    new_conv = store.get_conversation(new_cid)["conversation"]
    assert new_conv["user_id"] == invitee

    with psycopg.connect(store._dsn()) as c:
        row = c.execute(
            "SELECT origin, read_only, contains_personal_workspace, forked_from_share_id, "
            "  forked_from_user_id, forked_at FROM conversations WHERE id=%s", (new_cid,)).fetchone()
    assert row[0] == "own" and row[1] is False and row[2] is False
    assert row[3] == sid and row[4] == owner and row[5] is not None

    # 本文は「読者に見えている形」と一致（route/trace は NULL・伏字なし＝非個人ターン）。
    forked_msgs = store.get_conversation(new_cid)["messages"]
    assert [m["content"] for m in forked_msgs] == ["TAXCALC の影響は？", "TAXCALC に影響します"]
    asst = next(m for m in forked_msgs if m["role"] == "assistant")
    assert asst["route"] is None and asst["trace"] is None
    assert asst["answer"]["headline"] == "TAXCALC に影響します"

    # フォーク先へ通常どおりメッセージを追加できる。
    store.add_message(new_cid, "user", "続けて質問")
    assert len(store.get_conversation(new_cid)["messages"]) == 3

    # 元スナップショット/元会話・共有は不変。
    owner_view = store.get_conversation_for_read(owner, cid)
    assert len(owner_view["messages"]) == 2
    assert owner_view["messages"][1]["route"] == {"lens": "impact", "path": ["a"]}

    # 是正5: 通常共有（非サニタイズ）からのフォークは元 title をそのまま複製する（従来どおり）。
    assert new_conv["title"] == "フォーク元会話"


def test_fork_multiple_times_creates_new_conversation_each_call():
    """同じラッパーから何度でもフォークできる（冪等にしない）。"""
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fkmo", "fkmi")
    conv = store.create_conversation(user_id=owner, world="v1", title="複数回フォーク")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    sid = _mk_share(cid, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)

    c1 = store.fork_received_share(invitee, wid)
    c2 = store.fork_received_share(invitee, wid)
    assert c1 != c2
    assert store.owns_conversation(invitee, c1) and store.owns_conversation(invitee, c2)


def test_fork_redacted_turns_stay_redacted():
    """sanitize=true の共有からフォークしても、伏字ターンは伏字のまま（元の redaction を上書きしない）。"""
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fkro", "fkri")
    conv = store.create_conversation(user_id=owner, world="v1", title="my_salary.xlsx を要約して")
    cid = conv["id"]
    store.add_message(cid, "user", "my_salary.xlsx を要約して", personal=True)
    store.add_message(cid, "assistant", "個人ファイルによると年収は 900万 です",
                      answer={"headline": "個人ファイルによると年収は 900万 です",
                              "personal_sources": [{"doc_id": "my_salary.xlsx"}]}, personal=True)
    store.set_contains_personal_workspace(cid)
    snap = store.create_sanitized_snapshot(owner, cid)
    assert snap is not None

    sid = _mk_share(snap, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)
    new_cid = store.fork_received_share(invitee, wid)

    blob = str(store.get_conversation(new_cid)["messages"])
    assert "my_salary.xlsx" not in blob and "900万" not in blob
    assert store._REDACTED_TEXT in blob


def test_fork_denied_when_share_revoked():
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fkrvo", "fkrvi")
    conv = store.create_conversation(user_id=owner, world="v1", title="取消される共有")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    sid = _mk_share(cid, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)
    assert store.revoke_share(sid, owner) is True

    with pytest.raises(store.ForkNotAllowedError):
        store.fork_received_share(invitee, wid)


def test_fork_denied_when_share_expired():
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fkexo", "fkexi")
    conv = store.create_conversation(user_id=owner, world="v1", title="期限切れ共有")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    sid = _mk_share(cid, owner, invitee, expires_at=_past(), sfx=sfx)
    wid = store.accept_share(sid, invitee)

    with pytest.raises(store.ForkNotAllowedError):
        store.fork_received_share(invitee, wid)


def test_fork_denied_when_personal_blocked():
    """共有後に元会話が個人 workspace を参照した場合も拒否する（get_conversation_for_read と同じ posture）。"""
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fkpbo", "fkpbi")
    conv = store.create_conversation(user_id=owner, world="v1", title="後から個人参照")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    sid = _mk_share(cid, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)
    store.set_contains_personal_workspace(cid)   # 共有発行後に個人参照が付いたケース

    with pytest.raises(store.ForkNotAllowedError):
        store.fork_received_share(invitee, wid)


def test_fork_other_users_wrapper_is_not_found():
    """他人のラッパー id は自分のものとして扱えない（404 相当の LookupError）。"""
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee, other = _mk_users(sfx, "fkoo", "fkoi", "fkoO")
    conv = store.create_conversation(user_id=owner, world="v1", title="他人のラッパー")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    sid = _mk_share(cid, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)

    with pytest.raises(LookupError):
        store.fork_received_share(other, wid)


def test_fork_own_conversation_is_not_allowed():
    """受領共有ラッパーでない（自分の通常会話）は 403 相当。"""
    if not _try_init():
        return
    sfx = _sfx()
    (owner,) = _mk_users(sfx, "fkown")
    conv = store.create_conversation(user_id=owner, world="v1", title="通常の自分の会話")
    with pytest.raises(store.ForkNotAllowedError):
        store.fork_received_share(owner, conv["id"])


def test_fork_audit_failure_rolls_back_new_conversation(monkeypatch):
    """是正1: 複製と監査は同一トランザクション。監査 INSERT 失敗時は複製した会話・messages ごと
    ロールバックされる（`deleted_at` を立てる旧方式ではなく、行自体がそもそも存在しない）。"""
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fkao", "fkai")
    conv = store.create_conversation(user_id=owner, world="v1", title="監査失敗テスト")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    sid = _mk_share(cid, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)

    before_ids = {r["id"] for r in store.list_conversations(invitee)}

    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "_audit_insert", _boom)
    with pytest.raises(RuntimeError):
        store.fork_received_share(invitee, wid)
    monkeypatch.undo()

    after_ids = {r["id"] for r in store.list_conversations(invitee)}
    assert after_ids == before_ids, "監査失敗時に複製会話が一覧へ残っている（同一トランザクション化が効いていない）"

    # 一覧経由でなく、行そのものが存在しないことを直接確認する（soft delete との違いを担保）。
    with psycopg.connect(store._dsn()) as c:
        cnt = c.execute(
            "SELECT count(*) FROM conversations WHERE user_id=%s AND forked_from_share_id=%s",
            (invitee, sid)).fetchone()
    assert cnt[0] == 0, "監査失敗時に複製会話の行が commit されている"


def test_fork_sanitized_share_title_uses_first_non_redacted_user_message():
    """是正5: サニタイズ共有からのフォークは固定タイトルのままにせず、最初の伏字でない
    user 発言の先頭40文字を title にする（元会話の title は参照しない）。"""
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fktso", "fktsi")
    conv = store.create_conversation(user_id=owner, world="v1", title="my_salary.xlsx を要約して")
    cid = conv["id"]
    store.add_message(cid, "user", "my_salary.xlsx を要約して", personal=True)
    store.add_message(cid, "assistant", "個人ファイルによると年収は 900万 です",
                      answer={"headline": "個人ファイルによると年収は 900万 です"}, personal=True)
    long_question = "TAXCALC の影響範囲を教えてください" + "あ" * 40
    store.add_message(cid, "user", long_question)
    store.add_message(cid, "assistant", "TAXCALC に影響します", answer={"headline": "TAXCALC に影響します"})
    store.set_contains_personal_workspace(cid)
    snap = store.create_sanitized_snapshot(owner, cid)
    assert snap is not None

    sid = _mk_share(snap, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)
    new_cid = store.fork_received_share(invitee, wid)

    new_conv = store.get_conversation(new_cid)["conversation"]
    assert new_conv["title"] == long_question.strip()[:40]
    assert new_conv["title"] != store._SANITIZED_TITLE


def test_fork_sanitized_share_all_turns_redacted_uses_fallback_title():
    """是正5: サニタイズ共有の全ターンが伏字なら、フォールバック「引き継いだ会話」になる。"""
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fktfo", "fktfi")
    conv = store.create_conversation(user_id=owner, world="v1", title="my_salary.xlsx を要約して")
    cid = conv["id"]
    store.add_message(cid, "user", "my_salary.xlsx を要約して", personal=True)
    store.add_message(cid, "assistant", "個人ファイルによると年収は 900万 です",
                      answer={"headline": "個人ファイルによると年収は 900万 です"}, personal=True)
    store.set_contains_personal_workspace(cid)
    snap = store.create_sanitized_snapshot(owner, cid)
    sid = _mk_share(snap, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)
    new_cid = store.fork_received_share(invitee, wid)

    new_conv = store.get_conversation(new_cid)["conversation"]
    assert new_conv["title"] == "引き継いだ会話"


def test_forked_from_survives_share_row_deletion():
    """是正4: `forked_from_share_id` は共有行が消えると ON DELETE SET NULL で NULL に落ちるが、
    `forked_from_user_id`/`forked_at` は残るため、list_conversations の出所表示は
    （`forked_at IS NOT NULL` 判定に変えたことで）消えずに残る（share_id だけ null になる）。"""
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "ffso", "ffsi")
    conv = store.create_conversation(user_id=owner, world="v1", title="削除される共有元")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    sid = _mk_share(cid, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)
    new_cid = store.fork_received_share(invitee, wid)

    # 共有行の物理削除（`conversations.share_id`→`conversation_shares.id` の FK は SET NULL でない
    # ため、先に受領ラッパー行そのものを消して参照を外してから共有行を消す）。
    with psycopg.connect(store._dsn()) as c:
        c.execute("DELETE FROM conversations WHERE id=%s", (wid,))
        c.execute("DELETE FROM conversation_shares WHERE id=%s", (sid,))

    rows = store.list_conversations(invitee)
    row = next(r for r in rows if r["id"] == new_cid)
    assert row["forked_from"] is not None, "共有削除後に出所表示（forked_from）が消えている"
    assert row["forked_from"]["share_id"] is None
    assert row["forked_from"]["user_id"] == owner
    assert row["forked_from"]["at"] is not None


# ===================================================================================
# SH-1: フォーク（HTTP ステータス対応づけ）
# ===================================================================================

def test_fork_http_status_mapping():
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee, other = _mk_users(sfx, "fkho", "fkhi", "fkhO")
    conv = store.create_conversation(user_id=owner, world="v1", title="HTTPマッピング")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    sid = _mk_share(cid, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)

    invitee_c = _login(invitee, "Fork-Refresh-Pw!9")
    other_c = _login(other, "Fork-Refresh-Pw!9")

    # 他人のラッパー id → 404 or 403（本実装は 404）。
    r = other_c.post(f"/conversations/{wid}/fork")
    assert r.status_code in (403, 404), r.text

    # 正常系 → 200・新会話 id を返す。
    r = invitee_c.post(f"/conversations/{wid}/fork")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and isinstance(body["conversation_id"], int)

    # 取消後 → 403。
    assert store.revoke_share(sid, owner) is True
    r2 = invitee_c.post(f"/conversations/{wid}/fork")
    assert r2.status_code == 403, r2.text


# ===================================================================================
# SH-2: 再共有（store 層）
# ===================================================================================

def test_refresh_updates_content_swaps_wrapper_source_soft_deletes_old():
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "rfo", "rfi")
    conv = store.create_conversation(user_id=owner, world="v1", title="再共有元会話")
    cid = conv["id"]
    store.add_message(cid, "user", "最初の質問")
    store.add_message(cid, "assistant", "最初の回答")

    old_snap = store.create_sanitized_snapshot(owner, cid)
    th = hashlib.sha256(f"reftok-{sfx}".encode()).hexdigest()
    sid = store.create_share(old_snap, owner, th, _future(), [invitee])
    wid = store.accept_share(sid, invitee)

    # 元会話にターンを追加してから refresh。
    store.add_message(cid, "user", "追加の質問")
    store.add_message(cid, "assistant", "追加の回答")

    result = store.refresh_sanitized_share(owner, sid)
    assert result["old_snapshot_id"] == old_snap
    new_snap = result["new_snapshot_id"]
    assert new_snap != old_snap

    # 招待者が開くと新ターンが見える。
    r = store.get_conversation_for_read(invitee, wid)
    assert [m["content"] for m in r["messages"]] == [
        "最初の質問", "最初の回答", "追加の質問", "追加の回答"]

    # share_id/token/期限は不変（同じ token で解決でき、conversation_id だけ差し替わる）。
    resolved = store.resolve_share_by_token(th)
    assert resolved["id"] == sid and resolved["conversation_id"] == new_snap

    with psycopg.connect(store._dsn()) as c:
        old_row = c.execute(
            "SELECT deleted_at FROM conversations WHERE id=%s", (old_snap,)).fetchone()
        wrapper_row = c.execute(
            "SELECT source_conversation_id FROM conversations WHERE id=%s", (wid,)).fetchone()
        share_row = c.execute(
            "SELECT token_hash, expires_at FROM conversation_shares WHERE id=%s", (sid,)).fetchone()
    assert old_row[0] is not None, "旧 snapshot が soft delete されていない"
    assert wrapper_row[0] == new_snap, "受領ラッパーの source_conversation_id が新 snapshot に付け替わっていない"
    assert share_row[0] == th, "token_hash が refresh で変わった"


def test_refresh_non_sanitized_share_raises_not_sanitized():
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "rfnso", "rfnsi")
    conv = store.create_conversation(user_id=owner, world="v1", title="通常共有")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    sid = _mk_share(cid, owner, invitee, sfx=sfx)

    with pytest.raises(store.ShareNotSanitizedError):
        store.refresh_sanitized_share(owner, sid)


def test_refresh_by_non_owner_raises_permission_error():
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "rfpo", "rfpi")
    conv = store.create_conversation(user_id=owner, world="v1", title="所有者以外")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    snap = store.create_sanitized_snapshot(owner, cid)
    sid = _mk_share(snap, owner, invitee, sfx=sfx)

    with pytest.raises(PermissionError):
        store.refresh_sanitized_share(invitee, sid)


def test_refresh_missing_share_raises_lookup_error():
    if not _try_init():
        return
    with pytest.raises(LookupError):
        store.refresh_sanitized_share("nobody", 2_000_000_000)


def test_refresh_after_source_conversation_deleted_raises_lookup_error():
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "rfdo", "rfdi")
    conv = store.create_conversation(user_id=owner, world="v1", title="元会話削除")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    snap = store.create_sanitized_snapshot(owner, cid)
    sid = _mk_share(snap, owner, invitee, sfx=sfx)
    store.accept_share(sid, invitee)

    # ラッパーは snapshot を source にしているため、元会話 cid には生きたラッパーが無く物理削除される。
    assert store.delete_conversation(cid, owner) is True
    assert store.get_conversation(cid) is None

    with pytest.raises(LookupError):
        store.refresh_sanitized_share(owner, sid)


def test_list_shares_for_conversation_includes_sanitized_flag_and_invitees():
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee1, invitee2 = _mk_users(sfx, "lso", "lsi1", "lsi2")
    conv = store.create_conversation(user_id=owner, world="v1", title="一覧対象会話")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")

    sid_plain = _mk_share(cid, owner, invitee1, sfx=sfx + "p")
    snap = store.create_sanitized_snapshot(owner, cid)
    sid_sanitized = _mk_share(snap, owner, invitee2, sfx=sfx + "s")

    rows = store.list_shares_for_conversation(owner, cid)
    by_id = {r["share_id"]: r for r in rows}
    assert set(by_id) == {sid_plain, sid_sanitized}
    assert by_id[sid_plain]["sanitized"] is False
    assert by_id[sid_sanitized]["sanitized"] is True
    assert [i["uid"] for i in by_id[sid_plain]["invitees"]] == [invitee1]
    assert [i["uid"] for i in by_id[sid_sanitized]["invitees"]] == [invitee2]


def test_list_shares_reflects_current_snapshot_after_refresh():
    """refresh 後、一覧は「現在の」snapshot 経由で引き続き元会話を対象として拾える（二重に出ない）。"""
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "lsro", "lsri")
    conv = store.create_conversation(user_id=owner, world="v1", title="refresh後の一覧")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    snap = store.create_sanitized_snapshot(owner, cid)
    sid = _mk_share(snap, owner, invitee, sfx=sfx)

    store.refresh_sanitized_share(owner, sid)
    rows = store.list_shares_for_conversation(owner, cid)
    assert len(rows) == 1 and rows[0]["share_id"] == sid and rows[0]["sanitized"] is True


# ===================================================================================
# SH-2: 再共有・一覧（HTTP ステータス対応づけ）
# ===================================================================================

def test_refresh_http_status_mapping():
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "rfho", "rfhi")
    conv = store.create_conversation(user_id=owner, world="v1", title="HTTP refresh")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")

    owner_c = _login(owner, "Fork-Refresh-Pw!9")
    invitee_c = _login(invitee, "Fork-Refresh-Pw!9")

    # 通常共有 → 409。
    sid_plain = _mk_share(cid, owner, invitee, sfx=sfx + "p")
    r = owner_c.post(f"/conversation-shares/{sid_plain}/refresh")
    assert r.status_code == 409, r.text

    # 存在しない share_id → 404。
    r = owner_c.post("/conversation-shares/2000000001/refresh")
    assert r.status_code == 404, r.text

    # サニタイズ共有・所有者以外 → 403。
    snap = store.create_sanitized_snapshot(owner, cid)
    sid_sanitized = _mk_share(snap, owner, invitee, sfx=sfx + "s")
    r = invitee_c.post(f"/conversation-shares/{sid_sanitized}/refresh")
    assert r.status_code == 403, r.text

    # 所有者・サニタイズ共有 → 200。
    r = owner_c.post(f"/conversation-shares/{sid_sanitized}/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["share_id"] == sid_sanitized and body["refreshed_at"]


def test_conversation_shares_list_http_owner_only():
    if not _try_init():
        return
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "lsho", "lshi")
    conv = store.create_conversation(user_id=owner, world="v1", title="HTTP一覧")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    _mk_share(cid, owner, invitee, sfx=sfx)

    owner_c = _login(owner, "Fork-Refresh-Pw!9")
    invitee_c = _login(invitee, "Fork-Refresh-Pw!9")

    r = owner_c.get(f"/conversations/{cid}/shares")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1 and rows[0]["sanitized"] is False
    assert rows[0]["invitees"][0]["uid"] == invitee

    r2 = invitee_c.get(f"/conversations/{cid}/shares")
    assert r2.status_code == 403, r2.text
