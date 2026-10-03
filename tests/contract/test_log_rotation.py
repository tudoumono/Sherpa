"""起動時ログローテーション（`sherpa_rotate_log`・scripts/run-common.sh）の受け入れテスト。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "scripts" / "run-common.sh"


def _run_rotate(log: Path, *, keep: str | None = None, extra_bin: Path | None = None) -> subprocess.CompletedProcess:
    """`extra_bin` を PATH の先頭へ足す（`date` をフェイクに差し替えるため）。"""
    keep_arg = f' "{keep}"' if keep is not None else ""
    script = f'ROOT="{ROOT}"; . "{COMMON}"; sherpa_rotate_log "{log}"{keep_arg}'
    path = f"{extra_bin}:/usr/bin:/bin" if extra_bin is not None else "/usr/bin:/bin"
    return subprocess.run(["bash", "-c", script], env={"PATH": path}, capture_output=True, text=True, timeout=30)


def _archives(tmp_path: Path, stem: str = "api", suffix: str = ".log") -> list[Path]:
    # 退避命名規約（api-YYYYmmdd-HHMMSS.log）に厳密一致するものだけを数える
    pattern = re.compile(rf"^{re.escape(stem)}-\d{{8}}-\d{{6}}(?:-\d+)?{re.escape(suffix)}$")
    return sorted(p for p in tmp_path.iterdir() if pattern.match(p.name))


def test_empty_or_missing_log_is_not_archived_and_nonempty_is_archived_and_truncated(tmp_path: Path):
    log = tmp_path / "api.log"
    r = _run_rotate(log)
    assert r.returncode == 0, r.stderr
    assert log.exists() and log.read_text() == "" and _archives(tmp_path) == []   # 起動 1 回目は空のログを作るだけ
    log.write_text("run 1 の内容\n", encoding="utf-8")
    assert _run_rotate(log, keep="not-a-number").returncode == 0   # 保持数が数値でなければ既定へ
    assert log.read_text() == ""
    archives = _archives(tmp_path)
    assert len(archives) == 1 and archives[0].read_text(encoding="utf-8") == "run 1 の内容\n"


def test_keep_count_prunes_oldest_first_and_families_are_independent(tmp_path: Path):
    log = tmp_path / "api.log"
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep me", encoding="utf-8")
    stray = tmp_path / "api-notes.log"   # 命名が緩く似ているが退避パターンには一致しない
    stray.write_text("keep me too", encoding="utf-8")
    # 退避名は `date +%Y%m%d-%H%M%S`（秒精度）に依存する——呼び出しごとに単調増加する値を返すフェイクへ差し替える
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    counter_file = tmp_path / "date_counter"
    fake_date = fake_bin / "date"
    fake_date.write_text(
        "#!/usr/bin/env bash\n"
        f'n=0; [ -f "{counter_file}" ] && n=$(cat "{counter_file}")\n'
        "n=$((n + 1))\n"
        f'echo "$n" > "{counter_file}"\n'
        'printf "20260101-%06d\\n" "$n"\n', encoding="utf-8")
    fake_date.chmod(0o755)
    for i in range(4):
        log.write_text(f"run {i}\n", encoding="utf-8")
        assert _run_rotate(log, keep="2", extra_bin=fake_bin).returncode == 0
    archives = _archives(tmp_path)
    assert sorted(p.read_text(encoding="utf-8") for p in archives) == ["run 2\n", "run 3\n"]   # 最古から削除される
    assert unrelated.read_text(encoding="utf-8") == "keep me" and stray.read_text(encoding="utf-8") == "keep me too"
    # 別の stem（caddy）は別ファミリーで、互いの保持数に影響しない
    caddy = tmp_path / "caddy.log"
    caddy.write_text("caddy run\n", encoding="utf-8")
    assert _run_rotate(caddy, keep="1").returncode == 0
    assert len(_archives(tmp_path, stem="caddy")) == 1 and len(_archives(tmp_path, stem="api")) == 2
