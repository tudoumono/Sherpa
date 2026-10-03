"""モデルカタログ（`sherpa/model_catalog.py`）の単体テスト。

`sherpa/model_catalog.py` の純粋関数（DB を伴わない部分）を検証する。実 DB を使う
「初回シードは一度だけ」の意味論は `tests/api/test_system_settings.py`（既存の
`seed_system_settings_once` テスト群と同じ場所）に置く。
"""
from __future__ import annotations

from sherpa import model_catalog


def test_resolve_model_ignores_user_settings_uses_catalog_default(monkeypatch):
    """個人設定の個別モデル名は無い＝`user_settings` に何が入っていてもカタログ既定のみで解決する
    （一般ユーザーが任意のモデル名で解決結果を差し替えられないことの回帰）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "model_catalog": {"openai": {"chat": {"allowed": ["custom-a", "custom-b"], "default": "custom-a"}}}})
    assert model_catalog.resolve_model("openai", "chat", {"openai_model": "gpt-5.4-mini"}) == "custom-a"
    assert model_catalog.resolve_model("openai", "chat", {"openai_model": "totally-made-up"}) == "custom-a"
    assert model_catalog.resolve_model("openai", "chat", None) == "custom-a"


def test_resolve_model_falls_back_to_hardcoded_when_catalog_unset(monkeypatch):
    """system_settings に model_catalog が無ければ組み込み既定（今までの各呼び出し箇所のハードコード
    既定と同じ値）を使う＝カタログ導入前との後方互換。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert model_catalog.resolve_model("openai", "chat", None) == "gpt-5.5"
    assert model_catalog.resolve_model("ollama", "chat", None) == "qwen2.5"
    assert model_catalog.resolve_model("codex", "codex", None) == "gpt-5.5"


