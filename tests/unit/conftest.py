"""tests/unit 共通 fixture（外部サービス不要・DB 到達の遮断・2026-07-08 RV Low 対応）。

`make test-unit` の契約は「外部サービス不要」だが、Postgres へ実際に到達できる開発環境で pytest を
走らせると、`sherpa.store.get_system_settings()`（S1・system_settings 全体設定）が実 DB へ問い合わせに
行き、そのテーブルの実データ（管理者が実際に保存した値・他テストの残骸）がテスト結果に混入しうる。
既定でこの呼び出しを hermetic 化し、unit テストを DB 状態から完全に独立させる。
"""
from __future__ import annotations

import pytest

from _ai_env_isolation import AI_ENV_VARS, CODEX_HOME_SENTINEL


@pytest.fixture(autouse=True)
def _isolate_ai_env(monkeypatch):
    """AI 系 env（`OPENAI_BASE_URL`・`SHERPA_AGENT`・`CODEX_HOME` 等）を各テスト開始前に隔離する
    （一覧・`CODEX_HOME` の扱いは `tests/_ai_env_isolation.py` 参照）。特定の値を検証するテストは
    本体で `monkeypatch.setenv(...)` すれば上書きできる（autouse は本体実行より先に適用されるため）。
    """
    for name in AI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_HOME", CODEX_HOME_SENTINEL)


@pytest.fixture(autouse=True)
def _hermetic_system_settings(monkeypatch):
    """既定で `sherpa.store.get_system_settings` を固定 dict に据える（未設定＝env/既定へ・DB 非到達）。

    system_settings の優先順（system_settings > env > 既定）そのものを検証するテスト
    （`tests/unit/test_arms.py` 等）は、各テスト本体で `monkeypatch.setattr(store, "get_system_settings",
    ...)` を明示的に呼べばこの既定を上書きできる（pytest の fixture 解決順で conftest.py の autouse は
    テスト本体の実行より先に適用されるため、本体内の明示的 monkeypatch が最後に効く＝確実に上書きされる）。

    W2'（2026-07-08）: office_com の direct モードは実 `powershell.exe`（/mnt/c/... の WSL interop）を検出すると
    one-shot を起動しうる。開発機（実 Windows ホスト付き WSL）で unit を走らせても実プロセスを起こさないよう、
    既定で `SHERPA_POWERSHELL_BIN` を存在しないパスに固定して direct 検出を無効化する（＝office_com は
    URL 未設定なら unavailable）。direct を検証するテストは本体で偽 powershell を `monkeypatch.setenv` し上書きする。

    `personal_api_keys_allowed`（A6）は既定 `false`（production の既定と一致させる）。個人キーの
    解決を検証するテストは、本体で `monkeypatch.setattr(store, "get_system_settings", lambda: {
    "personal_api_keys_allowed": True, ...})` のように明示 opt-in する（`cloud_provider` も既定
    "openai" のまま・gemini/bedrock を検証するテストは同様に明示する）。

    WEB-1: `providers.get_provider()`/`provider_info()` の1ターン唯一の読取点は共有キャッシュを
    介さない `store._read_system_settings_fresh()` を直接呼ぶため、`get_system_settings` だけを
    固定してもこの読取点は塞がれず、未追随のテスト（`get_provider`/`provider_info` を呼ぶが
    system_settings を明示 mock しないテスト）が実 DB へ静かに到達してしまう。同じ流儀で
    `_read_system_settings_fresh` も固定する——fresh read の実体（DB 断・並行ターン競合）を
    検証するテストは、本体で `store_settings._ensure`/`_connect` 等を直接差し替えて実関数を
    通す（`tests/unit/test_store_settings_get_system_settings_timeout.py` 参照）。
    """
    from sherpa import store
    # `**kw` で受ける（`connect_timeout`/`statement_timeout_ms` を渡す呼び出し元・
    # `sherpa/research_service.py` 等・があるため）——0引数のままだと TypeError になる。
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {})
    monkeypatch.setattr(store, "_read_system_settings_fresh", lambda **kw: {})
    monkeypatch.setenv("SHERPA_POWERSHELL_BIN", "/nonexistent/sherpa-no-powershell")


