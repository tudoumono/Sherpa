"""ビルドの定義を読む場所の印を変えた後の作り直し: グラフと取り込みの台帳・名前の台帳を、入れ替えの行に通して一緒に作り直す。
MD 化・ES は触らない。台帳は下書き（`ledger_step.stage_ledger_step`）→ グラフの入れ替え（`load_world`）→ 切り替え（`ledger_step.switch_ledger_step`）の順で、それぞれ行へ「入れ替えた」を先に書く。
設計: docs/design/scope.md「ビルドの定義と実行名」
"""
from __future__ import annotations

from typing import Callable

from .. import build_marks, worlds
from . import ledger_step, world_graph, world_graph_service, world_neo4j
from ..store.replace_rows import ReplaceRow


class RebuildBlocked(RuntimeError):
    """グラフを組めなかった（資料フォルダが解決できない・読めないコードがある）。何も入れ替えていない。"""


def rebuild_graph_and_ledgers(world: str, ledger_rows: Callable[[], list], *, row: ReplaceRow, build_graph: Callable | None = None) -> tuple:
    """① グラフを組む ② 台帳と名前の台帳を下書きする ③ グラフを入れ替える ④ 台帳と名前の台帳の見せる番号を切り替える。
    守ること: 呼び出し元が資料フォルダの lock を持っている前提。グラフを入れ替えた後に切り替えが失敗したら例外のまま伝え、行を残す。
    戻りは `(ノード数, 線の数)`。"""
    return _reflect(world, ledger_rows, row, build_graph, with_ledgers=True)


def reflect_graph(world: str, ledger_rows: Callable[[], list], *, row: ReplaceRow, build_graph: Callable | None = None) -> tuple:
    """今の資料から組んだグラフでグラフを入れ替える。ただし、ビルドの定義を読む場所の印を当てはめた結果がグラフの持つ値と違うときは、
    台帳と名前の台帳も一緒に作り直す（グラフだけに新しい印が当たって台帳が古いまま残らないように）。戻りは `(ノード数, 線の数)`。
    守ること: 新しい印を当てる経路は、これか `rebuild_graph_and_ledgers` だけ。呼び出し元が資料フォルダの lock を持っている前提。"""
    return _reflect(world, ledger_rows, row, build_graph, with_ledgers=None)


def _reflect(world: str, ledger_rows: Callable[[], list], row: ReplaceRow, build_graph: Callable | None, *, with_ledgers: bool | None) -> tuple:
    """`with_ledgers` が None のときは、印の値がグラフの持つ値と違うとき（持つ値が取れないときを含む）だけ台帳も入れ替える。"""
    nodes, edges, flags = (build_graph or (lambda: world_graph_service.build_effective_world(world)))()
    build_report, flags = world_graph.pop_build_report(flags)
    blocked = sorted({str(f.get("reason")) for f in flags if f.get("action") == "blocked"})
    if blocked:
        raise RebuildBlocked(f"graph reflect blocked: {','.join(blocked)}")
    if with_ledgers is None:
        kept = world_neo4j.read_build_marks_sig(world)
        with_ledgers = kept is None or kept != world_graph.applied_marks_sig(worlds.world_dir(world), world, build_marks.load(world))   # 保存済みの値が取れないときも作り直す
    env = world_neo4j._env()
    staged = ledger_step.stage_ledger_step(world, ledger_rows(), nodes, row=row, edges=edges) if with_ledgers else None   # 古い番号の行を消し、新しい番号を下書きする（グラフの入れ替えより前）
    counts = world_neo4j.load_world(nodes, edges, world, env["uri"], env["user"], env["pw"],
                                    plugin_failures=world_graph.plugin_failures_from_flags(flags),
                                    unparsed_sources=world_graph.unparsed_sources_from_flags(flags),
                                    **({"build_report": build_report} if build_report else {}), row=row)
    if staged is not None:
        ledger_step.switch_ledger_step(world, staged, row=row)   # 見せる番号を切り替え、古い番号の行を小分けで消す
    return counts
