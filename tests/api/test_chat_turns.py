"""チャットターンのバックグラウンド実行（覗き窓方式）。正典: docs/proposals/2026-07-03-チャット背景実行.md。

`POST /chat/turns` はターンを background thread として起動して即座に turn_id を返す。
`GET /chat/turns/{turn_id}/stream?cursor=N` はバッファを cursor から replay→追従する「尾行」に
すぎない（購読ゼロでもターンは完走・DB永続する）。`GET /chat/turns/running`／
`POST /chat/turns/{turn_id}/stop` はそれぞれ一覧・停止。

会話フローは要 Neo4j（graph-load 済）＋ Postgres 起動（test_chat_m8.py と同じ前提）。
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest
from _world_setup import TEST_WORLD_ID, ensure_v1

os.environ.setdefault("SHERPA_STREAM_PACE", "0")   # 既定は即時（テスト高速化・pace が要るテストは個別に monkeypatch）

V = TEST_WORLD_ID


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """このファイルはログインせず直接叩く前提（compat モード＝合成 admin・test_chat_m8.py と同じ流儀）。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


def _client():
    from fastapi.testclient import TestClient
    from sherpa.api import app
    return TestClient(app)


def _wait_turn_done(turn_id, timeout=5.0):
    """turn_id の background thread が完走する（buffer.done）まで待つ（テストの後始末・レジストリ汚染防止）。"""
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
    """テスト用ヘルパ: 予約方式（MEDIUM・Codex RV 修正）の `chat_turns.start_turn` を、
    「conversation_id 決め打ち・run_fn そのまま」という以前の直感的な形で呼べるようにする橋渡し。
    `start_turn` は `conversation_factory`（lock 外で呼ばれ conversation_id を確定する）と
    `run_fn_factory`（確定した conversation_id を受けて実処理を組み立てる）を取るようになった。
    """
    from sherpa import chat_turns
    return chat_turns.start_turn(uid=uid, conversation_factory=lambda: conversation_id,
                                 run_fn_factory=lambda cid: run_fn)


# ===== 完走・永続（購読ゼロでも DB に回答が残る） =====

def test_turn_completes_and_persists_answer_without_any_subscriber():
    """POST /chat/turns の直後は誰も GET /chat/turns/{id}/stream を叩かなくても、
    background thread が最後まで走り DB（messages/trace）に答えが残る（proposal の核心）。"""
    ensure_v1()
    c = _client()
    r = c.post("/chat/turns", json={"message": "消費税率を変えたい。影響は？", "world": V, "knowledge": True})
    assert r.status_code == 200
    body = r.json()
    assert body.get("turn_id") and body.get("conversation_id")

    _wait_turn_done(body["turn_id"])

    conv = c.get(f"/conversations/{body['conversation_id']}").json()
    roles = [m["role"] for m in conv["messages"]]
    assert roles == ["user", "assistant"], f"購読ゼロで完走していない: {roles}"
    assistant = conv["messages"][-1]
    assert assistant["answer"]["lens"] == "impact"
    assert assistant["answer"]["sources"], "出典が保存されていない"
    assert assistant.get("trace"), "trace が保存されていない"


def test_turn_start_response_returns_promptly_before_completion(monkeypatch):
    """POST /chat/turns はターンの完走を待たずに即座に turn_id/conversation_id を返す
    （pace を大きくしても応答が遅延しないことで確認する）。"""
    from sherpa import chat_service
    monkeypatch.setattr(chat_service, "emit_pace", lambda: 1.5)
    ensure_v1()
    c = _client()
    t0 = time.time()
    r = c.post("/chat/turns", json={"message": "消費税率を変えたい。影響は？", "world": V, "knowledge": True})
    elapsed = time.time() - t0
    assert r.status_code == 200
    assert elapsed < 1.0, f"POST /chat/turns が完走を待って遅延している: {elapsed}s"
    _wait_turn_done(r.json()["turn_id"], timeout=10)


