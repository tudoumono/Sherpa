"""lifespan（起動処理）受け入れテスト（refactoring-plan フェーズ1・on_event→lifespan 移行）。

`@app.on_event("startup")` から `sherpa.lifespan.lifespan` への移行に伴い、起動 3 点の挙動を固定する:
  ① production で fixtures を参照し得る設定なら起動拒否（fail-closed）／dev は警告のみで続行
  ② folder poller が env（SHERPA_POLL_SECONDS）で有効/無効に切り替わる
  ③ TestClient 起動（lifespan の __enter__）で起動処理が旧登録順どおりに走る

いずれも DB/Neo4j/ES を要さないよう、外部サービスに触れる本体は monkeypatch でスタブ化する。
"""
from __future__ import annotations

import os

import pytest

import sherpa.api as api
from fastapi.testclient import TestClient
from sherpa import ext_api, llm, model_catalog, store
from sherpa.ingest import background


@pytest.fixture(autouse=True)
def _restore_background_accepting():
    """`with TestClient(api.app):` は実 lifespan の shutdown（`background.stop_accepting()`・
    ING-3）を経由する——`_accepting` はプロセス寿命のモジュールグローバルのため、ここで
    戻さないと本ファイルの後に実行される他の全テスト（`POST /worlds` 等の背景実行）が
    `ShuttingDownError`（503）で壊れる（pytest は同一プロセス内で全テストを実行するため）。"""
    yield
    background.start_accepting()


@pytest.fixture(autouse=True)
def _default_no_legacy_marker(monkeypatch):
    """`store.migrate_marker_if_legacy_exists` を既定で `False`（旧 `env_seed_version` は存在しない
    ＝新規導入環境）へ差し替える（autouse）。`_seed_settings_from_env`／`_seed_ollama_url_from_env`
    は候補構築より前に必ずこれを呼ぶため、個々のテストが明示的に差し替えない限り実 DB へ触れて
    しまう（`with TestClient(api.app):`／`system_router.healthz()` を使うテストを含め、本ファイルの
    大半は「新規導入環境」の挙動を検証する前提のため、既定を安全側＝False に倒す）。旧マーカー分岐
    そのものを検証するテストは、各テスト内で `monkeypatch.setattr(store,
    "migrate_marker_if_legacy_exists", ...)` により後勝ちで上書きする。"""
    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists",
                        lambda guard_key, legacy_key, guard_value=True: False)


class _FakeThread:
    """`threading.Thread(...).start()` を捕捉するスタブ（実スレッドは起動しない）。"""

    def __init__(self, records, *args, target=None, daemon=None, name=None, **kwargs):
        self._records = records
        self.name = name
        self.daemon = daemon
        self.target = target

    def start(self):
        self._records.append({"name": self.name, "daemon": self.daemon})


def _thread_recorder(records):
    def _factory(*args, **kwargs):
        return _FakeThread(records, *args, **kwargs)
    return _factory


# ---- ① fixtures fail-closed 検査 ----

def test_warn_fixtures_fail_closed_in_production(monkeypatch):
    """SHERPA_ENV=production かつ fixtures 到達可（SHERPA_USE_FIXTURES=1）なら RuntimeError で起動拒否。"""
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.setenv("SHERPA_USE_FIXTURES", "1")
    with pytest.raises(RuntimeError):
        api._warn_fixtures()


def test_warn_fixtures_dev_warns_but_continues(monkeypatch):
    """dev（本番マーカーなし）は fixtures 到達可でも警告のみ・例外は投げない（起動続行）。"""
    monkeypatch.setenv("SHERPA_ENV", "dev")
    monkeypatch.setenv("SHERPA_USE_FIXTURES", "1")
    assert api._warn_fixtures() is None


# ---- ①-0 初期 admin 既定パスワードの production fail-closed 検査（監査台帳#3） ----

def test_warn_default_admin_password_fail_closed_when_unset_in_production(monkeypatch):
    """production かつ SHERPA_ADMIN_PASSWORD 未設定なら RuntimeError で起動拒否。"""
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.delenv("SHERPA_ADMIN_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        api._warn_default_admin_password()


def test_warn_default_admin_password_allows_explicit_default_value_in_production(monkeypatch):
    """production で SHERPA_ADMIN_PASSWORD が明示設定されていれば開発既定と同値でも起動を許す
    （ユーザー決定 2026-07-10・2026-09-03 復元＝閉域前提＋初回ログインの変更強制でローテーション）。"""
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.setenv("SHERPA_ADMIN_PASSWORD", "Sherpa2026!")
    api._warn_default_admin_password()   # raise しない


def test_warn_default_admin_password_ok_when_custom_value_in_production(monkeypatch):
    """production で既定値以外を明示設定していれば起動OK（例外を投げない）。"""
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.setenv("SHERPA_ADMIN_PASSWORD", "correct-horse-battery-staple")
    assert api._warn_default_admin_password() is None


def test_warn_default_admin_password_dev_warns_but_continues(monkeypatch):
    """development（本番マーカーなし）は未設定でも警告のみ・例外は投げない。"""
    monkeypatch.setenv("SHERPA_ENV", "dev")
    monkeypatch.delenv("SHERPA_ADMIN_PASSWORD", raising=False)
    assert api._warn_default_admin_password() is None


def test_warn_default_admin_password_fail_closed_when_empty_string_in_production(monkeypatch):
    """production かつ SHERPA_ADMIN_PASSWORD="" は「未設定」と同じ扱いで起動拒否（監査台帳 LOW-3）。"""
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.setenv("SHERPA_ADMIN_PASSWORD", "")
    with pytest.raises(RuntimeError):
        api._warn_default_admin_password()


def test_warn_default_admin_password_fail_closed_when_whitespace_only_in_production(monkeypatch):
    """production かつ SHERPA_ADMIN_PASSWORD="   "（空白のみ）は「未設定」と同じ扱いで起動拒否
    （監査台帳 LOW-3: strip() で空になる値は明示設定とみなさない）。"""
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.setenv("SHERPA_ADMIN_PASSWORD", "   ")
    with pytest.raises(RuntimeError):
        api._warn_default_admin_password()


# ---- ①-0b CHANGE_ME プレースホルダの production fail-closed 検査（ENV-ONE・2026-09-03） ----

def test_warn_change_me_placeholders_fail_closed_in_production(monkeypatch):
    """production で値に CHANGE_ME を含む env があれば RuntimeError で起動拒否。"""
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.setenv("SHERPA_AUDIT_IP_SALT", "CHANGE_ME_LONG_RANDOM_SALT")
    with pytest.raises(RuntimeError):
        api._warn_change_me_placeholders()


def test_warn_change_me_placeholders_reports_key_names_in_message(monkeypatch):
    """どのキーが該当したかがエラーメッセージへ平文で出る（値そのものは伏せる）。"""
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.setenv("POSTGRES_PASSWORD", "CHANGE_ME_POSTGRES_PASSWORD")
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
        api._warn_change_me_placeholders()


def test_warn_change_me_placeholders_ok_when_no_placeholder_in_production(monkeypatch):
    """production でも CHANGE_ME を含む値が無ければ起動OK（例外を投げない）。"""
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.setenv("SHERPA_AUDIT_IP_SALT", "a-real-random-salt-value")
    assert api._warn_change_me_placeholders() is None


def test_warn_change_me_placeholders_dev_warns_but_continues(monkeypatch):
    """development（本番マーカーなし）は CHANGE_ME があっても警告のみ・例外は投げない。"""
    monkeypatch.setenv("SHERPA_ENV", "dev")
    monkeypatch.setenv("SHERPA_AUDIT_IP_SALT", "CHANGE_ME_LONG_RANDOM_SALT")
    assert api._warn_change_me_placeholders() is None


# ---- env → system_settings 初回シード（`api._seed_settings_from_env`） ----
# 完了マーカー（`system_settings.credential_seed_version`）方式。マーカーがあれば env を
# 一切読まない（管理者が中央キーを削除した後の再起動で env から復活しない・下記のテストが固定）。
# 旧共有マーカー（`env_seed_version`）は、このマーカーと `ollama_url_seed_version` の両方が
# 確定した後にだけ rollback 互換のため追いつき確定する集約マーカー（直接の guard には使わない・
# `api._confirm_legacy_env_seed_marker` 参照）。
# 上書き防止の不変条件そのものは `store.seed_system_settings_once`（各 INSERT が guard_key の
# 行の不在を WHERE NOT EXISTS で確認してから書く）が担保するため、以下は実 DB の代わりに
# 素朴な dict ベースの疑似永続化（`_FakeSystemSettingsDB`）でその意味論まで含めて検証する。

class _FakeSystemSettingsDB:
    """`store.seed_system_settings_once` の意味論を dict で模すテスト用の疑似永続化。

    実 DB の `WHERE NOT EXISTS (guard_key の行) ... ON CONFLICT (key) DO NOTHING` を再現する:
    guard_key の行が既にあれば（マーカー確認済み）**個々のキーの有無に関わらず一切書かない**
    （管理者が特定のキーだけ削除していても、その削除後の状態を尊重して再挿入しない）。
    guard_key が無いときだけ、キーごとに「既存なら書かない」を見る。"""

    def __init__(self, initial: dict | None = None, *, catchup_v2_reason: str = "skipped_unproven",
                catchup_v2_host: str | None = None):
        self.data: dict = dict(initial or {})
        self.seed_calls: list[dict] = []
        self.migrate_calls: list[dict] = []
        self.catchup_calls: list[dict] = []
        # v2 catch-up は audit ログを根拠に判定する（実 DB での意味論は
        # tests/api/test_system_settings.py の `test_catchup_v2_*` 群が固定する）。ここでは
        # api.py 側のオーケストレーション（marker gate・常時警告）だけを見るため、判定結果は
        # テストが差し替えられる缶詰値にする。
        self.catchup_v2_reason = catchup_v2_reason
        self.catchup_v2_host = catchup_v2_host

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
            if marker_present or k in self.data:   # マーカー確定済み、またはこのキーが既存なら書かない
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

    def migrate_marker_if_legacy_exists(self, guard_key: str, legacy_key: str,
                                        guard_value: object = True) -> bool:
        """`store.migrate_marker_if_legacy_exists` の意味論を dict で模す（`legacy_key` があれば
        `guard_key` だけを「移行済み」として確定し True・無ければ何もせず False）。
        `seed_calls` には記録しない（`seed_system_settings_once` とは別の関数のため）。"""
        self.migrate_calls.append({"guard_key": guard_key, "legacy_key": legacy_key})
        if legacy_key not in self.data:
            return False
        if guard_key not in self.data:
            self.data[guard_key] = guard_value
        return True

    def catchup_ollama_allowlist_for_env_seeded_url_v2(self, guard_key: str) -> str:
        """`store.catchup_ollama_allowlist_for_env_seeded_url_v2` の意味論を dict で模す（marker
        gate のみ・判定結果は `catchup_v2_reason`/`catchup_v2_host` で差し替え可能な缶詰値）。"""
        self.catchup_calls.append({"guard_key": guard_key})
        if guard_key in self.data:
            return "already_present"
        self.data[guard_key] = 1
        if self.catchup_v2_reason == "added" and self.catchup_v2_host:
            current = list(self.data.get("ollama_allowlist") or [])
            if self.catchup_v2_host not in current:
                self.data["ollama_allowlist"] = [*current, self.catchup_v2_host]
        return self.catchup_v2_reason


def _set_system_settings_recorder(monkeypatch):
    """`store.seed_system_settings_once` を差し替えて呼び出し引数を記録する（`_FakeSystemSettingsDB`
    を使わない単純なケース向け・guard_key/secret_keys/ollama_allowlist_merge kwarg も受理）。

    `store.migrate_marker_if_legacy_exists` も常に `False`（旧 `env_seed_version` は存在しない＝
    新規導入環境）へ差し替える（`_seed_settings_from_env`／`_seed_ollama_url_from_env` の両方が
    候補構築より前にこれを呼ぶため、差し替えないと実 DB へ触れてしまう）。旧マーカー分岐そのものを
    検証するテストは `monkeypatch.setattr(store, "migrate_marker_if_legacy_exists", ...)` で
    このデフォルトを個別に上書きすること。"""
    calls = []

    def _fake(updates, guard_key=None, secret_keys=None, *, ollama_allowlist_merge=None):
        calls.append({"updates": dict(updates), "guard_key": guard_key, "secret_keys": secret_keys,
                      "ollama_allowlist_merge": ollama_allowlist_merge})
        return dict(updates), {}
    monkeypatch.setattr(store, "seed_system_settings_once", _fake)
    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists",
                        lambda guard_key, legacy_key, guard_value=True: False)
    return calls


