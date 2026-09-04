"""非agentic の ES 補完（`search_service`/`chat_service`）向けの親返し（P3/P2/chunk 縮退）。

正典: `docs/proposals/2026-09-02-RAG表現の全形式展開と文脈保持.md` §3.3/§3.4（agentic の
`agentic_search._resolve_parent_return` と同じ設計・§3.4「適用範囲」を非agentic 側へ拡張＝CITE-1）。
検索（BM25/kNN のヒット選定）自体は変えない——ここは「ヒットを doc_id で束ねて、返す本文を
P3（全文）/P2（領域）/chunk（子チャンクのみ）へ振り分ける」返却直前の後処理のみ。

**独立モジュールにする理由**: `search_service.py` は `grep_tool`/`chat_service`/`api`/`agents` を
import しない境界を持つ（モジュール docstring 参照・プロセス分離可能・共有KBのみの契約）。
`agentic_search._resolve_parent_return` 系の実装は `grep_tool._CappedStreamReader` 等へ依存して
おりこの境界を満たさないため、ここでは標準ライブラリのファイル行イテレーションだけで P2/P3 を
実装する（Python のファイルオブジェクトは `for line in f:` で行単位に自然にストリーミングされる＝
全文を `.read()` しない限りメモリへ一括ロードしない）。

BUDGET-2（`agentic_search.resolve_tool_result_budgets` の管理画面連動）が並行して同系統の関数群を
拡張中のため、ここではその関数シグネチャに依存しない——予算は本モジュール専用の固定既定値
（env で調整可）を持つ。**`agentic_search.py` 側は変更しない**（agentic 経路は byte-identical のまま）。
実装が `_resolve_parent_return` 等と重複する点は既知の設計判断——agentic 側を将来この共有実装へ
寄せる作業は BUDGET-2 完了後のフォローアップとして残す（CITE-1 の指示どおり）。
"""
from __future__ import annotations

import os
from pathlib import Path

from . import es_index, worlds

# 非agentic 側専用の予算 env（新規）。agentic 側の予算（BUDGET-1/2・管理画面「検索1回あたりの
# 情報量」・env フォールバックは ENV-CLEAN で撤去済み）とは別物——チャット/外部検索APIの1呼び出しに
# 使う固定既定値。既定は BUDGET-1 の精度優先値（64KiB→256KiB）に揃える。
_BUDGET_ENV = "SHERPA_CHAT_ES_EXCERPT_BUDGET_BYTES"
DEFAULT_BUDGET_BYTES = 256 * 1024
_MIN_BUDGET_BYTES = 8 * 1024
_MAX_BUDGET_BYTES = 8 * 1024 * 1024
# 1回の全文/領域読みの安全弁（agentic 側の `_READ_AROUND_FILE_CAP_BYTES` とは独立した非agentic 専用値）。
_READ_CAP_BYTES = 8 * 1024 * 1024
# `es_index.chunk_ids_for_parent` の1クエリ取得上限（agentic 側 `_PARENT_RETURN_REGION_CHUNKS_MAX` と同値）。
_REGION_CHUNKS_MAX = 5000


def parent_return_enabled() -> bool:
    """常時 True（TOGGLE-RM・2026-09-03: グローバルな系統切替トグル `SHERPA_ES_PARENT_RETURN` を
    撤去し常時ONへ固定・agentic 側 `agentic_search._parent_return_enabled` と同じ扱い）。既存の
    呼び出し形（`apply_to_hits`）を変えない最小変更として関数自体は残す。"""
    return True


def excerpt_budget_bytes() -> int:
    """非agentic の親返しに使う予算（バイト・既定 256KiB）。不正値は既定へフォールバック。"""
    raw = os.environ.get(_BUDGET_ENV)
    if raw is None:
        return DEFAULT_BUDGET_BYTES
    try:
        v = int(raw)
    except ValueError:
        return DEFAULT_BUDGET_BYTES
    return v if _MIN_BUDGET_BYTES <= v <= _MAX_BUDGET_BYTES else DEFAULT_BUDGET_BYTES


