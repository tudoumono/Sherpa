"""調べる深さ（quick / standard / deep / max）に応じた探索量・査読の巡数・Codex 推論レベルを決める。
設計: docs/design/codex.md「調査台帳と回答前の関門」

- 探索量: grep/ES のヒット上限・読み取り窓に倍率を掛ける（`scaled_ratio`）。影響たどり等の深さは加算（`scaled_depth`）。
- 査読の巡数: `review_rounds_for`。
- Codex 推論レベル: `codex_reasoning_for`（クイックだけ 1 段下げる）。
- 根拠種別が揃わないときの 1 段引き上げ: `escalated_profile`。

基準値は system_settings（管理画面）が正で、未設定・不正・DB 不達は呼び出し側が渡すコード既定へ倒す（`effective_base`）。
他の sherpa モジュールを import しない葉ノード。
"""
from __future__ import annotations

DEPTH_PROFILES = ("quick", "standard", "deep", "max")

# Codex `-c model_reasoning_effort=...` が受理する語彙。
CODEX_REASONING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
# 管理画面が未設定のときの Codex 推論レベル。
CODEX_REASONING_DEFAULT = "medium"

# 査読の巡数: クイック 0・標準 2・深く 4・最大は system_settings `max_review_rounds`。
# MAX_REVIEW_ROUNDS_MAX は絶対上限。
REVIEW_ROUNDS_QUICK = 0
REVIEW_ROUNDS_STANDARD = 2
REVIEW_ROUNDS_DEEP = 4
MAX_REVIEW_ROUNDS_DEFAULT = 7
MAX_REVIEW_ROUNDS_MIN = 1
MAX_REVIEW_ROUNDS_MAX = 32

# 管理画面の基準値編集が読み書きする system_settings のキー名（`routers/system_extras.py` と揃える）。
BASE_SETTINGS_KEYS = {
    "grep_max_hits": "depth_base_grep_max_hits",
    "qa_max_hits": "depth_base_qa_max_hits",
    "read_window": "depth_base_read_window",
    "impact_depth": "depth_base_impact_depth",
    "troubleshoot_depth": "depth_base_troubleshoot_depth",
    "codex_reasoning": "depth_base_codex_reasoning",
}

# grep/ES ヒット上限・読み取り窓に掛ける倍率。
_RATIO_MULT = {"quick": 0.5, "standard": 1.0, "deep": 1.5, "max": 2.0}
# 影響たどり／トラブルシュート近傍の深さは加算。
_DEPTH_ADD = {"quick": 0, "standard": 0, "deep": 2, "max": 4}


def normalize_depth_profile(v) -> str:
    """深さを正規化する。欠落（`None`）は `"standard"`、不正値は `ValueError`。"""
    if v is None:
        return "standard"
    if isinstance(v, str) and v in DEPTH_PROFILES:
        return v
    raise ValueError(f"invalid depth_profile value: {v!r}")


def effective_max_review_rounds(system_settings: dict | None) -> int:
    """「最大」の査読巡数（system_settings `max_review_rounds`）。未設定・不正・範囲外は既定へ倒す。"""
    configured = system_settings.get("max_review_rounds") if isinstance(system_settings, dict) else None
    if isinstance(configured, bool) or not isinstance(configured, int):
        return MAX_REVIEW_ROUNDS_DEFAULT
    if configured < MAX_REVIEW_ROUNDS_MIN or configured > MAX_REVIEW_ROUNDS_MAX:
        return MAX_REVIEW_ROUNDS_DEFAULT
    return configured


def review_rounds_for(profile, system_settings: dict | None = None) -> int:
    """深さが許す査読の巡数。0 は査読なし（クイック）。
    固定の巡数（標準 2・深く 4）も共通上限で頭打ちにする。不正値は `ValueError`。
    """
    p = normalize_depth_profile(profile)
    _cap = effective_max_review_rounds(system_settings)
    if p == "quick":
        return REVIEW_ROUNDS_QUICK
    if p == "standard":
        return min(REVIEW_ROUNDS_STANDARD, _cap)
    if p == "deep":
        return min(REVIEW_ROUNDS_DEEP, _cap)
    return _cap


def effective_base(system_settings: dict | None, name: str, default):
    """system_settings の基準値の実効値。`default` はコード既定。未設定・無効値は `default` に戻す。"""
    key = BASE_SETTINGS_KEYS[name]
    v = (system_settings or {}).get(key)
    if v is None:
        return default
    if name == "codex_reasoning":
        return v if isinstance(v, str) and v.strip() else default
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return default
    return iv if iv > 0 else default


def scaled_ratio(base: int, profile, abs_max: int | None = None) -> int:
    """ヒット上限・読み取り窓に深さの倍率を掛ける（クイック ×0.5・標準 ×1・深く ×1.5・最大 ×2・最低 1）。
    `abs_max` は倍率適用後に一度だけ掛ける絶対上限。
    """
    v = max(int(base * _RATIO_MULT[normalize_depth_profile(profile)]), 1)
    return min(v, abs_max) if abs_max is not None else v


def scaled_depth(base_depth: int, profile, abs_max: int | None = None) -> int:
    """影響たどり・トラブルシュート近傍の深さ（クイック/標準 +0・深く +2・最大 +4）。`abs_max` は加算後の上限。"""
    v = int(base_depth) + _DEPTH_ADD[normalize_depth_profile(profile)]
    return min(v, abs_max) if abs_max is not None else v


def codex_reasoning_for(base_reasoning: str, profile) -> str:
    """Codex 推論レベル。クイックだけ `CODEX_REASONING_LEVELS` で 1 段下げ、標準以上は基準値のまま。
    未知の語彙はそのまま通す。深さの検証は常に通す。
    """
    p = normalize_depth_profile(profile)
    if p != "quick" or not isinstance(base_reasoning, str) or base_reasoning not in CODEX_REASONING_LEVELS:
        return base_reasoning
    i = CODEX_REASONING_LEVELS.index(base_reasoning)
    return CODEX_REASONING_LEVELS[max(i - 1, 0)]


def escalated_profile(profile) -> str | None:
    """1 段上の深さ。`"max"` は `None`。根拠種別が揃わないときの自動引き上げ（1 ターン 1 回）が使う。"""
    i = DEPTH_PROFILES.index(normalize_depth_profile(profile))
    return DEPTH_PROFILES[i + 1] if i + 1 < len(DEPTH_PROFILES) else None


def usage_reasoning_extras(profile, base_reasoning: str, effective_reasoning: str) -> dict:
    """usage メタへ足す depth 由来のキー（Codex 経路）。基準値と実際に渡した値が違うときだけ `reasoning_base` も足す。"""
    out = {"depth_profile": normalize_depth_profile(profile), "reasoning": effective_reasoning}
    if base_reasoning != effective_reasoning:
        out["reasoning_base"] = base_reasoning
    return out
