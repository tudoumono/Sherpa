"""名前の台帳の行を作る（取り込み）。名前の正規化と、番号だけが違う近い名前の判定もここに持つ。
`declared`＝グラフのファイルの主体の定義（ラベルと名前とパス）、`build`＝ビルドの定義の実行名と材料のソース（`BUILT_FROM`）、`filename`＝ソースの枝のファイルの拡張子を除いた名前。秘匿名は入れない。
設計: docs/design/scope.md「期の中の参照の張り方と名前の台帳」
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

from . import text_kind

SOURCE_DECLARED = "declared"
SOURCE_FILENAME = "filename"
SOURCE_BUILD = "build"
PROGRAM_KINDS = frozenset({"Module", "Batch", "Copybook"})
_TAIL_DIGITS = re.compile(r"^(.*?)(\d+)$")


def normalize_name(name) -> str:
    """名前の正規化（全角・半角をそろえ、前後の空白を除き、大文字にする）。"""
    return unicodedata.normalize("NFKC", str(name or "")).strip().upper()


def near_key(name: str) -> tuple | None:
    """正規化済みの名前を `(末尾の数字の手前, 末尾の数字の桁数)` にする。末尾が数字でない・手前が空なら None。"""
    m = _TAIL_DIGITS.match(name)
    return (m.group(1), len(m.group(2))) if m and m.group(1) else None


def is_near(name: str, other: str) -> bool:
    """末尾の数字の並びだけが違い（桁数は同じ）、それ以外が同じ別の名前か。"""
    a, b = near_key(name), near_key(other)
    return a is not None and a == b and name != other


def _is_declared_subject(n: dict) -> bool:
    """ファイルの主体のノードか（子・同じファイルの 2 件目以降の定義は `line` を持つ。資料のノードは `analyzer` が無い）。"""
    return bool(n.get("analyzer")) and "line" not in n and n.get("label") != "Document" and bool(n.get("path")) and bool(n.get("name"))


def build_rows(doc_rows: list, nodes: list, edges=()) -> list:
    """取り込みの台帳の行（`doc_rows`）とグラフのノード・線から、名前の台帳の行 `{name, kind, source, rel_path}` を作る。"""
    rows: dict = {}
    kind_of: dict = {}
    for n in nodes:
        if not _is_declared_subject(n) or text_kind.is_sensitive_doc_id(n["path"]):
            continue
        kind_of.setdefault(n["path"], n["label"])
        key = (normalize_name(n["name"]), n["label"], SOURCE_DECLARED, n["path"])
        rows[key] = None
    by_cid = {n["cid"]: n for n in nodes if n.get("cid")} if edges else {}
    for e in edges:
        if e.get("type") != "BUILT_FROM":
            continue
        exe, mat = by_cid.get(e["src"]), by_cid.get(e["dst"])
        if exe and mat and mat.get("path") and not text_kind.is_sensitive_doc_id(mat["path"]):
            rows[(normalize_name(exe["name"]), exe["label"], SOURCE_BUILD, mat["path"])] = None
    for r in doc_rows:
        rel = r.get("name") or ""
        stem = normalize_name(PurePosixPath(rel).stem)
        if r.get("branch") != "source" or not stem or text_kind.is_sensitive_doc_id(rel):
            continue
        rows[(stem, kind_of.get(rel, ""), SOURCE_FILENAME, rel)] = None
    return [{"name": k[0], "kind": k[1], "source": k[2], "rel_path": k[3]} for k in sorted(rows)]
