"""PowerPoint（.pptx）の生 OOXML 抽出層。`arms/ooxml_arm._build_pptx_ir` が消費する純関数群。

幾何・覆い判定は再実装せず、`office_md.py` のヘルパ（`_pptx_bbox`／`_pptx_fill_kind`／`_bbox_intersection_ratio`／`_OCCLUSION_RATIO`／`_pptx_shape_texts_list`／`_pptx_group_texts`）と表示順解決（`_slide_order`）を import して使う。ここで持つのは、MD 生成が対象にしていない構造（非表示スライド・発表者ノート・スライドサイズ）の抽出と、occlusion ヘルパを消費する shape 単位の分類（`slide_shapes`）だけ。MD 生成（`office_md._pptx_md`）には触れない。
透明塗り・白文字など、テーマ色の解決を要する隠しテキストの検出は対象外。
`xml.etree.ElementTree` ベースの決定的な純関数。壊れた/欠落したパートは例外を投げず空/None へ縮退する。
設計: docs/design/rag.md「アーム一覧」
"""
from __future__ import annotations

import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

from .. import drawing_fill, image_alpha
from ..observation_vocab import FILL_OPAQUE, FILL_UNKNOWN

_PR = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_IMAGE_READ_LIMIT = 64 * 1024 * 1024  # 透過の判定に読む画像の最大の長さ（超える分は読まず、判定は unknown になる）

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


_STRIKE_VALUES = frozenset({"sngStrike", "dblStrike"})
_STRIKE_LABEL = "取り消し線"  # Word の `word.stated_text` と同じラベル（`evidence_render._STATED_OPEN_RE` が括りの分割・閉じ直しに使う）


def _run_struck(r_el) -> bool:
    """run 自身の `a:rPr/@strike` が `sngStrike`／`dblStrike` か（`noStrike`・指定なしは通常。`lstStyle`・マスター・レイアウトからの継承は読まない）。"""
    r_pr = r_el.find(f"{_A}rPr")
    return r_pr is not None and r_pr.get("strike") in _STRIKE_VALUES


def _para_runs(p_el) -> list[tuple[bool, str]]:
    """段落内の `a:t` を文書順に `(取り消し線か, 文字)` で返す。`a:r`／`a:fld` 以外に包まれた `a:t` は通常として拾う。"""
    out: list[tuple[bool, str]] = []
    for child in p_el:
        if child.tag in (f"{_A}r", f"{_A}fld"):
            struck = _run_struck(child)
            out.extend((struck, t.text or "") for t in child.findall(f"{_A}t"))
        else:
            out.extend((False, t.text or "") for t in child.iter(f"{_A}t"))
    return out


def _struck_span(text: str) -> str:
    """取り消し線の run を連結した文字を `【取り消し線：…】` で括る。括りの前後の空白は括りの外へ出し、空白だけなら括らない。"""
    inner = text.strip()
    if not inner:
        return text
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return f"{lead}【{_STRIKE_LABEL}：{inner}】{trail}"


def stated_text(p_el) -> str:
    """段落（`a:p`）の本文。取り消し線の run は `【取り消し線：…】` と状態を添えて書く（Word の `word.stated_text` と同じ形）。

    ① run を文書順に拾う ② 同じ状態が続く run を1つの括りにまとめる ③ 括りの前後の空白は括りの外へ出す。
    直接指定の `a:rPr/@strike` だけを読む。状態の意味（廃止など）は断定せず、原本が持つ状態だけを書く。
    """
    segments: list[list] = []  # [取り消し線か, 連結文字列]
    for struck, text in _para_runs(p_el):
        if segments and segments[-1][0] == struck:
            segments[-1][1] += text
        else:
            segments.append([struck, text])
    return "".join(_struck_span(text) if struck else text for struck, text in segments)


def _stated_lines(p_el) -> list[str]:
    """段落の非空テキスト行（run 単位・順序保持）。連続する取り消し線の run は `【取り消し線：…】` の1行にまとめる。"""
    lines: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            span = _struck_span("".join(pending)).strip()
            if span:
                lines.append(span)
            pending.clear()

    for struck, text in _para_runs(p_el):
        if struck:
            pending.append(text)
            continue
        flush()
        if text.strip():
            lines.append(text.strip())
    flush()
    return lines


