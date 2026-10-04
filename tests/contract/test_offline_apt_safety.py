"""閉域導入の apt 経路の安全契約（2026-08-17・診断「apt 経路の再現/監査」に基づく）。"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "lib" / "apt_offline.sh"
INSTALL = ROOT / "scripts" / "install_offline_kit.sh"
MAKE = ROOT / "scripts" / "make_offline_kit.sh"


def _fake_apt(bin_dir: Path, sim_output: str, marker: Path) -> None:
    """PATH 上に置く偽 apt-get。-s なら sim_output を出し、-s 無しの install なら marker を作る。"""
    script = f"""#!/usr/bin/env bash
if printf '%s\\n' "$@" | grep -qx -- '-s'; then
  printf '%b\\n' {sim_output!r}
  exit 0
fi
case " $* " in
  *" update "*) exit 0 ;;
  *" install "*) echo REAL-INSTALL-RAN > {str(marker)!r}; exit 0 ;;
esac
exit 0
"""
    p = bin_dir / "apt-get"
    p.write_text(script, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def _run_lib(args: list[str], bin_dir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SHERPA_APT_OFFLINE_SUDO"] = ""  # sudo を挟まず PATH の偽 apt-get を叩かせる
    return subprocess.run(["bash", str(LIB), *args], env=env, capture_output=True, text=True, timeout=120)


def _make_kit(tmp_path: Path, with_index: bool) -> tuple[Path, Path]:
    kit = tmp_path / "kit"
    group = kit / "python" / "debs"
    group.mkdir(parents=True)
    (group / "dummy_1.0_amd64.deb").write_bytes(b"not a real deb")
    (group / "PACKAGES").write_text("python3 python3-venv\n", encoding="utf-8")
    if with_index:
        (kit / "Packages").write_text(
            "Package: dummy\nVersion: 1.0\nFilename: ./python/debs/dummy_1.0_amd64.deb\n\n",
            encoding="utf-8",
        )
    return kit, group


def test_install_side_never_calls_bare_apt_get_install():
    text = INSTALL.read_text(encoding="utf-8")
    lib = LIB.read_text(encoding="utf-8")
    for src, name in ((text, "install_offline_kit.sh"), (lib, "apt_offline.sh")):
        for line in src.splitlines():
            code = line.split("#", 1)[0]  # コメントは対象外（旧形を「廃止」と説明する行があるため）
            if code.lstrip().startswith(("fail ", "warn ", "note ", "echo ")):
                continue  # 案内文（復旧手順の表示）は実行行ではない
            if "apt-get" in code and re.search(r"\binstall\b", code) and "-s" not in code.split("install")[0]:
                # 本導入行は必ず --no-remove（共通配列 _APT_OFFLINE_OPTS 経由を含む）を伴う
                assert "--no-remove" in code or "_APT_OFFLINE_OPTS" in code or "apt_offline_install" in code, (
                    f"{name}: --no-remove の無い apt-get install: {line.strip()}"
                )
    assert "apt_offline_install" in text
    code_lines = [
        ln.split("#", 1)[0]
        for ln in lib.splitlines()
        if not ln.lstrip().startswith(("fail ", "warn ", "note ", "echo "))  # 案内文は除外
    ]
    assert "--no-remove" in lib
    assert not any("--allow-downgrades" in ln for ln in code_lines), "危険な --allow-downgrades が実行行にある"


@pytest.mark.parametrize("with_index", [True, False])
def test_lib_aborts_on_remv_before_real_install(tmp_path: Path, with_index: bool):
    """偽 apt-get が -s で『Remv linux-image-…』を返すと、本導入（-s 無し）へ進まず非0で止まる。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "REAL"
    _fake_apt(
        bin_dir,
        "Inst python3 (3.12.3 local)\nRemv linux-image-6.8.0-138-generic [6.8.0-138.138]\nConf python3",
        marker,
    )
    kit, group = _make_kit(tmp_path, with_index)
    r = _run_lib(["install", "Python 実行系", str(kit), str(group)], bin_dir)
    assert r.returncode == 2, r.stdout + r.stderr
    assert not marker.exists(), "Remv 検出後に本導入が実行された"
    out = r.stdout + r.stderr
    assert "Remv linux-image-6.8.0-138-generic" in out
    assert "削除" in out and "カーネル" in out


