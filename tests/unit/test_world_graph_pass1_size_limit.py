"""`world_graph.build_world()` Pass1 のサイズ上限退行是正（アナライザ拡張 S3b Codex RV・高）。

`build_world()` の `files` は `scope_infer.safe_files()` の生列挙で、`corpus_docs.classify_document`
の accepts()/サイズ判定を経由しない——Pass1 が拡張子候補のあるファイルを無条件に `read_text()`
していたため、登録アナライザ対象（新設 xml_config/properties/yaml_config はもちろん既存の
cobol/copybook/jcl/java も同様）の巨大ファイルが単一 worker を1ファイルで OOM させ得た。是正＝
`read_text()` の前に `text_kind.MAX_BYTES`（grep 上限と同じ 8MiB・単一の真実源）で読み飛ばし、
`flags` に `dropped_syntax`（`why="size_exceeded"`）を申告する（黙って落とさない）。

`registry._ANALYZERS` を最小のフェイクアナライザに差し替える流儀は
`tests/unit/test_world_graph_analyzer_expansion_common.py` と同じ——実ファイルを 8MiB 書くのではなく
`text_kind.MAX_BYTES` を小さく差し替えて実ファイルを軽く保つ（`test_corpus_docs_text_kind.py` と同じ
手法）。
"""
from __future__ import annotations

from sherpa.ingest import text_kind, world_graph
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


def test_oversize_registered_analyzer_file_is_dropped_before_read_text(tmp_path, monkeypatch):
    wd = _world(tmp_path, {"huge.fk": "x" * 100, "small.fk": "y"})
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(),))
    monkeypatch.setattr(text_kind, "MAX_BYTES", 10)

    nodes, edges, flags = world_graph.build_world(wd, "w")

    paths = {n["path"] for n in nodes}
    assert "small.fk" in paths
    assert "huge.fk" not in paths                     # 読み飛ばされ主体もノードも作られない

    size_flags = [f for f in flags if f.get("why") == "size_exceeded"]
    assert size_flags == [{"reason": "dropped_syntax", "analyzer": "fake", "from": "huge.fk",
                           "why": "size_exceeded", "line": 1, "snippet": ""}]


def test_undersize_registered_analyzer_file_still_builds_normally(tmp_path, monkeypatch):
    """上限を広げても既定の 8MiB 以内は従来どおりノード化される（回帰防止）。"""
    wd = _world(tmp_path, {"ok.fk": "z"})
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    assert {n["path"] for n in nodes} == {"ok.fk"}
    assert not [f for f in flags if f.get("why") == "size_exceeded"]


def test_oversize_file_read_error_still_uses_stat_gate_not_crash(tmp_path, monkeypatch):
    """stat が読めても中身の実読込前にサイズ判定するため、上限超過ファイルは `read_text()` を
    そもそも呼ばない（実際に呼ばれたかを monkeypatch で確認する）。"""
    wd = _world(tmp_path, {"huge.fk": "x" * 100})
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(),))
    monkeypatch.setattr(text_kind, "MAX_BYTES", 10)

    from pathlib import Path
    orig_read_text = Path.read_text
    calls = []

    def _tracking_read_text(self, *a, **kw):
        calls.append(self.name)
        return orig_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _tracking_read_text)
    world_graph.build_world(wd, "w")
    assert calls == []
