"""OpenAI 互換 API の接続先（`llm.openai_base_url`/`openai_url`/`openai_headers`/
`openai_endpoint_kind`/`openai_auth_header_style`/`openai_api_version` ほか）の単体テスト。

各関数へ `system_settings` を直接渡す純粋な単体テスト（DB・env・通信に依存しない）。
"""
from __future__ import annotations

import pytest

from sherpa import llm

AZURE = "https://myres.openai.azure.com/openai/v1"
BEARER_HEADERS = {"Authorization": "Bearer sk-x", "Content-Type": "application/json"}


def _custom(base: str, **extra) -> dict:
    return {"openai_endpoint_kind": "custom", "openai_base_url": base, **extra}


# ---- URL 組み立て ----------------------------------------------------------------------------

def test_default_base_url_and_urls():
    assert llm.openai_base_url({}) == "https://api.openai.com/v1"
    assert llm.openai_url("chat/completions", {}) == "https://api.openai.com/v1/chat/completions"
    assert llm.openai_url("embeddings", {}) == "https://api.openai.com/v1/embeddings"
    assert "?" not in llm.openai_url("chat/completions", {})


def test_constant_openai_urls_unchanged():
    # DB を読まない固定値（互換のため残す）。
    assert llm.OPENAI_CHAT_URL == "https://api.openai.com/v1/chat/completions"
    assert llm.OPENAI_EMBED_URL == "https://api.openai.com/v1/embeddings"


@pytest.mark.parametrize("base", [AZURE + "/", AZURE])
def test_azure_base_url_trailing_slash_normalized(base):
    sysset = {"openai_endpoint_kind": "azure", "openai_base_url": base}
    assert llm.openai_base_url(sysset) == AZURE
    assert llm.openai_url("chat/completions", sysset) == AZURE + "/chat/completions"
    assert llm.openai_url("embeddings", sysset) == AZURE + "/embeddings"


def test_api_version_appended_as_query():
    sysset = {"openai_endpoint_kind": "azure", "openai_base_url": AZURE + "/",
              "openai_api_version": "2026-05-01-preview"}
    assert llm.openai_url("chat/completions", sysset) == AZURE + "/chat/completions?api-version=2026-05-01-preview"


def test_api_version_combines_with_existing_query():
    sysset = _custom("https://gw.example.com/v1", openai_api_version="2026-05-01-preview")
    assert llm.openai_url("models?limit=10", sysset) == \
        "https://gw.example.com/v1/models?limit=10&api-version=2026-05-01-preview"


def test_api_version_and_auth_header_ignored_when_kind_is_openai():
    # 本家へ切り替えた後、古い Azure 設定が黙って有効なまま残らない。
    sysset = {"openai_endpoint_kind": "openai", "openai_base_url": AZURE,
              "openai_api_version": "2026-05-01-preview", "openai_auth_header": "api-key"}
    assert llm.openai_api_version(sysset) == ""
    assert llm.openai_auth_header_style(sysset) == "bearer"
    assert "?" not in llm.openai_url("chat/completions", sysset)
    assert llm.openai_headers("sk-x", sysset) == BEARER_HEADERS


def test_valid_explicit_port_accepted():
    assert llm.openai_url("chat/completions", _custom("https://host:8443/v1")) == \
        "https://host:8443/v1/chat/completions"


# ---- 不正な base URL の拒否 ------------------------------------------------------------------

@pytest.mark.parametrize("base", [
    "http://example.internal/v1",                       # 非 https（平文でキーを送らない）
    "http://127.0.0.1:9/v1",                            # ループバックでも http は拒否
    "http://localhost:9/v1",
    "https:///v1",                                      # ホスト空
    "https://user:secret@myres.openai.azure.com/v1",    # userinfo
    AZURE + "?api-version=2024-10-21",                  # クエリ（api-version は別欄）
    AZURE + "#frag",                                    # フラグメント
    "https://host:notaport/v1",
    "https://host:999999/v1",
])
def test_invalid_base_url_rejected(base):
    with pytest.raises(ValueError):
        llm.openai_url("chat/completions", _custom(base))


