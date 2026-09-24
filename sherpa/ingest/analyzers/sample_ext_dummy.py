"""拡張の契約（S4・docs/21-拡張の契約.md）のサンプル拡張アナライザ。

フォーク側が `<prefix>_*.py` 規約でアナライザを足す最小の実例——`registry.discover_extension_
analyzers()` が本ファイルを発見・契約検証して `_ANALYZERS` の末尾に登録する（既定で有効）。
拡張子 `.sampleext` は上流アナライザと衝突せず、実運用の資料にこの拡張子は存在しないため、
本ファイルが常駐しても既存の取り込みには影響しない。`make verify-extension`・
`tests/unit/test_analyzer_extensions.py` の受け入れ対象。
"""
from __future__ import annotations

from pathlib import PurePosixPath

# 絶対 import（相対 import ではない）: 発見時は `importlib.util.spec_from_file_location` で
# 独立モジュールとして読み込む（パッケージ文脈を持たない）ため、`._base` のような相対 import は
# 使えない——フォーク側の拡張アナライザも同じ制約を受ける（docs/21-拡張の契約.md）。
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


#: `registry.discover_extension_analyzers()` が探すモジュール属性（この名前で固定）。
ANALYZER = SampleExtDummyAnalyzer()
