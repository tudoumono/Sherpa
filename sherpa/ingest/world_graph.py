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

import hashlib
import os
import posixpath
import re
import stat as stat_mod
from collections import defaultdict
from pathlib import Path, PurePosixPath

from .. import corpus_docs, doc_text, scope_infer, text_encoding, worlds
from . import importance, text_kind
from .analyzers import registry as analyzer_registry


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


def _resolve_qualified(qualified_defs, kind, name, ref_rel):
    """完全修飾名の解決: `qualified_defs[(kind,name)]`（`[(rel,実名), ...]`）を同一 top_scope に絞り、パス距離で最近傍を選ぶ。

    最短距離が複数なら任意選択せず `'ambiguous'`。戻りは `(rel|None, 実名|None, status)`。
    `status` は `''` / `'ambiguous'` / `'cross_scope'` / `'unresolved'`（`unresolved`/`cross_scope` は呼び出し側が
    単純名フォールバックへ倒して qualified_fallback を flags に記録し、`ambiguous` はそのまま flags へ記録する）。
    """
    cands = qualified_defs.get((kind, name))
    if not cands:
        return None, None, "unresolved"
    same = [(r, nm) for r, nm in cands if _top(r) == _top(ref_rel)]
    if not same:
        return None, None, "cross_scope"
    ranked = sorted(same, key=lambda pair: _tree_distance(ref_rel, pair[0]))
    best = _tree_distance(ref_rel, ranked[0][0])
    if sum(1 for r, _ in same if _tree_distance(ref_rel, r) == best) > 1:
        return None, None, "ambiguous"
    rel, actual_name = ranked[0]
    return rel, actual_name, ""


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



def _simple_name_calls(analyzer_name) -> bool:
    """`analyzer_name` のアナライザが単純名の呼び出し解決（children への 2 段目）を要求するか。未登録名は False。"""
    for a in analyzer_registry.known_analyzers():
        if a.name == analyzer_name:
            return bool(getattr(a, "resolves_calls_by_simple_name", False))
    return False

def _resolve_include_relpath(rel_name, ref_rel, include_path, kind, name):
    """C の `#include "path"` の 1 段目解決: パス区切りを含む `include_path` を参照元 `ref_rel` からの相対パスとして解決する。

    その rel_path が実際に `name`（拡張子込みファイル名）の主体であれば一意に採用し、距離計算・曖昧判定は経由しない。
    同一 top_scope を跨ぐ解決はしない。見つからなければ `None`（呼び出し側が basename の最近傍へフォールバックする）。
    区切りは `normpath` の前に `\\`→`/` へ直す。
    """
    base_dir = ref_rel.rsplit("/", 1)[0] if "/" in ref_rel else ""
    joined = f"{base_dir}/{include_path}" if base_dir else include_path
    candidate = posixpath.normpath(joined.replace("\\", "/"))
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


def _aggregate_pass2_edges(raw_edges: list) -> list:
    """同一 `(src, type, dst)` の複数候補を 1 本へ集約する。

    `analyzer_registry.via_priority_rank`（FW 固有 via を汎用 via より優先）で採用する候補を選び、
    同順位は初出を採用する。line/via は採用した候補から丸ごと引き継ぐ。`(src,type,dst)` の初出順を保って返す。
    """
    best: dict = {}
    order: list = []
    for e in raw_edges:
        key = (e["src"], e["type"], e["dst"])
        if key not in best:
            best[key] = e
            order.append(key)
            continue
        if analyzer_registry.via_priority_rank(e.get("via")) < analyzer_registry.via_priority_rank(best[key].get("via")):
            best[key] = e
    return [best[k] for k in order]


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


def _mention_edges_for_doc(rel: str, text: str, mdict: dict, min_len: int, max_per_doc: int,
                           world_id: str, nodes: dict, edges: list, flags: list) -> None:
    """1 文書分の言及突合: トークン化→辞書突合→`Document -DOCUMENTS(via=mention)-> コード` を張る。

    1 文書あたりの上限（`max_per_doc`）を超えた分は張らず、件数を `flags`（`mention_overflow`）へ申告する。
    上限は 1 トークンが複数世代へ展開される場合もエッジ単位で数える。
    dst の cid は `targets` の `key`（修飾名を含み得る）で組み立てる。同一 `(doc, dst)` は 1 本にまとめる。
    """
    added = 0
    overflow = 0
    doc_cid = None
    seen_dst: set = set()
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
            edges.append({"type": "DOCUMENTS", "src": doc_cid, "dst": dst_cid,
                         "doc": rel, "line": 0, "status": "active",
                         "via": "mention"})
            seen_dst.add(dst_cid)
            added += 1
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


