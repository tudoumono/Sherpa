"""`name_lookup` の続きの取得（offset）・フォルダの絞り込み（path_prefix）・台帳の世代（generation）。"""
from __future__ import annotations

import json
import os
import uuid

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

from sherpa import mcp_coverage_outcome, store, tool_call_log  # noqa: E402
from sherpa import mcp_server as M  # noqa: E402
from sherpa.parts.read import name_lookup  # noqa: E402
from sherpa.store import name_ledger  # noqa: E402
from tests._replace_row import offline_row   # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def world(monkeypatch):
    wid = "namepage_" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("SHERPA_MCP_WORLD", wid)
    monkeypatch.delenv("SHERPA_MCP_SCOPE", raising=False)
    M._seen_tool_calls.clear()
    yield wid
    store.replace_documents(wid, [], row=offline_row(wid))
    M._seen_tool_calls.clear()


def _call(**args) -> dict:
    resp = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "name_lookup", "arguments": args}})
    return json.loads(resp["result"]["content"][0]["text"])


def _same_rows(n: int) -> list:
    rows = [{"name": "ZZSAME", "kind": "Module", "source": "declared", "rel_path": f"gen1/{'a' if i % 2 else 'b'}/S{i:03d}.cbl"}
            for i in range(n)]
    rows += [{"name": "ZZSAME", "kind": "", "source": "filename", "rel_path": r["rel_path"]} for r in rows[:5]]
    return rows


def _walk(**args) -> list:
    """next_offset が尽きるまで続きを取り、各ページの応答を返す。"""
    pages, offset, gen = [], 0, None
    while True:
        call = {**args, "offset": offset}
        if gen:
            call["generation"] = gen
        body = _call(**call)
        pages.append(body)
        gen = body["generation"]
        if body.get("next_offset") is None:
            return pages
        offset = body["next_offset"]


def test_matches_over_limit_are_fully_fetched_by_offset(world):
    n = name_lookup.MATCH_MAX * 2 + 7
    rows = _same_rows(n)
    name_ledger.replace_rows(world, rows, row=offline_row(world))
    pages = _walk(name="ZZSAME")
    got = [(m["rel_path"], m["source"]) for p in pages for m in p["matches"]]
    assert len(pages) == 3
    assert len(got) == len(set(got)) == len(rows)
    assert set(got) == {(r["rel_path"], r["source"]) for r in rows}
    assert [p["truncated"] for p in pages] == [True, True, False]
    assert all(p["count"] == len(rows) for p in pages)
    outcomes = [mcp_coverage_outcome.coverage_outcome("name_lookup", p, False) for p in pages]
    logged = [tool_call_log.summarize_result("name_lookup", {}, p, False, {})["truncated"] for p in pages]
    assert (outcomes, logged) == (["limit", "limit", "hit"], [True, True, False])


def test_path_prefix_narrows_within_scope(world, monkeypatch):
    name_ledger.replace_rows(world, _same_rows(30), row=offline_row(world))
    body = _call(name="ZZSAME", path_prefix="gen1/a")
    assert body["matches"] and all(m["rel_path"].startswith("gen1/a/") for m in body["matches"])
    assert body["count"] < _call(name="ZZSAME")["count"]
    monkeypatch.setenv("SHERPA_MCP_SCOPE", "gen1/b")
    M._seen_tool_calls.clear()
    assert _call(name="ZZSAME", path_prefix="gen1/a")["matches"] == []
    M._seen_tool_calls.clear()
    inner = _call(name="ZZSAME", path_prefix="gen1")["matches"]
    assert inner and all(m["rel_path"].startswith("gen1/b/") for m in inner)


def test_generation_change_between_pages_is_an_error(world):
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    first = _call(name="ZZSAME")
    assert first["next_offset"] == name_lookup.MATCH_MAX
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    body = _call(name="ZZSAME", offset=first["next_offset"], generation=first["generation"])
    assert body["error"] == name_lookup.GENERATION_MISMATCH_ERROR
    assert "offset 0" in body["notice"]
    assert "matches" not in body


def test_offset_without_generation_is_rejected(world):
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    assert _call(name="ZZSAME", offset=name_lookup.MATCH_MAX)["error"] == name_lookup.GENERATION_REQUIRED_ERROR


def test_near_candidates_can_be_fetched_by_offset(world):
    n = name_lookup.CANDIDATE_MAX + 5
    name_ledger.replace_rows(world, [
        {"name": f"ZZNEAR{i:02d}", "kind": "Module", "source": "declared", "rel_path": f"gen1/src/N{i:02d}.cbl"}
        for i in range(1, n + 1)], row=offline_row(world))
    pages = _walk(name="ZZNEAR00")
    got = [c["rel_path"] for p in pages for c in p["candidates"]]
    assert len(pages) == 2
    assert len(got) == len(set(got)) == n
    assert [p["candidates_truncated"] for p in pages] == [True, False]
    assert all(p["candidates_total"] == n for p in pages)


