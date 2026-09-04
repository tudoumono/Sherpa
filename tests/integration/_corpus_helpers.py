"""tests/integration 共通ヘルパ（最小 COBOL ソース1本を作る `_mk`・TEST-2 棚卸し）。

5ファイルが同一実装を再定義していたものを1本化する（`tests/api/_authz_probe.py` と
同じ抽出方針・ロジックは移動のみで挙動は変更しない）。
"""
from __future__ import annotations

import pathlib


def _mk(root, gen, prog):
    d = pathlib.Path(root) / gen / "03_開発" / "01_ソース"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{prog}.cbl").write_text(
        "       IDENTIFICATION DIVISION.\n"
        f"       PROGRAM-ID. {prog}.\n"
        "       PROCEDURE DIVISION.\n           STOP RUN.\n", encoding="utf-8")
