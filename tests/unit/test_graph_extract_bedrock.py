"""graph_extract の Bedrock（Claude）分岐の単体テスト（fake クライアント・実 AWS は叩かない）。

- `available()`: extract_provider=="bedrock" の明示選択（region/model/api_key の組み立て・認証未解決は
  None）。auto は選択中のクラウドプロバイダ（A7）→ ollama の順で、bedrock が選択中なら auto でも試す。
- `complete_json()`: 非ストリーミング `messages.create` で text ブロックを連結して返す・禁止パラメータ
  （temperature/top_p/top_k/thinking）を送らない・system は system パラメータで渡す。
- 例外整形: `_probe` が anthropic の APIStatusError/APIConnectionError を
  `_http_detail` 相当（status_code/message）に整形して `llm_error` に落とす（クラッシュしない）。
- キャッシュキー（`cfg["provider"]+"|"+cfg["model"]+"|"+text`）は bedrock でも既存構造のまま自然に効くことの確認。
"""
from __future__ import annotations

import hashlib

import anthropic
import httpx
import pytest

from sherpa import agents
from sherpa.ingest import graph_extract as GE


class _Block:
    def __init__(self, type, text=None):
        self.type, self.text = type, text


class _Resp:
    def __init__(self, content):
        self.content = content


def _fake_bedrock_client(resp=None, exc=None):
    """AnthropicBedrock の fake（テストごとに独立クラス＋生成した instance を state で回収）。"""
    state: dict = {"instances": []}

    class _Messages:
        def __init__(self):
            self.create_calls: list = []

        def create(self, **kwargs):
            self.create_calls.append(kwargs)
            if exc is not None:
                raise exc
            return resp

    class _Client:
        def __init__(self, **kwargs):
            self.init_kwargs = kwargs
            self.messages = _Messages()
            state["instances"].append(self)

    return _Client, state


_BANNED = ("temperature", "top_p", "top_k", "thinking")


# ---- available()（extract_provider 駆動の選択） ----

def _select_bedrock(monkeypatch):
    """A7（クラウドプロバイダ排他選択）: bedrock を選択中のプロバイダにする
    （`sherpa.keys.selected_cloud_provider`・既定は openai のため明示的に上書きしないと
    `graph_extract.available` の B() ファクトリが SigV4 等の有無を見る前に None を返す）。"""
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"personal_api_keys_allowed": True, "cloud_provider": "bedrock"})


def test_available_bedrock_explicit_with_auth_builds_cfg(monkeypatch):
    """region は東京固定（2026-07 決定）。settings に `bedrock_region` が混入していても無視される
    （個人設定の保存フィールド自体は撤去済み・ここでは値が渡っても効かないことのみ確認）。"""
    _select_bedrock(monkeypatch)
    cfg = GE.available({"extract_provider": "bedrock", "bedrock_api_key": "bkey",
                        "bedrock_region": "us-east-1", "bedrock_model": "anthropic.claude-haiku-4-5"})
    assert cfg == {"provider": "bedrock", "region": "ap-northeast-1",
                   "model": "anthropic.claude-haiku-4-5", "api_key": "bkey"}


def test_available_bedrock_default_model_and_region(monkeypatch):
    _select_bedrock(monkeypatch)
    monkeypatch.delenv("AWS_REGION", raising=False)
    cfg = GE.available({"extract_provider": "bedrock", "bedrock_api_key": "bkey"})
    assert cfg["model"] == agents._BEDROCK_MODEL           # 既定モデル（anthropic. プレフィックス必須）
    assert cfg["region"] == "ap-northeast-1"                # 既定リージョン


def test_available_bedrock_explicit_no_auth_is_llm_unavailable(monkeypatch):
    """bedrock が選択中のクラウドプロバイダ（A7）でも認証未解決（設定キーも SigV4 も無い）なら
    None（FBK-1・fail-loud＝他プロバイダへは倒さない。`force_provider` 明示選択の seam は
    GRAPH-SRC 2026-09-04 で唯一の利用者〔graph_ab.py〕ごと撤去済み・auto 解決のこの経路で
    同じ挙動を確認する）。"""
    _select_bedrock(monkeypatch)
    monkeypatch.setattr(agents, "_bedrock_auth_available", lambda api_key=None: False)
    cfg = GE.available({})
    assert cfg is None