def text_lines(el) -> list[str]:
    """要素（`p:txBody`・`p:sp`・`p:graphicFrame` など）の中の非空テキスト行を文書順に返す（run 単位・前後の空白は外す）。

    取り消し線の run を持たない要素は、`a:t` を1つずつ並べた行と完全に一致する。持つ要素だけ、段落ごとに `stated_text` と同じ括りを付ける。
    """
    if not any(r_pr.get("strike") in _STRIKE_VALUES for r_pr in el.iter(f"{_A}rPr")):
        return [t.text.strip() for t in el.iter(f"{_A}t") if t.text and t.text.strip()]
    lines: list[str] = []
    for p_el in el.iter(f"{_A}p"):
        lines.extend(_stated_lines(p_el))
    return lines


def slide_picture_fills(zf: zipfile.ZipFile, slide_name: str, root) -> dict[str, str]:
    """スライドの `a:blip r:embed` ごとに、埋め込み画像の塗りの区分（関係 ID → 区分）を返す。

    関係・画像が読めない／外部参照は unknown。透過の判定は `image_alpha.image_fill_kind`（画像全体の有無だけ）。
    """
    wanted = {blip.get(f"{_R}embed") for blip in root.iter(f"{_A}blip") if blip.get(f"{_R}embed")}
    if not wanted:
        return {}
    rels_name = posixpath.join(posixpath.dirname(slide_name), "_rels", posixpath.basename(slide_name) + ".rels")
    try:
        rels_root = ET.fromstring(zf.read(rels_name))
    except (KeyError, ET.ParseError):
        return {rid: FILL_UNKNOWN for rid in wanted}
    targets = {r.get("Id"): r for r in rels_root.iter(f"{_RELS}Relationship")}
    out: dict[str, str] = {}
    for rid in wanted:
        rel = targets.get(rid)
        target = rel.get("Target") if rel is not None else None
        if rel is None or not target or (rel.get("TargetMode") or "").casefold() == "external":
            out[rid] = FILL_UNKNOWN
            continue
        media = target.lstrip("/") if target.startswith("/") else posixpath.normpath(
            posixpath.join(posixpath.dirname(slide_name), target))
        try:
            with zf.open(media) as handle:
                out[rid] = image_alpha.image_fill_kind(handle.read(_IMAGE_READ_LIMIT))
        except KeyError:
            out[rid] = FILL_UNKNOWN
    return out


def _placeholder(el) -> tuple[str | None, str | None] | None:
    """図形のプレースホルダー指定 `p:nvPr/p:ph` の (type, idx)。プレースホルダーでなければ None。"""
    for child in el:
        if child.tag.startswith(_PR + "nv"):
            ph = child.find(f"{_PR}nvPr/{_PR}ph")
            return None if ph is None else (ph.get("type"), ph.get("idx"))
    return None


_MASTER_TYPE_OF = {"ctrTitle": "title", "subTitle": "body"}


def _related_part(zf: zipfile.ZipFile, part: str, suffix: str):
    """`part` の rels から Type が `suffix` で終わる関係の先のパート名とその XML。無い・壊れていれば (None, None)。"""
    rels_name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    try:
        rels_root = ET.fromstring(zf.read(rels_name))
        for rel in rels_root.iter(f"{_RELS}Relationship"):
            target = rel.get("Target")
            if (rel.get("Type") or "").endswith(suffix) and target:
                name = target.lstrip("/") if target.startswith("/") else posixpath.normpath(
                    posixpath.join(posixpath.dirname(part), target))
                return name, ET.fromstring(zf.read(name))
    except (KeyError, ET.ParseError):
        pass
    return None, None


def _normalized_idx(idx: str | None) -> str:
    """`p:ph/@idx` を比較用に正規化する。省略は既定値 0。整数として読めなければ文字列のまま。"""
    if idx is None:
        return "0"
    try:
        return str(int(idx))
    except ValueError:
        return idx


