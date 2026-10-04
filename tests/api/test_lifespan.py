"""起動処理（lifespan）の契約テスト。

- production の fail-closed 検査（fixtures・既定 admin パスワード・CHANGE_ME・Codex sandbox・複数 worker）と
  dev での警告のみ続行
- env → system_settings の初回シード（完了マーカー方式・一度だけ・管理画面が唯一の真実源）
- `TestClient` 起動で起動処理が決まった順序で走ること

DB/Neo4j/ES を要さないよう、外部サービスに触れる本体は monkeypatch でスタブ化する。
実 DB での `seed_system_settings_once` の意味論・個人キー削除の起動パスは test_system_settings.py が確かめる。
"""
from __future__ import annotations

import logging

import pytest

import sherpa.api as api
from fastapi.testclient import TestClient
from sherpa import ext_api, llm, model_catalog, store
from sherpa.ingest import background


@pytest.fixture(autouse=True)
def _restore_background_accepting():
    """`with TestClient(api.app):` は実 lifespan の shutdown（`background.stop_accepting()`）を経由する。
    プロセス寿命のグローバルのため戻さないと、後続の全テストの背景実行が 503 で壊れる。"""
    yield
    background.start_accepting()


# ===== production の fail-closed 検査・dev は警告のみ =====

_PROD = {"SHERPA_ENV": "production"}
_DEV = {"SHERPA_ENV": "dev"}
_RAISES, _OK, _WARNS, _SILENT = "raises", "ok", "warns", "silent"

# (検査関数, env（None は削除）, 結果, raises なら match／warns なら警告文に含まれる語)
_STARTUP_CHECKS = [
    # fixtures 到達可
    ("_warn_fixtures", {**_PROD, "SHERPA_USE_FIXTURES": "1"}, _RAISES, None),
    ("_warn_fixtures", {**_DEV, "SHERPA_USE_FIXTURES": "1"}, _OK, None),
    # 初期 admin パスワード: 未設定・空・空白のみは「未設定」と同じ扱い（strip 後に空）
    ("_warn_default_admin_password", {**_PROD, "SHERPA_ADMIN_PASSWORD": None}, _RAISES, None),
    ("_warn_default_admin_password", {**_PROD, "SHERPA_ADMIN_PASSWORD": ""}, _RAISES, None),
    ("_warn_default_admin_password", {**_PROD, "SHERPA_ADMIN_PASSWORD": "   "}, _RAISES, None),
    # 明示設定なら開発既定と同値でも許す（閉域前提＋初回ログインの変更強制）
    ("_warn_default_admin_password", {**_PROD, "SHERPA_ADMIN_PASSWORD": "Sherpa2026!"}, _OK, None),
    ("_warn_default_admin_password", {**_PROD, "SHERPA_ADMIN_PASSWORD": "correct-horse-battery-staple"}, _OK, None),
    ("_warn_default_admin_password", {**_DEV, "SHERPA_ADMIN_PASSWORD": None}, _OK, None),
    # CHANGE_ME プレースホルダ（どのキーかはメッセージに出る）
    ("_warn_change_me_placeholders", {**_PROD, "SHERPA_AUDIT_IP_SALT": "CHANGE_ME_LONG_RANDOM_SALT"}, _RAISES, None),
    ("_warn_change_me_placeholders", {**_PROD, "POSTGRES_PASSWORD": "CHANGE_ME_POSTGRES_PASSWORD"},
     _RAISES, "POSTGRES_PASSWORD"),
    ("_warn_change_me_placeholders", {**_PROD, "SHERPA_AUDIT_IP_SALT": "a-real-random-salt-value"}, _OK, None),
    ("_warn_change_me_placeholders", {**_DEV, "SHERPA_AUDIT_IP_SALT": "CHANGE_ME_LONG_RANDOM_SALT"}, _OK, None),
    # Codex sandbox 無効化
    ("_warn_codex_sandbox_disabled", {**_PROD, "SHERPA_CODEX_SANDBOX": "0"}, _RAISES, None),
    ("_warn_codex_sandbox_disabled", {**_DEV, "SHERPA_CODEX_SANDBOX": "0"}, _WARNS, "sandbox"),
    ("_warn_codex_sandbox_disabled", {**_PROD, "SHERPA_CODEX_SANDBOX": None}, _SILENT, None),
    # 複数 worker（chat_turns の in-memory 状態が worker 間で共有されない）
    ("_warn_multi_worker_chat_turns", {**_PROD, "SHERPA_UVICORN_WORKERS": "4"}, _RAISES, None),
    ("_warn_multi_worker_chat_turns", {**_DEV, "SHERPA_UVICORN_WORKERS": "4"}, _WARNS, "worker"),
    ("_warn_multi_worker_chat_turns", {"SHERPA_UVICORN_WORKERS": None}, _SILENT, None),
    ("_warn_multi_worker_chat_turns", {"SHERPA_UVICORN_WORKERS": "1"}, _SILENT, None),
]


@pytest.mark.parametrize("fn, env, outcome, detail", _STARTUP_CHECKS)
def test_startup_check_fails_closed_in_production_and_warns_only_in_dev(
        monkeypatch, caplog, fn, env, outcome, detail):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    check = getattr(api, fn)
    if outcome == _RAISES:
        with pytest.raises(RuntimeError, match=detail):
            check()
        return
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert check() is None
    if outcome == _WARNS:
        assert any(detail in r.message.lower() for r in caplog.records)
    elif outcome == _SILENT:
        assert not caplog.records


