"""OCR ワーカーのコードが、閉域（ベースイメージ無し）でも更新される契約（静的検査）。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_no_base_branch_rebuilds_overlay_instead_of_reusing_stale_image():
    up = _read("scripts/ocr-up.sh")
    start = up.index("ベースイメージが無く")
    branch = up[start:up.index("\nelse\n", start)]
    assert "docker build" in branch and "Dockerfile.overlay" in branch
    assert "SHERPA_APP_VERSION" in branch
    assert "up -d ocr-worker" in branch
    # 通常経路（ベースあり）は従来どおりフルビルド
    assert "up -d --build ocr-worker" in up


def test_overlay_replaces_sherpa_tree_and_labels_version():
    ov = _read("docker/ocr/Dockerfile.overlay")
    assert "rm -rf /app/sherpa" in ov and "COPY sherpa /app/sherpa" in ov
    assert ov.index("rm -rf /app/sherpa") < ov.index("COPY sherpa")
    assert "sherpa.code-version" in ov


def test_worker_code_version_is_visible_in_status():
    assert "sherpa.code-version" in _read("docker/ocr/compose.version.yml")
    assert "compose.version.yml" in _read("scripts/ocr-up.sh")
    status = _read("scripts/status.sh")
    assert "ワーカーのコードが古い" in status and "make up" in status


def test_fetch_ocr_models_preflights_shared_libs_on_linux_only():
    fetch = _read("scripts/fetch_ocr_models.sh")
    pre = fetch.index("libGL.so.1")
    assert pre < fetch.index("import paddleocr")
    assert 'uname -s)" = "Linux"' in fetch
    for lib in ("libglib-2.0.so.0", "libgomp.so.1"):
        assert lib in fetch
    assert "sudo apt-get install -y libgl1 libglib2.0-0 libgomp1" in fetch


def test_failed_code_update_is_not_swallowed_and_status_compares_commit():
    up = _read("scripts/ocr-up.sh")
    assert "コード更新に失敗" in up and "exit 1" in up[up.index("コード更新に失敗"):]
    assert "ocr-up.sh || true" not in _read("Makefile")
    start = _read("scripts/start.sh")
    assert "ocr-up.sh || true" not in start and "ocr_up_rc" in start
    status = _read("scripts/status.sh")
    assert "rev-parse --short HEAD" in status and "_cmp_img" in status
