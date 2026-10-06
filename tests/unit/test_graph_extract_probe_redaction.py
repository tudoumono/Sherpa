"""`graph_extract` の伏せ字（`_mask_secrets`/`_redact_reflected_urls`/`_safe_detail`/`_probe`/
`_log_masked_exception`）: 上流のエラー本文・例外メッセージに混じった秘密・反射 URL が
利用者向け detail・ログへ出ない契約。マスクしてから切断する順序も固定する。"""
from __future__ import annotations

import io
import json
import urllib.error
from urllib.parse import quote

import pytest

from sherpa.ingest import graph_extract as GE

pytestmark = pytest.mark.unit


def _http_error(status: int, body: dict) -> urllib.error.HTTPError:
    fp = io.BytesIO(json.dumps(body).encode("utf-8"))
    return urllib.error.HTTPError("https://myres.openai.azure.com/openai/v1/chat/completions",
                                  status, "error", {}, fp)


class _Boom(Exception):
    pass


# ===== 1. `_mask_secrets` =====

@pytest.mark.parametrize("text, secret, forbidden", [
    pytest.param("invalid header, saw Authorization: Bearer sk-REALSECRET1234567890",
                 "sk-REALSECRET1234567890", ["sk-REALSECRET1234567890"], id="literal"),
    pytest.param("upstream rejected token PLAINKEY-9988776655443322110099887766-TAIL without further detail",
                 "PLAINKEY-9988776655443322110099887766-TAIL",
                 ["PLAINKEY-9988776655443322110099887766-TAIL"], id="plain-non-sk-key"),
    pytest.param("redirected to https://example/callback?token=abc%2B%2Fsecret%3D",
                 "abc+/secret=", ["abc%2B%2Fsecret%3D"], id="url-encoded-quote"),
    pytest.param("form body contained token=my+secret+key",
                 "my secret key", ["my+secret+key"], id="url-encoded-quote-plus"),
    # 上流が小文字16進・桁ごとに大小混在で echo する形
    pytest.param("redirected to https://example/callback?token=abc%2b%2fsecret%3d",
                 "abc+/secret=", ["abc%2b%2fsecret%3d"], id="lowercase-hex"),
    pytest.param("redirected to https://example/callback?token=abc%2b%2Fsecret%3d",
                 "abc+/secret=", ["abc%2b%2Fsecret%3d"], id="mixed-case-hex"),
])
def test_mask_secrets_masks_secret_in_every_echoed_form(text, secret, forbidden):
    masked = GE._mask_secrets(text, secret)
    for f in forbidden:
        assert f not in masked
    if "sk-REAL" not in text:
        assert "[REDACTED]" in masked


def test_mask_secrets_plain_key_is_protected_only_by_exact_match():
    secret = "PLAINKEY-9988776655443322110099887766-TAIL"
    text = f"upstream rejected token {secret} without further detail"
    assert secret in GE._mask_secrets(text, None)


@pytest.mark.parametrize("text, secret_leak, kept", [
    pytest.param("authorization failed: Bearer abcSECRETxyz,code=invalid_api_key",
                 "abcSECRETxyz", ["code=invalid_api_key", "Bearer"], id="bearer-stops-at-comma"),
    pytest.param("rejected header api-key: azure-secret-value-9876;retry-after=30",
                 "azure-secret-value-9876", ["retry-after=30"], id="api-key-stops-at-semicolon"),
    pytest.param("duplicate key sk-anotherleakedtoken000111",
                 "sk-anotherleakedtoken000111", [], id="sk-token"),
])
def test_mask_secrets_general_patterns_without_secret_arg(text, secret_leak, kept):
    masked = GE._mask_secrets(text, None)
    assert secret_leak not in masked
    for k in kept:
        assert k in masked


def test_mask_secrets_handles_none_secret_and_empty_text_without_crashing():
    assert GE._mask_secrets("", "sk-x") == ""
    assert GE._mask_secrets("plain text, nothing sensitive", None) == "plain text, nothing sensitive"


def test_mask_secrets_masks_asterisk_masked_token_generic_form():
    text = "rejected: partial key abc12****************************wxyz observed"
    masked = GE._mask_secrets(text, None)
    assert "abc12" not in masked
    assert "wxyz" not in masked
    assert "****" not in masked
    assert "rejected: partial key" in masked
    assert "observed" in masked


@pytest.mark.parametrize("secret, text", [
    pytest.param("ABCDEFG-REST-OF-A-LONGER-SECRET-VALUE-TUVWXYZ",
                 "saw ABCDEF in an unrelated log line, nothing to do with UVWXYZ here",
                 id="under-min-len-fragments"),
    pytest.param("sk-proj-REALSECRETVALUE1234567890abcdef",
                 "unrelated log line mentions sk-pro as a generic term, nothing to do with the key",
                 id="sk-pro-six-chars"),
])
def test_mask_secrets_does_not_mask_short_coincidental_fragment(secret, text):
    assert GE._mask_secrets(text, secret) == text


