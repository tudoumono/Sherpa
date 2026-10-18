"""自分自身のソースと設計書の質問セット生成（scripts/bench/prepare_self_corpus.py）の単体テスト。
架空の小さなパッケージ（tests/fixtures/self_corpus）だけを使い、環境変数の抽出・呼び出しの閉包・漏れ防止の除外を確かめる。"""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from scripts.bench import prepare_self_corpus as psc
from scripts.bench.ledger_eval import SELF_SYSTEM, read_manifest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "self_corpus"

LEAK_FILES = ["docs/reports/answer.md", "docs/rv/ledger.md", "docs/proposals/p.md", "docs/notes/n.md", "docs/archive/a.md",
              "CHANGELOG.md", "CLAUDE.md", "AGENTS.md", "tests/test_x.py", "scripts/s.py", "fixtures/f.txt",
              ".claude/x.md", ".git/config", "sherpa/.claude/y.py", "sherpa/__pycache__/z.py"]
NOT_TEXT_OR_NOT_LISTED = ["web/logo.png", "docs/manual/img.png", "docs/random.md", "Makefile", "docs/templates/t.md"]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    dest = tmp_path / "tree"
    shutil.copytree(FIXTURE, dest)
    for rel in [*LEAK_FILES, *NOT_TEXT_OR_NOT_LISTED]:
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    return dest


def test_leak_guard_keeps_only_the_allow_list(tree: Path):
    files, skipped = psc.collect_corpus_files(tree)
    assert files == sorted(["README.md", "docs/01-入口.md", "docs/design/tools.md", "docs/manual/guide.md", "web/index.html",
                            *(f"sherpa/{n}.py" for n in ("__init__", "a", "bmod", "cmod", "config", "dmod", "env_int",
                                                         "mcp_server", "routes", "util"))])
    assert not any(psc.is_leak_path(f) for f in files)
    assert skipped["漏れ防止で除外: docs/reports/"] == 1
    assert skipped["画像などバイナリで除外: web/"] == 1
    psc.assert_no_leak(files)
    with pytest.raises(RuntimeError):
        psc.assert_no_leak([*files, "docs/reports/answer.md"])


def test_env_reads_resolve_wrappers_and_constants_and_skip_writes_and_dynamic_names():
    index = psc.PyIndex(FIXTURE)
    gold = psc.env_gold(index)
    assert {n: e["files"] for n, e in gold["items"].items()} == {
        "SHERPA_ALPHA": ["sherpa/config.py"], "SHERPA_BETA": ["sherpa/config.py"], "SHERPA_CONST": ["sherpa/config.py"],
        "SHERPA_DELTA": ["sherpa/config.py"], "SHERPA_GAMMA": ["sherpa/config.py"], "SHERPA_PRESENT": ["sherpa/config.py"]}
    assert "SHERPA_WRITE" not in gold["items"] and "SHERPA_ONLY_IN_MESSAGE" not in gold["items"]
    assert gold["dynamic_count"] == 1
    assert gold["non_sherpa_names"] == 1
    assert gold["wrappers"] == ["sherpa.env_int:env_int"]


@pytest.mark.parametrize("hop,expected", [
    (1, {"sherpa/a.py"}),
    (2, {"sherpa/a.py", "sherpa/bmod.py"}),
    (3, {"sherpa/a.py", "sherpa/bmod.py", "sherpa/cmod.py"}),
    (4, {"sherpa/a.py", "sherpa/bmod.py", "sherpa/cmod.py"}),
    (5, {"sherpa/a.py", "sherpa/bmod.py", "sherpa/cmod.py", "sherpa/dmod.py"}),
])
def test_call_closure_follows_alias_relative_module_and_class_calls(hop, expected):
    graph = psc.CallGraph(psc.PyIndex(FIXTURE))
    files = graph.files_at("sherpa.util:base", hop)
    assert set(files) == expected
    assert "sherpa/util.py" not in files  # 定義ファイル自体は除く
    assert files["sherpa/a.py"] == 1


def test_hop_target_choice_is_deterministic_and_has_callers_exactly_that_deep():
    graph = psc.CallGraph(psc.PyIndex(FIXTURE))
    first = psc.pick_hop_targets(graph, hops=(2, 3), lo=1, hi=5)
    assert first == psc.pick_hop_targets(graph, hops=(2, 3), lo=1, hi=5)
    assert first[2]["qual"] != first[3]["qual"]
    for hop, info in first.items():
        assert hop in graph.closure(info["qual"], hop).values()
    with pytest.raises(RuntimeError):
        psc.pick_hop_targets(graph, hops=(9,), lo=1, hi=5)


