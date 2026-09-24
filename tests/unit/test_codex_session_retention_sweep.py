"""R1b（会話継続・Codex ネイティブ resume・決定5）: `api._sweep_expired_codex_sessions` の単体テスト。

会話ごとの Codex resume セッション実体（`workspace/.codex-sessions/{cid}`）を、admin 設定
`codex_session_retention_days`（system_settings・既定0=無制限）に従って掃除する背景処理。
既存の workspace TTL sweep（`_sweep_expired_workspace`/`_gc_orphan_workspace_files`）と同じ
「安全に自動」思想（DB/設定不達なら何もしない・symlink は触らない・base-confined）を検証する。

`_USERS_DIR` は `sherpa.api` モジュール属性を直接 monkeypatch する（tests/api の conftest が
固定する共有の実 SHERPA_USERS_DIR とは独立に、本ファイルは per-test tmp_path で完全隔離する）。
DB は使わない（`store.get_system_settings` を monkeypatch）。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import api  # noqa: E402
from sherpa import store  # noqa: E402


def _mk_session_dir(users_dir: Path, uid: str, cid: str, age_days: float | None = None) -> Path:
    """`users_dir/uid/workspace/.codex-sessions/cid` を作り、中に config.toml 相当のダミーを置く。
    `age_days` を指定するとディレクトリの mtime をその日数だけ過去にずらす（None なら「今」のまま）。"""
    d = users_dir / uid / "workspace" / ".codex-sessions" / cid
    d.mkdir(parents=True)
    (d / "sessions_marker.jsonl").write_text("dummy\n")
    if age_days is not None:
        past = time.time() - age_days * 86400
        os.utime(d, (past, past))
    return d


def test_sweep_skips_when_retention_unset_default_zero(tmp_path, monkeypatch):
    users_dir = tmp_path / "users"
    d = _mk_session_dir(users_dir, "u1", "1", age_days=365)
    monkeypatch.setattr(api, "_USERS_DIR", users_dir)
    monkeypatch.setattr(store, "get_system_settings", lambda: {})

    result = api._sweep_expired_codex_sessions()

    assert result == {"skipped": "unlimited"}
    assert d.is_dir(), "既定（未設定=無制限）で削除されてしまった"


def test_sweep_skips_when_retention_explicitly_zero(tmp_path, monkeypatch):
    users_dir = tmp_path / "users"
    d = _mk_session_dir(users_dir, "u1", "1", age_days=365)
    monkeypatch.setattr(api, "_USERS_DIR", users_dir)
    monkeypatch.setattr(store, "get_system_settings", lambda: {"codex_session_retention_days": 0})

    result = api._sweep_expired_codex_sessions()

    assert result == {"skipped": "unlimited"}
    assert d.is_dir()


def test_sweep_deletes_directories_older_than_retention(tmp_path, monkeypatch):
    users_dir = tmp_path / "users"
    old = _mk_session_dir(users_dir, "u1", "old-conv", age_days=10)
    fresh = _mk_session_dir(users_dir, "u1", "fresh-conv", age_days=0.01)
    monkeypatch.setattr(api, "_USERS_DIR", users_dir)
    monkeypatch.setattr(store, "get_system_settings", lambda: {"codex_session_retention_days": 7})

    result = api._sweep_expired_codex_sessions()

    assert result["deleted"] == 1 and result.get("failed", 0) == 0
    assert not old.exists(), "保持期間（7日）を超えたセッションが削除されていない"
    assert fresh.is_dir(), "保持期間内のセッションが誤って削除された"


def test_sweep_across_multiple_users(tmp_path, monkeypatch):
    users_dir = tmp_path / "users"
    old_u1 = _mk_session_dir(users_dir, "u1", "c1", age_days=10)
    old_u2 = _mk_session_dir(users_dir, "u2", "c2", age_days=10)
    fresh_u2 = _mk_session_dir(users_dir, "u2", "c3", age_days=0)
    monkeypatch.setattr(api, "_USERS_DIR", users_dir)
    monkeypatch.setattr(store, "get_system_settings", lambda: {"codex_session_retention_days": 1})

    result = api._sweep_expired_codex_sessions()

    assert result["deleted"] == 2
    assert not old_u1.exists() and not old_u2.exists()
    assert fresh_u2.is_dir()


def test_sweep_settings_unreachable_is_fail_safe(tmp_path, monkeypatch):
    users_dir = tmp_path / "users"
    d = _mk_session_dir(users_dir, "u1", "1", age_days=365)
    monkeypatch.setattr(api, "_USERS_DIR", users_dir)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_system_settings", _boom)

    result = api._sweep_expired_codex_sessions()

    assert result == {"skipped": "settings_unreachable"}
    assert d.is_dir(), "設定取得不可時に削除してしまった（fail-safe 崩れ）"


def test_sweep_no_users_dir_is_fail_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_USERS_DIR", tmp_path / "does-not-exist")
    monkeypatch.setattr(store, "get_system_settings", lambda: {"codex_session_retention_days": 1})

    result = api._sweep_expired_codex_sessions()

    assert result == {"skipped": "no_users_dir"}


def test_sweep_does_not_descend_into_symlinked_sessions_root(tmp_path, monkeypatch):
    """`.codex-sessions` 自体が symlink なら中身を見ずスキップする（封じ込め崩壊防止）。"""
    users_dir = tmp_path / "users"
    real_target = tmp_path / "elsewhere" / ".codex-sessions"
    real_target.mkdir(parents=True)
    old_outside = real_target / "victim"
    old_outside.mkdir()
    past = time.time() - 10 * 86400
    os.utime(old_outside, (past, past))

    (users_dir / "u1" / "workspace").mkdir(parents=True)
    (users_dir / "u1" / "workspace" / ".codex-sessions").symlink_to(real_target)
    monkeypatch.setattr(api, "_USERS_DIR", users_dir)
    monkeypatch.setattr(store, "get_system_settings", lambda: {"codex_session_retention_days": 1})

    result = api._sweep_expired_codex_sessions()

    assert result == {"deleted": 0, "failed": 0}
    assert old_outside.is_dir(), "symlink 経由で外部ディレクトリの中身まで削除してしまった"


def test_sweep_does_not_delete_symlinked_conversation_dir(tmp_path, monkeypatch):
    """`.codex-sessions/{cid}` 個々が symlink の場合は削除しない（is_symlink 事前チェック）。"""
    users_dir = tmp_path / "users"
    real_target = tmp_path / "elsewhere" / "real-conv"
    real_target.mkdir(parents=True)
    past = time.time() - 10 * 86400
    os.utime(real_target, (past, past))

    sessions_root = users_dir / "u1" / "workspace" / ".codex-sessions"
    sessions_root.mkdir(parents=True)
    (sessions_root / "linked-conv").symlink_to(real_target)
    monkeypatch.setattr(api, "_USERS_DIR", users_dir)
    monkeypatch.setattr(store, "get_system_settings", lambda: {"codex_session_retention_days": 1})

    result = api._sweep_expired_codex_sessions()

    assert result == {"deleted": 0, "failed": 0}
    assert real_target.is_dir(), "symlink セッションディレクトリの指す先が削除されてしまった"
    assert (sessions_root / "linked-conv").is_symlink(), "symlink 自体が消えている"


def test_sweep_is_wired_into_workspace_maintenance(monkeypatch):
    """`_run_workspace_maintenance`（起動時／定期ポーリング共通）が本 sweep も呼ぶことを確認する。"""
    called = []
    monkeypatch.setattr(api, "_sweep_expired_codex_sessions", lambda: called.append(True))
    monkeypatch.setattr(api, "_sweep_expired_workspace", lambda: {"skipped": "test"})
    monkeypatch.setattr(api, "_gc_orphan_workspace_files", lambda: {"skipped": "test"})
    monkeypatch.setattr(api, "_sweep_expired_announcements", lambda: {"skipped": "test"})

    api._run_workspace_maintenance()

    assert called == [True], "_run_workspace_maintenance が codex session sweep を呼んでいない"


def test_sweep_error_in_maintenance_does_not_abort_other_sweeps(monkeypatch):
    """R1b sweep が例外を投げても、他の maintenance（workspace/gc/announcements）は実行される（best-effort）。"""
    called = []
    monkeypatch.setattr(api, "_sweep_expired_workspace", lambda: called.append("ws"))
    monkeypatch.setattr(api, "_gc_orphan_workspace_files", lambda: called.append("gc"))
    monkeypatch.setattr(api, "_sweep_expired_announcements", lambda: called.append("ann"))

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(api, "_sweep_expired_codex_sessions", _boom)

    api._run_workspace_maintenance()   # 例外を外へ漏らさない

    assert called == ["ws", "gc", "ann"]
