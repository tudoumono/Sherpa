"""変更一覧（記録）の読み取り。1 行目の見出し「<期> の変更一覧（基準: <親の期>）」から期と基準を取る。"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from .model import ChangeList

LIST_SUFFIXES = frozenset({".csv", ".tsv", ".md", ".txt"})
_HEADER_RE = re.compile(r"^\s*#?\s*(?P<p>.+?)\s*の変更一覧\s*[（(]\s*基準\s*[:：]\s*(?P<b>[^）)]+?)\s*[）)]")
_HEADER_SCAN_LINES = 5
_HEADER_MAX_CHARS = 500


def read_change_lists(rels, read_text):
    """変更一覧の候補（名前に「変更一覧」を含む csv・tsv・md・txt）を読む。(読めた一覧, 見出しが読めない一覧のパス, 読めなかったパス)。"""
    lists, unparsed, unreadable = [], [], []
    for rel in rels:
        name = rel.rsplit("/", 1)[-1]
        if "変更一覧" not in name or PurePosixPath(name).suffix.lower() not in LIST_SUFFIXES:
            continue
        text = read_text(rel)
        if text is None:
            unreadable.append(rel)
            continue
        head = next(((i, m.group("p").strip(), m.group("b").strip()) for i, ln in enumerate(text.splitlines()[:_HEADER_SCAN_LINES], 1)
                     if (m := _HEADER_RE.match(ln.strip()[:_HEADER_MAX_CHARS]))), None)
        if head is None:
            unparsed.append(rel)
            continue
        parts = rel.split("/")
        lists.append(ChangeList(period=head[1], basis=head[2], scope=parts[-2] if len(parts) >= 2 else None, path=rel, header_line=head[0]))
    return lists, unparsed, unreadable


def _after_period(cl: ChangeList, placement) -> tuple | None:
    """一覧が置かれたフォルダのうち、期のフォルダより下の部分。期のフォルダが無ければ None。"""
    p, i = placement.period_of(cl.path)
    return tuple(cl.path.split("/")[:-1][i + 1:]) if p is not None else None


def assign_scopes(lists, placement) -> None:
    """一覧の適用範囲を、置き場所から上にたどって決める。期のフォルダの直下の一覧は期全体（None）。
    下の階層の一覧は、同じ長さで 1 か所だけ違う置き場所の一覧がある階層をサブシステムのフォルダとする。決まらなければ置かれたフォルダ名。"""
    afters = {cl.path: _after_period(cl, placement) for cl in lists}
    levels: dict = {}
    uniq = sorted({a for a in afters.values() if a})
    for k, a in enumerate(uniq):
        for b in uniq[k + 1:]:
            diff = [j for j, (x, y) in enumerate(zip(a, b)) if x != y] if len(a) == len(b) else []
            if len(diff) == 1:
                levels.setdefault(len(a), set()).add(diff[0])
    for cl in lists:
        after = afters[cl.path]
        if after is None:
            continue
        if not after:
            cl.scope = None
        elif len(levels.get(len(after), ())) == 1:
            cl.scope = after[next(iter(levels[len(after)]))]
        else:
            cl.scope = after[-1]


def keep_matching(lists, placement):
    """置かれた期のフォルダと見出しの期が違う一覧を外す。(残した一覧, [(パス, 理由)])。期のフォルダ直下の置き場名は適用範囲にしない。"""
    kept, ignored = [], []
    for cl in lists:
        pp = placement.period_of(cl.path)[0]
        if pp is not None and pp != cl.period:
            ignored.append((cl.path, f"一覧の見出しの期 {cl.period} と置かれた期のフォルダ {pp} が違う"))
            continue
        if cl.scope == cl.period:
            cl.scope = None
        kept.append(cl)
    return kept, ignored

