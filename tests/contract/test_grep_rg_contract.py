"""grep_search の rg 化（候補の発見を rg に替え、整形は Python のまま）と派生側ディスクキャッシュの契約テスト。

固定するのは「結果の形は変えない」こと:
- 純 Python 走査（`engine="python"`＝参照実装）と rg 経由の走査が、合成コーパスで同じヒット（文書集合・節単位の整形・順序）を返す。
- `rg -i` と Python `lower()` の非 ASCII の畳み方の差は、Python に揃える（rg は候補を絞るだけで、確定は Python）。
- キャッシュ（ファイル分類・read_doc の総行数・ページ送りの候補一覧）はファイルの状態が変われば古い値を返さない。
- rg が無い・失敗するときは純 Python 走査に縮退し、結果は同じ。この検査は rg の有無にかかわらず走る（スキップしない）。
"""
from __future__ import annotations

import logging
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

from sherpa import corpus_docs, grep_tool as G, rg_search, search_cache, store, tool_dispatch as TD
from sherpa import worlds as W
from sherpa.parts.read import tools as RT

pytestmark = pytest.mark.usefixtures("upstream_only_registry")

_REPO = pathlib.Path(__file__).resolve().parents[2]

# 合成コーパス S の検索語 12 個: ASCII の識別子（大小文字違い含む）・日本語・該当なし・MD の節に当たるもの。
_TERMS = [
    ("CP0041-ITEM-02", True),    # コピーブックの項目（識別子）
    ("cp0041-item-02", True),    # 大文字小文字違い
    ("CP0041", True),           # コピーブック名
    ("PG00014", True),          # プログラム名
    ("EXEC PGM", True),         # JCL
    ("WS-COUNT", True),         # 多数のファイルに当たる語
    ("消費税率", True),          # 日本語
    ("夜間バッチ", True),        # 日本語（MD の節）
    ("手数料の計算", True),      # MD の節
    ("代理店区分", True),        # MD の節
    ("コピーブックの定義", True),  # MD の節
    ("ZZZ-NOSUCH-TERM", False),  # 該当なし
]


@pytest.fixture(scope="module")
def corpus_s(tmp_path_factory):
    """合成コーパス S（架空名のみ・seed 固定）。"""
    out = tmp_path_factory.mktemp("corpus_s")
    subprocess.run([sys.executable, str(_REPO / "scripts" / "bench" / "gen_corpus.py"), "--out", str(out),
                    "--programs", "300", "--copybooks", "60", "--jobs", "60", "--docs", "200", "--seed", "7"],
                   check=True, capture_output=True)
    return out


@pytest.fixture
def count_discover(monkeypatch):
    """`rg_search.discover` の呼び出し回数（rg で走査した回数）を数える。"""
    calls = {"n": 0}
    real = rg_search.discover

    def _spy(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(rg_search, "discover", _spy)
    return calls


def _isolated_world(monkeypatch, tmp_path, root, world="rgworld"):
    """`grep_search(roots=None)` が読む world_dir と派生の置き場を tmp へ隔離する（DB 不要）。"""
    der = tmp_path / "derived"
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(der))
    monkeypatch.setattr(W, "world_dir", lambda w: root)
    monkeypatch.setattr(store, "get_world_status_row", lambda w: None)
    (der / world).mkdir(parents=True, exist_ok=True)
    return world


# ===== (1) 参照実装（純 Python）との同値 =====

@pytest.mark.parametrize("term, expect_hits", _TERMS)
def test_rg_and_python_engines_return_identical_hits(corpus_s, count_discover, term, expect_hits):
    ref = G.grep_search(term, roots=[corpus_s], max_hits=1000, engine="python")
    got = G.grep_search(term, roots=[corpus_s], max_hits=1000)
    assert got == ref
    assert bool(ref) is expect_hits
    if rg_search.find_rg() is not None:
        assert count_discover["n"] >= 1  # rg の経路を実際に通っている


def test_md_section_hits_keep_section_level_formatting(corpus_s):
    """MD は該当見出し節ごとの 1 件（節の開始〜終了行を span とする）で、rg 経由でも同じ。"""
    got = G.grep_search("手数料の計算", roots=[corpus_s], max_hits=1000)
    md = [h for h in got if h["ext"] == ".md"]
    assert md and all(h["span"][0] <= h["line"] <= h["span"][1] for h in md)
    assert any(h["span"][1] > h["span"][0] for h in md)
    assert got == G.grep_search("手数料の計算", roots=[corpus_s], max_hits=1000, engine="python")


