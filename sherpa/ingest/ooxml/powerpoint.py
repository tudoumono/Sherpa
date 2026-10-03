"""PowerPoint（.pptx）の生 OOXML 抽出層。`arms/ooxml_arm._build_pptx_ir` が消費する純関数群。

幾何・覆い判定は再実装せず、`office_md.py` のヘルパ（`_pptx_bbox`／`_pptx_has_solid_fill`／`_bbox_intersection_ratio`／`_OCCLUSION_RATIO`／`_pptx_shape_texts_list`／`_pptx_group_texts`）と表示順解決（`_slide_order`）を import して使う。ここで持つのは、MD 生成が対象にしていない構造（非表示スライド・発表者ノート・スライドサイズ）の抽出と、occlusion ヘルパを消費する shape 単位の分類（`slide_shapes`）だけ。MD 生成（`office_md._pptx_md`）には触れない。
透明塗り・白文字など、テーマ色の解決を要する隠しテキストの検出は対象外。
`xml.etree.ElementTree` ベースの決定的な純関数。壊れた/欠落したパートは例外を投げず空/None へ縮退する。
設計: docs/design/rag.md「アーム一覧」
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
    """`ppt/presentation.xml` の `p:sldSz`（`(cx, cy)`・EMU）。パート欠落/壊れ/属性欠落は None（呼び出し側は「画面外」判定を行わないだけで IR 構築は継続する）。"""
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

    各 `ppt/slides/slideN.xml` を直接見て判定する（表示順は呼び出し側が `office_md._slide_order` で解決する）。壊れて読めないスライドは非表示扱いにしない。
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
    """`slide_name` の発表者ノート本文と notes part（段落を `"\\n"` 結合）を返す。

    スライドの `_rels/{slideN}.xml.rels` から `.../relationships/notesSlide` の Target を解決する（`Type` 属性の末尾一致）。rels／notesSlide が欠落/壊れている場合、ノートが無い・空の場合は None。段落は `.iter(a:p)` で全 `a:p` を拾う。
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
    if target.startswith("/"):  # 絶対パッケージパス（例 "/ppt/notesSlides/…"）も
        notes_name = target.lstrip("/")  # 正当な rels Target（zip 名の先頭に "/" は無い）
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
    """`p:cSld/p:spTree` 直下を文書順（z順・背面→前面）で歩いた shape 分類（`_build_pptx_ir` が消費する純データ）。

    `office_md._pptx_slide_texts_by_shape` と同じ判定基準を同じヘルパ関数で再現する。MD 側のフラット抽出のフォールバックは持たない（失敗は `_build_pptx_ir` が1文書分の IR 構築失敗として扱う）。`p:cSld`／`p:spTree` が無ければ空リスト。

    各要素（dict）: `{"kind": "sp"|"pic"|"grpSp"|"graphicFrame"|"other", "texts": list[str], "bbox": (x0,y0,x1,y1)|None, "occluder": bool, "has_text": bool, "group": bool}`。
    - `bbox` は `p:grpSp` では常に None（グループ内は幾何判定せず、テキストのみ再帰抽出する）。
    - `occluder`: 後続（前面）のテキスト shape を覆い得るか（無地塗りの空 `p:sp`、または `p:pic`）。`p:grpSp`／`p:graphicFrame`／`other` は常に False。
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
        else:  # p:cxnSp／未知要素等: テキストのみ拾う
            texts = [t.text.strip() for t in el.iter(f"{_A}t") if t.text and t.text.strip()]
            entries.append({"kind": "other", "texts": texts, "bbox": None,
                             "occluder": False, "has_text": bool(texts), "group": False})
    return entries
