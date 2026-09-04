"""`sherpa/model_windows.py`（BUDGET-2・
`docs/proposals/2026-09-02-RAG表現の全形式展開と文脈保持.md` §3.4・2026-09-03 裁定）の単体テスト。

- `derive_window_bytes`: 決定的な純関数（予約枠・安全係数・バイト換算率・下限）。
- `resolve_window_tokens`: 4段解決の優先順（登録値 > プロバイダAPI > シード表 > 不明）。
- `query_ollama_context_length`/`query_anthropic_context_length`: 失敗時 None（fail-safe）・
  TTL キャッシュ。実ネットワーク I/O はしない（`llm.urlopen_no_redirect` を monkeypatch）。
- `validate_model_windows`: 管理画面登録値の検証（形式・範囲）。
- `derive_ollama_base_url`: chat URL からの base 復元（決定的な文字列操作のみ）。

agentic_search.py 側の配線（`resolve_tool_result_budgets` の min() 適用・呼び出し元ごとの
provider/model 配線）は `tests/unit/test_agentic_search.py` の BUDGET-2 セクションが固定する。
"""
from __future__ import annotations

import json

import pytest

from sherpa import model_windows as MW
# `tests/unit/conftest.py::_hermetic_model_window_queries`（autouse）は既定でこの2関数を
# monkeypatch して常に None を返すようにする（多数の既存テストが実在しうる Ollama の既定 URL を
# 使っているため・conftest 参照）。段2そのもの（HTTP 応答の解釈・キャッシュ・fail-safe）を実際に
# 検証するテストは、モジュール import 時点（=autouse フィクスチャが動く前）に捕まえたこの実体
# 参照を明示的に貼り戻す（`monkeypatch.setattr(MW, "query_ollama_context_length",
# _real_query_ollama_context_length)`）——conftest の docstring が案内する「本体内の明示的
# monkeypatch が最後に効く」パターン。
from sherpa.model_windows import query_ollama_context_length as _real_query_ollama_context_length
from sherpa.model_windows import query_anthropic_context_length as _real_query_anthropic_context_length


@pytest.fixture(autouse=True)
def _reset_api_cache():
    """`model_windows._api_cache`（プロバイダAPI照会結果の TTL キャッシュ・モジュール全体で1つ）を
    各テスト開始前に空にする——このファイルの複数テストが同じ (base_url, model) キー（例:
    "http://localhost:11434"+"qwen2.5"）を使い回すため、前のテストが書いたキャッシュ値を次の
    テストへ持ち越さない（`tests/unit/conftest.py::_reset_tools_availability_cache` と同じ理由・
    同じ流儀）。"""
    MW._api_cache.clear()


# ===== derive_window_bytes（決定的な純関数） =====

def test_derive_window_bytes_deterministic():
    assert MW.derive_window_bytes(128_000) == MW.derive_window_bytes(128_000)


def test_derive_window_bytes_formula():
    # available = max(0, 128000 - 32000) = 96000 ; bytes = 96000 * 0.5 * 2 = 96000
    assert MW.derive_window_bytes(128_000) == 96_000


def test_derive_window_bytes_below_reserve_clamps_to_floor():
    """予約枠（32000 tokens）を下回る窓は available=0 になり、下限（1024 バイト）でクランプする
    （0 バイトにはならない＝極小窓でも検索そのものが機能不能にならない）。"""
    assert MW.derive_window_bytes(1000) == 1024
    assert MW.derive_window_bytes(0) == 1024
    assert MW.derive_window_bytes(-5) == 1024


def test_derive_window_bytes_monotonic_in_window_tokens():
    assert MW.derive_window_bytes(200_000) > MW.derive_window_bytes(128_000)


# ===== derive_ollama_base_url（決定的な文字列操作） =====

def test_derive_ollama_base_url_strips_known_suffix():
    assert MW.derive_ollama_base_url("http://localhost:11434/api/chat") == "http://localhost:11434"


def test_derive_ollama_base_url_mismatched_suffix_returns_none():
    assert MW.derive_ollama_base_url("http://x") is None
    assert MW.derive_ollama_base_url("http://x/api/show") is None
    assert MW.derive_ollama_base_url(123) is None


# ===== registered_window_tokens / validate_model_windows =====

def test_registered_window_tokens_hit():
    sysset = {MW.MODEL_WINDOWS_KEY: {"openai:my-model": 50_000}}
    assert MW.registered_window_tokens("openai", "my-model", sysset) == 50_000


def test_registered_window_tokens_miss_returns_none():
    assert MW.registered_window_tokens("openai", "unknown-model", {}) is None
    assert MW.registered_window_tokens("openai", "my-model", {MW.MODEL_WINDOWS_KEY: "not-a-dict"}) is None