def test_mask_secret_fragments_replaces_all_matching_lengths_not_just_first():
    secret = "ABCDEFGH-MIDDLE-PART-OF-SECRET-TUVWXYZ"   # 接頭辞8字・接尾辞7字
    text = "log mentions prefix ABCDEFGH in one place, and suffix TUVWXYZ in another"
    masked = GE._mask_secrets(text, secret)
    assert "ABCDEFGH" not in masked
    assert "TUVWXYZ" not in masked
    assert "log mentions prefix" in masked
    assert "in another" in masked


# ===== 2. `_safe_detail`: マスクしてから切断する =====

def test_safe_detail_masks_before_truncating_generic_exception():
    secret = "PLAINKEY-9988776655443322110099887766-STRADDLE-TAIL-VALUE"
    detail = GE._safe_detail(_Boom("x" * 260 + secret + "y" * 30), secret=secret)
    assert len(detail) <= GE._DETAIL_MAX_LEN_GENERIC
    assert secret not in detail
    assert secret[:15] not in detail, f"secret の断片が残っている: {detail!r}"


def test_safe_detail_masks_before_truncating_http_error():
    secret = "PLAINKEY-HTTPPATH-1122334455667788990011223344556677889900"
    exc = _http_error(401, {"error": {"message": "x" * 360 + secret + "y" * 30}})
    detail = GE._safe_detail(exc, secret=secret)
    assert len(detail) <= GE._DETAIL_MAX_LEN_HTTP
    assert secret not in detail
    assert secret[:15] not in detail, f"secret の断片が残っている: {detail!r}"


def test_safe_detail_does_not_raise_on_non_string_secret():
    assert isinstance(GE._safe_detail(_Boom("some failure"), secret={"not": "a-string"}), str)


@pytest.mark.parametrize("message", [
    pytest.param("deployment not found", id="short"),
    pytest.param("この本文はダミーです。実際のエラーメッセージを模した長文です。" * 20, id="long-over-limit"),
])
def test_safe_detail_keeps_deployment_not_found_hint(monkeypatch, message):
    from sherpa import llm
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda *a, **kw: "azure", raising=False)
    exc = _http_error(404, {"error": {"code": "DeploymentNotFound", "message": message}})
    detail = GE._safe_detail(exc)
    assert detail.endswith(GE._DEPLOYMENT_NOT_FOUND_HINT)
    assert len(detail) <= GE._DETAIL_MAX_LEN_HTTP


# ===== 2d. 反射 URL のマスク =====
# 「入力に含めた URL の部分文字列は一切出力に残らない」を事例横断で固定する。

_SECRET_BASE = "https://myres.openai.azure.com/openai/deployments/my-secret-deploy"
_SECRET_FULL = _SECRET_BASE + "/chat/completions?api-version=2024-01-01"


def _double_quote(s: str) -> str:
    return quote(quote(s, safe=""), safe="")


