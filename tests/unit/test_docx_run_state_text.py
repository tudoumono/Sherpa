"""Word 段落の本文に run の状態（取り消し線・非表示）を書くこと、状態行を長文分割の全片へ付けること、
段落の本文にテキストボックスの文字を入れないことを、実ファイルの往復（`office_md.build_derived`）で固定する。

設計: docs/design/rag.md「アーム一覧」
"""
from __future__ import annotations

import json
import os
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import json_io
from sherpa.ingest import office_md
from sherpa.ingest.arms import ooxml_arm

_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:v="urn:schemas-microsoft-com:vml" '
       'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
       'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"')


def _dirs() -> tuple[Path, Path]:
    d = tempfile.mkdtemp()
    src = Path(d) / "src"
    src.mkdir()
    return src, Path(d) / "derived"


def _docx(path: Path, body: str) -> None:
    xml = f'<?xml version="1.0"?><w:document {_NS}><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)


def _chunk_bodies(der: Path, rel: str) -> list[str]:
    text = json_io.read_text_maybe_gzip(der.parent / "rag" / f"{rel}.rag_chunks.jsonl")
    return [json.loads(line)["body"] for line in text.splitlines()]


def _para_texts(path: Path) -> list[str]:
    ir = ooxml_arm._build_docx_ir(path)
    return [e.text for e in ir.elements if e.type == "paragraph"]


def test_strike_run_is_written_with_state_in_body_chunk():
    src, der = _dirs()
    _docx(src / "設定仕様_架空.docx", (
        "<w:p><w:r><w:t>タイムアウトは</w:t></w:r>"
        "<w:r><w:rPr><w:strike/></w:rPr><w:t>30</w:t></w:r>"
        "<w:r><w:rPr><w:strike/></w:rPr><w:t>秒</w:t></w:r>"
        "<w:r><w:t>60秒です。</w:t></w:r></w:p>"))
    rep = office_md.build_derived(src, der)
    assert rep["rag_failed"] == 0, rep
    bodies = _chunk_bodies(der, "設定仕様_架空.docx")
    assert any("タイムアウトは【取り消し線：30秒】60秒です。" in b for b in bodies), bodies
    # 補助要素（strike_text）は残る
    ir = ooxml_arm._build_docx_ir(src / "設定仕様_架空.docx")
    assert [e.text for e in ir.elements if e.type == "strike_text"] == ["30", "秒"]


def test_double_strike_and_hidden_runs_are_written_with_state():
    src, der = _dirs()
    path = src / "設定仕様_架空.docx"
    _docx(path, (
        "<w:p><w:r><w:t>上限は</w:t></w:r>"
        "<w:r><w:rPr><w:dstrike/></w:rPr><w:t>10件</w:t></w:r>"
        "<w:r><w:t>、</w:t></w:r>"
        "<w:r><w:rPr><w:vanish/></w:rPr><w:t>内部メモ</w:t></w:r>"
        "<w:r><w:rPr><w:strike w:val=\"0\"/></w:rPr><w:t>です。</w:t></w:r></w:p>"))
    assert _para_texts(path) == ["上限は【取り消し線：10件】、【非表示：内部メモ】です。"]
    ir = ooxml_arm._build_docx_ir(path)
    assert [e.text for e in ir.elements if e.type == "hidden_text"] == ["内部メモ"]


def test_run_state_marker_keeps_surrounding_spaces_outside():
    src, _der = _dirs()
    path = src / "a.docx"
    _docx(path, ('<w:p><w:r><w:t>A</w:t></w:r>'
                 '<w:r><w:rPr><w:strike/></w:rPr><w:t xml:space="preserve"> 旧値 </w:t></w:r>'
                 '<w:r><w:t>B</w:t></w:r></w:p>'))
    assert _para_texts(path) == ["A 【取り消し線：旧値】 B"]


def test_paragraph_without_state_is_unchanged():
    src, _der = _dirs()
    path = src / "a.docx"
    _docx(path, "<w:p><w:r><w:t>通常の</w:t></w:r><w:r><w:t>本文です。</w:t></w:r></w:p>")
    assert _para_texts(path) == ["通常の本文です。"]


