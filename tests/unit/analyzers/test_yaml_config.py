"""`YamlConfigAnalyzer` の単体テスト（アナライザ拡張 S3'・A7 案B＝キー単位 `Config` children）。"""
from __future__ import annotations

import pytest

from sherpa.ingest.analyzers.yaml_config import YamlConfigAnalyzer

A = YamlConfigAnalyzer()
UNS = "yaml_unsupported"


def test_extensions_and_name():
    assert A.extensions == frozenset({".yaml", ".yml"})
    assert A.name == "yaml_config"
    assert A.doctype == "yaml_config"


@pytest.mark.parametrize("text,path,name", [
    ("server:\n  port: 8080\n", "config/config.yml", "config.yml"),
    ("a: 1\n", "settings.yaml", "settings.yaml"),
])
def test_collect_defs_returns_file_itself_as_config_primary(text, path, name):
    p = A.collect_defs(text, path).primary
    assert p is not None and p.label == "Config" and p.name == name


def test_collect_defs_builds_dotted_full_key_children_from_indentation_hierarchy():
    """children は親と同じ `Config` ラベル・階層を `.` で連結した完全キー。中間のマップ見出し自体は children に
    出さない。`cid_key` は `key:<key_kind>:` 接頭辞付き（key_kind は常に `property`）。"""
    res = A.collect_defs("tax:\n  rate: 0.08\nbatch:\n  plugin: csv-importer\n", "config.yml")
    assert [c.label for c in res.children] == ["Config", "Config"]
    assert [c.name for c in res.children] == ["tax.rate", "batch.plugin"]
    assert [c.cid_key for c in res.children] == ["key:property:tax.rate", "key:property:batch.plugin"]
    assert all(c.extra["key_kind"] == "property" for c in res.children)
    assert res.dropped == []


# (入力, children 名, Dropped[(reason, line)])
CASES = {
    "sequence_items_excluded_and_truncate_hierarchy": (
        "batch:\n  plugin: csv-importer\n  sources:\n    - a.csv\n    - b.csv\nafter: ok\n",
        ["batch.plugin", "after"], []),
    "sequence_item_nested_mapping_excluded": (
        "servers:\n  - host: a\n    port: 80\n  - host: b\n    port: 90\nafter: ok\n", ["after"], []),
    "tab_indented_line_flagged": ("\tkey: value\nafter: ok\n", ["after"], [(UNS, 1)]),
    "tab_indented_sequence_item_flagged": ("servers:\n\t- a\nafter: ok\n", ["after"], [(UNS, 2)]),
    "flow_collection_value_flagged": ("servers: {a: 1}\nafter: ok\n", ["after"], [(UNS, 1)]),
    "second_yaml_document_flagged_not_parsed": ("a: 1\n---\nb: 2\n", ["a"], [(UNS, 2)]),
    "anchor_marker_in_quoted_scalar_not_flagged": ('message: "foo &bar"\n', ["message"], []),
    "map_header_not_a_leaf_child": ("server:\n  port: 8080\n", ["server.port"], None),
    "quoted_keys_dequoted": ('"db.url": jdbc:x\n\'db.user\': admin\n', ["db.url", "db.user"], None),
    "leading_document_separator_ignored": ("---\ndb:\n  url: jdbc:x\n", ["db.url"], None),
    "anchor_and_alias_flagged": ("base: &default value\nother: *default\n", [], [(UNS, 1), (UNS, 2)]),
    "block_scalar_flagged_and_body_skipped": ("notes: |\n  line one\n  line two\nafter: ok\n", ["after"], [(UNS, 1)]),
}


@pytest.mark.parametrize("text,names,dropped", CASES.values(), ids=CASES)
def test_collect_defs_children(text, names, dropped):
    res = A.collect_defs(text, "config.yml")
    assert [c.name for c in res.children] == names
    if dropped is not None:
        assert [(d.reason, d.line) for d in res.dropped] == dropped


def test_extract_refs_returns_nothing():
    res = A.extract_refs("server:\n  port: 8080\n", "config.yml")
    assert res.refs == [] and res.dropped == []
