"""`PropertiesAnalyzer` の単体テスト（アナライザ拡張 S3'・A7 案B＝キー単位 `Config` children）。"""
from __future__ import annotations

from sherpa.ingest.analyzers.properties import PropertiesAnalyzer

A = PropertiesAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".properties"})
    assert A.name == "properties"
    assert A.doctype == "properties"


def test_collect_defs_returns_file_itself_as_config_primary():
    text = "db.url=jdbc:postgresql://localhost/db\ndb.user=admin\n"
    res = A.collect_defs(text, "config/app.properties")
    assert res.primary is not None
    assert res.primary.label == "Config" and res.primary.name == "app.properties"
    assert res.dropped == []


def test_collect_defs_extracts_keys_as_config_children_with_bare_key_index():
    """RV2-2: children のラベルは親と同じ `Config`（`DataItem` にしない）。`name`（表示名・
    構造参照/言及辞書の索引）は裸のキーのまま。`cid_key` は `"key:"+key_kind+":"` 接頭辞付きの
    値——primary（ファイル名）と cid の名前空間を分離し、かつ他 key_kind の producer（xml_config
    の bean/action 等）との cid 衝突も避けるため（同名衝突回避）。"""
    text = "db.url=jdbc:postgresql://localhost/db\ndb.user=admin\n"
    res = A.collect_defs(text, "config/app.properties")
    assert [c.label for c in res.children] == ["Config", "Config"]
    assert [c.name for c in res.children] == ["db.url", "db.user"]
    assert [c.cid_key for c in res.children] == ["key:property:db.url", "key:property:db.user"]
    assert [c.key for c in res.children] == ["key:property:db.url", "key:property:db.user"]  # `.key` は cid_key 優先


def test_collect_defs_key_kind_is_property():
    """Config キーは種別（`key_kind`）で名前空間を分ける（裁定2026-09-06）——properties のキーは
    常に `"property"`（共通層 A9 の `config_key_index` はこの値も索引キーに含める）。"""
    text = "db.url=jdbc:postgresql://localhost/db\n"
    res = A.collect_defs(text, "app.properties")
    assert res.children[0].extra["key_kind"] == "property"


def test_collect_defs_supports_colon_and_bare_space_separators():
    text = "db.url: jdbc:postgresql://localhost/db\ndb.user admin\n"
    res = A.collect_defs(text, "app.properties")
    assert [c.name for c in res.children] == ["db.url", "db.user"]


def test_collect_defs_ignores_hash_and_bang_comments_and_blank_lines():
    text = "# comment\n\n! bang comment\ndb.url=jdbc:x\n"
    res = A.collect_defs(text, "app.properties")
    assert [c.name for c in res.children] == ["db.url"]


def test_collect_defs_duplicate_key_keeps_only_the_first_occurrence():
    text = "tax.rate=0.08\ntax.rate=0.99\n"
    res = A.collect_defs(text, "app.properties")
    assert len(res.children) == 1
    assert res.children[0].name == "tax.rate"
    assert res.children[0].extra["config_value"] == "0.08"
    assert res.children[0].line == 1


def test_collect_defs_line_continuation_joins_into_one_logical_line():
    text = "long.note=first part \\\n    second part\n"
    res = A.collect_defs(text, "app.properties")
    assert [c.name for c in res.children] == ["long.note"]
    assert res.children[0].extra["config_value"] == "first part second part"
    assert res.children[0].line == 1                     # 来歴は先頭物理行


def test_collect_defs_escaped_trailing_backslash_does_not_continue():
    """行末が偶数個の `\\`（＝末尾がエスケープ済みの `\\` 自体）は継続しない。"""
    text = "path=C:\\\\\nnext.key=value\n"
    res = A.collect_defs(text, "app.properties")
    assert [c.name for c in res.children] == ["path", "next.key"]


def test_collect_defs_value_is_truncated_to_200_chars_and_kept_in_extra_only():
    text = "big.value=" + ("x" * 500) + "\n"
    res = A.collect_defs(text, "app.properties")
    assert len(res.children[0].extra["config_value"]) == 200
    assert res.children[0].value is None                 # `DefItem.value` は使わない（extra のみ）


def test_collect_defs_unicode_escape_is_kept_verbatim_not_decoded():
    text = "greeting=\\u3053\\u3093\\u306b\\u3061\\u306f\n"
    res = A.collect_defs(text, "app.properties")
    assert res.children[0].extra["config_value"] == "\\u3053\\u3093\\u306b\\u3061\\u306f"


def test_collect_defs_leading_bom_is_stripped_before_reading():
    text = "\ufeffdb.url=jdbc:x\n"
    res = A.collect_defs(text, "app.properties")
    assert [c.name for c in res.children] == ["db.url"]


def test_collect_defs_escaped_separator_in_key_is_unescaped_and_not_treated_as_separator():
    """`key\\=with\\=escape=value` → キー `key=with=escape`（アンエスケープ済み）・値は `value`。
    エスケープされた `=` は区切りとして扱わない（未エスケープの最初の `=` が本当の区切り）。"""
    text = "key\\=with\\=escape=value\n"
    res = A.collect_defs(text, "app.properties")
    assert [c.name for c in res.children] == ["key=with=escape"]
    assert res.children[0].extra["config_value"] == "value"


def test_extract_refs_returns_nothing():
    text = "db.url=jdbc:postgresql://localhost/db\n"
    res = A.extract_refs(text, "app.properties")
    assert res.refs == [] and res.dropped == []
