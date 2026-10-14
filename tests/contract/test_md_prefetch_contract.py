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
    "eval/office_ja/inputs/OJA-DOCX-HARD.docx",      # 画像（素材の書出し）を含む
    "eval/office_ja/inputs/OJA-PPTX-HARD.pptx",
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
    if workers > 1:   # 先読みの結果が実際に使われた（全件が直列へ戻って一致しているだけではない）
        used = [r.args for r in caplog.records if r.getMessage().startswith("MD化の先読みを使いました")]
        assert used and used[0][0] == used[0][1] > 0
        assert used[0][2] > 0                  # 描画まで子で済んだ件数（全件が親へ戻って一致しているだけではない）
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


def _jobs_for(src: Path, tmp_path: Path):
    from sherpa.ingest import arms

    targets = [(p, p.name) for p in sorted(src.iterdir())]
    return office_md._prefetch_jobs(
        targets, candidate=office_md.OFFICE_EXT | office_md.convertible_exts(), conv=office_md.convertible_exts(),
        enabled=arms.enabled_arms(), cache_hit=lambda rp, rel: None, obs_dir=None, assets_root=tmp_path / "rag")


def test_child_does_not_render_when_vlm_may_observe(tmp_path, monkeypatch):
    """VLM が使える構成では子で描画しない（VLM の補足観測は親でしか行わない）。使えないなら描画まで子へ任せる。"""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(FIXTURES / "eval/office_ja/inputs/OJA-DOCX-HARD.docx", src / "a.docx")
    shutil.copy(FIXTURES / "eval/office_ja/inputs/OJA-PPTX-HARD.pptx", src / "b.pptx")
    monkeypatch.setenv("SHERPA_MCP_ARMS", "ooxml,pdf_text,vision")
    monkeypatch.setenv("SHERPA_VLM_USABLE", "1")
    assert office_md._vlm_observation_possible()
    assert [j.render for j in _jobs_for(src, tmp_path)] == [False, False]
    monkeypatch.setenv("SHERPA_VLM_USABLE", "0")
    assert not office_md._vlm_observation_possible()
    jobs = _jobs_for(src, tmp_path)
    assert [j.render for j in jobs] == [True, True]
    assert jobs[0].assets_dir == str(tmp_path / "rag" / "a.docx.assets")


def test_render_failure_in_child_falls_back_without_leftovers(tmp_path):
    """子が素材を書けず描画に失敗したら、Evidence までで返し（親が描画し直す）、途中の素材を残さない。"""
    src = tmp_path / "a.docx"
    shutil.copy(FIXTURES / "eval/office_ja/inputs/OJA-DOCX-HARD.docx", src)
    blocker = tmp_path / "blocker"
    blocker.write_text("file")                       # 素材の置き場の親がファイル＝書出しが OSError になる
    job = md_prefetch.Job("a.docx", str(src), "ooxml", ".docx", render=True,
                          assets_dir=str(blocker / "a.docx.assets"))
    with md_prefetch.Prefetcher([job], 1) as pf:
        got = pf.take("a.docx", src)
    assert got is not None and got.rendered is None and got.evidence is not None


def test_unused_prefetched_assets_are_removed(tmp_path):
    """結果を使わない（原本が変わった）rel の素材は、親が計算し直す前に消える。使われなかった描画結果も終了時に消える。"""
    src = tmp_path / "a.docx"
    shutil.copy(FIXTURES / "eval/office_ja/inputs/OJA-DOCX-HARD.docx", src)
    assets = tmp_path / "rag" / "a.docx.assets"
    job = md_prefetch.Job("a.docx", str(src), "ooxml", ".docx", render=True, assets_dir=str(assets))
    with md_prefetch.Prefetcher([job], 1) as pf:
        pf._inflight[0][1].result()
        assert assets.is_dir()
        src.write_bytes(src.read_bytes() + b"\0")     # 子が読んだ後に原本が変わった
        assert pf.take("a.docx", src) is None
        assert not assets.exists()
    src2 = tmp_path / "b.docx"
    shutil.copy(FIXTURES / "eval/office_ja/inputs/OJA-DOCX-HARD.docx", src2)
    assets2 = tmp_path / "rag" / "b.docx.assets"
    job2 = md_prefetch.Job("b.docx", str(src2), "ooxml", ".docx", render=True, assets_dir=str(assets2))
    with md_prefetch.Prefetcher([job2], 1) as pf:
        got = pf.take("b.docx", src2)
        assert got is not None and got.rendered is not None and assets2.is_dir()
    assert not assets2.exists()                      # 親が `claim` しなかった描画結果の素材は残さない


def test_failed_job_returns_none_and_next_job_still_runs():
    """子が例外を投げたファイルは None（親が直列で計算し直す）。後続は影響を受けない。"""
    good = FIXTURES / "eval/office_ja/inputs/OJA-PDF-EASY.pdf"
    jobs = [
        md_prefetch.Job("bad.pdf", str(good), "no_such_arm", ".pdf"),
        md_prefetch.Job("good.pdf", str(good), "pdf_text", ".pdf"),
    ]
    with md_prefetch.Prefetcher(jobs, 2) as pf:
        assert pf.take("bad.pdf", good) is None
        got = pf.take("good.pdf", good)
        assert got is not None and got.result.md and got.evidence is not None
        assert pf.take("good.pdf", good) is None      # 取り出し済み


def test_source_changed_after_child_read_is_not_used(tmp_path):
    """子が読んだ後に原本が変わったら先読み結果を使わない（古い中身を新しいキャッシュキーで残さない）。"""
    src = tmp_path / "a.pdf"
    shutil.copy(FIXTURES / "eval/office_ja/inputs/OJA-PDF-EASY.pdf", src)
    with md_prefetch.Prefetcher([md_prefetch.Job("a.pdf", str(src), "pdf_text", ".pdf")], 1) as pf:
        pf._inflight[0][1].result()                   # 子が読み終えるまで待つ
        shutil.copy(FIXTURES / "eval/office_ja/inputs/OJA-PDF-MEDIUM.pdf", src)
        assert pf.take("a.pdf", src) is None


@pytest.mark.parametrize("raw", ["0", "9", "x", ""])
def test_worker_count_invalid_falls_back_to_default(monkeypatch, raw):
    monkeypatch.delenv("SHERPA_MD_WORKERS", raising=False)
    default = md_prefetch.worker_count()
    monkeypatch.setenv("SHERPA_MD_WORKERS", raw)
    assert md_prefetch.worker_count() == default
    assert 1 <= default <= 4
    monkeypatch.setenv("SHERPA_MD_WORKERS", "2")
    assert md_prefetch.worker_count() == 2
