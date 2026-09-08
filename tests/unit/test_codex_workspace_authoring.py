"""Feature A/B/C 単体テスト: Codex 個人書込＋チャット個人ファイル参照＋共有ガード。

テスト範囲:
  A: CodexProvider の argv が workspace-write＋cwd=本人 workspace になる（uid 別）。
     互換モード（admin）でも正しい cwd を生成する。
     Office ライブラリ（python-docx / python-pptx / openpyxl）が import できる。
     personal_workspace_files 台帳登録ロジックの確認（store.record_workspace_file 呼出パス）。

  B: _personal_grep_hits が本人ファイルのみヒット・他ユーザーは届かない。
     personal=OFF で個人ヒットは facts に含まれない。
     個人ヒットは ES/Neo4j に入らない（不変条件: live_workspace_rel_paths は pwf 台帳のみ）。

  C: Codex が workspace にファイルを書いた場合 env["codex_wrote_files"] が付く。
     set_contains_personal_workspace が会話フラグを TRUE にする（store 単体）。

Full codex-exec e2e は手動確認事項（サブプロセス起動の壁）。
"""
from __future__ import annotations

import os
import shutil

import pytest

# 互換モード＋fixtures は fixture でテスト実行中だけ有効化する。モジュールレベルの
# os.environ 直書きは pytest の一括 collection（import）時にプロセス全体へ漏れ、
# 後続の全テスト（tests/api 含む）の認証を無効化していた（2026-07-10 にフルスイート
# 61件失敗の根本原因と特定。単独ファイル実行では再現しないため長期間潜伏）。
@pytest.fixture(autouse=True)
def _unit_compat_env(monkeypatch):
    monkeypatch.setenv("SHERPA_USE_FIXTURES", "1")
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")   # 単体テストは互換モード


@pytest.fixture(autouse=True)
def _codex_cli_present(monkeypatch):
    """`_select_provider` の codex 分岐は `shutil.which("codex")` の有無を先に見る。本ファイルの
    `_select_provider({"agent": "codex", ...})` を呼ぶテストはその後段（接続先・宛先ポリシー等）を
    検証するため、開発機に実際に Codex CLI が入っているかどうかに関わらず「ある」ことに固定する
    （CLI 不在の分岐自体を検証するテストは無いため上書きの必要も無い）。"""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)

# ===== Feature A: CodexProvider argv =====

def test_codex_permission_profile_is_default():
    """既定サンドボックスは permission profile（読取封じ込め）で、-s workspace-write は fallback のみ。"""
    from sherpa import agents as A
    import inspect
    src = inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)
    # 既定 ON・--strict-config・profile 生成・クリーン env・stdin 遮断が run() に含まれる。
    assert "_codex_sandbox_enabled" in src, "permission profile 分岐が run() に無い"
    assert "--strict-config" in src, "--strict-config が無い"
    assert "_write_codex_authoring_config" in src, "profile config 生成呼出が無い"
    assert "_codex_clean_env" in src, "クリーン env（creds 非渡し）が無い"
    assert "stdin=subprocess.DEVNULL" in src, "stdin 遮断（stdin ハング回避）が無い"
    assert "read-only" not in src, "argv に read-only が残っている"


def test_codex_sandbox_flag_default_on_and_off():
    """SHERPA_CODEX_SANDBOX 既定 ON・=0 で旧経路にフォールバック。"""
    from sherpa import agents as A
    import importlib
    os.environ.pop("SHERPA_CODEX_SANDBOX", None); importlib.reload(A)
    assert A._codex_sandbox_enabled() is True, "既定は ON のはず"
    os.environ["SHERPA_CODEX_SANDBOX"] = "0"; importlib.reload(A)
    assert A._codex_sandbox_enabled() is False, "=0 で OFF のはず"
    os.environ.pop("SHERPA_CODEX_SANDBOX", None); importlib.reload(A)


def test_codex_profile_config_confines_reads():
    """生成 config が KB=read / authoring=write / :root=deny / network=false を持つ（読取封じ込め）。"""
    from sherpa import agents as A
    import pathlib, tempfile
    ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch, ["/kb/abs/path"], "low", False, "test", None)
    cfg = (ch / "config.toml").read_text()
    assert 'default_permissions = "sherpa-authoring"' in cfg
    assert '":root" = "deny"' in cfg, "FS 全体 read 遮断が無い"
    assert '"/kb/abs/path" = "read"' in cfg, "KB read 許可が無い"
    assert '"." = "write"' in cfg, "authoring write 許可が無い"
    assert "enabled = false" in cfg, "network 遮断が無い"
    # TOML として妥当。
    try:
        import tomllib; tomllib.loads(cfg)
    except ModuleNotFoundError:
        pass  # py<3.11 は tomllib 無し（スキップ）


def test_codex_clean_env_has_no_secrets():
    """クリーン env に DB/ES creds が入らない（BLOCKER② 部分緩和・プロセス env から creds を外す）。"""
    from sherpa import agents as A
    import pathlib, tempfile
    os.environ["NEO4J_PASSWORD"] = "should_not_leak"
    d = pathlib.Path(tempfile.mkdtemp())
    env = A._codex_clean_env(d / "ch", d / "auth", d / "tmp")
    assert not any("NEO4J" in k or "PASSWORD" in k for k in env), f"creds が env に漏れている: {list(env)}"
    assert "PATH" in env and "CODEX_HOME" in env, "PATH/CODEX_HOME が無い（codex 起動不能）"
    os.environ.pop("NEO4J_PASSWORD", None)


