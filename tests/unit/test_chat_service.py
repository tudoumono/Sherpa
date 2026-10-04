"""chat_service の単体テスト。

- `_cap_trace_v2`: 実行イベント v2 の二段上限（ソフト→ハード→honest failure）。決定的・orphan 無し・
  不正 parent_id は親なしへ正規化。`_trace_bytes` は実保存（`ensure_ascii=True`）と同じ測り方。
- 保存サイト（stream_message の answer・clarify）は store をフェイク差し替えして PG 不要で検証。
- 履歴 priming（`_history_pairs`/`_clip_history_msg`）・調べ方/深さ/検索経路の `_dispatch` 配線・
  `_resolve_scope`/`_resolve_lens`・`_retry_hints`/`_finalize`・グラフ縮退（S4）・個人由来の引き継ぎ。
"""
from __future__ import annotations

import json
import logging
import threading

import pytest

from sherpa import chat_service as CS
from sherpa import exec_event as EE
from sherpa import store


@pytest.fixture(autouse=True)
def _default_world_graph_not_empty(monkeypatch):
    """`_dispatch` の impact/troubleshoot 分岐は 0 件時に `world_graph_is_empty` を呼ぶ。既定は
    「実データがある」側に倒す（未構築の挙動を検証するテストが個別に上書きする）。"""
    monkeypatch.setattr(CS, "world_graph_is_empty", lambda session, world: False)


def _try_init():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _new_conv():
    _try_init()
    return store.create_conversation(user_id="admin", world="v1", title="history test")["id"]


_NODE = {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
         "detail": "内容を把握しました", "status": "done"}


def test_sources_importance_and_control_file(monkeypatch):
    from sherpa.ingest import importance as imp
    # `_重要度.txt`（設定ファイル自体）は出典に出さない
    assert {s["doc_id"] for s in CS._sources(["a.md", "_重要度.txt", "4期/_重要度.txt"], "v1")} == {"a.md"}
    # 解決できれば importance/importance_reason を足す（importance_source は出さない）・解決の無い doc は持たない
    monkeypatch.setattr(CS.worlds, "world_dir", lambda w: "/tmp/x")
    res = imp.Resolution(value="高", reason="契約書", config_path="_重要度.txt", rule_line=1)
    monkeypatch.setattr(CS.importance, "resolve_many", lambda w, rels, root=None, sig=None: {"a.md": res})
    out = {s["doc_id"]: s for s in CS._sources(["a.md", "b.md"], "v1")}
    assert out["a.md"]["importance"] == "高" and out["a.md"]["importance_reason"] == "契約書"
    assert "importance_source" not in out["a.md"] and "importance" not in out["b.md"]


def test_sources_unregistered_world_skips_resolve_call_and_stays_two_keys(monkeypatch):
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(CS.worlds, "world_dir", lambda w: None)
    monkeypatch.setattr(CS.importance, "resolve_many", _boom)
    out = CS._sources(["a.md"], "v1")
    assert called["n"] == 0 and set(out[0]) == {"doc_id", "download_url"}


# ---- _cap_trace_v2 の二段上限（純関数・DB 不要） ----

def _assert_dict_ids_match_keys(nodes: dict) -> None:
    for k, v in nodes.items():
        assert v["id"] == k, f"dict key {k!r} != node id {v['id']!r}"


def _assert_no_orphans(trace: list, *, allow_truncated: bool = False) -> None:
    """各ノードの `parent_id` の親が同じ trace 内にあること。honest failure 経路を検証するテストだけ
    `allow_truncated=True` で明示的に免除する（内容を見て検査を緩めない）。"""
    if allow_truncated:
        return
    ids = {n["id"] for n in trace}
    for n in trace:
        pid = n.get("parent_id")
        assert pid is None or pid in ids, f"orphan: {n['id']!r} が存在しない親 {pid!r} を参照"


def _v2_node(i, *, parent_id=None, kind="tool", agent_run_id=None, evidence_ids=None):
    return EE.build_event(f"n{i}", kind, f"label{i}", "d", "done",
                          parent_id=parent_id, agent_run_id=agent_run_id, evidence_ids=evidence_ids)


def _parent_child_nodes(n_pairs: int) -> dict:
    nodes = {}
    for i in range(n_pairs):
        nodes[f"p{i}"] = EE.build_event(f"p{i}", "agent", f"parent{i}", "d", "done")
        nodes[f"c{i}"] = EE.build_event(f"c{i}", "tool", f"child{i}", "d", "done", parent_id=f"p{i}")
    return nodes


def test_trace_bytes_matches_real_storage_serialization_not_sse():
    node = EE.build_event("n1", "tool", "検索テスト", "詳細な日本語のテキストです", "done")
    expected = len(json.dumps([node], ensure_ascii=True, default=str).encode("utf-8"))
    smaller_if_wrong = len(json.dumps([node], ensure_ascii=False, default=str).encode("utf-8"))
    assert CS._trace_bytes([node]) == expected
    assert expected > smaller_if_wrong


def test_cap_trace_v2_empty_passthrough_and_detail_truncation():
    assert CS._cap_trace_v2({}) is None
    nodes = {f"n{i}": _v2_node(i) for i in range(5)}
    _assert_dict_ids_match_keys(nodes)
    out = CS._cap_trace_v2(nodes)
    assert [n["id"] for n in out] == [f"n{i}" for i in range(5)]
    _assert_no_orphans(out)
    out = CS._cap_trace_v2({"n1": {**_v2_node(1), "detail": "x" * 500}})
    assert len(out[0]["detail"]) == CS._MAX_TRACE_DETAIL_CHARS


def test_cap_trace_v2_soft_cap_reserves_budget_for_the_aggregate_itself():
    n = CS._MAX_TRACE_NODES + 30
    nodes = {f"n{i}": _v2_node(i, kind="tool") for i in range(n)}
    _assert_dict_ids_match_keys(nodes)
    out = CS._cap_trace_v2(nodes)
    assert len(out) == CS._MAX_TRACE_NODES                    # 集約ノードも込みでちょうど上限
    summary = out[0]
    assert summary["id"].startswith("trace-omitted:") and summary["kind"] == "tool"
    assert summary["metrics"]["omitted_count"] == 31
    assert [x["id"] for x in out[1:]] == [f"n{i}" for i in range(31, n)]    # 末尾（最新）優先
    _assert_no_orphans(out)
    with pytest.raises(ValueError):                           # 集約 id は通常イベントとしては使えない
        EE.build_event(summary["id"], "tool", "l", "d", "done")


def test_cap_trace_v2_soft_cap_mixed_kind_aggregates_per_group():
    n = CS._MAX_TRACE_NODES + 10
    nodes = {f"n{i}": _v2_node(i, kind="think" if i % 2 == 0 else "tool") for i in range(n)}
    out = CS._cap_trace_v2(nodes)
    summaries = [x for x in out if x["id"].startswith("trace-omitted:")]
    assert len(summaries) == 2
    by_kind = {s["kind"]: s for s in summaries}
    assert by_kind["think"]["metrics"]["omitted_count"] == 6 and by_kind["tool"]["metrics"]["omitted_count"] == 6
    assert len(out) == CS._MAX_TRACE_NODES
    _assert_no_orphans(out)


@pytest.mark.parametrize("extra,omitted,kept_evidence,omitted_evidence", [
    (3, 4, 4, None),                                          # K 未満なら omitted_evidence_count は立たない
    (25, 26, CS._MAX_TRACE_AGGREGATE_EVIDENCE_IDS, 6),
])
def test_cap_trace_v2_evidence_ids_capped_with_omitted_count(extra, omitted, kept_evidence, omitted_evidence):
    n = CS._MAX_TRACE_NODES + extra
    nodes = {f"n{i}": _v2_node(i, evidence_ids=[f"ev-{i:03d}"]) for i in range(n)}
    out = CS._cap_trace_v2(nodes)
    summary = next(x for x in out if x["id"].startswith("trace-omitted:"))
    assert summary["metrics"]["omitted_count"] == omitted
    assert len(summary["evidence_ids"]) == kept_evidence
    assert summary["metrics"].get("omitted_evidence_count") == omitted_evidence
    _assert_no_orphans(out)


def test_cap_trace_v2_soft_cap_keeps_all_parents_up_to_hard_cap():
    n_parents = CS._MAX_TRACE_NODES + 5
    nodes = _parent_child_nodes(n_parents)
    _assert_dict_ids_match_keys(nodes)
    out = CS._cap_trace_v2(nodes)
    out_ids = {x["id"] for x in out}
    assert all(f"p{i}" in out_ids for i in range(n_parents))                # 親は全件生き残る
    summaries = [x for x in out if x["id"].startswith("trace-omitted:")]
    assert len(summaries) == n_parents
    assert {s["parent_id"] for s in summaries} == {f"p{i}" for i in range(n_parents)}
    assert len(out) == n_parents + len(summaries)
    _assert_no_orphans(out)


def test_cap_trace_v2_hard_cap_collapses_oldest_subtrees_no_orphans():
    nodes = _parent_child_nodes(250)
    out = CS._cap_trace_v2(nodes)
    assert len(out) == CS._MAX_TRACE_NODES_HARD
    assert len([n for n in out if n["id"].startswith("trace-subtree:")]) == 100
    assert not any(n.get("event_type") == "budget_limit_reached" for n in out)
    out_ids = {n["id"] for n in out}
    assert "p0" not in out_ids and not any(n.get("parent_id") == "p0" for n in out)    # 最古は畳まれる
    assert "p249" in out_ids and any(n.get("parent_id") == "p249" for n in out)        # 最新は残る
    _assert_no_orphans(out)
    assert [n["id"] for n in out] == [n["id"] for n in CS._cap_trace_v2(dict(nodes))]   # 決定的


def test_cap_trace_v2_byte_cap_collapses_even_singleton_subtrees():
    nodes = {}
    for i in range(5):
        n = EE.build_event(f"b{i}", "tool", f"label{i}", "d", "done")
        n["metrics"] = {"blob": "x" * 300_000}
        nodes[f"b{i}"] = n
    out = CS._cap_trace_v2(nodes)
    assert CS._trace_bytes(out) <= CS._MAX_TRACE_BYTES
    assert len(out) == 5
    assert [n for n in out if n["id"].startswith("trace-subtree:")]
    _assert_no_orphans(out)