def test_available_bedrock_selected_and_resolved_participates_in_auto(monkeypatch):
    """bedrock が選択中のクラウドプロバイダ（A7）かつ認証解決済みなら、auto
    （extract_provider 省略）でも選ばれる。"""
    _select_bedrock(monkeypatch)
    monkeypatch.setattr(agents, "_bedrock_auth_available", lambda api_key=None: True)
    cfg = GE.available({"bedrock_api_key": "x"})            # extract_provider 省略＝auto
    assert cfg == {"provider": "bedrock", "region": "ap-northeast-1",
                   "model": agents._BEDROCK_MODEL, "api_key": "x"}


def test_available_bedrock_not_selected_in_auto(monkeypatch):
    """bedrock が選択中のクラウドプロバイダでなければ、auto（extract_provider 省略）は
    bedrock_api_key があっても bedrock を試さず ollama（既定 URL）に落ちる。"""
    monkeypatch.setattr(agents, "_bedrock_auth_available", lambda api_key=None: True)   # 仮に解決可能でも
    cfg = GE.available({"bedrock_api_key": "x"})            # extract_provider 省略＝auto・既定選択は openai
    assert cfg == {"provider": "ollama", "url": "http://localhost:11434", "model": "qwen2.5"}


def test_available_reads_system_settings_exactly_once_for_bedrock_explicit(monkeypatch):
    """重大バグ是正（RV 4巡目 #7）: B() ファクトリ（A7 判定＋キー解決）と `llm.select_provider()`
    本体を同じスナップショットで行い、`store.get_system_settings()` は1回だけ読む
    （個別に読み直すと途中の admin 更新で「選択中」と「キーあり」の判定が食い違う窓ができる）。"""
    monkeypatch.setattr(agents, "_bedrock_auth_available", lambda api_key=None: True)
    calls = []

    def _spy():
        calls.append(1)
        return {"personal_api_keys_allowed": True, "cloud_provider": "bedrock"}

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy)
    cfg = GE.available({"extract_provider": "bedrock", "bedrock_api_key": "bkey"})
    assert cfg is not None
    assert len(calls) == 1


def test_available_passing_system_settings_snapshot_skips_own_read(monkeypatch):
    """明示的に `system_settings=` を渡した呼び出し元は、`available()` 自身は一切
    `store.get_system_settings()` を呼ばない（渡されたスナップショットをそのまま使う）。"""
    monkeypatch.setattr(agents, "_bedrock_auth_available", lambda api_key=None: True)

    def _boom():
        raise AssertionError("system_settings が渡されているのに自前で読み直した")

    monkeypatch.setattr("sherpa.store.get_system_settings", _boom)
    cfg = GE.available({"extract_provider": "bedrock", "bedrock_api_key": "bkey"},
                       system_settings={"personal_api_keys_allowed": True, "cloud_provider": "bedrock"})
    assert cfg is not None


def test_available_openai_gemini_ollama_unchanged(monkeypatch):
    """既存3プロバイダの auto 選択は bedrock 追加後もバイト単位で不変（回帰確認）。
    モデル名は個人設定でなく管理者のカタログ（組み込み既定）から解決される。"""
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    cfg = GE.available({"openai_api_key": "sk-x"})
    # `openai_endpoint_override`（`complete_json` の送信時接続先解決へ引き継ぐスナップショット）
    # を除いた本体は従来どおり。
    assert {k: v for k, v in cfg.items() if k != "openai_endpoint_override"} == \
        {"provider": "openai", "key": "sk-x", "model": "gpt-5.5"}


