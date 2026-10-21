"""軽い作り直し（`refresh_rag`・`refresh_human_md`）のあと、変換結果キャッシュ（`_conv_cache`）の枠が二重に残らず、古い派生物も戻らないことを固定する。

実害: 枠が古い inode をハードリンクで持ったまま残り、鍵も当たらないので、次の全件の取り込みまでディスクを二重に食う。
"""
from __future__ import annotations

from pathlib import Path

import openpyxl
from PIL import Image

from sherpa import worlds
from sherpa.ingest import evidence_render, human_md, office_md


def _setup(tmp_path, monkeypatch, world_id: str, make) -> tuple[Path, Path]:
    source = tmp_path / "kb" / world_id
    source.mkdir(parents=True)
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    make(source)
    dmd = worlds.derived_md_dir(world_id)
    assert office_md.build_derived(source, dmd, world_sig="s1").get("error") is None
    return source, dmd


def _xlsx(names):
    def make(source):
        for name in names:
            wb = openpyxl.Workbook()
            wb.active["A1"], wb.active["B1"] = "No", f"NEEDLE_{name}"
            wb.save(source / name)
    return make


def _count_conversions(monkeypatch) -> list[str]:
    calls: list[str] = []
    orig = office_md._convert_with_arms
    monkeypatch.setattr(office_md, "_convert_with_arms",
                        lambda path, enabled: (calls.append(Path(path).name), orig(path, enabled))[1])
    return calls


def test_light_refresh_drops_the_slot_so_no_second_copy_stays(monkeypatch, tmp_path):
    source, dmd = _setup(tmp_path, monkeypatch, "drop-ok", _xlsx(["a.xlsx", "b.xlsx"]))
    root = office_md._conv_cache_root_for(dmd)
    assert all(office_md._conv_cache_slot(root, r)[1].is_dir() for r in ("a.xlsx", "b.xlsx"))
    monkeypatch.setattr(evidence_render, "RAG_RENDERER_VERSION", str(evidence_render.RAG_RENDERER_VERSION) + "-bump")

    res = office_md.refresh_rag(source, dmd, write_rag_sig_marker=True)
    assert res["rag_failed"] == 0 and res["rag_generated"] == 2

    for rel in ("a.xlsx", "b.xlsx"):
        meta, content = office_md._conv_cache_slot(root, rel)
        assert not meta.exists() and not content.exists()      # 古い inode を持つ枠が残らない

    calls = _count_conversions(monkeypatch)
    assert office_md.build_derived(source, dmd, world_sig="s1").get("error") is None
    assert sorted(calls) == ["a.xlsx", "b.xlsx"]               # 次の取り込みで変換し直す（変更前と同じ）


def test_failed_rel_keeps_its_slot(monkeypatch, tmp_path):
    source, dmd = _setup(tmp_path, monkeypatch, "drop-fail", _xlsx(["a.xlsx", "b.xlsx"]))
    monkeypatch.setattr(evidence_render, "RAG_RENDERER_VERSION", str(evidence_render.RAG_RENDERER_VERSION) + "-bump")
    orig = evidence_render.write_chunks_atomic

    def fail_for_a(path, chunks):
        if "a.xlsx" in str(path):
            raise OSError("disk")
        return orig(path, chunks)

    monkeypatch.setattr(evidence_render, "write_chunks_atomic", fail_for_a)
    assert office_md.refresh_rag(source, dmd, write_rag_sig_marker=True)["rag_failed"] == 1
    root = office_md._conv_cache_root_for(dmd)
    assert office_md._conv_cache_slot(root, "a.xlsx")[0].exists()
    assert not office_md._conv_cache_slot(root, "b.xlsx")[0].exists()


