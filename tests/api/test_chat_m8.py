"""チャット主入口（意図でレンズ振り分け＋会話DB永続＋答え先頭＋出典）の契約テスト。

router 単体は依存ゼロ。会話フローは要 Neo4j ＋ Postgres。
"""
from __future__ import annotations

import pathlib
import re
import threading

import pytest
from _world_setup import TEST_WORLD_ID, ensure_v1
from _store_helpers import get_conversation
from sherpa.chat_router import route

ROOT = pathlib.Path(__file__).resolve().parents[2]
V = TEST_WORLD_ID
IMPACT_MSG = "消費税率を変えたい。影響は？"
CLARIFY_MSG = "税率を変えたら夜間バッチが落ちる？"


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """ログインせず直接叩く（compat モード）。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


# テストのベースラインを heuristic に固定（デモが残した設定行に左右されないように）。PG 未起動なら無視。
# API/テストは同じ user_id="admin" の設定行を使うため、実ユーザの設定（APIキー等）を壊さないよう
# スナップショットを取り、プロセス終了時に元へ戻す（atexit）。
import atexit  # noqa: E402

try:
    from sherpa import store as _store
    _ORIG_SETTINGS = _store.get_settings()

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


def _client():
    from fastapi.testclient import TestClient
    from sherpa.api import app
    return TestClient(app)


def _run_turn(message, knowledge=True, **kw) -> list[dict]:
    """1 ターンを `chat_service.stream_message` で直接実行し、イベント列を返す（knowledge ON は Neo4j セッションつき）。"""
    from sherpa import chat_service
    from sherpa.deps import neo4j_session
    kw.setdefault("user_id", "admin")
    if not knowledge:
        return list(chat_service.stream_message(None, message, V, knowledge=False, **kw))
    with neo4j_session() as s:
        return list(chat_service.stream_message(s, message, V, knowledge=True, **kw))


def _post_chat(c, message, **over):
    events = _run_turn(message, **over)
    ans = next(e for e in events if e["type"] == "answer")
    return {"conversation_id": ans["conversation_id"], "message": ans["message"]}


def _sse_events(c, message, knowledge=True) -> list[dict]:
    return _run_turn(message, knowledge=knowledge)


def _stream(c, message, knowledge=True):
    events = _sse_events(c, message, knowledge)
    nodes = [(e["id"], e["kind"]) for e in events if e["type"] == "node" and e["status"] == "done"]
    ans = next((e for e in events if e["type"] == "answer"), None)
    return nodes, ans


def _result_event(ctx, headline="フェイク回答", lens="chat", **env_extra):
    return {"type": "_result",
            "env": {"lens": lens, "headline": headline, "summary": {"total": 0}, "data": {},
                    "sources": [], "scope": {"world": ctx.world, "scope_paths": [], "source": "off"},
                    **env_extra},
            "decision": {"lens": lens, "input": ctx.message, "reason": "fake"}}


def _fake_provider(*events_or_fn, label="Fake LLM", model="fake-1"):
    """run(ctx) が events_or_fn（dict か ctx を受ける callable）を順に yield する偽 provider。"""
    class _P:
        def run(self, ctx):
            for e in events_or_fn:
                yield e(ctx) if callable(e) else e

        def _agentic_target_check(self):   # knowledge=True は受付段階で必ず呼ばれる（no-op）
            return None
    _P.label, _P.model = label, model
    return _P()


def _use_provider(monkeypatch, prov):
    """chat_service の get_provider を差し替える。"""
    from sherpa import chat_service
    monkeypatch.setattr(chat_service, "get_provider", lambda settings, **kw: prov)


def _saved_assistant(cid):
    from sherpa import store
    return next(m for m in get_conversation(cid)["messages"] if m["role"] == "assistant")


def _audit_detail(cid):
    from sherpa import store
    rows = store.list_audit(action="chat.turn", resource_id=f"conv:{cid}", limit=5)
    assert rows, "chat.turn が記録されていない"
    return rows[0]["detail"]


# ---- ルーティング（依存ゼロ） ----

def test_router_picks_lens_and_start():
    assert route(IMPACT_MSG)["lens"] == "impact"
    assert route("夜間バッチ NIGHTLY が ABEND。原因は？")["lens"] == "troubleshoot"
    assert route("消費税の端数処理の仕様は？")["lens"] == "qa"
    r = route("消費税率を変更したい", known_terms=["消費税率", "税率", "TAX-RATE"])
    assert r["lens"] == "impact" and r["input"] == "消費税率"


# ---- 会話フロー（要 Neo4j＋Postgres） ----

def test_chat_persists_and_answers_answer_first_and_routes_troubleshoot():
    """業務語「消費税率」は構造的に解決しない（REALIZES 橋の撤去）ので、起点はコード自身の識別子を使う。"""
    ensure_v1()
    c = _client()
    r = _post_chat(c, "TAX-RATE を変えたい。影響は？")
    cid = r["conversation_id"]
    ans = r["message"]["answer"]
    assert ans["lens"] == "impact"
    assert "影響" in ans["headline"] and ans["summary"]["total"] >= 1   # 答え先頭＋件数
    assert ans["sources"], "出典（原本DL）が付いていない"
    assert ans["sources"][0]["download_url"].startswith("/documents/")

    r2 = _post_chat(c, "端数処理の仕様は？", conversation_id=cid)   # 同じ会話を継続（別レンズ）
    assert r2["conversation_id"] == cid and r2["message"]["answer"]["lens"] == "qa"
    roles = [m["role"] for m in c.get(f"/conversations/{cid}").json()["messages"]]
    assert roles.count("user") == 2 and roles.count("assistant") == 2

    ts = _post_chat(c, "夜間バッチ NIGHTLY が ABEND。原因候補は？")["message"]["answer"]
    assert ts["lens"] == "troubleshoot" and ts["summary"]["total"] >= 1


def test_chat_stream_dynamic_nodes_and_saved_trace():
    """SSE は思考/ツールのノードを動的に流し、最後に answer を返す。"""
    ensure_v1()
    c = _client()
    nodes, ans = _stream(c, IMPACT_MSG)
    assert [n for n, _ in nodes] == ["understand", "intent", "tool-graph", "compose"]   # 影響＝ツール1
    assert any(k == "tool" for _, k in nodes)
    assert ans and ans["message"]["answer"]["lens"] == "impact"

    # SSE で流れた node は messages.trace に保存され、会話再取得でも同じ trace が返る（user ターンには付かない）。
    trace = ans["message"].get("trace")
    assert trace, "trace が保存されていない"
    assert [n["id"] for n in trace] == [nid for nid, _kind in nodes]   # id は初出順・重複更新は dedup
    conv = c.get(f"/conversations/{ans['conversation_id']}").json()
    assert next(m for m in conv["messages"] if m["role"] == "assistant")["trace"] == trace
    assert not next(m for m in conv["messages"] if m["role"] == "user").get("trace")

    t_nodes, _ = _stream(c, "夜間バッチ NIGHTLY が ABEND。原因は？")   # 使うツールが2つ＝ノードが動的に増える
    assert [n for n, k in t_nodes if k == "tool"] == ["tool-graph", "tool-docs"]


def test_chat_saves_trace_on_every_path_and_knowledge_off_is_plain_chat():
    """trace 保存の全経路: knowledge ON／OFF（_plain_run）。"""
    ensure_v1()
    c = _client()
    trace = _post_chat(c, IMPACT_MSG)["message"].get("trace")
    assert trace, "knowledge=True で trace が保存されていない"
    assert any(n["id"] == "understand" for n in trace)

    plain = _post_chat(c, "こんにちは", knowledge=False)["message"]
    ans = plain["answer"]   # knowledge=False＝検索せず素の会話（レンズ=chat・出典なし・範囲=off）
    assert ans["lens"] == "chat" and ans["sources"] == [] and ans["scope"]["source"] == "off"
    trace = plain.get("trace")
    assert trace, "knowledge=False（_plain_run）で trace が保存されていない"
    assert any(n["id"] == "brain" for n in trace)

    nodes, ans = _stream(c, "こんにちは", knowledge=False)
    trace = ans["message"].get("trace")
    assert trace, "knowledge=False（_plain_run）で配信したターンの trace が保存されていない"
    assert [n["id"] for n in trace] == [nid for nid, _kind in nodes]


def test_chat_stream_clarify_persists_question_card_as_assistant_message(monkeypatch):
    """clarify（question イベント）で終わったターンは確認カードを assistant メッセージとして永続化する
    （answer={"lens":"clarify","question":{...}}・content=prompt・trace も保存・answer イベントは出ない）。
    Tier2(LLM) は未接続化（共有 dev DB の実キーで先取り分類されるのを防ぐ）。"""
    from sherpa import intent_llm
    monkeypatch.setattr(intent_llm, "classify", lambda *a, **k: None)
    c = _client()
    events = _sse_events(c, CLARIFY_MSG)
    q_ev = next((e for e in events if e["type"] == "question"), None)
    assert q_ev, "clarify（question イベント）が発生しなかった"
    assert not any(e["type"] == "answer" for e in events), "clarify なのに answer が生成されている"
    conv = c.get(f"/conversations/{q_ev['conversation_id']}").json()
    assistant = [m for m in conv["messages"] if m["role"] == "assistant"]
    assert len(assistant) == 1, "確認カードが assistant メッセージとして保存されていない"
    saved = assistant[0]
    assert saved["answer"]["lens"] == "clarify"
    sq = saved["answer"]["question"]
    assert sq["interaction_id"] == q_ev["interaction_id"]
    assert sq["prompt"] == q_ev["prompt"] and sq["options"]
    assert sq["original_message"] == CLARIFY_MSG
    assert saved["content"] == q_ev["prompt"]
    assert saved["trace"], "clarify ターンの trace が保存されていない"


def _agentic_question():
    from sherpa import agentic_search
    return agentic_search._question_from_args({
        "mode": "single", "prompt": "対象範囲を教えてください",
        "options": [{"id": "a", "label": "A案"}, {"id": "b", "label": "B案"}], "allow_free_text": True})


def _codex_question():
    from sherpa.providers.codex import mcp
    return mcp._codex_ask_question({"tool": "ask_user", "arguments": {
        "mode": "single", "prompt": "Excel の列構成はどれにしますか？",
        "options": [{"id": "a", "label": "月別×金額"}, {"id": "b", "label": "案件別×工数"}],
        "allow_free_text": True}})


@pytest.mark.parametrize("make_question, message, prompt, labels", [
    (_agentic_question, "TAX-RATE の対象範囲を教えて", "対象範囲を教えてください", ("A案", "B案")),
    (_codex_question, "案件一覧を Excel にまとめて", "Excel の列構成はどれにしますか？", ("月別×金額", "案件別×工数")),
])
def test_chat_stream_ask_user_question_persists_with_correct_shape(monkeypatch, make_question, message, prompt, labels):
    """agentic ask_user・Codex の ask_user が作る question（original_message を持たない）が、
    chat_service を通って保存形（interaction_id・options・mode・allow_free_text・trace）で永続化され、
    original_message が元の依頼で補完される。knowledge=False（Neo4j 非依存）で回す。"""
    from sherpa import chat_service

    def _question(ctx):
        q = make_question()
        assert q and "original_message" not in q, "前提が崩れている（生成直後に original_message を持たない）"
        return q

    _use_provider(monkeypatch, _fake_provider(
        {"type": "node", "id": "tool-1", "kind": "tool", "label": "調べる", "detail": "x", "status": "done"},
        _question))
    events = list(chat_service.stream_message(None, message, V, None, knowledge=False, user_id="admin"))
    q_ev = next(e for e in events if e["type"] == "question")

    saved = _saved_assistant(q_ev["conversation_id"])
    assert saved["answer"]["lens"] == "clarify"
    sq = saved["answer"]["question"]
    assert sq["interaction_id"] == q_ev["interaction_id"]
    assert sq["mode"] == "single" and sq["allow_free_text"] is True
    assert sq["options"] == [{"id": "a", "label": labels[0], "description": ""},
                             {"id": "b", "label": labels[1], "description": ""}]
    assert sq["original_message"] == message
    assert saved["content"] == prompt
    assert saved.get("trace"), "clarify ターンでも trace が保存されるはず"


def test_chat_trace_capture_is_provider_agnostic(monkeypatch):
    """trace 捕捉（type=="node" かつ id を持つイベントを蓄積）は特定 provider に依存しない。"""
    from sherpa import chat_service
    _use_provider(monkeypatch, _fake_provider(
        {"type": "node", "id": "n1", "kind": "think", "label": "考える", "detail": "a", "status": "active"},
        {"type": "node", "id": "n1", "kind": "think", "label": "考える", "detail": "b", "status": "done"},
        _result_event))
    events = list(chat_service.stream_message(None, "任意の質問", V, None, knowledge=False, user_id="admin"))
    trace = next(e for e in events if e["type"] == "answer")["message"].get("trace")
    assert trace and trace[0]["id"] == "n1" and trace[0]["detail"] == "b", "node イベントが trace に捕捉されていない"


def test_chat_history_primes_next_turn_via_db_after_provider_reinstantiation(monkeypatch):
    """turn1 と turn2 を別の fake provider（状態を共有しない）で実行しても、turn2 の ctx.history に
    turn1 の (user, assistant) 対が入る＝会話継続はプロセス内状態でなく DB から再構築される。"""
    captured: dict = {}

    def _turn1(ctx):
        assert ctx.history == [], "新規会話の1ターン目に履歴が入ってはいけない"
        return _result_event(ctx, headline="最初の回答です")

    def _turn2(ctx):
        captured["history"] = ctx.history
        return _result_event(ctx, headline="2番目の回答です")

    c = _client()
    _use_provider(monkeypatch, _fake_provider(_turn1))
    cid = _post_chat(c, "最初の質問です", knowledge=False)["conversation_id"]
    _use_provider(monkeypatch, _fake_provider(_turn2))
    r2 = _post_chat(c, "続けて教えて", knowledge=False, conversation_id=cid)
    assert r2["conversation_id"] == cid
    assert captured["history"] == [
        {"role": "user", "content": "最初の質問です"},
        {"role": "assistant", "content": "最初の回答です"},
    ]


def test_provider_seam_uniform(monkeypatch):
    """頭脳を差し替えても可視化（node→answer）の形は不変。未接続 provider は正直に返す。
    「キー無し」の前提を自分で統制する（プロセス env はテスト実行順で汚染され得る：依存が import 時に
    リポジトリの .env を注入する）。"""
    from sherpa import store
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store.update_settings(agent="gemini")   # 環境で有効化されていない頭脳＝使えない
    try:
        nodes, ans = _stream(_client(), "何でもいい")
        assert ans and "選び直してください" in ans["message"]["answer"]["headline"]  # 嘘の回答をしない
        assert nodes and ans["type"] == "answer"                       # プロトコルは同一
    finally:
        store.update_settings(agent="heuristic")


def test_created_files_persisted_in_answer_and_triggers_personal_flag(monkeypatch):
    """env['created_files'] が answer に保存されて応答に載り、codex_wrote_files 連動で
    contains_personal_workspace が立ち、共有不可ゲート（409）が従来どおり効く。"""
    ensure_v1()
    from sherpa import store

    def _result(ctx):
        r = _result_event(ctx, headline="資料を作成しました。", lens="author",
                          route={"lens": "author", "reason": "fake", "input": ctx.message},
                          codex_wrote_files=["消費税率一覧.xlsx"],
                          created_files=[{"name": "消費税率一覧.xlsx",
                                          "download_url": "/workspace/files/123/download"}])
        r["env"]["scope"]["source"] = "all"
        return r

    _use_provider(monkeypatch, _fake_provider(
        {"type": "node", "id": "n1", "kind": "think", "label": "Codex が調べる", "detail": "作成しました",
         "status": "done"}, _result, label="Fake Codex", model="fake-codex"))
    c = _client()
    r = _post_chat(c, "消費税率の一覧をExcelにまとめて")
    assert r["message"]["answer"]["created_files"] == [
        {"name": "消費税率一覧.xlsx", "download_url": "/workspace/files/123/download"}]

    cid = r["conversation_id"]
    assert store.get_conversation_for_read("admin", cid)["conversation"].get("contains_personal_workspace") is True
    share_r = c.post(f"/conversations/{cid}/shares", json={"invitee_user_ids": []})
    assert share_r.status_code == 409, f"personal 会話の共有が拒否されなかった: {share_r.status_code} {share_r.text}"


def test_conversation_pin_and_delete():
    """ピン止め（一覧で pinned）・タイトル変更（空は 422）・削除（以後 404・二重削除も 404）。"""
    c = _client()
    cid = _post_chat(c, "x", knowledge=False)["conversation_id"]
    assert c.patch(f"/conversations/{cid}", json={"title": "新しい名前"}).json()["title"] == "新しい名前"
    assert c.get(f"/conversations/{cid}").json()["conversation"]["title"] == "新しい名前"
    assert c.patch(f"/conversations/{cid}", json={"title": "   "}).status_code == 422
    assert c.post(f"/conversations/{cid}/pin", json={"pinned": True}).json()["pinned"] is True
    assert any(x["id"] == cid and x["pinned"] for x in c.get("/conversations").json())
    assert c.post(f"/conversations/{cid}/pin", json={"pinned": False}).json()["pinned"] is False
    assert c.delete(f"/conversations/{cid}").json()["ok"] is True
    assert c.get(f"/conversations/{cid}").status_code == 404
    assert c.delete(f"/conversations/{cid}").status_code == 404


def test_settings_answer_policy_is_fixed_and_web_search_flag_reflects_admin_setting():
    """回答方針はアプリ固定: 利用者が PUT しても保存されず、provider は常に ANSWER_POLICY を前置する。
    web_search は既定 OFF。web_search_available は system_settings.web_search_allowed をそのまま映す
    （env は初回シードのみ・実行時には見ない）。"""
    from sherpa import store
    from sherpa.agents import get_provider
    from sherpa.providers.prompts import ANSWER_POLICY
    c = _client()
    assert c.put("/settings", json={"system_prompt": "必ず簡潔に答えてください。"}).status_code == 200
    assert "system_prompt" not in c.get("/settings").json()
    assert get_provider({**store.get_settings(), "agent": "heuristic"}).system_prompt == ANSWER_POLICY

    store.set_system_settings("admin-uid", {"web_search_allowed": None})
    try:
        assert c.get("/settings").json()["web_search_available"] is False, "未設定なのに利用可能フラグが立っている"
        store.set_system_settings("admin-uid", {"web_search_allowed": True})
        assert c.get("/settings").json()["web_search_available"] is True, "管理者許可時にフラグが立たない"
    finally:
        store.set_system_settings("admin-uid", {"web_search_allowed": None})


def test_guards_world_and_conversation():
    """world のパストラバーサル拒否（422）と不正会話IDの 404（500にしない）。"""
    c = _client()
    assert c.post("/chat/turns", json={"message": "x", "world": "../../etc"}).status_code == 422
    assert c.post("/chat/turns", json={"message": "x", "conversation_id": 99999999}).status_code == 404


# ---- chat.turn 監査 ----

def test_chat_turn_audit_recorded_without_body():
    """1ターン完了時の chat.turn 監査行は id とメタのみ（本文は入らない）。message_id は保存済みと一致。"""
    ensure_v1()
    c = _client()
    r = _post_chat(c, IMPACT_MSG)
    cid = r["conversation_id"]
    user_msg_id = next(m["id"] for m in c.get(f"/conversations/{cid}").json()["messages"] if m["role"] == "user")

    d = _audit_detail(cid)
    assert d["message_id_user"] == user_msg_id
    assert d["message_id_assistant"] == r["message"]["id"]
    assert d["lens"] == "impact" and d["world"] == V and d["personal"] is False
    assert d["provider"] == "heuristic" and d["scope_paths"] == 0
    assert "消費税率" not in str(d), "本文が detail に混入している"


def test_chat_turn_audit_normalizes_unknown_provider():
    """settings.agent が allowlist 外の保存済み不正値でも、監査 detail には "unknown" が入る。
    実行時の頭脳選択は黙って heuristic へ化けず honest failure（_UnwiredProvider）を返す。"""
    from sherpa import agents, store
    store.update_settings(agent="totally-bogus-provider")
    try:
        assert isinstance(agents.get_provider(store.get_settings()), agents._UnwiredProvider)
        ensure_v1()
        cid = _post_chat(_client(), IMPACT_MSG)["conversation_id"]
        assert _audit_detail(cid)["provider"] == "unknown"
    finally:
        store.update_settings(agent="heuristic")


def test_chat_turn_audit_clarify_records_persisted_question_message(monkeypatch):
    """clarify で終わったターンも chat.turn 監査に記録され、message_id_assistant は保存された確認カードの id。
    （衝突する cue で Tier1 が confident=False→Tier2 未接続→Tier3 の ask_user に落ちる。）"""
    from sherpa import intent_llm
    monkeypatch.setattr(intent_llm, "classify", lambda *a, **k: None)
    ensure_v1()
    c = _client()
    question_ev = next((e for e in _sse_events(c, CLARIFY_MSG) if e["type"] == "question"), None)
    assert question_ev, "clarify（question イベント）が発生しなかった"
    cid = question_ev["conversation_id"]

    d = _audit_detail(cid)
    assert d["lens"] == "clarify"
    assert d["message_id_user"] is not None
    conv = c.get(f"/conversations/{cid}").json()
    saved_q = next(m for m in conv["messages"] if m["role"] == "assistant" and m["answer"].get("question"))
    assert d["message_id_assistant"] == saved_q["id"]


# ---- 途中停止 ----

def test_stream_message_stop_event_skips_assistant_save_yields_stopped_and_audits_else_completes():
    """stop_event が立っていると {"type":"stopped"} を返し、user は保存済みのまま assistant は保存しない。
    停止ターンも chat.turn 監査へ記録される（message_id_assistant=None・stopped=True・本文なし）。"""
    from sherpa import chat_service, store
    stop_event = threading.Event()
    stop_event.set()

    events = list(chat_service.stream_message(
        None, "消費税率を変えたい", V, None, knowledge=False, user_id="admin", stop_event=stop_event))
    assert events[-1]["type"] == "stopped", f"stopped で終わっていない: {[e['type'] for e in events]}"
    cid = events[-1]["conversation_id"]
    roles = [m["role"] for m in get_conversation(cid)["messages"]]
    assert roles == ["user"], f"assistant メッセージが保存されている: {roles}"

    d = _audit_detail(cid)
    assert d["message_id_user"] is not None
    assert d["message_id_assistant"] is None
    assert d["stopped"] is True
    assert "消費税率" not in str(d), "本文が detail に混入している"

    events = list(chat_service.stream_message(      # stop_event 無し（既定）は通常どおり assistant まで保存される
        None, "消費税率を変えたい", V, None, knowledge=False, user_id="admin"))
    assert events[-1]["type"] == "answer", f"通常完了していない: {[e['type'] for e in events]}"
    roles = [m["role"] for m in get_conversation(events[-1]["conversation_id"])["messages"]]
    assert roles == ["user", "assistant"]


# ---- 静的ゲート（web/**/*.js） ----

def _web_js_files():
    """web/**/*.js（vendor/ 除外）の再帰列挙。走査本数の下限で glob の空振り（全テスト素通り）を防ぐ。"""
    web = ROOT / "web"
    files = sorted(p for p in web.rglob("*.js") if "vendor" not in p.relative_to(web).parts)
    assert len(files) >= 23, f"走査対象JSファイルが少なすぎる（再帰 glob の退行の疑い）: {len(files)} 件"
    assert any("chat" in p.relative_to(web).parts[:-1] for p in files), \
        "web/chat/ 配下の分割ファイルが走査対象に含まれていない（再帰 glob の退行）"
    return files


def test_no_inline_handlers_in_web_js():
    """XSS ガード: 配信JSにデータ込みインライン on*= ハンドラが無い／旧 impact.html は削除済み。"""
    web = ROOT / "web"
    assert not (web / "impact.html").exists() and not (web / "impact.js").exists()
    bad = re.compile(r"\bon\w+\s*=\s*([\"']).*?\$\{")   # onclick="...${...}" 等（aria-controls= を誤検出しない）
    for p in _web_js_files():
        assert not bad.search(p.read_text(encoding="utf-8")), f"inline handler with data in {p.name}"


def test_blob_download_helper_delays_revoke_and_is_used_by_dl_handlers():
    """blob URL の revoke を a.click() 直後に同期実行すると保存ダイアログが blob を読み切る前に無効化され
    DL が固まる。共通ヘルパ Sherpa.downloadBlob（common.js）が setTimeout で遅延 revoke し、blob DL を行う
    全ページがこのヘルパに一本化されて自前で revoke していないことをソースで固定する。"""
    web = ROOT / "web"
    common = (web / "common.js").read_text(encoding="utf-8")
    assert "downloadBlob" in common, "Sherpa.downloadBlob 共通ヘルパが無い"
    m = re.search(r"_sherpaDownloadBlob\s*=.*?\n};", common, re.S)
    assert m, "_sherpaDownloadBlob の実装が見つからない"
    body = m.group(0)
    assert "setTimeout" in body and "revokeObjectURL" in body, "downloadBlob が setTimeout 経由で revoke していない"
    assert re.search(r"a\.click\(\);\s*\n\s*URL\.revokeObjectURL", body) is None, \
        "click() 直後に同期で revoke している（修正前の不具合パターン）"

    for p in _web_js_files():
        if p.name == "common.js":
            continue
        assert "URL.revokeObjectURL" not in p.read_text(encoding="utf-8"), \
            f"{p.name} が自前で revokeObjectURL している（Sherpa.downloadBlob に一本化されていない）"
    for name in ("chat.js", "ingest.js", "audit.js"):
        assert "Sherpa.downloadBlob(" in (web / name).read_text(encoding="utf-8"), \
            f"{name} が共通ヘルパ Sherpa.downloadBlob を使っていない"
