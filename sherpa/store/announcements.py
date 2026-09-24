"""運営掲示板（2026-07-02-利用統計とホーム掲示板.md Feature 2）。

`sherpa/store/__init__.py` から純移動（フェーズ4 S2）。ロジックは一切変更していない。
"""
from __future__ import annotations

from .db import _connect, _ensure

_ANNOUNCEMENT_FIELDS = (
    "id, author_uid, title, body, category, pinned, published, publish_at, expire_at, created_at, updated_at"
)


def create_announcement(author_uid, title, body, category="notice", pinned=False, published=True,
                        publish_at=None, expire_at=None) -> dict:
    """お知らせを作成する。publish_at=None＝即時公開扱い、expire_at=None＝無期限掲載（S4）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            f"INSERT INTO announcements (author_uid, title, body, category, pinned, published, "
            f"  publish_at, expire_at) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_ANNOUNCEMENT_FIELDS}",
            (author_uid, title, body, category, bool(pinned), bool(published), publish_at, expire_at),
        ).fetchone()


def list_announcements(limit=20, offset=0, published_only=True) -> list:
    """お知らせ一覧（pinned 優先→新着順）。既定は published=true かつ掲載期間内のみ
    （publish_at 未到来・expire_at 経過は利用者向けトップ画面には出さない・S4）。
    """
    _ensure()
    where = ("WHERE published=TRUE AND (publish_at IS NULL OR publish_at<=now()) "
            "AND (expire_at IS NULL OR expire_at>now()) ") if published_only else ""
    with _connect() as c:
        return c.execute(
            f"SELECT {_ANNOUNCEMENT_FIELDS} FROM announcements {where}"
            f"ORDER BY pinned DESC, created_at DESC LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()


def get_announcement(aid) -> dict | None:
    _ensure()
    with _connect() as c:
        return c.execute(
            f"SELECT {_ANNOUNCEMENT_FIELDS} FROM announcements WHERE id=%s", (aid,)).fetchone()


# publish_at/expire_at の「変更しない」判定専用センチネル（S4）。この2列は None が「NULLへクリア
# （今すぐ公開／無期限に戻す）」という**正当な明示更新**なので、他の allowlist フィールドと同じ
# 「None＝変更しない」規約を適用できない。呼び出し側（api.py）は変更したい時だけ kwarg を渡す
# （渡さなければこの既定値のまま＝未指定として扱われる）。
_UNSET = object()


class AnnouncementOrderError(Exception):
    """更新後に publish_at > expire_at になる（S4 RV1・呼出側で 422 に変換する）。"""


def update_announcement(aid, publish_at=_UNSET, expire_at=_UNSET, **fields) -> dict | None:
    """お知らせを部分更新する。許可フィールド（title/body/category/pinned/published）のみ反映。

    未指定（None）のフィールドは変更しない。`published=False` のような明示的な False は反映する
    （`v is not None` で判定するため bool の False は落ちない）。
    `publish_at`/`expire_at` だけは別扱い（S4）: 呼び出し側が明示的に渡した場合のみ更新し、
    渡した値が None なら NULL へクリアする（キーワード省略時だけ「変更しない」＝`_UNSET` 既定値）。

    RV1（2026-07・並行更新対策）: 対象行を `SELECT...FOR UPDATE` でロックしてから現在値を読み、
    「今回の更新後に有効になる」publish_at/expire_at（未指定分はロック済みの現在値）を検証する。
    2つの PATCH が publish_at と expire_at を同時に別々に更新しても、行ロックにより直列化され、
    後続の PATCH は先行 PATCH の commit 後の値を「現在値」として見る＝各リクエスト単体の検証だけで
    整合性を保証できる（DB CHECK 制約 `announcements_publish_before_expire` は最後の砦として別途ある）。
    不正な組み合わせは `AnnouncementOrderError` を投げる（呼出側で 422 に変換）。
    """
    _ensure()
    allowed = ("title", "body", "category", "pinned", "published")
    upd = {k: v for k, v in fields.items() if k in allowed and v is not None}
    with _connect() as c:
        current = c.execute(
            f"SELECT {_ANNOUNCEMENT_FIELDS} FROM announcements WHERE id=%s FOR UPDATE", (aid,)).fetchone()
        if not current:
            return None
        if publish_at is not _UNSET:
            upd["publish_at"] = publish_at
        if expire_at is not _UNSET:
            upd["expire_at"] = expire_at
        eff_publish = upd.get("publish_at", current.get("publish_at"))
        eff_expire = upd.get("expire_at", current.get("expire_at"))
        if eff_publish and eff_expire and eff_publish > eff_expire:
            raise AnnouncementOrderError("公開日時は掲載終了日時より前にしてください")
        if not upd:
            return current
        set_clause = ", ".join(f"{k}=%s" for k in upd)
        params = list(upd.values()) + [aid]
        return c.execute(
            f"UPDATE announcements SET {set_clause}, updated_at=now() WHERE id=%s "
            f"RETURNING {_ANNOUNCEMENT_FIELDS}",
            params,
        ).fetchone()


def delete_announcement(aid) -> bool:
    _ensure()
    with _connect() as c:
        n = c.execute("DELETE FROM announcements WHERE id=%s", (aid,)).rowcount
    return n > 0


def delete_expired_announcements() -> list:
    """掲載終了日時（expire_at）を過ぎた行を条件付きで削除し、削除できた行を返す（S4・自動削除 sweep 用）。

    RV2（2026-07・TOCTOU 対策）: 以前は「列挙 → 各 id を無条件削除」だったため、列挙〜削除の間に
    admin が expire_at を延長/クリアした行まで巻き添えで消えてしまう競合があった。DELETE 文自体に
    条件（`expire_at IS NOT NULL AND expire_at<=now()`）を持たせることで、削除の瞬間に各行の
    最新状態を再評価する＝先に admin の UPDATE が commit していれば、その版で条件不成立となり
    削除されない（Postgres の MVCC/行ロックにより自然に安全・claim 用の別ステップは不要）。
    RETURNING は監査 before_state 用に publish_at/expire_at も含める（RV4）。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            f"DELETE FROM announcements WHERE expire_at IS NOT NULL AND expire_at<=now() "
            f"RETURNING {_ANNOUNCEMENT_FIELDS}",
        ).fetchall()


