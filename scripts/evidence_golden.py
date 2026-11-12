#!/usr/bin/env python3
"""`evidence_spike.extract` の出力の期待値（ゴールデン）を作り直す道具。

    scripts/evidence_golden.py --write      # tests/unit/goldens/evidence/golden.json を作り直す

試験: tests/unit/test_evidence_golden.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "unit"))
sys.path.insert(0, str(ROOT))
import _evidence_golden as eg  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", required=True)
    ap.parse_args()
    data = {}
    for rel in eg.sources():
        data[rel] = eg.snapshot(rel)
        print(f"{rel}: elements={data[rel]['elements']} sha256={data[rel]['sha256'][:12]}")
    eg.write_golden(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
