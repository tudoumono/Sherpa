"""EUC-JP の原本が、判定・grep・グラフ・台帳の保存結果のすべてで読めることの回帰テスト（名前・データは架空）。"""
from __future__ import annotations

import os

import pytest

from sherpa import corpus_docs, grep_tool, search_cache, store, text_encoding as TE, tool_dispatch, worlds
from sherpa.ingest import world_graph

_JCL_EUC = ("//ZZJOB01  JOB (ACCT),'ZZJOB01 BATCH'\n"
            "//* 顧客マスタ更新処理を実行する\n"
            "//STEP1    EXEC PGM=ZZAPROG00\n").encode("euc_jp")
_JCL = ("//ZZJOB01  JOB (ACCT),'ZZJOB01 BATCH'\n"
        "//* 顧客マスタ更新処理を実行する\n"
        "//STEP1    EXEC PGM=ZZAPROG00\n")
_COBOL = ("       IDENTIFICATION DIVISION.\n"
          "       PROGRAM-ID. ZZAPROG00.\n"
          "       PROCEDURE DIVISION.\n"
          "           GOBACK.\n")


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


def test_euc_jp_text_is_detected_as_euc_jp_with_no_garbling():
    enc, ratio, majority = TE.detect_bytes_quality(_JCL_EUC * 5, complete=True)
    assert (enc, ratio, majority) == ("euc_jp", 0.0, False)
    assert "顧客マスタ更新処理" in TE.decode(_JCL_EUC, enc)


def test_euc_with_nec_special_chars_is_read_as_euc_jis_2004():
    """Python の euc_jp が拒否する NEC 特殊文字（13 区）を含む EUC も読める。"""
    raw = ("//* 札幌".encode("euc_jp") + b"\xad\xea" + "支店の件数を数える\n".encode("euc_jp")) * 5
    enc, ratio, majority = TE.detect_bytes_quality(raw, complete=True)
    assert (enc, ratio, majority) == ("euc_jis_2004", 0.0, False)
    assert TE.decode(raw, enc).count("札幌") == 5


def test_plain_cp932_stays_cp932():
    raw = "//* 顧客マスタ更新処理を実行する\n//STEP1 EXEC PGM=ZZAPROG00\n".encode("cp932") * 5
    enc, ratio, majority = TE.detect_bytes_quality(raw, complete=True)
    assert (enc, TE.quality_of(ratio, majority)) == ("cp932", "ok")


def test_utf8_bom_and_strict_utf8_still_win_over_euc():
    text = "//* 顧客マスタ更新処理を実行する\n"
    assert TE.detect_bytes(text.encode("utf-8"), complete=True) == "utf-8"
    assert TE.detect_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"), complete=True) == "utf-8-sig"


def test_euc_jcl_is_parsed_and_exec_pgm_is_resolved(tmp_path):
    (tmp_path / "gen1").mkdir()
    (tmp_path / "gen1" / "ZZJOB01.jcl").write_bytes(_JCL_EUC)
    (tmp_path / "gen1" / "ZZAPROG00.cbl").write_text(_COBOL)
    nodes, edges, flags = world_graph.build_world(tmp_path, "zz_euc")
    by_cid = {n["cid"]: (n["label"], n["name"]) for n in nodes}
    assert ("Batch", "ZZJOB01") in by_cid.values()
    assert any(e["type"] == "INVOKES" and by_cid[e["src"]] == ("Batch", "ZZJOB01")
               and by_cid[e["dst"]] == ("Module", "ZZAPROG00") for e in edges)
    assert not [f for f in flags if f.get("reason") == "encoding_undetermined"]


def test_grep_finds_japanese_term_in_euc_document(tmp_path):
    (tmp_path / "ZZMEMO.txt").write_bytes(("顧客マスタ更新処理の注意事項\n詳細は別紙を参照すること\n" * 3).encode("euc_jp"))
    hits = grep_tool.grep_search("顧客マスタ", world="v1", roots=[tmp_path])
    assert hits and {h["path"].split("/")[-1] for h in hits} == {"ZZMEMO.txt"}


def test_classify_file_is_not_flagged_undetermined_for_euc_text(tmp_path):
    p = tmp_path / "ZZMEMO.txt"
    p.write_bytes(("顧客マスタ更新処理の注意事項\n詳細は別紙を参照すること\n" * 3).encode("euc_jp"))
    result = corpus_docs.classify_file(p, "ZZMEMO.txt", ".txt")
    assert result.get("unreadable_reason") is None


