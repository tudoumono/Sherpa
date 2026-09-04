"""改善ログエクスポート API（`GET /admin/improvement-log/export`）。

- admin のみ（未ログイン401は test_auth_snapshot.py で snapshot 済み・非admin403はここで確認）。
- CSV/JSONL 両対応・1行=1ターン（assistant メッセージ）。質問の対応付けは `chat.turn` 監査
  （message_id_user/message_id_assistant）で厳密に行う——対応付けられない（監査行が無い/
  欠けている）ターンは fail-closed で丸ごと除外する（個人情報の有無が確認できないため）。
- 個人情報由来のターン（質問・回答のいずれか・列/旧マーカーとも）・sanitized_snapshot の複製・
  論理削除済み会話は除外する。
- フィードバックが join される。
- honest_failure は stop_reason/evidence_selected/investigation_status/lens から導出する派生列
  （DB には保存しない）。stop_reason の語彙は未知語も含めそのまま通す（落とさない）。
  truncated/content_filtered（未完了・honest_failure に含めない）は、これらの値を実際に生成する
  経路が現時点のコードに無いため純関数テスト（tests/unit/test_improvement_log.py）側で固定し、
  ここでは検証しない（存在しない値を API 経由で注入するテストは書かない）。
- days で期間を絞り込む。
- CSV インジェクション対策（=+-@・全角＝＋－＠・タブ/CR/LF）。
- 出力上限到達時の truncated 通知（ヘッダ/JSONL末尾/監査）。上限到達後の probe が個人情報の行を
  無視することは tests/unit 側（`fetch_export_rows` を決定的なデータセットで直接呼ぶ）で固定する
  （共有 dev DB に対する実 API 呼び出しでは「これ以上の行が無い」という否定条件を決定的に
  再現できないため）。

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import csv
import io
import json
import time

import psycopg
import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app
from sherpa.routers import chat as chat_router_mod


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@improvlog.local", display_name=uid,
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def _qa_answer(*, stop_reason, evidence_selected, sources, investigation_status="insufficient"):
    return {
        "lens": "qa", "headline": "回答headline", "sources": sources,
        "data": {"evidence_packet": {
            "task_id": "t1", "investigation_status": investigation_status,
            "candidates_seen": 5, "candidates_inspected": 3,
            "evidence_selected": evidence_selected, "stop_reason": stop_reason,
        }},
        "usage": {"provider": "openai", "model": "gpt-test", "input_tokens": 10, "output_tokens": 5},
        "duration_ms": 1234,
    }


def _mk_chat_turn_audit(uid, conv_id, um_id, am_id, *, personal: bool = False, lens: str = "qa") -> None:
    """実際の chat_service._audit_chat_turn と同じ形（message_id_user/message_id_assistant を持つ
    chat.turn 監査行）を直接作る。改善ログエクスポートはこの2フィールドで質問を厳密に対応付ける
    （「直前の user 行」を推測しない・対応付けられないターンは丸ごと除外する）。"""
    store.audit(uid, "chat.turn", "conversation", f"conv:{conv_id}",
               detail={"message_id_user": um_id, "message_id_assistant": am_id,
                       "lens": lens, "world": "v1", "scope_paths": 0, "personal": personal,
                       "provider": "openai", "provider_saved": "openai", "stopped": False},
               outcome="success", severity="info")


def _message_personal_flag(message_id: int) -> bool:
    with psycopg.connect(store._dsn()) as c:
        row = c.execute("SELECT personal FROM messages WHERE id=%s", (message_id,)).fetchone()
    return bool(row[0])


def test_improvement_log_export_requires_admin():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"ilnonadm{sfx}", f"IlNonAdm{sfx}"
    _mk_user(uid, pw, role="user")
    c = _login(uid, pw)
    r = c.get("/admin/improvement-log/export")
    assert r.status_code == 403, r.text


def test_improvement_log_export_csv_and_jsonl_include_feedback_and_derived_fields():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"iladm{sfx}", f"IlAdm{sfx}"
    user_uid, user_pw = f"ilusr{sfx}", f"IlUsr{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")

    conv = store.create_conversation(user_id=user_uid, world="v1")
    question_text = f"改善ログ確認用の質問-{sfx}"
    um = store.add_message(conv["id"], "user", question_text)
    am = store.add_message(
        conv["id"], "assistant", "回答headlineです", lens="qa",
        answer=_qa_answer(stop_reason="evaluation_blocked", evidence_selected=0, sources=[]),
        trace=[{"id": "n1", "kind": "tool", "label": "資料を検索（語句そのまま）", "detail": "x", "status": "done"},
              {"id": "n2", "kind": "tool", "label": "該当箇所を精読", "detail": "y", "status": "done"}])
    _mk_chat_turn_audit(user_uid, conv["id"], um["id"], am["id"])

    admin = _login(admin_uid, admin_pw)
    # フィードバックは会話の所有者（user_uid）のみ投稿できる。
    user = _login(user_uid, user_pw)
    fb_r = user.post(f"/chat/{conv['id']}/messages/{am['id']}/feedback",
                     json={"rating": "down", "tags": ["wrong_evidence"], "comment": "根拠が違う"})
    assert fb_r.status_code == 200, fb_r.text

    jsonl_r = admin.get("/admin/improvement-log/export?format=jsonl&days=1")
    assert jsonl_r.status_code == 200, jsonl_r.text
    assert "application/x-ndjson" in jsonl_r.headers["content-type"]
    rows = [json.loads(line) for line in jsonl_r.text.splitlines()]
    row = next(r for r in rows if r["message_id"] == am["id"])
    assert row["conversation_id"] == conv["id"]
    assert row["question_head"] == question_text   # chat.turn 監査の対応付け（推測ではない）
    assert row["question_truncated"] is False
    assert row["answer_head"] == "回答headlineです"
    assert row["answer_truncated"] is False
    assert row["trace_truncated"] is False
    assert row["stop_reason"] == "evaluation_blocked"
    assert row["evidence_selected"] == 0
    assert row["investigation_status"] == "insufficient"
    assert row["tool_calls"] == 2
    assert row["files_read"] == 1
    assert row["honest_failure"] is True   # qa レンズ + evaluation_blocked（無条件語彙）
    assert row["duration_ms"] == 1234
    assert row["provider"] == "openai"
    assert row["feedback"] == {"rating": "down", "tags": ["wrong_evidence"], "comment": "根拠が違う"}

    csv_r = admin.get("/admin/improvement-log/export?format=csv&days=1")
    assert csv_r.status_code == 200, csv_r.text
    assert "text/csv" in csv_r.headers["content-type"]
    reader = csv.DictReader(io.StringIO(csv_r.text))
    csv_rows = list(reader)
    assert any(int(r["message_id"]) == am["id"] for r in csv_rows)

    audit_rows = store.list_audit(actor=admin_uid, action="admin.improvement_log_exported", limit=10)
    assert audit_rows, "admin.improvement_log_exported was not recorded"


def test_improvement_log_export_excludes_personal_turns():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"ilpadm{sfx}", f"IlPAdm{sfx}"
    user_uid, user_pw = f"ilpusr{sfx}", f"IlPUsr{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")

    conv = store.create_conversation(user_id=user_uid, world="v1")
    um = store.add_message(conv["id"], "user", "個人ファイル参照ターンの質問", personal=True)
    am = store.add_message(conv["id"], "assistant", f"個人参照の回答-{sfx}", lens="qa",
                           answer={"lens": "qa", "headline": "個人参照の回答", "sources": []},
                           personal=True)
    _mk_chat_turn_audit(user_uid, conv["id"], um["id"], am["id"], personal=True)

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/improvement-log/export?format=jsonl&days=1")
    assert r.status_code == 200, r.text
    ids = {json.loads(line)["message_id"] for line in r.text.splitlines()}
    assert am["id"] not in ids, "personal=true ターンがエクスポートに混入した"


def test_improvement_log_export_days_boundary_excludes_stale_turns():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"ildaysadm{sfx}", f"IlDaysAdm{sfx}"
    stale_uid, stale_pw = f"ildaysusr{sfx}", f"IlDaysUsr{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(stale_uid, stale_pw, role="user")

    conv = store.create_conversation(user_id=stale_uid, world="v1")
    um = store.add_message(conv["id"], "user", "古いターン")
    am = store.add_message(conv["id"], "assistant", "古い回答", lens="qa",
                           answer={"lens": "qa", "headline": "古い回答", "sources": []})
    _mk_chat_turn_audit(stale_uid, conv["id"], um["id"], am["id"])
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET created_at = now() - interval '10 days' WHERE conversation_id=%s",
                  (conv["id"],))

    admin = _login(admin_uid, admin_pw)
    r1 = admin.get("/admin/improvement-log/export?format=jsonl&days=1")
    ids_1d = {json.loads(line)["message_id"] for line in r1.text.splitlines()}
    assert am["id"] not in ids_1d

    r30 = admin.get("/admin/improvement-log/export?format=jsonl&days=30")
    ids_30d = {json.loads(line)["message_id"] for line in r30.text.splitlines()}
    assert am["id"] in ids_30d


def test_improvement_log_export_excludes_turns_without_chat_turn_audit_pairing():
    """`chat.turn` 監査行が無い（または対応付けが欠けた）assistant メッセージは fail-closed で
    エクスポートから丸ごと除外する——「直前の質問」を推測して結合しない。監査自体は fail-open
    （`chat_service.py::_audit_chat_turn`）なので、対応付けが無いことは「非個人と確認できた」を
    意味しない、という判断（正常なターンでも監査書き込みだけがまれに失敗すれば同様に除外される
    ことを受け入れる）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"ilnog{sfx}", f"IlNoG{sfx}"
    user_uid, user_pw = f"ilnogu{sfx}", f"IlNoGU{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")

    conv = store.create_conversation(user_id=user_uid, world="v1")
    store.add_message(conv["id"], "user", "無関係な先行質問")   # chat.turn 監査を作らない
    am = store.add_message(conv["id"], "assistant", "監査行の無い回答", lens="qa",
                           answer={"lens": "qa", "headline": "監査行の無い回答", "sources": []})

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/improvement-log/export?format=jsonl&days=1")
    assert r.status_code == 200, r.text
    ids = {json.loads(line)["message_id"] for line in r.text.splitlines() if line.strip()}
    assert am["id"] not in ids, "監査対応付けの無いターンが除外されず混入した"