def test_cap_trace_v2_budget_limit_reached_marker_when_hard_cap_unresolvable():
    n = 500
    nodes = {f"n{i}": EE.build_event(f"n{i}", "tool", f"label{i}", "d", "done", agent_run_id=f"run-{i}")
             for i in range(n)}
    out = CS._cap_trace_v2(nodes)
    assert len(out) == CS._MAX_TRACE_NODES_HARD
    assert out[0]["id"] == EE.BUDGET_LIMIT_REACHED_ID and out[0]["event_type"] == "budget_limit_reached"
    assert out[0]["metrics"]["omitted_count"] == n - (CS._MAX_TRACE_NODES_HARD - 1)
    assert CS._trace_bytes(out) <= CS._MAX_TRACE_BYTES
    _assert_no_orphans(out, allow_truncated=True)             # honest failure 経路そのもの


def test_budget_limit_truncate_converges_bytes_even_when_count_truncation_is_not_enough():
    nodes = {}
    for i in range(450):
        n = EE.build_event(f"n{i}", "tool", f"label{i}", "d", "done")
        if i >= 50:
            n["metrics"] = {"blob": "x" * 5000}
        nodes[f"n{i}"] = n
    age = {nid: i for i, nid in enumerate(nodes)}
    naive_kept = CS._order_by_age(nodes, age)[-(CS._MAX_TRACE_NODES_HARD - 1):]
    naive_out = [CS._budget_limit_marker(450 - len(naive_kept), 450)] + naive_kept
    assert CS._trace_bytes(naive_out) > CS._MAX_TRACE_BYTES        # 前提: 件数だけでは収まらない
    out = CS._budget_limit_truncate(nodes, age, original_total=450)
    assert CS._trace_bytes(out) <= CS._MAX_TRACE_BYTES
    assert out[0]["id"] == EE.BUDGET_LIMIT_REACHED_ID and len(out) <= CS._MAX_TRACE_NODES_HARD
    assert out[0]["metrics"]["omitted_count"] == 450 - (len(out) - 1)


def test_budget_limit_truncate_converges_to_minimal_kept_set_under_extreme_bloat():
    nodes = {f"n{i}": EE.build_event(f"n{i}", "tool", f"l{i}", "d", "done", metrics={"blob": "x" * 50_000})
             for i in range(60)}
    age = {nid: i for i, nid in enumerate(nodes)}
    out = CS._budget_limit_truncate(nodes, age, original_total=60)
    assert CS._trace_bytes(out) <= CS._MAX_TRACE_BYTES
    assert out[0]["id"] == EE.BUDGET_LIMIT_REACHED_ID and len(out) > 1


def test_budget_limit_truncate_falls_back_to_marker_alone_when_byte_budget_is_extremely_tight(monkeypatch):
    nodes = {f"n{i}": EE.build_event(f"n{i}", "tool", f"l{i}", "d", "done") for i in range(450)}
    age = {nid: i for i, nid in enumerate(nodes)}
    marker_alone_bytes = CS._trace_bytes([CS._budget_limit_marker(450, 450)])
    monkeypatch.setattr(CS, "_MAX_TRACE_BYTES", marker_alone_bytes + 50)
    out = CS._budget_limit_truncate(nodes, age, original_total=450)
    assert len(out) == 1 and out[0]["id"] == EE.BUDGET_LIMIT_REACHED_ID
    assert out[0]["metrics"]["omitted_count"] == 450
    assert CS._trace_bytes(out) <= CS._MAX_TRACE_BYTES


def test_cap_trace_v2_dangling_parent_id_is_normalized(caplog):
    n = CS._MAX_TRACE_NODES + 30
    nodes = {f"n{i}": _v2_node(i) for i in range(n)}
    nodes["n5"]["parent_id"] = "does-not-exist-in-this-set"
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = CS._cap_trace_v2(nodes)                                   # クラッシュしない
    assert any("親なしへ正規化" in r.getMessage() for r in caplog.records)
    _assert_no_orphans(out)

    # 生き残った実ノード自身の parent_id も書き換わる（保護対象＝他ノードの親のケース）
    victim = "ghost-parent-victim"
    nodes = {victim: EE.build_event(victim, "tool", "l", "d", "done", parent_id="does-not-exist"),
             "child-of-victim": EE.build_event("child-of-victim", "tool", "l2", "d", "done", parent_id=victim)}
    out = CS._cap_trace_v2(nodes)
    assert {n["id"]: n for n in out}[victim]["parent_id"] is None
    _assert_no_orphans(out)

    # ソフト上限未満の高速経路でも正規化は必ず通る
    out = CS._cap_trace_v2({"only-one": EE.build_event("only-one", "tool", "l", "d", "done", parent_id="ghost")})
    assert len(out) == 1 and out[0]["parent_id"] is None


def test_group_and_subtree_ids_are_full_sha1_and_distinguish_none():
    g1, g2 = CS._group_id(None, "tool", "main"), CS._group_id("root", "tool", None)
    assert g1 != g2 and g1.startswith("trace-omitted:") and g2.startswith("trace-omitted:")
    assert len(g1.split(":", 1)[1]) == 40 and len(g2.split(":", 1)[1]) == 40
    assert CS._group_id(None, "tool", "main") == g1
    sid = CS._subtree_id("some-root-id")
    assert sid.startswith("trace-subtree:") and len(sid.split(":", 1)[1]) == 40


def test_reserved_id_namespaces_are_enforced():
    for bad_id in ("trace-omitted:" + "0" * 40, "trace-subtree:" + "0" * 40, EE.BUDGET_LIMIT_REACHED_ID):
        with pytest.raises(ValueError):
            EE.build_event(bad_id, "tool", "l", "d", "done")
    with pytest.raises(ValueError):
        EE._build_reserved_event("not-a-reserved-id", "tool", "l", "d", "done")


def test_assert_no_orphans_helper_detects_orphans_unless_explicitly_allowed():
    broken = [{"id": "a", "parent_id": "does-not-exist"}]
    with pytest.raises(AssertionError):
        _assert_no_orphans(broken)
    _assert_no_orphans(broken, allow_truncated=True)


# ---- 保存サイト（stream_message の answer・clarify） ----

class _FakeExecEventProvider:
    def __init__(self, events):
        self._events = events

    def run(self, ctx):
        return iter(self._events)


def _fixed_result(headline):
    return {"type": "_result",
            "env": {"headline": headline, "summary": {}, "data": {}, "sources": [],
                    "scope": {"world": "v1", "scope_paths": [], "source": "all"}},
            "decision": {"lens": "qa", "input": "q", "reason": "t"}}


def _fixed_result_with_investigation(headline, investigation_record):
    """provider が `_result` の別項目として渡す `investigation_record`（None＝台帳ゲートが走らなかった）付き。"""
    env = {"headline": headline, "summary": {}, "data": {}, "sources": [],
           "scope": {"world": "v1", "scope_paths": [], "source": "all"}}
    if investigation_record is not None:
        env["investigation"] = {"complete": investigation_record["complete"], "counts": {}}
    return {"type": "_result", "env": env, "decision": {"lens": "qa", "input": "q", "reason": "t"},
            "investigation_record": investigation_record}


def _fixed_question():
    return {"type": "question", "interaction_id": "q1", "mode": "single",
            "prompt": "確認したいことがあります。",
            "options": [{"id": "yes", "label": "はい", "description": ""},
                        {"id": "no", "label": "いいえ", "description": ""}],
            "allow_free_text": False}


def _mock_store_no_db(monkeypatch):
    """`store.*` を DB 不要のフェイクへ差し替える。戻り値は `add_message` に渡された行（挿入順）。"""
    saved: list = []
    counter = [0]

    def fake_add_message(conversation_id, role, content="", lens=None, route=None, trace=None,
                         answer=None, personal=False):
        counter[0] += 1
        row = {"id": counter[0], "conversation_id": conversation_id, "role": role, "content": content,
               "lens": lens, "route": route, "trace": trace, "answer": answer, "personal": personal}
        saved.append(row)
        return row

    def fake_set_message_personal(message_id):
        for row in saved:
            if row["id"] == message_id:
                row["personal"] = True

    monkeypatch.setattr(store, "add_message", fake_add_message)
    monkeypatch.setattr(store, "recent_messages", lambda conversation_id, limit: [])
    monkeypatch.setattr(store, "get_session_id", lambda conversation_id: None)
    monkeypatch.setattr(store, "get_codex_usage_total", lambda conversation_id: None)
    monkeypatch.setattr(store, "get_settings", lambda user_id: {})
    monkeypatch.setattr(store, "_read_system_settings_fresh", lambda **kw: {})   # get_provider と共有する唯一の読取点
    monkeypatch.setattr(store, "set_contains_personal_workspace", lambda *a, **k: None)
    monkeypatch.setattr(store, "set_message_personal", fake_set_message_personal)
    monkeypatch.setattr(store, "set_session_id", lambda *a, **k: None)
    monkeypatch.setattr(store, "audit", lambda *a, **k: None)
    monkeypatch.setattr(store, "conversation_is_personal_tainted", lambda conversation_id: False)
    return saved


def _use(monkeypatch, events):
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeExecEventProvider(events))


def _turn(message="質問", session=None, **kw):
    """stream_message を最後まで回し、配信イベント列を返す。"""
    kw = {"world": "v1", "conversation_id": 999, "user_id": "admin", "knowledge": False, **kw}
    return list(CS.stream_message(session, message, **kw))


def _answer_message(events):
    """配信イベント列から保存済み assistant メッセージ（answer イベントの message）を取り出す。"""
    return next(e for e in events if e.get("type") == "answer")["message"]


def _audits(monkeypatch) -> list:
    audits: list = []
    monkeypatch.setattr(store, "audit", lambda uid, action, *a, detail=None, **kw: audits.append(detail))
    return audits


def _rows(saved, role):
    return [r for r in saved if r["role"] == role]


@pytest.mark.parametrize("site", ["answer", "clarify"])
def test_mock_store_saves_trace_version_2(monkeypatch, site):
    """trace_version は常に 2（保存サイト共通）。"""
    saved = _mock_store_no_db(monkeypatch)
    _use(monkeypatch, [_NODE, _fixed_question() if site == "clarify" else _fixed_result("mock 回答")])
    _turn()
    if site == "clarify":
        assert saved[-1]["lens"] == "clarify"
    else:
        assert saved[-1]["role"] == "assistant"
        assert [n["id"] for n in saved[-1]["trace"]] == ["understand"]
    assert saved[-1]["answer"]["trace_version"] == 2


