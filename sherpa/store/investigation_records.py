"""調査台帳の記録（`investigation_records`）。
回答（assistant メッセージ）ごとに、投稿時点の調査台帳（`investigation_ledger.py` の正規形: manifest/items/coverage/reviews）を JSONB で保存・取得する。
本文・資料名を運ばない検証は `investigation_ledger.py` 側が担い、ここでは検証しない。所有権確認は呼び出し側（routers）の責務。
設計: docs/design/codex.md「調査台帳と回答前の関門」
"""
from __future__ import annotations

import json

from psycopg.types.json import Json

from .db import _connect, _ensure

# 1 件（manifest+items+coverage 合算）の大きさの上限。超過時は items を id 降順に後ろから、次に coverage、reviews、最後に manifest を縮めて収め、何か落としたら `truncated` を立てる。
MAX_RECORD_BYTES = 1024 * 1024


def _record_size(manifest, items, coverage, reviews) -> int:
    """保存前のバイト見積り（UTF-8 JSON 直列化）。"""
    return len(json.dumps({"manifest": manifest, "items": items, "coverage": coverage,
                          "reviews": reviews}, ensure_ascii=False).encode("utf-8"))


def trim_to_budget(manifest: dict | None, items: dict, coverage: dict,
                   reviews: list | None = None) -> tuple[dict | None, dict, dict, list, bool]:
    """`MAX_RECORD_BYTES` を超える場合、items を id 降順に1件ずつ落とし、足りなければ coverage、reviews、manifest（質問の種類だけに縮める）の順に縮める（純関数）。
    何か落としたら `truncated=True`。戻り値は `(manifest, items, coverage, reviews, truncated)`。
    """
    items = dict(items or {})
    reviews = list(reviews or [])
    if _record_size(manifest, items, coverage, reviews) <= MAX_RECORD_BYTES:
        return manifest, items, coverage, reviews, False
    truncated = False
    for item_id in sorted(items, reverse=True):
        if _record_size(manifest, items, coverage, reviews) <= MAX_RECORD_BYTES:
            break
        del items[item_id]
        truncated = True
    if _record_size(manifest, items, coverage, reviews) > MAX_RECORD_BYTES:
        coverage = {}
        truncated = True
    if _record_size(manifest, items, coverage, reviews) > MAX_RECORD_BYTES:
        reviews = []
        truncated = True
    if _record_size(manifest, items, coverage, reviews) > MAX_RECORD_BYTES:
        kind = (manifest or {}).get("question_kind") if isinstance(manifest, dict) else None
        manifest = {"question_kind": kind} if isinstance(kind, str) and len(kind) <= 64 else None
        truncated = True
    return manifest, items, coverage, reviews, truncated


def save_investigation_record(message_id: int, conversation_id: int, *,
                              complete: bool, manifest: dict | None,
                              items: dict, coverage: dict, reviews: list | None = None) -> dict:
    """投稿時点の調査台帳を1行保存する。assistant message の保存直後に呼ぶ（`message_id` は `messages` に実在すること＝FK）。
    `trim_to_budget` で切り詰めてから書く。`reviews` は中間の見直し（省略時は []）。
    戻り値は保存した行（`complete`/`truncated`/`manifest`/`items`/`coverage`/`reviews` を含む）。
    """
    manifest, items, coverage, reviews, truncated = trim_to_budget(manifest, items, coverage, reviews)
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO investigation_records "
            "  (message_id, conversation_id, complete, truncated, manifest, items, coverage, reviews) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING message_id, conversation_id, created_at, complete, truncated, "
            "  manifest, items, coverage, reviews",
            (message_id, conversation_id, bool(complete), truncated,
             Json(manifest) if manifest is not None else None,
             Json(items), Json(coverage), Json(reviews)),
        ).fetchone()


def get_investigation_record(message_id: int) -> dict | None:
    """1件の調査台帳記録（無ければ `None`）。認可は呼び出し側の責務。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT message_id, conversation_id, created_at, complete, truncated, "
            "  manifest, items, coverage, reviews FROM investigation_records WHERE message_id=%s",
            (message_id,),
        ).fetchone()
