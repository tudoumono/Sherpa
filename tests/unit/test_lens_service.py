"""lens_service の Neo4j 安全弁（secRV 範囲外是正・2026-07-19）の単体テスト。

対象は `resolve_anchor`／`neo4j_related`（本モジュール内で `session.run` を呼ぶ唯一の2箇所・
共通の安全弁ロジックは `_run_capped`）。実 Neo4j は使わず fake session で検証する
（`tests/unit/test_presumed_impact.py` の `_Session`/`_Res` パターンを踏襲・cursor 反復に合わせて拡張）。

- 緊急天井（メモリ保護）: fake session が上限超の行を返すと、返却が `_NEO4J_MAX_ROWS` 件で
  打ち切られ `log.warning` が出る（LIMIT は入れない＝網羅性維持がユーザー決定・天井は安全弁）。
- per-query タイムアウト: `session.run` に渡るクエリが `neo4j.Query(timeout=...)` になっている。
- タイムアウト縮退: タイムアウト由来の `Neo4jError` は空リストへ縮退＋`log.warning`
  （黙殺しない）。タイムアウト以外の `Neo4jError` は再送出する（Cypher バグ等を握り潰さない）。
- 正常系回帰: 返却形状（cid/name/path/distance/edges 等）が変わらない。
- env 検証: 負値/非整数/巨大値が既定・クランプへ戻る（`agentic_search._env_int` と同一セマンティクス）。

secRV 範囲外是正 追補（2026-07-19・RV指摘 HIGH-1）: 天井 break 後に `Result.consume()` が呼ばれる
（未消費の Result を残すと driver 6.2.0 が同一 session の次クエリで残りを全件バッファしてしまう）。
"""
from __future__ import annotations

import logging

import pytest
from neo4j.exceptions import Neo4jError

import _fresh_import as FI   # noqa: E402   # import-time 固定 env 定数の実プロセス検証
from sherpa import lens_service as ls


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
    driver 6.2.0 は未消費 Result を残したまま同一 session で次の `session.run()` を呼ぶと、前の
    Result の残りを全件 fetch/buffer してしまうため、天井 break 後は必ず `consume()` される想定。
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
    """`resolve_anchor`/`neo4j_related` が呼ぶ最小 session スタブ（実 Neo4j 不要）。"""

    def __init__(self, rows=None, raise_exc=None):
        self._rows = rows or []
        self._raise_exc = raise_exc
        self.calls: list[tuple] = []   # [(query, params), ...]（timeout 検証用に記録）
        self.last_result: _FakeResult | None = None   # HIGH-1: consume() 検証用に直近の Result を保持

    def run(self, query, **params):
        self.calls.append((query, params))
        if self._raise_exc is not None:
            raise self._raise_exc
        self.last_result = _FakeResult(self._rows)
        return self.last_result


def _related_row(cid, name, label="Module"):
    return {"cid": cid, "name": name, "label": label, "em": "static", "status": "active",
            "path_names": ["ROOT", name], "edges": [{"type": "USES", "doc": "a.md"}], "dist": 1}


def _timeout_error():
    """実サーバが返す形式を模した Neo4jError（`Neo4jError._hydrate_neo4j` で生成・実物と同じ経路）。"""
    return Neo4jError._hydrate_neo4j(
        code="Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration",
        message="timed out")


def _other_error():
    """タイムアウトではない Neo4jError（Cypher バグ等・握り潰してはいけない）。"""
    return Neo4jError._hydrate_neo4j(code="Neo.ClientError.Statement.SyntaxError", message="bad cypher")


# ---- 緊急天井（メモリ保護・LIMIT は入れない） -------------------------------

def test_neo4j_related_caps_rows_and_warns(caplog):
    over = ls._NEO4J_MAX_ROWS + 5
    rows = [_related_row(f"cid:{i}", "NODE") for i in range(over)]
    s = _FakeSession(rows=rows)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = ls.neo4j_related(s, ["root"], "w1")
    assert len(out) == ls._NEO4J_MAX_ROWS               # 収集済み分はそのまま返す（削らない）
    assert any("緊急天井" in r.getMessage() and "w1" in r.getMessage() for r in caplog.records)


