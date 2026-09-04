"""計測サイト（消費側）の単体テスト（S1・2026-07-15-LLMオーケストレーション実装計画.md §3.4）。

各サイトが `metering.acc_begin/acc_add/acc_end` を正しく使い、`store.add_usage_event`
（ここでは `sherpa.store.usage_events.add_usage_event` をスパイ）へ1行記録すること、
LLM へのリクエストボディは記録の有無に関わらず変わらないことを確認する。

DB 不要（`sherpa.store.usage_events.add_usage_event` 自体をスパイに差し替えるため、実 Postgres を
一切叩かない）。tests/unit/conftest.py の autouse fixture `_hermetic_metering_record` が既定で
`metering.record` を no-op に固定する（TOGGLE-RM・2026-09-03 で計測は常時ONへ固定されたため、
実 DB 書き込みを防ぐ唯一の防御がこの autouse fixture になった）。実際に記録されることを検証する
各テストは `_enable()` で本物の `metering.record` へ明示的に戻す。
"""
from __future__ import annotations

import pathlib
import tempfile
import types
from types import SimpleNamespace

from sherpa import agentic_search, embeddings, graph_admin, intent_llm as I, llm, metering
from sherpa.ingest import graph_extract as GE
from sherpa.ingest.arms import vision_arm
from sherpa.store import usage_events as ue


_real_metering_record = metering.record


def _enable(monkeypatch):
    from sherpa import store
    # personal_api_keys_allowed も維持する（hermetic fixture の既定を上書きしてしまうため）。
    # これが無いと `sherpa.keys.resolve_api_key` が settings dict のインラインキーを無視して None
    # を返し、ask_graph 系テストが軒並み llm_unavailable に落ちる。
    monkeypatch.setattr(store, "get_system_settings",
                        lambda **kw: {"personal_api_keys_allowed": True})
    # conftest.py::_hermetic_metering_record（autouse）が no-op にした `metering.record` を、
    # 捕捉しておいた本物の関数オブジェクトへ明示的に戻す（TOGGLE-RM・2026-09-03 で計測は常時ON
    # のため、以前の「system_settings で有効化する」という意味の関数名は残すが中身はこれだけ）。
    monkeypatch.setattr(metering, "record", _real_metering_record)


def _spy(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ue, "add_usage_event", lambda **kw: calls.append(kw))
    return calls


# ---- complete_json（graph_extract の全プロバイダ分岐）----

