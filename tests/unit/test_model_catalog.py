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
    assert model_catalog.resolve_model("gemini", "chat", None) == "gemini-2.5-flash"
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


def test_field_valid_accepts_catalog_allowed(monkeypatch):
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert model_catalog.field_valid("openai_model", "gpt-5.5") is True
    assert model_catalog.field_valid("openai_model", "gpt-5.4-mini") is True


def test_field_valid_rejects_uncatalogued_value(monkeypatch):
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert model_catalog.field_valid("openai_model", "totally-made-up") is False


def test_field_valid_grandfathers_current_value(monkeypatch):
    """現在保存中の値（旧・自由入力時代のもの等）は、カタログに無くても弾かない（移行期の寛容）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert model_catalog.field_valid("openai_model", "legacy-custom-model", current="legacy-custom-model") is True
    assert model_catalog.field_valid("openai_model", "legacy-custom-model", current="something-else") is False


def test_field_valid_empty_always_allowed(monkeypatch):
    """空文字（クリア＝カタログ既定に従う指示）は常に許可する。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert model_catalog.field_valid("openai_model", "") is True


def test_field_valid_unknown_field_not_constrained(monkeypatch):
    """FIELD_CELLS に無いフィールド名は対象外（カタログ制約をかけない・防御的）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert model_catalog.field_valid("not_a_real_field", "anything") is True


def test_field_valid_intent_model_falls_back_to_union_when_provider_unresolved(monkeypatch):
    """`provider` を渡さない呼び出し（intent_provider が auto/未設定で実行時までプロバイダが
    決まらない場合）は、誤って全滅させないよう openai/gemini/ollama の intent 用途を合算した
    和集合で判定する。実効プロバイダが判明する通常の保存経路では、呼び出し側
    （`sherpa/routers/system.py::_effective_provider_for_field`）が `provider=` を渡して
    単一セル判定へ絞り込む（次のテスト参照）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert model_catalog.field_valid("intent_model", "gemini-2.5-flash") is True
    assert model_catalog.field_valid("intent_model", "qwen2.5") is True
    assert model_catalog.field_valid("intent_model", "gpt-4o-mini") is True


def test_field_valid_intent_model_with_resolved_provider_restricts_to_that_cell(monkeypatch):
    """重大バグ是正: `provider` が判明している場合（例: `intent_provider=gemini`）は、他プロバイダ
    専用のモデル名を紛れ込ませない。以前は常に和集合判定だったため
    `intent_provider=gemini` なのに openai 専用モデル名（`gpt-4o-mini`）を保存できてしまっていた。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert model_catalog.field_valid("intent_model", "gpt-4o-mini", provider="gemini") is False
    assert model_catalog.field_valid("intent_model", "gemini-2.5-flash", provider="gemini") is True
    assert model_catalog.field_valid("intent_model", "qwen2.5", provider="gemini") is False


def test_field_valid_search_helper_model_with_resolved_provider_restricts_to_that_cell(monkeypatch):
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert model_catalog.field_valid("search_helper_model", "qwen2.5", provider="openai") is False
    assert model_catalog.field_valid("search_helper_model", "gpt-5.4-mini", provider="openai") is True


def test_field_valid_openai_model_requires_valid_in_every_shared_usage(monkeypatch):
    """`openai_model` は chat/extract の両方で共用される。管理者がセルごとに別々の allowed を
    設定した場合、**両方に含まれる値だけ**を許可する（積集合・どちらの用途で実際に使われても
    安全な値だけ保存できる）。片方にしか無い値は拒否する。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "model_catalog": {"openai": {
            "chat": {"allowed": ["shared-model", "chat-only-model"], "default": "shared-model"},
            "extract": {"allowed": ["shared-model", "extract-only-model"], "default": "shared-model"},
        }}})
    assert model_catalog.field_valid("openai_model", "shared-model") is True
    assert model_catalog.field_valid("openai_model", "chat-only-model") is False
    assert model_catalog.field_valid("openai_model", "extract-only-model") is False


def test_field_valid_grandfather_requires_exact_current_match_not_partial(monkeypatch):
    """grandfather は「現在 DB 保存値と完全一致」のみ。別セルにたまたま同じ値が存在するだけでは
    grandfather 扱いにしない（そもそも catalog 側にあれば grandfather を経由せず許可されるため、
    これは主に catalog に無い値が current と食い違う場合の拒否を固定する）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert model_catalog.field_valid("openai_model", "totally-made-up", current="something-else") is False


def test_field_choice_info_merges_cells_without_duplicates(monkeypatch):
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    info = model_catalog.field_choice_info("ollama_model")
    assert info["allowed"] == ["qwen2.5"]   # chat/extract/subsearch は組み込み既定で全て同じ
    assert info["default"] == "qwen2.5"


def test_field_choice_info_unknown_field_returns_empty():
    info = model_catalog.field_choice_info("not_a_real_field")
    assert info == {"allowed": [], "default": ""}


def test_field_choice_info_single_provider_field_uses_intersection_not_union(monkeypatch):
    """重大バグ是正: `openai_model` のような単一プロバイダ共用欄（chat/extract）は、保存時検証
    （`field_valid` の積集合）と一致する選択肢だけを返す（以前は常に和集合で、PUT すると 422
    になる選択肢が画面に出ていた）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "model_catalog": {"openai": {
            "chat": {"allowed": ["shared-model", "chat-only-model"], "default": "shared-model"},
            "extract": {"allowed": ["shared-model", "extract-only-model"], "default": "shared-model"},
        }}})
    info = model_catalog.field_choice_info("openai_model")
    assert info["allowed"] == ["shared-model"]   # 積集合のみ（chat-only/extract-only は含まない）


