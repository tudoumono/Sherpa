"""実行構成（4構成）の契約（`sherpa/agent_constructs.py`・決定 2026-08-15）。

標準MVPが見せるのは OpenAI / ローカル(Ollama) / Codex(OpenAI) / Codex(Ollama) の4つだけ。
gemini/bedrock は `SHERPA_EXTRA_AGENTS` で明示的に有効化した時だけ選択肢に入り、
未指定なら設定に残っていても実行時に遮断する（黙って別の AI が答える状態を作らない）。
"""
from __future__ import annotations

import time

import pytest

from sherpa import agent_constructs as AC


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(AC.EXTRA_AGENTS_ENV, raising=False)


def test_default_shows_exactly_four_constructs():
    ids = [c["id"] for c in AC.available_constructs()]
    assert ids == ["openai_only", "ollama_only", "codex_openai", "codex_ollama"]


def test_is_real_api_key_returns_false_for_non_string_without_raising():
    """RV9 是正の固定: 非文字列（設定破損・型不正な入力等）を渡しても `AttributeError`
    （`.strip()`）を出さず、「キーなし」として fail-closed に扱う。"""
    for bad in ({"k": "v"}, ["sk-x"], 123, 1.5, object(), True, False):
        assert AC.is_real_api_key(bad) is False
    assert AC.is_real_api_key(None) is False
    assert AC.is_real_api_key("") is False
    assert AC.is_real_api_key("sk-real-key") is True


def test_extra_agents_appear_only_when_enabled(monkeypatch):
    """SHERPA_EXTRA_AGENTS ゲート単体（A7 の cloud_provider は gemini に明示的に揃えて
    無関係にする＝既定 openai のままだと gemini/bedrock は A7 側でも出ないため、この境界だけ見る）。"""
    assert [c["id"] for c in AC.available_constructs()] == [
        "openai_only", "ollama_only", "codex_openai", "codex_ollama"]
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"cloud_provider": "gemini"})
    monkeypatch.setenv(AC.EXTRA_AGENTS_ENV, "gemini, bedrock")
    ids = [c["id"] for c in AC.available_constructs()]
    # openai_only は cloud_provider=gemini のため出ない（A7）。bedrock は選択されていないため出ない。
    assert ids == ["ollama_only", "codex_openai", "codex_ollama", "gemini"]
    # 未知の名前は無視する（任意文字列で選択肢を増やせない）。
    monkeypatch.setenv(AC.EXTRA_AGENTS_ENV, "gemini,anthropic,../etc/passwd")
    assert [c["id"] for c in AC.available_constructs()][3:] == ["gemini"]


def test_heuristic_never_appears_as_a_selectable_construct(monkeypatch):
    """`heuristic`（簡易・AIなし）は `SHERPA_EXTRA_AGENTS` で有効化していても画面の選択肢には
    出さない（内部フォールバック専用・利用者が明示的に選ぶものにしない）。この除外は画面向け
    一覧だけの制約であり、`default_agent()`/`enabled_agents()` は heuristic を引き続きそれぞれの
    規則で扱う（オフライン「AIなし」構成は `SHERPA_AGENT=heuristic` の env 指定で動く）。"""
    monkeypatch.setenv(AC.EXTRA_AGENTS_ENV, "heuristic")
    ids = [c["id"] for c in AC.available_constructs()]
    assert "heuristic" not in ids
    assert ids == ["openai_only", "ollama_only", "codex_openai", "codex_ollama"]
    # 他の追加頭脳と併用していても heuristic だけは出ない。
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"cloud_provider": "gemini"})
    monkeypatch.setenv(AC.EXTRA_AGENTS_ENV, "heuristic,gemini")
    ids = [c["id"] for c in AC.available_constructs()]
    assert "heuristic" not in ids
    assert "gemini" in ids
    # default_agent() は SHERPA_AGENT=heuristic を明示すればそれをそのまま尊重する。
    monkeypatch.setenv("SHERPA_AGENT", "heuristic")
    assert AC.default_agent() == "heuristic"
    assert "heuristic" in AC.enabled_agents()