@pytest.mark.parametrize("term", ["CP0041", "WS-COUNT", "消費税率"])
def test_paging_matches_python_engine_page_by_page(corpus_s, term):
    for offset in (0, 7, 14, 400):
        ref = G.grep_search(term, roots=[corpus_s], max_hits=7, offset=offset, engine="python")
        assert G.grep_search(term, roots=[corpus_s], max_hits=7, offset=offset) == ref


def test_world_path_with_importance_matches_python_engine(monkeypatch, tmp_path, corpus_s, count_discover):
    """重要度（`_重要度.txt`）がある world は rank 順に候補を開く。順位・打切りは参照実装と同じ。"""
    root = tmp_path / "world"
    shutil.copytree(corpus_s, root)
    (root / "gold.json").unlink()
    (root / "_重要度.txt").write_text(
        "架空システム/03_開発/01_ソース/PG00014.cbl: 高  # 主要\n架空システム/02_設計/*: 低\n", encoding="utf-8")
    world = _isolated_world(monkeypatch, tmp_path, root)
    for term in ("CP0041-ITEM-02", "WS-COUNT", "手数料の計算", "PG00014"):
        for max_hits, offset in ((5, 0), (5, 5), (1000, 0)):
            ref = G.grep_search(term, world=world, max_hits=max_hits, offset=offset, engine="python")
            got = G.grep_search(term, world=world, max_hits=max_hits, offset=offset)
            assert got == ref, (term, max_hits, offset)
    top = G.grep_search("PG00014", world=world, max_hits=3)
    assert top[0]["doc_id"].endswith("PG00014.cbl") and top[0]["importance"] == "高"
    if rg_search.find_rg() is not None:
        assert count_discover["n"] >= 1


def test_grep_search_many_equals_per_term_search(corpus_s, count_discover):
    terms = ["CP0041-ITEM-02", "消費税率", "PG00014", "ZZZ-NOSUCH-TERM", "CP0041"]  # CP0041 は CP0041-ITEM-02 を含む語
    many = G.grep_search_many(terms, roots=[corpus_s], max_hits=1000)
    assert list(many) == terms
    for t in terms:
        assert many[t] == G.grep_search(t, roots=[corpus_s], max_hits=1000, engine="python"), t
    if rg_search.find_rg() is not None:
        assert count_discover["n"] >= 1


