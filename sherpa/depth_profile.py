"""調べる深さ（探索の踏み込み度合い）＝ EXT-5 Depth/Cost/Verification Profile の吸収
（調べ方ブロック §3.2・`docs/proposals/2026-08-29-調べ方ブロック.md`）。

`depth_profile`: `"quick" | "standard" | "deep" | "max"`（既定 `"standard"`）。
深さは2つの軸に効く:

- 1回あたりの探索量: 既存の per-call override（`impact_service.run_impact`/
  `lens_service.run_troubleshoot` の `depth`・`lens_service.run_qa` の `max_hits`・
  `agentic_search.openai_style` の `max_turns`）と上書き経路（`agentic_search.run_tool` の
  hits/window 上限）に、**倍率**として掛ける——基準値そのものは書き換えない（PROF-1 の env 積み増しの上に更に積む・§3.2）。
  クイックだけ ×0.5（他は従来どおり）で、最低1件は保証する（`scaled_turns`/`scaled_ratio`）。
- evaluator（査読）の巡数: `review_rounds_for`（クイック 0／標準 2／深く 4／最大＝管理画面の共通上限）。
- Codex 推論レベル: `codex_reasoning_for`——クイックだけ `CODEX_REASONING_LEVELS` を1段下げる
  （標準以上は管理画面の基準値のまま・変えない）。

本体が必要な根拠種別を揃えられないと判断したターンは、上限内で深さを1段だけ自動で引き上げる
（`escalated_profile`・`providers/base.py` の巡ループが唯一の消費者）。

基準値は「env → system_settings」（`docs/proposals/2026-08-23-設定の責務再設計.md` の SET-2・
WEB-1 と同じ思想）: 各モジュールの既存定数（`agentic_search.MAX_TURNS` 等）が env フォールバック
のまま残り、`effective_base()` が `system_settings`（管理画面 admin-settings.html の基準値編集
セクション・§3.2）にキーがあればそちらを優先する。DB 不達／未設定はそのまま呼び出し側が渡した
`env_default`（各モジュールの既存定数）で動作する（fail-open）。

このモジュールは他の sherpa モジュールを import しない（`layer.py` と同じ葉ノード原則）——
各呼び出し元が自分の env 由来の既定値（モジュール定数）を `env_default` として渡す。
"""
from __future__ import annotations

DEPTH_PROFILES = ("quick", "standard", "deep", "max")

# Codex `-c model_reasoning_effort=...` が受理する既知の語彙（`sherpa/providers/codex/
# provider.py::CodexProvider._reason` 参照・`"minimal"` は同モジュールが `"low"` へ丸める）。
CODEX_REASONING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")

# admin-settings.html の基準値編集セクション（§3.2・§6 SC-6c）が読み書きする system_settings
# キー名。`sherpa/routers/system_extras.py::SystemSettingsReq`／`_admin_settings_view()` と
# 同じキー名をここで一元管理する（呼び出し元は短い名前（例 `"max_turns"`）だけを扱う）。
# 深さ＝evaluator（査読）の巡数。クイック 0（査読を一度も発動しない＝確認 1 回だけ）・標準 2・
# 深く 4・最大は管理画面の共通上限（system_settings `max_review_rounds`）。`embed_parallel` と
# 同じ流儀で env フォールバックは持たない（設定は UI(DB) が唯一の持ち主）。絶対上限
# （`MAX_REVIEW_ROUNDS_MAX`）は安全弁＝管理者が手動で大きな値を入れても巡はここで頭打ちになる。
REVIEW_ROUNDS_QUICK = 0
REVIEW_ROUNDS_STANDARD = 2
REVIEW_ROUNDS_DEEP = 4
MAX_REVIEW_ROUNDS_DEFAULT = 7
MAX_REVIEW_ROUNDS_MIN = 1
MAX_REVIEW_ROUNDS_MAX = 32

BASE_SETTINGS_KEYS = {
    "max_turns": "depth_base_max_turns",
    "grep_max_hits": "depth_base_grep_max_hits",
    "qa_max_hits": "depth_base_qa_max_hits",
    "read_window": "depth_base_read_window",
    "impact_depth": "depth_base_impact_depth",
    "troubleshoot_depth": "depth_base_troubleshoot_depth",
    "codex_reasoning": "depth_base_codex_reasoning",
}

