"""チャットターンのバックグラウンド実行（覗き窓方式・POST /chat/turns）の契約テスト。

POST はターンを background thread として起動して即 turn_id を返し、購読ゼロでも完走・DB 永続する。
GET .../stream?cursor=N はバッファの replay→追従、GET /chat/turns/running は一覧、
POST .../stop は停止。会話フローは要 Neo4j ＋ Postgres（test_chat_m8.py と同じ前提）。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from _world_setup import TEST_WORLD_ID, ensure_v1
from _store_helpers import get_conversation


V = TEST_WORLD_ID
IMPACT_MSG = {"message": "消費税率を変えたい。影響は？", "world": V, "knowledge": True}


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """ログインせず直接叩く（compat モード＝合成 admin）。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


def _client():
    from fastapi.testclient import TestClient
    from sherpa.api import app
    return TestClient(app)


def _wait_turn_done(turn_id, timeout=5.0):
    """turn の background thread が完走（buffer.done）するまで待つ（レジストリ汚染防止）。"""
    from sherpa import chat_turns
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = chat_turns.get_turn(turn_id)
        if rec is None or rec.buffer.done:
            return
        time.sleep(0.02)
    raise AssertionError(f"turn {turn_id} が時間内に完了しなかった")


def _drain_stream(resp_text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in resp_text.splitlines() if line.startswith("data: ")]


def _start(uid, conversation_id, run_fn):
    """chat_turns.start_turn を「conversation_id 決め打ち・run_fn そのまま」で呼ぶ橋渡し。"""
    from sherpa import chat_turns
    return chat_turns.start_turn(uid=uid, conversation_factory=lambda: conversation_id,
                                 run_fn_factory=lambda cid: run_fn)


def _start_known(uid, cid, run_fn):
    """既存会話への継続（known_conversation_id 明示）。"""
    from sherpa import chat_turns
    return chat_turns.start_turn(uid=uid, conversation_factory=lambda: cid,
                                 run_fn_factory=lambda c: run_fn, known_conversation_id=cid)


class _Blocker:
    """gate が開くまで終わらない run_fn。start() で起動した turn は teardown で解放して完走を待つ。"""

    def __init__(self):
        self.gate = threading.Event()
        self.recs = []

    def run(self, stop_event, emit):
        emit({"type": "node", "id": "x", "kind": "think", "label": "x", "detail": "x", "status": "done"})
        self.gate.wait(timeout=10)

    def start(self, uid, cid):
        rec = _start(uid, cid, self.run)
        self.recs.append(rec)
        return rec

    def start_known(self, uid, cid):
        rec = _start_known(uid, cid, self.run)
        self.recs.append(rec)
        return rec


@pytest.fixture
def blocker():
    b = _Blocker()
    yield b
    b.gate.set()
    for rec in b.recs:
        _wait_turn_done(rec.turn_id)


def _post_turn(c, **over):
    r = c.post("/chat/turns", json={**IMPACT_MSG, **over})
    assert r.status_code == 200, r.text
    return r.json()["turn_id"], r.json()["conversation_id"]


# ===== 完走・永続・購読 =====

def test_turn_completes_and_persists_answer_without_any_subscriber_and_replays_after_completion():
    """誰も stream を購読しなくても background thread が最後まで走り、DB（messages/trace）に答えが残る。"""
    ensure_v1()
    c = _client()
    tid, cid = _post_turn(c)
    _wait_turn_done(tid)

    conv = c.get(f"/conversations/{cid}").json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"], "購読ゼロで完走していない"
    assistant = conv["messages"][-1]
    assert assistant["answer"]["lens"] == "impact"
    assert assistant["answer"]["sources"], "出典が保存されていない"
    assert assistant.get("trace"), "trace が保存されていない"

    # 完了済みターン（ナレッジ参照なしの素の会話）への購読は全イベントを replay して終わり（answer で完走）、
    # cursor を総数に合わせると空。
    tid, _cid = _post_turn(c, message="こんにちは", knowledge=False)
    _wait_turn_done(tid)
    resp = c.get(f"/chat/turns/{tid}/stream", params={"cursor": 0})
    assert resp.status_code == 200
    events = _drain_stream(resp.text)
    assert events and events[-1]["type"] == "answer", "完了済みターンの replay が空、または answer で終わっていない"
    resp2 = c.get(f"/chat/turns/{tid}/stream", params={"cursor": len(events)})
    assert resp2.status_code == 200
    assert _drain_stream(resp2.text) == []