@pytest.fixture(autouse=True)
def _hermetic_model_window_queries(monkeypatch):
    """BUDGET-2（§3.4）: `sherpa.model_windows` のプロバイダAPI照会（段2・Ollama `/api/show`・
    Anthropic Models API）を既定で無効化する（常に None＝「不明」段へ fail-safe）。

    `agentic_search.resolve_tool_result_budgets` は（`openai_style`/`anthropic_style`/`gemini` の
    通常呼び出し経路として）run 開始時に毎回この照会を試みる。多数の既存テストが
    `OllamaProvider`/`_SUB` 等で実在しうる Ollama の既定 URL（`http://localhost:11434`）をそのまま
    使っているため（`model_windows.query_ollama_context_length` 自身は失敗を握りつぶす fail-safe
    設計だが）、無対策だと「開発機で実際に Ollama が動いていればテスト実行中に本物の通信が発生する」
    （unit テストの契約「外部サービス不要」に反する・`_hermetic_system_settings` と同じ理由）。

    段2そのもの（照会結果の解釈・キャッシュ・fail-safe）を検証するテストは、本体で
    `monkeypatch.setattr(model_windows, "query_ollama_context_length", ...)`（または
    `query_anthropic_context_length`）を明示的に上書きする（autouse は本体実行より先に適用される
    ため、本体内の明示的 monkeypatch が最後に効く＝確実に上書きされる・上の `_hermetic_system_
    settings` と同じ流儀）。`query_anthropic_context_length` は `AnthropicBedrock` クライアントが
    `.models` を持たないため元々ネットワーク I/O を発生させない（`sherpa/model_windows.py`
    docstring 参照）が、対称性のためここでも固定する。
    """
    from sherpa import model_windows
    monkeypatch.setattr(model_windows, "query_ollama_context_length", lambda *a, **kw: None)
    monkeypatch.setattr(model_windows, "query_anthropic_context_length", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _hermetic_metering_record(monkeypatch):
    """TOGGLE-RM（2026-09-03）: `sherpa.metering.record()` は常時 DB に書く（旧 `usage_metering`
    トグルの ON/OFF ゲートを撤去済み）。従来、unit テストが実 DB へ書き込まない防御は
    `_hermetic_system_settings`（`store.get_system_settings` を `{}` に固定→`metering.enabled()`
    が False を返す）が担っていたが、その関数自体を撤去したため防御が消える——本 fixture で
    `metering.record` 自体を既定 no-op に差し替え、代わりに防御する。

    `metering.record` 自身の挙動（クランプ・suppress()・store.add_usage_event への委譲）を検証する
    テスト（`tests/unit/test_metering.py`/`test_metering_sites.py` 等）は、本体で
    `monkeypatch.setattr(sherpa.store.usage_events, "add_usage_event", ...)` を明示的に上書きすれば
    `metering.record` の実体（本 fixture が差し替えていない）がそのまま動く（`_hermetic_model_window_
    queries` と同じ「autouse は本体実行より先＝本体内の明示的 monkeypatch が最後に効く」流儀）。
    """
    from sherpa import metering
    monkeypatch.setattr(metering, "record", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _reset_tools_availability_cache():
    """SC-6e: `agentic_search.tool_availability()` の process-local TTL キャッシュを各テスト開始前に
    リセットする——多数のテストが `es_index.available`/`agentic_search._graph_available` を
    monkeypatch して異なる可用性を検証するため、前のテストが書いたキャッシュ値を次のテストへ
    持ち越さない（`sherpa/health.py::snapshot` の `_cache` と同じ流儀・`tests/unit/test_health.py`
    参照）。キャッシュの TTL/force 挙動そのものを検証するテストは本体で直接
    `agentic_search._tools_availability_cache` を読み書きしてよい（本 fixture は開始前の1回だけ）。
    """
    from sherpa import agentic_search
    agentic_search._tools_availability_cache = {"at": 0.0, "data": None}
