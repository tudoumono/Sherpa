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
    assert not any(k.endswith(".document.json") for k in files1) and any(k.endswith(".evidence.json") for k in files1)
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



def test_heavy_files_are_not_prefetched(tmp_path, monkeypatch):
    """目安を超える重いファイルは子へ渡さず、親が直列で処理する（子どうしでメモリを食い合わない）。"""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(FIXTURES / "eval/office_ja/inputs/OJA-DOCX-HARD.docx", src / "a.docx")
    shutil.copy(FIXTURES / "eval/excel_ja/inputs/JPX-001.xlsx", src / "b.xlsx")
    assert [j.rel for j in _jobs_for(src, tmp_path)] == ["a.docx", "b.xlsx"]
    monkeypatch.setattr(office_md, "_PREFETCH_MAX_XLSX_CELLS", 1)
    assert [j.rel for j in _jobs_for(src, tmp_path)] == ["a.docx"]
    monkeypatch.setattr(office_md, "_PREFETCH_MAX_FILE_BYTES", 1)
    assert _jobs_for(src, tmp_path) == []

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
        pf._inflight["a.docx"][1].result()
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



def test_broken_pool_stops_children_before_removing_assets(tmp_path):
    """子が強制終了してプールが壊れたら、全ての子が止まってから未取得分の素材を消す（以降は親が直列で計算する）。"""
    import os
    import signal

    jobs = []
    for name in ("a.docx", "b.docx", "c.docx"):
        shutil.copy(FIXTURES / "eval/office_ja/inputs/OJA-DOCX-HARD.docx", tmp_path / name)
        jobs.append(md_prefetch.Job(name, str(tmp_path / name), "ooxml", ".docx", render=True,
                                    assets_dir=str(tmp_path / "rag" / (name + ".assets"))))
    with md_prefetch.Prefetcher(jobs, 2) as pf:
        procs = list(pf._pool._processes.values())
        os.kill(procs[0].pid, signal.SIGKILL)
        assert pf.take("a.docx", tmp_path / "a.docx") is None
        assert pf._pool is None and not any(p.is_alive() for p in procs)
        assert not (tmp_path / "rag").exists() or list((tmp_path / "rag").iterdir()) == []
        assert pf.take("b.docx", tmp_path / "b.docx") is None
        assert pf.take("c.docx", tmp_path / "c.docx") is None

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
        pf._inflight["a.pdf"][1].result()             # 子が読み終えるまで待つ
        shutil.copy(FIXTURES / "eval/office_ja/inputs/OJA-PDF-MEDIUM.pdf", src)
        assert pf.take("a.pdf", src) is None


def test_largest_go_first_without_clogging_the_window(tmp_path):
    """大きい上位 ワーカー数 件を最初に出し、残りは名前順。先に出した分が窓を塞がず、名前順の分は常に ワーカー数×2 件まで出ている。"""
    base = (FIXTURES / "eval/office_ja/inputs/OJA-PDF-EASY.pdf").read_bytes()
    names = [f"{c}.pdf" for c in "abcdefghij"]
    pads = {n: 10 + i for i, n in enumerate(names)}
    pads["i.pdf"], pads["j.pdf"] = 5000, 6000                    # 名前順で最後の 2 件が最大
    jobs = []
    for n in names:
        (tmp_path / n).write_bytes(base + b"\n%" + b"x" * pads[n] + b"\n")
        jobs.append(md_prefetch.Job(n, str(tmp_path / n), "pdf_text", ".pdf"))
    workers, window = 2, 4
    with md_prefetch.Prefetcher(jobs, workers) as pf:
        assert list(pf._inflight)[:2] == ["j.pdf", "i.pdf"]       # 最初は大きい順
        assert list(pf._inflight)[2:] == ["a.pdf", "b.pdf", "c.pdf", "d.pdf"]   # 残りは名前順で窓いっぱい
        for k, n in enumerate(names):
            got = pf.take(n, tmp_path / n)
            assert got is not None and got.source_stat[0] == (tmp_path / n).stat().st_size
            remaining_normal = len(names) - 2 - (k + 1)
            if remaining_normal >= 0:
                assert pf._windowed_inflight() == min(window, remaining_normal)
            assert len(pf._inflight) <= window + workers
    assert pf.used == len(names) and pf.max_rss_kb > 0


@pytest.mark.parametrize("raw", ["0", "9", "x", ""])
def test_worker_count_invalid_falls_back_to_default(monkeypatch, raw):
    monkeypatch.delenv("SHERPA_MD_WORKERS", raising=False)
    default = md_prefetch.worker_count()
    monkeypatch.setenv("SHERPA_MD_WORKERS", raw)
    assert md_prefetch.worker_count() == default
    assert 1 <= default <= 4
    monkeypatch.setenv("SHERPA_MD_WORKERS", "2")
    assert md_prefetch.worker_count() == 2


def _plan_corpus(tmp_path: Path) -> Path:
    src = tmp_path / "plansrc"
    src.mkdir()
    for i, name in enumerate(CORPUS):
        for k in range(3):                                     # スレッドの取り合いが起きる件数にする
            shutil.copy(FIXTURES / name, src / f"{i}_{k}_{Path(name).name}")
    (src / "broken.docx").write_bytes(b"not a zip")
    return src