def test_turn_start_response_returns_promptly_before_completion(monkeypatch):
    from sherpa import chat_service
    monkeypatch.setattr(chat_service, "emit_pace", lambda: 1.5)
    ensure_v1()
    c = _client()
    t0 = time.time()
    r = c.post("/chat/turns", json=IMPACT_MSG)
    elapsed = time.time() - t0
    assert r.status_code == 200
    assert elapsed < 1.0, f"POST /chat/turns が完走を待って遅延している: {elapsed}s"
    _wait_turn_done(r.json()["turn_id"], timeout=10)


def test_background_turn_persists_clarify_question_card_without_subscriber(monkeypatch):
    """購読ゼロの背景ターンでも確認カード（clarify）は assistant メッセージとして trace 付きで永続化される。
    Tier2(LLM) は未接続化（共有 dev DB の実キーで先取り分類されるのを防ぐ）。"""
    from sherpa import intent_llm
    monkeypatch.setattr(intent_llm, "classify", lambda *a, **k: None)
    ensure_v1()
    c = _client()
    tid, cid = _post_turn(c, message="税率を変えたら夜間バッチが落ちる？")
    _wait_turn_done(tid)

    conv = c.get(f"/conversations/{cid}").json()
    assistant = [m for m in conv["messages"] if m["role"] == "assistant"]
    assert len(assistant) == 1, f"背景ターンで確認カードが保存されていない: {[m['role'] for m in conv['messages']]}"
    saved = assistant[0]
    assert saved["answer"]["lens"] == "clarify"
    q = saved["answer"]["question"]
    assert q["options"] and q["prompt"]
    assert q["original_message"] == "税率を変えたら夜間バッチが落ちる？"
    assert saved.get("trace"), "背景の clarify ターンでも trace が保存されるはず"


def test_turn_stream_reconnect_from_cursor_has_no_event_gap(monkeypatch):
    """最初の購読を途中で打ち切り、受け取った件数を cursor に再購読すると残りが欠落なく届き answer で完走する。"""
    from sherpa import chat_service
    monkeypatch.setattr(chat_service, "emit_pace", lambda: 0.12)   # 4ノード分の実時間の窓
    ensure_v1()
    c = _client()
    tid, _cid = _post_turn(c)

    first_events: list[dict] = []
    with c.stream("GET", f"/chat/turns/{tid}/stream", params={"cursor": 0}) as s:
        for line in s.iter_lines():
            if line and line.startswith("data: "):
                first_events.append(json.loads(line[6:]))
                if len(first_events) >= 2:
                    break   # 途中切断（turn 自体は続く）
    assert first_events, "最初の接続でイベントを受け取れなかった"
    assert first_events[-1]["type"] != "answer", "打ち切り前に完走してしまった（pace を確認）"

    rest_events: list[dict] = []
    with c.stream("GET", f"/chat/turns/{tid}/stream", params={"cursor": len(first_events)}) as s:
        for line in s.iter_lines():
            if line and line.startswith("data: "):
                rest_events.append(json.loads(line[6:]))

    all_types = [e["type"] for e in first_events] + [e["type"] for e in rest_events]
    assert all_types[-1] == "answer", f"再購読後に完走していない: {all_types}"
    assert all_types.count("answer") == 1, f"answer が重複配信された: {all_types}"
    _wait_turn_done(tid)


def test_iter_sse_yields_keepalive_when_no_progress(blocker):
    """新規イベントも完了も無いまま wait がタイムアウトすると keepalive コメント行を yield し続ける
    （切断後に generator が居座らないための安全弁）。"""
    from sherpa import chat_turns
    rec = blocker.start("keepalive-user", 900301)
    gen = chat_turns.iter_sse(rec.turn_id, "keepalive-user", cursor=0, wait_timeout=0.05)
    assert gen is not None
    first = next(gen)
    assert first.startswith("data: "), f"最初のイベントが data: で始まらない: {first!r}"
    assert next(gen) == ": keepalive\n\n"
    assert next(gen) == ": keepalive\n\n"


# ===== 停止 =====

