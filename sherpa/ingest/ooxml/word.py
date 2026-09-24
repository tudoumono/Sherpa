"""Word（.docx）の生 OOXML 抽出層（DOC-IR-002・パッケージ docstring＝`sherpa/ingest/ooxml/__init__.py` 参照）。

`arms/ooxml_arm._build_docx_ir` が消費する純関数群。**MD が表示しない構造**（隠し文字・変更履歴の削除
本文・ハイパーリンク先・テキストボックス・脚注・コメント・ヘッダ/フッタ）をここで一度だけ抽出する:

- 隠し文字（`w:vanish`）・テキストボックス（`w:txbxContent`）・採用本文（`w:ins`）は、`office_md._para_text`
  が段落配下の**全子孫** `w:t` を無差別に拾う（`.iter(w:t)`）ため、**既にホスト段落の全文テキストへ含まれて
  いる**（MD parity）。本モジュールはそこから「どの部分が隠し/テキストボックス由来か」を追加で切り出す。
- 削除本文（`w:del//w:delText`）は `w:t` ではなく `w:delText` のため、`_para_text` は元々これを拾わない
  （MD は採用本文のみ）。本モジュールで独立に取り出す。

すべて純関数・`xml.etree.ElementTree` ベース・LLM 不使用・決定的。壊れた/欠落した OOXML パート
（`footnotes.xml` 等）は例外を投げず空リストへ縮退する（呼び出し側の fail-safe 方針と一致・
`arms/ooxml_arm.py` の IR 構築は「1文書分の失敗を握って md 継続」という契約を持つため、ここでも
個々のパート単位で同じ流儀を踏襲する）。
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
    """1つの `w:r`（run）内の `w:t` テキスト連結（run は入れ子構造を持たない前提・保険で `.iter` を使う）。"""
    return "".join(t.text or "" for t in r_el.iter(f"{_W}t"))


def _paragraph_text(p_el) -> str:
    """1段落（脚注/コメント/ヘッダ/フッタの `w:p` を含む）の `w:t` テキスト連結。

    `office_md._para_text` と同じ方針（`.iter(w:t)`＝入れ子含め全子孫を拾う）。本モジュールは `office_md`
    に依存しない（循環 import を避ける・パッケージ docstring の位置付け上、MD 側の実装を変えずに独立して
    同じ考え方を再利用する）。
    """
    return "".join(t.text or "" for t in p_el.iter(f"{_W}t"))


_FALSY_TOGGLE_VALS = {"0", "false", "off"}


def _iter_skip_txbx(el, tag):
    """`el` 配下の `tag` 要素を文書順で yield する。ただし `w:txbxContent` サブツリーへは**降りない**。

    テキストボックス内部の run/リンク等をホスト段落側の抽出（`hidden_runs`／`deleted_runs`／`hyperlinks`）
    から除外するための walker（RV Med #2: 同一テキストが textbox 要素と hidden_text 等で二重に出るのを防ぐ。
    テキストボックス内部の構造は `textboxes()` が本文として一括で持つ＝内部の隠し/削除の個別マーク付けは
    現時点の簡略化として行わない・docstring で明示）。`.iter(tag)` と同じく一致要素の子孫も続けて歩く。
    """
    for child in el:
        if child.tag == f"{_W}txbxContent":
            continue
        if child.tag == tag:
            yield child
        yield from _iter_skip_txbx(child, tag)


def _toggle_true(el) -> bool:
    """OOXML の真偽トグル要素（`w:vanish` 等）: 要素が存在し `w:val` が明示的な偽値でなければ真
    （`w:val` 省略＝真がデフォルト・ECMA-376 の `ST_OnOff` 既定値）。"""
    if el is None:
        return False
    return (el.get(f"{_W}val") or "").strip().lower() not in _FALSY_TOGGLE_VALS


def hidden_runs(p_el) -> list[str]:
    """段落内の隠し文字 run（`w:rPr/w:vanish` を持つ `w:r`）のテキストを出現順で返す。

    `w:vanish` は run 単位の書式（`w:rPr` 内）。判定は各 run **自身の** `w:rPr` のみを見る（段落既定の
    `w:pPr/w:rPr` は対象外＝run ごとの明示指定のみを隠しとみなす・保守的）。`w:val="0"/"false"/"off"` で
    明示的に打ち消された `w:vanish` は隠しと扱わない（`_toggle_true`）。テキストが空の隠し run は
    出さない（意味を持たないマーカーの水増しを避ける）。
    テキストボックス（`w:txbxContent`）内部の隠し run は対象外（`_iter_skip_txbx`・textbox 要素側が本文として
    持つ＝二重表現を避ける・RV Med #2）。
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
    """段落内の取り消し線 run（`w:rPr/w:strike` または `w:rPr/w:dstrike` を持つ `w:r`）のテキストを
    出現順で返す（`hidden_runs` と同じ設計＝判定は各 run **自身の** `w:rPr` のみを見る・段落既定の
    `w:pPr/w:rPr` は対象外）。取り消し線は `w:vanish`（隠し文字）と異なり本文の可視性を変えない
    （`office_md._para_text`/`_paragraph_text` は元々 `w:t` を無差別に拾うため、この run のテキストは
    既にホスト段落の全文テキストに含まれている＝`hidden_runs` と同じ「MD parity」の関係）。1つの run が
    `w:strike`/`w:dstrike` を両方持っていても1回だけ拾う。`w:val="0"/"false"/"off"` で明示的に打ち消された
    場合は取り消し線と扱わない（`_toggle_true`）。テキストが空の run は出さない。テキストボックス
    （`w:txbxContent`）内部は対象外（`_iter_skip_txbx`・`hidden_runs` と同じ理由）。
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

    `w:del` ブロック単位で1文字列（1段落に複数の削除箇所が散在していれば複数エントリになる）。空文字列
    （空の削除ブロック）は出さない。挿入（`w:ins`）側は対象外＝採用本文として段落テキストに既に含まれる。
    テキストボックス内部（`w:txbxContent`）は対象外（`_iter_skip_txbx`・`hidden_runs` と同じ理由）。
    """
    out: list[str] = []
    for d in _iter_skip_txbx(p_el, f"{_W}del"):
        text = "".join(t.text or "" for t in d.iter(f"{_W}delText"))
        if text:
            out.append(text)
    return out


