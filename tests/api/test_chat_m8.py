"""M8 受け入れ: チャット主入口（意図でレンズ振り分け＋会話DB永続＋答え先頭＋出典）。

- router 単体は依存ゼロ（Neo4j/PG 不要）。
- 会話フローは要 Neo4j（graph-load 済）＋ Postgres 起動。
"""
from __future__ import annotations

import os
import pathlib

import pytest
from _world_setup import TEST_WORLD_ID, ensure_v1
from sherpa.chat_router import route

os.environ.setdefault("SHERPA_STREAM_PACE", "0")    # SSE は即時（テスト高速化）

ROOT = pathlib.Path(__file__).resolve().parents[2]
V = TEST_WORLD_ID   # 旧固定 'v1' から移行（2026-07-03 インシデント対応 HIGH#2・_world_setup.py 参照）


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """このファイルはログインせず直接叩く前提（compat モード）。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")

# テストのベースラインを heuristic に固定（デモが残した設定行に左右されないように）。PG 未起動なら無視。
# 重要: API/テストは同じ user_id="admin" の設定行を使うため、テストが**実ユーザの設定（APIキー等）を壊さない**よう
# 取り込み前にスナップショットを取り、プロセス終了時に元へ戻す（atexit）。
import atexit  # noqa: E402

try:
    from sherpa import store as _store
    _ORIG_SETTINGS = _store.get_settings()                     # 実設定を退避

    def _restore_settings():
        f = {}
        for k in _store._SETTINGS_FIELDS:
            v = _ORIG_SETTINGS.get(k)
            if k in ("openai_api_key", "gemini_api_key"):
                f[k] = v if v else ""                         # None→""（クリア）で元の状態に一致
            elif v is not None:
                f[k] = v
        try:
            _store.update_settings(**f)
        except Exception:
            pass

    atexit.register(_restore_settings)
    _store.update_settings(agent="heuristic")
except Exception:
    pass


# ---- ルーティング（依存ゼロ） -------------------------------------------

def test_router_picks_lens_and_start():
    assert route("消費税率を変えたい。影響は？")["lens"] == "impact"
    assert route("夜間バッチ NIGHTLY が ABEND。原因は？")["lens"] == "troubleshoot"
    assert route("消費税の端数処理の仕様は？")["lens"] == "qa"
    r = route("消費税率を変更したい", V, known_terms=["消費税率", "税率", "TAX-RATE"])
    assert r["lens"] == "impact" and r["input"] == "消費税率"


# ---- 会話フロー（要 Neo4j＋Postgres） -----------------------------------

def _client():
    from fastapi.testclient import TestClient
    from sherpa.api import app
    return TestClient(app)


def test_chat_persists_and_answers_answer_first():
    """S3（2026-09-04-グラフのソース正典化.md §4・K9-K11）: REALIZES 橋の撤去により業務語
    「消費税率」は構造的に解決しない——起点はコード自身の識別子（`TAX-RATE`）を使う。"""
    ensure_v1()
    c = _client()
    # 既定はナレッジ参照オフ（素の会話）。社内資料に基づく回答は knowledge=True で要求する。
    r = c.post("/chat", json={"message": "TAX-RATE を変えたい。影響は？", "world": V, "knowledge": True}).json()
    cid = r["conversation_id"]
    ans = r["message"]["answer"]
    assert ans["lens"] == "impact"
    assert "影響" in ans["headline"] and ans["summary"]["total"] >= 1   # 答え先頭＋件数
    assert ans["sources"], "出典（原本DL）が付いていない"
    assert ans["sources"][0]["download_url"].startswith("/documents/")

    # 同じ会話を継続（別レンズ）→ 会話に積み上がる
    r2 = c.post("/chat", json={"message": "端数処理の仕様は？", "world": V,
                               "conversation_id": cid, "knowledge": True}).json()
    assert r2["conversation_id"] == cid and r2["message"]["answer"]["lens"] == "qa"

    conv = c.get(f"/conversations/{cid}").json()
    roles = [m["role"] for m in conv["messages"]]
    assert roles.count("user") == 2 and roles.count("assistant") == 2


def test_chat_routes_troubleshoot():
    ensure_v1()
    c = _client()
    r = c.post("/chat", json={"message": "夜間バッチ NIGHTLY が ABEND。原因候補は？",
                              "world": V, "knowledge": True}).json()
    ans = r["message"]["answer"]
    assert ans["lens"] == "troubleshoot" and ans["summary"]["total"] >= 1


def test_knowledge_off_is_plain_chat():
    """既定（ナレッジ参照オフ）＝検索せず素の会話。レンズ=chat・出典なし・範囲=off。"""
    c = _client()
    ans = c.post("/chat", json={"message": "こんにちは", "world": V}).json()["message"]["answer"]
    assert ans["lens"] == "chat" and ans["sources"] == [] and ans["scope"]["source"] == "off"


def _stream(c, message, knowledge=True):
    import json
    nodes, ans = [], None
    params = {"message": message, "world": V, "knowledge": "true" if knowledge else "false"}
    with c.stream("GET", "/chat/stream", params=params) as s:
        for line in s.iter_lines():
            if line and line.startswith("data: "):
                e = json.loads(line[6:])
                if e["type"] == "node" and e["status"] == "done":
                    nodes.append((e["id"], e["kind"]))
                elif e["type"] == "answer":
                    ans = e
    return nodes, ans


def test_chat_stream_dynamic_nodes():
    """SSE: 思考/ツールのノードを動的に流し、最後に answer を返す（M9・provider 非依存）。"""
    ensure_v1()
    c = _client()
    nodes, ans = _stream(c, "消費税率を変えたい。影響は？")
    ids = [n for n, _ in nodes]
    assert ids == ["understand", "intent", "tool-graph", "compose"]      # 影響＝ツール1
    assert any(k == "tool" for _, k in nodes)
    assert ans and ans["message"]["answer"]["lens"] == "impact"

    # troubleshoot は使うツールが2つ＝ノードが動的に増える（固定でない証拠）
    t_nodes, _ = _stream(c, "夜間バッチ NIGHTLY が ABEND。原因は？")
    tools = [n for n, k in t_nodes if k == "tool"]
    assert tools == ["tool-graph", "tool-docs"]


def test_chat_stream_saves_trace_and_conversation_load_returns_it():
    """S3: SSE で流れた node が messages.trace に保存され、会話再取得（GET /conversations/{id}）でも
    そのまま返る（履歴の「思考の流れ」を右ペインへ静的復元するための土台・messages API 側の確認）。"""
    ensure_v1()
    c = _client()
    nodes, ans = _stream(c, "消費税率を変えたい。影響は？")
    msg = ans["message"]
    trace = msg.get("trace")
    assert trace, "trace が保存されていない"
    assert [n["id"] for n in trace] == [nid for nid, _kind in nodes]   # id は初出順・重複更新は dedup

    cid = ans["conversation_id"]
    conv = c.get(f"/conversations/{cid}").json()
    saved = next(m for m in conv["messages"] if m["role"] == "assistant")
    assert saved["trace"] == trace                                     # 再取得でも同じ trace が返る
    user_msg = next(m for m in conv["messages"] if m["role"] == "user")
    assert not user_msg.get("trace")                                   # user ターンには trace は付かない


# ---- バッチ2・2番（2026-07-03）: trace 保存の全経路検証 ----
# ユーザー報告「復元されるチャットとされないチャットがある」を受け、新規ターンで trace が
# 漏れる経路が無いかを網羅的に確認する（実装日以前のターンに trace が無いのは仕様＝対象外）。
# 経路×保存有無は最終報告の表にまとめる。

def test_chat_non_streaming_knowledge_on_saves_trace():
    """POST /chat（非ストリーミング）・ナレッジ参照ON。"""
    ensure_v1()
    c = _client()
    r = c.post("/chat", json={"message": "消費税率を変えたい。影響は？", "world": V, "knowledge": True}).json()
    trace = r["message"].get("trace")
    assert trace, "非ストリーミング・knowledge=True で trace が保存されていない"
    assert any(n["id"] == "understand" for n in trace)


def test_chat_non_streaming_knowledge_off_saves_trace():
    """POST /chat（非ストリーミング）・ナレッジ参照OFF（素の会話・_plain_run 経路）。"""
    c = _client()
    r = c.post("/chat", json={"message": "こんにちは", "world": V}).json()
    trace = r["message"].get("trace")
    assert trace, "非ストリーミング・knowledge=False（_plain_run）で trace が保存されていない"
    assert any(n["id"] == "brain" for n in trace)


def test_chat_stream_knowledge_off_saves_trace():
    """GET /chat/stream（SSE）・ナレッジ参照OFF（素の会話・_plain_run 経路）。
    ストリーミングは knowledge=True のみ既存テストで確認済み（test_chat_stream_saves_trace_...）。"""
    nodes, ans = _stream(_client(), "こんにちは", knowledge=False)
    trace = ans["message"].get("trace")
    assert trace, "ストリーミング・knowledge=False（_plain_run）で trace が保存されていない"
    assert [n["id"] for n in trace] == [nid for nid, _kind in nodes]


def test_chat_stream_clarify_persists_question_card_as_assistant_message():
    """S1（ask_user-improvements.md）: clarify（question イベント）で終わったターンは、確認カードを
    assistant メッセージとして**永続化**する（従来は保存しない設計だったが、履歴を開き直しても後から
    答えられるように保存へ変更）。answer は {"lens":"clarify","question":{...}}・content=prompt・
    trace も保存される（clarify ターンでも「思考の流れ」を復元できる副産物）。answer イベントは出さない。"""
    import json

    from sherpa import intent_llm
    orig = intent_llm.classify
    intent_llm.classify = lambda *a, **k: None   # Tier2 未接続化（[[feedback_intent_tier2_shared_dev_key_leak]]）
    try:
        c = _client()
        params = {"message": "税率を変えたら夜間バッチが落ちる？", "world": V, "knowledge": "true"}
        events = []
        with c.stream("GET", "/chat/stream", params=params) as s:
            for line in s.iter_lines():
                if line and line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        q_ev = next((e for e in events if e["type"] == "question"), None)
        assert q_ev, "clarify（question イベント）が発生しなかった"
        assert not any(e["type"] == "answer" for e in events), "clarify なのに answer が生成されている"
        cid = q_ev["conversation_id"]
        conv = c.get(f"/conversations/{cid}").json()
        assistant = [m for m in conv["messages"] if m["role"] == "assistant"]
        assert len(assistant) == 1, "確認カードが assistant メッセージとして保存されていない（S1）"
        saved = assistant[0]
        assert saved["answer"]["lens"] == "clarify"
        # 保存された question payload はフロントの askcard 再構築に必要な要素を持つ。
        sq = saved["answer"]["question"]
        assert sq["interaction_id"] == q_ev["interaction_id"]
        assert sq["prompt"] == q_ev["prompt"] and sq["options"]
        assert sq["original_message"] == "税率を変えたら夜間バッチが落ちる？"   # 再送フォーマット（元の依頼）
        assert saved["content"] == q_ev["prompt"]                              # content=prompt
        assert saved["trace"], "clarify ターンの trace が保存されていない（思考の流れの復元）"
    finally:
        intent_llm.classify = orig


def test_chat_stream_agentic_ask_user_question_persists_with_correct_shape():
    """S1・Low（Codex RV 2026-07-07）: 上のテストは router clarify（`chat_router.clarify_question` が
    自前で `original_message` を持つ）だけを固定していたため、agentic ask_user（OpenAI/Gemini/Bedrock
    経路・`agentic_search._question_from_args` が interaction_id/options を生成するが
    **original_message は持たない**）由来の question イベントの保存形が退行し得た。
    ここでは `_question_from_args` が実際に返す形（フェイク provider 経由）で、保存された
    answer.question に original_message が chat_service 側の setdefault で補完されること・
    interaction_id/options がそのまま保存されることを確認する。"""
    from sherpa import agentic_search, chat_service

    class _FakeAgenticProvider:
        label, model = "Fake Agentic", "fake-agentic-1"

        def run(self, ctx):
            yield {"type": "node", "id": "tool-1", "kind": "tool", "label": "資料を検索（語句そのまま）",
                  "detail": "「TAX-RATE」", "status": "done"}
            q = agentic_search._question_from_args({
                "mode": "single", "prompt": "対象範囲を教えてください",
                "options": [{"id": "a", "label": "A案"}, {"id": "b", "label": "B案"}],
                "allow_free_text": True,
            })
            assert "original_message" not in q, \
                "前提が崩れている: _question_from_args が original_message を持つようになった"
            yield q

    orig = chat_service.get_provider
    chat_service.get_provider = lambda settings, **kw: _FakeAgenticProvider()
    try:
        events = list(chat_service.stream_message(
            None, "TAX-RATE の対象範囲を教えて", V, None, knowledge=False, user_id="admin"))
        q_ev = next(e for e in events if e["type"] == "question")
        cid = q_ev["conversation_id"]

        from sherpa import store
        conv = store.get_conversation(cid)
        saved = next(m for m in conv["messages"] if m["role"] == "assistant")
        assert saved["answer"]["lens"] == "clarify"
        sq = saved["answer"]["question"]
        assert sq["interaction_id"] == q_ev["interaction_id"]
        assert sq["options"] == [{"id": "a", "label": "A案", "description": ""},
                                 {"id": "b", "label": "B案", "description": ""}]
        # agentic 由来の payload には無い original_message が message（元の依頼）で補完される。
        assert sq["original_message"] == "TAX-RATE の対象範囲を教えて"
        assert saved["content"] == "対象範囲を教えてください"
        assert saved.get("trace"), "agentic 経路の clarify ターンでも trace が保存されるはず"
    finally:
        chat_service.get_provider = orig


def test_chat_stream_codex_ask_user_question_persists_with_s1_shape():
    """S2（ask_user-improvements.md）: Codex の ask_user（MCP mcp_tool_call）をラッパーが
    question イベントへ丸めた出力（`agents._codex_ask_question`＝`_question_from_args` 経由）が、
    chat_service を通って S1 の保存形（answer.question・interaction_id・options・original_message 補完）で
    永続化されることを確認する（統合1本）。Codex サブプロセスは起動せず、ラッパーが --json の
    mcp_tool_call(ask_user) item から作る question を実際の生成器で流す。"""
    from sherpa import agents, chat_service

    # Codex の --json が出す ask_user の mcp_tool_call item（ラッパーはこの item から question を作る）。
    item = {"tool": "ask_user", "arguments": {
        "mode": "single", "prompt": "Excel の列構成はどれにしますか？",
        "options": [{"id": "a", "label": "月別×金額"}, {"id": "b", "label": "案件別×工数"}],
        "allow_free_text": True}}

    class _FakeCodexProvider:
        label, model = "Codex", "gpt-5.5"

        def run(self, ctx):
            yield {"type": "node", "id": "cx-1", "kind": "tool", "label": "ユーザに確認",
                   "detail": "「Excel の列構成はどれにしますか？」", "status": "done"}
            q = agents._codex_ask_question(item)
            assert q and "original_message" not in q       # 生成直後は持たない＝chat_service が補完する前提
            yield q                                          # ラッパーは question 優先＝ここで停止（_result 無し）

    orig = chat_service.get_provider
    chat_service.get_provider = lambda settings, **kw: _FakeCodexProvider()
    try:
        # knowledge=False（Neo4j 非依存）で回す。question の保存分岐（S1）は knowledge に依らず、
        # provider を差し替えているため Codex 実プロセス/ナレッジ参照の有無とは独立に検証できる
        # （姉妹テスト test_chat_stream_agentic_ask_user_question_persists_with_correct_shape と同流儀）。
        events = list(chat_service.stream_message(
            None, "案件一覧を Excel にまとめて", V, None, knowledge=False, user_id="admin"))
        q_ev = next(e for e in events if e["type"] == "question")
        cid = q_ev["conversation_id"]

        from sherpa import store
        conv = store.get_conversation(cid)
        saved = next(m for m in conv["messages"] if m["role"] == "assistant")
        assert saved["answer"]["lens"] == "clarify"
        sq = saved["answer"]["question"]
        assert sq["interaction_id"] == q_ev["interaction_id"]
        assert sq["mode"] == "single" and sq["allow_free_text"] is True
        assert sq["options"] == [{"id": "a", "label": "月別×金額", "description": ""},
                                 {"id": "b", "label": "案件別×工数", "description": ""}]
        assert sq["original_message"] == "案件一覧を Excel にまとめて"   # chat_service が元の依頼で補完
        assert saved["content"] == "Excel の列構成はどれにしますか？"
    finally:
        chat_service.get_provider = orig


def test_chat_trace_capture_is_provider_agnostic():
    """chat_service の trace 捕捉（type=="node" かつ id を持つイベントを蓄積）は特定 provider に
    依存しない一般ロジックであることを、heuristic とは別の provider（フェイク）で確認する。"""
    from sherpa import chat_service

    class _FakeProvider:
        label, model = "Fake LLM", "fake-1"

        def run(self, ctx):
            yield {"type": "node", "id": "n1", "kind": "think", "label": "考える", "detail": "a", "status": "active"}
            yield {"type": "node", "id": "n1", "kind": "think", "label": "考える", "detail": "b", "status": "done"}
            yield {"type": "_result",
                  "env": {"lens": "chat", "headline": "フェイク回答", "summary": {"total": 0}, "data": {},
                          "sources": [], "scope": {"world": ctx.world, "scope_paths": [], "source": "off"}},
                  "decision": {"lens": "chat", "input": ctx.message, "reason": "fake"}}

    orig = chat_service.get_provider
    chat_service.get_provider = lambda settings, **kw: _FakeProvider()
    try:
        events = list(chat_service.stream_message(None, "任意の質問", V, None, knowledge=False, user_id="admin"))
        ans = next(e for e in events if e["type"] == "answer")
        trace = ans["message"].get("trace")
        assert trace and trace[0]["id"] == "n1" and trace[0]["detail"] == "b", \
            "フェイク provider の node イベントが trace に捕捉されていない（provider 非依存であるべき）"
    finally:
        chat_service.get_provider = orig


def test_chat_history_primes_next_turn_via_db_after_provider_reinstantiation():
    """R1a（会話継続・履歴 priming）: turn1 と turn2 を**別の** fake provider インスタンス（クラスも別）
    で実行し、turn2 の `ctx.history` に turn1 の (user, assistant) 対が入っていることを確認する。

    2つの fake provider クラスはプロセス内で状態を共有しないため、turn2 が turn1 の内容を知り得る
    経路は DB round-trip（`store.add_message`→`store.recent_messages`）だけ＝「会話継続はプロセス内の
    provider インスタンス状態ではなく DB から再構築される」ことの証明になる（API 再起動があっても
    継続する、の実質的な等価物・store-roundtrip 流儀 test_shares_store_roundtrip.py と同じ考え方）。
    """
    from sherpa import chat_service

    captured: dict = {}

    class _FakeProviderTurn1:
        label, model = "Fake Turn1", "fake-t1"

        def run(self, ctx):
            assert ctx.history == [], "新規会話の1ターン目に履歴が入ってはいけない"
            yield {"type": "_result",
                  "env": {"lens": "chat", "headline": "最初の回答です", "summary": {"total": 0},
                          "data": {}, "sources": [], "scope": {"world": ctx.world, "scope_paths": [], "source": "off"}},
                  "decision": {"lens": "chat", "input": ctx.message, "reason": "fake-t1"}}

    class _FakeProviderTurn2:
        label, model = "Fake Turn2", "fake-t2"

        def run(self, ctx):
            captured["history"] = ctx.history
            yield {"type": "_result",
                  "env": {"lens": "chat", "headline": "2番目の回答です", "summary": {"total": 0},
                          "data": {}, "sources": [], "scope": {"world": ctx.world, "scope_paths": [], "source": "off"}},
                  "decision": {"lens": "chat", "input": ctx.message, "reason": "fake-t2"}}

    orig = chat_service.get_provider
    c = _client()
    try:
        chat_service.get_provider = lambda settings, **kw: _FakeProviderTurn1()
        r1 = c.post("/chat", json={"message": "最初の質問です", "world": V}).json()
        cid = r1["conversation_id"]

        chat_service.get_provider = lambda settings, **kw: _FakeProviderTurn2()
        r2 = c.post("/chat", json={"message": "続けて教えて", "world": V, "conversation_id": cid}).json()
        assert r2["conversation_id"] == cid
    finally:
        chat_service.get_provider = orig

    assert captured["history"] == [
        {"role": "user", "content": "最初の質問です"},
        {"role": "assistant", "content": "最初の回答です"},
    ]


def test_provider_seam_uniform(monkeypatch):
    """頭脳を差し替えても可視化（node→answer）の形は不変。未接続 provider は正直に返す。

    RV 派生修正（2026-07-14 フェーズ7）: 「キー無し」の前提を環境任せにせず自分で統制する。
    `_select_provider` は settings のキーが空だと env `OPENAI_API_KEY` へフォールバックするが、
    プロセス env は**テスト実行順に依存して汚染される**ことが実測で判明した（GET /admin/settings 系の
    handler が markitdown_ocr_arm を遅延 import → 依存の magika が import 時に `load_dotenv()` を実行し
    リポジトリの `.env`（dev では OPENAI_API_KEY=sk-REPLACE_ME）を親ディレクトリ探索で注入する）。
    """
    from sherpa import store
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store.update_settings(agent="openai", openai_api_key="")   # キー無し＝未接続
    try:
        c = _client()
        nodes, ans = _stream(c, "何でもいい")
        assert ans and "接続されていません" in ans["message"]["answer"]["headline"]  # 嘘の回答をしない
        assert nodes and ans["type"] == "answer"                                    # プロトコルは同一
    finally:
        store.update_settings(agent="heuristic")


def test_created_files_persisted_in_answer_and_triggers_personal_flag():
    """P1-c（Codex 強化計画 Phase1）: env['created_files']（fake Codex 風 provider）が answer JSONB に
    保存され、chat 応答に載る。既存の codex_wrote_files 連動で contains_personal_workspace も立ち、
    共有不可ゲートが created_files 追加後も従来どおり効くことを確認する（provider 非依存の配線確認・
    test_chat_trace_capture_is_provider_agnostic と同じ流儀）。"""
    ensure_v1()
    from sherpa import store
    from sherpa.routers import chat as chat_routes

    class _FakeCodexProvider:
        label, model = "Fake Codex", "fake-codex"

        def _agentic_target_check(self):   # SC-6e: knowledge=True は受付段階で必ず呼ばれる（no-op）
            return None

        def run(self, ctx):
            yield {"type": "node", "id": "n1", "kind": "think", "label": "Codex が調べる",
                  "detail": "作成しました", "status": "done"}
            yield {"type": "_result",
                  "env": {"lens": "author", "headline": "資料を作成しました。",
                          "summary": {"total": 0}, "data": {}, "sources": [],
                          "scope": {"world": ctx.world, "scope_paths": [], "source": "all"},
                          "route": {"lens": "author", "reason": "fake", "input": ctx.message},
                          "codex_wrote_files": ["消費税率一覧.xlsx"],
                          "created_files": [{"name": "消費税率一覧.xlsx",
                                              "download_url": "/workspace/files/123/download"}]},
                  "decision": {"lens": "author", "input": ctx.message, "reason": "fake"}}

    # SC-6e: knowledge=True の実HTTP入口は受付段階（`_prepare_agentic_snapshot`）で Provider を
    # 一度だけ組み立て実行本体まで渡すため、差し替え対象は `chat_service.get_provider` でなく
    # ルータ側の束縛（`sherpa.routers.chat.get_provider`）——`handle_message` 自身はもう
    # get_provider を呼ばない（受け取った provider をそのまま使う）。
    orig = chat_routes.get_provider
    chat_routes.get_provider = lambda settings, **kw: _FakeCodexProvider()
    try:
        c = _client()
        r = c.post("/chat", json={"message": "消費税率の一覧をExcelにまとめて", "world": V, "knowledge": True}).json()
        ans = r["message"]["answer"]
        assert ans["created_files"] == [
            {"name": "消費税率一覧.xlsx", "download_url": "/workspace/files/123/download"}]

        cid = r["conversation_id"]
        conv_data = store.get_conversation_for_read("admin", cid)
        assert conv_data["conversation"].get("contains_personal_workspace") is True, \
            "created_files を伴う会話が共有不可フラグ（contains_personal_workspace）を立てていない"

        # 共有不可ゲートが実際に効く（既存の Feature C 仕様の再確認・created_files 追加後も壊れていない）。
        share_r = c.post(f"/conversations/{cid}/shares", json={"invitee_user_ids": []})
        assert share_r.status_code == 409, f"personal 会話の共有が拒否されなかった: {share_r.status_code} {share_r.text}"
    finally:
        chat_routes.get_provider = orig


def test_conversation_pin_and_delete():
    """#8 ピン止め（一覧で pinned/上部）・タイトル変更・#6 削除（404化・連鎖）。"""
    c = _client()
    cid = c.post("/chat", json={"message": "x", "world": V}).json()["conversation_id"]
    assert c.patch(f"/conversations/{cid}", json={"title": "新しい名前"}).json()["title"] == "新しい名前"
    assert c.get(f"/conversations/{cid}").json()["conversation"]["title"] == "新しい名前"
    assert c.patch(f"/conversations/{cid}", json={"title": "   "}).status_code == 422   # 空は拒否
    assert c.post(f"/conversations/{cid}/pin", json={"pinned": True}).json()["pinned"] is True
    assert any(x["id"] == cid and x["pinned"] for x in c.get("/conversations").json())
    assert c.post(f"/conversations/{cid}/pin", json={"pinned": False}).json()["pinned"] is False
    assert c.delete(f"/conversations/{cid}").json()["ok"] is True
    assert c.get(f"/conversations/{cid}").status_code == 404      # 連鎖で消えた
    assert c.delete(f"/conversations/{cid}").status_code == 404   # 二重削除は 404


