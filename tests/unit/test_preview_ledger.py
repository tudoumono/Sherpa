"""資料の画面を開いたとき（`preview_service.build_preview`）は、取り込みの台帳から一覧を出し、資料フォルダのファイルを
分類のために歩かず、グラフを組み立てない。要素・関係は「関係を確認」（`build_preview_graph`）だけが返す。

資料フォルダがネットワーク共有（十数万ファイル）だと、開くたびの全走査とグラフの組み立てで画面が開かない。
台帳の書き込みは取り込みと同じ `worker._ledger_rows`、DB は実物（差し替えは資料フォルダの場所・世代の記録・取り込みの記録だけ）。
"""
from __future__ import annotations

import time
import uuid

import pytest

from sherpa import preview_service as ps
from sherpa import scope_infer, store, worlds
from sherpa.ingest import importance, worker

pytestmark = pytest.mark.unit

_FILES = {
    "gen1/design/spec_a.md": "# spec A\n",
    "gen1/design/sub/deep/memo.md": "# memo\n",
    "gen1/src/PROG1.cbl": "       PROGRAM-ID. PROG1.\n",
    "gen2/src/PROG1.cbl": "       PROGRAM-ID. PROG1.\n",
    "gen1/.git/config": "[core]\n",
    "gen1/" + importance.CONTROL_FILENAME: "*.md: 高\n不正な行\n",
}


@pytest.fixture
def world(monkeypatch, tmp_path):
    wid = "pvledger_" + uuid.uuid4().hex[:8]
    wd, arch, der = tmp_path / "world", tmp_path / "arch", tmp_path / "derived"
    for rel, text in _FILES.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    arch.mkdir()
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: arch)
    monkeypatch.setattr(worlds, "world_label", lambda w: w)
    monkeypatch.setattr(store, "get_latest_run_summary", lambda w, **kw: None)
    ps._LEDGER_VIEW_CACHE.clear()
    ps._SCAN_CACHE.clear()
    ps._GRAPH_VIEW_CACHE.clear()
    yield wid
    store.replace_documents(wid, [])
    ps._LEDGER_VIEW_CACHE.clear()
    ps._SCAN_CACHE.clear()
    ps._GRAPH_VIEW_CACHE.clear()


def _ingest(wid):
    store.replace_documents(wid, worker._ledger_rows(wid))


def _generation(monkeypatch, sig="sig-1", synced_at="t1"):
    monkeypatch.setattr(ps, "_current_world_status",
                        lambda world, **kw: {"sig": sig, "synced_at": synced_at, "root_path": None})


