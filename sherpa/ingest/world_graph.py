"""鏡モデルのグラフ構築（03-鏡モデル.md・再プラン手順2-3）。

登録ディレクトリ＝**1つの世界**を走査し、**パス同一性**のノードと**同一 top_scope 内の最近傍**で解決した
構造エッジ（COPIES/INVOKES/CONTAINS）を作る。各ノードは検索スコープのメタデータ
（`world_id / top_scope / phase / category / path`）を持つ（§3）。世代をまたぐ対応/意味エッジ
（CORRESPONDS_TO 等）は別途（影響 traversal 外）。

本採用（カットオーバー済）: 旧 `@version` 経路（merge/neo4j_load/semantic）は撤去。台帳/Neo4j/impact は
すべて本モジュールのパス同一性グラフに乗る。言語ごとの定義/参照抽出（Pass1/Pass2）は
`sherpa.ingest.analyzers`（`registry` が拡張子→アナライザ解決の単一の真実源）に委譲し、本モジュールは
名前解決（`_resolve_nearest`）・cid 組み立てなど言語非依存の共通層のみを持つ。
特定テーマの名前は持たない（語彙はデータ＝ファイル/パス由来）。

（2026-09-04-グラフのソース正典化.md §4）: 意味層フル抽出（L 抽出・`_load_semantic`）・
REALIZES 橋（`_load_concepts`/`_load_auto_concepts`・手動/自動）は概念ごと撤去。事前計算に残すのは
決定的（再現100%）に計算できる構造（骨格＝Pass1/Pass2＋言及エッジ＝Pass3）だけ——業務語からコードへの
入口はクエリ時のエージェント（文書 grep→辞書ノード）に委ねる（§2）。

Pass3（2026-09-04-グラフのソース正典化.md §2）: Pass1 の定義索引をそのまま辞書として資料文書
（`branch=="office"`）の本文と決定的に突合し、`Document -DOCUMENTS(via="mention")-> コード` を張る
（LLM ゼロ・影響 traversal 外・世代をまたいでよい制度化された例外＝`_mention_pass` 参照）。
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat as stat_mod
from pathlib import Path, PurePosixPath

from .. import corpus_docs, doc_text, grep_tool, scope_infer, worlds
from . import importance, text_kind
from .analyzers import registry as analyzer_registry
from .identifiers import normalize_code_name as _norm


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
    """ファイル由来＝パス同一性の canonical_id（複製は別ノード・MIRROR-MODEL §2.1）。"""
    return f"{label.lower()}:{world}:{rel}#{name}"


def _node(label, world, rel, name, value=None):
    return {"cid": _cid(label, world, rel, name), "label": label, "name": name,
            "world_id": world, "path": rel, "value": value,
            "extraction_method": "static", "status": "active", **_scope_meta(rel)}


def _top(rel: str):
    return rel.split("/", 1)[0] if "/" in rel else None


def _resolve_nearest(defs, kind, name, ref_rel):
    """`defs[(kind,name)]` の候補 rel から ref_rel に最も近いものを返す。

    **同一 top_scope（世代）に限定**してから最近傍（MIRROR-MODEL §2.2/§2.3＝構造エッジは世代をまたがない）。
    戻り `(rel | None, status)`＝`status` は `''`(解決) / `'ambiguous'`(同距離複数) / `'cross_scope'`(同世代に定義無し)。
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
    """完全修飾名（アナライザ拡張）の解決: `qualified_defs[(kind,name)]`（`[(rel,実名), ...]`）
    を同一 top_scope に絞り、パス距離で最近傍を解決する（**同一性＝パス**の帰結として同じ完全修飾名が
    複数 rel に存在し得る——`_resolve_nearest` と同じ規律で最短距離を採用し、最短距離が複数あれば
    任意選択せず `'ambiguous'` を返す）。戻り `(rel|None, 実名|None, status)`＝`status` は
    `''`(解決) / `'ambiguous'`(同距離複数) / `'cross_scope'` / `'unresolved'`（`unresolved`/`cross_scope`
    は呼び出し側が単純名フォールバックへ倒す・qualified_fallback を flags に記録する。`ambiguous` は
    フォールバックせずそのまま flags へ記録する）。
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
    """単純名での children 解決（アナライザ拡張 S6・波3 C↔VB 分離 RV＝手続き型言語の関数呼び出し
    解決共通）: `index[(analyzer_name,kind,name)]`（`[(rel, 実際の cid_key, c_kind|None), ...]`）を
    同一 top_scope に絞り、パス距離で最近傍を解決する。`_resolve_qualified` と同型だが、材料が
    完全修飾名ではなく「表示名→実際の cid_key」の対応表である点が異なる。索引キーに
    `analyzer_name` を含めるのは、C・VB のように複数アナライザが単純名解決を要求する場合に
    異なる言語間で同名の単純名が誤接続しないようにするため（登録・参照とも同一アナライザ内だけで
    解決する）。

    `c_kind == "definition"` の候補が1件でもあれば、`"declaration"`（宣言のみ）は候補から外し
    定義側だけで最近傍を決める（`.c` の定義を `.h` の宣言より優先する・アナライザ拡張§9——
    実装が同一ディレクトリに無ければ宣言側にフォールバックする）。定義候補が1件も無ければ
    従来どおり全候補（宣言のみ）で最近傍を決める。優先した側の中で同距離複数（同一 rel に
    複数の実キーが対応する場合を含む）は任意選択せず `'ambiguous'` にする。
    戻り `(rel|None, 実キー|None, status)`。
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
    """`analyzer_name` のアナライザが単純名の呼び出し解決（children への2段目）を要求するか
    （`Analyzer.resolves_calls_by_simple_name`）。未登録名は False。"""
    for a in analyzer_registry.known_analyzers():
        if a.name == analyzer_name:
            return bool(getattr(a, "resolves_calls_by_simple_name", False))
    return False