@pytest.mark.parametrize("base", [
    "https://host.example\\internal\\secret",   # backslash
    "https://host.example internal/v1",         # ASCII 空白
    "https://host.example　internal/v1",    # 全角空白
    "https://host.example\x07internal/v1",      # 制御文字
    "https://host.example／internal/v1",        # 非 ASCII
])
def test_base_url_with_illegal_characters_rejected(base):
    with pytest.raises(ValueError):
        llm.assert_openai_base_url_allowed(base)


@pytest.mark.parametrize("base, secret, via_openai_url", [
    ("https://user:secret-password@myres.openai.azure.com/v1", "secret-password", True),
    ("https://user:s3cr3t@[::1/v1", "s3cr3t", False),    # urlparse 自体が失敗する入力
    ("https://myres.openai.azure.com/openai/v1?leaked_key=sk-should-not-appear", "sk-should-not-appear", False),
])
def test_rejection_error_does_not_leak_secret(base, secret, via_openai_url):
    with pytest.raises(ValueError) as exc:
        if via_openai_url:
            llm.openai_url("chat/completions", _custom(base))
        else:
            llm.assert_openai_base_url_allowed(base)
    assert secret not in str(exc.value)
    assert base not in str(exc.value)


def test_invalid_scheme_error_does_not_leak_path_pseudo_secret():
    base = "http://myres.openai.azure.com/openai/deployments/sk-should-not-leak"
    with pytest.raises(ValueError) as exc:
        llm.openai_url("chat/completions", _custom(base))
    assert "sk-should-not-leak" not in str(exc.value)
    assert llm._redact_url_for_error(base) == "myres.openai.azure.com"
    assert "'myres.openai.azure.com'" in str(exc.value)   # host 表現そのものは repr で出る
    assert "http://myres" not in str(exc.value) and "https://myres" not in str(exc.value)


