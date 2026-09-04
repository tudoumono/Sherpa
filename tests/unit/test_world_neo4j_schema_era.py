"""グラフのスキーマ世代ゲート（rv-s3-removal・Codex RV HIGH）の単体テスト。

背景: K13 語彙撤去後、**再構築前の旧 Neo4j グラフ**を読むと「旧 LLM 由来エッジの混入」や
「もっともらしい影響なし」を正常応答として返してしまう。「last_sig 不一致で 503」（Codex 案）は
不採用——鏡モデルでは原本変更〜次 sync の署名差は正常運転。代わりに、グラフを構築した時の
スキーマ世代（`GRAPH_SCHEMA_ERA`）を Neo4j 側へ保存し、現行コードの世代と異なる場合**だけ**
（＝コード側のグラフ内部形式が変わったのに再取り込みが済んでいない場合だけ）読み取りを
明示エラーにする。

対象:
  - `_compute_graph_schema_era()`: 決定的な合成（同じ入力→同じ値・材料の版を1つ上げると変わる）。
  - `check_schema_era()`: 実データ0件の world は素通り／世代一致は素通り／世代不一致
    （未保存＝旧世代含む）は `GraphSchemaEraError`／世代プローブ自体の Neo4jError は
    警告ログを残したうえで re-raise する（RV是正・rv-periphery #9・2026-09-05——旧実装は
    黙ってスキップしていたが、それだとこの安全弁自体が Neo4j の一時的な不調のたびに無条件で
    無効化されてしまう）。存在確認は `LIMIT 1`（全件走査しない）。

実 Neo4j は使わず fake session で検証する（`tests/unit/test_world_neo4j_overload.py` の
`_FakeResult`/`.data()` パターンを踏襲）。実 Neo4j を使った `load_world`→世代保存→ゲート発動の
往復は `tests/integration/test_world_neo4j_schema_era.py` を参照。
"""
from __future__ import annotations

from neo4j.exceptions import Neo4jError

from sherpa.ingest import world_neo4j as wn


# ---- _compute_graph_schema_era(): 決定的な合成 ------------------------------

def test_compute_graph_schema_era_is_deterministic_and_matches_module_constant():
    a = wn._compute_graph_schema_era()
    b = wn._compute_graph_schema_era()
    assert a == b
    assert a == wn.GRAPH_SCHEMA_ERA
    assert isinstance(a, str) and len(a) == 12   # sha256 先頭12桁


def test_compute_graph_schema_era_changes_when_code_analyzer_version_bumps(monkeypatch):
    """`analyzers.registry.CODE_ANALYZERS_SCHEMA_VERSION` の版を1つ上げると era も変わる。"""
    from sherpa.ingest.analyzers import registry as analyzer_registry
    before = wn._compute_graph_schema_era()
    monkeypatch.setattr(analyzer_registry, "CODE_ANALYZERS_SCHEMA_VERSION",
                        analyzer_registry.CODE_ANALYZERS_SCHEMA_VERSION + 1)
    after = wn._compute_graph_schema_era()
    assert after != before


def test_compute_graph_schema_era_changes_when_mention_schema_version_bumps(monkeypatch):
    """`world_graph.MENTION_SCHEMA_VERSION` の版を1つ上げると era も変わる。"""
    from sherpa.ingest import world_graph
    before = wn._compute_graph_schema_era()
    monkeypatch.setattr(world_graph, "MENTION_SCHEMA_VERSION", world_graph.MENTION_SCHEMA_VERSION + 1)
    after = wn._compute_graph_schema_era()
    assert after != before


def test_compute_graph_schema_era_changes_when_vocabulary_changes(monkeypatch):
    """語彙集合（`model.NODE_LABELS`/`EDGE_TYPES`）が変われば era も変わる（クローズド語彙の版）。"""
    before = wn._compute_graph_schema_era()
    monkeypatch.setattr(wn, "NODE_LABELS", wn.NODE_LABELS | {"NewLabel"})
    after = wn._compute_graph_schema_era()
    assert after != before


# ---- check_schema_era(): ゲート本体 -----------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return [dict(r) for r in self._rows]


class _FakeSession:
    """era プローブ専用の最小 session スタブ。`rows`（.data() が返す行）または `raise_exc` を返す。"""

    def __init__(self, rows=None, raise_exc=None):
        self._rows = rows if rows is not None else []
        self._raise_exc = raise_exc
        self.calls: list[tuple] = []

    def run(self, query, **params):
        self.calls.append((query, params))
        if self._raise_exc is not None:
            raise self._raise_exc
        return _FakeResult(self._rows)


def _timeout_error():
    return Neo4jError._hydrate_neo4j(
        code="Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration",
        message="timed out")


def test_check_schema_era_no_op_when_world_has_no_entities():
    """実データ（`:Entity`）が0件の world はゲート対象外（未投入 world の既存挙動を変えない）。"""
    s = _FakeSession(rows=[{"c": 0, "era": None}])
    wn.check_schema_era(s, "w1")   # raise しない


def test_check_schema_era_no_op_when_era_matches():
    s = _FakeSession(rows=[{"c": 3, "era": wn.GRAPH_SCHEMA_ERA}])
    wn.check_schema_era(s, "w1")   # raise しない


def test_check_schema_era_raises_when_era_mismatches():
    s = _FakeSession(rows=[{"c": 3, "era": "old-era-stamp"}])
    try:
        wn.check_schema_era(s, "w1", lens="impact")
        assert False, "世代不一致は raise するはず"
    except wn.GraphSchemaEraError as e:
        assert e.world == "w1"
        assert e.stored_era == "old-era-stamp"
        assert e.lens == "impact"


def test_check_schema_era_raises_when_era_stamp_missing():
    """実データはあるが `:SherpaMeta` スタンプ自体が無い＝現行コード以前に作られた旧世代グラフ。"""
    s = _FakeSession(rows=[{"c": 3, "era": None}])
    try:
        wn.check_schema_era(s, "w1")
        assert False, "スタンプ未保存も旧世代として raise するはず"
    except wn.GraphSchemaEraError as e:
        assert e.stored_era is None


def test_check_schema_era_reraises_probe_neo4j_error_with_log(caplog):
    """RV是正（rv-periphery #9・2026-09-05）: 世代プローブ自体が失敗（timeout 等）した場合、
    警告ログを残したうえでそのまま re-raise する（黙ってスキップしない＝この安全弁自体が
    Neo4j の一時的な不調で無条件に無効化されない）。"""
    import logging
    exc = _timeout_error()
    s = _FakeSession(raise_exc=exc)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        try:
            wn.check_schema_era(s, "w1", lens="impact")
            assert False, "世代プローブの Neo4jError は re-raise されるはず"
        except Neo4jError as e:
            assert e is exc
    assert any("世代プローブ" in r.message for r in caplog.records)


def test_check_schema_era_probe_query_limits_existence_check_to_one_row():
    """RV是正（rv-periphery #9）: 存在確認（`:Entity` の有無）は `count(n)` を真偽判定にしか
    使わないため、`LIMIT 1` で1件見つかった時点で打ち切る（world_id 一致ノードが大量にある
    world でも全件を数え上げない）。"""
    s = _FakeSession(rows=[{"c": 1, "era": wn.GRAPH_SCHEMA_ERA}])
    wn.check_schema_era(s, "w1")
    query, _params = s.calls[0]
    assert "LIMIT 1" in query
