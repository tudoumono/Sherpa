"""Properties 設定ファイルアナライザ（アナライザ拡張 S3'・A7 案B＝キー単位 `Config` children）。

`.properties` はファイル自体を主体定義（`Config`・S3）にしたうえで、`key=value`／`key: value`／
`key value` の3書式（区切りは `=`・`:`・空白のいずれか最初に現れたもの）を読み、各キーを
`DefItem(label="Config", name=<裸キー>, cid_key="key:property:" + <裸キー>)` として children に返す
（primary はファイル単位 `Config` のまま・§4(b) RV2-2＝ラベルは `DataItem` ではなく親と同じ `Config`）。
`name` は裸キーのまま（構造参照・言及辞書の索引は変えない）だが、`cid_key` に `"key:"` 接頭辞を
付けて primary と cid の名前空間を分離する——`app.properties` というキー自身が持ち得るファイル
（例: 同名ファイル `app.properties` 内の `app.properties=...`）でも、primary の cid
（`config:{world}:{rel}#app.properties`）とキー child の cid
（`config:{world}:{rel}#key:property:app.properties`）が衝突して自己ループにならないようにするため。
ファイルの区別自体は cid（`rel_path` 込み）が担う。続く `key_kind`（本アナライザは常に `"property"`）
は xml_config.py 等の他の Config キー producer と cid 形式を揃える（波3 統合 RV・key_kind ごとの
名前空間分離を cid にも反映する）。

読み取りの規則（Java `Properties` 仕様のサブセット）:
- 先頭の BOM（U+FEFF）は除去してから読む。
- 先頭の空白を除いた最初の文字が `#`/`!` の行はコメント（無視）。空行も無視。
- 行末の奇数個の `\\` は継続指示——次の物理行（先頭の空白は読み飛ばす）と結合してから
  key/value を分離する（偶数個＝末尾の `\\` はエスケープ済みでそのまま・継続しない）。
- key/value の区切り（`=`・`:`・空白）は**未エスケープ**のものだけを対象に走査する
  （`\=`/`\:`/`\ ` 等でエスケープされた文字は区切りとして扱わない）。区切りとして認めた
  位置より前のキー部分は `\X` → `X` へアンエスケープしてから索引に使う
  （例: `key\=with\=escape=value` → キー `key=with=escape`・値は `value`）。
- 同一ファイル内の重複キーは最初の行のみを採用する（後続は無視）。
- `\\uXXXX` は無変換で保持する（native2ascii は模倣しない）。
- 値は `extra={"config_value": <先頭200文字>}` に保持する（将来用・ノード属性としては保存され
  なくてよい）。`extra` キー名は共通層が確定するノードプロパティ `value`（`DefItem.value`
  経由・primary/child 共通）と衝突しないよう `config_value` にする——`"value"` のままだと
  共通層の `_sanitized_extra` が予約キー衝突と判定して `extra` を丸ごと捨てる
  （`reserved_key_in_extra` flag）。
- 各キー child は `extra["key_kind"] = "property"` も持つ（Config キーは種別で名前空間を分ける・
  共通層 A9 の `config_key_index` はこの値も索引キーに含める）。properties/YAML のキーは常に
  `"property"`（Spring bean id/alias や Struts action 等の他種別は別アナライザが付ける）。

参照候補は返さない（コード側の設定キー参照抽出は `java.py` 側・§4(b) 案B）。
"""
from __future__ import annotations

from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, RefResult

PROPERTIES_EXT = frozenset({".properties"})

_VALUE_TRUNCATE = 200
_SEP_CHARS = (" ", "\t", "=", ":")


def _odd_trailing_backslashes(s: str) -> bool:
    """行末の連続する `\\` の個数が奇数か(奇数=継続指示・偶数=末尾がエスケープ済みで非継続)。"""
    return (len(s) - len(s.rstrip("\\"))) % 2 == 1


def _iter_logical_lines(text: str):
    """物理行を継続行結合し、コメント/空行を除いた論理行を `(line_no, content)` で返す
    （`line_no` は先頭物理行の1始まり行番号）。コメント判定は論理行の先頭物理行のみに適用する。
    """
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
            parts[-1] = parts[-1][:-1]         # ファイル末尾で継続指示のみ残った場合は素直に落とす
        yield first_no, "".join(parts)


def _find_unescaped_sep(s: str) -> int | None:
    """先頭からエスケープ（`\\` の次の1文字は常にリテラル扱い）を考慮しつつ、`_SEP_CHARS` の
    いずれかが最初に現れる位置を返す（無ければ `None`）。"""
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
    """キー中の `\\X`（バックスラッシュ＋任意の1文字）をアンエスケープする
    （区切り文字として書くために必要だったエスケープを含め、バックスラッシュを取り除く）。"""
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
    """論理行を `(key, value)` へ分離する（区切りは未エスケープの `=`・`:`・空白のいずれか
    最初に現れたもの）。

    分離できない（空行・区切りなしでキーも空）場合は `None`。区切りが無い行は値なし（`key` のみ）
    として `(key, "")` を返す——Java `Properties` 仕様どおり。返す `key` は常にアンエスケープ済み。
    """
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
    """ファイル自体 → `Config`（primary）。`key=value`／`key: value`／`key value` の各キー →
    `Config`（children・`primary -CONTAINS-> child`・索引キーは裸のキー・cid は `"key:"` 接頭辞で
    primary と分離）。"""

    name = "properties"
    extensions = PROPERTIES_EXT
    doctype = "properties"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        if text.startswith("\ufeff"):                     # 先頭 BOM は除去してから読む
            text = text[1:]
        primary = DefItem(label="Config", name=PurePosixPath(rel_path).name)
        children: list = []
        seen: set = set()
        for line_no, logical in _iter_logical_lines(text):
            kv = _split_kv(logical)
            if kv is None:
                continue
            key, value = kv
            if not key or key in seen:                  # 重複キーは最初の行のみ採用
                continue
            seen.add(key)
            children.append(DefItem(label="Config", name=key, line=line_no,
                                    cid_key=f"key:property:{key}",
                                    extra={"config_value": value[:_VALUE_TRUNCATE],
                                           "key_kind": "property"}))
        return DefResult(primary=primary, children=children)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        return RefResult()