def test_background_turn_persists_clarify_question_card_without_subscriber(monkeypatch):
    """S1（ask_user-improvements.md）: 背景ターン（覗き窓方式・購読ゼロ）でも確認カードは assistant
    メッセージとして永続化される。両経路（/chat/stream・/chat/turns）とも chat_service.stream_message
    を通るため、question 到達時の保存はここでも効く（run_fn が generator を最後まで drain する＝
    購読者が居なくても save は走る）。clarify に落とすため Tier2(LLM) を未接続化する
    （[[feedback_intent_tier2_shared_dev_key_leak]]・共有 dev DB の実キーで先取り分類されるのを防ぐ）。"""
    from sherpa import intent_llm
    monkeypatch.setattr(intent_llm, "classify", lambda *a, **k: None)
    ensure_v1()
    c = _client()
    r = c.post("/chat/turns", json={"message": "税率を変えたら夜間バッチが落ちる？",
                                    "world": V, "knowledge": True})
    assert r.status_code == 200
    tid, cid = r.json()["turn_id"], r.json()["conversation_id"]
    _wait_turn_done(tid)   # 購読者を一切張らずに完走を待つ

    conv = c.get(f"/conversations/{cid}").json()
    assistant = [m for m in conv["messages"] if m["role"] == "assistant"]
    assert len(assistant) == 1, f"背景ターンで確認カードが保存されていない（S1）: {[m['role'] for m in conv['messages']]}"
    saved = assistant[0]
    assert saved["answer"]["lens"] == "clarify"
    q = saved["answer"]["question"]
    assert q["options"] and q["prompt"]
    assert q["original_message"] == "税率を変えたら夜間バッチが落ちる？"
    assert saved.get("trace"), "背景の clarify ターンでも trace が保存されるはず（思考の流れの復元）"


# ===== 途中切断→再購読（cursor から replay・イベント欠落なし） =====

def test_turn_stream_reconnect_from_cursor_has_no_event_gap(monkeypatch):
    """最初の購読を意図的に打ち切り（＝途中切断を模す）、受け取った件数を cursor に再購読すると
    残りのイベントが欠落なく届き、最終的に answer で完走する。"""
    from sherpa import chat_service
    monkeypatch.setattr(chat_service, "emit_pace", lambda: 0.12)   # 4ノード分の実時間の窓を作る
    ensure_v1()
    c = _client()
    r = c.post("/chat/turns", json={"message": "消費税率を変えたい。影響は？", "world": V, "knowledge": True})
    tid = r.json()["turn_id"]

    first_events: list[dict] = []
    with c.stream("GET", f"/chat/turns/{tid}/stream", params={"cursor": 0}) as s:
        for line in s.iter_lines():
            if line and line.startswith("data: "):
                first_events.append(json.loads(line[6:]))
                if len(first_events) >= 2:
                    break   # ここで意図的に打ち切る（途中切断）＝ turn 自体は続く
    assert first_events, "最初の接続でイベントを受け取れなかった"
    assert first_events[-1]["type"] != "answer", "打ち切り前に完走してしまった（pace を確認）"

    cursor = len(first_events)   # 受け取った件数 = 最後に受けたイベントの seq（購読者はこの1本のみ）
    rest_events: list[dict] = []
    with c.stream("GET", f"/chat/turns/{tid}/stream", params={"cursor": cursor}) as s:
        for line in s.iter_lines():
            if line and line.startswith("data: "):
                rest_events.append(json.loads(line[6:]))

    all_types = [e["type"] for e in first_events] + [e["type"] for e in rest_events]
    assert all_types[-1] == "answer", f"再購読後に完走していない: {all_types}"
    assert all_types.count("answer") == 1, f"answer が重複配信された: {all_types}"
    _wait_turn_done(tid)


def test_completed_turn_stream_replays_all_and_ends():
    """完了済みターンへの購読は残イベントを replay してそのまま終了する（新規に増えない）。"""
    ensure_v1()
    c = _client()
    r = c.post("/chat/turns", json={"message": "こんにちは", "world": V, "knowledge": False})
    tid = r.json()["turn_id"]
    _wait_turn_done(tid)

    resp = c.get(f"/chat/turns/{tid}/stream", params={"cursor": 0})
    assert resp.status_code == 200
    events = _drain_stream(resp.text)
    assert events, "完了済みターンの replay が空だった"
    assert events[-1]["type"] == "answer"

    # cursor をイベント総数に合わせて再購読すると、新規イベントは無く即終了する（空 body）。
    resp2 = c.get(f"/chat/turns/{tid}/stream", params={"cursor": len(events)})
    assert resp2.status_code == 200
    assert _drain_stream(resp2.text) == []


# ===== 停止（turn_id 宛て・既存 stop_event 機構の流用） =====

