"""共有フォーク（引き継いで質問）と再共有（スナップショット更新）の契約。

store 層の関数でデータ・例外契約を固定し、HTTP ステータス（403/404/409）の対応づけはルータ経由で確認する。
要 Postgres。
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from _common import _login, _try_init
from _test_users import register_test_uid
from _store_helpers import get_conversation
from sherpa import auth, store

_PW = "Fork-Refresh-Pw!9"


@pytest.fixture(autouse=True)
def _db():
    _try_init()


def _future(days=7):
    return datetime.now(timezone.utc) + timedelta(days=days)


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _mk_users(sfx: str, *names: str):
    uids = [f"{n}{sfx}" for n in names]
    for u in uids:
        store.upsert_user(u, email=f"{u}@ex.local", display_name=u.upper(),
                          password_hash=auth.hash_password(_PW), role="user")
        register_test_uid(u)
    return uids


def _conv(owner: str, title: str, *msgs: str) -> int:
    cid = store.create_conversation(user_id=owner, world="v1", title=title)["id"]
    for i, m in enumerate(msgs):
        store.add_message(cid, "user" if i % 2 == 0 else "assistant", m)
    return cid


def _mk_share(cid, owner, invitee, *, sfx=""):
    th = hashlib.sha256(f"tok-{sfx}-{cid}-{invitee}".encode()).hexdigest()
    return store.create_share(cid, owner, th, _future(), [invitee])


def _personal_conv(owner: str, *, extra_turn: str | None = None) -> int:
    """個人ファイル参照ターン（サニタイズで伏字になる）だけの会話。extra_turn があれば通常ターンを足す。"""
    cid = store.create_conversation(user_id=owner, world="v1", title="my_salary.xlsx を要約して")["id"]
    store.add_message(cid, "user", "my_salary.xlsx を要約して", personal=True)
    store.add_message(cid, "assistant", "個人ファイルによると年収は 900万 です",
                      answer={"headline": "個人ファイルによると年収は 900万 です",
                              "personal_sources": [{"doc_id": "my_salary.xlsx"}]}, personal=True)
    if extra_turn:
        store.add_message(cid, "user", extra_turn)
        store.add_message(cid, "assistant", "TAXCALC に影響します", answer={"headline": "TAXCALC に影響します"})
    store.set_contains_personal_workspace(cid)
    return cid


def _row(q: str, *args):
    with psycopg.connect(store._dsn()) as c:
        return c.execute(q, args).fetchone()


# ===== フォーク（store 層）=====

def test_fork_copies_visible_form_and_marks_own():
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fko", "fki")
    cid = _conv(owner, "フォーク元会話", "TAXCALC の影響は？")
    store.add_message(cid, "assistant", "TAXCALC に影響します",
                      route={"lens": "impact", "path": ["a"]}, trace=[{"type": "node"}],
                      answer={"headline": "TAXCALC に影響します", "lens": "impact",
                              "sources": [{"doc_id": "kb1", "source": "KB"}]})
    sid = _mk_share(cid, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)

    new_cid = store.fork_received_share(invitee, wid)
    assert store.owns_conversation(invitee, new_cid) is True
    new_conv = get_conversation(new_cid)["conversation"]
    assert new_conv["user_id"] == invitee
    assert new_conv["title"] == "フォーク元会話"   # 通常共有（非サニタイズ）は元 title をそのまま複製する

    row = _row("SELECT origin, read_only, contains_personal_workspace, forked_from_share_id, "
               "  forked_from_user_id, forked_at FROM conversations WHERE id=%s", new_cid)
    assert row[0] == "own" and row[1] is False and row[2] is False
    assert row[3] == sid and row[4] == owner and row[5] is not None

    forked_msgs = get_conversation(new_cid)["messages"]
    assert [m["content"] for m in forked_msgs] == ["TAXCALC の影響は？", "TAXCALC に影響します"]
    asst = next(m for m in forked_msgs if m["role"] == "assistant")
    assert asst["route"] is None and asst["trace"] is None
    assert asst["answer"]["headline"] == "TAXCALC に影響します"

    store.add_message(new_cid, "user", "続けて質問")
    assert len(get_conversation(new_cid)["messages"]) == 3

    # 元会話・共有は不変。
    owner_view = store.get_conversation_for_read(owner, cid)
    assert len(owner_view["messages"]) == 2
    assert owner_view["messages"][1]["route"] == {"lens": "impact", "path": ["a"]}

    # 同じラッパーから何度でもフォークできる（冪等にしない）。
    again = store.fork_received_share(invitee, wid)
    assert again != new_cid and store.owns_conversation(invitee, again)


def test_fork_of_all_redacted_sanitized_share_stays_redacted_with_fallback_title():
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fkro", "fkri")
    snap = store.create_sanitized_snapshot(owner, _personal_conv(owner))
    assert snap is not None
    wid = store.accept_share(_mk_share(snap, owner, invitee, sfx=sfx), invitee)
    new_cid = store.fork_received_share(invitee, wid)

    blob = str(get_conversation(new_cid)["messages"])
    assert "my_salary.xlsx" not in blob and "900万" not in blob
    assert store._REDACTED_TEXT in blob
    assert get_conversation(new_cid)["conversation"]["title"] == "引き継いだ会話"


def test_fork_sanitized_share_title_uses_first_non_redacted_user_message():
    """サニタイズ共有からのフォークは、最初の伏字でない user 発言の先頭 40 文字を title にする。"""
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fktso", "fktsi")
    long_question = "TAXCALC の影響範囲を教えてください" + "あ" * 40
    snap = store.create_sanitized_snapshot(owner, _personal_conv(owner, extra_turn=long_question))
    assert snap is not None
    wid = store.accept_share(_mk_share(snap, owner, invitee, sfx=sfx), invitee)
    new_cid = store.fork_received_share(invitee, wid)

    title = get_conversation(new_cid)["conversation"]["title"]
    assert title == long_question.strip()[:40]
    assert title != store._SANITIZED_TITLE


@pytest.mark.parametrize("how", ["revoked", "expired", "personal_blocked"])
def test_fork_denied(how):
    """取消・期限切れ・共有後に元会話が個人 workspace を参照した場合は ForkNotAllowedError。"""
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fkd", "fki")
    cid = _conv(owner, "拒否される共有", "質問")
    sid = _mk_share(cid, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)   # 受領は有効なうちに済ませる
    if how == "revoked":
        assert store.revoke_share(sid, owner) is True
    elif how == "expired":
        with psycopg.connect(store._dsn()) as c:
            c.execute("UPDATE conversation_shares SET expires_at=%s WHERE id=%s",
                      (datetime.now(timezone.utc) - timedelta(days=1), sid))
            c.commit()
    else:
        store.set_contains_personal_workspace(cid)
    with pytest.raises(store.ForkNotAllowedError):
        store.fork_received_share(invitee, wid)


def test_fork_other_users_wrapper_not_found_and_own_conversation_not_allowed():
    sfx = _sfx()
    owner, invitee, other = _mk_users(sfx, "fkoo", "fkoi", "fkoO")
    cid = _conv(owner, "他人のラッパー", "質問")
    wid = store.accept_share(_mk_share(cid, owner, invitee, sfx=sfx), invitee)
    with pytest.raises(LookupError):
        store.fork_received_share(other, wid)
    with pytest.raises(store.ForkNotAllowedError):   # 受領共有ラッパーでない通常会話
        store.fork_received_share(owner, cid)


def test_fork_audit_failure_rolls_back_new_conversation(monkeypatch):
    """複製と監査は同一トランザクション: 監査失敗時は複製した会話の行自体が存在しない（soft delete でない）。"""
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "fkao", "fkai")
    cid = _conv(owner, "監査失敗テスト", "質問")
    sid = _mk_share(cid, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)
    before_ids = {r["id"] for r in store.list_conversations(invitee)}

    def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "_audit_insert", _boom)
    with pytest.raises(RuntimeError):
        store.fork_received_share(invitee, wid)
    monkeypatch.undo()

    assert {r["id"] for r in store.list_conversations(invitee)} == before_ids
    cnt = _row("SELECT count(*) FROM conversations WHERE user_id=%s AND forked_from_share_id=%s", invitee, sid)
    assert cnt[0] == 0


def test_forked_from_survives_share_row_deletion():
    """共有行が消えると forked_from_share_id は SET NULL だが、forked_from_user_id/forked_at は残り、
    出所表示（forked_from）は消えずに share_id だけ null になる。"""
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "ffso", "ffsi")
    cid = _conv(owner, "削除される共有元", "質問")
    sid = _mk_share(cid, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)
    new_cid = store.fork_received_share(invitee, wid)

    # conversations.share_id の FK は SET NULL でないため、先に受領ラッパー行を消して参照を外す。
    with psycopg.connect(store._dsn()) as c:
        c.execute("DELETE FROM conversations WHERE id=%s", (wid,))
        c.execute("DELETE FROM conversation_shares WHERE id=%s", (sid,))

    row = next(r for r in store.list_conversations(invitee) if r["id"] == new_cid)
    assert row["forked_from"] is not None
    assert row["forked_from"]["share_id"] is None
    assert row["forked_from"]["user_id"] == owner
    assert row["forked_from"]["at"] is not None


def test_fork_http_status_mapping():
    sfx = _sfx()
    owner, invitee, other = _mk_users(sfx, "fkho", "fkhi", "fkhO")
    cid = _conv(owner, "HTTPマッピング", "質問")
    sid = _mk_share(cid, owner, invitee, sfx=sfx)
    wid = store.accept_share(sid, invitee)
    invitee_c = _login(invitee, _PW)
    other_c = _login(other, _PW)

    r = other_c.post(f"/conversations/{wid}/fork")
    assert r.status_code in (403, 404), r.text

    r = invitee_c.post(f"/conversations/{wid}/fork")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and isinstance(r.json()["conversation_id"], int)

    assert store.revoke_share(sid, owner) is True
    r2 = invitee_c.post(f"/conversations/{wid}/fork")
    assert r2.status_code == 403, r2.text


# ===== 再共有（store 層）=====

def test_refresh_updates_content_swaps_wrapper_source_soft_deletes_old():
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "rfo", "rfi")
    cid = _conv(owner, "再共有元会話", "最初の質問", "最初の回答")
    old_snap = store.create_sanitized_snapshot(owner, cid)
    th = hashlib.sha256(f"reftok-{sfx}".encode()).hexdigest()
    sid = store.create_share(old_snap, owner, th, _future(), [invitee])
    wid = store.accept_share(sid, invitee)
    store.add_message(cid, "user", "追加の質問")
    store.add_message(cid, "assistant", "追加の回答")

    result = store.refresh_sanitized_share(owner, sid)
    assert result["old_snapshot_id"] == old_snap
    new_snap = result["new_snapshot_id"]
    assert new_snap != old_snap

    r = store.get_conversation_for_read(invitee, wid)
    assert [m["content"] for m in r["messages"]] == ["最初の質問", "最初の回答", "追加の質問", "追加の回答"]

    resolved = store.resolve_share_by_token(th)   # token は不変・conversation_id だけ差し替わる
    assert resolved["id"] == sid and resolved["conversation_id"] == new_snap
    assert _row("SELECT deleted_at FROM conversations WHERE id=%s", old_snap)[0] is not None
    assert _row("SELECT source_conversation_id FROM conversations WHERE id=%s", wid)[0] == new_snap
    assert _row("SELECT token_hash FROM conversation_shares WHERE id=%s", sid)[0] == th

    # refresh 後も一覧は現在の snapshot 経由で元会話を対象に拾い、二重に出ない。
    rows = store.list_shares_for_conversation(owner, cid)
    assert len(rows) == 1 and rows[0]["share_id"] == sid and rows[0]["sanitized"] is True


def test_refresh_audit_failure_rolls_back_refresh(monkeypatch):
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "rfao", "rfai")
    cid = _conv(owner, "再共有監査", "最初の質問")
    old_snap = store.create_sanitized_snapshot(owner, cid)
    th = hashlib.sha256(f"refaud-{sfx}".encode()).hexdigest()
    sid = store.create_share(old_snap, owner, th, _future(), [invitee])
    store.add_message(cid, "user", "追加の質問")

    def boom(*a, **kw):
        raise RuntimeError("audit down")
    monkeypatch.setattr(store, "_audit_insert", boom)
    with pytest.raises(RuntimeError):
        store.refresh_sanitized_share(owner, sid, audit={"ip_hash": None, "user_agent": ""})
    assert store.resolve_share_by_token(th)["conversation_id"] == old_snap


def test_refresh_rejections():
    """通常共有は ShareNotSanitizedError・所有者以外は PermissionError・存在しない共有／元会話削除後は LookupError。"""
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "rfr", "rfi")
    cid = _conv(owner, "通常共有", "質問")
    with pytest.raises(store.ShareNotSanitizedError):
        store.refresh_sanitized_share(owner, _mk_share(cid, owner, invitee, sfx=sfx))

    snap = store.create_sanitized_snapshot(owner, cid)
    sid = _mk_share(snap, owner, invitee, sfx=sfx + "s")
    with pytest.raises(PermissionError):
        store.refresh_sanitized_share(invitee, sid)
    with pytest.raises(LookupError):
        store.refresh_sanitized_share("nobody", 2_000_000_000)

    store.accept_share(sid, invitee)
    # ラッパーは snapshot を source にしているため、元会話 cid に生きたラッパーは無く物理削除される。
    assert store.delete_conversation(cid, owner) is True
    assert get_conversation(cid) is None
    with pytest.raises(LookupError):
        store.refresh_sanitized_share(owner, sid)


def test_list_shares_for_conversation_includes_sanitized_flag_and_invitees():
    sfx = _sfx()
    owner, invitee1, invitee2 = _mk_users(sfx, "lso", "lsi1", "lsi2")
    cid = _conv(owner, "一覧対象会話", "質問")
    sid_plain = _mk_share(cid, owner, invitee1, sfx=sfx + "p")
    snap = store.create_sanitized_snapshot(owner, cid)
    sid_sanitized = _mk_share(snap, owner, invitee2, sfx=sfx + "s")

    by_id = {r["share_id"]: r for r in store.list_shares_for_conversation(owner, cid)}
    assert set(by_id) == {sid_plain, sid_sanitized}
    assert by_id[sid_plain]["sanitized"] is False
    assert by_id[sid_sanitized]["sanitized"] is True
    assert [i["uid"] for i in by_id[sid_plain]["invitees"]] == [invitee1]
    assert [i["uid"] for i in by_id[sid_sanitized]["invitees"]] == [invitee2]


# ===== 再共有・一覧（HTTP）=====

def test_refresh_http_status_mapping():
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "rfho", "rfhi")
    cid = _conv(owner, "HTTP refresh", "質問")
    owner_c = _login(owner, _PW)
    invitee_c = _login(invitee, _PW)

    sid_plain = _mk_share(cid, owner, invitee, sfx=sfx + "p")
    assert owner_c.post(f"/conversation-shares/{sid_plain}/refresh").status_code == 409
    assert owner_c.post("/conversation-shares/2000000001/refresh").status_code == 404

    snap = store.create_sanitized_snapshot(owner, cid)
    sid_sanitized = _mk_share(snap, owner, invitee, sfx=sfx + "s")
    assert invitee_c.post(f"/conversation-shares/{sid_sanitized}/refresh").status_code == 403

    r = owner_c.post(f"/conversation-shares/{sid_sanitized}/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["share_id"] == sid_sanitized and body["refreshed_at"]


def test_conversation_shares_list_http_owner_only():
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "lsho", "lshi")
    cid = _conv(owner, "HTTP一覧", "質問")
    _mk_share(cid, owner, invitee, sfx=sfx)

    r = _login(owner, _PW).get(f"/conversations/{cid}/shares")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1 and rows[0]["sanitized"] is False
    assert rows[0]["invitees"][0]["uid"] == invitee
    assert _login(invitee, _PW).get(f"/conversations/{cid}/shares").status_code == 403
