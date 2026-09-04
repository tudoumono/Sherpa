"""管理グラフ検索/質問の単体テスト。Neo4j/LLM はスタブ。"""
from __future__ import annotations

from sherpa import agentic_search as A, graph_admin, lens_service


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows


def _row(src="module:w:src", dst="copybook:w:dst", etype="COPIES"):
    base = {
        "source_id": src, "source_name": "TAXCALC", "source_label": "Module",
        "source_em": "static", "source_status": "active", "source_value": None,
        "source_top_scope": "4期", "source_phase": "03_開発", "source_category": "01_ソース",
        "source_path": "4期/03_開発/TAXCALC.cbl",
        "target_id": dst, "target_name": "TAX-CPY", "target_label": "Copybook",
        "target_em": "static", "target_status": "active", "target_value": None,
        "target_top_scope": "4期", "target_phase": "03_開発", "target_category": "01_ソース",
        "target_path": "4期/03_開発/TAX-CPY.cpy",
        "edge_type": etype, "edge_em": "static", "edge_status": "active",
    }
    return base


class _Session:
    def __init__(self):
        self.calls = []

    def run(self, q, **kw):
        self.calls.append((q, kw))
        if "MATCH (a:Entity)-[r:" in q:
            return _Res([_row(etype="COPIES")])
        return _Res([{**_row(src="module:w:taxcalc", dst=None, etype=None),
                      "target_id": None, "target_name": None, "target_label": None,
                      "edge_type": None}])


def test_graph_search_relationship_query_and_shape():
    s = _Session()
    g = graph_admin.graph_search(s, "w", relationship_types=["copies"], scope_paths=["4期"], limit=50)
    q, kw = s.calls[0]
    assert "MATCH (a:Entity)-[r:COPIES]->(b:Entity)" in q
    assert kw["world"] == "w" and kw["prefixes"] == ["4期"] and kw["limit"] == 50
    assert {n["name"] for n in g["nodes"]} == {"TAXCALC", "TAX-CPY"}
    assert g["edges"] == [{"source": "module:w:src", "target": "copybook:w:dst",
                           "type": "COPIES", "em": "static", "status": "active"}]


def test_graph_search_condition_uses_allowlisted_field():
    s = _Session()
    g = graph_admin.graph_search(s, "w", field="role", value="Module", op="eq")
    q, kw = s.calls[0]
    assert "MATCH (n:Entity {world_id:$world})" in q
    assert "[l IN labels(n) WHERE l<>'Entity'][0]" in q
    assert kw["cond_value"] == "Module"
    assert g["nodes"][0]["type"] == "DataItem" or g["nodes"][0]["type"] == "Module"


def test_graph_search_rejects_unknown_terms():
    s = _Session()
    try:
        graph_admin.graph_search(s, "w", relationship_types=["CALLS"])
        assert False, "CALLS は実語彙ではないので拒否する"
    except ValueError as e:
        assert "unknown relationship type" in str(e)
    try:
        graph_admin.graph_search(s, "w", field="free_cypher", value="x")
        assert False, "属性名は allowlist のみ"
    except ValueError as e:
        assert "unknown condition field" in str(e)


