"""文書標準構造（document-ir-v1）の型と直列化（起案 docs/archive/proposals/2026-07-20-調査型RAG詳細修正計画.html §6.1・
外部レビュー確定の契約修正＝DOC-IR-001.5）。

**MD 出力と並行して生成する構造化表現**。MD が正のまま（検索/ES/grep/台帳/削除経路は不変）＝本モジュールは
`DocumentIR` を組み立てる純粋なデータ型と決定的 JSON 直列化だけを持つ（取り込み・書き出しは各アーム／
`office_md.py` 側の責務）。

`world_id` は今回含めない（取込側の責務・将来対応）。`doc_id` はアーム段階では空文字（`""`）で構わない＝
確定は取り込み側（`office_md.build_derived`）が原本相対パス（`rel`）を設定してから行う。

`element_id`（`para:N`／`heading:N`／`table:N` 等）は**文書版内で決定的な要素ID**（World 再構築内の識別子）
であり、原本へ要素を前方追加すると後続の連番はずれる＝**版をまたぐ安定IDではない**（採番規則の詳細は各
アーム実装＝`arms/ooxml_arm.py` の docstring を参照）。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

DOCUMENT_IR_SCHEMA_VERSION = "document-ir-v3"          # JSON 形式そのものの版（抽出処理の版とは分離＝DOC-IR-001.5）
# v1→v2（DOC-IR-003・提案書 §7 フェーズ2「PowerPoint」）: `Element.visibility_reason` を追加。JSON 形状が
# 変わるため版分離の規律（外部レビュー#2）に従い bump する＝「現在の（pptx）覆い判定を標準構造の
# visibility と visibility_reason へ移す」という設計方針の反映（DOCX 側の hidden_text にも同時に設定）。
# v2→v3（HM1 の docx 見送り分・2026-09-03）: `DocumentIR.picture_count` を追加（docx は xlsx の
# シート単位 `Element.source_map["picture_count"]` に相当する「シート」概念を持たないため、文書全体の
# 画像枚数を DocumentIR 直下に持つ＝`human_md.render_docx` の画像存在注記が読む）。xlsx/pptx は既定0のまま
# （xlsx は従来どおり sheet 要素の source_map を使う）。


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
    """表1セル（原本の行/列位置を保持した位置付きセル・DOC-IR-001.5・修正1）。

    `row`/`column` は 1-based の実グリッド座標（`w:gridSpan`/`w:vMerge` 等の結合を解決した後の位置）。
    `row_span`/`column_span` は結合の**起点セル**にのみ 2 以上が入る（結合の継続セル＝内容を持たない
    merged 継続セルは要素自体を作らない＝起点セルの span で表現する。採番規則は `arms/ooxml_arm.py` の
    `_docx_table_cells` docstring 参照）。`role` は意味付け（ヘッダ/データ等）。**IR は原本忠実**＝常に
    `"unknown"`（ヘッダ判定は検索用表現生成側＝`table_semantics.py`＝RAG-REP-001 の責務）。
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

    表要素（`type="table"`）は行ごとの子要素を持たず、`cells`（位置付きセル配列）だけで表現する
    （DOC-IR-001.5・修正1＝旧 `type="table_row"`／`values: dict` は撤去）。

    `visibility_reason`（document-ir-v2・DOC-IR-003、L3で意味を拡張）: 大半は `visibility="hidden"` の理由
    （オープン語彙・現状値は `"hidden_run"`（Word の `w:vanish`）／`"hidden_slide"`（PowerPoint の非表示
    スライド）／`"occluded"`（前面の無地図形/画像に覆われた・pptx A5 判定の構造化）／`"off_slide"`
    （スライド外配置）／`"hidden_sheet"`/`"very_hidden"`（Excel の非表示/完全非表示シート））。
    **例外が1つだけある**: `"strike"`（取り消し線・`w:strike`/`w:dstrike`／xlsx `font.strike`）は
    `visibility="visible"` のまま設定する——取り消し線は隠し文字と異なり本文が読める状態のまま
    （画面上は見えている）ため、`visibility="hidden"` にすると事実と異なる断定になる。それ以外の
    reason は引き続き `visibility="hidden"` とセットで使うこと。前面の**テキスト**による上書き
    （`covered_by_text`）は `visibility_reason` を使わず `source_map` 側に置く（意味の確定は検索表現層の
    責務・`"strike"` と混同しないよう別キーに分離）。
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
    """1文書の標準構造（document-ir-v1）。

    `picture_count`（v3・HM1）: 文書全体に含まれる画像の総数（0＝画像なし、または未計測のファイル種別）。
    docx はシート等の中間スコープを持たないため文書直下に置く（xlsx は代わりに各 `sheet` 要素の
    `source_map["picture_count"]` を使う＝`human_md.render_xlsx` 参照）。
    """
    schema_version: str
    doc_id: str
    source: Source
    elements: list[Element] = field(default_factory=list)
    picture_count: int = 0


def to_json_str(ir: DocumentIR) -> str:
    """決定的直列化（キー順固定・タイムスタンプ無し）。`office_md._write_provenance` の meta.json と同じ流儀。"""
    return json.dumps(asdict(ir), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def from_json_str(s: str) -> DocumentIR:
    """`to_json_str` の逆変換（round-trip 用）。"""
    return _document_ir_from_dict(json.loads(s))


def _document_ir_from_dict(d: dict) -> DocumentIR:
    source_d = d.get("source") or {}
    source = Source(path=source_d.get("path", ""), content_hash=source_d.get("content_hash", ""),
                     file_type=source_d.get("file_type", ""))
    elements = [_element_from_dict(e) for e in d.get("elements", [])]
    return DocumentIR(schema_version=d.get("schema_version", DOCUMENT_IR_SCHEMA_VERSION),
                       doc_id=d.get("doc_id", ""), source=source, elements=elements,
                       picture_count=d.get("picture_count", 0))


def _element_from_dict(d: dict) -> Element:
    ext_d = d.get("extraction") or {}
    extraction = Extraction(method=ext_d.get("method", ""), confidence=ext_d.get("confidence", 0.0))
    cells_d = d.get("cells")
    cells = [_cell_from_dict(c) for c in cells_d] if cells_d is not None else None
    return Element(element_id=d["element_id"], type=d["type"], parent_id=d.get("parent_id"),
                   order=d.get("order", 0), visibility=d.get("visibility", "visible"),
                   status=d.get("status", "active"), text=d.get("text"), cells=cells,
                   source_map=d.get("source_map") or {}, extraction=extraction,
                   visibility_reason=d.get("visibility_reason"))


def _cell_from_dict(d: dict) -> Cell:
    return Cell(row=d["row"], column=d["column"], text=d.get("text", ""),
                row_span=d.get("row_span", 1), column_span=d.get("column_span", 1),
                role=d.get("role", "unknown"))
