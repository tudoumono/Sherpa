"""AI の用途ごと（回答の AI・Codex・埋め込み）の接続先・モデル・提供元の解決と、Ollama のモデルが取得済みかの見方。
軽い確認（`health`）・実際に送る確認（`connectivity`）・管理画面と個人設定の接続テストが同じ部品を呼ぶ。
設計: docs/design/operations.md「システム状態とつながりの確認」
"""
from __future__ import annotations

import json
import os
from typing import NamedTuple

from . import keys, llm, model_catalog


class Target(NamedTuple):
    """用途が実際に使う提供元・モデル・Ollama の接続先（Ollama のときだけ。OpenAI は None）。`cfg` は埋め込みの解決結果。"""
    provider: str
    model: str | None
    base: str | None
    cfg: dict | None = None


def installed_names(tags) -> set[str]:
    """Ollama の `/api/tags` の応答から、取得済みのモデル名の集合。"""
    models = tags.get("models") if isinstance(tags, dict) else None
    if not isinstance(models, list):
        return set()
    return {m["name"] for m in models if isinstance(m, dict) and isinstance(m.get("name"), str) and m["name"]}


def model_installed(model: str | None, names) -> bool:
    """`model` が取得済みか。タグ無しは `:latest` とみなす。"""
    if not model:
        return True
    return model in names or (":" not in model and f"{model}:latest" in names)


def missing_models(models, names) -> list[str]:
    """`models` のうち取得済みでないもの（昇順）。"""
    return sorted({m for m in models if m and not model_installed(m, names)})


def fetch_tags(base: str, timeout: float, *, system_settings: dict | None = None) -> dict:
    """`base` の Ollama の `/api/tags` を読む（宛先の許可を通し、リダイレクトは追わない）。"""
    with llm.urlopen_no_redirect(llm.ollama_url(base, "/api/tags", system_settings=system_settings),
                                 timeout=timeout) as r:
        return json.loads(r.read())


def answer_target(system_settings: dict) -> Target:
    """回答の AI（簡易の回答）。提供元とモデルは `simple_chat.resolve_model_and_provider`、Ollama の接続先は中央の接続先。"""
    from . import simple_chat
    provider, model = simple_chat.resolve_model_and_provider(None, system_settings)
    base = keys.resolve_ollama_url(None, system_settings=system_settings) if provider == "ollama" else None
    return Target(provider, model, base)


def codex_target(settings: dict | None, system_settings: dict) -> Target:
    """Codex 調査。提供元は `agent_constructs.codex_model_provider` の実効値（`settings` は確認を開いた利用者の設定）。"""
    from . import agent_constructs
    provider = agent_constructs.codex_model_provider(settings, system_settings=system_settings)
    model = model_catalog.resolve_model("codex", "codex", None, system_settings=system_settings)
    base = keys.resolve_ollama_url(settings, system_settings=system_settings) if provider == "ollama" else None
    return Target(provider, model, base)


def embed_target(system_settings: dict, *, probe: bool = False) -> Target | None:
    """埋め込み。使わない（`SHERPA_DISABLE_EMBED`・未選択）は None。Ollama の接続先は埋め込み用の接続先。解決できなければ例外。"""
    from . import embeddings
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        return None
    ec = embeddings.cfg(None, system_settings=system_settings, probe=probe)
    if ec is None:
        return None
    provider = ec.get("provider")
    base = keys.resolve_ollama_url(None, system_settings=system_settings, purpose="embed") if provider == "ollama" else None
    return Target(provider, ec.get("model"), base, ec)
