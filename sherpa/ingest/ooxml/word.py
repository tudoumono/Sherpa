"""Word（.docx）の生 OOXML 抽出層。`arms/ooxml_arm._build_docx_ir` が消費する純関数群で、MD が表示しない構造（隠し文字・変更履歴の削除本文・ハイパーリンク先・テキストボックス・脚注・コメント・ヘッダ/フッタ）を一度だけ抽出する。

- 隠し文字（`w:vanish`）・テキストボックス（`w:txbxContent`）・採用本文（`w:ins`）は、`office_md._para_text` が全子孫の `w:t` を拾うため既にホスト段落の全文に含まれる。ここではそのうち隠し/テキストボックス由来の部分を追加で切り出す。
- 削除本文（`w:del//w:delText`）は `_para_text` が拾わないため、ここで独立に取り出す。

`xml.etree.ElementTree` ベースの決定的な純関数。壊れた/欠落した OOXML パートは例外を投げず空リストへ縮退する。
設計: docs/design/rag.md「アーム一覧」
"""
from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# footnote/endnote の区切り線・継続区切り線（本文ではない・IR には出さない）。
_NOTE_SEPARATOR_TYPES = {"separator", "continuationSeparator"}


def _run_text(r_el) -> str:
    """1つの `w:r`（run）内の `w:t` テキスト連結。"""
    return "".join(t.text or "" for t in r_el.iter(f"{_W}t"))


def _paragraph_text(p_el) -> str:
    """1段落（脚注/コメント/ヘッダ/フッタの `w:p` を含む）の `w:t` テキスト連結。

    `office_md._para_text` と同じ方針（入れ子含め全子孫の `w:t`）。`office_md` には依存しない（循環 import を避ける）。
    """
    return "".join(t.text or "" for t in p_el.iter(f"{_W}t"))


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
    from posixpath import basename, dirname, join
    rels_name = join(dirname(part_name), "_rels", basename(part_name) + ".rels")
    try:
        root = ET.fromstring(zf.read(rels_name))
    except (KeyError, ET.ParseError):
        return {}
    return {r.get("Id"): r.get("Target") for r in root.iter(f"{_RELS}Relationship") if r.get("Id")}


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


def textboxes(p_el) -> list[str]:
    """段落内のテキストボックス本文（`w:txbxContent`）を出現順で返す（1テキストボックス=1文字列・内部の段落は `"\\n"` 結合）。

    `w:txbxContent` は VML・DrawingML のどちらの図形経由でも WordprocessingML 名前空間（`_W`）の要素名で現れるため、1つの名前空間で両形式を拾える。同内容は `_paragraph_text` にも含まれる。
    """
    out: list[str] = []
    for tb in p_el.iter(f"{_W}txbxContent"):
        paras = [t for t in (_paragraph_text(p) for p in tb.findall(f"{_W}p")) if t]
        if paras:
            out.append("\n".join(paras))
    return out


def part_paragraphs(zf: zipfile.ZipFile, name: str) -> list[str]:
    """zip パート（`header*.xml`／`footer*.xml` 等・段落がフラットに並ぶ形式）配下の非空段落テキストを出現順で返す。パート欠落/壊れは空リスト。"""
    try:
        root = ET.fromstring(zf.read(name))
    except (KeyError, ET.ParseError):
        return []
    return [t for t in (_paragraph_text(p) for p in root.iter(f"{_W}p")) if t]


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
        paras = [t for t in (_paragraph_text(p) for p in note.findall(f"{_W}p")) if t]
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
        paras = [t for t in (_paragraph_text(p) for p in c.findall(f"{_W}p")) if t]
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
