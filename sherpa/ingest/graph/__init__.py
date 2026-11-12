"""フォルダの取り込みのグラフ構築の部品。外から使ってよい名前はここに出したものだけ。

設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

from .aggregate import (
    BUILD_REPORT_REASON, EDGE_RULES, EDGE_SOURCES_MAX, aggregate_pass2_edges, cap_sources, plugin_failures_from_flags,
    pop_build_report, unparsed_sources_from_flags,
)
from .build import applied_marks_sig, build_world
from .flow import dataflow_pass
from .mention import (
    MENTION_SCHEMA_VERSION, mention_edges_for_doc, mention_max_per_doc, mention_min_len, mention_pass, mention_tokenize,
)
from .paths import resolve_path, valid_rel_parts
from .periods_select import distance_of, scoped, stamp_with
from .select import resolve_nearest, resolve_path_suffix, top_folder, tree_distance, type_candidates
from .state import document_cid, make_cid, make_node
