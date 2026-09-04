"""範囲（scope）受け入れ（鏡モデル・MIRROR §3）: フォルダ prefix を grep/影響/出典に効かせる。

鏡では範囲＝フォルダ prefix そのもの（layer/common の自動合流・auto-scope 推定は撤去）。
純粋部（grep/scope/filter_items）は Neo4j/PG 不要。影響の縦切り e2e は要 Neo4j+PG（無ければ skip）。
"""
from __future__ import annotations

import pytest
from _world_setup import (OPS, S_DESIGN, S_OPS, S_SRC, SPEC, TAXCALC, TAXCPY,
                          TEST_WORLD_ID, ensure_v1)

from sherpa import scope
from sherpa.grep_tool import grep_search
from sherpa.lens_service import run_qa

V = TEST_WORLD_ID   # 旧固定 'v1' から移行（2026-07-03 インシデント対応 HIGH#2・_world_setup.py 参照）


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """このファイルはログインせず直接叩く前提（compat モード）。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


def _docs(query, **kw):
    return {h["doc_id"] for h in grep_search(query, V, **kw)}


def _skip(reason):
    pytest.skip(reason)


# ---- scope ルール（純粋・prefix 前方一致） --------------------------------

def test_in_scope_prefix_rules():
    assert scope.in_scope(SPEC, [S_DESIGN]) is True             # 完全一致
    assert scope.in_scope(SPEC, ["4期/02_設計"]) is True         # 親 prefix に前方一致
    assert scope.in_scope(SPEC, ["4期/02_設"]) is False          # 境界（部分セグメントは一致しない）
    assert scope.in_scope(TAXCALC, [S_DESIGN]) is False          # 別フォルダは外
    assert scope.in_scope(TAXCPY, [S_SRC]) is False              # 鏡: 共通の自動合流は無い（フォルダが真）
    assert scope.in_scope(TAXCALC, []) is True                   # 空選択＝world 全体
    assert scope.in_scope(TAXCALC, ["4期"]) is True              # 世代トップで全部入る


# ---- grep を範囲で絞る（純粋） -------------------------------------------

def test_grep_unscoped_is_superset():
    assert {SPEC, TAXCALC, OPS, TAXCPY} <= _docs("TAX-RATE")


def test_grep_scope_source_only():
    """ソース scope に絞るとソースだけ（鏡: 00_共通 の TAX-CPY は別フォルダ＝合流しない）。"""
    assert _docs("TAX-RATE", scope_paths=[S_SRC]) == {TAXCALC}


def test_grep_scope_design_only():
    assert _docs("TAX-RATE", scope_paths=[S_DESIGN]) == {SPEC}


def test_grep_scope_prefix_match():
    assert _docs("TAX-RATE", scope_paths=[S_OPS]) == {OPS}       # 02_保守 は 02_保守/03_運用手順 を含む


def test_grep_generation_top_includes_all():
    assert {SPEC, TAXCALC, OPS, TAXCPY} <= _docs("TAX-RATE", scope_paths=["4期"])


# ---- filter_items（根拠 doc 単位・純粋） ---------------------------------

def test_filter_items_by_prefix_and_keeps_no_evidence():
    items = [
        {"name": "TAXCALC", "evidence": [{"type": "USES", "doc": TAXCALC}]},   # 03_開発 → 外（設計scope）
        {"name": "spec", "evidence": [{"type": "REFERENCES", "doc": SPEC}]},   # 02_設計 → 残す
        {"name": "structural", "evidence": []},                                # 根拠なし → 残す（トレース）
        {"name": "bridge", "evidence": [{"type": "REALIZES", "doc": "名寄せ"}]},  # マーカーのみ → 残す
    ]
    out = {i["name"] for i in scope.filter_items(items, [S_DESIGN])}
    assert out == {"spec", "structural", "bridge"}
    assert len(scope.filter_items(items, [])) == 4              # 空選択は素通し


def test_filter_items_prunes_evidence_to_scope():
    items = [{"name": "spec", "evidence": [
        {"type": "REFERENCES", "doc": SPEC},     # 02_設計 → 残す
        {"type": "USES", "doc": TAXCALC},        # 03_開発 → 剪定
    ]}]
    out = scope.filter_items(items, [S_DESIGN])
    assert {e["doc"] for e in out[0]["evidence"]} == {SPEC}      # 範囲外は出典に出さない


def test_filter_items_neighbor_shape():
    items = [
        {"name": "ops", "evidence": {"edges": [], "grep": [{"doc_id": OPS}]}},
        {"name": "src", "evidence": {"edges": [{"doc": TAXCALC}], "grep": []}},
    ]
    assert {i["name"] for i in scope.filter_items(items, [S_OPS])} == {"ops"}


# ---- scope ツリー / 検証 / 正規化（純粋） --------------------------------

def test_scope_tree():
    t = scope.scope_tree(V)
    paths = {s["path"]: s for s in t["scopes"]}
    assert "4期" in paths and S_SRC in paths and S_DESIGN in paths   # 祖先パスも選べる
    assert paths[S_SRC]["count"] >= 1 and paths[S_SRC]["label"] == "ソース"  # 番号を外した見出し


def test_valid_scope_paths():
    assert scope.valid_scope_paths(V, []) is True
    assert scope.valid_scope_paths(V, [S_DESIGN, "4期/02_設計"]) is True   # 既知＋祖先
    assert scope.valid_scope_paths(V, ["存在しない"]) is False


def test_normalize_scope_paths():
    assert scope.normalize_scope_paths(None) == []
    assert scope.normalize_scope_paths([" a/b ", "a/b", "", "/a/b/"]) == ["a/b"]


def test_qa_scoped_citations_within_scope():
    full = {c["doc_id"] for c in run_qa("TAX-RATE", V)["citations"]}
    scoped = {c["doc_id"] for c in run_qa("TAX-RATE", V, scope_paths=[S_DESIGN])["citations"]}
    assert scoped == {SPEC} and scoped < full


def test_chatreq_accepts_scope_paths():
    from sherpa.api import ChatReq
    assert ChatReq(message="x").scope_paths == []
    assert ChatReq(message="x", scope_paths=["a/b", "c"]).scope_paths == ["a/b", "c"]


def test_scopes_endpoint():
    try:
        from fastapi.testclient import TestClient
        from sherpa.api import app
        r = TestClient(app).get("/scopes", params={"world": V})
    except Exception as e:
        return _skip(f"app import failed: {e}")
    assert r.status_code == 200
    assert any(s["path"] == S_SRC for s in r.json()["scopes"])


def test_api_rejects_unknown_scope():
    try:
        from fastapi.testclient import TestClient
        from sherpa.api import app
        c = TestClient(app)
        r = c.post("/chat", json={"message": "x", "world": V,
                                  "knowledge": True, "scope_paths": ["存在しない/フォルダ"]})
    except Exception as e:
        return _skip(f"infra down: {e}")
    assert r.status_code == 422


# ---- 探す対象（層フィルタ・調べ方ブロック §3.4） --------------------------------

def test_chatreq_accepts_layer():
    """既定 both・docs/code も受理・scope_paths と同じ並びのフィールド。"""
    from sherpa.api import ChatReq
    assert ChatReq(message="x").layer == "both"
    assert ChatReq(message="x", layer="docs").layer == "docs"
    assert ChatReq(message="x", layer="code").layer == "code"


def test_chatreq_rejects_invalid_layer_422():
    """不正な layer 値は pydantic Literal により 422（ExtSearchReq と同じ契約）。

    pydantic のモデル検証は FastAPI がハンドラ本体（DB/Neo4j 接続）へ入る**前**に行い、失敗は
    ASGI レベルで 422 レスポンスへ変換される——DB/インフラの状態に左右されない契約のため、
    広い `except Exception: skip` は使わない（検証そのものが壊れていても skip で隠さない）。
    """
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    r = c.post("/chat", json={"message": "x", "world": V, "knowledge": False, "layer": "bogus"})
    assert r.status_code == 422


def test_chat_stream_rejects_invalid_layer_422():
    """GET /chat/stream（Query パラメータ版）も同じ Literal 制約で 422（DB 非依存・上記と同じ理由で
    infra-down スキップは使わない）。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    r = c.get("/chat/stream", params={"message": "x", "world": V, "layer": "bogus"})
    assert r.status_code == 422


