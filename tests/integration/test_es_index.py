"""Elasticsearch 全文索引（日本語BM25・world単位）の単体テスト。

ES 未起動でも runner を赤にしない＝**到達できなければ SKIP**（純粋関数だけ検証）。ES 起動時は
index→search→範囲フィルタ→delete の往復と、agentic の es_search ツールを検証。
"""
from __future__ import annotations

import json
import pathlib

import pytest
from _world_setup import TEST_WORLD_ID

from sherpa import es_index as es

V = TEST_WORLD_ID   # 旧固定 'v1' から移行（2026-07-03 インシデント対応 HIGH#2・_world_setup.py 参照）


def test_scopes_pure():
    assert es._scopes("4期/02_設計/01_基本設計/税計算仕様書.md") == \
        ["4期", "4期/02_設計", "4期/02_設計/01_基本設計"]
    assert es._scopes("a.md") == []


def test_es_roundtrip_if_available():
    if not es.available():
        pytest.skip("ES 未起動")
    from _world_registry import register_test_world
    register_test_world(V)   # V の ES index をセッション終了時に削除（backstop・tests/_world_registry.py）
    r = es.index_world(V)
    assert r["available"] and r["indexed"] > 0 and r["chunks"] > 0
    assert (es.count(V) or 0) > 0
    # RV3（FBK-1・2026-09-01）: es_index.search() は (hits, degrade_reason) を返す（実 ES が
    # 正常応答する通常経路では reason は None のまま）。
    hits, reason = es.search(V, "消費税率", k=5)
    assert reason is None
    assert hits and all("doc_id" in h and "score" in h for h in hits)
    scoped, reason = es.search(V, "消費税率", scope_paths=["4期/02_設計"], k=5)
    assert reason is None
    assert all(h["doc_id"].startswith("4期/02_設計") for h in scoped)
    # agentic の es_search ツール経由
    from sherpa import agentic_search as A
    res, docs, cites, _cards = A.run_tool("es_search", {"query": "消費税率"}, V, None)
    assert res["hits"] and docs and all("span" in c for c in cites)
    assert "degrade_reason" not in res
    # 範囲外検索は 0 件（5期 に消費税率の設計は無い想定）でも doc は 5期 配下に限定される
    s2, reason = es.search(V, "バッチ", scope_paths=["5期"], k=5)
    assert reason is None
    assert all(h["doc_id"].startswith("5期") for h in s2)


def test_index_world_progress_callback():
    """`progress`（省略可）は Pass2 が文書グループを flush するたび `(done_docs, total_docs)`
    で呼ばれる（実環境で最長段の done/total を UI 進捗へ出す配線・`ingest/worker.py:_es_progress`
    が使う契約）。`total` は毎回同じ値（対象文書一覧の長さ・スキップされた文書も含む）・`done` は
    単調非減少で最終呼び出しの `done == r["indexed"]`（実際に索引した文書数）。`progress=None`
    （既定）は他の既存テストが引き続き検証する通常経路のまま（このテストは新規引数の追加分だけを見る）。
    """
    if not es.available():
        pytest.skip("ES 未起動")
    from _world_registry import register_test_world
    register_test_world(V)
    calls = []
    r = es.index_world(V, progress=lambda done, total: calls.append((done, total)))
    assert r["available"] and r["indexed"] > 0
    assert calls, "文書がある world なら progress が最低1回は呼ばれる"
    totals = {t for _, t in calls}
    assert len(totals) == 1                                 # total は呼び出し間で一貫している
    total = totals.pop()
    assert total >= r["indexed"]                            # total は対象一覧の長さ（スキップ分を含みうる）
    dones = [d for d, _ in calls]
    assert dones == sorted(dones)                           # 単調非減少
    assert dones[-1] == r["indexed"]                         # 最終呼び出しは実際に索引した件数と一致


