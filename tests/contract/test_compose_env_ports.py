"""Docker Compose への env 受け渡しと、ポートの整合/占有検査（2026-08-18）。

不具合: `docker compose` の呼び出しが --env-file を渡していなかったため、本番の
`SHERPA_ENV_FILE=/etc/sherpa/sherpa.env` に書いた PGPORT 等は無視され既定ポートで上がる。さらにアプリの
接続先（DATABASE_URL 等）とは別変数のため、片方だけ変えると「compose は 5433・アプリは 5432＝他人の DB へ」
となる。整合・占有の事前検査も無かった。

契約:
- compose の呼び出しは全て scripts/run-common.sh の `sherpa_compose`（Makefile は `$(COMPOSE)`）経由。
- scripts/check-ports.sh が整合（compose 公開 ⇔ アプリ接続先）と占有（他プロセス）を検査し、NG なら非0。
- アプリ側の既定は「ポートは 1 変数」: PGPASSWORD＞POSTGRES_PASSWORD、ES は SHERPA_ES_PORT、Neo4j は SHERPA_NEO4J_BOLT_PORT に追随。
外部サービス（docker/PG/ES/Neo4j）は不要。
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
CHECK = SCRIPTS / "check-ports.sh"

# 生の `docker compose` を許す例外: サブコマンド `version`（存在確認・env 不要）と、run-common.sh 本体。
_ALLOWED_RAW = re.compile(r"docker compose version\b")


def _code_lines(path: Path):
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#", 1)[0]
        if code.strip():
            yield i, code


def test_all_compose_calls_go_through_sherpa_compose():
    """scripts/*.sh の実行行に素の `docker compose` を残さない（--env-file 渡し忘れの根）。"""
    offenders = []
    for f in sorted(SCRIPTS.glob("*.sh")):
        if f.name == "run-common.sh":
            continue
        for i, code in _code_lines(f):
            if "docker compose" in code and not _ALLOWED_RAW.search(code):
                # echo/cat による案内文（利用者向けの表示）は実行ではない
                if re.match(r'\s*(echo|printf)\b', code) or code.lstrip().startswith('"'):
                    continue
                offenders.append(f"{f.name}:{i}: {code.strip()}")
    assert not offenders, "sherpa_compose 経由にしてください:\n" + "\n".join(offenders)


def test_run_common_defines_sherpa_compose_with_env_file():
    src = (SCRIPTS / "run-common.sh").read_text(encoding="utf-8")
    assert "sherpa_compose()" in src
    assert 'docker compose --env-file "$file" "$@"' in src


def test_run_common_rejects_missing_explicit_env_file(tmp_path: Path):
    missing = tmp_path / "missing.env"
    cmd = f'. "{SCRIPTS / "run-common.sh"}"; sherpa_compose config'
    env = dict(os.environ)
    env["SHERPA_ENV_FILE"] = str(missing)
    r = subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
    assert str(missing) in r.stderr and "ありません" in r.stderr


def test_makefile_compose_variable_carries_env_file():
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^COMPOSE\s*:=\s*docker compose \$\(COMPOSE_ENV_FLAG\)", mk, re.M)
    assert re.search(r"^COMPOSE_ALL\s*:=\s*\$\(COMPOSE\) --profile ocr", mk, re.M)
    assert '--env-file "$(SHERPA_ENV_FILE)"' in mk
    # レシピ行（タブ始まり）に素の docker compose を残さない
    raw = [ln for ln in mk.splitlines() if ln.startswith("\t") and "docker compose" in ln]
    assert not raw, raw
    assert re.search(r"^check-ports:", mk, re.M)


def test_makefile_rejects_missing_explicit_env_file(tmp_path: Path):
    missing = tmp_path / "missing.env"
    r = subprocess.run(
        ["make", "-n", f"SHERPA_ENV_FILE={missing}", "up"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode != 0
    assert str(missing) in r.stderr and "ありません" in r.stderr


def test_start_calls_check_ports_before_compose_up():
    src = (SCRIPTS / "start.sh").read_text(encoding="utf-8")
    assert src.index("./scripts/check-ports.sh") < src.index("sherpa_compose up -d")


def test_check_production_calls_check_ports():
    assert "check-ports.sh" in (SCRIPTS / "check-production.sh").read_text(encoding="utf-8")


# ---- check-ports.sh の実行（外部サービス不要・空きポートだけを使う） -----------------------------

def _free_ports(n: int) -> list[int]:
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        socks.append(s)
        ports.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return ports


def _run_check(tmp_path: Path, body: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    f = tmp_path / "test.env"
    f.write_text(body, encoding="utf-8")
    drop = {"PGPORT", "SHERPA_ES_PORT", "SHERPA_NEO4J_BOLT_PORT", "SHERPA_NEO4J_HTTP_PORT", "SHERPA_PORT",
            "SHERPA_PG_DSN", "DATABASE_URL", "ES_URL", "NEO4J_URI", "SHERPA_SKIP_PORT_CHECK"}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["SHERPA_ENV_FILE"] = str(f)
    env.update(extra_env or {})
    return subprocess.run([str(CHECK)], env=env, capture_output=True, text=True, timeout=120)


def _ports_block(p: list[int]) -> str:
    return (f"PGPORT={p[0]}\nSHERPA_ES_PORT={p[1]}\nSHERPA_NEO4J_BOLT_PORT={p[2]}\n"
            f"SHERPA_NEO4J_HTTP_PORT={p[3]}\nSHERPA_PORT={p[4]}\n")


def test_check_ports_ok_when_consistent_and_free(tmp_path: Path):
    p = _free_ports(5)
    r = _run_check(tmp_path, _ports_block(p) + f"ES_URL=http://localhost:{p[1]}\nNEO4J_URI=bolt://127.0.0.1:{p[2]}\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "一致" in r.stdout


def test_check_ports_fails_on_pg_mismatch_and_names_both_vars(tmp_path: Path):
    p = _free_ports(5)
    # compose は p[0] で公開・アプリは別ポートの localhost へ ＝ 他人の PostgreSQL に繋ぎに行く構成
    r = _run_check(tmp_path, _ports_block(p) + f"DATABASE_URL=postgresql://sherpa:x@localhost:{p[0] + 1}/sherpa\n")
    assert r.returncode != 0
    assert "PGPORT" in r.stderr and "DATABASE_URL" in r.stderr
    assert "不一致" in r.stdout


def test_check_ports_pg_dsn_takes_precedence_over_database_url(tmp_path: Path):
    """アプリは SHERPA_PG_DSN ＞ DATABASE_URL の順に見る（db.py::_dsn）。検査も使われる方だけを見る。"""
    p = _free_ports(5)
    body = _ports_block(p) + (f"SHERPA_PG_DSN=host=localhost port={p[0]} dbname=x user=u password=p\n"
                              f"DATABASE_URL=postgresql://sherpa:x@localhost:{p[0] + 1}/sherpa\n")
    r = _run_check(tmp_path, body)
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_ports_remote_unresolvable_host_is_ng(tmp_path: Path):
    """別ホスト名が名前解決できない（.env.example の例をそのまま有効化した等）は NG（閉域実機 2026-08-18）。"""
    p = _free_ports(5)
    r = _run_check(tmp_path, _ports_block(p) + "ES_URL=http://search.example.invalid:9200\n")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "名前解決できず" in r.stdout
    assert "ES_URL" in r.stderr and ".env.example" in r.stderr


def test_check_ports_remote_resolvable_but_unreachable_is_ng_unless_skipped(tmp_path: Path):
    """解決できても TCP で繋がらなければ NG（別ホストのストア未起動/FW）。SHERPA_SKIP_PORT_CHECK=1 で疎通だけ省略。"""
    p = _free_ports(5)
    # 127.0.0.2 は is_local_host に含まれない「別ホスト」扱いだが、数値 IP なので名前解決は通り、
    # loopback なので実際に接続を試せる（外部ネットワーク不要）。
    body = _ports_block(p) + f"ES_URL=http://127.0.0.2:{p[1]}\n"
    r = _run_check(tmp_path, body)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "接続できず" in r.stdout and "SHERPA_SKIP_PORT_CHECK" in r.stderr
    r2 = _run_check(tmp_path, body, {"SHERPA_SKIP_PORT_CHECK": "1"})
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "疎通検査は省略" in r2.stdout


def test_check_ports_skip_flag_set_only_in_dotenv_file_is_honored(tmp_path: Path):
    """RV MED（2026-08-18 Codex RV 指摘3）: fixes 文言は「.env に SHERPA_SKIP_PORT_CHECK=1 と書けば
    省略できる」と案内するが、以前は `sherpa_env_default`（run-common.sh）の列挙に無く、.env / 本番の
    SHERPA_ENV_FILE に書いても読まれなかった（コマンドラインで明示したときだけ効いた＝案内どおりに
    しても別ホストのストアをまだ起動していない構成で make start が fail-close していた）。

    `_run_check` は継承環境から SHERPA_SKIP_PORT_CHECK を drop 済み（`drop` 集合参照）＝
    この呼び出しは**プロセス環境変数を一切渡さず**、.env ファイル本文だけに書いて効くことを確認する
    （直前のテストの extra_env 経由＝明示指定の確認とは対）。"""
    p = _free_ports(5)
    body = _ports_block(p) + f"ES_URL=http://127.0.0.2:{p[1]}\nSHERPA_SKIP_PORT_CHECK=1\n"
    r = _run_check(tmp_path, body)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "疎通検査は省略" in r.stdout


def test_check_ports_remote_reachable_is_ok(tmp_path: Path):
    p = _free_ports(6)      # p[5]＝別ホスト側の listen ポート（compose 公開ポートと別＝占有検査に掛からない）
    srv = socket.socket()
    try:
        srv.bind(("127.0.0.2", p[5]))
        srv.listen(1)
    except OSError:
        pytest.skip("127.0.0.2 に bind できない環境")
    try:
        r = _run_check(tmp_path, _ports_block(p) + f"ES_URL=http://127.0.0.2:{p[5]}\n")
    finally:
        srv.close()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "疎通OK" in r.stdout


def test_check_ports_rejects_malformed_url_without_traceback(tmp_path: Path):
    """不正な URL を「別ホスト」として通したり、Python traceback だけを残して落ちたりしない。"""
    p = _free_ports(5)
    r = _run_check(tmp_path, _ports_block(p) + "ES_URL=http://localhost:not-a-port\n")
    assert r.returncode != 0
    assert "ES_URL" in r.stderr and "正しい URL" in r.stderr
    assert "Traceback" not in r.stderr


def test_check_ports_rejects_invalid_compose_port(tmp_path: Path):
    p = _free_ports(5)
    body = _ports_block(p).replace(f"PGPORT={p[0]}", "PGPORT=70000")
    r = _run_check(tmp_path, body)
    assert r.returncode != 0
    assert "PGPORT" in r.stderr and "1〜65535" in r.stderr


def test_check_ports_fails_when_app_port_taken_by_other_process(tmp_path: Path):
    p = _free_ports(4)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        taken = s.getsockname()[1]
        r = _run_check(tmp_path, _ports_block(p + [taken]))
        assert r.returncode != 0
        assert "SHERPA_PORT" in r.stderr and str(taken) in r.stderr
    finally:
        s.close()


def test_check_ports_fails_when_store_port_taken_and_not_ours(tmp_path: Path):
    """ストアのポートを（docker 以外の）他プロセスが聴いている → 所有者が sherpa-mvp- ではないので NG。
    設計判断: docker 照会ができない環境（CI 等）でも「不明＝他者扱いしない」ではなく **fail-closed** にする。
    この検査の目的は「他人のサービスへ静かに繋ぐ」事故の防止であり、疑わしきは止めて所有者確認を促す方が安全
    （通す逃げ道は SHERPA_SKIP_PORT_CHECK=1）。docker が使えても使えなくても本テストは NG になる。"""
    p = _free_ports(4)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        taken = s.getsockname()[1]
        r = _run_check(tmp_path, _ports_block([taken] + p))
        assert r.returncode != 0
        assert "PGPORT" in r.stderr
        # 逃げ道: 占有検査だけ省略
        r2 = _run_check(tmp_path, _ports_block([taken] + p), {"SHERPA_SKIP_PORT_CHECK": "1"})
        assert r2.returncode == 0, r2.stdout + r2.stderr
    finally:
        s.close()


# ---- アプリ側の既定「ポートは 1 変数」（unit・monkeypatch） -----------------------------------------

def test_dsn_falls_back_to_postgres_password(monkeypatch):
    from sherpa.store import db
    for k in ("SHERPA_PG_DSN", "DATABASE_URL", "PGPASSWORD", "PGPORT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", "from-compose")
    assert "password=from-compose" in db._dsn()
    monkeypatch.setenv("PGPASSWORD", "explicit")
    assert "password=explicit" in db._dsn()          # PGPASSWORD が優先
    monkeypatch.setenv("PGPORT", "15432")
    assert "port=15432" in db._dsn()


def test_production_check_accepts_compose_postgres_password():
    script = (ROOT / "scripts" / "check-production.sh").read_text(encoding="utf-8")
    assert '[ -z "${POSTGRES_PASSWORD:-}" ]' in script
    assert 'os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD"' in script


def test_es_default_url_follows_port_variable(monkeypatch):
    from sherpa import es_index
    monkeypatch.delenv("ES_URL", raising=False)
    monkeypatch.delenv("SHERPA_ES_PORT", raising=False)
    assert es_index._url() == "http://localhost:9200"
    monkeypatch.setenv("SHERPA_ES_PORT", "19200")
    assert es_index._url() == "http://localhost:19200"
    monkeypatch.setenv("ES_URL", "http://search.example.local:9200/")
    assert es_index._url() == "http://search.example.local:9200"      # 明示は優先


def test_neo4j_default_uri_follows_port_variable(monkeypatch):
    from sherpa.ingest import world_neo4j
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("SHERPA_NEO4J_BOLT_PORT", raising=False)
    assert world_neo4j.default_neo4j_uri() == "bolt://localhost:7687"
    assert world_neo4j._env()["uri"] == "bolt://localhost:7687"
    monkeypatch.setenv("SHERPA_NEO4J_BOLT_PORT", "17687")
    assert world_neo4j._env()["uri"] == "bolt://localhost:17687"
    monkeypatch.setenv("NEO4J_URI", "bolt://graph.example.local:7687")
    assert world_neo4j._env()["uri"] == "bolt://graph.example.local:7687"


# ---- 2026-08-18 再レビューで見つかった回帰の固定 ----------------------------------------------


def test_mcp_subprocess_gets_the_same_es_and_neo4j_targets_as_parent(monkeypatch):
    """ポート変数だけを設定した構成でも、Codex MCP サブプロセスは親と同じ ES/Neo4j へ繋ぐ
    （以前は ES_URL/NEO4J_URI が環境に無いと透過されず、MCP 側だけ既定ポートへ行った）。"""
    monkeypatch.setenv("SHERPA_ES_PORT", "19200")
    monkeypatch.setenv("SHERPA_NEO4J_BOLT_PORT", "17687")
    monkeypatch.delenv("ES_URL", raising=False)
    monkeypatch.delenv("NEO4J_URI", raising=False)
    from sherpa import es_index
    from sherpa.ingest import world_neo4j
    from sherpa.providers.codex import mcp

    env = mcp._mcp_env("test", None)
    assert env["ES_URL"] == es_index._url() == "http://localhost:19200"
    assert env["NEO4J_URI"] == world_neo4j.default_neo4j_uri() == "bolt://localhost:17687"


def test_check_ports_matches_any_listener_pid_for_multi_worker_app(tmp_path: Path):
    """uvicorn 多ワーカーでは ss の users に子 pid が先に並ぶ。いずれかが自アプリ pid なら自アプリと判定する。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # 偽 ss: 親 pid=4242 が末尾に並ぶ 3 プロセス共有リスナ
    (bin_dir / "ss").write_text(
        "#!/usr/bin/env bash\n"
        "echo 'LISTEN 0 2048 127.0.0.1:18765 0.0.0.0:* users:((\"python3\",pid=4244,fd=3),(\"python3\",pid=4243,fd=3),(\"python3\",pid=4242,fd=3))'\n",
        encoding="utf-8",
    )
    (bin_dir / "ss").chmod(0o755)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "api.pid").write_text("4242\n", encoding="utf-8")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", SHERPA_PORT="18765",
               SHERPA_ENV_FILE=str(tmp_path / "none.env"), SHERPA_SKIP_PORT_CHECK="")
    # live_matching_pid は pid の生存とコマンドラインを見るので、自分自身（このテストのシェル）を親に見立てる:
    # bash -c の中で $$ を pid ファイルへ書き、needle=bash に合わせて検査だけ通す。
    script = (
        f'echo $$ > "{run_dir}/api.pid"; '
        f'RUN_DIR="{run_dir}"; APP_PID_FILE="{run_dir}/api.pid"; APP_PROC_NEEDLE=bash; '
        f'export RUN_DIR APP_PID_FILE APP_PROC_NEEDLE; '
        f'sed -e "s/pid=4242/pid=$$/" -i "{bin_dir}/ss"; '
        f'"{ROOT}/scripts/check-ports.sh" 2>&1 | grep -E "アプリ 占有" '
    )
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert "自アプリ" in r.stdout, r.stdout + r.stderr