def test_routes_tools_and_design_sections():
    index = psc.PyIndex(FIXTURE)
    assert [(r["method"], r["path"], r["rel"]) for r in psc.http_routes(index)] == [
        ("GET", "/v1/ping", "sherpa/routes.py"), ("POST", "/items", "sherpa/routes.py")]
    assert sorted({t["name"] for t in psc.mcp_tools(index)}) == ["orphan_tool", "ping_tool"]
    files = [p.relative_to(FIXTURE).as_posix() for p in FIXTURE.rglob("*.md")]
    sections = psc.design_sections(FIXTURE, files)
    assert [s["heading"] for s in sections] == ["道具", "1. 疎通", "2. 別の節"]  # コードブロック内の # は見出しにしない
    assert [s["heading"] for s in psc.mentions_in_sections(sections, "ping_tool")] == ["1. 疎通"]
    assert psc.mentions_in_sections(sections, "orphan_tool") == []
    assert [s["heading"] for s in psc.mentions_in_sections(sections, "SHERPA_BETA")] == ["2. 別の節"]


def test_discrepancy_needs_old_claim_in_docs_and_opposite_fact_in_code(monkeypatch):
    good = {"id": "g", "topic": "t", "doc": "docs/design/tools.md", "doc_section": "1", "doc_needle": "疎通は常に同期で行う",
            "code_fact": "f", "code_paths": ["sherpa/a.py"], "code_checks": [{"kind": "contains", "path": "sherpa/a.py", "text": "def hop1"}]}
    bad_doc = {**good, "id": "bad_doc", "doc_needle": "どこにも無い記述"}
    bad_code = {**good, "id": "bad_code", "code_checks": [{"kind": "contains", "path": "sherpa/a.py", "text": "def nothing"}]}
    removed = {"docs/design/tools.md": ["旧記述: 疎通は常に同期で行う。"]}
    assert psc.verify_candidate(FIXTURE, good, removed)["verified"]
    assert not psc.verify_candidate(FIXTURE, bad_doc, removed)["verified"]
    assert not psc.verify_candidate(FIXTURE, bad_code, removed)["verified"]
    assert not psc.verify_candidate(FIXTURE, good, {"docs/design/tools.md": ["別の行"]})["verified"]  # 設計書を直した差分に出ない記述
    omission = {"id": "o", "source": "s", "topic": "t", "docs": ["docs/design/tools.md"], "doc_terms": ["region_context"],
                "code_fact": "f", "code_checks": [], "excluded_reason": "r"}
    assert psc.verify_omission(FIXTURE, omission)["doc_silent"]  # 設計書が黙っている＝食い違いでなく記述漏れ


def test_prepare_writes_corpus_manifest_and_gold_outside_the_corpus(tree: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(psc, "GAP_QUESTIONS", [{
        "qid": "self-gap-fixture", "question": "q", "doc": "docs/design/tools.md",
        "candidates": [{"id": "g", "topic": "t", "doc": "docs/design/tools.md", "doc_section": "1", "doc_needle": "疎通は常に同期で行う",
                        "code_fact": "f", "code_paths": ["sherpa/a.py"],
                        "code_checks": [{"kind": "contains", "path": "sherpa/a.py", "text": "def hop1"}]}]}])
    monkeypatch.setattr(psc, "OMISSION_CANDIDATES", [])
    out = tmp_path / "out"
    result = psc.prepare(Path("."), "fixture", out, None, src_override=tree, hops=(2, 3), file_range=(1, 5))
    manifest, digest = read_manifest(out / "manifest.json")  # ledger_eval の検査（登録ルート外の正解・パスの実在・範囲）を通る
    assert len(digest) == 64
    assert {q["system"] for q in manifest["questions"]} == {SELF_SYSTEM}
    by_id = {q["id"]: q for q in manifest["questions"]}
    assert set(by_id) == {"self-env-vars", "self-http-routes", "self-mcp-tools-docs", "self-env-vars-docs",
                          "self-impact-hop2", "self-impact-hop3", "self-gap-fixture"}
    assert by_id["self-gap-fixture"]["needs_user_review"] is True
    assert not any(q.get("needs_user_review") for q in manifest["questions"] if q["type"] != "discrepancy")
    assert by_id["self-env-vars"]["expected_paths"] == ["sherpa/config.py"]
    assert by_id["self-mcp-tools-docs"]["expected_paths"] == ["docs/design/tools.md", "sherpa/mcp_server.py"]
    tools = json.loads((out / "gold/self-mcp-tools-docs.json").read_text(encoding="utf-8"))
    assert tools["not_in_design"] == ["orphan_tool"]
    assert tools["items"]["orphan_tool"]["design_sections"] == "設計書に無い"
    assert by_id["self-impact-hop2"]["hop"] == 2
    assert sorted(p.name for p in (out / "corpus").iterdir() if p.is_file()) == ["README.md"]  # 正解・manifest は資料フォルダの外
    assert result["provenance"]["leak_guard"]["token_hits_in_corpus"]["SRH-08"] == 0
    # 同じ入力から同じ質問・資料の指紋になる（出力先のパスだけが違う）
    again = psc.prepare(Path("."), "fixture", tmp_path / "out2", None, src_override=tree, hops=(2, 3), file_range=(1, 5))
    assert again["manifest"]["questions"] == result["manifest"]["questions"]
    assert again["manifest"]["corpus_stats"] == result["manifest"]["corpus_stats"]
    with pytest.raises(SystemExit):  # 既存の出力は上書きしない
        psc.prepare(Path("."), "fixture", out, None, src_override=tree, hops=(2, 3), file_range=(1, 5))