def test_seed_settings_from_env_writes_keys_and_marker_in_one_call(monkeypatch):
    """未シード（マーカー無し）状態で、対象キーとマーカーを**同一の seed_system_settings_once 呼び出し**
    （＝同一トランザクション）で書く。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-seed-openai")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-seed-key")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("ANTHROPIC_AWS_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("SHERPA_PERSONAL_API_KEYS", raising=False)
    monkeypatch.delenv("SHERPA_ALLOW_WEB_SEARCH", raising=False)   # WEB-1: 未設定なら候補に含めない
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_settings_from_env()
    assert len(calls) == 1
    assert calls[0]["updates"] == {
        "openai_api_key": "sk-seed-openai", "gemini_api_key": "gemini-seed-key",
        "credential_seed_version": api._CREDENTIAL_SEED_VERSION,
    }
    assert calls[0]["secret_keys"] == {"openai_api_key", "gemini_api_key"}


def test_seed_settings_from_env_ignores_ollama_url_entirely(monkeypatch):
    """`OLLAMA_URL` はもう `_seed_settings_from_env()` の対象キーではない
    （`_seed_ollama_url_from_env()` へ分離した・下の専用テスト群参照）。不正な形式であっても
    このキーの妥当性は他の資格情報の確定に一切影響しない（マーカーは常に確定する）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-seed-openai")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("ANTHROPIC_AWS_API_KEY", raising=False)
    monkeypatch.delenv("SHERPA_PERSONAL_API_KEYS", raising=False)
    monkeypatch.delenv("SHERPA_ALLOW_WEB_SEARCH", raising=False)   # WEB-1: 未設定なら候補に含めない
    monkeypatch.setenv("OLLAMA_URL", "http://admin:s3cr3t@ollama-central.internal:11434")   # 不正形式
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_settings_from_env()
    assert len(calls) == 1
    assert "ollama_url" not in calls[0]["updates"]
    assert calls[0]["updates"]["credential_seed_version"] == api._CREDENTIAL_SEED_VERSION   # 他キーは通常どおり確定


# ===== OLLAMA_URL の独立シード（`api._seed_ollama_url_from_env`） =====
# 以前は `_seed_settings_from_env()` の共有マーカー（`env_seed_version`）へ相乗りしており、
# OLLAMA_URL が不正な形式でもマーカーが確定してしまい「env を直した後の次回起動で再評価される」
# という docstring の約束を果たせなかった。専用マーカー（`ollama_url_seed_version`）に分離し、
# 不正な間はこのマーカーだけ確定しない（他の資格情報の確定は妨げない・上のテスト参照）。

def _set_ollama_seed_recorder(monkeypatch):
    """`store.seed_system_settings_once` を差し替えて呼び出し引数を記録する
    （`_set_system_settings_recorder` と同じ形・`_seed_ollama_url_from_env` 専用・
    `store.migrate_marker_if_legacy_exists` の既定 False 差し替えも同様）。"""
    calls = []

    def _fake(updates, guard_key=None, secret_keys=None, *, ollama_allowlist_merge=None):
        calls.append({"updates": dict(updates), "guard_key": guard_key,
                      "ollama_allowlist_merge": ollama_allowlist_merge})
        return dict(updates), {}
    monkeypatch.setattr(store, "seed_system_settings_once", _fake)
    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists",
                        lambda guard_key, legacy_key, guard_value=True: False)
    return calls


