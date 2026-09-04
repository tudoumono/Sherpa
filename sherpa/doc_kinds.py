"""文書の「種類」判定に使う拡張子集合（単一の真実源）。

`grep_tool._TEXT_EXT` が対象にしている非MD拡張子（COBOL/JCL/COPYBOOK 等＝ソース原文）のうち
「コード」とみなす集合（高速な事前フィルタ用途・最終判定ではない）。`search_service.search(kinds=...)`
の docs/code フィルタ・`grep_tool` の対象拡張子判定・`layer.py`（探す対象＝層フィルタの拡張子ベース
近似・`layer_of()`）がこの集合を参照する（値がズレて片方だけ更新される事故を防ぐ）。最終判定
（accepts() 確定後の code/資料/未対応）は `corpus_docs.classify_document` に集約する（§7 裁定10）
——`CODE_EXT` の集合だけで「コード」と見なさない。

`search_service.py` は `grep_tool` を import できない契約（circular import 回避・
`sherpa.search_service` の docstring 参照）のため、本モジュールはどの sherpa モジュールも
import しない葉ノードにする（`grep_tool`・`layer`・`search_service` 等、複数の上位モジュールから
安全に import できる）。

`CODE_EXT` の値そのものの真実源は `sherpa.ingest.analyzers.registry.registered_extensions()`
（言語アナライザ登録簿・コード解析層のコンポーネント化）——本モジュールは葉ノードのまま保つため
`__getattr__` で属性アクセス時にだけ引く（モジュール読み込み時には何も import しない）。
"""
from __future__ import annotations


def __getattr__(name: str):
    if name == "CODE_EXT":
        from .ingest.analyzers import registry
        return registry.registered_extensions()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