def test_system_prompt_setting(monkeypatch):
    """#2 システムプロンプト: 保存→GETで返る／provider が system メッセージに前置する。"""
    from sherpa import store
    from sherpa.agents import get_provider
    # インラインの openai_api_key を実際に使わせるには personal_api_keys_allowed が要る
    # （既定 false・`sherpa.keys.resolve_api_key`）。WEB-1: `get_provider()` の1ターン唯一の
    # 読取点は `store._read_system_settings_fresh()`（共有キャッシュを介さない生の読取・
    # TOCTOU 対策）——`get_system_settings` ではない。
    monkeypatch.setattr("sherpa.store._read_system_settings_fresh", lambda: {"personal_api_keys_allowed": True})
    c = _client()
    c.put("/settings", json={"system_prompt": "必ず簡潔に答えてください。"})
    assert c.get("/settings").json()["system_prompt"] == "必ず簡潔に答えてください。"
    p = get_provider({**store.get_settings(), "agent": "openai", "openai_api_key": "x"})
    assert p.system_prompt == "必ず簡潔に答えてください。"
    assert p._messages("Q")[0] == {"role": "system", "content": "必ず簡潔に答えてください。"}
    store.update_settings(system_prompt=store.DEFAULT_SYSTEM_PROMPT)   # 後片付け（既定へ）


