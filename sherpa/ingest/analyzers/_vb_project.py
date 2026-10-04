"""VB.NET のプロジェクト設定（`.vbproj`）の読み取りと、ソース `.vb` への当てはめ。アナライザではない（登録簿に載らない）。

読み取り: 標準の `xml.etree`（外部実体・DTD を読まない: `DOCTYPE`／`ENTITY` を含む入力は拒否）。本文は BOM を見て UTF-8／UTF-16 を判定してから XML にする。
取り出すのは `RootNamespace`・プロジェクト全体の `<Import Include>`・ソースの含み方（SDK 形式＝フォルダの下を全部〔既定の除外 `bin/**`・`obj/**` と `Compile Remove` を除く〕／
旧形式・`EnableDefaultCompileItems=false`＝`<Compile Include>` の列挙）。`Include`／`Remove`／`Import` の値は `;` で分ける。`Compile Include` はプロジェクトの外を指してよい
（資料フォルダ root 相対へ正規化し、同じ最上位フォルダの外へ出るものは拒否して申告する）。
`RootNamespace` は MSBuild の取り込み順＝`.vbproj` → 上のフォルダの最も近い `Directory.Build.props` → 既定（`$(MSBuildProjectName)`＝`.vbproj` のファイル名から拡張子を除いたもの）。
MSBuild の `<Import Project>`（別ファイルの取り込み）は VB の名前の import（`<Import Include>`）とは別物で、取り込む先は評価せず申告する。空の `RootNamespace` は Root 無し。
`$(…)`・`@(…)`・`%(…)` を含む値と `Condition` の付いた要素（親の `Condition` を含む）は評価せず、当てはめずに申告する（`vbproj_unevaluated`）。
プロジェクトに属する `.vb` の型・手続きの本当の完全修飾名は `<RootNamespace>.<namespace>.<型>`＝共通層はこの名前だけを完全修飾名の索引へ載せ、同じプロジェクトのソースの参照には
`apply_project` が namespace の連鎖（Root を含む）とワイルドカード import の Root つきの形を足す。
`ProjectIndex.project_for` は、ソースのフォルダから最上位フォルダまで上へたどって最も近い `.vbproj` を当てる（資料フォルダ直下のソースは直下の `.vbproj`）。同じ最上位フォルダの中だけ。
そのソースを含む候補が複数なら当てず、候補を返す（呼び出し側が申告する）。読めない `.vbproj` はその場所の「止め」として残し、外側へは進まない。
名前は `identifiers.normalize_code_name`（VB は大文字小文字を区別しない）で正規化する。
設計: docs/proposals/2026-10-04-アナライザとグラフの改善.md「VB.NET のプロジェクト設定（V1）」
"""
from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

from ..identifiers import normalize_code_name as _norm
from ._base import FileContext, ImportItem

VBPROJ_EXT = ".vbproj"
PROPS_NAME = "directory.build.props"
_DEFAULT_REMOVES = ("bin/**", "obj/**")   # SDK 形式の既定の除外（`$(BaseOutputPath)`・`$(BaseIntermediateOutputPath)`）


def _top(rel: str):
    return rel.split("/", 1)[0] if "/" in rel else None