def _resolve_include_relpath(rel_name, ref_rel, include_path, kind, name):
    """C の `#include "path"` 1段目解決（アナライザ拡張 §4(a)/§12）: パス区切りを含む
    `include_path` を参照元 `ref_rel` からの相対パスとして解決し、その rel_path が実際に `name`
    （拡張子込みファイル名）の主体であれば一意にそのまま採用する——パス自体が一意な指定のため
    `_resolve_nearest` の距離計算・曖昧判定は経由しない。同一 top_scope を跨ぐ解決はしない
    （MIRROR-MODEL §2.2/§2.3）。見つからなければ `None`（呼び出し側が拡張子込み basename の
    同一 top_scope 内最近傍へフォールバックする＝2段目）。

    `include_path` は区切りを `/` に正規化してから渡される想定（アナライザ入口側・§4(a)）だが、
    ここでも防御的に `\\`→`/` を **`normpath` の前に**行う——`posixpath.normpath` は `\\` を
    区切りとして扱わないため、後から置換しても `..` の畳み込みが正しく行われない。
    """
    base_dir = ref_rel.rsplit("/", 1)[0] if "/" in ref_rel else ""
    joined = f"{base_dir}/{include_path}" if base_dir else include_path
    candidate = posixpath.normpath(joined.replace("\\", "/"))
    if _top(candidate) != _top(ref_rel):
        return None
    return candidate if rel_name.get(candidate) == (kind, name) else None