def test_vector_hybrid_stubbed():
    """ベクトル（dense_vector）索引＋ハイブリッド検索の配線を**スタブ埋め込み**で検証（実 API なし）。"""
    if not es.available():
        pytest.skip("ES 未起動")
    from _world_registry import register_test_world
    register_test_world(V)   # V の ES index をセッション終了時に削除（backstop・tests/_world_registry.py）
    from sherpa import embeddings
    oc, oe, orq = embeddings.cfg, embeddings.embed, es._req
    embeddings.cfg = lambda settings=None, **kw: {"provider": "stub", "key": "x", "model": "stub", "dim": 8}
    embeddings.embed = lambda texts, c, **kw: [[0.1] * 8 for _ in texts]
    bodies = []

    def cap(method, path, body=None, **kw):
        if isinstance(path, str) and path.endswith("/_search"):
            bodies.append(body)
        return orq(method, path, body, **kw)
    es._req = cap
    try:
        r = es.index_world(V, settings={})
        assert r.get("vectors") is True and r["chunks"] > 0      # dense_vector 付きで索引（_meta も記録）
        hits, reason = es.search(V, "消費税", k=3, settings={})
        assert reason is None
        assert hits and any("knn" in (b or {}) for b in bodies)  # _meta 一致で kNN 経路が実際に使われた
    finally:
        embeddings.cfg, embeddings.embed, es._req = oc, oe, orq
        es.index_world(V, settings={})                        # BM25 索引へ戻す（DISABLE_EMBED）


def test_qa_merge_brings_in_es():
    """Codex/非agentic も ES を参照できる＝qa の facts(citations) に ES ヒットが統合される（stub・重複排除）。"""
    from sherpa import chat_service, documents, es_index
    o_s, o_r = es_index.search, documents.world_rel_set
    # RV2（FBK-1・2026-09-01）: `es_index.search()` は (hits, degrade_reason) を返す。
    es_index.search = lambda world, q, scope_paths=None, k=8, vector=True, layer=None, **kw: ([
        {"doc_id": "案件/x.md", "line": 3, "text": "ES固有ヒット", "ext": ".md"},
        {"doc_id": "案件/dup.md", "line": 1, "text": "重複", "ext": ".md"}], None)
    documents.world_rel_set = lambda world=None, **kw: {"案件/x.md", "案件/dup.md"}      # 実在チェックを通す（stub）
    try:
        base = {"citations": [{"doc_id": "案件/dup.md", "span": [1, 1], "quote": "grep"}]}
        merged = chat_service._merge_qa_with_es(base, V, "クエリ", None)
        docs = [c["doc_id"] for c in merged["citations"]]
        assert "案件/x.md" in docs                       # ES 固有ヒットが入る
        assert docs.count("案件/dup.md") == 1           # doc_id+span で重複排除
    finally:
        es_index.search, documents.world_rel_set = o_s, o_r


def test_delete_missing_is_ok():
    if not es.available():
        pytest.skip("ES 未起動")
    assert es.delete_world("no_such_world_xyz") is True   # 無いインデックス削除は成功扱い


