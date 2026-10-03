"""閉域向け「頭脳（AI）の選び方」案内の契約。"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"
INSTALL_KIT = ROOT / "scripts" / "install_offline_kit.sh"
MANUAL = ROOT / "docs" / "manual" / "offline-kit.md"


def _active(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


@pytest.mark.parametrize("path", [INSTALL_KIT, MANUAL], ids=lambda p: p.name)
def test_shipped_guidance(path: Path):
    text = "\n".join(_active(path))
    # AI なしの定型応答（heuristic）はチャットで閉じた＝推奨しない
    assert "SHERPA_AGENT=heuristic" not in text and "SHERPA_EXTRA_AGENTS=heuristic" not in text
    # 動く構成（OpenAI／Ollama）を案内する。Codex CLI を同梱し OPENAI_API_KEY があれば導入時に認証まで自動で行う
    assert "OPENAI_API_KEY" in path.read_text(encoding="utf-8") and "OLLAMA_URL" in path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY 等は空のままにする" not in text


def test_env_example_openai_key_is_empty_and_agent_is_not_pinned():
    lines = _active(ENV_EXAMPLE)
    # 鍵の行は空の有効行 1 本だけ（プレースホルダ混入を防ぐ）
    assert [ln for ln in lines if ln.startswith("OPENAI_API_KEY=")] == ["OPENAI_API_KEY="]
    # SHERPA_AGENT を有効行にすると自動選択（agent_constructs._auto_default_agent）が一度も働かない
    assert not [ln for ln in lines if ln.startswith("SHERPA_AGENT=")]