def test_settings_web_search_available_flag_reflects_admin_setting():
    """WEB-1: web_search は既定 OFF。GET /settings の web_search_available は
    system_settings.web_search_allowed（管理画面「プロバイダ＋接続先」タブ）をそのまま映す
    （env `SHERPA_ALLOW_WEB_SEARCH` は初回シードのみ・実行時にはもう見ない）。"""
    from sherpa import store
    c = _client()
    store.set_system_settings("admin-uid", {"web_search_allowed": None})   # 未設定へ戻す（既定 false）
    try:
        s = c.get("/settings").json()
        assert s["web_search_available"] is False, "未設定なのに利用可能フラグが立っている"

        store.set_system_settings("admin-uid", {"web_search_allowed": True})
        s2 = c.get("/settings").json()
        assert s2["web_search_available"] is True, "管理者許可時にフラグが立たない"
    finally:
        store.set_system_settings("admin-uid", {"web_search_allowed": None})   # 後片付け（既定へ）


def test_settings_codex_web_search_saved_as_bool_and_kept_when_admin_off():
    """codex_web_search は bool として保存/取得できる（不正型は422）。列は互換のため残すが、
    WEB-1 以降この保存値は実行経路（Codex 実行時の config 生成）では読まない
    （チャットごとの `ChatReq.web_search` のみを見る）——管理者許可が無くても保存値自体は消さない。"""
    from sherpa import store
    c = _client()
    store.set_system_settings("admin-uid", {"web_search_allowed": None})   # 未設定（既定 false）のまま
    try:
        r = c.put("/settings", json={"codex_web_search": True})
        assert r.status_code == 200, r.text
        assert c.get("/settings").json()["codex_web_search"] is True, \
            "管理者未許可でも保存値自体は残るはず（消さない契約）"

        bad = c.put("/settings", json={"codex_web_search": "yes-please"})
        assert bad.status_code == 422, bad.text
    finally:
        store.update_settings(codex_web_search=False)   # 後片付け（既定へ）


