"""L5・§8.6-2「規則版で再生成」と、LLM 成形反映後の ES 再索引配線を pin する
（`worker.regenerate_rag_rule_only`／`worker._reindex_after_rag_rewrite`／`worker._llm_render_pass`）。

すべて monkeypatch のみ（実 DB/ES/LLM 呼び出しは発生しない）。
"""
from __future__ import annotations

import contextlib

from sherpa import es_index, store, worlds
from sherpa.ingest import llm_render, office_md, worker


@contextlib.contextmanager
def _noop_lock(world_id, **kw):
    yield


# ---- regenerate_rag_rule_only ----------------------------------------------------------------

def test_regenerate_rag_rule_only_unavailable_when_world_dir_missing(monkeypatch):
    monkeypatch.setattr(worlds, "world_dir", lambda world: None)
    result = worker.regenerate_rag_rule_only("v1")
    assert result == {"status": "unavailable"}


def test_regenerate_rag_rule_only_success_path(monkeypatch, tmp_path):
    dmd = tmp_path / "derived" / "md"
    dmd.mkdir(parents=True)
    monkeypatch.setattr(worlds, "world_dir", lambda world: tmp_path / "world")
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world: dmd)
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)

    cleared = []
    monkeypatch.setattr(llm_render, "clear_cache", lambda world: cleared.append(world))

    refresh_calls = []

    def _fake_refresh_rag(wd, derived, *, write_rag_sig_marker=True, world=None):
        refresh_calls.append(write_rag_sig_marker)
        return {"rag_generated": 3, "rag_failed": 0, "rag_failures": []}
    monkeypatch.setattr(office_md, "refresh_rag", _fake_refresh_rag)

    reindex_calls = []
    monkeypatch.setattr(worker, "_reindex_after_rag_rewrite", lambda world: reindex_calls.append(world) or True)

    result = worker.regenerate_rag_rule_only("v1")
    assert cleared == ["v1"]
    assert refresh_calls == [True]           # RAG_ES無効＝refresh_rag自身がマーカーを確定する
    assert reindex_calls == ["v1"]
    assert result["status"] == "ok"
    assert result["rag_generated"] == 3


def test_regenerate_rag_rule_only_defers_marker_when_rag_es_enabled(monkeypatch, tmp_path):
    dmd = tmp_path / "derived" / "md"
    dmd.mkdir(parents=True)
    monkeypatch.setattr(worlds, "world_dir", lambda world: tmp_path / "world")
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world: dmd)
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: True)
    monkeypatch.setattr(llm_render, "clear_cache", lambda world: None)

    refresh_calls = []

    def _fake_refresh_rag(wd, derived, *, write_rag_sig_marker=True, world=None):
        refresh_calls.append(write_rag_sig_marker)
        return {"rag_generated": 1, "rag_failed": 0, "rag_failures": []}
    monkeypatch.setattr(office_md, "refresh_rag", _fake_refresh_rag)
    monkeypatch.setattr(worker, "_reindex_after_rag_rewrite", lambda world: True)

    worker.regenerate_rag_rule_only("v1")
    assert refresh_calls == [False]          # RAG_ES有効＝マーカー保留（_reindex_after_rag_rewriteが確定）


def test_regenerate_rag_rule_only_partial_failure(monkeypatch, tmp_path):
    dmd = tmp_path / "derived" / "md"
    dmd.mkdir(parents=True)
    monkeypatch.setattr(worlds, "world_dir", lambda world: tmp_path / "world")
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world: dmd)
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    monkeypatch.setattr(llm_render, "clear_cache", lambda world: None)
    monkeypatch.setattr(office_md, "refresh_rag",
                        lambda wd, derived, **kw: {"rag_generated": 1, "rag_failed": 1,
                                                    "rag_failures": [{"doc": "a", "reason": "x"}]})
    result = worker.regenerate_rag_rule_only("v1")
    assert result["status"] == "partial_failure"


