"""Word（.docx）の生 OOXML 抽出層。`arms/ooxml_arm._build_docx_ir` が消費する純関数群で、MD が表示しない構造（隠し文字・変更履歴の削除本文・ハイパーリンク先・テキストボックス・脚注・コメント・ヘッダ/フッタ）を一度だけ抽出する。

- 隠し文字（`w:vanish`）・取り消し線（`w:strike`／`w:dstrike`）・採用本文（`w:ins`）は、`stated_text`（`office_md._para_text` の実体）がホスト段落の全文に含める（隠し・取り消し線の run は状態つきで書く）。ここではそのうち隠し/取り消し線由来の部分を追加で切り出す。
- テキストボックス（`w:txbxContent`）の文字はホスト段落の全文に含めない（`textboxes()` が独立の要素として持つ）。
- 削除本文（`w:del//w:delText`）は `stated_text` が拾わないため、ここで独立に取り出す。

`xml.etree.ElementTree` ベースの決定的な純関数。壊れた/欠落した OOXML パートは例外を投げず空リストへ縮退する。
設計: docs/design/rag.md「アーム一覧」
"""
from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree as ET

from .rels import load_relationships

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"

# footnote/endnote の区切り線・継続区切り線（本文ではない・IR には出さない）。
_NOTE_SEPARATOR_TYPES = {"separator", "continuationSeparator"}


def _run_text(r_el) -> str:
    """1つの `w:r`（run）内の `w:t` テキスト連結。run に埋め込まれたテキストボックス（`w:txbxContent`）の中は含めない。"""
    return "".join(t.text or "" for t in _iter_skip_txbx(r_el, f"{_W}t"))


_FALSY_TOGGLE_VALS = {"0", "false", "off"}


def _iter_skip_txbx(el, tag):
    """`el` 配下の `tag` 要素を文書順で yield する。ただし `w:txbxContent` サブツリーへは降りない。

    テキストボックス内部の run/リンクをホスト段落側の抽出から除外するための walker（同じテキストが二重に出るのを防ぐ。テキストボックス内部は `textboxes()` が本文として持ち、内部の隠し/削除の個別マーク付けはしない）。
    """
    for child in el:
        if child.tag == f"{_W}txbxContent":
            continue
        if child.tag == tag:
            yield child
        yield from _iter_skip_txbx(child, tag)


def _toggle_true(el) -> bool:
    """OOXML の真偽トグル要素（`w:vanish` 等）: 要素が存在し `w:val` が明示的な偽値でなければ真（`w:val` 省略＝真）。"""
    if el is None:
        return False
    return (el.get(f"{_W}val") or "").strip().lower() not in _FALSY_TOGGLE_VALS


def hidden_runs(p_el) -> list[str]:
    """段落内の隠し文字 run（`w:rPr/w:vanish` を持つ `w:r`）のテキストを出現順で返す。

    判定は各 run 自身の `w:rPr` のみ（段落既定の `w:pPr/w:rPr` は対象外）。`w:val="0"/"false"/"off"` で打ち消された `w:vanish` は隠しと扱わない。テキストが空の run は出さない。テキストボックス内部は対象外（`_iter_skip_txbx`）。
    """
    out: list[str] = []
    for r in _iter_skip_txbx(p_el, f"{_W}r"):
        r_pr = r.find(f"{_W}rPr")
        if r_pr is not None and _toggle_true(r_pr.find(f"{_W}vanish")):
            text = _run_text(r)
            if text:
                out.append(text)
    return out


def strike_runs(p_el) -> list[str]:
    """段落内の取り消し線 run（`w:rPr/w:strike` または `w:dstrike` を持つ `w:r`）のテキストを出現順で返す。

    判定は `hidden_runs` と同じ（各 run 自身の `w:rPr` のみ・`w:val` の打ち消し・空 run とテキストボックス内部は除く）。1つの run が両方持っていても1回だけ拾う。取り消し線は本文の可視性を変えないので、テキストは既にホスト段落の全文に含まれる。
    """
    out: list[str] = []
    for r in _iter_skip_txbx(p_el, f"{_W}r"):
        r_pr = r.find(f"{_W}rPr")
        if r_pr is not None and (_toggle_true(r_pr.find(f"{_W}strike")) or _toggle_true(r_pr.find(f"{_W}dstrike"))):
            text = _run_text(r)
            if text:
                out.append(text)
    return out


