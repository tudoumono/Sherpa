"""期のある資料フォルダの候補の選び方。参照元が期の中にあるとき、同じ期 → 親の期の連鎖（古い方へ）→ 期の外の順に候補を絞り、
選んだ相手のエッジの確かさ（`confirm`・`assumption`）を決める。期を見分けられなかった資料フォルダでは使わない（`PeriodSelect` を作らない）。

設計: docs/proposals「3.3a 段 3c の設計」・評価の読み方 §9
"""
from __future__ import annotations

from .. import periods

UNDETERMINED = "parent_period_undetermined"   # 親の期が決まらずつながないときの記録（flags）の理由
ASSUME_ORDER = "parent_by_order"              # 連鎖に名前の順で決めた親が入る
ASSUME_VARIANCE = "variance"                  # 表記の揺れで同じ部品と見なした


def top_of(rel: str):
    return rel.split("/", 1)[0] if "/" in rel else None


def tree_distance(a: str, b: str) -> int:
    da, db = a.split("/")[:-1], b.split("/")[:-1]
    c = 0
    for x, y in zip(da, db):
        if x != y:
            break
        c += 1
    return (len(da) - c) + (len(db) - c)


class PeriodSelect:
    """`periods.read_periods` の結果から、参照元ごとの親の期の連鎖と候補の絞り込みを返す。PJ＝トップフォルダ。"""

    def __init__(self, found: "periods.Periods"):
        self._p = found
        self._rows: dict = {}
        for pj, info in found.by_pj.items():
            for row in info.parents:
                self._rows.setdefault((pj, row.period), []).append(row)
        self._variants = {pj: {frozenset(pair) for c in info.comparisons.values() for pair in c.variants}
                          for pj, info in found.by_pj.items()}
        self._chains: dict = {}

    @staticmethod
    def active(found: "periods.Periods") -> bool:
        return any(info.slots for info in found.by_pj.values())

    def period_of(self, rel: str):
        """rel の期の名前。期の外（期の置き場の外・登録フォルダ直下が期の置き場のとき）は None。"""
        name, i = self._p.placement.period_of(rel)
        return name if name is not None and i > 0 else None

    def place_of(self, rel: str):
        name, i = self._p.placement.period_of(rel)
        return "/".join(rel.split("/")[:i]) if name is not None and i > 0 else None

    def _stripped(self, rel: str) -> str:
        return self._p.placement.rel_path(rel) if self.period_of(rel) else rel

    def dist(self, ref_rel: str, rel: str) -> int:
        """木の距離。両方が期の中なら期のフォルダを除いたパスで測る（別の期の定義とも比べられる）。"""
        if self.period_of(ref_rel) and self.period_of(rel):
            return tree_distance(self._stripped(ref_rel), self._stripped(rel))
        return tree_distance(ref_rel, rel)

    def _row(self, pj, period, comps):
        rows = self._rows.get((pj, period), [])
        hit = {(r.parent, r.kind) for r in rows if r.scope and r.scope in comps}
        if not hit:
            hit = {(r.parent, r.kind) for r in rows if r.scope is None} or {(r.parent, r.kind) for r in rows}
        return next(iter(hit)) if len(hit) == 1 else None

    def _chain(self, pj, period, comps: tuple):
        """(親の期の連鎖 [(期, 名前の順の親を含むか)], 終わり方 'oldest'|'undetermined')。"""
        key = (pj, period, comps)
        if key not in self._chains:
            steps, seen, cur, by_order, end = [], {period}, period, False, "undetermined"
            while True:
                row = self._row(pj, cur, comps)
                if row is None or row[1] in ("none", "conflict"):
                    break
                parent, kind = row
                if parent is None:
                    end = "oldest"
                    break
                if parent in seen:
                    break
                seen.add(parent)
                by_order = by_order or kind == "order"
                steps.append((parent, by_order))
                cur = parent
            self._chains[key] = (steps, end)
        return self._chains[key]

    def _comps(self, ref_rel: str) -> tuple:
        return tuple(self._stripped(ref_rel).split("/")[:-1])

    def narrow(self, items, ref_rel: str, key=lambda x: x):
        """参照元と同じトップフォルダの候補 items を、期の規則で絞る。戻りは `(絞った候補, status)`。
        status は '' か `UNDETERMINED`。連鎖のどの期にも・期の外にも無ければ絞った候補は空（呼び出し側は cross_scope と同じに扱う）。"""
        top = top_of(ref_rel)
        same = [x for x in items if top_of(key(x)) == top]
        period = self.period_of(ref_rel)
        if not same or period is None:
            return same, ""
        by: dict = {}
        for x in same:
            by.setdefault(self.period_of(key(x)), []).append(x)
        if period in by:
            return by[period], ""
        steps, end = self._chain(top, period, self._comps(ref_rel))
        for parent, _flag in steps:
            if parent in by:
                return by[parent], ""
        if end == "undetermined":
            return [], UNDETERMINED
        return by.get(None, []), ""

    def assess(self, ref_rel: str, rel: str):
        """選んだ相手 rel のエッジの確かさ。戻りは `(confirm, assumption の並び)`。"""
        period, target = self.period_of(ref_rel), self.period_of(rel)
        if period is None or target is None or target == period:
            return "confirmed", []
        top = top_of(ref_rel)
        steps, _end = self._chain(top, period, self._comps(ref_rel))
        out = []
        if any(p == target and by_order for p, by_order in steps):
            out.append(ASSUME_ORDER)
        if self._crosses_variance(top, ref_rel, rel):
            out.append(ASSUME_VARIANCE)
        return ("conditional" if out else "confirmed"), sorted(out)

    def _crosses_variance(self, pj, ref_rel: str, rel: str) -> bool:
        """参照元と相手のフォルダ名が、同じ PJ の隣どうしの期で同じファイル名の組として見つかった揺れの組（`src`↔`ソース` など）か。"""
        seen = self._variants.get(pj) or set()
        if not seen:
            return False
        a, b = self._stripped(ref_rel).split("/")[:-1], self._stripped(rel).split("/")[:-1]
        for x, y in zip(a, b):
            if x != y and frozenset((x, y)) in seen:
                return True
        return False

    def candidate_paths(self, rels, ref_rel: str) -> list:
        top = top_of(ref_rel)
        return sorted({r for r in rels if top_of(r) == top})[:5]