# ---- 調べ方の明示指定（調べ方ブロック §3.1） ------------------------------------

def test_chatreq_accepts_lens():
    """既定 None（省略）＝自動・4レンズを受理する。"""
    from sherpa.api import ChatReq
    assert ChatReq(message="x").lens is None
    for lens in ("impact", "troubleshoot", "qa", "author"):
        assert ChatReq(message="x", lens=lens).lens == lens


def test_chatreq_rejects_invalid_lens_422():
    """不正な lens 値は pydantic Literal により 422（layer と同じ契約・DB 非依存）。
    正典は4値＋省略のみ——非正典の "auto" 互換値も受理しない（RV1 #12）。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    for bad in ("bogus", "auto"):
        r = c.post("/chat", json={"message": "x", "world": V, "knowledge": False, "lens": bad})
        assert r.status_code == 422


def test_chat_stream_rejects_invalid_lens_422():
    """GET /chat/stream（Query パラメータ版）も同じ Literal 制約で 422。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    r = c.get("/chat/stream", params={"message": "x", "world": V, "lens": "bogus"})
    assert r.status_code == 422


# ---- 調べる深さ（調べ方ブロック §3.2・SC-6c） ------------------------------------

def test_chatreq_accepts_depth_profile():
    """既定 standard・deep/max も受理（layer/lens と同じ並びのフィールド）。"""
    from sherpa.api import ChatReq
    assert ChatReq(message="x").depth_profile == "standard"
    assert ChatReq(message="x", depth_profile="deep").depth_profile == "deep"
    assert ChatReq(message="x", depth_profile="max").depth_profile == "max"


