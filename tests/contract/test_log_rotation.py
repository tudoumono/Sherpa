"""起動時ログローテーション（LOG-2・2026-09-03）の受け入れテスト。

不具合: `scripts/start.sh` の `: > "$APP_LOG"` が起動のたびに run ログを空へ切り詰めていた
（前回の障害調査ができない）。`scripts/run-common.sh::sherpa_rotate_log` は既存ログが非空なら
タイムスタンプ付きへ退避してから空で作り直し、`sherpa_prune_log_family` が保持数
（`SHERPA_LOG_KEEP`・既定10）超過分の退避ファイルだけを古い順に削除する。
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "scripts" / "run-common.sh"


def _run_rotate(log: Path, *, keep: str | None = None) -> subprocess.CompletedProcess:
    script = f'ROOT="{ROOT}"; . "{COMMON}"; sherpa_rotate_log "{log}"'
    env = {"PATH": "/usr/bin:/bin"}
    if keep is not None:
        env["SHERPA_LOG_KEEP"] = keep
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)


def _archives(tmp_path: Path, stem: str = "api", suffix: str = ".log") -> list[Path]:
    # `sherpa_rotate_log` の退避命名規約（run-common.sh）に厳密一致するものだけを数える
    # （`api-notes.log` のような緩く似た無関係ファイルは対象外）。
    pattern = re.compile(rf"^{re.escape(stem)}-\d{{8}}-\d{{6}}(?:-\d+)?{re.escape(suffix)}$")
    return sorted(p for p in tmp_path.iterdir() if pattern.match(p.name))


def test_empty_or_missing_log_is_not_archived(tmp_path: Path):
    """起動1回目（ログ不在）は退避せず、空のログを新規に作るだけ。"""
    log = tmp_path / "api.log"
    r = _run_rotate(log)
    assert r.returncode == 0, r.stderr
    assert log.exists() and log.read_text() == ""
    assert _archives(tmp_path) == []


def test_nonempty_log_is_archived_with_timestamp_and_truncated(tmp_path: Path):
    """既存ログが非空なら `api-YYYYmmdd-HHMMSS.log` へ退避し、`api.log` は空で作り直される
    （起動2回で前回ログが退避される、の最小再現）。"""
    log = tmp_path / "api.log"
    log.write_text("run 1 の内容\n", encoding="utf-8")
    r = _run_rotate(log)
    assert r.returncode == 0, r.stderr
    assert log.read_text() == ""
    archives = _archives(tmp_path)
    assert len(archives) == 1
    assert archives[0].read_text(encoding="utf-8") == "run 1 の内容\n"
    assert archives[0].name.startswith("api-") and archives[0].name.endswith(".log")


def test_keep_count_prunes_oldest_first(tmp_path: Path):
    """保持数（SHERPA_LOG_KEEP）超過分は最古のものから削除される。無関係なファイルは触らない。"""
    log = tmp_path / "api.log"
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep me", encoding="utf-8")
    stray = tmp_path / "api-notes.log"   # 命名が緩く似ているが退避パターンには一致しない
    stray.write_text("keep me too", encoding="utf-8")

    for i in range(4):
        log.write_text(f"run {i}\n", encoding="utf-8")
        r = _run_rotate(log, keep="2")
        assert r.returncode == 0, r.stderr
        time.sleep(1.1)   # タイムスタンプ（秒精度）の重複を避ける

    archives = _archives(tmp_path)
    assert len(archives) == 2, [p.name for p in archives]
    # 直近2回分（run 2 / run 3）だけが残る＝最古（run 0 / run 1）が削除された。
    contents = sorted(p.read_text(encoding="utf-8") for p in archives)
    assert contents == ["run 2\n", "run 3\n"]
    # 無関係ファイルは削除も改変もされない。
    assert unrelated.read_text(encoding="utf-8") == "keep me"
    assert stray.read_text(encoding="utf-8") == "keep me too"


def test_keep_count_invalid_falls_back_to_default(tmp_path: Path):
    """SHERPA_LOG_KEEP が数値でなければ既定（10）にフォールバックする（0件は削除されない想定の範囲）。"""
    log = tmp_path / "api.log"
    log.write_text("run\n", encoding="utf-8")
    r = _run_rotate(log, keep="not-a-number")
    assert r.returncode == 0, r.stderr
    assert len(_archives(tmp_path)) == 1


def test_caddy_log_family_is_independent_of_api_log_family(tmp_path: Path):
    """異なる stem（api / caddy）は別ファミリーとして扱われ、互いの保持数プルーニングに影響しない。"""
    api_log = tmp_path / "api.log"
    caddy_log = tmp_path / "caddy.log"
    api_log.write_text("api run\n", encoding="utf-8")
    caddy_log.write_text("caddy run\n", encoding="utf-8")
    assert _run_rotate(api_log, keep="1").returncode == 0
    assert _run_rotate(caddy_log, keep="1").returncode == 0
    assert len(_archives(tmp_path, stem="api")) == 1
    assert len(_archives(tmp_path, stem="caddy")) == 1
