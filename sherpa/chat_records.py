"""調査の記録（investigation_record）の保存と、回答への「記録済み」の印。
設計: docs/design/chat.md「仕上げと保存」
"""
from __future__ import annotations

import copy
import logging

from . import answer_shape, store
from .store import investigation_records as store_investigation

_log = logging.getLogger("sherpa")


def _mark_investigation_recorded(env: dict, investigation_record: dict | None) -> None:
    """`investigation_record`（provider が `_result` の別項目として渡した台帳・`None`＝台帳ゲートが走らなかったターン）があれば、`env["investigation"]` へ `recorded` の印を立てる。
    assistant message を `store.add_message` で保存する前に呼ぶこと（この印は `messages.answer` と一緒に永続化される・保存を試みる印であって保存成否ではない）。
    """
    if investigation_record is not None and isinstance(env.get("investigation"), dict):
        env["investigation"]["recorded"] = True


_INVESTIGATION_RECORD_SAVE_FAILED_NOTICE = ("調査の記録を保存できませんでした（回答は保存しています）。"
                                            "調査の記録のダウンロードは使えません。")


def _save_investigation_record(investigation_record: dict | None, message_id, conversation_id,
                               message_row: dict | None = None) -> dict | None:
    """`investigation_record` を `investigation_records` 表へ保存する。assistant message を保存した直後（`message_id` が実在する状態）に呼ぶこと（`message_id` は `messages(id)` への FK）。
    保存に失敗しても回答の保存は失敗させない（警告ログ）。失敗は回答の注記に残し、調査の記録のダウンロード導線を外す（`store.append_answer_notice`）。
    戻り値は、注記を足して更新した assistant 行（失敗しなかった・`None` のときは `None`）。呼び出し側は配信する行をこれで差し替える。
    注記の保存も失敗したときは、渡された `message_row`（保存済みの assistant 行）の写しに注記を足し、記録済みの印を外した行を返す（画面には知らせる。保存済みの行は直せない）。
    """
    if investigation_record is None:
        return None
    try:
        store_investigation.save_investigation_record(
            message_id, conversation_id,
            complete=bool(investigation_record.get("complete")),
            manifest=investigation_record.get("manifest"),
            items=investigation_record.get("items") or {},
            coverage=investigation_record.get("coverage") or {},
            reviews=investigation_record.get("reviews") or [],
            coverage_detail=investigation_record.get("coverage_detail") or {},
            extras=investigation_record.get("extras") or {})
        return None
    except Exception as e:
        _log.warning("investigation record save failed: %s", e)
    try:
        return store.append_answer_notice(message_id, "investigation_record_failed",
                                          _INVESTIGATION_RECORD_SAVE_FAILED_NOTICE, unrecorded=True)
    except Exception as e:
        _log.warning("investigation record failure notice save failed: %s", e)
        return _unrecorded_live_row(message_row)


def _unrecorded_live_row(message_row: dict | None) -> dict | None:
    """保存済みの行を変えず、配信用の写しに「記録を保存できなかった」注記を足して記録済みの印を外す。行が無ければ `None`。"""
    if not isinstance(message_row, dict) or not isinstance(message_row.get("answer"), dict):
        return None
    answer = copy.deepcopy(message_row["answer"])
    answer_shape.add_notice(answer, "investigation_record_failed", _INVESTIGATION_RECORD_SAVE_FAILED_NOTICE)
    if isinstance(answer.get("investigation"), dict):
        answer["investigation"]["recorded"] = False
    answer_shape.seal(answer)
    return {**message_row, "answer": answer, "content": answer.get("headline") or message_row.get("content", "")}
