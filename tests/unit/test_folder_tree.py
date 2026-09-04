"""K6（`folder_tree`）の受け入れ条件テスト。

正典: `docs/proposals/2026-09-04-グラフのソース正典化.md` §3 K6・§4b S1。木の集計本体
（`sherpa/folder_tree.py`）は `doc_ledger.documents_for()` を monkeypatch した合成データで
精密に固定する（深さクランプ・per-フォルダ打切り・列挙件数の安全弁は fixtures の実木では
境界条件を作りにくいため）。配線（`agentic_search.run_tool`/`openai_tools`/`gemini_tools`/
`mcp_server._tool_defs`/SYSTEM）は実 fixtures（`v1`）で最小限の統合テストを添える。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import agentic_search, folder_tree, mcp_server  # noqa: E402


def _rows(*names_and_branch):
    """`doc_ledger.documents_for()` の戻り値相当（`name`＝rel_path・`branch` 省略時 "docs"）。"""
    out = []
    for item in names_and_branch:
        name, branch = item if isinstance(item, tuple) else (item, "docs")
        out.append({"name": name, "branch": branch, "doctype": "設計書", "state": "ready"})
    return out


_SAMPLE = _rows(
    "top/a/f1.md", "top/a/f2.md", "top/a/deep/f3.md", "top/b/f4.md",
    "other/f5.md", "loose.md",
)


def _patch(monkeypatch, rows):
    monkeypatch.setattr(folder_tree.doc_ledger, "documents_for",
                        lambda world, deadline=None: rows)


# ---- build(): 集計の形 ----

def test_build_aggregates_direct_total_and_subfolder_counts(monkeypatch):
    _patch(monkeypatch, _SAMPLE)
    res = folder_tree.build("v1", {})
    by_path = {f["path"]: f for f in res["folders"]}
    assert set(by_path) == {"top", "top/a", "top/a/deep", "top/b", "other"}

    assert by_path["top"] == {"path": "top", "depth": 1, "direct_files": 0,
                              "total_files": 4, "subfolders": 2, "truncated": False}
    assert by_path["top/a"] == {"path": "top/a", "depth": 2, "direct_files": 2,
                                "total_files": 3, "subfolders": 1, "truncated": False}
    assert by_path["top/a/deep"] == {"path": "top/a/deep", "depth": 3, "direct_files": 1,
                                     "total_files": 1, "subfolders": 0, "truncated": False}
    assert by_path["top/b"] == {"path": "top/b", "depth": 2, "direct_files": 1,
                                "total_files": 1, "subfolders": 0, "truncated": False}
    assert by_path["other"] == {"path": "other", "depth": 1, "direct_files": 1,
                                "total_files": 1, "subfolders": 0, "truncated": False}
    assert res["count"] == 5 and res["folders_truncated"] is False
    assert res["path_prefix"] == "" and res["depth"] == 3


def test_build_loose_root_file_creates_no_folder_entry(monkeypatch):
    """path_prefix 直下の裸ファイル（フォルダを持たない）はどのフォルダ集計にも現れない。"""
    _patch(monkeypatch, _rows("loose.md"))
    res = folder_tree.build("v1", {})
    assert res["folders"] == [] and res["count"] == 0


def test_build_depth_cutoff_marks_boundary_folder_truncated_but_keeps_real_subfolder_count(monkeypatch):
    """深さ上限に達し、まだ配下（deep）があるフォルダ（top/a）は truncated=True。
    total_files/subfolders は depth に関わらず正しい実数のまま（過小申告しない）。"""
    _patch(monkeypatch, _SAMPLE)
    res = folder_tree.build("v1", {"depth": 2})
    by_path = {f["path"]: f for f in res["folders"]}
    assert set(by_path) == {"top", "top/a", "top/b", "other"}   # top/a/deep は depth=2 で打ち切り
    assert by_path["top/a"]["truncated"] is True                # 深さ上限に達しまだ配下がある
    assert by_path["top/a"]["subfolders"] == 1                  # 打ち切っても実数は正しい
    assert by_path["top/a"]["total_files"] == 3                 # deep 配下の f3 も再帰件数に含む
    assert by_path["top"]["truncated"] is False                 # depth に達していないので打切りではない


def test_build_depth_clamped_to_1_and_10(monkeypatch):
    _patch(monkeypatch, _SAMPLE)
    assert folder_tree.build("v1", {"depth": 0})["depth"] == 1
    assert folder_tree.build("v1", {"depth": -5})["depth"] == 1
    assert folder_tree.build("v1", {"depth": 999})["depth"] == 10
    assert folder_tree.build("v1", {"depth": "not-a-number"})["depth"] == 3   # 既定へフォールバック
    assert folder_tree.build("v1", {})["depth"] == 3


def test_build_path_prefix_scopes_and_rebases_depth(monkeypatch):
    _patch(monkeypatch, _SAMPLE)
    res = folder_tree.build("v1", {"path_prefix": "top"})
    by_path = {f["path"]: f for f in res["folders"]}
    assert set(by_path) == {"top/a", "top/a/deep", "top/b"}   # "other"/"top" 自体は対象外
    assert by_path["top/a"]["depth"] == 1                     # prefix からの相対深さで再基準化
    assert by_path["top/a/deep"]["depth"] == 2


def test_build_entries_truncated_by_safety_valve_reports_total_count(monkeypatch):
    rows = _rows(*[f"f{i}/x.md" for i in range(5)])           # f0..f4 の5フォルダ
    _patch(monkeypatch, rows)
    orig = folder_tree._MAX_FOLDERS
    folder_tree._MAX_FOLDERS = 2
    try:
        res = folder_tree.build("v1", {})
        assert res["count"] == 5                              # 打ち切り前の総フォルダ数
        assert len(res["folders"]) == 2                        # 安全弁で2件だけ返す
        assert res["folders_truncated"] is True
    finally:
        folder_tree._MAX_FOLDERS = orig


def test_build_entries_truncated_by_byte_budget_reports_total_count(monkeypatch):
    """RV是正（rv-periphery #2）: `tool_result_max_bytes` の実効値までエントリ単位で詰め、
    件数上限（`_MAX_FOLDERS`）に達していなくてもバイト予算超過で打ち切る。`count` は打切り前の
    総数のまま・`folders_truncated` はバイト打切りでも真になる（黙って一部だけ返さない）。"""
    rows = _rows(*[f"f{i}/x.md" for i in range(5)])           # f0..f4 の5フォルダ（path はどれも2バイト）
    _patch(monkeypatch, rows)
    res = folder_tree.build("v1", {}, tool_result_max_bytes=6)   # "f0"+"f1"+"f2" で丁度6バイト
    assert res["count"] == 5                                    # 打ち切り前の総フォルダ数は不変
    assert len(res["folders"]) == 3                             # 3エントリ分でバイト予算を使い切る
    assert res["folders_truncated"] is True


def test_build_default_byte_budget_used_when_not_specified(monkeypatch):
    """`tool_result_max_bytes` 省略時はモジュール既定（`_DEFAULT_TOOL_RESULT_MAX_BYTES`）を使う
    ——少数フォルダの通常呼び出しでは打ち切りが発生しない（既存動作の非破壊確認）。"""
    _patch(monkeypatch, _SAMPLE)
    res = folder_tree.build("v1", {})
    assert res["folders_truncated"] is False
    assert len(res["folders"]) == 5


def test_run_tool_forwards_tool_result_max_bytes_to_folder_tree(monkeypatch):
    """`run_tool` は run 単位で解決した実効バイト予算（`tr_max_bytes`）をそのまま `folder_tree.
    build` へ転送する（他ツール＝doc_outline 等と同じ配線）。"""
    rows = _rows(*[f"f{i}/x.md" for i in range(5)])
    _patch(monkeypatch, rows)
    result, _docs, _cites, _cards = agentic_search.run_tool(
        "folder_tree", {}, "v1", None, tool_result_max_bytes=6)
    assert result["count"] == 5
    assert len(result["folders"]) == 3
    assert result["folders_truncated"] is True


def test_build_scope_paths_filters_before_counting(monkeypatch):
    _patch(monkeypatch, _SAMPLE)
    res = folder_tree.build("v1", {}, scope_paths=["top"])
    assert {f["path"] for f in res["folders"]} == {"top", "top/a", "top/a/deep", "top/b"}


def test_build_layer_docs_excludes_source_branch_files(monkeypatch):
    rows = _rows(("top/a/doc.md", "docs"), ("top/a/prog.cbl", "source"))
    _patch(monkeypatch, rows)
    res_docs = folder_tree.build("v1", {}, layer="docs")
    by_path = {f["path"]: f for f in res_docs["folders"]}
    assert by_path["top/a"]["total_files"] == 1                # source 側は数えない

    res_code = folder_tree.build("v1", {}, layer="code")
    by_path_code = {f["path"]: f for f in res_code["folders"]}
    assert by_path_code["top/a"]["total_files"] == 1            # docs 側は数えない


# ---- run_tool 配線（scope/layer/フォルダは docs に足さない）----

def test_run_tool_dispatches_and_does_not_add_folders_to_citation_docs(monkeypatch):
    _patch(monkeypatch, _SAMPLE)
    result, docs, cites, cards = agentic_search.run_tool("folder_tree", {}, "v1", None)
    assert result["count"] == 5
    assert docs == set() and cites == [] and cards == []       # フォルダは doc_id ではない


def test_run_tool_forwards_layer_to_folder_tree(monkeypatch):
    rows = _rows(("top/a/doc.md", "docs"), ("top/a/prog.cbl", "source"))
    _patch(monkeypatch, rows)
    result, _docs, _cites, _cards = agentic_search.run_tool("folder_tree", {}, "v1", None, layer="code")
    by_path = {f["path"]: f for f in result["folders"]}
    assert by_path["top/a"]["total_files"] == 1


# ---- ツール登録（§5 の各配線箇所）----

def test_registered_in_openai_tools():
    names = [t["function"]["name"] for t in agentic_search.openai_tools(with_es=True, with_graph=True)]
    assert "folder_tree" in names


def test_registered_in_gemini_tools():
    names = [f["name"] for f in agentic_search.gemini_tools(with_es=True, with_graph=True)[0]["functionDeclarations"]]
    assert "folder_tree" in names


def test_registered_in_mcp_tool_defs(monkeypatch):
    monkeypatch.delenv("SHERPA_MCP_ASK_DISABLED", raising=False)
    names = [d["name"] for d in mcp_server._tool_defs()]
    assert "folder_tree" in names


def test_mcp_tool_defs_es_search_still_placed_right_after_ripgrep_search(monkeypatch):
    """K6 で folder_tree を list_docs の直後に挿入したことで es_search の insert index がずれる
    退行を防ぐ（`insert_at`/固定 index を触るときの既知の罠・GEN-DIFF の教訓と同型）。"""
    monkeypatch.delenv("SHERPA_MCP_ASK_DISABLED", raising=False)
    monkeypatch.setattr(mcp_server.es_index, "available", lambda: True)
    names = [d["name"] for d in mcp_server._tool_defs()]
    assert names.index("es_search") == names.index("ripgrep_search") + 1


def test_system_prompt_mentions_folder_tree():
    assert "folder_tree" in agentic_search.SYSTEM
