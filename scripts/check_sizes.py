#!/usr/bin/env python3
"""ファイル・関数の大きさの上限と、部品どうしの依存の向きを検査する（`make gate-*` の先頭で走る）。

運用: docs/20-開発ハーネス.md §5。

- 大きさ: 物理行で数える。ファイル 1,000 行・Python 関数 100 行が上限。今超えているものは
  基準ファイル（`scripts/size_baseline.txt`）に載せる。基準と今の値が違えば失敗にする（増えた・新しく超えた・
  減ったのに基準を下げていない・消えたのに基準に残っている）。`--write-baseline` は基準を下げる・外すだけで、
  上げない。新しく超えたものを載せるのは `--accept-new` を付け、基準から消えた項目を移しただけ（その値以下）と
  対にできるときだけ。他の線を取り込んで増えた分は `--absorb <その線>` で、その線にある値までだけ上げる。
  基準ファイルが無ければ失敗（初めて作るときは `--init`）。
- 依存: `scripts/arch_rules.toml` の部品（パッケージ）と許す依存先に反する import、および
  部品の外から `_` 始まりの名前・`__init__.py` に出していない下位モジュールを import するものを失敗にする。
"""
from __future__ import annotations

import argparse
import ast
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

FILE_LIMIT = 1000
FUNC_LIMIT = 100
SIZE_ROOTS = ("sherpa", "web", "scripts", "tests")
SIZE_SUFFIXES = (".py", ".js", ".mjs", ".css", ".html", ".sh")
SKIP_DIRS = {"node_modules", "vendor", "goldens", "__pycache__", ".git", ".venv"}
DEFAULT_BASELINE = "scripts/size_baseline.txt"
DEFAULT_RULES = "scripts/arch_rules.toml"
DEFAULT_SCAN = ("sherpa", "scripts")


# --- 大きさ -------------------------------------------------------------------------------

def _count_lines(data: bytes) -> int:
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _iter_files(root: Path, roots=SIZE_ROOTS, suffixes=SIZE_SUFFIXES):
    for top in roots:
        base = root / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                if not name.endswith(suffixes) or ".min." in name:
                    continue
                p = Path(dirpath) / name
                yield p.relative_to(root).as_posix(), p


def _function_sizes(rel: str, tree: ast.AST) -> list[tuple[str, int]]:
    """入れ子・メソッドを含む全関数の (path::修飾名, 行数)。同名は出現順に `#2` `#3` を付ける。"""
    out: list[tuple[str, int]] = []
    seen: dict[str, int] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    key = f"{rel}::{qual}"
                    seen[key] = seen.get(key, 0) + 1
                    if seen[key] > 1:
                        key = f"{key}#{seen[key]}"
                    out.append((key, child.end_lineno - child.lineno + 1))
                visit(child, qual + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def measure_over_limit(root: Path) -> dict[str, int]:
    """上限を超えているファイルと関数だけを {キー: 行数} で返す。"""
    over: dict[str, int] = {}
    for rel, p in _iter_files(root):
        data = p.read_bytes()
        n = _count_lines(data)
        if n > FILE_LIMIT:
            over[rel] = n
        if rel.endswith(".py"):
            try:
                tree = ast.parse(data)
            except (SyntaxError, ValueError):
                continue
            for key, lines in _function_sizes(rel, tree):
                if lines > FUNC_LIMIT:
                    over[key] = lines
    return over


def read_baseline(path: Path) -> dict[str, int]:
    base: dict[str, int] = {}
    if not path.is_file():
        return base
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        key, _, n = line.rpartition("\t")
        base[key] = int(n)
    return base


def write_baseline(path: Path, over: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}\t{over[k]}\n" for k in sorted(over)), encoding="utf-8")


def measure_ref(root: Path, ref: str) -> dict[str, int]:
    """git の `ref` の木で `measure_over_limit` を測る（作業ツリーは触らない）。"""
    top = subprocess.run(["git", "-C", str(root), "ls-tree", "--name-only", ref],
                         check=True, capture_output=True, text=True).stdout.split()
    roots = [r for r in SIZE_ROOTS if r in top]
    if not roots:
        return {}
    data = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", ref, *roots],
                          check=True, capture_output=True).stdout
    with tempfile.TemporaryDirectory(prefix="check-sizes-") as d, tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(d, filter="data")
        return measure_over_limit(Path(d))