def build_world(world_dir, world_id: str, *, files=None):
    """資料フォルダ（登録ディレクトリ）を `(nodes, edges, flags)` にする。パス同一性＋同 top_scope 内の最近傍解決。

    骨格（Pass1/Pass2＝COPIES/CONTAINS/INVOKES/ACCESSES）と言及エッジ（Pass3）だけを決定的に構築する。
    `files`（省略可）: 呼び出し側が `scope_infer.safe_files(world_dir)` を materialize 済みなら渡す（再度歩かない）。
    `_重要度.txt` はここで除外する。
    """
    entries = files if files is not None else scope_infer.safe_files(
        world_dir, also=worlds.archives_dir(world_id))
    # 秘匿ファイル（環境変数ファイル系・秘密鍵系・credentials 等）はグラフ取り込みでも読まない。
    # Pass1 は拡張子で候補を引くため、拡張子付きの秘匿名（YAML・shell 等）を別に塞ぐ。
    files = [(rp, rel) for rp, rel in entries
             if not importance.is_importance_control_path(rel)
             and not text_kind.is_sensitive(PurePosixPath(rel).name, PurePosixPath(rel).suffix.lower())]

    defs: dict = {}            # (label, NAME) -> [rel, ...]
    qualified_defs: dict = {}  # (label, cid_key) -> [(rel, 実名), ...]（cid_key が付く定義は常時登録）
    rel_name: dict = {}        # rel -> (label, NAME)  ＝ファイルの主体名
    texts: dict = {}           # rel -> (text, analyzer)
    nodes: dict = {}           # cid -> node
    edges: list = []
    link_edges: list = []      # Pass2 の解決済みエッジ（集約前のステージング）
    flags: list = []
    # 言及突合の単純名エイリアス: (label, DefItem.name) -> [(rel, DefItem.key), ...]。
    # `key`（cid_key）が `name`（表示名）と異なる子定義（コピーブックの `GROUP.ITEM` 等）だけを登録する。
    mention_aliases: dict = {}
    # 手続き型言語の関数呼び出し解決専用の索引: (analyzer_name, label, 単純名) -> [(rel, cid_key, c_kind|None), ...]。
    # children は `defs` に cid_key（修飾名）でしか登録されないため、`_link` が通常解決で unresolved のときだけ
    # 2 段目として参照する。索引キーに analyzer_name を含め、他言語の同名と誤接続しない。
    simple_name_defs: dict = {}
    # `via=config_key` 専用の索引: (label, key_kind, 裸キー) -> [(rel, cid_key), ...]。
    # 設定ファイルのキー child（`label=="Config"` かつ `cid_key` が `"key:"` 接頭辞）だけを登録し、primary（`defs`）候補を混ぜない。
    # `key_kind`（"property"/"bean"/"action"/"mapper"/"url"/"env"）を索引キーに含め、同じ裸キーの別種別を同一視しない。
    config_key_index: dict = {}

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

        `cid_key` が付いているものは条件なしで登録する（children は cid が `cid_key` で組み立てられるため、
        skip すると完全修飾名参照が登録漏れになる）。
        """
        if cid_key is not None:
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
            _index_qualified(child.label, child.cid_key, rel, child.key)
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
        defres = analyzer.collect_defs(text, rel)
        _flag_dropped(analyzer.name, rel, defres.dropped)
        if defres.primary is None:                        # 構文にマッチせず主体を持たない
            continue
        if defres.primary.label not in analyzer_registry.NODE_LABELS:
            flags.append({"reason": "unknown_label", "analyzer": analyzer.name,
                          "label": defres.primary.label, "from": rel})
            continue
        _def(defres.primary.label, defres.primary.name, rel)
        _index_qualified(defres.primary.label, defres.primary.cid_key, rel, defres.primary.name)
        prim_cid = _cid(defres.primary.label, world_id, rel, defres.primary.name)
        prim_base = {**_node(defres.primary.label, world_id, rel, defres.primary.name,
                             value=defres.primary.value), "analyzer": analyzer.name}
        prim_extra = _sanitized_extra(analyzer.name, rel, defres.primary.label, defres.primary.name,
                                      prim_base.keys(), defres.primary.extra)
        nodes[prim_cid] = {**prim_base, **prim_extra}
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
            _index_qualified(item.label, item.cid_key, rel, item.name)
            extra_cid = _cid(item.label, world_id, rel, item.name)
            extra_base = {**_node(item.label, world_id, rel, item.name, value=item.value),
                          "line": item.line, "analyzer": analyzer.name}
            extra_props = _sanitized_extra(analyzer.name, rel, item.label, item.name,
                                           extra_base.keys(), item.extra)
            nodes[extra_cid] = {**extra_base, **extra_props}
            if item.key != item.name:                  # 修飾名≠表示名＝言及辞書に単純名でも登録
                mention_aliases.setdefault((item.label, item.name), []).append((rel, item.key))
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

    def _link_config_key_all(etype, src_cid, kind, name, ref_rel, line, analyzer_name, extra, reverse):
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
            link_edges.append(edge)

    def _link(etype, src_cid, kind, name, ref_rel, line, analyzer_name=None, extra=None, reverse=False):
        extra = dict(extra) if extra else {}
        # `qualified` は解決の指示であってエッジの事実ではないため、共通層が消費して取り除く
        qualified = bool(extra.pop("qualified", False)) and "." in name

        if extra.get("via") == "config_key":              # A9: 通常解決の前に特例へ分岐
            _link_config_key_all(etype, src_cid, kind, name, ref_rel, line, analyzer_name, extra, reverse)
            return

        resolved_name = name
        # 同様に pop する（残すと `_apply_extra` が edge のプロパティへ透過する）
        path_suffix = bool(extra.pop("path_suffix", False)) if extra.get("via") == "include" else False
        include_path = extra.get("include_path") if extra.get("via") == "include" else None
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
        elif include_path and "/" in include_path:
            # C の `#include` の 1 段目: 相対パス完全一致（同一 top_scope 内）。見つからなければ 2 段目（拡張子込み basename の最近傍）へ。
            rel = _resolve_include_relpath(rel_name, ref_rel, include_path, kind, name)
            if rel is None:
                rel, status = _resolve_nearest(defs, kind, name, ref_rel)
                if status:
                    flags.append({"reason": status, "from": ref_rel, "kind": kind, "name": name,
                                 "line": line, "via": extra.get("via")})
                    return
        elif qualified:
            rel, actual_name, status = _resolve_qualified(qualified_defs, kind, name, ref_rel)
            if status == "ambiguous":                     # 同距離複数＝任意選択しない（単純名へも倒さない）
                flags.append({"reason": "ambiguous", "from": ref_rel, "kind": kind, "name": name,
                             "line": line, "via": extra.get("via")})
                return
            if status:                                    # 完全一致なし（unresolved/cross_scope）＝単純名へフォールバック
                simple = name.rsplit(".", 1)[-1]
                rel, status = _resolve_nearest(defs, kind, simple, ref_rel)
                if status:
                    flags.append({"reason": status, "from": ref_rel, "kind": kind, "name": simple,
                                 "line": line, "via": extra.get("via")})
                    return
                resolved_name = simple
                flags.append({"reason": "qualified_fallback", "from": ref_rel, "kind": kind, "name": name})
            else:
                resolved_name = actual_name
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
            if status:                                    # ''=解決／ambiguous/cross_scope/unresolved は flag
                flags.append({"reason": status, "from": ref_rel, "kind": kind, "name": name,
                             "line": line, "via": extra.get("via")})
                return

        dst_cid = _cid(kind, world_id, rel, resolved_name)
        edge_src, edge_dst = (dst_cid, src_cid) if reverse else (src_cid, dst_cid)
        edge = {"type": etype, "src": edge_src, "dst": edge_dst,
               "doc": ref_rel, "line": line, "status": "active"}
        _apply_extra(edge, etype, ref_rel, analyzer_name, extra)
        link_edges.append(edge)

    # Pass1 完了直後（`rel_name` 確定後）に 1 回だけ構築し、`_link` が閉包で参照する
    path_suffix_index = _build_path_suffix_index(rel_name)

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
        _flag_dropped(analyzer.name, rel, ref_result.dropped)
        name_pair = rel_name.get(rel)                     # 主体を持たないファイル（例: JOB の無い JCL PROC）は src が無い
        if name_pair is None:                             # dropped は既に記録済み・参照エッジは張れない
            continue
        label, name = name_pair
        src = _cid(label, world_id, rel, name)
        for ref in ref_result.refs:
            if ref.edge_type not in analyzer_registry.EDGE_TYPES:
                flags.append({"reason": "unknown_edge_type", "analyzer": analyzer.name,
                              "from": rel, "edge_type": ref.edge_type})
                continue
            if ref.kind not in analyzer_registry.NODE_LABELS:
                flags.append({"reason": "unknown_label", "analyzer": analyzer.name,
                              "from": rel, "label": ref.kind})
                continue
            _link(ref.edge_type, src, ref.kind, ref.name, rel, ref.line,
                 analyzer_name=analyzer.name, extra=ref.extra, reverse=ref.reverse)

    # 同一 (src,type,dst) の複数候補を 1 本へ集約してから確定する（全 Pass2 エッジに適用・nodes/flags は不変）
    edges.extend(_aggregate_pass2_edges(link_edges))

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