# §3.2 の倍率表。クイックだけ 0.5（網羅性の強化と、クイックを本当に速くする・変更D①）——
# 実環境計測でクイックと標準の探索量がほぼ同じ（ツール呼び出し52対51）だった是正で、標準／深く／
# 最大は変更しない（裁定論点8で確定した値のまま）。
_TURNS_MULT = {"quick": 0.5, "standard": 1, "deep": 2, "max": 3}
# grep/ES ヒット上限・読み取り窓（`run_tool`/`run_qa`）は同じ倍率を共有する。
_RATIO_MULT = {"quick": 0.5, "standard": 1.0, "deep": 1.5, "max": 2.0}
# 影響たどり／トラブルシュート近傍の深さは倍率でなく加算。
_DEPTH_ADD = {"quick": 0, "standard": 0, "deep": 2, "max": 4}


def normalize_depth_profile(v) -> str:
    """欠落（`None`）は `"standard"`。HTTP 入口（`ChatReq.depth_profile`）は pydantic の
    `Literal["quick","standard","deep","max"]` で不正値を 422 にするため、ここに未検証の値が届くのは
    呼び出し側のプログラミングミス（検証を経ない内部値）——黙って `"standard"` へ丸めず
    `ValueError` を送出する（fail-loud・`layer.normalize_layer` と同じ契約）。
    """
    if v is None:
        return "standard"
    if isinstance(v, str) and v in DEPTH_PROFILES:
        return v
    raise ValueError(f"invalid depth_profile value: {v!r}")


def effective_max_review_rounds(system_settings: dict | None) -> int:
    """「最大」プロファイルの査読巡数（system_settings `max_review_rounds`・管理画面の 1 項目）。

    未設定／非整数／範囲外（`MAX_REVIEW_ROUNDS_MIN`〜`MAX_REVIEW_ROUNDS_MAX`）は既定
    （`MAX_REVIEW_ROUNDS_DEFAULT`）へ倒す——保存側の pydantic Field でも範囲検証するが、読み取り側
    でも独立に検証する（fail-safe・`embeddings.effective_embed_parallel` と同じ契約）。
    """
    configured = system_settings.get("max_review_rounds") if isinstance(system_settings, dict) else None
    if isinstance(configured, bool) or not isinstance(configured, int):
        return MAX_REVIEW_ROUNDS_DEFAULT
    if configured < MAX_REVIEW_ROUNDS_MIN or configured > MAX_REVIEW_ROUNDS_MAX:
        return MAX_REVIEW_ROUNDS_DEFAULT
    return configured


