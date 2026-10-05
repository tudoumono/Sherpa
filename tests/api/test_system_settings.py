"""全体設定 API（GET/PUT /admin/settings）と system_settings の初回シード（実 DB）のテスト。

- 認可（非 admin 403・未ログイン 401）・検証（範囲外/不正値は 422 で保存されない）・実効反映（system_settings > env > 既定）。
- 監査（system_settings.updated・severity=warning）・fail-closed（監査失敗で変更を取り消し 500）。
- 個人 API キーの一括削除・シード（store.seed_system_settings_once ほか）の意味論。

要 Postgres。DB 不可は SKIP。system_settings は全体（1 世界共有）なので各テスト前後で全消去する。
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app
from sherpa.ingest import arms, office_md

_ADMIN = "/admin/settings"


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _clear_system_settings() -> None:
    try:
        with store._connect() as c:
            c.execute("DELETE FROM system_settings")
        store._invalidate_system_settings_cache()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clean_system_settings():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")
    _clear_system_settings()
    yield
    _clear_system_settings()


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@sys.local", display_name=uid,
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def _new_client(role: str, prefix: str):
    sfx = _sfx()
    uid, pw = f"{prefix}{sfx}", f"Pw{sfx}"
    _mk_user(uid, pw, role=role)
    return _login(uid, pw), uid


@pytest.fixture
def admin_pair():
    return _new_client("admin", "sysadm")


@pytest.fixture
def admin(admin_pair) -> TestClient:
    return admin_pair[0]


@pytest.fixture
def admin_uid(admin_pair) -> str:
    return admin_pair[1]


def _put(admin, body):
    return admin.put(_ADMIN, json=body)


def _soffice_present(monkeypatch) -> None:
    """libreoffice を保存対象にするテストは soffice の有無に依らず通す（未導入なら保存は 422）。"""
    from sherpa.ingest.arms import legacy_convert
    monkeypatch.setattr(legacy_convert, "soffice_available", lambda: True)


def _insert_raw_setting(key, value):
    from psycopg.types.json import Json
    with store._connect() as c:
        c.execute(
            "INSERT INTO system_settings (key, value, updated_by) VALUES (%s, %s, 'test') "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (key, Json(value)))


# ===== 認可 =====

@pytest.mark.parametrize("body", [
    {"legacy_backend": "libreoffice"}, {"web_search_allowed": True}, {"chat_examples": {"items": ["x"]}}])
def test_admin_settings_gates(body):
    """未ログインは 401・非 admin は 403（GET/PUT とも）。"""
    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.get(_ADMIN).status_code == 401
    assert _put(anon, body).status_code == 401

    user, _ = _new_client("user", "sysusr")
    assert user.get(_ADMIN).status_code == 403
    assert _put(user, body).status_code == 403


# ===== GET の形（未設定） =====

def test_admin_settings_get_shape(admin):
    from sherpa import schemas as sc
    r = admin.get(_ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    # 余剰キーを無視する pydantic でも、スキーマへの追加漏れを検出できるよう完全一致で固定する
    assert set(body.keys()) == set(sc.AdminSettingsView.model_fields.keys())
    assert body["arms"]["known"] == arms.known_arm_names()
    assert set(body["arms"]["known"]) == {"ooxml", "pdf_text", "vision"}
    assert body["arms"]["configured"] is None
    assert set(body["arms"]["enabled"]) == set(arms.env_default_arm_names())
    avail = body["arms"]["available"]
    assert set(avail) == set(arms.known_arm_names()) and avail["ooxml"] is True
    assert all(isinstance(v, bool) for v in avail.values())
    assert "token_prices" not in body and "usd_jpy" not in body   # 金額系は撤去済み
    lb = body["legacy_backend"]
    assert lb["configured"] is None
    assert lb["effective"] in ("none", "libreoffice", "office_com")
    assert {"none", "libreoffice", "office_com"} <= set(lb["options"])
    assert isinstance(lb["libreoffice"]["available"], bool)
    # conftest が SHERPA_POWERSHELL_BIN を無効パスに固定するため URL 未設定のここでは mode="unavailable"
    assert isinstance(lb["office_com"]["configured_url"], bool)
    assert lb["office_com"]["mode"] in ("direct", "http", "unavailable")
    assert isinstance(lb["office_com"]["powershell"], bool)
    assert isinstance(lb["office_com"]["available"], bool)
    assert "versions" in lb["office_com"]
    vlm = body["vlm"]
    assert vlm["configured"] is None
    assert vlm["effective"]["provider"] in ("ollama", "openai")
    assert vlm["effective"]["cloud_allowed"] is False
    assert vlm["default"]["cloud_allowed"] is False
    assert vlm["providers"] == ["ollama", "openai"]
    assert isinstance(vlm["available"], bool) and isinstance(vlm["openai_key_present"], bool)
    csr = body["codex_session_retention_days"]
    assert csr["configured"] is None and csr["effective"] == 30 and csr["default"] == 30
    # cloud_provider を一度も PUT していなければ provider_raw は null（`provider` は既定込みの openai）
    assert body["cloud"]["provider"] == "openai"
    assert body["cloud"]["provider_raw"] is None
    assert body["cloud"]["web_search_allowed"] is False


def test_admin_settings_get_unset_defaults(admin):
    from sherpa import agentic_search, chat_turns, depth_profile, impact_service, lens_service, chat_service
    from sherpa import chat_examples as ce
    body = admin.get(_ADMIN).json()

    rdp = body["ext_keys"]["research_default_provider"]
    assert rdp == {"configured": None, "effective": "ollama", "default": "ollama"}

    cmt = body["chat_max_turns"]
    assert set(cmt.keys()) == {"per_user", "global"}
    for key, default in (("per_user", chat_turns.MAX_TURNS_PER_USER), ("global", chat_turns.MAX_TURNS_GLOBAL)):
        assert cmt[key] == {"configured": None, "effective": default, "default": default}

    ab = body["agentic_budget"]
    assert set(ab.keys()) == {"per_result"}   # モデル窓由来の上限は撤去済み
    assert ab["per_result"] == {"configured": None, "effective": agentic_search.TOOL_RESULT_MAX_BYTES,
                                "default": agentic_search.TOOL_RESULT_MAX_BYTES}

    dp = body["depth_profile"]
    assert set(dp.keys()) == set(depth_profile.BASE_SETTINGS_KEYS)
    for key, default in (
        ("grep_max_hits", agentic_search.MAX_HITS),
        ("qa_max_hits", chat_service.QA_MAX_HITS_DEFAULT), ("read_window", agentic_search.READ_WINDOW),
        ("impact_depth", impact_service.IMPACT_MAX_DEPTH),
        ("troubleshoot_depth", lens_service.TROUBLESHOOT_GRAPH_DEPTH),
    ):
        assert dp[key] == {"configured": None, "effective": default, "default": default}, key
    assert dp["codex_reasoning"]["configured"] is None
    assert dp["codex_reasoning"]["effective"] == dp["codex_reasoning"]["default"] == "medium"
    assert set(dp["codex_reasoning"]["options"]) == set(depth_profile.CODEX_REASONING_LEVELS)

    ws = body["workspace"]
    assert ws["max_bytes"] == {"configured": None, "effective": 10 * 1024 * 1024, "default": 10 * 1024 * 1024}
    assert ws["ttl_days"] == {"configured": None, "effective": 90, "default": 90}

    view = body["chat_examples"]
    assert view["configured"] is None
    assert view["effective"] == view["default"] == list(ce.DEFAULT_ITEMS)
    assert view["max_items"] == ce.MAX_ITEMS
    assert view["max_item_length"] == ce.MAX_ITEM_LENGTH

    mc = body["model_catalog"]
    assert mc["configured"] is None
    assert mc["effective"]["openai"]["chat"]["default"] == "gpt-5.5"
    assert mc["effective"]["ollama"]["chat"]["allowed"] == ["qwen2.5"]
    assert mc["effective"]["codex"]["codex"]["default"] == "gpt-5.5"
    assert "bedrock" not in mc["effective"] and "gemini" not in mc["effective"]
    assert set(mc["providers"]) == {"openai", "ollama", "codex"}
    assert set(mc["usages"]) == {"chat", "intent", "embed", "subsearch", "codex", "render"}


# ===== 検証（422・保存されない） =====

_REJECTED_BODIES = [
    # arms / legacy_backend
    {"arms_enabled": ["bogus-arm"]}, {"legacy_backend": "bogus"}, {"legacy_backend": 123},
    # vlm: 非オブジェクト・未知 provider・空 model・非 bool cloud・未知キー
    {"vlm": "notdict"}, {"vlm": {"provider": "gemini"}}, {"vlm": {"model": ""}},
    {"vlm": {"cloud_allowed": "yes"}}, {"vlm": {"bogus": 1}},
    # codex_session_retention_days: 0 以上の整数のみ（StrictInt で bool/数値文字列も拒否）
    {"codex_session_retention_days": -1}, {"codex_session_retention_days": 1.5},
    {"codex_session_retention_days": "abc"}, {"codex_session_retention_days": True},
    {"codex_session_retention_days": False}, {"codex_session_retention_days": "14"},
    # cloud_provider: 閉じた/未知のプロバイダは選べない
    {"cloud_provider": "gemini"}, {"cloud_provider": "bedrock"}, {"cloud_provider": "GEMINI"},
    {"cloud_provider": " bedrock "}, {"cloud_provider": "not-a-real-provider"},
    # 中央キーへの制御文字混入（strip 前の生値で検査・改行のみはクリア扱いにしない）
    {"openai_api_key": "sk-good-prefix\nAuthorization: evil"}, {"openai_api_key": "sk-good-prefix\r\nX-Injected: 1"},
    {"openai_api_key": "sk-good-prefix\x00tail"}, {"openai_api_key": "\r\nsk-ok\r\n"}, {"openai_api_key": "\n"},
    # model_catalog
    {"model_catalog": "notdict"}, {"model_catalog": {"openai": "notdict"}},
    {"model_catalog": {"openai": {"chat": "notdict"}}},
    {"model_catalog": {"openai": {"chat": {"allowed": "notalist"}}}},
    {"model_catalog": {"openai": {"chat": {"allowed": [1, 2]}}}},
    {"model_catalog": {"openai": {"chat": {"allowed": [], "default": 1}}}},
    {"model_catalog": {"opneai": {"chat": {"allowed": ["x"], "default": "x"}}}},          # 未知 provider
    {"model_catalog": {"openai": {"bogus-usage": {"allowed": ["x"], "default": "x"}}}},   # 未知 usage
    # 真偽値・語彙
    {"web_search_allowed": "yes"}, {"web_search_allowed": 1}, {"web_search_allowed": "true"},
    {"research_default_provider": "gemini"},
    {"depth_base_codex_reasoning": "ultra"}, {"codex_mode": "invalid"},
    {"embed_provider": "gemini"}, {"embed_provider": ""}, {"embed_provider": 3},
    # 整数の範囲（StrictInt+Field(ge,le)）・非整数・bool
    {"depth_base_grep_max_hits": 0}, {"depth_base_grep_max_hits": 5000}, {"depth_base_read_window": 5},
    {"depth_base_impact_depth": 100}, {"depth_base_troubleshoot_depth": 0},
    {"depth_base_grep_max_hits": "twelve"}, {"depth_base_grep_max_hits": True},
    *[{"embed_parallel": b} for b in (0, -1, 17, True, "3", 1.5)],
    *[{"max_review_rounds": b} for b in (0, -1, 33, True, "3", 1.5)],
    {"chat_max_turns_per_user": 0}, {"chat_max_turns_per_user": 17},
    {"chat_max_turns_global": 0}, {"chat_max_turns_global": 65},
    {"chat_max_turns_per_user": "five"}, {"chat_max_turns_per_user": True},
    {"agentic_budget_per_result": 0}, {"agentic_budget_per_result": -1},
    {"agentic_budget_per_result": 1023}, {"agentic_budget_per_result": 8 * 1024 * 1024 + 1},
    {"agentic_budget_per_result": "big"}, {"agentic_budget_per_result": True},
    {"workspace_max_bytes": 1024}, {"workspace_max_bytes": 2 * 1024 ** 3},
    {"workspace_ttl_days": -1}, {"workspace_ttl_days": 3651},
    # codex_worker_model の制御文字（TOML の 1 行文字列を壊す）
    {"codex_worker_model": "gpt-5.9\rinjected"}, {"codex_worker_model": "gpt-5.9\ninjected"},
    {"codex_worker_model": "gpt-5.9\x7f"},
    # chat_examples の形
    {"chat_examples": "not-a-dict"}, {"chat_examples": 123}, {"chat_examples": {"enabled": "yes"}},
    {"chat_examples": {"items": "not-a-list"}}, {"chat_examples": {"items": [1, 2]}},
    {"chat_examples": {"items": ["x"] * 9}}, {"chat_examples": {"items": ["x" * 201]}},
]


@pytest.mark.parametrize("body", _REJECTED_BODIES)
def test_admin_settings_put_rejected_with_422_and_not_stored(admin, body):
    r = _put(admin, body)
    assert r.status_code == 422, r.text
    store._invalidate_system_settings_cache()
    stored = store.get_system_settings()
    for key in body:
        assert key not in stored, f"{key} が 422 なのに保存されている"


def test_admin_settings_secret_key_normal_value_saves_and_empty_string_clears(admin):
    """前後空白のみの通常キーは保存でき（過剰検知の否定）、明示的な空文字だけがクリアになる。"""
    assert _put(admin, {"openai_api_key": "  sk-perfectly-normal-key  "}).status_code == 200
    assert _put(admin, {"openai_api_key": "sk-temp-value-before-clear"}).status_code == 200
    assert _put(admin, {"openai_api_key": ""}).status_code == 200


def test_admin_settings_retired_provider_keys_are_ignored(admin):
    """閉じたプロバイダの API キーは未知フィールドとして無視され保存されない。"""
    r = _put(admin, {"gemini_api_key": "gk-ignored", "bedrock_api_key": "bk-ignored"})
    assert r.status_code == 200, r.text
    sysset = store.get_system_settings()
    assert "gemini_api_key" not in sysset and "bedrock_api_key" not in sysset
    assert "gemini_key_set" not in r.json()["cloud"] and "bedrock_key_set" not in r.json()["cloud"]


def test_admin_settings_cloud_provider_raw_persists_even_when_equal_to_default(admin):
    """既定と同じ openai を明示 PUT しても生の保存値が残る（A7 の fail-loud 判定が効く前提）。"""
    assert admin.get(_ADMIN).json()["cloud"]["provider_raw"] is None
    r = _put(admin, {"cloud_provider": "openai"})
    assert r.status_code == 200, r.text
    assert r.json()["cloud"]["provider"] == "openai"
    assert r.json()["cloud"]["provider_raw"] == "openai"
    assert admin.get(_ADMIN).json()["cloud"]["provider_raw"] == "openai"


def test_admin_settings_legacy_backend_rejected_when_libreoffice_missing(monkeypatch, admin):
    from sherpa import required_tools
    from sherpa.ingest.arms import legacy_convert
    monkeypatch.setattr(legacy_convert, "soffice_available", lambda: False)
    monkeypatch.setattr(legacy_convert, "soffice_version", lambda: None)
    required_tools.snapshot(force=True)
    tools = {t["id"]: t for t in admin.get(_ADMIN).json()["required_tools"]}
    assert tools["libreoffice"]["installed"] is False
    r = _put(admin, {"legacy_backend": "libreoffice"})
    assert r.status_code == 422
    assert "LibreOffice が入っていません" in r.text
    assert _put(admin, {"legacy_backend": "none"}).status_code == 200


def test_put_settings_agent_codex_rejected_when_codex_cli_missing(monkeypatch, admin):
    from sherpa import required_tools
    monkeypatch.setattr(required_tools, "codex_cli_missing", lambda: True)
    r = admin.put("/settings", json={"agent": "codex"})
    assert r.status_code == 422
    assert "Codex CLI が入っていません" in r.text
    monkeypatch.setattr(required_tools, "codex_cli_missing", lambda: False)
    assert admin.put("/settings", json={"agent": "codex"}).status_code == 200


# ===== 実効反映（保存→effective・null で未設定へ戻る・他キーの更新で消えない） =====

# (PUT のキー, 値, 応答内の view への経路, null 後の既定値（None なら view["default"]）)
_ROUNDTRIPS = [
    ("depth_base_grep_max_hits", 50, ("depth_profile", "grep_max_hits"), None),
    ("depth_base_qa_max_hits", 25, ("depth_profile", "qa_max_hits"), None),
    ("depth_base_read_window", 80, ("depth_profile", "read_window"), None),
    ("depth_base_impact_depth", 12, ("depth_profile", "impact_depth"), None),
    ("depth_base_troubleshoot_depth", 6, ("depth_profile", "troubleshoot_depth"), None),
    ("depth_base_codex_reasoning", "high", ("depth_profile", "codex_reasoning"), None),
    ("chat_max_turns_per_user", 5, ("chat_max_turns", "per_user"), None),
    ("chat_max_turns_global", 30, ("chat_max_turns", "global"), None),
    ("agentic_budget_per_result", 100_000, ("agentic_budget", "per_result"), None),
    ("embed_parallel", 8, ("embed_parallel",), 4),
    ("max_review_rounds", 3, ("max_review_rounds",), 7),
    ("embed_provider", "ollama", ("embed_provider",), "auto"),
    ("codex_mode", "plain", ("codex_mode",), "standard"),
    ("codex_worker_model", "gpt-5.9-custom", ("codex_worker_model",), None),
    ("codex_session_retention_days", 14, ("codex_session_retention_days",), 30),
    ("codex_session_retention_days", 0, ("codex_session_retention_days",), 30),   # 0＝明示的に無制限
    ("research_default_provider", "ollama", ("ext_keys", "research_default_provider"), "ollama"),
]


@pytest.mark.parametrize("field, value, path, default_after_reset", _ROUNDTRIPS)
def test_admin_settings_put_roundtrip_reflects_and_resets(admin, field, value, path, default_after_reset):
    def view(resp):
        v = resp.json()
        for p in path:
            v = v[p]
        return v

    r = _put(admin, {field: value})
    assert r.status_code == 200, r.text
    v = view(r)
    assert v["configured"] == value and v["effective"] == value
    assert store.get_system_settings()[field] == value

    # 他の項目の部分更新で保存値が消えない
    assert _put(admin, {"chat_examples": {"items": ["z"]}}).status_code == 200
    assert view(admin.get(_ADMIN))["configured"] == value

    r2 = _put(admin, {field: None})
    assert r2.status_code == 200, r2.text
    v2 = view(r2)
    expected = v2["default"] if default_after_reset is None else default_after_reset
    assert v2["configured"] is None and v2["effective"] == expected
    assert default_after_reset is None or v2["default"] == expected


def test_admin_settings_arms_enabled_reflects_in_convertible_exts_and_resets(admin):
    r = _put(admin, {"arms_enabled": ["ooxml"]})
    assert r.status_code == 200, r.text
    assert r.json()["arms"]["configured"] == ["ooxml"]
    assert r.json()["arms"]["enabled"] == ["ooxml"]
    exts = office_md.convertible_exts()
    assert ".pdf" not in exts and ".docx" in exts   # pdf_text 無効＝PDF は MD 化対象外

    r2 = _put(admin, {"arms_enabled": None})
    assert r2.status_code == 200, r2.text
    assert r2.json()["arms"]["configured"] is None
    assert set(office_md.convertible_exts()) >= {".docx"}
    assert set(r2.json()["arms"]["enabled"]) == set(arms.env_default_arm_names())


def test_admin_settings_legacy_backend_reflects_resets_and_keeps_other_keys(monkeypatch, admin):
    _soffice_present(monkeypatch)
    r = _put(admin, {"legacy_backend": "libreoffice"})
    assert r.status_code == 200, r.text
    assert r.json()["legacy_backend"]["configured"] == "libreoffice"
    assert r.json()["legacy_backend"]["effective"] == "libreoffice"

    # arms_enabled だけの更新でも legacy_backend は残る
    r_partial = _put(admin, {"arms_enabled": ["ooxml", "pdf_text"]})
    assert r_partial.json()["legacy_backend"]["configured"] == "libreoffice"
    assert r_partial.json()["arms"]["configured"] == ["ooxml", "pdf_text"]

    r2 = _put(admin, {"legacy_backend": "none"})   # 明示 none は未設定へ畳まず生値のまま
    assert r2.status_code == 200, r2.text
    assert r2.json()["legacy_backend"]["configured"] == "none"

    r3 = _put(admin, {"legacy_backend": None})
    assert r3.status_code == 200, r3.text
    assert r3.json()["legacy_backend"]["configured"] is None


def test_admin_settings_legacy_backend_office_com_allowed(admin):
    """office_com はワーカー未起動でも設定自体は保持する（変換不可＝unavailable）。"""
    r = _put(admin, {"legacy_backend": "office_com"})
    assert r.status_code == 200, r.text
    lb = r.json()["legacy_backend"]
    assert lb["configured"] == "office_com" and lb["effective"] == "office_com"
    assert lb["office_com"]["mode"] == "unavailable"
    assert lb["office_com"]["available"] is False
    assert lb["office_com"]["configured_url"] is False


def test_admin_settings_vlm_reflects_and_resets(admin):
    """provider=openai・cloud_allowed=false でも保存は許可（実効は無効＝画像を送らない・fail-safe）。"""
    r = _put(admin, {"vlm": {"provider": "openai", "model": "gpt-4o", "cloud_allowed": False}})
    assert r.status_code == 200, r.text
    vlm = r.json()["vlm"]
    assert vlm["configured"] == {"provider": "openai", "model": "gpt-4o", "cloud_allowed": False}
    assert vlm["effective"]["provider"] == "openai" and vlm["effective"]["model"] == "gpt-4o"
    assert vlm["available"] is False

    r2 = _put(admin, {"vlm": {"provider": "ollama", "model": "qwen2.5vl"}})
    assert r2.status_code == 200, r2.text
    assert r2.json()["vlm"]["available"] is True

    r3 = _put(admin, {"vlm": None})
    assert r3.status_code == 200, r3.text
    assert r3.json()["vlm"]["configured"] is None
    assert r3.json()["vlm"]["effective"]["cloud_allowed"] is False


def test_admin_settings_workspace_limits_reflect_validate_and_reset(admin):
    r = _put(admin, {"workspace_max_bytes": 2 * 1024 * 1024, "workspace_ttl_days": 0})
    assert r.status_code == 200, r.text
    ws = r.json()["workspace"]
    assert ws["max_bytes"]["effective"] == 2 * 1024 * 1024 and ws["ttl_days"]["effective"] == 0
    r3 = _put(admin, {"workspace_max_bytes": None, "workspace_ttl_days": None})
    assert r3.status_code == 200, r3.text
    assert r3.json()["workspace"]["max_bytes"]["configured"] is None
    assert r3.json()["workspace"]["ttl_days"]["effective"] == 90


def test_admin_settings_web_search_allowed_round_trip(admin):
    assert admin.get(_ADMIN).json()["cloud"]["web_search_allowed"] is False
    r1 = _put(admin, {"web_search_allowed": True})
    assert r1.status_code == 200, r1.text
    assert r1.json()["cloud"]["web_search_allowed"] is True
    assert admin.get(_ADMIN).json()["cloud"]["web_search_allowed"] is True
    r2 = _put(admin, {"web_search_allowed": None})
    assert r2.status_code == 200, r2.text
    assert r2.json()["cloud"]["web_search_allowed"] is False


def test_admin_settings_codex_worker_model_blank_resets_and_azure_effective_uses_main_model(admin):
    assert _put(admin, {"codex_worker_model": "gpt-5.9-custom"}).status_code == 200
    blank = _put(admin, {"codex_worker_model": "  "})
    assert blank.status_code == 200, blank.text
    w = blank.json()["codex_worker_model"]
    assert w["configured"] is None and w["effective"] == w["default"]

    # Azure 接続先で未設定なら、effective は実際に適用される本体 Codex のカタログ既定（固定フォールバックではない）
    r = _put(admin, {"openai_endpoint_kind": "azure", "openai_base_url": "https://myres.openai.azure.com/openai/v1"})
    assert r.status_code == 200, r.text
    worker = r.json()["codex_worker_model"]
    assert worker["configured"] is None
    assert worker["effective"] == "gpt-5.5"
    assert worker["effective"] != worker["default"]


# ===== 使えるモデル（model_catalog） =====

def test_admin_settings_model_catalog_put_reflects_merges_and_resets(admin):
    """1 セルだけ差し替えても他セルは組み込み既定のまま（部分セル・全体マージ）・null で未設定へ戻る。
    `builtin` は管理者設定を一切重ねない。"""
    from sherpa import model_catalog
    builtin_before = admin.get(_ADMIN).json()["model_catalog"]["builtin"]
    assert builtin_before == model_catalog.get_catalog({})

    cell = {"allowed": ["custom-a", "custom-b"], "default": "custom-a"}
    r = _put(admin, {"model_catalog": {"openai": {"chat": cell}}})
    assert r.status_code == 200, r.text
    mc = r.json()["model_catalog"]
    assert mc["configured"] == {"openai": {"chat": cell}}
    assert mc["effective"]["openai"]["chat"] == cell
    assert mc["effective"]["ollama"]["chat"]["allowed"] == ["qwen2.5"]
    assert mc["builtin"] == builtin_before
    assert mc["builtin"]["openai"]["chat"]["default"] == "gpt-5.5"
    assert mc["builtin"] != mc["effective"]

    r2 = _put(admin, {"model_catalog": None})
    assert r2.status_code == 200, r2.text
    mc2 = r2.json()["model_catalog"]
    assert mc2["configured"] is None
    assert mc2["effective"]["openai"]["chat"]["default"] == "gpt-5.5"


def test_admin_settings_model_catalog_default_auto_added_to_allowed(admin):
    r = _put(admin, {"model_catalog": {"openai": {"chat": {"allowed": ["a"], "default": "b"}}}})
    assert r.status_code == 200, r.text
    cell = r.json()["model_catalog"]["configured"]["openai"]["chat"]
    assert cell["default"] == "b" and "b" in cell["allowed"] and "a" in cell["allowed"]


def test_admin_settings_view_catalog_and_allowlist_share_one_snapshot(monkeypatch):
    """`_admin_settings_view()` が自身の読んだ system_settings と同一オブジェクトを `get_catalog`／
    `_allowlisted_hosts` へ渡す（`configured` と `effective` が別時点にならない）。HTTP 経由だと他ヘルパーが
    独自に読み直して混線するため直接呼ぶ。"""
    from sherpa import llm, model_catalog
    from sherpa.routers import system_extras as sysx

    sentinel = store.get_system_settings()
    monkeypatch.setattr(store, "get_system_settings", lambda: sentinel)
    seen: dict[str, list] = {}

    def _spy(name, real):
        def _wrapped(*args, **kwargs):
            seen.setdefault(name, []).append(kwargs.get("system_settings", args[0] if args else None))
            return real(*args, **kwargs)
        return _wrapped

    monkeypatch.setattr(model_catalog, "get_catalog", _spy("get_catalog", model_catalog.get_catalog))
    monkeypatch.setattr(llm, "_allowlisted_hosts", _spy("_allowlisted_hosts", llm._allowlisted_hosts))
    sysx._admin_settings_view()

    assert {"get_catalog", "_allowlisted_hosts"} <= set(seen)
    assert any(arg is sentinel for arg in seen["get_catalog"])
    # builtin 用は None（読み直し）でも sentinel でもなく、明示的な空 {}
    assert any(arg == {} and arg is not sentinel for arg in seen["get_catalog"])
    for arg in seen["_allowlisted_hosts"]:
        assert arg is sentinel


# ===== Ollama 中央 URL と allowlist =====

def test_admin_settings_ollama_url_and_allowlist_set_together_then_removable(admin):
    """新ホストの allowlist 追加と中央既定への設定を同一 PUT で行える（置換後の allowlist で検証）。
    allowlist から中央ホストを外す操作は禁止しない（中央既定は残る）・allowlist だけの変更も動く。"""
    r = _put(admin, {"ollama_allowlist": ["10.9.9.9:11434"], "ollama_url": "http://10.9.9.9:11434"})
    assert r.status_code == 200, r.text
    assert r.json()["ollama_allowlist"]["configured"] == ["10.9.9.9:11434"]
    assert r.json()["cloud"]["ollama_url"] == "http://10.9.9.9:11434"

    r2 = _put(admin, {"ollama_allowlist": []})
    assert r2.status_code == 200, r2.text
    assert r2.json()["ollama_allowlist"]["configured"] is None
    assert r2.json()["cloud"]["ollama_url"] == "http://10.9.9.9:11434"

    r3 = _put(admin, {"ollama_allowlist": ["10.9.9.8:11434"]})
    assert r3.status_code == 200, r3.text
    assert r3.json()["ollama_allowlist"]["configured"] == ["10.9.9.8:11434"]


def test_admin_settings_ollama_url_change_must_be_authorized_by_pending_allowlist_not_old(admin):
    """新 URL は置換後の allowlist で検証する（旧一覧にだけ残るホストへ旧権限で変更できない）。"""
    assert _put(admin, {"ollama_allowlist": ["10.9.9.9:11434"]}).status_code == 200
    r = _put(admin, {"ollama_allowlist": ["10.9.9.8:11434"], "ollama_url": "http://10.9.9.9:11434"})
    assert r.status_code == 422, r.text


def test_admin_settings_ollama_url_unchanged_allowed_even_if_allowlist_narrowed_same_put(admin):
    """中央 URL が実際には変わらない再送は、同時に allowlist を狭めても拒否しない。"""
    assert _put(admin, {"ollama_allowlist": ["10.9.9.9:11434"],
                        "ollama_url": "http://10.9.9.9:11434"}).status_code == 200
    r2 = _put(admin, {"ollama_allowlist": [], "ollama_url": "http://10.9.9.9:11434"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["ollama_allowlist"]["configured"] is None
    assert r2.json()["cloud"]["ollama_url"] == "http://10.9.9.9:11434"


# ===== openai_endpoint_kind/openai_base_url のクロス検証（書込み直前の原子性） =====

_AZURE_BASE = "https://res.openai.azure.com"


def test_admin_settings_openai_endpoint_kind_base_cross_validation(admin):
    assert _put(admin, {"openai_endpoint_kind": "azure"}).status_code == 422   # base 無し

    r = _put(admin, {"openai_endpoint_kind": "azure", "openai_base_url": _AZURE_BASE})
    assert r.status_code == 200, r.text
    assert r.json()["openai_endpoint"]["configured"]["kind"] == "azure"

    # 保存済み base があれば kind 単独 PUT も通り、base は維持される
    r2 = _put(admin, {"openai_endpoint_kind": "azure"})
    assert r2.status_code == 200, r2.text
    view = r2.json()["openai_endpoint"]["configured"]
    assert view["kind"] == "azure" and view["base_url"] == _AZURE_BASE

    # openai へ切り替えても正常な保存済み base は触らない（往復で保持される）
    from sherpa import llm
    assert _put(admin, {"openai_endpoint_kind": "openai"}).status_code == 200
    sysset = store.get_system_settings()
    assert sysset.get("openai_base_url") == _AZURE_BASE
    assert llm.openai_endpoint_kind(sysset) == "openai"


def test_admin_settings_openai_endpoint_base_url_alone_uses_fresh_kind_not_stale_cache(admin):
    """クロス検証は advisory lock 後に同一コネクションで読み直した実効値で行う（3 秒 TTL キャッシュではない）。
    キャッシュを kind=openai へ巻き戻しても、azure のまま base だけを外す PUT は 422。"""
    assert _put(admin, {"openai_endpoint_kind": "azure", "openai_base_url": _AZURE_BASE}).status_code == 200
    # facade の re-export は別バインディングのため実体モジュールを直接書き換える
    from sherpa.store import settings as _store_settings
    _store_settings._system_settings_cache = {"openai_endpoint_kind": "openai"}
    _store_settings._system_settings_cache_ts = time.monotonic()
    try:
        r2 = _put(admin, {"openai_base_url": None})
        assert r2.status_code == 422, r2.text
        store._invalidate_system_settings_cache()
        view = admin.get(_ADMIN).json()["openai_endpoint"]["configured"]
        assert view["kind"] == "azure" and view["base_url"] == _AZURE_BASE
    finally:
        store._invalidate_system_settings_cache()


@pytest.mark.parametrize("corrupted", [0, {}])
def test_admin_settings_openai_endpoint_kind_openai_recovers_corrupted_base_url_in_one_put(
        admin, admin_uid, corrupted):
    """base_url が非文字列に破損した状態から、kind="openai" の単発 PUT だけで復旧できる（管理画面は
    「本家」選択時に base_url を送らない）。監査 before には破損の事実（`(不正な保存値)`）が残る。"""
    from sherpa import llm
    _insert_raw_setting("openai_endpoint_kind", "azure")
    _insert_raw_setting("openai_base_url", corrupted)
    store._invalidate_system_settings_cache()

    assert _put(admin, {"openai_endpoint_kind": "openai"}).status_code == 200

    with store._connect() as c:
        row = c.execute("SELECT value FROM system_settings WHERE key=%s", ("openai_base_url",)).fetchone()
    assert row is None, f"openai_base_url が復旧（未設定へ削除）されていない: {row}"
    store._invalidate_system_settings_cache()
    sysset = store.get_system_settings()
    assert llm.openai_endpoint_kind(sysset) == "openai"
    assert llm.openai_base_url(sysset) == "https://api.openai.com/v1"

    rows = store.list_audit(action="system_settings.updated", actor=admin_uid, limit=1)
    assert rows, "system_settings.updated が監査に残っていない"
    assert rows[0]["before_state"].get("openai_base_url") == "(不正な保存値)"
    assert rows[0]["after_state"].get("openai_base_url") == "<cleared>"


def test_set_system_settings_raises_conflict_exception_directly():
    """store 層は不整合な kind/base を `OpenAIEndpointSettingsConflict`（ValueError 派生）で拒否する。"""
    with pytest.raises(store.OpenAIEndpointSettingsConflict):
        store.set_system_settings("admin-uid", {"openai_endpoint_kind": "custom"})


# ===== 監査・fail-closed（設定変更と監査を同一トランザクションで実行） =====

@pytest.mark.parametrize("body, key, value", [
    ({"legacy_backend": "libreoffice"}, "legacy_backend", "libreoffice"),
    ({"research_default_provider": "ollama"}, "research_default_provider", "ollama"),
    ({"depth_base_grep_max_hits": 30}, "depth_base_grep_max_hits", 30),
    ({"chat_max_turns_global": 30}, "chat_max_turns_global", 30),
    ({"agentic_budget_per_result": 300_000}, "agentic_budget_per_result", 300_000),
])
def test_put_writes_audit_warning(monkeypatch, admin, admin_uid, body, key, value):
    _soffice_present(monkeypatch)
    r = _put(admin, body)
    assert r.status_code == 200, r.text
    rows = store.list_audit(action="system_settings.updated", actor=admin_uid, limit=10)
    assert rows, "system_settings.updated が監査に残っていない"
    assert rows[0]["severity"] == "warning"
    assert rows[0]["resource_type"] == "system_settings"
    assert rows[0]["after_state"].get(key) == value


def _boom(*_a, **_kw):
    raise RuntimeError("simulated audit failure")


def test_put_fail_closed_on_audit_failure(monkeypatch, admin):
    """監査 INSERT の失敗は設定変更ごとロールバックして 500（既存キーは before のまま・新規キーの行も残らない）。
    `store._audit_insert`（同一トランザクションで呼ばれる内部ヘルパー）を壊して検証する。"""
    assert _put(admin, {"legacy_backend": "libreoffice"}).status_code == 200

    monkeypatch.setattr(store, "_audit_insert", _boom)
    assert _put(admin, {"legacy_backend": "none"}).status_code == 500
    assert _put(admin, {"arms_enabled": ["ooxml"]}).status_code == 500
    monkeypatch.undo()

    # キャッシュを捨てて DB の行を直接確認する（キャッシュの偶然の一致を避ける）
    store._invalidate_system_settings_cache()
    stored = store.get_system_settings()
    assert stored.get("legacy_backend") == "libreoffice"
    assert stored.get("arms_enabled") is None
    with store._connect() as c:
        row = c.execute("SELECT value FROM system_settings WHERE key=%s", ("legacy_backend",)).fetchone()
        new_row = c.execute("SELECT value FROM system_settings WHERE key=%s", ("arms_enabled",)).fetchone()
    assert row["value"] == "libreoffice"
    assert new_row is None, "監査失敗時に新規キーの行がテーブルへ残っている"


def test_compat_mode_admin(auth_disabled):
    """互換モード（SHERPA_AUTH_DISABLED=1・合成 admin）でも GET/PUT が動く。"""
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get(_ADMIN).status_code == 200
    r = _put(c, {"legacy_backend": "libreoffice"})
    assert r.status_code == 200, r.text
    assert r.json()["legacy_backend"]["configured"] == "libreoffice"


# ===== store.seed_system_settings_once（実 DB での意味論） =====

def test_seed_system_settings_once_inserts_when_absent_and_never_overwrites():
    """未設定は INSERT され、既存値は上書きせず conflicts に現在値を返す。マーカー確認後に管理者が先に
    入れた値も上書きしない（マーカー自体は新規なので書ける）。"""
    secret = frozenset({"openai_api_key"})
    applied1, conflicts1 = store.seed_system_settings_once(
        {"openai_api_key": "sk-first-seed"}, guard_key="env_seed_version", secret_keys=secret)
    assert applied1 == {"openai_api_key": "sk-first-seed"} and conflicts1 == {}
    assert store.get_system_settings()["openai_api_key"] == "sk-first-seed"

    applied2, conflicts2 = store.seed_system_settings_once(
        {"openai_api_key": "sk-would-clobber"}, guard_key="env_seed_version", secret_keys=secret)
    assert applied2 == {} and conflicts2 == {"openai_api_key": "sk-first-seed"}
    assert store.get_system_settings()["openai_api_key"] == "sk-first-seed"

    _clear_system_settings()
    store.set_system_settings("admin-uid", {"openai_api_key": "sk-admin-entered"}, secret_keys=secret)
    applied, conflicts = store.seed_system_settings_once(
        {"openai_api_key": "sk-env-value", "env_seed_version": 1}, guard_key="env_seed_version",
        secret_keys=secret)
    assert applied == {"env_seed_version": 1}
    assert conflicts["openai_api_key"] == "sk-admin-entered"
    assert store.get_system_settings()["openai_api_key"] == "sk-admin-entered"


def test_seed_system_settings_once_does_not_reinsert_key_deleted_after_marker_set():
    """マーカーが既にあり、キー自体は無い（管理者が削除した）状態では、キー単体の挿入も起きない。"""
    store.set_system_settings("admin-uid", {"env_seed_version": 1})
    applied, conflicts = store.seed_system_settings_once(
        {"openai_api_key": "sk-old-env-value"}, guard_key="env_seed_version",
        secret_keys=frozenset({"openai_api_key"}))
    assert applied == {}
    assert conflicts == {"openai_api_key": None}
    assert store.get_system_settings().get("openai_api_key") is None


def test_seed_system_settings_once_ollama_allowlist_merge_only_when_url_newly_inserted():
    """URL を新規挿入できたときだけ既存 allowlist へ host:port を追記する。URL 行が既にあれば allowlist に触れない。"""
    merge = ("ollama_url", "central.internal:11434")
    store.set_system_settings("admin-uid", {"ollama_allowlist": ["10.1.1.1:11434"]})
    applied, _ = store.seed_system_settings_once(
        {"ollama_url": "http://central.internal:11434", "env_seed_version": 1},
        guard_key="env_seed_version", ollama_allowlist_merge=merge)
    assert applied["ollama_url"] == "http://central.internal:11434"
    assert set(applied["ollama_allowlist"]) == {"10.1.1.1:11434", "central.internal:11434"}
    assert set(store.get_system_settings()["ollama_allowlist"]) == {"10.1.1.1:11434", "central.internal:11434"}

    _clear_system_settings()
    store.set_system_settings("admin-uid", {"ollama_url": "http://already-set.internal:11434"})
    applied, conflicts = store.seed_system_settings_once(
        {"ollama_url": "http://central.internal:11434", "env_seed_version": 1},
        guard_key="env_seed_version", ollama_allowlist_merge=merge)
    assert "ollama_url" not in applied and "ollama_allowlist" not in applied
    assert conflicts["ollama_url"] == "http://already-set.internal:11434"
    assert store.get_system_settings().get("ollama_allowlist") is None


def test_seed_system_settings_once_rejects_ollama_allowlist_in_updates_with_merge():
    with pytest.raises(ValueError):
        store.seed_system_settings_once(
            {"ollama_url": "http://x:11434", "ollama_allowlist": ["x:11434"]},
            guard_key="env_seed_version", ollama_allowlist_merge=("ollama_url", "x:11434"))


# ===== A6: personal_api_keys_allowed=false → 個人キーの一括削除 =====

def _user_with_key(prefix, key, **extra):
    uid = f"{prefix}{_sfx()}"
    _mk_user(uid, f"pw-{uid}")
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": True})
    store.update_settings(uid, openai_api_key=key, **extra)
    return uid


def test_put_personal_keys_off_purges_all_users_and_audits_count(admin):
    """false 保存で全ユーザーの個人秘密キー（openai と閉じたプロバイダの旧キー列）が NULL になり、監査に削除件数が残る。"""
    u1 = _user_with_key("pkoff1", "sk-u1")
    u2 = f"pkoff2{_sfx()}"
    _mk_user(u2, f"pw-{u2}")
    store.update_settings(u2, agent="simple")
    with store._connect() as c:   # 閉じたプロバイダの旧キー列（アプリは書かない）を直接仕込む
        c.execute("UPDATE user_settings SET bedrock_api_key=%s WHERE user_id=%s", ("bk-u2", u2))
    assert store.get_settings(u1)["openai_api_key"] == "sk-u1"

    assert _put(admin, {"personal_api_keys_allowed": False}).status_code == 200

    assert store.get_settings(u1)["openai_api_key"] is None
    with store._connect() as c:
        legacy = c.execute("SELECT bedrock_api_key FROM user_settings WHERE user_id=%s", (u2,)).fetchone()
    assert legacy["bedrock_api_key"] is None
    rows = store.list_audit(action="user_settings.personal_keys_purged", limit=5)
    assert rows, "監査行が記録されていない"
    assert rows[0]["detail"]["count"] >= 2


def test_put_personal_keys_stays_true_does_not_purge_and_count_is_exposed(admin):
    u1 = _user_with_key("pkon1", "sk-keep")
    assert admin.get(_ADMIN).json()["cloud"]["personal_keys_in_use_count"] >= 1   # 保存前の確認ダイアログ用
    assert _put(admin, {"personal_api_keys_allowed": True}).status_code == 200
    assert store.get_settings(u1)["openai_api_key"] == "sk-keep"


def test_put_personal_keys_off_is_idempotent_no_audit_on_repeat_with_nothing_to_purge(admin):
    assert _put(admin, {"personal_api_keys_allowed": False}).status_code == 200
    after1 = len(store.list_audit(action="user_settings.personal_keys_purged", limit=1000))
    assert _put(admin, {"personal_api_keys_allowed": False}).status_code == 200
    assert len(store.list_audit(action="user_settings.personal_keys_purged", limit=1000)) == after1


def test_purge_personal_api_keys_idempotent():
    _user_with_key("pkidem1", "sk-idem")
    assert store.purge_personal_api_keys(actor="test") >= 1
    assert store.purge_personal_api_keys(actor="test") == 0


def test_startup_purge_deletes_when_flag_false_and_skips_when_true():
    """起動時の後方互換パス: 実際に false のときだけ個人キーを削除し、true なら残す。"""
    from sherpa import api
    u1 = _user_with_key("pkstart1", "sk-start-false")
    store.set_system_settings("test", {"personal_api_keys_allowed": False})
    api._purge_personal_keys_if_disabled_on_startup()
    assert store.get_settings(u1)["openai_api_key"] is None

    u2 = _user_with_key("pkstart2", "sk-start-true")
    api._purge_personal_keys_if_disabled_on_startup()
    assert store.get_settings(u2)["openai_api_key"] == "sk-start-true"


def test_update_settings_personal_key_write_serializes_with_purge_via_shared_lock():
    """個人キーの書込みと A6 無効化に伴う purge は同じ advisory lock を共有する。purge 側がロック保持中は
    書込みが実際にロック待ちに入り（`pg_blocking_pids` で観測）、解放後に A6 を読み直して
    `PersonalKeysDisallowedError` で拒否される（古い true を掴んだ書込みの競合の再現）。"""
    u1 = f"pklockrace{_sfx()}"
    _mk_user(u1, f"pw-{u1}")
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": True})

    holder_conn = store._connect()
    monitor_conn = store._connect()
    try:
        holder_pid = holder_conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        holder_conn.execute("SELECT pg_advisory_xact_lock(%s)", (store.settings._PERSONAL_KEY_LOCK,))
        store.set_system_settings("admin-uid", {"personal_api_keys_allowed": False})

        thread_done = threading.Event()
        errors: list[Exception] = []
        raised: list[Exception] = []

        def worker():
            try:
                store.update_settings(u1, openai_api_key="sk-stale-race")
            except store.PersonalKeysDisallowedError as e:
                raised.append(e)
            except Exception as e:   # pragma: no cover - 診断用
                errors.append(e)
            finally:
                thread_done.set()

        t = threading.Thread(target=worker)
        t.start()

        deadline = time.monotonic() + 5.0
        worker_pid = None
        while time.monotonic() < deadline:
            rows = monitor_conn.execute(
                "SELECT pid FROM pg_stat_activity "
                "WHERE wait_event_type='Lock' AND datname = current_database() "
                "  AND %s = ANY(pg_blocking_pids(pid))",
                (holder_pid,)).fetchall()
            monitor_conn.rollback()
            if len(rows) == 1:
                worker_pid = rows[0]["pid"]
                break
            if len(rows) > 1:
                raise AssertionError(f"holder をブロック中のバックエンドが複数: {[r['pid'] for r in rows]}")
            time.sleep(0.05)

        assert worker_pid is not None, "worker が advisory lock 待ちへ入ったことを観測できなかった"
        assert not thread_done.is_set()

        holder_conn.commit()   # ロック解放＋A6=false 確定＝worker が進める

        assert thread_done.wait(timeout=5), "commit 後もスレッドが完了しなかった"
        t.join(timeout=5)
        assert not t.is_alive()
        assert not errors, f"スレッドで例外: {errors}"
        assert len(raised) == 1, f"PersonalKeysDisallowedError が発生しなかった: raised={raised}"
        assert store.get_settings(u1)["openai_api_key"] is None
    finally:
        holder_conn.close()
        monitor_conn.close()


def test_put_settings_personal_key_race_returns_422_with_detail_via_real_http(monkeypatch):
    """`settings_put` の事前チェックが古い A6=true を掴み、書込み直前の実 DB 再確認では false に
    なっていた競合を実 HTTP 経路で再現し、store 例外が 422＋利用者向け文言へ変換されることを固定する。"""
    u1, pw1 = f"pkhttprace{_sfx()}", f"pw-{_sfx()}"
    _mk_user(u1, pw1)
    c1 = _login(u1, pw1)
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": False})
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"personal_api_keys_allowed": True})

    r = c1.put("/settings", json={"openai_api_key": "sk-http-race"})

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "個人 API キーは無効化されています（管理者が中央設定でキーを管理します）"
    assert store.get_settings(u1)["openai_api_key"] is None


def test_keyless_update_settings_does_not_resurrect_key_after_real_purge(monkeypatch):
    """キー無しの `update_settings`（例: agent だけ）は個人キー列を SET から除外する。キーを読んだ後・
    書込み前に本物の purge が割り込んでも、消えたキーを書き戻さない（その interleave を強制して固定）。"""
    from sherpa.store import settings as settings_mod

    u1 = _user_with_key("pkkeyless", "sk-will-be-purged")
    assert store.get_settings(u1)["openai_api_key"] == "sk-will-be-purged"

    real_get_settings = settings_mod.get_settings
    reader_entered = threading.Event()
    proceed = threading.Event()

    def _blocking_get_settings(user_id="admin"):
        result = real_get_settings(user_id)
        if user_id == u1:
            reader_entered.set()
            assert proceed.wait(timeout=5), "テスト側が purge を先に進めなかった"
        return result

    monkeypatch.setattr(settings_mod, "get_settings", _blocking_get_settings)
    errors: list[Exception] = []

    def worker():
        try:
            store.update_settings(u1, agent="codex")
        except Exception as e:   # pragma: no cover - 診断用
            errors.append(e)

    t = threading.Thread(target=worker)
    t.start()
    assert reader_entered.wait(timeout=5), "update_settings が cur の読取へ入らなかった"
    assert store.purge_personal_api_keys(actor="test") >= 1
    proceed.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert not errors, f"worker で例外: {errors}"

    fetched = store.get_settings(u1)
    assert fetched["openai_api_key"] is None, "キー無し保存が purge 後に古いキーを書き戻した"
    assert fetched["agent"] == "codex"


# ===== 起動時シード（実 DB） =====

def test_seed_catalog_once_inserts_when_absent_skips_on_second_call_and_reads_embed_env(monkeypatch):
    """未設定なら `model_catalog`＋完了マーカーが 1 回だけ書かれ、管理者が編集済みなら上書きしない。
    `OPENAI_EMBED_MODEL` は初回シード時にだけ openai/embed の既定へ取り込まれる。"""
    from sherpa import model_catalog
    assert store.get_system_settings().get(model_catalog._CATALOG_SEED_MARKER_KEY) is None
    model_catalog.seed_catalog_once()
    sysset = store.get_system_settings()
    assert sysset.get(model_catalog._CATALOG_SEED_MARKER_KEY) == model_catalog._CATALOG_SEED_VERSION
    assert sysset["model_catalog"]["openai"]["chat"]["default"] == "gpt-5.5"

    store.set_system_settings("admin-uid", {"model_catalog": {"openai": {"chat": {
        "allowed": ["admin-edited"], "default": "admin-edited"}}}})
    model_catalog.seed_catalog_once()
    assert store.get_system_settings()["model_catalog"]["openai"]["chat"]["default"] == "admin-edited"

    _clear_system_settings()
    monkeypatch.setenv("OPENAI_EMBED_MODEL", "my-embed-deployment")
    model_catalog.seed_catalog_once()
    cell = store.get_system_settings()["model_catalog"]["openai"]["embed"]
    assert cell["default"] == "my-embed-deployment"
    assert "my-embed-deployment" in cell["allowed"]


def test_settings_put_shares_one_system_settings_snapshot(monkeypatch):
    """`settings_put` は入口で取得した system_settings を A6 判定と ollama_url の allowlist 検証の両方へ
    同一オブジェクトで渡す。呼ばれるたびに別オブジェクトを返すよう仕込み、各ヘルパーの**最初の**呼び出し
    （保存前の検証フェーズ。None＝未伝播の回帰も記録する）だけを比較する。"""
    from sherpa import keys, llm

    c, _ = _new_client("user", "snapput")
    real_get_system_settings = store.get_system_settings
    call_id = {"n": 0}

    def _tagged_each_call():
        call_id["n"] += 1
        d = dict(real_get_system_settings())
        d["_call_id"] = call_id["n"]
        d["cloud_provider"] = "openai"   # cloud_provider を openai に揃える
        return d

    monkeypatch.setattr(store, "get_system_settings", _tagged_each_call)
    first_call_value: dict[str, dict | None] = {}

    def _spy(name, real):
        def _wrapped(*args, **kwargs):
            if name not in first_call_value:
                first_call_value[name] = kwargs.get("system_settings", args[0] if args else None)
            return real(*args, **kwargs)
        return _wrapped

    monkeypatch.setattr(keys, "personal_keys_allowed", _spy("personal_keys_allowed", keys.personal_keys_allowed))
    monkeypatch.setattr(llm, "_allowlisted_hosts", _spy("_allowlisted_hosts", llm._allowlisted_hosts))

    r = c.put("/settings", json={"agent": "simple", "ollama_url": "http://localhost:11434"})
    assert r.status_code == 200, r.text

    missing = {"personal_keys_allowed", "_allowlisted_hosts"} - set(first_call_value)
    assert not missing, f"呼ばれなかったヘルパー: {missing}"
    for name, v in first_call_value.items():
        assert v is not None, f"{name} の最初の呼び出しが system_settings=None だった（未伝播の回帰）"
    assert len({v["_call_id"] for v in first_call_value.values()}) == 1, first_call_value


# ===== research_default_provider（保存時 preflight） =====

def test_admin_settings_research_default_provider_accepts_openai_when_key_present_in_same_put(admin):
    """同一 PUT の openai_api_key を重ねた実効設定で preflight が通る。"""
    r = _put(admin, {"openai_api_key": "sk-test-real-key-1234567890", "research_default_provider": "openai"})
    assert r.status_code == 200, r.text
    rdp = r.json()["ext_keys"]["research_default_provider"]
    assert rdp["configured"] == "openai" and rdp["effective"] == "openai"


_AZURE_CHAT_ONLY_SETUP = {
    "openai_api_key": "sk-test-real-key-1234567890", "openai_endpoint_kind": "azure",
    "openai_base_url": "https://example.openai.azure.com/openai/v1",
    "model_catalog": {"openai": {"chat": {"allowed": ["my-chat-deployment"], "default": "my-chat-deployment"}}}}


@pytest.mark.parametrize("setup", [
    None,                                       # キー未設定
    {"openai_api_key": "sk-REPLACE_ME"},        # プレースホルダは「キーあり」と誤認しない
    _AZURE_CHAT_ONLY_SETUP,                     # 実際に送る用途（subsearch）のデプロイ名が未設定
], ids=["no-key", "placeholder-key", "chat-only-deployment"])
def test_admin_settings_research_default_provider_openai_rejected_when_not_sendable(admin, setup):
    """保存時点で送信不可能な組み合わせは 422 で、保存されない。"""
    if setup:
        assert _put(admin, setup).status_code == 200, setup
    r = _put(admin, {"research_default_provider": "openai"})
    assert r.status_code == 422, r.text
    assert admin.get(_ADMIN).json()["ext_keys"]["research_default_provider"]["configured"] is None


# ===== チャットの質問例 =====

def test_put_chat_examples_reflects_and_resets(admin):
    """items は trim・空要素除外込みで反映され、非 admin 向け `GET /settings` にも同じ内容が出る。
    null で未設定（組み込み既定）へ戻ると非 admin 向けは None。"""
    from sherpa import chat_examples as ce
    r = _put(admin, {"chat_examples": {"enabled": True, "items": ["  在庫の締め処理は？  ", "", "月次バッチの流れは？"]}})
    assert r.status_code == 200, r.text
    view = r.json()["chat_examples"]
    assert view["configured"] == {"enabled": True, "items": ["在庫の締め処理は？", "月次バッチの流れは？"]}
    assert view["effective"] == ["在庫の締め処理は？", "月次バッチの流れは？"]
    assert admin.get("/settings").json()["chat_examples"] == ["在庫の締め処理は？", "月次バッチの流れは？"]

    r2 = _put(admin, {"chat_examples": None})
    assert r2.status_code == 200, r2.text
    assert r2.json()["chat_examples"]["configured"] is None
    assert r2.json()["chat_examples"]["effective"] == list(ce.DEFAULT_ITEMS)
    assert admin.get("/settings").json()["chat_examples"] is None


@pytest.mark.parametrize("body, expected_configured", [
    ({"enabled": False, "items": ["これは表示されない"]}, None),   # enabled=false は items があっても非表示
    ({"enabled": True, "items": ["   ", ""]}, {"enabled": True, "items": []}),   # 実質空＝明示的な非表示
])
def test_put_chat_examples_explicit_hide_is_empty_list_not_unset(admin, body, expected_configured):
    """明示的な非表示は effective=[]・非 admin 向けも空配列（None＝未設定とは区別する）。"""
    r = _put(admin, {"chat_examples": body})
    assert r.status_code == 200, r.text
    assert r.json()["chat_examples"]["effective"] == []
    if expected_configured is not None:
        assert r.json()["chat_examples"]["configured"] == expected_configured
    assert admin.get("/settings").json()["chat_examples"] == []


def test_put_chat_examples_accepts_max_items_boundary(admin):
    items = [f"質問{i}" for i in range(8)]
    r = _put(admin, {"chat_examples": {"items": items}})
    assert r.status_code == 200, r.text
    assert r.json()["chat_examples"]["configured"]["items"] == items
    assert r.json()["chat_examples"]["configured"]["enabled"] is True   # enabled 省略時は既定 true

    r2 = _put(admin, {"chat_examples": {"items": ["x" * 200]}})
    assert r2.status_code == 200, r2.text
    assert r2.json()["chat_examples"]["configured"]["items"] == ["x" * 200]


def test_chat_max_turns_effective_limits_prefers_db_value(admin):
    """ターン受付が使う解決関数 `chat_turns.effective_limits()` が保存直後の DB 値を返す。"""
    from sherpa import chat_turns
    assert _put(admin, {"chat_max_turns_per_user": 5, "chat_max_turns_global": 30}).status_code == 200
    assert chat_turns.effective_limits() == (5, 30)


# ===== env→設定の初回シード（実 DB） =====

def test_seed_user_agent_from_env_fills_only_unselected_users(monkeypatch):
    """`SHERPA_AGENT=simple` は、頭脳が未選択の利用者（設定行なしを含む）だけへ一度だけ保存する。選択済みは変えない。"""
    from sherpa import api as api_mod
    sfx = _sfx()
    no_row, empty_row, chosen = f"agnone{sfx}", f"agempty{sfx}", f"agcodex{sfx}"
    for uid in (no_row, empty_row, chosen):
        _mk_user(uid, f"Pw{uid}")
    store.update_settings(empty_row, ollama_url="")
    store.update_settings(chosen, agent="codex")
    with store._connect() as c:
        before = {r["user_id"]: r["agent"] for r in c.execute("SELECT user_id, agent FROM user_settings").fetchall()}
    try:
        monkeypatch.setenv("SHERPA_AGENT", "simple")
        api_mod._seed_user_agent_from_env()
        assert store.get_settings(no_row)["agent"] == "simple"
        assert store.get_settings(empty_row)["agent"] == "simple"
        assert store.get_settings(chosen)["agent"] == "codex"
        assert store.get_system_settings().get(api_mod._USER_AGENT_SEED_MARKER_KEY) == 1
        monkeypatch.setenv("SHERPA_AGENT", "codex")  # 印があれば再実行しても変えない
        api_mod._seed_user_agent_from_env()
        assert store.get_settings(no_row)["agent"] == "simple"
    finally:
        with store._connect() as c:
            for r in c.execute("SELECT user_id FROM user_settings").fetchall():
                if r["user_id"] not in before:
                    c.execute("DELETE FROM user_settings WHERE user_id=%s", (r["user_id"],))
                elif not before[r["user_id"]]:
                    c.execute("UPDATE user_settings SET agent='' WHERE user_id=%s", (r["user_id"],))


def test_seed_vlm_ollama_url_from_env_only_when_central_unset(monkeypatch):
    """旧 `SHERPA_VLM_OLLAMA_URL` は中央の `ollama_url` が未保存のときだけ中央の値（と許可一覧）へ取り込む。"""
    from sherpa import api as api_mod
    monkeypatch.setenv("SHERPA_VLM_OLLAMA_URL", "http://vlm-host.internal:11434")
    api_mod._seed_vlm_ollama_url_from_env()
    got = store.get_system_settings()
    assert got["ollama_url"] == "http://vlm-host.internal:11434"
    assert got["ollama_allowlist"] == ["vlm-host.internal:11434"]

    _clear_system_settings()
    store.set_system_settings("admin-uid", {"ollama_url": "http://central.internal:11434"})
    api_mod._seed_vlm_ollama_url_from_env()
    assert store.get_system_settings()["ollama_url"] == "http://central.internal:11434"