def test_turn_stop_via_turn_id_skips_assistant_save_and_yields_stopped(monkeypatch):
    """POST /chat/turns/{turn_id}/stop は既存 stream_message(stop_event=...) と同じ意味論
    （assistant は保存されない・stopped で終わる）を turn_id 宛てに提供する。"""
    from sherpa import chat_service
    monkeypatch.setattr(chat_service, "emit_pace", lambda: 0.15)
    ensure_v1()
    c = _client()
    r = c.post("/chat/turns", json={"message": "消費税率を変えたい。影響は？", "world": V, "knowledge": True})
    tid, cid = r.json()["turn_id"], r.json()["conversation_id"]

    sr = c.post(f"/chat/turns/{tid}/stop")
    assert sr.json() == {"ok": True}

    resp = c.get(f"/chat/turns/{tid}/stream", params={"cursor": 0})
    events = _drain_stream(resp.text)
    assert events, "停止ターンからイベントを受け取れなかった"
    assert events[-1]["type"] == "stopped", f"stopped で終わっていない: {[e['type'] for e in events]}"

    conv = c.get(f"/conversations/{cid}").json()
    roles = [m["role"] for m in conv["messages"]]
    assert roles == ["user"], f"assistant が保存されている（停止なのに完走扱い）: {roles}"


def test_turn_stop_unknown_id_returns_ok_false():
    c = _client()
    r = c.post("/chat/turns/no-such-turn-id/stop")
    assert r.status_code == 200
    assert r.json() == {"ok": False}


# ===== 他人の turn 購読・停止拒否 =====

def test_turn_stream_and_stop_reject_other_users_turn():
    """他人の turn_id は購読（404・/conversations と同じ「存在有無を教えない」流儀）も
    停止（{"ok": false}・既存 /chat/stream/stop と同じ流儀）もできない。"""
    from sherpa import chat_turns
    gate = threading.Event()

    def _blocking_run(stop_event, emit):
        emit({"type": "node", "id": "x", "kind": "think", "label": "x", "detail": "x", "status": "done"})
        gate.wait(timeout=5)

    rec = _start("someone-else", 555001, _blocking_run)
    try:
        c = _client()   # compat モード = admin としてリクエストする
        r = c.get(f"/chat/turns/{rec.turn_id}/stream", params={"cursor": 0})
        assert r.status_code == 404

        sr = c.post(f"/chat/turns/{rec.turn_id}/stop")
        assert sr.json() == {"ok": False}
        assert not rec.stop_event.is_set(), "他人のターンなのに停止イベントが立ってしまった"
    finally:
        gate.set()
        _wait_turn_done(rec.turn_id)


# ===== GET /chat/turns/running（トップバー表示・再接続の両方が使う） =====

def test_running_turns_endpoint_reflects_lifecycle(monkeypatch):
    """開始直後は running 一覧に turn_id が出て、完了後は消える。"""
    from sherpa import chat_service
    monkeypatch.setattr(chat_service, "emit_pace", lambda: 0.15)
    ensure_v1()
    c = _client()
    r = c.post("/chat/turns", json={"message": "消費税率を変えたい。影響は？", "world": V, "knowledge": True})
    tid, cid = r.json()["turn_id"], r.json()["conversation_id"]

    running = c.get("/chat/turns/running").json()["turns"]
    hit = next((t for t in running if t["turn_id"] == tid), None)
    assert hit is not None, "開始直後は running 一覧に出るはず"
    assert hit["conversation_id"] == cid
    assert hit.get("started_at")

    _wait_turn_done(tid, timeout=10)
    running2 = c.get("/chat/turns/running").json()["turns"]
    assert not any(t["turn_id"] == tid for t in running2), "完了後も running 一覧に残っている"


def test_running_turns_endpoint_is_scoped_to_current_user():
    """他人のターンは自分の running 一覧に出ない。"""
    from sherpa import chat_turns
    gate = threading.Event()

    def _blocking_run(stop_event, emit):
        gate.wait(timeout=5)

    rec = _start("someone-else-2", 555002, _blocking_run)
    try:
        c = _client()
        running = c.get("/chat/turns/running").json()["turns"]
        assert not any(t["turn_id"] == rec.turn_id for t in running)
    finally:
        gate.set()
        _wait_turn_done(rec.turn_id)


# ===== 同時実行上限（1ユーザー2・全体8） =====

