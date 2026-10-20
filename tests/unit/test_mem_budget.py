"""メモリの割り振り（scripts/lib/mem_budget.sh）: 全体のメモリから ES・Neo4j・OCR の上限が規則どおりに決まり、compose に渡る。"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "lib" / "mem_budget.sh"
GB = 1024
TOTALS = [4 * GB, 8 * GB, 15 * GB, 32 * GB, 64 * GB]

# 全体 → (OS, ES ヒープ, ES 上限, Neo4j ヒープ, Neo4j キャッシュ, LibreOffice, OCR)
EXPECTED = {
    4 * GB: (1536, 512, 1536, 512, 256, 0, 256),
    8 * GB: (1536, 512, 1536, 655, 327, 3072, 1066),
    15 * GB: (1536, 768, 2048, 1228, 614, 3072, 4096),
    32 * GB: (3276, 1638, 3788, 2621, 1310, 4096, 8192),
    64 * GB: (6553, 3276, 7064, 4096, 2048, 4096, 8192),
}


def _env(tmp_path: pathlib.Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SHERPA_", "MB_"))}
    env["SHERPA_ENV_FILE"] = str(tmp_path / "empty.env")
    (tmp_path / "empty.env").write_text("", encoding="utf-8")
    env.update(extra)
    return env


def _budget(tmp_path: pathlib.Path, total_mb: int, **extra: str) -> dict:
    r = subprocess.run(["bash", str(LIB), "--total-mb", str(total_mb), "--json"],
                       env=_env(tmp_path, **extra), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize("total", TOTALS)
def test_limits_follow_the_rule_and_never_exceed_total(tmp_path, total):
    b = _budget(tmp_path, total)
    got = (b["os_mb"], b["es_heap_mb"], b["es_limit_mb"], b["neo4j_heap_mb"], b["neo4j_pagecache_mb"], b["libreoffice_mb"], b["ocr_limit_mb"])
    assert got == EXPECTED[total]
    assert b["es_limit_mb"] == b["es_heap_mb"] * 2 + 512
    parts = (b["os_mb"] + b["es_limit_mb"] + b["neo4j_heap_mb"] + b["neo4j_pagecache_mb"] + b["libreoffice_mb"] + b["ocr_limit_mb"] + b["rest_mb"])
    assert parts <= total
    assert b["libreoffice_bytes"] == b["libreoffice_mb"] * 1048576
    assert b["rest_mb"] >= 0 and b["ocr_overridden"] is False


def test_written_ocr_memory_limit_smaller_than_auto_wins_and_is_not_overwritten(tmp_path):
    b = _budget(tmp_path, 15 * GB, SHERPA_OCR_MEMORY_LIMIT="2g")
    assert b["ocr_overridden"] is True and b["ocr_limit_mb"] == 2048
    env_lines = subprocess.run(["bash", str(LIB), "--total-mb", str(15 * GB), "--env"],
                               env=_env(tmp_path, SHERPA_OCR_MEMORY_LIMIT="2g"), capture_output=True, text=True, timeout=30).stdout
    assert "SHERPA_OCR_MEMORY_LIMIT" not in env_lines
    env_lines = subprocess.run(["bash", str(LIB), "--total-mb", str(15 * GB), "--env"],
                               env=_env(tmp_path), capture_output=True, text=True, timeout=30).stdout
    assert "export SHERPA_OCR_MEMORY_LIMIT=4096m" in env_lines


def test_written_ocr_memory_limit_larger_than_auto_is_capped(tmp_path):
    b = _budget(tmp_path, 15 * GB, SHERPA_OCR_MEMORY_LIMIT="8g")
    assert b["ocr_overridden"] is False and b["ocr_limit_mb"] == 4096
    b = _budget(tmp_path, 8 * GB, SHERPA_OCR_MEMORY_LIMIT="8g")
    assert b["ocr_limit_mb"] == EXPECTED[8 * GB][6]
    env_lines = subprocess.run(["bash", str(LIB), "--total-mb", str(15 * GB), "--env"],
                               env=_env(tmp_path, SHERPA_OCR_MEMORY_LIMIT="8g"), capture_output=True, text=True, timeout=30).stdout
    assert "export SHERPA_OCR_MEMORY_LIMIT=4096m" in env_lines


def test_ocr_memory_limit_in_dotenv_is_read(tmp_path):
    env = _env(tmp_path)
    (tmp_path / "empty.env").write_text("SHERPA_OCR_MEMORY_LIMIT=3g\n", encoding="utf-8")
    r = subprocess.run(["bash", str(LIB), "--total-mb", str(15 * GB), "--json"], env=env, capture_output=True, text=True, timeout=30)
    assert json.loads(r.stdout)["ocr_limit_mb"] == 3072


def test_docker_vm_smaller_than_host_is_used(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text("#!/bin/sh\necho 8264000000\n", encoding="utf-8")
    (fake / "docker").chmod(0o755)
    env = _env(tmp_path)
    env["PATH"] = f"{fake}:{env['PATH']}"
    r = subprocess.run(["bash", str(LIB), "--json"], env=env, capture_output=True, text=True, timeout=30)
    b = json.loads(r.stdout)
    assert b["docker_mb"] == 7881 and b["total_source"] == "docker" and b["total_mb"] == 7881


def test_compose_config_carries_heap_pagecache_limit_and_restart(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("docker が無い")
    script = (f'eval "$(bash {LIB} --total-mb {15 * GB} --env)"; '
              f'docker compose --env-file /dev/null --profile ocr -f {ROOT / "docker-compose.yml"} config --format json')
    r = subprocess.run(["bash", "-c", script], env=_env(tmp_path, COMPOSE_PROJECT_NAME="mem_budget_cfg"),
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    if r.returncode != 0 and "docker compose" in (r.stderr or "").lower() and "not a docker command" in r.stderr:
        pytest.skip("docker compose が無い")
    assert r.returncode == 0, r.stderr
    svc = json.loads(r.stdout)["services"]
    neo = svc["neo4j"]["environment"]
    assert neo["NEO4J_server_memory_heap_initial__size"] == neo["NEO4J_server_memory_heap_max__size"] == "1228m"
    assert neo["NEO4J_server_memory_pagecache_size"] == "614m"
    es = svc["elasticsearch"]
    assert es["environment"]["ES_JAVA_OPTS"] == "-Xms768m -Xmx768m"
    assert int(es["mem_limit"]) == 2048 * 1024 * 1024
    assert int(svc["ocr-worker"]["mem_limit"]) == 4096 * 1024 * 1024
    for name in ("postgres", "neo4j", "elasticsearch"):
        assert svc[name]["restart"] == "unless-stopped"
