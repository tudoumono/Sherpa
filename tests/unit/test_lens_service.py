"""lens_service の Neo4j 安全弁（緊急天井・per-query タイムアウト・縮退）と近傍/QA の単体テスト。

実 Neo4j は使わず fake session で検証する（`resolve_anchor`/`neo4j_related` が `session.run` を呼ぶ唯一の
2 箇所・共通の安全弁は `_run_capped`）。

- 緊急天井: 上限超の行は `_NEO4J_MAX_ROWS` で打ち切り＋`log.warning`（LIMIT は入れない）。打ち切り後は
  `Result.consume()` を呼ぶ（未消費のまま残すと次クエリで残りを全件バッファする）。
- タイムアウト由来の `Neo4jError` は空へ縮退＋warning、それ以外は再送出する。
"""
from __future__ import annotations

import json
import logging

import neo4j as neo4j_mod
import pytest
from neo4j.exceptions import Neo4jError, ServiceUnavailable

import _fresh_import as FI   # noqa: E402   # import-time 固定 env 定数の実プロセス検証
from sherpa import lens_service as ls
from sherpa.ingest import world_neo4j


class _FakeRecord:
    def __init__(self, d):
        self._d = d

    def data(self):
        return dict(self._d)


class _FakeResult:
    """`consumed` は `consume()`／`.data()`（残りを一括取得）で立つ。"""

    def __init__(self, rows):
        self._rows = rows
        self.consumed = False

    def __iter__(self):
        return iter(_FakeRecord(r) for r in self._rows)

    def consume(self):
        self.consumed = True

    def data(self):
        self.consumed = True
        return [dict(r) for r in self._rows]


class _FakeSession:
    def __init__(self, rows=None, raise_exc=None):
        self._rows = rows or []
        self._raise_exc = raise_exc
        self.calls: list[tuple] = []
        self.last_result: _FakeResult | None = None

    def run(self, query, **params):
        self.calls.append((query, params))
        if self._raise_exc is not None:
            raise self._raise_exc
        self.last_result = _FakeResult(self._rows)
        return self.last_result


def _related_row(cid, name, label="Module"):
    return {"cid": cid, "name": name, "label": label, "status": "active",
            "path_names": ["ROOT", name], "edges": [{"type": "USES", "doc": "a.md"}], "dist": 1}


def _timeout_error():
    return Neo4jError._hydrate_neo4j(
        code="Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration", message="timed out")


def _other_error():
    return Neo4jError._hydrate_neo4j(code="Neo.ClientError.Statement.SyntaxError", message="bad cypher")


def _patch_driver(monkeypatch, session):
    """`GraphDatabase.driver(...).session()` が `session` を返すよう差し替える。"""
    class _Sess:
        def __enter__(self):
            return session

        def __exit__(self, *a):
            return False

    class _Driver:
        def session(self):
            return _Sess()

        def close(self):
            pass

    monkeypatch.setattr(neo4j_mod.GraphDatabase, "driver", lambda uri, auth: _Driver())
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})


# ---- 緊急天井 ---------------------------------------------------------------------------------

def _call_related(s):
    return ls.neo4j_related(s, ["root"], "w1")


def _call_resolve(s):
    return ls.resolve_anchor(s, "text containing node token", "w1")


def _rows_related(n):
    return [_related_row(f"cid:{i}", "NODE") for i in range(n)]


def _rows_resolve(n):
    # 全行が同じ小文字名を持ち text にもその語を含む＝フィルタ後も件数が変わらない。
    return [{"cid": f"cid:{i}", "name": "node"} for i in range(n)]


@pytest.mark.parametrize("call, make_rows", [(_call_related, _rows_related), (_call_resolve, _rows_resolve)])
def test_caps_rows_warns_and_consumes_result(call, make_rows, caplog):
    s = _FakeSession(rows=make_rows(ls._NEO4J_MAX_ROWS + 5))
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = call(s)
    assert len(out) == ls._NEO4J_MAX_ROWS               # 収集済み分はそのまま返す
    assert any("緊急天井" in r.getMessage() and "w1" in r.getMessage() for r in caplog.records)
    assert s.last_result is not None and s.last_result.consumed is True


def test_cap_not_triggered_under_limit(caplog):
    s = _FakeSession(rows=_rows_related(3))
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = _call_related(s)
    assert len(out) == 3
    assert not any("緊急天井" in r.getMessage() for r in caplog.records)