def test_resolve_anchor_caps_rows_and_warns(caplog):
    over = ls._NEO4J_MAX_ROWS + 5
    # 全行が同じ小文字名 "node" を持つようにし、text にもその語を含めることで
    # フィルタ後も件数が変わらない（天井そのものを検証する）。
    rows = [{"cid": f"cid:{i}", "name": "node"} for i in range(over)]
    s = _FakeSession(rows=rows)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = ls.resolve_anchor(s, "text containing node token", "w1")
    assert len(out) == ls._NEO4J_MAX_ROWS
    assert any("緊急天井" in r.getMessage() for r in caplog.records)


def test_cap_not_triggered_under_limit(caplog):
    """行数が上限未満なら打ち切らず、warning も出さない（正常系に影響しない）。"""
    rows = [_related_row(f"cid:{i}", "NODE") for i in range(3)]
    s = _FakeSession(rows=rows)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = ls.neo4j_related(s, ["root"], "w1")
    assert len(out) == 3
    assert not any("緊急天井" in r.getMessage() for r in caplog.records)


# ---- HIGH-1（secRV 範囲外是正 追補・2026-07-19）: 天井 break 後の Result.consume() -----------

def test_neo4j_related_consumes_result_on_cap_break():
    """天井到達で break する際、返す前に `Result.consume()` を呼ぶ（未消費のまま返すと driver
    6.2.0 が次の `session.run()` で残りを全件バッファする＝安全弁の逆流を防ぐ）。"""
    over = ls._NEO4J_MAX_ROWS + 5
    rows = [_related_row(f"cid:{i}", "NODE") for i in range(over)]
    s = _FakeSession(rows=rows)
    ls.neo4j_related(s, ["root"], "w1")
    assert s.last_result is not None
    assert s.last_result.consumed is True


def test_resolve_anchor_consumes_result_on_cap_break():
    over = ls._NEO4J_MAX_ROWS + 5
    rows = [{"cid": f"cid:{i}", "name": "node"} for i in range(over)]
    s = _FakeSession(rows=rows)
    ls.resolve_anchor(s, "text containing node token", "w1")
    assert s.last_result is not None
    assert s.last_result.consumed is True


# ---- per-query タイムアウトの引き渡し ---------------------------------------

def test_neo4j_related_passes_query_timeout():
    s = _FakeSession(rows=[_related_row("c1", "NODE")])
    ls.neo4j_related(s, ["root"], "w1")
    query, params = s.calls[0]
    assert query.timeout == ls._NEO4J_QUERY_TIMEOUT_S
    assert params["world"] == "w1" and params["anchors"] == ["root"]


def test_resolve_anchor_passes_query_timeout():
    s = _FakeSession(rows=[{"cid": "c1", "name": "node"}])
    ls.resolve_anchor(s, "node text", "w1")
    query, params = s.calls[0]
    assert query.timeout == ls._NEO4J_QUERY_TIMEOUT_S
    assert params["w"] == "w1"


# ---- タイムアウト時の縮退（黙殺ではなく log.warning＋空リスト） --------------

def test_neo4j_related_degrades_to_empty_on_timeout(caplog):
    s = _FakeSession(raise_exc=_timeout_error())
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = ls.neo4j_related(s, ["root"], "w1")
    assert out == []
    assert any("タイムアウト" in r.getMessage() and "w1" in r.getMessage() for r in caplog.records)


def test_resolve_anchor_degrades_to_empty_on_timeout(caplog):
    s = _FakeSession(raise_exc=_timeout_error())
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = ls.resolve_anchor(s, "symptom text", "w1")
    assert out == []
    assert any("タイムアウト" in r.getMessage() for r in caplog.records)


def test_non_timeout_neo4j_error_is_not_swallowed():
    """タイムアウト以外のサーバエラー（Cypher バグ等）は縮退させず再送出する。"""
    s = _FakeSession(raise_exc=_other_error())
    with pytest.raises(Neo4jError):
        ls.neo4j_related(s, ["root"], "w1")


def test_is_query_timeout_matches_known_code_shapes():
    assert ls._is_query_timeout(_timeout_error())
    assert not ls._is_query_timeout(_other_error())


# ---- 正常系回帰: 返却形状は不変 ---------------------------------------------

def test_neo4j_related_shape_unchanged():
    s = _FakeSession(rows=[_related_row("cid:1", "TAXCALC")])
    out = ls.neo4j_related(s, ["root"], "w1")
    assert out == [{
        "cid": "cid:1", "name": "TAXCALC", "label": "Module",
        "category": ls.CATEGORY.get("Module", "Module"),
        "extraction_method": "static", "status": "active",
        "path": ["ROOT", "TAXCALC"], "distance": 1,
        "edges": [{"type": "USES", "doc": "a.md"}],
    }]


