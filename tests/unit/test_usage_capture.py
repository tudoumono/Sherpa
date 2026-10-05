"""F3（2026-07-07-フィードバック一括.md）: 各 Provider の usage capture の単体テスト。

- agentic ループ（openai_style）が全ツールターンの usage を合算し `final` に載せる。
- Codex `turn.completed` イベントの usage 抽出（純粋ヘルパ）。`run()` での回答への載り方は `test_codex_turn_run.py`。
- 停止/ask_user では usage 無し（best-effort）。

すべて LLM 応答はモック（外部 API を実呼び出ししない）。
"""
from __future__ import annotations

import inspect
import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
from sherpa.providers.codex import usage as CUSAGE  # noqa: E402
from sherpa import agentic_search as AS  # noqa: E402


# ---- agentic ループの usage 合算（final に載る） ----

# ---- Codex turn.completed（純粋ヘルパ） ----

def test_usage_from_turn_completed_parses_real_shape():
    ev = {"type": "turn.completed", "usage": {"input_tokens": 341026, "cached_input_tokens": 244864,
                                              "output_tokens": 12318, "reasoning_output_tokens": 9392}}
    assert CUSAGE._usage_from_turn_completed(ev, "gpt-5.5", system_settings={}) == {
        "provider": "codex", "model": "gpt-5.5", "input_tokens": 341026,
        "cached_input_tokens": 244864, "output_tokens": 12318, "reasoning_output_tokens": 9392,
        "is_local": "cloud"}


def test_usage_from_turn_completed_codex_model_provider_drives_is_local():
    """Codex は常に provider_id="codex" を名乗るため、実際の接続先は呼び出し元が明示した
    `codex_model_provider`（"ollama"/"openai"）でしか分からない（`agent_constructs.is_local`
    へそのまま渡す・省略時は "openai" 相当＝`llm.openai_endpoint_kind`/接続先ホスト次第で
    cloud/on_prem/cloud_compat）。"""
    ev = {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0,
                                              "output_tokens": 2, "reasoning_output_tokens": 0}}
    assert CUSAGE._usage_from_turn_completed(ev, "qwen2.5", codex_model_provider="ollama")["is_local"] == "local"
    assert CUSAGE._usage_from_turn_completed(
        ev, "gpt-5.5", codex_model_provider="openai", system_settings={})["is_local"] == "cloud"
    assert CUSAGE._usage_from_turn_completed(
        ev, "gpt-5.5", codex_model_provider="openai",
        system_settings={"openai_endpoint_kind": "custom",
                         "openai_base_url": "http://10.0.0.5:8000/v1"})["is_local"] == "on_prem"
    assert CUSAGE._usage_from_turn_completed(
        ev, "gpt-5.5", codex_model_provider="openai",
        system_settings={"openai_endpoint_kind": "custom",
                         "openai_base_url": "https://api.example.com/v1"})["is_local"] == "cloud_compat"


def test_usage_from_turn_completed_none_for_other_events():
    assert CUSAGE._usage_from_turn_completed({"type": "item.completed"}, "gpt-5.5") is None
    assert CUSAGE._usage_from_turn_completed({"type": "turn.completed"}, "gpt-5.5") is None   # usage 無し
    assert CUSAGE._usage_from_turn_completed({"type": "turn.completed", "usage": "x"}, "gpt-5.5") is None

