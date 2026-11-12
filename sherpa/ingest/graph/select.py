"""候補の選び方。参照を定義へ解くとき、同じトップフォルダに絞り、木の距離で並べて最近傍を選ぶ規則を全部ここに集める。

設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

import posixpath
from collections import defaultdict

from .. import resolve_settings
from ..analyzers import TypeCandidate, TypeLookup, TypeRelations
from .periods_select import UNDETERMINED, distance_of, scoped


def tree_distance(a: str, b: str) -> int:
    """2つの rel_path の木距離（共通祖先までの上り＋下り）。同フォルダ=0。"""
    da, db = a.split("/")[:-1], b.split("/")[:-1]
    c = 0
    for x, y in zip(da, db):
        if x != y:
            break
        c += 1
    return (len(da) - c) + (len(db) - c)


def top_folder(rel: str):
    return rel.split("/", 1)[0] if "/" in rel else None


def resolve_nearest(defs, kind, name, ref_rel, ps=None):
    """`defs[(kind,name)]` の候補 rel から ref_rel に最も近いものを返す。

    同一 top_scope（世代）に限定してから最近傍を選ぶ。戻りは `(rel | None, status)`。
    `status` は `''`(解決) / `'ambiguous'`(同距離複数) / `'cross_scope'`(同世代に定義無し) / 期の規則の `'parent_period_undetermined'`。
    `ps`（`PeriodSelect`）があり参照元が期の中なら、候補を同じ期→親の期の連鎖→期の外の順に絞る。
    """
    cands = defs.get((kind, name))
    if not cands:
        return None, "unresolved"
    same, why = scoped(ps, cands, ref_rel)                                   # 構造リンクは同 top_scope 内のみ
    if why:
        return None, why
    if not same:
        return None, "cross_scope"                              # 別世代にしか無い＝引かない（誤検出防止）
    dist = distance_of(ps)
    ranked = sorted(same, key=lambda r: dist(ref_rel, r))
    best = dist(ref_rel, ranked[0])
    if sum(1 for r in same if dist(ref_rel, r) == best) > 1:
        return None, "ambiguous"                                # 同距離複数は任意選択しない
    return ranked[0], ""


def resolve_table(table_defs, name, ref_rel, ps=None):
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
    same, why = scoped(ps, cands, ref_rel, key=lambda c: c[0])
    if why:
        return None, None, why
    if not same:
        return None, None, "cross_scope"
    if schema is not None:
        exact = [c for c in same if c[2] == schema]
        same = exact or [c for c in same if c[2] is None]
        if not same:
            return None, None, "unresolved"
    elif len({c[2] for c in same}) > 1:
        return None, None, "ambiguous"
    dist = distance_of(ps)
    ranked = sorted(same, key=lambda c: dist(ref_rel, c[0]))
    best = dist(ref_rel, ranked[0][0])
    if sum(1 for c in same if dist(ref_rel, c[0]) == best) > 1:
        return None, None, "ambiguous"
    return ranked[0][0], ranked[0][1], ""



def resolve_qualified(qualified_defs, kind, name, ref_rel, ps=None):
    """完全修飾名の解決: `qualified_defs[(kind,name)]`（`[(rel,実名), ...]`）を同一 top_scope に絞り、パス距離で最近傍を選ぶ。

    同じ完全修飾名の候補が複数あるときだけ、パス距離で並べ替える（別の名前を選ぶ根拠にはしない）。最短距離が複数なら任意選択せず `'ambiguous'`。
    戻りは `(rel|None, 実名|None, status)`。`status` は `''` / `'ambiguous'` / `'cross_scope'` / `'unresolved'`。
    見つからなくても単純名へは倒さない（呼び出し側が未解決として `flags` に申告する）。
    """
    cands = qualified_defs.get((kind, name))
    if not cands:
        return None, None, "unresolved"
    same, why = scoped(ps, cands, ref_rel, key=lambda c: c[0])
    if why:
        return None, None, why
    if not same:
        return None, None, "cross_scope"
    owner = getattr(qualified_defs, "project_of", None)
    mine = owner.get(ref_rel) if owner else None
    if mine is not None and len(same) > 1:           # 同じ完全修飾名が複数のプロジェクトにあれば、参照元と同じプロジェクトの定義を先にする
        own = [pair for pair in same if owner.get(pair[0]) == mine]
        same = own or same
    dist = distance_of(ps)
    ranked = sorted(same, key=lambda pair: dist(ref_rel, pair[0]))
    best = dist(ref_rel, ranked[0][0])
    if sum(1 for r, _ in same if dist(ref_rel, r) == best) > 1:
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


def _resolve_wildcards(qualified_defs, kind, name, ref_rel, file_context, ps=None):
    """ワイルドカード import（`import p.*`・C# の `using N;`・VB の `Imports N`）の中で `name` が一意に決まるかを返す。

    `static` の import は型名を持ち込まないので使わない。同じ型が複数の import 先で一致したら任意選択せず `'ambiguous'`。
    戻りは `(rel|None, 実名|None, status)`（`status` は `''` / `'ambiguous'` / `'unresolved'`）。
    """
    hits: set = set()
    for imp in file_context.imports:
        if imp.kind != "wildcard" or imp.static:
            continue
        rel, actual, status = resolve_qualified(qualified_defs, kind, _join_fqn(imp.name, name), ref_rel, ps)
        if status in ("ambiguous", UNDETERMINED):
            return None, None, status
        if not status:
            hits.add((rel, actual))
    if len(hits) == 1:
        rel, actual = next(iter(hits))
        return rel, actual, ""
    return (None, None, "ambiguous") if hits else (None, None, "unresolved")


def resolve_type_name(qualified_defs, defs, kind, name, ref_rel, file_context, parents: bool, package,
                       rule_out: list | None = None, ps=None):
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
        rel, actual, status = resolve_qualified(qualified_defs, kind, imp.name, ref_rel, ps)
        if status == "unresolved":
            status = "unresolved_qualifier"
        if rule_out is not None:
            rule_out.append("alias" if imp.kind == "alias" else "single_import")
        return rel, actual, status, imp.name
    for prefix in _namespace_chain(package, parents):
        rel, actual, status = resolve_qualified(qualified_defs, kind, _join_fqn(prefix, name), ref_rel, ps)
        if status in ("", "ambiguous", UNDETERMINED):
            if rule_out is not None:
                rule_out.append("same_package")
            return rel, actual, status, name
    rel, actual, status = _resolve_wildcards(qualified_defs, kind, name, ref_rel, file_context, ps)
    if status in ("", "ambiguous", UNDETERMINED):
        if rule_out is not None:
            rule_out.append("wildcard")
        return rel, actual, status, name
    others = defs.get((kind, name))
    return None, None, ("cross_scope" if others and not any(top_folder(r) == top_folder(ref_rel) for r in others)
                        else "unresolved"), name


def resolve_dotted_type_name(qualified_defs, kind, name, ref_rel, file_context, parents: bool, package,
                              absolute: bool = False, ps=None):
    """C#・VB.NET の修飾付きの型名（`A.B.Type`・`Outer.Inner`）の解決。入れ子の型は外側の型へ寄せる。

    名前の長い接頭辞から順に、参照元の enclosing namespace（内側→グローバル）の下で完全修飾名として探す。戻りは `resolve_qualified` と同じ。
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
            rel, actual, status = resolve_qualified(qualified_defs, kind, _join_fqn(ns, prefix), ref_rel, ps)
            if status in ("", "ambiguous", UNDETERMINED):
                return rel, actual, status
    return None, None, "unresolved"


