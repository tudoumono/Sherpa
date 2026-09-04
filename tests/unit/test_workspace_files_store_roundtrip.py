"""sherpa/store/workspace_files.py の unit テスト（フェーズ7 S6・26%→引き上げ）。

個人 workspace ファイル台帳を実 DB（非破壊）で round-trip する: record→list→get→delete、
claim_workspace_file_expired の全条件分岐（未期限・無期限・二重 claim・無効化ユーザーの保持）、
re-upload による同一行 revive、expired_workspace_files/live_workspace_rel_paths の絞り込み。

`personal_workspace_files.user_id` は `users(uid)` への FK のため、各テストで実 user 行を先に作る。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from sherpa import store


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _try_init() -> None:
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _mk_user(tag: str) -> str:
    uid = f"unit-wsfile-{tag}"
    store.upsert_user(uid, display_name="WS", password_hash="x", status="active")
    return uid


def test_record_list_get_delete_round_trip():
    _try_init()
    sfx = _sfx()
    uid = _mk_user(sfx)
    rec = store.record_workspace_file(uid, "a.txt", "/orig/a.txt", 10, "sha-a")
    assert rec["rel_path"] == "a.txt" and rec["status"] == "uploaded"

    listed = store.list_workspace_files(uid)
    assert any(f["id"] == rec["id"] for f in listed)

    got = store.get_workspace_file(uid, rec["id"])
    assert got is not None and got["rel_path"] == "a.txt"
    assert store.get_workspace_file("someone-else", rec["id"]) is None   # 所有者以外は None

    deleted = store.delete_workspace_file(uid, rec["id"])
    assert deleted is not None and deleted["id"] == rec["id"]
    assert store.get_workspace_file(uid, rec["id"]) is None       # 削除後は取得不可
    assert store.delete_workspace_file(uid, rec["id"]) is None    # 二重削除は None


def test_record_workspace_file_upsert_revives_same_row_on_reupload():
    _try_init()
    sfx = _sfx()
    uid = _mk_user(sfx)
    first = store.record_workspace_file(uid, "b.txt", "/orig/b.txt", 5, "sha-1")
    store.delete_workspace_file(uid, first["id"])
    second = store.record_workspace_file(uid, "b.txt", "/orig/b.txt", 9, "sha-2")
    assert second["id"] == first["id"]      # ON CONFLICT (user_id, rel_path) は同じ行を revive
    assert second["status"] == "uploaded" and second["sha256"] == "sha-2"
    assert store.no_live_upload_for_path(uid, "nonexistent.txt") is True
    assert store.no_live_upload_for_path(uid, "b.txt") is False


def test_claim_workspace_file_expired_requires_all_conditions():
    _try_init()
    sfx = _sfx()
    uid = _mk_user(sfx)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=1)

    not_yet = store.record_workspace_file(uid, "not_yet.txt", "/orig/n.txt", 1, "sha-n", expires_at=future)
    assert store.claim_workspace_file_expired(not_yet["id"]) is None   # まだ期限内

    no_expiry = store.record_workspace_file(uid, "no_expiry.txt", "/orig/x.txt", 1, "sha-x", expires_at=None)
    assert store.claim_workspace_file_expired(no_expiry["id"]) is None   # 無期限は対象外

    expired = store.record_workspace_file(uid, "expired.txt", "/orig/e.txt", 1, "sha-e", expires_at=past)
    claimed = store.claim_workspace_file_expired(expired["id"])
    assert claimed is not None and claimed["rel_path"] == "expired.txt"
    assert store.claim_workspace_file_expired(expired["id"]) is None   # 二重 claim は不成立（既に expired）

    # 無効化ユーザーの領域は保持（claim 不成立・W4 §plan）。
    disabled_uid = _mk_user(f"disabled-{sfx}")
    store.upsert_user(disabled_uid, status="disabled")
    disabled_file = store.record_workspace_file(disabled_uid, "d.txt", "/orig/d.txt", 1, "sha-d", expires_at=past)
    assert store.claim_workspace_file_expired(disabled_file["id"]) is None


def test_expired_workspace_files_excludes_live_paths_and_mark_expired_is_idempotent():
    _try_init()
    sfx = _sfx()
    uid = _mk_user(sfx)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    live = store.record_workspace_file(uid, "live.txt", "/orig/live.txt", 1, "sha-l")
    expired = store.record_workspace_file(uid, "gone.txt", "/orig/gone.txt", 1, "sha-g", expires_at=past)

    ids = {r["id"] for r in store.expired_workspace_files()}
    assert expired["id"] in ids
    assert live["id"] not in ids

    # live_workspace_rel_paths は status='uploaded' のみを見る（expires_at 非依存＝claim 前は含まれる）。
    paths_before_claim = store.live_workspace_rel_paths(uid)
    assert {"live.txt", "gone.txt"} <= paths_before_claim

    store.claim_workspace_file_expired(expired["id"])   # sweep 相当で status='expired' に遷移
    paths_after_claim = store.live_workspace_rel_paths(uid)
    assert "live.txt" in paths_after_claim and "gone.txt" not in paths_after_claim

    assert store.mark_workspace_file_expired(live["id"]) is True
    assert store.mark_workspace_file_expired(live["id"]) is False   # 既に uploaded でない→再実行は不成立
