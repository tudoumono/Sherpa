"""チャットターンの仕上げの部品（実行前の失敗の保存・継続情報の保存・個人由来の判定）。
設計: docs/design/chat.md「仕上げと保存」
"""
from __future__ import annotations

import copy
import logging

from . import answer_shape, store
from .store import turn_metrics
from .chat_personal import _personal_citations, personal_unreadable_notice

_log = logging.getLogger("sherpa")

SESSION_UNSAVED_NOTICE = ("回答は保存しましたが、AI の継続情報を保存できませんでした。"
                          "次の質問は、保存済みの会話から新しく調べ直します。")


STATS_UNSAVED_NOTICE = "この回答の利用統計を保存できませんでした。利用統計の合計に含まれない場合があります。"


def fail_before_run(*, conversation_id, user_id, settings, message, personal, world, scope_meta,
                    on_user_saved, headline):
    """AI を呼ぶ前の準備（会話の履歴の読み取り）に失敗したターンを、質問と失敗の回答として会話に残して終える。
    成功したように続けない。`answer` イベントを 1 件 yield する。
    """
    from .chat_service import _audit_chat_turn
    user_msg = store.add_message(conversation_id, "user", message, personal=personal)
    if on_user_saved is not None:
        on_user_saved(user_msg["id"], personal)
    if personal:
        store.set_contains_personal_workspace(conversation_id)
    yield {"type": "trace_meta", "trace_version": 2}
    env = {"lens": "chat", "headline": headline, "summary": {"total": 0}, "data": {}, "sources": [],
           "completion": "failed", "agentic_failure": "error", "trace_version": 2}
    answer_shape.seal(env)
    msg = store.add_message(conversation_id, "assistant", env["headline"], lens="chat", answer=env,
                            personal=personal)
    _audit_chat_turn(user_id, conversation_id, settings, lens="error", user_msg_id=user_msg["id"],
                     assistant_msg_id=msg["id"], world=world,
                     scope_paths=(scope_meta or {}).get("scope_paths"), personal=personal)
    yield {"type": "answer", "conversation_id": conversation_id, "message": msg}


def save_session_id(conversation_id, env: dict) -> None:
    """Codex が捕捉した session id を会話に保存する（次の質問の resume 用）。失敗しても回答は成立させ、継続できないことを回答の注記に残す。"""
    sid = env.get("codex_session_id")
    if not sid:
        return
    try:
        store.set_session_id(conversation_id, sid)
    except Exception as e:
        _log.warning("codex session id の保存に失敗しました（回答は保存します）: %s", type(e).__name__)
        answer_shape.add_notice(env, "session_unsaved", SESSION_UNSAVED_NOTICE)


def resolve_used_personal(env: dict, conversation_id, *, personal: bool, personal_hits,
                          round_personal: bool) -> bool:
    """このターンが個人由来かを決め、個人ファイルの出典と「読み取れなかった」注記を回答へ足す。
    ヒット・書込・巡ループの累積・トグル ON・過去ターンの個人由来のどれかなら True。判定できなければ個人扱いへ倒す。
    """
    used = False
    if personal_hits and not env.get("busy"):
        env["personal_sources"] = _personal_citations(personal_hits)
        used = True
    unreadable = personal_unreadable_notice(personal_hits)
    if unreadable and not env.get("busy"):
        answer_shape.add_notice(env, "personal_unreadable", unreadable)
    if env.get("codex_wrote_files") or round_personal or env.get("wrote_files") or personal:
        used = True
    if not used:
        try:
            used = bool(store.conversation_is_personal_tainted(conversation_id))
        except Exception:
            used = True
    return used


def note_stats_unsaved(msg: dict) -> dict:
    """保存した回答の利用統計の書込に失敗していたら、回答に欠測の注記を足した行を返す（失敗していなければ `msg` のまま）。
    注記の保存が失敗したときは、配信用の写しに注記を足す。
    """
    if not turn_metrics.pop_failure(msg["id"]):
        return msg
    try:
        updated = store.append_answer_notice(msg["id"], "stats_unsaved", STATS_UNSAVED_NOTICE)
    except Exception as e:
        _log.warning("利用統計の欠測の注記を保存できませんでした: %s", type(e).__name__)
        updated = None
    if updated is not None:
        return updated
    answer = copy.deepcopy(msg.get("answer") or {})
    answer_shape.add_notice(answer, "stats_unsaved", STATS_UNSAVED_NOTICE)
    return {**msg, "answer": answer}
