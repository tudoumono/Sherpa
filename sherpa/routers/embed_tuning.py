"""システム設定のうち埋め込みの項目（接続先・同時送信数・細かさ・1 回の待ち時間・1 回の件数）の画面表示と検証。
設計: docs/design/settings.md「埋め込み」
"""
from __future__ import annotations

from fastapi import HTTPException

# 設定キー → (実効値の関数名, 既定, 最小, 最大)。待ち時間と件数は同じ置き場・同じ検証で扱う
RANGES = {
    "embed_request_timeout_s": ("effective_embed_request_timeout", "EMBED_TIMEOUT_DEFAULT", "EMBED_TIMEOUT_MIN", "EMBED_TIMEOUT_MAX"),
    "embed_batch_size": ("effective_embed_batch_size", "EMBED_BATCH_DEFAULT", "EMBED_BATCH_MIN", "EMBED_BATCH_MAX"),
}


def dim_view(sysset: dict) -> dict:
    """設定画面の `embed_dim`。既定は使っている OpenAI 埋め込みモデルの次元。Ollama のときは `applies` が偽。"""
    from sherpa import embeddings, llm, model_catalog
    model = model_catalog.resolve_model("openai", "embed", None, system_settings=sysset) or embeddings._MODELS["openai"][0]
    try:
        ollama = (embeddings.effective_embed_provider(sysset) == "ollama"
                  or llm.resolve_auto_provider(None, system_settings=sysset) == "ollama")
    except Exception:
        ollama = False
    return {
        "configured": sysset.get("embed_dim"),
        "effective": embeddings.effective_embed_dim(sysset, model),
        "default": embeddings.openai_model_default_dim(model),
        "model_max": embeddings.openai_model_max_dim(model),
        "min": embeddings.EMBED_DIM_MIN,
        "max": embeddings.EMBED_DIM_MAX,
        "applies": not ollama,
    }


def view(sysset: dict) -> dict:
    """設定画面の埋め込み 5 項目（`embed_provider`・`embed_parallel`・`embed_dim`・`embed_request_timeout_s`・`embed_batch_size`）。"""
    from sherpa import embeddings, model_catalog
    out = {
        "embed_provider": {
            "configured": sysset.get("embed_provider"), "invalid": embeddings.invalid_embed_provider(sysset) is not None,
            "effective": embeddings.effective_embed_provider(sysset),
            "default": embeddings.EMBED_PROVIDER_DEFAULT,
            "options": list(embeddings.EMBED_PROVIDERS),
            "ollama_model": model_catalog.resolve_model("ollama", "embed", None, system_settings=sysset)
                            or embeddings._MODELS["ollama"][0],
        },
        "embed_parallel": {
            "configured": sysset.get("embed_parallel"),
            "effective": embeddings.effective_embed_parallel(sysset),
            "default": embeddings.EMBED_PARALLEL_DEFAULT,
        },
        "embed_dim": dim_view(sysset),
    }
    for key, (fn, default, low, high) in RANGES.items():
        out[key] = {"configured": sysset.get(key), "effective": getattr(embeddings, fn)(sysset),
                    "default": getattr(embeddings, default), "min": getattr(embeddings, low), "max": getattr(embeddings, high)}
    return out


def _validate_provider(value):
    """`embed_provider` の検証。None は未設定（既定 "auto"）。閉じた語彙以外は 422。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "embed_provider は文字列で指定してください")
    from sherpa import embeddings
    v = value.strip().lower()
    if v not in embeddings.EMBED_PROVIDERS:
        raise HTTPException(422, f"embed_provider は {'/'.join(embeddings.EMBED_PROVIDERS)} のいずれかで指定してください")
    return v


def _validate_dim(value):
    """`embed_dim` の検証。None は未設定（1536）。64〜4096 以外は 422。"""
    from sherpa import embeddings
    if value is not None and not embeddings.EMBED_DIM_MIN <= value <= embeddings.EMBED_DIM_MAX:
        raise HTTPException(422, f"embed_dim は {embeddings.EMBED_DIM_MIN}〜{embeddings.EMBED_DIM_MAX} の整数で指定してください")
    return value


def validated_updates(provided: dict) -> dict:
    """送られた埋め込みの項目だけを検証して、保存する値にして返す（`embed_parallel` は型と範囲を pydantic が検証済み）。"""
    from sherpa import embeddings
    out: dict = {}
    if "embed_provider" in provided:
        out["embed_provider"] = _validate_provider(provided["embed_provider"])
    if "embed_parallel" in provided:
        out["embed_parallel"] = provided["embed_parallel"]
    if "embed_dim" in provided:
        out["embed_dim"] = _validate_dim(provided["embed_dim"])
    for key, (_fn, _default, low, high) in RANGES.items():
        if key in provided:
            v = provided[key]
            if v is not None and not getattr(embeddings, low) <= v <= getattr(embeddings, high):
                raise HTTPException(422, f"{key} は {getattr(embeddings, low)}〜{getattr(embeddings, high)} の整数で指定してください")
            out[key] = v
    return out
