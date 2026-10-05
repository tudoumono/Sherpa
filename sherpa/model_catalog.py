"""モデルカタログ（プロバイダ×用途ごとの選べるモデル一覧と既定）。管理者が管理画面で編集する。
設計: docs/design/settings.md「使えるモデル」

解決順: 利用者の選択（カタログ内）→ カタログ既定 → 組み込み既定（このモジュールの値）。
"""
from __future__ import annotations

import copy
import logging
import os
import re

_log = logging.getLogger("sherpa")

PROVIDERS = ("openai", "ollama", "codex")
USAGES = ("chat", "intent", "embed", "subsearch", "codex", "render")

# `render`（rag.md の LLM 成形）が未設定なら、`resolve_model` は旧 `extract` セルの解決結果を読む（読み取りのみの後方互換）。
# `_DEFAULT_CATALOG` の `extract` キーは静的値を置かない。
_USAGE_FALLBACK = {"render": "extract"}

# モデル名の文法（provider 共通の下限・1〜128 文字）。個人設定の入力欄（`routers/system.py::_MODEL_NAME_RE`）と同じパターン。
# Codex は argv `-m` に渡すため追加で 64 文字の上限がある。`:` は Ollama のタグ（gpt-oss:20b 等）に要る。
# `validate_catalog` が両方を強制し、管理者が保存できる値が各消費者でそのまま使えるようにする。
MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\-]{0,127}")
CODEX_MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\-]{0,63}")


class InvalidModelNameError(ValueError):
    """不正な非空モデル名。`ValueError` を継承する（モデル名起因だけを狭く捕捉したい呼び出し側向け）。"""


def _model_name_re_for(provider: str):
    """`provider` が実際に受け付けるモデル名パターン。"""
    return CODEX_MODEL_NAME_RE if provider == "codex" else MODEL_NAME_RE

# 組み込み既定（カタログ未設定・DB 不達時の最終フォールバック）。値はカタログ導入前のハードコード既定と同じ。
# `"extract"` キーは `USAGES` に含まれず、`_USAGE_FALLBACK`（render→extract）の既定として残す。
_DEFAULT_CATALOG: dict[str, dict[str, dict[str, object]]] = {
    "openai": {
        "chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.5"},
        "extract": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.5"},
        "intent": {"allowed": ["gpt-4o-mini", "gpt-5.4-mini"], "default": "gpt-4o-mini"},
        "embed": {"allowed": ["text-embedding-3-small", "text-embedding-3-large"],
                  "default": "text-embedding-3-small"},
        "subsearch": {"allowed": ["gpt-5.4-mini", "gpt-4o-mini"], "default": "gpt-5.4-mini"},
    },
    "ollama": {
        "chat": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
        "extract": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
        "intent": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
        "subsearch": {"allowed": ["qwen2.5"], "default": "qwen2.5"},
        # 埋め込みの次元は固定（768）。allowed は次元互換のモデルだけを登録する。
        "embed": {"allowed": ["nomic-embed-text"], "default": "nomic-embed-text"},
        # 空セルを置く。組み込み既定を空にして `resolve_model()` を `_USAGE_FALLBACK` へ倒すため、ここに既定値を入れない。
        "render": {"allowed": [], "default": ""},
    },
    "codex": {
        "codex": {"allowed": ["gpt-5.5"], "default": "gpt-5.5"},
    },
}

_CATALOG_KEY = "model_catalog"
# env→system_settings の「一度だけ」シード（`store.seed_system_settings_once`）。
_CATALOG_SEED_MARKER_KEY = "model_catalog_seed_version"
_CATALOG_SEED_VERSION = 1


def hardcoded_fallback(provider: str, usage: str) -> str:
    """カタログにも管理者設定にも無いときの最終フォールバック（組み込み既定）。"""
    return str(_DEFAULT_CATALOG.get(provider, {}).get(usage, {}).get("default") or "")


def get_catalog(system_settings: dict | None = None) -> dict:
    """有効なカタログ（組み込み既定 ∪ 管理者設定・セルごとに管理者設定を優先）。DB 不達は組み込み既定のみ。
    `system_settings` を渡すと読み直さない。
    """
    base = copy.deepcopy(_DEFAULT_CATALOG)
    try:
        if system_settings is not None:
            configured = system_settings.get(_CATALOG_KEY) or {}
        else:
            from sherpa import store
            configured = store.get_system_settings().get(_CATALOG_KEY) or {}
    except Exception:
        configured = {}
    if not isinstance(configured, dict):
        return base
    for provider, usages in configured.items():
        if not isinstance(usages, dict):
            continue
        base.setdefault(provider, {})
        for usage, cell in usages.items():
            if not isinstance(cell, dict):
                continue
            base[provider][usage] = {
                "allowed": [str(m) for m in (cell.get("allowed") or []) if isinstance(m, str)],
                "default": str(cell.get("default") or ""),
            }
    return base


def catalog_entry(provider: str, usage: str, system_settings: dict | None = None) -> dict:
    """`{provider}/{usage}` の 1 セル（`{"allowed": [...], "default": "..."}`・無ければ空）。"""
    return get_catalog(system_settings).get(provider, {}).get(usage) or {"allowed": [], "default": ""}


def resolve_model(provider: str, usage: str, user_settings: dict | None,
                  system_settings: dict | None = None) -> str:
    """モデル名を解決する。カタログ既定 → 組み込み既定 →（`_USAGE_FALLBACK` にあれば）別用途で再解決、の順。
    `user_settings` は読まない（個人の自由入力でカタログ外のモデル名を外部 API へ届かせない・予約引数）。
    """
    entry = catalog_entry(provider, usage, system_settings)
    value = str(entry.get("default") or "") or hardcoded_fallback(provider, usage)
    if value:
        return value
    fallback_usage = _USAGE_FALLBACK.get(usage)
    if fallback_usage:
        return resolve_model(provider, fallback_usage, user_settings, system_settings)
    return value