def test_chatreq_rejects_invalid_depth_profile_422():
    """不正な depth_profile 値は pydantic Literal により 422（layer/lens と同じ契約・DB 非依存）。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    r = c.post("/chat", json={"message": "x", "world": V, "knowledge": False, "depth_profile": "bogus"})
    assert r.status_code == 422


def test_chat_stream_rejects_invalid_depth_profile_422():
    """GET /chat/stream（Query パラメータ版）も同じ Literal 制約で 422。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    r = c.get("/chat/stream", params={"message": "x", "world": V, "depth_profile": "bogus"})
    assert r.status_code == 422


# ---- 検索経路トグル（調べ方ブロック §3.6・SC-6e） ------------------------------------

def test_chatreq_tools_omitted_or_null_is_none():
    """省略/null は None（`_resolve_scope` 側で全ONに正規化・depth_profile と同型の欠落契約）。"""
    from sherpa.api import ChatReq
    assert ChatReq(message="x").tools is None
    assert ChatReq(message="x", tools=None).tools is None


def test_chatreq_tools_partial_dict_keeps_only_explicit_keys():
    """SC-6e: 欠落キーは埋めない（生の dict のまま保持）——埋めてしまうと「明示的に true と
    指定したか」が失われ、可用性 422 判定（`unavailable_explicit_tools`）が省略キーまで誤検知する。
    実効値（欠落=全ON）への正規化は `_resolve_scope`（保存時）が別途行う。"""
    from sherpa.api import ChatReq
    assert ChatReq(message="x", tools={"grep": False}).tools == {"grep": False}


@pytest.mark.parametrize("endpoint", ["/chat", "/chat/turns"])
def test_chatreq_rejects_all_tools_off_422(endpoint):
    """grep/fulltext/graph の3つとも false は 422（検索経路が0個になるのを許さない）。
    共通 `ChatReq` の pydantic field_validator（リクエスト本文の型検証時点）で弾くため、
    /chat・/chat/turns のどちらでも同じ 422 になる（SC-6e）。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    r = c.post(endpoint, json={"message": "x", "world": V, "knowledge": False,
                               "tools": {"grep": False, "fulltext": False, "graph": False}})
    assert r.status_code == 422


def test_chatreq_rejects_unknown_tools_key_422():
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    r = c.post("/chat", json={"message": "x", "world": V, "knowledge": False,
                              "tools": {"bogus": True}})
    assert r.status_code == 422


@pytest.mark.parametrize("bad_value", ["false", "true", 0, 1, "yes", None, [], {}])
def test_chatreq_rejects_non_boolean_tools_value_422(bad_value):
    """SC-6e: `StrictBool` は非 bool 値（文字列/数値/null/配列/オブジェクト）を静かに
    coerce せず 422 で拒否する（素の `dict[str, bool]` は `"false"`/`0`/`"yes"` を黙って
    bool 化してしまい、`tools_pref.normalize_tools_pref` の契約と食い違っていた）。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    r = c.post("/chat", json={"message": "x", "world": V, "knowledge": False,
                              "tools": {"grep": bad_value}})
    assert r.status_code == 422


