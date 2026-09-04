"""言語アナライザ群（コード解析層のコンポーネント化・正典 docs/proposals/2026-08-29-コード解析層のコンポーネント化.md）。

`registry` が拡張子→アナライザ解決の単一の真実源。個々の言語クラスは `_base.Analyzer` から
直接派生する（中間の言語別基底は作らない・§7 裁定1）。
"""
from __future__ import annotations
