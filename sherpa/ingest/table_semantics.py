"""表の検索用表現（ヘッダ推定・dict 化）の部品（RAG-REP-001・起案 docs/archive/proposals/2026-07-20-調査型RAG詳細修正
計画.html §フェーズ3・DOC-IR-001.5 修正1 で `arms/ooxml_arm.py` から移設）。

document-ir-v1 の表要素は原本忠実な位置付きセル配列（`document_ir.Cell`）で持つ＝IR 自体はヘッダ判定を
しない。本モジュールは、IR の `cells` から**先頭行をヘッダとみなして** dict 化する側（検索用表現生成・
RAG-REP-001）が使う決定的な正規化ヘルパ:

- ヘッダキー正規化: 空文字ヘッダ→`column:N`（N=1-based 列位置）。重複ヘッダ→2個目以降を
  `名前#2`, `名前#3`…（出現順）。
- 行がヘッダより長い→超過セルは `column:N` キーで保持。短い→欠損列は `""` で埋める。

セルを一切黙って消さない（旧 `dict(zip(header, cells))` は重複ヘッダ・空ヘッダで暗黙の上書き、行長不一致で
暗黙の切り捨て/欠損が起きていた・B2・RV Med の教訓を IR 分離後もそのまま引き継ぐ）。
"""
from __future__ import annotations


def uniquify(base: str, used: set[str]) -> str:
    """`base` が `used` と衝突しない決定的なキーになるまで `#2`, `#3`… を付ける（採用したキーは `used` へ登録）。

    生成後のキー自体（例 `A#2`）も衝突チェックの対象＝実ヘッダ名が `A#2` や `column:N` でもセルを失わない
    （RV2巡目 Med: 生成キー形式の実ヘッダとの衝突対策）。
    """
    key, n = base, 1
    while key in used:
        n += 1
        key = f"{base}#{n}"
    used.add(key)
    return key


def normalize_table_header(header: list[str]) -> list[str]:
    """ヘッダ行のセル値を決定的な dict キーへ正規化する（B2・RV Med・モジュール docstring 参照）。

    - 空文字ヘッダ → `column:N`（N=1-based 列位置）。
    - 衝突（重複ヘッダ・生成キー形式の実ヘッダを含む）→ `uniquify` で `名前#2`, `名前#3`… を出現順に付ける。
    """
    used: set[str] = set()
    return [uniquify(name if name else f"column:{i}", used)
            for i, name in enumerate(header, start=1)]


def row_values(header_keys: list[str], cells: list[str]) -> dict[str, str]:
    """データ行のセルをヘッダキーへ対応付ける（B2・RV Med）。セルを一切黙って消さない:

    - 行がヘッダより短い → 欠損列は `""` で埋める（従来の `dict(zip(...))` は短い方に合わせ暗黙に切り捨てていた）。
    - 行がヘッダより長い → 超過セルは `column:N`（N=1-based 列位置）キーで保持する（従来は切り捨てていた）。
      実ヘッダが `column:N` そのものでも `uniquify` が `column:N#2` へ退避させるため衝突で消えない。
    """
    values: dict[str, str] = {key: (cells[i] if i < len(cells) else "") for i, key in enumerate(header_keys)}
    used = set(header_keys)
    for i in range(len(header_keys), len(cells)):
        values[uniquify(f"column:{i + 1}", used)] = cells[i]
    return values
