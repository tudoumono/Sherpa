#!/usr/bin/env python3
"""コメント／docstring だけを変えた差分かを機械的に検査する。

使い方: scripts/check_comment_only.py <base-ref> [path ...]
指定パス（省略時＝base-ref と作業ツリーの差分にある .py 全部）について、base-ref 版と作業ツリー版の
AST を docstring を除いて比較し、1 つでも違えば非 0 で終了する（＝コード変更が混ざっている）。
コメント規律（CLAUDE.md・現在の契約のみ）の整理作業を、挙動不変のまま検収するための門番。
"""
from __future__ import annotations

import ast
import subprocess
import sys


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _dump(src: str) -> str:
    return ast.dump(_strip_docstrings(ast.parse(src)), include_attributes=False)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    base = argv[1]
    paths = argv[2:]
    if not paths:
        out = subprocess.run(["git", "diff", "--name-only", base, "--", "*.py"], capture_output=True, text=True, check=True)
        paths = [p for p in out.stdout.split() if p.endswith(".py")]
    bad = []
    for p in paths:
        try:
            old = subprocess.run(["git", "show", f"{base}:{p}"], capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError:
            bad.append(f"{p}: base に存在しない（新規ファイル＝コメント整理の対象外）")
            continue
        new = open(p, encoding="utf-8").read()
        if _dump(old) != _dump(new):
            bad.append(f"{p}: AST が一致しない（コード変更が混ざっている）")
    for b in bad:
        print(b, file=sys.stderr)
    print(f"checked {len(paths)} files, code-identical: {len(paths) - len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