def test_improvement_log_export_excludes_personal_turn_via_real_crash_recovery_path():
    """クラッシュ復旧経路（`routers/chat.py::_persist_turn_crash`）を実際に通し、personal
    参照ターンがエクスポートから正しく除外されることを確認する。assistant は user の personal を
    継承し（列に固定 False ではない）、chat.turn 監査にも message_id_user/message_id_assistant が
    残ることも併せて検証する。
    """
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"ilcrash2{sfx}", f"IlCrash2{sfx}"
    user_uid, user_pw = f"ilcrash2u{sfx}", f"IlCrash2U{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")

    conv = store.create_conversation(user_id=user_uid, world="v1")
    message_text = f"個人ファイルに関する質問（クラッシュ実経路）-{sfx}"
    chat_router_mod._persist_turn_crash(conv["id"], message_text, user_uid, "v1", True,
                                        RuntimeError("boom"))

    msgs = store.get_conversation(conv["id"])["messages"]
    user_msg = next(m for m in msgs if m["role"] == "user")
    assistant_msg = next(m for m in msgs if m["role"] == "assistant")
    assert _message_personal_flag(assistant_msg["id"]) is True, \
        "クラッシュ復旧の assistant が user の personal を継承していない"

    audit_rows = store.list_audit(action="chat.turn", resource_id=f"conv:{conv['id']}", limit=5)
    assert audit_rows, "chat.turn 監査が記録されていない"
    detail = audit_rows[0]["detail"]
    assert detail["message_id_user"] == user_msg["id"]
    assert detail["message_id_assistant"] == assistant_msg["id"]

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/improvement-log/export?format=jsonl&days=1")
    assert r.status_code == 200, r.text
    ids = {json.loads(line)["message_id"] for line in r.text.splitlines() if line.strip()}
    assert assistant_msg["id"] not in ids, "クラッシュ復旧経路の personal ターンがエクスポートに混入した"


