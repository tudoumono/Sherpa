"""F3（2026-07-07-フィードバック一括.md）: トークン使用量の記録・集計（tokens セクション・入力/出力のみ）。

- 履歴 API（GET /conversations/{cid}）が answer.usage を返す（保存形の round-trip）。
- GET /admin/usage/stats の tokens セクション: provider/model 別・上位ユーザー別・日別・totals の集計。
- usage を持たないターン（heuristic 相当・usage 無し）は集計母数から外れる。
- 金額換算（cost_usd/cost_jpy/usd_jpy）は撤去済み（2026-07-08 フィードバック⑦）＝トークン数のみ返す。

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app


from _common import _login, _sfx, _try_init


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@tok.local", display_name=f"表示名-{uid}",
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def _turn_with_usage(cid: int, lens: str, usage: dict | None):
    store.add_message(cid, "user", "質問")
    answer = {"headline": "回答", "lens": lens, "sources": []}
    if usage is not None:
        answer["usage"] = usage
    store.add_message(cid, "assistant", "回答", lens=lens, answer=answer)


# ---- 保存形: 履歴 API が answer.usage を返す ----

def test_conversation_history_returns_usage():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"tokusr{sfx}", f"TokUser{sfx}"
    _mk_user(uid, pw)
    conv = store.create_conversation(user_id=uid, world="v1", title="usage往復")
    usage = {"provider": "openai", "model": "gpt-5.5", "input_tokens": 100,
             "cached_input_tokens": 20, "output_tokens": 30, "reasoning_output_tokens": 5}
    _turn_with_usage(conv["id"], "qa", usage)

    c = _login(uid, pw)
    r = c.get(f"/conversations/{conv['id']}")
    assert r.status_code == 200, r.text
    asst = next(m for m in r.json()["messages"] if m["role"] == "assistant")
    assert asst["answer"]["usage"] == usage


# ---- 統計: tokens 集計（入力/出力トークン数のみ） ----

def test_usage_stats_tokens_aggregation():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"tokadm{sfx}", f"TokAdmin{sfx}"
    user_uid, user_pw = f"tokusr2{sfx}", f"TokUser2{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw)

    world = f"tokworld{sfx}"
    conv = store.create_conversation(user_id=user_uid, world=world, title="集計対象")
    cid = conv["id"]
    # 同一 provider/model の2ターン＝合算される。
    _turn_with_usage(cid, "qa", {"provider": "openai", "model": "gpt-5.5",
                                 "input_tokens": 1000, "cached_input_tokens": 200,
                                 "output_tokens": 300, "reasoning_output_tokens": 50})
    _turn_with_usage(cid, "qa", {"provider": "openai", "model": "gpt-5.5",
                                 "input_tokens": 500, "cached_input_tokens": 0,
                                 "output_tokens": 100, "reasoning_output_tokens": 0})
    # 別 provider/model。
    _turn_with_usage(cid, "impact", {"provider": "gemini", "model": "gemini-2.5-flash",
                                     "input_tokens": 2000, "cached_input_tokens": 0,
                                     "output_tokens": 400, "reasoning_output_tokens": 0})
    # usage 無しターン（heuristic 相当）＝集計に入らない。
    _turn_with_usage(cid, "qa", None)

    c = _login(admin_uid, admin_pw)
    r = c.get("/admin/usage/stats?days=30")
    assert r.status_code == 200, r.text
    tokens = r.json()["tokens"]

    # by_model: openai/gpt-5.5 は2ターン合算・gemini は1ターン。
    by_model = {(m["provider"], m["model"]): m for m in tokens["by_model"]}
    oa = by_model[("openai", "gpt-5.5")]
    assert oa["turns"] == 2 and oa["input"] == 1500 and oa["output"] == 400
    assert oa["cached_input"] == 200 and oa["reasoning_output"] == 50
    gm = by_model[("gemini", "gemini-2.5-flash")]
    assert gm["turns"] == 1 and gm["input"] == 2000 and gm["output"] == 400

    # 金額換算（cost_usd/cost_jpy/usd_jpy）は撤去済み＝どの行にも付かない（トークン数のみ）。
    assert "cost_usd" not in oa and "cost_jpy" not in oa
    assert "cost_usd" not in tokens["totals"] and "cost_jpy" not in tokens["totals"]
    assert "usd_jpy" not in tokens

    # totals は by_model の合計（この user 分＝他テストデータが無ければ 3500）。
    assert tokens["totals"]["input"] >= 3500 and tokens["totals"]["output"] >= 800

    # by_user: 対象ユーザーが input/output を持つ行として現れる。
    by_user = {u["uid"]: u for u in tokens["by_user"]}
    assert user_uid in by_user
    assert by_user[user_uid]["input"] == 3500 and by_user[user_uid]["output"] == 800
    assert by_user[user_uid]["turns"] == 3   # usage を持つ3ターンのみ

    # daily: 今日の行に input/output が入る。
    assert any(d["input"] > 0 and d["output"] > 0 for d in tokens["daily"])


def test_usage_stats_tokens_empty_when_no_usage():
    """usage を持つターンが1つも無い期間では tokens が空（totals ゼロ・by_model 空）でも 500 にしない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"tokadm2{sfx}", f"TokAdmin2{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    c = _login(admin_uid, admin_pw)
    r = c.get("/admin/usage/stats?days=1")   # 1日＝新規テストデータのみが対象になりやすい
    assert r.status_code == 200, r.text
    tokens = r.json()["tokens"]
    assert "totals" in tokens and "by_model" in tokens and "by_user" in tokens and "daily" in tokens
    assert isinstance(tokens["totals"]["input"], int)
