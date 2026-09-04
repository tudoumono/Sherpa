"""JCL アナライザ（docs/05-グラフ語彙.md §4 トラック S）。

`JOB` を主体定義（`Batch`）とし、`EXEC PGM=` を参照候補（`Module` への INVOKES）として返す。
`EXEC PROC=`/カタログドプロシージャ実行・`INCLUDE MEMBER=` は現行未対応の構文として `dropped` に
記録して落とす（黙って消さない）。
"""
from __future__ import annotations

from ..identifiers import normalize_code_name as _norm
from ..static_analysis import (JCL_EXT, _EXEC, _JCL_EXEC_ANY, _JCL_INCLUDE,
                               _JOB, _is_comment)
from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult


class JclAnalyzer(Analyzer):
    """`JOB` → `Batch`。`EXEC PGM=` → `Module` 参照（INVOKES）。"""

    name = "jcl"
    extensions = frozenset(JCL_EXT)
    doctype = "jcl"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        job = next((_norm(mm.group(1)) for line in text.splitlines()
                    if not _is_comment(line) for mm in [_JOB.match(line)] if mm), None)
        if not job:
            return DefResult()
        return DefResult(primary=DefItem(label="Batch", name=job))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        refs: list = []
        dropped: list = []
        for i, line in enumerate(text.splitlines(), 1):
            if _is_comment(line):
                continue
            e = _EXEC.match(line)
            if e:
                refs.append(RefCandidate("INVOKES", "Module", _norm(e.group(2)), i))
                continue
            if _JCL_EXEC_ANY.match(line):
                # PGM= でない EXEC（カタログドプロシージャ実行等）。プロシージャの中身は解決できない
                # ため、静的には解析せず記録するだけ（誤った呼び先を推測しない）。
                dropped.append(Dropped("proc_exec", i, line.strip()[:120]))
            elif _JCL_INCLUDE.match(line):
                dropped.append(Dropped("include_member", i, line.strip()[:120]))
        return RefResult(refs=refs, dropped=dropped)