def test_chat_stream_rejects_all_tools_off_422():
    """GET /chat/stream は grep/fulltext/graph 個別 query param から組み立てて同じ検証を通る。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    r = c.get("/chat/stream", params={"message": "x", "world": V,
                                      "tools_grep": False, "tools_fulltext": False, "tools_graph": False})
    assert r.status_code == 422


def test_chat_stream_tools_query_params_default_true():
    """個別 query param の既定は True（省略=全ON・422 にならない）。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    c = TestClient(app)
    r = c.get("/chat/stream", params={"message": "x", "world": V, "knowledge": False})
    assert r.status_code == 200


# ---- 検索経路トグルの可用性（実接続・SC-6e） ------------------------------------

def _unavailable(graph=False, fulltext=True):
    return {"grep": True, "fulltext": fulltext, "graph": graph}


def test_chat_tools_availability_endpoint_shape(monkeypatch):
    """GET /chat/tools-availability は `agentic_search.tool_availability()` をそのまま返す
    （UIチップの表示可否・実行側デフォルトツール構築と同じ単一の真実源）。"""
    from fastapi.testclient import TestClient
    from sherpa import agentic_search
    from sherpa.api import app
    monkeypatch.setattr(agentic_search, "tool_availability", lambda: _unavailable(graph=False))
    c = TestClient(app)
    r = c.get("/chat/tools-availability")
    assert r.status_code == 200
    assert r.json() == {"grep": True, "fulltext": True, "graph": False}


def test_chatreq_rejects_explicit_on_unavailable_tool_422(monkeypatch):
    """明示的に true 指定した検索経路が実接続で到達不可なら 422（ツール名つき・fail-loud）。"""
    from fastapi.testclient import TestClient
    from sherpa import agentic_search
    from sherpa.api import app
    ensure_v1()
    monkeypatch.setattr(agentic_search, "tool_availability", lambda: _unavailable(graph=False))
    c = TestClient(app)
    r = c.post("/chat", json={"message": "x", "world": V, "knowledge": True,
                              "tools": {"graph": True}})
    assert r.status_code == 422
    assert "graph" in r.json()["detail"]


def test_chatreq_omitted_or_off_tool_silently_uses_available_only(monkeypatch):
    """省略/False のキーは可用性チェックの対象外——不達でも 422 にせず可用分だけを使う
    （既存の「利用可能時のみ登録」契約はそのまま維持する）。"""
    from fastapi.testclient import TestClient
    from sherpa import agentic_search
    from sherpa.api import app
    ensure_v1()
    monkeypatch.setattr(agentic_search, "tool_availability", lambda: _unavailable(graph=False))
    c = TestClient(app)
    r = c.post("/chat", json={"message": "消費税率とは？", "world": V, "knowledge": True,
                              "tools": {"graph": False}})
    assert r.status_code != 422


def test_chat_stream_rejects_explicit_on_unavailable_tool_422(monkeypatch):
    """GET /chat/stream も同じ可用性判定を経由する。"""
    from fastapi.testclient import TestClient
    from sherpa import agentic_search
    from sherpa.api import app
    ensure_v1()
    monkeypatch.setattr(agentic_search, "tool_availability", lambda: _unavailable(graph=False))
    c = TestClient(app)
    r = c.get("/chat/stream", params={"message": "x", "world": V, "knowledge": True,
                                      "tools_graph": True})
    assert r.status_code == 422
    assert "graph" in r.json()["detail"]


