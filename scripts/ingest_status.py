#!/usr/bin/env python3
"""資料フォルダごとの取り込み状態を、保存済みの記録だけから表示する。"""
from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

from psycopg import errors as pg_errors

from sherpa import store
from sherpa.ingest import derived_generation, failure_reasons
from sherpa.store import ingest as ingest_store
from sherpa.store import ocr_jobs

_CONNECT_TIMEOUT_SEC = 3.0
_DEFAULT_READ_TIMEOUT_SEC = 10.0
_UNREAD_TEXT = "時間内に読めませんでした"


class _Unread:
    """時間内に読めなかった欄の印。"""


_UNREAD = _Unread()


class _Unreachable(Exception):
    pass


def _read_timeout_ms() -> int:
    """読み取り1回の上限（ミリ秒）。サーバー側の statement_timeout に渡す。"""
    try:
        seconds = float(os.environ.get("SHERPA_INGEST_STATUS_TIMEOUT_SEC", ""))
    except ValueError:
        seconds = 0.0
    if seconds <= 0:
        seconds = _DEFAULT_READ_TIMEOUT_SEC
    return max(1, int(seconds * 1000))


def _bounded_read(reader: Callable[..., Any], *args: Any) -> Any:
    """専用接続で1回読む。時間切れなら `_UNREAD`、接続できないなどは `_Unreachable`。"""
    try:
        return reader(*args, connect_timeout=_CONNECT_TIMEOUT_SEC, statement_timeout_ms=_read_timeout_ms())
    except pg_errors.QueryCanceled:
        return _UNREAD
    except Exception as exc:  # noqa: BLE001 - DB例外の種類にかかわらず「届かない」として扱う。
        raise _Unreachable from exc


def _time_text(value: Any) -> str:
    if not isinstance(value, datetime):
        return "記録なし"
    local = value.astimezone() if value.tzinfo else value
    return local.strftime("%Y-%m-%d %H:%M:%S")


def _elapsed_text(started: Any) -> str:
    if not isinstance(started, datetime):
        return "不明"
    now = datetime.now(started.tzinfo) if started.tzinfo else datetime.now()
    seconds = max(0, int((now - started).total_seconds()))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f"{hours}時間{minutes}分"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"


_CLASSIFIED_PREFIXES = ("es_index_failed", "graph_reflect_failed", "pg_replace_failed", "rebind_")
_CLASSIFIED_REASONS = tuple(sorted(
    failure_reasons.known_reasons() | {"world_not_found", "world_dir_unreachable"}))


def _reason_text(snapshot: Any) -> str:
    if not isinstance(snapshot, dict):
        return "理由の記録がありません"
    if snapshot.get("stop_reason") == "low_disk":
        return "ディスクの空きが少ないため"
    if snapshot.get("interrupted") is True:
        return "途中で停止したため"
    flags = snapshot.get("flags")
    if isinstance(flags, list):
        reasons = [flag.get("reason") for flag in flags
                   if isinstance(flag, dict) and isinstance(flag.get("reason"), str)]
        for raw in reasons:
            known = failure_reasons.describe(raw)
            if known["code"] != "other":
                return known["label"]
            if raw.startswith("es_index_failed"):
                return "全文検索への反映に失敗したため"
            if raw.startswith("graph_reflect_failed"):
                return "資料のつながりの反映に失敗したため"
            if raw.startswith("pg_replace_failed"):
                return "取り込み結果の保存に失敗したため"
            if raw in ("world_not_found", "world_dir_unreachable"):
                return "資料フォルダを読み取れなかったため"
            if raw.startswith("rebind_"):
                return "資料フォルダの参照先を変更できなかったため"
        if reasons:
            return "詳しい理由はログを確認してください"
    return "理由の記録がありません"


def _current_text(latest: dict[str, Any] | None) -> str:
    if not latest:
        return "なし"
    status = latest.get("status")
    if status not in ("queued", "extracting"):
        return "なし"
    progress_value = latest.get("progress")
    if status == "extracting" and not isinstance(progress_value, dict):
        return "なし"
    progress = progress_value if isinstance(progress_value, dict) else {}
    if status == "queued":
        state, stage = "待っています", "受け付け済み"
    else:
        state = "取り込み中"
        stage = progress.get("stage_label") or "準備中"
    return f"{state}／段: {stage}／経過: {_elapsed_text(latest.get('created_at'))}"


def _folder_name(row: dict[str, Any]) -> str:
    label = row.get("label")
    world_id = str(row.get("world_id") or "")
    if isinstance(label, str) and label.strip() and label.strip() != world_id:
        return f"{label.strip()}（{world_id}）"
    return world_id


def _read_folder(row: dict[str, Any]) -> dict[str, Any]:
    world_id = str(row["world_id"])
    completed_at = _bounded_read(ingest_store.get_latest_completed_run_at, world_id)
    latest = _bounded_read(ingest_store.get_latest_run_progress, world_id)
    finished = _bounded_read(
        ingest_store.get_last_finished_run_brief, world_id, _CLASSIFIED_REASONS, _CLASSIFIED_PREFIXES)

    signature = str(row.get("last_sig") or "").strip()
    generation = derived_generation.generation_id_for(signature) if signature else None
    ocr = _bounded_read(ocr_jobs.get_progress_counts, world_id, generation)

    failed_at: Any = None
    failed_reason: str | None = None
    if finished is not _UNREAD and finished is not None and finished.get("status") == "failed":
        failed_at = finished.get("created_at")
        failed_reason = _reason_text(finished.get("snapshot"))

    return {
        "name": _folder_name(row),
        "published_at": completed_at,
        "current": _UNREAD_TEXT if latest is _UNREAD else _current_text(latest),
        "failed_unread": finished is _UNREAD,
        "failed_at": failed_at,
        "failed_reason": failed_reason,
        "ocr": ocr,
    }


def main() -> int:
    try:
        rows = _bounded_read(store.list_worlds_db)
        if rows is _UNREAD:
            raise _Unreachable
        folders = [_read_folder(row) for row in rows]
    except _Unreachable:
        print("データベースに届きません")
        return 0

    print("取り込みの状態")
    if not folders:
        print("資料フォルダはありません。")
        return 0
    for index, folder in enumerate(folders):
        if index:
            print()
        print(f"資料フォルダ: {folder['name']}")
        published = folder["published_at"]
        print(f"  最後まで終わった取り込み: {_UNREAD_TEXT if published is _UNREAD else _time_text(published)}")
        print(f"  今の取り込み: {folder['current']}")
        if folder["failed_unread"]:
            print(f"  前回の失敗: {_UNREAD_TEXT}")
        elif folder["failed_reason"] is None:
            print("  前回の失敗: なし")
        else:
            print(f"  前回の失敗: {_time_text(folder['failed_at'])}／{folder['failed_reason']}")
        ocr = folder["ocr"]
        if ocr is _UNREAD:
            print(f"  画像内文字の読み取り: {_UNREAD_TEXT}")
        else:
            print(
                "  画像内文字の読み取り: "
                f"待ち {ocr['pending']}件／"
                f"読み取り済み {ocr['processed']}件／"
                f"失敗 {ocr['failed']}件／"
                f"公開待ち {ocr['unpublished']}件"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