def test_turn_stop_via_turn_id_skips_assistant_save_and_yields_stopped_and_leaves_running_list(monkeypatch):
    """stop は stream_message(stop_event=...) と同じ意味論（assistant は保存されない・stopped で終わる）。
    running 一覧には開始直後に turn_id が出て、終了後は消える。存在しない turn_id の stop は {"ok": false}。"""
    from sherpa import chat_service
    monkeypatch.setattr(chat_service, "emit_pace", lambda: 0.15)
    ensure_v1()
    c = _client()
    tid, cid = _post_turn(c)

    hit = next((t for t in c.get("/chat/turns/running").json()["turns"] if t["turn_id"] == tid), None)
    assert hit is not None, "開始直後は running 一覧に出るはず"
    assert hit["conversation_id"] == cid
    assert hit.get("started_at")

    assert c.post(f"/chat/turns/{tid}/stop").json() == {"ok": True}
    r = c.post("/chat/turns/no-such-turn-id/stop")
    assert r.status_code == 200 and r.json() == {"ok": False}

    events = _drain_stream(c.get(f"/chat/turns/{tid}/stream", params={"cursor": 0}).text)
    assert events, "停止ターンからイベントを受け取れなかった"
    assert events[-1]["type"] == "stopped", f"stopped で終わっていない: {[e['type'] for e in events]}"
    roles = [m["role"] for m in c.get(f"/conversations/{cid}").json()["messages"]]
    assert roles == ["user"], f"assistant が保存されている（停止なのに完走扱い）: {roles}"
    _wait_turn_done(tid, timeout=10)
    assert not any(t["turn_id"] == tid for t in c.get("/chat/turns/running").json()["turns"]), \
        "完了後も running 一覧に残っている"


def test_turn_stream_stop_and_running_list_respect_ownership(blocker):
    """他人の turn_id は購読 404（存在有無を教えない）・一般利用者は停止不可（{"ok": false}）。
    管理者は all=true で全員分（uid 付き）を見て停止できる。既定（本人分）の running 一覧には
    他人のターンが出ず uid は値を持たない。"""
    from sherpa import chat_turns
    rec = blocker.start("someone-else", 555001)
    c = _client()   # compat モード = admin
    assert c.get(f"/chat/turns/{rec.turn_id}/stream", params={"cursor": 0}).status_code == 404

    # compat の client は admin なので、一般利用者の判定は stop_turn を直接呼ぶ。
    assert chat_turns.stop_turn(rec.turn_id, "another-plain-user") is False
    assert not rec.stop_event.is_set(), "他人のターンなのに停止イベントが立った"
    lr = c.get("/chat/turns/running", params={"all": "true"}).json()["turns"]
    assert any(t["turn_id"] == rec.turn_id and t["uid"] == "someone-else" for t in lr)
    mine = c.get("/chat/turns/running").json()["turns"]
    assert all(t["turn_id"] != rec.turn_id and t.get("uid") is None for t in mine)
    assert c.post(f"/chat/turns/{rec.turn_id}/stop").json() == {"ok": True}
    assert rec.stop_event.is_set(), "管理者の停止が効いていない"


# ===== 同時実行上限（1ユーザー・全体・同一会話） =====

def test_start_turn_enforces_per_user_limit(blocker):
    from sherpa import chat_turns
    for i in range(chat_turns.MAX_TURNS_PER_USER):
        blocker.start("limituser-a", 600 + i)
    with pytest.raises(chat_turns.TurnLimitError) as ei:
        _start("limituser-a", 699, blocker.run)
    assert ei.value.scope == "user"
    blocker.start("limituser-b", 698)   # 別ユーザーは影響を受けない


def test_start_turn_enforces_global_limit(blocker):
    """全体の未完了数が MAX_TURNS_GLOBAL に達すると別ユーザーでも scope='global'。
    他テストの残留ターンに影響されないよう、開始時点の未完了数を実測して不足分だけ埋める。"""
    from sherpa import chat_turns
    with chat_turns._REGISTRY_LOCK:
        current = sum(1 for r in chat_turns._REGISTRY.values() if not r.buffer.done)
    need = chat_turns.MAX_TURNS_GLOBAL - current
    assert need >= 1, "既に全体上限に達した状態でテストを開始できない（残留ターンを確認）"
    for i in range(need):
        blocker.start(f"globallimituser{i}", 700 + i)
    with pytest.raises(chat_turns.TurnLimitError) as ei:
        _start("yet-another-user", 799, blocker.run)
    assert ei.value.scope == "global"


