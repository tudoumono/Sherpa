"""資料フォルダの有効グラフ（骨格＋言及エッジ）を `(nodes, edges, flags)` で返す単一入口。

worker（Neo4j 反映）と preview_service（画面/件数）が共用する。
設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

from .. import worlds
from . import world_graph


def build_effective_world(world: str, *, files=None):
    """資料フォルダの `(nodes, edges, flags)` を返す。未解決は blocked flag。

    `files`: 列挙済みのファイル一覧があれば渡す（`world_graph.build_world` へそのまま転送）。
    """
    wd = worlds.world_dir(world)
    if not wd:
        return [], [], [{"doc": None, "reason": "world_unresolved", "action": "blocked"}]
    return world_graph.build_world(wd, world, files=files)
