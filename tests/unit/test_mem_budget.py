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


@pytest.mark.parametrize("total", TOTALS)
def test_default_has_one_worker_and_no_new_variable_effect(tmp_path, total):
    b = _budget(tmp_path, total)
    assert b["ocr_workers"] == b["ocr_workers_wanted"] == 1 and b["ocr_fixed"] is False
    other = (b["os_mb"] + b["es_limit_mb"] + b["neo4j_heap_mb"] + b["neo4j_pagecache_mb"] + b["libreoffice_mb"])
    assert b["rest_mb"] == max(0, total - other - b["ocr_limit_mb"])


def test_fixed_limit_can_exceed_the_auto_cap_and_replaces_dotenv_value(tmp_path):
    b = _budget(tmp_path, 32 * GB, SHERPA_OCR_MEMORY_FIXED="12g")
    assert b["ocr_fixed"] is True and b["ocr_overridden"] is False and b["ocr_limit_mb"] == 12 * GB
    assert b["rest_mb"] == 32 * GB - 3276 - 3788 - 2621 - 1310 - 4096 - 12 * GB
    env_lines = subprocess.run(["bash", str(LIB), "--total-mb", str(32 * GB), "--env"],
                               env=_env(tmp_path, SHERPA_OCR_MEMORY_FIXED="12g", SHERPA_OCR_MEMORY_LIMIT="2g"),
                               capture_output=True, text=True, timeout=30).stdout
    assert "export SHERPA_OCR_MEMORY_LIMIT=12288m" in env_lines


def test_fixed_limit_beats_limit_and_unreadable_fixed_is_ignored(tmp_path):
    assert _budget(tmp_path, 32 * GB, SHERPA_OCR_MEMORY_FIXED="10g", SHERPA_OCR_MEMORY_LIMIT="2g")["ocr_limit_mb"] == 10 * GB
    for bad in ("abc", "0", "100m"):
        b = _budget(tmp_path, 32 * GB, SHERPA_OCR_MEMORY_FIXED=bad)
        assert b["ocr_fixed"] is False and b["ocr_limit_mb"] == EXPECTED[32 * GB][6]


def test_fixed_limit_over_the_remainder_warns(tmp_path):
    env = _env(tmp_path, SHERPA_OCR_MEMORY_FIXED="12g")
    line = subprocess.run(["bash", str(LIB), "--total-mb", str(15 * GB), "--line"], env=env, capture_output=True, text=True, timeout=30).stdout
    assert "指定値" in line and "残りが少ない" in line
    assert _budget(tmp_path, 15 * GB, SHERPA_OCR_MEMORY_FIXED="12g")["rest_mb"] == 0


def test_workers_multiply_the_per_worker_limit(tmp_path):
    b = _budget(tmp_path, 64 * GB, SHERPA_OCR_WORKERS="3")
    assert b["ocr_workers"] == 3 and b["ocr_limit_mb"] == 8192
    solo = _budget(tmp_path, 64 * GB)
    assert b["rest_mb"] == solo["rest_mb"] - 2 * 8192
    env_lines = subprocess.run(["bash", str(LIB), "--total-mb", str(64 * GB), "--env"],
                               env=_env(tmp_path, SHERPA_OCR_WORKERS="3"), capture_output=True, text=True, timeout=30).stdout
    assert "export SHERPA_OCR_WORKERS_RESOLVED=3" in env_lines


def test_workers_with_fixed_limit_use_fixed_times_count(tmp_path):
    b = _budget(tmp_path, 64 * GB, SHERPA_OCR_MEMORY_FIXED="10g", SHERPA_OCR_WORKERS="3")
    assert b["ocr_workers"] == 3 and b["ocr_limit_mb"] == 10 * GB
    solo = _budget(tmp_path, 64 * GB, SHERPA_OCR_MEMORY_FIXED="10g")
    assert b["rest_mb"] == solo["rest_mb"] - 2 * 10 * GB


def test_workers_are_reduced_so_the_remainder_stays_at_least_4gb(tmp_path):
    b = _budget(tmp_path, 64 * GB, SHERPA_OCR_WORKERS="9")
    assert b["ocr_workers_wanted"] == 9 and b["ocr_workers"] == 4 and b["rest_mb"] >= 4096
    line = subprocess.run(["bash", str(LIB), "--total-mb", str(64 * GB), "--line"],
                          env=_env(tmp_path, SHERPA_OCR_WORKERS="9"), capture_output=True, text=True, timeout=30).stdout
    assert "×4本" in line and "9本の希望" in line


def test_small_machine_keeps_one_worker_even_if_more_are_asked(tmp_path):
    for total in (4 * GB, 8 * GB, 15 * GB):
        b = _budget(tmp_path, total, SHERPA_OCR_WORKERS="4")
        assert b["ocr_workers"] == 1 and b["ocr_limit_mb"] == EXPECTED[total][6]


