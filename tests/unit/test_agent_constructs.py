"""実行構成（3構成）の契約（`sherpa/agent_constructs.py`）。

標準が見せるのは 簡易 / Codex 調査(OpenAI) / Codex 調査(Ollama) の3つだけ。保存値が旧・直結経路
（openai/ollama）の利用者は簡易として扱う。
gemini/bedrock/heuristic はチャットで閉じており、設定に残っていても実行時に遮断する
（黙って別の AI が答える状態を作らない）。
"""
from __future__ import annotations

import time

import pytest

from sherpa import agent_constructs as AC


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    from sherpa import required_tools
    monkeypatch.setattr(required_tools, "codex_cli_missing", lambda: False)   # 実機の codex の有無に依らない


def test_default_shows_exactly_three_constructs():
    ids = [c["id"] for c in AC.available_constructs()]
    assert ids == ["codex_openai", "codex_ollama", "simple"]
    labels = {c["id"]: c["label"] for c in AC.available_constructs()}
    assert labels["codex_openai"] == "Codex 調査（OpenAI）"
    assert labels["codex_ollama"] == "Codex 調査（Ollama）"


def test_legacy_openai_ollama_agent_resolves_to_simple(monkeypatch):
    """保存値が旧・直結経路（openai/ollama）なら簡易として扱う（DB は書き換えない）。
    strict でも拒否しない。"""
    for legacy in ("openai", "ollama", " OpenAI "):
        assert AC.effective_agent({"agent": legacy}, system_settings={}) == "simple"
        assert AC.effective_agent({"agent": legacy}, system_settings={}, strict=True) == "simple"
        assert AC.construct_id({"agent": legacy}, system_settings={}) == "simple"


def test_auto_default_falls_back_to_simple_not_the_old_path(monkeypatch):
    import shutil
    monkeypatch.delenv("SHERPA_AGENT", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert AC._auto_default_agent({"openai_api_key": "sk-central-x"}) == "simple"
    assert AC.default_agent({"openai_api_key": "sk-central-x"}) == "simple"


def test_is_real_api_key_returns_false_for_non_string_without_raising():
    """RV9 是正の固定: 非文字列（設定破損・型不正な入力等）を渡しても `AttributeError`
    （`.strip()`）を出さず、「キーなし」として fail-closed に扱う。"""
    for bad in ({"k": "v"}, ["sk-x"], 123, 1.5, object(), True, False):
        assert AC.is_real_api_key(bad) is False
    assert AC.is_real_api_key(None) is False
    assert AC.is_real_api_key("") is False
    assert AC.is_real_api_key("sk-real-key") is True


def test_closed_agents_are_never_selectable_blocked_or_defaulted(monkeypatch):
    """AI なし（heuristic）・Gemini・AWS Bedrock はチャットで閉じている: 有効化されず、選択肢にも出ない。
    実行時は常に遮断（保存値が残っていてもチャットで選び直しを案内する）。"""
    assert AC.EXTRA_AGENTS == frozenset()
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"cloud_provider": "gemini"})
    assert AC.enabled_agents() == AC.STANDARD_AGENTS
    assert [c["id"] for c in AC.available_constructs()] == ["codex_openai", "codex_ollama", "simple"]
    assert AC.default_agent({}) in ("codex", "simple")
    assert AC.runtime_blocked("gemini") is True and AC.runtime_blocked("bedrock") is True
    assert AC.runtime_blocked("openai") is False


def test_construct_id_distinguishes_codex_model_provider():
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


def test_effective_agent_strict_raises_for_unknown_saved_agent_value(monkeypatch):
    """保存済み `agent`（PUT /settings のallowlist検証を経ていない旧データ等）が既知の頭脳名
    （STANDARD_AGENTS|EXTRA_AGENTS）のどれでもない非空の不正値のとき、`strict=True` は黙って
    そのまま返さない＝`_select_provider` がどの分岐にも一致せず HeuristicProvider（別の頭脳）へ
    縮退することを防ぐ。`strict=False`（既定）は従来どおり生値を返す（表示/監査を壊さない）。"""
    monkeypatch.delenv("SHERPA_AGENT", raising=False)
    with pytest.raises(AC.InvalidAgentConfigError, match="totally-bogus-provider"):
        AC.effective_agent({"agent": "totally-bogus-provider"}, strict=True)
    assert AC.effective_agent({"agent": "totally-bogus-provider"}) == "totally-bogus-provider"


def test_effective_agent_strict_rejects_falsy_non_string_saved_agent(monkeypatch):
    """保存済み `agent` が `False`/`0`/`[]`/`{}`（設定破損）のとき、truthiness で「未設定」に
    化けず strict では拒否する。非 strict は従来どおり未設定扱いで自動選択される。"""
    monkeypatch.delenv("SHERPA_AGENT", raising=False)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    for bad in (False, 0, [], {}):
        with pytest.raises(AC.InvalidAgentConfigError):
            AC.effective_agent({"agent": bad}, strict=True)
        assert AC.effective_agent({"agent": bad}) in AC.enabled_agents()


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

    saved.clear(); saved.update({"agent": "simple"})
    assert _knowledge_for("u", False) is True          # 簡易も資料参照ON固定（検索・出典確認が本体）

    saved.clear(); saved.update({"agent": "openai"})
    assert _knowledge_for("u", False) is True          # 旧・直結の保存値は簡易として扱う

    # 設定が読めない時は要求どおり（可用性優先＝チャット自体を止めない）
    def _boom(uid):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_settings", _boom)
    assert _knowledge_for("u", False) is False


def test_default_construct_is_selectable_from_the_screen(monkeypatch):
    """未設定の利用者が「画面から選び直せない構成」に張り付かないこと。

    実際に起きた不具合（2026-08-16）: 保存前の既定が `heuristic`（簡易・AIなし）で、これは
    チャットで閉じていて選択肢に出ない。初期状態の利用者はチャットの AI 選択が
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
    #    CLI 無しで simple が選ばれることを確認する。
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"openai_api_key": "sk-central-x", "personal_api_keys_allowed": True})
    fetched = store.get_settings(uid)
    p = P._select_provider(fetched)
    assert isinstance(p, facade.SimpleProvider), f"自動選択（simple）に落ちていない: {type(p)!r}"

    # 3) agent を明示した保存はこれまでどおり効く（次回以降そのまま返る＝明示の意思は尊重する）。
    saved2 = store.update_settings(uid, agent="ollama")
    assert saved2["agent"] == "ollama"
    assert store.get_settings(uid)["agent"] == "ollama"


def test_simple_is_a_standard_agent_and_maps_to_its_construct():
    """簡易（検索して答える）は env 無効化の対象外の標準頭脳で、保存値 agent=simple が構成 simple に対応する。"""
    assert "simple" in AC.STANDARD_AGENTS
    assert AC.construct_id({"agent": "simple"}, system_settings={}) == "simple"
    assert AC.effective_agent({"agent": "simple"}, system_settings={}, strict=True) == "simple"
    assert not AC.runtime_blocked("simple")