def test_chat_turns_rejects_explicit_on_unavailable_tool_422(monkeypatch):
    """POST /chat/turns も同じ可用性判定を経由する。"""
    from fastapi.testclient import TestClient
    from sherpa import agentic_search
    from sherpa.api import app
    ensure_v1()
    monkeypatch.setattr(agentic_search, "tool_availability", lambda: _unavailable(graph=False))
    c = TestClient(app)
    r = c.post("/chat/turns", json={"message": "x", "world": V, "knowledge": True,
                                    "tools": {"graph": True}})
    assert r.status_code == 422
    assert "graph" in r.json()["detail"]


def _counting_tool_availability(monkeypatch):
    """`agentic_search.tool_availability()` の呼出回数を数える偽物に差し替える。"""
    from sherpa import agentic_search
    calls: list = []

    def _fake():
        calls.append(1)
        return {"grep": True, "fulltext": True, "graph": True}

    monkeypatch.setattr(agentic_search, "tool_availability", _fake)
    return calls


def test_chat_computes_tool_availability_snapshot_exactly_once(monkeypatch):
    """`POST /chat` は受付時422判定（`_validate_tools_availability`）と実行本体
    （`handle_message`）へ同じ snapshot を渡す——別々に取得すると TTL キャッシュの境界を挟んで
    受付時と実行時の可用性が食い違い得るため、1リクエストにつき `tool_availability()` の呼出は
    1回だけであるべき。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    ensure_v1()
    calls = _counting_tool_availability(monkeypatch)
    c = TestClient(app)
    r = c.post("/chat", json={"message": "消費税率とは？", "world": V, "knowledge": True})
    assert r.status_code != 422
    assert len(calls) == 1


def test_chat_stream_computes_tool_availability_snapshot_exactly_once(monkeypatch):
    """`GET /chat/stream` も同様——SSE closure（`gen()`）は受付時と同じ snapshot を使う。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    ensure_v1()
    calls = _counting_tool_availability(monkeypatch)
    c = TestClient(app)
    r = c.get("/chat/stream", params={"message": "消費税率とは？", "world": V, "knowledge": True})
    assert r.status_code != 422
    assert len(calls) == 1


def test_chat_turns_computes_tool_availability_snapshot_exactly_once(monkeypatch):
    """`POST /chat/turns` も同様——背景実行ファクトリ（`_turn_run_fn`）は受付時と同じ snapshot を
    そのまま転送し、背景スレッド側では再取得しない。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    ensure_v1()
    calls = _counting_tool_availability(monkeypatch)
    c = TestClient(app)
    r = c.post("/chat/turns", json={"message": "消費税率とは？", "world": V, "knowledge": True})
    assert r.status_code != 422
    assert len(calls) == 1


def _spy_target_check_then_availability(monkeypatch):
    """`routers/chat.py::_prepare_agentic_snapshot` が組み立てる Provider（`get_provider`）と
    `agentic_search.tool_availability()` の両方を差し替え、(1) `get_provider` の呼出回数、
    (2) `_agentic_target_check`→`tool_availability` の呼出順序、を記録する。返り値の
    provider インスタンスは実行本体（`handle_message`/`stream_message`/`_turn_run_fn`）が
    受け取った `provider` kwarg との同一性（`is`）比較に使う。"""
    from sherpa import agentic_search
    from sherpa.routers import chat as chat_router_mod
    order: list = []
    calls = {"get_provider": 0}

    class _FakeProvider:
        def _agentic_target_check(self) -> None:
            order.append("target_check")

    provider_obj = _FakeProvider()

    def _fake_get_provider(settings, system_settings=None):
        calls["get_provider"] += 1
        return provider_obj

    def _fake_tool_availability():
        order.append("tool_availability")
        return {"grep": True, "fulltext": True, "graph": True}

    monkeypatch.setattr(chat_router_mod, "get_provider", _fake_get_provider)
    monkeypatch.setattr(agentic_search, "tool_availability", _fake_tool_availability)
    return provider_obj, order, calls


def test_chat_shares_single_provider_snapshot_with_execution(monkeypatch):
    """`POST /chat` は Provider を一度だけ組み立て、`_agentic_target_check`→`tool_availability`
    の順で呼び、実行本体（`handle_message`）へ同一の Provider インスタンスをそのまま渡す
    ——受付（422判定）と実行本体が別々に Provider/settings を組み立てると、その間に admin 保存が
    挟まった場合に新旧混在の接続先/鍵で動きうる。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    from sherpa.routers import chat as chat_router_mod
    ensure_v1()
    provider_obj, order, calls = _spy_target_check_then_availability(monkeypatch)
    captured: dict = {}

    def _fake_handle_message(*args, **kwargs):
        captured["provider"] = kwargs.get("provider")
        return {"ok": True}

    monkeypatch.setattr(chat_router_mod, "handle_message", _fake_handle_message)
    c = TestClient(app)
    r = c.post("/chat", json={"message": "消費税率とは？", "world": V, "knowledge": True})
    assert r.status_code == 200
    assert calls["get_provider"] == 1
    assert order == ["target_check", "tool_availability"]
    assert captured["provider"] is provider_obj


