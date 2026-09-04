"""`sherpa.routers.system._audit_settings_update` の監査記録の伏せ字を固定する。

多層防御: 通常は保存前の `llm.assert_ollama_url_allowed`（`_canonical_host_port` が userinfo 付き
URL を拒否する）で userinfo 付き `ollama_url` は 422 になり、ここへは届かないはずだが、監査ログ側
でも念のため除去する（`tests/contract/test_ssrf_allowlist.py::test_canonical_host_port_rejects_userinfo`
が入口側の拒否を固定する）。
"""
from __future__ import annotations

from sherpa.routers import system as system_router
from sherpa.store import settings as store_settings


def test_ollama_url_userinfo_is_stripped_from_audit_record(monkeypatch):
    calls = []
    monkeypatch.setattr("sherpa.store.audit", lambda *a, **kw: calls.append(kw))
    system_router._audit_settings_update("u1", {"ollama_url": "http://admin:secret@10.0.0.5:11434"})
    assert calls
    changed = calls[0]["detail"]["changes"]
    assert "secret" not in changed["ollama_url"]
    assert "admin" not in changed["ollama_url"]
    assert "10.0.0.5:11434" in changed["ollama_url"]


def test_ollama_url_without_userinfo_is_reduced_to_host_port_in_audit_record(monkeypatch):
    """scheme も落とし host:port のみを監査へ残す契約
    （`test_redact_url_for_error_strips_query_and_fragment` と同じ host 表現）。"""
    calls = []
    monkeypatch.setattr("sherpa.store.audit", lambda *a, **kw: calls.append(kw))
    system_router._audit_settings_update("u1", {"ollama_url": "http://10.0.0.5:11434"})
    changed = calls[0]["detail"]["changes"]
    assert changed["ollama_url"] == "10.0.0.5:11434"


def test_api_keys_still_masked_as_set_cleared(monkeypatch):
    calls = []
    monkeypatch.setattr("sherpa.store.audit", lambda *a, **kw: calls.append(kw))
    system_router._audit_settings_update("u1", {"openai_api_key": "sk-real-secret", "gemini_api_key": ""})
    changed = calls[0]["detail"]["changes"]
    assert changed["openai_api_key"] == "<set>"
    assert changed["gemini_api_key"] == "<cleared>"


# ---- `sherpa.store.settings._redact_secret_settings`（admin `PUT /admin/settings`／env シードの
# `system_settings.updated`／`system_settings.env_seeded` 監査）----
# `_audit_settings_update`（上記）とは別の関数・別の呼び出し経路（`store.set_system_settings`／
# `store.seed_system_settings_once`）だが、同じ「監査へ生の接続先 URL を残さない」契約を持つ。

def test_openai_base_url_redacted_to_host_in_admin_audit():
    """host 表現は scheme を含めない（`_redact_url_for_error` の契約変更）。"""
    out = store_settings._redact_secret_settings(
        {"openai_base_url": "https://myres.openai.azure.com/openai/deployments/my-secret-deploy"}, None)
    assert out["openai_base_url"] == "myres.openai.azure.com"
    assert "my-secret-deploy" not in out["openai_base_url"]


def test_ollama_url_redacted_to_host_in_admin_audit_settings_ledger():
    """`store.settings._redact_secret_settings`（`system_settings.updated`/`env_seeded` 監査）は
    `ollama_url` も他の URL キーと同列に host 表現へ畳む。`catchup_ollama_allowlist_for_env_seeded_url_v2`
    の tamper 検知は、この畳んだ表現ではなく専用の `ollama_url_fingerprint`（正規化 host:port・
    `seed_system_settings_once` が別フィールドとして記録する）を比較する＝生 URL を監査に残す
    必要がない（`_URL_SETTINGS_KEYS`／`llm.ollama_url_fingerprint` の docstring 参照）。"""
    out = store_settings._redact_secret_settings({"ollama_url": "http://10.0.0.5:11434/api"}, None)
    assert out["ollama_url"] == "10.0.0.5:11434"


def test_url_settings_cleared_value_maps_to_cleared_sentinel():
    out = store_settings._redact_secret_settings({"openai_base_url": None}, None)
    assert out["openai_base_url"] == "<cleared>"


def test_openai_base_url_invalid_legacy_value_does_not_leak_raw_content_in_audit():
    """保存時検証（`llm.assert_openai_base_url_allowed`）の強化より前に保存された不正値
    （バックスラッシュ混入等）は、`urlparse` がこれらを構造区切りとして扱わないため
    `_redact_url_for_error` がそのまま `hostname` として返してしまい、監査へ内部パスの断片が
    生で残っていた。再検証して不合格なら host 表現を作らず固定文字列へ畳む。"""
    out = store_settings._redact_secret_settings(
        {"openai_base_url": "https://host.example\\internal\\secret"}, None)
    assert out["openai_base_url"] == "<不正なURL>"
    assert "internal" not in out["openai_base_url"]
    assert "secret" not in out["openai_base_url"]


def test_openai_base_url_non_string_value_does_not_raise_and_falls_back_to_fixed_string():
    """`v` が数値・配列・辞書等の非文字列だと `assert_openai_base_url_allowed`
    （`for c in base` 等・文字列前提の処理）に通すと `ValueError` 以外の例外（`TypeError` 等）を
    送出しうる。ここは監査記録のフェイルセーフ経路であり、非文字列は検証を試みる前に型で弾いて
    固定文字列へ倒すことで、どんな値が混入しても監査記録（延いては修復 PUT）自体は必ず成功する
    ことを固定する。

    `<cleared>`（`None`／空文字列＝真の未設定）とは区別する固定文字列（`(不正な保存値)`）にする:
    一操作復旧（`_assert_openai_endpoint_update_consistent`）が拾う対象の破損値がここを通る際、
    `<cleared>` に畳んでしまうと監査の before-state から「実際には破損値が保存されていた」という
    事実が消えてしまう。"""
    for bad_value in (12345, ["https://host.example"], {"nested": "value"}, {}, [], 0, False):
        out = store_settings._redact_secret_settings({"openai_base_url": bad_value}, None)
        assert out["openai_base_url"] == "(不正な保存値)", f"{bad_value!r}: {out['openai_base_url']!r}"


def test_url_settings_redaction_applies_even_without_secret_keys():
    """`secret_keys` を渡さない呼び出しでも URL キーは畳まれる（openai_base_url 単体の
    変更等、secret_keys が空の PUT でも生 URL を audit_log へ残さない）。"""
    out = store_settings._redact_secret_settings(
        {"openai_base_url": "https://myres.openai.azure.com/openai/v1", "legacy_backend": "libreoffice"},
        frozenset())
    assert out["openai_base_url"] == "myres.openai.azure.com"
    assert out["legacy_backend"] == "libreoffice"   # 無関係キーはそのまま


def test_non_url_non_secret_keys_pass_through_unchanged():
    out = store_settings._redact_secret_settings({"arms_enabled": ["ooxml"]}, {"openai_api_key"})
    assert out["arms_enabled"] == ["ooxml"]
