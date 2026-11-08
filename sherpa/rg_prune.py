"""grep の候補発見で rg に歩かせなくてよい場所を決める（read-only・結果は変えない）。
① 原本の木: Office／PDF／画像は `corpus_docs.classify_document` が常に「テキストとして読めない」と判定し、`grep_search` が開かずに捨てる。rg の走査からも除く。
② 派生 MD の木（`md/`）: `.rag.md` がある文書の `md/{原本}.md` は `grep_tool._resolve_entry` が選ばず捨てる。そうした文書しかないフォルダは rg に歩かせない。
計画は資料フォルダの署名（`search_cache.tree_signature`）ごとにプロセス内へ置く。署名が変われば（追加・削除・改名）作り直す。計画を作れなければ何も削らない。
設計: docs/design/rag.md「読み取り部品の道具」
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from . import rg_search, scope_infer

_MEMO: dict = {}
_MEMO_MAX = 8
_LOCK = threading.Lock()
_NO_PRUNE = {"skip_exts": [], "md_root": None, "md_dirs": None}


def _skippable_exts() -> set[str]:
    """原本として開く意味がない拡張子（Office／PDF／画像）。担当アナライザが登録されている拡張子は含めない（内容判定で読める可能性があるため）。"""
    from . import corpus_docs
    from .ingest import office_md
    from .ingest.analyzers import registry
    exts = set(corpus_docs._OFFICE_DOCTYPE) | set(office_md.IMAGE_EXT)
    return {e for e in exts if not registry.candidates("x" + e)}


def _rg_files(roots, globs, budget, scoped: bool = False) -> list[str]:
    """`roots` 配下で `globs` に一致するファイルの絶対パス。歩き方は `rg_search.discover` と同じ（隠しファイルあり・版管理のフォルダなし・リンクを辿らない）。
    範囲なしの `globs` は大文字小文字を区別しない（`--iglob`）。`scoped` のときは根からの相対（`/a/b/...`）で、区別する（`-g`・拡張子だけ呼び出し側で畳む）。
    """
    rg = rg_search.find_rg()
    if rg is None:
        raise rg_search.RgUnavailable("rg が見つかりません")
    out: list[str] = []
    for cwd, begin in rg_search._starts(roots, scoped):
        cmd = [rg, "--files", "--no-config", "--no-messages", "--hidden", "--no-ignore", "-0"]
        for g in globs:
            cmd += ["-g" if scoped else "--iglob", g]
        for name in sorted(scope_infer.VCS_DIR_NAMES):
            cmd += ["-g", f"!{name}/"]
        cmd += ["--", *begin]
        timeout = budget.remaining() if budget is not None else None
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, cwd=cwd)
        except subprocess.TimeoutExpired as e:
            raise rg_search.RgDeadlineExceeded("rg の走査が期限を超えました") from e
        except OSError as e:
            raise rg_search.RgUnavailable(f"rg を起動できません ({type(e).__name__})") from e
        if r.returncode not in (0, 1):
            raise rg_search.RgUnavailable(f"rg が正常に終わりませんでした (rc={r.returncode})")
        out += [rg_search._absolute(cwd, os.fsdecode(p)) for p in r.stdout.split(b"\0") if p]
    return out


def _skip_exts(orig_roots, budget, scope=None) -> list[str]:
    """除外してよい拡張子。同じ綴りで終わる名前のフォルダの下にファイルがある拡張子は含めない（rg の除外パターンはフォルダにも当たり、中身ごと消えるため）。
    `scope`（正規化済みの範囲）があれば、その部分木と、範囲の道筋（根から範囲まで）のフォルダ名だけを調べる。
    """
    exts = _skippable_exts()
    if not exts or not orig_roots:
        return []
    if scope is None:
        inside = _rg_files(orig_roots, [f"**/*{e}/**" for e in sorted(exts)], budget)
    else:
        for comp in (c.lower() for s in scope for c in s.split("/")):
            exts = {e for e in exts if not comp.endswith(e)}
        globs = [f"{p}/**/*{rg_search.fold_case(e)}/**"
                 for p in rg_search.scope_prefixes(scope) or [] for e in sorted(exts)]
        inside = _rg_files(orig_roots, globs, budget, True) if globs else []
    for path in inside:
        for comp in Path(path).parts[:-1]:
            low = comp.lower()
            exts = {e for e in exts if not low.endswith(e)}
    return sorted(exts)


def _kept(rel: str, rag_files: set[str]) -> bool:
    """派生 MD の物理名 `rel`（`md/` 側）が検索対象に選ばれるか。`grep_tool._resolve_entry` の「`.rag.md` と `.md` の二重ヒットを作らない」規則と同じ。"""
    from . import grep_tool
    if not (rel.endswith(".rag.md") or rel.endswith(".md")):
        return True
    origin = grep_tool.strip_derived_suffix(rel)
    return (origin + (".rag.md" if origin + ".rag.md" in rag_files else ".md")) == rel


def _md_dirs(md_root: Path, rag_root: Path, budget) -> list[str]:
    """`md_root` のうち rg に歩かせるフォルダ（互いに入れ子にならない最上位）。検索対象に選ばれる `.md` がなければ空。"""
    prefix = str(rag_root) + os.sep
    rag_files = {p[len(prefix):] for p in _rg_files([rag_root], ["*.rag.md"], budget)}
    mprefix = str(md_root) + os.sep
    needed = {os.path.dirname(p) for p in _rg_files([md_root], ["*.md", "*.markdown"], budget)
              if _kept(p[len(mprefix):], rag_files)}
    top: list[str] = []
    for d in sorted(needed):
        if not any(d == t or d.startswith(t + os.sep) for t in top):
            top.append(d)
    return top


def plan(world: str, orig_roots, der_roots, sig: str, budget=None, scope=None) -> dict:
    """`{"skip_exts": [拡張子…], "md_root": str|None, "md_dirs": [フォルダ…]|None}`。`md_dirs` が None なら派生 MD の木は削らない。
    `scope`（正規化済みの範囲）があれば、除外する拡張子はその範囲だけで決め、派生 MD の木は削らない。計画は範囲と署名ごとに作る。
    """
    from . import worlds
    key = (world, tuple(str(r) for r in orig_roots), tuple(str(r) for r in der_roots), sig, tuple(scope or ()))
    with _LOCK:
        got = _MEMO.get(key)
    if got is not None:
        return got
    try:
        out = dict(_NO_PRUNE, skip_exts=_skip_exts(orig_roots, budget, scope or None))
        rag, md = worlds.derived_rag_dir(world), worlds.derived_md_dir(world)
        if not scope and rag.is_dir() and str(md) in key[2]:
            out["md_root"], out["md_dirs"] = str(md), _md_dirs(md, rag, budget)
    except rg_search.RgUnavailable:
        return dict(_NO_PRUNE)
    with _LOCK:
        if len(_MEMO) >= _MEMO_MAX:
            _MEMO.pop(next(iter(_MEMO)))
        _MEMO[key] = out
    return out


def derived_roots(der_roots: list, pl: dict) -> list:
    """`der_roots` の `md/` を、計画で残したフォルダに置き換えた一覧。"""
    if pl.get("md_dirs") is None or pl.get("md_root") is None:
        return der_roots
    out: list = []
    for r in der_roots:
        if str(r) == pl["md_root"]:
            out.extend(Path(d) for d in pl["md_dirs"])
        else:
            out.append(r)
    return out