def test_ask_graph_uses_existing_graph_tool_only(monkeypatch):
    """本テストの関心はツール配線（graph_neighbors のみを渡す・引数がそのまま渡る）であり、裏付け
    doc の機械検証（EXT-2）ではない。world "w" は実 fixture を持たないため、架空 doc_id をそのまま
    通せるよう `verify_doc_exists` を直接差し替える（検証自体は test_ext2_evidence.py の専用テスト。
    機械検証は常時実施＝TOGGLE-RM で明示 OFF の退避口を撤去済み）。
    """
    # settings に直接書いた openai_api_key を使わせるため個人キーの利用を許可する
    # （既定 false・その挙動自体は tests/unit/test_keys.py が個別に検証する）。
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    monkeypatch.setattr(A, "verify_doc_exists", lambda doc_id, world, scope_paths=None: True)
    fake_cards = [{"name": "TAXCALC", "label": "Module", "category": "ソース", "role": "実装",
                   "distance": 2, "path": ["消費税率", "TAX-RATE", "TAXCALC"],
                   "evidence": {"edges": [{"type": "REALIZES", "doc": "4期/TAX.cpy"}], "grep": []}}]
    calls = []
    bodies = []
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "graph_neighbors", "arguments": '{"name":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "TAX-RATE は TAXCALC と消費税率に関係します。"}}]},
    ]
    o_post, o_cards = A._post, lens_service.neighbor_cards
    A._post = lambda url, headers, body, timeout=90: (bodies.append(body) or seq.pop(0))
    lens_service.neighbor_cards = lambda world, term, sp=None: (calls.append((world, term, sp)) or list(fake_cards))
    try:
        res = graph_admin.ask_graph(
            "TAX-RATE の関連は？", "w", scope_paths=["4期"],
            settings={"agent": "openai", "openai_api_key": "x", "openai_model": "gpt-test"})
        names = [t["function"]["name"] for t in bodies[0]["tools"]]
        assert names == ["graph_neighbors"]                   # grep/ES/read は渡さない
        assert calls == [("w", "TAX-RATE", ["4期"])]
        assert res["status"] == "ok" and res["docs"] == ["4期/TAX.cpy"]
        assert res["cited_nodes"][0]["path"] == ["消費税率", "TAX-RATE", "TAXCALC"]
    finally:
        A._post, lens_service.neighbor_cards = o_post, o_cards