def _matching_placeholder(tree_root, key: tuple[str | None, str | None], *, master: bool):
    """レイアウト・マスターの `p:spTree` から、スライドのプレースホルダー (type, idx) に対応する図形。

    idx は省略を 0 として正規化して比べる。レイアウトは正規化した idx が同じもの（type の有無では切り替えない）。
    マスターは type で対応づけ（ctrTitle は title、subTitle と type の無いものは body）、同じ type の中で正規化した idx が
    同じものを優先する。
    守ること: 対応する候補がただ 1 つに決まらないとき（同じ idx が複数・マスターで idx が一致せず同じ type が複数）は None
    （別のプレースホルダーの形で判定しない）。
    """
    slide_type, raw_idx = key
    idx = _normalized_idx(raw_idx)
    if master:
        wanted_type = slide_type or "obj"
        wanted_type = _MASTER_TYPE_OF.get(wanted_type, "body" if wanted_type in ("obj", "tbl", "chart", "dgm", "media") else wanted_type)
        same_type = [el for el in tree_root.iter(f"{_PR}sp")
                     if (placeholder := _placeholder(el)) is not None and (placeholder[0] or "obj") == wanted_type]
        same_idx = [el for el in same_type if _normalized_idx(_placeholder(el)[1]) == idx]
        if same_idx:
            return same_idx[0] if len(same_idx) == 1 else None
        return same_type[0] if len(same_type) == 1 else None
    matches = [el for el in tree_root.iter(f"{_PR}sp")
               if (placeholder := _placeholder(el)) is not None and _normalized_idx(placeholder[1]) == idx]
    return matches[0] if len(matches) == 1 else None


def slide_inherited_covers_bbox(zf: zipfile.ZipFile, slide_name: str, root) -> dict[tuple[str | None, str | None], bool]:
    """形状を直接指定していないプレースホルダーについて、スライドレイアウト・スライドマスターから形状を受け継いだ結果を返す。

    キーはプレースホルダーの (type, idx)、値は受け継いだ形状が外接矩形と同一視できるか（`drawing_fill`）。
    レイアウト → マスターの順に、対応するプレースホルダー（`_matching_placeholder`）の形状の指定を探し、
    マスターはレイアウトで対応したプレースホルダーの (type, idx) で探す（スライド側の指定では探さない）。
    見つからない（継承元が読めない・レイアウトに対応するものが無い・どこにも形状の指定が無い）ものは含めない（呼び出し側は未確定として扱う）。
    """
    out: dict[tuple[str | None, str | None], bool] = {}
    pending: dict[tuple[str | None, str | None], None] = {}
    for el in root.iter():
        if el.tag in (f"{_PR}sp", f"{_PR}pic"):
            key = _placeholder(el)
            if key is not None and drawing_fill.explicit_geometry_covers_bounding_box(el.find(f"{_PR}spPr")) is None:
                pending[key] = None
    if not pending:
        return out
    layout_name, layout_root = _related_part(zf, slide_name, "/slideLayout")
    master_root = None
    if layout_name is not None:
        _, master_root = _related_part(zf, layout_name, "/slideMaster")
    for key in pending:
        if layout_root is None:
            continue
        layout_match = _matching_placeholder(layout_root, key, master=False)
        if layout_match is None:
            continue
        explicit = drawing_fill.explicit_geometry_covers_bounding_box(layout_match.find(f"{_PR}spPr"))
        if explicit is None and master_root is not None:
            master_match = _matching_placeholder(master_root, _placeholder(layout_match), master=True)
            if master_match is not None:
                explicit = drawing_fill.explicit_geometry_covers_bounding_box(master_match.find(f"{_PR}spPr"))
        if explicit is not None:
            out[key] = explicit
    return out


def _shape_identity(el) -> tuple[int | None, str]:
    """図形要素の `p:nv*Pr/p:cNvPr` から原本の図形 ID（`@id`）と名前（`@name`）。ID が読めなければ None。"""
    for child in el:
        if child.tag.startswith(_PR + "nv"):
            c_nv_pr = child.find(f"{_PR}cNvPr")
            if c_nv_pr is None:
                return None, ""
            try:
                shape_id = int(c_nv_pr.get("id"))
            except (TypeError, ValueError):
                shape_id = None
            return shape_id, c_nv_pr.get("name") or ""
    return None, ""


