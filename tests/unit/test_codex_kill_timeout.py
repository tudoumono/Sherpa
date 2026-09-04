"""フェーズ7 S5-3: Codex kill/timeout（偽 codex 実行ファイル方式・実プロセス管理の検証）。

`CodexProvider._run_authoring`（sherpa/providers/codex/provider.py）が起動する Codex CLI サブ
プロセスの実際のライフサイクル管理を、tmp に置いた偽の `codex` 実行ファイル（bash スクリプト）を
差し込んで検証する。既存 `test_codex_workspace_authoring.py` は「ソース検査」方式
（`inspect.getsource` で argv/env の形を確認するだけ）で「実 codex CLI 起動は対象外」と明言して
いるが、本ファイルはその明言されたギャップ（kill/timeout の実プロセス管理）を埋める。

検証する3経路（いずれも sherpa/ 本体は無改修＝偽実行ファイルを PATH 経由で差し込むだけ）:
  (a) SHERPA_CODEX_TIMEOUT を極小にして threading.Timer → `_killpg` が実際にプロセス「群」
      （偽 codex 本体＋その子）を殺すこと。殺した後も fail-open で `_result` を返す（クラッシュしない）。
  (b) ctx.stop_event セット → `_spawn_stop_watcher` が同じ `_killpg` でプロセス群を殺すこと。
  (c) generator の途中 close（クライアント切断相当）→ `_run_authoring` の finally
      （`killer.cancel()` → `_killpg` → `proc.wait(5)` → `shutil.rmtree(codex_home)`）で
      プロセス残骸・CODEX_HOME 残骸がゼロになること。

「codex コマンド解決」の差し替え点（sherpa/ 本体は無改修・実装を読んで特定）:
  起動ゲート `shutil.which("codex")` と `subprocess.Popen(argv, env=popen_env, ...)` の
  bare "codex" 探索は、いずれも Popen へ渡す env の PATH に依存する。sandbox 既定 ON では
  `sherpa/providers/codex/sandbox.py::_codex_clean_env` が `os.environ.get("PATH", ...)` を
  **呼出時点**で読んで popen_env["PATH"] に詰める（`subprocess.Popen` は bare コマンド名を
  `env` 引数の PATH で探す＝`os.get_exec_path(env)`）。そのため `monkeypatch.setenv("PATH", ...)`
  で先頭に偽スクリプトのディレクトリを足すだけで、起動ゲートと実際の解決の両方を同時に差し替えられる
  （sandbox OFF でも `_codex_clean_env` を経由しない経路は os.environ をそのまま/ほぼ継承するため
  同じ monkeypatch で効く）。

偽スクリプトへ渡すパラメータ（sentinel ファイルの絶対パス）は env 経由ではなくスクリプト本文に
直接埋め込む（sandbox 既定 ON では `_codex_clean_env` が PATH/HOME/CODEX_HOME/LANG/TMPDIR だけの
最小 env を新規に組み立てるため、カスタム env var は子プロセスへ渡らない）。

`agents._gather` は monkeypatch しない（本ファイルは gather 後の Popen 管理そのものを検証対象と
するため、実 `_gather`（ctx.route/ctx.dispatch は固定ラムダ＝DB 不要）をそのまま使う）。
"""
from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

# test_agents_author.py と同じ流儀（setdefault のみ・モジュールレベル直書きは pytest 一括収集時に
# プロセス全体へ漏れるため禁止・test_codex_workspace_authoring.py の教訓）。
os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import agents as A  # noqa: E402

# RV LOW（2026-07-14 フェーズ7 1巡目）: 偽 codex の自然終了は join(timeout=20) より十分長くする。
# 20秒＝join と同値だと、kill 経路が壊れた時に自然終了と競合して判定が揺れる（false-pass の温床）。
# 120秒なら kill が効かない場合 join(20) が確実に is_alive を検出する（daemon スレッドなので残っても
# テストプロセス終了で回収される・偽 codex 自体は teardown の PATH 破棄後に自然死）。
_SLEEP_SECONDS = 120


def _ctx(uid: str, stop_event=None) -> "A.Ctx":
    """DB 不要な最小 Ctx（route/dispatch を固定ラムダにし、_gather の実処理だけ本物を通す）。"""
    return A.Ctx(
        message="偽 codex kill/timeout テスト",
        world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True,
        uid=uid,
        stop_event=stop_event,
    )