def test_start_turn_rejects_same_known_conversation_id_while_unfinished(monkeypatch, blocker):
    """既存会話への継続（known_conversation_id 明示）だけが対象。別会話・新規会話（省略）は受け付ける。
    ユーザー上限にも同時に達している場合は scope='user' でなく scope='conversation'（会話単位の排他が先）。"""
    from sherpa import chat_turns
    monkeypatch.setattr(chat_turns, "effective_limits", lambda: (2, 100))
    cid = 9001
    blocker.start_known("convlimit-u1", cid)
    with pytest.raises(chat_turns.TurnLimitError) as ei:
        _start_known("convlimit-u1", cid, blocker.run)
    assert ei.value.scope == "conversation"

    blocker.start_known("convlimit-u1", 9002)   # 別会話は受け付ける（これでユーザー上限 2 に達する）
    with pytest.raises(chat_turns.TurnLimitError) as ei:
        _start_known("convlimit-u1", cid, blocker.run)
    assert ei.value.scope == "conversation", f"会話排他が優先されるべき: {ei.value.scope!r}"
    blocker.start("convlimit-u2", cid)   # known_conversation_id 省略＝対象外


def test_start_turn_reserves_known_conversation_id_before_factory_completes():
    """既存会話への継続は conversation_factory() の完了を待たず予約時点で conversation_id を確定する
    （factory 実行中に同じ会話への2本目が会話単位の排他をすり抜けない）。"""
    from sherpa import chat_turns
    cid = 31001
    factory_started = threading.Event()
    release_factory = threading.Event()

    def _slow_factory():
        factory_started.set()
        release_factory.wait(timeout=5)
        return cid

    def _noop_run(stop_event, emit):
        pass

    result: dict = {}

    def _drive_first():
        result["rec"] = chat_turns.start_turn(
            uid="race-u1", conversation_factory=_slow_factory,
            run_fn_factory=lambda c: _noop_run, known_conversation_id=cid)

    th = threading.Thread(target=_drive_first, daemon=True)
    th.start()
    try:
        assert factory_started.wait(timeout=5), "1本目の factory が開始しなかった"
        with pytest.raises(chat_turns.TurnLimitError) as ei:
            chat_turns.start_turn(uid="race-u2", conversation_factory=lambda: cid,
                                  run_fn_factory=lambda c: _noop_run, known_conversation_id=cid)
        assert ei.value.scope == "conversation", "factory 実行中でも同じ会話の2本目は拒否されるべき"
    finally:
        release_factory.set()
        th.join(timeout=5)
        _wait_turn_done(result["rec"].turn_id)


@pytest.mark.parametrize("scope, message", [("user", None), ("conversation", "この会話の別の回答を実行中です")])
def test_chat_turns_start_returns_429_when_limit_exceeded(monkeypatch, scope, message):
    """POST /chat/turns は TurnLimitError を 429 に変換する（会話継続は専用の文言で区別）。"""
    from sherpa import chat_turns

    def _raise(*a, **k):
        raise chat_turns.TurnLimitError(scope)

    monkeypatch.setattr(chat_turns, "start_turn", _raise)
    r = _client().post("/chat/turns", json={"message": "x", "world": V, "knowledge": False})
    assert r.status_code == 429
    if message:
        assert message in r.json()["detail"]


def test_chat_turns_start_429_when_admin_at_user_limit_creates_no_orphaned_conversation(blocker):
    """admin（compat の合成ユーザー）が上限まで実行中なら本物の HTTP も 429。上限判定は会話確定の前
    （予約方式）なので、弾かれた要求で空メッセージの会話が残らない。"""
    from sherpa import chat_turns
    for i in range(chat_turns.MAX_TURNS_PER_USER):
        blocker.start("admin", 800 + i)
    c = _client()
    before = len(c.get("/conversations").json())
    r = c.post("/chat/turns", json={"message": "orphan-check-unique-message", "world": V, "knowledge": False})
    assert r.status_code == 429
    assert len(c.get("/conversations").json()) == before, "429 で弾かれたのに会話が作られている"


