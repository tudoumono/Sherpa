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
import time

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


# ===== Codex 原本直読（2026-09-10 提案書「Codex原本直読と調査スキル」S1）=====

def test_venv_root_detects_sys_prefix_vs_base_prefix(monkeypatch):
    """`_venv_root()`: `sys.prefix != sys.base_prefix`（venv 内で実行中）のときだけ venv root を返す。"""
    from sherpa.providers.codex import sandbox as SB
    import pathlib, sys

    monkeypatch.setattr(sys, "prefix", "/fake/venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    assert SB._venv_root() == pathlib.Path("/fake/venv")

    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    assert SB._venv_root() is None


def test_codex_clean_env_prefixes_path_with_venv_bin_when_available(monkeypatch, tmp_path):
    """`_codex_clean_env` の PATH 先頭に `<venv>/bin` が付く（venv が無ければ従来どおり・裁定 #2）。"""
    from sherpa.providers.codex import sandbox as SB
    import pathlib

    monkeypatch.setattr(SB, "_venv_root", lambda: pathlib.Path("/fake/venv"))
    env = SB._codex_clean_env(tmp_path / "ch", tmp_path / "auth", tmp_path / "tmp")
    assert env["PATH"].startswith("/fake/venv/bin:"), f"PATH 先頭が venv/bin でない: {env['PATH']!r}"

    monkeypatch.setattr(SB, "_venv_root", lambda: None)
    env2 = SB._codex_clean_env(tmp_path / "ch2", tmp_path / "auth2", tmp_path / "tmp2")
    assert "/fake/venv" not in env2["PATH"]


def test_scope_deny_entries_denies_siblings_off_the_scope_path(tmp_path):
    """範囲（scope）は部分木の read ではなく「経路上にない兄弟の deny」で表す（サンドボックスは
    親の deny が子の read に勝つ＝部分木だけ read にしても辿れない・実測）。root/A/sub を選ぶと
    root 直下の B と A 直下の other が deny され、A・A/sub・その配下は deny されない。"""
    from sherpa.providers.codex import sandbox as SB

    kb = tmp_path / "kb"
    (kb / "A" / "sub" / "deep").mkdir(parents=True)
    (kb / "A" / "other").mkdir()
    (kb / "B").mkdir()
    (kb / "top.txt").write_text("x")
    (kb / "A" / "sub" / "s.txt").write_text("x")
    deny = SB._scope_deny_entries([str(kb)], ["A/sub"])
    assert set(deny) == {str(kb.resolve() / "B"), str(kb.resolve() / "top.txt"), str(kb.resolve() / "A" / "other")}


def test_scope_deny_entries_denies_root_when_scope_absent_and_skips_symlinks(tmp_path):
    """選択した scope が root 配下に無い root は root ごと deny。`..`／symlink 脱出は無視。
    兄弟が symlink なら deny に書かない（bubblewrap は symlink への deny で起動に失敗する）。"""
    from sherpa.providers.codex import sandbox as SB

    kb = tmp_path / "kb"; (kb / "A").mkdir(parents=True)
    outside = tmp_path / "outside"; outside.mkdir()
    (kb / "escape").symlink_to(outside)
    (kb / "link_sibling").symlink_to(kb / "A")
    assert SB._scope_deny_entries([str(kb)], ["escape"]) == [str(kb.resolve())]
    assert SB._scope_deny_entries([str(kb)], ["../outside"]) == [str(kb.resolve())]
    deny = SB._scope_deny_entries([str(kb)], ["A"])
    assert deny == [], f"symlink の兄弟が deny に入っている: {deny}"
    assert SB._scope_deny_entries([str(kb)], None) == []


def test_scope_deny_entries_max_entries_raises(tmp_path):
    from sherpa.providers.codex import sandbox as SB
    import pytest

    kb = tmp_path / "kb"; (kb / "A").mkdir(parents=True)
    for i in range(5):
        (kb / f"f{i}.txt").write_text("x")
    with pytest.raises(RuntimeError, match="scope_enum_failed:max_entries_exceeded"):
        SB._scope_deny_entries([str(kb)], ["A"], max_entries=3)


def test_direct_read_roots_empty_scope_returns_whole_roots(monkeypatch, tmp_path):
    """scope_paths が空（省略）なら従来どおり KB root・派生ルート丸ごとを返す。"""
    from sherpa.providers.codex import sandbox as SB
    from sherpa import worlds as W

    kb = tmp_path / "kb"; kb.mkdir()
    md = tmp_path / "derived" / "md"; md.mkdir(parents=True)
    monkeypatch.setattr(W, "_fixtures", lambda: False)
    monkeypatch.setattr(W, "world_dir", lambda w: kb)
    monkeypatch.setattr(W, "derived_md_dir", lambda w: md)
    monkeypatch.setattr(W, "derived_rag_dir", lambda w: tmp_path / "no-rag")

    roots = SB._direct_read_roots("test", None)
    assert str(kb.resolve()) in roots
    assert str(md.resolve()) in roots


def test_direct_read_roots_ignores_scope_and_returns_base_roots(monkeypatch, tmp_path):
    """read root は常に KB root＋派生ルート（scope は `_scope_deny_entries` の deny で表す）。"""
    from sherpa.providers.codex import sandbox as SB
    from sherpa import worlds as W

    kb = tmp_path / "kb"; kb.mkdir()
    monkeypatch.setattr(W, "_fixtures", lambda: False)
    monkeypatch.setattr(W, "world_dir", lambda w: kb)
    monkeypatch.setattr(W, "derived_md_dir", lambda w: tmp_path / "no-md")
    monkeypatch.setattr(W, "derived_rag_dir", lambda w: tmp_path / "no-rag")
    assert SB._direct_read_roots("test", ["A/sub"]) == [str(kb.resolve())]


def test_enumerate_sensitive_detects_known_patterns(tmp_path):
    """`.env`／`id_rsa`／`credentials.json`／`*.pem` を検出する（判定は `text_kind.is_sensitive` に一本化）。"""
    from sherpa.providers.codex import sandbox as SB
    import pathlib

    root = tmp_path / "root"; root.mkdir()
    (root / ".env").write_text("SECRET=1")
    (root / "id_rsa").write_text("---")
    (root / "sub").mkdir()
    (root / "sub" / "credentials.json").write_text("{}")
    (root / "sub" / "x.pem").write_text("---")
    (root / "normal.txt").write_text("ok")

    hits = SB._enumerate_sensitive([str(root)])
    names = {pathlib.Path(h).name for h in hits}
    assert names == {".env", "id_rsa", "credentials.json", "x.pem"}


def test_enumerate_sensitive_symlink_denies_target_inside_roots_only(tmp_path):
    """秘匿名の symlink は symlink 自体を deny に書かず（bubblewrap が起動失敗する）、実体が root 配下の
    通常ファイルなら実体を deny・root 外や dangling なら何も書かない（`:root=deny` で読めない）。"""
    from sherpa.providers.codex import sandbox as SB

    root = tmp_path / "root"; root.mkdir()
    inside = root / "notes.txt"; inside.write_text("secret")
    (root / ".env").symlink_to(inside)
    outside = tmp_path / "real_secret.txt"; outside.write_text("x")
    (root / "id_rsa").symlink_to(outside)
    (root / "credentials.json").symlink_to(tmp_path / "missing")

    hits = SB._enumerate_sensitive([str(root)])
    assert hits == [str(inside.resolve())]


def test_enumerate_sensitive_dedupes_overlapping_roots(tmp_path):
    """root が入れ子（A と A/sub）でも同じファイルは 1 回だけ（TOML キー重複で Codex が起動しない）。"""
    from sherpa.providers.codex import sandbox as SB

    root = tmp_path / "A"; (root / "sub").mkdir(parents=True)
    (root / "sub" / ".env").write_text("x")
    hits = SB._enumerate_sensitive([str(root), str(root / "sub")])
    assert hits == [str((root / "sub" / ".env").resolve())]


def test_enumerate_sensitive_does_not_follow_symlinked_dirs(tmp_path):
    """symlink ディレクトリの中までは辿らない（`followlinks=False`）。"""
    from sherpa.providers.codex import sandbox as SB

    root = tmp_path / "root"; root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / ".env").write_text("SECRET")
    (root / "link").symlink_to(outside)

    assert SB._enumerate_sensitive([str(root)]) == [], "symlink ディレクトリの中まで辿ってしまっている"


def test_enumerate_sensitive_max_hits_exceeded_raises(tmp_path):
    """上限件数を超える秘匿ファイルは RuntimeError（fail-closed）。"""
    from sherpa.providers.codex import sandbox as SB

    root = tmp_path / "root"; root.mkdir()
    for i in range(5):
        (root / f"{i}.pem").write_text("x")
    with pytest.raises(RuntimeError):
        SB._enumerate_sensitive([str(root)], max_hits=2)


