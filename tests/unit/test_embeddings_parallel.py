"""埋め込み HTTP の有界並列＋429/5xx 再送の単体テスト。

対象: `sherpa/embeddings.py` の `effective_embed_parallel`・`_embed_batches_parallel`・
`_embed_batch`（再送ループ）・`embed()`（並列分岐）。

- ベクトル値・window/pooling/正規化は一切変えない前提（並列化は HTTP 送信の束ね方だけを変える）。
- モックは外部境界（`llm.post_json`/`llm.openai_post_json`）または `embeddings._embed_batch`
  自体に置く（`docs/20-開発ハーネス.md` §6）。
- DB 不要（`tests/unit/conftest.py::_hermetic_metering_record` が autouse で `metering.record` を
  no-op に固定する）。
"""
from __future__ import annotations

import contextvars
import email.message
import threading
import time
import urllib.error

import pytest

from sherpa import embeddings, llm, metering
from sherpa.store import usage_events as ue

_real_metering_record = metering.record


def _enable(monkeypatch):
    from sherpa import store
    monkeypatch.setattr(store, "get_system_settings",
                        lambda **kw: {"personal_api_keys_allowed": True})
    monkeypatch.setattr(metering, "record", _real_metering_record)


def _spy(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ue, "add_usage_event", lambda **kw: calls.append(kw))
    return calls


def _http_error(code: int, headers=None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", headers, None)


# ===== effective_embed_parallel（クランプ）=====

@pytest.mark.parametrize("raw,expected", [
    (None, 4), ({}, 4), (0, 4), (-1, 4), (17, 4), (True, 4), ("3", 4),
    (1, 1), (16, 16), (8, 8),
])
def test_effective_embed_parallel_clamps_to_default(raw, expected):
    settings = {} if raw is None else {"embed_parallel": raw}
    assert embeddings.effective_embed_parallel(settings) == expected


def test_cfg_carries_effective_parallel_into_provider_dicts(monkeypatch):
    from sherpa import store
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    monkeypatch.setattr(store, "get_system_settings", lambda: {
        "personal_api_keys_allowed": True, "embed_parallel": 6})
    c = embeddings.cfg({"extract_provider": "openai", "openai_api_key": "sk-test"})
    assert c is not None and c["parallel"] == 6


# ===== 並列送信でも順序が保たれる（origins による復元・完了順に依存しない）=====

def test_embed_parallel_preserves_order_with_staggered_completion(monkeypatch):
    texts = [f"t{i}" for i in range(120)]   # _BATCH=50 → 3バッチ

    order = []
    lock = threading.Lock()

    def fake_embed_batch(batch, c):
        n = len(batch)
        # バッチごとに完了順を意図的にずらす（後から送られたバッチほど早く返る）。
        with lock:
            order.append(batch[0])
        time.sleep(0.03 * (3 - len(order)))
        return [[float(len(t))] * 1 for t in batch]

    monkeypatch.setattr(embeddings, "_embed_batch", fake_embed_batch)
    cfg_o = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 1,
             "parallel": 4}
    vecs = embeddings.embed(texts, cfg_o)
    assert vecs is not None and len(vecs) == 120
    # 入力順どおり（完了順ではない）＝各要素の値は元テキストの長さと一致する。
    for text, vector in zip(texts, vecs):
        assert vector == [float(len(text))]


def test_embed_serial_when_parallel_not_configured(monkeypatch):
    """`c` に `parallel` が無い（省略時=1）呼び出し元は今までどおり直列（既存契約の回帰なし）。"""
    texts = [f"t{i}" for i in range(120)]
    concurrent_count = 0
    max_concurrent = 0
    lock = threading.Lock()

    def fake_embed_batch(batch, c):
        nonlocal concurrent_count, max_concurrent
        with lock:
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
        try:
            return [[1.0] for _ in batch]
        finally:
            with lock:
                concurrent_count -= 1

    monkeypatch.setattr(embeddings, "_embed_batch", fake_embed_batch)
    cfg_o = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 1}
    vecs = embeddings.embed(texts, cfg_o)
    assert vecs is not None and len(vecs) == 120
    assert max_concurrent == 1


