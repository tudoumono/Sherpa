"""点検の決まり 2 のうち、期の置き場の外の観測（近い置き場・置き場の外の同じ部品）。"""
from __future__ import annotations

from collections import defaultdict

from sherpa.ingest.periods import period_number

from .model import BAD, WARN, Report, kanji_note, names_text


class _Skip:
    """期の置き場そのもの・その中や下・その祖先は、近い置き場や置き場の外の同じ部品の候補にしない。"""

    def __init__(self, containers):
        self.cont = set(containers)
        self.above = {"/".join(c.split("/")[:k]) for c in self.cont if c for k in range(len(c.split("/")))}
        self._memo: dict = {}

    def __call__(self, d: str) -> bool:
        if d not in self._memo:
            parts = d.split("/")
            self._memo[d] = (d in self.above or any("/".join(parts[:k]) in self.cont for k in range(1, len(parts) + 1))
                             or "" in self.cont)
        return self._memo[d]


def _shallowest(found) -> list:
    return sorted(d for d in found if not any("/".join(d.split("/")[:k]) in found for k in range(1, len(d.split("/")))))


def _near_candidates(periods, skip) -> dict:
    shared: dict = defaultdict(dict)
    for rel in periods.tree.files:
        parts = rel.split("/")
        for k in range(1, len(parts) - 1):
            d = "/".join(parts[:k])
            if not skip(d):
                shared[d].setdefault("/".join(parts[k + 1:]), set()).add(parts[k])
    near = {d: {t: ks for t, ks in tails.items() if len(ks) >= 2} for d, tails in shared.items()}
    return {d: t for d, t in near.items() if t and len(periods.tree.children(d)) >= 2}


def _report_near(rep: Report, d: str, kids: list, tails: dict) -> None:
    r = "決まり2"
    nums = {k: period_number(k) for k in kids}
    plain = [k for k in kids if nums[k] is None]
    dup = sorted({k for k in kids if nums[k] is not None and list(nums.values()).count(nums[k]) > 1})
    ex = [f"{t}（{names_text(sorted(ks), 4)} にある）" for t, ks in sorted(tails.items())]
    base = f"期のフォルダが並ぶ場所の外で、子のフォルダ {len(kids)} 個（{names_text(kids)}）に同じフォルダの中の場所のファイルが {len(tails)} 組ある"
    note = kanji_note(plain)
    if len(plain) == len(kids):
        rep.add(r, WARN, d, base + "が、どの子の名前にも番号が無く、期かどうかが分からない" + (f"。{note}" if note else ""),
                "期としては読まない。ふつうのフォルダとして扱い、期の親や並びは決めない。",
                "期のフォルダなら、名前に番号を入れると期として読む（例: 1期・2期）。期でなければ問題ない。",
                count=len(tails), examples=ex, children=kids)
        return
    why = []
    if plain:
        why.append(f"番号が取れない子: {names_text(plain)}")
    if dup:
        why.append(f"番号が重なる子: {names_text(dup)}")
    rep.add(r, BAD, d, base + "が、子の名前の番号が揃っていない（" + "。".join(why) + "）" + (f"。{note}" if note else ""),
            "期のフォルダの並びとしては読まない。ふつうのフォルダとして扱うため、同じ部品が複数の子にあるときは、どれかを選ばず「確かめられない」と候補を並べて答えることが増える。",
            "期のフォルダなら、全部の子の名前に重ならない番号を入れる（例: 1期・2期）。期でなければ問題ない。",
            count=len(tails), examples=ex, children=kids, unnumbered=plain, duplicated=dup)


def _outside_hits(periods, skip) -> dict:
    period_tails: dict = {}
    for pj in periods.pjs:
        tails: dict = {}
        for s in periods.by_pj[pj].slots:
            for period in sorted(periods.period_files.get(s.path, {})):
                for lst in periods.period_files[s.path][period].values():
                    for t in lst:
                        tails.setdefault(t, period)
        if tails:
            period_tails[pj] = tails
    hit: dict = defaultdict(list)
    for rel in periods.outside_files:
        parts = rel.split("/")
        tails = period_tails.get(parts[0])
        if not tails or len(parts) < 3:
            continue
        for k in range(1, len(parts) - 1):
            d = "/".join(parts[:k])
            if skip(d):
                continue
            t = "/".join(parts[k:])
            if t in tails:
                hit[d].append((t, tails[t]))
    return hit


def near_and_outside(rep: Report, periods) -> None:
    """置き場の外の、番号の揃わない子を持つ近い置き場と、期と同じ部品を持つのに並びに入っていないフォルダ。"""
    skip = _Skip(periods.slots)
    near = _near_candidates(periods, skip)
    for d in _shallowest(near):
        _report_near(rep, d, periods.tree.children(d), near[d])
    hit = _outside_hits(periods, skip)
    for d in _shallowest(hit):
        ex = [f"{t}（{p} にも同じフォルダの中の場所がある）" for t, p in sorted(hit[d])]
        slot = periods.by_pj[d.split("/")[0]].slots[0].path
        rep.add("決まり2", BAD, d, f"期の並びに入っていないフォルダの下に、期のフォルダの中と同じフォルダの中の場所のファイルが {len(hit[d])} 個ある",
                "期と同じ部品を持つが、期の並びに入っていない。期ではないふつうのフォルダとして扱うため、どの期の姿にも入らず、期をたどる答えにも使わない。",
                f"期の資料なら、期のフォルダが並ぶ場所（{slot}）の下へ、番号の入った名前のフォルダとして置く。期の資料でなければ、そのままでよいが期をまたぐ答えには入らない。",
                count=len(hit[d]), examples=ex)