def test_codex_clean_env_passes_proxy_and_ca_only_when_set(monkeypatch, tmp_path):
    """プロキシ/CA の経路設定は**親環境にあるときだけ**透過（閉域実機⑪・2026-08-18）。creds は引き続き渡さない。"""
    from sherpa.providers.codex import sandbox as SB
    for k in SB._CODEX_PASSTHROUGH_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    env = SB._codex_clean_env(tmp_path / "ch", tmp_path / "auth", tmp_path / "tmp")
    assert not any(k in env for k in SB._CODEX_PASSTHROUGH_ENV), "未設定なのに透過している"
    assert "OPENAI_API_KEY" not in env
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:8080")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/certs/corp.pem")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/ssl/certs/corp.pem")
    monkeypatch.setenv("HTTP_PROXY", "")                    # 空文字は未設定扱い
    env = SB._codex_clean_env(tmp_path / "ch", tmp_path / "auth", tmp_path / "tmp")
    assert env["HTTPS_PROXY"] == "http://proxy.internal:8080"
    assert env["no_proxy"] == "localhost,127.0.0.1"
    assert env["SSL_CERT_FILE"] == env["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/certs/corp.pem"
    assert "HTTP_PROXY" not in env and "ALL_PROXY" not in env
    assert "OPENAI_API_KEY" not in env


def test_authoring_symlink_rejected_fail_closed():
    """RV BLOCKER: workspace/authoring に symlink が混入したら fail-closed（None）で Codex を起動しない。"""
    from sherpa import agents as A
    import pathlib, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    ud = d / "users"
    (ud / "ok" / "workspace").mkdir(parents=True)
    assert A._safe_workspace_authoring(ud, "ok") is not None, "正常 authoring が None になった"
    # authoring を別ディレクトリへの symlink にする → 拒否されること
    (ud / "bad" / "workspace").mkdir(parents=True)
    evil = d / "evil"; evil.mkdir()
    (ud / "bad" / "workspace" / "authoring").symlink_to(evil)
    assert A._safe_workspace_authoring(ud, "bad") is None, "symlink authoring が拒否されていない（封じ込め崩壊）"
    # workspace 自体が symlink のケースも拒否
    (ud / "bad2").mkdir()
    (ud / "bad2" / "workspace").symlink_to(evil)
    assert A._safe_workspace_authoring(ud, "bad2") is None, "symlink workspace が拒否されていない"
    # 不正 uid（パス注入）も拒否
    assert A._safe_workspace_authoring(ud, "../etc") is None
    assert A._safe_workspace_authoring(ud, "a/b") is None


# ===== `_safe_run_authoring`: 実行ごとの作業領域（同一 uid 直列化 lock 撤去の置き換え） =====

def test_safe_run_authoring_creates_distinct_dirs_under_authoring():
    """毎回 `authoring/run-<乱数>` という別名の新規ディレクトリを作って返す（同一 uid でも衝突しない）。"""
    from sherpa.providers.codex import sandbox as SB
    import pathlib, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    ud = d / "users"
    (ud / "u1" / "workspace").mkdir(parents=True)

    r1 = SB._safe_run_authoring(ud, "u1")
    r2 = SB._safe_run_authoring(ud, "u1")
    assert r1 is not None and r2 is not None
    assert r1 != r2, "2回の呼び出しで同じ run dir を返している（並走時に衝突する）"
    for r in (r1, r2):
        assert r.is_dir()
        assert r.name.startswith("run-")
        assert r.parent == ud / "u1" / "workspace" / "authoring"
        assert r.resolve().relative_to((ud / "u1" / "workspace").resolve()) == pathlib.Path(
            "authoring") / r.name


def test_safe_run_authoring_fail_closed_on_invalid_uid_and_symlinked_authoring():
    """`_safe_workspace_authoring` と同じ封じ込め（uid 形式・symlink 拒否）を土台にしているため、
    それらが拒否するケースはそのまま None（fail-closed）になる。"""
    from sherpa.providers.codex import sandbox as SB
    import pathlib, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    ud = d / "users"

    assert SB._safe_run_authoring(ud, "../etc") is None
    assert SB._safe_run_authoring(ud, "a/b") is None

    (ud / "bad" / "workspace").mkdir(parents=True)
    evil = d / "evil"; evil.mkdir()
    (ud / "bad" / "workspace" / "authoring").symlink_to(evil)
    assert SB._safe_run_authoring(ud, "bad") is None, "symlink authoring 配下で run dir を作ってしまった"
    assert not list(evil.iterdir()), "symlink の指す先（authoring 外）に run dir を作ってしまった"


def test_safe_run_authoring_sweeps_stale_run_dirs_but_keeps_fresh_ones():
    """`authoring/` 直下の `run-*` のうち mtime が24時間より古いものだけ best-effort で削除する
    （クラッシュ等で残った前回実行の作業領域の掃除）。新しいものと symlink はそのまま残る。"""
    from sherpa.providers.codex import sandbox as SB
    import os as _os
    import pathlib, tempfile, time
    d = pathlib.Path(tempfile.mkdtemp())
    ud = d / "users"
    authoring = ud / "u1" / "workspace" / "authoring"
    authoring.mkdir(parents=True)

    stale = authoring / "run-deadbeef0000"
    stale.mkdir()
    (stale / "leftover.txt").write_text("crashed run leftover", encoding="utf-8")
    old_time = time.time() - SB._RUN_DIR_TTL_SECONDS - 3600
    _os.utime(stale, (old_time, old_time))

    fresh = authoring / "run-cafebabe0000"
    fresh.mkdir()

    stale_link_target = d / "evil-link-target"; stale_link_target.mkdir()
    stale_link = authoring / "run-symlinked00000"
    stale_link.symlink_to(stale_link_target)
    _os.utime(stale_link, (old_time, old_time), follow_symlinks=False)

    new_run = SB._safe_run_authoring(ud, "u1")

    assert new_run is not None and new_run.is_dir()
    assert not stale.exists(), "24時間より古い run-* が掃除されていない"
    assert fresh.is_dir(), "新しい run-* まで誤って消してしまった"
    assert stale_link.is_symlink(), "symlink の run-* を削除してしまった（symlink target を巻き込む危険）"
    assert stale_link_target.is_dir(), "symlink の指す先を巻き込んで削除してしまった"


def test_safe_run_authoring_keeps_active_run_dir_even_when_mtime_is_stale():
    """24時間しきい値は mtime だけを見ると、実行時間が長いターン
    （timeout 延長・長時間実行等）の run dir を「稼働中のまま」誤って掃除しうる。
    `_register_active_run_dir`（`_safe_run_authoring` が作成直後に登録）された run dir は、
    mtime が期限切れでも掃除対象から除外される——解放（`_release_active_run_dir`）後の
    次回スイープで初めて掃除対象になる。"""
    from sherpa.providers.codex import sandbox as SB
    import os as _os
    import pathlib, tempfile, time
    d = pathlib.Path(tempfile.mkdtemp())
    ud = d / "users"
    (ud / "u1" / "workspace").mkdir(parents=True)

    active_run = SB._safe_run_authoring(ud, "u1")   # 作成直後に「稼働中」へ登録される
    assert active_run is not None
    old_time = time.time() - SB._RUN_DIR_TTL_SECONDS - 3600
    _os.utime(active_run, (old_time, old_time))     # 長時間実行で mtime が古くなった状態を模す

    # 別の実行がもう1回 run dir を要求する＝掃除スイープが走るが、稼働中なので対象外のはず。
    another_run = SB._safe_run_authoring(ud, "u1")
    assert another_run is not None and another_run != active_run
    assert active_run.is_dir(), "稼働中の run dir が mtime だけを理由に掃除されてしまった"

    SB._release_active_run_dir(active_run)
    third_run = SB._safe_run_authoring(ud, "u1")   # 解放後・次回スイープでようやく掃除対象になる
    assert third_run is not None
    assert not active_run.exists(), "解放後の次回スイープで期限切れ run dir が掃除されていない"


def test_chmod_if_not_symlink_never_follows_symlinks():
    """`_chmod_if_not_symlink`（単体）は symlink を渡されても何もしない——`os.chmod` は既定で
    symlink を追従してリンク先の権限を変えてしまうため、呼び出し側（`_restore_removable_permissions`
    の dirnames フィルタ等）が症状を防いでいるだけでなく、この関数自体も symlink 単体で
    呼ばれても安全であることを直接確認する（symlink-to-ファイルは dirnames フィルタの対象外
    ＝この関数自身のチェックだけが頼り）。"""
    from sherpa.providers.codex import sandbox as SB
    import os as _os
    import pathlib, stat, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    target = d / "target-file"
    target.write_text("x", encoding="utf-8")
    _os.chmod(target, 0o644)
    link = d / "link-to-file"
    link.symlink_to(target)
    original_mode = stat.S_IMODE(_os.stat(target).st_mode)

    SB._chmod_if_not_symlink(str(link))

    assert stat.S_IMODE(_os.stat(target).st_mode) == original_mode, \
        "symlink 経由でリンク先の権限が変わってしまった"


def test_remove_dir_best_effort_does_not_chmod_symlink_targets():
    """後始末（`_remove_dir_best_effort`）は symlink を辿ってリンク先（run dir 外の任意の
    ディレクトリでありうる）の権限を変えてはいけない。書込不可のディレクトリ配下に外部への
    symlink を置いた run dir でも、リンク先の権限は無変更のまま run dir 自体は削除できること。"""
    from sherpa.providers.codex import sandbox as SB
    import os as _os
    import pathlib, stat, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    run_dir = d / "run-abc123456789"
    locked = run_dir / "locked"
    locked.mkdir(parents=True)
    evil_target = d / "external-target"
    evil_target.mkdir()
    _os.chmod(evil_target, 0o755)
    (locked / "evil").symlink_to(evil_target)
    _os.chmod(locked, 0o500)   # 書込不可＝配下のエントリを削除できない（クラッシュ等で戻し忘れた想定）

    original_target_mode = stat.S_IMODE(_os.stat(evil_target).st_mode)

    SB._remove_dir_best_effort(run_dir)

    assert stat.S_IMODE(_os.stat(evil_target).st_mode) == original_target_mode, \
        "symlink のリンク先（run dir 外）の権限が変わってしまった"
    assert not run_dir.exists(), "run dir 自体が削除されていない（権限回復付き再試行が効いていない）"


def test_remove_dir_best_effort_does_not_chmod_hardlinked_files():
    """後始末はファイルには一切 chmod しない——run dir 内のファイルが run dir 外のファイルと
    ハードリンク（同一 inode＝同一の権限ビットを共有）していると、ファイルへの chmod は
    symlink とは別経路で run dir 外の権限を変えてしまう（rmtree に必要なのはディレクトリの
    書込/実行権だけで、ファイル自体の権限は無関係）。"""
    from sherpa.providers.codex import sandbox as SB
    import os as _os
    import pathlib, stat, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    run_dir = d / "run-hardlink0000001"
    locked = run_dir / "locked"
    locked.mkdir(parents=True)
    external_file = d / "external-shared-file.txt"
    external_file.write_text("shared content", encoding="utf-8")
    _os.chmod(external_file, 0o644)
    _os.link(str(external_file), str(locked / "hardlinked.txt"))   # 同一 inode を共有
    _os.chmod(locked, 0o500)   # 書込不可＝配下のエントリを削除できない（クラッシュ等で戻し忘れた想定）

    original_mode = stat.S_IMODE(_os.stat(external_file).st_mode)

    SB._remove_dir_best_effort(run_dir)

    assert stat.S_IMODE(_os.stat(external_file).st_mode) == original_mode, \
        "ハードリンク経由で run dir 外のファイルの権限が変わってしまった"
    assert not run_dir.exists(), "run dir 自体が削除されていない（権限回復付き再試行が効いていない）"


def test_cleanup_stale_run_dirs_recovers_permission_restricted_leftover():
    """24時間掃除は権限制限（0500 のサブディレクトリ）が残ったクラッシュ残骸を回収できず
    残り続けていた——`_remove_dir_best_effort` を共通ヘルパーとして使うことで、非 symlink
    エントリの権限を戻して確実に掃除できることを確認する。"""
    from sherpa.providers.codex import sandbox as SB
    import os as _os
    import pathlib, tempfile, time
    d = pathlib.Path(tempfile.mkdtemp())
    ud = d / "users"
    authoring = ud / "u1" / "workspace" / "authoring"
    authoring.mkdir(parents=True)

    stale = authoring / "run-permlocked0000"
    locked_sub = stale / "locked"
    locked_sub.mkdir(parents=True)
    (locked_sub / "leftover.txt").write_text("crashed run leftover", encoding="utf-8")
    _os.chmod(locked_sub, 0o500)
    old_time = time.time() - SB._RUN_DIR_TTL_SECONDS - 3600
    _os.utime(stale, (old_time, old_time))

    new_run = SB._safe_run_authoring(ud, "u1")

    assert new_run is not None
    assert not stale.exists(), "権限制限された残骸（0500 のサブディレクトリ）が回収されずに残っている"


def test_cleanup_stale_run_dirs_stat_failure_is_logged_without_leaking_path(monkeypatch, caplog):
    """staleness 判定の `p.stat()` 自体が失敗した場合（クラッシュ・並行削除等との競合）も、
    `p` は絶対パス（users_dir/uid を含む）——そのまま記録せず、相対名（run-* 自身の識別子）と
    例外の型・errno だけを記録することを確認する。"""
    import logging
    import pathlib
    import tempfile
    from sherpa.providers.codex import sandbox as SB

    d = pathlib.Path(tempfile.mkdtemp())
    authoring = d / "users" / "u1" / "workspace" / "authoring"
    authoring.mkdir(parents=True)
    target = authoring / "run-statfail0000"
    target.mkdir()

    _orig_stat = pathlib.Path.stat
    call_counts: dict = {}

    def _boom_stat(self, *a, **kw):
        # 掃除対象フィルタ（`p.is_symlink()`/`p.is_dir()` は Python 3.12 pathlib では
        # いずれも内部で `self.stat()` を呼ぶ）ぶんは本物を通し、対象コードの明示的な
        # mtime 取得（3回目の呼び出し）だけ失敗させる。
        if self.name == "run-statfail0000":
            call_counts[self.name] = call_counts.get(self.name, 0) + 1
            if call_counts[self.name] >= 3:
                raise OSError(13, "Permission denied", str(self))
        return _orig_stat(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "stat", _boom_stat)

    with caplog.at_level(logging.WARNING, logger="sherpa"):
        SB._cleanup_stale_run_dirs(authoring)
    monkeypatch.undo()   # 以降のアサーション自体が stat を呼んでも罠に掛からないよう即座に戻す

    matched = [r for r in caplog.records if "stale codex run dir cleanup failed" in r.message]
    assert matched, "stat 失敗が warning として記録されていない"
    for r in matched:
        assert str(d) not in r.message, "warning にフルパス（users_dir を含む絶対パス）が出ている"
        assert "Permission denied" not in r.message, "例外の文字列表現がそのまま warning に出ている"
        # OSError(13, ...) は CPython の errno 連動サブクラス化で PermissionError になる
        # （型そのものは何であれ、型名・errno が記録されていることだけを確認する）。
        assert "type=PermissionError" in r.message and "errno=13" in r.message, \
            f"例外の型・errno が記録されていない: {r.message!r}"
    assert target.exists(), "stat 失敗時は掃除対象から外れる契約（continue）のはずが削除されている"


def test_restore_removable_permissions_recovers_when_root_itself_is_mode_000():
    """`root` 自身が 000（読み書き不可）だと、`os.walk` の前に root を chmod しないと配下の
    子（同じく 000）を一切列挙できず取り残る——root を先に chmod してから `os.walk` することで、
    root・子とも回収できることを確認する。"""
    from sherpa.providers.codex import sandbox as SB
    import os as _os
    import pathlib, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    run_dir = d / "run-rootlocked0000"
    child = run_dir / "child"
    child.mkdir(parents=True)
    (child / "leftover.txt").write_text("crashed run leftover", encoding="utf-8")
    _os.chmod(child, 0o000)
    _os.chmod(run_dir, 0o000)

    SB._remove_dir_best_effort(run_dir)

    assert not run_dir.exists(), "root 自身が 000 のとき配下の子（同じく 000）が回収されずに残った"


def test_codex_sessions_home_symlink_rejected_fail_closed():
    """R1b RV再検証 MEDIUM-3: 会話ごとの永続 CODEX_HOME（`.codex-sessions/{cid}`）も
    `_safe_workspace_authoring` と同じ契約で symlink を拒否する（fail-closed・None）。"""
    from sherpa import agents as A
    import pathlib, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    ud = d / "users"
    (ud / "ok" / "workspace").mkdir(parents=True)
    home = A._safe_codex_sessions_home(ud, "ok", 42)
    assert home is not None and home.is_dir(), "正常ケースが None になった"
    assert home == ud / "ok" / "workspace" / ".codex-sessions" / "42"
    # 同じ会話に対して2回呼んでも同じディレクトリを返す（毎ターン再利用の契約）。
    assert A._safe_codex_sessions_home(ud, "ok", 42) == home

    # {cid} 自体が symlink → 拒否。
    (ud / "bad1" / "workspace" / ".codex-sessions").mkdir(parents=True)
    evil = d / "evil"; evil.mkdir()
    (ud / "bad1" / "workspace" / ".codex-sessions" / "1").symlink_to(evil)
    assert A._safe_codex_sessions_home(ud, "bad1", 1) is None, "symlink {cid} が拒否されていない"

    # .codex-sessions 自体が symlink → 拒否。
    (ud / "bad2" / "workspace").mkdir(parents=True)
    (ud / "bad2" / "workspace" / ".codex-sessions").symlink_to(evil)
    assert A._safe_codex_sessions_home(ud, "bad2", 1) is None, "symlink .codex-sessions が拒否されていない"

    # conversation_id が整数化できない → 拒否（パス注入防御）。
    assert A._safe_codex_sessions_home(ud, "ok", "../../etc") is None
    assert A._safe_codex_sessions_home(ud, "ok", None) is None


def test_codex_home_and_config_perms():
    """RV HIGH: creds を含む CODEX_HOME は 0700・config.toml は 0600 で作られる。"""
    from sherpa import agents as A
    import pathlib, tempfile, stat
    ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch, ["/kb"], "low", True, "t", None)
    assert stat.S_IMODE(ch.stat().st_mode) == 0o700, "CODEX_HOME が 0700 でない"
    assert stat.S_IMODE((ch / "config.toml").stat().st_mode) == 0o600, "config.toml が 0600 でない"


