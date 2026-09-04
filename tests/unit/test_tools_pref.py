"""検索経路トグル（`sherpa.tools_pref`）の単体テスト（調べ方ブロック §3.6・SC-6e）。

- `normalize_tools_pref`: 省略（None）は全ON・未知キー/非bool値/3つとも False は ValueError（fail-loud）。
- `is_default`: 出典0件案内（`chat_service._retry_hints`）が使うヘルパの契約。
"""
from __future__ import annotations

import pytest

from sherpa import tools_pref as T


def test_normalize_tools_pref_omitted_defaults_to_all_on():
    assert T.normalize_tools_pref(None) == {"grep": True, "fulltext": True, "graph": True}


def test_normalize_tools_pref_partial_dict_fills_missing_keys_with_true():
    assert T.normalize_tools_pref({"grep": False}) == {"grep": False, "fulltext": True, "graph": True}


def test_normalize_tools_pref_all_false_raises():
    with pytest.raises(ValueError):
        T.normalize_tools_pref({"grep": False, "fulltext": False, "graph": False})


def test_normalize_tools_pref_unknown_key_raises():
    with pytest.raises(ValueError):
        T.normalize_tools_pref({"grep": True, "bogus": True})


def test_normalize_tools_pref_non_bool_value_raises():
    with pytest.raises(ValueError):
        T.normalize_tools_pref({"grep": "yes"})


def test_normalize_tools_pref_non_dict_raises():
    with pytest.raises(ValueError):
        T.normalize_tools_pref("grep")


def test_is_default_true_for_omitted_or_all_on():
    assert T.is_default(None) is True
    assert T.is_default({"grep": True, "fulltext": True, "graph": True}) is True


def test_is_default_false_when_any_off():
    assert T.is_default({"grep": False, "fulltext": True, "graph": True}) is False