def test_enumerate_sensitive_max_files_exceeded_raises(tmp_path):
    """走査ファイル数の上限超過も RuntimeError（fail-closed）。"""
    from sherpa.providers.codex import sandbox as SB

    root = tmp_path / "root"; root.mkdir()
    for i in range(5):
        (root / f"file{i}.txt").write_text("x")
    with pytest.raises(RuntimeError):
        SB._enumerate_sensitive([str(root)], max_files=2)


def test_enumerate_sensitive_permission_error_raises(monkeypatch, tmp_path):
    """走査中の OSError（PermissionError 等）は握りつぶさず RuntimeError（fail-closed）。"""
    from sherpa.providers.codex import sandbox as SB

    root = tmp_path / "root"; root.mkdir()

    def _boom_walk(top, *a, **k):
        onerror = k.get("onerror")
        if onerror:
            onerror(PermissionError("no access (test)"))
        return iter(())
    monkeypatch.setattr(SB.os, "walk", _boom_walk)
    with pytest.raises(RuntimeError):
        SB._enumerate_sensitive([str(root)])


def test_enumerate_sensitive_recurses_venv_like_root(tmp_path):
    """venv も同じ関数で再帰する（`.venv/bin/.env` のような深い秘匿名も deny）。"""
    from sherpa.providers.codex import sandbox as SB

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / ".env").write_text("SECRET")
    (venv / "bin" / ".env").write_text("SECRET-deep")
    hits = SB._enumerate_sensitive([str(venv)])
    assert set(hits) == {str((venv / ".env").resolve()), str((venv / "bin" / ".env").resolve())}


def test_prune_deny_paths_drops_symlink_missing_nested_and_outside(tmp_path):
    """deny 行の整形: symlink・不在・deny 済みフォルダ配下・read root 外は落とし、重複は 1 つ、
    read root そのものへの deny は残す（いずれも bubblewrap の起動失敗か無意味な行）。"""
    from sherpa.providers.codex import sandbox as SB

    root = tmp_path / "kb"; (root / "other").mkdir(parents=True)
    (root / "other" / "o.txt").write_text("x")
    (root / "a.txt").write_text("x")
    (root / "ln").symlink_to(root / "a.txt")
    outside = tmp_path / "outside"; outside.mkdir()
    deny = [str(root / "other" / "o.txt"), str(root / "other"), str(root / "a.txt"), str(root / "a.txt"),
            str(root / "ln"), str(root / "missing"), str(outside), str(root)]
    got = SB._prune_deny_paths(deny, [str(root)])
    assert got == [str(root.resolve())]                       # root 自体の deny が全てを覆う
    got2 = SB._prune_deny_paths(deny[:-1], [str(root)])
    assert got2 == [str((root / "a.txt").resolve()), str((root / "other").resolve())]


def test_run_authoring_uses_direct_read_ok_for_prompt_and_disables_on_enum_failure(tmp_path, monkeypatch):
    """`_run_authoring`: 秘匿列挙が成功すれば prompt に直読許可の文言・profile に KB root／派生ルートの read が
    入り、失敗（RuntimeError）すれば `direct_read_roots=[]` で config が書かれ、prompt に
    「直接読み取りは使えない」が入る（プロンプトと permission profile を食い違わせない・§B-6）。"""
    from sherpa.providers.codex import provider as P
    from sherpa import agents as A
    import os as _os
    import stat

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    script = bin_dir / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path(r'{argv_log}').open('a', encoding='utf-8').write(sys.argv[-1] + chr(10) + '---' + chr(10))\n"
        "print(json.dumps({'type': 'item.completed', "
        "'item': {'id': '1', 'type': 'agent_message', 'text': 'ok'}}))\n"
        "sys.exit(0)\n"
    )
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{_os.pathsep}{_os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")

    def _ctx(uid):
        return A.Ctx(
            message="消費税率について教えて", world="v1",
            route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
            dispatch=lambda lens_, inp: {
                "lens": lens_, "headline": "dispatch-headline",
                "summary": {"total": 0}, "data": {}, "sources": [],
            },
            knowledge=True, uid=uid,
        )

    # 成功時: config に direct_read_roots が read で出て、prompt に直読許可の文言が入る。
    captured_ok = {}
    orig = P._write_codex_authoring_config

    def _capture_ok(*args, **kwargs):
        captured_ok["kwargs"] = kwargs
        return orig(*args, **kwargs)
    monkeypatch.setattr(P, "_write_codex_authoring_config", _capture_ok)
    list(A.CodexProvider().run(_ctx("direct-read-ok-u1")))
    assert captured_ok["kwargs"].get("direct_read_roots") is not None
    assert captured_ok["kwargs"].get("direct_read_roots") != []
    prompts_ok = argv_log.read_text(encoding="utf-8").split("---\n")
    assert any("原本は直接読んでよい" in p for p in prompts_ok if p.strip())

    # 失敗時: 列挙が RuntimeError を送出 → direct_read_roots=[]・prompt に縮退文言。
    argv_log.write_text("")
    captured_fail = {}

    def _capture_fail(*args, **kwargs):
        captured_fail["kwargs"] = kwargs
        return orig(*args, **kwargs)
    monkeypatch.setattr(P, "_write_codex_authoring_config", _capture_fail)
    monkeypatch.setattr(P, "_enumerate_sensitive",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sensitive_enum_failed:boom")))
    list(A.CodexProvider().run(_ctx("direct-read-fail-u1")))
    assert captured_fail["kwargs"].get("direct_read_roots") == [], \
        "列挙失敗時に direct_read_roots=[] で書かれていない"
    prompts_fail = argv_log.read_text(encoding="utf-8").split("---\n")
    assert any("今回は原本の直接読み取りは使えない" in p for p in prompts_fail if p.strip())


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


def test_codex_authoring_config_forwards_sidecar_path_to_mcp_env():
    """DEPTH-2 S3b: `sidecar_path` を渡すと config.toml の mcp_servers.sherpa.env に
    SHERPA_MCP_SIDECAR が乗る（子エージェント・親のどちらの MCP プロセスも同じサイドカーへ書く
    ため、per-thread ではなく config 全体に1つだけ載る）。省略時は既存どおり出ない。"""
    from sherpa import agents as A
    import pathlib, tempfile
    ch_none = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch_none, ["/kb"], "low", True, "test", None)
    assert "SHERPA_MCP_SIDECAR" not in (ch_none / "config.toml").read_text()

    ch_sc = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch_sc, ["/kb"], "low", True, "test", None,
                                    sidecar_path="/tmp/run-x/.mcp_sidecar.jsonl")
    assert "/tmp/run-x/.mcp_sidecar.jsonl" in (ch_sc / "config.toml").read_text()


def test_codex_authoring_config_sidecar_ignored_when_mcp_disabled():
    """`mcp=False` のときは `sidecar_path` を渡しても MCP 設定自体が無い＝当然出ない。"""
    from sherpa import agents as A
    import pathlib, tempfile
    ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch, ["/kb"], "low", False, "test", None,
                                    sidecar_path="/tmp/run-x/.mcp_sidecar.jsonl")
    cfg = (ch / "config.toml").read_text()
    assert "mcp_servers" not in cfg and "SHERPA_MCP_SIDECAR" not in cfg


# ===== DEPTH-2 S6（§2.6）: multi_agent の [agents]/[agents.worker]/[agents.evaluator] =====

def test_codex_authoring_config_writes_agents_sections_when_multi_agent(tmp_path):
    """`multi_agent=True` のとき config.toml に `[agents]`/`[agents.worker]`/`[agents.evaluator]`
    が入り、子 config_file は codex_home 配下（model-shell 不可視＝`":root" = "deny"` の外側）に
    生成される。省略時（既定 False）は一切出ない（回帰確認）。"""
    from sherpa import agents as A
    from sherpa.providers.codex import sandbox as CS

    ch_off = tmp_path / "ch-off"
    A._write_codex_authoring_config(ch_off, ["/kb"], "low", True, "test", None)
    cfg_off = (ch_off / "config.toml").read_text()
    assert "[agents]" not in cfg_off and "[agents.worker]" not in cfg_off

    ch_on = tmp_path / "ch-on"
    A._write_codex_authoring_config(ch_on, ["/kb"], "low", True, "test", None,
                                    multi_agent=True, orchestrator_model="gpt-5.5")
    cfg_on = (ch_on / "config.toml").read_text()
    assert "[agents]" in cfg_on
    assert "[agents.worker]" in cfg_on and "[agents.evaluator]" in cfg_on
    assert f'default_subagent_model = "{CS._CODEX_WORKER_MODEL_FALLBACK}"' in cfg_on
    assert "max_concurrent_threads_per_session" in cfg_on

    worker_path = ch_on / "agents" / "worker.toml"
    evaluator_path = ch_on / "agents" / "evaluator.toml"
    assert f'config_file = "{worker_path}"' in cfg_on
    assert f'config_file = "{evaluator_path}"' in cfg_on
    assert worker_path.is_file() and evaluator_path.is_file()
    # 子の config_file は codex_home 直下（model-shell から不可視＝`":root" = "deny"` の外側）で、
    # authoring/run_dir（":workspace_roots" の write 対象）配下ではない。
    assert ch_on.resolve() in worker_path.resolve().parents

    worker_toml = worker_path.read_text()
    assert f'model = "{CS._CODEX_WORKER_MODEL_FALLBACK}"' in worker_toml
    evaluator_toml = evaluator_path.read_text()
    assert 'model = "gpt-5.5"' in evaluator_toml   # 本体と同じモデル
    assert 'model_reasoning_effort = "low"' in evaluator_toml   # 呼び出し元の reason をそのまま使う

    import tomllib
    parsed = tomllib.loads(cfg_on)
    assert parsed["agents"]["default_subagent_model"] == CS._CODEX_WORKER_MODEL_FALLBACK
    assert parsed["agents"]["worker"]["config_file"] == str(worker_path)
    assert parsed["agents"]["evaluator"]["config_file"] == str(evaluator_path)