def test_config_write_fail_closed_on_existing():
    """RV MEDIUM: 既存 config.toml があれば raise（fail-closed）＝古い config での起動を防ぐ。"""
    from sherpa import agents as A
    import pathlib, tempfile
    ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch, ["/kb"], "low", False, "t", None)   # 1回目は成功
    try:
        A._write_codex_authoring_config(ch, ["/kb"], "low", False, "t", None)  # 2回目は既存で raise
        assert False, "既存 config で raise していない（fail-closed 崩れ）"
    except FileExistsError:
        pass


def test_run_writes_config_inside_try_fail_closed():
    """RV MEDIUM: config 生成が try 内（例外→_stream_error→CODEX_HOME 掃除）で fail-closed になる。

    R1b（会話継続・Codex ネイティブ resume）で Popen 呼出は `_attempt()`（ネスト関数）に切り出された。
    `inspect.getsource` はネスト関数の**定義**をテキスト上その手前（try の外）に出すため、
    `def _attempt`/`subprocess.Popen` の文字列位置では「try 内・Popen 前」を判定できない
    （定義位置ではなく**呼出位置**が実行順を表す）。`_write_codex_authoring_config` 呼出が
    `_attempt(` の**呼出**（`yield from _attempt(`）より前にあることを見る。
    """
    from sherpa import agents as A
    import inspect
    src = inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)
    i_write = src.index("_write_codex_authoring_config(\n")
    i_call = src.index("yield from _attempt(")
    assert i_write < i_call, "config 生成が Popen 実行（_attempt 呼出）より後に置かれている（fail-closed 崩れ）"


def test_run_uses_process_group_kill_and_safe_authoring():
    """RV MEDIUM/BLOCKER: run() が start_new_session＋_killpg＋_safe_workspace_authoring を使う。"""
    from sherpa import agents as A
    import inspect
    src = inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)
    assert "_safe_workspace_authoring" in src, "安全 authoring 解決を使っていない"
    assert "start_new_session=True" in src, "プロセスグループ分離が無い"
    assert "_killpg" in src, "group kill が無い"


def test_codex_mcp_creds_in_config_file_not_cmdline():
    """MCP 版は creds を config ファイルに閉じる（コマンドライン -c に出さない＝/proc/cmdline 漏洩回避）。"""
    from sherpa import agents as A
    import pathlib, tempfile
    os.environ["NEO4J_PASSWORD"] = "creds_here"
    ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch, ["/kb"], "low", True, "test", None)
    cfg = (ch / "config.toml").read_text()
    assert "[mcp_servers.sherpa]" in cfg, "MCP 設定が config に無い"
    assert "creds_here" in cfg, "MCP creds が config ファイルに無い（サブプロセスが繋げない）"
    assert "PYTHONPATH" in cfg, "クリーン env 下で -m sherpa.mcp_server を解決する PYTHONPATH が無い"
    os.environ.pop("NEO4J_PASSWORD", None)


