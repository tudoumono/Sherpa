"""`graph_extract._mask_secrets`/`_safe_detail` の単体テストと、その呼び出し元（`_probe`）での
秘密漏出の回帰テスト。旧・意味層フル抽出（`_extract_doc`／`concept_propose.propose()`）経由の
回帰テストは GRAPH-SRC（2026-09-04）でその消費者ごと撤去済み。

`_http_detail`/`_error_detail` は上流のエラー本文・例外メッセージをほぼそのまま整形するため、
上流プロキシ/ゲートウェイやプロバイダ自身がリクエストヘッダやキー（の断片）をエラー本文へ echo
すると、そのまま利用者向けの detail（設定画面の接続テスト・health 疎通確認等）へ漏れうる。
実機では OpenAI が「先頭数文字＋アスタリスク列＋末尾数文字」でキーを部分マスクして
echo する形が観測されている（完全一致・URL エンコード形のどれにも該当しない）。`_safe_detail` が
**マスクしてから切断する**順序を保証する単一の入口であること、一般パターン（`Bearer <値>`・
`api-key: <値>`・`sk-` 形式のトークン・アスタリスク列で部分マスクされたトークン）が値の後ろに続く
分類情報（`code=...` 等）を消さないこと、secret の接頭辞/接尾辞断片（6文字以上）も伏せられることを
確認する。
"""
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


# ===== 1. `_mask_secrets`（純関数）の単体テスト =====

def test_mask_secrets_masks_literal_secret():
    text = "invalid header, saw Authorization: Bearer sk-REALSECRET1234567890"
    masked = GE._mask_secrets(text, "sk-REALSECRET1234567890")
    assert "sk-REALSECRET1234567890" not in masked


def test_mask_secrets_masks_non_sk_prefixed_plain_key():
    """`sk-` 形式でも `Bearer`/`api-key` パターンでもない素のキー文字列は、完全一致（`secret` 引数）
    だけが頼り。一般パターンに引っかからないことを逆に確認する。"""
    secret = "PLAINKEY-9988776655443322110099887766-TAIL"
    text = f"upstream rejected token {secret} without further detail"
    masked = GE._mask_secrets(text, secret)
    assert secret not in masked
    assert "[REDACTED]" in masked
    # 一般パターン（sk-/Bearer/api-key）には該当しないので、secret 引数を渡さなければ守られない
    # ことも確認する（完全一致 redaction が実際に効いていることの対照実験）。
    assert secret in GE._mask_secrets(text, None)


def test_mask_secrets_masks_url_encoded_form_with_quote():
    """`urllib.parse.quote(secret, safe="")` 形式（`+`/`/`/`=` を含むキーが URL エンコードされた形）
    が本文に混じっていても伏せられること。"""
    secret = "abc+/secret="
    encoded = "abc%2B%2Fsecret%3D"
    text = f"redirected to https://example/callback?token={encoded}"
    masked = GE._mask_secrets(text, secret)
    assert encoded not in masked
    assert "[REDACTED]" in masked


def test_mask_secrets_masks_url_encoded_form_with_quote_plus():
    """`quote_plus` 形式（空白 → `+`）も伏せられること。"""
    secret = "my secret key"
    encoded = "my+secret+key"
    text = f"form body contained token={encoded}"
    masked = GE._mask_secrets(text, secret)
    assert encoded not in masked


def test_mask_secrets_masks_lowercase_hex_percent_encoded_form():
    """`urllib.parse.quote()`/`quote_plus()` は常に大文字16進（`%2B`）を生成するが、上流プロキシ/
    ゲートウェイが RFC 3986 上は同値の小文字16進（`%2b`）で echo することがある——単純な文字列
    一致（大文字形のみ）だと小文字形はすり抜ける（実際に再現しうる漏洩パターン）。大小文字
    どちらの16進形でもマスクされることを固定する。"""
    secret = "abc+/secret="
    encoded_lower = "abc%2b%2fsecret%3d"   # quote() が生成する大文字形（%2B%2F...）の小文字版
    text = f"redirected to https://example/callback?token={encoded_lower}"
    masked = GE._mask_secrets(text, secret)
    assert encoded_lower not in masked
    assert "[REDACTED]" in masked


def test_mask_secrets_masks_mixed_case_hex_percent_encoded_form():
    """RV9 是正の固定: 上流プロキシ/ゲートウェイは同じ値の**中で**大文字/小文字16進を混在させて
    echo することがある（例: 同じトークンの中で `%2b` と `%2C` が混在）。固定の「全体を
    小文字化した変種」を1つ追加で比較するだけ（旧実装）では、桁ごとに大小文字が異なる
    組み合わせを網羅できず、こうした混在ケースはすり抜けうる——桁ごとに大小文字を問わない
    正規表現（`_percent_encoding_insensitive_pattern`）で照合することを固定する。"""
    secret = "abc+/secret="
    encoded_mixed = "abc%2b%2Fsecret%3d"   # %2b(小)・%2F(大)・%3d(小) が混在
    text = f"redirected to https://example/callback?token={encoded_mixed}"
    masked = GE._mask_secrets(text, secret)
    assert encoded_mixed not in masked
    assert "[REDACTED]" in masked


def test_mask_secrets_bearer_pattern_stops_at_comma_preserving_classification_info():
    """`Bearer <値>` の一般パターンは区切り（カンマ等）で止まり、値の後ろに続く分類情報
    （`code=invalid_api_key` 等の診断に有用な情報）を巻き込んで消さないこと。"""
    text = "authorization failed: Bearer abcSECRETxyz,code=invalid_api_key"
    masked = GE._mask_secrets(text, None)
    assert "abcSECRETxyz" not in masked
    assert "code=invalid_api_key" in masked
    assert "Bearer" in masked   # ラベル自体は残る


def test_mask_secrets_api_key_header_pattern_stops_at_semicolon():
    """値が引用符で囲まれていれば旧 regex でも `"` で止まるため、引用符の効果と区切りの効果を
    混同しないよう、未引用・空白なしの値（`;` の直前まで隙間が無い）で区切りの効果だけを固定する。"""
    text = "rejected header api-key: azure-secret-value-9876;retry-after=30"
    masked = GE._mask_secrets(text, None)
    assert "azure-secret-value-9876" not in masked
    assert "retry-after=30" in masked


def test_mask_secrets_sk_token_pattern_still_works():
    text = "duplicate key sk-anotherleakedtoken000111"
    masked = GE._mask_secrets(text, None)
    assert "sk-anotherleakedtoken000111" not in masked


def test_mask_secrets_handles_none_secret_and_empty_text_without_crashing():
    assert GE._mask_secrets("", "sk-x") == ""
    assert GE._mask_secrets("plain text, nothing sensitive", None) == "plain text, nothing sensitive"


def test_mask_secrets_masks_asterisk_masked_token_generic_form():
    """上流が「先頭数文字＋アスタリスク列＋末尾数文字」のような独自形式でキーを部分マスクして
    echo するケースは、secret が分からなくても（`None` でも）アスタリスク列（4個以上）を含む
    空白区切りトークンとして丸ごと伏せられること（実機観測の一般形）。"""
    text = "rejected: partial key abc12****************************wxyz observed"
    masked = GE._mask_secrets(text, None)
    assert "abc12" not in masked
    assert "wxyz" not in masked
    assert "****" not in masked
    assert "rejected: partial key" in masked
    assert "observed" in masked


def test_mask_secrets_does_not_mask_short_coincidental_fragment_under_min_len():
    """secret の接頭辞/接尾辞断片マスク（`_mask_secret_fragments`）は `_SECRET_FRAGMENT_MIN_LEN`
    （7文字）未満の偶然一致を対象にしない（誤爆防止）。"""
    secret = "ABCDEFG-REST-OF-A-LONGER-SECRET-VALUE-TUVWXYZ"
    text = "saw ABCDEF in an unrelated log line, nothing to do with UVWXYZ here"
    masked = GE._mask_secrets(text, secret)
    assert masked == text, f"7文字未満の偶然一致で本文が変わってしまっている: {masked!r}"


