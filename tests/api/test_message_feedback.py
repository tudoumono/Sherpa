"""回答ごとの利用者フィードバック API（`POST /chat/{conversation_id}/messages/{message_id}/feedback`）。

- 会話の所有者のみ投稿できる（他人の会話・受領共有は拒否）。
- 対象は assistant メッセージのみ（user メッセージ・存在しない id は拒否）。
- 会話 A の cid ＋会話 B の message_id（同一所有者・別会話）は message が conversation_id に
  属していないため拒否する（IDOR 回帰）。
- 同一利用者・同一メッセージへの再送は上書き（最新1件のみ）。会話削除時は message_feedback も
  CASCADE で消える。
- 定型タグは閉じた語彙・重複は 422 にせず一意にまとめる・一言は上限文字数超過を拒否する。
- 本文は解析前にチャンク読みでサイズ上限を適用する（413）。
- rating 等の不正値は固定文言の 422（送信値は応答に反射しない）。

未ログインは 401（test_auth_snapshot.py で snapshot 済み）。要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@msgfeedback.local", display_name=uid,
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def _mk_turn(uid: str) -> tuple[int, int, int]:
    """`uid` が所有する会話に user+assistant の1ターンを作る。(conversation_id, user_msg_id, assistant_msg_id)。"""
    conv = store.create_conversation(user_id=uid, world="v1")
    um = store.add_message(conv["id"], "user", "質問です")
    am = store.add_message(conv["id"], "assistant", "回答です", lens="qa",
                           answer={"lens": "qa", "headline": "回答です", "sources": []})
    return conv["id"], um["id"], am["id"]


def test_feedback_up_success_and_upsert_overwrites(monkeypatch):
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"fbowner{sfx}", f"FbOwner{sfx}"
    _mk_user(uid, pw)
    cid, _um, am = _mk_turn(uid)
    c = _login(uid, pw)

    r = c.post(f"/chat/{cid}/messages/{am}/feedback", json={"rating": "up"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["rating"] == "up" and body["tags"] == []

    # 再送（👎＋タグ＋一言）は上書き＝最新1件のみ残る。
    r2 = c.post(f"/chat/{cid}/messages/{am}/feedback",
               json={"rating": "down", "tags": ["wrong_evidence", "slow"], "comment": "根拠が古い"})
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["rating"] == "down"
    assert set(body2["tags"]) == {"wrong_evidence", "slow"}
    assert body2["comment"] == "根拠が古い"

    fb_map = store.get_feedback_by_message_ids([am])
    assert len(fb_map) == 1   # 1利用者×1メッセージにつき最新1件のみ
    assert fb_map[am]["rating"] == "down"


def test_feedback_rejects_unknown_tag_and_overlong_comment():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"fbtag{sfx}", f"FbTag{sfx}"
    _mk_user(uid, pw)
    cid, _um, am = _mk_turn(uid)
    c = _login(uid, pw)

    bad_tag = c.post(f"/chat/{cid}/messages/{am}/feedback",
                     json={"rating": "down", "tags": ["not_a_real_tag"]})
    assert bad_tag.status_code == 422, bad_tag.text

    too_long = c.post(f"/chat/{cid}/messages/{am}/feedback",
                      json={"rating": "down", "comment": "あ" * 501})
    assert too_long.status_code == 422, too_long.text


def test_feedback_denied_for_other_users_conversation_404():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    owner_uid, owner_pw = f"fbown2{sfx}", f"FbOwn2{sfx}"
    other_uid, other_pw = f"fbother{sfx}", f"FbOther{sfx}"
    _mk_user(owner_uid, owner_pw)
    _mk_user(other_uid, other_pw)
    cid, _um, am = _mk_turn(owner_uid)
    other = _login(other_uid, other_pw)

    r = other.post(f"/chat/{cid}/messages/{am}/feedback", json={"rating": "up"})
    assert r.status_code == 404, r.text


def test_feedback_denied_for_non_assistant_message_404():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"fbrole{sfx}", f"FbRole{sfx}"
    _mk_user(uid, pw)
    cid, um, _am = _mk_turn(uid)
    c = _login(uid, pw)

    r = c.post(f"/chat/{cid}/messages/{um}/feedback", json={"rating": "up"})
    assert r.status_code == 404, r.text


def test_feedback_denied_for_received_share_403():
    """共有された会話（読み取り専用）へのフィードバック投稿は 403（共有は閲覧専用という既存契約）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    owner_uid, owner_pw = f"fbshown{sfx}", f"FbShOwn{sfx}"
    viewer_uid, viewer_pw = f"fbshvw{sfx}", f"FbShVw{sfx}"
    _mk_user(owner_uid, owner_pw)
    _mk_user(viewer_uid, viewer_pw)
    cid, _um, am = _mk_turn(owner_uid)

    token_hash = hashlib.sha256(f"fb-share-{sfx}".encode()).hexdigest()
    share_id = store.create_share(cid, owner_uid, token_hash, None, [viewer_uid])
    wrapper_cid = store.accept_share(share_id, viewer_uid)

    viewer = _login(viewer_uid, viewer_pw)
    r = viewer.post(f"/chat/{wrapper_cid}/messages/{am}/feedback", json={"rating": "up"})
    assert r.status_code == 403, r.text


