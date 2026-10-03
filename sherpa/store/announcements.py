"""運営掲示板（お知らせ）の保存・取得。"""
from __future__ import annotations

from .db import _connect, _ensure

_ANNOUNCEMENT_FIELDS = (
    "id, author_uid, title, body, category, pinned, published, publish_at, expire_at, created_at, updated_at"
)


def create_announcement(author_uid, title, body, category="notice", pinned=False, published=True,
                        publish_at=None, expire_at=None) -> dict:
    """お知らせを作成する。publish_at=None は即時公開、expire_at=None は無期限掲載。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            f"INSERT INTO announcements (author_uid, title, body, category, pinned, published, "
            f"  publish_at, expire_at) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_ANNOUNCEMENT_FIELDS}",
            (author_uid, title, body, category, bool(pinned), bool(published), publish_at, expire_at),
        ).fetchone()


def list_announcements(limit=20, offset=0, published_only=True) -> list:
    """お知らせ一覧（pinned 優先→新着順）。既定は published かつ掲載期間内のものだけ。"""
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


# publish_at/expire_at の「変更しない」を表す既定値（None は NULL へのクリアを意味する）。
_UNSET = object()


class AnnouncementOrderError(Exception):
    """更新後に publish_at > expire_at になる（呼び出し側で 422 に変換する）。"""


def update_announcement(aid, publish_at=_UNSET, expire_at=_UNSET, **fields) -> dict | None:
    """お知らせを部分更新する。許可フィールド（title/body/category/pinned/published）のみ反映する。
    None は変更しない（False は反映する）。publish_at/expire_at は明示的に渡したときだけ更新し、None なら NULL へクリアする。
    対象行を SELECT ... FOR UPDATE でロックしてから公開期間の前後関係を検証し、不正なら AnnouncementOrderError を投げる。
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
    """掲載終了日時を過ぎた行を条件付き DELETE で削除し、削除した行を返す（自動削除用）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            f"DELETE FROM announcements WHERE expire_at IS NOT NULL AND expire_at<=now() "
            f"RETURNING {_ANNOUNCEMENT_FIELDS}",
        ).fetchall()


# 監査書込失敗時の補償専用: id/created_at/updated_at も含めて before の値へ戻す。


def restore_announcement(row: dict) -> dict:
    """delete の監査失敗補償専用: 削除済み行を id/created_at/updated_at ごと再現する。"""
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
    """update の監査失敗補償専用: 更新対象列と updated_at を before のスナップショットへ戻す。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            f"UPDATE announcements SET title=%s, body=%s, category=%s, pinned=%s, published=%s, "
            f"  publish_at=%s, expire_at=%s, updated_at=%s WHERE id=%s RETURNING {_ANNOUNCEMENT_FIELDS}",
            (before["title"], before["body"], before["category"], bool(before["pinned"]),
             bool(before["published"]), before.get("publish_at"), before.get("expire_at"),
             before["updated_at"], aid),
        ).fetchone()