def test_mask_secrets_does_not_mask_sk_pro_substring_when_secret_is_sk_proj_style():
    """secret が `sk-proj-...` 形式でも、本文中の無関係な6字の `sk-pro` はマスクされないこと
    （`_SECRET_FRAGMENT_MIN_LEN` を 6→7 へ引き上げた回帰確認・`sk-proj` なら7字で一致し検出力は
    維持される）。"""
    secret = "sk-proj-REALSECRETVALUE1234567890abcdef"
    text = "unrelated log line mentions sk-pro as a generic term, nothing to do with the key"
    masked = GE._mask_secrets(text, secret)
    assert masked == text, f"6字の偶然一致（sk-pro）が誤ってマスクされている: {masked!r}"


def test_mask_secret_fragments_replaces_all_matching_lengths_not_just_first():
    """secret の接頭辞・接尾辞のうち、異なる断片が本文の別々の場所に現れても両方とも伏せられる
    こと（最初に見つかった長さで打ち切ると、もう一方の断片が残ってしまう不具合の回帰＝
    `_mask_secret_fragments` は各長さを打ち切らず全て確認する）。"""
    secret = "ABCDEFGH-MIDDLE-PART-OF-SECRET-TUVWXYZ"   # 接頭辞8字 ABCDEFGH・接尾辞7字 TUVWXYZ
    text = "log mentions prefix ABCDEFGH in one place, and suffix TUVWXYZ in another"
    masked = GE._mask_secrets(text, secret)
    assert "ABCDEFGH" not in masked
    assert "TUVWXYZ" not in masked
    assert "log mentions prefix" in masked
    assert "in another" in masked


# ===== 2. `_safe_detail`: マスクしてから切断する順序の保証 =====

def test_safe_detail_masks_before_truncating_generic_exception_so_no_fragment_leaks():
    """secret が `_DETAIL_MAX_LEN_GENERIC`（300字）の切断境界をまたぐ位置にあっても、その断片
    （部分文字列）が最終的な detail に残らないこと。sk-/Bearer/api-key のどの一般パターンにも
    該当しない素のキーで検証する（完全一致 redaction が切断より先に効いていることの証拠）。"""
    secret = "PLAINKEY-9988776655443322110099887766-STRADDLE-TAIL-VALUE"  # 60字・一般パターン非該当
    prefix = "x" * 260   # 型名接頭辞（例: "RuntimeError: "）を足しても secret の開始位置が 300 手前になる余裕
    message = prefix + secret + "y" * 30

    class _Boom(Exception):
        pass

    detail = GE._safe_detail(_Boom(message), secret=secret)
    assert len(detail) <= GE._DETAIL_MAX_LEN_GENERIC
    assert secret not in detail
    assert secret[:15] not in detail, f"secret の断片が残っている: {detail!r}"


def test_safe_detail_masks_before_truncating_http_error_so_no_fragment_leaks():
    """同じ保証を HTTPError 経路（`_DETAIL_MAX_LEN_HTTP`＝400字）でも確認する。"""
    secret = "PLAINKEY-HTTPPATH-1122334455667788990011223344556677889900"  # 60字・一般パターン非該当
    prefix = "x" * 360
    exc = _http_error(401, {"error": {"message": prefix + secret + "y" * 30}})

    detail = GE._safe_detail(exc, secret=secret)
    assert len(detail) <= GE._DETAIL_MAX_LEN_HTTP
    assert secret not in detail
    assert secret[:15] not in detail, f"secret の断片が残っている: {detail!r}"


def test_safe_detail_does_not_raise_on_non_string_secret():
    """RV9 是正の固定: `secret` が文字列でない場合（設定破損等）でも `_safe_detail` は例外を
    出さない——`_mask_secrets` 内の `text.replace(secret, ...)` は非文字列だと `TypeError` を
    出すため、`_log_masked_exception` と同じ `str()` 化を `_safe_detail` の入口でも行う。"""
    class _Boom(Exception):
        pass

    detail = GE._safe_detail(_Boom("some failure"), secret={"not": "a-string"})
    assert isinstance(detail, str)


def test_safe_detail_anthropic_delegation_masks_key_straddling_boundary():
    """`_safe_detail` が Anthropic/Bedrock 例外を `_error_detail` 経由で `agents._safe_bedrock_detail`
    へ委譲する分岐でも、同じ「マスクしてから切断する」保証が効くこと（400字切断境界をまたぐキーの
    断片が残らない）。"""
    import anthropic
    import httpx

    key = "PLAINBEDROCKKEY-VIASAFEDETAIL-1122334455667788990011-TAIL"
    prefix = "x" * 360
    resp = httpx.Response(403, request=httpx.Request("POST", "http://x"))
    exc = anthropic.APIStatusError(prefix + key + "y" * 30, response=resp, body=None)

    detail = GE._safe_detail(exc, secret=key)
    assert key not in detail
    assert key[:15] not in detail, f"キーの断片が残っている: {detail!r}"


# ===== 2c. DeploymentNotFound 案内文: 本文が長くても切断で消えないこと =====

def test_safe_detail_appends_deployment_not_found_hint_for_short_message(monkeypatch):
    """基本ケース（本文が短い）で案内文が付くこと（切断ロジック変更の回帰確認）。"""
    from sherpa import llm
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda *a, **kw: "azure", raising=False)
    exc = _http_error(404, {"error": {"code": "DeploymentNotFound", "message": "deployment not found"}})
    detail = GE._safe_detail(exc)
    assert detail.endswith(GE._DEPLOYMENT_NOT_FOUND_HINT)


def test_safe_detail_preserves_deployment_not_found_hint_when_message_is_long(monkeypatch):
    """本文が400字の上限を優に超えていても、案内文は必ず末尾に残ること（本文を先に切ってから
    案内文を付ける・切断で案内が丸ごと消える表示回帰を防ぐ）。"""
    from sherpa import llm
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda *a, **kw: "azure", raising=False)
    long_message = "この本文はダミーです。実際のエラーメッセージを模した長文です。" * 20   # 400字を優に超える
    exc = _http_error(404, {"error": {"code": "DeploymentNotFound", "message": long_message}})
    detail = GE._safe_detail(exc)
    assert detail.endswith(GE._DEPLOYMENT_NOT_FOUND_HINT)
    assert len(detail) <= GE._DETAIL_MAX_LEN_HTTP


# ===== 2d. 反射 URL のマスク（上流が要求 URL を echo した場合の防御） =====
# custom/Azure OpenAI 互換の上流エンドポイントがエラー本文へ要求 URL をそのまま echo すると、
# admin だけが設定した base URL（path にデプロイ名等の内部識別子・query に api-version を
# 含みうる）が一般ユーザー向け応答（`settings_test`）や health ログへ露出しうる。
#
# `_redact_reflected_urls` は single-pass のトークンマスク方式（`sherpa/ingest/graph_extract.py`
# の該当コメント参照）: URL の指標を含む空白区切りの単語を丸ごと伏せる方式であり、個々の
# 反射形（port・IPv6・Unicode path・混在encode 等）ごとに文字集合を拡張する必要がない。以下は
# 「入力に含めた URL の部分文字列は一切出力に残らない」という単一の性質を、事例を横断する
# パラメタライズテストで検証する（個別の正規表現の内部構造は検証しない＝実装の詳細に結合しない）。

_SECRET_BASE = "https://myres.openai.azure.com/openai/deployments/my-secret-deploy"
_SECRET_FULL = _SECRET_BASE + "/chat/completions?api-version=2024-01-01"


def _double_quote(s: str) -> str:
    return quote(quote(s, safe=""), safe="")


