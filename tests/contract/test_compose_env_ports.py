"""Docker Compose への env 受け渡しと、ポートの整合/占有検査（2026-08-18）。"""
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
                if re.match(r'\s*(echo|printf)\b', code) or code.lstrip().startswith('"'):
                    continue   # 利用者向けの案内文は実行ではない
                offenders.append(f"{f.name}:{i}: {code.strip()}")
    assert not offenders, "sherpa_compose 経由にしてください:\n" + "\n".join(offenders)
    src = (SCRIPTS / "run-common.sh").read_text(encoding="utf-8")
    assert "sherpa_compose()" in src and 'docker compose --env-file "$file" "$@"' in src
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^COMPOSE\s*:=\s*docker compose \$\(COMPOSE_ENV_FLAG\)", mk, re.M)
    assert re.search(r"^COMPOSE_ALL\s*:=\s*\$\(COMPOSE\) --profile ocr", mk, re.M)
    assert '--env-file "$(SHERPA_ENV_FILE)"' in mk and re.search(r"^check-ports:", mk, re.M)
    assert not [ln for ln in mk.splitlines() if ln.startswith("\t") and "docker compose" in ln]
    start = (SCRIPTS / "start.sh").read_text(encoding="utf-8")
    assert start.index("./scripts/check-ports.sh") < start.index("sherpa_compose up -d")
    prod = (SCRIPTS / "check-production.sh").read_text(encoding="utf-8")
    assert "check-ports.sh" in prod
    assert '[ -z "${POSTGRES_PASSWORD:-}" ]' in prod and 'os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD"' in prod