def test_hidden_slide_long_text_keeps_visibility_line_on_every_chunk():
    src, der = _dirs()
    long_sentence = "あ" * 1300
    with zipfile.ZipFile(src / "deck.pptx", "w") as z:
        z.writestr("ppt/slides/slide1.xml", (
            '<?xml version="1.0"?>'
            '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
            ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" show="0">'
            "<p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id=\"2\" name=\"Text 1\"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
            "<p:spPr/><p:txBody><a:bodyPr/>"
            f"<a:p><a:r><a:t>{long_sentence}</a:t></a:r></a:p>"
            "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"))
    rep = office_md.build_derived(src, der)
    assert rep["rag_failed"] == 0, rep
    md = (der.parent / "rag" / "deck.pptx.rag.md").read_text(encoding="utf-8")
    pieces = [part for part in md.split("<!-- chunk:")[1:] if "あああ" in part]
    assert len(pieces) >= 2, md
    for part in pieces:
        assert "可視性: 「非表示スライド内の要素」" in part


def test_vml_textbox_text_is_not_in_host_paragraph_body_but_in_textbox_element():
    src, der = _dirs()
    path = src / "メモ_架空.docx"
    _docx(path, (
        "<w:p><w:r><w:t>架空本文A。</w:t></w:r>"
        "<w:r><w:pict><v:shape><v:textbox><w:txbxContent>"
        "<w:p><w:r><w:t>架空注記B。</w:t></w:r></w:p>"
        "</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>"))
    ir = ooxml_arm._build_docx_ir(path)
    assert [e.text for e in ir.elements if e.type == "paragraph"] == ["架空本文A。"]
    assert [e.text for e in ir.elements if e.type == "textbox"] == ["架空注記B。"]
    rep = office_md.build_derived(src, der)
    assert rep["rag_failed"] == 0, rep
    bodies = _chunk_bodies(der, "メモ_架空.docx")
    assert any("架空注記B。" in b for b in bodies)
    assert not any("架空本文A。架空注記B。" in b or "本文: 「架空本文A。架空注記B。" in b for b in bodies)


def test_empty_body_paragraph_with_anchored_textbox_keeps_floating_anchor_fact():
    src, der = _dirs()
    path = src / "帳票_架空.docx"
    _docx(path, (
        "<w:p><w:r><w:drawing>"
        '<wp:anchor behindDoc="0"><wp:docPr id="1" name="注記枠"/>'
        "<a:graphic><a:graphicData><wps:wsp><wps:txbx><w:txbxContent>"
        "<w:p><w:r><w:t>架空注記B。</w:t></w:r></w:p>"
        "</w:txbxContent></wps:txbx></wps:wsp></a:graphicData></a:graphic>"
        "</wp:anchor></w:drawing></w:r></w:p>"))
    ir = ooxml_arm._build_docx_ir(path)
    assert [e for e in ir.elements if e.type in {"paragraph", "heading"}] == []
    boxes = [e for e in ir.elements if e.type == "textbox"]
    assert [e.text for e in boxes] == ["架空注記B。"]
    assert boxes[0].source_map["paragraph_index"] == 0
    assert boxes[0].source_map["floating_anchors"] == [
        {"behind_doc": False, "name": "注記枠", "text": "架空注記B。"}]
    rep = office_md.build_derived(src, der)
    assert rep["rag_failed"] == 0 and rep["evidence_ir_failed"] == 0, rep
    md = (der.parent / "rag" / "帳票_架空.docx.rag.md").read_text(encoding="utf-8")
    assert "前面図形: 「架空注記B。」" in md


def test_empty_body_paragraph_with_anchor_and_hidden_run_keeps_fact_on_first_extra_element():
    src, _der = _dirs()
    path = src / "a.docx"
    _docx(path, (
        '<w:p><w:r><w:drawing><wp:anchor behindDoc="1"><wp:docPr id="1" name="背景枠"/></wp:anchor></w:drawing></w:r>'
        "<w:r><w:rPr><w:vanish/></w:rPr><w:t>  </w:t></w:r></w:p>"))
    ir = ooxml_arm._build_docx_ir(path)
    carriers = [e for e in ir.elements if e.source_map.get("floating_anchors")]
    assert [(e.type, e.source_map["floating_anchors"]) for e in carriers] == [
        ("hidden_text", [{"behind_doc": True, "name": "背景枠"}])]
    path2 = src / "b.docx"
    _docx(path2, (
        '<w:p><w:r><w:drawing><wp:anchor behindDoc="1"><wp:docPr id="1" name="背景枠"/></wp:anchor></w:drawing></w:r>'
        '<w:r><w:rPr><w:vanish/></w:rPr><w:t>非表示のみ</w:t></w:r></w:p>'))
    ir2 = ooxml_arm._build_docx_ir(path2)
    host = [e for e in ir2.elements if e.type == "paragraph"]
    assert host and host[0].source_map["floating_anchors"] == [{"behind_doc": True, "name": "背景枠"}]