def load_rels(zf: zipfile.ZipFile, part_name: str) -> dict[str, str]:
    """`part_name`（例 `"word/document.xml"`）に対応する `_rels/*.rels` から `{Id: Target}` を返す。

    パート・rels のいずれかが無い/壊れている場合は空 dict（fail-safe＝ハイパーリンク解決が全滅するだけで
    IR 構築自体は継続する）。
    """
    from posixpath import basename, dirname, join
    rels_name = join(dirname(part_name), "_rels", basename(part_name) + ".rels")
    try:
        root = ET.fromstring(zf.read(rels_name))
    except (KeyError, ET.ParseError):
        return {}
    return {r.get("Id"): r.get("Target") for r in root.iter(f"{_RELS}Relationship") if r.get("Id")}


def hyperlinks(p_el, rels: dict[str, str]) -> list[dict]:
    """段落内の `w:hyperlink` を出現順で返す（`{"text": <表示文字列>, "target": <URL または "#"+anchor>}`）。

    `target` の解決順は `r:id`（外部リレーションシップ＝rels 経由の URL）優先、無ければ `w:anchor`
    （文書内ブックマーク参照＝`"#" + anchor名`）。**いずれも解決できない場合（`r:id` が rels に無い＝
    壊れた関係、または `r:id`/`w:anchor` のどちらも無い）は、その hyperlink 要素自体を結果から省略する**
    （設計判断: 遷移先の無いリンク要素は RAG にとって意味を持たない付随情報のノイズになるため。表示文字列
    自体はホスト段落の全文テキストに元々含まれておりロストしない）。
    テキストボックス内部（`w:txbxContent`）は対象外（`_iter_skip_txbx`・`hidden_runs` と同じ理由）。
    rels の `Target` が**空文字列**の場合も未解決として扱い、`w:anchor` へフォールバック（それも無ければ
    省略）する（RV Low #3: `target=""` の意味を持たないリンク要素を出さない）。
    """
    out: list[dict] = []
    for h in _iter_skip_txbx(p_el, f"{_W}hyperlink"):
        target = None
        rid = h.get(f"{_R}id")
        if rid:
            target = rels.get(rid) or None                  # 空文字列 Target も未解決扱い（RV Low #3）
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
    """段落内のテキストボックス本文（`w:txbxContent`）を出現順で返す（1テキストボックス=1文字列・
    内部の段落は `"\\n"` 結合）。

    `w:txbxContent` は VML（`w:pict/v:shape/v:textbox/w:txbxContent`）・DrawingML（`w:drawing/…/wps:txbx/
    w:txbxContent`）のいずれの図形経由でも WordprocessingML 名前空間（`_W`）の要素名で現れる（ECMA-376）
    ため、名前空間を1つ見るだけで両形式を拾える。ホスト段落の全文テキスト（`_paragraph_text`）にも既に
    同内容が含まれている（`.iter(w:t)` は入れ子の `txbxContent` 内 `w:t` も拾うため＝MD parity）。
    """
    out: list[str] = []
    for tb in p_el.iter(f"{_W}txbxContent"):
        paras = [t for t in (_paragraph_text(p) for p in tb.findall(f"{_W}p")) if t]
        if paras:
            out.append("\n".join(paras))
    return out


