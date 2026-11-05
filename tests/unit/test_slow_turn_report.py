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


def test_degrade_reasons_are_counted_per_day_and_tool_once():
    """結果のいちばん外側の degrade_reason だけを実際の道具ごとに数え、本文の中の同じ文字列や数え済みの呼び出しは数えない。"""
    th = SimpleNamespace(tid="t1")
    calls = [
        CT.Call(ts=1762300800.0, thread=th, call_id="c1", name="es_search",
                result_text='{"hits": [], "degrade_reason": "query_embed_failed", "mode_used": "keyword"}'),
        CT.Call(ts=1762300801.0, thread=th, call_id="c2", name="es_search",
                result_text='{"hits": [{"text": "{\\"degrade_reason\\": \\"es_unavailable\\"} secretdoc"}], "degrade_reason": null}'),
        CT.Call(ts=1762300802.0, thread=th, call_id="c3", name="read_doc",
                result_text='{"text": "degrade_reason: query_embed_failed"}'),
    ]
    seen: set = set()
    got = R.degrade_counts(CT, calls, seen)
    day = R.datetime.fromtimestamp(1762300800.0, R._JST).strftime("%m/%d")
    assert got == {f"{day}|es_search": {"query_embed_failed": 1, "なし": 1}}
    assert R.degrade_counts(CT, calls, seen) == {}          # 次の回答の窓に入っても数え直さない
    recent = {"since": "a", "until": "b", "conversations": 0, "truncated": False, "turns": [], "tools": {},
              "degrade": got}
    text = R.render([], [], recent, detail=True)
    assert "query_embed_failed 1" in text and "es_unavailable" not in text and "secretdoc" not in text


@pytest.fixture
def embed_settings():
    from sherpa import store
    before = store.get_system_settings()
    keys = ("embed_provider", "ollama_url")
    yield lambda **kv: store.set_system_settings("admin", kv)
    store.set_system_settings("admin", {k: before.get(k) for k in keys})


def test_embed_trial_reports_failure_kind_without_the_address(embed_settings, monkeypatch):
    """質問の埋め込みが届かないとき、失敗の種類だけを出し、接続先を出さない。"""
    for k in R._PROXY_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    embed_settings(embed_provider="ollama", ollama_url="http://127.0.0.1:9")
    got = R.embed_trial()
    assert got["provider"] == "ollama" and got["url_kind"] == "loopback" and got["proxy_env"] is False
    assert got["result"] == "failed" and got["error_type"]
    text = R.render([], [], None, detail=True, searches=[{"world": "W1", "index": {"es": "unavailable"},
                                                          "embed_tool": got, "embed_app": got}])
    assert "**失敗**" in text and "127.0.0.1" not in text and ":9" not in text

    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    assert R.embed_trial()["result"] == "disabled"


def test_rg_on_ledger_files_reads_only_source_and_unknown_kinds(ledger_world, tmp_path):
    """原本の rg を台帳で絞るとき、ソースと種類不明のファイルだけを渡し、資料の名前を出さない。"""
    from sherpa import rg_search, store
    rg = rg_search.find_rg()
    if not rg:
        pytest.skip("rg が無い")
    root, archives = tmp_path / "kb", tmp_path / "archives"
    for base, name in ((root, "secretsys/src/SECRETPROG.cbl"), (root, "secretsys/src/SECRETNOEXT"),
                       (root, "secretsys/doc/SECRETSPEC.docx"), (archives, "secretsys/SECRETPKG.zip/SECRETIN.cbl")):
        (base / name).parent.mkdir(parents=True, exist_ok=True)
        (base / name).write_text("x")
    store.replace_documents(ledger_world, [_row("secretsys/src/SECRETPROG.cbl"),
                                           _row("secretsys/src/SECRETNOEXT", None, None, "unreadable"),
                                           _row("secretsys/doc/SECRETSPEC.docx", "Word", "office"),
                                           _row("secretsys/SECRETPKG.zip/SECRETIN.cbl")])
    got = R.rg_ledger_files(ledger_world, rg, root, 30, archives)
    assert got["files"] == 3 and got["done"] is True and got["unreadable"] is False   # アーカイブの中は展開先から読む
    row = {"world": "W1", "place": "原本", "fs": "ext4", "network": False, "rg": {"seconds": 1.0, "done": True},
           "rg_skip_kinds": {"plan_s": 0.2, "plan_done": True, "exts": 3, "seconds": 0.5, "done": True}, "rg_ledger": got}
    text = R.render([row], [], None, detail=True)
    assert "台帳のソースと種類不明のファイルだけ" in text and "| 3 |" in text
    for secret in ("secretsys", "SECRET", str(tmp_path)):
        assert secret not in text


def test_deadline_stops_are_counted_from_the_tool_result():
    """道具のサーバーが期限で止めた呼び出し（途中までの結果・期限切れの失敗）だけを期限切れと数える。"""
    th = SimpleNamespace(tid="t1")
    partial = CT.Call(ts=0.0, thread=th, call_id="c1", name="ripgrep_search",
                      result_text='{"hits": [], "deadline_exceeded": true}')
    stopped = CT.Call(ts=0.0, thread=th, call_id="c2", name="list_docs", is_error=True,
                      result_text='{"error": "tool_deadline_exceeded", "notice": "x"}')
    plain = CT.Call(ts=0.0, thread=th, call_id="c3", name="read_doc",
                    result_text='{"text": "deadline_exceeded tool_deadline_exceeded"}')
    assert [R.deadline_hit(CT, c) for c in (partial, stopped, plain)] == [True, True, False]