def test_no_time_based_termination_and_stop_turn_frees_slot():
    """調査を経過時間だけで終了させない契約（旧 reaper は撤去済み）。started_at を過去へ倒してレジストリへ
    触れても stop_event は立たず buffer.done にもならない。終了は stop_turn を run_fn が検知する経路だけで、
    枠の解放（buffer.done）は _run() の finally の mark_done() が行う。"""
    from sherpa import chat_turns
    stop_seen = threading.Event()

    def _run_fn(stop_event, emit):
        if stop_event.wait(timeout=15):
            stop_seen.set()

    uid = "no-reaper-test-user"
    rec = _start(uid, 900101, _run_fn)
    try:
        rec.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
        chat_turns.list_running(uid)   # レジストリに触れる＝sweep が走る
        assert not rec.stop_event.is_set(), "経過時間だけで stop_event が立った（reaper の復活）"
        assert not rec.buffer.done, "経過時間だけで強制終了された（reaper の復活）"

        assert chat_turns.stop_turn(rec.turn_id, uid) is True
        _wait_turn_done(rec.turn_id, timeout=5)
        assert stop_seen.is_set(), "run_fn が stop_event を検知していない"
        assert rec.buffer.done, "stop_turn 後に枠（buffer.done）が解放されていない"
    finally:
        rec.stop_event.set()
        _wait_turn_done(rec.turn_id, timeout=5)


# ===== background thread の例外時も best-effort で永続する =====

def test_turn_crash_persists_user_and_assistant_error_message(monkeypatch):
    """stream_message 呼び出し自体の例外でも (a) user メッセージが補完され (b) assistant にエラーの最小
    envelope が保存される。error イベント＋枠解放・chat.turn 監査（outcome=error）も残る。"""
    from sherpa import store
    from sherpa.routers import chat as chat_routes

    def _boom(*a, **k):
        raise RuntimeError("boom-for-test")

    monkeypatch.setattr(chat_routes, "stream_message", _boom)
    ensure_v1()
    c = _client()
    tid, cid = _post_turn(c, message="crash-check-unique", knowledge=False)
    _wait_turn_done(tid)

    conv = c.get(f"/conversations/{cid}").json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"], "クラッシュ時に user/assistant が永続されていない"
    assert conv["messages"][0]["content"] == "crash-check-unique"
    assert "エラー" in conv["messages"][1]["content"]

    events = _drain_stream(c.get(f"/chat/turns/{tid}/stream", params={"cursor": 0}).text)
    assert events and events[-1]["type"] == "error", f"error イベントで枠解放されていない: {events}"

    rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
    assert rows, "クラッシュ時の chat.turn 監査が記録されていない"
    assert rows[0]["outcome"] == "error"


def _crash_conv(title: str) -> int:
    from sherpa import store
    return store.create_conversation(user_id="admin", world=V, title=title)["id"]


def _msgs(cid: int) -> list:
    from sherpa import store
    return get_conversation(cid)["messages"]


def _assistant(cid: int) -> dict:
    return next(m for m in _msgs(cid) if m["role"] == "assistant")


def _audit_user_id(cid: int) -> int:
    from sherpa import store
    rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
    assert rows
    return rows[0]["detail"]["message_id_user"]


def test_persist_turn_crash_sets_personal_flag_when_user_row_not_yet_saved():
    """saved_user_id が無い（on_user_saved 発火前にクラッシュ）personal ターンでも会話フラグ
    contains_personal_workspace を立てる（受領共有は会話フラグだけでブロックするため）。"""
    from sherpa import api, store
    cid = _crash_conv("crash-personal-flag")

    api._persist_turn_crash(cid, "personal-q", "admin", V, True, RuntimeError("boom"))

    got = store.get_conversation_for_read("admin", cid)
    assert got["conversation"]["contains_personal_workspace"] is True, "共有ブロックが効かない"
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    assert _msgs(cid)[1]["personal"] is True   # get_conversation_for_read は personal 列を返さない


@pytest.mark.parametrize("exc, expected", [
    (TimeoutError("deadline exceeded"), "timeout"),           # 型だけで判定（メッセージは見ない）
    (ConnectionResetError("reset"), "transport_error"),       # OSError 系
    (RuntimeError("boom"), None),                             # 立てない（利用統計側で 'unknown' に畳む契約）
])
def test_persist_turn_crash_sets_stop_kind_by_exception_type(exc, expected):
    from sherpa import api
    cid = _crash_conv("crash-stop-kind")
    api._persist_turn_crash(cid, "stop-kind-q", "admin", V, False, exc)
    answer = _assistant(cid)["answer"]
    if expected is None:
        assert "stop_kind" not in answer
    else:
        assert answer["stop_kind"] == expected