def test_seed_ollama_url_from_env_adds_non_loopback_url_to_allowlist_atomically(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://ollama-central.internal:11434")
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_ollama_seed_recorder(monkeypatch)
    api._seed_ollama_url_from_env()
    assert len(calls) == 1
    assert calls[0]["guard_key"] == api._OLLAMA_URL_SEED_MARKER_KEY
    assert calls[0]["updates"] == {
        "ollama_url": "http://ollama-central.internal:11434",
        api._OLLAMA_URL_SEED_MARKER_KEY: api._OLLAMA_URL_SEED_VERSION}
    assert calls[0]["ollama_allowlist_merge"] == ("ollama_url", "ollama-central.internal:11434")


def test_seed_ollama_url_from_env_does_not_allowlist_loopback(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_ollama_seed_recorder(monkeypatch)
    api._seed_ollama_url_from_env()
    assert len(calls) == 1
    assert calls[0]["ollama_allowlist_merge"] is None


def test_seed_ollama_url_from_env_empty_env_confirms_marker_only(monkeypatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_ollama_seed_recorder(monkeypatch)
    api._seed_ollama_url_from_env()
    assert len(calls) == 1
    assert calls[0]["updates"] == {api._OLLAMA_URL_SEED_MARKER_KEY: api._OLLAMA_URL_SEED_VERSION}


def test_seed_ollama_url_from_env_normalizes_port_omission(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://ollama-central.internal")
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_ollama_seed_recorder(monkeypatch)
    api._seed_ollama_url_from_env()
    assert calls[0]["updates"]["ollama_url"] == "http://ollama-central.internal:80"
    assert calls[0]["ollama_allowlist_merge"] == ("ollama_url", "ollama-central.internal:80")


def test_seed_ollama_url_from_env_noop_when_marker_already_present(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://ollama-central.internal:11434")
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {api._OLLAMA_URL_SEED_MARKER_KEY: 1})
    calls = _set_ollama_seed_recorder(monkeypatch)
    api._seed_ollama_url_from_env()
    assert calls == []


def test_seed_ollama_url_from_env_allowlist_not_merged_when_url_conflicts(monkeypatch):
    """`ollama_url` 行が既に存在し（他プロセス/事前挿入等で）このトランザクションでは新規挿入
    できなかった場合、allowlist へは何も追記しない（URL とその送信先の認可を常にペアとして
    確定させる）。"""
    monkeypatch.setenv("OLLAMA_URL", "http://ollama-central.internal:11434")
    db = _FakeSystemSettingsDB({"ollama_url": "http://already-there:11434"})   # url 行が既に存在
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists", db.migrate_marker_if_legacy_exists)
    api._seed_ollama_url_from_env()
    assert "ollama_allowlist" not in db.data
    assert api._OLLAMA_URL_SEED_MARKER_KEY in db.data   # ollama_url 自体は競合したがマーカーは確定


def test_seed_ollama_url_from_env_rejects_userinfo_and_does_not_confirm_marker(monkeypatch, caplog):
    """userinfo 付き（不正形式）の間はマーカーを一切確定しない
    （`store.seed_system_settings_once` 自体を呼ばない＝他に確定すべきキーが無いため）。"""
    monkeypatch.setenv("OLLAMA_URL", "http://admin:s3cr3t@ollama-central.internal:11434")
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_ollama_seed_recorder(monkeypatch)
    with caplog.at_level("WARNING"):
        api._seed_ollama_url_from_env()
    assert calls == []
    assert any("OLLAMA_URL" in r.message for r in caplog.records)


def test_seed_ollama_url_from_env_rejects_query(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://ollama-central.internal:11434/?token=x")
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_ollama_seed_recorder(monkeypatch)
    api._seed_ollama_url_from_env()
    assert calls == []


def test_seed_ollama_url_from_env_reevaluates_after_env_is_fixed(monkeypatch):
    """不正な `OLLAMA_URL` の間はマーカーが立たないため、env を直した**次回呼び出し**
    （次回起動・または healthz 再試行）で正しく再評価され、成功する。`_FakeSystemSettingsDB` で
    状態を呼び出しをまたいで持ち回り、1回目（不正）→2回目（修正後）の遷移を固定する。"""
    db = _FakeSystemSettingsDB({})
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists", db.migrate_marker_if_legacy_exists)

    monkeypatch.setenv("OLLAMA_URL", "http://admin:s3cr3t@ollama-central.internal:11434")
    api._seed_ollama_url_from_env()
    assert api._OLLAMA_URL_SEED_MARKER_KEY not in db.data
    assert "ollama_url" not in db.data

    monkeypatch.setenv("OLLAMA_URL", "http://ollama-central.internal:11434")   # env を修正
    api._seed_ollama_url_from_env()
    assert db.data[api._OLLAMA_URL_SEED_MARKER_KEY] == api._OLLAMA_URL_SEED_VERSION
    assert db.data["ollama_url"] == "http://ollama-central.internal:11434"
    assert "ollama-central.internal:11434" in (db.data.get("ollama_allowlist") or [])


def test_seed_settings_from_env_noop_when_marker_already_present(monkeypatch):
    """`credential_seed_version` があれば env を一切読まない＝`migrate_marker_if_legacy_exists`
    すら呼ばれない（安価な早期 return）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-would-be-seeded")
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {api._CREDENTIAL_SEED_MARKER_KEY: api._CREDENTIAL_SEED_VERSION})
    calls = _set_system_settings_recorder(monkeypatch)
    migrate_calls = []
    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists",
                        lambda *a, **kw: (migrate_calls.append(1), False)[-1])
    api._seed_settings_from_env()
    assert calls == []
    assert migrate_calls == []   # 早期 return のため呼ばれない


def test_seed_settings_from_env_does_not_revive_deleted_key_after_marker_set(monkeypatch):
    """管理者が中央キーを削除（system_settings の行削除）した後、古い env 値を残したまま
    再起動しても、`credential_seed_version` があれば復活しない（env は一切見ない）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-old-env-value-admin-deleted-in-ui")
    # マーカーは立っているが openai_api_key 自体は無い（＝admin が UI で削除した状態を模す）。
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {api._CREDENTIAL_SEED_MARKER_KEY: api._CREDENTIAL_SEED_VERSION})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_settings_from_env()
    assert calls == []   # 復活しない


def test_seed_settings_from_env_migrates_via_legacy_marker_without_reading_env(monkeypatch):
    """旧 `env_seed_version` がある環境（`credential_seed_version` 分離より前に
    一度でも起動済み）では、`store.migrate_marker_if_legacy_exists()` が True を返した時点で
    env を一切読まない＝`seed_system_settings_once`（候補構築・実際の書込み）は一度も呼ばれない。
    admin が資格情報を削除済みで残存 env が古い値のままでも復活しない。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-old-env-value-admin-deleted-in-ui")
    monkeypatch.setattr(store, "get_system_settings", lambda: {})   # credential_seed_version 未確認
    calls = _set_system_settings_recorder(monkeypatch)
    migrate_calls = []

    def _migrate(guard_key, legacy_key, guard_value=True):
        migrate_calls.append((guard_key, legacy_key, guard_value))
        return True   # 旧 env_seed_version が存在する環境を模す

    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists", _migrate)
    api._seed_settings_from_env()
    assert calls == []   # env は一切読まない（seed_system_settings_once は呼ばれない）
    assert migrate_calls == [
        (api._CREDENTIAL_SEED_MARKER_KEY, api._ENV_SEED_MARKER_KEY, api._CREDENTIAL_SEED_VERSION)]


def test_seed_ollama_url_from_env_migrates_via_legacy_marker_without_reading_env(monkeypatch):
    """`ollama_url` 側も同じ移行分岐を持つ。旧 `env_seed_version` がある環境
    （`ollama_url_seed_version` 分離より前に一度でも起動済み）では、admin が意図的に削除した
    `ollama_url`／`ollama_allowlist` を残存 env から復活・再認可しない（env・URL の形式チェックにも
    一切進まない＝形式が不正な値でも同じくスキップされる）。"""
    monkeypatch.setenv("OLLAMA_URL", "http://ollama-central.internal:11434")   # 残存 env（有効な形式）
    monkeypatch.setattr(store, "get_system_settings", lambda: {})   # ollama_url_seed_version 未確認
    calls = _set_ollama_seed_recorder(monkeypatch)
    migrate_calls = []

    def _migrate(guard_key, legacy_key, guard_value=True):
        migrate_calls.append((guard_key, legacy_key, guard_value))
        return True

    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists", _migrate)
    api._seed_ollama_url_from_env()
    assert calls == []   # ollama_url・ollama_allowlist・fingerprint とも一切書かない
    assert migrate_calls == [
        (api._OLLAMA_URL_SEED_MARKER_KEY, api._ENV_SEED_MARKER_KEY, api._OLLAMA_URL_SEED_VERSION)]


def test_seed_ollama_url_from_env_migrates_via_legacy_marker_even_with_malformed_env(monkeypatch):
    """レガシー環境の判定は OLLAMA_URL の形式チェックより優先する＝残存 env が不正な形式（userinfo
    混入等）でも、レガシー判定自体には影響しない（`migrate_marker_if_legacy_exists` が env を
    一切見ずに True を返す設計のため・形式チェックへ一切進まないことを確認する）。"""
    monkeypatch.setenv("OLLAMA_URL", "http://admin:s3cr3t@ollama-central.internal:11434")   # 不正形式
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_ollama_seed_recorder(monkeypatch)
    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists", lambda *a, **kw: True)
    api._seed_ollama_url_from_env()
    assert calls == []


def test_seed_ollama_url_from_env_legacy_migration_does_not_race_with_concurrent_admin_clear(monkeypatch):
    """「管理者 clear との競合テスト」。レガシー判定（`migrate_marker_if_legacy_exists`）から
    実際のマーカー確定 INSERT までの間に、admin が別トランザクションで `ollama_url`／
    `ollama_allowlist` を削除（clear）しても、移行分岐はこれらのキーへ一切触れない（ガードキー
    以外は読み書きしない設計）ため、admin の削除操作を巻き戻したり競合したりしない。"""
    db = _FakeSystemSettingsDB({
        "env_seed_version": api._ENV_SEED_VERSION,   # 旧統合シード済み環境
        "ollama_url": "http://central.internal:11434",
        "ollama_allowlist": ["central.internal:11434"],
    })
    monkeypatch.setenv("OLLAMA_URL", "http://central.internal:11434")   # 残存 env（admin 削除前の値）
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)

    real_migrate = db.migrate_marker_if_legacy_exists

    def _migrate_with_concurrent_admin_clear(guard_key, legacy_key, guard_value=True):
        # 「レガシー判定の最中に admin が別トランザクションで先に削除した」を模す
        # （呼び出しのたびに実行＝判定の前後どちらで割り込んでも同じ結果になることを示す）。
        db.data.pop("ollama_url", None)
        db.data.pop("ollama_allowlist", None)
        return real_migrate(guard_key, legacy_key, guard_value)

    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists", _migrate_with_concurrent_admin_clear)
    api._seed_ollama_url_from_env()
    assert "ollama_url" not in db.data   # admin の削除がそのまま残る（移行分岐が復活させない）
    assert "ollama_allowlist" not in db.data
    assert db.data[api._OLLAMA_URL_SEED_MARKER_KEY] == api._OLLAMA_URL_SEED_VERSION   # マーカーだけ確定


# ===== `api._confirm_legacy_env_seed_marker`（旧共有マーカーの rollback 互換確定） =====
# `credential_seed_version`／`ollama_url_seed_version` の**両方**が確定して初めて旧共有マーカー
# （`env_seed_version`）を書く（ロールバック時に旧コードが「未シード」と正しく再評価できるようにする
# ため・`api._seed_settings_from_env` の docstring 参照）。3状態（両方未確定／片方だけ確定／両方確定）
# を fake DB で固定する。

def test_confirm_legacy_env_seed_marker_noop_when_neither_new_marker_confirmed(monkeypatch):
    db = _FakeSystemSettingsDB({})   # 両方とも未確定
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    api._confirm_legacy_env_seed_marker()
    assert db.seed_calls == []
    assert api._ENV_SEED_MARKER_KEY not in db.data


def test_confirm_legacy_env_seed_marker_noop_when_only_credential_confirmed(monkeypatch):
    db = _FakeSystemSettingsDB({api._CREDENTIAL_SEED_MARKER_KEY: api._CREDENTIAL_SEED_VERSION})
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    api._confirm_legacy_env_seed_marker()
    assert db.seed_calls == []
    assert api._ENV_SEED_MARKER_KEY not in db.data


def test_confirm_legacy_env_seed_marker_noop_when_only_ollama_url_confirmed(monkeypatch):
    db = _FakeSystemSettingsDB({api._OLLAMA_URL_SEED_MARKER_KEY: api._OLLAMA_URL_SEED_VERSION})
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    api._confirm_legacy_env_seed_marker()
    assert db.seed_calls == []
    assert api._ENV_SEED_MARKER_KEY not in db.data


def test_confirm_legacy_env_seed_marker_confirms_when_both_new_markers_present(monkeypatch):
    db = _FakeSystemSettingsDB({
        api._CREDENTIAL_SEED_MARKER_KEY: api._CREDENTIAL_SEED_VERSION,
        api._OLLAMA_URL_SEED_MARKER_KEY: api._OLLAMA_URL_SEED_VERSION,
    })
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    api._confirm_legacy_env_seed_marker()
    assert db.data[api._ENV_SEED_MARKER_KEY] == api._ENV_SEED_VERSION
    assert len(db.seed_calls) == 1
    assert db.seed_calls[0]["guard_key"] == api._ENV_SEED_MARKER_KEY


def test_confirm_legacy_env_seed_marker_already_confirmed_does_not_rewrite(monkeypatch):
    """旧マーカーが既に確定済みなら、新マーカー2つの状態に関わらず何もしない（早期 return）。"""
    db = _FakeSystemSettingsDB({api._ENV_SEED_MARKER_KEY: api._ENV_SEED_VERSION})
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    api._confirm_legacy_env_seed_marker()
    assert db.seed_calls == []


def test_seed_settings_from_env_marks_done_even_when_nothing_to_seed(monkeypatch):
    """env に何も設定が無くても、初回はマーカーだけ立てて完了とする（以後の起動を高速化・env を
    二度と読まないことを構造的に保証する）。"""
    for name, _ in api._SEED_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SHERPA_PERSONAL_API_KEYS", raising=False)
    monkeypatch.delenv("SHERPA_ALLOW_WEB_SEARCH", raising=False)   # WEB-1: 未設定なら候補に含めない
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_settings_from_env()
    assert len(calls) == 1
    assert calls[0]["updates"] == {"credential_seed_version": api._CREDENTIAL_SEED_VERSION}


def test_seed_settings_from_env_ignores_placeholder_openai_key(monkeypatch):
    """`.env.example` のプレースホルダ（`sk-REPLACE_ME`）はシードしない（`is_real_api_key` と同じ判定）。
    マーカーは立つ（プレースホルダしか無くても評価自体は完了しているため）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-REPLACE_ME")
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_settings_from_env()
    assert len(calls) == 1
    assert "openai_api_key" not in calls[0]["updates"]
    assert calls[0]["updates"]["credential_seed_version"] == api._CREDENTIAL_SEED_VERSION


def test_seed_settings_from_env_bedrock_prefers_bearer_token_over_alias(monkeypatch):
    """AWS_BEARER_TOKEN_BEDROCK と ANTHROPIC_AWS_API_KEY が両方あれば前者を採用する。"""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bearer-value")
    monkeypatch.setenv("ANTHROPIC_AWS_API_KEY", "alias-value")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_settings_from_env()
    assert calls[0]["updates"]["bedrock_api_key"] == "bearer-value"


def test_seed_settings_from_env_personal_api_keys_truthy_strings(monkeypatch):
    """SHERPA_PERSONAL_API_KEYS は "1"/"true"/"yes"/"on"（大小無視）を真として初回シードする。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SHERPA_PERSONAL_API_KEYS", "TRUE")
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_settings_from_env()
    assert calls[0]["updates"]["personal_api_keys_allowed"] is True


# ===== WEB-1: SHERPA_ALLOW_WEB_SEARCH → system_settings.web_search_allowed の初回シード =====

def test_seed_settings_from_env_web_search_allowed_truthy_and_falsy_strings(monkeypatch):
    """SHERPA_ALLOW_WEB_SEARCH は "1"/"true"/"yes"/"on"（大小無視）を真、それ以外を偽として初回
    シードする（`sherpa.providers.codex.sandbox._web_search_admin_allowed` と同じ判定・
    `personal_api_keys_allowed` と同型）。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SHERPA_ALLOW_WEB_SEARCH", "TRUE")
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_settings_from_env()
    assert calls[0]["updates"]["web_search_allowed"] is True

    monkeypatch.setenv("SHERPA_ALLOW_WEB_SEARCH", "0")
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls2 = _set_system_settings_recorder(monkeypatch)
    api._seed_settings_from_env()
    assert calls2[0]["updates"]["web_search_allowed"] is False


def test_seed_settings_from_env_web_search_allowed_omitted_when_env_unset(monkeypatch):
    """SHERPA_ALLOW_WEB_SEARCH 未設定なら候補に含めない（`system_settings` 側は行が無い＝
    `_web_search_admin_allowed` の既定 false のまま・他キーのシード確定は妨げない）。"""
    monkeypatch.delenv("SHERPA_ALLOW_WEB_SEARCH", raising=False)
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_settings_from_env()
    assert "web_search_allowed" not in calls[0]["updates"]


def test_seed_settings_from_env_web_search_allowed_ignored_once_marker_present(monkeypatch):
    """初回シード後（`credential_seed_version` 確定後）は、env を "1" に変えても
    `web_search_allowed` は二度と読まれない（以後は管理画面の設定が唯一の真実源）。"""
    monkeypatch.setenv("SHERPA_ALLOW_WEB_SEARCH", "1")
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {api._CREDENTIAL_SEED_MARKER_KEY: api._CREDENTIAL_SEED_VERSION})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_settings_from_env()
    assert calls == []   # 早期 return（env は一切読まない）


def test_seed_settings_from_env_aggregates_mismatch_warnings_into_one_line(monkeypatch, caplog):
    """DB に値があり env も設定されていて複数キーが食い違う場合、無視される旨の警告は**1行に集約**
    する（キーごとに複数行出さない）。マーカーは立ち、DB の値は上書きされない。"""
    import logging
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-value")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-env-value")
    monkeypatch.delenv("SHERPA_ALLOW_WEB_SEARCH", raising=False)   # WEB-1: 未設定なら候補に含めない
    db = _FakeSystemSettingsDB({"openai_api_key": "sk-db-value", "gemini_api_key": "gemini-db-value"})
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists", db.migrate_marker_if_legacy_exists)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        api._seed_settings_from_env()
    assert db.data == {  # DB 値は上書きしない
        "openai_api_key": "sk-db-value", "gemini_api_key": "gemini-db-value",
        "credential_seed_version": api._CREDENTIAL_SEED_VERSION,
    }
    warn_records = [r for r in caplog.records if "無視されます" in r.getMessage()]
    assert len(warn_records) == 1   # 1行に集約
    msg = warn_records[0].getMessage()
    assert "OPENAI_API_KEY" in msg and "GEMINI_API_KEY" in msg


def test_seed_settings_from_env_survives_db_unreachable_and_does_not_mark_seeded(monkeypatch):
    """DB 不達（例外）でも起動は止めない（warning ログのみ）。マーカーも立てない
    （`store.seed_system_settings_once` を一切呼ばない＝次回起動時に再試行できる）。"""
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "get_system_settings", _boom)
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_settings_from_env()   # 例外を投げなければ OK
    assert calls == []


def test_seed_settings_does_not_clobber_admin_write_that_races_with_the_check(monkeypatch):
    """マーカー確認（`get_system_settings`）と実際の書込み（`seed_system_settings_once`）の間に
    管理者が先に同じキーを設定しても、その値を env の値で上書きしない（両者が別呼び出しでも
    実際の書込み側の不変条件が保証する）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-value")
    db = _FakeSystemSettingsDB()   # 初期状態: 未シード・openai_api_key 無し

    def _racy_get():
        # 「マーカー確認」が読んだ直後に、管理者が別トランザクションで先にキーを入れる、という
        # レースを模す（本関数の最初の呼び出し時だけ割り込ませる）。
        snap = db.get_system_settings()
        db.data.setdefault("openai_api_key", "sk-admin-value")
        return snap

    monkeypatch.setattr(store, "get_system_settings", _racy_get)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists", db.migrate_marker_if_legacy_exists)
    api._seed_settings_from_env()
    assert db.data["openai_api_key"] == "sk-admin-value"   # env 値で上書きされていない


def test_seed_settings_does_not_revive_key_deleted_after_marker_even_when_precheck_is_stale(monkeypatch):
    """マーカー（`credential_seed_version` 相当）は既に存在する（＝シード完了済み）が、事前チェック
    （`get_system_settings`）が古い「マーカー無し」を返す状況（キャッシュ経由等）でも、実際の
    書込みは「今」のマーカー有無を見るため、管理者が削除した特定のキー（openai_api_key）が env
    から再挿入されない。マーカーの有無だけを見て個々のキーの ON CONFLICT に任せていた旧設計では、
    マーカーが既にあってもこのキーだけは復活してしまっていた。

    是正後は「旧統合マーカー（`env_seed_version`）が既にある」ケースがこれに該当する
    （`credential_seed_version` はまだ無いので事前チェック自体は `credential_seed_version` を
    見て「未シード」と判定するが、`migrate_marker_if_legacy_exists()` が
    `env_seed_version`（DB の実データ・precheck とは別の直接クエリ）をフレッシュに見て復活を防ぐ）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-old-env-value-admin-deleted-in-ui")
    # 実態: 旧統合マーカーは存在するが、admin が openai_api_key を削除済み（行が無い）。
    db = _FakeSystemSettingsDB({"env_seed_version": api._ENV_SEED_VERSION})
    # 事前チェックだけが古い情報（マーカー無し）を返す状況を模す（credential_seed_version の視点では
    # 未シードに見える）。
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists", db.migrate_marker_if_legacy_exists)
    api._seed_settings_from_env()
    assert "openai_api_key" not in db.data   # 復活しない
    assert db.data[api._CREDENTIAL_SEED_MARKER_KEY] == api._CREDENTIAL_SEED_VERSION   # 移行済みとして確定


def test_catchup_v2_not_invoked_when_marker_already_present(monkeypatch):
    """v2 marker が既にあれば `store.catchup_ollama_allowlist_for_env_seeded_url_v2` は呼ばない
    （安価な早期 return・正しさの根拠は store 側の marker 確認だが、無駄な監査ログ問い合わせを
    避ける）。中央URLが既に allowlist にあるため警告も出ない。"""
    db = _FakeSystemSettingsDB({
        "ollama_url": "http://central.internal:11434",
        "ollama_allowlist": ["central.internal:11434"],
        "ollama_allowlist_env_seed_catchup_v2": 1,
    })
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "catchup_ollama_allowlist_for_env_seeded_url_v2",
                        db.catchup_ollama_allowlist_for_env_seeded_url_v2)
    api._catchup_ollama_allowlist_for_central_url()
    assert db.catchup_calls == []