def test_registered_window_tokens_rejects_bad_values():
    """負値・0・bool・非整数は「未登録」扱い（fail-safe・保存経路は `validate_model_windows` が
    弾くが、DB 直接編集等の破損値でも落ちない）。"""
    for bad in (0, -1, True, "50000"):
        sysset = {MW.MODEL_WINDOWS_KEY: {"openai:m": bad}}
        assert MW.registered_window_tokens("openai", "m", sysset) is None, bad


def test_validate_model_windows_none_clears():
    assert MW.validate_model_windows(None) is None


def test_validate_model_windows_valid_roundtrip():
    out = MW.validate_model_windows({"openai:gpt-4o": 128_000, "ollama:qwen2.5": 32_768})
    assert out == {"openai:gpt-4o": 128_000, "ollama:qwen2.5": 32_768}


@pytest.mark.parametrize("bad", [
    "not-a-dict",
    {"": 100},                       # 空キー
    {"no-colon": 100},               # provider:model 形式でない
    {"unknownprovider:m": 100},      # 未知プロバイダ
    {"openai:": 100},                # モデル名が空
    {"openai:m": 0},                 # 0 は不可
    {"openai:m": -1},                # 負値
    {"openai:m": True},              # bool
    {"openai:m": "128000"},          # 文字列
    {"openai:m": 20_000_000},        # 上限超過
])
def test_validate_model_windows_rejects_bad_shapes(bad):
    with pytest.raises(ValueError):
        MW.validate_model_windows(bad)


# ===== プロバイダAPI照会（ライブ I/O はしない・`llm.urlopen_no_redirect` を monkeypatch） =====

class _FakeShowResp:
    """`llm.urlopen_no_redirect` の戻り（context manager＋`.read()`）を模す
    （`tests/unit/test_usage_capture.py::_FakeResp` と同じ流儀・こちらは非ストリーミング）。"""
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_query_ollama_context_length_top_level_field(monkeypatch):
    from sherpa import llm
    monkeypatch.setattr(MW, "query_ollama_context_length", _real_query_ollama_context_length)
    body = json.dumps({"context_length": 32768}).encode("utf-8")
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda req, timeout=None: _FakeShowResp(body))
    assert MW.query_ollama_context_length("http://localhost:11434", "qwen2.5") == 32768


def test_query_ollama_context_length_model_info_family_key(monkeypatch):
    """実際の Ollama `/api/show` 応答形（`model_info` 内の `<family>.context_length`）。"""
    from sherpa import llm
    monkeypatch.setattr(MW, "query_ollama_context_length", _real_query_ollama_context_length)
    body = json.dumps({"model_info": {"qwen2.context_length": 32768,
                                       "qwen2.embedding_length": 3584}}).encode("utf-8")
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda req, timeout=None: _FakeShowResp(body))
    assert MW.query_ollama_context_length("http://localhost:11434", "qwen2.5") == 32768


def test_query_ollama_context_length_malformed_response_returns_none(monkeypatch):
    from sherpa import llm
    monkeypatch.setattr(MW, "query_ollama_context_length", _real_query_ollama_context_length)
    monkeypatch.setattr(llm, "urlopen_no_redirect",
                        lambda req, timeout=None: _FakeShowResp(b"not json"))
    assert MW.query_ollama_context_length("http://localhost:11434", "qwen2.5") is None


def test_query_ollama_context_length_connection_failure_returns_none(monkeypatch):
    """実ネットワーク到達不可（例外）は None（fail-safe・次段へ）——例外を外へ漏らさない。"""
    from sherpa import llm
    monkeypatch.setattr(MW, "query_ollama_context_length", _real_query_ollama_context_length)

    def _boom(req, timeout=None):
        raise OSError("connection refused")
    monkeypatch.setattr(llm, "urlopen_no_redirect", _boom)
    assert MW.query_ollama_context_length("http://localhost:11434", "qwen2.5") is None


def test_query_ollama_context_length_is_ttl_cached(monkeypatch):
    from sherpa import llm
    monkeypatch.setattr(MW, "query_ollama_context_length", _real_query_ollama_context_length)
    calls = []

    def _fake(req, timeout=None):
        calls.append(1)
        return _FakeShowResp(json.dumps({"context_length": 1234}).encode())
    monkeypatch.setattr(llm, "urlopen_no_redirect", _fake)
    # 同じ (base_url, model) は2回目以降キャッシュを使い、実際の呼び出しは1回だけ
    # （host は allowlist 通過が必要なため loopback を使う・`_reset_api_cache` で他テストとは
    # 独立）。
    assert MW.query_ollama_context_length("http://localhost:11434", "cached-model") == 1234
    assert MW.query_ollama_context_length("http://localhost:11434", "cached-model") == 1234
    assert len(calls) == 1