def test_a7_cloud_provider_selection_filters_openai_and_extra_agents(monkeypatch):
    """A7（クラウドプロバイダ排他選択）: `openai_only` と有効化済みの gemini/bedrock は、
    選択中のクラウドプロバイダ（`cloud_provider`・既定 openai）と一致しない限り選択肢に出ない。
    Codex(OpenAI)/Codex(Ollama)/ローカル(Ollama) は対象外のまま常に出る
    （Codex(OpenAI) の既定=非Azure構成は Codex 自身の認証を使い Sherpa のキー解決を経由しない・
    Ollama は A7 排他の対象外）。"""
    monkeypatch.setenv(AC.EXTRA_AGENTS_ENV, "gemini,bedrock")
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})   # 既定 = openai
    assert [c["id"] for c in AC.available_constructs()] == [
        "openai_only", "ollama_only", "codex_openai", "codex_ollama"]

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"cloud_provider": "bedrock"})
    ids = [c["id"] for c in AC.available_constructs()]
    assert ids == ["ollama_only", "codex_openai", "codex_ollama", "bedrock"]
    assert "openai_only" not in ids and "gemini" not in ids


def test_external_ai_blocked_until_enabled(monkeypatch):
    assert AC.runtime_blocked("gemini") is True
    assert AC.runtime_blocked("bedrock") is True
    # heuristic は「AI が未設定のときの安全網」＝既定値でもあるため遮断しない。
    assert AC.runtime_blocked("heuristic") is False
    assert AC.runtime_blocked("openai") is False
    monkeypatch.setenv(AC.EXTRA_AGENTS_ENV, "gemini")
    assert AC.runtime_blocked("gemini") is False
    assert AC.runtime_blocked("bedrock") is True


def test_construct_id_distinguishes_codex_model_provider():
    assert AC.construct_id({"agent": "openai"}) == "openai_only"
    assert AC.construct_id({"agent": "ollama"}) == "ollama_only"
    assert AC.construct_id({"agent": "codex"}) == "codex_openai"                      # 未設定は openai
    assert AC.construct_id({"agent": "codex", "codex_model_provider": "openai"}) == "codex_openai"
    assert AC.construct_id({"agent": "codex", "codex_model_provider": "ollama"}) == "codex_ollama"


def test_construct_id_strips_whitespace_around_codex_model_provider():
    """`codex_model_provider()`（実行時の共通resolver）と同じく strip+lowercase してから
    比較する。" OLLAMA " のような値を誤って codex_openai 表示にしない
    （黙って構成を上書き表示しない）。"""
    assert AC.construct_id({"agent": "codex", "codex_model_provider": " OLLAMA "}) == "codex_ollama"
    assert AC.construct_id({"agent": "codex", "codex_model_provider": " openai "}) == "codex_openai"


def test_codex_model_provider_falls_back_to_openai_only_when_unset():
    assert AC.codex_model_provider({"agent": "codex"}) == "openai"
    assert AC.codex_model_provider({"codex_model_provider": ""}) == "openai"
    assert AC.codex_model_provider({"codex_model_provider": "ollama"}) == "ollama"


def test_codex_model_provider_raises_for_unknown_nonempty_value():
    """非空の不正値（env 誤記・旧データ等）を黙って openai へ倒さない（黙ったプロバイダ切替の
    是正）。"""
    with pytest.raises(AC.InvalidCodexModelProviderError, match="anthropic"):
        AC.codex_model_provider({"codex_model_provider": "anthropic"})


def test_codex_model_provider_rejects_falsy_non_string():
    """`False`/`0`/`[]`/`{}` は truthiness で「未設定」に化けず、常に拒否する
    （本関数は strict 引数を持たず常時 strict）。"""
    for bad in (False, 0, [], {}):
        with pytest.raises(AC.InvalidCodexModelProviderError):
            AC.codex_model_provider({"codex_model_provider": bad})


