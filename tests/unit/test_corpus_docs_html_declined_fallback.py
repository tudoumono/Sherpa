"""HTML の分類是正（コーディネータ裁定・アナライザ拡張 波3 レーン A）: `HtmlTemplateAnalyzer.
accepts()` が拒否した `.html` は `fallback_to_text_kind_when_declined=True` により
`corpus_docs._classify_generic_text` の通常の内容推定（`ingest.text_kind`）へ回る——
アプリ画面の目印（フォーム/script等）が無い Word 保存 HTML 相当（日本語本文主体）は資料側、
目印があるフォーム付き画面 HTML は `HtmlTemplateAnalyzer` が受理してコード（Module）側に
分類される。`HtmlTemplateAnalyzer` は本作業（波3 レーン A）で新規作成した未登録アナライザのため、
`registry._ANALYZERS` を monkeypatch する（`test_corpus_docs_analyzer_registry.py` と同じ流儀）。
"""
from __future__ import annotations

from sherpa import corpus_docs, worlds
from sherpa.ingest import text_kind
from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers.html import HtmlTemplateAnalyzer


def _register_html(monkeypatch):
    monkeypatch.setattr(registry, "_ANALYZERS", (HtmlTemplateAnalyzer(),))


def test_word_saved_html_without_markers_is_classified_as_document(monkeypatch):
    _register_html(monkeypatch)
    text = ("会議事録\n\n出席者: 山田太郎、鈴木花子。本日の議題について説明する。"
           "決定事項は以下の通りである。次回開催日は未定とする。\n" * 5)
    result = corpus_docs.classify_document("memo.html", ".html", lambda size=4096: text)
    assert result["kind"] == "document"
    assert result["had_code_candidates"] is True
    assert result["doctype"] != "html"


def test_form_html_is_accepted_and_classified_as_code_module(monkeypatch):
    _register_html(monkeypatch)
    text = '<html><body><form action="login" method="post"><input name="u"></form></body></html>'
    result = corpus_docs.classify_document("login.html", ".html", lambda size=4096: text)
    assert result["kind"] == "code"
    assert result["analyzer"] is not None and result["analyzer"].doctype == "html"


def test_form_marker_beyond_4kib_head_is_still_classified_as_code_module(monkeypatch):
    """`HtmlTemplateAnalyzer.head_bytes`（64KiB）に従い、`registry.resolve_lazy`
    は accepts() へ先頭64KiBを渡す——目印（`<form>`）が先頭4KiB（既定の `_read_head` サイズ）を
    超えた位置にあっても、正しくコード（Module）に分類される（`read_head` は実際のファイル読み
    取りと同じく `size` に応じて切り詰めるフェイク＝広い head を渡さなければ再現しないバグの固定）。"""
    _register_html(monkeypatch)
    padding = "x" * 5000
    text = f"<!-- {padding} -->\n<form action=\"/save\"></form>\n"

    def read_head(size=4096):
        return text[:size]

    result = corpus_docs.classify_document("big.html", ".html", read_head)
    assert result["kind"] == "code"
    assert result["analyzer"] is not None and result["analyzer"].doctype == "html"


def test_form_marker_beyond_64kib_byte_boundary_is_declined(monkeypatch, tmp_path):
    """`_read_head` はバイト単位で厳密に `size` を切る——`"あ" * 65520`（65520文字＝196,560バイト）の
    直後に `<form>` を置くと、先頭64KiB（65536バイト）には目印が入らない（バイト境界の外）ため
    decline する。旧実装（`size` 文字読む＝マルチバイト文字で実際の範囲が広がる）だと同じ内容が
    誤って64KiB以内に収まり accept してしまっていた（本テストが固定する回帰・`ingest.world_graph`
    の同じ判定と一致することは `test_world_graph_html_head_bytes.py` 側で固定する）。"""
    _register_html(monkeypatch)
    text = "あ" * 65520 + '<form action="/save"></form>'
    p = tmp_path / "big.html"
    p.write_text(text, encoding="utf-8")

    result = corpus_docs.classify_document(
        "big.html", ".html", lambda size=4096: corpus_docs._read_head(p, size))
    assert result["kind"] == "document"
    assert result["doctype"] != "html"


def test_status_doctype_agrees_with_ledger_for_declined_html_without_extra_io(monkeypatch, tmp_path):
    """`status_document_doctype()`（`allow_content_sniff=False`・原本DL/状態APIが
    使う経路）は、accepts() 用に既に読んだ head をそのまま内容推定へ再利用する——追加の
    `read_head()` 呼び出し（status 経路では `documents.resolve`→`worlds.world_dir` の再解決を
    伴う）を発生させずに、台帳（`world_documents`）と同じ「テキスト資料」判定へ一致する
    （画面目印の無い日本語本文主体の HTML）。"""
    _register_html(monkeypatch)
    wd = tmp_path / "world"
    wd.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    text = ("会議事録\n\n出席者: 山田太郎、鈴木花子。本日の議題について説明する。"
           "決定事項は以下の通りである。次回開催日は未定とする。\n" * 5)
    (wd / "memo.html").write_text(text, encoding="utf-8")

    docs = corpus_docs.world_documents("w")
    assert [d["name"] for d in docs] == ["memo.html"]
    assert docs[0]["doctype"] == text_kind.DOCUMENT_DOCTYPE_LABEL

    reads: list = []
    orig_read_head = corpus_docs._read_head

    def _tracking_read_head(rp, size=4096):
        reads.append((rp.name, size))
        return orig_read_head(rp, size)

    monkeypatch.setattr(corpus_docs, "_read_head", _tracking_read_head)

    assert corpus_docs.status_document_doctype("memo.html", "w") == text_kind.DOCUMENT_DOCTYPE_LABEL
    # accepts() 判定用の1回だけ読む（64KiB・`HtmlTemplateAnalyzer.head_bytes`）——内容推定の
    # ための追加読み取りは発生しない。
    assert reads == [("memo.html", 65536)]


def test_binary_looking_html_without_markers_is_not_forced_into_document(monkeypatch):
    """`declined_allows_text_kind` は早期returnを外すだけで、通常の内容推定の判定結果
    （バイナリ/コード寄り）まで document に固定するわけではない。"""
    _register_html(monkeypatch)
    text = "\x00\x01binary-looking-content\x00"
    result = corpus_docs.classify_document("blob.html", ".html", lambda size=4096: text)
    assert result["kind"] == "document"           # sniff_content の "binary" 判定も document 扱い
    assert result["doctype"] is None