def _qualified_all(qualified_defs, kind, name, ref_rel, ps=None) -> list:
    """`resolve_qualified` が曖昧にした候補も含めて返す（同一 top_scope で最短距離の `(rel, 実名, 完全修飾名)` 全部・昇順）。"""
    same, _why = scoped(ps, qualified_defs.get((kind, name), []), ref_rel, key=lambda c: c[0])
    if not same:
        return []
    dist = distance_of(ps)
    best = min(dist(ref_rel, r) for r, _ in same)
    return sorted({(r, nm, name) for r, nm in same if dist(ref_rel, r) == best})


def _dotted_all(qualified_defs, kind, name, ref_rel, file_context, parents: bool, ps=None) -> list:
    """`resolve_dotted_type_name` と同じ探し方で、曖昧にした候補も含めて返す。"""
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
            cands = _qualified_all(qualified_defs, kind, _join_fqn(ns, prefix), ref_rel, ps)
            if cands:
                return cands
    return []


def type_candidates(qualified_defs, defs, kind, name, ref_rel, file_context, parents: bool, *, qualified: bool = False, ps=None):
    """型名 `name` の解決先の候補を、`resolve_type_name` と同じ順序・規則で全部返す（曖昧なら複数のまま）。戻りは `([(rel, 実名, 完全修飾名)], status)`。

    `status` は `''`（1 件に決まった）／`'ambiguous'`／`'cross_scope'`／`'unresolved'`。`file_context` が無いときは同名の最近傍（`resolve_nearest` と同じ）。
    """
    def done(cands):
        return cands, ("" if len(cands) == 1 else "ambiguous")

    if "." in name and (qualified or file_context is None):   # 修飾のある名前は `_link` の完全修飾の入口と同じ規則（単純名へ倒さない）
        if parents and file_context is not None:              # C#・VB.NET＝入れ子の型・別名を `resolve_dotted_type_name` と同じに
            cands = _dotted_all(qualified_defs, kind, name, ref_rel, file_context, parents, ps)
        else:
            cands = _qualified_all(qualified_defs, kind, name, ref_rel, ps)
        return (cands, "" if len(cands) == 1 else "ambiguous") if cands else ([], "unresolved")
    if file_context is None:
        same, _why = scoped(ps, defs.get((kind, name), []), ref_rel)
        if not same:
            return [], ("cross_scope" if defs.get((kind, name)) else "unresolved")
        dist = distance_of(ps)
        best = min(dist(ref_rel, r) for r in same)
        return done(sorted({(r, name, name) for r in same if dist(ref_rel, r) == best}))
    for imp in file_context.imports:
        if imp.static or imp.kind == "wildcard":
            continue
        if (imp.kind == "alias" and imp.alias == name) or (imp.kind == "single" and imp.name.rsplit(".", 1)[-1] == name):
            cands = _qualified_all(qualified_defs, kind, imp.name, ref_rel, ps)
            return done(cands) if cands else ([], "unresolved")
    for prefix in _namespace_chain(file_context.package, parents):
        cands = _qualified_all(qualified_defs, kind, _join_fqn(prefix, name), ref_rel, ps)
        if cands:
            return done(cands)
    hits: set = set()
    for imp in file_context.imports:
        if imp.kind == "wildcard" and not imp.static:
            hits.update(_qualified_all(qualified_defs, kind, _join_fqn(imp.name, name), ref_rel, ps))
    if hits:
        return done(sorted(hits))
    others = defs.get((kind, name))
    return [], ("cross_scope" if others and not any(top_folder(r) == top_folder(ref_rel) for r in others) else "unresolved")