_LEAK_CASES = [
    pytest.param(
        f"upstream rejected request to {_SECRET_BASE}/chat/completions?api-version=2024-01-01",
        ["my-secret-deploy", "api-version", "chat"], id="plain-full-url"),
    pytest.param(
        "redirect target: " + quote(_SECRET_BASE, safe=""),
        ["my-secret-deploy"], id="encoded-base-url"),
    pytest.param(
        "upstream echoed request to "
        + quote(_SECRET_FULL, safe="") + " and rejected it",
        ["my-secret-deploy", "api-version", "2024-01-01", "chat"], id="encoded-full-url-not-just-base-prefix"),
    pytest.param(
        "upstream echoed request to "
        + quote(_SECRET_FULL, safe="").lower() + " and rejected it",
        ["my-secret-deploy", "api-version"], id="encoded-lowercase-hex"),
    pytest.param(
        "upstream echoed request to "
        + quote(_SECRET_FULL, safe="")
          .replace("%2F", "%2f").replace("%3D", "%3d")
        + " and rejected it",
        ["my-secret-deploy", "api-version"], id="encoded-mixed-case-hex"),
    pytest.param(
        "upstream echoed https%3A//myres.openai.azure.com/openai/deployments/"
        "my-secret-deploy?api-version=2024-01-01 and rejected",
        ["my-secret-deploy", "api-version"], id="mixed-plain-and-encoded-scheme-separator"),
    pytest.param(
        "unauthorized for deployment %2Fopenai%2Fdeployments%2Fmy-secret-deploy"
        "?api-version=2024-01-01 rejected",
        ["my-secret-deploy", "api-version"], id="schemeless-encoded-path-fragment"),
    pytest.param(
        f"upstream echoed {_double_quote(_SECRET_FULL)} and rejected",
        ["my-secret-deploy", "api-version"], id="double-encoded-url"),
    pytest.param(
        "upstream echoed https%3A%2F%2Fmyres.openai.azure.com%2Fopenai%2Fdeployments"
        "%2Fmy-secret-deploy?api-version=2024-01-01 and rejected",
        ["my-secret-deploy", "api-version"], id="literal-query-tail-after-encoded-path"),
    pytest.param(
        "upstream https%3A%2F%2Fmyres.openai.azure.com%2Fopenai%2Fdeployments"
        "%2Fmy-secret-deploy%3Fapi-version%3D2024-01-01 rejected",
        ["my-secret-deploy", "api-version"], id="fully-encoded-query-with-percent-3F"),
    pytest.param(
        "upstream https%3A//host.example:8443/openai/deployments/"
        "my-secret-deploy?api-version=2024 rejected",
        ["my-secret-deploy", "api-version", "8443"], id="encoded-scheme-with-port"),
    pytest.param(
        "upstream https%3A%2F%2F%5B2001%3Adb8%3A%3A1%5D%3A8443%2Fopenai%2Fdeployments"
        "%2Fmy-secret-deploy rejected",
        ["my-secret-deploy", "2001"], id="encoded-scheme-with-ipv6-host"),
    pytest.param(
        "upstream https%3A%2F%2Fhost.example%2Fopenai%3Fredirect=https://other.example/"
        "secret-path rejected",
        ["other.example", "secret-path", "redirect"], id="encoded-scheme-with-slash-in-query-value"),
    pytest.param(
        "see https://host.example/openai?redirect=https://other.example/secret-path next",
        ["other.example", "secret-path", "redirect"], id="plain-url-with-embedded-url-in-query"),
    pytest.param(
        "see 'https://host.example/secret'. next",
        ["secret"], id="closing-quote-preserved-but-secret-masked"),
    pytest.param(
        "upstream https://host.example%2Fopenai%2Fdeployments%2Fmy-secret-deploy rejected",
        ["my-secret-deploy"], id="plain-scheme-with-encoded-path-mixed"),
    pytest.param(
        "https://host.example/内部/deployments/秘密?api-version=2024 failed",
        ["秘密", "内部", "api-version"], id="unicode-path-and-query"),
    pytest.param(
        "connect to https://user@host.example/path;param,x?a=1 next",
        ["user", "path", "param", "a=1"], id="userinfo-and-reserved-punctuation-in-url"),
    pytest.param(
        "upstream %252Fmy-secret-deploy rejected",
        ["my-secret-deploy"], id="single-double-encoded-slash-fragment"),
    pytest.param(
        "upstream %2F内部%2F秘密 rejected",
        ["秘密", "内部"], id="unicode-segment-encoded-path-fragment"),
    pytest.param(
        "upstream %2Fopenai;param%2Fmy-secret-deploy rejected",
        ["my-secret-deploy", "param"], id="semicolon-interrupted-encoded-path-fragment"),
    pytest.param(
        "InvalidURL: /openai/deployments/my-secret-deploy contains control characters",
        ["my-secret-deploy", "deployments"], id="plain-multi-segment-request-target"),
    pytest.param(
        "C:%2FWindows%2FSystem32",
        ["Windows", "System32"], id="windows-path-with-encoded-slash-masked-tradeoff"),
    pytest.param(
        "%2Fteam%2Fmember@example.com",
        ["team", "member@example.com"], id="email-like-encoded-slash-masked-tradeoff"),
    pytest.param(
        "InvalidURL: '/openai/deployments/my-secret-deploy' contains control characters",
        ["my-secret-deploy", "deployments"], id="quoted-plain-multi-segment-request-target"),
    pytest.param(
        "params%3Fapi-version%3D2024-01-01 rejected",
        ["api-version", "2024-01-01"], id="percent-3F-3D-only-no-percent-2F"),
    pytest.param(
        "upstream host%3A8443%3Fapi-version%3D2024 rejected",
        ["8443", "api-version", "2024"], id="percent-3A-3F-only-no-percent-2F"),
    pytest.param(
        "unauthorized for openai%2Fdeployments%2Fmy-secret-deploy rejected",
        ["my-secret-deploy", "deployments"], id="encoded-fragment-directly-after-word-char"),
    pytest.param(
        "connect failed: postgresql://admin:db-secret@db.internal/app timeout",
        ["admin", "db-secret", "app"], id="postgresql-dsn"),
    pytest.param(
        "connect failed: redis://user:pass@cache.internal:6379/0 timeout",
        ["user", "pass", "6379"], id="redis-dsn"),
    pytest.param(
        "connect failed: bolt://neo4j:s3cr3t@graph.internal:7687 timeout",
        ["neo4j", "s3cr3t", "7687"], id="bolt-dsn"),
    pytest.param(
        "InvalidURL: '/openai/deployments/my-secret deploy' contains control characters",
        ["my-secret", "deploy", "deployments"], id="quoted-request-target-with-embedded-space"),
    pytest.param(
        "「(https://host.example/openai/deployments/my-secret-deploy)。」は無効です",
        ["my-secret-deploy", "deployments"], id="fullwidth-quote-and-paren-wrapped-url"),
    pytest.param(
        "rejected 【/openai/deployments/my-secret-deploy】 request",
        ["my-secret-deploy", "deployments"], id="fullwidth-bracket-wrapped-request-target"),
    pytest.param(
        "unauthorized for /openai//deployments/my-secret-deploy rejected",
        ["my-secret-deploy", "deployments"], id="multi-segment-path-with-empty-segment"),
    pytest.param(
        "upstream //host.internal/openai/my-secret-path rejected",
        ["host.internal", "my-secret-path"], id="multi-segment-path-leading-double-slash"),
    pytest.param(
        "contact mailto:secret.deploy@internal.example for access",
        ["secret.deploy", "internal.example"], id="mailto-scheme-without-slashes"),
    pytest.param(
        "payload data:text/plain;base64,c2VjcmV0LWRlcGxveQ== embedded",
        ["c2VjcmV0LWRlcGxveQ", "text/plain"], id="data-scheme-with-payload"),
    pytest.param(
        "reading file:/etc/my-secret-deploy failed",
        ["my-secret-deploy"], id="file-scheme-single-slash"),
    pytest.param(
        "don't call '/openai/deployments/my-secret TOPSECRET' now",
        ["my-secret", "TOPSECRET", "deployments"], id="quote-prepass-apostrophe-before-real-quote"),
    pytest.param(
        "『outer 「/openai/deployments/my-secret TOPSECRET」 tail』",
        ["my-secret", "TOPSECRET", "deployments"], id="quote-prepass-nested-fullwidth-quotes"),
    pytest.param(
        "outer '/openai/deployments/my-secret TOPSECRET' tail",
        ["my-secret", "TOPSECRET", "deployments"], id="quote-prepass-single-quote-baseline"),
    pytest.param(
        '"outer \'/openai/deployments/my-secret TOPSECRET\' tail"',
        ["my-secret", "TOPSECRET", "deployments"], id="quote-prepass-nested-double-outer-single-inner"),
    pytest.param(
        "『outer \"/openai/deployments/my-secret TOPSECRET\" tail』",
        ["my-secret", "TOPSECRET", "deployments"], id="quote-prepass-nested-fullwidth-outer-double-inner"),
    pytest.param(
        "//user:PASS@host.internal?token=abc123 rejected",
        ["PASS", "token", "abc123"], id="protocol-relative-userinfo-and-query-no-path"),
    pytest.param(
        "//host.internal#TOPSECRET rejected",
        ["TOPSECRET"], id="protocol-relative-fragment-no-path"),
    pytest.param(
        "reading data:,,secretpayload now",
        ["secretpayload"], id="explicit-scheme-punctuation-then-alnum-body"),
    pytest.param(
        "reading data:,, now",
        ["data:,,"], id="explicit-scheme-punctuation-only-body-still-masked"),
    pytest.param(
        "don't call '/openai/deployments/my-secret TOPSECRET' now",
        ["my-secret", "TOPSECRET", "deployments"], id="quote-prepass-word-internal-apostrophe-before-real-quote"),
    pytest.param(
        "outer '/openai's/deployments/my-secret TOPSECRET' tail",
        ["my-secret", "TOPSECRET", "deployments"], id="quote-prepass-apostrophe-inside-quoted-content"),
    pytest.param(
        "『『/openai/deployments/my-secret TOPSECRET』』",
        ["my-secret", "TOPSECRET", "deployments"], id="quote-prepass-same-species-nested-fullwidth-quotes"),
    pytest.param(
        "エラー「/openai/deployments/my-secret TOPSECRET」でした",
        ["my-secret", "TOPSECRET", "deployments"], id="fullwidth-quote-opener-preceded-by-word-char"),
    pytest.param(
        "error「/openai/deployments/my-secret TOPSECRET」occurred",
        ["my-secret", "TOPSECRET", "deployments"], id="fullwidth-quote-opener-preceded-by-ascii-word-char"),
    pytest.param(
        "'field0' '/openai/deployments/my-secret TOPSECRET' tail",
        ["my-secret", "TOPSECRET", "deployments"], id="quote-prepass-independent-same-species-spans-merged-ascii"),
    pytest.param(
        "「field0」 「/openai/deployments/my-secret TOPSECRET」 tail",
        ["my-secret", "TOPSECRET", "deployments"], id="quote-prepass-independent-same-species-spans-merged-fullwidth"),
    pytest.param(
        "'field0''/openai/deployments/my-secret TOPSECRET' tail",
        ["my-secret", "TOPSECRET", "deployments"],
        id="quote-prepass-independent-same-species-spans-merged-ascii-no-whitespace"),
    pytest.param(
        "「field0」「/openai/deployments/my-secret TOPSECRET」tail",
        ["my-secret", "TOPSECRET", "deployments"],
        id="quote-prepass-independent-same-species-spans-merged-fullwidth-no-whitespace"),
    pytest.param(
        "outer '%2''Fopenai/deployments/my-secret TOPSECRET' tail",
        ["my-secret", "TOPSECRET", "deployments"],
        id="quote-boundary-splits-percent-escape-sequence-ascii"),
    pytest.param(
        "outer 「%2」「Fopenai/deployments/my-secret TOPSECRET」 tail",
        ["my-secret", "TOPSECRET", "deployments"],
        id="quote-boundary-splits-percent-escape-sequence-fullwidth"),
]


