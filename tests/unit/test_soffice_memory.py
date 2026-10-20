"""soffice のメモリの守り（`ingest/mem_guard.py`）の単体テスト。

上限を超えた変換がその資料だけの失敗になること・時間切れ／メモリ超えの資料を原本が変わるまでやり直さないこと・バッチの失敗で原因の資料の名前がログに残ること・
空きメモリが足りない間は先読みが待ち、待ちきったら並列を減らして進むことを、偽 soffice と空きメモリの読み取りの差し替えで確かめる。
"""
from __future__ import annotations

import json
import stat
import zipfile

import pytest

from sherpa.ingest import failure_reasons, md_prefetch, mem_guard, office_md
from sherpa.ingest.arms import legacy_convert, legacy_prefetch

_DOCX_XML = ('<?xml version="1.0"?>'
             '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
             '<w:body><w:p><w:r><w:t>旧資料の中身テキストXYZ</w:t></w:r></w:p></w:body></w:document>')

_FAKE_SOFFICE = """#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "LibreOffice 7.5.0.0 fake"; exit 0; fi
fmt=""; outdir=""; inputs=()
while [ $# -gt 0 ]; do
  case "$1" in
    --headless) shift;;
    -env:*) shift;;
    --convert-to) fmt="$2"; shift 2;;
    --outdir) outdir="$2"; shift 2;;
    *) inputs+=("$1"); shift;;
  esac
done
echo "${#inputs[@]}" >> "$FAKE_LAUNCHES"
for f in "${inputs[@]}"; do
  if LC_ALL=C grep -aq HUGE "$f"; then echo "terminate called after throwing 'std::bad_alloc'" >&2; kill -ABRT $$; fi
  if LC_ALL=C grep -aq SLOW "$f"; then sleep 5; fi
  if LC_ALL=C grep -aq BAD "$f"; then continue; fi
  stem=$(basename "$f"); stem="${stem%.*}"
  cp "$FAKE_TEMPLATE" "$outdir/$stem.$fmt"
done
if [ -n "$FAKE_BATCH_EXIT" ] && [ "${#inputs[@]}" -gt 1 ]; then exit "$FAKE_BATCH_EXIT"; fi
"""


def _setup(tmp_path, monkeypatch, *, workers="0", batch="4", memory_limit=True):
    script = tmp_path / "fake_soffice.sh"
    script.write_text(_FAKE_SOFFICE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    template = tmp_path / "template.docx"
    with zipfile.ZipFile(template, "w") as z:
        z.writestr("word/document.xml", _DOCX_XML)
    launches = tmp_path / "launches.txt"
    monkeypatch.setenv("SHERPA_SOFFICE_BIN", str(script))
    monkeypatch.setenv("SHERPA_MCP_LEGACY_BACKEND", "libreoffice")
    monkeypatch.setenv("SHERPA_LEGACY_WORKERS", workers)
    monkeypatch.setenv("SHERPA_LEGACY_BATCH", batch)
    monkeypatch.setenv("FAKE_TEMPLATE", str(template))
    monkeypatch.setenv("FAKE_LAUNCHES", str(launches))
    monkeypatch.setenv("RUN_DIR", str(tmp_path / "run"))
    monkeypatch.delenv("SHERPA_MCP_ARMS", raising=False)
    monkeypatch.delenv("SHERPA_LEGACY_TIMEOUT", raising=False)
    legacy_convert._version_cache.clear()
    if memory_limit:        # macOS でも「Linux で上限が付く」経路を通す（setrlimit が効かない環境では何もしないだけ）
        monkeypatch.setattr(mem_guard, "limit_supported", lambda: True)
        monkeypatch.setattr(mem_guard, "total_bytes", lambda: 16 * 1024 ** 3)
    return launches


def _src(tmp_path, files: dict[str, bytes]):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    for name, body in files.items():
        (src / name).write_bytes(b"\xd0\xcf\x11\xe0 " + body)
    return src


def _launches(p) -> list[str]:
    return p.read_text(encoding="utf-8").split() if p.exists() else []


def _messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records]


# ---- ① 子の soffice に上限が付く ----

