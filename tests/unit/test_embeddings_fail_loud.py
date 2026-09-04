"""`sherpa.embeddings` の fail-loud 境界（RV1・FBK-1・2026-09-01）単体テスト。

- `cfg()` は system_settings 読取失敗を `{}` へ縮退させず、そのまま伝播する（黙って
  「クラウド未選択」に化けて Ollama へ倒れない・`sherpa/llm.py::resolve_auto_provider` 参照）。
- `cloud_selected_but_unavailable()` は「クラウド未選択（通常の埋め込み未設定）」と
  「A7 で明示選択したクラウドが解決できない」を区別する（`es_index.py` の再索引/検索が
  この区別で BM25-only への静かな縮退を避ける）。
"""
from __future__ import annotations

import pytest

from sherpa import embeddings


def _no_env(monkeypatch):
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL", "SHERPA_DISABLE_EMBED"):
        monkeypatch.delenv(k, raising=False)


def test_cfg_propagates_system_settings_read_failure_instead_of_degrading_to_empty_dict(monkeypatch):
    """設定取得が例外を出したら `cfg()` はそのまま伝播する（`{}` へ縮退して「クラウド未選択」に
    化け、`llm.select_provider` の auto 解決が Ollama へ黙って倒れてはいけない＝実測で問題に
    なった経路の回帰固定）。`select_provider` 自体が一度も呼ばれないこと（＝縮退した
    system_settings で auto 解決が進んでいないこと）を spy で確認する。"""
    _no_env(monkeypatch)

    def _boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr("sherpa.store.get_system_settings", _boom)
    select_provider_calls = []
    monkeypatch.setattr(embeddings.llm, "select_provider",
                        lambda *a, **k: select_provider_calls.append(1))

    with pytest.raises(RuntimeError, match="db unreachable"):
        embeddings.cfg({})
    assert select_provider_calls == []


def test_cloud_selected_but_unavailable_false_when_cloud_never_selected(monkeypatch):
    # `SHERPA_DISABLE_EMBED`（他テストファイルが os.environ.setdefault で立てうる・import された
    # 時点でプロセス全体に残る kill-switch）を明示的に外す＝この判定自体を確実に検証する。
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert embeddings.cloud_selected_but_unavailable() is False


def test_cloud_selected_but_unavailable_true_when_explicit_provider_set(monkeypatch):
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)   # 上のテストと同じ理由
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"cloud_provider": "openai"})
    assert embeddings.cloud_selected_but_unavailable() is True


def test_cloud_selected_but_unavailable_false_when_disable_embed_killswitch_active(monkeypatch):
    """テスト用 kill-switch（`SHERPA_DISABLE_EMBED`）が有効な間は、クラウドが明示選択されていても
    「障害」とは扱わない（意図した運用停止）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"cloud_provider": "openai"})
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    assert embeddings.cloud_selected_but_unavailable() is False


def test_cfg_returns_none_when_cloud_selected_and_key_missing(monkeypatch):
    """FBK-1 の fail-loud 契約が embeddings 経由でも効くこと（Ollama factory を呼ばない）を
    実際の `cfg()` 呼び出しで固定する。"""
    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"cloud_provider": "openai", "personal_api_keys_allowed": True})
    assert embeddings.cfg({}) is None
    assert embeddings.cloud_selected_but_unavailable() is True
