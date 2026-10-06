"""影響一覧: Codex がグラフの道具でたどった結果の記録から作る（要約ではない・たどっていないときは「影響なし」と出さない）。"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
from types import SimpleNamespace

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
os.environ["SHERPA_MCP_WORLD"] = "v1"
os.environ.pop("SHERPA_MCP_SCOPE", None)

from sherpa import mcp_server as M  # noqa: E402
from sherpa import tool_call_log as T  # noqa: E402
from sherpa.providers.codex import impact_list as IL  # noqa: E402
from sherpa.providers.codex.turn_state import TurnToolUse  # noqa: E402

TURN = "turn-g"
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _impact_result(n=3):
    return {"start": {"canonical_id": "c0", "kind": "Class", "name": "OrderBatch", "path": "src/OrderBatch.java"},
            "impact": [{"canonical_id": f"c{i}", "kind": "Class", "name": f"Caller{i}", "path": f"src/Caller{i}.java",
                        "distance": 1, "route": [{"doc": "x"}]} for i in range(1, n + 1)]
            + [{"canonical_id": "cs", "kind": "File", "name": "secret", "path": "keys/server.pem", "distance": 2}],
            "count": n + 1,
            "coverage": {"complete": False, "omitted": 4,
                         "limits": [{"kind": "depth", "stage": "impact"}, {"kind": "plugin_failed", "plugin": "p1"}],
                         "depth": {"requested": 5, "truncated": True}},
            "unresolved": {"items": [{"path": "src/a.java"}, {"path": "src/b.java"}]}}


def _st(graph=None, rows=None, parent_tools=None, read_docs=(), child_spawned=False, unreliable=False, broken=0):
    merged = T.MergedCallLog()
    merged.rows = rows if rows is not None else [
        {"call_id": g["call_id"], "tool": g["tool"], "status": "ok", "role": "parent"} for g in (graph or [])]
    merged.graph = graph or []
    merged.graph_broken = broken
    tu = TurnToolUse()
    tu.child_spawned, tu.record_unreliable = child_spawned, unreliable
    return SimpleNamespace(call_log=merged, _parent_mcp_tools=parent_tools or {1: [g["tool"] for g in (graph or [])]},
                           _mcp_read_docs=list(read_docs), _tool_use=tu)


def _graph_entry(call_id="1:1", **view):
    return {"call_id": call_id, "tool": "graph_impact", "role": "parent", **view}


def test_mcp_writes_graph_rows_to_their_own_file_and_not_to_the_call_log(tmp_path, monkeypatch):
    monkeypatch.setenv(T.ENV_DIR, str(tmp_path))
    monkeypatch.setenv("SHERPA_MCP_TURN_ID", TURN)
    monkeypatch.setattr(M, "_call_log_writer", None)
    M._seen_tool_calls.clear()
    monkeypatch.setattr(M.tool_dispatch, "run_tool", lambda *a, **kw: (_impact_result(), set(), [], []))
    M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "graph_impact", "arguments": {"canonical_id": "c0"}}})
    calls = next(tmp_path.glob("calls-*.jsonl")).read_text(encoding="utf-8")
    assert "Caller1" not in calls and "OrderBatch" not in calls  # 道具の記録（ログの元）にグラフの行は入れない
    merged = T.merge_call_logs(tmp_path, TURN)
    T.merge_graph_results(tmp_path, TURN, merged)
    (g,) = merged.graph
    assert g["call_id"] == merged.rows[0]["call_id"] and g["role"] == merged.rows[0].get("role", "undetermined")
    assert [r["name"] for r in g["rows"]] == ["Caller1", "Caller2", "Caller3"]  # 秘匿名のファイルの行は入れない
    assert g["start"]["name"] == "OrderBatch" and g["unresolved"] == 2 and g["complete"] is False


def test_rows_carry_origin_states_and_fixed_reasons_and_cap_with_more(monkeypatch):
    g = _graph_entry(**T.graph_view("graph_impact", _impact_result(5)))
    env = {"data": {"claims": [{"evidence_refs": ["src/Caller1.java:10"]}]}}
    st = _st([g], read_docs=["src\\Caller2.java"])
    out = IL.build_impact_list(st, env, {"lens": "impact"})
    by = {r["name"]: r for r in out["rows"]}
    assert out["traced"] is True and by["OrderBatch"]["role"] == "origin"
    assert (by["Caller1"]["state"], by["Caller2"]["state"], by["Caller3"]["state"]) == ("used", "inspected", "candidate")
    assert "secret" not in by
    assert IL._LIMIT_REASON["depth"] in out["reasons"] and IL._LIMIT_REASON["plugin_failed"] in out["reasons"]
    assert "解決できなかった参照が 2 件あります" in out["reasons"]
    monkeypatch.setattr(IL, "IMPACT_LIST_MAX_ROWS", 3)
    capped = IL.build_impact_list(st, env, {"lens": "impact"})
    assert len(capped["rows"]) == 3 and capped["more"] == 3  # 返った全部（起点＋5）のうち省いた件数
    nopath = _graph_entry(rows=[{"name": "Orphan", "path": ""}], start={"name": "S", "path": "src/S.java"}, complete=True, limits=[])
    assert {r["name"]: r["state"] for r in IL.build_impact_list(_st([nopath]), env, {"lens": "impact"})["rows"]}["Orphan"] == "unmapped"


def test_not_traced_or_unrecorded_is_never_reported_as_no_impact():
    none = IL.build_impact_list(_st([]), {}, {"lens": "impact"})
    assert none["traced"] is False and none["rows"] == [] and IL.REASON_NOT_TRACED in none["reasons"]
    assert IL.build_impact_list(_st([]), {}, {"lens": "qa"}) is None
    assert IL.build_impact_list(_st([_graph_entry(**T.graph_view("graph_impact", _impact_result()))]), {}, {"lens": "author"}) is None
    st = _st([]); st.call_log = None
    assert IL.build_impact_list(st, {}, {"lens": "impact"})["reasons"] == [IL.REASON_NO_RECORD]
    # 親はグラフを呼んだのに結果が記録に無い・子の結果が取れない
    lost = IL.build_impact_list(_st([], parent_tools={1: ["graph_impact"]}, child_spawned=True, unreliable=True), {}, {"lens": "impact"})
    assert lost["traced"] is False and IL.REASON_RECORD_MISSING in lost["reasons"] and IL.REASON_CHILD_UNKNOWN in lost["reasons"]
    failed = _graph_entry(error="graph_unavailable", complete=False, limits=[{"kind": "graph_unavailable"}], tool="graph_resolve")
    assert IL._LIMIT_REASON["graph_unavailable"] in IL.build_impact_list(_st([failed]), {}, {"lens": "impact"})["reasons"]


def test_screen_table_distinguishes_states_and_not_traced_shows_why():
    node = shutil.which("node")
    if not node:
        pytest.skip("node が見つからない")
    src = (ROOT / "web" / "chat" / "render.js").read_text(encoding="utf-8")
    snippet = src[src.index("const IMPACT_STATE_LABEL"):src.index("function renderFoundDocs")].replace("export ", "")
    common = (ROOT / "web" / "common.js").read_text(encoding="utf-8")
    esc = common[common.index("const _sherpaEsc"):common.index("\n\n", common.index("const _sherpaEsc"))] + "\nconst esc = _sherpaEsc;"
    traced = {"impact_list": {"v": 1, "traced": True, "reasons": ["深さの上限で止まり、先にまだ影響が残っている可能性があります"],
                              "rows": [{"name": "OrderBatch", "path": "src/O.java", "role": "origin", "state": "used", "reason": "回答の根拠に出てきます"},
                                       {"name": "Orphan", "path": "", "role": "affected", "state": "unmapped", "reason": "資料の場所が分からず、回答の根拠と結べません"}]}}
    untraced = {"impact_list": {"v": 1, "traced": False, "rows": [], "reasons": ["グラフで影響をたどった記録がありません"]}}
    script = esc + "\n" + snippet + f"\nconsole.log(JSON.stringify([renderImpactList({json.dumps(traced)}), renderImpactList({json.dumps(untraced)})]));"
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    a, b = json.loads(r.stdout)
    assert "impact-used" in a and "impact-unmapped" in a and "対応不明" in a and "起点" in a and "深さの上限" in a
    assert "影響をたどっていません" in b and "記録がありません" in b and "影響なし" not in a + b