def test_complete_json_feeds_acc_per_provider(monkeypatch):
    # openai
    monkeypatch.setattr(llm, "post_json", lambda *a, **k: {
        "choices": [{"message": {"content": '{"ok":true}'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "prompt_tokens_details": {"cached_tokens": 2},
                  "completion_tokens_details": {"reasoning_tokens": 1}}})
    cfg_o = {"provider": "openai", "key": "k", "model": "gpt-5.5"}
    metering.acc_begin()
    text = GE.complete_json("s", "u", cfg_o)
    tokens, n = metering.acc_end()
    assert text == '{"ok":true}'
    assert n == 1
    assert tokens == {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5,
                      "reasoning_output_tokens": 1}
    # accスコープを開かない場合も同じ呼び出しが成功し、返り値のテキストは不変（probe 経路の no-op 保証）。
    text2 = GE.complete_json("s", "u", cfg_o)
    assert text2 == text

    # gemini
    monkeypatch.setattr(llm, "post_json", lambda *a, **k: {
        "candidates": [{"content": {"parts": [{"text": '{"ok":true}'}]}}],
        "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3,
                          "cachedContentTokenCount": 1, "thoughtsTokenCount": 2}})
    metering.acc_begin()
    GE.complete_json("s", "u", {"provider": "gemini", "key": "k", "model": "gemini-2.5-flash"})
    tokens, n = metering.acc_end()
    assert n == 1
    assert tokens == {"input_tokens": 7, "cached_input_tokens": 1, "output_tokens": 3,
                      "reasoning_output_tokens": 2}

    # ollama
    monkeypatch.setattr(llm, "post_json", lambda *a, **k: {
        "message": {"content": '{"ok":true}'}, "prompt_eval_count": 4, "eval_count": 2})
    metering.acc_begin()
    GE.complete_json("s", "u", {"provider": "ollama", "url": "http://localhost:11434", "model": "qwen2.5"})
    tokens, n = metering.acc_end()
    assert n == 1
    assert tokens == {"input_tokens": 4, "cached_input_tokens": 0, "output_tokens": 2,
                      "reasoning_output_tokens": 0}

    # bedrock（fake AnthropicBedrock クライアント・実 AWS は叩かない）
    import anthropic

    class _FakeResp:
        def __init__(self):
            self.content = [SimpleNamespace(type="text", text="ok")]
            self.usage = SimpleNamespace(input_tokens=9, cache_read_input_tokens=1,
                                         cache_creation_input_tokens=0, output_tokens=4)

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeResp()

    class _FakeClient:
        def __init__(self, **kwargs):
            self.messages = _FakeMessages()

    monkeypatch.setattr(anthropic, "AnthropicBedrock", _FakeClient)
    metering.acc_begin()
    out = GE.complete_json("s", "u", {"provider": "bedrock", "region": "ap-northeast-1",
                                      "model": "anthropic.claude-opus-4-8", "api_key": "k"})
    tokens, n = metering.acc_end()
    assert out == "ok"
    assert n == 1
    assert tokens == {"input_tokens": 10, "cached_input_tokens": 1, "output_tokens": 4,
                      "reasoning_output_tokens": 0}


# ---- intent_llm.classify ----

def test_intent_classify_records(monkeypatch):
    monkeypatch.setattr(I, "_cfg", lambda settings, **kw: {"provider": "openai", "key": "k", "model": "gpt-4o-mini"})
    monkeypatch.setattr(llm, "post_json", lambda *a, **k: {
        "choices": [{"message": {"content": '{"lens":"qa","confident":true}'}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 4}})
    _enable(monkeypatch)
    calls = _spy(monkeypatch)
    r = I.classify("消費税率は？", {}, user_id="u1", world="v1")
    assert r == {"lens": "qa", "confident": True}
    assert len(calls) == 1
    c = calls[0]
    assert c["kind"] == "intent" and c["provider"] == "openai" and c["model"] == "gpt-4o-mini"
    assert c["input_tokens"] == 20 and c["output_tokens"] == 4
    assert c["user_id"] == "u1" and c["world"] == "v1" and c["calls"] == 1


def test_intent_classify_records_even_on_json_parse_failure(monkeypatch):
    monkeypatch.setattr(I, "_cfg", lambda settings, **kw: {"provider": "openai", "key": "k", "model": "gpt-4o-mini"})
    monkeypatch.setattr(llm, "post_json", lambda *a, **k: {
        "choices": [{"message": {"content": "not json"}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 1}})
    _enable(monkeypatch)
    calls = _spy(monkeypatch)
    assert I.classify("なにか", {}) is None
    assert len(calls) == 1 and calls[0]["input_tokens"] == 8


def test_intent_classify_records_nothing_when_cfg_none(monkeypatch):
    monkeypatch.setattr(I, "_cfg", lambda settings, **kw: None)
    _enable(monkeypatch)
    calls = _spy(monkeypatch)
    assert I.classify("なにか", {}) is None
    assert calls == []


# ---- embeddings.embed ----

def test_embed_records_and_gemini_null_marker(monkeypatch):
    texts = [f"t{i}" for i in range(120)]   # _BATCH=50 → 50/50/20 の3バッチ

    # openai: usage.prompt_tokens の合計・output=0。
    def _openai_post(url, headers, body, timeout):
        n = len(body["input"])
        return {"data": [{"embedding": [1.0] * 8} for _ in range(n)], "usage": {"prompt_tokens": n * 3}}

    monkeypatch.setattr(llm, "post_json", _openai_post)
    _enable(monkeypatch)
    calls = _spy(monkeypatch)
    cfg_o = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 8}
    vecs = embeddings.embed(texts, cfg_o, world="v1")
    assert vecs is not None and len(vecs) == 120
    assert len(calls) == 1
    c = calls[0]
    assert c["kind"] == "embed" and c["calls"] == 3
    assert c["input_tokens"] == 50 * 3 + 50 * 3 + 20 * 3
    assert c["output_tokens"] == 0
    assert c["world"] == "v1" and c["user_id"] is None
    calls.clear()

    # gemini: batchEmbedContents に usage フィールドが無い＝報告不能マーカー（全 None）。
    def _gemini_post(url, headers, body, timeout):
        n = len(body["requests"])
        return {"embeddings": [{"values": [1.0] * 8} for _ in range(n)]}

    monkeypatch.setattr(llm, "post_json", _gemini_post)
    cfg_g = {"provider": "gemini", "key": "k", "model": "gemini-embedding-001", "dim": 8}
    vecs = embeddings.embed(texts, cfg_g)
    assert vecs is not None and len(vecs) == 120
    assert len(calls) == 1
    c = calls[0]
    assert c["kind"] == "embed" and c["calls"] == 3
    assert c["input_tokens"] is None and c["cached_input_tokens"] is None
    assert c["output_tokens"] is None and c["reasoning_output_tokens"] is None


def test_embed_metering_noop_does_not_affect_vectors_or_request(monkeypatch):
    """計測が記録しない（ここでは conftest の autouse `_hermetic_metering_record` が既定で
    `metering.record` を no-op にしたまま・`_enable()` で実体を戻していない）状態でも、
    埋め込み呼び出し自体の結果・リクエストボディは一切変わらない（計測は横から観測するだけの
    ラッパーであり、TOGGLE-RM 後の「常時記録」であっても呼び出し元の挙動を変えない契約）。"""
    def _openai_post(url, headers, body, timeout):
        n = len(body["input"])
        return {"data": [{"embedding": [1.0] * 8} for _ in range(n)], "usage": {"prompt_tokens": n}}

    monkeypatch.setattr(llm, "post_json", _openai_post)
    calls = _spy(monkeypatch)
    cfg_o = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 8}
    vecs = embeddings.embed(["a", "b"], cfg_o)
    assert vecs == [[1.0] * 8, [1.0] * 8]
    assert calls == []


def test_openai_embed_restores_input_order_from_response_indices(monkeypatch):
    def _openai_post(url, headers, body, timeout):
        assert body["input"] == ["first", "second"]
        return {
            "data": [
                {"index": 1, "embedding": [2.0, 2.0]},
                {"index": 0, "embedding": [1.0, 1.0]},
            ],
            "usage": {"prompt_tokens": 2},
        }

    monkeypatch.setattr(llm, "post_json", _openai_post)
    config = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 2}

    assert embeddings.embed(["first", "second"], config) == [[1.0, 1.0], [2.0, 2.0]]


