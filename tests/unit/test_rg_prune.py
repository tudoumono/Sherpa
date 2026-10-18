"""rg_prune（rg に歩かせない場所の計画）の契約テスト。

計画ありの `grep_search` が、計画なし・純 Python 走査と同じヒットを返すこと（結果を変えない）と、
計画が追加・削除・改名に追随することを確かめる。
"""
from __future__ import annotations

import os
import pathlib

import pytest

from sherpa import grep_tool as G
from sherpa import rg_prune, rg_search

pytestmark = [pytest.mark.usefixtures("upstream_only_registry"),
              pytest.mark.skipif(rg_search.find_rg() is None, reason="rg が無い環境では候補の絞り込みを使わない")]

WORLD = "tw"
NEEDLE = "NEEDLEWORD"


def _write(root: pathlib.Path, rel: str, body) -> pathlib.Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    return p


@pytest.fixture
def env(monkeypatch, tmp_path):
    """資料フォルダ `world/` と派生領域 `derived/tw/{md,rag}` を tmp_path に置く。"""
    from sherpa import worlds as W
    root = tmp_path / "world"
    root.mkdir()
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    monkeypatch.setattr(W, "world_dir", lambda w: root)
    rg_prune._MEMO.clear()
    yield root, tmp_path / "derived" / WORLD / "md", tmp_path / "derived" / WORLD / "rag"
    rg_prune._MEMO.clear()


def _key(hits):
    return sorted((h["doc_id"], h["line"], h["text"]) for h in hits)


def _compare(**kw):
    """計画あり・計画なし・純 Python 走査のヒットが同じことを確かめ、計画ありのヒットを返す。"""
    pruned = _key(G.grep_search(NEEDLE, world=WORLD, **kw))
    orig = rg_prune.plan
    try:
        rg_prune.plan = lambda *a, **k: dict(rg_prune._NO_PRUNE)
        plain = _key(G.grep_search(NEEDLE, world=WORLD, **kw))
    finally:
        rg_prune.plan = orig
    python = _key(G.grep_search(NEEDLE, world=WORLD, engine="python", **kw))
    assert pruned == plain == python
    return pruned


def _bump(*paths):
    """フォルダの更新時刻を確実に進める（署名の粒度に依存しないため）。"""
    for p in paths:
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def _populate(root, md, rag):
    _write(root, "規程/元からMD.md", f"# 元からMD\n{NEEDLE} original markdown\n")
    _write(root, "規程/仕様.pdf", f"%PDF-1.4\nstream\n{NEEDLE} raw text in pdf\nendstream\n".encode())
    _write(root, "規程/表.xlsx", f"PK\x03\x04 {NEEDLE} raw".encode())
    _write(root, "規程/図.png", b"\x89PNG\r\n" + NEEDLE.encode())
    _write(root, "規程/メモ.txt", f"{NEEDLE} memo\n")
    # 派生: A は .rag.md と legacy .md の両方 / B は legacy .md だけ / C は .markdown
    _write(rag, "規程/仕様.pdf.rag.md", f"# 仕様\n{NEEDLE} from rag\n")
    _write(md, "規程/仕様.pdf.md", f"# 仕様\n{NEEDLE} from legacy md\n")
    _write(md, "別/表.xlsx.md", f"# 表\n{NEEDLE} legacy only\n")
    _write(md, "別/資料.markdown", f"# 資料\n{NEEDLE} markdown ext\n")
    _write(rag, "別/他.docx.rag.md", f"# 他\n{NEEDLE} rag only\n")
    _write(md, "別/他.docx.md", f"# 他\n{NEEDLE} dup of rag only\n")


def test_pruned_hits_equal_unpruned_and_python(env):
    root, md, rag = env
    _populate(root, md, rag)
    hits = _compare()
    docs = {h[0] for h in hits}
    assert "規程/元からMD.md" in docs          # 元から MD の資料は派生 MD と取り違えない
    assert "規程/仕様.pdf" in docs              # 派生は原本の名前で出る
    assert "別/表.xlsx" in docs                 # rag.md が無い文書は md/ から拾う
    assert "別/資料.markdown" in docs
    assert "規程/仕様.pdf.md" not in docs and "規程/表.xlsx" not in docs


def test_plan_prunes_what_it_should(env):
    root, md, rag = env
    _populate(root, md, rag)
    from sherpa import search_cache
    from sherpa import worlds as W
    orig, der = [root], [rag, md]
    pl = rg_prune.plan(WORLD, orig, der, search_cache.tree_signature(orig + der))
    assert {".pdf", ".xlsx", ".docx", ".png"} <= set(pl["skip_exts"])
    # 規程/ は rag.md がある文書の legacy .md だけ＝歩かない。別/ は legacy だけの文書があるので歩く
    assert pl["md_dirs"] == [str(md / "別")]
    assert rg_prune.derived_roots(der, pl) == [rag, md / "別"]


