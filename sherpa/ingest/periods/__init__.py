"""期（改修の世代）の読み方。PJ（登録フォルダの直下のフォルダ）の中の期の置き場・並び順・親の期・表記の揺れを、
ファイル名の一覧（root からの相対パス）と、変更一覧を読む関数から決める。フォルダは歩かない。
外から使ってよい名前はここに出したものだけ。
"""
from __future__ import annotations

from .model import ChangeList, Comparison, NestedSlot, PeriodParent, PjPeriods, Slot
from .names import period_number
from .reader import Periods, read_periods

__all__ = ["ChangeList", "Comparison", "NestedSlot", "PeriodParent", "Periods", "PjPeriods", "Slot",
           "period_number", "read_periods"]
