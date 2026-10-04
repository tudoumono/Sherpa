"""利用統計の SQL 集計（分布・会話別上位・調査ツール群）の契約テスト。

- 回答時間（avg/median/p90/max）・会話あたりターン数分布を SQL（`percentile_cont`/`percentile_disc`）で求めた値が、
  参照実装（`_compute_response_time_stats`/`_compute_conversation_turn_stats`＝最近傍順位・中央値は
  偶数件で中央 2 値の平均）と一致する。
- 会話別上位（`conversations_top`）の並び・件数・内訳が既知の入力どおり。
- 画面（`usage_stats`）と利用明細（`usage_export_turns`）が `turn_metrics` を読む（失敗ターンの実消費も数える）。
- `usage_stats` は集計値を返すだけで、ターン・巡の行を Python へ引かない（返る行数がデータ量に比例しない）。

要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from _common import _sfx, _try_init
from sherpa import store
import _usage_reference as R
from sherpa.store import usage as U

_JST = timezone(timedelta(hours=9))
_BASE = datetime(2004, 3, 5, 3, 0, tzinfo=_JST)
_FROM, _TO = "2004-03-01T00:00:00+09:00", "2004-03-20T00:00:00+09:00"


def _answer(*, provider="codex", tokens=(10, 0, 1, 0), duration_ms=1000, stop_kind="completed",
            activity_tokens=None, with_usage=True):
    a: dict = {"stop_kind": stop_kind, "duration_ms": duration_ms, "sources": [{"doc_id": "d"}]}
    if with_usage:
        i, ci, o, r = tokens
        a["usage"] = {"provider": provider, "model": "m", "depth_profile": "standard", "input_tokens": i,
                      "cached_input_tokens": ci, "output_tokens": o, "reasoning_output_tokens": r}
    if activity_tokens is not None:
        i, ci, o, r = activity_tokens
        a["activity"] = {"v": 1, "agents": [{"role": "parent", "model": "m", "rounds": [], "tools": {},
                                              "tokens": {"input_tokens": i, "cached_input_tokens": ci,
                                                         "output_tokens": o, "reasoning_output_tokens": r}}]}
    return a


class _Seed:
    def __init__(self):
        self.sfx = _sfx()
        self.users = (f"sqlA{self.sfx}", f"sqlB{self.sfx}")
        self.ids: list[tuple[int, datetime]] = []
        self.cids: list[int] = []

    def conv(self, uid, day, *, session=False):
        c = store.create_conversation(user_id=uid, world=f"sqlw{self.sfx}")
        self._cid, self._t = c["id"], _BASE + timedelta(days=day)
        self.cids.append(c["id"])
        if session:
            with psycopg.connect(store._dsn()) as k:
                k.execute("UPDATE conversations SET codex_session_id=%s WHERE id=%s", (f"sess{self.sfx}", c["id"]))
        return c["id"]

    def msg(self, role, **kw):
        m = store.add_message(self._cid, role, "x", **kw)
        self._t += timedelta(minutes=1)
        self.ids.append((m["id"], self._t))
        return m["id"]

    def event(self, kind, cid, **kw):
        store.add_usage_event(kind=kind, provider=kw.pop("provider", "openai"), model="m",
                              ts=self._t, conversation_id=cid, **kw)

    def place(self):
        with psycopg.connect(store._dsn()) as c:
            for mid, t in self.ids:
                c.execute("UPDATE messages SET created_at=%s WHERE id=%s", (t, mid))
            c.execute("UPDATE turn_metrics tm SET created_at = m.created_at FROM messages m WHERE m.id = tm.message_id")


def _seed() -> _Seed:
    """利用者 A: 会話 1（4 ターン・codex・セッションあり・失敗ターン 1 件）／利用者 B: 会話 2（2 ターン・openai）・
    会話 3（1 ターン・返答なし）。"""
    s = _Seed()
    ua, ub = s.users
    c1 = s.conv(ua, 0, session=True)
    for dur, toks in ((1000, (10, 0, 1, 0)), (2000, (20, 0, 2, 0)), (3000, (30, 0, 3, 0))):
        s.msg("user")
        s.msg("assistant", lens="qa", answer=_answer(duration_ms=dur, tokens=toks))
    s.msg("user")                                                   # 失敗ターン（usage は 0・activity に実消費）
    s.msg("assistant", lens="qa", answer=_answer(duration_ms=10000, tokens=(0, 0, 0, 0), stop_kind="timeout",
                                                 activity_tokens=(500, 100, 50, 7)))
    s.event("chat-sub", c1, input_tokens=100, output_tokens=50, elapsed_ms=20, calls=2)
    s.event("intent", c1, input_tokens=None, output_tokens=None, elapsed_ms=None)
    c2 = s.conv(ub, 1)
    for dur, toks in ((500, (7, 0, 3, 0)), (700, (8, 0, 4, 0))):
        s.msg("user")
        s.msg("assistant", lens="chat", answer=_answer(provider="openai", duration_ms=dur, tokens=toks))
    s.event("chat-review", c2, input_tokens=1000, output_tokens=10, elapsed_ms=40)
    c3 = s.conv(ub, 2)
    s.msg("user")                                                   # 返答なし（停止）
    s.place()
    s.c1, s.c2, s.c3 = c1, c2, c3
    return s


@pytest.fixture
def seed():
    """固定の窓へ仕込み、テストごとに片付ける（窓全体の合計を確かめるため、他のテストの仕込みと混ぜない）。"""
    if not _try_init():
        pytest.skip("DB down")
    s = _seed()
    yield s
    with psycopg.connect(store._dsn(), autocommit=True) as c:
        c.execute("DELETE FROM usage_events WHERE conversation_id = ANY(%s)", (s.cids,))
        c.execute("DELETE FROM conversations WHERE id = ANY(%s)", (s.cids,))


def test_response_time_and_conversation_distribution_match_reference_definitions(seed):
    s = seed
    got = store.usage_stats(time_from=_FROM, time_to=_TO)
    all_d = [1000, 2000, 3000, 10000, 500, 700]
    by_p = {"codex": [1000, 2000, 3000, 10000], "openai": [500, 700]}
    ref = R._compute_response_time_stats(all_d)
    assert {k: got["response_time"]["overall"][k] for k in ("avg", "median", "max", "p90", "n")} == ref
    assert got["response_time"]["overall"]["provider"] is None
    assert got["response_time"]["overall"]["median"] == 1500.0 and got["response_time"]["overall"]["p90"] == 10000.0
    rows = got["response_time"]["by_provider"]
    assert [r["provider"] for r in rows] == ["codex", "openai"]   # 件数の多い順
    for r in rows:
        assert {k: r[k] for k in ("avg", "median", "max", "p90", "n")} == R._compute_response_time_stats(by_p[r["provider"]])
    # 会話あたり user ターン数: 会話 1=4・会話 2=2・会話 3=1
    turns, resume = R._compute_conversation_turn_stats(
        [{"user_turns": 4, "codex_session_id": "x"}, {"user_turns": 2, "codex_session_id": None},
         {"user_turns": 1, "codex_session_id": None}])
    assert got["conversation_turns"] == turns and got["resume_rate"] == resume == 0.5
    assert got["conversation_turns"]["session_eligible"] == 2 and got["conversation_turns"]["session_recorded"] == 1
    # 回答時間の無い母集団は全て None・件数 0
    none = store.usage_stats(time_from="2004-05-01T00:00:00+09:00", time_to="2004-05-02T00:00:00+09:00")
    assert none["response_time"]["overall"] == {"avg": None, "median": None, "max": None, "p90": None,
                                                 "n": 0, "provider": None}
    assert none["conversation_turns"]["avg"] is None and none["resume_rate"] is None
    assert none["conversations_top"] == [] and s.cids


def test_conversations_top_ranks_by_tokens_turns_elapsed_with_kind_breakdown(seed):
    s = seed
    # トークン: 会話 1 = chat (10+1 + 20+2 + 30+3 + 失敗ターン 500+50) + chat-sub 150 = 766／会話 2 = chat 22 + 1010 = 1032
    top = store.usage_stats(time_from=_FROM, time_to=_TO)["conversations_top"]
    mine = [e for e in top if e["conversation_id"] in (s.c1, s.c2, s.c3)]
    assert [e["conversation_id"] for e in mine] == [s.c2, s.c1, s.c3]
    c1 = next(e for e in mine if e["conversation_id"] == s.c1)
    assert c1["user_turns"] == 4 and c1["uid"] == s.users[0] and c1["world"] == f"sqlw{s.sfx}"
    kinds = {k["kind"]: k for k in c1["kinds"]}
    assert kinds["chat"]["calls"] == 4 and kinds["chat"]["input"] == 560 and kinds["chat"]["output"] == 56
    assert kinds["chat-sub"]["calls"] == 2 and kinds["chat-sub"]["input"] == 100 and kinds["chat-sub"]["elapsed_ms_total"] == 20
    assert kinds["intent"]["input"] is None and kinds["intent"]["elapsed_ms_avg"] is None   # 報告不能は None のまま
    assert [k["kind"] for k in c1["kinds"]] == ["chat", "chat-sub", "intent"]               # input+output の降順
    assert c1["response_time_avg_ms"] == 4000.0                                            # (1000+2000+3000+10000)/4
    c3 = next(e for e in mine if e["conversation_id"] == s.c3)
    assert c3["kinds"] == [] and c3["response_time_avg_ms"] is None


def test_usage_stats_screen_reads_turn_metrics(seed):
    stats = store.usage_stats(time_from=_FROM, time_to=_TO)
    # 日別 tokens は失敗ターンの実消費も数える
    d0, d1 = (_BASE + timedelta(days=0)).date().isoformat(), (_BASE + timedelta(days=1)).date().isoformat()
    tokens_series = {r["date"]: r for r in stats["tokens"]["daily"]}
    assert tokens_series[d0]["input"] == 560 and tokens_series[d1]["input"] == 15
    # 終了理由は返答のあるターンだけ（停止・実行中は数えない）
    assert [x for x in stats["stop_kinds"] if x["stop_kind"] == "timeout"] == [{"stop_kind": "timeout", "turns": 1}]


def test_export_turns_use_turn_metrics(seed):
    s = seed
    # 利用明細: 回答 1 件 = 1 行・トークンは画面と同じ turn_metrics（失敗ターンの実消費を含む）
    rows = [r for r in U.usage_export_turns(time_from=_FROM, time_to=_TO) if r["conversation_id"] in s.cids]
    assert len(rows) == 6                                          # 返答なしの会話 3 は含まない
    assert sum(r["answer"]["usage"]["input_tokens"] for r in rows) == 560 + 15
    failed = next(r for r in rows if r["answer"]["stop_kind"] == "timeout")
    assert failed["answer"]["usage"]["input_tokens"] == 500 and failed["answer"]["duration_ms"] == 10000
    assert set(failed["answer"]) == {"usage", "limits", "activity", "stop_kind", "codex_error_code",
                                     "duration_ms", "investigation"}
    assert [r["message_id"] for r in rows] == sorted(r["message_id"] for r in rows)


class _CountingConn:
    """`usage.py` の `_connect()` を包み、`fetchall`/`fetchone` が返した行数を数える。"""

    def __init__(self, inner, counter):
        self._inner, self._counter = inner, counter

    def __enter__(self):
        self._c = self._inner.__enter__()
        return self

    def __exit__(self, *a):
        return self._inner.__exit__(*a)

    def execute(self, sql, params=None):
        cur = self._c.execute(sql, params)
        counter = self._counter

        class _R:
            def fetchall(self_):
                rows = cur.fetchall()
                counter[0] += len(rows)
                return rows

            def fetchone(self_):
                row = cur.fetchone()
                counter[0] += 1 if row else 0
                return row
        return _R()


def _fetched_rows(monkeypatch, **kw) -> tuple[int, float]:
    counter = [0]
    orig = U._connect
    monkeypatch.setattr(U, "_connect", lambda: _CountingConn(orig(), counter))
    t0 = time.perf_counter()
    store.usage_stats(days=90, **kw)
    return counter[0], time.perf_counter() - t0


def test_usage_stats_returns_aggregates_not_rows_at_scale(monkeypatch):
    """合成 3,000 会話×5 ターン（巡・補助 AI 呼び出し付き）を足しても、Python へ返る行数が増えない
    （ターン・巡・会話の行を引いて数えていない）・表示時間は 3 秒以内。巡の meta には巡ごとに値の違う欄
    （`roles` のトークン数）も入れる＝巡を束ねる単位に集計に使わない欄が混ざると巡数に比例して遅くなる。"""
    if not _try_init():
        pytest.skip("DB down")
    base_rows, _ = _fetched_rows(monkeypatch)
    with psycopg.connect(store._dsn(), autocommit=True) as c:
        c.execute("INSERT INTO conversations (user_id, version, title, created_at) "
                  "SELECT 'sqlperf' || (g % 20), 'w' || (g % 3), 'x', now() - (g % 80 || ' days')::interval "
                  "FROM generate_series(1, 3000) g")
        cids = [r[0] for r in c.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT 3000").fetchall()]
        c.execute(
            "INSERT INTO messages (conversation_id, role, content, lens, personal, answer, created_at) "
            "SELECT cid, 'user', 'q', NULL, false, NULL, now() - ((n %% 85) || ' days')::interval - (n || ' minutes')::interval "
            "FROM unnest(%s::int[]) cid, generate_series(1, 5) n", (cids,))
        c.execute(
            "INSERT INTO messages (conversation_id, role, content, lens, answer, created_at) "
            "SELECT conversation_id, 'assistant', 'a', 'qa', '{}'::jsonb, created_at + interval '30 seconds' "
            "FROM messages WHERE role='user' AND conversation_id = ANY(%s)", (cids,))
        c.execute(
            "INSERT INTO turn_metrics (message_id, conversation_id, user_message_id, user_id, world, created_at, lens, "
            "  provider, model, depth_profile, input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, "
            "  stop_kind, duration_ms, sources_count, claims_unknown_reasons, mapping_source, mapping_version) "
            "SELECT a.id, a.conversation_id, (SELECT max(u.id) FROM messages u WHERE u.conversation_id=a.conversation_id "
            "  AND u.role='user' AND u.created_at = a.created_at - interval '30 seconds'), "
            "  cv.user_id, cv.version, a.created_at, 'qa', (ARRAY['codex','openai'])[1 + a.id %% 2], 'm', "
            "  (ARRAY['light','standard','deep'])[1 + a.id %% 3], 10, 0, 1, 0, 'completed', 1000 + a.id %% 5000, 1, "
            "  jsonb_build_object('not_found', a.id %% 7), 'answer_only', 1 "
            "FROM messages a JOIN conversations cv ON cv.id=a.conversation_id "
            "WHERE a.role='assistant' AND a.conversation_id = ANY(%s)", (cids,))
        c.execute(
            "INSERT INTO usage_events (ts, kind, provider, model, input_tokens, output_tokens, calls, elapsed_ms, "
            "  user_id, conversation_id, meta) "
            "SELECT u.created_at + (r * 10 || ' seconds')::interval, 'chat-round', 'codex', 'm', 5, 1, 1, 100, "
            "  'sqlperf0', u.conversation_id, jsonb_build_object('round', r, 'citations_delta', r %% 3, "
            "    'claims', jsonb_build_object('confirmed', r, 'inferred', 1, 'unknown', 0), "
            "    'verdict', 'sufficient', 'stop', 'sufficient', 'lens', 'qa', "
            "    'roles', jsonb_build_object('worker', jsonb_build_object('input_tokens', (random() * 100000)::int, "
            "      'output_tokens', (random() * 5000)::int))) "
            "FROM messages u, generate_series(1, 2) r WHERE u.role='user' AND u.conversation_id = ANY(%s)", (cids,))
        c.execute(
            "INSERT INTO usage_events (ts, kind, provider, model, input_tokens, output_tokens, calls, elapsed_ms, "
            "  user_id, conversation_id) "
            "SELECT now() - ((g %% 80) || ' days')::interval, 'chat-sub', 'openai', 'm', 100, 10, 1, 50, "
            "  'sqlperf' || (g %% 20), cids[1 + g %% 3000] FROM (SELECT %s::int[] AS cids) x, generate_series(1, 6000) g",
            (cids,))
        for t in ("messages", "turn_metrics", "usage_events"):
            c.execute(f"ANALYZE {t}")
    try:
        rows, dt = _fetched_rows(monkeypatch)
        assert dt < 3.0, f"usage_stats(90日) が {dt:.2f}s"
        # 15,000 ターン・30,000 巡・6,000 呼び出しを足しても、返る行数の増えは分布・上位 20 件・日別の分だけ
        assert rows - base_rows < 400, (base_rows, rows)
        r = store.usage_stats(days=90)
        assert sum(u["turns"] for u in r["users"] if u["uid"].startswith("sqlperf")) >= 14000
        assert sum(b["rounds"] for b in r["rounds"]["by_depth_provider"]) >= 29000
        assert len(r["conversations_top"]) == 20
    finally:
        with psycopg.connect(store._dsn(), autocommit=True) as c:
            c.execute("DELETE FROM usage_events WHERE conversation_id = ANY(%s)", (cids,))
            c.execute("DELETE FROM conversations WHERE id = ANY(%s)", (cids,))


def test_retention_sql_matches_reference(seed):
    """週次人数と連続週ペアの再訪率（SQL）が参照実装 `_compute_retention` と一致する。
    利用者 A は 1・2 週目、利用者 B は 2・4 週目（3 週目は空き＝前週扱いしない）。"""
    from datetime import date
    ua, ub = seed.users
    for uid, weeks in ((ua, (0, 1)), (ub, (1, 3))):
        for w in weeks:
            seed.conv(uid, 7 * w + 20)          # 2004-03-25 起点（既存シードと別の週）
            seed.msg("user")
    seed.place()
    got = store.usage_stats(time_from="2004-03-24T00:00:00+09:00", time_to="2004-05-01T00:00:00+09:00")["retention"]
    d0 = date(2004, 3, 5) + timedelta(days=20)
    base = d0 - timedelta(days=d0.weekday())
    ref = R._compute_retention([
        {"uid": ua, "week_start": base}, {"uid": ua, "week_start": base + timedelta(days=7)},
        {"uid": ub, "week_start": base + timedelta(days=7)}, {"uid": ub, "week_start": base + timedelta(days=21)}])
    assert got == ref
    assert got["revisit_rate"] == 1.0   # 連続する週ペアは 1→2 週目だけ（A が継続）
