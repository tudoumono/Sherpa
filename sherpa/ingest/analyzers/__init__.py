"""言語アナライザ群。`registry` が拡張子→アナライザ解決の単一の真実源で、言語クラスは `_base.Analyzer` から直接派生する。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations
