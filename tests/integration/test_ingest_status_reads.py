"""`make ingest-status`（scripts/ingest_status.py）を実 DB で動かす。重い run があっても、1つの読み取りが時間切れでも、表示が壊れないこと。"""
from __future__ import annotations

import hashlib
import importlib.util
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from sherpa import store
from sherpa.store import ingest as ingest_store
from sherpa.store.db import _connect_bounded

ROOT = Path(__file__).resolve().parents[2]
_UNREAD = "時間内に読めませんでした"
_BIG = 30000


def _init_or_skip():
    try:
        store.init_schema()
    except Exception as exc:
        pytest.skip(f"infra down: {exc}")


def _module():
    spec = importlib.util.spec_from_file_location("ingest_status_real", ROOT / "scripts/ingest_status.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def folders(monkeypatch):
    """この試験が作った資料フォルダだけを一覧に出す。終了時に行を消す。"""
    _init_or_skip()
    created: list[str] = []
    module = _module()
    real_list = store.list_worlds_db

    def register(label: str, *, with_sig: bool = False) -> str:
        world = f"test-ingst-{uuid.uuid4().hex[:12]}"
        store.upsert_world(world, f"/nonexistent/{world}", label)
        if with_sig:
            store.set_world_sig(world, "a" * 64)
        created.append(world)
        return world

    monkeypatch.setattr(
        module.store, "list_worlds_db",
        lambda **kw: [row for row in real_list(**kw) if row["world_id"] in created],
    )
    yield module, register
    with store._connect() as c:
        for world in created:
            c.execute("DELETE FROM ingest_runs WHERE version=%s", (world,))
            c.execute("DELETE FROM ocr_jobs WHERE world=%s", (world,))
            c.execute("DELETE FROM worlds WHERE world_id=%s", (world,))


def _failed_run(world: str, snapshot: dict) -> None:
    ingest_store.add_ingest_run(
        world, status="failed", source_doc_ids=[f"doc-{i}.md" for i in range(_BIG)],
        extraction_snapshot=snapshot,
    )


def _failure_line(output: str) -> str:
    return next(line for line in output.splitlines() if line.startswith("  前回の失敗: "))


def _heavy_flags(tail_reason: str | None) -> list[dict]:
    flags = [{"doc": f"doc-{i}.md", "action": "warn", "reason": "unclassified_reason"} for i in range(_BIG)]
    if tail_reason is not None:
        flags.append({"doc": None, "action": "blocked", "reason": tail_reason})
    return flags


def _many_unclassified(count: int) -> list[dict]:
    return [{"doc": None, "action": "warn", "reason": f"unclassified_reason_{i}"} for i in range(count)]


@pytest.mark.parametrize("snapshot_factory", [
    lambda: {"flags": _heavy_flags("es_index_failed:bulk_failed")},
    lambda: {"flags": _heavy_flags("graph_reflect_failed:x")},
    lambda: {"flags": _heavy_flags(None)},
    lambda: {"stop_reason": "low_disk", "flags": _heavy_flags(None)},
    lambda: {"interrupted": True, "flags": _heavy_flags("es_index_failed:bulk_failed")},
    lambda: {"flags": _many_unclassified(60) + [{"doc": None, "action": "blocked", "reason": "es_index_failed:x"}]},
    lambda: {"flags": _many_unclassified(60) + [{"doc": None, "action": "blocked", "reason": "esXindex_failed:x"}]},
    lambda: {"flags": [{"reason": "esXindex_failed:x"}, {"reason": "rebindXfoo"}, {"reason": "world_not_found"}]},
    lambda: {"flags": ["not-an-object", {"reason": 3}, {"reason": "pg_replace_failed:x"}]},
    lambda: {"flags": []},
    lambda: {},
])
def test_failure_reason_matches_full_snapshot_reading(folders, capsys, snapshot_factory):
    module, register = folders
    world = register("重い履歴")
    snapshot = snapshot_factory()
    _failed_run(world, snapshot)

    assert module.main() == 0
    output = capsys.readouterr().out
    expected = module._reason_text(snapshot)
    assert _failure_line(output).endswith(f"／{expected}")
    assert "データベースに届きません" not in output


def test_timed_out_read_does_not_break_the_next_reads(folders, capsys, monkeypatch):
    module, register = folders
    world = register("止まる一覧", with_sig=True)
    _failed_run(world, {"interrupted": True})
    monkeypatch.setenv("SHERPA_INGEST_STATUS_TIMEOUT_SEC", "0.3")

    blocker = psycopg.connect(store._dsn(), autocommit=False)
    try:
        blocker.execute("SET lock_timeout = '5s'")
        blocker.execute("LOCK TABLE ingest_runs IN ACCESS EXCLUSIVE MODE")
        assert module.main() == 0
        blocked = capsys.readouterr().out
    finally:
        blocker.rollback()
        blocker.close()

    assert "データベースに届きません" not in blocked
    assert f"最後まで終わった取り込み: {_UNREAD}" in blocked
    assert f"今の取り込み: {_UNREAD}" in blocked
    assert f"前回の失敗: {_UNREAD}" in blocked
    assert "画像内文字の読み取り: 待ち 0件／読み取り済み 0件／失敗 0件／公開待ち 0件" in blocked

    assert module.main() == 0
    recovered = capsys.readouterr().out
    assert _UNREAD not in recovered
    assert "前回の失敗: " in recovered and "途中で停止したため" in recovered


def test_one_folder_timing_out_does_not_hide_the_other(folders, capsys, monkeypatch):
    module, register = folders
    slow = register("遅い方", with_sig=True)
    fast = register("速い方", with_sig=True)
    ingest_store.add_ingest_run(fast, status="auto_published", published_snapshot={"nodes": 1, "edges": 0})
    _failed_run(slow, {"interrupted": True})
    monkeypatch.setenv("SHERPA_INGEST_STATUS_TIMEOUT_SEC", "0.3")

    real_counts = module.ocr_jobs.get_progress_counts

    def counts(world, generation, *, connect_timeout, statement_timeout_ms):
        if world == slow:
            with _connect_bounded(connect_timeout, statement_timeout_ms) as c:
                c.execute("SELECT pg_sleep(5)")
        return real_counts(world, generation, connect_timeout=connect_timeout,
                           statement_timeout_ms=statement_timeout_ms)

    monkeypatch.setattr(module.ocr_jobs, "get_progress_counts", counts)

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "データベースに届きません" not in output
    sections = output.split("\n\n")
    slow_section = next(text for text in sections if "資料フォルダ: 遅い方" in text)
    fast_section = next(text for text in sections if "資料フォルダ: 速い方" in text)

    assert f"画像内文字の読み取り: {_UNREAD}" in slow_section
    assert "前回の失敗: " in slow_section and "途中で停止したため" in slow_section
    assert "今の取り込み: なし" in slow_section

    assert _UNREAD not in fast_section
    assert "今の取り込み: なし" in fast_section
    assert "前回の失敗: なし" in fast_section
    assert "最後まで終わった取り込み: 記録なし" not in fast_section
    assert "画像内文字の読み取り: 待ち 0件／読み取り済み 0件／失敗 0件／公開待ち 0件" in fast_section


def test_status_does_not_wait_for_schema_initialisation(folders, capsys, monkeypatch):
    module, register = folders
    world = register("初期化待ちなし")
    _failed_run(world, {"interrupted": True})
    monkeypatch.setenv("SHERPA_INGEST_STATUS_TIMEOUT_SEC", "1")
    monkeypatch.setattr(store.db, "_inited", False)
    key = int.from_bytes(hashlib.sha1(f"schema:{store._KB_ID}".encode("utf-8")).digest()[:8], "big", signed=True)

    holder = psycopg.connect(store._dsn(), autocommit=True)
    try:
        holder.execute("SELECT pg_advisory_lock(%s)", (key,))
        done = threading.Event()

        def run():
            module.main()
            done.set()

        started = time.monotonic()
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        finished = done.wait(timeout=8)
        elapsed = time.monotonic() - started
    finally:
        holder.execute("SELECT pg_advisory_unlock(%s)", (key,))
        holder.close()
    worker.join(timeout=20)

    assert finished, "状態コマンドがスキーマ初期化の lock を待って戻らない"
    assert elapsed < 8
    output = capsys.readouterr().out
    assert "データベースに届きません" not in output
    assert "途中で停止したため" in output