def test_chat_stream_shares_single_provider_snapshot_with_execution(monkeypatch):
    """GET /chat/stream も同様——SSE closure（`gen()`）は受付時に組み立てた同一の Provider
    インスタンスをそのまま `stream_message` へ渡す。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    from sherpa.routers import chat as chat_router_mod
    ensure_v1()
    provider_obj, order, calls = _spy_target_check_then_availability(monkeypatch)
    captured: dict = {}

    def _fake_stream_message(*args, **kwargs):
        captured["provider"] = kwargs.get("provider")
        return iter(())

    monkeypatch.setattr(chat_router_mod, "stream_message", _fake_stream_message)
    c = TestClient(app)
    r = c.get("/chat/stream", params={"message": "消費税率とは？", "world": V, "knowledge": True})
    assert r.status_code == 200
    assert calls["get_provider"] == 1
    assert order == ["target_check", "tool_availability"]
    assert captured["provider"] is provider_obj


def test_chat_turns_shares_single_provider_snapshot_with_execution(monkeypatch):
    """POST /chat/turns も同様——背景実行ファクトリ（`_turn_run_fn`）は受付時に組み立てた
    同一の Provider インスタンスをそのまま受け取る（背景スレッド側では再構築しない）。
    `chat_turns.start_turn` 自体は差し替え、実際の背景スレッド起動はしない（このテストが
    検証したいのは `chat_turns_start` から `_turn_run_fn` への同一性のみ）。"""
    from fastapi.testclient import TestClient
    from sherpa import chat_turns as chat_turns_mod
    from sherpa.api import app
    from sherpa.routers import chat as chat_router_mod
    ensure_v1()
    provider_obj, order, calls = _spy_target_check_then_availability(monkeypatch)
    captured: dict = {}

    def _fake_turn_run_fn(*args, **kwargs):
        captured["provider"] = kwargs.get("provider")
        return lambda conversation_id: (lambda stop_event, emit: None)

    class _Rec:
        def __init__(self, turn_id, conversation_id):
            self.turn_id = turn_id
            self.conversation_id = conversation_id

    def _fake_start_turn(uid, conversation_factory, run_fn_factory):
        return _Rec(turn_id="fake-turn-id", conversation_id=conversation_factory())

    monkeypatch.setattr(chat_router_mod, "_turn_run_fn", _fake_turn_run_fn)
    monkeypatch.setattr(chat_turns_mod, "start_turn", _fake_start_turn)
    c = TestClient(app)
    r = c.post("/chat/turns", json={"message": "消費税率とは？", "world": V, "knowledge": True})
    assert r.status_code == 200
    assert calls["get_provider"] == 1
    assert order == ["target_check", "tool_availability"]
    assert captured["provider"] is provider_obj


class _TargetCheckRejectingProvider:
    """`_agentic_target_check` が接続先ポリシー違反（`llm.SsrfBlocked`）を送出する偽 Provider。"""

    def _agentic_target_check(self):
        from sherpa import llm
        raise llm.SsrfBlocked("許可されていない接続先です: evil.example.com:80")


def test_chat_target_check_rejection_is_422_json_not_500(monkeypatch):
    """`POST /chat`: `_agentic_target_check` が `llm.SsrfBlocked`（`PreflightRejected` 継承）を
    送出しても、未捕捉の 500 text/plain ではなく、安全な固定文言つきの 422 application/json に
    なる——生の例外文言（接続先ホスト名等）は応答へ含めない。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    from sherpa.routers import chat as chat_router_mod
    ensure_v1()
    monkeypatch.setattr(chat_router_mod, "get_provider",
                        lambda settings, **kw: _TargetCheckRejectingProvider())
    c = TestClient(app)
    r = c.post("/chat", json={"message": "x", "world": V, "knowledge": True})
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/json")
    detail = r.json()["detail"]
    assert detail
    assert "evil.example.com" not in detail


