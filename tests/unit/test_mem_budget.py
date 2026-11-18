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
    assert b["neo4j_tx_memory_mb"] == b["neo4j_heap_mb"] * 70 // 100


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


def test_16gb_machine_default_keeps_heap_and_makes_write_limit_explicit(tmp_path):
    """試験環境（16GB）で 1 回の書き込みの上限が 896MiB になった割り振りを、そのまま明示の値として固定する。"""
    b = _budget(tmp_path, 16000)
    assert (b["neo4j_heap_mb"], b["neo4j_pagecache_mb"], b["neo4j_tx_memory_mb"]) == (1280, 640, 896)
    env_lines = subprocess.run(["bash", str(LIB), "--total-mb", "16000", "--env"],
                               env=_env(tmp_path), capture_output=True, text=True, timeout=30).stdout
    assert "export SHERPA_NEO4J_TX_MEMORY_RESOLVED=896m" in env_lines


def test_neo4j_fixed_values_replace_auto_and_leave_other_parts_as_computed(tmp_path):
    auto = _budget(tmp_path, 16000)
    b = _budget(tmp_path, 16000, SHERPA_NEO4J_HEAP_FIXED="3g", SHERPA_NEO4J_PAGECACHE_FIXED="1024m")
    assert (b["neo4j_heap_mb"], b["neo4j_pagecache_mb"]) == (3072, 1024)
    assert b["neo4j_tx_memory_mb"] == 3072 * 70 // 100                  # 書き込み上限はヒープに追随
    for k in ("os_mb", "es_heap_mb", "es_limit_mb", "libreoffice_mb", "ocr_limit_mb"):
        assert b[k] == auto[k]
    assert b["rest_mb"] == auto["rest_mb"] - (3072 - 1280) - (1024 - 640)


def test_neo4j_fixed_too_large_for_the_machine_shows_the_low_rest_warning(tmp_path):
    r = subprocess.run(["bash", str(LIB), "--total-mb", "8192"], env=_env(tmp_path, SHERPA_NEO4J_HEAP_FIXED="6g"),
                       capture_output=True, text=True, timeout=30)
    assert "Neo4j ヒープ 6.0" in r.stdout and "残りが少ない" in r.stdout
    assert _budget(tmp_path, 8192, SHERPA_NEO4J_HEAP_FIXED="6g")["rest_mb"] == 0


def test_neo4j_write_limit_setting_absolute_and_unlimited(tmp_path):
    assert _budget(tmp_path, 16000, SHERPA_NEO4J_TX_MEMORY_MAX="2g")["neo4j_tx_memory_mb"] == 2048
    b = _budget(tmp_path, 16000, SHERPA_NEO4J_TX_MEMORY_MAX="0")
    assert b["neo4j_tx_memory_mb"] == 0
    env_lines = subprocess.run(["bash", str(LIB), "--total-mb", "16000", "--env"],
                               env=_env(tmp_path, SHERPA_NEO4J_TX_MEMORY_MAX="0"), capture_output=True, text=True, timeout=30).stdout
    assert "export SHERPA_NEO4J_TX_MEMORY_RESOLVED=0\n" in env_lines


@pytest.mark.parametrize("var,bad", [
    ("SHERPA_NEO4J_HEAP_FIXED", "4096"),           # 単位なしはバイト扱い
    ("SHERPA_NEO4J_HEAP_FIXED", "100m"),           # 下限（512m）未満
    ("SHERPA_NEO4J_HEAP_FIXED", "lots"),
    ("SHERPA_NEO4J_PAGECACHE_FIXED", "64m"),
    ("SHERPA_NEO4J_TX_MEMORY_MAX", "5m"),          # Neo4j の下限（10MiB）未満
])
def test_neo4j_unreadable_settings_are_ignored_and_named_in_the_line(tmp_path, var, bad):
    auto = _budget(tmp_path, 16000)
    assert _budget(tmp_path, 16000, **{var: bad}) == auto
    r = subprocess.run(["bash", str(LIB), "--total-mb", "16000"], env=_env(tmp_path, **{var: bad}),
                       capture_output=True, text=True, timeout=30)
    assert f"無視した設定: {var}" in r.stdout


def test_neo4j_fixed_values_in_dotenv_are_read(tmp_path):
    env = _env(tmp_path)
    (tmp_path / "empty.env").write_text("SHERPA_NEO4J_HEAP_FIXED=2g\nSHERPA_NEO4J_TX_MEMORY_MAX=1g\n", encoding="utf-8")
    r = subprocess.run(["bash", str(LIB), "--total-mb", "16000", "--json"], env=env, capture_output=True, text=True, timeout=30)
    b = json.loads(r.stdout)
    assert (b["neo4j_heap_mb"], b["neo4j_tx_memory_mb"]) == (2048, 1024)