def test_catchup_v2_invoked_once_when_marker_absent_and_adds_host(monkeypatch):
    """marker が無ければ一度だけ v2 を呼ぶ。`added` の結果は allowlist へ反映される。"""
    db = _FakeSystemSettingsDB(
        {"ollama_url": "http://central.internal:11434"},
        catchup_v2_reason="added", catchup_v2_host="central.internal:11434")
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "catchup_ollama_allowlist_for_env_seeded_url_v2",
                        db.catchup_ollama_allowlist_for_env_seeded_url_v2)
    api._catchup_ollama_allowlist_for_central_url()
    assert len(db.catchup_calls) == 1
    assert db.data["ollama_allowlist"] == ["central.internal:11434"]


def test_catchup_v2_skipped_unproven_logs_persistent_warning(monkeypatch, caplog):
    """重大バグ是正（RV 4巡目 #1・簡素化裁定）: fail-closed で追加できなかった（`skipped_unproven`）
    場合、自動修復はしないが、中央URLが非loopbackでallowlistに無ければ healthz のたびに
    1行警告する（管理者への手動修復の誘導）。"""
    import logging

    db = _FakeSystemSettingsDB(
        {"ollama_url": "http://central.internal:11434"}, catchup_v2_reason="skipped_unproven")
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "catchup_ollama_allowlist_for_env_seeded_url_v2",
                        db.catchup_ollama_allowlist_for_env_seeded_url_v2)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        api._catchup_ollama_allowlist_for_central_url()
    assert "ollama_allowlist" not in db.data
    assert any("central.internal:11434" in r.getMessage() for r in caplog.records)


def test_catchup_v2_warning_persists_across_calls_after_marker_set(monkeypatch, caplog):
    """marker 確定後（v2 を二度と呼ばない状態）でも、中央URLが allowlist に無い限り、健全性確認の
    たびに警告し続ける（自動修復しない代わりに気づけるようにする・簡素化裁定）。"""
    import logging

    db = _FakeSystemSettingsDB({
        "ollama_url": "http://central.internal:11434",
        "ollama_allowlist_env_seed_catchup_v2": 1,   # 既に評価済み・追加されなかった状態
    })
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "catchup_ollama_allowlist_for_env_seeded_url_v2",
                        db.catchup_ollama_allowlist_for_env_seeded_url_v2)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        api._catchup_ollama_allowlist_for_central_url()
    assert db.catchup_calls == []   # v2 は呼ばれない
    assert any("central.internal:11434" in r.getMessage() for r in caplog.records)


def test_catchup_v2_no_warning_for_loopback_central_url(monkeypatch, caplog):
    import logging

    db = _FakeSystemSettingsDB(
        {"ollama_url": "http://localhost:11434"}, catchup_v2_reason="skipped_unproven")
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "catchup_ollama_allowlist_for_env_seeded_url_v2",
                        db.catchup_ollama_allowlist_for_env_seeded_url_v2)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        api._catchup_ollama_allowlist_for_central_url()
    assert not any("許可一覧にありません" in r.getMessage() for r in caplog.records)


def test_healthz_retries_seed_on_schema_readiness_recovery(monkeypatch):
    """DB 不達で未 ready → readiness 回復（`store.init_schema()` 成功）の瞬間に、healthz が
    `_seed_settings_from_env()` を再試行する（DB 不達時はマーカーを付けず、readiness 回復時に
    再試行するという契約）。healthz は同じ single-flight の枠で他の起動時シード処理（OLLAMA_URL・
    旧マーカー互換確定・ollama allowlist 追いつき・openai_endpoint・model_catalog）も呼ぶが、この
    テストは env シードの再試行契約だけを見るため、それらは全て no-op に差し替える（別テストで
    両シードの独立性を固定する・`test_healthz_retries_both_seeds_independently_with_separate_markers`）。
    差し替えないと、この関数の焦点外の処理が実テスト DB へマーカーを書き込んだり
    （`_seed_openai_endpoint_from_env` 等が）プロセス内の openai I/O ブロック状態を変えたりし得る。"""
    from sherpa.routers import system as system_router

    ready_calls = iter([False, True])   # 1回目=未 ready（init_schema 実行前）・以後 ready
    monkeypatch.setattr(store, "schema_ready", lambda: next(ready_calls, True))
    monkeypatch.setattr(store, "init_schema", lambda: None)
    monkeypatch.setattr(model_catalog, "seed_catalog_once", lambda: None)
    monkeypatch.setattr(api, "_seed_ollama_url_from_env", lambda: None)
    monkeypatch.setattr(api, "_confirm_legacy_env_seed_marker", lambda: None)
    monkeypatch.setattr(api, "_catchup_ollama_allowlist_for_central_url", lambda: None)
    monkeypatch.setattr(api, "_seed_openai_endpoint_from_env", lambda: None)
    monkeypatch.setattr(api, "_seed_depth_profile_from_env", lambda: None)
    seed_calls = []
    monkeypatch.setattr(api, "_seed_settings_from_env", lambda: seed_calls.append(True))
    system_router.healthz()
    assert seed_calls == [True]


def test_healthz_retries_seed_when_already_ready_not_just_on_transition(monkeypatch):
    """schema が既に ready のまま（今回の呼び出しで新たに ready になったわけではない）場合でも、
    healthz は呼び出しのたびにシードを再試行する（schema-ready への遷移の瞬間だけに限らない・
    シードだけが一時的に失敗しても次の呼び出しで再試行される）。同じ single-flight の枠で呼ばれる
    他の起動時シード処理（`test_healthz_retries_seed_on_schema_readiness_recovery` 参照）は全て
    no-op に差し替える（env シードの再試行契約だけを見るテストのため・実 DB への焦点外の書込みを
    避ける）。"""
    from sherpa.routers import system as system_router

    monkeypatch.setattr(store, "schema_ready", lambda: True)   # 遷移ではなく常に ready
    monkeypatch.setattr(model_catalog, "seed_catalog_once", lambda: None)
    monkeypatch.setattr(api, "_seed_ollama_url_from_env", lambda: None)
    monkeypatch.setattr(api, "_confirm_legacy_env_seed_marker", lambda: None)
    monkeypatch.setattr(api, "_catchup_ollama_allowlist_for_central_url", lambda: None)
    monkeypatch.setattr(api, "_seed_openai_endpoint_from_env", lambda: None)
    monkeypatch.setattr(api, "_seed_depth_profile_from_env", lambda: None)
    seed_calls = []
    monkeypatch.setattr(api, "_seed_settings_from_env", lambda: seed_calls.append(True))
    system_router.healthz()
    system_router.healthz()
    assert seed_calls == [True, True]   # 冪等なので毎回呼んで良い＝呼び出しのたびに再試行される


def test_healthz_retries_seed_after_transient_seed_failure_and_succeeds_once(monkeypatch):
    """シードだけが一時的に失敗（DB 瞬断等・schema 自体は ready のまま）しても、次の healthz
    呼び出しで再試行され、最終的に1回だけ実際にシードされる。`model_catalog.seed_catalog_once()`
    ・`api._seed_ollama_url_from_env()`・`api._confirm_legacy_env_seed_marker()`・
    `api._catchup_ollama_allowlist_for_central_url()`・`api._seed_openai_endpoint_from_env()` は
    no-op に差し替える（同じ `_flaky_get`/`db` を共有すると、そちらの呼び出しが
    `db.seed_calls`/失敗回数のカウントに混ざり、本テストの意図（資格情報シード単体の再試行）を
    ずらしてしまうため）。"""
    from sherpa.routers import system as system_router

    monkeypatch.setenv("OPENAI_API_KEY", "sk-seed-openai")
    monkeypatch.setattr(store, "schema_ready", lambda: True)
    monkeypatch.setattr(model_catalog, "seed_catalog_once", lambda: None)
    monkeypatch.setattr(api, "_seed_ollama_url_from_env", lambda: None)
    monkeypatch.setattr(api, "_confirm_legacy_env_seed_marker", lambda: None)
    monkeypatch.setattr(api, "_catchup_ollama_allowlist_for_central_url", lambda: None)
    monkeypatch.setattr(api, "_seed_openai_endpoint_from_env", lambda: None)
    monkeypatch.setattr(api, "_seed_depth_profile_from_env", lambda: None)
    db = _FakeSystemSettingsDB()
    state = {"n": 0}

    def _flaky_get():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient db blip")
        return db.get_system_settings()

    monkeypatch.setattr(store, "get_system_settings", _flaky_get)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)

    system_router.healthz()   # 1回目: get_system_settings が例外→シード失敗（healthz は落ちない）
    assert "credential_seed_version" not in db.data

    system_router.healthz()   # 2回目: 再試行して成功
    assert db.data["credential_seed_version"] == api._CREDENTIAL_SEED_VERSION
    assert db.data["openai_api_key"] == "sk-seed-openai"
    assert len(db.seed_calls) == 1   # 実際に書いたのは1回だけ


