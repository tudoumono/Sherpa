"""調べる深さ（探索の踏み込み度合い）＝ EXT-5 Depth/Cost/Verification Profile の吸収
（調べ方ブロック §3.2・`docs/proposals/2026-08-29-調べ方ブロック.md`）。

`depth_profile`: `"standard" | "deep" | "max"`（既定 `"standard"`＝既存の挙動と完全同一）。
既存の per-call override（`impact_service.run_impact`/`lens_service.run_troubleshoot` の
`depth`・`lens_service.run_qa` の `max_hits`・`agentic_search.openai_style` の `max_turns`）と、
新設の上書き経路（`agentic_search.run_tool` の hits/window 上限・Codex `reasoning` の per-turn
上書き）に、**倍率**として掛ける——基準値そのものは書き換えない（PROF-1 の env 積み増しの
上に更に積む・§3.2）。

基準値は「env → system_settings」（`docs/proposals/2026-08-23-設定の責務再設計.md` の SET-2・
WEB-1 と同じ思想）: 各モジュールの既存定数（`agentic_search.MAX_TURNS` 等）が env フォールバック
のまま残り、`effective_base()` が `system_settings`（管理画面 admin-settings.html の基準値編集
セクション・§3.2）にキーがあればそちらを優先する。DB 不達／未設定はそのまま呼び出し側が渡した
`env_default`（各モジュールの既存定数）で動作する（fail-open）。

このモジュールは他の sherpa モジュールを import しない（`layer.py` と同じ葉ノード原則）——
各呼び出し元が自分の env 由来の既定値（モジュール定数）を `env_default` として渡す。
"""
from __future__ import annotations

DEPTH_PROFILES = ("standard", "deep", "max")

# Codex `-c model_reasoning_effort=...` が受理する既知の語彙（`sherpa/providers/codex/
# provider.py::CodexProvider._reason` 参照・`"minimal"` は同モジュールが `"low"` へ丸める）。
CODEX_REASONING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")

# admin-settings.html の基準値編集セクション（§3.2・§6 SC-6c）が読み書きする system_settings
# キー名。`sherpa/routers/system_extras.py::SystemSettingsReq`／`_admin_settings_view()` と
# 同じキー名をここで一元管理する（呼び出し元は短い名前（例 `"max_turns"`）だけを扱う）。
BASE_SETTINGS_KEYS = {
    "max_turns": "depth_base_max_turns",
    "grep_max_hits": "depth_base_grep_max_hits",
    "qa_max_hits": "depth_base_qa_max_hits",
    "read_window": "depth_base_read_window",
    "impact_depth": "depth_base_impact_depth",
    "troubleshoot_depth": "depth_base_troubleshoot_depth",
    "codex_reasoning": "depth_base_codex_reasoning",
}

# §3.2 の倍率表（依頼の初期案どおり・裁定論点8で確定）。
_TURNS_MULT = {"standard": 1, "deep": 2, "max": 3}
# grep/ES ヒット上限・読み取り窓（`run_tool`/`run_qa`）は同じ倍率を共有する。
_RATIO_MULT = {"standard": 1.0, "deep": 1.5, "max": 2.0}
# 影響たどり／トラブルシュート近傍の深さは倍率でなく加算。
_DEPTH_ADD = {"standard": 0, "deep": 2, "max": 4}
# Codex 推論レベルの per-turn 上書き（`None`＝基準値のまま・上書きしない）。
_REASONING_OVERRIDE = {"standard": None, "deep": "high", "max": "xhigh"}


def normalize_depth_profile(v) -> str:
    """欠落（`None`）は `"standard"`。HTTP 入口（`ChatReq.depth_profile`）は pydantic の
    `Literal["standard","deep","max"]` で不正値を 422 にするため、ここに未検証の値が届くのは
    呼び出し側のプログラミングミス（検証を経ない内部値）——黙って `"standard"` へ丸めず
    `ValueError` を送出する（fail-loud・`layer.normalize_layer` と同じ契約）。
    """
    if v is None:
        return "standard"
    if isinstance(v, str) and v in DEPTH_PROFILES:
        return v
    raise ValueError(f"invalid depth_profile value: {v!r}")


def effective_base(system_settings: dict | None, name: str, env_default):
    """管理画面の基準値編集（`system_settings`）を env フォールバックの上に重ねた実効基準値。

    `env_default` は呼び出し側が既に解決済みの env 既定値（各モジュールの既存定数・例:
    `agentic_search.MAX_TURNS`）——ここでは env を再読しない（各モジュールの既存定数を単一の
    真実源のまま保つ）。`system_settings` が `None`（未取得・DB 不達）の場合や、該当キーに
    無効値（`codex_reasoning` 以外は 0 以下・非数値）がある場合は `env_default` に戻す
    （fail-open・DB 不達を理由に調べる深さの計算自体を落とさない）。
    """
    key = BASE_SETTINGS_KEYS[name]
    v = (system_settings or {}).get(key)
    if v is None:
        return env_default
    if name == "codex_reasoning":
        return v if isinstance(v, str) and v.strip() else env_default
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return env_default
    return iv if iv > 0 else env_default


def scaled_turns(base_max_turns: int, profile) -> int:
    """反復上限（`Main Round 上限`相当・`MAX_TURNS`）。標準=×1・深く=×2・最大=×3（切り捨て）。"""
    return int(base_max_turns * _TURNS_MULT[normalize_depth_profile(profile)])


def scaled_ratio(base: int, profile, abs_max: int | None = None) -> int:
    """grep/ES ヒット上限（`MAX_HITS`／`run_qa` の `max_hits`）・読み取り窓（`READ_WINDOW`）。
    標準=×1・深く=×1.5・最大=×2（切り捨て）。

    `abs_max`（省略可・既定 `None`＝クランプなし＝既存呼び出し元は無変更）: 倍率適用
    **後に一度だけ**適用する絶対上限（各モジュールの既存 env 定数の env-parse hi 引数と同じ値を
    渡す想定・例: `agentic_search.MAX_HITS_ABS_MAX`）。管理画面の基準値編集が Field 上限いっぱい
    （例: grep ヒット上限 1000）を指定し、かつ調べる深さが「最大」（×2）のとき、倍率だけでは
    2000 まで無制限に伸びてしまう——`abs_max` は「基準値そのものの妥当な範囲」とは独立に、
    「倍率適用後に実際に外部（grep/ES）へ渡してよい値」を最終的に一度だけ縛る。"""
    v = int(base * _RATIO_MULT[normalize_depth_profile(profile)])
    return min(v, abs_max) if abs_max is not None else v


def scaled_depth(base_depth: int, profile, abs_max: int | None = None) -> int:
    """影響たどり（`IMPACT_MAX_DEPTH`）・トラブルシュート近傍（`TROUBLESHOOT_GRAPH_DEPTH`）の深さ。
    標準=+0・深く=+2・最大=+4。`abs_max`（省略可・既定 `None`）は `scaled_ratio` と同じ契約
    （加算後に一度だけ適用する絶対上限）。"""
    v = int(base_depth) + _DEPTH_ADD[normalize_depth_profile(profile)]
    return min(v, abs_max) if abs_max is not None else v


def codex_reasoning_for(base_reasoning: str, profile) -> str:
    """Codex 推論レベルの per-turn 上書き。標準=基準値のまま・深く=`"high"`・最大=`"xhigh"`。"""
    override = _REASONING_OVERRIDE[normalize_depth_profile(profile)]
    return override if override is not None else base_reasoning
