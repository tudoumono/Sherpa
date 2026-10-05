"""YAML 設定ファイルアナライザ。ファイル自体を主体定義（`Config`）とし、階層を `.` で連結した完全キー（例 `tax.rate`）を `DefItem(label="Config", name=<完全キー>, cid_key="key:property:" + <完全キー>)` の children として返す。参照候補は返さない（コード側の設定キー参照は `java.py`）。

外部ライブラリを使わない最小パーサ（インデントによる階層のみを見る）で読む。`cid_key` の `key:` 接頭辞は primary との cid 名前空間分離のため（`properties.py` と同じ）。
規則:
- `key: value`／`key:` を読み、インデントでスタック管理する。リーフ（`key: value`）だけを children にし、`key:` のみの見出しは積まない。
- シーケンス（`- ` 始まり）は階層に含めず、基準インデント以下に dedent するまで配下を読み飛ばす。
- `---` はファイル先頭なら1個目の文書の開始として無視する。実コンテンツを読んだ後に現れたら2個目以降の文書として、以降を `Dropped("yaml_unsupported")` で1回申告して読まない。
- 引用付きキーは引用を外す。
- アンカー/エイリアス・ブロックスカラー（`|`/`>`）・タブ字下げ・フローコレクション（`{...}`/`[...]`）は `Dropped("yaml_unsupported")` で申告する（ブロックスカラー本文は読み飛ばす）。アンカー判定は引用スカラーの外だけで行う。
各キー child は `extra["key_kind"] = "property"` を持つ。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, Dropped, RefResult

YAML_EXT = frozenset({".yaml", ".yml"})

_YAML_KV = re.compile(
    r'^(?P<indent>[ \t]*)(?P<key>"[^"]*"|\'[^\']*\'|[A-Za-z0-9_.\-]+)\s*:\s*(?P<value>.*)$')
_BLOCK_SCALAR = re.compile(r'^[|>][+\-]?\d*\s*(?:#.*)?$')
_ANCHOR_OR_ALIAS = re.compile(r'(?:^|\s)[&*][^\s#]+')
_FLOW_COLLECTION = re.compile(r'^[\{\[]')


def _dequote(key: str) -> str:
    if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
        return key[1:-1]
    return key


def _is_fully_quoted_scalar(value: str) -> bool:
    """値全体が単一の引用符（`"..."`/`'...'`）で囲まれた1個のスカラーか。"""
    return len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]


def _iter_yaml_children(text: str):
    """完全キー（`.` 連結・裸）を `(line_no, full_key)` で、サポート外構文を `(line_no, snippet)` で返す。"""
    key_stack: list = []  # [(indent, key), ...]
    skip_block_indent: int | None = None  # ブロックスカラー本文をスキップ中の基準インデント
    skip_seq_indent: int | None = None  # シーケンス項目の配下をスキップ中の基準インデント
    in_extra_doc = False  # 2個目以降のドキュメント本文中か
    content_seen = False  # 現在の文書で実コンテンツ行を既に読んだか
    hits: list = []
    unsupported: list = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if stripped == "...":
            continue
        if stripped == "---":
            # 実コンテンツをまだ読んでいなければファイル先頭の文書開始として無視し、既に読んでいれば2個目以降の文書＝サポート外として打ち切る。
            if content_seen and not in_extra_doc:
                in_extra_doc = True
                unsupported.append((line_no, "---"))
            continue
        if in_extra_doc:
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        if skip_block_indent is not None:
            if not stripped or indent > skip_block_indent:
                continue  # ブロックスカラーの本文行（キーとして解釈しない）
            skip_block_indent = None  # dedent＝ブロック終了
        if skip_seq_indent is not None:
            if not stripped or indent > skip_seq_indent:
                continue  # シーケンス項目の配下（本体・入れ子とも読み飛ばす）
            skip_seq_indent = None  # dedent＝シーケンス項目終了
        if not stripped or stripped.startswith("#"):
            continue
        content_seen = True
        leading_ws = raw[:indent]
        if "\t" in leading_ws:  # タブ判定はシーケンス項目判定より先に行う
            unsupported.append((line_no, stripped[:120]))
            continue
        if stripped.startswith("- "):
            skip_seq_indent = indent  # このインデントより深い配下を丸ごと読み飛ばす
            continue
        m = _YAML_KV.match(raw)
        if not m:
            continue
        key = _dequote(m.group("key"))
        value = m.group("value").strip()
        while key_stack and key_stack[-1][0] >= indent:
            key_stack.pop()
        key_stack.append((indent, key))
        full_key = ".".join(k for _, k in key_stack)
        if _BLOCK_SCALAR.match(value):
            unsupported.append((line_no, value[:120]))
            skip_block_indent = indent
            continue
        if _FLOW_COLLECTION.match(value):
            unsupported.append((line_no, value[:120]))
            continue
        if not _is_fully_quoted_scalar(value) and _ANCHOR_OR_ALIAS.search(" " + value):
            unsupported.append((line_no, value[:120]))
            continue
        if value == "":
            continue  # マップ見出し（`key:` のみ）＝リーフではない
        hits.append((line_no, full_key))
    return hits, unsupported


class YamlConfigAnalyzer(Analyzer):
    """ファイル自体 → `Config`（primary）。階層の完全キー → `Config`（children・`primary -CONTAINS-> child`）。"""

    name = "yaml_config"
    extensions = YAML_EXT
    doctype = "yaml_config"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        primary = DefItem(label="Config", name=PurePosixPath(rel_path).name)
        hits, unsupported = _iter_yaml_children(text)
        children = [DefItem(label="Config", name=key, line=line_no,
                            cid_key=f"key:property:{key}",
                            extra={"key_kind": "property"})
                    for line_no, key in hits]
        dropped = [Dropped("yaml_unsupported", line_no, snippet)
                   for line_no, snippet in unsupported]
        return DefResult(primary=primary, children=children, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        return RefResult()