_DEPLOY = ["my-secret-deploy", "api-version"]
_REQ_TARGET = ["my-secret-deploy", "deployments"]
_QUOTE_PREPASS = ["my-secret", "TOPSECRET", "deployments"]
_LEAK_CASES = [
    pytest.param(f"upstream rejected request to {_SECRET_BASE}/chat/completions?api-version=2024-01-01",
                 ["my-secret-deploy", "api-version", "chat"], id="plain-full-url"),
    pytest.param("redirect target: " + quote(_SECRET_BASE, safe=""), ["my-secret-deploy"], id="encoded-base-url"),
    pytest.param("upstream echoed request to " + quote(_SECRET_FULL, safe="") + " and rejected it",
                 ["my-secret-deploy", "api-version", "2024-01-01", "chat"],
                 id="encoded-full-url-not-just-base-prefix"),
    pytest.param("upstream echoed request to " + quote(_SECRET_FULL, safe="").lower() + " and rejected it",
                 _DEPLOY, id="encoded-lowercase-hex"),
    pytest.param("upstream echoed request to "
                 + quote(_SECRET_FULL, safe="").replace("%2F", "%2f").replace("%3D", "%3d") + " and rejected it",
                 _DEPLOY, id="encoded-mixed-case-hex"),
    pytest.param("upstream echoed https%3A//myres.openai.azure.com/openai/deployments/"
                 "my-secret-deploy?api-version=2024-01-01 and rejected",
                 _DEPLOY, id="mixed-plain-and-encoded-scheme-separator"),
    pytest.param("unauthorized for deployment %2Fopenai%2Fdeployments%2Fmy-secret-deploy"
                 "?api-version=2024-01-01 rejected", _DEPLOY, id="schemeless-encoded-path-fragment"),
    pytest.param(f"upstream echoed {_double_quote(_SECRET_FULL)} and rejected", _DEPLOY, id="double-encoded-url"),
    pytest.param("upstream echoed https%3A%2F%2Fmyres.openai.azure.com%2Fopenai%2Fdeployments"
                 "%2Fmy-secret-deploy?api-version=2024-01-01 and rejected", _DEPLOY,
                 id="literal-query-tail-after-encoded-path"),
    pytest.param("upstream https%3A%2F%2Fmyres.openai.azure.com%2Fopenai%2Fdeployments"
                 "%2Fmy-secret-deploy%3Fapi-version%3D2024-01-01 rejected", _DEPLOY,
                 id="fully-encoded-query-with-percent-3F"),
    pytest.param("upstream https%3A//host.example:8443/openai/deployments/my-secret-deploy?api-version=2024 rejected",
                 ["my-secret-deploy", "api-version", "8443"], id="encoded-scheme-with-port"),
    pytest.param("upstream https%3A%2F%2F%5B2001%3Adb8%3A%3A1%5D%3A8443%2Fopenai%2Fdeployments"
                 "%2Fmy-secret-deploy rejected", ["my-secret-deploy", "2001"], id="encoded-scheme-with-ipv6-host"),
    pytest.param("upstream https%3A%2F%2Fhost.example%2Fopenai%3Fredirect=https://other.example/secret-path rejected",
                 ["other.example", "secret-path", "redirect"], id="encoded-scheme-with-slash-in-query-value"),
    pytest.param("see https://host.example/openai?redirect=https://other.example/secret-path next",
                 ["other.example", "secret-path", "redirect"], id="plain-url-with-embedded-url-in-query"),
    pytest.param("see 'https://host.example/secret'. next", ["secret"], id="closing-quote-preserved-but-secret-masked"),
    pytest.param("upstream https://host.example%2Fopenai%2Fdeployments%2Fmy-secret-deploy rejected",
                 ["my-secret-deploy"], id="plain-scheme-with-encoded-path-mixed"),
    pytest.param("https://host.example/内部/deployments/秘密?api-version=2024 failed",
                 ["秘密", "内部", "api-version"], id="unicode-path-and-query"),
    pytest.param("connect to https://user@host.example/path;param,x?a=1 next",
                 ["user", "path", "param", "a=1"], id="userinfo-and-reserved-punctuation-in-url"),
    pytest.param("upstream %252Fmy-secret-deploy rejected", ["my-secret-deploy"], id="single-double-encoded-slash-fragment"),
    pytest.param("upstream %2F内部%2F秘密 rejected", ["秘密", "内部"], id="unicode-segment-encoded-path-fragment"),
    pytest.param("upstream %2Fopenai;param%2Fmy-secret-deploy rejected", ["my-secret-deploy", "param"],
                 id="semicolon-interrupted-encoded-path-fragment"),
    pytest.param("InvalidURL: /openai/deployments/my-secret-deploy contains control characters",
                 _REQ_TARGET, id="plain-multi-segment-request-target"),
    pytest.param("C:%2FWindows%2FSystem32", ["Windows", "System32"], id="windows-path-with-encoded-slash-masked-tradeoff"),
    pytest.param("%2Fteam%2Fmember@example.com", ["team", "member@example.com"],
                 id="email-like-encoded-slash-masked-tradeoff"),
    pytest.param("InvalidURL: '/openai/deployments/my-secret-deploy' contains control characters",
                 _REQ_TARGET, id="quoted-plain-multi-segment-request-target"),
    pytest.param("params%3Fapi-version%3D2024-01-01 rejected", ["api-version", "2024-01-01"],
                 id="percent-3F-3D-only-no-percent-2F"),
    pytest.param("upstream host%3A8443%3Fapi-version%3D2024 rejected", ["8443", "api-version", "2024"],
                 id="percent-3A-3F-only-no-percent-2F"),
    pytest.param("unauthorized for openai%2Fdeployments%2Fmy-secret-deploy rejected", _REQ_TARGET,
                 id="encoded-fragment-directly-after-word-char"),
    pytest.param("connect failed: postgresql://admin:db-secret@db.internal/app timeout",
                 ["admin", "db-secret", "app"], id="postgresql-dsn"),
    pytest.param("connect failed: redis://user:pass@cache.internal:6379/0 timeout", ["user", "pass", "6379"],
                 id="redis-dsn"),
    pytest.param("connect failed: bolt://neo4j:s3cr3t@graph.internal:7687 timeout", ["neo4j", "s3cr3t", "7687"],
                 id="bolt-dsn"),
    pytest.param("InvalidURL: '/openai/deployments/my-secret deploy' contains control characters",
                 ["my-secret", "deploy", "deployments"], id="quoted-request-target-with-embedded-space"),
    pytest.param("「(https://host.example/openai/deployments/my-secret-deploy)。」は無効です", _REQ_TARGET,
                 id="fullwidth-quote-and-paren-wrapped-url"),
    pytest.param("rejected 【/openai/deployments/my-secret-deploy】 request", _REQ_TARGET,
                 id="fullwidth-bracket-wrapped-request-target"),
    pytest.param("unauthorized for /openai//deployments/my-secret-deploy rejected", _REQ_TARGET,
                 id="multi-segment-path-with-empty-segment"),
    pytest.param("upstream //host.internal/openai/my-secret-path rejected", ["host.internal", "my-secret-path"],
                 id="multi-segment-path-leading-double-slash"),
    pytest.param("contact mailto:secret.deploy@internal.example for access", ["secret.deploy", "internal.example"],
                 id="mailto-scheme-without-slashes"),
    pytest.param("payload data:text/plain;base64,c2VjcmV0LWRlcGxveQ== embedded", ["c2VjcmV0LWRlcGxveQ", "text/plain"],
                 id="data-scheme-with-payload"),
    pytest.param("reading file:/etc/my-secret-deploy failed", ["my-secret-deploy"], id="file-scheme-single-slash"),
    pytest.param("don't call '/openai/deployments/my-secret TOPSECRET' now", _QUOTE_PREPASS,
                 id="quote-prepass-apostrophe-before-real-quote"),
    pytest.param("『outer 「/openai/deployments/my-secret TOPSECRET」 tail』", _QUOTE_PREPASS,
                 id="quote-prepass-nested-fullwidth-quotes"),
    pytest.param("outer '/openai/deployments/my-secret TOPSECRET' tail", _QUOTE_PREPASS,
                 id="quote-prepass-single-quote-baseline"),
    pytest.param('"outer \'/openai/deployments/my-secret TOPSECRET\' tail"', _QUOTE_PREPASS,
                 id="quote-prepass-nested-double-outer-single-inner"),
    pytest.param("『outer \"/openai/deployments/my-secret TOPSECRET\" tail』", _QUOTE_PREPASS,
                 id="quote-prepass-nested-fullwidth-outer-double-inner"),
    pytest.param("//user:PASS@host.internal?token=abc123 rejected", ["PASS", "token", "abc123"],
                 id="protocol-relative-userinfo-and-query-no-path"),
    pytest.param("//host.internal#TOPSECRET rejected", ["TOPSECRET"], id="protocol-relative-fragment-no-path"),
    pytest.param("reading data:,,secretpayload now", ["secretpayload"], id="explicit-scheme-punctuation-then-alnum-body"),
    pytest.param("reading data:,, now", ["data:,,"], id="explicit-scheme-punctuation-only-body-still-masked"),
    pytest.param("outer '/openai's/deployments/my-secret TOPSECRET' tail", _QUOTE_PREPASS,
                 id="quote-prepass-apostrophe-inside-quoted-content"),
    pytest.param("『『/openai/deployments/my-secret TOPSECRET』』", _QUOTE_PREPASS,
                 id="quote-prepass-same-species-nested-fullwidth-quotes"),
    pytest.param("エラー「/openai/deployments/my-secret TOPSECRET」でした", _QUOTE_PREPASS,
                 id="fullwidth-quote-opener-preceded-by-word-char"),
    pytest.param("error「/openai/deployments/my-secret TOPSECRET」occurred", _QUOTE_PREPASS,
                 id="fullwidth-quote-opener-preceded-by-ascii-word-char"),
    pytest.param("'field0' '/openai/deployments/my-secret TOPSECRET' tail", _QUOTE_PREPASS,
                 id="quote-prepass-independent-same-species-spans-merged-ascii"),
    pytest.param("「field0」 「/openai/deployments/my-secret TOPSECRET」 tail", _QUOTE_PREPASS,
                 id="quote-prepass-independent-same-species-spans-merged-fullwidth"),
    pytest.param("'field0''/openai/deployments/my-secret TOPSECRET' tail", _QUOTE_PREPASS,
                 id="quote-prepass-independent-same-species-spans-merged-ascii-no-whitespace"),
    pytest.param("「field0」「/openai/deployments/my-secret TOPSECRET」tail", _QUOTE_PREPASS,
                 id="quote-prepass-independent-same-species-spans-merged-fullwidth-no-whitespace"),
    pytest.param("outer '%2''Fopenai/deployments/my-secret TOPSECRET' tail", _QUOTE_PREPASS,
                 id="quote-boundary-splits-percent-escape-sequence-ascii"),
    pytest.param("outer 「%2」「Fopenai/deployments/my-secret TOPSECRET」 tail", _QUOTE_PREPASS,
                 id="quote-boundary-splits-percent-escape-sequence-fullwidth"),
]



