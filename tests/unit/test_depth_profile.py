"""調べる深さ（`sherpa.depth_profile`）の単体テスト（調べ方ブロック §3.2・SC-6c。深さは4段＝
クイック/標準/深く/最大）。

- `review_rounds_for`: 見直しの巡数（クイック 0／標準 2／深く 4／最大＝管理画面の共通上限）。
- `scaled_turns`/`scaled_ratio`/`scaled_depth`: 深さに応じて探索量を拡大する（ターン数
  ×1/×1/×2/×3・ヒット上限と読取窓 ×1/×1/×1.5/×2・たどる深さ +0/+0/+2/+4）。
  `abs_max` の絶対上限は倍率適用後に一度だけ効く。不正な depth_profile は fail-loud のまま。
- `codex_reasoning_for`: 推論レベルは**深さで変えない**（どの深さでも管理画面の基準値のまま）。
- `escalated_profile`: 1段上の深さ（最大は上限＝None）。
- `effective_base`: system_settings の基準値編集が env 既定値より優先されること・
  無効値（0以下・非数値）は env 既定へ fail-open すること。
- `normalize_depth_profile`: 省略は standard・不正値は ValueError（fail-loud）。
"""
from __future__ import annotations

import pytest

from sherpa import depth_profile as D


def test_normalize_depth_profile_omitted_defaults_to_standard():
    assert D.normalize_depth_profile(None) == "standard"


@pytest.mark.parametrize("v", ["quick", "standard", "deep", "max"])
def test_normalize_depth_profile_valid_passthrough(v):
    assert D.normalize_depth_profile(v) == v


def test_normalize_depth_profile_invalid_raises():
    with pytest.raises(ValueError):
        D.normalize_depth_profile("bogus")


# ===== 深さ4段の対応表（巡数・倍率・加算・推論・引き上げ・usage）=====

# 1行＝1つの深さ。`rounds`（`max_review_rounds` 既定 7 のとき）・反復上限（基準 12）・
# ヒット上限（基準 30）・読取窓（基準 40）・影響の段数（基準 8）・推論（基準 medium）・
# 1段上の深さ。深さは推論レベルを変えない＝`reasoning` 列は全段で基準値のまま。
_DEPTH_MATRIX = [
    # profile,    rounds, turns, hits, window, depth, reasoning, escalated
    ("quick",     0,      12,    30,   40,     8,     "medium",  "standard"),
    ("standard",  2,      12,    30,   40,     8,     "medium",  "deep"),
    ("deep",      4,      24,    45,   60,     10,    "medium",  "max"),
    ("max",       7,      36,    60,   80,     12,    "medium",  None),
]


@pytest.mark.parametrize("profile,rounds,turns,hits,window,depth,reasoning,escalated", _DEPTH_MATRIX)
def test_depth_matrix(profile, rounds, turns, hits, window, depth, reasoning, escalated):
    """4段（クイック/標準/深く/最大）の期待値表を1本で固定する。"""
    assert D.review_rounds_for(profile) == rounds
    assert D.scaled_turns(12, profile) == turns
    assert D.scaled_ratio(30, profile) == hits
    assert D.scaled_ratio(40, profile) == window
    assert D.scaled_depth(8, profile) == depth
    assert D.codex_reasoning_for("medium", profile) == reasoning
    assert D.escalated_profile(profile) == escalated
    assert D.usage_extras(profile)["depth_profile"] == profile


@pytest.mark.parametrize("profile,expected", [("quick", 3), ("standard", 3), ("deep", 5), ("max", 7)])
def test_scaled_depth_adds_with_depth_troubleshoot(profile, expected):
    assert D.scaled_depth(3, profile) == expected


@pytest.mark.parametrize("base", ["low", "medium", "high", "xhigh", "bogus"])
@pytest.mark.parametrize("profile", ["quick", "standard", "deep", "max"])
def test_codex_reasoning_for_is_fixed_by_configured_base(base, profile):
    """推論レベルは深さで変えない——どの深さでも管理画面の基準値をそのまま返す
    （未知の語彙＝設定の壊れもそのまま通す＝深さ由来の上書きは無い）。"""
    assert D.codex_reasoning_for(base, profile) == base


def test_codex_reasoning_for_rejects_invalid_profile():
    """推論を変えなくなっても depth_profile 自体の妥当性検証（fail-loud）は残す。"""
    with pytest.raises(ValueError):
        D.codex_reasoning_for("medium", "deeper")


# ===== escalated_profile: 自動引き上げの1段上 =====

def test_escalated_profile_omitted_profile_steps_up_from_standard():
    assert D.escalated_profile(None) == "deep"      # 欠落は standard


def test_escalated_profile_max_is_capped():
    """利用者が既に「最大」を選んでいれば上限＝引き上げない。"""
    assert D.escalated_profile("max") is None