def test_improvement_log_export_crash_recovery_does_not_misattribute_to_stale_same_text_turn():
    """同じ会話で過去に**同じ文面**を非 personal で送っていた場合、そのクラッシュ復旧
    （`_persist_turn_crash`）が古い user 行を「このターン自身の行」と誤認してはいけない
    （本文一致だけで判定すると、古い行の id・personal（False）を新しい personal ターンへ
    誤って対応付けてしまう）。新しいターン専用の user/assistant 行が現在ターンの ID で作られ、
    personal=True を継承し、エクスポートから除外されることを確認する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"ilcrash3{sfx}", f"IlCrash3{sfx}"
    user_uid, user_pw = f"ilcrash3u{sfx}", f"IlCrash3U{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")

    conv = store.create_conversation(user_id=user_uid, world="v1")
    same_text = f"同文の質問（新旧ターン共通）-{sfx}"
    # 過去の非 personal ターン（正常完了・クラッシュ経路とは無関係）。
    old_user = store.add_message(conv["id"], "user", same_text, personal=False)
    old_assistant = store.add_message(conv["id"], "assistant", "旧ターンの回答", lens="qa",
                                      answer=_qa_answer(stop_reason="evaluation_sufficient",
                                                        evidence_selected=1, sources=["a.md"]),
                                      personal=False)

    # `saved_user_id` を渡さない＝ this run は user 行を保存していないという想定（コールバック前の
    # クラッシュ）。本文一致の探索はもう行わないため、常に新規保存される。以降の assertion で
    # 「旧行より新しい id」を見分けるための基準点として、直前に保存した old_assistant の id を
    # 使う（テスト自身の都合・会話内で最後に保存した行の id と一致する）。
    before_id = old_assistant["id"]
    chat_router_mod._persist_turn_crash(conv["id"], same_text, user_uid, "v1", True,
                                        RuntimeError("boom"))

    msgs = store.get_conversation(conv["id"])["messages"]
    new_user = next(m for m in msgs if m["role"] == "user" and m["id"] > before_id)
    new_assistant = next(m for m in msgs if m["role"] == "assistant" and m["id"] > before_id)
    assert new_user["id"] != old_user["id"], "新しいターンが古い user 行を使い回してしまった"
    assert new_user["personal"] is True
    assert new_assistant["personal"] is True, "personal 継承が古い（False の）行に引きずられた"

    audit_rows = store.list_audit(action="chat.turn", resource_id=f"conv:{conv['id']}", limit=5)
    assert audit_rows, "chat.turn 監査が記録されていない"
    detail = audit_rows[0]["detail"]
    assert detail["message_id_user"] == new_user["id"], "監査の対応付けが現在ターンの ID になっていない"
    assert detail["message_id_assistant"] == new_assistant["id"]
    assert detail["personal"] is True

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/improvement-log/export?format=jsonl&days=1")
    assert r.status_code == 200, r.text
    ids = {json.loads(line)["message_id"] for line in r.text.splitlines() if line.strip()}
    assert new_assistant["id"] not in ids, "誤対応付けにより personal ターンがエクスポートに混入した"


def test_improvement_log_export_excludes_unbackfilled_legacy_personal_marker_on_answer():
    """`messages.personal` 列導入前の未バックフィル行（列は false のまま）でも、answer 内の
    旧マーカー（personal_sources 等）があれば除外する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"illegacy{sfx}", f"IlLegacy{sfx}"
    user_uid, user_pw = f"illegacyu{sfx}", f"IlLegacyU{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")

    conv = store.create_conversation(user_id=user_uid, world="v1")
    um = store.add_message(conv["id"], "user", "未バックフィルの個人参照質問")
    am = store.add_message(
        conv["id"], "assistant", "未バックフィルの回答", lens="qa",
        answer={"lens": "qa", "headline": "未バックフィルの回答", "sources": [],
               "personal_sources": [{"doc_id": "個人ファイル.md", "quote": "x"}]},
        personal=False)   # personal 列は旧データのまま未バックフィル（false）
    _mk_chat_turn_audit(user_uid, conv["id"], um["id"], am["id"])

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/improvement-log/export?format=jsonl&days=1")
    assert r.status_code == 200, r.text
    ids = {json.loads(line)["message_id"] for line in r.text.splitlines() if line.strip()}
    assert am["id"] not in ids, "未バックフィルの personal_sources マーカーが見逃された"


