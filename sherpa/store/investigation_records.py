"""調査台帳の記録（`investigation_records`）。
回答（assistant メッセージ）ごとに、投稿時点の調査台帳（`investigation_ledger.py` の正規形: manifest/items/coverage/reviews）を JSONB で保存・取得する。
本文・資料名を運ばない検証は `investigation_ledger.py` 側が担い、ここでは検証しない。所有権確認は呼び出し側（routers）の責務。
設計: docs/design/codex.md「調査台帳と回答前の関門」
"""
from __future__ import annotations

import json

from psycopg.types.json import Json

from ..investigation_record_render import describe_dropped  # noqa: F401  (呼び出し側が一緒に使う)
from .db import _connect, _ensure

# 1 件（manifest+items+coverage+reviews+検索語の記録 合算）の大きさの上限。超過時は `trim_record` の順に落として収め、何か落としたら `truncated` を立て、内訳を `detail.dropped` に残す。
MAX_RECORD_BYTES = 1024 * 1024


def _record_size(manifest, items, coverage, reviews, coverage_detail=None) -> int:
    """保存前のバイト見積り（UTF-8 JSON 直列化）。"""
    return len(json.dumps({"manifest": manifest, "items": items, "coverage": coverage,
                          "reviews": reviews, "coverage_detail": coverage_detail or {}},
                         ensure_ascii=False).encode("utf-8"))


def trim_record(manifest: dict | None, items: dict, coverage: dict, reviews: list | None = None,
                coverage_detail: dict | None = None) -> tuple[dict | None, dict, dict, list, dict, dict]:
    """`MAX_RECORD_BYTES` を超える場合、coverage_detail（検索語・資料・範囲）、items（id 降順に 1 件ずつ）、coverage、reviews（1 件ずつ後ろから）、manifest（質問の種類だけに縮める）の順に落とす（純関数）。
    戻り値は `(manifest, items, coverage, reviews, coverage_detail, dropped)`。`dropped` は落としたものの内訳 `{"coverage_detail": 落とした項目数, "items": 落とした件数, "coverage": 落とした項目数, "reviews": 落とした件数, "manifest": 縮めたか}`（落とさなかった欄は無い・何も落とさなければ空）。
    """
    items = dict(items or {})
    reviews = list(reviews or [])
    coverage = dict(coverage or {})
    coverage_detail = dict(coverage_detail or {})
    dropped: dict = {}

    def _over() -> bool:
        return _record_size(manifest, items, coverage, reviews, coverage_detail) > MAX_RECORD_BYTES

    if not _over():
        return manifest, items, coverage, reviews, coverage_detail, dropped
    for item_id in sorted(coverage_detail, reverse=True):
        if not _over():
            break
        del coverage_detail[item_id]
        dropped["coverage_detail"] = dropped.get("coverage_detail", 0) + 1
    for item_id in sorted(items, reverse=True):
        if not _over():
            break
        del items[item_id]
        dropped["items"] = dropped.get("items", 0) + 1
    if _over() and coverage:
        dropped["coverage"] = len(coverage)
        coverage = {}
    while _over() and reviews:
        reviews.pop()
        dropped["reviews"] = dropped.get("reviews", 0) + 1
    if _over():
        kind = (manifest or {}).get("question_kind") if isinstance(manifest, dict) else None
        manifest = {"question_kind": kind} if isinstance(kind, str) and len(kind) <= 64 else None
        dropped["manifest"] = True
    return manifest, items, coverage, reviews, coverage_detail, dropped


