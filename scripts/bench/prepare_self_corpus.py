#!/usr/bin/env python3
"""Sherpa 自身のソースと設計書を、台帳評価の 4 系統目（`self`）の資料フォルダと質問セットにする。

合成コーパスや公開データより複雑で、Sherpa の本来の用途（設計書とソースを突き合わせ、食い違いはソースを正として報告する）に
近い試験を作る。正解はプログラムで機械的に作る（AI の判断を正解に入れない）。例外は「食い違い」型だけで、
こちらは設計書を直した過去のコミットの差分に出る旧記述を種にし、旧記述と実装の双方を機械的に確かめたものだけを入れ、
利用者が確認するまで評価に使わない印（`needs_user_review`）を付ける。

使い方（Codex・API・Docker は使わない。git と標準ライブラリだけ）:

    python3 scripts/bench/prepare_self_corpus.py \\
        --repo . --commit 9a539bb1 --design-fix-commit 64ca61cc \\
        --out /Users/sanomsf/work/sherpa-eval/datasets/sherpa-self-20261016

出力（`--out` の下。リポジトリには入れない・既存の出力があれば止まる）:

    corpus/       登録する資料フォルダ（正解・manifest を含まない）
    manifest.json 評価の土台（scripts/bench/ledger_eval.py）が読む manifest（系統は self だけ）
    gold/*.json   問いごとの正解の詳細（項目と資料の対応・生成の根拠）
    provenance.json 元のコミット・規則・除外したもの・漏れ防止の検査・食い違い候補の検証結果
    source-hashes.json 資料ごとの SHA-256
"""
from __future__ import annotations

import argparse
import ast
import collections
import dataclasses
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

SYSTEM = "self"
SEED = "sherpa-self-20261016"
DEFAULT_COMMIT = "9a539bb1"
DEFAULT_DESIGN_FIX = "64ca61cc"
GENERATOR = "scripts/bench/prepare_self_corpus.py"

# 資料フォルダに入れるもの。許可リスト方式なので、ここに無いもの（reports・rv・proposals・notes・archive・CHANGELOG・tests・
# scripts・fixtures・.git・.claude・AGENTS.md・CLAUDE.md など）は自動的に入らない。
INCLUDE_TREES = ("sherpa/", "web/", "docs/design/", "docs/manual/")
INCLUDE_FILES = ("README.md", "INSTALL.md", "EXTENDING.md")
NUMBERED_DOC_RE = re.compile(r"^docs/[0-2]\d-[^/]+\.md$")
TEXT_EXTS = frozenset({".py", ".md", ".json", ".css", ".js", ".html", ".txt"})

# 漏れ防止: 許可リストとは別に、含まれていてはならないものを明示して二重に検査する（答えが漏れる・評価の対象外）。
LEAK_PREFIXES = ("docs/reports/", "docs/rv/", "docs/proposals/", "docs/notes/", "docs/archive/", "tests/", "scripts/",
                 "fixtures/", ".git/", ".claude/")
LEAK_FILES = ("CHANGELOG.md", "AGENTS.md", "CLAUDE.md")
LEAK_PART_NAMES = frozenset({".git", ".claude", "__pycache__"})
# 本文に現れてはならない語（正解の出どころ・評価の仕組み）。件数は provenance に残す。
LEAK_TOKENS = ("SRH-08", "ANA-22", "ledger_eval", "prepare_self_corpus", "sherpa-eval", "needs_user_review")

HTTP_METHODS = ("get", "post", "put", "delete", "patch")
DIRECT_ENV_READERS = frozenset({"os.environ.get", "environ.get", "os.getenv", "getenv"})
ENV_CONTAINERS = frozenset({"os.environ", "environ"})
ENV_PREFIX = "SHERPA_"

SCORING_NOTES = [
    "一覧型は件数が多いほど Recall が下がりやすい。項目単位の採点は gold_items を使う別の集計が必要で、ledger_eval.py の Recall/Precision は"
    "資料パス単位のまま。",
    "Python の解析器が無いので、グラフ（Neo4j）はほぼ使われない。多段（呼び出しをたどる）型は rg と読み取りで解くことになり、"
    "グラフの有無の比較にはならない。",
    "多段型の正解は静的に解決できる呼び出し（import の別名・相対 import・モジュール経由・self.メソッド・クラスの生成）だけを"
    "たどった下限。インスタンスのメソッド呼び出し（obj.m()）・動的な呼び出しはたどらないため、Precision が実際より低く出ることがある。",
    "食い違い型の正解は未確認（needs_user_review=true）。確認が済むまで評価に使わない（ledger_eval.py は既定で除外する）。",
    "hop は多段型だけの段数。一覧型・食い違い型は多段ではないので 0（hop1〜hop4+ の表に入らず all にだけ入る）。",
    "設計書とソースをまたぐ型は、docs/design 以外の文書（docs/0*〜2*・docs/manual）に同じ語が出ても正解に入れない。引用が増えると Precision が下がる。",
]


# ---------------------------------------------------------------------------
# 資料フォルダ（コーパス）
# ---------------------------------------------------------------------------

def extract_archive(repo: Path, commit: str, dest: Path) -> None:
    """`git archive` でコミットの木を dest へ取り出す（作業ツリーは見ない）。"""
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit], capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        tar.extractall(dest, filter="data")


def is_leak_path(rel: str) -> bool:
    return (rel in LEAK_FILES or any(rel.startswith(p) for p in LEAK_PREFIXES)
            or any(part in LEAK_PART_NAMES for part in rel.split("/")))


def is_included(rel: str) -> bool:
    if is_leak_path(rel) or Path(rel).suffix.lower() not in TEXT_EXTS:
        return False
    return rel in INCLUDE_FILES or NUMBERED_DOC_RE.match(rel) is not None or rel.startswith(INCLUDE_TREES)


def collect_corpus_files(src: Path) -> tuple[list[str], dict]:
    """取り出した木から、資料フォルダへ入れる相対パス（昇順）と、入れなかったものの内訳を返す。"""
    files, skipped = [], collections.Counter()
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src).as_posix()
        if is_included(rel):
            files.append(rel)
            continue
        if is_leak_path(rel):
            skipped["漏れ防止で除外: " + _top(rel)] += 1
        elif rel.startswith(INCLUDE_TREES) or rel in INCLUDE_FILES or NUMBERED_DOC_RE.match(rel):
            skipped["画像などバイナリで除外: " + _top(rel)] += 1
        else:
            skipped["対象外: " + _top(rel)] += 1
    return files, dict(sorted(skipped.items()))


def _top(rel: str) -> str:
    parts = rel.split("/")
    if len(parts) == 1:
        return rel
    return "/".join(parts[:2]) + "/" if parts[0] == "docs" and len(parts) > 2 else parts[0] + "/"


def assert_no_leak(files: list[str]) -> None:
    bad = [f for f in files if is_leak_path(f)]
    if bad:
        raise RuntimeError(f"漏れ防止の対象が資料に入っています: {bad[:5]}")


def copy_corpus(src: Path, files: list[str], dest: Path) -> None:
    for rel in files:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, target)


