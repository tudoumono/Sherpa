"""派生物の安全な差し替え（世代公開）の部品。

公開中の派生ディレクトリを先に全消しせず、別ディレクトリへ作り切ってから改名 2 回で差し替える。
失敗時はステージングを捨てるだけで公開中の内容は無傷。
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from ..store import replace_rows
from ..store.replace_rows import ReplaceRow
from . import derived_numbers

_log = logging.getLogger("sherpa.ingest.convert")

_STAGING_SUFFIX = ".staging"
_RETIRED_SUFFIX = ".retired"


def _recover_interrupted_swap(target: Path) -> None:
    """改名 2 回の間で中断した場合（公開中が無く退避先だけがある）に、退避先を公開中へ戻す。

    - target 有り: 何もしない。
    - target 無し・retired 無し: 初回ビルドのため何もしない。
    - target 無し・retired 有り: retired を target へ rename して復旧する。rename に失敗したら（二重障害）例外を送出する。
      握り潰すと後続の `_publish_staging` が retired（唯一残った旧世代）を削除し、派生物が全消失する。
    """
    retired = target.with_name(target.name + _RETIRED_SUFFIX)
    if not target.exists() and retired.is_dir():
        try:
            retired.rename(target)
        except OSError:
            _log.error(
                "派生物の差し替え中断からの復旧に失敗しました"
                "（retired を保持したまま build を打ち切ります）: %s", target, exc_info=True)
            raise
        _log.warning(
            "派生物の差し替えが中断していたため復旧しました: %s", target)


def _publish_staging(staging: Path, target: Path, *, row: ReplaceRow, item: str) -> None:
    """ステージングを公開中へ差し替える（旧公開分は削除）。同一ファイルシステム内の改名のみ。

    ① ステージングの直下に取り込みの番号と設定の値（`.ingest_no`）を書く ② 改名の前に、退避する旧公開分の名前と `item`（`md`／`rag`／`ir`）の「入れ替えた」を行へ書く
    ③ 退避を消したら「古いものを消した」にする。

    後半の改名（staging→target）が失敗したら、retired→target で即時ロールバックしてから例外を再送出する。
    ロールバック自体が失敗した場合は旧内容が retired に残るため、その旨をログに残す（次回 build の `_recover_interrupted_swap` が復旧を試みる）。
    """
    retired = target.with_name(target.name + _RETIRED_SUFFIX)
    shutil.rmtree(retired, ignore_errors=True)
    derived_numbers.write(staging, row, item)
    target_existed = target.exists()
    replace_rows.before_swap(row, row.world, item, old=[str(retired)] if target_existed else [])
    if target_existed:
        target.rename(retired)          # ここから下の rename までが唯一の窓（ミリ秒）
    try:
        staging.rename(target)
    except OSError:
        if target_existed:
            try:
                retired.rename(target)
            except OSError:
                _log.error(
                    "派生物の差し替えに失敗し、旧公開分の復元にも失敗しました"
                    "（derived root が消失した可能性）: %s", target, exc_info=True)
        raise
    shutil.rmtree(retired, ignore_errors=True)
    replace_rows.after_old_removed(row, row.world, item)



class PublishRollbackError(OSError):
    """1 ファイルの差し替えの途中で失敗し、前の状態へも戻せなかった。"""


def _remove_path(p: Path) -> None:
    """ファイルかディレクトリを消す（無ければ何もしない）。"""
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink(missing_ok=True)


def _move_into(src: Path, dst: Path) -> None:
    """ステージングの新しい出力を公開中の場所へ移す（同じファイルシステムの中の改名）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)


def _proc_rss_gib() -> float | None:
    """自プロセスの RSS（GiB）。/proc/self/status の VmRSS を読む（Linux 専用・読めなければ None）。重い変換の前後でログへ出す。"""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except OSError:
        pass
    return None

_DERIVED_FAIL_WARN_RATIO_DEFAULT = 0.1


def derived_fail_warn_ratio() -> float:
    """作れなかった文書がこの割合（候補に対して）を超えたら、管理画面で知らせる。env `SHERPA_DERIVED_FAIL_WARN_RATIO`（0〜1・既定 0.1）。"""
    raw = os.environ.get("SHERPA_DERIVED_FAIL_WARN_RATIO", "").strip()
    try:
        v = float(raw) if raw else _DERIVED_FAIL_WARN_RATIO_DEFAULT
    except ValueError:
        return _DERIVED_FAIL_WARN_RATIO_DEFAULT
    return v if 0.0 <= v <= 1.0 else _DERIVED_FAIL_WARN_RATIO_DEFAULT
