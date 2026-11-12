"""ES への検索要求の下請け（`es_index` の検索）。不完全な応答を成功として返さない。
設計: docs/design/rag.md「融合検索（RRF）」
"""
from __future__ import annotations

import urllib.error


class PartialResult(OSError):
    """ES が時間切れ・一部の分割で失敗したまま返した応答（`timed_out`／`_shards.failed`）。取りこぼしがありうる。"""


def run_search(world: str, body: dict) -> dict:
    """資料フォルダの索引へ検索を投げる。時間切れ・一部失敗の応答は `PartialResult`（`classify_exception` が `es_query_failed` にする）。"""
    from . import es_index
    res = es_index._req("POST", f"/{es_index._index(world)}/_search", body)
    if isinstance(res, dict):
        failed = (res.get("_shards") or {}).get("failed")
        if res.get("timed_out") or (isinstance(failed, int) and failed > 0):
            raise PartialResult("ES の応答が完全ではありません（時間切れまたは一部の分割で失敗）")
    return res


def classify_exception(exc: Exception) -> str:
    """BM25 クエリ実行時の例外を固定コードへ分類する（呼び出し元が回復可否を区別できるよう、握りつぶす前に 1 回だけ判定する）。例外は再送出しない（`search()`／`search_knn_only()` は例外を投げず `(hits, degrade_reason)` を返す契約）。
    - `HTTPError`: 404（索引未作成）・429・5xx は `es_query_failed`（回復可能）、それ以外の 4xx（クエリの拒否）は `es_query_rejected`（回復不可）。
    - それ以外の `OSError`（接続断・タイムアウト・不完全な応答 `PartialResult` 等）は `es_query_failed`。
    - 非通信例外（`JSONDecodeError`／`TypeError`／`KeyError` 等＝想定外の応答形）は `es_query_rejected`。
    """
    if isinstance(exc, urllib.error.HTTPError):
        return ("es_query_failed" if (exc.code in (404, 429) or 500 <= exc.code <= 599)
               else "es_query_rejected")
    if isinstance(exc, OSError):
        return "es_query_failed"
    return "es_query_rejected"
