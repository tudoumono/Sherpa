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

S3（2026-09-04-グラフのソース正典化.md §4・K9-K11）: 意味層フル抽出（L 抽出・`_load_semantic`）・
REALIZES 橋（`_load_concepts`/`_load_auto_concepts`・手動/自動）は概念ごと撤去。事前計算に残すのは
決定的（再現100%）に計算できる構造（骨格＝Pass1/Pass2＋言及エッジ＝Pass3）だけ——業務語からコードへの
入口はクエリ時のエージェント（文書 grep→辞書ノード）に委ねる（§2）。

Pass3（S2・2026-09-04-グラフのソース正典化.md §2）: Pass1 の定義索引をそのまま辞書として資料文書
（`branch=="office"`）の本文と決定的に突合し、`Document -DOCUMENTS(via="mention")-> コード` を張る
（LLM ゼロ・影響 traversal 外・世代をまたいでよい制度化された例外＝`_mention_pass` 参照）。
"""
from __future__ import annotations

import json
import os
import re
import stat as stat_mod
from pathlib import Path

from .. import corpus_docs, doc_text, grep_tool, scope_infer, worlds
from . import importance
from .analyzers import registry as analyzer_registry
from .identifiers import normalize_code_name as _norm


def _scope_meta(rel: str) -> dict:
    """rel_path（POSIX）→ 検索スコープのメタ（top_scope/phase/category）。導出は scope_infer に集約（rv-full B3）。"""
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
        return None, "cross_scope"                              # 別世代にしか無い＝引かない（誤検出防止・RV High）
    ranked = sorted(same, key=lambda r: _tree_distance(ref_rel, r))
    best = _tree_distance(ref_rel, ranked[0])
    if sum(1 for r in same if _tree_distance(ref_rel, r) == best) > 1:
        return None, "ambiguous"                                # 同距離複数は任意選択しない
    return ranked[0], ""


def _document_cid(world_id: str, rel: str) -> str:
    """Document ノードの同一性＝**パス**（03-鏡モデル.md・D1b-2）。言及エッジ（Pass3）の言及元
    Document ノードが、文書ごとに揺れうる表題ではなく rel_path で同一ノードに収束するための cid。"""
    return f"document:{world_id}:{rel}"


# --- S2（2026-09-04-グラフのソース正典化.md §2・K3-K5）: 辞書突合→言及エッジ（Pass3） ---
# アナライザ（不変・K2）が Pass1 で作る defs 索引（(label,name)->[rel,...]）を**そのまま辞書**として
# 再利用し、資料文書（`branch=="office"`）の本文と決定的（LLM ゼロ）に突合する。
# `Document -DOCUMENTS(via="mention")-> コードノード` を張る——`DOCUMENTS` は既存の型（`CORRESPONDS_TO`
# と同族）で影響 traversal（`_IMPACT_REL`）に含まれない＝規律は自動で満たされる。

MENTION_SCHEMA_VERSION = 3   # 突合仕様の版（worker._sig の材料。仕様変更時に既存 world を素通りさせない）
                              # v2（S2-LEAFNAME）: 修飾名（cid_key）を持つ子定義も単純名（表示名）で
                              # 辞書突合できるようにした（後述 `_mention_dictionary` 参照）。
                              # v3（rv-s2-mention #3・2026-09-05）: トークン文字集合をアナライザの
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
    """名前が辞書突合の対象になり得るか（rv-s2-mention #4）。

    ①長さ下限未満、②`_MENTION_TOKEN_RE` の1トークンとして丸ごと一致しない名前（例:
    コピーブック子項目の修飾名 `GROUP.LEAFNAME`——`.` を含み `_mention_tokenize` が
    構造的に複数トークンへ分断するため、資料文書のどんなテキストからも単一トークンとして
    出現し得ない）は、辞書に載せても絶対に突合しない死重みなので除外する。"""
    return len(name) >= min_len and _MENTION_TOKEN_RE.fullmatch(name) is not None


def _mention_dictionary(defs: dict, aliases: dict | None = None, *, min_len: int = 1) -> tuple[dict, int]:
    """`defs`（Pass1 の定義索引 `(label,key)->[rel,...]`）→ 言及突合の辞書 `name->[(label,rel,key),...]`。

    `min_len`（rv-s2-mention #4）: 突合され得ない名前（長さ下限未満／`_mention_tokenize` が
    決して1トークンとして生成しない修飾名）を**辞書構築の時点で**除外する——以前は文書側の
    トークンを都度 `min_len` で足切りしていたため、辞書自体には短い名前/修飾名がそのまま残り、
    (a) 定義が全て短名だけの world でも辞書が非空になり文書走査がスキップされない、
    (b) 突合し得ない修飾名の同世代衝突まで `ambiguous_alias_count` に数えてしまう、の2点で
    無駄・誤カウントを生んでいた。ここで先に落とすことで両方解消する（辞書構築後の
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
    files = [(rp, rel) for rp, rel in entries
             if not importance.is_importance_control_path(rel)]

    defs: dict = {}            # (label, NAME) -> [rel, ...]
    rel_name: dict = {}        # rel -> (label, NAME)  ＝ファイルの主体名（next() 廃止）
    texts: dict = {}           # rel -> (text, analyzer)
    nodes: dict = {}           # cid -> node
    edges: list = []
    flags: list = []
    # 言及突合の単純名エイリアス（S2-LEAFNAME）: (label, DefItem.name) -> [(rel, DefItem.key), ...]。
    # `key`（cid_key）が `name`（表示名）と異なる子定義（例: コピーブックの `GROUP.ITEM`）だけを
    # 登録する——`key` はそのまま構造解決 `defs` のキーとして使われ続けるので触らず、Pass3 の
    # 辞書突合だけ単純名でも引けるように別枠で足す（`_mention_dictionary` 参照）。
    mention_aliases: dict = {}

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

    # --- Pass 1: 定義収集＋ノード（拡張子→アナライザを引いて collect_defs を呼ぶ汎用ループ・
    # 言語ごとの分岐は sherpa.ingest.analyzers 配下のクラスへ移設済み）---
    for rp, rel in files:
        candidates = analyzer_registry.candidates(rel)
        if not candidates:                                # どのアナライザも拡張子を担当しない＝資料
            continue
        try:
            text = rp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # 受理済み（拡張子が一致する）コード文書の実読込失敗は run 全体を失敗させる（fail-closed）。
            # `corpus_docs.classify_document` は既定 accepts のアナライザでは内容を読まないため
            # 検知できない——実際に読み込む Pass1 だけがこの失敗を確実に検知できる。blocked flag は
            # `worker._run_locked` の既存チェックにより台帳書込・Neo4j 反映へ進ませず、正しい sig への
            # 確定も行わせない（部分グラフを確定しない・復旧後の次回 sync で全再構築される）。
            flags.append({"doc": rel, "reason": "unreadable_code_file", "action": "blocked"})
            continue
        analyzer = next((a for a in candidates if a.accepts(rel, text[:4096])), None)
        if analyzer is None:                              # 拡張子は一致するが内容判定で不採用
            continue
        # 受理済み（拡張子一致＋accepts 通過）なら主体の有無に関わらず Pass2 を通す——JOB を持たない
        # JCL PROC ファイルのような「主体なしファイル」も dropped_syntax 検知の対象にする。
        texts[rel] = (text, analyzer)
        defres = analyzer.collect_defs(text, rel)
        _flag_dropped(analyzer.name, rel, defres.dropped)
        if defres.primary is None:                        # 構文にマッチせず主体を持たない
            continue
        if defres.primary.label not in analyzer_registry.NODE_LABELS:
            flags.append({"reason": "unknown_label", "analyzer": analyzer.name,
                          "label": defres.primary.label, "from": rel})
            continue
        _def(defres.primary.label, defres.primary.name, rel)
        prim_cid = _cid(defres.primary.label, world_id, rel, defres.primary.name)
        prim_base = {**_node(defres.primary.label, world_id, rel, defres.primary.name,
                             value=defres.primary.value), "analyzer": analyzer.name}
        prim_extra = _sanitized_extra(analyzer.name, rel, defres.primary.label, defres.primary.name,
                                      prim_base.keys(), defres.primary.extra)
        nodes[prim_cid] = {**prim_base, **prim_extra}
        for child in defres.children:
            if child.label not in analyzer_registry.NODE_LABELS:
                flags.append({"reason": "unknown_label", "analyzer": analyzer.name,
                              "label": child.label, "from": rel})
                continue
            child_cid = _cid(child.label, world_id, rel, child.key)
            child_base = {**_node(child.label, world_id, rel, child.name, value=child.value),
                          "cid": child_cid, "line": child.line, "analyzer": analyzer.name}
            child_extra = _sanitized_extra(analyzer.name, rel, child.label, child.name,
                                           child_base.keys(), child.extra)
            nodes[child_cid] = {**child_base, **child_extra}
            _index_def(child.label, child.key, rel)   # JAVA-1 残課題#3: children も解決対象にする
            if child.key != child.name:                # 修飾名≠表示名＝言及辞書に単純名でも登録（S2-LEAFNAME）
                mention_aliases.setdefault((child.label, child.name), []).append((rel, child.key))
            edges.append({"type": "CONTAINS", "src": prim_cid, "dst": child_cid, "doc": rel,
                          "line": child.line, "extraction_method": "static", "status": "active"})

    # --- Pass 2: 参照解決（同 top_scope 内 最近傍）＋構造エッジ ---
    def _link(etype, src_cid, kind, name, ref_rel, line, analyzer_name=None, extra=None):
        rel, status = _resolve_nearest(defs, kind, name, ref_rel)
        if status:                                       # ''=解決／ambiguous/cross_scope/unresolved は flag
            flags.append({"reason": status, "from": ref_rel, "kind": kind, "name": name})
            return
        edge = {"type": etype, "src": src_cid, "dst": _cid(kind, world_id, rel, name),
               "doc": ref_rel, "line": line, "extraction_method": "static", "status": "active"}
        if extra:
            # `RefCandidate.extra`（CODE-2・JAVA-1 残課題#4）を解決後のエッジへ加算的に透過する。
            # 細分ラベル `via` は既知値（`KNOWN_VIA`）のみ通す——未知値は Dropped と同様に flags へ
            # 記録し、その属性だけを落とす（構造の事実＝エッジ自体は張る・黙って新値を増やさない）。
            via = extra.get("via")
            if via is not None and via not in analyzer_registry.KNOWN_VIA:
                flags.append({"reason": "unknown_via", "analyzer": analyzer_name,
                              "from": ref_rel, "edge_type": etype, "via": via})
                extra = {k: v for k, v in extra.items() if k != "via"}
            bad = set(extra) & edge.keys()               # 共通層が確定した既存キーは上書きさせない
            if bad:
                flags.append({"reason": "reserved_key_in_extra", "analyzer": analyzer_name,
                              "from": ref_rel, "edge_type": etype, "keys": sorted(bad)})
            else:
                edge.update(extra)
        edges.append(edge)

    for rel, (text, analyzer) in texts.items():
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
                 analyzer_name=analyzer.name, extra=ref.extra)

    # rv-s2-mention #6（2026-09-05）: Pass2 完了直後にコード本文を解放する——Pass3（`_mention_pass`）
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


def resolve_path(world_dir, rel: str):
    """`rel`（world root 相対 POSIX）→ 原本 Path（**パス基準**・無ければ None）。

    root から `rel` の各階層へ直接 `os.lstat` して降りるだけで解決する——world 全体を走査しない
    （コストは `rel` の階層数のみに依存・世界内のファイル総数に依存しない）。
    途中経路のどれか1つでも symlink なら拒否する（`safe_files` が symlink file/dir を辿らず
    実在扱いしないのと同じ contract＝symlink 越しに同じ内容へ辿り着けても document とは認めない）。
    `rel` の検証は**一切の FS アクセスより前**に文字列だけで行う: 絶対パス・`\\`・NUL・空/`.`/`..`
    要素はすべて拒否する（`\\` はファイル名内に含まれ得ても POSIX rel 契約を優先して拒否する
    意図的な制限・NUL は `os.lstat` 等に渡すと `ValueError` になり得るため事前に弾く）。
    最後に解決後パスが world root 配下に収まることを再確認する（脱出防止の多層防御）。
    """
    if not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
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
