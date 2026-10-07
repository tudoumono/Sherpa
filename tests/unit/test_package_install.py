"""配布パッケージの作成と ./install.sh・起動の拒否（docs/proposals/2026-10-05-パッケージと導入・更新の一本化.md）。

外部の境界（apt・docker・ネットワークで準備する部分＝インストーラー本体）だけを小さな偽物に差し替え、
一時フォルダの中でパッケージを作る→展開する→インストールする流れを再現する。
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PKG_TOOL = ROOT / "scripts" / "lib" / "pkg_tool.py"
PLATFORM = "linux_x86_64"
CODEX_VERSION = "0.0.1"

# 実物のスクリプトのうち、パッケージの中に入れて動かすもの。
REAL_FILES = (
    "install.sh", "INSTALL.md", "scripts/start.sh", "scripts/run-api.sh", "scripts/stop.sh",
    "scripts/run-common.sh", "scripts/check-production.sh", "scripts/lib/install_state.sh",
    "scripts/lib/pkg_tool.py", "scripts/lib/req_hash.sh", "scripts/lib/codex_pin.sh",
    "scripts/lib/portable_tools.py", "scripts/node-version.env", "tools/marp/package-lock.json",
)


def _write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def make_source(tmp: Path, name: str, files: dict[str, str], requirements: str = "pkg-a\n") -> Path:
    """実物のスクリプトに、版ごとに違う小さなアプリのファイルを足した「アプリのツリー」を作る。"""
    src = tmp / name / "Sherpa"
    for rel in REAL_FILES:
        dest = src / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    _write(src / "scripts" / "codex-version.env", f'CODEX_PIN_VERSION="{CODEX_VERSION}"\n')
    _write(src / "requirements.txt", requirements)
    _write(src / "constraints.txt", "# pins\n")
    _write(src / ".env.example", "A=1\n")
    for rel, text in files.items():
        _write(src / rel, text)
    return src


CODEX_FILES = ("bin/codex", "bin/codex-code-mode-host", "codex-path/rg", "codex-resources/zsh/bin/zsh", "codex-resources/bwrap")
CODEX_SCRIPT = f'#!/bin/sh\necho "codex-cli {CODEX_VERSION}"\n'


def make_kit(tmp: Path, codex: bool = True) -> Path:
    kit = tmp / "kit"
    _write(kit / "wheels" / "COLLECTED-WITH-PYTHON-VERSION.txt", f"Python {sys.version_info[0]}.{sys.version_info[1]}.0\n")
    _write(kit / "wheels" / "dummy.whl", "w")
    _write(kit / "app" / "old-app.tar.gz", "stale")
    if codex:
        pkg_dir = tmp / "codexpkg"
        for rel in CODEX_FILES:
            _write(pkg_dir / rel, CODEX_SCRIPT)
        (kit / "codex").mkdir(exist_ok=True)
        subprocess.run(["tar", "czf", str(kit / "codex" / "codex-package-test.tar.gz"), "-C", str(pkg_dir), "."], check=True)
    return kit


def build(tmp: Path, src: Path, kit: Path, kind: str, version: str, platforms: str = PLATFORM) -> Path:
    out = tmp / f"out-{version}-{kind}"
    subprocess.run([sys.executable, str(PKG_TOOL), "build", "--kind", kind, "--source", str(src), "--kit", str(kit),
                    "--out-dir", str(out), "--version", version, "--commit", f"c{version.replace('.', '')}",
                    "--platforms", platforms], check=True, capture_output=True, text=True)
    return next(out.glob("*.tar.gz"))


def extract(archive: Path, work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "xzf", str(archive), "-C", str(work)], check=True)
    return work / "Sherpa"


FAKE_INSTALLER = f"""#!/usr/bin/env bash
set -e
cd "$FAKE_ROOT"
if [ -n "${{FAKE_INSTALL_FAIL:-}}" ]; then mkdir -p .venv/bin; echo partial > .venv/partial; exit 1; fi
mkdir -p .venv/bin
printf '#!/bin/sh\\nexec "{sys.executable}" "$@"\\n' > .venv/bin/python
chmod +x .venv/bin/python
echo "{{FAKE_VENV_TAG}}" > .venv/tag
c=tools/codex
mkdir -p $c/bin $c/codex-path $c/codex-resources/zsh/bin
for f in bin/codex bin/codex-code-mode-host codex-path/rg codex-resources/zsh/bin/zsh codex-resources/bwrap; do
  printf '#!/bin/sh\\necho "codex-cli {CODEX_VERSION}"\\n' > $c/$f; chmod +x $c/$f
