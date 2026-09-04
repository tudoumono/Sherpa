"""TEST-3（ゲートの真の並走化）の回帰テスト。

実サービス（Postgres/Neo4j/ES）は使わず、subprocess と一時 flock だけで以下を検証する:
- `tests/_world_setup.py::TEST_WORLD_ID` の env 既定/上書き/接頭辞強制、および
  `ensure_v1()` の実 world 保護（base registry 照会・None 時 fail-closed）。
- `tests/_world_registry.py` のレーン別登録・drain 分離。
- `scripts/gate-lane.sh`／`scripts/gate-integration.sh` の `--only` 境界
  （許可 prefix・`..` によるディレクトリ迂回の拒否）。
- 同名レーンの cross-entry 排他（`/tmp/sherpa-gate-<lane>.lock` を lane/integration が共有）。
- `scripts/lib/gate_common.sh::gate_acquire_all_or_none` の部分確保 rollback。
- 外部 TERM を受けたときのテスト子プロセス停止・ロック解放・ログ保全（`gate_run_group`／
  `gate_handle_signal`）。
- `sherpa/world_admin_service.py` の `pytest-` namespace 予約（実登録の authoritative 境界）。

`--only` 境界テストは worktree/venv より先に検証が走ることを前提に、存在しない worktree
（`/nonexistent-worktree`）を渡して DB・実サービスに触れずに完走する。ロック系のテストは
`fcntl.flock()` をテストプロセス自身で使い、外部プロセスの起動を待つ固定 sleep に頼らない
（確保完了は `fcntl.flock()` の復帰そのものが保証する）。
"""
from __future__ import annotations

import fcntl
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = sys.executable


