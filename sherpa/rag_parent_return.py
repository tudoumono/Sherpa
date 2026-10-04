"""非agentic の ES 補完（`fused_search`/`chat_service`）向けの親返し。

ES のヒットを doc_id で束ね、返す本文を P3（全文）/P2（領域）/chunk（子チャンクのみ）へ振り分ける返却直前の後処理
（検索のヒット選定は変えない）。`fused_search.py` が `grep_tool`/`chat_service`/`api`/`agents` を import しない境界を持つため、
標準ライブラリの行イテレーションだけで実装する。予算は本モジュール専用の固定値。
設計: docs/design/rag.md「検索での使われ方」
"""
from __future__ import annotations

from . import es_index, worlds

# 非agentic 側専用の予算（チャット/外部検索APIの1呼び出し用。agentic 側の予算とは別）
DEFAULT_BUDGET_BYTES = 256 * 1024
# 1回の全文/領域読みの安全弁
_READ_CAP_BYTES = 8 * 1024 * 1024
# `es_index.chunk_ids_for_parent` の1クエリ取得上限
_REGION_CHUNKS_MAX = 5000


def excerpt_budget_bytes() -> int:
    """非agentic の親返しに使う予算（バイト・256KiB）。"""
    return DEFAULT_BUDGET_BYTES


def rag_md_size(world: str, doc_id: str) -> int | None:
    """親返しの P3/P2 判定用: `doc_id` の rag.md バイトサイズを `stat` で見る（読む前に見る）。"""
    p = worlds.rag_md_path(world, doc_id)
    if p is None:
        return None
    try:
        return p.stat().st_size
    except OSError:
        return None


def rag_md_read_full(world: str, doc_id: str) -> str | None:
    """親返し P3: rag.md 全文を読む。`rag_md_size` で予算内と確認済みの doc にのみ呼ぶ（`_READ_CAP_BYTES` はサイズ変化への保険）。"""
    p = worlds.rag_md_path(world, doc_id)
    if p is None:
        return None
    try:
        if p.stat().st_size > _READ_CAP_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None


def rag_md_region_text(world: str, doc_id: str, target_chunk_ids, byte_cap: int) -> str | None:
    """親返し P2: rag.md をアンカー（`<!-- chunk:{chunk_id} -->`）単位で行走査し、`target_chunk_ids` のチャンク本文だけを集める。

    `byte_cap` を超えたら None を返し、部分的な本文は使わない。
    """
    if not target_chunk_ids:
        return None
    p = worlds.rag_md_path(world, doc_id)
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
    """親返し本体（決定的な貪欲配分）。

    1. 全 doc の最低保証（子チャンク本文の合計＝baseline）を `budget_for_rag` から確保する。
    2. 残り予算をベストスコア順（同点は doc_id 昇順）に、rag.md サイズが入るなら P3 全文／領域なら P2／無理なら chunk のまま使う。
    3. 各 doc は必ず1エントリを返し、`tier` を必ず申告する。

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
    """ES ヒット list（スコア降順）へ親返しを適用する。

    `chunk_id` を持つヒットだけを doc_id で束ねて `resolve_parent_return` に通し、1 doc につき代表1件（最高スコア）へ集約する
    （`text`/`tier` だけ差し替え、他フィールドは代表のもの）。`chunk_id` を持たないヒットは素通し。
    集約結果は代表ヒットが元あった位置へ戻す（スコア降順を保つ）。代表以外のメンバーは出力しない。
    `fused_search.py`/`chat_service.py` の共有部品。
    """
    rag_hits = [h for h in hits if h.get("chunk_id")]
    if not rag_hits:
        return hits
    by_doc: dict[str, list] = {}
    for h in rag_hits:
        by_doc.setdefault(h["doc_id"], []).append(h)
    groups = {
        doc: [{"chunk_id": h["chunk_id"], "parent_id": h.get("parent_id"),
              "score": h.get("score"), "text": h.get("text", "")} for h in items]
        for doc, items in by_doc.items()
    }
    resolved = resolve_parent_return(world, groups, excerpt_budget_bytes())
    resolved_by_doc = {r["doc_id"]: r for r in resolved}
    # 各 doc の代表ヒット（最高スコア・同点は先に出現した方）の identity を記録し、その位置だけへ集約結果を書き戻す
    rep_id_by_doc = {doc: id(max(items, key=lambda h: float(h.get("score") or 0)))
                     for doc, items in by_doc.items()}
    out = []
    for h in hits:
        doc_id = h.get("doc_id")
        if h.get("chunk_id") and doc_id in resolved_by_doc:
            if id(h) == rep_id_by_doc[doc_id]:
                r = resolved_by_doc[doc_id]
                out.append({**h, "text": r["text"], "tier": r["tier"]})
            continue
        out.append(h)
    return out
