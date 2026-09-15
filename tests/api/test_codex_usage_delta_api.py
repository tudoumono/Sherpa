"""Codex 累計 usage の会話単位の永続化（`store.get_codex_usage_total`）。

`docs/proposals/2026-09-07-Codex途中経過で止まる.md` §3.5 の是正: resume ターンの `answer.usage` を
ターン差分にするため、`CodexProvider` は直近ターンの累計（`answer.codex_usage_total`）を次ターンへ
持ち越す（`chat_service` が `Ctx.codex_usage_prev_total` として前渡しする）。
`test_conversations_search.py`/`test_auth_sharing.py` と同じ流儀: store 層の関数を直接呼んでデータ
契約を固定する。要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

from _common import _try_init

from sherpa import store


def _mk_conv(uid: str = "codex-usage-delta-admin") -> int:
    return store.create_conversation(user_id=uid, world="v1", title="codex usage delta test")["id"]


def test_get_codex_usage_total_none_for_conversation_without_messages():
    if not _try_init():
        return
    cid = _mk_conv()
    assert store.get_codex_usage_total(cid) is None


def test_get_codex_usage_total_none_when_latest_assistant_message_lacks_it():
    """Codex 以外の provider（累計 usage を持たない）ターンの後は None（無ければ None の契約）。"""
    if not _try_init():
        return
    cid = _mk_conv()
    store.add_message(cid, "user", "質問")
    store.add_message(cid, "assistant", "回答", answer={"headline": "回答"})
    assert store.get_codex_usage_total(cid) is None


def test_get_codex_usage_total_returns_latest_assistant_value():
    """複数ターンあれば最新の assistant メッセージの値を返す（最初のターンの値ではない）。"""
    if not _try_init():
        return
    cid = _mk_conv()
    store.add_message(cid, "user", "質問1")
    store.add_message(cid, "assistant", "回答1", answer={
        "headline": "回答1",
        "codex_usage_total": {"session_id": "sid-1", "input_tokens": 10,
                              "cached_input_tokens": 2, "output_tokens": 5,
                              "reasoning_output_tokens": 1}})
    store.add_message(cid, "user", "質問2")
    store.add_message(cid, "assistant", "回答2", answer={
        "headline": "回答2",
        "codex_usage_total": {"session_id": "sid-1", "input_tokens": 30,
                              "cached_input_tokens": 2, "output_tokens": 13,
                              "reasoning_output_tokens": 3}})

    assert store.get_codex_usage_total(cid) == {
        "session_id": "sid-1", "input_tokens": 30, "cached_input_tokens": 2,
        "output_tokens": 13, "reasoning_output_tokens": 3}


def test_get_codex_usage_total_uses_latest_assistant_row_even_if_a_newer_user_row_exists():
    """最新行が user メッセージ（次の質問）でも、role='assistant' 限定のクエリなので、
    その前の assistant メッセージの値を返す（「最新の行」ではなく「最新の assistant メッセージ」
    という契約を固定する）。"""
    if not _try_init():
        return
    cid = _mk_conv()
    store.add_message(cid, "user", "質問1")
    store.add_message(cid, "assistant", "回答1", answer={
        "headline": "回答1",
        "codex_usage_total": {"session_id": "sid-1", "input_tokens": 10,
                              "cached_input_tokens": 0, "output_tokens": 5,
                              "reasoning_output_tokens": 0}})
    store.add_message(cid, "user", "質問2（回答前）")

    assert store.get_codex_usage_total(cid) == {
        "session_id": "sid-1", "input_tokens": 10, "cached_input_tokens": 0,
        "output_tokens": 5, "reasoning_output_tokens": 0}
