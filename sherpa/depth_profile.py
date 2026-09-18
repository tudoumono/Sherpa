"""調べる深さ（探索の踏み込み度合い）＝ EXT-5 Depth/Cost/Verification Profile の吸収
（調べ方ブロック §3.2・`docs/proposals/2026-08-29-調べ方ブロック.md`）。

`depth_profile`: `"standard" | "deep" | "max"`（既定 `"standard"`＝既存の挙動と完全同一）。
深さは2つの軸に効く:

- 1回あたりの探索量: 既存の per-call override（`impact_service.run_impact`/
  `lens_service.run_troubleshoot` の `depth`・`lens_service.run_qa` の `max_hits`・
  `agentic_search.openai_style` の `max_turns`）と上書き経路（`agentic_search.run_tool` の
  hits/window 上限・Codex `reasoning` の per-turn 上書き）に、**倍率**として掛ける——基準値
  そのものは書き換えない（PROF-1 の env 積み増しの上に更に積む・§3.2）。
- evaluator（査読）の巡数: `review_rounds_for`（標準 0／深く 2／最大＝管理画面の共通上限）。

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

DEPTH_PROFILES = ("standard", "deep", "max")

# Codex `-c model_reasoning_effort=...` が受理する既知の語彙（`sherpa/providers/codex/
# provider.py::CodexProvider._reason` 参照・`"minimal"` は同モジュールが `"low"` へ丸める）。
CODEX_REASONING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")

# admin-settings.html の基準値編集セクション（§3.2・§6 SC-6c）が読み書きする system_settings
# キー名。`sherpa/routers/system_extras.py::SystemSettingsReq`／`_admin_settings_view()` と
# 同じキー名をここで一元管理する（呼び出し元は短い名前（例 `"max_turns"`）だけを扱う）。
# 深さ＝evaluator（査読）の巡数（DEPTH-2 §2.3）。標準 0（査読を一度も発動しない）・深く 2・
# 最大は管理画面の共通上限（system_settings `max_review_rounds`）。`embed_parallel` と同じ流儀で
# env フォールバックは持たない（設定は UI(DB) が唯一の持ち主）。絶対上限（`MAX_REVIEW_ROUNDS_MAX`）
# は安全弁＝管理者が手動で大きな値を入れても巡はここで頭打ちになる。
REVIEW_ROUNDS_STANDARD = 0
REVIEW_ROUNDS_DEEP = 2
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

# §3.2 の倍率表（依頼の初期案どおり・裁定論点8で確定）。
_TURNS_MULT = {"standard": 1, "deep": 2, "max": 3}
# grep/ES ヒット上限・読み取り窓（`run_tool`/`run_qa`）は同じ倍率を共有する。
_RATIO_MULT = {"standard": 1.0, "deep": 1.5, "max": 2.0}
# 影響たどり／トラブルシュート近傍の深さは倍率でなく加算。
_DEPTH_ADD = {"standard": 0, "deep": 2, "max": 4}
# Codex 推論レベルの per-turn 上書き（`None`＝基準値のまま・上書きしない）。基準値が既に上なら
# 下げない（`codex_reasoning_for` が `CODEX_REASONING_LEVELS` の順序で比較する）。
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
    """選択した深さが許す evaluator（査読）の巡数（DEPTH-2 §2.3・`providers/base.py::_agentic_run`
    の巡ループが唯一の消費者）。0＝査読を一度も発動しない（標準）。

    保存済み会話の depth 値（`"standard"`/`"deep"`/`"max"`）はそのまま読み替えるだけで移行は不要。
    不正値は `normalize_depth_profile` と同じ fail-loud（`ValueError`）。
    """
    p = normalize_depth_profile(profile)
    if p == "standard":
        return REVIEW_ROUNDS_STANDARD
    if p == "deep":
        return REVIEW_ROUNDS_DEEP
    return effective_max_review_rounds(system_settings)


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
    """Codex 推論レベルの per-turn 上書き。標準=基準値のまま・深く=`"high"`・最大=`"xhigh"`。

    深さの指定は基準値（管理画面の `codex_reasoning`）を**下げない**——基準が既に `"xhigh"` の
    環境で「深く」を選ぶと `"high"` へ落ちる逆転が起きるため、`CODEX_REASONING_LEVELS` の順序で
    比較して高い方を返す。基準値が未知の語彙（設定の壊れ）なら比較できないので深さの指定を使う。
    """
    override = _REASONING_OVERRIDE[normalize_depth_profile(profile)]
    if override is None:
        return base_reasoning
    if base_reasoning not in CODEX_REASONING_LEVELS:
        return override
    return (override if CODEX_REASONING_LEVELS.index(override) > CODEX_REASONING_LEVELS.index(base_reasoning)
            else base_reasoning)


def escalated_profile(profile) -> str | None:
    """1段上の深さ（`"standard"`→`"deep"`→`"max"`）。`"max"` は上限のため `None`。

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
    （`base_reasoning`＝構成の `codex_reasoning`／author 用の env）と上書き後の値が異なるときだけ
    `reasoning_base` も足す（一致時は省略＝標準プロファイルで基準値どおりのケースがほとんどのため
    冗長なキーを増やさない）。"""
    out = {"depth_profile": normalize_depth_profile(profile), "reasoning": effective_reasoning}
    if base_reasoning != effective_reasoning:
        out["reasoning_base"] = base_reasoning
    return out
