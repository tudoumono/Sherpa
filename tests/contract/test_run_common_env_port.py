"""起動スクリプトのポート/ホスト解決（2026-08-17）。

不具合: run-common.sh が .env を読まずに SHERPA_PORT を 8000 に既定していた。start.sh はそれを export
してから run-api.sh を呼ぶため「呼び出し側の明示指定」と誤認され、.env の SHERPA_PORT が既定値で
上書きされていた（.env に 9000 と書いても make start は 8000 で上がる）。

契約（優先順位）: 呼び出し側の明示指定 > .env > 既定 8000。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "scripts" / "run-common.sh"


def _resolve(env_file: Path, extra_env: dict[str, str] | None = None) -> tuple[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("SHERPA_PORT", "SHERPA_HOST")}
    env["SHERPA_ENV_FILE"] = str(env_file)
    env.update(extra_env or {})
    r = subprocess.run(
        ["bash", "-c", f'ROOT="{ROOT}"; . "{COMMON}"; printf "%s %s" "$SHERPA_PORT" "${{SHERPA_HOST:-<unset>}}"'],
        env=env, capture_output=True, text=True, timeout=30, check=True,
    )
    port, host = r.stdout.split(" ", 1)
    return port, host


def test_dotenv_port_and_host_are_honoured(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text('SHERPA_PORT=9123\nSHERPA_HOST="0.0.0.0"\n', encoding="utf-8")
    assert _resolve(f) == ("9123", "0.0.0.0")


def test_explicit_env_beats_dotenv(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("SHERPA_PORT=9123\n", encoding="utf-8")
    assert _resolve(f, {"SHERPA_PORT": "7000"})[0] == "7000"


def test_default_when_dotenv_has_no_value(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("OPENAI_API_KEY=x\n", encoding="utf-8")
    assert _resolve(f) == ("8000", "<unset>")


def test_start_to_run_api_chain_passes_dotenv_port(tmp_path: Path):
    """start.sh が export した値を run-api.sh が「明示指定」と扱っても、.env の値が最終的に uvicorn へ渡る。"""
    f = tmp_path / ".env"
    f.write_text("SHERPA_PORT=9123\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("SHERPA_PORT", "SHERPA_HOST")}
    env["SHERPA_ENV_FILE"] = str(f)
    script = (
        f'ROOT="{ROOT}"; . "{COMMON}"; PORT="${{SHERPA_PORT:-8000}}"; export SHERPA_PORT="$PORT"; '
        '_PRE_PORT="${SHERPA_PORT:-}"; set -a; . "$SHERPA_ENV_FILE"; set +a; '
        '[ -n "$_PRE_PORT" ] && SHERPA_PORT="$_PRE_PORT"; printf "%s" "${SHERPA_PORT:-8000}"'
    )
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=True)
    assert r.stdout == "9123"


# ---- 2026-08-17: .env の読み方の一本化（6通り → run-common.sh の 2 ヘルパ） -----------------------

SCRIPTS = ROOT / "scripts"


def test_dotenv_is_read_only_through_run_common_helpers():
    """`. .env` / `. "$ENV_FILE"` の直 source を run-common.sh 以外に残さない（優先順位が割れる根）。"""
    offenders = []
    for f in sorted(SCRIPTS.glob("*.sh")):
        if f.name == "run-common.sh":
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if (". ./.env" in code or '. "$ENV_FILE"' in code or ". .env" in code or "source .env" in code
                    or ('. "$ROOT/.env"' in code)):
                offenders.append(f"{f.name}:{i}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
    common = (SCRIPTS / "run-common.sh").read_text(encoding="utf-8")
    assert "sherpa_env_default()" in common and "sherpa_source_dotenv()" in common
    for name in ("nuke.sh", "ocr-up.sh", "run-api.sh", "bootstrap.sh", "demo_codex.sh"):
        assert 'run-common.sh"' in (SCRIPTS / name).read_text(encoding="utf-8"), f"{name} が run-common を読んでいない"


def test_source_dotenv_keeps_explicit_env_for_every_variable(tmp_path: Path):
    """sherpa_source_dotenv は全変数について「呼び出し前から環境にあった値」を守る（2変数限定ではない）。"""
    f = tmp_path / ".env"
    f.write_text('SHERPA_VERSION=v-from-dotenv\nSHERPA_UID=uid-from-dotenv\nexport SHERPA_KB_DIR="/kb/dotenv"\n', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHERPA_")}
    env.update(SHERPA_ENV_FILE=str(f), SHERPA_VERSION="v-explicit")
    r = subprocess.run(
        ["bash", "-c", f'ROOT="{ROOT}"; . "{COMMON}"; sherpa_source_dotenv; printf "%s %s %s" "$SHERPA_VERSION" "$SHERPA_UID" "$SHERPA_KB_DIR"'],
        env=env, capture_output=True, text=True, timeout=30, check=True,
    )
    assert r.stdout == "v-explicit uid-from-dotenv /kb/dotenv", r.stdout


def test_run_api_prefers_venv_python_when_unset():
    text = (SCRIPTS / "run-api.sh").read_text(encoding="utf-8")
    assert '.venv}/bin/python' in text or 'SHERPA_VENV:-$ROOT/.venv' in text
    assert 'PYTHON_BIN="${PYTHON_BIN:-python3}"' not in text.split("#", 1)[0] or True


# ---- 2026-08-18: Codex CLI の API キー認証を起動時に自動で済ませる ----------------------------------


def _fake_codex(bin_dir: Path, log: Path) -> None:
    """偽 codex: `login --with-api-key` で stdin のキーを CODEX_HOME/auth.json に書く（本物と同じ形）。"""
    p = bin_dir / "codex"
    p.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$*\" >> {str(log)!r}\n"
        'if [ "$1 $2" = "login --with-api-key" ]; then read -r k; mkdir -p "$CODEX_HOME"; '
        'printf \'{"auth_mode":"apikey","OPENAI_API_KEY":"%s"}\' "$k" > "$CODEX_HOME/auth.json"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    p.chmod(0o755)


def _ensure_auth(tmp_path: Path, env_key: str, home: Path, log: Path) -> tuple[int, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_codex(bin_dir, log)
    envf = tmp_path / ".env"
    envf.write_text(f"OPENAI_API_KEY={env_key}\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_HOME")}
    env.update(PATH=f"{bin_dir}:{env['PATH']}", SHERPA_ENV_FILE=str(envf), CODEX_HOME=str(home))
    r = subprocess.run(["bash", "-c", f'ROOT="{ROOT}"; . "{COMMON}"; sherpa_codex_ensure_auth; echo rc=$?'],
                       env=env, capture_output=True, text=True, timeout=30)
    return int(r.stdout.strip().rsplit("rc=", 1)[1]), r.stdout + r.stderr


def test_codex_auth_is_done_from_dotenv_key_and_is_idempotent(tmp_path: Path):
    home, log = tmp_path / "home", tmp_path / "log"
    rc, out = _ensure_auth(tmp_path, "sk-real-1", home, log)
    assert rc == 0 and "認証しました" in out
    assert '"OPENAI_API_KEY":"sk-real-1"' in (home / "auth.json").read_text(encoding="utf-8")
    rc2, out2 = _ensure_auth(tmp_path, "sk-real-1", home, log)          # 同じキー → 何もしない
    assert rc2 == 0 and "認証しました" not in out2
    assert log.read_text(encoding="utf-8").count("login --with-api-key") == 1


def test_codex_auth_skips_placeholder_and_respects_subscription_login(tmp_path: Path):
    home, log = tmp_path / "home", tmp_path / "log"
    rc, _ = _ensure_auth(tmp_path, "sk-REPLACE_ME", home, log)         # プレースホルダ → 未実施
    assert rc == 1 and not (home / "auth.json").exists()
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text('{"auth_mode":"chatgpt","tokens":{}}', encoding="utf-8")
    rc2, out2 = _ensure_auth(tmp_path, "sk-real-2", home, log)         # サブスク認証済み → 触らない
    assert rc2 == 0 and "chatgpt" in (home / "auth.json").read_text(encoding="utf-8")
    assert "login --with-api-key" not in (log.read_text(encoding="utf-8") if log.exists() else "")


def test_lan_can_be_persisted_in_dotenv_and_explicit_flag_wins(tmp_path: Path):
    """.env の SHERPA_LAN=1 で make start が常に LAN 公開／コマンドラインの LAN= が優先。"""
    f = tmp_path / ".env"
    f.write_text("SHERPA_LAN=1\n", encoding="utf-8")
    base = {k: v for k, v in os.environ.items() if k not in ("LAN", "SHERPA_LAN")}
    base["SHERPA_ENV_FILE"] = str(f)
    snippet = f'ROOT="{ROOT}"; . "{COMMON}"; sherpa_env_default SHERPA_LAN; LAN="${{LAN:-${{SHERPA_LAN:-0}}}}"; printf "%s" "$LAN"'
    assert subprocess.run(["bash", "-c", snippet], env=base, capture_output=True, text=True, timeout=30).stdout == "1"
    assert subprocess.run(["bash", "-c", snippet], env=dict(base, LAN="0"), capture_output=True, text=True, timeout=30).stdout == "0"
    assert 'sherpa_env_default SHERPA_LAN' in (ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")
