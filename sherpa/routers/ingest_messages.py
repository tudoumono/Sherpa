"""取り込み状況の画面・API に出す、利用者向けの一文。
設計: docs/design/operations.md「止まった取り込みの見え方」
"""
from __future__ import annotations

from fastapi import HTTPException

from sherpa import store
from sherpa.store import ingest as ingest_store

ACTION_BLOCKED_REASON = "取り込み中のため、この操作はできません。終わってからお試しください"
INGEST_ACTIONS = ("recount", "file_state", "refresh", "rag_regenerate_rules", "delete", "reconvert")
UNFINISHED_ACTION_REASON = "途中で止まった取り込みを先に「更新」で続けてください"
# 入れ替えの途中で止まっているとき押せなくする操作。1 文書の行だけが残るときの再変換は、その文書そのもののやり直しだけ受け付ける（受付で確かめる）
_BLOCKED_WHILE_UNFINISHED = {"whole": ("rag_regenerate_rules", "reconvert"), "doc": ("rag_regenerate_rules",)}

# 入れ替えの途中で止まっているとき（`store.unfinished_state` の値）の一文。「更新」で続きから入れ替える。
UNFINISHED_MESSAGES = {
    "whole": "取り込みの途中で止まったため、一部の検索が古い内容のままです。「更新」を押すと続きから入れ替えます",
    "doc": "取り込みの途中で止まったため、一部の資料が古いままです。「更新」を押すと続きから入れ替えます",
}


def action_availability(last_status: str | None, unfinished: str | None = None) -> tuple[bool, dict[str, dict]]:
    """背景受付が競合とする状態と同じ `ACTIVE_STATUSES` から、画面操作の可否を返す。実行中でなく入れ替えの途中で止まっているときは、止まった行を追い越す操作を押せなくして案内を付ける（「更新」は押せるまま）。"""
    active = last_status in ingest_store.ACTIVE_STATUSES
    state = {"allowed": not active, "reason": ACTION_BLOCKED_REASON if active else None}
    actions = {action: dict(state) for action in INGEST_ACTIONS}
    for action in () if active else _BLOCKED_WHILE_UNFINISHED.get(unfinished, ()):
        actions[action] = {"allowed": False, "reason": UNFINISHED_ACTION_REASON}
    return active, actions


def refuse_while_unfinished(wid: str, op: str, rel: str | None = None) -> None:
    """止まった入れ替えの行が残る資料フォルダへ、それを追い越す操作（規則版の再生成・ほかの文書の再変換）を受け付けない（409）。行の読み取りに失敗したら 503。"""
    try:
        stopped = store.unfinished_docs(wid)
    except Exception as e:
        raise HTTPException(503, "取り込み台帳を読めませんでした。時間をおいてやり直してください") from e
    if any(doc != rel for doc in stopped):
        raise HTTPException(409, UNFINISHED_ACTION_REASON)


def with_unfinished(message: str | None, unfinished: str | None) -> str | None:
    """直近の失敗の一文に、入れ替えの途中で止まっている旨を足す（止まっていなければそのまま）。"""
    note = UNFINISHED_MESSAGES.get(unfinished)
    if note is None:
        return message
    return note if message is None else f"{message}。{note}"


def last_failure_message(status: str | None, warnings: list[str], blocked: list[dict]) -> str | None:
    """直近の失敗理由コードを、画面と API の利用者向けの一文へ畳み込む。"""
    if status != "failed":
        return None
    low_disk = next((w for w in warnings if w.startswith("low_disk_stop:")), None)
    if low_disk:
        return ("前回の取り込みは、ディスクの空きが少ないため停止しました"
                f"（空き {low_disk.split(':', 1)[1]} GB）")
    unreadable = [b["doc"] for b in blocked
                  if b.get("reason") == "unreadable_code_file" and b.get("doc")]
    if unreadable:
        return f"前回の取り込みは、{ '・'.join(unreadable) } を読み取れなかったため停止しました"
    mappings = (
        ("graph_reflect_failed", "資料どうしの関係を書き込めませんでした"),
        ("ledger_", "資料の一覧を保存できませんでした"),
        ("es_index_failed", "検索できる状態に更新できませんでした"),
        ("reconcile_failed", "使われなくなった検索用データを片付けられませんでした"),
        ("office_md", "Office 資料を読み取れませんでした"),
        ("reconvert_", "指定した資料を読み取り直せませんでした"),
        ("rebind_", "参照先を変更できませんでした"),
        ("unexpected_error", "想定外のエラーで最後まで終わりませんでした"),
    )
    for prefix, message in mappings:
        if any(w.startswith(prefix) for w in warnings):
            return f"前回の取り込みは失敗しました（{message}）"
    if status == "failed":
        return "前回の取り込みは失敗しました（処理を最後まで終えられませんでした）"
    return None
