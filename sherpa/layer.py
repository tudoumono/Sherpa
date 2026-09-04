"""探す対象（層）フィルタ＝資料/コードのハードフィルタ（調べ方ブロック §3.4・鏡モデルの範囲フィルタと同型）。

`layer`: `"docs" | "code" | "both"`（既定 `"both"`＝フィルタなし）。範囲（`scope.py`）と同じく
grep・ES・list_docs・read_around に対する硬いフィルタとして適用する。

判定は2段構え:
- `layer_of()`/`in_layer()`（拡張子ベース・`doc_kinds.CODE_EXT` を参照）は**高速な近似**——
  実ファイルを読まずに拡張子だけで判定する。CODE-1a のコード解析層では、登録拡張子でも
  `accepts()`（内容判定）が全滅すれば資料/未対応へ戻る（§7 裁定10）ため、この近似は
  「担当アナライザが必ず受理する」という前提が崩れると不正確になりうる。
- `layer_of_code()`/`in_layer_code()`（`accepts()` 確定後の bool を受け取る）が**確定判定**——
  呼び出し元（`grep_tool.grep_search` の `classify_document` 結果・`agentic_search` の
  `doc_ledger` 行の `branch=="source"`）が既に持っている「実際に code と確定したか」を渡す。
  実ファイルを読める文脈ではこちらを使う（`corpus_docs.classify_document` と同じ判定を
  二重に持たない・§7 裁定10）。

**適用しない対象**（`docs/proposals/2026-08-29-調べ方ブロック.md` §3.5・§8 裁定1/7）:
- グラフ traversal（impact レンズ・troubleshoot レンズの近傍探索）は言及エッジ（`DOCUMENTS` の
  `via=mention`）が Document とコードを木を跨いで繋ぐため、層で分断しない
  （`applies_to_lens()` が qa/author のみ真を返す）。
- 個人ファイル（workspace）検索は独立の拡張子集合（`chat_service._PERSONAL_SEARCHABLE_EXT`）を持ち、
  共有 KB の層フィルタとは無関係（CLAUDE.md の grep-only 契約）。

このモジュールは `doc_kinds` 以外の sherpa モジュールを import しない（`grep_tool`・`es_index`・
`search_service`・`agentic_search`・`chat_service`・`providers` 等どこからでも安全に import できる・
`corpus_docs`（`classify_document` の実体）は import しない——葉ノードのまま保つ）。
"""
from __future__ import annotations

from pathlib import Path

from .doc_kinds import CODE_EXT

LAYERS = ("docs", "code", "both")

# 層フィルタが実効しないレンズ（§3.5・裁定1: troubleshoot も ES 補完/運用手順 grep を含め全体を非適用にする）。
_LENS_NOT_APPLIED = frozenset({"impact", "troubleshoot"})


def normalize_layer(layer) -> str:
    """欠落（`layer is None`）は `"both"`（フィルタなし）。

    HTTP 入口（`ChatReq`/`ExtSearchReq`）は pydantic `Literal["docs","code","both"]` で不正値を
    422 にするため、ここに `None` でも `"docs"/"code"/"both"` でもない値が届くのは呼び出し側の
    プログラミングミス（検証を経ない内部値）を示す——黙って `"both"` へ丸めず `ValueError` にする
    （fail-loud）。
    """
    if layer is None:
        return "both"
    if isinstance(layer, str):
        v = layer.strip().lower()
        if v in LAYERS:
            return v
    raise ValueError(f"invalid layer value: {layer!r}")


def layer_of(rel_path: str) -> str:
    """rel_path（doc_id＝元ファイルの拡張子）が属す層の**近似**（`CODE_EXT` に含まれれば `"code"`、
    それ以外は `"docs"`・決定的MD・直置き `.md`/`.txt`・Office/PDF 派生MD・画像メタデータ由来MD を
    含む）。実ファイルを読める文脈（`accepts()` の確定結果を持てる）では `layer_of_code()` を使う。
    """
    ext = Path(rel_path or "").suffix.lower()
    return "code" if ext in CODE_EXT else "docs"


def layer_of_code(is_code: bool) -> str:
    """`accepts()` 確定後の bool（`classify_document` の `kind=="code"` 相当）から層を導く——
    確定 `False`（資料・未対応・unreadable のいずれも）は一律 `"docs"` 側（§7 裁定10 と同じ
    「CODE_EXT の集合だけで『コード』と見なさない」原則を層フィルタにも揃える）。"""
    return "code" if is_code else "docs"


