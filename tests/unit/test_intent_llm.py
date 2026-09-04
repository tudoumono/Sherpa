"""intent_llm（Tier2 LLM 分類）単体テスト: 補完を差し替えてネットワーク無しで検証。

未接続(None)・正常分類・不正応答(None)・空入力(None) の各分岐。
"""
from __future__ import annotations

from sherpa import intent_llm as I

_KEY = {"openai_api_key": "test-key"}   # _cfg が openai を選ぶ（鍵あり）


def _with_complete(ret):
    orig = I._complete
    I._complete = lambda system, user, cfg: ret
    return orig


def test_none_without_provider_key():
    orig = I._cfg                                                  # 鍵なし＝cfg None（env キー有無に依存しない）
    I._cfg = lambda settings, **kw: None
    try:
        assert I.classify("税率を変えたら落ちる？", {}) is None
    finally:
        I._cfg = orig


def test_classify_valid():
    orig = _with_complete('{"lens":"impact","confident":true}')
    try:
        r = I.classify("税率を変えたら何に響く？", _KEY)
        assert r == {"lens": "impact", "confident": True}
    finally:
        I._complete = orig


def test_classify_low_confidence_passthrough():
    orig = _with_complete('{"lens":"qa","confident":false}')
    try:
        assert I.classify("消費税について", _KEY) == {"lens": "qa", "confident": False}
    finally:
        I._complete = orig


def test_classify_author_lens_valid():
    """P1-a（Codex 強化計画 Phase1）: author が有効な分類ラベルとして通る。"""
    orig = _with_complete('{"lens":"author","confident":true}')
    try:
        assert I.classify("これをパワポにまとめて", _KEY) == {"lens": "author", "confident": True}
    finally:
        I._complete = orig


def test_bad_or_unknown_lens_returns_none():
    for ret in ('{"lens":"bogus","confident":true}', '{not json', '{"x":1}', '"impact"'):
        orig = _with_complete(ret)
        try:
            assert I.classify("なにか", _KEY) is None
        finally:
            I._complete = orig


def test_empty_message_none():
    assert I.classify("   ", _KEY) is None


def test_cfg_model_catalog_override(monkeypatch):
    """モデル名は個人設定の `intent_model`（もう読まれない）でなく、管理者のカタログ
    （openai/intent）から解決される。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True,
        "model_catalog": {"openai": {"intent": {"allowed": ["my-cheap-model"],
                                                 "default": "my-cheap-model"}}}})
    cfg = I._cfg({"openai_api_key": "test-key", "intent_model": "ignored-value"})
    assert cfg["model"] == "my-cheap-model"
    assert cfg["provider"] == "openai"


def test_cfg_intent_model_default_unchanged(monkeypatch):
    """個人設定の `intent_model` はもう読まれない＝消費側のハードコード既定のまま。"""
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})
    cfg = I._cfg({"openai_api_key": "test-key"})
    assert cfg["model"] == "gpt-4o-mini"
    cfg = I._cfg({"openai_api_key": "test-key", "intent_model": "ignored-value"})
    assert cfg["model"] == "gpt-4o-mini"


def test_cfg_returns_none_for_invalid_cloud_provider(monkeypatch, caplog):
    """`cloud_provider`（A7）が非空の不正値のとき、黙って既定（openai）へ倒れたキーで意図分類を
    送信しない＝`_cfg` は既存契約どおり None（`classify` は clarify へ縮退）に寄せる
    （「主AIがローカルでも intent 分類だけ課金され得る」経路の是正）。利用者向けエラーにはしない
    が、黙って握り潰さず管理者が診断できるログを残す（strict 例外の黙殺の是正）。"""
    import logging

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True, "cloud_provider": "not-a-real-provider"})
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert I._cfg({"openai_api_key": "test-key"}) is None
        assert I.classify("税率を変えたら何に響く？", {"openai_api_key": "test-key"}) is None
    assert any("not-a-real-provider" in r.getMessage() for r in caplog.records)


def test_classify_swallows_cfg_resolution_exception_and_returns_none(monkeypatch, caplog):
    """RV1（FBK-1・境界回帰#6・2026-09-01）: `_cfg()` 自体が例外を送出（例: `store.
    get_system_settings()` の DB 一時障害）しても `classify()` はチャット全体へ伝播させず None
    （既存の clarify 縮退経路）に丸める。ログにはクラス名だけを残す（秘密や生の例外文言は残さない）。"""
    import logging

    orig = I._cfg

    def _boom(settings, **kw):
        raise RuntimeError("db unreachable: secret-token-should-not-leak")

    I._cfg = _boom
    try:
        with caplog.at_level(logging.WARNING, logger="sherpa"):
            assert I.classify("税率を変えたら何に響く？", {}) is None
        assert any("RuntimeError" in r.getMessage() for r in caplog.records)
        assert not any("secret-token-should-not-leak" in r.getMessage() for r in caplog.records)
    finally:
        I._cfg = orig


def test_string_confident_coerced():
    # LLM が confident を文字列で返しても正しく解釈（bool("false")==True の罠を回避・RV MED）。
    orig = _with_complete('{"lens":"impact","confident":"false"}')
    try:
        assert I.classify("x", _KEY) == {"lens": "impact", "confident": False}
    finally:
        I._complete = orig
    orig = _with_complete('{"lens":"qa","confident":"true"}')
    try:
        assert I.classify("x", _KEY) == {"lens": "qa", "confident": True}
    finally:
        I._complete = orig
