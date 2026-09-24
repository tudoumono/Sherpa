"""調べる深さ（`sherpa.depth_profile`）の単体テスト（調べ方ブロック §3.2・SC-6c）。

- 倍率表（標準/深く/最大）が依頼の初期案どおりの値を返すこと。
- `effective_base`: system_settings の基準値編集が env 既定値より優先されること・
  無効値（0以下・非数値）は env 既定へ fail-open すること。
- `normalize_depth_profile`: 省略は standard・不正値は ValueError（fail-loud）。
"""
from __future__ import annotations

import pytest

from sherpa import depth_profile as D


def test_normalize_depth_profile_omitted_defaults_to_standard():
    assert D.normalize_depth_profile(None) == "standard"


@pytest.mark.parametrize("v", ["standard", "deep", "max"])
def test_normalize_depth_profile_valid_passthrough(v):
    assert D.normalize_depth_profile(v) == v


def test_normalize_depth_profile_invalid_raises():
    with pytest.raises(ValueError):
        D.normalize_depth_profile("bogus")


# ===== §3.2 の倍率表 =====

@pytest.mark.parametrize("profile,expected", [("standard", 12), ("deep", 24), ("max", 36)])
def test_scaled_turns_matches_table(profile, expected):
    assert D.scaled_turns(12, profile) == expected


@pytest.mark.parametrize("profile,expected", [("standard", 30), ("deep", 45), ("max", 60)])
def test_scaled_ratio_matches_table_grep_hits(profile, expected):
    assert D.scaled_ratio(30, profile) == expected


@pytest.mark.parametrize("profile,expected", [("standard", 40), ("deep", 60), ("max", 80)])
def test_scaled_ratio_matches_table_read_window(profile, expected):
    assert D.scaled_ratio(40, profile) == expected


@pytest.mark.parametrize("profile,expected", [("standard", 8), ("deep", 10), ("max", 12)])
def test_scaled_depth_matches_table_impact(profile, expected):
    assert D.scaled_depth(8, profile) == expected


@pytest.mark.parametrize("profile,expected", [("standard", 3), ("deep", 5), ("max", 7)])
def test_scaled_depth_matches_table_troubleshoot(profile, expected):
    assert D.scaled_depth(3, profile) == expected


@pytest.mark.parametrize("profile,expected", [("standard", "low"), ("deep", "high"), ("max", "xhigh")])
def test_codex_reasoning_for_matches_table(profile, expected):
    assert D.codex_reasoning_for("low", profile) == expected


def test_codex_reasoning_for_standard_keeps_arbitrary_base():
    """標準は基準値をそのまま返す（author 専用の別 env 既定でも上書きしない）。"""
    assert D.codex_reasoning_for("medium", "standard") == "medium"


def test_ratio_truncates_fractional_result():
    """×1.5 の切り捨て（例: 奇数の基準値）。"""
    assert D.scaled_ratio(15, "deep") == 22   # 15*1.5=22.5 -> 22


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


# ===== abs_max（倍率適用後の絶対上限）=====

def test_scaled_ratio_abs_max_clamps_after_multiplication():
    """管理APIのbase上限（例: grepヒット上限1000）×最大倍率(×2)=2000 のような組み合わせでも、
    abs_max を渡せば一度だけ最終値をクランプできる。"""
    assert D.scaled_ratio(1000, "max", abs_max=1000) == 1000   # 2000 -> 1000 にクランプ
    assert D.scaled_ratio(1000, "deep", abs_max=1000) == 1000  # 1500 -> 1000 にクランプ
    assert D.scaled_ratio(1000, "standard", abs_max=1000) == 1000   # 1000 のまま（クランプ不要）


def test_scaled_ratio_abs_max_does_not_affect_values_already_within_bound():
    """既定の基準値（env 既定）では abs_max=1000 を渡しても標準/深く/最大とも従来の値のまま
    （通常構成ではクランプが一切効かない・既存挙動を壊さない）。"""
    assert D.scaled_ratio(30, "standard", abs_max=1000) == 30
    assert D.scaled_ratio(30, "deep", abs_max=1000) == 45
    assert D.scaled_ratio(30, "max", abs_max=1000) == 60


def test_scaled_ratio_abs_max_omitted_keeps_existing_behavior():
    """abs_max 省略（既定 None）は従来どおりクランプなし（既存呼び出し元は無変更）。"""
    assert D.scaled_ratio(1000, "max") == 2000


def test_scaled_depth_abs_max_clamps_after_addition():
    """管理APIのbase上限（例: 影響深さ64）＋最大の加算(+4)=68 のような組み合わせでも、abs_max を
    渡せば一度だけ最終値をクランプできる。"""
    assert D.scaled_depth(64, "max", abs_max=64) == 64   # 68 -> 64 にクランプ
    assert D.scaled_depth(64, "deep", abs_max=64) == 64  # 66 -> 64 にクランプ
    assert D.scaled_depth(60, "max", abs_max=64) == 64   # 64 ちょうど（クランプ境界）


def test_scaled_depth_abs_max_does_not_affect_values_already_within_bound():
    assert D.scaled_depth(8, "standard", abs_max=64) == 8
    assert D.scaled_depth(8, "deep", abs_max=64) == 10
    assert D.scaled_depth(8, "max", abs_max=64) == 12


def test_scaled_depth_abs_max_omitted_keeps_existing_behavior():
    assert D.scaled_depth(64, "max") == 68


# ===== STAT-3 S1（利用統計の拡充）: usage メタへ足す depth 由来のキー =====

@pytest.mark.parametrize("profile,expected", [("standard", 12), ("deep", 24), ("max", 36)])
def test_effective_max_turns_composes_effective_base_and_scaled_turns(profile, expected):
    """`effective_base(...,"max_turns",12)` → `scaled_turns(...)` と同じ結果（単一の真実源）。"""
    assert D.effective_max_turns(None, 12, profile) == expected
    assert D.effective_max_turns({"depth_base_max_turns": 12}, 1, profile) == expected


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


def test_usage_reasoning_extras_includes_base_when_overridden():
    """深く/最大は基準値を上書きする＝`reasoning_base` も残す（実効値と基準値の両方が分かる）。"""
    assert D.usage_reasoning_extras("deep", "medium", "high") == {
        "depth_profile": "deep", "reasoning": "high", "reasoning_base": "medium"}