@pytest.mark.parametrize("text, forbidden", _LEAK_CASES)
def test_redact_reflected_urls_leaves_no_url_substring_in_output(text, forbidden):
    """反射 URL の各事例（port・IPv6・Unicode path/query・`'`/`@`/`;`/`,` を含む URL・混在
    encode・二重 encode・平文 scheme と encoded path の混在・query 内へ埋め込まれた URL・
    `%3F` 断片等）をまとめて固定する: 入力に含めた URL の部分文字列（デプロイ名・path・
    query・ホスト名・userinfo 等）はどれも出力に一切残らない。"""
    masked = GE._redact_reflected_urls(text, _SECRET_BASE)
    for substr in forbidden:
        assert substr not in masked, f"{substr!r} が漏洩している: {masked!r}"


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


@pytest.mark.parametrize("text", _NO_OP_CASES)
def test_redact_reflected_urls_does_not_over_mask(text):
    """過剰マスクの固定: 空白で区切られた平文（`%2F` を含む日付・割合表記＝全角数字版・3区切り版
    含む）・URL/percent-encoded 断片を含まない通常の日本語文/英語の診断文・1段の Unix パス
    （末尾スラッシュ付きも含む）・通常のメールアドレス・Windows パス（backslash 形/forward-slash 形）
    は変更されない。日付/割合の除外判定は**トークン全体**が数字と `%2F` だけで構成される場合に
    限る＝地の文と空白なしで結合したトークン（`日付は2026%2F08%2F26です` 等）は除外対象に
    ならず伏せられる（`test_redact_reflected_urls_masks_date_glued_to_prose_without_whitespace`
    参照・許容するトレードオフ）。2段以上の平文絶対パス（`/etc/passwd` 等）は request-target
    反射との区別が付かないため `_LEAK_CASES` の `plain-multi-segment-request-target` が示すとおり
    伏せる契約へ変わった（診断価値より漏洩防止を優先する既存契約に含める）。"""
    assert GE._redact_reflected_urls(text, None) == text


def test_redact_reflected_urls_masks_date_glued_to_prose_without_whitespace():
    """日付/割合の除外判定はトークン**全体**が数字と `%2F` だけの形に一致する場合に限るため、
    空白を挟まず日本語文に結合したトークン（`日付は2026%2F08%2F26です` のように前後に文字が
    付いている場合）は除外対象にならず、単語全体が `[URL]` へ伏せられる（許容するトレードオフ・
    `test_redact_reflected_urls_does_not_over_mask` の空白区切り版と対で固定する）。"""
    assert GE._redact_reflected_urls("日付は2026%2F08%2F26です", None) == "[URL]"


def test_redact_reflected_urls_masks_dotted_double_slash_mentions_documented_tradeoff():
    """裁定: `//README.md`・`//version.2` のような、URL ではない「`//` 始まり＋ドット」の言及も
    protocol-relative の指標（`_is_protocol_relative_url`）に一致して `[URL]` へ伏せられる契約
    （漏洩防止を優先する既存の fail-closed 契約を維持・診断価値の低下は許容するトレードオフ）。"""
    assert GE._redact_reflected_urls("see //README.md for details", None) == "see [URL] for details"
    assert GE._redact_reflected_urls("bumped to //version.2 today", None) == "bumped to [URL] today"


def test_redact_reflected_urls_preserves_trailing_punctuation_with_space():
    """URL の後に空白を挟んで句読点付きの地の文が続く場合、句読点は URL 表記側に取り込まず
    地の文として残す（host[:port] へ丸めても文が読めるようにする）。"""
    text = "詳細は https://myres.openai.azure.com/openai/deployments/my-secret-deploy をご確認ください。"
    masked = GE._redact_reflected_urls(text, "https://myres.openai.azure.com/v1")
    assert masked == "詳細は myres.openai.azure.com をご確認ください。"

    text2 = "See https://myres.openai.azure.com/v1/deployments/my-secret-deploy."
    masked2 = GE._redact_reflected_urls(text2, "https://myres.openai.azure.com/v1")
    assert masked2 == "See myres.openai.azure.com."


def test_redact_reflected_urls_does_not_reattach_url_data_disguised_as_punctuation():
    """query/fragment の区切り直後に続くデータが句読点に見えても、無条件に地の文として
    再付加しない（`;`/`!` は保持対象の句読点集合に含めないため、これらはそもそも本体から
    切り離されず、host への丸め込みで一緒に破棄される＝`?token=;;;` や `#!!!` が
    `host;;;`/`host!!!` のように出力へ残っていた穴の再発防止）。"""
    assert GE._redact_reflected_urls("https://host.example/path?token=;;; end", None) == "host.example end"
    assert GE._redact_reflected_urls("https://host.example/path#!!! end", None) == "host.example end"


def test_redact_reflected_urls_preserves_closing_paren_and_fullwidth_period():
    """通常の閉じ括弧・全角句点は句読点として正しく保持される（`; ! ? #` を除外しても、
    本来保持すべき句読点まで巻き込まれないことの対照）。"""
    assert GE._redact_reflected_urls("https://host/path)。", None) == "host)。"