def test_improvement_log_export_excludes_sanitized_snapshot_conversations():
    """sanitized share の複製（`conversations.origin='sanitized_snapshot'`）は元会話の内容を
    複製したものなので除外する（元会話自体は監査対応付けがあれば通常どおり出る＝
    「監査が無いから除外」ではなく「sanitized_snapshot だから除外」であることを区別する）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"ilsan{sfx}", f"IlSan{sfx}"
    user_uid, user_pw = f"ilsanu{sfx}", f"IlSanU{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")

    conv = store.create_conversation(user_id=user_uid, world="v1")
    um = store.add_message(conv["id"], "user", "サニタイズ複製元の質問")
    am = store.add_message(conv["id"], "assistant", "サニタイズ複製元の回答", lens="qa",
                           answer={"lens": "qa", "headline": "サニタイズ複製元の回答", "sources": []})
    _mk_chat_turn_audit(user_uid, conv["id"], um["id"], am["id"])
    snapshot_cid = store.create_sanitized_snapshot(user_uid, conv["id"])
    assert snapshot_cid is not None

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/improvement-log/export?format=jsonl&days=1")
    assert r.status_code == 200, r.text
    rows = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    conv_ids = {row["conversation_id"] for row in rows}
    assert snapshot_cid not in conv_ids, "sanitized_snapshot の複製がエクスポートに混入した"
    assert conv["id"] in conv_ids, "元会話（監査対応付けあり）まで除外された"


def test_improvement_log_export_excludes_soft_deleted_conversations_and_cascades_feedback():
    """共有先が生きている会話は物理削除できず soft delete（`deleted_at`）になる。抽出は
    `deleted_at IS NULL` の会話に限定し、soft delete 時に `message_feedback` も明示削除する。"""
    if not _try_init():
        pytest.skip("DB down")
    import hashlib
    sfx = _sfx()
    admin_uid, admin_pw = f"ilsoft{sfx}", f"IlSoft{sfx}"
    owner_uid, owner_pw = f"ilsofto{sfx}", f"IlSoftO{sfx}"
    viewer_uid, viewer_pw = f"ilsoftv{sfx}", f"IlSoftV{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(owner_uid, owner_pw, role="user")
    _mk_user(viewer_uid, viewer_pw, role="user")

    conv = store.create_conversation(user_id=owner_uid, world="v1")
    um = store.add_message(conv["id"], "user", "共有先が生きている会話の質問")
    am = store.add_message(conv["id"], "assistant", "共有先が生きている会話の回答", lens="qa",
                           answer={"lens": "qa", "headline": "共有先が生きている会話の回答",
                                  "sources": []})
    _mk_chat_turn_audit(owner_uid, conv["id"], um["id"], am["id"])

    owner = _login(owner_uid, owner_pw)
    fb_r = owner.post(f"/chat/{conv['id']}/messages/{am['id']}/feedback", json={"rating": "up"})
    assert fb_r.status_code == 200, fb_r.text
    assert store.get_feedback_by_message_ids([am["id"]])

    token_hash = hashlib.sha256(f"il-soft-share-{sfx}".encode()).hexdigest()
    share_id = store.create_share(conv["id"], owner_uid, token_hash, None, [viewer_uid])
    store.accept_share(share_id, viewer_uid)   # 生きた受領ラッパーを作る＝soft delete になる条件

    assert store.delete_conversation(conv["id"], owner_uid) is True
    with psycopg.connect(store._dsn()) as c:
        row = c.execute("SELECT deleted_at FROM conversations WHERE id=%s", (conv["id"],)).fetchone()
    assert row[0] is not None, "生きた受領共有がある会話が物理削除された（soft delete のはず）"
    assert store.get_feedback_by_message_ids([am["id"]]) == {}, \
        "soft delete 後も message_feedback が残っている"

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/improvement-log/export?format=jsonl&days=1")
    assert r.status_code == 200, r.text
    ids = {json.loads(line)["message_id"] for line in r.text.splitlines() if line.strip()}
    assert am["id"] not in ids, "soft delete 済みの会話がエクスポートに混入した"


@pytest.mark.parametrize("prefix", [
    "=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠",
])
def test_improvement_log_export_csv_escapes_all_formula_injection_trigger_prefixes(prefix):
    """半角/全角の = + - @・タブ・CR・LF いずれかで始まるセルは `'` を前置して無害化する
    （CSV インジェクション対策・OWASP WSTG 準拠・トリガー文字を1件ずつ全数固定する）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"ilcsv{sfx}", f"IlCsv{sfx}"
    user_uid, user_pw = f"ilcsvu{sfx}", f"IlCsvU{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")

    conv = store.create_conversation(user_id=user_uid, world="v1")
    cell_value = f"{prefix}cmd|'/c calc'!A1"
    um = store.add_message(conv["id"], "user", "質問本文")
    am = store.add_message(conv["id"], "assistant", cell_value, lens="qa",
                           answer={"lens": "qa", "headline": cell_value, "sources": []})
    _mk_chat_turn_audit(user_uid, conv["id"], um["id"], am["id"])

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/improvement-log/export?format=csv&days=1")
    assert r.status_code == 200, r.text
    reader = csv.DictReader(io.StringIO(r.text))
    row = next(x for x in reader if int(x["message_id"]) == am["id"])
    assert row["answer_head"] == "'" + cell_value


