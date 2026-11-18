"""優先・除外 区切り 2 の 2 巡目の是正: Evidence Packet・派生物の前方一致・資料を指す欄の洗い出し。名前は架空。"""
from __future__ import annotations

import json

import pytest

from sherpa import path_rules as PR

WORLD = "prrv5"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("SHERPA_MCP_WORLD", raising=False)
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: [
        {"path": "ZZold", "kind": "exclude", "outside_scope": False},
        {"path": "A", "kind": "exclude", "outside_scope": False, "target_type": "folder"},
        {"path": "ZZdoc/x.docx", "kind": "exclude", "outside_scope": False, "target_type": "file"}]}))


def test_evidence_packet_and_other_source_shapes_drop_excluded_documents():
    """今の回答の Evidence Packet（`source_path`・`matched_doc_ids`）ほか、資料を指す欄を持つ出典・根拠の形。"""
    data = {
        "evidence_packet": {"task_id": "main", "evidence": [
            {"evidence_id": "e1", "source_type": "file", "source_path": "ZZold/a.md"},
            {"evidence_id": "e2", "source_type": "file", "source_path": "ZZnew/b.md"},
            {"evidence_id": "e3", "source_type": "list", "matched_doc_ids": ["ZZold/c.md", "ZZnew/d.md"]}]},
        "claims": [{"id": "c1", "text": "ZZold/a.md に書いてある", "evidence_refs": ["ZZold/a.md:12", "ZZnew/b.md:3", "ev-1"]}],
        "sources_verified": ["ZZold/a.md", "ZZnew/b.md"],
        "citations": [{"doc_id": "ZZold/a.md", "span": [1, 2]}, {"doc_id": "ZZnew/b.md", "span": [1, 2]}],
        "candidates": [{"name": "k", "evidence": {"edges": [{"doc": "ZZold/e.cbl"}, {"doc": "ZZnew/e.cbl"}], "grep": []}}],
        "found_docs": [{"path": "ZZold/f.md"}, {"path": "ZZnew/f.md"}],
        "referenced_docs": [{"path": "ZZold/g.md", "ranges": [[1, 2]]}],
        "compare": {"left_doc_id": "ZZold/l.md", "right_doc_id": "ZZnew/r.md"},
    }
    out = PR.filter_output({"data": data}, WORLD)["data"]
    ev = out["evidence_packet"]["evidence"]
    assert [e["evidence_id"] for e in ev if "source_path" in e] == ["e2"]
    assert [e["matched_doc_ids"] for e in ev if "matched_doc_ids" in e] == [["ZZnew/d.md"]]
    assert out["claims"][0]["evidence_refs"] == ["ZZnew/b.md:3", "ev-1"]
    assert out["claims"][0]["text"] == "ZZold/a.md に書いてある"           # 本文は消さない
    assert out["sources_verified"] == ["ZZnew/b.md"]
    assert [c["doc_id"] for c in out["citations"]] == ["ZZnew/b.md"]
    assert out["candidates"][0]["evidence"]["edges"] == [{"doc": "ZZnew/e.cbl"}]
    assert out["found_docs"] == [{"path": "ZZnew/f.md"}] and out["referenced_docs"] == []
    assert "compare" not in out
    assert "ZZold" not in json.dumps({**out, "claims": []}, ensure_ascii=False)


def test_derived_names_match_only_artifacts_of_an_excluded_file():
    """派生物は、除外された実ファイルから作られた生成物の形だけ。フォルダの規則・似た名前の別の資料には当てない。"""
    r = PR.rules_for(WORLD)
    assert r.derived_of_excluded("ZZdoc/x.docx.rag.md") and r.derived_of_excluded("ZZdoc/x.docx.assets/p1.png")
    assert r.derived_of_excluded("ZZdoc/x.docx.md.meta.json")
    assert not r.derived_of_excluded("A.docs/x.md")                      # `A`（拡張子の無い名前）を除外しても別の資料
    assert not r.derived_of_excluded("ZZdoc/x.docx.old/y.md")            # 生成物の接尾ではない
    assert not r.derived_of_excluded("ZZdoc/x.docxy")
    out = PR.filter_output({"hits": [{"path": "A.docs/x.md"}, {"rag_path": "ZZdoc/x.docx.rag.md"}, {"path": "A/x.md"},
                                      {"path": "ZZold.bak/y.md"}]}, WORLD)
    assert out["hits"] == [{"path": "A.docs/x.md"}, {"path": "ZZold.bak/y.md"}]


def test_sandbox_does_not_deny_similarly_named_siblings(tmp_path):
    from sherpa.providers.codex import sandbox
    kb, rag = tmp_path / "kb", tmp_path / "rag"
    for d in (kb / "A", kb / "A.docs", kb / "ZZdoc", rag / "ZZdoc", rag / "A.docs"):
        d.mkdir(parents=True)
    (kb / "A.docs" / "x.md").write_text("keep")
    (kb / "ZZdoc" / "x.docx").write_text("x")
    (kb / "ZZdoc" / "x.docx.old").write_text("keep")                     # 生成物の接尾ではない
    (rag / "ZZdoc" / "x.docx.rag.md").write_text("x")
    (rag / "ZZdoc" / "x.docx.old").write_text("keep")
    rules = PR.rules_for(WORLD)
    deny = {p.replace(str(tmp_path.resolve()) + "/", "") for p in sandbox._exclude_deny_entries(
        [str(kb), str(rag)], rules, derived_roots=[str(rag.resolve())])}
    assert deny == {"kb/A", "kb/ZZdoc/x.docx", "rag/ZZdoc/x.docx.rag.md"}
