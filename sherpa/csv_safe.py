"""CSV のセルを表計算ソフトの数式として解釈させない（CSV インジェクション対策）。"""
from __future__ import annotations

# 先頭がこれらの文字のセルは数式として解釈されうる。先頭に `'` を前置して無害化する。
CSV_FORMULA_TRIGGER_PREFIXES = (
    "=", "+", "-", "@", "\t", "\r", "\n",
    "＝", "＋", "－", "＠",   # 全角 = + - @
)


def csv_safe(value):
    if not isinstance(value, str):
        return value
    if value.lstrip(" ").startswith(CSV_FORMULA_TRIGGER_PREFIXES):
        return "'" + value
    return value