def test_resolve_anchor_shape_unchanged():
    s = _FakeSession(rows=[{"cid": "cid:1", "name": "TAXCALC"}])
    out = ls.resolve_anchor(s, "TAXCALC が ABEND", "w1")
    assert out == [("cid:1", "TAXCALC")]


def test_neo4j_related_empty_anchors_short_circuits_without_query():
    """anchors が空なら session.run 自体を呼ばない（既存の早期 return）。"""
    s = _FakeSession(rows=[_related_row("cid:1", "X")])
    assert ls.neo4j_related(s, [], "w1") == []
    assert s.calls == []


# ---- cid（Neo4j canonical_id）は内部専用: run_troubleshoot は公開前に除去する ----------------

def test_troubleshoot_cards_internal_helper_carries_cid(monkeypatch):
    """内部専用 `_troubleshoot_cards` は card に `cid`（Neo4j canonical_id）を含む——
    `neighbor_cards`（agentic 経路）だけがこれを消費する契約。"""
    monkeypatch.setattr(ls, "grep_search", lambda *a, **k: [])
    s = _FakeSession(rows=[_related_row("module:w1:a/b#TAXCALC", "TAXCALC")])
    anchor_names, cards, truncated_docs = ls._troubleshoot_cards(s, "TAXCALC の ABEND", "w1")
    assert anchor_names == {"TAXCALC"}
    assert len(cards) == 1
    assert cards[0]["cid"] == "module:w1:a/b#TAXCALC"
    assert truncated_docs == []


def test_run_troubleshoot_public_result_omits_cid(monkeypatch):
    """公開 `run_troubleshoot()` の候補カードには `cid` を含めない——直接 API
    （`routers/impact.py::troubleshoot_run`）・非 agentic 会話保存（`chat_service.py`）・
    sanitized 共有（`store/shares.py::_safe_share_answer` の浅いコピー）・JSON 書き出し
    （`web/chat/menus.js` は保存済み `data` をそのまま書き出すため同じ経路）へ内部専用フィールドを
    漏らさないための契約。応答全体を完全な期待 dict と一致させる（golden・cid 以外の形も含めて
    意図しない差分が紛れ込まないことを固定する）。"""
    monkeypatch.setattr(ls, "grep_search", lambda *a, **k: [])
    s = _FakeSession(rows=[_related_row("module:w1:a/b#TAXCALC", "TAXCALC")])
    result = ls.run_troubleshoot(s, "TAXCALC の ABEND", "w1")
    assert result == {
        "type": "troubleshoot", "world": "w1", "symptom": "TAXCALC の ABEND",
        "anchors": ["TAXCALC"],
        "candidates": [{
            "name": "TAXCALC", "label": "Module", "category": "ソース", "role": "実装",
            "distance": 1, "path": ["ROOT", "TAXCALC"], "source": "graph",
            "evidence": {"edges": [{"type": "USES", "doc": "a.md"}], "grep": []},
        }],
    }


def test_neighbor_cards_agentic_path_preserves_cid(monkeypatch):
    """agentic ツール `graph_neighbors` 専用の `neighbor_cards` は `_troubleshoot_cards` の card を
    そのまま返す——`run_troubleshoot`（公開・cid 除去済み）を経由しないため cid が失われない。"""
    import neo4j as neo4j_mod

    from sherpa.ingest import world_neo4j

    fake_cards = [{"name": "TAXCALC", "label": "Module", "cid": "module:w1:a/b#TAXCALC",
                  "category": "プログラム", "role": "実装", "distance": 1, "path": [],
                  "source": "graph", "evidence": {"edges": [], "grep": []}}]
    monkeypatch.setattr(ls, "_troubleshoot_cards",
                        lambda session, term, world, scope_paths=None: ({"TAXCALC"}, fake_cards, []))

    class _Sess:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    class _Driver:
        def session(self):
            return _Sess()

        def close(self):
            pass

    monkeypatch.setattr(neo4j_mod.GraphDatabase, "driver", lambda uri, auth: _Driver())
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})

    cards = ls.neighbor_cards("w1", "TAXCALC の ABEND")
    assert cards and cards[0]["cid"] == "module:w1:a/b#TAXCALC"


