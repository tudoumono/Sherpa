"""`scripts/nuke.sh` の確認の流れ（2 回の確認・端末必須・YES の扱い・本番）。"""
from __future__ import annotations

import os
import pathlib
import pty
import shutil
import subprocess
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOKEN = "I-UNDERSTAND-ALL-DATA-WILL-BE-DELETED"


@pytest.fixture()
def sandbox(tmp_path):
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True)
    for name in ("nuke.sh", "run-common.sh"):
        shutil.copy(ROOT / "scripts" / name, app / "scripts" / name)
    (app / "scripts" / "stop.sh").write_text("#!/bin/sh\nexit 0\n")
    (app / "scripts" / "stop.sh").chmod(0o755)
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text("#!/bin/sh\nexit 0\n")
    (fake / "docker").chmod(0o755)
    (fake / "sleep").write_text("#!/bin/sh\necho x >> \"$SLEEP_LOG\"\n")   # 猶予の待ちを外側で潰す
    (fake / "sleep").chmod(0o755)
    data = {}
    for key in ("derived", "users", "observations", "kb"):
        d = tmp_path / "store" / key
        d.mkdir(parents=True)
        (d / "x.txt").write_text("x")
        data[key] = d
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "SHERPA_ENV_FILE": str(tmp_path / "none.env"),
           "SHERPA_DERIVED_DIR": str(data["derived"]), "SHERPA_USERS_DIR": str(data["users"]),
           "SHERPA_OBSERVATION_DIR": str(data["observations"]), "SHERPA_KB_DIR": str(data["kb"]),
           "SHERPA_NUKE_COUNTDOWN_SEC": "0",   # 旧指定（今は効かない）
           "SLEEP_LOG": str(tmp_path / "sleep.log")}
    (tmp_path / "none.env").write_text("")
    env.pop("YES", None)
    env.pop("SHERPA_ENV", None)
    return app, env, data


