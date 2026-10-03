"""`scripts/check-production.sh` の `OPENAI_BASE_URL` 検査（S3・ docs/archive/2026-08-18-AzureOpenAI対応.md）。"""
from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
CHECK_PRODUCTION = ROOT / "scripts" / "check-production.sh"


def _closed_local_port() -> int:
    """どのプロセスも listen していないローカルポート番号を1つ返す（一時的に bind して即 close ＝小さな race はあるが、テスト実行中に他プロセスがこの高番ポートを奪う可能性は無視できる）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_check_production(tmp_path: Path, *, openai_base_url: str | None,
                           fake_getent: str | None = None,
                           force_db_unreachable: bool = True) -> subprocess.CompletedProcess:
    """`check-production.sh` を実行する。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    if fake_getent is not None:
        fake = fake_bin / "getent"
        fake.write_text(fake_getent, encoding="utf-8")
        fake.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["SHERPA_ENV_FILE"] = str(tmp_path / "does-not-exist.env")
    env.pop("OPENAI_BASE_URL", None)
    if openai_base_url is not None:
        env["OPENAI_BASE_URL"] = openai_base_url
    if force_db_unreachable:
        env.pop("SHERPA_PG_DSN", None)
        env.pop("DATABASE_URL", None)
        env["PGHOST"] = "127.0.0.1"
        env["PGPORT"] = str(_closed_local_port())
    return subprocess.run([str(CHECK_PRODUCTION)], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=120)


def test_ok_when_openai_base_url_unset(tmp_path: Path):
    out = (lambda r: r.stdout + r.stderr)(_run_check_production(tmp_path, openai_base_url=None))
    assert "OPENAI_BASE_URL is not set" in out
    assert "OPENAI_BASE_URL" not in "\n".join(ln for ln in out.splitlines() if ln.startswith("NG:"))


@pytest.mark.parametrize("url,needles", [
    ("http://evil.example.com/openai/deployments/sk-should-not-leak",
     ["https", "evil.example.com", "接続先の検査モード: env 候補"]),   # http は拒否（DB 未到達は env 候補モード）
    ("not-a-url-sk-should-not-leak", []),   # 解析自体に失敗する値も NG
])
def test_ng_for_plain_http_or_malformed_url_without_leaking_value(tmp_path: Path, url, needles):
    r = _run_check_production(tmp_path, openai_base_url=url)
    out = r.stdout + r.stderr
    assert "NG: OPENAI_BASE_URL" in out and "sk-should-not-leak" not in out
    assert all(n in out for n in needles)


@pytest.mark.parametrize("getent,ok", [("#!/usr/bin/env bash\nexit 1\n", False), ("#!/usr/bin/env bash\necho ok\nexit 0\n", True)])
def test_hostname_resolution(tmp_path: Path, getent, ok):
    r = _run_check_production(tmp_path, openai_base_url="https://my-resource.openai.azure.com/openai/v1/",
                              fake_getent=getent)
    out = r.stdout + r.stderr
    if ok:
        assert "OK: OPENAI_BASE_URL scheme: https://my-resource.openai.azure.com" in out
        assert "OK: OPENAI_BASE_URL host resolves: my-resource.openai.azure.com" in out
        assert "NG: OPENAI_BASE_URL" not in out and "接続先の検査モード: env 候補（DB 未到達" in out
    else:
        assert "NG" in out and "名前解決できません" in out


def test_tcp_unreachable_is_warn_not_fail(tmp_path: Path):
    """TEST-NET-1（ブラックホール）宛の TCP 疎通は `timeout 3` で打ち切られ warn（fail ではない）。
    `timeout 3` の上限ガードが外れると SYN 再送で subprocess の timeout を超えて赤になる＝時間上限の契約を担保する唯一のテスト。"""
    r = _run_check_production(tmp_path, openai_base_url="https://192.0.2.1/v1",
                              fake_getent="#!/usr/bin/env bash\necho ok\nexit 0\n")
    out = r.stdout + r.stderr
    assert "NG: OPENAI_BASE_URL" not in out and "WARN: OPENAI_BASE_URL" in out


def _run_with_fake_probe(tmp_path: Path, probe_lines: list[str], getent_log: Path | None = None):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/usr/bin/env bash\n" + "".join(f"echo {ln}\n" for ln in probe_lines), encoding="utf-8")
    fake_python.chmod(0o755)
    if getent_log is not None:
        fake_getent = fake_bin / "getent"
        fake_getent.write_text(f"#!/usr/bin/env bash\necho \"$@\" >> {getent_log}\necho ok\nexit 0\n", encoding="utf-8")
        fake_getent.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ.get('PATH', '')}", PYTHON_BIN=str(fake_python),
               SHERPA_ENV_FILE=str(tmp_path / "does-not-exist.env"))
    env.pop("OPENAI_BASE_URL", None)
    r = subprocess.run([str(CHECK_PRODUCTION)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    return r.stdout + r.stderr


def test_db_endpoint_invalid_is_hard_fail_and_ipv6_host_goes_to_getent_unbracketed(tmp_path: Path):
    """probe が DB_ENDPOINT_INVALID を返したら env 候補モードへ倒さず fail（probe の出力は PYTHON_BIN の差し替えで固定）。"""
    out = _run_with_fake_probe(tmp_path, ["DB_ENDPOINT_INVALID"])
    assert "system_settings" in out and "NG:" in out and "openai_base_url" in out and "env 候補（" not in out
    # probe が返す IPv6 host（角括弧なしの生値）は getent へそのまま渡り、表示は角括弧付き
    (tmp_path / "v6").mkdir()
    log = tmp_path / "getent.log"
    out = _run_with_fake_probe(tmp_path / "v6", ["MARKER_FOUND", "custom", "https", "2001:db8::1", "8443"], log)
    logged = log.read_text(encoding="utf-8") if log.exists() else ""
    assert "2001:db8::1" in logged and "[2001:db8::1]" not in logged and "[2001:db8::1]" in out