class TypeRelationsImpl(TypeRelations):
    """`TypeRelations` の実装。`records`＝全ファイルの継承・実装の宣言（本体のアナライザの参照のうち `via` が `extends`／`implements`）。

    宣言ごとの継承元の解決は `type_candidates`（共通層の規則）で行い、引くたびに計算せず最初の問い合わせで索引にする。
    """

    def __init__(self, qualified_defs, defs, records, parents_of, ps=None):
        self._qd, self._defs, self._records, self._parents_of = qualified_defs, defs, records, parents_of
        self._ps = ps
        self._by_target: dict | None = None

    @staticmethod
    def _candidate(rel, actual, qkey, exact=True) -> TypeCandidate:
        return TypeCandidate(path=rel, name=actual, qualified=qkey or actual, exact=exact)

    def _index(self) -> dict:
        if self._by_target is None:
            idx: dict = {}
            for r in self._records:
                targets, _status = type_candidates(self._qd, self._defs, r["kind"], r["name"], r["rel"], r["ctx"],
                                                    self._parents_of(r["rel"]), qualified=r["qualified"], ps=self._ps)
                for t in targets:
                    idx.setdefault((r["kind"], *t), []).append((r, len(targets) == 1))
            self._by_target = idx
        return self._by_target

    def subtypes(self, type_name, from_rel, file_context=None, line=None, *, kind="Module") -> TypeLookup:
        ctx = file_context.at(line) if (file_context is not None and line is not None) else file_context
        targets, status = type_candidates(self._qd, self._defs, kind, type_name, from_rel, ctx, self._parents_of(from_rel),
                                           qualified="." in type_name, ps=self._ps)
        subs: dict = {}   # (rel, 実名, 完全修飾名) -> 宣言の解決が曖昧でなかったか
        for t in targets:
            for rec, exact in self._index().get((kind, *t), []):
                key = (rec["rel"], rec["actual"], rec["qkey"] or "")
                subs[key] = exact and subs.get(key, True)
        return TypeLookup(
            status="resolved" if status == "" else status,
            targets=tuple(self._candidate(*t) for t in targets),
            subtypes=tuple(self._candidate(k[0], k[1], k[2], subs[k]) for k in sorted(subs)))