def test_field_choice_info_default_empty_when_cells_disagree(monkeypatch):
    """重大バグ是正（RV 3巡目・低）: 積集合（単一プロバイダ複数用途）で、用途ごとの既定が
    異なる場合、先頭セルの既定だけを代表表示すると実際の解決と食い違う。既定が割れている場合は
    空文字（UI は具体例を出さず「管理者の既定を使う」とだけ表示する）にする。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "model_catalog": {"openai": {
            "chat": {"allowed": ["shared-model"], "default": "shared-model"},
            "extract": {"allowed": ["shared-model", "other-default"], "default": "other-default"},
        }}})
    info = model_catalog.field_choice_info("openai_model")
    assert info["default"] == ""   # chat="shared-model" と extract="other-default" が食い違う


def test_field_choice_info_default_shown_when_cells_agree(monkeypatch):
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "model_catalog": {"openai": {
            "chat": {"allowed": ["shared-model"], "default": "shared-model"},
            "extract": {"allowed": ["shared-model"], "default": "shared-model"},
        }}})
    info = model_catalog.field_choice_info("openai_model")
    assert info["default"] == "shared-model"


def test_field_choice_info_provider_param_restricts_multi_provider_field(monkeypatch):
    """`provider` を渡すと（実効プロバイダが判明している場合）、そのプロバイダのセルだけを返す
    （`search_helper=ollama` のとき openai の選択肢・既定を混ぜない・RV 是正）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    info = model_catalog.field_choice_info("search_helper_model", provider="ollama")
    assert info == {"allowed": ["qwen2.5"], "default": "qwen2.5"}
    info_openai = model_catalog.field_choice_info("search_helper_model", provider="openai")
    assert info_openai == {"allowed": ["gpt-5.4-mini", "gpt-4o-mini"], "default": "gpt-5.4-mini"}


def test_field_choice_info_without_provider_still_unions_multi_provider_field(monkeypatch):
    """`provider` 未指定（実効プロバイダが実行時まで決まらない場合）は、誤って選択肢を全滅させない
    よう従来どおり和集合のまま。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    info = model_catalog.field_choice_info("search_helper_model")
    assert set(info["allowed"]) == {"gpt-5.4-mini", "gpt-4o-mini", "qwen2.5"}


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


def test_validate_catalog_rejects_bedrock_provider():
    """bedrock は実在確認済みモデルの専用機構と二重の真実源になるため、admin 直接 API 経由でも
    保存できない（管理画面 UI は既にこの列を編集不可にしているが、API 側でも拒否する）。"""
    import pytest
    with pytest.raises(ValueError):
        model_catalog.validate_catalog({"bedrock": {"chat": {"allowed": ["x"], "default": "x"}}})


def test_validate_catalog_rejects_unknown_provider():
    """タイプミス（例: `opneai`）を黙って保存すると、UI にも実行にも効かない隠れ設定になる。"""
    import pytest
    with pytest.raises(ValueError):
        model_catalog.validate_catalog({"opneai": {"chat": {"allowed": ["x"], "default": "x"}}})


def test_validate_catalog_rejects_unknown_usage():
    import pytest
    with pytest.raises(ValueError):
        model_catalog.validate_catalog({"openai": {"bogus-usage": {"allowed": ["x"], "default": "x"}}})


def test_validate_catalog_accepts_all_known_providers_except_bedrock():
    for provider in model_catalog.PROVIDERS:
        if provider == "bedrock":
            continue
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


def test_validate_catalog_rejects_codex_model_name_with_colon():
    """Codex は argv `-m` に渡すため、個人設定/カタログより厳しい文法（`:` 不可・64文字以内・
    `CodexProvider.__init__` の `CODEX_MODEL_NAME_RE`）を持つ。カタログ側もこれに揃え、
    Ollama の `sha256:abcd` のような他プロバイダでは有効な形式を codex 用途では拒否する。"""
    import pytest
    with pytest.raises(ValueError):
        model_catalog.validate_catalog({"codex": {"codex": {"allowed": ["gpt-5.5:latest"], "default": ""}}})


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
    """重大バグ是正（RV 3巡目 #9）: `validate_catalog` が拒否するのと同じ形（`:` を含む）を
    `CodexProvider` に直接渡すと、黙って `gpt-5.5` へ置換せず `InvalidModelNameError`（honest
    failure・`ValueError` のサブクラス）を送出する。表示したモデルと実際に実行されるモデルが
    食い違う事故を防ぐ（呼び出し側は `sherpa/providers/__init__.py::_select_provider` が
    この型だけを狭く捕捉して `_UnwiredProvider` にする＝RV 4巡目 #9）。"""
    import pytest

    from sherpa.providers.codex.provider import CodexProvider
    with pytest.raises(model_catalog.InvalidModelNameError):
        CodexProvider(model="gpt-5.5:latest")


