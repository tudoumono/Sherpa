"""埋め込み API への `encoding_format: "base64"` 指定と、応答の base64 を `array('f')` に戻す処理の単体テスト。

モックは外部境界（`llm.post_json`）だけに置く。同じ値を float の JSON と base64 で返す偽の応答で、
後段（`to_vector`・`quantize_vector`）に渡る値が同じになることを確かめる。
"""
from __future__ import annotations

import base64
import io
import urllib.error
from array import array

import pytest

from sherpa import embeddings, llm, metering

_VALUES = [[0.25, -0.5, 0.75, 1.0], [1.5, 0.125, -2.0, 0.5]]  # float32 で丸めずに表せる値
_OPENAI = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 4}
_OLLAMA = {"provider": "ollama", "url": "http://localhost:11434", "model": "nomic-embed-text", "dim": 4}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    embeddings._float_only_urls.clear()
    embeddings._ollama_legacy_urls.clear()
    yield
    embeddings._float_only_urls.clear()
    embeddings._ollama_legacy_urls.clear()


def _b64(values) -> str:
    return base64.b64encode(embeddings.vector_to_bytes(array("f", values))).decode("ascii")


def _data(vectors, encode: bool, tokens: int = 7) -> dict:
    return {"data": [{"index": i, "embedding": _b64(v) if encode else list(v)} for i, v in enumerate(vectors)],
            "usage": {"prompt_tokens": tokens}}


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", None, io.BytesIO(b"{}"))


def _fake_server(monkeypatch, handler) -> list:
    sent: list = []

    def fake_post(url, headers, body, timeout=90):
        sent.append((url, body))
        return handler(url, body)

    monkeypatch.setattr(llm, "post_json", fake_post)
    return sent


def test_openai_sends_base64_and_returns_same_vectors_as_float_response(monkeypatch):
    sent = _fake_server(monkeypatch, lambda url, body: _data(_VALUES, encode=True))
    got_b64 = embeddings.embed(["a", "b"], _OPENAI)
    assert sent[0][1]["encoding_format"] == "base64"
    assert sent[0][1]["dimensions"] == 4

    _fake_server(monkeypatch, lambda url, body: _data(_VALUES, encode=False))
    got_float = embeddings.embed(["a", "b"], _OPENAI)

    assert all(isinstance(v, array) and v.typecode == "f" for v in got_b64)
    assert [embeddings.to_vector(v) for v in got_b64] == [embeddings.to_vector(v) for v in got_float]
    assert [embeddings.quantize_vector(v) for v in got_b64] == [embeddings.quantize_vector(v) for v in got_float]
    assert embeddings.quantize_vector(got_b64[0]) is not None


def test_pooled_long_text_is_same_for_base64_and_float(monkeypatch):
    long_text = "あ" * 5000  # UTF-8 で 15000 バイト＝2 window を平均する
    _fake_server(monkeypatch, lambda url, body: _data([_VALUES[0], _VALUES[1]][:len(body["input"])], encode=True))
    pooled_b64 = embeddings.embed([long_text], _OPENAI)
    _fake_server(monkeypatch, lambda url, body: _data([_VALUES[0], _VALUES[1]][:len(body["input"])], encode=False))
    pooled_float = embeddings.embed([long_text], _OPENAI)
    assert pooled_b64 is not None and pooled_b64 == pooled_float


def test_ollama_sends_base64_to_v1_embeddings_and_counts_prompt_tokens(monkeypatch):
    sent = _fake_server(monkeypatch, lambda url, body: _data(_VALUES, encode=True, tokens=11))
    metering.acc_begin()
    try:
        got = embeddings._embed_batch(["a", "b"], _OLLAMA)
    finally:
        tokens, calls = metering.acc_end()
    assert sent[0][0] == "http://localhost:11434/v1/embeddings"
    assert sent[0][1] == {"model": "nomic-embed-text", "input": ["a", "b"], "encoding_format": "base64"}
    assert got == [array("f", v) for v in _VALUES]
    assert calls == 1 and tokens["input_tokens"] == 11 and tokens["output_tokens"] == 0


def test_ollama_v1_404_falls_back_to_api_embed_once(monkeypatch, caplog):
    def handler(url, body):
        if url.endswith("/v1/embeddings"):
            raise _http_error(404)
        return {"embeddings": [list(v) for v in _VALUES], "prompt_eval_count": 13}

    sent = _fake_server(monkeypatch, handler)
    caplog.set_level("WARNING", logger="sherpa.embed")
    metering.acc_begin()
    try:
        got = embeddings._embed_batch(["a", "b"], _OLLAMA)
        again = embeddings._embed_batch(["a", "b"], _OLLAMA)
    finally:
        tokens, _calls = metering.acc_end()
    assert got == again == [list(v) for v in _VALUES]
    assert [u.rsplit("/", 2)[-2:] for u, _b in sent] == [["v1", "embeddings"], ["api", "embed"], ["api", "embed"]]
    assert sent[1][1] == {"model": "nomic-embed-text", "input": ["a", "b"]}  # /api/embed には encoding_format を付けない
    assert tokens["input_tokens"] == 26
    assert len([r for r in caplog.records if "/api/embed" in r.getMessage()]) == 1