# ---- graph_admin.ask_graph ----

def test_ask_graph_stops_discarding_usage(monkeypatch):
    events = [
        {"node": "graph_neighbors"},
        {"final": "ans", "docs": [], "cards": [{"name": "TAX-RATE", "label": "Parameter"}],
         "searched": True, "usage": {"input_tokens": 30, "cached_input_tokens": 0,
                                     "output_tokens": 10, "reasoning_output_tokens": 0}},
    ]
    monkeypatch.setattr(agentic_search, "openai_style", lambda *a, **k: events)
    _enable(monkeypatch)
    calls = _spy(monkeypatch)
    s = {"agent": "openai", "openai_api_key": "k", "openai_model": "gpt-5.5"}
    res = graph_admin.ask_graph("消費税率は？", "v1", settings=s, user_id="u1")
    assert res["status"] == "ok"
    assert len(calls) == 1
    c = calls[0]
    assert c["kind"] == "graph_ask" and c["provider"] == "openai" and c["model"] == "gpt-5.5"
    assert c["input_tokens"] == 30 and c["output_tokens"] == 10
    assert c["user_id"] == "u1" and c["world"] == "v1"


def test_ask_graph_not_searched_path_records_nothing(monkeypatch):
    """not-searched（graph tool was not used）raise パスは、usage 付きの final イベントごと握りつぶす
    （既知の限界・S1 スコープ外）。except → status='failed' に落ち、record() は呼ばれない。"""
    events = [{"final": "ans", "docs": [], "cards": [], "searched": False,
              "usage": {"input_tokens": 5, "output_tokens": 1}}]
    monkeypatch.setattr(agentic_search, "openai_style", lambda *a, **k: events)
    _enable(monkeypatch)
    calls = _spy(monkeypatch)
    s = {"agent": "openai", "openai_api_key": "k"}
    res = graph_admin.ask_graph("なにか", "v1", settings=s)
    assert res["status"] == "failed"
    assert calls == []


