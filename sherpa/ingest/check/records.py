"""点検の「今ある記録」（変更一覧・読めなかったファイル）。ビルドの設定は数えない。"""
from __future__ import annotations

from .model import BAD, GOOD, WARN, Report


def records(rep: Report, periods) -> None:
    """読んだ変更一覧・使わなかった一覧・見出しが読めない一覧・読めなかったファイルを報告する。"""
    r = "今ある記録"
    lists, ignored, unparsed = periods.change_lists, periods.ignored_lists, periods.unparsed_lists
    if not (lists or ignored or unparsed):
        rep.add(r, GOOD, "", "変更一覧は見つからなかった", "期の親は名前の番号の並びだけで決め、仮定つきの答えになる。削除・改名の宣言は使えない。", count=0)
    if lists:
        ex = [f"{cl.path}（期 {cl.period}・基準 {cl.basis}・適用範囲 {cl.scope or '期全体'}）" for cl in lists]
        rep.add(r, GOOD, "", f"変更一覧を {len(lists)} 個読んだ", "期の親子・削除・改名を、この一覧の宣言から確認済みで答える。適用範囲の外には当てはめない。",
                count=len(lists), examples=ex)
    if ignored:
        rep.add(r, BAD, "", f"使わなかった変更一覧が {len(ignored)} 個ある",
                "この一覧は読まない。期の親子・削除・改名の根拠にしない。", "一覧の 1 行目の期の名前を、置かれた期のフォルダ名に合わせる。",
                count=len(ignored), examples=[f"{p}（{why}）" for p, why in ignored])
    if unparsed:
        rep.add(r, BAD, "", f"1 行目の見出しが読めない変更一覧が {len(unparsed)} 個ある",
                "この一覧は読まない。期の親子・削除・改名の根拠にしない。",
                "1 行目を「<期> の変更一覧（基準: <親の期>）」の形に直す。変更一覧でないファイルなら、名前から「変更一覧」を外す。",
                count=len(unparsed), examples=unparsed)
    bad = sorted(periods.unreadable)
    if bad:
        rep.add(r, WARN, "", f"読めないファイルが {len(bad)} 個ある（大きすぎる、または文字コードが UTF-8・CP932 でない）",
                "このファイルは中身を読まない。定義・変更一覧・ビルドの設定としては数えない。",
                "2MB 以下にし、文字コードを UTF-8 か CP932 にする。資料として要らないファイルなら問題ない。",
                count=len(bad), examples=bad)
