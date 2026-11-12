"""ビルドの定義の段（データの流れの後）: つながらない実行物の呼び出しを実行名のノードへ張り、つないだ分の未解決の申告を取り消す。

設計: docs/design/scope.md「ビルドの定義と実行名」
"""
from __future__ import annotations

from .. import build_link
from .aggregate import BUILD_REPORT_REASON
from .state import WorldBuild


def add_build_definitions(b: WorldBuild) -> None:
    """ビルドの定義から読んだ実行名と材料の組を作業領域へ足す。読み取りの報告は `BUILD_REPORT_REASON` の申告に載せる。"""
    built = build_link.link_builds(b.world_id, b.files, b.exec_calls, b.rel_name, b.build_marks, b.period_select,
                                  b.exec_calls_resolved)
    b.nodes.update(built.nodes)
    if built.replaced:
        gone = {id(e) for e in built.replaced}
        b.link_edges[:] = [e for e in b.link_edges if id(e) not in gone]
    b.link_edges.extend(built.edges)
    if built.linked:
        linked_flags = {id(c.flag) for c in built.linked}
        linked_items = {id(c.item) for c in built.linked}
        b.flags[:] = [f for f in b.flags if id(f) not in linked_flags]
        for rel in {c.rel for c in built.linked}:
            kept = [it for it in b.unresolved_by_rel[rel] if id(it) not in linked_items]
            if kept:
                b.unresolved_by_rel[rel] = kept
            else:
                del b.unresolved_by_rel[rel]
    if built.report:
        b.flags.append({"reason": BUILD_REPORT_REASON, "report": built.report})
