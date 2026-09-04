"""D1b-2: Document ノードの同一性＝パス（03-鏡モデル.md）。

S3（2026-09-04-グラフのソース正典化.md §4・K9-K11）: 意味層フル抽出（l_extract）は撤去済み。
Document ノードの現行の供給源は言及エッジ（Pass3・S2）のみ——同じ文書内で複数の識別子に
言及しても、`_ensure_mention_document` の get-or-create（同一性＝パス・`_document_cid`）により
1ノードへ収束することを固定する（実測バグ 2026-09-02-RAG表現の全形式展開と文脈保持.md §5.1b-4
＝ファイル約52本に対し Document 63件、の再発防止）。
"""
from __future__ import annotations

from sherpa.ingest import world_graph
from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers._base import Analyzer, DefItem, DefResult, RefResult


class _FakeAnalyzer(Analyzer):
    """Pass1 の定義（`DefItem`）だけを rel ごとに固定で返す最小フェイク（`.fk` 拡張子担当・参照は無し）。"""

    name = "fake"
    extensions = frozenset({".fk"})
    doctype = "fake"

    def __init__(self, defs_by_rel: dict):
        self._defs = defs_by_rel

    def collect_defs(self, text, rel_path):
        return self._defs.get(rel_path, DefResult())

    def extract_refs(self, text, rel_path):
        return RefResult()


def _def(label, name):
    return DefResult(primary=DefItem(label=label, name=name))


def _world(tmp_path, files: dict):
    wd = tmp_path / "world"
    wd.mkdir()
    for rel, content in files.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return wd


def _mention_edges(edges):
    return [e for e in edges if e["type"] == "DOCUMENTS"]


def test_document_cid_is_stable_across_different_source_files():
    assert world_graph._document_cid("w", "a/note.txt") == "document:w:a/note.txt"
    assert (world_graph._document_cid("w", "a/note.txt")
            != world_graph._document_cid("w", "a/other.txt"))


def test_document_node_is_shared_across_multiple_mentions_in_same_doc(tmp_path, monkeypatch):
    """同じ文書が複数の識別子に言及しても、Document ノードは1つに収束する
    （`_ensure_mention_document` の get-or-create・同一性＝パス）。"""
    wd = _world(tmp_path, {
        "g1/a.fk": "x", "g1/b.fk": "y",
        "g1/note.md": "この文書は NAME-ONE と NAME-TWO の両方に言及する。",
    })
    defs = {"g1/a.fk": _def("Module", "NAME-ONE"), "g1/b.fk": _def("Batch", "NAME-TWO")}
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    doc_edges = _mention_edges(edges)
    assert len(doc_edges) == 2                                  # NAME-ONE/NAME-TWO 各1本
    doc_nodes = [n for n in nodes if n["label"] == "Document"]
    assert len(doc_nodes) == 1                                  # 2つの言及が同一 Document ノードへ収束
    assert doc_nodes[0]["cid"] == world_graph._document_cid("w", "g1/note.md")
    assert doc_nodes[0]["path"] == "g1/note.md"                 # ファイル由来ノードと同じ scope 判定に乗る
    assert {e["src"] for e in doc_edges} == {doc_nodes[0]["cid"]}


def test_document_identity_is_by_path_not_by_target_name(tmp_path, monkeypatch):
    """異なる文書（別 rel_path）が同じ識別子に言及しても、別々の Document ノードのまま
    （同一性＝パス・言及先の名前や件数では合流しない）。"""
    wd = _world(tmp_path, {
        "g1/a.fk": "x",
        "g1/note1.md": "この文書は SOMENAME に言及する。",
        "g1/note2.md": "この文書も SOMENAME に言及する。",
    })
    defs = {"g1/a.fk": _def("Module", "SOMENAME")}
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    doc_nodes = {n["cid"] for n in nodes if n["label"] == "Document"}
    assert doc_nodes == {world_graph._document_cid("w", "g1/note1.md"),
                         world_graph._document_cid("w", "g1/note2.md")}