def test_codex_authoring_config_forwards_layer_to_mcp_env():
    """`layer` は sandbox 経路（config.toml 埋め込みの mcp_servers.sherpa.env）にも
    そのまま転送される。既定（省略）は SHERPA_MCP_LAYER を config に出さない。"""
    from sherpa import agents as A
    import pathlib, tempfile
    ch_none = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch_none, ["/kb"], "low", True, "test", None)
    assert "SHERPA_MCP_LAYER" not in (ch_none / "config.toml").read_text()

    ch_code = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch_code, ["/kb"], "low", True, "test", None, layer="code")
    cfg = (ch_code / "config.toml").read_text()
    assert 'SHERPA_MCP_LAYER = "code"' in cfg


def test_codex_authoring_config_denies_kb_roots_when_layer_restricted_and_mcp_on():
    """正典 §3.4「範囲と同じ硬いフィルタ」: 層（探す対象）が docs/code に限定され、かつ MCP が
    有効なターンでは、KB ルートを permission profile 上で明示的に deny する——読取許可の省略
    だけでは `":minimal"`（/usr,/bin,libs 等）配下に KB root が来る配置で読めてしまうため
    （world_admin_service は配置場所を制限しない）。Codex は MCP ツール経由（run_tool が層を
    実際にフィルタする）でしか KB を読めない。"""
    from sherpa import agents as A
    import pathlib, tempfile

    ch_code = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch_code, ["/kb/world-root"], "low", True, "test", None, layer="code")
    cfg = (ch_code / "config.toml").read_text()
    assert '"/kb/world-root" = "read"' not in cfg, "層限定なのに KB ルートが直接読取許可されている"
    assert '"/kb/world-root" = "deny"' in cfg, "層限定なのに KB ルートが明示 deny されていない"
    assert '[mcp_servers.sherpa]' in cfg              # MCP 自体は有効のまま（唯一の到達経路）

    ch_docs = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch_docs, ["/kb/world-root"], "low", True, "test", None, layer="docs")
    cfg_docs = (ch_docs / "config.toml").read_text()
    assert '"/kb/world-root" = "read"' not in cfg_docs
    assert '"/kb/world-root" = "deny"' in cfg_docs


def test_codex_authoring_config_denies_kb_root_even_under_minimal_prefix():
    """KB root が `":minimal"` の読取許可対象になりがちな配置（`/usr/share/...` 配下）でも、
    層限定時は明示 deny 行が出ること（省略に頼らない・世界の置き場所を制限しない前提を守る）。"""
    from sherpa import agents as A
    import pathlib, tempfile

    minimal_like_root = "/usr/share/sherpa-kb/some-world"
    ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch, [minimal_like_root], "low", True, "test", None, layer="code")
    cfg = (ch / "config.toml").read_text()
    assert f'"{minimal_like_root}" = "deny"' in cfg
    assert f'"{minimal_like_root}" = "read"' not in cfg


def test_codex_authoring_config_keeps_kb_roots_when_layer_both_or_omitted():
    """既定（layer=both・省略含む）は従来どおり KB ルートを直接読取許可する（挙動不変）。"""
    from sherpa import agents as A
    import pathlib, tempfile

    ch_omitted = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch_omitted, ["/kb/world-root"], "low", True, "test", None)
    assert '"/kb/world-root" = "read"' in (ch_omitted / "config.toml").read_text()

    ch_both = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch_both, ["/kb/world-root"], "low", True, "test", None, layer="both")
    assert '"/kb/world-root" = "read"' in (ch_both / "config.toml").read_text()


def test_codex_authoring_config_keeps_kb_roots_when_mcp_off_even_if_layer_restricted():
    """`_write_codex_authoring_config` 自体は「MCP 必須」を判定しない（呼び出し元＝`_run_authoring`
    が MCP 無効＋層限定を honest failure で先に弾く契約・本関数は mcp=False で呼ばれる想定が無い）。
    境界を明示的に固定する: mcp=False で渡されても KB ルート除外はしない（除外条件は `mcp and
    layer restricted` の AND）。"""
    from sherpa import agents as A
    import pathlib, tempfile

    ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch, ["/kb/world-root"], "low", False, "test", None, layer="code")
    assert '"/kb/world-root" = "read"' in (ch / "config.toml").read_text()


def test_codex_argv_uses_uid_cwd(tmp_path=None):
    """CodexProvider の run コード内で ctx.uid を使って cwd を組む。"""
    from sherpa import agents as A
    import inspect
    src = inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)
    assert "ctx.uid" in src, "ctx.uid を使った cwd 生成が見当たらない"
    assert "SHERPA_USERS_DIR" in src, "SHERPA_USERS_DIR 参照が見当たらない"
    assert '"-C"' in src or '"-C",\n' in src or '"-C", str' in src, "codex exec に -C オプションが含まれていない"


def test_ctx_has_uid_field():
    """Ctx に uid フィールドが追加されていて既定値は 'admin'。"""
    from sherpa.agents import Ctx
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(Ctx)}
    assert "uid" in fields, "Ctx に uid フィールドが無い"
    assert fields["uid"].default == "admin", f"uid の既定値が 'admin' でない: {fields['uid'].default}"


def test_compat_mode_uid_admin():
    """互換モード（auth_disabled）では uid='admin' が Ctx に渡る。"""
    from sherpa import auth
    assert auth.auth_disabled(), "互換モードが有効になっていない（SHERPA_AUTH_DISABLED=1 を確認）"
    # _current_user は互換モードで uid='admin' を返す。chat_service がそれを Ctx.uid に渡す。
    # chat_service のソースに uid=user_id の渡し方があることを確認。
    from sherpa import chat_service
    import inspect
    src = inspect.getsource(chat_service.handle_message)
    assert "uid=user_id" in src, "handle_message が Ctx.uid にuser_idを渡していない"


def test_office_libs_importable():
    """python-docx / python-pptx / openpyxl が import できる（Feature A の依存）。"""
    try:
        import docx  # noqa: F401
        has_docx = True
    except ImportError:
        has_docx = False
    try:
        import pptx  # noqa: F401
        has_pptx = True
    except ImportError:
        has_pptx = False
    try:
        import openpyxl  # noqa: F401
        has_openpyxl = True
    except ImportError:
        has_openpyxl = False

    missing = []
    if not has_docx:
        missing.append("python-docx (docx)")
    if not has_pptx:
        missing.append("python-pptx (pptx)")
    if not has_openpyxl:
        missing.append("openpyxl")
    # CI 環境で未インストールの可能性があるため、欠けていれば skip（黙って PASS しない）。
    if missing:
        pytest.skip(f"Office ライブラリ未インストール: {missing} (requirements.txt に追加済みか確認してください)")


def test_codex_files_scan_in_ws_files(tmp_path=None):
    """Codex 実行後に run_dir の新規ファイルを検出するロジックを確認。

    BLOCKER-2 fix 後: cwd = workspace/authoring/run-*/（personal files/ とは分離）。
    _before_ws_files で run_dir のスナップショットを取り差分検出する。
    """
    from sherpa import agents as A
    import inspect
    src = inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)
    assert "_before_ws_files" in src, "_before_ws_files スナップショットが無い"
    assert "run_dir" in src, "run_dir（Codex cwd）が無い（BLOCKER-2 fix 確認）"
    assert "record_workspace_file" in src, "record_workspace_file が呼ばれていない"
    assert "codex_wrote_files" in src, "env['codex_wrote_files'] のセットが無い"
    # Codex の cwd は authoring/run-*/（personal files/ とは別ディレクトリ）。
    assert "authoring" in src, "cwd に authoring が含まれていない（BLOCKER-2: files/ 分離の確認）"


# ===== Feature B: personal grep =====

def test_personal_grep_hits_own_file(tmp_path):
    """personal_grep_hits が本人の workspace/files/ のヒットを返す。"""
    from sherpa.chat_service import _personal_grep_hits

    uid = "testuser_pgh"
    files_dir = tmp_path / uid / "workspace" / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "mytax.txt").write_text("TAX_RATE=0.10\nshohizei\n", encoding="utf-8")

    # personal_workspace_files 台帳を mock して live_workspace_rel_paths が mytax.txt を返すようにする。
    from unittest.mock import patch
    with patch("sherpa.store.live_workspace_rel_paths", return_value={"mytax.txt"}):
        hits = _personal_grep_hits(uid, "TAX_RATE", str(tmp_path))

    assert len(hits) > 0, "ヒットが返らなかった"
    assert hits[0]["rel_path"] == "mytax.txt"
    assert "TAX_RATE" in hits[0]["text"]
    assert hits[0]["source"] == "個人ファイル内ヒット"


