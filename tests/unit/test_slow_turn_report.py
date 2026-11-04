"""回答の時間の調べ（`scripts/slow_turn_report.py`）が、資料の名前やパスを出さずに道具の呼び出しを分けられるか。"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


R = _load("slow_turn_report")
CT = _load("conversation_trace")


def test_exec_code_is_split_into_tools_and_shell_programs_without_paths():
    code = ('const [a, b] = await Promise.all([tools.mcp__sherpa__ripgrep_search({query:"受付"}), '
            'tools.mcp__sherpa__es_search({query:"仕様"})]);\n'
            'const r = await tools.exec_command({cmd:"sed -n \'1,40p\' /data/kb/w/設計/詳細設計書.md","workdir":"/x"});\n'
            "const s = await tools.exec_command({cmd:'cat /data/kb/w/顧客一覧.csv'});")
    parts = R.exec_parts(CT, code)
    assert parts == ["ripgrep_search", "es_search", "シェル sed", "シェル cat"]
    assert R._call_key(parts) == "es_search・ripgrep_search・シェル cat…"
    assert "設計" not in "".join(parts) and "/data" not in "".join(parts)


def test_unknown_programs_and_odd_names_are_not_shown():
    assert R.exec_parts(CT, 'await tools.exec_command({cmd:"./顧客集計.sh --all"})') == ["シェル その他"]
    assert R._tool_name("mcp__sherpa__帳票") == "その他"
    assert R._code("ConnectionRefusedError") == "ConnectionRefusedError"
    assert R._code("/mnt/share/設計 が見つかりません") == "その他"


def test_busy_time_does_not_double_count_overlapping_calls():
    calls = [SimpleNamespace(ts=0.0, duration_ms=10_000), SimpleNamespace(ts=5.0, duration_ms=10_000),
             SimpleNamespace(ts=30.0, duration_ms=2_000), SimpleNamespace(ts=40.0, duration_ms=None)]
    assert R._busy_seconds(calls) == pytest.approx(17.0)


@pytest.fixture
def ledger_world():
    import uuid
    from sherpa import store
    wid = "slowledger_" + uuid.uuid4().hex[:8]
    yield wid
    store.replace_documents(wid, [])
    with store._connect() as c:
        c.execute("DELETE FROM ingest_runs WHERE version=%s", (wid,))


def _row(name, doctype="cobol", branch="source", status="indexed"):
    return {"name": name, "layer": "version", "scope_path": "secretsys", "doctype": doctype, "branch": branch,
            "original_path": None, "md_path": None, "status": status}


def test_ledger_section_tells_whether_listing_walks_and_hides_names(ledger_world):
    """一覧の道具が台帳から出せるか（空の台帳・名前で補えない行は歩く）を、資料の名前と接続先を出さずに示す。"""
    from sherpa import store
    assert R.ledger_status(ledger_world)["listing"] == "walk"          # 台帳が空

    store.replace_documents(ledger_world, [_row("secretsys/src/SECRETPROG.cbl"),
                                           _row("secretsys/src/SECRETX.cbl", None, None, "unreadable")])
    run = store.start_ingest_run(ledger_world)
    store.finish_ingest_run(run["id"], status="failed", extraction_snapshot={"flags": [
        {"doc": "secretsys/src/SECRETBAD.cbl", "action": "blocked", "reason": "unreadable_code_file"},
        {"doc": None, "action": "blocked", "reason": "graph_reflect_failed:MemoryPoolOutOfMemoryError@secrethost:7687（上げる設定）"}]})
    got = R.ledger_status(ledger_world)
    assert got["listing"] == "ledger" and got["rows"] == 2 and got["missing_kind"] == 1 and got["not_completable"] == 0
    assert got["runs"][0]["blocked"] == 2
    assert got["runs"][0]["reasons"] == {"unreadable_code_file": 1, "graph_reflect_failed:MemoryPoolOutOfMemoryError": 1}

    run = store.start_ingest_run(ledger_world)    # 空きの不足で止まった取り込みは、止めた件数が 0 でも止まったと分かる
    store.finish_ingest_run(run["id"], status="failed", extraction_snapshot={
        "stop_reason": "low_disk", "flags": [{"doc": None, "action": "warn", "reason": "low_disk_stop:1.2"}]})
    assert R.ledger_status(ledger_world)["runs"][0]["status"] == "failed（low_disk）"

    store.replace_documents(ledger_world, [_row("secretsys/src/SECRETPROG.cbl"),
                                           _row("secretsys/src/SECRETNOEXT", None, None, "unreadable")])
    got = R.ledger_status(ledger_world)
    assert got["listing"] == "walk" and got["not_completable"] == 1   # 名前で種類を補えない行が 1 件で台帳をあきらめる

    text = R.render([], [], None, detail=True, ledgers=[{"world": "W1", **got}])
    assert "資料フォルダを全部歩く" in text
    for secret in ("secretsys", "SECRET", "secrethost", "7687", ledger_world):
        assert secret not in text
