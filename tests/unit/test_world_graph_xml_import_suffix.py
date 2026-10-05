"""`<import resource="...">`（`xml_config.py`）の解決は共通層のパス末尾一致
（`world_graph._resolve_path_suffix`）で行う——ファイル名（basename）だけの同一 top_scope 内
最近傍（旧実装）は、世界内に同名の別ファイルがあると誤接続し得るための RV 是正。

Codex RV（1巡目）是正: import 先の誤接続防止・あいまい時は `config_import_ambiguous`・
`resource` 属性無しは `config_import_missing_resource`（後者は `tests/unit/analyzers/
test_xml_config.py` 側で確認済み・本ファイルは共通層のパス解決だけを対象にする）。
"""
from __future__ import annotations

from sherpa.ingest import world_graph


def _write(tmp_path, rel: str, text: str):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return (p, rel)


def test_import_resolves_by_path_suffix_and_ignores_same_named_file_elsewhere(tmp_path):
    """同じ basename（`common.xml`）の別ファイルが同一世界内の別ディレクトリに在っても、
    `<import resource>` の指定パス（`META-INF/spring/common.xml`）と末尾一致する方だけへ
    接続する——basename だけの最近傍解決なら距離次第で decoy へ誤接続しうる構成。"""
    files = [
        _write(tmp_path, "project/app/spring-mvc.xml",
              '<beans>\n  <import resource="classpath:/META-INF/spring/common.xml"/>\n</beans>\n'),
        _write(tmp_path, "project/app/src/main/resources/META-INF/spring/common.xml",
              "<beans></beans>\n"),
        # decoy: 同じ basename だが指定パス（META-INF/spring/common.xml）とは末尾一致しない。
        # `project/app` からの tree 距離は正解より近い（誤接続の罠）。
        _write(tmp_path, "project/app/common.xml", "<beans></beans>\n"),
    ]
    nodes, edges, flags = world_graph.build_world(tmp_path, "w", files=files)
    by_cid = {n["cid"]: n for n in nodes}
    invokes = [e for e in edges if e["type"] == "INVOKES" and e.get("via") == "include"]
    assert len(invokes) == 1
    dst = by_cid[invokes[0]["dst"]]
    assert dst["path"] == "project/app/src/main/resources/META-INF/spring/common.xml"
    assert not [f for f in flags if f.get("reason") in
               ("config_import_ambiguous", "config_import_unresolved")]


def test_import_with_ambiguous_path_suffix_does_not_connect(tmp_path):
    """同一 top_scope 内に、指定パスと末尾一致する候補が2つ在れば一意に決まらない——
    推測接続せず `config_import_ambiguous` を flags へ記録する。"""
    files = [
        _write(tmp_path, "project/app/spring-mvc.xml",
              '<beans>\n  <import resource="classpath:/META-INF/spring/common.xml"/>\n</beans>\n'),
        _write(tmp_path, "project/mod1/src/main/resources/META-INF/spring/common.xml",
              "<beans></beans>\n"),
        _write(tmp_path, "project/mod2/src/main/resources/META-INF/spring/common.xml",
              "<beans></beans>\n"),
    ]
    nodes, edges, flags = world_graph.build_world(tmp_path, "w", files=files)
    invokes = [e for e in edges if e["type"] == "INVOKES" and e.get("via") == "include"]
    assert invokes == []
    ambiguous = [f for f in flags if f.get("reason") == "config_import_ambiguous"]
    assert len(ambiguous) == 1
    assert ambiguous[0]["name"] == "META-INF/spring/common.xml"
    assert ambiguous[0]["from"] == "project/app/spring-mvc.xml"
