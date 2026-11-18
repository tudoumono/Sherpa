"""ビルドの定義から「実行名 ← 材料のソース」を読み、呼び出しが今つながらない実行名をグラフでつなぐ（AI は使わない）。
読むのはソースの組のフォルダの下の決まった名前・拡張子のファイルと、管理者が「読む場所にする」と印を付けた場所だけ。読み方は 1 つに固定する。
設計: docs/design/scope.md「ビルドの定義と実行名」・docs/05-グラフ語彙.md
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .. import corpus_docs, text_encoding
from . import text_kind
from .analyzers import registry as analyzer_registry
from .periods.variance import SOURCE_DIR_NAMES

MARK_READ, MARK_SKIP = "read", "skip"
MARKS = (MARK_READ, MARK_SKIP)

BUILD_NAMES = frozenset({"makefile", "gnumakefile", "cmakelists.txt", "pom.xml", "build.gradle", "build.gradle.kts",
                         "build.xml", "pyproject.toml", "setup.cfg"})
BUILD_EXTS = frozenset({".mk", ".cmake", ".csproj", ".vbproj", ".vcxproj"})
CONFIG_EXTS = frozenset({".ini", ".cfg", ".conf", ".json", ".toml", ".properties", ".yaml", ".yml", ".xml"})

# 実行物を呼ぶ書き方を読む解析器と、その参照の via（それ以外の INVOKES は型の依存や関数呼び出しなので対象にしない）
EXEC_CALL_VIAS = {"jcl": frozenset({None}), "shell": frozenset({"call"}), "cobol": frozenset({"call", "cics_xctl", "cics_link"})}
UNLINKED_REASONS = frozenset({"unresolved", "cross_scope", "ambiguous"})

READ_MAX_BYTES = 2 * 1024 * 1024
REPORT_LIST_MAX = 30
EVIDENCE_MAX = 200
SCOPE_LABEL = {"line": "行", "block": "まとまり", "file": "ファイル全体"}
_SCOPE_RANK = {"line": 0, "block": 1, "file": 2}

_EXE_SUFFIXES = (".exe", ".dll", ".so")
_TOKEN_RE = re.compile(r"[^\s\"'`,;:=(){}\[\]<>|&!?*%+$^~]+")
_LINE_COMMENT_RE = re.compile(r"\s*(?:#|//|\*|;|--|::|!|'|[Rr][Ee][Mm](?:\s|$))")
_XML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_XML_TAG_RE = re.compile(r"<(/?)([A-Za-z_][\w.:-]*)((?:\"[^\"]*\"|'[^']*'|[^<>\"'])*?)(/?)>")
_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = {")", "]", "}"}


@dataclass
class Call:
    """呼び出しが今つながらない実行名の参照 1 件。`flag`・`item` は取り消すときに使う未解決の申告（同じオブジェクトで照合する）。"""

    rel: str
    src: str
    name: str
    line: int
    via: str | None
    from_def: dict
    flag: dict
    item: dict


@dataclass
class _Input:
    rel: str | None
    token: str
    line: int
    why: str = ""
    evidence: str = ""


@dataclass
class _Pair:
    """ビルドの定義 1 ファイルの中の「実行名 ← 材料」の組。"""

    path: str
    key: str
    scope: str
    line: int
    evidence: str
    inputs: list = field(default_factory=list)

    @property
    def linked(self) -> list:
        return [i for i in self.inputs if i.rel]


@dataclass
class Result:
    nodes: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    linked: list = field(default_factory=list)
    report: dict = field(default_factory=dict)


def is_exec_call(analyzer_name: str, extra: dict | None) -> bool:
    """実行物の呼び出しの参照か。修飾名（Java のクラス指定）とパスの指定は対象にしない。"""
    extra = extra or {}
    vias = EXEC_CALL_VIAS.get(analyzer_name)
    return (vias is not None and extra.get("via") in vias
            and not extra.get("qualified") and not extra.get("include_path"))


def marks_signature(marks: dict | None) -> str:
    """印の値（印が無ければ空）。グラフに持たせた値と比べて、次の更新で作り直すかを決める。"""
    if not marks:
        return ""
    return hashlib.sha256(json.dumps(sorted(marks.items()), ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _fold_dir(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def is_source_dir(name: str) -> bool:
    return _fold_dir(name) in SOURCE_DIR_NAMES


def in_source_group(rel: str) -> bool:
    return any(is_source_dir(p) for p in rel.split("/")[:-1])


def source_group_dirs(rels) -> list:
    """資料のパスの一覧から、ソースの組のフォルダ（名前が一覧に入るフォルダ）を返す。"""
    found: set = set()
    for rel in rels:
        parts = rel.split("/")[:-1]
        for i, part in enumerate(parts):
            if is_source_dir(part):
                found.add("/".join(parts[:i + 1]))
    return sorted(found)


def is_build_definition(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].casefold()
    return name in BUILD_NAMES or PurePosixPath(name).suffix in BUILD_EXTS


def _is_config(rel: str) -> bool:
    return PurePosixPath(rel).suffix.casefold() in CONFIG_EXTS


def is_program_source(rel: str) -> bool:
    """材料にできるファイルか（解析器の有無は問わない）。設定・ビルドの定義・文書は材料にしない。"""
    if is_build_definition(rel) or _is_config(rel):
        return False
    ext = PurePosixPath(rel).suffix.casefold()
    return (ext in text_kind.CODE_EXT and ext not in CONFIG_EXTS) or bool(analyzer_registry.candidates(rel))


def _deepest_mark(rel: str, marks: dict) -> tuple:
    p = rel
    while marks:
        if p in marks:
            return p, marks[p]
        if "/" not in p:
            break
        p = p.rsplit("/", 1)[0]
    return None, None


def should_read(rel: str, marks: dict) -> bool:
    """ビルドの定義として読むか。一番深い場所に付けた印が勝つ。印が無ければ置き方（ソースの組の下の決まった一覧）で決める。"""
    at, mark = _deepest_mark(rel, marks)
    if mark == MARK_SKIP:
        return False
    if mark == MARK_READ:
        return at == rel or is_build_definition(rel) or _is_config(rel)
    return in_source_group(rel) and is_build_definition(rel)


def _exec_key(token: str) -> str:
    low = token.casefold()
    for suffix in _EXE_SUFFIXES:
        if low.endswith(suffix) and len(low) > len(suffix):
            return low[:-len(suffix)]
    return low


def _blank_comments(text: str) -> tuple:
    """コメントの行を空行に置き換えた行の列と、元から空行だった行の番号（0 始まり）を返す。行番号は変えない。"""
    text = _XML_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    lines = text.split("\n")
    blank = {i for i, ln in enumerate(lines) if not ln.strip()}
    in_block = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if in_block:
            lines[i] = ""
            in_block = "*/" not in s
        elif s.startswith("/*"):
            lines[i] = ""
            in_block = "*/" not in s[2:]
        elif _LINE_COMMENT_RE.match(ln):
            lines[i] = ""
    return lines, blank


def _bracket_spans(lines: list) -> list:
    """複数行にまたがる括弧の対の (始め, 終わり) の行。"""
    spans: list = []
    stack: list = []
    for i, ln in enumerate(lines):
        for ch in ln:
            if ch in _OPEN:
                stack.append((_OPEN[ch], i))
            elif ch in _CLOSE and stack and stack[-1][0] == ch:
                _, start = stack.pop()
                if start != i:
                    spans.append((start, i))
    return spans


def _xml_spans(lines: list) -> list:
    """複数行にまたがる XML の要素の (始め, 終わり) の行。"""
    if not "\n".join(lines).lstrip().startswith("<"):
        return []
    spans: list = []
    stack: list = []
    for i, ln in enumerate(lines):
        for m in _XML_TAG_RE.finditer(ln):
            closing, name, _attrs, empty = m.groups()
            if empty:
                continue
            if not closing:
                stack.append((name, i))
                continue
            for k in range(len(stack) - 1, -1, -1):
                if stack[k][0] == name:
                    start = stack[k][1]
                    del stack[k:]
                    if start != i:
                        spans.append((start, i))
                    break
    return spans


def _paragraph(line: int, blank: set, n: int) -> tuple:
    start = line
    while start > 0 and start - 1 not in blank:
        start -= 1
    end = line
    while end < n - 1 and end + 1 not in blank:
        end += 1
    return start, end


def _logical_line(line: int, lines: list) -> tuple:
    """行末の `\\`・`,`・`+` で続く行を 1 行とみなした (始め, 終わり)。"""
    def _continues(s: str) -> bool:
        return s.rstrip().endswith(("\\", ",", "+"))
    start = line
    while start > 0 and _continues(lines[start - 1]):
        start -= 1
    end = line
    while end < len(lines) - 1 and _continues(lines[end]):
        end += 1
    return start, end


class _Sources:
    """材料の名前からソースのファイルを引く。同じトップフォルダの中で、定義のファイルに一番近いものを選ぶ。"""

    def __init__(self, files, rel_name: dict):
        self.exact: set = set()
        self.folded: dict = defaultdict(list)
        self.by_file: dict = defaultdict(list)
        self.by_stem: dict = defaultdict(list)
        self.by_decl: dict = defaultdict(list)
        self.rel_name = rel_name
        self._cache: dict = {}
        for _rp, rel in files:
            if not is_program_source(rel):
                continue
            self.exact.add(rel)
            self.folded[rel.casefold()].append(rel)
            base = rel.rsplit("/", 1)[-1]
            self.by_file[base.casefold()].append(rel)
            self.by_stem[PurePosixPath(base).stem.casefold()].append(rel)
            if rel in rel_name:
                self.by_decl[str(rel_name[rel][1]).casefold()].append(rel)

    @staticmethod
    def _nearest(cands: list, def_rel: str) -> tuple:
        from .world_graph import _top, _tree_distance
        same = sorted({r for r in cands if _top(r) == _top(def_rel)})
        if not same:
            return None, "no_source"
        best = min(_tree_distance(def_rel, r) for r in same)
        near = [r for r in same if _tree_distance(def_rel, r) == best]
        return (near[0], "") if len(near) == 1 else (None, "ambiguous")

    def resolve(self, token: str, def_rel: str) -> tuple:
        """`(rel|None, why)`。材料の名前でないトークンは `(None, "")`。"""
        key = (token, posixpath.dirname(def_rel))
        if key in self._cache:
            return self._cache[key]
        out = self._resolve(token, def_rel)
        self._cache[key] = out
        return out

    def _resolve(self, token: str, def_rel: str) -> tuple:
        from .world_graph import _top
        path = token.replace("\\", "/")
        base = path.rstrip("/").rsplit("/", 1)[-1]
        if not base:
            return None, ""
        if "/" in path:
            cand = posixpath.normpath(posixpath.join(posixpath.dirname(def_rel), path.lstrip("/")))
            if cand in self.exact and _top(cand) == _top(def_rel):
                return cand, ""
            folded = [r for r in self.folded.get(cand.casefold(), []) if _top(r) == _top(def_rel)]
            if len(folded) > 1:
                return None, "ambiguous"
            if folded:
                return folded[0], ""
        low = base.casefold()
        for pool, key in ((self.by_file, low), (self.by_stem, PurePosixPath(low).stem), (self.by_decl, low)):
            if key in pool:
                rel, why = self._nearest(pool[key], def_rel)
                if rel or why == "ambiguous":
                    return rel, why
        return None, ""


def _pairs_in_file(rel: str, text: str, called: set, sources: _Sources) -> tuple:
    """1 ファイルから組を取り出す。`(組の一覧, 組にならなかった実行名の一覧)` を返す。"""
    lines, blank = _blank_comments(text)
    tokens = [_TOKEN_RE.findall(ln) for ln in lines]
    called_at: dict = defaultdict(set)
    for i, toks in enumerate(tokens):
        for t in toks:
            k = _exec_key(t)
            if k in called:
                called_at[i].add(k)
    if not called_at:
        return [], []
    enclosing = _bracket_spans(lines) + _xml_spans(lines)
    n = len(lines)

    def _spans(line: int) -> list:
        ln_span = _logical_line(line, lines)
        wider = {s for s in enclosing if s[0] <= line <= s[1]} | {_paragraph(line, blank, n)}
        ordered = [("line", ln_span)] + [("block", s) for s in sorted(wider, key=lambda s: s[1] - s[0]) if s != ln_span]
        ordered.append(("file", (0, n - 1)))
        return ordered

    span_cache: dict = {}

    def _in_span(span: tuple) -> tuple:
        if span not in span_cache:
            names: set = set()
            mats: list = []
            for i in range(span[0], span[1] + 1):
                names |= called_at.get(i, set())
                for t in tokens[i]:
                    r, why = sources.resolve(t, rel)
                    if r or why:
                        mats.append(_Input(r, t, i + 1, why, lines[i].strip()[:EVIDENCE_MAX]))
            span_cache[span] = (names, mats)
        return span_cache[span]

    best: dict = {}    # 実行名 -> (範囲の順位, [(範囲の名前, 出た行, 材料)])
    failed: set = set()
    for line in sorted(called_at):
        for key in sorted(called_at[line]):
            found = False
            for scope, span in _spans(line):
                names, mats = _in_span(span)
                if names - {key}:
                    break
                mats = [m for m in mats if _exec_key(m.token) != key]
                if not mats:
                    continue
                rank = _SCOPE_RANK[scope]
                cur = best.get(key)
                if cur is None or rank < cur[0]:
                    best[key] = (rank, [(scope, line, mats)])
                elif rank == cur[0]:
                    cur[1].append((scope, line, mats))
                found = True
                break
            if not found:
                failed.add(key)

    pairs: list = []
    for key, (_rank, hits) in best.items():
        failed.discard(key)
        scope, line, _ = hits[0]
        seen: dict = {}
        for _s, _l, mats in hits:
            for m in mats:
                seen.setdefault((m.rel, m.token), m)
        pairs.append(_Pair(rel, key, scope, line + 1, lines[line].strip()[:EVIDENCE_MAX],
                           sorted(seen.values(), key=lambda m: (m.line, m.token))))
    return pairs, sorted(failed)


def _capped(items: list) -> list:
    return items[:REPORT_LIST_MAX]


def link_builds(world_id: str, files, calls: list, rel_name: dict, marks: dict | None) -> Result:
    """① ビルドの定義を読む ② 組を作る ③ 呼び出しごとに一番近い定義を選び、実行名のノードと線を作る ④ 点検の報告を作る。"""
    from .world_graph import _cid, _node, _top, _tree_distance
    marks = marks or {}
    res = Result()
    targets = [(rp, rel) for rp, rel in files if should_read(rel, marks)]
    if not targets and not marks:
        return res
    spellings: dict = defaultdict(set)
    for c in calls:
        spellings[_exec_key(c.name)].add(c.name)
    called = set(spellings)
    sources = _Sources(files, rel_name)
    unreadable: list = []
    pairs_by_key: dict = defaultdict(list)
    read = 0
    for rp, rel in targets:
        try:
            if rp.stat().st_size > READ_MAX_BYTES:
                unreadable.append({"path": rel, "why": "size_exceeded"})
                continue
            text, raw = corpus_docs.read_full_text_and_raw(rp)
        except OSError:
            unreadable.append({"path": rel, "why": "unreadable"})
            continue
        _enc, ratio, garbled = text_encoding.detect_bytes_quality(raw, complete=True)
        if text_encoding.quality_of(ratio, garbled) == "undetermined":
            unreadable.append({"path": rel, "why": "encoding_undetermined"})
            continue
        read += 1
        pairs, failed = _pairs_in_file(rel, text, called, sources)
        for p in pairs:
            pairs_by_key[p.key].append(p)
        unreadable.extend({"path": rel, "why": "no_pair", "name": min(spellings[k])} for k in failed)

    conflicts: list = []
    no_definition: set = set()
    used: dict = {}
    input_unresolved: dict = {}
    for c in calls:
        key = _exec_key(c.name)
        cands = [p for p in pairs_by_key.get(key, ()) if p.linked and _top(p.path) == _top(c.rel)]
        for p in pairs_by_key.get(key, ()):
            for i in p.inputs:
                if not i.rel or (i.rel not in rel_name):
                    input_unresolved[(p.path, i.token)] = {
                        "path": p.path, "name": c.name, "token": i.token, "why": i.why or "no_analyzer"}
        cands = [p for p in cands if any(i.rel in rel_name for i in p.linked)]
        if not cands:
            if not pairs_by_key.get(key):
                no_definition.add(c.name)
            continue
        best = min(_tree_distance(c.rel, p.path) for p in cands)
        near = sorted((p for p in cands if _tree_distance(c.rel, p.path) == best), key=lambda p: p.path)
        if len({frozenset(i.rel for i in p.linked if i.rel in rel_name) for p in near}) > 1:
            conflicts.append({"name": c.name, "from": c.rel, "line": c.line, "candidates": [p.path for p in near][:5]})
            continue
        pair = near[0]
        used.setdefault((pair.path, key), pair)
        cid = _cid("Executable", world_id, pair.path, f"build:{key}")
        res.edges.append({"type": "INVOKES", "src": c.src, "dst": cid, "doc": c.rel, "line": c.line, "status": "active",
                          **({"via": c.via} if c.via else {}), "_rule": "build_definition", "_from_def": c.from_def})
        res.linked.append(c)

    for (path, key), pair in sorted(used.items()):
        cid = _cid("Executable", world_id, path, f"build:{key}")
        node = {**_node("Executable", world_id, path, min(spellings[key]), value=SCOPE_LABEL[pair.scope]),
                "cid": cid, "analyzer": "build", "line": pair.line,
                "sources": [{"doc_id": path, "file": path, "line": pair.line, "evidence_text": pair.evidence}]}
        res.nodes[cid] = node
        for i in pair.linked:
            if i.rel not in rel_name:
                continue
            label, name = rel_name[i.rel]
            res.edges.append({"type": "BUILT_FROM", "src": cid, "dst": _cid(label, world_id, i.rel, name), "doc": path,
                              "line": i.line, "status": "active", "_rule": "build_definition",
                              "_from_def": {"file": path, "key": None}, "_evidence_text": i.evidence})

    conflicts.sort(key=lambda x: (x["name"], x["from"], x["line"]))
    unresolved_inputs = [input_unresolved[k] for k in sorted(input_unresolved)]
    unreadable.sort(key=lambda x: (x["path"], x.get("name", "")))
    scopes = {k: sum(1 for p in used.values() if p.scope == k) for k in _SCOPE_RANK}
    res.report = {
        "marks_sig": marks_signature(marks), "read": read, "linked_names": len(used), "linked_calls": len(res.linked),
        "scopes": scopes,
        "unreadable": _capped(unreadable), "unreadable_count": len(unreadable),
        "input_unresolved": _capped(unresolved_inputs), "input_unresolved_count": len(unresolved_inputs),
        "conflicts": _capped(conflicts), "conflict_count": len(conflicts),
        "no_definition": _capped(sorted(no_definition)), "no_definition_count": len(no_definition),
    }
    return res