def test_redact_reflected_urls_does_not_preserve_overlong_trailing_cluster():
    """末尾クラスタの長さが上限（3文字）を超える場合は句読点とみなさず本体側に残す
    （host への丸め込みで一緒に破棄される）。"""
    assert GE._redact_reflected_urls("https://host/path.... next", None) == "host next"


def test_redact_reflected_urls_may_swallow_trailing_prose_without_whitespace_documented_tradeoff():
    """契約として明示的に受け入れるトレードオフ: URL の直後に空白を挟まず地の文（全角句読点＋
    日本語文）が続く場合、単語境界（空白）で範囲を決める方式では「URL を含む単語」がその文
    全体になり、末尾の句読点の連なりを除いた部分が丸ごと伏せられる（漏洩防止を優先し、この
    可読性低下は許容する・`_redact_reflected_urls` のモジュールコメント参照）。空白を挟む
    現実的なケース（`test_redact_reflected_urls_masks_unicode_path_and_query_with_space_preserves_sentence`
    参照）では後続の文章は正しく保持される。"""
    text = "https://example.com/path）をご覧ください。詳細は別紙を参照。"
    masked = GE._redact_reflected_urls(text, "https://myres.openai.azure.com/v1")
    assert masked == "example.com。"


def test_redact_reflected_urls_masks_unicode_path_and_query_with_space_preserves_sentence():
    """URL の後に空白を挟めば、Unicode（日本語含む）の path/query を持つ URL 全体を伏せつつ、
    続く全角の句読点・後続の文章は取り込まれない（現実的なケースでの読みやすさの根拠）。"""
    text = "https://host.example/内部/deployments/秘密?api-version=2024 failed）続報は別途。"
    masked = GE._redact_reflected_urls(text, "https://myres.openai.azure.com/v1")
    assert masked == "host.example failed）続報は別途。"


def test_redact_reflected_urls_preserves_ipv6_closing_bracket_with_path():
    text = "connect to https://[2001:db8::1]/v1 failed"
    masked = GE._redact_reflected_urls(text, "https://myres.openai.azure.com/v1")
    assert masked == "connect to [2001:db8::1] failed"


def test_redact_reflected_urls_preserves_ipv6_closing_bracket_without_path():
    """IPv6 リテラル `[2001:db8::1]` が語の末尾（`]` の直後に path が続かない）場合でも `]` は
    保持される。`_TRAILING_PUNCT` は `]`/`}` を含まない（IPv6 リテラルとの衝突を避けるため）ので、
    末尾の `]` はそもそも句読点として剥がされない（`_pair_outer_brackets` は対応する開き括弧が
    ある場合のみ働く別の仕組みで、この単独 IPv6 のケースには関与しない）。"""
    text = "see https://[2001:db8::1] for details."
    masked = GE._redact_reflected_urls(text, "https://myres.openai.azure.com/v1")
    assert masked == "see [2001:db8::1] for details."


def test_redact_reflected_urls_preserves_outer_brackets_as_a_pair():
    """`[https://host/path]` のように URL 全体を角括弧で囲んだ場合、開き括弧だけが地の文へ
    分離され対応する閉じ括弧が本体側に取り込まれたまま丸められて消える（`[host` のように非対称に
    欠落する）ことなく、両方とも地の文として保持される（`_pair_outer_brackets` 参照）。"""
    text = "see [https://host/path] for details"
    masked = GE._redact_reflected_urls(text, None)
    assert masked == "see [host] for details"


def test_redact_reflected_urls_preserves_fullwidth_quote_and_paren_leading_cluster():
    """全角引用符＋半角括弧の入れ子（`「(https://host)。」`）: `「」` は `_QUOTE_PAIRS`（引用符
    pre-pass）の対象でもあるため、`_mask_quoted_url_spans` が先に区間全体（`(https://host)。`）を
    検出し、中身を丸ごと `[URL]` に伏せる（外側の `「」` 自体は地の文として保持される契約）。"""
    text = "「(https://host)。」"
    masked = GE._redact_reflected_urls(text, None)
    assert masked == "「[URL]」"


def test_redact_reflected_urls_preserves_fullwidth_brackets_as_a_pair():
    """`【https://host/path】` のように URL 全体を全角角括弧で囲んだ場合も、ASCII の `[...]`
    （`test_redact_reflected_urls_preserves_outer_brackets_as_a_pair` 参照）と同様に開き/閉じの
    両方が地の文として保持される。"""
    text = "see 【https://host/path】 for details"
    masked = GE._redact_reflected_urls(text, None)
    assert masked == "see 【host】 for details"


def test_mask_quoted_url_spans_leaves_unclosed_quote_untouched():
    """対応する閉じ引用符が見つからない開き引用符（通常の英文中のアポストロフィ等）は pre-pass の
    対象にせず、そのまま地の文に残す（誤って後続の全文を1つの引用符区間とみなさない）。後続の
    独立した URL は通常どおり単語単位でマスクされる。"""
    text = "don't forget to check https://host.example/path afterwards"
    masked = GE._redact_reflected_urls(text, None)
    assert masked == "don't forget to check host.example afterwards"


def test_redact_reflected_urls_catches_unrelated_url_not_matching_base_url():
    """呼び出し元が渡した `base_url` と厳密一致しない URL（上流が別形式で反射した・別ホストを
    案内した等）も、一般防御として `host[:port]` へ丸められる。"""
    text = "see https://other-internal-gw.example.com:8443/secret/path?token=abc for details"
    masked = GE._redact_reflected_urls(text, "https://myres.openai.azure.com/v1")
    assert masked == "see other-internal-gw.example.com:8443 for details"


def test_safe_detail_masks_reflected_base_url_in_http_error(monkeypatch):
    """`_safe_detail` は送信時スナップショット（`system_settings`）の実効 base URL を計算し、
    エラー本文中の反射を切断前にマスクする。"""
    sys_s = {"openai_endpoint_kind": "azure",
            "openai_base_url": "https://myres.openai.azure.com/openai/deployments/my-secret-deploy"}
    exc = _http_error(400, {"error": {"message": ("bad request: POST https://myres.openai.azure.com/"
                                                  "openai/deployments/my-secret-deploy/chat/completions"
                                                  "?api-version=2024-01-01 rejected")}})
    detail = GE._safe_detail(exc, system_settings=sys_s)
    assert "my-secret-deploy" not in detail
    assert "api-version" not in detail


def test_safe_detail_masks_reflected_url_in_generic_exception(monkeypatch):
    sys_s = {"openai_endpoint_kind": "custom", "openai_base_url": "https://gw.example.com/v1"}

    class _Boom(Exception):
        pass

    err = _Boom("connection to https://gw.example.com/v1/internal/route?key=leak failed")
    detail = GE._safe_detail(err, system_settings=sys_s)
    assert "/internal/route" not in detail
    assert "key=leak" not in detail


def test_safe_detail_masks_full_percent_encoded_request_url_in_http_error():
    """HTTPError 経路で完全な要求 URL（デプロイ名＋api-version 込み）が percent-encoded で
    反射されても、`_safe_detail` の戻り値からは一切残らない。"""
    from urllib.parse import quote
    base = "https://myres.openai.azure.com/openai/deployments/my-secret-deploy"
    full = base + "/chat/completions?api-version=2024-01-01"
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": base}
    exc = _http_error(400, {"error": {"message": f"upstream echoed {quote(full, safe='')} and rejected it"}})
    detail = GE._safe_detail(exc, system_settings=sys_s)
    assert "my-secret-deploy" not in detail
    assert "api-version" not in detail
    assert "2024-01-01" not in detail


def test_safe_detail_masks_full_percent_encoded_request_url_in_generic_exception():
    """同じ保証を一般例外経路（`_error_detail`）でも確認する。"""
    from urllib.parse import quote
    base = "https://gw.example.com/v1/internal/route"
    full = base + "?key=leak&api-version=2024-01-01"
    sys_s = {"openai_endpoint_kind": "custom", "openai_base_url": base}

    class _Boom(Exception):
        pass

    err = _Boom(f"connection to {quote(full, safe='')} failed")
    detail = GE._safe_detail(err, system_settings=sys_s)
    assert "/internal/route" not in detail
    assert "key=leak" not in detail
    assert "api-version" not in detail