@pytest.mark.parametrize("extract_provider,extra_settings,extra_sentinel,expected_cfg", [
    ("openai", {"openai_api_key": "sk-x"}, {},
     {"provider": "openai", "key": "sk-x", "model": "gpt-5.5"}),
    ("gemini", {"gemini_api_key": "gk-x"}, {"cloud_provider": "gemini"},
     {"provider": "gemini", "key": "gk-x", "model": "gemini-2.5-flash"}),
    ("ollama", {}, {},
     {"provider": "ollama", "url": "http://localhost:11434", "model": "qwen2.5"}),
])
def test_available_g_o_l_factories_share_snapshot_with_model_catalog(
        monkeypatch, extract_provider, extra_settings, extra_sentinel, expected_cfg):
    """G()/O()/L() ファクトリ（gemini/openai/ollama）の `model_catalog.resolve_model` 呼び出しは、
    `available()` が一度だけ取得した snapshot を渡さず独自に読み直すと、1回の呼び出し内で
    admin 更新が挟まった場合に判定が新旧混在しうる（`system.py` 側の `system_settings=sys_s`
    配線と同型の契約）。ここでは openai/gemini/ollama の3経路それぞれで、モデル解決が
    `available()` と同じスナップショットを使うことを、stub せず実経路で固定する
    （`store.get_system_settings()` は1回だけ）。"""
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL"):
        monkeypatch.delenv(k, raising=False)
    sentinel = {"personal_api_keys_allowed": True, **extra_sentinel}
    read_calls = []

    def _spy_get_system_settings():
        read_calls.append(1)
        return sentinel

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy_get_system_settings)

    from sherpa import model_catalog
    seen = []
    real_resolve = model_catalog.resolve_model

    def _resolve_spy(*a, **kw):
        seen.append(kw.get("system_settings"))
        return real_resolve(*a, **kw)

    monkeypatch.setattr(model_catalog, "resolve_model", _resolve_spy)

    cfg = GE.available({"extract_provider": extract_provider, **extra_settings})

    assert {k: v for k, v in cfg.items() if k != "openai_endpoint_override"} == expected_cfg
    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"
    assert seen, "model_catalog.resolve_model が呼ばれなかった"
    assert all(snap is sentinel for snap in seen), \
        "resolve_model が available() と異なる system_settings オブジェクトを受け取った"
    if extract_provider == "openai":
        assert cfg["openai_endpoint_override"] is sentinel, \
            "openai_endpoint_override が available() と異なる system_settings オブジェクトになっている"


# ---- available(strict=...): 意図しない課金の是正 ----

def test_available_strict_raises_for_invalid_cloud_provider(monkeypatch):
    """`cloud_provider`（A7）が非空の不正値のとき、`strict=True` は黙って既定（openai）へ
    倒れたキーで送信しない。`strict=False`（既定）は従来どおり openai へ倒れる。"""
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "OLLAMA_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True, "cloud_provider": "not-a-real-provider",
        "openai_api_key": "sk-x"})
    from sherpa import keys

    with pytest.raises(keys.InvalidCloudProviderConfigError):
        GE.available({}, strict=True)
    cfg = GE.available({})
    assert cfg["provider"] == "openai"


# ---- complete_json（非ストリーミング・禁止パラメータ非送信）----

def test_complete_json_bedrock_joins_text_blocks_and_omits_forbidden_params(monkeypatch):
    resp = _Resp([_Block("text", "こんにちは"), _Block("thinking"), _Block("text", "。")])
    FakeClient, state = _fake_bedrock_client(resp=resp)
    monkeypatch.setattr(anthropic, "AnthropicBedrock", FakeClient)
    cfg = {"provider": "bedrock", "region": "ap-northeast-1", "model": "anthropic.claude-opus-4-8", "api_key": "bkey"}
    out = GE.complete_json("system prompt", "user prompt", cfg)
    assert out == "こんにちは。"                              # text ブロックのみ連結（thinking は無視）
    inst = state["instances"][-1]
    assert inst.init_kwargs["api_key"] == "bkey" and inst.init_kwargs["aws_region"] == "ap-northeast-1"
    kw = inst.messages.create_calls[-1]
    assert kw["model"] == "anthropic.claude-opus-4-8" and kw["max_tokens"] == GE._BEDROCK_MAX_TOKENS
    assert kw["system"] == "system prompt"
    assert kw["messages"] == [{"role": "user", "content": "user prompt"}]
    assert all(m["role"] != "system" for m in kw["messages"])   # system は message に混ぜず system パラメータで渡す
    for banned in _BANNED:
        assert banned not in kw