def test_ollama_v1_and_api_embed_both_404_does_not_switch(monkeypatch):
    sent = _fake_server(monkeypatch, lambda url, body: (_ for _ in ()).throw(_http_error(404)))
    assert embeddings._embed_batch(["a"], _OLLAMA) is None
    assert embeddings._ollama_legacy_urls == set()
    embeddings._embed_batch(["a"], _OLLAMA)
    assert [u.endswith("/v1/embeddings") for u, _b in sent] == [True, False, True, False]


def test_openai_400_switches_to_float_and_stays_float(monkeypatch, caplog):
    def handler(url, body):
        if "encoding_format" in body:
            raise _http_error(400)
        return _data(_VALUES, encode=False)

    sent = _fake_server(monkeypatch, handler)
    caplog.set_level("WARNING", logger="sherpa.embed")
    first = embeddings.embed(["a", "b"], _OPENAI)
    second = embeddings.embed(["a", "b"], _OPENAI)
    assert first == second == [list(v) for v in _VALUES]
    assert ["encoding_format" in b for _u, b in sent] == [True, False, False]
    assert len([r for r in caplog.records if "encoding_format" in r.getMessage()]) == 1


def test_400_that_also_fails_without_encoding_format_does_not_switch(monkeypatch):
    sent = _fake_server(monkeypatch, lambda url, body: (_ for _ in ()).throw(_http_error(400)))
    assert embeddings.embed(["a"], _OPENAI) is None
    assert embeddings._float_only_urls == set()
    assert ["encoding_format" in b for _u, b in sent] == [True, False]


def test_non_400_error_is_not_a_switch(monkeypatch):
    def handler(url, body):
        raise _http_error(401)

    sent = _fake_server(monkeypatch, handler)
    assert embeddings.embed(["a"], _OPENAI) is None
    assert len(sent) == 1 and embeddings._float_only_urls == set()


def _nan_b64() -> str:
    return base64.b64encode(array("f", [float("nan"), 1.0, 1.0, 1.0]).tobytes()).decode("ascii")


@pytest.mark.parametrize("broken", [
    "not base64!!",                               # base64 として読めない
    base64.b64encode(b"\x00" * 12).decode(),      # 長さが次元×4（16 バイト）でない
    base64.b64encode(b"\x00" * 20).decode(),
    _nan_b64(),                                   # 有限でない値を含む
    "",                                           # 空
])
def test_broken_base64_is_treated_as_not_obtained(monkeypatch, broken):
    _fake_server(monkeypatch, lambda url, body: {"data": [{"index": 0, "embedding": broken}],
                                                 "usage": {"prompt_tokens": 1}})
    assert embeddings._embed_batch(["a"], _OPENAI) is None
    assert embeddings.embed(["a"], _OPENAI) is None


def test_mixed_formats_in_response_are_accepted(monkeypatch):
    _fake_server(monkeypatch, lambda url, body: {"data": [{"index": 0, "embedding": _b64(_VALUES[0])},
                                                          {"index": 1, "embedding": list(_VALUES[1])}]})
    got = embeddings.embed(["a", "b"], _OPENAI)
    assert got is not None and embeddings.to_vector(got[0]) == array("f", _VALUES[0])
    assert embeddings.to_vector(got[1]) == array("f", _VALUES[1])



def test_reach_with_unknown_dim_accepts_base64_response(monkeypatch):
    _fake_server(monkeypatch, lambda url, body: _data(_VALUES, True))
    vecs, failure = embeddings.reach_with_failure(["a", "b"], {**_OLLAMA, "dim": None})
    assert failure is None and [len(v) for v in vecs] == [4, 4]


@pytest.mark.parametrize("values", [[0.0, 0.0, 0.0, 0.0], [1.0, float("nan"), 1.0, 1.0]])
def test_reach_with_unknown_dim_rejects_zero_or_non_finite_vector(monkeypatch, values):
    _fake_server(monkeypatch, lambda url, body: _data([values], False if values[1] != values[1] else True))
    vecs, failure = embeddings.reach_with_failure(["a"], {**_OLLAMA, "dim": None})
    assert vecs is None and failure is not None and failure.kind == "invalid"
