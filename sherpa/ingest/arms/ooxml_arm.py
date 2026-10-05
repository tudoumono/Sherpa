"""OOXML アーム（docx/pptx/xlsx を扱う既定アーム・値の権威）。受理判定と来歴メタ（method/confidence/notes）の付与を担い、変換ロジックは `office_md`／本モジュール（決定的・LLM 不使用）にある。

- docx/xlsx: `word/document.xml`／`excel.py` を歩いて document-ir（`_build_docx_ir`／`_build_xlsx_ir`）を1回だけ構築し、人間向け MD（`human_md.render_docx`/`render_xlsx`）と `ArmResult.document` の両方に使い回す。IR 構築に失敗すれば未対応（fail-safe）。
- pptx: `office_md.to_markdown` へ委譲して MD を作り、document-ir（`_build_pptx_ir`）は MD 生成とは独立に並行構築する。
抽出は共通生抽出層（`sherpa/ingest/ooxml/word.py`・`powerpoint.py`・`excel.py`）の純関数を消費する。
設計: docs/design/rag.md「アーム一覧」
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from . import ArmResult
from .. import document_ir

# OOXML＝外部ライブラリ不要（docx/pptx は XML 直読み・xlsx は openpyxl）で常時 MD化できる。
_EXTS = {".docx", ".xlsx", ".pptx"}

# 浮動図形（`wp:anchor`）の関連付け（`_docx_floating_anchor_facts`）用の名前空間。
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
# 画像本体（`pic:pic`・`_docx_picture_count`）の名前空間。インライン/浮動のいずれの配置でもこのタグで現れる。
_PIC = "{http://schemas.openxmlformats.org/drawingml/2006/picture}"

# MS-OFFCRYPTO で暗号化された OOXML は OLE2/CFB コンテナに包まれるが、OLE2 は旧 Office バイナリ（非暗号化）とも共通なので、magic bytes だけでは暗号化と確定できない。`olefile` で `EncryptionInfo` ストリームの有無まで確認する。


def _looks_password_protected(p: Path) -> bool:
    """OLE2/CFB コンテナ内に `EncryptionInfo` ストリームがあるか（`olefile` で確認）。`olefile` 未導入/読めない/OLE2 でない場合は判定不能＝False。"""
    try:
        import olefile
    except ImportError:
        return False
    try:
        if not olefile.isOleFile(str(p)):
            return False
        with olefile.OleFileIO(str(p)) as ole:
            return ole.exists("EncryptionInfo")
    except Exception:
        return False


def _document_ir_failure_detail(p: Path, exc: Exception) -> str:
    """`document_ir_failed:<detail>` の detail を決める（閉じた語彙 `sherpa.ingest.failure_reasons` 準拠）。

    `EncryptionInfo` ストリームがある OLE2/CFB＝パスワード保護、zip/XML の構造破損（`BadZipFile`／XML `ParseError`／要素欠落の `KeyError`）＝ファイル破損。`MemoryError` は入力上限超過と確認できないため `other`。それ以外は例外クラス名のまま（`failure_reasons.classify` が `other` に分類する）。
    """
    if _looks_password_protected(p):
        return "password_protected"
    if isinstance(exc, (zipfile.BadZipFile, ET.ParseError, KeyError)):
        return "malformed_structure"
    return exc.__class__.__name__


# 抽出不完全の疑い（静かな部分抽出の検知）。宣言行数（openpyxl `ws.max_row`）に対して実際に値が入っていた行が極端に少なく、かつ最終非空行が宣言終端の手前で途切れていれば疑いに計上する（cap/予算打ち切りの自己申告と、最終非空行が宣言終端に達している疎シートは除く）。
_PARTIAL_XLSX_MIN_DECLARED_ROWS = 100  # これ未満の宣言行数は比率のブレが大きいため対象外
_PARTIAL_XLSX_MAX_EXTRACTED_RATIO = 0.05  # 抽出行/宣言行がこの比率未満なら疑い

# document-ir の JSON 形式の版（`document_ir.DOCUMENT_IR_SCHEMA_VERSION`）と、このアームの抽出処理自体の版を分離する。抽出対象（要素種別・採番規則・表の座標解決規則等）を増やしたらこちらを上げる（派生が `.document_ir_sig` の drift 経由で再生成される）。
DOCX_EXTRACTOR_VERSION = "docx-ooxml-v5"
# PowerPoint 抽出器の版。docx/xlsx とは独立した版番号（`_current_document_ir_sig` が `docx=`/`pptx=`/`xlsx=` を別成分で持つ）。
PPTX_EXTRACTOR_VERSION = "pptx-ooxml-v2"
# Excel 抽出器の版。docx/pptx とは独立した版番号。座標解決・要素の抽出規則が変わったら上げる。この値は `_current_document_ir_sig`/`_current_evidence_ir_sig` の `xlsx=` 成分に入る。
# 上げると `worker.sync`（`_refresh_derived_representations`）が `document_ir_sig_drift` を検知し、`refresh_document_ir` が document.json／evidence／rag（RAG_ES 有効時は ES 索引）まで連鎖再生成する。人間向け `{rel}.md` は別系統（`office_md._current_human_md_sig()`）だが、この版と `DOCX_EXTRACTOR_VERSION` が合成されるため、上げると `{rel}.md` も1回だけ作り直される。
XLSX_EXTRACTOR_VERSION = "xlsx-ooxml-v5"

# document-ir を並行構築する対象拡張子（legacy .xls は対象外＝来歴を汚さないため）。
_IR_EXTS = {".docx", ".pptx", ".xlsx"}


class OoxmlArm:
    """docx/pptx/xlsx を扱う既定アーム（値・構造の権威）。pptx は `office_md.to_markdown` へ委譲（バイト一致）。docx/xlsx は document-ir を構築し、人間向け MD と `ArmResult.document` の両方をそこから作る。"""
    name = "ooxml"

    def accepts(self, path) -> bool:
        return Path(path).suffix.lower() in _EXTS

    def convert(self, path) -> ArmResult | None:
        from .. import office_md
        p = Path(path)
        ext = p.suffix.lower()
        if ext == ".pptx":  # pptx は `office_md.to_markdown` に委譲する
            md = office_md.to_markdown(path)
            if md is None:
                return None
            notes: list[str] = []
            document = None
            try:
                document = _build_pptx_ir(p)
            except Exception as e:  # IR 生成の失敗は握りつぶして md 継続（fail-safe）
                notes.append(f"document_ir_failed:{_document_ir_failure_detail(p, e)}")
            return ArmResult(md=md, method="ooxml", confidence=1.0, notes=notes, document=document)
        if ext not in _EXTS:
            return None
        # docx/xlsx: document-ir を1回だけ構築し、人間向け MD 生成と `ArmResult.document` に使い回す（独立に行うと xlsx の2回ロードを倍払いする）。IR 構築に失敗すれば未対応（MD だけ生き残る経路は無い）。
        notes = []
        try:
            document = _build_docx_ir(p) if ext == ".docx" else _build_xlsx_ir(p)
        except Exception as e:
            notes.append(f"document_ir_failed:{_document_ir_failure_detail(p, e)}")
            document = None
        if document is None:
            # fail-closed: bare None ではなく notes 付きの `ArmResult` を返す。bare None だと失敗の詳細が失われ、`document_ir_failed` を計上できないまま `.document_ir_sig` 等の版マーカーが「全件成功」として確定してしまう（次回 sync の drift 検知が働かなくなる）。
            if not notes:  # 例外は起きず構造的に None（本文/シート欠落等）
                notes.append("document_ir_failed:malformed_structure")
            return ArmResult(md=None, method="ooxml", confidence=0.0, notes=notes, document=None)
        if ext == ".xlsx":
            # 未計算式（`has_cached_value=False`）が1件でもあれば警告を来歴に残す（`_build_xlsx_ir` の戻り値契約を保つため、ここで要素を走査して数える）。
            uncached = sum(1 for e in document.elements
                           if e.type == "formula" and e.source_map.get("has_cached_value") is False)
            if uncached:
                notes.append(f"xlsx_uncached_formulas:{uncached}")
            truncated_sheets = sum(1 for e in document.elements
                                   if e.type == "sheet" and e.source_map.get("truncated"))
            if truncated_sheets:  # cap 打切りの黙認防止（来歴にも残す）
                notes.append(f"xlsx_truncated_sheets:{truncated_sheets}")
        elif ext == ".docx":
            # `_docx_table_walk` が付けた flags（列/行 span のクランプ・vMerge 継続セルの本文救済）を持つ表の件数を来歴に残す。
            flagged = [f for e in document.elements if e.type == "table"
                      for f in e.source_map.get("flags", [])]
            for flag_name in sorted(set(flagged)):
                notes.append(f"{flag_name}_tables:{flagged.count(flag_name)}")
        md = office_md._render_human_md(document, p, ext)
        if md is None:
            # IR は構築できたが、レンダラが本文を1つも見つけられなかった場合（docx の空文書等）。失敗ではなく「作れたが空」なので document は残したまま返す（`document_ir_failed` は計上しない）。
            return ArmResult(md=None, method="ooxml", confidence=1.0, notes=notes, document=document)
        return ArmResult(md=md, method="ooxml", confidence=1.0, notes=notes, document=document)


def _docx_floating_anchor_facts(p_el) -> list[dict]:
    """段落 `p_el` 内の浮動図形（`wp:anchor`）を、幾何断定なしの関連付けの事実として返す。

    Word はフロー配置のため、覆われる側の段落の実座標がレイアウト計算前に確定しない。幾何的な覆い判定はせず、「同一アンカー段落に浮動図形がある」事実と、図形の name／テキスト／`behindDoc`（背面か前面か）だけを残す（意味の断定はしない）。インライン図形（`wp:inline`）は対象外。
    """
    from .. import office_md  # 遅延 import（他 `_build_*_ir` と同じ・循環 import 回避）

    facts: list[dict] = []
    for anchor in p_el.findall(f".//{_WP}anchor"):
        doc_pr = anchor.find(f"{_WP}docPr")
        name = doc_pr.get("name", "") if doc_pr is not None else ""
        text = "".join(t.text or "" for t in anchor.iter(f"{office_md._W}t"))
        fact: dict = {"behind_doc": anchor.get("behindDoc") in {"1", "true"}}
        if name:
            fact["name"] = name
        if text:
            fact["text"] = text
        facts.append(fact)
    return facts


def _docx_picture_count(root) -> int:
    """文書本文（`root`＝`word/document.xml` のルート）に含まれる画像（`pic:pic`）の総数。

    インライン/浮動・グループ化図形の中も `.iter()` で数える。図形・チャート・SmartArt は数えない。ヘッダ/フッタ・脚注/コメント等の別パートは対象外。
    """
    return sum(1 for _ in root.iter(f"{_PIC}pic"))


def _build_docx_ir(p: Path) -> document_ir.DocumentIR | None:
    """DOCX から document-ir を構築する。

    新規抽出（MD が表示しない構造）は `sherpa/ingest/ooxml/word.py` の純関数を消費する。表（gridSpan/vMerge/gridBefore の座標解決）は本ファイル内（`_docx_table_walk`）。

    採番規則（World 再構築内で決定的な要素ID。原本に要素を前方追加すると後続の連番はずれる）。型ごとに独立したカウンタ（1-based・実際に要素を生成した時だけ増分）:
    - `para:N`／`heading:N`／`table:N`: 本文。
    - `hidden:N`（隠し文字）／`strike:N`（取り消し線）／`deleted:N`（削除本文）／`link:N`（ハイパーリンク）／`textbox:N`（テキストボックス）: 文書全体を通した連番。`order` は同一段落内の出現順（段落ごとに1から数え直す）で、種別ごとにまとめた固定順（hidden→strike→deleted→hyperlink→textbox）で振る。`strike:N` は `visibility="visible"`・`status="active"` のまま `visibility_reason="strike"` だけを立てる。`parent_id` はホスト段落/見出しの `element_id`。段落全体が削除で本文ゼロの場合は `parent_id=None` とし、`source_map.paragraph_index` が原本位置を示す。
    - `hyperlink` の `target`: `r:id`（`word/_rels/document.xml.rels` 経由の URL）優先、無ければ `w:anchor`（`"#"+anchor名`）。どちらも解決できなければ要素を作らない。
    - `footnote:N`／`endnote:N`／`comment:N`／`header:N`／`footer:N`（`word/header*.xml`／`footer*.xml` は zip 名ソート順）: その型内だけの連番で `order` も同じ値、`parent_id=None`。区切り線・空パートは要素を作らない。パートの欠落/壊れはその型を空扱いにする。
    - ネスト表（`w:tc` 内の `w:tbl`）: 外側表の `cells` は `tc` 直下の `w:p` のみ。ネスト表は独立した `table:N`（トップレベル表と同じグローバル連番）で、`parent_id=<外側 table:N>`・`order=<外側表内での出現順>`・`source_map={"table_index", "host_row", "host_column"}`（`host_row`/`host_column` は直接の親表内での 1-based 位置）。何段でも再帰する。空のネスト表は追加しない。
    """
    from .. import office_md
    from ..ooxml import word
    with zipfile.ZipFile(p) as z:
        root = ET.fromstring(z.read("word/document.xml"))
        rels = word.load_rels(z, "word/document.xml")
        header_names, footer_names = word.header_footer_names(z)
        header_parts = [(name, word.part_paragraphs(z, name)) for name in header_names]
        footer_parts = [(name, word.part_paragraphs(z, name)) for name in footer_names]
        footnote_list = word.footnotes(z)
        endnote_list = word.endnotes(z)
        comment_list = word.comments(z)
    body = root.find(f"{office_md._W}body")
    if body is None:
        return None

    content_hash = "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
    source = document_ir.Source(path=str(p), content_hash=content_hash, file_type="docx")
    elements: list[document_ir.Element] = []

    body_order = 0
    para_seq = heading_seq = table_seq = 0
    hidden_seq = deleted_seq = link_seq = textbox_seq = strike_seq = 0
    para_index = table_index = 0  # 0-based・生の <w:p>/<w:tbl> 出現順（スキップされても増分）

    def _append_paragraph_extras(p_el, host_id, para_idx, cell_map=None) -> None:
        """段落付随要素（hidden/deleted/link/textbox）を出現順（種別ごとにまとめた固定順）で追加する。

        `cell_map` 指定時＝表セル内の段落: `source_map` は `{"table_index", "row", "column", "cell_paragraph_index"}`（row/column は cells と同じグリッド座標）になり、`host_id` にはホスト表の `element_id`（`table:N`）が入る。
        """
        nonlocal hidden_seq, deleted_seq, link_seq, textbox_seq, strike_seq

        def _sm(extra=None) -> dict:
            base = dict(cell_map) if cell_map is not None else {"paragraph_index": para_idx}
            if extra:
                base.update(extra)
            return base

        sub_order = 0
        extraction = document_ir.Extraction(method="ooxml", confidence=1.0)
        for text in word.hidden_runs(p_el):
            sub_order += 1
            hidden_seq += 1
            elements.append(document_ir.Element(
                element_id=f"hidden:{hidden_seq}", type="hidden_text", parent_id=host_id,
                order=sub_order, visibility="hidden", visibility_reason="hidden_run",
                status="active", text=text, cells=None,
                source_map=_sm(), extraction=extraction))
        for text in word.strike_runs(p_el):
            # 取り消し線は隠し文字と違い可視性を変えない。本文に残る「廃止」運用の幾何的事実だけを独立要素にする（意味の断定はしない）。
            sub_order += 1
            strike_seq += 1
            elements.append(document_ir.Element(
                element_id=f"strike:{strike_seq}", type="strike_text", parent_id=host_id,
                order=sub_order, visibility="visible", visibility_reason="strike",
                status="active", text=text, cells=None,
                source_map=_sm(), extraction=extraction))
        for text in word.deleted_runs(p_el):
            sub_order += 1
            deleted_seq += 1
            elements.append(document_ir.Element(
                element_id=f"deleted:{deleted_seq}", type="deleted_text", parent_id=host_id,
                order=sub_order, visibility="visible", status="deleted", text=text, cells=None,
                source_map=_sm(), extraction=extraction))
        for link in word.hyperlinks(p_el, rels):
            sub_order += 1
            link_seq += 1
            elements.append(document_ir.Element(
                element_id=f"link:{link_seq}", type="hyperlink", parent_id=host_id,
                order=sub_order, visibility="visible", status="active", text=link["text"], cells=None,
                source_map=_sm({"target": link["target"]}), extraction=extraction))
        for text in word.textboxes(p_el):
            sub_order += 1
            textbox_seq += 1
            elements.append(document_ir.Element(
                element_id=f"textbox:{textbox_seq}", type="textbox", parent_id=host_id,
                order=sub_order, visibility="visible", status="active", text=text, cells=None,
                source_map=_sm(), extraction=extraction))

    def _append_table(tbl_el, parent_id, order, base_source_map) -> None:
        """1つの `w:tbl`（トップレベル/ネスト共通）を `table:N` として追加し、ネスト表を再帰的に追加する。

        セル内の段落付随要素も `_append_paragraph_extras` の `cell_map` 経由で要素化する。`parent_id` はこの表の `element_id`、座標は cells と同じグリッド位置（継続セルの `w:tc` 内も対象）。
        """
        nonlocal table_seq
        cells, nested, cell_paras, flags = _docx_table_walk(tbl_el)
        table_seq += 1
        tid = f"table:{table_seq}"
        table_sm = dict(base_source_map)
        if flags:  # クランプ/救済の発生を来歴に残す
            table_sm["flags"] = flags
        elements.append(document_ir.Element(
            element_id=tid, type="table", parent_id=parent_id, order=order,
            visibility="visible", status="active", text=None, cells=cells,
            source_map=table_sm,
            extraction=document_ir.Extraction(method="ooxml", confidence=1.0)))
        table_index_value = base_source_map.get("table_index")
        for row, col, p_els in cell_paras:  # セル内段落の付随要素も要素化
            for p_i, p_el in enumerate(p_els):
                _append_paragraph_extras(p_el, tid, None, cell_map={
                    "table_index": table_index_value, "row": row, "column": col,
                    "cell_paragraph_index": p_i})
        for n, (row, col, nested_tbl) in enumerate(nested, start=1):
            if not office_md._table_md(nested_tbl):  # 空のネスト表はトップレベルと同じ基準で出さない
                continue
            _append_table(nested_tbl, parent_id=tid, order=n,
                          base_source_map={"table_index": table_index_value, "host_row": row, "host_column": col})

    for el in body:
        if el.tag == f"{office_md._W}p":
            idx = para_index
            para_index += 1
            text = office_md._para_text(el).strip()
            host_id = None
            if text:
                lvl = office_md._heading_level(el)
                body_order += 1
                extraction = document_ir.Extraction(method="ooxml", confidence=1.0)
                # この段落に浮動図形（`wp:anchor`）がアンカーされていれば、幾何断定なしの関連付けの事実を source_map へ載せる。
                floating_anchors = _docx_floating_anchor_facts(el)
                if lvl:
                    heading_seq += 1
                    host_id = f"heading:{heading_seq}"
                    heading_source_map = {"paragraph_index": idx, "level": lvl}
                    if floating_anchors:
                        heading_source_map["floating_anchors"] = floating_anchors
                    elements.append(document_ir.Element(
                        element_id=host_id, type="heading", parent_id=None,
                        order=body_order, visibility="visible", status="active",
                        text=text, cells=None, source_map=heading_source_map,
                        extraction=extraction))
                else:
                    para_seq += 1
                    host_id = f"para:{para_seq}"
                    para_source_map = {"paragraph_index": idx}
                    if floating_anchors:
                        para_source_map["floating_anchors"] = floating_anchors
                    elements.append(document_ir.Element(
                        element_id=host_id, type="paragraph", parent_id=None,
                        order=body_order, visibility="visible", status="active",
                        text=text, cells=None, source_map=para_source_map,
                        extraction=extraction))
            # host_id は本文が空（段落全体が削除等）なら None のまま、隠し/削除/リンク/テキストボックスは独立に抽出する（`parent_id=None`＋`paragraph_index` で位置を示す）。
            _append_paragraph_extras(el, host_id, idx)
        elif el.tag == f"{office_md._W}tbl":
            idx = table_index
            table_index += 1
            if not office_md._table_md(el):  # MD に出ない表（空）は IR にも出さない
                continue
            body_order += 1
            _append_table(el, parent_id=None, order=body_order, base_source_map={"table_index": idx})

    header_seq = 0
    for name, paras in header_parts:
        text = "\n".join(paras)
        if not text:
            continue
        header_seq += 1
        elements.append(document_ir.Element(
            element_id=f"header:{header_seq}", type="header", parent_id=None, order=header_seq,
            visibility="visible", status="active", text=text, cells=None,
            source_map={"part": Path(name).name},
            extraction=document_ir.Extraction(method="ooxml", confidence=1.0)))

    footer_seq = 0
    for name, paras in footer_parts:
        text = "\n".join(paras)
        if not text:
            continue
        footer_seq += 1
        elements.append(document_ir.Element(
            element_id=f"footer:{footer_seq}", type="footer", parent_id=None, order=footer_seq,
            visibility="visible", status="active", text=text, cells=None,
            source_map={"part": Path(name).name},
            extraction=document_ir.Extraction(method="ooxml", confidence=1.0)))

    footnote_seq = 0
    for note in footnote_list:
        footnote_seq += 1
        elements.append(document_ir.Element(
            element_id=f"footnote:{footnote_seq}", type="footnote", parent_id=None, order=footnote_seq,
            visibility="visible", status="active", text=note["text"], cells=None,
            source_map={"note_id": note["note_id"]},
            extraction=document_ir.Extraction(method="ooxml", confidence=1.0)))

    endnote_seq = 0
    for note in endnote_list:
        endnote_seq += 1
        elements.append(document_ir.Element(
            element_id=f"endnote:{endnote_seq}", type="endnote", parent_id=None, order=endnote_seq,
            visibility="visible", status="active", text=note["text"], cells=None,
            source_map={"note_id": note["note_id"]},
            extraction=document_ir.Extraction(method="ooxml", confidence=1.0)))

    comment_seq = 0
    for c in comment_list:
        comment_seq += 1
        elements.append(document_ir.Element(
            element_id=f"comment:{comment_seq}", type="comment", parent_id=None, order=comment_seq,
            visibility="visible", status="active", text=c["text"], cells=None,
            source_map={"comment_id": c["comment_id"], "author": c["author"], "date": c["date"]},
            extraction=document_ir.Extraction(method="ooxml", confidence=1.0)))

    return document_ir.DocumentIR(schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="",
                                  source=source, elements=elements,
                                  picture_count=_docx_picture_count(root))


def _build_pptx_ir(p: Path) -> document_ir.DocumentIR | None:
    """PPTX から document-ir を構築する。

    幾何・覆い判定は再実装せず、`office_md.py` のヘルパ（`_pptx_bbox`／`_pptx_has_solid_fill`／`_bbox_intersection_ratio`／`_OCCLUSION_RATIO`）と表示順解決（`_slide_order`）を消費する。非表示スライド・発表者ノート・スライドサイズ・shape 分類は `sherpa/ingest/ooxml/powerpoint.py` の純関数を消費する。MD 生成（`_pptx_md`／`_pptx_slide_texts*`）には触れない。

    採番規則（型ごとに独立したカウンタ・1-based・実際に要素を生成した時だけ増分）:
    - `slide:N`（`N`＝`_slide_order` の表示順＝MD の `## スライド N` と同じ番号）。`text=None`・`order=N`・`source_map={"slide": N, "part": <slide パート名>}`。非表示スライド（`p:sld @show="0"`）は `visibility="hidden"`／`visibility_reason="hidden_slide"`。
    - `shape:N`（スライドをまたいだグローバル連番・テキストを持つ shape だけ要素化する）。`parent_id=<所属 slide:N>`・`text=<shape 内テキストの "\\n" 結合>`・`order=<スライド内の出現順>`（スライドごとに1から数え直す）。`source_map={"slide": n, "z_index": i}`（`z_index`＝`powerpoint.slide_shapes()` が返す配列内の 0-based 位置＝z順。テキストの無い occluder 候補にも振られ、`occluded_by` から前面 occluder を指すのに使う）。bbox が取れれば `bounds: [x, y, cx, cy]`（EMU）を追加し、取れなければキーを省略する。グループ内（`p:grpSp` 直下）は幾何判定せず bbox 不明のまま要素化し、`source_map` に `{"group": true}` を追加する（`bounds` は出さない）。
    - 可視性の判定は優先順 off_slide → occluded → covered_by_text で、最初に成立した1つだけを記録する。
      - 画面外: `powerpoint.slide_size()` が取れ、自 bbox がスライド矩形と交差しなければ `visibility="hidden"`／`visibility_reason="off_slide"`。
      - 覆い: 前面の無地塗りの空 shape または画像が `_bbox_intersection_ratio(自bbox, 前面bbox) >= _OCCLUSION_RATIO` なら `visibility="hidden"`／`visibility_reason="occluded"`、`source_map["occluded_by"] = {"kind": "solid_shape"|"picture", "z_index": j}`。MD の `［隠し候補］` と同じ関数呼び出しで判定するので、MD で隠し候補になる shape は IR でも必ず `occluded` になる。
      - 前面文字による上書き: 覆いに該当しない場合のみ、前面にテキストを持つ shape が同じ閾値以上重なっていれば（先着1件）、`visibility="visible"` のまま `source_map["covered_by_text"] = "shape:M"` を追加する。意味の確定は検索表現層の責務で、IR は幾何的な前後関係だけを記録する。
    - `notes:N`（スライドをまたいだグローバル連番・発表者ノートが非空のスライドだけ）: `parent_id=<所属 slide:N>`・`text=<発表者ノート>`・`order=1`・`source_map={"slide": n}`。

    ノートの rels（`powerpoint.notes_for_slide`）が壊れている/欠落している場合は None に縮退する。壊れて読めないスライドパートは、そのスライドの shape 抽出だけをスキップする（`slide:N` は作る）。`_slide_order` が1件も返さなければ None を返す。
    """
    from .. import office_md
    from ..ooxml import powerpoint as pptx_ooxml

    with zipfile.ZipFile(p) as z:
        slide_names = office_md._slide_order(z)
        if not slide_names:
            return None
        hidden_names = pptx_ooxml.hidden_slide_names(z)
        size = pptx_ooxml.slide_size(z)
        slide_data: list[tuple[str, object, tuple[str, str] | None]] = []
        for name in slide_names:
            try:
                root = ET.fromstring(z.read(name))
            except (KeyError, ET.ParseError):
                root = None
            notes_payload = pptx_ooxml.notes_with_part_for_slide(z, name)
            slide_data.append((name, root, notes_payload))

    content_hash = "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
    source = document_ir.Source(path=str(p), content_hash=content_hash, file_type="pptx")
    elements: list[document_ir.Element] = []
    extraction = document_ir.Extraction(method="ooxml", confidence=1.0)
    slide_rect = (0, 0, size[0], size[1]) if size is not None else None

    shape_seq = 0
    notes_seq = 0
    for n, (name, root, notes_payload) in enumerate(slide_data, start=1):
        sid = f"slide:{n}"
        is_hidden_slide = name in hidden_names
        elements.append(document_ir.Element(
            element_id=sid, type="slide", parent_id=None, order=n,
            visibility=("hidden" if is_hidden_slide else "visible"),
            visibility_reason=("hidden_slide" if is_hidden_slide else None),
            status="active", text=None, cells=None,
            source_map={"slide": n, "part": name}, extraction=extraction))

        if root is not None:
            entries = pptx_ooxml.slide_shapes(root)
            # ① このスライド内でテキストを持つ shape へ先に element_id/order を割り当てる（`covered_by_text` は z順で後に処理される前面 shape の element_id を参照するため、組み立て前に採番だけ済ませる）。
            entry_ids: dict[int, str] = {}
            entry_order: dict[int, int] = {}
            local_order = 0
            for i, entry in enumerate(entries):
                if not entry["has_text"]:
                    continue
                local_order += 1
                shape_seq += 1
                entry_ids[i] = f"shape:{shape_seq}"
                entry_order[i] = local_order

            def _own_state(i, entry):
                """entry 自身の隠れ状態（off_slide → occluded の優先順・covered_by_text は含まない）。

                `covered_by_text` の参照可否判定にも使うため、要素組み立てとは独立に前計算する（前面文字 shape 自身が隠れているなら「可視の上書き」ではない）。
                """
                bbox = entry["bbox"]
                if bbox is not None and slide_rect is not None and \
                        office_md._bbox_intersection_ratio(bbox, slide_rect) == 0.0:
                    return "hidden", "off_slide", None
                if bbox is not None:
                    for j in range(i + 1, len(entries)):
                        other = entries[j]
                        if not other["occluder"] or other["bbox"] is None:
                            continue
                        if office_md._bbox_intersection_ratio(bbox, other["bbox"]) >= office_md._OCCLUSION_RATIO:
                            return "hidden", "occluded", {
                                "kind": "picture" if other["kind"] == "pic" else "solid_shape", "z_index": j}
                return "visible", None, None

            states = {i: _own_state(i, e) for i, e in enumerate(entries) if e["has_text"]}
            # ② 実要素を組み立てる（`occluded_by`/`covered_by_text` は①で確定した ID を参照する）。
            for i, entry in enumerate(entries):
                if not entry["has_text"]:
                    continue
                text = "\n".join(entry["texts"])
                sm: dict = {"slide": n, "z_index": i}
                if entry["group"]:
                    sm["group"] = True
                elif entry["bbox"] is not None:
                    x0, y0, x1, y1 = entry["bbox"]
                    sm["bounds"] = [x0, y0, x1 - x0, y1 - y0]

                visibility, reason, occluded_by = states[i]
                if occluded_by is not None:
                    sm["occluded_by"] = occluded_by
                bbox = entry["bbox"]
                if visibility == "visible" and bbox is not None:
                    for j in range(i + 1, len(entries)):
                        other = entries[j]
                        if not other["has_text"] or other["bbox"] is None:
                            continue
                        if states[j][0] != "visible":  # 自身が隠れている前面文字は参照しない
                            continue
                        if office_md._bbox_intersection_ratio(bbox, other["bbox"]) >= office_md._OCCLUSION_RATIO:
                            sm["covered_by_text"] = entry_ids[j]
                            break

                elements.append(document_ir.Element(
                    element_id=entry_ids[i], type="shape", parent_id=sid, order=entry_order[i],
                    visibility=("hidden" if is_hidden_slide else visibility),
                    visibility_reason=("hidden_slide_inherited" if is_hidden_slide else reason), status="active",
                    text=text, cells=None, source_map=sm, extraction=extraction))

        if notes_payload:
            notes_text, notes_part = notes_payload
            notes_seq += 1
            elements.append(document_ir.Element(
                element_id=f"notes:{notes_seq}", type="notes", parent_id=sid, order=1,
                visibility=("hidden" if is_hidden_slide else "visible"),
                visibility_reason=("hidden_slide_inherited" if is_hidden_slide else None),
                status="active", text=notes_text, cells=None,
                source_map={"slide": n, "part": notes_part}, extraction=extraction))

    return document_ir.DocumentIR(schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="",
                                  source=source, elements=elements)


# Word の実仕様上のテーブル最大列数。`w:gridSpan`/`w:trPr/gridBefore` の `w:val` は壊れた/悪意ある OOXML で巨大値を取りうるため、この値でクランプする（`human_md` 側のセル配置・メモリが原本のセル数と無関係に膨張するのを防ぐ）。
_DOCX_MAX_TABLE_COLUMNS = 63


def _docx_table_cells(tbl_el) -> list[document_ir.Cell]:
    """1つの `w:tbl` から位置付きセル配列だけを組む（`_docx_table_walk` の cells 部分）。座標解決規則は `_docx_table_walk` を参照。"""
    return _docx_table_walk(tbl_el)[0]


def _docx_table_walk(tbl_el) -> tuple[list[document_ir.Cell], list[tuple[int, int, object]],
                                      list[tuple[int, int, list]], list[str]]:
    """1つの `w:tbl` から位置付きセル配列と、各セル直下のネスト表の位置を1回の走査で同時に組む。戻り値の4番目 `flags` は、この表で発生した構造的な異常（クランプ・救済）の種別一覧（重複無し・空リスト＝異常無し）。

    - `column`: 行内の `w:tc` を左から歩き、直前までの `column_span` 累計（`w:trPr/gridBefore` があれば列開始をずらす）で実グリッド位置を求める。`column_span` は `w:gridSpan` の `w:val`（無ければ 1）。列位置の開始が `_DOCX_MAX_TABLE_COLUMNS`（63）を超えるセルは表として出さない（`cells`/`active_vmerge`/`nested`/`cell_paras` のいずれにも加えず、`flags` に `"docx_column_overflow_dropped"`）。座標をクランプして他のセルと衝突させることはしない。開始位置は範囲内で `column_span` が63列を超えて伸びるセルは `column_span` だけをクランプする（`flags` に `"docx_column_span_clamped"`）。
    - `row_span`: 列開始位置ごとの進行中の縦マージを `active_vmerge` で追跡する。`w:vMerge w:val="restart"` のセルを起点として登録し、以降の行の同じ列位置の継続セル（`w:vMerge` はあるが `w:val` が無い、または `"continue"`）ごとに起点セルの `row_span` を1つ増やす。継続セルは `cells` に要素を作らない。増分は表の総行数を超えない（超過を検知したら `flags` に `"docx_row_span_clamped"`）。`w:vMerge` の無い通常セル、またはその列位置の `w:tc` が現れなかった行は連鎖を打ち切る。孤児継続セル（起点 restart が無い継続）は通常セルとして `cells` に出す（セルを失わない）。
    - 継続セルが `<w:t>` を持つ不整形な OOXML では、その本文を起点セルの `text` へ改行連結する（`flags` に `"docx_vmerge_text_merged"`）。
    - `role` は全セル `"unknown"`（ヘッダ判定は検索用表現生成層の責務）。
    - `cells` の並びは行→列の走査順（row-major）。
    - ネスト表: 各 `w:tc` 直下の `w:tbl`（1段目のみ。孫以降は呼び出し側 `_append_table` が再帰で渡す）を `(row_idx, col_start, nested_tbl_el)` として `nested` に集める。継続セルの `w:tc` にネスト表がある不整形でも、`w:tc` 自身の row/col で記録する。
    """
    from .. import office_md
    cells: list[document_ir.Cell] = []
    nested: list[tuple[int, int, object]] = []
    # `(row, col, [直下の w:p ...])`＝セル内段落の付随要素抽出用。継続セルの `w:tc` も含める。
    cell_paras: list[tuple[int, int, list]] = []
    flags: list[str] = []
    total_rows = len(tbl_el.findall(f"{office_md._W}tr"))
    active_vmerge: dict[int, document_ir.Cell] = {}  # 列開始位置 -> 進行中の縦マージの起点 Cell
    for row_idx, tr in enumerate(tbl_el.findall(f"{office_md._W}tr"), start=1):
        col = 1
        tr_pr = tr.find(f"{office_md._W}trPr")  # 行頭の省略列（gridBefore）だけ列開始をずらす
        if tr_pr is not None:
            gb = tr_pr.find(f"{office_md._W}gridBefore")
            if gb is not None:
                try:
                    raw_before = max(0, int(gb.get(f"{office_md._W}val", "0")))
                except (TypeError, ValueError):
                    raw_before = 0
                col = 1 + raw_before  # クランプしない（下のセル単位の判定に委ねる）
        seen_cols: set[int] = set()
        for tc in tr.findall(f"{office_md._W}tc"):
            tc_pr = tc.find(f"{office_md._W}tcPr")
            raw_span = 1
            vmerge_val = None
            has_vmerge = False
            if tc_pr is not None:
                grid_span_el = tc_pr.find(f"{office_md._W}gridSpan")
                if grid_span_el is not None:
                    try:
                        raw_span = max(1, int(grid_span_el.get(f"{office_md._W}val", "1")))
                    except (TypeError, ValueError):
                        raw_span = 1
                vmerge_el = tc_pr.find(f"{office_md._W}vMerge")
                if vmerge_el is not None:
                    has_vmerge = True
                    vmerge_val = vmerge_el.get(f"{office_md._W}val")
            col_start = col
            if col_start > _DOCX_MAX_TABLE_COLUMNS:
                # 開始位置そのものが範囲外＝表として出さない（他のセルと座標が衝突しないよう63列へ丸めない）。この tc は cells/active_vmerge/nested/cell_paras のいずれにも加えず、列位置だけ進める。
                if "docx_column_overflow_dropped" not in flags:
                    flags.append("docx_column_overflow_dropped")
                col += raw_span
                continue
            span = raw_span
            if col_start + raw_span - 1 > _DOCX_MAX_TABLE_COLUMNS:
                span = _DOCX_MAX_TABLE_COLUMNS - col_start + 1  # 63列で止まるよう column_span だけ縮める
                if "docx_column_span_clamped" not in flags:
                    flags.append("docx_column_span_clamped")
            seen_cols.add(col_start)
            origin = active_vmerge.get(col_start) if (has_vmerge and vmerge_val != "restart") else None
            if has_vmerge and vmerge_val != "restart" and origin is not None:
                # 継続セル: 起点セルの row_span を伸ばすだけ（表の総行数は超えない）。`active_vmerge` は実座標（63列以内）だけをキーに持つので、1行で複数回加算されることはない。
                if origin.row_span < total_rows:
                    origin.row_span += 1
                elif "docx_row_span_clamped" not in flags:
                    flags.append("docx_row_span_clamped")
                # 継続セルが本文を持つ不整形 OOXML でも、値を起点セルへ改行連結して残す（値を黙って捨てない）。
                cont_text = " ".join(office_md._para_text(pp).strip()
                                     for pp in tc.findall(f"{office_md._W}p")).strip()
                if cont_text:
                    origin.text = f"{origin.text}\n{cont_text}" if origin.text else cont_text
                    if "docx_vmerge_text_merged" not in flags:
                        flags.append("docx_vmerge_text_merged")
            else:
                # 通常セル・restart・孤児継続（起点無しの continue）のいずれもセルとして出す（孤児継続を捨てると本文つきセルが消える）。
                text = " ".join(office_md._para_text(pp).strip()
                                 for pp in tc.findall(f"{office_md._W}p")).strip()
                cell = document_ir.Cell(row=row_idx, column=col_start, text=text,
                                        row_span=1, column_span=span, role="unknown")
                cells.append(cell)
                if has_vmerge and vmerge_val == "restart":
                    active_vmerge[col_start] = cell  # 新しい縦マージの起点として登録
                else:
                    active_vmerge.pop(col_start, None)  # 通常セル＝この列位置の縦マージ連鎖を打ち切る
            for nested_tbl in tc.findall(f"{office_md._W}tbl"):  # 直下のネスト表を位置付きで収集
                nested.append((row_idx, col_start, nested_tbl))
            p_els = tc.findall(f"{office_md._W}p")  # セル内段落（付随要素の抽出対象）
            if p_els:
                cell_paras.append((row_idx, col_start, p_els))
            col += raw_span
        for c in [c for c in active_vmerge if c not in seen_cols]:
            active_vmerge.pop(c, None)  # この行に現れなかった列の連鎖も打ち切る（穴あき対策）
    return cells, nested, cell_paras, flags


def _build_xlsx_ir(p: Path) -> document_ir.DocumentIR | None:
    """XLSX から document-ir を構築する。

    共通生抽出層 `sherpa/ingest/ooxml/excel.py` の純関数を消費する。`office_md._xlsx_md`（人間向け MD）は本関数の戻り値を `human_md.render_xlsx` へ渡して生成する。安全弁は2系統: ① `excel.regions()`/`merged_map` の cap（`excel.DEFAULT_CAP_CELLS` 等・走査自体の頭打ち）、② `human_md` 側の出力バイト予算（`_MAX_HUMAN_MD_BYTES`・総出力サイズの頭打ち）。結合セルの値の複製で出力バイトは走査セル数と別に膨らむため、①だけでは足りない。
    `excel.load_two` が `data_only=True`（表示値）と `False`（数式）で同一ファイルを2回パースする（openpyxl は両方を同時に返す API を持たず、正確な `formula:N` を得るために必要）。値用だけ `read_only=False`（結合セル/非表示行列/ハイパーリンク/コメントの取得に必要）、数式用は `read_only=True`。呼び出し側はシートの実使用範囲と動的 cap の小さい方までしか読まない。

    採番規則（型ごとに独立したグローバルカウンタ・1-based・実際に要素を生成した時だけ増分）:
    - `sheet:N`（`N`＝`wb.worksheets` のブック内シート順）。`text=None`・`order=N`・`source_map={"sheet": <シート名>}`。非表示シートは `visibility="hidden"`／`visibility_reason="hidden_sheet"`/`"very_hidden"`。画像（`xdr:pic`・グループ化図形の中も含む）が1枚以上あるシートは `source_map["picture_count"]` に枚数を追加する（0枚ならキーなし・`excel.picture_counts_by_sheet()`）。図形・チャート・SmartArt は数えない。
    - `table:N`（シートをまたいだグローバル連番・`excel.regions()` が返す領域ごとに1つ）。1シートの1箇所の値クラスタから複数の `table:N` が生まれうる。`parent_id=<所属 sheet:N>`・`order=<シート内の領域出現順>`。`cells`＝矩形内の絶対座標（1-based row/column）を row-major で埋めた `Cell` 配列（空白セルは `text=""`）。他領域が所有するセルは出さない。`excel.merged_map()` の結合 anchor には `row_span`/`column_span` を反映し、非 anchor（継続セル）は出さない。矩形は `excel.expand_regions_for_merges()` で結合 span まで拡張済み。`text` は常に `None`。
      `source_map={"sheet", "range": "A1:C10", "hidden_rows": [...], "hidden_columns": [...], "score": 0.0-1.0}`（`truncated: true`・`split_budget_exhausted: true` は該当時のみ）。`hidden_rows`/`hidden_columns` はこの表の矩形範囲内に絞り込んだ一覧。`score` は `Region.score` のまま。分類・抑制はしない（低スコアでも `table:N` として出す）。
      `split_budget_exhausted` が立った `table:N` は、隣接する別の表を巻き込んでいる可能性がある（`cells` に無い座標が矩形内に混在しうる）ので、消費側（人間向け MD・rag.md レンダラ）は表の分割精度を保証する用途に使わない。値・セル座標の完全性は `cells` で保たれる。
    - `formula:N`（シートをまたいだグローバル連番・`=` で始まるセルごと）。`text=<数式文字列>`。`parent_id` は、そのセル座標を所有する領域（`Region.cells` 基準）の `table:N` を最優先し、所有領域が無ければ外接矩形包含（`regions()` の返却順で最初に一致）、それも無ければ `sheet:N`。`order` はホスト（table または sheet）ごとのローカル連番。`source_map={"sheet", "cell", "has_cached_value": bool}`。未計算式が1件でもあれば `OoxmlArm.convert()` が `ArmResult.notes` に `"xlsx_uncached_formulas:<件数>"` を追記する。
    - `named_range:N`／`comment:N`／`hyperlink:N`／`strike:N`／`external_link:N`: いずれも型内だけの連番で `order` も同じ値、`parent_id=None`（セル座標を `source_map` が直接指すため）。
      - `named_range:N`: `text=<参照先文字列>`・`source_map={"name", "scope": "workbook"|<シート名>}`。`excel.defined_names()` の順。
      - `comment:N`: `text=<コメント本文>`・`source_map={"sheet", "cell", "author"}`。
      - `hyperlink:N`: `text=<セル表示値>`・`source_map={"sheet", "cell", "target"}`。
      - `strike:N`: `visibility="visible"`／`status="active"`（取り消し線は可視性を変えない）。`text=<セル表示値>`・`source_map={"sheet", "cell"}`。値の無いセルは出さない（`excel.strike_cells()`）。`evidence_spike._adapt_document_ir` が同じ `(sheet, cell)` の `cell` 要素にも `extension["visibility_reason"]="strike"` を反映する。
      - `external_link:N`: `text=None`・`source_map={"target": <zip 内 rels の Target>}`。`excel.external_link_targets()` の順。

    要素リストの並び: `external_link:*` → `named_range:*` →（シート順に）`sheet:N` とその `table:*`/`formula:*`/`comment:*`/`hyperlink:*`/`strike:*`。
    図形/画像による覆いは本関数では扱わない（DrawingML は `evidence_spike.py` の `_xlsx_objects` が解析し、Evidence IR 構築後に `cell` 要素の `visibility`/`extension["visibility_reason"]` を差し替える）。非表示行/列・取り消し線の cell 単位への反映も本関数ではせず、`table:N` の `source_map`（`hidden_rows`/`hidden_columns`）と独立の `strike:N` に格納するだけに留める（cell 単位の反映は `evidence_spike._adapt_document_ir` が行う）。
    壊れて開けないファイルは `openpyxl.load_workbook` が例外を投げ、`OoxmlArm.convert()` 側の try/except が処理する。ワークシートが1枚も無ければ None。
    """
    from .. import document_ir
    from ..ooxml import excel

    wb_values, wb_formula = excel.load_two(p)
    try:
        if not wb_values.worksheets:
            return None

        content_hash = "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
        source = document_ir.Source(path=str(p), content_hash=content_hash, file_type="xlsx")
        elements: list[document_ir.Element] = []
        extraction = document_ir.Extraction(method="ooxml", confidence=1.0)

        states = {s["name"]: s["state"] for s in excel.sheet_states(wb_values)}
        reason_by_state = {"hidden": "hidden_sheet", "veryHidden": "very_hidden"}
        # ワークブックあたり1つだけ構築し、全シートの `filled_cells()` 呼び出しに使い回す（シートごとに構築するとテーマのパースがシート数だけ繰り返される）。
        color_resolver = excel.ColorResolver(wb_values)

        with zipfile.ZipFile(p) as z:
            ext_targets = excel.external_link_targets(z)
            picture_counts = excel.picture_counts_by_sheet(z)  # シート名→画像枚数（0枚のシートはキー無し）
        for i, target in enumerate(ext_targets, start=1):
            elements.append(document_ir.Element(
                element_id=f"external_link:{i}", type="external_link", parent_id=None, order=i,
                visibility="visible", status="active", text=None, cells=None,
                source_map={"target": target}, extraction=extraction))

        for i, dn in enumerate(excel.defined_names(wb_values), start=1):
            elements.append(document_ir.Element(
                element_id=f"named_range:{i}", type="named_range", parent_id=None, order=i,
                visibility="visible", status="active", text=dn["value"], cells=None,
                source_map={"name": dn["name"], "scope": dn["scope"]}, extraction=extraction))

        sheet_seq = table_seq = formula_seq = comment_seq = hyperlink_seq = strike_seq = 0
        for ws_v in wb_values.worksheets:
            sheet_seq += 1
            sid = f"sheet:{sheet_seq}"
            name = ws_v.title
            state = states.get(name, "visible")
            reason = reason_by_state.get(state)

            ws_f = wb_formula[name]
            cap_rows = excel.effective_cap_rows(ws_v.max_column)
            max_row = min(ws_v.max_row or 1, cap_rows + 1)
            max_col = min(ws_v.max_column or 1, excel.DEFAULT_CAP_COLS + 1)
            grid = [list(row) for row in
                    ws_v.iter_rows(min_row=1, max_row=max_row, min_col=1, max_col=max_col, values_only=True)]
            merges = excel.merged_map(ws_v, cap_rows, excel.DEFAULT_CAP_COLS)
            filled = excel.filled_cells(ws_v, cap_rows, excel.DEFAULT_CAP_COLS, resolver=color_resolver)
            sheet_regions = excel.expand_regions_for_merges(
                excel.regions(grid, cap_rows, excel.DEFAULT_CAP_COLS, merged=merges, filled=filled),
                merges, cap_rows, excel.DEFAULT_CAP_COLS)  # 結合 span まで range を拡張
            hidden_r = set(excel.hidden_rows(ws_v))
            hidden_c = set(excel.hidden_cols(ws_v))

            sheet_sm: dict = {"sheet": name}
            if name in picture_counts:
                sheet_sm["picture_count"] = picture_counts[name]  # 画像の存在（枚数）のみ・内容には触れない
            truncated = excel.sheet_truncated(grid, cap_rows, excel.DEFAULT_CAP_COLS,
                                              sheet_max_row=ws_v.max_row or 1, sheet_max_col=ws_v.max_column or 1)
            if truncated:
                sheet_sm["truncated"] = True  # cap 外だけの領域も黙認しない（申告範囲基準）
            declared_rows = ws_v.max_row or 0
            if declared_rows >= _PARTIAL_XLSX_MIN_DECLARED_ROWS and not truncated:
                # cap 打切り（自己申告＝正常）とは独立の整合チェック。`grid` は declared_rows まで読み切っているので新たな走査は要らない。
                extracted_rows = 0
                last_nonempty_row = 0
                for i, row in enumerate(grid, start=1):
                    if any(v is not None for v in row):
                        extracted_rows += 1
                        last_nonempty_row = i
                # 最終非空行が宣言終端に達している疎シートは除外する（抽出は宣言の最後まで到達しており、内容がまばらなだけの正常なファイルと区別が付かない）。黙って途中で打ち切られた場合との違いは、宣言終端の手前で実データが途切れているか。
                if last_nonempty_row < declared_rows and extracted_rows < declared_rows * _PARTIAL_XLSX_MAX_EXTRACTED_RATIO:
                    sheet_sm["partial_extraction_suspected"] = True
                    sheet_sm["declared_rows"] = declared_rows
                    sheet_sm["extracted_rows"] = extracted_rows
            elements.append(document_ir.Element(
                element_id=sid, type="sheet", parent_id=None, order=sheet_seq,
                visibility=("hidden" if reason else "visible"), visibility_reason=reason,
                status="active", text=None, cells=None,
                source_map=sheet_sm, extraction=extraction))

            # 座標→所有領域。外接矩形どうしが重なっても、非空セルは所有領域の table にだけ出す。
            owner: dict[tuple[int, int], int] = {}
            for ri, rg in enumerate(sheet_regions):
                for coord in rg.cells:
                    owner[coord] = ri

            region_tables: list[tuple[object, str]] = []  # [(Region, table_id)]（formula 親解決用）
            for local_order, rg in enumerate(sheet_regions, start=1):
                table_seq += 1
                tid = f"table:{table_seq}"
                region_tables.append((rg, tid))
                cells: list[document_ir.Cell] = []
                for r in range(rg.min_row, rg.max_row + 1):
                    for c in range(rg.min_col, rg.max_col + 1):
                        own = owner.get((r, c))
                        if own is not None and sheet_regions[own] is not rg:
                            continue  # 他領域が所有する非空セルは重複出力しない
                        info = merges.get((r, c))
                        if info is not None and info["anchor"] != (r, c):
                            continue  # 非anchor継続セルは出さない
                        row_span = info["row_span"] if info else 1
                        col_span = info["column_span"] if info else 1
                        raw = grid[r - 1][c - 1] if (r - 1 < len(grid) and c - 1 < len(grid[r - 1])) else None
                        cells.append(document_ir.Cell(
                            row=r, column=c, text=("" if raw is None else str(raw)),
                            row_span=row_span, column_span=col_span, role="unknown"))
                from openpyxl.utils import column_index_from_string
                sm = {
                    "sheet": name, "range": rg.range,
                    "hidden_rows": sorted(r for r in hidden_r if rg.min_row <= r <= rg.max_row),
                    "hidden_columns": sorted(
                        (c for c in hidden_c if rg.min_col <= column_index_from_string(c) <= rg.max_col),
                        key=column_index_from_string),
                    "score": round(rg.score, 3),
                }
                if rg.truncated:
                    sm["truncated"] = True
                if rg.split_budget_exhausted:
                    sm["split_budget_exhausted"] = True
                elements.append(document_ir.Element(
                    element_id=tid, type="table", parent_id=sid, order=local_order,
                    visibility="visible", status="active", text=None, cells=cells,
                    source_map=sm, extraction=extraction))

            host_local_order: dict[str, int] = {}
            for f in excel.formulas(ws_f, ws_v):
                formula_seq += 1
                coord = (f["row"], f["column"])
                # 親解決は所有領域を最優先（bbox 重複時の誤親子化を防ぐ）。未計算式（キャッシュ無し＝非占有）はどの領域にも属さないため、bbox 包含 → sheet の順で縮退する。
                own = owner.get(coord)
                if own is not None:
                    host_id = region_tables[own][1]
                else:
                    host_id = next((tid for rg, tid in region_tables
                                    if rg.min_row <= f["row"] <= rg.max_row
                                    and rg.min_col <= f["column"] <= rg.max_col), sid)
                host_local_order[host_id] = host_local_order.get(host_id, 0) + 1
                elements.append(document_ir.Element(
                    element_id=f"formula:{formula_seq}", type="formula", parent_id=host_id,
                    order=host_local_order[host_id], visibility="visible", status="active",
                    text=f["formula"], cells=None,
                    source_map={"sheet": name, "cell": f["cell"], "has_cached_value": f["has_cached"]},
                    extraction=extraction))

            for cm in excel.cell_comments(ws_v):
                comment_seq += 1
                elements.append(document_ir.Element(
                    element_id=f"comment:{comment_seq}", type="comment", parent_id=None,
                    order=comment_seq, visibility="visible", status="active",
                    text=cm["text"], cells=None,
                    source_map={"sheet": name, "cell": cm["cell"], "author": cm["author"]},
                    extraction=extraction))

            for hl in excel.cell_hyperlinks(ws_v):
                hyperlink_seq += 1
                elements.append(document_ir.Element(
                    element_id=f"hyperlink:{hyperlink_seq}", type="hyperlink", parent_id=None,
                    order=hyperlink_seq, visibility="visible", status="active",
                    text=hl["text"], cells=None,
                    source_map={"sheet": name, "cell": hl["cell"], "target": hl["target"]},
                    extraction=extraction))

            for sk in excel.strike_cells(ws_v):
                # 取り消し線は可視性を変えない幾何的事実。
                strike_seq += 1
                elements.append(document_ir.Element(
                    element_id=f"strike:{strike_seq}", type="strike_text", parent_id=None,
                    order=strike_seq, visibility="visible", status="active",
                    text=sk["text"], cells=None,
                    source_map={"sheet": name, "cell": sk["cell"]},
                    extraction=extraction))

        return document_ir.DocumentIR(schema_version=document_ir.DOCUMENT_IR_SCHEMA_VERSION, doc_id="",
                                      source=source, elements=elements)
    finally:
        wb_values.close()
        wb_formula.close()
