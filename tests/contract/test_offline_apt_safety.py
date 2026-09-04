"""閉域導入の apt 経路の安全契約（2026-08-17・診断「apt 経路の再現/監査」に基づく）。

契約:
- 導入側の apt-get install は必ず --no-remove を伴う（bare な `apt-get install -y` を残さない）。
- scripts/lib/apt_offline.sh は -s（シミュレーション）を先に走らせ、Remv（削除提案）を検出したら
  **本導入に進まず**非0で止まる。grep だけでなく PATH 上の偽 apt-get で実際に実行して確かめる。
- 収集側は PACKAGES / BASELINE / apt-ftparchive 索引を生成するコードを持つ。
外部サービス不要（docker も不要）。
"""

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
    assert r.returncode == 1
    assert "不一致" in r.stdout + r.stderr
    env = dict(os.environ, SHERPA_OFFLINE_ALLOW_BASELINE_MISMATCH="1", SHERPA_APT_OFFLINE_SUDO="")
    r2 = subprocess.run(["bash", str(LIB), "baseline", str(kit)], env=env, capture_output=True, text=True)
    assert r2.returncode == 0


def test_collector_writes_packages_baseline_and_index():
    text = MAKE.read_text(encoding="utf-8")
    assert "_apt_write_packages" in text and '/PACKAGES"' in text
    assert "write-baseline" in text and "/BASELINE" in text
    assert "docker image inspect" in text  # digest を記録
    assert "apt-ftparchive packages" in text and "apt-ftparchive" in text and "release" in text
    assert "--download-only --reinstall" in text  # 土台の閉包
    assert "--skip-base" in text
    # 導入側の照合ステップ
    assert "apt_offline_check_baseline" in INSTALL.read_text(encoding="utf-8")


def test_lib_and_scripts_parse():
    for f in (LIB, INSTALL, MAKE):
        subprocess.run(["bash", "-n", str(f)], check=True)


