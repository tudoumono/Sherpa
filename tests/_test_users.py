"""テストユーザー作成の残骸を防ぐ共通登録簿＋クリーンアップ（2026-07・ユーザー指摘の再発防止）。

背景: dev DB の `users` がテスト実行の蓄積で肥大化していた（実アカウントは admin 1件のみなのに
数千件規模）。各 `tests/api/test_*.py` の `_mk_user`/`_mk_admin` 系ヘルパ・API 経由でのユーザー作成
（POST /admin/users）は、作成したら本モジュールの `register_test_uid(uid)` を呼ぶ。実際の削除は
`tests/api/conftest.py` の autouse fixture が各テスト終了後に `drain_registered_uids()` で
取り出し `cleanup_users()` に渡して行う（test_ 接頭辞でないので pytest 収集対象外＝
`tests/_world_setup.py` と同じ位置づけ）。

**audit_log は一切参照・削除しない**（hash-chain・append-only が設計＝改ざん検知の前提を壊さない。
残骸ユーザーの actor_user_id 参照が audit_log に残るのは正常な状態）。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

_REGISTRY: list[str] = []


def register_test_uid(uid: str | None) -> None:
    """このプロセスで作成したテストユーザー uid を記録する。

    'admin'・空値は無視する（誤って渡っても実アカウントを消してしまわないための多層防御）。
    """
    if uid and uid != "admin":
        _REGISTRY.append(uid)


def drain_registered_uids() -> list[str]:
    """登録済み uid を重複除去して取り出し、レジストリを空にする（fixture teardown から呼ぶ）。"""
    uids = list(dict.fromkeys(_REGISTRY))
    _REGISTRY.clear()
    return uids


def cleanup_users(uids: list[str]) -> dict:
    """指定 uid の `users` 行＋従属データを削除する。

    'admin' は常に除外する（呼び出し側が誤って含めても無視＝多層防御）。

    削除順序（FK 制約の都合。`conversations.share_id` は ON DELETE を指定していないため
    RESTRICT 相当＝受領ラッパーが参照している共有元 conversation は先にラッパーを消さないと
    削除できない）:
      1. 対象ユーザーが所有する conversations を共有元とする conversation_shares を参照している
         受領ラッパー（`conversations.share_id`）を先に削除する。
      2. 対象ユーザーに紐づく conversations を削除する（`ON DELETE CASCADE` で messages・
         conversation_shares → conversation_share_invites も一緒に消える）。
      3. auth_sessions（users への FK が無いため明示削除が必要）。
      4. user_settings（同上）。
      4.5. bedrock_verified_models（2026-07-16・RV MED F1/F4/F6 是正で新設した専用テーブル。
           `user_settings` と同じく users への FK が無いため明示削除が必要。verify/列挙を直接叩く
           store 単体テスト（tests/api/test_bedrock_settings.py）が users 行を作らずにこのテーブルへ
           書き込むことがあるため、users 削除の成否とは独立して uids ベースで消す）。
      5. conversation_share_invites の招待先として残る分（FK 無し・念のため明示削除）。
      6. users 本体を削除する（personal_workspace_files は `ON DELETE CASCADE` で自動的に消える）。
      7. `SHERPA_USERS_DIR`（既定 `data/users`）配下の `{uid}` 実ディレクトリを削除する。

    audit_log は一切触れない。
    """
    uids = [u for u in dict.fromkeys(uids) if u and u != "admin"]
    if not uids:
        return {"deleted_users": 0, "uids": []}

    from sherpa import store

    store._ensure()
    with store._connect() as c:
        share_ids = [
            r["id"]
            for r in c.execute(
                "SELECT id FROM conversation_shares WHERE conversation_id IN "
                "  (SELECT id FROM conversations WHERE user_id = ANY(%s))",
                (uids,),
            ).fetchall()
        ]
        if share_ids:
            c.execute("DELETE FROM conversations WHERE share_id = ANY(%s)", (share_ids,))
        c.execute("DELETE FROM conversations WHERE user_id = ANY(%s)", (uids,))
        c.execute("DELETE FROM conversation_share_invites WHERE invitee_user_id = ANY(%s)", (uids,))
        c.execute("DELETE FROM auth_sessions WHERE user_id = ANY(%s)", (uids,))
        c.execute("DELETE FROM user_settings WHERE user_id = ANY(%s)", (uids,))
        c.execute("DELETE FROM bedrock_verified_models WHERE user_id = ANY(%s)", (uids,))
        n = c.execute("DELETE FROM users WHERE uid = ANY(%s)", (uids,)).rowcount

    users_dir = Path(os.environ.get("SHERPA_USERS_DIR", "data/users"))
    for uid in uids:
        d = users_dir / uid
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)

    return {"deleted_users": n, "uids": uids}