def absorbed_baseline(over: dict[str, int], base: dict[str, int], theirs: dict[str, int]) -> dict[str, int]:
    """取り込んだ線（`theirs`）にある値までだけ基準を上げる。今の値・基準・取り込んだ線の値の範囲を超えない。"""
    new = dict(base)
    for key, n in over.items():
        cap = max(base.get(key, 0), theirs.get(key, 0))
        if n <= cap:
            new[key] = max(base.get(key, 0), n) if key in base else n
    return new


def lowered_baseline(over: dict[str, int], base: dict[str, int], accept_moves: bool) -> tuple[dict[str, int], list[str]]:
    """書き直す基準と、載せなかった新しい超過を返す。既にある値は上げない。

    `accept_moves` のとき、新しい超過は基準から消えた同じ種類（ファイル／関数）の項目 1 つと対にできて、
    その基準の値以下のときだけ載せる（移しただけ・名前を変えただけ）。対にした消えた項目は 1 回しか使わない。
    """
    new: dict[str, int] = {}
    refused: list[str] = []
    gone = {k: v for k, v in base.items() if k not in over}
    used: set[str] = set()
    for key, n in sorted(over.items(), key=lambda kv: (-kv[1], kv[0])):
        if key in base:
            new[key] = min(n, base[key])
            continue
        match = None
        if accept_moves:
            kind = "::" in key
            cands = [g for g in gone if g not in used and ("::" in g) == kind and gone[g] >= n]
            match = min(cands, key=lambda g: (gone[g], g)) if cands else None
        if match is None:
            refused.append(f"{key}: {n} 行（上限 {_limit_of(key)}・基準なし）")
            continue
        used.add(match)
        new[key] = n
    return new, sorted(refused)


def _limit_of(key: str) -> int:
    return FUNC_LIMIT if "::" in key else FILE_LIMIT


def judge_sizes(over: dict[str, int], base: dict[str, int]) -> list[str]:
    """失敗の行を返す。基準と今の値が一致しないものは全部失敗（減ったものは `--write-baseline` で基準を下げる）。"""
    failures: list[str] = []
    for key in sorted(over):
        n, limit = over[key], _limit_of(key)
        if key not in base:
            failures.append(f"{key}: {n} 行（上限 {limit}・基準なし＝新しく超えた）")
        elif n > base[key]:
            failures.append(f"{key}: {n} 行（上限 {limit}・基準 {base[key]} より増えた）")
        elif n < base[key]:
            failures.append(f"{key}: {n} 行に減った（基準 {base[key]}）。--write-baseline で基準を下げる")
    for key in sorted(set(base) - set(over)):
        failures.append(f"{key}: 上限内に戻ったか消えた（基準 {base[key]}）。--write-baseline で基準から外す")
    return failures


# --- 依存の向き ----------------------------------------------------------------------------

def _module_name(rel: str) -> str:
    parts = rel[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _exports(root: Path, comp: str) -> set[str]:
    """部品の `__init__.py` が束縛している名前（import した名前・定義・代入・`__all__`）。"""
    init = root / comp.replace(".", "/") / "__init__.py"
    names: set[str] = set()
    if not init.is_file():
        return names
    try:
        tree = ast.parse(init.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError):
        return names
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                    if t.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                        names.update(
                            e.value for e in node.value.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)
                        )
    return names


def _is_module(root: Path, dotted: str) -> bool:
    p = root / dotted.replace(".", "/")
    return p.with_suffix(".py").is_file() or (p / "__init__.py").is_file()


def _component_of(dotted: str, comps: dict[str, list[str]]) -> str | None:
    best = None
    for c in comps:
        if (dotted == c or dotted.startswith(c + ".")) and (best is None or len(c) > len(best)):
            best = c
    return best


def _inside(mod: str, comp: str) -> bool:
    return mod == comp or mod.startswith(comp + ".")


def _imports(rel: str, tree: ast.AST, root: Path):
    """(行, 対象モジュール, 取り込む名前 or None) を順に返す。相対 import は絶対名へ直す。"""
    mod = _module_name(rel)
    pkg = mod if rel.endswith("__init__.py") else mod.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield node.lineno, a.name, None
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".") if pkg else []
                parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                base = ".".join(parts + ([node.module] if node.module else []))
            for a in node.names:
                sub = f"{base}.{a.name}" if base else a.name
                if a.name != "*" and _is_module(root, sub):
                    yield node.lineno, sub, None
                else:
                    yield node.lineno, base, a.name