done
"""


def run_install(root: Path, tmp: Path, *, fail: bool = False, tag: str = "v1", extra_env: dict | None = None):
    installer = tmp / "fake-installer.sh"
    _write(installer, FAKE_INSTALLER.replace("{FAKE_VENV_TAG}", tag), 0o755)
    env = {**os.environ, "SHERPA_INSTALL_PLATFORM": PLATFORM, "SHERPA_BACKUP_BEFORE_SWITCH": "0",
           "SHERPA_INSTALL_KIT_INSTALLER": str(installer), "FAKE_ROOT": str(root), "SHERPA_ENV_FILE": str(tmp / "empty.env")}
    (tmp / "empty.env").write_text("", encoding="utf-8")
    if fail:
        env["FAKE_INSTALL_FAIL"] = "1"
    env.update(extra_env or {})
    return subprocess.run(["bash", "install.sh"], cwd=root, env=env, capture_output=True, text=True, timeout=120)


def run_guard(root: Path, script: str = "scripts/run-api.sh"):
    env = {**os.environ, "SHERPA_ENV_FILE": str(root / "data" / "none.env")}
    Path(env["SHERPA_ENV_FILE"]).write_text("", encoding="utf-8")
    return subprocess.run(["bash", script, "serve"], cwd=root, env=env, capture_output=True, text=True, timeout=60)


def test_package_top_folder_and_metadata_first(tmp_path: Path):
    src = make_source(tmp_path, "src", {"app/a.txt": "a"})
    # 入れてはいけないもの（.env・data・.venv・tools の実行物）が源にあっても入らない。
    _write(src / ".env", "SECRET=1\n")
    _write(src / "data" / "x", "x")
    _write(src / ".venv" / "bin" / "python", "")
    _write(src / "tools" / "codex" / "bin" / "codex", "")
    kit = make_kit(tmp_path)
    full = build(tmp_path, src, kit, "full", "1.0.0")
    app = build(tmp_path, src, kit, "app", "1.0.0")

    with tarfile.open(full) as tf:
        names = tf.getnames()
    assert names[0] == "Sherpa/PACKAGE-INFO" and names[1] == "Sherpa/PACKAGE-MANIFEST.sha256"
    assert all(n.startswith("Sherpa/") for n in names)
    assert "Sherpa/dist/offline-kit/wheels/dummy.whl" in names
    assert not any("/app/old-app" in n for n in names)
    for forbidden in ("Sherpa/.env", "Sherpa/data/x", "Sherpa/.venv/bin/python", "Sherpa/tools/codex/bin/codex"):
        assert forbidden not in names
    with tarfile.open(app) as tf:
        app_names = tf.getnames()
    assert app_names[0] == "Sherpa/PACKAGE-INFO" and not any("offline-kit" in n for n in app_names)

    root = extract(full, tmp_path / "x")
    info = (root / "PACKAGE-INFO").read_text(encoding="utf-8")
    assert "kind=full" in info and f"fp.{PLATFORM}.requirements=" in info
    check = subprocess.run(["sha256sum", "-c", "PACKAGE-MANIFEST.sha256"], cwd=root, capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr
    sidecar = full.with_name(full.name + ".sha256").read_text(encoding="utf-8")
    assert sidecar.split()[1] == full.name
    assert stat.S_IMODE((root / "install.sh").stat().st_mode) & 0o111


def _installed_v1(tmp_path: Path) -> Path:
    """フルのパッケージ（版 1）を空のフォルダへ入れた状態。"""
    src = make_source(tmp_path, "v1", {"keep.txt": "k1", "old-same.txt": "s", "old-edited.txt": "e"})
    pkg = build(tmp_path, src, make_kit(tmp_path), "full", "1.0.0")
    root = extract(pkg, tmp_path / "work")
    r = run_install(root, tmp_path, tag="v1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (root / "data" / ".installing").exists()
    assert (root / "data" / ".installed").is_file()
    return root


def test_update_removes_old_files_only_when_hash_matches(tmp_path: Path):
    root = _installed_v1(tmp_path)
    _write(root / "old-edited.txt", "edited by user")
    _write(root / "mine.txt", "user file")
    _write(root / ".env", "KEEP=1\n")
    _write(root / "data" / "users" / "u1.txt", "data")
    src2 = make_source(tmp_path, "v2", {"keep.txt": "k2"})
    app = build(tmp_path, src2, make_kit(tmp_path), "app", "2.0.0")
    extract(app, tmp_path / "work")

    r = run_install(root, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (root / "old-same.txt").exists()                       # 前回の一覧にあり今回無く、ハッシュ一致 → 消える
    assert (root / "old-edited.txt").read_text() == "edited by user"  # 手が入っている → 残る
    assert "old-edited.txt" in r.stdout and "残しました" in r.stdout    # 最後に一覧で表示される
    assert (root / "mine.txt").exists() and (root / ".env").exists() and (root / "data" / "users" / "u1.txt").exists()
    assert not (root / "dist" / "offline-kit").exists()               # 古いキットも同じ規則で片付く
    assert (root / "keep.txt").read_text() == "k2"
    assert "version=2.0.0" in (root / "data" / ".installed").read_text()
    assert (root / ".venv" / "tag").read_text().strip() == "v1"       # アプリだけは依存に触れない


def test_app_package_with_changed_dependency_stops_without_touching_deps(tmp_path: Path):
    root = _installed_v1(tmp_path)
    src2 = make_source(tmp_path, "v2", {"keep.txt": "k2"}, requirements="pkg-a\npkg-b\n")
    app = build(tmp_path, src2, make_kit(tmp_path), "app", "2.0.0")
    extract(app, tmp_path / "work")

    r = run_install(root, tmp_path)
    assert r.returncode != 0 and "フルのパッケージ" in r.stderr
    assert (root / ".venv" / "tag").read_text().strip() == "v1" and (root / "tools" / "codex" / "bin" / "codex").exists()
    assert "version=1.0.0" in (root / "data" / ".installed").read_text()   # 記録は前回のまま
    assert (root / "data" / ".installing").exists()                          # 印を残したまま止まる
    r = run_guard(root)                                                      # 印がある間は systemd の入口（run-api.sh）も断る
    assert r.returncode == 78 and "インストールが途中" in r.stderr


def test_full_install_failure_restores_previous_venv(tmp_path: Path):
    root = _installed_v1(tmp_path)
    src2 = make_source(tmp_path, "v2", {"keep.txt": "k2"})
    extract(build(tmp_path, src2, make_kit(tmp_path), "full", "2.0.0"), tmp_path / "work")

    r = run_install(root, tmp_path, fail=True)
    assert r.returncode != 0
    assert (root / ".venv" / "tag").read_text().strip() == "v1" and not (root / ".venv" / "partial").exists()
    assert (root / "tools" / "codex" / "bin" / "codex").exists()
    assert "version=1.0.0" in (root / "data" / ".installed").read_text() and (root / "data" / ".installing").exists()


def test_install_stops_before_deps_for_app_first_install_and_unsupported_platform(tmp_path: Path):
    app = build(tmp_path, make_source(tmp_path, "a", {}), make_kit(tmp_path), "app", "1.0.0")
    root = extract(app, tmp_path / "work-a")
    r = run_install(root, tmp_path)
    assert r.returncode != 0 and "フルのパッケージが必要" in r.stderr and not (root / ".venv").exists()
    full = build(tmp_path, make_source(tmp_path, "b", {}), make_kit(tmp_path), "full", "1.0.0", platforms="darwin_arm64")
    root = extract(full, tmp_path / "work-b")
    r = run_install(root, tmp_path)
    assert r.returncode != 0 and "対応していません" in r.stderr and not (root / ".venv").exists()


def _guard_ok(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'ROOT="{root}"; . scripts/lib/install_state.sh; pkg_install_guard'],
                          cwd=root, capture_output=True, text=True)


def test_extracted_but_not_installed_is_refused_until_install_succeeds(tmp_path: Path):
    root = _installed_v1(tmp_path)
    assert _guard_ok(root).returncode == 0
    # 展開しただけ（./install.sh 未実行）: 版・コミットが記録と違う → systemd の入口（run-api.sh）も make start も断る
    src2 = make_source(tmp_path, "v2", {"keep.txt": "k2"})
    app2 = build(tmp_path, src2, make_kit(tmp_path), "app", "2.0.0")
    extract(app2, tmp_path / "work")
    r = run_guard(root)
    assert r.returncode == 78 and "まだインストールされていない" in r.stderr
    r = run_guard(root, "scripts/start.sh")
    assert r.returncode == 1 and "まだインストールされていない" in r.stderr
    assert run_install(root, tmp_path).returncode == 0 and _guard_ok(root).returncode == 0
    # 展開の途中で失敗しても（先頭のメタデータだけ書かれた状態でも）、成功するまで断られる
    app3 = build(tmp_path, make_source(tmp_path, "v3", {}), make_kit(tmp_path), "app", "3.0.0")
    subprocess.run(["tar", "xzf", str(app3), "-C", str(tmp_path / "work"), "Sherpa/PACKAGE-INFO"], check=True)
    assert "まだインストールされていない" in _guard_ok(root).stderr
    # 依存の記録（requirements）と今のファイルが違えば、インストール済みでも断る
    extract(app2, tmp_path / "work")
    assert run_install(root, tmp_path).returncode == 0
    _write(root / "requirements.txt", "pkg-a\npkg-b\n")
    assert "依存の記録" in _guard_ok(root).stderr


def test_previous_dependency_set_is_swapped_back_when_old_app_package_is_reinstalled(tmp_path: Path):
    root = _installed_v1(tmp_path)
    src1 = tmp_path / "v1" / "Sherpa"
    app1 = build(tmp_path, src1, make_kit(tmp_path), "app", "1.0.0")
    full2 = build(tmp_path, make_source(tmp_path, "v2", {}, requirements="pkg-a\npkg-b\n"), make_kit(tmp_path), "full", "2.0.0")
    extract(full2, tmp_path / "work")
    assert run_install(root, tmp_path, tag="v2").returncode == 0
    assert (root / ".venv" / "tag").read_text().strip() == "v2" and (root / ".venv.prev" / "tag").read_text().strip() == "v1"
    # 前の版（アプリだけ）を入れ直す: 今の .venv は合わないが、残してある前の組が合うので戻る
    extract(app1, tmp_path / "work")
    r = run_install(root, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / ".venv" / "tag").read_text().strip() == "v1" and "version=1.0.0" in (root / "data" / ".installed").read_text()


def _fake_bin(tmp_path: Path, scripts: dict[str, str]) -> Path:
    bindir = tmp_path / "fakebin"
    for name, body in scripts.items():
        _write(bindir / name, "#!/bin/sh\n" + body, 0o755)
    return bindir


def _app_root(tmp_path: Path, rels: tuple[str, ...]) -> Path:
    root = tmp_path / "app"
    for rel in rels:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, root / rel)
    return root


def test_start_runs_production_check_first_and_does_not_install_dependencies(tmp_path: Path):
    root = _app_root(tmp_path, ("scripts/start.sh", "scripts/run-common.sh", "scripts/lib/install_state.sh", "scripts/lib/req_hash.sh"))
    log = tmp_path / "calls.log"
    _write(root / "scripts" / "check-production.sh", f"#!/bin/sh\necho prod-check >> {log}\necho 'NG: fake' >&2\nexit 1\n", 0o755)
    _write(root / ".venv" / "bin" / "python", f'#!/bin/sh\necho "py $*" >> {log}\nexec "{sys.executable}" "$@"\n', 0o755)
    _write(root / "requirements.txt", "pkg-a\n")
    _write(root / "constraints.txt", "\n")
    _write(root / "data" / ".installed", "version=1\ncommit=c\n")
    _write(root / ".env", "SHERPA_ENV=production\n")
    bindir = _fake_bin(tmp_path, {"docker": f'echo "docker $*" >> {log}\nexit 0\n'})
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "SHERPA_ENV_FILE": str(root / ".env")}
    r = subprocess.run(["bash", "scripts/start.sh"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
    calls = log.read_text(encoding="utf-8")
    assert r.returncode == 1 and "本番の検査で問題が見つかった" in r.stderr
    assert "prod-check" in calls and "pip" not in calls and "compose up" not in calls   # 依存を入れず・ストアも起動しない


def test_stop_stops_active_systemd_unit_or_tells_how(tmp_path: Path):
    root = _app_root(tmp_path, ("scripts/stop.sh", "scripts/run-common.sh", "scripts/lib/install_state.sh"))
    log = tmp_path / "systemctl.log"
    ctl = ('case "$1" in\n'
           f'  list-units) echo "sherpa-api.service loaded active running Sherpa" ;;\n'
           f'  show) echo "$FAKE_WD" ;;\n'
           f'  stop) echo "stop $2" >> {log}; [ -z "$FAKE_STOP_FAIL" ] ;;\n'
           'esac\n')
    bindir = _fake_bin(tmp_path, {"systemctl": ctl, "sudo": "exit 1\n"})
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "KEEP_STORES": "1", "SHERPA_ENV_FILE": str(tmp_path / "e.env"),
           "FAKE_WD": str(root), "SHERPA_SYSTEMD_RUN_DIR": str(tmp_path)}
    (tmp_path / "e.env").write_text("", encoding="utf-8")
    r = subprocess.run(["bash", "scripts/stop.sh"], cwd=root, env={**env, "FAKE_STOP_FAIL": "1"}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "sudo systemctl stop sherpa-api.service" in r.stderr
    r = subprocess.run(["bash", "scripts/stop.sh"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "stop sherpa-api.service" in log.read_text(encoding="utf-8")
    # 動いたままの更新のインストールは先へ進まない
    pkg = build(tmp_path, make_source(tmp_path, "v1", {}), make_kit(tmp_path), "full", "1.0.0")
    inst = extract(pkg, tmp_path / "work")
    r = run_install(inst, tmp_path, extra_env={"PATH": env["PATH"], "FAKE_WD": str(inst), "SHERPA_SYSTEMD_RUN_DIR": str(tmp_path)})
    assert r.returncode != 0 and "systemd で動いているユニット" in r.stderr and not (inst / ".venv").exists()


def test_check_production_accepts_data_dir_inside_app_but_not_elsewhere_inside(tmp_path: Path):
    root = _app_root(tmp_path, ("scripts/check-production.sh", "scripts/run-common.sh", "scripts/lib/portable_tools.py"))
    _write(root / "scripts" / "check-ports.sh", "#!/bin/sh\nexit 0\n", 0o755)
    _write(root / "scripts" / "check_production_openai_probe.py", "print('NO_MARKER')\nprint('ENV_CANDIDATE_OK')\nprint('openai')\n")

    def run(users: Path) -> str:
        env_file = tmp_path / "env"
        env_file.write_text(f"SHERPA_ENV=production\nSHERPA_USERS_DIR={users}\nSHERPA_DERIVED_DIR={root}/data/derived\n", encoding="utf-8")
        env = {**os.environ, "SHERPA_ENV_FILE": str(env_file), "PYTHON_BIN": sys.executable}
        r = subprocess.run(["bash", "scripts/check-production.sh"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
        return r.stdout + r.stderr

    out = run(root / "data" / "users")
    assert f"SHERPA_USERS_DIR={root}/data/users (under the app's data/ directory)" in out
    out = run(root / "other")
    assert "outside its data/ directory" in out


def test_late_failure_restores_parked_sets_and_keeps_the_older_set(tmp_path: Path):
    root = _installed_v1(tmp_path)
    kit = make_kit(tmp_path)
    extract(build(tmp_path, make_source(tmp_path, "v2", {}), kit, "full", "2.0.0"), tmp_path / "work")
    assert run_install(root, tmp_path, tag="v2").returncode == 0
    extract(build(tmp_path, make_source(tmp_path, "v3", {}), kit, "full", "3.0.0"), tmp_path / "work")
    (root / "data" / ".installed.tmp").mkdir()          # 記録の書き込みを失敗させる（依存の準備のあと）
    r = run_install(root, tmp_path, tag="v3")
    assert r.returncode != 0
    assert (root / ".venv" / "tag").read_text().strip() == "v2" and (root / ".venv.prev" / "tag").read_text().strip() == "v1"
    assert (root / "data" / ".installing").exists()


def test_full_update_keeps_existing_tool_when_kit_has_no_material_for_it(tmp_path: Path):
    root = _installed_v1(tmp_path)
    _write(root / "tools" / "node" / "bin" / "node", '#!/bin/sh\necho v22.0.0\n', 0o755)
    extract(build(tmp_path, make_source(tmp_path, "v2", {}), make_kit(tmp_path), "full", "2.0.0"), tmp_path / "work")
    r = run_install(root, tmp_path, tag="v2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / "tools" / "node" / "bin" / "node").exists()
    assert "fp.node=v22.0.0" in (root / "data" / ".installed").read_text()


def test_full_package_requires_codex_in_kit_and_always_fingerprints_it(tmp_path: Path):
    src = make_source(tmp_path, "v1", {})
    r = subprocess.run([sys.executable, str(PKG_TOOL), "build", "--kind", "full", "--source", str(src),
                        "--kit", str(make_kit(tmp_path, codex=False)), "--out-dir", str(tmp_path / "o"),
                        "--version", "1.0.0", "--commit", "c"], capture_output=True, text=True)
    assert r.returncode != 0 and "Codex" in r.stderr and not (tmp_path / "o").exists()
    pkg = build(tmp_path, src, make_kit(tmp_path / "k2"), "full", "1.0.1")
    info = (extract(pkg, tmp_path / "x") / "PACKAGE-INFO").read_text()
    assert f"fp.{PLATFORM}.codex_version=" in info and f"fp.{PLATFORM}.codex_files_sha256=" in info


def test_systemd_not_running_is_not_an_error_but_a_failed_query_is(tmp_path: Path):
    bindir = _fake_bin(tmp_path, {"systemctl": 'echo "no permission" >&2\nexit 1\n'})
    path = {"PATH": f"{bindir}:{os.environ['PATH']}"}
    root = extract(build(tmp_path, make_source(tmp_path, "v1", {}), make_kit(tmp_path), "full", "1.0.0"), tmp_path / "work")
    r = run_install(root, tmp_path, extra_env={**path, "SHERPA_SYSTEMD_RUN_DIR": str(tmp_path / "none")})
    assert r.returncode == 0, r.stdout + r.stderr                       # systemd が動いていない → 対象なしで続ける
    (tmp_path / "sd").mkdir()
    r = run_install(root, tmp_path, extra_env={**path, "SHERPA_SYSTEMD_RUN_DIR": str(tmp_path / "sd")})
    assert r.returncode != 0 and "systemctl の照会に失敗" in r.stderr    # 本当の照会失敗は止まる


def test_package_version_file_carries_commit_once(tmp_path: Path):
    src = make_source(tmp_path, "src", {"VERSION": "1.0.0+old\n"})
    pkg = build(tmp_path, src, make_kit(tmp_path), "app", "1.0.0")
    assert pkg.name == "sherpa-1.0.0-app-c100.tar.gz"
    root = extract(pkg, tmp_path / "x")
    assert (root / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0+c100"
    assert "version=1.0.0\n" in (root / "PACKAGE-INFO").read_text(encoding="utf-8")
    check = subprocess.run(["sha256sum", "-c", "PACKAGE-MANIFEST.sha256"], cwd=root, capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr


def test_export_public_version_is_version_only(tmp_path: Path):
    dest = tmp_path / "public"
    (dest / ".git").mkdir(parents=True)
    r = subprocess.run(["bash", str(ROOT / "scripts" / "export_public.sh"), str(dest)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    base = (ROOT / "VERSION").read_text(encoding="utf-8").strip().split("+", 1)[0]
    assert (dest / "VERSION").read_text(encoding="utf-8").strip() == base
    assert (dest / "install.sh").is_file() and (dest / "INSTALL.md").is_file()
    assert not (dest / "scripts" / "publish_public_commit.sh").exists()


def test_publish_public_commit_records_content_commit_in_version(tmp_path: Path):
    repo = tmp_path / "pub"
    repo.mkdir()
    def git(*a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo,
                              capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-q")
    (repo / "VERSION").write_text("1.2.3\n")
    (repo / "a.txt").write_text("a")
    script = str(ROOT / "scripts" / "publish_public_commit.sh")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    r = subprocess.run(["bash", script, str(repo), "公開の更新", "abc123def"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert git("rev-list", "--count", "HEAD") == "2"
    content = git("rev-parse", "--short=9", "HEAD~1")
    assert (repo / "VERSION").read_text().strip() == f"1.2.3+{content}"
    assert "内部 abc123def から書き出し" in git("log", "-1", "--format=%B", "HEAD~1")
    r2 = subprocess.run(["bash", script, str(repo), "again", "abc123def"], capture_output=True, text=True, env=env)
    assert r2.returncode == 0 and git("rev-list", "--count", "HEAD") == "2"
    git("reset", "-q", "--hard", "HEAD~1")                       # 1 つ目だけの状態
    r3 = subprocess.run(["bash", script, str(repo), "again", "abc123def"], capture_output=True, text=True, env=env)
    assert r3.returncode == 0 and git("rev-list", "--count", "HEAD") == "2"
    assert (repo / "VERSION").read_text().strip() == f"1.2.3+{git('rev-parse', '--short=9', 'HEAD~1')}"


def test_package_commit_uses_public_commit_from_version(tmp_path: Path):
    (tmp_path / "VERSION").write_text("1.2.3+pub123456\n")
    out = subprocess.run(["make", "-pn", "-f", str(ROOT / "Makefile"), "-C", str(tmp_path), "package-app"], capture_output=True, text=True).stdout
    assert "PACKAGE_COMMIT := pub123456" in out


def test_macos_package_fingerprints_node_marp_and_brew_parts_without_versions(tmp_path: Path):
    pkg = build(tmp_path, make_source(tmp_path, "m", {}), make_kit(tmp_path), "full", "1.0.0", platforms="darwin_arm64")
    info = (extract(pkg, tmp_path / "x") / "PACKAGE-INFO").read_text()
    assert "fp.darwin_arm64.node=v22." in info and "fp.darwin_arm64.marp=4." in info
    assert "fp.darwin_arm64.libreoffice=libreoffice\n" in info
    assert "fp.darwin_arm64.fonts=font-hackgen,font-noto-sans-cjk-jp\n" in info


def _macos_parts_root(tmp_path: Path) -> tuple[Path, dict]:
    """install_macos_parts.sh を、偽の brew・curl・npm・python3 の境界だけ差し替えて走らせる場所。"""
    root = tmp_path / "Sherpa"
    for rel in ("scripts/install_macos_parts.sh", "scripts/run-common.sh", "scripts/node-version.env", "tools/marp/package.json"):
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    arch = "arm64" if platform.machine() in ("arm64", "aarch64") else "x64"
    ver = "22.22.3"
    top = tmp_path / "node-src" / f"node-v{ver}-darwin-{arch}"
    _write(top / "bin" / "node", "#!/bin/sh\necho v22.22.3\n", 0o755)
    dist = tmp_path / "dist" / f"v{ver}"
    dist.mkdir(parents=True)
    tarball = dist / f"node-v{ver}-darwin-{arch}.tar.gz"
    subprocess.run(["tar", "czf", str(tarball), "-C", str(tmp_path / "node-src"), top.name], check=True)
    digest = subprocess.run(["sha256sum", str(tarball)], check=True, capture_output=True, text=True).stdout.split()[0]
    (dist / "SHASUMS256.txt").write_text(f"{digest}  {tarball.name}\n", encoding="utf-8")
    log = tmp_path / "calls.log"
    fake = tmp_path / "fakebin"
    _write(fake / "brew", f'#!/bin/sh\necho "brew $*" >> "{log}"\n[ "$1 $2" = "list --cask" ] && [ "$3" = libreoffice ] && exit 0\n[ "$1 $2" = "list --cask" ] && exit 1\nexit 0\n', 0o755)
    _write(fake / "npm", f'#!/bin/sh\necho "npm $*" >> "{log}"\n', 0o755)
    _write(fake / "docker", "#!/bin/sh\nexit 0\n", 0o755)
    _write(fake / "pybin", f'#!/bin/sh\necho "python $*" >> "{log}"\nmkdir -p "$3/bin"\ncp "$0" "$3/bin/python"\n', 0o755)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "PYTHON_BIN": str(fake / "pybin"), "HOME": str(tmp_path / "home"),
           "SHERPA_NODE_DIST_BASE": (tmp_path / "dist").as_uri(), "SHERPA_ENV_FILE": str(tmp_path / "empty.env")}
    env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    (tmp_path / "empty.env").write_text("", encoding="utf-8")
    return root, env


def test_macos_parts_install_tools_side_and_system_side_parts(tmp_path: Path):
    root, env = _macos_parts_root(tmp_path)
    r = subprocess.run(["bash", str(root / "scripts" / "install_macos_parts.sh")], cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "calls.log").read_text()
    assert (root / "tools" / "node" / "bin" / "node").is_file()
    assert "npm --prefix" in calls and "ci" in calls
    assert "playwright install chromium" in calls
    assert "brew install --cask font-noto-sans-cjk-jp" in calls and "brew install --cask font-hackgen" in calls
    assert "brew install --cask libreoffice" not in calls


def test_macos_parts_stop_when_node_checksum_differs_or_brew_is_missing(tmp_path: Path):
    root, env = _macos_parts_root(tmp_path)
    shasums = next((tmp_path / "dist").glob("*/SHASUMS256.txt"))
    shasums.write_text("0" * 64 + "  other\n", encoding="utf-8")
    r = subprocess.run(["bash", str(root / "scripts" / "install_macos_parts.sh")], cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "sha256" in r.stderr and not (root / "tools" / "node").exists()
    (tmp_path / "fakebin" / "brew").unlink()
    env["PATH"] = f"{tmp_path / 'fakebin'}:/usr/bin:/bin"
    r = subprocess.run(["bash", str(root / "scripts" / "install_macos_parts.sh")], cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "Homebrew" in r.stderr


def test_macos_cask_fingerprint_reports_missing_part(tmp_path: Path, monkeypatch):
    sys.path.insert(0, str(PKG_TOOL.parent))
    import pkg_tool
    fake = tmp_path / "bin"
    _write(fake / "brew", "#!/bin/sh\necho libreoffice\necho font-hackgen\n", 0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    expected = {"libreoffice": "libreoffice", "fonts": "font-hackgen,font-noto-sans-cjk-jp"}
    got = pkg_tool.measure_expected(tmp_path, "darwin_arm64", False, expected)
    bad = pkg_tool.mismatches(expected, got, "導入後の環境")
    assert len(bad) == 1 and "font-noto-sans-cjk-jp" in bad[0]


def test_macos_install_stops_before_building_when_python_differs_from_package(tmp_path: Path):
    full = build(tmp_path, make_source(tmp_path, "p", {}), make_kit(tmp_path), "full", "1.0.0", platforms="darwin_arm64")
    root = extract(full, tmp_path / "work")
    fake = tmp_path / "py313"
    _write(fake, '#!/bin/sh\necho "3.13/cpython-313"\n', 0o755)
    r = run_install(root, tmp_path, extra_env={"SHERPA_INSTALL_PLATFORM": "darwin_arm64", "PYTHON_BIN": str(fake)})
    assert r.returncode != 0 and "3.12" in r.stderr and "PYTHON_BIN" in r.stderr
    assert not (root / ".venv").exists()