def test_strike_run_inside_textbox_is_written_with_state_in_textbox_element_and_rag_md():
    src, der = _dirs()
    path = src / "メモ_架空.docx"
    _docx(path, (
        "<w:p><w:r><w:t>架空本文A。</w:t></w:r>"
        "<w:r><w:pict><v:shape><v:textbox><w:txbxContent>"
        "<w:p><w:r><w:t>期限は</w:t></w:r>"
        "<w:r><w:rPr><w:strike/></w:rPr><w:t>5日</w:t></w:r>"
        "<w:r><w:t>7日です。</w:t></w:r></w:p>"
        "<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>架空内部メモ</w:t></w:r></w:p>"
        "</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>"))
    ir = ooxml_arm._build_docx_ir(path)
    assert [e.text for e in ir.elements if e.type == "textbox"] == [
        "期限は【取り消し線：5日】7日です。\n【非表示：架空内部メモ】"]
    rep = office_md.build_derived(src, der)
    assert rep["rag_failed"] == 0, rep
    md = (der.parent / "rag" / "メモ_架空.docx.rag.md").read_text(encoding="utf-8")
    assert "期限は【取り消し線：5日】7日です。" in md
    assert "【非表示：架空内部メモ】" in md


def _bracket_pieces(runs: str, rel: str) -> list[str]:
    src, _der = _dirs()
    path = src / rel
    _docx(path, f"<w:p>{runs}</w:p>")
    ir = ooxml_arm._build_docx_ir(path)
    from sherpa.ingest import evidence_render
    exact = next(e.text for e in ir.elements if e.type == "paragraph")
    return exact, [exact[s:e] for s, e, _ in evidence_render._split_exact(exact)], evidence_render


def test_split_does_not_cut_inside_state_bracket_when_it_fits_one_piece():
    runs = (
        f"<w:r><w:t>{'あ' * 1100}</w:t></w:r>"
        f"<w:r><w:rPr><w:strike/></w:rPr><w:t>{'い' * 300}</w:t></w:r>"
        f"<w:r><w:t>{'う' * 200}。</w:t></w:r>")
    exact, parts, er = _bracket_pieces(runs, "a.docx")
    assert len(parts) >= 2
    assert "".join(parts) == exact
    for part in parts:
        assert part.count("【") == part.count("】"), part
    assert any(part.startswith("【取り消し線：") for part in parts)
    assert all(len(part) <= er.MAX_TEXT_SPAN_CHARS for part in parts)


def test_split_closes_and_reopens_state_bracket_longer_than_one_piece():
    runs = (
        f"<w:r><w:t>{'あ' * 100}</w:t></w:r>"
        f"<w:r><w:rPr><w:vanish/><w:strike/></w:rPr><w:t>{'い' * 3000}</w:t></w:r>"
        f"<w:r><w:t>{'う' * 100}</w:t></w:r>")
    exact, _parts, er = _bracket_pieces(runs, "b.docx")
    spans = er._split_exact(exact)
    assert len(spans) >= 3
    # exact は素の切り出し（offset と一致）。表示用だけが括りを閉じ・開き直す。
    assert "".join(part for _s, _e, part in spans) == exact
    shown = [er._continued_part(exact, s, e) for s, e, _ in spans]
    for text in shown:
        assert text.count("【") == text.count("】"), text
    # 括りが1片に収まらないので、括りの手前で切り、括りの中の切れ目は閉じ・開き直す。
    assert "【" not in shown[0]
    assert shown[1].startswith("【非表示・取り消し線：") and shown[1].endswith("】")
    assert shown[2].startswith("【非表示・取り消し線（続き）：")
    assert shown[-1].startswith("【非表示・取り消し線（続き）：") and shown[-1].endswith("う" * 100)


def test_long_paragraph_without_state_bracket_splits_as_before():
    from sherpa.ingest import evidence_render as er
    text = ("あいうえお。" * 400) + "終わり"
    spans = er._split_exact(text)
    assert "".join(part for _s, _e, part in spans) == text
    assert [(s, e) for s, e, _ in spans] == [(0, 1200), (1200, 2400), (2400, len(text))]
    assert all(er._continued_part(text, s, e) == part for s, e, part in spans)


