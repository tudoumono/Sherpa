"""AWS Bedrock（Claude）プロバイダの単体テスト（SDK クライアントは fake・実 AWS は叩かない）。

- 非ストリーミング応答の整形（`_complete` が text ブロックを連結・thinking 等は無視）。
- streaming イベント列（`_stream` が text_stream を逐次 yield）。
- temperature/top_p/top_k/thinking を **送っていない** こと（fake が受けた kwargs を検証）。
- probe の例外整形（APIStatusError の status_code/message ・ APIConnectionError）。
"""
from __future__ import annotations

import anthropic
import httpx

from sherpa.agents import BedrockProvider


class _Block:
    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type, self.text, self.name, self.input, self.id = type, text, name, input, id


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content, self.stop_reason = content, stop_reason


class _Stream:
    def __init__(self, chunks):
        self.text_stream = iter(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeMessages:
    def __init__(self):
        self.create_calls: list = []
        self.stream_calls: list = []
        self._create_resp = None
        self._create_exc = None
        self._stream_chunks: list = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        if self._create_exc:
            raise self._create_exc
        return self._create_resp

    def stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        return _Stream(self._stream_chunks)


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


def _provider_with_fake():
    p = BedrockProvider(region="ap-northeast-1",
                        model="jp.anthropic.claude-haiku-4-5-20251001-v1:0", api_key="bedrock-test-key")
    p._client = _FakeClient()            # 遅延生成をスキップ（実 AnthropicBedrock を作らない）
    return p, p._client


_BANNED = ("temperature", "top_p", "top_k", "thinking")


def test_complete_joins_text_blocks_and_omits_sampling_params():
    p, fake = _provider_with_fake()
    fake.messages._create_resp = _Resp([_Block("text", "税率は"), _Block("thinking"), _Block("text", "10%です。")])
    assert p._complete("消費税率は?") == "税率は10%です。"           # text ブロックのみ連結（thinking は無視）
    kw = fake.messages.create_calls[-1]
    assert kw["model"] == "jp.anthropic.claude-haiku-4-5-20251001-v1:0" and kw["max_tokens"]
    assert kw["messages"] == [{"role": "user", "content": "消費税率は?"}]
    assert all(b not in kw for b in _BANNED)


def test_stream_yields_text_chunks_and_omits_sampling_params():
    p, fake = _provider_with_fake()
    fake.messages._stream_chunks = ["税率", "は10%", "です。"]
    assert list(p._stream("消費税率は?")) == ["税率", "は10%", "です。"]
    kw = fake.messages.stream_calls[-1]
    assert kw["max_tokens"] and kw["messages"][0]["role"] == "user"
    assert all(b not in kw for b in _BANNED)


def test_system_prompt_passed_as_system_param_not_message():
    p, fake = _provider_with_fake()
    p.system_prompt = "簡潔に。"
    fake.messages._create_resp = _Resp([_Block("text", "ok")])
    p._complete("q")
    kw = fake.messages.create_calls[-1]
    assert kw["system"] == "簡潔に。"                                # Anthropic は system をトップレベル引数で渡す
    assert all(m["role"] != "system" for m in kw["messages"])       # system を message ロールに混ぜない


def test_probe_ok_uses_minimal_max_tokens():
    p, fake = _provider_with_fake()
    fake.messages._create_resp = _Resp([_Block("text", "pong")])
    ok, detail = p.probe()
    assert ok is True and detail == ""
    assert fake.messages.create_calls[-1]["max_tokens"] == 16       # 接続テストは最小リクエスト


def test_probe_formats_api_status_error():
    p, fake = _provider_with_fake()
    resp = httpx.Response(403, request=httpx.Request("POST", "http://x"))
    fake.messages._create_exc = anthropic.APIStatusError("access denied", response=resp, body=None)
    ok, detail = p.probe()
    assert ok is False and "403" in detail and "access denied" in detail


def test_probe_formats_connection_error():
    p, fake = _provider_with_fake()
    fake.messages._create_exc = anthropic.APIConnectionError(
        message="no route to host", request=httpx.Request("POST", "http://x"))
    ok, detail = p.probe()
    assert ok is False and "no route to host" in detail


# ===== _safe_bedrock_detail: マスクしてから切断する順序の保証（明示キー・env キーの両方）=====

def test_probe_masks_explicit_key_straddling_400_char_boundary_leaves_no_fragment():
    """明示キー（`BedrockProvider(api_key=...)`）が `_bedrock_error_detail` の400字切断境界を
    またぐ位置にあっても、断片が残らないこと。Bearer/api-key/sk- のどの一般パターンにも該当しない
    素のキーで検証する（完全一致 redaction だけが頼りの状態で、マスクが切断より先に効いている証拠）。
    """
    key = "PLAINBEDROCKKEY-99887766554433221100998877665544-STRADDLE-TAIL"
    p = BedrockProvider(region="ap-northeast-1", model="jp.anthropic.claude-haiku-4-5-20251001-v1:0",
                        api_key=key)
    p._client = _FakeClient()
    prefix = "x" * 360   # secret の開始位置(360)が400字の切断境界をまたぐ余裕を持たせる
    resp = httpx.Response(403, request=httpx.Request("POST", "http://x"))
    p._client.messages._create_exc = anthropic.APIStatusError(
        prefix + key + "y" * 30, response=resp, body=None)
    ok, detail = p.probe()
    assert ok is False
    assert key not in detail
    assert key[:15] not in detail, f"キーの断片が残っている: {detail!r}"
    assert len(detail) <= 400


def test_probe_masks_env_key_straddling_400_char_boundary_leaves_no_fragment(monkeypatch):
    """env 認証（`AWS_BEARER_TOKEN_BEDROCK`・`BedrockProvider(api_key=None)` で SDK の env/SigV4
    チェーンへ委譲したケース）でも、SDK 例外メッセージに紛れ込んだ env キーの断片が
    400字切断境界をまたいでも残らないこと（呼び出し元は「どのキーが実際に使われたか」を知らない）。
    """
    env_key = "ENVBEDROCKKEY-11223344556677889900112233445566-STRADDLE-TAIL"
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", env_key)
    p = BedrockProvider(region="ap-northeast-1", model="jp.anthropic.claude-haiku-4-5-20251001-v1:0",
                        api_key=None)
    p._client = _FakeClient()
    prefix = "x" * 360
    resp = httpx.Response(401, request=httpx.Request("POST", "http://x"))
    p._client.messages._create_exc = anthropic.APIStatusError(
        prefix + env_key + "y" * 30, response=resp, body=None)
    ok, detail = p.probe()
    assert ok is False
    assert env_key not in detail
    assert env_key[:15] not in detail, f"env キーの断片が残っている: {detail!r}"


def test_default_model_and_region_is_always_tokyo(monkeypatch):
    """region は東京（ap-northeast-1）固定（2026-07 決定・誤設定防止）。env AWS_REGION も明示指定も無視する。"""
    monkeypatch.delenv("AWS_REGION", raising=False)
    p = BedrockProvider()
    assert p.model == "jp.anthropic.claude-haiku-4-5-20251001-v1:0"  # JP 推論プロファイルの既定（2026-07 runtime 切替）
    assert p._region == "ap-northeast-1"
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert BedrockProvider()._region == "ap-northeast-1"             # env AWS_REGION も無視される
    assert BedrockProvider(region="eu-west-1")._region == "ap-northeast-1"   # 明示指定も無視される


def test_get_client_builds_anthropic_bedrock_not_mantle(monkeypatch):
    """_get_client は bedrock-runtime クライアント（AnthropicBedrock）を構築する（Mantle は使わない・2026-07 切替）。"""
    calls: list = []

    class _FakeSDKClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(anthropic, "AnthropicBedrock", _FakeSDKClient)
    p = BedrockProvider(region="ap-northeast-1", api_key="bedrock-test-key")
    client = p._get_client()
    assert isinstance(client, _FakeSDKClient)
    assert calls[-1] == {"api_key": "bedrock-test-key", "aws_region": "ap-northeast-1",
                         "base_url": "https://bedrock-runtime.ap-northeast-1.amazonaws.com"}
    assert p._get_client() is client                     # 遅延生成はキャッシュされる（再構築しない）


def test_get_client_pins_base_url_against_malicious_env_override(monkeypatch):
    """`ANTHROPIC_BEDROCK_BASE_URL` が悪性値でも、実 SDK（fake でない）の接続先は
    東京固定の bedrock-runtime URL のまま（`.env` が全キーを export する構成のため、無関係な env の
    紛れ込みで接続先を上書きできてしまう穴を塞ぐ）。実 HTTP は送らない（コンストラクタのみ）。"""
    monkeypatch.setenv("ANTHROPIC_BEDROCK_BASE_URL", "https://evil.example.com")
    p = BedrockProvider(region="ap-northeast-1", api_key="bedrock-test-key")
    client = p._get_client()
    assert str(client.base_url).rstrip("/") == "https://bedrock-runtime.ap-northeast-1.amazonaws.com"
