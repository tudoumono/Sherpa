"""点検の報告を表すデータ（項目・結果の 3 段・決まりの一覧）と、報告の文面の部品。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sherpa.ingest.periods import period_number

GOOD, WARN, BAD = "合う", "注意", "合わない"
RESULTS = (GOOD, WARN, BAD)
RULES = (
    ("決まり1", "システムのフォルダは登録したフォルダの直下のフォルダ"),
    ("決まり2", "期のフォルダが同じ高さに並ぶ"),
    ("決まり3", "期をまたいで同じ部品は同じ場所にある"),
    ("決まり4", "名前で前後が分かる"),
    ("今ある記録", "（変更一覧）"),
)
EXAMPLE_LIMIT = 5
NOT_COUNTED = ("ビルドの設定（pom・Gradle・pyproject・requirements・package.json・csproj・sln）の件数と、読めない書き方の Gradle は、この点検では数えない。",)
_KANJI_NUMBER_RE = re.compile(r"第?[〇一二三四五六七八九十百千]+(?:期|次|年度|年|版|回|弾|段階)?")
KANJI_NOTE = "漢数字は番号として読まない（漢数字だけの名前: {names}）"


@dataclass
class Item:
    """点検の項目 1 つ。結果が「合う」以外には、Sherpa の扱い（treatment）と直し方（fix）が要る。"""

    rule: str
    result: str
    path: str
    what: str
    treatment: str
    fix: str = ""
    count: int | None = None
    examples: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class Report:
    """点検の報告。items は項目、not_counted は数えなかったものの断り書き。"""

    items: list = field(default_factory=list)
    not_counted: tuple = NOT_COUNTED

    def add(self, rule, result, path, what, treatment, fix="", count=None, examples=(), **extra):
        if result != GOOD and not (treatment and fix):
            raise ValueError(f"{rule} {path}: 合う以外には Sherpa の扱いと直し方が要る")
        self.items.append(Item(rule, result, path, what, treatment, fix, count, list(examples)[:EXAMPLE_LIMIT], extra))

    def pick(self, rule, result, path=None) -> list:
        return [it for it in self.items if it.rule == rule and it.result == result and (path is None or it.path == path)]


def where(path: str) -> str:
    return {"": "（全体）", ".": "（登録したフォルダの直下）"}.get(path, path)


def label(name: str, title: str) -> str:
    return name + ("" if not title or title.startswith("（") else " ") + title


def kanji_names(names) -> list:
    """番号が取れず、漢数字だけで番号を書いている名前。"""
    return [n for n in names if period_number(n) is None and _KANJI_NUMBER_RE.fullmatch(n)]


def kanji_note(names) -> str:
    k = kanji_names(names)
    return KANJI_NOTE.format(names=names_text(k)) if k else ""


def names_text(names, limit: int = 8) -> str:
    names = list(names)
    return "・".join(names[:limit]) + (f" ほか {len(names) - limit} 個" if len(names) > limit else "")