def test_settings_codex_web_search_rejects_loosely_coercible_values():
    """RV LOW 4: codex_web_search は StrictBool＝JSON の真偽値のみ許可する。ネット到達可否を
    左右するフラグなので、pydantic 既定の緩い型強制（"true"/"1"/1/0 等の bool 化）を受理しない
    （通常の bool フィールドなら通ってしまう値で 422 になることを確認する）。"""
    from sherpa import store
    c = _client()
    try:
        for loose in ("true", "false", "1", "0", "yes", "no", 1, 0):
            r = c.put("/settings", json={"codex_web_search": loose})
            assert r.status_code == 422, f"{loose!r} は緩い型強制で受理されるべきでない: {r.status_code}"
        # 本物の JSON boolean は引き続き受理される。
        assert c.put("/settings", json={"codex_web_search": True}).status_code == 200
    finally:
        store.update_settings(codex_web_search=False)   # 後片付け（既定へ）


def test_guards_version_and_conversation():
    """版IDのパストラバーサル拒否（422）と不正会話IDの 404（500にしない）。"""
    c = _client()
    assert c.post("/chat", json={"message": "x", "world": "../../etc"}).status_code == 422
    assert c.post("/chat", json={"message": "x", "conversation_id": 99999999}).status_code == 404


def test_chat_turn_audit_recorded_without_body():
    """S5: /chat の1ターン完了時に chat.turn 監査行が記録される。detail に本文は入らない
    （id とメタのみ・hash-chain 行の肥大化防止）。message_id は実際の保存済みメッセージと一致する。"""
    from sherpa import store
    ensure_v1()
    c = _client()
    r = c.post("/chat", json={"message": "消費税率を変えたい。影響は？",
                              "world": V, "knowledge": True}).json()
    cid = r["conversation_id"]
    user_msg_id = next(m["id"] for m in c.get(f"/conversations/{cid}").json()["messages"]
                       if m["role"] == "user")
    assistant_msg_id = r["message"]["id"]

    rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
    assert rows, "chat.turn が記録されていない"
    d = rows[0]["detail"]
    assert d["message_id_user"] == user_msg_id
    assert d["message_id_assistant"] == assistant_msg_id
    assert d["lens"] == "impact" and d["world"] == V and d["personal"] is False
    assert d["provider"] == "heuristic" and d["scope_paths"] == 0
    assert "消費税率" not in str(d), "本文が detail に混入している"


