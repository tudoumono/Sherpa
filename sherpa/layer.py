"""探す対象（層）フィルタ＝資料/コードのハードフィルタ。

`layer`: `"docs" | "code" | "both"`（既定 `"both"`＝フィルタなし）。範囲（`scope.py`）と同じく
grep・ES・list_docs・read_around に対する硬いフィルタとして適用する。

判定は2段構え:
- `layer_of()`/`in_layer()`: 拡張子ベース（`doc_kinds.CODE_EXT`）の高速な近似。
- `layer_of_code()`/`in_layer_code()`: `accepts()` 確定後の bool を受け取る確定判定。実ファイルを読める文脈ではこちらを使う。

適用しない対象: グラフ traversal（言及エッジが資料とコードを繋ぐため。`applies_to_lens()` が qa/author のみ真）・
個人ファイル（workspace）検索（共有 KB の層フィルタと無関係）。
`doc_kinds` 以外の sherpa モジュールを import しない葉ノード。
設計: docs/design/scope.md「層（layer＝`docs`/`code`/`both`）——同名で別物の列に注意」
"""
from __future__ import annotations

from pathlib import Path

from .doc_kinds import CODE_EXT

LAYERS = ("docs", "code", "both")

# 層フィルタが実効しないレンズ
_LENS_NOT_APPLIED = frozenset({"impact", "troubleshoot"})


def normalize_layer(layer) -> str:
    """欠落（`None`）は `"both"`。docs/code/both 以外は黙って丸めず `ValueError`。"""
    if layer is None:
        return "both"
    if isinstance(layer, str):
        v = layer.strip().lower()
        if v in LAYERS:
            return v
    raise ValueError(f"invalid layer value: {layer!r}")


def layer_of(rel_path: str) -> str:
    """rel_path が属す層の近似（`CODE_EXT` に含まれれば `"code"`、それ以外は `"docs"`）。"""
    ext = Path(rel_path or "").suffix.lower()
    return "code" if ext in CODE_EXT else "docs"


def layer_of_code(is_code: bool) -> str:
    """`accepts()` 確定後の bool から層を導く（False は一律 `"docs"`）。"""
    return "code" if is_code else "docs"


def in_layer(rel_path: str, layer) -> bool:
    """rel_path が指定層に含まれるか（近似）。`"both"` は常に真。"""
    lv = normalize_layer(layer)
    return lv == "both" or layer_of(rel_path) == lv


def in_layer_code(is_code: bool, layer) -> bool:
    """`accepts()` 確定後の bool から層一致を判定する。`"both"` は常に真。"""
    lv = normalize_layer(layer)
    return lv == "both" or layer_of_code(is_code) == lv


def es_filter(layer):
    """`es_index.search()`/`search_knn_only()` 用の層フィルタ節。`"both"` は `None`（フィルタなし）。

    拡張子でなく確定判定（索引時に保存した `branch`＝`"source"` が code、それ以外が docs）で絞る。
    """
    lv = normalize_layer(layer)
    if lv == "both":
        return None
    if lv == "code":
        return {"term": {"branch": "source"}}
    return {"bool": {"must_not": {"term": {"branch": "source"}}}}


def applies_to_lens(lens: str) -> bool:
    """このレンズで層フィルタが実効するか（`answer.scope.layer_applied` に使う）。"""
    return lens not in _LENS_NOT_APPLIED


def effective_layer(scope_meta, lens: str):
    """検索ツールへ実際に渡す layer 値。非適用レンズでは `"both"`、適用レンズでは `scope_meta` の `layer`。"""
    if not applies_to_lens(lens):
        return "both"
    return (scope_meta or {}).get("layer")


def scope_with_layer(scope_meta, *, world: str, lens: str) -> dict:
    """`scope_meta`（None なら既定 world 全体／both）に `layer_applied` を足した新しい dict を返す（元は変更しない）。

    返す dict の `layer` は要求された元の値のまま。
    """
    sm = dict(scope_meta) if scope_meta else {"world": world, "scope_paths": [], "source": "all",
                                              "layer": "both"}
    sm["layer_applied"] = applies_to_lens(lens)
    return sm
