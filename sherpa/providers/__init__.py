"""思考プロバイダの registry（頭脳選択）。`sherpa/providers/` パッケージのトップレベル。

`sherpa/agents.py`（旧・巨大モノリス）→ `sherpa/providers/` パッケージ化（リファクタリング計画
フェーズ5 S2〜S11・docs/proposals/2026-07-02-リファクタリング計画.md）の最終スライス（S11）で、
registry（`get_provider` / `provider_info` / `AGENT_PROVIDERS` / `_select_provider` /
`_UnwiredProvider`）がここに来た。ドメイン別モジュールは:

    base.py           Provider 抽象・Ctx・_gather・_plain_run 等の共通土台
    prompts.py        システムプロンプト・facts 整形
    heuristic.py      HeuristicProvider（既定・決定的頭脳）
    openai.py         OpenAIProvider
    ollama.py         OllamaProvider
    gemini.py         GeminiProvider
    bedrock.py        BedrockProvider・認証/redact/profile 補助
    codex/            CodexProvider（provider.py）・サンドボックス（sandbox.py）・MCP 連携（mcp.py）

`sherpa/agents.py` はこのパッケージ（と各サブモジュール）から re-export するだけの facade として
存続する（既存 import・monkeypatch 先は全て `sherpa.agents.X` のまま無改修で動く。詳細は
`sherpa/agents.py` の docstring 参照）。

**シーム規則（重要）**: `_select_provider` が組み立てる各 Provider クラス（`CodexProvider`/
`OpenAIProvider`/`OllamaProvider`/`GeminiProvider`/`BedrockProvider`/`_UnwiredProvider`）と
`_bedrock_auth_available` は、モジュールレベル import で束縛せず、呼び出し時に
`from sherpa import agents as _facade` して `_facade.X` 経由で解決する（store フェーズ4の
`_audit_insert`・base.py/heuristic.py の `_gather` と同じ方式。詳細は `sherpa/store/settings.py`・
`sherpa/providers/base.py` の docstring 参照）。理由: `tests/unit/test_health.py`・
`tests/unit/test_agents_seams.py` が `agents.BedrockProvider = Fake` のように facade 属性を
直接差し替えて挙動を検証しており、モジュールレベル import（ローカル束縛）にすると Python の
名前束縛はコピーのため facade 側の差し替えが素通りしてしまう。`from sherpa import agents as
_facade` を関数内（呼び出し時点）に置くのは、`sherpa/agents.py` が本パッケージを import する
過程ではまだ `sherpa.agents` が完成していない（循環 import）ため。

新規コードは `from sherpa.providers import get_provider` のような直 import を推奨する
（facade 経由の間接参照は既存呼び出し側・テスト互換のために維持している）。
"""
from __future__ import annotations

import shutil
from typing import Iterator

from .. import layer as layer_mod
from .base import Ctx, Provider, _node, _plain_run


