"""`world_graph.build_world()` Pass1 の HTML `accepts()` head バイト境界是正（波3 統合 RV）。

`accepts()` へ渡す head は `Analyzer.head_bytes`（`HtmlTemplateAnalyzer` は64KiB）ちょうどの
**バイト**で切る——`corpus_docs.read_full_text_and_raw()` が1回のバイナリ読取で返す生バイト列を
スライスしてデコードする（再オープンなし）。旧実装（`text[:head_bytes]`
の**文字**数切り詰め）は、マルチバイト文字（日本語等）を含む文書で実際に読む範囲が宣言より
広がってしまい、64KiBバイト境界の直後にある目印（`<form>` 等）まで誤って拾って accept して
しまっていた。`corpus_docs.classify_document`（`registry.resolve_lazy` 経由）側の判定と
一致することも合わせて固定する。
"""
from __future__ import annotations

from sherpa import corpus_docs
from sherpa.ingest import world_graph
from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers.html import HtmlTemplateAnalyzer


def _register_html(monkeypatch):
    monkeypatch.setattr(registry, "_ANALYZERS", (HtmlTemplateAnalyzer(),))


def test_form_marker_beyond_64kib_byte_boundary_is_declined_by_world_graph(tmp_path, monkeypatch):
    """`"あ" * 65520`（65520文字＝196,560バイト）の直後に `<form>` を置くと、先頭64KiB
    （65536バイト）には目印が入らない（バイト境界）ため decline し、Module ノードを作らない。
    文字数切り詰めの旧実装だと同じ内容が誤って64KiB以内に収まり accept してしまっていた。"""
    _register_html(monkeypatch)
    wd = tmp_path / "world"
    wd.mkdir()
    text = "あ" * 65520 + '<form action="/save"></form>'
    (wd / "big.html").write_text(text, encoding="utf-8")

    nodes, _edges, _flags = world_graph.build_world(wd, "w")

    assert nodes == []                                    # decline＝拡張子は一致しても主体化しない


def test_form_marker_within_64kib_byte_boundary_is_accepted_by_world_graph(tmp_path, monkeypatch):
    """目印がバイト境界（64KiB）の内側にあれば、従来どおり受理されて Module ノードになる
    （上のテストが decline 側だけを固定しないための対照）。"""
    _register_html(monkeypatch)
    wd = tmp_path / "world"
    wd.mkdir()
    text = "あ" * 100 + '<form action="/save"></form>'
    (wd / "small.html").write_text(text, encoding="utf-8")

    nodes, _edges, _flags = world_graph.build_world(wd, "w")

    assert [(n["label"], n["path"]) for n in nodes] == [("Module", "small.html")]


def test_world_graph_and_classify_document_agree_on_the_byte_boundary_decline(tmp_path, monkeypatch):
    """`world_graph.build_world()`（Pass1・`corpus_docs.read_full_text_and_raw()` が返す生バイト列を
    head_bytes でスライス）と `corpus_docs.classify_document()`（`registry.resolve_lazy` 経由・
    `_read_head`）が、同じファイルに対して同じ decline 判定（バイト境界の外）で一致することを固定する。"""
    _register_html(monkeypatch)
    wd = tmp_path / "world"
    wd.mkdir()
    text = "あ" * 65520 + '<form action="/save"></form>'
    p = wd / "big.html"
    p.write_text(text, encoding="utf-8")

    nodes, _edges, _flags = world_graph.build_world(wd, "w")
    assert nodes == []

    result = corpus_docs.classify_document(
        "big.html", ".html", lambda size=4096: corpus_docs._read_head(p, size))
    assert result["kind"] == "document"
    assert result["doctype"] != "html"
