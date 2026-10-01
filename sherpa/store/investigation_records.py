"""調査台帳の記録（`investigation_records`・COD-18 ①〜③「調査台帳を回答ごとに残す」提案書）。

回答（assistant メッセージ）ごとに、投稿時点の調査台帳（`investigation_ledger.py` の正規形・
manifest/items/coverage）を保存する。本文・資料名そのものを運ばない契約は `investigation_ledger.py`
側（item/evidence のキー集合が完全一致する正規形）で既に担保されている——本モジュールはその
正規形をそのまま JSONB へ永続化する窓口に徹する（新しい検証はしない）。

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


def _record_size(manifest, items, coverage) -> int:
    """保存前のバイト見積り（UTF-8 JSON 直列化・`Json(...)` が実際に送る内容と同じ形）。"""
    return len(json.dumps({"manifest": manifest, "items": items, "coverage": coverage},
                          ensure_ascii=False).encode("utf-8"))


def trim_to_budget(manifest: dict | None, items: dict,
                   coverage: dict) -> tuple[dict | None, dict, dict, bool]:
    """`MAX_RECORD_BYTES` を超える場合、`items` を id 降順に1件ずつ落として収める（純関数・
    DB アクセスなし）。超過しなければそのまま返す（`truncated=False`）。item を全部落としても
    超える場合は `coverage` も落とし、それでも超えれば `manifest` を質問の種類だけに縮める。
    何かを落とした経路では必ず `truncated=True`。戻り値は `(manifest, items, coverage, truncated)`。

    `sherpa/store/investigation_records.py::save_investigation_record` が使う（二重実装しない）。
    """
    items = dict(items or {})
    coverage = dict(coverage or {})
    if _record_size(manifest, items, coverage) <= MAX_RECORD_BYTES:
        return manifest, items, coverage, False
    truncated = False
    for item_id in sorted(items, reverse=True):
        if _record_size(manifest, items, coverage) <= MAX_RECORD_BYTES:
            break
        del items[item_id]
        truncated = True
    if _record_size(manifest, items, coverage) > MAX_RECORD_BYTES:
        coverage = {}
        truncated = True
    if _record_size(manifest, items, coverage) > MAX_RECORD_BYTES:
        kind = (manifest or {}).get("question_kind") if isinstance(manifest, dict) else None
        manifest = {"question_kind": kind} if isinstance(kind, str) and len(kind) <= 64 else None
        truncated = True
    return manifest, items, coverage, truncated


def save_investigation_record(message_id: int, conversation_id: int, *,
                              complete: bool, manifest: dict | None,
                              items: dict, coverage: dict) -> dict:
    """投稿時点の調査台帳を1行保存する(assistant message 保存の**直後**に呼ぶこと・`message_id`
    は `messages` に実在していなければならない＝FK 制約)。`trim_to_budget` で事前に切り詰めてから
    書く。呼び出し側（chat_service.py）は例外を fail-open で拾い、回答の保存自体は妨げない。

    戻り値は保存した行（`complete`/`truncated`/`manifest`/`items`/`coverage` を含む）。
    """
    manifest, items, coverage, truncated = trim_to_budget(manifest, items, coverage)
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO investigation_records "
            "  (message_id, conversation_id, complete, truncated, manifest, items, coverage) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING message_id, conversation_id, created_at, complete, truncated, "
            "  manifest, items, coverage",
            (message_id, conversation_id, bool(complete), truncated,
             Json(manifest) if manifest is not None else None,
             Json(items), Json(coverage)),
        ).fetchone()


def get_investigation_record(message_id: int) -> dict | None:
    """1件の調査台帳記録（存在しなければ `None`）。所有権確認は呼び出し側の責務
    （`conversations.py::owns_assistant_message` 等・本関数自身は認可を持たない）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT message_id, conversation_id, created_at, complete, truncated, "
            "  manifest, items, coverage FROM investigation_records WHERE message_id=%s",
            (message_id,),
        ).fetchone()
