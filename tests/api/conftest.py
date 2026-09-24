"""tests/api 共通設定・fixture（refactoring-plan フェーズ0 スライス2b）。

tests/api 配下は FastAPI TestClient 経由で `sherpa.api.app` を叩く。以前は各ファイルが
`python3 file.py` の別プロセス直実行（Makefile の for ループ）を前提に、import 直後で
`os.environ` を書き換えていた。pytest で一括収集すると同一プロセスに複数ファイルが同居するため、
**import 時にしか読まれない env**（module 定数）は最初に import したファイルの値で固定されてしまい、
後続ファイルが書き換えても手遅れになる（実測で確認済み: `sherpa.api._USERS_DIR` /
`_WORKSPACE_TTL_DAYS` は import 時の1度きりの評価）。

このファイルが解決する対象:
  - `SHERPA_USERS_DIR` / `SHERPA_WORKSPACE_TTL_DAYS`（api.py が import 時に1度だけ読む定数）
    → tests/api のどの test_*.py よりも**先に**（conftest はテストファイル収集前に import される）
      ここで確定する。個人 workspace を一時ディレクトリへ逃がしたい各ファイルは
      `os.environ["SHERPA_USERS_DIR"]` を**読むだけ**にし、自前の `tempfile.mkdtemp` は行わない
      （uid は各ファイルでプレフィックスが異なるため一つの一時ディレクトリを共有しても衝突しない）。
  - `sherpa.auth.auth_disabled()` は**呼び出し時に** env を読むため import 順に依存しない。
    既定は実認証（ログイン必須）。互換モード（ログイン不要・合成 admin）が要るテストファイルは
    `auth_disabled` fixture（monkeypatch・自動復元）か、ファイル内の autouse fixture を使う。
"""
from __future__ import annotations

import os
import tempfile

import pytest

# ---- import 時定数の確定（tests/api 配下のどの test_*.py よりも先に読み込まれる）----
os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")   # 実埋め込み API を叩かない（ES は BM25 のみ）
os.environ.pop("SHERPA_AUTH_DISABLED", None)
os.environ.setdefault("SHERPA_USERS_DIR", tempfile.mkdtemp(prefix="sherpa_api_users_"))
os.environ.setdefault("SHERPA_WORKSPACE_TTL_DAYS", "90")
# W2'（2026-07-08）: office_com direct モードは実 powershell.exe を検出すると one-shot を起動しうる。
# _admin_settings_view は office_com の到達性を毎回プローブする＝開発機（実 Windows ホスト付き WSL）で
# api を走らせても実プロセスを起こさないよう、既定で direct 検出を無効化する（direct を検証するテストは
# 本体で偽 powershell を monkeypatch する）。RV Low（2026-07-08）: `setdefault` だと開発機のシェルで既に
# export 済みの実 SHERPA_POWERSHELL_BIN（例: 実機検証セッションの残り）に迂回されうる＝
# tests/unit/conftest.py の autouse fixture（`monkeypatch.setenv`＝常に上書き）と同じ「強制上書き」に統一する。
os.environ["SHERPA_POWERSHELL_BIN"] = "/nonexistent/sherpa-no-powershell"


@pytest.fixture
def auth_disabled(monkeypatch):
    """互換モード（`SHERPA_AUTH_DISABLED=1`・ログイン不要で合成 admin）に切り替える。

    `auth.auth_disabled()` は呼び出し時に env を読むため monkeypatch で十分
    （テスト終了で自動的に既定の実認証へ戻り、他ファイルに影響しない）。
    """
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


# ---- テストユーザーの残骸防止（2026-07・ユーザー指摘）----
# dev DB の `users` がテスト実行の蓄積で肥大化していた問題への対処。tests/_test_users.py 参照。


@pytest.fixture(autouse=True)
def _cleanup_test_users():
    """各テスト終了後、そのテストが作成した users 行＋従属データを削除する。

    各ファイルの `_mk_user`/`_mk_admin` 系ヘルパ・API 経由のユーザー作成は
    `_test_users.register_test_uid(uid)` を呼んでおく（本 fixture が teardown で回収する）。
    'admin'（実アカウント）は `cleanup_users` 側で常に除外される。audit_log は一切触らない
    （hash-chain・append-only）。DB 接続不可時はクリーンアップも失敗するが、その場合はテスト自体が
    `_try_init()` で先に SKIP しているはず＝到達しない想定。万一失敗しても次のテストへは進める
    （teardown 例外を握りつぶすと再発防止の意味が無いため、失敗は警告として可視化する）。
    """
    yield
    from _test_users import cleanup_users, drain_registered_uids

    uids = drain_registered_uids()
    if not uids:
        return
    try:
        cleanup_users(uids)
    except Exception as e:  # pragma: no cover - DB 障害時のみ
        import warnings

        warnings.warn(f"test user cleanup failed for {uids}: {e}", stacklevel=2)


@pytest.fixture
def user_factory():
    """テストユーザー作成の共通ファクトリ。作成した uid は自動的に teardown で削除される
    （`_cleanup_test_users` autouse fixture が実際の削除を行う）。

    使い方: `uid = user_factory("myuid123", "pw", role="admin")`。
    API 経由（`POST /admin/users` 等）で作成した uid は `user_factory.register(uid)` で登録する。
    """
    from sherpa import auth, store
    from _test_users import register_test_uid

    def _create(uid: str, password: str, *, role: str = "user", email: str | None = None,
               display_name: str | None = None, status: str = "active") -> str:
        store.upsert_user(uid, email=email, display_name=display_name or uid,
                          password_hash=auth.hash_password(password), role=role, status=status)
        register_test_uid(uid)
        return uid

    _create.register = register_test_uid
    return _create
