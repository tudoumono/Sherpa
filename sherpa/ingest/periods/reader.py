"""rels と read_text から期の読み取り結果（Periods）を組み立てる。"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .model import NestedSlot, PjPeriods, Slot
from .names import ordering
from .order import parent_rows
from .places import FolderTree, Placement, layout_containers, recorded_containers
from .records import assign_scopes, keep_matching, read_change_lists
from .variance import compare_neighbours

ReadText = Callable[[str], "str | None"]


@dataclass
class Periods:
    """期の読み取りの結果。slots は期の置き場（root 自身を含む）、by_pj は PJ ごとの整理、tree・placement は点検が使う。"""

    tree: FolderTree
    placement: Placement
    pjs: list
    slots: dict
    by_pj: dict
    change_lists: list
    ignored_lists: list
    unparsed_lists: list
    unreadable: list
    period_names: set
    period_files: dict
    outside_files: list

    def period_of(self, rel: str):
        return self.placement.period_of(rel)

    def rel_path(self, rel: str) -> str:
        return self.placement.rel_path(rel)


def _make_slots(tree: FolderTree, recorded: set, layout: dict) -> dict:
    out = {}
    for path in recorded | set(layout):
        kids = tree.children(path)
        out[path] = Slot(path, path.split("/")[0] if path else None, kids, ordering(kids), path in recorded, path in layout)
    return out


def _by_pj(tree: FolderTree, placement: Placement, slots: dict, lists: list, period_files: dict) -> dict:
    out = {}
    for pj in tree.children(""):
        mine = sorted((s for s in slots.values() if s.path and s.pj == pj), key=lambda s: s.path)
        outer = [s for s in mine if not placement.outer_of(s.path)]
        info = PjPeriods(pj, slots=outer)
        for s in mine:
            o = placement.outer_of(s.path)
            if o:
                info.nested.append(NestedSlot(s, o[0], o[1]))
        info.parents = parent_rows(tree, list(slots.values()), outer, lists, pj)
        info.comparisons = {s.path: compare_neighbours(s.order, period_files.get(s.path, {})) for s in outer if s.order}
        out[pj] = info
    return out


def read_periods(rels: Iterable[str], read_text: ReadText) -> Periods:
    """① フォルダの木を作る ② 変更一覧を読み、期の名前を集める ③ 期の置き場を決める ④ 一覧を期のフォルダ・PJ・適用範囲に当てる
    ⑤ PJ ごとに置き場・親の期・隣どうしの比べ方を整理する。read_text は読めなければ None を返す。"""
    tree = FolderTree(rels)
    lists, unparsed, unreadable = read_change_lists(tree.files, read_text)
    names = {n for cl in lists for n in (cl.period, cl.basis)}
    recorded = recorded_containers(tree, names)
    layout = layout_containers(tree, recorded)
    slots = _make_slots(tree, recorded, layout)
    placement = Placement(slots)
    lists, ignored = keep_matching(lists, placement)
    for cl in lists:
        p, i = placement.period_of(cl.path)
        slot = slots.get("/".join(cl.path.split("/")[:i])) if p is not None else None
        cl.pj = slot.pj if slot else None
    assign_scopes(lists, placement)
    period_files, outside = placement.split_files(tree.files)
    return Periods(tree, placement, list(tree.children("")), slots, _by_pj(tree, placement, slots, lists, period_files),
                   lists, ignored, unparsed, unreadable, names, period_files, outside)
