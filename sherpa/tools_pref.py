"""検索経路トグル（調べ方ブロック §3.6・SC-6e ツール制御）＝会話ごとに grep／全文・ベクトル（ES）／
グラフの3経路の利用可否を選べる。

`tools`: `{"grep": bool, "fulltext": bool, "graph": bool}`（既定 `None`/省略＝全 ON＝既存挙動と
完全同一）。土台系ツール（list_docs・doc_outline・read_doc・read_around・ask_user）は対象外＝
常時 ON のまま（`agentic_search.openai_tools`/`gemini_tools` 参照）。`grep` は `ripgrep_search` に加えて
`glob_search`（ファイル名/パスのグロブ検索）も同時にゲートする。3つとも False は不正
（検索経路が1つも無い状態を許さない）。

ES/Neo4j が到達不可な環境では、この設定に関わらずそのツール自体を提示しない
（`agentic_search._graph_available()`/`es_index.available()` の既存ゲートが優先・
「使えないものは選べる状態にしない」契約）——本モジュールはその可用性判定の結果と
利用者の希望を掛け合わせる側であり、可用性そのものは判定しない。

このモジュールは他の sherpa モジュールを import しない（`layer.py`/`depth_profile.py` と同じ
葉ノード原則）。
"""
from __future__ import annotations

TOOLS_PREF_KEYS = ("grep", "fulltext", "graph")
DEFAULT_TOOLS_PREF = {"grep": True, "fulltext": True, "graph": True}


def normalize_tools_pref(v) -> dict:
    """欠落（`None`）は全 ON。HTTP 入口（`ChatReq.tools`・`/chat/stream` の個別 query）はこの関数を
    経由して不正値を検出する——ここで送出する `ValueError` は pydantic の field_validator からは
    そのまま 422 に、GET の個別 query から組み立てた dict は呼び出し側が `HTTPException(422, ...)`
    へ変換する（`layer.normalize_layer`/`depth_profile.normalize_depth_profile` と同じ fail-loud
    契約）。既知の3キー以外・bool 以外の値・3つとも False はすべて不正。
    """
    if v is None:
        return dict(DEFAULT_TOOLS_PREF)
    if not isinstance(v, dict):
        raise ValueError(f"invalid tools value: {v!r}")
    extra = set(v) - set(TOOLS_PREF_KEYS)
    if extra:
        raise ValueError(f"invalid tools keys: {sorted(extra)!r}")
    out = {}
    for k in TOOLS_PREF_KEYS:
        raw = v.get(k, True)
        if not isinstance(raw, bool):
            raise ValueError(f"invalid tools.{k} value: {raw!r}")
        out[k] = raw
    if not any(out.values()):
        raise ValueError("tools: grep/fulltext/graph の少なくとも1つは有効にしてください")
    return out


def is_default(pref) -> bool:
    """全 ON（既定・省略）かどうか。"""
    return all(normalize_tools_pref(pref).values())