def test_construct_id_returns_out_of_list_id_for_invalid_codex_model_provider():
    """非空の不正値（env 誤記・旧データ・型破損等）を `codex_openai` に丸めて表示しない
    （`codex_model_provider()` は同じ値で honest failure になるのに画面だけ「Codex(OpenAI) が
    動いている」と偽って見える食い違いを防ぐ）。一覧に無い id を返すことで、画面側
    （`web/settings.js::renderConstructOptions`）の既存の「一覧外」保持機構に自然に乗せる。"""
    cid = AC.construct_id({"agent": "codex", "codex_model_provider": "anthropic"})
    assert cid not in [c["id"] for c in AC.CONSTRUCTS]
    for bad in (False, 0, [], {}):
        cid = AC.construct_id({"agent": "codex", "codex_model_provider": bad})
        assert cid not in [c["id"] for c in AC.CONSTRUCTS]


def test_effective_agent_strict_raises_for_invalid_explicit_sherpa_agent(monkeypatch):
    """`SHERPA_AGENT` が非空の不正値のとき、`strict=True` は黙って自動選択へ倒さない
    （env 誤記・旧データで到達しうる）。`strict=False`（既定）は従来どおり自動選択のまま＝
    表示/監査経由の呼び出しは壊れた設定でも動き続ける。"""
    monkeypatch.setenv("SHERPA_AGENT", "bogus-agent-name")
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    with pytest.raises(AC.InvalidAgentConfigError, match="bogus-agent-name"):
        AC.effective_agent({}, strict=True)
    assert AC.effective_agent({}) in AC.enabled_agents()   # 非strict は従来どおり動く


def test_effective_agent_strict_raises_for_unknown_saved_agent_value(monkeypatch):
    """保存済み `agent`（PUT /settings のallowlist検証を経ていない旧データ等）が既知の頭脳名
    （STANDARD_AGENTS|EXTRA_AGENTS）のどれでもない非空の不正値のとき、`strict=True` は黙って
    そのまま返さない＝`_select_provider` がどの分岐にも一致せず HeuristicProvider（別の頭脳）へ
    縮退することを防ぐ。`strict=False`（既定）は従来どおり生値を返す（表示/監査を壊さない）。"""
    monkeypatch.delenv("SHERPA_AGENT", raising=False)
    with pytest.raises(AC.InvalidAgentConfigError, match="totally-bogus-provider"):
        AC.effective_agent({"agent": "totally-bogus-provider"}, strict=True)
    assert AC.effective_agent({"agent": "totally-bogus-provider"}) == "totally-bogus-provider"


def test_effective_agent_strict_raises_for_invalid_cloud_provider(monkeypatch):
    """保存済み `agent` がクラウド系で、`cloud_provider`（A7）が非空の不正値のとき、`strict=True`
    は黙って ollama へ倒さない。`strict=False`（既定）は従来どおり ollama へフォールバックする。"""
    from sherpa import keys

    monkeypatch.setenv(AC.EXTRA_AGENTS_ENV, "bedrock")
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"cloud_provider": "not-a-real-provider"})
    with pytest.raises(keys.InvalidCloudProviderConfigError, match="not-a-real-provider"):
        AC.effective_agent({"agent": "bedrock"}, strict=True)
    assert AC.effective_agent({"agent": "bedrock"}) == "ollama"   # 非strict は従来どおり動く


def test_effective_agent_strict_rejects_falsy_non_string_saved_agent(monkeypatch):
    """保存済み `agent` が `False`/`0`/`[]`/`{}`（設定破損）のとき、truthiness で「未設定」に
    化けず strict では拒否する。非 strict は従来どおり未設定扱いで自動選択される。"""
    monkeypatch.delenv("SHERPA_AGENT", raising=False)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    for bad in (False, 0, [], {}):
        with pytest.raises(AC.InvalidAgentConfigError):
            AC.effective_agent({"agent": bad}, strict=True)
        assert AC.effective_agent({"agent": bad}) in AC.enabled_agents()


def test_disabled_agent_is_reported_not_silently_replaced(monkeypatch):
    """env で無効な AI が設定に残っていたら、既定へ倒さず明示的に伝える。"""
    from sherpa.providers import _DisabledProvider, get_provider

    p = get_provider({"agent": "bedrock", "bedrock_api_key": "dummy"})
    assert isinstance(p, _DisabledProvider)
    assert "利用できません" in p._plain_text("")