class _UnwiredProvider(Provider):
    """未接続のLLMバックエンド（設定が要る）。**正直に**「未接続」と返す（嘘の回答をしない）。"""

    def __init__(self, name: str, howto: str):
        self.label, self.model, self.howto = name, "", howto

    def _plain_text(self, message: str = "") -> str:
        return f"{self.label} はまだ接続されていません。{self.howto}"

    def run(self, ctx: Ctx) -> Iterator[dict]:
        if not ctx.knowledge:                          # オフでも未接続を正直に（lens=chat で出典枠を出さない・RV Med）
            yield from _plain_run(self, ctx); return
        yield _node("connect", "think", f"{self.label} に接続", self.howto, "active")
        env = {"lens": "qa", "headline": f"{self.label} はまだ接続されていません。{self.howto}",
               "summary": {"total": 0}, "data": {}, "sources": [],
               "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world, lens="qa")}
        yield _node("connect", "think", f"{self.label} に接続", "未接続", "done")
        yield {"type": "answer_delta", "text": env["headline"]}
        yield {"type": "_result", "env": env,
               "decision": {"lens": "qa", "input": ctx.message, "reason": f"{self.label} 未接続"}}


# 有効な agent（頭脳）値の allowlist（単一の真実源。PUT /settings の検証・chat.turn 監査の正規化で
# 共有する）。ここに無い非空の値（env 誤記・旧データ等）は、実行の唯一の入口
# （`_select_provider`）では `agent_constructs.effective_agent(strict=True)` が honest failure
# （`_UnwiredProvider`）にする＝黙って heuristic（別の頭脳）へは縮退しない。監査 detail 側は
# 代わりに "unknown" と記録する（api.py 経由の任意文字列がそのまま検索可能な監査ログへ入るのを
# 防ぐ・監査は `effective_agent()` を非strictで呼ぶため、こちらは値を見るだけで実行は止めない）。
AGENT_PROVIDERS = frozenset({"heuristic", "codex", "openai", "ollama", "gemini", "bedrock"})


class _DisabledProvider(Provider):
    """この環境では無効化されている頭脳（`SHERPA_EXTRA_AGENTS` 未設定）。

    黙って別の頭脳へ倒さず**明示的に伝える**。設定に残った古い選択のまま、利用者が気付かないうちに
    別の AI が答えている状態を作らないため（決定 2026-08-15）。
    """

    def __init__(self, agent: str):
        self.label, self.model = "利用できないAI", ""
        self._agent = agent
        self.howto = ("この AI はこの環境では利用できません。設定画面で利用できる AI を選び直してください"
                      "（管理者が環境変数で有効化することもできます）。")

    def _plain_text(self, message: str = "") -> str:
        return self.howto

    def run(self, ctx: Ctx) -> Iterator[dict]:
        if not ctx.knowledge:
            yield from _plain_run(self, ctx); return
        env = {"lens": "qa", "headline": self.howto, "summary": {"total": 0}, "data": {}, "sources": [],
              "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world, lens="qa")}
        yield _node("disabled", "think", "利用できないAI", "設定を確認してください", "done")
        yield {"type": "answer_delta", "text": env["headline"]}
        yield {"type": "_result", "env": env,
               "decision": {"lens": "qa", "input": ctx.message, "reason": "選択中のAIは無効"}}


def _codex_openai_compat_block_reason(s: dict, *, explicit_openai_api_key: str | None = None,
                                      system_settings: dict | None = None) -> str | None:
    """Codex(OpenAI) 構成で、実際の接続先（`llm.openai_endpoint_kind()`）が既定(api.openai.com)以外
    （Azure OpenAI 等）のときに実行できない理由を返す（`None`＝問題なし＝実行できる）。

    `_select_provider`（本関数の直後）と `routers/system.py::settings_test`（接続テスト・
    POST /settings/test）が同じ判定を**共有**するための単一の真実源（重複実装しない）。

    `explicit_openai_api_key`（接続テスト専用）: 入力中の未保存キーで試せるようにする明示
    override。個人設定の永続的な上書きとは別物＝保存も監査ログも行わない（呼び出し元＝
    `settings_test` がリクエスト本文の値をそのまま渡す・DB へは書かない）。
    `sherpa.keys.resolve_api_key` は A6（`personal_api_keys_allowed`）が偽だと `s` に入れたキーを
    個人キーとして扱わず無視する（保存済みの個人/中央キーの解決に徹する仕様）ため、接続テストの
    「入力中のキーでこの場だけ試す」用途には別経路が要る。モデル名は個人設定・接続テストの
    どちらにも明示 override が無い＝常に `model_catalog.resolve_model` の解決値を使う
    （一般ユーザーが任意のモデル名を実 probe へ到達させられないようにするため）。

    判定順（早い者勝ち・全部揃わないと Codex(Azure等) は使えない）:
      1. 接続先が既定(openai)なら何もしない（そもそも Azure 判定の対象外・回帰ゼロ）。
      2. サンドボックス（`_codex_sandbox_enabled()`）が無効なら常に拒否する。fallback
         経路（`SHERPA_CODEX_SANDBOX=0`）は Azure 等への接続先リダイレクト自体が未対応
         （config.toml でなく `-c` 引数を使い、独自 model_provider を書けない）ため、そのまま
         実行すると Codex が既定の `openai` provider へ実キーを送ってしまいかねない
         （fail-closed・docs/08-実行権限と隔離.md §11 参照）。
      3. base URL 自体の妥当性（`llm.assert_openai_base_url_allowed`）を検証する
         （不正な URL が config.toml に書かれてキーが誤った宛先へ渡ることを防ぐ・迂回経路が
         あっても `sandbox._openai_compat_base_url()` が再検証する多層防御と対）。
      4. 実キー（`openai_api_key`）が無ければ拒否する（Azure 等は auth.json でなく env 変数から
         キーを読む設計のため）。
      5. `codex_model` の解決結果（`model_catalog.resolve_model` のカタログ既定→組み込み既定）が
         組み込み既定（`model_catalog.hardcoded_fallback("codex","codex")`＝`"gpt-5.5"`）のままなら
         拒否する。既定のまま Azure へ切り替えると、空チェックだけでは早期エラーにならず
         「デプロイ名でなく gpt-5.5 を送って 404」という気付きにくい失敗になる。判定対象は実際に
         送信される値＝管理者がカタログ既定を実際のデプロイ名へ変えていれば正しく動く。

    `system_settings`（省略可）: 呼び出し側が既に読んだスナップショットを渡すと、キー・モデル
    両方の解決をそれで行う（省略時はここで1回だけ読み、以後の両方の解決へ同じ値を渡す＝
    別々に省略値のまま渡すと呼び出し先ごとに個別に読み直され、この1回の判定の中で admin 更新が
    挟まった場合にキーとモデルが新旧混在しうる）。
    """
    from sherpa import agent_constructs, llm, model_catalog
    from sherpa import store as _store
    from .codex.sandbox import _codex_sandbox_enabled
    # 接続先の判定（openai_endpoint_kind/base_url）も含め、この判定全体を1回のスナップショット
    # （`sys_s`）だけで行う（省略時はここで1回だけ読む・`llm.openai_endpoint_kind()` を素で
    # 呼ぶと呼び出しのたびに個別 read が挟まりうるため）。
    sys_s = system_settings if system_settings is not None else _store.get_system_settings()
    # `sys_s` の openai_endpoint_kind/openai_base_url は JSONB のため非文字列の破損値もあり得る。
    # `openai_endpoint_kind()` は判定分岐より先に型検査する契約のため、破損時は
    # ValueError を送出しうる＝ここで捕捉し「未接続」の理由として返す（呼び出し元を未捕捉の
    # 例外で落とさない・本関数の戻り値契約＝None=OK／文字列=理由 にそのまま乗せる）。
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
    # 意図しないプロバイダ切替の是正: codex は A7（cloud_provider）の対象外だが、Azure 等への
    # リダイレクト時はここで central openai_api_key を実際に子プロセスへ渡すため、この解決だけは
    # strict=True にする（cloud_provider が非空の不正値でも黙って既定 openai へ倒れたキーを
    # 送らない）。`explicit_openai_api_key`（接続テストの未保存入力）は A7 の対象外の別経路
    # なので strict 判定を経由しない。
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
    """「OpenAI 直結」構成（Codex を介さず OpenAI へ直接送る全消費者共通）の送信前チェック。
    ブロック理由（未接続として表示する文言）を返す。問題無ければ `None`。

    `_select_provider`（このモジュール）と `usage_chat.py::_resolve_cfg` の openai 分岐が共有する
    唯一の preflight（重複実装を避ける・新しい消費者もここを呼ぶ）。`key` は呼び出し側が
    解決済みの値（`keys.resolve_api_key("openai", ...)`）をそのまま渡す。`usage`（既定 "chat"）は
    実際に送信するモデルカタログの用途——呼び出し側がこの後に送信するのと**同じ**用途を渡す
    契約（例: `sherpa/research_service.py` は下調べ検索で使う "subsearch" を渡す。用途を誤ると、
    デプロイ名を設定していない用途のセルが実送信されてしまう）。
    モデルは**キー検証に通った後で**この関数の内部で解決する（`model_catalog.resolve_model`
    引数に受け取らない）——キー未設定時にモデル解決を経由させない短絡順序を保つのが目的で、
    `system_settings.model_catalog` が破損した JSONB の環境でも、キーが無ければモデル解決を
    一切試みない（キー未設定＋カタログ破損の組み合わせで、本来 503 相当（未接続）になるべき所が
    カタログ解決の例外で 500 に化けることを防ぐ）。呼び出し側は `reason is None` を確認した後、
    実際に使うモデル値を改めて `model_catalog.resolve_model("openai", usage, ...)` で取得する
    （同じ純粋な辞書参照を二重に呼ぶだけで副作用は無い）。

    1. `key` が `agent_constructs.is_real_api_key()` を満たさない（未設定・空白のみ・
       `.env.example` のプレースホルダ `sk-REPLACE_ME` 等）なら拒否する（真偽値だけの判定は
       プレースホルダを「キーあり」と誤認し、実行時 401 まで気付けない）。
    2. 接続先（`llm.openai_endpoint_kind`）が既定(openai)以外（Azure 等）かつ `usage` のモデルが
       未解決/組み込み既定のままなら拒否する（Azure へ既定モデル名のまま送って404になる気付き
       にくい失敗を早期に防ぐ・Codex(OpenAI 互換) の同種チェックと揃える）。
    3. `system_settings` の openai_endpoint_kind/openai_base_url が非文字列の破損値の場合
       `llm.openai_endpoint_kind()` は `ValueError` を送出しうる＝未接続として拒否する。

    利用元を増やす時（本関数を呼ぶ消費者を追加する時）は、必ずこの一覧のどれかに該当する
    早期拒否を経由させること（新しい消費者だけ迂回できる別経路を作らない）。
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
    """Codex(Ollama) 構成の実行可否をサンドボックス側から判定する単一の真実源。`_select_provider`
    と `routers/system.py::settings_test`（接続テスト）が共有する（`_codex_openai_compat_block_reason`
    と同型）。

    `SHERPA_CODEX_SANDBOX=0`（緊急避難経路）は独自 model_provider の config.toml 書込に対応して
    いない（`-c` 引数のみ）ため、この経路のまま Codex(Ollama) を起動すると Codex CLI は既定の
    `openai` provider（実 home の auth.json 経由）へ黙って接続してしまう。honest failure にする
    （黙って別の課金プロバイダへ倒さない）。
    """
    from .codex.sandbox import _codex_sandbox_enabled
    if _codex_sandbox_enabled():
        return None
    return ("緊急時のサンドボックス無効モードでは Codex をローカルAIへ切り替えられません"
           "（このまま実行すると意図せず OpenAI に接続されます）。"
           "サンドボックスを有効に戻すか、頭脳の設定で「Ollama」単体を選んでください。")


def _select_provider(s: dict, system_settings: dict | None = None) -> Provider:
    from sherpa import agent_constructs
    from sherpa import agents as _facade   # 上記 docstring 参照: 実行時解決（monkeypatch シーム維持）
    from sherpa import keys as _keys
    from sherpa import store as _store
    # この呼び出し内の system_settings 依存の解決（A7・各キー・Bedrock 認証）はすべて同じ
    # スナップショットで行う（個別に読み直すと、1リクエストの処理中に admin 更新が挟まった場合、
    # どのプロバイダを選ぶかの判定とキー解決が新旧混在しうる）。`get_provider()` が入口で1回
    # 読んだスナップショットを渡す（省略時のみここで自分で読む・単体テスト互換）。
    sys_s = system_settings if system_settings is not None else _store.get_system_settings()
    # `effective_agent()`（A7 対応・保存済みだが選択中でないクラウド系 agent は ollama へ）を経由する
    # ＝表示（`agent_constructs.construct_id`）と実行がここで食い違わない単一の真実源。
    # `strict=True`: `SHERPA_AGENT`/`cloud_provider` の非空の不正値（env 誤記・旧データ等）を
    # 黙って自動選択/既定へ倒さない（本関数が実行の唯一の入口＝honest failure に変換する）。
    try:
        agent = agent_constructs.effective_agent(s, system_settings=sys_s, strict=True)
    except (agent_constructs.InvalidAgentConfigError, _keys.InvalidCloudProviderConfigError) as e:
        return _facade._UnwiredProvider("AI の選択", str(e))
    # env で有効化していない外部AI（gemini/bedrock）は既定へ倒さず明示エラーにする。
    # `heuristic` は未設定環境の安全網なので遮断しない（`agent_constructs.runtime_blocked` 参照）。
    if agent_constructs.runtime_blocked(agent):
        return _DisabledProvider(agent)
    if agent == "codex":
        # Codex CLI 不在は**未接続として正直に返す**（閉域実機・2026-08-18）: 以前は CodexProvider を組み立て、
        # provider.py の `shutil.which("codex")` 分岐が外れて決定的回答（定型文）が返り、利用者には
        # 「AI が答えていない」ことが分からなかった。OpenAI キー無しと同じ扱いにする。
        if not shutil.which("codex"):
            return _facade._UnwiredProvider(
                "Codex", "Codex CLI が見つかりません（閉域キットの tools/codex か npm で導入）")
        # 4構成（2026-08-15）: Codex(Ollama) は Codex CLI を Ollama へ向ける。接続先は Sherpa の
        # `ollama_url` 設定をそのまま使う（組み込みプロバイダは localhost 固定で設定が効かないため
        # 独自プロバイダを書く・`providers/codex/sandbox.py::_ollama_provider_lines` 参照）。
        # 宛先ポリシー（loopback または admin allowlist）はここで検証する＝不許可なら Codex を
        # 起動せず未接続として正直に返す（全シンク共通のチョークポイントを迂回しない）。
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
            # S2（Azure OpenAI 対応・2026-08-18）: Codex(OpenAI) 構成のときだけ、実際の接続先
            # （`sherpa.llm.openai_endpoint_kind()`・S1 実装）が既定(api.openai.com)以外へリダイレクト
            # されていないかを見る。既定のときは何もしない（＝この分岐に入らない・回帰ゼロ）。
            # LOW-1（2026-08-18 Codex RV）: S1 は着地済みなので `getattr(..., None)` 防御は撤去し、
            # 直接呼ぶ（判定ロジック自体は `_codex_openai_compat_block_reason` に切り出した・MED-4）。
            from sherpa import llm
            # 起動時 env シードが未確定（`llm.assert_openai_io_allowed` 参照）なら、kind が既定
            # "openai" に見えていても Codex(OpenAI) を組み立てず正直に未接続へ倒す（Codex(Ollama)
            # 分岐はこの手前で確定済みなので対象外）。
            try:
                llm.assert_openai_io_allowed()
            except RuntimeError as e:
                return _facade._UnwiredProvider("Codex（OpenAI 互換の接続先）", str(e))
            reason = _codex_openai_compat_block_reason(s, system_settings=sys_s)
            if reason is not None:
                return _facade._UnwiredProvider("Codex（OpenAI 互換の接続先）", reason)
            if llm.openai_endpoint_kind(sys_s) != "openai":
                # Azure 等は auth.json（ChatGPT ログイン）でなく env 変数からキーを読む設計
                # （`sandbox._openai_compat_provider_lines` の env_key）。実キーが無いまま起動して
                # 無出力失敗にする（T2 と同種の穴）代わりに、`_codex_openai_compat_block_reason` が
                # 上で正直に未接続を返す（実キー・デプロイ名・サンドボックス有効・base URL は検証済み）。
                # strict=True: `_codex_openai_compat_block_reason` が同じ system_settings で
                # 既に strict 検証済みだが、独立した再計算でも同じ契約を保つ（多層防御）。
                try:
                    openai_api_key = _keys.resolve_api_key("openai", s, system_settings=sys_s, strict=True)
                except _keys.InvalidCloudProviderConfigError as e:
                    return _facade._UnwiredProvider("Codex（OpenAI 互換の接続先）", str(e))
        from sherpa import model_catalog
        codex_model = model_catalog.resolve_model("codex", "codex", None, system_settings=sys_s)
        # カタログ外の値は縮退させない（`model_catalog.resolve_model` の契約）ため、`codex_model` は
        # 管理者のカタログ設定に不整合が生じた直後の一時的な値でありうる。
        # `CodexProvider.__init__` の `InvalidModelNameError`（不正な非空モデル名）だけを狭く拾い、
        # 想定外の構成でも honest failure として `_UnwiredProvider` で正直に失敗を返す
        # （`SHERPA_CODEX_TIMEOUT` の数値パース失敗等、無関係な `ValueError` は素通りさせて
        # 呼び出し元へ伝播させる＝誤って「モデル名が不正」と表示しない）。
        # `reasoning` は個人設定を読まない＝env（`SHERPA_CODEX_REASONING`）/組み込み既定のみ
        # （`_facade.CodexProvider.__init__` 参照）。
        try:
            return _facade.CodexProvider(None, codex_model,
                                         s.get("codex_web_search"), ollama_base_url,
                                         openai_api_key=openai_api_key, system_settings=sys_s)
        except model_catalog.InvalidModelNameError as e:
            return _facade._UnwiredProvider("Codex", f"モデル名が不正です（{e}）")
    if agent == "openai":
        from sherpa import keys as _keys, model_catalog
        key = _keys.resolve_api_key("openai", s, system_settings=sys_s)
        # 送信前 preflight（プレースホルダキー・Azure 既定モデル名等）は消費者間で共有する
        # （`openai_direct_block_reason` docstring 参照・usage_chat.py も同じ関数を呼ぶ）。
        # キー検証→モデル解決の順序を保つため、モデルはこの preflight を通過してから解決する
        # （キー未設定時にカタログ解決へ進ませない・破損カタログ環境でも未接続=503相当のまま
        # 保つ）。
        reason = openai_direct_block_reason(key, sys_s)
        if reason is not None:
            return _facade._UnwiredProvider("OpenAI API", reason)
        _model = model_catalog.resolve_model("openai", "chat", None, system_settings=sys_s)
        return _facade.OpenAIProvider(key, _model, system_settings=sys_s)
    if agent == "ollama":
        from sherpa import keys as _keys
        from sherpa import model_catalog
        # SC-6e: URL 解決に使ったのと同じ fresh sys_s を OllamaProvider にも渡す
        # （渡さないと `_agentic_target_check` 等の `llm.ollama_url()` が省略時 fallback で
        # DB を読み直し、URL 解決と allowlist 判定が別世代の設定を見うる）。
        return _facade.OllamaProvider(_keys.resolve_ollama_url(s, system_settings=sys_s),
                                      model_catalog.resolve_model("ollama", "chat", None,
                                                                  system_settings=sys_s),
                                      system_settings=sys_s)
    if agent == "gemini":
        from sherpa import keys as _keys
        from sherpa import model_catalog
        key = _keys.resolve_api_key("gemini", s, system_settings=sys_s)
        return _facade.GeminiProvider(
            key, model_catalog.resolve_model("gemini", "chat", None, system_settings=sys_s)) \
            if key else _facade._UnwiredProvider("Gemini", _keys.NO_CENTRAL_KEY_MESSAGE)
    if agent == "bedrock":
        from sherpa import keys as _keys
        # A7（クラウドプロバイダ排他選択）: bedrock が選ばれていなければ、SigV4/AWS 認証情報ファイル等の
        # 静的な手掛かりが端末にあっても使わない（`_bedrock_auth_available` の SigV4 ヒントは「選択済み」
        # の場合のみ意味を持たせる＝非選択プロバイダの温存キー/認証情報を黙って使わないという契約を
        # `_bedrock_auth_available` 単体の判定に任せず、ここで明示的にゲートする）。
        if _keys.selected_cloud_provider(sys_s) != "bedrock":
            return _facade._UnwiredProvider(
                "AWS Bedrock (Claude)", _keys.NO_CENTRAL_KEY_MESSAGE)
        api_key = _keys.resolve_api_key("bedrock", s, system_settings=sys_s)
        if _facade._bedrock_auth_available(api_key):
            # region は常に東京固定（`_bedrock_region` 参照）。利用者設定からは読まない。
            return _facade.BedrockProvider(None, s.get("bedrock_model"), api_key)
        return _facade._UnwiredProvider("AWS Bedrock (Claude)", _keys.NO_CENTRAL_KEY_MESSAGE)
    return _facade.HeuristicProvider()


def get_provider(settings: dict | None = None, system_settings: dict | None = None) -> Provider:
    """ユーザ設定（DB・`user_settings`）と管理者の全体設定（DB・`system_settings`）で頭脳を選ぶ
    （env は初回起動時のシードのみで実行時には読まない・「設定の所有原則」参照）。UI/プロトコルは
    provider に依存しない。

    回答方針（system プロンプト・#2）を provider に載せる（LLM 系は system メッセージに前置）。

    検索アシスタント（`sherpa/search_helper.py`）: agent=openai
    （`provider_id == "openai"`）かつ per-user `search_helper` が解決できた時だけ `p._sub` を設定する
    （ハイブリッド有効化のゲート）。他頭脳（heuristic/codex/gemini/bedrock/ollama）と OpenAI の
    `_UnwiredProvider`（provider_id ''）は `provider_id` ゲートで影響を受けない。解決失敗（未設定／
    鍵未設定）は例外にせず `resolve` が None を返す＝OFF 縮退（`p._sub` は `Provider` のクラス属性
    `None` のまま）。

    本関数が1ターンの唯一の入口＝`system_settings` をここで1回だけ読み、メインプロバイダの選択
    （`_select_provider`）・検索アシスタント（`search_helper.resolve`）まで同じスナップショットを
    渡す（個別に読み直すと、1ターンの処理中に admin 保存が挟まった場合、メインと検索アシスタントが
    新旧混在の接続先/鍵で動きうる）。

    WEB-1: この唯一の読取点は `store._read_system_settings_fresh()` を直接呼ぶ——共有キャッシュを
    一切参照・更新しない生の DB 読取（`get_system_settings` 自体のシグネチャは変えない・多数の
    呼び出し元が `lambda: {...}` の形で丸ごと monkeypatch しているため引数追加は互換性を壊す）。
    「`_invalidate_system_settings_cache()` を呼んでから `get_system_settings()` を呼ぶ」方式だと、
    並行ターン（`/chat` の threadpool・`/chat/turns` の background thread）が invalidate 直後に
    キャッシュを再加熱し、その後 DB が落ちても再加熱された値を読んでしまう TOCTOU が残る——
    共有キャッシュに一切触れないこの関数なら他スレッドのキャッシュ状態に依らない。DB 停止直後は
    キャッシュ済みの許可設定（`web_search_allowed` 等）が生き残る fail-open 窓があり、外部実行
    （Codex 等）へ踏み切る直前のこの1点だけ塞ぐ（キャッシュ機構自体・他の呼び出し元は変更しない）。
    読取失敗はそのまま例外として呼び出し元（`chat_service.handle_message`/`stream_message`）へ
    伝播する＝fail-closed（このターンの provider を組み立てず終える）。

    `system_settings`（省略可）: 呼び出し元が既に読んだ fresh スナップショットをそのまま使う
    （省略時のみこの関数自身が `_read_system_settings_fresh()` を読む）。`provider_info()` が
    `effective_agent()` と同一世代の設定を共有するために渡す——省略して両者が別々に読むと、
    その間に admin 保存が挟まった場合 `agent`（`effective_agent` 側）と `label`/`model`
    （こちら側）が別世代の設定を反映しうる。
    """
    from sherpa import store as _store
    s = settings or {}
    sys_s = system_settings if system_settings is not None else _store._read_system_settings_fresh()
    p = _select_provider(s, sys_s)
    p.system_prompt = (s.get("system_prompt") or "").strip()
    if getattr(p, "provider_id", "") == "openai":
        from .. import search_helper as _sh
        # 検索アシスタント（`sherpa/search_helper.py`）: 利用者ごとの1設定から組み立てる。非空の
        # 不正値（未知の選択肢・解決先の管理者モデル破損等）は黙って OFF 縮退させず、
        # `p._search_helper_error` に理由を残す（`run()` が honest failure として停止する＝
        # メインAIの高コスト経路を利用者の承認前に黙って開始しない）。
        try:
            helper = _sh.resolve(s, system_settings=sys_s)
        except _sh.InvalidSearchHelperConfigError as e:
            p._search_helper_error = str(e)
        else:
            if helper is not None:
                p._sub = helper
    return p


def provider_info(settings: dict | None = None) -> dict:
    """ヘッダのバッジ用: 使う頭脳の表示名・モデル。

    `agent` は `effective_agent()` 経由（A7 で選択中でないクラウド系 agent は ollama 扱いに統一）＝
    実際に `get_provider` が選ぶプロバイダとバッジ表示が食い違わない（保存済み設定が bedrock でも
    A7 が openai を選択中なら、バッジも実行も ollama を示す）。

    WEB-1: `system_settings` の fresh read はここで1回だけ行い、`get_provider()`・
    `effective_agent()` の両方へ同じスナップショットを渡す——それぞれが別々に fresh read すると、
    1リクエストの処理中に admin 保存が挟まった場合、`agent`（`effective_agent` 側）と
    `label`/`model`（`get_provider` 側）が別世代の設定を反映しうる（`get_provider` 自体の
    docstring にある「1ターン唯一の読取点」原則をこの関数でも保つ）。
    """
    from sherpa import agent_constructs, store as _store   # 循環回避のため実行時 import（_select_provider と同じ）

    sys_s = _store._read_system_settings_fresh()
    p = get_provider(settings, system_settings=sys_s)
    return {"agent": agent_constructs.effective_agent(settings, system_settings=sys_s),
            "label": p.label, "model": p.model}