def _plan(src: Path, tmp_path: Path, threads: int, cache_hit=lambda rp, rel: None):
    from sherpa.ingest import arms

    memo: dict = {}
    jobs = office_md._prefetch_jobs(
        [(p, p.name) for p in sorted(src.iterdir())],
        candidate=office_md.OFFICE_EXT | office_md.convertible_exts(), conv=office_md.convertible_exts(),
        enabled=arms.enabled_arms(), cache_hit=cache_hit, obs_dir=None, assets_root=tmp_path / "rag",
        threads=threads, precheck_memo=memo)
    return jobs, memo


def test_plan_threads_do_not_change_the_plan(tmp_path):
    """計画（先読みの対象・順序・入口ガードの判定）はスレッド数に依らず同じ。"""
    src = _plan_corpus(tmp_path)
    hit = {p.name for p in src.iterdir() if p.name.startswith("3_1_")}

    jobs1, memo1 = _plan(src, tmp_path, 1, cache_hit=lambda rp, rel: object() if rel in hit else None)
    jobs8, memo8 = _plan(src, tmp_path, 8, cache_hit=lambda rp, rel: object() if rel in hit else None)

    assert len(jobs1) > 10 and jobs8 == jobs1
    assert memo8 == memo1
    assert hit and not (hit & {j.rel for j in jobs1}) and not (hit & set(memo1))   # キャッシュに当たるものは確認しない
    assert memo1["broken.docx"][1][0] == "ok"                  # 壊れた zip は測れない＝入口ガードは通し、変換側が失敗にする


def test_cache_hit_skips_zip_and_xlsx_inspection(tmp_path, monkeypatch):
    """変換キャッシュに当たるファイルは、zip を開く確認（非圧縮サイズ・セル数）をしない。"""
    src = _plan_corpus(tmp_path)
    opened = []
    real = office_md._office_uncompressed_total_bytes
    monkeypatch.setattr(office_md, "_office_uncompressed_total_bytes", lambda rp: opened.append(rp) or real(rp))

    jobs, memo = _plan(src, tmp_path, 4, cache_hit=lambda rp, rel: object())

    assert jobs == [] and memo == {} and opened == []


def test_plan_threads_do_not_change_derived_bytes(tmp_path, monkeypatch, caplog):
    """計画のスレッド数（1 と 8）を変えても、変換結果（派生ファイルと rep）は 1 バイトも変わらない。"""
    src = tmp_path / "src"
    src.mkdir()
    for name in CORPUS:
        shutil.copy(FIXTURES / name, src / Path(name).name)
    (src / "broken.docx").write_bytes(b"not a zip")

    monkeypatch.setenv("SHERPA_MD_PLAN_THREADS", "1")
    rep1, files1 = _build(src, tmp_path / "t1", 3, monkeypatch, caplog)
    monkeypatch.setenv("SHERPA_MD_PLAN_THREADS", "8")
    rep8, files8 = _build(src, tmp_path / "t8", 3, monkeypatch, caplog)

    assert rep1["converted"] >= len(CORPUS) - 1 and rep1["failed"] >= 1
    assert sorted(files8) == sorted(files1)
    assert [k for k in files1 if files1[k] != files8[k]] == []
    assert rep8 == rep1


def test_stale_plan_is_not_used_when_source_changed(tmp_path, monkeypatch):
    """計画の後に原本が変わったら、本体ループは計画の判定を使わず測り直す（stat が同じときだけ使う）。"""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(FIXTURES / "eval/office_ja/inputs/OJA-DOCX-HARD.docx", src / "a.docx")
    shutil.copy(FIXTURES / "eval/office_ja/inputs/OJA-PPTX-HARD.pptx", src / "b.pptx")
    monkeypatch.setenv("SHERPA_MD_WORKERS", "2")
    real_jobs = office_md._prefetch_jobs

    def _plan_then_grow(targets, **kw):
        jobs = real_jobs(targets, **kw)
        for rp, rel in targets:
            if rel == "b.pptx":
                rp.write_bytes(rp.read_bytes() + b"\0")        # 計画の後・本体ループの前に原本が変わる
        return jobs

    monkeypatch.setattr(office_md, "_prefetch_jobs", _plan_then_grow)
    calls = []
    real_pre = office_md._precheck_target
    monkeypatch.setattr(office_md, "_precheck_target",
                        lambda rp, ext, conv, *a: calls.append(Path(rp).name) or real_pre(rp, ext, conv, *a))

    rep = office_md.build_derived(src, tmp_path / "out" / "md")

    assert rep["converted"] == 2
    assert calls.count("a.docx") == 1          # 変わっていない原本は、計画の判定を本体ループが使い回す
    assert calls.count("b.pptx") == 2          # 変わった原本は、本体ループが測り直す


def test_plan_threads_env_parse(monkeypatch):
    monkeypatch.delenv("SHERPA_MD_PLAN_THREADS", raising=False)
    assert md_prefetch.plan_threads() == 8
    for raw, want in (("1", 1), ("32", 32), ("0", 8), ("33", 8), ("x", 8), ("", 8)):
        monkeypatch.setenv("SHERPA_MD_PLAN_THREADS", raw)
        assert md_prefetch.plan_threads() == want
