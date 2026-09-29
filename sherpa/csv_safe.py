"""CSV のセルを表計算ソフトの数式として解釈させない（CSV インジェクション対策）。"""
from __future__ import annotations

# 先頭がこれらの文字（半角/全角の = + - @・タブ・CR・LF）のセルは、スプレッドシートアプリが
# 数式として解釈しうる（OWASP WSTG 準拠）。先頭の半角空白は無視して判定する（空白の後に = 等が
# 来ても検知する）。先頭に `'`（テキスト強制の慣用記法）を前置して無害化する。
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
