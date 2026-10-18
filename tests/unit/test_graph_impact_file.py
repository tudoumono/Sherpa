"""`graph_impact` の戻り（要約＋先頭 limit 件＋全件のファイル）と、途中で過負荷になった場合の申告。Neo4j 不要。"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import graph_tools, tool_dispatch


def _run(name, args, layer=None):
    return tool_dispatch.run_tool(name, args, "v1", None, layer=layer)[0]


def _stub_impact(monkeypatch, n, *, info_extra=None, paths=None, flow_n=0):
    """起点 `c0` から n 件の影響先（距離 1〜3）を返す `world_impact` に差し替える。`flow_n` 件は流れの段で届いた候補（`flow_depth` が正のときだけ返す）。"""
    from sherpa.ingest import world_neo4j as WN

    class _Drv:
        def session(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            pass

    pool = paths or ["top/a/N{}.cbl"]

    def impact(session, cids, world, sp, depth, info=None, detail=False, flow_depth=0, **k):
        info.update({"depth_truncated": False, **(info_extra or {})})
        if flow_depth > 0:
            info.setdefault("flow_depth_truncated", False)
        out = []
        for i in range(n):
            cid = f"c{i + 1}"
            dist = i % 3 + 1
            out.append({"name": f"N{i}", "label": "Module" if i % 2 else "Batch", "category": "x", "status": "active",
                        "analyzer": None, "top_scope": "t", "path": pool[i % len(pool)].format(i),
                        "trace": [f"T{j}" for j in range(dist + 1)], "evidence": [], "canonical_id": cid,
                        "trace_cids": [cid] + [f"m{j}" for j in range(dist - 1)] + ["c0"],
                        "trace_paths": ["top/p"] * (dist + 1),
                        "edges": [{"type": "INVOKES", "doc": "top/d.md", "line": j + 1} for j in range(dist)]})
        for i in range(flow_n if flow_depth > 0 else 0):
            cid = f"f{i + 1}"
            out.append({"name": f"F{i}", "label": "Module", "category": "x", "status": "active", "analyzer": None,
                        "top_scope": "t", "path": f"top/f/F{i}.cbl", "origin": "flow",
                        "trace": ["F", "D", "S", "T"], "evidence": [], "canonical_id": cid,
                        "trace_cids": [cid, "d1", "m0", "c0"], "trace_paths": ["top/p"] * 4,
                        "edges": [{"type": "FLOWS_TO", "doc": "top/f.cbl", "line": 3, "via": "read_file"},
                                  {"type": "FLOWS_TO", "doc": "top/g.cbl", "line": 4, "via": "write_file"},
                                  {"type": "INVOKES", "doc": "top/d.md", "line": 1}]})
        return out

    monkeypatch.setattr(graph_tools, "_open_driver", lambda: _Drv())
    monkeypatch.setattr(WN, "get_world_entities", lambda *a, **k: [{"canonical_id": "c0", "label": "Module", "name": "S",
                                                                    "path": "top/s.cbl"}])
    monkeypatch.setattr(WN, "world_impact", impact)
    monkeypatch.setattr(WN, "read_unresolved", lambda *a, **k: {"items": [], "omitted": 0})
    monkeypatch.setattr(WN, "world_related_documents", lambda *a, **k: [])
    monkeypatch.setattr(WN, "read_plugin_failures", lambda *a, **k: [])
    monkeypatch.setattr(WN, "read_unparsed_sources", lambda *a, **k: None)
    monkeypatch.setattr(WN, "read_generation", lambda *a, **k: "g1")


def test_over_limit_returns_head_summary_and_a_file_with_every_item(monkeypatch, tmp_path):
    tmp = tmp_path / ".tmp"
    tmp.mkdir()
    monkeypatch.setenv(graph_tools.OUT_DIR_ENV, str(tmp))
    _stub_impact(monkeypatch, 130, paths=["top/a/N{}.cbl", "top/b/N{}.cbl", "other/N{}.cbl"])
    res = _run("graph_impact", {"canonical_id": "c0", "limit": 50})
    n = len(res["impact"])
    assert 0 < n <= 50 and res["count"] == 130                                 # 件数の上限とバイトの上限の小さい方
    assert res["summary"]["total"] == 130
    assert sum(res["summary"]["by_kind"].values()) == 130 and set(res["summary"]["by_kind"]) == {"Module", "Batch"}
    assert res["summary"]["by_folder"] == {"top": 87, "other": 43}
    assert res["summary"]["by_distance"] == {"1": 44, "2": 43, "3": 43}
    assert res["coverage"]["limits"] == [{"kind": "result_cap", "stage": "impact"}]
    assert res["coverage"]["complete"] is False and res["coverage"]["omitted"] == 130 - n
    rel = res["result_file"]
    assert rel.startswith(".tmp/graph_impact/") and rel.endswith(".jsonl")
    lines = [json.loads(x) for x in (tmp_path / rel).read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 130 and len({x["canonical_id"] for x in lines}) == 130
    assert {"canonical_id", "path", "kind", "distance", "route"} <= set(lines[0])
    assert all("doc" in r and "line" in r for r in lines[0]["route"])
    assert [x["canonical_id"] for x in lines[:n]] == [x["canonical_id"] for x in res["impact"]]


def test_within_limit_writes_no_file_and_stays_complete(monkeypatch, tmp_path):
    monkeypatch.setenv(graph_tools.OUT_DIR_ENV, str(tmp_path))
    _stub_impact(monkeypatch, 5)
    res = _run("graph_impact", {"canonical_id": "c0"})
    assert len(res["impact"]) == 5 and "result_file" not in res and res["summary"]["total"] == 5
    assert res["coverage"]["complete"] is True and not list(tmp_path.iterdir())


def test_without_a_work_dir_returns_the_head_only(monkeypatch):
    monkeypatch.delenv(graph_tools.OUT_DIR_ENV, raising=False)
    _stub_impact(monkeypatch, 60)
    res = _run("graph_impact", {"canonical_id": "c0"})
    assert 0 < len(res["impact"]) <= 50 and res["count"] == 60 and "result_file" not in res
    assert res["coverage"]["limits"] == [{"kind": "result_cap", "stage": "impact"}]


def test_does_not_write_through_a_symlinked_directory(monkeypatch, tmp_path):
    tmp, elsewhere = tmp_path / ".tmp", tmp_path / "elsewhere"
    tmp.mkdir()
    elsewhere.mkdir()
    (tmp / graph_tools.IMPACT_FILE_DIR).symlink_to(elsewhere)
    monkeypatch.setenv(graph_tools.OUT_DIR_ENV, str(tmp))
    _stub_impact(monkeypatch, 60)
    res = _run("graph_impact", {"canonical_id": "c0"})
    assert "result_file" not in res and 0 < len(res["impact"]) <= 50 and not list(elsewhere.iterdir())


@pytest.mark.parametrize("kind", ["timeout", "row_cap"])
def test_a_partial_traversal_is_reported_with_the_overload_kind(monkeypatch, kind):
    _stub_impact(monkeypatch, 3, info_extra={"depth_truncated": None, "depth_check_limit": kind})
    res = _run("graph_impact", {"canonical_id": "c0"})
    assert len(res["impact"]) == 3 and res["truncated"] is True
    assert res["coverage"]["complete"] is False and res["coverage"]["omitted"] is None
    assert {"kind": kind, "stage": "impact"} in res["coverage"]["limits"]
    assert res["coverage"]["depth"] == {"requested": 5, "truncated": None}


def test_flow_candidates_are_returned_apart_from_impact_with_their_own_limit_and_file(monkeypatch, tmp_path):
    tmp = tmp_path / ".tmp"
    tmp.mkdir()
    monkeypatch.setenv(graph_tools.OUT_DIR_ENV, str(tmp))
    _stub_impact(monkeypatch, 4, flow_n=70)
    res = _run("graph_impact", {"canonical_id": "c0", "limit": 50, "evidence_limit": 0})
    assert len(res["impact"]) == 4 and res["count"] == 4 and "result_file" not in res
    shown = len(res["flow_candidates"])
    assert 0 < shown <= 50 and res["summary"]["flow_total"] == 70 and res["summary"]["total"] == 4   # 件数の上限とバイトの上限の小さい方
    assert all(c["flow_edges"] == 2 and c["canonical_id"].startswith("f") for c in res["flow_candidates"])
    assert {"kind": "result_cap", "stage": "flow"} in res["coverage"]["limits"]
    assert {"kind": "result_cap", "stage": "impact"} not in res["coverage"]["limits"]
    assert res["coverage"]["flow"] == {"requested": 3, "truncated": False}
    assert res["coverage"]["omitted"] == 70 - shown and res["coverage"]["complete"] is False
    assert res["transfer"]["other_parts"]["flow"]["next_offset"] == shown
    rel = res["flow_result_file"]
    assert rel.startswith(".tmp/graph_impact/flow-") and rel.endswith(".jsonl")
    lines = [json.loads(x) for x in (tmp_path / rel).read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 70 and {"canonical_id", "path", "via", "flow_edges"} <= set(lines[0])
    assert lines[0]["via"][:2] == ["FLOWS_TO", "FLOWS_TO"]


def test_impact_and_flow_overflow_write_two_separate_files(monkeypatch, tmp_path):
    tmp = tmp_path / ".tmp"
    tmp.mkdir()
    monkeypatch.setenv(graph_tools.OUT_DIR_ENV, str(tmp))
    _stub_impact(monkeypatch, 60, flow_n=60)
    res = _run("graph_impact", {"canonical_id": "c0", "limit": 50, "evidence_limit": 0})
    assert res["result_file"] != res["flow_result_file"]
    assert {x.name[:5] for x in (tmp / graph_tools.IMPACT_FILE_DIR).iterdir()} == {"impac", "flow-"}
    assert res["coverage"]["omitted"] == 120 - len(res["impact"]) - len(res["flow_candidates"])


def test_flow_depth_zero_returns_the_previous_shape(monkeypatch):
    _stub_impact(monkeypatch, 3, flow_n=5)
    res = _run("graph_impact", {"canonical_id": "c0", "flow_depth": 0})
    assert "flow_candidates" not in res and "flow_total" not in res["summary"] and "flow" not in res["coverage"]
    assert len(res["impact"]) == 3 and res["coverage"]["complete"] is True


def test_flow_depth_limit_is_declared_and_an_unknown_flow_stage_is_not_complete(monkeypatch):
    _stub_impact(monkeypatch, 2, flow_n=1, info_extra={"flow_depth_truncated": True})
    res = _run("graph_impact", {"canonical_id": "c0", "flow_depth": 2})
    assert res["coverage"]["flow"] == {"requested": 2, "truncated": True} and res["truncated"] is True
    assert {"kind": "depth", "stage": "flow"} in res["coverage"]["limits"] and res["coverage"]["omitted"] is None
    _stub_impact(monkeypatch, 2, flow_n=1, info_extra={"flow_depth_truncated": None, "flow_depth_check_limit": "timeout"})
    res = _run("graph_impact", {"canonical_id": "c0"})
    assert {"kind": "timeout", "stage": "flow"} in res["coverage"]["limits"] and res["coverage"]["flow"]["truncated"] is None


def test_oversized_result_drops_flow_candidates_before_impact():
    big = [{"canonical_id": f"f{i}", "name": "x" * 200} for i in range(40)]
    impact = [{"canonical_id": f"c{i}", "name": "y" * 50} for i in range(10)]
    result = {"impact": impact, "flow_candidates": big,
              "coverage": {"complete": True, "limits": [], "omitted": 0}}
    out, clipped = graph_tools.fit_to_bytes(result, 3000)
    assert clipped and out["impact"] == impact and len(out["flow_candidates"]) < 40
    assert {"kind": "result_cap", "stage": "flow"} in out["coverage"]["limits"]
    assert out["coverage"]["omitted"] == 40 - len(out["flow_candidates"])
