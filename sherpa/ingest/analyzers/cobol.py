"""標準 COBOL アナライザ（docs/05-グラフ語彙.md §4 トラック S）。

`PROGRAM-ID` を主体定義（`Module`）とし、`COPY`/`CALL` を参照候補として返す。標準1本
（ベンダー差の概念は持たない・§7 裁定1）。標準で解釈できない構文（動的 CALL＝識別子呼び出し等）は
解析せず `dropped` に記録して落とす（黙って消さない）。
"""
from __future__ import annotations

from ..identifiers import normalize_code_name as _norm
from ..static_analysis import (COBOL_EXT, _CALL, _COPY, _DYNAMIC_CALL,
                               _PROGRAM_ID, _is_comment, _is_free_format,
                               _split_statements, _strip_inline_comment,
                               _strip_quoted)
from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

# 固定形式 COBOL の実コード領域は8〜72桁（73桁以降は採番/識別領域）。自由形式（`_is_free_format`
# 参照＝ファイル単位の近似・最初の指示文で判定・途中切替や comment 中の指示は CODE-2 対象）は
# この制限を持たない。行の論理正規化（継続行結合・7桁 indicator 等）は現行実装にも無く CODE-2
# 「静的解析の深化」の対象——ここでは動的 CALL 検知が固定形式の採番領域の文字列を誤って拾わない
# ための最小対応（この定数を使うのは動的 CALL 検知だけ・literal CALL/COPY 抽出は対象外・かつ
# 自由形式ファイルには適用しない）。
_FIXED_FORMAT_CODE_END = 72


class CobolAnalyzer(Analyzer):
    """`PROGRAM-ID` → `Module`。`COPY` → `Copybook` 参照（COPIES）。`CALL` → `Module` 参照（INVOKES）。"""

    name = "cobol"
    extensions = frozenset(COBOL_EXT)
    doctype = "cobol"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        pid = next((_norm(m) for line in text.splitlines() if not _is_comment(line)
                    for m in _PROGRAM_ID.findall(line)), None)
        if not pid:
            return DefResult()
        return DefResult(primary=DefItem(label="Module", name=pid))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        refs: list = []
        dropped: list = []
        after_id = False
        free_format = _is_free_format(text)
        for i, line in enumerate(text.splitlines(), 1):
            if _is_comment(line):
                continue
            if not after_id:
                if _PROGRAM_ID.search(line):
                    after_id = True
                continue
            # COPY/CALL のリテラル抽出は、行末インラインコメント（`*>` 以降・rv-s2-mention #5）を
            # 先に切り捨ててから行う——`MOVE X TO Y. *> COPY FAKE` のような行末コメント中の語を
            # 構文と誤認しない（`_is_comment` は行全体がコメントの場合しか見ない）。
            code = _strip_inline_comment(line)
            # COPY 抽出だけはさらに引用文字列の中身も除去する（`DISPLAY 'COPY FAKECPY'.` のような
            # 文字列リテラル中の語を誤検知しない）。CALL は引用符の中身（呼び出し先プログラム名）
            # 自体を読むため、こちらには適用しない。
            for cb in _COPY.findall(_strip_quoted(code)):
                refs.append(RefCandidate("COPIES", "Copybook", _norm(cb), i))
            # CALL は文単位（ピリオド／END-CALL 区切り）で判定する——同一行に複数の CALL 文が
            # 並ぶ場合（`CALL 'A'. CALL B.`・`CALL 'A' END-CALL CALL B END-CALL.`）でも
            # 取りこぼさない。引用文字列の中身は動的呼び出し判定の対象外にする（`DISPLAY 'CALL X'.`
            # のような文字列リテラルを誤検知しない）。
            for stmt in _split_statements(code):
                for callee in _CALL.findall(stmt):
                    refs.append(RefCandidate("INVOKES", "Module", _norm(callee), i))
            # 動的 CALL の検知だけは固定形式の採番/識別領域（73桁以降）を見ない（自由形式は切り詰めない・
            # _FIXED_FORMAT_CODE_END 参照）。
            code_line = line if free_format else line[:_FIXED_FORMAT_CODE_END]
            for stmt in _split_statements(code_line):
                # `_strip_quoted` が文字列リテラルの中身を空にするため、残る `CALL` はすべて
                # リテラルでない（動的）呼び出し——同一断片に literal CALL が同居していても
                # （例: `CALL 'A' ON EXCEPTION CALL B END-CALL`）出現ごとに個別判定する。
                for _callee in _DYNAMIC_CALL.findall(_strip_quoted(stmt)):
                    # 静的には解決できないため解析せず記録するだけ（誤った固定先を推測しない）。
                    dropped.append(Dropped("dynamic_call", i, stmt.strip()[:120]))
        return RefResult(refs=refs, dropped=dropped)