def test_healthz_retries_both_seeds_independently_with_separate_markers(monkeypatch):
    """healthz は資格情報シード（`api._seed_settings_from_env`）と model_catalog シード
    （`model_catalog.seed_catalog_once`）を同じ枠で呼ぶが、両者は独立したマーカー
    （`credential_seed_version`／`model_catalog_seed_version`）を持つ別々の呼び出しであり、互いを
    上書き・スキップさせない。1回目の healthz で両方が実際に書き込み、2回目は両方ともマーカー
    済みのため書き込みが増えない（重複シードしない）ことを固定する。`ollama_allowlist` 追いつき
    移行（`_catchup_ollama_allowlist_for_central_url`）・`api._seed_openai_endpoint_from_env`
    （SET-2c・独立した第3のマーカー・別テストで単体検証する）は本テストの焦点（この2つのマーカー
    の独立性）と無関係なため no-op に差し替える（`api._seed_ollama_url_from_env`／
    `api._confirm_legacy_env_seed_marker` も同様＝どちらも本テストが検証する2マーカーとは別の
    マーカーを扱う・専用テスト群で単体検証する）。"""
    from sherpa.routers import system as system_router

    monkeypatch.setattr(store, "schema_ready", lambda: True)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_EMBED_MODEL", raising=False)
    monkeypatch.setattr(api, "_seed_ollama_url_from_env", lambda: None)
    monkeypatch.setattr(api, "_confirm_legacy_env_seed_marker", lambda: None)
    monkeypatch.setattr(api, "_catchup_ollama_allowlist_for_central_url", lambda: None)
    monkeypatch.setattr(api, "_seed_openai_endpoint_from_env", lambda: None)
    monkeypatch.setattr(api, "_seed_depth_profile_from_env", lambda: None)
    db = _FakeSystemSettingsDB()
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)

    system_router.healthz()
    assert db.data.get("credential_seed_version") == api._CREDENTIAL_SEED_VERSION
    assert db.data.get("model_catalog_seed_version") == model_catalog._CATALOG_SEED_VERSION
    assert "model_catalog" in db.data
    assert len(db.seed_calls) == 2   # 資格情報分・model_catalog 分の各1回

    system_router.healthz()   # 2回目: 両方ともマーカー済み＝どちらも再書込みしない
    assert len(db.seed_calls) == 2


def test_healthz_model_catalog_seed_retries_after_transient_failure_independent_of_env_seed(monkeypatch):
    """model_catalog シードだけが一時的に失敗しても、次の healthz 呼び出しで再試行され、最終的に
    1回だけ実際にシードされる（`test_healthz_retries_seed_after_transient_seed_failure_and_succeeds_once`
    の env シード版と対の検証・env シード側／OLLAMA_URL シード／env_seed_version 互換確定／
    ollama_allowlist 追いつき移行／openai_endpoint シード（SET-2c）は no-op に差し替えて分離する）。"""
    from sherpa.routers import system as system_router

    monkeypatch.setattr(store, "schema_ready", lambda: True)
    monkeypatch.setattr(api, "_seed_settings_from_env", lambda: None)
    monkeypatch.setattr(api, "_seed_ollama_url_from_env", lambda: None)
    monkeypatch.setattr(api, "_confirm_legacy_env_seed_marker", lambda: None)
    monkeypatch.setattr(api, "_catchup_ollama_allowlist_for_central_url", lambda: None)
    monkeypatch.setattr(api, "_seed_openai_endpoint_from_env", lambda: None)
    monkeypatch.setattr(api, "_seed_depth_profile_from_env", lambda: None)
    db = _FakeSystemSettingsDB()
    state = {"n": 0}

    def _flaky_get():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient db blip")
        return db.get_system_settings()

    monkeypatch.setattr(store, "get_system_settings", _flaky_get)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)

    system_router.healthz()   # 1回目: get_system_settings が例外→シード失敗（healthz は落ちない）
    assert "model_catalog_seed_version" not in db.data

    system_router.healthz()   # 2回目: 再試行して成功
    assert db.data["model_catalog_seed_version"] == model_catalog._CATALOG_SEED_VERSION
    assert len(db.seed_calls) == 1   # 実際に書いたのは1回だけ


def test_healthz_openai_endpoint_seed_retries_after_transient_failure_independent_of_others(monkeypatch):
    """SET-2c: `api._seed_openai_endpoint_from_env`（第3の独立マーカー
    `openai_endpoint_seed_version`）だけが一時的に失敗しても、次の healthz 呼び出しで再試行され、
    最終的に1回だけ実際にシードされる（env シード／OLLAMA_URL シード／env_seed_version 互換確定／
    model_catalog シードは no-op に差し替えて分離する・上記2テストと対の検証）。"""
    from sherpa.routers import system as system_router

    monkeypatch.setattr(store, "schema_ready", lambda: True)
    monkeypatch.setattr(api, "_seed_settings_from_env", lambda: None)
    monkeypatch.setattr(api, "_seed_ollama_url_from_env", lambda: None)
    monkeypatch.setattr(api, "_confirm_legacy_env_seed_marker", lambda: None)
    monkeypatch.setattr(api, "_catchup_ollama_allowlist_for_central_url", lambda: None)
    monkeypatch.setattr(api, "_seed_depth_profile_from_env", lambda: None)
    monkeypatch.setattr(model_catalog, "seed_catalog_once", lambda: None)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    db = _FakeSystemSettingsDB()
    state = {"n": 0}

    def _flaky_get():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient db blip")
        return db.get_system_settings()

    monkeypatch.setattr(store, "get_system_settings", _flaky_get)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)

    system_router.healthz()   # 1回目: get_system_settings が例外→シード失敗（healthz は落ちない）
    assert "openai_endpoint_seed_version" not in db.data

    system_router.healthz()   # 2回目: 再試行して成功（OPENAI_BASE_URL 未設定＝何も取り込まないが
    # 完了マーカー自体は書く）
    assert db.data["openai_endpoint_seed_version"] == api._OPENAI_ENDPOINT_SEED_VERSION
    assert len(db.seed_calls) == 1   # 実際に書いたのは1回だけ


def test_healthz_ollama_url_seed_reevaluates_after_env_fixed_independent_of_others(monkeypatch):
    """healthz 経由でも OLLAMA_URL の独立マーカー（`ollama_url_seed_version`）が正しく
    配線されている（不正な間はマーカーが立たず、env を直した次の healthz 呼び出しで再評価される・
    他の3つのシード処理は no-op に差し替えて分離する）。"""
    from sherpa.routers import system as system_router

    monkeypatch.setattr(store, "schema_ready", lambda: True)
    monkeypatch.setattr(api, "_seed_settings_from_env", lambda: None)
    monkeypatch.setattr(api, "_catchup_ollama_allowlist_for_central_url", lambda: None)
    monkeypatch.setattr(api, "_seed_openai_endpoint_from_env", lambda: None)
    monkeypatch.setattr(api, "_seed_depth_profile_from_env", lambda: None)
    monkeypatch.setattr(model_catalog, "seed_catalog_once", lambda: None)
    db = _FakeSystemSettingsDB()
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    monkeypatch.setattr(store, "migrate_marker_if_legacy_exists", db.migrate_marker_if_legacy_exists)

    monkeypatch.setenv("OLLAMA_URL", "http://admin:s3cr3t@ollama-central.internal:11434")   # 不正形式
    system_router.healthz()
    assert api._OLLAMA_URL_SEED_MARKER_KEY not in db.data

    monkeypatch.setenv("OLLAMA_URL", "http://ollama-central.internal:11434")   # env を修正
    system_router.healthz()
    assert db.data[api._OLLAMA_URL_SEED_MARKER_KEY] == api._OLLAMA_URL_SEED_VERSION
    assert db.data["ollama_url"] == "http://ollama-central.internal:11434"


# ---- SET-2c: `_openai_endpoint_seed_candidate` の原子性・明示 kind 優先 ----