def test_stream_message_saves_investigation_record_when_present(monkeypatch):
    """台帳は assistant message 保存の直後にその message id で保存を試みる（complete/incomplete の両方）。
    台帳の中身（manifest/items/coverage）は `messages.answer`／`messages.trace` に一切現れない。"""
    saved_records: list = []
    monkeypatch.setattr(CS.store_investigation, "save_investigation_record",
                        lambda message_id, conversation_id, **kw: saved_records.append(
                            {"message_id": message_id, "conversation_id": conversation_id, **kw}))
    for complete in (True, False):
        investigation_record = {"complete": complete,
                                "manifest": {"question_kind": "qa", "created_at": "t", "items": ["i1"]},
                                "items": {"i1": {"id": "i1", "subject": "本文っぽい何か"}},
                                "coverage": {"i1": ["hit"]}}
        _use(monkeypatch, [_fixed_result_with_investigation(f"回答(complete={complete})", investigation_record)])
        saved = _mock_store_no_db(monkeypatch)
        msg = _answer_message(_turn("台帳つきテスト"))
        assert msg["answer"]["investigation"]["recorded"] is True
        assert "manifest" not in msg["answer"]["investigation"]
        assert "本文っぽい何か" not in str(msg["answer"])
        for node in (msg["trace"] or []):
            assert "manifest" not in node and "items" not in node
        assert saved[-1] is msg
    assert [r["complete"] for r in saved_records] == [True, False]
    assert all(r["conversation_id"] == 999 and r["manifest"]["question_kind"] == "qa" for r in saved_records)
    assert all(r["items"]["i1"]["subject"] == "本文っぽい何か" for r in saved_records)


def test_stream_message_skips_investigation_save_when_no_ledger(monkeypatch):
    _mock_store_no_db(monkeypatch)
    save_calls: list = []
    monkeypatch.setattr(CS.store_investigation, "save_investigation_record",
                        lambda *a, **k: save_calls.append((a, k)))
    _use(monkeypatch, [_fixed_result("台帳なし回答")])
    msg = _answer_message(_turn("台帳なしテスト"))
    assert "investigation" not in msg["answer"] and save_calls == []


# ---- 個人由来の引き継ぎ ----

@pytest.mark.parametrize("tainted", [True, False])
def test_turn_inherits_conversation_personal_taint(monkeypatch, tainted):
    """会話が既に個人由来なら personal=False のターンの user/assistant 行も personal=True で保存する。"""
    saved = _mock_store_no_db(monkeypatch)
    monkeypatch.setattr(store, "conversation_is_personal_tainted", lambda conversation_id: tainted)
    _use(monkeypatch, [_fixed_result("回答")])
    _turn("2ターン目の質問", personal=False)
    assert _rows(saved, "user")[0]["personal"] is tainted
    assert _rows(saved, "assistant")[0]["personal"] is tainted


@pytest.mark.parametrize("wrote_files,expected", [(["一覧.md"], True), (None, False), (True, True)])
def test_turn_marks_personal_when_env_has_wrote_files(monkeypatch, wrote_files, expected):
    """書込みを発生させたターン（`env["wrote_files"]`）は個人由来（作成物カードの有無では判定しない）。"""
    saved = _mock_store_no_db(monkeypatch)
    env = {"headline": "一覧.md を作成しました。", "summary": {}, "data": {}, "sources": [],
           "scope": {"world": "v1", "scope_paths": [], "source": "all"}}
    if wrote_files:
        env["wrote_files"] = wrote_files
    if isinstance(wrote_files, list):
        env["created_files"] = [{"name": "一覧.md", "download_url": "/workspace/files/1/download"}]
    _use(monkeypatch, [{"type": "_result", "env": env, "decision": {"lens": "author", "input": "q", "reason": "t"}}])
    _turn("消費税率の一覧をExcelにまとめて", personal=False)
    assert _rows(saved, "user")[0]["personal"] is expected
    assert _rows(saved, "assistant")[0]["personal"] is expected


