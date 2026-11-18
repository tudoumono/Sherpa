"""期をまたいだ同じ部品の見つけ方と、表記の揺れ（ソース・文書のフォルダ名の違い）。"""
from __future__ import annotations

from collections import defaultdict

from .model import Comparison

SOURCE_DIR_NAMES = frozenset({"src", "source", "sources", "ソース", "ソースコード"})
DOC_DIR_NAMES = frozenset({"docs", "doc", "設計", "設計書", "ドキュメント"})
VARIANT_GROUPS = (SOURCE_DIR_NAMES, DOC_DIR_NAMES)


def variant_pair(a: str, b: str):
    """期を除いた相対パス a・b が、フォルダ名 1 か所だけ違い（ファイル名は同じ）、その組が同じ組（ソース・文書）に入るなら (x, y)。"""
    pa, pb = a.split("/"), b.split("/")
    if len(pa) != len(pb):
        return None
    diff = [k for k, (x, y) in enumerate(zip(pa, pb)) if x != y]
    if len(diff) != 1 or diff[0] == len(pa) - 1:
        return None
    x, y = pa[diff[0]], pb[diff[0]]
    return (x, y) if any(x.lower() in g and y.lower() in g for g in VARIANT_GROUPS) else None


def compare_neighbours(order: list, files: dict) -> Comparison:
    """並び順の隣どうしの期を比べる。files は 期 → ファイル名 → [期を除いた相対パス]。
    ファイル名が前の期にも後の期にもちょうど 1 つずつあり、期を除いた相対パスが違うものを、揺れの組か別の部品に分ける。"""
    out = Comparison()
    variants = defaultdict(list)
    for prev, nxt in zip(order, order[1:]):
        a, b = files.get(prev, {}), files.get(nxt, {})
        shared = {t for lst in a.values() for t in lst} & {t for lst in b.values() for t in lst}
        out.same += len(shared)
        if not shared:
            out.empty_pairs.append(f"{prev} → {nxt}")
        for name in sorted(set(a) & set(b)):
            if len(a[name]) != 1 or len(b[name]) != 1 or a[name][0] == b[name][0]:
                continue
            ta, tb = a[name][0], b[name][0]
            pair = variant_pair(ta, tb)
            line = f"{prev}/{ta} → {nxt}/{tb}"
            (variants[pair] if pair else out.others).append(line)
    out.variants = dict(sorted(variants.items()))
    return out