def resolve_nearest_keyed(index, analyzer_name, kind, name, ref_rel, ps=None):
    """単純名での children 解決（手続き型言語の関数呼び出し解決共通）。

    `index[(analyzer_name,kind,name)]`（`[(rel, 実際の cid_key, c_kind|None), ...]`）を同一 top_scope に絞り、
    パス距離で最近傍を選ぶ。索引キーに `analyzer_name` を含め、他言語の同名と誤接続しない。
    `c_kind == "definition"` の候補があれば `"declaration"` を外して定義側だけで決める（無ければ全候補）。
    同距離複数は任意選択せず `'ambiguous'`。戻りは `(rel|None, 実キー|None, status)`。
    """
    cands = index.get((analyzer_name, kind, name))
    if not cands:
        return None, None, "unresolved"
    same, why = scoped(ps, cands, ref_rel, key=lambda c: c[0])
    if why:
        return None, None, why
    if not same:
        return None, None, "cross_scope"
    definitions = [(r, k) for r, k, ck in same if ck == "definition"]
    pool = definitions if definitions else [(r, k) for r, k, _ck in same]
    dist = distance_of(ps)
    ranked = sorted(pool, key=lambda pair: dist(ref_rel, pair[0]))
    best = dist(ref_rel, ranked[0][0])
    nearest = [(r, k) for r, k in pool if dist(ref_rel, r) == best]
    if len(nearest) > 1:
        return None, None, "ambiguous"
    rel, actual_key = nearest[0]
    return rel, actual_key, ""


def resolve_include_relpath(rel_name, ref_rel, include_path, kind, name):
    """C の `#include "path"` の 1 段目解決: パス区切りを含む `include_path` を参照元 `ref_rel` からの相対パスとして解決する。

    その rel_path が実際に `name`（拡張子込みファイル名）の主体であれば一意に採用し、距離計算・曖昧判定は経由しない。
    同一 top_scope を跨ぐ解決はしない。見つからなければ `None`（`path_exact` の参照は未解決として申告する。それ以外は呼び出し側が basename の最近傍へ倒す）。
    区切りは `normpath` の前に `\\`→`/` へ直す。
    """
    base_dir = ref_rel.rsplit("/", 1)[0] if "/" in ref_rel else ""
    joined = f"{base_dir}/{include_path}" if base_dir else include_path
    candidate = posixpath.normpath(joined.replace("\\", "/"))
    if top_folder(candidate) != top_folder(ref_rel):
        return None
    return candidate if rel_name.get(candidate) == (kind, name) else None


def resolve_by_copy_paths(defs, kind, name, ref_rel, copy_paths):
    """最近傍で決まらない COPY を、設定の `copy_paths` を先頭から試して決める。最初に一意に決まった prefix を採用する。

    参照元と同じ最上位フォルダ（世代）の prefix の下にある定義だけを候補にする。1 つの prefix の下に複数あれば、その prefix は決まらない扱いで次へ進む。
    戻りは `rel | None`。
    """
    cands = sorted(set(defs.get((kind, name), [])))
    top = top_folder(ref_rel)
    for prefix in copy_paths:
        if prefix.split("/", 1)[0] != top:
            continue
        hits = [r for r in cands if r.startswith(prefix + "/") and top_folder(r) == top]
        if len(hits) == 1:
            return hits[0]
    return None


def resolve_include_alias(rel_name, aliases, include_path, ref_rel, kind, name):
    """`include_path` の先頭が設定の別名なら prefix に置き換え、同じ最上位フォルダの実在の定義に一致するときだけ `rel` を返す。"""
    expanded = resolve_settings.expand_alias(include_path, aliases)
    if expanded is None:
        return None
    candidate = posixpath.normpath(expanded)
    if top_folder(candidate) != top_folder(ref_rel):
        return None
    return candidate if rel_name.get(candidate) == (kind, name) else None