def _write_fake_codex(bin_dir: Path, sentinel_dir: Path, sleep_seconds: int = _SLEEP_SECONDS) -> None:
    """`codex exec --json ...` を模す bash スクリプトを bin_dir/codex に作る。

    起動直後に (1) 自分の PID、(2) バックグラウンド子（`_killpg` がプロセス**グループ**ごと
    殺すことの実証用）の PID を sentinel_dir 配下のファイルへ書き、(3) command_execution の
    item.completed、(4) agent_message の item.completed を1行ずつ JSON で出してから長時間 sleep
    する（自然終了させず、必ず timeout/stop_event/close のいずれかで kill される前提）。
    """
    script = bin_dir / "codex"
    pid_file = sentinel_dir / "codex.pid"
    child_pid_file = sentinel_dir / "child.pid"
    script.write_text(
        "#!/bin/bash\n"
        f'echo $$ > "{pid_file}"\n'
        f"sleep {sleep_seconds} &\n"
        f'echo $! > "{child_pid_file}"\n'
        "echo '{\"type\":\"item.completed\",\"item\":{\"id\":\"c1\",\"type\":\"command_execution\","
        "\"command\":\"grep -r foo bar\",\"status\":\"completed\",\"exit_code\":0}}'\n"
        "echo '{\"type\":\"item.completed\",\"item\":{\"id\":\"1\",\"type\":\"agent_message\","
        "\"text\":\"処理を開始しました。\"}}'\n"
        f"sleep {sleep_seconds}\n"
    )
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_for_file(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.02)
    return False


def _read_pid(path: Path) -> int:
    return int(path.read_text().strip())


