"""`_finalize` が、取り込みが途中で止まっている資料フォルダの回答に注記（`ingest_incomplete`）を足す。"""
from __future__ import annotations

import logging

import pytest

from sherpa import chat_service as CS

_DECISION = {"lens": "qa", "reason": "テスト"}


def _env(*, world="w1", source="all"):
    scope = {"scope_paths": [], "source": source}
    if world is not None:
        scope["world"] = world
    return {"headline": "標準税率は 10% です。", "summary": {"total": 1},
            "data": {"citations": [{"doc_id": "a.md"}]}, "sources": [], "scope": scope}


def _kinds(out):
    return [n["kind"] for n in out.get("notices", [])]


@pytest.fixture
def state(monkeypatch):
    calls = []

    def _set(value):
        def _read(world, **kw):
            calls.append((world, kw))
            if isinstance(value, Exception):
                raise value
            return value
        monkeypatch.setattr(CS.store, "unfinished_state", _read)
        return calls
    return _set


def test_no_notice_when_nothing_is_unfinished(state):
    calls = state(None)

    out = CS._finalize(_env(), _DECISION)

    assert "ingest_incomplete" not in _kinds(out)
    assert [c[0] for c in calls] == ["w1"]


@pytest.mark.parametrize("value, phrase", [("whole", "検索の一部が古い内容のまま"), ("doc", "一部の資料が古いまま")])
def test_unfinished_world_adds_a_notice_and_keeps_the_body(state, value, phrase):
    state(value)

    out = CS._finalize(_env(), _DECISION)

    notice = next(n for n in out["notices"] if n["kind"] == "ingest_incomplete")
    assert "取り込みが途中で止まっている" in notice["text"] and phrase in notice["text"]
    assert out["body"] == "標準税率は 10% です。"
    assert notice["text"] in out["headline"]


def test_the_read_is_bounded_by_a_short_limit(state):
    calls = state("whole")

    CS._finalize(_env(), _DECISION)

    kw = calls[0][1]
    assert 0 < kw["connect_timeout"] <= 3 and 0 < kw["statement_timeout_ms"] <= 3000


def test_no_notice_and_no_read_when_knowledge_is_off(state):
    calls = state("whole")

    out = CS._finalize(_env(source="off"), _DECISION)

    assert "ingest_incomplete" not in _kinds(out) and calls == []


def test_no_notice_and_no_read_without_a_world(state):
    calls = state("whole")

    out = CS._finalize(_env(world=None), _DECISION)

    assert "ingest_incomplete" not in _kinds(out) and calls == []


def test_a_failed_read_is_a_warning_and_the_answer_is_still_returned(state, caplog):
    state(RuntimeError("db down"))

    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = CS._finalize(_env(), _DECISION)

    assert "ingest_incomplete" not in _kinds(out)
    assert out["body"] == "標準税率は 10% です。"
    assert any("取り込みの状態を読めませんでした" in r.getMessage() for r in caplog.records)