def corpus_hashes(root: Path, files: list[str]) -> dict:
    """ledger_eval.read_manifest と同じ方式の corpus SHA-256 と、ファイル数・総バイト・資料ごとの SHA-256。"""
    digest, total, per_file = hashlib.sha256(), 0, {}
    for rel in sorted(files):
        data = (root / rel).read_bytes()
        sha = hashlib.sha256(data).digest()
        digest.update(rel.encode() + b"\0" + sha)
        total += len(data)
        per_file[rel] = sha.hex()
    return {"file_count": len(files), "total_bytes": total, "corpus_sha256": digest.hexdigest(), "per_file": per_file}


def leak_scan(root: Path, files: list[str]) -> dict:
    hits = {t: 0 for t in LEAK_TOKENS}
    for rel in files:
        text = (root / rel).read_text(encoding="utf-8", errors="ignore")
        for t in LEAK_TOKENS:
            if t in text:
                hits[t] += 1
    return hits


# ---------------------------------------------------------------------------
# Python の静的解析（定義・import・参照の解決）
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Func:
    qual: str
    module: str
    name: str
    cls: str | None
    rel: str
    lineno: int
    returns_value: bool


def _module_name(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _walk_own(node: ast.AST):
    """入れ子の関数・クラスの中へは入らずにたどる。"""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from _walk_own(child)


def _returns_value(fn: ast.AST) -> bool:
    for n in _walk_own(fn):
        if isinstance(n, ast.Return) and n.value is not None and not (isinstance(n.value, ast.Constant) and n.value.value is None):
            return True
    return False


class Module:
    def __init__(self, name: str, rel: str, tree: ast.Module, is_pkg: bool):
        self.name, self.rel, self.tree, self.is_pkg = name, rel, tree, is_pkg
        self.funcs: dict[str, Func] = {}
        self.classes: dict[str, dict[str, Func]] = {}
        self.nested: set[str] = set()
        self.consts: dict[str, str] = {}
        self.imports: dict[str, tuple] = {}

    @property
    def package(self) -> str:
        return self.name if self.is_pkg else self.name.rpartition(".")[0]


class PyIndex:
    """`sherpa/` 配下の Python を ast で読み、定義・import・参照を解決する。"""

    def __init__(self, root: Path, package_dir: str = "sherpa"):
        self.root = root
        self.modules: dict[str, Module] = {}
        self.parse_errors: list[str] = []
        for p in sorted((root / package_dir).rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"), filename=rel)
            except (SyntaxError, UnicodeDecodeError) as exc:
                self.parse_errors.append(f"{rel}: {type(exc).__name__}")
                continue
            mod = Module(_module_name(rel), rel, tree, p.name == "__init__.py")
            self.modules[mod.name] = mod
        for mod in self.modules.values():
            self._collect(mod)

    def _collect(self, mod: Module) -> None:
        for stmt in mod.tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                mod.funcs[stmt.name] = Func(f"{mod.name}:{stmt.name}", mod.name, stmt.name, None, mod.rel, stmt.lineno,
                                            _returns_value(stmt))
                for n in ast.walk(stmt):
                    if n is not stmt and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        mod.nested.add(n.name)
            elif isinstance(stmt, ast.ClassDef):
                methods = {}
                for item in stmt.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods[item.name] = Func(f"{mod.name}:{stmt.name}.{item.name}", mod.name, item.name, stmt.name,
                                                  mod.rel, item.lineno, _returns_value(item))
                mod.classes[stmt.name] = methods
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                value = stmt.value
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    for t in targets:
                        if isinstance(t, ast.Name):
                            mod.consts[t.id] = value.value
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        mod.imports.setdefault(a.asname, ("module", a.name))
                    else:
                        top = a.name.split(".")[0]
                        mod.imports.setdefault(top, ("module", top))
            elif isinstance(node, ast.ImportFrom):
                base = self._from_base(mod, node)
                if base is None:
                    continue
                for a in node.names:
                    if a.name == "*":
                        continue
                    alias = a.asname or a.name
                    sub = f"{base}.{a.name}" if base else a.name
                    if sub in self.modules:
                        mod.imports.setdefault(alias, ("module", sub))
                    else:
                        mod.imports.setdefault(alias, ("symbol", base, a.name))

    def _from_base(self, mod: Module, node: ast.ImportFrom) -> str | None:
        if node.level == 0:
            return node.module or ""
        parts = mod.package.split(".") if mod.package else []
        up = node.level - 1
        if up > len(parts):
            return None
        parts = parts[:len(parts) - up] if up else parts
        if node.module:
            parts = parts + node.module.split(".")
        return ".".join(parts)

    def symbol(self, modname: str, name: str, depth: int = 0):
        """モジュール内の名前を、再エクスポート（from .x import y）も含めて関数・クラス・モジュールへ解決する。"""
        mod = self.modules.get(modname)
        if mod is None or depth > 6:
            return None
        if name in mod.funcs:
            return ("func", modname, name)
        if name in mod.classes:
            return ("class", modname, name)
        sub = f"{modname}.{name}"
        if sub in self.modules:
            return ("module", sub)
        imp = mod.imports.get(name)
        if imp and imp[0] == "symbol":
            return self.symbol(imp[1], imp[2], depth + 1)
        if imp and imp[0] == "module":
            return ("module", imp[1])
        return None

    def resolve(self, mod: Module, expr: ast.AST, cls: str | None = None):
        """Name / Attribute の連なりを ("func"|"class"|"method"|"module"|"const", ...) へ解決する。解決できなければ None。"""
        chain = []
        node = expr
        while isinstance(node, ast.Attribute):
            chain.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        chain.reverse()
        base = node.id
        state = None
        if base in ("self", "cls") and cls:
            state = ("instance", mod.name, cls)
        elif base in mod.funcs:
            state = ("func", mod.name, base)
        elif base in mod.classes:
            state = ("class", mod.name, base)
        elif base in mod.nested:
            state = ("func", mod.name, base)
        elif base in mod.imports:
            imp = mod.imports[base]
            state = ("module", imp[1]) if imp[0] == "module" else self.symbol(imp[1], imp[2])
            if state is None and imp[0] == "symbol":
                const = self._const(imp[1], imp[2])
                state = ("const", const) if const is not None else None
        elif base in mod.consts:
            state = ("const", mod.consts[base])
        for attr in chain:
            if state is None:
                return None
            kind = state[0]
            if kind == "module":
                nxt = self.symbol(state[1], attr)
                if nxt is None:
                    const = self._const(state[1], attr)
                    nxt = ("const", const) if const is not None else None
                state = nxt
            elif kind == "class":
                state = ("method", state[1], state[2], attr) if attr in self.modules[state[1]].classes.get(state[2], {}) else None
            elif kind == "instance":
                state = ("method", state[1], state[2], attr) if attr in self.modules[state[1]].classes.get(state[2], {}) else None
            else:
                return None
        return state

    def _const(self, modname: str, name: str):
        mod = self.modules.get(modname)
        return mod.consts.get(name) if mod else None

    def func_for(self, state) -> Func | None:
        """解決結果が指す関数・メソッド（クラスは __init__）。"""
        if not state:
            return None
        if state[0] == "func":
            return self.modules[state[1]].funcs.get(state[2])
        if state[0] == "method":
            return self.modules[state[1]].classes[state[2]].get(state[3])
        if state[0] == "class":
            return self.modules[state[1]].classes[state[2]].get("__init__")
        return None


# ---------------------------------------------------------------------------
# 環境変数の読み取り
# ---------------------------------------------------------------------------

def _direct_env_arg(node: ast.AST):
    """os.environ の直接の読み取り（get・getenv・[...]・in）なら、名前の式を返す。"""
    if isinstance(node, ast.Call) and node.args and ast.unparse(node.func) in DIRECT_ENV_READERS:
        return node.args[0], ast.unparse(node.func)
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and ast.unparse(node.value) in ENV_CONTAINERS:
        return node.slice, "os.environ[...]"
    if (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and ast.unparse(node.comparators[0]) in ENV_CONTAINERS):
        return node.left, "in os.environ"
    return None


def _param_names(fn) -> list[str]:
    a = fn.args
    return [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


def find_env_wrappers(index: PyIndex) -> dict[tuple[str, str], tuple[str, int | None]]:
    """環境変数名を引数で受けて os.environ を読む関数（env_int など）を、固定点まで自動で見つける。
    キーは (モジュール, 関数名)、値は (名前を受ける仮引数名, 位置引数の番号。キーワード専用なら None)。"""
    wrappers: dict[tuple[str, str], tuple[str, int | None]] = {}
    changed = True
    while changed:
        changed = False
        for mod in index.modules.values():
            for fn in ast.walk(mod.tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or (mod.name, fn.name) in wrappers:
                    continue
                positional = [x.arg for x in (*fn.args.posonlyargs, *fn.args.args)]
                params = set(_param_names(fn))
                for node in ast.walk(fn):
                    direct = _direct_env_arg(node)
                    arg = direct[0] if direct else None
                    if arg is None and isinstance(node, ast.Call):
                        wr = _wrapper_call(index, mod, node, wrappers)
                        arg = wr[0] if wr else None
                    if isinstance(arg, ast.Name) and arg.id in params:
                        idx = positional.index(arg.id) if arg.id in positional else None
                        wrappers[(mod.name, fn.name)] = (arg.id, idx)
                        changed = True
                        break
    return wrappers


def _wrapper_call(index: PyIndex, mod: Module, node: ast.Call, wrappers: dict):
    """呼び出しが環境変数名を受ける関数なら、(名前の式, 関数名) を返す。"""
    state = index.resolve(mod, node.func)
    if not state or state[0] != "func" or (state[1], state[2]) not in wrappers:
        return None
    param, idx = wrappers[(state[1], state[2])]
    arg = node.args[idx] if idx is not None and idx < len(node.args) else next((k.value for k in node.keywords if k.arg == param), None)
    return (arg, state[2]) if arg is not None else None


def env_reads(index: PyIndex) -> dict:
    """環境変数の読み取りを集める。文字列リテラル（同じモジュール・import 先の定数に解決できるものを含む）の名前だけ採り、
    動的な名前は除外して件数を残す。"""
    wrappers = find_env_wrappers(index)
    reads, dynamic, passthrough = [], [], 0
    for mod in index.modules.values():
        for fn_scope in _scopes(mod):
            params = set(_param_names(fn_scope)) if isinstance(fn_scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else set()
            for node in _walk_own(fn_scope):
                found = _direct_env_arg(node)
                via = found[1] if found else None
                arg = found[0] if found else None
                if not found and isinstance(node, ast.Call):
                    wr = _wrapper_call(index, mod, node, wrappers)
                    if wr:
                        arg, via = wr[0], f"wrapper:{wr[1]}"
                if arg is None:
                    continue
                name = _literal_name(index, mod, arg)
                line = getattr(node, "lineno", 0)
                if name is not None:
                    reads.append({"name": name, "rel": mod.rel, "line": line, "via": via})
                elif isinstance(arg, ast.Name) and arg.id in params:
                    passthrough += 1
                else:
                    dynamic.append({"rel": mod.rel, "line": line, "expr": ast.unparse(arg)[:80], "via": via})
    return {"reads": sorted(reads, key=lambda r: (r["name"], r["rel"], r["line"])),
            "dynamic": sorted(dynamic, key=lambda r: (r["rel"], r["line"])),
            "wrappers": sorted(f"{m}:{n}" for m, n in wrappers), "passthrough_in_wrappers": passthrough}


def _scopes(mod: Module):
    """各トップレベルの関数・メソッドと、残りのモジュール直下のコードをそれぞれ 1 スコープとして返す。"""
    top_defs = []
    for stmt in mod.tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            top_defs.append(stmt)
        elif isinstance(stmt, ast.ClassDef):
            top_defs.extend(i for i in stmt.body if isinstance(i, (ast.FunctionDef, ast.AsyncFunctionDef)))
    yield mod.tree
    for d in top_defs:
        yield d
        # 入れ子の関数も独立のスコープとして見る（仮引数の判定のため）
        for n in ast.walk(d):
            if n is not d and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield n


def _literal_name(index: PyIndex, mod: Module, arg: ast.AST) -> str | None:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    state = index.resolve(mod, arg) if isinstance(arg, (ast.Name, ast.Attribute)) else None
    if state and state[0] == "const":
        return state[1]
    return None


def env_gold(index: PyIndex) -> dict:
    result = env_reads(index)
    by_name: dict[str, dict] = collections.defaultdict(lambda: {"files": set(), "sites": []})
    other = collections.Counter()
    for r in result["reads"]:
        if not r["name"].startswith(ENV_PREFIX):
            other[r["name"]] += 1
            continue
        entry = by_name[r["name"]]
        entry["files"].add(r["rel"])
        entry["sites"].append(f'{r["rel"]}:{r["line"]}')
    items = {n: {"files": sorted(e["files"]), "sites": sorted(set(e["sites"]))} for n, e in sorted(by_name.items())}
    return {"items": items, "dynamic_count": len(result["dynamic"]), "dynamic_sites": result["dynamic"],
            "non_sherpa_names": len(other), "non_sherpa_reads": sum(other.values()), "wrappers": result["wrappers"],
            "passthrough_in_wrappers": result["passthrough_in_wrappers"]}


# ---------------------------------------------------------------------------
# HTTP ルート・MCP の道具
# ---------------------------------------------------------------------------

def http_routes(index: PyIndex) -> list[dict]:
    """`@x.get("/path")` などのデコレータ（先頭引数が文字列リテラル）から、メソッド・パス・定義ファイルを集める。
    APIRouter(prefix=...) があればその接頭辞を付ける。"""
    routes = []
    for mod in index.modules.values():
        prefixes = {}
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and ast.unparse(node.value.func).endswith("APIRouter"):
                prefix = next((k.value.value for k in node.value.keywords
                               if k.arg == "prefix" and isinstance(k.value, ast.Constant)), "")
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        prefixes[t.id] = prefix
        for fn in ast.walk(mod.tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in fn.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in HTTP_METHODS
                        and dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str)):
                    recv = dec.func.value.id if isinstance(dec.func.value, ast.Name) else ""
                    routes.append({"method": dec.func.attr.upper(), "path": prefixes.get(recv, "") + dec.args[0].value,
                                   "rel": mod.rel, "line": dec.lineno, "function": fn.name})
    return sorted(routes, key=lambda r: (r["rel"], r["line"]))


def mcp_tools(index: PyIndex) -> list[dict]:
    """`{"name": ..., "description": ..., "inputSchema": ...}` の辞書リテラルから、道具の名前と定義ファイルを集める。"""
    tools = []
    for mod in index.modules.values():
        for node in ast.walk(mod.tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            name = keys.get("name")
            if ("description" in keys and "inputSchema" in keys and isinstance(name, ast.Constant)
                    and isinstance(name.value, str)):
                tools.append({"name": name.value, "rel": mod.rel, "line": node.lineno})
    return sorted(tools, key=lambda t: (t["name"], t["rel"], t["line"]))


# ---------------------------------------------------------------------------
# 設計書の節
# ---------------------------------------------------------------------------

def md_sections(text: str) -> list[dict]:
    """見出しごとの節（見出し行から次の見出しの直前まで）。コードブロック内の # は見出しにしない。"""
    sections, cur, fence = [], None, False
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fence = not fence
        m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line) if not fence else None
        if m:
            cur = {"level": len(m.group(1)), "heading": m.group(2), "line": i, "body": []}
            sections.append(cur)
        elif cur is not None:
            cur["body"].append(line)
    for s in sections:
        s["text"] = s["heading"] + "\n" + "\n".join(s.pop("body"))
    return sections


def design_sections(root: Path, files: list[str]) -> list[dict]:
    out = []
    for rel in files:
        if not rel.startswith("docs/design/") or not rel.endswith(".md"):
            continue
        for s in md_sections((root / rel).read_text(encoding="utf-8")):
            out.append({"rel": rel, "heading": s["heading"], "line": s["line"], "text": s["text"]})
    return out


def mentions_in_sections(sections: list[dict], name: str, backticked: bool = True) -> list[dict]:
    """名前を含む節。backticked=True ならコード片（`name`）として書かれている節だけ。"""
    if backticked:
        pat = re.compile(r"`" + re.escape(name) + r"`")
    else:
        pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
    return [{"path": s["rel"], "heading": s["heading"], "line": s["line"]} for s in sections if pat.search(s["text"])]


# ---------------------------------------------------------------------------
# 呼び出しグラフ
# ---------------------------------------------------------------------------

class CallGraph:
    """関数・メソッドの単位で「誰が誰を参照しているか」を静的に解決した逆向きの辺を持つ。"""

    def __init__(self, index: PyIndex):
        self.index = index
        self.node_file: dict[str, str] = {}
        self.callers: dict[str, set[str]] = collections.defaultdict(set)
        for mod in index.modules.values():
            module_scope = f"{mod.name}:<module>"
            self.node_file[module_scope] = mod.rel
            for stmt in mod.tree.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._scan(mod, f"{mod.name}:{stmt.name}", stmt, None)
                elif isinstance(stmt, ast.ClassDef):
                    for base in stmt.bases + [k.value for k in stmt.keywords]:
                        self._scan(mod, module_scope, base, None)
                    for item in stmt.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            self._scan(mod, f"{mod.name}:{stmt.name}.{item.name}", item, stmt.name)
                        else:
                            self._scan(mod, module_scope, item, stmt.name)
                else:
                    self._scan(mod, module_scope, stmt, None)

    def _scan(self, mod: Module, scope: str, root: ast.AST, cls: str | None) -> None:
        self.node_file[scope] = mod.rel
        for node in ast.walk(root):
            if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
                target = self.index.func_for(self.index.resolve(mod, node, cls))
                if target is not None and target.qual != scope:
                    self.node_file[target.qual] = target.rel
                    self.callers[target.qual].add(scope)

    def closure(self, qual: str, max_hop: int) -> dict[str, int]:
        """qual の呼び出し元を max_hop 段まで逆向きにたどる。node -> 最短の段数。"""
        dist, frontier = {qual: 0}, [qual]
        for hop in range(1, max_hop + 1):
            nxt = []
            for n in frontier:
                for c in sorted(self.callers.get(n, ())):
                    if c not in dist:
                        dist[c] = hop
                        nxt.append(c)
            frontier = nxt
        dist.pop(qual)
        return dist

    def files_at(self, qual: str, max_hop: int) -> dict[str, int]:
        """影響するファイル -> そのファイルに最初に届く段数（qual の定義ファイル自体は除く）。"""
        own = self.node_file.get(qual)
        out: dict[str, int] = {}
        for node, hop in self.closure(qual, max_hop).items():
            rel = self.node_file[node]
            if rel != own and (rel not in out or hop < out[rel]):
                out[rel] = hop
        return out


def _stable_key(*parts) -> str:
    return hashlib.sha256((SEED + ":" + ":".join(map(str, parts))).encode()).hexdigest()


def pick_hop_targets(graph: CallGraph, hops=(2, 3, 4), lo: int = 5, hi: int = 30, exclude_modules=()) -> dict:
    """段数ごとに、呼び出し元が ちょうどその段数まで続き、ファイル数が lo〜hi になるモジュール直下の関数を決定的に 1 つ選ぶ。
    選び方は SEED と関数の修飾名のハッシュの最小。戻り値を持たない関数・__dunder__・除外モジュールは候補にしない。"""
    chosen, taken = {}, set()
    funcs = [f for m in graph.index.modules.values() for f in m.funcs.values()
             if f.returns_value and not f.name.startswith("__") and f.module not in exclude_modules]
    cache = {}
    for f in funcs:
        cache[f.qual] = graph.closure(f.qual, max(hops))
    for h in hops:
        cands = []
        for f in funcs:
            dist = cache[f.qual]
            if not any(d == h for d in dist.values()):
                continue
            files = graph.files_at(f.qual, h)
            if lo <= len(files) <= hi and f.qual not in taken:
                cands.append((_stable_key(h, f.qual), f.qual, len(files)))
        if not cands:
            raise RuntimeError(f"hop={h} の候補がありません")
        cands.sort()
        chosen[h] = {"qual": cands[0][1], "candidates": len(cands)}
        taken.add(cands[0][1])
    return chosen


# ---------------------------------------------------------------------------
# 食い違い（設計書を直した過去のコミットの差分から）
# ---------------------------------------------------------------------------

def removed_lines(repo: Path, fix_commit: str, rel: str) -> list[str] | None:
    """設計書を直したコミットで消えた行（旧記述）。git が使えなければ None。"""
    proc = subprocess.run(["git", "-C", str(repo), "diff", "--unified=0", f"{fix_commit}^", fix_commit, "--", rel],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return [ln[1:] for ln in proc.stdout.splitlines() if ln.startswith("-") and not ln.startswith("---")]


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _set_len(root: Path, rel: str, name: str) -> int | None:
    for node in ast.parse(_read(root, rel)).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                return len(ast.literal_eval(node.value))
            except (ValueError, TypeError):
                return None
    return None


def run_code_check(root: Path, check: dict) -> dict:
    kind, rel = check["kind"], check["path"]
    text = _read(root, rel)
    if kind == "contains":
        ok = check["text"] in text
    elif kind == "not_regex":
        ok = re.search(check["pattern"], text) is None
    elif kind == "set_len":
        ok = _set_len(root, rel, check["name"]) == check["value"]
    else:
        raise ValueError(kind)
    return {**check, "ok": ok}


def verify_candidate(root: Path, cand: dict, removed: dict[str, list[str] | None]) -> dict:
    """旧記述が資料の設計書にあり（かつ設計書を直したコミットで消えた行であり）、実装の側が反対の事実を持つことを機械的に確かめる。"""
    doc_text = _read(root, cand["doc"])
    needle = cand["doc_needle"]
    doc_has = needle in doc_text
    gone = removed.get(cand["doc"])
    in_removed = None if gone is None else needle in "\n".join(gone)
    code = [run_code_check(root, c) for c in cand["code_checks"]]
    verified = doc_has and in_removed is not False and all(c["ok"] for c in code)
    return {"id": cand["id"], "topic": cand["topic"], "doc": cand["doc"], "doc_section": cand["doc_section"],
            "doc_claim": needle, "doc_text_present": doc_has, "removed_by_design_fix": in_removed,
            "code_fact": cand["code_fact"], "code_paths": cand["code_paths"], "code_checks": code, "verified": verified}


def verify_omission(root: Path, cand: dict) -> dict:
    """設計書が黙っている（記述漏れ）かどうか。語が資料の設計書に無ければ「食い違いでなく記述漏れ」として除外する。"""
    silent = {}
    for term in cand["doc_terms"]:
        silent[term] = [rel for rel in cand["docs"] if term in _read(root, rel)]
    code = [run_code_check(root, c) for c in cand["code_checks"]]
    return {"id": cand["id"], "source": cand["source"], "topic": cand["topic"], "doc_mentions": silent,
            "doc_silent": all(not v for v in silent.values()), "code_checks": code, "code_fact": cand["code_fact"],
            "excluded_reason": cand["excluded_reason"]}


GAP_QUESTIONS = [
    {
        "qid": "self-gap-rag-es",
        "question": ("設計書 docs/design/rag.md の ES 索引まわりの節（§3 の検索の入力、§5.2 埋め込み、§5.3 ES 索引の構成）と、"
                     "実装（sherpa/es_index.py）の食い違いを挙げてください。設計書と実装が食い違うときはソースを正として報告し、"
                     "根拠のファイルを示してください。"),
        "doc": "docs/design/rag.md",
        "candidates": [
            {"id": "rag-es-hybrid-query", "topic": "ハイブリッド検索の組み立て",
             "doc": "docs/design/rag.md", "doc_section": "5.3 ES 索引の構成（検索）",
             "doc_needle": "`match`/`knn` を同じ `bool.should` に入れてから",
             "code_fact": "BM25 と kNN は別々の問い合わせで引き、アプリ側で fuse_rrf により順位を融合する（1 つの bool.should にはしない）",
             "code_paths": ["sherpa/es_index.py"],
             "code_checks": [{"kind": "contains", "path": "sherpa/es_index.py", "text": "def _fuse_hybrid("},
                             {"kind": "contains", "path": "sherpa/es_index.py", "text": "from .parts.read.fused_search import fuse_rrf"}]},
            {"id": "rag-es-source-input", "topic": "RAG 検索（ES）の入力にソース原文が入るか",
             "doc": "docs/design/rag.md", "doc_section": "3 派生物の層（RAG 検索の入力）",
             "doc_needle": "**RAG 検索の入力はソース原文＋ `rag/` 層のみ**",
             "code_fact": "登録コード（branch==source）と汎用コードは ES の索引に入れない（_is_source_doc）。ソースの全文検索は rg に一本化",
             "code_paths": ["sherpa/es_index.py"],
             "code_checks": [{"kind": "contains", "path": "sherpa/es_index.py", "text": "def _is_source_doc("},
                             {"kind": "contains", "path": "sherpa/es_index.py", "text": 'emeta["source_excluded"] = True'}]},
            {"id": "rag-es-source-embed", "topic": "ソースのチャンクの埋め込み除外",
             "doc": "docs/design/rag.md", "doc_section": "5.2 埋め込み",
             "doc_needle": "`branch==\"source\"`（コード原文・軽量テキスト枠）のチャンクは埋め込み対象から除外",
             "code_fact": "ソースのチャンクは ES に存在しない（埋め込みから「除外」ではなく索引に入らない）。埋め込みを外すのはテキスト資料（no_embed）",
             "code_paths": ["sherpa/es_index.py"],
             "code_checks": [{"kind": "contains", "path": "sherpa/es_index.py", "text": "ソース原文は ES に入れない"},
                             {"kind": "contains", "path": "sherpa/es_index.py", "text": "no_embed"}]},
            {"id": "rag-es-code-text-slot", "topic": "軽量テキスト枠のコード側が ES 全文に入るか",
             "doc": "docs/design/rag.md", "doc_section": "4 取り込みのアーム（軽量テキスト枠）",
             "doc_needle": "いずれも台帳・出典 DL・grep・ES 全文・read_around まで対象に入るが",
             "code_fact": "軽量テキスト枠の第 1 段（汎用コード）は _is_source_doc が真で ES に入らない。第 2 段（テキスト資料）だけが入る",
             "code_paths": ["sherpa/es_index.py", "sherpa/ingest/text_kind.py"],
             "code_checks": [{"kind": "contains", "path": "sherpa/es_index.py", "text": "text_kind.CODE_DOCTYPE_LABEL"},
                             {"kind": "contains", "path": "sherpa/ingest/text_kind.py", "text": "CODE_DOCTYPE_LABEL"}]},
        ],
    },
    {
        "qid": "self-gap-graph-cobol-io",
        "question": ("設計書 docs/05-グラフ語彙.md のノードラベル・エッジ型の数と、COBOL のファイル入出力（SELECT／READ／WRITE）の"
                     "扱いの記述（§1・§2・§4 の抽出規則の表・§9 の未対応の一覧）について、実装との食い違いを挙げてください。"
                     "設計書と実装が食い違うときはソースを正として報告し、根拠のファイルを示してください。"),
        "doc": "docs/05-グラフ語彙.md",
        "candidates": [
            {"id": "graph-cobol-file-io-planned", "topic": "COBOL の SELECT/READ/WRITE が計画枠か（§4 の表）",
             "doc": "docs/05-グラフ語彙.md", "doc_section": "4 抽出規則（COBOL の EXEC SQL の行）",
             "doc_needle": "`SELECT/READ/WRITE`／FILE SECTION は引き続き計画枠",
             "code_fact": "COBOL アナライザが SELECT…ASSIGN と OPEN/READ/WRITE を FileIo として返し、world_graph._dataflow_pass がデータの流れの辺にする",
             "code_paths": ["sherpa/ingest/analyzers/cobol.py", "sherpa/ingest/world_graph.py"],
             "code_checks": [{"kind": "contains", "path": "sherpa/ingest/analyzers/cobol.py", "text": "FileIo("},
                             {"kind": "contains", "path": "sherpa/ingest/world_graph.py", "text": "def _dataflow_pass("}]},
            {"id": "graph-cobol-file-io-limit", "topic": "未対応として残るもの（§9 の 2）に COBOL のファイル入出力があるか",
             "doc": "docs/05-グラフ語彙.md", "doc_section": "9 未解決の課題（2）",
             "doc_needle": "残るのは COBOL の `SELECT`/`READ`/`WRITE`",
             "code_fact": "同上（実装済み。Dataset と FLOWS_TO に結ぶ）",
             "code_paths": ["sherpa/ingest/analyzers/cobol.py", "sherpa/ingest/world_graph.py"],
             "code_checks": [{"kind": "contains", "path": "sherpa/ingest/analyzers/cobol.py", "text": "FileIo("}]},
            {"id": "graph-node-label-count", "topic": "ノードラベルの数（§1 の見出し）",
             "doc": "docs/05-グラフ語彙.md", "doc_section": "1 ノードラベル",
             "doc_needle": "## 1. ノードラベル（クローズド・7種）",
             "code_fact": "NODE_LABELS は 8 種（Dataset が加わっている）",
             "code_paths": ["sherpa/ingest/model.py"],
             "code_checks": [{"kind": "set_len", "path": "sherpa/ingest/model.py", "name": "NODE_LABELS", "value": 8}]},
            {"id": "graph-edge-type-count", "topic": "エッジ型の数（§2 の見出し）",
             "doc": "docs/05-グラフ語彙.md", "doc_section": "2 エッジ型",
             "doc_needle": "## 2. エッジ型（クローズド・5種＋世代跨ぎの対応1種）",
             "code_fact": "EDGE_TYPES は 6 種（FLOWS_TO が加わっている）",
             "code_paths": ["sherpa/ingest/model.py"],
             "code_checks": [{"kind": "set_len", "path": "sherpa/ingest/model.py", "name": "EDGE_TYPES", "value": 6}]},
        ],
    },
    {
        "qid": "self-gap-graph-impact-bfs",
        "question": ("設計書 docs/05-グラフ語彙.md §6「影響分析クエリ」（影響のたどり方と深さの上限）と、実装（sherpa/ingest/world_neo4j.py の"
                     "影響たどり）の食い違いを挙げてください。設計書と実装が食い違うときはソースを正として報告し、根拠のファイルを示してください。"),
        "doc": "docs/05-グラフ語彙.md",
        "candidates": [
            {"id": "graph-impact-variable-length", "topic": "影響たどりが可変長パスの Cypher か",
             "doc": "docs/05-グラフ語彙.md", "doc_section": "6 影響分析クエリ",
             "doc_needle": "-[r:COPIES|CONTAINS|INVOKES|ACCESSES*1..8]->(start)",
             "code_fact": "段ごとに 1 段広げる幅優先探索（world_impact）。訪問済みと親は Python 側が持ち、可変長パス（*1..N）の問い合わせは使わない",
             "code_paths": ["sherpa/ingest/world_neo4j.py"],
             "code_checks": [{"kind": "contains", "path": "sherpa/ingest/world_neo4j.py", "text": "_IMPACT_FRONTIER_CHUNK"},
                             {"kind": "contains", "path": "sherpa/ingest/world_neo4j.py", "text": "def world_impact("},
                             {"kind": "not_regex", "path": "sherpa/ingest/world_neo4j.py", "pattern": r"\*1\.\."}]},
            {"id": "graph-impact-depth-limit", "topic": "深さの上限の値",
             "doc": "docs/05-グラフ語彙.md", "doc_section": "6 影響分析クエリ",
             "doc_needle": "-[r:COPIES|CONTAINS|INVOKES|ACCESSES*1..8]->(start)",
             "code_fact": "深さの上限は IMPACT_MAX_DEPTH = 10（設計書の例の 8 と違う）",
             "code_paths": ["sherpa/ingest/world_neo4j.py"],
             "code_checks": [{"kind": "contains", "path": "sherpa/ingest/world_neo4j.py", "text": "IMPACT_MAX_DEPTH = 10"}]},
        ],
    },
]

# 設計書が黙っている（記述漏れ）ため、食い違いの正解に入れない候補。課題管理簿の起票内容は 9a539bb1 の設計書では「食い違い」でない。
OMISSION_CANDIDATES = [
    {"id": "SRH-08", "source": "docs/proposals/課題管理簿.md SRH-08", "topic": "ES の labels・region_context が検索条件に使われない",
     "docs": ["docs/design/rag.md"], "doc_terms": ["region_context", "`labels`"],
     "code_fact": "索引のマッピングには labels・region_context があるが、問い合わせの組み立てには出てこない（コード側の課題）",
     "code_checks": [{"kind": "contains", "path": "sherpa/es_index.py", "text": '"region_context": {"type": "text"'}],
     "excluded_reason": "9a539bb1 の設計書（rag.md）は labels・region_context に一切言及しない。設計書の記述と実装が食い違うのではなく、"
                        "コード側の課題と設計書の記述漏れ。0.17.2 の設計書反映で初めて記述が入った。"},
    {"id": "ANA-22", "source": "docs/proposals/課題管理簿.md ANA-22", "topic": "ALLOWED_REFS が FLOWS_TO を許すがルールから出した場合が未検証",
     "docs": ["docs/21-拡張の契約.md", "docs/05-グラフ語彙.md"], "doc_terms": ["`use`＝`FLOWS_TO`", "FLOWS_TO（use"],
     "code_fact": "ALLOWED_REFS に FLOWS_TO の組がある（コード側の課題）",
     "code_checks": [{"kind": "contains", "path": "sherpa/ingest/analyzers/_rules.py", "text": "FLOWS_TO"}],
     "excluded_reason": "9a539bb1 の設計書は ALLOWED_REFS の FLOWS_TO の組に言及しない（記述漏れ）。ルールで足すことを想定しないかの判断が"
                        "未決の実装側の課題で、設計書が実装に反する記述ではない。"},
    {"id": "RAG-STRUCT-FIELDS", "source": "64ca61cc の rag.md 差分（追加された行）", "topic": "構造の欄（identifier_values など）と exact の追加",
     "docs": ["docs/design/rag.md", "docs/design/interfaces.md"], "doc_terms": ["identifier_values", "record_identity_values"],
     "code_fact": "マッピングに identifier_values・record_identity_values があり、es_search に exact がある",
     "code_checks": [{"kind": "contains", "path": "sherpa/es_index.py", "text": '"identifier_values": {"type": "keyword"}'}],
     "excluded_reason": "旧記述が実装に反するのではなく、旧記述にこれらの欄・引数が無い（記述漏れ）。食い違いの正解には入れない。"},
    {"id": "IMPACT-GRAPH-RETURN", "source": "64ca61cc の interfaces.md 差分（追加された行）", "topic": "graph_impact の戻りの summary・result_file・flow_candidates",
     "docs": ["docs/design/interfaces.md"], "doc_terms": ["flow_candidates", "result_file"],
     "code_fact": "run_tool の graph_impact が flow_depth の引数と summary 等の欄を持つ",
     "code_checks": [{"kind": "contains", "path": "sherpa/graph_tools.py", "text": "flow_depth"}],
     "excluded_reason": "旧記述にこれらの引数・欄が無いだけの記述漏れ。旧記述の明示的な誤りではないため食い違いの正解には入れない。"},
]


def gap_gold(root: Path, repo: Path | None, fix_commit: str | None) -> dict:
    removed: dict[str, list[str] | None] = {}
    for q in GAP_QUESTIONS:
        for c in q["candidates"]:
            if c["doc"] not in removed:
                removed[c["doc"]] = removed_lines(repo, fix_commit, c["doc"]) if repo and fix_commit else None
    questions, excluded = [], []
    for q in GAP_QUESTIONS:
        results = [verify_candidate(root, c, removed) for c in q["candidates"]]
        kept = [r for r in results if r["verified"]]
        questions.append({"qid": q["qid"], "question": q["question"], "doc": q["doc"], "results": results, "kept": [r["id"] for r in kept]})
        excluded.extend({"id": r["id"], "reason": "検証に通らなかった", "detail": {k: r[k] for k in
                                                                               ("doc_text_present", "removed_by_design_fix")}}
                        for r in results if not r["verified"])
    omissions = [verify_omission(root, c) for c in OMISSION_CANDIDATES]
    return {"questions": questions, "excluded": excluded, "omissions": omissions,
            "diff_checked": all(v is not None for v in removed.values())}


# ---------------------------------------------------------------------------
# 質問の組み立て
# ---------------------------------------------------------------------------

def build_questions(root: Path, files: list[str], index: PyIndex, repo: Path | None, fix_commit: str | None,
                    hops=(2, 3, 4), file_range=(5, 30)) -> tuple[list, dict, dict]:
    questions, gold, notes = [], {}, {}
    scope: list[str] = []  # 資料フォルダ全体（範囲はフォルダでしか指定できず、ルート直下のファイルを含めるには空＝全体にする）

    def add(q: dict, detail: dict) -> None:
        q = {**q, "system": SYSTEM, "scope_paths": scope}
        questions.append(q)
        gold[q["id"]] = {"id": q["id"], "type": q["type"], "question": q["question"], **detail}

    # 1. 環境変数（一覧・件数が多い）
    env = env_gold(index)
    env_files = sorted({f for e in env["items"].values() for f in e["files"]})
    add({"id": "self-env-vars", "type": "list", "hop": 0,
         "question": ("Sherpa（sherpa/ 配下のコード）が読む環境変数のうち、名前が SHERPA_ で始まるものを全て挙げ、"
                      "それぞれを読んでいるファイルを示してください。"),
         "expected_paths": env_files, "item_count": len(env["items"]),
         "generation": {"function": "env_gold", "rule": "ast で sherpa/ 配下を走査。os.environ.get／os.getenv／os.environ[...]／`in os.environ` と、"
                        "環境変数名を引数で受けて os.environ を読む関数（env_int など・固定点まで自動検出）の呼び出しのうち、"
                        "名前が文字列リテラル（同モジュール・import 先の文字列定数に解決できるものを含む）で SHERPA_ から始まるものを採る。"
                        "ファイルは呼び出しのあるファイル。動的な名前は除外して件数を記録。"}},
        {"items": {n: e["files"] for n, e in env["items"].items()}, "sites": {n: e["sites"] for n, e in env["items"].items()},
         "dynamic_count": env["dynamic_count"], "dynamic_sites": env["dynamic_sites"], "wrappers": env["wrappers"],
         "non_sherpa_names": env["non_sherpa_names"], "non_sherpa_reads": env["non_sherpa_reads"]})

    # 1b. HTTP ルート（一覧・件数が多い）
    routes = http_routes(index)
    route_files = sorted({r["rel"] for r in routes})
    add({"id": "self-http-routes", "type": "list", "hop": 0,
         "question": ("Sherpa の HTTP API（FastAPI）で、@router／@app の get・post・put・delete・patch のデコレータに文字列リテラルのパスで"
                      "定義されているエンドポイントを全て挙げ（メソッドとパス）、それぞれを定義しているファイルを示してください。"),
         "expected_paths": route_files, "item_count": len(routes),
         "generation": {"function": "http_routes", "rule": "ast で sherpa/ 配下の関数のデコレータ `@x.get|post|put|delete|patch(\"…\")`（先頭引数が"
                        "文字列リテラル）を集め、同じファイルの APIRouter(prefix=…) があれば接頭辞を付ける。ファイルは定義のあるファイル。"}},
        {"items": [{"method": r["method"], "path": r["path"], "file": r["rel"], "line": r["line"], "function": r["function"]} for r in routes]})

    # 2. 設計書とコードをまたぐ一覧 × 2
    sections = design_sections(root, files)
    tools = mcp_tools(index)
    tool_names = sorted({t["name"] for t in tools})
    tool_def_files = sorted({t["rel"] for t in tools})
    tool_map, design_files = {}, set()
    for n in tool_names:
        found = mentions_in_sections(sections, n)
        tool_map[n] = found
        design_files.update(s["path"] for s in found)
    add({"id": "self-mcp-tools-docs", "type": "list_cross", "hop": 0,
         "question": ("Sherpa の MCP サーバが公開する道具（ツール）の名前を全て挙げ、それぞれを説明している設計書（docs/design 配下）の節を"
                      "示してください。設計書に出てこない道具は「設計書に無い」と明記してください。"),
         "expected_paths": sorted(set(tool_def_files) | design_files), "item_count": len(tool_names),
         "generation": {"function": "mcp_tools + mentions_in_sections", "rule": "ast で {\"name\", \"description\", \"inputSchema\"} を持つ辞書リテラルから"
                        "道具名と定義ファイルを集め、docs/design/*.md の節（見出しから次の見出しまで）のうち、道具名がコード片（`名前`）として"
                        "書かれている節を対応づける。対応が無い道具は「設計書に無い」。expected_paths は定義ファイルと対応した設計書。"}},
        {"items": {n: {"defined_in": sorted({t["rel"] for t in tools if t["name"] == n}),
                       "design_sections": tool_map[n] or "設計書に無い"} for n in tool_names},
         "not_in_design": [n for n in tool_names if not tool_map[n]]})

    env_map, env_design_files = {}, set()
    for n in env["items"]:
        found = mentions_in_sections(sections, n)
        env_map[n] = found
        env_design_files.update(s["path"] for s in found)
    add({"id": "self-env-vars-docs", "type": "list_cross", "hop": 0,
         "question": ("Sherpa が読む環境変数のうち SHERPA_ で始まるものを全て挙げ、それぞれを説明している設計書（docs/design 配下）の節を"
                      "示してください。設計書に出てこない環境変数は「設計書に無い」と明記してください。"),
         "expected_paths": sorted(env_design_files), "item_count": len(env["items"]),
         "generation": {"function": "env_gold + mentions_in_sections", "rule": "環境変数の一覧は self-env-vars と同じ規則。docs/design/*.md の節のうち、"
                        "変数名がコード片（`名前`）として書かれている節を対応づける。対応が無い変数は「設計書に無い」。"
                        "expected_paths は対応した設計書のファイル（読んでいるコードのファイルは含めない）。"}},
        {"items": {n: env_map[n] or "設計書に無い" for n in env["items"]}, "not_in_design": [n for n in env["items"] if not env_map[n]]})

    # 3. 多段（呼び出しをたどる）× 3
    graph = CallGraph(index)
    targets = pick_hop_targets(graph, hops, *file_range)
    for h, info in sorted(targets.items()):
        qual = info["qual"]
        fn = next(f for m in index.modules.values() for f in m.funcs.values() if f.qual == qual)
        file_hops = graph.files_at(qual, h)
        add({"id": f"self-impact-hop{h}", "type": "multi_hop", "hop": h,
             "question": (f"{fn.rel} にある関数 {fn.name}() の戻り値の形（返す値の構造）を変えるとします。"
                          f"{fn.name}() を呼んでいる関数、その関数の呼び出し元、……と、呼び出しを {h} 段目の呼び出し元までたどって、"
                          f"影響を受けるファイルを全て挙げてください（import した別名や別モジュール経由の呼び出しも含む。{fn.rel} 自体は除く）。"),
             "expected_paths": sorted(file_hops), "item_count": len(file_hops),
             "generation": {"function": "pick_hop_targets + CallGraph.files_at", "rule": f"ast で sherpa/ の関数・メソッドごとに、Name／Attribute の参照を"
                            f"import（別名・相対・再エクスポート）と self.メソッド・クラス生成（__init__）を含めて解決し、{fn.name}() の呼び出し元を"
                            f"逆向きに {h} 段まで広げた閉包に含まれる関数のファイル。候補は戻り値のある関数で、ちょうど {h} 段目の呼び出し元があり、"
                            f"ファイル数が 5〜30 のもの。候補から SEED と修飾名のハッシュが最小のものを選ぶ。"}},
            {"target": {"qual": qual, "file": fn.rel, "line": fn.lineno, "function": fn.name}, "hop": h,
             "candidates_considered": info["candidates"], "files": file_hops,
             "nodes": {n: d for n, d in sorted(graph.closure(qual, h).items(), key=lambda kv: (kv[1], kv[0]))}})

    # 4. 食い違い × 3（未確認）
    gaps = gap_gold(root, repo, fix_commit)
    for g in gaps["questions"]:
        kept = [r for r in g["results"] if r["verified"]]
        if not kept:
            continue
        paths = sorted({g["doc"], *(p for r in kept for p in r["code_paths"])})
        add({"id": g["qid"], "type": "discrepancy", "hop": 0, "question": g["question"], "expected_paths": paths,
             "item_count": len(kept), "needs_user_review": True,
             "generation": {"function": "gap_gold", "rule": f"設計書を直したコミット（{fix_commit}）の差分で消えた旧記述のうち、(1) 資料の設計書に"
                            "そのまま残っていて、(2) 実装（資料のコード）が反対の事実を持つことを文字列・ast で確かめられたものだけを正解にする。"
                            "旧記述の選択と事実の要約は人が書いた定数（GAP_QUESTIONS）なので、利用者の確認が済むまで評価に使わない。"}},
            {"discrepancies": kept, "all_candidates": g["results"], "needs_user_review": True})
    notes["gaps"] = gaps
    notes["env"] = {"count": len(env["items"]), "dynamic_count": env["dynamic_count"]}
    notes["call_graph_targets"] = targets
    return questions, gold, notes


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def prepare(repo: Path, commit: str, out: Path, fix_commit: str | None = DEFAULT_DESIGN_FIX,
            src_override: Path | None = None, hops=(2, 3, 4), file_range=(5, 30)) -> dict:
    """出力先に資料フォルダ・manifest・正解を作る。src_override があればそのフォルダを取り出し済みの木として使う（テスト用）。"""
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"出力先が空ではありません（既存の固定データを上書きしないため停止）: {out}")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sherpa-self-") as tmp:
        if src_override is None:
            src = Path(tmp) / "tree"
            extract_archive(repo, commit, src)
            resolved = subprocess.run(["git", "-C", str(repo), "rev-parse", commit], capture_output=True, text=True, check=True).stdout.strip()
        else:
            src, resolved = src_override, commit
        files, skipped = collect_corpus_files(src)
        assert_no_leak(files)
        corpus = out / "corpus"
        copy_corpus(src, files, corpus)
    hashes = corpus_hashes(corpus, files)
    index = PyIndex(corpus)
    git_repo = repo if src_override is None else None
    questions, gold, notes = build_questions(corpus, files, index, git_repo, fix_commit if git_repo else None, hops, file_range)
    manifest = {
        "corpus_root": str(corpus), "corpus_files": files, "seed": SEED, "questions": questions,
        "source": {"commit": resolved, "requested": commit, "design_fix_commit": fix_commit},
        "corpus_stats": {k: hashes[k] for k in ("file_count", "total_bytes", "corpus_sha256")},
        "generator": GENERATOR, "scoring_notes": SCORING_NOTES,
    }
    write_json(out / "manifest.json", manifest)
    for qid, detail in gold.items():
        write_json(out / "gold" / f"{qid}.json", detail)
    write_json(out / "source-hashes.json", hashes["per_file"])
    provenance = {
        "generator": GENERATOR, "seed": SEED, "source": manifest["source"], "corpus_stats": manifest["corpus_stats"],
        "include_rule": {"trees": INCLUDE_TREES, "files": INCLUDE_FILES, "numbered_docs": NUMBERED_DOC_RE.pattern,
                         "text_extensions": sorted(TEXT_EXTS)},
        "leak_guard": {"prefixes": LEAK_PREFIXES, "files": LEAK_FILES, "part_names": sorted(LEAK_PART_NAMES),
                       "excluded_counts": skipped, "token_hits_in_corpus": leak_scan(corpus, files)},
        "parse_errors": index.parse_errors, "python_modules": len(index.modules), "notes": notes,
        "questions": [{k: q[k] for k in ("id", "type", "hop", "item_count")} | {"expected": len(q["expected_paths"])} for q in questions],
    }
    write_json(out / "provenance.json", provenance)
    return {"manifest": manifest, "provenance": provenance}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--commit", default=DEFAULT_COMMIT)
    ap.add_argument("--design-fix-commit", default=DEFAULT_DESIGN_FIX)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    result = prepare(args.repo.resolve(), args.commit, args.out.resolve(), args.design_fix_commit)
    prov = result["provenance"]
    print(json.dumps({"questions": prov["questions"], "corpus": prov["corpus_stats"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