def test_start_turn_enforces_per_user_limit():
    """chat_turns.start_turn は同一 uid の未完了ターンが MAX_TURNS_PER_USER に達すると
    TurnLimitError(scope='user') を送出する（レジストリの数え方そのものを直接確認する）。"""
    from sherpa import chat_turns
    gate = threading.Event()
    recs = []

    def _blocking_run(stop_event, emit):
        gate.wait(timeout=5)

    try:
        for i in range(chat_turns.MAX_TURNS_PER_USER):
            recs.append(_start("limituser-a", 600 + i, _blocking_run))
        with pytest.raises(chat_turns.TurnLimitError) as ei:
            _start("limituser-a", 699, _blocking_run)
        assert ei.value.scope == "user"
        # 別ユーザーは影響を受けない。
        other = _start("limituser-b", 698, _blocking_run)
        recs.append(other)
    finally:
        gate.set()
        for rec in recs:
            _wait_turn_done(rec.turn_id)


def test_start_turn_enforces_global_limit():
    """全体の未完了ターン数が MAX_TURNS_GLOBAL に達すると、別ユーザーでも TurnLimitError(scope='global')。

    他ファイル/他テストの残留ターンに影響されないよう、開始時点の未完了数を実測してから
    不足分だけ埋める（決め打ちで「今 0 件のはず」とは仮定しない）。
    """
    from sherpa import chat_turns
    gate = threading.Event()
    recs = []

    def _blocking_run(stop_event, emit):
        gate.wait(timeout=10)

    try:
        with chat_turns._REGISTRY_LOCK:
            current = sum(1 for r in chat_turns._REGISTRY.values() if not r.buffer.done)
        need = chat_turns.MAX_TURNS_GLOBAL - current
        assert need >= 1, "既に全体上限に達した状態でテストを開始できない（残留ターンを確認）"
        for i in range(need):
            recs.append(_start(f"globallimituser{i}", 700 + i, _blocking_run))
        with pytest.raises(chat_turns.TurnLimitError) as ei:
            _start("yet-another-user", 799, _blocking_run)
        assert ei.value.scope == "global"
    finally:
        gate.set()
        for rec in recs:
            _wait_turn_done(rec.turn_id)


def test_chat_turns_start_returns_429_when_limit_exceeded(monkeypatch):
    """POST /chat/turns は chat_turns.TurnLimitError を 429 に変換する（HTTP 層の配線確認）。"""
    from sherpa import chat_turns

    def _raise(*a, **k):
        raise chat_turns.TurnLimitError("user")

    monkeypatch.setattr(chat_turns, "start_turn", _raise)
    c = _client()
    r = c.post("/chat/turns", json={"message": "x", "world": V, "knowledge": False})
    assert r.status_code == 429


def test_chat_turns_start_429_when_admin_already_at_user_limit():
    """実際に admin（compat モードの合成ユーザー）が上限まで実行中のとき、本物の HTTP エンドポイントも
    429 を返す（レジストリ状態と HTTP 層の両方を通した end-to-end 確認）。"""
    from sherpa import chat_turns
    gate = threading.Event()

    def _blocking_run(stop_event, emit):
        gate.wait(timeout=5)

    recs = [_start("admin", 800 + i, _blocking_run) for i in range(chat_turns.MAX_TURNS_PER_USER)]
    try:
        c = _client()   # compat モード = admin
        r = c.post("/chat/turns", json={"message": "x", "world": V, "knowledge": False})
        assert r.status_code == 429
    finally:
        gate.set()
        for rec in recs:
            _wait_turn_done(rec.turn_id)


def test_chat_turns_start_429_does_not_create_orphaned_conversation():
    """MEDIUM（Codex RV）: 予約方式（`chat_turns.start_turn` が lock 内で上限判定＋枠登録を atomic に
    行い、conversation_factory は lock の外＝予約成功後にしか呼ばれない）により、429（上限超過）は
    会話を確定させる前に弾かれる＝空メッセージの会話が残らない。"""
    from sherpa import chat_turns
    gate = threading.Event()

    def _blocking_run(stop_event, emit):
        gate.wait(timeout=5)

    recs = [_start("admin", 820 + i, _blocking_run) for i in range(chat_turns.MAX_TURNS_PER_USER)]
    try:
        c = _client()
        before = len(c.get("/conversations").json())
        r = c.post("/chat/turns", json={"message": "orphan-check-unique-message", "world": V, "knowledge": False})
        assert r.status_code == 429
        after = len(c.get("/conversations").json())
        assert after == before, "429 で弾かれたのに会話が作られている（空会話が残る副作用）"
    finally:
        gate.set()
        for rec in recs:
            _wait_turn_done(rec.turn_id)


# ===== HIGH（Codex RV）: 固まった provider の reaper（2段階解放） =====

