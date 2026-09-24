"""`sherpa/store/turn_metrics.py`（`docs/proposals/2026-09-23-利用統計の刷新.md` §3.1/§4 が正典）
の単体テスト。

前半は `metrics_from_answer`（DB 非依存の純粋写像）、後半は `store.add_message`/`backfill_all`/
`ensure_rows`/`scripts/usage-backfill.sh` の round-trip（`_try_init()` で DB 不達なら skip・
`tests/unit/test_audit_store_roundtrip.py` と同じ型）。

`answer["activity"]` を書く側はまだ無い契約（§3.1）——ここでは同じ形の辞書を手で作って
テスト用の入力にする。
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

from sherpa import store
from sherpa.store import turn_metrics


def _sfx() -> str:
    return str(int(time.time() * 1_000_000))[-10:]


def _try_init() -> None:
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _activity(*, parent_tokens=None, child_tokens_list=None, app_version="v-test+abc123",
             phases=None) -> dict:
    """§3.1 の形の activity 辞書をテスト用に組み立てる（本文・資料名・ツール引数は含まない＝
    契約どおりの形。この辞書がそのまま `activity_json` に入る）。"""
    parent_tokens = parent_tokens or {"input_tokens": 100, "cached_input_tokens": 10,
                                      "output_tokens": 20, "reasoning_output_tokens": 5}
    agents = [{
        "role": "parent", "model": "gpt-test",
        "tokens": parent_tokens,
        "rounds": [[100, 10, 20, 5]],
        "compactions": [],
        "tools": {"ripgrep_search": {"calls": 3, "bytes": 1234, "max_bytes": 500,
                                     "clipped": 1, "truncated": 0, "errors": 0, "ms": 42}},
        "unparsed": {},
    }]
    for ct in (child_tokens_list or []):
        agents.append({
            "role": "child", "model": "gpt-test-mini",
            "tokens": ct, "rounds": [[10, 1, 2, 0]], "compactions": [],
            "tools": {"read_doc": {"calls": 2, "bytes": 500, "max_bytes": 300,
                                   "clipped": 0, "truncated": 1, "errors": 0, "ms": 7}},
            "unparsed": {},
        })
    return {
        "v": 1, "source": "codex_rollout", "app_version": app_version,
        "settings": {"provider": "codex", "model": "gpt-test", "reasoning": "medium", "depth": "standard"},
        "phases_ms": phases or {"prepare": 11, "agent": 222, "post": 33, "total": 266},
        "agents": agents,
    }


# ===================== metrics_from_answer（純粋関数） =====================

def test_activity_priority_over_legacy_including_failed_turn():
    """activity（§3.1）が有効ならそこから求まる値を使う——失敗ターン（usage が 0 のまま保存）
    でも activity の実消費を拾う。壊れた activity（版不一致・agents 非配列）は全面的に
    旧フィールドへ倒れる（部分的な混在をしない）。"""
    with pytest.raises(TypeError):
        turn_metrics.metrics_from_answer(None)

    answer = {
        "usage": {"provider": "codex", "model": "gpt-test", "input_tokens": 0,
                  "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0},
        "activity": _activity(
            parent_tokens={"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20,
                          "reasoning_output_tokens": 5},
            child_tokens_list=[{"input_tokens": 15, "cached_input_tokens": 1, "output_tokens": 3,
                                "reasoning_output_tokens": 0}],
        ),
    }
    answer["activity"]["agents"][0]["compactions"] = [3]
    m = turn_metrics.metrics_from_answer(answer)
    assert (m["input_tokens"], m["output_tokens"]) == (115, 23)     # usage=0 でも activity の実消費
    assert (m["parent_input_tokens"], m["parent_output_tokens"]) == (100, 20)
    assert (m["child_input_tokens"], m["child_output_tokens"]) == (15, 3)
    assert m["children_detected"] == 1 and m["children_usage_found"] == 1
    assert m["children_usage_missing"] is None   # activity だけでは欠落数は分からない
    assert m["tool_calls_total"] == 5 and m["tool_result_bytes_total"] == 1734   # parent+child 合算
    assert m["api_rounds_total"] == 2 and m["compactions_total"] == 1
    assert m["app_version"] == "v-test+abc123"
    assert (m["phase_prepare_ms"], m["phase_agent_ms"], m["phase_post_ms"]) == (11, 222, 33)
    assert m["activity_json"] == answer["activity"]
    assert m["mapping_source"] == "activity"

    for bad_activity in ({"v": 2, "agents": []}, {"v": 1, "agents": "not-a-list"}):
        m2 = turn_metrics.metrics_from_answer({"usage": answer["usage"], "activity": bad_activity})
        assert m2["mapping_source"] == "answer_only" and m2["activity_json"] is None


def test_legacy_fallback_and_null_vs_zero_rules():
    """activity が無い旧回答は既存フィールドへ全面フォールバックする。「区画自体が丸ごと
    無い」は NULL、「区画はあるが個々の項目が無い」は 0/false——0 と不明を区別する。"""
    usage_only = {"usage": {"provider": "openai", "model": "gpt-5.5", "depth_profile": "standard",
                            "reasoning": "medium", "input_tokens": 100, "cached_input_tokens": 10,
                            "output_tokens": 20, "reasoning_output_tokens": 5}}
    m = turn_metrics.metrics_from_answer(usage_only)
    assert m["provider"] == "openai" and m["depth_profile"] == "standard" and m["reasoning"] == "medium"
    assert (m["input_tokens"], m["parent_input_tokens"]) == (100, 100)   # breakdown が無ければ usage=parent
    assert m["child_input_tokens"] is None and m["children_detected"] is None
    assert m["app_version"] is None and m["activity_json"] is None
    assert m["tool_calls_total"] is None and m["api_rounds_total"] is None

    breakdown_answer = {
        "usage": {"provider": "codex", "model": "m", "input_tokens": 130, "cached_input_tokens": 12,
                  "output_tokens": 24, "reasoning_output_tokens": 6,
                  "codex_usage_breakdown": {
                      "parent": {"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20,
                                "reasoning_output_tokens": 5},
                      "children": {"input_tokens": 30, "cached_input_tokens": 2, "output_tokens": 4,
                                  "reasoning_output_tokens": 1}}},
        "codex_usage_children": {"found": 2, "missing": 1, "input_tokens": 30, "cached_input_tokens": 2,
                                 "output_tokens": 4, "reasoning_output_tokens": 1},
    }
    m2 = turn_metrics.metrics_from_answer(breakdown_answer)
    assert (m2["parent_input_tokens"], m2["child_input_tokens"]) == (100, 30)
    assert (m2["children_detected"], m2["children_usage_found"], m2["children_usage_missing"]) == (3, 2, 1)

    m3 = turn_metrics.metrics_from_answer({"limits": {"tool_result_clipped": 3, "total_budget_hit": True}})
    assert m3["tool_result_clipped"] == 3 and m3["context_compactions"] == 0
    assert m3["total_budget_hit"] is True and m3["synthesis_truncated"] is False

    m4 = turn_metrics.metrics_from_answer({})
    for f in ("tool_result_clipped", "context_compactions", "search_truncated",
             "auto_continues", "duplicate_tool_call",
             "total_budget_hit", "synthesis_truncated", "depth_escalated",
             "backend_unavailable_fulltext", "backend_unavailable_graph",
             "graph_reingest_required", "tool_calls_exhausted"):
        assert m4[f] is None, f   # limits 区画自体が無い→全項目 NULL
    assert m4["input_tokens"] is None and m4["parent_input_tokens"] is None and m4["child_input_tokens"] is None
    assert m4["mapping_source"] == "answer_only" and m4["activity_json"] is None


def test_reads_match_usage_py_definitions():
    """出典件数・主張の区分/理由コード・最終ゲートの不足コード・調査台帳は store/usage.py の
    既存の集計定義と同じ規則で読む（数え方がずれると新旧の集計値が食い違う）。"""
    assert turn_metrics.metrics_from_answer({"sources": []})["sources_count"] == 0
    assert turn_metrics.metrics_from_answer(
        {"sources": [{"doc_id": "a"}, {"doc_id": "b"}]})["sources_count"] == 2
    assert turn_metrics.metrics_from_answer({"sources": "not-a-list"})["sources_count"] == 0

    data = {"claims": [
        {"status": "confirmed"}, {"status": "confirmed"}, {"status": "unknown"},
        {"status": "unknown", "reason_code": "ambiguous"},
        {"status": "unknown", "reason_code": 123},          # 文字列でない reason_code は飛ばす
        {"status": "not-a-real-status"}, "garbage",
    ], "evidence_gate": {"missing_codes": ["source", "", 123, "source", "spec_doc"]}}
    m = turn_metrics.metrics_from_answer({"data": data})
    assert m["claims_confirmed"] == 2 and m["claims_inferred"] == 0 and m["claims_unknown"] == 3
    # reason_code 欠落分は 'unknown' へ・数値の reason_code は飛ばす（usage.py と同じ規則）。
    assert m["claims_unknown_reasons"] == {"unknown": 1, "ambiguous": 1}
    assert m["gate_missing_codes"] == {"source": 2, "spec_doc": 1}   # 空文字・非文字列は数えない

    m_missing = turn_metrics.metrics_from_answer({})
    assert (m_missing["claims_confirmed"], m_missing["claims_unknown_reasons"],
           m_missing["gate_missing_codes"]) == (None, None, None)

    inv = turn_metrics.metrics_from_answer({"investigation": {
        "complete": True, "continuations": 2, "counts": {"confirmed": 3, "unknown": 1},
        "non_terminal": ["truncated-list-not-used-for-counting"]}})
    assert inv["investigation_complete"] is True and inv["investigation_continuations"] == 2
    assert inv["investigation_counts"] == {"confirmed": 3, "unknown": 1}
    assert "investigation_item_count" not in inv   # 非終端等は50件で切詰め＝全体件数は確定できない
    assert turn_metrics.metrics_from_answer({})["investigation_complete"] is None


def test_no_body_title_docname_or_args_leak_into_mapping():
    """本文・タイトル・資料名は列にならない（activity_json は §3.1 契約でそもそも
    これらを含まない前提を信頼するだけで、他の answer フィールドからの漏れは無い）。"""
    marker = f"SECRET-BODY-{_sfx()}"
    answer = {
        "headline": marker, "title": marker,
        "usage": {"provider": "codex", "model": "m", "input_tokens": 1, "cached_input_tokens": 0,
                  "output_tokens": 1, "reasoning_output_tokens": 0},
        "sources": [{"doc_id": marker, "title": marker}],
        "activity": _activity(),
    }
    m = turn_metrics.metrics_from_answer(answer)
    dumped = json.dumps(m, default=str, ensure_ascii=False)
    assert marker not in dumped


# ===================== DB round-trip（add_message フック・backfill・ensure_rows・shell wrapper） =====================

def _fetch_turn_metrics(message_id):
    with store._connect() as c:
        return c.execute("SELECT * FROM turn_metrics WHERE message_id=%s", (message_id,)).fetchone()


def _fetch_tool_stats(message_id):
    with store._connect() as c:
        return c.execute(
            "SELECT * FROM turn_tool_stats WHERE message_id=%s ORDER BY agent_index, tool",
            (message_id,)).fetchall()


def _new_conversation():
    uid = f"tm-{_sfx()}"
    store.upsert_user(uid, uid, "turn-metrics test")
    cid = store.create_conversation(uid, "v1", "t")["id"]
    return uid, cid


def test_add_message_writes_both_tables_and_conversation_delete_cascades():
    _try_init()
    uid, cid = _new_conversation()
    answer = {"lens": "qa", "usage": {"provider": "codex", "model": "gpt-test",
                                      "depth_profile": "standard", "input_tokens": 1,
                                      "cached_input_tokens": 0, "output_tokens": 1,
                                      "reasoning_output_tokens": 0},
             "sources": [{"doc_id": "a"}], "stop_kind": "completed",
             "activity": _activity()}
    # Codex 組み込みツール（function_call 等）は max_bytes/clipped/truncated/errors/ms を測らず
    # activity に置かない契約（§3.1）——calls/bytes だけの形を混ぜて NULL になることを確認する。
    answer["activity"]["agents"][0]["tools"]["function_call"] = {"calls": 5, "bytes": 100}
    # web_search は結果の大きさも取れず calls だけのエントリになる（bytes キー自体が無い）。
    answer["activity"]["agents"][0]["tools"]["web_search"] = {"calls": 2}
    msg = store.add_message(cid, "assistant", "headline text", lens="qa", answer=answer, personal=False)

    row = _fetch_turn_metrics(msg["id"])
    assert row is not None
    assert row["conversation_id"] == cid and row["user_id"] == uid and row["world"] == "v1"
    assert row["lens"] == "qa" and row["personal"] is False
    assert row["provider"] == "codex" and row["depth_profile"] == "standard"
    assert row["mapping_source"] == "activity" and row["activity_json"] is not None
    assert row["sources_count"] == 1
    assert row["user_message_id"] is None   # この会話には先行する user 発言が無い
    forbidden = {"content", "headline", "title", "body", "query", "args", "doc_name", "sources"}
    assert forbidden.isdisjoint(row.keys())   # 本文・タイトル・資料名・引数の列自体が無い

    tools = _fetch_tool_stats(msg["id"])   # ORDER BY agent_index, tool → function_call, ripgrep_search, web_search
    assert len(tools) == 3
    assert tools[0]["tool"] == "function_call"
    assert tools[0]["calls"] == 5 and tools[0]["bytes_total"] == 100
    assert (tools[0]["max_bytes"], tools[0]["clipped"], tools[0]["truncated"],
           tools[0]["errors"], tools[0]["ms"]) == (None, None, None, None, None)   # キーが無ければ NULL
    assert tools[1]["tool"] == "ripgrep_search" and tools[1]["role"] == "parent"
    assert tools[1]["calls"] == 3 and tools[1]["bytes_total"] == 1234
    assert (tools[1]["clipped"], tools[1]["truncated"], tools[1]["errors"], tools[1]["ms"]) == (1, 0, 0, 42)   # MCP は値が入る
    assert tools[2]["tool"] == "web_search"
    assert tools[2]["calls"] == 2 and tools[2]["bytes_total"] is None   # calls だけのツールは bytes_total も NULL

    # turn_metrics 側の合計（tool_calls_total/tool_result_bytes_total）は bytes の無いツールを
    # 0 として合算するだけで、合計値自体は影響を受けない（100 + 1234・web_search の分は乗らない）。
    assert row["tool_calls_total"] == 5 + 3 + 2
    assert row["tool_result_bytes_total"] == 100 + 1234

    # role='user' や非 dict answer では行ができない（add_message のガード条件）。
    user_msg = store.add_message(cid, "user", "hello")
    assert _fetch_turn_metrics(user_msg["id"]) is None
    stray = store.add_message(cid, "assistant", "x", answer=None)
    assert _fetch_turn_metrics(stray["id"]) is None

    assert store.delete_conversation(cid, user_id=uid) is True
    assert _fetch_turn_metrics(msg["id"]) is None
    assert _fetch_tool_stats(msg["id"]) == []


def test_turn_metrics_write_failure_does_not_fail_message_save(monkeypatch):
    """行の書込に失敗しても回答の保存は失敗させない（turn_metrics.upsert を壊して確認）。"""
    _try_init()
    uid, cid = _new_conversation()

    def _boom(*a, **kw):
        raise RuntimeError("induced failure for test")

    monkeypatch.setattr(store.turn_metrics, "upsert", _boom)
    msg = store.add_message(cid, "assistant", "x",
                            answer={"usage": {"provider": "codex", "model": "m", "input_tokens": 1,
                                             "cached_input_tokens": 0, "output_tokens": 1,
                                             "reasoning_output_tokens": 0}})
    conv = store.get_conversation(cid)
    assert any(m["id"] == msg["id"] for m in conv["messages"])   # 本体は保存されている
    assert _fetch_turn_metrics(msg["id"]) is None   # savepoint で巻き戻り、行だけが無い


def test_user_message_id_points_to_most_recent_preceding_user_message():
    """1ターンに assistant が複数あっても全員が同じ（直近の）user 発言の id を指す。
    先行する user 発言が無い assistant は NULL。"""
    _try_init()
    _uid, cid = _new_conversation()
    ans = {"usage": {"provider": "openai", "model": "m", "input_tokens": 1, "cached_input_tokens": 0,
                     "output_tokens": 1, "reasoning_output_tokens": 0}}

    a0 = store.add_message(cid, "assistant", "x", answer=dict(ans))   # 先行 user 無し
    u1 = store.add_message(cid, "user", "q1")
    a1 = store.add_message(cid, "assistant", "x", answer=dict(ans))
    u2 = store.add_message(cid, "user", "q2")
    a2 = store.add_message(cid, "assistant", "x", answer=dict(ans))
    a3 = store.add_message(cid, "assistant", "x", answer=dict(ans))   # 同一ターンにもう1件

    assert _fetch_turn_metrics(a0["id"])["user_message_id"] is None
    assert _fetch_turn_metrics(a1["id"])["user_message_id"] == u1["id"]
    assert _fetch_turn_metrics(a2["id"])["user_message_id"] == u2["id"]
    assert _fetch_turn_metrics(a3["id"])["user_message_id"] == u2["id"]


def test_backfill_is_idempotent_and_ensure_rows_fills_only_missing_in_range():
    _try_init()
    uid, cid = _new_conversation()
    answer = {"lens": "impact", "usage": {"provider": "openai", "model": "m", "input_tokens": 7,
                                          "cached_input_tokens": 1, "output_tokens": 2,
                                          "reasoning_output_tokens": 0},
             "sources": []}
    msg = store.add_message(cid, "assistant", "x", lens="impact", answer=answer)
    with store._connect() as c:
        c.execute("DELETE FROM turn_metrics WHERE message_id=%s", (msg["id"],))
    assert _fetch_turn_metrics(msg["id"]) is None   # 旧データ相当（行が無い状態）を模擬

    turn_metrics.backfill_all()
    row1 = dict(_fetch_turn_metrics(msg["id"]))
    assert row1 is not None and row1["input_tokens"] == 7 and row1["sources_count"] == 0

    turn_metrics.backfill_all()
    row2 = dict(_fetch_turn_metrics(msg["id"]))
    assert row1 == row2   # 2回実行しても行数・値が同じ（冪等）

    msg2 = store.add_message(cid, "assistant", "x", answer=answer)
    with store._connect() as c:
        c.execute("DELETE FROM turn_metrics WHERE message_id=%s", (msg2["id"],))
    assert _fetch_turn_metrics(msg2["id"]) is None

    now = datetime.now(timezone.utc)
    turn_metrics.ensure_rows(datetime(2000, 1, 1, tzinfo=timezone.utc),
                             datetime(2000, 1, 2, tzinfo=timezone.utc))
    assert _fetch_turn_metrics(msg2["id"]) is None   # 範囲外では埋まらない

    turn_metrics.ensure_rows(now - timedelta(minutes=5), now + timedelta(minutes=5))
    assert _fetch_turn_metrics(msg2["id"]) is not None   # 範囲内では埋まる

    # 境界: 質問が期間の終わりの直前・回答は期間の終わりの直後に保存された（日付を跨ぐターン）。
    # usage.py は質問（user）の created_at で期間に入れるため、assistant.created_at だけで
    # 判定すると取りこぼす——先行 user の created_at で拾えることを確認する。
    u3 = store.add_message(cid, "user", "q3")
    msg3 = store.add_message(cid, "assistant", "x", answer=answer)
    boundary = now + timedelta(hours=1)
    with store._connect() as c:
        c.execute("UPDATE messages SET created_at=%s WHERE id=%s",
                  (boundary - timedelta(minutes=1), u3["id"]))          # 期間の終わりの1分前
        c.execute("UPDATE messages SET created_at=%s WHERE id=%s",
                  (boundary + timedelta(minutes=1), msg3["id"]))         # 期間の終わりの1分後
        c.execute("DELETE FROM turn_metrics WHERE message_id=%s", (msg3["id"],))
    assert _fetch_turn_metrics(msg3["id"]) is None

    turn_metrics.ensure_rows(boundary - timedelta(hours=1), boundary)
    row3 = _fetch_turn_metrics(msg3["id"])
    assert row3 is not None and row3["user_message_id"] == u3["id"]


def test_usage_backfill_wrapper_reads_env_file(tmp_path):
    """scripts/usage-backfill.sh が SHERPA_ENV_FILE の接続設定を読むこと。DB の既定値には
    依存しない最小のシェルレベル確認（偽の PGHOST を .env 経由で読ませ、その値が接続エラーに
    出ることだけを見る・実際の backfill 成否は問わない）。"""
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    marker_host = "usage-backfill-nonexistent-host-marker.invalid"
    fake_env = tmp_path / "fake.env"
    fake_env.write_text(f"PGHOST={marker_host}\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("SHERPA_PG_DSN", "DATABASE_URL", "PGHOST", "PGPORT", "PGUSER",
                        "PGPASSWORD", "POSTGRES_PASSWORD", "SHERPA_ENV_FILE")}
    env["SHERPA_ENV_FILE"] = str(fake_env)
    proc = subprocess.run(
        [str(repo_root / "scripts" / "usage-backfill.sh")],
        cwd=repo_root, env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode != 0
    assert marker_host in (proc.stdout + proc.stderr)