def test_explicit_missing_env_file_is_rejected_by_run_common_and_makefile(tmp_path: Path):
    missing = tmp_path / "missing.env"
    r = subprocess.run(["bash", "-c", f'. "{SCRIPTS / "run-common.sh"}"; sherpa_compose config'],
                       env=dict(os.environ, SHERPA_ENV_FILE=str(missing)), capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and str(missing) in r.stderr and "ありません" in r.stderr
    r = subprocess.run(["make", "-n", f"SHERPA_ENV_FILE={missing}", "up"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and str(missing) in r.stderr and "ありません" in r.stderr


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


# (追記する設定, 追加 env, 期待 rc==0, stdout に含む, stderr に含む)
@pytest.mark.parametrize("extra,env,ok,out_needles,err_needles", [
    (lambda p: f"ES_URL=http://localhost:{p[1]}\nNEO4J_URI=bolt://127.0.0.1:{p[2]}\n", {}, True, ["一致"], []),
    # compose は p[0]・アプリは別ポートの localhost ＝ 他人の PostgreSQL へ繋ぐ構成
    (lambda p: f"DATABASE_URL=postgresql://sherpa:x@localhost:{p[0] + 1}/sherpa\n", {}, False, ["不一致"], ["PGPORT", "DATABASE_URL"]),
    # アプリは SHERPA_PG_DSN ＞ DATABASE_URL の順に見る＝検査も使われる方だけを見る
    (lambda p: f"SHERPA_PG_DSN=host=localhost port={p[0]} dbname=x user=u password=p\n"
               f"DATABASE_URL=postgresql://sherpa:x@localhost:{p[0] + 1}/sherpa\n", {}, True, [], []),
    (lambda p: "ES_URL=http://search.example.invalid:9200\n", {}, False, ["名前解決できず"], ["ES_URL", ".env.example"]),
    # 127.0.0.2 は「別ホスト」扱いで名前解決は通り、実際に接続を試せる（外部ネットワーク不要）
    (lambda p: f"ES_URL=http://127.0.0.2:{p[1]}\n", {}, False, ["接続できず"], ["SHERPA_SKIP_PORT_CHECK"]),
    (lambda p: f"ES_URL=http://127.0.0.2:{p[1]}\n", {"SHERPA_SKIP_PORT_CHECK": "1"}, True, ["疎通検査は省略"], []),
    (lambda p: f"ES_URL=http://127.0.0.2:{p[1]}\nSHERPA_SKIP_PORT_CHECK=1\n", {}, True, ["疎通検査は省略"], []),   # 設定ファイルに書いても効く
    (lambda p: "ES_URL=http://localhost:not-a-port\n", {}, False, [], ["ES_URL", "正しい URL"]),
])
def test_check_ports_consistency_and_remote_checks(tmp_path: Path, extra, env, ok, out_needles, err_needles):
    p = _free_ports(5)
    r = _run_check(tmp_path, _ports_block(p) + extra(p), env)
    assert (r.returncode == 0) == ok, r.stdout + r.stderr
    assert all(n in r.stdout for n in out_needles) and all(n in r.stderr for n in err_needles)
    assert "Traceback" not in r.stderr   # 不正な URL でも traceback だけを残して落ちない


def test_check_ports_invalid_port_and_occupied_ports(tmp_path: Path):
    p = _free_ports(5)
    r = _run_check(tmp_path, _ports_block(p).replace(f"PGPORT={p[0]}", "PGPORT=70000"))
    assert r.returncode != 0 and "PGPORT" in r.stderr and "1〜65535" in r.stderr
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        taken = s.getsockname()[1]
        r = _run_check(tmp_path, _ports_block(p[:4] + [taken]))   # アプリのポートを他プロセスが聴いている
        assert r.returncode != 0 and "SHERPA_PORT" in r.stderr and str(taken) in r.stderr
        r = _run_check(tmp_path, _ports_block([taken] + p[:4]))   # ストアのポートを（docker 以外の）他プロセスが聴いている
        assert r.returncode != 0 and "PGPORT" in r.stderr
        r = _run_check(tmp_path, _ports_block([taken] + p[:4]), {"SHERPA_SKIP_PORT_CHECK": "1"})   # 逃げ道
        assert r.returncode == 0, r.stdout + r.stderr
    finally:
        s.close()


def test_check_ports_remote_reachable_is_ok(tmp_path: Path):
    p = _free_ports(6)   # p[5]＝別ホスト側の listen ポート
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


def test_default_targets_follow_port_variables(monkeypatch):
    """アプリ側の既定は「ポートは 1 変数」: PGPASSWORD＞POSTGRES_PASSWORD、ES/Neo4j は各ポート変数に追随・明示 URL が優先。"""
    from sherpa import es_index
    from sherpa.ingest import world_neo4j
    from sherpa.providers.codex import mcp
    from sherpa.store import db
    for k in ("SHERPA_PG_DSN", "DATABASE_URL", "PGPASSWORD", "PGPORT", "ES_URL", "NEO4J_URI", "SHERPA_ES_PORT",
              "SHERPA_NEO4J_BOLT_PORT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", "from-compose")
    assert "password=from-compose" in db._dsn()
    monkeypatch.setenv("PGPASSWORD", "explicit")
    assert "password=explicit" in db._dsn()
    monkeypatch.setenv("PGPORT", "15432")
    assert "port=15432" in db._dsn()
    assert es_index._url() == "http://localhost:9200" and world_neo4j.default_neo4j_uri() == "bolt://localhost:7687"
    monkeypatch.setenv("SHERPA_ES_PORT", "19200")
    monkeypatch.setenv("SHERPA_NEO4J_BOLT_PORT", "17687")
    assert es_index._url() == "http://localhost:19200" and world_neo4j._env()["uri"] == "bolt://localhost:17687"
    env = mcp._mcp_env("test", None)   # Codex MCP サブプロセスも親と同じ ES/Neo4j へ繋ぐ
    assert env["ES_URL"] == "http://localhost:19200" and env["NEO4J_URI"] == "bolt://localhost:17687"
    monkeypatch.setenv("ES_URL", "http://search.example.local:9200/")
    monkeypatch.setenv("NEO4J_URI", "bolt://graph.example.local:7687")
    assert es_index._url() == "http://search.example.local:9200"
    assert world_neo4j._env()["uri"] == "bolt://graph.example.local:7687"


@pytest.mark.parametrize("tool", ["ss", "lsof"])
def test_check_ports_recognizes_own_app_listener(tmp_path: Path, tool: str):
    """多ワーカー（ss の users に子 pid が先に並ぶ）でも、macOS（ss 無し・lsof）でも、自アプリを他プロセスと誤判定しない。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if tool == "ss":
        (bin_dir / "ss").write_text(
            "#!/usr/bin/env bash\necho 'LISTEN 0 2048 127.0.0.1:18765 0.0.0.0:* users:((\"python3\",pid=4244,fd=3),"
            "(\"python3\",pid=4243,fd=3),(\"python3\",pid=4242,fd=3))'\n", encoding="utf-8")
        edit = f'sed -e "s/pid=4242/pid=$$/" -i "{bin_dir}/ss"'
    else:
        (bin_dir / "ss").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        (bin_dir / "lsof").write_text("#!/usr/bin/env bash\nprintf 'p4242\\ncbash\\nf3\\n'\n", encoding="utf-8")
        edit = f'sed -e "s/p4242/p$$/" -i "{bin_dir}/lsof"'
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", SHERPA_PORT="18765",
               SHERPA_ENV_FILE=str(tmp_path / "none.env"), SHERPA_SKIP_PORT_CHECK="")
    script = (f'echo $$ > "{run_dir}/api.pid"; RUN_DIR="{run_dir}"; APP_PID_FILE="{run_dir}/api.pid"; APP_PROC_NEEDLE=bash; '
              f'export RUN_DIR APP_PID_FILE APP_PROC_NEEDLE; {edit}; '
              f'"{ROOT}/scripts/check-ports.sh" 2>&1 | grep -E "アプリ 占有" ')
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert "自アプリ" in r.stdout, r.stdout + r.stderr