_NO_OP_CASES = [
    pytest.param("date: 2026%2F08%2F26 confirmed", id="date-with-encoded-slash"),
    pytest.param("the value is 20260826%2F01%2F02 as-is", id="date-like-value-with-encoded-slash"),
    pytest.param("ratio: 50%2F100 confirmed", id="ratio-with-encoded-slash"),
    pytest.param("ratio: 50%2F100%2F200 confirmed", id="ratio-three-part-with-encoded-slash"),
    pytest.param("date: ２０２６%2F０８%2F２６ confirmed", id="date-with-fullwidth-digits-and-encoded-slash"),
    pytest.param("これは通常の日本語の文章です。特にURLは含まれていません。", id="normal-japanese-prose"),
    pytest.param("The request failed due to a network timeout while contacting the server.",
                id="normal-english-prose"),
    pytest.param("see /tmp for details", id="single-segment-unix-path-stays"),
    pytest.param("see /tmp/ for details", id="single-segment-unix-path-with-trailing-slash-stays"),
    pytest.param("contact us at team.member@example.com for help", id="normal-email-stays"),
    pytest.param("open C:\\Users\\test\\file.txt please", id="windows-path-backslash-stays"),
    pytest.param("open C:/Users/test/file.txt please", id="windows-path-forwardslash-stays"),
    pytest.param("invalid_api_key: the key you provided is not valid", id="no-url-present"),
    pytest.param("meeting at 12:30 today", id="plain-time-not-masked-as-scheme"),
    pytest.param("注: この処理には時間がかかります", id="japanese-colon-prefix-not-masked-as-scheme"),
    pytest.param("she said 'this is fine' and left", id="quoted-plain-prose-no-url-indicator"),
    pytest.param("open \\\\host\\share\\file.txt please", id="windows-unc-path-stays"),
    pytest.param("metadata:value seen", id="scheme-name-suffix-of-larger-word-not-masked"),
    pytest.param("notdata:payload seen", id="scheme-name-suffix-of-larger-word-not-masked-2"),
    pytest.param("profile:/etc/config seen", id="file-scheme-suffix-of-larger-word-not-masked"),
    pytest.param(
        "don't forget to check '/no such indicator here' either",
        id="quote-prepass-quoted-content-without-indicator-stays"),
]



