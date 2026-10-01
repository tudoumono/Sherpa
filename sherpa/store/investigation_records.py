"""調査台帳の記録（`investigation_records`・COD-18 ①〜③「調査台帳を回答ごとに残す」提案書・
⑤「調査の途中で台帳を確かめ、目的や観点を見直す」利用者2026-10-01指示）。

回答（assistant メッセージ）ごとに、投稿時点の調査台帳（`investigation_ledger.py` の正規形・
manifest/items/coverage/reviews）を保存する。本文・資料名そのものを運ばない契約は
`investigation_ledger.py` 側（item/evidence/review のキー集合が完全一致する正規形）で既に
担保されている——本モジュールはその正規形をそのまま JSONB へ永続化する窓口に徹する（新しい
検証はしない）。`reviews`（中間の見直し）だけは item/evidence と異なり本文（purpose/summary/
perspectives）を持つ——`investigation_ledger.validate_review_entry()` の上限文字数・配列件数の
上限が無制限肥大化の歯止め（本モジュールの責務ではない）。

所有権確認は呼び出し側（routers）が既存の `conversations.py::owns_assistant_message` を使う
（本モジュールは認可を持たない・feedback.py と同じ流儀）。`messages.trace` には入れない
別の持ち場（`sherpa/chat_service.py` のコメント参照）。
"""
from __future__ import annotations

import json

from psycopg.types.json import Json

from .db import _connect, _ensure

# 1 件（manifest+items+coverage 合算）の大きさの上限。超過時は items を id 降順（台帳ファイルの
# 列挙順＝`investigation_ledger.load_ledger` の昇順と対称）に後ろから切り落として `truncated` を
# 立てる（黙って切らない・COD-18 ①）。items を全部落としてもなお超える場合は coverage も落とし、
# それでも超えれば manifest を質問の種類だけに縮める（RV 1 巡目是正: 何かを落とした経路では
# 必ず `truncated=True`）。
MAX_RECORD_BYTES = 1024 * 1024  # 1 MiB


def _record_size(manifest, items, coverage, reviews) -> int:
    """保存前のバイト見積り（UTF-8 JSON 直列化・`Json(...)` が実際に送る内容と同じ形）。"""
    return len(json.dumps({"manifest": manifest, "items": items, "coverage": coverage,
                          "reviews": reviews}, ensure_ascii=False).encode("utf-8"))


def trim_to_budget(manifest: dict | None, items: dict, coverage: dict,
                   reviews: list | None = None) -> tuple[dict | None, dict, dict, list, bool]:
    """`MAX_RECORD_BYTES` を超える場合、`items` を id 降順に1件ずつ落として収める（純関数・
    DB アクセスなし）。超過しなければそのまま返す（`truncated=False`）。item を全部落としても
    超える場合は `coverage` を落とし、それでも超えれば `reviews`（COD-18 ⑤・本文を持つため
    `coverage` の次に重い想定）も落とし、それでも超えれば `manifest` を質問の種類だけに縮める。
    何かを落とした経路では必ず `truncated=True`。戻り値は
    `(manifest, items, coverage, reviews, truncated)`。

    `sherpa/store/investigation_records.py::save_investigation_record` が使う（二重実装しない）。
    `reviews`（既定 `None`→`[]`）は後方互換の省略可引数——呼び出し元を増やさず既存呼び出し
    （4引数）はそのまま動く。
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
    """投稿時点の調査台帳を1行保存する(assistant message 保存の**直後**に呼ぶこと・`message_id`
    は `messages` に実在していなければならない＝FK 制約)。`trim_to_budget` で事前に切り詰めてから
    書く。呼び出し側（chat_service.py）は例外を fail-open で拾い、回答の保存自体は妨げない。

    `reviews`（COD-18 ⑤・既定 `None`→`[]`）: 中間の見直し（`investigation_ledger.load_reviews`
    の正規形の配列）。省略可引数のため既存呼び出し（台帳ゲートが走らず見直し自体を使わない構成）
    はそのまま動く。

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
    """1件の調査台帳記録（存在しなければ `None`）。所有権確認は呼び出し側の責務
    （`conversations.py::owns_assistant_message` 等・本関数自身は認可を持たない）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT message_id, conversation_id, created_at, complete, truncated, "
            "  manifest, items, coverage, reviews FROM investigation_records WHERE message_id=%s",
            (message_id,),
        ).fetchone()
