"""`sherpa.routers.chat` の薄い単体テスト（DB/Neo4j 不要・依存を monkeypatch で差し替える）。

RV1 #10: `/chat/turns` は `chat_turns.start_turn` が枠を予約した直後に会話を作る
（`_make_conversation`）ため、`/chat`・`/chat/stream`（`stream_message`/`handle_message` 内の
`_resolve_lens` が既にスラッシュ接頭辞を除去した本文でタイトルを作る）と異なり、会話タイトル生成
だけ raw な `req.message` を使っていた（`/影響 ...` がそのままタイトルに残る）。
"""
from __future__ import annotations

from sherpa.routers import chat as RC


def test_chat_turns_start_uses_slash_stripped_message_for_conversation_title(monkeypatch):
    captured: dict = {}

    monkeypatch.setattr(RC, "_current_user", lambda request: {"uid": "u1", "role": "user"})
    monkeypatch.setattr(RC, "_check_chat_write", lambda u, cid: None)
    monkeypatch.setattr(RC, "_resolve_world", lambda w: "w1")
    # SC-6e: `chat_turns_start` は knowledge 判定と Provider 準備をまとめて
    # `_prepare_agentic_snapshot` から受け取る（`_knowledge_for` はもう呼ばない）——ここでは
    # 要求どおりの knowledge をそのまま返し、Provider 系はすべて未使用（DB 不要）にする。
    monkeypatch.setattr(RC, "_prepare_agentic_snapshot",
                        lambda uid, requested, web_search: (requested, None, None, None, None))
    monkeypatch.setattr(RC, "validated_scope", lambda w, sp: None)

    def fake_ensure_conversation(conversation_id, message, world, user_id):
        captured["message"] = message
        return 999

    monkeypatch.setattr(RC, "_ensure_conversation", fake_ensure_conversation)

    class _FakeRec:
        turn_id = "turn-x"
        conversation_id = 999

    def fake_start_turn(uid, conversation_factory, run_fn_factory):
        # 本番と同じ呼び出し順（枠予約の直後・lock の外で会話を作る）。
        conversation_factory()
        return _FakeRec()

    monkeypatch.setattr(RC.chat_turns, "start_turn", fake_start_turn)

    req = RC.ChatReq(message="/影響 消費税率を変えたい", world="w1", knowledge=False)
    out = RC.chat_turns_start(req, request=None)

    assert captured["message"] == "消費税率を変えたい"   # スラッシュ接頭辞が除かれている
    assert out == {"turn_id": "turn-x", "conversation_id": 999}


def test_chat_turns_start_no_slash_message_unchanged(monkeypatch):
    """スラッシュ接頭辞が無ければそのまま（誤って本文を壊さない）。"""
    captured: dict = {}

    monkeypatch.setattr(RC, "_current_user", lambda request: {"uid": "u1", "role": "user"})
    monkeypatch.setattr(RC, "_check_chat_write", lambda u, cid: None)
    monkeypatch.setattr(RC, "_resolve_world", lambda w: "w1")
    # SC-6e: `chat_turns_start` は knowledge 判定と Provider 準備をまとめて
    # `_prepare_agentic_snapshot` から受け取る（`_knowledge_for` はもう呼ばない）——ここでは
    # 要求どおりの knowledge をそのまま返し、Provider 系はすべて未使用（DB 不要）にする。
    monkeypatch.setattr(RC, "_prepare_agentic_snapshot",
                        lambda uid, requested, web_search: (requested, None, None, None, None))
    monkeypatch.setattr(RC, "validated_scope", lambda w, sp: None)

    def fake_ensure_conversation(conversation_id, message, world, user_id):
        captured["message"] = message
        return 999

    monkeypatch.setattr(RC, "_ensure_conversation", fake_ensure_conversation)

    class _FakeRec:
        turn_id = "turn-x"
        conversation_id = 999

    def fake_start_turn(uid, conversation_factory, run_fn_factory):
        conversation_factory()
        return _FakeRec()

    monkeypatch.setattr(RC.chat_turns, "start_turn", fake_start_turn)

    req = RC.ChatReq(message="消費税率を変えたい", world="w1", knowledge=False)
    RC.chat_turns_start(req, request=None)

    assert captured["message"] == "消費税率を変えたい"


class _FakeAgenticProvider:
    def _agentic_target_check(self) -> None:
        return None


def test_prepare_agentic_snapshot_reads_settings_exactly_once_for_knowledge_and_provider(monkeypatch):
    """`_prepare_agentic_snapshot` は settings を一度だけ読み、knowledge の実効値（Codex構成は
    常時ON）と Provider 構築の両方をその同じスナップショットから決める。

    以前は `_knowledge_for`（knowledge 判定）と `_prepare_agentic_snapshot`（Provider 構築）が
    それぞれ独立に `store.get_settings` を呼んでいたため、その間に admin が構成を変えると
    knowledge 判定と実際に構築される Provider の種類が食い違い得た（例: 1回目 codex で
    knowledge=True と判定された直後、2回目 openai で Provider が構築される・逆方向では
    Codex が knowledge=False のまま構築され常時ON契約が壊れる）。settings の読み取りが
    構造的に1回だけなら、この食い違いはそもそも起こり得ない。
    """
    calls = {"n": 0}

    def _fake_get_settings(uid):
        calls["n"] += 1
        return {"agent": "codex", "codex_model_provider": "openai"}

    monkeypatch.setattr(RC.store, "get_settings", _fake_get_settings)
    monkeypatch.setattr(RC.store, "_read_system_settings_fresh", lambda: {})
    monkeypatch.setattr(RC, "get_provider", lambda settings, system_settings=None: _FakeAgenticProvider())
    monkeypatch.setattr(RC.agentic_search, "tool_availability",
                        lambda: {"grep": True, "fulltext": True, "graph": True})

    knowledge, provider, settings, sys_settings, tools_availability = RC._prepare_agentic_snapshot(
        "u1", False, False)   # 要求は knowledge=False だが Codex 構成なので実効値は True になるはず

    assert calls["n"] == 1        # settings は一度しか読まない
    assert knowledge is True      # Codex 構成は常にON（多層防御）が正しく効く
    assert provider is not None   # knowledge の実効値どおり Provider が準備される


def test_prepare_agentic_snapshot_reads_settings_exactly_once_when_knowledge_stays_off(monkeypatch):
    """非Codex構成・要求どおり knowledge=False で終わる経路でも settings の読み取りは1回だけ
    （Provider は準備しない・`get_provider` は呼ばれない）。"""
    calls = {"n": 0}

    def _fake_get_settings(uid):
        calls["n"] += 1
        return {"agent": "openai"}

    monkeypatch.setattr(RC.store, "get_settings", _fake_get_settings)

    def _must_not_be_called(*a, **kw):
        raise AssertionError("knowledge=False では Provider を準備してはいけない")

    monkeypatch.setattr(RC, "get_provider", _must_not_be_called)

    knowledge, provider, settings, sys_settings, tools_availability = RC._prepare_agentic_snapshot(
        "u1", False, False)

    assert calls["n"] == 1
    assert knowledge is False
    assert provider is None
