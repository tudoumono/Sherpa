"""検索アシスタント（下調べだけを安いモデルへ任せる・決定 2026-08-15）。

動機: 資料調査は入力トークンが支配的（実測 1問で input 118k / output 1k）。読む作業を安いモデルへ
任せ、最終回答だけメインの AI が作れば費用を大きく下げられる。旧「サブエージェント・プロファイル」
（admin がプロファイルを定義）を、**利用者ごとの1設定**へ置き換えたもの。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import search_helper as SH  # noqa: E402


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_unset_means_main_ai_searches():
    """未設定＝従来どおりメインの AI が自分で検索する（既定は変えない）。"""
    assert SH.resolve({}) is None
    assert SH.resolve({"search_helper": ""}) is None


def test_resolve_raises_for_unknown_nonempty_choice():
    """未設定（空）は正当な「使わない」選択＝None。非空の不正値（env 誤記・旧データ等）は
    黙って OFF 縮退させず `InvalidSearchHelperConfigError` を送出する（黙ったプロバイダ切替の
    是正・裁定＝エラー化）。"""
    assert SH.resolve({}) is None
    assert SH.resolve({"search_helper": ""}) is None
    with pytest.raises(SH.InvalidSearchHelperConfigError, match="gemini"):
        SH.resolve({"search_helper": "gemini"})


def test_resolve_strips_whitespace_around_valid_choice():
    """`search_helper` の前後の空白を取り除いてから照合する（strip 不整合の是正）。
    " ollama " のような値も正当な ollama 選択として解決される（不正値として誤ってエラー化しない）。"""
    sub = SH.resolve({"search_helper": " ollama ", "ollama_url": "http://localhost:11434"})
    assert sub is not None and sub["provider"] == "ollama"


def test_resolve_raises_for_broken_admin_model(monkeypatch):
    """選択肢自体は正当でも、解決先の管理者モデル設定（`model_catalog`）が壊れている場合は
    黙ってメインAIへ倒さず `InvalidSearchHelperConfigError` を送出する（裁定＝エラー化）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "model_catalog": {"ollama": {"subsearch": {"allowed": ["bad name!"], "default": "bad name!"}}}})
    with pytest.raises(SH.InvalidSearchHelperConfigError):
        SH.resolve({"search_helper": "ollama", "ollama_url": "http://localhost:11434"})

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True,
        "model_catalog": {"openai": {"subsearch": {"allowed": ["bad model!"], "default": "bad model!"}}}})
    with pytest.raises(SH.InvalidSearchHelperConfigError):
        SH.resolve({"search_helper": "openai", "openai_api_key": "sk-x"})


def test_ollama_uses_user_settings():
    """`ollama_url` は個人設定のまま。モデル名は個人設定（`search_helper_model`）でなく
    管理者のカタログ（ollama/subsearch）から解決される。"""
    sub = SH.resolve({"search_helper": "ollama", "ollama_url": "http://localhost:11434"})
    assert sub["provider"] == "ollama"
    assert sub["url"] == "http://localhost:11434" and sub["model"] == "qwen2.5"
    # search_helper_model を明示しても無視される（個人設定に無い）。
    sub2 = SH.resolve({"search_helper": "ollama", "search_helper_model": "llama3.2:1b"})
    assert sub2["model"] == "qwen2.5"


def test_openai_requires_key_and_defaults_to_cheap_model(monkeypatch):
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    assert SH.resolve({"search_helper": "openai"}) is None       # 鍵が無ければ使えない＝メインへ戻す
    sub = SH.resolve({"search_helper": "openai", "openai_api_key": "sk-x"})
    assert sub["provider"] == "openai" and sub["key"] == "sk-x"
    assert sub["model"] == SH.DEFAULT_OPENAI_MODEL               # 空欄なら安価な既定
    sub2 = SH.resolve({"search_helper": "openai", "openai_api_key": "sk-x",
                       "search_helper_model": "gpt-5.4-mini"})
    assert sub2["model"] == "gpt-5.4-mini"