def _hold_flock(path: pathlib.Path) -> int:
    """指定パスをブロッキング flock（fcntl・排他）で確保し、呼び出し元プロセス自身が保持者になる。
    戻り値の fd を `os.close()` するまで保持し続ける——`fcntl.flock()` の復帰そのものが「今、
    確保できた」ことの証明になるため、外部プロセスを起動して固定 sleep で待つ方式より確実。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


# ===== SHERPA_TEST_WORLD_ID の既定/上書き/接頭辞強制 ============================================

def _world_id_subprocess(env_overrides: dict) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("SHERPA_TEST_WORLD_ID", None)
    env.update(env_overrides)
    code = "import sys; sys.path.insert(0, 'tests'); import _world_setup as ws; print(ws.TEST_WORLD_ID)"
    return subprocess.run([PY, "-c", code], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=30)


def test_world_id_defaults_to_pytest_v1_without_env():
    r = _world_id_subprocess({})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "pytest-v1"


def test_world_id_uses_env_override_with_valid_prefix():
    r = _world_id_subprocess({"SHERPA_TEST_WORLD_ID": "pytest-mylane"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "pytest-mylane"


def test_world_id_rejects_non_prefixed_env_value():
    r = _world_id_subprocess({"SHERPA_TEST_WORLD_ID": "mylane"})
    assert r.returncode != 0
    assert "pytest-" in r.stderr


# ===== ensure_v1() の実 world 保護（base registry 照会・None 時 fail-closed） ====================

def test_ensure_v1_fails_closed_when_base_registry_query_returns_none(tmp_path):
    """base registry 照会が None（DSN 未設定・接続不可・SELECT 失敗のいずれか）を返したら、
    「確認できない」を「安全」として進めず即 RuntimeError にし、load_world() を一切呼ばない。

    スパイは original の load_world() を**呼ばない**——「到達したかどうか」だけを見たいので、
    到達した時点で専用 sentinel 例外を投げて即座に止める。これにより、もし fail-closed が
    壊れて到達してしまっても、Neo4j への DETACH DELETE・PG 台帳更新（`ensure_v1()` 末尾の
    `store.replace_documents`）を含む実書き込みが一切走らない。"""
    script = tmp_path / "check_ensure_v1_failclosed.py"
    script.write_text(
        "import sys\n"
        "sys.path.insert(0, 'tests')\n"
        "import _world_setup as ws\n"
        "import _world_registry as wr\n"
        "import sherpa.ingest.world_neo4j as wn\n"
        "class _ReachedLoadWorld(Exception):\n"
        "    pass\n"
        "def _spy(*a, **kw):\n"
        "    raise _ReachedLoadWorld()\n"
        "wn.load_world = _spy\n"
        "wr._query_real_registered_world_ids = lambda: None\n"
        "try:\n"
        "    ws.ensure_v1()\n"
        "    print('NO_EXCEPTION')\n"
        "except _ReachedLoadWorld:\n"
        "    print('REACHED_LOAD_WORLD')\n"
        "except RuntimeError:\n"
        "    print('RAISED_RUNTIME_ERROR')\n"
    )
    r = subprocess.run([PY, str(script)], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "RAISED_RUNTIME_ERROR" in r.stdout, r.stdout + r.stderr
    assert "REACHED_LOAD_WORLD" not in r.stdout, r.stdout + r.stderr


# ===== レーン別 world 登録簿（tests/_world_registry.py）の分離 ==================================

def test_register_test_world_drain_is_isolated_per_call(monkeypatch):
    sys.path.insert(0, str(ROOT / "tests"))
    import _world_registry as wr
    monkeypatch.setattr(wr, "_query_real_registered_world_ids", lambda: None)
    wr._REGISTRY.clear()
    wr.register_test_world("pytest-lanea")
    wr.register_test_world("pytest-laneb")
    assert wr.drain_registered_worlds() == ["pytest-lanea", "pytest-laneb"]
    # 前のレーンの残骸を次の drain が引き継がない（レーン別 cleanup 分離の前提）。
    assert wr.drain_registered_worlds() == []


def test_register_test_world_still_protects_denylist_and_real_registry(monkeypatch):
    sys.path.insert(0, str(ROOT / "tests"))
    import _world_registry as wr
    monkeypatch.setattr(wr, "_query_real_registered_world_ids", lambda: {"pytest-reallane"})
    wr._REGISTRY.clear()
    wr.register_test_world("test")              # 静的 denylist
    wr.register_test_world("pytest-reallane")    # base registry 実在（動的照会）
    wr.register_test_world("pytest-safelane")    # 保護対象外
    assert wr.drain_registered_worlds() == ["pytest-safelane"]


# ===== --only の境界（許可 prefix・`..` によるディレクトリ迂回の拒否） ==========================

def _run_only_check(script: str, head_args: list, only_args: list) -> subprocess.CompletedProcess:
    cmd = ["bash", str(ROOT / "scripts" / script), *head_args, "--only", *only_args]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=15)


def test_gate_lane_only_rejects_integration_path():
    r = _run_only_check("gate-lane.sh", ["/nonexistent-worktree", "fakelane"],
                        ["tests/integration/test_x.py"])
    assert r.returncode == 2
    assert "許可" in r.stderr


def test_gate_lane_only_rejects_bare_tests_root():
    r = _run_only_check("gate-lane.sh", ["/nonexistent-worktree", "fakelane"], ["tests"])
    assert r.returncode == 2
    assert "許可" in r.stderr


def test_gate_lane_only_accepts_api_prefix_and_proceeds_past_boundary_check():
    r = _run_only_check("gate-lane.sh", ["/nonexistent-worktree", "fakelane"],
                        ["tests/api/test_x.py"])
    assert r.returncode == 2
    # 境界チェックは通過し、別の理由（worktree 不在）で失敗している証拠。
    assert "worktree が見つかりません" in r.stderr
    assert "許可" not in r.stderr


def test_gate_lane_only_rejects_dotdot_escape_into_integration():
    """`tests/api/../integration/x.py` は文字列としては許可 prefix (`tests/api`) で始まるが、
    `..` で実際には tests/integration を指す——prefix 判定より前に `..` そのものを拒否する。"""
    r = _run_only_check("gate-lane.sh", ["/nonexistent-worktree", "fakelane"],
                        ["tests/api/../integration/test_x.py"])
    assert r.returncode == 2
    assert "迂回" in r.stderr


def test_gate_integration_only_rejects_unit_path():
    r = _run_only_check("gate-integration.sh", ["/nonexistent-worktree", "--lane", "fakelane"],
                        ["tests/unit/test_x.py"])
    assert r.returncode == 2
    assert "tests/integration" in r.stderr


def test_gate_integration_only_accepts_integration_prefix_and_proceeds_past_boundary_check():
    r = _run_only_check("gate-integration.sh", ["/nonexistent-worktree", "--lane", "fakelane"],
                        ["tests/integration/test_x.py"])
    assert r.returncode == 2
    assert "worktree が見つかりません" in r.stderr


def test_gate_integration_only_rejects_dotdot_escape_into_unit():
    """逆方向（integration→lane 領域への迂回）も同様に拒否する。"""
    r = _run_only_check("gate-integration.sh", ["/nonexistent-worktree", "--lane", "fakelane"],
                        ["tests/integration/../unit/test_x.py"])
    assert r.returncode == 2
    assert "迂回" in r.stderr


# ===== 同名レーンの cross-entry 排他（gate-lane.sh と gate-integration.sh が共有） ================

def test_cross_entry_same_lane_lock_blocks_integration_while_held():
    """`/tmp/sherpa-gate-<lane>.lock` を外部で保持したまま gate-integration.sh --lane <同名> を
    起動すると、レーン別ロック待ちでブロックし続ける（worktree/venv は本物・実サービスには
    到達しない——ロック取得は DB reset より前で止まるため）。ブロック箇所が「レーン別ロック待ち」
    であって、その先の「専用ロック待ち」にはまだ到達していないことも区別して確認する。"""
    lane = f"rvtest{os.getpid()}"
    lockfile = pathlib.Path(f"/tmp/sherpa-gate-{lane}.lock")
    fd = _hold_flock(lockfile)
    try:
        r = subprocess.run(
            ["timeout", "3", "bash", str(ROOT / "scripts" / "gate-integration.sh"),
             str(ROOT), "--lane", lane, "--only", "tests/integration/test_nonexistent_rv.py"],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        assert r.returncode == 124, f"ブロックされず進行してしまった: {r.stdout}{r.stderr}"
        assert "レーン別ロック待ち" in r.stdout, r.stdout
        assert "専用ロック待ち" not in r.stdout, r.stdout
    finally:
        os.close(fd)
        lockfile.unlink(missing_ok=True)


# ===== gate-quiet.sh の部分確保 rollback（gate_acquire_all_or_none） ============================

def test_gate_acquire_all_or_none_rolls_back_partial_acquisition(tmp_path):
    slot1 = tmp_path / "slot1.lock"
    slot2 = tmp_path / "slot2.lock"
    integ = tmp_path / "integ.lock"
    fd = _hold_flock(slot2)
    try:
        script = (
            "set -u\n"
            ". " + str(ROOT / "scripts" / "lib" / "gate_common.sh") + "\n"
            "GATE_LANE_SLOT_LOCKS=(\"" + str(slot1) + "\" \"" + str(slot2) + "\")\n"
            "GATE_INTEGRATION_LOCKFILE=\"" + str(integ) + "\"\n"
            "if gate_acquire_all_or_none; then echo ACQUIRED; else echo ROLLED_BACK; fi\n"
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
        assert r.stdout.strip() == "ROLLED_BACK", r.stdout + r.stderr
        # slot1 は一旦確保されたはずだが、rollback で解放され再ロック可能になっている。
        r2 = subprocess.run(["flock", "-n", str(slot1), "-c", "true"])
        assert r2.returncode == 0, "slot1 が rollback 後も held のまま（部分確保のまま居座っている）"
    finally:
        os.close(fd)


def test_gate_acquire_all_or_none_succeeds_when_all_free(tmp_path):
    slot1 = tmp_path / "slot1.lock"
    slot2 = tmp_path / "slot2.lock"
    integ = tmp_path / "integ.lock"
    script = (
        "set -u\n"
        ". " + str(ROOT / "scripts" / "lib" / "gate_common.sh") + "\n"
        "GATE_LANE_SLOT_LOCKS=(\"" + str(slot1) + "\" \"" + str(slot2) + "\")\n"
        "GATE_INTEGRATION_LOCKFILE=\"" + str(integ) + "\"\n"
        "if gate_acquire_all_or_none; then echo ACQUIRED fds=${#GATE_QUIET_FDS[@]}; "
        "else echo ROLLED_BACK; fi\n"
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert r.stdout.strip() == "ACQUIRED fds=3", r.stdout + r.stderr


# ===== 外部 TERM: テスト子プロセス停止・ロック解放・ログ保全 =====================================

def test_term_signal_stops_running_test_releases_lock_and_preserves_log(tmp_path):
    """gate_run_group/gate_handle_signal の核（launcher 本体が setsid テスト子の直接の親になる・
    子のプロセスグループへの signal 転送・fd 非継承）を、実サービス無しの subprocess で検証する。
    8秒 sleep するダミーテストを起動し、2秒後に外部 TERM を送る——自然完了（`1 passed`）せずに
    数秒以内で死に、確保していた名前付きロックが解放され、かつログに exit 理由が残ることを
    確認する（signal 処理後の要約出力が tee もろとも失われないこと）。"""
    slow_test = tmp_path / "test_slow_rv.py"
    slow_test.write_text("import time\ndef test_slow():\n    time.sleep(8)\n    assert True\n")
    lockfile = tmp_path / "named.lock"
    logfile = tmp_path / "gate.log"
    script = (
        "set -u\n"
        "cd " + str(ROOT) + "\n"
        ". scripts/lib/gate_common.sh\n"
        "GATE_LOG_FILE='" + str(logfile) + "'\n"
        "trap 'gate_handle_signal TERM' TERM\n"
        "gate_acquire_named_lock '" + str(lockfile) + "' || exit 1\n"
        "GATE_HELD_FDS=(\"$GATE_NAMED_LOCK_FD\")\n"
        "PY=" + PY + "\n"
        "GROUP_TIMEOUT=45m\n"
        "run(){ gate_run_group \"$@\"; }\n"
        "run slow " + str(slow_test) + "\n"
    )
    proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        time.sleep(2)
        assert proc.poll() is None, "TERM を送る前に終わってしまった（テスト前提が崩れている）"
        t0 = time.time()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        elapsed = time.time() - t0
        out = proc.stdout.read() if proc.stdout else ""
        assert elapsed < 6, f"停止に時間がかかりすぎている（自然完了8秒に近い＝殺せていない疑い）: {elapsed}s output={out}"
        assert "1 passed" not in out, f"自然完了してしまっている（TERM が効いていない）: {out}"
        r = subprocess.run(["flock", "-n", str(lockfile), "-c", "true"])
        assert r.returncode == 0, "named lock が解放されていない（fd 継承漏れ等の疑い）"
        log_content = logfile.read_text() if logfile.exists() else ""
        assert "exit=" in log_content, f"signal 処理後の要約がログに残っていない: {log_content!r}"
        assert "SCRIPT_EXIT" in log_content, f"SCRIPT_EXIT マーカーがログに残っていない: {log_content!r}"
        # 要約（exit=...）を書き終えてから SCRIPT_EXIT マーカーを書く順序になっている
        # （PID/出力状態のクリアも両方を書き終えた後——順序が乱れると要約が欠けたまま
        # マーカーだけ残る事故になりうる）。
        assert log_content.index("exit=") < log_content.index("SCRIPT_EXIT"), (
            f"exit= より先に SCRIPT_EXIT が出てしまっている（書き込み順序の退行）: {log_content!r}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_term_signal_does_not_let_next_group_start(tmp_path):
    """複数群を順に呼ぶ想定（gate-lane.sh の unit; contract; ...）を最小再現する——1群目を kill
    したら、2群目（マーカーファイル作成の直後）が起動しないことを確認する。launcher が signal
    ハンドラの exit で即座に終了するため、監督者不在のまま次のテストが走り続けることはない。"""
    marker = tmp_path / "second_group_started.marker"
    slow_test = tmp_path / "test_slow_rv2.py"
    slow_test.write_text("import time\ndef test_slow():\n    time.sleep(8)\n    assert True\n")
    script = (
        "set -u\n"
        "cd " + str(ROOT) + "\n"
        ". scripts/lib/gate_common.sh\n"
        "trap 'gate_handle_signal TERM' TERM\n"
        "PY=" + PY + "\n"
        "GROUP_TIMEOUT=45m\n"
        "run(){ gate_run_group \"$@\"; }\n"
        "run first " + str(slow_test) + "\n"
        "touch '" + str(marker) + "'\n"
        "run second " + str(slow_test) + "\n"
    )
    proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        time.sleep(2)
        assert proc.poll() is None, "TERM を送る前に終わってしまった（テスト前提が崩れている）"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        time.sleep(1)   # 万一続いていた場合に marker が書かれる猶予
        assert not marker.exists(), "1群目を殺した後、監督者不在のまま2群目へ進んでしまった"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# ===== sherpa/world_admin_service.py: pytest- namespace の予約 ==================================

def test_register_or_rerun_rejects_pytest_namespace_world_id(monkeypatch, tmp_path):
    """実 world 登録の authoritative 境界（`register_or_rerun`）は、明示・自動生成いずれの
    world_id も `pytest-` 接頭辞なら `worlds.register()` に到達する前に拒否する
    （TEST-3 のレーン別 world 分離が使う env 注入 namespace との衝突防止）。"""
    from sherpa import store, world_admin_service as was
    from sherpa import worlds as _worlds

    root = tmp_path / "somefolder"
    root.mkdir()
    monkeypatch.setattr(store, "world_by_root", lambda r: None)
    monkeypatch.setattr(store, "list_worlds_db", lambda: [])

    def _must_not_call(*a, **kw):
        raise AssertionError("worlds.register に到達してしまった（pytest- namespace の拒否が効いていない）")

    monkeypatch.setattr(_worlds, "register", _must_not_call)

    with pytest.raises(was.WorldAdminValidationError):
        was.register_or_rerun(str(root), world_id="pytest-shouldnotregister")