def review_rounds_for(profile, system_settings: dict | None = None) -> int:
    """選択した深さが許す evaluator（査読）の巡数（`providers/base.py::_agentic_run` の巡ループが
    唯一の消費者）。0＝査読を一度も発動しない（クイック＝確認 1 回だけで答える）。

    固定の巡数（標準 2・深く 4）も共通上限（`max_review_rounds`）で頭打ちにする——管理者が上限を
    下げた環境で「深く」が「最大」を超える逆転を起こさないため。
    保存済み会話の depth 値はそのまま読み替えるだけで移行は不要（`"standard"` の意味だけが
    0 巡から 2 巡へ変わる）。不正値は `normalize_depth_profile` と同じ fail-loud（`ValueError`）。
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
    """反復上限（`Main Round 上限`相当・`MAX_TURNS`）。クイック=×0.5・標準=×1・深く=×2・最大=×3
    （切り捨て・最低1を保証——基準値が小さい構成でクイックの×0.5が0まで落ちるのを防ぐ）。"""
    v = int(base_max_turns * _TURNS_MULT[normalize_depth_profile(profile)])
    return max(v, 1)


def scaled_ratio(base: int, profile, abs_max: int | None = None) -> int:
    """grep/ES ヒット上限（`MAX_HITS`／`run_qa` の `max_hits`）・読み取り窓（`READ_WINDOW`）。
    クイック=×0.5・標準=×1・深く=×1.5・最大=×2（切り捨て・最低1を保証）。

    `abs_max`（省略可・既定 `None`＝クランプなし＝既存呼び出し元は無変更）: 倍率適用
    **後に一度だけ**適用する絶対上限（各モジュールの既存 env 定数の env-parse hi 引数と同じ値を
    渡す想定・例: `agentic_search.MAX_HITS_ABS_MAX`）。管理画面の基準値編集が Field 上限いっぱい
    （例: grep ヒット上限 1000）を指定し、かつ調べる深さが「最大」（×2）のとき、倍率だけでは
    2000 まで無制限に伸びてしまう——`abs_max` は「基準値そのものの妥当な範囲」とは独立に、
    「倍率適用後に実際に外部（grep/ES）へ渡してよい値」を最終的に一度だけ縛る。"""
    v = max(int(base * _RATIO_MULT[normalize_depth_profile(profile)]), 1)
    return min(v, abs_max) if abs_max is not None else v


def scaled_depth(base_depth: int, profile, abs_max: int | None = None) -> int:
    """影響たどり（`IMPACT_MAX_DEPTH`）・トラブルシュート近傍（`TROUBLESHOOT_GRAPH_DEPTH`）の深さ。
    クイック/標準=+0・深く=+2・最大=+4。`abs_max`（省略可・既定 `None`）は `scaled_ratio` と同じ契約
    （加算後に一度だけ適用する絶対上限）。"""
    v = int(base_depth) + _DEPTH_ADD[normalize_depth_profile(profile)]
    return min(v, abs_max) if abs_max is not None else v


def codex_reasoning_for(base_reasoning: str, profile) -> str:
    """Codex 推論レベル。**クイックだけ例外**——クイックは `CODEX_REASONING_LEVELS` の並び順で
    1段下げる（最下段 `"minimal"` は据え置き）。標準以上はどの深さでも管理画面の基準値
    （`codex_reasoning`／`depth_base_codex_reasoning`）をそのまま使う（従来どおり「深さでは
    変えない」）。

    クイックを1段下げる理由（クイックを本当に速くする・変更D②）: クイックは探索量（①）だけでなく
    推論そのものを軽くしてこそ「速い」を体感できる——標準以上は査読（`review_rounds_for`）が
    品質を担保するため対象外にする。`base_reasoning` が既知の語彙（`CODEX_REASONING_LEVELS`）に
    無い（設定の壊れ）ときは fail-open でそのまま通す。深さの妥当性検証（fail-loud）はどちらの
    分岐でも通す（呼び出し元が未検証の値を渡していないことをこの経路でも保証する）。
    """
    p = normalize_depth_profile(profile)
    if p != "quick" or not isinstance(base_reasoning, str) or base_reasoning not in CODEX_REASONING_LEVELS:
        return base_reasoning
    i = CODEX_REASONING_LEVELS.index(base_reasoning)
    return CODEX_REASONING_LEVELS[max(i - 1, 0)]


def escalated_profile(profile) -> str | None:
    """1段上の深さ（`"quick"`→`"standard"`→`"deep"`→`"max"`）。`"max"` は上限のため `None`。

    本体が必要な根拠種別を揃えられないと判断したときの自動引き上げ（1ターンに1回まで）が
    唯一の消費者——利用者が選んだ深さ自体は書き換えない（実効値だけが1段上になる）。
    """
    i = DEPTH_PROFILES.index(normalize_depth_profile(profile))
    return DEPTH_PROFILES[i + 1] if i + 1 < len(DEPTH_PROFILES) else None


def effective_max_turns(system_settings: dict | None, env_default: int, profile) -> int:
    """反復上限の実効値＝ `effective_base(...,"max_turns",env_default)` → `scaled_turns(...)` の合成。

    OpenAI/Ollama の `_agentic_loop` がループへ渡す値の単一の真実源（記録側は再計算せず、ループが
    実際に渡した値を `_last_main_depth_usage` から読む）。下調べ役（`_sub_loop`）はプロファイル固有の
    guard 値と横断予算の上書きがあるため同じ合成を自前で持ち、`_last_sub_depth_usage` に実値を残す。"""
    return scaled_turns(effective_base(system_settings, "max_turns", env_default), profile)


def usage_extras(profile, *, max_turns: int | None = None, max_tools_per_turn: int | None = None) -> dict:
    """usage メタ（`messages.answer.usage`）へ足す depth 由来のキー（API 経路・STAT-3 S1）。

    `depth_profile` は常に入れる（欠落は `"standard"`）。`max_turns`/`max_tools_per_turn`
    （その深さで実際に使った実効上限・呼び出し元が計算済みの値をそのまま渡す契約）は両方揃っている
    ときだけ足す（過去データ遡及なし＝欠落は欄ごと省略の流儀を usage dict でも踏襲する）。"""
    out = {"depth_profile": normalize_depth_profile(profile)}
    if max_turns is not None and max_tools_per_turn is not None:
        out["max_turns"] = int(max_turns)
        out["max_tools_per_turn"] = int(max_tools_per_turn)
    return out


def usage_reasoning_extras(profile, base_reasoning: str, effective_reasoning: str) -> dict:
    """usage メタへ足す depth 由来のキー（Codex 経路・STAT-3 S1）。

    `reasoning` は実際に `codex exec -c model_reasoning_effort=...` へ渡した値。基準値
    （`base_reasoning`＝構成の `codex_reasoning`／author 用の env）と実際に渡した値が異なるときだけ
    `reasoning_base` も足す（一致時は省略——深さは推論レベルを変えないため、呼び出し元が別の理由で
    上書きした回だけがここに残る）。"""
    out = {"depth_profile": normalize_depth_profile(profile), "reasoning": effective_reasoning}
    if base_reasoning != effective_reasoning:
        out["reasoning_base"] = base_reasoning
    return out
