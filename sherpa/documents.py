"""根拠文書の解決。evidence の `doc`（資料フォルダ root 相対の rel_path）を原本 Path に解決する。
同名でも別パスを取り違えない（root 配下への直接解決・symlink／`..`／絶対パスは拒否）。
設計: docs/design/scope.md「同一性＝パス」
"""
from __future__ import annotations

import os
import time

from . import scope_infer, worlds
from .ingest import importance, text_kind
from .ingest.world_graph import resolve_path, valid_rel_parts


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


def _listed_by_walk(root, rel: str) -> bool:
    """`scope_infer.safe_files(root)` が列挙するはずの rel か（木を歩かず、rel の各階層を直接 lstat して判定する）。
    歩き方と同じ除外（版管理フォルダ配下・ノイズファイル・symlink・root 外）を適用する。"""
    parts = valid_rel_parts(rel)
    if parts is None or any(scope_infer.is_vcs_dir_name(p) for p in parts[:-1]):
        return False
    name = parts[-1]
    if text_kind.is_noise(name, os.path.splitext(name)[1].lower()):
        return False
    return resolve_path(root, rel) is not None


def existing_rels(world: str | None, rels, *, deadline: float | None = None) -> set:
    """`rels` のうち資料フォルダ（とアーカイブ展開先）に実在するものの集合。`world_rel_set(world) & rels` と同じ結果を、木を全部歩かずに返す。
    rel の形が不正で直接判定できないもの（`\\` を含む実ファイル名など）がある場合だけ、全走査の `world_rel_set` で確かめる。
    """
    if deadline is not None and time.monotonic() > deadline:
        raise scope_infer.ScopeWalkDeadlineExceeded("scope 走査がデッドラインを超えました")
    rels = list(rels)
    if not rels:
        return set()
    world = world or worlds.default_world()
    wd = worlds.world_dir(world)
    if not wd:
        return set()
    arch = worlds.archives_dir(world)
    found, undecidable = set(), False
    for rel in {r for r in rels if isinstance(r, str) and r}:
        if importance.is_importance_control_path(rel):
            continue
        if valid_rel_parts(rel) is None:
            undecidable = True
        elif _listed_by_walk(wd, rel) or _listed_by_walk(arch, rel):
            found.add(rel)
    if undecidable:
        full = world_rel_set(world, deadline=deadline)
        found |= {r for r in rels if isinstance(r, str) and r in full}
    return found