def test_sweep_reaper_two_phase_stop_then_force_done_frees_slot():
    """固まった provider（stop_event を一切見ない run_fn）は、started_at を過去に差し替えて
    レジストリへ触れる（sweep が走る）と、まず MAX_TURN_SECONDS 超過で stop_event.set()
    （協調停止要求・まだ未完了＝枠は埋まったまま）、さらに +FORCE_DONE_GRACE_SECONDS 超過で
    buffer にタイムアウト error を積んで強制 mark_done（枠解放）される。解放後は同じユーザーの
    新規ターンが上限に引っかからず通ることまで確認する。"""
    from datetime import datetime, timedelta, timezone

    from sherpa import chat_turns
    gate = threading.Event()

    def _wedged_run(stop_event, emit):
        gate.wait(timeout=15)   # stop_event を一切見ない＝協調停止に応じない「固まった」provider を模す

    uid = "reaper-test-user"
    rec = _start(uid, 900101, _wedged_run)
    fillers: list = []
    filler_gate = threading.Event()
    try:
        # フェーズ1: MAX_TURN_SECONDS は超えたが GRACE 内 → 協調停止要求のみ（まだ未完了）。
        rec.started_at = datetime.now(timezone.utc) - timedelta(seconds=chat_turns.MAX_TURN_SECONDS + 1)
        chat_turns.list_running(uid)   # レジストリに触れる＝sweep が走る
        assert rec.stop_event.is_set(), "MAX_TURN_SECONDS 超過で stop_event が立っていない"
        assert not rec.buffer.done, "GRACE 期間内なのに既に強制終了されている"

        # この時点で枠はまだ埋まったまま（このターンは未完了のカウントに入る）＝残り枠を埋めると
        # 同ユーザーの新規ターンは弾かれることを確認（reaper 前と挙動が変わっていないことの確認）。
        for i in range(chat_turns.MAX_TURNS_PER_USER - 1):
            fillers.append(_start(uid, 900110 + i, lambda se, em: filler_gate.wait(timeout=5)))
        with pytest.raises(chat_turns.TurnLimitError):
            _start(uid, 900199, lambda se, em: None)

        # フェーズ2: MAX_TURN_SECONDS + GRACE も超えた → 強制 done（枠解放）。
        rec.started_at = datetime.now(timezone.utc) - timedelta(
            seconds=chat_turns.MAX_TURN_SECONDS + chat_turns.FORCE_DONE_GRACE_SECONDS + 1)
        chat_turns.list_running(uid)
        assert rec.buffer.done, "GRACE も超えたのに強制終了されていない"
        events = [e.payload for e in rec.buffer.replay_from(0)]
        assert events and events[-1]["type"] == "error", f"タイムアウト error イベントが積まれていない: {events}"

        # 枠が解放されたので、新規ターンが通る。
        new_rec = _start(uid, 900198, lambda se, em: None)
        _wait_turn_done(new_rec.turn_id)
    finally:
        gate.set()
        filler_gate.set()
        _wait_turn_done(rec.turn_id, timeout=5)
        for r in fillers:
            _wait_turn_done(r.turn_id, timeout=5)


# ===== HIGH（Codex RV）: background thread の例外時も best-effort で永続する =====

def test_turn_crash_persists_user_and_assistant_error_message(monkeypatch):
    """background thread が stream_message 呼び出し自体で例外を投げても、(a) このターンの user
    メッセージが補完され、(b) assistant にエラーの最小 envelope が保存される（POST /chat/turns は
    conversation_id を返したのに会話が空、という事故を防ぐ）。chat_turns 側の枠解放（error イベント＋
    mark_done）も従来どおり動くことも併せて確認する。"""
    from sherpa import store
    from sherpa.routers import chat as chat_routes

    def _boom(*a, **k):
        raise RuntimeError("boom-for-test")

    # ハンドラ（POST /chat/turns の背景実行）は sherpa/routers/chat.py 内で解決された stream_message
    # を参照する（フェーズ3スライス7で router へ移動・api.stream_message はもう存在しない束縛）。
    monkeypatch.setattr(chat_routes, "stream_message", _boom)
    ensure_v1()
    c = _client()
    r = c.post("/chat/turns", json={"message": "crash-check-unique", "world": V, "knowledge": False})
    assert r.status_code == 200
    tid, cid = r.json()["turn_id"], r.json()["conversation_id"]
    _wait_turn_done(tid)

    conv = c.get(f"/conversations/{cid}").json()
    roles = [m["role"] for m in conv["messages"]]
    assert roles == ["user", "assistant"], f"クラッシュ時に user/assistant が永続されていない: {roles}"
    assert conv["messages"][0]["content"] == "crash-check-unique"
    assert "エラー" in conv["messages"][1]["content"]

    resp = c.get(f"/chat/turns/{tid}/stream", params={"cursor": 0})
    events = _drain_stream(resp.text)
    assert events and events[-1]["type"] == "error", f"error イベントで枠解放されていない: {events}"

    rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
    assert rows, "クラッシュ時の chat.turn 監査が記録されていない"
    assert rows[0]["outcome"] == "error"