def test_stream_message_saves_clarify_card(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    _use(monkeypatch, [_fixed_question()])
    events = _turn("確認が要る質問")
    assert next(e for e in events if e.get("type") == "question")["conversation_id"] == 999
    assert saved[-1]["role"] == "assistant" and saved[-1]["lens"] == "clarify"
    assert saved[-1]["answer"]["question"]["interaction_id"] == "q1"


def test_stream_message_clarify_inherits_conversation_personal_taint(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    monkeypatch.setattr(store, "conversation_is_personal_tainted", lambda conversation_id: True)
    marked: list = []
    monkeypatch.setattr(store, "set_message_personal", lambda mid: marked.append(mid))
    _use(monkeypatch, [_fixed_question()])
    _turn("確認が要る質問", personal=False)
    assert saved[-1]["lens"] == "clarify" and saved[-1].get("personal") is True
    assert marked


def test_stream_message_personal_check_failure_falls_closed(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)

    def _boom(conversation_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "conversation_is_personal_tainted", _boom)
    _use(monkeypatch, [_fixed_result("回答")])
    _turn(personal=False)
    assert saved[-1]["role"] == "assistant" and saved[-1].get("personal") is True


# ---- system_settings は 1 ターン 1 回の fresh read を共有する ----

def test_turn_shares_one_system_settings_snapshot_with_get_provider(monkeypatch):
    _mock_store_no_db(monkeypatch)
    sentinel = {"depth_base_grep_max_hits": 42}
    monkeypatch.setattr(store, "_read_system_settings_fresh", lambda **kw: sentinel)
    captured = {}

    def fake_get_provider(settings, system_settings=None):
        captured["system_settings"] = system_settings
        return _FakeExecEventProvider([_fixed_result("mock 回答")])

    monkeypatch.setattr(CS, "get_provider", fake_get_provider)
    _turn()
    assert captured["system_settings"] is sentinel


def test_turn_fails_closed_when_system_settings_read_fails(monkeypatch):
    _mock_store_no_db(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "_read_system_settings_fresh", _boom)
    _use(monkeypatch, [_fixed_result("x")])
    with pytest.raises(RuntimeError):
        _turn()


@pytest.mark.parametrize("stored,requested", [(True, False), (False, True), (False, False), (True, True)])
def test_turn_web_search_param_overrides_stored_codex_web_search(monkeypatch, stored, requested):
    """実行に使う codex_web_search はチャットごとの引数のみ（保存済み個人設定は無視）。"""
    _mock_store_no_db(monkeypatch)
    monkeypatch.setattr(store, "get_settings", lambda user_id: {"codex_web_search": stored})
    captured = {}

    def _fake_get_provider(settings, **kw):
        captured["settings"] = settings
        return _FakeExecEventProvider([_fixed_result("mock 回答")])

    monkeypatch.setattr(CS, "get_provider", _fake_get_provider)
    _turn(web_search=requested)
    assert captured["settings"]["codex_web_search"] is requested


# ---- 1 ターンの所要時間（answer.duration_ms）・停止 ----

@pytest.mark.parametrize("events,t0,t1,expected", [
    ([_fixed_result("mock stream 回答")], 200.0, 200.5, 500),
    ([_fixed_question()], 300.0, 300.1, 100),
], ids=["answer", "clarify"])
def test_turn_saves_duration_ms(monkeypatch, events, t0, t1, expected):
    saved = _mock_store_no_db(monkeypatch)
    it = iter((t0, t1))
    monkeypatch.setattr(CS.time, "monotonic", lambda: next(it))
    _use(monkeypatch, events)
    _turn()
    assert saved[-1]["role"] == "assistant" and saved[-1]["answer"]["duration_ms"] == expected


def _stopped_terminal_result(headline="1巡目で打ち切りました（利用者の操作で停止したため）。"):
    ev = _fixed_result(headline)
    ev["env"]["_terminal"] = "stopped"
    ev["env"]["stopped_by_user"] = True
    return ev


def _stopped():
    ev = threading.Event()
    ev.set()
    return ev


def test_stop_before_result_saves_no_assistant_and_audits_stop_once(monkeypatch):
    """停止後に届く通常の `_result` は保存しない（duration_ms も存在しない）。stopped 応答＋停止監査は 1 回。"""
    saved = _mock_store_no_db(monkeypatch)
    audits = _audits(monkeypatch)
    _use(monkeypatch, [_NODE, _fixed_result("到達しないはずの回答")])
    out = _turn(stop_event=_stopped())
    assert _rows(saved, "assistant") == []
    assert [e.get("type") for e in out].count("stopped") == 1
    assert [d["stopped"] for d in audits] == [True]


@pytest.mark.parametrize("with_node", [False, True])
def test_stopped_terminal_is_saved_as_incomplete_answer(monkeypatch, with_node):
    """停止終端だけは保存する（停止後も終端まで読み切る・途中ノードは配信も保存もしない・監査は 1 回）。"""
    saved = _mock_store_no_db(monkeypatch)
    audits = _audits(monkeypatch)
    node = {"type": "node", "id": "main-review-r1", "kind": "think", "label": "査読",
            "detail": "読んでいます", "status": "done"}
    _use(monkeypatch, ([node] if with_node else []) + [_stopped_terminal_result()])
    out = _turn("停止終端 テスト", stop_event=_stopped())
    assistant = _rows(saved, "assistant")
    assert len(assistant) == 1 and "打ち切りました" in assistant[0]["content"]
    assert assistant[0]["answer"]["stop_kind"] == "stopped_by_user"      # 完了として数えない
    assert "_terminal" not in assistant[0]["answer"]                     # 内部キーは保存しない
    assert [d["stopped"] for d in audits] == [True] and audits[-1]["lens"] == "stopped"
    assert audits[-1]["message_id_assistant"] == assistant[0]["id"]
    assert any(e.get("type") == "answer" for e in out)
    assert not [e for e in out if e.get("type") in ("node", "stopped")]


def test_round_personal_flag_marks_answer_personal_and_is_not_saved(monkeypatch):
    """巡ループが全巡で累積した個人由来フラグは最終巡に無くても回答を個人扱いにし、内部キーは保存しない。"""
    _mock_store_no_db(monkeypatch)
    ev = _fixed_result("巡の途中で個人 workspace へ書いた回答")
    ev["env"]["_personal_rounds"] = True
    _use(monkeypatch, [ev])
    msg = _answer_message(_turn(personal=False))
    assert msg["personal"] is True and "_personal_rounds" not in msg["answer"]


def test_round_personal_flag_marks_clarify_card_personal_and_is_not_saved(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    _use(monkeypatch, [{**_fixed_question(), "_personal_rounds": True}])
    out = _turn()
    clarify = _rows(saved, "assistant")
    assert len(clarify) == 1 and clarify[0]["personal"] is True
    assert "_personal_rounds" not in clarify[0]["answer"] and all("_personal_rounds" not in e for e in out)


# ---- evidence_committed は `_result.env` のサイドカー（独立イベントとして yield しない） ----

def _evidence_committed_sidecar():
    return {"type": "node", "id": "evidence-committed", "kind": "evidence", "label": "根拠を確定",
            "detail": "1 件の根拠を機械検証済みとして確定しました", "status": "done",
            "event_type": "evidence_committed", "evidence_ids": ["ev-1"]}


def test_stream_message_evidence_committed_sidecar_persisted_and_streamed_after_result(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    result = _fixed_result("evidence 付き回答")
    result["env"]["_evidence_committed"] = _evidence_committed_sidecar()
    _use(monkeypatch, [_NODE, result])
    out_events = _turn()
    assert "_evidence_committed" not in saved[-1]["answer"]
    assert "evidence-committed" in [n["id"] for n in saved[-1]["trace"]]
    idx_node = next(i for i, e in enumerate(out_events)
                    if e.get("type") == "node" and e.get("id") == "evidence-committed")
    idx_answer = next(i for i, e in enumerate(out_events) if e.get("type") == "answer")
    assert idx_node < idx_answer                                  # 永続化成功後に配信（孤児化しない順序）


def _stoppable_provider(stop_event, env):
    class _P:
        def run(self, ctx):
            yield dict(_NODE)
            stop_event.set()
            yield {"type": "_result", "env": env, "decision": {"lens": "qa", "input": "q", "reason": "t"}}
    return _P()


def test_stream_message_stop_before_result_discards_evidence_committed_sidecar_atomically(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    stop_event = threading.Event()
    env = {"headline": "回答", "summary": {}, "data": {}, "sources": [],
           "scope": {"world": "v1", "scope_paths": [], "source": "all"},
           "_evidence_committed": _evidence_committed_sidecar()}
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _stoppable_provider(stop_event, env))
    out_events = _turn("stop test", stop_event=stop_event)
    assert _rows(saved, "assistant") == []
    assert any(e.get("type") == "stopped" for e in out_events)
    assert not any(e.get("event_type") == "evidence_committed" for e in out_events)


def test_stream_message_stop_mid_stream_forwards_partial_deltas_but_persists_no_assistant(monkeypatch):
    """停止時は配信済みの部分本文を client は受け取るが、`_result` は discard され履歴には残らない。"""
    saved = _mock_store_no_db(monkeypatch)
    stop_event = threading.Event()
    partial_text = "調査した結果、"

    class _P:
        def run(self, ctx):
            for ch in partial_text:
                yield {"type": "answer_delta", "text": ch}
            stop_event.set()
            yield {"type": "_result",
                   "env": {"headline": partial_text, "summary": {}, "data": {}, "sources": [],
                           "scope": {"world": "v1", "scope_paths": [], "source": "all"}},
                   "decision": {"lens": "qa", "input": "q", "reason": "t"}}

    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _P())
    out_events = _turn("mid-stream stop test", stop_event=stop_event)
    assert "".join(e["text"] for e in out_events if e.get("type") == "answer_delta") == partial_text
    assert any(e.get("type") == "stopped" for e in out_events)
    assert not any(e.get("type") == "answer" for e in out_events)
    assert _rows(saved, "assistant") == []


def test_stream_message_persistence_failure_prevents_sidecar_live_delivery(monkeypatch):
    _mock_store_no_db(monkeypatch)

    def failing_add_message(conversation_id, role, content="", **k):
        if role == "assistant":
            raise RuntimeError("db write failed")
        return {"id": 1, "conversation_id": conversation_id, "role": role, "content": content, **k}

    monkeypatch.setattr(store, "add_message", failing_add_message)
    result = _fixed_result("evidence 付き回答")
    result["env"]["_evidence_committed"] = _evidence_committed_sidecar()
    _use(monkeypatch, [_NODE, result])
    collected = []
    with pytest.raises(RuntimeError, match="db write failed"):
        for ev in CS.stream_message(None, "persist fail test", world="v1", conversation_id=999,
                                    user_id="admin", knowledge=False):
            collected.append(ev)
    assert not any(e.get("event_type") == "evidence_committed" for e in collected)
    assert not any(e.get("type") == "answer" for e in collected)


# ---- stream_message end-to-end（要 Postgres・DB down は skip） ----

def test_stream_message_saves_trace_version_and_hierarchy(monkeypatch):
    conv_id = _new_conv()
    parent = EE.build_event("agent-1", "agent", "サブ開始", "worker1 を起動", "done",
                            event_type="agent_started", agent_run_id="sub:worker1:1",
                            parent_agent_run_id="main", run_id="run-abc")
    child = EE.build_event("tool-1", "tool", "資料を検索", "「消費税」", "done",
                           event_type="tool_started", parent_id="agent-1",
                           agent_run_id="sub:worker1:1", run_id="run-abc", phase="gather", seq=1)
    _use(monkeypatch, [parent, child, _fixed_result("v2 テスト回答")])
    msg = _answer_message(list(CS.stream_message(None, "EXT-1 flag on テスト", world="v1", conversation_id=conv_id,
                                                 user_id="admin", knowledge=False)))
    assert msg["answer"]["trace_version"] == 2
    by_id = {n["id"]: n for n in msg["trace"]}
    assert by_id["agent-1"]["agent_run_id"] == "sub:worker1:1"
    assert by_id["agent-1"]["event_type"] == "agent_started"
    assert by_id["tool-1"]["parent_id"] == "agent-1" and by_id["tool-1"]["agent_run_id"] == "sub:worker1:1"
    assert by_id["tool-1"]["phase"] == "gather"


# ---- 履歴 priming（`_clip_history_msg` 純関数・`_history_pairs` は要 Postgres） ----

def test_clip_history_msg():
    assert CS._clip_history_msg("短い文") == "短い文"
    assert CS._clip_history_msg("") == "" and CS._clip_history_msg(None) == ""
    out = CS._clip_history_msg("あ" * (CS._HISTORY_MSG_CHARS + 50))
    assert out.startswith("あ" * 10) and out.endswith("…（省略）")
    assert len(out) == CS._HISTORY_MSG_CHARS + len("…（省略）")


def _add_pairs(cid, *pairs):
    for q, a in pairs:
        store.add_message(cid, "user", q)
        if a is not None:
            store.add_message(cid, "assistant", a)


def _pair_msgs(*pairs):
    return [m for q, a in pairs for m in ({"role": "user", "content": q}, {"role": "assistant", "content": a})]


def test_history_pairs_none_conversation_id_returns_empty():
    assert CS._history_pairs(None) == []


def test_history_pairs_only_complete_pairs_in_chronological_order():
    cid = _new_conv()
    _add_pairs(cid, ("質問1", "回答1"), ("質問2", "回答2"))
    assert CS._history_pairs(cid) == _pair_msgs(("質問1", "回答1"), ("質問2", "回答2"))


def test_history_pairs_drops_unpaired_user_row_from_stopped_turn():
    """途中停止で assistant 未保存のまま残る不対 user 行は履歴から落ちる（交互制約に対して安全）。"""
    cid = _new_conv()
    _add_pairs(cid, ("質問1", "回答1"), ("止められた質問", None), ("質問2", "回答2"))
    hist = CS._history_pairs(cid)
    assert hist == _pair_msgs(("質問1", "回答1"), ("質問2", "回答2"))
    assert "止められた質問" not in [m["content"] for m in hist]


def test_history_pairs_caps_to_recent_n_pairs():
    cid = _new_conv()
    n = CS._HISTORY_TURNS + 2
    _add_pairs(cid, *[(f"質問{i}", f"回答{i}") for i in range(n)])
    hist = CS._history_pairs(cid)
    assert len(hist) == CS._HISTORY_TURNS * 2
    assert [m["content"] for m in hist if m["role"] == "user"] == [f"質問{i}" for i in range(n - CS._HISTORY_TURNS, n)]


def test_history_pairs_respects_char_budget_dropping_oldest_pairs_first():
    cid = _new_conv()
    big = "x" * 1000
    _add_pairs(cid, *[(f"{big}-u{i}", f"{big}-a{i}") for i in range(5)])
    hist = CS._history_pairs(cid)
    assert sum(len(m["content"]) for m in hist) <= CS._HISTORY_CHAR_BUDGET
    kept_users = [m["content"] for m in hist if m["role"] == "user"]
    assert kept_users[-1] == f"{big}-u4" and f"{big}-u0" not in kept_users


def test_history_pairs_clips_individual_message_over_char_limit():
    cid = _new_conv()
    _add_pairs(cid, ("質問1", "あ" * (CS._HISTORY_MSG_CHARS + 100)))
    a = next(m for m in CS._history_pairs(cid) if m["role"] == "assistant")
    assert len(a["content"]) == CS._HISTORY_MSG_CHARS + len("…（省略）") and a["content"].endswith("…（省略）")


def test_history_pairs_degrades_to_empty_on_read_failure(monkeypatch):
    def _boom(conversation_id, limit):
        raise RuntimeError("boom")
    monkeypatch.setattr(store, "recent_messages", _boom)
    assert CS._history_pairs(123) == []


def test_history_pairs_survives_unpaired_row_pileup_pushing_window():
    """固定窓のまま不対行が積まれても古い完全対が押し出されない（段階的な窓拡大）。"""
    cid = _new_conv()
    n = CS._HISTORY_TURNS
    _add_pairs(cid, *[(f"質問{i}", f"回答{i}") for i in range(n)])
    _add_pairs(cid, *[(f"止められた質問{i}", None) for i in range(10)])
    kept_users = [m["content"] for m in CS._history_pairs(cid) if m["role"] == "user"]
    assert kept_users == [f"質問{i}" for i in range(n)]


def test_history_pairs_zero_turns_disables_priming(monkeypatch):
    cid = _new_conv()
    _add_pairs(cid, ("質問1", "回答1"))
    monkeypatch.setattr(CS, "_HISTORY_TURNS", 0)
    assert CS._history_pairs(cid) == []


def test_history_pairs_window_expansion_capped_at_512_rows(monkeypatch):
    calls = []

    def _fake_recent_messages(conversation_id, limit):
        calls.append(limit)
        return [{"id": i, "role": "user", "content": f"u{i}"} for i in range(limit)]   # 常に不対な user 行

    monkeypatch.setattr(store, "recent_messages", _fake_recent_messages)
    assert CS._history_pairs(999) == []
    assert calls[-1] == 512 and calls == sorted(calls)


# ---- 固定文言の縮退（Neo4j 安全弁） ----

def test_impact_overload_result_shape_is_fixed_and_preserves_scope_meta():
    r = CS._impact_overload_result("消費税率を変えたら", "w1", None)
    env, decision = r["env"], r["decision"]
    assert env["lens"] == "impact" and env["headline"] == CS.GRAPH_OVERLOAD_USER_MESSAGE
    assert env["summary"] == {"total": 0} and env["data"] == {} and env["sources"] == []
    assert env["scope"] == {"world": "w1", "scope_paths": [], "source": "all", "layer": "both", "layer_applied": False}
    assert decision["lens"] == "impact" and decision["input"] == "消費税率を変えたら"
    sm = {"world": "w1", "scope_paths": ["4期/設計"], "source": "explicit"}
    assert CS._impact_overload_result("m", "w1", sm)["env"]["scope"] == {**sm, "layer_applied": False}


# ---- _dispatch の配線（layer・調べる深さ・検索経路） ----

def _patch_runners(monkeypatch) -> dict:
    """run_qa/run_impact/run_troubleshoot と ES 補完を差し替え、渡された引数を捕捉する。
    impact/troubleshoot の固定シグネチャは layer を受け取らない（渡されたら TypeError で検出）。"""
    cap: dict = {}

    def run_qa(payload, world, scope_paths=None, layer=None, max_hits=None):
        cap.update(run_qa_layer=layer, max_hits=max_hits)
        return {"type": "qa", "question": payload, "answered": True, "citations": []}

    def merge_qa(result, world, query, sp, layer=None):
        cap["merge_layer"] = layer
        return result

    def run_impact(session, payload, world, scope_prefixes=None, depth=None):
        cap["impact_depth"] = depth
        return {"items": [], "presumed": [], "start": payload, "starts": []}

    def run_ts(session, symptom, world, scope_paths=None, depth=None):
        cap["ts_depth"] = depth
        return {"type": "troubleshoot", "world": world, "symptom": symptom, "anchors": [], "candidates": []}

    monkeypatch.setattr(CS, "run_qa", run_qa)
    monkeypatch.setattr(CS, "_merge_qa_with_es", merge_qa)
    monkeypatch.setattr(CS, "run_impact", run_impact)
    monkeypatch.setattr(CS, "run_troubleshoot", run_ts)
    monkeypatch.setattr(CS, "_merge_troubleshoot_with_es", lambda result, world, query, sp: result)
    return cap


def _sm(depth_profile=None, **extra):
    return {"world": "w1", "scope_paths": [], "source": "all", "layer": "both",
            "depth_profile": depth_profile, **extra}


_PAYLOADS = {"qa": "消費税率とは", "impact": "消費税率", "troubleshoot": "夜間バッチ停止"}


@pytest.mark.parametrize("lens,layer,applied", [("qa", "code", True), ("impact", "code", False),
                                                ("troubleshoot", "docs", False)])
def test_dispatch_layer_wiring(monkeypatch, lens, layer, applied):
    cap = _patch_runners(monkeypatch)
    env = CS._dispatch(None, lens, _PAYLOADS[lens], "w1",
                       {"world": "w1", "scope_paths": [], "source": "all", "layer": layer})
    assert env["scope"] == {"world": "w1", "scope_paths": [], "source": "all", "layer": layer,
                            "layer_applied": applied}
    if lens == "qa":
        assert (cap["run_qa_layer"], cap["merge_layer"]) == (layer, layer)


def test_dispatch_no_scope_meta_defaults_to_both_and_qa_applies(monkeypatch):
    _patch_runners(monkeypatch)
    env = CS._dispatch(None, "qa", "消費税率とは", "w1", None)
    assert env["scope"] == {"world": "w1", "scope_paths": [], "source": "all", "layer": "both", "layer_applied": True}


@pytest.mark.parametrize("lens,profile,settings,key,expected", [
    ("impact", None, None, "impact_depth", 10), ("impact", "standard", None, "impact_depth", 10),
    ("impact", "deep", None, "impact_depth", 12), ("impact", "max", None, "impact_depth", 14),
    ("troubleshoot", None, None, "ts_depth", 4), ("troubleshoot", "standard", None, "ts_depth", 4),
    ("troubleshoot", "deep", None, "ts_depth", 6), ("troubleshoot", "max", None, "ts_depth", 8),
    ("qa", None, None, "max_hits", 20), ("qa", "standard", None, "max_hits", 20),
    ("qa", "deep", None, "max_hits", 30), ("qa", "max", None, "max_hits", 40),
    # 管理画面の基準値編集が env 既定より優先され、加算・倍率はその実効基準値に載る
    ("impact", "deep", {"depth_base_impact_depth": 20}, "impact_depth", 22),
    # 倍率・加算の適用後に絶対上限でクランプされる
    ("impact", "max", {"depth_base_impact_depth": 68}, "impact_depth", 64),
    ("troubleshoot", "max", {"depth_base_troubleshoot_depth": 20}, "ts_depth", 16),
    ("qa", "max", {"depth_base_qa_max_hits": 2000}, "max_hits", 1000),
])
def test_dispatch_depth_profile_scales_base_and_clamps_at_abs_max(monkeypatch, lens, profile, settings, key, expected):
    cap = _patch_runners(monkeypatch)
    CS._dispatch(None, lens, _PAYLOADS[lens], "w1", _sm(profile), system_settings=settings)
    assert cap[key] == expected


def _raise_if_called(*_a, **_kw):
    raise AssertionError("OFF/不達のツールが呼ばれてしまった（迂回封鎖のはずが実行された）")


@pytest.mark.parametrize("lens,runner", [("impact", "run_impact"), ("troubleshoot", "run_troubleshoot")])
def test_dispatch_graph_lens_degrades_when_graph_off_but_search_remains(monkeypatch, lens, runner):
    """グラフ OFF/不達でも grep か全文が残っていれば明示エラーで終わらせず qa 相当の下地へ縮退する。"""
    monkeypatch.setattr(CS, runner, _raise_if_called)
    env = CS._dispatch(None, lens, _PAYLOADS[lens], "w1", _sm(tools={"grep": True, "fulltext": True, "graph": False}))
    assert env["graph_degraded"] == "blocked" and env["data"]["type"] == "qa"


def test_dispatch_impact_blocked_when_no_search_tool_remains(monkeypatch):
    for name in ("run_impact", "run_qa", "_es_citations"):
        monkeypatch.setattr(CS, name, _raise_if_called)
    env = CS._dispatch(None, "impact", "消費税率", "w1", _sm(),
                       tools_availability={"grep": False, "fulltext": False, "graph": False})
    assert env["data"] == {} and env["sources"] == [] and "グラフ" in env["headline"]


def test_dispatch_qa_blocked_when_grep_and_fulltext_off_returns_honest_failure(monkeypatch):
    monkeypatch.setattr(CS, "run_qa", _raise_if_called)
    monkeypatch.setattr(CS, "_es_citations", _raise_if_called)
    env = CS._dispatch(None, "qa", "消費税率とは", "w1", _sm(tools={"grep": False, "fulltext": False, "graph": True}))
    assert env["data"] == {} and env["sources"] == []


def test_dispatch_qa_skips_es_merge_when_fulltext_off(monkeypatch):
    monkeypatch.setattr(CS, "run_qa", lambda payload, world, scope_paths=None, layer=None, max_hits=None:
                        {"type": "qa", "question": payload, "answered": True,
                         "citations": [{"doc_id": "a.md", "quote": "x", "span": [1, 1]}]})
    monkeypatch.setattr(CS, "_merge_qa_with_es", _raise_if_called)
    assert CS._dispatch(None, "qa", "消費税率とは", "w1", _sm(tools={"fulltext": False}))["summary"]["total"] == 1


def test_dispatch_qa_uses_es_only_when_grep_off(monkeypatch):
    monkeypatch.setattr(CS, "run_qa", _raise_if_called)
    monkeypatch.setattr(CS, "_es_citations", lambda world, query, sp, layer=None:
                        [{"doc_id": "b.md", "quote": "y", "span": [2, 2]}])
    env = CS._dispatch(None, "qa", "消費税率とは", "w1", _sm(tools={"grep": False}))
    assert env["summary"]["total"] == 1 and env["data"]["citations"][0]["doc_id"] == "b.md"


def test_dispatch_troubleshoot_skips_es_merge_when_fulltext_off(monkeypatch):
    monkeypatch.setattr(CS, "run_troubleshoot", lambda session, symptom, world, scope_paths=None, depth=None:
                        {"type": "troubleshoot", "world": world, "symptom": symptom, "anchors": [],
                         "candidates": [{"name": "X", "label": "Program", "category": "コード", "role": "近傍",
                                         "distance": 1, "path": [], "evidence": {}}]})
    monkeypatch.setattr(CS, "_merge_troubleshoot_with_es", _raise_if_called)
    assert CS._dispatch(None, "troubleshoot", "夜間バッチ停止", "w1", _sm(tools={"fulltext": False}))["summary"]["total"] == 1


def test_dispatch_tools_availability_param_decides_regardless_of_pref(monkeypatch):
    monkeypatch.setattr(CS, "run_impact", _raise_if_called)
    env = CS._dispatch(None, "impact", "消費税率", "w1", _sm(),
                       tools_availability={"grep": True, "fulltext": True, "graph": False})
    assert env["graph_degraded"] == "graph_unavailable"          # 実接続の不達＝統計に残す側のコード
    cap = _patch_runners(monkeypatch)                             # 省略時は全て利用可能扱い
    CS._dispatch(None, "impact", "消費税率", "w1", _sm())
    assert cap["impact_depth"] is not None


# ---- _resolve_scope / _resolve_lens ----

_TOOLS_ALL = {"grep": True, "fulltext": True, "graph": True}
_DEFAULT_SCOPE = {"world": "w1", "scope_paths": [], "source": "all", "layer": "both", "lens_source": "auto",
                  "lens_block": None, "web_search": False, "depth_profile": "standard", "tools": _TOOLS_ALL}


def test_resolve_scope_defaults_and_valid_layer_passthrough():
    assert CS._resolve_scope("質問", "w1", []) == _DEFAULT_SCOPE
    assert CS._resolve_scope("質問", "w1", ["4期/設計"], "code") == {
        **_DEFAULT_SCOPE, "scope_paths": ["4期/設計"], "source": "explicit", "layer": "code"}


@pytest.mark.parametrize("kwargs,key,expected", [
    ({"lens_source": "explicit"}, "lens_source", "explicit"),
    ({"lens_source": "slash"}, "lens_source", "slash"),
    ({"lens_source": "slash", "lens_block": "qa"}, "lens_block", "qa"),    # スラッシュでもブロックの継続設定を保持
    ({"web_search": True}, "web_search", True),
    ({"depth_profile": "standard"}, "depth_profile", "standard"),
    ({"depth_profile": "deep"}, "depth_profile", "deep"),
    ({"depth_profile": "max"}, "depth_profile", "max"),
    ({"tools": {"grep": False, "fulltext": True, "graph": True}}, "tools",
     {"grep": False, "fulltext": True, "graph": True}),
])
def test_resolve_scope_passthrough(kwargs, key, expected):
    assert CS._resolve_scope("質問", "w1", [], **kwargs)[key] == expected


@pytest.mark.parametrize("kwargs", [
    {"layer": "bogus"}, {"depth_profile": "bogus"}, {"tools": {"grep": False, "fulltext": False, "graph": False}},
], ids=["layer", "depth_profile", "tools_all_off"])
def test_resolve_scope_invalid_value_raises(kwargs):
    """省略（None）だけが既定・内部の不正値は ValueError（fail-loud）。"""
    args = ("質問", "w1", [], kwargs.pop("layer")) if "layer" in kwargs else ("質問", "w1", [])
    with pytest.raises(ValueError):
        CS._resolve_scope(*args, **kwargs)


@pytest.mark.parametrize("lens_in,message,expected", [
    (None, "消費税率を変えたい", (None, "auto", None, "消費税率を変えたい")),
    ("auto", "消費税率を変えたい", (None, "auto", None, "消費税率を変えたい")),
    ("impact", "消費税率を変えたい", ("impact", "explicit", "impact", "消費税率を変えたい")),
    # スラッシュ接頭辞は ChatReq.lens より優先し本文から除く・ブロックの継続設定は lens_block に残す
    ("qa", "/影響 消費税率を変えたい", ("impact", "slash", "qa", "消費税率を変えたい")),
    (None, "/原因 x", ("troubleshoot", "slash", None, "x")),
    (None, "/内容 x", ("qa", "slash", None, "x")),
    (None, "/作成 x", ("author", "slash", None, "x")),
    (None, "これは /影響 ではない", (None, "auto", None, "これは /影響 ではない")),
])
def test_resolve_lens(lens_in, message, expected):
    assert CS._resolve_lens(lens_in, message) == expected


# ---- 巡の縮退・履歴の混入・_known_terms ----

def _fake_provider_gen(events):
    def _gen():
        for ev in events:
            if isinstance(ev, BaseException):
                raise ev
            yield ev
    return _gen()


def test_degrade_overload_passthrough_converts_overload_and_does_not_swallow_others(caplog):
    from sherpa.ingest.world_neo4j import GraphQueryOverloadError
    events = [{"type": "node", "id": "n1"}, {"type": "_result", "env": {"headline": "ok"}, "decision": {}}]
    assert list(CS._degrade_overload(_fake_provider_gen(events), "m", "w1", None)) == events

    events = [{"type": "node", "id": "tool-graph"}, GraphQueryOverloadError("timeout", world="w1")]
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = list(CS._degrade_overload(_fake_provider_gen(events), "消費税率", "w1", None))
    assert len(out) == 2 and out[0] == {"type": "node", "id": "tool-graph"}
    assert out[1]["type"] == "_result" and out[1]["env"]["headline"] == CS.GRAPH_OVERLOAD_USER_MESSAGE
    assert out[1]["decision"]["lens"] == "impact"
    assert any("安全弁で縮退" in r.getMessage() and "w1" in r.getMessage() for r in caplog.records)

    with pytest.raises(RuntimeError):
        list(CS._degrade_overload(_fake_provider_gen([RuntimeError("boom")]), "m", "w1", None))


def test_history_does_not_leak_into_message_for_confirm_id_or_routing():
    from sherpa import chat_router
    history = [{"role": "user", "content": "選択: 影響を調べる\n確認ID: ask-0011\n元の依頼: 消費税率を変えたい"},
               {"role": "assistant", "content": "影響分析の結果です。"}]
    assert "確認ID" in history[0]["content"]
    current_message = "追加で教えて"
    assert chat_router._resume_lens(current_message) == (None, None)


class _CSFakeRecord:
    def __init__(self, d):
        self._d = d

    def data(self):
        return dict(self._d)


class _CSFakeResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(_CSFakeRecord(r) for r in self._rows)

    def consume(self):
        pass


class _CSFakeSession:
    def __init__(self, rows=None, raise_exc=None):
        self._rows = rows or []
        self._raise_exc = raise_exc

    def run(self, query, **params):
        if self._raise_exc is not None:
            raise self._raise_exc
        return _CSFakeResult(self._rows)


def test_known_terms_degrades_softly_and_keeps_shape(caplog):
    from neo4j.exceptions import Neo4jError
    from sherpa import lens_service as LS
    exc = Neo4jError._hydrate_neo4j(
        code="Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration", message="timed out")
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert CS._known_terms(_CSFakeSession(raise_exc=exc), "w1") == []
    assert any("タイムアウト" in r.getMessage() for r in caplog.records)
    caplog.clear()
    rows = [{"name": f"NODE{i}"} for i in range(LS._NEO4J_MAX_ROWS + 5)]
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert len(CS._known_terms(_CSFakeSession(rows=rows), "w1")) == LS._NEO4J_MAX_ROWS
    assert any("緊急天井" in r.getMessage() for r in caplog.records)
    assert CS._known_terms(_CSFakeSession(rows=[{"name": "TAXCALC"}, {"name": None}, {"name": "TAX-RATE"}]),
                           "w1") == ["TAXCALC", "TAX-RATE"]


# ---- _build_router ----

_CONFIRM_FIRST = "税率の一覧を Excel にまとめて。確認してから進めて。"


def test_build_router_explicit_lens_bypasses_heuristic_and_llm(monkeypatch):
    calls = []
    monkeypatch.setattr(CS.intent_llm, "classify", lambda m, s, **kw: calls.append(m) or None)
    d = CS._build_router([], "w1", {}, can_ask=True, explicit_lens="impact")("消費税の仕様は？")
    assert d["lens"] == "impact" and d["reason"] == "明示指定" and calls == []


@pytest.mark.parametrize("can_ask,expected", [(True, "clarify"), (False, "impact")])
def test_build_router_confirm_first_overrides_explicit_lens_only_when_can_ask(can_ask, expected):
    """「確認してから進めて」は明示指定より優先する（確認カードを出せないときは明示指定がそのまま適用）。"""
    assert CS._build_router([], "w1", {}, can_ask=can_ask, explicit_lens="impact")(_CONFIRM_FIRST)["lens"] == expected


def test_build_router_confirm_first_embeds_scope_meta_tools():
    """確認カードに `scope_meta["tools"]` を載せる（無いと再送時に全 ON へ復元される）。"""
    sm = {"world": "w1", "scope_paths": [], "source": "all", "layer": "both",
          "tools": {"grep": False, "fulltext": False, "graph": True}}
    d = CS._build_router([], "w1", {}, can_ask=True, scope_meta=sm)(_CONFIRM_FIRST)
    assert d["lens"] == "clarify" and d["question"]["tools"] == sm["tools"]
    d = CS._build_router([], "w1", {}, can_ask=True, scope_meta=None)(_CONFIRM_FIRST)
    assert d["question"]["tools"] is None


# ---- _no_genuine_results / _retry_hints / _finalize ----

def _env(sources, scope, data=None):
    """`data` 省略時は通常の検索結果 envelope を模す非空 dict。明示エラーは `data={}` を渡す。"""
    return {"sources": sources, "scope": scope, "data": data if data is not None else {"type": "qa"}}


def _scope(scope_paths=(), layer="both", applied=True, **extra):
    return {"scope_paths": list(scope_paths), "layer": layer, "layer_applied": applied, **extra}


def test_no_genuine_results():
    assert CS._no_genuine_results(_env([], {})) is True
    assert CS._no_genuine_results(_env(["doc1"], {})) is False
    assert CS._no_genuine_results(_env([], {"scope_paths": ["4期/"]}, data={})) is False   # 明示エラーは data={}


_SUB_TASK_DATA = {"evidence_packet": {"task_id": "sub:profile-1"}}
_SCOPE_HINT = {"kind": "scope", "label": "範囲を全体に広げる", "action": {"scope_paths": []}}


def _depth_hint(label, to="max"):
    return {"kind": "depth", "label": label, "action": {"depth_profile": to}}


_COVERAGE_LABEL = "網羅性を求める質問です。『標準』以上で調べ直すと抜けが減ります"


@pytest.mark.parametrize("scope,data,message,expected", [
    (_scope(["4期/設計"]), None, "", [_SCOPE_HINT]),
    (_scope(layer="docs"), None, "", [{"kind": "layer", "label": "コードも含めて探す（今は資料のみ）",
                                       "action": {"layer": "both"}}]),
    (_scope(layer="code"), None, "", "資料も含めて探す（今はコードのみ）"),
    (_scope(["4期/"], layer="docs", applied=False), None, "", [_SCOPE_HINT]),     # layer 非適用の層は案内しない
    (_scope(), None, "", []),                                                      # 最も緩い
    # 調べる深さ（下調べ役あり構成）
    (_scope(depth_profile="standard"), _SUB_TASK_DATA, "", [_depth_hint("調べる深さを上げて探す（今は標準）")]),
    (_scope(depth_profile="deep"), _SUB_TASK_DATA, "", [_depth_hint("調べる深さを上げて探す（今は深く）")]),
    (_scope(depth_profile="max"), _SUB_TASK_DATA, "", []),
    (_scope(depth_profile="quick"), _SUB_TASK_DATA, "区分ごとに起動方式を教えて",
     [_depth_hint(_COVERAGE_LABEL, "standard")]),                                   # クイック＋網羅要求は専用案内
    (_scope(depth_profile="quick"), _SUB_TASK_DATA, "これは何ですか",
     [_depth_hint("調べる深さを上げて探す（今はクイック）")]),
    (_scope(depth_profile="quick"), _SUB_TASK_DATA, "", [_depth_hint("調べる深さを上げて探す（今はクイック）")]),
    (_scope(depth_profile="standard"), _SUB_TASK_DATA, "各画面の入力項目を教えて",
     [_depth_hint("調べる深さを上げて探す（今は標準）")]),                          # 専用文言はクイックだけ
    (_scope(depth_profile="standard"), {"evidence_packet": {"task_id": "main"}}, "", []),   # 下調べ役なし
    (_scope(depth_profile="deep"), None, "", []),                                          # evidence_packet 無し
    # 検索経路トグル
    (_scope(tools={"grep": False, "fulltext": False, "graph": True}), None, "",
     [{"kind": "tools", "label": "OFF にした検索を戻す", "action": {"tools": _TOOLS_ALL}}]),
    (_scope(tools=_TOOLS_ALL), None, "", []),
    (_scope(), None, "", []),
], ids=["scope", "layer_docs", "layer_code", "layer_not_applied", "loosest", "depth_standard", "depth_deep",
        "depth_max", "quick_coverage", "quick_plain", "quick_no_message", "standard_coverage", "no_helper",
        "no_packet", "tools_off", "tools_all_on", "tools_missing"])
def test_retry_hints(scope, data, message, expected):
    env = _env([], scope, data=data)
    hints = CS._retry_hints(env, message) if message else CS._retry_hints(env)
    if isinstance(expected, str):
        assert hints[0]["label"] == expected
    else:
        assert hints == expected


@pytest.mark.parametrize("scope,data,kinds", [
    (_scope(["4期/"], layer="docs"), None, ["scope", "layer"]),
    (_scope(["4期/"], layer="docs", depth_profile="deep"), _SUB_TASK_DATA, ["scope", "layer", "depth"]),
    (_scope(["4期/"], layer="docs", depth_profile="deep", tools={"grep": False, "fulltext": True, "graph": True}),
     _SUB_TASK_DATA, ["scope", "layer", "depth", "tools"]),
])
def test_retry_hints_order(scope, data, kinds):
    assert [h["kind"] for h in CS._retry_hints(_env([], scope, data=data))] == kinds


@pytest.mark.parametrize("usage,multi,expected", [
    ({"provider": "codex", "is_local": "cloud"}, True, True),      # 既定 OpenAI＋サンドボックス有効
    ({"provider": "codex", "is_local": "local"}, False, False),    # Ollama
    ({"provider": "codex", "is_local": "cloud"}, False, False),    # Azure/独自（multi_agent 自動無効）
])
def test_depth_hint_follows_codex_multi_agent(usage, multi, expected):
    assert CS._depth_actually_helps({"usage": usage, "codex_multi_agent": multi}) is expected
    env = _env([], _scope(depth_profile="standard"))
    env["usage"], env["codex_multi_agent"] = usage, multi
    assert CS._retry_hints(env) == ([_depth_hint("調べる深さを上げて探す（今は標準）")] if expected else [])


def test_finalize_attaches_and_omits_retry_hints():
    out = CS._finalize(_env([], _scope(["4期/"])), {"lens": "qa", "reason": "既定（検索）"})
    assert out["retry_hints"] == [_SCOPE_HINT]
    assert "retry_hints" not in CS._finalize(_env(["doc1"], _scope()), {"lens": "qa", "reason": "既定（検索）"})


def test_finalize_passes_message_to_retry_hints_for_coverage_guidance():
    env = _env([], _scope(depth_profile="quick"), data=_SUB_TASK_DATA)
    out = CS._finalize(env, {"lens": "qa", "reason": "既定（検索）"}, "各画面の項目を教えて")
    assert out["retry_hints"] == [_depth_hint(_COVERAGE_LABEL, "standard")]


def test_finalize_no_retry_hints_for_explicit_error_envelope():
    env = _env([], _scope(["4期/"], layer="docs"), data={})
    env["headline"] = "下調べAIでの調査がうまくいきませんでした。設定を確認するか、下調べ機能をOFFにしてください。"
    out = CS._finalize(env, {"lens": "qa", "reason": "下調べ設定の不正"})
    assert "retry_hints" not in out and out["headline"] != CS._NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE


def test_finalize_replaces_headline_when_loosest_and_no_hints_qa():
    env = _env([], _scope())
    env["headline"] = "該当する記述は見つかりませんでした（確証なし）。検索語を変えて試してください。"
    out = CS._finalize(env, {"lens": "qa", "reason": "既定（検索）"})
    assert "retry_hints" not in out and out["headline"] == CS._NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE


@pytest.mark.parametrize("task_id,replaced", [("main", False), ("sub:worker", True)])
def test_finalize_budget_headline_kept_only_for_main_task_id(task_id, replaced):
    """予算到達の固定 headline は task_id=="main" のときだけ守る（ハイブリッド sub: は従来どおり置換）。"""
    env = _env([], _scope(), data={"evidence_packet": {"task_id": task_id, "stop_reason": "turns_exhausted"}})
    env["headline"] = "調査が上限に達したため、ここまでに確認できた内容のみをお伝えします。"
    out = CS._finalize(env, {"lens": "qa", "reason": "既定（検索）"})
    assert "retry_hints" not in out
    assert (out["headline"] == CS._NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE) is replaced


def test_finalize_keeps_partial_headline_when_codex_stopped_early_even_with_zero_sources():
    env = _env([], _scope(), data={"citations": []})
    env["headline"] = "続いて関連ファイルを確認します。"
    env["codex_stopped_early"] = True
    out = CS._finalize(env, {"lens": "qa", "reason": "既定（検索）"})
    assert out["headline"] == "続いて関連ファイルを確認します。"
    assert any(h["kind"] == "resume" for h in out["retry_hints"])


def test_finalize_does_not_replace_headline_for_impact_even_when_loosest():
    env = _env([], _scope(applied=False))
    env["headline"] = "「税率」の影響先は見つかりませんでした（表記ゆれ、または影響なし）。"
    out = CS._finalize(env, {"lens": "impact", "reason": "変更・影響の語"})
    assert "retry_hints" not in out
    assert out["headline"] == "「税率」の影響先は見つかりませんでした（表記ゆれ、または影響なし）。"


# `_finalize` が `env["stop_kind"]` を立てる配線（各値の導出は test_stop_kind.py が固定する）
_BUDGET_PACKET = {"evidence_packet": {"task_id": "main", "stop_reason": "turns_exhausted"}}


@pytest.mark.parametrize("sources,data,extra,expected", [
    (["doc1"], None, {}, "completed"),
    ([], _BUDGET_PACKET, {"headline": "調査が上限に達したため、ここまでに確認できた内容のみをお伝えします。"}, "budget"),
    ([], {"evidence_packet": {"task_id": "main", "stop_reason": "evaluation_blocked"}}, {}, "no_evidence"),
    ([], {"citations": []}, {"headline": "続いて関連ファイルを確認します。", "codex_stopped_early": True},
     "codex_partial"),
    ([], {}, {"headline": "Codex に接続できませんでした。", "codex_silent_failure": True}, "codex_silent"),
    ([], {}, {"headline": "同じ会話で別の依頼を実行中です。", "busy": True}, None),
    ([], {}, {"headline": "下調べAIでの調査がうまくいきませんでした。", "agentic_failure": "error"}, None),
])
def test_finalize_sets_stop_kind(sources, data, extra, expected):
    env = {**_env(sources, _scope(), data=data), **extra}
    out = CS._finalize(env, {"lens": "qa", "reason": "既定（検索）"})
    assert out.get("stop_kind") == expected
    if expected is None:
        assert "stop_kind" not in out


# ---- stream_message の lens 配線（DB 不要） ----

class _FakeCtxCaptureProvider:
    def __init__(self, captured):
        self._captured = captured

    def run(self, ctx):
        self._captured["ctx"] = ctx
        return iter([_fixed_result("mock 回答")])


@pytest.mark.parametrize("message,lens,source,route_lens,saved_content", [
    ("消費税率を変えたい", "impact", "explicit", "impact", "消費税率を変えたい"),
    ("/影響 消費税率を変えたい", "qa", "slash", "impact", "消費税率を変えたい"),     # スラッシュが勝つ
    ("消費税率を変えたい", None, "auto", None, None),
    ("夜間バッチが心配", "troubleshoot", "explicit", "troubleshoot", None),
])
def test_turn_lens_wires_into_ctx_route(monkeypatch, message, lens, source, route_lens, saved_content):
    saved = _mock_store_no_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeCtxCaptureProvider(captured))
    monkeypatch.setattr(CS, "_known_terms", lambda session, world: [])
    _turn(message, knowledge=True, lens=lens)
    ctx = captured["ctx"]
    assert ctx.scope_meta["lens_source"] == source
    if route_lens:
        d = ctx.route(message.removeprefix("/影響 "))
        assert d["lens"] == route_lens
        if source == "explicit":
            assert d["reason"] == "明示指定"
    if saved_content:
        assert _rows(saved, "user")[0]["content"] == saved_content   # 接頭辞は保存された質問からも除かれる


# ---- 打切り申告・秘匿名・清書プロンプトの事実 ----

def test_qa_headline_carries_truncation_note_on_zero_hits():
    note = "「大規模一覧.xlsx」は大きすぎて全体を検索できていません（先頭部分のみ）。"
    env = CS._answer_qa({"citations": [], "notes": [note]}, "w")
    assert "見つかりませんでした" in env["headline"] and "大きすぎて全体を検索できていません" in env["headline"]
    base = CS._answer_qa({"citations": []}, "w")["headline"]       # 打切りが無ければ headline は不変
    assert base == CS._answer_qa({"citations": [], "notes": []}, "w")["headline"] and "⚠" not in base
    assert note in CS._answer_troubleshoot({"candidates": [], "notes": [note]}, "w")["headline"]
    assert note in CS._answer_impact({"items": [], "start": "契約", "notes": [note]}, "w")["headline"]


def test_es_hits_excludes_sensitive_doc_ids(monkeypatch):
    from sherpa import documents as documents_mod
    from sherpa import es_index as es_index_mod
    monkeypatch.setattr(documents_mod, "world_rel_set", lambda world: {"a/credentials.xlsx", "a/report.xlsx"})
    monkeypatch.setattr(es_index_mod, "search", lambda world, query, scope_paths=None, k=8, vector=False, layer=None: (
        [{"doc_id": "a/credentials.xlsx", "text": "secret"}, {"doc_id": "a/report.xlsx", "text": "ok"}], None))
    assert [h["doc_id"] for h in CS._es_hits("w", "q", None)] == ["a/report.xlsx"]


# ---- S4（縮退の可視化と計数）: 事前検索（グラフ）が不調でも調査を止めない ----
# モックは外部境界（Neo4j セッション）だけ——`run_impact`/`run_troubleshoot` は差し替えない。

_DEGRADE_SCOPE_META = {"world": "v1", "scope_paths": [], "source": "all"}
_DEGRADE_TOOLS = {"grep": True, "fulltext": False, "graph": True}


class _BoomSession:
    def __init__(self, exc):
        self.exc = exc

    def run(self, query, **kw):
        raise self.exc


def test_dispatch_degrades_to_grep_on_recoverable_graph_failures():
    from neo4j.exceptions import ServiceUnavailable
    from sherpa.ingest.world_neo4j import GraphSchemaEraError
    env = CS._dispatch(_BoomSession(ServiceUnavailable("down")), "impact", "消費税率を変えたい", "v1",
                       scope_meta=_DEGRADE_SCOPE_META, tools_availability=_DEGRADE_TOOLS)
    assert env["graph_degraded"] == "graph_unavailable" and env["data"]["type"] == "qa"
    env = CS._dispatch(_BoomSession(GraphSchemaEraError("v1", "old-era", lens="troubleshoot")),
                       "troubleshoot", "請求の不具合", "v1",
                       scope_meta=_DEGRADE_SCOPE_META, tools_availability=_DEGRADE_TOOLS)
    assert env["graph_degraded"] == "graph_reingest_required"


@pytest.mark.parametrize("exc_name,lens,message", [
    ("ClientError", "impact", "消費税率を変えたい"),             # Cypher のバグ等は握り潰さない
    ("ConfigurationError", "troubleshoot", "請求の不具合"),     # 非一時的な設定不備＝回復不可
])
def test_dispatch_does_not_degrade_on_non_recoverable_graph_error(exc_name, lens, message):
    import neo4j.exceptions as ne
    exc = getattr(ne, exc_name)("bad")
    with pytest.raises(getattr(ne, exc_name)):
        CS._dispatch(_BoomSession(exc), lens, message, "v1", scope_meta=_DEGRADE_SCOPE_META,
                     tools_availability=_DEGRADE_TOOLS)


def test_finalize_turns_graph_degraded_into_notice_and_limit():
    env = {"headline": "該当箇所が 3件見つかりました。", "summary": {"total": 3},
           "data": {"citations": [{"doc_id": "a.md"}]}, "sources": [], "graph_degraded": "graph_reingest_required"}
    out = CS._finalize(env, {"lens": "troubleshoot", "reason": "テスト"})
    assert "graph_degraded" not in out                        # 閉じたコードは公開しない
    assert out["headline"].startswith("関係のつながりの情報が古いため")
    assert out["headline"].endswith("該当箇所が 3件見つかりました。")
    assert out["limits"]["graph_reingest_required"] is True

    env = {"headline": "本文", "summary": {"total": 0}, "data": {"citations": []}, "sources": [],
           "graph_degraded": "graph_unavailable"}
    out = CS._finalize(env, {"lens": "impact", "reason": "テスト"})
    assert "接続できなかった" in out["headline"] and out["limits"] == {"backend_unavailable_graph": True}


def test_finalize_graph_degraded_notice_survives_no_results_headline():
    """0 件案内は headline を全置換する——縮退の告知は案内の前に付く（0 件でも伝える）。"""
    env = {"headline": "該当する記述は見つかりませんでした。", "summary": {"total": 0},
           "data": {"citations": []}, "sources": [],
           "scope": {"world": "w1", "scope_paths": [], "source": "all", "layer": "both", "depth_profile": "max"},
           "graph_degraded": "graph_unavailable"}
    out = CS._finalize(env, {"lens": "qa", "reason": "テスト"})
    assert out["headline"].startswith("関係のつながりをたどる検索に接続できなかったため")
    assert out["headline"].endswith(CS._NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE)
    assert out["limits"]["backend_unavailable_graph"] is True


def test_dispatch_entry_degrade_counts_graph_unavailable_only_when_unreachable():
    """入口の縮退は実接続の不達だけを統計に残す。利用者が自分で OFF にしただけなら計数しない。"""
    sm_off = _sm(tools={"grep": True, "fulltext": True, "graph": False})
    env_off = CS._dispatch(None, "impact", "消費税率", "w1", sm_off,
                           tools_availability={"grep": True, "fulltext": True, "graph": True})
    assert env_off["graph_degraded"] == "blocked" and CS._GRAPH_DEGRADED_LIMIT_FIELD.get("blocked") is None
    env_both = CS._dispatch(None, "impact", "消費税率", "w1", sm_off,
                            tools_availability={"grep": True, "fulltext": True, "graph": False})
    assert env_both["graph_degraded"] == "blocked"
    assert "backend_unavailable_graph" not in (CS._finalize(env_both, {"lens": "impact", "reason": "テスト"}).get("limits") or {})

    env = CS._dispatch(None, "impact", "消費税率", "w1", _sm(),
                       tools_availability={"grep": True, "fulltext": True, "graph": False})
    assert env["graph_degraded"] == "graph_unavailable"
    assert CS._finalize(env, {"lens": "impact", "reason": "テスト"})["limits"]["backend_unavailable_graph"] is True


def test_dispatch_graph_failure_without_any_search_tool_stays_blocked():
    from neo4j.exceptions import ServiceUnavailable
    env = CS._dispatch(_BoomSession(ServiceUnavailable("down")), "impact", "消費税率を変えたい", "v1",
                       scope_meta=_DEGRADE_SCOPE_META,
                       tools_availability={"grep": False, "fulltext": False, "graph": True})
    assert env["data"] == {} and "graph_degraded" not in env and env["agentic_failure"] == "error"


# ---- 空グラフ（未構築）を「影響なし」と誤読させない ----

def _use_real_graph_empty_check(monkeypatch):
    from sherpa.ingest import world_neo4j
    monkeypatch.setattr(CS, "world_graph_is_empty", world_neo4j.world_graph_is_empty)


class _CountSession:
    """`:Entity{world_id}` の存在確認クエリに `count(n)` を返す fake セッション。"""

    def __init__(self, count: int):
        self.count = count
        self.queries: list = []

    def run(self, query, **kw):
        self.queries.append(query)
        c = self.count

        class _R:
            @staticmethod
            def data():
                return [{"c": c}]
        return _R()


def _empty_graph_runners(monkeypatch):
    monkeypatch.setattr(CS, "run_impact", lambda session, payload, world, scope_prefixes=None, depth=None:
                        {"items": [], "presumed": [], "start": payload, "starts": []})
    monkeypatch.setattr(CS, "run_troubleshoot", lambda session, symptom, world, scope_paths=None, depth=None:
                        {"type": "troubleshoot", "world": world, "symptom": symptom, "anchors": [], "candidates": []})
    _use_real_graph_empty_check(monkeypatch)


@pytest.mark.parametrize("lens,message", [("impact", "消費税率を変えたい"), ("troubleshoot", "夜間バッチ停止")])
def test_dispatch_degrades_when_world_graph_is_empty(monkeypatch, lens, message):
    _empty_graph_runners(monkeypatch)
    session = _CountSession(0)
    env = CS._dispatch(session, lens, message, "v1", scope_meta=_DEGRADE_SCOPE_META, tools_availability=_DEGRADE_TOOLS)
    assert any("Entity" in q for q in session.queries)
    assert env["graph_degraded"] == "graph_empty" and env["data"]["type"] == "qa"


def test_dispatch_impact_stays_no_results_when_graph_has_data(monkeypatch):
    _empty_graph_runners(monkeypatch)
    env = CS._dispatch(_CountSession(1), "impact", "消費税率を変えたい", "v1",
                       scope_meta=_DEGRADE_SCOPE_META, tools_availability=_DEGRADE_TOOLS)
    assert "graph_degraded" not in env and "影響先は見つかりませんでした" in env["headline"]


def test_finalize_graph_empty_notice_has_no_limit_field():
    env = {"headline": "該当箇所が 3件見つかりました。", "summary": {"total": 3},
           "data": {"citations": [{"doc_id": "a.md"}]}, "sources": [], "graph_degraded": "graph_empty"}
    out = CS._finalize(env, {"lens": "troubleshoot", "reason": "テスト"})
    assert "graph_degraded" not in out
    assert out["headline"].startswith("関係のつながりの情報がまだ作られていない")
    assert out["headline"].endswith("該当箇所が 3件見つかりました。")
    assert "limits" not in out or not out["limits"]


# ---- グラフ接続断でターン全体が 500 にならず縮退へ進む ----

class _DispatchingProvider:
    def run(self, ctx):
        env = ctx.dispatch("impact", ctx.message)
        env.setdefault("headline", "回答")
        yield {"type": "_result", "env": env, "decision": {"lens": "impact", "input": ctx.message, "reason": "テスト"}}


def test_turn_degrades_instead_of_crashing_when_graph_connection_is_down(monkeypatch):
    from neo4j.exceptions import ServiceUnavailable
    saved = _mock_store_no_db(monkeypatch)
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _DispatchingProvider())
    _turn("消費税率の影響は？", session=_BoomSession(ServiceUnavailable("down")), knowledge=True)
    row = saved[-1]
    assert row["role"] == "assistant"
    assert row["answer"]["headline"].startswith("関係のつながりをたどる検索に接続できなかったため")
    assert row["answer"]["limits"]["backend_unavailable_graph"] is True


@pytest.mark.parametrize("message,shown", [("処理ごとの抽出条件を教えて", True), ("標準税率は何%ですか", False)])
def test_coverage_hint_for_quick_even_when_results_exist(message, shown):
    """網羅性を求める質問をクイックで実行したときは、結果が有っても『標準以上で調べ直す』案内を出す
    （列挙の抜けは結果が有るときにこそ起きる）。網羅要求のない質問では深さの案内を出さない。"""
    env = {"headline": "回答", "sources": [{"doc_id": "a.md"}], "data": {"type": "qa"},
           "usage": {"provider": "codex"}, "codex_multi_agent": True,
           "scope": {"scope_paths": [], "depth_profile": "quick", "layer": "both"}}
    CS._finalize(env, {"lens": "qa", "reason": "test"}, message=message)
    hints = env.get("retry_hints") or []
    if shown:
        assert [h for h in hints if h["kind"] == "depth" and "網羅性" in h["label"]]
        assert hints[0]["action"] == {"depth_profile": "standard"}
    else:
        assert not hints