def test_chat_turn_audit_normalizes_unknown_provider():
    """settings.agent が allowlist 外（PUT /settings の検証が入る前に混入した保存済み不正値・
    SHERPA_AGENT env の誤設定 等）でも、監査 detail にはその生値ではなく "unknown" が入る
    （検索/集計対象の監査ログへ任意文字列を持ち込まない）。

    実行時の頭脳選択（agents.get_provider）は、黙って heuristic（別の頭脳）へフォールバックせず
    honest failure（`_UnwiredProvider`）を返す（意図しない黙ったプロバイダ切替の是正）。
    PUT /settings の allowlist 検証は新規保存だけをガードし、既存の保存済み不正値には効かない
    ため、実行時にこの honest failure 経路が必要（従来はここで黙って heuristic に化けていた）。"""
    from sherpa import agents, store
    store.update_settings(agent="totally-bogus-provider")
    try:
        assert isinstance(agents.get_provider(store.get_settings()), agents._UnwiredProvider)

        ensure_v1()
        c = _client()
        r = c.post("/chat", json={"message": "消費税率を変えたい。影響は？",
                                  "world": V, "knowledge": True}).json()
        cid = r["conversation_id"]
        rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
        assert rows, "chat.turn が記録されていない"
        assert rows[0]["detail"]["provider"] == "unknown"
    finally:
        store.update_settings(agent="heuristic")   # 以降のテストのベースラインへ戻す