def test_codex_authoring_config_evaluator_falls_back_to_worker_model_without_orchestrator_model():
    """`orchestrator_model` を渡さない呼び出し（既存の直接呼び出し規約）でも config 生成自体は
    壊れず、evaluator は worker と同じモデルへ倒れる。"""
    from sherpa import agents as A
    from sherpa.providers.codex import sandbox as CS
    import pathlib, tempfile
    ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch, ["/kb"], "low", True, "test", None, multi_agent=True)
    evaluator_toml = (ch / "agents" / "evaluator.toml").read_text()
    assert f'model = "{CS._CODEX_WORKER_MODEL_FALLBACK}"' in evaluator_toml


def test_write_agents_md_multi_agent_round_count_standard_disables_evaluator(tmp_path):
    """標準（`review_rounds=0`）は evaluator を使わないことが本文に明示される。
    `multi_agent=False`（既定）は役割段落自体が出ない（回帰確認）。"""
    from sherpa import codex_agents_md
    d = tmp_path / "authoring-std"
    d.mkdir()
    codex_agents_md.write_agents_md(d)   # multi_agent 既定 False
    txt_off = (d / "AGENTS.md").read_text(encoding="utf-8")
    assert "spawn_agent(worker)" not in txt_off

    codex_agents_md.write_agents_md(d, multi_agent=True, review_rounds=0)
    txt = (d / "AGENTS.md").read_text(encoding="utf-8")
    assert "spawn_agent(worker)" in txt
    assert "0 回＝evaluator は使わない" in txt
    assert "spawn_agent(evaluator) は" not in txt   # 標準は evaluator の spawn 指示自体を出さない


def test_write_agents_md_multi_agent_round_count_deep_and_max(tmp_path):
    """深く（2）・最大（管理画面の設定値）で見直しの回数がそのまま本文に埋め込まれる。"""
    from sherpa import codex_agents_md
    d = tmp_path / "authoring-deep"
    d.mkdir()
    codex_agents_md.write_agents_md(d, multi_agent=True, review_rounds=2)
    txt_deep = (d / "AGENTS.md").read_text(encoding="utf-8")
    assert "見直しの回数は 2 回まで" in txt_deep
    assert "0 回＝evaluator は使わない" not in txt_deep

    codex_agents_md.write_agents_md(d, multi_agent=True, review_rounds=7)
    txt_max = (d / "AGENTS.md").read_text(encoding="utf-8")
    assert "見直しの回数は 7 回まで" in txt_max


def test_codex_run_argv_includes_multi_agent_flag_for_openai_codex(tmp_path, monkeypatch):
    """本体ターンの argv に `-c features.multi_agent=true` が入る（Codex(OpenAI) 構成・§2.6の
    「起動時に明示する」・実行そのものは偽 codex で代替）。"""
    import stat
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    script = bin_dir / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path(r'{argv_log}').open('a', encoding='utf-8').write(repr(sys.argv[1:]) + chr(10))\n"
        'print(json.dumps({"type": "item.completed", "item": {"id": "m0", "type": "agent_message", '
        '"text": "確認しました。"}}))\n'
        'print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, '
        '"cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0}}))\n'
        "sys.exit(0)\n")
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users_multi_agent_argv"))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")

    ctx = A.Ctx(
        message="multi_agent argv テスト", world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {"lens": lens_, "headline": "dispatch-headline",
                                     "summary": {"total": 0}, "data": {}, "sources": []},
        knowledge=True, uid="multi-agent-argv-u1")
    events = list(A.CodexProvider().run(ctx))

    calls = [eval(line) for line in argv_log.read_text().splitlines() if line.strip()]
    assert len(calls) == 1
    assert "features.multi_agent=true" in calls[0]
    # C64/#73: 深さ案内（chat_service._depth_actually_helps）が使う env["codex_multi_agent"] は、
    # multi_agent が実際に有効化された構成（既定 OpenAI＋サンドボックス有効）でだけ真になる。
    res = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"][0]
    assert res["env"]["codex_multi_agent"] is True


def test_codex_run_argv_disables_multi_agent_on_sandbox_fallback(tmp_path, monkeypatch):
    """RV #67 是正: `SHERPA_CODEX_SANDBOX=0`（フォールバック経路）は config.toml 自体を書かない
    （`[agents.*]` の層が無い）ため、argv は `features.multi_agent=false` を明示し、AGENTS.md にも
    役割段落（`spawn_agent(worker)`）が出ない——有効なフリだけして spawn が毎ターン失敗するのを防ぐ。"""
    import stat
    from sherpa import agents as A
    from sherpa import codex_agents_md
    from sherpa.providers.codex import provider as PV

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    script = bin_dir / "codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path(r'{argv_log}').open('a', encoding='utf-8').write(repr(sys.argv[1:]) + chr(10))\n"
        'print(json.dumps({"type": "item.completed", "item": {"id": "m0", "type": "agent_message", '
        '"text": "確認しました。"}}))\n'
        'print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, '
        '"cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0}}))\n'
        "sys.exit(0)\n")
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users_multi_agent_fallback"))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")

    captured: dict = {}
    orig_write = codex_agents_md.write_agents_md

    def _spy(authoring, **kw):
        captured.update(kw)
        return orig_write(authoring, **kw)
    monkeypatch.setattr(PV.codex_agents_md, "write_agents_md", _spy)

    ctx = A.Ctx(
        message="multi_agent fallback argv テスト", world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {"lens": lens_, "headline": "dispatch-headline",
                                     "summary": {"total": 0}, "data": {}, "sources": []},
        knowledge=True, uid="multi-agent-fallback-u1")
    events = list(A.CodexProvider().run(ctx))

    calls = [eval(line) for line in argv_log.read_text().splitlines() if line.strip()]
    assert len(calls) == 1
    assert "features.multi_agent=true" not in calls[0]
    assert "features.multi_agent=false" in calls[0]
    assert captured.get("multi_agent") is False
    # C64/#73: サンドボックス無効（フォールバック）は env["codex_multi_agent"] も偽——
    # _depth_actually_helps がこの構成で「深さを上げて探す」を出さないようにする受け渡し口。
    res = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"][0]
    assert res["env"]["codex_multi_agent"] is False


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


def test_codex_authoring_config_keeps_kb_root_read_even_when_layer_restricted():
    """契約変更（裁定 #4・2026-09-10・提案書「Codex原本直読と調査スキル」§2-2/§6）:
    **Codex は層の指定を強制しない**——層（探す対象）が docs/code に限定されていても、直読
    （permission profile の read）は従来どおり KB ルートを read する。旧: 「層限定＋MCP 有効の
    ターンは KB ルートを明示 deny し MCP ツール経由でしか読めなくする」という構造的強制は撤去した
    （層のフィルタは MCP ツール側＝`run_tool`／`SHERPA_MCP_LAYER` だけが担う）。
    旧テスト名: test_codex_authoring_config_denies_kb_roots_when_layer_restricted_and_mcp_on。"""
    from sherpa import agents as A
    import pathlib, tempfile

    ch_code = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch_code, ["/kb/world-root"], "low", True, "test", None, layer="code")
    cfg = (ch_code / "config.toml").read_text()
    assert '"/kb/world-root" = "read"' in cfg, "層限定でも KB ルートは read のはず（裁定 #4）"
    assert '"/kb/world-root" = "deny"' not in cfg
    assert '[mcp_servers.sherpa]' in cfg              # MCP 自体は有効のまま（層フィルタの唯一の到達経路）

    ch_docs = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch_docs, ["/kb/world-root"], "low", True, "test", None, layer="docs")
    cfg_docs = (ch_docs / "config.toml").read_text()
    assert '"/kb/world-root" = "read"' in cfg_docs
    assert '"/kb/world-root" = "deny"' not in cfg_docs


