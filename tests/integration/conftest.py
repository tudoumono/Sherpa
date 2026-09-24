"""tests/integration 共通設定・fixture（refactoring-plan フェーズ0 スライス2b）。

tests/api と同様、`sherpa.api.app` を TestClient で直接叩くファイルがある
（test_ingest_p1.py の一部・test_worlds_admin.py の大半）。ログインせず直接叩く前提の箇所は
`auth_disabled` fixture（monkeypatch・`auth.auth_disabled()` は呼び出し時に env を読むため
import 順に依存しない）を使う。

tests/integration は `sherpa.api._USERS_DIR` のような import 時定数には依存しないため
（personal workspace 系のテストは tests/api 側）、env の確定はここでは最小限でよい。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")   # 実埋め込み API を叩かない（既定 BM25）
os.environ.pop("SHERPA_AUTH_DISABLED", None)


@pytest.fixture
def auth_disabled(monkeypatch):
    """互換モード（`SHERPA_AUTH_DISABLED=1`・ログイン不要で合成 admin）に切り替える。

    テスト終了で自動的に既定の実認証へ戻り、他ファイルに影響しない。
    """
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")
