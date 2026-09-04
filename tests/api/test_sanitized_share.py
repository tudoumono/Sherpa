"""sanitized share snapshot（2026-07-01-認証と共有の提案.md §Phase2）テスト。

  - _sanitize_answer: 個人ターンは本文伏字＋個人 citation/facts 除去、非個人ターンは保持（純関数）。
  - create_sanitized_snapshot: 個人参照会話の凍結コピーを作り、個人本文が漏れない。
  - snapshot は list_conversations に出ない（内部成果物）。
  - snapshot は contains_personal_workspace=FALSE（受領共有 guard を通過して読める）。

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import pytest

from _test_users import register_test_uid
from sherpa import store


def _db() -> bool:
    try:
        store._ensure()
        return True
    except Exception:
        return False


# ---- 純関数: allowlist 再構築 ----
def test_safe_share_answer_allowlist():
    """未知キー・個人由来・route/trace を持ち込まず、KB 由来だけを allowlist で再構築。"""
    out = store._safe_share_answer({
        "headline": "KB 由来の回答", "lens": "impact", "data": {"cards": 1}, "summary": {"total": 3},
        "scope": {"world": "test"},
        "sources": [{"doc_id": "kb1", "source": "KB", "quote": "x"},
                    {"doc_id": "p1", "source": "個人ファイル内ヒット", "quote": "secret"}],
        "personal_sources": [{"doc_id": "salary.xlsx"}],       # 個人由来
        "route": {"tool": "grep salary.xlsx"},                 # 内部・落とす
        "_personal_facts": "年収900万",                         # 落とす
        "unknown_key": "leak?",                                 # 未知キー・落とす
    })
    assert out["headline"] == "KB 由来の回答" and out["lens"] == "impact"
    assert [s["doc_id"] for s in out["sources"]] == ["kb1"]     # 個人ヒット除去
    assert "secret" not in str(out)                            # 個人 quote 消える
    assert "personal_sources" not in out and "route" not in out
    assert "_personal_facts" not in out and "unknown_key" not in out
    assert "salary.xlsx" not in str(out) and "900万" not in str(out)


def test_safe_share_answer_drops_bad_lens():
    out = store._safe_share_answer({"headline": "x", "lens": "個人的な内部ラベル"})
    assert "lens" not in out


# ---- DB 統合 ----
def _mk_conv_with_personal(uid: str):
    """個人ターン（personal=True・Q/A とも）＋非個人ターンを持つ会話。title に個人ファイル名を入れて漏れ検査。"""
    register_test_uid(uid)   # 固定 uid の会話残骸防止（users 行が無くても conversations を掃除する・2026-07）
    conv = store.create_conversation(user_id=uid, world="v1", title="my_salary.xlsx を要約して")
    cid = conv["id"]
    # 個人ターン: 質問＋回答（personal=True・route/trace に内部情報）
    um = store.add_message(cid, "user", content="my_salary.xlsx を要約して", personal=True)
    store.add_message(cid, "assistant", content="個人ファイルによると年収は 900万 です",
                      route={"tool": "grep my_salary.xlsx"}, trace={"q": "900万"},
                      answer={"headline": "個人ファイルによると年収は 900万 です",
                              "personal_sources": [{"doc_id": "my_salary.xlsx", "source": "個人ファイル内ヒット"}]},
                      personal=True)
    # 非個人ターン: KB 由来
    store.add_message(cid, "user", content="TAX-RATE の影響は？")
    store.add_message(cid, "assistant", content="共有KBの一般回答",
                      route={"tool": "es_search TAX-RATE"},
                      answer={"headline": "共有KBの一般回答", "lens": "impact",
                              "sources": [{"doc_id": "kb1", "source": "KB"}]})
    store.set_contains_personal_workspace(cid)
    return cid


def test_snapshot_no_personal_leak_anywhere():
    """RV BLOCKER 反映: title/content/answer/route/trace のどこにも個人データが漏れない。"""
    if not _db():
        pytest.skip("DB down")
    uid = "shareu_snap"
    cid = _mk_conv_with_personal(uid)
    snap = store.create_sanitized_snapshot(uid, cid)
    assert snap is not None
    got = store.get_conversation_for_read(uid, snap)
    conv, msgs = got["conversation"], got["messages"]
    assert conv["contains_personal_workspace"] is False
    # title が固定文言（元 title のファイル名を漏らさない）
    assert conv["title"] == store._SANITIZED_TITLE, f"title leak: {conv['title']}"
    # 全メッセージの content/answer/route/trace を連結して個人データを検査
    blob = conv["title"]
    for m in msgs:
        blob += (m.get("content") or "") + str(m.get("answer") or "") + str(m.get("route") or "") + str(m.get("trace") or "")
    for leak in ("my_salary.xlsx", "900万", "個人ファイル内ヒット"):
        assert leak not in blob, f"個人データ '{leak}' が snapshot に漏れている"
    assert store._REDACTED_TEXT in blob, "個人ターンが伏字化されていない"
    assert "共有KBの一般回答" in blob and "TAX-RATE" in blob, "非個人ターンは保持されるべき"


def test_snapshot_hidden_from_conversation_list():
    if not _db():
        pytest.skip("DB down")
    uid = "shareu_snap2"
    cid = _mk_conv_with_personal(uid)
    snap = store.create_sanitized_snapshot(uid, cid)
    ids = [c["id"] for c in store.list_conversations(user_id=uid, limit=100)]
    assert snap not in ids, "sanitized_snapshot が会話一覧に出ている（内部成果物のはず）"
    assert cid in ids, "元会話は一覧に出るべき"


def test_toggle_marks_user_message_at_save_in_source():
    """RV BLOCKER(in-flight): トグルONは保存時点で質問を個人扱いにし、provider 前に会話フラグを立てる。"""
    import inspect
    from sherpa import chat_service
    for fn in (chat_service.handle_message, chat_service.stream_message):
        src = inspect.getsource(fn)
        # 保存時点で personal=personal（provider 前）＋会話フラグを provider 前に立てる。
        assert 'add_message(conversation_id, "user", message, personal=personal)' in src, \
            f"{fn.__name__}: user 保存時に personal=personal になっていない（in-flight 漏れ）"
        i_save = src.index('add_message(conversation_id, "user", message, personal=personal)')
        i_flag = src.index("set_contains_personal_workspace(conversation_id)", i_save)
        # provider は `_provider = ... get_provider(...)` で解決してから `.run` する2行形
        # （サブエージェント並用の合流以降）。`.run` まで含めた単一文はもう存在しないため、
        # 解決行を提示に使う——解決は実行より必ず前なので「フラグ < 解決」は「フラグ < 実行」を含意する。
        i_provider = src.index("get_provider(settings, system_settings=sys_settings)", i_save)
        assert i_flag < i_provider, f"{fn.__name__}: 会話フラグが provider 実行より後（in-flight 漏れ）"


def test_snapshot_redacts_old_taint_markers():
    """RV BLOCKER(backfill無し): messages.personal=false でも answer に旧マーカーがあれば snapshot で伏字。
    直前の user 質問も 2-pass で伏字化される。"""
    if not _db():
        pytest.skip("DB down")
    uid = "shareu_old"
    register_test_uid(uid)   # 固定 uid の会話残骸防止（2026-07）
    conv = store.create_conversation(user_id=uid, world="v1", title="旧データ")
    cid = conv["id"]
    # 旧データを模擬: personal 列は False（backfill 前）だが answer に旧 taint マーカー。
    store.add_message(cid, "user", content="secret_old.xlsx を要約して", personal=False)
    store.add_message(cid, "assistant", content="旧データによると値は 777 です", personal=False,
                      answer={"headline": "旧データによると値は 777 です",
                              "personal_sources": [{"doc_id": "secret_old.xlsx", "source": "個人ファイル内ヒット"}]})
    store.add_message(cid, "assistant", content="codex 生成ターン", personal=False,
                      answer={"headline": "report を作成しました", "codex_wrote_files": ["report.docx"]})
    store.set_contains_personal_workspace(cid)
    snap = store.create_sanitized_snapshot(uid, cid)
    got = store.get_conversation_for_read(uid, snap)
    blob = got["conversation"]["title"]
    for m in got["messages"]:
        blob += (m.get("content") or "") + str(m.get("answer") or "")
    for leak in ("secret_old.xlsx", "777", "個人ファイル内ヒット", "report.docx"):
        assert leak not in blob, f"旧マーカー由来の個人データ '{leak}' が snapshot に漏れている"
    assert store._REDACTED_TEXT in blob, "旧 taint ターンが伏字化されていない"


def test_snapshot_drops_question_card_payload():
    """S1（ask_user-improvements.md）: sanitized snapshot は確認カード（answer.question）を
    持ち込まない（_safe_share_answer の allowlist に question が無い）。interaction_id・options 等の
    内部情報が受領者に漏れないことを固定する（sanitized 側で question を伏せる要件）。"""
    if not _db():
        pytest.skip("DB down")
    uid = "shareu_q"
    register_test_uid(uid)   # 固定 uid の会話残骸防止（2026-07）
    conv = store.create_conversation(user_id=uid, world="v1", title="確認あり会話")
    cid = conv["id"]
    store.add_message(cid, "user", content="税率を変えたら夜間バッチが落ちる？")
    store.add_message(cid, "assistant", content="どの調べ方をしますか？", lens="clarify",
                      answer={"lens": "clarify", "question": {
                          "interaction_id": "lens-deadbeef", "mode": "single",
                          "prompt": "どの調べ方をしますか？",
                          "options": [{"id": "impact", "label": "影響範囲該否", "description": "説明文"}],
                          "allow_free_text": False,
                          "original_message": "税率を変えたら夜間バッチが落ちる？"}})
    snap = store.create_sanitized_snapshot(uid, cid)
    assert snap is not None
    got = store.get_conversation_for_read(uid, snap)
    blob = ""
    for m in got["messages"]:
        blob += (m.get("content") or "") + str(m.get("answer") or "")
        assert not (isinstance(m.get("answer"), dict) and m["answer"].get("question")), \
            "snapshot の answer に question payload が残っている"
    for leak in ("lens-deadbeef", "影響範囲該否", "説明文", "original_message"):
        assert leak not in blob, f"確認カードの内部情報 '{leak}' が snapshot に漏れている"


def test_snapshot_drops_usage():
    """F3（2026-07-07-フィードバック一括.md）: sanitized snapshot は answer.usage を持ち込まない
    （_safe_share_answer の allowlist に usage が無く自動で落ちる＝無改修で守られることを固定）。"""
    if not _db():
        pytest.skip("DB down")
    uid = "shareu_usage"
    register_test_uid(uid)
    conv = store.create_conversation(user_id=uid, world="v1", title="usage含む会話")
    cid = conv["id"]
    store.add_message(cid, "user", content="TAX-RATE の影響は？")
    store.add_message(cid, "assistant", content="共有KBの一般回答", lens="impact",
                      answer={"headline": "共有KBの一般回答", "lens": "impact",
                              "sources": [{"doc_id": "kb1", "source": "KB"}],
                              "usage": {"provider": "codex", "model": "gpt-5.5",
                                        "input_tokens": 341026, "cached_input_tokens": 244864,
                                        "output_tokens": 12318, "reasoning_output_tokens": 9392}})
    snap = store.create_sanitized_snapshot(uid, cid)
    assert snap is not None
    got = store.get_conversation_for_read(uid, snap)
    for m in got["messages"]:
        assert not (isinstance(m.get("answer"), dict) and "usage" in m["answer"]), \
            "snapshot の answer に usage が残っている"
        assert "341026" not in str(m.get("answer") or "")
    # 非個人ターンの KB 由来 headline/sources は保持される（漏れないだけで空にはしない）。
    asst = next(m for m in got["messages"] if m["role"] == "assistant")
    assert asst["answer"]["headline"] == "共有KBの一般回答"


def test_snapshot_clarify_answer_is_exactly_lens_only():
    """RV Med（Codex 2026-07-07）: `_SHARE_SAFE_LENS` に clarify が無かったため、sanitized snapshot の
    確認カードは lens ごと落ちて `answer={}` になり、chat.js の `m.answer.lens === 'clarify'`
    プレースホルダ分岐（「（確認のやり取り）」）に入れず空白/崩れ表示になっていた。
    非個人ターンの clarify メッセージは answer が `{"lens": "clarify"}` の**み**（question は無い・
    他の一般 allowlist キーも持ち込まない）で保存されることを固定する。"""
    if not _db():
        pytest.skip("DB down")
    uid = "shareu_clarify_shape"
    register_test_uid(uid)
    conv = store.create_conversation(user_id=uid, world="v1", title="確認カードのみの会話")
    cid = conv["id"]
    store.add_message(cid, "user", content="税率を変えたら夜間バッチが落ちる？")
    store.add_message(cid, "assistant", content="どの調べ方をしますか？", lens="clarify",
                      answer={"lens": "clarify", "question": {
                          "interaction_id": "lens-shapecheck", "mode": "single",
                          "prompt": "どの調べ方をしますか？", "options": [], "allow_free_text": False}})
    snap = store.create_sanitized_snapshot(uid, cid)
    assert snap is not None
    got = store.get_conversation_for_read(uid, snap)
    saved = next(m for m in got["messages"] if m["role"] == "assistant")
    assert saved["answer"] == {"lens": "clarify"}, f"clarify answer が最小形になっていない: {saved['answer']}"
    assert saved["lens"] == "clarify", "messages.lens 列も clarify として保持されるはず（allowlist に追加済み）"


def test_snapshot_personal_clarify_still_redacted_qa():
    """RV Med 確認事項: 個人ターンの clarify は既存の per-turn personal 伏字が優先される
    （Q/A とも `_REDACTED_TEXT` に伏字化・clarify 専用の {"lens":"clarify"} 最小形にはならない）。
    tainted 判定が先に評価されるため、clarify の特別扱いが個人情報の伏字を弱めないことを確認する。"""
    if not _db():
        pytest.skip("DB down")
    uid = "shareu_clarify_personal"
    register_test_uid(uid)
    conv = store.create_conversation(user_id=uid, world="v1", title="個人参照中の確認")
    cid = conv["id"]
    store.add_message(cid, "user", content="my_salary.xlsx の件で確認したい", personal=True)
    store.add_message(cid, "assistant", content="どの調べ方をしますか？", lens="clarify", personal=True,
                      answer={"lens": "clarify", "question": {
                          "interaction_id": "lens-personalcheck", "mode": "single",
                          "prompt": "my_salary.xlsx について、どの調べ方をしますか？",
                          "options": [], "allow_free_text": False}})
    store.set_contains_personal_workspace(cid)
    snap = store.create_sanitized_snapshot(uid, cid)
    got = store.get_conversation_for_read(uid, snap)
    saved_user, saved_assistant = got["messages"]
    assert saved_user["content"] == store._REDACTED_TEXT
    assert saved_assistant["content"] == store._REDACTED_TEXT
    assert saved_assistant["answer"] == {"headline": store._REDACTED_TEXT}
    assert saved_assistant["lens"] is None
    assert "my_salary.xlsx" not in str(saved_assistant["answer"])


def test_conversation_has_personal_message():
    """多層防御: 個人ターンが1件でもあれば True（会話フラグとは独立）。"""
    if not _db():
        pytest.skip("DB down")
    uid = "shareu_guard"
    register_test_uid(uid)   # 固定 uid の会話残骸防止（2026-07）
    conv = store.create_conversation(user_id=uid, world="v1", title="g")
    cid = conv["id"]
    store.add_message(cid, "user", content="通常の質問")
    assert store.conversation_has_personal_message(cid) is False
    store.add_message(cid, "assistant", content="個人ターン", personal=True)
    assert store.conversation_has_personal_message(cid) is True


def test_share_guard_uses_message_level_check_in_source():
    """RV 任意提案: 通常共有 guard が会話フラグ OR conversation_has_personal_message で拒否する。"""
    import inspect
    from sherpa import api
    src = inspect.getsource(api.conversation_share_create)
    assert "conversation_has_personal_message(cid)" in src, \
        "通常共有 guard に message レベルの個人チェックが無い（フラグ単一依存）"