def test_resolve_model_uses_catalog_or_hardcoded(monkeypatch):
    """embed 等・カタログにセルが無ければ組み込み既定、あればカタログ既定を使う。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert model_catalog.resolve_model("openai", "embed", None) == "text-embedding-3-small"
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "model_catalog": {"openai": {"embed": {"allowed": ["my-deploy"], "default": "my-deploy"}}}})
    assert model_catalog.resolve_model("openai", "embed", None) == "my-deploy"


def test_db_unreachable_falls_back_to_hardcoded(monkeypatch):
    """DB 不達（get_system_settings が例外）でも解決は止まらない（fail-safe）。"""
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("sherpa.store.get_system_settings", _boom)
    assert model_catalog.resolve_model("openai", "chat", None) == "gpt-5.5"


def test_validate_catalog_none_clears():
    assert model_catalog.validate_catalog(None) is None


def test_validate_catalog_normalizes_and_dedupes():
    out = model_catalog.validate_catalog(
        {"openai": {"chat": {"allowed": ["a", "a", " b ", ""], "default": "a"}}})
    assert out == {"openai": {"chat": {"allowed": ["a", "b"], "default": "a"}}}


def test_validate_catalog_default_not_in_allowed_gets_added():
    """default が allowed に無ければ先頭へ足す（保存直後に「既定が選択肢に無い」矛盾を作らない）。"""
    out = model_catalog.validate_catalog({"openai": {"chat": {"allowed": ["a"], "default": "b"}}})
    assert out["openai"]["chat"]["allowed"][0] == "b"
    assert "a" in out["openai"]["chat"]["allowed"]


def test_validate_catalog_rejects_non_dict():
    import pytest
    with pytest.raises(ValueError):
        model_catalog.validate_catalog("not-a-dict")


def test_validate_catalog_rejects_bad_allowed_type():
    import pytest
    with pytest.raises(ValueError):
        model_catalog.validate_catalog({"openai": {"chat": {"allowed": "not-a-list", "default": ""}}})


def test_validate_catalog_rejects_non_string_allowed_entries():
    import pytest
    with pytest.raises(ValueError):
        model_catalog.validate_catalog({"openai": {"chat": {"allowed": [1, 2], "default": ""}}})


def test_validate_catalog_rejects_unknown_provider():
    """タイプミス（例: `opneai`）を黙って保存すると、UI にも実行にも効かない隠れ設定になる。"""
    import pytest
    with pytest.raises(ValueError):
        model_catalog.validate_catalog({"opneai": {"chat": {"allowed": ["x"], "default": "x"}}})


def test_validate_catalog_rejects_unknown_usage():
    import pytest
    with pytest.raises(ValueError):
        model_catalog.validate_catalog({"openai": {"bogus-usage": {"allowed": ["x"], "default": "x"}}})


def test_validate_catalog_accepts_all_known_providers():
    for provider in model_catalog.PROVIDERS:
        out = model_catalog.validate_catalog({provider: {"chat": {"allowed": ["x"], "default": "x"}}})
        assert out[provider]["chat"]["default"] == "x"


def test_hardcoded_fallback_unknown_cell_is_empty():
    assert model_catalog.hardcoded_fallback("unknown-provider", "unknown-usage") == ""


def test_seed_candidate_ignores_invalid_openai_embed_model_env(monkeypatch):
    """低リスク是正（RV 3巡目）: `OPENAI_EMBED_MODEL` env の値も、管理 API（`validate_catalog`）と
    同じモデル名文法を満たさない限り取り込まない（env だけが無効な値（空白混入等）を素通り
    できると、管理画面では拒否される値が env 経由でだけ紛れ込む食い違いになる）。"""
    monkeypatch.setenv("OPENAI_EMBED_MODEL", "bad embed model")
    catalog = model_catalog._seed_candidate()
    assert catalog["openai"]["embed"]["default"] == "text-embedding-3-small"   # 組み込み既定のまま


def test_seed_candidate_accepts_valid_openai_embed_model_env(monkeypatch):
    monkeypatch.setenv("OPENAI_EMBED_MODEL", "my-embed-deployment")
    catalog = model_catalog._seed_candidate()
    assert catalog["openai"]["embed"]["default"] == "my-embed-deployment"


def test_validate_catalog_rejects_model_name_with_internal_whitespace():
    """管理者が保存できるモデル名は、Ollama の形式検証（`_MODEL_NAME_RE`）・Codex の argv 検証
    （`CodexProvider`）と同じ文法でなければならない（保存できたのに個人設定では 422／Codex では
    honest failure になる、という食い違いを防ぐ）。"""
    import pytest
    with pytest.raises(ValueError):
        model_catalog.validate_catalog({"openai": {"chat": {"allowed": ["bad model"], "default": ""}}})


def test_validate_catalog_rejects_openai_model_name_over_128_chars():
    import pytest
    with pytest.raises(ValueError):
        model_catalog.validate_catalog({"openai": {"chat": {"allowed": ["a" * 129], "default": ""}}})


def test_validate_catalog_accepts_openai_model_name_up_to_128_chars():
    out = model_catalog.validate_catalog({"openai": {"chat": {"allowed": ["a" * 128], "default": ""}}})
    assert out["openai"]["chat"]["allowed"] == ["a" * 128]


def test_validate_catalog_accepts_codex_ollama_model_tag_with_colon():
    """Codex(Ollama) のモデルはタグ付き（例 gpt-oss:20b）が普通なので、codex 用途でも `:` を受け付ける。"""
    model_catalog.validate_catalog({"codex": {"codex": {"allowed": ["gpt-oss:20b"], "default": "gpt-oss:20b"}}})


def test_validate_catalog_rejects_codex_model_name_over_64_chars():
    import pytest
    with pytest.raises(ValueError):
        model_catalog.validate_catalog({"codex": {"codex": {"allowed": ["a" * 65], "default": ""}}})


def test_validate_catalog_accepts_codex_model_name_with_colon_rejected_but_slash_allowed():
    """codex の文法は `:` だけ不可（`/`・`.`・`_`・`-` は許可・Ollama タグ形式との違いを固定する）。"""
    out = model_catalog.validate_catalog({"codex": {"codex": {"allowed": ["custom/gpt-5.5"], "default": ""}}})
    assert out["codex"]["codex"]["allowed"] == ["custom/gpt-5.5"]


def test_codex_model_name_re_matches_catalog_grammar():
    """`CodexProvider` が実際に使う正規表現（`sherpa/providers/codex/provider.py`）が、
    `model_catalog.CODEX_MODEL_NAME_RE` と同一オブジェクトであること（二重定義していないこと）を
    固定する。片方だけ変更してもう片方を更新し忘れる drift を防ぐ。"""
    from sherpa.providers.codex import provider as codex_provider
    assert codex_provider.model_catalog.CODEX_MODEL_NAME_RE is model_catalog.CODEX_MODEL_NAME_RE


def test_codex_provider_rejects_invalid_nonempty_model_name_as_honest_failure():
    """`validate_catalog` が拒否するのと同じ形（空白を含む）を
    `CodexProvider` に直接渡すと、黙って `gpt-5.5` へ置換せず `InvalidModelNameError`（honest
    failure・`ValueError` のサブクラス）を送出する。表示したモデルと実際に実行されるモデルが
    食い違う事故を防ぐ（呼び出し側は `sherpa/providers/__init__.py::_select_provider` が
    この型だけを狭く捕捉して `_UnwiredProvider` にする＝RV 4巡目 #9）。"""
    import pytest

    from sherpa.providers.codex.provider import CodexProvider
    with pytest.raises(model_catalog.InvalidModelNameError):
        CodexProvider(model="gpt 5.5")


def test_codex_provider_resolves_none_or_empty_model_to_default():
    """未指定（None／空文字）だけが既定 `gpt-5.5` へ解決される（不正な非空値との違いを固定する）。"""
    from sherpa.providers.codex.provider import CodexProvider
    assert CodexProvider(model=None).model == "gpt-5.5"
    assert CodexProvider(model="").model == "gpt-5.5"
    assert CodexProvider(model="gpt-5.4-mini").model == "gpt-5.4-mini"


