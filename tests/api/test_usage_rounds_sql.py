"""巡別記録（`chat-round`）の SQL 集計の契約テスト。

- 旧実装（巡の行を Python へ引いて `_compute_round_stats`/`_compute_final_*` で集計）の凍結コピーと、
  SQL 集計（`_build_usage_rounds`＋`_round_stats_from_sql`／`_final_claims_from_sql`）の結果一致。
  過去に保存済みの巡（欠けた欄・型の崩れた欄・meta が NULL/配列の行）も同じ規則で数える。
- 巡の行を Python へ引かない（返答が増えても集計時間が巡数に比例しない）合成データでの表示時間。

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.types.json import Jsonb

from _common import _sfx, _try_init
from sherpa import store
from sherpa.store import usage as U

_JST = timezone(timedelta(hours=9))
_FROM, _TO = "2002-03-01T00:00:00+09:00", "2002-03-20T00:00:00+09:00"
_T0 = datetime(2002, 3, 5, 3, 0, tzinfo=_JST)   # 他のテストが書かない過去の窓

# 旧 `_round_rows_query` の凍結コピー（巡の行＝meta ごと Python へ返す方式）。
_OLD_ROUND_ROWS_SQL = (
    "WITH rounds AS ("
    "  SELECT e.id, e.ts, e.conversation_id FROM usage_events e "
    "  WHERE e.kind = 'chat-round' AND e.ts >= %s AND e.conversation_id IS NOT NULL"
    "), user_msgs AS ("
    "  SELECT m.conversation_id, m.created_at, "
    "    LEAD(m.created_at) OVER (PARTITION BY m.conversation_id ORDER BY m.created_at) AS next_user_created_at "
    "  FROM messages m WHERE m.role = 'user' AND m.conversation_id IN (SELECT conversation_id FROM rounds)"
    "), in_period AS ("
    "  SELECT r.id AS round_id, r.ts, r.conversation_id, u.next_user_created_at "
    "  FROM rounds r LEFT JOIN user_msgs u "
    "    ON u.conversation_id = r.conversation_id AND u.created_at <= r.ts "
    "    AND (u.next_user_created_at IS NULL OR r.ts < u.next_user_created_at) "
    "  JOIN conversations c ON c.id = r.conversation_id "
    "  WHERE c.deleted_at IS NULL AND c.origin = 'own' "
    "    AND COALESCE(u.created_at, r.ts) >= %s AND COALESCE(u.created_at, r.ts) < %s"
    "), matched AS ("
    "  SELECT DISTINCT ON (o.round_id) o.round_id, tm.message_id AS turn_message_id, tm.depth_profile "
    "  FROM in_period o "
    "  LEFT JOIN (turn_metrics tm JOIN messages am ON am.id = tm.message_id) "
    "    ON tm.conversation_id = o.conversation_id AND am.created_at >= o.ts "
    "    AND (o.next_user_created_at IS NULL OR am.created_at < o.next_user_created_at) "
    "  ORDER BY o.round_id, am.created_at ASC"
    ") "
    "SELECT e.ts, e.provider, e.input_tokens, e.output_tokens, e.elapsed_ms, e.meta, "
    "  x.turn_message_id, x.depth_profile "
    "FROM matched x JOIN usage_events e ON e.id = x.round_id")
_OLD_FINAL_ROWS_SQL = (
    "SELECT provider, depth_profile, claims_unknown_reasons AS unknown_reasons, gate_missing_codes "
    "FROM usage_turns WHERE claims_unknown_reasons IS NOT NULL")


def _answer(depth, *, provider="codex", unknown_reasons=(), gate=None):
    data: dict = {"claims": [{"status": "unknown", "reason_code": c} for c in unknown_reasons]
                  + [{"status": "confirmed"}]}
    if gate is not None:
        data["evidence_gate"] = {"missing_codes": gate}
    return {"stop_kind": "completed", "duration_ms": 10, "sources": [],
            "usage": {"provider": provider, "model": "m", "depth_profile": depth,
                      "input_tokens": 1, "output_tokens": 1}, "data": data}


class _World:
    """窓の中へ会話を仕込む。`at(分)` は基準時刻からの経過で時刻を置く。"""

    def __init__(self):
        self.sfx = _sfx()
        self.placed: list[tuple[int, datetime]] = []
        self.events: list[tuple] = []

    def conv(self, *, origin="own", deleted=False):
        cid = store.create_conversation(user_id=f"rs{self.sfx}", world=f"rsw{self.sfx}")["id"]
        with psycopg.connect(store._dsn()) as c:
            c.execute("UPDATE conversations SET origin=%s, deleted_at=CASE WHEN %s THEN now() END WHERE id=%s",
                      (origin, deleted, cid))
        return cid

    def msg(self, cid, role, t, **kw):
        m = store.add_message(cid, role, "x", **kw)
        self.placed.append((m["id"], t))
        return m["id"]

    def round(self, cid, t, meta, *, provider="codex", tokens=(1, 1), elapsed=5):
        self.events.append((t, provider, tokens[0], tokens[1], elapsed, cid, meta))

    def flush(self):
        with psycopg.connect(store._dsn()) as c:
            for mid, t in self.placed:
                c.execute("UPDATE messages SET created_at=%s WHERE id=%s", (t, mid))
            for t, provider, i, o, el, cid, meta in self.events:
                c.execute("INSERT INTO usage_events (ts, kind, provider, model, input_tokens, output_tokens, "
                          "calls, elapsed_ms, conversation_id, meta) VALUES (%s,'chat-round',%s,'m',%s,%s,1,%s,%s,%s)",
                          (t, provider, i, o, el, cid, Jsonb(meta) if meta is not None else None))
            # 返答の `turn_metrics.created_at` も置いた時刻に揃える
            c.execute("UPDATE turn_metrics tm SET created_at = m.created_at FROM messages m WHERE m.id = tm.message_id")


def _meta_variants():
    lim_int, lim_int2 = U._USAGE_LIMIT_INT_FIELDS[0], U._USAGE_LIMIT_INT_FIELDS[1]
    lim_bool, lim_bool2 = U._USAGE_LIMIT_BOOL_FIELDS[0], U._USAGE_LIMIT_BOOL_FIELDS[1]
    return [
        {"round": 1, "citations_delta": 2,
         "claims": {"confirmed": 3, "inferred": 1, "unknown": 2,
                    "reason_codes": {"not_found": 2, "conflict": 1, "bad_bool": True, "bad_float": 1.5, "zero": 0}},
         "limits": {lim_int: 2, lim_int2: 1.5, lim_bool: True, lim_bool2: False, "not_a_limit": 9,
                    U._USAGE_LIMIT_INT_FIELDS[2]: "x"},
         "verdict": "insufficient", "stop": "max_rounds",
         "missing_codes": ["source", "", "source", 3, None, "design"], "missing": ["自由文"]},
        {"round": 2, "citations_delta": 1.5, "claims": {"confirmed": True, "inferred": 2.0, "unknown": 0},
         "verdict": "", "stop": 5, "missing_codes": []},
        {"round": 2.0, "citations_delta": True, "claims": None, "limits": []},   # 巡番号が整数でない
        {"round": True, "claims": {"reason_codes": None}, "verdict": "sufficient"},
        {"round": "3", "stop": "sufficient", "missing_codes": ["source"]},
        {},                                                                       # 空の meta
        [],                                                                       # 配列の meta（空）
        None,                                                                     # meta なし
        {"round": 3, "citations_delta": -1, "claims": {"confirmed": 1, "unknown": 1,
                                                         "reason_codes": {"not_found": 1}},
         "verdict": "sufficient", "stop": "sufficient", "missing_codes": ["design"]},
    ]


def _seed() -> _World:
    w = _World()
    metas = _meta_variants()
    # 会話 A: 通常の 2 巡＋返答 / 返答なし（巡だけ残る）/ 次のターンは返答 2 件（最初の返答へ結合）
    a = w.conv()
    w.msg(a, "user", _T0)
    w.round(a, _T0 + timedelta(seconds=10), metas[0])
    w.round(a, _T0 + timedelta(seconds=20), metas[1], provider="openai", tokens=(None, 7))
    w.msg(a, "assistant", _T0 + timedelta(seconds=60), lens="qa",
          answer=_answer("standard", unknown_reasons=("not_found", "not_found"), gate=["source", "design"]))
    w.msg(a, "user", _T0 + timedelta(minutes=10))
    w.round(a, _T0 + timedelta(minutes=10, seconds=5), metas[2], tokens=(None, None), elapsed=None)
    w.round(a, _T0 + timedelta(minutes=10, seconds=6), metas[3])
    w.msg(a, "user", _T0 + timedelta(minutes=20))
    w.round(a, _T0 + timedelta(minutes=20, seconds=5), metas[4])
    w.round(a, _T0 + timedelta(minutes=20, seconds=15), metas[8], elapsed=40)
    w.msg(a, "assistant", _T0 + timedelta(minutes=20, seconds=30), lens="qa",
          answer=_answer("", unknown_reasons=("conflict",), gate=[]))
    w.msg(a, "assistant", _T0 + timedelta(minutes=20, seconds=40), lens="qa", answer=_answer("deep"))
    # 会話 B: 期間の境界。所属ターンの時刻で数える（巡の ts ではない）
    b = w.conv()
    w.msg(b, "user", datetime(2002, 3, 1, 0, 0, tzinfo=_JST))                      # 下限ちょうど＝期間内
    w.round(b, datetime(2002, 3, 1, 0, 0, 5, tzinfo=_JST), metas[0])
    w.msg(b, "assistant", datetime(2002, 3, 1, 0, 0, 30, tzinfo=_JST), lens="qa", answer=_answer("deep", provider="openai"))
    w.msg(b, "user", datetime(2002, 2, 28, 23, 59, tzinfo=_JST))                   # 期間外のターン
    w.round(b, datetime(2002, 3, 1, 0, 0, 1, tzinfo=_JST), metas[1])               # 巡の ts は期間内でも除外
    w.msg(b, "user", datetime(2002, 3, 19, 23, 59, 30, tzinfo=_JST))               # 期間内の最後のターン
    w.round(b, datetime(2002, 3, 20, 0, 0, 30, tzinfo=_JST), metas[8])             # 巡の ts は `to` を越える＝数える
    w.msg(b, "user", datetime(2002, 3, 20, 0, 0, tzinfo=_JST))                     # 上限ちょうど＝期間外
    w.round(b, datetime(2002, 3, 20, 0, 0, 10, tzinfo=_JST), metas[0])
    # 会話 C: 削除済み・内部成果物は数えない
    for kw in ({"deleted": True}, {"origin": "sanitized_snapshot"}):
        c = w.conv(**kw)
        w.msg(c, "user", _T0 + timedelta(hours=1))
        w.round(c, _T0 + timedelta(hours=1, seconds=5), metas[0])
    # 会話 D: user 発言が無い巡（巡自身の ts で期間判定・対応する返答なし）／会話 id なしの巡
    d = w.conv()
    w.round(d, _T0 + timedelta(hours=2), metas[5])
    w.round(d, _T0 + timedelta(hours=2, minutes=1), metas[6], provider="")
    w.round(d, _T0 + timedelta(hours=2, minutes=2), metas[7], provider="ollama")
    w.round(None, _T0 + timedelta(hours=3), metas[0])
    # 会話 E: 別利用者・別の深さ（返答 1 件・巡 3 つ）
    e = w.conv()
    w.msg(e, "user", _T0 + timedelta(days=2))
    for n, t in enumerate((5, 10, 15)):
        w.round(e, _T0 + timedelta(days=2, seconds=t), {"round": n + 1, "verdict": "sufficient", "stop": "sufficient",
                                                         "citations_delta": n, "missing_codes": ["source"]})
    w.msg(e, "assistant", _T0 + timedelta(days=2, seconds=40), lens="qa", answer=_answer("light", provider="ollama"))
    w.flush()
    return w


def _reference(c, start, end) -> dict:
    """旧実装: 巡の行を Python へ引き、Python 側で集計する。"""
    rows = c.execute(_OLD_ROUND_ROWS_SQL, (start, start, end)).fetchall()
    out = U._compute_round_stats(rows)
    U._build_usage_turns(c, start, end)
    final_rows = c.execute(_OLD_FINAL_ROWS_SQL).fetchall()
    out["reason_codes"] = U._round_reason_codes(out, U._compute_final_reason_codes(final_rows))
    U._merge_final_missing_codes(out, final_rows)
    return out


def test_sql_round_stats_equal_python_aggregation_on_edge_cases():
    if not _try_init():
        pytest.skip("DB down")
    _seed()
    start, end, _ = U._usage_period(time_from=_FROM, time_to=_TO)
    with store._connect() as c:
        old = _reference(c, start, end)
        U._build_usage_rounds(c, start, end)
        new = U._round_stats_from_sql(c)
        new["reason_codes"] = U._round_reason_codes(new, U._final_claims_from_sql(c)[0])
        U._merge_final_missing_agg(new, U._final_claims_from_sql(c)[1])
    assert old["by_depth_provider"], "データが入っていない"
    assert old["unmatched_rounds"] >= 2 and any(b["round_no"] is None for b in old["by_round"])
    dp = old["by_depth_provider"]
    assert all(any(b[f] for b in dp) for f in ("limits", "verdicts", "stops", "missing_codes", "reason_codes"))
    assert any(b["elapsed_n"] < b["rounds"] for b in dp) and any(b["tokens_n"] < b["rounds"] for b in dp)
    assert {b["provider"] for b in dp} >= {"codex", "openai", "unknown", "ollama"}
    assert len(old["round_distribution"]) >= 3 and old["reason_codes"]["final"]
    assert new == old


def test_usage_stats_and_depth_rounds_use_the_same_numbers():
    if not _try_init():
        pytest.skip("DB down")
    _seed()
    start, end, _ = U._usage_period(time_from=_FROM, time_to=_TO)
    with store._connect() as c:
        old = _reference(c, start, end)
    got = store.usage_stats(time_from=_FROM, time_to=_TO)["rounds"]
    assert got == old
    tool = store.usage_depth_rounds(time_from=_FROM, time_to=_TO)
    assert tool["unmatched_rounds"] == old["unmatched_rounds"]
    assert tool["round_distribution"] == old["round_distribution"]
    assert tool["reason_codes"] == old["reason_codes"]


def test_round_stats_are_empty_when_no_rounds_in_period():
    if not _try_init():
        pytest.skip("DB down")
    start, end, _ = U._usage_period(time_from="2003-03-01T00:00:00+09:00", time_to="2003-03-02T00:00:00+09:00")
    with store._connect() as c:
        U._build_usage_rounds(c, start, end)
        new = U._round_stats_from_sql(c)
    assert new == U._compute_round_stats([])


def test_round_stats_time_does_not_grow_with_round_count():
    """合成 3,000 ターン×3 巡（9,000 巡）。巡の行を Python へ引かない集計は 1 秒台に収まる。"""
    if not _try_init():
        pytest.skip("DB down")
    with psycopg.connect(store._dsn(), autocommit=True) as c:
        c.execute("INSERT INTO conversations (user_id, version, title, created_at) "
                  "SELECT 'rperf' || (g % 20), 'w' || (g % 3), 'x', now() - (g % 80 || ' days')::interval "
                  "FROM generate_series(1, 600) g")
        cids = [r[0] for r in c.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT 600").fetchall()]
        c.execute(
            "INSERT INTO messages (conversation_id, role, content, answer, created_at) "
            "SELECT cid, 'user', 'q', NULL, now() - ((n %% 85) || ' days')::interval - (n || ' minutes')::interval "
            "FROM unnest(%s::int[]) cid, generate_series(1, 5) n", (cids,))
        c.execute(
            "INSERT INTO messages (conversation_id, role, content, lens, answer, created_at) "
            "SELECT conversation_id, 'assistant', 'a', 'qa', '{}'::jsonb, created_at + interval '40 seconds' "
            "FROM messages WHERE role='user' AND conversation_id = ANY(%s)", (cids,))
        c.execute(
            "INSERT INTO turn_metrics (message_id, conversation_id, user_message_id, user_id, world, created_at, lens, "
            "  provider, model, depth_profile, stop_kind, duration_ms, sources_count, mapping_source, mapping_version) "
            "SELECT a.id, a.conversation_id, (SELECT max(u.id) FROM messages u WHERE u.conversation_id=a.conversation_id "
            "  AND u.role='user' AND u.id < a.id), cv.user_id, cv.version, a.created_at, 'qa', 'codex', 'm', "
            "  (ARRAY['light','standard','deep'])[1 + a.id %% 3], 'completed', 1000, 1, 'answer_only', 1 "
            "FROM messages a JOIN conversations cv ON cv.id=a.conversation_id "
            "WHERE a.role='assistant' AND a.conversation_id = ANY(%s)", (cids,))
        c.execute(
            "INSERT INTO usage_events (ts, kind, provider, model, input_tokens, output_tokens, calls, elapsed_ms, "
            "  conversation_id, meta) "
            "SELECT u.created_at + (r * 10 || ' seconds')::interval, 'chat-round', 'codex', 'm', 5, 1, 1, 100, "
            "  u.conversation_id, jsonb_build_object('round', r, 'citations_delta', r, "
            "    'claims', jsonb_build_object('confirmed', r, 'inferred', 1, 'unknown', 0, "
            "      'reason_codes', jsonb_build_object('not_found', 1)), "
            "    'verdict', 'sufficient', 'stop', 'sufficient', 'missing_codes', jsonb_build_array('source'), "
            "    'missing', repeat(md5(random()::text), 4)) "
            "FROM messages u, generate_series(1, 3) r WHERE u.role='user' AND u.conversation_id = ANY(%s)", (cids,))
        for t in ("messages", "turn_metrics", "usage_events"):
            c.execute(f"ANALYZE {t}")
    try:
        start, end, _ = U._usage_period(90)
        t0 = time.perf_counter()
        with store._connect() as c:
            U._build_usage_rounds(c, start, end)
            stats = U._round_stats_from_sql(c)
        dt = time.perf_counter() - t0
        assert dt < 3.0, f"巡の集計が {dt:.2f}s"
        assert sum(b["rounds"] for b in stats["by_depth_provider"]) >= 9000 - 600
        assert stats["unmatched_rounds"] == 0
    finally:
        with psycopg.connect(store._dsn(), autocommit=True) as c:
            c.execute("DELETE FROM usage_events WHERE conversation_id = ANY(%s)", (cids,))
            c.execute("DELETE FROM conversations WHERE id = ANY(%s)", (cids,))
