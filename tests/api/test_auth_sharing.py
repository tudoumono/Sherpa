"""認証・会話共有の store 層＋auth ヘルパの契約（リンク→クリック→受領ラッパー→本文読取→越境不可→取消）。要 Postgres。"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from _test_users import register_test_uid
from _store_helpers import get_conversation
from sherpa import auth, store


@pytest.fixture(autouse=True)
def _db():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")


def _sfx() -> str:
    return str(int(time.time() * 1000))


def _future(days=7):
    return datetime.now(timezone.utc) + timedelta(days=days)


def _mk_users(sfx: str, *names: str):
    uids = [f"{n}{sfx}" for n in names]
    for u in uids:
        store.upsert_user(u, email=f"{u}@ex.local", display_name=u.upper(),
                          password_hash=auth.hash_password("pw"), role="user")
        register_test_uid(u)
    return uids


def _conv(owner: str, title: str, *msgs: str) -> int:
    cid = store.create_conversation(user_id=owner, world="v1", title=title)["id"]
    for i, m in enumerate(msgs):
        store.add_message(cid, "user" if i % 2 == 0 else "assistant", m)
    return cid


def _tok(tag: str, sfx: str) -> str:
    return hashlib.sha256((f"{tag}-{sfx}").encode()).hexdigest()


def _sql_one(q: str, *args):
    with psycopg.connect(store._dsn()) as c:
        return c.execute(q, args).fetchone()


def _assistant(view: dict, content: str) -> dict:
    return next(m for m in view["messages"] if m["role"] == "assistant" and m["content"] == content)


def test_password_hash_roundtrip():
    h = auth.hash_password("hunter2")
    assert h.startswith("pbkdf2_sha256$")
    assert auth.verify_password("hunter2", h) is True
    assert auth.verify_password("wrong", h) is False
    assert auth.verify_password("x", None) is False and auth.verify_password("x", "garbage") is False


def test_share_click_history_and_revoke():
    sfx = _sfx()
    sato, tan, other = _mk_users(sfx, "sato", "tan", "oth")
    cid = _conv(sato, "請求機能の影響調査", "請求はどう算出？", "税計算ルールで算出します")
    th = _tok("tok", sfx)
    sid = store.create_share(cid, sato, th, _future(), [tan])

    assert store.resolve_share_by_token(th)["active"] is True
    assert store.is_invited(sid, tan) is True and store.is_invited(sid, other) is False

    wid = store.accept_share(sid, tan)
    assert store.accept_share(sid, tan) == wid   # 同 uid×share は 1 行（冪等）

    rec = [r for r in store.list_conversations(tan) if r["origin"] == "received_share"]
    assert len(rec) == 1
    assert rec[0]["shared_by_user_id"] == sato and rec[0]["shared_by_name"] == sato.upper()
    assert rec[0]["share_status"] == "active" and rec[0]["read_only"] is True

    r = store.get_conversation_for_read(tan, wid)
    assert len(r["messages"]) == 2 and r["conversation"]["id"] == wid
    assert r["messages"][0]["content"] == "請求はどう算出？"

    assert store.get_conversation_for_read(other, wid) is None
    assert store.owns_conversation(tan, wid) is False
    assert store.owns_conversation(sato, cid) is True

    assert store.revoke_share(sid, sato) is True
    r2 = store.get_conversation_for_read(tan, wid)
    assert r2["share_status"] == "unavailable" and r2["messages"] == []
    assert [x for x in store.list_conversations(tan) if x["id"] == wid][0]["share_status"] == "revoked"


def test_received_share_strips_internal_info_but_owner_still_sees_it():
    """受領共有は答え/出典だけ見せ、route/trace・answer.route・answer.usage・answer.question は伏せる。
    所有者本人には引き続き見える（伏せるのは受領共有の read path だけ）。"""
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "rvo", "rvi")
    cid = _conv(owner, "内部情報を含む調査", "TAXCALC の影響は？")

    trace = [{"type": "node", "id": "tool-grep", "kind": "tool", "label": "資料を検索（語句そのまま）",
              "detail": "「TAX-RATE」", "status": "done"}]
    route = {"lens": "qa", "path": ["4期/03_開発/01_ソース/TAXCALC.cbl"], "reason": "grep hit"}
    store.add_message(cid, "assistant", "A-route-trace", route=route, trace=trace)
    # chat_service._finalize は answer envelope 自身にも route を埋め込む（route 列は付けない）。
    store.add_message(cid, "assistant", "A-answer-route",
                      answer={"headline": "h", "lens": "impact", "sources": [],
                              "route": {"lens": "impact", "reason": "AI判定（意図分類）", "path": ["関係を確認"]}})
    store.add_message(cid, "assistant", "A-usage",
                      answer={"headline": "h", "lens": "impact", "sources": [],
                              "usage": {"provider": "codex", "model": "gpt-5.5", "input_tokens": 341026,
                                        "cached_input_tokens": 244864, "output_tokens": 12318,
                                        "reasoning_output_tokens": 9392}})
    question = {"interaction_id": "ask-cafef00d", "mode": "single", "prompt": "どの調べ方をしますか？",
                "options": [{"id": "impact", "label": "影響範囲", "description": ""}],
                "allow_free_text": False, "original_message": "税率を変えたら夜間バッチが落ちる？"}
    store.add_message(cid, "assistant", "A-question", lens="clarify",
                      answer={"lens": "clarify", "question": question})

    sid = store.create_share(cid, owner, _tok("tok-rv", sfx), _future(), [invitee])
    wid = store.accept_share(sid, invitee)
    view = store.get_conversation_for_read(invitee, wid)
    assert len(view["messages"]) == 5

    m = _assistant(view, "A-route-trace")
    assert m["route"] is None and m["trace"] is None

    m = _assistant(view, "A-answer-route")
    assert m["answer"]["headline"] == "h"
    assert "route" not in m["answer"], "answer.route が受領共有に漏れている"

    m = _assistant(view, "A-usage")
    assert "usage" not in m["answer"], "answer.usage が受領共有に漏れている"
    assert "341026" not in str(m["answer"])

    m = _assistant(view, "A-question")
    assert "question" not in (m["answer"] or {})
    assert m["answer"].get("lens") == "clarify"
    assert m["route"] is None and m["trace"] is None
    blob = str(m["answer"])
    assert "ask-cafef00d" not in blob and "影響範囲" not in blob

    owner_view = store.get_conversation_for_read(owner, cid)
    assert _assistant(owner_view, "A-route-trace")["trace"] == trace
    assert _assistant(owner_view, "A-route-trace")["route"] == route
    assert _assistant(owner_view, "A-answer-route")["answer"]["route"]["lens"] == "impact"
    assert _assistant(owner_view, "A-usage")["answer"]["usage"]["input_tokens"] == 341026
    assert _assistant(owner_view, "A-question")["answer"]["question"]["interaction_id"] == "ask-cafef00d"


def test_share_default_expiry_and_legacy_null_row_expires_at_created_plus_30d():
    """create_share(None)＝作成+30日。expires_at IS NULL の旧行は created_at+30日で読み取り時に失効
    （resolve_share_by_token・受領側の一覧/本文読みの両経路）。"""
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "ulo", "uli")
    cid = _conv(owner, "既定期限共有", "質問", "回答")
    th = _tok("ultok", sfx)
    sid = store.create_share(cid, owner, th, None, [invitee])
    assert store.resolve_share_by_token(th)["active"] is True
    with psycopg.connect(store._dsn()) as c:
        row = c.execute("SELECT expires_at, created_at + interval '30 days' AS want "
                        "FROM conversation_shares WHERE id=%s", (sid,)).fetchone()
        assert row[0] == row[1]
        # 旧行（NULL）へ戻し、created_at を 29 日前 → まだ有効。
        c.execute("UPDATE conversation_shares SET expires_at=NULL, "
                  "created_at=now() - interval '29 days' WHERE id=%s", (sid,))
        c.commit()
    assert store.resolve_share_by_token(th)["active"] is True
    wid = store.accept_share(sid, invitee)
    assert [r for r in store.list_conversations(invitee) if r["id"] == wid][0]["share_status"] == "active"
    assert len(store.get_conversation_for_read(invitee, wid)["messages"]) == 2
    assert store.list_shares_for_conversation(owner, cid)[0]["expires_at"] is not None

    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE conversation_shares SET created_at=now() - interval '31 days' WHERE id=%s", (sid,))
        c.commit()
    assert store.resolve_share_by_token(th)["active"] is False
    assert [r for r in store.list_conversations(invitee) if r["id"] == wid][0]["share_status"] == "expired"
    r2 = store.get_conversation_for_read(invitee, wid)
    assert r2["share_status"] == "unavailable" and r2["messages"] == []


def test_accept_share_rechecks_revoked_inside_transaction():
    """事前判定の後に所有者が取消した場合、accept_share はラッパー・accepted_at・last_used_at・
    `share.accepted` 監査のいずれも書かず拒否する。"""
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "rco", "rci")
    cid = _conv(owner, "受領再確認")
    th = _tok("rctok", sfx)
    sid = store.create_share(cid, owner, th, None, [invitee])

    assert store.resolve_share_by_token(th)["active"] is True
    assert store.is_invited(sid, invitee) is True
    assert store.revoke_share(sid, owner) is True
    with pytest.raises(store.ShareUnavailableError):
        store.accept_share(sid, invitee, audit={"ip_hash": None, "user_agent": ""})

    assert [c for c in store.list_conversations(invitee) if c["origin"] == "received_share"] == []
    acc = _sql_one("SELECT accepted_at FROM conversation_share_invites WHERE share_id=%s AND invitee_user_id=%s",
                   sid, invitee)
    used = _sql_one("SELECT last_used_at FROM conversation_shares WHERE id=%s", sid)
    n = _sql_one("SELECT count(*) FROM audit_log WHERE action='share.accepted' AND resource_id=%s", f"share:{sid}")
    assert acc[0] is None and used[0] is None and n[0] == 0


# ===== 共有元の削除 =====

def test_delete_conversation_without_wrapper_hard_deletes():
    (owner,) = _mk_users(_sfx(), "hdo")
    cid = _conv(owner, "共有なし")
    assert store.delete_conversation(cid, owner) is True
    assert get_conversation(cid) is None


def test_delete_conversation_with_live_wrapper_soft_deletes():
    """生きたラッパー有り→soft delete（所有者一覧から消えるが受領側は読める・二重削除は False）。"""
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "sdo", "sdi")
    cid = _conv(owner, "削除しても共有先は残る", "質問", "回答")
    sid = store.create_share(cid, owner, _tok("sdtok", sfx), _future(), [invitee])
    wid = store.accept_share(sid, invitee)

    assert store.delete_conversation(cid, owner) is True
    row = _sql_one("SELECT deleted_at FROM conversations WHERE id=%s", cid)
    assert row is not None and row[0] is not None, "生きたラッパーがあるのに物理削除された"
    assert cid not in [c["id"] for c in store.list_conversations(owner)]
    assert len(store.get_conversation_for_read(invitee, wid)["messages"]) == 2
    assert store.delete_conversation(cid, owner) is False


def test_delete_source_after_sanitized_share_keeps_recipient_readable():
    """snapshot 共有→元会話を削除しても共有先は読める（FK SET NULL・本文コピー済みで独立）。"""
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "sno", "sni")
    cid = _conv(owner, "サニタイズ共有元", "通常の質問", "通常の回答")
    snap = store.create_sanitized_snapshot(owner, cid)
    assert snap is not None
    sid = store.create_share(snap, owner, _tok("sntok", sfx), _future(), [invitee])
    wid = store.accept_share(sid, invitee)

    assert store.delete_conversation(cid, owner) is True
    assert get_conversation(cid) is None
    snap_row = _sql_one("SELECT source_conversation_id FROM conversations WHERE id=%s", snap)
    assert snap_row is not None, "元会話削除で snapshot まで消えた（CASCADE 巻き添え）"
    assert snap_row[0] is None, "FK が SET NULL でなく元会話の残骸を指したまま"
    assert len(store.get_conversation_for_read(invitee, wid)["messages"]) == 2


# ===== delete_conversation × accept_share の行ロック直列化 =====
# 別コネクションで対象行を FOR UPDATE ロックしたまま、本物の delete/accept がブロックされること
# （＝ロックが効いていること）と、解放後の最終状態が先に commit された側を反映することを確認する。

def _wait_until_blocked_by(holder, thread, timeout=30.0):
    """`thread` の処理が `holder` 接続のロック待ちに入るまで待つ（pg_blocking_pids で確認する）。固定時間の待ちで
    「ブロックされている」と見なさないための待ち合わせ。スレッドが先に終わった・期限内に待ちに入らなければ失敗させる。"""
    pid = holder.info.backend_pid
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert thread.is_alive(), "処理が対象行のロック待ちに入る前に終了した"
        with psycopg.connect(store._dsn(), autocommit=True) as c:
            n = c.execute("SELECT count(*) FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                          (pid,)).fetchone()[0]
        if n:
            return
        time.sleep(0.05)
    raise AssertionError("処理が対象行のロックでブロックされていない")


def test_concurrent_accept_before_delete_forces_soft_delete_not_physical():
    """delete が行ロック待ちの間に別トランザクションが先に wrapper を commit したら、delete は
    必ず soft-delete を選ぶ（物理削除しない）。"""
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "cro", "cri")
    cid = _conv(owner, "行ロック直列化テスト", "質問", "回答")
    sid = store.create_share(cid, owner, _tok("racetok2", sfx), _future(), [invitee])

    holder = psycopg.connect(store._dsn())
    holder.execute("SELECT id FROM conversations WHERE id=%s FOR UPDATE", (cid,))
    result: dict = {}

    def _run_delete():
        try:
            result["deleted"] = store.delete_conversation(cid, owner)
        except Exception as e:
            result["error"] = repr(e)

    t = threading.Thread(target=_run_delete)
    t.start()
    try:
        _wait_until_blocked_by(holder, t)
        holder.execute(
            "INSERT INTO conversations (user_id, version, title, origin, source_conversation_id, "
            "  share_id, shared_by_user_id, received_at, read_only) "
            "SELECT %s, c.version, c.title, 'received_share', c.id, s.id, s.owner_user_id, now(), true "
            "FROM conversation_shares s JOIN conversations c ON c.id=s.conversation_id WHERE s.id=%s",
            (invitee, sid))
        holder.commit()
    finally:
        holder.close()   # 未 commit ならロールバックされ、待機中のスレッドも解放される
        t.join(timeout=30)
    assert not t.is_alive(), "delete_conversation がロック解放後も完了しない"
    assert result.get("deleted") is True, result
    row = _sql_one("SELECT deleted_at FROM conversations WHERE id=%s", cid)
    assert row is not None, "行そのものが消えた（物理削除された）"
    assert row[0] is not None, "先に commit された wrapper があるのに物理削除した（TOCTOU 再発）"


def test_concurrent_delete_before_accept_share_raises_cleanly_when_source_gone():
    """逆順（物理削除が先に commit）でも accept_share はクラッシュせず ValueError で拒否する。"""
    sfx = _sfx()
    owner, invitee = _mk_users(sfx, "dro", "dri")
    cid = _conv(owner, "逆順レーステスト", "質問")
    sid = store.create_share(cid, owner, _tok("racetok3", sfx), _future(), [invitee])

    holder = psycopg.connect(store._dsn())
    holder.execute("SELECT id FROM conversations WHERE id=%s FOR UPDATE", (cid,))
    result: dict = {}

    def _run_accept():
        try:
            result["wid"] = store.accept_share(sid, invitee)
        except ValueError as e:
            result["error"] = str(e)

    t = threading.Thread(target=_run_accept)
    t.start()
    try:
        _wait_until_blocked_by(holder, t)
        holder.execute("DELETE FROM conversations WHERE id=%s", (cid,))   # conversation_shares も CASCADE で消える
        holder.commit()
    finally:
        holder.close()   # 未 commit ならロールバックされ、待機中のスレッドも解放される
        t.join(timeout=30)
    assert not t.is_alive(), "accept_share がロック解放後も完了しない"
    assert "error" in result, f"共有元が消えているのに accept_share が例外を出さず完了した: {result}"
    assert "wid" not in result


# ===== 期限「ちょうど」境界 =====

def test_share_boundary_expires_at_equals_now_is_inactive():
    """expires_at == now() ちょうどの共有は active=False（`>` なので境界は期限切れ）。同一トランザクション内の
    now() は同値のため、resolve_share_by_token と同一の active 式の複製 SQL で判定する
    （複製のズレは次の pin テストが検知）。"""
    sfx = _sfx()
    (owner,) = _mk_users(sfx, "beo")
    cid = _conv(owner, "境界テスト")
    with psycopg.connect(store._dsn()) as c:
        sid = c.execute(
            "INSERT INTO conversation_shares "
            "  (conversation_id, owner_user_id, token_hash, expires_at, created_by) "
            "VALUES (%s, %s, %s, now(), %s) RETURNING id",
            (cid, owner, _tok("boundarytok", sfx), owner),
        ).fetchone()[0]
        active = c.execute(
            "SELECT (revoked_at IS NULL AND COALESCE(expires_at, created_at + interval '30 days')>now()) AS active "
            "FROM conversation_shares WHERE id=%s", (sid,),
        ).fetchone()[0]
    assert active is False, "expires_at == now() ちょうどの共有が active=True になった"


def test_share_boundary_operator_pinned_to_implementation():
    """active 式を持つ実装関数のソースに期待する式が現存すること。変わったら上の境界テストの複製 SQL も
    同時に更新する（受領共有の active 判定は conversations._resolve_received_share_msg_src が共通で持つ）。"""
    import inspect

    from sherpa.store import conversations as _conv_mod
    from sherpa.store import shares as _shares

    for fn in (_conv_mod._resolve_received_share_msg_src, _shares.resolve_share_by_token):
        assert "SHARE_EFFECTIVE_EXPIRES_SQL}>now()" in inspect.getsource(fn), (
            f"{fn.__name__} の active 式が変わった＝境界テスト（複製 SQL）が実装とズレている可能性")