def test_rows_and_generation_come_from_the_same_snapshot(world, monkeypatch):
    old = [{"name": "ZZSAME", "kind": "Module", "source": "declared", "rel_path": "old/S1.cbl"}]
    new = [{"name": "ZZSAME", "kind": "Module", "source": "declared", "rel_path": "new/S1.cbl"}]
    name_ledger.replace_rows(world, old, row=offline_row(world))
    old_gen = name_ledger.built_at(world)
    real = name_ledger.built_at

    def swap_after_check(w, conn=None):
        gen = real(w, conn)
        name_ledger.replace_rows(w, new, row=offline_row(w))  # 世代の確認の後・行の読み出しの前に入れ替わる
        return gen

    monkeypatch.setattr(name_ledger, "built_at", swap_after_check)
    body = _call(name="ZZSAME")
    assert body["generation"].startswith(old_gen + ":")
    assert [m["rel_path"] for m in body["matches"]] == ["old/S1.cbl"]


def test_found_over_all_pages_is_not_inflated(world):
    name_ledger.replace_rows(world, [
        {"name": "ZZSAME", "kind": "Module", "source": "declared", "rel_path": f"gen1/S{i:03d}.cbl"} for i in range(207)], row=offline_row(world))
    pages = _walk(name="ZZSAME")
    counts = [tool_call_log.summarize_result("name_lookup", {}, p, False, {})["count"] for p in pages]
    assert counts == [100, 100, 7]
    assert sum(counts) == 207


def test_saving_name_rules_between_pages_changes_the_generation(world, tmp_path):
    store.upsert_world(world, str(tmp_path / "world"))
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    try:
        first = _call(name="ZZSAME")
        store.set_resolve_settings(world, {"copy_paths": [], "path_aliases": {}, "name_rules": [{"from": "*X", "to": "*Y"}]})
        body = _call(name="ZZSAME", offset=first["next_offset"], generation=first["generation"])
        assert body["error"] == name_lookup.GENERATION_MISMATCH_ERROR
    finally:
        store.delete_world_row(world)


def test_saving_exclude_rules_between_pages_changes_the_generation(world, monkeypatch):
    from sherpa import path_rules
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    first = _call(name="ZZSAME")
    monkeypatch.setenv(path_rules.ENV_KEY, json.dumps({world: [{"path": "gen1/a/S001.cbl", "kind": "exclude"}]}))
    body = _call(name="ZZSAME", offset=first["next_offset"], generation=first["generation"])
    assert body["error"] == name_lookup.GENERATION_MISMATCH_ERROR


def test_saving_outside_scope_priority_between_pages_changes_the_generation(world, monkeypatch):
    from sherpa import path_rules
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    first = _call(name="ZZSAME")
    monkeypatch.setenv(path_rules.ENV_KEY, json.dumps({world: [{"path": "gen1", "kind": "priority", "outside_scope": True}]}))
    body = _call(name="ZZSAME", offset=first["next_offset"], generation=first["generation"])
    assert body["error"] == name_lookup.GENERATION_MISMATCH_ERROR


@pytest.fixture
def one_connection_pool(world, monkeypatch):
    from sherpa.store import db
    monkeypatch.setenv("SHERPA_PG_POOL_MIN", "1")
    monkeypatch.setenv("SHERPA_PG_POOL_MAX", "1")
    monkeypatch.setenv("SHERPA_PG_POOL_TIMEOUT", "2")
    db.close_pg_pool()
    yield
    monkeypatch.undo()
    db.close_pg_pool()


def test_lookup_without_exact_match_needs_only_one_connection(world, one_connection_pool, tmp_path):
    name_ledger.replace_rows(world, _same_rows(3), row=offline_row(world))
    store.upsert_world(world, str(tmp_path / "world"))
    try:
        store.set_resolve_settings(world, {"copy_paths": [], "path_aliases": {}, "name_rules": [{"from": "*X", "to": "*Y"}]})
        body = _call(name="ZZOTHER")
        assert "error" not in body and body["notice"]
    finally:
        store.delete_world_row(world)


def test_restart_from_zero_after_generation_error_is_not_a_duplicate(world):
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    first = _call(name="ZZSAME")
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    assert _call(name="ZZSAME", offset=first["next_offset"], generation=first["generation"])["error"]
    again = _call(name="ZZSAME")
    assert again.get("error") != "duplicate_tool_call" and again["matches"]


def test_oversize_pages_are_clipped_structurally_and_still_complete(world, monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "65536")  # Codex 経路の上限
    pad = "d" * 1500
    rows = [{"name": "ZZSAME", "kind": "Module", "source": "declared", "rel_path": f"gen1/{pad}/S{i:03d}.cbl"} for i in range(100)]
    name_ledger.replace_rows(world, rows, row=offline_row(world))
    pages = _walk(name="ZZSAME")
    assert len(pages) > 1
    got = [m["rel_path"] for p in pages for m in p["matches"]]
    assert len(got) == len(set(got)) == len(rows)
    assert all("text" not in p and p["count"] == len(rows) and p["generation"] for p in pages)
    assert all(p["truncated"] for p in pages[:-1])


