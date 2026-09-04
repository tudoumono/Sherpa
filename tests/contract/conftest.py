"""tests/contract 共通 fixture。"""
from __future__ import annotations

import pytest

from _ai_env_isolation import AI_ENV_VARS, CODEX_HOME_SENTINEL


@pytest.fixture(autouse=True)
def _isolate_ai_env(monkeypatch):
    """AI 系 env を各テスト開始前に隔離する（`tests/unit/conftest.py` と同じ fixture・
    一覧は `tests/_ai_env_isolation.py` 参照）。子プロセスへ渡す env を個別に組み立てるテスト
    （`test_azure_smoke_script.py::_run` 等）は、この隔離済み `os.environ` を土台にする。"""
    for name in AI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_HOME", CODEX_HOME_SENTINEL)
