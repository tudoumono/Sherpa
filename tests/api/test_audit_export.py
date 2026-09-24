from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app


def _future(days=7):
    return datetime.now(timezone.utc) + timedelta(days=days)


def _sfx() -> str:
    return str(time.time_ns())[-12:]


from _common import _login, _try_init


def _mk_user(uid: str, password: str, role: str) -> None:
    store.upsert_user(uid, email=f"{uid}@audit.local", display_name=uid,
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）


def test_admin_audit_export_csv_jsonl_and_user_denied():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"audexpadm{sfx}", f"AdminExport{sfx}"
    user_uid, user_pw = f"audexpusr{sfx}", f"UserExport{sfx}"
    _mk_user(admin_uid, admin_pw, "admin")
    _mk_user(user_uid, user_pw, "user")

    store.audit(user_uid, "auth.login_failed", "user", f"user:{user_uid}",
                detail={"password": "never-export-this", "reason": "fixture"},
                outcome="deny", severity="warning")

    admin = _login(admin_uid, admin_pw)
    csv_r = admin.get("/admin/audit/export?format=csv&action=auth.login_failed")
    assert csv_r.status_code == 200, csv_r.text
    assert "text/csv" in csv_r.headers["content-type"]
    assert "sherpa-audit-" in csv_r.headers["content-disposition"]
    assert "auth.login_failed" in csv_r.text
    assert "never-export-this" not in csv_r.text

    jsonl_r = admin.get("/admin/audit/export?format=jsonl&action=auth.login_failed")
    assert jsonl_r.status_code == 200, jsonl_r.text
    assert "application/x-ndjson" in jsonl_r.headers["content-type"]
    assert "auth.login_failed" in jsonl_r.text
    assert "never-export-this" not in jsonl_r.text

    rows = store.list_audit(actor=admin_uid, action="admin.audit_exported", limit=20)
    assert rows, "admin.audit_exported was not recorded"

    user = _login(user_uid, user_pw)
    denied = user.get("/admin/audit/export?format=csv")
    assert denied.status_code == 403


def _mk_chat_turn(uid: str, *, user_content: str, assistant_content: str,
                  personal: bool = False, detail_personal: bool | None = None,
                  user_personal: bool | None = None,
                  assistant_personal: bool | None = None) -> tuple[int, int, int]:
    """S5: chat.turn 監査行1件＋裏付けの会話/メッセージを直接作る（テスト用の最小フィクスチャ）。
    実際の /chat 経路と同じ形の detail（本文なし・id とメタのみ）を積む。

    `detail_personal`/`user_personal`/`assistant_personal` は RV MEDIUM（2026-07-03）の
    混在 flag 回帰テスト用に個別上書きできる（省略時は全部 `personal` に揃える＝通常ケース）。
    """
    dp = personal if detail_personal is None else detail_personal
    up = personal if user_personal is None else user_personal
    ap = personal if assistant_personal is None else assistant_personal
    conv = store.create_conversation(user_id=uid, world="v1")
    um = store.add_message(conv["id"], "user", user_content, personal=up)
    am = store.add_message(conv["id"], "assistant", assistant_content, lens="qa", personal=ap)
    store.audit(uid, "chat.turn", "conversation", f"conv:{conv['id']}",
               detail={"message_id_user": um["id"], "message_id_assistant": am["id"],
                       "lens": "qa", "world": "v1", "scope_paths": 0,
                       "personal": dp, "provider": "heuristic"},
               outcome="success", severity="info")
    return conv["id"], um["id"], am["id"]


def test_admin_audit_export_chat_content_include_and_omit():
    """S5: include_chat_content=1 の時だけ chat.turn 行に user_prompt/assistant_answer が
    join される。未指定は従来どおり（本文キー無し＝完全互換）。エクスポート自体の監査 detail にも
    include_chat_content の有無が記録される。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"audexpc{sfx}", f"AdminChat{sfx}"
    _mk_user(admin_uid, admin_pw, "admin")
    cid, um_id, am_id = _mk_chat_turn(
        admin_uid, user_content="エクスポートテスト用の質問です", assistant_content="回答headlineです")

    admin = _login(admin_uid, admin_pw)
    with_content = admin.get(
        f"/admin/audit/export?format=jsonl&resource_id=conv:{cid}&include_chat_content=1")
    assert with_content.status_code == 200, with_content.text
    row = next(json.loads(line) for line in with_content.text.splitlines()
              if json.loads(line)["action"] == "chat.turn")
    assert row["detail"]["user_prompt"] == "エクスポートテスト用の質問です"
    assert row["detail"]["assistant_answer"] == "回答headlineです"

    without = admin.get(f"/admin/audit/export?format=jsonl&resource_id=conv:{cid}")
    assert without.status_code == 200, without.text
    row2 = next(json.loads(line) for line in without.text.splitlines()
               if json.loads(line)["action"] == "chat.turn")
    assert "user_prompt" not in row2["detail"] and "assistant_answer" not in row2["detail"]
    assert row2["detail"]["message_id_user"] == um_id
    assert row2["detail"]["message_id_assistant"] == am_id

    exported = store.list_audit(actor=admin_uid, action="admin.audit_exported", limit=20)
    flags = [r["detail"].get("include_chat_content") for r in exported]
    assert True in flags and False in flags


def test_admin_audit_export_chat_content_personal_placeholder():
    """S5: personal=true のターンは本文の代わりにプレースホルダ（越境防止・admin にも平文で出さない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"audexppr{sfx}", f"AdminPriv{sfx}"
    _mk_user(admin_uid, admin_pw, "admin")
    cid, _um, _am = _mk_chat_turn(
        admin_uid, user_content="個人ファイルの中身です", assistant_content="個人向けの回答です",
        personal=True)

    admin = _login(admin_uid, admin_pw)
    r = admin.get(f"/admin/audit/export?format=jsonl&resource_id=conv:{cid}&include_chat_content=1")
    assert r.status_code == 200, r.text
    row = next(json.loads(line) for line in r.text.splitlines() if json.loads(line)["action"] == "chat.turn")
    placeholder = "（個人ファイル参照ターン・本文はエクスポート対象外）"
    assert row["detail"]["user_prompt"] == placeholder
    assert row["detail"]["assistant_answer"] == placeholder
    assert "個人ファイルの中身です" not in r.text and "個人向けの回答です" not in r.text


def test_admin_audit_export_chat_content_personal_placeholder_on_flag_mismatch():
    """RV MEDIUM（2026-07-03）: personal 判定は chat.turn.detail.personal（ターン全体の記録）と
    messages.personal（メッセージ個々の flag）の OR。片方だけが立っている混在ケースでも
    user/assistant 両方が無条件プレースホルダになることを確認する（片方の flag 欠落による越境防止）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"audexpmx{sfx}", f"AdminMix{sfx}"
    _mk_user(admin_uid, admin_pw, "admin")
    placeholder = "（個人ファイル参照ターン・本文はエクスポート対象外）"

    # ケースA: detail.personal=True だが message 側 flag は両方 False（欠落想定）→ それでも両方プレースホルダ。
    cid_a, _u, _a = _mk_chat_turn(
        admin_uid, user_content="A-個人質問", assistant_content="A-個人回答",
        detail_personal=True, user_personal=False, assistant_personal=False)
    # ケースB: detail.personal=False だが assistant 側 message.personal だけ True → assistant 側だけでも
    # プレースホルダに落ちる（user 側は通常どおり本文が出る＝OR は「どちらかが立てば隠す」であって
    # 全体を一律隠すわけではないことも確認）。
    cid_b, _u2, _a2 = _mk_chat_turn(
        admin_uid, user_content="B-通常質問", assistant_content="B-個人回答のみ",
        detail_personal=False, user_personal=False, assistant_personal=True)

    admin = _login(admin_uid, admin_pw)
    r = admin.get(f"/admin/audit/export?format=jsonl&include_chat_content=1"
                  f"&actor={admin_uid}&action=chat.turn")
    assert r.status_code == 200, r.text
    rows = {json.loads(line)["resource_id"]: json.loads(line) for line in r.text.splitlines()}

    row_a = rows[f"conv:{cid_a}"]
    assert row_a["detail"]["user_prompt"] == placeholder
    assert row_a["detail"]["assistant_answer"] == placeholder

    row_b = rows[f"conv:{cid_b}"]
    assert row_b["detail"]["user_prompt"] == "B-通常質問"          # user 側は flag 無し＝本文のまま
    assert row_b["detail"]["assistant_answer"] == placeholder       # assistant 側だけ flag 有り＝隠れる
    assert "A-個人質問" not in r.text and "A-個人回答" not in r.text
    assert "B-個人回答のみ" not in r.text


def test_admin_audit_export_chat_content_soft_deleted_conversation_placeholder():
    """RV HIGH (2026-07-03): 受領共有ラッパーが生きている会話は delete_conversation が
    soft-delete（deleted_at のみ・messages 行は物理的に残る）に留まる。この経路で
    include_chat_content=1 の export に本文が漏れないこと（削除済み扱い＝プレースホルダ）を確認する。
    admin 自身が export 対象の owner を兼ねる（export の admin ゲートとは独立の検証）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"audexpsd{sfx}", f"AdminSoft{sfx}"
    recipient_uid = f"audexpsr{sfx}"
    _mk_user(admin_uid, admin_pw, "admin")
    _mk_user(recipient_uid, f"Recip{sfx}", "user")
    cid, _um, _am = _mk_chat_turn(
        admin_uid, user_content="共有される予定の質問", assistant_content="共有される予定の回答")

    th = hashlib.sha256(("tok-" + sfx).encode()).hexdigest()
    sid = store.create_share(cid, admin_uid, th, _future(), [recipient_uid])
    store.accept_share(sid, recipient_uid)          # 生きた受領ラッパーを作る
    assert store.delete_conversation(cid, user_id=admin_uid)   # ラッパー有り→soft delete（messages は残る）
    conv = store.get_conversation(cid)
    assert conv is not None, "soft delete のはずが会話ごと消えている（テスト前提が崩れている）"

    admin = _login(admin_uid, admin_pw)
    r = admin.get(f"/admin/audit/export?format=jsonl&resource_id=conv:{cid}&include_chat_content=1")
    assert r.status_code == 200, r.text
    row = next(json.loads(line) for line in r.text.splitlines() if json.loads(line)["action"] == "chat.turn")
    assert row["detail"]["user_prompt"] == "（削除済み）", \
        "soft-delete 済み会話の本文が export に残っている（越境）"
    assert row["detail"]["assistant_answer"] == "（削除済み）"
    assert "共有される予定の質問" not in r.text and "共有される予定の回答" not in r.text


def test_admin_audit_export_chat_content_deleted_resilience():
    """S5: 会話/メッセージが削除済みでもエクスポートは落ちない（「（削除済み）」プレースホルダ）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"audexpdl{sfx}", f"AdminDel{sfx}"
    _mk_user(admin_uid, admin_pw, "admin")
    cid, _um, _am = _mk_chat_turn(
        admin_uid, user_content="消えるはずの質問", assistant_content="消えるはずの回答")
    assert store.delete_conversation(cid, user_id=admin_uid)   # messages は FK CASCADE で一緒に消える

    admin = _login(admin_uid, admin_pw)
    r = admin.get(f"/admin/audit/export?format=jsonl&resource_id=conv:{cid}&include_chat_content=1")
    assert r.status_code == 200, r.text
    row = next(json.loads(line) for line in r.text.splitlines() if json.loads(line)["action"] == "chat.turn")
    assert row["detail"]["user_prompt"] == "（削除済み）"
    assert row["detail"]["assistant_answer"] == "（削除済み）"
