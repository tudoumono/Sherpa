"""AWS Bedrock（Claude）設定の保存/公開・接続テスト分岐（FastAPI TestClient・要 Postgres）。

- bedrock_model / bedrock_api_key の保存 → GET /settings で model と bedrock_key_set（有無のみ）が
  返り、キー値は本人にも返らない（openai_api_key と同じ書込専用扱い）。
- POST /settings/test（provider=bedrock）は BedrockProvider.probe を fake にして ok/model を返す。
- 2026-07 決定（誤設定の余地を減らす）: region は東京固定・model は allowlist 選択式。
  - PUT /settings の bedrock_model allowlist 検証（許可外 422・空文字/未指定は既定を許可）。
  - region は常に東京固定（`agents._bedrock_region`）。`bedrock_region`（個人設定）は撤去済み
    （保存フィールド自体が無い＝送っても無視され保存されない）。
DB 不可は graceful SKIP（test_health_api.py の流儀）。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error

import pytest

from _test_users import register_test_uid

IMPORT_ERROR: Exception | None = None
try:
    from fastapi.testclient import TestClient

    from sherpa import agents, api, auth, store
    from sherpa.api import app
    from sherpa.routers import system as system_router
except Exception as e:  # pragma: no cover
    IMPORT_ERROR = e
    TestClient = None  # type: ignore[assignment]
    system_router = None  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _enable_bedrock(monkeypatch):
    """4構成（2026-08-15・`sherpa/agent_constructs.py`）: bedrock は `SHERPA_EXTRA_AGENTS` で
    有効化した環境でだけ保存・実行できる。このファイルは Bedrock 設定そのものの契約を見るため、
    全テストで有効化した状態を前提にする（未有効時に 422 になることは
    `tests/unit/test_agent_constructs.py` が固定する）。

    `sherpa.keys.resolve_api_key("bedrock", ...)` は
    (1) A7＝`cloud_provider` が bedrock を選択中であること (2) A6＝`personal_api_keys_allowed` が
    真であること、の両方を要求する（既定は openai / false）。このファイルは per-user の
    `bedrock_api_key` を直接 PUT する契約を見るため、両方を有効化した状態を前提にする
    （プロセス内 TestClient のため `store.get_system_settings` の monkeypatch が実ハンドラにも効く。
    A6/A7 が実際に false/非選択のときの挙動は `tests/api/test_settings_provider_split.py` 等が別途見る）。

    `store.update_settings()` の個人キー書込みは、A6 を関数呼出し時点で実 DB から直接（この
    monkeypatch を経由せず）再確認する（advisory lock 付き）。このファイルの多くのテストが
    `store.update_settings(uid, bedrock_api_key=...)` を直接呼ぶため、monkeypatch だけでは
    通らない＝実 DB にも `personal_api_keys_allowed=True` を書いておく。他のテストファイルと
    共有する実 DB の状態を汚さないよう、元の値を退避してテスト終了後に復元する
    （fixture の実行順に依存しない・DB 不可時は monkeypatch のみで進め、各テストの
    `_try_init()` が改めて検出して skip する）。
    """
    monkeypatch.setenv("SHERPA_EXTRA_AGENTS", "bedrock,gemini,heuristic")
    from sherpa import store
    monkeypatch.setattr(store, "get_system_settings",
                        lambda: {"cloud_provider": "bedrock", "personal_api_keys_allowed": True})
    # WEB-1 以降、get_provider は共有キャッシュ非経由の生読取（_read_system_settings_fresh）を
    # 唯一の読取点にする（TOCTOU 封鎖）。テストの前提（A7=bedrock 選択中）を実 DB へ書かずに
    # 揃えるため、こちらも同じスナップショットへ固定する。
    monkeypatch.setattr(store, "_read_system_settings_fresh",
                        lambda: {"cloud_provider": "bedrock", "personal_api_keys_allowed": True})
    try:
        store.init_schema()
        with store._connect() as c:
            _prev_row = c.execute(
                "SELECT value FROM system_settings WHERE key='personal_api_keys_allowed'").fetchone()
    except Exception:
        yield
        return
    store.set_system_settings("admin-uid", {"personal_api_keys_allowed": True})
    try:
        yield
    finally:
        store.set_system_settings(
            "admin-uid",
            {"personal_api_keys_allowed": bool(_prev_row["value"]) if _prev_row else None})


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    if IMPORT_ERROR is not None:
        pytest.skip(f"infra down: {IMPORT_ERROR}")
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"infra down: {e}")


def _mk_user(uid: str, password: str) -> None:
    store.upsert_user(uid, email=f"{uid}@bedrock.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(password), role="user", status="active")
    register_test_uid(uid)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）


def _login(uid: str, password: str) -> "TestClient":
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return c


def _wait_until_blocked_by(monitor_conn, table_hint: str, holder_pid: int, timeout: float = 5.0) -> int | None:
    """R4-5（2026-07-16 Codex RV 4巡目再検証）→L6（5巡目再検証）→C3（6巡目再検証で観測方式を
    pg_blocking_pids ベースに是正）: 「1秒待って完了していないこと」という負のヒューリスティック
    ではなく、bounded polling で「ワーカーが実際にロック待ちへ入ったこと」を積極的に確認する。

    L6 時点では `table_hint`（対象クエリの ILIKE パターン）と自分自身（holder/monitor）の
    backend pid 除外だけで一意化していたが、これでも**第三の無関係なバックエンド**が同じテーブルに
    対して別の理由でロック待ちになった場合には誤認しうる（Codex RV 指摘・C3）。ここでは
    `pg_blocking_pids(candidate_pid)` に `holder_pid` が含まれるバックエンドだけを候補にする＝
    「holder に実際にブロックされている」という因果関係そのものを確認する（本番コードへ
    `application_name` 注入等のテスト専用フックを足す必要が無い・より直接的で誤認耐性が高い）。
    候補が**ちょうど1件**であることを要求する（0件はまだロック待ちに入っていない・2件以上は
    一意に特定できないとして例外にする＝false positive を握りつぶさない）。監視専用の別コネクション
    を使う（holder/worker のトランザクションには関与しない・診断クエリごとに rollback して次の
    反復に備える）。見つかったワーカーの pid を返す（timeout まで見つからなければ None）。
    """
    deadline = time.monotonic() + timeout
    pattern = f"%{table_hint}%"
    while time.monotonic() < deadline:
        rows = monitor_conn.execute(
            "SELECT pid FROM pg_stat_activity "
            "WHERE wait_event_type='Lock' AND query ILIKE %s AND datname = current_database() "
            "  AND %s = ANY(pg_blocking_pids(pid))",
            (pattern, holder_pid)).fetchall()
        monitor_conn.rollback()
        if len(rows) == 1:
            return rows[0]["pid"]
        if len(rows) > 1:
            raise AssertionError(
                f"holder（pid={holder_pid}）にブロックされているバックエンドが複数見つかった"
                f"（対象ワーカーを一意に特定できない）: {[r['pid'] for r in rows]}")
        time.sleep(0.05)
    return None


def test_bedrock_settings_saved_and_key_masked():
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bdrks{sfx}", f"bdrks-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    key = f"bedrock-key-{sfx}"
    r = c.put("/settings", json={"agent": "bedrock",
                                 "bedrock_model": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
                                 "bedrock_api_key": key})
    assert r.status_code == 200, r.text
    s = c.get("/settings").json()
    assert s["agent"] == "bedrock"
    assert "bedrock_region" not in s                        # 撤去済み（応答からも消える）
    assert s["bedrock_model"] == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert s["bedrock_key_set"] is True
    assert "bedrock_api_key" not in s                       # 値は返さない（有無のみ）
    assert key not in json.dumps(s)                         # 本人にもキー値は漏らさない


def test_settings_test_bedrock_uses_fake_probe(monkeypatch):
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bdrkt{sfx}", f"bdrkt-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    monkeypatch.setattr(agents.BedrockProvider, "probe", lambda self: (True, ""))
    r = c.post("/settings/test", json={"provider": "bedrock",
                                       "bedrock_model": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
                                       "bedrock_api_key": "k"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["provider"] == "bedrock"
    assert d["model"] == "global.anthropic.claude-haiku-4-5-20251001-v1:0" and d["detail"] == "接続OK"


def test_settings_test_bedrock_reports_probe_failure(monkeypatch):
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bdrkf{sfx}", f"bdrkf-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    monkeypatch.setattr(agents.BedrockProvider, "probe", lambda self: (False, "403: access denied"))
    r = c.post("/settings/test", json={"provider": "bedrock", "bedrock_api_key": "k"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False and d["detail"] == "403: access denied"
    assert d["model"] == "jp.anthropic.claude-haiku-4-5-20251001-v1:0"        # 既定モデル（未指定時）


def test_settings_test_bedrock_redacts_key_leaked_in_probe_detail(monkeypatch):
    """RV HIGH（2026-07-03再検証）: SDK/プロキシの例外メッセージに（保存前の）入力中キーが混入しても、
    /settings/test の応答にキー文字列が一切現れない（write-only キーの往復漏洩防止・回帰テスト）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bdrkleak{sfx}", f"BdrkLeak{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    leaked_key = f"secret-bedrock-key-{sfx}"
    monkeypatch.setattr(
        agents.BedrockProvider, "probe",
        lambda self: (False,
                      f"403 Forbidden: upstream said Authorization: Bearer {leaked_key} rejected (key={leaked_key})"))
    r = c.post("/settings/test", json={"provider": "bedrock", "bedrock_api_key": leaked_key})
    assert r.status_code == 200, r.text
    assert leaked_key not in r.text, "キーがレスポンス本文のどこかに漏れている"
    d = r.json()
    assert d["ok"] is False
    assert leaked_key not in d["detail"]
    assert "Bearer [REDACTED]" in d["detail"]


# ===== 2026-07 決定: region 東京固定・model allowlist 選択式 =====

def test_bedrock_settings_rejects_invalid_model():
    """PUT /settings は allowlist 外の bedrock_model を 422 で拒否する（誤設定防止）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bdrkinv{sfx}", f"bdrkinv-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    r = c.put("/settings", json={"bedrock_model": "anthropic.claude-not-a-real-model"})
    assert r.status_code == 422, r.text
    assert "選択肢" in r.json().get("detail", "")


def test_bedrock_settings_rejects_legacy_mantle_short_id():
    """Mantle 時代の短縮ID（`anthropic.claude-opus-4-8` 等）は runtime 切替（2026-07）で allowlist から
    外れたので 422 になる（保存済みの旧値は既存の「旧設定」UI 表示＋null 送信で無害に扱われるだけで、
    新規保存は許可しない）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bdrklgc{sfx}", f"bdrklgc-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    r = c.put("/settings", json={"bedrock_model": "anthropic.claude-opus-4-8"})
    assert r.status_code == 422, r.text
    assert "選択肢" in r.json().get("detail", "")


# ===== RV HIGH（2026-07-03・S5）: agent も同じ allowlist 検証の流儀にする =====

def test_settings_rejects_invalid_agent():
    """PUT /settings は allowlist 外の agent を 422 で拒否する
    （chat.turn 監査 detail に任意文字列が入るのを防ぐための誤設定ガード）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"agtinv{sfx}", f"agtinv-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    r = c.put("/settings", json={"agent": "not-a-real-agent"})
    assert r.status_code == 422, r.text
    assert "heuristic" in r.json().get("detail", "")


def test_settings_accepts_all_allowlisted_agents(monkeypatch):
    """AGENT_PROVIDERS の全値が PUT /settings で保存できる（allowlist 追加時の取りこぼし防止）。

    クラウド系 agent（openai/gemini/bedrock）は選択中のクラウドプロバイダ（A7）と一致しないと
    422 になるため、対象の agent ごとに選択中プロバイダを合わせてから保存する。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"agtok{sfx}", f"agtok-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    from sherpa import keys as _keys
    for a in sorted(agents.AGENT_PROVIDERS):
        provider = a if a in _keys.CLOUD_PROVIDERS else "bedrock"
        monkeypatch.setattr(
            store, "get_system_settings",
            lambda provider=provider: {"cloud_provider": provider, "personal_api_keys_allowed": True})
        r = c.put("/settings", json={"agent": a})
        assert r.status_code == 200, f"{a}: {r.text}"
        assert c.get("/settings").json()["agent"] == a


def test_bedrock_settings_accepts_all_allowlisted_models():
    """BEDROCK_MODEL_CHOICES の全モデルが保存でき、GET /settings に反映される。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bdrkall{sfx}", f"bdrkall-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    for model_id, _label in agents.BEDROCK_MODEL_CHOICES:
        r = c.put("/settings", json={"bedrock_model": model_id})
        assert r.status_code == 200, f"{model_id}: {r.text}"
        assert c.get("/settings").json()["bedrock_model"] == model_id


def test_bedrock_settings_empty_model_falls_back_to_default():
    """bedrock_model 空文字は許可され、既定モデルへフォールバックする（allowlist 検証をすり抜けない）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bdrkempty{sfx}", f"bdrkempty-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    # まず非既定モデルを保存してから、空文字で既定へ戻す。
    r1 = c.put("/settings", json={"bedrock_model": "global.anthropic.claude-haiku-4-5-20251001-v1:0"})
    assert r1.status_code == 200, r1.text
    r2 = c.put("/settings", json={"bedrock_model": ""})
    assert r2.status_code == 200, r2.text
    assert c.get("/settings").json()["bedrock_model"] == "jp.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_bedrock_region_function_always_returns_tokyo(monkeypatch):
    """`_bedrock_region` は引数を取らず、env AWS_REGION も無視して常に東京固定を返す（2026-07 決定）。"""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert agents._bedrock_region() == "ap-northeast-1"
    monkeypatch.delenv("AWS_REGION", raising=False)
    assert agents._bedrock_region() == "ap-northeast-1"


def test_bedrock_provider_ignores_explicit_region_argument():
    """`BedrockProvider(region=...)` は呼び出し側互換のため引数を受け取るが、実際の接続では
    `_bedrock_region()`（引数無し・常に東京固定）へ完全に委譲され、渡した値は一切効かない。"""
    p = agents.BedrockProvider(region="us-east-1", model="m", api_key="k")
    assert p._region == "ap-northeast-1"


def test_bedrock_provider_uses_tokyo_region_regardless_of_saved_settings(monkeypatch):
    """保存済み bedrock_region（旧設定・DB には残るが無視されるだけ）があっても、実際に構築される
    BedrockProvider は常に東京リージョンを使う（_select_provider 経由の統合確認）。"""
    monkeypatch.delenv("AWS_REGION", raising=False)
    settings = {"agent": "bedrock", "bedrock_region": "us-east-1",
                "bedrock_model": "jp.anthropic.claude-haiku-4-5-20251001-v1:0", "bedrock_api_key": "dummy-key"}
    provider = agents.get_provider(settings)
    assert isinstance(provider, agents.BedrockProvider)
    assert provider._region == "ap-northeast-1"


def test_bedrock_settings_region_field_is_retired_and_not_stored(monkeypatch):
    """`bedrock_region`（個人設定・死んだ設定）は撤去済み。API 経由で送っても
    `SettingsReq` の未知フィールドとして黙って無視され（422 にはならない）、保存されない
    （store→agents.get_provider の end-to-end 確認・DB 不可なら SKIP）。"""
    if not _try_init():
        pytest.skip("infra down")
    monkeypatch.delenv("AWS_REGION", raising=False)
    sfx = _sfx()
    uid, pw = f"bdrke2e{sfx}", f"bdrke2e-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    r = c.put("/settings", json={"agent": "bedrock", "bedrock_region": "eu-central-1",
                                 "bedrock_api_key": "dummy-key"})
    assert r.status_code == 200, r.text
    saved = store.get_settings(uid)
    assert "bedrock_region" not in saved                     # 撤去済み＝保存されない
    provider = agents.get_provider(saved)
    assert isinstance(provider, agents.BedrockProvider)
    assert provider._region == "ap-northeast-1"                # region は常に東京固定


