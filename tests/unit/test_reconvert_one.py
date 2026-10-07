"""1 ファイルの再変換（`office_md.derive_one`/`publish_one`・`worker._reconvert_locked`）の契約テスト。

Evidence の生成だけを失敗させて失敗の知らせへ縮退させた xlsx を、1 ファイルだけ変換し直す。
"""
from __future__ import annotations

import pathlib
import uuid

import openpyxl
import pytest

from sherpa import json_io, store, worlds
from sherpa.ingest import office_md, worker


def _xlsx(path: pathlib.Path, value: str) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "項目"
    wb.active["B1"] = value
    wb.save(path)


def _boom(*a, **kw):
    raise RuntimeError("injected")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """a.xlsx が失敗の知らせ・b.xlsx が成功の派生を公開した資料フォルダ（署名 sig1）。"""
    src = tmp_path / "src"
    src.mkdir()
    _xlsx(src / "a.xlsx", "甲の値")
    _xlsx(src / "b.xlsx", "乙の値")
    der = tmp_path / "derived" / "md"
    real = office_md._extract_canonical_evidence

    def fail_a(source_path, **kw):
        if pathlib.Path(source_path).name == "a.xlsx":
            raise RuntimeError("injected")
        return real(source_path, **kw)
    with monkeypatch.context() as m:
        m.setattr(office_md, "_extract_canonical_evidence", fail_a)
        rep = office_md.build_derived(src, der, world_sig="sig1")
    assert {"doc": "a.xlsx", "reason": "source_parse_failed"} in rep["conversion_failures"]
    w = f"rc-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(worlds, "world_dir", lambda world_id: src)
    monkeypatch.setattr(worlds, "derived_dir", lambda world_id: der.parent)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world_id: der)
    monkeypatch.setattr(worlds, "derived_rag_dir", lambda world_id: der.parent / "rag")
    manifest = {rel: [m, c, z] for rel, m, c, z in worker._scan_dir(src)}
    monkeypatch.setattr(store, "get_world", lambda world_id: {"world_id": world_id, "last_sig": "sig1",
                                                             "last_manifest": manifest})
    yield w, src, der
    store.clear_failed_docs(w)


def _tree(root: pathlib.Path, rel: str) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.relative_to(root).as_posix().startswith(rel)}


def test_derive_one_publishes_same_output_as_full_build_and_leaves_other_docs(world, tmp_path):
    """1 ファイルの再変換は全体の作り直しと同じ出力をそのファイルの分だけ公開し、ほかの文書の派生には触れない。"""
    _w, src, der = world
    layers = [der, der.parent / "rag", der.parent / "ir"]
    other_before = [_tree(d, "b.xlsx") for d in layers]
    other_cache = (office_md._conv_cache_root_for(der) / "b.xlsx.key.json").read_bytes()
    assert office_md.drop_conversion_cache(der, "a.xlsx")
    rep = office_md.derive_one(src, der, (src / "a.xlsx").resolve(), "a.xlsx")
    assert not rep.get("error") and not rep["conversion_failures"] and rep["converted"] == 1
    office_md.publish_one(der, "a.xlsx")
    assert office_md.failure_notice_reason(json_io.read_json(der / "a.xlsx.md.meta.json")) is None
    assert "甲の値" in (der.parent / "rag" / "a.xlsx.rag.md").read_text(encoding="utf-8")
    assert [_tree(d, "b.xlsx") for d in layers] == other_before
    assert (office_md._conv_cache_root_for(der) / "b.xlsx.key.json").read_bytes() == other_cache
    assert (office_md._conv_cache_root_for(der) / "a.xlsx.key.json").exists()
    assert not any(d.with_name(d.name + ".staging").exists() for d in layers)
    # 全体の作り直し（キャッシュを使わない）と同じ中身
    full = tmp_path / "full" / "md"
    office_md.build_derived(src, full, world_sig="sig1")
    for layer, name in (("md", "a.xlsx.md"), ("rag", "a.xlsx.rag.md"), ("rag", "a.xlsx.rag_chunks.jsonl"),
                        ("ir", "a.xlsx.document.json"), ("ir", "a.xlsx.derived.json")):
        assert (der.parent / layer / name).read_bytes() == (full.parent / layer / name).read_bytes(), name


def test_reconvert_conversion_failure_keeps_published_and_updates_row(world, monkeypatch):
    """変換がまた失敗したら公開中の派生はそのままで、失敗の一覧の行（理由・tries）だけを更新する。"""
    w, _src, der = world
    store.replace_failed_docs(w, {"a.xlsx": "source_parse_failed"})
    before = [_tree(d, "a.xlsx") for d in (der, der.parent / "rag", der.parent / "ir")]
    monkeypatch.setattr(office_md, "_extract_canonical_evidence", _boom)
    monkeypatch.setattr(worker, "_reflect_graph_after_rag_rewrite", lambda world: pytest.fail("グラフに触れない"))
    res = worker.reconvert(w, "a.xlsx", run_id=store.start_ingest_run(w)["id"])
    assert res["status"] == "failed"
    assert [_tree(d, "a.xlsx") for d in (der, der.parent / "rag", der.parent / "ir")] == before
    rows = {r["rel"]: r for r in store.list_failed_docs(w)}
    assert rows["a.xlsx"]["reason"] == "source_parse_failed" and rows["a.xlsx"]["tries"] == 2


def test_reconvert_graph_failure_keeps_row_for_retry(world, monkeypatch):
    """変換は成功してグラフの反映で失敗したら、失敗の一覧に「反映待ち」で残り、もう一度の再変換でやり直せる。"""
    w, _src, der = world
    store.replace_failed_docs(w, {"a.xlsx": "source_parse_failed"})
    monkeypatch.setattr(worker, "_reflect_graph_after_rag_rewrite", _boom)
    res = worker.reconvert(w, "a.xlsx", run_id=store.start_ingest_run(w)["id"])
    assert res["status"] == "failed"
    assert res["flags"][-1]["reason"] == "reconvert_graph_failed:RuntimeError"
    assert {r["rel"]: r["reason"] for r in store.list_failed_docs(w)} == {"a.xlsx": "reconvert_reflect_failed"}
    assert office_md.failure_notice_reason(json_io.read_json(der / "a.xlsx.md.meta.json")) is None