def _rag_md_path(world: str, doc_id: str) -> Path | None:
    """`doc_id` の `{rel}.rag.md`（RAG 正本）の実パス。封じ込め・symlink 拒否込み（無効/不在は None）。

    doc_id はここに至るまでに ES ヒット（`es_index.search` の実在フィルタ）を経由しており、既に
    信頼できる入力という前提だが、防御的にもう一段検証する（traversal・symlink 脱出は拒否）。
    """
    if not isinstance(doc_id, str) or not doc_id or doc_id.startswith("/") or "\\" in doc_id or "\x00" in doc_id:
        return None
    parts = doc_id.split("/")
    if ".." in parts or "" in parts:
        return None
    root = worlds.derived_rag_dir(world)
    if not root:
        return None
    root = Path(root)
    cand = root / (doc_id + ".rag.md")
    try:
        rr = root.resolve()
        rp = cand.resolve()
        if not (rp == rr or rp.is_relative_to(rr)):
            return None
        if not rp.is_file() or rp.is_symlink():
            return None
    except OSError:
        return None
    return rp


def rag_md_size(world: str, doc_id: str) -> int | None:
    """親返しの P3/P2 判定用: `doc_id` の rag.md バイトサイズを `stat` で見る（読む前に見る・§3.3）。"""
    p = _rag_md_path(world, doc_id)
    if p is None:
        return None
    try:
        return p.stat().st_size
    except OSError:
        return None


def rag_md_read_full(world: str, doc_id: str) -> str | None:
    """親返し P3: rag.md 全文を読む。呼び出し元は `rag_md_size` で予算内と確認済みの doc にのみ呼ぶ
    （`_READ_CAP_BYTES` は TOCTOU 的なサイズ変化に対する保険）。"""
    p = _rag_md_path(world, doc_id)
    if p is None:
        return None
    try:
        if p.stat().st_size > _READ_CAP_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None


def rag_md_region_text(world: str, doc_id: str, target_chunk_ids, byte_cap: int) -> str | None:
    """親返し P2: rag.md をアンカー（`<!-- chunk:{chunk_id} -->`・`es_index.rag_md_anchor_chunk_id`）
    単位で行走査し、`target_chunk_ids` に属するチャンクの本文だけを集める
    （agentic 側 `agentic_search._rag_md_region_text` と同じ設計・素の行イテレーションで実装）。

    対象外のチャンク本文は保持しないためメモリは「現在集めている1チャンク分」に留まる。`byte_cap`
    を超えたら None を返し、それまでに集めた部分的な本文は使わない（黙って中途半端な本文を返さない）。
    """
    if not target_chunk_ids:
        return None
    p = _rag_md_path(world, doc_id)
    if p is None:
        return None
    remaining = set(target_chunk_ids)
    collected: dict[str, str] = {}
    order: list[str] = []
    cur_id: str | None = None
    cur_buf: list[str] = []
    total_bytes = 0
    over = False

    def _close(cid: str, buf: list[str]) -> None:
        nonlocal total_bytes, over
        body = "\n".join(buf).strip()
        collected[cid] = body
        order.append(cid)
        remaining.discard(cid)
        total_bytes += len(body.encode("utf-8"))
        if total_bytes > byte_cap:
            over = True

    try:
        with p.open("r", encoding="utf-8", errors="strict") as f:
            for raw_line in f:
                line = raw_line.rstrip("\n")
                anchor_id = es_index.rag_md_anchor_chunk_id(line)
                if anchor_id is not None:
                    if cur_id is not None and cur_id in remaining:
                        _close(cur_id, cur_buf)
                        if over:
                            break
                    if not remaining:
                        break
                    cur_id, cur_buf = anchor_id, []
                    continue
                if cur_id is not None and cur_id in remaining:
                    cur_buf.append(line)
            else:
                if cur_id is not None and cur_id in remaining:
                    _close(cur_id, cur_buf)
    except (OSError, UnicodeDecodeError):
        return None
    if over or not collected:
        return None
    return "\n\n".join(collected[cid] for cid in order)