def test_ask_graph_uses_effective_agent_not_saved_when_a7_mismatches(monkeypatch):
    """保存済み agent（openai）が選択中のクラウドプロバイダ（A7・gemini）と一致しない場合、
    ask_graph は保存値をそのまま見ず effective_agent() 経由で ollama として実行する。

    観測方法: settings に `openai_api_key` を明示しても、A7 不一致では
    `keys.resolve_api_key("openai", ...)` が常に None を返す（他モジュールで固定済みの契約）ため、
    保存値をそのまま使う実装なら openai 分岐に入って honest failure（status="llm_unavailable"）に
    なるはずの設定である。effective_agent() 経由なら ollama 分岐（キー不要）に入り
    status="ok" まで到達する。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"cloud_provider": "gemini"})
    # 本テストの関心は A7 フォールバック配線であり裏付け doc の機械検証（EXT-2）ではない。
    # world "w" は実 fixture を持たないため `verify_doc_exists` を直接差し替える
    # （検証自体は test_ext2_evidence.py。機械検証は常時実施＝TOGGLE-RM で明示 OFF の退避口を撤去済み）。
    monkeypatch.setattr(A, "verify_doc_exists", lambda doc_id, world, scope_paths=None: True)
    fake_cards = [{"name": "TAXCALC", "label": "Module", "category": "ソース", "role": "実装",
                   "distance": 2, "path": ["消費税率", "TAX-RATE", "TAXCALC"],
                   "evidence": {"edges": [{"type": "REALIZES", "doc": "4期/TAX.cpy"}], "grep": []}}]
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "graph_neighbors", "arguments": '{"name":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "TAX-RATE は TAXCALC と消費税率に関係します。"}}]},
    ]
    o_post, o_cards = A._post, lens_service.neighbor_cards
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    lens_service.neighbor_cards = lambda world, term, sp=None: list(fake_cards)
    try:
        res = graph_admin.ask_graph(
            "TAX-RATE の関連は？", "w", scope_paths=["4期"],
            settings={"agent": "openai", "openai_api_key": "x", "openai_model": "gpt-test"})
        assert res["status"] == "ok"   # 保存値のまま openai 分岐に入れば key=None で llm_unavailable のはず
    finally:
        A._post, lens_service.neighbor_cards = o_post, o_cards


def test_ask_graph_invalid_cloud_provider_is_honest_llm_unavailable(monkeypatch):
    """`cloud_provider`（A7）が非空の不正値（env 誤記・旧データ等）のとき、`ask_graph` は
    `_select_provider` と同じ理由（strict な `effective_agent`）で honest failure にする＝
    黙って ollama へ倒れて実行を続けたり、黙って openai へ倒れたキーで送信したりしない。"""
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"cloud_provider": "not-a-real-provider"})
    res = graph_admin.ask_graph(
        "TAX-RATE の関連は？", "w", scope_paths=["4期"],
        settings={"agent": "openai", "openai_api_key": "x"})
    assert res["status"] == "llm_unavailable"
    assert "not-a-real-provider" in res["answer"]


def test_ask_graph_rejects_runtime_blocked_agent(monkeypatch):
    """env で有効化していない外部AI（gemini/bedrock）は、A7 が一致していても黙って実行せず
    拒否する（`_select_provider` の `runtime_blocked()` チェックと同じ・欠落すると環境で無効な
    はずの gemini/bedrock 分岐へそのまま進んで実送信してしまう）。"""
    monkeypatch.delenv("SHERPA_EXTRA_AGENTS", raising=False)   # gemini を有効化しない
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"cloud_provider": "gemini"})   # A7 は一致させる（ここが本題ではない）
    key_resolved = []
    from sherpa import keys as _keys
    orig = _keys.resolve_api_key
    def _spy(provider, *a, **kw):
        key_resolved.append(provider)
        return orig(provider, *a, **kw)
    monkeypatch.setattr(_keys, "resolve_api_key", _spy)
    res = graph_admin.ask_graph(
        "TAX-RATE の関連は？", "w", scope_paths=["4期"],
        settings={"agent": "gemini", "gemini_api_key": "x"})
    assert res["status"] == "llm_unavailable"
    assert "利用できません" in res["answer"]
    assert key_resolved == [], "無効化されているはずの gemini のキー解決が実行されている"


def test_ask_graph_bedrock_pins_base_url_against_malicious_env_override(monkeypatch):
    """ask_graph の bedrock 分岐（`providers/bedrock.py::_get_client` と兄弟の構築コード）も、
    `AnthropicBedrock` へ `base_url=_bedrock_runtime_base_url()` を明示する。省略すると SDK が
    env `ANTHROPIC_BEDROCK_BASE_URL` を読んで接続先を上書きできてしまう（`.env` の全キー export
    構成のため）——悪性 env を立てても、実際にコードが渡した base_url がその値ではなく
    正準の東京 runtime URL であることを確認する。"""
    monkeypatch.setenv("ANTHROPIC_BEDROCK_BASE_URL", "https://evil.example.com")
    monkeypatch.setenv("SHERPA_EXTRA_AGENTS", "bedrock")
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"personal_api_keys_allowed": True, "cloud_provider": "bedrock"})

    import anthropic

    calls: list = []

    class _FakeClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(anthropic, "AnthropicBedrock", _FakeClient)

    from sherpa import agentic_search as AS
    monkeypatch.setattr(AS, "anthropic_style", lambda *a, **kw: iter([]))   # 構築後は即終了させる

    graph_admin.ask_graph("TAX-RATE の関連は？", "w",
                          settings={"agent": "bedrock", "bedrock_api_key": "bkey"})
    assert len(calls) == 1
    assert calls[0]["base_url"] == "https://bedrock-runtime.ap-northeast-1.amazonaws.com"


def test_ask_graph_no_evidence_discards_llm_answer(monkeypatch):
    """根拠ノードが無いとき、LLM の文章をそのまま返さず固定の「根拠なし」回答にする（04-画面の原則.md §4）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "graph_neighbors", "arguments": '{"name":"NOEXIST"}'}}]}}]},
        {"choices": [{"message": {"content": "（根拠のない作り話の断定）NOEXIST は重要部品です。"}}]},
    ]
    o_post, o_cards = A._post, lens_service.neighbor_cards
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    lens_service.neighbor_cards = lambda world, term, sp=None: []      # グラフ近傍ゼロ＝根拠なし
    try:
        res = graph_admin.ask_graph(
            "NOEXIST の関連は？", "w",
            settings={"agent": "openai", "openai_api_key": "x", "openai_model": "gpt-test"})
        assert res["status"] == "no_graph_evidence"
        assert res["cited_nodes"] == []
        assert "作り話" not in res["answer"] and "根拠が見つかりません" in res["answer"]  # LLM 文は捨てる
    finally:
        A._post, lens_service.neighbor_cards = o_post, o_cards
