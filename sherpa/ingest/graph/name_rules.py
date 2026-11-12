"""名前の対応（`name_rules`）のグラフ解決: 未解決の参照を、決まりで置き換えた「目標の形」に一致する定義全部へつなぐ。

設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

from .. import name_rows, resolve_settings
from .select import resolve_nearest
from .state import WorldBuild

MAX_TARGETS = 20


def _index_of(b: WorldBuild, kind: str) -> dict:
    """種類 `kind` の定義の名前の索引（取り込み 1 回の中で 1 回だけ作る）: `{突き合わせの形の長さ: {形: [定義の名前, ...]}}`。"""
    if kind not in b.name_index:
        idx: dict = {}
        for n in sorted({n for k, n in b.defs if k == kind and isinstance(n, str)}):
            for key in resolve_settings.match_keys(n):
                idx.setdefault(len(key), {}).setdefault(key, []).append(n)
        b.name_index[kind] = idx
    return b.name_index[kind]


def _hits(b: WorldBuild, kind: str, target: str) -> list | None:
    """目標の形に一致する定義の名前の列。`MAX_TARGETS` を超えたら None（21 件目で打ち切る）。"""
    t = name_rows.normalize_name(target)
    by_key = _index_of(b, kind).get(len(t), {})
    if "?" not in t:
        return list(by_key.get(t, []))
    pat, out = resolve_settings.target_pattern(t), []
    for key, names in by_key.items():
        if pat.fullmatch(key):
            out += [n for n in names if n not in out]
            if len(out) > MAX_TARGETS:
                return None
    return sorted(out)


def link_by_rules(b: WorldBuild, kind: str, name: str, ref_rel: str, push) -> str:
    """決まりを上から当て、目標の形に一致した定義の名前ごとに最近傍で解き、解けたもの全部を `push(rel, 定義の名前)` で張る。

    1 本でも張れた決まりで止めて `''` を返す。一致する名前が `MAX_TARGETS` を超えたら張らず `'ambiguous'`、どれも張れなければ `'unresolved'`。
    """
    for rule in b.name_rules:
        for target in resolve_settings.map_target(name, [rule]):
            hits = _hits(b, kind, target)
            if hits is None:
                return "ambiguous"
            resolved = []
            for n in hits:
                rel, status = resolve_nearest(b.defs, kind, n, ref_rel, b.period_select)
                if not status:
                    resolved.append((rel, n))
            if resolved:
                for rel, n in resolved:
                    push(rel, n)
                return ""
    return "unresolved"