def test_embed_cache_reuse():
    """差分embed: 内容ハッシュキャッシュで未変更チャンクは再 embed しない（コスト最適化）。

    EMBED-3: キャッシュはシャード化（`_embed_cache_dir`）＝`_embed_cached` はもう剪定/削除を
    自動で行わない（`index_world` が doc グループごとに複数回呼ぶため、呼ぶたびに剪定/削除すると
    前のグループの分を消してしまう・`_embed_cached` docstring 参照）。剪定・削除は
    `_prune_embed_cache`/`_delete_embed_cache` を明示的に呼ぶ（`index_world` が world 全体の
    doc ストリームを一巡した後に1回だけ行うのと同じ契約）。ES 不要・stub。"""
    import shutil
    import tempfile
    from sherpa import embeddings
    calls = []                                            # embed に渡したテキスト群を記録（=実 API コール）
    o_embed, o_dd = embeddings.embed, es.worlds.derived_dir
    embeddings.embed = lambda texts, c, **kw: (calls.append(list(texts)) or [[0.0] * c["dim"] for _ in texts])
    tmp = tempfile.mkdtemp()
    es.worlds.derived_dir = lambda w: pathlib.Path(tmp) / w
    ec = {"provider": "p", "model": "m", "dim": 4}
    w = "embcache_test"
    cache_dir = pathlib.Path(tmp) / w / "semantic" / "embed_cache"
    try:
        # 初回: distinct {a,b}（重複 b は1回だけ）→ 2件 embed・3ベクトル返る
        vecs, reused, embedded = es._embed_cached(w, ["a", "b", "b"], ec)
        assert len(vecs) == 3 and (reused, embedded) == (0, 2) and len(calls) == 1 and len(calls[-1]) == 2
        # 変更: c だけ新規 → c のみ embed・a,b は再利用
        vecs, reused, embedded = es._embed_cached(w, ["a", "b", "c"], ec)
        assert (reused, embedded) == (2, 1) and calls[-1] == ["c"]
        # 全て既知 → embed は呼ばれない（コール数据え置き）
        n = len(calls)
        vecs, reused, embedded = es._embed_cached(w, ["a"], ec)
        assert (reused, embedded) == (1, 0) and len(calls) == n
        # 剪定は明示呼び出し（`_prune_embed_cache`）でのみ起きる＝現存（a だけ）に縮む
        es._prune_embed_cache(w, {es._chunk_key(ec, "a")})
        assert _all_cache_keys(cache_dir) == {es._chunk_key(ec, "a")}
        # 素性変更（モデル違い）→ キー別＝再 embed
        es._embed_cached(w, ["a"], {**ec, "model": "m2"})
        assert calls[-1] == ["a"]
        # embed 失敗（None）→ BM25 降格・キャッシュは壊さない（このバッチ分は何も書き込まれない）
        before = {f.name: f.read_text() for f in cache_dir.glob("*.json")}
        embeddings.embed = lambda texts, c, **kw: None
        assert es._embed_cached(w, ["zzz-new"], ec) == (None, 0, 0)
        after = {f.name: f.read_text() for f in cache_dir.glob("*.json")}
        assert after == before
        # `_embed_cached` 自体は現存チャンク無し/埋め込み無効でもキャッシュへ一切触れない（ADD-only）
        # ——削除は呼び出し元（`_delete_embed_cache`）の責務（RV Med 相当・削除残骸を残さない・鏡）。
        embeddings.embed = lambda texts, c, **kw: (calls.append(list(texts)) or [[0.0] * c["dim"] for _ in texts])
        es._embed_cached(w, ["k1", "k2"], ec)
        assert cache_dir.is_dir() and list(cache_dir.glob("*.json"))
        assert es._embed_cached(w, [], ec) == (None, 0, 0)
        assert cache_dir.is_dir() and list(cache_dir.glob("*.json"))    # 空呼び出しでは自動では消えない
        es._delete_embed_cache(w)
        assert not cache_dir.exists()                                   # 明示削除で消える
        es._embed_cached(w, ["k1"], ec)
        assert cache_dir.is_dir()
        assert es._embed_cached(w, ["k1"], None) == (None, 0, 0)
        assert cache_dir.is_dir()                                       # ec=None でも自動では消えない
        es._delete_embed_cache(w)
        assert not cache_dir.exists()
        # RV Low: 壊れた（次元不一致の）キャッシュは miss 扱いで再 embed（毒ベクトルを使わない）
        es._embed_cache_write_batch(w, {es._chunk_key(ec, "k9"): [0.0, 0.0]})   # dim=2≠4
        m = len(calls)
        es._embed_cached(w, ["k9"], ec)
        assert calls[-1] == ["k9"] and len(calls) == m + 1
    finally:
        embeddings.embed, es.worlds.derived_dir = o_embed, o_dd
        shutil.rmtree(tmp, ignore_errors=True)


def _all_cache_keys(cache_dir):
    """テスト用: シャード化キャッシュ（`es._embed_cache_dir`）配下の全シャードから現存キーを集める。"""
    keys = set()
    for f in cache_dir.glob("*.json"):
        keys.update(json.loads(f.read_text())["vectors"].keys())
    return keys


