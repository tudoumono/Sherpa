"""LLM での書き換えの失敗を、`failed` の回として残す。
実行中は回を作らない（一覧に出さず、依頼の衝突判定にも画面の操作止めにも掛けない）。失敗したときだけ、入れ替えの行を残したまま回を 1 件足す。
設計: docs/design/operations.md「止まった取り込みの見え方」
"""
from __future__ import annotations

import logging
from datetime import datetime

from .. import store
from ..store import replace_rows
from ..store.replace_rows import ReplaceRow

_log = logging.getLogger("sherpa")

OP = "llm_rewrite"


def record(world: str, exc: BaseException, *, started_at: datetime) -> None:
    """書き換えの後追いが例外で終わったので、`failed` の回を残す（入れ替えの行は呼び出し元が持っていた行のまま残る）。記録できなくても後追いは続ける。
    `started_at` は書き換えを始めた時刻（この間に受け付けた依頼は、失敗より後に動くので状態では依頼の回を先に見せる）。
    """
    _add_failed_run(world, f"llm_rewrite_failed:{exc.__class__.__name__}", started_at)


def fail_reindex(world: str, *, row: ReplaceRow) -> None:
    """書き換えた rag.md の ES への反映が済まなかったので、ES の項目を「失敗」にして行を残し、`failed` の回を残す。"""
    replace_rows.mark_failed(row, world, "es", "llm_rewrite_reindex_failed")
    _add_failed_run(world, "llm_rewrite_reindex_failed", replace_rows.created_at(row))


def _add_failed_run(world: str, reason: str, started_at: datetime | None) -> None:
    try:
        store.add_ingest_run(
            world, status="failed", op=OP,
            extraction_snapshot={"op": OP, "flags": [{"doc": None, "action": "blocked", "reason": reason}],
                                 **({"started_at": started_at.isoformat()} if started_at else {})})
    except Exception:
        _log.warning("LLM 成形の失敗の記録に失敗しました（best-effort）: world=%s", world, exc_info=True)
