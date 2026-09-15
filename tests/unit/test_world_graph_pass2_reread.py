"""`world_graph.build_world()` の Pass1/Pass2 間メモリ非有界の是正（B2）。

旧実装は Pass1 で読んだコード全文を `texts[rel] = (text, analyzer)` として Pass2 完了まで
辞書に保持していた——world 内の全コード本文を同時にメモリへ抱えることになり、単一 worker・
100GB 級コーパス前提では合計サイズに比例して RSS が伸びる（1 ファイルあたりの 8MiB 上限は
`test_world_graph_pass1_size_limit.py` が塞ぐが、合計の上限は無かった）。

是正: `texts[rel]` はパスとアナライザだけを保持し（本文文字列は持たない）、Pass2 のループ内で
`corpus_docs.read_full_text_and_raw()` により rel ごとに読み直す（Pass3 の都度読み直しと同じ
流儀）。本テストは「Pass1 完了後、Pass2 実行前の時点で本文がメモリ上に残っていない」ことと
「Pass2 で各ファイルが1回ずつ再読取される」ことを固定する。
"""
from __future__ import annotations

from sherpa import corpus_docs
from sherpa.ingest import world_graph
from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers._base import Analyzer, DefItem, DefResult, RefResult


class _FakeAnalyzer(Analyzer):
    name = "fake"
    extensions = frozenset({".fk"})
    doctype = "fake"

    def collect_defs(self, text, rel_path):
        return DefResult(primary=DefItem(label="Config", name=rel_path))

    def extract_refs(self, text, rel_path):
        return RefResult()


def _world(tmp_path, files: dict):
    wd = tmp_path / "world"
    wd.mkdir()
    for rel, content in files.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return wd


def test_pass2_rereads_each_file_and_pass1_does_not_retain_text(tmp_path, monkeypatch):
    wd = _world(tmp_path, {"a.fk": "content-a", "b.fk": "content-b"})
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(),))

    read_calls: list[str] = []
    real_read = corpus_docs.read_full_text_and_raw

    def _counting_read(rp):
        read_calls.append(rp.name)
        return real_read(rp)

    monkeypatch.setattr(corpus_docs, "read_full_text_and_raw", _counting_read)

    nodes, _edges, flags = world_graph.build_world(wd, "w")

    assert flags == []
    assert {n["path"] for n in nodes} == {"a.fk", "b.fk"}
    # Pass1で1回・Pass2で1回＝ファイルごとに正確に2回（Pass1内で2回開く旧経路には戻らない）。
    assert sorted(read_calls) == ["a.fk", "a.fk", "b.fk", "b.fk"]


def test_pass2_unreadable_file_is_blocked_not_silent(tmp_path, monkeypatch):
    """Pass1 で読めたファイルが Pass2 再読取時にだけ失敗しても、黙って落とさず
    `unreadable_code_file` の blocked flag になる（Pass1 の fail-closed と同じ流儀）。"""
    wd = _world(tmp_path, {"a.fk": "content-a"})
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(),))

    real_read = corpus_docs.read_full_text_and_raw
    calls = [0]

    def _fail_on_second_read(rp):
        calls[0] += 1
        if calls[0] >= 2:                 # Pass1（1回目）は成功・Pass2（2回目）だけ失敗させる
            raise OSError("vanished between passes")
        return real_read(rp)

    monkeypatch.setattr(corpus_docs, "read_full_text_and_raw", _fail_on_second_read)

    nodes, _edges, flags = world_graph.build_world(wd, "w")

    assert [(n["label"], n["path"]) for n in nodes] == [("Config", "a.fk")]  # Pass1のノードは残る
    assert flags == [{"doc": "a.fk", "reason": "unreadable_code_file", "action": "blocked"}]


def test_pass2_changed_file_is_blocked_not_mixed(tmp_path, monkeypatch):
    """Pass1 と Pass2 の間に原本が書き換わったファイルは、旧本文の定義と新本文の参照を混ぜず
    blocked にする（存在しない依存関係を作らない）。"""
    wd = _world(tmp_path, {"a.fk": "content-a"})
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(),))
    real_read = corpus_docs.read_full_text_and_raw
    calls = [0]

    def _rewrite_on_second_read(rp):
        calls[0] += 1
        if calls[0] >= 2:
            return "content-CHANGED", b"content-CHANGED"
        return real_read(rp)

    monkeypatch.setattr(corpus_docs, "read_full_text_and_raw", _rewrite_on_second_read)
    nodes, _edges, flags = world_graph.build_world(wd, "w")
    assert flags == [{"doc": "a.fk", "reason": "changed_between_passes", "action": "blocked"}]


def test_pass2_oversized_file_is_blocked_without_reading(tmp_path, monkeypatch):
    """Pass1 の後に原本が上限を超えて肥大したファイルは Pass2 で全量を読まず blocked にする。"""
    from sherpa.ingest import text_kind
    wd = _world(tmp_path, {"a.fk": "content-a"})
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(),))
    real_read = corpus_docs.read_full_text_and_raw
    reads = [0]

    def _count(rp):
        reads[0] += 1
        return real_read(rp)
    monkeypatch.setattr(corpus_docs, "read_full_text_and_raw", _count)
    real_stat = type(wd / "a.fk").stat
    state = {"pass1_done": False}

    class _Big:
        st_size = text_kind.MAX_BYTES + 1

    def _stat(self, *a, **k):
        if state["pass1_done"] and self.name == "a.fk":
            return _Big()
        return real_stat(self, *a, **k)
    monkeypatch.setattr(type(wd / "a.fk"), "stat", _stat)
    orig_read = corpus_docs.read_full_text_and_raw

    def _mark(rp):                     # Pass1 の読取が終わった時点から肥大したことにする
        out = orig_read(rp)
        state["pass1_done"] = True
        return out
    monkeypatch.setattr(corpus_docs, "read_full_text_and_raw", _mark)
    nodes, _edges, flags = world_graph.build_world(wd, "w")
    assert flags == [{"doc": "a.fk", "reason": "changed_between_passes", "action": "blocked"}]
