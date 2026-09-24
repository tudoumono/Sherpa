"""VLM（vision_arm）の Ollama 送信が、自分の設定した接続先だけをこの呼び出しに閉じて許可する
（`llm.ollama_url(..., extra_allowed=...)`）ことを固定する。

VLM 専用 env（`SHERPA_VLM_OLLAMA_URL`）は一般の Ollama 許可リスト（`llm._allowlisted_hosts()`）へは
もう加算されない（`tests/contract/test_ssrf_allowlist.py` 参照）。VLM 自身の送信が引き続き動くのは、
`vision_arm._read_ollama` が自分の接続先だけを `extra_allowed` で明示的に許可しているため。
`resolve_vlm()`（`_is_local_url`／`cloud_allowed`）で既に許可判定を済ませた接続先のみがここへ届く。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sherpa.ingest.arms import vision_arm

_PRIVATE_UNLISTED = "http://192.168.50.50:11434"   # RFC1918・admin allowlist 未登録・env にも無い


@pytest.fixture
def tiny_image(tmp_path) -> Path:
    p = tmp_path / "tiny.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    return p


def test_read_ollama_allows_its_own_configured_destination_via_extra_allowed(monkeypatch, tiny_image):
    """一般 allowlist に登録が無い接続先でも、VLM 自身が `cloud_allowed`（ここでは true にして
    local 判定を迂回）で許可した接続先なら、`_read_ollama` は `extra_allowed` 経由で SsrfBlocked に
    ならず実際に `llm.post_json` まで届く。"""
    from sherpa.ingest.arms import vision_arm as va
    monkeypatch.setattr(va, "_cloud_allowed_now", lambda: True)

    calls = []

    def _fake_post_json(url, headers, body, timeout=None):
        calls.append(url)
        return {"message": {"content": "読み取り結果"}}

    monkeypatch.setattr("sherpa.llm.post_json", _fake_post_json)
    monkeypatch.setattr("sherpa.metering.acc_add", lambda *a, **kw: None)

    cfg = {"ollama_url": _PRIVATE_UNLISTED, "model": "qwen2.5vl"}
    result = va._read_ollama(tiny_image, cfg, timeout=5)
    assert result == "読み取り結果"
    assert calls and calls[0].startswith(_PRIVATE_UNLISTED)


def test_read_ollama_without_extra_allowed_would_be_blocked(monkeypatch):
    """回帰確認: `extra_allowed` を使わず一般の `assert_ollama_url_allowed` だけで同じ接続先を
    検証すると拒否される（＝ `_read_ollama` の許可は本当に `extra_allowed` に依っている・
    一般 allowlist が緩んだわけではないことの確認）。"""
    from sherpa import llm
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    with pytest.raises(llm.SsrfBlocked):
        llm.assert_ollama_url_allowed(_PRIVATE_UNLISTED)


def test_openai_key_returns_none_for_invalid_cloud_provider(monkeypatch, caplog):
    """`cloud_provider`（A7）が非空の不正値のとき、VLM(openai) は黙って既定（openai）へ倒れた
    キーで画像を送信しない＝`_openai_key` は既存契約どおり None（送信 OFF）に寄せる
    （意図しない課金の是正）。利用者向けエラーにはしないが、黙って握り潰さず管理者が診断できる
    ログを残す（呼び出し元の「OPENAI_API_KEY が未設定」という決め打ちの誤記録も避ける・
    strict 例外の黙殺の是正）。"""
    import logging

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True, "cloud_provider": "not-a-real-provider",
        "openai_api_key": "sk-x"})
    from sherpa.ingest.arms import vision_arm as va
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert va._openai_key() is None
    assert any("not-a-real-provider" in r.getMessage() for r in caplog.records)


def test_vlm_usable_disables_openai_with_accurate_log_when_cloud_provider_invalid(monkeypatch, caplog):
    """呼び出し元（`resolve_vlm`）は `_openai_key()` が None を返した理由を「OPENAI_API_KEY が
    未設定」と決め打ちで誤記録しない（実際の理由は cloud_provider 不正でも、キー自体は
    設定済みのことがある）。"""
    import logging

    from sherpa.ingest.arms import vision_arm as va

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True, "cloud_provider": "not-a-real-provider",
        "openai_api_key": "sk-x", "vlm": {"provider": "openai", "cloud_allowed": True}})
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert va.resolve_vlm() is None
    messages = [r.getMessage() for r in caplog.records]
    assert not any("OPENAI_API_KEY が未設定" in m for m in messages), messages
    assert any("not-a-real-provider" in m for m in messages), messages
    # この経路（cloud_provider 不正）は実際に `_openai_key_with_reason` が診断ログを残して
    # いるため「詳細は直前のログを参照」と案内してよい。
    assert any("直前のログを参照" in m for m in messages), messages


def test_vlm_usable_disables_openai_without_misleading_log_reference_when_key_merely_unset(
        monkeypatch, caplog):
    """キーが単に未設定（`cloud_provider` は正常）の場合、`_openai_key_with_reason` は診断ログを
    一切残さない＝`resolve_vlm` は「詳細は直前のログを参照」と案内しない（ログが実在しない
    経路にまで参照を促す誤案内を防ぐ）。"""
    import logging

    from sherpa.ingest.arms import vision_arm as va

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "personal_api_keys_allowed": True, "cloud_provider": "openai",
        "vlm": {"provider": "openai", "cloud_allowed": True}})   # openai_api_key 未設定
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert va.resolve_vlm() is None
    messages = [r.getMessage() for r in caplog.records]
    assert not any("直前のログを参照" in m for m in messages), messages
    assert any("未設定" in m for m in messages), messages
