r"""Properties 設定ファイルアナライザ。ファイル自体を主体定義（`Config`）とし、各キーを `DefItem(label="Config", name=<裸キー>, cid_key="key:property:" + <裸キー>)` の children として返す。参照候補は返さない（コード側の設定キー参照は `java.py`）。

`cid_key` の `key:` 接頭辞で primary と cid の名前空間を分ける（キー名がファイル名と同じでも自己ループにならない）。
読み取り規則（Java `Properties` のサブセット）:
- 先頭の BOM は除去する。先頭が `#`/`!` の行はコメント、空行は無視。
- 行末の奇数個の `\` は継続指示（次の物理行と結合）。
- 区切り（`=`・`:`・空白の最初に現れた未エスケープのもの）でキーと値を分け、キーは `\X` → `X` にアンエスケープする。
- 同一ファイル内の重複キーは最初の行のみ採用する。`\uXXXX` は変換しない。
- 値は `extra["config_value"]`（先頭200文字）に持つ（`"value"` は共通層の予約キーと衝突する）。
- 各キー child は `extra["key_kind"] = "property"` を持つ。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, RefResult

PROPERTIES_EXT = frozenset({".properties"})

_VALUE_TRUNCATE = 200
_SEP_CHARS = (" ", "\t", "=", ":")


def _odd_trailing_backslashes(s: str) -> bool:
    """行末の連続する `\\` の個数が奇数か（奇数＝継続指示・偶数＝エスケープ済みで非継続）。"""
    return (len(s) - len(s.rstrip("\\"))) % 2 == 1


def _iter_logical_lines(text: str):
    """物理行を継続行結合し、コメント/空行を除いた論理行を `(line_no, content)` で返す（`line_no` は先頭物理行の1始まり）。コメント判定は先頭物理行のみ。"""
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        first_no = i + 1
        head = lines[i].lstrip(" \t\f")
        if not head or head[0] in ("#", "!"):
            i += 1
            continue
        parts = [lines[i]]
        i += 1
        while _odd_trailing_backslashes(parts[-1]) and i < n:
            parts[-1] = parts[-1][:-1]
            parts.append(lines[i].lstrip(" \t\f"))
            i += 1
        if _odd_trailing_backslashes(parts[-1]):
            parts[-1] = parts[-1][:-1]  # ファイル末尾で継続指示のみ残った場合は落とす
        yield first_no, "".join(parts)


def _find_unescaped_sep(s: str) -> int | None:
    """`\\` の次の1文字をリテラル扱いしつつ、`_SEP_CHARS` のいずれかが最初に現れる位置を返す（無ければ `None`）。"""
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch in _SEP_CHARS:
            return i
        i += 1
    return None


def _unescape_key(raw_key: str) -> str:
    """キー中の `\\X` をアンエスケープする（バックスラッシュを取り除く）。"""
    out: list = []
    i, n = 0, len(raw_key)
    while i < n:
        ch = raw_key[i]
        if ch == "\\" and i + 1 < n:
            out.append(raw_key[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_kv(logical: str):
    """論理行を `(key, value)` へ分離する。区切りが無い行は値なし `(key, "")`、キーも空なら `None`。返す `key` はアンエスケープ済み。"""
    stripped = logical.lstrip(" \t\f")
    if not stripped:
        return None
    sep_pos = _find_unescaped_sep(stripped)
    if sep_pos is None:
        return _unescape_key(stripped), ""
    raw_key = stripped[:sep_pos]
    if not raw_key:
        return None
    key = _unescape_key(raw_key)
    rest = stripped[sep_pos:].lstrip(" \t\f")
    if rest[:1] in ("=", ":"):
        rest = rest[1:].lstrip(" \t\f")
    return key, rest


class PropertiesAnalyzer(Analyzer):
    """ファイル自体 → `Config`（primary）。各キー → `Config`（children・`primary -CONTAINS-> child`）。"""

    name = "properties"
    extensions = PROPERTIES_EXT
    doctype = "properties"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        if text.startswith("\ufeff"):  # 先頭 BOM は除去してから読む
            text = text[1:]
        primary = DefItem(label="Config", name=PurePosixPath(rel_path).name)
        children: list = []
        seen: set = set()
        for line_no, logical in _iter_logical_lines(text):
            kv = _split_kv(logical)
            if kv is None:
                continue
            key, value = kv
            if not key or key in seen:
                continue
            seen.add(key)
            children.append(DefItem(label="Config", name=key, line=line_no,
                                    cid_key=f"key:property:{key}",
                                    extra={"config_value": value[:_VALUE_TRUNCATE],
                                           "key_kind": "property"}))
        return DefResult(primary=primary, children=children)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        return RefResult()
