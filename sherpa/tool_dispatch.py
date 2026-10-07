"""道具ディスパッチ。`run_tool(name, args, world, scope_paths, ...)` が読み取り部品 `parts/read/tools.run_tool` へ渡す。

MCP・回答ループ・外部 API が共有する。
設計: docs/design/interfaces.md「MCP の道具」
"""
from __future__ import annotations

from .parts.read import tools as read_tools


def run_tool(name: str, args: dict, world: str, scope_paths,
            deadline: float | None = None, layer=None,
            max_hits: int | None = None, window_cap: int | None = None,
            tool_result_max_bytes: int | None = None,
            graph_only: bool = False) -> tuple[dict, set, list, list]:
    """道具名で振り分けて実行する。戻り値は `(結果, 触れた doc_id 集合, 引用候補, 候補カード)`。"""
    args = args or {}
    return read_tools.run_tool(name, args, world, scope_paths, deadline=deadline, layer=layer,
                               max_hits=max_hits, window_cap=window_cap,
                               tool_result_max_bytes=tool_result_max_bytes, graph_only=graph_only)


def pop_hit_scores() -> list:
    """直前の `es_search` のヒットの点数を取り出す（結果とは別に、呼び出しの記録だけが使う）。"""
    return read_tools.pop_hit_scores()


def pop_hit_ranks() -> list:
    """直前の検索のヒットの元の順位を取り出す（結果とは別に、呼び出しの記録だけが使う）。"""
    return read_tools.pop_hit_ranks()