def test_all_duplicates_means_md_tree_not_walked(env):
    root, md, rag = env
    _write(rag, "a/x.pdf.rag.md", f"{NEEDLE} a\n")
    _write(md, "a/x.pdf.md", f"{NEEDLE} a legacy\n")
    from sherpa import search_cache
    pl = rg_prune.plan(WORLD, [root], [rag, md], search_cache.tree_signature([root, rag, md]))
    assert pl["md_dirs"] == []
    assert rg_prune.derived_roots([rag, md], pl) == [rag]
    assert [h["doc_id"] for h in G.grep_search(NEEDLE, world=WORLD)] == ["a/x.pdf"]


def test_dir_named_like_skipped_ext_is_still_searched(env):
    root, md, rag = env
    _write(root, "旧.pdf/メモ.txt", f"{NEEDLE} in dir named pdf\n")
    _write(root, "旧.pdf/深い/下.txt", f"{NEEDLE} deeper\n")
    _write(root, "他/実物.pdf", f"{NEEDLE} raw\n".encode())
    md.mkdir(parents=True)
    rag.mkdir(parents=True)
    hits = _compare()
    assert {h[0] for h in hits} == {"旧.pdf/メモ.txt", "旧.pdf/深い/下.txt"}


def test_extension_only_name_is_searched_like_before(env):
    root, md, rag = env
    _write(root, ".pdf", f"{NEEDLE} dotfile named pdf\n")
    _write(root, "x/.xlsx", f"{NEEDLE} dotfile named xlsx\n")
    md.mkdir(parents=True)
    rag.mkdir(parents=True)
    _compare()


def test_uppercase_extension_is_skipped_too(env):
    root, md, rag = env
    _write(root, "A.PDF", f"{NEEDLE} raw\n".encode())
    _write(root, "B.Docx", f"{NEEDLE} raw\n".encode())
    _write(root, "c.txt", f"{NEEDLE} text\n")
    md.mkdir(parents=True)
    rag.mkdir(parents=True)
    assert {h[0] for h in _compare()} == {"c.txt"}


def test_plan_follows_added_and_removed_files(env):
    root, md, rag = env
    _write(rag, "a/x.pdf.rag.md", f"{NEEDLE} rag\n")
    _write(md, "a/x.pdf.md", f"{NEEDLE} legacy\n")
    assert [h["doc_id"] for h in G.grep_search(NEEDLE, world=WORLD)] == ["a/x.pdf"]
    # rag.md が消えると legacy .md が選ばれる
    (rag / "a/x.pdf.rag.md").unlink()
    _bump(rag / "a", rag, md / "a", md)
    hits = G.grep_search(NEEDLE, world=WORLD)
    assert [(h["doc_id"], h["text"].strip().splitlines()[-1]) for h in hits] == [("a/x.pdf", f"{NEEDLE} legacy")]
    # 歩かせていなかったフォルダに新しい .md（rag.md なし）ができたら見つかる
    _write(md, "a/y.docx.md", f"{NEEDLE} new\n")
    _bump(md / "a", md)
    assert {h["doc_id"] for h in G.grep_search(NEEDLE, world=WORLD)} == {"a/x.pdf", "a/y.docx"}
    # 原本側に PDF と同じ綴りのフォルダができたら、その中の文書も見つかる
    _write(root, "新.pdf/z.txt", f"{NEEDLE} z\n")
    _bump(root)
    assert "新.pdf/z.txt" in {h["doc_id"] for h in G.grep_search(NEEDLE, world=WORLD)}


def test_analyzer_registered_ext_is_not_skipped(env, monkeypatch):
    from sherpa.ingest.analyzers import registry
    real = registry.candidates
    monkeypatch.setattr(registry, "candidates", lambda rel: ("x",) if rel.endswith(".xlsx") else real(rel))
    skip = rg_prune._skippable_exts()
    assert ".xlsx" not in skip and ".pdf" in skip


@pytest.mark.parametrize("rel,rag_files,kept", [
    ("a/x.pdf.md", {"a/x.pdf.rag.md"}, False),       # rag.md があれば legacy は選ばれない
    ("a/x.pdf.md", set(), True),
    ("a/x.pdf.rag.md", {"a/x.pdf.rag.md"}, True),
    ("a/x.pdf.rag_observations.md", set(), False),   # 観測の本文は rag.md へ統合済み
    ("a/y.markdown", {"a/y.rag.md"}, True),          # `.markdown` は二重ヒットの規則の対象外
    ("a/n.md", set(), True),
])
def test_kept_matches_resolve_entry(env, rel, rag_files, kept):
    root, md, rag = env
    for r in rag_files:
        _write(rag, r, "x")
    _write(md, rel, "x")
    got = G._resolve_entry(md / rel, md.resolve(), True, rag, None)
    assert (got is not None) == kept == rg_prune._kept(rel, rag_files)
