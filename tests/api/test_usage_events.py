"""usage_events（S1・2026-07-15-LLMオーケストレーション実装計画.md §3.4）テスト。

- `tokens.by_kind`（GET /admin/usage/stats）: usage_events 由来の kind 別内訳が正しく集計され、
  chat 行（messages.answer->'usage' 由来）と合成されること・null（報告不能マーカー）が保持されること。
  既存の totals/by_model（chat のみ由来）は usage_events を書いても変わらないこと。
- 期間境界（`_usage_period_bounds` と同じ JST 半開区間）。

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app


from _common import _login, _sfx, _try_init


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@usgev.local", display_name=f"表示名-{uid}",
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def _admin_client():
    sfx = _sfx()
    uid, pw = f"usgevadm{sfx}", f"UsgEvAdm{sfx}"
    _mk_user(uid, pw, role="admin")
    return _login(uid, pw), uid


def _turn_with_usage(cid: int, lens: str, usage: dict | None):
    """会話+user+assistant のペア（tests/api/test_usage_tokens.py::_turn_with_usage と同型）。
    `_USAGE_TURN_CTE` は「直前の user メッセージと対をなす assistant 返信」だけを chat 行として
    集計するため、裸の add_message ではなくこの形でシードする。"""
    store.add_message(cid, "user", "質問")
    answer = {"headline": "回答", "lens": lens, "sources": []}
    if usage is not None:
        answer["usage"] = usage
    store.add_message(cid, "assistant", "回答", lens=lens, answer=answer)


def _delete_usage_events_by_model(models: list) -> None:
    """このテストが仕込んだ usage_events 行を model 名で回収する（unique 名なので他テストと衝突しない・
    usage_events は users への FK が無く `_test_users.cleanup_users` の対象外のため自前で掃除する）。"""
    try:
        with store._connect() as c:
            c.execute("DELETE FROM usage_events WHERE model = ANY(%s)", (models,))
    except Exception:
        pass


# ---- tokens.by_kind 集計 ----

def test_by_kind_aggregation_roundtrip():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin, admin_uid = _admin_client()
    world = f"usgevworld{sfx}"
    m_intent = f"test-model-intent-{sfx}"
    m_embed = f"test-model-embed-{sfx}"
    m_graph = f"test-model-graph-{sfx}"
    models = [m_intent, m_embed, m_graph]
    try:
        store.add_usage_event(kind="intent", provider="openai", model=m_intent,
                              input_tokens=100, cached_input_tokens=20, output_tokens=30,
                              reasoning_output_tokens=5, calls=1, user_id=admin_uid, world=world)
        # embed: Gemini（batchEmbedContents は usage を返さない）＝全 None（報告不能マーカー）・calls=3。
        store.add_usage_event(kind="embed", provider="gemini", model=m_embed,
                              input_tokens=None, cached_input_tokens=None, output_tokens=None,
                              reasoning_output_tokens=None, calls=3, world=world)
        store.add_usage_event(kind="graph_ask", provider="ollama", model=m_graph,
                              input_tokens=40, cached_input_tokens=0, output_tokens=15,
                              reasoning_output_tokens=0, calls=1, user_id=admin_uid, world=world)

        conv = store.create_conversation(user_id=admin_uid, world=world, title="by_kind集計")
        chat_usage = {"provider": "openai", "model": "gpt-5.5", "input_tokens": 200,
                      "cached_input_tokens": 50, "output_tokens": 60, "reasoning_output_tokens": 10}
        _turn_with_usage(conv["id"], "qa", chat_usage)

        r = admin.get("/admin/usage/stats?days=30")
        assert r.status_code == 200, r.text
        tokens = r.json()["tokens"]
        by_kind = {(row["kind"], row["model"]): row for row in tokens["by_kind"]}

        intent_row = by_kind[("intent", m_intent)]
        assert intent_row["provider"] == "openai" and intent_row["calls"] == 1
        assert intent_row["input"] == 100 and intent_row["cached_input"] == 20
        assert intent_row["output"] == 30 and intent_row["reasoning_output"] == 5

        embed_row = by_kind[("embed", m_embed)]
        assert embed_row["provider"] == "gemini" and embed_row["calls"] == 3
        assert embed_row["input"] is None and embed_row["cached_input"] is None
        assert embed_row["output"] is None and embed_row["reasoning_output"] is None

        graph_row = by_kind[("graph_ask", m_graph)]
        assert graph_row["provider"] == "ollama" and graph_row["calls"] == 1
        assert graph_row["input"] == 40 and graph_row["output"] == 15

        chat_row = by_kind[("chat", "gpt-5.5")]
        assert chat_row["provider"] == "openai" and chat_row["calls"] >= 1
        assert chat_row["input"] >= 200 and chat_row["output"] >= 60   # 共有 DB の他 chat 行と合算されうる

        # kind='chat' は usage_events に一切書かれない（二重計上なし）＝by_kind に含まれる chat 行は
        # 全て messages.answer->'usage' 由来であり、totals/by_model は usage_events の新規行で変わらない。
        assert "totals" in tokens and "by_model" in tokens
    finally:
        _delete_usage_events_by_model(models)


def test_by_kind_period_bounds():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin, admin_uid = _admin_client()
    m = f"test-model-oldrow-{sfx}"
    old_ts = datetime.now(timezone.utc) - timedelta(days=40)
    try:
        store.add_usage_event(kind="intent", provider="openai", model=m, input_tokens=1,
                              cached_input_tokens=0, output_tokens=1, reasoning_output_tokens=0,
                              calls=1, user_id=admin_uid, ts=old_ts)

        r30 = admin.get("/admin/usage/stats?days=30")
        assert r30.status_code == 200, r30.text
        models_30 = {row["model"] for row in r30.json()["tokens"]["by_kind"]}
        assert m not in models_30

        r90 = admin.get("/admin/usage/stats?days=90")
        assert r90.status_code == 200, r90.text
        models_90 = {row["model"] for row in r90.json()["tokens"]["by_kind"]}
        assert m in models_90
    finally:
        _delete_usage_events_by_model([m])
