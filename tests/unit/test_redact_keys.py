"""store._redact / _REDACT_KEYS のキー伏字の単体契約（外部サービス不要の unit）。

R2b（2026-07-13-横断レビュー対応.md）の実変更＝`bedrock_api_key` を `_REDACT_KEYS` に追加した
ことを**直接**担保する。設定API応答の非漏洩テスト（tests/api/test_settings_key_redaction.py）は
`_public_settings` のマスク経由なので、`_REDACT_KEYS` から bedrock を外しても緑のまま通ってしまう。
ここは監査 detail/state に載る生キーが store 層の二重 redaction で必ず消えることを、キーごとに固定する。
"""
import pytest

pytestmark = pytest.mark.unit

from sherpa import store


def test_redact_strips_all_provider_api_keys():
    raw = "sk-super-secret-value-1234567890"
    detail = {
        "openai_api_key": raw,
        "gemini_api_key": raw,
        "bedrock_api_key": raw,  # R2b で追加した対象
        "nested": {"before_state": {"bedrock_api_key": raw}},
        "list": [{"openai_api_key": raw}],
    }
    red = store._redact(detail)
    assert red["openai_api_key"] == "<redacted>"
    assert red["gemini_api_key"] == "<redacted>"
    assert red["bedrock_api_key"] == "<redacted>"
    assert red["nested"]["before_state"]["bedrock_api_key"] == "<redacted>"
    assert red["list"][0]["openai_api_key"] == "<redacted>"
    # 生キー文字列が redaction 後のどこにも残らない（再帰全走査）。
    import json
    assert raw not in json.dumps(red)


def test_bedrock_api_key_is_in_redact_set():
    # 実変更そのものの回帰: 集合から外れたら即赤にする。
    for key in ("openai_api_key", "gemini_api_key", "bedrock_api_key"):
        assert key in store._REDACT_KEYS
