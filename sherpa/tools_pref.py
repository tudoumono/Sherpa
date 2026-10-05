"""検索経路トグル。会話ごとに grep／全文・ベクトル（ES）／グラフの3経路の利用可否を選ぶ。

`tools`: `{"grep": bool, "fulltext": bool, "graph": bool}`（省略＝全 ON）。土台系ツールは対象外で常時 ON。
`grep` は `ripgrep_search` と `glob_search` を同時にゲートする。3つとも False は不正。
ES/Neo4j が到達不可なら、この設定に関わらずそのツールは提示しない（可用性そのものは判定しない）。
他の sherpa モジュールを import しない葉ノード。
"""
from __future__ import annotations

TOOLS_PREF_KEYS = ("grep", "fulltext", "graph")
DEFAULT_TOOLS_PREF = {"grep": True, "fulltext": True, "graph": True}


def normalize_tools_pref(v) -> dict:
    """欠落（`None`）は全 ON。不正値（既知の3キー以外・bool 以外・3つとも False）は `ValueError`。"""
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