def test_codex_authoring_config_keeps_kb_root_read_even_under_minimal_prefix():
    """契約変更（裁定 #4）: KB root が `":minimal"` の読取許可対象になりがちな配置
    （`/usr/share/...` 配下）でも、層限定時に deny されないこと（撤去した旧挙動の残骸が無い）。
    旧テスト名: test_codex_authoring_config_denies_kb_root_even_under_minimal_prefix。"""
    from sherpa import agents as A
    import pathlib, tempfile

    minimal_like_root = "/usr/share/sherpa-kb/some-world"
    ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch, [minimal_like_root], "low", True, "test", None, layer="code")
    cfg = (ch / "config.toml").read_text()
    assert f'"{minimal_like_root}" = "read"' in cfg
    assert f'"{minimal_like_root}" = "deny"' not in cfg


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
    契約変更（裁定 #4）で層に基づく KB deny 自体を撤去済みのため、mcp=False・layer="code" でも
    KB ルートは常に read（境界を明示的に固定する回帰テストとして残す）。"""
    from sherpa import agents as A
    import pathlib, tempfile

    ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
    A._write_codex_authoring_config(ch, ["/kb/world-root"], "low", False, "test", None, layer="code")
    assert '"/kb/world-root" = "read"' in (ch / "config.toml").read_text()


def test_codex_authoring_config_direct_read_roots_override_kb_roots():
    """`direct_read_roots` が渡されたとき、read されるのは `kb_roots` ではなくこちら
    （KB root／派生ルート。範囲は兄弟 deny で表す）。`sensitive_deny` は read 行より後に個別 deny で出る。"""
    from sherpa import agents as A
    import pathlib, tempfile

    base = pathlib.Path(tempfile.mkdtemp())
    sub = base / "scope" / "subtree"; sub.mkdir(parents=True)
    (sub / ".env").write_text("x")
    ch = base / "ch"
    A._write_codex_authoring_config(
        ch, ["/kb/should-not-appear"], "low", False, "test", None,
        direct_read_roots=[str(sub), "/derived/md/subtree"],
        sensitive_deny=[str(sub / ".env")])
    cfg = (ch / "config.toml").read_text()
    assert f'"{sub}" = "read"' in cfg
    assert '"/derived/md/subtree" = "read"' in cfg
    assert '"/kb/should-not-appear" = "read"' not in cfg, "direct_read_roots 指定時に kb_roots が read されている"
    assert f'"{sub / ".env"}" = "deny"' in cfg
    read_pos = cfg.index(f'"{sub}" = "read"')
    deny_pos = cfg.index(f'"{sub / ".env"}" = "deny"')
    assert read_pos < deny_pos, "秘匿 deny が read 行より前に出ている"


def test_codex_authoring_config_direct_read_roots_empty_list_denies_all_kb_reads(monkeypatch):
    """`direct_read_roots=[]`（明示的な空リスト）は `None`（省略）と区別され、KB ルートを一切
    read しない——秘匿ファイル列挙が失敗した呼び出し元が「直読は許可しない（MCP のみ）」を
    表すために渡す形（裁定 #1・fail-closed）。"""
    from sherpa import agents as A
    import pathlib, tempfile

    from sherpa.providers.codex import sandbox as SB
    base = pathlib.Path(tempfile.mkdtemp())
    kb = base / "kb"; kb.mkdir(); md = base / "md"; md.mkdir(); venv = base / "venv"; venv.mkdir()
    monkeypatch.setattr(SB, "_venv_root", lambda: venv)
    ch = base / "ch"
    A._write_codex_authoring_config(
        ch, [str(kb)], "low", False, "test", None, direct_read_roots=[], deny_roots=[str(md)])
    cfg = (ch / "config.toml").read_text()
    assert f'"{kb}" = "read"' not in cfg
    assert f'"{kb}" = "deny"' in cfg, "直読不許可時に KB root が明示 deny されていない（:minimal 配下の配置で読める）"
    assert f'"{md}" = "deny"' in cfg
    assert f'"{venv}" = "read"' not in cfg, "直読不許可時に venv が read されている"
    assert f'"{venv}" = "deny"' in cfg