def resolve_parent_return(world: str, rag_groups: dict, budget_for_rag: int) -> list:
    """親返し本体（agentic `_resolve_parent_return` と同じ決定的な貪欲配分・§3.4）。

    1. 全 doc の**最低保証**（子チャンク本文の合計＝baseline）を `budget_for_rag` から確保する。
    2. 残り予算をベストスコア順（同点は doc_id 昇順）に、rag.md サイズが入るなら P3 全文／
       領域なら P2／どちらも無理なら chunk（子チャンクの結合）のまま使う。
    3. 各 doc は必ず1エントリを返し、`tier` を必ず申告する（黙って縮退しない）。

    `rag_groups`: `{doc_id: [{"chunk_id", "parent_id", "score", "text"}, ...]}`。
    戻り値: `[{"doc_id", "tier", "text", "chunk_ids"}, ...]`（`tier` は `"full"|"region"|"chunk"`）。
    """
    groups = []
    for doc_id, items in rag_groups.items():
        baseline = sum(len(it["text"].encode("utf-8")) for it in items)
        best_score = max(float(it.get("score") or 0) for it in items)
        groups.append((doc_id, items, baseline, best_score))
    remaining = max(0, budget_for_rag - sum(g[2] for g in groups))
    groups.sort(key=lambda g: (-g[3], g[0]))

    out = []
    for doc_id, items, baseline, _best_score in groups:
        chunk_ids = [it["chunk_id"] for it in items]
        tier = "chunk"
        text = "\n\n".join(it["text"] for it in items)
        full_size = rag_md_size(world, doc_id)
        if full_size is not None:
            delta = full_size - baseline
            if delta <= remaining:
                full_text = rag_md_read_full(world, doc_id)
                if full_text is not None:
                    text = full_text
                    tier = "full"
                    remaining -= delta
        if tier == "chunk":
            parent_ids = sorted({it["parent_id"] for it in items if it.get("parent_id")})
            if parent_ids:
                target_ids = set(es_index.chunk_ids_for_parent(
                    world, doc_id, parent_ids, limit=_REGION_CHUNKS_MAX))
                target_ids |= set(chunk_ids)
                region_cap = baseline + remaining
                region_text = rag_md_region_text(world, doc_id, target_ids, region_cap)
                if region_text is not None:
                    delta_region = len(region_text.encode("utf-8")) - baseline
                    if delta_region <= remaining:
                        text = region_text
                        tier = "region"
                        remaining -= delta_region
        out.append({"doc_id": doc_id, "tier": tier, "text": text, "chunk_ids": chunk_ids})
    return out


def apply_to_hits(world: str, hits: list) -> list:
    """ES ヒット list（`es_index.search`/`search_knn_only` の戻り値）へ親返しを適用する。

    `chunk_id` を持つヒット（rag チャンク由来）だけを doc_id で束ね、`resolve_parent_return` に通す。
    1 doc につき1エントリへ集約する（複数チャンクのヒットは代表1件＝最高スコアのヒットへ、`text`/
    `tier` だけを差し替えて統合する——locator/section_path 等の他フィールドは代表ヒットのものを保つ）。
    `chunk_id` を持たないヒット（legacy 40行チャンク由来）はそのまま素通し。無効化時・対象ヒットが
    無いときは無変更のまま返す。

    `search_service.py`/`chat_service.py` の両方から呼ばれる共有部品（重複実装しない）。
    """
    if not parent_return_enabled():
        return hits
    rag_hits = [h for h in hits if h.get("chunk_id")]
    if not rag_hits:
        return hits
    other_hits = [h for h in hits if not h.get("chunk_id")]
    by_doc: dict[str, list] = {}
    for h in rag_hits:
        by_doc.setdefault(h["doc_id"], []).append(h)
    groups = {
        doc: [{"chunk_id": h["chunk_id"], "parent_id": h.get("parent_id"),
              "score": h.get("score"), "text": h.get("text", "")} for h in items]
        for doc, items in by_doc.items()
    }
    resolved = resolve_parent_return(world, groups, excerpt_budget_bytes())
    out = list(other_hits)
    for r in resolved:
        items = by_doc[r["doc_id"]]
        rep = max(items, key=lambda h: float(h.get("score") or 0))   # 代表ヒット（最高スコア）
        out.append({**rep, "text": r["text"], "tier": r["tier"]})
    return out
