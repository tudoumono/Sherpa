"""XML 設定アナライザ（本体）。標準の `xml.parsers.expat` で読み、設定の種別（`config_kind`）とファイル自体の主体定義（`Config`）だけを返す。

FW 固有の規則（Spring の `<bean>`・`<import resource>`・Spring Batch／MyBatis の mapper・SQL・型／Struts の action）は FW プラグイン
（`xml_config_fw.py`・`docs/21-拡張の契約.md` §3a・§3b）が本体の結果へ足す。本体は FW の知識を持たない（増設方針③）。

`.xml` は全件受理する。設定 XML と判定できたファイル（`config_kind` が `None` でない）だけ primary を持ち、判定できないもの（pom・web.xml 等）は
primary なし＋`Dropped("xml_not_config")`。壊れた XML は `Dropped("xml_parse_error")`（種別は `None`＝プラグインも適用しない）。
種別はルート要素のローカル名で判定する（namespace URI・prefix は問わない・外部 DTD は読まない）。種別の名前は閉じた集合 `CONFIG_KINDS`。

汎用の読み取り `parse_xml` は、要素ツリー（`XmlElement`＝名前空間 URI・ローカル名・属性・開始行・深さ・親・文書順の内容）を 1 パスで作る。
行番号は expat の `CurrentLineNumber`（コメント・CDATA の中身で行がずれない）。外部実体は展開しない（パラメータ実体展開を無効化し、
外部実体参照ハンドラは常に拒否する）。小さい本文（256KiB 相当まで）の木は直近の数件だけ使い回す（読み取り専用として扱う）。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import xml.parsers.expat as expat
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, Dropped, RefResult

XML_CONFIG_EXT = frozenset({".xml"})

# ルート要素のローカル名 → 設定の種別（閉じた集合・namespace URI/prefix は無視）。
_ROOT_KINDS = {"beans": "spring_beans", "mapper": "mybatis_mapper", "struts": "struts"}
CONFIG_KINDS = frozenset(_ROOT_KINDS.values())


def split_ns(tag: str) -> tuple:
    """expat が返す `"URI local"`（名前空間あり）または `"local"`（無名前空間）から `(URI または None, local)` を取り出す。"""
    if " " in tag:
        uri, local = tag.rsplit(" ", 1)
        return uri, local
    return None, tag


class XmlElement:
    """要素 1 件。`content`＝文書順の子（`XmlElement` または文字データ `(行, テキスト)`）。`depth`＝ルートが 1。"""

    __slots__ = ("ns", "local", "attrs", "line", "depth", "parent", "content")

    def __init__(self, ns, local, attrs, line, depth, parent):
        self.ns, self.local, self.attrs, self.line, self.depth, self.parent = ns, local, attrs, line, depth, parent
        self.content: list = []

    def walk(self):
        """この要素から文書順（先行順）にすべての要素を返す。"""
        stack = [self]
        while stack:
            el = stack.pop()
            yield el
            stack.extend(reversed([c for c in el.content if isinstance(c, XmlElement)]))

    def text_frags(self) -> list:
        """この要素の配下の文字データを文書順に `[(開始行, テキスト), ...]` で返す（子要素は展開しない・テキストだけ）。"""
        out: list = []
        stack = [iter(self.content)]
        while stack:
            for c in stack[-1]:
                if isinstance(c, XmlElement):
                    stack.append(iter(c.content))
                    break
                out.append(c)
            else:
                stack.pop()
        return out


@dataclass(frozen=True)
class XmlDocument:
    """`parse_xml` の結果。`root`＝ルート要素（読めなければ `None`）・`error`＝構文エラー（無ければ `None`）。"""

    root: XmlElement | None
    error: expat.ExpatError | None = None


# 木を使い回す本文の上限（文字数）。これより大きい本文は毎回読み、木を常駐させない。
_CACHE_MAX_CHARS = 256 * 1024


def parse_xml(text: str) -> XmlDocument:
    """`text` を expat で 1 回走査して要素ツリーを作る。構文エラーのときは `error` を持つ（そのときの `root` は使わない）。小さい本文だけ木を使い回す。"""
    return _parse_cached(text) if len(text) <= _CACHE_MAX_CHARS else _parse(text)


@lru_cache(maxsize=4)
def _parse_cached(text: str) -> XmlDocument:
    return _parse(text)


def _parse(text: str) -> XmlDocument:
    root: XmlElement | None = None
    stack: list = []
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.ExternalEntityRefHandler = lambda context, base, system_id, public_id: 0

    def _start(name, attrs):
        nonlocal root
        ns_uri, local = split_ns(name)
        parent = stack[-1] if stack else None
        el = XmlElement(ns_uri, local, attrs, parser.CurrentLineNumber, len(stack) + 1, parent)
        if parent is None:
            root = el
        else:
            parent.content.append(el)
        stack.append(el)

    def _end(name):
        stack.pop()

    def _chars(data):
        if stack:
            stack[-1].content.append((parser.CurrentLineNumber, data))

    parser.StartElementHandler = _start
    parser.EndElementHandler = _end
    parser.CharacterDataHandler = _chars
    try:
        parser.Parse(text, True)
    except expat.ExpatError as exc:
        return XmlDocument(root=None, error=exc)
    return XmlDocument(root=root)


def line_for_offset(frags: list, offset: int) -> int:
    """`frags`（`(開始line, テキスト)` の列）から、連結後テキスト中の `offset` 位置が属する物理行番号を返す。"""
    pos = 0
    for start_line, frag_text in frags:
        end = pos + len(frag_text)
        if offset < end:
            return start_line + frag_text[:offset - pos].count("\n")
        pos = end
    if frags:
        start_line, frag_text = frags[-1]
        return start_line + frag_text.count("\n")
    return 1


class XmlConfigAnalyzer(Analyzer):
    """設定 XML → `Config`（primary）。キー単位の `Config` children・参照は FW プラグイン（`xml_config_fw.py`）が足す。設定でない XML は primary なし＋`Dropped("xml_not_config")`。"""

    name = "xml_config"
    extensions = XML_CONFIG_EXT
    doctype = "xml_config"
    # 本体の解析結果が変わる変更をしたときに上げる（`registry.config_signature()` の材料。上げると既存の資料フォルダが全再構築される）。
    version = 3

    def config_kind(self, text: str, rel_path: str) -> str | None:
        doc = parse_xml(text)
        if doc.error is not None or doc.root is None:
            return None
        return _ROOT_KINDS.get(doc.root.local)

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        doc = parse_xml(text)
        if doc.error is not None:
            line = getattr(doc.error, "lineno", None) or 1
            return DefResult(dropped=[Dropped("xml_parse_error", line, str(doc.error)[:120])])
        root_name = doc.root.local if doc.root is not None else None
        if root_name is None or root_name not in _ROOT_KINDS:
            return DefResult(dropped=[Dropped("xml_not_config", 1, root_name or "")])
        return DefResult(primary=DefItem(label="Config", name=PurePosixPath(rel_path).name))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        return RefResult()
