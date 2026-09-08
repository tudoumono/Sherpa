"""JCL アナライザ（docs/05-グラフ語彙.md §4 トラック S）。

`JOB` 文または `PROC` 文を主体定義（`Batch`）とする。`JOB` を持つファイルは `Batch(JOB名)`、
`JOB` を持たず `PROC` 文（`//name PROC`／名前無しの `// PROC`）を持つファイルは `Batch(PROC名)`
（名前が無ければファイル名ステム）。どちらも持たないファイル（`INCLUDE` 対象の断片等）は
`Batch(ファイル名ステム)` とする——`extra["jcl_kind"]`（`job`/`proc`/`include`）で区別する。
JOB を持つファイル内のインストリーム `PROC`（`//name PROC` 〜 `// PEND`）はこの判定の対象にせず
主体を増やさない（JOB の Batch のまま・中の `EXEC PGM=` も従来どおり JOB 側の INVOKES として拾う）。

参照は `EXEC PGM=` → `Module`（INVOKES）、`EXEC PROC=x`/カタログド実行の `EXEC x` →
`Batch`（INVOKES・`via=exec_proc`）、`INCLUDE MEMBER=x` → `Batch`（INVOKES・`via=include_member`）。
`Batch(JOB) -INVOKES-> Batch(PROC) -INVOKES-> Module` の2段になり、PROC の実体展開（中身を JOB 側へ
インライン展開する処理）はしない——世代内最近傍で共通層が解決し、見つからなければ通常の
`unresolved`/`cross_scope` flag に倒れる（誤った先を推測しない）。EXEC の対象がシンボリック
パラメータ（`&NAME`）の場合は静的に解決先を特定できないため `dropped` に記録して落とす
（`PGM=&NAME` は `pgm_symbolic`、`PROC=&NAME`/裸の `&NAME` は `proc_symbolic`）。
"""
from __future__ import annotations

from pathlib import PurePosixPath

from ..identifiers import normalize_code_name as _norm
from ..static_analysis import (JCL_EXT, _EXEC, _JCL_EXEC_PGM_SYMBOLIC,
                               _JCL_EXEC_PROC, _JCL_INCLUDE,
                               _JCL_INCLUDE_MEMBER, _JCL_PROC, _JOB, _is_comment)
from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult


class JclAnalyzer(Analyzer):
    """`JOB`/`PROC` → `Batch`。`EXEC PGM=` → `Module`（INVOKES）。
    `EXEC PROC=`/カタログド実行・`INCLUDE MEMBER=` → `Batch`（INVOKES）。"""

    name = "jcl"
    extensions = frozenset(JCL_EXT)
    doctype = "jcl"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        lines = [line for line in text.splitlines() if not _is_comment(line)]
        job = next((_norm(mm.group(1)) for line in lines for mm in [_JOB.match(line)] if mm), None)
        if job:
            return DefResult(primary=DefItem(label="Batch", name=job, extra={"jcl_kind": "job"}))
        stem = _norm(PurePosixPath(rel_path).stem)
        proc = next((mm for line in lines for mm in [_JCL_PROC.match(line)] if mm), None)
        if proc is not None:
            name = _norm(proc.group(1)) or stem
            return DefResult(primary=DefItem(label="Batch", name=name, extra={"jcl_kind": "proc"}))
        return DefResult(primary=DefItem(label="Batch", name=stem, extra={"jcl_kind": "include"}))

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
            if _JCL_EXEC_PGM_SYMBOLIC.match(line):
                dropped.append(Dropped("pgm_symbolic", i, line.strip()[:120]))
                continue
            pm = _JCL_EXEC_PROC.match(line)
            if pm:
                target = pm.group(2).split(",", 1)[0]     # 末尾の `,PARM=...` 等は切り落とす
                if target.startswith("&"):
                    dropped.append(Dropped("proc_symbolic", i, line.strip()[:120]))
                else:
                    refs.append(RefCandidate("INVOKES", "Batch", _norm(target), i,
                                             extra={"via": "exec_proc"}))
                continue
            im = _JCL_INCLUDE_MEMBER.match(line)
            if im:
                refs.append(RefCandidate("INVOKES", "Batch", _norm(im.group(1)), i,
                                         extra={"via": "include_member"}))
            elif _JCL_INCLUDE.match(line):
                # `INCLUDE` は検知したが `MEMBER=` が無い等、対象名を抽出できない未対応形
                # （黙って消さない）。
                dropped.append(Dropped("include_unparsed", i, line.strip()[:120]))
        return RefResult(refs=refs, dropped=dropped)
