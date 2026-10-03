"""VLM（vision_arm）の Ollama 送信は、中央の Ollama 接続先と同じ許可リスト
（loopback＋管理画面の `ollama_allowlist`）で検証される。
`resolve_vlm()`（`_ollama_url_permitted`／`cloud_allowed`）の判定を通った接続先だけがここへ届く。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sherpa.ingest.arms import vision_arm

_PRIVATE = "http://192.168.50.50:11434"   # RFC1918


@pytest.fixture
def tiny_image(tmp_path) -> Path:
    p = tmp_path / "tiny.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    return p


def test_vlm_ollama_follows_central_url_and_admin_allowlist(monkeypatch, tiny_image):
    """許可リストに載った接続先は `llm.post_json` まで届き、載っていない接続先は SsrfBlocked になる。"""
    from sherpa import llm
    from sherpa.ingest.arms import vision_arm as va
    monkeypatch.setattr(va, "_cloud_allowed_now", lambda: True)
    calls = []
    monkeypatch.setattr("sherpa.llm.post_json",
                        lambda url, headers, body, timeout=None: calls.append(url) or {"message": {"content": "読み取り結果"}})
    monkeypatch.setattr("sherpa.metering.acc_add", lambda *a, **kw: None)
    cfg = {"ollama_url": _PRIVATE, "model": "qwen2.5vl"}

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"ollama_allowlist": ["192.168.50.50:11434"]})
    assert va._read_ollama(tiny_image, cfg, timeout=5) == "読み取り結果"
    assert calls and calls[0].startswith(_PRIVATE)

    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    with pytest.raises(llm.SsrfBlocked):
        va._read_ollama(tiny_image, cfg, timeout=5)

    # 接続先そのものは管理画面の中央の Ollama 接続先（未設定は既定）。環境変数は読まない。
    monkeypatch.setenv("SHERPA_VLM_OLLAMA_URL", "http://10.9.9.9:11434")
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"ollama_url": "http://10.0.0.5:11434"})
    assert vision_arm.vlm_config()["ollama_url"] == "http://10.0.0.5:11434"
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert vision_arm.vlm_config()["ollama_url"] == "http://localhost:11434"


def test_resolve_vlm_ollama_uses_central_allowlist_not_ip_literal(monkeypatch):
    """許可一覧にある DNS 名は cloud_allowed=false でも使え、許可されていない宛先は使えない。"""
    monkeypatch.delenv("SHERPA_VLM_USABLE", raising=False)
    host = "http://host.docker.internal:11434"
    base = {"vlm": {"provider": "ollama"}, "ollama_url": host}
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {**base, "ollama_allowlist": ["host.docker.internal:11434"]})
    assert vision_arm.resolve_vlm()["ollama_url"] == host
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: base)
    assert vision_arm.resolve_vlm() is None


def test_resolve_vlm_ollama_stops_when_central_url_unreadable(monkeypatch):
    """中央の接続先を読めないときは localhost へ置き換えず、VLM を使えないものとして止める。"""
    monkeypatch.delenv("SHERPA_VLM_USABLE", raising=False)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"vlm": {"provider": "ollama"}})
    monkeypatch.setattr("sherpa.keys.resolve_ollama_url", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    assert vision_arm.vlm_config()["ollama_url"] == ""
    assert vision_arm.resolve_vlm() is None


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