def test_escalated_profile_rejects_invalid_profile():
    with pytest.raises(ValueError):
        D.escalated_profile("deeper")


def test_scaled_ratio_invalid_profile_still_raises():
    """基準値パススルーになっても depth_profile 自体の妥当性検証（fail-loud）は残す。"""
    with pytest.raises(ValueError):
        D.scaled_ratio(15, "bogus")


# ===== effective_base: system_settings（管理画面の基準値編集）→ env 既定 =====

def test_effective_base_none_settings_uses_env_default():
    assert D.effective_base(None, "max_turns", 12) == 12


def test_effective_base_empty_settings_uses_env_default():
    assert D.effective_base({}, "max_turns", 12) == 12


def test_effective_base_configured_overrides_env_default():
    assert D.effective_base({"depth_base_max_turns": 20}, "max_turns", 12) == 20


def test_effective_base_zero_or_negative_falls_back_to_env_default():
    """0以下は無効値として env_default へ fail-open（管理画面のクリア操作の安全網）。"""
    assert D.effective_base({"depth_base_max_turns": 0}, "max_turns", 12) == 12
    assert D.effective_base({"depth_base_max_turns": -5}, "max_turns", 12) == 12


def test_effective_base_non_numeric_falls_back_to_env_default():
    assert D.effective_base({"depth_base_max_turns": "not-a-number"}, "max_turns", 12) == 12


def test_effective_base_codex_reasoning_string_passthrough():
    assert D.effective_base({"depth_base_codex_reasoning": "high"}, "codex_reasoning", "low") == "high"


def test_effective_base_codex_reasoning_blank_falls_back_to_env_default():
    assert D.effective_base({"depth_base_codex_reasoning": ""}, "codex_reasoning", "low") == "low"


def test_effective_base_codex_reasoning_non_string_falls_back_to_env_default():
    assert D.effective_base({"depth_base_codex_reasoning": 123}, "codex_reasoning", "low") == "low"


def test_base_settings_keys_cover_all_seven_knobs():
    """admin-settings.html の基準値編集セクション（§3.2・§6 SC-6c）が扱う7項目。"""
    assert set(D.BASE_SETTINGS_KEYS) == {
        "max_turns", "grep_max_hits", "qa_max_hits", "read_window",
        "impact_depth", "troubleshoot_depth", "codex_reasoning",
    }


# ===== abs_max（倍率適用後に一度だけ効く絶対上限）=====

def test_scaled_ratio_abs_max_clamps_after_multiplier():
    """管理画面の基準値編集（Field 上限）＋深さ「最大」（×2）でも絶対上限を超えない。"""
    assert D.scaled_ratio(1000, "max", abs_max=1000) == 1000
    assert D.scaled_ratio(1000, "deep", abs_max=1000) == 1000
    assert D.scaled_ratio(2000, "standard", abs_max=1000) == 1000


def test_scaled_ratio_abs_max_does_not_affect_values_within_bound():
    """倍率適用後も abs_max 以内なら倍率どおりの値。"""
    assert D.scaled_ratio(30, "quick", abs_max=1000) == 30
    assert D.scaled_ratio(30, "standard", abs_max=1000) == 30
    assert D.scaled_ratio(30, "deep", abs_max=1000) == 45
    assert D.scaled_ratio(30, "max", abs_max=1000) == 60


def test_scaled_ratio_abs_max_omitted_keeps_existing_behavior():
    """abs_max 省略（既定 None）はクランプなし（既存呼び出し元は無変更）。"""
    assert D.scaled_ratio(2000, "max") == 4000


def test_scaled_depth_abs_max_clamps_after_addition():
    assert D.scaled_depth(68, "max", abs_max=64) == 64
    assert D.scaled_depth(63, "deep", abs_max=64) == 64
    assert D.scaled_depth(64, "standard", abs_max=64) == 64   # 64 ちょうど（クランプ境界）


def test_scaled_depth_abs_max_does_not_affect_values_within_bound():
    assert D.scaled_depth(8, "standard", abs_max=64) == 8
    assert D.scaled_depth(8, "deep", abs_max=64) == 10
    assert D.scaled_depth(8, "max", abs_max=64) == 12


def test_scaled_depth_abs_max_omitted_keeps_existing_behavior():
    assert D.scaled_depth(68, "max") == 72


# ===== STAT-3 S1（利用統計の拡充）: usage メタへ足す depth 由来のキー =====

@pytest.mark.parametrize("profile,mult", [("quick", 1), ("standard", 1), ("deep", 2), ("max", 3)])
def test_effective_max_turns_composes_effective_base_and_scaled_turns(profile, mult):
    """`effective_base(...,"max_turns",12)` → `scaled_turns(...)` と同じ結果（単一の真実源）＝
    管理画面の基準値編集と深さの倍率の両方が効く。"""
    assert D.effective_max_turns(None, 12, profile) == 12 * mult
    assert D.effective_max_turns({"depth_base_max_turns": 20}, 1, profile) == 20 * mult


