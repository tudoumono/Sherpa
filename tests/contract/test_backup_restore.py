"""バックアップ/復元スクリプトの契約（2026-08-18・docs/18 §7「make backup 未整備」の穴埋め）。"""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts" / "backup.sh"
RESTORE = ROOT / "scripts" / "restore.sh"
RUN_COMMON = ROOT / "scripts" / "run-common.sh"

FAKE_DOCKER = r"""#!/usr/bin/env bash
# 偽 docker: 引数を ARGLOG に追記し、サブコマンドごとに決め打ちの応答を返す。
echo "$*" >> "$ARGLOG"
case "$1 $2" in
  "info ") exit 0 ;;
  "ps --format"|"ps -a")
    filter=""; fmt=""
    for a in "$@"; do case "$a" in volume=*) filter="${a#volume=}" ;; "{{.Names}}"*) fmt="$a" ;; esac; done
    if [ -z "${FAKE_RUNNING_VOLUME:-}" ] || [ "$filter" = "$FAKE_RUNNING_VOLUME" ]; then
      for n in ${FAKE_RUNNING:-}; do
        case "$fmt" in *State*) printf '%s\trunning\n' "$n" ;; *) printf '%s\n' "$n" ;; esac
      done
    fi
    ;;
  "image ls") printf '%s\n' ${FAKE_IMAGES-postgres:16} ;;
  "image inspect") echo "sha256:deadbeef" ;;
  "volume inspect")
    [ "${3:-}" = "${FAKE_MISSING_VOLUME:-}" ] && [ -n "${FAKE_MISSING_VOLUME:-}" ] && exit 1
    exit 0
    ;;
  "volume rm"|"volume create") exit 0 ;;
  "run --rm")
    # tar czf /b/<vol>.tar.gz … の形なら空の tar.gz を置く（chown/xzf は何もしない）
    for a in "$@"; do case "$a" in /b/*.tar.gz) [ "$3" = "tar" ] || :; ;; esac; done
    ;;
  "stop "*) exit 0 ;;
esac
exit 0
"""


def _fake_docker(
    bin_dir: Path,
    arglog: Path,
    running: str = "",
    images: str = "postgres:16",
    running_volume: str = "",
    missing_volume: str = "",
) -> dict[str, str]:
    bin_dir.mkdir(parents=True, exist_ok=True)
    d = bin_dir / "docker"
    d.write_text(FAKE_DOCKER, encoding="utf-8")
    d.chmod(d.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["ARGLOG"] = str(arglog)
    env["FAKE_RUNNING"] = running
    env["FAKE_RUNNING_VOLUME"] = running_volume
    env["FAKE_MISSING_VOLUME"] = missing_volume
    env["FAKE_IMAGES"] = images
    return env


def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60, cwd=ROOT)


def _env_for(tmp_path: Path, env: dict[str, str], project: str = "sherpa-wftest") -> dict[str, str]:
    users = tmp_path / "users"
    users.mkdir(exist_ok=True)
    (users / "u1.txt").write_text("x", encoding="utf-8")
    dotenv = tmp_path / "env"
    dotenv.write_text("SHERPA_PORT=8000\nOPENAI_API_KEY=dummy\n", encoding="utf-8")
    env.update(
        {
            "SHERPA_COMPOSE_PROJECT": project,
            "SHERPA_BACKUP_DIR": str(tmp_path / "bk"),
            "SHERPA_USERS_DIR": str(users),
            "SHERPA_ENV_FILE": str(dotenv),
        }
    )
    return env


