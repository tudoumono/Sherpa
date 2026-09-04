"""sherpa/metering.py の単体テスト（S1・2026-07-15-LLMオーケストレーション実装計画.md §3.4）。

- パーサ群: プロバイダ別ペイロード → 標準4フィールド辞書（壊れた/欠落ペイロードは None・例外を出さない）。
- `record()`: 常時 store.add_usage_event へ委譲（TOGGLE-RM・2026-09-03 で `enabled()` トグルを撤去し
  常時ONへ固定）・`suppress()` 中は no-op・store 側が例外を出しても飲み込む。
- アキュムレータスタック（acc_begin/acc_add/acc_end）: スコープ外は no-op・begin/add/add/end の合算。
"""
from __future__ import annotations

from types import SimpleNamespace

from sherpa import metering

# tests/unit/conftest.py::_hermetic_metering_record が autouse で `metering.record` を no-op に
# 差し替える（実 DB 書き込み防止）。`record()` 自体の挙動を検証する本ファイルのテストは、
# 捕捉しておいた本物の関数オブジェクトへ `monkeypatch.setattr(metering, "record", _real_record)`
# で明示的に戻す（autouse は本体実行より先に適用されるため、本体内の明示的 monkeypatch が
# 最後に効く＝確実に上書きされる）。
_real_record = metering.record


# ---- パーサ群 ----