@pytest.mark.parametrize("text, forbidden", _LEAK_CASES)
def test_redact_reflected_urls_leaves_no_url_substring_in_output(text, forbidden):
    masked = GE._redact_reflected_urls(text, _SECRET_BASE)
    for substr in forbidden:
        assert substr not in masked, f"{substr!r} が漏洩している: {masked!r}"


@pytest.mark.parametrize("text", _NO_OP_CASES)
def test_redact_reflected_urls_does_not_over_mask(text):
    assert GE._redact_reflected_urls(text, None) == text


_AZ = "https://myres.openai.azure.com/v1"


@pytest.mark.parametrize("text, base_url, expected", [
    # 地の文と結合した日付はトークン全体が伏せられる（許容するトレードオフ）
    pytest.param("日付は2026%2F08%2F26です", None, "[URL]", id="date-glued-to-prose"),
    pytest.param("see //README.md for details", None, "see [URL] for details", id="dotted-double-slash-1"),
    pytest.param("bumped to //version.2 today", None, "bumped to [URL] today", id="dotted-double-slash-2"),
    pytest.param("詳細は https://myres.openai.azure.com/openai/deployments/my-secret-deploy をご確認ください。",
                 _AZ, "詳細は myres.openai.azure.com をご確認ください。", id="trailing-punct-japanese"),
    pytest.param("See https://myres.openai.azure.com/v1/deployments/my-secret-deploy.",
                 _AZ, "See myres.openai.azure.com.", id="trailing-punct-period"),
    # `;`/`!` は句読点として再付加しない（URL データの取りこぼし防止）
    pytest.param("https://host.example/path?token=;;; end", None, "host.example end", id="query-semicolons"),
    pytest.param("https://host.example/path#!!! end", None, "host.example end", id="fragment-bangs"),
    pytest.param("https://host/path)。", None, "host)。", id="closing-paren-fullwidth-period"),
    pytest.param("https://host/path.... next", None, "host next", id="overlong-trailing-cluster"),
    pytest.param("https://example.com/path）をご覧ください。詳細は別紙を参照。",
                 _AZ, "example.com。", id="trailing-prose-without-whitespace-swallowed"),
    pytest.param("https://host.example/内部/deployments/秘密?api-version=2024 failed）続報は別途。",
                 _AZ, "host.example failed）続報は別途。", id="unicode-url-then-space-keeps-sentence"),
    pytest.param("connect to https://[2001:db8::1]/v1 failed", _AZ,
                 "connect to [2001:db8::1] failed", id="ipv6-with-path"),
    pytest.param("see https://[2001:db8::1] for details.", _AZ,
                 "see [2001:db8::1] for details.", id="ipv6-without-path"),
    pytest.param("see [https://host/path] for details", None, "see [host] for details", id="outer-brackets-pair"),
    pytest.param("「(https://host)。」", None, "「[URL]」", id="fullwidth-quote-paren-nesting"),
    pytest.param("see 【https://host/path】 for details", None, "see 【host】 for details",
                 id="fullwidth-brackets-pair"),
    pytest.param("don't forget to check https://host.example/path afterwards", None,
                 "don't forget to check host.example afterwards", id="unclosed-quote-untouched"),
    pytest.param("see https://other-internal-gw.example.com:8443/secret/path?token=abc for details", _AZ,
                 "see other-internal-gw.example.com:8443 for details", id="url-not-matching-base-url"),
])
def test_redact_reflected_urls_exact_output(text, base_url, expected):
    assert GE._redact_reflected_urls(text, base_url) == expected


