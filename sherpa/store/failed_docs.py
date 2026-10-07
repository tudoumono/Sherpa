"""取り込みで失敗（縮退）した文書の一覧（world_failed_docs）の読み書き。
設計: docs/design/data.md「PostgreSQL（主な表と役割）」
"""
from __future__ import annotations

from .db import _connect, _ensure


def replace_failed_docs(world, failures: dict) -> int:
    """world の失敗一覧を `failures`（`{rel: 生の理由}`）へ置き換える（冪等）。

    ① 今回の一覧に無い rel（成功した・原本が無くなった）は消す ② 続けて失敗した rel は reason・last_tried・tries を更新する
    ③ 新しく失敗した rel は足す。1 トランザクションで行う。
    """
    _ensure()
    with _connect() as c:
        return replace_failed_docs_in(c, world, failures)


def replace_failed_docs_in(c, world, failures: dict) -> int:
    """`replace_failed_docs` の本体（呼び出し元のトランザクション `c` の中で行う）。"""
    rels = sorted(failures)
    c.execute("DELETE FROM world_failed_docs WHERE world_id=%s AND NOT (rel = ANY(%s))", (world, rels))
    for rel in rels:
        c.execute(
            "INSERT INTO world_failed_docs (world_id, rel, reason) VALUES (%s,%s,%s) "
            "ON CONFLICT (world_id, rel) DO UPDATE SET reason=EXCLUDED.reason, "
            "  last_tried=now(), tries=world_failed_docs.tries+1",
            (world, rel, failures[rel]))
    return len(rels)


def record_failed_doc(world, rel: str, reason: str) -> None:
    """1 文書の失敗を記録する（無ければ足す・あれば reason・last_tried・tries を更新する）。"""
    _ensure()
    with _connect() as c:
        c.execute(
            "INSERT INTO world_failed_docs (world_id, rel, reason) VALUES (%s,%s,%s) "
            "ON CONFLICT (world_id, rel) DO UPDATE SET reason=EXCLUDED.reason, "
            "  last_tried=now(), tries=world_failed_docs.tries+1",
            (world, rel, reason))


def remove_failed_doc(world, rel: str) -> int:
    """1 文書を失敗の一覧から外す（直った文書）。"""
    _ensure()
    with _connect() as c:
        return c.execute("DELETE FROM world_failed_docs WHERE world_id=%s AND rel=%s", (world, rel)).rowcount


def list_failed_docs(world) -> list:
    """world の失敗一覧（rel 昇順）。各行は rel/reason/first_failed/last_tried/tries。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT rel, reason, first_failed, last_tried, tries FROM world_failed_docs "
            "WHERE world_id=%s ORDER BY rel", (world,)).fetchall()


def clear_failed_docs(world) -> int:
    """world の失敗一覧を全て消す（資料フォルダの削除・作り直しの前段）。"""
    _ensure()
    with _connect() as c:
        return c.execute("DELETE FROM world_failed_docs WHERE world_id=%s", (world,)).rowcount
