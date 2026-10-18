"""JCL アナライザ。`JOB` 文または `PROC` 文を主体定義（`Batch`）とする。

`JOB` を持つファイルは `Batch(JOB名)`、持たず `PROC` を持つファイルは `Batch(PROC名)`、どちらも持たない断片は `Batch(ファイル名ステム)`。`extra["jcl_kind"]`（`job`/`proc`/`include`）で区別する。JOB 内のインストリーム `PROC` は主体を増やさない。
参照: `EXEC PGM=` → `Module`（INVOKES）、`EXEC PROC=x`／カタログド実行 → `Batch`（`via=exec_proc`）、`INCLUDE MEMBER=x` → `Batch`（`via=include_member`）。PROC の実体展開はしない。シンボリックパラメータ（`&NAME`）は静的に解決できないため `dropped` に記録する（`pgm_symbolic`/`proc_symbolic`）。
データセット: 継続行を結合した論理文から、`EXEC PGM=` の直後の `DD DSN=`／`DSNAME=` を `RefResult.dd_bindings`（`DdBinding`）に積む。連結 DD は直前の DD 名の追加源、
`GDG(+1)` などは基底名と相対世代に分け、`&&NAME` は一時データセット、`DSN=*.STEP.DD` は同じファイル内の DD から引く。`SYSOUT=`・`DUMMY`・`DD *` は出さない。
引けないものは `dropped`（`dsn_symbolic`／`dd_override`／`dsn_backref_unresolved`／`utility_step`）に記録する。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from ..identifiers import normalize_code_name as _norm
from ..static_analysis import (JCL_EXT, _EXEC, _JCL_EXEC_PGM_SYMBOLIC,
                               _JCL_EXEC_PROC, _JCL_INCLUDE,
                               _JCL_INCLUDE_MEMBER, _JCL_PROC, _JOB, _is_comment)
from ._base import Analyzer, DdBinding, DefItem, DefResult, Dropped, RefCandidate, RefResult

# JCL の 1 行の有効桁（73 桁目以降は識別・連番）
_JCL_COLS = 72
# 実行するのが利用者のプログラムではない代表的なユーティリティ（DD の対応表は持たない）
_JCL_UTILITIES = frozenset({
    "IEFBR14", "IEBGENER", "ICEGENER", "IEBCOPY", "IEBCOMPR", "IEBPTPCH", "IEBUPDTE", "IEBDG", "IDCAMS",
    "SORT", "ICEMAN", "SYNCSORT", "DFSORT", "ADRDSSU", "IKJEFT01", "IKJEFT1B", "IRXJCL",
})
_GDG_REL = re.compile(r"^(?P<base>.+)\((?P<rel>[+-]?\d+)\)$")


def _split_operand_field(s: str) -> tuple:
    """文の操作の後ろ `s` を、引用符の外の最初の空白で `(オペランド欄, 注釈)` に分ける。"""
    in_quote = False
    for i, ch in enumerate(s):
        if ch == "'":
            in_quote = not in_quote
        elif ch == " " and not in_quote:
            return s[:i], s[i:].strip()
    return s, ""


def _jcl_statements(text: str) -> list:
    """継続行を結合した JCL の論理文 `(最初の行, 名前欄, 操作, オペランド)` の列。

    `//` で始まる行だけが文で、`//*` は捨てる。72 桁目までを読み、オペランド欄の末尾が `,` なら次の `//` 行を継続として結合する（間の `//*` は読み飛ばす）。
    名前欄が空なら名前は空文字（連結 DD・名前を省いた EXEC）。
    """
    lines = text.splitlines()
    out: list = []
    i, n = 0, len(lines)
    while i < n:
        raw = lines[i][:_JCL_COLS].rstrip()
        first = i + 1
        i += 1
        if not raw.startswith("//") or raw.startswith("//*"):
            continue
        body = raw[2:]
        if not body.strip():
            out.append((first, "", "", ""))      # 空の `//`（ジョブの終わり）
            continue
        if body[0] == " ":
            name, rest = "", body.strip()
        else:
            name, _, rest = body.partition(" ")
            rest = rest.strip()
        op, _, rest = rest.partition(" ")
        operands, _comment = _split_operand_field(rest.strip())
        while operands.endswith(",") and i < n:
            nxt = lines[i][:_JCL_COLS].rstrip()
            if nxt.startswith("//*"):
                i += 1
                continue
            if not nxt.startswith("//") or nxt[2:3] != " ":
                break
            cont, _comment = _split_operand_field(nxt[2:].strip())
            operands += cont
            i += 1
        out.append((first, name, op.upper(), operands))
    return out


def _split_params(operands: str) -> list:
    """オペランド欄を、引用符・括弧の外のカンマで分ける。"""
    out, depth, in_quote, start = [], 0, False, 0
    for i, ch in enumerate(operands):
        if ch == "'":
            in_quote = not in_quote
        elif in_quote:
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(operands[start:i])
            start = i + 1
    out.append(operands[start:])
    return [p for p in out if p]


def _dd_bindings(statements: list) -> tuple:
    """論理文から `(DdBinding の列, Dropped の列)` を作る。`EXEC PGM=` のステップに属する DD だけを対象にする。"""
    bindings: list = []
    dropped: list = []
    step_dds: dict = {}        # (ステップ名, DD 名) -> (dsn, gdg_rel) 逆方向参照用
    cur: tuple | None = None   # (exec 行, ステップ名)・対象外のステップは None
    last_dd = ""
    for line, name, op, operands in statements:
        if op in ("JOB", "PROC", "PEND", ""):
            cur, last_dd = None, ""
            continue
        if op == "EXEC":
            last_dd = ""
            pm = re.match(r"PGM=([^,\s]+)", operands, re.IGNORECASE)
            cur = None
            if pm and not pm.group(1).startswith("&"):
                if pm.group(1).upper() in _JCL_UTILITIES:
                    dropped.append(Dropped("utility_step", line, f"//{name} EXEC {operands}"[:120]))
                else:
                    cur = (line, name.upper())
            continue
        if op != "DD":
            continue
        if "." in name:                       # `//STEP.DD`・`//PROCSTEP.DD`: PROC の DD の上書き
            dropped.append(Dropped("dd_override", line, f"//{name} DD {operands}"[:120]))
            continue
        ddname = name.upper() or last_dd
        if name:
            last_dd = ddname
        if cur is None or not ddname:
            continue
        dsn, disp = None, ""
        for prm in _split_params(operands):
            key, eq, val = prm.partition("=")
            key = key.strip().upper()
            if key in ("DSN", "DSNAME") and eq:
                dsn = val.strip()
            elif key == "DISP" and eq:
                disp = val.strip().strip("()")
        if not dsn:
            continue
        if len(dsn) >= 2 and dsn.startswith("'") and dsn.endswith("'"):
            dsn = dsn[1:-1].replace("''", "'")
        dsn = dsn.upper()
        snippet = f"//{name} DD {operands}"[:120]
        gdg_rel = ""
        if dsn.startswith("*."):
            parts = dsn[2:].split(".")
            ref = step_dds.get((parts[0], parts[-1])) if len(parts) >= 2 else step_dds.get((cur[1], parts[0]))
            if ref is None:
                dropped.append(Dropped("dsn_backref_unresolved", line, snippet))
                continue
            dsn, gdg_rel = ref
        elif "&" in dsn and not dsn.startswith("&&"):
            dropped.append(Dropped("dsn_symbolic", line, snippet))
            continue
        else:
            gm = _GDG_REL.match(dsn)
            if gm and not dsn.startswith("&&"):
                dsn, gdg_rel = gm.group("base"), gm.group("rel")
        step_dds[(cur[1], ddname)] = (dsn, gdg_rel)
        bindings.append(DdBinding(exec_line=cur[0], ddname=ddname, dsn=dsn, disp=disp, gdg_rel=gdg_rel, line=line))
    return bindings, dropped


class JclAnalyzer(Analyzer):
    """`JOB`/`PROC` → `Batch`。`EXEC PGM=` → `Module`（INVOKES）。
    `EXEC PROC=`/カタログド実行・`INCLUDE MEMBER=` → `Batch`（INVOKES）。"""

    name = "jcl"
    extensions = frozenset(JCL_EXT)
    doctype = "jcl"
    version = 2

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
                target = pm.group(2).split(",", 1)[0]  # 末尾の `,PARM=...` 等は切り落とす
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
                # `INCLUDE` を検知したが `MEMBER=` が無い等、対象名を抽出できない未対応形。
                dropped.append(Dropped("include_unparsed", i, line.strip()[:120]))
        bindings, dd_dropped = _dd_bindings(_jcl_statements(text))
        return RefResult(refs=refs, dropped=dropped + dd_dropped, dd_bindings=bindings)