def test_regenerate_rag_rule_only_es_reindex_failed(monkeypatch, tmp_path):
    dmd = tmp_path / "derived" / "md"
    dmd.mkdir(parents=True)
    monkeypatch.setattr(worlds, "world_dir", lambda world: tmp_path / "world")
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world: dmd)
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: True)
    monkeypatch.setattr(llm_render, "clear_cache", lambda world: None)
    monkeypatch.setattr(office_md, "refresh_rag",
                        lambda wd, derived, **kw: {"rag_generated": 1, "rag_failed": 0, "rag_failures": []})
    monkeypatch.setattr(worker, "_reindex_after_rag_rewrite", lambda world: False)
    result = worker.regenerate_rag_rule_only("v1")
    assert result["status"] == "es_reindex_failed"


# ---- _reindex_after_rag_rewrite ----------------------------------------------------------------

def test_reindex_after_rag_rewrite_returns_false_when_sig_missing(monkeypatch):
    monkeypatch.setattr(store, "get_world", lambda world: {"last_sig": ""})
    assert worker._reindex_after_rag_rewrite("v1") is False


def test_reindex_after_rag_rewrite_skips_es_when_rag_es_disabled(monkeypatch):
    monkeypatch.setattr(store, "get_world", lambda world: {"last_sig": "sig"})
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)

    def _boom(*a, **kw):
        raise AssertionError("RAG_ES無効ならESに触れてはいけない")
    monkeypatch.setattr(store, "world_lock", _boom)
    assert worker._reindex_after_rag_rewrite("v1") is True


def test_reindex_after_rag_rewrite_marker_drop_failure(monkeypatch):
    monkeypatch.setattr(store, "get_world", lambda world: {"last_sig": "sig"})
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: True)
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world: "dmd")
    monkeypatch.setattr(office_md, "drop_rag_sig_marker", lambda dmd: False)

    def _boom(*a, **kw):
        raise AssertionError("マーカー無効化に失敗したら索引を開始してはいけない")
    monkeypatch.setattr(worker, "index_world_with_human_md_holdback", _boom)
    assert worker._reindex_after_rag_rewrite("v1") is False


def test_reindex_after_rag_rewrite_success_writes_marker(monkeypatch):
    monkeypatch.setattr(store, "get_world", lambda world: {"last_sig": "sig"})
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: True)
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world: "dmd")
    monkeypatch.setattr(office_md, "drop_rag_sig_marker", lambda dmd: True)
    monkeypatch.setattr(worker, "index_world_with_human_md_holdback",
                        lambda world, content_sig=None: {"available": True})
    written = []
    monkeypatch.setattr(office_md, "write_rag_sig_marker", lambda dmd, world=None: written.append((dmd, world)))
    assert worker._reindex_after_rag_rewrite("v1") is True
    assert written == [("dmd", "v1")]


def test_reindex_after_rag_rewrite_es_failure_does_not_write_marker(monkeypatch):
    monkeypatch.setattr(store, "get_world", lambda world: {"last_sig": "sig"})
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: True)
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world: "dmd")
    monkeypatch.setattr(office_md, "drop_rag_sig_marker", lambda dmd: True)
    monkeypatch.setattr(worker, "index_world_with_human_md_holdback",
                        lambda world, content_sig=None: {"available": False, "error": "unreachable"})

    def _boom(dmd, world=None):
        raise AssertionError("ES失敗時にマーカーを確定してはいけない")
    monkeypatch.setattr(office_md, "write_rag_sig_marker", _boom)
    assert worker._reindex_after_rag_rewrite("v1") is False


# ---- _llm_render_pass ----------------------------------------------------------------------

def test_llm_render_pass_triggers_reindex_only_when_something_changed(monkeypatch):
    class _Result:
        changed_rels = ["a.xlsx"]
    monkeypatch.setattr(llm_render, "run_world_pass", lambda world: _Result())
    calls = []
    monkeypatch.setattr(worker, "_reindex_after_rag_rewrite", lambda world: calls.append(world))
    worker._llm_render_pass("v1")
    assert calls == ["v1"]


def test_llm_render_pass_skips_reindex_when_nothing_changed(monkeypatch):
    class _Result:
        changed_rels = []
    monkeypatch.setattr(llm_render, "run_world_pass", lambda world: _Result())
    calls = []
    monkeypatch.setattr(worker, "_reindex_after_rag_rewrite", lambda world: calls.append(world))
    worker._llm_render_pass("v1")
    assert calls == []
