"""回答の途中の本文を実行の記録に残し、起動時の回収で会話に残る本文にすることの契約テスト（実 Postgres・down なら skip）。"""
from __future__ import annotations

import os
import uuid

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest  # noqa: E402

from sherpa import chat_run_journal as J  # noqa: E402
from sherpa import store  # noqa: E402
from sherpa.providers.base import _answer_partial  # noqa: E402
from sherpa.store import chat_runs  # noqa: E402


@pytest.fixture
def conv():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")
    uid = f"u-{uuid.uuid4().hex[:8]}"
    cid = store.create_conversation(user_id=uid, world="v1", title="partial")["id"]
    store.add_message(cid, "user", "調べて")
    return {"cid": cid, "uid": uid}


def _journal(conv) -> J.RunJournal:
    return J.RunJournal(turn_id=uuid.uuid4().hex, conversation_id=conv["cid"], user_id=conv["uid"])


def _last_assistant(cid: int) -> dict:
    with store._connect() as c:
        return c.execute("SELECT content, answer FROM messages WHERE conversation_id=%s AND role='assistant' "
                         "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()


def test_recovered_message_carries_the_partial_body(conv):
    j = _journal(conv)
    j.begin()
    sent = []
    emit = j.wrap(sent.append)
    emit({"type": "answer_partial", "text": "調べた範囲では、税率は"})
    emit({"type": "answer_partial", "text": "調べた範囲では、税率は10%です。続きを確認中"})
    assert sent == []   # 画面へは流さない
    j.flush()
    assert J.recover_unfinished() and J.interrupt_run(chat_runs.get_run(j.turn_id), "process_exit") is False
    last = _last_assistant(conv["cid"])
    assert last["answer"]["body"] == "調べた範囲では、税率は10%です。続きを確認中"
    assert J.INTERRUPTED_NOTICE in last["content"]


def test_partial_text_stays_within_the_progress_budget(conv):
    j = _journal(conv)
    j.observe({"type": "answer_partial", "text": "あ" * 400_000})
    assert len(j._partial.encode("utf-8")) <= J.MAX_PARTIAL_BYTES
    assert j._bytes + j._partial_bytes <= J.MAX_PROGRESS_BYTES


def test_structured_final_json_keeps_only_the_answer_field():
    assert _answer_partial('{"status":"in_progress","answer":"途中です","claims":[]}')["text"] == "途中です"
    assert _answer_partial('{"status":"in_pro') is None