def _setup(tmp_path: Path, monkeypatch, users_dirname: str = "users") -> tuple[Path, Path]:
    """PATH に偽 codex を挿し込み、SHERPA_USERS_DIR を隔離する共通セットアップ。戻り値: (bin_dir, sentinel_dir)。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sentinel_dir = tmp_path / "sentinel"
    sentinel_dir.mkdir()
    _write_fake_codex(bin_dir, sentinel_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / users_dirname))
    return bin_dir, sentinel_dir


# ===== (a) SHERPA_CODEX_TIMEOUT 極小 → Timer → _killpg が実際にプロセス群を殺す =====

def test_timeout_timer_kills_process_group_and_fails_open(tmp_path, monkeypatch):
    _bin_dir, sentinel_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("SHERPA_CODEX_TIMEOUT", "1.0")   # 極小（既定180秒に対して）だが起動猶予は確保

    prov = A.CodexProvider()
    ctx = _ctx(uid="killtimeout-u1")

    events: list = []

    def _drive():
        for ev in prov.run(ctx):
            events.append(ev)

    th = threading.Thread(target=_drive, daemon=True)
    th.start()
    th.join(timeout=20)
    assert not th.is_alive(), "timeout 経路が想定時間内に完走しない（Timer→_killpg が効いていない疑い）"

    pid_file = sentinel_dir / "codex.pid"
    child_pid_file = sentinel_dir / "child.pid"
    assert pid_file.exists() and pid_file.read_text().strip(), "偽 codex プロセスが起動した形跡が無い（テスト前提が崩れている）"
    assert child_pid_file.exists() and child_pid_file.read_text().strip(), "偽 codex の子プロセスが起動した形跡が無い"
    pid = _read_pid(pid_file)
    child_pid = _read_pid(child_pid_file)

    # _killpg はプロセス「グループ」ごと殺す（MCP subprocess 等の子まで確実に殺す設計）。
    assert not _pid_alive(pid), f"timeout 後も偽 codex 本体(pid={pid})が生きている（_killpg が効いていない）"
    assert not _pid_alive(child_pid), f"timeout 後もプロセスグループ内の子(pid={child_pid})が生きている（グループ kill が効いていない）"

    # fail-open: 落ちずに _result を返す（部分的に受け取った agent_message からの見出しでもよい）。
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(results) == 1, f"timeout 後に _result が出ていない（fail-open 経路が壊れている）events={events!r}"
    assert results[0]["env"].get("headline"), "fail-open の headline が空"


# ===== (b) stop_event セット → _spawn_stop_watcher による kill =====

def test_stop_event_triggers_watcher_kill(tmp_path, monkeypatch):
    _bin_dir, sentinel_dir = _setup(tmp_path, monkeypatch, users_dirname="users_stop")
    # Timer が先に発火して stop_event 経路の検証を汚染しないよう、timeout は十分大きくしておく。
    monkeypatch.setenv("SHERPA_CODEX_TIMEOUT", "120")

    prov = A.CodexProvider()
    stop_event = threading.Event()
    ctx = _ctx(uid="killstop-u1", stop_event=stop_event)

    events: list = []

    def _drive():
        for ev in prov.run(ctx):
            events.append(ev)

    th = threading.Thread(target=_drive, daemon=True)
    th.start()

    pid_file = sentinel_dir / "codex.pid"
    child_pid_file = sentinel_dir / "child.pid"
    assert _wait_for_file(pid_file, timeout=10), "偽 codex プロセスが起動した形跡が無い（テスト前提が崩れている）"
    assert _wait_for_file(child_pid_file, timeout=10), "偽 codex の子プロセスが起動した形跡が無い"
    pid = _read_pid(pid_file)
    child_pid = _read_pid(child_pid_file)

    stop_event.set()   # UI 側の /chat/stream/stop 相当（_spawn_stop_watcher が 0.3秒間隔で検知）

    th.join(timeout=20)
    assert not th.is_alive(), "stop_event 経路が想定時間内に完走しない（watcher→_killpg が効いていない疑い）"

    assert not _pid_alive(pid), f"stop_event 後も偽 codex 本体(pid={pid})が生きている（watcher kill が効いていない）"
    assert not _pid_alive(child_pid), f"stop_event 後もプロセスグループ内の子(pid={child_pid})が生きている"

    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(results) == 1, f"stop_event 後に _result が出ていない（fail-open 経路が壊れている）events={events!r}"
    assert results[0]["env"].get("headline"), "fail-open の headline が空"


# ===== (c) generator close（クライアント切断相当）→ finally でプロセス/CODEX_HOME 残骸ゼロ =====

def test_generator_close_kills_process_and_removes_codex_home(tmp_path, monkeypatch):
    _bin_dir, sentinel_dir = _setup(tmp_path, monkeypatch, users_dirname="users_close")
    monkeypatch.setenv("SHERPA_CODEX_TIMEOUT", "120")   # Timer が先に発火しないよう十分大きく

    prov = A.CodexProvider()
    uid = "killclose-u1"
    ctx = _ctx(uid=uid)

    gen = prov.run(ctx)
    seen: list = []
    # 実 _gather の node 群 → "codex が調べる"(active) → 偽 codex の command_execution node（"cx-c1"）
    # まで安全に（yield 点で）進める。command_execution の yield 直後は for ループの次イテレーション
    # 直前で停止しており、そこで close() すれば「実行中の generator を別スレッドから close する」
    # という未定義動作を踏まずに済む（同スレッド・yield 点での close＝安全な手順）。
    for _ in range(20):
        ev = next(gen)
        seen.append(ev)
        if isinstance(ev, dict) and str(ev.get("id", "")).startswith("cx-"):
            break
    else:
        raise AssertionError(f"command_execution node（cx-*）に到達しなかった。seen={seen!r}")

    pid_file = sentinel_dir / "codex.pid"
    child_pid_file = sentinel_dir / "child.pid"
    assert pid_file.exists() and pid_file.read_text().strip(), "偽 codex プロセスが起動した形跡が無い（テスト前提が崩れている）"
    assert child_pid_file.exists() and child_pid_file.read_text().strip(), "偽 codex の子プロセスが起動した形跡が無い"
    pid = _read_pid(pid_file)
    child_pid = _read_pid(child_pid_file)
    assert _pid_alive(pid), "close() 前提: 偽 codex 本体がまだ生きていること"
    assert _pid_alive(child_pid), "close() 前提: 偽 codex の子がまだ生きていること"

    users_dir = Path(os.environ["SHERPA_USERS_DIR"]).resolve()
    ws_dir = users_dir / uid / "workspace"
    codex_homes_before = list(ws_dir.glob(".codexhome-*"))
    assert len(codex_homes_before) == 1, (
        f"per-request CODEX_HOME が想定どおり1個作られていない: {codex_homes_before!r}"
    )
    codex_home = codex_homes_before[0]

    gen.close()   # クライアント切断相当（finally: killer.cancel→_killpg→proc.wait(5)→rmtree(codex_home)）

    assert not _pid_alive(pid), f"close() 後も偽 codex 本体(pid={pid})が生きている（finally の _killpg が効いていない）"
    assert not _pid_alive(child_pid), f"close() 後もプロセスグループ内の子(pid={child_pid})が生きている"
    assert not codex_home.exists(), f"close() 後も CODEX_HOME が残っている（finally の rmtree が効いていない）: {codex_home}"
