"""固定版 Codex CLI 導入（scripts/codex_install.sh・scripts/lib/codex_pin.sh）の契約テスト。

固定版・配布物名・sha256 は
scripts/codex-version.env に集約する。ここでは実ネットワークを一切使わず、取得元を
`CODEX_PIN_BASE_URL`（file:// スキーム）で差し替えて次の2つの安全性だけを固定する:

  - sha256 が固定値と一致しない配布物は導入しない（fail-closed・tools/codex/bin/codex を作らない）。
  - 配布物を取得できない（ネットワーク不可・閉域網を模す）場合は日本語で警告するだけで、
    `make start` の文脈（`./scripts/codex_install.sh || true`）では非0にならない
    （＝起動そのものは止めない設計）。

スクリプトは自分の場所（$0）から ROOT（tools/codex/ の置き場所）を解決するため、tmp_path に
codex_install.sh が動くのに要る最小限のファイルだけをコピーした「リポジトリもどき」を作って
実行する。実ワークツリーの tools/codex/（この worktree で導入済みの固定版）には一切触れない。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _make_repo_skeleton(tmp_path: Path) -> Path:
    """codex_install.sh の実行に要る最小限のファイルだけを tmp_path/repo へコピーする。"""
    repo = tmp_path / "repo"
    (repo / "scripts" / "lib").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "codex_install.sh", repo / "scripts" / "codex_install.sh")
    shutil.copy2(SCRIPTS / "run-common.sh", repo / "scripts" / "run-common.sh")
    shutil.copy2(SCRIPTS / "codex-version.env", repo / "scripts" / "codex-version.env")
    shutil.copy2(SCRIPTS / "lib" / "codex_pin.sh", repo / "scripts" / "lib" / "codex_pin.sh")
    (repo / "scripts" / "codex_install.sh").chmod(0o755)
    return repo


def _fake_path_codex(tmp_path: Path, version: str) -> Path:
    """PATH 上の既存 codex（npm 版など）を模す。開発機の本物の codex を試験に混ぜない。"""
    d = tmp_path / "pathbin"
    d.mkdir(exist_ok=True)
    f = d / "codex"
    f.write_text(f"#!/usr/bin/env bash\necho 'codex-cli {version}'\n", encoding="utf-8")
    f.chmod(0o755)
    return d


def _run(repo: Path, extra_env: dict[str, str], *, wrap_or_true: bool = False,
         path_codex_version: str = "0.100.0") -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{_fake_path_codex(repo.parent, path_codex_version)}:{env['PATH']}"
    env.update(extra_env)
    if wrap_or_true:
        cmd = ["bash", "-c", "./scripts/codex_install.sh || true"]
    else:
        cmd = ["bash", str(repo / "scripts" / "codex_install.sh")]
    return subprocess.run(cmd, cwd=str(repo), env=env, capture_output=True, text=True, timeout=60)


def test_sha256_mismatch_refuses_to_install(tmp_path):
    """偽の配布物（sha256 が固定値と一致しない）は fail-closed で導入しない。"""
    repo = _make_repo_skeleton(tmp_path)
    fixture_dir = tmp_path / "fixture" / "rust-v0.153.4"
    fixture_dir.mkdir(parents=True)
    # 内容は何でもよい（scripts/codex-version.env の固定 sha256 と一致しないことを試すだけ）。
    (fixture_dir / "codex-x86_64-unknown-linux-musl.tar.gz").write_bytes(b"not the real codex binary")

    result = _run(repo, {"CODEX_PIN_BASE_URL": f"file://{tmp_path / 'fixture'}"})

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "sha256" in out
    assert not (repo / "tools" / "codex" / "bin" / "codex").exists()


def test_unreachable_source_warns_in_japanese_without_installing(tmp_path):
    """取得元に届かない（file:// が存在しない＝閉域網を模す）場合は日本語で警告し、導入しない。"""
    repo = _make_repo_skeleton(tmp_path)

    result = _run(repo, {"CODEX_PIN_BASE_URL": f"file://{tmp_path / 'does-not-exist'}"})

    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "ネットワーク不可" in out or "取得できませんでした" in out
    assert not (repo / "tools" / "codex" / "bin" / "codex").exists()


def test_start_context_does_not_block_when_fetch_fails(tmp_path):
    """make start の文脈（`./scripts/codex_install.sh || true`）では、取得できなくても非0にならない
    （起動は止めない設計）。"""
    repo = _make_repo_skeleton(tmp_path)

    result = _run(
        repo,
        {"CODEX_PIN_BASE_URL": f"file://{tmp_path / 'does-not-exist'}"},
        wrap_or_true=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_pinned_version_already_on_path_skips_fetch(tmp_path):
    """PATH 上の codex が既に固定版なら取得しない（閉域で起動のたびに取得を待たない）。"""
    repo = _make_repo_skeleton(tmp_path)
    from_version = (SCRIPTS / "codex-version.env").read_text(encoding="utf-8")
    pin = from_version.split('CODEX_PIN_VERSION="', 1)[1].split('"', 1)[0]
    result = _run(repo, {"CODEX_PIN_BASE_URL": f"file://{tmp_path / 'does-not-exist'}"},
                  path_codex_version=pin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "取得しません" in result.stdout
    assert not (repo / "tools" / "codex").exists()

