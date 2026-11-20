"""管理者の全体設定のうち「既定の提供元」を選ぶ 2 項目（簡易回答の AI・Codex 調査の接続先）の検証と実効値。
設計: docs/design/settings.md
"""
from __future__ import annotations

from fastapi import HTTPException

from sherpa import agent_constructs, simple_chat

_INVALID = "(不正な保存値)"


def _options() -> dict[str, frozenset[str]]:
    return {"research_default_provider": simple_chat.RESEARCH_PROVIDERS,
            "codex_default_model_provider": agent_constructs.CODEX_MODEL_PROVIDERS}


def validate_choice(key: str, value, options) -> str | None:
    """None・空文字は未設定（None）。文字列以外・選べない値は 422。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, f"{key} は文字列で指定してください")
    v = value.strip().lower()
    if not v:
        return None
    if v not in options:
        raise HTTPException(422, f"{key} は {'/'.join(sorted(options))} のいずれかで指定してください")
    return v


def validated(provided: dict) -> dict:
    """PUT の `provided` のうちこの 2 項目だけを検証して `{キー: 保存値}` で返す。"""
    return {k: validate_choice(k, provided[k], opts) for k, opts in _options().items() if k in provided}


def research_info(sysset: dict) -> dict:
    """簡易回答の既定 AI。壊れた保存値があっても管理画面全体を 500 にしない。"""
    try:
        effective = simple_chat.default_research_provider(sysset)
    except ValueError:
        effective = _INVALID
    return {"configured": sysset.get("research_default_provider"), "effective": effective, "default": "ollama"}


def codex_info(sysset: dict) -> dict:
    """Codex 調査の既定の接続先。`effective` は未設定のときの自動選択込みの実効値。"""
    try:
        effective = agent_constructs.codex_model_provider({}, system_settings=sysset)
    except agent_constructs.InvalidCodexModelProviderError:
        effective = _INVALID
    return {"configured": sysset.get("codex_default_model_provider"), "effective": effective, "default": "auto"}
