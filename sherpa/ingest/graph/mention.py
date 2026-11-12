"""Pass 3: 定義の索引を辞書にして資料文書の本文と決定的に突合し、言及エッジ（`DOCUMENTS via="mention"`）を張る。LLM は使わない。

設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

import bisect
import re

from ... import corpus_docs, doc_text, worlds
from .. import substage
from .aggregate import EDGE_SOURCES_MAX, cap_sources
from .select import top_folder
from .state import document_cid, make_cid, scope_meta


# --- 辞書突合→言及エッジ（Pass3） ---
# Pass1 の定義索引 `(label,name)->[rel,...]` をそのまま辞書として、資料文書（`branch=="office"`）の本文と
# 決定的（LLM なし）に突合し、`Document -DOCUMENTS(via="mention")-> コードノード` を張る。
# `DOCUMENTS` は影響 traversal（`_IMPACT_REL`）に含まれない。

MENTION_SCHEMA_VERSION = 4   # 突合仕様の版（worker._sig の材料。仕様変更時に既存の資料フォルダを素通りさせない）

_MENTION_TOKEN_RE = re.compile(r"[A-Za-z0-9_#@$-]+")


def mention_tokenize(text: str) -> list:
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


def mention_min_len() -> int:
    """辞書突合の名前長下限。"""
    return _MENTION_MIN_LEN


def mention_max_per_doc() -> int:
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
    - 同一トップフォルダ内に同名の定義が複数ある場合は曖昧として、そのトップフォルダは除外する（ラベルは問わない）。
      トップフォルダが違う同名は曖昧ではなく、それぞれ辞書に残す（張る先の絞り込みは `mention_edges_for_doc`）。
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
            by_name_gen.setdefault(key, {}).setdefault(top_folder(rel), []).append((label, rel, key))
    for (label, name), pairs in (aliases or {}).items():
        if not _mention_eligible(name, min_len):
            continue
        alias_names.add(name)
        for rel, key in pairs:
            by_name_gen.setdefault(name, {}).setdefault(top_folder(rel), []).append((label, rel, key))
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
    """言及元 Document ノードを get-or-create する（同一性＝パス・`document_cid` と同じ規約）。"""
    cid = document_cid(world_id, rel)
    if cid in nodes:
        return cid
    meta = scope_meta(rel)
    nodes[cid] = {"cid": cid, "label": "Document", "name": rel, "world_id": world_id,
                 "top_scope": top_folder(rel), "phase": meta.get("phase"), "category": meta.get("category"),
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


def mention_edges_for_doc(rel: str, text: str, mdict: dict, min_len: int, max_per_doc: int,
                           world_id: str, nodes: dict, edges: list, flags: list) -> None:
    """1 文書分の言及突合: トークン化→辞書突合→`Document -DOCUMENTS(via=mention)-> コード` を張る。

    言及元の文書と同じトップフォルダ（`top_folder`）の定義にだけ張る（設計: docs/03-鏡モデル.md §2.4）。
    1 文書あたりの上限（`max_per_doc`）を超えた分は張らず、件数を `flags`（`mention_overflow`）へ申告する。
    dst の cid は `targets` の `key`（修飾名を含み得る）で組み立てる。同一 `(doc, dst)` は 1 本にまとめ、文書内の言及の位置を
    根拠 `sources[].locator` に並べる（`doc` は言及元の文書・`line` は 0）。
    """
    added = 0
    overflow = 0
    doc_cid = None
    doc_top = top_folder(rel)
    seen_dst: set = set()
    pending: list = []                               # (tok, edge)
    for tok in mention_tokenize(text):
        if len(tok) < min_len:
            continue
        targets = mdict.get(tok)
        if not targets:
            continue
        for label, trel, key in targets:
            if top_folder(trel) != doc_top:                 # 言及も同じトップフォルダの中だけ
                continue
            dst_cid = make_cid(label, world_id, trel, key)
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


def mention_pass(world_dir, world_id: str, defs: dict, aliases: dict, files, nodes: dict,
                  edges: list, flags: list) -> None:
    """Pass3: 資料文書（`branch=="office"`）を辞書と突合し言及エッジを張る。

    コード定義が無ければ辞書が空のため文書列挙を省く。ソース原文（`branch=="source"`）は突合対象外。
    `aliases` は `_mention_dictionary` へそのまま渡し、単純名の同名衝突があれば `mention_ambiguous_names` を 1 件申告する。
    `worlds.pin_world_root` で `world_id→world_dir` の解決をこの呼び出しが受けた root に固定する
    （`doc_text.read_world_doc_text` が内部で `worlds.world_dir` を引くため）。
    """
    min_len = mention_min_len()
    max_per_doc = mention_max_per_doc()
    mdict, ambiguous_count = _mention_dictionary(defs, aliases, min_len=min_len)
    if ambiguous_count:
        flags.append({"reason": "mention_ambiguous_names", "count": ambiguous_count})
    if not mdict:
        return
    with worlds.pin_world_root(world_id, world_dir), substage.step("graph_mentions", "資料の言及を突き合わせています") as st:
        docs = corpus_docs.iter_world_documents(world_id, include_rag=True,
                                                root=world_dir, files=files)
        for n_seen, d in enumerate(docs):
            st.tick(n_seen)
            if d.get("branch") != "office" or d.get("state") != "ready":
                continue
            rel = d["name"]
            text = doc_text.read_world_doc_text(world_id, d)
            if text is None:                          # 読めない＝スキップ
                flags.append({"reason": "unreadable_mention_doc", "doc": rel})
                continue
            mention_edges_for_doc(rel, text, mdict, min_len, max_per_doc, world_id, nodes, edges, flags)
