"""思考プロバイダの registry（頭脳選択）。

`get_provider` / `provider_info` / `AGENT_PROVIDERS` / `_select_provider` / `_UnwiredProvider` を持つ。
各 Provider クラスは `from sherpa import agents as _facade` で呼び出し時に解決する（`sherpa.agents` の差し替えを効かせる・循環 import 回避）。
  base.py＝共通土台、prompts.py＝プロンプト、simple.py＝簡易、codex/＝Codex 経路。
設計: docs/design/chat.md「2つの頭脳と、頭脳の選び方」
"""
from __future__ import annotations

import shutil
from typing import Iterator

from .. import layer as layer_mod
from .base import Ctx, Provider, _node, _plain_run


class _UnwiredProvider(Provider):
    """未接続の LLM バックエンド。「未接続」と正直に返す（嘘の回答をしない）。"""

    def __init__(self, name: str, howto: str):
        self.label, self.model, self.howto = name, "", howto

    def _plain_text(self, message: str = "") -> str:
        return f"{self.label} はまだ接続されていません。{self.howto}"

    def run(self, ctx: Ctx) -> Iterator[dict]:
        if not ctx.knowledge:  # オフでも未接続を正直に返す（出典枠は出さない）
            yield from _plain_run(self, ctx); return
        yield _node("connect", "think", f"{self.label} に接続", self.howto, "active")
        env = {"lens": "qa", "headline": f"{self.label} はまだ接続されていません。{self.howto}",
               "summary": {"total": 0}, "data": {}, "sources": [],
               "agentic_failure": "error",  # 終了理由の分布で完了扱いにしない
               "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world, lens="qa")}
        yield _node("connect", "think", f"{self.label} に接続", "未接続", "done")
        yield {"type": "answer_delta", "text": env["headline"]}
        yield {"type": "_result", "env": env,
               "decision": {"lens": "qa", "input": ctx.message, "reason": f"{self.label} 未接続"}}


# 有効な agent（頭脳）値の allowlist（PUT /settings の検証・chat.turn 監査の正規化で共有する）。
# `heuristic`/`gemini`/`bedrock` は閉じた保存値で、履歴が自分の名前で残るよう置く（PUT では選べない）。
# ここに無い非空の値は実行側（`_select_provider`）では `_UnwiredProvider`、監査 detail では "unknown" にする。
AGENT_PROVIDERS = frozenset({"heuristic", "codex", "simple", "openai", "ollama", "gemini", "bedrock"})


# チャットでは閉じた頭脳。保存済みの値が残っていても別の AI へ黙って切り替えず、この文言で選び直しを案内する。
_CLOSED_AGENTS = {"gemini": "Gemini", "bedrock": "AWS Bedrock (Claude)", "heuristic": "簡易（AIなし）"}


class _DisabledProvider(Provider):
    """この環境では無効化されている頭脳（チャットで閉じた gemini/bedrock/heuristic）。
    黙って別の頭脳へ倒さず明示的に伝える。
    """

    def __init__(self, agent: str):
        self.label, self.model = "利用できないAI", ""
        self._agent = agent
        if agent in _CLOSED_AGENTS:
            self.howto = (f"{_CLOSED_AGENTS[agent]} はチャットでは利用できなくなりました。"
                          "設定画面で「簡易」または「Codex 調査」を選び直してください。")
        else:
            self.howto = ("この AI はこの環境では利用できません。設定画面で利用できる AI を選び直してください"
                          "（管理者が環境変数で有効化することもできます）。")

    def _plain_text(self, message: str = "") -> str:
        return self.howto

    def run(self, ctx: Ctx) -> Iterator[dict]:
        if not ctx.knowledge:
            yield from _plain_run(self, ctx); return
        env = {"lens": "qa", "headline": self.howto, "summary": {"total": 0}, "data": {}, "sources": [],
              "agentic_failure": "error",  # 終了理由の分布で完了扱いにしない
              "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world, lens="qa")}
        yield _node("disabled", "think", "利用できないAI", "設定を確認してください", "done")
        yield {"type": "answer_delta", "text": env["headline"]}
        yield {"type": "_result", "env": env,
               "decision": {"lens": "qa", "input": ctx.message, "reason": "選択中のAIは無効"}}


def _codex_openai_compat_block_reason(s: dict, *, explicit_openai_api_key: str | None = None,
                                      system_settings: dict | None = None) -> str | None:
    """Codex(OpenAI) 構成で、実際の接続先（`llm.openai_endpoint_kind()`）が既定以外（Azure OpenAI 等）のときに実行できない理由を返す（`None`＝実行できる）。
    `_select_provider` と `routers/system.py::settings_test` が共有する。
    `explicit_openai_api_key`（接続テスト専用）は未保存の入力キーで試すための override（保存も監査ログもしない）。モデル名は常に `model_catalog.resolve_model` の解決値を使う。
    判定順（早い者勝ち）: ① 接続先が既定なら対象外 ② サンドボックス無効なら拒否（fallback 経路は接続先リダイレクト未対応）③ base URL の妥当性（`llm.assert_openai_base_url_allowed`）④ 実キーが無ければ拒否 ⑤ `codex_model` が組み込み既定のままなら拒否（デプロイ名でなく既定名を送って 404 になるため）。
    `system_settings`（省略可）: 渡したスナップショットでキー・モデル両方を解決する（省略時は1回だけ読む）。
    """
    from sherpa import agent_constructs, llm, model_catalog
    from sherpa import store as _store
    from .codex.sandbox import _codex_sandbox_enabled
    # 接続先の判定を含め、この判定全体を1回のスナップショット（`sys_s`）だけで行う。
    sys_s = system_settings if system_settings is not None else _store.get_system_settings()
    # `openai_endpoint_kind()` は破損値で ValueError を送出しうる＝捕捉して「未接続」の理由として返す。
    try:
        eff_kind = llm.openai_endpoint_kind(sys_s)
    except ValueError:
        return "接続先の設定が不正です。管理者に確認してください"
    if eff_kind == "openai":
        return None
    if not _codex_sandbox_enabled():
        return "Azure OpenAI 等の接続先は Codex サンドボックス有効時のみ対応です"
    try:
        llm.assert_openai_base_url_allowed(llm.openai_base_url(sys_s))
    except ValueError:
        return "接続先 URL が不正です（https のみ）"
    from sherpa import keys as _keys
    # codex は strict=True で解決する（不正な cloud_provider のまま既定 openai へ倒れてキーを送らない）。接続テストの未保存キーは strict 判定を経由しない。
    if explicit_openai_api_key:
        openai_api_key = explicit_openai_api_key
    else:
        try:
            openai_api_key = _keys.resolve_api_key("openai", s, system_settings=sys_s, strict=True)
        except _keys.InvalidCloudProviderConfigError as e:
            return str(e)
    if not agent_constructs.is_real_api_key(openai_api_key):
        return f"{_keys.NO_CENTRAL_KEY_MESSAGE}（Azure 等の接続先の認証にも使います）"
    codex_model = model_catalog.resolve_model("codex", "codex", None, system_settings=sys_s)
    if not codex_model or codex_model == model_catalog.hardcoded_fallback("codex", "codex"):
        return ("管理画面の「使えるモデル」で Codex に接続先（Azure 等）のデプロイ名を登録してください"
                "（gpt-5.5 のままでは送信できません）")
    return None


def openai_direct_block_reason(key: str | None, system_settings: dict | None = None, *,
                               usage: str = "chat") -> str | None:
    """「OpenAI 直結」構成（Codex を介さず OpenAI へ直接送る全消費者共通）の送信前チェック。ブロック理由を返す。問題無ければ `None`。
    `key` は解決済みの値、`usage`（既定 "chat"）は実際に送信するモデルカタログの用途（呼び出し側が送信するのと同じ用途を渡す）。
    モデルはキー検証に通った後でこの関数内で解決する（キー未設定時にモデル解決を経由させない）。
    ① `key` が `is_real_api_key()` を満たさない（未設定・空白・プレースホルダ）なら拒否 ② 接続先が既定以外かつ `usage` のモデルが未解決/組み込み既定なら拒否 ③ 非文字列の破損値で `ValueError` なら未接続として拒否。
    新しい消費者もここを経由させる（迂回経路を作らない）。
    """
    from sherpa import agent_constructs, keys as _keys, llm, model_catalog
    if not agent_constructs.is_real_api_key(key):
        return _keys.NO_CENTRAL_KEY_MESSAGE
    model = model_catalog.resolve_model("openai", usage, None, system_settings=system_settings)
    try:
        eff_kind = llm.openai_endpoint_kind(system_settings)
    except ValueError:
        return "接続先の設定が不正です。管理者に確認してください"
    fallback = model_catalog.hardcoded_fallback("openai", usage)
    if eff_kind != "openai" and (not model or model == fallback):
        return ("管理画面の「使えるモデル」で OpenAI に接続先（Azure 等）のデプロイ名を登録してください"
                f"（{fallback} のままでは送信できません）")
    return None


def _codex_ollama_sandbox_disabled_reason() -> str | None:
    """Codex(Ollama) 構成の実行可否をサンドボックス側から判定する（`_select_provider` と `settings_test` が共有）。
    `SHERPA_CODEX_SANDBOX=0` では独自 model_provider を書けず、既定の `openai` へ黙って接続してしまうため honest failure にする。
    """
    from .codex.sandbox import _codex_sandbox_enabled
    if _codex_sandbox_enabled():
        return None
    return ("緊急時のサンドボックス無効モードでは Codex をローカルAIへ切り替えられません"
           "（このまま実行すると意図せず OpenAI に接続されます）。"
           "サンドボックスを有効に戻すか、頭脳の設定で「Ollama」単体を選んでください。")


def _select_simple(sys_s: dict) -> Provider:
    """簡易（検索して答える）の頭脳。使う AI は管理者設定「外部からの簡易回答に使う AI」。接続先の許可判定（SSRF）は `_resolve_llm` に集約。"""
    from sherpa import agents as _facade
    from sherpa import simple_chat
    try:
        llm_provider, llm_model, endpoint, headers = simple_chat._resolve_llm(sys_s)
    except simple_chat.LLMUnavailable as e:
        return _facade._UnwiredProvider("簡易（検索して答える）", str(e))
    return _facade.SimpleProvider(llm_provider, llm_model, endpoint, headers, system_settings=sys_s)


def _select_provider(s: dict, system_settings: dict | None = None) -> Provider:
    from sherpa import agent_constructs
    from sherpa import agents as _facade  # 実行時解決
    from sherpa import keys as _keys
    from sherpa import store as _store
    # この呼び出し内の system_settings 依存の解決はすべて同じスナップショットで行う（省略時のみここで読む）。
    sys_s = system_settings if system_settings is not None else _store.get_system_settings()
    # `effective_agent()` を経由する（表示と実行が食い違わない）。`strict=True` で非空の不正値は honest failure にする。
    # 保存済みの閉じた頭脳（gemini/bedrock/heuristic）は ollama 縮退より前に止める。
    saved = s.get("agent")
    if isinstance(saved, str) and saved.strip().lower() in _CLOSED_AGENTS:
        return _DisabledProvider(saved.strip().lower())
    try:
        agent = agent_constructs.effective_agent(s, system_settings=sys_s, strict=True)
    except (agent_constructs.InvalidAgentConfigError, _keys.InvalidCloudProviderConfigError) as e:
        return _facade._UnwiredProvider("AI の選択", str(e))
    # 未設定環境の既定が閉じた頭脳に落ちた場合も同じ案内で止める。
    if agent in _CLOSED_AGENTS or agent_constructs.runtime_blocked(agent):
        return _DisabledProvider(agent)
    if agent == "codex":
        # Codex CLI 不在は未接続として正直に返す。
        if not shutil.which("codex"):
            return _facade._UnwiredProvider(
                "Codex", "Codex CLI が見つかりません（閉域キットの tools/codex か npm で導入）")
        # Codex(Ollama) は Codex CLI を Ollama へ向ける。接続先は `ollama_url` 設定を使う。
        # 宛先ポリシー（loopback または admin allowlist）をここで検証し、不許可なら未接続として返す。
        ollama_base_url = None
        openai_api_key = None
        try:
            codex_provider_choice = agent_constructs.codex_model_provider(s)
        except agent_constructs.InvalidCodexModelProviderError as e:
            return _facade._UnwiredProvider("Codex", str(e))
        if codex_provider_choice == "ollama":
            reason = _codex_ollama_sandbox_disabled_reason()
            if reason is not None:
                return _facade._UnwiredProvider("Codex（ローカルLLM）", reason)
            from sherpa import llm
            ollama_base_url = _keys.resolve_ollama_url(s, system_settings=sys_s)
            try:
                llm.assert_ollama_url_allowed(ollama_base_url, system_settings=sys_s)
            except Exception:
                return _facade._UnwiredProvider(
                    "Codex（ローカルLLM）",
                    "設定のローカルAIの接続先が許可されていません。設定画面で確認してください")
        else:
            # Codex(OpenAI) 構成のときだけ、実際の接続先が既定以外へリダイレクトされていないかを見る。
            from sherpa import llm
            # Codex(OpenAI) は既定の接続先では auth.json で認証するため keys.py を通らない。
            # 廃止済み・不正な cloud_provider の保存値では OpenAI へ送らない。
            try:
                _keys.selected_cloud_provider(sys_s, strict=True)
            except _keys.InvalidCloudProviderConfigError as e:
                return _facade._UnwiredProvider("Codex（OpenAI）", str(e))
            # 起動時 env シードが未確定なら、Codex(OpenAI) を組み立てず未接続へ倒す。
            try:
                llm.assert_openai_io_allowed()
            except RuntimeError as e:
                return _facade._UnwiredProvider("Codex（OpenAI 互換の接続先）", str(e))
            reason = _codex_openai_compat_block_reason(s, system_settings=sys_s)
            if reason is not None:
                return _facade._UnwiredProvider("Codex（OpenAI 互換の接続先）", reason)
            if llm.openai_endpoint_kind(sys_s) != "openai":
                # Azure 等は env 変数からキーを読む。実キーが無い場合は上の `_codex_openai_compat_block_reason` が未接続を返す。
                try:
                    openai_api_key = _keys.resolve_api_key("openai", s, system_settings=sys_s, strict=True)
                except _keys.InvalidCloudProviderConfigError as e:
                    return _facade._UnwiredProvider("Codex（OpenAI 互換の接続先）", str(e))
        from sherpa import model_catalog
        codex_model = model_catalog.resolve_model("codex", "codex", None, system_settings=sys_s)
        # Codex(Ollama) で既定の OpenAI 向けの名前のままなら、実行前に理由を返す。
        if ollama_base_url is not None and (
                not codex_model or codex_model == model_catalog.hardcoded_fallback("codex", "codex")):
            return _facade._UnwiredProvider(
                "Codex（ローカルLLM）",
                "管理画面の「使えるモデル」で Codex の列に Ollama のモデル名（例 gpt-oss:20b）を登録してください"
                "（既定の OpenAI 向けの名前のままでは動きません）")
        # `InvalidModelNameError`（不正な非空モデル名）だけを拾い、`_UnwiredProvider` で正直に失敗を返す。
        # `reasoning` は個人設定を読まず管理画面の基準値（`depth_base_codex_reasoning`）/組み込み既定のみ。
        try:
            return _facade.CodexProvider(None, codex_model,
                                         s.get("codex_web_search"), ollama_base_url,
                                         openai_api_key=openai_api_key, system_settings=sys_s)
        except model_catalog.InvalidModelNameError as e:
            return _facade._UnwiredProvider("Codex", f"モデル名が不正です（{e}）")
    if agent == "simple":
        return _select_simple(sys_s)
    if agent in ("openai", "ollama"):
        # 保存済みの openai/ollama は簡易へ寄せる。
        return _select_simple(sys_s)
    return _facade._UnwiredProvider(
        "AI の選択", "選べる AI は「簡易」か「Codex 調査」です。設定画面で選び直してください。")


def get_provider(settings: dict | None = None, system_settings: dict | None = None) -> Provider:
    """利用者設定と管理者の全体設定（DB）で頭脳を選ぶ。固定の回答方針（`prompts.ANSWER_POLICY`）を provider に載せる。
    チャットの頭脳は codex / simple のみ。保存済みの openai/ollama は simple へ寄せ、gemini/bedrock/heuristic は `_DisabledProvider` で止める。
    1ターンの唯一の入口＝`system_settings` をここで `store._read_system_settings_fresh()`（共有キャッシュを使わない生の DB 読取）により1回だけ読み、`_select_provider` まで同じスナップショットを渡す。
    読取失敗は呼び出し元へ伝播する（fail-closed）。
    `system_settings`（省略可）: 渡した fresh スナップショットを使う。
    """
    from sherpa import store as _store
    s = settings or {}
    sys_s = system_settings if system_settings is not None else _store._read_system_settings_fresh()
    p = _select_provider(s, sys_s)
    from .prompts import ANSWER_POLICY
    p.system_prompt = ANSWER_POLICY
    return p


def provider_info(settings: dict | None = None) -> dict:
    """ヘッダのバッジ用: 使う頭脳の表示名・モデル。
    `agent` は `effective_agent()` 経由（実際の選択と食い違わない）。`system_settings` の fresh read はここで1回だけ行い、`get_provider()`・`effective_agent()` へ同じスナップショットを渡す。
    """
    from sherpa import agent_constructs, store as _store  # 循環回避のため実行時 import

    sys_s = _store._read_system_settings_fresh()
    p = get_provider(settings, system_settings=sys_s)
    return {"agent": agent_constructs.effective_agent(settings, system_settings=sys_s),
            "label": p.label, "model": p.model}