def _fake_docker(tmp_path: pathlib.Path) -> tuple[dict[str, str], pathlib.Path]:
    """`docker info` には巨大な全体を答え、compose の呼び出しでは環境に渡った Neo4j の値を記録する。"""
    fake = tmp_path / "bin"
    fake.mkdir()
    out = tmp_path / "compose-env.txt"
    (fake / "docker").write_text(
        '#!/bin/sh\n'
        'if [ "$1" = info ]; then echo 1099511627776; exit 0; fi\n'
        'echo "$SHERPA_NEO4J_HEAP_RESOLVED $SHERPA_NEO4J_PAGECACHE_RESOLVED $SHERPA_NEO4J_TX_MEMORY_RESOLVED" > "$OUT"\n',
        encoding="utf-8")
    (fake / "docker").chmod(0o755)
    env = _env(tmp_path, OUT=str(out))
    env["PATH"] = f"{fake}:{env['PATH']}"
    return env, out


def test_dotenv_fixed_values_reach_compose_through_sherpa_compose_and_start_path(tmp_path):
    """.env の設定が、make start（先に割り振って export → 以降の compose 呼び出し）と、割り振り前の
    compose 呼び出し（restart・ocr-up など）のどちらでも compose の環境へ渡る。"""
    env, out = _fake_docker(tmp_path)
    (tmp_path / "empty.env").write_text(
        "SHERPA_NEO4J_HEAP_FIXED=5g\nSHERPA_NEO4J_PAGECACHE_FIXED=3g\nSHERPA_NEO4J_TX_MEMORY_MAX=4g\n", encoding="utf-8")
    common = f'ROOT="{ROOT}"; . "{ROOT / "scripts" / "run-common.sh"}"; '
    r = subprocess.run(["bash", "-c", common + "sherpa_compose up -d"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert out.read_text().split() == ["5120m", "3072m", "4096m"]       # 自動の上限（ヒープ 4096・キャッシュ 2048）より大きい
    out.unlink()
    start_path = common + ". \"$ROOT/scripts/lib/mem_budget.sh\"; mem_budget_load_env; mem_budget_apply; sherpa_compose up -d"
    r = subprocess.run(["bash", "-c", start_path], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert out.read_text().split() == ["5120m", "3072m", "4096m"]


def test_no_fixed_values_keep_auto_caps_in_compose_env(tmp_path):
    env, out = _fake_docker(tmp_path)
    common = f'ROOT="{ROOT}"; . "{ROOT / "scripts" / "run-common.sh"}"; '
    r = subprocess.run(["bash", "-c", common + "sherpa_compose up -d"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    heap, pc, tx = out.read_text().split()
    assert int(heap[:-1]) <= 4096 and int(pc[:-1]) <= 2048 and int(tx[:-1]) == int(heap[:-1]) * 70 // 100


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
    assert neo["NEO4J_dbms_memory_transaction_total_max"] == "859m"
    es = svc["elasticsearch"]
    assert es["environment"]["ES_JAVA_OPTS"] == "-Xms768m -Xmx768m"
    assert int(es["mem_limit"]) == 2048 * 1024 * 1024
    assert int(svc["ocr-worker"]["mem_limit"]) == 4096 * 1024 * 1024
    for name in ("postgres", "neo4j", "elasticsearch"):
        assert svc[name]["restart"] == "unless-stopped"


@pytest.mark.parametrize("extra,expected", [
    ({}, "4300m"),                                         # ヒープ 6g の 7 割
    ({"SHERPA_NEO4J_TX_MEMORY_MAX": "5g"}, "5120m"),
    ({"SHERPA_NEO4J_TX_MEMORY_MAX": "0"}, "0"),
])
def test_resolved_heap_without_write_limit_gets_the_limit_from_the_heap(tmp_path, extra, expected):
    """割り振り済み（SHERPA_NEO4J_HEAP_RESOLVED を渡した）ときも、1 回の書き込みの上限を compose の小さい既定値に落とさない。"""
    env, out = _fake_docker(tmp_path)
    env.update({"SHERPA_NEO4J_HEAP_RESOLVED": "6g", "SHERPA_NEO4J_PAGECACHE_RESOLVED": "1g", **extra})
    common = f'ROOT="{ROOT}"; . "{ROOT / "scripts" / "run-common.sh"}"; '
    start_path = common + ". \"$ROOT/scripts/lib/mem_budget.sh\"; mem_budget_load_env; mem_budget_apply; sherpa_compose up -d"
    r = subprocess.run(["bash", "-c", start_path], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert out.read_text().split() == ["6g", "1g", expected]