def test_stale_raster_md_is_not_restored_after_light_refresh(monkeypatch, tmp_path):
    def make(source):
        Image.new("RGB", (64, 64), "white").save(source / "a.png")
    orig = evidence_render.render
    tag = {"v": "OLDRENDER"}

    def render(*a, **k):
        r = orig(*a, **k)
        object.__setattr__(r, "markdown", r.markdown + "\n" + tag["v"])
        return r

    monkeypatch.setattr(evidence_render, "render", render)
    source, dmd = _setup(tmp_path, monkeypatch, "drop-raster", make)
    assert "OLDRENDER" in (dmd / "a.png.md").read_text()
    tag["v"] = "NEWRENDER"
    monkeypatch.setattr(evidence_render, "RAG_RENDERER_VERSION", str(evidence_render.RAG_RENDERER_VERSION) + "-bump")
    assert office_md.refresh_rag(source, dmd, write_rag_sig_marker=True)["rag_failed"] == 0
    assert office_md.build_derived(source, dmd, world_sig="s2").get("error") is None
    assert "OLDRENDER" not in (dmd / "a.png.md").read_text()   # 古い .md が戻らない


def test_human_md_and_rag_bump_together_does_not_repeat_conversion(monkeypatch, tmp_path):
    source, dmd = _setup(tmp_path, monkeypatch, "drop-loop", _xlsx(["a.xlsx"]))
    monkeypatch.setattr(human_md, "HUMAN_MD_RENDERER_VERSION", human_md.HUMAN_MD_RENDERER_VERSION + "-bump")
    monkeypatch.setattr(evidence_render, "RAG_RENDERER_VERSION", str(evidence_render.RAG_RENDERER_VERSION) + "-bump")
    assert office_md.refresh_human_md(source, dmd)["human_md_failed"] == 0
    assert office_md.refresh_rag(source, dmd, write_rag_sig_marker=True)["rag_failed"] == 0
    assert office_md.build_derived(source, dmd, world_sig="s2").get("error") is None
    assert not office_md.human_md_sig_drift(source, dmd)
    calls = _count_conversions(monkeypatch)
    assert office_md.build_derived(source, dmd, world_sig="s2").get("error") is None
    assert calls == []                                          # 2 回目は枠が当たり、作り直しが繰り返されない


def test_refresh_without_version_change_keeps_matching_slots(monkeypatch, tmp_path):
    from sherpa.ingest import md_prefetch
    source, dmd = _setup(tmp_path, monkeypatch, "drop-same", _xlsx(["a.xlsx", "b.xlsx", "c.xlsx"]))
    assert office_md.drop_rag_sig_marker(dmd)                  # 版は変わらないが rag の印が無い（LLM 成形・欠落からの再構築と同じ）
    assert office_md.refresh_rag(source, dmd, write_rag_sig_marker=True)["rag_failed"] == 0
    _xlsx(["d.xlsx"])(source)
    monkeypatch.setattr(md_prefetch, "worker_count", lambda: 1)
    calls = _count_conversions(monkeypatch)
    assert office_md.build_derived(source, dmd, world_sig="s2").get("error") is None
    assert calls == ["d.xlsx"]                                  # 今の版の枠は当たり、増えた 1 件だけ変換する


def test_slot_with_missing_contents_is_not_a_hit(monkeypatch, tmp_path):
    import shutil
    from sherpa.ingest import md_prefetch
    source, dmd = _setup(tmp_path, monkeypatch, "drop-partial", _xlsx(["a.xlsx"]))
    meta, content = office_md._conv_cache_slot(office_md._conv_cache_root_for(dmd), "a.xlsx")
    shutil.rmtree(content / "rag")                              # 消す途中で止まって鍵だけ残った状態
    assert meta.exists()
    monkeypatch.setattr(md_prefetch, "worker_count", lambda: 1)
    calls = _count_conversions(monkeypatch)
    assert office_md.build_derived(source, dmd, world_sig="s2").get("error") is None
    assert calls == ["a.xlsx"]
    assert (office_md._sibling_layer_dir(dmd, "rag") / "a.xlsx.rag.md").exists()


def test_drop_removes_the_key_first(monkeypatch, tmp_path):
    source, dmd = _setup(tmp_path, monkeypatch, "drop-order", _xlsx(["a.xlsx"]))
    root = office_md._conv_cache_root_for(dmd)
    meta, content = office_md._conv_cache_slot(root, "a.xlsx")
    seen = {}
    orig = office_md.shutil.rmtree
    monkeypatch.setattr(office_md.shutil, "rmtree",
                        lambda p, *a, **k: (seen.setdefault("key_gone_first", not meta.exists()), orig(p, *a, **k))[1])
    office_md._conv_cache_drop(root, "a.xlsx")
    assert seen["key_gone_first"] is True