def test_safe_detail_masks_encoded_url_straddling_truncation_boundary():
    """percent-encoded の反射 URL が `_DETAIL_MAX_LEN_GENERIC`（300字）の切断境界をまたぐ位置に
    あっても、マスクが切断より先に効くため断片が残らない（`_mask_secrets` 側の既存の
    「マスクしてから切断する」保証と同じ順序をこの経路でも確認する）。"""
    from urllib.parse import quote
    base = "https://myres.openai.azure.com/openai/deployments/my-secret-deploy"
    full = base + "/chat/completions?api-version=2024-01-01"
    encoded = quote(full, safe="")
    prefix = "x" * 260
    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": base}

    class _Boom(Exception):
        pass

    err = _Boom(prefix + encoded + "y" * 30)
    detail = GE._safe_detail(err, system_settings=sys_s)
    assert len(detail) <= GE._DETAIL_MAX_LEN_GENERIC
    assert "my-secret-deploy" not in detail
    assert "api-version" not in detail


def test_safe_detail_passes_full_untruncated_text_to_redact_reflected_urls(monkeypatch):
    """`_safe_detail` の「マスクしてから切断する」契約を、文字列の境界だけで検出しようとすると
    false green になりうる: `_redact_reflected_urls` の正規表現は末尾切断に対して頑健
    （トークンが途中で切れても、そこまでを正しく畳み込める）ため、境界をまたぐ構成を選んでも
    伏せ字前に切断する mutation で偶然 green のまま通ってしまう（`_mask_secrets` の secret
    完全一致とは異なり、部分一致でもマスクが効いてしまうため）。ここでは `_redact_reflected_urls`
    自体を spy 化し、受け取るテキストが**切断前の生の全長**（`_DETAIL_MAX_LEN_GENERIC` を
    超える）であることを直接固定する——切断が先に走っていれば、ここで受け取る長さは
    `_DETAIL_MAX_LEN_GENERIC` 以下に縮んでいるはず。"""
    from urllib.parse import quote
    base = "https://myres.openai.azure.com/openai/deployments/my-secret-deploy"
    full = base + "/chat/completions?api-version=2024-01-01"
    encoded = quote(full, safe="")
    raw_message = "x" * 260 + encoded + "y" * 30
    assert len(raw_message) > GE._DETAIL_MAX_LEN_GENERIC, "テスト前提が崩れている（生の長さが上限を超えていない）"

    seen_lengths: list[int] = []
    real_redact = GE._redact_reflected_urls

    def _spy_redact(text, base_url):
        seen_lengths.append(len(text))
        return real_redact(text, base_url)

    monkeypatch.setattr(GE, "_redact_reflected_urls", _spy_redact)

    class _Boom(Exception):
        pass

    sys_s = {"openai_endpoint_kind": "azure", "openai_base_url": base}
    detail = GE._safe_detail(_Boom(raw_message), system_settings=sys_s)

    assert seen_lengths, "_redact_reflected_urls が呼ばれなかった"
    assert seen_lengths[0] > GE._DETAIL_MAX_LEN_GENERIC, (
        f"_redact_reflected_urls が既に切断済み（{seen_lengths[0]}字）のテキストを受け取った"
        "＝切断がマスクより先に走っている（順序契約違反）")
    assert len(detail) <= GE._DETAIL_MAX_LEN_GENERIC
    assert "my-secret-deploy" not in detail
    assert "api-version" not in detail


# ===== 3. `_probe`（`sherpa/routers/system.py::settings_test`・`health.py` 等が使う）=====

def test_probe_masks_configured_key_leaked_in_http_error_message(monkeypatch):
    key = "sk-REALSECRET1234567890abcdef"
    exc = _http_error(401, {"error": {"message": f"invalid header, saw Authorization: Bearer {key}"}})
    monkeypatch.setattr(GE, "complete_json", lambda *a, **k: (_ for _ in ()).throw(exc))
    ok, detail = GE._probe({"provider": "openai", "key": key, "model": "gpt-5.5"})
    assert ok is False
    assert key not in detail
    assert "401" in detail


def test_probe_masks_bearer_pattern_in_http_error_message_even_if_not_the_configured_key(monkeypatch):
    """上流プロキシが Sherpa の設定キーとは別のトークン（自身の内部ヘッダ等）を echo した場合でも、
    `Bearer <値>` の一般パターンとして伏せられること（実キーの完全一致だけに頼らない）。"""
    leaked = "some-upstream-proxy-internal-token-xyz"
    exc = _http_error(403, {"error": {"message": f"blocked request with Authorization: Bearer {leaked}"}})
    monkeypatch.setattr(GE, "complete_json", lambda *a, **k: (_ for _ in ()).throw(exc))
    ok, detail = GE._probe({"provider": "openai", "key": "sk-configured-key-not-leaked", "model": "gpt-5.5"})
    assert ok is False
    assert leaked not in detail
    assert "Bearer" in detail   # ラベル自体は残る（伏せるのは値だけ）


def test_probe_masks_api_key_header_pattern_in_http_error_message(monkeypatch):
    exc = _http_error(401, {"error": {"message": 'rejected header api-key: "azure-secret-value-9876"'}})
    monkeypatch.setattr(GE, "complete_json", lambda *a, **k: (_ for _ in ()).throw(exc))
    ok, detail = GE._probe({"provider": "openai", "key": "sk-unrelated", "model": "gpt-5.5"})
    assert ok is False
    assert "azure-secret-value-9876" not in detail


def test_probe_masks_sk_prefixed_token_in_http_error_message(monkeypatch):
    """`error.message` に、設定キーとは別の sk- 形式トークンが混じっていても伏せられること。"""
    exc = _http_error(400, {"error": {"message": "duplicate key sk-anotherleakedtoken000111"}})
    monkeypatch.setattr(GE, "complete_json", lambda *a, **k: (_ for _ in ()).throw(exc))
    ok, detail = GE._probe({"provider": "openai", "key": "sk-configured", "model": "gpt-5.5"})
    assert ok is False
    assert "sk-anotherleakedtoken000111" not in detail


def test_probe_masks_configured_key_leaked_in_generic_exception_str(monkeypatch):
    key = "sk-REALSECRET1234567890abcdef"

    def _boom(*a, **k):
        raise RuntimeError(f"connection reset while sending Authorization: Bearer {key}")

    monkeypatch.setattr(GE, "complete_json", _boom)
    ok, detail = GE._probe({"provider": "openai", "key": key, "model": "gpt-5.5"})
    assert ok is False
    assert key not in detail


def test_probe_masks_generic_exception_bearer_pattern_regardless_of_configured_key(monkeypatch):
    leaked = "some-other-token-value-222333"

    def _boom(*a, **k):
        raise RuntimeError(f"gateway error, upstream sent Authorization: Bearer {leaked}")

    monkeypatch.setattr(GE, "complete_json", _boom)
    ok, detail = GE._probe({"provider": "openai", "key": "sk-configured-key", "model": "gpt-5.5"})
    assert ok is False
    assert leaked not in detail


def test_probe_masks_openai_partial_masked_key_echo_observed_in_production(monkeypatch):
    """実機で観測された形（OpenAI 本家に Azure キーで接続テストした際の 401）: OpenAI が上流エラーで
    「先頭8文字＋アスタリスク列（約60個）＋末尾4文字」にキーを部分マスクして echo する。完全一致・
    URL エンコード形・Bearer/api-key/sk- のどのパターンにも該当しないため、専用のマスクが無いと
    素通りしていた（rule 1: アスタリスク列トークン／rule 2: secret の接頭辞/接尾辞断片、の両方で守る）。
    """
    key = "AbCd1234" + "X" * 44 + "Zz99"           # ダミーキー（先頭8字/末尾4字は実機観測と一致させる）
    masked_echo = "AbCd1234" + "*" * 60 + "Zz99"    # 上流が実際に返した形（実機同様の部分マスク）
    exc = _http_error(401, {"error": {
        "code": "invalid_api_key",
        "message": f"Incorrect API key provided: {masked_echo}. You can find your API key at "
                   "https://platform.openai.com/account/api-keys."}})
    monkeypatch.setattr(GE, "complete_json", lambda *a, **k: (_ for _ in ()).throw(exc))
    ok, detail = GE._probe({"provider": "openai", "key": key, "model": "gpt-5.5"})
    assert ok is False
    assert "AbCd1234" not in detail
    assert "Zz99" not in detail
    assert "****" not in detail
    assert key not in detail