def _run(app, env, *, lines=None, **extra):
    """lines があれば疑似端末の標準入力へ打つ。無ければ標準入力は端末でない。"""
    env = {**env, **extra}
    cmd = ["bash", str(app / "scripts" / "nuke.sh")]
    if lines is None:
        return subprocess.run(cmd, cwd=app, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=60)
    master, slave = pty.openpty()
    p = subprocess.Popen(cmd, cwd=app, env=env, stdin=slave, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    os.close(slave)
    os.write(master, ("".join(f"{x}\n" for x in lines) + "\x04").encode())   # 末尾は Ctrl-D（入力の終わり）
    out, _ = p.communicate(timeout=60)
    os.close(master)
    return subprocess.CompletedProcess(cmd, p.returncode, out, "")


def _word() -> str:
    return subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip()


def _alive(data) -> bool:
    return all((d / "x.txt").exists() for d in data.values())


def test_nuke_requires_two_confirmations_and_a_terminal(sandbox):
    app, env, data = sandbox
    # 1 回目の yes だけ・2 回目の語が違う → 消えない
    r = _run(app, env, lines=["yes"])
    assert _alive(data), r.stdout
    r = _run(app, env, lines=["yes", "wrong-name"])
    assert _alive(data) and "名前が一致しません" in r.stdout, r.stdout
    r = _run(app, env, lines=["no"])
    assert _alive(data) and "中止しました" in r.stdout
    # 端末でなければ確認できず中止
    r = _run(app, env)
    assert r.returncode == 1 and _alive(data) and "端末ではなく" in r.stderr
    # 旧 YES=1 は廃止（使い方を出して中止）
    r = _run(app, env, YES="1")
    assert r.returncode == 2 and _alive(data) and "廃止" in r.stderr
    # 2 回とも正しく答えると消える
    r = _run(app, env, lines=["yes", _word()])
    assert "すべて消えます。元に戻せません。" in r.stdout and "この環境の名前" in r.stdout, r.stdout
    assert not any((d / "x.txt").exists() for d in data.values()), r.stdout


def test_nuke_skip_token_and_production(sandbox):
    app, env, data = sandbox
    # 本番では省く指定も受け付けず、何も消さない
    r = _run(app, env, YES=TOKEN, SHERPA_ENV="production")
    assert r.returncode == 2 and _alive(data) and "本番" in r.stderr
    # 長い決まった文字列と完全一致なら、非端末でも確認なしで消える
    r = _run(app, env, YES=TOKEN)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any((d / "x.txt").exists() for d in data.values())


def _interactive(app, env, first, answer_for):
    """疑似端末で対話する: first を打ち、プロンプトに出た語を answer_for(出力) で返信する。"""
    master, slave = pty.openpty()
    p = subprocess.Popen(["bash", str(app / "scripts" / "nuke.sh")], cwd=app, env=env, stdin=slave,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    os.close(slave)
    os.write(master, f"{first}\n".encode())
    seen = b""
    sent = False
    deadline = time.time() + 30
    import select
    while time.time() < deadline:
        r, _, _ = select.select([p.stdout], [], [], 0.5)
        if r:
            chunk = os.read(p.stdout.fileno(), 4096)
            if not chunk:
                break
            seen += chunk
        if not sent and "入力: ".encode() in seen:
            os.write(master, f"{answer_for(seen.decode())}\n".encode())
            sent = True
    p.wait(timeout=30)
    os.close(master)
    return p.returncode, seen.decode()


def test_nuke_env_file_production_fixed_countdown_and_random_code(sandbox, tmp_path):
    app, env, data = sandbox
    # 環境変数ファイルの SHERPA_ENV=production でも、確認を省く指定は受け付けない
    envfile = tmp_path / "prod.env"
    envfile.write_text("SHERPA_ENV=production\n")
    r = _run(app, {**env, "SHERPA_ENV_FILE": str(envfile)}, YES=TOKEN)
    assert r.returncode == 2 and _alive(data) and "本番" in r.stderr
    # 明示した環境変数ファイルが読めなければ、確認を省く指定でも何も消さずに止まる
    r = _run(app, {**env, "SHERPA_ENV_FILE": str(tmp_path / "missing.env")}, YES=TOKEN)
    assert r.returncode == 1 and _alive(data) and "読めません" in r.stderr
    r = _run(app, {**env, "SHERPA_ENV_FILE": str(tmp_path)}, YES=TOKEN)   # ディレクトリは通さない
    assert r.returncode == 1 and _alive(data) and "読めません" in r.stderr
    # 猶予は固定 5 秒（旧 SHERPA_NUKE_COUNTDOWN_SEC=0 は効かない・待ちは偽 sleep で潰している）
    r = _run(app, env, lines=["yes", _word()])
    assert not _alive(data) and (tmp_path / "sleep.log").read_text().count("x") == 5, r.stdout
    # ホスト名が取れないときは乱数 6 桁の確認コードを打たせる（固定語にしない）
    for key in data:
        (data[key]).mkdir(exist_ok=True)
        (data[key] / "x.txt").write_text("x")
    fake = pathlib.Path(env["PATH"].split(":")[0])
    (fake / "hostname").write_text("#!/bin/sh\nexit 1\n")
    (fake / "uname").write_text("#!/bin/sh\n[ \"$1\" = -n ] && exit 1\nexec /usr/bin/uname \"$@\"\n")
    for n in ("hostname", "uname"):
        (fake / n).chmod(0o755)
    import re
    rc, out = _interactive(app, env, "yes", lambda o: re.search(r"確認コード: (\d{6})", o).group(1))
    assert "確認コード:" in out and re.search(r"確認コード: \d{6}", out) and not _alive(data), out
    rc, out = _interactive(app, env, "yes", lambda o: "sherpa")   # 固定語では通らない
    assert "名前が一致しません" in out
