"""PowerPoint（.pptx）の生 OOXML 抽出層（DOC-IR-003・パッケージ docstring＝`sherpa/ingest/ooxml/__init__.py` 参照）。

`arms/ooxml_arm._build_pptx_ir` が消費する純関数群。**幾何・覆い判定のロジックは再実装しない**（外部レビュー
#5 の「同じ抽出処理を複数箇所で重複実装しない」原則）: `office_md.py` の A5 幾何ヘルパ
（`_pptx_bbox`／`_pptx_has_solid_fill`／`_bbox_intersection_ratio`／`_OCCLUSION_RATIO`／
`_pptx_shape_texts_list`／`_pptx_group_texts`）と表示順解決（`_slide_order`）をそのまま import して使う。
本モジュールが新たに持つのは、MD 生成が対象にしていない構造（非表示スライド・発表者ノート・スライド
サイズ）の抽出と、`office_md` の occlusion ヘルパを消費する shape 単位の分類（`slide_shapes`）だけ。
**MD 生成（`office_md._pptx_md`／`_pptx_slide_texts*`）には一切触れない**＝MD バイト不変。

透明塗り・白文字などの「見た目には隠れているがOOXML属性上は隠しでない」テキストの検出（テーマ色の解決を
要する）は本スライスのスコープ外（Windows ワーカー／後続スライスの領域）。

すべて純関数・`xml.etree.ElementTree` ベース・LLM 不使用・決定的。壊れた/欠落した OOXML パート
（`presentation.xml`／`notesSlide*.xml`／rels 等）は例外を投げず空/None へ縮退する（`ooxml/word.py` と
同じ fail-safe 方針）。
"""
from __future__ import annotations

import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

_PR = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_SLIDE_NAME_RE = re.compile(r"ppt/slides/slide\d+\.xml")


def slide_size(zf: zipfile.ZipFile) -> tuple[int, int] | None:
    """`ppt/presentation.xml` の `p:sldSz`（`(cx, cy)`・EMU）。パート欠落/壊れ/属性欠落は None
    （呼び出し側＝`_build_pptx_ir` は None なら「画面外」判定を行わないだけで IR 構築自体は継続する）。
    """
    try:
        root = ET.fromstring(zf.read("ppt/presentation.xml"))
    except (KeyError, ET.ParseError):
        return None
    sz = root.find(f"{_PR}sldSz")
    if sz is None:
        return None
    try:
        return int(sz.get("cx")), int(sz.get("cy"))
    except (TypeError, ValueError):
        return None


def hidden_slide_names(zf: zipfile.ZipFile) -> set[str]:
    """非表示スライド（`p:sld @show="0"`）の zip パート名（例 `"ppt/slides/slide3.xml"`）集合。

    `p:sldIdLst`→rels の解決を経ず、各 `ppt/slides/slideN.xml` を直接見て判定する（`show` 属性はスライド
    自身が持つため表示順の解決とは独立に求まる＝素直な実装。表示順自体は呼び出し側が
    `office_md._slide_order` で別途解決する）。壊れて読めないスライドパートは非表示扱いにしない
    （fail-safe：判定不能を「隠す」方向へ倒さない）。
    """
    out: set[str] = set()
    for name in zf.namelist():
        if not _SLIDE_NAME_RE.fullmatch(name):
            continue
        try:
            root = ET.fromstring(zf.read(name))
        except (KeyError, ET.ParseError):
            continue
        show = root.get("show")
        if show is not None and show.strip() == "0":
            out.add(name)
    return out


def notes_with_part_for_slide(zf: zipfile.ZipFile, slide_name: str) -> tuple[str, str] | None:
    """`slide_name`の発表者ノート本文とnotes part（段落を`"\\n"`結合）。

    スライドの `_rels/{slideN}.xml.rels` から `.../relationships/notesSlide` の Target を解決する
    （`Type` 属性の末尾一致で判定＝完全一致より緩いが、OOXML の Relationship Type URI は仕様上固定の
    サフィックスを持つため十分）。rels／notesSlide パートいずれかが欠落/壊れていれば None（fail-safe）。
    ノートが無い／空文字だけ（雛形のみ）の場合も None（意味を持たない空ノートを出さない・
    `ooxml/word.py` の脚注/コメントと同じ方針）。

    段落抽出は `notesSlide` の具体的な `p:cSld/p:spTree` 構造を辿らず `.iter(a:p)` で全 `a:p` を拾う
    （ノート雛形は「スライド縮小表示プレースホルダ」＋「本文プレースホルダ」の2 shape を持つのが通例だが、
    前者はテキストを持たないため段落抽出には影響しない＝構造への依存を減らした素直な実装）。
    """
    rels_name = posixpath.join(posixpath.dirname(slide_name), "_rels",
                                posixpath.basename(slide_name) + ".rels")
    try:
        rels_root = ET.fromstring(zf.read(rels_name))
    except (KeyError, ET.ParseError):
        return None
    target = None
    for r in rels_root.iter(f"{_RELS}Relationship"):
        if (r.get("Type") or "").endswith("/notesSlide"):
            target = r.get("Target")
            break
    if not target:
        return None
    if target.startswith("/"):                              # 絶対パッケージパス（例 "/ppt/notesSlides/…"）も
        notes_name = target.lstrip("/")                     # 正当な rels Target（RV Low #2: zip 名に "/" 先頭は無い）
    else:
        notes_name = posixpath.normpath(posixpath.join(posixpath.dirname(slide_name), target))
    try:
        notes_root = ET.fromstring(zf.read(notes_name))
    except (KeyError, ET.ParseError):
        return None
    paras = [t for t in ("".join(r.text or "" for r in p.iter(f"{_A}t")) for p in notes_root.iter(f"{_A}p")) if t]
    text = "\n".join(paras)
    return (text, notes_name) if text else None