def test_probe_masks_spaced_partial_key_echo_leaves_no_fragment(monkeypatch):
    """`AbCd1234 **** **** Zz99`（空白入りの部分マスク echo・アスタリスク列が空白で複数トークンに
    分かれる形）でも断片が一切残らないこと。アスタリスク列トークン単体では拾えない接尾辞
    （`_SECRET_FRAGMENT_MIN_LEN` 未満）を、`[REDACTED]` が2個以上連続する隣接ルールで補う。
    分類情報・案内文（「次の文の先頭語」を隣接ルールが誤って飲み込んでいないこと）も併せて確認する。
    """
    key = "AbCd1234" + "X" * 44 + "Zz99"
    masked_echo = "AbCd1234 **** **** Zz99"   # 空白区切りで prefix・アスタリスク列2つ・suffix に分かれる形
    exc = _http_error(401, {"error": {
        "code": "invalid_api_key",
        "message": f"Incorrect API key provided: {masked_echo}. You can find your API key at "
                   "https://platform.openai.com/account/api-keys."}})
    monkeypatch.setattr(GE, "complete_json", lambda *a, **k: (_ for _ in ()).throw(exc))
    ok, detail = GE._probe({"provider": "openai", "key": key, "model": "gpt-5.5"})
    assert ok is False
    assert "AbCd1234" not in detail
    assert "Zz99" not in detail
    assert "****" not in detail
    assert key not in detail
    assert "401" in detail
    assert "invalid_api_key" in detail
    assert "You can find your API key at" in detail


def test_probe_preserves_classification_info_when_masking_partial_key_echo(monkeypatch):
    """アスタリスク列マスク（rule 1・secret 未一致でも効く一般パターン）を適用しても、ステータス
    コード・エラーコード・案内文などの分類情報は残ること（過剰マスクで診断価値を落とさない）。
    設定キーがメッセージ中の値と一致しない場合（rule 2 が発火しない場合）でも rule 1 単独で
    守れることも合わせて確認する。"""
    exc = _http_error(401, {"error": {
        "code": "invalid_api_key",
        "message": "Incorrect API key provided: ab12****************cd34. "
                   "You can find your API key at https://platform.openai.com/account/api-keys."}})
    monkeypatch.setattr(GE, "complete_json", lambda *a, **k: (_ for _ in ()).throw(exc))
    ok, detail = GE._probe({"provider": "openai", "key": "totally-different-key-not-in-message",
                            "model": "gpt-5.5"})
    assert ok is False
    assert "401" in detail
    assert "invalid_api_key" in detail
    assert "You can find your API key at" in detail
    assert "ab12" not in detail and "cd34" not in detail


def test_probe_ok_path_never_calls_mask_secrets(monkeypatch):
    """成功パスは detail 整形（`_mask_secrets`）を一切経由しないこと（spy で実証する）。"""
    calls = []
    monkeypatch.setattr(GE, "_mask_secrets", lambda text, secret: (calls.append((text, secret)), text)[1])
    monkeypatch.setattr(GE, "complete_json", lambda *a, **k: '{"ok":true}')
    ok, detail = GE._probe({"provider": "openai", "key": "sk-x", "model": "gpt-5.5"})
    assert ok is True
    assert detail == ""
    assert calls == []


def test_probe_without_key_in_cfg_does_not_crash_on_redaction(monkeypatch):
    """cfg に key/api_key が無い構成（ollama 等）でも `_safe_detail`/`_mask_secrets` が落ちないこと。"""
    exc = _http_error(500, {"error": {"message": "internal error"}})
    monkeypatch.setattr(GE, "complete_json", lambda *a, **k: (_ for _ in ()).throw(exc))
    ok, detail = GE._probe({"provider": "ollama", "url": "http://localhost:11434", "model": "qwen2.5"})
    assert ok is False
    assert "500" in detail


def test_probe_masks_reflected_base_url_via_openai_endpoint_override(monkeypatch):
    """`cfg["openai_endpoint_override"]`（送信時スナップショット）に基づき、上流が echo した
    要求 URL（デプロイ名を含む path・api-version の query）を `_probe` の戻り値からも伏せる。"""
    sys_s = {"openai_endpoint_kind": "azure",
            "openai_base_url": "https://myres.openai.azure.com/openai/deployments/my-secret-deploy"}
    exc = _http_error(400, {"error": {"message": ("bad request to https://myres.openai.azure.com/"
                                                  "openai/deployments/my-secret-deploy/chat/completions"
                                                  "?api-version=2024-01-01")}})
    monkeypatch.setattr(GE, "complete_json", lambda *a, **k: (_ for _ in ()).throw(exc))
    ok, detail = GE._probe({"provider": "openai", "key": "sk-x", "model": "gpt-5.5",
                            "openai_endpoint_override": sys_s})
    assert ok is False
    assert "my-secret-deploy" not in detail
    assert "api-version" not in detail


# ===== 4. 性能・堅牢性: 閉じ引用符の無い大量反復入力・上流本文の読み込み上限 =====

def test_mask_quoted_url_spans_handles_large_adversarial_input_without_hanging():
    """引用符の対付けを貪欲（最後の同種閉じ文字まで）にしたことで、閉じ引用符の無い同種文字が
    大量に連続する入力（32万字クラス）は非常に深い入れ子を作りうる。素朴な実装だと
    (a) Python の関数呼び出し再帰では `RecursionError` になり、(b) 各階層で子の結果文字列を
    まるごとコピーするコストが階層数に比例して積み上がり O(n^2) に退化しうる。どちらも
    起きないこと（実用時間内に正しい結果が返ること）を固定する。厳密な時間断言はしない
    （実装は明示スタック＋入れ子深さの上限 `_MAX_QUOTE_NESTING_DEPTH` で両方を防ぐ）。"""
    import time

    text = "'x " * 100000   # 30万字・閉じない単語内でない開き引用符の反復（指標なし）
    start = time.monotonic()
    result = GE._redact_reflected_urls(text, None)
    elapsed = time.monotonic() - start
    assert result == text, "指標の無い入力なので無加工のはず"
    assert elapsed < 10.0, f"想定より大幅に遅い（O(n^2) 退化やハングの疑い）: {elapsed:.2f}s"


def test_mask_quoted_url_spans_still_masks_indicator_within_deeply_nested_input():
    """入れ子深さの上限（`_MAX_QUOTE_NESTING_DEPTH`）を超えるほど深い同種引用符の反復の**手前**に
    現れる secret は、上限内の入れ子として通常どおり検出・マスクされることを固定する
    （性能対策の上限が通常のユースケースの検出力を落とさないことの対照）。"""
    secret = "TOPSECRET-DEEP-NESTING-VALUE"
    text = f"outer '/openai/deployments/{secret}' " + ("'x " * 1000)
    masked = GE._redact_reflected_urls(text, None)
    assert secret not in masked, f"{secret!r} が漏洩している: {masked!r}"


def _nest_alternating_quotes(inner: str, depth: int) -> str:
    """`inner` を ASCII の `'...'` と全角の `「...」` を交互に使って `depth` 段だけ入れ子にする
    （同種の引用符を連続させると貪欲マッチで1階層に併合されてしまうため、階層ごとに種類を
    変えて確実に独立した入れ子を作る）。"""
    text = inner
    for i in range(depth):
        text = f"'{text}'" if i % 2 == 0 else f"「{text}」"
    return text


