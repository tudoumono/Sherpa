"""個人 workspace ファイル台帳（2026-07-01-認証と共有の提案.md §5・W4 TTL）。

`sherpa/store/__init__.py` から純移動（フェーズ4 S9）。ロジックは一切変更していない。
不変条件: このモジュールの関数は個人ファイルの台帳管理だけを行う。
ES/Neo4j/共有 KB の取り込みからは参照されない（RAG 非索引）。
`workspace_file_lock` は db.py 所属のまま（S1 で先に純移動済み・本モジュールでは扱わない）。
`set_contains_personal_workspace`（会話への個人参照フラグ）は conversations テーブルを更新する
ため conversations.py 側（S10）に置く（計画のドメイン割り付けどおり）。
"""
from __future__ import annotations

from .db import _connect, _ensure


def record_workspace_file(uid: str, rel_path: str, original_path: str,
                          size_bytes: int, sha256: str,
                          expires_at=None) -> dict:
    """個人 workspace ファイルを台帳に登録（同一 rel_path は上書き upsert）。

    `expires_at`: datetime（tz-aware 推奨）または None（無期限）。W4 TTL。
    """
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
    """ユーザーの個人 workspace ファイル一覧（削除済み除外）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT id, rel_path, original_path, size_bytes, sha256, status, created_at, expires_at "
            "FROM personal_workspace_files "
            "WHERE user_id=%s AND status='uploaded' AND deleted_at IS NULL "
            "ORDER BY created_at DESC",
            (uid,),
        ).fetchall()


def delete_workspace_file(uid: str, file_id: int) -> dict | None:
    """個人 workspace ファイルを論理削除（status='deleted'・deleted_at 更新）。
    所有者以外の操作は None を返す。"""
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
    """個人 workspace ファイル1件取得（所有者確認用）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT id, user_id, rel_path, original_path, size_bytes, sha256, status "
            "FROM personal_workspace_files "
            "WHERE id=%s AND user_id=%s AND status='uploaded' AND deleted_at IS NULL",
            (file_id, uid),
        ).fetchone()


def expired_workspace_files() -> list:
    """期限切れ（expires_at <= now()）の status='uploaded' 行を返す。

    W4 TTL 掃除用。無効化ユーザー（users.status='disabled'）の行は **除外**（保持）。
    不変条件: personal_workspace_files のみ参照。ES/Neo4j とは無関係。
    """
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
            "  AND u.status != 'disabled'",   # 無効化ユーザーの領域は保持（plan §W4）
        ).fetchall()


def mark_workspace_file_expired(file_id: int) -> bool:
    """台帳行を status='expired' に強制更新（テスト/管理用・競合チェックなし）。
    sweep パスでは使わない。sweep は claim_workspace_file_expired を使う。
    """
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE personal_workspace_files "
            "SET status='expired', deleted_at=now() "
            "WHERE id=%s AND status='uploaded'",
            (file_id,),
        ).rowcount
    return n > 0


def claim_workspace_file_expired(file_id: int) -> dict | None:
    """台帳行を status='expired' に条件付き UPDATE し、成功行（id + rel_path + user_id）を返す。

    UPDATE は以下の全条件を同一ステートメントで再検証する（SELECT→UPDATE のギャップなし）:
      - status='uploaded' かつ deleted_at IS NULL（二重削除防止）
      - expires_at IS NOT NULL かつ expires_at <= now()（真に期限切れ）
      - オーナーユーザーが status='disabled' でない（無効化ユーザーの保持）
    上記の全条件を満たすときのみ行が更新されて RETURNING で返る（None = 条件不成立 = 物理削除禁止）。

    注: 再アップロード（claim→unlink のギャップ）対策として rel_path も返す。
    呼出側は claimed 後に no_live_upload_for_path() で re-upload が起きていないかを確認してから unlink。
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
    """指定の (user_id, rel_path) に status='uploaded' の生きた行が存在しなければ True を返す。

    _sweep_expired_workspace で claim → unlink のギャップ中に再アップロードが起きていないか
    を確認するために使う。True = 安全に unlink 可、False = 再アップロード検出 = unlink 禁止。
    """
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT 1 FROM personal_workspace_files "
            "WHERE user_id = %s AND rel_path = %s AND status = 'uploaded' AND deleted_at IS NULL",
            (uid, rel_path),
        ).fetchone()
    return row is None


def live_workspace_rel_paths(uid: str) -> set[str]:
    """台帳上 status='uploaded' の rel_path 集合を返す（W1: 台帳基準 grep 用）。
    この集合だけを grep 対象にすることで、論理削除済みファイルの FS 残骸がヒットしない。
    不変条件: このテーブルは RAG（ES/Neo4j）と無関係。個人 workspace 台帳のみ。
    """
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT rel_path FROM personal_workspace_files "
            "WHERE user_id=%s AND status='uploaded' AND deleted_at IS NULL",
            (uid,),
        ).fetchall()
    return {r["rel_path"] for r in rows}