def test_ask_graph_llm_unavailable_records_nothing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)   # 開発機の実 env キーに左右されない（settings 側も鍵無し）
    _enable(monkeypatch)
    calls = _spy(monkeypatch)
    res = graph_admin.ask_graph("なにか", "v1", settings={"agent": "openai"})   # key 無し
    assert res["status"] == "llm_unavailable"
    assert calls == []


# ---- vision_arm.VisionArm.convert（VLM）----

def _img(dirpath, name="scan.png"):
    p = pathlib.Path(dirpath) / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n dummy raster bytes")
    return p


def test_vlm_convert_records_aggregated(monkeypatch):
    monkeypatch.setenv("SHERPA_ARMS", "ooxml,pdf_text,vision")
    monkeypatch.setattr(llm, "post_json", lambda url, headers, body, timeout=90: {
        "message": {"content": "スキャン本文"}, "prompt_eval_count": 12, "eval_count": 6})
    _enable(monkeypatch)
    calls = _spy(monkeypatch)
    d = tempfile.mkdtemp()
    res = vision_arm.VisionArm().convert(_img(d))
    assert res is not None
    assert len(calls) == 1
    c = calls[0]
    assert c["kind"] == "vlm" and c["provider"] == "ollama" and c["calls"] == 1
    assert c["input_tokens"] == 12 and c["output_tokens"] == 6
    assert c["user_id"] is None and c["world"] is None


def test_vlm_convert_pdf_records_calls_equal_pages(monkeypatch):
    from sherpa.ingest import office_md

    monkeypatch.setenv("SHERPA_ARMS", "ooxml,pdf_text,vision")
    monkeypatch.setattr(llm, "post_json", lambda url, headers, body, timeout=90: {
        "message": {"content": "ページ本文"}, "prompt_eval_count": 5, "eval_count": 2})

    fake = types.ModuleType("pypdfium2")

    class _Bitmap:
        def to_pil(self):
            from PIL import Image
            return Image.new("RGB", (8, 6), (20, 40, 60))

        def close(self):
            return None

    class _Page:
        def get_size(self):
            return 612.0, 792.0

        def render(self, *, scale):
            return _Bitmap()

        def close(self):
            return None

    class _Doc:
        def __len__(self):
            return 2

        def __getitem__(self, i):
            return _Page()

        def close(self):
            return None

    fake.PdfDocument = lambda path: _Doc()
    import sys
    monkeypatch.setitem(sys.modules, "pypdfium2", fake)

    _enable(monkeypatch)
    calls = _spy(monkeypatch)
    d = tempfile.mkdtemp()
    pdf = pathlib.Path(d) / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")
    o_b, o_p = office_md._pdf_backend, office_md._pdf_pages
    try:
        office_md._pdf_backend = lambda: "pypdf"
        office_md._pdf_pages = lambda p: ["", ""]   # テキスト層ゼロ＝vision 担当
        res = vision_arm.VisionArm().convert(pdf)
    finally:
        office_md._pdf_backend, office_md._pdf_pages = o_b, o_p
    assert res is not None
    assert len(calls) == 1 and calls[0]["calls"] == 2   # 1ファイル1行に集約（2ページ分）


# ---- 既定 OFF は byte-identical（読み取り専用計装の証明）----

