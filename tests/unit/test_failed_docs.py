"""失敗の一覧（world_failed_docs）・失敗の知らせの扱い・ロックファイルの除外の単体テスト。

Evidence の生成だけを失敗させた xlsx（失敗の知らせへ縮退する）で、一覧への記載・変換キャッシュ・軽量再生成の混在を確かめる。
"""
from __future__ import annotations

import pathlib
import uuid

import openpyxl
import pytest

from sherpa import json_io, store, worlds
from sherpa.ingest import office_md, worker


def _dirs(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    return src, tmp_path / "derived" / "md"


def _xlsx(path: pathlib.Path) -> None:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "値"
    wb.save(path)


@pytest.fixture
def degraded_xlsx(tmp_path, monkeypatch):
    """Evidence の抽出だけが失敗する xlsx を 1 回変換した状態（失敗の知らせへ縮退）を返す。"""
    src, der = _dirs(tmp_path)
    _xlsx(src / "a.xlsx")

    def boom(*a, **kw):
        raise RuntimeError("injected")
    monkeypatch.setattr(office_md, "_extract_canonical_evidence", boom)
    rep = office_md.build_derived(src, der)
    monkeypatch.undo()
    return src, der, rep


def test_degraded_doc_is_listed_not_cached_and_not_mixed_by_refresh(degraded_xlsx):
    src, der, rep = degraded_xlsx
    assert {"doc": "a.xlsx", "reason": "source_parse_failed"} in rep["conversion_failures"]
    meta = json_io.read_json(der / "a.xlsx.md.meta.json")
    assert meta["method"] == "source_failure_notice"
    assert not (office_md._conv_cache_root_for(der) / "a.xlsx.key.json").exists()
    # 失敗の知らせは版の判定から外れ、軽量再生成は .md を書き換えない
    assert office_md.human_md_sig_drift(src, der) is False
    before = (der / "a.xlsx.md").read_bytes()
    assert office_md.refresh_human_md(src, der)["human_md_generated"] == 0
    assert office_md.refresh_document_ir(src, der)["document_ir_generated"] == 0
    assert (der / "a.xlsx.md").read_bytes() == before
    assert not (der.parent / "ir" / "a.xlsx.document.json").exists()


def test_conv_cache_lookup_misses_when_cached_md_is_failure_notice(tmp_path):
    cache_root = tmp_path / "_conv_cache"
    (cache_root / "a.xlsx.d" / "md").mkdir(parents=True)
    json_io.write_json_atomic(cache_root / "a.xlsx.d" / "md" / "a.xlsx.md.meta.json",
                              {"arm": "evidence_notice", "method": "source_failure_notice",
                               "notes": ["coverage_status=failed", "reason_code=source_parse_failed"]})
    json_io.write_json_atomic(cache_root / "a.xlsx.key.json", {"key": "k1", "rep_delta": {}})
    assert office_md._conv_cache_lookup(cache_root, "a.xlsx", "k1") is None


def test_office_lock_file_is_not_converted(tmp_path):
    src, der = _dirs(tmp_path)
    _xlsx(src / "~$a.xlsx")
    rep = office_md.build_derived(src, der)
    assert rep["converted"] == 0 and rep["failed"] == 0 and not rep["conversion_failures"]
    assert not (der / "~$a.xlsx.md").exists()


def test_failed_docs_table_replace_keeps_first_failed_and_counts_tries():
    w = f"fd-{uuid.uuid4().hex[:8]}"
    try:
        store.replace_failed_docs(w, {"a.xlsx": "source_parse_failed", "b.docx": "size_exceeded"})
        first = {r["rel"]: r for r in store.list_failed_docs(w)}
        store.replace_failed_docs(w, {"a.xlsx": "source_parse_failed"})
        rows = {r["rel"]: r for r in store.list_failed_docs(w)}
        assert set(rows) == {"a.xlsx"}                                  # 成功した（外れた）文書は消える
        assert rows["a.xlsx"]["tries"] == 2 and rows["a.xlsx"]["first_failed"] == first["a.xlsx"]["first_failed"]
    finally:
        store.clear_failed_docs(w)
    assert store.list_failed_docs(w) == []


def test_seed_failed_docs_from_derived_runs_once(degraded_xlsx, monkeypatch):
    src, der, _rep = degraded_xlsx
    w = f"fd-{uuid.uuid4().hex[:8]}"
    droot = der.parent
    monkeypatch.setattr(worlds, "derived_dir", lambda world_id: droot)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda world_id: der)
    try:
        assert worker._seed_failed_docs_locked(w) is True
        assert [r["rel"] for r in store.list_failed_docs(w)] == ["a.xlsx"]
        assert worker._seed_failed_docs_locked(w) is False             # 印があるので二度目は走らない
    finally:
        store.clear_failed_docs(w)