def test_db_unreachable_falls_back_to_openai_defaults(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("sherpa.store.get_system_settings", _boom)
    assert llm.openai_base_url() == "https://api.openai.com/v1"
    assert llm.openai_endpoint_kind() == "openai"
    assert llm.openai_api_version() == ""
    assert llm.openai_auth_header_style() == "bearer"
    assert llm.openai_headers("sk-x") == BEARER_HEADERS
    assert llm.openai_url("chat/completions") == "https://api.openai.com/v1/chat/completions"


# ---- 保存値の型検査（falsy な非文字列が本家既定 URL へ黙って縮退しない） ---------------------------

_CALLS = {
    "kind": lambda s: llm.openai_endpoint_kind(s),
    "base": lambda s: llm.openai_base_url(s),
    "url": lambda s: llm.openai_url("chat/completions", s),
}
_FALSY = [{}, [], 0, False]


def _typed_cases():
    for bad in _FALSY:
        yield {"openai_endpoint_kind": "azure", "openai_base_url": bad}, ("base", "url")
        yield {"openai_endpoint_kind": "openai", "openai_base_url": bad}, ("kind", "base", "url")
        yield {"openai_base_url": bad}, ("kind", "base")
        yield {"openai_endpoint_kind": bad, "openai_base_url": "https://real.example.com/v1"}, ("kind", "base")
    for bad in (["https://evil.example.com"], {"nested": "v"}, 12345):
        yield {"openai_endpoint_kind": "azure", "openai_base_url": bad}, ("base",)


@pytest.mark.parametrize("sysset, calls", list(_typed_cases()))
def test_non_string_saved_values_raise_instead_of_degrading(sysset, calls):
    # kind=openai でも型検査は判定分岐より先に行う（早期 return で素通りさせない）。
    for name in calls:
        with pytest.raises(ValueError):
            _CALLS[name](sysset)


def test_none_saved_base_url_still_falls_back_to_openai_default():
    # None（真の未設定）は本家既定へ fail-safe（falsy 非文字列との対照）。
    assert llm.openai_base_url({"openai_endpoint_kind": "azure", "openai_base_url": None}) == \
        "https://api.openai.com/v1"


# ---- ヘッダ ----------------------------------------------------------------------------------

@pytest.mark.parametrize("sysset, expected", [
    ({}, BEARER_HEADERS),
    ({"openai_endpoint_kind": "azure", "openai_base_url": AZURE, "openai_auth_header": "api-key"},
     {"api-key": "sk-x", "Content-Type": "application/json"}),
    (_custom("https://gw.example.com/v1", openai_auth_header="something-else"), BEARER_HEADERS),
])
def test_headers(sysset, expected):
    assert llm.openai_headers("sk-x", sysset) == expected


def test_headers_rejects_non_string_key_without_sending():
    # 非文字列キーは dict の repr がヘッダ値へ混入して後でエコーされうる＝送信前に拒否（fail-closed）。
    with pytest.raises(RuntimeError):
        llm.openai_headers({"unexpected": "AZUREKEY-SHOULD-NEVER-BE-SENT-1234567890"}, {})
    with pytest.raises(RuntimeError):
        llm.openai_headers(["also", "not", "a", "string"], {})


# ---- openai_endpoint_kind --------------------------------------------------------------------

@pytest.mark.parametrize("base, expected", [
    (None, "openai"),
    ("https://myres.openai.azure.com/openai/v1/", "azure"),             # 明示未設定は host で推定
    ("https://myres.services.ai.azure.com/openai/v1/", "azure"),
    ("https://openai-compatible.example.com/v1", "custom"),
    # DNS ルートドット・大文字ホストは正規化してから分類する。
    ("https://api.openai.com./v1", "openai"),
    ("https://myres.openai.azure.com./openai/v1", "azure"),
    ("https://myres.services.ai.azure.com./openai/v1", "azure"),
    ("https://api.example.com./v1", "custom"),
    ("https://API.OPENAI.COM/v1", "openai"),
    ("https://API.OPENAI.COM./v1", "openai"),
    ("https://MYRES.OPENAI.AZURE.COM/openai/v1", "azure"),
    ("https://MYRES.OPENAI.AZURE.COM./openai/v1", "azure"),
    ("https://API.EXAMPLE.COM./v1", "custom"),
])
def test_endpoint_kind_classification_when_not_explicit(base, expected):
    sysset = {} if base is None else {"openai_base_url": base}
    assert llm.openai_endpoint_kind(sysset) == expected


def test_endpoint_kind_explicit_overrides_host_heuristic():
    sysset = {"openai_base_url": "https://azure-proxy.internal/openai/v1", "openai_endpoint_kind": "azure"}
    assert llm.openai_endpoint_kind(sysset) == "azure"


def test_endpoint_kind_explicit_openai_ignores_leftover_base_url():
    sysset = {"openai_endpoint_kind": "openai", "openai_base_url": AZURE}
    assert llm.openai_endpoint_kind(sysset) == "openai"
    assert llm.openai_base_url(sysset) == "https://api.openai.com/v1"


def test_endpoint_consistent_openai_kind_never_requires_base_url():
    llm.assert_openai_endpoint_consistent("openai", "")
    llm.assert_openai_endpoint_consistent("openai", "https://leftover.example.com/v1")


def test_endpoint_consistent_non_openai_kind_requires_base_url():
    with pytest.raises(ValueError):
        llm.assert_openai_endpoint_consistent("azure", "")
    with pytest.raises(ValueError):
        llm.assert_openai_endpoint_consistent("custom", "   ")
    llm.assert_openai_endpoint_consistent("azure", AZURE)


# ---- _redact_url_for_error（scheme・path・query・fragment・params を残さない）-----------------

@pytest.mark.parametrize("url, expected", [
    ("https://host/v1?a=1#frag", "host"),
    ("https://host/openai/deployments/sk-should-not-appear-in-path", "host"),
    ("https://host/path;sk-should-not-leak-via-params?q=1#f", "host"),
    ("https://host:8443/v1", "host:8443"),
    ("https://[2001:db8::1]:8443/v1", "[2001:db8::1]:8443"),
    ("https://[2001:db8::1]/v1", "[2001:db8::1]"),
    ("https://[::1/v1", None),     # パース失敗
    ("https:///v1", None),         # ホスト空
])
def test_redact_url_for_error(url, expected):
    assert llm._redact_url_for_error(url) == expected


# ---- ollama_url_fingerprint ------------------------------------------------------------------

def test_ollama_url_fingerprint():
    fp = llm.ollama_url_fingerprint
    assert fp("http://10.0.0.5") == fp("http://10.0.0.5:80")                     # ポート省略の表記ゆれを吸収
    assert llm._redact_url_for_error("http://10.0.0.5") != llm._redact_url_for_error("http://10.0.0.5:80")
    assert fp("http://10.0.0.5:11434") != fp("http://10.0.0.5:11435")
    assert fp("http://user:secret@10.0.0.5:11434") is None
    assert fp("http://10.0.0.5:11434?token=x") is None
    assert fp("http://[::1]:11434") == "[::1]:11434"


# ---- openai_post_json / post_json -----------------------------------------------------------

def test_openai_post_json_raises_before_socket_open_when_blocked(monkeypatch):
    called = []
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda *a, **kw: called.append(1))
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", "test-blocked")
    with pytest.raises(RuntimeError):
        llm.openai_post_json("https://api.openai.com/v1/x", {}, {"a": 1})
    assert called == []