def test_default_off_byte_identical(monkeypatch):
    """メータリング既定（無効・conftest の autouse スタブのまま）で、classify/complete_json/embed/
    ask_graph/convert のリクエストボディが計装の有無で変わらないことを確認する。"""
    bodies_off: list = []

    def _post_capture(url, headers, body, timeout=90):
        bodies_off.append(body)
        if "input" in body:   # embeddings（openai）
            n = len(body["input"])
            return {"data": [{"embedding": [1.0] * 8} for _ in range(n)], "usage": {"prompt_tokens": n}}
        return {"choices": [{"message": {"content": '{"lens":"qa","confident":true}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(llm, "post_json", _post_capture)
    calls_off = _spy(monkeypatch)   # 既定 OFF

    monkeypatch.setattr(I, "_cfg", lambda settings, **kw: {"provider": "openai", "key": "k", "model": "gpt-4o-mini"})
    r_off = I.classify("消費税率は？", {})
    cfg_o = {"provider": "openai", "key": "k", "model": "text-embedding-3-small", "dim": 8}
    v_off = embeddings.embed(["a", "b"], cfg_o)

    assert calls_off == []   # 無効時は一切記録しない

    bodies_on: list = []

    def _post_capture2(url, headers, body, timeout=90):
        bodies_on.append(body)
        if "input" in body:
            n = len(body["input"])
            return {"data": [{"embedding": [1.0] * 8} for _ in range(n)], "usage": {"prompt_tokens": n}}
        return {"choices": [{"message": {"content": '{"lens":"qa","confident":true}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(llm, "post_json", _post_capture2)
    _enable(monkeypatch)
    calls_on = _spy(monkeypatch)
    r_on = I.classify("消費税率は？", {})
    v_on = embeddings.embed(["a", "b"], cfg_o)

    assert len(calls_on) == 2   # intent 1行 + embed 1行
    assert r_off == r_on
    assert v_off == v_on
    assert bodies_off == bodies_on   # 送信リクエストボディは有効/無効で完全に同一


# ---- RV8: metering.record の失敗ログ・timeout 転送 ----

def test_metering_record_failure_logs_masked_warning_not_raw_traceback(monkeypatch, caplog):
    """RV8 是正の固定: `add_usage_event` が例外を投げても `record()` は外へ出さず（既存契約）、
    従来の `_log.debug(..., exc_info=True)`（生の traceback を無条件出力）ではなく、
    `_log_masked_exception` 経由でマスク済みメッセージだけを WARNING ログへ残す。"""
    import logging

    _enable(monkeypatch)
    secret = "sk-shouldnotleak-metering-1234567890"

    def _boom(**kw):
        raise RuntimeError(f"db write failed: Authorization: Bearer {secret}")

    monkeypatch.setattr(ue, "add_usage_event", _boom)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        metering.record("research", "ollama", "qwen2.5", {"input_tokens": 1}, calls=1)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged
    assert secret not in logged


def test_metering_record_forwards_timeout_kwargs_to_add_usage_event(monkeypatch):
    """`connect_timeout`/`statement_timeout_ms` は `add_usage_event` へそのまま転送する
    （TOGGLE-RM・2026-09-03 で `enabled()` の設定確認クエリを撤去したため、`record()` の DB 接続は
    常に1回のみ＝以前あった2回接続の予算分割・目減りロジックは無くなった。指定値がそのまま届く）。"""
    _enable(monkeypatch)
    captured: dict = {}

    def _capture(**kw):
        captured.update(kw)

    monkeypatch.setattr(ue, "add_usage_event", _capture)
    metering.record("research", "ollama", "qwen2.5", {"input_tokens": 1}, calls=1,
                    connect_timeout=5, statement_timeout_ms=5000)
    assert captured["connect_timeout"] == 5
    assert captured["statement_timeout_ms"] == 5000


def test_metering_record_without_timeout_kwargs_passes_none(monkeypatch):
    _enable(monkeypatch)
    captured: dict = {}

    def _capture(**kw):
        captured.update(kw)

    monkeypatch.setattr(ue, "add_usage_event", _capture)
    metering.record("intent", "openai", "gpt-4o-mini", {"input_tokens": 1}, calls=1)
    assert captured["connect_timeout"] is None
    assert captured["statement_timeout_ms"] is None