def test_openai_endpoint_seed_candidate_empty_when_env_unset(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("SHERPA_OPENAI_ENDPOINT_KIND", raising=False)
    monkeypatch.delenv("SHERPA_OPENAI_AUTH_HEADER", raising=False)
    monkeypatch.delenv("SHERPA_OPENAI_API_VERSION", raising=False)
    assert api._openai_endpoint_seed_candidate() == {}


def test_openai_endpoint_seed_candidate_defers_host_inference_to_read_time(monkeypatch):
    """kind 未指定なら host 推定した値を候補へ**書き込まない**（`openai_base_url` だけを候補に
    含め、`openai_endpoint_kind` は含めない）。推定は `llm.openai_endpoint_kind()` の読み取り時
    フォールバックに委ねる＝将来 host 判定が改善されても、既に確定した候補に遡って影響しない。"""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://myres.openai.azure.com/openai/v1")
    monkeypatch.delenv("SHERPA_OPENAI_ENDPOINT_KIND", raising=False)
    candidate = api._openai_endpoint_seed_candidate()
    assert candidate == {"openai_base_url": "https://myres.openai.azure.com/openai/v1"}


def test_openai_endpoint_seed_candidate_explicit_kind_openai_overrides_azure_host(monkeypatch):
    """host が Azure でも `SHERPA_OPENAI_ENDPOINT_KIND=openai` の明示があれば、その値を
    そのまま候補にする（「明示値→未指定時のみ host 推定」の順序）。"""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://myres.openai.azure.com/openai/v1")
    monkeypatch.setenv("SHERPA_OPENAI_ENDPOINT_KIND", "openai")
    candidate = api._openai_endpoint_seed_candidate()
    assert candidate["openai_endpoint_kind"] == "openai"
    assert candidate["openai_base_url"] == "https://myres.openai.azure.com/openai/v1"


def test_openai_endpoint_seed_candidate_explicit_kind_custom_accepted(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gw.example.com/v1")
    monkeypatch.setenv("SHERPA_OPENAI_ENDPOINT_KIND", "custom")
    candidate = api._openai_endpoint_seed_candidate()
    assert candidate["openai_endpoint_kind"] == "custom"


def test_openai_endpoint_seed_candidate_rejects_unknown_kind(monkeypatch):
    """生の env 値は例外文言に含めない（固定 reason code
    `invalid_endpoint_kind` のみ）＝この文言はそのまま `set_openai_endpoint_seed_blocked` の
    理由としてプロセス内に残り続け、healthz/接続テストのエラー詳細経由で外へ出うるため。"""
    monkeypatch.setenv("SHERPA_OPENAI_ENDPOINT_KIND", "bogus-value-should-not-leak")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with pytest.raises(ValueError) as exc:
        api._openai_endpoint_seed_candidate()
    assert "invalid_endpoint_kind" in str(exc.value)
    assert "bogus-value-should-not-leak" not in str(exc.value)


def test_openai_endpoint_seed_candidate_rejects_kind_without_base_url(monkeypatch):
    """明示 kind が openai 以外なのに base_url が無ければ候補全体を拒否する
    （kind だけ確定して実際は本家へ縮退する食い違いを防ぐ）。"""
    monkeypatch.setenv("SHERPA_OPENAI_ENDPOINT_KIND", "azure")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with pytest.raises(ValueError):
        api._openai_endpoint_seed_candidate()


def test_openai_endpoint_seed_candidate_rejects_invalid_base_url_entirely(monkeypatch):
    """base URL が不正なら auth_header/api_version が有効でも候補全体を無効にする
    （base だけを無視して他の項目を確定させることはしない＝一貫性のある候補だけを受理する）。"""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://myres.openai.azure.com/openai/v1")   # https 以外は拒否
    monkeypatch.setenv("SHERPA_OPENAI_AUTH_HEADER", "api-key")
    monkeypatch.setenv("SHERPA_OPENAI_API_VERSION", "2024-10-21")
    with pytest.raises(ValueError):
        api._openai_endpoint_seed_candidate()


def test_openai_endpoint_seed_candidate_rejects_unknown_auth_header(monkeypatch):
    """固定 reason code `invalid_auth_header` のみ・生の env 値は含めない。"""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("SHERPA_OPENAI_AUTH_HEADER", "bogus-value-should-not-leak")
    with pytest.raises(ValueError) as exc:
        api._openai_endpoint_seed_candidate()
    assert "invalid_auth_header" in str(exc.value)
    assert "bogus-value-should-not-leak" not in str(exc.value)


def test_seed_openai_endpoint_from_env_does_not_mark_done_on_invalid_candidate(monkeypatch):
    """不正な候補（base URL 不正）は完了マーカーを立てない＝env を直せば次回起動で再試行される
    （候補全体が無効なときにマーカーだけ確定させて再試行の機会を失わせることはしない）。"""
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", None)   # 他テストへ漏らさない
    monkeypatch.setattr(store, "schema_ready", lambda: True)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://myres.openai.azure.com/openai/v1")
    monkeypatch.setenv("SHERPA_OPENAI_AUTH_HEADER", "api-key")
    db = _FakeSystemSettingsDB()
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)

    api._seed_openai_endpoint_from_env()

    assert "openai_endpoint_seed_version" not in db.data
    assert "openai_auth_header" not in db.data
    assert "openai_base_url" not in db.data
    assert db.seed_calls == []   # store.seed_system_settings_once 自体を一度も呼ばない

    # env を直せば次回（再試行）で取り込まれる。
    monkeypatch.setenv("OPENAI_BASE_URL", "https://myres.openai.azure.com/openai/v1")
    api._seed_openai_endpoint_from_env()
    assert db.data["openai_endpoint_seed_version"] == api._OPENAI_ENDPOINT_SEED_VERSION
    assert db.data["openai_base_url"] == "https://myres.openai.azure.com/openai/v1"
    assert db.data["openai_auth_header"] == "api-key"


def test_seed_openai_endpoint_from_env_blocks_openai_io_while_candidate_invalid(monkeypatch):
    """不正な候補は「正当な OpenAI 本家既定」と区別し、確定するまで OpenAI 系
    I/O を fail-closed にする（DB 上は未設定＝本家既定と見分けが付かないため、プロセス内フラグで
    ブロックする・`sherpa/llm.py::set_openai_endpoint_seed_blocked` 参照）。env を直して再試行が
    成功すれば解除される（DB 一時障害はこの経路を通らず、ブロックも立たない＝別状態）。
    """
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", None)
    monkeypatch.setattr(store, "schema_ready", lambda: True)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://myres.openai.azure.com/openai/v1")   # http は不許可（不正）
    db = _FakeSystemSettingsDB()
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)

    assert llm.openai_endpoint_seed_blocked_reason() is None   # 前提: 未ブロック
    api._seed_openai_endpoint_from_env()
    assert llm.openai_endpoint_seed_blocked_reason() is not None   # 不正確定＝ブロックが立つ

    # OpenAI 系 I/O のチョークポイント（openai_url/openai_headers）が実際に拒否する（キー送信を止める）。
    with pytest.raises(RuntimeError):
        llm.openai_url("chat/completions")
    with pytest.raises(RuntimeError):
        llm.openai_headers("sk-dummy")

    # DB 一時障害はブロックを立てない（別状態＝次回再試行を待てばよい・本テストでは既存ブロックを
    # 巻き込まないよう先に解除してから検証する）。
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", None)
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    api._seed_openai_endpoint_from_env()
    assert llm.openai_endpoint_seed_blocked_reason() is None

    # env を直せば次回再試行でブロックが解除され、OpenAI 系 I/O も再び通る。
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://myres.openai.azure.com/openai/v1")
    api._seed_openai_endpoint_from_env()
    assert llm.openai_endpoint_seed_blocked_reason() is None
    llm.openai_url("chat/completions")   # 例外を出さない


def test_seed_openai_endpoint_from_env_unblocks_when_marker_already_confirmed(monkeypatch):
    """マーカーが既に確定している（このプロセスが確定させたのでなくても＝
    他プロセス/手動 DB 修正等いずれの経路でも）のに、このプロセスのブロックフラグだけが残っている
    場合、早期 return の前に検知して解除する（マーカー確定を見逃して blocked のまま
    再起動まで固定されることを防ぐ）。
    """
    monkeypatch.setattr(store, "schema_ready", lambda: True)
    # マーカーは最初から確定済み（このプロセスが今回書いたのではない状態を模す）。
    db = _FakeSystemSettingsDB({api._OPENAI_ENDPOINT_SEED_MARKER_KEY: api._OPENAI_ENDPOINT_SEED_VERSION,
                                "openai_base_url": "https://myres.openai.azure.com/openai/v1"})
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    # このプロセスは（別の理由で）blocked のまま残っている前提。
    monkeypatch.setattr(llm, "_openai_endpoint_seed_blocked_reason", "旧試行で不正だった名残")

    api._seed_openai_endpoint_from_env()

    assert llm.openai_endpoint_seed_blocked_reason() is None, "マーカー確定済みなのにブロックが残っている"
    assert db.seed_calls == [], "マーカーが既にあるのに再度 seed_system_settings_once を呼んでいる"
    llm.openai_url("chat/completions")   # 例外を出さない（解除されている）


# ===== 調べる深さの基準値7項目・env→system_settings 初回シード（`api._seed_depth_profile_from_env`） =====
# SC-6c（調べ方ブロック §3.2）。`_seed_openai_endpoint_from_env`／`model_catalog.seed_catalog_once`
# と同じ「一度だけ」方式（独立マーカー `depth_profile_seed_version`）。候補値は各モジュールの既存
# env 定数を複製するだけで、数値6項目の妥当性検証（openai_endpoint のような fail-closed ブロック）は
# 不要——各モジュールの起動時に既に検証済みの値のため常に有効。例外は Codex 推論（自由文字列）＝
# 語彙検証し不正なら何も書かない（下の invalid/normalize テスト参照）。

def test_seed_depth_profile_from_env_writes_all_seven_keys_and_marker_in_one_call(monkeypatch):
    """未シード状態で、7項目とマーカーを同一の `seed_system_settings_once` 呼び出しで書く。
    値は各モジュールの既存 env 定数そのもの（ここでは env を再読しない）。"""
    from sherpa import agentic_search, chat_service, impact_service, lens_service
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_depth_profile_from_env()
    assert len(calls) == 1
    assert calls[0]["guard_key"] == api._DEPTH_PROFILE_SEED_MARKER_KEY
    assert calls[0]["updates"] == {
        "depth_base_max_turns": agentic_search.MAX_TURNS,
        "depth_base_grep_max_hits": agentic_search.MAX_HITS,
        "depth_base_qa_max_hits": chat_service.QA_MAX_HITS_DEFAULT,
        "depth_base_read_window": agentic_search.READ_WINDOW,
        "depth_base_impact_depth": impact_service.IMPACT_MAX_DEPTH,
        "depth_base_troubleshoot_depth": lens_service.TROUBLESHOOT_GRAPH_DEPTH,
        "depth_base_codex_reasoning": os.environ.get("SHERPA_CODEX_REASONING", "low").strip().lower(),
        api._DEPTH_PROFILE_SEED_MARKER_KEY: api._DEPTH_PROFILE_SEED_VERSION,
    }


def test_seed_depth_profile_from_env_noop_when_marker_already_present(monkeypatch):
    """マーカーがあれば `seed_system_settings_once` を一切呼ばない（安価な早期 return）。"""
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {api._DEPTH_PROFILE_SEED_MARKER_KEY: api._DEPTH_PROFILE_SEED_VERSION})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_depth_profile_from_env()
    assert calls == []


def test_seed_depth_profile_from_env_does_not_revive_deleted_key_after_marker_set(monkeypatch):
    """管理者が基準値（例: depth_base_max_turns）を admin-settings.html で削除した後、マーカーが
    立っていれば env 既定値から復活しない。"""
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {api._DEPTH_PROFILE_SEED_MARKER_KEY: api._DEPTH_PROFILE_SEED_VERSION})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_depth_profile_from_env()
    assert calls == []


def test_seed_depth_profile_from_env_does_not_overwrite_existing_admin_value(monkeypatch):
    """マーカー確定前でも、既に個別に保存済みの管理値（`depth_base_max_turns` 等）を env 由来の
    候補で上書きしない（実 DB の `WHERE NOT EXISTS` 意味論を `_FakeSystemSettingsDB` で固定）。"""
    db = _FakeSystemSettingsDB({"depth_base_max_turns": 99})
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)
    api._seed_depth_profile_from_env()
    assert db.data["depth_base_max_turns"] == 99   # 既存の管理値は上書きされない
    assert db.data[api._DEPTH_PROFILE_SEED_MARKER_KEY] == api._DEPTH_PROFILE_SEED_VERSION   # マーカーは確定
    assert db.data["depth_base_grep_max_hits"] is not None   # 他の6項目は通常どおりシードされる


def test_seed_depth_profile_from_env_survives_db_unreachable_and_does_not_mark_seeded(monkeypatch):
    """DB 不達（例外）でも起動は止めない（warning ログのみ）。マーカーも立てない
    （次回起動、または `healthz()` の再試行で再試行される）。"""
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "get_system_settings", _boom)
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_depth_profile_from_env()   # 例外を投げなければ OK
    assert calls == []


def test_seed_depth_profile_from_env_rejects_unknown_codex_reasoning_and_writes_nothing(monkeypatch, caplog):
    """`SHERPA_CODEX_REASONING` が既知語彙以外なら、7項目・マーカーとも一切書かない
    （不正値を一回性マーカー付きで永続化すると env 修正後も自動回復しなくなるため）。
    エラーログを出す（見送ったことが起動ログから分かるように）。"""
    monkeypatch.setenv("SHERPA_CODEX_REASONING", "ultra")
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_system_settings_recorder(monkeypatch)
    with caplog.at_level("ERROR", logger="sherpa"):
        api._seed_depth_profile_from_env()
    assert calls == []   # seed_system_settings_once 自体を一切呼ばない
    assert any("SHERPA_CODEX_REASONING" in r.message for r in caplog.records)


def test_seed_depth_profile_from_env_normalizes_codex_reasoning_case_and_whitespace(monkeypatch):
    """`SHERPA_CODEX_REASONING` の前後空白・大文字小文字は管理 API
    （`_validate_depth_base_codex_reasoning`）と同じ `strip().lower()` で正規化してから
    シードする（`" HIGH "` → `"high"`）。"""
    monkeypatch.setenv("SHERPA_CODEX_REASONING", " HIGH ")
    monkeypatch.setattr(store, "get_system_settings", lambda: {})
    calls = _set_system_settings_recorder(monkeypatch)
    api._seed_depth_profile_from_env()
    assert len(calls) == 1
    assert calls[0]["updates"]["depth_base_codex_reasoning"] == "high"


