"""`llm.select_provider` は個人設定の機能別プロバイダ（`graph_provider`/`intent_provider`/
`embed_provider`/`extract_provider`）を読まない＝常に auto 解決（選択中のクラウドプロバイダ・A7）へ
進む。本ファイルのテストはクラウドを一度も選んでいない構成（`cloud_provider` 未設定）で検証する
ため、鍵が無ければ Ollama（既定 URL）まで落ちる（FBK-1・クラウド明示選択時の fail-loud は
`tests/unit/test_llm_select_provider.py` 参照）。`llm.select_provider` 単体と、その消費側
（`graph_extract.available`／`intent_llm._cfg`／`embeddings.cfg`）の両方でこれを確認する。
"""
from __future__ import annotations

from sherpa import embeddings, intent_llm, llm
from sherpa.ingest import graph_extract


def _factories(calls):
    def openai(key):
        calls.append(("openai", key))
        return {"provider": "openai", "key": key}

    def gemini(key):
        calls.append(("gemini", key))
        return {"provider": "gemini", "key": key}

    def ollama(url):
        calls.append(("ollama", url))
        return {"provider": "ollama", "url": url}

    return openai, gemini, ollama


def _no_env(monkeypatch):
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL"):
        monkeypatch.delenv(k, raising=False)
    # 本ファイルは provider_key 解決ロジックの検証が目的のため、settings に直接書く
    # テスト用キーが解決されるよう個人キーの利用を許可しておく（A6 既定 false・その挙動自体は
    # tests/unit/test_keys.py が個別に検証する）。
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})