def _run_state_label(r_el) -> str:
    """run 自身の `w:rPr` が持つ状態の平文ラベル（`非表示`／`取り消し線`／両方は `非表示・取り消し線`）。状態が無ければ空文字。判定は `hidden_runs`／`strike_runs` と同じ。"""
    r_pr = r_el.find(f"{_W}rPr")
    if r_pr is None:
        return ""
    labels = []
    if _toggle_true(r_pr.find(f"{_W}vanish")):
        labels.append("非表示")
    if _toggle_true(r_pr.find(f"{_W}strike")) or _toggle_true(r_pr.find(f"{_W}dstrike")):
        labels.append("取り消し線")
    return "・".join(labels)


def stated_text(p_el) -> str:
    """段落の本文。取り消し線・非表示の run は `【取り消し線：…】`／`【非表示：…】` と状態を添えて書く。

    ① run を文書順に拾う（テキストボックスの中は含めない）② 同じ状態が続く run を1つの括りにまとめる ③ 括りの前後の空白は括りの外へ出す。
    状態の意味（廃止など）は断定せず、原本が持つ状態だけを書く。
    """
    segments: list[list[str]] = []  # [状態ラベル, 連結文字列]
    for r in _iter_skip_txbx(p_el, f"{_W}r"):
        text = _run_text(r)
        if not text:
            continue
        label = _run_state_label(r)
        if segments and segments[-1][0] == label:
            segments[-1][1] += text
        else:
            segments.append([label, text])
    out: list[str] = []
    for label, text in segments:
        inner = text.strip()
        if not label or not inner:
            out.append(text)
            continue
        lead = text[:len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        out.append(f"{lead}【{label}：{inner}】{trail}")
    return "".join(out)


def deleted_runs(p_el) -> list[str]:
    """段落内の変更履歴「削除」ブロック（`w:del`）ごとの削除本文（`w:delText` の連結）を出現順で返す。

    `w:del` ブロック単位で1文字列（空は出さない）。挿入（`w:ins`）側は採用本文として段落テキストに含まれるため対象外。テキストボックス内部は対象外（`_iter_skip_txbx`）。
    """
    out: list[str] = []
    for d in _iter_skip_txbx(p_el, f"{_W}del"):
        text = "".join(t.text or "" for t in d.iter(f"{_W}delText"))
        if text:
            out.append(text)
    return out


def load_rels(zf: zipfile.ZipFile, part_name: str) -> dict[str, str]:
    """`part_name`（例 `"word/document.xml"`）に対応する `_rels/*.rels` から `{Id: Target}` を返す。パート・rels が無い/壊れている場合は空 dict。"""
    return {rel.id: rel.target for rel in load_relationships(zf.read, part_name) if rel.id}


def hyperlinks(p_el, rels: dict[str, str]) -> list[dict]:
    """段落内の `w:hyperlink` を出現順で返す（`{"text": <表示文字列>, "target": <URL または "#"+anchor>}`）。

    `target` は `r:id`（rels 経由の URL）優先、無ければ `w:anchor`（`"#" + anchor名`）。どちらも解決できない・rels の `Target` が空文字列のリンク要素は結果から省略する（表示文字列はホスト段落の全文に残る）。テキストボックス内部は対象外（`_iter_skip_txbx`）。
    """
    out: list[dict] = []
    for h in _iter_skip_txbx(p_el, f"{_W}hyperlink"):
        target = None
        rid = h.get(f"{_R}id")
        if rid:
            target = rels.get(rid) or None  # 空文字列 Target も未解決扱い
        if target is None:
            anchor = h.get(f"{_W}anchor")
            if anchor:
                target = "#" + anchor
        if target is None:
            continue
        text = "".join(t.text or "" for t in h.iter(f"{_W}t"))
        out.append({"text": text, "target": target})
    return out


def _textbox_contents(p_el):
    """段落内の文字を持つテキストボックスを `(w:txbxContent, 本文)` で出現順に yield する（`textboxes()` と `textbox_object_ids()` が共有）。"""
    for tb in _iter_one_alternative(p_el, f"{_W}txbxContent"):
        paras = [t for t in (stated_text(p) for p in tb.findall(f"{_W}p")) if t]
        if paras:
            yield tb, "\n".join(paras)


def textboxes(p_el) -> list[str]:
    """段落内のテキストボックス本文（`w:txbxContent`）を出現順で返す（1テキストボックス=1文字列・内部の段落は `"\\n"` 結合）。

    `w:txbxContent` は VML・DrawingML のどちらの図形経由でも WordprocessingML 名前空間（`_W`）の要素名で現れるため、1つの名前空間で両形式を拾える。内部の段落は `stated_text` で取り出し、取り消し線・非表示の run は本文と同じ書き方（状態つき）にする。
    """
    return [text for _tb, text in _textbox_contents(p_el)]


_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_V = "{urn:schemas-microsoft-com:vml}"
# テキストボックスを持ちうる VML の図形（group は子の図形を含むだけで自身はテキストボックスを持たない）
_VML_FIGURES = frozenset(f"{_V}{name}" for name in ("shape", "rect", "roundrect", "oval", "line", "polyline", "curve", "arc"))


def _enclosing_figure(node, parents, stop):
    """`node`（`w:txbxContent`）を実際に囲む直近の図形（DrawingML は `wp:anchor`／`wp:inline`、VML は `v:rect`・`v:shape` など）。"""
    node = parents.get(node)
    while node is not None and node is not stop:
        if node.tag in (f"{_WP}anchor", f"{_WP}inline") or node.tag in _VML_FIGURES:
            return node
        node = parents.get(node)
    return None


def _figure_id(figure) -> str | None:
    if figure is None:
        return None
    if figure.tag in (f"{_WP}anchor", f"{_WP}inline"):
        props = figure.find(f"{_WP}docPr")
        return props.get("id") if props is not None else None
    return figure.get("id")


def textbox_object_ids(p_el) -> list[list[str]]:
    """`textboxes()` と同じ順・同じ個数で、各テキストボックスを囲む図形の原本 ID（`wp:docPr/@id`・VML の図形の `@id`）。

    テキストボックスを実際に囲む直近の図形の ID だけを返す（グループ全体の ID を子に配らない）。
    `mc:AlternateContent` の中では、Choice と Fallback のテキストボックスがどちらも 1 つのときだけ、Fallback の図形の ID も足す
    （同じ図形の代替表現）。複数あるときは順番だけでは対応が確かめられないので、自身を囲む図形の ID だけにする。
    Evidence IR の図形（浮動テキストボックス・VML）と同じ原本の図形かを、文字列ではなく ID で確かめるために使う。
    """
    parents = {child: parent for parent in p_el.iter() for child in parent}

    def alternate_of(tb):
        node = parents.get(tb)
        while node is not None and node is not p_el:
            if node.tag == f"{_MC}AlternateContent":
                return node
            node = parents.get(node)
        return None

    def contents_in(branch) -> list:
        return list(branch.iter(f"{_W}txbxContent"))

    out: list[list[str]] = []
    for tb, _text in _textbox_contents(p_el):
        own = _figure_id(_enclosing_figure(tb, parents, p_el))
        ids = [own] if own else []
        alternate = alternate_of(tb)
        if alternate is not None:
            choice, fallback = alternate.find(f"{_MC}Choice"), alternate.find(f"{_MC}Fallback")
            if choice is not None and fallback is not None:
                chosen, others = contents_in(choice), contents_in(fallback)
                if len(chosen) == 1 and len(others) == 1 and tb in chosen:
                    other_id = _figure_id(_enclosing_figure(others[0], parents, p_el))
                    if other_id and other_id not in ids:
                        ids.append(other_id)
        out.append(ids)
    return out


def _iter_one_alternative(el, tag):
    """`el` 配下の `tag` 要素を文書順で yield する。`mc:AlternateContent` は最初の `mc:Choice`（無ければ `mc:Fallback`）だけを辿る。

    守ること: 同じ図形の代替表現を両方辿らない（同じテキストボックスが二重に出る）。
    """
    for child in el:
        if child.tag == f"{_MC}AlternateContent":
            branch = child.find(f"{_MC}Choice")
            if branch is None:
                branch = child.find(f"{_MC}Fallback")
            if branch is not None:
                yield from _iter_one_alternative(branch, tag)
            continue
        if child.tag == tag:
            yield child
        yield from _iter_one_alternative(child, tag)


def part_paragraphs(zf: zipfile.ZipFile, name: str) -> list[str]:
    """zip パート（`header*.xml`／`footer*.xml` 等・段落がフラットに並ぶ形式）配下の非空段落テキスト（`stated_text`＝取り消し線・非表示の run は状態つき）を出現順で返す。パート欠落/壊れは空リスト。"""
    try:
        root = ET.fromstring(zf.read(name))
    except (KeyError, ET.ParseError):
        return []
    return [t for t in (stated_text(p) for p in root.iter(f"{_W}p")) if t]


def _natural_part_key(name: str) -> tuple[int, str]:
    """`header10.xml` が `header2.xml` の後に来る自然順キー（数値 suffix 順）。"""
    m = re.search(r"(\d+)\.xml$", name)
    return (int(m.group(1)) if m else 0, name)


def header_footer_names(zf: zipfile.ZipFile) -> tuple[list[str], list[str]]:
    """zip 内の `word/header*.xml`／`word/footer*.xml` パート名を数値 suffix の自然順で返す。"""
    names = zf.namelist()
    headers = sorted((n for n in names if re.fullmatch(r"word/header\d+\.xml", n)), key=_natural_part_key)
    footers = sorted((n for n in names if re.fullmatch(r"word/footer\d+\.xml", n)), key=_natural_part_key)
    return headers, footers


def _para_with_textboxes(p_el) -> list[str]:
    """段落の本文（状態つき）と、その段落の中のテキストボックスの本文を出現順で返す（`stated_text` はテキストボックスの中を含めないため）。"""
    return [stated_text(p_el), *textboxes(p_el)]


def _notes(zf: zipfile.ZipFile, part_name: str, note_tag: str) -> list[dict]:
    """`footnotes`/`endnotes` 共通実装（`{"note_id": <int|str>, "text": <段落 "\\n" 結合>}`）。

    区切り線（`w:type="separator"`/`"continuationSeparator"`）と本文が空のノートは出さない。`w:id` は数値化できれば int、できなければ文字列のまま。
    """
    try:
        root = ET.fromstring(zf.read(part_name))
    except (KeyError, ET.ParseError):
        return []
    out: list[dict] = []
    for note in root.findall(note_tag):
        if note.get(f"{_W}type") in _NOTE_SEPARATOR_TYPES:
            continue
        paras = [t for p in _iter_skip_txbx(note, f"{_W}p") for t in _para_with_textboxes(p) if t]  # 表・コンテンツコントロールの中の段落も拾う
        text = "\n".join(paras)
        if not text:
            continue
        raw_id = note.get(f"{_W}id")
        try:
            note_id: int | str | None = int(raw_id)
        except (TypeError, ValueError):
            note_id = raw_id
        out.append({"note_id": note_id, "text": text})
    return out


def footnotes(zf: zipfile.ZipFile) -> list[dict]:
    """`word/footnotes.xml` の各脚注（区切り線を除く）を出現順で返す。パート欠落は []。"""
    return _notes(zf, "word/footnotes.xml", f"{_W}footnote")


def endnotes(zf: zipfile.ZipFile) -> list[dict]:
    """`word/endnotes.xml` 版（`footnotes` と同型）。パート欠落は []。"""
    return _notes(zf, "word/endnotes.xml", f"{_W}endnote")


def comments(zf: zipfile.ZipFile) -> list[dict]:
    """`word/comments.xml` の各コメントを出現順で返す（`{"comment_id": <int|str>, "author": str, "date": str, "text": <段落 "\\n" 結合>}`）。

    本文が空のコメントは出さない。`author`/`date` はファイル由来の属性値のまま。パート欠落/壊れは空リスト。
    """
    try:
        root = ET.fromstring(zf.read("word/comments.xml"))
    except (KeyError, ET.ParseError):
        return []
    out: list[dict] = []
    for c in root.findall(f"{_W}comment"):
        paras = [t for p in _iter_skip_txbx(c, f"{_W}p") for t in _para_with_textboxes(p) if t]  # 表・コンテンツコントロールの中の段落も拾う
        text = "\n".join(paras)
        if not text:
            continue
        raw_id = c.get(f"{_W}id")
        try:
            comment_id: int | str | None = int(raw_id)
        except (TypeError, ValueError):
            comment_id = raw_id
        out.append({"comment_id": comment_id, "author": c.get(f"{_W}author") or "",
                    "date": c.get(f"{_W}date") or "", "text": text})
    return out
