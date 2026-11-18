"""EUC-JP の原本が grep・精読で読めること、既存の UTF-8・CP932 の結果が変わらないことの回帰テスト（名前・データは架空）。"""
from __future__ import annotations

import os

import pytest

from sherpa import corpus_docs, grep_tool, search_cache, store, tool_dispatch, worlds
from sherpa import text_encoding as TE

pytestmark = pytest.mark.usefixtures("upstream_only_registry")

_JCL = ("//ZZJOB01  JOB (ACCT),'ZZJOB01 BATCH'\n"
        "//* 顧客マスタ更新処理を実行する\n"
        "//STEP1    EXEC PGM=ZZAPROG00\n")


def _world(monkeypatch, tmp_path):
    wd = tmp_path / "world"
    wd.mkdir()
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "derived_rag_dir", lambda w: der)
    monkeypatch.setattr(worlds, "observation_current_dir", lambda w: None)
    monkeypatch.setattr(store, "get_world_status_row", lambda world: None)
    return wd


def test_euc_jp_jcl_is_found_by_grep_and_read_by_the_reading_tools(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    (wd / "ZZJOB01.jcl").write_bytes(_JCL.encode("euc_jp"))

    hits = tool_dispatch.run_tool("ripgrep_search", {"query": "顧客マスタ"}, "w", None)[0]["hits"]
    assert [h["doc_id"] for h in hits] == ["ZZJOB01.jcl"]
    assert "encoding_caution" not in hits[0]

    doc = tool_dispatch.run_tool("read_doc", {"doc_id": "ZZJOB01.jcl", "start_line": 1}, "w", None)[0]
    assert "顧客マスタ更新処理" in str(doc) and "encoding_caution" not in doc
    around = tool_dispatch.run_tool("read_around", {"doc_id": "ZZJOB01.jcl", "line": 2, "window": 1}, "w", None)[0]
    assert "顧客マスタ更新処理" in str(around)
    head = tool_dispatch.run_tool("file_head", {"doc_id": "ZZJOB01.jcl"}, "w", None)[0]
    assert "顧客マスタ更新処理" in str(head)


def test_ledger_entry_saved_as_undetermined_does_not_block_reading(monkeypatch, tmp_path):
    """判定を直す前に「判別できない」と保存された分類の結果は、ファイルが変わっていなくても使わず判定し直す。"""
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    p = tmp_path / "ZZJOB01.jcl"
    p.write_bytes(_JCL.encode("euc_jp"))
    os.utime(p, ns=(10**18, 10**18))   # 書いた直後（未確定）の分類は保存されないので、古い時刻にする
    old = {"kind": "document", "doctype": None, "unreadable_reason": "encoding_undetermined",
           "encoding": "cp932", "_analyzer": None, "_q": ["cp932", 1.0, True]}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(TE, "DETECT_VERSION", TE.DETECT_VERSION - 1)
        stale = search_cache.ClassifyCache("zz_euc")
        stale.put("ZZJOB01.jcl", os.stat(p), old)
        stale.flush()
    assert search_cache.ClassifyCache("zz_euc").get("ZZJOB01.jcl", os.stat(p)) is None
    fresh = corpus_docs.classify_file(p, "ZZJOB01.jcl", ".jcl", cache=search_cache.ClassifyCache("zz_euc"))
    assert fresh.get("unreadable_reason") is None


def test_utf8_and_cp932_sources_give_the_same_grep_results(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    (wd / "ZZA.txt").write_bytes("顧客マスタ更新処理\n".encode())
    (wd / "ZZB.txt").write_bytes(("申請書を確認してください。顧客マスタ更新処理\n" * 3).encode("cp932"))
    (wd / "ZZC.txt").write_bytes("顧客マスタ更新処理\n".encode("utf-8-sig"))

    hits = grep_tool.grep_search("顧客マスタ", world="w")
    assert sorted(h["doc_id"] for h in hits) == ["ZZA.txt", "ZZB.txt", "ZZC.txt"]
    assert all("顧客マスタ更新処理" in h["text"] for h in hits)
    assert [TE.detect_bytes(p.read_bytes(), complete=True) for p in sorted(wd.iterdir())] == ["utf-8", "cp932", "utf-8-sig"]


def test_ambiguous_bytes_fall_back_to_cp932_without_charset_normalizer(monkeypatch):
    """charset-normalizer が使えなくても判定は落ちず、曖昧なものは従来どおり CP932 になる。"""
    raw = "あいうえお".encode("euc_jp")
    monkeypatch.setattr(TE, "_normalizer_from_bytes", None)
    assert TE.detect_bytes(raw, complete=True) == "cp932"
    assert TE.detect_bytes(_JCL.encode("euc_jp") * 5, complete=True) == "euc_jp"   # CP932 では化けるので曖昧ではない