def notes_for_slide(zf: zipfile.ZipFile, slide_name: str) -> str | None:
    """後方互換API。本文だけが必要な呼び出しへ発表者ノート文字列を返す。"""
    payload = notes_with_part_for_slide(zf, slide_name)
    return payload[0] if payload is not None else None


def slide_shapes(root) -> list[dict]:
    """`p:cSld/p:spTree` 直下を文書順（=z順・背面→前面）で歩いた shape 分類（`_build_pptx_ir` が消費する
    純データ）。`office_md._pptx_slide_texts_by_shape` の entries 構築と同じ判定基準を、同じヘルパ関数
    （`_pptx_bbox`／`_pptx_has_solid_fill`／`_pptx_shape_texts_list`／`_pptx_group_texts`）を呼び出す形で
    再現する（幾何ロジック自体の二重実装はしない）。**MD 側のフォールバック（フラット抽出）は持たない**
    ＝`_build_pptx_ir` 側で1文書分の IR 構築失敗として fail-safe に処理する（`office_md` 側の
    フォールバックは MD の取りこぼし防止が目的で IR の要件とは別軸）。

    `p:cSld`／`p:spTree` が無い（壊れた/非標準の spTree）場合は空リストを返す。

    各要素（dict）: `{"kind": "sp"|"pic"|"grpSp"|"graphicFrame"|"other", "texts": list[str],
    "bbox": (x0,y0,x1,y1)|None, "occluder": bool, "has_text": bool, "group": bool}`。
    - `bbox` は `p:grpSp` は常に None（グループ内は幾何判定しない＝現行 MD と同じ「テキストのみ再帰抽出」）。
    - `occluder`: 後続（前面）のテキスト shape に対する被覆判定の対象になりうるか（無地塗りの空 `p:sp`、
      または `p:pic`）。`p:grpSp`／`p:graphicFrame`／`other` は常に False（MD 側と同じ）。
    """
    from .. import office_md
    try:
        cSld = root.find(f"{_PR}cSld")
        if cSld is None:
            return []
        spTree = cSld.find(f"{_PR}spTree")
        if spTree is None:
            return []
    except Exception:
        return []

    entries: list[dict] = []
    for el in spTree:
        tag = el.tag
        if tag == f"{_PR}sp":
            texts = office_md._pptx_shape_texts_list(el)
            bbox = office_md._pptx_bbox(el)
            has_text = bool(texts)
            is_occluder = (not has_text) and office_md._pptx_has_solid_fill(el) and bbox is not None
            entries.append({"kind": "sp", "texts": texts, "bbox": bbox,
                             "occluder": is_occluder, "has_text": has_text, "group": False})
        elif tag == f"{_PR}pic":
            bbox = office_md._pptx_bbox(el)
            entries.append({"kind": "pic", "texts": [], "bbox": bbox,
                             "occluder": bbox is not None, "has_text": False, "group": False})
        elif tag == f"{_PR}grpSp":
            group_texts = office_md._pptx_group_texts(el)
            entries.append({"kind": "grpSp", "texts": group_texts, "bbox": None,
                             "occluder": False, "has_text": bool(group_texts), "group": True})
        elif tag == f"{_PR}graphicFrame":
            texts = [t.text.strip() for t in el.iter(f"{_A}t") if t.text and t.text.strip()]
            entries.append({"kind": "graphicFrame", "texts": texts, "bbox": None,
                             "occluder": False, "has_text": bool(texts), "group": False})
        else:                                                       # p:cxnSp／未知要素等: テキストのみ拾う
            texts = [t.text.strip() for t in el.iter(f"{_A}t") if t.text and t.text.strip()]
            entries.append({"kind": "other", "texts": texts, "bbox": None,
                             "occluder": False, "has_text": bool(texts), "group": False})
    return entries
