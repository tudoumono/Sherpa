"""world の「有効グラフ」構築の単一入口（鏡モデル）。

worker（Neo4j 反映）と preview_service（画面/件数）が、world 解決→`world_graph.build_world()` を
それぞれ重複実装していたのを集約（rv-full (1)#3・(2)）。骨格（Pass1/Pass2）＋言及エッジ（Pass3）を
込みで `(nodes, edges, flags)` を返す（S3・K9-K11＝意味層フル抽出・REALIZES 橋は撤去済み）。
"""
from __future__ import annotations

from .. import worlds
from . import world_graph


def build_effective_world(world: str, *, files=None):
    """world の `(nodes, edges, flags)`（骨格＋言及エッジ込み）。world 未解決は blocked flag。

    `files`（省略可・キーワード専用）: 呼び出し側が既に `scope_infer.safe_files(wd)` を1回
    materialize 済みなら渡す（`world_graph.build_world` へそのまま転送・S工事③是正）。省略時は
    従来どおり `build_world` 内で直接歩く（`worker.py` の既存呼び出しは無変更）。
    """
    wd = worlds.world_dir(world)
    if not wd:
        return [], [], [{"doc": None, "reason": "world_unresolved", "action": "blocked"}]
    return world_graph.build_world(wd, world, files=files)