# ---- env 検証（_env_int・agentic_search と同一セマンティクス） --------------

def test_env_int_falls_back_on_invalid_values(monkeypatch):
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "-1")
    assert ls._env_int("SHERPA_TEST_LIMIT", 30, 1, 600) == 30
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "0")
    assert ls._env_int("SHERPA_TEST_LIMIT", 30, 1, 600) == 30
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "999999")
    assert ls._env_int("SHERPA_TEST_LIMIT", 30, 1, 600) == 30
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "abc")
    assert ls._env_int("SHERPA_TEST_LIMIT", 30, 1, 600) == 30
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "60")
    assert ls._env_int("SHERPA_TEST_LIMIT", 30, 1, 600) == 60
    monkeypatch.delenv("SHERPA_TEST_LIMIT", raising=False)
    assert ls._env_int("SHERPA_TEST_LIMIT", 30, 1, 600) == 30


def test_env_int_clamps_dynamic_default(monkeypatch):
    hi = 1_000_000
    big_default = 2_000_000   # hi を超える既定値
    monkeypatch.delenv("SHERPA_TEST_LIMIT", raising=False)
    assert ls._env_int("SHERPA_TEST_LIMIT", big_default, 100, hi) == hi
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "-1")
    assert ls._env_int("SHERPA_TEST_LIMIT", big_default, 100, hi) == hi
    monkeypatch.setenv("SHERPA_TEST_LIMIT", "abc")
    assert ls._env_int("SHERPA_TEST_LIMIT", big_default, 100, hi) == hi
    assert ls._env_int("SHERPA_TEST_LIMIT", 1, 100, hi) == 100   # lo 側のクランプも対称


def test_module_defaults_are_clamped_into_range():
    """モジュール読み込み時に計算済みの既定（未設定 env 時）が仕様の範囲内にある。"""
    assert 1 <= ls._NEO4J_QUERY_TIMEOUT_S <= 600
    assert 100 <= ls._NEO4J_MAX_ROWS <= 1_000_000


# ---- SHERPA_TROUBLESHOOT_GRAPH_DEPTH ----
# import 時に一度だけ確定する定数は実プロセスを新規に起こして検証する（`_fresh_import`）。

def test_troubleshoot_graph_depth_fresh_import_env_unset_is_default():
    assert FI.fresh_import_attr("sherpa.lens_service", "TROUBLESHOOT_GRAPH_DEPTH",
                                env={"SHERPA_TROUBLESHOOT_GRAPH_DEPTH": None}) == 3


def test_troubleshoot_graph_depth_fresh_import_env_valid_value():
    assert FI.fresh_import_attr("sherpa.lens_service", "TROUBLESHOOT_GRAPH_DEPTH",
                                env={"SHERPA_TROUBLESHOOT_GRAPH_DEPTH": "5"}) == 5


def test_troubleshoot_graph_depth_fresh_import_env_invalid_falls_back_to_default():
    for bad in ("0", "17", "abc"):
        assert FI.fresh_import_attr("sherpa.lens_service", "TROUBLESHOOT_GRAPH_DEPTH",
                                    env={"SHERPA_TROUBLESHOOT_GRAPH_DEPTH": bad}) == 3, bad


def test_troubleshoot_graph_depth_env_change_after_import_has_no_effect(monkeypatch):
    before = ls.TROUBLESHOOT_GRAPH_DEPTH
    monkeypatch.setenv("SHERPA_TROUBLESHOOT_GRAPH_DEPTH", "10")
    assert ls.TROUBLESHOOT_GRAPH_DEPTH == before == 3


def test_troubleshoot_depth_default_params_are_troubleshoot_graph_depth():
    """`neo4j_related`/`_troubleshoot_cards`/`run_troubleshoot` の `depth` 既定値は
    `TROUBLESHOOT_GRAPH_DEPTH` に揃っている。

    既定値と異なる env（7）で fresh import して確認する＝既定値どうしが偶然一致するだけの
    「旧リテラル `depth=3` への退行」を検出できない自己言及を避ける。
    """
    env = {"SHERPA_TROUBLESHOOT_GRAPH_DEPTH": "7"}
    assert FI.fresh_import_param_default("sherpa.lens_service", "neo4j_related", "depth", env=env) == 7
    assert FI.fresh_import_param_default(
        "sherpa.lens_service", "_troubleshoot_cards", "depth", env=env) == 7
    assert FI.fresh_import_param_default(
        "sherpa.lens_service", "run_troubleshoot", "depth", env=env) == 7


