"""make restart-web は、動いているストアを作り直さない（取り込み中のグラフの書き込みを切らない）。"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_restart_web_asks_start_to_keep_running_stores():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    block = re.search(r"^restart-web:.*?(?=^\S)", makefile, re.S | re.M).group(0)
    assert "SHERPA_KEEP_RUNNING_STORES=1" in block


def test_start_skips_compose_up_and_ocr_update_when_keeping_running_stores():
    start = (ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")
    keep = start.index('SHERPA_KEEP_RUNNING_STORES:-0')
    up = start.index("sherpa_compose up -d", keep)
    assert start.index("else", keep) < up                    # up はストアが動いていないときだけ
    assert '[ "$keep_stores" = "1" ] || ./scripts/ocr-up.sh' in start