@dataclass(frozen=True)
class VbProject:
    """1 つの `.vbproj`。`rel`＝資料フォルダ root 相対のパス、`dir`＝その親フォルダ（POSIX・直下は `""`）、`top`＝最上位フォルダ（直下は `None`）。

    `root_namespace`／`imports` は正規化済み。`root_declared`＝`RootNamespace` の要素がある（評価できなくても真）。`includes`／`removes` は資料フォルダ root 相対・小文字のパターン。
    `enumerated`＝ソースを `includes` の列挙だけで決める（旧形式、または SDK で `EnableDefaultCompileItems=false`）。
    `unevaluated`／`outside`／`dropped_imports`＝取り込まなかった値（申告する）。`broken`＝読めなかった（当てはめの「止め」だけに使う）。
    """

    rel: str
    dir: str
    top: str | None
    root_namespace: str | None = None
    root_declared: bool = False
    imports: tuple = ()
    includes: tuple = ()
    removes: tuple = ()
    enumerated: bool = True
    dropped_imports: tuple = ()
    unevaluated: tuple = ()
    outside: tuple = ()
    broken: bool = False
    membership_unknown: bool = False   # ソースを含むかの判定に評価できない値（`EnableDefaultCompileItems`・`Compile`）が関わる＝当てはめず、その場所で止める

    def includes_source(self, src_rel: str) -> bool:
        """`src_rel` をこのプロジェクトが含むか（同じ最上位フォルダのソースだけ）。"""
        if self.broken or self.membership_unknown or _top(src_rel) != self.top:
            return False
        p = src_rel.lower()
        if any(_glob_match(r, p) for r in self.removes):
            return False
        if any(_glob_match(i, p) for i in self.includes):
            return True
        if self.enumerated:
            return False
        base = self.dir.lower() + "/" if self.dir else ""
        return p.startswith(base) and not any(_glob_match(base + d, p) for d in _DEFAULT_REMOVES)


@dataclass(frozen=True)
class PropsRoot:
    """`Directory.Build.props` の `RootNamespace`（`declared`＝要素がある。評価できなかった・読めなかったときは `value=None`）。"""

    declared: bool
    value: str | None = None
    unevaluated: tuple = ()


def decode_xml(raw: bytes) -> str:
    """`.vbproj`・`.props` の生バイト列を BOM（UTF-8／UTF-16 LE・BE）で判定して文字列にする。BOM が無ければ UTF-8。不正なバイト列は `UnicodeDecodeError`（`ValueError`）＝読めない設定。"""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def _norm_path(p: str) -> str:
    return p.strip().replace("\\", "/").lower()


def _glob_match(pattern: str, path: str) -> bool:
    """MSBuild 風の glob（`*`＝区切り以外・`**`＝区切りを含む任意・`?`）。"""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(out), path) is not None


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _dynamic(value: str) -> bool:
    return "$(" in value or "@(" in value or "%(" in value


def _parse_root(text: str):
    body = text.lstrip("﻿")
    if "<!DOCTYPE" in body.upper() or "<!ENTITY" in body.upper():
        raise ValueError("vbproj_dtd")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise ValueError(f"vbproj_xml: {e}") from e
    if _local(root.tag) != "Project":
        raise ValueError("vbproj_not_project")
    return root


def _walk(el, conditional: bool):
    """`(要素, 名前, Condition の下か)`。親のどれかに `Condition` があれば真。"""
    stack = [(iter(el), conditional)]               # 再帰しない（入れ子の深い XML で `RecursionError` にしない）
    while stack:
        it, parent_cond = stack[-1]
        c = next(it, None)
        if c is None:
            stack.pop()
            continue
        cond = parent_cond or c.get("Condition") is not None
        yield c, _local(c.tag), cond
        stack.append((iter(c), cond))


def _root_namespace(root) -> tuple:
    """`(RootNamespace の要素があるか, 評価できた最後の値|None, 評価しなかった値の一覧)`。"""
    declared, value, skipped = False, None, []
    for el, name, cond in _walk(root, False):
        if name != "RootNamespace":
            continue
        declared = True
        v = (el.text or "").strip()
        if cond or _dynamic(v):
            skipped.append(f"RootNamespace: {v}"[:80])
        elif v:
            value = _norm(v)
    if skipped:                                       # 定義の 1 つでも評価できなければ、どの値が効くか決まらない＝Root は当てない
        value = None
    return declared, value, tuple(skipped)


def parse_props(rel: str, text: str) -> PropsRoot:
    """`Directory.Build.props` の `RootNamespace`。読めなければ `ValueError`。"""
    declared, value, skipped = _root_namespace(_parse_root(text))
    return PropsRoot(declared=declared, value=value, unevaluated=skipped)


