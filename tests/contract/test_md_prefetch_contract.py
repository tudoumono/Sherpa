"""MD化の先読み（`md_prefetch`）の契約: ワーカー数を変えても派生物は 1 バイトも変わらない。

直列（SHERPA_MD_WORKERS=1）と並列（=3）で同じ実コーパスを `office_md.build_derived` にかけ、
全派生ファイル（md／rag／ir 層・サイドカー・マーカー）と返り値の rep が一致することを固定する。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sherpa.ingest import md_prefetch, office_md

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
CORPUS = (
    "eval/office_ja/inputs/OJA-PPTX-EASY.pptx",
    "eval/office_ja/inputs/OJA-PDF-EASY.pdf",
    "eval/office_ja/inputs/OJA-PDF-MEDIUM.pdf",
    "eval/excel_ja/inputs/JPX-001.xlsx",
    "eval/excel_ja/inputs/JPX-002.xlsx",
    "pdf_export/PDFX-MERGED.docx",
    "pdf_export/PDFX-MERGED.pdf",
)


def _build(src: Path, out: Path, workers: int, monkeypatch, caplog) -> tuple[dict, dict[str, bytes]]:
    monkeypatch.setenv("SHERPA_MD_WORKERS", str(workers))
    caplog.clear()
    with caplog.at_level("INFO", logger="sherpa.ingest.convert"):
        rep = office_md.build_derived(src, out / "md")
    assert any("先読みします" in r.getMessage() for r in caplog.records) is (workers > 1)
    files = {
        p.relative_to(out).as_posix(): p.read_bytes()
        for p in sorted(out.rglob("*"))
        if p.is_file() and not p.relative_to(out).parts[0].startswith("_")   # `_conv_cache` 等は絶対パス・mtime を含む
    }
    return rep, files


def test_workers_do_not_change_derived_bytes(tmp_path, monkeypatch, caplog):
    src = tmp_path / "src"
    src.mkdir()
    for name in CORPUS:
        shutil.copy(FIXTURES / name, src / Path(name).name)
    (src / "broken.docx").write_bytes(b"not a zip")        # 失敗の知らせまで同じになること

    rep1, files1 = _build(src, tmp_path / "w1", 1, monkeypatch, caplog)
    rep3, files3 = _build(src, tmp_path / "w3", 3, monkeypatch, caplog)

    assert rep1["converted"] >= len(CORPUS) - 1 and rep1["failed"] >= 1
    assert any(k.endswith(".document.json") for k in files1) and any(k.endswith(".evidence.json") for k in files1)
    assert sorted(files3) == sorted(files1)
    assert [k for k in files1 if files1[k] != files3[k]] == []
    assert rep3 == rep1


def test_failed_job_returns_none_and_next_job_still_runs():
    """子が例外を投げたファイルは None（親が直列で計算し直す）。後続は影響を受けない。"""
    good = FIXTURES / "eval/office_ja/inputs/OJA-PDF-EASY.pdf"
    jobs = [
        md_prefetch.Job("bad.pdf", str(good), "no_such_arm", ".pdf"),
        md_prefetch.Job("good.pdf", str(good), "pdf_text", ".pdf"),
    ]
    with md_prefetch.Prefetcher(jobs, 2) as pf:
        assert pf.take("bad.pdf") is None
        got = pf.take("good.pdf")
        assert got is not None and got.result.md and got.evidence is not None
        assert pf.take("good.pdf") is None            # 取り出し済み


@pytest.mark.parametrize("raw", ["0", "9", "x", ""])
def test_worker_count_invalid_falls_back_to_default(monkeypatch, raw):
    monkeypatch.delenv("SHERPA_MD_WORKERS", raising=False)
    default = md_prefetch.worker_count()
    monkeypatch.setenv("SHERPA_MD_WORKERS", raw)
    assert md_prefetch.worker_count() == default
    assert 1 <= default <= 4
    monkeypatch.setenv("SHERPA_MD_WORKERS", "2")
    assert md_prefetch.worker_count() == 2