# ===== 1バッチ失敗で None・未着手バッチは送信されない =====

def test_embed_batches_parallel_one_failure_returns_none_and_skips_unstarted(monkeypatch):
    """並列度2・10バッチ。最初の2バッチ（0/1）だけが確実に「開始」し、それ以外（2〜9）は
    ワーカーが埋まっている間は絶対にデキューされない（開始判定を `threading.Event` で
    ハンドシェイクし、実時間の偶然に頼らない）。開始を確認した後に batch0 を失敗させ、
    `embed()` が None を返すこと・未着手側の開始フラグが立たない（許容: 失敗検知と
    cancel の間の極小な競合で高々1件のみ）ことを確認する。
    """
    started = {i: threading.Event() for i in range(10)}
    go_fail = threading.Event()
    release_b1 = threading.Event()
    release_others = threading.Event()   # 万一 2〜9 が競り勝って開始しても、即完了させず塞いだまま保つ

    def fake_embed_batch(batch, c):
        idx = batch[0]
        started[idx].set()
        if idx == 0:
            go_fail.wait(3)
            return None   # 形が壊れている＝embed() 契約上の失敗
        if idx == 1:
            release_b1.wait(3)
            return [[1.0]]
        # 未着手であるべきバッチ。開始してしまっても即完了させない＝連鎖的に
        # 残り全部を巻き込まない（started フラグだけを観測できれば十分）。
        release_others.wait(3)
        return [[1.0]]

    monkeypatch.setattr(embeddings, "_embed_batch", fake_embed_batch)
    batches = [([i], [i]) for i in range(10)]
    result_holder: dict = {}

    def run():
        result_holder["result"] = embeddings._embed_batches_parallel(
            batches, {"provider": "openai"}, 2)

    t = threading.Thread(target=run)
    t.start()
    try:
        # 2ワーカーとも埋まる（batch0・batch1 が確実に開始）まで待つ＝この時点で
        # batch2〜9 は絶対に未着手（ワーカーが2つとも塞がっているため）。
        assert started[0].wait(5)
        assert started[1].wait(5)
        for i in range(2, 10):
            assert not started[i].is_set(), f"batch{i} started before any worker freed"
        # batch0 だけを失敗させる（batch1・2〜9 はまだ解放しない＝cancel が効く猶予を作る）。
        go_fail.set()
        time.sleep(0.05)
        # cancel と「たまたま空いたワーカーが次を掴む」瞬間の競合は原理的に排除できないため、
        # 高々1件の紛れ込みだけ許容する（0件が大半・regressionが起きれば全件開始してしまい検出できる）。
        straggler_count = sum(1 for i in range(2, 10) if started[i].is_set())
        assert straggler_count <= 1, f"too many unstarted batches were started: {straggler_count}"
    finally:
        release_b1.set()
        release_others.set()
        t.join(timeout=5)

    assert not t.is_alive()
    assert result_holder["result"] is None


def test_embed_returns_none_when_any_parallel_batch_shape_invalid(monkeypatch):
    texts = [f"t{i}" for i in range(120)]

    def fake_embed_batch(batch, c):
        if batch[0].endswith("0"):
            return None
        return [[1.0] for _ in batch]

    monkeypatch.setattr(embeddings, "_embed_batch", fake_embed_batch)
    cfg_o = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 1,
             "parallel": 4}
    assert embeddings.embed(texts, cfg_o) is None


# ===== 429 + Retry-After 後に成功・calls は物理送信数 =====