def test_resolve_model_embed_covers_ollama_too(monkeypatch):
    """OpenAI だけでなく Ollama の埋め込みもカタログへ配線されている
    （`sherpa/embeddings.py::cfg` の L() が使う）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert model_catalog.resolve_model("ollama", "embed", None) == "nomic-embed-text"
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "model_catalog": {"ollama": {"embed": {"allowed": ["custom-embed"], "default": "custom-embed"}}}})
    assert model_catalog.resolve_model("ollama", "embed", None) == "custom-embed"


# ---- render 用途（L5 残課題の是正: LLM 成形＝llm_render.py が extract セルを共用していた件）---------

def test_render_usage_is_registered_but_absent_from_default_catalog():
    """`render` は `USAGES` に登録するが、openai/codex は `route` と同型で `_DEFAULT_CATALOG`
    に持たせない（静的な既定値を置くと、admin が extract 側だけ変更した場合にフォールバックが
    追随しなくなるため・`resolve_model` が動的に extract の解決結果へフォールバックする設計）。

    ollama だけは**空セル**（`allowed=[]`／`default=""`）を持つ（#48 是正）: 管理画面の
    「使えるモデル」表は、対象セルが `_DEFAULT_CATALOG` に無い（＝`effective` に無い）と
    「一覧を編集」ボタン自体が出ず、admin が一度も触れないセルを永遠に編集できない
    （route と同じ扱いのまま放置すると Ollama 構成で render 用途を設定する手段が無くなる）。
    空セルなら `resolve_model` の動的フォールバックは変わらない（下の
    `test_resolve_model_render_falls_back_to_extract_when_unset` が固定）まま、UI からだけ編集可能になる。"""
    assert "render" in model_catalog.USAGES
    for provider, provider_cells in model_catalog._DEFAULT_CATALOG.items():
        if provider == "ollama":
            assert provider_cells["render"] == {"allowed": [], "default": ""}
        else:
            assert "render" not in provider_cells


def test_resolve_model_render_falls_back_to_extract_when_unset(monkeypatch):
    """render を一度も設定していなければ、extract の解決結果と完全に同一の値になる
    （組み込み既定・管理者上書きのどちらでも追随する）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    for provider in ("openai", "ollama"):
        assert (model_catalog.resolve_model(provider, "render", None)
                == model_catalog.resolve_model(provider, "extract", None))

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "model_catalog": {"openai": {"extract": {"allowed": ["custom-extract"], "default": "custom-extract"}}}})
    assert model_catalog.resolve_model("openai", "render", None) == "custom-extract"


def test_resolve_model_render_uses_its_own_cell_when_configured(monkeypatch):
    """管理者が render を明示的に設定すれば、extract とは独立した値を使う（分離できる）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "model_catalog": {"openai": {
            "extract": {"allowed": ["custom-extract"], "default": "custom-extract"},
            "render": {"allowed": ["custom-render"], "default": "custom-render"},
        }}})
    assert model_catalog.resolve_model("openai", "render", None) == "custom-render"
    assert model_catalog.resolve_model("openai", "extract", None) == "custom-extract"


def test_ollama_render_can_be_configured_from_the_empty_builtin_cell(monkeypatch):
    """#48: Ollama 構成でも render 用途のモデルを管理画面から設定できる（`_DEFAULT_CATALOG` の
    空セルが `validate_catalog` の保存対象になり、`resolve_model` がその値をそのまま使う）。"""
    saved = model_catalog.validate_catalog(
        {"ollama": {"render": {"allowed": ["gemma3:latest"], "default": "gemma3:latest"}}})
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"model_catalog": saved})
    assert model_catalog.resolve_model("ollama", "render", None) == "gemma3:latest"
    # extract 側は無関係のまま（render を設定しても extract の解決には影響しない）。
    assert model_catalog.resolve_model("ollama", "extract", None) == "qwen2.5"


def test_hardcoded_fallback_render_is_empty_by_design():
    """`render` は組み込み既定を持たない＝`hardcoded_fallback` 単体では空文字（フォールバック連鎖は
    `resolve_model` 側の責務）。"""
    assert model_catalog.hardcoded_fallback("openai", "render") == ""


def test_validate_catalog_accepts_legacy_extract_cell_passthrough():
    """GRAPH-SRC 是正（2026-09-05）: 既存 DB の extract セル（レガシー）を含むカタログを
    保存し直しても拒否しない（拒否すると管理画面の設定保存が全滅する・実環境で観測）。
    値は素通し＝render→extract フォールバックの読み取り元として保持される。"""
    from sherpa import model_catalog as mc
    cat = {"openai": {"extract": {"allowed": ["gpt-5.5"], "default": "gpt-5.5"}}}
    out = mc.validate_catalog(cat)
    assert out["openai"]["extract"]["default"] == "gpt-5.5"


