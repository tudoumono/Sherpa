"""根拠文書の解決。evidence の `doc`（資料フォルダ root 相対の rel_path）を原本 Path に解決する。
同名でも別パスを取り違えない（root 配下への直接解決・symlink／`..`／絶対パスは拒否）。
設計: docs/design/scope.md「同一性＝パス」
"""
from __future__ import annotations

from . import scope_infer, worlds
from .ingest import importance
from .ingest.world_graph import resolve_path


def resolve(rel: str, world: str | None = None):
    """rel_path（資料フォルダ root 相対）→ 原本 Path（無ければ None）。
    `world` 省略時は既定の資料フォルダ（`worlds.default_world()`）。トラバーサル／絶対／未実在／重要度設定ファイル自体は None。
    filesystem のみで完結する（DB に依存しない）。アーカイブ配下の rel は展開先（`worlds.archives_dir`）の写しを返す。
    """
    if importance.is_importance_control_path(rel):
        return None
    world = world or worlds.default_world()
    wd = worlds.world_dir(world)
    if not wd:
        return None
    found = resolve_path(wd, rel)
    if found is not None:
        return found
    return resolve_path(worlds.archives_dir(world), rel)


def world_rel_set(world: str | None = None, root=None, strict: bool = False, *,
                  deadline: float | None = None) -> set:
    """資料フォルダ内の実在 rel_path 集合（`safe_files` を 1 回だけ走査する batch 版）。資料フォルダ未解決は空集合。
    `root`: 解決済みの root を渡すと `worlds.world_dir()` を呼び直さない。`strict`／`deadline` は `scope_infer.safe_files` へそのまま渡す。
    """
    wd = root
    if wd is None:
        world = world or worlds.default_world()
        wd = worlds.world_dir(world)
    if not wd:
        return set()
    also = worlds.archives_dir(world) if world is not None else None
    return {r for _rp, r in scope_infer.safe_files(wd, strict=strict, deadline=deadline, also=also)
           if not importance.is_importance_control_path(r)}