# ===== run_qa の layer 転送（探す対象・調べ方ブロック §3.4） =====
# troubleshoot（`_troubleshoot_cards`/`run_troubleshoot`）は layer を受け取らない設計（§3.5 非適用）
# のため、layer 転送テストは qa（`run_qa`）のみ対象。

def test_run_qa_forwards_layer_to_grep_search_on_first_call(monkeypatch):
    """`run_qa(question, layer=...)` は自然文の1発目 grep_search へ layer をそのまま転送する。"""
    captured = {}

    def fake_grep(q, world, max_hits=20, scope_paths=None, layer=None, truncated_docs=None):
        captured["layer"] = layer
        return [{"doc_id": "a.md", "line": 1, "span": [1, 1], "text": "hit", "ext": ".md", "match": q}]

    monkeypatch.setattr(ls, "grep_search", fake_grep)
    ls.run_qa("消費税率", "w1", layer="code")
    assert captured.get("layer") == "code"


def test_run_qa_forwards_layer_to_grep_search_in_term_split_fallback(monkeypatch):
    """1発目が0件のとき、語分割してのフォールバック grep_search 呼び出しにも layer が転送される。"""
    calls = []

    def fake_grep(q, world, max_hits=20, scope_paths=None, layer=None, truncated_docs=None):
        calls.append(layer)
        if q == "消費税率について":   # 1発目は自然文そのまま＝0件（フォールバック誘発）
            return []
        return [{"doc_id": "a.md", "line": 1, "span": [1, 1], "text": "hit", "ext": ".md", "match": q}]

    monkeypatch.setattr(ls, "grep_search", fake_grep)
    result = ls.run_qa("消費税率について", "w1", layer="docs")
    assert result["answered"]
    assert calls and all(v == "docs" for v in calls)   # 1発目・フォールバックとも同じ layer


def test_run_qa_layer_omitted_forwards_none_unchanged(monkeypatch):
    """layer 省略時は None がそのまま grep_search へ渡る（既存呼び出し元は無変更＝both 相当）。"""
    captured = {}

    def fake_grep(q, world, max_hits=20, scope_paths=None, layer=None, truncated_docs=None):
        captured["layer"] = layer
        return []

    monkeypatch.setattr(ls, "grep_search", fake_grep)
    ls.run_qa("消費税率", "w1")
    assert captured.get("layer") is None


# ===== grep 打切りの平文申告（`truncated_docs` out-param → `notes`・非エージェント経路） =====
# `grep_tool.grep_search(truncated_docs=...)` はヒット0件でも打切りを申告できる「本命」経路
# （ヒット由来の `file_truncated` だけでは無音になるケース）。`agentic_search.ripgrep_search` は
# 既に受け取っているが、非エージェント経路（run_qa/run_troubleshoot）は本スライスまで無音だった。

def test_truncated_search_note_empty_is_none():
    assert ls._truncated_search_note([]) is None


def test_truncated_search_note_single_doc_is_plain_text():
    note = ls._truncated_search_note(["設計/税率.md"])
    assert note == "「設計/税率.md」は大きすぎて全体を検索できていません（先頭部分のみ）。"
    for forbidden in ("file_truncated", "cap", "バイト", "byte"):   # 内部語彙を出さない（docs/04）
        assert forbidden not in note


def test_truncated_search_note_multiple_docs_joined():
    note = ls._truncated_search_note(["a.md", "b.md"])
    assert note == "次の資料は大きすぎて全体を検索できていません（先頭部分のみ）: 「a.md」「b.md」"


def test_truncated_search_note_caps_display_at_five():
    docs = [f"{i}.md" for i in range(7)]
    note = ls._truncated_search_note(docs)
    assert "ほか2件" in note
    assert "6.md" not in note   # 6件目以降は個別表示しない


def test_run_qa_truncated_docs_becomes_plain_note(monkeypatch):
    """grep が打ち切った文書があれば、平文の注記（`notes`）が run_qa の出力へ加算される。"""
    def fake_grep(q, world, max_hits=20, scope_paths=None, layer=None, truncated_docs=None):
        if truncated_docs is not None:
            truncated_docs.append("大きい資料.md")
        return [{"doc_id": "a.md", "line": 1, "span": [1, 1], "text": "hit", "ext": ".md", "match": q}]

    monkeypatch.setattr(ls, "grep_search", fake_grep)
    result = ls.run_qa("消費税率", "w1")
    assert result["notes"] == ["「大きい資料.md」は大きすぎて全体を検索できていません（先頭部分のみ）。"]