def broken_project(rel: str) -> VbProject:
    """読めなかった `.vbproj`（当てはめの「止め」としてだけ残す）。"""
    d = str(PurePosixPath(rel).parent) if "/" in rel else ""
    return VbProject(rel=rel, dir=d, top=_top(rel), broken=True)


def _world_rel(project_dir: str, top, pattern: str):
    """`dir` 相対のパターンを資料フォルダ root 相対へ。同じ最上位フォルダの外へ出れば `None`。"""
    joined = posixpath.normpath(posixpath.join(project_dir, pattern)) if project_dir else posixpath.normpath(pattern)
    joined = joined.lower()
    if joined.startswith("..") or joined.startswith("/") or _top(joined) != (top.lower() if top else None):
        return None
    return joined


def parse_vbproj(rel: str, text: str) -> VbProject:
    """`.vbproj` の本文を読む。読めない（XML の誤り・DTD あり・ルートが `Project` でない）ときは `ValueError`（理由を持つ）。"""
    root = _parse_root(text)
    project_dir = str(PurePosixPath(rel).parent) if "/" in rel else ""
    top = _top(rel)
    sdk = root.get("Sdk") is not None or any(_local(c.tag) == "Sdk" for c in root)
    default_compile = True
    imports: list = []
    dropped_imports: list = []
    includes: list = []
    removes: list = []
    unevaluated: list = []
    outside: list = []
    membership_unknown = False

    def values(el, attr):
        raw = el.get(attr)
        return [] if raw is None else [v for v in (x.strip() for x in raw.split(";")) if v]

    for el, name, cond in _walk(root, False):
        if name == "RootNamespace":
            continue
        if name == "EnableDefaultCompileItems":
            v = (el.text or "").strip()
            if cond or _dynamic(v):
                unevaluated.append(f"EnableDefaultCompileItems: {v}"[:80])
                membership_unknown = True
            else:
                default_compile = v.lower() != "false"
        elif name == "Import" and el.get("Include") is not None:
            for v in values(el, "Include"):
                if cond or _dynamic(v):
                    unevaluated.append(f"Import: {v}"[:80])
                elif "=" in v:                             # 別名つきの Import はプロジェクト設定にできない形
                    dropped_imports.append(v)
                elif _norm(v) and _norm(v) not in imports:
                    imports.append(_norm(v))
        elif name == "Import" and el.get("Project") is not None:
            # MSBuild の取り込み（別ファイル）は VB の名前の import（`Include`）とは別物。中の RootNamespace・Import は評価しない＝申告する
            # （ツールセットの標準の `Microsoft.*` は RootNamespace・Import を定めないので対象外）
            v = el.get("Project").strip()
            if not posixpath.basename(v.replace("\\", "/")).lower().startswith("microsoft."):
                unevaluated.append(f"Import Project: {v}"[:80])
        elif name == "Compile":
            for attr, target in (("Include", includes), ("Remove", removes)):
                for v in values(el, attr):
                    if cond or _dynamic(v):
                        unevaluated.append(f"Compile {attr}: {v}"[:80])
                        membership_unknown = True
                        continue
                    w = _world_rel(project_dir, top, _norm_path(v))
                    if w is None:
                        outside.append(v[:80])
                    else:
                        target.append(w)
    declared, value, skipped = _root_namespace(root)
    return VbProject(rel=rel, dir=project_dir, top=top, root_namespace=value, root_declared=declared,
                     imports=tuple(imports), includes=tuple(includes), removes=tuple(removes),
                     enumerated=(not sdk) or (not default_compile), dropped_imports=tuple(dropped_imports),
                     unevaluated=tuple(unevaluated) + skipped, outside=tuple(outside),
                     membership_unknown=membership_unknown)