def test_codex_provider_invalid_model_exception_does_not_shadow_unrelated_value_error(monkeypatch):
    """重大バグ是正（RV 4巡目 #9・5巡目 #12 で docstring を実態に是正）: このテストが直接
    確認するのは `CodexProvider.__init__` 自身の挙動だけ——`SHERPA_CODEX_TIMEOUT` のような
    無関係な env 値が壊れて `float()` 変換が失敗した場合、送出されるのは無印の `ValueError`
    であって `model_catalog.InvalidModelNameError` ではないこと（モデル名には有効な値を渡して
    いる）。これにより、`sherpa/providers/__init__.py::_select_provider` の
    `except model_catalog.InvalidModelNameError`（この型だけを狭く捕捉する設計）が、この種の
    無関係な `ValueError` を誤って「モデル名が不正」の `_UnwiredProvider` に化けさせない前提
    条件が成り立つ。`_select_provider` 自体を通した結合テストは
    `tests/unit/test_codex_azure_provider.py::test_select_provider_narrows_exception_catch_to_invalid_model_name_only`
    が担う。"""
    import pytest

    monkeypatch.setenv("SHERPA_CODEX_TIMEOUT", "not-a-number")
    from sherpa.providers.codex.provider import CodexProvider
    with pytest.raises(ValueError):
        CodexProvider(model="gpt-5.4-mini")
    # モデル名文法の例外ではない（`float()` の変換失敗）ことを確認する。
    try:
        CodexProvider(model="gpt-5.4-mini")
    except model_catalog.InvalidModelNameError:
        pytest.fail("モデル名は有効なのに InvalidModelNameError が送出された")
    except ValueError:
        pass


def test_codex_provider_resolves_none_or_empty_model_to_default():
    """未指定（None／空文字）だけが既定 `gpt-5.5` へ解決される（不正な非空値との違いを固定する）。"""
    from sherpa.providers.codex.provider import CodexProvider
    assert CodexProvider(model=None).model == "gpt-5.5"
    assert CodexProvider(model="").model == "gpt-5.5"
    assert CodexProvider(model="gpt-5.4-mini").model == "gpt-5.4-mini"


def test_resolve_model_embed_covers_gemini_and_ollama_too(monkeypatch):
    """重大バグ是正: OpenAI だけでなく Gemini/Ollama の埋め込みもカタログへ配線されている
    （`sherpa/embeddings.py::cfg` の G()/L() が使う）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert model_catalog.resolve_model("gemini", "embed", None) == "gemini-embedding-001"
    assert model_catalog.resolve_model("ollama", "embed", None) == "nomic-embed-text"
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "model_catalog": {"gemini": {"embed": {"allowed": ["custom-embed"], "default": "custom-embed"}}}})
    assert model_catalog.resolve_model("gemini", "embed", None) == "custom-embed"


def test_bedrock_and_route_excluded_from_default_catalog():
    """bedrock は既存の実在確認済みモデル機構（store.add_bedrock_verified_models）と二重の真実源に
    ならないよう対象外。route は消費箇所が無いため allowed 未定義（データ形だけ用意）。"""
    assert "bedrock" not in model_catalog._DEFAULT_CATALOG
    for provider_cells in model_catalog._DEFAULT_CATALOG.values():
        assert "route" not in provider_cells


# ---- render 用途（L5 残課題の是正: LLM 成形＝llm_render.py が extract セルを共用していた件）---------

def test_render_usage_is_registered_but_absent_from_default_catalog():
    """`render` は `USAGES` に登録するが、`route` と同型で `_DEFAULT_CATALOG` には持たせない
    （静的な既定値を置くと、admin が extract 側だけ変更した場合にフォールバックが追随しなくなる
    ため・`resolve_model` が動的に extract の解決結果へフォールバックする設計）。"""
    assert "render" in model_catalog.USAGES
    for provider_cells in model_catalog._DEFAULT_CATALOG.values():
        assert "render" not in provider_cells


def test_resolve_model_render_falls_back_to_extract_when_unset(monkeypatch):
    """render を一度も設定していなければ、extract の解決結果と完全に同一の値になる
    （組み込み既定・管理者上書きのどちらでも追随する）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    for provider in ("openai", "gemini", "ollama"):
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


def test_hardcoded_fallback_render_is_empty_by_design():
    """`render` は組み込み既定を持たない＝`hardcoded_fallback` 単体では空文字（フォールバック連鎖は
    `resolve_model` 側の責務）。"""
    assert model_catalog.hardcoded_fallback("openai", "render") == ""