def scoped(ps, items, ref_rel: str, key=lambda x: x):
    """`PeriodSelect` があれば期の規則で、無ければ今までどおり同じトップフォルダだけに絞る。戻りは `(候補, status)`。"""
    if ps is None:
        top = top_of(ref_rel)
        return [x for x in items if top_of(key(x)) == top], ""
    return ps.narrow(items, ref_rel, key)


def distance_of(ps):
    return tree_distance if ps is None else ps.dist


def mark_period_nodes(b) -> None:
    """期を見分けたときだけ、期の中のファイルのノードに `period`（期の名前）・`period_place`（期の置き場の相対パス）を付ける。"""
    ps = b.period_select
    if ps is None:
        return
    for node in b.nodes.values():
        path = node.get("path")
        period = ps.period_of(path) if path else None
        if period is not None:
            node["period"], node["period_place"] = period, ps.place_of(path)


def stamp_edge(b, edge: dict, ref_rel: str, rel: str) -> None:
    """解決した候補のエッジに確かさ（集約前の内部の欄 `_confirm`・`_assumption`）を付ける。参照元が期の外なら付けない。"""
    stamp_with(b.period_select, edge, ref_rel, rel)


def stamp_with(ps, edge: dict, ref_rel: str, rel: str) -> None:
    """`stamp_edge` の本体。`PeriodSelect`（無ければ None）を直接受ける。"""
    if ps is None or ps.period_of(ref_rel) is None:
        return
    edge["_confirm"], edge["_assumption"] = ps.assess(ref_rel, rel)


def merge_confirm(cands: list) -> dict:
    """同じエッジの候補を 1 本にまとめるときの確かさ。1 つでも条件つきなら条件つき（`assumption` は条件つきの候補の和集合）。
    確かさを持つ候補が無ければ何も付けない。"""
    marked = [e for e in cands if e.get("_confirm")]
    if not marked:
        return {}
    cond = [e for e in marked if e["_confirm"] == "conditional"]
    if not cond:
        return {"confirm": "confirmed"}
    return {"confirm": "conditional", "assumption": sorted({a for e in cond for a in e["_assumption"]})}


def undetermined_candidates(b, f: dict, ref_rel: str, analyzer_name) -> list:
    """親の期が決まらずつながなかった参照の、同じトップフォルダにある候補のパス（昇順・5 件まで）。"""
    kind, name = f["kind"], f["name"]
    simple = getattr(name, "simple", name)
    rels = {r for r in b.defs.get((kind, name), [])}
    rels |= {r for r, _n in b.qualified_defs.get((kind, name), [])}
    rels |= {c[0] for c in b.simple_name_defs.get((analyzer_name, kind, name), [])}
    rels |= {c[0] for c in b.table_defs.get(simple, [])} if kind == "Table" else set()
    rels |= set(b.path_suffix_index.get((kind, top_of(ref_rel), name), []))
    rels |= {c[0] for (k, _kk, n), cs in b.config_key_index.items() if (k, n) == (kind, name) for c in cs}
    return b.period_select.candidate_paths(rels, ref_rel)
