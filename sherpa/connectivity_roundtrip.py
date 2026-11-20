"""つながりの確認の「書く → 引く → 消す」。確認専用の索引・ラベルだけに 1 件書き、引けることを見て消す。
資料の索引（`sherpa-kb-*`）・グラフ（`Entity`・`SherpaMeta`・`world_id`）・台帳には触れない。
索引とラベルの名前に経路名を含め、別の経路の確認と互いのデータを消し合わない。
設計: docs/design/operations.md「システム状態とつながりの確認」
"""
from __future__ import annotations

import urllib.error
import uuid

from .connectivity_base import _DIRECT, _Ng, _Skip, _embed_failure_reason, classify, host_of, resolve_embed, route_for

ES_PREFIX = "sherpa-check"
GRAPH_LABEL = "SherpaCheck"
_TIMEOUT = 5
_EMBED_DEADLINE_S = 15
_DOC_ID = "check"
_TEXT = "接続確認 書いて引けるかの確認用の文"
_TEXT_QUERY = "接続確認"


def index_name(path: str, kind: str) -> str:
    """確認専用の索引名。`sherpa-kb-*` に拾われない接頭辞に、経路名と種類を付ける。"""
    return f"{ES_PREFIX}-{path}-{kind}"


def _es_delete(name: str) -> None:
    from . import es_index
    try:
        es_index._req("DELETE", "/" + name, timeout=_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise


def _es_cycle(path: str, kind: str, mapping: dict, doc: dict, query: dict, *, note: str | None = None,
              paid: bool = False) -> dict:
    """① 前回の残りを消す ② 確認専用の索引を作る ③ 1 件書く ④ 引く ⑤ 索引ごと消す。⑤ は途中で失敗しても必ず試みる。"""
    from . import es_index
    url = es_index._url()
    route, host = route_for(url, "http"), host_of(url)
    name = index_name(path, kind)
    stage, err, found = "確認専用の索引を用意できなかった", None, False
    try:
        _es_delete(name)
        es_index._req("PUT", "/" + name, mapping, timeout=_TIMEOUT)
        stage = "書けなかった"
        es_index._req("PUT", f"/{name}/_doc/{_DOC_ID}?refresh=true", doc, timeout=_TIMEOUT)
        stage = "引けなかった"
        res = es_index._req("POST", f"/{name}/_search", query, timeout=_TIMEOUT)
        found = any(h.get("_id") == _DOC_ID for h in ((res.get("hits") or {}).get("hits") or []))
    except Exception as e:
        err = _Ng(f"{stage}（{classify(e)}）", route=route, host=host, paid=paid)
    finally:
        try:
            _es_delete(name)
            leftover = False
        except Exception:
            leftover = True
    if leftover:
        left = "確認専用の索引を消せなかった（残っている可能性）"
        raise _Ng(f"{err.reason}・{left}" if err is not None else left, route=route, host=host, paid=paid)
    if err is not None:
        raise err
    if not found:
        raise _Ng("書いた文を引けなかった", route=route, host=host, paid=paid)
    out = {"route": route, "host": host, "paid": paid}
    if note:
        out["note"] = note
    return out


def check_es(ctx, paid: bool) -> dict:
    """語の一致の検索: 確認専用の索引（本物と同じ日本語の解析）に 1 文を書き、語で引く。"""
    from . import es_index
    return _es_cycle(ctx.path, "text", es_index._mapping(None, "kuromoji"),
                     {"doc_id": _DOC_ID, "text": _TEXT},
                     {"query": {"match": {"text": _TEXT_QUERY}}, "size": 1})


def check_vector(ctx, paid: bool) -> dict:
    """意味での検索: 今の埋め込みの設定で 1 文をベクトルにし、その次元の確認専用の索引に書いて kNN で引く。OpenAI の埋め込みは有料。"""
    from . import embeddings, es_index
    sys_s = ctx.need()
    ec = resolve_embed(sys_s).cfg
    provider = ec.get("provider")
    is_paid = provider == "openai"
    url = es_index._url()
    if is_paid and not paid:
        raise _Skip("有料のため流していない", paid=True, route=route_for(url, "http"), host=host_of(url))
    deadline_s = max(_EMBED_DEADLINE_S, embeddings.request_timeout(ec))
    note = f"{provider} / {ec.get('model')}"
    if ec.get("dim") is None:
        vecs, failure = embeddings.reach_with_failure([_TEXT], ec, deadline_s=deadline_s)
    else:
        vecs, failure = embeddings.embed_with_failure([_TEXT], ec, deadline_s=deadline_s)
    if not vecs or not hasattr(vecs[0], "__len__") or not len(vecs[0]):
        raise _Ng("文をベクトルにできなかった: " + _embed_failure_reason(failure, ec.get("dim")),
                  route=route_for(url, "http"), host=host_of(url), paid=is_paid)
    dim = len(vecs[0])
    qb = embeddings.vector_to_es_bytes(vecs[0])
    if qb is None:
        raise _Ng("応答のベクトルが正しくない（数でない値・すべて 0 など）",
                  route=route_for(url, "http"), host=host_of(url), paid=is_paid)
    return _es_cycle(ctx.path, "vector", es_index._mapping(dim, "kuromoji"),
                     {"doc_id": _DOC_ID, "text": _TEXT, "embedding": qb},
                     {"knn": {"field": "embedding", "query_vector": qb, "k": 1, "num_candidates": 10}, "size": 1},
                     note=f"{note} / {dim}次元", paid=is_paid)


def check_graph(ctx, paid: bool) -> dict:
    """グラフ: 確認専用のラベルのノード 1 件を書き、読み、消す。"""
    from neo4j import GraphDatabase

    from .ingest import world_neo4j
    env = world_neo4j._env()
    host = host_of(env["uri"])
    token = uuid.uuid4().hex
    drop = f"MATCH (n:{GRAPH_LABEL} {{path:$p}}) DETACH DELETE n"
    stage, err, found, leftover = "つながらない", None, False, False
    try:
        with GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]),
                                  connection_timeout=_TIMEOUT,
                                  connection_acquisition_timeout=_TIMEOUT) as driver:
            with driver.session() as session:
                try:
                    stage = "前回の残りを片付けられなかった"
                    session.run(drop, p=ctx.path).consume()
                    stage = "書けなかった"
                    session.run(f"CREATE (n:{GRAPH_LABEL} {{path:$p, token:$t}})", p=ctx.path, t=token).consume()
                    stage = "読めなかった"
                    row = session.run(f"MATCH (n:{GRAPH_LABEL} {{path:$p, token:$t}}) RETURN n.token AS t",
                                      p=ctx.path, t=token).single()
                    found = bool(row) and row["t"] == token
                finally:
                    try:
                        session.run(drop, p=ctx.path).consume()
                    except Exception:
                        leftover = True
    except Exception as e:
        err = _Ng(f"{stage}（{classify(e)}）", route=_DIRECT, host=host)
    if leftover:
        left = "確認専用のノードを消せなかった（残っている可能性）"
        raise _Ng(f"{err.reason}・{left}" if err is not None else left, route=_DIRECT, host=host)
    if err is not None:
        raise err
    if not found:
        raise _Ng("書いたノードを読めなかった", route=_DIRECT, host=host)
    return {"route": _DIRECT, "host": host}