def test_embed_cache_streaming_flush_and_resume():
    """EMBED-2/EMBED-3: 不足分（`need`）を `_EMBED_FLUSH_CHUNKS` バッチへ分割し、バッチ成功ごとに即
    シャード（`es._embed_cache_dir`）へフラッシュする（メモリ有界化＋再開性）。ES 不要・stub。

    EMBED-3: `_embed_cached` はもう「現存分だけへ剪定」しない（`index_world` が doc グループごとに
    複数回呼ぶため・`_embed_cached` docstring 参照）——このテストの (c) は元々「全成功後に自動で
    剪定される」ことを検証していたが、新しい契約では ADD-only なので、代わりに `_prune_embed_cache`
    を明示的に呼んで確認する。"""
    import tempfile
    from sherpa import embeddings

    def vec_for(t, dim):
        return [float((sum(ord(c) for c in t) % 97) + 1)] * dim

    calls = []

    def fake_embed(texts, c, **kw):
        calls.append(list(texts))
        if any(t == "FAIL" for t in texts):
            return None
        return [vec_for(t, c["dim"]) for t in texts]

    o_embed, o_dd, o_flush = embeddings.embed, es.worlds.derived_dir, es._EMBED_FLUSH_CHUNKS
    embeddings.embed = fake_embed
    tmp = tempfile.mkdtemp()
    es.worlds.derived_dir = lambda w: pathlib.Path(tmp) / w
    ec = {"provider": "p", "model": "m", "dim": 4}
    try:
        # フラッシュ単位=2件・distinct 4件（a,b,c,d）→ 2バッチに分かれる
        es._EMBED_FLUSH_CHUNKS = 2
        w = "embcache_stream_test"
        cache_dir = pathlib.Path(tmp) / w / "semantic" / "embed_cache"
        vecs, reused, embedded = es._embed_cached(w, ["a", "b", "c", "d"], ec)
        assert (reused, embedded) == (0, 4)
        assert [len(c) for c in calls] == [2, 2]                          # 2件ずつ2バッチで embed が呼ばれた
        assert vecs == [vec_for(t, 4) for t in ["a", "b", "c", "d"]]      # (d) 順序・値が従来（単発呼び）と同一
        assert _all_cache_keys(cache_dir) == {es._chunk_key(ec, t) for t in "abcd"}   # フラッシュ済み
        # (c) 剪定は呼び出し元が明示的に行う（`index_world` が Pass1 完了後に1回だけ呼ぶのと同じ契約）。
        es._prune_embed_cache(w, {es._chunk_key(ec, "a")})
        assert _all_cache_keys(cache_dir) == {es._chunk_key(ec, "a")}     # 現存分（a だけ）に縮む

        # (a) 途中バッチ失敗: need=[a,b,c,d,FAIL]（flush=2）→ [a,b]・[c,d] は成功して flush 済み、
        # 最後の [FAIL] だけが失敗する。成功済みバッチの分はキャッシュに残る。
        calls.clear()
        w2 = "embcache_stream_fail_test"
        cache_dir2 = pathlib.Path(tmp) / w2 / "semantic" / "embed_cache"
        vecs, reused, embedded = es._embed_cached(w2, ["a", "b", "c", "d", "FAIL"], ec)
        assert (vecs, reused, embedded) == (None, 0, 0)                   # 呼び出し全体としては失敗＝BM25 縮退
        assert [len(c) for c in calls] == [2, 2, 1]
        kept = _all_cache_keys(cache_dir2)
        assert kept == {es._chunk_key(ec, t) for t in "abcd"}             # a,b,c,d は flush 済みのまま残る
        assert es._chunk_key(ec, "FAIL") not in kept                      # 失敗したバッチ分は入らない

        # (b) 再実行: 残った a,b,c,d はキャッシュヒット（reused）、FAIL を外した新規 e だけ再 embed される
        calls.clear()
        vecs, reused, embedded = es._embed_cached(w2, ["a", "b", "c", "d", "e"], ec)
        assert (reused, embedded) == (4, 1)
        assert calls == [["e"]]

        # (e) フラッシュ単位の境界値: 件数がちょうど flush 単位に収まる場合は1バッチだけで済む
        calls.clear()
        es._EMBED_FLUSH_CHUNKS = 3
        w3 = "embcache_stream_boundary_test"
        vecs, reused, embedded = es._embed_cached(w3, ["x", "y", "z"], ec)
        assert (reused, embedded) == (0, 3) and len(calls) == 1 and len(calls[0]) == 3
    finally:
        embeddings.embed, es.worlds.derived_dir, es._EMBED_FLUSH_CHUNKS = o_embed, o_dd, o_flush
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
