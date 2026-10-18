"""es_search の path_prefix は、ターンの範囲の内側へ狭めるだけで、範囲を広げない。"""
from sherpa.parts.read.tools import _narrow_scope_by_prefix


def test_prefix_inside_scope_narrows_to_prefix():
    assert _narrow_scope_by_prefix(["営業"], "営業/2024年度") == (["営業/2024年度"], None)


def test_prefix_without_turn_scope_becomes_the_scope():
    assert _narrow_scope_by_prefix([], "総務/規程") == (["総務/規程"], None)


def test_prefix_wider_than_scope_keeps_only_scopes_under_it():
    assert _narrow_scope_by_prefix(["営業/2024年度", "経理"], "営業") == (["営業/2024年度"], None)


def test_prefix_outside_scope_or_traversal_is_rejected():
    for bad in ("人事", "../人事", "/etc", "営業\\x"):
        sp, err = _narrow_scope_by_prefix(["営業"], bad)
        assert err and sp == ["営業"]


def test_empty_prefix_keeps_scope():
    assert _narrow_scope_by_prefix(["営業"], "") == (["営業"], None)
