"""閉域導入の運用面の是正（2026-08-18・実機報告⑦⑧）。

不具合:
  ⑦ `install_offline_kit.sh` 末尾の起動案内が `docker compose up -d` ＋ `run-api.sh serve`（127.0.0.1
     待受）だけで、社内 LAN の他端末から使わせる前提なのに `LAN=1`/`SHERPA_LAN` の案内が
     インストーラにもオフラインマニュアルにも無かった。
  ⑧ Elasticsearch の `vm.max_map_count` 案内が「不足していれば恒久設定」だけで、(a) 既に十分な
     値をさらに下げてしまう・(b) 同居する別製品が別ファイルで設定したより大きい値を
     `/etc/sysctl.d/` の後勝ちで踏む、の事故になり得た（実機は同居製品が 1048576 を設定済みで、
     素直に 262144 を書くと下がって壊れた）。

このテストは:
  - `scripts/install_offline_kit.sh`・`docs/manual/offline-kit.md` の双方に、LAN 公開の案内
    （`make start`／`LAN=1`／`SHERPA_LAN=1`）と、vm.max_map_count の安全な案内
    （262144 以上なら何もしない・`99-sherpa-vm-max-map-count.conf`・sysctl.d の後勝ち）が
    存在することをテキストで固定する。
  - `scripts/check-production.sh` に vm.max_map_count の検査（閾値 262144・読み取りのみ）が
    あることを、実行結果で固定する。
"""
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


# ---- ⑦ LAN 公開の案内 -------------------------------------------------------------------------

def test_install_kit_recommends_make_start_with_lan_option():
    src = INSTALL_KIT.read_text(encoding="utf-8")
    assert "make start" in src
    assert "LAN=1" in src
    assert "SHERPA_LAN=1" in src
    # 127.0.0.1 のみで足りる場合との違いが1行で分かること。
    assert "127.0.0.1" in src


def test_manual_recommends_make_start_with_lan_option():
    src = MANUAL.read_text(encoding="utf-8")
    assert "make start" in src
    assert "LAN=1" in src
    assert "SHERPA_LAN=1" in src
    assert "127.0.0.1" in src


def test_install_kit_no_longer_recommends_bare_run_api_serve_as_the_startup_step():
    """旧案内（`docker compose ... up -d` の直後に素の `run-api.sh serve`）を起動手順として
    残していないこと（`make start`／`scripts/start.sh` に置き換え済み）。"""
    src = INSTALL_KIT.read_text(encoding="utf-8")
    assert "run-api.sh serve" not in src


# ---- ⑧ vm.max_map_count（同居ホストを壊さない）-----------------------------------------------

def test_install_kit_max_map_count_guidance_does_not_downgrade():
    src = INSTALL_KIT.read_text(encoding="utf-8")
    assert "262144" in src
    assert "99-sherpa-vm-max-map-count.conf" in src
    assert "後勝ち" in src              # sysctl.d はファイル名の番号順・後勝ちの説明
    assert "下げ" in src                # 「下げないでください/下げると壊れます」等の注意
    assert "grep -rn max_map_count" in src   # 既存設定の探し方


def test_manual_max_map_count_guidance_does_not_downgrade():
    src = MANUAL.read_text(encoding="utf-8")
    assert "262144" in src
    assert "99-sherpa-vm-max-map-count.conf" in src
    assert "後勝ち" in src
    assert "下げ" in src
    assert "grep -rn max_map_count" in src


def test_check_production_checks_max_map_count_read_only():
    """check-production.sh はしきい値 262144 を検査するが、sysctl.d へは一切書き込まない
    （読み取りのみ・T3 の契約）。`fail()` の案内文の中で `sysctl --system`/`/etc/sysctl.d` に
    **言及する**のは許すが、行頭コマンドとして**実行**していないことを確認する
    （＝これらが実行文として現れるのは fail/echo の文字列引数の中だけであること）。"""
    src = CHECK_PRODUCTION.read_text(encoding="utf-8")
    assert "262144" in src
    assert "max_map_count" in src
    for lineno, raw in enumerate(src.splitlines(), start=1):
        line = raw.strip()
        if line.startswith(("fail ", "warn ", "ok ", "echo ", "#")):
            continue   # 案内文・コメントの中での言及は書込みではない
        assert not line.startswith("sysctl -w"), f"line {lineno}: writes via sysctl -w: {raw}"
        assert not line.startswith("sysctl --system"), f"line {lineno}: executes sysctl --system: {raw}"
        assert "tee " not in line or "/etc/sysctl.d" not in line, f"line {lineno}: writes to sysctl.d: {raw}"


# ---- check-production.sh の実行（外部サービス不要・sysctl をフェイクで差し替え） --------------

def _run_check_production(tmp_path: Path, fake_sysctl_output: str) -> subprocess.CompletedProcess:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sysctl = fake_bin / "sysctl"
    fake_sysctl.write_text(f"#!/usr/bin/env bash\necho {fake_sysctl_output}\n", encoding="utf-8")
    fake_sysctl.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    # env ファイルは意図的に存在しないパスにする（この検査より前の項目は失敗するが、
    # `fail()` は非0終了しない実装のため、後続の vm.max_map_count 検査までスクリプトは進む）。
    env["SHERPA_ENV_FILE"] = str(tmp_path / "does-not-exist.env")
    return subprocess.run([str(CHECK_PRODUCTION)], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=120)


def test_check_production_ng_when_max_map_count_too_low(tmp_path: Path):
    r = _run_check_production(tmp_path, "65530")
    out = r.stdout + r.stderr
    assert "vm.max_map_count=65530" in out
    assert "NG" in out
    assert "99-sherpa-vm-max-map-count.conf" in out


def test_check_production_ok_when_max_map_count_sufficient(tmp_path: Path):
    r = _run_check_production(tmp_path, "1048576")
    out = r.stdout + r.stderr
    assert "OK: vm.max_map_count=1048576" in out
