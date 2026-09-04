"""バックアップ/復元スクリプトの契約（2026-08-18・docs/18 §7「make backup 未整備」の穴埋め）。

外部サービス不要: docker は PATH 先頭に置いた偽物で置き換え、呼び出し引数をログに残して検証する。
実 docker ボリュームでの往復（印を置く→backup→消す→restore→戻る）は手動の実機確認で行う。

契約:
- backup.sh / restore.sh は bash -n を通り、--help で使い方を出す
- backup.sh --dry-run は計画（出力先・ボリューム3つ・個人領域・.env・ワークイメージ）を出し、何も書かない
- <project>- 接頭辞のコンテナが動いていれば backup は fail（`make stop` を案内・--stop で自動停止）
- restore は MANIFEST の sha256 が1つでも合わなければ何も変更せず止まる（docker volume rm を呼ばない）
- Makefile に backup / restore ターゲットがある
- install_offline_kit.sh は current 切替の前に backup.sh を呼ぶ経路を持つ（稼働中は警告して続行）
"""
from __future__ import annotations

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


def test_scripts_parse_and_help():
    for s in (BACKUP, RESTORE):
        assert subprocess.run(["bash", "-n", str(s)], capture_output=True).returncode == 0, s
        r = subprocess.run([str(s), "--help"], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "使い方" in r.stdout, s


def test_dry_run_prints_plan_and_writes_nothing(tmp_path: Path):
    env = _env_for(tmp_path, _fake_docker(tmp_path / "bin", tmp_path / "args.log"))
    r = _run([str(BACKUP), "--dry-run"], env)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    for needle in (
        "sherpa-wftest_pg sherpa-wftest_neo4j sherpa-wftest_es",
        str(tmp_path / "users"),
        str(tmp_path / "env"),
        "ワークイメージ: postgres:16",
        "含めない（--with-derived",
        "--dry-run のため何も書きませんでした",
    ):
        assert needle in out, (needle, out)
    assert not (tmp_path / "bk").exists()
    # dry-run では tar 用の docker run を一切呼ばない
    assert "run --rm" not in (tmp_path / "args.log").read_text(encoding="utf-8")


def test_dry_run_with_derived_lists_derived_dir(tmp_path: Path):
    env = _env_for(tmp_path, _fake_docker(tmp_path / "bin", tmp_path / "args.log"))
    env["SHERPA_DERIVED_DIR"] = str(tmp_path / "derived")
    r = _run([str(BACKUP), "--dry-run", "--with-derived"], env)
    assert r.returncode == 0, r.stderr
    assert f"派生物:        {tmp_path / 'derived'}" in r.stdout


def test_backup_fails_when_store_running_and_suggests_make_stop(tmp_path: Path):
    arglog = tmp_path / "args.log"
    env = _env_for(
        tmp_path,
        _fake_docker(
            tmp_path / "bin",
            arglog,
            running="sherpa-wftest-postgres-1",
            running_volume="sherpa-wftest_pg",
        ),
    )
    r = _run([str(BACKUP)], env)
    assert r.returncode == 3  # 3=稼働中
    assert "稼働中" in r.stderr and "make stop" in r.stderr and "--stop" in r.stderr
    assert not (tmp_path / "bk").exists()
    assert "run --rm" not in arglog.read_text(encoding="utf-8")


def test_backup_ignores_other_projects_containers(tmp_path: Path):
    """接頭辞が違うコンテナ（別プロジェクト）は「稼働中」と数えない。"""
    env = _env_for(
        tmp_path,
        _fake_docker(
            tmp_path / "bin",
            tmp_path / "args.log",
            running="sherpa-mvp-postgres-1",
            running_volume="sherpa-mvp_pg",
        ),
    )
    r = _run([str(BACKUP), "--dry-run"], env)
    assert r.returncode == 0, r.stderr
    assert "稼働中" not in r.stderr


def test_backup_stop_flag_stops_only_that_projects_containers(tmp_path: Path):
    """--stop（既定でないプロジェクト）は docker stop <そのコンテナ> を呼び、止まった後に続行する。"""
    arglog = tmp_path / "args.log"
    env = _env_for(
        tmp_path,
        _fake_docker(
            tmp_path / "bin",
            arglog,
            running="sherpa-wftest-postgres-1",
            running_volume="sherpa-wftest_pg",
        ),
    )
    # 偽 docker は ps が常に同じ答えを返すので、stop 後も「稼働中」→ fail する。stop が呼ばれたことだけ確認する。
    r = _run([str(BACKUP), "--stop"], env)
    log = arglog.read_text(encoding="utf-8")
    assert "stop sherpa-wftest-postgres-1" in log and "rm sherpa-wftest-postgres-1" in log  # 止めるだけでなく外す
    assert r.returncode == 3  # 3=稼働中（呼び出し側の install 13a が「警告して続行」と区別できる）


def test_backup_fails_without_any_image(tmp_path: Path):
    env = _env_for(tmp_path, _fake_docker(tmp_path / "bin", tmp_path / "args.log", images=""))
    r = _run([str(BACKUP), "--dry-run"], env)
    assert r.returncode == 1
    assert "既知の docker イメージがありません" in r.stderr


def test_backup_refuses_missing_required_volume(tmp_path: Path):
    env = _env_for(
        tmp_path,
        _fake_docker(
            tmp_path / "bin",
            tmp_path / "args.log",
            missing_volume="sherpa-wftest_es",
        ),
    )
    r = _run([str(BACKUP)], env)
    assert r.returncode == 1
    assert "必要な3 volume" in r.stderr and "sherpa-wftest_es" in r.stderr
    assert not (tmp_path / "bk").exists()


def test_backup_refuses_missing_explicit_env_file(tmp_path: Path):
    env = _env_for(tmp_path, _fake_docker(tmp_path / "bin", tmp_path / "args.log"))
    missing = tmp_path / "missing.env"
    env["SHERPA_ENV_FILE"] = str(missing)
    r = _run([str(BACKUP), "--dry-run"], env)
    assert r.returncode == 1
    assert str(missing) in r.stderr and "不完全なバックアップ" in r.stderr


def _make_backup_dir(tmp_path: Path, tamper: bool, *, with_volume: bool = True) -> Path:
    import hashlib

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
        # 改ざんは「別内容の**正しい** tar.gz」にする。非 tar のバイト列だと後段の `tar tzf` 検査でも止まり、
        # sha256 ゲート自体が欠けても緑のままになる（RV: 変異実験で検出できなかった）。
        evil = tmp_path / "evil.txt"
        evil.write_text("evil", encoding="utf-8")
        with tarfile.open(bk / "users.tar.gz", "w:gz") as tf:
            tf.add(evil, arcname="u1.txt")
    lines = ["sherpa_backup=1", "version=0.1.0", "created=now", "host=t", "project=sherpa-wftest",
             "work_image=postgres:16", "complete=1", "[sha256]"]
    lines += [f"{h}  {n}" for n, h in sorted(files.items())]
    (bk / "MANIFEST").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return bk


def test_restore_stops_on_sha256_mismatch_without_touching_anything(tmp_path: Path):
    arglog = tmp_path / "args.log"
    env = _env_for(tmp_path, _fake_docker(tmp_path / "bin", arglog))
    env["YES"] = "1"
    bk = _make_backup_dir(tmp_path, tamper=True)
    r = _run([str(RESTORE), str(bk)], env)
    assert r.returncode == 1
    assert "sha256 が一致しない" in r.stderr and "何も変更していません" in r.stderr
    log = arglog.read_text(encoding="utf-8") if arglog.exists() else ""
    assert "volume rm" not in log and "run --rm" not in log
    assert (tmp_path / "users" / "u1.txt").exists()  # 個人領域も無傷


def test_restore_refuses_when_store_running(tmp_path: Path):
    arglog = tmp_path / "args.log"
    env = _env_for(tmp_path, _fake_docker(tmp_path / "bin", arglog, running="sherpa-wftest-neo4j-1"))
    env["YES"] = "1"
    bk = _make_backup_dir(tmp_path, tamper=False)
    r = _run([str(RESTORE), str(bk)], env)
    assert r.returncode == 1
    assert "参照しているコンテナ" in r.stderr and "make stop" in r.stderr
    assert "volume rm" not in arglog.read_text(encoding="utf-8")


def test_restore_rejects_unlisted_payload_before_docker_or_filesystem_changes(tmp_path: Path):
    """MANIFEST 外の追加 tar を glob で拾い、任意名の volume を消してはならない。"""
    arglog = tmp_path / "args.log"
    env = _env_for(tmp_path, _fake_docker(tmp_path / "bin", arglog))
    env["YES"] = "1"
    bk = _make_backup_dir(tmp_path, tamper=False)
    (bk / "volumes" / "victim_volume.tar.gz").write_bytes(b"not-listed")

    r = _run([str(RESTORE), str(bk)], env)

    assert r.returncode == 1
    assert "MANIFEST にないファイル" in r.stderr
    log = arglog.read_text(encoding="utf-8") if arglog.exists() else ""
    assert "volume rm" not in log and "run --rm" not in log
    assert (tmp_path / "users" / "u1.txt").exists()


def test_restore_rejects_listed_unexpected_volume_name(tmp_path: Path):
    """checksum が正しくても、3つの所定名以外の volume tar は削除対象にしない。"""
    import hashlib

    arglog = tmp_path / "args.log"
    env = _env_for(tmp_path, _fake_docker(tmp_path / "bin", arglog))
    env["YES"] = "1"
    bk = _make_backup_dir(tmp_path, tamper=False, with_volume=False)
    extra = bk / "volumes" / "victim_volume.tar.gz"
    extra.write_bytes(b"listed-but-not-allowed")
    with (bk / "MANIFEST").open("a", encoding="utf-8") as fh:
        fh.write(f"{hashlib.sha256(extra.read_bytes()).hexdigest()}  volumes/{extra.name}\n")

    r = _run([str(RESTORE), str(bk)], env)

    assert r.returncode == 1
    assert "許可されていない復元対象" in r.stderr
    log = arglog.read_text(encoding="utf-8") if arglog.exists() else ""
    assert "volume rm" not in log and "run --rm" not in log


def test_restore_redacts_env_values_when_showing_difference(tmp_path: Path):
    env = _env_for(tmp_path, dict(os.environ))
    current_env = Path(env["SHERPA_ENV_FILE"])
    current_env.write_text("OPENAI_API_KEY=current-secret\nSHERPA_PORT=8000\n", encoding="utf-8")
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


def test_restore_requires_manifest():
    r = subprocess.run([str(RESTORE), "/nonexistent-dir"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "ありません" in r.stderr


def test_makefile_has_backup_and_restore_targets():
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\nbackup:" in mk and "scripts/backup.sh" in mk
    assert "\nrestore:" in mk and "scripts/restore.sh" in mk and "FROM" in mk
    phony = mk.split(".PHONY:", 1)[1].split("\n\n", 1)[0]
    assert "backup" in phony and "restore" in phony


def test_installer_backs_up_before_switch_and_warns_when_running():
    """更新時の current 切替の前に backup.sh を呼ぶ経路がある（稼働中は警告して続行・抑止変数あり）。"""
    s = (ROOT / "scripts" / "install_offline_kit.sh").read_text(encoding="utf-8")
    hook = s.index("# 13a. 更新時のバックアップ")
    finalize = s.index('rm -f "$PENDING_MARKER_PATH"')
    swap = s.index('if _atomic_symlink_swap "$TARGET_DIR" "$PENDING_SWAP_TO"')
    assert hook < finalize < swap, "バックアップは版の確定・current 切替より前でなければ意味がない"
    body = s[hook:swap]
    assert 'scripts/backup.sh' in body
    assert "SHERPA_BACKUP_BEFORE_SWITCH" in body
    assert "バックアップ未取得（ストア/アプリ稼働中）" in body and "make stop && make backup" in body
    assert "3)" in body and "SHERPA_DOCKER" in body  # 稼働中=exit 3 を区別・docker コマンドを引き継ぐ
    assert "版の確定と current の切替を中止" in body and "VERIFY_FAILED=1" in body
    # project 名の解決は backup.sh に委ねる（13a では二重判定しない＝判定ずれで fail-close になった RV の是正）
    assert 'docker ps' not in body
    # `${var:+KEY="$var"}` を未引用で env へ渡すと、値に引用符が混入し、空白を含む path は分割される。
    assert 'env ${_BK_ENV:+' not in body
    assert 'SHERPA_ENV_FILE="$_BK_ENV"' in body


def test_docs_mention_backup_as_implemented():
    d18 = (ROOT / "docs" / "18-オフライン構築.md").read_text(encoding="utf-8")
    assert "未整備（既知の穴）" not in d18 and "make backup" in d18 and "make restore" in d18
    m40 = (ROOT / "docs" / "manual" / "40-運用.md").read_text(encoding="utf-8")
    assert "バックアップと復元" in m40 and "SHERPA_BACKUP_BEFORE_SWITCH" in m40
    m90 = (ROOT / "docs" / "manual" / "90-リファレンス.md").read_text(encoding="utf-8")
    assert "SHERPA_BACKUP_DIR" in m90
