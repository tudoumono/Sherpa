"""図形とコネクタ（Evidence IR の `connects_to` 関係）から Mermaid flowchart を組み立てる。

同じ入力なら常に同じ Markdown を返す純関数（LLM・座標からの推測は使わない）。
- ノード ID: 表示名ではなく `Locator.object_id` 由来にする。
- ノード集合: `connects_to` の端点だけ。
- ラベル: ①要素自身のテキスト ②`overlaps` で重なる近傍セルの値 ③図形名 ④要素種別 の順。
- 未接続コネクタ: ノードには出さず、`%%` コメント行として残す。
設計: docs/design/rag.md「ほかの取り込み部品」
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from . import evidence_ir

MERMAID_RENDERER_VERSION = "mermaid-flowchart-v1alpha1"

# ラベル中の形状デリミタは HTML 実体参照へ変換する（ラベルは常に "..." で囲む）
_LABEL_ESCAPES = {
    "[": "&#91;", "]": "&#93;",
    "{": "&#123;", "}": "&#125;",
    "|": "&#124;",
    '"': "&#34;", "'": "&#39;",
}

# prst（DrawingML のプリセット図形種）→ Mermaid のノード形状デリミタ（開き, 閉じ）
_SHAPE_BY_PRST: dict[str, tuple[str, str]] = {
    "flowChartDecision": ("{", "}"),
    "diamond": ("{", "}"),
    "flowChartTerminator": ("([", "])"),
    "ellipse": ("([", "])"),
    "roundRect": ("([", "])"),
    "flowChartInputOutput": ("[/", "/]"),
    "trapezoid": ("[/", "/]"),
    "flowChartPreparation": ("{{", "}}"),
    "hexagon": ("{{", "}}"),
    # 手作業の記号は逆台形
    "flowChartManualOperation": ("[\\", "/]"),
    # document は専用記法が無いため非対称形で代用
    "flowChartDocument": (">", "]"),
    "flowChartConnector": ("((", "))"),
}
_DEFAULT_SHAPE = ("[", "]")  # 未知/既定＝process（矩形）


def escape_label(text: str) -> str:
    """Mermaidのノード形状デリミタと衝突する文字をHTML実体参照へ変換し、空白を1行へ畳む。"""
    escaped = "".join(_LABEL_ESCAPES.get(char, char) for char in text)
    return " ".join(escaped.split())


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip() if not isinstance(value, str) else value.strip()


def _shape_for(prst: Any) -> tuple[str, str]:
    if isinstance(prst, str) and prst in _SHAPE_BY_PRST:
        return _SHAPE_BY_PRST[prst]
    return _DEFAULT_SHAPE


def _node_slug(element: evidence_ir.EvidenceElement, used: set[str]) -> str:
    object_id = element.locator.object_id
    base = f"n{object_id}" if isinstance(object_id, int) else f"n{_fallback_slug(element.element_id)}"
    slug, suffix = base, 2
    while slug in used:
        slug = f"{base}_{suffix}"
        suffix += 1
    used.add(slug)
    return slug


def _fallback_slug(element_id: str) -> str:
    # object_id が無いときだけの経路。コロンを避けるため短い hex へ畳む
    return hashlib.sha256(element_id.encode("utf-8")).hexdigest()[:12]


def _nearby_cell_label(
    element_id: str,
    node_ids: set[str],
    overlaps: Sequence[evidence_ir.EvidenceRelation],
    pool: Mapping[str, evidence_ir.EvidenceElement],
) -> str:
    for relation in overlaps:
        if element_id not in (relation.source_id, relation.target_id):
            continue
        other_id = relation.target_id if relation.source_id == element_id else relation.source_id
        other = pool.get(other_id)
        if other is not None and other.type == "cell":
            text = _text(other.value)
            if text:
                return text
    return ""


def render_flowchart(
    elements: Sequence[evidence_ir.EvidenceElement],
    connects_to: Sequence[evidence_ir.EvidenceRelation],
    *,
    overlaps: Sequence[evidence_ir.EvidenceRelation] = (),
    elements_by_id: Mapping[str, evidence_ir.EvidenceElement] | None = None,
) -> str | None:
    """`elements`（コンテナ内の図形/コネクタ）と`connects_to`関係からMermaid flowchartを組み立てる。

    コネクタ要素が1個も無ければ`None`（呼び出し元のコンテナ単位ゲートと同じ判定をここでも
    独立して満たす）。コネクタが有れば、解決できた分はノード+エッジへ、解決できなかった分は
    `%%`コメント行の注記として残す（結果が0エッジのみでも`None`にはしない＝黙って消えない）。
    """
    connectors = [element for element in elements if element.type == "connector"]
    if not connectors:
        return None

    pool: dict[str, evidence_ir.EvidenceElement] = dict(elements_by_id or {})
    pool.update({element.element_id: element for element in elements})

    node_ids = list(dict.fromkeys(
        endpoint
        for relation in connects_to
        for endpoint in (relation.source_id, relation.target_id)
        if endpoint in pool
    ))
    node_id_set = set(node_ids)
    ordered_nodes = sorted(node_ids, key=lambda eid: (pool[eid].order, eid))

    used_slugs: set[str] = set()
    slug_by_id = {element_id: _node_slug(pool[element_id], used_slugs) for element_id in ordered_nodes}

    lines = ["flowchart TD"]
    for element_id in ordered_nodes:
        element = pool[element_id]
        label = (
            _text(element.value)
            or _nearby_cell_label(element_id, node_id_set, overlaps, pool)
            or _text(element.extension.get("name"))
            or element.type
        )
        opener, closer = _shape_for(element.extension.get("prst"))
        lines.append(f'{slug_by_id[element_id]}{opener}"{escape_label(label)}"{closer}')

    resolved_connector_ids: set[str] = set()
    ordered_edges = sorted(
        (relation for relation in connects_to if relation.source_id in slug_by_id and relation.target_id in slug_by_id),
        key=lambda relation: (pool[relation.source_id].order, relation.source_id, relation.target_id),
    )
    for relation in ordered_edges:
        connector_id = relation.extension.get("connector_element_id")
        if isinstance(connector_id, str):
            resolved_connector_ids.add(connector_id)
        lines.append(f"{slug_by_id[relation.source_id]} --> {slug_by_id[relation.target_id]}")

    unconnected = sorted(
        (connector for connector in connectors if connector.element_id not in resolved_connector_ids),
        key=lambda connector: (connector.order, connector.element_id),
    )
    for connector in unconnected:
        label = _text(connector.value) or _text(connector.extension.get("name")) or "コネクタ"
        lines.append(f"%% 未接続: {escape_label(label)}（object {connector.locator.object_id}）")

    return "\n".join(lines) + "\n"