def test_embed_batch_retries_on_429_with_retry_after_then_succeeds(monkeypatch):
    attempts = []
    headers = email.message.Message()
    headers["Retry-After"] = "0"   # 実待ちゼロ（_sleep もモックしてテストを遅くしない）

    def fake_post(url, hdrs, body, timeout):
        attempts.append(1)
        if len(attempts) == 1:
            raise _http_error(429, headers)
        n = len(body["input"])
        return {"data": [{"embedding": [1.0]} for _ in range(n)], "usage": {"prompt_tokens": n}}

    slept = []
    monkeypatch.setattr(embeddings, "_sleep", lambda s: slept.append(s))
    monkeypatch.setattr(llm, "post_json", fake_post)
    c = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 1}
    out = embeddings._embed_batch(["a"], c)
    assert out == [[1.0]]
    assert len(attempts) == 2   # 初回失敗＋再送1回で成功
    assert slept == [0]         # Retry-After（0秒）を尊重・指数バックオフ表は使っていない


def test_embed_records_calls_as_physical_sends_after_retry(monkeypatch):
    """`metering.record('embed', ..., calls=n)` は物理送信数（再送を含む）のまま
    （STAT-3 の calls 契約・`embeddings.embed` docstring 参照）。"""
    attempts = []

    def fake_post(url, hdrs, body, timeout):
        attempts.append(1)
        if len(attempts) == 1:
            raise _http_error(500)
        n = len(body["input"])
        return {"data": [{"embedding": [1.0]} for _ in range(n)], "usage": {"prompt_tokens": n}}

    monkeypatch.setattr(embeddings, "_sleep", lambda s: None)
    monkeypatch.setattr(llm, "post_json", fake_post)
    _enable(monkeypatch)
    calls = _spy(monkeypatch)
    c = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 1}
    vecs = embeddings.embed(["a"], c)
    assert vecs == [[1.0]]
    assert len(calls) == 1
    assert calls[0]["calls"] == 2   # 初回失敗＋再送1回＝物理送信2回


def test_embed_batch_does_not_retry_non_retryable_4xx(monkeypatch):
    attempts = []

    def fake_post(url, hdrs, body, timeout):
        attempts.append(1)
        raise _http_error(401)

    slept = []
    monkeypatch.setattr(embeddings, "_sleep", lambda s: slept.append(s))
    monkeypatch.setattr(llm, "post_json", fake_post)
    c = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 1}
    assert embeddings._embed_batch(["a"], c) is None
    assert len(attempts) == 1
    assert slept == []


def test_embed_batch_gives_up_after_max_retries(monkeypatch):
    attempts = []

    def fake_post(url, hdrs, body, timeout):
        attempts.append(1)
        raise _http_error(503)

    slept = []
    monkeypatch.setattr(embeddings, "_sleep", lambda s: slept.append(s))
    monkeypatch.setattr(llm, "post_json", fake_post)
    c = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 1}
    assert embeddings._embed_batch(["a"], c) is None
    # 初回送信＋最大5回再送＝合計6回。バックオフ表（1,2,4,8,16）どおりに5回 sleep する。
    assert len(attempts) == 6
    assert slept == [1, 2, 4, 8, 16]


def test_embed_batch_stops_retrying_once_total_budget_exceeded(monkeypatch):
    """合計 300 秒を超える待機は、最大再送回数に達していなくても None で打ち切る
    （`time.monotonic` を差し替えた偽 clock で、実際の壁時計は進めずに検証する）。"""
    attempts = []

    def fake_post(url, hdrs, body, timeout):
        attempts.append(1)
        raise _http_error(429)

    # start=0（ループ開始時の1回）→ 1回目失敗時の経過=295秒（+backoff 1秒=296秒・300秒以内な
    # ので再送）→ 2回目送信前の残り時間確認=296秒（残り4秒＝timeout 4 で送る）→ 2回目失敗時の
    # 経過=400秒（+backoff 2秒=402秒・300秒を超えるため打ち切り）。
    monotonic_values = iter([0.0, 295.0, 296.0, 400.0])
    monkeypatch.setattr(embeddings.time, "monotonic", lambda: next(monotonic_values, 400.0))
    slept = []
    monkeypatch.setattr(embeddings, "_sleep", lambda s: slept.append(s))
    monkeypatch.setattr(llm, "post_json", fake_post)
    c = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 1}
    assert embeddings._embed_batch(["a"], c) is None
    assert len(attempts) == 2
    assert slept == [1]   # 1回目は再送（1秒バックオフ）・2回目は合計秒数超過で再送せず打ち切り