def test_long_state_bracket_is_paired_in_every_rag_md_chunk():
    runs = (
        f"<w:r><w:t>{'あ' * 100}</w:t></w:r>"
        f"<w:r><w:rPr><w:strike/></w:rPr><w:t>{'い' * 3000}</w:t></w:r>"
        f"<w:r><w:t>{'う' * 100}</w:t></w:r>")
    src, der = _dirs()
    _docx(src / "c.docx", f"<w:p>{runs}</w:p>")
    rep = office_md.build_derived(src, der)
    assert rep["rag_failed"] == 0, rep
    md = (der.parent / "rag" / "c.docx.rag.md").read_text(encoding="utf-8")
    pieces = [part for part in md.split("<!-- chunk:")[1:] if "paragraph内容" in part]
    assert len(pieces) >= 3, md
    for part in pieces:
        body = part.split("\n", 1)[1] if "\n" in part else part
        assert body.count("【") == body.count("】"), part
    assert any("【取り消し線（続き）：" in part for part in pieces)


def _strike_para(before: str, struck: str, after: str) -> str:
    return (f"<w:p><w:r><w:t>{before}</w:t></w:r>"
            f"<w:r><w:rPr><w:strike/></w:rPr><w:t>{struck}</w:t></w:r>"
            f"<w:r><w:t>{after}</w:t></w:r></w:p>")


def test_strike_run_in_header_footer_footnote_endnote_comment_is_written_with_state():
    """ヘッダー・フッター・脚注・文末脚注・コメントの取り消し線の run も、本文と同じ書き方（状態つき）で要素と rag.md に入る。"""
    src, der = _dirs()
    path = src / "覚書_架空.docx"
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    _docx(path, "<w:p><w:r><w:t>架空本文。</w:t></w:r></w:p>")
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("word/header1.xml", f'<?xml version="1.0"?><w:hdr {ns}>{_strike_para("版は", "旧", "新です")}</w:hdr>')
        z.writestr("word/footer1.xml", f'<?xml version="1.0"?><w:ftr {ns}>{_strike_para("頁は", "上", "下です")}</w:ftr>')
        z.writestr("word/footnotes.xml", (
            f'<?xml version="1.0"?><w:footnotes {ns}><w:footnote w:id="1">'
            f'{_strike_para("脚注の", "旧語", "新語")}</w:footnote></w:footnotes>'))
        z.writestr("word/endnotes.xml", (
            f'<?xml version="1.0"?><w:endnotes {ns}><w:endnote w:id="1">'
            f'{_strike_para("文末の", "旧注", "新注")}</w:endnote></w:endnotes>'))
        z.writestr("word/comments.xml", (
            f'<?xml version="1.0"?><w:comments {ns}><w:comment w:id="0" w:author="架空太郎" w:date="2026-01-01T00:00:00Z">'
            f'{_strike_para("指摘の", "旧案", "新案")}</w:comment></w:comments>'))
    ir = ooxml_arm._build_docx_ir(path)
    by_type = {t: [e.text for e in ir.elements if e.type == t]
               for t in ("header", "footer", "footnote", "endnote", "comment")}
    assert by_type["header"] == ["版は【取り消し線：旧】新です"]
    assert by_type["footer"] == ["頁は【取り消し線：上】下です"]
    assert by_type["footnote"] == ["脚注の【取り消し線：旧語】新語"]
    assert by_type["endnote"] == ["文末の【取り消し線：旧注】新注"]
    assert by_type["comment"] == ["指摘の【取り消し線：旧案】新案"]
    rep = office_md.build_derived(src, der)
    assert rep["rag_failed"] == 0, rep
    md = (der.parent / "rag" / "覚書_架空.docx.rag.md").read_text(encoding="utf-8")
    for marked in ("版は【取り消し線：旧】新です", "頁は【取り消し線：上】下です", "脚注の【取り消し線：旧語】新語",
                   "文末の【取り消し線：旧注】新注", "指摘の【取り消し線：旧案】新案"):
        assert marked in md, marked


