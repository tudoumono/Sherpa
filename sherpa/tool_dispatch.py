"""道具ディスパッチ。`run_tool(name, args, world, scope_paths, ...)` が道具名で振り分ける。

`write_output_file` は `output_files`（個人 workspace への書き込み）、それ以外は読み取り部品 `parts/read/tools.run_tool`。
MCP・回答ループ・外部 API が共有する。
設計: docs/design/interfaces.md「MCP の道具」
"""
from __future__ import annotations

from .parts.read import tools as read_tools
from . import output_files


def run_tool(name: str, args: dict, world: str, scope_paths,
            deadline: float | None = None, layer=None,
            max_hits: int | None = None, window_cap: int | None = None,
            tool_result_max_bytes: int | None = None,
            uid: str | None = None, graph_only: bool = False) -> tuple[dict, set, list, list]:
    """道具名で振り分けて実行する。戻り値は `(結果, 触れた doc_id 集合, 引用候補, 候補カード)`。"""
    args = args or {}
    if name == "write_output_file":
        docs: set = set()
        # 個人 workspace の成果物は出典検証の対象外（docs/cites/cards は空）
        return (output_files._run_write_output_file(args, uid), docs, [], [])
    return read_tools.run_tool(name, args, world, scope_paths, deadline=deadline, layer=layer,
                               max_hits=max_hits, window_cap=window_cap,
                               tool_result_max_bytes=tool_result_max_bytes, uid=uid, graph_only=graph_only)