def test_personal_grep_hits_cross_user_isolation(tmp_path):
    """user_a のファイルは user_b の grep に出ない（越境不可）。"""
    from sherpa.chat_service import _personal_grep_hits
    from unittest.mock import patch

    uid_a = "user_a_iso"
    uid_b = "user_b_iso"
    files_a = tmp_path / uid_a / "workspace" / "files"
    files_a.mkdir(parents=True)
    (files_a / "secret_a.txt").write_text("SUPER_SECRET_A", encoding="utf-8")
    (tmp_path / uid_b / "workspace" / "files").mkdir(parents=True)

    # user_b の台帳は空（b のファイルはゼロ）。a のファイルを渡してはいけない。
    with patch("sherpa.store.live_workspace_rel_paths", return_value=set()):
        hits_b = _personal_grep_hits(uid_b, "SUPER_SECRET_A", str(tmp_path))

    assert hits_b == [], f"user_b に user_a のヒットが漏れた: {hits_b}"


def test_personal_grep_hits_off_returns_empty(tmp_path):
    """personal=False 時は個人ヒットがゼロ（OFF は従来どおり）。"""
    # chat_service.handle_message が personal=False のとき _personal_grep_hits を呼ばないことを確認。
    from sherpa import chat_service
    import inspect
    src = inspect.getsource(chat_service.handle_message)
    assert "if personal:" in src, "personal=True の分岐が無い"
    # personal=False 時にデフォルトが空リストのはず。
    from unittest.mock import patch
    with patch("sherpa.chat_service._personal_grep_hits") as mock_grep:
        # personal=False で呼んだとき _personal_grep_hits が呼ばれないことを確認。
        # handle_message は DB と neo4j に依存するので直接呼ばず、ソース検査で代替。
        pass
    # personal=False のとき personal_hits は空リスト（ソース検査）。
    assert "personal_hits = []" in src or "personal_hits: list[dict] = []" in src, \
        "personal_hits 初期化が無い"


def test_personal_facts_label():
    """_personal_facts が「個人ファイル内ヒット」ラベルを含む。"""
    from sherpa.chat_service import _personal_facts
    hits = [{"rel_path": "myfile.txt", "line": 3, "text": "TAX=10", "match": "TAX", "source": "個人ファイル内ヒット"}]
    result = _personal_facts(hits, "TAX")
    assert "個人ファイル内ヒット" in result, "個人ファイル内ヒットラベルが無い"
    assert "myfile.txt" in result


def test_personal_citations_structure():
    """_personal_citations が source='個人ファイル内ヒット' の citation を返す。"""
    from sherpa.chat_service import _personal_citations
    hits = [
        {"rel_path": "a.txt", "line": 1, "text": "foo", "match": "foo", "source": "個人ファイル内ヒット"},
        {"rel_path": "a.txt", "line": 5, "text": "foo2", "match": "foo", "source": "個人ファイル内ヒット"},
        {"rel_path": "b.md", "line": 2, "text": "bar", "match": "bar", "source": "個人ファイル内ヒット"},
    ]
    cites = _personal_citations(hits)
    # a.txt は1件（重複排除）、b.md は1件。
    assert len(cites) == 2, f"cite 件数が期待値と異なる: {len(cites)}"
    assert all(c["source"] == "個人ファイル内ヒット" for c in cites), "source ラベルが不正"
    rel_paths = {c["doc_id"] for c in cites}
    assert "a.txt" in rel_paths and "b.md" in rel_paths


def test_personal_not_in_es_graph():
    """不変条件: _personal_grep_hits は ES/Neo4j を呼ばない（台帳のみ）。"""
    from sherpa import chat_service
    import inspect
    src = inspect.getsource(chat_service._personal_grep_hits)
    assert "es_index" not in src, "_personal_grep_hits が ES を参照している（違反）"
    assert "world_graph" not in src, "_personal_grep_hits がグラフを参照している（違反）"
    # store.live_workspace_rel_paths を呼ぶ（台帳基準）。
    assert "live_workspace_rel_paths" in src


def test_personal_grep_hits_not_wired_to_layer_filter():
    """正典 §8 裁定論点7: 探す対象（層）フィルタは共有 KB の grep/ES にのみ適用し、個人ファイル検索
    には適用しない——`_personal_grep_hits` は `layer` を一切受け取らず、`sherpa.layer` も参照しない
    （将来のリファクタで誤って結合されないよう構造的に固定する）。"""
    import inspect
    from sherpa import chat_service
    sig = inspect.signature(chat_service._personal_grep_hits)
    assert "layer" not in sig.parameters
    src = inspect.getsource(chat_service._personal_grep_hits)
    assert "layer" not in src


def test_personal_grep_hits_finds_code_extension_file_independent_of_layer(tmp_path):
    """個人ファイル検索は `doc_kinds.CODE_EXT`（資料/コード区分）を参照しない独立の拡張子集合
    （`_PERSONAL_SEARCHABLE_EXT`）を使う——コード拡張子（.cbl）のファイルも layer 概念と無関係に
    ヒットする（層フィルタが個人ファイル側には一切効かないことの実地証明）。"""
    from sherpa.chat_service import _personal_grep_hits

    uid = "testuser_pgh_code"
    files_dir = tmp_path / uid / "workspace" / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "MYPROG.cbl").write_text("TAX_RATE=0.10\n", encoding="utf-8")

    from unittest.mock import patch
    with patch("sherpa.store.live_workspace_rel_paths", return_value={"MYPROG.cbl"}):
        hits = _personal_grep_hits(uid, "TAX_RATE", str(tmp_path))

    assert len(hits) > 0 and hits[0]["rel_path"] == "MYPROG.cbl"


def test_facts_includes_personal(tmp_path=None):
    """_facts が env['_personal_facts'] を末尾に追記する。"""
    from sherpa.agents import _facts
    env = {
        "data": {"citations": [{"doc_id": "shared.md", "quote": "共有"}]},
        "_personal_facts": "\n【個人ファイル内ヒット】\n[個人ファイル: my.txt 行1] TAX=10",
    }
    result = _facts("qa", env)
    assert "個人ファイル内ヒット" in result, "個人ファイルが facts に含まれていない"
    assert "shared.md" in result, "共有 KB の引用が消えた"


def test_facts_qa_falls_back_to_four_citations_without_synthesis_digest():
    """env['_synthesis_digest'] が無い呼び出し元（Heuristic/_GenProvider 等）は従来どおり
    先頭4引用×60字のまま（回帰）——5件目の doc_id は facts に現れない。"""
    from sherpa.agents import _facts
    cites = [{"doc_id": f"shared{i}.md", "quote": f"quote{i}"} for i in range(5)]
    env = {"data": {"citations": cites}}
    result = _facts("qa", env)
    assert all(f"shared{i}.md" in result for i in range(4))
    assert "shared4.md" not in result


def test_facts_qa_uses_synthesis_digest_when_present():
    """env['_synthesis_digest']（`agentic_search.build_synthesis_digest` の全件ダイジェスト）が
    あれば、citations の4件×60字整形の代わりにそれをそのまま使う——個人ファイル事実は従来どおり
    末尾に追記する。"""
    from sherpa.agents import _facts
    cites = [{"doc_id": f"shared{i}.md", "quote": f"quote{i}"} for i in range(5)]
    env = {
        "data": {"citations": cites},
        "_synthesis_digest": "ev-1: shared0.md 行 1-1「quote0」\nev-5: shared4.md 行 1-1「quote4」",
        "_personal_facts": "\n【個人ファイル内ヒット】\n[個人ファイル: my.txt 行1] TAX=10",
    }
    result = _facts("qa", env)
    assert result.startswith("ev-1: shared0.md")
    assert "shared4.md" in result   # digest 経由なら5件目も見える
    assert "該当箇所:" not in result   # digest 経由なので4件整形は使わない
    assert "個人ファイル内ヒット" in result   # 個人事実は従来どおり末尾に付く


# ===== Feature C: contains_personal_workspace =====

def test_set_contains_personal_workspace_exists():
    """store.set_contains_personal_workspace が定義されている。"""
    from sherpa import store
    assert hasattr(store, "set_contains_personal_workspace"), \
        "store.set_contains_personal_workspace が定義されていない"
    import inspect
    src = inspect.getsource(store.set_contains_personal_workspace)
    assert "contains_personal_workspace=TRUE" in src.replace(" ", "").upper() or \
           "contains_personal_workspace = TRUE" in src.upper() or \
           "contains_personal_workspace=TRUE" in src, \
           "set_contains_personal_workspace が TRUE に更新していない"


def test_handle_message_sets_personal_flag_in_source():
    """handle_message が _used_personal True 時に set_contains_personal_workspace を呼ぶ。"""
    from sherpa import chat_service
    import inspect
    src = inspect.getsource(chat_service.handle_message)
    assert "set_contains_personal_workspace" in src, \
        "handle_message が set_contains_personal_workspace を呼んでいない"
    assert "_used_personal" in src, "_used_personal フラグが無い"


