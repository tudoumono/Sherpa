"""取り込みのメモリの守り。LibreOffice（soffice）1 本のメモリの上限と、空きメモリを見て待つ処理。

- soffice の上限: Linux だけ、子に `RLIMIT_AS` を付ける（macOS は効かないので何もしない）。値は `RUN_DIR/mem_budget.json` の `libreoffice_bytes`（または `libreoffice_mb`）があればそれ、無ければホストの全体のメモリの 15%（3GB〜4GB）。
- 空きメモリ: Linux は `/proc/meminfo` の MemAvailable、macOS は `vm_stat`。下限（全体の 1 割か 1.5GB の大きい方）を下回る間は、先読みの子と旧形式の先変換が短く待つ。
設計: docs/design/rag.md「アーム一覧」
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from typing import Callable

from . import substage

_log = logging.getLogger("sherpa.ingest.convert")

_GB = 1024 ** 3
_MB = 1024 ** 2
_LIMIT_FRACTION = 0.15
_LIMIT_MIN = 3 * _GB
_LIMIT_MAX = 4 * _GB
_BUDGET_MIN = 2 * _GB
_FREE_FRACTION = 0.10
_FREE_MIN = int(1.5 * _GB)
_POLL_SEC = 2.0
_WAIT_MAX_SEC = 120.0
_BUDGET_FILE = "mem_budget.json"

# soffice がメモリ不足で落ちたときに stderr に出る語
_MEMORY_MARKERS = (b"bad_alloc", b"MemoryError", b"Cannot allocate memory", b"out of memory", b"Out of memory")
# メモリの上限（RLIMIT_AS）超えで落ちたときの終了コード（SIGABRT・SIGKILL。シェル経由は 128+n）
_MEMORY_EXIT_CODES = (-6, -9, 134, 137)


def total_bytes() -> int | None:
    """ホストの全体のメモリ。取れなければ None。"""
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None


def available_bytes() -> int | None:
    """OS が新しいプロセスに渡せる空きメモリ。Linux は MemAvailable、macOS は vm_stat の空き＋再利用できる分。取れなければ None。"""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="ascii") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
            return None
        if sys.platform == "darwin":
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = re.search(r"page size of (\d+) bytes", out)
            size = int(page.group(1)) if page else 16384
            pages = 0
            for name in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
                m = re.search(rf"{name}:\s+(\d+)", out)
                if m:
                    pages += int(m.group(1))
            return pages * size or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def _budget_bytes() -> int | None:
    """`RUN_DIR/mem_budget.json` の LibreOffice 1 本あたりの割り当て（バイト）。無い・読めない・不正は None。"""
    from .. import log_setup
    try:
        data = json.loads((log_setup.run_dir() / _BUDGET_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key, unit in (("libreoffice_bytes", 1), ("libreoffice_mb", _MB)):
        v = data.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return max(_BUDGET_MIN, int(v * unit))
    return None


def soffice_limit_bytes() -> int | None:
    """soffice 1 本のメモリの上限（バイト）。割り当ての記録があればそれ、無ければ全体の 15%（3GB〜4GB）。全体が分からなければ None（上限なし）。"""
    budget = _budget_bytes()
    if budget is not None:
        return budget
    total = total_bytes()
    if not total:
        return None
    return max(_LIMIT_MIN, min(_LIMIT_MAX, int(total * _LIMIT_FRACTION)))


def limit_supported() -> bool:
    """この OS で `RLIMIT_AS` が効くか。Linux だけ（macOS は設定しても効かない）。"""
    return sys.platform.startswith("linux")


def soffice_preexec() -> Callable[[], None] | None:
    """子の soffice に上限を付ける `preexec_fn`。付けない環境（macOS・全体のメモリ不明）は None。"""
    if not limit_supported():
        return None
    limit = soffice_limit_bytes()
    if not limit:
        return None

    def _apply() -> None:
        import resource
        try:
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (ValueError, OSError):
            pass
    return _apply


def looks_memory_exceeded(returncode: int, stderr: bytes | None) -> bool:
    """上限を付けた soffice の異常終了が、メモリの上限超えによるものか（stderr の語、または SIGABRT・SIGKILL）。"""
    if stderr and any(m in stderr for m in _MEMORY_MARKERS):
        return True
    return returncode in _MEMORY_EXIT_CODES


def free_floor_bytes() -> int | None:
    """空きメモリの下限（全体の 1 割か 1.5GB の大きい方）。全体が分からなければ None。"""
    total = total_bytes()
    return max(int(total * _FREE_FRACTION), _FREE_MIN) if total else None


def memory_ok() -> bool:
    """空きメモリが下限以上か。測れないときは True（待たない）。"""
    floor, avail = free_floor_bytes(), available_bytes()
    return floor is None or avail is None or avail >= floor


def wait_for_free_memory(what: str) -> bool:
    """空きメモリが下限を下回っている間、2 秒ごとに見て待つ（最大 2 分）。上回った（または待たなくてよい）なら True、最大まで待っても空かなければ False。"""
    from . import background
    if memory_ok():
        return True
    floor = free_floor_bytes() or 0
    step = substage.begin("mem_wait", f"メモリの空きを待っています（{what}）")
    _log.warning("メモリの空きが少ないため待ちます（空き %d MB・下限 %d MB）: %s",
                 (available_bytes() or 0) // _MB, floor // _MB, what)
    deadline = time.monotonic() + _WAIT_MAX_SEC
    ok = False
    try:
        while True:
            time.sleep(_POLL_SEC)
            if memory_ok() or background.stop_requested():
                ok = True
                break
            if time.monotonic() >= deadline:
                break
    finally:
        step.end()
    return ok
