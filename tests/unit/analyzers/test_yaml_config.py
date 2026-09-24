"""`YamlConfigAnalyzer` の単体テスト（アナライザ拡張 S3'・A7 案B＝キー単位 `Config` children）。"""
from __future__ import annotations

from sherpa.ingest.analyzers.yaml_config import YamlConfigAnalyzer

A = YamlConfigAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".yaml", ".yml"})
    assert A.name == "yaml_config"
    assert A.doctype == "yaml_config"


def test_collect_defs_returns_file_itself_as_config_primary():
    text = "server:\n  port: 8080\n"
    res = A.collect_defs(text, "config/config.yml")
    assert res.primary is not None
    assert res.primary.label == "Config" and res.primary.name == "config.yml"


def test_collect_defs_works_for_yaml_extension_too():
    res = A.collect_defs("a: 1\n", "settings.yaml")
    assert res.primary.name == "settings.yaml"


def test_collect_defs_builds_dotted_full_key_children_from_indentation_hierarchy():
    """RV2-2: children のラベルは親と同じ `Config`（`DataItem` にしない）。`name` は階層を `.` で
    連結した裸の完全キーのまま——中間のマップ見出し（値なしの `key:`）自体は children に出さない。
    `cid_key` は `"key:"+key_kind+":"` 接頭辞付き（properties.py と同じ理由＝primary との cid
    衝突回避・key_kind を挟むのは他 producer との cid 衝突回避）。"""
    text = "tax:\n  rate: 0.08\nbatch:\n  plugin: csv-importer\n"
    res = A.collect_defs(text, "config.yml")
    assert [c.label for c in res.children] == ["Config", "Config"]
    assert [c.name for c in res.children] == ["tax.rate", "batch.plugin"]
    assert [c.cid_key for c in res.children] == ["key:property:tax.rate", "key:property:batch.plugin"]
    assert res.dropped == []


def test_collect_defs_key_kind_is_property():
    """Config キーは種別（`key_kind`）で名前空間を分ける（裁定2026-09-06）——YAML のキーも
    properties.py と同じく常に `"property"`（共通層 A9 の `config_key_index` はこの値も索引キーに
    含める）。"""
    text = "tax:\n  rate: 0.08\n"
    res = A.collect_defs(text, "config.yml")
    assert res.children[0].extra["key_kind"] == "property"


def test_collect_defs_sequence_items_are_excluded_and_truncate_the_hierarchy():
    text = "batch:\n  plugin: csv-importer\n  sources:\n    - a.csv\n    - b.csv\nafter: ok\n"
    res = A.collect_defs(text, "config.yml")
    names = [c.name for c in res.children]
    assert names == ["batch.plugin", "after"]             # sources/シーケンス項目は出ない
    assert res.dropped == []


def test_collect_defs_sequence_item_nested_mapping_is_also_excluded():
    """シーケンス項目自身が入れ子のマップを持つ場合（`- host: a` に続く `  port: 80`）も、
    基準インデントより深い配下は丸ごと読み飛ばす——`servers.port` を作らない。"""
    text = "servers:\n  - host: a\n    port: 80\n  - host: b\n    port: 90\nafter: ok\n"
    res = A.collect_defs(text, "config.yml")
    names = [c.name for c in res.children]
    assert names == ["after"]
    assert res.dropped == []


def test_collect_defs_tab_indented_line_is_flagged_yaml_unsupported():
    text = "\tkey: value\nafter: ok\n"
    res = A.collect_defs(text, "config.yml")
    assert [c.name for c in res.children] == ["after"]
    assert [d.reason for d in res.dropped] == ["yaml_unsupported"]
    assert res.dropped[0].line == 1


def test_collect_defs_tab_indented_sequence_item_is_flagged_yaml_unsupported():
    """タブ字下げのシーケンス項目（`\\t- a`）もキーとして解釈せず `Dropped("yaml_unsupported")` で
    申告する——タブ判定をシーケンス分岐より先に行うため、`skip_seq_indent` 経由で黙って
    読み飛ばされない。"""
    text = "servers:\n\t- a\nafter: ok\n"
    res = A.collect_defs(text, "config.yml")
    assert [c.name for c in res.children] == ["after"]
    assert [d.reason for d in res.dropped] == ["yaml_unsupported"]
    assert res.dropped[0].line == 2


def test_collect_defs_flow_collection_value_is_flagged_yaml_unsupported():
    text = "servers: {a: 1}\nafter: ok\n"
    res = A.collect_defs(text, "config.yml")
    assert [c.name for c in res.children] == ["after"]
    assert [d.reason for d in res.dropped] == ["yaml_unsupported"]
    assert res.dropped[0].line == 1


def test_collect_defs_second_yaml_document_is_flagged_and_not_parsed():
    """複数ドキュメントの2個目以降は `Dropped("yaml_unsupported")` で1回申告し、階層に入れない
    （最初の `---` はファイル先頭の明示的な文書開始として無視する）。"""
    text = "a: 1\n---\nb: 2\n"
    res = A.collect_defs(text, "config.yml")
    assert [c.name for c in res.children] == ["a"]
    assert [(d.reason, d.line) for d in res.dropped] == [("yaml_unsupported", 2)]


def test_collect_defs_anchor_marker_inside_quoted_scalar_is_not_flagged():
    """アンカー/エイリアス判定は引用スカラーの外だけで行う——`message: "foo &bar"` は通常キー。"""
    text = 'message: "foo &bar"\n'
    res = A.collect_defs(text, "config.yml")
    assert [c.name for c in res.children] == ["message"]
    assert res.dropped == []


def test_collect_defs_map_header_without_scalar_value_is_not_a_leaf_child():
    text = "server:\n  port: 8080\n"
    res = A.collect_defs(text, "config.yml")
    names = [c.name for c in res.children]
    assert "server" not in names
    assert names == ["server.port"]


def test_collect_defs_quoted_keys_are_dequoted():
    text = '"db.url": jdbc:x\n\'db.user\': admin\n'
    res = A.collect_defs(text, "config.yml")
    assert [c.name for c in res.children] == ["db.url", "db.user"]


def test_collect_defs_document_separator_is_ignored():
    text = "---\ndb:\n  url: jdbc:x\n"
    res = A.collect_defs(text, "config.yml")
    assert [c.name for c in res.children] == ["db.url"]


def test_collect_defs_anchor_and_alias_are_flagged_yaml_unsupported():
    text = "base: &default value\nother: *default\n"
    res = A.collect_defs(text, "config.yml")
    assert res.children == []
    reasons = [(d.reason, d.line) for d in res.dropped]
    assert reasons == [("yaml_unsupported", 1), ("yaml_unsupported", 2)]


def test_collect_defs_block_scalar_is_flagged_yaml_unsupported_and_body_lines_are_skipped():
    text = "notes: |\n  line one\n  line two\nafter: ok\n"
    res = A.collect_defs(text, "config.yml")
    assert [d.reason for d in res.dropped] == ["yaml_unsupported"]
    assert res.dropped[0].line == 1
    assert [c.name for c in res.children] == ["after"]     # 本文行はキーとして誤読されない


def test_extract_refs_returns_nothing():
    res = A.extract_refs("server:\n  port: 8080\n", "config.yml")
    assert res.refs == [] and res.dropped == []
