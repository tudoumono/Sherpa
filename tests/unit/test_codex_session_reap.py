"""`sherpa.providers.codex.provider._kill_session`（孤児プロセスの session 単位回収）の実害再現。

背景: `subprocess.Popen(argv, start_new_session=True)` で起動した codex 本体が、内部で
`setpgid(0,0)` により別プロセスグループへ移った子を残したまま（本体だけが）先に終了すると、
`_killpg`（プロセスグループ宛）はもうその子に届かない。子は新しい PID 名前空間の init として
振る舞うためシグナルハンドラも無く SIGTERM も効かない。一方 `start_new_session=True` が作った
session だけは setpgid の影響を受けず残るため、session 番号でだけは串刺しに捕捉できる
（`_kill_session` のコメント参照）。呼び出し側（`_attempt` の finally）は、セッションリーダーを
`wait()` で回収する**前**に `_kill_session` を呼ぶ契約になっている——回収するまで pid はゾンビと
してカーネルに予約され続け、同じ番号で新しいセッションが作られないため、sid の再利用そのものが
起きない。本テストはモックを使わず、実プロセスと実 `/proc` でこの回収が実際に効くこと、無関係な
別セッションのプロセスには触らないこと、そしてこの契約どおり「リーダーを回収する前に
`_kill_session` を呼んでいる」状態を実際に作れることを確かめる。
"""
from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading
import time

# 同じファミリーのテスト（test_codex_kill_timeout.py 等）と同じ流儀（setdefault のみ）。
os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa.providers.codex import provider as PV  # noqa: E402


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


_LEADER_SCRIPT = (
    "import os, subprocess, sys\n"
    "child = subprocess.Popen(\n"
    "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
    "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
    "    preexec_fn=lambda: os.setpgid(0, 0))\n"
    "print(child.pid, flush=True)\n"
    "os._exit(0)\n"   # child を wait() せず即終了＝孤児化させる（実害の再現そのもの）
)

# 別プロセスグループへ移った子が stdout を継承したまま残るケース: 子の Popen 呼び出しで
# stdout を指定しない＝親（このリーダー）の stdout（テスト側から見た pipe の書き込み端）を
# そのまま引き継ぐ。リーダー自身が終了して自分の分の fd を閉じても、子がもう1つの書き込み端を
# 握ったままだと pipe は閉じない。
_PIPE_LEADER_SCRIPT = (
    "import os, subprocess, sys\n"
    "child = subprocess.Popen(\n"
    "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
    "    stderr=subprocess.DEVNULL, preexec_fn=lambda: os.setpgid(0, 0))\n"
    "print(child.pid, flush=True)\n"
    "os._exit(0)\n"
)