# ===== ollama: no_proxy_requests の ContextVar がワーカースレッドへ伝播する =====

def test_ollama_no_proxy_propagates_to_parallel_worker(monkeypatch):
    seen_no_proxy = []

    def fake_post(url, hdrs, body, timeout):
        seen_no_proxy.append(llm._no_proxy_ctx.get())
        n = len(body["input"])
        return {"embeddings": [[1.0] for _ in range(n)]}

    monkeypatch.setattr(llm, "post_json", fake_post)
    texts = [f"t{i}" for i in range(120)]   # 3バッチ（_BATCH=50）
    c = {"provider": "ollama", "url": "http://127.0.0.1:11434", "model": "nomic-embed-text",
         "dim": 1, "parallel": 4}
    vecs = embeddings.embed(texts, c)
    assert vecs is not None and len(vecs) == 120
    assert len(seen_no_proxy) == 3
    assert all(seen_no_proxy)   # 全ワーカーで no_proxy_requests() の効果が見えている


def test_context_copy_run_used_for_workers(monkeypatch):
    """ワーカーは `contextvars.copy_context().run()` 経由で起動する（グローバル状態ではなく
    ContextVar 伝播であることの直接固定）。呼び出し元スレッドで立てた ContextVar が
    ワーカースレッド内でも見える。"""
    probe: contextvars.ContextVar[str] = contextvars.ContextVar("probe", default="absent")
    seen = []

    def fake_embed_batch(batch, c):
        seen.append(probe.get())
        return [[1.0] for _ in batch]

    monkeypatch.setattr(embeddings, "_embed_batch", fake_embed_batch)
    texts = [f"t{i}" for i in range(120)]
    c = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 1,
         "parallel": 4}
    token = probe.set("present")
    try:
        vecs = embeddings.embed(texts, c)
    finally:
        probe.reset(token)
    assert vecs is not None
    assert seen == ["present"] * 3


def test_parallel_early_break_still_records_calls_of_inflight_batches(monkeypatch):
    """batch0 が即失敗して打ち切っても、実行中だった batch1 は shutdown 中に完了する。
    その物理送信/トークンが記録から欠落してはならない（費用が実態より少なく見える）。"""
    from sherpa import metering
    started1 = threading.Event()
    release = threading.Event()

    def fake_embed_batch(batch, c):
        if batch[0] == 0:
            started1.wait(3)          # batch1 が確実に「実行中」になってから失敗する
            return None
        started1.set()
        release.wait(3)
        metering.acc_add({"input_tokens": 7, "output_tokens": 0})
        return [[1.0]]

    monkeypatch.setattr(embeddings, "_embed_batch", fake_embed_batch)
    # 合算先の acc frame は呼び出しスレッド専有＝このスレッドで begin/end し、同じスレッドで呼ぶ。
    threading.Timer(0.2, release.set).start()
    metering.acc_begin()
    try:
        r = embeddings._embed_batches_parallel([([0], [0]), ([1], [1])], {"provider": "openai"}, 2)
    finally:
        tokens, calls = metering.acc_end()
    assert r is None
    assert calls == 1 and tokens and tokens.get("input_tokens") == 7