def _aggregate_pass2_edges(raw_edges: list) -> list:
    """§4(f)（アナライザ拡張・エッジ集約規則）: 同一 `(src, type, dst)` の複数候補を1本へ集約する。

    `analyzer_registry.via_priority_rank`（FW 固有 via を汎用 via より優先）で採用する候補を選び、
    同順位は初出（最初に見つかった候補）を採用する——line/via ともに**採用した候補から丸ごと**
    引き継ぐ（一部だけ混ぜない・§4(f) 是正③）。`(src,type,dst)` の初出順を保って返す。
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
    """Document ノードの同一性＝**パス**（03-鏡モデル.md・D1b-2）。言及エッジ（Pass3）の言及元
    Document ノードが、文書ごとに揺れうる表題ではなく rel_path で同一ノードに収束するための cid。"""
    return f"document:{world_id}:{rel}"


# --- （2026-09-04-グラフのソース正典化.md §2）: 辞書突合→言及エッジ（Pass3） ---
# アナライザ（不変・K2）が Pass1 で作る defs 索引（(label,name)->[rel,...]）を**そのまま辞書**として
# 再利用し、資料文書（`branch=="office"`）の本文と決定的（LLM ゼロ）に突合する。
# `Document -DOCUMENTS(via="mention")-> コードノード` を張る——`DOCUMENTS` は既存の型（`CORRESPONDS_TO`
# と同族）で影響 traversal（`_IMPACT_REL`）に含まれない＝規律は自動で満たされる。

MENTION_SCHEMA_VERSION = 3   # 突合仕様の版（worker._sig の材料。仕様変更時に既存 world を素通りさせない）
                              # v2（S2-LEAFNAME）: 修飾名（cid_key）を持つ子定義も単純名（表示名）で
                              # 辞書突合できるようにした（後述 `_mention_dictionary` 参照）。
                              # v3: トークン文字集合をアナライザの
                              # COBOL 識別子文字集合（`static_analysis._PROGRAM_ID` 等の `[A-Z0-9#@$-]`）
                              # と揃えた（`#@$` を追加）——`BILL@01` のような識別子が `BILL`/`01` に
                              # 分割され、無関係な定義へ誤って言及リンクしていた穴を塞ぐ。

_MENTION_TOKEN_RE = re.compile(r"[A-Za-z0-9_#@$-]+")


def _mention_tokenize(text: str) -> list:
    """文書テキスト→識別子形トークン（`[A-Za-z0-9_-]+` の最大連続・1パス・K4①）。

    突合専用の正規化（本関数が唯一の定義・K4②）: **生値のまま**（大文字小文字を区別する）。
    `identifiers.normalize_code_name` は `upper()` するため使わない——大小区別の突合と矛盾する
    （`DATA`/`data` のような誤リンクは逆引きを汚染するため fail-closed 側に倒す・K4②）。
    重複トークンは初出順で1つにまとめる（部分文字列検索はしない＝トークン全体一致のみ・
    1文書内の探索・上限判定を軽くするだけで突合の意味は変えない）。
    """
    seen: set = set()
    out: list = []
    for m in _MENTION_TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """security-limit 系 env の整数解析（`world_neo4j._env_int` と同一セマンティクス）。

    重複の理由も同じ: 下位層モジュール（本モジュール）が上位（`world_neo4j`）を import すると
    循環 import になるため、6行のヘルパーをここに複製する。負値/非整数、および範囲 [lo, hi] 外の
    値は既定へフォールバックする（既定値自体も [lo, hi] へクランプ）。
    """
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


def _mention_min_len() -> int:
    """辞書突合の名前長下限（既定4・env `SHERPA_MENTION_MIN_LEN`・範囲1〜64・裁定3）。"""
    return _env_int("SHERPA_MENTION_MIN_LEN", 4, 1, 64)


def _mention_max_per_doc() -> int:
    """1文書あたりの言及エッジ上限（既定200・env `SHERPA_MENTION_MAX_PER_DOC`・裁定6）。"""
    return _env_int("SHERPA_MENTION_MAX_PER_DOC", 200, 1, 100_000)


def _mention_eligible(name: str, min_len: int) -> bool:
    """名前が辞書突合の対象になり得るか。

    ①長さ下限未満、②`_MENTION_TOKEN_RE` の1トークンとして丸ごと一致しない名前（例:
    コピーブック子項目の修飾名 `GROUP.LEAFNAME`——`.` を含み `_mention_tokenize` が
    構造的に複数トークンへ分断するため、資料文書のどんなテキストからも単一トークンとして
    出現し得ない）は、辞書に載せても絶対に突合しない死重みなので除外する。"""
    return len(name) >= min_len and _MENTION_TOKEN_RE.fullmatch(name) is not None


def _mention_dictionary(defs: dict, aliases: dict | None = None, *, min_len: int = 1) -> tuple[dict, int]:
    """`defs`（Pass1 の定義索引 `(label,key)->[rel,...]`）→ 言及突合の辞書 `name->[(label,rel,key),...]`。

    `min_len`: 突合され得ない名前（長さ下限未満／`_mention_tokenize` が
    決して1トークンとして生成しない修飾名）を**辞書構築の時点で**除外する——文書側の
    トークンを都度 `min_len` で足切りするだけだと、辞書自体には短い名前/修飾名がそのまま残り、
    (a) 定義が全て短名だけの world でも辞書が非空になり文書走査がスキップされない、
    (b) 突合し得ない修飾名の同世代衝突まで `ambiguous_alias_count` に数えてしまう、の2点で
    無駄・誤カウントを生む。ここで先に落とすことで両方解消する（辞書構築後の
    突合結果自体は同値——除外される名前はそもそも一致し得なかったもののみ）。

    **同一 top_scope（世代）内に同名の定義が複数あるものは曖昧＝その世代は除外**（K4④・名前解決と
    同じ流儀・ラベルは問わない＝同一世代で異なるラベルが同名でも曖昧）。**世代が違う同名は
    同一論理実体の各世代＝曖昧ではない**——名前一致する全世代の定義それぞれを辞書に残す（K5）。

    `key` は `defs` のキーそのもの（`DefItem.cid_key` を含み得る修飾名）——ノードの cid は常に
    この `key` で組み立てられているため、辞書引きに使った文字列（トークン／単純名）とは別に
    保持して返す（S2-LEAFNAME）。

    `aliases`（省略可・`(label,simple_name)->[(rel,key),...]`）: コピーブックの子項目
    （`GROUP.ITEM` のような修飾名）のように `cid_key`（辞書突合のトークン化では `.` で分断され
    構造的に一致しえない）と表示名（単純名）が異なる定義を、**表示名でも**辞書に登録する
    （03-鏡モデル.md §2.4 追記）。表示名バケットは修飾名バケットと別キーなので同一定義が
    二重にカウントされることはない——`build_world` 側は `key != name` のときだけ渡す。

    戻り値: `(mdict, ambiguous_alias_count)`。`ambiguous_alias_count` は `aliases` 経由で登録された
    単純名のうち、いずれかの世代で同名衝突（曖昧）により張れなかった名前の数
    （world 単位の1件の flag 申告用・実測目的）。
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
    """言及元 Document ノードを get-or-create（同一性＝パス・`_document_cid` と同じ規約）。

    複数の言及がある場合でも同一 cid の既存ノードをそのまま再利用する
    （Document の同一性契約そのもの）。
    """
    cid = _document_cid(world_id, rel)
    if cid in nodes:
        return cid
    meta = _scope_meta(rel)
    nodes[cid] = {"cid": cid, "label": "Document", "name": rel, "world_id": world_id,
                 "top_scope": _top(rel), "phase": meta.get("phase"), "category": meta.get("category"),
                 "path": rel, "scope_path": "/".join(rel.split("/")[:-1]),
                 "value": None, "extraction_method": "static", "status": "active"}
    return cid