def test_feedback_dedupes_tags():
    """タグの重複は 422 にせず一意にまとめる（保存・レスポンスとも一意）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"fbdedup{sfx}", f"FbDedup{sfx}"
    _mk_user(uid, pw)
    cid, _um, am = _mk_turn(uid)
    c = _login(uid, pw)

    r = c.post(f"/chat/{cid}/messages/{am}/feedback",
              json={"rating": "down", "tags": ["slow", "slow", "wrong_evidence"]})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["tags"]) == ["slow", "wrong_evidence"]

    fb_map = store.get_feedback_by_message_ids([am])
    assert sorted(fb_map[am]["tags"]) == ["slow", "wrong_evidence"]


def test_feedback_invalid_rating_returns_fixed_message_without_reflecting_input():
    """rating の不正値は固定文言の 422（送信値そのものは応答本文に反射しない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"fbrat{sfx}", f"FbRat{sfx}"
    _mk_user(uid, pw)
    cid, _um, am = _mk_turn(uid)
    c = _login(uid, pw)

    r = c.post(f"/chat/{cid}/messages/{am}/feedback", json={"rating": "とても良い秘密の値XYZ123"})
    assert r.status_code == 422, r.text
    assert "とても良い秘密の値XYZ123" not in r.text


def test_feedback_body_size_over_cap_returns_413():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"fbbig{sfx}", f"FbBig{sfx}"
    _mk_user(uid, pw)
    cid, _um, am = _mk_turn(uid)
    c = _login(uid, pw)

    huge_comment = "あ" * 100_000   # 64KiB 上限を確実に超える
    r = c.post(f"/chat/{cid}/messages/{am}/feedback",
              json={"rating": "down", "comment": huge_comment})
    assert r.status_code == 413, r.text


def test_feedback_idor_message_from_other_conversation_is_denied():
    """会話 A の cid ＋会話 B の message_id（同一所有者の別会話）は、message が conversation_id に
    属していないため拒否する（URL の conversation_id だけを見た所有権判定の穴を防ぐ）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"fbidor{sfx}", f"FbIdor{sfx}"
    _mk_user(uid, pw)
    cid_a, _um_a, _am_a = _mk_turn(uid)
    _cid_b, _um_b, am_b = _mk_turn(uid)
    c = _login(uid, pw)

    r = c.post(f"/chat/{cid_a}/messages/{am_b}/feedback", json={"rating": "up"})
    assert r.status_code == 404, r.text


def test_feedback_cascades_on_conversation_delete():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"fbcasc{sfx}", f"FbCasc{sfx}"
    _mk_user(uid, pw)
    cid, _um, am = _mk_turn(uid)
    c = _login(uid, pw)

    r = c.post(f"/chat/{cid}/messages/{am}/feedback", json={"rating": "up"})
    assert r.status_code == 200, r.text
    assert store.get_feedback_by_message_ids([am])

    assert store.delete_conversation(cid, uid) is True
    assert store.get_feedback_by_message_ids([am]) == {}