# ===== フォルダ選択ルート（既定 /mnt）不在の警告 =====

@pytest.mark.parametrize("env_roots, is_dir, expected", [
    (None, lambda self: False, "フォルダ選択のルート"),        # 全ルート不在
    (None, lambda self: False, "SHERPA_BROWSE_ROOTS=/Users"),  # macOS 向けの直し方も案内する
    ("/srv/sherpa-data", lambda self: False, "/srv/sherpa-data"),   # env 指定のルートを検査する
    (None, "raise", "フォルダ選択のルート"),                    # is_dir が OSError でも例外を伝播しない
    (None, lambda self: True, None),                           # 既定ルートが存在すれば無警告
])
def test_warn_browse_roots_missing(monkeypatch, caplog, env_roots, is_dir, expected):
    if env_roots is None:
        monkeypatch.delenv("SHERPA_BROWSE_ROOTS", raising=False)
    else:
        monkeypatch.setenv("SHERPA_BROWSE_ROOTS", env_roots)

    def _raise(self):
        raise OSError("stale mount")

    monkeypatch.setattr("pathlib.Path.is_dir", _raise if is_dir == "raise" else is_dir)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert api._warn_browse_roots_missing() is None
    if expected is None:
        assert not caplog.records
    else:
        assert any(expected in r.message for r in caplog.records)


@pytest.mark.parametrize("env_roots, expected", [
    ("/missing:", [api.Path("/missing")]),   # 末尾コロンの空セグメントは cwd 扱いになるため除外
    (":", [api.Path("/mnt"), api.Path("/srv"), api.Path("/home"), api.Path("/Users")]),   # 全空は既定へ
])
def test_browse_roots_excludes_empty_segments(monkeypatch, env_roots, expected):
    monkeypatch.setenv("SHERPA_BROWSE_ROOTS", env_roots)
    assert api._browse_roots() == expected


# ===== env → system_settings 初回シード =====
# 完了マーカーがあれば env を一切読まない（管理者が削除した値が再起動で env から復活しない）。
# 上書き防止の不変条件は `store.seed_system_settings_once`（実 DB は test_system_settings.py）が担保し、
# ここでは dict ベースの疑似永続化でその意味論まで含めて api 側の呼び方を検証する。

class _FakeSystemSettingsDB:
    """`store.seed_system_settings_once` の意味論を dict で模す。guard_key の行が既にあれば個々のキーの
    有無に関わらず一切書かず、無いときだけキーごとに「既存なら書かない」を見る。"""

    def __init__(self, initial: dict | None = None):
        self.data: dict = dict(initial or {})
        self.seed_calls: list[dict] = []

    def get_system_settings(self) -> dict:
        return dict(self.data)

    def seed_system_settings_once(self, updates: dict, guard_key: str, secret_keys=None,
                                  *, ollama_allowlist_merge=None):
        self.seed_calls.append({"updates": dict(updates), "guard_key": guard_key, "secret_keys": secret_keys,
                               "ollama_allowlist_merge": ollama_allowlist_merge})
        if ollama_allowlist_merge is not None and "ollama_allowlist" in updates:
            raise ValueError("ollama_allowlist_merge 使用時は updates に ollama_allowlist を含められません")
        applied: dict = {}
        conflicts: dict = {}
        marker_present = guard_key in self.data
        for k, v in updates.items():
            if marker_present or k in self.data:
                conflicts[k] = self.data.get(k)
            else:
                self.data[k] = v
                applied[k] = v
        if ollama_allowlist_merge is not None:
            url_key, host_entry = ollama_allowlist_merge
            if url_key in applied and host_entry:
                current = list(self.data.get("ollama_allowlist") or [])
                if host_entry not in current:
                    merged = [*current, host_entry]
                    self.data["ollama_allowlist"] = merged
                    applied["ollama_allowlist"] = merged
        return applied, conflicts


def _use_fake_db(monkeypatch, initial=None) -> _FakeSystemSettingsDB:
    db = _FakeSystemSettingsDB(initial)
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    return db


def _seed_recorder(monkeypatch, current=None):
    """`seed_system_settings_once` の呼び出し引数を記録する（現在の system_settings は `current`）。"""
    calls = []

    def _fake(updates, guard_key=None, secret_keys=None, *, ollama_allowlist_merge=None):
        calls.append({"updates": dict(updates), "guard_key": guard_key, "secret_keys": secret_keys,
                      "ollama_allowlist_merge": ollama_allowlist_merge})
        return dict(updates), {}

    monkeypatch.setattr(store, "get_system_settings", lambda: dict(current or {}))
    monkeypatch.setattr(store, "seed_system_settings_once", _fake)
    return calls


_CRED_ENV_NAMES = ("OPENAI_API_KEY", "GEMINI_API_KEY", "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_AWS_API_KEY",
                   "OLLAMA_URL", "SHERPA_PERSONAL_API_KEYS", "SHERPA_ALLOW_WEB_SEARCH")
_CRED_V = {api._CREDENTIAL_SEED_MARKER_KEY: api._CREDENTIAL_SEED_VERSION}