def check_arch(root: Path, rules_path: Path) -> list[str]:
    if not rules_path.is_file():
        return []
    rules = tomllib.loads(rules_path.read_text(encoding="utf-8"))
    comps = {name: list(v.get("allow", [])) for name, v in rules.get("components", {}).items()}
    if not comps:
        return []
    scan = tuple(rules.get("scan", DEFAULT_SCAN))
    shims = set(rules.get("shims", []))  # 旧の入口（部品の名前を出し直すだけ）は非公開の名前の検査から外す
    exports = {c: _exports(root, c) for c in comps}
    problems: list[str] = []
    for rel, p in _iter_files(root, scan, (".py",)):
        try:
            tree = ast.parse(p.read_bytes())
        except (SyntaxError, ValueError):
            continue
        me = _module_name(rel)
        mine = _component_of(me, comps)
        for lineno, target, sym in _imports(rel, tree, root):
            dest = _component_of(target, comps)
            if dest is None or _inside(me, dest):
                continue
            where = f"{rel}:{lineno}"
            if me in shims:
                continue
            if (mine is not None and not _inside(dest, mine)
                    and dest not in comps[mine]):
                problems.append(f"{where}: 部品 {mine} は {dest} を import してよい一覧に無い（{target}）")
            rest = target[len(dest) + 1:].split(".") if target != dest else []
            if rest and (any(s.startswith("_") for s in rest) or rest[0] not in exports[dest]):
                problems.append(
                    f"{where}: 部品 {dest} の外から出していない下位モジュール {target} を import している"
                )
            elif sym and sym.startswith("_"):
                problems.append(f"{where}: 部品 {dest} の外から非公開の名前 {sym} を import している")
    return sorted(problems)


# --- 入口 ----------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ファイル・関数の大きさの上限と部品の依存の向きを検査する")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent), help="リポジトリのルート")
    ap.add_argument("--baseline", default=None, help=f"基準ファイル（既定 {DEFAULT_BASELINE}）")
    ap.add_argument("--rules", default=None, help=f"依存の規則（既定 {DEFAULT_RULES}）")
    ap.add_argument("--write-baseline", action="store_true", help="基準を今の値まで下げる・上限内に戻ったものを外す（上げない）")
    ap.add_argument("--accept-new", action="store_true",
                    help="--write-baseline で、基準から消えた項目を移しただけ（その値以下）の新しい超過を載せる")
    ap.add_argument("--init", action="store_true", help="基準ファイルが無いときだけ、今の超過で作る")
    ap.add_argument("--absorb", metavar="REF",
                    help="--write-baseline で、取り込んだ線 REF にある値までだけ基準を上げる（他の線の取り込みの後に使う）")
    args = ap.parse_args(argv)

    root = Path(args.root)
    baseline = Path(args.baseline) if args.baseline else root / DEFAULT_BASELINE
    rules = Path(args.rules) if args.rules else root / DEFAULT_RULES

    over = measure_over_limit(root)
    if args.init:
        if baseline.exists():
            print(f"NG 基準ファイルがすでにある: {baseline}")
            return 1
        write_baseline(baseline, over)
        print(f"基準を作りました: {baseline}（{len(over)} 件）")
        return 0
    if not baseline.exists():
        print(f"NG 基準ファイルが無い: {baseline}（初めてなら --init）")
        return 1
    if args.write_baseline:
        base = read_baseline(baseline)
        if args.absorb:
            base = absorbed_baseline(over, base, measure_ref(root, args.absorb))
        new, refused = lowered_baseline(over, base, args.accept_new)
        for line in refused:
            print(f"NG 基準に載せない（移しただけと対にできない）: {line}")
        if refused:
            return 1
        write_baseline(baseline, new)
        files = sum(1 for k in new if "::" not in k)
        print(f"基準を書きました: {baseline}（ファイル {files} 件・関数 {len(new) - files} 件）")
        return 0

    failures = judge_sizes(over, read_baseline(baseline))
    failures += check_arch(root, rules)
    for line in failures:
        print(f"NG {line}")
    if failures:
        print(f"check_sizes: 失敗 {len(failures)} 件")
        return 1
    print(f"check_sizes: OK（基準内の超過 {len(over)} 件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
