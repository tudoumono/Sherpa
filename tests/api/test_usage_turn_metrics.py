"""利用統計（`usage_stats`）の turn_metrics 読み替えの契約テスト。

- 旧集計（`messages.answer` JSON から `_USAGE_TURN_CTE` で組む方式）と新集計（`turn_metrics` を
  1 回組み立てて読む方式）の結果一致（トークン合計だけは意図した差＝失敗ターンの実消費を数える）
- 回答 JSON を読まないこと（`messages.answer` を壊しても集計が変わらない）
- 合成 5,000 ターン・90 日の表示時間
- 起動時の欠落行補完（埋まる・再実行は何もしない・失敗しても起動を止めない）

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from _common import _sfx, _try_init
from sherpa import api, store
from sherpa.store import usage as U
from sherpa.store import turn_metrics as TM

_JST = timezone(timedelta(hours=9))
# 他のテストが書かない過去の窓に仕込む＝全体集計でも自分のデータだけを見られる。
_BASE = datetime(2001, 3, 5, 3, 0, tzinfo=_JST)
_FROM, _TO = "2001-03-01T00:00:00+09:00", "2001-03-20T00:00:00+09:00"


def _answer(*, provider="codex", model="m1", usage_tokens=(100, 10, 20, 5), depth="standard",
            sources=1, stop_kind="completed", limits=None, claims=None, gate=None, duration_ms=1000,
            activity_tokens=None, with_usage=True):
    a: dict = {"stop_kind": stop_kind, "duration_ms": duration_ms, "sources": [{"doc_id": f"d{i}"} for i in range(sources)]}
    if with_usage:
        i, ci, o, r = usage_tokens
        a["usage"] = {"provider": provider, "model": model, "depth_profile": depth,
                      "input_tokens": i, "cached_input_tokens": ci, "output_tokens": o,
                      "reasoning_output_tokens": r}
    if limits is not None:
        a["limits"] = limits
    data: dict = {}
    if claims is not None:
        data["claims"] = claims
    if gate is not None:
        data["evidence_gate"] = {"missing_codes": gate}
    if data:
        a["data"] = data
    if activity_tokens is not None:
        i, ci, o, r = activity_tokens
        a["activity"] = {"v": 1, "agents": [{"role": "parent", "model": model, "rounds": [], "tools": {},
                                              "tokens": {"input_tokens": i, "cached_input_tokens": ci,
                                                         "output_tokens": o, "reasoning_output_tokens": r}}]}
    return a


class _Seed:
    """窓の中へ会話を仕込む。作成順に 1 分刻みで created_at を置き直す（会話ごとに日をずらす）。"""

    def __init__(self):
        self.sfx = _sfx()
        self.ids: list[tuple[int, datetime]] = []   # (message id, 置く時刻)
        self.users = (f"tmA{self.sfx}", f"tmB{self.sfx}")

    def conv(self, uid, day):
        c = store.create_conversation(user_id=uid, world=f"tmworld{self.sfx}")
        self._cid, self._t = c["id"], _BASE + timedelta(days=day)
        return c["id"]

    def msg(self, role, **kw):
        m = store.add_message(self._cid, role, "x", **kw)
        self._t += timedelta(minutes=1)
        self.ids.append((m["id"], self._t))
        return m["id"]

    def place(self):
        with psycopg.connect(store._dsn()) as c:
            for mid, t in self.ids:
                c.execute("UPDATE messages SET created_at=%s WHERE id=%s", (t, mid))

    def time_of(self, mid):
        return dict(self.ids)[mid]


def _seed_dataset() -> _Seed:
    s = _Seed()
    ua, ub = s.users
    s.conv(ua, 0)
    s.msg("user")                                                     # 通常（根拠あり・主張に不明理由）
    s.msg("assistant", lens="qa", answer=_answer(
        limits={"tool_result_clipped": 2, "total_budget_hit": True},
        claims=[{"status": "unknown", "reason_code": "not_found"}, {"status": "unknown", "reason_code": "not_found"},
                {"status": "confirmed"}], gate=["source", "source"], duration_ms=1500))
    s.msg("user", personal=True)                                      # 個人利用・根拠なし
    s.msg("assistant", lens="impact", answer=_answer(provider="openai", model="m2", usage_tokens=(7, 0, 3, 0),
                                                     sources=0, duration_ms=700), personal=False)
    s.msg("user")                                                     # 失敗ターン（usage は 0・activity に実消費）
    s.msg("assistant", lens="qa", answer=_answer(usage_tokens=(0, 0, 0, 0), stop_kind="timeout",
                                                 activity_tokens=(500, 100, 50, 7)))
    s.msg("user")                                                     # 利用者停止
    s.msg("assistant", lens="qa", answer=_answer(with_usage=False, stop_kind="stopped_by_user", sources=0))
    s.msg("user")                                                     # 確認カード
    s.msg("assistant", lens="clarify", answer={})
    s.msg("user")                                                     # 返答なし（停止で未保存）＋巡だけ残る
    orphan_user = s.ids[-1][0]
    s.msg("user")                                                     # 次のターン（巡が混ざってはいけない）
    s.msg("assistant", lens="qa", answer=_answer(depth="deep"))
    s.conv(ub, 1)
    s.msg("user")
    s.msg("assistant", lens="chat", answer=_answer(provider="openai", model="m2", usage_tokens=(40, 0, 4, 0),
                                                   sources=0))
    two_round_user = s.msg("user")
    s.msg("assistant", lens="troubleshoot", answer=_answer(
        provider="openai", model="m2", usage_tokens=(5, 0, 1, 0), duration_ms=300, depth="deep",
        claims=[{"status": "unknown", "reason_code": "conflict"}, {"status": "unknown"}], gate=[]))
    s.msg("assistant", lens="qa", answer=_answer(usage_tokens=(9, 9, 9, 9)))          # 同じターンの 2 件目
    s.place()
    s.orphan_user, s.two_round_user = orphan_user, two_round_user
    return s


# ---- 旧集計（answer JSON から `_USAGE_TURN_CTE` で組む方式）の凍結コピー ----

def _tok(f):
    return (f"CASE WHEN (answer->'usage'->>'{f}') ~ '^[0-9]+$' THEN (answer->'usage'->>'{f}')::bigint ELSE 0 END")


def _lim_int(f):
    return (f"CASE WHEN (answer->'limits'->>'{f}') ~ '^[0-9]+$' THEN (answer->'limits'->>'{f}')::bigint ELSE 0 END")


def _old_limit_cols():
    cols = []
    for f in U._USAGE_LIMIT_INT_FIELDS:
        cols += [f"COUNT(*) FILTER (WHERE {_lim_int(f)} > 0) AS {f}_turns",
                 f"COALESCE(SUM({_lim_int(f)}), 0) AS {f}_total"]
    for f in U._USAGE_LIMIT_BOOL_FIELDS:
        cols.append(f"COUNT(*) FILTER (WHERE (answer->'limits'->>'{f}') = 'true') AS {f}_turns")
    return ", ".join(cols)


_OLD_ROUND_SQL = (
    "WITH rounds AS ("
    "  SELECT e.id, e.ts, e.provider, e.conversation_id FROM usage_events e WHERE e.kind='chat-round' AND e.ts >= %s"
    "), touched_convs AS (SELECT DISTINCT conversation_id FROM rounds"
    "), user_msgs AS ("
    "  SELECT m.conversation_id, m.created_at, LEAD(m.created_at) OVER (PARTITION BY m.conversation_id ORDER BY m.created_at) "
    "    AS next_user_created_at FROM messages m JOIN touched_convs t ON t.conversation_id = m.conversation_id WHERE m.role='user'"
    "), owning AS ("
    "  SELECT r.id AS round_id, r.ts, r.provider, r.conversation_id, COALESCE(u.created_at, r.ts) AS turn_created_at, "
    "    u.next_user_created_at FROM rounds r LEFT JOIN LATERAL ("
    "    SELECT um.created_at, um.next_user_created_at FROM user_msgs um "
    "    WHERE um.conversation_id = r.conversation_id AND um.created_at <= r.ts ORDER BY um.created_at DESC LIMIT 1) u ON true"
    ") SELECT o.ts, o.provider, ta.id AS turn_message_id, ta.answer->'usage'->>'depth_profile' AS depth_profile "
    "FROM owning o JOIN conversations c ON c.id = o.conversation_id LEFT JOIN LATERAL ("
    "  SELECT m.id, m.answer FROM messages m WHERE m.conversation_id = o.conversation_id AND m.role='assistant' "
    "    AND m.created_at >= o.ts AND (o.next_user_created_at IS NULL OR m.created_at < o.next_user_created_at) "
    "  ORDER BY m.created_at ASC LIMIT 1) ta ON true "
    "WHERE c.deleted_at IS NULL AND c.origin='own' AND o.turn_created_at >= %s AND o.turn_created_at < %s")


def _old(c, tail, s, e):
    return c.execute(U._USAGE_TURN_CTE + " " + tail, (s, e, s, e)).fetchall()


def _norm(rows):
    return sorted(tuple(sorted((k, (float(v) if hasattr(v, "as_tuple") else v)) for k, v in r.items())) for r in rows)


def test_new_aggregation_matches_old_except_failed_turn_tokens():
    if not _try_init():
        pytest.skip("DB down")
    s = _seed_dataset()
    start, end, _ = U._usage_period(time_from=_FROM, time_to=_TO)
    new = store.usage_stats(time_from=_FROM, time_to=_TO)
    mine = set(s.users)
    with store._connect() as c:
        # 利用者別: ターン・lens・personal（user 発言側）・根拠なし
        old_users = _old(c, "SELECT user_id AS uid, COUNT(*) AS turns, COUNT(DISTINCT conversation_id) AS conversations, "
                            "COUNT(*) FILTER (WHERE lens='impact') AS i, COUNT(*) FILTER (WHERE lens='qa') AS q, "
                            "COUNT(*) FILTER (WHERE lens='troubleshoot') AS t, COUNT(*) FILTER (WHERE lens='chat') AS ch, "
                            "COUNT(*) FILTER (WHERE user_personal) AS p, "
                            "COUNT(*) FILTER (WHERE lens IS NOT NULL AND lens!='chat') AS k, "
                            "COUNT(*) FILTER (WHERE lens IS NOT NULL AND lens!='chat' AND "
                            "  CASE WHEN jsonb_typeof(answer->'sources')='array' THEN jsonb_array_length(answer->'sources') ELSE 0 END = 0) AS z "
                            "FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s GROUP BY user_id", start, end)
        old_stop = c.execute(
            U._USAGE_TURN_CTE + " SELECT CASE WHEN answer->>'stop_kind' = ANY(%s) THEN answer->>'stop_kind' ELSE 'unknown' END AS k, "
            "COUNT(*) AS n FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s AND answer IS NOT NULL "
            "AND lens IS DISTINCT FROM 'clarify' AND answer->>'stop_kind' IS DISTINCT FROM 'stopped_by_user' GROUP BY 1",
            (start, end, list(U.stop_kind.STOP_KINDS), start, end)).fetchall()
        old_limits = c.execute(
            U._USAGE_TURN_CTE + " SELECT COALESCE(answer->'usage'->>'provider','unknown') AS provider, COUNT(*) AS turns, "
            + _old_limit_cols() + " FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s AND answer IS NOT NULL "
            "AND lens IS DISTINCT FROM 'clarify' GROUP BY provider ORDER BY turns DESC", (start, end, start, end)).fetchall()
        tok_where = " WHERE turn_created_at >= %s AND turn_created_at < %s AND jsonb_typeof(answer->'usage')='object' "
        sums = ("COUNT(*) AS turns, SUM({i}) AS input, SUM({c}) AS cached_input, SUM({o}) AS output, SUM({r}) AS reasoning_output"
                .format(i=_tok("input_tokens"), c=_tok("cached_input_tokens"), o=_tok("output_tokens"),
                        r=_tok("reasoning_output_tokens")))
        old_model = _old(c, "SELECT answer->'usage'->>'provider' AS provider, answer->'usage'->>'model' AS model, " + sums
                         + " FROM turns" + tok_where + "GROUP BY 1,2", start, end)
        old_user_tok = _old(c, "SELECT user_id AS uid, " + sums + " FROM turns" + tok_where + "GROUP BY 1", start, end)
        old_daily = _old(c, "SELECT (turn_created_at AT TIME ZONE 'Asia/Tokyo')::date AS date, SUM(" + _tok("input_tokens")
                         + ") AS input, SUM(" + _tok("output_tokens") + ") AS output FROM turns" + tok_where + "GROUP BY 1",
                         start, end)
        old_claims = _old(c, "SELECT answer->'usage'->>'provider' AS provider, answer->'usage'->>'depth_profile' AS depth_profile, "
                             "answer->'data'->'claims' AS claims, answer->'data'->'evidence_gate'->'missing_codes' AS gate "
                             "FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s "
                             "AND jsonb_typeof(answer->'data'->'claims')='array'", start, end)
        old_conv = _old(c, "SELECT conversation_id AS cid, COUNT(*) AS user_turns, AVG(CASE WHEN lens IS DISTINCT FROM 'clarify' "
                           "AND answer->>'duration_ms' ~ '^[0-9]+$' THEN (answer->>'duration_ms')::bigint END) AS rt "
                           "FROM turns WHERE turn_created_at >= %s AND turn_created_at < %s GROUP BY 1", start, end)
        old_rt = c.execute("SELECT (answer->>'duration_ms')::bigint AS d FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                           "WHERE m.created_at >= %s AND m.created_at < %s AND m.role='assistant' AND c.deleted_at IS NULL "
                           "AND c.origin='own' AND m.lens IS DISTINCT FROM 'clarify' AND answer->>'duration_ms' ~ '^[0-9]+$'",
                           (start, end)).fetchall()

    # 利用者別
    got = {u["uid"]: u for u in new["users"]}
    for r in old_users:
        u = got[r["uid"]]
        assert (u["turns"], u["conversations"], u["lens"], u["personal_turns"], u["knowledge_turns"], u["zero_hit_turns"]) == (
            r["turns"], r["conversations"], {"impact": r["i"], "qa": r["q"], "troubleshoot": r["t"], "chat": r["ch"]},
            r["p"], r["k"], r["z"]), r["uid"]
    assert {r["uid"] for r in old_users} == set(got) and mine == set(got)
    assert got[s.users[0]]["personal_turns"] == 1   # user 発言の personal（assistant 側の列ではない）
    # 終了理由・打ち切り
    assert {r["stop_kind"]: r["turns"] for r in new["stop_kinds"]} == {r["k"]: r["n"] for r in old_stop}
    by_provider = lambda rows: sorted(rows, key=lambda r: r["provider"])  # noqa: E731
    assert by_provider(new["limits"]["by_provider"]) == by_provider(
        [U._usage_limits_provider_row(r) for r in old_limits])
    # トークン: 失敗ターン（usage=0・activity に実消費）だけ新が多い＝それ以外は一致。
    # 失敗ターンの実消費を数えるのは意図した変更（activity 優先）。
    extra = {"input": 500, "cached_input": 100, "output": 50, "reasoning_output": 7}

    def _plus(row):
        return row | {k: row[k] + extra[k] for k in extra}

    def _by(rows, key):
        return {tuple(r[k] for k in key): r for r in rows}

    om = _by(old_model, ("provider", "model"))
    om[("codex", "m1")] = _plus(om[("codex", "m1")])
    nm = _by(new["tokens"]["by_model"], ("provider", "model"))
    assert {k: (v["input"], v["cached_input"], v["output"], v["reasoning_output"]) for k, v in nm.items()} == {
        k: (v["input"], v["cached_input"], v["output"], v["reasoning_output"]) for k, v in om.items()}
    ou = _by(old_user_tok, ("uid",))
    ou[(s.users[0],)] = _plus(ou[(s.users[0],)])
    assert {k: (v["input"], v["output"]) for k, v in _by(new["tokens"]["by_user"], ("uid",)).items()} == {
        k: (v["input"], v["output"]) for k, v in ou.items()}
    assert sum(d["input"] for d in new["tokens"]["daily"]) == sum(int(r["input"]) for r in old_daily) + extra["input"]
    # 最終回答の不明理由・不足軸
    exp_reasons: dict = {}
    exp_gate: dict = {}
    for r in old_claims:
        key = (r["depth_profile"] or "unknown", r["provider"] or "unknown")
        b = exp_reasons.setdefault(key, {})
        for cl in r["claims"]:
            if isinstance(cl, dict) and cl.get("status") == "unknown":
                b[cl.get("reason_code") or "unknown"] = b.get(cl.get("reason_code") or "unknown", 0) + 1
        if isinstance(r["gate"], list):
            g = exp_gate.setdefault(key, {})
            for code in r["gate"]:
                g[code] = g.get(code, 0) + 1
    assert {(x["depth_profile"], x["provider"], x["reason_code"]): x["claims"]
            for x in new["rounds"]["reason_codes"]["final"]} == {
        (d, p, code): n for (d, p), b in exp_reasons.items() for code, n in b.items()}
    assert {(b["depth_profile"], b["provider"]): b["missing_codes"] for b in new["rounds"]["by_depth_provider"]
            if b["missing_codes"]} == {k: v for k, v in exp_gate.items() if v}
    # 会話別・回答時間
    top = {e["conversation_id"]: e for e in new["conversations_top"]}
    for r in old_conv:
        if r["cid"] in top:
            assert top[r["cid"]]["user_turns"] == r["user_turns"]
            assert (top[r["cid"]]["response_time_avg_ms"] is None) == (r["rt"] is None)
    assert new["response_time"]["overall"]["n"] == len(old_rt)


def test_round_matching_equals_old_lateral_query():
    if not _try_init():
        pytest.skip("DB down")
    s = _seed_dataset()
    with psycopg.connect(store._dsn()) as c0:
        cid_a = c0.execute("SELECT conversation_id FROM messages WHERE id=%s", (s.orphan_user,)).fetchone()[0]
        cid_b = c0.execute("SELECT conversation_id FROM messages WHERE id=%s", (s.two_round_user,)).fetchone()[0]
    for mid, cid, offs in ((s.orphan_user, cid_a, (30,)), (s.two_round_user, cid_b, (10, 20))):
        for n, o in enumerate(offs, start=1):
            store.add_usage_event(kind="chat-round", provider="codex", model="m1", input_tokens=1, output_tokens=1,
                                  user_id=s.users[0], world="w", ts=s.time_of(mid) + timedelta(seconds=o),
                                  conversation_id=cid, meta={"round": n, "verdict": "sufficient", "stop": "sufficient"})
    start, end, _ = U._usage_period(time_from=_FROM, time_to=_TO)
    with store._connect() as c:
        old = c.execute(_OLD_ROUND_SQL, (start, start, end)).fetchall()
        new = U._round_rows_query(c, start, end)
    def key(r):
        return (r["ts"], r["provider"], r["turn_message_id"], r["depth_profile"])

    assert sorted(map(key, new)) == sorted(map(key, old))
    assert len(new) == 3
    # 返答の保存されなかったターンの巡は、次のターンの返答へ結合しない
    assert [r["turn_message_id"] for r in new if r["ts"] == s.time_of(s.orphan_user) + timedelta(seconds=30)] == [None]


def test_aggregation_does_not_read_answer_json():
    if not _try_init():
        pytest.skip("DB down")
    s = _seed_dataset()
    before = store.usage_stats(time_from=_FROM, time_to=_TO)
    with psycopg.connect(store._dsn()) as c:
        c.execute("UPDATE messages SET answer = '{}'::jsonb WHERE id = ANY(%s) AND role='assistant'",
                  ([m for m, _ in s.ids],))
    after = store.usage_stats(time_from=_FROM, time_to=_TO)
    assert after == before


def test_usage_stats_5000_turns_90_days_within_3s():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    with psycopg.connect(store._dsn(), autocommit=True) as c:
        c.execute("INSERT INTO conversations (user_id, version, title, created_at) "
                  "SELECT 'perf' || (g % 20), 'w' || (g % 3), 'x', now() - (g % 80 || ' days')::interval "
                  "FROM generate_series(1, 1000) g")
        cids = [r[0] for r in c.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT 1000").fetchall()]
        c.execute(
            "INSERT INTO messages (conversation_id, role, content, lens, personal, answer, created_at) "
            "SELECT cid, 'user', 'q', NULL, false, NULL, now() - ((n %% 85) || ' days')::interval - (n || ' minutes')::interval "
            "FROM unnest(%s::int[]) cid, generate_series(1, 5) n", (cids,))
        c.execute(
            "INSERT INTO messages (conversation_id, role, content, lens, answer, created_at) "
            "SELECT conversation_id, 'assistant', 'a', 'qa', "
            "  jsonb_build_object('usage', jsonb_build_object('provider','codex','model','m','input_tokens',10,'output_tokens',1), "
            "                     'pad', repeat(md5(random()::text), 60)), created_at + interval '30 seconds' "
            "FROM messages WHERE role='user' AND conversation_id = ANY(%s)", (cids,))
        c.execute(
            "INSERT INTO turn_metrics (message_id, conversation_id, user_message_id, user_id, world, created_at, lens, "
            "  provider, model, input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, stop_kind, "
            "  duration_ms, sources_count, mapping_source, mapping_version) "
            "SELECT a.id, a.conversation_id, (SELECT max(u.id) FROM messages u WHERE u.conversation_id=a.conversation_id "
            "  AND u.role='user' AND u.id < a.id), cv.user_id, cv.version, a.created_at, 'qa', 'codex', 'm', 10, 0, 1, 0, "
            "  'completed', 1000, 1, 'answer_only', 1 "
            "FROM messages a JOIN conversations cv ON cv.id=a.conversation_id "
            "WHERE a.role='assistant' AND a.conversation_id = ANY(%s)", (cids,))
        c.execute("ANALYZE messages")
        c.execute("ANALYZE turn_metrics")
    try:
        t0 = time.perf_counter()
        r = store.usage_stats(days=90)
        dt = time.perf_counter() - t0
        assert sum(u["turns"] for u in r["users"] if u["uid"].startswith("perf")) >= 4000
        assert dt < 3.0, f"usage_stats(90日) が {dt:.2f}s"
    finally:
        with psycopg.connect(store._dsn(), autocommit=True) as c:
            c.execute("DELETE FROM conversations WHERE id = ANY(%s)", (cids,))


# ---- 起動時の欠落行補完 ----

def _make_missing_rows(n=3):
    s = _Seed()
    s.conv(s.users[0], 0)
    ids = []
    for _ in range(n):
        s.msg("user")
        ids.append(s.msg("assistant", lens="qa", answer=_answer()))
    with psycopg.connect(store._dsn()) as c:
        c.execute("DELETE FROM turn_metrics WHERE message_id = ANY(%s)", (ids,))
    return ids


def test_backfill_missing_fills_only_missing_rows_and_rerun_is_noop():
    if not _try_init():
        pytest.skip("DB down")
    ids = _make_missing_rows()
    with psycopg.connect(store._dsn()) as c:
        keep = store.add_message(store.create_conversation(user_id="tmkeep", world="w")["id"], "assistant", "x",
                                 lens="qa", answer=_answer())["id"]
        c.execute("UPDATE turn_metrics SET model='sentinel' WHERE message_id=%s", (keep,))
        c.commit()
    r1 = TM.backfill_missing()
    assert r1["failed"] == 0 and r1["written"] >= len(ids)
    with psycopg.connect(store._dsn()) as c:
        assert c.execute("SELECT count(*) FROM turn_metrics WHERE message_id = ANY(%s)", (ids,)).fetchone()[0] == len(ids)
        # 既に行のあるメッセージは読み直さない・書き直さない
        assert c.execute("SELECT model FROM turn_metrics WHERE message_id=%s", (keep,)).fetchone()[0] == "sentinel"
    assert TM.backfill_missing() == {"written": 0, "failed": 0}


def test_startup_hook_runs_backfill_in_background_and_survives_failure(monkeypatch, caplog):
    import threading
    calls = []
    monkeypatch.setattr(TM, "backfill_missing", lambda: calls.append("ok") or {"written": 2, "failed": 1})
    with caplog.at_level("INFO", logger="sherpa"):
        api._backfill_turn_metrics_on_startup()
        for t in threading.enumerate():
            if t.name == "sherpa-turn-metrics-backfill":
                t.join(5)
    assert calls == ["ok"] and "written=2 failed=1" in caplog.text

    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(TM, "backfill_missing", _boom)
    caplog.clear()
    with caplog.at_level("WARNING", logger="sherpa"):
        api._backfill_turn_metrics_on_startup()   # 例外を呼び出し元へ出さない
        for t in threading.enumerate():
            if t.name == "sherpa-turn-metrics-backfill":
                t.join(5)
    assert "起動時補完に失敗" in caplog.text