@pytest.mark.parametrize("kwargs, expected_lens, check_answer_lens", [
    ({"knowledge": True, "lens": "qa"}, "qa", True),     # 決定済み lens（"chat" 固定だと knowledge_turns/zero-hit の分母から漏れる）
    ({"knowledge": True, "lens": None}, None, True),     # 意図判定前は "chat" で偽装せず None
    ({"knowledge": False, "lens": "qa"}, "chat", False), # knowledge オフは lens 引数を使わない
    ({}, "chat", False),                                 # 引数省略の既定は従来どおり "chat"
])
def test_persist_turn_crash_lens(kwargs, expected_lens, check_answer_lens):
    from sherpa import api
    cid = _crash_conv("crash-lens")
    api._persist_turn_crash(cid, "lens-q", "admin", V, False, RuntimeError("boom"), **kwargs)
    assistant = _assistant(cid)
    assert assistant["lens"] == expected_lens
    if check_answer_lens:
        assert assistant["answer"]["lens"] == expected_lens


@pytest.mark.parametrize("extra, expected_lens", [({"lens": "qa"}, "qa"), ({}, None)])
def test_turn_crash_via_background_run_stores_decided_lens_not_chat(monkeypatch, extra, expected_lens):
    """POST /chat/turns の実背景実行（make_run）で neo4j_session() 自体が落ちても、assistant 行の lens は
    明示指定（"qa"）のまま・自動判定（未指定）は "chat" でなく None（knowledge/lens が配線されている）。"""
    from sherpa.routers import chat as chat_routes

    def _boom_neo4j(*a, **k):
        raise RuntimeError("boom-neo4j-session")
    monkeypatch.setattr(chat_routes, "neo4j_session", _boom_neo4j)

    ensure_v1()
    tid, cid = _post_turn(_client(), message=f"crash-lens-bg-unique-{expected_lens}", **extra)
    _wait_turn_done(tid)

    assert _assistant(cid)["lens"] == expected_lens


def test_persist_turn_crash_without_saved_user_id_ignores_stale_same_text_turn():
    """saved_user_id が無い（user 行保存前のクラッシュ）なら本文検索はせず常に新規保存する
    （過去の別ターンの同文行・personal 値に対応付けない）。"""
    from sherpa import api, store
    cid = _crash_conv("crash-no-callback-stale-row")
    same_text = "同文の質問（コールバック前クラッシュ）"
    stale_user = store.add_message(cid, "user", same_text, personal=False)

    api._persist_turn_crash(cid, same_text, "admin", V, True, RuntimeError("boom"))

    user_rows = [m for m in _msgs(cid) if m["role"] == "user"]
    assert len(user_rows) == 2, f"新規保存されず、古い行を再利用した: {user_rows}"
    new_user = next(m for m in user_rows if m["id"] != stale_user["id"])
    assert new_user["personal"] is True
    assistant_rows = [m for m in _msgs(cid) if m["role"] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["personal"] is True
    assert _audit_user_id(cid) == new_user["id"] != stale_user["id"]


def test_persist_turn_crash_early_returns_without_assistant_or_audit_when_user_save_fails(monkeypatch):
    """user 行の新規保存が失敗したら assistant 行・監査のどちらも作らない（何も残さない方が安全）。"""
    from sherpa import api, store
    cid = _crash_conv("crash-user-save-fails")
    real_add_message = store.add_message

    def _boom_on_user(conversation_id, role, *a, **kw):
        if role == "user":
            raise RuntimeError("db write failed")
        return real_add_message(conversation_id, role, *a, **kw)
    monkeypatch.setattr(store, "add_message", _boom_on_user)

    api._persist_turn_crash(cid, "some-question", "admin", V, False, RuntimeError("boom"))

    assert _msgs(cid) == [], "user 保存失敗にもかかわらず何か残った"
    assert store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5) == [], \
        "user 保存失敗時に監査行が残った"


