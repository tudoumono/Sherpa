"""埋め込みの 1 回の要求の待ち時間・件数の設定と、待ち時間切れが続いたときの件数の自動調整。

境目は HTTP の送信だけ。遅い Ollama は、試験の中で小さい HTTP サーバーを立てて真似る（1 件あたり一定の時間をかけて返す）。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sherpa import connectivity, embeddings

_PER_ITEM_S = 0.3
_DIM = 4


class _SlowOllama:
    """`/v1/embeddings` に、入力 1 件あたり `per_item` 秒かけて答える。受けた入力の件数を順に記録する。"""

    def __init__(self, per_item: float = _PER_ITEM_S):
        self.sizes: list = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                n = len(body["input"])
                outer.sizes.append(n)
                time.sleep(per_item * n)
                out = json.dumps({"data": [{"index": i, "embedding": [0.5] * _DIM} for i in range(n)]}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except OSError:
                    pass   # 待ちきれずに切った相手

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def slow():
    s = _SlowOllama()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    monkeypatch.setattr(embeddings, "_RETRY_MAX_RETRIES", 1)
    monkeypatch.setattr(embeddings, "_sleep", lambda s: None)
    embeddings._ollama_dim_cache.clear()
    embeddings._ollama_dim_failed.clear()


def _ec(url, **kw):
    return {"provider": "ollama", "url": url, "model": "m", "dim": _DIM, "parallel": 1, **kw}


def test_timeouts_halve_the_batch_and_the_rest_of_the_round_starts_small(slow):
    ec = _ec(slow.url, timeout_s=1, batch=8)
    got = embeddings.embed([f"t{i}" for i in range(8)], ec)
    assert got is not None and len(got) == 8
    assert slow.sizes[:3] == [8, 4, 2]        # 8 件・4 件は 1 秒に間に合わず、2 件で通る
    slow.sizes.clear()
    assert embeddings.embed([f"u{i}" for i in range(8)], ec) is not None
    assert max(slow.sizes) <= 2                # 同じ回の続きは、下げた件数から始める


def test_single_item_that_times_out_gives_up_as_before(slow):
    ec = _ec(slow.url, timeout_s=1, batch=1)
    slow2 = _SlowOllama(per_item=1.6)
    try:
        ec["url"] = slow2.url
        vecs, failure = embeddings.embed_with_failure(["a", "b"], {**ec, "batch": 2})
        assert vecs is None and failure is not None and failure.kind == "timeout"
        assert slow2.sizes == [2, 1, 1]   # 2 件は再送せず 1 件に下げ、1 件は今までどおり 1 回再送して諦める
    finally:
        slow2.close()


def test_non_timeout_failures_do_not_shrink(slow, monkeypatch):
    from sherpa import llm
    calls = []

    def boom(url, headers, body, timeout):
        calls.append(len(body["input"]))
        raise ValueError("bad")
    monkeypatch.setattr(llm, "post_json", boom)
    assert embeddings.embed([f"t{i}" for i in range(8)], _ec(slow.url, batch=8)) is None
    assert calls == [8]


def test_dimension_probe_waits_as_long_as_the_request_timeout(slow):
    assert embeddings.ollama_embed_dim(slow.url, "m", timeout_s=2) is not None   # 1 件 0.3 秒
    slow_big = _SlowOllama(per_item=3)
    try:
        embeddings._ollama_dim_failed.clear()
        assert embeddings.ollama_embed_dim(slow_big.url, "m", timeout_s=2) is None
        embeddings._ollama_dim_failed.clear()
        assert embeddings.ollama_embed_dim(slow_big.url, "m", timeout_s=6) == _DIM
    finally:
        slow_big.close()


def test_connectivity_says_why_the_dimension_could_not_be_measured():
    f = embeddings.EmbedFailure("timeout", error_type="TimeoutError")
    assert "時間切れ" in connectivity._embed_failure_reason(f, None, 60) and "60 秒" in connectivity._embed_failure_reason(f, None, 60)


def test_cfg_carries_timeout_and_batch_only_when_not_default(monkeypatch):
    monkeypatch.setattr(embeddings, "ollama_embed_dim", lambda url, model, **kw: kw["timeout_s"])
    base = {"embed_provider": "ollama", "ollama_url": "http://localhost:11434"}
    ec = embeddings.cfg(None, system_settings=base)
    assert "timeout_s" not in ec and "batch" not in ec and ec["dim"] == 60
    ec = embeddings.cfg(None, system_settings={**base, "embed_request_timeout_s": 240, "embed_batch_size": 8})
    assert ec["timeout_s"] == 240 and ec["batch"] == 8 and ec["dim"] == 240     # 次元の測定にも同じ待ち時間


@pytest.mark.parametrize("raw, want", [(None, 60), (10, 10), (600, 600), (9, 60), (601, 60), (True, 60), ("30", 60), (1.5, 60)])
def test_effective_timeout_clamps_to_default(raw, want):
    assert embeddings.effective_embed_request_timeout({} if raw is None else {"embed_request_timeout_s": raw}) == want


@pytest.mark.parametrize("raw, want", [(None, 50), (1, 1), (200, 200), (0, 50), (201, 50), (True, 50), ("8", 50)])
def test_effective_batch_size_clamps_to_default(raw, want):
    assert embeddings.effective_embed_batch_size({} if raw is None else {"embed_batch_size": raw}) == want


def test_window_batches_respect_the_count_limit():
    sizes = [len(b) for _o, b in embeddings._window_batches([f"t{i}" for i in range(10)], 4)]
    assert sizes == [4, 4, 2]


def test_first_request_gets_the_whole_configured_wait_in_a_deadline_call(monkeypatch):
    """総期限つきの呼び出し（次元の測定・つながりの確認）でも、最初の要求は設定の待ち時間をまるごと使える。"""
    seen = []
    monkeypatch.setattr(embeddings, "_embed_batch_once", lambda texts, c, timeout=60: seen.append(timeout) or [[0.5] * 4])
    ec = {"provider": "ollama", "url": "http://x", "model": "m", "dim": 4, "timeout_s": 600}
    assert embeddings._embed_batch(["a"], ec, time.monotonic() + 600) is not None
    assert seen[0] >= 599


def test_ingest_retry_total_is_unchanged_without_a_deadline(monkeypatch):
    seen = []
    monkeypatch.setattr(embeddings, "_embed_batch_once", lambda texts, c, timeout=60: seen.append(timeout) or [[0.5] * 4])
    ec = {"provider": "ollama", "url": "http://x", "model": "m", "dim": 4, "timeout_s": 600}
    assert embeddings._embed_batch(["a"], ec) is not None
    assert seen == [600]