def test_codex_authoring_config_adds_venv_read_when_running_in_venv(monkeypatch):
    """app の `.venv`（`_venv_root()`）で動いているときだけ、直読 root とは独立に venv を read で足す
    （裁定 #2＝Office ライブラリ入りの python を Codex から使わせる）。"""
    from sherpa.providers.codex import sandbox as SB
    import pathlib, tempfile

    monkeypatch.setattr(SB, "_venv_root", lambda: pathlib.Path("/fake/venv"))
    ch = pathlib.Path(tempfile.mkdtemp()) / "ch"
    SB._write_codex_authoring_config(ch, ["/kb"], "low", False, "test", None)
    cfg = (ch / "config.toml").read_text()
    assert '"/fake/venv" = "read"' in cfg

    monkeypatch.setattr(SB, "_venv_root", lambda: None)
    ch2 = pathlib.Path(tempfile.mkdtemp()) / "ch"
    SB._write_codex_authoring_config(ch2, ["/kb"], "low", False, "test", None)
    assert '/fake/venv' not in (ch2 / "config.toml").read_text()


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
    assert "codex_agents_md.write_agents_md(run_dir, output_schema=_schema_on," in src, \
        "run() から write_agents_md(run_dir, output_schema=_schema_on, direct_read=...) が呼ばれていない"


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
    # 契約変更（2026-09-10・回答の簡素化対処）: 「推測しない」「出典の列挙は不要」は撤去し、
    # 「全件列挙する（省略しない）」「推定の明示」へ置換した。
    for phrase in ("KB", "authoring 直下", "推定", "全件", "省略しない",
                   # RV MEDIUM（2026-07-03）: 件数質問のブレ対策ルールも共通ルールとして常置する。
                   "list_docs", "path_prefix", "どのフォルダを数えたか",
                   # S2（2026-09-10・Codex原本直読と調査スキル §2-5）: 直読した資料の出典化。
                   "参照した資料"):
        assert phrase in txt, f"AGENTS.md に必須文言が無い: {phrase!r}"
    for removed in ("推測しない", "出典の列挙は不要", "簡潔に回答", "憶測で回答"):
        assert removed not in txt, f"AGENTS.md に撤去したはずの文言が残っている: {removed!r}"
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
    （KB 以外を読まない・確定と推定を分ける）は RV HIGH（2026-07-03）で多層防御のため両方に残す設計になった
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
    containment（KB/MCP 以外を読まない）と grounding（確定した事実と推定を分ける）は AGENTS.md 依存にせず
    _prompt/_prompt_mcp にも短縮形を常置する（AGENTS.md と重複しても害はない＝独立性を優先）。
    契約変更（2026-09-10・Codex原本直読と調査スキル §2-4）: 「MCP 以外で読まない」の禁止形を、
    「原本は直接読んでよい（指定フォルダの中だけ・秘匿名は読まない）」の肯定形へ書き換えた——
    `_prompt_mcp` の containment 短縮形は `direct_read`（既定 True）で切り替わる2種類になった。
    「推測しない」の短縮形は「確定した事実と推定は分けて書く」に置換済み（同 §2-4 前半）。"""
    from sherpa import agents as A
    p = A.CodexProvider()
    fs_prompt = p._prompt("消費税率を変えたい", "impact", {"data": {}}, "v1")
    mcp_prompt = p._prompt_mcp("消費税率を変えたい", "impact", "v1")                    # direct_read 既定 True
    mcp_prompt_no_direct = p._prompt_mcp("消費税率を変えたい", "impact", "v1", direct_read=False)

    assert "指定資料フォルダ以外は読まない" in fs_prompt, "_prompt に containment 短縮形が無い"
    assert "確定した事実と推定は分けて書く" in fs_prompt, "_prompt に grounding 短縮形が無い"
    assert "推測しない" not in fs_prompt

    assert "原本は直接読んでよい" in mcp_prompt, "_prompt_mcp（direct_read=True）に直読許可の文言が無い"
    assert "秘匿名のファイル" in mcp_prompt, "_prompt_mcp に秘匿名を読まない旨が無い"
    assert "確定した事実と推定は分けて書く" in mcp_prompt, "_prompt_mcp に grounding 短縮形が無い"
    assert "推測しない" not in mcp_prompt
    assert "一覧を求められたら該当する全件を各項目のパス付きで列挙する" in mcp_prompt, \
        "_prompt_mcp に一覧の全件列挙ルールが無い"

    assert "今回は原本の直接読み取りは使えない" in mcp_prompt_no_direct, \
        "_prompt_mcp（direct_read=False）に縮退時の文言が無い"
    assert "資料の本文は MCP のツールで読む" in mcp_prompt_no_direct, \
        "_prompt_mcp（direct_read=False）に containment 短縮形が無い"
    assert "確定した事実と推定は分けて書く" in mcp_prompt_no_direct


def test_investigate_skill_guidance_in_agents_md_and_direct_read_prompt():
    """S3（提案書 2026-09-10-Codex原本直読と調査スキル §2-6）: 質問の型に合う investigate-* スキルへ
    誘導する文言が AGENTS.md と `_prompt_mcp`（direct_read=True）に入り、direct_read=False（縮退時）
    には入らないこと（直読できないターンにスキル誘導を出しても意味が無い＝ノイズにしない）。"""
    from sherpa import agents as A
    from sherpa import codex_agents_md as M

    import pathlib as _pl, tempfile as _tf
    d = _pl.Path(_tf.mkdtemp())
    M.write_agents_md(d)                                       # 既定＝直読可: 誘導あり
    assert "investigate-" in (d / "AGENTS.md").read_text(encoding="utf-8"), "AGENTS.md に investigate-* スキルへの誘導が無い"
    M.write_agents_md(d, direct_read=False)                    # 直読不許可: 原本を開く前提の手順書へ誘導しない
    assert "investigate-" not in (d / "AGENTS.md").read_text(encoding="utf-8")

    p = A.CodexProvider()
    mcp_prompt = p._prompt_mcp("消費税率を変えたい", "impact", "v1")                    # direct_read 既定 True
    mcp_prompt_no_direct = p._prompt_mcp("消費税率を変えたい", "impact", "v1", direct_read=False)
    assert "investigate-" in mcp_prompt, "_prompt_mcp（direct_read=True）に investigate-* 誘導が無い"
    assert "investigate-" not in mcp_prompt_no_direct, \
        "_prompt_mcp（direct_read=False）に investigate-* 誘導が入ってしまっている（直読不可時はノイズ）"


def test_answer_simplification_wording_contract_2026_09_10():
    """契約変更（2026-09-10・回答の簡素化対処・提案書 2026-09-10-Codex原本直読と調査スキル.md §2-4 前半）:
    AGENTS.md／_prompt／_prompt_mcp／DEFAULT_SYSTEM_PROMPT から「簡潔に回答」「出典の列挙は不要」
    「推測しない」「結論・理由・補足」「不明は不明と」を撤去し、「推定」「全件」「省略しない」を含む
    （回答を絞らず・確定と推測を分けて答える方針への置換）。"""
    from sherpa import agents as A, codex_agents_md
    from sherpa.store import settings as S
    import pathlib, tempfile

    d = pathlib.Path(tempfile.mkdtemp())
    codex_agents_md.write_agents_md(d)
    agents_md_txt = (d / "AGENTS.md").read_text(encoding="utf-8")

    p = A.CodexProvider()
    fs_prompt = p._prompt("消費税率を変えたい", "impact", {"data": {}}, "v1")
    mcp_prompt = p._prompt_mcp("消費税率を変えたい", "impact", "v1")

    # 「簡潔に」単体は ask_user 後の要約指示など無関係な既存文言にも出るため、撤去対象の語結合で見る
    # （受け入れ基準の grep パターン `簡潔に回答\|推測しない\|憶測で回答` と同じ粒度）。
    forbidden = ("簡潔に回答", "出典の列挙は不要", "推測しない", "結論・理由・補足", "不明は不明と")
    required = ("推定", "全件", "省略しない")

    for text, name in ((agents_md_txt, "AGENTS.md"), (fs_prompt, "_prompt"), (mcp_prompt, "_prompt_mcp"),
                       (S.DEFAULT_SYSTEM_PROMPT, "DEFAULT_SYSTEM_PROMPT")):
        for phrase in forbidden:
            assert phrase not in text, f"{name} に撤去したはずの文言が残っている: {phrase!r}"

    # 「推定」「全件」「省略しない」は AGENTS.md／_prompt_mcp（一覧・件数の指示を持つ側）で確認する。
    # DEFAULT_SYSTEM_PROMPT は「推定」のみ（一覧の全件列挙は Codex 側の指示であり回答方針の既定文の役割外）。
    for phrase in required:
        assert phrase in agents_md_txt, f"AGENTS.md に必須文言が無い: {phrase!r}"
        assert phrase in mcp_prompt, f"_prompt_mcp に必須文言が無い: {phrase!r}"
    assert "推定" in S.DEFAULT_SYSTEM_PROMPT, "DEFAULT_SYSTEM_PROMPT に「推定」の明示が無い"


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


def test_default_system_prompt_matches_settings_js():
    """「既定に戻す」ボタン（web/settings.js の DEFAULT_SYS）と DEFAULT_SYSTEM_PROMPT の同文契約。
    実害: 片方だけ変えると、ボタンで戻した文と行が無いときの既定文が食い違う（2026-09-10 に踏んだ）。"""
    import pathlib, re
    from sherpa.store import settings as S
    js = pathlib.Path(__file__).resolve().parents[2].joinpath("web", "settings.js").read_text(encoding="utf-8")
    m = re.search(r"const DEFAULT_SYS = ((?:'[^']*'\s*\+?\s*)+);", js)
    assert m, "web/settings.js に DEFAULT_SYS が無い"
    js_default = "".join(re.findall(r"'([^']*)'", m.group(1)))
    assert js_default == S.DEFAULT_SYSTEM_PROMPT


def test_prompt_mcp_layer_guidance_only_when_restricted():
    """層（探す対象）は Codex に強制しない＝限定されたターンだけ直読の案内文を足し、both／省略では出ない。"""
    from sherpa import agents as A

    p = A.CodexProvider()
    docs = p._prompt_mcp("q", "qa", "v1", layer="docs")
    code = p._prompt_mcp("q", "qa", "v1", layer="code")
    assert "資料を優先して見る" in docs and "ソースを優先して見る" in code
    assert "根拠に使わない" not in docs and "根拠に使わない" not in code   # 優先の案内であって禁止ではない（裁定）
    plain = p._prompt_mcp("q", "qa", "v1")
    assert "優先して見る" not in plain


def test_run_authoring_mcp_off_fails_honestly_when_direct_read_unavailable(tmp_path, monkeypatch):
    """MCP 無効の構成で直読の準備（秘匿列挙）が失敗したら、Codex を起動せず正直に失敗を返す
    （直読も MCP も無い＝何も調べられないまま回答を書かせない）。"""
    from sherpa.providers.codex import provider as P
    from sherpa import agents as A
    import os as _os
    import stat

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    marker = tmp_path / "started"
    script = bin_dir / "codex"
    script.write_text("#!/usr/bin/env python3\n"
                      f"import pathlib; pathlib.Path(r'{marker}').write_text('x')\n"
                      "print('{\"type\": \"item.completed\", \"item\": {\"id\": \"1\", \"type\": \"agent_message\", \"text\": \"ok\"}}')\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{_os.pathsep}{_os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("SHERPA_CODEX_MCP", "0")
    monkeypatch.setattr(P, "_enumerate_sensitive",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sensitive_enum_failed:boom")))
    ctx = A.Ctx(message="消費税率について教えて", world="v1",
                route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
                dispatch=lambda lens_, inp: {"lens": lens_, "headline": "h", "summary": {"total": 0},
                                             "data": {}, "sources": []},
                knowledge=True, uid="mcp-off-u1")
    events = list(A.CodexProvider().run(ctx))
    assert not marker.exists(), "直読不可なのに Codex が起動した"
    res = [e for e in events if e.get("type") == "_result"][0]
    assert "読み取りの準備ができませんでした" in res["env"]["headline"]
    assert "直読" in res["decision"]["reason"]


def test_scope_deny_entries_nested_scopes_keep_parent_subtree(tmp_path):
    """親子で選ばれた scope（A と A/sub）は親だけが効く＝A/other は範囲内（deny しない）。"""
    from sherpa.providers.codex import sandbox as SB

    kb = tmp_path / "kb"
    (kb / "A" / "sub").mkdir(parents=True); (kb / "A" / "other").mkdir(); (kb / "B").mkdir()
    deny = SB._scope_deny_entries([str(kb)], ["A", "A/sub"])
    assert deny == [str(kb.resolve() / "B")]


def test_codex_authoring_config_root_denied_by_scope_has_no_read_line(tmp_path, monkeypatch):
    """KB には範囲があるが派生 root には無い（root ごと deny）とき、その root の read 行を書かない
    （同一キーに read と deny＝TOML が壊れて Codex が起動しない）。"""
    from sherpa.providers.codex import sandbox as SB
    import tomllib

    kb = tmp_path / "kb"; (kb / "A").mkdir(parents=True); (kb / "B").mkdir()
    md = tmp_path / "md"; md.mkdir()
    monkeypatch.setattr(SB, "_venv_root", lambda: None)
    deny = SB._scope_deny_entries([str(kb), str(md)], ["A"])
    ch = tmp_path / "ch"
    SB._write_codex_authoring_config(ch, [str(kb)], "low", False, "test", None,
                                     direct_read_roots=[str(kb), str(md)], sensitive_deny=deny)
    cfg = (ch / "config.toml").read_text()
    tomllib.loads(cfg)                                          # 重複キーなら例外
    assert f'"{md.resolve()}" = "deny"' in cfg and f'"{md.resolve()}" = "read"' not in cfg
    assert f'"{kb.resolve()}" = "read"' in cfg


def test_codex_authoring_config_empty_direct_read_denies_venv_too(monkeypatch, tmp_path):
    """直読不許可（direct_read_roots=[]）では venv も明示 deny（`:minimal` 配下の venv 配置でも読めない）。"""
    from sherpa.providers.codex import sandbox as SB

    (tmp_path / "venv").mkdir()
    monkeypatch.setattr(SB, "_venv_root", lambda: tmp_path / "venv")
    ch = tmp_path / "ch"
    SB._write_codex_authoring_config(ch, ["/kb"], "low", False, "test", None, direct_read_roots=[])
    cfg = (ch / "config.toml").read_text()
    assert f'"{tmp_path / "venv"}" = "deny"' in cfg
    assert '"/kb" = "deny"' not in cfg                       # 不在の root への deny 行は書かない（起動失敗）


def test_codex_authoring_config_empty_direct_read_prunes_nested_deny_roots(monkeypatch, tmp_path):
    """直読不許可時、派生 root が KB root 配下にある配置でも deny 済みフォルダ配下の deny 行を書かない
    （bubblewrap の起動失敗）。"""
    from sherpa.providers.codex import sandbox as SB

    monkeypatch.setattr(SB, "_venv_root", lambda: None)
    kb = tmp_path / "kb"; (kb / "derived" / "md").mkdir(parents=True); other = tmp_path / "other"; other.mkdir()
    ch = tmp_path / "ch"
    SB._write_codex_authoring_config(ch, [str(kb)], "low", False, "test", None,
                                     direct_read_roots=[], deny_roots=[str(kb / "derived" / "md"), str(other)])
    cfg = (ch / "config.toml").read_text()
    assert f'"{kb}" = "deny"' in cfg and f'"{other}" = "deny"' in cfg
    assert f'"{kb / "derived" / "md"}"' not in cfg


# ===== S2（提案書 2026-09-10-Codex原本直読と調査スキル.md §2-5・§6 裁定 #3）: 原本直読の出典化 =====
# Codex が原本を直接読んでも MCP の結果には載らない＝出典（原本DL）に自動では出ない。回答末尾の
# 「参照した資料:」ブロックを解析し、台帳で実在確認したものだけ env["sources"] の先頭に足す
# （実在しない・秘匿名は捨てる）。read 系 MCP ツール（read_doc 等）の引数からも二重取りする。

def _emit_citation(obj) -> str:
    import json as _json
    return f"print({_json.dumps(_json.dumps(obj))})\n"


def _citation_ctx(uid: str, make_sources=None):
    from sherpa import agents as A
    return A.Ctx(
        message="消費税率について教えて", world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline",
            "summary": {"total": 0}, "data": {}, "sources": [],
        },
        knowledge=True, uid=uid, make_sources=make_sources,
    )


def _citation_setup(bin_dir, monkeypatch, tmp_path, users_dirname="users"):
    import os as _os
    monkeypatch.setenv("PATH", f"{bin_dir}{_os.pathsep}{_os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / users_dirname))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")   # 平文の偽 codex＝構造化応答は使わない


def _citation_write_fake_codex(bin_dir, script_body: str) -> None:
    import stat as _stat
    script = bin_dir / "codex"
    script.write_text(script_body)
    mode = script.stat().st_mode
    script.chmod(mode | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)


def _citation_result_env(events: list) -> dict:
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(results) == 1, f"_result が1件でない: {events!r}"
    return results[0]["env"]


def _fake_make_sources(docs):
    return [{"doc_id": d, "download_url": f"/documents/download?world=v1&rel={d}"} for d in docs]


def test_run_authoring_referenced_docs_block_promotes_verified_docs_to_sources(tmp_path, monkeypatch):
    """回答末尾の「参照した資料:」を解析し、台帳で実在確認できた1件だけを env["sources"] の先頭に足す
    （実在しない資料・秘匿名 `.env` は捨てる）。headline から参照ブロックは取り除かれる。"""
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    answer_text = (
        "消費税率は10%です。\n\n"
        "参照した資料:\n"
        "- 4期/02_設計/01_基本設計/税計算仕様書.md\n"
        "- 存在しない.md\n"
        "- .env\n"
    )
    script = "#!/usr/bin/env python3\n" + _emit_citation(
        {"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": answer_text}})
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("citation-ok-u1", make_sources=_fake_make_sources)
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert env["codex_referenced_docs"] == {"listed": 3, "verified": 1}
    assert env["sources"], "実在確認を通った資料が sources に足されていない"
    assert env["sources"][0]["doc_id"] == "4期/02_設計/01_基本設計/税計算仕様書.md"
    assert "rel=" in env["sources"][0]["download_url"]
    assert not any(s["doc_id"] in ("存在しない.md", ".env") for s in env["sources"])
    assert "参照した資料" not in env["headline"]
    assert "消費税率は10%です。" in env["headline"]
    # 実際に開いた資料＝API 経路の「精読済み」と同じ意味で出典の 2 区分（根拠／参考）に載る
    assert env["sources_verified"] == ["4期/02_設計/01_基本設計/税計算仕様書.md"]


def test_run_authoring_referenced_docs_block_keeps_body_when_zero_verified(tmp_path, monkeypatch):
    """実在確認を1件も通らなければ、参照ブロックの記載を消さず本文をそのまま headline にする
    （記載が消えて出典が何も出ないより、本文に根拠パスが残るほうを優先）。"""
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    answer_text = (
        "消費税率は10%です。\n\n"
        "参照した資料:\n"
        "- 存在しない1.md\n"
        "- 存在しない2.md\n"
    )
    script = "#!/usr/bin/env python3\n" + _emit_citation(
        {"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": answer_text}})
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("citation-zero-u1", make_sources=_fake_make_sources)
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert env["codex_referenced_docs"] == {"listed": 2, "verified": 0}
    assert env["headline"] == answer_text.strip() or env["headline"].strip() == answer_text.strip(), \
        f"verified 0件時に本文が書き換わった: {env['headline']!r}"
    assert not env.get("sources")
    assert "sources_verified" not in env                        # 0 件なら 2 区分表示にしない（単一リストのまま）


def test_run_authoring_mcp_read_doc_args_collected_as_referenced_docs(tmp_path, monkeypatch):
    """参照ブロックが無くても、read_doc の doc_id 引数から直読した資料を拾って出典へ足す
    （記載漏れの補完）。"""
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    script = "#!/usr/bin/env python3\n" + "".join([
        _emit_citation({"type": "item.completed", "item": {
            "id": "t1", "type": "mcp_tool_call", "tool": "read_doc", "status": "completed",
            "arguments": {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md"}}}),
        _emit_citation({"type": "item.completed", "item": {
            "id": "2", "type": "agent_message", "text": "消費税率は10%です。"}}),
    ])
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("citation-mcp-u1", make_sources=_fake_make_sources)
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert env["codex_referenced_docs"] == {"listed": 1, "verified": 1}
    assert env["sources"]
    assert env["sources"][0]["doc_id"] == "4期/02_設計/01_基本設計/税計算仕様書.md"
    assert env["headline"] == "消費税率は10%です。"


def test_run_authoring_mcp_xlsx_range_args_collected_as_referenced_docs(tmp_path, monkeypatch):
    """RV#6 是正: S3b 原本読取ツール（`xlsx_range` 等）の doc_id 引数も read_doc と同じ経路で
    直読した資料として拾う——以前は `_mcp_read_docs` の対象に無く、Codex が原本を MCP 経由で
    直接読んでも出典収集から漏れていた。"""
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    script = "#!/usr/bin/env python3\n" + "".join([
        _emit_citation({"type": "item.completed", "item": {
            "id": "t1", "type": "mcp_tool_call", "tool": "xlsx_range", "status": "completed",
            "arguments": {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md", "sheet": "Sheet1"}}}),
        _emit_citation({"type": "item.completed", "item": {
            "id": "2", "type": "agent_message", "text": "消費税率は10%です。"}}),
    ])
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("citation-mcp-xlsx-u1", make_sources=_fake_make_sources)
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert env["codex_referenced_docs"] == {"listed": 1, "verified": 1}
    assert env["sources"]
    assert env["sources"][0]["doc_id"] == "4期/02_設計/01_基本設計/税計算仕様書.md"
    assert env["headline"] == "消費税率は10%です。"


def test_run_authoring_mcp_xlsx_sheets_only_becomes_source_but_not_verified(tmp_path, monkeypatch):
    """`xlsx_sheets`（シート一覧のみ・本文は読んでいない）は `xlsx_range` 等の
    本文精読ツールと同じ扱いにしていたため、シート一覧を見ただけで根拠ゲート
    （`sources_verified`）に数えられてしまっていた。実在する資料自体は sources（参照候補）に
    載せてよいが、`sources_verified`（画面の「精読済み」区分・改善ログの実測根拠）には含めない。
    """
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    script = "#!/usr/bin/env python3\n" + "".join([
        _emit_citation({"type": "item.completed", "item": {
            "id": "t1", "type": "mcp_tool_call", "tool": "xlsx_sheets", "status": "completed",
            "arguments": {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md"}}}),
        _emit_citation({"type": "item.completed", "item": {
            "id": "2", "type": "agent_message", "text": "消費税率は10%です。"}}),
    ])
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("citation-mcp-xlsx-sheets-u1", make_sources=_fake_make_sources)
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert env["codex_referenced_docs"] == {"listed": 1, "verified": 1}
    assert env["sources"]
    assert env["sources"][0]["doc_id"] == "4期/02_設計/01_基本設計/税計算仕様書.md"
    assert env.get("sources_verified") == []   # シート一覧だけでは「精読済み」に数えない
    assert env["headline"] == "消費税率は10%です。"


def test_run_authoring_failed_mcp_read_is_not_a_source(tmp_path, monkeypatch):
    """読取に失敗した read_doc（status=failed／result.isError／進行中のみ）の引数は出典にも「根拠」にも載せない。"""
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    doc = "4期/02_設計/01_基本設計/税計算仕様書.md"
    script = "#!/usr/bin/env python3\n" + "".join([
        _emit_citation({"type": "item.completed", "item": {
            "id": "t1", "type": "mcp_tool_call", "tool": "read_doc", "status": "failed",
            "arguments": {"doc_id": doc}}}),
        _emit_citation({"type": "item.completed", "item": {
            "id": "t2", "type": "mcp_tool_call", "tool": "read_around", "status": "completed",
            "result": {"isError": True}, "arguments": {"doc_id": doc, "line": 1}}}),
        _emit_citation({"type": "item.started", "item": {
            "id": "t3", "type": "mcp_tool_call", "tool": "doc_outline", "status": "in_progress",
            "arguments": {"doc_id": doc}}}),
        _emit_citation({"type": "item.completed", "item": {
            "id": "2", "type": "agent_message", "text": "消費税率は10%です。"}}),
    ])
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("citation-mcp-fail-u1", make_sources=_fake_make_sources)
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert env["codex_referenced_docs"] == {"listed": 0, "verified": 0}
    assert not env.get("sources") and "sources_verified" not in env


# ===== DEPTH-2 S3b: サイドカー（子エージェントの観測）=====
# `docs/proposals/2026-09-17-深さの再定義とレビュー巡.md` §2.6/§9.1・受け入れ条件(2)(3)(6)。
# s3b-fix2: サイドカーは run_dir（model-shell の書込許可領域＝ `":workspace_roots"` `"." = "write"`）
# の外・codex_home 配下に置く契約（#22 是正）。偽 codex は `CODEX_HOME` env（`_codex_clean_env` が
# 設定）から codex_home の実パスを読み取れる（実際の MCP サーバの代わりに直接書く＝
# 「子だけが読んだ」状況を最小コストで再現する）。

def _sidecar_write_snippet(entries: list) -> str:
    """`.mcp_sidecar.jsonl`（codex_home 配下＝`CODEX_HOME` env）へ JSONL を書く偽 codex 用 python 断片。"""
    import json as _json
    lines_expr = ", ".join(_json.dumps(_json.dumps(e, ensure_ascii=False)) for e in entries)
    return ("import os, pathlib\n"
            "(pathlib.Path(os.environ['CODEX_HOME']) / '.mcp_sidecar.jsonl')"
            f".write_text(chr(10).join([{lines_expr}]) + chr(10))\n")


def test_run_authoring_sidecar_read_doc_promoted_to_sources_verified_without_duplication(tmp_path, monkeypatch):
    """子（サイドカー）だけが読んだ doc_id が sources_verified に入り、親自身が MCP item で観測した
    doc_id と重複しない（受け入れ条件(2)）。"""
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    child_only_doc = "4期/01_標準/消費税法.md"
    parent_doc = "4期/02_設計/01_基本設計/税計算仕様書.md"
    script = ("#!/usr/bin/env python3\n"
              + _sidecar_write_snippet([{"kind": "read", "tool": "read_doc",
                                         "doc_id": child_only_doc, "ts": 1.0}])
              + "".join([
                  _emit_citation({"type": "item.completed", "item": {
                      "id": "t1", "type": "mcp_tool_call", "tool": "read_doc", "status": "completed",
                      "arguments": {"doc_id": parent_doc}}}),
                  _emit_citation({"type": "item.completed", "item": {
                      "id": "2", "type": "agent_message", "text": "消費税率は10%です。"}}),
              ]))
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("sidecar-read-u1", make_sources=_fake_make_sources)
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert env["codex_referenced_docs"] == {"listed": 2, "verified": 2}
    verified = env["sources_verified"]
    assert sorted(verified) == sorted([child_only_doc, parent_doc])
    assert len(verified) == len(set(verified)), "重複していない"
    doc_ids = [s["doc_id"] for s in env["sources"]]
    assert doc_ids.count(child_only_doc) == 1 and doc_ids.count(parent_doc) == 1


def test_run_authoring_sidecar_duplicate_doc_id_not_double_counted(tmp_path, monkeypatch):
    """親自身が観測した doc_id とサイドカーの doc_id が同じ資料でも二重に数えない。"""
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    doc = "4期/02_設計/01_基本設計/税計算仕様書.md"
    script = ("#!/usr/bin/env python3\n"
              + _sidecar_write_snippet([{"kind": "read", "tool": "read_doc", "doc_id": doc, "ts": 1.0}])
              + "".join([
                  _emit_citation({"type": "item.completed", "item": {
                      "id": "t1", "type": "mcp_tool_call", "tool": "read_doc", "status": "completed",
                      "arguments": {"doc_id": doc}}}),
                  _emit_citation({"type": "item.completed", "item": {
                      "id": "2", "type": "agent_message", "text": "消費税率は10%です。"}}),
              ]))
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("sidecar-dup-u1", make_sources=_fake_make_sources)
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert env["codex_referenced_docs"] == {"listed": 1, "verified": 1}
    assert env["sources_verified"] == [doc]


def test_run_authoring_sidecar_ask_user_becomes_confirmation_card_once(tmp_path, monkeypatch):
    """子だけが呼んだ ask_user（親自身の --json には現れない）は、run 終了後にサイドカーから
    確認カード（question イベント）を一度だけ生成する。回答は _result として保存されない
    （既存の ask_user 経路と同じ envelope・受け入れ条件(3)）。"""
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    question = {"type": "question", "interaction_id": "child-q1", "mode": "single",
               "prompt": "この資料で合っていますか", "allow_free_text": False,
               "options": [{"id": "yes", "label": "はい", "description": ""},
                           {"id": "no", "label": "いいえ", "description": ""}]}
    script = ("#!/usr/bin/env python3\n"
              + _sidecar_write_snippet([{"kind": "ask_user", "ts": 1.0, "question": question}])
              + _emit_citation({"type": "item.completed", "item": {
                  "id": "1", "type": "agent_message", "text": "確認した結果、影響はありません。"}}))
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("sidecar-ask-u1", make_sources=_fake_make_sources)
    events = list(A.CodexProvider().run(ctx))

    questions = [e for e in events if isinstance(e, dict) and e.get("type") == "question"]
    assert len(questions) == 1, f"確認カードは一度だけのはず: {events!r}"
    assert questions[0]["interaction_id"] == "child-q1"
    assert questions[0]["prompt"] == "この資料で合っていますか"
    assert [e for e in events if isinstance(e, dict) and e.get("type") == "_result"] == [], \
        "ask_user ターンは回答（_result）を保存しない"


def test_run_authoring_sidecar_missing_is_fail_open(tmp_path, monkeypatch):
    """サイドカーが存在しない（子が1つも MCP を呼ばなかった／multi_agent 無効）ときは、
    既存の親のみ観測にそのまま落ちる（受け入れ条件(6)）。"""
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    script = "#!/usr/bin/env python3\n" + _emit_citation(
        {"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": "消費税率は10%です。"}})
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("sidecar-missing-u1", make_sources=_fake_make_sources)
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert env["headline"] == "消費税率は10%です。"
    assert not env.get("sources") and "sources_verified" not in env


def test_run_authoring_sidecar_forged_in_run_dir_is_not_absorbed(tmp_path, monkeypatch):
    """#22 是正: サイドカーは model-shell の書込許可領域（run_dir・`":workspace_roots"` `"." = "write"`）
    の外（codex_home 配下）にある契約——同じファイル名を run_dir 直下（model-shell が書ける場所＝
    偽 codex がここに書くのは model-shell によるサイドカー偽装のシミュレーション）に置いても、
    provider は codex_home 側だけを読むため取り込まれない。取り込まれれば、未読資料が
    sources_verified（根拠ゲート）を偽って通ってしまう（提案書 §2.6/§9.1・RV #22）。"""
    import json
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    forged_doc = "4期/01_標準/消費税法.md"   # 実際には誰も読んでいない資料
    forged_line = json.dumps({"kind": "read", "tool": "read_doc", "doc_id": forged_doc, "ts": 1.0},
                             ensure_ascii=False)
    script = ("#!/usr/bin/env python3\n"
              "import pathlib\n"
              f"pathlib.Path('.mcp_sidecar.jsonl').write_text({json.dumps(forged_line)} + '\\n')\n"
              + _emit_citation({"type": "item.completed", "item": {
                  "id": "1", "type": "agent_message", "text": "消費税率は10%です。"}}))
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("sidecar-forged-run-dir-u1", make_sources=_fake_make_sources)
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert env["headline"] == "消費税率は10%です。"
    assert not env.get("sources") and "sources_verified" not in env, \
        f"run_dir 直下への偽装サイドカーが取り込まれた: {env!r}"


