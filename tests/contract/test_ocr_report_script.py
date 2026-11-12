"""`scripts/ocr_report.sh`（OCR レポート）: 試験用 DB の仕事から状態・理由・種類・大きさ・重なりの表を出し、
資料フォルダ名・ファイル名・理由の自由文を画面にも詳細にも出さない。bash 3.2 でも動く書き方。"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Json

from sherpa import store
from sherpa.store import ocr_jobs

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ocr_report.sh"

# 出力に出してはいけない名前（架空）
WORLD_BIG = "機密プロジェクトXYZ"
WORLD_SMALL = "他社案件ABC"
FILE_NAME = "極秘設計書_ABC.xlsx"
FREE_TEXT_CODE = "極秘設計書_ABC"
FORBIDDEN = [WORLD_BIG, WORLD_SMALL, FILE_NAME, FREE_TEXT_CODE, "極秘", "機密", "他社案件"]


def _job(world: str, route: str, status: str, *, kind: str, sha: str | None = None,
         size: tuple[int, int] | None = None, error_code: str | None = None) -> None:
    route_input = {"route_input_id": route, "status": "selected", "input_kind": "asset", "reason_code": kind,
                   "asset_sha256": sha, "pixel_size": list(size) if size else None, "asset_rel_path": FILE_NAME}
    if kind == "scan_page_render_fallback":
        route_input.update(input_kind="page_render", asset_sha256=None, asset_rel_path=None)
    leased = status == "leased"
    with ocr_jobs._connect() as connection:
        connection.execute(
            "INSERT INTO ocr_jobs (world, source_rel_path, canonical_generation_id, source_content_hash, "
            "route_manifest_hash, route_input_id, route_input, engine_profile_hash, status, attempts, "
            "error_code, lease_owner, lease_token, lease_expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,CASE WHEN %s THEN now() + interval '1 hour' END)",
            (
                world, FILE_NAME, "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64, route, Json(route_input),
                "sha256:" + "e" * 64, status, error_code, "w" if leased else None, "t" if leased else None, leased,
            ),
        )


@pytest.fixture()
def seeded():
    try:
        store.init_schema()
    except Exception as exc:
        pytest.skip(f"infra down: {exc}")
    dsn = os.environ.get("SHERPA_PG_DSN", "")
    if "sherpa_test" not in str(conninfo_to_dict(dsn).get("dbname", "")):
        pytest.skip("試験用 DB ではないため、仕事の表を空にする試験は流さない")
    if not shutil.which("psql") and not shutil.which("docker"):
        pytest.skip("infra down: psql も docker も無い")
    with ocr_jobs._connect() as connection:
        connection.execute("DELETE FROM ocr_jobs")
    sha = {name: "sha256:" + name * 64 for name in "12345"}
    emb, mimg, mdraw, page = "evidence_raster_asset", "metafile_embedded_bitmap", "metafile_rendered", "scan_page_render_fallback"
    # 件数の多い WORLD_BIG が W1、WORLD_SMALL が W2
    _job(WORLD_BIG, "a1", "failed", kind=emb, sha=sha["1"], size=(4299, 3036), error_code="oom")
    _job(WORLD_BIG, "a2", "succeeded", kind=emb, sha=sha["1"], size=(4299, 3036))
    _job(WORLD_BIG, "a3", "succeeded", kind=emb, sha=sha["2"], size=(800, 600))
    _job(WORLD_BIG, "a4", "failed", kind=mimg, sha=sha["3"], size=(2000, 1500), error_code="timeout")
    _job(WORLD_BIG, "a5", "queued", kind=mdraw, sha=sha["4"], size=(3000, 3000))
    _job(WORLD_BIG, "a6", "succeeded", kind=page)
    _job(WORLD_SMALL, "b1", "failed", kind=page, error_code="engine_failure")
    _job(WORLD_SMALL, "b2", "leased", kind=emb, sha=sha["5"], size=(5000, 4000))
    _job(WORLD_SMALL, "b3", "failed", kind=emb, sha=sha["1"], size=(4299, 3036), error_code=FREE_TEXT_CODE)
    yield
    with ocr_jobs._connect() as connection:
        connection.execute("DELETE FROM ocr_jobs")


def _run(tmp_path: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), "--out", str(tmp_path / "out"), *args],
        capture_output=True, text=True, cwd=cwd or tmp_path, env=dict(os.environ), timeout=300,
    )


def _section(text: str, title: str) -> list[str]:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(title))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(("[", "詳細:"))), len(lines))
    return lines[start:end]


def _cells(lines: list[str], label: str) -> list[str]:
    line = next(item for item in lines if item.strip().startswith(label))
    return line.split()


def test_report_prints_tables_for_the_test_database(seeded, tmp_path):
    done = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    out = done.stdout

    assert _cells(_section(out, "[仕事の状態]"), "全体")[1:] == ["1", "1", "3", "4", "0", "0", "9"]
    assert _cells(_section(out, "[仕事の状態]"), "W1")[1:] == ["1", "0", "3", "2", "0", "0", "6"]
    assert _cells(_section(out, "[仕事の状態]"), "W2")[1:] == ["0", "1", "0", "2", "0", "0", "3"]

    reasons = _section(out, "[失敗の理由]")
    assert _cells(reasons, "oom")[:3] == ["oom", "メモリ不足", "1"]
    assert _cells(reasons, "timeout")[:3] == ["timeout", "時間切れ", "1"]
    assert _cells(reasons, "other")[1:2] == ["1"]

    kinds = _section(out, "[対象の種類]")
    assert _cells(kinds, "埋め込み画像")[1:] == ["5", "2", "2", "1"]
    assert any(line.split()[:2] == ["WMF/EMF", "の中の画像"] and line.split()[2:] == ["1", "0", "1", "0"] for line in kinds)
    assert any(line.split()[:2] == ["WMF/EMF", "の図全体の描画"] and line.split()[2:] == ["1", "0", "0", "1"] for line in kinds)
    assert _cells(kinds, "スキャン")[3:] == ["2", "1", "1", "0"]

    bands = _section(out, "[画像の大きさ]")
    assert _cells(bands, "12～16M画素")[1:] == ["3", "1", "2", "1", "0"]
    assert _cells(bands, "16M画素～")[1:] == ["1", "0", "0", "0", "1"]
    assert _cells(bands, "大きさ不明")[1:] == ["2", "1", "1", "0", "0"]

    dups = _section(out, "[同じ画像を除く]")
    assert _cells(dups, "全体")[1:] == ["9", "7", "2"]
    assert _cells(dups, "未完了（待ち・実行中・失敗）")[1:] == ["6", "5", "1"]
    assert _cells(dups, "失敗")[1:] == ["4", "3", "1"]
    assert _cells(dups, "失敗のうち同じ画像が成功済み")[1:] == ["2", "1", "1"]

    assert len(out.splitlines()) <= 40


def test_report_saves_detail_and_never_prints_names_or_free_text(seeded, tmp_path):
    done = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    shown = re.search(r"^詳細: (.+)$", done.stdout, re.M)
    assert shown is not None
    saved = sorted((tmp_path / "out").glob("ocr-report-*-detail.txt"))
    assert len(saved) == 1 and saved[0].name in shown.group(1)
    detail = saved[0].read_text(encoding="utf-8")
    assert "W1" in detail and "[資料フォルダ × 失敗の理由]" in detail and "[対象の種類 × 画像の大きさ × 状態]" in detail
    summary = next((tmp_path / "out").glob("ocr-report-*-summary.txt")).read_text(encoding="utf-8")
    for text in (done.stdout, done.stderr, detail, summary):
        for word in FORBIDDEN:
            assert word not in text
        assert "写真を撮って" not in text


def test_report_runs_from_any_directory_and_reports_empty_queue(seeded, tmp_path):
    with ocr_jobs._connect() as connection:
        connection.execute("DELETE FROM ocr_jobs")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = _run(tmp_path, cwd=elsewhere)
    assert done.returncode == 0, done.stderr
    assert "[OCR] 仕事はありません" in done.stdout


def test_pruning_old_reports_does_not_print_the_saved_path_when_removal_fails(seeded, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for index in range(12):
        stamp = f"20200101-{index:06d}"
        (out / f"ocr-report-{stamp}-detail.txt").write_text("old", encoding="utf-8")
        (out / f"ocr-report-{stamp}-summary.txt").write_text("old", encoding="utf-8")
    # 消せない状況を作る外部コマンドの代役（保存先の絶対パスを stderr に出す本物の rm の失敗を模す）
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_rm = fake_bin / "rm"
    fake_rm.write_text(
        '#!/bin/sh\nfor a in "$@"; do case "$a" in -*) ;; *) echo "rm: $a: Permission denied" >&2 ;; esac; done\nexit 1\n'
    )
    fake_rm.chmod(0o755)
    done = subprocess.run(
        ["/bin/bash", str(SCRIPT), "--out", str(out)],
        capture_output=True, text=True, cwd=tmp_path, timeout=300,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
    )
    assert done.returncode == 0, done.stderr
    assert str(tmp_path) not in done.stderr and str(tmp_path) not in done.stdout
    assert "古いレポートを消せませんでした" in done.stdout
