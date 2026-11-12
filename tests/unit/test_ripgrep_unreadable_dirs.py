"""`ripgrep_search` は読めずに飛ばしたフォルダの数を `unreadable_dirs` に載せ、調査の台帳はそれを `unreadable` にする。"""
from __future__ import annotations

import os

from sherpa import mcp_coverage_outcome as MCO
from sherpa import tool_dispatch as TD
from test_agentic_search import _isolate_world_kb


def test_ripgrep_reports_unreadable_dirs_and_coverage_marks_unreadable(monkeypatch, tmp_path):
    world = "rg-unreadable-dirs"
    _isolate_world_kb(monkeypatch, tmp_path, world, {"ok.md": "other\n", "locked/in.md": "NEEDLE\n"})
    locked = tmp_path / "kb" / world / "locked"
    locked.chmod(0)
    try:
        res, *_ = TD.run_tool("ripgrep_search", {"query": "NEEDLE"}, world, None)
    finally:
        locked.chmod(0o755)
    if os.geteuid() != 0:  # root は権限 0 のフォルダも開けてしまう
        assert res["unreadable_dirs"] == 1
        assert MCO.coverage_outcome("ripgrep_search", res, False) == "unreadable"
