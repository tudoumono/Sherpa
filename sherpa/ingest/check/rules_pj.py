"""点検の決まり 1（PJ）と決まり 2 のうち、期の置き場の見つかり方。"""
from __future__ import annotations

from .model import BAD, GOOD, WARN, Report, names_text, where


def rule1(rep: Report, periods) -> None:
    """root の直下のフォルダを PJ として並べる。直下のファイルはどの PJ にも入らない。"""
    r = "決まり1"
    root_files = [f for f in periods.tree.files if "/" not in f]
    root_slot = periods.slots.get("")
    if root_slot is not None:
        rep.add(r, WARN, ".", f"登録したフォルダの直下のフォルダ（{names_text(root_slot.children)}）が、期のフォルダとして読まれている",
                "システムのフォルダに分けて読めない。期の並び・親の期・変更一覧をシステムごとに決められず、システムをまたぐ参照は使わない。",
                "システムのフォルダを 1 つ挟み、その下に期のフォルダを置く。期のフォルダをシステムのフォルダとして登録する意図なら問題ない。",
                count=len(root_slot.children), children=root_slot.children)
    elif not periods.pjs:
        rep.add(r, BAD, ".", "登録したフォルダの直下にフォルダが無い",
                "システムのフォルダが無いので、期を読めず、期をまたぐ答えは「確かめられない」になる。",
                "登録したフォルダの直下にシステムごとのフォルダを作り、その下に期のフォルダを置く。", count=0)
    else:
        rep.add(r, GOOD, ".", f"登録したフォルダの直下のフォルダ {len(periods.pjs)} 個をシステムのフォルダとして読んだ（{names_text(periods.pjs, 30)}）",
                "期の並び・親の期・変更一覧はシステムのフォルダの中だけで決める。システムをまたいで同じ名前の期を結び付けない。",
                count=len(periods.pjs), pjs=periods.pjs)
    if root_files:
        rep.add(r, WARN, ".", f"登録したフォルダの直下にファイルが {len(root_files)} 個あり、どのシステムのフォルダにも入らない",
                "どのシステムの期の姿にも入らない。変更一覧もシステムに当てはめず、使わない。",
                "ファイルをシステムのフォルダの中へ移す。システムのフォルダに属さない資料なら問題ない。",
                count=len(root_files), examples=root_files)


def _found_by(slot) -> str:
    if slot.recorded and slot.layout:
        return "変更一覧の期の名前と、同じ名前のフォルダがあること（記録）と、名前の番号と置き方の両方"
    if slot.recorded:
        return "変更一覧の期の名前と、同じ名前のフォルダがあること（記録）"
    return "子のフォルダの名前の番号と、期を除いたフォルダの中の場所が同じファイルがあること（置き方と名前）"


def rule2_slots(rep: Report, periods) -> None:
    """PJ ごとに期の置き場を並べる。入れ子の置き場は合わない。置き場の無い PJ は注意。"""
    r = "決まり2"
    for pj in periods.pjs:
        info = periods.by_pj[pj]
        for s in info.slots:
            rep.add(r, GOOD, s.path, f"期のフォルダが並ぶ場所を見つけた。見分けた根拠: {_found_by(s)}。期のフォルダ {len(s.children)} 個（{names_text(s.children)}）",
                    "ここを期のフォルダが並ぶ場所として読み、子のフォルダを期として扱う。",
                    count=len(s.children), children=s.children,
                    found_by=[k for k, on in (("record", s.recorded), ("layout", s.layout)) if on])
        for n in info.nested:
            s = n.slot
            outer_children = periods.slots[n.outer].children
            rep.add(r, BAD, s.path, f"期のフォルダが並ぶ場所（期のフォルダ {len(s.children)} 個: {names_text(s.children)}）が、別の置き場の期のフォルダ（{where(n.outer)}/{n.outer_period}）の下にある",
                    f"期のフォルダの高さが違う。外側の期（{names_text(outer_children)}）を期として読むため、内側の期（{names_text(s.children)}）は期として使われない。",
                    f"期のフォルダを同じ高さに並べる（例: {n.outer_period} と同じ高さの 1期 などの名前のフォルダにする）。",
                    count=len(s.children), outer=n.outer, outer_period=n.outer_period, children=s.children)
        if not info.slots and not info.nested:
            rep.add(r, WARN, pj, "期のフォルダが並ぶ場所が見つからない",
                    "期を持たないシステムのフォルダとして扱う。期ごとの親・変更一覧・期をまたぐ参照は使わない。",
                    "期を持つシステムなら、期のフォルダを同じ高さに並べ、名前に番号を入れる（例: 1期・2期）。期を持たないシステムなら問題ない。")
