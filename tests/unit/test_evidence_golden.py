"""`evidence_spike.extract` の出力の回帰試験（部品化の前後で Evidence IR が変わらないことの網）。

`fixtures/` 配下の原本ごとに、IR の SHA-256・件数・取り出した素材を期待値
（`tests/unit/goldens/evidence/golden.json`）と突き合わせる。期待値の作り直しは `scripts/evidence_golden.py --write`。
"""
from __future__ import annotations

import pytest

import _evidence_golden as eg


def test_every_source_has_a_golden():
    expected = eg.read_golden()
    names = eg.sources()
    missing = [n for n in names if n not in expected]
    stale = sorted(n for n in expected if n not in names)
    assert not missing and not stale, f"期待値が無い: {missing} / 原本が無い: {stale}（scripts/evidence_golden.py --write）"


@pytest.mark.parametrize("rel", eg.sources())
def test_evidence_matches_golden(rel):
    expected = eg.read_golden()[rel]
    got = eg.snapshot(rel)
    if got == expected:
        return
    rows = eg.diff_rows(expected, got)
    pytest.fail(f"{rel}: Evidence IR が期待値と違う（sha256 {expected['sha256'][:12]} -> {got['sha256'][:12]}）\n" + "\n".join(rows))
