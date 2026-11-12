"""Pass 1: 解析器を呼んで定義を集め、ノードと解決用の索引を作る。VB.NET のプロジェクト設定（`.vbproj`）を読む段も持つ。

設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

from ... import corpus_docs, text_encoding
from .. import text_kind
from ..analyzers import registry as analyzer_registry
from ..analyzers import vb_project
from .select import top_folder
from .state import WorldBuild, make_cid, make_node


def analyzer_by_name(analyzer_name):
    for a in analyzer_registry.known_analyzers():
        if a.name == analyzer_name:
            return a
    return None


def analyzer_parents(analyzer_name):
    """型名の解決で名前空間が親へさかのぼる言語か。未登録名は `None`。"""
    a = analyzer_by_name(analyzer_name)
    return None if a is None else bool(a.resolves_parent_namespaces)


def analyzer_requires_context(analyzer_name) -> bool:
    a = analyzer_by_name(analyzer_name)
    return bool(a is not None and a.requires_file_context)


def simple_name_calls(analyzer_name) -> bool:
    """`analyzer_name` のアナライザが単純名の呼び出し解決（children への 2 段目）を要求するか。未登録名は False。"""
    for a in analyzer_registry.known_analyzers():
        if a.name == analyzer_name:
            return bool(getattr(a, "resolves_calls_by_simple_name", False))
    return False


def _vb_flag(b: WorldBuild, rel, why, snippet):
    b.flags.append({"reason": "dropped_syntax", "analyzer": "vb", "from": rel, "why": why, "line": 1, "snippet": snippet[:200]})


def read_vb_projects(b: WorldBuild) -> None:
    """VB.NET のプロジェクト設定（`.vbproj`・`Directory.Build.props`）を読み、ソースへ当てるための索引 `b.vb_index` を作る。"""
    vb_proj_dirs = {str(PurePosixPath(rel).parent) if "/" in rel else ""
                    for _rp, rel in b.files if rel.lower().endswith(vb_project.VBPROJ_EXT)}
    for rp, rel in b.files:
        low = rel.lower()
        is_proj, is_props = low.endswith(vb_project.VBPROJ_EXT), low.rsplit("/", 1)[-1] == vb_project.PROPS_NAME
        if not (is_proj or is_props):
            continue
        if is_props:                                      # 当てはめる `.vbproj` の祖先（自分のフォルダを含む）にあたる props だけ読む
            pdir = str(PurePosixPath(rel).parent) if "/" in rel else ""
            if not any(pdir == "" or d == pdir or d.startswith(pdir + "/") for d in vb_proj_dirs):
                continue
        _read_vb_config(b, rp, rel, is_proj)
    b.vb_index = vb_project.ProjectIndex([vb_project.resolve_root(p, b.vb_props) for p in b.vb_projects])


def _read_vb_config(b: WorldBuild, rp, rel, is_proj) -> None:
    try:
        if rp.stat().st_size > text_kind.MAX_BYTES:
            raise ValueError("size_exceeded")
        vb_text = vb_project.decode_xml(rp.read_bytes())
        parsed = vb_project.parse_vbproj(rel, vb_text) if is_proj else vb_project.parse_props(rel, vb_text)
    except (OSError, ValueError, RecursionError) as e:   # 読めない設定は当てはめの「止め」にして申告する（外側の設定へは倒さない）
        _vb_flag(b, rel, "vbproj_unreadable", str(e) if isinstance(e, ValueError) else type(e).__name__)
        parsed = vb_project.broken_project(rel) if is_proj else vb_project.PropsRoot(declared=True)
    if is_proj:
        b.vb_projects.append(parsed)
    else:
        b.vb_props[str(PurePosixPath(rel).parent) if "/" in rel else ""] = parsed
    for v in parsed.unevaluated:
        _vb_flag(b, rel, "vbproj_unevaluated", v)
    for v in getattr(parsed, "outside", ()):
        _vb_flag(b, rel, "vbproj_include_outside", v)
    for v in getattr(parsed, "dropped_imports", ()):
        _vb_flag(b, rel, "vbproj_import_alias", v)


def _def(b: WorldBuild, label, name, rel):
    b.defs.setdefault((label, name), []).append(rel)
    b.rel_name[rel] = (label, name)


def _index_def(b: WorldBuild, label, name, rel):
    """`defs` 解決索引にだけ登録する（`rel_name` は更新しない）。

    children は解決対象にしたいがファイルの主体ではないため、`_def` と違い主体の src 決定を壊さない。
    """
    b.defs.setdefault((label, name), []).append(rel)


def _index_qualified(b: WorldBuild, label, cid_key, rel, actual_name):
    """完全修飾名を解決索引（`defs` とは別枠）へ追加登録する。

    `cid_key`（または `DefItem.qualified`）が付いているものは条件なしで登録する（children は cid が `cid_key` で組み立てられるため、
    skip すると完全修飾名参照が登録漏れになる）。
    """
    if cid_key is not None:
        proj = b.vb_bound.get(rel)
        if proj is not None and proj.root_namespace:      # VB.NET のプロジェクトのソースは本当の完全修飾名（Root Namespace つき）だけ
            cid_key = f"{proj.root_namespace}.{cid_key}"
        b.qualified_defs.setdefault((label, cid_key), []).append((rel, actual_name))


def _sanitized_extra(b: WorldBuild, analyzer_name, rel, label, name, base_keys, extra):
    """`DefItem.extra` から共通層が確定したフィールドと同名のキーを除去する。

    `cid`/`label`/`name`/`analyzer` 等を上書きさせない。1 つでも衝突したら `extra` を丸ごと捨て、理由を `flags` に記録する。
    """
    bad = set(extra) & base_keys
    if not bad:
        return extra
    b.flags.append({"reason": "reserved_key_in_extra", "analyzer": analyzer_name, "from": rel,
                    "label": label, "name": name, "keys": sorted(bad)})
    return {}


def note_plugin_failures(b: WorldBuild, rel, failures):
    """適用時に例外を出した FW プラグインを集計する（最後に 1 プラグイン 1 件の `plugin_failed` として `flags` へ出す）。"""
    for f in failures:
        rec = b.plugin_failed.setdefault(f.plugin, {"files": set(), "from": rel, "why": f"{f.phase}: {f.why}"})
        rec["files"].add(rel)


def flag_dropped(b: WorldBuild, analyzer_name, rel, dropped):
    """`Dropped`（解析せず落とした構文）を `flags` へ記録する（黙って消さない）。"""
    for d in dropped:
        b.flags.append({"reason": "dropped_syntax", "analyzer": analyzer_name, "from": rel,
                        "why": d.reason, "line": d.line, "snippet": d.snippet})


def _register_children(b: WorldBuild, parent_cid, children, rel, analyzer_name):
    """`parent -CONTAINS-> child` を 1 本ずつ生成する共通処理（primary の `children` と `DefResult.extras` の両方で使う）。"""
    for child in children:
        if child.label not in analyzer_registry.NODE_LABELS:
            b.flags.append({"reason": "unknown_label", "analyzer": analyzer_name,
                            "label": child.label, "from": rel})
            continue
        child_cid = make_cid(child.label, b.world_id, rel, child.key)
        child_base = {**make_node(child.label, b.world_id, rel, child.name, value=child.value),
                      "cid": child_cid, "line": child.line, "analyzer": analyzer_name}
        child_extra = _sanitized_extra(b, analyzer_name, rel, child.label, child.name,
                                       child_base.keys(), child.extra)
        b.nodes[child_cid] = {**child_base, **child_extra}
        _index_def(b, child.label, child.key, rel)   # children も解決対象にする
        b.symbol_cids[(rel, child.key)] = child_cid
        b.def_qkey[(rel, child.key)] = child.resolve_key
        _index_qualified(b, child.label, child.resolve_key, rel, child.key)
        if child.key != child.name:                # 修飾名≠表示名＝言及辞書に単純名でも登録
            b.mention_aliases.setdefault((child.label, child.name), []).append((rel, child.key))
            if simple_name_calls(analyzer_name):
                # 手続き型言語の関数呼び出し解決専用の単純名索引（`Analyzer.resolves_calls_by_simple_name`）:
                # `defs` は children を cid_key（修飾名）でしか索引しないため、単純名の呼び出しは `link` が 2 段目で `simple_name_defs` を引く。
                # `c_kind` は定義（`.c`）を宣言（`.h`）より優先する材料。索引キーに analyzer_name を含め、他言語の同名と誤接続しない。
                b.simple_name_defs.setdefault((analyzer_name, child.label, child.name), []).append(
                    (rel, child.key, child.extra.get("c_kind")))
        if child.cid_key is not None and child.cid_key.startswith("key:"):
            # `via=config_key` 専用索引（設定ファイルのキー child のみ）。key_kind で名前空間を分ける。
            b.config_key_index.setdefault(
                (child.label, child.extra.get("key_kind"), child.name), []).append((rel, child.key))
        b.edges.append({"type": "CONTAINS", "src": parent_cid, "dst": child_cid, "doc": rel,
                        "line": child.line, "status": "active"})


def collect_definitions(b: WorldBuild, step) -> None:
    """① ファイルごとに拡張子→アナライザを引いて `collect_defs` を呼び、定義ノードと解決用の索引を作る。`step` は進み具合の通知。"""
    for n_seen, (rp, rel) in enumerate(b.files):
        step.tick(n_seen)
        _collect_file(b, rp, rel)


def _collect_file(b: WorldBuild, rp, rel) -> None:
    candidates = analyzer_registry.candidates(rel)
    if not candidates:                                # どのアナライザも拡張子を担当しない＝資料
        return
    got = _read_source(b, rp, rel, candidates)
    if got is None:
        return
    text, raw, analyzer = got
    # 受理済み（拡張子一致＋accepts 通過）なら主体の有無に関わらず Pass2 を通す（主体なしファイルも dropped_syntax 検知の対象）。
    b.texts[rel] = (rp, analyzer, hashlib.sha1(raw).hexdigest())   # 本文は保持しない（Pass2 で読み直す＝メモリを有界化）・指紋で同一性を照合
    _bind_vb_project(b, analyzer, rel)
    defres = _collect_defres(b, analyzer, text, rel)
    _register_definitions(b, analyzer, rel, defres)


def _read_source(b: WorldBuild, rp, rel, candidates):
    """ソースを読み、受理するアナライザを決める。戻りは `(本文, 生バイト列, アナライザ)`。対象外・読めない・文字コード不明は申告して `None`。"""
    # サイズ上限（`text_kind.MAX_BYTES`）: 全アナライザに一律で適用する。
    # 読み飛ばさないと `read_full_text_and_raw()` が巨大ファイルを全量メモリに読み込み、単一 worker が OOM し得る。
    try:
        oversize = rp.stat().st_size > text_kind.MAX_BYTES
    except OSError:
        oversize = False               # stat 失敗は下の read_full_text_and_raw() 側の OSError 処理に委ねる
    if oversize:
        b.flags.append({"reason": "dropped_syntax", "analyzer": candidates[0].name, "from": rel,
                        "why": "size_exceeded", "line": 1, "snippet": ""})
        return None
    try:
        text, raw = corpus_docs.read_full_text_and_raw(rp)
    except OSError:
        # 受理済み（拡張子が一致する）コード文書の実読込失敗は run 全体を失敗させる（fail-closed）。
        # blocked flag により台帳書込・Neo4j 反映へ進ませず、部分グラフを確定しない（次回 sync で全再構築）。
        b.flags.append({"doc": rel, "reason": "unreadable_code_file", "action": "blocked"})
        return None
    # 台帳（`corpus_docs.classify_document`）・grep・精読が「文字コードを判別できない」として対象外にする原本は、
    # グラフにもノード・エッジを作らない（判定は `text_encoding.quality_of` を直接使う）。
    encoding, enc_ratio, enc_majority_garbled = text_encoding.detect_bytes_quality(raw, complete=True)
    if text_encoding.quality_of(enc_ratio, enc_majority_garbled) == "undetermined":
        b.flags.append({"doc": rel, "reason": "encoding_undetermined", "action": "warn"})
        return None

    # `accepts()` に渡す head は `Analyzer.head_bytes`（既定 4KiB）のバイト数で、読み取り済みの生バイト列 `raw` を
    # スライス・デコードする（ファイルを開き直さない）。
    def _head_for(a):
        return text_encoding.decode(raw[:getattr(a, "head_bytes", 4096)], encoding)
    analyzer = next((a for a in candidates if a.accepts(rel, _head_for(a))), None)
    if analyzer is None:                              # 拡張子は一致するが内容判定で不採用
        return None
    return text, raw, analyzer


def _bind_vb_project(b: WorldBuild, analyzer, rel) -> None:
    """VB.NET のソースへ、属する `.vbproj` を当てる。同じフォルダに当たり得るものが複数なら当てずに申告する。"""
    if analyzer.name == "vb" and rel.lower().endswith(".vb") and b.vb_index:
        proj, ambiguous = b.vb_index.project_for(rel)
        if proj is not None:
            b.vb_bound[rel] = proj
        elif ambiguous:                               # 同じフォルダに当たり得る .vbproj が複数＝当てずに申告する
            b.flags.append({"reason": "dropped_syntax", "analyzer": "vb", "from": rel,
                            "why": "vbproj_ambiguous", "line": 1, "snippet": ", ".join(ambiguous)[:200]})


def _collect_defres(b: WorldBuild, analyzer, text, rel):
    """アナライザの `collect_defs` に FW プラグインの追加分を足した定義。落とした構文と世代内の import も集める。"""
    defres = analyzer.collect_defs(text, rel)
    fw_here = analyzer_registry.applicable_fw_plugins(analyzer, text, rel)
    if fw_here:                                       # FW プラグイン: 本体の定義へ追加分を足す（失敗は本体の結果を残して申告）
        base_defres = defres
        defres, pfails = analyzer_registry.apply_fw_defs(fw_here, text, rel, base_defres, b.fw_build_ctx)
        note_plugin_failures(b, rel, pfails)
        fw_failed = {f.plugin for f in pfails}
        b.fw_ctx[rel] = ([p for p in fw_here if p.name not in fw_failed], base_defres)
    for imp in analyzer.global_imports(text, rel):
        b.global_imports.setdefault((analyzer.name, top_folder(rel)), []).append(imp)
    flag_dropped(b, analyzer.name, rel, defres.dropped)
    return defres


def _register_definitions(b: WorldBuild, analyzer, rel, defres) -> None:
    """主体（primary）と子（children）、同一ファイル内の主体以外のトップレベル定義（extras）をノード化し、索引へ登録する。"""
    if defres.primary is None:                        # 構文にマッチせず主体を持たない
        if defres.dropped:                            # 落とした構文があるのに主体も無い＝ファイルごとグラフに入らない
            b.flags.append({"reason": "source_not_registered", "analyzer": analyzer.name, "from": rel})
        return
    if defres.primary.label not in analyzer_registry.NODE_LABELS:
        b.flags.append({"reason": "unknown_label", "analyzer": analyzer.name,
                        "label": defres.primary.label, "from": rel})
        return
    _def(b, defres.primary.label, defres.primary.name, rel)
    _index_qualified(b, defres.primary.label, defres.primary.resolve_key, rel, defres.primary.name)
    b.def_qkey[(rel, None)] = defres.primary.resolve_key
    prim_cid = make_cid(defres.primary.label, b.world_id, rel, defres.primary.name)
    prim_base = {**make_node(defres.primary.label, b.world_id, rel, defres.primary.name,
                             value=defres.primary.value), "analyzer": analyzer.name}
    prim_extra = _sanitized_extra(b, analyzer.name, rel, defres.primary.label, defres.primary.name,
                                  prim_base.keys(), defres.primary.extra)
    b.nodes[prim_cid] = {**prim_base, **prim_extra}
    if defres.primary.label == "Table":
        b.table_defs.setdefault(defres.primary.name, []).append(
            (rel, defres.primary.key, prim_extra.get("schema")))
    _register_children(b, prim_cid, defres.children, rel, analyzer.name)
    _register_extras(b, analyzer, rel, defres)


def _register_extras(b: WorldBuild, analyzer, rel, defres) -> None:
    # 同一ファイル内の主体以外のトップレベル定義（DDL の 2 件目以降の `CREATE TABLE` 等）。
    # primary と同じ規則でノード化・索引登録するが、`_index_def` を使い `rel_name[rel]` は更新しない。
    for group in defres.extras:
        item = group.primary
        if item.label not in analyzer_registry.NODE_LABELS:
            b.flags.append({"reason": "unknown_label", "analyzer": analyzer.name,
                            "label": item.label, "from": rel})
            continue
        _index_def(b, item.label, item.name, rel)
        _index_qualified(b, item.label, item.resolve_key, rel, item.name)
        extra_cid = make_cid(item.label, b.world_id, rel, item.key)
        extra_base = {**make_node(item.label, b.world_id, rel, item.name, value=item.value),
                      "cid": extra_cid, "line": item.line, "analyzer": analyzer.name}
        extra_props = _sanitized_extra(b, analyzer.name, rel, item.label, item.name,
                                       extra_base.keys(), item.extra)
        b.nodes[extra_cid] = {**extra_base, **extra_props}
        if item.key != item.name:                  # 修飾名≠表示名＝言及辞書に単純名でも登録
            b.mention_aliases.setdefault((item.label, item.name), []).append((rel, item.key))
        if item.label == "Table":
            b.table_defs.setdefault(item.name, []).append((rel, item.key, extra_props.get("schema")))
        _register_children(b, extra_cid, group.children, rel, analyzer.name)