def test_codex_construct_forces_knowledge_on(monkeypatch):
    """Codex 構成は資料参照ON固定（決定 2026-08-15）。

    Codex CLI は read-only 実行でも自分で grep/ファイル参照ができるため、「参照オフのつもりなのに
    KB を覗く」状態を作らない。画面はトグルをON固定にするが、UI を信頼せずサーバでも強制する。
    """
    from sherpa import store
    from sherpa.routers.chat import _knowledge_for

    saved = {}
    monkeypatch.setattr(store, "get_settings", lambda uid: saved)

    saved.clear(); saved.update({"agent": "codex"})
    assert _knowledge_for("u", False) is True          # OFF 要求でも ON にする
    assert _knowledge_for("u", True) is True

    saved.clear(); saved.update({"agent": "codex", "codex_model_provider": "ollama"})
    assert _knowledge_for("u", False) is True          # Codex(Ollama) も同じ

    saved.clear(); saved.update({"agent": "openai"})
    assert _knowledge_for("u", False) is False         # 直結構成は要求どおり（素の会話ができる）
    assert _knowledge_for("u", True) is True

    # 設定が読めない時は要求どおり（可用性優先＝チャット自体を止めない）
    def _boom(uid):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_settings", _boom)
    assert _knowledge_for("u", False) is False


def test_default_construct_is_selectable_from_the_screen(monkeypatch):
    """未設定の利用者が「画面から選び直せない構成」に張り付かないこと。

    実際に起きた不具合（2026-08-16）: 保存前の既定が `heuristic`（簡易・AIなし）で、これは
    `SHERPA_EXTRA_AGENTS` を立てない限り選択肢に出ない。初期状態の利用者はチャットの AI 選択が
    「簡易（AIなし）」のまま AI が動かず、しかもその選択肢が一覧に無いので状況が分からなかった。

    Codex CLI・認証は自動選択（`_auto_default_agent`）の判定材料になるため、開発機の実際の状態に
    関わらず決定的になるよう明示的に揃える（CLI あり・実キーありで既定 `DEFAULT_CONSTRUCT_ID`
    ＝codex_openai と一致する構成にする）。実キーは中央設定（system_settings）で用意する
    （`_codex_auth_available`/`_auto_default_agent` はもう env を読まず `sherpa.keys.resolve_api_key`
    経由で解決するため）。
    """
    import shutil

    monkeypatch.delenv("SHERPA_AGENT", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"openai_api_key": "sk-x"})
    ids = {c["id"] for c in AC.available_constructs()}
    assert AC.DEFAULT_CONSTRUCT_ID in ids
    assert AC.construct_id({}) == AC.DEFAULT_CONSTRUCT_ID
    assert AC.construct_id(None) == AC.DEFAULT_CONSTRUCT_ID
    assert AC.default_agent() in AC.enabled_agents()


def test_default_agent_env_override_must_also_be_selectable(monkeypatch):
    """env で既定を変えられるが、選択肢に無い頭脳を指定されたら固定値へではなく自動選択へ戻す
    （`tests/unit/test_default_agent_autoselect.py` がこのフォールバック自体の詳細な契約を持つ・
    ここでは「選択肢に無ければ heuristic に居座らない」ことだけ確認する）。"""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)   # 自動選択の行き先を固定して決定的にする
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SHERPA_AGENT", "ollama")
    assert AC.default_agent() == "ollama"
    monkeypatch.setenv("SHERPA_AGENT", "heuristic")        # 有効化していない＝選べない
    assert AC.default_agent() == "ollama"                  # 固定 codex ではなく自動選択（この場合は ollama）
    monkeypatch.setenv(AC.EXTRA_AGENTS_ENV, "heuristic")   # 明示的に有効化したなら尊重する
    assert AC.default_agent() == "heuristic"


# ===== ローカル/社内サーバ/クラウド/クラウド（OpenAI 互換）判定（4値）の唯一の真実源 =====
# UI の担当バッジ（render.js）はここの判定結果（`_usage_meta`/`_sub_agent_metrics` 経由で
# `metrics.is_local`/`usage.is_local` として渡る）をそのまま表示するだけで、自分では推測しない。
# `system_settings={}` を明示的に渡し、DB 未接続の unit test でも `llm.openai_endpoint_kind` の
# fail-safe 既定（"openai"）へ確実に倒す（省略すると `store.get_system_settings()` を実際に叩く）。