@pytest.mark.parametrize("supported,expect_limit", [(True, True), (False, False)], ids=["linux", "macos"])
def test_soffice_child_gets_address_space_limit_only_where_it_works(tmp_path, monkeypatch, supported, expect_limit):
    """Linux では子の起動前に RLIMIT_AS を設定し、効かない macOS では何もしない。値は割り当ての記録が優先。"""
    _setup(tmp_path, monkeypatch, memory_limit=False)
    monkeypatch.setattr(mem_guard, "limit_supported", lambda: supported)
    run = tmp_path / "run"
    run.mkdir()
    (run / "mem_budget.json").write_text(json.dumps({"libreoffice_bytes": 3 * 1024 ** 3}), encoding="utf-8")

    seen = {}
    real_popen = legacy_convert.subprocess.Popen

    def spy(cmd, **kw):
        if cmd != ["soffice"]:
            return real_popen(cmd, **kw)
        seen["preexec"] = kw.get("preexec_fn")
        return real_popen(["true"], **{k: v for k, v in kw.items() if k != "preexec_fn"})
    monkeypatch.setattr(legacy_convert.subprocess, "Popen", spy)
    legacy_convert._run_soffice(["soffice"], "x.doc")

    if not expect_limit:
        assert seen["preexec"] is None
        return
    calls = []
    import resource
    monkeypatch.setattr(resource, "setrlimit", lambda which, lim: calls.append((which, lim)))
    seen["preexec"]()
    assert calls == [(resource.RLIMIT_AS, (3 * 1024 ** 3, 3 * 1024 ** 3))]


@pytest.mark.parametrize("total_gb,expect_gb", [(8, 3), (15, 3), (24, 3.6), (64, 4)], ids=["small", "15gb", "mid", "large"])
def test_limit_rule_is_15_percent_of_host_memory_clamped(tmp_path, monkeypatch, total_gb, expect_gb):
    monkeypatch.setenv("RUN_DIR", str(tmp_path / "none"))
    monkeypatch.setattr(mem_guard, "total_bytes", lambda: total_gb * 1024 ** 3)
    assert mem_guard.soffice_limit_bytes() == int(expect_gb * 1024 ** 3)