def test_plain_repeat_is_duplicate_but_restart_after_generation_error_passes(world):
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    first = _call(name="ZZSAME")
    assert _call(name="ZZSAME")["error"] == "duplicate_tool_call"
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    assert _call(name="ZZSAME", offset=first["next_offset"], generation=first["generation"])["error"] == name_lookup.GENERATION_MISMATCH_ERROR
    assert _call(name="ZZSAME")["matches"]
    assert _call(name="ZZSAME")["error"] == "duplicate_tool_call"


def test_missing_generation_error_does_not_reopen_the_first_page(world):
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    first = _call(name="ZZSAME")
    assert _call(name="ZZSAME", offset=first["next_offset"])["error"] == name_lookup.GENERATION_REQUIRED_ERROR
    assert _call(name="ZZSAME")["error"] == "duplicate_tool_call"


def test_single_oversize_match_returns_budget_error_not_a_text_wrapper(world, monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "1024")
    name_ledger.replace_rows(world, [
        {"name": "ZZSAME", "kind": "Module", "source": "declared", "rel_path": "gen1/" + "d" * 3000 + "/S1.cbl"}], row=offline_row(world))
    body = _call(name="ZZSAME")
    assert body["error"] == "tool_result_budget_too_small"
    assert "text" not in body and "next_offset" not in body


def _clipped_entries(path) -> list:
    if not path.exists():
        return []
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return [r for r in rows if r.get("field") == "tool_result_clipped"]


def test_budget_clip_is_counted_once_as_tool_result_clipped(world, monkeypatch, tmp_path):
    side = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(side))
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "4096")
    name_ledger.replace_rows(world, [
        {"name": "ZZSAME", "kind": "Module", "source": "declared", "rel_path": f"gen1/{'d' * 400}/S{i:03d}.cbl"} for i in range(30)], row=offline_row(world))
    body = _call(name="ZZSAME")
    assert body["byte_clipped"] is True and body["next_offset"] < 30
    assert len(_clipped_entries(side)) == 1


def test_budget_too_small_replacement_is_counted_as_tool_result_clipped(world, monkeypatch, tmp_path):
    side = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(side))
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "1024")
    name_ledger.replace_rows(world, [
        {"name": "ZZSAME", "kind": "Module", "source": "declared", "rel_path": "gen1/" + "d" * 3000 + "/S1.cbl"}], row=offline_row(world))
    assert _call(name="ZZSAME")["error"] == "tool_result_budget_too_small"
    assert len(_clipped_entries(side)) == 1


def test_fit_measures_the_finished_shape_so_it_never_falls_back_to_a_text_wrapper(world, monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "1024")
    for width in range(780, 840):  # 仮の形は収まり完成形は収まらない境目を必ず通す
        M._seen_tool_calls.clear()
        name_ledger.replace_rows(world, [
            {"name": "ZZSAME", "kind": "Module", "source": "declared", "rel_path": f"{'d' * width}{i}.cbl"} for i in range(2)], row=offline_row(world))
        body = _call(name="ZZSAME")
        assert "text" not in body
        assert len(json.dumps(body, ensure_ascii=False).encode("utf-8")) <= 1024


def test_restart_after_generation_error_ignores_omittable_args(world):
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    first = _call(name="ZZSAME")
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    bad = _call(name="ZZSAME", path_prefix="", offset=first["next_offset"], generation=first["generation"])
    assert bad["error"] == name_lookup.GENERATION_MISMATCH_ERROR
    assert _call(name="ZZSAME")["matches"]
    assert _call(name="ZZSAME")["error"] == "duplicate_tool_call"
    assert _call(name="ZZSAME", path_prefix="", offset=0)["error"] == "duplicate_tool_call"


def test_restart_after_generation_error_matches_name_spelled_differently(world):
    # 検索が同じ名前とみなす表記違い（全角・半角）でも、取り直しの重複のキーは同じになる
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    first = _call(name="ZZSAME")
    name_ledger.replace_rows(world, _same_rows(name_lookup.MATCH_MAX + 10), row=offline_row(world))
    bad = _call(name="ｚｚｓａｍｅ", offset=first["next_offset"], generation=first["generation"])
    assert bad["error"] == name_lookup.GENERATION_MISMATCH_ERROR
    assert _call(name="ZZSAME")["matches"]
    assert _call(name="ｚｚｓａｍｅ")["error"] == "duplicate_tool_call"


def test_unused_args_do_not_make_a_different_duplicate_key(world):
    name_ledger.replace_rows(world, _same_rows(3), row=offline_row(world))
    assert _call(name="ZZSAME")["matches"]
    assert _call(name="ZZSAME", unused=1)["error"] == "duplicate_tool_call"
