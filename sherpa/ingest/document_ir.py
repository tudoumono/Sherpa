"""文書標準構造（document-ir-v3）の型と決定的 JSON 直列化。

MD 出力と並行して作る構造化表現。取り込み・書き出しは各アーム／`office_md.py` の責務で、
ここは `DocumentIR` のデータ型と直列化だけを持つ。
`doc_id` はアーム段階では空文字で、`office_md.build_derived` が原本相対パスを設定して確定する。
`element_id`（`para:N` 等）は再構築内で決定的な ID であり、要素の前方追加で連番がずれる（採番規則は `arms/ooxml_arm.py`）。
設計: docs/design/rag.md「人向け MD と RAG 正本の作り分け（マージの実際）」
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields

DOCUMENT_IR_SCHEMA_VERSION = "document-ir-v3"          # JSON 形式の版（抽出処理の版とは別）


@dataclass
class Source:
    """原本の来歴（相対パス・内容ハッシュ・ファイル種別）。"""
    path: str
    content_hash: str          # "sha256:<hex>"（原本バイトの sha256）
    file_type: str


@dataclass
class Extraction:
    """抽出手法と信頼度（アームの method/confidence と対応）。"""
    method: str
    confidence: float


@dataclass
class Cell:
    """表1セル（原本の行/列位置を保持した位置付きセル）。

    `row`/`column` は結合を解決した後の 1-based グリッド座標。`row_span`/`column_span` は結合の起点セルにだけ
    2 以上が入り、継続セルは要素を作らない（`arms/ooxml_arm.py` の `_docx_table_cells`）。
    `role` は常に `"unknown"`（ヘッダ判定は検索用表現の生成側）。
    """
    row: int
    column: int
    text: str
    row_span: int = 1
    column_span: int = 1
    role: str = "unknown"


@dataclass
class Element:
    """1構造要素（段落/見出し/表/スライド/shape 等）。`text`/`cells` は要素型に応じ一方のみで可（他方は None）。

    表要素（`type="table"`）は行の子要素を持たず、`cells`（位置付きセル配列）だけで表現する。

    `visibility_reason` は `visibility="hidden"` の理由（`hidden_run`／`hidden_slide`／`occluded`／
    `off_slide`／`hidden_sheet`／`very_hidden`）だけを持つ。要素全体の取り消し線は可視性ではないので
    `source_map["format_state"]={"strike": "whole"}` に置く。前面テキストによる上書き（`covered_by_text`）も `source_map` に置く。
    """
    element_id: str
    type: str                              # "paragraph" | "heading" | "table" | "slide" | "shape" | "notes" | ...
    parent_id: str | None
    order: int
    visibility: str
    status: str
    text: str | None
    cells: list[Cell] | None
    source_map: dict
    extraction: Extraction
    visibility_reason: str | None = None


@dataclass
class DocumentIR:
    """1文書の標準構造。

    `picture_count` は文書全体の画像総数（0＝画像なしまたは未計測）。xlsx は各 `sheet` 要素の
    `source_map["picture_count"]` を使う（`human_md.render_xlsx`）。
    """
    schema_version: str
    doc_id: str
    source: Source
    elements: list[Element] = field(default_factory=list)
    picture_count: int = 0


def _plain(value, names: tuple[str, ...]) -> dict:
    """平坦な dataclass を `asdict` と同じ dict にする（フィールド以外の属性が付いていれば名前で拾い直す）。"""
    payload = dict(value.__dict__)
    if len(payload) != len(names):
        payload = {name: getattr(value, name) for name in names}
    return payload


def _payload(ir: DocumentIR) -> dict:
    """`asdict(ir)` と同じ内容の dict。セル配列が大きいので、入れ子を 1 件ずつ再帰して複製することを避ける。"""
    cell_names = tuple(f.name for f in fields(Cell))
    element_names = tuple(f.name for f in fields(Element))
    elements = []
    for element in ir.elements:
        item = _plain(element, element_names)
        if isinstance(element.cells, list):
            item["cells"] = [_plain(cell, cell_names) for cell in element.cells]
        item["extraction"] = asdict(element.extraction)
        elements.append(item)
    payload = _plain(ir, tuple(f.name for f in fields(DocumentIR)))
    payload["source"] = asdict(ir.source)
    payload["elements"] = elements
    return payload


def to_json_str(ir: DocumentIR) -> str:
    """決定的に JSON 化する（キー順固定・タイムスタンプ無し）。"""
    try:
        payload = _payload(ir)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    except TypeError:                                  # 入れ子の dataclass など、dict への展開が要る値は asdict で変換する
        return json.dumps(asdict(ir), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