def trim_calls(manifest: dict | None, items: dict, coverage: dict, reviews: list | None,
               coverage_detail: dict | None, calls: dict | None) -> tuple[dict | None, dict]:
    """調べた経路・見つかった資料（`detail.calls`）を、他の欄と合わせて `MAX_RECORD_BYTES` に収まるよう後ろから落とす（見つかった資料 → 経路の順・他の欄より先に落とす）。
    戻り値は `(calls, dropped)`。`dropped` は `{"calls": 落とした経路の件数, "found_docs": 落とした資料の件数}`（落とさなければ空）。落とした件数は `calls` の `route_omitted`・`found_more` にも足す（黙って落とさない）。
    """
    if not isinstance(calls, dict):
        return calls, {}
    rest = _record_size(manifest, items or {}, coverage or {}, reviews or [], coverage_detail)
    route, found = list(calls.get("route") or []), list(calls.get("found") or [])
    sizes = {"route": [len(json.dumps(r, ensure_ascii=False).encode("utf-8")) + 1 for r in route],
             "found": [len(json.dumps(r, ensure_ascii=False).encode("utf-8")) + 1 for r in found]}
    total = rest + len(json.dumps({**calls, "route": [], "found": []}, ensure_ascii=False).encode("utf-8")) + 64
    total += sum(sizes["route"]) + sum(sizes["found"])
    dropped: dict = {}
    while total > MAX_RECORD_BYTES and (found or route):
        if found:
            total -= sizes["found"].pop()
            found.pop()
            dropped["found_docs"] = dropped.get("found_docs", 0) + 1
        else:
            total -= sizes["route"].pop()
            route.pop()
            dropped["calls"] = dropped.get("calls", 0) + 1
    if not dropped:
        return calls, {}
    out = {**calls, "route": route, "found": found}
    if dropped.get("calls"):
        out["route_omitted"] = int(calls.get("route_omitted") or 0) + dropped["calls"]
    if dropped.get("found_docs"):
        out["found_more"] = int(calls.get("found_more") or 0) + dropped["found_docs"]
    return out, dropped


def trim_to_budget(manifest: dict | None, items: dict, coverage: dict,
                   reviews: list | None = None) -> tuple[dict | None, dict, dict, list, bool]:
    """`trim_record` の旧形式の戻り値（何か落としたら `truncated=True`）。戻り値は `(manifest, items, coverage, reviews, truncated)`。"""
    manifest, items, coverage, reviews, _detail, dropped = trim_record(manifest, items, coverage, reviews)
    return manifest, items, coverage, reviews, bool(dropped)


def save_investigation_record(message_id: int, conversation_id: int, *,
                              complete: bool, manifest: dict | None,
                              items: dict, coverage: dict, reviews: list | None = None,
                              coverage_detail: dict | None = None, extras: dict | None = None) -> dict:
    """投稿時点の調査台帳を1行保存する。assistant message の保存直後に呼ぶ（`message_id` は `messages` に実在すること＝FK）。
    `trim_record` で切り詰めてから書き、落としたものの内訳と検索語・読んだ範囲を `detail`（`{"dropped", "coverage", ...}`）に残す。`extras` は呼び出し側が先に切り詰めた分の内訳などを `detail` へ足す（`dropped` は合算）。
    戻り値は保存した行（`complete`/`truncated`/`manifest`/`items`/`coverage`/`reviews`/`detail` を含む）。
    """
    manifest, items, coverage, reviews, coverage_detail, dropped = trim_record(
        manifest, items, coverage, reviews, coverage_detail)
    detail: dict = dict(extras or {})
    prior = detail.get("dropped") if isinstance(detail.get("dropped"), dict) else {}
    merged = {**prior, **dropped}
    if merged:
        detail["dropped"] = merged
    if coverage_detail:
        detail["coverage"] = coverage_detail
    truncated = bool(merged)
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO investigation_records "
            "  (message_id, conversation_id, complete, truncated, manifest, items, coverage, reviews, detail) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING message_id, conversation_id, created_at, complete, truncated, "
            "  manifest, items, coverage, reviews, detail",
            (message_id, conversation_id, bool(complete), truncated,
             Json(manifest) if manifest is not None else None,
             Json(items), Json(coverage), Json(reviews), Json(detail)),
        ).fetchone()


def get_investigation_record(message_id: int) -> dict | None:
    """1件の調査台帳記録（無ければ `None`）。認可は呼び出し側の責務。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT message_id, conversation_id, created_at, complete, truncated, "
            "  manifest, items, coverage, reviews, detail FROM investigation_records WHERE message_id=%s",
            (message_id,),
        ).fetchone()
