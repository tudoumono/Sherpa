"""`world_graph.build_world()` Pass1 は全文と head を同一のバイナリ読み取りから得る（波3 3巡目 RV・高#3）。

旧実装は全文を `rp.read_text()` で読んだ後、`accepts()` 判定用の head を `corpus_docs._read_head()`
経由で**再度ファイルを開いて**読み直していた——全文読取と head 読取の間でファイルが消える/権限が
変わるなどの失敗が起きると、既に読めた全文を握りつぶし、head 側だけ「読み取り失敗」を空文字
（＝目印なし＝不採用）へ静かに丸めてしまっていた（silent decline）。

`corpus_docs.read_full_text_and_raw()` は1回のバイナリ読み取りから全文と生バイト列の両方を返し、
head は生バイト列をスライス・デコードするだけ（追加I/Oなし）にした——再オープン自体が無くなった
ため、head だけを失敗させる monkeypatch はもう Pass1 に影響しない。全文読取自体の失敗は従来どおり
`unreadable_code_file` の blocked flag になる。
"""
from __future__ import annotations

from sherpa import corpus_docs
from sherpa.ingest import world_graph
from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers.html import HtmlTemplateAnalyzer


def _register_html(monkeypatch):
    monkeypatch.setattr(registry, "_ANALYZERS", (HtmlTemplateAnalyzer(),))


def test_head_only_read_failure_no_longer_reachable_from_pass1(tmp_path, monkeypatch):
    """`corpus_docs._read_head` を丸ごと失敗させても、Pass1 はもうこの関数を呼ばない
    （`read_full_text_and_raw()` が返す生バイト列をスライスするだけ）ので、accepts() 判定は
    通常どおり成立し Module ノードが作られる。"""
    _register_html(monkeypatch)
    wd = tmp_path / "world"
    wd.mkdir()
    (wd / "ok.html").write_text('<form action="/save"></form>', encoding="utf-8")

    def _boom(rp, size=4096):
        raise corpus_docs._HeadUnreadable("boom")
    monkeypatch.setattr(corpus_docs, "_read_head", _boom)

    nodes, _edges, flags = world_graph.build_world(wd, "w")

    assert [(n["label"], n["path"]) for n in nodes] == [("Module", "ok.html")]
    assert all(f.get("reason") != "unreadable_code_file" for f in flags)


def test_full_read_failure_is_blocked_flag_not_silent_decline(tmp_path, monkeypatch):
    """全文読取自体（`read_full_text_and_raw`）が失敗したら、明示の `unreadable_code_file`
    blocked flag になる（decline＝主体化しないだけの静かな見逃しにしない・fail-closed）。"""
    _register_html(monkeypatch)
    wd = tmp_path / "world"
    wd.mkdir()
    (wd / "bad.html").write_text('<form action="/save"></form>', encoding="utf-8")

    def _boom(rp):
        raise OSError("cannot read")
    monkeypatch.setattr(corpus_docs, "read_full_text_and_raw", _boom)

    nodes, _edges, flags = world_graph.build_world(wd, "w")

    assert nodes == []
    assert flags == [{"doc": "bad.html", "reason": "unreadable_code_file", "action": "blocked"}]


def test_read_full_text_and_raw_decodes_full_text_and_head_from_one_read(tmp_path):
    """`read_full_text_and_raw()` 自体の契約: 生バイト列の先頭スライスをデコードした値が、
    そのまま `_read_head()`（従来の head 専用読み取り）の結果と一致する（同じバイト列から
    両方を切り出している証拠）。"""
    p = tmp_path / "f.txt"
    content = "あ" * 10 + "tail"
    p.write_text(content, encoding="utf-8")

    text, raw = corpus_docs.read_full_text_and_raw(p)

    assert text == content
    head_from_raw = raw[:8].decode("utf-8", errors="replace")
    assert head_from_raw == corpus_docs._read_head(p, 8)


def test_read_full_text_and_raw_normalizes_newlines_like_read_text(tmp_path):
    """全文は `Path.read_text` と同じユニバーサル改行（CRLF/単独 CR → LF）・BOM と不正 UTF-8 は
    置換デコードのまま・生バイト列は無加工。"""
    from sherpa import corpus_docs
    p = tmp_path / "x.c"
    raw = b"\xef\xbb\xbfint value;\rint f(void) { return 0; }\r\nlast\xff"
    p.write_bytes(raw)
    text, got_raw = corpus_docs.read_full_text_and_raw(p)
    assert got_raw == raw
    assert text == p.read_text(encoding="utf-8", errors="replace")
    assert "\r" not in text and text.count("\n") == 2
