"""サンプル拡張アナライザ。フォーク側が `<prefix>_*.py` 規約でアナライザを足す最小の実例。

`registry.discover_extension_analyzers()` が本ファイルを発見・契約検証して `_ANALYZERS` の末尾に登録する。拡張子 `.sampleext` は上流と衝突せず、実運用の資料に存在しないため、既存の取り込みには影響しない。
"""
from __future__ import annotations

from pathlib import PurePosixPath

# 絶対 import を使う（発見時は独立モジュールとして読み込むため、`._base` のような相対 import は使えない）。
from sherpa.ingest.analyzers._base import Analyzer, DefItem, DefResult, RefResult


class SampleExtDummyAnalyzer(Analyzer):
    """最小の拡張アナライザ（ファイル名を1個の `Module` 定義として返すだけ）。"""

    name = "sample_ext:dummy"
    extensions = frozenset({".sampleext"})
    doctype = "sample_ext_dummy"
    version = 1

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        primary = DefItem(label="Module", name=PurePosixPath(rel_path).name)
        return DefResult(primary=primary)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        return RefResult()


# `registry.discover_extension_analyzers()` が探すモジュール属性（この名前で固定）。
ANALYZER = SampleExtDummyAnalyzer()