_AZ_SYS = {"openai_endpoint_kind": "azure", "openai_base_url": _SECRET_BASE}
_GW_BASE = "https://gw.example.com/v1/internal/route"
_GW_SYS = {"openai_endpoint_kind": "custom", "openai_base_url": _GW_BASE}


@pytest.mark.parametrize("kind, message, sys_s, forbidden", [
    pytest.param("http", "bad request: POST https://myres.openai.azure.com/openai/deployments/my-secret-deploy"
                 "/chat/completions?api-version=2024-01-01 rejected", _AZ_SYS,
                 ["my-secret-deploy", "api-version"], id="http-plain-url"),
    pytest.param("generic", "connection to https://gw.example.com/v1/internal/route?key=leak failed",
                 {"openai_endpoint_kind": "custom", "openai_base_url": "https://gw.example.com/v1"},
                 ["/internal/route", "key=leak"], id="generic-plain-url"),
    pytest.param("http", f"upstream echoed {quote(_SECRET_FULL, safe='')} and rejected it", _AZ_SYS,
                 ["my-secret-deploy", "api-version", "2024-01-01"], id="http-encoded-url"),
    pytest.param("generic", f"connection to {quote(_GW_BASE + '?key=leak&api-version=2024-01-01', safe='')} failed",
                 _GW_SYS, ["/internal/route", "key=leak", "api-version"], id="generic-encoded-url"),
])
def test_safe_detail_masks_reflected_base_url(kind, message, sys_s, forbidden):
    exc = _http_error(400, {"error": {"message": message}}) if kind == "http" else _Boom(message)
    detail = GE._safe_detail(exc, system_settings=sys_s)
    for f in forbidden:
        assert f not in detail


def test_safe_detail_passes_full_untruncated_text_to_redact_reflected_urls(monkeypatch):
    # 正規表現は末尾切断に頑健で、境界をまたぐ入力だけでは「切断が先」の mutation を検出できない
    # ＝spy で `_redact_reflected_urls` が切断前の全長を受け取ることを直接固定する。
    raw_message = "x" * 260 + quote(_SECRET_FULL, safe="") + "y" * 30
    assert len(raw_message) > GE._DETAIL_MAX_LEN_GENERIC

    seen_lengths: list[int] = []
    real_redact = GE._redact_reflected_urls

    def _spy_redact(text, base_url):
        seen_lengths.append(len(text))
        return real_redact(text, base_url)

    monkeypatch.setattr(GE, "_redact_reflected_urls", _spy_redact)
    detail = GE._safe_detail(_Boom(raw_message), system_settings=_AZ_SYS)

    assert seen_lengths, "_redact_reflected_urls が呼ばれなかった"
    assert seen_lengths[0] > GE._DETAIL_MAX_LEN_GENERIC, "切断がマスクより先に走っている"
    assert len(detail) <= GE._DETAIL_MAX_LEN_GENERIC
    assert "my-secret-deploy" not in detail
    assert "api-version" not in detail


# ===== 3. `_probe` =====

_KEY = "sk-REALSECRET1234567890abcdef"
_PARTIAL_KEY = "AbCd1234" + "X" * 44 + "Zz99"
_KEY_HINT = ("Incorrect API key provided: {echo}. You can find your API key at "
             "https://platform.openai.com/account/api-keys.")