# ===== MEDIUM（Codex RV）: SSE 購読の切断検知（keepalive） =====

def test_iter_sse_yields_keepalive_when_no_progress():
    """新規イベントも完了も無いまま wait がタイムアウトすると keepalive コメント行を yield する
    （クライアント切断後もこの generator が居座り続けないための安全弁）。"""
    from sherpa import chat_turns
    gate = threading.Event()

    def _blocking_run(stop_event, emit):
        emit({"type": "node", "id": "x", "kind": "think", "label": "x", "detail": "x", "status": "done"})
        gate.wait(timeout=5)

    rec = _start("keepalive-user", 900301, _blocking_run)
    try:
        gen = chat_turns.iter_sse(rec.turn_id, "keepalive-user", cursor=0, wait_timeout=0.05)
        assert gen is not None
        first = next(gen)
        assert first.startswith("data: "), f"最初のイベントが data: で始まらない: {first!r}"
        second = next(gen)
        assert second == ": keepalive\n\n", f"keepalive が来ていない: {second!r}"
        third = next(gen)
        assert third == ": keepalive\n\n", f"keepalive が継続して来ない: {third!r}"
    finally:
        gate.set()
        _wait_turn_done(rec.turn_id)


def test_persist_turn_crash_sets_personal_flag_when_user_row_not_yet_saved():
    """personal ターンのクラッシュ永続は、`saved_user_id` が無い（＝ user 行をまだ一度も
    保存していない・on_user_saved 発火前にクラッシュした）場合でも会話フラグ
    contains_personal_workspace を立てる（受領共有は会話フラグだけでブロックするため、
    立てないと共有先に personal な質問文が見え得る）。"""
    from sherpa import api, store
    conv = store.create_conversation(user_id="admin", world=V, title="crash-personal-flag")
    cid = conv["id"]

    api._persist_turn_crash(cid, "personal-q", "admin", V, True, RuntimeError("boom"))

    got = store.get_conversation_for_read("admin", cid)
    assert got["conversation"]["contains_personal_workspace"] is True, \
        "personal クラッシュ後に会話フラグが立っていない（共有ブロックが効かない）"
    roles = [m["role"] for m in got["messages"]]
    assert roles == ["user", "assistant"]
    msgs = store.get_conversation(cid)["messages"]   # get_conversation_for_read は personal 列を返さない
    assert msgs[1]["personal"] is True


def test_persist_turn_crash_without_saved_user_id_ignores_stale_same_text_turn():
    """`saved_user_id` が無い（`stream_message` が user 行保存**前**にクラッシュした＝
    on_user_saved が一度も呼ばれなかった）場合、この run は user 行を保存していないことが
    確定しているため、過去の**別ターン**に同文の user 行があっても本文検索はせず常に
    新規保存する（本文一致で誤って別ターンの行・personal 値に対応付けない）。"""
    from sherpa import api, store
    conv = store.create_conversation(user_id="admin", world=V, title="crash-no-callback-stale-row")
    cid = conv["id"]
    same_text = "同文の質問（コールバック前クラッシュ）"
    stale_user = store.add_message(cid, "user", same_text, personal=False)

    api._persist_turn_crash(cid, same_text, "admin", V, True, RuntimeError("boom"))

    msgs = store.get_conversation(cid)["messages"]
    user_rows = [m for m in msgs if m["role"] == "user"]
    assert len(user_rows) == 2, f"新規保存されず、古い行を再利用した: {user_rows}"
    new_user = next(m for m in user_rows if m["id"] != stale_user["id"])
    assert new_user["personal"] is True
    assistant_rows = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["personal"] is True

    audit_rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
    assert audit_rows
    assert audit_rows[0]["detail"]["message_id_user"] == new_user["id"]
    assert audit_rows[0]["detail"]["message_id_user"] != stale_user["id"]