def test_usage_from_openai_chat_full_and_broken():
    resp = {"usage": {"prompt_tokens": 100, "completion_tokens": 20,
                      "prompt_tokens_details": {"cached_tokens": 30},
                      "completion_tokens_details": {"reasoning_tokens": 5}}}
    assert metering.usage_from_openai_chat(resp) == {
        "input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 20, "reasoning_output_tokens": 5}
    assert metering.usage_from_openai_chat({}) is None
    assert metering.usage_from_openai_chat(None) is None
    assert metering.usage_from_openai_chat({"usage": "not-a-dict"}) is None


def test_usage_from_gemini_output_is_candidates_only():
    """レビュー是正 major #3: output は candidatesTokenCount のみ（thoughts を混ぜない）。"""
    data = {"usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20,
                              "cachedContentTokenCount": 10, "thoughtsTokenCount": 5}}
    assert metering.usage_from_gemini(data) == {
        "input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20, "reasoning_output_tokens": 5}
    assert metering.usage_from_gemini({}) is None
    assert metering.usage_from_gemini(None) is None


def test_usage_from_ollama_chat_full_and_broken():
    resp = {"message": {"content": "答え"}, "prompt_eval_count": 80, "eval_count": 12}
    assert metering.usage_from_ollama_chat(resp) == {
        "input_tokens": 80, "cached_input_tokens": None, "output_tokens": 12, "reasoning_output_tokens": None}
    assert metering.usage_from_ollama_chat({"message": {"content": "答え"}}) is None
    assert metering.usage_from_ollama_chat(None) is None


def test_usage_from_anthropic_dict_and_sdk_object():
    d = {"input_tokens": 50, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5,
         "output_tokens": 20}
    assert metering.usage_from_anthropic(d) == {
        "input_tokens": 65, "cached_input_tokens": 10, "output_tokens": 20, "reasoning_output_tokens": 0}
    # SDK オブジェクト（SimpleNamespace で属性アクセスを模す）も同じ式。
    obj = SimpleNamespace(input_tokens=50, cache_read_input_tokens=10,
                          cache_creation_input_tokens=5, output_tokens=20)
    assert metering.usage_from_anthropic(obj) == {
        "input_tokens": 65, "cached_input_tokens": 10, "output_tokens": 20, "reasoning_output_tokens": 0}
    assert metering.usage_from_anthropic(None) is None


def test_usage_from_openai_embed_and_ollama_embed():
    assert metering.usage_from_openai_embed({"usage": {"prompt_tokens": 40}}) == {
        "input_tokens": 40, "cached_input_tokens": None, "output_tokens": 0, "reasoning_output_tokens": None}
    assert metering.usage_from_openai_embed({}) is None
    assert metering.usage_from_ollama_embed({"prompt_eval_count": 33}) == {
        "input_tokens": 33, "cached_input_tokens": None, "output_tokens": 0, "reasoning_output_tokens": None}
    assert metering.usage_from_ollama_embed({}) is None


def test_parsers_never_raise_on_garbage():
    """壊れた/型不正なペイロードは None を返す（例外を出さない）。"""
    for parser in (metering.usage_from_openai_chat, metering.usage_from_gemini,
                  metering.usage_from_ollama_chat, metering.usage_from_openai_embed,
                  metering.usage_from_ollama_embed):
        assert parser("not-a-dict") is None
        assert parser(123) is None
    # usage_from_anthropic は dict でも SDK オブジェクトでもない値を渡されても例外を出さない
    # （getattr の既定値 None → 属性なし=0 扱い・None そのものだけが None を返す）。
    assert metering.usage_from_anthropic(object()) == {
        "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}


# ---- record() ----

def test_record_gating_and_safety(monkeypatch):
    from sherpa.store import usage_events as ue

    monkeypatch.setattr(metering, "record", _real_record)   # autouse no-op を戻す
    calls = []

    def spy(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(ue, "add_usage_event", spy)

    # 常時記録: kind/provider/model/tokens/calls/user_id/world 付きで1回（TOGGLE-RM・2026-09-03）。
    metering.record("intent", "openai", "gpt-4o-mini",
                    {"input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 5,
                     "reasoning_output_tokens": 1}, user_id="u1", world="v1", calls=2)
    assert len(calls) == 1
    c = calls[0]
    assert c["kind"] == "intent" and c["provider"] == "openai" and c["model"] == "gpt-4o-mini"
    assert c["input_tokens"] == 10 and c["cached_input_tokens"] == 3
    assert c["output_tokens"] == 5 and c["reasoning_output_tokens"] == 1
    assert c["calls"] == 2 and c["user_id"] == "u1" and c["world"] == "v1"
    calls.clear()

    # usage=None ならトークン列は全て None（報告不能マーカー）。
    metering.record("embed", "gemini", "gemini-embedding-001", None, calls=3)
    assert len(calls) == 1
    c = calls[0]
    assert c["input_tokens"] is None and c["cached_input_tokens"] is None
    assert c["output_tokens"] is None and c["reasoning_output_tokens"] is None
    calls.clear()

    # 欠落サブフィールドは0に補正（_usage_meta のクランプ意味論と一致）。
    metering.record("vlm", "ollama", "qwen2.5vl", {"input_tokens": 7}, calls=1)
    c = calls[0]
    assert c["input_tokens"] == 7
    assert c["cached_input_tokens"] == 0 and c["output_tokens"] == 0 and c["reasoning_output_tokens"] == 0
    calls.clear()

    # store 側が例外を出しても record() は伝播しない。
    def boom(**kwargs):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(ue, "add_usage_event", boom)
    metering.record("intent", "openai", "gpt-4o-mini", {"input_tokens": 1}, calls=1)   # raise しないこと


def test_record_clamps_oversized_string_fields(monkeypatch):
    """secRV MED-4是正: 巨大 ollama_model（数MB級の自由文字列。`routers/system.py::settings_put` の
    形式検証や `subagent_profiles.resolve_sub` の防御的検証を経ずに旧データ等から届いた想定）を
    毎回 usage_events へ複製し続けるストレージ増幅/DoS を防ぐため、
    provider/model/user_id/world は防御的に256字で切り詰められる。"""
    from sherpa.store import usage_events as ue

    monkeypatch.setattr(metering, "record", _real_record)   # autouse no-op を戻す
    calls = []
    monkeypatch.setattr(ue, "add_usage_event", lambda **kwargs: calls.append(kwargs))

    huge_model = "x" * 5000
    huge_world = "w" * 5000
    huge_user = "u" * 5000
    metering.record("chat-sub", "ollama", huge_model, {"input_tokens": 1},
                    user_id=huge_user, world=huge_world, calls=1)
    assert len(calls) == 1
    c = calls[0]
    assert len(c["model"]) == 256 and c["model"] == huge_model[:256]
    assert len(c["world"]) == 256 and c["world"] == huge_world[:256]
    assert len(c["user_id"]) == 256 and c["user_id"] == huge_user[:256]
    assert c["provider"] == "ollama"   # 短い値は無変化


def test_record_clamp_preserves_none_and_short_values(monkeypatch):
    from sherpa.store import usage_events as ue

    monkeypatch.setattr(metering, "record", _real_record)   # autouse no-op を戻す
    calls = []
    monkeypatch.setattr(ue, "add_usage_event", lambda **kwargs: calls.append(kwargs))
    metering.record("intent", "openai", "gpt-4o-mini", {"input_tokens": 1})   # user_id/world 省略＝None
    c = calls[0]
    assert c["user_id"] is None and c["world"] is None
    assert c["model"] == "gpt-4o-mini"   # 短い値はそのまま


# ---- アキュムレータスタック ----

def test_acc_stack_no_scope_is_noop():
    metering.acc_add({"input_tokens": 1})   # スコープが無い状態＝例外なく無視
    assert metering.acc_end() == (None, 0)


def test_acc_stack_begin_add_add_end():
    metering.acc_begin()
    metering.acc_add({"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5,
                      "reasoning_output_tokens": 1})
    metering.acc_add(None)   # 報告不能な1回（calls だけ増える・tokens は不変）
    tokens, calls = metering.acc_end()
    assert calls == 2
    assert tokens == {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5,
                      "reasoning_output_tokens": 1}


def test_acc_stack_nested_scopes_are_isolated():
    metering.acc_begin()
    metering.acc_add({"input_tokens": 1})
    metering.acc_begin()
    metering.acc_add({"input_tokens": 100})
    inner_tokens, inner_calls = metering.acc_end()
    outer_tokens, outer_calls = metering.acc_end()
    assert inner_calls == 1 and inner_tokens["input_tokens"] == 100
    assert outer_calls == 1 and outer_tokens["input_tokens"] == 1


def test_acc_stack_all_none_stays_none():
    metering.acc_begin()
    metering.acc_add(None)
    metering.acc_add(None)
    tokens, calls = metering.acc_end()
    assert calls == 2 and tokens is None


# ---- suppress()（S2 RV MED 是正・2026-07-17: 読み取り専用経路からの記録禁止） ----

def test_suppress_blocks_record_and_restores(monkeypatch):
    monkeypatch.setattr(metering, "record", _real_record)   # autouse no-op を戻す
    calls = []
    monkeypatch.setattr(metering._ue, "add_usage_event", lambda **kw: calls.append(kw))
    with metering.suppress():
        metering.record("extract", "openai", "m", {"input_tokens": 1})
        with metering.suppress():   # ネスト再入
            metering.record("propose", "openai", "m", {"input_tokens": 1})
        metering.record("intent", "openai", "m", {"input_tokens": 1})   # 内側を抜けても外側の抑止が生きる
    assert calls == []
    metering.record("extract", "openai", "m", {"input_tokens": 2})      # スコープ外＝通常どおり記録
    assert len(calls) == 1 and calls[0]["kind"] == "extract"


def test_suppress_blocks_acc_add_into_outer_scope():
    """S2 RV 2巡目是正: suppress() 中は外側の既存アキュムレータにも合算しない（合成可能性）。"""
    metering.acc_begin()
    with metering.suppress():
        metering.acc_add({"input_tokens": 7})
    assert metering.acc_end() == (None, 0)


# ---- LOG-UX（2026-09-04）: record() の sherpa.usage ログ1行・acc_elapsed() ----

def test_record_emits_usage_log_line_with_elapsed_and_no_user_id(monkeypatch, caplog):
    monkeypatch.setattr(metering, "record", _real_record)   # autouse no-op を戻す
    monkeypatch.setattr(metering._ue, "add_usage_event", lambda **kw: None)
    metering.acc_begin()
    metering.acc_add({"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20,
                      "reasoning_output_tokens": 0})
    tokens, calls = metering.acc_end()
    with caplog.at_level("INFO", logger="sherpa.usage"):
        metering.record("embed", "openai", "text-embedding-3-small", tokens,
                        user_id="secret-user", world="test2", calls=calls)
    assert len(caplog.records) == 1
    msg = caplog.records[0].message
    assert "kind=embed" in msg and "provider=openai" in msg and "model=text-embedding-3-small" in msg
    assert "in=100" in msg and "cached=10" in msg and "out=20" in msg and "calls=1" in msg
    assert "world=test2" in msg
    assert "elapsed=" in msg   # acc_begin/acc_end のスコープがあったので経過秒が乗る
    assert "secret-user" not in msg   # user_id はログへ出さない


def test_record_usage_log_shows_question_mark_for_unreported_tokens(monkeypatch, caplog):
    monkeypatch.setattr(metering, "record", _real_record)
    monkeypatch.setattr(metering._ue, "add_usage_event", lambda **kw: None)
    with caplog.at_level("INFO", logger="sherpa.usage"):
        metering.record("chat-plan", "openai", "gpt-x", None, calls=1)   # usage=None＝報告不能
    msg = caplog.records[0].message
    assert "in=? cached=? out=?" in msg
    assert "elapsed=" not in msg   # acc スコープが無い呼び出し＝省略


def test_record_omits_world_when_absent(monkeypatch, caplog):
    monkeypatch.setattr(metering, "record", _real_record)
    monkeypatch.setattr(metering._ue, "add_usage_event", lambda **kw: None)
    with caplog.at_level("INFO", logger="sherpa.usage"):
        metering.record("intent", "openai", "m", {"input_tokens": 1}, calls=1)   # world 省略
    msg = caplog.records[0].message
    assert "world=" not in msg


def test_record_usage_log_never_raises_even_if_logger_broken(monkeypatch, caplog):
    """`log_usage_line` 自身の失敗は `record()` 全体を壊さない（DB 記録は成功したことにする）。"""
    monkeypatch.setattr(metering, "record", _real_record)
    calls = []
    monkeypatch.setattr(metering._ue, "add_usage_event", lambda **kw: calls.append(kw))

    def boom(*a, **kw):
        raise RuntimeError("logging exploded")

    monkeypatch.setattr(metering._usage_log, "info", boom)
    metering.record("intent", "openai", "m", {"input_tokens": 1}, calls=1)   # raise しないこと
    assert len(calls) == 1   # DB 記録自体は行われている


def test_acc_elapsed_is_none_without_scope():
    assert metering.acc_elapsed() is None


def test_acc_elapsed_is_consumed_once():
    metering.acc_begin()
    metering.acc_end()
    first = metering.acc_elapsed()
    assert first is not None
    assert metering.acc_elapsed() is None   # 1回読んだら消費済み


def test_acc_elapsed_stale_value_is_not_reused_by_unrelated_record(monkeypatch, caplog):
    """`acc_end()` を呼んだのに `record()` を呼ばない経路（例: embed() の calls=0 キャッシュヒットのみ）
    の後、無関係な `record()` 呼び出し（acc スコープ無し）へ古い経過秒が誤って乗らないこと
    （`_ELAPSED_FRESHNESS_SEC` の fail-safe）。"""
    monkeypatch.setattr(metering, "record", _real_record)
    monkeypatch.setattr(metering._ue, "add_usage_event", lambda **kw: None)
    monkeypatch.setattr(metering, "_ELAPSED_FRESHNESS_SEC", 0.0)   # 即座に「古い」扱いにする
    metering.acc_begin()
    metering.acc_end()   # record() を呼ばずに終える（embed() の calls=0 パターンを模す）
    with caplog.at_level("INFO", logger="sherpa.usage"):
        metering.record("graph_ask", "bedrock", "claude", {"input_tokens": 1}, calls=1)
    msg = caplog.records[0].message
    assert "elapsed=" not in msg