def test_persist_turn_crash_same_text_concurrent_turns_uses_carried_id_not_content_match():
    """同一利用者が同文で2ターン並走（B 非 personal → A personal の順に保存）し A がクラッシュした場合、
    渡された saved_user_id（A の id）に対応付け、B の行に引きずられない（A の復旧は personal=True）。"""
    from sherpa import api, store
    cid = _crash_conv("crash-same-text-concurrent")
    b_user = store.add_message(cid, "user", "同文の質問", personal=False)
    a_user = store.add_message(cid, "user", "同文の質問", personal=True)

    api._persist_turn_crash(cid, "同文の質問", "admin", V, True, RuntimeError("boom"),
                            saved_user_id=a_user["id"], saved_user_personal=True)

    assert len([m for m in _msgs(cid) if m["role"] == "user"]) == 2, "新たな user 行が追加された"
    assistant_rows = [m for m in _msgs(cid) if m["role"] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["personal"] is True, "A（personal）の復旧が B の非personalに引きずられた"
    assert _audit_user_id(cid) == a_user["id"] != b_user["id"]


def test_persist_turn_crash_via_real_stream_message_callback_ignores_stale_same_text_turn(monkeypatch):
    """chat_service.stream_message を実際に呼び、on_user_saved の本物の callback が渡した実 id を使う
    （callback 発火後のクラッシュでも、過去の別ターンの同文の非 personal 行に対応付けない）。"""
    from sherpa import chat_service, store
    from sherpa.routers import chat as chat_routes

    cid = _crash_conv("crash-real-callback")
    same_text = "同文の質問（実callback経路）"
    stale_user = store.add_message(cid, "user", same_text, personal=False)

    def _boom(*a, **k):
        raise RuntimeError("boom-after-user-saved")
    monkeypatch.setattr(store, "get_settings", _boom)   # user 行保存の直後に呼ばれる箇所でクラッシュ

    saved: dict = {}

    def _on_user_saved(message_id, is_personal):
        saved["id"], saved["personal"] = message_id, is_personal

    caught = None
    try:
        for _ in chat_service.stream_message(None, same_text, V, conversation_id=cid,
                                             knowledge=False, user_id="admin", personal=True,
                                             on_user_saved=_on_user_saved):
            pass
    except Exception as e:
        caught = e
    assert caught is not None, "get_settings のクラッシュが伝播していない"
    assert saved.get("id") is not None, "on_user_saved が呼ばれていない"
    assert saved["id"] != stale_user["id"]
    assert saved["personal"] is True

    chat_routes._persist_turn_crash(cid, same_text, "admin", V, True, caught,
                                    saved_user_id=saved["id"], saved_user_personal=saved["personal"])

    assistant_rows = [m for m in _msgs(cid) if m["role"] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["personal"] is True
    assert _audit_user_id(cid) == saved["id"] != stale_user["id"]


def test_turn_crash_via_background_run_ignores_stale_same_text_turn(monkeypatch):
    """バックグラウンド run（POST /chat/turns）経由のクラッシュでも on_user_saved の id を使い、
    同じ会話内の過去の別ターンの同文行に対応付けない（make_run から stream_message までの配線ごと）。"""
    from sherpa import store

    cid = _crash_conv("crash-bg-stale-row")
    same_text = "同文の質問（バックグラウンドrun経由）"
    stale_user = store.add_message(cid, "user", same_text, personal=False)

    # 受付段階（_prepare_agentic_snapshot）も get_settings を1度読む＝1回目は通し、2回目以降
    # （stream_message 内・user 行保存後）から失敗させる。
    calls = {"n": 0}
    store_get_settings_orig = store.get_settings

    def _boom_from_second_call(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return store_get_settings_orig(*a, **k)
        raise RuntimeError("boom-after-user-saved-bg")
    monkeypatch.setattr(store, "get_settings", _boom_from_second_call)

    tid, _ = _post_turn(_client(), message=same_text, knowledge=False, conversation_id=cid, personal=True)
    _wait_turn_done(tid)

    user_rows = [m for m in _msgs(cid) if m["role"] == "user"]
    assert len(user_rows) == 2, f"新規保存されず、古い行を再利用した: {user_rows}"
    new_user = next(m for m in user_rows if m["id"] != stale_user["id"])
    assert new_user["personal"] is True
    assistant_rows = [m for m in _msgs(cid) if m["role"] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["personal"] is True
    assert _audit_user_id(cid) == new_user["id"] != stale_user["id"]
