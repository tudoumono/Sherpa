"""固定版 Codex CLI 導入（scripts/codex_install.sh・scripts/lib/codex_pin.sh）の契約テスト。"""
from __future__ import annotations

import os
import hashlib
import io
import shutil
import subprocess
import tarfile
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


def _fake_path_codex(tmp_path: Path, version: str, npm_like: bool = False) -> Path:
    """PATH 上の既存 codex を模す。"""
    d = tmp_path / "pathbin"
    d.mkdir(exist_ok=True)
    if not npm_like:
        f = d / "codex"
        f.write_text(f"#!/usr/bin/env bash\necho 'codex-cli {version}'\n", encoding="utf-8")
        f.chmod(0o755)
        return d
    # npm 版の配置: <pkg>/bin/codex.js と <pkg>/node_modules/@openai/codex-<os>/vendor/<triple>/ の一式。
    pkg = tmp_path / "npm-codex"
    js = pkg / "bin" / "codex.js"
    js.parent.mkdir(parents=True, exist_ok=True)
    js.write_text(f"#!/usr/bin/env bash\necho 'codex-cli {version}'\n", encoding="utf-8")
    js.chmod(0o755)
    vendor = pkg / "node_modules" / "@openai" / "codex-linux-x64" / "vendor" / "x86_64-unknown-linux-musl"
    for rel in ("bin/codex", "bin/codex-code-mode-host", "codex-path/rg", "codex-resources/zsh/bin/zsh",
                "codex-resources/bwrap"):
        q = vendor / rel
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text("#!/bin/sh\n", encoding="utf-8")
        q.chmod(0o755)
    (d / "codex").symlink_to(js)
    return d


def _run(repo: Path, extra_env: dict[str, str], *, wrap_or_true: bool = False,
         path_codex_version: str = "0.100.0", path_codex_npm_like: bool = False) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{_fake_path_codex(repo.parent, path_codex_version, path_codex_npm_like)}:{env['PATH']}"
    env.update(extra_env)
    if wrap_or_true:
        cmd = ["bash", "-c", "./scripts/codex_install.sh || true"]
    else:
        cmd = ["bash", str(repo / "scripts" / "codex_install.sh")]
    return subprocess.run(cmd, cwd=str(repo), env=env, capture_output=True, text=True, timeout=60)


def test_sha256_mismatch_and_unreachable_source_refuse_to_install(tmp_path):
    """偽の配布物（sha256 不一致）や取得元に届かない場合は fail-closed で導入しない。"""
    repo = _make_repo_skeleton(tmp_path)
    fixture_dir = tmp_path / "fixture" / "rust-v0.153.4"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "codex-package-x86_64-unknown-linux-musl.tar.gz").write_bytes(b"not the real codex binary")
    result = _run(repo, {"CODEX_PIN_BASE_URL": f"file://{tmp_path / 'fixture'}"})
    out = result.stdout + result.stderr
    assert result.returncode != 0 and "sha256" in out, out
    assert not (repo / "tools" / "codex" / "bin" / "codex").exists()

    gone = {"CODEX_PIN_BASE_URL": f"file://{tmp_path / 'does-not-exist'}"}
    result = _run(repo, gone)
    out = result.stdout + result.stderr
    assert result.returncode != 0 and ("ネットワーク不可" in out or "取得できませんでした" in out), out
    assert not (repo / "tools" / "codex" / "bin" / "codex").exists()
    # make start の文脈（`./scripts/codex_install.sh || true`）では取得できなくても起動を止めない
    assert _run(repo, gone, wrap_or_true=True).returncode == 0


