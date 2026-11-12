"""取り込みの計測（待ちの理由ごとの時間・機械全体のメモリの標本）。DB/ネットワーク不要。

段ごとの `stage_timings` に待ちの理由ごとの時間とメモリの標本が入ること、機械全体のメモリと取り込みのプロセス群の RSS の
標本と最大値が記録されること、/proc が無い環境では「取れない」と記録して取り込みを続けることを固定する。
/proc の代わりに、同じ並びのフォルダ（`proc_root`）を一時ディレクトリに作って読ませる。
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest  # noqa: E402

from sherpa import store  # noqa: E402
from sherpa.ingest import md_prefetch, mem_guard, resource_meter, substage, worker  # noqa: E402
from test_worker_ing1_ing2 import _stub_pipeline  # noqa: E402,F401  取り込みを DB 無しで駆動する土台

_GIB_KB = 1024 * 1024


def _meminfo(proc, *, total_gib: int, avail_gib: int) -> None:
    proc.mkdir(parents=True, exist_ok=True)
    (proc / "meminfo").write_text(
        f"MemTotal:       {total_gib * _GIB_KB} kB\nMemFree:         1 kB\nMemAvailable:   {avail_gib * _GIB_KB} kB\n",
        encoding="ascii")


def _proc_entry(proc, pid: int, ppid: int, rss_mb: int, comm: str = "python") -> None:
    d = proc / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194560\n", encoding="ascii")
    (d / "status").write_text(f"Name:\t{comm}\nVmRSS:\t{rss_mb * 1024} kB\n", encoding="ascii")


@pytest.fixture
def fake_proc(tmp_path):
    """ワーカー(100) → 子(101) → 孫(102)、無関係のプロセス(200)。RSS は 300/200/100/900 MB。"""
    proc = tmp_path / "proc"
    _meminfo(proc, total_gib=16, avail_gib=10)
    _proc_entry(proc, 100, 1, 300)
    _proc_entry(proc, 101, 100, 200, comm="soffice (bin)")     # 名前に空白・括弧があっても親を読み違えない
    _proc_entry(proc, 102, 101, 100)
    _proc_entry(proc, 200, 1, 900)
    (proc / "self").mkdir()                                    # 数字でない項目は読み飛ばす
    return proc


@pytest.fixture(autouse=True)
def _clean_meter():
    resource_meter.end()
    yield
    resource_meter.end()


def test_tree_rss_sums_worker_and_descendants_only(fake_proc):
    total, n = resource_meter.read_tree_rss(100, str(fake_proc))
    assert (total, n) == ((300 + 200 + 100) * 1024 * 1024, 3)
    sys_total, sys_avail = resource_meter.read_system_memory(str(fake_proc))
    assert (sys_total, sys_avail) == (16 * 1024 ** 3, 10 * 1024 ** 3)


def test_waits_are_recorded_per_stage_and_reason():
    resource_meter.begin(proc_root="/nonexistent-proc")
    resource_meter.set_stage("office_md")
    with resource_meter.waiting(resource_meter.RESOURCE, task="md_prefetch_memory"):
        time.sleep(0.05)
    resource_meter.record_wait(resource_meter.RESOURCE, 0.25, task="md_prefetch_memory")
    resource_meter.record_wait(resource_meter.EXTERNAL, 1.0, task="embed")
    resource_meter.set_stage("es_index")
    resource_meter.record_wait(resource_meter.RETRY, 2.0)
    resource_meter.record_wait(resource_meter.QUEUE, 0.5)

    office = resource_meter.stage_summary("office_md")["waits"]
    assert set(office) == {"resource", "external"}
    assert office["resource"]["count"] == 2 and office["resource"]["ms"] >= 290
    assert office["resource"]["tasks"] == {"md_prefetch_memory": office["resource"]["ms"]}
    assert office["external"] == {"ms": 1000, "count": 1, "tasks": {"embed": 1000}}
    es = resource_meter.stage_summary("es_index")["waits"]
    assert es == {"retry": {"ms": 2000, "count": 1}, "queue": {"ms": 500, "count": 1}}

    whole = resource_meter.snapshot()["waits"]
    assert whole["resource"]["count"] == 2 and whole["retry"] == {"ms": 2000, "count": 1}


def test_waiting_outside_a_run_is_a_noop_and_unknown_reason_is_rejected():
    with resource_meter.waiting(resource_meter.RESOURCE):
        pass
    resource_meter.record_wait(resource_meter.RETRY, 1.0)
    assert resource_meter.snapshot() is None and resource_meter.stage_summary("office_md") == {}
    with pytest.raises(ValueError):
        with resource_meter.waiting("sleeping"):
            pass


def test_waiting_records_even_when_the_wrapped_work_raises():
    resource_meter.begin(proc_root="/nonexistent-proc")
    resource_meter.set_stage("es_index")
    with pytest.raises(RuntimeError):
        with resource_meter.waiting(resource_meter.EXTERNAL, task="embed"):
            raise RuntimeError("応答なし")
    assert resource_meter.stage_summary("es_index")["waits"]["external"]["count"] == 1


def test_samples_record_machine_memory_and_process_tree_peaks(fake_proc):
    resource_meter.begin(proc_root=str(fake_proc), pid=100)
    resource_meter.set_stage("office_md")
    _meminfo(fake_proc, total_gib=16, avail_gib=4)                  # 使用量が増え、空きが減る
    _proc_entry(fake_proc, 102, 101, 600)                           # 孫が膨らむ
    resource_meter.sample_if_due(0)
    _meminfo(fake_proc, total_gib=16, avail_gib=12)                 # その後に戻る（最大・最小は残る）
    _proc_entry(fake_proc, 102, 101, 100)
    resource_meter.sample_if_due(0)

    mem = resource_meter.stage_summary("office_md", sample=False)["mem"]
    assert mem["available"] is True and mem["samples"] == 2         # 段に入ってからの 2 回（開始時の標本は段の前）
    assert mem["mem_total_mb"] == 16 * 1024
    assert mem["used_peak_mb"] == 12 * 1024                         # 16 - 4
    assert mem["avail_min_mb"] == 4 * 1024
    assert mem["rss_peak_mb"] == 300 + 200 + 600 and mem["rss_procs_peak"] == 3
    whole = resource_meter.snapshot(sample=False)["mem"]
    assert whole["used_peak_mb"] == 12 * 1024 and whole["avail_min_mb"] == 4 * 1024
    assert "RSS" in resource_meter.latest_note() and "空き 12288MB" in resource_meter.latest_note()


def test_sample_if_due_waits_for_the_interval(fake_proc):
    resource_meter.begin(proc_root=str(fake_proc), pid=100)
    assert resource_meter.snapshot(sample=False)["mem"]["samples"] == 1
    for _ in range(5):
        resource_meter.sample_if_due(3600)                          # 間隔に満たない間は読みにいかない
    assert resource_meter.snapshot(sample=False)["mem"]["samples"] == 1
    resource_meter.sample_if_due(0)
    assert resource_meter.snapshot(sample=False)["mem"]["samples"] == 2


def test_missing_proc_is_recorded_as_unavailable_and_the_run_continues(tmp_path):
    resource_meter.begin(proc_root=str(tmp_path / "no-proc"))
    resource_meter.set_stage("office_md")
    resource_meter.sample_if_due(0)
    resource_meter.record_wait(resource_meter.RESOURCE, 0.1)

    stage = resource_meter.stage_summary("office_md")
    assert stage["mem"] == {"available": False, "reason": "no_proc"}
    assert stage["waits"]["resource"]["count"] == 1                  # 待ちの計測は /proc が無くても続く
    whole = resource_meter.snapshot()
    assert whole["mem"] == {"available": False, "reason": "no_proc"}
    assert resource_meter.latest_note() == ""


def test_heartbeat_samples_on_its_interval_and_adds_the_latest_sample(fake_proc, monkeypatch, caplog):
    monkeypatch.setattr(substage, "HEARTBEAT_SEC", 0.2)
    resource_meter.begin(proc_root=str(fake_proc), pid=100)
    caplog.set_level("INFO", logger="sherpa.ingest.convert")
    st = substage.begin("ledger_rows", "台帳の行を作っています")
    _meminfo(fake_proc, total_gib=16, avail_gib=6)
    time.sleep(0.9)
    st.end()
    assert resource_meter.snapshot(sample=False)["mem"]["samples"] >= 2        # 開始 + 見張りの標本
    assert resource_meter.snapshot(sample=False)["mem"]["avail_min_mb"] == 6 * 1024
    still = [r.getMessage() for r in caplog.records if r.getMessage().startswith("まだ動いています")]
    assert still and "機械の使用" in still[-1] and "空き 6144MB" in still[-1]


def _fake_begin(monkeypatch, proc_root):
    orig = resource_meter.begin
    monkeypatch.setattr(resource_meter, "begin", lambda: orig(proc_root=str(proc_root), pid=100))


def _capture_snapshot(monkeypatch) -> dict:
    captured: dict = {}

    def _finish(run_id, world, *, status, extraction_snapshot=None, **kw):
        captured["snap"] = extraction_snapshot
        return {"id": run_id, "status": status}
    monkeypatch.setattr(store, "finish_ingest_run_and_confirm_world", _finish)
    return captured


def test_run_records_waits_and_memory_in_stage_timings_and_log(fake_proc, monkeypatch, _stub_pipeline, caplog):
    _fake_begin(monkeypatch, fake_proc)
    captured = _capture_snapshot(monkeypatch)

    def _derived(world, **_kw):
        with resource_meter.waiting(resource_meter.RESOURCE, task="md_prefetch_memory"):
            time.sleep(0.05)
        resource_meter.sample_if_due(0)
        return {"converted": 1, "failed": 0, "unsupported": 0, "by_ext": {}}
    monkeypatch.setattr(worker, "_build_derived", _derived)
    caplog.set_level("INFO", logger="sherpa.ingest.convert")

    res = worker.run("w")
    assert res["status"] == "auto_published"
    snap = captured["snap"]
    office = snap["stage_timings"]["office_md"]
    assert office["waits"]["resource"]["ms"] >= 40 and office["waits"]["resource"]["count"] == 1
    assert office["waits"]["resource"]["tasks"]["md_prefetch_memory"] >= 40
    assert office["mem"]["available"] is True and office["mem"]["rss_peak_mb"] == 600
    assert "waits" not in snap["stage_timings"]["finalize"]                    # 待ちの無い段にキーは付かない
    assert snap["resource"]["mem"]["used_peak_mb"] == 6 * 1024                 # 16GiB - 10GiB
    assert snap["resource"]["waits"]["resource"]["count"] == 1
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("取り込みの計測")]
    assert any("資源待ち" in m and "RSS 最大 600MB" in m for m in lines), lines
    assert any(m.startswith("取り込みの計測: 取り込み全体") for m in lines), lines
    assert resource_meter.snapshot() is None                                   # 取り込みの終わりで計測は閉じる


def test_run_without_proc_records_unavailable_and_still_publishes(tmp_path, monkeypatch, _stub_pipeline):
    _fake_begin(monkeypatch, tmp_path / "no-proc")
    captured = _capture_snapshot(monkeypatch)

    res = worker.run("w")
    assert res["status"] == "auto_published"
    snap = captured["snap"]
    assert snap["resource"]["mem"] == {"available": False, "reason": "no_proc"}
    assert snap["stage_timings"]["office_md"]["mem"] == {"available": False, "reason": "no_proc"}
    assert snap["stage_timings"]["office_md"]["elapsed_ms"] >= 0


def test_run_failure_closes_the_meter(monkeypatch, _stub_pipeline):
    monkeypatch.setattr(worker, "build_world_graph",
                        lambda world: (_ for _ in ()).throw(RuntimeError("グラフ構築が落ちた")))
    with pytest.raises(RuntimeError):
        worker.run("w")
    assert resource_meter.snapshot() is None


def test_prefetch_memory_wait_is_counted_as_a_resource_wait(monkeypatch):
    reads = {"n": 0}
    monkeypatch.setattr(mem_guard, "total_bytes", lambda: 16 * 1024 ** 3)

    def avail():
        reads["n"] += 1
        return 8 * 1024 ** 3 if reads["n"] > 4 else 100 * 1024 ** 2
    monkeypatch.setattr(mem_guard, "available_bytes", avail)
    monkeypatch.setattr(mem_guard, "_POLL_SEC", 0.02)
    pf = md_prefetch.Prefetcher.__new__(md_prefetch.Prefetcher)      # プロセスプールは起こさず、待つ条件だけ動かす
    pf._parallel = 2
    resource_meter.begin(proc_root="/nonexistent-proc")
    resource_meter.set_stage("office_md")

    assert pf._memory_ready(wait=True) is True
    waits = resource_meter.stage_summary("office_md")["waits"]["resource"]
    assert waits["count"] == 1 and waits["ms"] >= 40
    assert set(waits["tasks"]) == {"md_prefetch_memory"}
    assert pf._parallel == 2                                          # 待ちきる前に戻ったので並列は減らない