def resolve_root(project: VbProject, props_by_dir: dict) -> VbProject:
    """`RootNamespace` が `.vbproj` に無ければ、上のフォルダの最も近い `Directory.Build.props`、それも無ければプロジェクト名にする。

    `props_by_dir`＝`{フォルダ: PropsRoot}`。最も近い `.props` が `RootNamespace` を持たなければ既定へ進む。評価できない値は `None`（Root Namespace を当てない）。
    """
    if project.broken or project.root_declared:
        return project
    parts = project.dir.split("/") if project.dir else []
    dirs = ["/".join(parts[:n]) for n in range(len(parts), -1, -1)]   # 資料フォルダの root（""）まで
    for d in dirs:
        props = props_by_dir.get(d)
        if props is None:
            continue
        if props.declared:
            return replace(project, root_namespace=props.value, root_declared=True)
        break
    return replace(project, root_namespace=_norm(PurePosixPath(project.rel).stem) or None, root_declared=True)


def apply_project(fc: FileContext, project: VbProject) -> FileContext:
    """プロジェクトに属する `.vb` の `file_context` に、プロジェクト全体の Import と Root Namespace を反映する。

    package・namespace の名前は Root を頭に足した本当の名前にし（namespace の連鎖が Root を含む）、ワイルドカード import には Root つきの形も足す。
    """
    root = project.root_namespace

    def rn(n):
        return root if not n else f"{root}.{n}"

    imports = list(fc.imports)
    seen = {(i.kind, i.name, i.alias, i.static) for i in imports}
    for n in project.imports:
        if ("wildcard", n, None, False) not in seen:
            imports.append(ImportItem(kind="wildcard", name=n))
            seen.add(("wildcard", n, None, False))
    if root:
        for i in list(imports):
            if i.kind == "wildcard" and not i.static and ("wildcard", rn(i.name), None, False) not in seen:
                imports.append(replace(i, name=rn(i.name)))
                seen.add(("wildcard", rn(i.name), None, False))
        return FileContext(package=rn(fc.package), imports=imports,
                           namespaces=[(s, e, rn(n)) for s, e, n in fc.namespaces])
    return FileContext(package=fc.package, imports=imports, namespaces=fc.namespaces)


class ProjectIndex:
    """`.vbproj` の一覧からソースへ当てるプロジェクトを引く。"""

    def __init__(self, projects) -> None:
        self._by_dir: dict = {}
        self._outside: list = []          # 自分のフォルダの外のソースを `Compile Include` で名指ししているプロジェクト
        for p in projects:
            self._by_dir.setdefault(p.dir, []).append(p)
            if not p.broken and not p.membership_unknown and p.includes:
                self._outside.append(p)

    def __bool__(self) -> bool:
        return bool(self._by_dir)

    def project_for(self, src_rel: str) -> tuple:
        """`(プロジェクト | None, 曖昧な候補の rel のリスト)`。

        ソースのフォルダから最上位フォルダまで上へたどり（資料フォルダ直下のソースは直下だけ）、`.vbproj` がある最初のフォルダで、そのソースを含む候補を数える。
        そこの `.vbproj` に読めないもの・ソースを含むかの判定に評価できない値が関わるものがあれば「止め」＝当てない。どれも含まなければ当てない（外側へは進まない）。
        上へたどっても `.vbproj` が 1 つも無いときだけ、フォルダの外から `Compile Include` で名指ししているプロジェクトを候補にする。候補が 1 つなら当て、複数なら当てずに候補を返す。
        """
        parts = src_rel.split("/")[:-1]
        dirs = ["/".join(parts[:n]) for n in range(len(parts), 0, -1)] if parts else [""]
        hit: list = []
        found = False
        for d in dirs:
            here = self._by_dir.get(d)
            if not here:
                continue
            if any(p.broken or p.membership_unknown for p in here):
                return None, []
            hit = [p for p in here if p.includes_source(src_rel)]
            found = True
            break
        if not found:                                     # 外から名指す Include は、上へたどっても `.vbproj` が 1 つも無いときだけ候補にする
            hit = [p for p in self._outside if p.includes_source(src_rel)
                   and not (src_rel.startswith(p.dir + "/") if p.dir else "/" not in src_rel)]
        if len(hit) == 1:
            return hit[0], []
        return None, sorted(p.rel for p in hit)