def test_scripts_parse_help_and_makefile_wiring():
    for s in (BACKUP, RESTORE):
        assert subprocess.run(["bash", "-n", str(s)], capture_output=True).returncode == 0, s
        r = subprocess.run([str(s), "--help"], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "使い方" in r.stdout, s
    r = subprocess.run([str(RESTORE), "/nonexistent-dir"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "ありません" in r.stderr   # MANIFEST が無いものは復元しない
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\nbackup:" in mk and "scripts/backup.sh" in mk and "\nrestore:" in mk and "scripts/restore.sh" in mk
    phony = mk.split(".PHONY:", 1)[1].split("\n\n", 1)[0]
    assert "backup" in phony and "restore" in phony


def test_sha256_helper_falls_back_without_sha256sum(tmp_path: Path):
    """run-common.sh の sha256 ヘルパーは sha256sum が PATH に無くても shasum -a 256 へフォールバックする。"""
    payload = tmp_path / "f.txt"
    payload.write_bytes(b"hello sherpa\n")
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        d = Path(entry)
        if not d.is_dir():
            continue
        for exe in d.iterdir():
            if exe.name == "sha256sum" or (stub_bin / exe.name).exists():
                continue
            try:
                (stub_bin / exe.name).symlink_to(exe)
            except OSError:
                continue
    r = subprocess.run(["bash", "-c", f'. "{RUN_COMMON}"; sherpa_sha256_hex "{payload}"'],
                       env=dict(os.environ, PATH=str(stub_bin)), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == hashlib.sha256(payload.read_bytes()).hexdigest()


def test_dry_run_prints_plan_and_writes_nothing(tmp_path: Path):
    env = _env_for(tmp_path, _fake_docker(tmp_path / "bin", tmp_path / "args.log"))
    r = _run([str(BACKUP), "--dry-run"], env)
    assert r.returncode == 0, r.stderr
    for needle in ("sherpa-wftest_pg sherpa-wftest_neo4j sherpa-wftest_es", str(tmp_path / "users"),
                   str(tmp_path / "env"), "ワークイメージ: postgres:16", "含めない（--with-derived",
                   "--dry-run のため何も書きませんでした"):
        assert needle in r.stdout, (needle, r.stdout)
    assert not (tmp_path / "bk").exists()
    assert "run --rm" not in (tmp_path / "args.log").read_text(encoding="utf-8")   # tar 用の docker run を呼ばない
    env["SHERPA_DERIVED_DIR"] = str(tmp_path / "derived")
    r = _run([str(BACKUP), "--dry-run", "--with-derived"], env)
    assert r.returncode == 0 and f"派生物:        {tmp_path / 'derived'}" in r.stdout


def test_backup_refuses_while_store_running(tmp_path: Path):
    arglog = tmp_path / "args.log"
    env = _env_for(tmp_path, _fake_docker(tmp_path / "bin", arglog, running="sherpa-wftest-postgres-1",
                                          running_volume="sherpa-wftest_pg"))
    r = _run([str(BACKUP)], env)
    assert r.returncode == 3   # 3=稼働中（install 13a が「警告して続行」と区別できる）
    assert "稼働中" in r.stderr and "make stop" in r.stderr and "--stop" in r.stderr
    assert not (tmp_path / "bk").exists() and "run --rm" not in arglog.read_text(encoding="utf-8")
    # --stop はそのプロジェクトのコンテナだけを止めて外す（偽 docker は ps が同じ答えを返すので最終的には 3）
    r = _run([str(BACKUP), "--stop"], env)
    log = arglog.read_text(encoding="utf-8")
    assert "stop sherpa-wftest-postgres-1" in log and "rm sherpa-wftest-postgres-1" in log and r.returncode == 3
    # 接頭辞が違う別プロジェクトのコンテナは稼働中と数えない
    other = _env_for(tmp_path, _fake_docker(tmp_path / "bin", arglog, running="sherpa-mvp-postgres-1",
                                            running_volume="sherpa-mvp_pg"))
    r = _run([str(BACKUP), "--dry-run"], other)
    assert r.returncode == 0 and "稼働中" not in r.stderr


@pytest.mark.parametrize("kw,env_missing,needles", [
    (dict(images=""), False, ["既知の docker イメージがありません"]),
    (dict(missing_volume="sherpa-wftest_es"), False, ["必要な3 volume", "sherpa-wftest_es"]),
    (dict(), True, ["不完全なバックアップ"]),
])
def test_backup_refuses_incomplete_environment(tmp_path: Path, kw, env_missing, needles):
    env = _env_for(tmp_path, _fake_docker(tmp_path / "bin", tmp_path / "args.log", **kw))
    if env_missing:
        env["SHERPA_ENV_FILE"] = str(tmp_path / "missing.env")
        needles = [*needles, "missing.env"]
    r = _run([str(BACKUP), *(["--dry-run"] if kw.get("images") == "" or env_missing else [])], env)
    assert r.returncode == 1 and all(n in r.stderr for n in needles)
    assert not (tmp_path / "bk").exists()


def _make_backup_dir(tmp_path: Path, tamper: bool, *, with_volume: bool = True) -> Path:
    bk = tmp_path / "bk" / "20260818-000000"
    (bk / "volumes").mkdir(parents=True)
    files = {}
    payload = tmp_path / "payload.txt"
    payload.write_text("restored", encoding="utf-8")
    names = ["users.tar.gz", "env"]
    if with_volume:
        names.insert(0, "volumes/sherpa-wftest_pg.tar.gz")
    for name in names:
        p = bk / name
        if name.endswith(".tar.gz"):
            with tarfile.open(p, "w:gz") as tf:
                tf.add(payload, arcname="payload.txt")
        else:
            p.write_text("OPENAI_API_KEY=backup-secret\nSHERPA_PORT=9000\n", encoding="utf-8")
        files[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    if tamper:
        # 改ざんは「別内容の正しい tar.gz」にする（非 tar だと後段の tar tzf で止まり sha256 ゲートの欠落を検出できない）
        evil = tmp_path / "evil.txt"
        evil.write_text("evil", encoding="utf-8")
        with tarfile.open(bk / "users.tar.gz", "w:gz") as tf:
            tf.add(evil, arcname="u1.txt")
    lines = ["sherpa_backup=1", "version=0.1.0", "created=now", "host=t", "project=sherpa-wftest",
             "work_image=postgres:16", "complete=1", "[sha256]"]
    lines += [f"{h}  {n}" for n, h in sorted(files.items())]
    (bk / "MANIFEST").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return bk


def _unlisted(bk):
    (bk / "volumes" / "victim_volume.tar.gz").write_bytes(b"not-listed")


def _listed_unexpected(bk):
    extra = bk / "volumes" / "victim_volume.tar.gz"
    extra.write_bytes(b"listed-but-not-allowed")
    with (bk / "MANIFEST").open("a", encoding="utf-8") as fh:
        fh.write(f"{hashlib.sha256(extra.read_bytes()).hexdigest()}  volumes/{extra.name}\n")


@pytest.mark.parametrize("scenario,tamper,with_volume,running,message", [
    ("sha256 mismatch", True, True, "", ["sha256 が一致しない", "何も変更していません"]),
    ("unlisted payload", False, True, "", ["MANIFEST にないファイル"]),
    ("listed unexpected volume", False, False, "", ["許可されていない復元対象"]),
    ("store running", False, True, "sherpa-wftest-neo4j-1", ["参照しているコンテナ", "make stop"]),
])
def test_restore_refuses_without_touching_anything(tmp_path: Path, scenario, tamper, with_volume, running, message):
    arglog = tmp_path / "args.log"
    env = _env_for(tmp_path, _fake_docker(tmp_path / "bin", arglog, running=running))
    env["YES"] = "1"
    bk = _make_backup_dir(tmp_path, tamper=tamper, with_volume=with_volume)
    if scenario == "unlisted payload":
        _unlisted(bk)
    elif scenario == "listed unexpected volume":
        _listed_unexpected(bk)
    r = _run([str(RESTORE), str(bk)], env)
    assert r.returncode == 1 and all(m in r.stderr for m in message)
    log = arglog.read_text(encoding="utf-8") if arglog.exists() else ""
    assert "volume rm" not in log and "run --rm" not in log   # 任意名の volume を消さない・何も復元しない
    assert (tmp_path / "users" / "u1.txt").exists()   # 個人領域も無傷


def test_restore_redacts_env_values_when_showing_difference(tmp_path: Path):
    env = _env_for(tmp_path, dict(os.environ))
    Path(env["SHERPA_ENV_FILE"]).write_text("OPENAI_API_KEY=current-secret\nSHERPA_PORT=8000\n", encoding="utf-8")
    env["YES"] = "1"
    bk = _make_backup_dir(tmp_path, tamper=False, with_volume=False)
    r = _run([str(RESTORE), str(bk)], env)
    assert r.returncode == 0, r.stdout + r.stderr
    combined = r.stdout + r.stderr
    assert "OPENAI_API_KEY" in combined and "SHERPA_PORT" in combined
    assert "current-secret" not in combined and "backup-secret" not in combined
    assert not (tmp_path / "users" / "u1.txt").exists()
    assert (tmp_path / "users" / "payload.txt").read_text(encoding="utf-8") == "restored"
    before = list(tmp_path.glob("users.before-restore-*"))
    assert len(before) == 1 and (before[0] / "u1.txt").exists()


def test_installer_backs_up_before_switch_and_docs_describe_it():
    s = (ROOT / "scripts" / "install_offline_kit.sh").read_text(encoding="utf-8")
    hook = s.index("# 13a. 更新時のバックアップ")
    finalize = s.index('rm -f "$PENDING_MARKER_PATH"')
    swap = s.index('if _atomic_symlink_swap "$TARGET_DIR" "$PENDING_SWAP_TO"')
    assert hook < finalize < swap, "バックアップは版の確定・current 切替より前でなければ意味がない"
    body = s[hook:swap]
    assert "scripts/backup.sh" in body and "SHERPA_BACKUP_BEFORE_SWITCH" in body
    assert "バックアップ未取得（ストア/アプリ稼働中）" in body and "make stop && make backup" in body
    assert "3)" in body and "SHERPA_DOCKER" in body   # 稼働中=exit 3 を区別
    assert "版の確定と current の切替を中止" in body and "VERIFY_FAILED=1" in body
    assert "docker ps" not in body   # project 名の解決は backup.sh に委ねる
    assert "env ${_BK_ENV:+" not in body and 'SHERPA_ENV_FILE="$_BK_ENV"' in body   # 空白入り path を分割しない
    d18 = (ROOT / "docs" / "18-オフライン構築.md").read_text(encoding="utf-8")
    assert "未整備（既知の穴）" not in d18 and "make backup" in d18 and "make restore" in d18
    m40 = (ROOT / "docs" / "manual" / "40-運用.md").read_text(encoding="utf-8")
    assert "バックアップと復元" in m40 and "SHERPA_BACKUP_BEFORE_SWITCH" in m40
    assert "SHERPA_BACKUP_DIR" in (ROOT / "docs" / "manual" / "90-リファレンス.md").read_text(encoding="utf-8")