def test_healthz_depth_profile_seed_recovers_after_env_fixed(monkeypatch):
    """不正な env で見送った直後の healthz 再試行では引き続き未シード。env を既知語彙へ直した後の
    再試行で、7項目・マーカーが一括で確定する（`test_healthz_depth_profile_seed_retries_after_
    transient_failure_independent_of_others` と対の検証・DB 瞬断ではなく env 不正が原因）。"""
    from sherpa.routers import system as system_router

    monkeypatch.setattr(store, "schema_ready", lambda: True)
    monkeypatch.setattr(api, "_seed_settings_from_env", lambda: None)
    monkeypatch.setattr(api, "_seed_ollama_url_from_env", lambda: None)
    monkeypatch.setattr(api, "_confirm_legacy_env_seed_marker", lambda: None)
    monkeypatch.setattr(api, "_catchup_ollama_allowlist_for_central_url", lambda: None)
    monkeypatch.setattr(api, "_seed_openai_endpoint_from_env", lambda: None)
    monkeypatch.setattr(model_catalog, "seed_catalog_once", lambda: None)
    db = _FakeSystemSettingsDB()
    monkeypatch.setattr(store, "get_system_settings", db.get_system_settings)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)

    monkeypatch.setenv("SHERPA_CODEX_REASONING", "ultra")
    system_router.healthz()   # 1回目: 不正 env のため見送り
    assert api._DEPTH_PROFILE_SEED_MARKER_KEY not in db.data
    assert len(db.seed_calls) == 0

    monkeypatch.setenv("SHERPA_CODEX_REASONING", "high")
    system_router.healthz()   # 2回目: env 修正後は一括で確定
    assert db.data[api._DEPTH_PROFILE_SEED_MARKER_KEY] == api._DEPTH_PROFILE_SEED_VERSION
    assert db.data["depth_base_codex_reasoning"] == "high"
    assert len(db.seed_calls) == 1


def test_healthz_depth_profile_seed_retries_after_transient_failure_independent_of_others(monkeypatch):
    """調べる深さシードだけが一時的に失敗（DB 瞬断等）しても、次の healthz 呼び出しで再試行され、
    最終的に1回だけ実際にシードされる（`model_catalog` 版と対の検証・他の一度だけシードは no-op に
    差し替えて分離する）。"""
    from sherpa.routers import system as system_router

    monkeypatch.setattr(store, "schema_ready", lambda: True)
    monkeypatch.setattr(api, "_seed_settings_from_env", lambda: None)
    monkeypatch.setattr(api, "_seed_ollama_url_from_env", lambda: None)
    monkeypatch.setattr(api, "_confirm_legacy_env_seed_marker", lambda: None)
    monkeypatch.setattr(api, "_catchup_ollama_allowlist_for_central_url", lambda: None)
    monkeypatch.setattr(api, "_seed_openai_endpoint_from_env", lambda: None)
    monkeypatch.setattr(model_catalog, "seed_catalog_once", lambda: None)
    db = _FakeSystemSettingsDB()
    state = {"n": 0}

    def _flaky_get():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient db blip")
        return db.get_system_settings()

    monkeypatch.setattr(store, "get_system_settings", _flaky_get)
    monkeypatch.setattr(store, "seed_system_settings_once", db.seed_system_settings_once)

    system_router.healthz()   # 1回目: get_system_settings が例外→シード失敗（healthz は落ちない）
    assert api._DEPTH_PROFILE_SEED_MARKER_KEY not in db.data

    system_router.healthz()   # 2回目: 再試行して成功
    assert db.data[api._DEPTH_PROFILE_SEED_MARKER_KEY] == api._DEPTH_PROFILE_SEED_VERSION
    assert len(db.seed_calls) == 1   # 実際に書いたのは1回だけ


# ---- 起動時: A6（個人 API キー原則）が false なら個人キーを一括削除（`api._purge_personal_keys_if_disabled_on_startup`） ----
# OFF のとき個人キーは保存されない状態を保つ。

def test_purge_personal_keys_on_startup_when_disabled(monkeypatch):
    """personal_api_keys_allowed が false なら `store.purge_personal_api_keys` を呼ぶ。"""
    monkeypatch.setattr("sherpa.keys.personal_keys_allowed", lambda: False)
    calls = []
    monkeypatch.setattr(store, "purge_personal_api_keys", lambda actor="system": (calls.append(actor), 3)[1])
    api._purge_personal_keys_if_disabled_on_startup()
    assert calls == ["system"]


def test_purge_personal_keys_on_startup_skips_when_enabled(monkeypatch):
    """personal_api_keys_allowed が true なら起動時に何もしない（個人キーは残る）。"""
    monkeypatch.setattr("sherpa.keys.personal_keys_allowed", lambda: True)
    called = []
    monkeypatch.setattr(store, "purge_personal_api_keys", lambda actor="system": called.append(True))
    api._purge_personal_keys_if_disabled_on_startup()
    assert called == []


def test_purge_personal_keys_on_startup_logs_only_when_count_positive(monkeypatch, caplog):
    """削除件数>0のときだけ起動ログに1行出す（0件なら出さない＝ノイズにしない）。"""
    import logging
    monkeypatch.setattr("sherpa.keys.personal_keys_allowed", lambda: False)

    monkeypatch.setattr(store, "purge_personal_api_keys", lambda actor="system": 5)
    with caplog.at_level(logging.INFO, logger="sherpa"):
        api._purge_personal_keys_if_disabled_on_startup()
    assert any("削除" in r.getMessage() and "5" in r.getMessage() for r in caplog.records)

    caplog.clear()
    monkeypatch.setattr(store, "purge_personal_api_keys", lambda actor="system": 0)
    with caplog.at_level(logging.INFO, logger="sherpa"):
        api._purge_personal_keys_if_disabled_on_startup()
    assert not any("削除" in r.getMessage() for r in caplog.records)


def test_purge_personal_keys_on_startup_survives_db_unreachable(monkeypatch):
    """DB 不達（例外）でも起動は止めない（warning ログのみ）。"""
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("sherpa.keys.personal_keys_allowed", _boom)
    api._purge_personal_keys_if_disabled_on_startup()   # 例外を投げなければ OK


# ---- ①-0' Codex sandbox 無効化の production fail-closed 検査（2026-07-13-横断レビュー対応.md R4） ----

def test_warn_codex_sandbox_disabled_fail_closed_in_production(monkeypatch):
    """production かつ SHERPA_CODEX_SANDBOX が無効（agents._codex_sandbox_enabled() が False）なら
    RuntimeError で起動拒否する。"""
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    with pytest.raises(RuntimeError):
        api._warn_codex_sandbox_disabled()


def test_warn_codex_sandbox_disabled_dev_warns_but_continues(monkeypatch, caplog):
    """development（本番マーカーなし）は sandbox 無効でも警告のみ・例外は投げない。"""
    import logging
    monkeypatch.setenv("SHERPA_ENV", "dev")
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert api._warn_codex_sandbox_disabled() is None
    assert any("sandbox" in r.message.lower() for r in caplog.records)


def test_warn_codex_sandbox_enabled_silent_even_in_production(monkeypatch, caplog):
    """SHERPA_CODEX_SANDBOX が有効（既定含む）なら production でも無警告・起動継続。"""
    import logging
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.delenv("SHERPA_CODEX_SANDBOX", raising=False)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert api._warn_codex_sandbox_disabled() is None
    assert not caplog.records


# ---- ①' 背景実行チャットターン: workers>1（docs/proposals/2026-07-03-チャット背景実行.md §制約） ----
# 2026-07-13-横断レビュー対応.md R4: production では警告のみ→fail-closed（起動拒否）へ格上げ。
# chat_turns の他 ratelimit の in-memory 状態も worker 間非共有のため。

def test_warn_multi_worker_chat_turns_fail_closed_in_production(monkeypatch):
    """SHERPA_ENV=production かつ SHERPA_UVICORN_WORKERS>1 は RuntimeError で起動拒否する。"""
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.setenv("SHERPA_UVICORN_WORKERS", "4")
    with pytest.raises(RuntimeError):
        api._warn_multi_worker_chat_turns()


def test_warn_multi_worker_chat_turns_dev_warns_but_does_not_raise(monkeypatch, caplog):
    """development（本番マーカーなし）は workers>1 でも警告するだけ（起動は続行）。"""
    import logging
    monkeypatch.setenv("SHERPA_ENV", "dev")
    monkeypatch.setenv("SHERPA_UVICORN_WORKERS", "4")
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert api._warn_multi_worker_chat_turns() is None
    assert any("workers" in r.message.lower() or "worker" in r.message for r in caplog.records)


def test_warn_multi_worker_chat_turns_silent_when_single_or_unset(monkeypatch, caplog):
    """workers=1（既定）・未設定はどちらも無警告（既定運用を邪魔しない）。"""
    import logging
    monkeypatch.delenv("SHERPA_UVICORN_WORKERS", raising=False)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        api._warn_multi_worker_chat_turns()
        monkeypatch.setenv("SHERPA_UVICORN_WORKERS", "1")
        api._warn_multi_worker_chat_turns()
    assert not caplog.records


# ---- ①'' フォルダ選択ルート（既定 /mnt）不在の警告（Linux サーバホスト対応 L1） ----

def test_warn_browse_roots_missing_warns_when_all_roots_absent(monkeypatch, caplog):
    """既定ルート（/mnt）が1つも存在しなければ警告する（Linux サーバ等・SHERPA_BROWSE_ROOTS 未設定）。"""
    import logging
    monkeypatch.delenv("SHERPA_BROWSE_ROOTS", raising=False)
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert api._warn_browse_roots_missing() is None
    assert any("フォルダ選択のルート" in r.message for r in caplog.records)


def test_warn_browse_roots_silent_when_root_exists(monkeypatch, caplog):
    """既定ルートが存在すれば無警告（既存 WSL 運用を邪魔しない）。"""
    import logging
    monkeypatch.delenv("SHERPA_BROWSE_ROOTS", raising=False)
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: True)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        api._warn_browse_roots_missing()
    assert not caplog.records


def test_warn_browse_roots_checks_env_override(monkeypatch, caplog):
    """SHERPA_BROWSE_ROOTS 設定時はそちらのルートを検査する（既定 /mnt は見ない）。"""
    import logging
    monkeypatch.setenv("SHERPA_BROWSE_ROOTS", "/srv/sherpa-data")
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert api._warn_browse_roots_missing() is None
    assert any("/srv/sherpa-data" in r.message for r in caplog.records)


def test_warn_browse_roots_missing_survives_is_dir_exception(monkeypatch, caplog):
    """`Path.is_dir()` が OSError 等を投げても警告のみで例外は伝播しない（起動を絶対に壊さない）。"""
    import logging

    def _raise(self):
        raise OSError("stale mount")

    monkeypatch.delenv("SHERPA_BROWSE_ROOTS", raising=False)
    monkeypatch.setattr("pathlib.Path.is_dir", _raise)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert api._warn_browse_roots_missing() is None
    assert any("フォルダ選択のルート" in r.message for r in caplog.records)


def test_browse_roots_excludes_empty_segments(monkeypatch):
    """`SHERPA_BROWSE_ROOTS=/missing:` のような末尾コロンで生じる空セグメントは除外する
    （`Path("")` は cwd 扱いになり、警告抑止や許可ルートへの意図しない cwd 混入を招くため）。"""
    monkeypatch.setenv("SHERPA_BROWSE_ROOTS", "/missing:")
    assert api._browse_roots() == [api.Path("/missing")]


def test_browse_roots_falls_back_to_default_when_all_segments_empty(monkeypatch):
    """全セグメントが空（例 `SHERPA_BROWSE_ROOTS=:`）なら既定 `/mnt:/srv:/home` にフォールバックする
    （既定の3ルートは2026-09-04裁定・`deps._browse_roots` docstring 参照）。"""
    monkeypatch.setenv("SHERPA_BROWSE_ROOTS", ":")
    assert api._browse_roots() == [api.Path("/mnt"), api.Path("/srv"), api.Path("/home")]


# ---- ② poller の有効/無効 ----

def test_poller_disabled_by_default(monkeypatch):
    """SHERPA_POLL_SECONDS 未設定（<=0）ならポーラースレッドを起動しない。"""
    monkeypatch.delenv("SHERPA_POLL_SECONDS", raising=False)
    records: list = []
    monkeypatch.setattr("threading.Thread", _thread_recorder(records))
    api._start_poller()
    assert records == []