@pytest.mark.parametrize("env, expected_updates", [
    # 閉じたプロバイダ・OLLAMA_URL（別シードへ分離済み＝不正形式でも他の確定に影響しない）は読まない
    ({"OPENAI_API_KEY": "sk-seed-openai", "GEMINI_API_KEY": "gemini-seed-key",
      "AWS_BEARER_TOKEN_BEDROCK": "bedrock-seed-key",
      "OLLAMA_URL": "http://admin:s3cr3t@ollama-central.internal:11434"},
     {"openai_api_key": "sk-seed-openai", **_CRED_V}),
    ({}, _CRED_V),                                                 # 何も無くてもマーカーだけ立てる
    ({"OPENAI_API_KEY": "sk-REPLACE_ME"}, _CRED_V),                # .env.example のプレースホルダはシードしない
    ({"SHERPA_PERSONAL_API_KEYS": "TRUE"}, {"personal_api_keys_allowed": True, **_CRED_V}),
    ({"SHERPA_ALLOW_WEB_SEARCH": "TRUE"}, {"web_search_allowed": True, **_CRED_V}),
    ({"SHERPA_ALLOW_WEB_SEARCH": "0"}, {"web_search_allowed": False, **_CRED_V}),   # 未設定のときだけ候補外
])
def test_seed_settings_from_env_writes_keys_and_marker_in_one_call(monkeypatch, env, expected_updates):
    """対象キーとマーカーを同一の `seed_system_settings_once` 呼び出し（＝同一トランザクション）で書く。"""
    for name in _CRED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    calls = _seed_recorder(monkeypatch)
    api._seed_settings_from_env()
    assert len(calls) == 1
    assert calls[0]["updates"] == expected_updates
    assert calls[0]["secret_keys"] == ({"openai_api_key"} if "openai_api_key" in expected_updates else set())


@pytest.mark.parametrize("fn, marker_key, version, env", [
    ("_seed_settings_from_env", api._CREDENTIAL_SEED_MARKER_KEY, api._CREDENTIAL_SEED_VERSION,
     {"OPENAI_API_KEY": "sk-would-be-seeded", "SHERPA_ALLOW_WEB_SEARCH": "1"}),
    ("_seed_depth_profile_from_env", api._DEPTH_PROFILE_SEED_MARKER_KEY, api._DEPTH_PROFILE_SEED_VERSION, {}),
    ("_seed_ollama_url_from_env", api._OLLAMA_URL_SEED_MARKER_KEY, 1,
     {"OLLAMA_URL": "http://ollama-central.internal:11434"}),
])
def test_seed_noop_when_marker_already_present(monkeypatch, fn, marker_key, version, env):
    """マーカーがあれば env を読まず `seed_system_settings_once` も呼ばない（管理者が削除した値は復活しない）。"""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    calls = _seed_recorder(monkeypatch, current={marker_key: version})
    getattr(api, fn)()
    assert calls == []


@pytest.mark.parametrize("fn", ["_seed_settings_from_env", "_seed_depth_profile_from_env"])
def test_seed_survives_db_unreachable_and_does_not_mark_seeded(monkeypatch, fn):
    """DB 不達でも起動は止めず、マーカーも立てない（次回起動・healthz で再試行できる）。"""
    def _boom():
        raise RuntimeError("db down")

    calls = _seed_recorder(monkeypatch)
    monkeypatch.setattr(store, "get_system_settings", _boom)
    getattr(api, fn)()
    assert calls == []