def test_textbox_inside_footnote_and_comment_is_kept_with_state():
    """脚注・コメントの段落の中のテキストボックスの文字も、状態つきで要素に残る（段落の本文だけを取って落とさない）。"""
    src, _der = _dirs()
    path = src / "注記_架空.docx"
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    box = ('<w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml"><v:textbox><w:txbxContent>'
           '<w:p><w:r><w:t>箱の</w:t></w:r><w:r><w:rPr><w:strike/></w:rPr><w:t>旧</w:t></w:r></w:p>'
           '</w:txbxContent></v:textbox></v:shape></w:pict></w:r>')
    _docx(path, "<w:p><w:r><w:t>架空本文。</w:t></w:r></w:p>")
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("word/footnotes.xml", (
            f'<?xml version="1.0"?><w:footnotes {ns}><w:footnote w:id="1">'
            f'<w:p><w:r><w:t>脚注本文</w:t></w:r>{box}</w:p></w:footnote></w:footnotes>'))
        z.writestr("word/comments.xml", (
            f'<?xml version="1.0"?><w:comments {ns}><w:comment w:id="0" w:author="架空太郎" w:date="2026-01-01T00:00:00Z">'
            f'<w:p><w:r><w:t>指摘本文</w:t></w:r>{box}</w:p></w:comment></w:comments>'))
    ir = ooxml_arm._build_docx_ir(path)
    footnote = [e.text for e in ir.elements if e.type == "footnote"]
    comment = [e.text for e in ir.elements if e.type == "comment"]
    assert footnote == ["脚注本文\n箱の【取り消し線：旧】"]
    assert comment == ["指摘本文\n箱の【取り消し線：旧】"]


def test_paragraphs_in_table_and_content_control_inside_footnote_and_comment_are_kept():
    """脚注・コメントの中の表とコンテンツコントロールの段落も、直下の段落と同じく要素の本文に残る。"""
    src, _der = _dirs()
    path = src / "注記表_架空.docx"
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    inner = ('<w:p><w:r><w:t>直下の段落</w:t></w:r></w:p>'
             '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>表の中の</w:t></w:r><w:r><w:rPr><w:strike/></w:rPr><w:t>旧値</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
             '<w:sdt><w:sdtContent><w:p><w:r><w:t>コントロールの中</w:t></w:r></w:p></w:sdtContent></w:sdt>')
    _docx(path, "<w:p><w:r><w:t>架空本文。</w:t></w:r></w:p>")
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("word/footnotes.xml", (
            f'<?xml version="1.0"?><w:footnotes {ns}><w:footnote w:id="1">{inner}</w:footnote></w:footnotes>'))
        z.writestr("word/comments.xml", (
            f'<?xml version="1.0"?><w:comments {ns}><w:comment w:id="0" w:author="架空太郎" w:date="2026-01-01T00:00:00Z">'
            f'{inner}</w:comment></w:comments>'))
    ir = ooxml_arm._build_docx_ir(path)
    expected = "直下の段落\n表の中の【取り消し線：旧値】\nコントロールの中"
    assert [e.text for e in ir.elements if e.type == "footnote"] == [expected]
    assert [e.text for e in ir.elements if e.type == "comment"] == [expected]


def test_textbox_with_alternate_content_is_taken_once_in_body_and_footnote():
    """図形が代替表現（Choice と Fallback）を持つとき、テキストボックスの文字は本文でも脚注の表の中でも1回だけ出る。"""
    src, _der = _dirs()
    path = src / "代替図形_架空.docx"
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    mc = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
    box = '<w:txbxContent><w:p><w:r><w:t>同じ箱</w:t></w:r></w:p></w:txbxContent>'
    shape = (f'<w:r><mc:AlternateContent {mc}><mc:Choice Requires="wps"><w:drawing>{box}</w:drawing></mc:Choice>'
             f'<mc:Fallback><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml"><v:textbox>{box}</v:textbox>'
             '</v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>')
    _docx(path, f"<w:p><w:r><w:t>架空本文。</w:t></w:r>{shape}</w:p>")
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("word/footnotes.xml", (
            f'<?xml version="1.0"?><w:footnotes {ns}><w:footnote w:id="1">'
            f'<w:tbl><w:tr><w:tc><w:p><w:r><w:t>脚注本文</w:t></w:r>{shape}</w:p></w:tc></w:tr></w:tbl>'
            '</w:footnote></w:footnotes>'))
    ir = ooxml_arm._build_docx_ir(path)
    assert [e.text for e in ir.elements if e.type == "textbox"] == ["同じ箱"]
    assert [e.text for e in ir.elements if e.type == "footnote"] == ["脚注本文\n同じ箱"]