def _mention_edges_for_doc(rel: str, text: str, mdict: dict, min_len: int, max_per_doc: int,
                           world_id: str, nodes: dict, edges: list, flags: list) -> None:
    """1文書分の言及突合: トークン化→辞書突合→`Document -DOCUMENTS(via=mention)-> コード` を張る。

    1文書あたりの上限（`max_per_doc`）で安全弁をかける——超過分は張らずに件数だけ
    `flags`（`mention_overflow`）へ申告する（黙って切り捨てない）。K5 で1トークンが複数世代へ
    展開される場合も**エッジ**単位でカウントする。

    `targets` の各要素は `(label, rel, key)`——`key` は dst cid の組み立てに使うノードの実際の
    識別子（修飾名を含み得る）で、トークン文字列 `tok` とは別物（S2-LEAFNAME・トークンをそのまま
    dst cid に使うと存在しないノードを指してしまう）。同一 `(doc, dst)` は1本にまとめる——
    修飾キーと単純名の両方の辞書エントリが同じ定義を指す場合の重複エッジを防ぐ。
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
                         "doc": rel, "line": 0, "extraction_method": "static", "status": "active",
                         "via": "mention"})
            seen_dst.add(dst_cid)
            added += 1
    if overflow:
        flags.append({"reason": "mention_overflow", "doc": rel, "count": overflow})


def _mention_pass(world_dir, world_id: str, defs: dict, aliases: dict, files, nodes: dict,
                  edges: list, flags: list) -> None:
    """Pass3: 資料文書（`branch=="office"`）を辞書と突合し言及エッジを張る。

    世界にコード定義が無ければ辞書が空＝文書列挙自体を省く（コスト・flags 双方の無駄を避ける）。
    ソース原文（`branch=="source"`）は突合対象外（§2・裁定5）。

    `aliases`（`build_world` が Pass1 で集めた `(label,simple_name)->[(rel,key),...]`）は
    `_mention_dictionary` へそのまま渡す——単純名でも同名衝突があれば world 単位で1件
    `mention_ambiguous_names` を申告する（実測目的・S2-LEAFNAME）。

    `worlds.pin_world_root` で `world_id→world_dir` の解決をこの `build_world` 呼び出しが受けた
    実際の root に固定する: `corpus_docs.iter_world_documents` へは `root=world_dir` を直接渡すが、
    `doc_text.read_world_doc_text` は `md_path` を持たない文書で内部的に `worlds.world_dir(world_id)`
    を呼ぶため、レジストリ未登録/別 root での呼び出し（テスト fixture・`world_id` がまだ登録されて
    いない preview 等）でも同じ物理 root を確実に見る（`pin_world_root` の既存規律と同じ）。
    """
    min_len = _mention_min_len()
    max_per_doc = _mention_max_per_doc()
    mdict, ambiguous_count = _mention_dictionary(defs, aliases, min_len=min_len)
    if ambiguous_count:
        flags.append({"reason": "mention_ambiguous_names", "count": ambiguous_count})
    if not mdict:
        return
    with worlds.pin_world_root(world_id, world_dir):
        docs = corpus_docs.iter_world_documents(world_id, include_rag=grep_tool.rag_grep_enabled(),
                                                root=world_dir, files=files)
        for d in docs:
            if d.get("branch") != "office" or d.get("state") != "ready":
                continue
            rel = d["name"]
            text = doc_text.read_world_doc_text(world_id, d)
            if text is None:                          # 読めない＝スキップ（黙って落とさない・裁定5）
                flags.append({"reason": "unreadable_mention_doc", "doc": rel})
                continue
            _mention_edges_for_doc(rel, text, mdict, min_len, max_per_doc, world_id, nodes, edges, flags)


def build_world(world_dir, world_id: str, *, files=None):
    """世界（登録ディレクトリ）を `(nodes, edges, flags)` に。パス同一性＋同 top_scope 内最近傍解決。

    骨格（Pass1/Pass2＝COPIES/CONTAINS/INVOKES/ACCESSES）＋言及エッジ（Pass3・辞書突合）のみを
    決定的に構築する（S3・K9-K11＝意味層フル抽出・REALIZES 橋は撤去済み）。

    `files`（省略可・キーワード専用）: 呼び出し側が既に `scope_infer.safe_files(world_dir)` を1回
    materialize（`list(...)`）済みなら渡す——与えられれば再度歩かない（`_重要度.txt` はここで
    除外する＝呼び出し側が渡す `files` には含めてよい・S工事③是正・`preview_service.build_preview`
    参照）。省略時は従来どおりここで直接歩く。
    """
    entries = files if files is not None else scope_infer.safe_files(world_dir)
    # 秘匿ファイル（.env 系・id_rsa 系・credentials 等＝名前規約を含む）はグラフ取り込みでも読まない。
    # 台帳・grep・精読は classify_document で外れるが、Pass1 は拡張子で候補を引くため別に塞ぐ
    # （上流構成でも `.env.yaml`／`.env.sh` は YAML／shell アナライザの候補になる）。
    files = [(rp, rel) for rp, rel in entries
             if not importance.is_importance_control_path(rel)
             and not text_kind.is_sensitive(PurePosixPath(rel).name, PurePosixPath(rel).suffix.lower())]

    defs: dict = {}            # (label, NAME) -> [rel, ...]
    qualified_defs: dict = {}  # (label, cid_key) -> [(rel, 実名), ...]（cid_key が付く定義は常時登録）
    rel_name: dict = {}        # rel -> (label, NAME)  ＝ファイルの主体名（next() 廃止）
    texts: dict = {}           # rel -> (text, analyzer)
    nodes: dict = {}           # cid -> node
    edges: list = []
    link_edges: list = []      # Pass2 の解決済みエッジ（§4(f) 集約前のステージング）
    flags: list = []
    # 言及突合の単純名エイリアス（S2-LEAFNAME）: (label, DefItem.name) -> [(rel, DefItem.key), ...]。
    # `key`（cid_key）が `name`（表示名）と異なる子定義（例: コピーブックの `GROUP.ITEM`）だけを
    # 登録する——`key` はそのまま構造解決 `defs` のキーとして使われ続けるので触らず、Pass3 の
    # 辞書突合だけ単純名でも引けるように別枠で足す（`_mention_dictionary` 参照）。
    mention_aliases: dict = {}
    # 手続き型言語の関数呼び出し解決専用の索引（§9）: (analyzer_name, label, 単純名) ->
    # [(rel, cid_key, c_kind|None), ...]。children は `defs` に cid_key（修飾名）でしか登録されない
    # （primary の src 固定と cid 組み立ての整合を保つため）——`_link` が通常解決（`defs`）で
    # unresolved のときだけ2段目として参照する。索引キーに `analyzer_name` を含めることで、
    # C・VB のように複数アナライザが `resolves_calls_by_simple_name` を持つ場合でも、たまたま
    # 同名の単純名が異なる言語のアナライザ間で誤接続しない（登録・参照とも同一アナライザ内だけで
    # 解決する）。参照側も同じアナライザ由来かつ `via=call` のときだけに限定する。
    simple_name_defs: dict = {}
    # A9（`via=config_key`）専用の索引: (label, key_kind, 裸キー) -> [(rel, cid_key), ...]。
    # properties/YAML/XML 設定のキー child（`label=="Config"` かつ `cid_key` に `"key:"` 接頭辞・
    # `_register_children` 参照）だけを登録する——`_link_config_key_all` はこれだけを見て
    # primary（`defs`）候補を混ぜない（primary と同名の裸キーがあっても primary には張らない）。
    # `key_kind`（`child.extra.get("key_kind")`・Config キーは種別で名前空間を
    # 分ける："property"/"bean"/"action"/"mapper"/"url"/"env"）を索引キーへ含めることで、
    # たまたま同じ裸キー文字列を持つ bean と property 等が誤って同一視されない。参照側
    # （`RefCandidate.extra["key_kind"]`）にも定義側にも `key_kind` が無い場合は `None` 同士
    # だけが一致する（タプル比較の自然な帰結・フェイクアナライザを使う既存テストとの後方互換）。
    config_key_index: dict = {}

    def _def(label, name, rel):
        defs.setdefault((label, name), []).append(rel)
        rel_name[rel] = (label, name)

    def _index_def(label, name, rel):
        """`defs` 解決索引にだけ登録する（`rel_name` は更新しない）。

        `_def` は「このファイルの主体」を兼ねて `rel_name[rel]` も書き換えるため、children に
        そのまま使うと後勝ちで primary の src 決定（Pass2 の `rel_name.get(rel)`）を壊す。
        children（JAVA-1 残課題#3・非 public 兄弟型等）は解決対象にはしたいが主体ではないため、
        索引登録だけを行う専用ヘルパーにする。
        """
        defs.setdefault((label, name), []).append(rel)

    def _index_qualified(label, cid_key, rel, actual_name):
        """完全修飾名を解決索引へ追加登録する（`defs` とは別枠）。

        `cid_key` が付いているものは常に登録する——children は cid が `.key`（＝`cid_key` が
        設定されていればその値）で組み立てられるため（`child_cid = _cid(..., child.key)`）、
        `cid_key == actual_name` は children では常に成立する（`DefItem.key` が `cid_key` をそのまま
        返す・`_base.py`）。`_resolve_qualified` は `qualified_defs` だけを見るため、ここで
        「一致するなら skip」してしまうと children の完全修飾名参照は常に登録漏れになり、
        `unresolved` に落ちたあとの単純名フォールバックも `defs`（cid_key キー）と噛み合わず
        解決できない。`actual_name` は常に cid 構築に使った値そのものなので、登録を条件なしに
        行っても `_cid(kind, world_id, rel, resolved_name)` の整合は崩れない。
        """
        if cid_key is not None:
            qualified_defs.setdefault((label, cid_key), []).append((rel, actual_name))

    def _sanitized_extra(analyzer_name, rel, label, name, base_keys, extra):
        """`DefItem.extra` から共通層が確定したフィールドと同名のキーを除去する（黙って上書きさせない）。

        アナライザは `extra` で任意プロパティを足せるが、`cid`/`label`/`name`/`analyzer` 等
        共通層が算出した既存キーまで上書きできてしまうと、来歴（誰が解析したか）や識別子を
        偽装できてしまう。1つでも衝突したら `extra` を**丸ごと**捨てて `flags` に理由付きで記録する
        （衝突していない他のキーだけ部分採用しない＝「ここまでは信じてよい」という誤った安心感を
        与えない）。
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
        """`parent -CONTAINS-> child` を1本ずつ生成する共通処理（primary の `children` と
        `DefResult.extras` 各グループの `children` の両方で使う・アナライザ拡張 A10）。"""
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
            _index_def(child.label, child.key, rel)   # JAVA-1 残課題#3: children も解決対象にする
            _index_qualified(child.label, child.cid_key, rel, child.key)   # child.key は cid_key 設定時それと同値
            if child.key != child.name:                # 修飾名≠表示名＝言及辞書に単純名でも登録（S2-LEAFNAME）
                mention_aliases.setdefault((child.label, child.name), []).append((rel, child.key))
                if _simple_name_calls(analyzer_name):
                    # 手続き型言語の関数呼び出し解決専用（§9・`Analyzer.resolves_calls_by_simple_name`）: `defs` は children を cid_key（修飾名）でしか
                    # 索引しない（`_index_def(child.label, child.key, rel)`・上記）ため、呼び出し側が
                    # 単純名（例: `util_add`）を渡す `_resolve_nearest(defs, ...)` は解決できない——
                    # `_index_qualified` の「cid_key==actual_name なら常に登録される」という別の抜け穴
                    # （reference_qualified_index_children_key_equals_cid_key_gap）と同根の穴。
                    # `simple_name_defs`（`(analyzer_name,label,単純名)->[(rel,実際のcid_key,c_kind|None),...]`）
                    # を専用の索引として別枠で持ち、`_link` が `defs` 解決失敗（unresolved）時のみ
                    # 2段目として参照する（`mention_aliases` は Pass3 辞書突合専用のため転用しない・
                    # 別枠にする）。`c_kind`（`child.extra["c_kind"]`・c.py が付与）は定義（`.c`）を
                    # 宣言（`.h`）より優先するための材料（`_resolve_nearest_keyed` 参照）。索引キーに
                    # `analyzer_name` を含めることで、C・VB のように複数アナライザが同じ索引へ
                    # 書き込んでも、他言語の unresolved 参照が偶然の同名一致で誤接続しない
                    # （言語混在 world での誤接続防止・波3 統合 RV）。
                    simple_name_defs.setdefault((analyzer_name, child.label, child.name), []).append(
                        (rel, child.key, child.extra.get("c_kind")))
            if child.cid_key is not None and child.cid_key.startswith("key:"):
                # A9 config_key 専用索引（properties/YAML/XML 設定のキー child のみ）。key_kind
                # で名前空間を分ける（未設定は None・`_link_config_key_all` 参照）。
                config_key_index.setdefault(
                    (child.label, child.extra.get("key_kind"), child.name), []).append((rel, child.key))
            edges.append({"type": "CONTAINS", "src": parent_cid, "dst": child_cid, "doc": rel,
                          "line": child.line, "extraction_method": "static", "status": "active"})

    # --- Pass 1: 定義収集＋ノード（拡張子→アナライザを引いて collect_defs を呼ぶ汎用ループ・
    # 言語ごとの分岐は sherpa.ingest.analyzers 配下のクラスへ移設済み）---
    for rp, rel in files:
        candidates = analyzer_registry.candidates(rel)
        if not candidates:                                # どのアナライザも拡張子を担当しない＝資料
            continue
        # サイズ上限（8MiB・grep 上限と同じ・単一の真実源＝`text_kind.MAX_BYTES`）: 登録アナライザ
        # 全般（cobol/copybook/jcl/java/xml_config/properties/yaml_config）に一律で適用する——
        # `corpus_docs.classify_document` の accepts() 判定は既定 accepts のアナライザでは内容を
        # 読まない（実ファイルサイズも見ない）ため、ここで読み飛ばさないと本ループの
        # `corpus_docs.read_full_text_and_raw()` が巨大ファイルを全量メモリに読み込み、単一 worker を
        # 1ファイルで OOM させ得る。8MiB 超のソースは言語を問わず実務上あり得ない前提
        # （`corpus_docs._text_oversize` と同じ前提）。
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
            # `corpus_docs.classify_document` は既定 accepts のアナライザでは内容を読まないため
            # 検知できない——実際に読み込む Pass1 だけがこの失敗を確実に検知できる。blocked flag は
            # `worker._run_locked` の既存チェックにより台帳書込・Neo4j 反映へ進ませず、正しい sig への
            # 確定も行わせない（部分グラフを確定しない・復旧後の次回 sync で全再構築される）。
            flags.append({"doc": rel, "reason": "unreadable_code_file", "action": "blocked"})
            continue
        # `accepts()` に渡す head サイズはアナライザごとの宣言（`Analyzer.head_bytes`・既定4KiB）に
        # 従う。`text[:head_bytes]` の**文字**数切り詰めはマルチバイト文字を含む文書で
        # 実際のバイト範囲が宣言より広がってしまう（`corpus_docs._read_head` docstring 参照）ため、
        # 上の読み取りが返した生バイト列 `raw` を `head_bytes` バイトちょうどでスライス・デコードする
        # （`rp` を再度開き直さない——ファイルを2回開くと、全文は読めたのに間でファイルが消える/
        # 権限が変わるなどの TOCTOU で head 側だけ失敗しうる。`raw` は既に読み終えたバイト列なので
        # スライス・デコードは失敗しない）。
        def _head_for(a, raw=raw):
            return raw[:getattr(a, "head_bytes", 4096)].decode("utf-8", errors="replace")
        analyzer = next((a for a in candidates if a.accepts(rel, _head_for(a))), None)
        if analyzer is None:                              # 拡張子は一致するが内容判定で不採用
            continue
        # 受理済み（拡張子一致＋accepts 通過）なら主体の有無に関わらず Pass2 を通す——JOB を持たない
        # JCL PROC ファイルのような「主体なしファイル」も dropped_syntax 検知の対象にする。
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

        # アナライザ拡張 A10: 同一ファイル内の主体以外のトップレベル定義（DDL の2件目以降の
        # `CREATE TABLE` 等）。primary と同じ規則でノード化・索引登録するが、`_index_def`
        # （`_def` ではない）を使い `rel_name[rel]` は更新しない——ファイルの主体（Pass2 の
        # 参照元）は引き続き1つのまま。
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
            if item.key != item.name:                  # 修飾名≠表示名＝言及辞書に単純名でも登録（S2-LEAFNAME）
                mention_aliases.setdefault((item.label, item.name), []).append((rel, item.key))
            _register_children(extra_cid, group.children, rel, analyzer.name)

    # --- Pass 2: 参照解決（同 top_scope 内 最近傍）＋構造エッジ ---
    def _apply_extra(edge, etype, ref_rel, analyzer_name, extra):
        """`RefCandidate.extra`（CODE-2・JAVA-1 残課題#4）を解決後のエッジへ加算的に透過する。

        細分ラベル `via` は既知値（`KNOWN_VIA`）のみ通す——未知値は Dropped と同様に flags へ
        記録し、その属性だけを落とす（構造の事実＝エッジ自体は張る・黙って新値を増やさない）。
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
        """A9（アナライザ拡張）: `via=config_key` の参照だけ、同一 top_scope 内の同名
        `Config` キー全件へ1本ずつエッジを張る特例——通常の最近傍/ambiguous 判定を迂回する
        （環境別設定ファイル・application-dev/prod 等が同名キーを持つ場合に両方へ張るため）。

        child の cid は `cid_key`（`DefItem.key`）で組み立てられる（`_register_children`）。
        properties/YAML のキー child のように `cid_key` が表示名（`name`）と異なる（`"key:"` 接頭辞・
        primary との自己ループ回避）ため、`config_key_index`（`_register_children` が同じ場所で
        `"key:"` 接頭辞を持つ Config child だけを集めた専用索引）だけを候補源にする——`defs`
        （primary も含む構造解決索引）は混ぜない。primary と同名の裸キーが存在しても、この索引には
        Config child しか登録されないため primary へは張られない。
        dst cid は必ず**一致した候補の cid_key**で組み立てる（rel ごとに異なり得る）——裸の `name`
        でそのまま組み立てると、properties/YAML 側の名前空間分離が効かず別ノード（file primary 等）
        の cid と衝突し得る（RV波1是正）。

        `key_kind`: 参照側 `extra.get("key_kind")` も索引キーに含める——
        `config_key_index` の登録側（`_register_children`）と同じタプル形 `(label, key_kind, name)`
        で引くため、`key_kind` が無い（`None`）参照は `key_kind` が無い定義としか一致しない
        （タプル比較の自然な帰結・フェイクアナライザを使う既存テストとの後方互換）。
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
                   "doc": ref_rel, "line": line, "extraction_method": "static", "status": "active"}
            _apply_extra(edge, etype, ref_rel, analyzer_name, dict(extra))
            link_edges.append(edge)

    def _link(etype, src_cid, kind, name, ref_rel, line, analyzer_name=None, extra=None, reverse=False):
        extra = dict(extra) if extra else {}
        # `qualified` は解決の指示であってエッジの事実ではない——共通層が消費して取り除く
        # （残っていると `_apply_extra` がそのまま edge のプロパティへ透過してしまう）。
        qualified = bool(extra.pop("qualified", False)) and "." in name

        if extra.get("via") == "config_key":              # A9: 通常解決の前に特例へ分岐
            _link_config_key_all(etype, src_cid, kind, name, ref_rel, line, analyzer_name, extra, reverse)
            return

        resolved_name = name
        include_path = extra.get("include_path") if extra.get("via") == "include" else None
        if include_path and "/" in include_path:
            # C の `#include`（§4(a)/§12）1段目: 相対パス完全一致（同一 top_scope 内）。
            # 見つからなければ2段目（拡張子込み basename の最近傍）へフォールバックする——
            # `qualified` の完全一致→単純名フォールバックと同型の2段構成だが、解決の材料が
            # cid_key ではなく `include_path`（パス文字列）である点が異なるため専用分岐にする。
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
            if status:                                    # 完全一致なし（unresolved/cross_scope）＝単純名へフォールバック（§4(c)'）
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
                # 手続き型言語（C/VB）の関数呼び出し解決専用（§9）: `defs` は children を
                # cid_key（修飾名）でしか索引しないため、単純名（例: `util_add`）は通常解決で
                # 見つからない。`simple_name_defs` を2段目として参照し、その判定結果
                # （解決/ambiguous/cross_scope/unresolved）をそのまま採用する——1段目の `status`
                # （"unresolved" 固定）で上書きせず2段目の判定を伝播する（2段目が ambiguous でも
                # "unresolved" に化けさせない）。索引キーに `analyzer_name` を含むため、参照側も
                # 同じアナライザ由来かつ `via=call` のときだけに限定する——他言語からの unresolved
                # 参照（例: Java の `new Worker()`）まで2段目に倒すと、C・VB のように複数アナライザが
                # `resolves_calls_by_simple_name` を持つ場合にたまたま同名の関数へ誤接続し得る
                # （言語混在 world での誤接続防止・登録側の限定と対をなす）。
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
               "doc": ref_rel, "line": line, "extraction_method": "static", "status": "active"}
        _apply_extra(edge, etype, ref_rel, analyzer_name, extra)
        link_edges.append(edge)

    for rel, (rp, analyzer, raw_sha1) in texts.items():
        # Pass1 は本文を保持しない（`texts[rel]` は `(rp, analyzer)` のみ）——world 全体のコード
        # 総量に比例したメモリを Pass1〜Pass2 間で同時保持しない（単一 worker・100GB 級コーパス
        # 前提）。1 ファイルあたり Pass1 で1回・Pass2 で1回の計2回読む（Pass3 の都度読み直しと
        # 同じ流儀）。Pass1 で accepts() 済み＝通常は再読取も成功するが、取り込み中の削除/権限変更
        # 等で失敗しうるため Pass1 と同じ fail-closed（blocked flag・部分グラフを確定しない）で扱う。
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
            # Pass1 と Pass2 の間に原本が書き換わった＝Pass1 の定義（旧本文）と Pass2 の参照（新本文）を
            # 混ぜると存在しない依存を作る。黙って通さず blocked（次回 sync で全再構築）。
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

    # §4(f)（アナライザ拡張）: 同一 (src,type,dst) の複数候補を1本へ集約してから確定する
    # （FW 固有 via を汎用 via より優先・line は採用した via の出現行のまま）。全 Pass2 エッジに適用
    # する——nodes/flags は不変のまま、edges は (src,type,dst) の集合は不変で本数だけ重複の分減り得る
    # （§8 受け入れ条件・golden 固定＝tests/unit/test_world_graph_analyzer_expansion_common.py）。
    edges.extend(_aggregate_pass2_edges(link_edges))

    # Pass2 完了直後にコード本文を解放する——Pass3（`_mention_pass`）
    # は `corpus_docs.iter_world_documents`/`doc_text.read_world_doc_text` 経由で資料文書
    # （`branch=="office"`）の本文を都度読み直す独立した経路で、この `texts` 辞書（Pass1 で読んだ
    # コード全文）を参照しない。大きい world ではコード全文を Pass3 の間も保持し続けるだけ無駄
    # （メモリの早期解放）。
    texts.clear()

    # Pass3: 辞書突合→言及エッジ（S2・単純名エイリアス込み＝S2-LEAFNAME）
    _mention_pass(world_dir, world_id, defs, mention_aliases, files, nodes, edges, flags)

    return list(nodes.values()), edges, flags