def test_query_anthropic_context_length_no_models_attribute_returns_none(monkeypatch):
    """本アプリの唯一の Anthropic 接続（`AnthropicBedrock`）は `.models` を持たない——
    ネットワーク I/O を発生させず即 None（`sherpa/model_windows.py` docstring 参照）。"""
    monkeypatch.setattr(MW, "query_anthropic_context_length", _real_query_anthropic_context_length)

    class _NoModelsClient:
        pass
    assert MW.query_anthropic_context_length(_NoModelsClient(), "some-model") is None


def test_query_anthropic_context_length_uses_max_input_tokens(monkeypatch):
    monkeypatch.setattr(MW, "query_anthropic_context_length", _real_query_anthropic_context_length)

    class _ModelInfo:
        max_input_tokens = 200_000

    class _Models:
        def retrieve(self, model):
            assert model == "claude-x"
            return _ModelInfo()

    class _Client:
        models = _Models()
    assert MW.query_anthropic_context_length(_Client(), "claude-x") == 200_000


def test_query_anthropic_context_length_retrieve_raises_returns_none(monkeypatch):
    monkeypatch.setattr(MW, "query_anthropic_context_length", _real_query_anthropic_context_length)

    class _Models:
        def retrieve(self, model):
            raise RuntimeError("boom")

    class _Client:
        models = _Models()
    assert MW.query_anthropic_context_length(_Client(), "claude-x") is None


# ===== resolve_window_tokens（4段解決の優先順） =====

def test_resolve_window_tokens_empty_provider_or_model_is_unknown():
    assert MW.resolve_window_tokens("", "m", system_settings={}) == (None, "unknown")
    assert MW.resolve_window_tokens("openai", "", system_settings={}) == (None, "unknown")


def test_resolve_window_tokens_registered_wins_over_seed():
    sysset = {MW.MODEL_WINDOWS_KEY: {"openai:gpt-4o-mini": 9_999}}
    assert MW.resolve_window_tokens("openai", "gpt-4o-mini", system_settings=sysset) == (9_999, "registered")


def test_resolve_window_tokens_seed_when_no_registered(monkeypatch):
    monkeypatch.setattr(MW, "query_ollama_context_length", lambda *a, **kw: None)
    assert MW.resolve_window_tokens("openai", "gpt-4o-mini", system_settings={}) == (128_000, "seed")


def test_resolve_window_tokens_unknown_when_nothing_matches():
    assert MW.resolve_window_tokens("openai", "totally-unknown-model", system_settings={}) == (
        None, "unknown")


def test_resolve_window_tokens_ollama_api_used_when_base_url_given(monkeypatch):
    monkeypatch.setattr(MW, "query_ollama_context_length", lambda base, model, **kw: 4096)
    assert MW.resolve_window_tokens("ollama", "qwen2.5", system_settings={},
                                    ollama_base_url="http://x:11434") == (4096, "api")


def test_resolve_window_tokens_ollama_api_skipped_without_base_url(monkeypatch):
    """`ollama_base_url` を渡さなければ段2は呼ばれない（ライブ照会が呼び出し側の任意）。"""
    called = []
    monkeypatch.setattr(MW, "query_ollama_context_length", lambda *a, **kw: called.append(1))
    assert MW.resolve_window_tokens("ollama", "qwen2.5", system_settings={}) == (None, "unknown")
    assert called == []


def test_resolve_window_tokens_ollama_api_failure_falls_through_to_unknown(monkeypatch):
    monkeypatch.setattr(MW, "query_ollama_context_length", lambda *a, **kw: None)
    assert MW.resolve_window_tokens("ollama", "qwen2.5", system_settings={},
                                    ollama_base_url="http://x:11434") == (None, "unknown")


def test_resolve_window_tokens_anthropic_api_used_when_client_given(monkeypatch):
    monkeypatch.setattr(MW, "query_anthropic_context_length", _real_query_anthropic_context_length)

    class _Models:
        def retrieve(self, model):
            class _Info:
                max_input_tokens = 200_000
            return _Info()

    class _Client:
        models = _Models()
    assert MW.resolve_window_tokens("bedrock", "claude-x", system_settings={},
                                    anthropic_client=_Client()) == (200_000, "api")


def test_resolve_window_tokens_seed_only_covers_openai():
    """シード表（段3）は provider="openai" のみ対象——他プロバイダは同名モデルでも不明のまま。"""
    assert MW.seed_window_tokens("gemini", "gpt-4o-mini") is None
    assert MW.seed_window_tokens("ollama", "gpt-4o-mini") is None
    assert MW.seed_window_tokens("openai", "gpt-4o-mini") == 128_000