def test_is_local_ollama_always_local():
    assert AC.is_local("ollama") == "local"
    assert AC.is_local("Ollama") == "local"   # 大小文字を問わない（他の判定関数と同じ規律）


def test_is_local_known_cloud_providers_always_cloud():
    for name in ("openai", "gemini", "bedrock"):
        assert AC.is_local(name, system_settings={}) == "cloud"


def test_is_local_openai_on_prem_when_endpoint_kind_custom_and_host_private():
    """DGX Spark 等・LAN 内に自前で立てた OpenAI 互換エンドポイント（`openai_endpoint_kind=custom`
    かつホストが私有/ローカル範囲）は「クラウド」ではなく「社内サーバ」（on_prem）。"""
    assert AC.is_local("openai", system_settings={
        "openai_endpoint_kind": "custom", "openai_base_url": "http://10.0.0.5:8000/v1"}) == "on_prem"
    assert AC.is_local("openai", system_settings={"openai_endpoint_kind": "azure"}) == "cloud"


def test_is_local_openai_cloud_compat_when_endpoint_kind_custom_and_host_public():
    """`openai_endpoint_kind=custom` でもホストが公開 FQDN/グローバル IP なら「社内サーバ」では
    なく「クラウド（OpenAI 互換）」（"cloud_compat"）——"custom" というだけで一律 on_prem 扱いに
    すると、単に OpenAI 本家・Azure 以外の外部クラウド API を「社内サーバ」と誤表示してしまう
    （`openai_base_url` 省略時は既定 URL（api.openai.com＝公開）へ落ちるので同じく cloud_compat）。"""
    assert AC.is_local("openai", system_settings={
        "openai_endpoint_kind": "custom", "openai_base_url": "https://api.example.com/v1"}) == "cloud_compat"
    assert AC.is_local("openai", system_settings={"openai_endpoint_kind": "custom"}) == "cloud_compat"


def test_is_local_openai_trailing_dns_root_dot_still_classified_as_cloud():
    """`openai_endpoint_kind` 未設定（host から推定）かつ `openai_base_url` に DNS ルートドット
    （`"api.openai.com."`）が付いていても、`llm.openai_endpoint_kind()` がホストを正規化してから
    判定するため引き続き "openai" 扱いになり、`is_local()` は "cloud"（"cloud_compat" ではない）
    のまま——正規化が `openai_endpoint_kind()` の入口まで届いていないと "custom" に誤分類され、
    ここが "cloud_compat" になってしまっていた（openai_endpoint_kind()→is_local() を通しで固定）。"""
    assert AC.is_local(
        "openai", system_settings={"openai_base_url": "https://api.openai.com./v1"}) == "cloud"
    assert AC.is_local("openai", system_settings={
        "openai_base_url": "https://myres.openai.azure.com./openai/v1"}) == "cloud"


def test_is_local_codex_depends_on_codex_model_provider():
    """Codex は常に provider_id="codex" を名乗るため、実際の接続先は `codex_model_provider` でしか
    分からない（見ずに「クラウド」と決め打つと Codex(Ollama) 構成を誤分類する）。"""
    assert AC.is_local("codex", codex_model_provider="ollama") == "local"
    assert AC.is_local("codex", codex_model_provider="openai", system_settings={}) == "cloud"
    assert AC.is_local("codex", system_settings={}) == "cloud"   # 未指定＝ codex_model_provider() の「既定 openai」と同じ仕様
    assert AC.is_local("codex", codex_model_provider="openai", system_settings={
        "openai_endpoint_kind": "custom", "openai_base_url": "http://10.0.0.5:8000/v1"}) == "on_prem"
    assert AC.is_local("codex", codex_model_provider="openai", system_settings={
        "openai_endpoint_kind": "custom", "openai_base_url": "https://api.example.com/v1"}) == "cloud_compat"


def test_is_local_unknown_provider_is_none_not_a_guess():
    """未知の値（将来の新規頭脳・壊れた設定等）は None（誤断定しない）。"""
    assert AC.is_local(None) is None
    assert AC.is_local("") is None
    assert AC.is_local("unknown-future-provider") is None