def subgraph(nodes, edges, prefix: str | None = None):
    """**範囲フィルタ**＝`path` prefix（top_scope/phase…どの階層でも）で部分グラフに絞る（MIRROR §3）。

    `prefix=None`/`''` は全体。両端点が範囲内のエッジだけ残す（traversal を Cypher で絞るのと同じ意味の in-memory 版）。
    """
    if not prefix:
        return list(nodes), list(edges)
    pref_top = prefix.split("/", 1)[0]

    def _in(n):
        p = n.get("path")
        if p:                                            # ファイル由来＝path prefix
            return p == prefix or p.startswith(prefix + "/")
        sp = n.get("scope_path")
        if sp:                                           # 概念（定義 doc あり）＝scope_path prefix（深い階層でも正しい）
            return sp == prefix or sp.startswith(prefix + "/")
        return n.get("top_scope") == pref_top            # doc 無し概念＝世代で所属

    keep = [n for n in nodes if _in(n)]
    cids = {n["cid"] for n in keep}
    sub_edges = [e for e in edges if e["src"] in cids and e["dst"] in cids]
    return keep, sub_edges


def _lstat_kind(p) -> str | None:
    """`os.lstat()` ベースで種別を返す（`"dir"`/`"file"`/`"symlink"`/`None`＝不在扱い）。

    `Path.is_dir()`/`is_file()`/`is_symlink()` は内部で `OSError` を握って `False` を返すため
    使わない（`scope_infer._lstat_kind`/`worlds._lstat_kind` と同じ設計）。`resolve_path` は
    fail-closed（見えなければ「無い」）でよい経路のため、`OSError` は種別不明＝`None` に潰す。
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
    """`rel`（world root 相対 POSIX パス）の文字列検証だけを行う（FS アクセス無し）。

    絶対パス・`\\`・NUL・空/`.`/`..` 要素はすべて拒否する（`\\` はファイル名内に含まれ得ても
    POSIX rel 契約を優先して拒否する意図的な制限・NUL は `os.lstat` 等に渡すと `ValueError` に
    なり得るため事前に弾く）。通れば `"/"` 区切りのセグメント列を返す。

    `resolve_path`（内容判定・FS 解決）と `ext_api._doc_path_segments`（原本DL配信・非FS）が
    **この1関数だけ**を正規化の真実源として共有する——2箇所で別々に検証条件を書くと、片方だけ
    緩い/厳しいまま個別に直されて再びズレる（`.` 要素だけ片方が許容していた実害＝内容判定は
    「読み取り不可」扱いで通すのに配信側は実ファイルを開いて返してしまう秘匿ファイル漏洩の穴に
    なっていた）。
    """
    if not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        return None
    parts = tuple(rel.split("/"))
    if any(p in ("", ".", "..") for p in parts):
        return None
    return parts


def resolve_path(world_dir, rel: str):
    """`rel`（world root 相対 POSIX）→ 原本 Path（**パス基準**・無ければ None）。

    root から `rel` の各階層へ直接 `os.lstat` して降りるだけで解決する——world 全体を走査しない
    （コストは `rel` の階層数のみに依存・世界内のファイル総数に依存しない）。
    途中経路のどれか1つでも symlink なら拒否する（`safe_files` が symlink file/dir を辿らず
    実在扱いしないのと同じ contract＝symlink 越しに同じ内容へ辿り着けても document とは認めない）。
    `rel` の検証（絶対パス・`\\`・NUL・空/`.`/`..` 要素の拒否）は `valid_rel_parts` に集約する
    （一切の FS アクセスより前に文字列だけで判定する）。
    最後に解決後パスが world root 配下に収まることを再確認する（脱出防止の多層防御）。
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
