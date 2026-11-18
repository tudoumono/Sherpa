"""期の親の決め方。変更一覧の基準（確認済み）・名前の番号の並びで 1 つ前の期（条件つき）・決まらない。"""
from __future__ import annotations

from .model import PeriodParent


def lists_for(lists, pj: str, period: str, scope):
    """期・適用範囲に当てはまる、同じ PJ の変更一覧。"""
    return [cl for cl in lists if cl.period == period and (cl.scope is None or cl.scope == scope) and cl.pj == pj]


def order_parent(slots, pj: str, name: str):
    """変更一覧が無い期の親を、置き場の子の番号の並びで 1 つ前の期とする。(親, 置き場のパスの並び)。最も古い期は親が None。
    並び順が決まらない・置き場どうしで食い違うときは None。"""
    mine = [s for s in slots if s.path and s.pj == pj and len(s.children) >= 2 and name in s.children]
    if not mine or any(s.order is None for s in mine):
        return None
    parents = {(s.order[s.order.index(name) - 1] if s.order.index(name) > 0 else None) for s in mine}
    if len(parents) != 1:
        return None
    return parents.pop(), sorted(s.path for s in mine)


def parent_kind(slots, lists, pj: str, period: str, scope):
    """(親の期, 区分)。親を古い方へたどり、途中で変更一覧の基準が食い違う・循環するなら conflict。"""
    cur, seen, first, conflict = period, {period}, None, False
    while True:
        ls = lists_for(lists, pj, cur, scope)
        bases = sorted({cl.basis for cl in ls})
        if not bases:
            op = order_parent(slots, pj, cur)
            if op is None:
                break
            step = (op[0], "order" if op[0] is not None else "oldest")
            first = first or step
            if op[0] is None or op[0] in seen:
                break
            seen.add(op[0])
            cur = op[0]
            continue
        if len(bases) > 1:
            conflict = True
            break
        first = first or (bases[0], "list")
        if bases[0] in seen:
            conflict = True
            break
        seen.add(bases[0])
        cur = bases[0]
    if conflict:
        return None, "conflict"
    return first if first else (None, "none")


def parent_rows(tree, slots, outer_slots, lists, pj: str) -> list:
    """PJ の外側の置き場の期ごと・適用範囲ごとの親（PeriodParent の並び）。"""
    periods = list(dict.fromkeys(p for s in outer_slots for p in (s.order or s.children)))
    out = []
    for period in periods:
        ls = [cl for cl in lists if cl.period == period and cl.pj == pj]
        scopes = sorted({cl.scope for cl in ls if cl.scope})
        subdirs = {k for s in outer_slots if period in s.children for k in tree.children(f"{s.path}/{period}")}
        rows = list(scopes)
        if not scopes or any(cl.scope is None for cl in ls) or subdirs - set(scopes):
            rows.append(None)
        for scope in rows:
            parent, kind = parent_kind(slots, lists, pj, period, scope)
            out.append(PeriodParent(period, scope, parent, kind))
    return out