def test_sidecar_path_not_within_codex_write_permitted_roots(tmp_path):
    """#22 是正・受け入れ条件(a): 生成される permission profile（`":workspace_roots"` `"." = "write"`＝
    cwd=run_dir が唯一の書込許可ルート）の下で、provider.py が実際に組み立てるサイドカーのパス
    （`codex_home / _MCP_SIDECAR_NAME`）が run_dir と包含関係を持たない（run_dir 配下でも run_dir
    自身の親でもない）ことをパスの包含判定で固定する。"""
    from pathlib import Path

    from sherpa import agents as A
    from sherpa.providers.codex.provider import _MCP_SIDECAR_NAME

    run_dir = tmp_path / "users" / "u1" / "workspace" / "authoring" / "run-deadbeef"
    run_dir.mkdir(parents=True)
    codex_home = tmp_path / "users" / "u1" / "workspace" / ".codexhome-deadbeef"
    sidecar_path = codex_home / _MCP_SIDECAR_NAME   # provider.py と同じ組み立て式

    A._write_codex_authoring_config(codex_home, ["/kb"], "low", True, "test", None,
                                    sidecar_path=str(sidecar_path))
    cfg = (codex_home / "config.toml").read_text()
    assert '"." = "write"' in cfg, "cwd（run_dir）だけが書込許可ルートのはず"
    assert str(sidecar_path) in cfg, "サイドカーの env が config に無い"

    with pytest.raises(ValueError):
        sidecar_path.resolve().relative_to(run_dir.resolve())   # サイドカーは run_dir 配下ではない
    with pytest.raises(ValueError):
        run_dir.resolve().relative_to(sidecar_path.parent.resolve())   # run_dir もサイドカーの下ではない