def test_chat_turn_audit_records_effective_provider_not_saved_when_a7_mismatches(monkeypatch):
    """保存済み agent（openai）が選択中のクラウドプロバイダ（A7・gemini）と一致しない場合、
    監査 detail の "provider"（実効値）は "ollama" を示し、保存値は "provider_saved" に
    別途残す（実行は effective_agent() 経由で ollama にフォールバックするため、監査上も実行と
    一致させる＝実際は ollama で答えたのに provider=openai と誤解される事故を防ぐ）。

    実行そのもの（実際に ollama へ繋ぐか）は本テストの対象外＝`_FakeProvider` に差し替えて
    hermetic にする（test_chat_trace_capture_is_provider_agnostic と同じ流儀）。"""
    from sherpa import chat_service, store
    monkeypatch.setattr(store, "get_system_settings", lambda: {"cloud_provider": "gemini"})
    store.update_settings(agent="openai")

    class _FakeProvider:
        label, model = "Fake LLM", "fake-1"

        def run(self, ctx):
            yield {"type": "_result",
                  "env": {"lens": "chat", "headline": "フェイク回答", "summary": {"total": 0}, "data": {},
                          "sources": [], "scope": {"world": ctx.world, "scope_paths": [], "source": "off"}},
                  "decision": {"lens": "chat", "input": ctx.message, "reason": "fake"}}

    orig = chat_service.get_provider
    chat_service.get_provider = lambda settings, **kw: _FakeProvider()
    try:
        ensure_v1()
        c = _client()
        r = c.post("/chat", json={"message": "消費税率を変えたい。影響は？",
                                  "world": V, "knowledge": True}).json()
        cid = r["conversation_id"]
        rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
        assert rows, "chat.turn が記録されていない"
        d = rows[0]["detail"]
        assert d["provider"] == "ollama"        # 実効値（A7 フォールバック後）
        assert d["provider_saved"] == "openai"  # 保存値（ユーザーが選んだ構成のまま）
    finally:
        chat_service.get_provider = orig
        store.update_settings(agent="heuristic")   # 以降のテストのベースラインへ戻す


def test_chat_turn_audit_clarify_records_persisted_question_message(monkeypatch):
    """S5＋S1: clarify で終わったターンも chat.turn 監査へ記録される（衝突する cue＝影響/変え＋障害/落ち
    で Tier1 が confident=False になり、Tier2(LLM) 未接続なら Tier3 の ask_user に落ちて質問イベントに
    なる）。共有 dev DB の admin 設定に実 API キーが入っている場合 Tier2 が実際に分類してしまい clarify
    に落ちないため、Tier2 を明示的に未接続化する（`intent_llm.classify` を None 固定・本テストの意図＝
    Tier3 経路の検証）。S1 で確認カードを assistant として保存するようになったため、監査の
    message_id_assistant は**保存された確認カードの id**を指す（従来は None）。"""
    import json

    from sherpa import intent_llm, store
    monkeypatch.setattr(intent_llm, "classify", lambda *a, **k: None)
    ensure_v1()
    c = _client()
    params = {"message": "税率を変えたら夜間バッチが落ちる？", "world": V, "knowledge": "true"}
    question_ev = None
    with c.stream("GET", "/chat/stream", params=params) as s:
        for line in s.iter_lines():
            if line and line.startswith("data: "):
                e = json.loads(line[6:])
                if e["type"] == "question":
                    question_ev = e
    assert question_ev, "clarify（question イベント）が発生しなかった"
    cid = question_ev["conversation_id"]

    rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
    assert rows, "clarify ターンの chat.turn が記録されていない"
    d = rows[0]["detail"]
    assert d["lens"] == "clarify"
    assert d["message_id_user"] is not None
    # S1: 確認カードが保存されるようになった＝監査は保存済みメッセージ id を指す（実在も確認）。
    conv = c.get(f"/conversations/{cid}").json()
    saved_q = next(m for m in conv["messages"] if m["role"] == "assistant" and m["answer"].get("question"))
    assert d["message_id_assistant"] == saved_q["id"]


# ===== UI フィードバック1（途中停止・2026-07-03） =====

def test_stream_message_stop_event_skips_assistant_save_and_yields_stopped():
    """stop_event が立っている状態で stream_message を実行すると {"type":"stopped"} を返し、
    user メッセージは保存済みのまま assistant メッセージは保存しない（次の質問がそのまま続けられる、
    という選んだ設計の裏付け＝実装として最も素直: provider ループを単に途中で抜けるだけでよい）。"""
    import threading

    from sherpa import chat_service, store
    stop_event = threading.Event()
    stop_event.set()   # 開始前から停止要求済み＝最初の provider イベントで即座に検知されるはず

    events = list(chat_service.stream_message(
        None, "消費税率を変えたい", V, None, knowledge=False,
        user_id="admin", stop_event=stop_event))
    types = [e["type"] for e in events]
    assert types[-1] == "stopped", f"stopped イベントで終わっていない: {types}"
    cid = events[-1]["conversation_id"]
    conv = store.get_conversation(cid)
    roles = [m["role"] for m in conv["messages"]]
    assert roles == ["user"], f"assistant メッセージが保存されている（保存しない設計のはず）: {roles}"


