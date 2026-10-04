"""個人 workspace ファイルの台帳（personal_workspace_files）の管理。
個人ファイルの台帳だけを扱い、ES/Neo4j/共有 KB の取り込みからは参照しない（RAG 非索引）。
設計: docs/design/users.md「個人の作業領域（workspace）」
"""
from __future__ import annotations

from .db import _connect, _ensure


def record_workspace_file(uid: str, rel_path: str, original_path: str,
                          size_bytes: int, sha256: str,
                          expires_at=None) -> dict:
    """個人 workspace ファイルを台帳に登録する（同一 rel_path は上書き）。`expires_at` は tz-aware の datetime または None（無期限）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "INSERT INTO personal_workspace_files "
            "  (user_id, rel_path, original_path, size_bytes, sha256, status, expires_at) "
            "VALUES (%s,%s,%s,%s,%s,'uploaded',%s) "
            "ON CONFLICT (user_id, rel_path) DO UPDATE SET "
            "  original_path=EXCLUDED.original_path, size_bytes=EXCLUDED.size_bytes, "
            "  sha256=EXCLUDED.sha256, status='uploaded', created_at=now(), "
            "  expires_at=EXCLUDED.expires_at, deleted_at=NULL "
            "RETURNING id, user_id, rel_path, original_path, size_bytes, sha256, "
            "  status, created_at, expires_at",
            (uid, rel_path, original_path, size_bytes, sha256, expires_at),
        ).fetchone()


def list_workspace_files(uid: str) -> list:
    """ユーザーの個人 workspace ファイル一覧（削除済み・期限切れ除外）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT id, rel_path, original_path, size_bytes, sha256, status, created_at, expires_at "
            "FROM personal_workspace_files "
            "WHERE user_id=%s AND status='uploaded' AND deleted_at IS NULL "
            "  AND (expires_at IS NULL OR expires_at > now()) "
            "ORDER BY created_at DESC",
            (uid,),
        ).fetchall()


def delete_workspace_file(uid: str, file_id: int) -> dict | None:
    """個人 workspace ファイルを論理削除する（status='deleted'）。所有者以外の操作は None。"""
    _ensure()
    with _connect() as c:
        row = c.execute(
            "UPDATE personal_workspace_files "
            "SET status='deleted', deleted_at=now() "
            "WHERE id=%s AND user_id=%s AND status='uploaded' "
            "RETURNING id, user_id, rel_path, original_path",
            (file_id, uid),
        ).fetchone()
    return row


def get_workspace_file(uid: str, file_id: int) -> dict | None:
    """個人 workspace ファイル1件取得（所有者確認用・期限切れは返さない）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT id, user_id, rel_path, original_path, size_bytes, sha256, status "
            "FROM personal_workspace_files "
            "WHERE id=%s AND user_id=%s AND status='uploaded' AND deleted_at IS NULL "
            "  AND (expires_at IS NULL OR expires_at > now())",
            (file_id, uid),
        ).fetchone()


def expired_workspace_files() -> list:
    """期限切れ（expires_at <= now()）の status='uploaded' 行を返す（TTL 掃除用）。無効化ユーザーの行は除く。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT f.id, f.user_id, f.rel_path, f.original_path "
            "FROM personal_workspace_files f "
            "JOIN users u ON u.uid = f.user_id "
            "WHERE f.status = 'uploaded' "
            "  AND f.expires_at IS NOT NULL "
            "  AND f.expires_at <= now() "
            "  AND f.deleted_at IS NULL "
            "  AND u.status != 'disabled'",
        ).fetchall()


def claim_workspace_file_expired(file_id: int) -> dict | None:
    """台帳行を status='expired' に条件付き UPDATE し、成功行（id + rel_path + user_id）を返す。
    条件（uploaded かつ未削除・期限切れ・所有者が無効化されていない）を同一文で再検証し、不成立なら None（物理削除禁止）。
    呼び出し側は claim 後に `no_live_upload_for_path()` で再アップロードが無いことを確かめてから unlink する。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            "UPDATE personal_workspace_files p "
            "SET status='expired', deleted_at=now() "
            "FROM users u "
            "WHERE p.id = %s "
            "  AND p.user_id = u.uid "
            "  AND p.status = 'uploaded' "
            "  AND p.deleted_at IS NULL "
            "  AND p.expires_at IS NOT NULL "
            "  AND p.expires_at <= now() "
            "  AND u.status <> 'disabled' "
            "RETURNING p.id, p.user_id, p.rel_path",
            (file_id,),
        ).fetchone()


def no_live_upload_for_path(uid: str, rel_path: str) -> bool:
    """指定の (user_id, rel_path) に status='uploaded' の行が無ければ True（再アップロードが無く unlink してよい）。"""
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT 1 FROM personal_workspace_files "
            "WHERE user_id = %s AND rel_path = %s AND status = 'uploaded' AND deleted_at IS NULL",
            (uid, rel_path),
        ).fetchone()
    return row is None


def live_workspace_rel_paths(uid: str) -> set[str]:
    """台帳上 status='uploaded' の rel_path 集合を返す（台帳基準の grep 用。論理削除済み・期限切れは含まない）。"""
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT rel_path FROM personal_workspace_files "
            "WHERE user_id=%s AND status='uploaded' AND deleted_at IS NULL "
            "  AND (expires_at IS NULL OR expires_at > now())",
            (uid,),
        ).fetchall()
    return {r["rel_path"] for r in rows}
