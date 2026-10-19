"""取り込みワーカーと Web アプリの版の照合（取り込み画面のお知らせの元）。
設計: docs/design/operations.md「取り込みワーカーと版の照合」
"""
from __future__ import annotations

from .. import app_version, store
from ..store import workers as store_workers

MSG_VERSION_MISMATCH = ("ワーカーの版が Web アプリと違うため、取り込みを止めています。"
                        "make restart で両方を同じ版にしてください")
MSG_NO_WORKER = "取り込みのワーカーが動いていません。make restart-worker で起動してください"


def snapshot() -> dict:
    """Web アプリ自身の版・心拍が新しい取り込みワーカーの版と状態・待っている依頼の数・お知らせ文（無ければ None）。
    ワーカーが複数見えるとき（入れ替え中）は、Web アプリと同じ版が 1 つでもいれば版は合っているとみなす。
    停止の途中（`stopping`）の行は、もう依頼を拾わないので数えない。
    """
    web_version = app_version.current()
    live = [w for w in store_workers.list_live("ingest") if w["state"] != "stopping"]
    mine = next((w for w in live if w["app_version"] == web_version), None)
    head = mine or (live[0] if live else None)
    mismatch = bool(live) and mine is None
    same, other = store.count_queued_runs(web_version)
    message = None
    if not live:
        message = MSG_NO_WORKER
    elif mismatch:
        message = MSG_VERSION_MISMATCH
    return {
        "web_version": web_version,
        "worker_alive": bool(live),
        "worker_version": head["app_version"] if head else None,
        "worker_state": head["state"] if head else None,
        "version_mismatch": mismatch,
        "queued": same + other,
        "queued_other_version": other,
        "message": message,
    }