def test_stored_agent_default_matches_the_code_default():
    """DB の既定値とコードの既定値がずれていないこと（片方だけ直すと再発する）。"""
    from sherpa.store import db

    ddl = "\n".join(db._SCHEMA)
    assert f"agent TEXT NOT NULL DEFAULT '{AC.DEFAULT_AGENT}'" in ddl
    assert f"ALTER TABLE user_settings ALTER COLUMN agent SET DEFAULT '{AC.DEFAULT_AGENT}'" in ddl


def test_update_settings_without_agent_field_leaves_it_unset_not_baked(monkeypatch):
    """RV HIGH（2026-08-18 Codex RV 2巡目 指摘1）: まだ頭脳を選んでいない利用者が `agent` を含まない
    `PUT /settings`（例: `web/chat/menus.js::saveModel()` が `{codex_model: v}` だけ PUT する）を
    1回踏んだだけで、以前は無条件に `"heuristic"` が永続化されていた（RV1是正）。続く RV1是正
    （`... or agent_constructs.default_agent()`）は "heuristic" 直書きよりマシだが、**その瞬間の
    PATH/env に依存する値を DB へ焼き付ける**問題を残していた＝後から Codex CLI が消えた／
    OPENAI_API_KEY を入れた／PATH が変わった、といった環境変化があっても DB の古い選択に
    張り付いたままになる。

    直し方（この2巡目の是正）: `agent` を明示された値だけ保存し、一度も選ばれていないなら
    DB 上も「未設定」（空文字 `''`）のままにする。`_select_provider`／`construct_id` は両方とも
    `s.get("agent") or default_agent()` の形で読むため、`''` は保存時ではなく**呼び出しのたびに**
    その時点の環境で自動選択される（DB には何も焼き付けない）。

    実 DB（テスト用に分離された `sherpa_test`・`tests/conftest.py` 参照）に対して実際に
    `update_settings` を呼び、保存後の `get_settings()['agent']` が空文字のままであること、
    その状態で `_select_provider` が実際に自動選択へ落ちること、`agent` を明示した保存は
    これまでどおり効くことを確認する。"""
    from sherpa import agents as facade
    from sherpa import providers as P
    from sherpa import store

    monkeypatch.delenv("SHERPA_AGENT", raising=False)   # 明示指定なし＝自動選択のケースを再現
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")

    uid = f"unit-agent-unset-{int(time.time() * 1000)}"

    # 1) agent を含まない更新 → 保存後も DB 上は空文字（未設定）のまま＝値が焼き付かない。
    saved = store.update_settings(uid, codex_reasoning="medium")
    assert saved["agent"] == "", f"未選択のまま具体値が焼き付いた: {saved!r}"
    fetched = store.get_settings(uid)
    assert fetched["agent"] == "", f"保存後の読み出しでも空のまま（未設定）であるべき: {fetched!r}"

    # 2) その状態で _select_provider が実際に自動選択へ落ちること（焼き付いていない証拠。
    #    CLI 無し・実キー有りで openai が選ばれることを確認する。`_auto_default_agent()`（頭脳の
    #    自動選択）も `_select_provider` の実キー解決も、もう env を読まず
    #    `sherpa.keys.resolve_api_key` 経由で解決する＝中央設定（system_settings）にキーを
    #    用意してから読み直す。中央キーのみで A7 は解決できるため（`cloud_provider` 省略時は
    #    既定 openai）、個人キーの実 DB 書込みは不要（A6 実 DB を汚さない・復元漏れの余地も無い）。
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"openai_api_key": "sk-central-x", "personal_api_keys_allowed": True})
    fetched = store.get_settings(uid)
    p = P._select_provider(fetched)
    assert isinstance(p, facade.OpenAIProvider), f"自動選択（openai）に落ちていない: {type(p)!r}"

    # 3) agent を明示した保存はこれまでどおり効く（次回以降そのまま返る＝明示の意思は尊重する）。
    saved2 = store.update_settings(uid, agent="ollama")
    assert saved2["agent"] == "ollama"
    assert store.get_settings(uid)["agent"] == "ollama"