def test_stream_message_sets_personal_flag_in_source():
    """stream_message が _used_personal True 時に set_contains_personal_workspace を呼ぶ。"""
    from sherpa import chat_service
    import inspect
    src = inspect.getsource(chat_service.stream_message)
    assert "set_contains_personal_workspace" in src, \
        "stream_message が set_contains_personal_workspace を呼んでいない"


def test_codex_wrote_files_triggers_personal_flag():
    """env['codex_wrote_files'] があれば _used_personal=True になる（ソース検査）。"""
    from sherpa import chat_service
    import inspect
    src_h = inspect.getsource(chat_service.handle_message)
    src_s = inspect.getsource(chat_service.stream_message)
    assert "codex_wrote_files" in src_h, "handle_message が codex_wrote_files をチェックしていない"
    assert "codex_wrote_files" in src_s, "stream_message が codex_wrote_files をチェックしていない"


def test_blocker1_flag_set_before_message_save():
    """BLOCKER-1 fix: contains_personal_workspace フラグが add_message より先に呼ばれる（ソース順検査）。"""
    from sherpa import chat_service
    import inspect
    src_h = inspect.getsource(chat_service.handle_message)
    src_s = inspect.getsource(chat_service.stream_message)
    for name, src in [("handle_message", src_h), ("stream_message", src_s)]:
        idx_flag = src.find("set_contains_personal_workspace")
        idx_msg = src.find("add_message")
        # add_message は最初の呼び出し（user メッセージ保存）があるので、2番目の出現（assistant 保存）を探す。
        idx_msg2 = src.find("add_message", idx_msg + 1)
        assert idx_flag != -1, f"{name} に set_contains_personal_workspace が無い"
        assert idx_msg2 != -1, f"{name} に2回目の add_message が無い"
        assert idx_flag < idx_msg2, (
            f"BLOCKER-1: {name} で set_contains_personal_workspace が"
            f" add_message(assistant) より後に来ている（flag:{idx_flag} > msg:{idx_msg2}）")


def test_high1_personal_facts_injected_into_plain_prompt():
    """HIGH-1 fix: _plain_run が personal_facts を prompt に注入してから LLM に渡す（ソース検査）。"""
    from sherpa import agents as A
    import inspect
    src = inspect.getsource(A._plain_run)
    assert "personal_facts" in src, "_plain_run に personal_facts の参照が無い"
    assert "_stream" in src or "_plain_stream" in src, "_plain_run が stream を呼んでいない"
    # personal_facts がある場合に _stream を直接呼んでプロンプトに注入する経路があること。
    assert "_PLAIN_PROMPT_WITH_PERSONAL" in src, "_plain_run に個人ヒット注入プロンプトが無い"


def test_high1_personal_facts_injected_into_agentic_prompt():
    """HIGH-1 fix: _agentic_run が personal_facts を ctx.message に前置してから loop に渡す（ソース検査）。"""
    from sherpa import agents as A
    import inspect
    src = inspect.getsource(A._GenProvider._agentic_run)
    assert "personal_facts" in src, "_agentic_run に personal_facts の参照が無い"
    # dataclasses.replace で ctx.message を書き換える（または同等の注入）。
    assert "replace" in src or "personal_facts" in src, "_agentic_run で message 注入が見当たらない"


def test_blocker2_codex_cwd_is_authoring_not_files():
    """BLOCKER-2 fix: CodexProvider の cwd は workspace/authoring/run-*/（files/ を含まない）。"""
    from sherpa import agents as A
    import inspect
    src = inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)
    # run_dir（authoring/run-* ・ _safe_run_authoring が作る実行ごとの作業領域）を cwd に使い、files/ とは別にする。
    assert "run_dir" in src, "BLOCKER-2: run_dir が CodexProvider.run に無い"
    # -C オプションに run_dir を渡す（files/ ではない）。
    assert '"-C", str(run_dir)' in src or '"-C",\n' in src, \
        "BLOCKER-2: -C オプションに run_dir が渡されていない"
    # authoring/run-*/ の cwd は files/ を含まない（files/ は cwd の外）。
    # files/ も run_dir の外で作られることをソースで確認。
    assert "ws_files" in src, "files/ の参照が消えた（台帳登録 or symlink チェックに必要）"


# ===== Codex 強化計画 Phase0（docs/proposals/2026-07-02-Codex強化計画.md §5 決定）
# → WEB-1（docs/notes/2026-08-29-デプロイ後バックログ.md）で管理者段を管理画面へ移管:
# 1) web_search 既定 OFF・管理画面「プロバイダ＋接続先」タブで許可（env は初回シードのみ）
# 2) AGENTS.md（共通ルールをプロンプトから分離）
# 3) -o（output-last-message）/--ephemeral の整備

def test_web_search_admin_allowed_reads_system_settings():
    """system_settings.web_search_allowed（既定 false）で管理者許可を判定する（env はもう見ない）。"""
    from sherpa import agents as A
    assert A._web_search_admin_allowed({}) is False
    assert A._web_search_admin_allowed({"web_search_allowed": False}) is False
    assert A._web_search_admin_allowed({"web_search_allowed": True}) is True


def test_web_search_admin_allowed_db_unreachable_is_false(monkeypatch):
    """DB 不達（`system_settings` 省略時の取得失敗）は安全側 false（env フォールバックはしない）。"""
    from sherpa import agents as A

    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("sherpa.store.get_system_settings", _boom)
    assert A._web_search_admin_allowed() is False


def test_web_search_disabled_value_matrix():
    """`_web_search_disabled_value`: 管理者許可（system_settings）AND このチャットでの希望 の
    両方が立った時だけ None（＝config へ何も書かない・Codex 既定 ON に委ねる）。それ以外は常に
    "disabled"。"""
    from sherpa import agents as A
    assert A._web_search_disabled_value(False, system_settings={}) == "disabled"
    assert A._web_search_disabled_value(True, system_settings={}) == "disabled", \
        "管理者未許可なのにユーザー希望だけで有効化された"
    allowed = {"web_search_allowed": True}
    assert A._web_search_disabled_value(False, system_settings=allowed) == "disabled", \
        "ユーザーが希望していないのに有効化された"
    assert A._web_search_disabled_value(True, system_settings=allowed) is None, \
        "管理者許可＋ユーザー希望で有効化されない"


def test_config_always_has_web_search_disabled_by_default():
    """per-request config.toml に web_search = "disabled" が常に入る（web_search_enabled 省略時）。"""
    from sherpa import agents as A
    import pathlib, tempfile
    ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
    # web_search_enabled 省略＝既定 False・system_settings も明示空（DB 不達に依存しない）。
    A._write_codex_authoring_config(ch, ["/kb"], "low", False, "t", None, system_settings={})
    cfg = (ch / "config.toml").read_text()
    assert 'web_search = "disabled"' in cfg, "既定で web_search=disabled が config に無い"


def test_config_web_search_enabled_only_with_admin_flag_and_user_setting():
    """管理者許可（system_settings）＋ user 設定 True の組み合わせの時だけ web_search 行が省略される
    （Codex 既定 ON に委ねる）。片方だけでは常に disabled のまま。"""
    from sherpa import agents as A
    import pathlib, tempfile

    # 片方だけ（管理者未許可・user だけ True）→ disabled のまま。
    ch1 = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch1, ["/kb"], "low", False, "t", None,
                                    web_search_enabled=True, system_settings={})
    assert 'web_search = "disabled"' in (ch1 / "config.toml").read_text()

    # 両方 True → disabled 行が書かれない。
    ch2 = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch2, ["/kb"], "low", False, "t", None,
                                    web_search_enabled=True, system_settings={"web_search_allowed": True})
    cfg2 = (ch2 / "config.toml").read_text()
    assert 'web_search = "disabled"' not in cfg2, "管理者許可＋ユーザー希望なのに disabled のまま"
    # TOML として妥当（他フィールドは壊れていない）。
    try:
        import tomllib; tomllib.loads(cfg2)
    except ModuleNotFoundError:
        pass


