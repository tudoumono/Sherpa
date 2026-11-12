"""ES 検索へ管理者の優先・除外（`path_rules`）を当てる部品（優先の統合取得・範囲と除外のフィルタ・質問の埋め込みの使い回し）。
設計: docs/design/rag.md
"""
from __future__ import annotations

import contextvars

from . import scope as scope_mod

# 優先の検索（`prioritized`）が、優先・通常の 2 本の問い合わせで同じ質問の埋め込みを 1 回で済ませるための入れ物（外側が立てる）
QV_MEMO: contextvars.ContextVar = contextvars.ContextVar("es_index_query_embedding_memo", default=None)
_ES_QUERY_FAILURES = frozenset({"es_unavailable", "es_query_failed", "es_query_rejected"})  # 問い合わせ自体の失敗（結果が空でも「無かった」ではない）


def _hit_key(h: dict) -> tuple:
    return (h.get("doc_id"), h.get("chunk_id"), h.get("line"), h.get("text"))


def prioritized(rules, run, k: int) -> tuple[list, str | None]:
    """管理者の優先を、取った後の並べ替えでなく問い合わせの中で効かせる。優先の下の資料だけを同じ条件・同じ `k` で別に取り（`run(True)`）、
    通常の取得（`run(False)`）の優先でないヒットと統合して、最後に `k` 件へ切る（優先が `k` に押し出されない）。優先が無ければ通常の取得をそのまま返す。"""
    if not rules.priority_paths:
        return run(False)
    token = QV_MEMO.set({})  # 質問の埋め込みは 1 回だけ（優先・通常の両方の問い合わせで使い回す）
    try:
        prio, prio_reason = run(True)
        hits, reason = run(False)
    finally:
        QV_MEMO.reset(token)
    if prio_reason in _ES_QUERY_FAILURES:
        # 優先だけの問い合わせが失敗したときは、通常の結果の優先のヒットを捨てず、優先を先に寄せて返す
        return rules.prefer(hits)[:k], reason
    seen, out = set(), []
    for h in [*prio, *[h for h in hits if not rules.priority(h.get("doc_id"))]]:
        key = _hit_key(h)
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out[:k], reason


def scope_filters(rules, scope_paths, prio_only: bool = False) -> list:
    """範囲フィルタ（「範囲の外でも読む」を足した範囲）＋管理者の除外のフィルタ（＋優先の下だけに絞る節）を `filter` 節に積む形で返す。設定が無ければ従来の `scopes` の terms だけ。"""
    sel = scope_mod.normalize_scope_paths(rules.extend_scope(scope_paths))
    flt = [rules.es_scope_clause(sel)] if sel else []
    must_not = rules.es_must_not()
    if must_not:
        flt.append({"bool": {"must_not": must_not}})
    if prio_only:
        flt.append({"bool": {"should": rules.es_should(), "minimum_should_match": 1}})
    return flt


def embed_memo(q: str, ec: dict, world: str, embed):
    """質問の埋め込み（`embed()`）を、`prioritized` の中では同じ質問につき 1 回だけ呼ぶ。"""
    memo = QV_MEMO.get()
    key = (q, world, ec.get("provider"), ec.get("model"))
    if memo is not None and key in memo:
        return memo[key]
    qv = embed()
    if memo is not None:
        memo[key] = qv
    return qv
