"""図形＋コネクタ（Evidence IRの`connects_to`関係）からMermaid flowchartを組み立てる（L9・R3）。

`docs/proposals/2026-09-02-RAG表現の全形式展開と文脈保持.md`§8.4/§8.5の裁定に従い、外部ライブラリ
（excel2md等）へは依存しない自前実装。入力（要素集合＋`connects_to`/`overlaps`関係）が同じなら常に
同じMarkdownテキストを返す純関数——LLMは使わず、推測（座標からのエッジ捏造等）もしない。

**ノードID**: 表示名ではなく`Locator.object_id`由来にする（`object_id`はOOXMLの`prstGeom`と同じ
namespace内で一意——excel2mdの`_v14_sanitize_node_id`のようにASCII以外の表示名を`_`へ潰すと
日本語ラベルで衝突する欠陥を踏まない）。

**ノード集合**: コンテナ（シート/スライド）内の全図形ではなく、`connects_to`の端点だけに絞る
（無関係な図形をMermaidへ混ぜて肥大化させない・「同一コンテナ内の図形群＋コネクタ」の
「図形群」は"フローを構成する図形"の意で解釈する）。

**ラベルの優先順位**: ①要素自身のテキスト → ②`overlaps`関係で重なる近傍セルの値
（bboxの再走査はしない・既存関係から引くだけ） → ③図形名（`extension["name"]`）→ ④要素種別。

**未接続コネクタ**: `connects_to`へ解決できなかったコネクタ要素は、ノードからは静かに落ちるが
Mermaidコード内の`%%`コメント行として残す（黙って消えない）。
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from . import evidence_ir

MERMAID_RENDERER_VERSION = "mermaid-flowchart-v1alpha1"

# Mermaidのノード形状デリミタがラベル中に出現すると構文と衝突するため、HTML実体参照へ変換する
# （ラベルは常に`"..."`で囲むため、二重引用符自体もここで潰しておけば追加のエスケープ処理は不要）。
_LABEL_ESCAPES = {
    "[": "&#91;", "]": "&#93;",
    "{": "&#123;", "}": "&#125;",
    "|": "&#124;",
    '"': "&#34;", "'": "&#39;",
}

# prst（DrawingMLのプリセット図形種）→ Mermaid flowchartのノード形状デリミタ（開き, 閉じ）。
# 標準のMermaid flowchart記法（rhombus={}/stadium=([])/hexagon={{}}/parallelogram=[//]/
# circle=(())等）に基づく。**注意**: 依頼文面が挙げていた「decision={{}}」は実際にはhexagonの
# 記法であり、decision（菱形）とhexagonが衝突する。ここでは標準記法どおりdecision={}・
# hexagon={{}}へ是正して固定する（判断の根拠はL9実装時のコメントとしてここへ残す）。
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
    # 手作業（manual operation）の古典記号は「上辺が広い台形」。Mermaidの逆台形記法`[\...\/]`を
    # 割り当てる（decision/hexagonのような既存の衝突は無いための単純な語彙拡張）。
    "flowChartManualOperation": ("[\\", "/]"),
    # documentの波形下端に対応する専用記法はMermaid flowchartに無いため、視覚的に「特殊な処理」を
    # 示す非対称形（asymmetric）を代用として割り当てる。
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
    # object_idが無い（あり得ないが防御的に）場合だけの経路。element_idはASCIIハッシュ形式のため
    # そのままでも安全だが、"evidence:"接頭辞のコロンを避けるため短いhexへ畳む。
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