def test_unreadable_workers_fall_back_to_one(tmp_path):
    for bad in ("x", "0", "-2", "1.5"):
        assert _budget(tmp_path, 64 * GB, SHERPA_OCR_WORKERS=bad)["ocr_workers"] == 1


def test_docker_vm_smaller_than_host_limits_the_worker_count(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text("#!/bin/sh\necho 17179869184\n", encoding="utf-8")
    (fake / "docker").chmod(0o755)
    env = _env(tmp_path, SHERPA_OCR_WORKERS="4")
    env["PATH"] = f"{fake}:{env['PATH']}"
    b = json.loads(subprocess.run(["bash", str(LIB), "--json"], env=env, capture_output=True, text=True, timeout=30).stdout)
    assert b["total_mb"] <= 16 * GB and b["ocr_workers"] == 1 and b["rest_mb"] >= 0


def test_workers_and_fixed_in_dotenv_are_read(tmp_path):
    env = _env(tmp_path)
    (tmp_path / "empty.env").write_text("SHERPA_OCR_MEMORY_FIXED=10g\nSHERPA_OCR_WORKERS=2\n", encoding="utf-8")
    b = json.loads(subprocess.run(["bash", str(LIB), "--total-mb", str(64 * GB), "--json"], env=env, capture_output=True, text=True, timeout=30).stdout)
    assert b["ocr_limit_mb"] == 10 * GB and b["ocr_workers"] == 2


def test_compose_scales_workers_with_per_container_limit(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("docker が無い")
    script = (f'eval "$(bash {LIB} --total-mb {64 * GB} --env)"; '
              f'docker compose --env-file /dev/null --profile ocr -f {ROOT / "docker-compose.yml"} config --format json')
    r = subprocess.run(["bash", "-c", script], env=_env(tmp_path, COMPOSE_PROJECT_NAME="mem_budget_cfg", SHERPA_OCR_WORKERS="3"),
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    if r.returncode != 0 and "not a docker command" in (r.stderr or ""):
        pytest.skip("docker compose が無い")
    assert r.returncode == 0, r.stderr
    worker = json.loads(r.stdout)["services"]["ocr-worker"]
    assert int(worker["mem_limit"]) == 8192 * 1024 * 1024 and "container_name" not in worker


def _fake_docker_and_nproc(tmp_path: pathlib.Path, docker_bytes: int, docker_cpus: int, host_cpus: int) -> dict[str, str]:
    fake = tmp_path / "bin"
    fake.mkdir(parents=True, exist_ok=True)
    (fake / "docker").write_text(f"#!/bin/sh\necho {docker_bytes} {docker_cpus}\n", encoding="utf-8")
    (fake / "nproc").write_text(f"#!/bin/sh\necho {host_cpus}\n", encoding="utf-8")
    for name in ("docker", "nproc"):
        (fake / name).chmod(0o755)
    env = _env(tmp_path)
    env["PATH"] = f"{fake}:{env['PATH']}"
    return env


def _line_output(env: dict[str, str]) -> str:
    r = subprocess.run(["bash", str(LIB), "--line"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


HUGE = 1 << 40  # どの機械より大きい Docker のメモリ


def test_docker_memory_smaller_than_host_warns_with_how_to_raise_it(tmp_path):
    out = _line_output(_fake_docker_and_nproc(tmp_path, 1 << 30, 8, 8))
    assert "機械: メモリ" in out and "Docker に割り当てられた分: メモリ 1.0GB・CPU 8 個" in out
    assert "Docker に割り当てられたメモリが機械より小さい" in out
    assert "Settings → Resources" in out and ".wslconfig" in out and "memory=" in out and "processors=" in out


def test_docker_cpus_fewer_than_host_warns_only_about_cpu(tmp_path):
    out = _line_output(_fake_docker_and_nproc(tmp_path, HUGE, 2, 8))
    assert "CPU 8 個" in out and "CPU 2 個" in out
    assert "Docker に割り当てられたCPUが機械より小さい" in out and "メモリが機械より" not in out
    assert ".wslconfig" in out


def test_same_resources_as_host_do_not_warn(tmp_path):
    out = _line_output(_fake_docker_and_nproc(tmp_path, HUGE, 8, 8))
    assert "機械: メモリ" in out and "CPU 8 個" in out
    assert "注意" not in out and ".wslconfig" not in out


def test_docker_unavailable_prints_no_warning(tmp_path):
    env = _env(tmp_path)
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (fake / "docker").chmod(0o755)
    env["PATH"] = f"{fake}:{env['PATH']}"
    out = _line_output(env)
    assert "取得できませんでした" in out and "注意" not in out


def test_docker_cpus_are_recorded_and_do_not_change_the_memory_split(tmp_path):
    outs = []
    for name, docker_cpus in (("small", 2), ("same", 8)):
        env = _fake_docker_and_nproc(tmp_path / name, 8264000000, docker_cpus, 8)
        r = subprocess.run(["bash", str(LIB), "--json"], env=env, capture_output=True, text=True, timeout=30)
        outs.append(json.loads(r.stdout))
    small, same = outs
    assert small["docker_cpus"] == 2 and small["host_cpus"] == 8 and same["docker_cpus"] == 8
    cpu_keys = {"docker_cpus", "cpu_total", "ocr_cpus", "ocr_threads"}
    for key in set(small) - cpu_keys:
        assert small[key] == same[key], key
    assert (small["cpu_total"], small["ocr_cpus"]) == (2, 1) and (same["cpu_total"], same["ocr_cpus"]) == (8, 4)
    assert small["total_mb"] == 7881 and small["docker_mb"] == 7881 and small["total_source"] == "docker"
    for key in ("total_mb", "host_mb", "docker_mb", "total_source", "os_mb", "es_heap_mb", "es_limit_mb", "neo4j_heap_mb",
                "neo4j_pagecache_mb", "libreoffice_mb", "libreoffice_bytes", "ocr_limit_mb", "ocr_overridden", "ocr_fixed",
                "ocr_workers", "ocr_workers_wanted", "rest_mb"):
        assert key in small


def test_given_total_has_no_cpu_numbers(tmp_path):
    b = _budget(tmp_path, 15 * GB)
    assert b["host_cpus"] is None and b["docker_cpus"] is None


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


# ---- OCR の CPU の割り振り（CPU の数 C の半分が OCR・残りは他に残す） ----

def _cpu_budget(tmp_path: pathlib.Path, cpus: int, total_mb: int = 64 * GB, **extra: str) -> dict:
    r = subprocess.run(["bash", str(LIB), "--total-mb", str(total_mb), "--cpus", str(cpus), "--json"],
                       env=_env(tmp_path, **extra), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _cpu_env(tmp_path: pathlib.Path, cpus: int, total_mb: int = 64 * GB, **extra: str) -> str:
    return subprocess.run(["bash", str(LIB), "--total-mb", str(total_mb), "--cpus", str(cpus), "--env"],
                          env=_env(tmp_path, **extra), capture_output=True, text=True, timeout=30).stdout


def test_four_cpu_machine_gives_ocr_half_and_leaves_the_rest(tmp_path):
    b = _cpu_budget(tmp_path, 4)
    assert (b["cpu_total"], b["ocr_cpus"], b["ocr_threads"], b["ocr_workers"]) == (4, 2, 2, 1)
    assert b["cpu_total"] - b["ocr_cpus"] * b["ocr_workers"] >= 2
    env_lines = _cpu_env(tmp_path, 4)
    assert "export SHERPA_OCR_CPUS_RESOLVED=2\n" in env_lines and "export SHERPA_OCR_THREADS_RESOLVED=2\n" in env_lines
    for cpus, expected in ((1, 1), (2, 1), (3, 1), (8, 4), (16, 8)):
        assert _cpu_budget(tmp_path, cpus)["ocr_cpus"] == expected, cpus


def test_cpu_overrides_win_and_threads_never_exceed_the_cpu_limit(tmp_path):
    b = _cpu_budget(tmp_path, 4, SHERPA_OCR_CPUS="3")
    assert (b["ocr_cpus"], b["ocr_threads"]) == (3, 3)
    b = _cpu_budget(tmp_path, 8, SHERPA_OCR_CPUS="4", SHERPA_OCR_THREADS="3")
    assert (b["ocr_cpus"], b["ocr_threads"]) == (4, 3)
    b = _cpu_budget(tmp_path, 8, SHERPA_OCR_THREADS="3")        # スレッド数だけ書いたときは 1 本の上限もそれに合わせる
    assert (b["ocr_cpus"], b["ocr_threads"]) == (3, 3)
    b = _cpu_budget(tmp_path, 8, SHERPA_OCR_CPUS="2", SHERPA_OCR_THREADS="6")
    assert (b["ocr_cpus"], b["ocr_threads"]) == (2, 2)
    b = _cpu_budget(tmp_path, 4, SHERPA_OCR_CPUS="16")          # 機械の CPU の数を超えない
    assert (b["ocr_cpus"], b["ocr_threads"]) == (4, 4)


def test_unreadable_cpu_settings_are_ignored_and_reported(tmp_path):
    for bad in ("x", "0", "-1", "1.5"):
        b = _cpu_budget(tmp_path, 4, SHERPA_OCR_CPUS=bad, SHERPA_OCR_THREADS=bad)
        assert (b["ocr_cpus"], b["ocr_threads"]) == (2, 2), bad
    line = subprocess.run(["bash", str(LIB), "--total-mb", str(64 * GB), "--cpus", "4", "--line"],
                          env=_env(tmp_path, SHERPA_OCR_CPUS="x"), capture_output=True, text=True, timeout=30).stdout
    assert "読めない SHERPA_OCR_CPUS を無視" in line


def test_workers_times_cpus_over_the_limit_are_reduced_with_a_warning(tmp_path):
    b = _cpu_budget(tmp_path, 4, SHERPA_OCR_WORKERS="4")        # 取り分は 2 個＝1 個ずつ 2 本まで
    assert (b["ocr_workers_wanted"], b["ocr_workers_by_memory"], b["ocr_workers"], b["ocr_cpus"]) == (4, 4, 2, 1)
    b = _cpu_budget(tmp_path, 4, SHERPA_OCR_WORKERS="2", SHERPA_OCR_CPUS="3")   # 書いた値でも機械の CPU の数は超えない
    assert (b["ocr_workers"], b["ocr_cpus"]) == (1, 3)
    b = _cpu_budget(tmp_path, 8, SHERPA_OCR_WORKERS="2", SHERPA_OCR_CPUS="4")   # ちょうど収まる
    assert b["ocr_workers"] == 2
    line = subprocess.run(["bash", str(LIB), "--total-mb", str(64 * GB), "--cpus", "4", "--line"],
                          env=_env(tmp_path, SHERPA_OCR_WORKERS="4"), capture_output=True, text=True, timeout=30).stdout
    assert "4本を CPU が足りないため 2本に減らしました" in line and "CPUの割り振り: 全体 4個" in line
    assert "SHERPA_OCR_WORKERS_RESOLVED=2" in _cpu_env(tmp_path, 4, SHERPA_OCR_WORKERS="4")
    # 残りのメモリは CPU で減らした後の本数で計算する
    assert b["rest_mb"] == _budget(tmp_path, 64 * GB, SHERPA_OCR_WORKERS="2")["rest_mb"]


def test_memory_cut_and_cpu_cut_are_reported_separately(tmp_path):
    b = _cpu_budget(tmp_path, 4, total_mb=15 * GB, SHERPA_OCR_WORKERS="4")      # メモリで 1 本・CPU は 1 本のまま
    assert (b["ocr_workers_wanted"], b["ocr_workers_by_memory"], b["ocr_workers"]) == (4, 1, 1)


def test_without_cpu_numbers_nothing_is_allocated_and_workers_are_kept(tmp_path):
    b = _budget(tmp_path, 64 * GB, SHERPA_OCR_WORKERS="3")
    assert b["cpu_total"] is None and b["ocr_cpus"] is None and b["ocr_threads"] is None and b["ocr_workers"] == 3
    env_lines = subprocess.run(["bash", str(LIB), "--total-mb", str(64 * GB), "--env"],
                               env=_env(tmp_path), capture_output=True, text=True, timeout=30).stdout
    assert "SHERPA_OCR_CPUS_RESOLVED" not in env_lines and "SHERPA_OCR_THREADS_RESOLVED" not in env_lines


def test_cpu_total_is_the_smaller_of_host_and_docker(tmp_path):
    for host, docker_cpus, expected in ((8, 2, 2), (2, 8, 2), (8, 8, 8)):
        env = _fake_docker_and_nproc(tmp_path / f"{host}-{docker_cpus}", HUGE, docker_cpus, host)
        b = json.loads(subprocess.run(["bash", str(LIB), "--json"], env=env, capture_output=True, text=True, timeout=30).stdout)
        assert b["cpu_total"] == expected and b["ocr_cpus"] == max(1, expected // 2)


def test_compose_gives_the_worker_cpu_cap_and_thread_count(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("docker が無い")
    script = (f'eval "$(bash {LIB} --total-mb {64 * GB} --cpus 8 --env)"; '
              f'docker compose --env-file /dev/null --profile ocr -f {ROOT / "docker-compose.yml"} config --format json')
    r = subprocess.run(["bash", "-c", script], env=_env(tmp_path, COMPOSE_PROJECT_NAME="mem_budget_cfg", SHERPA_OCR_MODEL="mobile",
                                                        SHERPA_OCR_ONEDNN="1"),
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    if r.returncode != 0 and "not a docker command" in (r.stderr or ""):
        pytest.skip("docker compose が無い")
    assert r.returncode == 0, r.stderr
    worker = json.loads(r.stdout)["services"]["ocr-worker"]
    assert float(worker["cpus"]) == 4.0
    assert worker["environment"]["SHERPA_OCR_THREADS"] == "4"
    assert worker["environment"]["SHERPA_OCR_MODEL"] == "mobile" and worker["environment"]["SHERPA_OCR_ONEDNN"] == "1"