def _select_gemini(monkeypatch):
    """A7（クラウドプロバイダ排他選択）: gemini の明示選択を検証するテストは、gemini が
    選択中のクラウドプロバイダであることを明示する必要がある（既定は openai・`sherpa.keys`）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"personal_api_keys_allowed": True, "cloud_provider": "gemini"})


def test_select_provider_without_key_is_unchanged(monkeypatch):
    """`provider_key` 未指定の既存呼び出し（intent_llm/embeddings の旧経路相当）は byte-identical。"""
    _no_env(monkeypatch)
    _select_gemini(monkeypatch)
    calls = []
    openai, gemini, ollama = _factories(calls)
    cfg = llm.select_provider({"extract_provider": "gemini", "gemini_api_key": "k"},
                              openai=openai, gemini=gemini, ollama=ollama)
    assert cfg == {"provider": "gemini", "key": "k"} and calls == [("gemini", "k")]


def test_select_provider_ignores_individual_overrides_and_uses_auto(monkeypatch):
    """個人設定に `graph_provider`/`extract_provider` が残っていても読まれない＝常に auto 解決
    （選択中のクラウドプロバイダ→ollama）へ進む。ここでは gemini を選択中にし、settings 側の
    `extract_provider`/`graph_provider`（どちらも openai/ollama を指す）が無視されることを示す。"""
    _no_env(monkeypatch)
    _select_gemini(monkeypatch)
    calls = []
    openai, gemini, ollama = _factories(calls)
    settings = {"extract_provider": "ollama", "graph_provider": "openai",
                "gemini_api_key": "k", "openai_api_key": "o"}
    cfg = llm.select_provider(settings, openai=openai, gemini=gemini, ollama=ollama)
    assert cfg == {"provider": "gemini", "key": "k"} and calls == [("gemini", "k")]


def test_graph_extract_available_ignores_graph_provider_uses_auto(monkeypatch):
    """`graph_extract.available` は `graph_provider` を読まない＝settings に ollama を指す値が
    残っていても、実際の選択は auto 解決（このテストでは openai の鍵のみ有効）に従う。"""
    _no_env(monkeypatch)
    settings = {"extract_provider": "ollama", "graph_provider": "ollama",
                "openai_api_key": "o", "ollama_url": "http://localhost:11434"}
    cfg = graph_extract.available(settings)
    assert cfg["provider"] == "openai"


def test_graph_extract_available_usage_render_falls_back_to_extract_model(monkeypatch):
    """`usage="render"`（`sherpa/ingest/llm_render.py` 用・L5 残課題の是正）は model_catalog の
    `render` セルが未設定なら、`extract` の解決結果と同じモデルを使う
    （`model_catalog._USAGE_FALLBACK`）。管理者が `render` を明示設定すればそちらに従う。"""
    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True,
        "model_catalog": {"openai": {"extract": {"allowed": ["custom-extract"], "default": "custom-extract"}}},
    })
    settings = {"openai_api_key": "o"}
    cfg_extract = graph_extract.available(settings, usage="extract")
    cfg_render = graph_extract.available(settings, usage="render")
    assert cfg_extract["model"] == "custom-extract"
    assert cfg_render["model"] == "custom-extract"

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True,
        "model_catalog": {"openai": {
            "extract": {"allowed": ["custom-extract"], "default": "custom-extract"},
            "render": {"allowed": ["custom-render"], "default": "custom-render"},
        }},
    })
    assert graph_extract.available(settings, usage="render")["model"] == "custom-render"


def test_intent_cfg_ignores_intent_provider_uses_auto(monkeypatch):
    """`intent_llm._cfg` は `intent_provider` を読まない＝auto 解決に従う。"""
    _no_env(monkeypatch)
    settings = {"extract_provider": "ollama", "intent_provider": "ollama",
                "ollama_url": "http://localhost:11434", "openai_api_key": "o"}
    cfg = intent_llm._cfg(settings)
    assert cfg["provider"] == "openai"


def test_embeddings_cfg_ignores_embed_provider_uses_auto(monkeypatch):
    """`embeddings.cfg` は `embed_provider` を読まない＝auto 解決（このテストでは ollama のみ
    到達可能）に従う。"""
    _no_env(monkeypatch)
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    settings = {"extract_provider": "openai", "embed_provider": "openai",
                "ollama_url": "http://localhost:11434"}
    cfg = embeddings.cfg(settings)
    assert cfg == {"provider": "ollama", "url": "http://localhost:11434",
                   "model": "nomic-embed-text", "dim": 768}


def test_embeddings_cfg_openai_default_model_unchanged(monkeypatch):
    """MED-3（2026-08-18 Codex RV）: `OPENAI_EMBED_MODEL` 未設定なら従来どおり固定モデル名のまま
    （回帰ゼロ）。次元（1536）も変わらない。"""
    _no_env(monkeypatch)
    monkeypatch.delenv("OPENAI_EMBED_MODEL", raising=False)
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    cfg = embeddings.cfg({"extract_provider": "openai", "openai_api_key": "o"})
    # `system_settings`（`_embed_batch` の送信時接続先解決へ引き継ぐスナップショット）を除いた
    # 本体は従来どおり。
    assert {k: v for k, v in cfg.items() if k != "system_settings"} == \
        {"provider": "openai", "key": "o", "model": "text-embedding-3-small", "dim": 1536}


def test_embeddings_cfg_openai_embed_model_catalog_override(monkeypatch):
    """埋め込み用デプロイ名は `model_catalog`（openai/embed・管理者が管理画面で編集）から解決する
    （次元は 1536 のまま＝Azure 側も同じ次元のデプロイである前提）。env は初回シード専用
    （`model_catalog._seed_candidate` 参照）で以後は読まない＝ここでは system_settings.model_catalog
    を直接与えて検証する。"""
    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True,
        "model_catalog": {"openai": {"embed": {"allowed": ["my-embed-deployment"],
                                                "default": "my-embed-deployment"}}}})
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    cfg = embeddings.cfg({"extract_provider": "openai", "openai_api_key": "o"})
    assert {k: v for k, v in cfg.items() if k != "system_settings"} == \
        {"provider": "openai", "key": "o", "model": "my-embed-deployment", "dim": 1536}


def test_embeddings_cfg_openai_embed_model_defaults_without_catalog(monkeypatch):
    """`model_catalog` 未設定（`system_settings` に無い）なら組み込み既定にフォールバックする。"""
    _no_env(monkeypatch)
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    cfg = embeddings.cfg({"extract_provider": "openai", "openai_api_key": "o"})
    assert cfg["model"] == "text-embedding-3-small"


def test_embeddings_cfg_gemini_embed_model_catalog_override(monkeypatch):
    """`embeddings.cfg` 経由（`model_catalog.resolve_model` を直接呼ぶのではなく実際の消費者
    パスを通す）で gemini/embed のカタログ上書きが効くことを固定する。組み込み既定のまま比較する
    弱いテスト（`tests/unit/test_model_catalog.py`）だけだと、`embeddings.py::cfg` の配線自体が
    外れても検知できない（RV 是正・カスタム値を使い、旧ハードコード値へ戻す回帰も検出できる形にする）。"""
    _no_env(monkeypatch)
    _select_gemini(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True, "cloud_provider": "gemini",
        "model_catalog": {"gemini": {"embed": {"allowed": ["my-gemini-embed"],
                                                "default": "my-gemini-embed"}}}})
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    cfg = embeddings.cfg({"extract_provider": "gemini", "gemini_api_key": "g"})
    assert cfg == {"provider": "gemini", "key": "g", "model": "my-gemini-embed", "dim": 1536}


def test_embeddings_cfg_ollama_embed_model_catalog_override(monkeypatch):
    """`embeddings.cfg` 経由で ollama/embed のカタログ上書きが効くことを固定する（上記 gemini 版と対）。"""
    _no_env(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True,
        "model_catalog": {"ollama": {"embed": {"allowed": ["my-ollama-embed"],
                                               "default": "my-ollama-embed"}}}})
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    cfg = embeddings.cfg({"extract_provider": "ollama", "ollama_url": "http://localhost:11434"})
    assert cfg == {"provider": "ollama", "url": "http://localhost:11434",
                   "model": "my-ollama-embed", "dim": 768}
