"""閉域導入の運用面の案内（LAN 公開・vm.max_map_count）の契約。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
INSTALL_KIT = SCRIPTS / "install_offline_kit.sh"
CHECK_PRODUCTION = SCRIPTS / "check-production.sh"
MANUAL = ROOT / "docs" / "manual" / "offline-kit.md"


@pytest.mark.parametrize("path", [INSTALL_KIT, MANUAL], ids=lambda p: p.name)
def test_guidance_lan_option_and_max_map_count_without_downgrade(path: Path):
    src = path.read_text(encoding="utf-8")
    # LAN 公開は make start（LAN=1／SHERPA_LAN=1）で案内し、127.0.0.1 のみで足りる場合との違いも示す
    for needle in ("make start", "LAN=1", "SHERPA_LAN=1", "127.0.0.1"):
        assert needle in src, needle
    # vm.max_map_count は 262144 を求めるが、同居ホストの既存設定を下げない（sysctl.d は番号順・後勝ち）
    for needle in ("262144", "99-sherpa-vm-max-map-count.conf", "後勝ち", "下げ", "grep -rn max_map_count"):
        assert needle in src, needle
    if path == INSTALL_KIT:
        assert "run-api.sh serve" not in src   # 起動手順は make start／scripts/start.sh


def test_check_production_checks_max_map_count_read_only():
    src = CHECK_PRODUCTION.read_text(encoding="utf-8")
    assert "262144" in src and "max_map_count" in src
    for lineno, raw in enumerate(src.splitlines(), start=1):
        line = raw.strip()
        if line.startswith(("fail ", "warn ", "ok ", "echo ", "#")):
            continue   # 案内文・コメントの中での言及は書込みではない
        assert not line.startswith(("sysctl -w", "sysctl --system")), f"line {lineno}: {raw}"
        assert "tee " not in line or "/etc/sysctl.d" not in line, f"line {lineno}: writes to sysctl.d: {raw}"


@pytest.mark.parametrize("value,needles", [
    ("65530", ["vm.max_map_count=65530", "NG", "99-sherpa-vm-max-map-count.conf"]),
    ("1048576", ["OK: vm.max_map_count=1048576"]),
])
def test_check_production_max_map_count_threshold(tmp_path: Path, value, needles):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sysctl = fake_bin / "sysctl"
    fake_sysctl.write_text(f"#!/usr/bin/env bash\necho {value}\n", encoding="utf-8")
    fake_sysctl.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ.get('PATH', '')}",
               SHERPA_ENV_FILE=str(tmp_path / "does-not-exist.env"))   # `fail()` は非0終了しないので後続の検査まで進む
    r = subprocess.run([str(CHECK_PRODUCTION)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert all(n in out for n in needles), out