def test_mask_quoted_url_spans_masks_secret_beyond_nesting_depth_limit_fail_closed():
    """入れ子深さの上限（`_MAX_QUOTE_NESTING_DEPTH`）を超える段数の引用符に囲まれた secret は、
    対付けを試みずに「未処理のまま素通り」させるのではなく、残り部分文字列に URL 指標があれば
    丸ごと `[URL]` に伏せる fail-closed へ倒す契約を固定する（上限の数え方は root フレームを
    除いて数える＝`_MAX_QUOTE_NESTING_DEPTH` ちょうどの入れ子までは通常どおり検出でき、それを
    1段超えた場合に本テストが対象とする経路へ入る）。

    secret の不在だけでなく出力の**厳密な形**も固定する: 上限ちょうどの段（`cap`）は通常の
    再帰的対付けで処理されるため、出力は `[URL]` を同じ入れ子でそのまま包んだ形
    （`_nest_alternating_quotes("[URL]", cap)`）と一致する。上限を1段でも超えると、外側
    ちょうど `cap` 段だけが個別の引用符対で残り、それより内側はまとめて1個の不透明区間として
    `[URL]` に伏せられる（対付けを保ったまま個々に包む「上限ちょうど」の形とは異なる形になる）。
    種類を交互（ASCII/全角）にする `_nest_alternating_quotes` は周期2で種類が一巡するため、
    ちょうど2段だけ超過した場合（`cap+2`）は外側 `cap` 段の種類の並びが偶然「上限ちょうど」と
    一致し、文字列としては区別できない（これは実装の欠陥ではなく交互構成そのものの周期性）。
    区別できることを保証できるのは1段だけ超過した場合（`cap+1`）のみなので、その depth に
    絞って厳密不一致を固定する。この厳密な形の一致/不一致は、入れ子段数の数え方
    （`len(stack) - 1`）を1つずらす off-by-one（`len(stack)` に変える等）で崩れる＝
    `secret not in masked` だけでは検出できない（どちらの形でも secret 自体は漏れないため）。"""
    secret = "TOPSECRET-BEYOND-DEPTH-LIMIT"
    content = f"/openai/deployments/my-secret {secret} tail"
    cap = GE._MAX_QUOTE_NESTING_DEPTH

    text_at_cap = _nest_alternating_quotes(content, cap)
    masked_at_cap = GE._redact_reflected_urls(text_at_cap, None)
    expected_at_cap = _nest_alternating_quotes("[URL]", cap)
    assert masked_at_cap == expected_at_cap, (
        f"上限ちょうどの段で通常の対付け形から外れている: {masked_at_cap!r}")

    for depth in (cap, cap + 1, cap + 2):
        text = _nest_alternating_quotes(content, depth)
        masked = GE._redact_reflected_urls(text, None)
        assert secret not in masked, f"depth={depth}: {secret!r} が漏洩している: {masked!r}"
        if depth > cap:
            pre, _, post = masked.partition("[URL]")
            assert len(pre) == cap and len(post) == cap, (
                f"depth={depth}: 上限超過後に外側で生き残る引用符の段数が cap と一致しない: {masked!r}")
        if depth == cap + 1:
            assert masked != expected_at_cap, (
                f"depth={depth}: 上限超過時の出力が「上限ちょうど」の対付け形と区別できていない: {masked!r}")


def test_mask_quoted_url_spans_masks_remainder_when_no_enclosing_frame_can_rescue(monkeypatch):
    """上限超過時の fail-closed 処理を狙い撃ちで固定する: 上で確認した通常の入れ子
    （`_MAX_QUOTE_NESTING_DEPTH` を1〜2段超える程度）は、上限で対付けを打ち切られた区間の外側に
    正しく完了する引用符区間が必ず存在し、その外側区間自身の指標判定（`_quoted_content_
    has_url_indicator`・句読点を剥がしてから判定する契約込み）が救済してしまうため、
    「上限到達時のfail-closed処理」そのものの要否をこの構成だけでは区別できない（mutation で
    無効化しても他のテストは落ちない）。本テストは上限を意図的に 0 まで下げ、外側に一切
    完了する引用符区間が存在しない（最初の開き引用符自体が上限超過になる）状況を作ることで、
    「上限到達時は残り部分文字列に指標があれば丸ごと [URL] に伏せる」という本項目の修正
    そのものだけが secret を守っていることを固定する。"""
    monkeypatch.setattr(GE, "_MAX_QUOTE_NESTING_DEPTH", 0)
    secret = "TOPSECRET"
    text = f"outer '/openai/deployments/my-secret {secret}' tail"
    masked = GE._redact_reflected_urls(text, None)
    assert secret not in masked, f"{secret!r} が漏洩している: {masked!r}"


def test_http_detail_caps_error_body_read_size():
    """上流のエラー本文サイズを信用せず、`e.read()` を `_HTTP_ERROR_BODY_MAX_BYTES`
    バイトに制限する（悪意/誤動作した上流が巨大な本文を返してもメモリを消費し尽くさない）。
    上限を大幅に超える本文でも例外にならず、切り詰められた（壊れた JSON になる）分は
    `_http_detail` の既存の except 経路（`HTTP {code}` へのフォールバック）で安全に処理される。"""
    huge_message = "x" * (GE._HTTP_ERROR_BODY_MAX_BYTES * 2)
    body = json.dumps({"error": {"message": huge_message}}).encode("utf-8")
    assert len(body) > GE._HTTP_ERROR_BODY_MAX_BYTES
    fp = io.BytesIO(body)
    exc = urllib.error.HTTPError("https://example.com", 400, "error", {}, fp)
    detail, hint = GE._http_detail(exc)
    assert hint is None
    # 切り詰められた本文は妥当な JSON にならないため、except 経路の固定フォールバックになる
    # （huge_message の全長がそのまま detail に載ることはない＝メモリに残る量が上限で抑えられる）。
    assert detail == "HTTP 400"
    assert huge_message not in detail


# ===== `_log_masked_exception`（5xx/504 翻訳点で使う共通ロギングヘルパー・RV7）=====

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
    """`secret` 省略時も汎用パターン（Bearer 等）は引き続きマスクされる。"""
    log = _FakeLogger()
    GE._log_masked_exception(log, "ctx", RuntimeError("Authorization: Bearer sk-abcdefgh12345"))
    assert "Bearer [REDACTED]" in log.records[0] or "REDACTED" in log.records[0]


def test_log_masked_exception_non_string_secret_falls_back_to_none_without_raising():
    """RV7 是正の固定: `secret` が文字列以外（設定破損等で JSONB 値が想定外の型になっている
    場合）でも例外を投げず、汎用パターンだけを適用してログに残す（マスク自体は諦めない）。"""
    log = _FakeLogger()
    corrupted_secret = {"unexpected": "dict-not-a-string"}
    GE._log_masked_exception(log, "ctx", RuntimeError("plain message, no secret here"),
                             corrupted_secret)
    assert len(log.records) == 1
    assert "RuntimeError" in log.records[0]


def test_log_masked_exception_non_string_secret_str_form_is_masked():
    """RV8 是正の固定: `secret` が非文字列（設定破損）でも `str()` 化した全体をマスク対象へ
    加える（二重防御）——`llm.openai_headers()` の非文字列キー拒否をすり抜けた場合でも、
    `f"Bearer {key}"` のように dict がそのまま文字列化されて例外メッセージへエコーされた
    ケースを、汎用パターンだけに頼らず確実にマスクする（実際に再現した漏洩の固定）。"""
    log = _FakeLogger()
    corrupted_secret = {"unexpected": "AZUREKEY-SHOULDNOTLEAK-1234567890"}
    boom = RuntimeError(f"invalid header value: Bearer {corrupted_secret}")
    GE._log_masked_exception(log, "ctx", boom, corrupted_secret)
    assert len(log.records) == 1
    assert "AZUREKEY-SHOULDNOTLEAK-1234567890" not in log.records[0]
    assert "REDACTED" in log.records[0]


def test_log_masked_exception_masking_failure_is_swallowed_and_does_not_leak_via_context(monkeypatch):
    """RV7 是正の固定: マスク処理自体（`_mask_secrets`/`_redact_reflected_urls`）が想定外の例外を
    投げても、それを握り潰してプレースホルダでログに残す——ここで新しい例外を伝播させると、
    Python の暗黙連鎖（`__context__`）で元の（秘密を含みうる）例外にぶら下がったまま呼び出し元の
    外へ伝播し、せっかく隠した秘密が別の traceback 経由で復活してしまう。"""
    def _boom_mask(text, secret):
        raise TypeError("simulated masking bug")

    monkeypatch.setattr(GE, "_mask_secrets", _boom_mask)
    log = _FakeLogger()
    secret_in_original = "sk-SHOULDNOTLEAK1234567890"
    # 例外を投げずに正常終了し、ログには何らかのプレースホルダが残る。
    GE._log_masked_exception(log, "ctx", RuntimeError(f"boom: {secret_in_original}"), "irrelevant")
    assert len(log.records) == 1
    assert secret_in_original not in log.records[0]