def test_usage_extras_always_includes_depth_profile_defaulting_to_standard():
    assert D.usage_extras(None) == {"depth_profile": "standard"}
    assert D.usage_extras("deep") == {"depth_profile": "deep"}


def test_usage_extras_omits_limits_unless_both_given():
    """max_turns/max_tools_per_turn は両方揃ったときだけ足す（欠落は欄ごと省略）。"""
    assert D.usage_extras("standard", max_turns=12) == {"depth_profile": "standard"}
    assert D.usage_extras("standard", max_tools_per_turn=16) == {"depth_profile": "standard"}
    assert D.usage_extras("deep", max_turns=24, max_tools_per_turn=16) == {
        "depth_profile": "deep", "max_turns": 24, "max_tools_per_turn": 16}


def test_usage_reasoning_extras_omits_base_when_unchanged():
    assert D.usage_reasoning_extras("standard", "medium", "medium") == {
        "depth_profile": "standard", "reasoning": "medium"}


def test_usage_reasoning_extras_includes_base_when_caller_values_differ():
    """`usage_reasoning_extras` 自体は与えられた2値の比較のみを行う（呼び出し元契約）。"""
    assert D.usage_reasoning_extras("deep", "medium", "high") == {
        "depth_profile": "deep", "reasoning": "high", "reasoning_base": "medium"}


@pytest.mark.parametrize("profile", ["quick", "standard", "deep", "max"])
def test_usage_reasoning_extras_via_codex_reasoning_for_omits_base_at_every_depth(profile):
    """実際の呼び出し経路（`codex_reasoning_for` の戻り値を渡す）では、深さが推論を変えない
    ＝どの深さでも `reasoning_base` は付かない。"""
    assert D.usage_reasoning_extras(profile, "medium", D.codex_reasoning_for("medium", profile)) == {
        "depth_profile": profile, "reasoning": "medium"}


# ---- 深さ＝evaluator（査読）の巡数 ----

def test_review_rounds_quick_is_zero_and_standard_deep_are_fixed():
    """クイックは 0 巡（査読を一度も発動しない＝確認 1 回だけ）・標準 2 巡／深く 4 巡（固定）。"""
    assert D.review_rounds_for("quick") == 0
    assert D.review_rounds_for(None) == 2          # 欠落は standard＝既定の 2 巡
    assert D.review_rounds_for("standard", {"max_review_rounds": 3}) == 2   # 上限内なら固定値
    assert D.review_rounds_for("deep") == 4


def test_review_rounds_fixed_tiers_are_clamped_by_the_common_cap():
    """管理者が共通上限を下げた環境でも、固定の巡数（標準 2・深く 4）が「最大」を超えない。"""
    assert D.review_rounds_for("deep", {"max_review_rounds": 3}) == 3
    assert D.review_rounds_for("deep", {"max_review_rounds": 1}) == 1
    assert D.review_rounds_for("standard", {"max_review_rounds": 1}) == 1
    # 上限そのもの（「最大」）を下回らない＝どの段も「最大」以下に収まる。
    for cap in (1, 2, 3, 4, 7):
        rounds = [D.review_rounds_for(p, {"max_review_rounds": cap})
                  for p in ("quick", "standard", "deep", "max")]
        assert rounds == sorted(rounds) and rounds[-1] == cap, cap


def test_review_rounds_max_follows_system_setting_with_default_seven():
    """最大は管理画面の共通上限（system_settings `max_review_rounds`・既定 7）。"""
    assert D.review_rounds_for("max") == D.MAX_REVIEW_ROUNDS_DEFAULT == 7
    assert D.review_rounds_for("max", {"max_review_rounds": 3}) == 3
    assert (D.review_rounds_for("max", {"max_review_rounds": D.MAX_REVIEW_ROUNDS_MAX})
            == D.MAX_REVIEW_ROUNDS_MAX)


def test_effective_max_review_rounds_falls_back_on_invalid_values():
    """未設定・非整数・bool・範囲外はいずれも既定へ倒す（読み取り側でも独立に検証・fail-safe）。"""
    for bad in (None, "3", 3.5, True, 0, -1, D.MAX_REVIEW_ROUNDS_MAX + 1):
        assert (D.effective_max_review_rounds({"max_review_rounds": bad})
                == D.MAX_REVIEW_ROUNDS_DEFAULT)
    assert D.effective_max_review_rounds(None) == D.MAX_REVIEW_ROUNDS_DEFAULT
    assert D.effective_max_review_rounds({}) == D.MAX_REVIEW_ROUNDS_DEFAULT


def test_review_rounds_rejects_invalid_profile():
    """不正な深さは `normalize_depth_profile` と同じ fail-loud。"""
    with pytest.raises(ValueError):
        D.review_rounds_for("deeper")
