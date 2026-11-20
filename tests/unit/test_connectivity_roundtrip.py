"""つながりの確認の「書く → 引く → 消す」（`connectivity_roundtrip`）。外部の境界（ES の HTTP・Neo4j のドライバ・埋め込みの送信）だけ差し替える。
成功時も途中で失敗したときも確認専用の索引・ノードを残さず、資料の索引・`Entity` に触れないことを確かめる。
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from sherpa import connectivity, connectivity_roundtrip as rt, embeddings

_KB_INDEX = "sherpa-kb-sample-0123456789"


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


class _FakeEs:
    """索引名 -> {"mapping", "docs"} だけを持つ ES。`fail` は (メソッド, パスの末尾) -> 失敗させる。触れたパスは `touched` に残す。"""

    def __init__(self):
        self.indices = {_KB_INDEX: {"mapping": {}, "docs": {"keep": {"text": "資料の本文"}}}}
        self.touched, self.fail, self.mappings = [], set(), []

    def urlopen(self, req, timeout=None):
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        parts = path.split("?")[0].split("/")
        name, rest = parts[0], parts[1:]
        self.touched.append((req.get_method(), path))
        if any(m == req.get_method() and path.split("?")[0].endswith(tail) for m, tail in self.fail):
            raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)
        body = json.loads(req.data) if req.data else None
        if req.get_method() == "DELETE":
            if name not in self.indices:
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
            del self.indices[name]
            return _Resp({})
        if req.get_method() == "PUT" and not rest:
            self.indices[name] = {"mapping": body, "docs": {}}
            self.mappings.append(body)
            return _Resp({})
        if req.get_method() == "PUT":
            self.indices[name]["docs"][rest[1]] = body
            return _Resp({})
        docs = self.indices[name]["docs"]
        if "knn" in body:
            return _Resp({"hits": {"hits": [{"_id": i} for i in docs if "embedding" in docs[i]]}})
        word = body["query"]["match"]["text"]
        return _Resp({"hits": {"hits": [{"_id": i} for i, d in docs.items() if word in d.get("text", "")]}})

    def check_indices(self):
        return sorted(n for n in self.indices if n.startswith(rt.ES_PREFIX))


@pytest.fixture
def es(monkeypatch):
    fake = _FakeEs()
    monkeypatch.setenv("ES_URL", "http://localhost:9200")
    monkeypatch.setattr("urllib.request.urlopen", fake.urlopen)
    return fake


class _Ctx:
    def __init__(self, path="ingest"):
        self.path = path

    def need(self):
        return {}


def test_text_roundtrip_leaves_no_check_index_and_never_touches_the_kb_index(es):
    es.indices[rt.index_name("ingest", "text")] = {"mapping": {}, "docs": {"old": {}}}   # 前回の残り
    out = rt.check_es(_Ctx(), False)
    assert out["host"] == "localhost"
    assert es.check_indices() == []
    assert es.indices[_KB_INDEX]["docs"] == {"keep": {"text": "資料の本文"}}
    assert all(not p.startswith(_KB_INDEX) and not p.startswith("sherpa-kb") for _m, p in es.touched)
    assert es.mappings[0]["mappings"]["properties"]["text"]["analyzer"] == "kuromoji"


@pytest.mark.parametrize("stage, tail", [("索引を作る", "sherpa-check-ingest-text"), ("書く", "_doc/check?refresh=true"),
                                         ("引く", "_search")])
def test_text_roundtrip_failure_midway_still_removes_the_check_index(es, stage, tail):
    es.fail = {("PUT", tail.split("?")[0])} if stage != "引く" else {("POST", tail)}
    with pytest.raises(connectivity._Ng) as e:
        rt.check_es(_Ctx(), False)
    assert "書けなかった" in e.value.reason or "引けなかった" in e.value.reason or "用意できなかった" in e.value.reason
    assert es.check_indices() == []
    assert _KB_INDEX in es.indices


def test_roundtrip_fails_when_the_written_sentence_cannot_be_found(es, monkeypatch):
    orig = es.urlopen

    def blind(req, timeout=None):
        if req.get_method() == "POST" and req.full_url.endswith("/_search"):
            return _Resp({"hits": {"hits": []}})
        return orig(req, timeout)
    monkeypatch.setattr("urllib.request.urlopen", blind)
    with pytest.raises(connectivity._Ng, match="引けなかった"):
        rt.check_es(_Ctx(), False)
    assert es.check_indices() == []


def test_a_failed_cleanup_is_reported_not_hidden(es):
    es.fail = {("DELETE", "sherpa-check-ingest-text")}
    with pytest.raises(connectivity._Ng, match="消せなかった"):
        rt.check_es(_Ctx(), False)


def test_other_paths_checks_do_not_remove_each_others_data(es):
    es.indices[rt.index_name("api", "text")] = {"mapping": {}, "docs": {}}
    rt.check_es(_Ctx("ingest"), False)
    assert es.check_indices() == [rt.index_name("api", "text")]


def _embed_cfg(provider="ollama", dim=4):
    return {"provider": provider, "url": "http://localhost:11434", "model": "embed-sample", "dim": dim, "key": "sk-sample-key"}


def test_vector_roundtrip_builds_the_index_with_the_embedding_dimension_and_removes_it(es, monkeypatch):
    monkeypatch.setattr(embeddings, "cfg", lambda *a, **k: _embed_cfg())
    monkeypatch.setattr(embeddings, "embed_with_failure", lambda texts, c, **k: ([[0.1, 0.2, 0.3, 0.4]], None))
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    out = rt.check_vector(_Ctx(), False)
    assert out["paid"] is False and "4次元" in out["note"]
    assert es.mappings[0]["mappings"]["properties"]["embedding"]["dims"] == 4
    assert es.check_indices() == [] and _KB_INDEX in es.indices


def test_vector_roundtrip_with_openai_embedding_is_paid_and_skipped_without_the_paid_flag(es, monkeypatch):
    monkeypatch.setattr(embeddings, "cfg", lambda *a, **k: _embed_cfg("openai", 4))
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    sent = []
    monkeypatch.setattr(embeddings, "embed_with_failure", lambda *a, **k: sent.append(1) or ([[0.1, 0.2, 0.3, 0.4]], None))
    with pytest.raises(connectivity._Skip) as e:
        rt.check_vector(_Ctx(), False)
    assert e.value.paid is True and sent == [] and es.touched == []
    assert rt.check_vector(_Ctx(), True)["paid"] is True and sent == [1]


def test_vector_roundtrip_without_embedding_is_skipped(es, monkeypatch):
    monkeypatch.setattr(embeddings, "cfg", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "cloud_selected_but_unavailable", lambda s=None: False)
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    with pytest.raises(connectivity._Skip):
        rt.check_vector(_Ctx(), False)
    assert es.touched == []


class _FakeNeo4j:
    """ノードを (ラベル, 属性) の組で持つ。資料の `Entity` を 1 件置き、確認の問い合わせがそれに触れないことを見る。"""

    def __init__(self):
        self.nodes = [("Entity", {"world_id": "sample", "name": "資料のノード"})]
        self.queries, self.fail_on = [], None

    def check_nodes(self):
        return [n for n in self.nodes if n[0] == rt.GRAPH_LABEL]

    def driver(self, uri, auth=None, **kw):
        outer = self

        class _Session:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def run(s, query, **params):
                outer.queries.append(query)
                if outer.fail_on and outer.fail_on in query:
                    raise OSError("boom")
                result = type("R", (), {"consume": lambda self: None, "single": lambda self: None})()
                if "DETACH DELETE" in query:
                    outer.nodes = [n for n in outer.nodes if not (n[0] == rt.GRAPH_LABEL and n[1]["path"] == params["p"])]
                elif query.startswith("CREATE"):
                    outer.nodes.append((rt.GRAPH_LABEL, {"path": params["p"], "token": params["t"]}))
                else:
                    row = next((n[1] for n in outer.nodes if n[0] == rt.GRAPH_LABEL and n[1]["token"] == params["t"]), None)
                    result.single = lambda: {"t": row["token"]} if row else None
                return result

        class _Driver:
            def __enter__(d):
                return d

            def __exit__(d, *a):
                return False

            def session(d):
                return _Session()
        return _Driver()


@pytest.fixture
def neo(monkeypatch):
    fake = _FakeNeo4j()
    monkeypatch.setattr("neo4j.GraphDatabase.driver", fake.driver)
    return fake


def test_graph_roundtrip_writes_reads_and_removes_without_touching_entities(neo):
    neo.nodes.append((rt.GRAPH_LABEL, {"path": "ingest", "token": "old"}))   # 前回の残り
    assert rt.check_graph(_Ctx(), False)["route"]
    assert neo.check_nodes() == []
    assert neo.nodes == [("Entity", {"world_id": "sample", "name": "資料のノード"})]
    assert all("Entity" not in q and "SherpaMeta" not in q and "world_id" not in q for q in neo.queries)


@pytest.mark.parametrize("fail_on", ["CREATE", "RETURN n.token"])
def test_graph_roundtrip_failure_midway_still_removes_the_check_node(neo, fail_on):
    neo.fail_on = fail_on
    with pytest.raises(connectivity._Ng):
        rt.check_graph(_Ctx(), False)
    assert neo.check_nodes() == []
    assert len(neo.nodes) == 1


def test_only_the_ingest_path_runs_the_roundtrip_checks():
    ids = {"store_roundtrip_es", "store_roundtrip_vector", "store_roundtrip_graph"}
    assert ids <= set(connectivity._PATH_CHECKS["ingest"])
    for path in ("api", "mcp", "codex"):
        assert not ids & set(connectivity._PATH_CHECKS[path])
    assert "ollama" not in {c[0] for c in connectivity._CHECKS}


def test_check_index_name_is_not_picked_up_as_a_kb_index():
    """`list_kb_indices` は `sherpa-kb-*` だけを列挙する（孤児の判定もこれに従う）。"""
    from fnmatch import fnmatch
    assert not fnmatch(rt.index_name("ingest", "vector"), "sherpa-kb-*")
