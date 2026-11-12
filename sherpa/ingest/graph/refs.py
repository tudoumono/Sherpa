"""Pass 2: 定義を集め終えたファイルを読み直して参照を取り出し、1 件ずつ `link` で解く。未解決・曖昧の申告とデータの流れの入力もここで集める。

設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

import hashlib

from ... import corpus_docs
from .. import build_link, text_kind
from ..analyzers import FileContext, RefResult, vb_project
from ..analyzers import registry as analyzer_registry
from .aggregate import NO_PRIMARY_LINES_MAX, is_unresolved_flag
from .analyze import flag_dropped, note_plugin_failures
from .link import link
from .periods_select import UNDETERMINED, undetermined_candidates
from .select import TypeRelationsImpl, ambiguous_count, top_folder
from .state import WorldBuild, make_cid


def with_global_imports(b: WorldBuild, analyzer, rel, file_context):
    """`file_context` へ、同じ世代・同じ言語の全ファイルへ効く import（`global using` など）と、VB.NET のプロジェクト設定
    （`.vbproj` の Import・Root Namespace）を足す。本体の解決・FW プラグイン・型の関係の段が同じ文脈を使う。"""
    if file_context is None:
        return file_context
    if b.global_imports.get((analyzer.name, top_folder(rel))):
        def _key(i):
            return (i.kind, i.name, i.alias, i.static, i.scope, i.is_global)   # 有効範囲が違う同名の import は別物（namespace 内の `using` と `global using`）
        seen = {_key(i) for i in file_context.imports}
        extra_imports = [i for i in b.global_imports[(analyzer.name, top_folder(rel))] if _key(i) not in seen]
        file_context = FileContext(package=file_context.package, imports=file_context.imports + extra_imports,
                                   namespaces=file_context.namespaces)
    proj = b.vb_bound.get(rel)
    if proj is not None:
        file_context = vb_project.apply_project(file_context, proj)
    return file_context


def build_type_relations(b: WorldBuild) -> None:
    """型の継承・実装の関係を使う FW プラグインがあるときだけ、全ファイルの継承・実装の宣言（本体の参照のうち `via` が `extends`／`implements`）を
    集めて、プラグインへ渡す読み取り専用の口（`TypeRelations`）を `b.type_relations` に作る。宣言の継承元の解決は `type_candidates`（`link` と同じ規則）。
    """
    type_langs = {lang for plugins, _base in b.fw_ctx.values() for p in plugins if p.uses_type_relations for lang in p.languages}
    if not type_langs:
        return
    # 継承・実装は同じ言語の中で引く前提＝型の関係を使うプラグインの `languages` のアナライザのファイルだけ集める
    type_records: list = []
    for t_rel, (t_rp, t_analyzer, t_sha) in b.texts.items():
        if t_analyzer.name not in type_langs:
            continue
        try:
            if t_rp.stat().st_size > text_kind.MAX_BYTES:
                b.flags.append({"doc": t_rel, "reason": "changed_between_passes", "action": "blocked"})
                continue
            t_text, t_raw = corpus_docs.read_full_text_and_raw(t_rp)
        except OSError:
            b.flags.append({"doc": t_rel, "reason": "unreadable_code_file", "action": "blocked"})   # 型の候補が欠けたグラフを確定させない
            continue
        if hashlib.sha1(t_raw).hexdigest() != t_sha:
            b.flags.append({"doc": t_rel, "reason": "changed_between_passes", "action": "blocked"})
            continue
        if t_rel not in b.rel_name:
            continue
        t_res = t_analyzer.extract_refs(t_text, t_rel)
        t_ctx = with_global_imports(b, t_analyzer, t_rel, t_res.file_context)
        for r in t_res.refs:
            ex = r.extra or {}
            if ex.get("via") not in ("extends", "implements") or r.reverse:
                continue
            sub_actual, sub_qkey = b.rel_name[t_rel][1], b.def_qkey.get((t_rel, None))
            if r.source_symbol_id is not None and r.source_symbol_id[0] == t_rel and r.source_symbol_id in b.symbol_cids:
                sub_actual, sub_qkey = r.source_symbol_id[1], b.def_qkey.get(r.source_symbol_id)
            type_records.append({
                "rel": t_rel, "qkey": sub_qkey, "actual": sub_actual, "kind": r.kind, "name": r.name,
                "ctx": t_ctx.at(r.line) if t_ctx is not None else None, "qualified": bool(ex.get("qualified"))})
    b.type_relations = TypeRelationsImpl(
        b.qualified_defs, b.defs, type_records,
        lambda rel: bool(b.texts[rel][1].resolves_parent_namespaces) if rel in b.texts else False,
        b.period_select)


def resolve_references(b: WorldBuild, step) -> None:
    """② 集めたファイルごとに参照を取り出して解き、解決済みの候補を `b.link_edges` に積む。`step` は進み具合の通知。"""
    b.item_names = {n["name"] for n in b.nodes.values() if n["label"] == "DataItem"}
    for n_seen, (rel, (rp, analyzer, raw_sha1)) in enumerate(b.texts.items()):
        step.tick(n_seen)
        _resolve_file(b, rel, rp, analyzer, raw_sha1)


def _read_again(b: WorldBuild, rel, rp, raw_sha1):
    """Pass1 は本文を保持しない（コード総量に比例したメモリを持たない）。Pass2 で 1 回読み直し、失敗時は Pass1 と同じ
    fail-closed（blocked flag・部分グラフを確定しない）で扱う。戻りは本文（読めなければ `None`）。"""
    try:
        # Pass1 と同じサイズ上限（Pass1〜Pass2 の間に原本側で肥大したファイルを全量読まない）。
        if rp.stat().st_size > text_kind.MAX_BYTES:
            b.flags.append({"doc": rel, "reason": "changed_between_passes", "action": "blocked"})
            return None
        text, _raw = corpus_docs.read_full_text_and_raw(rp)
    except OSError:
        b.flags.append({"doc": rel, "reason": "unreadable_code_file", "action": "blocked"})
        return None
    if hashlib.sha1(_raw).hexdigest() != raw_sha1:
        # Pass1 と Pass2 の間に原本が書き換わった場合は、定義と参照を混ぜず blocked にする（次回 sync で全再構築）
        b.flags.append({"doc": rel, "reason": "changed_between_passes", "action": "blocked"})
        return None
    return text


def _extract_refs(b: WorldBuild, rel, text, analyzer):
    """アナライザの `extract_refs` に FW プラグインの追加分を足した参照。戻りは `(RefResult, プラグインが決められなかった参照)`。"""
    ref_result = analyzer.extract_refs(text, rel)
    plugin_ambiguities: list = []
    if rel in b.fw_ctx and b.fw_ctx[rel][0]:
        # プラグインへ渡す `file_context` は、世代内の全ファイルへ効く import（`global using` など）を足した後のもの
        ref_result = RefResult(refs=ref_result.refs, dropped=ref_result.dropped,
                               file_context=with_global_imports(b, analyzer, rel, ref_result.file_context),
                               file_io=ref_result.file_io, dd_bindings=ref_result.dd_bindings,
                               proc_names=ref_result.proc_names)              # FW プラグイン: 本体の参照へ追加分を足す（失敗は本体の結果を残して申告）
        ref_result, pfails, plugin_ambiguities = analyzer_registry.apply_fw_refs(
            b.fw_ctx[rel][0], text, rel, b.fw_ctx[rel][1], ref_result, b.type_relations, b.fw_build_ctx)
        note_plugin_failures(b, rel, pfails)
    return ref_result, plugin_ambiguities


def _resolve_file(b: WorldBuild, rel, rp, analyzer, raw_sha1) -> None:
    text = _read_again(b, rel, rp, raw_sha1)
    if text is None:
        return
    ref_result, plugin_ambiguities = _extract_refs(b, rel, text, analyzer)
    flag_dropped(b, analyzer.name, rel, ref_result.dropped)
    file_context = with_global_imports(b, analyzer, rel, ref_result.file_context)
    if file_context is None and getattr(analyzer, "requires_file_context", False):
        b.flags.append({"reason": "file_context_missing", "analyzer": analyzer.name, "from": rel})
    name_pair = b.rel_name.get(rel)                   # 主体を持たないファイルは src が無い
    if name_pair is None:                             # dropped は既に記録済み・参照エッジは張れない＝読まなかったこと自体を申告する
        if ref_result.refs:
            lines = sorted(r.line for r in ref_result.refs)
            nopd = {"reason": "no_primary_definition", "analyzer": analyzer.name, "from": rel,
                    "count": len(lines), "line": lines[0], "lines": lines[:NO_PRIMARY_LINES_MAX]}
            if len(lines) > NO_PRIMARY_LINES_MAX:
                nopd["lines_omitted"] = len(lines) - NO_PRIMARY_LINES_MAX
            b.flags.append(nopd)
        return
    label, name = name_pair
    file_src = make_cid(label, b.world_id, rel, name)
    _keep_flow_inputs(b, rel, file_src, ref_result)
    _note_plugin_ambiguities(b, rel, file_src, plugin_ambiguities)
    for ref in ref_result.refs:
        _link_ref(b, rel, analyzer, file_src, file_context, ref)


def _keep_flow_inputs(b: WorldBuild, rel, file_src, ref_result) -> None:
    """データの流れの段への入力（COBOL のファイル入出力・JCL の DD 文・手続き部に出るコピーブックの項目名）を取っておく。"""
    if ref_result.file_io:
        b.io_by_rel[rel] = (file_src, ref_result.file_io)
    if ref_result.dd_bindings:
        b.dd_by_rel[rel] = ref_result.dd_bindings
    if ref_result.proc_names and b.item_names:
        used = {nm: ln for nm, ln in ref_result.proc_names.items() if nm in b.item_names}
        if used:
            b.names_by_rel[rel] = (file_src, used)


def _note_plugin_ambiguities(b: WorldBuild, rel, file_src, plugin_ambiguities) -> None:
    for amb in plugin_ambiguities:                    # FW プラグインが 1 つに決められなかった参照（任意に選ばず、辺を張らずに申告する）
        amb_def = {"file": rel, "key": None}
        if amb.source_symbol_id is not None and amb.source_symbol_id[0] == rel and amb.source_symbol_id in b.symbol_cids:
            amb_def["key"] = amb.source_symbol_id[1]
        amb_why = {"why": amb.why} if amb.why else {}
        b.flags.append({"reason": "ambiguous", "from": rel, "kind": amb.kind, "name": amb.name, "line": amb.line,
                        "via": amb.via, "candidates": len(amb.candidates), **amb_why})
        b.unresolved_by_rel.setdefault(rel, []).append({
            "line": amb.line, "reason": "ambiguous", "kind": amb.kind, "name": str(amb.name), "via": amb.via,
            "from_def": amb_def, "candidates": len(amb.candidates),
            "candidate_paths": sorted({c.path for c in amb.candidates})[:5], **amb_why})
        b.unresolved_subject[rel] = file_src


def _link_ref(b: WorldBuild, rel, analyzer, file_src, file_context, ref) -> None:
    """参照 1 件を `link` で解く。解けずに積まれた申告は、参照元の定義とともに未解決の申告（`unresolved_by_rel`）へも写す。"""
    if ref.edge_type not in analyzer_registry.EDGE_TYPES:
        b.flags.append({"reason": "unknown_edge_type", "analyzer": analyzer.name,
                        "from": rel, "edge_type": ref.edge_type})
        return
    if ref.kind not in analyzer_registry.NODE_LABELS:
        b.flags.append({"reason": "unknown_label", "analyzer": analyzer.name,
                        "from": rel, "label": ref.kind})
        return
    src = file_src                                # 既定の始点＝ファイルの主体
    from_def = {"file": rel, "key": None}         # 申告の `from_def`＝参照元の定義（主体は key=None）
    if ref.source_symbol_id is not None:
        src = b.symbol_cids.get(ref.source_symbol_id) if ref.source_symbol_id[0] == rel else None
        if src is None:                           # 自ファイルに無い定義を指した＝主体へ倒し、申告する
            b.flags.append({"reason": "unknown_source_symbol", "analyzer": analyzer.name, "from": rel,
                            "key": ref.source_symbol_id[1], "line": ref.line})
            src = file_src
        else:
            from_def = {"file": rel, "key": ref.source_symbol_id[1]}
    n_flags, n_edges = len(b.flags), len(b.link_edges)
    link(b, ref.edge_type, src, ref.kind, ref.name, rel, ref.line,
         analyzer_name=analyzer.name, extra=ref.extra, reverse=ref.reverse,
         file_context=file_context, from_def=from_def)
    if (ref.edge_type == "INVOKES" and ref.kind == "Module" and not ref.reverse and len(b.link_edges) == n_edges + 1
            and len(b.flags) == n_flags and build_link.is_exec_call(analyzer.name, ref.extra)):
        b.exec_calls_resolved.append(build_link.Call(rel, src, str(ref.name), ref.line, (ref.extra or {}).get("via"),
                                                     from_def, {}, {}, b.link_edges[-1]))
    for f in b.flags[n_flags:]:
        if not is_unresolved_flag(f, rel):
            continue
        f["from_def"] = from_def
        if f["reason"] in ("ambiguous", "config_import_ambiguous"):
            f["candidates"] = ambiguous_count(b, f, rel, analyzer.name) or None   # None＝数えられなかった（0 ではない）
        if f["reason"] == UNDETERMINED:
            f["candidate_paths"] = undetermined_candidates(b, f, rel, analyzer.name)
        item = {"line": f["line"], "reason": f["reason"], "kind": f["kind"], "name": str(f["name"]),
                "via": f.get("via"), "from_def": from_def}
        if "candidate_paths" in f:
            item["candidate_paths"] = f["candidate_paths"]
        if "candidates" in f:
            item["candidates"] = f["candidates"]
        b.unresolved_by_rel.setdefault(rel, []).append(item)
        b.unresolved_subject[rel] = file_src
        if (ref.edge_type == "INVOKES" and ref.kind == "Module" and not ref.reverse
                and (f["reason"] in build_link.UNLINKED_REASONS or f["reason"] == UNDETERMINED) and build_link.is_exec_call(analyzer.name, ref.extra)):
            b.exec_calls.append(build_link.Call(rel, src, str(ref.name), ref.line, (ref.extra or {}).get("via"),
                                                from_def, f, item))