_PROBE_CASES = [
    pytest.param(
        _http_error(401, {"error": {"message": f"invalid header, saw Authorization: Bearer {_KEY}"}}),
        {"provider": "openai", "key": _KEY, "model": "gpt-5.5"},
        [_KEY], ["401"], id="http-configured-key"),
    pytest.param(
        _http_error(403, {"error": {"message": "blocked request with Authorization: Bearer "
                                               "some-upstream-proxy-internal-token-xyz"}}),
        {"provider": "openai", "key": "sk-configured-key-not-leaked", "model": "gpt-5.5"},
        ["some-upstream-proxy-internal-token-xyz"], ["Bearer"], id="http-bearer-not-configured-key"),
    pytest.param(
        _http_error(401, {"error": {"message": 'rejected header api-key: "azure-secret-value-9876"'}}),
        {"provider": "openai", "key": "sk-unrelated", "model": "gpt-5.5"},
        ["azure-secret-value-9876"], [], id="http-api-key-header"),
    pytest.param(
        _http_error(400, {"error": {"message": "duplicate key sk-anotherleakedtoken000111"}}),
        {"provider": "openai", "key": "sk-configured", "model": "gpt-5.5"},
        ["sk-anotherleakedtoken000111"], [], id="http-sk-token"),
    pytest.param(
        RuntimeError(f"connection reset while sending Authorization: Bearer {_KEY}"),
        {"provider": "openai", "key": _KEY, "model": "gpt-5.5"},
        [_KEY], [], id="generic-configured-key"),
    pytest.param(
        RuntimeError("gateway error, upstream sent Authorization: Bearer some-other-token-value-222333"),
        {"provider": "openai", "key": "sk-configured-key", "model": "gpt-5.5"},
        ["some-other-token-value-222333"], [], id="generic-bearer-not-configured-key"),
    # 実機観測: 先頭8字＋アスタリスク約60個＋末尾4字の部分マスク echo
    pytest.param(
        _http_error(401, {"error": {"code": "invalid_api_key", "message": _KEY_HINT.format(
            echo="AbCd1234" + "*" * 60 + "Zz99")}}),
        {"provider": "openai", "key": _PARTIAL_KEY, "model": "gpt-5.5"},
        ["AbCd1234", "Zz99", "****", _PARTIAL_KEY], [], id="partial-masked-key-echo"),
    pytest.param(
        _http_error(401, {"error": {"code": "invalid_api_key", "message": _KEY_HINT.format(
            echo="AbCd1234 **** **** Zz99")}}),
        {"provider": "openai", "key": _PARTIAL_KEY, "model": "gpt-5.5"},
        ["AbCd1234", "Zz99", "****", _PARTIAL_KEY],
        ["401", "invalid_api_key", "You can find your API key at"], id="spaced-partial-masked-key-echo"),
    pytest.param(
        _http_error(401, {"error": {"code": "invalid_api_key", "message": _KEY_HINT.format(
            echo="ab12****************cd34")}}),
        {"provider": "openai", "key": "totally-different-key-not-in-message", "model": "gpt-5.5"},
        ["ab12", "cd34"], ["401", "invalid_api_key", "You can find your API key at"],
        id="partial-masked-echo-with-unrelated-configured-key"),
    pytest.param(
        _http_error(500, {"error": {"message": "internal error"}}),
        {"provider": "ollama", "url": "http://localhost:11434", "model": "qwen2.5"},
        [], ["500"], id="no-key-in-cfg"),
    pytest.param(
        _http_error(400, {"error": {"message": "bad request to https://myres.openai.azure.com/openai/"
                                               "deployments/my-secret-deploy/chat/completions?api-version=2024-01-01"}}),
        {"provider": "openai", "key": "sk-x", "model": "gpt-5.5", "openai_endpoint_override": _AZ_SYS},
        ["my-secret-deploy", "api-version"], [], id="reflected-base-url-via-endpoint-override"),
]


@pytest.mark.parametrize("exc, cfg, forbidden, required", _PROBE_CASES)
def test_probe_masks_secrets_and_keeps_classification(monkeypatch, exc, cfg, forbidden, required):
    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr(GE, "complete_json", _raise)
    ok, detail = GE._probe(cfg)
    assert ok is False
    for f in forbidden:
        assert f not in detail
    for r in required:
        assert r in detail


def test_probe_ok_path_never_calls_mask_secrets(monkeypatch):
    calls = []
    monkeypatch.setattr(GE, "_mask_secrets", lambda text, secret: (calls.append((text, secret)), text)[1])
    monkeypatch.setattr(GE, "complete_json", lambda *a, **k: '{"ok":true}')
    ok, detail = GE._probe({"provider": "openai", "key": "sk-x", "model": "gpt-5.5"})
    assert ok is True
    assert detail == ""
    assert calls == []


# ===== 4. 性能・堅牢性 =====

def test_mask_quoted_url_spans_handles_large_adversarial_input_without_hanging():
    import time

    text = "'x " * 100000   # 閉じない引用符の大量反復（指標なし）
    start = time.monotonic()
    result = GE._redact_reflected_urls(text, None)
    elapsed = time.monotonic() - start
    assert result == text, "指標の無い入力なので無加工のはず"
    assert elapsed < 10.0, f"想定より大幅に遅い（O(n^2) 退化やハングの疑い）: {elapsed:.2f}s"


def test_mask_quoted_url_spans_still_masks_indicator_within_deeply_nested_input():
    secret = "TOPSECRET-DEEP-NESTING-VALUE"
    text = f"outer '/openai/deployments/{secret}' " + ("'x " * 1000)
    masked = GE._redact_reflected_urls(text, None)
    assert secret not in masked, f"{secret!r} が漏洩している: {masked!r}"


def _nest_alternating_quotes(inner: str, depth: int) -> str:
    # 同種を連続させると貪欲マッチで1階層に併合されるため ASCII/全角を交互にする
    text = inner
    for i in range(depth):
        text = f"'{text}'" if i % 2 == 0 else f"「{text}」"
    return text


