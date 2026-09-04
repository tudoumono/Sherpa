"""影響分析（world_impact/resolve_world_entity）の Neo4j 安全弁（secRV 範囲外是正・2026-07-19）の単体テスト。

直前に `sherpa/lens_service.py`（近傍探索・補助情報）へ実装した安全弁（`_run_capped`・タイムアウト/
緊急天井到達を空リストへ縮退）と**縮退の意味が逆**: 影響分析（本モジュール・`_run_read_capped`）は
「空＝影響なし」と誤読される偽陰性を避けるため、timeout・天井到達のどちらも
`GraphQueryOverloadError` を **raise** する（部分結果や空を黙って返さない・fail-loud）。

実 Neo4j は使わず fake session で検証する（`tests/unit/test_lens_service.py` のストリーム反復版
フェイクパターンを踏襲・複数回の `session.run` 呼び出しに対応できるようキュー方式に拡張）。

secRV 範囲外是正 追補（2026-07-19・RV指摘 HIGH-1）: 天井到達で raise する前に `Result.consume()`
が呼ばれる（未消費の Result を残すと driver 6.2.0 が同一 session の次クエリで残りを全件バッファ
してしまう）。
"""
from __future__ import annotations

import logging

import pytest
from neo4j.exceptions import Neo4jError

import _fresh_import as FI   # noqa: E402   # import-time 固定 env 定数の実プロセス検証
from sherpa.ingest import world_neo4j as wn


class _FakeRecord:
    """`neo4j.Record` の最小スタブ（`.data()` のみ使う）。"""

    def __init__(self, d):
        self._d = d

    def data(self):
        return dict(self._d)