def test_post_json_not_gated_by_openai_block(monkeypatch):
    # Gemini/Ollama と共用する `post_json` は OpenAI の block と無関係に動く。
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda *a, **kw: _Resp())
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", "test-blocked")
    assert llm.post_json("http://localhost:11434/api/chat", {}, {}) == {}


# ---- PreflightRejected（実送信前ガードの共通基底・型による「未送信」判定）--------------------------

def test_openai_io_blocked_raises_preflight_rejected(monkeypatch):
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", "boom")
    with pytest.raises(llm.PreflightRejected):
        llm.assert_openai_io_allowed()


def test_openai_base_url_rejected_raises_preflight_rejected():
    with pytest.raises(llm.PreflightRejected):
        llm.assert_openai_base_url_allowed("http://example.internal/v1")


def test_preflight_rejected_hierarchy():
    # 既存の `except RuntimeError`／`except ValueError`／`except llm.SsrfBlocked` 呼び出し元と互換。
    assert issubclass(llm.PreflightRejected, RuntimeError)
    assert issubclass(llm.PreflightRejected, ValueError)
    exc = llm.PreflightRejected("test")
    assert isinstance(exc, RuntimeError) and isinstance(exc, ValueError)
    assert issubclass(llm.SsrfBlocked, llm.PreflightRejected)
    exc = llm.SsrfBlocked("blocked")
    assert isinstance(exc, llm.PreflightRejected) and isinstance(exc, ValueError)


# ---- endpoint_locality ----------------------------------------------------------------------

@pytest.mark.parametrize("base_url, expected", [
    ("http://10.0.0.5:8000/v1", "on_prem"),
    ("http://192.168.1.20:8000/v1", "on_prem"),
    ("http://localhost:8000/v1", "on_prem"),
    ("http://llm.lan:8000/v1", "on_prem"),
    ("https://api.example.com/v1", "cloud"),
    ("http://127.0.0.1:8000/v1", "on_prem"),
    ("http://169.254.1.1:8000/v1", "on_prem"),
    ("http://[::1]:8000/v1", "on_prem"),
    ("http://llmhost:8000/v1", "on_prem"),               # ドットを含まない裸のホスト名
    ("http://8.8.8.8/v1", "cloud"),
    ("", "cloud"),                                       # 解決できない場合は cloud へ倒す
    (None, "cloud"),
    ("http://100.64.0.5:8000/v1", "on_prem"),            # CGNAT 帯域（RFC 6598）
    ("http://100.127.255.254:8000/v1", "on_prem"),
    ("http://100.63.255.255:8000/v1", "cloud"),          # 帯域の外
    ("http://100.128.0.0:8000/v1", "cloud"),
    ("http://llm.internal.:8000/v1", "on_prem"),         # DNS ルートドットを正規化してから判定
    ("https://api.example.com.:443/v1", "cloud"),
])
def test_endpoint_locality(base_url, expected):
    assert llm.endpoint_locality(base_url) == expected
