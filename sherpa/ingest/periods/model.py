"""期の読み取りの結果を表すデータ。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChangeList:
    """読めた変更一覧 1 つ分（1 行目の見出しの期・基準と、置かれた場所から決めた適用範囲）。"""

    period: str
    basis: str
    scope: str | None
    path: str
    header_line: int
    pj: str | None = None


@dataclass
class Slot:
    """期のフォルダが並ぶ場所。path が "" なら root 自身。recorded＝変更一覧の名前から、layout＝置き方と名前から見分けた。"""

    path: str
    pj: str | None
    children: list
    order: list | None
    recorded: bool
    layout: bool


@dataclass
class NestedSlot:
    """別の置き場の期のフォルダの下にある置き場（期としては使われない）。"""

    slot: Slot
    outer: str
    outer_period: str


@dataclass
class PeriodParent:
    """期（と適用範囲）ごとの親の期。kind は list（変更一覧で確認済み）・order（名前の順で条件つき）・
    oldest（名前の順で最も古い＝親なし）・none（決まらない）・conflict（変更一覧の基準が食い違う）。"""

    period: str
    scope: str | None
    parent: str | None
    kind: str


@dataclass
class Comparison:
    """並びの隣どうしの期の比べ方。same＝期を除いた相対パスが同じファイルの数、empty_pairs＝それが 1 個も無い隣の組、
    variants＝表記の揺れ（フォルダ名の組 → 例）、others＝揺れの組に入らない違い。"""

    same: int = 0
    empty_pairs: list = field(default_factory=list)
    variants: dict = field(default_factory=dict)
    others: list = field(default_factory=list)


@dataclass
class PjPeriods:
    """PJ（root の直下のフォルダ）ごとの期の置き場。slots は外側の置き場だけ、nested は期として使われない内側の置き場。"""

    pj: str
    slots: list = field(default_factory=list)
    nested: list = field(default_factory=list)
    parents: list = field(default_factory=list)
    comparisons: dict = field(default_factory=dict)
