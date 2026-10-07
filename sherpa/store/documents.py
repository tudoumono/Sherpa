"""文書台帳（documents）の読み書き。
設計: docs/design/data.md「KB（取り込み・範囲・同一性）」
"""
from __future__ import annotations

from .db import _KB_ID, _connect, _ensure


def list_document_worlds() -> list:
    """文書台帳に行が存在する world_id の一覧（孤児掃除用）。"""
    _ensure()
    with _connect() as c:
        return [r["version"] for r in
               c.execute("SELECT DISTINCT version FROM documents WHERE kb_id=%s", (_KB_ID,)).fetchall()]


def replace_documents(world, rows) -> int:
    """world の文書台帳を丸ごと入れ替える（冪等）。`rows` は doc dict のリスト。
    列名 `version` は world_id を保持する。importance 系 3 列は `rows` に無ければ NULL。
    """
    _ensure()
    with _connect() as c:
        c.execute("DELETE FROM documents WHERE kb_id=%s AND version=%s", (_KB_ID, world))
        for r in rows:
            c.execute(
                "INSERT INTO documents (kb_id, version, name, layer, scope_path, doctype, branch, "
                "  original_path, md_path, status, importance, importance_reason, importance_source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (_KB_ID, world, r.get("name"), r.get("layer") or "version", r.get("scope_path"),
                 r.get("doctype"), r.get("branch"), r.get("original_path"), r.get("md_path"), r.get("status"),
                 r.get("importance"), r.get("importance_reason"), r.get("importance_source")))
    return len(rows)


def get_document(world, name) -> dict | None:
    """world の文書台帳の 1 行（`list_documents` と同じ列・無ければ None）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT name, layer, scope_path, doctype, branch, original_path, md_path, status, "
            "  importance, importance_reason, importance_source "
            "FROM documents WHERE kb_id=%s AND version=%s AND name=%s", (_KB_ID, world, name)).fetchone()


def replace_document(world, name, row: dict | None) -> None:
    """world の文書台帳の 1 行だけを入れ替える（`row` が None なら消す）。列は `replace_documents` と同じ。"""
    _ensure()
    with _connect() as c:
        c.execute("DELETE FROM documents WHERE kb_id=%s AND version=%s AND name=%s", (_KB_ID, world, name))
        if row is not None:
            c.execute(
                "INSERT INTO documents (kb_id, version, name, layer, scope_path, doctype, branch, "
                "  original_path, md_path, status, importance, importance_reason, importance_source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (_KB_ID, world, name, row.get("layer") or "version", row.get("scope_path"),
                 row.get("doctype"), row.get("branch"), row.get("original_path"), row.get("md_path"),
                 row.get("status"), row.get("importance"), row.get("importance_reason"),
                 row.get("importance_source")))


def list_documents(world) -> list:
    """world の文書台帳（name/layer/scope_path/doctype/branch/original_path/md_path/status/importance 系）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT name, layer, scope_path, doctype, branch, original_path, md_path, status, "
            "  importance, importance_reason, importance_source "
            "FROM documents WHERE kb_id=%s AND version=%s ORDER BY name", (_KB_ID, world)).fetchall()


def document_exists(world, name) -> bool:
    """world の文書台帳に `name`（正準表記）が完全一致で存在するか。原本DL の別名拒否に使う。"""
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT 1 FROM documents WHERE kb_id=%s AND version=%s AND name=%s LIMIT 1",
            (_KB_ID, world, name)).fetchone()
    return row is not None


def count_documents(world) -> int:
    """world の文書台帳の総件数（GET /documents のページング用）。"""
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE kb_id=%s AND version=%s",
            (_KB_ID, world)).fetchone()
    return row["n"] if row else 0


def list_documents_page(world, *, limit: int, offset: int) -> list:
    """world の文書台帳を `name` 順にページング取得する（LIMIT/OFFSET）。列は `list_documents` と同じ。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT name, layer, scope_path, doctype, branch, original_path, md_path, status, "
            "  importance, importance_reason, importance_source "
            "FROM documents WHERE kb_id=%s AND version=%s ORDER BY name LIMIT %s OFFSET %s",
            (_KB_ID, world, limit, offset)).fetchall()