# ---- 監査 fail-closed の補償専用ヘルパ（RV ラウンド2 MEDIUM）----
# announcement_create/update/delete の監査書込失敗時、変更を「完全に」元へ戻すために使う。
# 通常の create_announcement/update_announcement は id を新規採番・updated_at=now() を打つため
# 補償には使えない（id/created_at/updated_at まで含めて before の値へ戻す必要がある）。


def restore_announcement(row: dict) -> dict:
    """delete の監査失敗補償専用: 削除済み行を id/created_at/updated_at を含めて完全に再現する。

    `id` は SERIAL 列でも明示 INSERT 可能。削除直後の id は既にシーケンスの現在値より小さいため
    （シーケンスは前方専有＝一度発行した値まで戻らない）、明示 INSERT しても将来の nextval() と
    衝突しない。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            f"INSERT INTO announcements (id, author_uid, title, body, category, pinned, published, "
            f"  publish_at, expire_at, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            f"RETURNING {_ANNOUNCEMENT_FIELDS}",
            (row["id"], row["author_uid"], row["title"], row["body"], row["category"],
             bool(row["pinned"]), bool(row["published"]), row.get("publish_at"), row.get("expire_at"),
             row["created_at"], row["updated_at"]),
        ).fetchone()


def restore_announcement_state(aid, before: dict) -> dict | None:
    """update の監査失敗補償専用: title/body/category/pinned/published/publish_at/expire_at
    **と updated_at** をまとめて before スナップショットへ戻す（update_announcement は
    updated_at=now() を必ず打つため補償には使えない＝更新前の updated_at を明示的に書き戻す）。
    """
    _ensure()
    with _connect() as c:
        return c.execute(
            f"UPDATE announcements SET title=%s, body=%s, category=%s, pinned=%s, published=%s, "
            f"  publish_at=%s, expire_at=%s, updated_at=%s WHERE id=%s RETURNING {_ANNOUNCEMENT_FIELDS}",
            (before["title"], before["body"], before["category"], bool(before["pinned"]),
             bool(before["published"]), before.get("publish_at"), before.get("expire_at"),
             before["updated_at"], aid),
        ).fetchone()
