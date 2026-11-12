"""期の名前の読み方（番号の取り出し）。"""
from __future__ import annotations

import re

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def period_number(name: str) -> int | None:
    """期の名前の最初の数字の並び（全角は半角に直す）。漢数字は取らない。無ければ None。"""
    m = re.search(r"[0-9]+", name.translate(_FULLWIDTH_DIGITS))
    return int(m.group()) if m else None


def ordering(names: list[str]) -> list[str] | None:
    """名前の全部から重ならない番号が取れ、2 つ以上あるときだけ、番号の小さい順（古い順）を返す。"""
    nums = {n: period_number(n) for n in names}
    if len(names) < 2 or any(v is None for v in nums.values()) or len(set(nums.values())) != len(nums):
        return None
    return sorted(names, key=lambda n: nums[n])
