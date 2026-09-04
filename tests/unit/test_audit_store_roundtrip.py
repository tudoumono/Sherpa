"""sherpa/store/audit.py の unit テスト（フェーズ7 S6・23%→引き上げ）。

list_audit の絞り込み分岐・get_messages_by_ids の conv_deleted 判定・_redact の再帰的除去を、
実 DB（audit_log・非破壊）で round-trip する。破壊的な hash-chain 改ざん検証は既存
tests/unit/test_audit_chain.py::test_db_chain_detects_tamper_and_truncation に譲る（本ファイルは
非破壊のみ＝SHERPA_AUDIT_CHAIN_DB_TEST ゲート不要・audit_log に行を追記するだけで chain 自体は壊さない）。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from sherpa import store


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _try_init() -> None:
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


# ===== 純関数（DB 非依存）=====

def test_redact_removes_known_keys_recursively_in_nested_structures():
    raw = {
        "password": "p@ss",
        "nested": {"token_hash": "abc", "keep": "ok"},
        "list": [{"api_key": "x"}, {"keep": 1}],
        "keep_top": "value",
    }
    out = store._redact(raw)
    assert out["password"] == "<redacted>"
    assert out["nested"]["token_hash"] == "<redacted>" and out["nested"]["keep"] == "ok"
    assert out["list"][0]["api_key"] == "<redacted>" and out["list"][1] == {"keep": 1}
    assert out["keep_top"] == "value"


def test_get_messages_by_ids_empty_returns_empty_dict():
    assert store.get_messages_by_ids([]) == {}


# ===== DB round-trip =====

def test_audit_insert_redacts_detail_before_storing_and_list_audit_finds_it():
    _try_init()
    sfx = _sfx()
    actor = f"unit-audit-{sfx}"
    store.audit(actor, "unit.test.redact", "test", f"rid-{sfx}",
               detail={"password": "leak-me", "keep": "ok"},
               outcome="success", severity="info")
    rows = store.list_audit(actor=actor, action="unit.test.redact")
    assert len(rows) == 1
    assert rows[0]["detail"]["password"] == "<redacted>"
    assert rows[0]["detail"]["keep"] == "ok"


def test_list_audit_filters_by_action_prefix_outcome_severity_and_resource():
    _try_init()
    sfx = _sfx()
    actor = f"unit-audit-pfx-{sfx}"
    store.audit(actor, "unit.prefix.a", "widget", f"w-{sfx}", outcome="success", severity="info")
    store.audit(actor, "unit.prefix.b", "widget", f"w-{sfx}", outcome="deny", reason="nope", severity="warning")
    store.audit(actor, "unit.other.c", "widget", f"other-{sfx}", outcome="success", severity="info")

    by_prefix = store.list_audit(actor=actor, action="unit.prefix.*")
    assert {r["action"] for r in by_prefix} == {"unit.prefix.a", "unit.prefix.b"}

    by_outcome = store.list_audit(actor=actor, outcome="deny")
    assert len(by_outcome) == 1 and by_outcome[0]["action"] == "unit.prefix.b"

    by_severity = store.list_audit(actor=actor, severity="warning")
    assert len(by_severity) == 1 and by_severity[0]["reason"] == "nope"

    by_resource = store.list_audit(actor=actor, resource_type="widget", resource_id=f"other-{sfx}")
    assert len(by_resource) == 1 and by_resource[0]["action"] == "unit.other.c"

    limited = store.list_audit(actor=actor, limit=1, offset=0)
    assert len(limited) == 1   # 新しい順（created_at DESC, id DESC）の先頭1件


def test_list_audit_filters_by_time_range_and_request_id():
    _try_init()
    sfx = _sfx()
    actor = f"unit-audit-time-{sfx}"
    rid = f"req-{sfx}"
    store.audit(actor, "unit.time.a", "widget", None, outcome="success", request_id=rid)

    future = datetime.now(timezone.utc) + timedelta(days=1)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    in_range = store.list_audit(actor=actor, time_from=past, time_to=future)
    assert len(in_range) == 1

    out_of_range = store.list_audit(actor=actor, time_from=future)
    assert out_of_range == []

    by_request = store.list_audit(actor=actor, request_id=rid)
    assert len(by_request) == 1


def test_get_messages_by_ids_flags_conv_deleted_for_soft_deleted_conversation():
    _try_init()
    sfx = _sfx()
    conv = store.create_conversation(user_id=f"unit-audit-msg-{sfx}", world="v1", title="t")
    cid = conv["id"]
    m1 = store.add_message(cid, "user", content="hello")
    m2 = store.add_message(cid, "assistant", content="world", personal=True)

    got = store.get_messages_by_ids([m1["id"], m2["id"], -999])
    assert set(got.keys()) == {m1["id"], m2["id"]}          # 存在しない id は含まれない
    assert got[m1["id"]]["content"] == "hello" and got[m1["id"]]["conv_deleted"] is False
    assert got[m2["id"]]["personal"] is True

    # soft delete（受領共有ラッパーが生きている場合の delete_conversation と同じ状態）を直接 SQL で
    # 再現する（本テストの主眼は get_messages_by_ids の conv_deleted 判定であり、delete_conversation
    # 自体の分岐は conversations.py 側の担当）。
    with store._connect() as c:
        c.execute("UPDATE conversations SET deleted_at=now() WHERE id=%s", (cid,))
    got2 = store.get_messages_by_ids([m1["id"]])
    assert got2[m1["id"]]["conv_deleted"] is True
