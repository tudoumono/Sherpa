"""ES 再索引と、その成否の記録（human_md の ES 反映マーカーの確定を含む）。"""
from __future__ import annotations

import logging
from collections.abc import Callable

from .. import es_index, store, worlds
from ..store import replace_rows
from ..store.replace_rows import ReplaceRow
from . import background, office_md

_log = logging.getLogger("sherpa.ingest.convert")


MARKER_FAILURE_REASONS = {
    "sig_marker": "human_md_es_sig_marker_write_failed",
    "meta": "human_md_es_meta_confirm_failed",
}


def confirm_es_markers(world: str) -> str | None:
    """bulk 成功後に、`.human_md_es_sig` と ES の `_meta.human_md_sig` を現行値で確定する。

    戻り値は、確定できたか対象外なら None、印の書き込みを見送った・失敗したら "sig_marker"、ES 側のメタの書き直しに失敗したら "meta"。
    """
    world_dir = worlds.world_dir(world)
    derived_md_dir = worlds.derived_md_dir(world)
    if not (world_dir and derived_md_dir.exists()):
        return None
    if not office_md.confirm_human_md_es_sig(world_dir, derived_md_dir, world=world):
        return "sig_marker"
    if not es_index.confirm_human_md_meta(world):
        return "meta"
    return None


def keep_row_on_es_failure(world: str, row: ReplaceRow, esr: dict) -> None:
    """全体の取り込みで ES が失敗した（届かなかった場合を含む）なら、行の `es` を「失敗」にして残す（回の状態は変えない）。"""
    if esr.get("available") is not True or esr.get("error"):
        replace_rows.mark_failed(row, world, "es", str(esr.get("error") or "unavailable"))


def index_world_with_human_md_holdback(world: str, *, row: ReplaceRow, content_sig=None, settings: dict | None = None,
                                       run_id: int | None = None,
                                       progress: Callable[[int, int], None] | None = None) -> dict:
    """`es_index.index_world()` を、human_md の ES 反映ホールドバック込みで呼ぶ共通ヘルパ。

    ① 呼び出し前に `.human_md_es_sig` マーカーを無効化する（確定済みマーカーが残ると、bulk が部分失敗しても meta が確定値になり欠けた索引が固定されるため）。
       無効化に失敗（`OSError`）したら `index_world()` を呼ばず、失敗を記録して終える。
    ② bulk が成功（`available` かつ `error` なし）したときだけ `confirm_es_markers` でマーカーと ES 側の `_meta.human_md_sig` を現行署名へ確定する（書かないと次回 `needs_reindex()` が収束しない）。
    `ingest_runs` へ `status="failed"` で記録するのは (a) マーカー無効化の失敗 (b) `index_world()` の失敗/未接続/例外 (c) マーカー・メタの確定の失敗。
    戻り値は `index_world()` の結果（呼ばなかった/例外時は `{"available": False, "error": ...}`）。確定の失敗は `error` に `MARKER_FAILURE_REASONS` の理由を載せて返す（呼び出し元が成功と取り違えない）。
    `run_id`（`sync` の unchanged 分岐専用）を指定すると失敗しても新規 `ingest_runs` 行を作らない（呼び出し元が戻り値から受付 run を terminal 化する）。
    `progress` は `es_index.index_world()` へ転送する（`(done_docs, total_docs)`）。
    `worker._refresh_derived_representations`・`worker.sync`（ES 自己修復分岐）・`worker._reindex_after_rag_rewrite` が共用する（全再構築の `_run_locked` は含めない）。
    """
    world_dir = worlds.world_dir(world)
    derived_md_dir = worlds.derived_md_dir(world)
    tracked = bool(world_dir and derived_md_dir.exists())
    if tracked and not office_md.drop_human_md_es_sig_marker(derived_md_dir):
        _record_es_index_failure(world, "human_md_es_sig_marker_drop_failed", run_id=run_id)
        return {"available": False, "error": "human_md_es_sig_marker_drop_failed"}
    try:
        esr = es_index.index_world(world, content_sig=content_sig, settings=settings, progress=progress, row=row)
    except background.StopRequested:                        # 停止要求は失敗として記録せず呼び出し元へ通す（マーカーは無効化したまま＝次回作り直す）
        raise
    except Exception as e:
        _log.warning(
            "ES 再索引で例外が発生しました（次回 sync で再試行）: world=%s", world, exc_info=True)
        esr = {"available": False, "error": e.__class__.__name__}
    ok = esr.get("available") is True and not esr.get("error")
    failure_reason = None
    if not ok:
        failure_reason = esr.get("error") or "unavailable"
    else:
        marker_failure = confirm_es_markers(world)
        if marker_failure is not None:
            failure_reason = MARKER_FAILURE_REASONS[marker_failure]
            esr = {**esr, "error": failure_reason}
    if failure_reason is not None:
        _record_es_index_failure(world, failure_reason, run_id=run_id)
    return esr


def _record_es_index_failure(world: str, reason: str, *, run_id: int | None = None) -> None:
    """ES 反映失敗を記録する。`run_id` 指定時は何もしない（呼び出し元が戻り値から受付 run 自身を terminal 化する）。"""
    if run_id is not None:
        return
    try:
        store.add_ingest_run(
            world, status="failed",
            extraction_snapshot={"stage": "es_index", "error": reason,
                                 # 接続できないだけ（unavailable）は失敗の理由ではなく未接続として残す
                                 "es": {"available": False,
                                        "error": None if reason == "unavailable" else reason}},
            created_by="admin")
    except Exception:
        _log.warning(
            "ES 反映失敗の ingest_runs 記録に失敗しました: world=%s", world, exc_info=True)


def with_marker_failure(reason: str | None, esr: dict) -> str | None:
    """ES の結果が印・メタの確定の失敗なら、その理由を失敗の理由 `reason`（`;` 区切り）へ足して返す。"""
    marker = esr.get("error")
    if marker not in MARKER_FAILURE_REASONS.values():
        return reason
    return f"{reason};{marker}" if reason else marker


def settle_marker_failure(outcome: str | None, reason: str | None, esr: dict) -> tuple[str | None, str | None]:
    """後の ES の作り直しが成功したとき、`with_marker_failure` で足した理由を消す。ほかの理由が残らなければ `rag_failed` を `handled` に戻す。"""
    if esr.get("available") is not True or esr.get("error") or not reason:
        return outcome, reason
    kept = [r for r in reason.split(";") if r not in MARKER_FAILURE_REASONS.values()]
    if len(kept) == len(reason.split(";")):
        return outcome, reason
    if kept:
        return outcome, ";".join(kept)
    return ("handled" if outcome == "rag_failed" else outcome), None
