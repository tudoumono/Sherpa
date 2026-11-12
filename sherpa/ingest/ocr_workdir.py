"""推論の子プロセスの後始末: 親が持つ入力ファイルの置き場と、cgroup の OOM の数え取り。

設計: docs/design/rag.md「OCR（非同期・隔離ワーカー）」
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

CGROUP_MEMORY_EVENTS = Path("/sys/fs/cgroup/memory.events")


def cgroup_oom_kills() -> int | None:
    """cgroup v2 の `memory.events` の `oom_kill` 累積。読めなければ None。"""
    try:
        for line in CGROUP_MEMORY_EVENTS.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(" ")
            if name == "oom_kill":
                return int(value)
    except (OSError, ValueError):
        return None
    return None


def remove_input(image_path: Path) -> None:
    """入力ファイルと、子がその隣に作った縮め後のファイルを消す。"""
    for path in image_path.parent.glob(f"{image_path.stem}.*"):
        path.unlink(missing_ok=True)


def default_work_root() -> Path:
    """親が所有する入力ファイルの置き場。子の強制終了後も親が消せるよう、子には作らせない。"""
    return Path(tempfile.gettempdir()) / "sherpa-ocr-work"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reclaim_work_dirs(root: Path) -> int:
    """`root` 直下の `<pid>` ディレクトリのうち、自分の pid または生きていない pid のものを消す。この領域の外には触れない。"""
    if root.is_symlink() or not root.is_dir():
        return 0
    removed = 0
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_dir() or not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid() or not pid_alive(pid):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed
