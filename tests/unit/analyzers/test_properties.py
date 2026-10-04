"""`PropertiesAnalyzer` の単体テスト（アナライザ拡張 S3'・A7 案B＝キー単位 `Config` children）。"""
from __future__ import annotations

import pytest

from sherpa.ingest.analyzers.properties import PropertiesAnalyzer

A = PropertiesAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".properties"})
    assert A.name == "properties"
    assert A.doctype == "properties"


def test_collect_defs_returns_file_itself_as_config_primary():
    res = A.collect_defs("db.url=jdbc:postgresql://localhost/db\ndb.user=admin\n", "config/app.properties")
    assert res.primary is not None and res.primary.label == "Config" and res.primary.name == "app.properties"
    assert res.dropped == []


def test_collect_defs_extracts_keys_as_config_children_with_bare_key_index():
    """children は親と同じ `Config` ラベル・`name` は裸のキー・`cid_key` は `key:<key_kind>:` 接頭辞付き
    （primary や他 producer の bean/action 等との cid 衝突回避・key_kind は常に `property`）。"""
    res = A.collect_defs("db.url=jdbc:postgresql://localhost/db\ndb.user=admin\n", "config/app.properties")
    assert [c.label for c in res.children] == ["Config", "Config"]
    assert [c.name for c in res.children] == ["db.url", "db.user"]
    assert [c.cid_key for c in res.children] == ["key:property:db.url", "key:property:db.user"]
    assert [c.key for c in res.children] == ["key:property:db.url", "key:property:db.user"]
    assert all(c.extra["key_kind"] == "property" for c in res.children)


# (入力, [(name, config_value|None)])
CASES = {
    "colon_and_space_separators": ("db.url: jdbc:postgresql://localhost/db\ndb.user admin\n",
                                   [("db.url", None), ("db.user", None)]),
    "comments_and_blank_lines_ignored": ("# comment\n\n! bang comment\ndb.url=jdbc:x\n", [("db.url", None)]),
    "duplicate_key_keeps_first": ("tax.rate=0.08\ntax.rate=0.99\n", [("tax.rate", "0.08")]),
    "line_continuation_joined": ("long.note=first part \\\n    second part\n", [("long.note", "first part second part")]),
    "escaped_trailing_backslash_does_not_continue": ("path=C:\\\\\nnext.key=value\n", [("path", None), ("next.key", None)]),
    "unicode_escape_kept_verbatim": ("greeting=\\u3053\\u3093\\u306b\\u3061\\u306f\n",
                                     [("greeting", "\\u3053\\u3093\\u306b\\u3061\\u306f")]),
    "leading_bom_stripped": ("\ufeffdb.url=jdbc:x\n", [("db.url", None)]),
    "escaped_separator_in_key": ("key\\=with\\=escape=value\n", [("key=with=escape", "value")]),
}


@pytest.mark.parametrize("text,expected", CASES.values(), ids=CASES)
def test_collect_defs_children(text, expected):
    res = A.collect_defs(text, "app.properties")
    assert [c.name for c in res.children] == [n for n, _v in expected]
    for c, (_n, value) in zip(res.children, expected):
        assert value is None or c.extra["config_value"] == value


def test_duplicate_and_continuation_report_first_physical_line():
    assert A.collect_defs("tax.rate=0.08\ntax.rate=0.99\n", "a.properties").children[0].line == 1
    assert A.collect_defs("long.note=first part \\\n    second part\n", "a.properties").children[0].line == 1


def test_value_truncated_to_200_chars_and_kept_in_extra_only():
    res = A.collect_defs("big.value=" + ("x" * 500) + "\n", "app.properties")
    assert len(res.children[0].extra["config_value"]) == 200
    assert res.children[0].value is None


def test_extract_refs_returns_nothing():
    res = A.extract_refs("db.url=jdbc:postgresql://localhost/db\n", "app.properties")
    assert res.refs == [] and res.dropped == []
