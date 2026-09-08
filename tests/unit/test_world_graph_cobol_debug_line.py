"""COBOL のデバッグ行（7桁目 `D`）が world 単位で二重に flags へ載らないことの回帰テスト。

`WITH DEBUGGING MODE` が無いファイルの D 行は `Dropped("debug_line", ...)` として記録されるが、
所有は `collect_defs`（Pass1）側だけ——`extract_refs`（Pass2）は同じ行を読み飛ばすだけで
`Dropped` を返さない（`sherpa/ingest/analyzers/cobol.py` 参照）。build_world の `_flag_dropped`
は Pass1/Pass2 の両方の `dropped` を flags へ変換するため、二重所有だと同じ物理行が2件載る。
"""
from __future__ import annotations

import pathlib

from sherpa.ingest import world_graph

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_single_debug_line_produces_exactly_one_debug_line_flag(tmp_path):
    wd = tmp_path / "world"
    wd.mkdir()
    (wd / "P.cbl").write_text(
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "      D    DISPLAY 'X'.\n",
        encoding="utf-8",
    )
    nodes, edges, flags = world_graph.build_world(wd, "w")
    debug_flags = [f for f in flags if f.get("why") == "debug_line"]
    assert len(debug_flags) == 1
    assert debug_flags[0]["line"] == 2
    assert debug_flags[0]["analyzer"] == "cobol"