def test_grep_search_many_scans_once_for_all_terms(corpus_s, monkeypatch):
    runs = {"n": 0}
    real = rg_search._run

    def _spy(*a, **kw):
        runs["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(rg_search, "_run", _spy)
    terms = ["CP0041-ITEM-02", "PG00014", "EXEC PGM", "WS-COUNT"]
    many = G.grep_search_many(terms, roots=[corpus_s], max_hits=50)
    # 語の数によらず原本の木を 1 回だけ走査する（ASCII の語だけなので CP932 の 2 回目も無い）。rg が無い環境は 0 回（純 Python 走査）。
    assert runs["n"] == (1 if rg_search.find_rg() is not None else 0)
    assert all(many[t] == G.grep_search(t, roots=[corpus_s], max_hits=50, engine="python") for t in terms)


# ===== (2) rg -i と Python lower() の非 ASCII の畳み方の差 =====

def test_nonascii_case_folding_difference_is_aligned_to_python_lower(tmp_path):
    """語尾のシグマ「ς」は Python の `lower()` では「σ」と別の文字だが、rg の -i は同じ文字として畳む。
    最終判定は Python に揃える（rg は候補を絞るだけ）ので、「σ」で「ς」だけの行はヒットにならない。
    """
    (tmp_path / "final_sigma.txt").write_text("ς\n", encoding="utf-8")
    (tmp_path / "small_sigma.txt").write_text("σ\n", encoding="utf-8")
    ref = G.grep_search("σ", roots=[tmp_path], engine="python")
    got = G.grep_search("σ", roots=[tmp_path])
    assert [h["doc_id"] for h in ref] == ["small_sigma.txt"]
    assert got == ref
    if rg_search.find_rg() is not None:
        cand = rg_search.discover(["σ"], [tmp_path], derived=False, first_only=True)
        assert {pathlib.Path(p).name for p in cand} == {"final_sigma.txt", "small_sigma.txt"}  # rg 側は広く拾う


def test_known_gap_dotted_capital_i_is_found_by_python_only(tmp_path):
    """既知の差（取りこぼし側）: 「İ」(U+0130) は Python の `lower()` で「i」＋結合点に展開されるが、rg の -i は「i」と畳まない。
    「i」で「İ」だけの行を探すと、参照実装はヒットし、rg 経由は候補に上がらずヒットしない。
    """
    (tmp_path / "dotted.txt").write_text("İ\n", encoding="utf-8")
    assert [h["doc_id"] for h in G.grep_search("i", roots=[tmp_path], engine="python")] == ["dotted.txt"]
    expected = ["dotted.txt"] if rg_search.find_rg() is None else []
    assert [h["doc_id"] for h in G.grep_search("i", roots=[tmp_path])] == expected


# ===== (3) キャッシュの無効化 =====

def _backdate(path: pathlib.Path, seconds: int = 3600) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_classify_cache_hits_then_invalidates_when_file_changes(monkeypatch, tmp_path):
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    f = tmp_path / "w" / "note.zzz"
    f.parent.mkdir()
    f.write_text("plain text\n", encoding="utf-8")
    _backdate(f)
    reads = {"n": 0}
    real = corpus_docs._read_head

    def _spy(rp, size=4096):
        reads["n"] += 1
        return real(rp, size)

    monkeypatch.setattr(corpus_docs, "_read_head", _spy)

    def _classify():
        cache = search_cache.ClassifyCache("cw")
        try:
            return corpus_docs.classify_file(f, "note.zzz", ".zzz", cache=cache)
        finally:
            cache.flush()

    first = _classify()
    assert corpus_docs._classify_verdict_reachable(first) and reads["n"] == 1
    assert _classify() == first and reads["n"] == 1  # 状態が同じ: 先頭を読まず保存済みの結果
    # 中身をバイナリに置き換える（サイズは同じ・更新時刻だけ変わる）
    f.write_bytes(b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a")
    t = time.time() - 1800
    os.utime(f, (t, t))
    assert f.stat().st_size == len("plain text\n")
    changed = _classify()
    assert reads["n"] == 2 and not corpus_docs._classify_verdict_reachable(changed)  # 古い値を返さない


def test_classify_cache_is_written_only_under_derived_dir(monkeypatch, tmp_path):
    """キャッシュは派生側にだけ置く。資料フォルダ（原本）には何も書かない。"""
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    root = tmp_path / "w"
    root.mkdir()
    f = root / "a.md"
    f.write_text("# t\nNEEDLE\n", encoding="utf-8")
    _backdate(f)
    before = sorted(p.name for p in root.iterdir())
    cache = search_cache.ClassifyCache("cw")
    corpus_docs.classify_file(f, "a.md", ".md", cache=cache)
    cache.flush()
    assert sorted(p.name for p in root.iterdir()) == before
    assert (tmp_path / "derived" / "cw" / "search_cache" / "cache.sqlite3").is_file()


def test_documents_listing_uses_cache_and_follows_file_changes(monkeypatch, tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    for n in ("a.md", "b.zzz"):
        (root / n).write_text("text\n", encoding="utf-8")
        _backdate(root / n)
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    reads = {"n": 0}
    real = corpus_docs._read_head

    def _spy(rp, size=4096):
        reads["n"] += 1
        return real(rp, size)

    monkeypatch.setattr(corpus_docs, "_read_head", _spy)
    first = {d["name"]: d for d in corpus_docs.world_documents("cw", root=root)}
    n1 = reads["n"]
    assert set(first) == {"a.md", "b.zzz"} and n1 >= 1
    again = {d["name"]: d for d in corpus_docs.world_documents("cw", root=root)}
    assert again == first and reads["n"] == n1  # 2 回目は先頭を読まない
    (root / "b.zzz").write_bytes(b"\x00\x01\x02\x03\x04\x05")  # バイナリへ置換 → 台帳から消える
    t = time.time() - 600
    os.utime(root / "b.zzz", (t, t))
    (root / "c.md").write_text("new\n", encoding="utf-8")
    _backdate(root / "c.md")
    after = {d["name"] for d in corpus_docs.world_documents("cw", root=root)}
    assert after == {"a.md", "c.md"}


def test_read_doc_total_lines_cached_and_invalidated(monkeypatch, tmp_path):
    world = "rdcache"
    kb = tmp_path / "kb"
    doc = kb / world / "doc.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("\n".join(f"line {i}" for i in range(1, 1001)) + "\n", encoding="utf-8")
    _backdate(doc)
    monkeypatch.setenv("SHERPA_KB_DIR", str(kb))
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    for env in ("SHERPA_MCP_WORLD", "SHERPA_MCP_WORLD_ROOT"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(store, "get_world", lambda world_id: None)
    seen = {"n": 0}
    real = G._logical_lines

    def _count(reader, cap, encoding="utf-8"):
        for line in real(reader, cap, encoding=encoding):
            seen["n"] += 1
            yield line

    monkeypatch.setattr(RT.grep_tool, "_logical_lines", _count)
    r1, *_ = TD.run_tool("read_doc", {"doc_id": "doc.md"}, world, None, window_cap=5)
    assert r1["total_lines"] == 1000 and r1["end_line"] == 200 and seen["n"] == 1000  # 初回は末尾まで読んで数える
    seen["n"] = 0
    r2, *_ = TD.run_tool("read_doc", {"doc_id": "doc.md", "start_line": 201}, world, None, window_cap=5)
    assert r2["total_lines"] == 1000 and r2["start_line"] == 201 and r2["end_line"] == 400
    assert r2["text"].splitlines()[0] == "201: line 201"
    assert seen["n"] == 400  # 2 ページ目はページの終端までだけ読む
    # 書き換え（行数が変わる）→ 古い総行数を返さない
    doc.write_text("\n".join(f"line {i}" for i in range(1, 1051)) + "\n", encoding="utf-8")
    r3, *_ = TD.run_tool("read_doc", {"doc_id": "doc.md", "start_line": 201}, world, None, window_cap=5)
    assert r3["total_lines"] == 1050


def test_page_cache_reused_by_offset_calls_and_invalidated_by_new_file(monkeypatch, tmp_path, count_discover):
    scans = 1 if rg_search.find_rg() is not None else 0  # rg が無い環境は純 Python 走査（候補一覧は作らない）
    root = tmp_path / "world"
    root.mkdir()
    for i in range(12):
        (root / f"f{i:02d}.md").write_text("# 見出し\nPAGEKEY を含む\n", encoding="utf-8")
    _backdate(root)
    world = _isolated_world(monkeypatch, tmp_path, root)
    page0 = G.grep_search("PAGEKEY", world=world, max_hits=5, offset=0)
    assert count_discover["n"] == scans
    page1 = G.grep_search("PAGEKEY", world=world, max_hits=5, offset=5)
    assert count_discover["n"] == scans  # 2 ページ目は保存済みの候補一覧から切り出す（rg を走らせない）
    assert [h["doc_id"] for h in page0 + page1] == [f"f{i:02d}.md" for i in range(10)]
    assert page1 == G.grep_search("PAGEKEY", world=world, max_hits=5, offset=5, engine="python")
    (root / "f99.md").write_text("# 見出し\nPAGEKEY も含む\n", encoding="utf-8")  # フォルダの状態が変わる
    page2 = G.grep_search("PAGEKEY", world=world, max_hits=5, offset=10)
    assert count_discover["n"] == 2 * scans  # 署名が変わったので走査し直し、新しいファイルも拾う
    assert [h["doc_id"] for h in page2] == ["f10.md", "f11.md", "f99.md"]


# ===== (4) rg が無いときの縮退 =====

def test_degrades_to_python_scan_when_rg_is_missing(monkeypatch, caplog, corpus_s):
    ref = G.grep_search("CP0041-ITEM-02", roots=[corpus_s], max_hits=1000, engine="python")
    monkeypatch.setattr(rg_search, "find_rg", lambda: None)
    monkeypatch.setattr(G, "_RG_FALLBACK_LOGGED", False)
    called = {"n": 0}
    monkeypatch.setattr(rg_search, "discover", lambda *a, **kw: called.__setitem__("n", called["n"] + 1) or {})
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        got = G.grep_search("CP0041-ITEM-02", roots=[corpus_s], max_hits=1000)
        again = G.grep_search("消費税率", roots=[corpus_s], max_hits=1000)
    assert got == ref and called["n"] == 0
    assert again == G.grep_search("消費税率", roots=[corpus_s], max_hits=1000, engine="python")
    msgs = [r.getMessage() for r in caplog.records if "縮退" in r.getMessage()]
    assert len(msgs) == 1 and "rg が見つかりません" in msgs[0]  # ログで知らせる（プロセスごとに 1 回）


def test_degrades_when_rg_fails_midway_and_keeps_world_semantics(monkeypatch, caplog, tmp_path, corpus_s):
    root = tmp_path / "world"
    shutil.copytree(corpus_s, root)
    (root / "gold.json").unlink()
    (root / "_重要度.txt").write_text("架空システム/03_開発/01_ソース/PG00014.cbl: 高\n", encoding="utf-8")
    world = _isolated_world(monkeypatch, tmp_path, root)
    ref = G.grep_search("PG00014", world=world, max_hits=10, engine="python")

    def _boom(*a, **kw):
        raise rg_search.RgUnavailable("rg が正常に終わりませんでした (rc=2)")

    monkeypatch.setattr(rg_search, "discover", _boom)
    monkeypatch.setattr(G, "_RG_FALLBACK_LOGGED", False)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert G.grep_search("PG00014", world=world, max_hits=10) == ref
    assert any("縮退" in r.getMessage() for r in caplog.records)


def test_degrades_when_rg_cannot_be_started(monkeypatch, tmp_path):
    (tmp_path / "a.md").write_text("NEEDLE\n", encoding="utf-8")
    monkeypatch.setattr(rg_search, "find_rg", lambda: str(tmp_path / "no-such-rg"))
    monkeypatch.setattr(G, "_RG_FALLBACK_LOGGED", False)
    assert [h["doc_id"] for h in G.grep_search("NEEDLE", roots=[tmp_path])] == ["a.md"]


def test_rg_lookup_prefers_codex_bundled_then_path(monkeypatch, tmp_path):
    """探索順: Codex 同梱の rg → PATH 上の rg → 無ければ None。"""
    bundled = tmp_path / "codex" / "codex-path"
    bundled.mkdir(parents=True)
    exe = bundled / "rg"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    rg_search.find_rg.cache_clear()
    try:
        from sherpa.providers.codex import sandbox
        monkeypatch.setattr(sandbox, "_codex_bundled_rg_dir", lambda: bundled)
        assert rg_search.find_rg() == str(exe)
        rg_search.find_rg.cache_clear()
        monkeypatch.setattr(sandbox, "_codex_bundled_rg_dir", lambda: None)
        monkeypatch.setattr(W, "_repo_root", lambda: tmp_path / "norepo")
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/rg" if name == "rg" else None)
        assert rg_search.find_rg() == "/usr/bin/rg"
        rg_search.find_rg.cache_clear()
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert rg_search.find_rg() is None
    finally:
        rg_search.find_rg.cache_clear()


def test_unreadable_candidate_is_still_counted_as_unreadable(monkeypatch, tmp_path):
    """rg が読めなかったファイルも、純 Python 走査と同じく「読めなかったファイル」として申告する。"""
    if os.geteuid() == 0:
        return  # root は権限 0 のファイルも開ける
    ok = tmp_path / "ok.md"
    ok.write_text("NEEDLE\n", encoding="utf-8")
    locked = tmp_path / "locked.md"
    locked.write_text("NEEDLE\n", encoding="utf-8")
    locked.chmod(0)
    try:
        s_py, s_rg = {}, {}
        ref = G.grep_search("NEEDLE", roots=[tmp_path], stats=s_py, engine="python")
        got = G.grep_search("NEEDLE", roots=[tmp_path], stats=s_rg)
        assert got == ref and s_rg == s_py == {"unreadable_files": 1}
    finally:
        locked.chmod(0o644)


def test_secret_named_files_are_dropped_after_rg_finds_them(tmp_path):
    """rg の走査対象に秘匿名のファイルが含まれても、ヒットの前に今と同じ判定で落とす。"""
    (tmp_path / "ok.txt").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "id_rsa").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "prod.env").write_text("NEEDLE=1\n", encoding="utf-8")
    (tmp_path / "credentials.txt").write_text("NEEDLE\n", encoding="utf-8")
    assert [h["doc_id"] for h in G.grep_search("NEEDLE", roots=[tmp_path])] == ["ok.txt"]
    assert G.grep_search("NEEDLE", roots=[tmp_path]) == G.grep_search("NEEDLE", roots=[tmp_path], engine="python")


def test_vcs_dirs_hidden_files_and_ignore_files_do_not_change_the_candidate_set(tmp_path):
    """.gitignore／隠しファイルは rg の既定では除外されるが、純 Python 走査と同じ集合（隠しは対象・版管理のフォルダだけ対象外）にする。"""
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / ".hidden.txt").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.txt").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.txt").write_text("NEEDLE\n", encoding="utf-8")
    got = G.grep_search("NEEDLE", roots=[tmp_path])
    assert got == G.grep_search("NEEDLE", roots=[tmp_path], engine="python")
    assert {h["doc_id"] for h in got} == {"ignored.txt", ".hidden.txt", "sub/x.txt"}