def test_stream_message_stop_event_records_chat_turn_audit_like_clarify():
    """RV MEDIUM（2026-07-03再検証）: 停止ターンも clarify と同格に chat.turn 監査へ記録される
    （停止前は監査から丸ごと消えていた＝「誰が何を聞いて途中で止めたか」が追えない欠落だった）。
    assistant は保存しない設計のため message_id_assistant は None・detail.stopped は True。"""
    import threading

    from sherpa import chat_service, store
    stop_event = threading.Event()
    stop_event.set()

    events = list(chat_service.stream_message(
        None, "消費税率を変えたい", V, None, knowledge=False,
        user_id="admin", stop_event=stop_event))
    cid = events[-1]["conversation_id"]

    rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
    assert rows, "停止ターンの chat.turn が記録されていない"
    d = rows[0]["detail"]
    assert d["message_id_user"] is not None
    assert d["message_id_assistant"] is None
    assert d["stopped"] is True
    assert "消費税率" not in str(d), "本文が detail に混入している"


def test_stream_message_without_stop_event_completes_normally():
    """回帰確認: stop_event を渡さない（既定 None）通常経路は従来どおり assistant まで保存される。"""
    from sherpa import chat_service, store
    events = list(chat_service.stream_message(
        None, "消費税率を変えたい", V, None, knowledge=False, user_id="admin"))
    types = [e["type"] for e in events]
    assert types[-1] == "answer", f"通常完了していない: {types}"
    cid = events[-1]["conversation_id"]
    conv = store.get_conversation(cid)
    roles = [m["role"] for m in conv["messages"]]
    assert roles == ["user", "assistant"], f"通常完了で assistant が保存されていない: {roles}"


def test_chat_stream_registers_and_deregisters_stop_event_around_lifecycle():
    """GET /chat/stream に stream_id を渡すと、ストリーミング中はレジストリに登録され、
    完了後は自動的に取り除かれる（stream_id が使い回されても前回分の残骸が残らない）。"""
    from sherpa import api
    ensure_v1()
    c = _client()
    sid = f"lifecycle-{id(c)}"
    assert sid not in api._STREAM_STOP_EVENTS
    nodes, ans = _stream(c, "消費税率を変えたい。影響は？")   # stream_id は使わないヘルパ（既存動作の回帰確認）
    assert ans is not None
    # 別途 stream_id 付きでも普通に完走し、完了後はレジストリから消えている。
    params = {"message": "消費税率を変えたい。影響は？", "world": V, "knowledge": "true", "stream_id": sid}
    with c.stream("GET", "/chat/stream", params=params) as s:
        for _ in s.iter_lines():
            pass
    assert sid not in api._STREAM_STOP_EVENTS, "完了後もストリーム停止レジストリに残骸が残っている"


def test_chat_stream_stop_unknown_id_returns_ok_false():
    """存在しない stream_id を停止しようとしても 200 + {"ok": false}（エラーにしない・二重クリック等で普通に起こる）。"""
    c = _client()
    r = c.post("/chat/stream/stop", json={"stream_id": "no-such-stream-id"})
    assert r.status_code == 200
    assert r.json() == {"ok": False}


def test_chat_stream_stop_rejects_other_users_stream():
    """他人の stream_id は停止できない（存在有無を教えず {"ok": false}・イベントも立てない＝越境防止）。"""
    import threading

    from sherpa import api
    sid = "test-cross-user-stream-rv"
    ev = threading.Event()
    with api._STREAM_STOP_LOCK:
        api._STREAM_STOP_EVENTS[sid] = ("someone-else", ev)
    try:
        c = _client()   # 互換モード＝admin としてリクエストする
        r = c.post("/chat/stream/stop", json={"stream_id": sid})
        assert r.json() == {"ok": False}
        assert not ev.is_set(), "他人のストリームなのに停止イベントが立ってしまった"
    finally:
        with api._STREAM_STOP_LOCK:
            api._STREAM_STOP_EVENTS.pop(sid, None)


def test_chat_stream_stop_sets_event_for_own_stream():
    """本人の stream_id を停止すると {"ok": true} でイベントが立つ。"""
    import threading

    from sherpa import api
    sid = "test-own-stream-rv"
    ev = threading.Event()
    with api._STREAM_STOP_LOCK:
        api._STREAM_STOP_EVENTS[sid] = ("admin", ev)
    try:
        c = _client()
        r = c.post("/chat/stream/stop", json={"stream_id": sid})
        assert r.json() == {"ok": True}
        assert ev.is_set()
    finally:
        with api._STREAM_STOP_LOCK:
            api._STREAM_STOP_EVENTS.pop(sid, None)


def test_chat_stream_rejects_duplicate_in_use_stream_id():
    """RV MEDIUM（2026-07-03再検証）: 同じ stream_id が既に使用中なら 409 で拒否する
    （後勝ち上書きすると先勝ちストリームの Event 参照が失われ、`/chat/stream/stop` から
    止められなくなる＝停止不能バグ）。登録も上書きされない。"""
    import threading

    from sherpa import api
    ensure_v1()
    sid = "test-duplicate-stream-rv"
    ev = threading.Event()
    with api._STREAM_STOP_LOCK:
        api._STREAM_STOP_EVENTS[sid] = ("admin", ev)
    try:
        c = _client()
        params = {"message": "消費税率を変えたい。", "world": V, "knowledge": "false", "stream_id": sid}
        r = c.get("/chat/stream", params=params)
        assert r.status_code == 409
        with api._STREAM_STOP_LOCK:
            assert api._STREAM_STOP_EVENTS[sid] == ("admin", ev), "既存登録が上書きされてしまった"
    finally:
        with api._STREAM_STOP_LOCK:
            api._STREAM_STOP_EVENTS.pop(sid, None)


