"""認証・会話共有の store 層＋auth ヘルパ（docs/proposals/2026-07-01-認証と共有の提案.md MVP スライス1）。

要 Postgres（他の api テスト同様）。DB 不可は SKIP。共有の核（リンク→クリック→受領ラッパー→
本文読取→越境不可→取消で不可）を固定する。
"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from _test_users import register_test_uid
from sherpa import auth


def test_password_hash_roundtrip():
    h = auth.hash_password("hunter2")
    assert h.startswith("pbkdf2_sha256$")
    assert auth.verify_password("hunter2", h) is True
    assert auth.verify_password("wrong", h) is False
    assert auth.verify_password("x", None) is False and auth.verify_password("x", "garbage") is False


def _future(days=7):
    return datetime.now(timezone.utc) + timedelta(days=days)


def test_share_click_history_and_revoke():
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    sfx = str(int(time.time() * 1000))
    sato, tan, other = f"sato{sfx}", f"tan{sfx}", f"oth{sfx}"
    for u in (sato, tan, other):
        store.upsert_user(u, email=f"{u}@ex.local", display_name=u.upper(),
                          password_hash=auth.hash_password("pw"), role="user")
        register_test_uid(u)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）

    conv = store.create_conversation(user_id=sato, world="v1", title="請求機能の影響調査")
    cid = conv["id"]
    store.add_message(cid, "user", "請求はどう算出？")
    store.add_message(cid, "assistant", "税計算ルールで算出します")

    th = hashlib.sha256(("tok-" + sfx).encode()).hexdigest()
    sid = store.create_share(cid, sato, th, _future(), [tan])

    # 招待判定
    assert store.resolve_share_by_token(th)["active"] is True
    assert store.is_invited(sid, tan) is True and store.is_invited(sid, other) is False

    # クリック → 受領ラッパー（冪等）
    wid = store.accept_share(sid, tan)
    assert store.accept_share(sid, tan) == wid                 # 同 uid×share は1行

    # tan の履歴に受領共有が「誰から／状態／読取専用」付きで出る
    rec = [r for r in store.list_conversations(tan) if r["origin"] == "received_share"]
    assert len(rec) == 1
    assert rec[0]["shared_by_user_id"] == sato and rec[0]["shared_by_name"] == sato.upper()
    assert rec[0]["share_status"] == "active" and rec[0]["read_only"] is True

    # tan は本文（sato の元会話のメッセージ）を読める・conversation.id は wrapper 維持
    r = store.get_conversation_for_read(tan, wid)
    assert len(r["messages"]) == 2 and r["conversation"]["id"] == wid
    assert r["messages"][0]["content"] == "請求はどう算出？"

    # 越境不可: 招待外/非所有者は wrapper に触れない
    assert store.get_conversation_for_read(other, wid) is None
    # 受領共有は書き込み対象にならない（owns=False）／sato は own
    assert store.owns_conversation(tan, wid) is False
    assert store.owns_conversation(sato, cid) is True

    # 取消 → tan は開けない（履歴には残るが unavailable）
    assert store.revoke_share(sid, sato) is True
    r2 = store.get_conversation_for_read(tan, wid)
    assert r2["share_status"] == "unavailable" and r2["messages"] == []
    assert [x for x in store.list_conversations(tan)
            if x["id"] == wid][0]["share_status"] == "revoked"


def test_received_share_strips_route_and_trace():
    """RV HIGH: 受領共有は答え/出典だけ見せる。route/trace（grep クエリ・doc_id・Codex 実コマンド
    detail 等の内部情報）は受領者には返さない（sanitized と同じ posture）。所有者本人には引き続き
    見える（伏せるのは受領共有の read path だけ）。"""
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    sfx = str(int(time.time() * 1000))
    owner, invitee = _mk_users(sfx, "rvo", "rvi")

    conv = store.create_conversation(user_id=owner, world="v1", title="内部情報を含む調査")
    cid = conv["id"]
    store.add_message(cid, "user", "TAXCALC の影響は？")
    trace = [{"type": "node", "id": "tool-grep", "kind": "tool", "label": "資料を検索（語句そのまま）",
             "detail": "「TAX-RATE」", "status": "done"}]
    route = {"lens": "qa", "path": ["4期/03_開発/01_ソース/TAXCALC.cbl"], "reason": "grep hit"}
    store.add_message(cid, "assistant", "TAXCALC に影響します", route=route, trace=trace)

    th = hashlib.sha256(("tok-rv-" + sfx).encode()).hexdigest()
    sid = store.create_share(cid, owner, th, _future(), [invitee])
    wid = store.accept_share(sid, invitee)

    r = store.get_conversation_for_read(invitee, wid)
    assert len(r["messages"]) == 2
    assistant_msg = next(m for m in r["messages"] if m["role"] == "assistant")
    assert assistant_msg["content"] == "TAXCALC に影響します"    # 答え自体は見える
    assert assistant_msg["route"] is None                        # 内部情報は伏せる
    assert assistant_msg["trace"] is None

    # 所有者本人は引き続き route/trace を見られる（伏せるのは受領共有だけ）。
    owner_view = store.get_conversation_for_read(owner, cid)
    owner_assistant = next(m for m in owner_view["messages"] if m["role"] == "assistant")
    assert owner_assistant["trace"] == trace and owner_assistant["route"] == route


def test_received_share_strips_answer_embedded_route():
    """RV HIGH（Codex 2026-07-07）: `chat_service._finalize` はレンズ・reason・path を含む
    `env["route"]` を answer envelope 自身にも埋め込む（`add_message(..., answer=env, ...)`）ため、
    トップレベル messages.route/trace 列の NULL 化だけでは受領共有の読者に answer.route が
    そのまま届いてしまっていた（web/chat.js の route チップ描画に使われる＝ポリシー違反）。
    S1 以前からの既存バグだが、`_strip_shared_message` を触ったので併せて固定する。"""
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    sfx = str(int(time.time() * 1000))
    owner, invitee = _mk_users(sfx, "rto", "rti")

    conv = store.create_conversation(user_id=owner, world="v1", title="answer内route確認")
    cid = conv["id"]
    store.add_message(cid, "user", "TAXCALC の影響は？")
    answer = {"headline": "TAXCALC に影響します", "lens": "impact",
             "route": {"lens": "impact", "reason": "AI判定（意図分類）", "path": ["関係を確認"]},
             "sources": []}
    store.add_message(cid, "assistant", "TAXCALC に影響します", answer=answer)   # route 列自体は付けない

    th = hashlib.sha256(("tok-rt-" + sfx).encode()).hexdigest()
    sid = store.create_share(cid, owner, th, _future(), [invitee])
    wid = store.accept_share(sid, invitee)

    r = store.get_conversation_for_read(invitee, wid)
    assistant_msg = next(m for m in r["messages"] if m["role"] == "assistant")
    assert assistant_msg["answer"]["headline"] == "TAXCALC に影響します"   # 答え自体は見える
    assert "route" not in assistant_msg["answer"], "answer.route が受領共有に漏れている"

    # 所有者本人は引き続き answer.route を見られる（伏せるのは受領共有だけ）。
    owner_view = store.get_conversation_for_read(owner, cid)
    owner_assistant = next(m for m in owner_view["messages"] if m["role"] == "assistant")
    assert owner_assistant["answer"]["route"]["lens"] == "impact"


def test_received_share_strips_usage():
    """F3（2026-07-07-フィードバック一括.md）: answer.usage（トークン使用量＝コストの手掛かり）は
    route/trace/question と同格の内部情報として受領共有では伏せる。所有者本人には見える。"""
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    sfx = str(int(time.time() * 1000))
    owner, invitee = _mk_users(sfx, "uso", "usi")

    conv = store.create_conversation(user_id=owner, world="v1", title="usage確認")
    cid = conv["id"]
    store.add_message(cid, "user", "TAXCALC の影響は？")
    answer = {"headline": "TAXCALC に影響します", "lens": "impact", "sources": [],
             "usage": {"provider": "codex", "model": "gpt-5.5", "input_tokens": 341026,
                       "cached_input_tokens": 244864, "output_tokens": 12318,
                       "reasoning_output_tokens": 9392}}
    store.add_message(cid, "assistant", "TAXCALC に影響します", answer=answer)

    th = hashlib.sha256(("tok-us-" + sfx).encode()).hexdigest()
    sid = store.create_share(cid, owner, th, _future(), [invitee])
    wid = store.accept_share(sid, invitee)

    r = store.get_conversation_for_read(invitee, wid)
    assistant_msg = next(m for m in r["messages"] if m["role"] == "assistant")
    assert assistant_msg["answer"]["headline"] == "TAXCALC に影響します"   # 答え自体は見える
    assert "usage" not in assistant_msg["answer"], "answer.usage が受領共有に漏れている"
    assert "341026" not in str(assistant_msg["answer"])

    owner_view = store.get_conversation_for_read(owner, cid)
    owner_assistant = next(m for m in owner_view["messages"] if m["role"] == "assistant")
    assert owner_assistant["answer"]["usage"]["input_tokens"] == 341026   # 本人には見える


def test_received_share_strips_question_card():
    """S1（ask_user-improvements.md）: 確認カード（answer.question）も route/trace と同じ posture で
    受領共有には返さない（interaction_id・options 等の内部情報を含み、読み取り専用の共有会話に
    対話カードを出す意味も無い）。所有者本人には引き続き見える。"""
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    sfx = str(int(time.time() * 1000))
    owner, invitee = _mk_users(sfx, "qvo", "qvi")

    conv = store.create_conversation(user_id=owner, world="v1", title="確認を含む調査")
    cid = conv["id"]
    store.add_message(cid, "user", "税率を変えたら夜間バッチが落ちる？")
    question = {"interaction_id": "ask-cafef00d", "mode": "single", "prompt": "どの調べ方をしますか？",
                "options": [{"id": "impact", "label": "影響範囲", "description": ""}],
                "allow_free_text": False, "original_message": "税率を変えたら夜間バッチが落ちる？"}
    store.add_message(cid, "assistant", "どの調べ方をしますか？", lens="clarify",
                      answer={"lens": "clarify", "question": question})

    th = hashlib.sha256(("tok-q-" + sfx).encode()).hexdigest()
    sid = store.create_share(cid, owner, th, _future(), [invitee])
    wid = store.accept_share(sid, invitee)

    r = store.get_conversation_for_read(invitee, wid)
    assistant_msg = next(m for m in r["messages"] if m["role"] == "assistant")
    assert assistant_msg["content"] == "どの調べ方をしますか？"     # 答え（prompt）自体は見える
    assert "question" not in (assistant_msg["answer"] or {})       # 確認カード payload は伏せる
    assert assistant_msg["answer"].get("lens") == "clarify"        # 他の answer キーは posture どおり保持
    assert assistant_msg["route"] is None and assistant_msg["trace"] is None
    blob = str(assistant_msg["answer"])
    assert "ask-cafef00d" not in blob and "影響範囲" not in blob

    # 所有者本人は引き続き確認カードを読める（伏せるのは受領共有だけ）。
    owner_view = store.get_conversation_for_read(owner, cid)
    owner_assistant = next(m for m in owner_view["messages"] if m["role"] == "assistant")
    assert owner_assistant["answer"]["question"]["interaction_id"] == "ask-cafef00d"


# ===== 2026-07-02-共有の無期限と永続化.md: 無期限＋削除巻き添え解消 =====

def _mk_users(sfx: str, *names: str):
    from sherpa import store
    uids = [f"{n}{sfx}" for n in names]
    for u in uids:
        store.upsert_user(u, email=f"{u}@ex.local", display_name=u.upper(),
                          password_hash=auth.hash_password("pw"), role="user")
        register_test_uid(u)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）
    return uids


def test_unlimited_expiry_active_forever_until_revoked():
    """expires_at=None（無期限）は時間経過に依存せず active・revoke で即時無効。"""
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    sfx = str(int(time.time() * 1000))
    owner, invitee = _mk_users(sfx, "ulo", "uli")

    conv = store.create_conversation(user_id=owner, world="v1", title="無期限共有")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    store.add_message(cid, "assistant", "回答")

    th = hashlib.sha256(("ultok-" + sfx).encode()).hexdigest()
    sid = store.create_share(cid, owner, th, None, [invitee])   # expires_at=None＝無期限

    resolved = store.resolve_share_by_token(th)
    assert resolved["expires_at"] is None
    assert resolved["active"] is True

    wid = store.accept_share(sid, invitee)
    rec = [r for r in store.list_conversations(invitee) if r["id"] == wid][0]
    assert rec["share_status"] == "active"

    r = store.get_conversation_for_read(invitee, wid)
    assert len(r["messages"]) == 2

    # revoke は無期限でも即時有効。
    assert store.revoke_share(sid, owner) is True
    assert store.resolve_share_by_token(th)["active"] is False
    r2 = store.get_conversation_for_read(invitee, wid)
    assert r2["share_status"] == "unavailable" and r2["messages"] == []


def test_delete_conversation_without_wrapper_hard_deletes():
    """(b) ラッパー無し→従来どおり物理削除（行そのものが消える）。"""
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    sfx = str(int(time.time() * 1000))
    (owner,) = _mk_users(sfx, "hdo")

    conv = store.create_conversation(user_id=owner, world="v1", title="共有なし")
    cid = conv["id"]

    assert store.delete_conversation(cid, owner) is True
    assert store.get_conversation(cid) is None, "ラッパー無しの会話が物理削除されていない"


def test_delete_conversation_with_live_wrapper_soft_deletes():
    """(a) 生きたラッパー有り→soft delete（所有者一覧から消えるが受領側は読める・二重削除は404相当）。"""
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    sfx = str(int(time.time() * 1000))
    owner, invitee = _mk_users(sfx, "sdo", "sdi")

    conv = store.create_conversation(user_id=owner, world="v1", title="削除しても共有先は残る")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    store.add_message(cid, "assistant", "回答")

    th = hashlib.sha256(("sdtok-" + sfx).encode()).hexdigest()
    sid = store.create_share(cid, owner, th, _future(), [invitee])
    wid = store.accept_share(sid, invitee)

    assert store.delete_conversation(cid, owner) is True

    # 物理的には行が残る（deleted_at のみ設定）。
    with psycopg.connect(store._dsn()) as c:
        row = c.execute("SELECT deleted_at FROM conversations WHERE id=%s", (cid,)).fetchone()
    assert row is not None and row[0] is not None, "生きたラッパーがあるのに物理削除された"

    # 所有者の一覧からは消える（既存の deleted_at IS NULL フィルタ）。
    assert cid not in [c["id"] for c in store.list_conversations(owner)]

    # 受領側は引き続き読める（本文は元会話の messages を都度読む経路のまま）。
    r = store.get_conversation_for_read(invitee, wid)
    assert len(r["messages"]) == 2, "生きたラッパー経由の読取が soft delete で壊れた"

    # 二重削除は False（既に deleted_at 設定済み・再更新されない＝呼出側で404相当）。
    assert store.delete_conversation(cid, owner) is False


def test_delete_source_after_sanitized_share_keeps_recipient_readable():
    """(c) snapshot 共有→元会話を削除しても共有先は読める（FK SET NULL・本文コピー済みで独立）。"""
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    sfx = str(int(time.time() * 1000))
    owner, invitee = _mk_users(sfx, "sno", "sni")

    conv = store.create_conversation(user_id=owner, world="v1", title="サニタイズ共有元")
    cid = conv["id"]
    store.add_message(cid, "user", "通常の質問")
    store.add_message(cid, "assistant", "通常の回答")

    snap = store.create_sanitized_snapshot(owner, cid)
    assert snap is not None

    th = hashlib.sha256(("sntok-" + sfx).encode()).hexdigest()
    sid = store.create_share(snap, owner, th, _future(), [invitee])
    wid = store.accept_share(sid, invitee)

    # 元会話（サニタイズ前）を物理削除。ラッパーは snapshot を source にしているため
    # 元会話に生きたラッパーは無い＝物理削除される。snapshot 自身は FK SET NULL で無傷のはず。
    assert store.delete_conversation(cid, owner) is True
    assert store.get_conversation(cid) is None

    with psycopg.connect(store._dsn()) as c:
        snap_row = c.execute(
            "SELECT source_conversation_id FROM conversations WHERE id=%s", (snap,)).fetchone()
    assert snap_row is not None, "元会話削除で snapshot まで消えた（CASCADE 巻き添え）"
    assert snap_row[0] is None, "FK が SET NULL でなく元会話の残骸を指したまま"

    r = store.get_conversation_for_read(invitee, wid)
    assert len(r["messages"]) == 2, "snapshot 経由の共有が元会話削除で巻き添えになった"


# ===== RV ラウンド2 HIGH: delete_conversation × accept_share の行ロック直列化 =====
# 別コネクションで対象行を明示的に FOR UPDATE ロックしたまま保持し、その間に本物の
# delete_conversation / accept_share がブロックされること（＝ロックが本当に効いていること）と、
# ロック解放後の最終状態が「先に commit された方」を正しく反映することを決定的に確認する。

def test_concurrent_accept_before_delete_forces_soft_delete_not_physical():
    """delete_conversation が行ロック待ちの間に、別トランザクションが先に wrapper を commit したら、
    delete は必ず soft-delete を選ぶ（物理削除しない＝TOCTOU 再発防止の決定的再現）。"""
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    sfx = str(int(time.time() * 1000))
    owner, invitee = _mk_users(sfx, "cro", "cri")

    conv = store.create_conversation(user_id=owner, world="v1", title="行ロック直列化テスト")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")
    store.add_message(cid, "assistant", "回答")

    th = hashlib.sha256(("racetok2-" + sfx).encode()).hexdigest()
    sid = store.create_share(cid, owner, th, _future(), [invitee])

    # 対象行を別コネクションで FOR UPDATE ロックし、まだ commit しない
    # （accept_share が wrapper を INSERT する直前でロックを保持している状態を模す）。
    holder = psycopg.connect(store._dsn())
    holder.execute("SELECT id FROM conversations WHERE id=%s FOR UPDATE", (cid,))

    result: dict = {}

    def _run_delete():
        result["deleted"] = store.delete_conversation(cid, owner)

    th_delete = threading.Thread(target=_run_delete)
    th_delete.start()
    th_delete.join(timeout=0.5)
    assert th_delete.is_alive(), (
        "delete_conversation が対象行のロック中にブロックされていない"
        "（FOR UPDATE 未導入 or 効いていない＝競合防止が機能していない）"
    )

    # ロック保持側が accept_share と同じ INSERT を行ってから commit（先に wrapper を確定させる）。
    holder.execute(
        "INSERT INTO conversations (user_id, version, title, origin, source_conversation_id, "
        "  share_id, shared_by_user_id, received_at, read_only) "
        "SELECT %s, c.version, c.title, 'received_share', c.id, s.id, s.owner_user_id, now(), true "
        "FROM conversation_shares s JOIN conversations c ON c.id=s.conversation_id WHERE s.id=%s",
        (invitee, sid))
    holder.commit()
    holder.close()

    th_delete.join(timeout=5)
    assert not th_delete.is_alive(), "delete_conversation がロック解放後も完了しない"
    assert result.get("deleted") is True

    with psycopg.connect(store._dsn()) as c:
        row = c.execute("SELECT deleted_at FROM conversations WHERE id=%s", (cid,)).fetchone()
    assert row is not None, "行そのものが消えた（物理削除された）"
    assert row[0] is not None, (
        "先に commit された wrapper があるのに delete_conversation が物理削除した（TOCTOU 再発）"
    )


# ===== フェーズ7 S5-2: 共有 token の期限「ちょうど」境界 =====

def test_share_boundary_expires_at_equals_now_is_inactive():
    """境界: expires_at == now() ちょうどの共有は active=False（期限切れ扱い）。

    resolve_share_by_token / get_conversation_for_read（sherpa/store/shares.py）の active 判定は
    `revoked_at IS NULL AND (expires_at IS NULL OR expires_at>now())`（`>=` ではなく `>`）＝
    境界（等しい瞬間）は active から外れる。この等号境界を実際の関数呼び出し2回（別トランザクション
    ＝別 now()）で再現するのは実時間が必ず前進するため不可能なので、同一トランザクション内で
    now() を2回参照する（Postgres の now() はトランザクション開始時刻で安定）ことで expires_at を
    "ちょうど" now() と厳密に等しくし、resolve_share_by_token と同一の active 式で判定させる。
    """
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    sfx = str(int(time.time() * 1000))
    (owner,) = _mk_users(sfx, "beo")

    conv = store.create_conversation(user_id=owner, world="v1", title="境界テスト")
    cid = conv["id"]

    th = hashlib.sha256(("boundarytok-" + sfx).encode()).hexdigest()

    dsn = store._dsn()
    with psycopg.connect(dsn) as c:
        # INSERT と active 判定 SELECT を同一トランザクション内（同一コネクション・未 commit）で
        # 行うことで両方の now() が同一値になる。
        ins = c.execute(
            "INSERT INTO conversation_shares "
            "  (conversation_id, owner_user_id, token_hash, expires_at, created_by) "
            "VALUES (%s, %s, %s, now(), %s) RETURNING id",
            (cid, owner, th, owner),
        ).fetchone()
        sid = ins[0] if isinstance(ins, tuple) else ins["id"]

        # resolve_share_by_token（sherpa/store/shares.py）と同一の active 式。
        active_row = c.execute(
            "SELECT (revoked_at IS NULL AND (expires_at IS NULL OR expires_at>now())) AS active "
            "FROM conversation_shares WHERE id=%s", (sid,),
        ).fetchone()
    active = active_row[0] if isinstance(active_row, tuple) else active_row["active"]

    assert active is False, (
        "境界 FAIL: expires_at == now() ちょうどの共有が active=True になった "
        "（実装の比較演算子は `>` なので境界は「期限切れ」扱いのはず）"
    )


def test_share_boundary_operator_pinned_to_implementation():
    """RV MED（2026-07-14 フェーズ7 1巡目）: 上の境界テストは実装の active 式を複製 SQL で
    再現している（別トランザクションでは now() が前進し「ちょうど」を作れないため）。
    複製と実装の乖離（`>` が `>=` に変わる等）を検知するため、active 式を持つ実装2関数の
    ソースに期待する式が現存することを pin する。落ちたら境界テストの複製 SQL と同時に更新すること。"""
    import inspect

    from sherpa.store import shares as _shares

    for fn in (_shares.get_conversation_for_read, _shares.resolve_share_by_token):
        src = inspect.getsource(fn)
        assert "expires_at IS NULL OR expires_at>now()" in src, (
            f"{fn.__name__} の active 式が 'expires_at IS NULL OR expires_at>now()' から変わった＝"
            "本ファイルの境界テスト（複製 SQL）が実装とズレている可能性。両方を同時に更新すること"
        )


def test_concurrent_delete_before_accept_share_raises_cleanly_when_source_gone():
    """逆順（対象行の物理削除が先に commit）でも accept_share はクラッシュせず ValueError で拒否する
    （FOR UPDATE ロック中に共有元が消え、conversation_shares も CASCADE で無くなるケース）。"""
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    sfx = str(int(time.time() * 1000))
    owner, invitee = _mk_users(sfx, "dro", "dri")

    conv = store.create_conversation(user_id=owner, world="v1", title="逆順レーステスト")
    cid = conv["id"]
    store.add_message(cid, "user", "質問")

    th = hashlib.sha256(("racetok3-" + sfx).encode()).hexdigest()
    sid = store.create_share(cid, owner, th, _future(), [invitee])

    holder = psycopg.connect(store._dsn())
    holder.execute("SELECT id FROM conversations WHERE id=%s FOR UPDATE", (cid,))

    result: dict = {}

    def _run_accept():
        try:
            result["wid"] = store.accept_share(sid, invitee)
        except ValueError as e:
            result["error"] = str(e)

    th_accept = threading.Thread(target=_run_accept)
    th_accept.start()
    th_accept.join(timeout=0.5)
    assert th_accept.is_alive(), "accept_share が対象行のロック中にブロックされていない"

    # 物理削除を commit（wrapper 無しの delete_conversation の物理削除分岐と同じ効果）。
    # conversation_shares も CASCADE で一緒に消える。
    holder.execute("DELETE FROM conversations WHERE id=%s", (cid,))
    holder.commit()
    holder.close()

    th_accept.join(timeout=5)
    assert not th_accept.is_alive(), "accept_share がロック解放後も完了しない"
    assert "error" in result, f"共有元が消えているのに accept_share が例外を出さず完了した: {result}"
    assert "wid" not in result