def test_stale_undetermined_classification_is_redone_without_changing_the_file(tmp_path, monkeypatch):
    """古い判定で「判別できない」と保存された結果は、ファイルが変わっていなくても次の分類で判定し直す。"""
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    p = tmp_path / "ZZMEMO.txt"
    p.write_bytes(("顧客マスタ更新処理の注意事項\n詳細は別紙を参照すること\n" * 3).encode("euc_jp"))
    st = os.stat(p)
    old = {"kind": "document", "doctype": None, "unreadable_reason": "encoding_undetermined",
           "encoding": "cp932", "_analyzer": None, "_q": ["cp932", 1.0, True]}   # 判定の版を持たない旧形式
    cache = search_cache.ClassifyCache("zz_euc")
    cache.put("ZZMEMO.txt", st, old)
    cache.flush()
    fresh = corpus_docs.classify_file(p, "ZZMEMO.txt", ".txt", cache=search_cache.ClassifyCache("zz_euc"))
    assert fresh.get("unreadable_reason") is None


def test_cp932_with_user_defined_char_is_not_mistaken_for_euc():
    raw = (b"A," + b"\xf9\xfc" + b"\n") * 20
    enc, _ratio, _majority = TE.detect_bytes_quality(raw, complete=True)
    assert enc == "cp932"
    assert TE.decode(raw, enc).startswith("A,")


def test_euc_jp_sentences_with_hiragana_are_detected_as_euc_jp():
    """EUC のひらがなは CP932 では半角カナ 2 文字に読めて置換文字が出ないので、かなの数で EUC と見分ける。"""
    for raw in (("申請書を確認してください。\n" * 20).encode("euc_jp"), "あいうえお".encode("euc_jp")):
        enc, ratio, majority = TE.detect_bytes_quality(raw, complete=True)
        assert (enc, ratio, majority) == ("euc_jp", 0.0, False)


def test_cp932_with_gaiji_or_halfwidth_kana_or_japanese_stays_cp932():
    cases = [
        (b"A," + b"\xf9\xfc" + b"\n") * 20,
        "ｱｲｳｴｵ".encode("cp932") * 20,
        "申請書を確認してください。\n".encode("cp932") * 20,
    ]
    for raw in cases:
        assert TE.detect_bytes_quality(raw, complete=True)[0] == "cp932"


def test_classification_saved_by_an_old_detect_version_is_redone_even_when_not_undetermined(tmp_path, monkeypatch):
    """版 2 が CP932 と判定して保存した EUC の原本は、「判別できない」でなくても、版 3 で EUC として読み直す。"""
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    p = tmp_path / "ZZMEMO.txt"
    p.write_bytes("あいうえお".encode("euc_jp"))
    os.utime(p, ns=(10**18, 10**18))   # 書いた直後（未確定）の分類は保存されないので、古い時刻にする
    old = {"kind": "document", "doctype": None, "encoding": "cp932", "_analyzer": None,
           "_q": ["cp932", 0.0, False], "_v": TE.DETECT_VERSION - 1}
    cache = search_cache.ClassifyCache("zz_euc2")
    cache.put("ZZMEMO.txt", os.stat(p), old)
    cache.flush()
    assert corpus_docs._payload_to_verdict(old) is None
    redo = search_cache.ClassifyCache("zz_euc2")
    fresh = corpus_docs.classify_file(p, "ZZMEMO.txt", ".txt", cache=redo)
    redo.flush()
    assert fresh["kind"] == "document"
    cache2 = search_cache.ClassifyCache("zz_euc2")
    saved = cache2.get("ZZMEMO.txt", os.stat(p))
    assert saved is not None and saved["_v"] == TE.DETECT_VERSION
    assert saved["_q"] is None or saved["_q"][0] == "euc_jp"


def test_classification_saved_by_the_current_detect_version_is_reused():
    payload = {"kind": "document", "doctype": None, "encoding": "cp932", "_analyzer": None,
               "_q": ["cp932", 0.0, False], "_v": TE.DETECT_VERSION}
    assert corpus_docs._payload_to_verdict(payload) is not None


def test_euc_over_the_normalizer_limit_is_not_lost_by_a_leading_newline():
    """1 MiB で切る位置は末尾から数バイトだけ戻る。先頭の改行まで戻って本文を捨てない。"""
    raw = ("\n" + "売上集計" * 200000).encode("euc_jp")
    assert TE.detect_bytes_quality(raw, complete=True)[0] == "euc_jp"


@pytest.mark.usefixtures("upstream_only_registry")
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


@pytest.mark.usefixtures("upstream_only_registry")
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