def slide_shapes(root, picture_fills: dict[str, str] | None = None,
                 inherited_covers_bbox: dict[tuple[str | None, str | None], bool] | None = None) -> list[dict]:
    """`p:cSld/p:spTree` 直下を文書順（z順・背面→前面）で歩いた shape 分類（`_build_pptx_ir` が消費する純データ）。

    `office_md._pptx_slide_texts_by_shape` と同じ判定基準を同じヘルパ関数で再現する。MD 側のフラット抽出のフォールバックは持たない（失敗は `_build_pptx_ir` が1文書分の IR 構築失敗として扱う）。`p:cSld`／`p:spTree` が無ければ空リスト。

    各要素（dict）: `{"kind": "sp"|"pic"|"grpSp"|"graphicFrame"|"other", "texts": list[str], "bbox": (x0,y0,x1,y1)|None, "occluder": bool, "fill": str|None, "covers_bbox": bool, "has_text": bool, "group": bool, "shape_id": int|None, "name": str}`。
    - `shape_id`／`name`: 原本の図形 ID（`cNvPr/@id`）と名前（`cNvPr/@name`）。Evidence 側の図形との対応は z 順ではなくこの ID で取る。
    - `bbox` は `p:grpSp` では常に None（グループ内は幾何判定せず、テキストのみ再帰抽出する）。
    - `fill`: `p:sp`（文字の有無を問わない）と `p:pic` の塗りの区分（`observation_vocab.FILL_*`）。それ以外は None。`picture_fills` は `slide_picture_fills` の結果。
    - `covers_bbox`: 覆う範囲を外接矩形と同一視してよい形か（`office_md._pptx_covers_bbox`。ドーナツ・楕円・カスタム形状、形を指定していない図形、
      継承元を解決できないプレースホルダーは False）。`inherited_covers_bbox` は `slide_inherited_covers_bbox` の結果。
    - `occluder`: 背面のテキスト shape を隠し得るか（bbox があり、塗りが opaque で `covers_bbox` の `p:sp`、または同条件の `p:pic`）。`p:grpSp`／`p:graphicFrame`／`other` は常に False。
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
        shape_id, shape_name = _shape_identity(el)
        if tag == f"{_PR}sp":
            texts = office_md._pptx_shape_texts_list(el)
            bbox = office_md._pptx_bbox(el)
            has_text = bool(texts)
            fill = office_md._pptx_fill_kind(el, picture_fills, has_text=has_text)
            entries.append({"kind": "sp", "texts": texts, "bbox": bbox,
                             "occluder": office_md._pptx_hides_behind(el, picture_fills, inherited_covers_bbox, has_text=has_text) and bbox is not None,
                             "fill": fill, "covers_bbox": office_md._pptx_covers_bbox(el, inherited_covers_bbox),
                             "has_text": has_text, "group": False,
                             "shape_id": shape_id, "name": shape_name})
        elif tag == f"{_PR}pic":
            bbox = office_md._pptx_bbox(el)
            fill = office_md._pptx_fill_kind(el, picture_fills)
            entries.append({"kind": "pic", "texts": [], "bbox": bbox,
                             "occluder": office_md._pptx_hides_behind(el, picture_fills, inherited_covers_bbox) and bbox is not None,
                             "fill": fill, "covers_bbox": office_md._pptx_covers_bbox(el, inherited_covers_bbox),
                             "has_text": False, "group": False,
                             "shape_id": shape_id, "name": shape_name})
        elif tag == f"{_PR}grpSp":
            group_texts = office_md._pptx_group_texts(el)
            entries.append({"kind": "grpSp", "texts": group_texts, "bbox": None,
                             "occluder": False, "fill": None, "has_text": bool(group_texts), "group": True,
                             "shape_id": shape_id, "name": shape_name})
        elif tag == f"{_PR}graphicFrame":
            texts = text_lines(el)
            entries.append({"kind": "graphicFrame", "texts": texts, "bbox": None,
                             "occluder": False, "fill": None, "has_text": bool(texts), "group": False,
                             "shape_id": shape_id, "name": shape_name})
        else:  # p:cxnSp／未知要素等: テキストのみ拾う
            texts = text_lines(el)
            entries.append({"kind": "other", "texts": texts, "bbox": None,
                             "occluder": False, "fill": None, "has_text": bool(texts), "group": False,
                             "shape_id": shape_id, "name": shape_name})
    return entries