# ---- per-query タイムアウトの引き渡し・縮退 ----------------------------------------------------

def test_neo4j_related_passes_query_timeout_and_directed_edges_cypher():
    s = _FakeSession(rows=[_related_row("c1", "NODE")])
    _call_related(s)
    query, params = s.calls[0]
    assert query.timeout == ls._NEO4J_QUERY_TIMEOUT_S
    assert params["world"] == "w1" and params["anchors"] == ["root"]
    # 探索は無向のまま、代表経路の edges は各辺の実際の向き（from/to）を持ち帰る。
    assert "startNode(e).name" in str(query)
    assert "endNode(e).name" in str(query)


def test_resolve_anchor_passes_query_timeout():
    s = _FakeSession(rows=[{"cid": "c1", "name": "node"}])
    ls.resolve_anchor(s, "node text", "w1")
    query, params = s.calls[0]
    assert query.timeout == ls._NEO4J_QUERY_TIMEOUT_S
    assert params["w"] == "w1"


def test_neo4j_related_main_query_degrades_but_era_probe_failure_still_propagates(caplog):
    # 主クエリのタイムアウトは空へ縮退するが、直後の世代プローブが同じ理由で失敗したら re-raise する
    # （Neo4j 全体の不調を「近傍0件」という誤った安心にしない）。両方のログが残る。
    s = _FakeSession(raise_exc=_timeout_error())
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        with pytest.raises(Neo4jError):
            _call_related(s)
    assert any("タイムアウト" in r.getMessage() and "w1" in r.getMessage() for r in caplog.records)
    assert any("世代プローブ" in r.getMessage() for r in caplog.records)


def test_resolve_anchor_degrades_to_empty_on_timeout(caplog):
    s = _FakeSession(raise_exc=_timeout_error())
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = ls.resolve_anchor(s, "symptom text", "w1")
    assert out == []
    assert any("タイムアウト" in r.getMessage() for r in caplog.records)


def test_non_timeout_neo4j_error_is_not_swallowed():
    with pytest.raises(Neo4jError):
        _call_related(_FakeSession(raise_exc=_other_error()))


def test_is_query_timeout_matches_known_code_shapes():
    assert ls._is_query_timeout(_timeout_error())
    assert not ls._is_query_timeout(_other_error())


# ---- 返却形状 ---------------------------------------------------------------------------------

def test_neo4j_related_shape_unchanged():
    out = ls.neo4j_related(_FakeSession(rows=[_related_row("cid:1", "TAXCALC")]), ["root"], "w1")
    assert out == [{
        "cid": "cid:1", "name": "TAXCALC", "label": "Module",
        "category": ls.CATEGORY.get("Module", "Module"),
        "status": "active",
        "path": ["ROOT", "TAXCALC"], "distance": 1,
        "edges": [{"type": "USES", "doc": "a.md"}],
    }]


def test_resolve_anchor_shape_unchanged():
    out = ls.resolve_anchor(_FakeSession(rows=[{"cid": "cid:1", "name": "TAXCALC"}]), "TAXCALC が ABEND", "w1")
    assert out == [("cid:1", "TAXCALC")]


def test_neo4j_related_empty_anchors_still_checks_schema_era():
    # anchors が空でも近傍探索の主クエリは省略しつつ世代プローブは省略しない
    # （旧世代グラフを「近傍0件」という平常の結果と区別できなくなるため）。
    s = _FakeSession(rows=[{"c": 0, "era": None}])
    assert ls.neo4j_related(s, [], "w1") == []
    assert len(s.calls) == 1
    assert "LIMIT 1" in s.calls[0][0]   # 呼ばれたのは era プローブ


# ---- cid（canonical_id）は内部専用 -------------------------------

def test_troubleshoot_cards_internal_helper_carries_cid(monkeypatch):
    monkeypatch.setattr(ls, "grep_search", lambda *a, **k: [])
    s = _FakeSession(rows=[_related_row("module:w1:a/b#TAXCALC", "TAXCALC")])
    anchor_names, cards, truncated_docs = ls._troubleshoot_cards(s, "TAXCALC の ABEND", "w1")
    assert anchor_names == {"TAXCALC"}
    assert len(cards) == 1
    assert cards[0]["cid"] == "module:w1:a/b#TAXCALC"
    assert truncated_docs == []