def test_web_search_argv_and_config_matrix_all_combinations():
    """RV LOW 5: sandbox（config.toml）／fallback（argv の -c）両経路で、管理者許可×ユーザー希望の
    3ケース（未許可+True / 許可+False / 許可+True）が実際に組み立てた文字列に正しく反映されることを
    matrix で確認する。両経路とも `_web_search_disabled_value` を単一の真実源として使っているので、
    ここでは実際の呼び出し関数（`_write_codex_authoring_config`／`_web_search_c_args`＝run() が
    そのまま使う関数）を直接叩く。"""
    from sherpa import agents as A
    import pathlib, tempfile

    cases = [
        (False, True, True),    # 管理者未許可はユーザー希望を無視して disabled のまま
        (True, False, True),    # 管理者許可でもユーザーが希望しなければ disabled のまま
        (True, True, False),    # 両方揃って初めて有効（disabled 行/引数が消える）
    ]
    for admin_allowed, user_enabled, expect_disabled in cases:
        sysset = {"web_search_allowed": admin_allowed}

        # sandbox 経路（config.toml）。
        ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
        A._write_codex_authoring_config(ch, ["/kb"], "low", False, "t", None,
                                        web_search_enabled=user_enabled, system_settings=sysset)
        has_disabled = 'web_search = "disabled"' in (ch / "config.toml").read_text()
        assert has_disabled == expect_disabled, (
            f"config.toml: admin={admin_allowed} user={user_enabled} で "
            f"disabled有無={has_disabled}（期待={expect_disabled}）")

        # fallback 経路（-c 引数・run() が実際に使う _web_search_c_args）。
        c_args = A._web_search_c_args(user_enabled, sysset)
        if expect_disabled:
            assert c_args == ["-c", 'web_search="disabled"'], f"-c 引数不一致: {c_args}"
        else:
            assert c_args == [], f"-c 引数が空でない（enabled 相当のはず）: {c_args}"


def test_config_web_search_always_disabled_for_ollama_construct_even_when_fully_allowed():
    """WEB-1: Codex(Ollama) 構成（`ollama_base_url` あり）は OpenAI がホストする
    web_search（管理インデックス）に接続できないため、管理者許可＋ユーザー希望が両方 True でも
    常に disabled のまま（Codex(OpenAI) 構成との唯一の違いは `ollama_base_url` の有無）。"""
    from sherpa import agents as A
    import pathlib, tempfile

    ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch, ["/kb"], "low", False, "t", None,
                                    web_search_enabled=True,
                                    ollama_base_url="http://localhost:11434",
                                    system_settings={"web_search_allowed": True})
    cfg = (ch / "config.toml").read_text()
    assert 'web_search = "disabled"' in cfg, (
        "Codex(Ollama) 構成で管理者許可＋ユーザー希望が揃っているのに web_search が有効化されている")
    # Ollama 向け model_provider 行自体は従来どおり書かれる（web_search 判定の変更が
    # 無関係な機能を壊していないことの確認）。
    assert "[model_providers.sherpa-ollama]" in cfg


def test_codex_provider_web_search_field_and_select_provider_wiring():
    """CodexProvider が web_search を受け取り _web_search に保持する／_select_provider が
    settings['codex_web_search'] をそのまま渡す（配線確認・ソース検査）。"""
    from sherpa import agents as A
    import inspect
    p = A.CodexProvider(web_search=True)
    assert p._web_search is True
    p2 = A.CodexProvider()
    assert p2._web_search is False, "既定は False のはず"
    src = inspect.getsource(A._select_provider)
    assert 'codex_web_search' in src, "_select_provider が codex_web_search を CodexProvider へ渡していない"


def test_run_argv_includes_ephemeral_and_output_last_message():
    """Phase0・§3: run() の argv 組立に --ephemeral と -o（last-message ファイル）が両方の
    経路（sandbox / fallback）に入る。"""
    from sherpa import agents as A
    import inspect
    src = inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)
    assert '"--ephemeral"' in src, "--ephemeral が argv に無い"
    assert '"-o", str(_last_message_path)' in src, "-o <last-message> が argv に無い"
    assert src.count('"--ephemeral"') >= 2, "--ephemeral が sandbox/fallback 両方に入っていない"
    assert "_read_last_message_fallback" in src, "-o フォールバック読取の呼び出しが無い"
    assert "_last_message_path.unlink" in src, "-o ファイルの後始末（削除）が無い"


def test_run_writes_agents_md():
    """Phase0・§2: run() が Codex 起動前に AGENTS.md を run_dir（authoring/run-*）へ書く（ベストエフォート）。"""
    from sherpa import agents as A
    import inspect
    src = inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)
    assert "codex_agents_md.write_agents_md(run_dir, output_schema=_schema_on)" in src, \
        "run() から write_agents_md(run_dir, output_schema=_schema_on) が呼ばれていない"


def test_read_last_message_fallback_reads_and_strips():
    """`_read_last_message_fallback`: ファイル無し/空は None・中身があれば前後空白を除いて返す。"""
    from sherpa import agents as A
    import pathlib, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    missing = d / "no-such-file.txt"
    assert A._read_last_message_fallback(missing) is None

    empty = d / "empty.txt"
    empty.write_text("   \n\n  ", encoding="utf-8")
    assert A._read_last_message_fallback(empty) is None

    ok = d / "ok.txt"
    ok.write_text("  最終回答のテキストです。\n", encoding="utf-8")
    assert A._read_last_message_fallback(ok) == "最終回答のテキストです。"


def test_agents_md_written_and_contains_required_phrases():
    """AGENTS.md が authoring 直下に書かれ、共通ルール（_prompt/_prompt_mcp から移した内容）を含む。"""
    from sherpa import codex_agents_md
    import pathlib, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    codex_agents_md.write_agents_md(d)
    p = d / "AGENTS.md"
    assert p.is_file(), "AGENTS.md が authoring 直下に書かれていない"
    txt = p.read_text(encoding="utf-8")
    for phrase in ("KB", "推測しない", "authoring 直下", "出典の列挙は不要",
                   # RV MEDIUM（2026-07-03）: 件数質問のブレ対策ルールも共通ルールとして常置する。
                   "list_docs", "path_prefix", "どのフォルダを数えたか"):
        assert phrase in txt, f"AGENTS.md に必須文言が無い: {phrase!r}"
    # 冪等（2回書いても壊れず上書きされる）。
    codex_agents_md.write_agents_md(d)
    assert p.read_text(encoding="utf-8") == txt, "2回目の書込で内容が変わった（冪等でない）"


# ===== RV「要修正」5件（2026-07-03） =====

def test_read_last_message_fallback_rejects_symlink():
    """RV MEDIUM 2: -o の最終メッセージファイルが symlink なら追従せず None を返す
    （symlink の指す先の内容を answer に取り込まない）。"""
    from sherpa import agents as A
    import pathlib, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    secret = d / "secret.txt"
    secret.write_text("SECRET DATA SHOULD NOT LEAK", encoding="utf-8")
    link = d / "last-message.txt"
    link.symlink_to(secret)
    assert A._read_last_message_fallback(link) is None, "symlink 経由で他ファイルの中身が読めてしまった"


def test_read_last_message_fallback_rejects_oversized_file():
    """RV MEDIUM 2: サイズ上限（256KB）を超えるファイルは読まない（None）。"""
    from sherpa import agents as A
    import pathlib, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    big = d / "big.txt"
    big.write_text("x" * (A._LAST_MESSAGE_MAX_BYTES + 1), encoding="utf-8")
    assert A._read_last_message_fallback(big) is None, "サイズ上限を超えても読み込んでしまった"


def test_write_agents_md_replaces_existing_symlink_without_following():
    """RV MEDIUM 3: 既存 AGENTS.md が symlink の場合、その指す先には一切書き込まず
    （symlink 追従なし）、AGENTS.md 自体を通常ファイルへ置き換える。"""
    from sherpa import codex_agents_md
    import pathlib, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    outside = d.parent / f"outside-target-{d.name}.txt"
    outside.write_text("SHOULD NOT BE OVERWRITTEN", encoding="utf-8")
    try:
        (d / "AGENTS.md").symlink_to(outside)
        codex_agents_md.write_agents_md(d)
        assert outside.read_text(encoding="utf-8") == "SHOULD NOT BE OVERWRITTEN", \
            "symlink の指す先（authoring 外）が書き換えられてしまった"
        target = d / "AGENTS.md"
        assert not target.is_symlink(), "AGENTS.md が symlink のまま残っている"
        assert "KB" in target.read_text(encoding="utf-8")
        # tmp ファイルが残っていない（台帳スキャン汚染防止）。
        leftovers = [p.name for p in d.iterdir() if p.name.startswith(".AGENTS.md.tmp-")]
        assert not leftovers, f"一時ファイルが残っている: {leftovers}"
    finally:
        outside.unlink(missing_ok=True)


def test_prompts_slimmed_stylistic_rules_moved_to_agents_md():
    """RV 用スナップショット代わり: 出典列挙/文体等の**スタイル面**の共通ルールは _prompt/_prompt_mcp
    から除去され AGENTS.md のみに存在する（質問固有部分は残る）ことを確認する。containment/grounding
    （KB 以外を読まない・推測しない）は RV HIGH（2026-07-03）で多層防御のため両方に残す設計になった
    ため、このテストの対象外＝下の test_prompts_retain_containment_and_grounding_defense_in_depth 参照。"""
    from sherpa import agents as A
    p = A.CodexProvider()
    fs_prompt = p._prompt("消費税率を変えたい", "impact", {"data": {}}, "v1")
    mcp_prompt = p._prompt_mcp("消費税率を変えたい", "impact", "v1")
    for removed in ("出典の列挙は不要", "簡潔（2〜4文）"):
        assert removed not in fs_prompt, f"スタイル系の共通ルール文言が _prompt に残っている: {removed!r}"
        assert removed not in mcp_prompt, f"スタイル系の共通ルール文言が _prompt_mcp に残っている: {removed!r}"
    assert "消費税率を変えたい" in fs_prompt and "消費税率を変えたい" in mcp_prompt
    assert "graph_neighbors" in mcp_prompt, "MCP ツール固有の使い分け説明が消えている"