def test_persist_turn_crash_early_returns_without_assistant_or_audit_when_user_save_fails(monkeypatch):
    """user 行の保存自体（`saved_user_id` が無い場合の新規保存）が失敗したら、assistant 行・
    監査のどちらも作らずに終わる（ID が確定しないまま作ると、後から別ターンの行に誤って
    紐付ける余地を残すより「何も残さない」方が安全）。"""
    from sherpa import api, store
    conv = store.create_conversation(user_id="admin", world=V, title="crash-user-save-fails")
    cid = conv["id"]

    real_add_message = store.add_message

    def _boom_on_user(conversation_id, role, *a, **kw):
        if role == "user":
            raise RuntimeError("db write failed")
        return real_add_message(conversation_id, role, *a, **kw)
    monkeypatch.setattr(store, "add_message", _boom_on_user)

    api._persist_turn_crash(cid, "some-question", "admin", V, False, RuntimeError("boom"))

    msgs = store.get_conversation(cid)["messages"]
    assert msgs == [], f"user 保存失敗にもかかわらず何か残ってしまった: {msgs}"
    audit_rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
    assert audit_rows == [], "user 保存失敗時に監査行が残ってしまった"


def test_persist_turn_crash_same_text_concurrent_turns_uses_carried_id_not_content_match():
    """同一利用者が同文で2ターンを並走させ、B（非 personal）→A（personal）の順で user 行が
    保存された後に A がクラッシュした場合、`saved_user_id`（stream_message の on_user_saved
    コールバックで A のターン自身が直接受け取った値という想定）を渡せば、B の行
    （id が小さい方＝先に保存された行）を誤って対応付けない。A 専用の復旧行が A の user 行
    id・personal=True になる（`on_user_saved` を経由しない直接呼び出しでこの契約を補助的に
    固定する・本物の callback を通す経路は
    `test_persist_turn_crash_via_real_stream_message_callback_ignores_stale_same_text_turn`
    が担う）。"""
    from sherpa import api, store
    conv = store.create_conversation(user_id="admin", world=V, title="crash-same-text-concurrent")
    cid = conv["id"]
    b_user = store.add_message(cid, "user", "同文の質問", personal=False)   # ターンB（非personal）が先に保存
    a_user = store.add_message(cid, "user", "同文の質問", personal=True)    # ターンA（personal）が後に保存

    api._persist_turn_crash(cid, "同文の質問", "admin", V, True, RuntimeError("boom"),
                            saved_user_id=a_user["id"], saved_user_personal=True)

    msgs = store.get_conversation(cid)["messages"]
    user_rows = [m for m in msgs if m["role"] == "user"]
    assert len(user_rows) == 2, f"新たな user 行が追加されてしまった: {user_rows}"
    assistant_rows = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["personal"] is True, "A（personal）の復旧が B の非personalに引きずられた"

    audit_rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
    assert audit_rows
    assert audit_rows[0]["detail"]["message_id_user"] == a_user["id"]
    assert audit_rows[0]["detail"]["message_id_user"] != b_user["id"]


def test_persist_turn_crash_via_real_stream_message_callback_ignores_stale_same_text_turn(monkeypatch):
    """`chat_service.stream_message` を実際に呼び、`on_user_saved` が本物のコールバックとして
    機能することを確認する。callback 発火後（＝ user 行を実際に保存した後）にクラッシュしても、
    過去の別ターンに同文の非 personal な user 行があれば、callback が渡した実 id を使い、
    その別ターンの行に誤って対応付けない。"""
    from sherpa import chat_service, store
    from sherpa.routers import chat as chat_routes

    conv = store.create_conversation(user_id="admin", world=V, title="crash-real-callback")
    cid = conv["id"]
    same_text = "同文の質問（実callback経路）"
    stale_user = store.add_message(cid, "user", same_text, personal=False)

    def _boom(*a, **k):
        raise RuntimeError("boom-after-user-saved")
    monkeypatch.setattr(store, "get_settings", _boom)   # user 行保存の直後に呼ばれる箇所でクラッシュさせる

    saved: dict = {}

    def _on_user_saved(message_id, is_personal):
        saved["id"] = message_id
        saved["personal"] = is_personal

    caught = None
    try:
        for _ in chat_service.stream_message(None, same_text, V, conversation_id=cid,
                                             knowledge=False, user_id="admin", personal=True,
                                             on_user_saved=_on_user_saved):
            pass
    except Exception as e:
        caught = e
    assert caught is not None, "get_settings のクラッシュが伝播していない"
    assert saved.get("id") is not None, "on_user_saved が呼ばれていない（本物の callback が発火しなかった）"
    assert saved["id"] != stale_user["id"]
    assert saved["personal"] is True

    chat_routes._persist_turn_crash(cid, same_text, "admin", V, True, caught,
                                    saved_user_id=saved["id"], saved_user_personal=saved["personal"])

    msgs = store.get_conversation(cid)["messages"]
    assistant_rows = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["personal"] is True

    audit_rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
    assert audit_rows
    assert audit_rows[0]["detail"]["message_id_user"] == saved["id"]
    assert audit_rows[0]["detail"]["message_id_user"] != stale_user["id"]


