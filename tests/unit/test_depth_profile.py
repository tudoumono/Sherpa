"""調べる深さ（`sherpa.depth_profile`）の単体テスト（調べ方ブロック §3.2・SC-6c。倍率撤去は
DEPTH-2 S7・`docs/proposals/2026-09-17-深さの再定義とレビュー巡.md` §2.3・§5）。

- `scaled_turns`/`scaled_ratio`/`scaled_depth`/`codex_reasoning_for`: DEPTH-2 S7 以降は
  深さ（standard/deep/max）に依らず基準値をそのまま返す（倍率・加算・推論レベル上書きは撤去）。
  `abs_max` の絶対上限は引き続き効く。不正な depth_profile は fail-loud のまま。
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


# ===== DEPTH-2 S7: 倍率撤去＝深さに依らず基準値をそのまま返す =====

@pytest.mark.parametrize("profile", ["standard", "deep", "max"])
def test_scaled_turns_returns_base_regardless_of_depth(profile):
    """深く/最大でも上限が変わらない（倍率撤去・受け入れ条件(1)）。"""
    assert D.scaled_turns(12, profile) == 12


@pytest.mark.parametrize("profile", ["standard", "deep", "max"])
def test_scaled_ratio_returns_base_regardless_of_depth_grep_hits(profile):
    assert D.scaled_ratio(30, profile) == 30


@pytest.mark.parametrize("profile", ["standard", "deep", "max"])
def test_scaled_ratio_returns_base_regardless_of_depth_read_window(profile):
    assert D.scaled_ratio(40, profile) == 40


@pytest.mark.parametrize("profile", ["standard", "deep", "max"])
def test_scaled_depth_returns_base_regardless_of_depth_impact(profile):
    assert D.scaled_depth(8, profile) == 8


@pytest.mark.parametrize("profile", ["standard", "deep", "max"])
def test_scaled_depth_returns_base_regardless_of_depth_troubleshoot(profile):
    assert D.scaled_depth(3, profile) == 3


@pytest.mark.parametrize("profile", ["standard", "deep", "max"])
def test_codex_reasoning_for_returns_base_regardless_of_depth(profile):
    """深く=high／最大=xhigh への per-turn 上書きは撤去——常に基準値を返す（受け入れ条件(2)）。"""
    assert D.codex_reasoning_for("low", profile) == "low"


def test_codex_reasoning_for_standard_keeps_arbitrary_base():
    """標準は基準値をそのまま返す（author 専用の別 env 既定でも上書きしない）。"""
    assert D.codex_reasoning_for("medium", "standard") == "medium"


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


# ===== abs_max（絶対上限。倍率は撤去したが abs_max のクランプ自体は引き続き効く）=====

def test_scaled_ratio_abs_max_clamps_base_over_limit():
    """倍率が無くなった後も、基準値そのものが abs_max を超える構成（管理画面での手動設定）は
    最終的に abs_max へクランプされる（深さに依らない・受け入れ条件(1)）。"""
    assert D.scaled_ratio(2000, "max", abs_max=1000) == 1000
    assert D.scaled_ratio(2000, "deep", abs_max=1000) == 1000
    assert D.scaled_ratio(2000, "standard", abs_max=1000) == 1000


def test_scaled_ratio_abs_max_does_not_affect_values_already_within_bound():
    """基準値が abs_max 以内なら深さに依らず基準値のまま。"""
    assert D.scaled_ratio(30, "standard", abs_max=1000) == 30
    assert D.scaled_ratio(30, "deep", abs_max=1000) == 30
    assert D.scaled_ratio(30, "max", abs_max=1000) == 30


def test_scaled_ratio_abs_max_omitted_keeps_existing_behavior():
    """abs_max 省略（既定 None）はクランプなし（既存呼び出し元は無変更）。"""
    assert D.scaled_ratio(2000, "max") == 2000


def test_scaled_depth_abs_max_clamps_base_over_limit():
    assert D.scaled_depth(68, "max", abs_max=64) == 64
    assert D.scaled_depth(68, "deep", abs_max=64) == 64
    assert D.scaled_depth(64, "max", abs_max=64) == 64   # 64 ちょうど（クランプ境界）


def test_scaled_depth_abs_max_does_not_affect_values_already_within_bound():
    assert D.scaled_depth(8, "standard", abs_max=64) == 8
    assert D.scaled_depth(8, "deep", abs_max=64) == 8
    assert D.scaled_depth(8, "max", abs_max=64) == 8


def test_scaled_depth_abs_max_omitted_keeps_existing_behavior():
    assert D.scaled_depth(68, "max") == 68


# ===== STAT-3 S1（利用統計の拡充）: usage メタへ足す depth 由来のキー =====

@pytest.mark.parametrize("profile", ["standard", "deep", "max"])
def test_effective_max_turns_composes_effective_base_and_scaled_turns(profile):
    """`effective_base(...,"max_turns",12)` → `scaled_turns(...)` と同じ結果（単一の真実源）。
    DEPTH-2 S7 以降、深さに依らず基準値が素通りする（受け入れ条件(4)＝管理画面の基準値編集は
    引き続き効く）。"""
    assert D.effective_max_turns(None, 12, profile) == 12
    assert D.effective_max_turns({"depth_base_max_turns": 20}, 1, profile) == 20


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
    """`usage_reasoning_extras` 自体は与えられた2値の比較のみを行う（呼び出し元契約）。
    DEPTH-2 S7 以降、実際の呼び出し元（`codex_reasoning_for`）は常に基準値を返すため、
    深さに依らず `reasoning == reasoning_base` になり `reasoning_base` は省略される
    （受け入れ条件(3)）——本テストは関数自体の比較ロジックが差異を検出できることの確認。"""
    assert D.usage_reasoning_extras("deep", "medium", "high") == {
        "depth_profile": "deep", "reasoning": "high", "reasoning_base": "medium"}


def test_usage_reasoning_extras_omits_base_for_all_depths_via_codex_reasoning_for():
    """実際の呼び出し経路（`codex_reasoning_for` の戻り値を渡す）では、深さに依らず
    `reasoning_base` が出ない（受け入れ条件(3)＝reasoning と reasoning_base の値が矛盾しない）。"""
    for profile in ("standard", "deep", "max"):
        base = "medium"
        effective = D.codex_reasoning_for(base, profile)
        assert D.usage_reasoning_extras(profile, base, effective) == {
            "depth_profile": profile, "reasoning": "medium"}


# ---- DEPTH-2 S5: 深さ＝evaluator（査読）の巡数（§2.3）----

def test_review_rounds_standard_is_zero_and_deep_is_two():
    """標準は 0 巡（査読を一度も発動しない＝従来の標準と同じ）・深くは 2 巡（固定）。"""
    assert D.review_rounds_for("standard") == 0
    assert D.review_rounds_for(None) == 0          # 欠落は standard
    assert D.review_rounds_for("deep") == 2        # 設定に依らず固定
    assert D.review_rounds_for("deep", {"max_review_rounds": 3}) == 2


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
