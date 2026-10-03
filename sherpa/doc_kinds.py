"""文書の「種類」判定に使う拡張子集合。

`CODE_EXT`（コードとみなす拡張子）を grep・融合検索・層フィルタが共有する。事前フィルタ用で最終判定ではない
（最終判定は `corpus_docs.classify_document`）。値の源は `ingest.analyzers.registry.registered_extensions()`。
循環 import を避けるため sherpa の他モジュールを import しない葉ノード（属性アクセス時に `__getattr__` で引く）。
"""
from __future__ import annotations


def __getattr__(name: str):
    if name == "CODE_EXT":
        from .ingest.analyzers import registry
        return registry.registered_extensions()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
