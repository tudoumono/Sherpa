"""調査の記録のダウンロード API（COD-18「調査台帳を回答ごとに残す」提案書・
`GET /conversations/{conversation_id}/messages/{message_id}/investigation`）。

- 会話の所有者のみ（`store.owns_assistant_message` と同じ判定）。他人の会話・共有の受領ラッパー・
  論理削除済みの会話・記録が無いメッセージはすべて404（存在を漏らさない）。
- `format=md`（既定）は人が読む Markdown・`format=json` は保存した3つ（manifest/items/coverage）を
  そのまま返す。いずれも固定の一般名で `Content-Disposition: attachment`。

未ログインは 401（test_authz_matrix.py で固定済み）。要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app
from sherpa.store import investigation_records as store_investigation


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _mk_user(uid: str, password: str) -> None:
    store.upsert_user(uid, email=f"{uid}@invdl.local", display_name=uid,
                      password_hash=auth.hash_password(password), role="user", status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def _mk_turn_with_record(uid: str, *, complete: bool = True) -> tuple[int, int]:
    """`uid` が所有する会話に assistant メッセージ＋調査の記録を1件作る。(conversation_id, message_id)。"""
    conv = store.create_conversation(user_id=uid, world="v1")
    am = store.add_message(conv["id"], "assistant", "回答です", lens="qa",
                           answer={"lens": "qa", "headline": "回答です", "sources": [],
                                   "investigation": {"recorded": True}})
    store_investigation.save_investigation_record(
        am["id"], conv["id"], complete=complete,
        manifest={"question_kind": "qa", "created_at": "2026-10-01T00:00:00Z", "items": ["i1"]},
        items={"i1": {"id": "i1", "kind": "qa", "subject": "対象A", "required_checks": ["source"],
                      "evidence": [{"kind": "source", "path": "a/b.md", "line": 3}],
                      "status": "source_confirmed", "reason": "", "owner": "main"}},
        coverage={"i1": ["hit"]})
    return conv["id"], am["id"]


def test_owner_downloads_md_and_json_shapes():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"invowner{sfx}", f"InvOwner{sfx}"
    _mk_user(uid, pw)
    cid, mid = _mk_turn_with_record(uid, complete=False)
    c = _login(uid, pw)

    r_md = c.get(f"/conversations/{cid}/messages/{mid}/investigation")
    assert r_md.status_code == 200, r_md.text
    assert r_md.headers["content-type"].startswith("text/markdown")
    assert 'filename="investigation.md"' in r_md.headers["content-disposition"]
    assert r_md.text.startswith("# 調査の記録")
    assert "対象A" in r_md.text and "source_confirmed" in r_md.text
    assert "完了: いいえ" in r_md.text

    r_json = c.get(f"/conversations/{cid}/messages/{mid}/investigation?format=json")
    assert r_json.status_code == 200, r_json.text
    assert r_json.headers["content-type"].startswith("application/json")
    assert 'filename="investigation.json"' in r_json.headers["content-disposition"]
    body = r_json.json()
    assert body["complete"] is False and body["truncated"] is False
    assert body["items"]["i1"]["status"] == "source_confirmed"
    assert body["manifest"]["question_kind"] == "qa"

    r_bad = c.get(f"/conversations/{cid}/messages/{mid}/investigation?format=csv")
    assert r_bad.status_code == 422, r_bad.text   # FastAPI の pattern 検証


def test_non_owner_shared_viewer_no_record_and_deleted_conversation_all_404():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    owner_uid, owner_pw = f"invown2{sfx}", f"InvOwn2{sfx}"
    other_uid, other_pw = f"invother{sfx}", f"InvOther{sfx}"
    viewer_uid, viewer_pw = f"invvwr{sfx}", f"InvVwr{sfx}"
    _mk_user(owner_uid, owner_pw)
    _mk_user(other_uid, other_pw)
    _mk_user(viewer_uid, viewer_pw)
    cid, mid = _mk_turn_with_record(owner_uid)
    owner = _login(owner_uid, owner_pw)
    other = _login(other_uid, other_pw)

    # 他人の会話。
    r_other = other.get(f"/conversations/{cid}/messages/{mid}/investigation")
    assert r_other.status_code == 404, r_other.text

    # 共有の受領ラッパー（閲覧専用）経由＝所有者ではない（受領側は常に404・403との区別をしない）。
    token_hash = hashlib.sha256(f"inv-share-{sfx}".encode()).hexdigest()
    share_id = store.create_share(cid, owner_uid, token_hash, None, [viewer_uid])
    wrapper_cid = store.accept_share(share_id, viewer_uid)
    viewer = _login(viewer_uid, viewer_pw)
    r_viewer = viewer.get(f"/conversations/{wrapper_cid}/messages/{mid}/investigation")
    assert r_viewer.status_code == 404, r_viewer.text
    # 受領共有の読者には「記録あり」の旗を見せない（見せると画面に押しても 404 の導線が出る）。
    conv_view = viewer.get(f"/conversations/{wrapper_cid}").json()
    shared_answers = [m.get("answer") or {} for m in conv_view.get("messages", []) if m.get("role") == "assistant"]
    assert shared_answers and all("recorded" not in (a.get("investigation") or {}) for a in shared_answers)

    # 記録の無いメッセージ（台帳を使わない構成・ゲートが走らなかったターン相当）は所有者でも404。
    conv_no_record = store.create_conversation(user_id=owner_uid, world="v1")
    am_no_record = store.add_message(conv_no_record["id"], "assistant", "回答のみ",
                                     answer={"headline": "回答のみ"})
    r_no_record = owner.get(
        f"/conversations/{conv_no_record['id']}/messages/{am_no_record['id']}/investigation")
    assert r_no_record.status_code == 404, r_no_record.text

    # 会話削除後は所有者本人でも404（論理削除・investigation_records は messages の CASCADE で消える・
    # 実削除後のカスケードは tests/unit/test_investigation_records.py が直接確認する）。
    assert store.delete_conversation(cid, owner_uid) is True
    r_after_delete = owner.get(f"/conversations/{cid}/messages/{mid}/investigation")
    assert r_after_delete.status_code == 404, r_after_delete.text
    # 共有の受け取り手がいる会話は論理削除（messages が残り CASCADE が発火しない）でも記録は消える。
    from sherpa.store import investigation_records as _inv
    assert _inv.get_investigation_record(mid) is None