def test_poller_enabled_when_env_positive(monkeypatch):
    """SHERPA_POLL_SECONDS>0 なら daemon ポーラースレッドを1本起動する。"""
    monkeypatch.setenv("SHERPA_POLL_SECONDS", "300")
    records: list = []
    monkeypatch.setattr("threading.Thread", _thread_recorder(records))
    api._start_poller()
    assert len(records) == 1
    assert records[0]["name"] == "sherpa-poller"
    assert records[0]["daemon"] is True


# ---- ③ TestClient 起動で startup が旧登録順どおりに走る ----

def test_lifespan_runs_startup_steps_in_order(monkeypatch):
    """`with TestClient(app)` で lifespan が起動処理を旧 on_event 登録順どおりに呼ぶ
    （背景実行チャットターン導入時に追加した workers>1 警告は②の直後に挟まる）。
    `_warn_change_me_placeholders`／`_warn_default_admin_password` は auth bootstrap が
    admin を DB に刻む前に検査する必要があるため、api 側の起動処理ではこの2つだけが
    auth より前に呼ばれる（監査台帳#3・ENV-ONE で `_warn_change_me_placeholders` を追加）。
    R5（2026-07-13-横断レビュー対応.md）でさらにその前に `store.init_schema()` が入った
    （auth bootstrap 自体が schema 依存のため全 startup の先頭・RV LOW で順序をここに固定）。"""
    calls: list[str] = []
    monkeypatch.setattr(store, "init_schema", lambda: calls.append("schema"))
    monkeypatch.setattr(api, "_seed_settings_from_env", lambda: calls.append("seed_settings"))
    monkeypatch.setattr(api, "_seed_ollama_url_from_env", lambda: calls.append("seed_ollama_url"))
    monkeypatch.setattr(api, "_confirm_legacy_env_seed_marker",
                        lambda: calls.append("confirm_legacy_env_seed"))
    monkeypatch.setattr(api, "_catchup_ollama_allowlist_for_central_url",
                        lambda: calls.append("catchup_ollama_allowlist"))
    monkeypatch.setattr(api, "_seed_openai_endpoint_from_env", lambda: calls.append("seed_openai_endpoint"))
    monkeypatch.setattr(api, "_seed_depth_profile_from_env", lambda: calls.append("seed_depth_profile"))
    monkeypatch.setattr(model_catalog, "seed_catalog_once", lambda: calls.append("model_catalog_seed"))
    monkeypatch.setattr(api, "_purge_personal_keys_if_disabled_on_startup",
                        lambda: calls.append("purge_personal_keys"))
    monkeypatch.setattr(api, "_warn_change_me_placeholders", lambda: calls.append("warn_change_me"))
    monkeypatch.setattr(api, "_warn_default_admin_password", lambda: calls.append("warn_default_admin"))
    monkeypatch.setattr(api, "_auth_bootstrap_on_startup", lambda: calls.append("auth"))
    monkeypatch.setattr(api, "_warn_fixtures", lambda: calls.append("warn_fixtures"))
    monkeypatch.setattr(api, "_warn_test_db_isolated", lambda: calls.append("warn_test_db_isolated"))
    monkeypatch.setattr(api, "_warn_codex_sandbox_disabled", lambda: calls.append("warn_codex_sandbox"))
    monkeypatch.setattr(api, "_warn_multi_worker_chat_turns", lambda: calls.append("warn_multi_worker"))
    monkeypatch.setattr(api, "_warn_browse_roots_missing", lambda: calls.append("warn_browse_roots"))
    monkeypatch.setattr(api, "_start_poller", lambda: calls.append("poller"))
    monkeypatch.setattr(api, "_reconcile_orphans", lambda: calls.append("reconcile"))
    monkeypatch.setattr(api, "_sweep_expired_on_startup", lambda: calls.append("sweep"))
    with TestClient(api.app):
        pass
    assert calls == [
        "schema", "seed_settings", "seed_ollama_url", "confirm_legacy_env_seed", "catchup_ollama_allowlist",
        "seed_openai_endpoint", "seed_depth_profile", "model_catalog_seed", "purge_personal_keys", "warn_change_me", "warn_default_admin", "auth",
        "warn_fixtures", "warn_test_db_isolated", "warn_codex_sandbox", "warn_multi_worker", "warn_browse_roots",
        "poller", "reconcile", "sweep",
    ]


def test_lifespan_reattaches_request_id_filter_before_other_startup_steps(monkeypatch):
    """起動処理の先頭で `ext_api._attach_request_id_filter()` を（再度）呼ぶ——ASGI サーバーが
    root logger へ自身の handler を import より後に追加した場合でも、そこに request_id filter が
    届くようにするため（`sherpa/ext_api.py::_attach_request_id_filter` の契約）。"""
    calls: list[str] = []
    monkeypatch.setattr(ext_api, "_attach_request_id_filter", lambda: calls.append("attach_request_id_filter"))
    monkeypatch.setattr(store, "init_schema", lambda: calls.append("schema"))
    monkeypatch.setattr(api, "_seed_settings_from_env", lambda: calls.append("seed_settings"))
    monkeypatch.setattr(api, "_seed_ollama_url_from_env", lambda: calls.append("seed_ollama_url"))
    monkeypatch.setattr(api, "_confirm_legacy_env_seed_marker",
                        lambda: calls.append("confirm_legacy_env_seed"))
    monkeypatch.setattr(api, "_catchup_ollama_allowlist_for_central_url",
                        lambda: calls.append("catchup_ollama_allowlist"))
    monkeypatch.setattr(api, "_seed_openai_endpoint_from_env", lambda: calls.append("seed_openai_endpoint"))
    monkeypatch.setattr(api, "_seed_depth_profile_from_env", lambda: calls.append("seed_depth_profile"))
    monkeypatch.setattr(model_catalog, "seed_catalog_once", lambda: calls.append("model_catalog_seed"))
    monkeypatch.setattr(api, "_purge_personal_keys_if_disabled_on_startup",
                        lambda: calls.append("purge_personal_keys"))
    monkeypatch.setattr(api, "_warn_change_me_placeholders", lambda: calls.append("warn_change_me"))
    monkeypatch.setattr(api, "_warn_default_admin_password", lambda: calls.append("warn_default_admin"))
    monkeypatch.setattr(api, "_auth_bootstrap_on_startup", lambda: calls.append("auth"))
    monkeypatch.setattr(api, "_warn_fixtures", lambda: calls.append("warn_fixtures"))
    monkeypatch.setattr(api, "_warn_test_db_isolated", lambda: calls.append("warn_test_db_isolated"))
    monkeypatch.setattr(api, "_warn_codex_sandbox_disabled", lambda: calls.append("warn_codex_sandbox"))
    monkeypatch.setattr(api, "_warn_multi_worker_chat_turns", lambda: calls.append("warn_multi_worker"))
    monkeypatch.setattr(api, "_warn_browse_roots_missing", lambda: calls.append("warn_browse_roots"))
    monkeypatch.setattr(api, "_start_poller", lambda: calls.append("poller"))
    monkeypatch.setattr(api, "_reconcile_orphans", lambda: calls.append("reconcile"))
    monkeypatch.setattr(api, "_sweep_expired_on_startup", lambda: calls.append("sweep"))
    with TestClient(api.app):
        pass
    assert calls[0] == "attach_request_id_filter"


def test_lifespan_continues_when_schema_init_fails(monkeypatch):
    """R5: 起動時 `store.init_schema()` の失敗（DB 不達等）は warning のみで、後続の startup
    処理を1つも止めない（fail-open＝readiness が false のままになるだけ・lifespan の契約）。"""
    calls: list[str] = []

    def _boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(store, "init_schema", _boom)
    monkeypatch.setattr(api, "_seed_settings_from_env", lambda: calls.append("seed_settings"))
    monkeypatch.setattr(api, "_seed_ollama_url_from_env", lambda: calls.append("seed_ollama_url"))
    monkeypatch.setattr(api, "_confirm_legacy_env_seed_marker",
                        lambda: calls.append("confirm_legacy_env_seed"))
    monkeypatch.setattr(api, "_catchup_ollama_allowlist_for_central_url",
                        lambda: calls.append("catchup_ollama_allowlist"))
    monkeypatch.setattr(api, "_seed_openai_endpoint_from_env", lambda: calls.append("seed_openai_endpoint"))
    monkeypatch.setattr(api, "_seed_depth_profile_from_env", lambda: calls.append("seed_depth_profile"))
    monkeypatch.setattr(model_catalog, "seed_catalog_once", lambda: calls.append("model_catalog_seed"))
    monkeypatch.setattr(api, "_purge_personal_keys_if_disabled_on_startup",
                        lambda: calls.append("purge_personal_keys"))
    monkeypatch.setattr(api, "_warn_change_me_placeholders", lambda: calls.append("warn_change_me"))
    monkeypatch.setattr(api, "_warn_default_admin_password", lambda: calls.append("warn_default_admin"))
    monkeypatch.setattr(api, "_auth_bootstrap_on_startup", lambda: calls.append("auth"))
    monkeypatch.setattr(api, "_warn_fixtures", lambda: calls.append("warn_fixtures"))
    monkeypatch.setattr(api, "_warn_test_db_isolated", lambda: calls.append("warn_test_db_isolated"))
    monkeypatch.setattr(api, "_warn_codex_sandbox_disabled", lambda: calls.append("warn_codex_sandbox"))
    monkeypatch.setattr(api, "_warn_multi_worker_chat_turns", lambda: calls.append("warn_multi_worker"))
    monkeypatch.setattr(api, "_warn_browse_roots_missing", lambda: calls.append("warn_browse_roots"))
    monkeypatch.setattr(api, "_start_poller", lambda: calls.append("poller"))
    monkeypatch.setattr(api, "_reconcile_orphans", lambda: calls.append("reconcile"))
    monkeypatch.setattr(api, "_sweep_expired_on_startup", lambda: calls.append("sweep"))
    with TestClient(api.app):
        pass
    assert calls == [
        "seed_settings", "seed_ollama_url", "confirm_legacy_env_seed", "catchup_ollama_allowlist",
        "seed_openai_endpoint", "seed_depth_profile", "model_catalog_seed", "purge_personal_keys", "warn_change_me", "warn_default_admin", "auth",
        "warn_fixtures", "warn_test_db_isolated", "warn_codex_sandbox", "warn_multi_worker", "warn_browse_roots",
        "poller", "reconcile", "sweep",
    ]


def test_lifespan_stops_audit_writer_even_when_startup_step_raises(monkeypatch):
    """`ext_api._audit_writer.start()` の後で起動処理の途中（例: `_warn_default_admin_password`）
    が例外を投げても、`ext_api._audit_writer.stop()` は try/finally で必ず呼ばれる——さもないと
    start() 済みの writer が誰にも stop() されないまま取り残される（`sherpa/lifespan.py` の
    try/finally 契約）。`_warn_default_admin_password` より前に走る起動時シード処理は本テストの
    焦点（例外時の stop() 保証）と無関係だが、差し替えないと実テスト DB へマーカーを書き込んだり
    プロセス内の openai I/O ブロック状態を変えたりし得るため、全て明示的に no-op へ差し替える。"""
    monkeypatch.setattr(store, "init_schema", lambda: None)
    monkeypatch.setattr(api, "_seed_settings_from_env", lambda: None)
    monkeypatch.setattr(api, "_seed_ollama_url_from_env", lambda: None)
    monkeypatch.setattr(api, "_confirm_legacy_env_seed_marker", lambda: None)
    monkeypatch.setattr(api, "_catchup_ollama_allowlist_for_central_url", lambda: None)
    monkeypatch.setattr(api, "_seed_openai_endpoint_from_env", lambda: None)
    monkeypatch.setattr(api, "_seed_depth_profile_from_env", lambda: None)
    monkeypatch.setattr(model_catalog, "seed_catalog_once", lambda: None)
    monkeypatch.setattr(api, "_purge_personal_keys_if_disabled_on_startup", lambda: None)
    monkeypatch.setattr(api, "_warn_change_me_placeholders", lambda: None)

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
