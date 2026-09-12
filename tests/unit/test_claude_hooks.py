"""`scripts/claude-hooks/deny-secrets.sh`（Claude Code PreToolUse フック）の単体テスト。

過去セッションで Azure API キーを端末に印字した実害の再発防止として、拒否/許可の境界を固定する
（CLAUDE.md「### セキュリティ」節・docs/20-開発ハーネス.md）。フックへ渡る JSON（`tool_input.command`）
を stdin から流し込み、拒否は exit 2、許可は exit 0 を検証する。
"""
from __future__ import annotations

import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "claude-hooks" / "deny-secrets.sh"


def _run(command: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ===== 拒否（機密の端末印字） =====

def test_denies_cat_env():
    r = _run("cat .env")
    assert r.returncode == 2
    assert ".env" in r.stderr


def test_denies_printenv():
    r = _run("printenv")
    assert r.returncode == 2


def test_denies_echo_secret_var():
    r = _run("echo $DATABASE_PASSWORD")
    assert r.returncode == 2


def test_denies_source_env():
    r = _run("source .env")
    assert r.returncode == 2


def test_denies_bare_env():
    r = _run("env")
    assert r.returncode == 2


def test_denies_cat_env_glob():
    r = _run("cat .env*")
    assert r.returncode == 2


def test_denies_echo_command_substitution():
    r = _run('echo "$(cat .env)"')
    assert r.returncode == 2


def test_denies_timeout_wrapped_cat():
    r = _run("timeout 5 cat .env")
    assert r.returncode == 2


def test_denies_nl_env():
    r = _run("nl .env")
    assert r.returncode == 2


def test_denies_python_open_env():
    r = _run('python3 -c "print(open(\'.env\').read())"')
    assert r.returncode == 2


# ===== 許可（誤検知にしない） =====

def test_allows_ls():
    r = _run("ls -la")
    assert r.returncode == 0


def test_allows_wc_on_env():
    r = _run("wc -l .env")
    assert r.returncode == 0


def test_allows_grep_count_only():
    r = _run("grep -c ERROR .env")
    assert r.returncode == 0


def test_allows_grep_combined_count_flag():
    r = _run("grep -ic KEY .env")
    assert r.returncode == 0


def test_allows_env_example():
    r = _run("cat .env.example")
    assert r.returncode == 0


def test_allows_env_with_args():
    r = _run("env FOO=bar python3 script.py")
    assert r.returncode == 0


def test_allows_cp_from_example():
    r = _run("cp .env.example .env")
    assert r.returncode == 0


def test_allows_sed_in_place():
    r = _run("sed -i s/a/b/ .env")
    assert r.returncode == 0


def test_denies_sed_display_without_in_place():
    r = _run("sed -n '1p' .env")
    assert r.returncode == 2


def test_allows_echo_prefix_slice():
    r = _run('echo "${OPENAI_API_KEY:0:4}"')
    assert r.returncode == 0


def test_allows_echo_var_length():
    r = _run("echo ${#OPENAI_API_KEY}")
    assert r.returncode == 0


def test_allows_cut_prefix():
    r = _run("cut -c1-4 .env")
    assert r.returncode == 0


def test_denies_cut_that_dumps_env_contents():
    # フィールド抽出や広い範囲は中身の表示＝保険判定へ落ちて拒否
    assert _run("cut -d= -f2- .env").returncode == 2
    assert _run("cut -c1-200 .env").returncode == 2


def test_allows_other_env_named_files_by_path_boundary():
    # `azure.env`（別名の env ファイル）や `foo.envelope` は .env 系ではない
    assert _run('make azure-smoke ARGS="--env-file azure.env --yes"').returncode == 0
    assert _run("cat foo.envelope").returncode == 0


def test_denies_wide_partial_expansion_of_secret_var():
    # 接頭辞確認の範囲（8 文字）を超える部分展開は鍵全体を出せる
    assert _run('echo "${OPENAI_API_KEY:0:200}"').returncode == 2
    assert _run('echo "${OPENAI_API_KEY:0:8}"').returncode == 0


def test_denies_copy_of_env_to_terminal_device():
    assert _run("cp .env /dev/stdout").returncode == 2
    assert _run("mv .env /dev/tty").returncode == 2
    assert _run("cp .env .env.bak").returncode == 0


def test_hook_emits_no_syntax_warning():
    r = _run("ls")
    assert "SyntaxWarning" not in (r.stderr or "")


def test_denies_non_prefix_partial_expansion_of_secret_var():
    # offset 付きのスライスは 8 文字ずつ連結して全体を出せる＝接頭辞（offset 0）だけ許す
    assert _run("echo ${OPENAI_API_KEY:8:8}${OPENAI_API_KEY:16:8}").returncode == 2


def test_denies_copy_of_env_to_proc_fd():
    assert _run("cp .env /proc/self/fd/1").returncode == 2


def test_denies_env_read_via_input_redirect():
    assert _run("cat <.env").returncode == 2
    assert _run("echo $(<.env)").returncode == 2
    assert _run("tr -d x <.env").returncode == 2


def test_denies_command_substitution_inside_allowlisted_commands():
    # allowlist のコマンドでも、引数のコマンド置換で中身が端末（stderr 含む）に出る形は拒否
    assert _run("ls $(cat .env)").returncode == 2
    assert _run("wc -c $(cat .env)").returncode == 2
    assert _run("cut -c1-8 $(cat .env)").returncode == 2
    assert _run("grep -c x $(cat .env)").returncode == 2
    # 置換を含まない正当操作は従来どおり
    assert _run("cp .env /tmp/x").returncode == 0
    assert _run("ls $(pwd)").returncode == 0
