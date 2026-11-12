"""点検の決まり 3（期をまたいで同じ部品は同じ場所）と決まり 4（名前で前後が分かる）。"""
from __future__ import annotations

from sherpa.ingest.periods import period_number

from .model import BAD, GOOD, WARN, Report, kanji_note, names_text


def rule3(rep: Report, periods) -> None:
    """並び順の決まる置き場で、並びの隣どうしの期を比べる（同じ相対パス・表記の揺れ・別の部品）。"""
    r = "決まり3"
    for pj in periods.by_pj.values():
        for s in pj.slots:
            cmp = pj.comparisons.get(s.path)
            if cmp is None:
                continue
            if cmp.same:
                rep.add(r, GOOD, s.path, f"並びの隣どうしの期で、期を除いたフォルダの中の場所が同じファイルが {cmp.same} 個ある",
                        "同じ場所にある同じ名前のファイルは、同じ部品の別の期として結び、確かめて答える。", count=cmp.same)
            if cmp.empty_pairs:
                rep.add(r, WARN, s.path, f"並びの隣どうしの期 {len(cmp.empty_pairs)} 組で、期を除いたフォルダの中の場所が同じファイルが 1 個も無い",
                        "期どうしで同じ場所の部品が無く、前の期へたどれない。同じ部品でも、前の期の定義を示す答えは「確かめられない」になる。",
                        "同じ部品は、期をまたいでフォルダの中の同じ場所に置く。期ごとに別の部品しか無いなら問題ない。",
                        count=len(cmp.empty_pairs), examples=cmp.empty_pairs, pairs=cmp.empty_pairs)
            for pair, lines in cmp.variants.items():
                rep.add(r, WARN, s.path, f"フォルダ名が「{pair[0]}」と「{pair[1]}」の違いだけで、同じ名前のファイルが隣の期にある（{len(lines)} 個）",
                        "同じ部品の別の期として結ぶが、答えは仮定つき（「" + f"{pair[0]}と{pair[1]}は同じ場所」という仮定）になる。",
                        f"期をまたいで「{pair[0]}」か「{pair[1]}」のどちらかに名前を揃えると、仮定なしで確かめて答えられる。",
                        count=len(lines), examples=lines, names=list(pair))
            if cmp.others:
                rep.add(r, BAD, s.path, f"隣の期に同じ名前のファイルが 1 つずつあるが、期を除いたフォルダの中の場所が違う（{len(cmp.others)} 個）",
                        "別の部品として扱い、前の期へたどらない。「前の期の同じ部品」を辿る答えは確かめられない。",
                        "同じ部品なら、期をまたいでフォルダの中の同じ場所に置く。別の部品ならそのままでよい。",
                        count=len(cmp.others), examples=cmp.others)


def _unordered(rep: Report, s) -> None:
    kids = s.children
    nums = {k: period_number(k) for k in kids}
    plain = [k for k in kids if nums[k] is None]
    dup = sorted({k for k in kids if nums[k] is not None and list(nums.values()).count(nums[k]) > 1})
    if len(kids) < 2:
        why = "期のフォルダが 1 つだけで、並びを比べる相手が無い"
    else:
        why = "；".join(x for x in (f"番号が取れない: {names_text(plain)}" if plain else "", f"番号が重なる: {names_text(dup)}" if dup else "") if x)
    note = kanji_note(plain)
    rep.add("決まり4", WARN, s.path, f"名前の番号では並びが決まらない（{why}）" + (f"。{note}" if note else ""),
            "名前の順で親を決めない。親の期は変更一覧の基準があるときだけ確認済みで答え、無い期は「確かめられない」になる。",
            "全部の期の名前に、重ならない番号を入れる（例: 1期・2期）。変更一覧の基準で親を示す方法もある。",
            count=len(kids), children=kids, unnumbered=plain, duplicated=dup)


_PARENT_TEXT = {
    "list": (GOOD, "変更一覧の基準で親の期が決まる", "確認済みの親として答える。", ""),
    "order": (WARN, "変更一覧が無く、名前の番号の並びで 1 つ前の期を親にする", "名前の順で 1 つ前の期の続き（枝ではない）という仮定つきで答える。",
              "期の直下かサブシステムのフォルダに、1 行目が「<期> の変更一覧（基準: <親の期>）」の変更一覧を置くと、確認済みで答えられる。"),
    "oldest": (WARN, "名前の番号で最も古い期（親なし）", "親なしの期として、仮定つきで答える。ほかの期の続きとは見ない。",
               "親の期がある場合は、変更一覧の基準で示す。本当に最初の期なら問題ない。"),
    "none": (BAD, "変更一覧が無く、名前の並びでも親が決まらない", "親の期を決めない。期をまたぐ参照・影響の答えは「確かめられない」になる。",
             "1 行目が「<期> の変更一覧（基準: <親の期>）」の変更一覧を置くか、全部の期の名前に重ならない番号を入れる。"),
    "conflict": (BAD, "変更一覧の基準が食い違っている（または循環している）", "親の期を決めない。期をまたぐ答えは「確かめられない」になる。",
                 "同じ期・同じ範囲の変更一覧の基準を 1 つにそろえる。"),
}


def _parents(rep: Report, pj_name: str, rows: list) -> None:
    for kind in ("conflict", "none", "order", "oldest", "list"):
        mine = [x for x in rows if x.kind == kind]
        if not mine:
            continue
        result, what, treatment, fix = _PARENT_TEXT[kind]
        ex = [(f"{x.period}（{x.scope}）" if x.scope else x.period) + " → "
              + (x.parent if x.parent is not None else "決まらない" if kind in ("none", "conflict") else "親なし") for x in mine]
        rep.add("決まり4", result, pj_name, f"{what}（期と範囲の組 {len(mine)} 件）", treatment, fix, count=len(mine), examples=ex, kind=kind)


def rule4(rep: Report, periods) -> None:
    """置き場ごとの並び順（か決まらない理由）と、期ごとの親の決まり方。"""
    for pj in periods.pjs:
        info = periods.by_pj[pj]
        for s in info.slots:
            if s.order:
                rep.add("決まり4", GOOD, s.path, f"名前の番号で並びが決まる（古い順: {' → '.join(s.order[:10])}{' …' if len(s.order) > 10 else ''}）",
                        "変更一覧が無い期の親は、並びで 1 つ前の期の続きとして仮定つきで答える。", count=len(s.order), order=s.order)
            else:
                _unordered(rep, s)
        _parents(rep, pj, info.parents)
