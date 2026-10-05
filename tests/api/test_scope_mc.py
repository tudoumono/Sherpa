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

V = TEST_WORLD_ID

ENDPOINTS = ["/chat/turns"]


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """このファイルはログインせず直接叩く前提（compat モード）。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


def _docs(query, **kw):
    return {h["doc_id"] for h in grep_search(query, V, **kw)}


def _skip(reason):
    pytest.skip(reason)


def _client(**kw):
    from fastapi.testclient import TestClient
    from sherpa.api import app
    return TestClient(app, **kw)


def _call(c, endpoint, *, knowledge=False, message="x", tools=None, **fields):
    """受付口（POST /chat/turns）を同じ入力で叩く。"""
    body = {"message": message, "world": V, "knowledge": knowledge, **fields}
    if tools is not None:
        body["tools"] = tools
    return c.post(endpoint, json=body)


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

@pytest.mark.parametrize("kw", [{}, {"scope_paths": ["4期"]}])
def test_grep_unscoped_and_generation_top_include_all(kw):
    assert {SPEC, TAXCALC, OPS, TAXCPY} <= _docs("TAX-RATE", **kw)


@pytest.mark.parametrize("scope_path,expected", [
    (S_SRC, {TAXCALC}),    # 鏡: 00_共通 の TAX-CPY は別フォルダ＝合流しない
    (S_DESIGN, {SPEC}),
    (S_OPS, {OPS}),        # 02_保守 は 02_保守/03_運用手順 を含む
])
def test_grep_scope_filters_by_folder_prefix(scope_path, expected):
    assert _docs("TAX-RATE", scope_paths=[scope_path]) == expected


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


def test_scopes_endpoint():
    try:
        r = _client().get("/scopes", params={"world": V})
    except Exception as e:
        return _skip(f"app import failed: {e}")
    assert r.status_code == 200
    assert any(s["path"] == S_SRC for s in r.json()["scopes"])


def test_api_rejects_unknown_scope():
    try:
        r = _call(_client(), "/chat/turns", knowledge=True, scope_paths=["存在しない/フォルダ"])
    except Exception as e:
        return _skip(f"infra down: {e}")
    assert r.status_code == 422


# ---- リクエスト項目（層・調べ方・深さ・検索経路トグル） ----------------------

def test_chatreq_accepts_scope_layer_lens_depth_profile_and_tools():
    from sherpa.api import ChatReq
    d = ChatReq(message="x")
    assert (d.scope_paths, d.layer, d.lens, d.depth_profile, d.tools) == ([], "both", None, "standard", None)
    assert ChatReq(message="x", scope_paths=["a/b", "c"]).scope_paths == ["a/b", "c"]
    for layer in ("docs", "code"):
        assert ChatReq(message="x", layer=layer).layer == layer
    for lens in ("impact", "troubleshoot", "qa", "author"):
        assert ChatReq(message="x", lens=lens).lens == lens
    for depth in ("deep", "max"):
        assert ChatReq(message="x", depth_profile=depth).depth_profile == depth
    assert ChatReq(message="x", tools=None).tools is None
    # 欠落キーは埋めない（明示 true の可用性 422 判定が省略キーまで誤検知しないため）
    assert ChatReq(message="x", tools={"grep": False}).tools == {"grep": False}


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("field,bad", [
    ("layer", "bogus"), ("lens", "bogus"), ("lens", "auto"), ("depth_profile", "bogus"),
])
def test_invalid_layer_lens_depth_profile_is_422(endpoint, field, bad):
    """pydantic の Literal 検証は DB/インフラの状態に左右されない（skip で隠さない）。"""
    r = _call(_client(), endpoint, **{field: bad})
    assert r.status_code == 422


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_all_tools_off_is_422(endpoint):
    """grep/fulltext/graph の3つとも false は 422（検索経路が0個になるのを許さない）。"""
    r = _call(_client(), endpoint, tools={"grep": False, "fulltext": False, "graph": False})
    assert r.status_code == 422


@pytest.mark.parametrize("tools", [
    {"bogus": True},
    *[{"grep": v} for v in ("false", "true", 0, 1, "yes", None, [], {})],   # StrictBool: 非 bool は coerce せず拒否
])
def test_unknown_tools_key_or_non_boolean_value_is_422(tools):
    assert _call(_client(), "/chat/turns", tools=tools).status_code == 422


# ---- 検索経路トグルの可用性（実接続） ------------------------------------

def _unavailable(graph=False, fulltext=True):
    return {"grep": True, "fulltext": fulltext, "graph": graph}


def test_chat_tools_availability_endpoint_shape(monkeypatch):
    from sherpa import agentic_search
    monkeypatch.setattr(agentic_search, "tool_availability", lambda: _unavailable(graph=False))
    r = _client().get("/chat/tools-availability")
    assert r.status_code == 200
    assert r.json() == {"grep": True, "fulltext": True, "graph": False}


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_explicit_on_unavailable_tool_is_422_with_tool_name(monkeypatch, endpoint):
    from sherpa import agentic_search
    ensure_v1()
    monkeypatch.setattr(agentic_search, "tool_availability", lambda: _unavailable(graph=False))
    r = _call(_client(), endpoint, knowledge=True, tools={"graph": True})
    assert r.status_code == 422
    assert "graph" in r.json()["detail"]


def test_omitted_or_off_tool_silently_uses_available_only(monkeypatch):
    """省略/False のキーは可用性チェックの対象外（不達でも 422 にせず可用分だけを使う）。"""
    from sherpa import agentic_search
    ensure_v1()
    monkeypatch.setattr(agentic_search, "tool_availability", lambda: _unavailable(graph=False))
    r = _call(_client(), "/chat/turns", knowledge=True, message="消費税率とは？", tools={"graph": False})
    assert r.status_code != 422


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_tool_availability_snapshot_computed_exactly_once_per_request(monkeypatch, endpoint):
    """受付時 422 判定と実行本体は同じ snapshot を使う（別取得だと TTL 境界で食い違い得る）。"""
    from sherpa import agentic_search
    ensure_v1()
    calls: list = []

    def _fake():
        calls.append(1)
        return {"grep": True, "fulltext": True, "graph": True}

    monkeypatch.setattr(agentic_search, "tool_availability", _fake)
    r = _call(_client(), endpoint, knowledge=True, message="消費税率とは？")
    assert r.status_code != 422
    assert len(calls) == 1


def _spy_target_check_then_availability(monkeypatch):
    """`get_provider` と `tool_availability` を差し替え、get_provider の呼出回数・
    `_agentic_target_check`→`tool_availability` の順序を記録する。"""
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


def _capture_execution_provider(monkeypatch, endpoint, captured):
    """実行本体（_turn_run_fn）が受け取る provider を捕まえる。"""
    from sherpa import chat_turns as chat_turns_mod
    from sherpa.routers import chat as chat_router_mod

    # start_turn も差し替え、背景スレッドは起動しない（受付→_turn_run_fn の同一性だけを見る）
    def _fake_turn_run_fn(*args, **kwargs):
        captured["provider"] = kwargs.get("provider")
        return lambda conversation_id: (lambda stop_event, emit: None)

    class _Rec:
        def __init__(self, turn_id, conversation_id):
            self.turn_id = turn_id
            self.conversation_id = conversation_id

    def _fake_start_turn(uid, conversation_factory, run_fn_factory, known_conversation_id=None):
        return _Rec(turn_id="fake-turn-id", conversation_id=conversation_factory())

    monkeypatch.setattr(chat_router_mod, "_turn_run_fn", _fake_turn_run_fn)
    monkeypatch.setattr(chat_turns_mod, "start_turn", _fake_start_turn)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_single_provider_snapshot_is_shared_with_execution(monkeypatch, endpoint):
    """Provider は受付で一度だけ組み立て、`_agentic_target_check`→`tool_availability` の順で呼び、
    実行本体へ同一インスタンスを渡す（受付と実行で別々に組むと admin 保存を挟んで新旧混在し得る）。"""
    ensure_v1()
    provider_obj, order, calls = _spy_target_check_then_availability(monkeypatch)
    captured: dict = {}
    _capture_execution_provider(monkeypatch, endpoint, captured)
    r = _call(_client(), endpoint, knowledge=True, message="消費税率とは？")
    assert r.status_code == 200
    assert calls["get_provider"] == 1
    assert order == ["target_check", "tool_availability"]
    assert captured["provider"] is provider_obj


class _TargetCheckRejectingProvider:
    """`_agentic_target_check` が接続先ポリシー違反（`llm.SsrfBlocked`）を送出する偽 Provider。"""

    def _agentic_target_check(self):
        from sherpa import llm
        raise llm.SsrfBlocked("許可されていない接続先です: evil.example.com:80")


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_target_check_rejection_is_422_json_not_500(monkeypatch, endpoint):
    """接続先ポリシー違反は未捕捉の 500 でなく固定文言の 422 application/json（生の例外文言＝
    接続先ホスト名は応答へ含めない）。"""
    from sherpa.routers import chat as chat_router_mod
    ensure_v1()
    monkeypatch.setattr(chat_router_mod, "get_provider",
                        lambda settings, **kw: _TargetCheckRejectingProvider())
    r = _call(_client(), endpoint, knowledge=True)
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/json")
    detail = r.json()["detail"]
    assert detail
    assert "evil.example.com" not in detail


def test_chat_settings_read_failure_propagates_as_500_not_swallowed(monkeypatch):
    """`_prepare_agentic_snapshot` 内の `store.get_settings` 失敗はフォールバックせず 500 で止める。
    握りつぶすと実行本体が settings を再読取し、単一スナップショット契約と
    `_agentic_target_check → tool_availability` の順序保証を迂回する（1 回目だけ失敗する偽物で、
    読み取り回数が 1 回であることも固定する）。"""
    from sherpa import store
    ensure_v1()

    orig_get_settings = store.get_settings
    calls = {"n": 0}

    def _fails_once_then_recovers(uid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("DB unreachable")
        return orig_get_settings(uid)

    monkeypatch.setattr(store, "get_settings", _fails_once_then_recovers)
    r = _call(_client(raise_server_exceptions=False), "/chat/turns", knowledge=True)
    assert r.status_code == 500
    assert calls["n"] == 1


def test_impact_scoped_narrows():
    """範囲を絞ると影響件数は増えない（要 Neo4j+PG）。"""
    from sherpa import chat_service
    from sherpa.deps import neo4j_session

    def _answer(s, **kw):
        for ev in chat_service.stream_message(s, "TAX-RATE を変えたい。影響は？", V, knowledge=True,
                                              user_id="admin", **kw):
            if ev.get("type") == "answer":
                return ev["message"]["answer"]
        return None

    try:
        ensure_v1()
        with neo4j_session() as s:
            f = _answer(s)
            if f is None:
                return _skip("infra down (Neo4j/PG 未起動)")
            ans = _answer(s, scope_paths=[S_DESIGN])
    except Exception as e:
        return _skip(f"infra down: {e}")
    assert ans is not None
    ft = f["summary"]["total"]
    s_total = ans["summary"]["total"]
    assert ft >= 1 and s_total <= ft
    assert ans.get("scope", {}).get("scope_paths") == [S_DESIGN]
    assert ans["scope"]["source"] == "explicit"
    assert all(scope.in_scope(src["doc_id"], [S_DESIGN]) for src in ans["sources"])