def test_budget_file_is_preferred_but_never_below_what_soffice_needs_to_start(tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_DIR", str(tmp_path))
    monkeypatch.setattr(mem_guard, "total_bytes", lambda: 15 * 1024 ** 3)
    (tmp_path / "mem_budget.json").write_text(json.dumps({"libreoffice_mb": 2500}), encoding="utf-8")
    assert mem_guard.soffice_limit_bytes() == 2500 * 1024 ** 2
    (tmp_path / "mem_budget.json").write_text(json.dumps({"libreoffice_bytes": 512 * 1024 ** 2}), encoding="utf-8")
    assert mem_guard.soffice_limit_bytes() == 2 * 1024 ** 3


# ---- ② 上限超えはその資料だけの失敗（理由 memory_exceeded）----

def test_memory_exceeded_fails_only_that_file_with_one_log_line(tmp_path, monkeypatch, caplog):
    _setup(tmp_path, monkeypatch)
    src = _src(tmp_path, {"普通.doc": b"ok", "巨大.doc": b"HUGE"})
    with caplog.at_level("INFO", logger="sherpa.ingest.convert"):
        rep = office_md.build_derived(src, tmp_path / "d" / "w" / "md")
    assert rep["converted"] == 1 and rep["failed"] == 1
    assert rep["legacy_conversion_failures"] == [{"doc": "巨大.doc", "reason": "memory_exceeded"}]
    assert failure_reasons.classify("memory_exceeded") == "memory_exceeded"
    lines = [m for m in _messages(caplog) if "巨大.doc" in m and "メモリの上限" in m]
    assert len(lines) == 1 and "普通.doc" not in lines[0]


# ---- ③ バッチの失敗で、原因の資料が名前つきのログに残る ----

def test_failed_batch_is_logged_and_culprit_file_is_named_by_single_conversion(tmp_path, monkeypatch, caplog):
    launches = _setup(tmp_path, monkeypatch, workers="1", batch="4", memory_limit=False)
    monkeypatch.setenv("FAKE_BATCH_EXIT", "1")
    names = [f"旧{i}.doc" for i in range(4)]
    src = _src(tmp_path, {n: (b"BAD" if n == "旧2.doc" else n.encode()) for n in names})
    with caplog.at_level("INFO", logger="sherpa.ingest.convert"), caplog.at_level("INFO", logger="sherpa.convert.libreoffice"):
        rep = office_md.build_derived(src, tmp_path / "d" / "w" / "md")
    assert rep["legacy_conversion_failures"] == [{"doc": "旧2.doc", "reason": "legacy_conversion_failed"}]
    msgs = _messages(caplog)
    batch = [m for m in msgs if "先変換のバッチが失敗" in m]
    assert len(batch) == 1 and "4件" in batch[0] and "異常終了" in batch[0]
    assert [m for m in msgs if "旧形式の変換に失敗しました" in m and "旧2.doc" in m]
    assert not [m for m in msgs if "旧形式の変換に失敗しました" in m and "旧0.doc" in m]
    assert _launches(launches)[0] == "4"


# ---- ④ 時間切れ・メモリ超え・変換の失敗は原本が変わるまでやり直さない ----

@pytest.mark.parametrize("body,reason", [(b"SLOW", "legacy_conversion_timeout"), (b"HUGE", "memory_exceeded"),
                                      (b"BAD", "legacy_conversion_failed")],
                         ids=["timeout", "memory", "failed"])
def test_timeout_memory_and_failed_conversions_are_not_retried_until_the_source_changes(tmp_path, monkeypatch, body, reason):
    launches = _setup(tmp_path, monkeypatch, workers="1")
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")
    src = _src(tmp_path, {"重い.doc": body})
    derived = tmp_path / "d" / "w" / "md"

    first = office_md.build_derived(src, derived)
    assert first["legacy_conversion_failures"] == [{"doc": "重い.doc", "reason": reason}]
    if reason == "legacy_conversion_failed":                              # 変換の失敗は 2 回続いてから＝1 回目の後はもう一度試す
        n = len(_launches(launches))
        office_md.build_derived(src, derived)
        assert len(_launches(launches)) > n
    after_first = len(_launches(launches))

    second = office_md.build_derived(src, derived)                       # 原本が同じ＝変換し直さない・失敗の一覧には理由つきで載る
    assert len(_launches(launches)) == after_first
    assert second["failed"] == 1 and second["legacy_conversion_failures"] == first["legacy_conversion_failures"]
    assert (derived / "重い.doc.md").is_file()                            # 資料名だけが検索に出る知らせは残る

    (src / "重い.doc").write_bytes(b"\xd0\xcf\x11\xe0 " + body + b" changed")   # 原本が変わった＝もう一度試す
    third = office_md.build_derived(src, derived)
    assert len(_launches(launches)) > after_first
    assert third["legacy_conversion_failures"] == first["legacy_conversion_failures"]


def test_timeout_is_retried_when_the_limit_is_extended(tmp_path, monkeypatch):
    launches = _setup(tmp_path, monkeypatch, workers="1")
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.3")
    src = _src(tmp_path, {"遅い.doc": b"SLOW"})
    derived = tmp_path / "d" / "w" / "md"
    office_md.build_derived(src, derived)
    n = len(_launches(launches))
    monkeypatch.setenv("SHERPA_LEGACY_TIMEOUT", "0.5")                    # 時間切れの設定を延ばした
    office_md.build_derived(src, derived)
    assert len(_launches(launches)) > n


# ---- ⑤ 空きメモリが足りない間は待ち、待ちきったら並列を減らして進む ----

def _low_memory(monkeypatch, *, recovers_after: int | None):
    """空きメモリを下限未満にする。`recovers_after` 回の読み取りの後に十分になる（None なら戻らない）。"""
    reads = {"n": 0}
    monkeypatch.setattr(mem_guard, "total_bytes", lambda: 16 * 1024 ** 3)       # 下限 1.6GB

    def avail():
        reads["n"] += 1
        return 8 * 1024 ** 3 if recovers_after is not None and reads["n"] > recovers_after else 100 * 1024 ** 2
    monkeypatch.setattr(mem_guard, "available_bytes", avail)
    monkeypatch.setattr(mem_guard, "_POLL_SEC", 0.01)
    monkeypatch.setattr(mem_guard, "_WAIT_MAX_SEC", 0.3)
    return reads


def test_wait_returns_when_memory_recovers_and_gives_up_at_the_maximum(monkeypatch, caplog):
    _low_memory(monkeypatch, recovers_after=3)
    with caplog.at_level("INFO", logger="sherpa.ingest.convert"):
        assert mem_guard.wait_for_free_memory("テスト") is True
    assert any("メモリの空きを待っています" in m for m in _messages(caplog))
    _low_memory(monkeypatch, recovers_after=None)
    assert mem_guard.wait_for_free_memory("テスト") is False


def test_legacy_prefetch_waits_then_runs_with_fewer_threads(tmp_path, monkeypatch, caplog):
    launches = _setup(tmp_path, monkeypatch, workers="3", batch="1", memory_limit=False)
    reads = _low_memory(monkeypatch, recovers_after=None)
    names = [f"旧{i}.doc" for i in range(6)]
    src = _src(tmp_path, {n: n.encode() for n in names})
    with caplog.at_level("INFO", logger="sherpa.ingest.convert"):
        rep = office_md.build_derived(src, tmp_path / "d" / "w" / "md")
    assert rep["converted"] == 6 and rep["failed"] == 0                  # 止まらず全件が済む
    assert reads["n"] > 0
    assert any("メモリの空きを待っています" in m for m in _messages(caplog))
    assert any("先変換の並列を 2 に減らして進めます" in m for m in _messages(caplog))


def test_md_prefetch_waits_before_handing_jobs_to_children_then_reduces_parallelism(tmp_path, monkeypatch, caplog):
    reads = _low_memory(monkeypatch, recovers_after=None)
    submitted = []

    class FakePool:
        def __init__(self, *a, **kw):
            pass

        def submit(self, fn, *args):
            submitted.append(args[3])
            from concurrent.futures import Future
            f = Future()
            f.set_exception(RuntimeError("not run"))
            return f

        def shutdown(self, **kw):
            pass
    monkeypatch.setattr(md_prefetch, "ProcessPoolExecutor", FakePool)
    jobs = [md_prefetch.Job(rel=f"{i}.docx", path=str(tmp_path / f"{i}.docx"), arm_name="ooxml", ext=".docx", size=1)
            for i in range(6)]
    with caplog.at_level("INFO", logger="sherpa.ingest.convert"):
        pf = md_prefetch.Prefetcher(jobs, 3)
    pf.close()
    assert reads["n"] > 0 and submitted                                  # 待ったうえで、止まらず子へ渡している
    assert any("メモリの空きを待っています" in m for m in _messages(caplog))
    assert any("MD化の先読みの並列を 2 に減らして進めます" in m for m in _messages(caplog))
    assert pf._parallel == 1                                             # 待ちきるたびに 1 つ減り、1 では待たない


# ---- ⑤ 空の旧形式は、失敗として 1 行ログに残り「完了」とは出ない ----

def test_empty_legacy_files_are_logged_as_failed_not_completed(tmp_path, monkeypatch, caplog):
    _setup(tmp_path, monkeypatch, workers="1")
    with zipfile.ZipFile(tmp_path / "template.docx", "w") as z:      # 0 バイトの .doc から LibreOffice が作る、本文のない文書
        z.writestr("word/document.xml", _DOCX_XML.replace("<w:p><w:r><w:t>旧資料の中身テキストXYZ</w:t></w:r></w:p>", ""))
    src = tmp_path / "src"
    src.mkdir()
    for name in ("空.doc", "空.xls"):
        (src / name).write_bytes(b"")
    with caplog.at_level("INFO", logger="sherpa.ingest.convert"):
        rep = office_md.build_derived(src, tmp_path / "d" / "w" / "md")
    assert {e["doc"] for e in rep["conversion_failures"]} == {"空.doc", "空.xls"}
    msgs = _messages(caplog)
    for name in ("空.doc", "空.xls"):
        assert len([m for m in msgs if "変換できませんでした" in m and name in m]) == 1
        assert [m for m in msgs if "MD化が失敗しました" in m and name in m]
        assert not [m for m in msgs if "MD化が完了" in m and name in m]
