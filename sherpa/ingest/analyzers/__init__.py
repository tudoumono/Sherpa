"""言語アナライザ群。`registry` が拡張子→アナライザ解決の単一の真実源で、言語クラスは `_base.Analyzer` から直接派生する。
外から使ってよい名前はここに出したものだけ。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

from . import _vb_project as vb_project
from . import registry
from ._base import (DefResult, FileContext, PluginDefs, PluginRefs, RefResult, TypeCandidate, TypeLookup,
                    TypeRelations)
from ._ts import require as require_tree_sitter
