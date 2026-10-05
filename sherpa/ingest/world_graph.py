"""鏡モデルのグラフ構築（資料フォルダの走査からノード・エッジの生成まで）。

資料フォルダ（登録ディレクトリ）を走査し、パス同一性のノードと、同一 top_scope 内の最近傍で解決した
構造エッジ（COPIES/INVOKES/CONTAINS/ACCESSES）を作る。各ノードは検索スコープのメタデータ
（`world_id / top_scope / phase / category / path`）を持つ。
① Pass1: 言語アナライザ（`sherpa.ingest.analyzers`）で定義を集める。
② Pass2: 参照を同 top_scope 内の最近傍で解決して構造エッジを張る。
③ Pass3: 定義索引を辞書として資料文書の本文と決定的に突合し、`Document -DOCUMENTS(via="mention")-> コード` を張る
   （世代をまたいでよい例外・影響 traversal の対象外）。
LLM は使わない。言語ごとの抽出はアナライザ側、本モジュールは名前解決・cid 組み立てなど言語非依存の共通層。
設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

import bisect
import hashlib
import os
import posixpath
import re
import stat as stat_mod
from collections import defaultdict
from pathlib import Path, PurePosixPath

from .. import corpus_docs, doc_text, scope_infer, text_encoding, worlds
from . import importance, resolve_settings, text_kind
from .analyzers import registry as analyzer_registry
from .analyzers import _vb_project as vb_project
from .analyzers._base import FileContext, RefResult, TypeCandidate, TypeLookup, TypeRelations


def _scope_meta(rel: str) -> dict:
    """rel_path（POSIX）→ 検索スコープのメタ（top_scope/phase/category）。導出は scope_infer に集約。"""
    return scope_infer.rel_scope_meta(rel)


def _tree_distance(a: str, b: str) -> int:
    """2つの rel_path の木距離（共通祖先までの上り＋下り）。同フォルダ=0。"""
    da, db = a.split("/")[:-1], b.split("/")[:-1]
    c = 0
    for x, y in zip(da, db):
        if x != y:
            break
        c += 1
    return (len(da) - c) + (len(db) - c)


def _cid(label: str, world: str, rel: str, name: str) -> str:
    """ファイル由来＝パス同一性の canonical_id（複製は別ノード）。"""
    return f"{label.lower()}:{world}:{rel}#{name}"


def _node(label, world, rel, name, value=None):
    return {"cid": _cid(label, world, rel, name), "label": label, "name": name,
            "world_id": world, "path": rel, "value": value,
            "status": "active", **_scope_meta(rel)}


def _top(rel: str):
    return rel.split("/", 1)[0] if "/" in rel else None


def _resolve_nearest(defs, kind, name, ref_rel):
    """`defs[(kind,name)]` の候補 rel から ref_rel に最も近いものを返す。

    同一 top_scope（世代）に限定してから最近傍を選ぶ。戻りは `(rel | None, status)`。
    `status` は `''`(解決) / `'ambiguous'`(同距離複数) / `'cross_scope'`(同世代に定義無し)。
    """
    cands = defs.get((kind, name))
    if not cands:
        return None, "unresolved"
    same = [r for r in cands if _top(r) == _top(ref_rel)]        # 構造リンクは同 top_scope 内のみ
    if not same:
        return None, "cross_scope"                              # 別世代にしか無い＝引かない（誤検出防止）
    ranked = sorted(same, key=lambda r: _tree_distance(ref_rel, r))
    best = _tree_distance(ref_rel, ranked[0])
    if sum(1 for r in same if _tree_distance(ref_rel, r) == best) > 1:
        return None, "ambiguous"                                # 同距離複数は任意選択しない
    return ranked[0], ""


def _resolve_table(table_defs, name, ref_rel):
    """`Table` 参照の解決。`name` は `NAME` か、schema を書いた `SCHEMA.NAME`。

    `name` は `_sql_scan.TableName`（`schema`・`simple` を持つ）か単純名の文字列。
    `table_defs[NAME]`（`[(rel, cid_key, schema|None), ...]`）を同一 top_scope に絞る。schema を書いた参照 `S.NAME` は、
    schema=S の定義があればそれだけ、無ければ schema の記載が無い定義だけを候補にして修飾なしと同じ規則（最近傍・同距離の複数は ambiguous）。
    schema を明示した別の schema の定義へは張らない（候補が無ければ未解決）。書いていない参照は、同名が複数 schema にあれば
    `'ambiguous'`（任意選択しない）、1 つなら最近傍。戻りは `(rel|None, cid_key|None, status)`。`status` は `''` / `'ambiguous'` / `'cross_scope'` / `'unresolved'`。
    """
    if not getattr(name, "supported", True):    # 4 部以上の名前は表として解決しない
        return None, None, "unresolved"
    # 引用符内の `.` と区別するため、文字列を再分解せず scanner が持たせた schema・名前を読む。
    schema, simple = getattr(name, "schema", None), getattr(name, "simple", name)
    cands = table_defs.get(simple)
    if not cands:
        return None, None, "unresolved"
    same = [c for c in cands if _top(c[0]) == _top(ref_rel)]
    if not same:
        return None, None, "cross_scope"
    if schema is not None:
        exact = [c for c in same if c[2] == schema]
        same = exact or [c for c in same if c[2] is None]
        if not same:
            return None, None, "unresolved"
    elif len({c[2] for c in same}) > 1:
        return None, None, "ambiguous"
    ranked = sorted(same, key=lambda c: _tree_distance(ref_rel, c[0]))
    best = _tree_distance(ref_rel, ranked[0][0])
    if sum(1 for c in same if _tree_distance(ref_rel, c[0]) == best) > 1:
        return None, None, "ambiguous"
    return ranked[0][0], ranked[0][1], ""


class _QualifiedIndex(dict):
    """完全修飾名の索引。`project_of`（ソース rel → 属する VB.NET プロジェクト）を持ち、同じ完全修飾名の候補が複数あるとき参照元と同じプロジェクトの候補を先にする。"""

    project_of: dict = {}


def _resolve_qualified(qualified_defs, kind, name, ref_rel):
    """完全修飾名の解決: `qualified_defs[(kind,name)]`（`[(rel,実名), ...]`）を同一 top_scope に絞り、パス距離で最近傍を選ぶ。

    同じ完全修飾名の候補が複数あるときだけ、パス距離で並べ替える（別の名前を選ぶ根拠にはしない）。最短距離が複数なら任意選択せず `'ambiguous'`。
    戻りは `(rel|None, 実名|None, status)`。`status` は `''` / `'ambiguous'` / `'cross_scope'` / `'unresolved'`。
    見つからなくても単純名へは倒さない（呼び出し側が未解決として `flags` に申告する）。
    """
    cands = qualified_defs.get((kind, name))
    if not cands:
        return None, None, "unresolved"
    same = [(r, nm) for r, nm in cands if _top(r) == _top(ref_rel)]
    if not same:
        return None, None, "cross_scope"
    owner = getattr(qualified_defs, "project_of", None)
    mine = owner.get(ref_rel) if owner else None
    if mine is not None and len(same) > 1:           # 同じ完全修飾名が複数のプロジェクトにあれば、参照元と同じプロジェクトの定義を先にする
        own = [pair for pair in same if owner.get(pair[0]) == mine]
        same = own or same
    ranked = sorted(same, key=lambda pair: _tree_distance(ref_rel, pair[0]))
    best = _tree_distance(ref_rel, ranked[0][0])
    if sum(1 for r, _ in same if _tree_distance(ref_rel, r) == best) > 1:
        return None, None, "ambiguous"
    rel, actual_name = ranked[0]
    return rel, actual_name, ""


def _namespace_chain(package, parents: bool) -> list:
    """参照元の名前空間から、型名を探す順の修飾子の列を返す（内側→外側・最後は名前空間なし＝グローバル）。

    `parents` が偽（Java）は宣言された package だけ。真（C#・VB.NET）は親の名前空間もさかのぼる。
    """
    if not package:
        return [None]
    if not parents:
        return [package]
    parts = package.split(".")
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)] + [None]


def _join_fqn(prefix, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _resolve_wildcards(qualified_defs, kind, name, ref_rel, file_context):
    """ワイルドカード import（`import p.*`・C# の `using N;`・VB の `Imports N`）の中で `name` が一意に決まるかを返す。

    `static` の import は型名を持ち込まないので使わない。同じ型が複数の import 先で一致したら任意選択せず `'ambiguous'`。
    戻りは `(rel|None, 実名|None, status)`（`status` は `''` / `'ambiguous'` / `'unresolved'`）。
    """
    hits: set = set()
    for imp in file_context.imports:
        if imp.kind != "wildcard" or imp.static:
            continue
        rel, actual, status = _resolve_qualified(qualified_defs, kind, _join_fqn(imp.name, name), ref_rel)
        if status == "ambiguous":
            return None, None, "ambiguous"
        if not status:
            hits.add((rel, actual))
    if len(hits) == 1:
        rel, actual = next(iter(hits))
        return rel, actual, ""
    return (None, None, "ambiguous") if hits else (None, None, "unresolved")


def _resolve_type_name(qualified_defs, defs, kind, name, ref_rel, file_context, parents: bool, package,
                       rule_out: list | None = None):
    """修飾なしの型名 `name` を、参照元ファイルの `file_context`（package／namespace・import／using）の規則で解決する。

    順序: ① 別名（`using A = …`・`Imports A = …`）→ ② 単一型 import（Java）→ ③ 同じ package／enclosing namespace（C#・VB は親→グローバルの順）
    → ④ ワイルドカード import。どれにも一致しなければ、近くに同名の別 package・別 namespace の定義があっても張らない。
    `parents`＝名前空間が親へさかのぼる言語。戻りは `(rel|None, 実名|None, status, 申告の名前)`。
    解決できたとき、決めた規則の名前（`alias`／`single_import`／`same_package`／`wildcard`）を `rule_out`（省略可）へ 1 件足す。
    `status` は `''` / `'ambiguous'` / `'cross_scope'` / `'unresolved'` / `'unresolved_qualifier'`（明示した修飾に一致する定義が無い＝申告の名前は修飾名）。
    """
    for imp in file_context.imports:
        if imp.static or imp.kind == "wildcard":
            continue
        hit = (imp.kind == "alias" and imp.alias == name) or (
            imp.kind == "single" and imp.name.rsplit(".", 1)[-1] == name)
        if not hit:
            continue
        rel, actual, status = _resolve_qualified(qualified_defs, kind, imp.name, ref_rel)
        if status == "unresolved":
            status = "unresolved_qualifier"
        if rule_out is not None:
            rule_out.append("alias" if imp.kind == "alias" else "single_import")
        return rel, actual, status, imp.name
    for prefix in _namespace_chain(package, parents):
        rel, actual, status = _resolve_qualified(qualified_defs, kind, _join_fqn(prefix, name), ref_rel)
        if status in ("", "ambiguous"):
            if rule_out is not None:
                rule_out.append("same_package")
            return rel, actual, status, name
    rel, actual, status = _resolve_wildcards(qualified_defs, kind, name, ref_rel, file_context)
    if status in ("", "ambiguous"):
        if rule_out is not None:
            rule_out.append("wildcard")
        return rel, actual, status, name
    others = defs.get((kind, name))
    return None, None, ("cross_scope" if others and not any(_top(r) == _top(ref_rel) for r in others)
                        else "unresolved"), name


def _resolve_dotted_type_name(qualified_defs, kind, name, ref_rel, file_context, parents: bool, package,
                              absolute: bool = False):
    """C#・VB.NET の修飾付きの型名（`A.B.Type`・`Outer.Inner`）の解決。入れ子の型は外側の型へ寄せる。

    名前の長い接頭辞から順に、参照元の enclosing namespace（内側→グローバル）の下で完全修飾名として探す。戻りは `_resolve_qualified` と同じ。
    """
    segs = name.split(".")
    chain = [None] if absolute else _namespace_chain(package, parents)   # `absolute`＝`global::` の指定（今の namespace の下は探さない）
    if file_context is not None and not absolute:                          # 先頭の要素が別名なら展開し、完全修飾名として解決する（今の namespace の下は探さない）
        for imp in file_context.imports:
            if imp.kind == "alias" and imp.alias == segs[0]:
                segs = imp.name.split(".") + segs[1:]
                chain = [None]
                break
    for n in range(len(segs), 0, -1):
        prefix = ".".join(segs[:n])
        for ns in chain:
            rel, actual, status = _resolve_qualified(qualified_defs, kind, _join_fqn(ns, prefix), ref_rel)
            if status in ("", "ambiguous"):
                return rel, actual, status
    return None, None, "unresolved"


def _qualified_all(qualified_defs, kind, name, ref_rel) -> list:
    """`_resolve_qualified` が曖昧にした候補も含めて返す（同一 top_scope で最短距離の `(rel, 実名, 完全修飾名)` 全部・昇順）。"""
    same = [(r, nm) for r, nm in qualified_defs.get((kind, name), []) if _top(r) == _top(ref_rel)]
    if not same:
        return []
    best = min(_tree_distance(ref_rel, r) for r, _ in same)
    return sorted({(r, nm, name) for r, nm in same if _tree_distance(ref_rel, r) == best})


def _dotted_all(qualified_defs, kind, name, ref_rel, file_context, parents: bool) -> list:
    """`_resolve_dotted_type_name` と同じ探し方で、曖昧にした候補も含めて返す。"""
    segs = name.split(".")
    chain = _namespace_chain(file_context.package, parents)
    for imp in file_context.imports:
        if imp.kind == "alias" and imp.alias == segs[0]:
            segs = imp.name.split(".") + segs[1:]
            chain = [None]
            break
    for n in range(len(segs), 0, -1):
        prefix = ".".join(segs[:n])
        for ns in chain:
            cands = _qualified_all(qualified_defs, kind, _join_fqn(ns, prefix), ref_rel)
            if cands:
                return cands
    return []


def _type_candidates(qualified_defs, defs, kind, name, ref_rel, file_context, parents: bool, *, qualified: bool = False):
    """型名 `name` の解決先の候補を、`_resolve_type_name` と同じ順序・規則で全部返す（曖昧なら複数のまま）。戻りは `([(rel, 実名, 完全修飾名)], status)`。

    `status` は `''`（1 件に決まった）／`'ambiguous'`／`'cross_scope'`／`'unresolved'`。`file_context` が無いときは同名の最近傍（`_resolve_nearest` と同じ）。
    """
    def done(cands):
        return cands, ("" if len(cands) == 1 else "ambiguous")

    if "." in name and (qualified or file_context is None):   # 修飾のある名前は `_link` の完全修飾の入口と同じ規則（単純名へ倒さない）
        if parents and file_context is not None:              # C#・VB.NET＝入れ子の型・別名を `_resolve_dotted_type_name` と同じに
            cands = _dotted_all(qualified_defs, kind, name, ref_rel, file_context, parents)
        else:
            cands = _qualified_all(qualified_defs, kind, name, ref_rel)
        return (cands, "" if len(cands) == 1 else "ambiguous") if cands else ([], "unresolved")
    if file_context is None:
        same = [r for r in defs.get((kind, name), []) if _top(r) == _top(ref_rel)]
        if not same:
            return [], ("cross_scope" if defs.get((kind, name)) else "unresolved")
        best = min(_tree_distance(ref_rel, r) for r in same)
        return done(sorted({(r, name, name) for r in same if _tree_distance(ref_rel, r) == best}))
    for imp in file_context.imports:
        if imp.static or imp.kind == "wildcard":
            continue
        if (imp.kind == "alias" and imp.alias == name) or (imp.kind == "single" and imp.name.rsplit(".", 1)[-1] == name):
            cands = _qualified_all(qualified_defs, kind, imp.name, ref_rel)
            return done(cands) if cands else ([], "unresolved")
    for prefix in _namespace_chain(file_context.package, parents):
        cands = _qualified_all(qualified_defs, kind, _join_fqn(prefix, name), ref_rel)
        if cands:
            return done(cands)
    hits: set = set()
    for imp in file_context.imports:
        if imp.kind == "wildcard" and not imp.static:
            hits.update(_qualified_all(qualified_defs, kind, _join_fqn(imp.name, name), ref_rel))
    if hits:
        return done(sorted(hits))
    others = defs.get((kind, name))
    return [], ("cross_scope" if others and not any(_top(r) == _top(ref_rel) for r in others) else "unresolved")


class _TypeRelationsImpl(TypeRelations):
    """`TypeRelations` の実装。`records`＝全ファイルの継承・実装の宣言（本体のアナライザの参照のうち `via` が `extends`／`implements`）。

    宣言ごとの継承元の解決は `_type_candidates`（共通層の規則）で行い、引くたびに計算せず最初の問い合わせで索引にする。
    """

    def __init__(self, qualified_defs, defs, records, parents_of):
        self._qd, self._defs, self._records, self._parents_of = qualified_defs, defs, records, parents_of
        self._by_target: dict | None = None

    @staticmethod
    def _candidate(rel, actual, qkey, exact=True) -> TypeCandidate:
        return TypeCandidate(path=rel, name=actual, qualified=qkey or actual, exact=exact)

    def _index(self) -> dict:
        if self._by_target is None:
            idx: dict = {}
            for r in self._records:
                targets, _status = _type_candidates(self._qd, self._defs, r["kind"], r["name"], r["rel"], r["ctx"],
                                                    self._parents_of(r["rel"]), qualified=r["qualified"])
                for t in targets:
                    idx.setdefault((r["kind"], *t), []).append((r, len(targets) == 1))
            self._by_target = idx
        return self._by_target

    def subtypes(self, type_name, from_rel, file_context=None, line=None, *, kind="Module") -> TypeLookup:
        ctx = file_context.at(line) if (file_context is not None and line is not None) else file_context
        targets, status = _type_candidates(self._qd, self._defs, kind, type_name, from_rel, ctx, self._parents_of(from_rel),
                                           qualified="." in type_name)
        subs: dict = {}   # (rel, 実名, 完全修飾名) -> 宣言の解決が曖昧でなかったか
        for t in targets:
            for rec, exact in self._index().get((kind, *t), []):
                key = (rec["rel"], rec["actual"], rec["qkey"] or "")
                subs[key] = exact and subs.get(key, True)
        return TypeLookup(
            status="resolved" if status == "" else status,
            targets=tuple(self._candidate(*t) for t in targets),
            subtypes=tuple(self._candidate(k[0], k[1], k[2], subs[k]) for k in sorted(subs)))


def _resolve_nearest_keyed(index, analyzer_name, kind, name, ref_rel):
    """単純名での children 解決（手続き型言語の関数呼び出し解決共通）。

    `index[(analyzer_name,kind,name)]`（`[(rel, 実際の cid_key, c_kind|None), ...]`）を同一 top_scope に絞り、
    パス距離で最近傍を選ぶ。索引キーに `analyzer_name` を含め、他言語の同名と誤接続しない。
    `c_kind == "definition"` の候補があれば `"declaration"` を外して定義側だけで決める（無ければ全候補）。
    同距離複数は任意選択せず `'ambiguous'`。戻りは `(rel|None, 実キー|None, status)`。
    """
    cands = index.get((analyzer_name, kind, name))
    if not cands:
        return None, None, "unresolved"
    same = [(r, k, ck) for r, k, ck in cands if _top(r) == _top(ref_rel)]
    if not same:
        return None, None, "cross_scope"
    definitions = [(r, k) for r, k, ck in same if ck == "definition"]
    pool = definitions if definitions else [(r, k) for r, k, _ck in same]
    ranked = sorted(pool, key=lambda pair: _tree_distance(ref_rel, pair[0]))
    best = _tree_distance(ref_rel, ranked[0][0])
    nearest = [(r, k) for r, k in pool if _tree_distance(ref_rel, r) == best]
    if len(nearest) > 1:
        return None, None, "ambiguous"
    rel, actual_key = nearest[0]
    return rel, actual_key, ""



def _analyzer_by_name(analyzer_name):
    for a in analyzer_registry.known_analyzers():
        if a.name == analyzer_name:
            return a
    return None


def _analyzer_parents(analyzer_name):
    """型名の解決で名前空間が親へさかのぼる言語か。未登録名は `None`。"""
    a = _analyzer_by_name(analyzer_name)
    return None if a is None else bool(a.resolves_parent_namespaces)


def _analyzer_requires_context(analyzer_name) -> bool:
    a = _analyzer_by_name(analyzer_name)
    return bool(a is not None and a.requires_file_context)


def _simple_name_calls(analyzer_name) -> bool:
    """`analyzer_name` のアナライザが単純名の呼び出し解決（children への 2 段目）を要求するか。未登録名は False。"""
    for a in analyzer_registry.known_analyzers():
        if a.name == analyzer_name:
            return bool(getattr(a, "resolves_calls_by_simple_name", False))
    return False

def _resolve_include_relpath(rel_name, ref_rel, include_path, kind, name):
    """C の `#include "path"` の 1 段目解決: パス区切りを含む `include_path` を参照元 `ref_rel` からの相対パスとして解決する。

    その rel_path が実際に `name`（拡張子込みファイル名）の主体であれば一意に採用し、距離計算・曖昧判定は経由しない。
    同一 top_scope を跨ぐ解決はしない。見つからなければ `None`（`path_exact` の参照は未解決として申告する。それ以外は呼び出し側が basename の最近傍へ倒す）。
    区切りは `normpath` の前に `\\`→`/` へ直す。
    """
    base_dir = ref_rel.rsplit("/", 1)[0] if "/" in ref_rel else ""
    joined = f"{base_dir}/{include_path}" if base_dir else include_path
    candidate = posixpath.normpath(joined.replace("\\", "/"))
    if _top(candidate) != _top(ref_rel):
        return None
    return candidate if rel_name.get(candidate) == (kind, name) else None


def _resolve_by_copy_paths(defs, kind, name, ref_rel, copy_paths):
    """最近傍で決まらない COPY を、設定の `copy_paths` を先頭から試して決める。最初に一意に決まった prefix を採用する。

    参照元と同じ最上位フォルダ（世代）の prefix の下にある定義だけを候補にする。1 つの prefix の下に複数あれば、その prefix は決まらない扱いで次へ進む。
    戻りは `rel | None`。
    """
    cands = sorted(set(defs.get((kind, name), [])))
    top = _top(ref_rel)
    for prefix in copy_paths:
        if prefix.split("/", 1)[0] != top:
            continue
        hits = [r for r in cands if r.startswith(prefix + "/") and _top(r) == top]
        if len(hits) == 1:
            return hits[0]
    return None


def _resolve_include_alias(rel_name, aliases, include_path, ref_rel, kind, name):
    """`include_path` の先頭が設定の別名なら prefix に置き換え、同じ最上位フォルダの実在の定義に一致するときだけ `rel` を返す。"""
    expanded = resolve_settings.expand_alias(include_path, aliases)
    if expanded is None:
        return None
    candidate = posixpath.normpath(expanded)
    if _top(candidate) != _top(ref_rel):
        return None
    return candidate if rel_name.get(candidate) == (kind, name) else None


def _build_path_suffix_index(rel_name):
    """`_resolve_path_suffix` 用の事前索引 `(label, top_scope, 末尾パス) -> [rel, ...]` を Pass1 完了直後に 1 回だけ構築する。

    1 つの rel_path が持つ末尾パスは、パス区切り単位の末尾（`"c.xml"`／`"b/c.xml"`／`"a/b/c.xml"`）の深さの数だけ。
    """
    index: dict = defaultdict(list)
    for rel, (label, _name) in rel_name.items():
        top = _top(rel)
        parts = rel.split("/")
        for i in range(len(parts)):
            index[(label, top, "/".join(parts[i:]))].append(rel)
    return index


def _resolve_path_suffix(suffix_index, kind, suffix, ref_rel):
    """パス末尾一致での解決（Spring `<import resource>` 等の classpath 相対パス参照）。

    classpath 相対の指定は参照元ファイルの位置と無関係のため、資料フォルダ内の実ファイルの rel_path が
    指定パス（`suffix`）と末尾一致する形でしか解決しない（basename だけの最近傍解決はしない）。
    `suffix_index` から同一 top_scope 内の候補を引き、1 件に決まらなければ接続しない。
    戻りは `(rel|None, status)`。`status` は `''`(解決) / `'ambiguous'`(2 件以上) / `'unresolved'`(0 件)。
    """
    matches = suffix_index.get((kind, _top(ref_rel), suffix), [])
    if not matches:
        return None, "unresolved"
    if len(matches) > 1:
        return None, "ambiguous"
    return matches[0], ""


EDGE_SOURCES_MAX = 20     # 1 本の辺の `sources`（根拠）の上限。超過は `sources_overflow_count`

# 辺の根拠 `sources[].rule`（接続を決めた解決規則の名前）の閉じた一覧。解決器（`_link`）と言及の突合がここの名前だけを付ける。
EDGE_RULES = frozenset({
    "alias",                 # 別名（`using A = …`・`Imports A = …`）
    "single_import",         # 単一型の import
    "same_package",          # 同じ package／enclosing namespace
    "wildcard",              # ワイルドカード import（`using N;`・`Imports N`）
    "qualified_name",        # 完全修飾名の一致
    "path_exact",            # `#include "相対パス"` の相対パスの完全一致
    "path_suffix",           # classpath 相対のパス末尾の一致
    "config_key_all",        # 同名の設定キー全件
    "schema_exact",          # schema を書いた参照が同じ schema の表に一致
    "schema_unqualified",    # schema を書いた参照が schema の記載が無い定義に一致
    "table_name",            # schema を書かない参照の表名の一致
    "nearest_name",          # 同じ最上位フォルダの最近傍の同名
    "di_qualifier",          # DI の注入先の実装: 名前の指定（`@Qualifier`／`@Named`／`@Resource(name=)`）が bean 名に一致
    "di_primary",            # DI の注入先の実装: `@Primary` の付いた実装
    "di_single_impl",        # DI の注入先の実装: 実装が 1 つだけ
    "copy_path_setting",     # 最近傍で決まらない COPY を、資料フォルダの設定の `copy_paths` の先頭から試して一意に決めた
    "path_alias_setting",    # 資料フォルダの設定の `path_aliases` で別名を prefix に置き換えた実在の相対パスに一致
    "dictionary_match",      # 資料の本文と定義名の辞書突合（言及）
})


def _edge_source(e: dict) -> dict:
    """Pass 2 の候補 1 件 → 辺の根拠 1 件（`{via?, doc_id, file, line, rule?, from_def?}`）。辺が `via` を持たない（COBOL の COPY など）ときは `via` も持たない。"""
    src = {"doc_id": e["doc"], "file": e["doc"], "line": e["line"]}
    if e.get("via"):
        src = {"via": e["via"], **src}
    if e.get("_rule"):
        src["rule"] = e["_rule"]
    if e.get("_from_def") is not None:
        src["from_def"] = e["_from_def"]
    return src


def _source_dedupe_key(src: dict) -> tuple:
    fd = src.get("from_def")
    return (src["doc_id"], src["line"], src.get("via"), src.get("rule"),
            (fd["file"], fd["key"]) if fd else None, src.get("locator"))


def _source_place_key(src: dict) -> tuple:
    """根拠の位置（`via` を除く）。"""
    fd = src.get("from_def")
    return (src["doc_id"], src["line"], src.get("rule"), (fd["file"], fd["key"]) if fd else None, src.get("locator"))


def _source_sort_key(src: dict) -> tuple:
    fd = src.get("from_def")
    return (analyzer_registry.via_priority_rank(src.get("via")), src["doc_id"], src["line"],
            src.get("rule") or "", (fd["file"], fd["key"] or "") if fd else ("", ""), src.get("locator") or "")


def cap_sources(sources: list, first: dict | None = None) -> tuple:
    """根拠を重複排除 `(doc_id, line, via, rule, from_def, locator)` し、`via` の優先順位→文書→行で並べて上限で切る。戻りは `(根拠, 超過件数)`。
    `first`（省略可）は辺の代表の根拠で、並びの先頭に置く（残りだけを並べる）。
    """
    seen: set = set()
    uniq = []
    for src in sources:
        k = _source_dedupe_key(src)
        if k not in seen:
            seen.add(k)
            uniq.append(src)
    # 注入（`inject`）は同じ位置の宣言型（`field_type`）の格上げ: 同じ位置に `inject` の根拠があれば `field_type` の根拠は畳む。
    injected = {_source_place_key(u) for u in uniq if u.get("via") == "inject"}
    if injected:
        uniq = [u for u in uniq if not (u.get("via") == "field_type" and _source_place_key(u) in injected)]
    uniq.sort(key=_source_sort_key)
    if first is not None:
        fk = _source_dedupe_key(first)
        uniq = [u for u in uniq if _source_dedupe_key(u) == fk] + [u for u in uniq if _source_dedupe_key(u) != fk]
    return uniq[:EDGE_SOURCES_MAX], max(0, len(uniq) - EDGE_SOURCES_MAX)


def _aggregate_pass2_edges(raw_edges: list) -> list:
    """同一 `(src, type, dst)` の複数候補を 1 本へ集約し、候補の根拠を `sources` に並べる。

    代表（`doc`・`line`・`via` と `include_path` などの追加属性の持ち主）は、`via_priority_rank` が最小の候補（同順位は先着）。
    `sources` の先頭の件が代表の根拠で、残りは `cap_sources`（重複排除・並び・上限 20 件）。`(src,type,dst)` の初出順を保って返す。
    """
    groups: dict = {}
    order: list = []
    for e in raw_edges:
        key = (e["src"], e["type"], e["dst"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)
    out = []
    for key in order:
        cands = groups[key]
        rep = cands[0]
        for e in cands[1:]:
            if analyzer_registry.via_priority_rank(e.get("via")) < analyzer_registry.via_priority_rank(rep.get("via")):
                rep = e
        sources, overflow = cap_sources([_edge_source(e) for e in cands], first=_edge_source(rep))
        edge = {k: v for k, v in rep.items() if not k.startswith("_")}
        edge["sources"] = sources
        if overflow:
            edge["sources_overflow_count"] = overflow
        out.append(edge)
    return out


def _document_cid(world_id: str, rel: str) -> str:
    """Document ノードの同一性＝パス。言及元 Document ノードを rel_path で同一ノードに収束させるための cid。"""
    return f"document:{world_id}:{rel}"


# --- 辞書突合→言及エッジ（Pass3） ---
# Pass1 の定義索引 `(label,name)->[rel,...]` をそのまま辞書として、資料文書（`branch=="office"`）の本文と
# 決定的（LLM なし）に突合し、`Document -DOCUMENTS(via="mention")-> コードノード` を張る。
# `DOCUMENTS` は影響 traversal（`_IMPACT_REL`）に含まれない。

MENTION_SCHEMA_VERSION = 3   # 突合仕様の版（worker._sig の材料。仕様変更時に既存の資料フォルダを素通りさせない）

_MENTION_TOKEN_RE = re.compile(r"[A-Za-z0-9_#@$-]+")


def _mention_tokenize(text: str) -> list:
    """文書テキスト→識別子形トークン（`[A-Za-z0-9_-]+` の最大連続）。

    突合専用の正規化: 生値のまま（大文字小文字を区別する）。`identifiers.normalize_code_name` は使わない。
    重複トークンは初出順で 1 つにまとめる（トークン全体一致のみ・部分文字列検索はしない）。
    """
    seen: set = set()
    out: list = []
    for m in _MENTION_TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


# 辞書突合の名前長下限（1〜64）／1 文書あたりの言及エッジ上限。`worker.world_signature` の材料にもなる。
_MENTION_MIN_LEN = 4
_MENTION_MAX_PER_DOC = 200


def _mention_min_len() -> int:
    """辞書突合の名前長下限。"""
    return _MENTION_MIN_LEN


def _mention_max_per_doc() -> int:
    """1文書あたりの言及エッジ上限。"""
    return _MENTION_MAX_PER_DOC


def _mention_eligible(name: str, min_len: int) -> bool:
    """名前が辞書突合の対象になり得るか。

    長さ下限未満、または `_MENTION_TOKEN_RE` の 1 トークンとして丸ごと一致しない名前（例: `GROUP.LEAFNAME`）は
    突合し得ないため除外する。"""
    return len(name) >= min_len and _MENTION_TOKEN_RE.fullmatch(name) is not None


def _mention_dictionary(defs: dict, aliases: dict | None = None, *, min_len: int = 1) -> tuple[dict, int]:
    """`defs`（Pass1 の定義索引 `(label,key)->[rel,...]`）→ 言及突合の辞書 `name->[(label,rel,key),...]`。

    - `min_len` 未満の名前と、1 トークンとして生成されない修飾名は、辞書構築の時点で除外する。
    - 同一 top_scope（世代）内に同名の定義が複数ある場合は曖昧として、その世代は除外する（ラベルは問わない）。
      世代が違う同名は曖昧ではなく、全世代の定義をそれぞれ辞書に残す。
    - `key` は `defs` のキーそのもの（修飾名を含み得る）。dst の cid 組み立てに使うため、辞書引きの文字列とは別に保持する。
    - `aliases`（`(label,simple_name)->[(rel,key),...]`）は、`cid_key` と表示名が異なる定義（コピーブックの
      `GROUP.ITEM` 等）を表示名でも登録する。`build_world` は `key != name` のときだけ渡す。
    戻り値は `(mdict, ambiguous_alias_count)`。後者は `aliases` 経由の単純名のうち、同名衝突で張れなかった名前の数。
    """
    by_name_gen: dict = {}
    alias_names: set = set()
    for (label, key), rels in defs.items():
        if not _mention_eligible(key, min_len):
            continue
        for rel in rels:
            by_name_gen.setdefault(key, {}).setdefault(_top(rel), []).append((label, rel, key))
    for (label, name), pairs in (aliases or {}).items():
        if not _mention_eligible(name, min_len):
            continue
        alias_names.add(name)
        for rel, key in pairs:
            by_name_gen.setdefault(name, {}).setdefault(_top(rel), []).append((label, rel, key))
    out: dict = {}
    ambiguous_alias_count = 0
    for name, by_gen in by_name_gen.items():
        targets = []
        ambiguous_here = False
        for entries in by_gen.values():
            if len(entries) == 1:
                targets.append(entries[0])
            else:
                ambiguous_here = True                 # 同世代に複数定義＝その世代は任意選択しない
        if ambiguous_here and name in alias_names:
            ambiguous_alias_count += 1
        if targets:
            out[name] = targets
    return out, ambiguous_alias_count


def _ensure_mention_document(nodes: dict, world_id: str, rel: str) -> str:
    """言及元 Document ノードを get-or-create する（同一性＝パス・`_document_cid` と同じ規約）。"""
    cid = _document_cid(world_id, rel)
    if cid in nodes:
        return cid
    meta = _scope_meta(rel)
    nodes[cid] = {"cid": cid, "label": "Document", "name": rel, "world_id": world_id,
                 "top_scope": _top(rel), "phase": meta.get("phase"), "category": meta.get("category"),
                 "path": rel, "scope_path": "/".join(rel.split("/")[:-1]),
                 "value": None, "status": "active"}
    return cid


def _mention_locators(text: str, names: set) -> dict:
    """文書本文のうち `names`（辞書に一致したトークン）が現れる位置を `{名前: ([位置の表記, ...最大 EDGE_SOURCES_MAX 件], 位置の総数)}` で返す。

    位置は本文（派生 MD または原本のテキスト）の行で、直前の見出しがあれば添える。同じ行に何度現れても 1 件。
    """
    nl = [m.start() for m in re.finditer("\n", text)]
    head_lines: list = []                            # 見出しの行番号（昇順）
    head_texts: list = []
    for lineno, ln in enumerate(text.split("\n"), start=1):
        if ln.startswith("#"):
            h = ln.lstrip("#").strip()
            if h:
                head_lines.append(lineno)
                head_texts.append(h)
    seen: dict = {}
    for m in _MENTION_TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok not in names:
            continue
        lineno = bisect.bisect_left(nl, m.start()) + 1
        locs, total, last = seen.setdefault(tok, ([], 0, None))
        if last == lineno:
            continue
        hi = bisect.bisect_right(head_lines, lineno) - 1
        head = head_texts[hi] if hi >= 0 else None
        label = f"本文 {lineno} 行目" if head is None else f"{head}・本文 {lineno} 行目"
        if len(locs) < EDGE_SOURCES_MAX + 1:
            locs.append(label)
        seen[tok] = (locs, total + 1, lineno)
    return {t: (locs, total) for t, (locs, total, _l) in seen.items()}


def _mention_edges_for_doc(rel: str, text: str, mdict: dict, min_len: int, max_per_doc: int,
                           world_id: str, nodes: dict, edges: list, flags: list) -> None:
    """1 文書分の言及突合: トークン化→辞書突合→`Document -DOCUMENTS(via=mention)-> コード` を張る。

    1 文書あたりの上限（`max_per_doc`）を超えた分は張らず、件数を `flags`（`mention_overflow`）へ申告する。
    上限は 1 トークンが複数世代へ展開される場合もエッジ単位で数える。
    dst の cid は `targets` の `key`（修飾名を含み得る）で組み立てる。同一 `(doc, dst)` は 1 本にまとめ、文書内の言及の位置を
    根拠 `sources[].locator` に並べる（`doc` は言及元の文書・`line` は 0）。
    """
    added = 0
    overflow = 0
    doc_cid = None
    seen_dst: set = set()
    pending: list = []                               # (tok, edge)
    for tok in _mention_tokenize(text):
        if len(tok) < min_len:
            continue
        targets = mdict.get(tok)
        if not targets:
            continue
        for label, trel, key in targets:
            dst_cid = _cid(label, world_id, trel, key)
            if dst_cid in seen_dst:                   # 同一 (doc,dst) の重複エッジは作らない
                continue
            if added >= max_per_doc:
                overflow += 1
                continue
            if doc_cid is None:
                doc_cid = _ensure_mention_document(nodes, world_id, rel)
            edge = {"type": "DOCUMENTS", "src": doc_cid, "dst": dst_cid,
                    "doc": rel, "line": 0, "status": "active",
                    "via": "mention"}
            edges.append(edge)
            pending.append((tok, edge))
            seen_dst.add(dst_cid)
            added += 1
    if pending:
        located = _mention_locators(text, {t for t, _e in pending})
        for tok, edge in pending:
            locs, total = located.get(tok, ([], 0))
            sources = [{"via": "mention", "doc_id": rel, "file": rel, "line": 0, "rule": "dictionary_match",
                        "locator": loc} for loc in locs]
            if not sources:                           # 位置を特定できなかった（本文の再走査で見つからない）＝位置なしの根拠 1 件
                sources = [{"via": "mention", "doc_id": rel, "file": rel, "line": 0, "rule": "dictionary_match"}]
                total = 1
            capped, over = cap_sources(sources)
            edge["sources"] = capped
            if total > len(capped):
                edge["sources_overflow_count"] = total - len(capped)
    if overflow:
        flags.append({"reason": "mention_overflow", "doc": rel, "count": overflow})


def _mention_pass(world_dir, world_id: str, defs: dict, aliases: dict, files, nodes: dict,
                  edges: list, flags: list) -> None:
    """Pass3: 資料文書（`branch=="office"`）を辞書と突合し言及エッジを張る。

    コード定義が無ければ辞書が空のため文書列挙を省く。ソース原文（`branch=="source"`）は突合対象外。
    `aliases` は `_mention_dictionary` へそのまま渡し、単純名の同名衝突があれば `mention_ambiguous_names` を 1 件申告する。
    `worlds.pin_world_root` で `world_id→world_dir` の解決をこの呼び出しが受けた root に固定する
    （`doc_text.read_world_doc_text` が内部で `worlds.world_dir` を引くため）。
    """
    min_len = _mention_min_len()
    max_per_doc = _mention_max_per_doc()
    mdict, ambiguous_count = _mention_dictionary(defs, aliases, min_len=min_len)
    if ambiguous_count:
        flags.append({"reason": "mention_ambiguous_names", "count": ambiguous_count})
    if not mdict:
        return
    with worlds.pin_world_root(world_id, world_dir):
        docs = corpus_docs.iter_world_documents(world_id, include_rag=True,
                                                root=world_dir, files=files)
        for d in docs:
            if d.get("branch") != "office" or d.get("state") != "ready":
                continue
            rel = d["name"]
            text = doc_text.read_world_doc_text(world_id, d)
            if text is None:                          # 読めない＝スキップ
                flags.append({"reason": "unreadable_mention_doc", "doc": rel})
                continue
            _mention_edges_for_doc(rel, text, mdict, min_len, max_per_doc, world_id, nodes, edges, flags)


UNRESOLVED_PER_FILE_MAX = 50       # 1 ファイルの未解決の申告を Neo4j のノードへ保存する上限（超過は `unresolved_overflow_count`）
NO_PRIMARY_LINES_MAX = 20          # `no_primary_definition` の `lines` の上限（超過は `lines_omitted`）


def _count_nearest(rels, ref_rel) -> int:
    """同一 top_scope の候補 rel のうち、`ref_rel` から最短距離にある数（`_resolve_*` が曖昧にした候補数と同じ数え方）。"""
    same = [r for r in rels if _top(r) == _top(ref_rel)]
    if not same:
        return 0
    best = min(_tree_distance(ref_rel, r) for r in same)
    return sum(1 for r in same if _tree_distance(ref_rel, r) == best)


def _is_unresolved_flag(f: dict, ref_rel: str) -> bool:
    """`_link` が積んだ未解決系の申告（理由を列挙せず形で判定する＝`from`・`kind`・`name`・`line` を持つ）。"""
    return f.get("from") == ref_rel and "kind" in f and "name" in f and "line" in f


def _find_copy_cycles(nodes: dict, edges: list) -> list:
    """COPIES の辺（ファイル単位に畳む）から循環（強連結成分・2 パス以上）を検出し、含まれるパスの昇順の列を返す。"""
    graph: dict = defaultdict(set)
    for e in edges:
        if e.get("type") != "COPIES":
            continue
        a, b = nodes.get(e["src"]), nodes.get(e["dst"])
        if a is None or b is None or a["path"] == b["path"]:
            continue
        graph[a["path"]].add(b["path"])
        graph.setdefault(b["path"], set())
    index: dict = {}
    low: dict = {}
    on_stack: set = set()
    stack: list = []
    out: list = []
    counter = 0
    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(graph[w]))))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
    return sorted(out)


def plugin_failures_from_flags(flags) -> list:
    """`build_world` の `flags` から FW プラグインの失敗（`[{plugin, files, why}]`・プラグイン名順）を取り出す（`world_neo4j.load_world` が同じ世代で保存する）。"""
    return sorted(({"plugin": f["plugin"], "files": f.get("files"), "why": f.get("why")}
                   for f in flags if f.get("reason") == "plugin_failed" and f.get("plugin")),
                  key=lambda x: x["plugin"])


def build_world(world_dir, world_id: str, *, files=None, resolve_config=None):
    """資料フォルダ（登録ディレクトリ）を `(nodes, edges, flags)` にする。パス同一性＋同 top_scope 内の最近傍解決。

    骨格（Pass1/Pass2＝COPIES/CONTAINS/INVOKES/ACCESSES）と言及エッジ（Pass3）だけを決定的に構築する。
    `files`（省略可）: 呼び出し側が `scope_infer.safe_files(world_dir)` を materialize 済みなら渡す（再度歩かない）。
    `_重要度.txt` はここで除外する。
    `resolve_config`（省略可）: 資料フォルダの解決範囲の設定 `{copy_paths, path_aliases}`（`resolve_settings` で正規化済み）。
    最近傍で決まらない COPY を `copy_paths` で、`#include` の別名を `path_aliases` で解く。無ければ今までの解決だけ。
    """
    from .analyzers import _ts
    _ts.require()   # Tree-sitter の本体と文法が読めなければ、ここで取り込みを失敗させる（ファイルごとの失敗に散らさない）
    copy_paths = list((resolve_config or {}).get("copy_paths") or [])
    path_aliases = dict((resolve_config or {}).get("path_aliases") or {})
    entries = files if files is not None else scope_infer.safe_files(
        world_dir, also=worlds.archives_dir(world_id))
    # 秘匿ファイル（環境変数ファイル系・秘密鍵系・credentials 等）はグラフ取り込みでも読まない。
    # Pass1 は拡張子で候補を引くため、拡張子付きの秘匿名（YAML・shell 等）を別に塞ぐ。
    files = [(rp, rel) for rp, rel in entries
             if not importance.is_importance_control_path(rel)
             and not text_kind.is_sensitive(PurePosixPath(rel).name, PurePosixPath(rel).suffix.lower())]

    defs: dict = {}            # (label, NAME) -> [rel, ...]
    qualified_defs = _QualifiedIndex()  # (label, cid_key) -> [(rel, 実名), ...]（cid_key が付く定義は常時登録）
    rel_name: dict = {}        # rel -> (label, NAME)  ＝ファイルの主体名
    def_qkey: dict = {}        # (rel, 子の識別子|None=主体) -> 完全修飾名（型の関係の候補が持つ）
    symbol_cids: dict = {}     # (rel, cid_key) -> 定義ノード（children）の cid  ＝`RefCandidate.source_symbol_id` の解決先
    texts: dict = {}           # rel -> (text, analyzer)
    nodes: dict = {}           # cid -> node
    edges: list = []
    link_edges: list = []      # Pass2 の解決済みエッジ（集約前のステージング）
    flags: list = []
    fw_build_ctx: dict = {}    # FW プラグイン名 -> その取り込み 1 回だけの作業領域（`uses_build_context`）
    fw_ctx: dict = {}          # rel -> ([適用する FW プラグイン], 本体の DefResult)（Pass2 でも同じ本体の定義を渡す）
    plugin_failed: dict = {}   # FW プラグイン名 -> {"files": {rel}, "from": 最初の rel, "why": 理由}
    # 言及突合の単純名エイリアス: (label, DefItem.name) -> [(rel, DefItem.key), ...]。
    # `key`（cid_key）が `name`（表示名）と異なる子定義（コピーブックの `GROUP.ITEM` 等）だけを登録する。
    mention_aliases: dict = {}
    # 手続き型言語の関数呼び出し解決専用の索引: (analyzer_name, label, 単純名) -> [(rel, cid_key, c_kind|None), ...]。
    # children は `defs` に cid_key（修飾名）でしか登録されないため、`_link` が通常解決で unresolved のときだけ
    # 2 段目として参照する。索引キーに analyzer_name を含め、他言語の同名と誤接続しない。
    simple_name_defs: dict = {}
    # `Table` 参照の解決専用: NAME -> [(rel, cid_key, schema|None), ...]（同名の表が複数 schema にある場合を区別する）。
    table_defs: dict = {}
    # `via=config_key` 専用の索引: (label, key_kind, 裸キー) -> [(rel, cid_key), ...]。
    # 設定ファイルのキー child（`label=="Config"` かつ `cid_key` が `"key:"` 接頭辞）だけを登録し、primary（`defs`）候補を混ぜない。
    # `key_kind`（"property"/"bean"/"action"/"mapper"/"url"/"env"）を索引キーに含め、同じ裸キーの別種別を同一視しない。
    config_key_index: dict = {}
    global_imports: dict = {}  # (アナライザ名, 最上位フォルダ) -> [ImportItem, ...]（`global using` など世代内の同じ言語の全ファイルへ効く import）
    # VB.NET のプロジェクト設定（`.vbproj`・`Directory.Build.props`）。ソース rel -> 当てたプロジェクト。
    # 当てたプロジェクトのソースの型・手続きは、完全修飾名の索引に本当の名前（`<RootNamespace>.…`）だけで載せる（ノードの識別子は変えない）。
    vb_bound: dict = {}
    qualified_defs.project_of = vb_bound
    vb_projects: list = []
    vb_props: dict = {}

    def _vb_flag(rel, why, snippet):
        flags.append({"reason": "dropped_syntax", "analyzer": "vb", "from": rel, "why": why, "line": 1, "snippet": snippet[:200]})

    vb_proj_dirs = {str(PurePosixPath(rel).parent) if "/" in rel else ""
                    for _rp, rel in files if rel.lower().endswith(vb_project.VBPROJ_EXT)}
    for rp, rel in files:
        low = rel.lower()
        is_proj, is_props = low.endswith(vb_project.VBPROJ_EXT), low.rsplit("/", 1)[-1] == vb_project.PROPS_NAME
        if not (is_proj or is_props):
            continue
        if is_props:                                      # 当てはめる `.vbproj` の祖先（自分のフォルダを含む）にあたる props だけ読む
            pdir = str(PurePosixPath(rel).parent) if "/" in rel else ""
            if not any(pdir == "" or d == pdir or d.startswith(pdir + "/") for d in vb_proj_dirs):
                continue
        try:
            if rp.stat().st_size > text_kind.MAX_BYTES:
                raise ValueError("size_exceeded")
            vb_text = vb_project.decode_xml(rp.read_bytes())
            parsed = vb_project.parse_vbproj(rel, vb_text) if is_proj else vb_project.parse_props(rel, vb_text)
        except (OSError, ValueError, RecursionError) as e:   # 読めない設定は当てはめの「止め」にして申告する（外側の設定へは倒さない）
            _vb_flag(rel, "vbproj_unreadable", str(e) if isinstance(e, ValueError) else type(e).__name__)
            parsed = vb_project.broken_project(rel) if is_proj else vb_project.PropsRoot(declared=True)
        if is_proj:
            vb_projects.append(parsed)
        else:
            vb_props[str(PurePosixPath(rel).parent) if "/" in rel else ""] = parsed
        for v in parsed.unevaluated:
            _vb_flag(rel, "vbproj_unevaluated", v)
        for v in getattr(parsed, "outside", ()):
            _vb_flag(rel, "vbproj_include_outside", v)
        for v in getattr(parsed, "dropped_imports", ()):
            _vb_flag(rel, "vbproj_import_alias", v)
    vb_index = vb_project.ProjectIndex([vb_project.resolve_root(p, vb_props) for p in vb_projects])

    def _def(label, name, rel):
        defs.setdefault((label, name), []).append(rel)
        rel_name[rel] = (label, name)

    def _index_def(label, name, rel):
        """`defs` 解決索引にだけ登録する（`rel_name` は更新しない）。

        children は解決対象にしたいがファイルの主体ではないため、`_def` と違い主体の src 決定を壊さない。
        """
        defs.setdefault((label, name), []).append(rel)

    def _index_qualified(label, cid_key, rel, actual_name):
        """完全修飾名を解決索引（`defs` とは別枠）へ追加登録する。

        `cid_key`（または `DefItem.qualified`）が付いているものは条件なしで登録する（children は cid が `cid_key` で組み立てられるため、
        skip すると完全修飾名参照が登録漏れになる）。
        """
        if cid_key is not None:
            proj = vb_bound.get(rel)
            if proj is not None and proj.root_namespace:      # VB.NET のプロジェクトのソースは本当の完全修飾名（Root Namespace つき）だけ
                cid_key = f"{proj.root_namespace}.{cid_key}"
            qualified_defs.setdefault((label, cid_key), []).append((rel, actual_name))

    def _sanitized_extra(analyzer_name, rel, label, name, base_keys, extra):
        """`DefItem.extra` から共通層が確定したフィールドと同名のキーを除去する。

        `cid`/`label`/`name`/`analyzer` 等を上書きさせない。1 つでも衝突したら `extra` を丸ごと捨て、理由を `flags` に記録する。
        """
        bad = set(extra) & base_keys
        if not bad:
            return extra
        flags.append({"reason": "reserved_key_in_extra", "analyzer": analyzer_name, "from": rel,
                      "label": label, "name": name, "keys": sorted(bad)})
        return {}

    def _note_plugin_failures(rel, failures):
        """適用時に例外を出した FW プラグインを集計する（最後に 1 プラグイン 1 件の `plugin_failed` として `flags` へ出す）。"""
        for f in failures:
            rec = plugin_failed.setdefault(f.plugin, {"files": set(), "from": rel, "why": f"{f.phase}: {f.why}"})
            rec["files"].add(rel)

    def _flag_dropped(analyzer_name, rel, dropped):
        """`Dropped`（解析せず落とした構文）を `flags` へ記録する（黙って消さない）。"""
        for d in dropped:
            flags.append({"reason": "dropped_syntax", "analyzer": analyzer_name, "from": rel,
                          "why": d.reason, "line": d.line, "snippet": d.snippet})

    def _register_children(parent_cid, children, rel, analyzer_name):
        """`parent -CONTAINS-> child` を 1 本ずつ生成する共通処理（primary の `children` と `DefResult.extras` の両方で使う）。"""
        for child in children:
            if child.label not in analyzer_registry.NODE_LABELS:
                flags.append({"reason": "unknown_label", "analyzer": analyzer_name,
                              "label": child.label, "from": rel})
                continue
            child_cid = _cid(child.label, world_id, rel, child.key)
            child_base = {**_node(child.label, world_id, rel, child.name, value=child.value),
                          "cid": child_cid, "line": child.line, "analyzer": analyzer_name}
            child_extra = _sanitized_extra(analyzer_name, rel, child.label, child.name,
                                           child_base.keys(), child.extra)
            nodes[child_cid] = {**child_base, **child_extra}
            _index_def(child.label, child.key, rel)   # children も解決対象にする
            symbol_cids[(rel, child.key)] = child_cid
            def_qkey[(rel, child.key)] = child.resolve_key
            _index_qualified(child.label, child.resolve_key, rel, child.key)
            if child.key != child.name:                # 修飾名≠表示名＝言及辞書に単純名でも登録
                mention_aliases.setdefault((child.label, child.name), []).append((rel, child.key))
                if _simple_name_calls(analyzer_name):
                    # 手続き型言語の関数呼び出し解決専用の単純名索引（`Analyzer.resolves_calls_by_simple_name`）:
                    # `defs` は children を cid_key（修飾名）でしか索引しないため、単純名の呼び出しは `_link` が 2 段目で `simple_name_defs` を引く。
                    # `c_kind` は定義（`.c`）を宣言（`.h`）より優先する材料。索引キーに analyzer_name を含め、他言語の同名と誤接続しない。
                    simple_name_defs.setdefault((analyzer_name, child.label, child.name), []).append(
                        (rel, child.key, child.extra.get("c_kind")))
            if child.cid_key is not None and child.cid_key.startswith("key:"):
                # `via=config_key` 専用索引（設定ファイルのキー child のみ）。key_kind で名前空間を分ける。
                config_key_index.setdefault(
                    (child.label, child.extra.get("key_kind"), child.name), []).append((rel, child.key))
            edges.append({"type": "CONTAINS", "src": parent_cid, "dst": child_cid, "doc": rel,
                          "line": child.line, "status": "active"})

    # --- Pass 1: 定義収集＋ノード（拡張子→アナライザを引いて collect_defs を呼ぶ汎用ループ）---
    for rp, rel in files:
        candidates = analyzer_registry.candidates(rel)
        if not candidates:                                # どのアナライザも拡張子を担当しない＝資料
            continue
        # サイズ上限（`text_kind.MAX_BYTES`）: 全アナライザに一律で適用する。
        # 読み飛ばさないと `read_full_text_and_raw()` が巨大ファイルを全量メモリに読み込み、単一 worker が OOM し得る。
        try:
            oversize = rp.stat().st_size > text_kind.MAX_BYTES
        except OSError:
            oversize = False               # stat 失敗は下の read_full_text_and_raw() 側の OSError 処理に委ねる
        if oversize:
            flags.append({"reason": "dropped_syntax", "analyzer": candidates[0].name, "from": rel,
                          "why": "size_exceeded", "line": 1, "snippet": ""})
            continue
        try:
            text, raw = corpus_docs.read_full_text_and_raw(rp)
        except OSError:
            # 受理済み（拡張子が一致する）コード文書の実読込失敗は run 全体を失敗させる（fail-closed）。
            # blocked flag により台帳書込・Neo4j 反映へ進ませず、部分グラフを確定しない（次回 sync で全再構築）。
            flags.append({"doc": rel, "reason": "unreadable_code_file", "action": "blocked"})
            continue
        # 台帳（`corpus_docs.classify_document`）・grep・精読が「文字コードを判別できない」として対象外にする原本は、
        # グラフにもノード・エッジを作らない（判定は `text_encoding.quality_of` を直接使う）。
        encoding, enc_ratio, enc_majority_garbled = text_encoding.detect_bytes_quality(raw, complete=True)
        if text_encoding.quality_of(enc_ratio, enc_majority_garbled) == "undetermined":
            flags.append({"doc": rel, "reason": "encoding_undetermined", "action": "warn"})
            continue
        # `accepts()` に渡す head は `Analyzer.head_bytes`（既定 4KiB）のバイト数で、読み取り済みの生バイト列 `raw` を
        # スライス・デコードする（ファイルを開き直さない）。
        def _head_for(a, raw=raw, encoding=encoding):
            return text_encoding.decode(raw[:getattr(a, "head_bytes", 4096)], encoding)
        analyzer = next((a for a in candidates if a.accepts(rel, _head_for(a))), None)
        if analyzer is None:                              # 拡張子は一致するが内容判定で不採用
            continue
        # 受理済み（拡張子一致＋accepts 通過）なら主体の有無に関わらず Pass2 を通す（主体なしファイルも dropped_syntax 検知の対象）。
        texts[rel] = (rp, analyzer, hashlib.sha1(raw).hexdigest())   # 本文は保持しない（Pass2 で読み直す＝メモリを有界化）・指紋で同一性を照合
        if analyzer.name == "vb" and rel.lower().endswith(".vb") and vb_index:
            proj, ambiguous = vb_index.project_for(rel)
            if proj is not None:
                vb_bound[rel] = proj
            elif ambiguous:                               # 同じフォルダに当たり得る .vbproj が複数＝当てずに申告する
                flags.append({"reason": "dropped_syntax", "analyzer": "vb", "from": rel,
                              "why": "vbproj_ambiguous", "line": 1, "snippet": ", ".join(ambiguous)[:200]})
        defres = analyzer.collect_defs(text, rel)
        fw_here = analyzer_registry.applicable_fw_plugins(analyzer, text, rel)
        if fw_here:                                       # FW プラグイン: 本体の定義へ追加分を足す（失敗は本体の結果を残して申告）
            base_defres = defres
            defres, pfails = analyzer_registry.apply_fw_defs(fw_here, text, rel, base_defres, fw_build_ctx)
            _note_plugin_failures(rel, pfails)
            fw_failed = {f.plugin for f in pfails}
            fw_ctx[rel] = ([p for p in fw_here if p.name not in fw_failed], base_defres)
        for imp in analyzer.global_imports(text, rel):
            global_imports.setdefault((analyzer.name, _top(rel)), []).append(imp)
        _flag_dropped(analyzer.name, rel, defres.dropped)
        if defres.primary is None:                        # 構文にマッチせず主体を持たない
            continue
        if defres.primary.label not in analyzer_registry.NODE_LABELS:
            flags.append({"reason": "unknown_label", "analyzer": analyzer.name,
                          "label": defres.primary.label, "from": rel})
            continue
        _def(defres.primary.label, defres.primary.name, rel)
        _index_qualified(defres.primary.label, defres.primary.resolve_key, rel, defres.primary.name)
        def_qkey[(rel, None)] = defres.primary.resolve_key
        prim_cid = _cid(defres.primary.label, world_id, rel, defres.primary.name)
        prim_base = {**_node(defres.primary.label, world_id, rel, defres.primary.name,
                             value=defres.primary.value), "analyzer": analyzer.name}
        prim_extra = _sanitized_extra(analyzer.name, rel, defres.primary.label, defres.primary.name,
                                      prim_base.keys(), defres.primary.extra)
        nodes[prim_cid] = {**prim_base, **prim_extra}
        if defres.primary.label == "Table":
            table_defs.setdefault(defres.primary.name, []).append(
                (rel, defres.primary.key, prim_extra.get("schema")))
        _register_children(prim_cid, defres.children, rel, analyzer.name)

        # 同一ファイル内の主体以外のトップレベル定義（DDL の 2 件目以降の `CREATE TABLE` 等）。
        # primary と同じ規則でノード化・索引登録するが、`_index_def` を使い `rel_name[rel]` は更新しない。
        for group in defres.extras:
            item = group.primary
            if item.label not in analyzer_registry.NODE_LABELS:
                flags.append({"reason": "unknown_label", "analyzer": analyzer.name,
                              "label": item.label, "from": rel})
                continue
            _index_def(item.label, item.name, rel)
            _index_qualified(item.label, item.resolve_key, rel, item.name)
            extra_cid = _cid(item.label, world_id, rel, item.key)
            extra_base = {**_node(item.label, world_id, rel, item.name, value=item.value),
                          "cid": extra_cid, "line": item.line, "analyzer": analyzer.name}
            extra_props = _sanitized_extra(analyzer.name, rel, item.label, item.name,
                                           extra_base.keys(), item.extra)
            nodes[extra_cid] = {**extra_base, **extra_props}
            if item.key != item.name:                  # 修飾名≠表示名＝言及辞書に単純名でも登録
                mention_aliases.setdefault((item.label, item.name), []).append((rel, item.key))
            if item.label == "Table":
                table_defs.setdefault(item.name, []).append((rel, item.key, extra_props.get("schema")))
            _register_children(extra_cid, group.children, rel, analyzer.name)

    # --- Pass 2: 参照解決（同 top_scope 内 最近傍）＋構造エッジ ---
    def _apply_extra(edge, etype, ref_rel, analyzer_name, extra):
        """`RefCandidate.extra` を解決後のエッジへ加算的に透過する。

        細分ラベル `via` は既知値（`KNOWN_VIA`）のみ通し、未知値は flags へ記録してその属性だけを落とす（エッジ自体は張る）。
        """
        if not extra:
            return
        via = extra.get("via")
        if via is not None and via not in analyzer_registry.KNOWN_VIA:
            flags.append({"reason": "unknown_via", "analyzer": analyzer_name,
                          "from": ref_rel, "edge_type": etype, "via": via})
            extra = {k: v for k, v in extra.items() if k != "via"}
        bad = set(extra) & edge.keys()                   # 共通層が確定した既存キーは上書きさせない
        if bad:
            flags.append({"reason": "reserved_key_in_extra", "analyzer": analyzer_name,
                          "from": ref_rel, "edge_type": etype, "keys": sorted(bad)})
        else:
            edge.update(extra)

    def _link_config_key_all(etype, src_cid, kind, name, ref_rel, line, analyzer_name, extra, reverse, from_def):
        """`via=config_key` の参照だけ、同一 top_scope 内の同名 `Config` キー全件へ 1 本ずつエッジを張る特例。

        通常の最近傍/ambiguous 判定を迂回する（環境別設定ファイルが同名キーを持つ場合に両方へ張るため）。
        候補源は `config_key_index`（`"key:"` 接頭辞を持つ Config child だけ）で、primary（`defs`）は混ぜない。
        dst cid は一致した候補の `cid_key` で組み立てる。`key_kind` も索引キーに含める（無い参照は無い定義としか一致しない）。
        """
        cands = list(config_key_index.get((kind, extra.get("key_kind"), name), []))
        same = [(r, key) for r, key in cands if _top(r) == _top(ref_rel)]
        if not same:
            flags.append({"reason": "cross_scope" if cands else "unresolved",
                          "from": ref_rel, "kind": kind, "name": name,
                          "line": line, "via": extra.get("via")})
            return
        for rel, key in same:
            dst_cid = _cid(kind, world_id, rel, key)
            edge_src, edge_dst = (dst_cid, src_cid) if reverse else (src_cid, dst_cid)
            edge = {"type": etype, "src": edge_src, "dst": edge_dst,
                   "doc": ref_rel, "line": line, "status": "active"}
            _apply_extra(edge, etype, ref_rel, analyzer_name, dict(extra))
            edge["_rule"], edge["_from_def"] = "config_key_all", from_def
            link_edges.append(edge)

    def _link(etype, src_cid, kind, name, ref_rel, line, analyzer_name=None, extra=None, reverse=False,
              file_context=None, from_def=None):
        """参照 1 件を解決して構造エッジを積む。`file_context` はその参照を含むファイルの解析の文脈（解決器が読む入力）。
        `from_def` は参照元の定義 `{file, key}`。積む候補に、接続を決めた解決規則（`_rule`）と `_from_def` を付ける（集約が根拠 `sources` にする）。
        """
        extra = dict(extra) if extra else {}
        if file_context is not None:                      # その参照の行から見える package／namespace と import だけを解決器へ渡す
            file_context = file_context.at(line)
        # `qualified` は解決の指示であってエッジの事実ではないため、共通層が消費して取り除く
        absolute = bool(extra.pop("absolute", False))
        qualified = (bool(extra.pop("qualified", False)) and "." in name) or absolute
        type_ref = bool(extra.pop("type_ref", False))
        rule_hint = extra.pop("resolution_rule", None)    # FW プラグインが解決の根拠として付ける規則名（`EDGE_RULES` の名前だけ）
        chain_parents = _analyzer_parents(analyzer_name) if type_ref else None
        requires_context = _analyzer_requires_context(analyzer_name) if type_ref else False

        if extra.get("via") == "config_key":              # A9: 通常解決の前に特例へ分岐
            _link_config_key_all(etype, src_cid, kind, name, ref_rel, line, analyzer_name, extra, reverse, from_def)
            return

        resolved_name = name
        rule = "nearest_name"                             # 接続を決めた解決規則（分岐ごとに上書きする）
        # 同様に pop する（残すと `_apply_extra` が edge のプロパティへ透過する）
        path_suffix = bool(extra.pop("path_suffix", False)) if extra.get("via") == "include" else False
        path_exact = bool(extra.pop("path_exact", False)) if extra.get("via") == "include" else False
        include_path = extra.get("include_path") if extra.get("via") == "include" else None

        def _flag(reason, flag_name):
            flags.append({"reason": reason, "from": ref_rel, "kind": kind, "name": flag_name,
                          "line": line, "via": extra.get("via")})

        if path_suffix:
            # Spring `<import resource>` 等の classpath 相対パス参照: 資料フォルダ内の実ファイルの rel_path が末尾一致する形でしか解決しない。
            # 一意に決まらなければ接続しない。
            rel, status = _resolve_path_suffix(path_suffix_index, kind, name, ref_rel)
            if status:
                flags.append({"reason": ("config_import_ambiguous" if status == "ambiguous"
                                         else "config_import_unresolved"),
                             "from": ref_rel, "kind": kind, "name": name, "line": line,
                             "via": extra.get("via")})
                return
            resolved_name = rel_name[rel][1]
            rule = "path_suffix"
        elif include_path and "/" in include_path:
            # `#include "path"` の 1 段目: 相対パス完全一致（同一 top_scope 内）。`path_exact` の参照は、一致しなければ未解決として申告する
            # （basename の最近傍へ倒さない）。それ以外は 2 段目（拡張子込み basename の最近傍）へ。
            rel = _resolve_include_relpath(rel_name, ref_rel, include_path, kind, name)
            if rel is None and path_aliases:
                rel = _resolve_include_alias(rel_name, path_aliases, include_path, ref_rel, kind, name)
                if rel is not None:
                    rule = "path_alias_setting"
            if rel is None:
                if path_exact:
                    _flag("unresolved_qualifier", include_path)
                    return
                rel, status = _resolve_nearest(defs, kind, name, ref_rel)
                if status:
                    _flag(status, name)
                    return
            elif rule != "path_alias_setting":
                rule = "path_exact"
        elif kind == "Table":
            schema_name, simple_name = getattr(name, "schema", None), getattr(name, "simple", name)
            rel, actual_name, status = _resolve_table(table_defs, name, ref_rel)
            if status:                                    # schema 違い・同名の複数 schema は任意選択しない
                flags.append({"reason": status, "from": ref_rel, "kind": kind, "name": name,
                             "line": line, "via": extra.get("via")})
                return
            resolved_name = actual_name
            if schema_name is None:
                rule = "table_name"
            else:
                chosen = next((c for c in table_defs.get(simple_name, []) if c[0] == rel and c[1] == actual_name), None)
                rule = "schema_exact" if chosen is not None and chosen[2] == schema_name else "schema_unqualified"
        elif qualified:
            # 完全修飾名の解決: 一致する定義が無ければ、短名へ倒さず未解決として申告する。
            if type_ref and chain_parents:
                rel, actual_name, status = _resolve_dotted_type_name(
                    qualified_defs, kind, name, ref_rel, file_context, chain_parents,
                    file_context.package_at(line) if file_context is not None else None, absolute)
            else:
                rel, actual_name, status = _resolve_qualified(qualified_defs, kind, name, ref_rel)
            if status:                                    # ambiguous／cross_scope／unresolved
                _flag("unresolved_qualifier" if status == "unresolved" else status, name)
                return
            resolved_name = actual_name
            rule = "qualified_name"
        elif type_ref and (file_context is not None or requires_context):
            if file_context is None:                      # 必須の文脈が届かなかった（`file_context_missing` を別に申告済み）
                _flag("unresolved", name)
                return
            rule_out: list = []
            rel, actual_name, status, shown = _resolve_type_name(
                qualified_defs, defs, kind, name, ref_rel, file_context, bool(chain_parents), file_context.package_at(line),
                rule_out=rule_out)
            if status:
                _flag(status, shown)
                return
            resolved_name = actual_name
            rule = rule_out[0] if rule_out else "nearest_name"
        else:
            rel, status = _resolve_nearest(defs, kind, name, ref_rel)
            if status == "unresolved" and _simple_name_calls(analyzer_name) and extra.get("via") == "call":
                # 手続き型言語（C/VB）の関数呼び出し解決: 通常解決で見つからない単純名は `simple_name_defs` を 2 段目として参照し、
                # その判定結果（解決/ambiguous/cross_scope/unresolved）をそのまま採用する。
                # 参照側も同じアナライザ由来かつ `via=call` のときだけに限る。
                alt_rel, alt_key, alt_status = _resolve_nearest_keyed(
                    simple_name_defs, analyzer_name, kind, name, ref_rel)
                if alt_status:
                    status = alt_status
                else:
                    rel, resolved_name, status = alt_rel, alt_key, ""
            if status and etype == "COPIES" and copy_paths:
                # 最近傍で決まらない COPY: 設定の `copy_paths` を先頭から試す。決まらなければ元の状態（未解決・曖昧）のまま申告する
                alt_rel = _resolve_by_copy_paths(defs, kind, name, ref_rel, copy_paths)
                if alt_rel is not None:
                    rel, status, rule = alt_rel, "", "copy_path_setting"
            if status:                                    # ''=解決／ambiguous/cross_scope/unresolved は flag
                _flag(status, name)
                return

        dst_cid = _cid(kind, world_id, rel, resolved_name)
        edge_src, edge_dst = (dst_cid, src_cid) if reverse else (src_cid, dst_cid)
        edge = {"type": etype, "src": edge_src, "dst": edge_dst,
               "doc": ref_rel, "line": line, "status": "active"}
        _apply_extra(edge, etype, ref_rel, analyzer_name, extra)
        if rule_hint is not None:
            if rule_hint in EDGE_RULES:
                rule = rule_hint
            else:
                flags.append({"reason": "unknown_rule", "analyzer": analyzer_name, "from": ref_rel, "rule": str(rule_hint)})
        edge["_rule"], edge["_from_def"] = rule, from_def
        link_edges.append(edge)

    # Pass1 完了直後（`rel_name` 確定後）に 1 回だけ構築し、`_link` が閉包で参照する
    path_suffix_index = _build_path_suffix_index(rel_name)

    def _ambiguous_count(f, ref_rel, analyzer_name):
        """曖昧にした候補の数（`flags` の `candidates`）。判定した索引（修飾名・単純名・表・手続き型の単純名・パス末尾）を引き直して数える。"""
        kind, name = f["kind"], f["name"]
        if f.get("reason") == "config_import_ambiguous":
            return len(path_suffix_index.get((kind, _top(ref_rel), name), []))
        if kind == "Table":
            same = [c for c in table_defs.get(getattr(name, "simple", name), []) if _top(c[0]) == _top(ref_rel)]
            schema = getattr(name, "schema", None)
            if schema is not None:
                same = [c for c in same if c[2] == schema] or [c for c in same if c[2] is None]
            elif len({c[2] for c in same}) > 1:
                return len(same)
            return _count_nearest([c[0] for c in same], ref_rel)
        pools = [[r for r, _n in qualified_defs.get((kind, name), [])],
                 list(defs.get((kind, name), []))]
        keyed = [(r, ck) for r, _k, ck in simple_name_defs.get((analyzer_name, kind, name), [])
                 if _top(r) == _top(ref_rel)]
        pools.append([r for r, ck in keyed if ck == "definition"] or [r for r, _ck in keyed])
        for pool in pools:
            n = _count_nearest(pool, ref_rel)
            if n > 1:
                return n
        return None

    unresolved_by_rel: dict = {}   # rel -> 未解決の申告（ファイルの主体ノードへ保存する）
    unresolved_subject: dict = {}  # rel -> 主体ノードの cid

    def _with_global_imports(analyzer, rel, file_context):
        """`file_context` へ、同じ世代・同じ言語の全ファイルへ効く import（`global using` など）と、VB.NET のプロジェクト設定
        （`.vbproj` の Import・Root Namespace）を足す。本体の解決・FW プラグイン・型の関係の段が同じ文脈を使う。"""
        if file_context is None:
            return file_context
        if global_imports.get((analyzer.name, _top(rel))):
            def _key(i):
                return (i.kind, i.name, i.alias, i.static, i.scope, i.is_global)   # 有効範囲が違う同名の import は別物（namespace 内の `using` と `global using`）
            seen = {_key(i) for i in file_context.imports}
            extra_imports = [i for i in global_imports[(analyzer.name, _top(rel))] if _key(i) not in seen]
            file_context = FileContext(package=file_context.package, imports=file_context.imports + extra_imports,
                                       namespaces=file_context.namespaces)
        proj = vb_bound.get(rel)
        if proj is not None:
            file_context = vb_project.apply_project(file_context, proj)
        return file_context

    # Pass 2 の前の段: 型の継承・実装の関係を使う FW プラグインがあるときだけ、全ファイルの継承・実装の宣言（本体の参照のうち `via` が `extends`／`implements`）を
    # 集めて、プラグインへ渡す読み取り専用の口（`TypeRelations`）にする。宣言の継承元の解決は `_type_candidates`（`_link` と同じ規則）。
    type_relations = None
    type_langs = {lang for plugins, _base in fw_ctx.values() for p in plugins if p.uses_type_relations for lang in p.languages}
    if type_langs:   # 継承・実装は同じ言語の中で引く前提＝型の関係を使うプラグインの `languages` のアナライザのファイルだけ集める
        type_records: list = []
        for t_rel, (t_rp, t_analyzer, t_sha) in texts.items():
            if t_analyzer.name not in type_langs:
                continue
            try:
                if t_rp.stat().st_size > text_kind.MAX_BYTES:
                    flags.append({"doc": t_rel, "reason": "changed_between_passes", "action": "blocked"})
                    continue
                t_text, t_raw = corpus_docs.read_full_text_and_raw(t_rp)
            except OSError:
                flags.append({"doc": t_rel, "reason": "unreadable_code_file", "action": "blocked"})   # 型の候補が欠けたグラフを確定させない
                continue
            if hashlib.sha1(t_raw).hexdigest() != t_sha:
                flags.append({"doc": t_rel, "reason": "changed_between_passes", "action": "blocked"})
                continue
            if t_rel not in rel_name:
                continue
            t_res = t_analyzer.extract_refs(t_text, t_rel)
            t_ctx = _with_global_imports(t_analyzer, t_rel, t_res.file_context)
            for r in t_res.refs:
                ex = r.extra or {}
                if ex.get("via") not in ("extends", "implements") or r.reverse:
                    continue
                sub_actual, sub_qkey = rel_name[t_rel][1], def_qkey.get((t_rel, None))
                if r.source_symbol_id is not None and r.source_symbol_id[0] == t_rel and r.source_symbol_id in symbol_cids:
                    sub_actual, sub_qkey = r.source_symbol_id[1], def_qkey.get(r.source_symbol_id)
                type_records.append({
                    "rel": t_rel, "qkey": sub_qkey, "actual": sub_actual, "kind": r.kind, "name": r.name,
                    "ctx": t_ctx.at(r.line) if t_ctx is not None else None, "qualified": bool(ex.get("qualified"))})
        type_relations = _TypeRelationsImpl(
            qualified_defs, defs, type_records,
            lambda rel: bool(texts[rel][1].resolves_parent_namespaces) if rel in texts else False)

    for rel, (rp, analyzer, raw_sha1) in texts.items():
        # Pass1 は本文を保持しない（コード総量に比例したメモリを持たない）。Pass2 で 1 回読み直し、失敗時は Pass1 と同じ
        # fail-closed（blocked flag・部分グラフを確定しない）で扱う。
        try:
            # Pass1 と同じサイズ上限（Pass1〜Pass2 の間に原本側で肥大したファイルを全量読まない）。
            if rp.stat().st_size > text_kind.MAX_BYTES:
                flags.append({"doc": rel, "reason": "changed_between_passes", "action": "blocked"})
                continue
            text, _raw = corpus_docs.read_full_text_and_raw(rp)
        except OSError:
            flags.append({"doc": rel, "reason": "unreadable_code_file", "action": "blocked"})
            continue
        if hashlib.sha1(_raw).hexdigest() != raw_sha1:
            # Pass1 と Pass2 の間に原本が書き換わった場合は、定義と参照を混ぜず blocked にする（次回 sync で全再構築）
            flags.append({"doc": rel, "reason": "changed_between_passes", "action": "blocked"})
            continue
        ref_result = analyzer.extract_refs(text, rel)
        plugin_ambiguities: list = []
        if rel in fw_ctx and fw_ctx[rel][0]:
            # プラグインへ渡す `file_context` は、世代内の全ファイルへ効く import（`global using` など）を足した後のもの
            ref_result = RefResult(refs=ref_result.refs, dropped=ref_result.dropped,
                                   file_context=_with_global_imports(analyzer, rel, ref_result.file_context))              # FW プラグイン: 本体の参照へ追加分を足す（失敗は本体の結果を残して申告）
            ref_result, pfails, plugin_ambiguities = analyzer_registry.apply_fw_refs(
                fw_ctx[rel][0], text, rel, fw_ctx[rel][1], ref_result, type_relations, fw_build_ctx)
            _note_plugin_failures(rel, pfails)
        _flag_dropped(analyzer.name, rel, ref_result.dropped)
        file_context = _with_global_imports(analyzer, rel, ref_result.file_context)
        if file_context is None and getattr(analyzer, "requires_file_context", False):
            flags.append({"reason": "file_context_missing", "analyzer": analyzer.name, "from": rel})
        name_pair = rel_name.get(rel)                     # 主体を持たないファイルは src が無い
        if name_pair is None:                             # dropped は既に記録済み・参照エッジは張れない＝読まなかったこと自体を申告する
            if ref_result.refs:
                lines = sorted(r.line for r in ref_result.refs)
                nopd = {"reason": "no_primary_definition", "analyzer": analyzer.name, "from": rel,
                        "count": len(lines), "line": lines[0], "lines": lines[:NO_PRIMARY_LINES_MAX]}
                if len(lines) > NO_PRIMARY_LINES_MAX:
                    nopd["lines_omitted"] = len(lines) - NO_PRIMARY_LINES_MAX
                flags.append(nopd)
            continue
        label, name = name_pair
        file_src = _cid(label, world_id, rel, name)
        for amb in plugin_ambiguities:                    # FW プラグインが 1 つに決められなかった参照（任意に選ばず、辺を張らずに申告する）
            amb_def = {"file": rel, "key": None}
            if amb.source_symbol_id is not None and amb.source_symbol_id[0] == rel and amb.source_symbol_id in symbol_cids:
                amb_def["key"] = amb.source_symbol_id[1]
            amb_why = {"why": amb.why} if amb.why else {}
            flags.append({"reason": "ambiguous", "from": rel, "kind": amb.kind, "name": amb.name, "line": amb.line,
                          "via": amb.via, "candidates": len(amb.candidates), **amb_why})
            unresolved_by_rel.setdefault(rel, []).append({
                "line": amb.line, "reason": "ambiguous", "kind": amb.kind, "name": str(amb.name), "via": amb.via,
                "from_def": amb_def, "candidates": len(amb.candidates),
                "candidate_paths": sorted({c.path for c in amb.candidates})[:5], **amb_why})
            unresolved_subject[rel] = file_src
        for ref in ref_result.refs:
            if ref.edge_type not in analyzer_registry.EDGE_TYPES:
                flags.append({"reason": "unknown_edge_type", "analyzer": analyzer.name,
                              "from": rel, "edge_type": ref.edge_type})
                continue
            if ref.kind not in analyzer_registry.NODE_LABELS:
                flags.append({"reason": "unknown_label", "analyzer": analyzer.name,
                              "from": rel, "label": ref.kind})
                continue
            src = file_src                                # 既定の始点＝ファイルの主体
            from_def = {"file": rel, "key": None}         # 申告の `from_def`＝参照元の定義（主体は key=None）
            if ref.source_symbol_id is not None:
                src = symbol_cids.get(ref.source_symbol_id) if ref.source_symbol_id[0] == rel else None
                if src is None:                           # 自ファイルに無い定義を指した＝主体へ倒し、申告する
                    flags.append({"reason": "unknown_source_symbol", "analyzer": analyzer.name, "from": rel,
                                  "key": ref.source_symbol_id[1], "line": ref.line})
                    src = file_src
                else:
                    from_def = {"file": rel, "key": ref.source_symbol_id[1]}
            n_flags = len(flags)
            _link(ref.edge_type, src, ref.kind, ref.name, rel, ref.line,
                 analyzer_name=analyzer.name, extra=ref.extra, reverse=ref.reverse,
                 file_context=file_context, from_def=from_def)
            for f in flags[n_flags:]:
                if not _is_unresolved_flag(f, rel):
                    continue
                f["from_def"] = from_def
                if f["reason"] in ("ambiguous", "config_import_ambiguous"):
                    f["candidates"] = _ambiguous_count(f, rel, analyzer.name) or None   # None＝数えられなかった（0 ではない）
                item = {"line": f["line"], "reason": f["reason"], "kind": f["kind"], "name": str(f["name"]),
                        "via": f.get("via"), "from_def": from_def}
                if "candidates" in f:
                    item["candidates"] = f["candidates"]
                unresolved_by_rel.setdefault(rel, []).append(item)
                unresolved_subject[rel] = file_src

    # 同一 (src,type,dst) の複数候補を 1 本へ集約してから確定する（全 Pass2 エッジに適用・nodes/flags は不変）
    edges.extend(_aggregate_pass2_edges(link_edges))

    # COPY のファイルをまたぐ循環（辺は残す）。取り込み記録（flags）だけに出す＝保存先のノードが 1 つに決まらない
    for cycle in _find_copy_cycles(nodes, edges):
        flags.append({"reason": "copy_cycle", "paths": cycle})

    for pname in sorted(plugin_failed):                   # FW プラグインの失敗（グラフはそのプラグインの分だけ欠ける）
        rec = plugin_failed[pname]
        flags.append({"reason": "plugin_failed", "action": "warn", "plugin": pname, "from": rec["from"],
                      "files": len(rec["files"]), "why": rec["why"]})

    # 未解決の申告を、参照を書いたファイルの主体ノードへ載せる（`world_neo4j.load_world` が同じ tx で保存する）
    for rel, items in unresolved_by_rel.items():
        items.sort(key=lambda it: (it["line"], it["reason"], it["kind"], it["name"]))
        node = nodes[unresolved_subject[rel]]
        # 上限で切るとき、プロジェクトの中に候補がありうる理由（ambiguous・cross_scope・unresolved_qualifier ほか）を先に残し、素の unresolved を後にする
        kept = (sorted(items, key=lambda it: (it["reason"] == "unresolved", it["reason"], it["line"], it["kind"], it["name"]))
                [:UNRESOLVED_PER_FILE_MAX] if len(items) > UNRESOLVED_PER_FILE_MAX else items)
        node["unresolved"] = sorted(kept, key=lambda it: (it["line"], it["reason"], it["kind"], it["name"]))
        node["unresolved_names"] = sorted({nm for it in items
                                           for nm in (it["name"], it["name"].rsplit(".", 1)[-1])})
        if len(items) > UNRESOLVED_PER_FILE_MAX:
            node["unresolved_overflow_count"] = len(items) - UNRESOLVED_PER_FILE_MAX

    # Pass2 完了直後にコード本文を解放する（Pass3 は資料文書の本文を別経路で読み直す）
    texts.clear()

    # Pass3: 辞書突合→言及エッジ
    _mention_pass(world_dir, world_id, defs, mention_aliases, files, nodes, edges, flags)

    return list(nodes.values()), edges, flags


def _lstat_kind(p) -> str | None:
    """`os.lstat()` ベースで種別を返す（`"dir"`/`"file"`/`"symlink"`/`None`＝不在扱い）。

    `Path.is_dir()` 等は `OSError` を握るため使わない。`OSError` は種別不明＝`None` にする。
    """
    try:
        st = os.lstat(p)
    except OSError:
        return None
    if stat_mod.S_ISLNK(st.st_mode):
        return "symlink"
    if stat_mod.S_ISDIR(st.st_mode):
        return "dir"
    if stat_mod.S_ISREG(st.st_mode):
        return "file"
    return None


def valid_rel_parts(rel: str) -> tuple[str, ...] | None:
    """`rel`（資料フォルダ root 相対 POSIX パス）の文字列検証だけを行う（FS アクセス無し）。

    絶対パス・`\\`・NUL・空/`.`/`..` 要素はすべて拒否し、通れば `"/"` 区切りのセグメント列を返す。
    `resolve_path` と `ext_api._doc_path_segments`（原本 DL 配信）が、正規化の真実源としてこの 1 関数だけを共有する。
    """
    if not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        return None
    parts = tuple(rel.split("/"))
    if any(p in ("", ".", "..") for p in parts):
        return None
    return parts


def resolve_path(world_dir, rel: str):
    """`rel`（資料フォルダ root 相対 POSIX）→ 原本 Path（パス基準・無ければ None）。

    ① `rel` を `valid_rel_parts` で検証する（FS アクセスより前）。
    ② root から `rel` の各階層へ直接 `os.lstat` して降りる（資料フォルダ全体は走査しない）。途中経路に symlink があれば拒否する。
    ③ 解決後パスが root 配下に収まることを再確認する（脱出防止）。
    """
    parts = valid_rel_parts(rel)
    if parts is None:
        return None
    root = Path(world_dir)
    if _lstat_kind(root) != "dir":
        return None
    cur = root
    for i, part in enumerate(parts):
        cur = cur / part
        kind = _lstat_kind(cur)
        if i == len(parts) - 1:
            if kind != "file":
                return None
        elif kind != "dir":
            return None
    try:
        rp = cur.resolve()
        rootr = root.resolve()
    except OSError:
        return None
    if not rp.is_relative_to(rootr):
        return None
    return rp