def test_mask_quoted_url_spans_masks_secret_beyond_nesting_depth_limit_fail_closed():
    # 上限ちょうどは通常の対付け形・上限超過は外側 cap 段だけ残して内側を1個の [URL] に伏せる。
    # 出力の厳密な形まで見る（`len(stack) - 1` の off-by-one は secret の不在だけでは検出できない）。
    # cap+2 は交互構成の周期で cap と区別できないため、不一致の固定は cap+1 のみ。
    secret = "TOPSECRET-BEYOND-DEPTH-LIMIT"
    content = f"/openai/deployments/my-secret {secret} tail"
    cap = GE._MAX_QUOTE_NESTING_DEPTH

    masked_at_cap = GE._redact_reflected_urls(_nest_alternating_quotes(content, cap), None)
    expected_at_cap = _nest_alternating_quotes("[URL]", cap)
    assert masked_at_cap == expected_at_cap, f"上限ちょうどの段で通常の対付け形から外れている: {masked_at_cap!r}"

    for depth in (cap, cap + 1, cap + 2):
        masked = GE._redact_reflected_urls(_nest_alternating_quotes(content, depth), None)
        assert secret not in masked, f"depth={depth}: {secret!r} が漏洩している: {masked!r}"
        if depth > cap:
            pre, _, post = masked.partition("[URL]")
            assert len(pre) == cap and len(post) == cap, (
                f"depth={depth}: 上限超過後に外側で生き残る引用符の段数が cap と一致しない: {masked!r}")
        if depth == cap + 1:
            assert masked != expected_at_cap, f"depth={depth}: 上限超過時の出力が上限ちょうどと区別できない: {masked!r}"


def test_mask_quoted_url_spans_masks_remainder_when_no_enclosing_frame_can_rescue(monkeypatch):
    # 上限を 0 に下げ、外側に完了する引用符区間が無い状況で fail-closed 処理だけが守ることを固定する。
    monkeypatch.setattr(GE, "_MAX_QUOTE_NESTING_DEPTH", 0)
    secret = "TOPSECRET"
    masked = GE._redact_reflected_urls(f"outer '/openai/deployments/my-secret {secret}' tail", None)
    assert secret not in masked, f"{secret!r} が漏洩している: {masked!r}"


def test_http_detail_caps_error_body_read_size():
    # 上限超過の本文は切り詰められて壊れた JSON になり、`HTTP {code}` へ縮退する。
    huge_message = "x" * (GE._HTTP_ERROR_BODY_MAX_BYTES * 2)
    body = json.dumps({"error": {"message": huge_message}}).encode("utf-8")
    assert len(body) > GE._HTTP_ERROR_BODY_MAX_BYTES
    exc = urllib.error.HTTPError("https://example.com", 400, "error", {}, io.BytesIO(body))
    detail, hint = GE._http_detail(exc)
    assert hint is None
    assert detail == "HTTP 400"
    assert huge_message not in detail


# ===== `_log_masked_exception` =====

class _FakeLogger:
    def __init__(self):
        self.records: list = []

    def warning(self, fmt, *args):
        self.records.append(fmt % args)


def test_log_masked_exception_masks_secret_and_includes_exception_type():
    log = _FakeLogger()
    secret = "sk-REALSECRET1234567890"
    GE._log_masked_exception(log, "test-context", RuntimeError(f"boom: {secret}"), secret)
    assert len(log.records) == 1
    assert secret not in log.records[0]
    assert "RuntimeError" in log.records[0]
    assert "test-context" in log.records[0]


def test_log_masked_exception_generic_pattern_without_secret_arg():
    log = _FakeLogger()
    GE._log_masked_exception(log, "ctx", RuntimeError("Authorization: Bearer sk-abcdefgh12345"))
    assert "Bearer [REDACTED]" in log.records[0] or "REDACTED" in log.records[0]


def test_log_masked_exception_non_string_secret_falls_back_to_none_without_raising():
    log = _FakeLogger()
    GE._log_masked_exception(log, "ctx", RuntimeError("plain message, no secret here"),
                             {"unexpected": "dict-not-a-string"})
    assert len(log.records) == 1
    assert "RuntimeError" in log.records[0]


def test_log_masked_exception_non_string_secret_str_form_is_masked():
    # 非文字列 secret が `Bearer {dict}` として echo された実漏洩の再現
    log = _FakeLogger()
    corrupted_secret = {"unexpected": "AZUREKEY-SHOULDNOTLEAK-1234567890"}
    GE._log_masked_exception(log, "ctx", RuntimeError(f"invalid header value: Bearer {corrupted_secret}"),
                             corrupted_secret)
    assert len(log.records) == 1
    assert "AZUREKEY-SHOULDNOTLEAK-1234567890" not in log.records[0]
    assert "REDACTED" in log.records[0]


def test_log_masked_exception_masking_failure_is_swallowed_and_does_not_leak_via_context(monkeypatch):
    # マスク処理自体が失敗しても握り潰してプレースホルダで残す（例外連鎖で秘密が復活しない）。
    def _boom_mask(text, secret):
        raise TypeError("simulated masking bug")

    monkeypatch.setattr(GE, "_mask_secrets", _boom_mask)
    log = _FakeLogger()
    secret_in_original = "sk-SHOULDNOTLEAK1234567890"
    GE._log_masked_exception(log, "ctx", RuntimeError(f"boom: {secret_in_original}"), "irrelevant")
    assert len(log.records) == 1
    assert secret_in_original not in log.records[0]
