"""env_int（環境変数の整数読み取り）の境界の契約。"""
from __future__ import annotations

import pytest

from sherpa.env_int import env_int

_NAME = "SHERPA_TEST_ENV_INT"


@pytest.mark.parametrize("raw, expected", [
    (None, 30), ("", 30), ("abc", 30), ("1.5", 30),       # 未設定・空・非整数は既定
    ("-1", 30), ("0", 30), ("601", 30),                   # 範囲外は端へ寄せず既定へ（負値でスライス上限が反転しない）
    ("1", 1), ("600", 600), ("60", 60),                   # 境界は含む
])
def test_env_int_boundaries(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(_NAME, raising=False)
    else:
        monkeypatch.setenv(_NAME, raw)
    assert env_int(_NAME, 30, 1, 600) == expected


def test_env_int_clamps_default_into_range(monkeypatch):
    hi = 64 * 1024 * 1024
    for raw in (None, "-1", "abc"):
        if raw is None:
            monkeypatch.delenv(_NAME, raising=False)
        else:
            monkeypatch.setenv(_NAME, raw)
        assert env_int(_NAME, hi * 2, 4096, hi) == hi
        assert env_int(_NAME, 1, 4096, hi) == 4096
