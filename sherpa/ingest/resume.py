"""取り込みワーカーの起動時の自動再開。

停止要求で区切りに止まった run と、強制終了で中断へ格下げされた run のうち、資料フォルダの最新の run が
中断のままのものについて、同期（refresh）の依頼を 1 回だけ積む（実行はそのままワーカーが拾う）。済んだファイルは変換キャッシュから取れる。
ディスクの空きが少ないため止めた run は再開しない（利用者が空きを作って「更新」を押すまで待つ）。
設計: docs/design/operations.md「起動・停止・状態」
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .. import store, worlds
from ..env_int import env_int
from . import background

_log = logging.getLogger("sherpa")

# 再開してよい操作。削除・参照先の変更・1 件の再変換などは「同期のやり直し」が正しい続きではないので対象外。
RESUMABLE_OPS = frozenset({"register", "sync", "refresh", "rerun"})


def enabled() -> bool:
    return env_int("SHERPA_RESUME_INTERRUPTED", 1, 0, 1) == 1


def _start_sync(world_id: str) -> bool:
    from . import worker

    _run_id, joined = background.submit(
        world_id, "refresh", "{}",
        progress={"stage": "accepted", "stage_label": worker.STAGE_LABELS["accepted"],
                  "done": None, "total": None, "updated_at": datetime.now(timezone.utc).isoformat()})
    return not joined


def resume_interrupted() -> list[str]:
    """再開を始めた資料フォルダの一覧を返す。無効化（`SHERPA_RESUME_INTERRUPTED=0`）・対象なしは空。失敗は起動を止めない。"""
    if not enabled():
        return []
    started: list[str] = []
    try:
        candidates = store.list_worlds_with_interrupted_latest_run()
    except Exception as e:
        _log.warning("中断した取り込みの検索に失敗しました（自動再開は行いません）: %s", e.__class__.__name__)
        return []
    for c in candidates:
        wid = c["world_id"]
        op = c.get("op")
        if op is None:
            # 操作名を記録しない古い版（0.18.1 以前）の中断 run。資料フォルダの参照先が解決できるときだけ再開する
            try:
                if not worlds.world_dir(wid):
                    continue
            except Exception:
                continue
        elif op not in RESUMABLE_OPS:
            continue
        if c.get("stop_reason") == background.LOW_DISK_STOP_REASON:
            _log.warning("ディスクの空きが少ないため止めた取り込みは自動では再開しません。空きを作ってから「更新」を押してください: world=%s", wid)
            continue
        try:
            if _start_sync(wid):
                started.append(wid)
                _log.info("中断していた取り込みを自動で再開します: world=%s（中断した run_id=%s）", wid, c.get("run_id"))
        except (background.ConflictError, background.ShuttingDownError):
            continue
        except Exception:
            _log.warning("取り込みの自動再開を始められませんでした: world=%s", wid, exc_info=True)
    return started