def test_chat_stream_target_check_rejection_is_422_json_not_500(monkeypatch):
    """`GET /chat/stream` も同様。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    from sherpa.routers import chat as chat_router_mod
    ensure_v1()
    monkeypatch.setattr(chat_router_mod, "get_provider",
                        lambda settings, **kw: _TargetCheckRejectingProvider())
    c = TestClient(app)
    r = c.get("/chat/stream", params={"message": "x", "world": V, "knowledge": True})
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/json")
    detail = r.json()["detail"]
    assert detail
    assert "evil.example.com" not in detail


def test_chat_turns_target_check_rejection_is_422_json_not_500(monkeypatch):
    """`POST /chat/turns` も同様（背景実行を起動する前・受付段階で弾かれる）。"""
    from fastapi.testclient import TestClient
    from sherpa.api import app
    from sherpa.routers import chat as chat_router_mod
    ensure_v1()
    monkeypatch.setattr(chat_router_mod, "get_provider",
                        lambda settings, **kw: _TargetCheckRejectingProvider())
    c = TestClient(app)
    r = c.post("/chat/turns", json={"message": "x", "world": V, "knowledge": True})
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/json")
    detail = r.json()["detail"]
    assert detail
    assert "evil.example.com" not in detail


def test_chat_settings_read_failure_propagates_as_500_not_swallowed(monkeypatch):
    """`POST /chat`: `_prepare_agentic_snapshot` 内の `store.get_settings` が失敗したら、
    フォールバックして受付を継続させず、そのまま伝播させて 500 で止める（読み取りは1回だけで
    終わる）。

    ここで例外を握りつぶして楽観的な値（例: 要求どおりの knowledge）へ倒すと、`knowledge=True`
    の受付が読み取り失敗後も継続し、実行本体（`handle_message`）が `settings=None` を受け取って
    **自分で settings を再読取**することになる。この2回目の読み取りは受付から時間が経った後
    （会話・user行の保存後）に起こるため、単一スナップショット契約（本関数が1回だけ読んだ値を
    実行本体までそのまま渡す契約）と `_agentic_target_check → tool_availability` の順序保証の
    両方を迂回してしまう——1回目だけ失敗し2回目以降は成功する偽物にすると、フォールバック実装
    では2回目の再読取が拾われて 200 で完了してしまう（読み取り回数を1回に固定して検出する）。"""
    from fastapi.testclient import TestClient
    from sherpa import store
    from sherpa.api import app
    ensure_v1()

    orig_get_settings = store.get_settings
    calls = {"n": 0}

    def _fails_once_then_recovers(uid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("DB unreachable")
        return orig_get_settings(uid)

    monkeypatch.setattr(store, "get_settings", _fails_once_then_recovers)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/chat", json={"message": "x", "world": V, "knowledge": True})
    assert r.status_code == 500
    assert calls["n"] == 1


def test_impact_scoped_narrows():
    """範囲を絞ると影響件数は増えない（要 Neo4j+PG）。"""
    try:
        ensure_v1()
        from fastapi.testclient import TestClient
        from sherpa.api import app
        c = TestClient(app)
        q = {"message": "TAX-RATE を変えたい。影響は？", "world": V, "knowledge": True}
        full = c.post("/chat", json=q)
        if full.status_code != 200:
            return _skip("infra down (Neo4j/PG 未起動)")
        scoped = c.post("/chat", json={**q, "scope_paths": [S_DESIGN]})
    except Exception as e:
        return _skip(f"infra down: {e}")
    ans = scoped.json()["message"]["answer"]
    f = full.json()["message"]["answer"]["summary"]["total"]
    s = ans["summary"]["total"]
    assert f >= 1 and s <= f
    assert ans.get("scope", {}).get("scope_paths") == [S_DESIGN]
    assert ans["scope"]["source"] == "explicit"
    assert all(scope.in_scope(src["doc_id"], [S_DESIGN]) for src in ans["sources"])