def test_pinned_version_already_on_path_skips_fetch(tmp_path):
    """PATH 上の codex が固定版の npm 版（付属物つき）なら取得しない（閉域で起動のたびに取得を待たない）。"""
    repo = _make_repo_skeleton(tmp_path)
    from_version = (SCRIPTS / "codex-version.env").read_text(encoding="utf-8")
    pin = from_version.split('CODEX_PIN_VERSION="', 1)[1].split('"', 1)[0]
    result = _run(repo, {"CODEX_PIN_BASE_URL": f"file://{tmp_path / 'does-not-exist'}"},
                  path_codex_version=pin, path_codex_npm_like=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "取得しません" in result.stdout
    assert not (repo / "tools" / "codex").exists()


def _pin_version() -> str:
    env = (SCRIPTS / "codex-version.env").read_text(encoding="utf-8")
    return env.split('CODEX_PIN_VERSION="', 1)[1].split('"', 1)[0]


def _host_key() -> str:
    out = subprocess.run(
        ["bash", "-c", f". {SCRIPTS / 'lib' / 'codex_pin.sh'}; codex_pin_platform_key"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not out:
        pytest.skip("この OS/CPU は固定版 Codex の自動導入の対象外")
    return out


def _script(version: str) -> bytes:
    return f"#!/bin/sh\necho 'codex-cli {version}'\n".encode()


def _build_package(path: Path, key: str, *, extra=None) -> str:
    """小さな偽 package（本物と同じ構成）を作り sha256 を返す。extra=[(TarInfo, bytes|None)] を足す。"""
    members = {"bin/codex": _script(_pin_version()), "codex-path/rg": b"#!/bin/sh\n",
               "bin/codex-code-mode-host": b"#!/bin/sh\n", "codex-resources/zsh/bin/zsh": b"#!/bin/sh\n",
               "codex-package.json": b"{}"}
    if key.startswith("linux_"):
        members["codex-resources/bwrap"] = b"#!/bin/sh\n"
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = 0o755
            tf.addfile(ti, io.BytesIO(data))
        for ti, data in extra or []:
            tf.addfile(ti, io.BytesIO(data) if data is not None else None)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup_fixture(tmp_path: Path, extra=None) -> tuple[Path, dict[str, str]]:
    key = _host_key()
    repo = _make_repo_skeleton(tmp_path)
    asset = "codex-package-test.tar.gz"
    fixture_dir = tmp_path / "fixture" / "rust-v0.153.4"
    fixture_dir.mkdir(parents=True)
    sha = _build_package(fixture_dir / asset, key, extra=extra)
    env_file = repo / "scripts" / "codex-version.env"
    env_file.write_text(
        env_file.read_text(encoding="utf-8")
        + f'\nCODEX_PIN_ASSET_{key}="{asset}"\nCODEX_PIN_SHA256_{key}="{sha}"\n',
        encoding="utf-8")
    return repo, {"CODEX_PIN_BASE_URL": f"file://{tmp_path / 'fixture'}"}


def test_package_with_link_or_escape_path_is_not_installed(tmp_path):
    """リンク・脱出パスを含む package は入れない。既存の導入先は壊さない。"""
    link = tarfile.TarInfo("codex-path/evil")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    for extra in ([(link, None)], [(_escape_member(), b"x")]):
        sub = tmp_path / f"case{len(extra[0][0].name)}"
        sub.mkdir()
        repo, env = _setup_fixture(sub, extra=extra)
        keep = repo / "tools" / "codex" / "bin"
        keep.mkdir(parents=True)
        (keep / "codex").write_bytes(_script("0.100.0"))
        (keep / "codex").chmod(0o755)

        result = _run(repo, env)

        assert result.returncode != 0, result.stdout + result.stderr
        assert (keep / "codex").read_bytes() == _script("0.100.0")
        assert not (repo / "tools" / "codex" / "codex-path").exists()


def _escape_member() -> tarfile.TarInfo:
    ti = tarfile.TarInfo("../escape.txt")
    ti.size = 1
    return ti


def test_bin_only_layout_is_reinstalled_as_full_package(tmp_path):
    """本体だけの既存配置（0.14.28〜31）は固定版でも入れ直し、古い残りを残さず付属物をそろえる。"""
    repo, env = _setup_fixture(tmp_path)
    old = repo / "tools" / "codex"
    (old / "bin").mkdir(parents=True)
    (old / "bin" / "codex").write_bytes(_script(_pin_version()))
    (old / "bin" / "codex").chmod(0o755)
    (old / "stale.txt").write_text("old", encoding="utf-8")

    check = _run(repo, env, path_codex_version="0.100.0")
    assert check.returncode == 0, check.stdout + check.stderr
    assert (old / "codex-path" / "rg").exists()
    assert not (old / "stale.txt").exists()
    again = _run(repo, {"CODEX_PIN_BASE_URL": f"file://{tmp_path / 'does-not-exist'}"})
    assert again.returncode == 0 and "付属物あり" in again.stdout, again.stdout + again.stderr