def test_retry_bounds_http_timeout_by_remaining_total_budget(monkeypatch):
    """各送信が長く掛かって 503 を返し続けると、待ち時間だけ数えていては合計 300 秒を超える。
    再送時の HTTP timeout は残り時間以下に絞り、残りが無ければ送らない。"""
    import urllib.error
    clock = {"t": 0.0}
    seen_timeouts: list = []

    def fake_once(texts, c, timeout=embeddings._TIMEOUT):
        seen_timeouts.append(timeout)
        clock["t"] += 50.0            # 応答に 50 秒掛かってから 503
        raise urllib.error.HTTPError("u", 503, "x", None, None)

    monkeypatch.setattr(embeddings, "_embed_batch_once", fake_once)
    monkeypatch.setattr(embeddings, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(embeddings.time, "monotonic", lambda: clock["t"])
    assert embeddings._embed_batch(["a"], {"provider": "openai"}) is None
    assert seen_timeouts[0] == embeddings._TIMEOUT
    # 送信開始時刻＋timeout が 300 秒を超える試行が無い
    elapsed = 0.0
    for i, to in enumerate(seen_timeouts):
        if i:
            assert elapsed + to <= embeddings._RETRY_MAX_TOTAL_SECONDS + 1e-6, (i, elapsed, to)
        elapsed += 50.0 + (embeddings._RETRY_BACKOFF_SCHEDULE[min(i, 4)] if i < len(seen_timeouts) - 1 else 0)
    assert clock["t"] <= embeddings._RETRY_MAX_TOTAL_SECONDS + embeddings._TIMEOUT


def test_embed_counts_timed_out_send_as_physical_call(monkeypatch):
    """応答待ちで timeout した要求も送信は行われている＝再送で成功したとき calls は 2。"""
    import socket
    attempts = []

    def fake_post(url, hdrs, body, timeout):
        attempts.append(1)
        if len(attempts) == 1:
            raise socket.timeout("timed out")
        return {"data": [{"index": 0, "embedding": [1.0]}], "usage": {"prompt_tokens": 1, "total_tokens": 1}}

    monkeypatch.setattr(embeddings, "_sleep", lambda s: None)
    monkeypatch.setattr(llm, "post_json", fake_post)
    _enable(monkeypatch)
    calls = _spy(monkeypatch)
    c = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 1}
    assert embeddings.embed(["a"], c) == [[1.0]]
    assert calls[0]["calls"] == 2


def test_retry_does_not_send_when_less_than_one_second_remains(monkeypatch):
    """残り 1 秒未満で timeout=1 に丸め上げると合計上限を超えるため、送らずに諦める。"""
    import urllib.error
    clock = {"t": 0.0}
    sent = []

    def fake_once(texts, c, timeout=embeddings._TIMEOUT):
        sent.append(timeout)
        clock["t"] += 53.76
        raise urllib.error.HTTPError("u", 503, "x", None, None)

    monkeypatch.setattr(embeddings, "_embed_batch_once", fake_once)
    monkeypatch.setattr(embeddings, "_sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(embeddings.time, "monotonic", lambda: clock["t"])
    assert embeddings._embed_batch(["a"], {"provider": "openai"}) is None
    # 各送信の開始時刻 + timeout が 300 秒を超えない
    t = 0.0
    for i, to in enumerate(sent):
        assert t + to <= embeddings._RETRY_MAX_TOTAL_SECONDS + 1e-6 or i == 0, (i, t, to)
        t += 53.76 + embeddings._RETRY_BACKOFF_SCHEDULE[min(i, 4)]


def test_broken_response_after_http_success_counts_one_call_and_no_retry(monkeypatch):
    """HTTP は成功したが応答の形が壊れている（embedding 欠落）とき、送信 1 回＝calls 1 で、
    再送もしない（再送しても直らない）。"""
    attempts = []

    def fake_post(url, hdrs, body, timeout):
        attempts.append(1)
        return {"data": [{"index": 0}], "usage": {"prompt_tokens": 1}}

    monkeypatch.setattr(embeddings, "_sleep", lambda s: None)
    monkeypatch.setattr(llm, "post_json", fake_post)
    _enable(monkeypatch)
    calls = _spy(monkeypatch)
    c = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 1}
    assert embeddings.embed(["a"], c) is None
    assert len(attempts) == 1
    assert calls[0]["calls"] == 1


def test_preflight_rejection_is_not_counted_as_a_send(monkeypatch):
    """接続先の検証で送信前に拒否された（urllib に到達していない）試行は物理送信ではない＝
    record('embed') を書かない。"""
    def fake_url(*a, **k):
        raise llm.PreflightRejected("blocked")
    monkeypatch.setattr(llm, "openai_url", fake_url)
    _enable(monkeypatch)
    calls = _spy(monkeypatch)
    c = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 1}
    assert embeddings.embed(["a"], c) is None
    assert calls == []