def test_complete_json_bedrock_passes_none_api_key_when_absent(monkeypatch):
    """api_key 未設定（None）は SDK の env/SigV4 チェーンに委譲＝素直に None を渡す。"""
    resp = _Resp([_Block("text", "ok")])
    FakeClient, state = _fake_bedrock_client(resp=resp)
    monkeypatch.setattr(anthropic, "AnthropicBedrock", FakeClient)
    cfg = {"provider": "bedrock", "region": "ap-northeast-1", "model": "anthropic.claude-opus-4-8", "api_key": None}
    GE.complete_json("s", "u", cfg)
    assert state["instances"][-1].init_kwargs["api_key"] is None


def test_complete_json_bedrock_pins_base_url_against_malicious_env_override(monkeypatch):
    """`complete_json` の bedrock 分岐も `providers/bedrock.py::_get_client` と兄弟の構築コード——
    `AnthropicBedrock` へ `base_url=_bedrock_runtime_base_url()` を明示する。省略すると SDK が
    env `ANTHROPIC_BEDROCK_BASE_URL` を読んで接続先を上書きできてしまう（`.env` の全キー export
    構成のため）。悪性 env を立てても、実際に渡された base_url が正準の東京 runtime URL の
    ままであることを確認する。"""
    monkeypatch.setenv("ANTHROPIC_BEDROCK_BASE_URL", "https://evil.example.com")
    resp = _Resp([_Block("text", "ok")])
    FakeClient, state = _fake_bedrock_client(resp=resp)
    monkeypatch.setattr(anthropic, "AnthropicBedrock", FakeClient)
    cfg = {"provider": "bedrock", "region": "ap-northeast-1", "model": "anthropic.claude-opus-4-8", "api_key": "bkey"}
    GE.complete_json("s", "u", cfg)
    assert state["instances"][-1].init_kwargs["base_url"] == "https://bedrock-runtime.ap-northeast-1.amazonaws.com"


# ---- 例外整形（_http_detail 相当・llm_error への正しい落とし込み）----

def test_probe_formats_bedrock_api_status_error(monkeypatch):
    resp_httpx = httpx.Response(403, request=httpx.Request("POST", "http://x"))
    exc = anthropic.APIStatusError("access denied", response=resp_httpx, body=None)
    FakeClient, _ = _fake_bedrock_client(exc=exc)
    monkeypatch.setattr(anthropic, "AnthropicBedrock", FakeClient)
    cfg = {"provider": "bedrock", "region": "ap-northeast-1", "model": "anthropic.claude-opus-4-8", "api_key": "bkey"}
    ok, detail = GE._probe(cfg)
    assert ok is False
    assert "403" in detail and "access denied" in detail    # _http_detail 相当（status_code + message）


def test_probe_formats_bedrock_connection_error(monkeypatch):
    exc = anthropic.APIConnectionError(message="no route to host", request=httpx.Request("POST", "http://x"))
    FakeClient, _ = _fake_bedrock_client(exc=exc)
    monkeypatch.setattr(anthropic, "AnthropicBedrock", FakeClient)
    cfg = {"provider": "bedrock", "region": "ap-northeast-1", "model": "anthropic.claude-opus-4-8", "api_key": None}
    ok, detail = GE._probe(cfg)
    assert ok is False and "no route to host" in detail


def test_error_detail_falls_back_generically_for_non_anthropic_exceptions():
    """anthropic 由来でない例外は従来どおり型名＋メッセージ（既存挙動の回帰確認）。"""
    detail = GE._error_detail(ValueError("boom"))
    assert detail == "ValueError: boom"


# ---- キャッシュキー（既存構造のまま bedrock でも自然に効くことの確認・確認のみ／変更なし）----

def test_cache_key_differentiates_bedrock_from_other_providers_and_models():
    text = "同じ本文"
    h_bedrock_opus = hashlib.sha1(("bedrock|anthropic.claude-opus-4-8|" + text).encode("utf-8")).hexdigest()
    h_bedrock_haiku = hashlib.sha1(("bedrock|anthropic.claude-haiku-4-5|" + text).encode("utf-8")).hexdigest()
    h_openai = hashlib.sha1(("openai|gpt-5.5|" + text).encode("utf-8")).hexdigest()
    assert len({h_bedrock_opus, h_bedrock_haiku, h_openai}) == 3   # provider/model が違えば別キャッシュキー
