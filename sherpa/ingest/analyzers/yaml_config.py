"""YAML 設定ファイルアナライザ（アナライザ拡張 S3'・A7 案B＝キー単位 `Config` children）。

`.yaml`/`.yml` はファイル自体を主体定義（`Config`・S3）にしたうえで、外部ライブラリを使わない
**最小パーサ**（インデントによる階層のみを見る・完全な YAML 文法は解釈しない）でキーを読み、
階層を `.` で連結した完全キー（例 `tax.rate`）を
`DefItem(label="Config", name=<完全キー>, cid_key="key:property:" + <完全キー>)` として children に
返す（RV2-2＝ラベルは親と同じ `Config`）。`cid_key` に `"key:"` 接頭辞を付けるのは properties.py と
同じ理由——完全キー自身がファイル名と一致するとき primary と cid が衝突しないようにするため
（`name` は裸の完全キーのまま・構造参照・言及辞書の索引は変えない）。続く `key_kind`（常に
`"property"`）は xml_config.py 等の他の Config キー producer と cid 形式を揃える（波3 統合 RV）。

パーサの規則（`scripts/syntax_profile.py` の `_process_yaml` と同じ規則・コピーはしない）:
- `key: value`／`key:`（マップ見出し・値なし）を読み、インデントでスタック管理する
  （現在行のインデント以上のスタック要素を pop してから push＝親を辿り直す）。
- **リーフのみを children に積む**——`key:` だけの行（マップ見出し）はスタックには積むが
  children には出さない（値を持たない中間キー自体を設定キーとしては扱わない）。
  `key: value` のスカラーだけがリーフとして children になる。
- シーケンス（`- ` で始まる行）は**階層に含めず親キーで打ち切る**——その行のインデントを基準に、
  それより深くインデントされた行（シーケンス項目自身の入れ子構造・`- host: a` に続く
  `  port: 80` 等）を含め、基準インデント以下に dedent するまで配下全体を読み飛ばす
  （スタックへの push は行わない・`servers.port` のような誤ったキーを作らない）。
- `---`（ドキュメント区切り）は、まだ実コンテンツ行を読んでいない時点（ファイル先頭）に現れれば
  「1個目の文書の明示的な開始」として無視する。既に実コンテンツを読んだ後に現れれば**2個目以降の
  文書の開始**と判定し、その行以降（EOF まで）は `Dropped("yaml_unsupported")` で1回申告して
  階層に入れない（`...` はドキュメント終端の目印として常に無視するだけ・この判定に使わない）。
- 引用付きキー（`"a.b"`/`'a.b'`）は引用を外す。
- アンカー/エイリアス（`&name`/`*name`）・複数行スカラー（`|`/`>` のブロックスカラー記法）は
  最小パーサでは解釈できないため `Dropped("yaml_unsupported")` で申告する（黙って誤解釈しない）。
  ブロックスカラーはヘッダ行より深いインデントの本文行をキーとして誤読しないよう読み飛ばす。
  アンカー/エイリアス判定は**引用スカラーの外だけ**で行う（値全体が単一の引用符で囲まれた
  スカラーなら中身は検査しない——`message: "foo &bar"` は通常の文字列値であり、YAML アンカーではない）。
- タブでインデントされた行（キー候補・シーケンス項目を問わず）は、空白とタブの混在で階層の
  対応関係が不定になるため `Dropped("yaml_unsupported")` で申告し、キーとして解釈しない。
- フローコレクション（値が `{...}`/`[...]` で始まる）は最小パーサでは展開しないため
  `Dropped("yaml_unsupported")` で申告し、リーフとして扱わない。

各キー child は `extra["key_kind"] = "property"` も持つ（Config キーは種別で名前空間を分ける・
properties.py と同じ扱い——共通層 A9 の `config_key_index` はこの値も索引キーに含める）。

参照候補は返さない（コード側の設定キー参照抽出は `java.py` 側・§4(b) 案B）。
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
    """完全キー（`.` 連結・裸）を `(line_no, full_key)` で、サポート外構文を
    `(line_no, snippet)` で返す。"""
    key_stack: list = []                 # [(indent, key), ...]
    skip_block_indent: int | None = None  # ブロックスカラー本文をスキップ中の基準インデント
    skip_seq_indent: int | None = None    # シーケンス項目の配下をスキップ中の基準インデント
    in_extra_doc = False                  # 2個目以降のドキュメント本文中か
    content_seen = False                  # 現在の文書で実コンテンツ行を既に読んだか（先頭の
                                           # `---` を「1個目の文書開始」と区別するために使う）
    hits: list = []
    unsupported: list = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if stripped == "...":
            continue
        if stripped == "---":
            # 実コンテンツをまだ読んでいなければ（ファイル先頭の明示的な文書開始）1個目の文書と
            # みなす。既に読んでいれば、そこから先は2個目以降の文書＝サポート外として打ち切る。
            if content_seen and not in_extra_doc:
                in_extra_doc = True
                unsupported.append((line_no, "---"))
            continue
        if in_extra_doc:
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        if skip_block_indent is not None:
            if not stripped or indent > skip_block_indent:
                continue                  # ブロックスカラーの本文行（キーとして解釈しない）
            skip_block_indent = None      # dedent＝ブロック終了
        if skip_seq_indent is not None:
            if not stripped or indent > skip_seq_indent:
                continue                  # シーケンス項目の配下（本体・入れ子とも読み飛ばす）
            skip_seq_indent = None        # dedent＝シーケンス項目終了
        if not stripped or stripped.startswith("#"):
            continue
        content_seen = True
        leading_ws = raw[:indent]
        if "\t" in leading_ws:             # シーケンス項目かどうかより先にタブ判定する——
            unsupported.append((line_no, stripped[:120]))  # タブ字下げのシーケンス項目が
            continue                       # `skip_seq_indent` 経由で黙って読み飛ばされないため
        if stripped.startswith("- "):
            skip_seq_indent = indent      # このインデントより深い配下を丸ごと読み飛ばす
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
            continue                      # マップ見出し（`key:` のみ）＝リーフではない
        hits.append((line_no, full_key))
    return hits, unsupported


class YamlConfigAnalyzer(Analyzer):
    """ファイル自体 → `Config`（primary）。階層キーの完全キー（`.` 連結）→ `Config`
    （children・`primary -CONTAINS-> child`・索引キーは裸の完全キー・cid は `"key:"` 接頭辞で
    primary と分離）。"""

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
