"""図形＋コネクタ→Mermaidフローチャート（L9・R3・
`docs/proposals/2026-09-02-RAG表現の全形式展開と文脈保持.md`§8.4/§8.5）の契約を pin する。

対象: `mermaid_render.py`（純関数のノードID・ラベル優先順位・prst→形状マッピング・未接続コネクタの
注記）と、`evidence_render._flow_diagram_records`（コンテナ単位recordのcitation完全性）。
`evidence_spike.py`側（xlsxコネクタのrelation化・prst抽出）は実データ（fixtures/eval配下の実
xlsx/pptx）で固定する——L1スパイク時点の観測値ではなく、現在のコードで実際に抽出した結果を見る。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pathlib

import pytest

from sherpa.ingest import evidence_ir as IR
from sherpa.ingest import evidence_render, evidence_spike, mermaid_render

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_EXCEL_JA_INPUTS = _ROOT / "fixtures/eval/excel_ja/inputs"
_OFFICE_JA_INPUTS = _ROOT / "fixtures/eval/office_ja/inputs"


def _element(eid: str, etype: str, value, *, object_id=None, sheet="S", order=0, **extension) -> IR.EvidenceElement:
    return IR.EvidenceElement(
        element_id=eid, type=etype, parent_id=None, order=order, value=value,
        locator=IR.Locator(part="xl/drawings/drawing1.xml", sheet=sheet, object_id=object_id),
        coverage_id=f"cov:{eid}", extension=extension,
    )


def _connects(source: IR.EvidenceElement, target: IR.EvidenceElement, connector: IR.EvidenceElement) -> IR.EvidenceRelation:
    return IR.EvidenceRelation(
        relation_id=f"rel:{connector.element_id}", type="connects_to",
        source_id=source.element_id, target_id=target.element_id,
        evidence_ids=[source.element_id, connector.element_id, target.element_id], confidence=1.0,
        extension={"connector_element_id": connector.element_id, "directed": True},
    )


# ---- mermaid_render: 純関数の契約 ---------------------------------------------------------------


def test_render_flowchart_returns_none_without_connectors():
    shape = _element("e1", "shape", "孤立図形", object_id=1)
    assert mermaid_render.render_flowchart([shape], []) is None


def test_node_id_is_object_id_based_and_japanese_safe():
    """ノードIDは表示名（日本語）由来ではなくobject_id由来——excel2mdの`_v14_sanitize_node_id`の
    ようにASCII以外を`_`へ潰す欠陥を踏まない。"""
    start = _element("e-start", "shape", "開始", object_id=3, order=1)
    end = _element("e-end", "shape", "終了", object_id=4, order=2)
    connector = _element("e-conn", "connector", None, object_id=5, order=3)
    relation = _connects(start, end, connector)
    out = mermaid_render.render_flowchart([start, end, connector], [relation])
    assert out is not None
    assert "開始" in out and "終了" in out
    assert "_" not in out.split("\n")[1]     # 日本語ラベルの前段（ノードID）に`_`潰れが無い
    assert "n3" in out and "n4" in out
    assert "n3 --> n4" in out


def test_node_id_deterministic_across_calls():
    start = _element("e-start", "shape", "開始", object_id=3, order=1)
    end = _element("e-end", "shape", "終了", object_id=4, order=2)
    connector = _element("e-conn", "connector", None, object_id=5, order=3)
    relation = _connects(start, end, connector)
    out1 = mermaid_render.render_flowchart([start, end, connector], [relation])
    out2 = mermaid_render.render_flowchart([start, end, connector], [relation])
    assert out1 == out2


def test_node_id_collision_is_disambiguated_deterministically():
    """object_idが偶然重なる場合でも、決定的にsuffixで衝突を回避する（防御的・実データでは
    object_idはdrawing part内で一意だが、入力契約としては保証しない）。"""
    a = _element("e-a", "shape", "A", object_id=1, order=1)
    b = _element("e-b", "shape", "B", object_id=1, order=2)
    connector = _element("e-conn", "connector", None, object_id=2, order=3)
    relation = _connects(a, b, connector)
    out = mermaid_render.render_flowchart([a, b, connector], [relation])
    assert out is not None
    assert "n1(" not in out or out.count("n1[") + out.count("n1(") <= 1
    assert "n1_2" in out


@pytest.mark.parametrize(
    "prst,expected_open,expected_close",
    [
        ("flowChartDecision", "{", "}"),
        ("diamond", "{", "}"),
        ("flowChartTerminator", "([", "])"),
        ("ellipse", "([", "])"),
        ("roundRect", "([", "])"),
        ("flowChartInputOutput", "[/", "/]"),
        ("trapezoid", "[/", "/]"),
        ("flowChartPreparation", "{{", "}}"),
        ("hexagon", "{{", "}}"),
        ("flowChartManualOperation", "[\\", "/]"),
        ("flowChartDocument", ">", "]"),
        ("flowChartConnector", "((", "))"),
        ("customUnknownGeom", "[", "]"),
        (None, "[", "]"),
    ],
)
def test_prst_shape_mapping(prst, expected_open, expected_close):
    """decision（菱形={}）とhexagon（六角形={{}}）は依頼文面の素案（両方{{}}）とは異なり衝突しない
    よう是正して固定する（標準Mermaid flowchart記法）。"""
    kwargs = {"prst": prst} if prst is not None else {}
    node = _element("e1", "shape", "ラベル", object_id=9, order=1, **kwargs)
    other = _element("e2", "shape", "対象", object_id=10, order=2)
    connector = _element("e-conn", "connector", None, object_id=11, order=3)
    relation = _connects(node, other, connector)
    out = mermaid_render.render_flowchart([node, other, connector], [relation])
    assert out is not None
    node_line = next(line for line in out.splitlines() if line.startswith("n9"))
    assert node_line == f'n9{expected_open}"ラベル"{expected_close}'


def test_label_priority_own_text_then_nearby_cell_then_name():
    # ①own text
    with_text = _element("e1", "shape", "自図形テキスト", object_id=1, order=1, name="図形名1")
    # ②own textが空 → overlapsで重なるcellの値
    cell = _element("e-cell", "cell", "近傍セル値", object_id=None, order=0)
    without_text = _element("e2", "shape", None, object_id=2, order=2, name="図形名2")
    # ③own textもoverlapsも無い → 図形名
    name_only = _element("e3", "shape", None, object_id=3, order=3, name="図形名3")
    connector1 = _element("c1", "connector", None, object_id=10, order=4)
    connector2 = _element("c2", "connector", None, object_id=11, order=5)
    relations = [
        _connects(with_text, without_text, connector1),
        _connects(without_text, name_only, connector2),
    ]
    overlaps = [IR.EvidenceRelation(
        relation_id="ov1", type="overlaps", source_id=without_text.element_id, target_id=cell.element_id,
        evidence_ids=[without_text.element_id, cell.element_id], confidence=1.0,
    )]
    pool = {e.element_id: e for e in (with_text, without_text, name_only, cell)}
    out = mermaid_render.render_flowchart(
        [with_text, without_text, name_only, connector1, connector2], relations,
        overlaps=overlaps, elements_by_id=pool,
    )
    assert out is not None
    assert '"自図形テキスト"' in out
    assert '"近傍セル値"' in out
    assert '"図形名3"' in out


def test_unconnected_connector_leaves_a_note_not_silently_dropped():
    """未接続コネクタは黙って消えず、`%%`コメント行として残る。"""
    start = _element("e-start", "shape", "開始", object_id=3, order=1)
    end = _element("e-end", "shape", "終了", object_id=4, order=2)
    connected = _element("e-conn", "connector", None, object_id=5, order=3, name="接続コネクタ")
    unconnected = _element("e-orphan", "connector", None, object_id=6, order=4, name="孤立コネクタ")
    relation = _connects(start, end, connected)
    out = mermaid_render.render_flowchart([start, end, connected, unconnected], [relation])
    assert out is not None
    assert "n3 --> n4" in out
    assert "%% 未接続: 孤立コネクタ（object 6）" in out


def test_only_unconnected_connector_still_returns_diagram_with_note_only():
    """コネクタは存在するが1件も解決できない場合（実データ: JPX-012.xlsx）でも`None`にはしない
    ——`flowchart TD`ヘッダ＋注記だけの結果を返す（黙って消えない）。"""
    unconnected = _element("e-orphan", "connector", None, object_id=2, order=1, name="直線 1")
    out = mermaid_render.render_flowchart([unconnected], [])
    assert out is not None
    assert out.startswith("flowchart TD\n")
    assert "%% 未接続: 直線 1（object 2）" in out
    assert "-->" not in out


def test_escape_label_neutralizes_mermaid_shape_delimiters():
    raw = 'a[b]{c}|d"e\'f\ng'
    escaped = mermaid_render.escape_label(raw)
    for char in "[]{}|\"'":
        assert char not in escaped
    assert "\n" not in escaped     # 改行は空白へ畳む（1行ラベル）


# ---- evidence_render: citation完全性・レコード生成 ------------------------------------------------


def test_flow_diagram_record_citation_completeness_synthetic():
    """図を構成する全ノード/コネクタのevidence_idがcitationに含まれる（過去のpptx束ね事故の再発防止）。

    `_Builder`（`evidence_spike.py`の内部組み立てヘルパー）経由で組み立てる——`add_element`が
    coverageも同時に登録するため、手組みの`EvidenceElement`より`evidence_ir.validation_errors`の
    参照整合性（coverage_id）を壊しにくい。
    """
    builder = evidence_spike._Builder(IR.EvidenceSource(file_type="xlsx", content_hash="sha256:" + "0" * 64))
    sheet_id = builder.add_element(
        "sheet", IR.Locator(part="xl/workbook.xml", sheet="処理フロー", object_id="sheet:1"),
        parent_id=None, order=1, value=None,
    )
    start_id = builder.add_element(
        "shape", IR.Locator(part="xl/drawings/drawing1.xml", sheet="処理フロー", object_id=3),
        parent_id=sheet_id, order=2, value="開始",
    )
    end_id = builder.add_element(
        "shape", IR.Locator(part="xl/drawings/drawing1.xml", sheet="処理フロー", object_id=4),
        parent_id=sheet_id, order=3, value="終了",
    )
    connector_id = builder.add_element(
        "connector", IR.Locator(part="xl/drawings/drawing1.xml", sheet="処理フロー", object_id=5),
        parent_id=sheet_id, order=4, value=None,
    )
    builder.add_relation(
        "connects_to", start_id, end_id,
        evidence_ids=[start_id, connector_id, end_id],
        extension={"connector_element_id": connector_id, "directed": True},
    )
    ir = builder.ir
    result = evidence_render.render(ir, source_name="synthetic.xlsx")
    errors = evidence_render.validation_errors(ir, result)
    assert errors == []
    flow_chunks = [chunk for chunk in result.chunks if chunk["content_type"] == "flow_diagram"]
    assert len(flow_chunks) == 1
    cited_ids = {c["evidence_id"] for c in flow_chunks[0]["citations"]}
    assert cited_ids == {start_id, end_id, connector_id}
    assert "```mermaid" in result.markdown


def test_flow_diagram_body_starts_with_dedicated_marker():
    """llm_render.pyの`_is_ai_observation_body`と同型の本文マーカー方式に接続できる形——
    本文の先頭行が固定文言で始まる（llm_render.py側の配線はL9のスコープ外・残課題として報告）。"""
    assert evidence_render.FLOW_DIAGRAM_BODY_MARKER == "フロー図（機械生成・Mermaid）"


# ---- evidence_spike: 実データでの固定（xlsx側の対称化・prst抽出） --------------------------------


@pytest.mark.parametrize("filename,sheet,expected_edges", [
    ("JPX-014.xlsx", "処理フロー", [("顧客ID入力", "顧客登録処理")]),
    ("JPX-021.xlsx", "統合設計", [("EVT-EXEC", "ERR-021"), ("SCR-ONE-01 振込承認入力", "SCR-ONE-02 振込内容確認")]),
])
def test_xlsx_connector_resolves_to_connects_to_relation_real_data(filename, sheet, expected_edges):
    """xlsxコネクタのrelation化（pptxとの対称化）。実データで確認した現在の抽出結果を固定する。"""
    ir = evidence_spike.extract(_EXCEL_JA_INPUTS / filename)
    by_id = {e.element_id: e for e in ir.elements}
    relations = [r for r in ir.relations if r.type == "connects_to"]
    edges = sorted(
        (by_id[r.source_id].value, by_id[r.target_id].value)
        for r in relations
        if by_id[r.source_id].locator.sheet == sheet
    )
    assert edges == sorted(expected_edges)
    for relation in relations:
        assert relation.extension.get("directed") is True
        assert isinstance(relation.extension.get("connector_element_id"), str)


def test_xlsx_connector_with_missing_endpoints_stays_unresolved_real_data():
    """JPX-012.xlsx: コネクタは存在するが`stCxn`/`endCxn`が無く、`connects_to`関係は作られない
    （端点情報自体はextensionに残る＝黙って消えない）。"""
    ir = evidence_spike.extract(_EXCEL_JA_INPUTS / "JPX-012.xlsx")
    connectors = [e for e in ir.elements if e.type == "connector"]
    assert len(connectors) == 1
    assert connectors[0].extension.get("start_object_id") is None
    assert connectors[0].extension.get("end_object_id") is None
    assert not [r for r in ir.relations if r.type == "connects_to"]


def test_xlsx_prst_extraction_real_data():
    ir = evidence_spike.extract(_EXCEL_JA_INPUTS / "JPX-014.xlsx")
    shapes = {e.value: e.extension.get("prst") for e in ir.elements if e.type == "shape"}
    assert shapes["顧客ID入力"] == "roundRect"
    assert shapes["顧客登録処理"] == "roundRect"


def test_pptx_prst_extraction_real_data():
    ir = evidence_spike.extract(_OFFICE_JA_INPUTS / "OJA-PPTX-HARD.pptx")
    shapes = [e for e in ir.elements if e.type == "shape" and e.value == "A01 入力検証"]
    assert shapes and shapes[0].extension.get("prst") == "rect"


def test_pptx_connector_resolves_to_connects_to_relation_real_data():
    ir = evidence_spike.extract(_OFFICE_JA_INPUTS / "OJA-PPTX-HARD.pptx")
    relations = [r for r in ir.relations if r.type == "connects_to"]
    assert len(relations) == 1
    by_id = {e.element_id: e for e in ir.elements}
    assert by_id[relations[0].source_id].value == "A01 入力検証"
    assert by_id[relations[0].target_id].value == "A02 更新確定"


# ---- コンテナにコネクタが無ければ図は生成されない（実データ・end-to-end） --------------------------


def test_no_flow_diagram_when_container_has_no_connector_real_data():
    ir = evidence_spike.extract(_EXCEL_JA_INPUTS / "JPX-001.xlsx")
    assert not [e for e in ir.elements if e.type == "connector"]
    result = evidence_render.render(ir, source_name="JPX-001.xlsx")
    assert not [c for c in result.chunks if c["content_type"] == "flow_diagram"]
    assert evidence_render.validation_errors(ir, result) == []


@pytest.mark.parametrize("filename", ["JPX-014.xlsx", "JPX-021.xlsx", "JPX-012.xlsx"])
def test_flow_diagram_end_to_end_real_data_no_validation_errors(filename):
    """実xlsxのextract→render往復で、フロー図recordを含めcitation完全性が破れないことを固定する。"""
    ir = evidence_spike.extract(_EXCEL_JA_INPUTS / filename)
    result = evidence_render.render(ir, source_name=filename)
    assert evidence_render.validation_errors(ir, result) == []
    flow_chunks = [c for c in result.chunks if c["content_type"] == "flow_diagram"]
    assert len(flow_chunks) == 1
    assert flow_chunks[0]["citations"]


def test_flow_diagram_end_to_end_real_pptx_no_validation_errors():
    ir = evidence_spike.extract(_OFFICE_JA_INPUTS / "OJA-PPTX-HARD.pptx")
    result = evidence_render.render(ir, source_name="OJA-PPTX-HARD.pptx")
    assert evidence_render.validation_errors(ir, result) == []
    flow_chunks = [c for c in result.chunks if c["content_type"] == "flow_diagram"]
    assert len(flow_chunks) == 1
    assert "```mermaid" in result.markdown
    assert "flowchart TD" in result.markdown