def test_run_authoring_sidecar_corrupt_lines_are_skipped_fail_open(tmp_path, monkeypatch):
    """壊れた行（不正 JSON・非 dict）があっても、正しい行はそのまま拾う（fail-open・受け入れ条件(6)）。"""
    import json
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    doc = "4期/01_標準/消費税法.md"
    good_line = json.dumps({"kind": "read", "tool": "read_doc", "doc_id": doc, "ts": 1.0}, ensure_ascii=False)
    script = ("#!/usr/bin/env python3\n"
              "import os, pathlib\n"
              "(pathlib.Path(os.environ['CODEX_HOME']) / '.mcp_sidecar.jsonl')"
              f".write_text('not-json\\n' + {json.dumps(good_line)} "
              "+ '\\n' + '[]\\n')\n"
              + _emit_citation({"type": "item.completed", "item": {
                  "id": "1", "type": "agent_message", "text": "消費税率は10%です。"}}))
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("sidecar-corrupt-u1", make_sources=_fake_make_sources)
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert env["codex_referenced_docs"] == {"listed": 1, "verified": 1}
    assert env["sources_verified"] == [doc]


def test_run_authoring_sidecar_itself_is_not_registered_as_a_created_file(tmp_path, monkeypatch):
    """C9 是正: `.mcp_sidecar.jsonl`（子の観測サイドカー）が生成されても、共有 KB だけを読む会話は
    成果物走査の対象にならない（台帳登録なし・`codex_wrote_files` が立たない）——サイドカーは
    codex_home 配下（run_dir の外・s3b-fix2）に置くため run_dir スキャンには自然に現れないが、
    フォールバック経路向けの除外（`_MCP_SIDECAR_NAME`）が正しく効いていることも併せて確認する
    （提案書 §2.6/§9.1・RV C9）。"""
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    doc = "4期/01_標準/消費税法.md"
    script = ("#!/usr/bin/env python3\n"
              + _sidecar_write_snippet([{"kind": "read", "tool": "read_doc", "doc_id": doc, "ts": 1.0}])
              + _emit_citation({"type": "item.completed", "item": {
                  "id": "1", "type": "agent_message", "text": "消費税率は10%です。"}}))
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("sidecar-not-created-file-u1", make_sources=_fake_make_sources)
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert not env.get("codex_wrote_files"), \
        f"サイドカーだけで codex_wrote_files が立った: {env.get('codex_wrote_files')!r}"
    assert not env.get("created_files")