def test_openai_uses_custom_catalog_default_over_hardcoded_default(monkeypatch):
    """低リスク是正（RV 3巡目）: 空欄時のモデル解決は `model_catalog`（openai/subsearch）を経由する
    （`SH.DEFAULT_OPENAI_MODEL` は catalog が空のときだけ使われる最終フォールバック）。admin が
    カタログでカスタム既定を設定した場合、それが実際に使われることを固定する（組み込み既定と
    たまたま同じ値を比較するだけの弱いテストにしない）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True,
        "model_catalog": {"openai": {"subsearch": {"allowed": ["my-custom-subsearch-model"],
                                                    "default": "my-custom-subsearch-model"}}}})
    sub = SH.resolve({"search_helper": "openai", "openai_api_key": "sk-x"})
    assert sub["model"] == "my-custom-subsearch-model"
    assert sub["model"] != SH.DEFAULT_OPENAI_MODEL


def test_malformed_search_helper_model_is_ignored_not_fatal(monkeypatch):
    """`search_helper_model` は個人設定に無い＝壊れた/悪意ある値を送っても無視され、管理者の
    カタログ既定（常に形式的に正しい値）で解決が続く（読まれないので fail-closed 分岐にも
    到達しない）。"""
    sub = SH.resolve({"search_helper": "ollama", "search_helper_model": "../etc/passwd"})
    assert sub is not None and sub["model"] == "qwen2.5"
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    sub2 = SH.resolve({"search_helper": "openai", "openai_api_key": "sk-x",
                       "search_helper_model": "bad model name"})
    assert sub2 is not None and sub2["model"] == SH.DEFAULT_OPENAI_MODEL


def test_never_gets_ask_user_tool():
    """下調べ役はユーザーへの確認カードを出せない（モデル生成文を公式UIとして出さない・secRV 踏襲）。"""
    sub = SH.resolve({"search_helper": "ollama"})
    assert "ask_user" not in sub["tools"]
    assert {"ripgrep_search", "read_around", "list_docs"} <= set(sub["tools"])


# `providers/base.py::_sub_loop`（base.py:757-）が実際に参照するキー（`sub["provider"]`・
# `sub["tools"]`・`sub["url"]`（ollama）／`sub["key"]`（openai）・`sub["model"]`・
# `sub["guard"]["max_turns"]`・`sub["guard"]["llm_timeout"]`）と、根拠ゲート（`_plan_min_citations`
# 等）が使う `sub["guard"]["min_citations"]`・usage タグ付けに使う `sub["profile_id"]`。
# `get_provider()` が組み立てた実物の `_sub` がこの形を満たさなければ、`_sub_loop` は
# KeyError で落ちる（`test_sub_loop.py::test_incomplete_sub_dict_raises_instead_of_silently_falling_back`
# が確認）＝ここで形を固定しておかないと、欠落を検出できないまま黙って検索アシスタントが
# フォールバックする回帰が起こり得る。
def _assert_sub_shape(sub: dict) -> None:
    assert sub["provider"] in ("ollama", "openai")
    tools = sub["tools"]
    assert isinstance(tools, (set, frozenset)) and tools
    assert all(isinstance(t, str) for t in tools)
    assert isinstance(sub["model"], str) and sub["model"]
    assert isinstance(sub["profile_id"], str) and sub["profile_id"]
    if sub["provider"] == "ollama":
        assert isinstance(sub["url"], str) and sub["url"]
    else:
        assert isinstance(sub["key"], str) and sub["key"]
    guard = sub["guard"]
    assert isinstance(guard["min_citations"], int)
    assert isinstance(guard["max_turns"], int)
    assert isinstance(guard["llm_timeout"], int)


def test_wired_only_for_openai_construct(monkeypatch):
    """メインが OpenAI 直結のときだけ効く（Codex は自分でツールを回すため介在しない）。

    `_sub` は `is not None` だけでなく、`_sub_loop` が実際に参照する形（`_assert_sub_shape`）を
    満たすことまで固定する（手組みの辞書ではなく `get_provider()` が組み立てた実物で確認する
    ＝配線のどこかでキーが欠落する回帰を検出できる）。"""
    from sherpa.providers import get_provider

    # WEB-1: `get_provider()` の1ターン唯一の読取点は `store._read_system_settings_fresh()`
    # （共有キャッシュを介さない生の読取・TOCTOU 対策）——`get_system_settings` ではない。
    monkeypatch.setattr("sherpa.store._read_system_settings_fresh", lambda: {"personal_api_keys_allowed": True})
    s = {"agent": "openai", "openai_api_key": "sk-x", "search_helper": "ollama",
         "ollama_url": "http://localhost:11434", "ollama_model": "qwen2.5"}
    sub = get_provider(s)._sub
    assert sub is not None
    _assert_sub_shape(sub)

    for other in ({**s, "agent": "codex"}, {**s, "agent": "ollama"}):
        assert get_provider(other)._sub is None


def test_wired_invalid_search_helper_sets_error_not_silent_off(monkeypatch):
    """非空の不正値（typo/旧データ）は `_sub` を組み立てず、`_search_helper_error` に理由を残す
    （`run()` が honest failure として停止する＝メインAIの高コスト経路を利用者の承認前に
    黙って開始しない・裁定＝エラー化）。"""
    from sherpa.providers import get_provider

    # WEB-1: `get_provider()` の1ターン唯一の読取点は `store._read_system_settings_fresh()`
    # （共有キャッシュを介さない生の読取・TOCTOU 対策）——`get_system_settings` ではない。
    monkeypatch.setattr("sherpa.store._read_system_settings_fresh", lambda: {"personal_api_keys_allowed": True})
    s = {"agent": "openai", "openai_api_key": "sk-x", "search_helper": "gemini"}
    p = get_provider(s)
    assert p._sub is None
    assert p._search_helper_error is not None
    assert "gemini" in p._search_helper_error


def test_run_stops_with_honest_failure_on_invalid_search_helper():
    """`p._search_helper_error` が設定されていると、`run()` はメインAIの反復検索（`_agentic_run`）
    へ進まず honest failure で停止する（黙って高コスト経路を開始しない）。scope も含める
    （会話再表示で範囲が「全体」へ戻る回帰の防止）。"""
    from sherpa.providers.base import Ctx, _GenProvider

    class _P(_GenProvider):
        label, model, provider_id = "T", "m", "openai"

        def _agentic_run(self, ctx, decision):
            raise AssertionError("下調べ設定が不正なのにメインAIの反復検索が呼ばれている")

    p = _P()
    p._search_helper_error = "下調べ役の設定が不正です（'gemini'）。設定画面で選び直してください。"
    narrow_scope = {"world": "v1", "scope_paths": ["4期/"], "source": "scope"}
    ctx = Ctx(message="バッチ停止の記録は？", world="v1", knowledge=True, scope_meta=narrow_scope,
             route=lambda m: {"lens": "qa", "reason": "t", "input": m},
             dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}, "sources": []},
             make_sources=lambda docs: [{"doc_id": d} for d in docs])
    events = list(p.run(ctx))
    nodes = [e for e in events if e.get("type") == "node"]
    assert any("下調べ設定" in (n.get("label") or "") for n in nodes)
    result = next(e for e in events if e.get("type") == "_result")
    assert "下調べ" in result["env"]["headline"]
    # qa レンズは層フィルタが実効するため layer_applied=True が足される（scope 自体は不変）。
    assert result["env"]["scope"] == {**narrow_scope, "layer_applied": True}


def test_wired_shape_openai_branch(monkeypatch):
    """openai 分岐（search_helper='openai'）も同じ形の契約を満たす（`_assert_sub_shape` 参照・
    provider='openai' のときは `url` ではなく `key` が必須）。"""
    from sherpa.providers import get_provider

    # WEB-1: `get_provider()` の1ターン唯一の読取点は `store._read_system_settings_fresh()`
    # （共有キャッシュを介さない生の読取・TOCTOU 対策）——`get_system_settings` ではない。
    monkeypatch.setattr("sherpa.store._read_system_settings_fresh", lambda: {"personal_api_keys_allowed": True})
    # メイン・検索アシスタントとも同じ `openai_api_key`（実配線どおり・専用のサブ鍵は無い）。
    s = {"agent": "openai", "openai_api_key": "sk-main", "search_helper": "openai"}
    sub = get_provider(s)._sub
    assert sub is not None
    _assert_sub_shape(sub)
    assert sub["provider"] == "openai"


def test_sub_failure_returns_honest_failure_without_main_agentic_retry():
    """下調べ役が根拠を集めきれなかったら honest failure で停止する（黙って別の高コスト経路
    ＝メイン自身の反復検索へ倒さない）。メイン（`_agentic_loop`）は一度も実行されず、利用者には
    原因を特定できる honest failure を返す（設定確認／下調べ OFF は利用者の判断に委ねる）。
    `p._sub` は一時的にも外さない＝設定はそのまま保持される。
    """
    from sherpa.providers.base import Ctx, _GenProvider

    ran = []

    class _P(_GenProvider):
        label, model, provider_id = "T", "m", "openai"

        def _sub_agentic_loop(self, ctx):
            ran.append("sub")
            yield {"final": "", "docs": set(), "searched": True, "cites": [], "cards": []}   # 引用0件

        def _agentic_loop(self, ctx):
            ran.append("main")   # 呼ばれてはいけない（意図しない高コスト経路）
            yield {"final": "メインが調べ直した回答", "docs": set(), "searched": True,
                  "cites": [], "cards": []}

    p = _P()
    p._sub = {"provider": "openai", "key": "sk-x", "url": None, "model": "gpt-5.4-mini",
              "tools": frozenset({"ripgrep_search"}), "guard": {"min_citations": 1, "max_turns": 6,
                                                                "llm_timeout": 60},
              "profile_id": "search-helper-openai", "description": "", "name": "下調べ役"}
    ctx = Ctx(message="バッチ停止の記録は？", world="v1", knowledge=True,
              route=lambda m: {"lens": "qa", "reason": "t", "input": m},
              dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}, "sources": []},
              make_sources=lambda docs: [{"doc_id": d} for d in docs])
    events = list(p.run(ctx))

    assert ran == ["sub"], "下調べ役の失敗後にメインの反復検索へ黙って落ちてしまっている"
    env = next(e["env"] for e in events if e.get("type") == "_result")
    assert "下調べ" in env["headline"]                  # 原因（下調べAI）を特定できる
    assert p._sub is not None                          # 一時的にも外さない＝設定は壊さない
    # scope 欠落なし。qa レンズは層フィルタが実効するため layer/layer_applied が足される。
    assert env["scope"] == {"world": "v1", "scope_paths": [], "source": "all",
                            "layer": "both", "layer_applied": True}


def test_audit_records_who_read_the_documents(monkeypatch):
    """監査ログ（chat.turn）から「資料を読んだのが誰か」を追える（2026-08-15 追加）。

    以前は provider（メイン）しか記録されず、費用の内訳や「安いモデルにしたのに高い」の切り分けが
    監査ログだけではできなかった。生値は入れない（provider は allowlist・モデル名は形式検証済み）。
    """
    from sherpa.chat_service import _audit_search_helper

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    base = {"agent": "openai", "openai_api_key": "sk-x"}

    assert _audit_search_helper(base) is None                       # 未設定＝メインが読んだターン
    # モデル名は管理者のカタログ既定（openai/ollama の subsearch）から解決される。
    assert _audit_search_helper({**base, "search_helper": "openai"}) == "openai/gpt-5.4-mini"
    assert _audit_search_helper({**base, "search_helper": "ollama",
                                 "ollama_url": "http://localhost:11434"}) == "ollama/qwen2.5"

    # 効かない構成（Codex は自分でツールを回す）では出さない＝誤読を招かない
    assert _audit_search_helper({**base, "agent": "codex", "search_helper": "openai"}) is None

    # search_helper_model はもう読まれない＝壊れた値を送っても管理者のカタログ既定のまま監査に出る。
    assert _audit_search_helper({**base, "search_helper": "openai",
                                 "search_helper_model": "bad model"}) == "openai/gpt-5.4-mini"


def test_audit_search_helper_uses_effective_agent_not_saved_when_a7_mismatches(monkeypatch):
    """保存 agent=openai でも、選択中のクラウドプロバイダ（A7）が openai でなければ実行は
    effective_agent() 経由で ollama にフォールバックする＝その場合は openai 向けの下調べ役判定を
    走らせない（保存値だけを見ていた頃は、実際には ollama で答えたのに openai 向け監査表記が
    出てしまう食い違いがあった）。"""
    from sherpa.chat_service import _audit_search_helper

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"personal_api_keys_allowed": True, "cloud_provider": "gemini"})
    base = {"agent": "openai", "openai_api_key": "sk-x",
           "search_helper": "openai", "search_helper_model": "gpt-5.4-mini"}
    assert _audit_search_helper(base) is None
