"""点検の決まり 5 のうち、ビルドの定義を読む場所の見分け。取り込みの台帳のパスだけから出し、資料フォルダは歩かない。
設計: docs/design/scope.md「ビルドの定義と実行名」
"""
from __future__ import annotations

import posixpath

from sherpa.ingest.build_link import MARK_READ, is_program_source
from sherpa.ingest.source_groups import in_source_group, source_group_dirs

from .model import GOOD, WARN, Report, where

_R = "決まり5"


def _units(periods) -> list:
    """読む場所を見分ける単位（期を見分けた PJ では期のフォルダ、それ以外は PJ）。"""
    out: list = []
    for pj in periods.pjs:
        slots = periods.by_pj[pj].slots
        if slots:
            out.extend(posixpath.join(s.path, c) if s.path else c for s in slots for c in s.children)
        else:
            out.append(pj)
    return sorted(out)


def _unit_of(rel: str, units: set) -> str | None:
    p = rel
    while "/" in p:
        p = p.rsplit("/", 1)[0]
        if p in units:
            return p
    return None


def _under_read_mark(rel: str, marks: dict) -> bool:
    p = rel
    while True:
        if p in marks:
            return marks[p] == MARK_READ
        if "/" not in p:
            return False
        p = p.rsplit("/", 1)[0]


def build_places(rep: Report, rels, periods, marks: dict) -> None:
    """期（期の無い PJ は PJ）ごとの読む場所と、ソースの組の無い単位・ソースの組の外にあるソースを並べる。"""
    rels = list(rels)
    units = _units(periods)
    unit_set = set(units)
    files: dict = {u: [] for u in units}
    for rel in rels:
        u = _unit_of(rel, unit_set)
        if u is not None:
            files[u].append(rel)
    outside: list = []
    for u in units:
        if not files[u]:
            continue
        groups = source_group_dirs(files[u])
        if groups:
            rep.add(_R, GOOD, u, f"置き方から読むビルドの定義の場所: {len(groups)} 個（{', '.join(g.rsplit('/', 1)[-1] for g in groups[:5])}）",
                    "ソースの組のフォルダの下の、決まった名前のビルドの定義を読む。", count=len(groups), found_by="layout")
        else:
            rep.add(_R, WARN, u, "ソースの組のフォルダ（src・source・sources・ソース・ソースコード）が無い",
                    "ビルドの定義を読む場所が無いので、実行名はビルドの定義からはつながらない。回答のときの近い名前の候補で補う。",
                    "ソースとビルドの定義をソースの組のフォルダに置くか、管理画面で「読む場所にする」を付ける。", count=0)
        outside.extend(r for r in files[u] if is_program_source(r) and not in_source_group(r) and not _under_read_mark(r, marks))
    reads = sorted(p for p, m in marks.items() if m == MARK_READ)
    if reads:
        rep.add(_R, GOOD, ".", f"管理画面で「読む場所にする」を付けた場所: {len(reads)} 個", "その場所のビルドの定義と設定を読む。",
                count=len(reads), examples=[where(p) for p in reads], found_by="marks")
    if outside:
        rep.add(_R, WARN, ".", f"ソースの組のフォルダの外にあるソースが {len(outside)} 個ある",
                "ビルドの定義を読む場所の外なので、この下のビルドの定義は読まない（ソースは材料にならない）。",
                "ソースとビルドの定義をソースの組のフォルダに置くか、管理画面で「読む場所にする」を付ける。",
                count=len(outside), examples=outside)
