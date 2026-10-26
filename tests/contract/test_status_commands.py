"""サーバー状態と取り込み状態を別々に読める運用コマンドの出力契約。"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def _fake_status_commands(tmp_path: Path) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *\"compose version\"*) exit 0 ;;\n"
        "  *\"info --format\"*) echo 17179869184; exit 0 ;;\n"
        "esac\n"
        "if [ \"$1\" = info ]; then exit 0; fi\n"
        "if [ \"$1\" = compose ]; then\n"
        "  eval 'last=${'$'#}'\n"
        "  case \" $* \" in *\" ps -q \"*) echo \"${last}-cid\" ;; esac\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1\" = inspect ]; then\n"
        "  case \"$*\" in *Health*) echo healthy ;; *) echo running ;; esac\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    python = venv / "python"
    python.write_text("#!/bin/sh\necho NONE\n", encoding="utf-8")
    python.chmod(0o755)
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    return bindir, env_file


def test_status_shows_overall_server_rows_and_final_guidance(tmp_path):
    bindir, env_file = _fake_status_commands(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "RUN_DIR": str(tmp_path / "run"),
        "SHERPA_ENV_FILE": str(env_file),
        "SHERPA_LAN": "0",
        "SHERPA_VENV": str(tmp_path / "venv"),
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/status.sh")], check=False, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "止まっているもの: アプリ・取り込みワーカー・OCRワーカー"
    assert "[未使用]" in next(line for line in lines if "Caddy" in line)
    assert "使っていない" in next(line for line in lines if "Caddy" in line)
    assert "取り込み中:" not in result.stdout
    assert lines.index("=== 案内 ===") > lines.index("=== メモリの割り振り ===")
    assert "全部を起動: make start" in lines[-2]

    descriptions = {
        "PostgreSQL": "動いています",
        "Elasticsearch": "動いています",
        "Neo4j": "動いています",
        "アプリ": "未起動",
        "取り込みワーカー": "未起動",
        "Caddy": "使っていない",
        "OCRワーカー": "未起動",
    }
    starts = set()
    for name, description in descriptions.items():
        line = next(row for row in lines if name in row and description in row)
        starts.add(_display_width(line[:line.index(description)]))
    assert starts == {33}


def _ingest_status_module():
    spec = importlib.util.spec_from_file_location("ingest_status", ROOT / "scripts/ingest_status.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completed_at_from(runs):
    """正常終了の run の反映時刻を、store の照会と同じ条件で返す。"""
    return next((r["published_at"] for r in runs
                 if r.get("status") in ("auto_published", "auto_published_with_flags")
                 and r.get("published_at") is not None), None)


def _brief(run):
    """run の行を、store の `get_last_finished_run_brief` が返す形にする。"""
    return {"status": run["status"], "created_at": run["created_at"], "snapshot": run.get("extraction_snapshot")}


def _patch_reads(module, monkeypatch, *, worlds, latest=None, finished=None, completed_at=None, ocr=None):
    monkeypatch.setattr(module.store, "list_worlds_db", lambda **_k: worlds)
    monkeypatch.setattr(module.ingest_store, "get_latest_run_progress", lambda *_a, **_k: latest)
    monkeypatch.setattr(module.ingest_store, "get_last_finished_run_brief", lambda *_a, **_k: finished)
    monkeypatch.setattr(module.ingest_store, "get_latest_completed_run_at", lambda *_a, **_k: completed_at)
    monkeypatch.setattr(module.ocr_jobs, "get_progress_counts", lambda *_a, **_k: ocr or {
        "pending": 0, "processed": 0, "failed": 0, "unpublished": 0,
    })


def test_ingest_status_prints_one_folder_from_read_boundaries(monkeypatch, capsys):
    module = _ingest_status_module()
    now = datetime.now(UTC)
    _patch_reads(
        module, monkeypatch,
        worlds=[{"world_id": "sample", "label": "共有資料", "last_sig": "a" * 64}],
        latest={"status": "extracting", "created_at": now - timedelta(seconds=65),
                "progress": {"stage_label": "全文検索へ反映中"}},
        finished=_brief({"status": "failed", "created_at": now - timedelta(days=1),
                         "extraction_snapshot": {"flags": [{"reason": "es_index_failed:bulk_failed"}]}}),
        completed_at=now - timedelta(days=2),
        ocr={"pending": 2, "processed": 3, "failed": 1, "unpublished": 4},
    )

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "資料フォルダ: 共有資料（sample）" in output
    assert "今の取り込み: 取り込み中／段: 全文検索へ反映中／経過:" in output
    assert "前回の失敗:" in output and "全文検索への反映に失敗したため" in output
    assert f"最後まで終わった取り込み: {module._time_text(now - timedelta(days=2))}" in output
    assert "待ち 2件／読み取り済み 3件／失敗 1件／公開待ち 4件" in output


def _print_status_for_runs(module, monkeypatch, capsys, runs, *, last_sig="a" * 64, completed_at=None):
    finished = next((run for run in runs if run.get("status") not in ("queued", "extracting")), None)
    _patch_reads(
        module, monkeypatch,
        worlds=[{"world_id": "sample", "label": "sample", "last_sig": last_sig}],
        latest=runs[0] if runs else None,
        finished=_brief(finished) if finished else None,
        completed_at=completed_at if completed_at is not None else _completed_at_from(runs),
    )
    assert module.main() == 0
    return capsys.readouterr().out


def test_ingest_status_failed_run_with_published_at_is_not_completed(monkeypatch, capsys):
    module = _ingest_status_module()
    now = datetime.now(UTC)
    runs = [
        {"status": "failed", "created_at": now - timedelta(hours=1), "published_at": now - timedelta(hours=1),
         "extraction_snapshot": {"flags": [{"reason": "es_index_failed:bulk_failed"}]}},
        {"status": "auto_published_with_flags", "created_at": now - timedelta(days=5),
         "published_at": now - timedelta(days=4)},
    ]
    output = _print_status_for_runs(module, monkeypatch, capsys, runs)
    assert f"最後まで終わった取り込み: {module._time_text(now - timedelta(days=4))}" in output
    assert "前回の失敗: " in output and "前回の失敗: なし" not in output

    output = _print_status_for_runs(module, monkeypatch, capsys, runs[:1])
    assert "最後まで終わった取り込み: 記録なし" in output


def test_ingest_status_finds_completion_older_than_recent_unchanged_runs(monkeypatch, capsys):
    module = _ingest_status_module()
    now = datetime.now(UTC)
    unchanged = [{"status": "auto_published", "created_at": now - timedelta(minutes=i), "published_at": None}
                 for i in range(50)]
    older = now - timedelta(days=7)
    output = _print_status_for_runs(module, monkeypatch, capsys, unchanged, completed_at=older)
    assert f"最後まで終わった取り込み: {module._time_text(older)}" in output


def test_ingest_status_hides_failure_older_than_latest_success(monkeypatch, capsys):
    module = _ingest_status_module()
    now = datetime.now(UTC)
    runs = [
        {"status": "auto_published", "created_at": now - timedelta(hours=1),
         "published_at": now - timedelta(hours=1)},
        {"status": "failed", "created_at": now - timedelta(days=1), "published_at": None,
         "extraction_snapshot": {"interrupted": True}},
    ]
    assert "前回の失敗: なし" in _print_status_for_runs(module, monkeypatch, capsys, runs)

    queued_after_failure = [{"status": "queued", "created_at": now, "published_at": None}, runs[1]]
    assert "前回の失敗: " in _print_status_for_runs(module, monkeypatch, capsys, queued_after_failure)
    assert "前回の失敗: なし" not in _print_status_for_runs(module, monkeypatch, capsys, queued_after_failure)


def test_ingest_status_queries_ocr_by_generation_id(monkeypatch, capsys):
    module = _ingest_status_module()
    signature = "world-signature-not-a-generation-id"
    expected = module.derived_generation.generation_id_for(signature)
    seen = []
    _patch_reads(module, monkeypatch, worlds=[{"world_id": "sample", "label": "sample", "last_sig": signature}])
    monkeypatch.setattr(module.ocr_jobs, "get_progress_counts", lambda *a, **_k: seen.append(a) or {
        "pending": 1, "processed": 0, "failed": 0, "unpublished": 2,
    })
    assert module.main() == 0
    output = capsys.readouterr().out
    assert seen == [("sample", expected)]
    assert "待ち 1件／" in output and "公開待ち 2件" in output


def test_ingest_status_handles_no_folders_and_database_failure(monkeypatch, capsys):
    module = _ingest_status_module()
    monkeypatch.setattr(module.store, "list_worlds_db", lambda **_k: [])
    assert module.main() == 0
    assert capsys.readouterr().out == "取り込みの状態\n資料フォルダはありません。\n"

    def unavailable(**_k):
        raise OSError("unreachable")

    monkeypatch.setattr(module.store, "list_worlds_db", unavailable)
    assert module.main() == 0
    assert capsys.readouterr().out == "データベースに届きません\n"

    def timed_out(**_k):
        raise module.pg_errors.QueryCanceled("canceled")

    monkeypatch.setattr(module.store, "list_worlds_db", timed_out)
    assert module.main() == 0
    assert capsys.readouterr().out == "データベースに届きません\n"


def test_ingest_status_classified_reasons_match_failure_reasons():
    module = _ingest_status_module()
    for raw in module._CLASSIFIED_REASONS:
        known = module.failure_reasons.describe(raw)["code"] != "other"
        assert known or raw in ("world_not_found", "world_dir_unreachable"), raw
        assert module._reason_text({"flags": [{"reason": raw}]}) != "詳しい理由はログを確認してください", raw
    for prefix in module._CLASSIFIED_PREFIXES:
        assert module._reason_text({"flags": [{"reason": prefix + ":x"}]}) != "詳しい理由はログを確認してください"


def test_ingest_status_read_timeout_follows_environment(monkeypatch):
    module = _ingest_status_module()
    monkeypatch.delenv("SHERPA_INGEST_STATUS_TIMEOUT_SEC", raising=False)
    assert module._read_timeout_ms() == 10000
    monkeypatch.setenv("SHERPA_INGEST_STATUS_TIMEOUT_SEC", "2.5")
    assert module._read_timeout_ms() == 2500
    for broken in ("abc", "0", "-3"):
        monkeypatch.setenv("SHERPA_INGEST_STATUS_TIMEOUT_SEC", broken)
        assert module._read_timeout_ms() == 10000


def test_makefile_exposes_both_status_commands():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    status = (ROOT / "scripts/status.sh").read_text(encoding="utf-8")
    assert "status:" in makefile and "./scripts/status.sh" in makefile
    assert "ingest-status:" in makefile and "./scripts/ingest_status.sh" in makefile
    assert '取り込み中: ${active_world}（詳しくは make ingest-status）' in status
    assert "処理中" not in status
