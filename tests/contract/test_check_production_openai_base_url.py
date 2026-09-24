"""`scripts/check-production.sh` の `OPENAI_BASE_URL` 検査（S3・
docs/proposals/2026-08-18-AzureOpenAI対応.md）。

背景: 実行環境が Azure と分かり、`OPENAI_BASE_URL`（env・`sherpa/llm.py::openai_base_url`）で
OpenAI 互換の接続先（Azure OpenAI・Private Link 経由のゲートウェイ等）へ切り替えられるようにした。
起動前チェック（`scripts/check-production.sh`）で「https であること」「ホスト名が名前解決できること」を
読み取り専用で検査する（`scripts/check-ports.sh::resolve_host` と同じ発想）。TCP 疎通も試みるが、
Private Link 等でこの preflight の実行元から到達できない構成でも正常なことがあるため warn 止まり
（fail にしない）。

このテストは `tests/contract/test_offline_kit_ops_guidance.py::_run_check_production`（vm.max_map_count
検査）と同じやり方（`SHERPA_ENV_FILE` を存在しないパスにし、`fail()` が非0終了しない実装を利用して
後続の検査まで進ませる・外部コマンドはフェイクの実行ファイルで差し替える）で、外部サービス・実DNS・
実ネットワークに依存せず検査する。

http 拒否が loopback でも成り立つこと（`http://127.0.0.1` 等）は
`tests/unit/test_llm_openai_endpoint.py::test_http_localhost_rejected` が同じ判定（scheme が
https 以外なら host によらず拒否）を見ているため、ここでは重複させない。
"""
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
    """どのプロセスも listen していないローカルポート番号を1つ返す（一時的に bind して即 close
    ＝小さな race はあるが、テスト実行中に他プロセスがこの高番ポートを奪う可能性は無視できる）。

    到達不能アドレス（TEST-NET-1 のようなブラックホール）宛の TCP connect は OS の
    connect_timeout をフルに待つ（`check_production_openai_probe.py::_CONNECT_TIMEOUT`＝3秒・
    `scripts/check-production.sh::_check_openai_endpoint_host` の `timeout 3`）が、ループバックの
    「誰も listen していない」ポートへの接続は ECONNREFUSED でほぼ即座に失敗する。「到達できない」
    という判定結果自体は同じため（検査対象の contract は変えない）、待ち時間だけを高速化できる。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_check_production(tmp_path: Path, *, openai_base_url: str | None,
                           fake_getent: str | None = None,
                           force_db_unreachable: bool = True) -> subprocess.CompletedProcess:
    """`check-production.sh` を実行する。`fake_getent` を渡すと `getent` をそのスクリプト内容で
    差し替える（実 DNS に依存させないため）。env ファイルは意図的に存在しないパスにする
    （この検査より前の項目は失敗するが、`fail()` は非0終了しない実装のため、後続の検査まで進む）。

    `force_db_unreachable`（既定 True）: `PGHOST`/`SHERPA_PG_DSN` を「誰も listen していない
    ローカルポート」（`_closed_local_port`・ECONNREFUSED で即座に失敗＝到達不能という判定結果は
    TEST-NET-1 と同じだが待たない）へ強制し、system_settings 実効値モードの判定を確実に
    「DB 未到達」へ倒す（env 候補モードを検査する本ファイルの大半のテストを、pytest プロセスの
    ambient な DB 接続先（他のテストが `openai_endpoint_seed_version` マーカーを既に立てている
    可能性があり、テスト実行順に応じて結果が変わりうる＝共有 DB へは触れない方針）から
    切り離すため。DB モード自体を検査するテストだけ明示的に `False` を渡す）。
    """
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
    r = _run_check_production(tmp_path, openai_base_url=None)
    out = r.stdout + r.stderr
    assert "OPENAI_BASE_URL is not set" in out
    assert "OPENAI_BASE_URL" not in "\n".join(
        ln for ln in out.splitlines() if ln.startswith("NG:"))


def test_ng_when_openai_base_url_is_plain_http(tmp_path: Path):
    """http は拒否される（DB 未到達時は env 候補モードへ fail-safe し、そのモードでも同じ判定に
    なることを含む）。path に秘密らしき文字列が混入していても（Azure のデプロイ名欄等に誤って
    貼り付けた想定）、拒否メッセージには host 以外を出さない（scheme も含めない・env 候補モードは
    本番の `_openai_endpoint_seed_candidate`／`assert_openai_base_url_allowed` を共有するため、
    この検証は bash 側でなく python 側の安全な host 表現を経由する）。"""
    r = _run_check_production(
        tmp_path, openai_base_url="http://evil.example.com/openai/deployments/sk-should-not-leak")
    out = r.stdout + r.stderr
    assert "NG" in out
    assert "https" in out
    assert "OPENAI_BASE_URL" in out
    assert "NG: OPENAI_BASE_URL" in out
    assert "sk-should-not-leak" not in out
    assert "evil.example.com" in out
    assert "接続先の検査モード: env 候補" in out


def test_ng_when_openai_base_url_is_malformed(tmp_path: Path):
    """解析自体に失敗する値も NG にする。生の env 値をメッセージへ出さない（path に秘密らしき
    文字列が混入していても解析失敗時の安全な表現になることを含む）。"""
    r = _run_check_production(tmp_path, openai_base_url="not-a-url-sk-should-not-leak")
    out = r.stdout + r.stderr
    assert "NG" in out
    assert "OPENAI_BASE_URL" in out
    assert "NG: OPENAI_BASE_URL" in out
    assert "sk-should-not-leak" not in out


def test_ng_when_hostname_unresolvable(tmp_path: Path):
    """名前解決できないホスト（`.env` の例をそのまま有効化した等）は fail にする。実 DNS に
    依存させないよう `getent` を常に失敗するフェイクに差し替える。"""
    r = _run_check_production(
        tmp_path,
        openai_base_url="https://my-resource.openai.azure.com/openai/v1/",
        fake_getent="#!/usr/bin/env bash\nexit 1\n",
    )
    out = r.stdout + r.stderr
    assert "NG" in out
    assert "名前解決できません" in out


def test_ok_when_hostname_resolves_azure(tmp_path: Path):
    """名前解決できれば https チェックまでは OK になる（TCP 疎通の成否は warn 止まりなので
    ここでは問わない）。DB に到達できない（preflight はサービス起動前に実行されることが多い）
    ときは env 候補モードへ fail-safe し、従来どおり env の OPENAI_BASE_URL を検査することも
    同じ出力でまとめて確認する。実 DNS に依存させないよう `getent` を常に成功するフェイクに
    差し替える。"""
    r = _run_check_production(
        tmp_path,
        openai_base_url="https://my-resource.openai.azure.com/openai/v1/",
        fake_getent="#!/usr/bin/env bash\necho ok\nexit 0\n",
    )
    out = r.stdout + r.stderr
    assert "OK: OPENAI_BASE_URL scheme: https://my-resource.openai.azure.com" in out
    assert "OK: OPENAI_BASE_URL host resolves: my-resource.openai.azure.com" in out
    assert "NG: OPENAI_BASE_URL" not in out
    assert "接続先の検査モード: env 候補（DB 未到達" in out


def test_tcp_unreachable_is_warn_not_fail(tmp_path: Path):
    """TEST-NET-1（ブラックホール）宛の TCP 疎通は `timeout 3` で打ち切られ warn（fail ではない）。
    このテストだけは実接続の上限ガードを担保するためブラックホール宛を維持する（所要約 3 秒）。"""
    r = _run_check_production(
        tmp_path,
        # この 1 本だけはブラックホール宛（TEST-NET-1）を維持する: `timeout 3` の上限ガードが外れると
        # SYN 再送で subprocess の timeout を超えて赤になる＝時間上限の契約を担保する唯一のテスト（所要約 3 秒）。
        openai_base_url="https://192.0.2.1/v1",
        fake_getent="#!/usr/bin/env bash\necho ok\nexit 0\n",
    )
    out = r.stdout + r.stderr
    assert "NG: OPENAI_BASE_URL" not in out
    assert "WARN: OPENAI_BASE_URL" in out


def test_db_endpoint_invalid_status_line_is_hard_fail_not_env_fallback(tmp_path: Path):
    """`check_production_openai_probe.py` が `DB_ENDPOINT_INVALID`（マーカー確定済みだが
    DB 実効の base_url が不正）を返した場合、`check-production.sh` は env 候補モードへ
    フォールバックせず fail にする（実 DB を使わず、`PYTHON_BIN` を差し替えて probe の出力を
    決定的に固定する・本ファイルの「共有 DB へは触れない」方針を保つ・probe() 内部のロジック自体は
    tests/unit/test_check_production_openai_probe.py が別途固定する）。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    # `check-production.sh` は `${PYTHON_BIN:-python3} .../check_production_openai_probe.py` を
    # 呼ぶ（引数は無視して固定行だけ返す）。
    fake_python.write_text("#!/usr/bin/env bash\necho DB_ENDPOINT_INVALID\n", encoding="utf-8")
    fake_python.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["PYTHON_BIN"] = str(fake_python)
    env["SHERPA_ENV_FILE"] = str(tmp_path / "does-not-exist.env")
    env.pop("OPENAI_BASE_URL", None)
    r = subprocess.run([str(CHECK_PRODUCTION)], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert "system_settings" in out   # env 候補モードへは倒れていない
    assert "NG:" in out and "openai_base_url" in out
    assert "env 候補（" not in out   # env 候補モードのメッセージは出ていない


def test_ipv6_host_passed_to_getent_without_brackets(tmp_path: Path):
    """probe が返す IPv6 host（角括弧なしの生値）は `getent`/`/dev/tcp` へそのまま渡る。
    以前は `_scheme_host_port` が表示用に角括弧を付けた値をそのまま渡していたため、正当な IPv6
    接続先が名前解決に失敗して hard fail していた（`PYTHON_BIN` を差し替えて probe の出力を
    決定的に固定し、`getent` の実引数をファイルへ記録して検査する・実 DB は使わない）。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "echo MARKER_FOUND\n"
        "echo custom\n"
        "echo https\n"
        "echo 2001:db8::1\n"
        "echo 8443\n",
        encoding="utf-8")
    fake_python.chmod(0o755)
    getent_log = tmp_path / "getent.log"
    fake_getent = fake_bin / "getent"
    fake_getent.write_text(
        f"#!/usr/bin/env bash\necho \"$@\" >> {getent_log}\necho ok\nexit 0\n", encoding="utf-8")
    fake_getent.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["PYTHON_BIN"] = str(fake_python)
    env["SHERPA_ENV_FILE"] = str(tmp_path / "does-not-exist.env")
    env.pop("OPENAI_BASE_URL", None)
    r = subprocess.run([str(CHECK_PRODUCTION)], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    logged = getent_log.read_text(encoding="utf-8") if getent_log.exists() else ""
    assert "2001:db8::1" in logged, f"getent が呼ばれなかった/ホストが記録されなかった: {logged!r}"
    assert "[2001:db8::1]" not in logged, f"getent へ角括弧付きホストが渡された: {logged!r}"
    # 表示メッセージ側は角括弧付き（人間可読性）。
    assert "[2001:db8::1]" in out