def test_seed_settings_from_env_aggregates_mismatch_warnings_into_one_line(monkeypatch, caplog):
    """DB に値があり env も設定されて複数キーが食い違うとき、無視される旨の警告は 1 行に集約する。
    マーカーは立ち、DB の値は上書きされない。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-value")
    monkeypatch.setenv("SHERPA_ALLOW_WEB_SEARCH", "1")
    db = _use_fake_db(monkeypatch, {"openai_api_key": "sk-db-value", "web_search_allowed": False})
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        api._seed_settings_from_env()
    assert db.data == {
        "openai_api_key": "sk-db-value", "web_search_allowed": False,
        "credential_seed_version": api._CREDENTIAL_SEED_VERSION,
    }
    warn_records = [r for r in caplog.records if "無視されます" in r.getMessage()]
    assert len(warn_records) == 1
    msg = warn_records[0].getMessage()
    assert "OPENAI_API_KEY" in msg and "SHERPA_ALLOW_WEB_SEARCH" in msg


# ---- OLLAMA_URL の独立シード（専用マーカー・不正な間はこのマーカーだけ確定しない） ----

_OLLAMA_MARKER = {api._OLLAMA_URL_SEED_MARKER_KEY: api._OLLAMA_URL_SEED_VERSION}


@pytest.mark.parametrize("env_url, expected_updates, expected_merge", [
    ("http://ollama-central.internal:11434",   # 非 loopback は allowlist へ原子的に追記
     {"ollama_url": "http://ollama-central.internal:11434", **_OLLAMA_MARKER},
     ("ollama_url", "ollama-central.internal:11434")),
    ("http://localhost:11434", None, None),   # loopback は allowlist 不要
    (None, _OLLAMA_MARKER, None),             # env 空はマーカーだけ確定
    ("http://ollama-central.internal",        # ポート省略は scheme の既定ポートへ正規化
     {"ollama_url": "http://ollama-central.internal:80", **_OLLAMA_MARKER},
     ("ollama_url", "ollama-central.internal:80")),
])
def test_seed_ollama_url_from_env_writes_url_marker_and_allowlist_atomically(
        monkeypatch, env_url, expected_updates, expected_merge):
    if env_url is None:
        monkeypatch.delenv("OLLAMA_URL", raising=False)
    else:
        monkeypatch.setenv("OLLAMA_URL", env_url)
    calls = _seed_recorder(monkeypatch)
    api._seed_ollama_url_from_env()
    assert len(calls) == 1
    assert calls[0]["guard_key"] == api._OLLAMA_URL_SEED_MARKER_KEY
    if expected_updates is not None:
        assert calls[0]["updates"] == expected_updates
    assert calls[0]["ollama_allowlist_merge"] == expected_merge


def test_seed_ollama_url_from_env_allowlist_not_merged_when_url_conflicts(monkeypatch):
    """`ollama_url` 行が既にあり新規挿入できなければ allowlist へ何も追記しない（URL と送信先の認可を
    常にペアで確定）。ollama_url 自体は競合してもマーカーは確定する。"""
    monkeypatch.setenv("OLLAMA_URL", "http://ollama-central.internal:11434")
    db = _use_fake_db(monkeypatch, {"ollama_url": "http://already-there:11434"})
    api._seed_ollama_url_from_env()
    assert "ollama_allowlist" not in db.data
    assert api._OLLAMA_URL_SEED_MARKER_KEY in db.data


@pytest.mark.parametrize("bad_url", [
    "http://admin:s3cr3t@ollama-central.internal:11434",   # userinfo 付き
    "http://ollama-central.internal:11434/?token=x",       # query 付き
])
def test_seed_ollama_url_from_env_rejects_malformed_url_and_does_not_confirm_marker(
        monkeypatch, caplog, bad_url):
    """不正形式の間は `seed_system_settings_once` 自体を呼ばず、マーカーを確定しない（警告ログを残す）。"""
    monkeypatch.setenv("OLLAMA_URL", bad_url)
    calls = _seed_recorder(monkeypatch)
    with caplog.at_level("WARNING"):
        api._seed_ollama_url_from_env()
    assert calls == []
    assert any("OLLAMA_URL" in r.message for r in caplog.records)


def test_warn_central_ollama_not_allowed_warns_only_for_non_loopback_missing_from_allowlist(monkeypatch, caplog):
    for url, allowlist, warns in (
            ("http://central.internal:11434", [], True),
            ("http://central.internal:11434", ["central.internal:11434"], False),
            ("http://localhost:11434", [], False)):
        db = _FakeSystemSettingsDB({"ollama_url": url, "ollama_allowlist": allowlist})
        monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="sherpa"):
            api._warn_central_ollama_not_allowed()
        assert any("許可一覧にありません" in r.getMessage() for r in caplog.records) is warns, url


# ---- OpenAI 接続先（`_openai_endpoint_seed_candidate` の原子性・明示 kind 優先） ----

_ENDPOINT_ENV = ("OPENAI_BASE_URL", "SHERPA_OPENAI_ENDPOINT_KIND", "SHERPA_OPENAI_AUTH_HEADER",
                 "SHERPA_OPENAI_API_VERSION")
_AZURE_V1 = "https://myres.openai.azure.com/openai/v1"


@pytest.mark.parametrize("env, expected", [
    ({}, {}),
    # kind 未指定なら host 推定した値を書かない（推定は読み取り時フォールバックに委ねる）
    ({"OPENAI_BASE_URL": _AZURE_V1}, {"openai_base_url": _AZURE_V1}),
    # 明示 kind が優先（host が Azure でも openai の明示はそのまま）
    ({"OPENAI_BASE_URL": _AZURE_V1, "SHERPA_OPENAI_ENDPOINT_KIND": "openai"},
     {"openai_endpoint_kind": "openai", "openai_base_url": _AZURE_V1}),
    ({"OPENAI_BASE_URL": "https://gw.example.com/v1", "SHERPA_OPENAI_ENDPOINT_KIND": "custom"},
     {"openai_endpoint_kind": "custom", "openai_base_url": "https://gw.example.com/v1"}),
])
def test_openai_endpoint_seed_candidate_accepts(monkeypatch, env, expected):
    for name in _ENDPOINT_ENV:
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    candidate = api._openai_endpoint_seed_candidate()
    if expected:
        assert {k: candidate[k] for k in expected} == expected
        if "openai_endpoint_kind" not in expected:
            assert "openai_endpoint_kind" not in candidate
    else:
        assert candidate == {}


@pytest.mark.parametrize("env, code", [
    ({"SHERPA_OPENAI_ENDPOINT_KIND": "bogus-value-should-not-leak"}, "invalid_endpoint_kind"),
    ({"SHERPA_OPENAI_AUTH_HEADER": "bogus-value-should-not-leak"}, "invalid_auth_header"),
    ({"SHERPA_OPENAI_ENDPOINT_KIND": "azure"}, None),   # openai 以外の kind は base_url 必須
    # base URL が不正なら他の項目が有効でも候補全体を無効にする（https 以外は拒否）
    ({"OPENAI_BASE_URL": "http://myres.openai.azure.com/openai/v1",
      "SHERPA_OPENAI_AUTH_HEADER": "api-key", "SHERPA_OPENAI_API_VERSION": "2024-10-21"}, None),
])
def test_openai_endpoint_seed_candidate_rejects(monkeypatch, env, code):
    """不正な候補は例外。固定 reason code のみで、生の env 値は文言に含めない（外へ反射されうるため）。"""
    for name in _ENDPOINT_ENV:
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValueError) as exc:
        api._openai_endpoint_seed_candidate()
    assert "bogus-value-should-not-leak" not in str(exc.value)
    if code:
        assert code in str(exc.value)


def test_seed_openai_endpoint_from_env_invalid_candidate_blocks_openai_io_until_env_fixed(monkeypatch):
    """不正な候補はマーカーを立てず（env を直せば次回再試行で取り込まれる）、確定するまで OpenAI 系 I/O を
    fail-closed にする（DB 上は未設定＝本家既定と見分けが付かないためプロセス内フラグでブロック）。
    DB 一時障害はこの経路を通らずブロックを立てない。"""
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", None)   # 他テストへ漏らさない
    monkeypatch.setattr(store, "schema_ready", lambda: True)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://myres.openai.azure.com/openai/v1")   # http は不許可
    monkeypatch.setenv("SHERPA_OPENAI_AUTH_HEADER", "api-key")
    db = _use_fake_db(monkeypatch)

    assert llm.openai_endpoint_seed_blocked_reason() is None
    api._seed_openai_endpoint_from_env()
    assert "openai_endpoint_seed_version" not in db.data
    assert "openai_auth_header" not in db.data and "openai_base_url" not in db.data
    assert db.seed_calls == []
    assert llm.openai_endpoint_seed_blocked_reason() is not None
    with pytest.raises(RuntimeError):
        llm.openai_url("chat/completions")
    with pytest.raises(RuntimeError):
        llm.openai_headers("sk-dummy")

    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", None)
    monkeypatch.setattr(store, "get_system_settings", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    api._seed_openai_endpoint_from_env()
    assert llm.openai_endpoint_seed_blocked_reason() is None

    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setenv("OPENAI_BASE_URL", _AZURE_V1)   # env を直す
    api._seed_openai_endpoint_from_env()
    assert db.data["openai_endpoint_seed_version"] == api._OPENAI_ENDPOINT_SEED_VERSION
    assert db.data["openai_base_url"] == _AZURE_V1
    assert db.data["openai_auth_header"] == "api-key"
    assert llm.openai_endpoint_seed_blocked_reason() is None
    llm.openai_url("chat/completions")   # 例外を出さない


def test_seed_openai_endpoint_from_env_unblocks_when_marker_already_confirmed(monkeypatch):
    """マーカーが既に確定済み（他プロセス・手動修正でも）なのにこのプロセスのブロックだけが残っているとき、
    早期 return の前に検知して解除する（再起動まで固定されない）。"""
    monkeypatch.setattr(store, "schema_ready", lambda: True)
    db = _use_fake_db(monkeypatch, {api._OPENAI_ENDPOINT_SEED_MARKER_KEY: api._OPENAI_ENDPOINT_SEED_VERSION,
                                    "openai_base_url": _AZURE_V1})
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", "旧試行で不正だった名残")
    api._seed_openai_endpoint_from_env()
    assert llm.openai_endpoint_seed_blocked_reason() is None
    assert db.seed_calls == []
    llm.openai_url("chat/completions")


# ---- 調べる深さの基準値・画面で変えられる運用設定 ----

def test_seed_depth_profile_from_env_writes_all_six_keys_and_marker_in_one_call(monkeypatch):
    """6 項目とマーカーを同一呼び出しで書く。値は env の有効値→各モジュールのコード既定の順。"""
    from sherpa import chat_service
    for name in ("SHERPA_GREP_MAX_HITS", "SHERPA_READ_WINDOW", "SHERPA_IMPACT_MAX_DEPTH",
                 "SHERPA_CODEX_REASONING"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SHERPA_TROUBLESHOOT_GRAPH_DEPTH", "6")
    calls = _seed_recorder(monkeypatch)
    api._seed_depth_profile_from_env()
    assert len(calls) == 1
    assert calls[0]["guard_key"] == api._DEPTH_PROFILE_SEED_MARKER_KEY
    assert calls[0]["updates"] == {
        "depth_base_grep_max_hits": 45,
        "depth_base_qa_max_hits": chat_service.QA_MAX_HITS_DEFAULT,
        "depth_base_read_window": 60,
        "depth_base_impact_depth": 10,
        "depth_base_troubleshoot_depth": 6,
        "depth_base_codex_reasoning": "medium",
        api._DEPTH_PROFILE_SEED_MARKER_KEY: api._DEPTH_PROFILE_SEED_VERSION,
    }


def test_seed_depth_profile_from_env_does_not_overwrite_existing_admin_value(monkeypatch):
    db = _use_fake_db(monkeypatch, {"depth_base_grep_max_hits": 99})
    api._seed_depth_profile_from_env()
    assert db.data["depth_base_grep_max_hits"] == 99
    assert db.data[api._DEPTH_PROFILE_SEED_MARKER_KEY] == api._DEPTH_PROFILE_SEED_VERSION
    assert db.data["depth_base_read_window"] is not None   # 他の 5 項目は通常どおりシードされる


def test_seed_depth_profile_from_env_rejects_unknown_codex_reasoning_and_writes_nothing(monkeypatch, caplog):
    """既知語彙以外は 6 項目・マーカーとも書かない（一回性マーカー付きで永続化すると env 修正後に回復しない）。"""
    monkeypatch.setenv("SHERPA_CODEX_REASONING", "ultra")
    calls = _seed_recorder(monkeypatch)
    with caplog.at_level("ERROR", logger="sherpa"):
        api._seed_depth_profile_from_env()
    assert calls == []
    assert any("SHERPA_CODEX_REASONING" in r.message for r in caplog.records)


def test_seed_depth_profile_from_env_normalizes_codex_reasoning_case_and_whitespace(monkeypatch):
    """管理 API と同じ `strip().lower()` で正規化してからシードする（`" HIGH "` → `"high"`）。"""
    monkeypatch.setenv("SHERPA_CODEX_REASONING", " HIGH ")
    calls = _seed_recorder(monkeypatch)
    api._seed_depth_profile_from_env()
    assert len(calls) == 1
    assert calls[0]["updates"]["depth_base_codex_reasoning"] == "high"


def test_screen_settings_seed_from_env_then_workspace_limits_follow_db_only(monkeypatch):
    """env に有効な値がある項目だけ初回シードで DB へ入り（未設定・不正は入れない）、以後の実行時は
    DB の値だけを読む（env を後から変えても効かない・DB 未設定ならコード既定）。"""
    from sherpa import workspace_limits
    monkeypatch.delenv("SHERPA_CHAT_MAX_TURNS_PER_USER", raising=False)
    monkeypatch.setenv("SHERPA_WORKSPACE_MAX_BYTES", str(2 * 1024 * 1024))
    monkeypatch.setenv("SHERPA_WORKSPACE_TTL_DAYS", "30")
    monkeypatch.setenv("SHERPA_CHAT_MAX_TURNS_GLOBAL", "5")
    monkeypatch.setenv("SHERPA_ARMS", "ooxml,bogus,pdf_text")
    monkeypatch.setenv("SHERPA_LEGACY_BACKEND", "bogus")   # 不正な値は取り込まない
    db = _use_fake_db(monkeypatch)
    api._seed_screen_settings_from_env()
    assert db.data["workspace_max_bytes"] == 2 * 1024 * 1024
    assert db.data["workspace_ttl_days"] == 30
    assert db.data["chat_max_turns_global"] == 5
    assert db.data["arms_enabled"] == ["ooxml", "pdf_text"]
    assert "legacy_backend" not in db.data and "chat_max_turns_per_user" not in db.data
    assert db.data[api._SCREEN_SETTINGS_SEED_MARKER_KEY] == api._SCREEN_SETTINGS_SEED_VERSION

    monkeypatch.setenv("SHERPA_WORKSPACE_MAX_BYTES", str(50 * 1024 * 1024))   # 実行時は読まない
    assert workspace_limits.max_bytes() == 2 * 1024 * 1024
    assert workspace_limits.ttl_days() == 30
    db.data["workspace_ttl_days"] = 0   # 管理画面で「無期限」に変えた値がそのまま効く
    assert workspace_limits.ttl_days() == 0
    del db.data["workspace_max_bytes"]   # 未設定はコード既定（env ではない）
    assert workspace_limits.max_bytes() == workspace_limits.MAX_BYTES_DEFAULT


# ===== healthz による起動時シードの再試行 =====
# healthz は schema ready のたびに全シードを同じ single-flight の枠で再試行する（冪等）。
# 各テストは検証対象のシード以外を no-op に差し替える（実テスト DB へのマーカー書込みや
# プロセス内 openai I/O ブロック状態の変更を避けるため）。

_HEALTHZ_API_SEEDS = {
    "credential": "_seed_settings_from_env", "ollama_url": "_seed_ollama_url_from_env",
    "ollama_allowlist": "_warn_central_ollama_not_allowed", "openai_endpoint": "_seed_openai_endpoint_from_env",
    "depth_profile": "_seed_depth_profile_from_env", "screen": "_seed_screen_settings_from_env",
    "user_agent": "_seed_user_agent_from_env", "vlm_ollama_url": "_seed_vlm_ollama_url_from_env",
}


def _healthz_isolate(monkeypatch, keep=()):
    """healthz が呼ぶ起動時シードのうち `keep` 以外（"catalog" は model_catalog）を no-op にする。"""
    monkeypatch.setattr(store, "schema_ready", lambda: True)
    for key, name in _HEALTHZ_API_SEEDS.items():
        if key not in keep:
            monkeypatch.setattr(api, name, lambda: None)
    if "catalog" not in keep:
        monkeypatch.setattr(model_catalog, "seed_catalog_once", lambda: None)


def _flaky_first_read(monkeypatch, db):
    """最初の `get_system_settings` だけ例外（DB 瞬断）。"""
    state = {"n": 0}

    def _flaky_get():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient db blip")
        return db.get_system_settings()

    monkeypatch.setattr(store, "get_system_settings", _flaky_get)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)


@pytest.mark.parametrize("ready_sequence, n_calls, expected", [
    ([False, True], 1, [True]),       # 未 ready → readiness 回復の瞬間に再試行（DB 不達時はマーカーを付けない）
    (None, 2, [True, True]),          # 常に ready でも呼び出しのたびに再試行（遷移の瞬間だけではない）
])
def test_healthz_retries_seed_on_schema_readiness_and_on_every_call(
        monkeypatch, ready_sequence, n_calls, expected):
    from sherpa.routers import system as system_router
    _healthz_isolate(monkeypatch)
    if ready_sequence is not None:
        seq = iter(ready_sequence)
        monkeypatch.setattr(store, "schema_ready", lambda: next(seq, True))
        monkeypatch.setattr(store, "init_schema", lambda: None)
    seed_calls = []
    monkeypatch.setattr(api, "_seed_settings_from_env", lambda: seed_calls.append(True))
    for _ in range(n_calls):
        system_router.healthz()
    assert seed_calls == expected


@pytest.mark.parametrize("keep, marker_key, version, env, extra", [
    ("credential", api._CREDENTIAL_SEED_MARKER_KEY, api._CREDENTIAL_SEED_VERSION,
     {"OPENAI_API_KEY": "sk-seed-openai"}, {"openai_api_key": "sk-seed-openai"}),
    ("catalog", model_catalog._CATALOG_SEED_MARKER_KEY, model_catalog._CATALOG_SEED_VERSION, {}, {}),
    ("openai_endpoint", api._OPENAI_ENDPOINT_SEED_MARKER_KEY, api._OPENAI_ENDPOINT_SEED_VERSION,
     {"OPENAI_BASE_URL": None}, {}),   # 未設定＝何も取り込まないが完了マーカー自体は書く
    ("depth_profile", api._DEPTH_PROFILE_SEED_MARKER_KEY, api._DEPTH_PROFILE_SEED_VERSION, {}, {}),
])
def test_healthz_seed_retries_after_transient_failure_and_seeds_once(
        monkeypatch, keep, marker_key, version, env, extra):
    """そのシードだけが一時的に失敗（DB 瞬断）しても、次の healthz で再試行され最終的に 1 回だけ書かれる。"""
    from sherpa.routers import system as system_router
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    _healthz_isolate(monkeypatch, keep=(keep,))
    db = _FakeSystemSettingsDB()
    _flaky_first_read(monkeypatch, db)

    system_router.healthz()   # 1 回目: 例外→シード失敗（healthz は落ちない）
    assert marker_key not in db.data

    system_router.healthz()   # 2 回目: 再試行して成功
    assert db.data[marker_key] == version
    for k, v in extra.items():
        assert db.data[k] == v
    assert len(db.seed_calls) == 1


def test_healthz_retries_credential_and_catalog_seeds_independently_with_separate_markers(monkeypatch):
    """資格情報シードと model_catalog シードは独立したマーカーを持ち互いを上書き・スキップさせない。
    1 回目で両方が書き、2 回目はどちらも書き込みが増えない。"""
    from sherpa.routers import system as system_router
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "OPENAI_EMBED_MODEL"):
        monkeypatch.delenv(name, raising=False)
    _healthz_isolate(monkeypatch, keep=("credential", "catalog"))
    db = _use_fake_db(monkeypatch)

    system_router.healthz()
    assert db.data.get("credential_seed_version") == api._CREDENTIAL_SEED_VERSION
    assert db.data.get("model_catalog_seed_version") == model_catalog._CATALOG_SEED_VERSION
    assert "model_catalog" in db.data
    assert len(db.seed_calls) == 2

    system_router.healthz()
    assert len(db.seed_calls) == 2


@pytest.mark.parametrize("keep, env_name, bad, good, marker_key, version, expected_data", [
    ("ollama_url", "OLLAMA_URL", "http://admin:s3cr3t@ollama-central.internal:11434",
     "http://ollama-central.internal:11434", api._OLLAMA_URL_SEED_MARKER_KEY, api._OLLAMA_URL_SEED_VERSION,
     {"ollama_url": "http://ollama-central.internal:11434"}),
    ("depth_profile", "SHERPA_CODEX_REASONING", "ultra", "high", api._DEPTH_PROFILE_SEED_MARKER_KEY,
     api._DEPTH_PROFILE_SEED_VERSION, {"depth_base_codex_reasoning": "high"}),
])
def test_healthz_seed_reevaluates_after_env_is_fixed(
        monkeypatch, keep, env_name, bad, good, marker_key, version, expected_data):
    """不正な env の間はマーカーが立たず（書込みもしない）、env を直した次の healthz で再評価されて確定する。"""
    from sherpa.routers import system as system_router
    _healthz_isolate(monkeypatch, keep=(keep,))
    db = _use_fake_db(monkeypatch)

    monkeypatch.setenv(env_name, bad)
    system_router.healthz()
    assert marker_key not in db.data
    assert db.seed_calls == []

    monkeypatch.setenv(env_name, good)
    system_router.healthz()
    assert db.data[marker_key] == version
    for k, v in expected_data.items():
        assert db.data[k] == v


# ===== 起動時の個人キー削除（A6 が false のとき）=====
# 実 DB での「false のときだけ削除・true なら残す」は test_system_settings.py が確かめる。

def test_purge_personal_keys_on_startup_logs_only_when_count_positive_and_runs_as_system(monkeypatch, caplog):
    monkeypatch.setattr("sherpa.keys.personal_keys_allowed", lambda: False)
    actors = []
    for count in (5, 0):
        monkeypatch.setattr(store, "purge_personal_api_keys", lambda actor="system", c=count: (actors.append(actor), c)[1])
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="sherpa"):
            api._purge_personal_keys_if_disabled_on_startup()
        logged = any("削除" in r.getMessage() and "5" in r.getMessage() for r in caplog.records)
        assert logged is (count == 5)   # 0 件ならノイズにしない
    assert actors == ["system", "system"]


def test_purge_personal_keys_on_startup_survives_db_unreachable(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("sherpa.keys.personal_keys_allowed", _boom)
    api._purge_personal_keys_if_disabled_on_startup()   # 例外を投げなければ OK


# ===== TestClient 起動で起動処理が決まった順序で走る =====

# (対象, 属性名, 呼ばれたときのラベル)。ラベル None は no-op。
_STARTUP_STEPS = [
    (api, "_seed_settings_from_env", "seed_settings"),
    (api, "_seed_ollama_url_from_env", "seed_ollama_url"),
    (api, "_warn_central_ollama_not_allowed", "catchup_ollama_allowlist"),
    (api, "_seed_openai_endpoint_from_env", "seed_openai_endpoint"),
    (api, "_seed_depth_profile_from_env", "seed_depth_profile"),
    (api, "_seed_screen_settings_from_env", "seed_screen_settings"),
    (model_catalog, "seed_catalog_once", "model_catalog_seed"),
    (api, "_purge_personal_keys_if_disabled_on_startup", "purge_personal_keys"),
    (api, "_warn_change_me_placeholders", "warn_change_me"),
    (api, "_warn_default_admin_password", "warn_default_admin"),
    (api, "_auth_bootstrap_on_startup", "auth"),
    (api, "_warn_fixtures", "warn_fixtures"),
    (api, "_warn_test_db_isolated", "warn_test_db_isolated"),
    (api, "_warn_codex_sandbox_disabled", "warn_codex_sandbox"),
    (api, "_warn_multi_worker_chat_turns", "warn_multi_worker"),
    (api, "_warn_browse_roots_missing", "warn_browse_roots"),
    (api, "_reconcile_orphans", "reconcile"),
    (api, "_sweep_expired_on_startup", "sweep"),
    (api, "_backfill_turn_metrics_on_startup", "turn_metrics_backfill"),
]


def _record_startup_steps(monkeypatch) -> list[str]:
    calls: list[str] = []
    for owner, name, label in _STARTUP_STEPS:
        monkeypatch.setattr(owner, name, lambda *_a, _l=label: calls.append(_l))
    return calls


@pytest.mark.parametrize("schema_fails", [False, True])
def test_lifespan_runs_startup_steps_in_order(monkeypatch, schema_fails):
    """`with TestClient(app)` で起動処理が決まった順序で走る。先頭は request_id filter の再 attach
    （ASGI サーバーが後から追加した handler にも届くように）、次に `store.init_schema()`（auth bootstrap が
    schema 依存のため全 startup の先頭）。`_warn_change_me_placeholders`／`_warn_default_admin_password` は
    auth bootstrap が admin を DB に刻む前に検査する。schema 初期化の失敗（DB 不達）は warning のみで後続を
    1 つも止めない（readiness が false のままになるだけ）。"""
    calls = _record_startup_steps(monkeypatch)
    monkeypatch.setattr(ext_api, "_attach_request_id_filter", lambda: calls.append("attach_request_id_filter"))

    def _boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(store, "init_schema", _boom if schema_fails else lambda: calls.append("schema"))
    with TestClient(api.app):
        pass
    assert calls == [
        "attach_request_id_filter", *([] if schema_fails else ["schema"]),
        "seed_settings", "seed_ollama_url", "catchup_ollama_allowlist", "seed_openai_endpoint",
        "seed_depth_profile", "seed_screen_settings", "model_catalog_seed", "purge_personal_keys",
        "warn_change_me", "warn_default_admin", "auth",
        "warn_fixtures", "warn_test_db_isolated", "warn_codex_sandbox", "warn_multi_worker", "warn_browse_roots",
        "reconcile", "sweep", "turn_metrics_backfill",
    ]


def _noop_all_startup_steps(monkeypatch):
    monkeypatch.setattr(store, "init_schema", lambda: None)
    for owner, name, _label in _STARTUP_STEPS:
        monkeypatch.setattr(owner, name, lambda *_a: None)
    for name in ("_seed_user_agent_from_env", "_seed_vlm_ollama_url_from_env"):
        monkeypatch.setattr(api, name, lambda *_a: None)


def test_lifespan_stops_audit_writer_even_when_startup_step_raises(monkeypatch):
    """`ext_api._audit_writer.start()` の後で起動処理が例外を投げても、stop() は try/finally で必ず呼ばれる
    （さもないと start() 済みの writer が取り残される）。シードは実 DB へ書かないよう no-op にする。"""
    _noop_all_startup_steps(monkeypatch)

    def _boom():
        raise RuntimeError("simulated startup failure")

    monkeypatch.setattr(api, "_warn_default_admin_password", _boom)
    stop_calls: list[bool] = []
    orig_stop = ext_api._audit_writer.stop

    def _tracking_stop(*a, **kw):
        stop_calls.append(True)
        return orig_stop(*a, **kw)

    monkeypatch.setattr(ext_api._audit_writer, "stop", _tracking_stop)

    with pytest.raises(RuntimeError):
        with TestClient(api.app):
            pass
    assert stop_calls == [True], "起動処理中の例外でも audit writer の stop() が呼ばれていない"
    assert ext_api._audit_writer._state == ext_api._WRITER_STOPPED


def test_lifespan_joins_startup_sweep_before_closing_pg_pool(monkeypatch):
    """起動時の掃除スレッドは停止イベントの管理下に置かれ、終了時に PG プールを閉じる前に join される。"""
    from sherpa.store import db as store_db

    events: list[str] = []

    class _FakeThread:
        def join(self, timeout=None):
            events.append("join")

    _noop_all_startup_steps(monkeypatch)
    stops: list[bool] = []

    def _sweep(stop):
        stops.append(stop.is_set())
        return _FakeThread()

    monkeypatch.setattr(api, "_sweep_expired_on_startup", _sweep)
    monkeypatch.setattr(api, "_start_workspace_maintenance_loop", lambda stop: _FakeThread())
    monkeypatch.setattr(store_db, "close_pg_pool", lambda: events.append("close_pool"))
    with TestClient(api.app):
        pass
    assert stops == [False]
    assert events == ["join", "join", "close_pool"]
