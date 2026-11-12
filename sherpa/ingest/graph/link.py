"""Pass 2 の核: 参照 1 件を解いて構造エッジの候補にする。解決の規則ごとの分岐と、設定キーの特例をここから呼ぶ。

設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

from .. import resolve_settings
from ..analyzers import registry as analyzer_registry
from . import name_rules
from .aggregate import EDGE_RULES
from .analyze import analyzer_parents, analyzer_requires_context, simple_name_calls
from .select import (
    config_key_candidates, resolve_by_copy_paths, resolve_dotted_type_name, resolve_include_alias,
    resolve_include_relpath, resolve_nearest, resolve_nearest_keyed, resolve_path_suffix, resolve_qualified,
    resolve_table, resolve_type_name,
)
from .periods_select import UNDETERMINED, stamp_edge
from .state import WorldBuild, make_cid
from .undefined_tables import RULE as UNDEFINED_TABLE_RULE, VIAS as UNDEFINED_TABLE_VIAS, undefined_table


def apply_extra(b: WorldBuild, edge, etype, ref_rel, analyzer_name, extra):
    """`RefCandidate.extra` を解決後のエッジへ加算的に透過する。

    細分ラベル `via` は既知値（`KNOWN_VIA`）のみ通し、未知値は flags へ記録してその属性だけを落とす（エッジ自体は張る）。
    """
    if not extra:
        return
    via = extra.get("via")
    if via is not None and via not in analyzer_registry.KNOWN_VIA:
        b.flags.append({"reason": "unknown_via", "analyzer": analyzer_name,
                        "from": ref_rel, "edge_type": etype, "via": via})
        extra = {k: v for k, v in extra.items() if k != "via"}
    bad = set(extra) & edge.keys()                   # 共通層が確定した既存キーは上書きさせない
    if bad:
        b.flags.append({"reason": "reserved_key_in_extra", "analyzer": analyzer_name,
                        "from": ref_rel, "edge_type": etype, "keys": sorted(bad)})
    else:
        edge.update(extra)


def link_config_key_all(b: WorldBuild, etype, src_cid, kind, name, ref_rel, line, analyzer_name, extra, reverse, from_def):
    """`via=config_key` の参照だけ、同一 top_scope 内の同名 `Config` キー全件へ 1 本ずつエッジを張る特例。

    通常の最近傍/ambiguous 判定を迂回する（環境別設定ファイルが同名キーを持つ場合に両方へ張るため）。
    候補源は `config_key_index`（`"key:"` 接頭辞を持つ Config child だけ）で、primary（`defs`）は混ぜない。
    dst cid は一致した候補の `cid_key` で組み立てる。`key_kind` も索引キーに含める（無い参照は無い定義としか一致しない）。
    """
    cands, same, status = config_key_candidates(b.config_key_index, kind, extra.get("key_kind"), name, ref_rel, b.period_select)
    if not same:
        b.flags.append({"reason": status or ("cross_scope" if cands else "unresolved"),
                        "from": ref_rel, "kind": kind, "name": name,
                        "line": line, "via": extra.get("via")})
        return
    for rel, key in same:
        dst_cid = make_cid(kind, b.world_id, rel, key)
        edge_src, edge_dst = (dst_cid, src_cid) if reverse else (src_cid, dst_cid)
        edge = {"type": etype, "src": edge_src, "dst": edge_dst,
                "doc": ref_rel, "line": line, "status": "active"}
        apply_extra(b, edge, etype, ref_rel, analyzer_name, dict(extra))
        edge["_rule"], edge["_from_def"] = "config_key_all", from_def
        stamp_edge(b, edge, ref_rel, rel)
        b.link_edges.append(edge)


def link(b: WorldBuild, etype, src_cid, kind, name, ref_rel, line, analyzer_name=None, extra=None, reverse=False,
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
    chain_parents = analyzer_parents(analyzer_name) if type_ref else None
    requires_context = analyzer_requires_context(analyzer_name) if type_ref else False

    if extra.get("via") == "config_key":              # A9: 通常解決の前に特例へ分岐
        link_config_key_all(b, etype, src_cid, kind, name, ref_rel, line, analyzer_name, extra, reverse, from_def)
        return

    resolved_name = name
    rule = "nearest_name"                             # 接続を決めた解決規則（分岐ごとに上書きする）
    path_suffix, path_exact, include_path = _include_hints(extra)

    def _flag(reason, flag_name):
        b.flags.append({"reason": reason, "from": ref_rel, "kind": kind, "name": flag_name,
                        "line": line, "via": extra.get("via")})

    if path_suffix:
        # Spring `<import resource>` 等の classpath 相対パス参照: 資料フォルダ内の実ファイルの rel_path が末尾一致する形でしか解決しない。
        # 一意に決まらなければ接続しない。
        rel, status = resolve_path_suffix(b.path_suffix_index, kind, name, ref_rel, b.period_select)
        if status:
            b.flags.append({"reason": (status if status == UNDETERMINED else "config_import_ambiguous" if status == "ambiguous"
                                       else "config_import_unresolved"),
                            "from": ref_rel, "kind": kind, "name": name, "line": line,
                            "via": extra.get("via")})
            return
        resolved_name = b.rel_name[rel][1]
        rule = "path_suffix"
    elif include_path and "/" in include_path:
        # `#include "path"` の 1 段目: 相対パス完全一致（同一 top_scope 内）。`path_exact` の参照は、一致しなければ未解決として申告する
        # （basename の最近傍へ倒さない）。それ以外は 2 段目（拡張子込み basename の最近傍）へ。
        rel = resolve_include_relpath(b.rel_name, ref_rel, include_path, kind, name)
        if rel is None and b.path_aliases:
            rel = resolve_include_alias(b.rel_name, b.path_aliases, include_path, ref_rel, kind, name)
            if rel is not None:
                rule = "path_alias_setting"
        if rel is None:
            if path_exact:
                _flag("unresolved_qualifier", include_path)
                return
            rel, status = resolve_nearest(b.defs, kind, name, ref_rel, b.period_select)
            if status:
                _flag(status, name)
                return
        elif rule != "path_alias_setting":
            rule = "path_exact"
    elif kind == "Table":
        got = _resolve_table_ref(b, kind, name, ref_rel, line, extra)
        if got is None:
            return
        rel, resolved_name, rule = got
    elif qualified:
        # 完全修飾名の解決: 一致する定義が無ければ、短名へ倒さず未解決として申告する。
        if type_ref and chain_parents:
            rel, actual_name, status = resolve_dotted_type_name(
                b.qualified_defs, kind, name, ref_rel, file_context, chain_parents,
                file_context.package_at(line) if file_context is not None else None, absolute, b.period_select)
        else:
            rel, actual_name, status = resolve_qualified(b.qualified_defs, kind, name, ref_rel, b.period_select)
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
        rel, actual_name, status, shown = resolve_type_name(
            b.qualified_defs, b.defs, kind, name, ref_rel, file_context, bool(chain_parents), file_context.package_at(line),
            rule_out=rule_out, ps=b.period_select)
        if status:
            _flag(status, shown)
            return
        resolved_name = actual_name
        rule = rule_out[0] if rule_out else "nearest_name"
    else:
        rel, resolved_name, rule, status = _resolve_nearest_ref(b, etype, kind, name, ref_rel, analyzer_name, extra.get("via"))
        if status == "unresolved" and b.name_rules:   # 設定の名前の対応は未解決のときだけ（曖昧・世代跨ぎ・期の未決定には使わない）
            return _link_by_name_rules(b, etype, src_cid, kind, name, ref_rel, line, analyzer_name, extra, reverse, rule_hint, from_def)
        if status:                                    # ''=解決／ambiguous/cross_scope/unresolved は flag
            _flag(status, name)
            return

    _push_edge(b, etype, src_cid, kind, rel, resolved_name, ref_rel, line, analyzer_name, extra, reverse, rule, rule_hint,
               from_def)


def _include_hints(extra):
    """`via=include` の参照の解決の指示 `(path_suffix, path_exact, include_path)`。指示は pop する（残すと `apply_extra` が edge のプロパティへ透過する）。"""
    if extra.get("via") != "include":
        return False, False, None
    return bool(extra.pop("path_suffix", False)), bool(extra.pop("path_exact", False)), extra.get("include_path")


def _link_by_name_rules(b: WorldBuild, etype, src_cid, kind, name, ref_rel, line, analyzer_name, extra, reverse, rule_hint, from_def):
    """未解決の参照へ `name_rules` を当て、張れたものを積む。張れなければ理由（ambiguous／unresolved）を申告する。"""
    status = name_rules.link_by_rules(
        b, kind, name, ref_rel,
        lambda r, n: _push_edge(b, etype, src_cid, kind, r, n, ref_rel, line, analyzer_name, dict(extra), reverse,
                                "name_rule_setting", rule_hint, from_def))
    if status:
        b.flags.append({"reason": status, "from": ref_rel, "kind": kind, "name": name, "line": line, "via": extra.get("via")})


def _resolve_table_ref(b: WorldBuild, kind, name, ref_rel, line, extra):
    """`Table` 参照の解決。戻りは `(rel, 解決後の名前, 規則)`。決まらなければ申告して `None`。"""
    schema_name, simple_name = getattr(name, "schema", None), getattr(name, "simple", name)
    rel, actual_name, status = resolve_table(b.table_defs, name, ref_rel, b.period_select)
    no_def_here = status == "cross_scope" or (status == "unresolved" and not b.table_defs.get(simple_name))   # 別の schema の定義が同じトップフォルダにあるときは対象にしない
    if no_def_here and extra.get("via") in UNDEFINED_TABLE_VIAS and getattr(name, "supported", True):
        got = undefined_table(b, name, ref_rel)   # 定義が資料フォルダに無い表は、トップフォルダの中の定義の無い表につなぐ
        if got is not None:
            return got[0], got[1], UNDEFINED_TABLE_RULE
    if status:                                    # schema 違い・同名の複数 schema は任意選択しない
        b.flags.append({"reason": status, "from": ref_rel, "kind": kind, "name": name,
                        "line": line, "via": extra.get("via")})
        return None
    if schema_name is None:
        return rel, actual_name, "table_name"
    chosen = next((c for c in b.table_defs.get(simple_name, []) if c[0] == rel and c[1] == actual_name), None)
    return rel, actual_name, ("schema_exact" if chosen is not None and chosen[2] == schema_name else "schema_unqualified")


def _resolve_nearest_ref(b: WorldBuild, etype, kind, name, ref_rel, analyzer_name, via):
    """名前だけの参照の解決（同じトップフォルダの最近傍）。戻りは `(rel, 解決後の名前, 規則, status)`。`status` が空でなければ決まらなかった。"""
    resolved_name = name
    rule = "nearest_name"
    rel, status = resolve_nearest(b.defs, kind, name, ref_rel, b.period_select)
    if status == "unresolved" and simple_name_calls(analyzer_name) and via == "call":
        # 手続き型言語（C/VB）の関数呼び出し解決: 通常解決で見つからない単純名は `simple_name_defs` を 2 段目として参照し、
        # その判定結果（解決/ambiguous/cross_scope/unresolved）をそのまま採用する。
        # 参照側も同じアナライザ由来かつ `via=call` のときだけに限る。
        alt_rel, alt_key, alt_status = resolve_nearest_keyed(
            b.simple_name_defs, analyzer_name, kind, name, ref_rel, b.period_select)
        if alt_status:
            status = alt_status
        else:
            rel, resolved_name, status = alt_rel, alt_key, ""
    if status and status != UNDETERMINED and etype == "COPIES" and b.copy_paths:
        # 最近傍で決まらない COPY: 設定の `copy_paths` を先頭から試す。決まらなければ元の状態（未解決・曖昧）のまま申告する
        alt_rel = resolve_by_copy_paths(b.defs, kind, name, ref_rel, b.copy_paths)
        if alt_rel is not None:
            rel, status, rule = alt_rel, "", "copy_path_setting"
    return rel, resolved_name, rule, status


def _push_edge(b: WorldBuild, etype, src_cid, kind, rel, resolved_name, ref_rel, line, analyzer_name, extra, reverse, rule,
               rule_hint, from_def) -> None:
    """解決できた参照のエッジの候補を `b.link_edges` に積む。接続を決めた規則と参照元の定義を付ける（集約が根拠 `sources` にする）。"""
    dst_cid = make_cid(kind, b.world_id, rel, resolved_name)
    edge_src, edge_dst = (dst_cid, src_cid) if reverse else (src_cid, dst_cid)
    edge = {"type": etype, "src": edge_src, "dst": edge_dst,
            "doc": ref_rel, "line": line, "status": "active"}
    apply_extra(b, edge, etype, ref_rel, analyzer_name, extra)
    if rule_hint is not None:
        if rule_hint in EDGE_RULES:
            rule = rule_hint
        else:
            b.flags.append({"reason": "unknown_rule", "analyzer": analyzer_name, "from": ref_rel, "rule": str(rule_hint)})
    edge["_rule"], edge["_from_def"] = rule, from_def
    stamp_edge(b, edge, ref_rel, rel)
    b.link_edges.append(edge)