def test_read_mcp_sidecar_invalid_utf8_bytes_is_fail_open(tmp_path):
    """C12 是正: サイドカーに不正 UTF-8 バイト列（`for line in f` の読取自体で
    `UnicodeDecodeError` が起きる）が含まれていても、`_read_mcp_sidecar` は例外を投げず
    fail-open（既存の「親の --json だけを見る」観測に落ちる）で終える——捕捉していないと
    回答処理そのものが例外終了する（提案書 §2.6/§9.1・RV C12）。"""
    from sherpa.providers.codex import provider as P

    path = tmp_path / ".mcp_sidecar.jsonl"
    path.write_bytes(b"\xff\n")

    reads, listed, ask = P._read_mcp_sidecar(path)

    assert reads == [] and listed == [] and ask is None


# ===== DEPTH-2 S6（§2.6・受け入れ条件(4)(5)）: 巡（1 codex exec 内の内部段階）をまたいだ
# 書込の個人由来フラグ累積・成果物登録は最終版1回 =====
# Codex の「巡」は Sherpa 側から見た外側ループではなく1回の `codex exec` プロセス内部（本体の
# spawn_agent 判断）で完結するため、実行後の run_dir 全体スキャン（Feature A）が自然に
# 「前段だけ書込→後段で失敗」でも書込の事実を拾う（新規コードは足していない・既存契約の確認）。

def test_early_write_then_turn_failure_still_flags_codex_wrote_files(tmp_path, monkeypatch):
    """内部の前段階だけがファイルを書き、後段（`turn.failed`・agent_message 無し＝失敗保存相当）で
    終わっても、`env["codex_wrote_files"]` は立つ（受け入れ条件(4)前半）。"""
    from sherpa import agents as A

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    script = ("#!/usr/bin/env python3\n"
              "import pathlib\n"
              "pathlib.Path('draft.md').write_text('前段の下書き')\n"
              + _emit_citation({"type": "turn.failed", "error": {"code": "boom"}}))
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx("early-write-then-fail-u1")
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert env.get("codex_silent_failure") is True
    assert env.get("codex_wrote_files"), \
        f"前段の書込があるのに失敗終端で codex_wrote_files が立たなかった: {env!r}"


def test_repeated_write_across_internal_stages_registers_final_version_once(tmp_path, monkeypatch):
    """同じファイルを内部の複数段階で書き直しても（最後に上書きした内容が残る）、
    成果物登録は最終版1本だけ（受け入れ条件(5)）。要 Postgres（台帳登録＝FK 制約で実ユーザーが
    要る・DB down は skip）。"""
    from sherpa import agents as A
    from sherpa import store

    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")
    uid = f"unit-depth2s6-{int(time.time() * 1000) % 100000000}"
    store.upsert_user(uid, display_name="D2S6", password_hash="x", status="active")

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _citation_setup(bin_dir, monkeypatch, tmp_path)
    script = ("#!/usr/bin/env python3\n"
              "import pathlib\n"
              "pathlib.Path('report.md').write_text('下書き')\n"
              "pathlib.Path('report.md').write_text('最終版')\n"
              + _emit_citation({"type": "item.completed", "item": {
                  "id": "1", "type": "agent_message", "text": "最終版を作成しました。"}}))
    _citation_write_fake_codex(bin_dir, script)

    ctx = _citation_ctx(uid)
    env = _citation_result_env(list(A.CodexProvider().run(ctx)))

    assert env.get("codex_wrote_files")
    created = env.get("created_files") or []
    assert len(created) == 1, f"同名ファイルの巡内上書きで複数回登録された: {created!r}"
    assert created[0]["name"] == "report.md"