def test_chat_stream_finally_only_pops_if_registry_entry_still_own_event():
    """RV MEDIUM（2026-07-03再検証）: 完了時の pop は「登録されている Event が自分の Event と
    同一の場合のみ」行う（多層防御。通常は上の 409 拒否で防げるが、万一何らかの経路で
    再登録が起きていても他人の登録を巻き添えに消して停止不能にしない）。

    `TestClient.stream()` は ASGI transport がジェネレータをほぼ即座に完走させてしまうため
    （既知の制約・過去の調査で確認済み）、単純に途中で registry を覗いても間に合わない。
    ここでは `stream_message` を差し替えてハンドラ実行をこちらの合図で一時停止させ、
    別スレッドでリクエストを進行させることで「登録直後・pop 前」を確定的に作る。
    """
    import threading

    from sherpa import api
    from sherpa.routers import chat as chat_routes
    ensure_v1()
    sid = "test-finally-identity-rv"
    other_event = threading.Event()
    registered = threading.Event()
    release = threading.Event()

    def fake_stream_message(*_a, **_k):
        registered.set()          # ハンドラ側の Event 登録が完了した直後（gen() 実行開始）の合図
        release.wait(timeout=5)   # テスト側が registry を差し替えるまで足止め
        yield {"type": "answer", "conversation_id": 1, "message": {"id": 1}}

    # ハンドラ（GET /chat/stream）は sherpa/routers/chat.py 内で解決された stream_message を参照する
    # （フェーズ3スライス7で router へ移動・api.stream_message はもう存在しない束縛）。
    orig = chat_routes.stream_message
    chat_routes.stream_message = fake_stream_message
    try:
        c = _client()
        params = {"message": "x", "world": V, "knowledge": "false", "stream_id": sid}

        def _do_request():
            with c.stream("GET", "/chat/stream", params=params) as s:
                for _ in s.iter_lines():
                    pass

        t = threading.Thread(target=_do_request)
        t.start()
        assert registered.wait(timeout=5), "ハンドラが実行されなかった"
        with api._STREAM_STOP_LOCK:
            assert sid in api._STREAM_STOP_EVENTS
            # 万一の再登録（他人の Event）をシミュレート＝多層防御の対象シナリオ
            # （通常は上の 409 拒否で起こらないが、pop 側の安全性を単体で確認する）。
            api._STREAM_STOP_EVENTS[sid] = ("someone-else", other_event)
        release.set()
        t.join(timeout=5)
        assert not t.is_alive(), "リクエストスレッドが完了しなかった"

        with api._STREAM_STOP_LOCK:
            entry = api._STREAM_STOP_EVENTS.get(sid)
        assert entry is not None and entry[1] is other_event, \
            "自分の Event と一致しない登録を pop してしまった（他人の登録を巻き添えにした）"
    finally:
        chat_routes.stream_message = orig
        with api._STREAM_STOP_LOCK:
            api._STREAM_STOP_EVENTS.pop(sid, None)


def test_chat_stream_stream_id_must_match_uuid_like_pattern():
    """RV MEDIUM（2026-07-03再検証）: stream_id は UUID相当の形式に制約される（無制限文字列を受理しない）。"""
    c = _client()
    ensure_v1()
    params = {"message": "x", "world": V, "knowledge": "false", "stream_id": "a"}   # 短すぎる
    r = c.get("/chat/stream", params=params)
    assert r.status_code == 422


def test_chat_stream_stop_rejects_malformed_stream_id():
    """RV MEDIUM（2026-07-03再検証）: /chat/stream/stop の body も同じ形式制約を受ける。"""
    c = _client()
    r = c.post("/chat/stream/stop", json={"stream_id": "a"})
    assert r.status_code == 422


def _web_js_files():
    """web/**/*.js（vendor/ 除外）の再帰列挙（フェーズ6 S1: chat.js 分割後に web/chat/*.js へ
    ファイルが増えても検査対象から黙って外れないよう非再帰 glob から変更）。走査本数の下限を
    ここで固定し、glob が空振り（0件マッチで全テストが素通り）していないことも合わせて担保する。"""
    web = ROOT / "web"
    files = sorted(p for p in web.rglob("*.js") if "vendor" not in p.relative_to(web).parts)
    # RV LOW（2026-07-14 フェーズ6）: トップレベルだけで16件あるため下限15では rglob→glob の退行を
    # 検知できない。分割後の実数（23件）基準＋ web/chat/ 配下が実際に含まれることを明示 assert。
    assert len(files) >= 23, f"走査対象JSファイルが少なすぎる（再帰 glob の退行の疑い）: {len(files)} 件"
    assert any("chat" in p.relative_to(web).parts[:-1] for p in files), \
        "web/chat/ 配下の分割ファイルが走査対象に含まれていない（再帰 glob の退行）"
    return files


def test_no_inline_handlers_in_web_js():
    """XSS ガード: 配信JSにデータ込みインライン on*= ハンドラが無い／旧 impact.html は削除済み。"""
    import re
    web = ROOT / "web"
    assert not (web / "impact.html").exists() and not (web / "impact.js").exists()
    bad = re.compile(r"on\w+\s*=\s*([\"']).*?\$\{")   # onclick="...${...}" 等
    for p in _web_js_files():
        assert not bad.search(p.read_text(encoding="utf-8")), f"inline handler with data in {p.name}"


def test_blob_download_helper_delays_revoke_and_is_used_by_dl_handlers():
    """UI フィードバック3（2026-07-03・原本DL「フリーズ」修正）: blob URL の revoke を
    a.click() 直後に同期実行すると、ブラウザの保存ダイアログがまだ blob を読み切る前に無効化され、
    保存/キャンセルいずれでもダウンロードが完了せず固まって見える。共通ヘルパ Sherpa.downloadBlob
    が setTimeout で revoke を遅延させていること、blob ダウンロードを行う全ページがこのヘルパに
    一本化されていて自前で即時 revoke していないことをソースで固定する（JS 用テストランナーが無い
    ため、このリポジトリの既存流儀＝Python からソース文字列を検査するテストで担保する）。
    フェーズ6 S2: window.Sherpa（downloadBlob 含む）は nav.js から common.js へ分離済み。"""
    import re
    web = ROOT / "web"
    common = (web / "common.js").read_text(encoding="utf-8")
    assert "downloadBlob" in common, "Sherpa.downloadBlob 共通ヘルパが無い"
    # revoke が click() と同期実行ではなく setTimeout 経由（イベント待ちに依存しない遅延revoke）。
    m = re.search(r"_sherpaDownloadBlob\s*=.*?\n};", common, re.S)
    assert m, "_sherpaDownloadBlob の実装が見つからない"
    body = m.group(0)
    assert "setTimeout" in body and "revokeObjectURL" in body, \
        "downloadBlob が setTimeout 経由で revoke していない"
    assert re.search(r"a\.click\(\);\s*\n\s*URL\.revokeObjectURL", body) is None, \
        "click() 直後に同期で revoke している（修正前の不具合パターンに戻っている）"

    # web/*.js 全体（common.js 自身を除く）で、自前の revokeObjectURL が残っていないこと（共通ヘルパへの一本化）。
    for p in _web_js_files():
        if p.name == "common.js":
            continue
        src = p.read_text(encoding="utf-8")
        assert "URL.revokeObjectURL" not in src, \
            f"{p.name} が自前で revokeObjectURL している（共通ヘルパ Sherpa.downloadBlob に一本化されていない）"
    for name in ("chat.js", "ingest.js", "audit.js"):
        assert "Sherpa.downloadBlob(" in (web / name).read_text(encoding="utf-8"), \
            f"{name} が共通ヘルパ Sherpa.downloadBlob を使っていない"