def is_valid_model(provider: str, usage: str, value: str, system_settings: dict | None = None) -> bool:
    """`value` がこの 1 セルで許可されるか（allowed に含まれる／セルの既定と一致）"""
    entry = catalog_entry(provider, usage, system_settings)
    allowed = set(entry.get("allowed") or [])
    default = str(entry.get("default") or "")
    return value in allowed or (bool(default) and value == default)


def validate_catalog(value) -> dict | None:
    """管理画面からの `model_catalog` 保存値を検証する（`None` は未設定へ戻す）。
    形式: `{provider: {usage: {"allowed": [str,...], "default": str}}}`。未知の provider/usage は拒否する。`default` は非空なら allowed の先頭へ足す。不正な形は `ValueError`。
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("model_catalog はオブジェクトで指定してください")
    out: dict = {}
    for provider, usages in value.items():
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("model_catalog のプロバイダ名は空でない文字列で指定してください")
        if provider not in PROVIDERS:
            raise ValueError(
                f"model_catalog の未知/対象外のプロバイダです: {provider}"
                f"（利用可能: {', '.join(PROVIDERS)}）")
        if not isinstance(usages, dict):
            raise ValueError(f"model_catalog[{provider}] はオブジェクトで指定してください")
        out_usages: dict = {}
        for usage, cell in usages.items():
            if not isinstance(usage, str) or not usage.strip():
                raise ValueError("model_catalog の用途名は空でない文字列で指定してください")
            if usage not in USAGES and usage != "extract":
                # `extract` は既存 DB のセルを UI が読み込んで PUT し返すため受け入れる（中身の検証は他用途と同じ・管理画面には出ない）。
                raise ValueError(f"model_catalog[{provider}] の未知の用途です: {usage}"
                                 f"（利用可能: {', '.join(USAGES)}）")
            if not isinstance(cell, dict):
                raise ValueError(f"model_catalog[{provider}][{usage}] はオブジェクトで指定してください")
            allowed_raw = cell.get("allowed") if cell.get("allowed") is not None else []
            if not isinstance(allowed_raw, list) or not all(isinstance(m, str) for m in allowed_raw):
                raise ValueError(f"model_catalog[{provider}][{usage}].allowed は文字列の配列で指定してください")
            allowed = list(dict.fromkeys(m.strip() for m in allowed_raw if m.strip()))
            default = cell.get("default") if cell.get("default") is not None else ""
            if not isinstance(default, str):
                raise ValueError(f"model_catalog[{provider}][{usage}].default は文字列で指定してください")
            default = default.strip()
            if default and default not in allowed:
                allowed = [default, *allowed]
            # プロバイダが受け付けるモデル名文法を満たさない値は拒否する。
            name_re = _model_name_re_for(provider)
            bad = [m for m in allowed if not name_re.fullmatch(m)]
            if bad:
                rule = "英数字で始まり . _ / - のみ・64文字以内（':' 不可）" if provider == "codex" \
                    else "英数字で始まり . _ : / - のみ・128文字以内"
                raise ValueError(
                    f"model_catalog[{provider}][{usage}].allowed に無効なモデル名があります: "
                    f"{', '.join(bad)}（{rule}）")
            out_usages[usage] = {"allowed": allowed, "default": default}
        out[provider] = out_usages
    return out or None


def _seed_candidate() -> dict:
    """初回シード値（組み込み既定のコピー）。`OPENAI_EMBED_MODEL` env があれば openai/embed の既定へ一度だけ取り込む。
    env 値も `MODEL_NAME_RE` を満たさなければ警告ログを残して無視する。
    """
    catalog = copy.deepcopy(_DEFAULT_CATALOG)
    raw = (os.environ.get("OPENAI_EMBED_MODEL") or "").strip()
    if raw:
        if not MODEL_NAME_RE.fullmatch(raw):
            _log.warning("OPENAI_EMBED_MODEL の値が不正なため無視します（モデル名文法違反）: %r", raw)
            return catalog
        cell = catalog.setdefault("openai", {}).setdefault("embed", {"allowed": [], "default": ""})
        cell["default"] = raw
        if raw not in cell["allowed"]:
            cell["allowed"] = [raw, *cell["allowed"]]
    return catalog


def seed_catalog_once() -> None:
    """`model_catalog` を env（`OPENAI_EMBED_MODEL` のみ）から一度だけ初期化する（完了マーカー `model_catalog_seed_version`）。DB 不達なら何もしない。"""
    try:
        from sherpa import store
        sysset = store.get_system_settings()
    except Exception as e:
        _log.warning("起動時シード（model_catalog）に失敗しました（DB 不達の可能性）: %s", e)
        return
    if sysset.get(_CATALOG_SEED_MARKER_KEY) is not None:
        return
    try:
        candidate = {_CATALOG_KEY: _seed_candidate(), _CATALOG_SEED_MARKER_KEY: _CATALOG_SEED_VERSION}
        applied, _conflicts = store.seed_system_settings_once(candidate, guard_key=_CATALOG_SEED_MARKER_KEY)
        if _CATALOG_KEY in applied:
            _log.info("起動時シード: model_catalog を初期化しました")
    except Exception as e:
        _log.warning("起動時シード（model_catalog）に失敗しました（DB 不達の可能性）: %s", e)