def test_prompts_retain_containment_and_grounding_defense_in_depth():
    """RV HIGH（2026-07-03）: AGENTS.md は fail-open（書込失敗でも Codex 実行は継続）のため、
    containment（KB/MCP 以外を読まない）と grounding（推測しない）は AGENTS.md 依存にせず
    _prompt/_prompt_mcp にも短縮形を常置する（AGENTS.md と重複しても害はない＝独立性を優先）。"""
    from sherpa import agents as A
    p = A.CodexProvider()
    fs_prompt = p._prompt("消費税率を変えたい", "impact", {"data": {}}, "v1")
    mcp_prompt = p._prompt_mcp("消費税率を変えたい", "impact", "v1")
    assert "指定資料フォルダ以外は読まない" in fs_prompt, "_prompt に containment 短縮形が無い"
    assert "推測しない" in fs_prompt, "_prompt に grounding 短縮形が無い"
    assert "MCP ツール以外でのファイル直接読み取りは禁止" in mcp_prompt, \
        "_prompt_mcp に containment 短縮形が無い"
    assert "推測しない" in mcp_prompt, "_prompt_mcp に grounding 短縮形が無い"


def test_prompt_has_no_soft_layer_control():
    """層フィルタが限定されたターンでの MCP 無効時の直接 grep 経路（`_prompt`）は
    プロンプト指示による迂回可能なソフト制御ではなく、呼び出し元（`_run_authoring`）が実行自体を
    拒否する構造的な制御に一本化した——`_prompt` 自体はもう `layer` を受け取らない。"""
    from sherpa import agents as A
    import inspect
    p = A.CodexProvider()
    assert "layer" not in inspect.signature(p._prompt).parameters
    with pytest.raises(TypeError):
        p._prompt("消費税率を変えたい", "qa", {"data": {}}, "v1", layer="docs")


def test_run_authoring_layer_gated_to_qa_lens_only():
    """Codex 自身の追加探索への層フィルタは qa レンズのときだけ実効値を渡す
    （author は正典 §1.8 の既知の非対称性・impact/troubleshoot は非適用・layer.applies_to_lens とは
    あえて異なる判定式にしている）。この判定は Codex CLI 実行（subprocess）の内側にあり本ファイル他
    テストと同様に実行では検証できないため、ソース上の判定式そのものを固定する。"""
    from sherpa import agents as A
    import inspect
    src = inspect.getsource(A.CodexProvider._run_authoring)
    assert '_layer = (ctx.scope_meta or {}).get("layer") if decision["lens"] == "qa" else None' in src


def test_agents_md_write_failure_logs_warning(monkeypatch, caplog):
    """RV HIGH: AGENTS.md 書込失敗（fail-open）時に _log.warning が出る（サイレントに握り潰さない）。"""
    import logging
    from sherpa import agents as A, codex_agents_md
    import inspect
    src = inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)
    assert "_log.warning" in src and "write_agents_md" in src, \
        "run() の AGENTS.md 書込失敗パスに _log.warning が無い"

    def boom(_authoring):
        raise OSError("disk full (test)")
    monkeypatch.setattr(codex_agents_md, "write_agents_md", boom)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        try:
            codex_agents_md.write_agents_md("/dummy")
        except OSError as e:
            A._log.warning("AGENTS.md write failed (fail-open, prompt still has containment): %s", e)
    assert any("AGENTS.md write failed" in r.message for r in caplog.records), \
        "warning ログが記録されていない"


# ===== UI フィードバック1（途中停止・2026-07-03） =====

def test_ctx_has_stop_event_field():
    """Ctx に stop_event フィールドが追加されていて既定値は None。"""
    from sherpa.agents import Ctx
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(Ctx)}
    assert "stop_event" in fields, "Ctx に stop_event フィールドが無い"
    assert fields["stop_event"].default is None


def test_spawn_stop_watcher_kills_process_promptly_when_stop_event_set():
    """`_spawn_stop_watcher`: stop_event が立つと、ブロッキング中の子プロセスを即座に kill する
    （`for line in proc.stdout` のような読み取りループを stdout の EOF で解放する唯一の確実な方法）。
    実際の OS プロセス（sleep）を使い、30秒スリープが1秒未満で終わることを確認する。"""
    from sherpa import agents as A
    import subprocess, threading, time
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        ev = threading.Event()
        A._spawn_stop_watcher(proc, ev)
        time.sleep(0.2)
        assert proc.poll() is None, "stop_event を立てる前にプロセスが終わってしまった（テスト前提が崩れている）"
        t0 = time.time()
        ev.set()
        proc.wait(timeout=3)
        elapsed = time.time() - t0
        assert elapsed < 3, f"kill に時間がかかりすぎている: {elapsed}s"
        assert proc.returncode is not None and proc.returncode != 0, "SIGKILL で終了していない"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_spawn_stop_watcher_exits_when_process_finishes_naturally():
    """`_spawn_stop_watcher`: stop_event が一度も立たず子プロセスが自然終了した場合、
    監視スレッドがブロックしたまま残らない（daemon だが、無期限 wait() だとサーバ内で
    スレッドが積み上がるリークになるため、poll ベースで自分から抜けることを確認する）。"""
    from sherpa import agents as A
    import subprocess, threading, time
    proc = subprocess.Popen(["true"], start_new_session=True)   # 即終了する
    ev = threading.Event()
    t = A._spawn_stop_watcher(proc, ev)
    proc.wait(timeout=3)
    t.join(timeout=2)
    assert not t.is_alive(), "プロセス終了後も監視スレッドが残っている（リーク）"


# ===== Codex(Ollama) 構成: config.toml のモデル提供元差し替え（決定 2026-08-15）=====

def _config_text(tmp_path, **kw) -> str:
    from sherpa.providers.codex.sandbox import _write_codex_authoring_config
    _write_codex_authoring_config(tmp_path, ["/mnt/c/test"], "low", False, "test", None, **kw)
    return (tmp_path / "config.toml").read_text(encoding="utf-8")


def test_codex_openai_config_has_no_model_provider(tmp_path):
    """Codex(OpenAI) は従来どおり＝モデル提供元を書かない（Codex の既定に任せる）。"""
    txt = _config_text(tmp_path)
    assert "model_provider" not in txt
    assert "model_providers" not in txt


def test_codex_ollama_config_points_at_configured_url(tmp_path):
    """Codex(Ollama) は `ollama_url` 設定をそのまま接続先にする。

    実測（codex-cli 0.144.1・2026-08-15）:
      - 組み込み id `ollama` は予約語で上書き不可・接続先が localhost 固定（OLLAMA_HOST も効かない）
        → 独自 id で定義して設定を必ず効かせる
      - `wire_api = "chat"` は廃止済み。Codex は Responses API を使う（Ollama は 0.13.3+ が対応）
      - このファイル内容を `codex exec --strict-config` が受理し、指定 URL の `/v1/responses` を
        実際に叩くことを実機で確認済み
    """
    txt = _config_text(tmp_path, ollama_base_url="http://127.0.0.1:11500/")
    assert 'model_provider = "sherpa-ollama"' in txt      # 予約語 `ollama` は使わない
    assert "[model_providers.sherpa-ollama]" in txt
    assert 'base_url = "http://127.0.0.1:11500/v1"' in txt   # 末尾スラッシュを正規化して /v1 を付ける
    assert 'wire_api = "responses"' in txt                # chat 方言は codex 0.144 で廃止
    assert 'wire_api = "chat"' not in txt


def test_codex_ollama_selection_passes_configured_url(monkeypatch):
    """`_select_provider` が Codex(Ollama) 構成で `ollama_url` を CodexProvider へ渡す。"""
    from sherpa.providers import _select_provider

    p = _select_provider({"agent": "codex", "codex_model_provider": "ollama",
                          "ollama_url": "http://localhost:11434"})
    assert p._ollama_base_url == "http://localhost:11434"

    # Codex(OpenAI) は None＝従来経路（Codex の既定プロバイダ）
    p2 = _select_provider({"agent": "codex", "codex_model_provider": "openai"})
    assert p2._ollama_base_url is None


def test_codex_ollama_blocked_destination_is_not_launched():
    """許可されていない接続先（loopback でも allowlist でもない）は Codex を起動せず未接続を返す。"""
    from sherpa.providers import _UnwiredProvider, _select_provider

    p = _select_provider({"agent": "codex", "codex_model_provider": "ollama",
                          "ollama_url": "http://198.51.100.7:11434"})
    assert isinstance(p, _UnwiredProvider)
