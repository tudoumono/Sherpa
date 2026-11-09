"""登録するフォルダが案 B の決まり（PJ の下に期が並ぶ置き方）に合うかの点検。ファイル名の一覧と、変更一覧を読む関数と、
期の読み取りの結果（`sherpa.ingest.periods`）から報告を組む。読み取りだけで、資料フォルダには何も書かない。
外から使ってよい名前はここに出したものだけ。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from sherpa.ingest.periods import Periods, read_periods

from .model import BAD, GOOD, RESULTS, RULES, WARN, Item, Report
from .records import records
from .rules_build import rule5
from .report import json_text, summarize, to_json, to_markdown, write_json, write_markdown
from .rules_near import near_and_outside
from .rules_periods import rule3, rule4
from .rules_pj import rule1, rule2_slots

__all__ = ["BAD", "GOOD", "Item", "RESULTS", "RULES", "Report", "WARN", "inspect_folder", "json_text", "summarize",
           "to_json", "to_markdown", "write_json", "write_markdown"]


def inspect_folder(rels: Iterable[str], read_text: Callable[[str], "str | None"], periods: Periods | None = None,
                   build: dict | None = None) -> Report:
    """決まり 1〜5 と今ある記録の報告を組む。periods（期の読み取りの結果）を渡せば rels・read_text からは読み直さない。
    build（`{"report", "marks_sig"}`）を渡したときだけ決まり 5 を入れる。"""
    periods = periods or read_periods(rels, read_text)
    rep = Report()
    rule1(rep, periods)
    rule2_slots(rep, periods)
    near_and_outside(rep, periods)
    rule3(rep, periods)
    rule4(rep, periods)
    if build is not None:
        rule5(rep, build.get("report"), build.get("marks_sig") or "")
    records(rep, periods)
    return rep