def test_bedrock_legacy_mantle_id_migrated_to_jp_profile_on_init():
    """起動時マイグレーション: 保存済みの旧 Mantle 短縮 ID（runtime では 400＝全ユーザーで無効）は
    init_schema で JP 推論プロファイル既定へ自動移行される（設定画面の「旧設定」表示が消える・2026-07-03）。
    既に新 ID を選んでいるユーザーは書き換えない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    legacy_uid, kept_uid = f"bmig1{sfx}", f"bmig2{sfx}"
    _mk_user(legacy_uid, f"BMig1{sfx}")
    _mk_user(kept_uid, f"BMig2{sfx}")
    # allowlist は API 層の検証なので、旧デプロイで保存された値を store 直書きで再現する。
    store.update_settings(legacy_uid, bedrock_model="anthropic.claude-haiku-4-5")
    globl = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    store.update_settings(kept_uid, bedrock_model=globl)

    store.init_schema()   # 冪等マイグレーションが走る

    assert store.get_settings(legacy_uid)["bedrock_model"] == \
        "jp.anthropic.claude-haiku-4-5-20251001-v1:0", "旧 Mantle 短縮 ID が JP プロファイルへ移行されていない"
    assert store.get_settings(kept_uid)["bedrock_model"] == globl, "有効な新 ID が移行で潰された"


# ===== S6（2026-07-03）: Bedrock モデルの動的取得（GET /settings/bedrock-models）=====
# 実 AWS 疎通はできない（キーはユーザー環境のみ）ので control-plane 呼び出しは fake する。
# 実疎通確認はユーザーが自分の設定画面で「利用可能なモデルを取得」ボタンを押して行う。

def test_bedrock_models_no_key_returns_empty_with_error(monkeypatch):
    """API キー未設定（per-user もサーバ env も無し）は例外にせず 200 のまま
    `{models: [], error: "..."}`（設定画面の UX を壊さない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bmodnk{sfx}", f"BModNk{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    for k in agents._BEDROCK_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    r = c.get("/settings/bedrock-models")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["models"] == []
    assert d["error"], "キー未設定の理由が返っていない"


def test_bedrock_models_filters_active_anthropic_only_and_hides_key(monkeypatch):
    """fake control-plane 応答の整形は agents 側で検証済み（test_graph_extract_bedrock 等）なので、
    ここではエンドポイントが保存済みキーをそのまま渡す配線と、キー自体を応答に出さないことを確認する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bmodok{sfx}", f"BModOk{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    key = f"fake-key-{sfx}"
    assert c.put("/settings", json={"bedrock_api_key": key}).status_code == 200

    def fake_list(api_key):
        assert api_key == key                     # 保存済みキーがそのまま渡ってくる
        return ([{"id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0",
                  "label": "Claude Sonnet 4.6（JP 推論プロファイル）"}], None)
    monkeypatch.setattr(system_router, "list_bedrock_inference_profiles", fake_list)
    api._BEDROCK_MODELS_CACHE.pop(uid, None)

    r = c.get("/settings/bedrock-models")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["error"] is None
    assert d["models"] == [{"id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0",
                            "label": "Claude Sonnet 4.6（JP 推論プロファイル）"}]
    assert key not in r.text                       # キー自体は応答に含まれない


def test_bedrock_models_filters_inactive_and_non_anthropic(monkeypatch):
    """ACTIVE でない、または anthropic 系でないプロファイルは除外する（agents.list_bedrock_inference_profiles
    本体の単体確認・fake の control-plane 応答を直接与える）。"""
    fake_data = {"inferenceProfileSummaries": [
        {"inferenceProfileId": "jp.anthropic.claude-sonnet-4-6-v1:0", "inferenceProfileName": "Sonnet 4.6", "status": "ACTIVE"},
        {"inferenceProfileId": "jp.anthropic.claude-old-v1:0", "inferenceProfileName": "Old", "status": "INACTIVE"},
        {"inferenceProfileId": "jp.meta.llama-v1:0", "inferenceProfileName": "Llama", "status": "ACTIVE"},
    ]}

    class _FakeResp:
        def __init__(self, data):
            self._data = json.dumps(data).encode()

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResp(fake_data))
    models, error = agents.list_bedrock_inference_profiles("fake-key")
    assert error is None
    assert models == [{"id": "jp.anthropic.claude-sonnet-4-6-v1:0",
                       "label": "Sonnet 4.6（JP 推論プロファイル）"}]


def test_bedrock_models_endpoint_caches_per_user(monkeypatch):
    """同一ユーザーの2回目の取得は（TTL 内なら）列挙関数を再度呼ばない（プロセス内キャッシュ）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bmodch{sfx}", f"BModCh{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    calls = {"n": 0}

    def fake_list(api_key):
        calls["n"] += 1
        return ([{"id": "jp.anthropic.claude-haiku-4-5-20251001-v1:0", "label": "x"}], None)
    monkeypatch.setattr(system_router, "list_bedrock_inference_profiles", fake_list)
    api._BEDROCK_MODELS_CACHE.pop(uid, None)

    r1 = c.get("/settings/bedrock-models")
    r2 = c.get("/settings/bedrock-models")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert calls["n"] == 1, "2回目がキャッシュを使わず再取得している"


def test_bedrock_models_cache_busted_on_key_change():
    """bedrock_api_key を変更して保存すると、そのユーザーのキャッシュが破棄される
    （古いキーでの列挙結果を TTL 満了まで持ち越さない・過去に有効なキャッシュがあっても消える）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bmodbust{sfx}", f"BModBust{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    fp = api._bedrock_key_fingerprint(None)   # このユーザーはまだ bedrock_api_key 未設定
    api._BEDROCK_MODELS_CACHE[uid] = (time.monotonic() + 10_000, fp, [{"id": "stale", "label": "stale"}], None)
    r = c.put("/settings", json={"bedrock_api_key": f"new-key-{sfx}"})
    assert r.status_code == 200, r.text
    assert uid not in api._BEDROCK_MODELS_CACHE


def test_bedrock_settings_rejects_well_formed_unverified_model_id():
    """RV MED（2026-07-15・①の回帰テスト＝必須）: 推論プロファイル ID の形式（region prefix +
    anthropic + バージョン付き）に一致するだけの ID は、verify/列挙で実在確認していなければ 422。

    旧実装は `BEDROCK_MODEL_ID_RE.fullmatch` の形式一致だけで許可していたため、
    `jp.anthropic.not-a-real-model-v999:999` のような形だけ正しい架空 ID が verify を経ずに保存でき、
    チャット/グラフQA/グラフ抽出が Bedrock 4xx で全滅する実害があった（Codex RV 指摘）。
    実在確認済みの ID を保存する経路は `test_bedrock_settings_accepts_model_id_after_successful_verify`／
    `test_bedrock_settings_accepts_model_id_after_successful_list` を参照。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bmodfmt{sfx}", f"BModFmt{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    dynamic_id = "us.anthropic.claude-sonnet-4-6-20260115-v1:0"   # 形式は正しいが verify/列挙していない
    r = c.put("/settings", json={"bedrock_model": dynamic_id})
    assert r.status_code == 422, r.text
    assert "選択肢" in r.json().get("detail", "")

    r2 = c.put("/settings", json={"bedrock_model": "not-a-real-model-string"})   # 形式も不正
    assert r2.status_code == 422, r2.text


def test_bedrock_settings_accepts_model_id_after_successful_verify(monkeypatch):
    """verify（POST /settings/bedrock-models/verify）で実在確認できた ID は、以後 PUT /settings で
    保存でき GET /settings に反映される（②の核心回帰＝実在確認済みIDの保存経路そのもの）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bmodvok{sfx}", f"BModVOk{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    assert c.put("/settings", json={"bedrock_api_key": f"key-{sfx}"}).status_code == 200
    dynamic_id = "us.anthropic.claude-sonnet-4-6-20260115-v1:0"
    monkeypatch.setattr(agents.BedrockProvider, "probe",
                        lambda self, timeout=None, max_tokens=16: (True, ""))
    rv = c.post("/settings/bedrock-models/verify", json={"model_id": dynamic_id})
    assert rv.status_code == 200 and rv.json()["ok"] is True, rv.text

    r = c.put("/settings", json={"bedrock_model": dynamic_id})
    assert r.status_code == 200, r.text
    assert c.get("/settings").json()["bedrock_model"] == dynamic_id


def test_bedrock_settings_accepts_model_id_after_successful_list(monkeypatch):
    """GET /settings/bedrock-models の動的列挙で返ってきた ID も、以後 PUT /settings で保存できる
    （列挙成功時に `store.add_bedrock_verified_models` へ記録する経路の確認）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bmodlok{sfx}", f"BModLOk{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    listed_id = "jp.anthropic.claude-sonnet-4-6-20260101-v1:0"

    def fake_list(api_key):
        return ([{"id": listed_id, "label": "Sonnet 4.6"}], None)
    monkeypatch.setattr(system_router, "list_bedrock_inference_profiles", fake_list)
    api._BEDROCK_MODELS_CACHE.pop(uid, None)

    r0 = c.get("/settings/bedrock-models")
    assert r0.status_code == 200 and r0.json()["models"], r0.text

    r = c.put("/settings", json={"bedrock_model": listed_id})
    assert r.status_code == 200, r.text
    assert c.get("/settings").json()["bedrock_model"] == listed_id


def test_bedrock_settings_resaving_current_value_is_grandfathered():
    """RV MED: 現在保存中の値をそのまま再送する no-op 保存は、verify/列挙していなくても 422 にしない
    （grandfather）。旧デプロイで保存済みだった未検証値の再保存にも当てはまる（store 直書きで再現・
    allowlist 検証は API 層のみなので直接書き込みは通る）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bmodgf{sfx}", f"BModGf{sfx}"
    _mk_user(uid, pw)
    legacy_id = "us.anthropic.claude-legacy-saved-v1:0"
    store.update_settings(uid, bedrock_model=legacy_id)
    c = _login(uid, pw)
    r = c.put("/settings", json={"bedrock_model": legacy_id, "system_prompt": "変更ついで"})
    assert r.status_code == 200, r.text
    assert c.get("/settings").json()["bedrock_model"] == legacy_id


def test_bedrock_settings_null_model_field_does_not_change_saved_value():
    """RV MED（F8・2026-07-16再検証）: bedrock_model に**明示的に JSON null** を送っても変更しない
    （フィールド省略ではなく `"bedrock_model": null` を実際に送る形で確認・既存セマンティクス
    非破壊の確認をより厳密にする）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bmodnull{sfx}", f"BModNull{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    static_id = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert c.put("/settings", json={"bedrock_model": static_id}).status_code == 200
    r = c.put("/settings", json={"system_prompt": "他フィールドだけ変更", "bedrock_model": None})
    assert r.status_code == 200, r.text
    assert c.get("/settings").json()["bedrock_model"] == static_id


def test_settings_put_ignores_client_supplied_bedrock_verified_models():
    """RV MED: `SettingsReq` に `bedrock_verified_models` フィールドは無い＝クライアントから偽造でき
    ない（pydantic は未知フィールドを無視する）。混入させても保存内容には一切反映されず、その ID を
    allowlist へ紛れ込ませることもできない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bmodforge{sfx}", f"BModForge{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    forged = "jp.anthropic.forged-model-v1:0"
    r = c.put("/settings", json={"system_prompt": "x", "bedrock_verified_models": [forged]})
    assert r.status_code == 200, r.text
    r2 = c.put("/settings", json={"bedrock_model": forged})
    assert r2.status_code == 422, "偽造した bedrock_verified_models が allowlist に反映されている"


def test_public_settings_exposes_bedrock_model_known_and_label(monkeypatch):
    """`_public_settings` が `bedrock_model_known`（静的∪verified 済みか）と `bedrock_model_label`
    （整形済みラベル）を返す（web/settings.js の legacy 判定・表示に使う）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bmodknown{sfx}", f"BModKnown{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    s0 = c.get("/settings").json()
    assert s0["bedrock_model_known"] is True    # 既定値は静的 choices の1つ
    assert s0["bedrock_model_label"]

    legacy_id = "us.anthropic.claude-unknown-legacy-v1:0"
    store.update_settings(uid, bedrock_model=legacy_id)   # 旧デプロイ由来の未検証値を再現
    s1 = c.get("/settings").json()
    assert s1["bedrock_model_known"] is False

    assert c.put("/settings", json={"bedrock_api_key": f"key-{sfx}"}).status_code == 200
    monkeypatch.setattr(agents.BedrockProvider, "probe",
                        lambda self, timeout=None, max_tokens=16: (True, ""))
    rv = c.post("/settings/bedrock-models/verify", json={"model_id": legacy_id})
    assert rv.status_code == 200 and rv.json()["ok"] is True, rv.text
    s2 = c.get("/settings").json()
    assert s2["bedrock_model_known"] is True
    assert s2["bedrock_model_label"] == agents._bedrock_profile_label(legacy_id, "")


def test_add_bedrock_verified_models_dedups_and_upserts_missing_row():
    """`store.add_bedrock_verified_models`（`_bedrock_model_id_valid` の正本）の単体確認: 重複除去・
    単調保持（monotonic・R4-1 是正＝既存 ID は絶対に evict されず、並び替えもしない）・ユーザーの
    行が無くても動く（専用テーブル `bedrock_verified_models` への upsert・F1/F4/F6 是正で
    `user_settings` から分離済み）ことを確認する。cap（満杯時の挙動）は
    `test_add_bedrock_verified_models_is_monotonic_never_evicts_and_reports_capacity_full` 参照。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"bvstore{sfx}"
    register_test_uid(uid)   # users 行は作らない（行が無いケースの確認）が teardown 対象には入れる

    retained1 = store.add_bedrock_verified_models(uid, ["m1", "m2", "m1"])
    assert retained1 == ["m1", "m2"]
    assert store.get_settings(uid)["bedrock_verified_models"] == ["m1", "m2"]

    # 既に記録済みの ID を再確認しても、順序も内容も変わらない（LRU 的な並び替えは R4-1 で廃止）。
    retained2 = store.add_bedrock_verified_models(uid, ["m1"])
    assert retained2 == ["m1"]
    assert store.get_settings(uid)["bedrock_verified_models"] == ["m1", "m2"]

    retained3 = store.add_bedrock_verified_models(uid, ["m3", "m4"])
    assert retained3 == ["m3", "m4"]
    assert store.get_settings(uid)["bedrock_verified_models"] == ["m1", "m2", "m3", "m4"]


def test_add_bedrock_verified_models_concurrent_first_writes_do_not_lose_updates():
    """RV MED（F1・2026-07-16再検証→N6・3巡目→R4-5・4巡目→L6・5巡目→C3・6巡目で観測方式を作り直し）:
    行ロック直列化の直接確認（行が既にあるケース）。

    旧々版は `threading.Barrier` で2スレッドを「ほぼ同時」に走らせるだけで、直列化の有無を判別
    できていなかった。旧版（N6）は「1秒待ってスレッドが完了していないこと」を確認する負の
    ヒューリスティックへ改善したが、これも弱い確認だった。R4-5 で `pg_stat_activity.
    wait_event_type='Lock'` の bounded polling へ改善したが、テーブル名だけのクエリ文字列一致
    フィルタでは、同じテーブルを触る**別の**バックエンドがたまたま同時にロック待ちになった場合に
    対象ワーカーを一意に特定できなかった（Codex RV 指摘・L6）。L6 は holder/monitor 自身の pid
    除外で対応したが、**第三の無関係なバックエンド**まではまだ誤認しうる（Codex RV 指摘・C3）。
    ここでは `pg_blocking_pids(candidate_pid)` に holder の pid が含まれるバックエンドだけを候補に
    する `_wait_until_blocked_by`（「holder に実際にブロックされている」という因果関係そのものを
    確認）を使う。両方の ID が union として残っていれば、スレッドの書込みが保持側の commit 後の
    最新状態を読んでいる＝直列化が効いている証拠になる。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"bvrace{sfx}"
    register_test_uid(uid)

    # 行を先に実体化する（このテストが確認したいのは「行が既にある」状態での FOR UPDATE 直列化。
    # 「行が無い」状態からの初回競合は test_add_bedrock_verified_models_concurrent_initial_row_creation
    # 参照）。
    store.add_bedrock_verified_models(uid, ["seed"])

    holder_conn = store._connect()
    monitor_conn = store._connect()
    try:
        holder_pid = holder_conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]

        holder_conn.execute(
            "SELECT ids FROM bedrock_verified_models WHERE user_id=%s FOR UPDATE", (uid,))

        thread_done = threading.Event()
        errors: list[Exception] = []

        def worker():
            try:
                store.add_bedrock_verified_models(uid, ["thread-id"])
            except Exception as e:   # pragma: no cover - 診断用（assert で検出する）
                errors.append(e)
            finally:
                thread_done.set()

        t = threading.Thread(target=worker)
        t.start()
        worker_pid = _wait_until_blocked_by(monitor_conn, "bedrock_verified_models", holder_pid, timeout=5)
        assert worker_pid is not None, \
            "スレッドが holder にブロックされていることを一意に観測できなかった（直列化が効いていない/検知できない）"
        assert not thread_done.is_set(), \
            "ロック待ちを観測した直後にスレッドが完了扱いになっている（矛盾・診断ロジック不整合）"

        holder_conn.execute(
            "UPDATE bedrock_verified_models SET ids = ids || '[\"test-id\"]'::jsonb WHERE user_id=%s",
            (uid,))
        holder_conn.commit()   # ここでロック解放＝スレッドの FOR UPDATE が進める

        assert thread_done.wait(timeout=5), "commit 後もスレッドが完了しなかった"
        t.join(timeout=5)
        assert not t.is_alive()
        assert not errors, f"スレッドで例外: {errors}"
    finally:
        holder_conn.close()
        monitor_conn.close()

    saved = set(store.get_settings(uid)["bedrock_verified_models"])
    assert saved == {"seed", "test-id", "thread-id"}, \
        f"直列化が効いていない（holder の commit 後の状態をスレッドが読めていない）: {saved}"


def test_add_bedrock_verified_models_concurrent_initial_row_creation():
    """R4-5（LOW・2026-07-16 Codex RV 4巡目再検証）→L6（5巡目再検証）→C3（6巡目再検証で観測方式を
    作り直し）: 「行が最初から無い」ケースの並行呼び出し variant。

    旧版（R4-5）はこの競合（Postgres 自身の `INSERT ... ON CONFLICT` 一意制約解決）を
    「decisiveに観測するのは構造的に難しい」として、Barrier＋複数試行の確率的方式に留めていた
    （Codex RV 指摘・見せかけの直列実行を排除しきれない false green の余地が残る）。ここでは
    決定的に再構成する: テスト側が対象行への**未コミット** `INSERT`（`ON CONFLICT` 無し）を先に
    発行して保持する→worker の `add_bedrock_verified_models` の1文目（`INSERT ... ON CONFLICT (user_id)
    DO NOTHING`）は、この未コミット行との一意制約競合の解決待ち（speculative insertion 待ち・
    `wait_event_type='Lock'`）でブロックされる→`_wait_until_blocked_by`（`pg_blocking_pids` で
    holder に実際にブロックされていることまで確認・C3）で一意に観測してから commit する→union
    assert。
    """
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"bvrace0{sfx}"
    register_test_uid(uid)

    holder_conn = store._connect()
    monitor_conn = store._connect()
    try:
        holder_pid = holder_conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]

        # 行が無い状態から、テスト側が先に（ON CONFLICT 無しの）INSERT を発行してコミットせず
        # 保持する＝「この行を作ろうとしている最中」の未確定状態を作る。
        holder_conn.execute(
            "INSERT INTO bedrock_verified_models (user_id, ids) VALUES (%s, '[]'::jsonb)", (uid,))

        thread_done = threading.Event()
        errors: list[Exception] = []

        def worker():
            try:
                store.add_bedrock_verified_models(uid, ["thread-id"])
            except Exception as e:   # pragma: no cover - 診断用（assert で検出する）
                errors.append(e)
            finally:
                thread_done.set()

        t = threading.Thread(target=worker)
        t.start()
        worker_pid = _wait_until_blocked_by(monitor_conn, "bedrock_verified_models", holder_pid, timeout=5)
        assert worker_pid is not None, \
            "worker の INSERT が holder にブロックされていることを一意に観測できなかった " \
            "（行無し初回競合の直列化を確認できない）"
        assert not thread_done.is_set(), \
            "ロック待ちを観測した直後にスレッドが完了扱いになっている（矛盾・診断ロジック不整合）"

        holder_conn.execute(
            "UPDATE bedrock_verified_models SET ids = ids || '[\"test-id\"]'::jsonb WHERE user_id=%s",
            (uid,))
        holder_conn.commit()   # ここでロック解放＝worker の INSERT ON CONFLICT が解決へ進める

        assert thread_done.wait(timeout=5), "commit 後もスレッドが完了しなかった"
        t.join(timeout=5)
        assert not t.is_alive()
        assert not errors, f"スレッドで例外: {errors}"
    finally:
        holder_conn.close()
        monitor_conn.close()

    saved = set(store.get_settings(uid)["bedrock_verified_models"])
    assert saved == {"test-id", "thread-id"}, \
        f"行が無い状態からの初回競合で片方の記録が消えている: {saved}"


# ===== N6（2026-07-16 Codex RV 3巡目再検証）: (a)-(d) 追加回帰 =====

def test_bedrock_verify_key_changed_during_probe_is_not_recorded(monkeypatch):
    """N6(a): probe 実行中（ここでは probe の副作用として模す）にこのユーザーのキーが変更されて
    コミットされると、検証結果は記録されない（ok:false・「設定が変更されました」・N1 の atomic
    fingerprint 確認が効いていることの直接確認）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvmidkey{sfx}", f"BVMidKey{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    assert c.put("/settings", json={"bedrock_api_key": f"old-key-{sfx}"}).status_code == 200

    def fake_probe(self, timeout=None, max_tokens=16):
        # probe の「実 I/O 中」を模して、その最中にこのユーザーの鍵が変わってコミットされる。
        assert c.put("/settings", json={"bedrock_api_key": f"new-key-{sfx}"}).status_code == 200
        return True, ""
    monkeypatch.setattr(agents.BedrockProvider, "probe", fake_probe)

    r = c.post("/settings/bedrock-models/verify",
               json={"model_id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False
    assert d["error"] == "設定が変更されました。もう一度お試しください"
    assert store.get_settings(uid)["bedrock_verified_models"] == []


def test_bedrock_models_cache_hit_key_changed_before_record_returns_empty(monkeypatch):
    """N6(b)→R4-5（4巡目再検証で assert を強化）: キャッシュヒット経路で、記録
    （`store.add_bedrock_verified_models`）を呼ぶ直前にキーが変わってコミットされた場合、記録
    されず空リスト応答になる（fresh 経路と同じ意味論・N2）。応答だけでなく、durable な記録が
    実際に行われていないこと・キャッシュに汚染された entry が残っていないことまで確認する。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvcachekey{sfx}", f"BVCacheKey{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    old_key = f"old-key-{sfx}"
    assert c.put("/settings", json={"bedrock_api_key": old_key}).status_code == 200

    fp = api._bedrock_key_fingerprint(old_key)
    target_id = "jp.anthropic.claude-sonnet-4-6-20260101-v1:0"
    cached_models = [{"id": target_id, "label": "x"}]
    api._BEDROCK_MODELS_CACHE[uid] = (time.monotonic(), fp, cached_models, None)
    gen_before = api._BEDROCK_MODELS_CACHE_GEN.get(uid, 0)

    real_add = store.add_bedrock_verified_models

    def fake_add(user_id, ids, expected_key_fp=None):
        # 「記録」しようとする直前で**別リクエスト**がキーを変更してコミットしたことを模す。
        # `settings_put`（PUT /settings）を実際に叩く＝キャッシュ pop・世代 increment という
        # 本物の副作用込みで再現する（`store.update_settings` を直接呼ぶだけだとこれらの
        # router 層の副作用が起きず、後続の assert が現実と異なるものになってしまうため）。
        assert c.put("/settings", json={"bedrock_api_key": f"new-key-{sfx}"}).status_code == 200
        return real_add(user_id, ids, expected_key_fp=expected_key_fp)
    monkeypatch.setattr(store, "add_bedrock_verified_models", fake_add)

    r = c.get("/settings/bedrock-models")
    assert r.status_code == 200, r.text
    assert r.json() == {"models": [], "error": "設定が変更されました。もう一度お試しください"}

    # durable 未記録: 専用テーブルに target_id が記録されていない。
    assert target_id not in store.get_settings(uid)["bedrock_verified_models"]
    # PUT でも 422（記録されていないので allowlist に無い）。
    r2 = c.put("/settings", json={"bedrock_model": target_id})
    assert r2.status_code == 422, r2.text
    # キャッシュ状態: fake_add 内の PUT が既にキャッシュを pop・世代を increment 済み（本物の
    # 「キー変更」副作用）。この GET 自身の空リスト結果がそれを上書きして復活させていないこと
    # （＝汚染された entry が残っていないこと）を確認する。
    assert uid not in api._BEDROCK_MODELS_CACHE, "この GET の空リスト結果でキャッシュが汚染された"
    assert api._BEDROCK_MODELS_CACHE_GEN.get(uid, 0) > gen_before


def test_bedrock_models_add_verified_exception_leaves_cache_unpolluted(monkeypatch):
    """N6(c): 記録（`store.add_bedrock_verified_models`）が例外を投げたら列挙は 500 になるが、
    キャッシュには汚染された entry が残らない（記録→キャッシュ書込の順序どおり、記録の例外で
    それより後のコードに到達しないため）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvaddboom{sfx}", f"BVAddBoom{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    def fake_list(api_key):
        return ([{"id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0", "label": "x"}], None)
    monkeypatch.setattr(system_router, "list_bedrock_inference_profiles", fake_list)
    api._BEDROCK_MODELS_CACHE.pop(uid, None)

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(store, "add_bedrock_verified_models", boom)

    r = c.get("/settings/bedrock-models")
    assert r.status_code == 500
    assert uid not in api._BEDROCK_MODELS_CACHE, "記録失敗にもかかわらずキャッシュに entry が残った"


def test_bedrock_models_cache_hit_record_call_happens_with_lock_released(monkeypatch):
    """N6(d)（親検収 cache-lock 是正の直接確認）: キャッシュヒット経路で
    `store.add_bedrock_verified_models` を呼ぶ時点では、`_BEDROCK_MODELS_CACHE_LOCK` は既に
    解放されている（保持したまま呼ぶと、他ユーザーの列挙キャッシュ読取が行ロック待ちでブロック
    されてしまうため）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvlockchk{sfx}", f"BVLockChk{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    key = f"key-{sfx}"
    assert c.put("/settings", json={"bedrock_api_key": key}).status_code == 200

    fp = api._bedrock_key_fingerprint(key)
    cached_models = [{"id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0", "label": "x"}]
    api._BEDROCK_MODELS_CACHE[uid] = (time.monotonic(), fp, cached_models, None)

    observed = {}

    def spy_add(user_id, ids, expected_key_fp=None):
        observed["locked"] = api._BEDROCK_MODELS_CACHE_LOCK.locked()
        return list(ids)
    monkeypatch.setattr(store, "add_bedrock_verified_models", spy_add)

    r = c.get("/settings/bedrock-models")
    assert r.status_code == 200, r.text
    assert observed.get("locked") is False, "add_bedrock_verified_models がロック保持中に呼ばれている"


# ===== R4（2026-07-16 Codex RV 4巡目再検証）: cap 単調保持・キャッシュ世代カウンタ =====

def test_add_bedrock_verified_models_is_monotonic_never_evicts_and_reports_capacity_full():
    """R4-1（MED・2026-07-16 Codex RV 4巡目再検証・最重要）: cap による「古い方から捨てる」方式
    （LRU）は、一度『保存可能』と返した ID を後から取り消しうる実害があった（repro: キャッシュに
    200件→verify V 成功（既存1件が evict されて V 記録）→次のキャッシュヒット再記録で旧200件が
    復活し V が evict→V の PUT が 422）。単調保持（monotonic）への是正を、`retained` 返り値・
    エンドポイント応答・durable 状態・後続 PUT 可否まで通しで確認する: 既存 ID は新規追加で絶対に
    消えない／容量が満杯なら新規 ID は ok:false で正直に失敗する／既存の（retained な）ID は
    引き続き PUT できる。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bvcapfull{sfx}", f"BVCapFull{sfx}"
    _mk_user(uid, pw)

    existing = [f"cap-model-{i}" for i in range(200)]
    retained0 = store.add_bedrock_verified_models(uid, existing)
    assert retained0 == existing
    assert store.get_settings(uid)["bedrock_verified_models"] == existing   # 満杯（200件）

    # 満杯の状態で「再確認」（列挙のキャッシュヒット再記録を模す）しても、既存分は一切消えない。
    retained1 = store.add_bedrock_verified_models(uid, existing)
    assert retained1 == existing
    assert store.get_settings(uid)["bedrock_verified_models"] == existing

    # 満杯の状態で新規 ID を追加しようとすると、容量が無いので入らない（evict もしない＝既存不変）。
    retained2 = store.add_bedrock_verified_models(uid, ["new-model-over-cap"])
    assert retained2 == []
    saved = store.get_settings(uid)["bedrock_verified_models"]
    assert saved == existing   # 既存は完全に不変（順序も内容も）
    assert "new-model-over-cap" not in saved

    c = _login(uid, pw)
    # 既存の1件は引き続き PUT できる（evict されていない＝正当な verified ID）。
    r_existing = c.put("/settings", json={"bedrock_model": existing[0]})
    assert r_existing.status_code == 200, r_existing.text
    assert c.get("/settings").json()["bedrock_model"] == existing[0]

    # 満杯なので入らなかった新規 ID の PUT は 422（正直に失敗する＝ok:true で保存不能の握りつぶしを
    # 作らない、という中核契約を PUT 側からも確認）。
    r_new = c.put("/settings", json={"bedrock_model": "new-model-over-cap"})
    assert r_new.status_code == 422, r_new.text


def test_bedrock_verify_returns_capacity_full_when_store_is_at_max(monkeypatch):
    """R4-1: 保存枠（200件）が満杯の状態で新規 ID を verify すると、probe 自体は成功しても
    ok:false・専用メッセージ（「設定が変更されました」ではなく容量不足であることが分かる文言）を
    返す（ok:true で保存不能、を作らない）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvcapverify{sfx}", f"BVCapVerify{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    assert c.put("/settings", json={"bedrock_api_key": f"key-{sfx}"}).status_code == 200

    existing = [f"cap-model-{i}" for i in range(200)]
    store.add_bedrock_verified_models(uid, existing)   # 保存枠を満杯にしておく

    monkeypatch.setattr(agents.BedrockProvider, "probe",
                        lambda self, timeout=None, max_tokens=16: (True, ""))
    r = c.post("/settings/bedrock-models/verify",
               json={"model_id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False
    assert d["error"] == "検証済みモデルIDの保存枠（200件）に達しています"


def test_bedrock_verify_static_model_id_succeeds_even_when_dynamic_capacity_is_full(monkeypatch):
    """L2（LOW・2026-07-16 Codex RV 5巡目再検証）: 静的 choices（`_BEDROCK_MODEL_IDS`）は
    `_bedrock_model_id_valid` が無条件で受理するため、動的な実在確認済みテーブルが満杯でも verify
    は ok:false にならない（静的 ID は記録する必要が無い＝記録しない・記録しないので容量を消費も
    しない）。旧実装は静的 ID もテーブルへ記録しようとしていたため、動的分で容量が満杯だと
    「保存枠が足りません」という嘘のエラーになっていた（中核契約とは無関係の誤検知＝実害）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvl2verify{sfx}", f"BVL2Verify{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    assert c.put("/settings", json={"bedrock_api_key": f"key-{sfx}"}).status_code == 200

    existing = [f"cap-model-{i}" for i in range(200)]
    store.add_bedrock_verified_models(uid, existing)   # 動的分で保存枠を満杯にしておく

    static_id = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
    monkeypatch.setattr(agents.BedrockProvider, "probe",
                        lambda self, timeout=None, max_tokens=16: (True, ""))
    r = c.post("/settings/bedrock-models/verify", json={"model_id": static_id})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "id": static_id, "label": agents._bedrock_profile_label(static_id, "")}

    # 記録はスキップされている（テーブルは動的200件のまま・静的IDは含まれない）。
    saved = store.get_settings(uid)["bedrock_verified_models"]
    assert static_id not in saved
    assert len(saved) == 200

    # PUT は静的なので無条件で成功する（記録の有無に関係なく allowlist に元から入っている）。
    r2 = c.put("/settings", json={"bedrock_model": static_id})
    assert r2.status_code == 200, r2.text


def test_bedrock_models_listing_does_not_consume_capacity_for_static_ids(monkeypatch):
    """L2: 列挙結果に静的 choices が含まれていても、記録（`store.add_bedrock_verified_models`）へは
    渡さない＝動的分の容量を消費しない（静的は `_BEDROCK_MODEL_IDS` で無条件に受理されるため記録
    不要）。応答には静的・動的の両方が引き続き含まれる（`keep = retained ∪ _BEDROCK_MODEL_IDS`）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvl2list{sfx}", f"BVL2List{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    static_id = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
    dynamic_id = "us.anthropic.claude-sonnet-4-6-20260115-v1:0"

    def fake_list(api_key):
        return ([{"id": static_id, "label": "static"}, {"id": dynamic_id, "label": "dynamic"}], None)
    monkeypatch.setattr(system_router, "list_bedrock_inference_profiles", fake_list)
    api._BEDROCK_MODELS_CACHE.pop(uid, None)

    r = c.get("/settings/bedrock-models")
    assert r.status_code == 200, r.text
    ids = {m["id"] for m in r.json()["models"]}
    assert ids == {static_id, dynamic_id}   # 応答には両方残る（静的は常に keep 対象）

    saved = store.get_settings(uid)["bedrock_verified_models"]
    assert static_id not in saved   # 記録テーブルには動的分だけが入る
    assert dynamic_id in saved


def test_bedrock_models_static_only_listing_rechecks_fingerprint(monkeypatch):
    """C1（LOW・2026-07-16 Codex RV 6巡目再検証）: 列挙結果が全て静的 choices の場合、記録処理
    （`store.add_bedrock_verified_models`）自体はスキップされるが、それでも現在キーの fingerprint
    再確認は行う。取得中にキーが変わっていれば、非静的経路と同じ意味論で
    `{"models": [], "error": "設定が変更されました。もう一度お試しください"}` を返す（静的は
    PUT が無条件受理するため保存契約自体は破れないが、体験を揃える）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvc1list{sfx}", f"BVC1List{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    assert c.put("/settings", json={"bedrock_api_key": f"old-key-{sfx}"}).status_code == 200

    static_id = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"

    def fake_list(api_key):
        # 「fetch 完了直後・記録判定の前」にキーが変わってコミットされたことを模す。
        assert c.put("/settings", json={"bedrock_api_key": f"new-key-{sfx}"}).status_code == 200
        return ([{"id": static_id, "label": "static"}], None)
    monkeypatch.setattr(system_router, "list_bedrock_inference_profiles", fake_list)
    api._BEDROCK_MODELS_CACHE.pop(uid, None)

    r = c.get("/settings/bedrock-models")
    assert r.status_code == 200, r.text
    assert r.json() == {"models": [], "error": "設定が変更されました。もう一度お試しください"}


def test_bedrock_verify_static_model_id_rechecks_fingerprint_after_probe(monkeypatch):
    """C1（LOW・2026-07-16 Codex RV 6巡目再検証）: 静的 choices の verify ファストパス（記録処理を
    スキップする経路）でも、probe 完了後に現在キーの fingerprint を再確認する。probe 実行中に
    キーが変わっていれば、非静的経路と同じ意味論で ok:false・「設定が変更されました」を返す。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvc1verify{sfx}", f"BVC1Verify{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    assert c.put("/settings", json={"bedrock_api_key": f"old-key-{sfx}"}).status_code == 200

    static_id = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"

    def fake_probe(self, timeout=None, max_tokens=16):
        assert c.put("/settings", json={"bedrock_api_key": f"new-key-{sfx}"}).status_code == 200
        return True, ""
    monkeypatch.setattr(agents.BedrockProvider, "probe", fake_probe)

    r = c.post("/settings/bedrock-models/verify", json={"model_id": static_id})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False
    assert d["error"] == "設定が変更されました。もう一度お試しください"


def test_bedrock_models_cache_write_skipped_after_generation_bump(monkeypatch):
    """R4-2（LOW・2026-07-16 Codex RV 4巡目再検証）: 記録（`store.add_bedrock_verified_models`）
    成功後・キャッシュ書込前の間に、別リクエストがキーを変更（＝世代カウンタが increment）すると、
    その古いリクエストの遅延書込はスキップされる（新しい有効な cache entry を潰さない・直接関数
    呼び出しで決定的に確認する）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvgenskip{sfx}", f"BVGenSkip{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    assert c.put("/settings", json={"bedrock_api_key": f"key-{sfx}"}).status_code == 200

    def fake_list(api_key):
        return ([{"id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0", "label": "x"}], None)
    monkeypatch.setattr(system_router, "list_bedrock_inference_profiles", fake_list)
    api._BEDROCK_MODELS_CACHE.pop(uid, None)

    real_add = store.add_bedrock_verified_models

    def fake_add(user_id, ids, expected_key_fp=None):
        retained = real_add(user_id, ids, expected_key_fp=expected_key_fp)
        # 記録成功「後」・キャッシュ書込「前」の窓を模して、ここで別リクエストがキーを変更する
        # （世代カウンタが増える＝この GET の遅延書込はスキップされるべき）。
        assert c.put("/settings", json={"bedrock_api_key": f"new-key-{sfx}"}).status_code == 200
        return retained
    monkeypatch.setattr(store, "add_bedrock_verified_models", fake_add)

    r = c.get("/settings/bedrock-models")
    assert r.status_code == 200, r.text
    assert r.json()["models"], "fetch/記録自体は成功しているはず（応答は stale ではない）"
    assert uid not in api._BEDROCK_MODELS_CACHE, \
        "世代が進んだのに古いリクエストの書込でキャッシュが汚染された"


def test_bedrock_models_generation_captured_before_key_read(monkeypatch):
    """L1（LOW・2026-07-16 Codex RV 5巡目再検証）: 上の
    `test_bedrock_models_cache_write_skipped_after_generation_bump` は「記録成功後」に世代を
    進める（＝N1 の fingerprint 再照合が先に不一致を検知して早期 return するため、世代チェックの
    タイミング自体のバグは検知できない＝旧実装でも通ってしまう false green だった）。

    L1 が是正するのは別の隙間: 世代の捕捉が「キー読取の**後**」だと、このリクエストがキーを
    読んだ直後・まだ世代を捕捉する前に、別リクエストが世代だけを進めた場合、このリクエストは
    （実際には古いキーで処理しているのに）既に進んだ後の世代を自分の基準として捕捉してしまい、
    書込直前チェックが「何も変わっていない」と誤認する。ここでは記録対象を空応答にして N1 の
    fingerprint チェック自体を経由させず（記録するものが無いので `add_bedrock_verified_models` は
    呼ばれない）、世代チェックだけの正しさを単体で確認する。`store.get_settings`（キー読取）を
    1回だけフックし、その呼び出しの**直後**に世代を進める副作用を仕込む＝世代捕捉がキー読取より
    前であれば、この副作用より前に古い世代を掴んでいるはずなので、書込は必ずスキップされる。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvl1{sfx}", f"BVL1{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    assert c.put("/settings", json={"bedrock_api_key": f"key-{sfx}"}).status_code == 200
    api._BEDROCK_MODELS_CACHE.pop(uid, None)
    gen_before = api._BEDROCK_MODELS_CACHE_GEN.get(uid, 0)

    real_get_settings = store.get_settings
    bumped = {"done": False}

    def fake_get_settings(user_id="admin"):
        result = real_get_settings(user_id)
        if user_id == uid and not bumped["done"]:
            bumped["done"] = True
            # 「キー読取の直後」に別リクエストがキーを変更してコミットしたことを模す
            # （世代カウンタの increment だけが本質＝settings_put の実処理を経由する必要はない）。
            with api._BEDROCK_MODELS_CACHE_LOCK:
                api._BEDROCK_MODELS_CACHE_GEN[uid] = api._BEDROCK_MODELS_CACHE_GEN.get(uid, 0) + 1
        return result
    monkeypatch.setattr(store, "get_settings", fake_get_settings)

    def fake_list(api_key):
        return ([], "接続に失敗しました（模擬）")   # 空応答＝ add_bedrock_verified_models は呼ばれない
    monkeypatch.setattr(system_router, "list_bedrock_inference_profiles", fake_list)

    r = c.get("/settings/bedrock-models")
    assert r.status_code == 200, r.text
    assert api._BEDROCK_MODELS_CACHE_GEN.get(uid, 0) == gen_before + 1   # 世代は確かに進んだ
    assert uid not in api._BEDROCK_MODELS_CACHE, \
        "世代捕捉がキー読取より後だと、古い世代を掴んで書き込んでしまう（L1 regression）"


def test_add_bedrock_verified_models_does_not_touch_user_settings_row():
    """RV MED（F4・2026-07-16再検証）: 行が無いユーザーに `add_bedrock_verified_models` を呼んでも
    `user_settings` には一切触れない（`agent` 列既定 'heuristic' が実体化しない＝
    `SHERPA_AGENT=bedrock` のような env フォールバックが、列挙/検証しただけで壊れない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"bvnorow{sfx}"
    register_test_uid(uid)

    store.add_bedrock_verified_models(uid, ["m1"])
    s = store.get_settings(uid)
    assert s["bedrock_verified_models"] == ["m1"]
    assert s["agent"] is None, "add_bedrock_verified_models が user_settings に行を作ってしまっている（F4 regression）"


def test_update_settings_ignores_bedrock_verified_models_kwarg():
    """RV MED（F6・2026-07-16再検証）: `update_settings(uid, bedrock_verified_models=[...])` を
    直接呼んでも無視される（`_SETTINGS_FIELDS` に含まれない＝専用テーブルにも `user_settings` にも
    書かれない・「記録は verify/列挙成功のみ」という不変条件がコード構造で保証される）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bvkwarg{sfx}", f"BVKwarg{sfx}"
    _mk_user(uid, pw)
    store.update_settings(uid, bedrock_verified_models=["forged-via-update-settings"])
    assert store.get_settings(uid)["bedrock_verified_models"] == []


# ===== Codex RV「要修正」3件（MEDIUM・2026-07-03）=====

def test_bedrock_model_id_valid_membership_only_and_rejects_whitespace_variants():
    """RV MED（2026-07-15）: `_bedrock_model_id_valid` は静的 choices／verified／current の完全一致
    membership 判定のみ（正規表現の形式一致は使わない・シグネチャ変更）。`good` 自体が verified/current
    に含まれていても、末尾改行・前後空白つきの変種は別の文字列として扱われ常に無効
    （旧 RV MEDIUM 1「`.match()` の末尾改行許容」の教訓は、正規表現を使わなくなった今も
    「完全一致でなければ弾く」という形で維持されていることの確認）。"""
    good = "us.anthropic.claude-sonnet-4-6-20260115-v1:0"
    assert api._bedrock_model_id_valid(good, [good], None) is True     # verified 経由
    assert api._bedrock_model_id_valid(good, [], good) is True         # grandfather（現在保存中の値）経由
    assert api._bedrock_model_id_valid(good, [], None) is False        # どこにも無ければ無効
    for bad in (good + "\n", good + "\r\n", good + "\n\n", " " + good, good + " ", "\n" + good):
        assert api._bedrock_model_id_valid(bad, [good], good) is False, repr(bad)


def test_settings_rejects_bedrock_model_with_trailing_newline_or_whitespace():
    """RV MEDIUM 1 系: PUT /settings 経由でも同じ（末尾改行/前後空白つき ID は 422）。
    RV MED（2026-07-15）再検証: 検証対象を静的 choices の1つ（ホワイトスペース混入時に「形式不一致」
    ではなく「membership 不一致」で弾かれることを示す）に変更。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bmodnl{sfx}", f"BModNl{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    base = "global.anthropic.claude-haiku-4-5-20251001-v1:0"   # 静的 choices の1つ
    for bad in (base + "\n", " " + base, base + " ", base + "\r\n"):
        r = c.put("/settings", json={"bedrock_model": bad})
        assert r.status_code == 422, f"{bad!r}: got {r.status_code} {r.text}"
    assert c.put("/settings", json={"bedrock_model": base}).status_code == 200   # 正常値は引き続き通る


def test_bedrock_list_error_uses_fixed_phrase_and_never_leaks_key(monkeypatch):
    """RV MEDIUM 2: 上流エラー本文をそのまま返さない。403 は HTTP status ベースの固定日本語文言に
    なり、実キー値は結果の error 文字列に一切混ざらない。"""
    key = "super-secret-bedrock-key-12345"

    def raise_403(*a, **k):
        raise urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", raise_403)

    models, error = agents.list_bedrock_inference_profiles(key)
    assert models == []
    assert error == "認証エラー（403）。API キー/権限を確認してください。"
    assert key not in error


def test_bedrock_list_error_network_failure_uses_fixed_phrase(monkeypatch):
    """RV MEDIUM 2: ネットワーク例外（HTTPError でない）も上流の生メッセージを使わず固定文言。"""
    key = "another-secret-key"

    def raise_timeout(*a, **k):
        raise TimeoutError(f"timed out talking to bedrock with key={key}")   # 万一メッセージにキー混入
    monkeypatch.setattr("urllib.request.urlopen", raise_timeout)

    models, error = agents.list_bedrock_inference_profiles(key)
    assert models == []
    assert error == "接続できませんでした（ネットワークエラー）。"
    assert key not in error


def test_redact_bedrock_secret_strips_key_and_bearer_pattern():
    """RV MEDIUM 2: `_redact_bedrock_secret` 自体の確認（生テキストが万一混ざっても伏せる最後の砦）。"""
    key = "super-secret-key-xyz"
    leaked = f"upstream said: Authorization: Bearer {key} was rejected (key={key})"
    redacted = agents._redact_bedrock_secret(leaked, key)
    assert key not in redacted
    assert "Bearer [REDACTED]" in redacted


def test_redact_bedrock_secret_also_strips_env_key_values(monkeypatch):
    """RV HIGH（2026-07-03再検証）: 呼出元が明示的な key を持たない（env/SigV4 チェーンに委譲した）
    ケースでも、`_BEDROCK_ENV_KEYS`（AWS_BEARER_TOKEN_BEDROCK/ANTHROPIC_AWS_API_KEY）の値が
    メッセージに混入していれば伏せる。呼出元は「実際にどのキーが使われたか」を知らなくてよい。"""
    env_key = "env-bearer-token-abcdef"
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", env_key)
    monkeypatch.delenv("ANTHROPIC_AWS_API_KEY", raising=False)
    leaked = f"upstream said: Authorization: Bearer {env_key} was rejected"
    redacted = agents._redact_bedrock_secret(leaked, None)   # 呼出元は key=None（env 委譲）
    assert env_key not in redacted
    assert "Bearer [REDACTED]" in redacted


# ===== バッチ2・1番（2026-07-03）: 検証つき手動追加（POST /settings/bedrock-models/verify）=====
# 実環境の「接続テストOK・モデル取得は失敗」報告を受け、control-plane 列挙に頼らず、
# ユーザーが分かっているモデルID を実際に1回（max_tokens=1）叩いて検証してから追加できる経路。

def test_bedrock_verify_rejects_malformed_model_id_without_network_call(monkeypatch):
    """形式検証が先＝BEDROCK_MODEL_ID_RE に一致しない ID は probe を1回も呼ばずに ok:false。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvfmt{sfx}", f"BVFmt{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    def fail_if_called(self, *a, **k):
        raise AssertionError("形式不正なのに probe が呼ばれた")
    monkeypatch.setattr(agents.BedrockProvider, "probe", fail_if_called)

    r = c.post("/settings/bedrock-models/verify", json={"model_id": "not-a-valid-model-id"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False
    assert "形式" in d["error"]


def test_bedrock_verify_no_key_returns_ok_false(monkeypatch):
    """API キー未設定（per-user もサーバ env も無し）は ok:false（形式は正しいので probe 手前まで進む）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvnk{sfx}", f"BVNk{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    for k in agents._BEDROCK_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)

    r = c.post("/settings/bedrock-models/verify",
               json={"model_id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False
    assert "API キー" in d["error"]


def test_bedrock_verify_success_returns_id_and_label_uses_max_tokens_1(monkeypatch):
    """実際に1回叩いて成功したら {ok:true, id, label} を返す。max_tokens=1 で呼ばれることも確認する。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvok{sfx}", f"BVOk{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    assert c.put("/settings", json={"bedrock_api_key": f"key-{sfx}"}).status_code == 200

    captured = {}

    def fake_probe(self, timeout=None, max_tokens=16):
        captured["max_tokens"] = max_tokens
        captured["model"] = self.model
        return True, ""
    monkeypatch.setattr(agents.BedrockProvider, "probe", fake_probe)

    model_id = "us.anthropic.claude-sonnet-4-6-20260115-v1:0"
    r = c.post("/settings/bedrock-models/verify", json={"model_id": model_id})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d == {"ok": True, "id": model_id, "label": agents._bedrock_profile_label(model_id, "")}
    assert captured["max_tokens"] == 1
    assert captured["model"] == model_id


def test_bedrock_verify_probe_failure_returns_detail(monkeypatch):
    """probe が失敗したら detail（キー redact/固定文言流儀に沿った短い理由）を error に載せる。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvfail{sfx}", f"BVFail{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    assert c.put("/settings", json={"bedrock_api_key": f"key-{sfx}"}).status_code == 200
    monkeypatch.setattr(agents.BedrockProvider, "probe",
                        lambda self, timeout=None, max_tokens=16: (False, "403: access denied"))

    r = c.post("/settings/bedrock-models/verify",
               json={"model_id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is False
    assert d["error"] == "403: access denied"


def test_bedrock_verify_redacts_key_leaked_in_probe_detail(monkeypatch):
    """RV HIGH（2026-07-03再検証）: verify エンドポイントも SDK/プロキシの例外メッセージへの
    キー混入を redact する（応答にキー文字列が一切現れない・回帰テスト）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvleak{sfx}", f"BVLeak{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    leaked_key = f"secret-verify-key-{sfx}"
    assert c.put("/settings", json={"bedrock_api_key": leaked_key}).status_code == 200
    monkeypatch.setattr(
        agents.BedrockProvider, "probe",
        lambda self, timeout=None, max_tokens=16: (
            False, f"403 Forbidden: upstream said Authorization: Bearer {leaked_key} rejected (key={leaked_key})"))

    r = c.post("/settings/bedrock-models/verify",
               json={"model_id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0"})
    assert r.status_code == 200, r.text
    assert leaked_key not in r.text, "キーがレスポンス本文のどこかに漏れている"
    d = r.json()
    assert d["ok"] is False
    assert leaked_key not in d["error"]
    assert "Bearer [REDACTED]" in d["error"]


def test_bedrock_verify_rate_limits_rapid_calls_per_user(monkeypatch):
    """連打抑制: 直近 _BEDROCK_VERIFY_MIN_INTERVAL 秒以内の2回目は 429（悪用防止）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvrate{sfx}", f"BVRate{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    api._bedrock_verify_last_call.pop(uid, None)
    monkeypatch.setattr(agents.BedrockProvider, "probe",
                        lambda self, timeout=None, max_tokens=16: (True, ""))

    body = {"model_id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0"}
    r1 = c.post("/settings/bedrock-models/verify", json=body)
    assert r1.status_code == 200, r1.text
    r2 = c.post("/settings/bedrock-models/verify", json=body)
    assert r2.status_code == 429, r2.text


def test_bedrock_verify_rate_limit_also_applies_to_malformed_id_attempts(monkeypatch):
    """RV観点: 形式不正な入力の連打も「試行」としてレート制限の対象にする（形式チェックだけを
    無限に叩けるエンドポイントにしない）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"bvratefmt{sfx}", f"BVRateFmt{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    api._bedrock_verify_last_call.pop(uid, None)

    r1 = c.post("/settings/bedrock-models/verify", json={"model_id": "bad-format"})
    assert r1.status_code == 200 and r1.json()["ok"] is False
    r2 = c.post("/settings/bedrock-models/verify", json={"model_id": "also-bad-format"})
    assert r2.status_code == 429, r2.text


def test_bedrock_verify_requires_login():
    """未ログインは 401（他の /settings 系エンドポイントと同じ）。"""
    if not _try_init():
        pytest.skip("infra down")
    anon = TestClient(app, raise_server_exceptions=False)
    r = anon.post("/settings/bedrock-models/verify",
                  json={"model_id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0"})
    assert r.status_code == 401


def test_bedrock_models_cache_race_stale_write_discarded(monkeypatch):
    """RV MEDIUM 3: GET が旧キーで control-plane 呼び出し中（＝結果をまだ書き込む前）に、
    別リクエストが PUT でキーを変更すると、GET 完了時に旧キーの結果を誤って書き戻し、
    以後 TTL（5分）は新キーに切り替わらない read-modify-write 競合を再現する。key の
    fingerprint 比較により、その古い結果が新キーの取得結果として再利用されないことを確認する。

    RV MED（N1/N2・2026-07-16 Codex RV 3巡目再検証で契約を強化）: 当初（RV MEDIUM 3 時点）は
    「GET 自身の応答はその場では旧キーの結果を返してよい（キャッシュに残らなければ良い）」だったが、
    N1（`add_bedrock_verified_models` の同一トランザクション内 fingerprint 再照合）・N2（不一致時は
    verify と同じ意味論に統一）により、**GET 自身の応答も**旧キーの結果ではなく
    `{"models": [], "error": "設定が変更されました。もう一度お試しください"}` を返すようになった
    （「返す ID は必ず保存できる」という中核契約を、その場のレスポンスにも一貫して適用するため）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"bmodrace{sfx}", f"BModRace{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    old_key, new_key = f"old-key-{sfx}", f"new-key-{sfx}"
    assert c.put("/settings", json={"bedrock_api_key": old_key}).status_code == 200
    with api._BEDROCK_MODELS_CACHE_LOCK:
        api._BEDROCK_MODELS_CACHE.pop(uid, None)

    started, proceed = threading.Event(), threading.Event()
    captured: dict = {}

    def slow_fetch(api_key):
        captured["key"] = api_key
        started.set()
        assert proceed.wait(timeout=5), "PUT 側がタイムアウト（競合を再現できなかった）"
        return ([{"id": "stale-from-old-key", "label": "stale"}], None)

    monkeypatch.setattr(system_router, "list_bedrock_inference_profiles", slow_fetch)
    result: dict = {}

    def do_get():
        result["resp"] = c.get("/settings/bedrock-models").json()

    t = threading.Thread(target=do_get)
    t.start()
    assert started.wait(timeout=5), "fetch が開始しなかった"
    assert captured["key"] == old_key

    c2 = _login(uid, pw)   # 別クライアント（同一ユーザー）で、GET の control-plane 呼び出し中に鍵を変更
    assert c2.put("/settings", json={"bedrock_api_key": new_key}).status_code == 200

    proceed.set()   # 旧キーでの fetch を完了させる
    t.join(timeout=5)
    assert not t.is_alive(), "GET スレッドが終了しなかった"
    # N1/N2: 記録直前の同一トランザクション内 fingerprint 再照合で不一致を検出し、GET 自身の応答も
    # 「設定が変更されました」に統一される（旧キーの fetch 結果をそのまま返さない）。
    assert result["resp"] == {"models": [], "error": "設定が変更されました。もう一度お試しください"}, \
        result["resp"]

    # 本題: 競合で書き込まれかけた旧キーの結果が、新キーでの取得に再利用されていないこと
    # （バグがあれば下の monkeypatch が呼ばれず、直前の stale 結果がそのまま返る）。
    monkeypatch.setattr(system_router, "list_bedrock_inference_profiles",
                        lambda api_key: ([{"id": "fresh-from-new-key", "label": "fresh"}], None))
    r2 = c.get("/settings/bedrock-models").json()
    assert r2["models"] == [{"id": "fresh-from-new-key", "label": "fresh"}], \
        "競合中に書き込まれた旧キーの結果がキャッシュに残り、新キーでの取得に混入している"
