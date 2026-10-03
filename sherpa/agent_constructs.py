"""チャットで選べる「実行構成」（どのオーケストレータで、どのモデルを使うか）の単一の真実源。
設計: docs/design/chat.md「2つの頭脳と、頭脳の選び方」

標準の構成は 3 つ:
    simple        簡易。資料を数回検索して手早く答える（使う AI は管理者設定の `research_default_provider`）
    codex_openai  Codex CLI が段取りとツール実行を行い、実行モデルが OpenAI
    codex_ollama  同上で実行モデルが Ollama

保存値 `agent` が `openai`/`ollama` の利用者は読み取り時に `simple` として扱う（DB 行は書き換えない・`effective_agent`）。
`gemini` / `bedrock` / `heuristic` はチャットで閉じており、選べない（`_select_provider` が選び直しを案内する）。
Codex 構成だけが `codex_model_provider` を持ち、Codex CLI の接続先モデルを決める。
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

_log = logging.getLogger("sherpa")

# 標準の構成。`id` は UI/API の識別子、`agent`/`codex_model_provider` は保存される設定値。
CONSTRUCTS: tuple[dict[str, Any], ...] = (
    {"id": "codex_openai", "agent": "codex", "codex_model_provider": "openai",
     "label": "Codex 調査（OpenAI）", "hint": "Codex が自分で資料を探して調べる・モデルは OpenAI"},
    {"id": "codex_ollama", "agent": "codex", "codex_model_provider": "ollama",
     "label": "Codex 調査（Ollama）", "hint": "Codex が自分で資料を探して調べる・モデルは Ollama"},
    {"id": "simple", "agent": "simple", "codex_model_provider": None,
     "label": "簡易（検索して答える）", "hint": "資料を数回検索して手早く答える・網羅性が要る質問は Codex 調査へ"},
)

# 標準構成が使う頭脳（env に関わらず常に有効）。
STANDARD_AGENTS = frozenset({"codex", "simple"})
# 旧・直結経路の保存値/env 値。読み取り時に `simple` へ読み替える（DB は書き換えない）。
LEGACY_AGENTS = frozenset({"openai", "ollama"})
# 標準構成に加えて選べる追加頭脳（現在は空）。
EXTRA_AGENTS: frozenset[str] = frozenset()

# 追加頭脳を選んだときの表示（設定画面・チャットの頭脳バッジ共通）。
_EXTRA_LABELS: dict[str, tuple[str, str]] = {}

# Codex 構成が接続できるモデル提供元。
CODEX_MODEL_PROVIDERS = frozenset({"openai", "ollama"})

# 何も設定されていないときの構成（既定は Codex(OpenAI)）。既定の唯一の真実源。
DEFAULT_CONSTRUCT_ID = "codex_openai"
DEFAULT_AGENT = "codex"


# `.env.example` のプレースホルダ値。無編集コピーを「キーあり」と誤認させない（`scripts/run-common.sh` の判定と揃える）。
_PLACEHOLDER_API_KEY_VALUES = frozenset({"sk-REPLACE_ME", "REPLACE_ME"})


def is_real_api_key(value: str | None) -> bool:
    """`.env.example` のプレースホルダ・空白のみ・文字列でない値を「キー未設定」として扱う（fail-closed）。"""
    if not isinstance(value, str):
        return False
    v = value.strip()
    return bool(v) and v not in _PLACEHOLDER_API_KEY_VALUES


def _codex_auth_available(system_settings: dict | None = None) -> bool:
    """Codex CLI が使える認証を持っているか。
    解決済みの OpenAI キー（`keys.resolve_api_key` 経由）または `~/.codex/auth.json` の存在（`CODEX_HOME` を尊重）。中身の検証はしない。
    `system_settings` を渡すとキー解決に使う（省略時は自分で読む）。
    """
    from sherpa import keys
    if is_real_api_key(keys.resolve_api_key("openai", None, system_settings=system_settings)):
        return True
    codex_home = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    return (codex_home / "auth.json").exists()


def _auto_default_agent(system_settings: dict | None = None) -> str:
    """個人設定が未選択のときに、この環境で使える頭脳を選ぶ。
    Codex CLI が PATH にあり使える認証がある（`_codex_auth_available`）→ codex、無ければ simple。
    """
    from sherpa import required_tools
    if shutil.which("codex") and _codex_auth_available(system_settings) and not required_tools.codex_cli_missing():
        return "codex"
    return "simple"


class InvalidAgentConfigError(ValueError):
    """保存済み `agent` の値が不正なとき、`effective_agent(strict=True)` が送出する。"""


def default_agent(system_settings: dict | None = None) -> str:
    """個人設定が未選択のときに使う頭脳。この環境で使える頭脳を自動選択する（`_auto_default_agent`）。`enabled_agents()` に含まれる頭脳を返す。
    `system_settings` は `_auto_default_agent()` へ渡す。
    """
    value = _auto_default_agent(system_settings)
    return value if value in enabled_agents() else DEFAULT_AGENT


def enabled_extra_agents() -> frozenset[str]:
    """選べる追加頭脳（現在は空）。"""
    return EXTRA_AGENTS


def enabled_agents() -> frozenset[str]:
    """現在の環境で選べる頭脳（標準 2 ＋有効化した追加分）。"""
    return STANDARD_AGENTS | enabled_extra_agents()


def agent_enabled(agent: str | None) -> bool:
    return bool(agent) and agent in enabled_agents()


# 実行時に遮断する頭脳（チャットで閉じている外部 AI）。
_RUNTIME_BLOCKABLE = frozenset({"gemini", "bedrock"})


def runtime_blocked(agent: str | None) -> bool:
    """この環境では実行させない頭脳か。"""
    name = (agent or "").lower()
    return name in _RUNTIME_BLOCKABLE and name not in enabled_extra_agents()


def effective_agent(settings: dict | None, *, system_settings: dict | None = None,
                    strict: bool = False) -> str:
    """実行（`_select_provider`）と表示（`construct_id`）が共通で経由する実効の頭脳。
    保存済み `agent` が旧・直結経路の値なら `simple` へ読み替える。`codex` は対象外。閉じた頭脳はそのまま返す。
    `system_settings` を渡すと agent 未設定時の既定選択に使う。
    `strict=True` は保存済み `agent`／`cloud_provider` の非空の不正値で例外
    （`InvalidAgentConfigError`／`keys.InvalidCloudProviderConfigError`）を送出する。実行の入口だけが使い、表示/監査は既定 False。
    """
    from sherpa import store
    s = settings or {}
    raw = s.get("agent")
    # 文字列以外の非 None（設定破損）は truthiness 判定の前に拒否する。
    if raw is not None and not isinstance(raw, str):
        if strict:
            raise InvalidAgentConfigError(
                f"agent の値が不正です（{raw!r}）。"
                f"選べる値: {', '.join(sorted(STANDARD_AGENTS | EXTRA_AGENTS))}。"
                "設定画面で選び直してください。")
        raw = ""
    raw_agent = str(raw or "").strip().lower()
    if raw_agent in LEGACY_AGENTS:
        return "simple"
    if not raw_agent:
        sys_s = system_settings if system_settings is not None else store.get_system_settings()
        return default_agent(sys_s)
    else:
        # 既知の頭脳名なら有効化していないだけの正当な経路としてそのまま返す。既知でない非空の値は strict 時だけ例外にする。
        if strict and raw_agent not in (STANDARD_AGENTS | EXTRA_AGENTS):
            raise InvalidAgentConfigError(
                f"agent の値が不正です（{raw_agent!r}）。"
                f"選べる値: {', '.join(sorted(STANDARD_AGENTS | EXTRA_AGENTS))}。"
                "設定画面で選び直してください。")
        return raw_agent


def available_constructs(system_settings: dict | None = None) -> list[dict[str, Any]]:
    """画面に出す実行構成の一覧（標準 3 ＋有効化した追加頭脳）。追加頭脳は `codex_model_provider=None` の 1 件として並べる。
    保存済みの構成が一覧から消えても `construct_id()` は値を返す。`system_settings` は署名互換のため受け取る（未使用）。
    """
    from sherpa import required_tools
    codex_ready = not required_tools.codex_cli_missing()  # codex 本体が無ければ選ばせない
    out = [dict(c) for c in CONSTRUCTS if not (c["agent"] == "codex" and not codex_ready)]
    for name in sorted(enabled_extra_agents()):
        label, hint = _EXTRA_LABELS[name]
        out.append({"id": name, "agent": name, "codex_model_provider": None, "label": label, "hint": hint})
    return out


def construct_id(settings: dict | None, *, system_settings: dict | None = None) -> str:
    """保存済み設定から現在の構成 id を求める（該当が無ければ agent 名のまま）。`effective_agent()` 経由で実行と表示を一致させる。
    `system_settings` は `effective_agent()` へ渡す。
    """
    s = settings or {}
    agent = effective_agent(s, system_settings=system_settings)
    if agent == "codex":
        # `codex_model_provider()` と同じく strip+lowercase して比較する。
        raw = s.get("codex_model_provider")
        if raw is None or raw == "":
            provider = ""
        elif not isinstance(raw, str):
            # 文字列以外の非 None（設定破損）は、実行時の失敗と表示を揃えるため一覧に無い id を返す。
            return "codex_invalid"
        else:
            provider = raw.strip().lower()
        if not provider or provider == "openai":
            return "codex_openai"
        if provider == "ollama":
            return "codex_ollama"
        # 非空の不正値は一覧に無い id（`codex_invalid`）を返し、実行時の失敗と表示を揃える。
        return "codex_invalid"
    for c in CONSTRUCTS:
        if c["agent"] == agent:
            return c["id"]
    return agent


class InvalidCodexModelProviderError(ValueError):
    """`codex_model_provider` が非空の不正値のとき `codex_model_provider()` が送出する。"""


def codex_model_provider(settings: dict | None) -> str:
    """Codex CLI が接続するモデル提供元（未設定は既定 openai）。
    非空の不正値は `InvalidCodexModelProviderError`（黙って openai へ倒さない）。呼び出し元は `_select_provider`。
    """
    raw = (settings or {}).get("codex_model_provider")
    # 文字列以外の非 None（設定破損）は truthiness 判定の前に拒否する。
    if raw is not None and not isinstance(raw, str):
        raise InvalidCodexModelProviderError(
            f"codex_model_provider の値が不正です（{raw!r}）。"
            f"選べる値: {', '.join(sorted(CODEX_MODEL_PROVIDERS))}。設定画面で選び直してください。")
    value = str(raw or "").strip().lower()
    if not value:
        return "openai"
    if value not in CODEX_MODEL_PROVIDERS:
        raise InvalidCodexModelProviderError(
            f"codex_model_provider の値が不正です（{value!r}）。"
            f"選べる値: {', '.join(sorted(CODEX_MODEL_PROVIDERS))}。設定画面で選び直してください。")
    return value


def is_local(provider_id: str | None, *, codex_model_provider: str | None = None,
            system_settings: dict | None = None) -> str | None:
    """`provider_id` の配置区分を返す。画面の担当バッジの唯一の真実源（フロントは推測しない）。
    `"local"`（Ollama）／`"on_prem"`（LAN 内の OpenAI 互換）／`"cloud"`（OpenAI 本家・Azure・Gemini・Bedrock）／
    `"cloud_compat"`（外部の OpenAI 互換クラウド）。判定不能は `None`（決め打たない）。
    - `"ollama"` → local。`"gemini"`/`"bedrock"` → cloud。`"openai"` → `_openai_compat_locality`。
    - `"codex"` → `codex_model_provider` が `"ollama"` なら local、それ以外/省略は `_openai_compat_locality`。
    - 未知の値 → None。
    `system_settings` は `_openai_compat_locality` へ渡す。
    """
    name = (provider_id or "").strip().lower()
    if name == "ollama":
        return "local"
    if name == "openai":
        return _openai_compat_locality(system_settings)
    if name in ("gemini", "bedrock"):
        return "cloud"
    if name == "codex":
        if (codex_model_provider or "").strip().lower() == "ollama":
            return "local"
        return _openai_compat_locality(system_settings)
    return None


def _openai_compat_locality(system_settings: dict | None) -> str:
    """`openai_endpoint_kind() != "custom"`（本家/Azure）は `"cloud"`。`"custom"` は host が私有/ローカル範囲かで
    `"on_prem"`／`"cloud_compat"` に分ける（判定は `llm.endpoint_locality()`）。
    """
    from . import llm
    if llm.openai_endpoint_kind(system_settings) != "custom":
        return "cloud"
    locality = llm.endpoint_locality(llm.openai_base_url(system_settings))
    return locality if locality == "on_prem" else "cloud_compat"