def test_run_qa_truncated_docs_dedup_across_fallback_calls(monkeypatch):
    """1発目・語分割フォールバックの複数回の grep_search 呼び出しをまたいで同じ list を渡す
    （grep_search 自身の重複排除が呼び出し間でも効くようにする）。"""
    seen_ids = []

    def fake_grep(q, world, max_hits=20, scope_paths=None, layer=None, truncated_docs=None):
        seen_ids.append(id(truncated_docs))
        if q == "消費税率について":   # 1発目は自然文そのまま＝0件（フォールバック誘発）
            return []
        return [{"doc_id": "a.md", "line": 1, "span": [1, 1], "text": "hit", "ext": ".md", "match": q}]

    monkeypatch.setattr(ls, "grep_search", fake_grep)
    ls.run_qa("消費税率について", "w1")
    assert len(seen_ids) >= 2 and len(set(seen_ids)) == 1   # 同一オブジェクトを毎回渡す


def test_run_qa_no_truncation_output_unchanged(monkeypatch):
    """打切りが無いとき、run_qa の出力は `notes` キーを持たない従来どおりの形（加算的変更）。

    CITE-1（H3・SC-4接続）: quote は excerpts.display_quote で引き直す——`w1`/`a.md` は実ファイルが
    無いため対応が取れず `excerpt_source="rag"`（fallback）で quote 自体は不変。この加算的フィールド
    以外は従来どおり（`locator_hint` は locator/section_path が無いため付かない）。
    """
    def fake_grep(q, world, max_hits=20, scope_paths=None, layer=None, truncated_docs=None):
        return [{"doc_id": "a.md", "line": 1, "span": [1, 1], "text": "hit", "ext": ".md", "match": q}]

    monkeypatch.setattr(ls, "grep_search", fake_grep)
    result = ls.run_qa("消費税率", "w1")
    assert result == {
        "type": "qa", "world": "w1", "question": "消費税率", "answered": True,
        "citations": [{"doc_id": "a.md", "span": [1, 1], "quote": "hit", "ext": ".md", "match": "消費税率",
                       "excerpt_source": "rag"}],
    }
    assert "notes" not in result


def test_troubleshoot_cards_reuses_same_truncated_docs_list_across_anchor_calls(monkeypatch):
    """`_troubleshoot_cards` はアンカー名ごとに grep_search を呼ぶが、`truncated_docs` は同じ
    list オブジェクトを毎回渡す——`grep_search` 自身の重複排除（`rel not in truncated_docs`）が
    複数回の呼び出しをまたいで効くようにするため。"""
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


def test_run_troubleshoot_truncated_docs_becomes_plain_note(monkeypatch):
    """運用手順 grep が打ち切った文書があれば、平文の注記（`notes`）が run_troubleshoot の
    出力へ加算される（内部語彙 file_truncated/cap/バイトは出さない）。"""
    def fake_grep(nm, world, scope_paths=None, truncated_docs=None):
        if truncated_docs is not None:
            truncated_docs.append("運用手順.md")
        return []

    monkeypatch.setattr(ls, "grep_search", fake_grep)
    s = _FakeSession(rows=[_related_row("module:w1:a/b#TAXCALC", "TAXCALC")])
    result = ls.run_troubleshoot(s, "TAXCALC の ABEND", "w1")
    assert result["notes"] == ["「運用手順.md」は大きすぎて全体を検索できていません（先頭部分のみ）。"]
    for forbidden in ("file_truncated", "cap", "バイト", "byte"):
        assert forbidden not in result["notes"][0]


def test_run_troubleshoot_no_truncation_output_unchanged(monkeypatch):
    """打切りが無いとき、run_troubleshoot の出力は `notes` キーを持たない従来どおりの形
    （加算的変更・`test_run_troubleshoot_public_result_omits_cid` と同じ期待 dict）。"""
    monkeypatch.setattr(ls, "grep_search", lambda *a, **k: [])
    s = _FakeSession(rows=[_related_row("module:w1:a/b#TAXCALC", "TAXCALC")])
    result = ls.run_troubleshoot(s, "TAXCALC の ABEND", "w1")
    assert "notes" not in result
