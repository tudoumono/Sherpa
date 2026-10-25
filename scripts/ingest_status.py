#!/usr/bin/env python3
"""資料フォルダごとの取り込み状態を、保存済みの記録だけから表示する。"""
from __future__ import annotations

import os
import signal
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sherpa import store
from sherpa.ingest import derived_generation, failure_reasons
from sherpa.store import ingest as ingest_store
from sherpa.store import ocr_jobs

_QUERY_TIMEOUT_SEC = 3
_IN_FLIGHT_STATUSES = ("queued", "extracting")


@contextmanager
def _database_timeouts():
    """このコマンドの問い合わせだけに短い上限を付ける。"""
    values = {
        "PGCONNECT_TIMEOUT": "2",
        "PGOPTIONS": "-c statement_timeout=2000",
        "SHERPA_PG_POOL_TIMEOUT": "2",
    }
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class _QueryTimeout(TimeoutError):
    pass


def _timed_read(reader: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """1回の読み取りを短い時間で打ち切る。"""
    previous = signal.getsignal(signal.SIGALRM)

    def _expired(_signum: int, _frame: Any) -> None:
        raise _QueryTimeout

    signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, _QUERY_TIMEOUT_SEC)
    try:
        return reader(*args, **kwargs)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


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
    latest = _timed_read(
        ingest_store.get_latest_run_summary, world_id,
        connect_timeout=2.0, statement_timeout_ms=2000,
    )
    runs = _timed_read(
        ingest_store.list_ingest_runs, world_id, 50,
        connect_timeout=2.0, statement_timeout_ms=2000,
    )
    completed_at = _timed_read(ingest_store.get_latest_completed_run_at, world_id)
    last_finished = next((run for run in runs if run.get("status") not in _IN_FLIGHT_STATUSES), None)
    failed = last_finished if (last_finished or {}).get("status") == "failed" else None

    signature = str(row.get("last_sig") or "").strip()
    generation = derived_generation.generation_id_for(signature) if signature else None
    ocr = _timed_read(ocr_jobs.status_summary, world_id, generation)
    publication_wait = 0
    if generation is not None:
        generation_status = _timed_read(ocr_jobs.generation_state, world_id, generation)
        publication_wait = int(generation_status.get("unpublished_jobs") or 0)

    return {
        "name": _folder_name(row),
        "published_at": completed_at,
        "current": _current_text(latest),
        "failed_at": (failed or {}).get("created_at"),
        "failed_reason": _reason_text((failed or {}).get("extraction_snapshot")) if failed else None,
        "ocr_pending": int(ocr.get("pending") or 0),
        "ocr_processed": int(ocr.get("processed") or 0),
        "ocr_failed": int(ocr.get("failed") or 0),
        "ocr_publication_wait": publication_wait,
    }


def main() -> int:
    try:
        with _database_timeouts():
            rows = _timed_read(store.list_worlds_db)
            folders = [_read_folder(row) for row in rows]
    except Exception:  # noqa: BLE001 - DB例外の種類にかかわらず状態表示を最後まで返す。
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
        print(f"  最後まで終わった取り込み: {_time_text(folder['published_at'])}")
        print(f"  今の取り込み: {folder['current']}")
        if folder["failed_reason"] is None:
            print("  前回の失敗: なし")
        else:
            print(f"  前回の失敗: {_time_text(folder['failed_at'])}／{folder['failed_reason']}")
        print(
            "  画像内文字の読み取り: "
            f"待ち {folder['ocr_pending']}件／"
            f"読み取り済み {folder['ocr_processed']}件／"
            f"失敗 {folder['ocr_failed']}件／"
            f"公開待ち {folder['ocr_publication_wait']}件"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
