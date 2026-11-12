"""部品化の移行のための入口。新しいコードは `sherpa.ingest.graph` を使う。

鏡モデルのグラフ構築（資料フォルダの走査からノード・エッジの生成まで）の実体は `sherpa.ingest.graph`。
ここは、これまで `world_graph.<名前>` で外から使っていた名前を、部品から出し直すだけ。
設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

from .. import corpus_docs  # noqa: F401  # 試験が `world_graph.corpus_docs` を差し替える
from .graph import (  # noqa: F401
    BUILD_REPORT_REASON, EDGE_RULES, applied_marks_sig, EDGE_SOURCES_MAX, MENTION_SCHEMA_VERSION, aggregate_pass2_edges, build_world, cap_sources,
    dataflow_pass, mention_edges_for_doc, mention_max_per_doc, mention_min_len, mention_pass, mention_tokenize,
    plugin_failures_from_flags, pop_build_report, resolve_nearest, resolve_path, resolve_path_suffix, type_candidates,
    unparsed_sources_from_flags, valid_rel_parts,
)
from .graph import document_cid as _document_cid  # noqa: F401
from .graph import make_cid as _cid  # noqa: F401

_dataflow_pass = dataflow_pass
_mention_edges_for_doc = mention_edges_for_doc
_mention_max_per_doc = mention_max_per_doc
_mention_min_len = mention_min_len
_mention_pass = mention_pass
_mention_tokenize = mention_tokenize
_resolve_nearest = resolve_nearest
_resolve_path_suffix = resolve_path_suffix
_type_candidates = type_candidates
