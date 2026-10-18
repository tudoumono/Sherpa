"""ソース（登録コードと汎用コード）のチャンクを ES に入れないことを、実 ES・実際の取り込みで確かめる結合テスト。

`fixtures/corpus/v1`（ソースと設計書）と `fixtures/eval` の Excel を取り込み、次を固定する:
ES のチャンク数がソース分だけ減る、Office・設計書の検索結果が変更前後で同じ、`es_search` が層 code で案内を返す、
ソースのチャンクが残った旧索引（印なし）は次の更新で 1 回だけ入れ直されて消える。ES に到達できなければ SKIP。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sherpa import corpus_docs, tool_dispatch, worlds
from sherpa import es_index as es
from sherpa.ingest import office_md, worker

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "fixtures" / "corpus" / "v1"
EXCEL = ROOT / "fixtures" / "eval" / "excel_ja" / "inputs"
WORLD = "es-nosrc-check"
_QUERIES = ("消費税率", "手数料", "TAXCALC", "現行顧客区分", "A01", "請求書", "COMPUTE", "表示名")


@pytest.fixture
def es_world(tmp_path, monkeypatch):
    if not es.available():
        pytest.skip("ES 未起動")
    from _world_registry import register_test_world
    src = tmp_path / "src"
    shutil.copytree(CORPUS, src)
    for name in ("JPX-016.xlsx", "JPX-001.xlsx"):
        shutil.copy(EXCEL / name, src / name)
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    real_world_dir = worlds.world_dir
    monkeypatch.setattr(worlds, "world_dir", lambda w: src if w == WORLD else real_world_dir(w))
    monkeypatch.setattr(es.embeddings, "cfg", lambda settings=None, **kw: None)
    rep = office_md.build_derived(src, worlds.derived_md_dir(WORLD), world_sig="s1")
    assert rep.get("error") is None, rep
    register_test_world(WORLD)
    yield WORLD
    es.delete_world(WORLD)


def _raw_search(world: str, query: dict, size: int = 2000) -> list:
    res = es._req("POST", f"/{es._index(world)}/_search", {"size": size, "query": query})
    return [h["_source"] for h in res["hits"]["hits"]]


def _docs_snapshot(world: str) -> dict:
    """資料側（層 docs）の本文検索のヒット（doc_id・チャンク・順位）。"""
    out = {}
    for q in _QUERIES:
        hits, reason = es.search(world, q, k=50, vector=False, layer="docs")
        assert reason is None
        out[q] = [(h["doc_id"], h.get("chunk_id"), h.get("line")) for h in hits]
    return out


def _index_with_source(world: str):
    """変更前の挙動（ソースも ES に入れる）で索引する。"""
    real = es._is_source_doc
    es._is_source_doc = lambda d: False
    try:
        return es.index_world(world)
    finally:
        es._is_source_doc = real


def test_source_chunks_are_not_in_es_and_docs_results_do_not_change(es_world, monkeypatch):
    src_docs = [d["name"] for d in corpus_docs.world_documents(es_world, include_rag=True) if d.get("branch") == "source"]
    assert len(src_docs) >= 10                                                   # cbl／cpy／jcl
    before = _index_with_source(es_world)
    n_before = es.count(es_world)
    source_chunks = len(_raw_search(es_world, {"term": {"branch": "source"}}))
    assert source_chunks > 0 and n_before == before["chunks"]
    snap_before = _docs_snapshot(es_world)

    after = es.index_world(es_world)
    n_after = es.count(es_world)
    assert after["chunks"] == n_after == n_before - source_chunks                # ソース分だけ減る
    assert not _raw_search(es_world, {"term": {"branch": "source"}})
    assert {s["doc_id"] for s in _raw_search(es_world, {"match_all": {}})}.isdisjoint(src_docs)
    assert es._index_meta(es_world)["source_excluded"] is True
    snap_after = _docs_snapshot(es_world)
    # Office・設計書の結果は同じ。語の出現数の統計がソース分だけ変わるので、同点付近の並びは入れ替わりうる＝ヒットの集合と先頭で見る
    assert any(snap_after.values())
    for q, hits in snap_before.items():
        assert set(snap_after[q]) == set(hits), q
        assert snap_after[q][:1] == hits[:1], q
    print(f"ES chunks before={n_before} after={n_after} source={source_chunks}")


def test_es_search_tool_notes_where_to_search_source(es_world):
    es.index_world(es_world)
    both = tool_dispatch.run_tool("es_search", {"query": "TAXCALC"}, es_world, None)[0]
    assert "ripgrep_search" in both["notice"] and "error" not in both
    assert all(not h["doc_id"].endswith((".cbl", ".cpy", ".jcl")) for h in both["hits"])
    code = tool_dispatch.run_tool("es_search", {"query": "TAXCALC"}, es_world, None, layer="code")[0]
    assert code["hits"] == [] and "ripgrep_search" in code["notice"] and "error" not in code
    docs = tool_dispatch.run_tool("es_search", {"query": "消費税率"}, es_world, None, layer="docs")[0]
    assert docs["hits"] and "notice" not in docs


def test_old_index_with_source_chunks_is_rebuilt_once_and_loses_them(es_world, monkeypatch):
    """ソースのチャンクが残った旧索引（`source_excluded` の印なし）は、次の更新で 1 回だけ入れ直されてソースが消える。"""
    def sync() -> bool:
        if es.needs_reindex(es_world, "sig-1", settings={}):
            return worker.index_world_with_human_md_holdback(es_world, content_sig="sig-1", settings={}).get("error") is None
        return False

    real = es._is_source_doc
    monkeypatch.setattr(es, "_is_source_doc", lambda d: False)
    assert sync() is True
    monkeypatch.setattr(es, "_is_source_doc", real)
    meta = es._index_meta(es_world)
    es._req("PUT", f"/{es._index(es_world)}/_mapping", {"_meta": {k: v for k, v in meta.items() if k != "source_excluded"}})
    assert _raw_search(es_world, {"term": {"branch": "source"}})                 # 旧索引にはソースが残っている
    assert es.needs_reindex(es_world, "sig-1", settings={}) is True              # 版は同じでも印が無ければ入れ直す
    assert sync() is True
    assert not _raw_search(es_world, {"term": {"branch": "source"}})
    assert es.needs_reindex(es_world, "sig-1", settings={}) is False             # 以降は入れ直さない
    assert sync() is False
