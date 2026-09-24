"""`world_graph.build_world()` 経由のシェル/バッチ（POSIX/bat）・Spring Batch XML 統合テスト
（アナライザ拡張 波3 レーン B・docs/proposals/2026-09-05-アナライザ拡張.md §13）。

`shell.py` は本作業（波3 レーン B）で新規作成した未登録アナライザのため、`registry._ANALYZERS`
を monkeypatch して既存の登録済みアナライザ（`CobolAnalyzer`/`JavaAnalyzer`/`XmlConfigAnalyzer` 等）
と共存させたうえで `fixtures/corpus/batch1` を実際に `build_world()` へ通す
（`tests/unit/test_world_graph_web1.py` と同じ流儀）。

固定する内容: `. ./common.sh` の2段解決による `Batch(nightly.sh) -INVOKES(via=include)->
Batch(common.sh)`・`java -cp lib com.acme.BatchMain` の完全修飾名解決・`./PAYROLL` の COBOL 単独行
呼び出し・`$LOG_DIR` の A9（同世代の同名 `Config` キー全件）解決・Spring Batch の `<batch:job>`
children と `<batch:tasklet ref>` の `config_key` 解決・`sqlplus … @load.sql` の
`Dropped("shell_sql_script", ...)`。既存 golden（`test_world_graph_analyzer_expansion_common.py` 等）
は本テストの monkeypatch がテスト関数スコープに閉じるため不変のまま。
"""
from __future__ import annotations

import pathlib

import pytest

from sherpa.ingest import world_graph
from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers.shell import ShellBatchAnalyzer

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "batch1"
WORLD_ID = "batch1_test"


@pytest.fixture(autouse=True)
def _register_shell_analyzer(monkeypatch):
    monkeypatch.setattr(registry, "_ANALYZERS", (*registry.known_analyzers(), ShellBatchAnalyzer()))


def _build():
    return world_graph.build_world(WORLD_DIR, WORLD_ID)


def _edge_tuples(nodes, edges):
    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}
    return {(e["type"], by_cid[e["src"]], by_cid[e["dst"]], e.get("via")) for e in edges}


def test_nightly_sh_includes_common_sh_via_dot_source():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    nightly = ("Batch", "nightly.sh", "gen1/bin/nightly.sh")
    common = ("Batch", "common.sh", "gen1/bin/common.sh")
    assert ("INVOKES", nightly, common, "include") in tuples


def test_nightly_sh_invokes_batch_main_via_classpath_qualified_name():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    nightly = ("Batch", "nightly.sh", "gen1/bin/nightly.sh")
    batch_main = ("Module", "BatchMain", "gen1/com/acme/BatchMain.java")
    assert ("INVOKES", nightly, batch_main, "call") in tuples


def test_nightly_sh_invokes_payroll_cobol_program_via_bare_exec_line():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    nightly = ("Batch", "nightly.sh", "gen1/bin/nightly.sh")
    payroll = ("Module", "PAYROLL", "gen1/PAYROLL.cbl")
    assert ("INVOKES", nightly, payroll, "call") in tuples


def test_nightly_sh_accesses_log_dir_config_key_in_common_sh():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    nightly = ("Batch", "nightly.sh", "gen1/bin/nightly.sh")
    log_dir_key = ("Config", "LOG_DIR", "gen1/bin/common.sh")
    assert ("ACCESSES", nightly, log_dir_key, "config_key") in tuples


def test_batch_context_xml_contains_nightly_job_config_key():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    batch_ctx = ("Config", "batch-context.xml", "gen1/batch-context.xml")
    job_key = ("Config", "nightlyJob", "gen1/batch-context.xml")
    assert ("CONTAINS", batch_ctx, job_key, None) in tuples


def test_batch_context_xml_accesses_payroll_tasklet_config_key():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    batch_ctx = ("Config", "batch-context.xml", "gen1/batch-context.xml")
    tasklet_key = ("Config", "payrollTasklet", "gen1/batch-context.xml")
    assert ("ACCESSES", batch_ctx, tasklet_key, "config_key") in tuples


def test_sqlplus_script_invocation_is_dropped_not_a_node():
    _nodes, _edges, flags = _build()
    dropped = [f for f in flags if f.get("reason") == "dropped_syntax" and f.get("why") == "shell_sql_script"
              and f.get("from") == "gen1/bin/nightly.sh"]
    assert len(dropped) == 1 and dropped[0]["snippet"] == "load.sql"