def test_kill_session_reaps_orphan_but_spares_unrelated_session(monkeypatch):
    """session leader（start_new_session=True）が、別プロセスグループへ移った子を残して
    先に終了した状態を作り、`PV._kill_session(sid)` を呼ぶとその子だけが消えることを確かめる。
    同じセッションに属さない別プロセス（このテストが別に起動したもの）は消えないことも確かめる。
    `_attempt` の finally と同じ順序（リーダーを `wait()` で回収する**前**に `_kill_session` を
    呼ぶ）を踏襲し、その時点でリーダーがまだ回収されていない（＝sid の pid がまだこのテストに
    予約されたまま＝再利用され得ない）ことも、reap せずに終了だけを確認する `os.waitid(WNOWAIT)`
    で直接確かめる（`_kill_session` を呼んだ後で初めてこのテストが `leader.wait()` する）。
    続けて、別プロセスグループへ移った子が stdout（pipe）を継承したまま残るケースで
    `_spawn_stop_watcher`（stop_event=None＝自然終了の検知経路）がリーダーの終了を検知して
    その子を回収し、pipe を閉じさせて読み取りが時間の上限内に EOF で終わることも確かめる。
    最後に、リーダーを先に reap 済みにしてから `_spawn_stop_watcher` を呼んでも（waitid が
    ECHILD になる経路）`_kill_session` が呼ばれないこと（sid の再利用に触れないこと）を、
    `_kill_session` 自体を振る舞いを変えずに記録するラッパで確かめる。"""
    leader = subprocess.Popen(
        [sys.executable, "-u", "-c", _LEADER_SCRIPT],
        start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    orphan_pid = None
    bystander = None
    pipe_leader = None
    pipe_child_pid = None
    try:
        line = leader.stdout.readline()
        assert line.strip(), "セッションリーダーが子の pid を出力しなかった（テスト前提が崩れている）"
        orphan_pid = int(line.strip())
        sid = leader.pid

        assert _wait_until(lambda: _alive(orphan_pid)), (
            "孤児プロセスが起動した形跡が無い（テスト前提が崩れている）")

        # リーダー自身がまだ回収されていない（reap 前）ことを、reap せずに確かめる
        # （`os.waitid(..., WNOWAIT)` は終了を検知しても zombie を消費しない＝`_kill_session`
        # 実装内部の peek と同じ手順）。ChildProcessError（ECHILD）になれば既に回収済み。
        def _exited_unreaped(pid: int) -> bool:
            try:
                return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            except ChildProcessError:
                return False
        assert _wait_until(lambda: _exited_unreaped(leader.pid)), (
            "セッションリーダーが終了した形跡が無い（テスト前提が崩れている）")
        assert _alive(leader.pid), (
            "前提が崩れている: _kill_session を呼ぶ前にリーダーの pid が既に無くなっている"
            "（reap 前は zombie として残り続けるはず）")

        # 前提の裏付け: 孤児は setpgid(0,0) で自分自身の pid を新しい pgid にしている＝
        # 元のプロセスグループ（＝sid。start_new_session の性質上リーダー自身の pgid でも
        # ある）とは別グループに属する＝_killpg 方式（pgid 宛）ではこの孤児に届かないことの
        # 確認（session でしか捕まえられないことの根拠。リーダーがまだ zombie として pgid=sid
        # に残っているためこの時点で `killpg(sid, 0)` 自体は成功してしまう＝孤児の pgid を
        # 直接見る）。
        assert os.getpgid(orphan_pid) != sid, (
            "前提が崩れている: 孤児が元のプロセスグループ（setpgid 前）のまま"
            "＝別グループへ移っていない")

        # 無関係プロセス（このテストプロセス自身の session＝sid とは無関係）。
        # _kill_session が sid 以外へ絶対に触らないことの対照群。直接の子なので、万一
        # SIGKILL されてもこのテストが reap するまでゾンビとして残る＝`os.kill(pid, 0)` は
        # 「殺された直後」でも成功してしまい偽の合格になる。`Popen.poll()`（waitpid(WNOHANG)）
        # で確かめる。
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        assert bystander.poll() is None, "対照群プロセスが起動していない（テスト前提が崩れている）"

        # `_kill_session` はリーダーがまだ未回収（zombie 化はしていても reap 前）の状態で呼ぶ
        # ——このテストはまだ `leader.wait()` を呼んでいない。
        PV._kill_session(sid)

        assert _wait_until(lambda: not _alive(orphan_pid)), (
            f"_kill_session が同一セッションの孤児(pid={orphan_pid})を回収できていない")
        assert bystander.poll() is None, (
            "_kill_session が無関係な別セッションのプロセスまで殺してしまった（過剰殺傷）")

        # `_attempt` の finally と同じ順序: `_kill_session` の後でようやく reap する。
        leader.wait(timeout=5)

        # 別プロセスグループへ移った子が stdout（pipe）を継承したまま残るケース:
        # `_spawn_stop_watcher` が（stop_event=None＝自然終了の検知経路でも）リーダーの終了を
        # 検知したら `_kill_session` を呼んで pipe を閉じさせ、`for line in proc.stdout` 相当の
        # 読み取りが時間の上限内に EOF で終わることを確かめる（上限を超えたら失敗）。
        pipe_leader = subprocess.Popen(
            [sys.executable, "-u", "-c", _PIPE_LEADER_SCRIPT],
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        pline = pipe_leader.stdout.readline()
        assert pline.strip(), "pipe リーダーが子の pid を出力しなかった（テスト前提が崩れている）"
        pipe_child_pid = int(pline.strip())
        assert _wait_until(lambda: _alive(pipe_child_pid)), (
            "pipe を継承する子が起動した形跡が無い（テスト前提が崩れている）")

        PV._spawn_stop_watcher(pipe_leader, None, threading.Lock(), {"done": False})

        readable, _, _ = select.select([pipe_leader.stdout], [], [], 5.0)
        assert readable, (
            "stdout の読み取りが時間の上限内に EOF にならない（別プロセスグループの子が pipe を"
            "握ったままの疑い＝監視スレッドの回収が効いていない）")
        remaining = pipe_leader.stdout.read()
        assert remaining == "", f"EOF のはずが本文が返ってきた: {remaining!r}"
        assert _wait_until(lambda: not _alive(pipe_child_pid)), (
            f"監視スレッドが pipe を握っていた子(pid={pipe_child_pid})を回収できていない")
        pipe_leader.wait(timeout=5)

        # finally が既に reap した後（waitid が ECHILD になる状態）で監視の判定が走っても
        # `_kill_session` を呼ばないことを確かめる: sid は reap 後に再利用され得るため、
        # ECHILD の経路では触れてはいけない（`_kill_session` 自体を、振る舞いを変えずに
        # 記録するだけのラッパで包んで観測する）。
        _kill_session_calls = []
        _real_kill_session = PV._kill_session

        def _recording_kill_session(sid):
            _kill_session_calls.append(sid)
            return _real_kill_session(sid)

        monkeypatch.setattr(PV, "_kill_session", _recording_kill_session)
        echild_leader = subprocess.Popen(
            [sys.executable, "-c", "pass"], start_new_session=True,
        )
        echild_leader.wait(timeout=5)   # 完全に reap 済み（以後 waitid はこの pid に ECHILD を返す）

        PV._spawn_stop_watcher(echild_leader, None, threading.Lock(), {"done": False})
        time.sleep(0.2)   # 監視スレッドが最低1回はループを回るのに十分な猶予
        assert _kill_session_calls == [], (
            f"reap 済み（ECHILD）のリーダーに対して _kill_session が呼ばれてしまった: "
            f"{_kill_session_calls!r}")
    finally:
        if bystander is not None:
            try:
                bystander.kill()
            except Exception:
                pass
            try:
                bystander.wait(timeout=5)
            except Exception:
                pass
        if leader.poll() is None:
            try:
                leader.kill()
            except Exception:
                pass
            try:
                leader.wait(timeout=5)
            except Exception:
                pass
        # orphan_pid はこのテストプロセスの子ではない（reparent 済み）ため wait() できない。
        # _kill_session が失敗した場合の後始末だけ行う。
        if orphan_pid is not None and _alive(orphan_pid):
            try:
                os.kill(orphan_pid, signal.SIGKILL)
            except Exception:
                pass
        if pipe_leader is not None and pipe_leader.poll() is None:
            try:
                pipe_leader.kill()
            except Exception:
                pass
            try:
                pipe_leader.wait(timeout=5)
            except Exception:
                pass
        # pipe_child_pid も reparent 済みのため wait() できない。監視スレッドの回収が
        # 失敗した場合の後始末だけ行う。
        if pipe_child_pid is not None and _alive(pipe_child_pid):
            try:
                os.kill(pipe_child_pid, signal.SIGKILL)
            except Exception:
                pass