def test_turn_crash_via_background_run_ignores_stale_same_text_turn(monkeypatch):
    """バックグラウンド run（`POST /chat/turns` の実背景実行）経由でクラッシュした場合も、
    `on_user_saved` で直接受け取った id を使い、同じ会話内の過去の別ターンの同文行に
    誤って対応付けない（`_turn_run_fn.make_run` から `stream_message` までの配線を丸ごと通す）。"""
    from sherpa import store

    conv = store.create_conversation(user_id="admin", world=V, title="crash-bg-stale-row")
    cid = conv["id"]
    same_text = "同文の質問（バックグラウンドrun経由）"
    stale_user = store.add_message(cid, "user", same_text, personal=False)

    # `_prepare_agentic_snapshot`（受付段階・routers/chat.py）も `store.get_settings` を
    # 一度だけ読む——ここを無条件に失敗させると受付自体が 500 で止まり、この試験が狙う
    # 「バックグラウンド run 側で（on_user_saved 後に）クラッシュする」状況を作れない。
    # 1回目（受付）は成功させ、2回目以降（`stream_message` 内・user 行保存後の読み取り）から
    # 失敗させる。
    calls = {"n": 0}

    def _boom_from_second_call(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return store_get_settings_orig(*a, **k)
        raise RuntimeError("boom-after-user-saved-bg")
    store_get_settings_orig = store.get_settings
    monkeypatch.setattr(store, "get_settings", _boom_from_second_call)

    c = _client()
    r = c.post("/chat/turns", json={"message": same_text, "world": V, "knowledge": False,
                                    "conversation_id": cid, "personal": True})
    assert r.status_code == 200, r.text
    tid = r.json()["turn_id"]
    _wait_turn_done(tid)

    msgs = store.get_conversation(cid)["messages"]
    user_rows = [m for m in msgs if m["role"] == "user"]
    assert len(user_rows) == 2, f"新規保存されず、古い行を再利用した: {user_rows}"
    new_user = next(m for m in user_rows if m["id"] != stale_user["id"])
    assert new_user["personal"] is True
    assistant_rows = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["personal"] is True

    audit_rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
    assert audit_rows
    assert audit_rows[0]["detail"]["message_id_user"] == new_user["id"]
    assert audit_rows[0]["detail"]["message_id_user"] != stale_user["id"]


def test_start_turn_skips_spawn_when_reservation_force_done_during_factory():
    """RV r2 MEDIUM: conversation_factory の実行中（予約が枠を持っている間）に reaper が予約を
    強制解放（force-done）した場合、factory 復帰後に実処理 thread を起動しない
    （起動すると limit/running/stop の管理外で LLM 実行が走る「枠外実行」になる）。"""
    from sherpa import chat_turns
    uid = "expired-res-user"
    spawned: list[int] = []

    def factory() -> int:
        # factory が固まっている間に reaper が動いたことを、予約レコードの force-done で模す。
        rec = next(r for r in chat_turns._REGISTRY.values() if r.uid == uid)
        rec.buffer.append({"type": "error", "message": "応答がタイムアウトしました。もう一度お試しください。"})
        rec.buffer.mark_done()
        return 900901

    def run_fn_factory(cid: int):
        spawned.append(cid)
        def run(stop_event, emit):
            emit({"type": "node"})
        return run

    rec = chat_turns.start_turn(uid=uid, conversation_factory=factory, run_fn_factory=run_fn_factory)
    assert rec.buffer.done is True
    assert spawned == [], "予約が強制解放済みなのに実処理が起動された（枠外実行）"
    # 購読側はタイムアウトの error イベント replay で完結できる（graceful degradation）。
    assert any(e.payload.get("type") == "error" for e in rec.buffer.replay_from(0))