def build_path_suffix_index(rel_name):
    """`resolve_path_suffix` 用の事前索引 `(label, top_scope, 末尾パス) -> [rel, ...]` を Pass1 完了直後に 1 回だけ構築する。

    1 つの rel_path が持つ末尾パスは、パス区切り単位の末尾（`"c.xml"`／`"b/c.xml"`／`"a/b/c.xml"`）の深さの数だけ。
    """
    index: dict = defaultdict(list)
    for rel, (label, _name) in rel_name.items():
        top = top_folder(rel)
        parts = rel.split("/")
        for i in range(len(parts)):
            index[(label, top, "/".join(parts[i:]))].append(rel)
    return index


def resolve_path_suffix(suffix_index, kind, suffix, ref_rel, ps=None):
    """パス末尾一致での解決（Spring `<import resource>` 等の classpath 相対パス参照）。

    classpath 相対の指定は参照元ファイルの位置と無関係のため、資料フォルダ内の実ファイルの rel_path が
    指定パス（`suffix`）と末尾一致する形でしか解決しない（basename だけの最近傍解決はしない）。
    `suffix_index` から同一 top_scope 内の候補を引き、1 件に決まらなければ接続しない。
    戻りは `(rel|None, status)`。`status` は `''`(解決) / `'ambiguous'`(2 件以上) / `'unresolved'`(0 件)。
    """
    matches = suffix_index.get((kind, top_folder(ref_rel), suffix), [])
    if not matches:
        return None, "unresolved"
    matches, why = scoped(ps, matches, ref_rel)
    if why:
        return None, why
    if not matches:
        return None, "unresolved"
    if len(matches) > 1:
        return None, "ambiguous"
    return matches[0], ""


def count_nearest(rels, ref_rel, ps=None) -> int:
    """同一 top_scope の候補 rel のうち、`ref_rel` から最短距離にある数（`_resolve_*` が曖昧にした候補数と同じ数え方）。"""
    same, _why = scoped(ps, rels, ref_rel)
    if not same:
        return 0
    dist = distance_of(ps)
    best = min(dist(ref_rel, r) for r in same)
    return sum(1 for r in same if dist(ref_rel, r) == best)


def config_key_candidates(config_key_index, kind, key_kind, name, ref_rel, ps=None) -> tuple[list, list, str]:
    """`via=config_key` の候補。戻りは `(索引の全候補, そのうち参照元と同じトップフォルダのもの, status)`（候補は `[(rel, cid_key), ...]`・
    `status` は `''` か期の規則の `'parent_period_undetermined'`）。"""
    cands = list(config_key_index.get((kind, key_kind, name), []))
    same, status = scoped(ps, cands, ref_rel, key=lambda c: c[0])
    return cands, same, status


def ambiguous_count(b, f, ref_rel, analyzer_name):
    """曖昧にした候補の数（`flags` の `candidates`）。判定した索引（修飾名・単純名・表・手続き型の単純名・パス末尾）を引き直して数える。"""
    kind, name, ps = f["kind"], f["name"], b.period_select
    if f.get("reason") == "config_import_ambiguous":
        return len(scoped(ps, b.path_suffix_index.get((kind, top_folder(ref_rel), name), []), ref_rel)[0])
    if kind == "Table":
        same = scoped(ps, b.table_defs.get(getattr(name, "simple", name), []), ref_rel, key=lambda c: c[0])[0]
        schema = getattr(name, "schema", None)
        if schema is not None:
            same = [c for c in same if c[2] == schema] or [c for c in same if c[2] is None]
        elif len({c[2] for c in same}) > 1:
            return len(same)
        return count_nearest([c[0] for c in same], ref_rel, ps)
    pools = [[r for r, _n in b.qualified_defs.get((kind, name), [])],
             list(b.defs.get((kind, name), []))]
    keyed = [(r, ck) for r, _k, ck in scoped(ps, b.simple_name_defs.get((analyzer_name, kind, name), []), ref_rel,
                                             key=lambda c: c[0])[0]]
    pools.append([r for r, ck in keyed if ck == "definition"] or [r for r, _ck in keyed])
    for pool in pools:
        n = count_nearest(pool, ref_rel, ps)
        if n > 1:
            return n
    return None
