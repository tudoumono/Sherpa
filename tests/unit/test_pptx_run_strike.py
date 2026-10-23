"""PowerPoint の run 単位の取り消し線（直接指定の `a:rPr/@strike`）が、文字の取り出しから索引・人用 Markdown まで残る契約。

手組みの最小 pptx（zipfile で XML を直接書く・架空の文面）で、実 ES・実モデルなしに検証する。
設計: docs/design/rag.md「チャンク化の考え方」
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest

from sherpa import es_index, json_io, worlds
from sherpa.ingest import evidence_render as R
from sherpa.ingest import evidence_spike, office_md
from sherpa.ingest.ooxml import powerpoint
from sherpa.parts.read import tools as read_tools

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_NS = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
)


def _prose(n: int, tag: str = "記述") -> str:
    return "".join(f"{tag}{i:04d}です。" for i in range(n // 9 + 1))[:n]


# ---- 組み立て ----

def _r(text: str, strike: str | None = None) -> str:
    attr = f' strike="{strike}"' if strike else ""
    return f'<a:r><a:rPr lang="ja-JP"{attr}/><a:t>{text}</a:t></a:r>'


def _p(*runs: str) -> str:
    return f"<a:p>{''.join(runs)}</a:p>"


def _sp(shape_id: int, name: str, *paras: str, y: int = 0) -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="0" y="{y}"/><a:ext cx="4000000" cy="2000000"/></a:xfrm></p:spPr>'
            f'<p:txBody>{"".join(paras)}</p:txBody></p:sp>')


def _table(shape_id: int, name: str, rows: list[list[str]], y: int = 0) -> str:
    """セルの中身（`a:p` の並び）を渡す表の `p:graphicFrame`。"""
    trs = "".join("<a:tr>" + "".join(f"<a:tc><a:txBody>{cell}</a:txBody></a:tc>" for cell in row) + "</a:tr>"
                  for row in rows)
    return (f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{shape_id}" name="{name}"/>'
            '<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
            f'<p:xfrm><a:off x="0" y="{y}"/><a:ext cx="4000000" cy="2000000"/></p:xfrm>'
            f'<a:graphic><a:graphicData><a:tbl>{trs}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>')


def _write_pptx(path: Path, *shapes: str) -> Path:
    xml = f'<?xml version="1.0"?><p:sld {_NS}><p:cSld><p:spTree>{"".join(shapes)}</p:spTree></p:cSld></p:sld>'
    info = zipfile.ZipInfo("ppt/slides/slide1.xml", date_time=(2026, 1, 1, 0, 0, 0))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(info, xml)
    return path


def _para(xml_paragraph: str):
    return ET.fromstring(f"<root {_NS}>{xml_paragraph}</root>").find(f"{_A}p")


def _anchored(rendered) -> dict[str, str]:
    bodies, reason = es_index._parse_rag_md_chunks(rendered.markdown)
    assert reason is None
    return bodies


def _shape_texts(rendered) -> list[str]:
    texts = _anchored(rendered)
    return [texts[c["chunk_id"]] for c in rendered.chunks if c["content_type"] == "shape"]


# ---- (1) 純関数: 括りの範囲・状態の読み分け ----

def test_same_word_twice_only_the_struck_one_is_bracketed():
    p = _para(_p(_r("返却期限は7日とする。", "sngStrike"), _r("返却期限は14日とする。")))
    assert powerpoint.stated_text(p) == "【取り消し線：返却期限は7日とする。】返却期限は14日とする。"
    p = _para(_p(_r("返却期限は14日とする。"), _r("返却期限は7日とする。", "dblStrike")))
    assert powerpoint.stated_text(p) == "返却期限は14日とする。【取り消し線：返却期限は7日とする。】"


@pytest.mark.parametrize("strike,bracketed", [("sngStrike", True), ("dblStrike", True), ("noStrike", False), (None, False)])
def test_strike_values_are_read_directly_and_no_strike_is_plain(strike, bracketed):
    p = _para(_p(_r("前。"), _r("旧記述", strike), _r("後。")))
    assert powerpoint.stated_text(p) == ("前。【取り消し線：旧記述】後。" if bracketed else "前。旧記述後。")


def test_consecutive_struck_runs_make_one_bracket_and_edge_spaces_go_outside():
    p = _para(_p(_r("前 "), _r(" 旧A", "sngStrike"), _r("旧B ", "dblStrike"), _r(" 後")))
    assert powerpoint.stated_text(p) == "前  【取り消し線：旧A旧B】  後"
    p = _para(_p(_r("甲", "sngStrike"), _r("乙"), _r("丙", "sngStrike")))
    assert powerpoint.stated_text(p) == "【取り消し線：甲】乙【取り消し線：丙】"


def test_whitespace_only_struck_run_is_not_bracketed():
    p = _para(_p(_r("前。"), _r("  ", "sngStrike"), _r("後。")))
    assert powerpoint.stated_text(p) == "前。  後。"


def test_inherited_strike_is_not_resolved():
    p = _para('<a:p><a:pPr><a:defRPr strike="sngStrike"/></a:pPr><a:r><a:t>継承だけ</a:t></a:r></a:p>')
    assert powerpoint.stated_text(p) == "継承だけ"


def test_text_lines_bracket_struck_runs_per_run_line():
    txbody = ET.fromstring(f"<root {_NS}>" + _p(_r("前。"), _r("旧A", "sngStrike"), _r("旧B", "dblStrike"), _r("後。"))
                           + _p(_r("次の段落")) + "</root>")
    assert powerpoint.text_lines(txbody) == ["前。", "【取り消し線：旧A旧B】", "後。", "次の段落"]


# ---- (2) 取り消し線の無い要素は、a:t を1つずつ並べた行と完全に一致する ----

@pytest.mark.parametrize("xml", [
    _p(_r("甲 "), _r(" 乙"), _r("丙", "noStrike")) + _p(_r("丁")),
    _p(_r("  "), _r("戊")) + "<a:p/>",
    '<a:p><a:pPr><a:defRPr strike="sngStrike"/></a:pPr><a:r><a:t>継承</a:t></a:r><a:fld id="{1}" type="slidenum"><a:t>‹#›</a:t></a:fld></a:p>',
])
def test_text_lines_without_direct_strike_equal_the_flat_a_t_lines(xml):
    el = ET.fromstring(f"<root {_NS}>{xml}</root>")
    assert powerpoint.text_lines(el) == [t.text.strip() for t in el.iter(f"{_A}t") if t.text and t.text.strip()]


# ---- (3) 文字を取り出す全ての所 ----

def test_strike_reaches_shape_group_and_other_texts_in_the_human_markdown(tmp_path):
    group = ('<p:grpSp><p:nvGrpSpPr><p:cNvPr id="9" name="枠"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
             + _sp(10, "内側", _p(_r("内側の旧文", "sngStrike"), _r("内側の新文"))) + "</p:grpSp>")
    cxn = ('<p:cxnSp><p:nvCxnSpPr><p:cNvPr id="11" name="線"/><p:cNvCxnSpPr/><p:nvPr/></p:nvCxnSpPr><p:spPr/>'
           f'<p:txBody>{_p(_r("線の旧文", "dblStrike"))}</p:txBody></p:cxnSp>')
    path = _write_pptx(tmp_path / "a.pptx", _sp(4, "本文", _p(_r("通常"), _r("旧文", "sngStrike"))), group, cxn)
    md = office_md.to_markdown(path)
    for line in ("【取り消し線：旧文】", "【取り消し線：内側の旧文】", "内側の新文", "【取り消し線：線の旧文】"):
        assert line in md, md


def test_slide_shapes_texts_carry_the_bracket_for_shape_group_table_and_other():
    cell = _p(_r("旧セル", "sngStrike"), _r("新セル"))
    shapes = (_sp(4, "本文", _p(_r("旧文", "sngStrike"))) + _table(5, "表", [[cell]])
              + '<p:cxnSp><p:nvCxnSpPr><p:cNvPr id="6" name="線"/><p:cNvCxnSpPr/><p:nvPr/></p:nvCxnSpPr><p:spPr/>'
                f'<p:txBody>{_p(_r("線", "dblStrike"))}</p:txBody></p:cxnSp>')
    root = ET.fromstring(f'<p:sld {_NS}><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>')
    entries = powerpoint.slide_shapes(root)
    assert [e["texts"] for e in entries] == [["【取り消し線：旧文】"], ["【取り消し線：旧セル】", "新セル"], ["【取り消し線：線】"]]


def test_table_cell_strike_is_bracketed_in_evidence_rag_and_human_markdown(tmp_path):
    cell_old = _p(_r("旧", "sngStrike"), _r("新規"))
    path = _write_pptx(tmp_path / "t.pptx", _table(5, "料金表", [[_p(_r("項目")), _p(_r("内容"))],
                                                             [_p(_r("A01")), cell_old]]))
    ir = evidence_spike.extract(path)
    values = [e.value for e in ir.elements if e.type == "cell"]
    assert "【取り消し線：旧】新規" in values
    rendered = R.render(ir, source_name="t.pptx")
    assert any("【取り消し線：旧】新規" in text for text in _anchored(rendered).values())
    assert "【取り消し線：旧】" in office_md.to_markdown(path)


def test_shape_value_read_from_the_original_keeps_the_range_and_the_bracket(tmp_path):
    path = _write_pptx(tmp_path / "a.pptx", _sp(4, "返却期限の条文",
                                                _p(_r("返却期限は7日とする。", "sngStrike"), _r("返却期限は14日とする。"))))
    ir = evidence_spike.extract(path)
    shape = next(e for e in ir.elements if e.type == "shape" and e.extension.get("name") == "返却期限の条文")
    assert shape.value == "【取り消し線：返却期限は7日とする。】返却期限は14日とする。"
    rendered = R.render(ir, source_name="a.pptx")
    assert any("図形: 「【取り消し線：返却期限は7日とする。】返却期限は14日とする。」" in t for t in _shape_texts(rendered))
    assert "【取り消し線：返却期限は7日とする。】" in office_md.to_markdown(path)
    assert "【取り消し線：返却期限は14日" not in office_md.to_markdown(path)


# ---- (4) 1,200 字を超える図形の後半に取り消し線: 分割後の後半の片にも状態が残る ----

def _long_strike_world(monkeypatch, tmp_path, struck_repeat: int = 40):
    root = tmp_path / "kb" / "w1"
    root.mkdir(parents=True)
    monkeypatch.setattr(worlds, "world_dir", lambda w: root if w == "w1" else None)
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    head = _prose(1500, "前半")
    _write_pptx(root / "長文.pptx", _sp(4, "長い条文", _p(_r(head), _r("後半の旧記述" * struck_repeat, "sngStrike"), _r("現行の記述。"))))
    rep = office_md.build_derived(root, worlds.derived_md_dir("w1"))
    assert rep["rag_failed"] == 0, rep
    rag = worlds.derived_rag_dir("w1")
    jsonl, md_path = rag / "長文.pptx.rag_chunks.jsonl", rag / "長文.pptx.rag.md"
    records = [json.loads(line) for line in json_io.read_text_maybe_gzip(jsonl).splitlines()]
    return jsonl, md_path, [r for r in records if r["content_type"] == "shape"]


@pytest.mark.parametrize("struck_repeat", [40, 500])
def test_strike_in_the_second_half_of_a_long_shape_reaches_index_vector_input_evidence_and_human_markdown(
        monkeypatch, tmp_path, struck_repeat):
    jsonl, md_path, records = _long_strike_world(monkeypatch, tmp_path, struck_repeat)
    assert len(records) >= 2
    anchors, reason = es_index._load_rag_md_anchors(md_path)
    assert reason is None
    struck_ids = [r["chunk_id"] for r in records if "旧記述" in r["body"]]
    plain_ids = [r["chunk_id"] for r in records if "旧記述" not in r["body"]]
    assert records[-1]["chunk_id"] in struck_ids and plain_ids and len(struck_ids) >= 1
    # ① 索引へ渡す本文（括りの途中で切れた片は閉じ・開き直す）
    for chunk_id in struck_ids:
        assert "【取り消し線" in anchors[chunk_id]
    assert all("取り消し線" not in anchors[chunk_id] for chunk_id in plain_ids)      # 通常文字だけの片は変わらない
    if struck_repeat > 100:
        assert "【取り消し線（続き）：" in anchors[records[-1]["chunk_id"]]
    # ② 束ねたベクトルの入力
    entries = list(es_index._iter_rag_chunk_entries(jsonl, "長文.pptx", anchors, {"doc_id": "長文.pptx"}))
    vector_inputs = {body["chunk_id"]: text for _i, body, text, skip in entries if not skip}
    for chunk_id in struck_ids:
        assert "【取り消し線" in vector_inputs[chunk_id]
    # ③ 道具が返す根拠本文
    info: dict = {}
    region = read_tools._rag_md_region_text("w1", "長文.pptx", [], None, set(struck_ids), 10 ** 7, info)
    assert region is not None and info["missing_chunks"] == 0
    for chunk_id in struck_ids:
        assert anchors[chunk_id] in region
    # 人用 Markdown（長文でも状態を括りで残す）
    human = (worlds.derived_md_dir("w1") / "長文.pptx.md").read_text(encoding="utf-8")
    assert re.search(r"【取り消し線：(後半の旧記述)+】現行の記述。", human.replace("\n", ""))


# ---- (5) 取り消し線の無いスライドの出力は変わらない（陰性） ----

_GOLDEN_HUMAN = "969da6878262e52dc623e30b66adb23df0de86e50c9b3a28d0af2844e899a6e2"
_GOLDEN_RAG = "7768733de1a05a434d026d8bf9263c817b1d9d884910e1aa6206c6e8bdb11835"


def _plain_slide(tmp_path) -> Path:
    # 図形どうしを重ねない（重なりの注記を出さず、取り消し線の有無だけを比べる）
    shapes = (_sp(4, "見出し", _p(_r("貸出条件の改定案")), _p(_r("返却期限は14日とする。"), _r(" 延長は1回まで。", "noStrike")))
              + _sp(5, "注記", _p(_r("旧と新の対応表は別紙。")), y=2500000)
              + _table(6, "表", [[_p(_r("項目")), _p(_r("内容"))], [_p(_r("A01")), _p(_r("貸出"), _r("可"))]], y=5000000))
    return _write_pptx(tmp_path / "plain.pptx", shapes)


def _rag_fingerprint(path: Path) -> str:
    rendered = R.render(evidence_spike.extract(path), source_name="plain.pptx")
    md = re.sub(r"(?s)\A.*?(?=<!-- chunk:)", "", rendered.markdown)
    md = re.sub(r"<!-- chunk:[^>]*-->\n?", "", md)
    bodies = [c["body"] for c in rendered.chunks]
    return hashlib.sha256(json.dumps([md, bodies], ensure_ascii=False).encode("utf-8")).hexdigest()


def test_slide_without_strike_keeps_rag_and_human_markdown_unchanged(tmp_path):
    path = _plain_slide(tmp_path)
    human = office_md.to_markdown(path)
    assert "取り消し線" not in human and "取り消し線" not in R.render(
        evidence_spike.extract(path), source_name="plain.pptx").markdown
    assert hashlib.sha256(human.encode("utf-8")).hexdigest() == _GOLDEN_HUMAN
    assert _rag_fingerprint(path) == _GOLDEN_RAG