def test_neighbor_cards_agentic_path_preserves_cid(monkeypatch):
    fake_cards = [{"name": "TAXCALC", "label": "Module", "cid": "module:w1:a/b#TAXCALC",
                   "category": "プログラム", "role": "実装", "distance": 1, "path": [],
                   "source": "graph", "evidence": {"edges": [], "grep": []}}]
    monkeypatch.setattr(ls, "_troubleshoot_cards",
                        lambda session, term, world, scope_paths=None, **kw: ({"TAXCALC"}, ls._Cards(fake_cards, ls.Coverage()), []))
    monkeypatch.setattr(ls, "read_unresolved", lambda *a, **k: {"available": False, "items": [], "omitted": 0})
    _patch_driver(monkeypatch, object())
    cards = ls.neighbor_cards("w1", "TAXCALC の ABEND")
    assert cards and cards[0]["cid"] == "module:w1:a/b#TAXCALC"


def test_neighbor_cards_graph_only_skips_grep_and_returns_graph_rows(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("neighbor_cards_graph_only が grep_search を呼んでいる")

    monkeypatch.setattr(ls, "grep_search", _boom)
    _patch_driver(monkeypatch, _FakeSession(rows=[_related_row("module:w1:a/b#TAXCALC", "TAXCALC")]))
    assert ls.neighbor_cards_graph_only("w1", "TAXCALC") == [{
        "name": "TAXCALC", "label": "Module", "category": "ソース", "role": "実装",
        "distance": 1, "path": ["ROOT", "TAXCALC"], "source": "graph",
        "evidence": {"edges": [{"type": "USES", "doc": "a.md"}], "grep": []},
        "cid": "module:w1:a/b#TAXCALC",
    }]


def test_neighbor_cards_recoverable_failure_returns_error_coded_empty_list(monkeypatch):
    class _Driver:
        def session(self):
            raise ServiceUnavailable("boom")

        def close(self):
            pass

    monkeypatch.setattr(neo4j_mod.GraphDatabase, "driver", lambda uri, auth: _Driver())
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})
    cards = ls.neighbor_cards("w1", "TAXCALC の ABEND")
    assert cards == []
    assert getattr(cards, "error_code", None) == "graph_unavailable"


def test_neighbor_cards_non_recoverable_failure_returns_error_coded_empty_list(monkeypatch):
    monkeypatch.setattr(ls, "_troubleshoot_cards",
                        lambda session, term, world, scope_paths=None: (_ for _ in ()).throw(TypeError("bug")))
    _patch_driver(monkeypatch, object())
    cards = ls.neighbor_cards("w1", "TAXCALC の ABEND")
    assert cards == []
    assert getattr(cards, "error_code", None) == "graph_internal_error"


def test_module_defaults_are_clamped_into_range():
    assert 1 <= ls._NEO4J_QUERY_TIMEOUT_S <= 600
    assert 100 <= ls._NEO4J_MAX_ROWS <= 1_000_000


def test_troubleshoot_graph_depth_default_is_shared_by_signatures_and_ignores_env():
    # 既定は 4。env `SHERPA_TROUBLESHOOT_GRAPH_DEPTH` は実行時の値に影響しない（画面だけが正）。
    script = (
        "import inspect, json\n"
        "import sherpa.lens_service as m\n"
        "print(json.dumps([m.TROUBLESHOOT_GRAPH_DEPTH] + [\n"
        "    inspect.signature(f).parameters['depth'].default\n"
        "    for f in (m.neo4j_related, m._troubleshoot_cards)]))\n"
    )
    assert json.loads(FI.run_script(script, env={"SHERPA_TROUBLESHOOT_GRAPH_DEPTH": "5"})) == [4, 4, 4]




# ---- grep 打切りの申告（`truncated_docs`） -------------------------------------------



def test_troubleshoot_cards_reuses_same_truncated_docs_list_across_anchor_calls(monkeypatch):
    seen_ids = []

    def fake_grep(nm, world, scope_paths=None, truncated_docs=None):
        seen_ids.append(id(truncated_docs))
        return []

    monkeypatch.setattr(ls, "grep_search", fake_grep)
    s = _FakeSession(rows=[
        _related_row("module:w1:a/b#TAXCALC", "TAXCALC"),
        _related_row("module:w1:a/b#TAXCALC2", "TAXCALC2"),
    ])
    ls._troubleshoot_cards(s, "TAXCALC TAXCALC2 の ABEND", "w1")
    assert len(seen_ids) == 2 and len(set(seen_ids)) == 1