# ---- 2026-08-17 敵対的レビューで実測された穴の固定 ---------------------------------------------


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
    """実 apt は --no-remove 付き -s で削除が要ると Remv 行を出さず『will be REMOVED … remove is disabled』で
    非0終了する（実測）。この形でも「削除を伴うため中止」と説明し、カーネル含有を警告し、本導入へ進まない。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker, argslog = tmp_path / "REAL", tmp_path / "ARGS"
    sim = (
        "The following packages will be REMOVED:\n"
        "  linux-image-6.8.0-138-generic linux-image-virtual\n"
        "0 upgraded, 1 newly installed, 2 to remove and 0 not upgraded.\n"
        "E: Packages need to be removed but remove is disabled."
    )
    _fake_apt_logging(bin_dir, argslog, 100, sim, marker)
    kit, group = _make_kit(tmp_path, with_index=True)
    r = _run_lib(["install", "py", str(kit), str(group)], bin_dir)
    out = r.stdout + r.stderr
    assert r.returncode == 2 and not marker.exists(), out
    assert "削除を伴うため中止" in out and "カーネル" in out, out
    assert "linux-image-6.8.0-138-generic" in out


def test_relative_and_spaced_kit_paths_become_absolute_encoded_file_uri(tmp_path: Path):
    """相対パス／空白入りパスの kit_root でも file: URI は絶対＋パーセントエンコードで書かれる（実測: 相対だと
    'Invalid URI, local URIS must not start with //'、空白は '/dists/…' 誤解釈で update が失敗した）。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker, argslog = tmp_path / "REAL", tmp_path / "ARGS"
    _fake_apt_logging(bin_dir, argslog, 0, "Inst python3 (3.12.3 local)", marker)
    kit_parent = tmp_path / "キット 空白"
    kit_parent.mkdir()
    kit = kit_parent / "kit"
    group = kit / "python" / "debs"
    group.mkdir(parents=True)
    (group / "dummy_1.0_amd64.deb").write_bytes(b"x")
    (group / "PACKAGES").write_text("python3\n", encoding="utf-8")
    (kit / "Packages").write_text("Package: dummy\nVersion: 1.0\nFilename: ./python/debs/dummy_1.0_amd64.deb\n\n", encoding="utf-8")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", SHERPA_APT_OFFLINE_SUDO="")
    r = subprocess.run(
        ["bash", str(LIB), "install", "py", "キット 空白/kit", "キット 空白/kit/python/debs"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    logged = argslog.read_text(encoding="utf-8")
    uri_lines = [ln for ln in logged.splitlines() if ln.startswith("deb ")]
    assert uri_lines, logged
    assert all(" file:/" in ln for ln in uri_lines), uri_lines          # 絶対パス
    assert all("%20" in ln and "キット 空白" not in ln.split("file:")[1].split(" ")[0] for ln in uri_lines), uri_lines
    assert "Acquire::Check-Date=false" in logged                         # 時計ずれで update が止まらない
    assert "LC_ALL=C.UTF-8" in LIB.read_text(encoding="utf-8")            # 出力を英語固定（抽出が空振りしない）


def test_collector_propagates_docker_failure(tmp_path: Path):
    """収集側: docker run が失敗したら関数も失敗し、PACKAGES を書かない（RV HIGH: 握り潰して OK と出ていた）。"""
    src = MAKE.read_text(encoding="utf-8")
    m = re.search(r"^_apt_collect_debs\(\) \{.*?^\}", src, re.M | re.S)
    assert m, "_apt_collect_debs が見つからない"
    fn = m.group(0)
    assert '" || return $?' in fn, "docker run の失敗が呼び出し元へ返らない"
    # 実行でも確かめる: 偽 docker（exit 125）で関数を動かし、非0＋PACKAGES 無しを確認
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text("#!/usr/bin/env bash\necho 'fake docker: failing' >&2\nexit 125\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    helpers = "note(){ :; }; ok(){ :; }; warn(){ :; }; fail(){ :; }; reset_dir(){ rm -rf \"$1\"; mkdir -p \"$1\"; }\n"
    write_pk = re.search(r"^_apt_write_packages\(\) \{.*?^\}", src, re.M | re.S).group(0)
    dest = tmp_path / "out" / "python" / "debs"
    script = helpers + write_pk + "\n" + fn + f"\nAPT_BASE_IMAGE=ubuntu:24.04\n_apt_collect_debs 'python3' '{dest}'\necho rc=$?\n"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert "rc=125" in r.stdout, r.stdout + r.stderr
    assert not (dest / "PACKAGES").exists(), "docker 失敗後に PACKAGES が書かれている"


def test_kit_carries_codex_cli_and_installer_puts_it_on_path():
    """Codex(OpenAI) 構成は「OpenAI へだけ穴あけ」の閉域で使う前提＝Codex CLI はキットが運ぶ。
    収集: npm 導入済みツリーを tar（--skip-codex で除外可）／導入: tools/codex/bin/codex に静的バイナリを
    リンク／run-common が PATH に載せる（sherpa は shutil.which("codex") で探す）。"""
    make = MAKE.read_text(encoding="utf-8")
    inst = INSTALL.read_text(encoding="utf-8")
    common = (ROOT / "scripts" / "run-common.sh").read_text(encoding="utf-8")
    assert "--skip-codex" in make and "openai-codex-" in make and "@openai/codex" in make
    assert 'tools/codex/bin/codex' in inst and "codex login --with-api-key" in inst
    assert 'tools/codex/bin' in common
    # 認証案内は「通信不要」であることを明記（実測 2026-08-18: unshare -n 下で Successfully logged in）
    assert "通信不要" in inst


# ---- 2026-08-18 閉域実機報告①②③の是正固定（VERIFY-KIT-001） -------------------------------------
# 実機で make_offline_kit.sh --fetch → 閉域相当ホストへ導入 → 削除提案で fail-close、という具体的な
# 事象が3件（①土台の閉包漏れ／②Python版の未照合／③出荷ゲート不在）報告された。docker を使う実導入検証は
# scripts/verify_offline_kit_apt.sh 自身（実機・手動）で行い、ここでは静的に「直っている」ことだけを固定する。


# ---- 2026-08-18 敵対的レビュー第2巡（出荷ゲート4件）の是正固定 -----------------------------------
# VERIFY-KIT-001 で作った出荷ゲート自体への RV で4件（HIGH: 未収集グループを無条件に通す／MED: ホスト像
# 再利用が退行を隠す／MED: base 閉包の収集失敗が非致命／LOW: Python 版照合が stale BASELINE で false pass）
# が指摘され、是正した。docker を使う実導入・実ホスト像構築の検証は手元で実行して確認済み（作業報告に記録）。
# ここでは静的固定に加え、docker 不要で実行できる範囲（メタ文字拒否・関数の実失敗）は実コードを実行して確かめる。

VERIFY = ROOT / "scripts" / "verify_offline_kit_apt.sh"


def test_verify_kit_required_groups_default_ng_with_escape_hatch():
    """報告 RV-HIGH-1: 検証対象の5グループ（Python実行系/Docker Engine/Chromiumシステム依存/LibreOffice/
    Noto Sans CJK JP）が未収集でも従来は黙って [未収集] 表示のまま rc=0 で通っていた（--skip-* で作った
    部分キット・収集途中で失敗したキットが出荷ゲートを素通りする）。既定では未収集を NG にし、
    --allow-missing-groups を付けたときだけ従来どおり許容することを固定する（実際の apt 導入結果は
    docker 実行で確認済み・作業報告参照）。"""
    text = VERIFY.read_text(encoding="utf-8")
    assert "--allow-missing-groups" in text
    assert "ALLOW_MISSING_GROUPS=1" in text
    assert "SHERPA_WFTEST_ALLOW_MISSING_GROUPS" in text
    fn = re.search(r"^_verify_group\(\) \{.*?\n\}", text, re.M | re.S)
    assert fn, "_verify_group が見つからない"
    fn_text = fn.group(0)
    # 未収集の分岐で、許容フラグが立っていなければ VERIFY_FAILED=1（NG）になること
    assert "VERIFY_FAILED=1" in fn_text
    assert "SHERPA_WFTEST_ALLOW_MISSING_GROUPS" in fn_text
    # 検証対象5グループの呼び出しが全部残っていること（一部だけ必須化されていない）
    for group_dir in ("python/debs", "docker-engine/debs", "chromium/deps-debs", "libreoffice/debs", "fonts/noto-cjk-debs"):
        assert f'"{group_dir}"' in text, f"{group_dir} の _verify_group 呼び出しが見つからない"
    # 既定値（フラグ未指定）は 0（必須＝NG 側）
    assert 'ALLOW_MISSING_GROUPS=0' in text


def test_verify_kit_and_apt_offline_scripts_parse():
    subprocess.run(["bash", "-n", str(VERIFY)], check=True)


def test_host_image_reuse_checks_provenance_labels_and_prereqs():
    """報告 RV-MED-2: ホスト像を『存在すれば中身を見ずに再利用』すると、ローカルに古い／別内容の同名
    イメージがあってもゲートは別物のホストで通ってしまう。来歴（レシピ版・ベースタグ・snapshot）を
    docker label に持たせ、起動前に照合して不一致なら作り直す。実機 Ubuntu Server の導入済み集合の
    代表パッケージ（ubuntu-server 系メタが要求していた上げ先）が実際に入っていることも明示的に確認する。
    （実行しての確認: 来歴 label の無い旧世代の既存イメージに対して実際に作り直しが走ることを docker で
    確認済み・作業報告参照）"""
    text = VERIFY.read_text(encoding="utf-8")
    for pkg in ("libglib2.0-bin", "packagekit", "software-properties-common", "ubuntu-server", "linux-image-generic"):
        assert pkg in text, f"前提パッケージ {pkg} の検査が無い"
    assert "HOST_PREREQ_PACKAGES" in text
    for label in ("sherpa.wftest.recipe_version", "sherpa.wftest.base_tag", "sherpa.wftest.snapshot"):
        assert label in text, f"来歴 label {label} が無い"
    assert "docker commit" in text and "--change" in text
    fresh_fn = re.search(r"^_host_image_fresh\(\) \{.*?\n\}", text, re.M | re.S)
    assert fresh_fn, "_host_image_fresh が見つからない"
    prereq_fn = re.search(r"^_host_image_prereqs_ok\(\) \{.*?\n\}", text, re.M | re.S)
    assert prereq_fn, "_host_image_prereqs_ok が見つからない"
    assert "_host_image_prereqs_ok" in fresh_fn.group(0), "来歴一致の判定に前提パッケージ確認が含まれていない"
    # 再利用の分岐は _host_image_fresh を経由し、不一致なら作り直す（_build_host_image を呼ぶ）
    reuse_block = text.split('if [ "${SHERPA_WFTEST_REBUILD_HOST:-0}" = 1 ]; then', 1)[1]
    assert "_host_image_fresh" in reuse_block and "_build_host_image" in reuse_block


def test_host_image_fresh_actually_compares_pkgset_sha256_label():
    """2026-08-18 Codex RV 2巡目 指摘6: `_build_host_image` は導入パッケージ集合の sha256
    （`sherpa.wftest.pkgset_sha256`）を label に書いているのに、再利用可否の判定
    （`_host_image_fresh`）はレシピ版・ベースタグ・snapshot の label しか見ておらず、この label を
    一度も比較していなかった（書くだけで検証に使わない見せかけの照合）。像の中身
    （`/root/PACKAGE-LIST.txt`）を読み直したハッシュと label の値を突き合わせることを固定する
    （実行しての確認: 実際に label を偽の値へ書き換えた像に対して不一致検出→作り直しが走ることを
    docker で確認済み・作業報告参照）。"""
    text = VERIFY.read_text(encoding="utf-8")
    assert "sherpa.wftest.pkgset_sha256" in text
    fresh_fn = re.search(r"^_host_image_fresh\(\) \{.*?\n\}", text, re.M | re.S)
    assert fresh_fn, "_host_image_fresh が見つからない"
    fn_text = fresh_fn.group(0)
    assert 'sherpa.wftest.pkgset_sha256' in fn_text, (
        "_host_image_fresh が pkgset_sha256 label を一度も読んでいない（書くだけで比較しない見せかけの照合）")
    assert "sha256sum /root/PACKAGE-LIST.txt" in fn_text, (
        "像の中身を読み直してハッシュを取り直していない")
    assert re.search(r'label_sha.*!=.*live_sha|live_sha.*!=.*label_sha', fn_text), (
        "label のハッシュと像の中身のハッシュを比較していない")


def test_host_image_build_uses_recommends_not_no_install_recommends():
    """報告 RV-MED-2: ホスト像を --no-install-recommends で作ると、実機 Ubuntu Server の標準導入
    （apt 既定 Install-Recommends=yes）より導入済み集合を過小評価しうる。判断: この検証の目的
    （搬入先の導入済み集合の近似）に照らして忠実性を優先し、recommends 込みで導入する
    （像は肥大するが実測で前提パッケージが揃うことを確認済み・作業報告参照）。"""
    text = VERIFY.read_text(encoding="utf-8")
    m = re.search(
        r"apt-get -qq -o Acquire::https::Verify-Peer=false install -y.*?ubuntu-minimal ubuntu-standard ubuntu-server linux-image-generic",
        text, re.S,
    )
    assert m, "ubuntu-minimal/standard/server 導入行が見つからない"
    assert "--no-install-recommends" not in m.group(0)


def test_base_closure_collection_failure_is_fatal():
    """報告 RV-MED-3: base 閉包の収集失敗を warn で握り潰すと『直したつもりの壊れたキット』が完成する
    （今回の実機不具合そのものが base 閉包の欠落）。--skip-base による明示省略を除き、収集失敗は
    キット生成を止める（非0終了）。"""
    text = MAKE.read_text(encoding="utf-8")
    m = re.search(
        r'echo "--- 12\. 土台の閉包.*?\nif \[ "\$FETCH" = 1 \] && \[ "\$SKIP_BASE" != 1 \]; then\n(.*?)\nelif \[ "\$SKIP_BASE" = 1 \]',
        text, re.S,
    )
    assert m, "土台の閉包の呼び出しブロックが見つからない"
    block = m.group(1)
    assert "exit 1" in block, "base 閉包の収集失敗時に exit 1 していない（warn で握り潰している疑い）"
    else_part = block.split("else", 1)[1] if "else" in block else ""
    # コメント行は対象外（「従来は warn で続行していた」という来歴コメント自体に warn の語が出るため）
    else_code_lines = [ln.split("#", 1)[0] for ln in else_part.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("warn " in ln for ln in else_code_lines), "収集失敗時になお warn で継続している"
    assert any("fail " in ln for ln in else_code_lines)


def test_base_closure_packages_rejects_shell_metacharacters_and_accepts_names(tmp_path: Path):
    """報告 RV-MED-3: BASE_CLOSURE_PACKAGES は docker の bash -c 文字列にそのまま埋め込まれるため
    （収集側は外側 bash が展開してからコンテナへ渡す）、シェルのメタ文字を含む値は事前に拒否する。
    実行して確かめる: 実スクリプトを『メタ文字入りの値』で起動すると make dist 等の重い処理へ進む前に
    即座に拒否され、埋め込みが実行されないこと（外部に痕跡ファイルを作らせて確認）。"""
    text = MAKE.read_text(encoding="utf-8")
    fn = re.search(r"^_validate_base_closure_packages\(\) \{.*?^\}", text, re.M | re.S)
    assert fn, "_validate_base_closure_packages が見つからない"
    assert re.search(r'_validate_base_closure_packages "\$BASE_CLOSURE_PACKAGES" \|\| exit 1', text)

    marker = tmp_path / "PWNED"
    env = dict(os.environ, BASE_CLOSURE_PACKAGES=f"ubuntu-server; touch {marker}")
    r = subprocess.run(["bash", str(MAKE)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "不正な値" in (r.stdout + r.stderr)
    assert not marker.exists(), "シェルのメタ文字が実行されてしまった"

    env_ok = dict(os.environ, BASE_CLOSURE_PACKAGES="ubuntu-minimal ubuntu-standard ubuntu-server")
    # 妥当な値は検証だけなら通る（--fetch を付けないので即座に計画表示へ進み、重い処理はしない）。
    r2 = subprocess.run(["bash", str(MAKE)], cwd=ROOT, env=env_ok, capture_output=True, text=True, timeout=30)
    assert "不正な値" not in (r2.stdout + r2.stderr)


def test_target_python_mm_prefers_live_docker_query_over_stale_baseline():
    """報告 RV-LOW-4: 「BASELINE の IMAGE= タグ文字列が一致すれば流用」だと、ローカルの $APT_BASE_IMAGE
    タグが同じ名前のまま差し替わっていたり BASELINE が古いまま残っていても ABI 照合が黙って通ってしまう
    （タグはdigestを保証しない）。docker があれば毎回 docker run で現物から読み直し、BASELINE は
    docker も apt-cache も無い最後の手段としてのみ使うことを固定する（実行しての確認: 実際に stale な
    PYTHON_DEB_VERSION を書いた BASELINE を用意し、docker があるときはそれを無視して実際の候補版を
    返すことを docker で確認済み・作業報告参照）。"""
    text = MAKE.read_text(encoding="utf-8")
    fn = re.search(r"^_target_python_mm\(\) \{.*?^\}", text, re.M | re.S)
    assert fn, "_target_python_mm が見つからない"
    fn_text = fn.group(0)
    docker_pos = fn_text.find("command -v docker")
    baseline_pos = fn_text.find("$OUT/BASELINE")
    assert docker_pos != -1 and baseline_pos != -1
    assert docker_pos < baseline_pos, "docker より先に BASELINE を優先している（stale BASELINE で false pass しうる）"
    assert 'docker run --rm "$APT_BASE_IMAGE"' in fn_text
    # 2026-08-18 Codex RV 2巡目 指摘4: 以前は command -v の有無だけで docker/apt-cache/BASELINE の
    # どれか1つに elif 分岐しており、「docker コマンドはあるが実行が失敗する」（daemon 停止等）場合に
    # apt-cache/BASELINE へフォールスルーしなかった（ver が空のままその1分岐で確定していた）。今は
    # 各手段の**結果が空だったら**次を試す（`[ -z "$ver" ] && ...`）＝BASELINE 借用は「docker も
    # apt-cache も実際に版を返せなかった」ときのみの最後の手段になっていることを固定する。
    assert re.search(r'\[ -z "\$ver" \] && command -v apt-cache', fn_text), \
        "apt-cache フォールバックが「docker が無い」ではなく「ver が空」で条件付けされていない（fail-open の再発）"
    assert re.search(r'\[ -z "\$ver" \] && \[ -f "\$OUT/BASELINE" \]', fn_text), \
        "BASELINE 借用が「ver が空」で条件付けされた最後の手段になっていない"
    # コメント中に来歴として「以前は elif だった」という説明があるのは構わない（消さない）。
    # 実コード行（# 以降を除く）に elif が残っていないことだけを見る＝構造そのものが戻っていないか。
    code_only = "\n".join(ln.split("#", 1)[0] for ln in fn_text.splitlines())
    assert "elif" not in code_only, (
        "docker/apt-cache/BASELINE が elif で1つだけ選ばれる構造に戻っている"
        "（コマンドの有無ではなく実際に版が取れたかどうかで順に試すべき）"
    )


def test_check_python_version_match_fails_closed_when_target_undeterminable():
    """2026-08-18 Codex RV 2巡目 指摘4: 以前は対象 python3 版を特定できないとき warn だけして
    続行していた（fail-open）。docker はあるが daemon 停止／pull 不可のときもこの経路に落ちる
    ため、「版照合という安全網が無言で外れたまま ABI 不一致キットが完成する」実害になっていた。
    どうしても特定できないなら収集を止める（SHERPA_ALLOW_PY_MISMATCH=1 のときだけ警告で続行）。"""
    text = MAKE.read_text(encoding="utf-8")
    fn = re.search(r"^_check_python_version_match\(\) \{.*?^\}", text, re.M | re.S)
    assert fn, "_check_python_version_match が見つからない"
    fn_text = fn.group(0)
    empty_branch = re.search(r'if \[ -z "\$target_mm" \]; then(.*?)\n  fi', fn_text, re.S)
    assert empty_branch, "target_mm が空のときの分岐が見つからない"
    branch_text = empty_branch.group(1)
    assert "SHERPA_ALLOW_PY_MISMATCH" in branch_text, (
        "版を特定できないときに SHERPA_ALLOW_PY_MISMATCH のエスケープハッチが無い")
    assert 'return 0' in branch_text and 'return 1' in branch_text, (
        "版を特定できないとき、エスケープハッチ有り(0)/無し(1)の両方の返り値が無い（fail-open のまま）")
    # 「エスケープハッチ無しなら fail（非0で止める）」であること（warn だけで return 0 に落ちていない）。
    no_escape_part = branch_text.split('return 0', 1)[1] if 'return 0' in branch_text else branch_text
    assert "fail " in no_escape_part, "エスケープハッチ無しの経路で fail（停止）していない"


def test_base_closure_includes_ubuntu_server_metas_and_is_wired():
    """報告①: 最小イメージ（ubuntu:24.04）の導入済み集合だけでは、実機 Ubuntu Server の導入済み集合
    （ubuntu-server/ubuntu-standard 系メタが要求する libglib2.0-bin/packagekit/software-properties-common 等）の
    上げ先が足りず、apt が「その群ごと削除」で辻褄合わせしようとして --no-remove で fail-close する
    （実機報告 2026-08-18・base/debs 92→583 個で解消）。既定 BASE_CLOSURE_PACKAGES が3メタを含み、
    実際の収集経路（_apt_collect_base_closure・--download-only）へ渡っていることを固定する。"""
    text = MAKE.read_text(encoding="utf-8")
    m = re.search(r'^BASE_CLOSURE_PACKAGES="\$\{BASE_CLOSURE_PACKAGES:-([^}]+)\}"', text, re.M)
    assert m, "BASE_CLOSURE_PACKAGES の既定値が見つからない"
    defaults = m.group(1).split()
    for pkg in ("ubuntu-minimal", "ubuntu-standard", "ubuntu-server"):
        assert pkg in defaults, f"既定の BASE_CLOSURE_PACKAGES に {pkg} が無い"
    fn = re.search(r"^_apt_collect_base_closure\(\) \{.*?^\}", text, re.M | re.S)
    assert fn, "_apt_collect_base_closure が見つからない"
    fn_text = fn.group(0)
    assert "$BASE_CLOSURE_PACKAGES" in fn_text and "download-only" in fn_text
    # 実機報告の実測値（92→583）を来歴コメントとして残す（無言で消さない）
    assert "92" in text and "583" in text
    # RV: ls *.deb | wc -l は0件のとき stderr ノイズ（環境によっては set -e で落ちうる）を出す。
    # find に置き換え済みであること（両箇所とも）。
    assert fn_text.count("find /var/cache/apt/archives -maxdepth 1 -name '*.deb'") >= 2


def test_base_closure_has_host_fallback_when_docker_missing():
    """2026-08-18 Codex RV 2巡目 指摘5: RV1是正（収集失敗の致命化）の副作用で、土台の閉包
    （base/debs）だけ docker が無いと即座に失敗していた（他の apt 系グループには
    `_apt_collect_debs_fallback` があるのに土台の閉包だけ逃げ道が無い＝
    `docs/manual/offline-kit.md` が案内している「docker の無い収集機」経路が壊れていた）。
    `--skip-base` で回避させると報告①の実機不具合を再導入するため、`_apt_collect_debs_fallback`
    と同じ作法（sudo・`Dir::Cache::Archives` の一時ディレクトリ差し替え）のフォールバックを
    `_apt_collect_base_closure` の docker 不在分岐に配線したことを固定する（実際に動くことは
    偽 sudo/apt-get で確認済み・作業報告参照。実 sudo が使えないサンドボックスのため本物の
    apt-get 実行そのものは未検証）。"""
    text = MAKE.read_text(encoding="utf-8")
    fallback_fn = re.search(r"^_apt_collect_base_closure_fallback\(\) \{.*?^\}", text, re.M | re.S)
    assert fallback_fn, "_apt_collect_base_closure_fallback が見つからない（host fallback 未実装）"
    fb_text = fallback_fn.group(0)
    assert "sudo apt-get" in fb_text
    assert "Dir::Cache::Archives=" in fb_text
    assert "--reinstall" in fb_text and "--download-only" in fb_text
    assert "$BASE_CLOSURE_PACKAGES" in fb_text

    main_fn = re.search(r"^_apt_collect_base_closure\(\) \{.*?^\}", text, re.M | re.S)
    assert main_fn, "_apt_collect_base_closure が見つからない"
    main_text = main_fn.group(0)
    docker_missing_branch = re.search(
        r'if ! command -v docker >/dev/null 2>&1; then(.*?)\n  fi', main_text, re.S)
    assert docker_missing_branch, "docker 不在分岐が見つからない"
    branch_text = docker_missing_branch.group(1)
    assert "_apt_collect_base_closure_fallback" in branch_text, (
        "docker 不在時にフォールバック関数を呼んでいない（--skip-base 以外の逃げ道が無いまま）")
    assert "return 1" in branch_text, "フォールバックが失敗したときに失敗を返していない（黙って空の閉包で成功にしていないか）"


def test_python_version_mismatch_stops_host_collection():
    """報告②: 収集側 Python の版を記録するだけでは、閉域側と ABI が違う wheel が黙って完成してしまう
    （3.9/3.11 収集機で作ったキットが閉域で初めて壊れる）。端末 Python 収集（SHERPA_WHEELS_IN_CONTAINER=0）の
    ときは major.minor の不一致で fail・非0を返し、呼び出し元がそれを見て収集を止める（exit 1）。
    コンテナ収集（既定）のときは対象OS内 python3 で pip download するため版照合は不要だが、実際に
    使われた python3 の版は記録する（版照合が要らない＝記録もしない、にはしない）。"""
    text = MAKE.read_text(encoding="utf-8")
    fn = re.search(r"^_check_python_version_match\(\) \{.*?^\}", text, re.M | re.S)
    assert fn, "_check_python_version_match が見つからない"
    fn_text = fn.group(0)
    assert 'fail "収集側 Python（' in fn_text
    assert "return 1" in fn_text and "SHERPA_ALLOW_PY_MISMATCH" in fn_text
    # 呼び出し元: 端末 Python 収集のときだけ必須
    assert '_check_python_version_match "$HOST_PY_MM" || exit 1' in text
    # コンテナ収集時も実際に使われた python3 の版を記録する（wheels/COLLECTED-WITH-PYTHON-VERSION.txt）
    pip_fn = re.search(r"^_pip_download_wheels\(\) \{.*?^\}", text, re.M | re.S)
    assert pip_fn, "_pip_download_wheels が見つからない"
    assert "COLLECTED-WITH-PYTHON-VERSION.txt" in pip_fn.group(0)


def test_ocr_wheels_go_through_same_download_helper():
    """OCR 専用 wheel（requirements-ocr.txt）もコア wheel（requirements.txt）と同じ _pip_download_wheels を
    通ること（版照合・コンテナ収集の契約が OCR だけ抜け穴にならないように・T1 スコープ確認）。"""
    text = MAKE.read_text(encoding="utf-8")
    assert "_pip_download_wheels requirements-ocr.txt" in text
    assert "_pip_download_wheels requirements.txt constraints.txt" in text


def test_verify_offline_kit_apt_script_exists_and_is_wired():
    """③: 出荷ゲート（scripts/verify_offline_kit_apt.sh）が存在し、構文が通り、apt_offline.sh をそのまま
    使い（別経路を作らない）、docker 不在では黙って成功にせず、make verify-kit から呼べること。
    実際の apt 導入（docker必須・--network none）は実機・手動で確認する（ここでは静的固定のみ）。"""
    verify = ROOT / "scripts" / "verify_offline_kit_apt.sh"
    assert verify.exists(), "scripts/verify_offline_kit_apt.sh が無い"
    subprocess.run(["bash", "-n", str(verify)], check=True)
    text = verify.read_text(encoding="utf-8")
    # 導入経路は apt_offline_install（scripts/lib/apt_offline.sh）をそのまま使う＝別経路を作らない
    assert "apt_offline_install" in text and "/mnt/apt_offline.sh" in text
    assert "--network none" in text
    # docker が無い環境は「検証できません」と明示して非0（黙って成功にしない）
    assert "docker がありません" in text
    # 稼働中の docker 資産（sherpa-mvp-*/sherpa-mvp_*）に触らない・自分が作るものは sherpa-wftest- 接頭辞
    assert "sherpa-wftest-" in text
    assert "sherpa-mvp" not in text
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    m2 = re.search(r"^verify-kit:.*\n(?:\t.*\n?)+", makefile, re.M)
    assert m2, "Makefile に verify-kit ターゲットが無い"
    assert "verify_offline_kit_apt.sh" in m2.group(0)
    phony_block = makefile.split(".PHONY:", 1)[1].split("\n\n", 1)[0]
    assert "verify-kit" in phony_block, "verify-kit が .PHONY に無い"