class _FakeResult:
    """`neo4j.Result` の最小スタブ（for 反復が主・`check_schema_era`（rv-s3-removal）向けに
    `.data()` 一括展開も持つ）。

    `consumed`（HIGH-1・secRV 範囲外是正 追補・2026-07-19）: `consume()` 呼び出しを記録する。
    `.data()` も実 driver と同じく「残りを一括取得＝実質フルに消費する」ため consumed を立てる。
    """

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
    """`_run_read_capped` 経由の関数向け最小 session スタブ（実 Neo4j 不要）。

    `resolve_world_entity`／`world_impact` はいずれも `session.run` を1回だけ呼ぶ（K10＝REALIZES
    橋の2段目クエリ撤去・K12＝静的判定の二重クエリ撤去）。`responses` に呼び出し順のキュー
    （rows のリスト、または送出したい例外インスタンス）を渡す。キューが呼び出し回数より短ければ
    最後の要素を使い回す（想定より多く呼ばれても落ちない）。
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple] = []   # [(query, params), ...]（timeout/params 検証用に記録）
        self.last_result: _FakeResult | None = None   # HIGH-1: consume() 検証用に直近の Result を保持

    def run(self, query, **params):
        self.calls.append((query, params))
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        resp = self._responses[idx]
        if isinstance(resp, BaseException):
            raise resp
        self.last_result = _FakeResult(resp)
        return self.last_result


def _timeout_error():
    """実サーバが返す形式を模した Neo4jError（`Neo4jError._hydrate_neo4j` で生成・実物と同じ経路）。"""
    return Neo4jError._hydrate_neo4j(
        code="Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration",
        message="timed out")


def _other_error():
    """タイムアウトではない Neo4jError（Cypher バグ等・握り潰してはいけない）。"""
    return Neo4jError._hydrate_neo4j(code="Neo.ClientError.Statement.SyntaxError", message="bad cypher")


def _impact_row(cid, name, label="Module", status="active", top="4期", path=None, analyzer=None):
    return {"cid": cid, "name": name, "label": label, "status": status,
            "dpath": path or f"{top}/{name}.cbl", "top": top, "analyzer": analyzer,
            "path_names": ["ORDER-LIMIT", name],
            "edges": [{"type": "INVOKES", "doc": f"{top}/design.md", "line": 3}]}


# ---- _run_read_capped: 緊急天井（fail-loud・lens_service と逆＝raise） ------

def test_run_read_capped_raises_on_row_cap(caplog):
    over = wn._NEO4J_MAX_ROWS + 5
    rows = [{"cid": f"c{i}"} for i in range(over)]
    s = _FakeSession([rows])
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        with pytest.raises(wn.GraphQueryOverloadError) as ei:
            wn._run_read_capped(s, "MATCH (n) RETURN n", world="w1")
    assert ei.value.reason == "too_many_rows"
    assert ei.value.world == "w1"
    assert ei.value.rows == wn._NEO4J_MAX_ROWS
    assert any("緊急天井" in r.getMessage() and "w1" in r.getMessage() for r in caplog.records)


def test_run_read_capped_not_triggered_under_limit():
    """行数が上限未満なら打ち切らず、正常に全件返す（正常系に影響しない）。"""
    rows = [{"cid": f"c{i}"} for i in range(3)]
    s = _FakeSession([rows])
    out = wn._run_read_capped(s, "MATCH (n) RETURN n", world="w1")
    assert out == rows


def test_run_read_capped_consumes_result_before_raising_on_row_cap():
    """HIGH-1（secRV 範囲外是正 追補・2026-07-19）: 天井到達で `GraphQueryOverloadError` を raise
    する前に `Result.consume()` を呼ぶ（未消費のまま raise すると、呼び出し元が同一 session で
    別クエリを流した際に driver 6.2.0 が残りを全件バッファしてしまう＝安全弁の逆流を防ぐ）。
    """
    over = wn._NEO4J_MAX_ROWS + 5
    rows = [{"cid": f"c{i}"} for i in range(over)]
    s = _FakeSession([rows])
    with pytest.raises(wn.GraphQueryOverloadError):
        wn._run_read_capped(s, "MATCH (n) RETURN n", world="w1")
    assert s.last_result is not None
    assert s.last_result.consumed is True


# ---- per-query タイムアウトの引き渡し ---------------------------------------

def test_run_read_capped_passes_query_timeout_and_world_param():
    s = _FakeSession([[{"cid": "c1"}]])
    wn._run_read_capped(s, "MATCH (n) RETURN n", world="w1", foo="bar")
    query, params = s.calls[0]
    assert query.timeout == wn._NEO4J_QUERY_TIMEOUT_S
    assert params["world"] == "w1" and params["foo"] == "bar"


# ---- タイムアウト時は raise（縮退しない・fail-loud） -------------------------

def test_run_read_capped_raises_on_timeout(caplog):
    s = _FakeSession([_timeout_error()])
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        with pytest.raises(wn.GraphQueryOverloadError) as ei:
            wn._run_read_capped(s, "MATCH (n) RETURN n", world="w1")
    assert ei.value.reason == "timeout"
    assert ei.value.world == "w1"
    assert any("タイムアウト" in r.getMessage() and "w1" in r.getMessage() for r in caplog.records)


def test_run_read_capped_non_timeout_error_is_not_swallowed():
    """タイムアウト以外のサーバエラー（Cypher バグ等）は変換も縮退もせず再送出する。"""
    s = _FakeSession([_other_error()])
    with pytest.raises(Neo4jError):
        wn._run_read_capped(s, "MATCH (n) RETURN n", world="w1")


def test_is_query_timeout_matches_known_code_shapes():
    assert wn._is_query_timeout(_timeout_error())
    assert not wn._is_query_timeout(_other_error())


# ---- env 検証（_env_int・agentic_search/lens_service と同一セマンティクス） --

def test_env_int_falls_back_on_invalid_values(monkeypatch):
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "-1")
    assert wn._env_int("SHERPA_TEST_LIMIT", 30, 1, 600) == 30
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "abc")
    assert wn._env_int("SHERPA_TEST_LIMIT", 30, 1, 600) == 30
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "60")
    assert wn._env_int("SHERPA_TEST_LIMIT", 30, 1, 600) == 60
    monkeypatch.delenv("SHERPA_TEST_LIMIT", raising=False)
    assert wn._env_int("SHERPA_TEST_LIMIT", 30, 1, 600) == 30


def test_module_defaults_are_clamped_into_range():
    assert 1 <= wn._NEO4J_QUERY_TIMEOUT_S <= 600
    assert 100 <= wn._NEO4J_MAX_ROWS <= 1_000_000


# ---- SHERPA_IMPACT_MAX_DEPTH ----
# import 時に一度だけ確定する定数は実プロセスを新規に起こして検証する（`_fresh_import`）。

def test_impact_max_depth_fresh_import_env_unset_is_default():
    assert FI.fresh_import_attr("sherpa.ingest.world_neo4j", "IMPACT_MAX_DEPTH",
                                env={"SHERPA_IMPACT_MAX_DEPTH": None}) == 8


def test_impact_max_depth_fresh_import_env_valid_value():
    assert FI.fresh_import_attr("sherpa.ingest.world_neo4j", "IMPACT_MAX_DEPTH",
                                env={"SHERPA_IMPACT_MAX_DEPTH": "12"}) == 12


def test_impact_max_depth_fresh_import_env_invalid_falls_back_to_default():
    for bad in ("0", "65", "abc"):
        assert FI.fresh_import_attr("sherpa.ingest.world_neo4j", "IMPACT_MAX_DEPTH",
                                    env={"SHERPA_IMPACT_MAX_DEPTH": bad}) == 8, bad


def test_impact_max_depth_env_change_after_import_has_no_effect(monkeypatch):
    before = wn.IMPACT_MAX_DEPTH
    monkeypatch.setenv("SHERPA_IMPACT_MAX_DEPTH", "40")
    assert wn.IMPACT_MAX_DEPTH == before == 8


def test_world_impact_default_depth_param_is_impact_max_depth():
    """`world_impact`/`run_world_impact` の `depth` 既定値は `IMPACT_MAX_DEPTH` に揃っている。

    既定値と異なる env（20）で fresh import して確認する＝既定値どうしが偶然一致するだけの
    「旧リテラル `depth=8` への退行」を検出できない自己言及を避ける。
    """
    env = {"SHERPA_IMPACT_MAX_DEPTH": "20"}
    assert FI.fresh_import_param_default(
        "sherpa.ingest.world_neo4j", "world_impact", "depth", env=env) == 20
    assert FI.fresh_import_param_default(
        "sherpa.ingest.world_neo4j", "run_world_impact", "depth", env=env) == 20


# ---- resolve_world_entity / world_impact: overload が fail-loud で伝播 ------

def test_resolve_world_entity_propagates_overload_on_timeout():
    s = _FakeSession([_timeout_error()])
    with pytest.raises(wn.GraphQueryOverloadError):
        wn.resolve_world_entity(s, "TAX-RATE", "w1")


def test_world_impact_propagates_overload_from_query():
    """K12（2026-09-04-グラフのソース正典化.md §4）で1本化された Cypher がタイムアウトしても
    静かに空へは縮退しない。"""
    s = _FakeSession([_timeout_error()])
    with pytest.raises(wn.GraphQueryOverloadError):
        wn.world_impact(s, ["cid:1"], "w1")


# ---- 正常系回帰: resolve_world_entity/world_impact の返却形状は不変 ---------

def test_resolve_world_entity_shape_unchanged():
    """K10（REALIZES 橋の撤去）以降、`resolve_world_entity` は名前一致の1クエリだけで業務ロジックが
    完結する（＝旧 REALIZES 橋の2段目クエリは復活していない）。RV是正（rv-periphery #9・
    2026-09-05）: 単独では `check_schema_era` の世代プローブをもう呼ばない（唯一の呼び出し元
    `run_world_impact` が直後に呼ぶ `world_impact` 側で1回だけ確認する・重複ラウンドトリップの
    解消）——`session.run` 呼び出し総数は1のまま。
    """
    rows = [{"cid": "dataitem:w1:D", "label": "DataItem", "name": "TAX-RATE"}]
    s = _FakeSession([rows])
    starts = wn.resolve_world_entity(s, "TAX-RATE", "w1")
    assert len(s.calls) == 1
    assert "MATCH (n:Entity {world_id:$w}) WHERE n.name=$name" in s.calls[0][0].text
    by = {x["canonical_id"]: x for x in starts}
    assert set(by) == {"dataitem:w1:D"}
    assert by["dataitem:w1:D"] == {"canonical_id": "dataitem:w1:D", "label": "DataItem", "name": "TAX-RATE"}


def test_run_world_impact_probes_schema_era_exactly_once():
    """RV是正（rv-periphery #9・2026-09-05）: `run_world_impact`（`resolve_world_entity` →
    `world_impact` の合成）は世代プローブを1回だけ実行する——旧実装は両関数がそれぞれ独立に
    プローブしており、同じ world に対して era プローブが2回連続で走っていた（重複ラウンド
    トリップ）。"""
    resolve_rows = [{"cid": "dataitem:w1:D", "label": "DataItem", "name": "TAX-RATE"}]
    impact_rows = [_impact_row("cid:a", "NODE-A")]
    era_rows = {"c": 1, "era": wn.GRAPH_SCHEMA_ERA}

    class _SeqSession(_FakeSession):
        def run(self, query, **params):
            self.calls.append((query, params))
            text = query.text if hasattr(query, "text") else query
            if "SherpaMeta" in text:
                return _FakeResult([era_rows])
            if "n.name=$name" in text:
                return _FakeResult(resolve_rows)
            return _FakeResult(impact_rows)

    s = _SeqSession([])
    wn.run_world_impact(s, "TAX-RATE", "w1")
    era_probe_calls = [c for c in s.calls if "SherpaMeta" in (c[0].text if hasattr(c[0], "text") else c[0])]
    assert len(era_probe_calls) == 1


def test_world_impact_shape_unchanged():
    """K12（判定表示の撤去）以降、items は judgement/extraction_method を持たない（全件同格）。
    影響たどり自体は1本の Cypher で完結する（二重クエリ撤去は復活していない）——rv-s3-removal で
    主クエリの後に `check_schema_era` の世代プローブが1回加わるため、呼び出し総数は2になる。
    """
    impact_rows = [_impact_row("cid:a", "NODE-A", analyzer="cobol"),
                  _impact_row("cid:b", "NODE-B")]     # analyzer 無し(None) の来歴も通る
    s = _FakeSession([impact_rows])
    items = wn.world_impact(s, ["start:1"], "w1")
    assert len(s.calls) == 2                          # K12: 影響たどりは1本の Cypher（+ era プローブ1本）
    by = {i["name"]: i for i in items}
    assert by["NODE-A"]["analyzer"] == "cobol"                        # 担当アナライザの来歴が通る
    assert by["NODE-B"]["analyzer"] is None
    for it in items:
        assert set(it) == {"name", "label", "category", "status",
                           "analyzer", "top_scope", "path", "trace", "evidence"}
        assert "judgement" not in it and "extraction_method" not in it
        assert it["trace"] == ["ORDER-LIMIT", it["name"]]
        assert it["evidence"] and it["evidence"][0]["doc"].endswith("design.md")


def test_world_impact_passes_query_timeout():
    s = _FakeSession([[]])
    wn.world_impact(s, ["start:1"], "w1")
    assert len(s.calls) == 2                        # 主クエリ（timeout 付き）+ era プローブ（rv-s3-removal）
    query, params = s.calls[0]
    assert query.timeout == wn._NEO4J_QUERY_TIMEOUT_S
    assert params["world"] == "w1" and params["starts"] == ["start:1"]


# ---- GRAPH-MEM（2026-09-04）: load_world の UNWIND バッチ化 -----------------
# `load_world` は `GraphDatabase.driver(...)` を関数内で遅延 import して直接呼ぶため、
# `neo4j.GraphDatabase.driver` 自体を fake に差し替える（実 Neo4j 不要）。実 driver の managed
# transaction が本当にロールバックすることの確認（fake では検証できない）と、バッチ行数を変えても
# 最終結果が同一であることの確認は `tests/integration/test_world_neo4j_batch_load.py` に置く。

class _FakeBatchTx:
    """`tx.run(cypher, **params)` を記録する最小スタブ。`fail_on_call`（1始まりの通し番号）に
    達したら例外を投げる——`session.execute_write` に渡す関数が例外を投げれば実 driver は tx を
    コミットしない（＝ロールバック）契約を、この fake は `_FakeBatchSession.committed` が
    立たないことで表現する。"""

    def __init__(self, log, fail_on_call=None):
        self.log = log
        self.fail_on_call = fail_on_call
        self._i = 0

    def run(self, cypher, **params):
        self._i += 1
        self.log.append((cypher, params))
        if self.fail_on_call is not None and self._i == self.fail_on_call:
            raise RuntimeError("boom mid-batch")


class _FakeBatchSession:
    def __init__(self, log, fail_on_call=None):
        self.log = log
        self.fail_on_call = fail_on_call
        self.schema_calls: list[str] = []
        self.committed = False

    def run(self, cypher, **params):             # schema コマンド（制約作成）はここを通る
        self.schema_calls.append(cypher)

    def execute_write(self, fn):
        tx = _FakeBatchTx(self.log, self.fail_on_call)
        result = fn(tx)                           # 例外はそのまま伝播＝ committed は立たない
        self.committed = True
        return result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeBatchDriver:
    def __init__(self, log, fail_on_call=None):
        self.log = log
        self.fail_on_call = fail_on_call
        self.closed = False
        self.sessions: list[_FakeBatchSession] = []

    def session(self):
        s = _FakeBatchSession(self.log, self.fail_on_call)
        self.sessions.append(s)
        return s

    def close(self):
        self.closed = True


def _patch_fake_driver(monkeypatch, log, fail_on_call=None):
    """`neo4j.GraphDatabase.driver` を差し替え、fake driver インスタンスを返す（呼出側で
    `.sessions[-1].committed`/`.closed` を検証できるように参照を渡す）。"""
    import neo4j

    driver = _FakeBatchDriver(log, fail_on_call)
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda uri, auth=None, **kw: driver)
    return driver


def _node(cid, label="Module", **extra):
    n = {"cid": cid, "label": label, "name": cid, "world_id": "w1"}
    n.update(extra)
    return n


def test_batched_helper_splits_at_boundary():
    assert list(wn._batched([1, 2, 3], 2)) == [[1, 2], [3]]
    assert list(wn._batched([1, 2, 3, 4], 2)) == [[1, 2], [3, 4]]
    assert list(wn._batched([], 2)) == []
    assert list(wn._batched([1], 5)) == [[1]]


def test_load_world_sends_n_unwind_batches_for_batch_rows_boundary(monkeypatch):
    """N=2 の小設定で3ノード（同一ラベル）→ UNWIND が2回、行数は [2, 1]。"""
    monkeypatch.setattr(wn, "_NEO4J_BATCH_ROWS", 2)
    log: list = []
    _patch_fake_driver(monkeypatch, log)
    nodes = [_node("c1"), _node("c2"), _node("c3")]
    n, m = wn.load_world(nodes, [], "w1", "bolt://x", "u", "p")
    assert (n, m) == (3, 0)
    unwind_calls = [(cypher, params) for cypher, params in log if "UNWIND" in cypher]
    assert len(unwind_calls) == 2
    assert [len(params["rows"]) for _, params in unwind_calls] == [2, 1]


def test_load_world_single_batch_when_rows_fit_default(monkeypatch):
    """既定バッチ行数（5000）以下なら1回の UNWIND に収まる（小 world で余計な往復を増やさない）。"""
    log: list = []
    _patch_fake_driver(monkeypatch, log)
    nodes = [_node("c1"), _node("c2")]
    edges = [{"src": "c1", "dst": "c2", "type": "INVOKES"}]
    wn.load_world(nodes, edges, "w1", "bolt://x", "u", "p")
    unwind_calls = [(cypher, params) for cypher, params in log if "UNWIND" in cypher]
    assert len(unwind_calls) == 2                 # ノード1バッチ＋エッジ1バッチ
    assert len(unwind_calls[0][1]["rows"]) == 2
    assert len(unwind_calls[1][1]["rows"]) == 1


def test_load_world_propagates_exception_on_mid_batch_failure(monkeypatch):
    """途中バッチで例外が起きたら `load_world` はそのまま伝播する（呼び出し元 worker.py は成功を
    確定しない・pre-invalidate 済みの last_sig がそのまま次回 sync を強制する）。tx はコミットされず
    （fake の `committed` が立たない）、driver は必ず close される（finally 節・既存契約の回帰確認）。
    """
    monkeypatch.setattr(wn, "_NEO4J_BATCH_ROWS", 2)
    log: list = []
    # DETACH DELETE(1回目) + ノード batch1(2件・2回目) + ノード batch2(1件・3回目) → 3回目で失敗。
    driver = _patch_fake_driver(monkeypatch, log, fail_on_call=3)
    nodes = [_node("c1"), _node("c2"), _node("c3")]
    with pytest.raises(RuntimeError, match="boom mid-batch"):
        wn.load_world(nodes, [], "w1", "bolt://x", "u", "p")
    session = driver.sessions[-1]
    assert session.committed is False
    assert driver.closed is True
    unwind_calls = [c for c in log if "UNWIND" in c[0]]
    assert len(unwind_calls) == 2                 # batch1 は送信済み・batch2 は送信して例外


def test_load_world_unknown_vocab_rejected_before_any_write(monkeypatch):
    """未知ラベルは書込 tx に入る前に拒否する（fail-closed・既存契約の回帰確認）。"""
    log: list = []
    _patch_fake_driver(monkeypatch, log)
    nodes = [_node("c1", label="NotARealLabel")]
    with pytest.raises(ValueError):
        wn.load_world(nodes, [], "w1", "bolt://x", "u", "p")
    assert log == []                               # 検証前に落ちるので tx すら開始しない


# ---- SHERPA_NEO4J_BATCH_ROWS（import 時に一度だけ確定・`_fresh_import` で実プロセス検証） ---

def test_neo4j_batch_rows_fresh_import_env_unset_is_default():
    assert FI.fresh_import_attr("sherpa.ingest.world_neo4j", "_NEO4J_BATCH_ROWS",
                                env={"SHERPA_NEO4J_BATCH_ROWS": None}) == 5000


def test_neo4j_batch_rows_fresh_import_env_valid_value():
    assert FI.fresh_import_attr("sherpa.ingest.world_neo4j", "_NEO4J_BATCH_ROWS",
                                env={"SHERPA_NEO4J_BATCH_ROWS": "10"}) == 10


def test_neo4j_batch_rows_fresh_import_env_invalid_falls_back_to_default():
    for bad in ("0", "abc", "-5"):
        assert FI.fresh_import_attr("sherpa.ingest.world_neo4j", "_NEO4J_BATCH_ROWS",
                                    env={"SHERPA_NEO4J_BATCH_ROWS": bad}) == 5000, bad


def test_neo4j_batch_rows_env_change_after_import_has_no_effect(monkeypatch):
    before = wn._NEO4J_BATCH_ROWS
    monkeypatch.setenv("SHERPA_NEO4J_BATCH_ROWS", "999")
    assert wn._NEO4J_BATCH_ROWS == before == 5000