def _forbid_document_walk(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("台帳があるのに資料フォルダを歩いて分類した")
    monkeypatch.setattr(scope_infer, "safe_files", boom)
    monkeypatch.setattr(ps.doc_ledger, "preview_documents", boom)


def test_documents_come_from_the_ledger_not_from_the_folder(world, monkeypatch, tmp_path):
    _generation(monkeypatch)
    _ingest(world)
    (tmp_path / "world" / "gen1" / "design" / "added_later.md").write_text("# new\n", encoding="utf-8")
    _forbid_document_walk(monkeypatch)

    out = ps.build_preview(world)

    names = [d["name"] for d in out["documents"]]
    assert names == ["gen1/design/spec_a.md", "gen1/design/sub/deep/memo.md", "gen1/src/PROG1.cbl", "gen2/src/PROG1.cbl"]
    assert importance.CONTROL_FILENAME not in " ".join(names)
    row = {d["name"]: d for d in out["documents"]}["gen1/src/PROG1.cbl"]
    assert row["folder"] == "gen1/src" and row["top_scope"] == "gen1" and row["branch"] == "source"
    assert row["state"] == "ready" and row["label"] == "使えます" and row["analyzer"] is None
    assert out["counts"]["documents"] == 4
    assert out["entities"] == [] and out["relations"] == [] and out["issues"] == []
    assert out["documents_note"] is None


def test_ledger_rows_are_reused_until_the_ledger_changes(world, monkeypatch):
    _generation(monkeypatch)
    _ingest(world)
    reads = []
    real = store.count_and_list_documents
    monkeypatch.setattr(store, "count_and_list_documents", lambda *a, **kw: (reads.append(1), real(*a, **kw))[1])

    ps.build_preview(world)
    ps.build_preview(world)
    assert len(reads) == 1

    time.sleep(0.01)
    rows = worker._ledger_rows(world)
    store.replace_documents(world, rows[:2])
    out = ps.build_preview(world)
    assert len(reads) == 2 and len(out["documents"]) == 2


def test_state_follows_the_last_run_and_never_silently_ready(world, monkeypatch):
    _generation(monkeypatch)
    _ingest(world)
    flags = [{"doc": "gen1/src/PROG1.cbl", "reason": "unreadable_code_file", "action": "blocked"}]
    monkeypatch.setattr(store, "get_latest_run_summary",
                        lambda w, **kw: {"status": "auto_published", "extraction_snapshot": {"flags": flags}})
    docs = {d["name"]: d for d in ps.build_preview(world)["documents"]}
    assert docs["gen1/src/PROG1.cbl"]["state"] == "unreadable"
    assert docs["gen1/src/PROG1.cbl"]["reason"] == "unreadable_code_file"
    assert docs["gen1/src/PROG1.cbl"]["label"] == "読み取れません"
    assert docs["gen2/src/PROG1.cbl"]["state"] == "ready" and docs["gen2/src/PROG1.cbl"]["reason"] is None

    monkeypatch.setattr(ps.corpus_docs, "last_run_flags", lambda w, **kw: None)   # 直近の取り込みを確認できない
    docs = {d["name"]: d for d in ps.build_preview(world)["documents"]}
    assert docs["gen1/src/PROG1.cbl"]["state"] == "unknown"
    assert docs["gen1/src/PROG1.cbl"]["label"] == "状態を確認できませんでした"
    assert docs["gen1/design/spec_a.md"]["state"] == "ready"      # ソース枝だけが「確認できない」に倒れる


def test_empty_ledger_walks_with_a_deadline_and_explains_when_it_runs_out(world, monkeypatch):
    _generation(monkeypatch, sig="")
    out = ps.build_preview(world)
    assert len(out["documents"]) == 4 and out["documents_note"] is None

    monkeypatch.setattr(scope_infer, "USER_OP_WALK_SECONDS", -1.0)
    out = ps.build_preview(world)
    assert out["documents"] == []
    assert out["documents_note"] == ps._NOTE_LIST_PENDING


def test_graph_is_built_only_by_the_graph_call_and_counts_documents_from_the_ledger(world, monkeypatch):
    _generation(monkeypatch)
    _ingest(world)
    built = []
    real = ps._build
    monkeypatch.setattr(ps, "_build", lambda w, **kw: (built.append(w), real(w, **kw))[1])

    preview = ps.build_preview(world)
    assert built == [] and preview["entities"] == [] and preview["relations"] == []

    graph = ps.build_preview_graph(world)
    assert built == [world]
    assert graph["graph_note"] is None and graph["entities"]
    assert graph["counts"]["documents"] == 4 and graph["counts"]["entities"] == len(graph["entities"])


def test_graph_is_not_built_above_the_document_limit(world, monkeypatch):
    _generation(monkeypatch)
    _ingest(world)
    monkeypatch.setattr(ps, "GRAPH_PREVIEW_MAX_DOCS", 3)
    monkeypatch.setattr(store, "get_latest_published_run_summary",
                        lambda w: {"published_snapshot": {"nodes": 900, "edges": 1800}, "created_at": None})
    built = []
    monkeypatch.setattr(ps, "_build", lambda w, **kw: built.append(w))

    out = ps.build_preview_graph(world)

    assert built == []
    assert out["graph_note"] == ps._NOTE_GRAPH_TOO_MANY
    assert out["entities"] == [] and out["relations"] == [] and out["issues"] == []
    assert out["counts"] == {"entities": 900, "relations": 1800, "deprecated": None, "hidden": None, "documents": 4}


def test_graph_is_built_at_exactly_the_limit_and_not_when_the_count_is_unknown(world, monkeypatch):
    _generation(monkeypatch)
    _ingest(world)
    monkeypatch.setattr(ps, "GRAPH_PREVIEW_MAX_DOCS", 4)
    assert ps.build_preview_graph(world)["graph_note"] is None

    def down(w):
        raise RuntimeError("db down")

    ps._GRAPH_VIEW_CACHE.clear()
    built = []
    monkeypatch.setattr(ps, "_build", lambda w, **kw: built.append(w))
    monkeypatch.setattr(store, "ledger_stamp", down)
    out = ps.build_preview_graph(world)
    assert built == [] and out["graph_note"] == ps._NOTE_GRAPH_NO_COUNT and out["entities"] == []


def test_graph_is_not_built_when_the_folder_grew_past_the_limit_after_the_ingest(world, monkeypatch, tmp_path):
    _generation(monkeypatch)
    _ingest(world)
    monkeypatch.setattr(ps, "GRAPH_PREVIEW_MAX_DOCS", 4)
    for name in ("added.md", "NOEXT"):
        (tmp_path / "world" / "gen1" / "design" / name).write_text("# new\n", encoding="utf-8")
    built = []
    monkeypatch.setattr(ps, "_build", lambda w, **kw: built.append(w))

    out = ps.build_preview_graph(world)

    assert built == []
    assert out["graph_note"] == ps._NOTE_GRAPH_TOO_MANY and out["entities"] == []


def test_ledger_rows_are_rebuilt_after_a_single_document_reconvert(world, monkeypatch):
    _generation(monkeypatch)
    _ingest(world)
    run = {"id": 1, "status": "auto_published"}
    monkeypatch.setattr(store, "get_latest_run_summary", lambda w, **kw: dict(run))
    reads = []
    real = store.count_and_list_documents
    monkeypatch.setattr(store, "count_and_list_documents", lambda *a, **kw: (reads.append(1), real(*a, **kw))[1])

    ps.build_preview(world)
    ps.build_preview(world)
    assert len(reads) == 1

    run["id"] = 2   # 再変換は台帳を変えずに取り込みの記録を 1 つ足す
    ps.build_preview(world)
    assert len(reads) == 2

    run["status"] = "queued"   # 再変換の最中に開いても、終わった後で作り直す
    ps.build_preview(world)
    run["status"] = "auto_published"
    ps.build_preview(world)
    assert len(reads) == 4

def test_expired_folder_scan_is_partial_keeps_ledger_folders_and_is_reused_per_generation(world, monkeypatch):
    _generation(monkeypatch)
    _ingest(world)
    monkeypatch.setattr(scope_infer, "USER_OP_WALK_SECONDS", -1.0)

    out = ps.build_preview(world)

    assert out["folders_partial"] is True
    assert {"gen1", "gen1/design", "gen1/design/sub", "gen1/design/sub/deep", "gen1/src", "gen2", "gen2/src"} <= set(out["folders"])
    assert "gen1/.git" not in out["folders"]

    monkeypatch.setattr(scope_infer, "USER_OP_WALK_SECONDS", 10.0)
    assert ps.build_preview(world)["folders_partial"] is True      # 同じ世代の間は止まった結果を使い回す

    _generation(monkeypatch, synced_at="t2")
    again = ps.build_preview(world)
    assert again["folders_partial"] is False and again["importance_diagnostics"]


def test_scan_without_a_generation_is_never_reused(world, monkeypatch):
    _generation(monkeypatch, sig="")
    monkeypatch.setattr(scope_infer, "USER_OP_WALK_SECONDS", -1.0)
    assert ps.build_preview(world)["folders_partial"] is True
    monkeypatch.setattr(scope_infer, "USER_OP_WALK_SECONDS", 10.0)
    assert ps.build_preview(world)["folders_partial"] is False


def test_folder_scan_rules_and_diagnostics_survive_the_folder_limit(world, monkeypatch, tmp_path):
    _generation(monkeypatch)
    wd = tmp_path / "world"
    (wd / "link_target").mkdir()
    (wd / "link").symlink_to(wd / "link_target")
    (wd / ".hidden" / "inner").mkdir(parents=True)
    (wd / ".hidden" / importance.CONTROL_FILENAME).write_text("不正な行\n", encoding="utf-8")
    (wd / "empty_dir").mkdir()
    monkeypatch.setattr(ps, "FOLDERS_MAX", 2)

    out = ps.build_preview(world)

    assert out["folders_truncated"] is True and out["folders_partial"] is False
    assert len(out["folders"]) >= 2
    assert not any(f.startswith((".", "link/")) or f == "link" or "/.git" in f for f in out["folders"])
    assert {d["config_path"] for d in out["importance_diagnostics"]} == {
        "gen1/" + importance.CONTROL_FILENAME, ".hidden/" + importance.CONTROL_FILENAME}


def test_folder_scan_lists_empty_folders_without_a_ledger(world, monkeypatch, tmp_path):
    _generation(monkeypatch)
    (tmp_path / "world" / "empty_dir").mkdir()
    out = ps.build_preview(world)
    assert "empty_dir" in out["folders"] and "gen1/design/sub" in out["folders"]
    assert "gen1/.git" not in out["folders"]