def test_improvement_log_export_csv_escapes_prefix_after_leading_space():
    """先頭の半角空白は無視して判定する（空白の後に = 等が来ても検知する）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"ilcsvsp{sfx}", f"IlCsvSp{sfx}"
    user_uid, user_pw = f"ilcsvspu{sfx}", f"IlCsvSpU{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")

    conv = store.create_conversation(user_id=user_uid, world="v1")
    um = store.add_message(conv["id"], "user", " =cmd|'/c calc'!A1")   # 先頭空白＋=
    am = store.add_message(conv["id"], "assistant", "普通の回答", lens="qa",
                           answer={"lens": "qa", "headline": "普通の回答", "sources": []})
    _mk_chat_turn_audit(user_uid, conv["id"], um["id"], am["id"])

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/improvement-log/export?format=csv&days=1")
    assert r.status_code == 200, r.text
    reader = csv.DictReader(io.StringIO(r.text))
    row = next(x for x in reader if int(x["message_id"]) == am["id"])
    assert row["question_head"] == "' =cmd|'/c calc'!A1"


def test_improvement_log_export_truncation_sets_header_trailer_and_audit(monkeypatch):
    """出力上限に到達したら無通知にせず、`X-Truncated` ヘッダ・JSONL 末尾行・監査 detail に残す。"""
    if not _try_init():
        pytest.skip("DB down")
    from sherpa import improvement_log
    sfx = _sfx()
    admin_uid, admin_pw = f"iltrunchdr{sfx}", f"IlTruncHdr{sfx}"
    user_uid, user_pw = f"iltrunchdru{sfx}", f"IlTruncHdrU{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")

    conv = store.create_conversation(user_id=user_uid, world="v1")
    for i in range(3):
        um = store.add_message(conv["id"], "user", f"上限確認用の質問{i}")
        am = store.add_message(conv["id"], "assistant", f"上限確認用の回答{i}", lens="qa",
                               answer={"lens": "qa", "headline": f"上限確認用の回答{i}", "sources": []})
        _mk_chat_turn_audit(user_uid, conv["id"], um["id"], am["id"])
    monkeypatch.setattr(improvement_log, "EXPORT_MAX_ROWS", 2)

    admin = _login(admin_uid, admin_pw)
    r = admin.get("/admin/improvement-log/export?format=jsonl&days=1")
    assert r.status_code == 200, r.text
    assert r.headers.get("x-truncated") == "true"
    lines = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert lines[-1] == {"truncated": True}
    assert len([x for x in lines if "message_id" in x]) == 2

    audit_rows = store.list_audit(actor=admin_uid, action="admin.improvement_log_exported", limit=5)
    assert audit_rows[0]["detail"]["truncated"] is True


# 「上限到達後の probe も taint 判定を通す（残りが個人情報の行だけなら truncated を立てない）」の
# 検証は tests/unit/test_improvement_log.py（`fetch_export_rows` を直接・決定的なデータセットで
# 呼ぶ）側で行う。このファイルは共有 dev DB に対する実 API 呼び出しのため、`truncated=False`
# （＝「この時点でこれ以上の行が無い」）という否定条件は、同時に動く他テスト/他セッションが
# 同じ期間内に別の行を書き込みうる環境では決定的に再現できない。