def part_paragraphs(zf: zipfile.ZipFile, name: str) -> list[str]:
    """zip パート（`header*.xml`／`footer*.xml` 等・段落がフラットに並ぶ形式）配下の非空段落テキストを
    出現順で返す。パート欠落/壊れは例外を投げず空リストへ縮退する（fail-safe）。
    """
    try:
        root = ET.fromstring(zf.read(name))
    except (KeyError, ET.ParseError):
        return []
    return [t for t in (_paragraph_text(p) for p in root.iter(f"{_W}p")) if t]


def _natural_part_key(name: str) -> tuple[int, str]:
    """`header10.xml` が `header2.xml` の後に来る自然順キー（数値 suffix 順・RV Low #4）。"""
    m = re.search(r"(\d+)\.xml$", name)
    return (int(m.group(1)) if m else 0, name)


def header_footer_names(zf: zipfile.ZipFile) -> tuple[list[str], list[str]]:
    """zip 内の `word/header*.xml`／`word/footer*.xml` パート名を数値 suffix の自然順で返す（決定的な走査順・
    `header1, header2, …, header10` の順＝辞書順だと `header10` が `header2` の前に来てしまう・RV Low #4）。"""
    names = zf.namelist()
    headers = sorted((n for n in names if re.fullmatch(r"word/header\d+\.xml", n)), key=_natural_part_key)
    footers = sorted((n for n in names if re.fullmatch(r"word/footer\d+\.xml", n)), key=_natural_part_key)
    return headers, footers


def _notes(zf: zipfile.ZipFile, part_name: str, note_tag: str) -> list[dict]:
    """`footnotes`/`endnotes` 共通実装（`{"note_id": <int|str>, "text": <段落 "\\n" 結合>}`）。

    区切り線（`w:type="separator"`/`"continuationSeparator"`）は本文ではないため除外する。本文が空の
    ノート（区切り線以外で内容が無い異常系）も出さない。`w:id` は数値化できればそのまま int、できなければ
    文字列のまま保持する（fail-safe）。
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
    """`word/footnotes.xml` の各脚注（区切り線を除く）を出現順で返す（`_notes` 参照）。パート欠落は []。"""
    return _notes(zf, "word/footnotes.xml", f"{_W}footnote")


def endnotes(zf: zipfile.ZipFile) -> list[dict]:
    """`word/endnotes.xml` 版（`footnotes` と同型）。パート欠落は []。"""
    return _notes(zf, "word/endnotes.xml", f"{_W}endnote")


def comments(zf: zipfile.ZipFile) -> list[dict]:
    """`word/comments.xml` の各コメントを出現順で返す
    （`{"comment_id": <int|str>, "author": str, "date": str, "text": <段落 "\\n" 結合>}`）。

    本文が空のコメントは出さない。`author`/`date` はファイル由来の属性値をそのまま使う（決定的・LLM 不使用）。
    パート欠落/壊れは空リストへ縮退する（fail-safe）。
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