def in_layer(rel_path: str, layer) -> bool:
    """rel_path が指定層に含まれるか（近似・`layer_of()` 参照）。`"both"`（既定＝layer 省略）は
    常に真。不正な layer 値は `normalize_layer` が `ValueError` を送出する（fail-loud・黙って
    both へ丸めない）。"""
    lv = normalize_layer(layer)
    return lv == "both" or layer_of(rel_path) == lv


def in_layer_code(is_code: bool, layer) -> bool:
    """`accepts()` 確定後の bool から層一致を判定する（`in_layer()` の確定判定版・`layer_of_code()`
    参照）。`"both"`（既定＝layer 省略）は常に真。"""
    lv = normalize_layer(layer)
    return lv == "both" or layer_of_code(is_code) == lv


def es_filter(layer):
    """`es_index.search()`/`search_knn_only()` 用の層フィルタ節（`scopes` の `terms` フィルタと
    同じ場所に足す）。`"both"`（既定＝layer 省略）は `None`（フィルタなし＝既存挙動と完全同一）。
    不正な layer 値は `normalize_layer` が `ValueError` を送出する。

    grep/agentic と同じ**確定判定**（`branch=="source"`）で絞る——拡張子（`CODE_EXT` membership）
    ではない（`layer_of_code()`/`in_layer_code()` と同じ原則・§7 裁定10）。`ext` ベースだと、
    登録拡張子でも `accepts()` が全滅して資料へ倒れた文書（例: `.txt` を宣言するアナライザが
    拒否したファイル）が誤って code 側に数えられてしまう。`es_index.index_world()` が索引時に
    `branch`（`corpus_docs.iter_world_documents` の confirmed 値＝`"source"`＝code、それ以外＝
    docs）をフィールドとして保存する（マッピングは keyword・`ES_MAPPING_VERSION` 参照）。"""
    lv = normalize_layer(layer)
    if lv == "both":
        return None
    if lv == "code":
        return {"term": {"branch": "source"}}
    return {"bool": {"must_not": {"term": {"branch": "source"}}}}


def applies_to_lens(lens: str) -> bool:
    """このレンズで層フィルタが実効するか（`answer.scope.layer_applied` に使う・§3.5）。"""
    return lens not in _LENS_NOT_APPLIED


def effective_layer(scope_meta, lens: str):
    """検索ツール（grep/ES／`run_tool`／Codex の MCP・直接探索）へ実際に渡す layer 値。

    非適用レンズ（`applies_to_lens(lens)` が偽）では明示的に `"both"` を返し、要求された層に
    かかわらず実際の検索を絞らない——`env["scope"]`（`scope_with_layer` が返す・元の layer 値を
    保持したまま `layer_applied=False` を添える）と実際の検索結果を一致させるための、provider
    共通の単一の判定点（qa/troubleshoot/impact/author の各呼び出し元がそれぞれ判定を複製しない）。
    適用レンズでは `scope_meta.get("layer")`（省略時 `None`＝`normalize_layer` が `"both"` に丸める）
    をそのまま返す。
    """
    if not applies_to_lens(lens):
        return "both"
    return (scope_meta or {}).get("layer")


def scope_with_layer(scope_meta, *, world: str, lens: str) -> dict:
    """`scope_meta`（None なら既定 world 全体／both）に `layer_applied` を足した新しい dict を返す。

    `chat_service._dispatch` と `providers/base.py`（LLM の反復ツール検索経路）の両方が使う共通
    ヘルパー——同じ「どのレンズで層フィルタが効くか」判定を2箇所に複製しない。呼び出し元の
    `scope_meta` 自体は変更しない（コピーを返す）。**返す dict の `layer` は要求された元の値の
    ままにする**（`effective_layer` が実検索へ渡す値を both に丸めても、ここでは丸めない——
    利用者が実際に選んだ値と「このレンズでは実効しなかった」という事実の両方を残す）。
    """
    sm = dict(scope_meta) if scope_meta else {"world": world, "scope_paths": [], "source": "all",
                                              "layer": "both"}
    sm["layer_applied"] = applies_to_lens(lens)
    return sm