def test_lib_reports_missing_packages_and_stops(tmp_path: Path):
    """-s が unmet で失敗したら本導入へ進まず、不足名（Depends: X but …）を表示する。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "REAL"
    sim = (
        "The following packages have unmet dependencies:\n"
        " libc6-dev : Depends: libc6 (= 2.39-0ubuntu8.8) but 2.39-0ubuntu8 is to be installed\n"
        " libperl5.38t64 : Depends: libdb5.3t64 but it is not installable\n"
        "E: Unable to correct problems, you have held broken packages."
    )
    # -s のとき exit 100 を返す偽 apt-get
    p = bin_dir / "apt-get"
    p.write_text(
        "#!/usr/bin/env bash\n"
        "if printf '%s\\n' \"$@\" | grep -qx -- '-s'; then printf '%b\\n' " + repr(sim) + "; exit 100; fi\n"
        f"case \" $* \" in *' install '*) echo x > {str(marker)!r};; esac\nexit 0\n",
        encoding="utf-8",
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    kit, group = _make_kit(tmp_path, with_index=False)
    r = _run_lib(["install", "Python 実行系", str(kit), str(group)], bin_dir)
    assert r.returncode == 2
    assert not marker.exists()
    out = r.stdout + r.stderr
    assert "libc6 (= 2.39-0ubuntu8.8)" in out and "libdb5.3t64" in out


def test_lib_proceeds_when_simulation_is_clean(tmp_path: Path):
    """Remv 無しなら本導入（-s 無し・--no-remove 付き）へ進む。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "REAL"
    argslog = tmp_path / "ARGS"
    p = bin_dir / "apt-get"
    p.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {str(argslog)!r}\n"
        "if printf '%s\\n' \"$@\" | grep -qx -- '-s'; then echo 'Inst python3 (3.12.3 local)'; exit 0; fi\n"
        f"case \" $* \" in *' install '*) echo x > {str(marker)!r};; esac\nexit 0\n",
        encoding="utf-8",
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    kit, group = _make_kit(tmp_path, with_index=True)
    r = _run_lib(["install", "Python 実行系", str(kit), str(group)], bin_dir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert marker.exists()
    calls = argslog.read_text(encoding="utf-8").splitlines()
    installs = [c for c in calls if " install " in f" {c} "]
    assert all("--no-remove" in c for c in installs), calls
    assert any(" -s " in f" {c} " for c in installs) and any(" -s " not in f" {c} " for c in installs)
    # 索引付きキットは file: repo として名前指定（./*.deb 列挙ではない）
    assert any("python3 python3-venv" in c for c in installs), calls
    assert any("Dir::Etc::sourceparts=-" in c for c in calls), calls


def test_baseline_mismatch_stops_unless_relaxed(tmp_path: Path):
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "BASELINE").write_text("ID=ubuntu\nVERSION_ID=99.99\nVERSION_CODENAME=nowhere\nARCH=amd64\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    r = _run_lib(["baseline", str(kit)], bin_dir)
    assert r.returncode == 1 and "不一致" in r.stdout + r.stderr
    env = dict(os.environ, SHERPA_OFFLINE_ALLOW_BASELINE_MISMATCH="1", SHERPA_APT_OFFLINE_SUDO="")
    assert subprocess.run(["bash", str(LIB), "baseline", str(kit)], env=env, capture_output=True, text=True).returncode == 0


def _fake_apt_logging(bin_dir: Path, argslog: Path, sim_exit: int, sim_output: str, marker: Path) -> None:
    """引数と、-s 時に渡された sources.list の中身を記録する偽 apt-get。"""
    p = bin_dir / "apt-get"
    p.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {str(argslog)!r}\n"
        "for a in \"$@\"; do case \"$a\" in Dir::Etc::sourcelist=*) cat \"${a#Dir::Etc::sourcelist=}\" >> "
        f"{str(argslog)!r};; esac; done\n"
        "if printf '%s\\n' \"$@\" | grep -qx -- '-s'; then printf '%b\\n' " + repr(sim_output) + f"; exit {sim_exit}; fi\n"
        f"case \" $* \" in *' install '*) echo x > {str(marker)!r};; esac\nexit 0\n",
        encoding="utf-8",
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def test_real_apt_style_removal_refusal_is_explained_and_stops(tmp_path: Path):
    """実 apt は --no-remove 付き -s で削除が要ると Remv 行を出さず『remove is disabled』で非0終了する。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker, argslog = tmp_path / "REAL", tmp_path / "ARGS"
    sim = ("The following packages will be REMOVED:\n  linux-image-6.8.0-138-generic linux-image-virtual\n"
           "0 upgraded, 1 newly installed, 2 to remove and 0 not upgraded.\n"
           "E: Packages need to be removed but remove is disabled.")
    _fake_apt_logging(bin_dir, argslog, 100, sim, marker)
    kit, group = _make_kit(tmp_path, with_index=True)
    r = _run_lib(["install", "py", str(kit), str(group)], bin_dir)
    out = r.stdout + r.stderr
    assert r.returncode == 2 and not marker.exists(), out
    assert "削除を伴うため中止" in out and "カーネル" in out and "linux-image-6.8.0-138-generic" in out


def test_relative_and_spaced_kit_paths_become_absolute_encoded_file_uri(tmp_path: Path):
    """相対／空白入りの kit_root でも file: URI は絶対＋パーセントエンコードで書かれる。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker, argslog = tmp_path / "REAL", tmp_path / "ARGS"
    _fake_apt_logging(bin_dir, argslog, 0, "Inst python3 (3.12.3 local)", marker)
    kit = tmp_path / "キット 空白" / "kit"
    group = kit / "python" / "debs"
    group.mkdir(parents=True)
    (group / "dummy_1.0_amd64.deb").write_bytes(b"x")
    (group / "PACKAGES").write_text("python3\n", encoding="utf-8")
    (kit / "Packages").write_text("Package: dummy\nVersion: 1.0\nFilename: ./python/debs/dummy_1.0_amd64.deb\n\n", encoding="utf-8")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", SHERPA_APT_OFFLINE_SUDO="")
    r = subprocess.run(["bash", str(LIB), "install", "py", "キット 空白/kit", "キット 空白/kit/python/debs"],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    logged = argslog.read_text(encoding="utf-8")
    uri_lines = [ln for ln in logged.splitlines() if ln.startswith("deb ")]
    assert uri_lines and all(" file:/" in ln and "%20" in ln for ln in uri_lines), logged
    assert "Acquire::Check-Date=false" in logged   # 時計ずれで update が止まらない
    assert "LC_ALL=C.UTF-8" in LIB.read_text(encoding="utf-8")   # 出力を英語固定


def _fn(text: str, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.M | re.S)
    assert m, f"{name} が見つからない"
    return m.group(0)


def test_collector_propagates_docker_failure(tmp_path: Path):
    """docker run が失敗したら収集関数も失敗し、PACKAGES を書かない。"""
    src = MAKE.read_text(encoding="utf-8")
    fn = _fn(src, "_apt_collect_debs")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text("#!/usr/bin/env bash\necho 'fake docker: failing' >&2\nexit 125\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    helpers = "note(){ :; }; ok(){ :; }; warn(){ :; }; fail(){ :; }; reset_dir(){ rm -rf \"$1\"; mkdir -p \"$1\"; }\n"
    dest = tmp_path / "out" / "python" / "debs"
    script = (helpers + _fn(src, "_apt_write_packages") + "\n" + fn
              + f"\nAPT_BASE_IMAGE=ubuntu:24.04\n_apt_collect_debs 'python3' '{dest}'\necho rc=$?\n")
    r = subprocess.run(["bash", "-c", script], env=dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}"),
                       capture_output=True, text=True, timeout=60)
    assert "rc=125" in r.stdout, r.stdout + r.stderr
    assert not (dest / "PACKAGES").exists()


def test_base_closure_packages_rejects_shell_metacharacters_and_accepts_names(tmp_path: Path):
    """BASE_CLOSURE_PACKAGES は docker の bash -c 文字列に埋め込まれるため、メタ文字を含む値は拒否する。"""
    marker = tmp_path / "PWNED"
    env = dict(os.environ, BASE_CLOSURE_PACKAGES=f"ubuntu-server; touch {marker}")
    r = subprocess.run(["bash", str(MAKE)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "不正な値" in (r.stdout + r.stderr)
    assert not marker.exists(), "シェルのメタ文字が実行されてしまった"
    env_ok = dict(os.environ, BASE_CLOSURE_PACKAGES="ubuntu-minimal ubuntu-standard ubuntu-server")
    r2 = subprocess.run(["bash", str(MAKE)], cwd=ROOT, env=env_ok, capture_output=True, text=True, timeout=30)
    assert "不正な値" not in (r2.stdout + r2.stderr)


def test_collector_static_contracts():
    """収集側スクリプトの配線（静的固定）。"""
    text = MAKE.read_text(encoding="utf-8")
    inst = INSTALL.read_text(encoding="utf-8")
    assert "_apt_write_packages" in text and "write-baseline" in text and "apt-ftparchive packages" in text
    assert "--download-only --reinstall" in text and "--skip-base" in text
    assert "apt_offline_check_baseline" in inst   # 導入側の照合ステップ
    # 土台の閉包: 既定にメタを含み、収集失敗は致命（warn で握り潰さない）・docker 不在は host fallback
    defaults = re.search(r'^BASE_CLOSURE_PACKAGES="\$\{BASE_CLOSURE_PACKAGES:-([^}]+)\}"', text, re.M).group(1).split()
    assert {"ubuntu-minimal", "ubuntu-standard", "ubuntu-server"} <= set(defaults)
    block = re.search(r'echo "--- 12\. 土台の閉包.*?\nif \[ "\$FETCH" = 1 \] && \[ "\$SKIP_BASE" != 1 \]; then\n(.*?)\nelif \[ "\$SKIP_BASE" = 1 \]',
                      text, re.S).group(1)
    assert "exit 1" in block
    fb = _fn(text, "_apt_collect_base_closure_fallback")
    assert "sudo apt-get" in fb and "--reinstall" in fb and "--download-only" in fb
    main = _fn(text, "_apt_collect_base_closure")
    docker_missing = re.search(r"if ! command -v docker >/dev/null 2>&1; then(.*?)\n  fi", main, re.S).group(1)
    assert "_apt_collect_base_closure_fallback" in docker_missing and "return 1" in docker_missing
    # Python 版: 収集側と閉域側の ABI 照合は fail-closed（docker→apt-cache→BASELINE の順・特定不能なら逃げ道が無い限り停止）
    assert '_check_python_version_match "$HOST_PY_MM" || exit 1' in text
    check = _fn(text, "_check_python_version_match")
    assert 'fail "収集側 Python（' in check and "SHERPA_ALLOW_PY_MISMATCH" in check
    empty = re.search(r'if \[ -z "\$target_mm" \]; then(.*?)\n  fi', check, re.S).group(1)
    assert "return 0" in empty and "return 1" in empty and "fail " in empty.split("return 0", 1)[1]
    mm = _fn(text, "_target_python_mm")
    assert mm.find("command -v docker") < mm.find("$OUT/BASELINE")   # stale BASELINE を優先しない
    assert re.search(r'\[ -z "\$ver" \] && \[ -f "\$OUT/BASELINE" \]', mm)
    assert "COLLECTED-WITH-PYTHON-VERSION.txt" in _fn(text, "_pip_download_wheels")
    assert "_pip_download_wheels requirements-ocr.txt" in text and "_pip_download_wheels requirements.txt constraints.txt" in text


def test_kit_carries_codex_cli_and_installer_puts_it_on_path():
    make = MAKE.read_text(encoding="utf-8")
    inst = INSTALL.read_text(encoding="utf-8")
    common = (ROOT / "scripts" / "run-common.sh").read_text(encoding="utf-8")
    version_env = (ROOT / "scripts" / "codex-version.env").read_text(encoding="utf-8")
    assert "--skip-codex" in make and "codex_pin_fetch_asset" in make
    assert "CODEX_PIN_VERSION" in version_env and "CODEX_PIN_SHA256_linux_x86_64" in version_env
    assert "tools/codex/bin/codex" in inst and "codex login --with-api-key" in inst and "通信不要" in inst
    assert "codex_pin_extract_package" in inst and "tools/codex/bin" in common


VERIFY = ROOT / "scripts" / "verify_offline_kit_apt.sh"


def test_verify_kit_gate_static_contracts():
    """出荷ゲート（verify_offline_kit_apt.sh）: 未収集グループは既定で NG・ホスト像の再利用は来歴を照合・apt_offline.sh をそのまま使う。"""
    for f in (LIB, INSTALL, MAKE, VERIFY):
        subprocess.run(["bash", "-n", str(f)], check=True)
    text = VERIFY.read_text(encoding="utf-8")
    assert "--allow-missing-groups" in text and "ALLOW_MISSING_GROUPS=0" in text
    vg = re.search(r"^_verify_group\(\) \{.*?\n\}", text, re.M | re.S).group(0)
    assert "VERIFY_FAILED=1" in vg and "SHERPA_WFTEST_ALLOW_MISSING_GROUPS" in vg
    for group_dir in ("python/debs", "docker-engine/debs", "chromium/deps-debs", "libreoffice/debs", "fonts/noto-cjk-debs"):
        assert f'"{group_dir}"' in text
    fresh = re.search(r"^_host_image_fresh\(\) \{.*?\n\}", text, re.M | re.S).group(0)
    assert "_host_image_prereqs_ok" in fresh
    for label in ("sherpa.wftest.recipe_version", "sherpa.wftest.base_tag", "sherpa.wftest.snapshot"):
        assert label in text
    assert "sherpa.wftest.pkgset_sha256" in fresh and "sha256sum /root/PACKAGE-LIST.txt" in fresh   # 書くだけでなく比較する
    assert re.search(r"label_sha.*!=.*live_sha|live_sha.*!=.*label_sha", fresh)
    m = re.search(r"apt-get -qq -o Acquire::https::Verify-Peer=false install -y.*?ubuntu-minimal ubuntu-standard ubuntu-server linux-image-generic", text, re.S)
    assert m and "--no-install-recommends" not in m.group(0)   # 実機 Ubuntu Server と同じ導入済み集合
    assert "apt_offline_install" in text and "/mnt/apt_offline.sh" in text and "--network none" in text
    assert "docker がありません" in text and "sherpa-wftest-" in text and "sherpa-mvp" not in text
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "verify_offline_kit_apt.sh" in re.search(r"^verify-kit:.*\n(?:\t.*\n?)+", makefile, re.M).group(0)
    assert "verify-kit" in makefile.split(".PHONY:", 1)[1].split("\n\n", 1)[0]


def test_offline_install_records_requirements_hash_with_start_sh_formula(tmp_path: Path):
    """閉域の導入成功後に start.sh と同じ式のハッシュを書く（無いと start.sh が PyPI へ出て止まる）。"""
    import hashlib
    start = (ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")
    inst = INSTALL.read_text(encoding="utf-8")
    assert "lib/req_hash.sh" in start and "lib/req_hash.sh" in inst
    ok_pos = inst.index('ok "Python 依存を --no-index でインストールしました')
    assert ok_pos < inst.index(".requirements.sha256") and 'req_hash "$VENV/bin/python"' in inst[ok_pos:ok_pos + 600]
    (tmp_path / "requirements.txt").write_bytes(b"a\n")
    (tmp_path / "constraints.txt").write_bytes(b"b\n")
    out = subprocess.run(["bash", "-c", f'. "{ROOT}/scripts/lib/req_hash.sh"; req_hash python3'],
                         cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()
    assert out == hashlib.sha256(b"a\nb\n").hexdigest()


def test_kit_checks_tree_sitter_wheels_and_rejects_sdist(tmp_path: Path):
    """Tree-sitter 8 パッケージはホイールだけ（対象タグで全部揃い・sdist なし）。"""
    src = MAKE.read_text(encoding="utf-8")
    assert src.count('pip download --disable-pip-version-check --only-binary') + src.count('-m pip download --only-binary') == 2 and "--only-binary :all:" not in src
    pk = re.search(r'^TS_PACKAGES="(.*)"', src, re.M).group(1).split()
    assert len(pk) == 8
    body = _fn(src, "_check_ts_wheels")
    harness = ('fail(){ echo "$@" >&2; }; ok(){ :; }\n' + f'TS_PACKAGES="{" ".join(pk)}"\n' + body +
               '\n_check_ts_wheels "$1" "$2"')
    cons = tmp_path / "constraints.txt"
    cons.write_text("".join(f"{p}==1.0.0\n" for p in pk))
    d = tmp_path / "w"
    d.mkdir()
    (d / "COLLECTED-WITH-PYTHON-VERSION.txt").write_text("Python 3.12.3\n")
    for p in pk:
        n = p.replace("-", "_")
        tagpart = "cp312-cp312" if p == "tree-sitter" else "cp310-abi3"
        (d / f"{n}-1.0.0-{tagpart}-manylinux_2_28_x86_64.whl").write_text("")

    def run():
        return subprocess.run(["bash", "-c", harness, "x", str(d), str(cons)], capture_output=True, text=True).returncode
    assert run() == 0
    (d / "tree-sitter-css-1.0.0.tar.gz").write_text("")
    assert run() != 0
    (d / "tree-sitter-css-1.0.0.tar.gz").unlink()
    (d / "tree_sitter_c-1.0.0-cp310-abi3-manylinux_2_28_x86_64.whl").unlink()
    assert run() != 0
    (d / "tree_sitter_c-1.0.0-cp310-abi3-manylinux_2_28_x86_64.whl").write_text("")
    assert run() == 0
    (d / "tree_sitter_c-1.0.0-cp310-abi3-manylinux_2_28_x86_64.whl").rename(
        d / "tree_sitter_c-0.9.0-cp310-abi3-manylinux_2_28_x86_64.whl")
    assert run() != 0   # 固定版と違うホイール


def test_offline_install_stops_when_wheels_are_missing():
    """wheels が無いまま成功させない（ハッシュも書かれず次の start.sh が PyPI へ出るため）。"""
    inst = INSTALL.read_text(encoding="utf-8")
    tail = inst[inst.index('ok "Python 依存を --no-index でインストールしました'):]
    branch = tail[tail.index("\nelse\n"):tail.index("\nfi\n")]
    assert "wheel 一式が見つかりません" in branch and "exit 1" in branch and "warn" not in branch
