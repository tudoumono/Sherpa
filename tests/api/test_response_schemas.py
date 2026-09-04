"""API 応答スキーマの契約テスト（フェーズ7-1・docs/proposals/2026-07-02-リファクタリング計画.md フェーズ7）。

`sherpa/schemas.py` に定義した応答モデルを、実 `TestClient` 応答（実測）に対して `TypeAdapter` で
検証する。対象は `tests/e2e/mock_api.py` の `MOCKED` レジストリ全ルート（件数は
`test_mocked_registry_route_count_matches_docstring_claim` が機械的に固定する・response_model の
付与有無に関わらず全て検証する＝付与していないルートも含めて実測担保を取る）。

このファイルは挙動を変えない（read のみ・作成した副産物は必ず teardown で消す）。赤が出た場合は
「スキーマが実測とずれている」方を直す（アプリ側 `sherpa/routers/*.py`・`sherpa/schemas.py` は
変えても、応答内容そのものは変えない）。

非対象（62件中5件・JSON でないため TypeAdapter 適用不可）:
  - GET /documents/download（StreamingResponse・fd 配信）・GET /workspace/files/{file_id}/download（FileResponse）
  - GET /admin/audit/export（CSV/JSONL の Response）
  - GET /chat/stream・GET /chat/turns/{turn_id}/stream（StreamingResponse・SSE）
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from _test_users import register_test_uid
from _world_setup import TEST_WORLD_ID, ensure_v1

# tests/e2e/mock_api.py を bare import する（tests/api/test_mock_api_contract.py と同じ流儀・
# tests/conftest.py が ROOT/tests を sys.path に載せるが tests/e2e 自体は載らないため個別追加）。
_E2E_DIR = Path(__file__).resolve().parents[1] / "e2e"
if str(_E2E_DIR) not in sys.path:
    sys.path.insert(0, str(_E2E_DIR))

import mock_api  # noqa: E402

from sherpa import auth, store
from sherpa import schemas as sc
from sherpa.api import app

os.environ.setdefault("SHERPA_STREAM_PACE", "0")

V = TEST_WORLD_ID


@pytest.fixture(scope="module", autouse=True)
def _compat_mode():
    """このファイルはログインせず直接叩く前提（compat モード＝合成 admin。既存 tests/api の流儀）。

    module scope: `registered_world`（module スコープ）が最初のテストより先に setup されるため、
    function スコープの `monkeypatch.setenv` では間に合わない（このファイルは終始 compat モード
    固定でよく、per-test isolation は不要）。
    """
    prev = os.environ.get("SHERPA_AUTH_DISABLED")
    os.environ["SHERPA_AUTH_DISABLED"] = "1"
    yield
    if prev is None:
        os.environ.pop("SHERPA_AUTH_DISABLED", None)
    else:
        os.environ["SHERPA_AUTH_DISABLED"] = prev


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _validate(model, payload) -> None:
    TypeAdapter(model).validate_python(payload)


# ===================================================================================
# 認証
# ===================================================================================

def test_auth_login_me_logout(client):
    uid = f"schema-{_sfx()}"
    pw = "Schema-Contract-Pw!9"
    store.upsert_user(uid, email=f"{uid}@x.local", display_name=uid,
                      password_hash=auth.hash_password(pw), role="user", status="active")
    register_test_uid(uid)

    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    _validate(sc.AuthLoginResponse, r.json())

    # 互換モード（SHERPA_AUTH_DISABLED=1）では /auth/me は cookie を見ず合成 admin を返す
    # （auth_login 自体は互換モードと無関係に実ログインを行うため、ここまでは実ユーザーで検証済み）。
    r = client.get("/auth/me")
    assert r.status_code == 200
    _validate(sc.AuthMeResponse, r.json())

    r = client.post("/auth/logout")
    assert r.status_code == 200
    _validate(sc.OkResponse, r.json())


# ===================================================================================
# システム
# ===================================================================================

def test_health_summary(client):
    r = client.get("/health/summary")
    assert r.status_code == 200
    _validate(sc.HealthSummaryResponse, r.json())


def test_admin_health(client):
    r = client.get("/admin/health")
    assert r.status_code == 200
    _validate(sc.AdminHealthResponse, r.json())


def test_config(client):
    r = client.get("/config")
    assert r.status_code == 200
    _validate(sc.ConfigResponse, r.json())


def test_settings_get_put(client):
    r = client.get("/settings")
    assert r.status_code == 200
    _validate(sc.SettingsResponse, r.json())

    r = client.put("/settings", json={})
    assert r.status_code == 200, r.text
    _validate(sc.SettingsResponse, r.json())


def test_settings_test(client):
    r = client.post("/settings/test", json={"provider": "openai"})
    assert r.status_code == 200, r.text
    _validate(sc.SettingsTestResponse, r.json())


def test_settings_bedrock_models(client):
    r = client.get("/settings/bedrock-models")
    assert r.status_code == 200
    _validate(sc.BedrockModelsResponse, r.json())


def test_settings_bedrock_models_verify(client):
    """形式不正な model_id を送り、ネットワーク I/O 無しで決定的に失敗分岐（BedrockVerifyErr）を踏む。"""
    r = client.post("/settings/bedrock-models/verify", json={"model_id": "not-a-valid-id"})
    assert r.status_code == 200, r.text
    _validate(sc.BedrockVerifyResponse, r.json())


def test_admin_settings_get_put(client):
    r = client.get("/admin/settings")
    assert r.status_code == 200
    _validate(sc.AdminSettingsView, r.json())

    r = client.put("/admin/settings", json={})
    assert r.status_code == 200, r.text
    _validate(sc.AdminSettingsView, r.json())


def test_announcements_list_create_patch(client):
    marker = f"schema-{_sfx()}"
    made: list[int] = []
    try:
        r = client.post("/admin/announcements", json={"title": f"{marker}-A", "body": "本文A"})
        assert r.status_code == 200, r.text
        _validate(sc.AnnouncementMutateResponse, r.json())
        aid = r.json()["announcement"]["id"]
        made.append(aid)

        r = client.patch(f"/admin/announcements/{aid}", json={"pinned": True})
        assert r.status_code == 200, r.text
        _validate(sc.AnnouncementMutateResponse, r.json())

        r = client.get("/announcements", params={"limit": 100})
        assert r.status_code == 200
        _validate(sc.AnnouncementsListResponse, r.json())
    finally:
        for aid in made:
            store.delete_announcement(aid)


# 正規表現: 末尾が "+00:00"（UTC オフセット表記・response_model 非付与時の既定 jsonable_encoder と
# 同じ形）で終わり、pydantic v2 の既定正規化である "Z" サフィックスになっていないことを確認する。
_UTC_OFFSET_SUFFIX = re.compile(r"\+00:00(?:\"|$)")
_Z_SUFFIX = re.compile(r"\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\"")


def _assert_wire_datetime_preserved(raw_text: str, *, field_label: str) -> None:
    """Codex RV HIGH（2026-07-16 再RV）回帰テスト用アサーション: response_model 付与ルートの
    実応答**文字列**（`r.text`）を直接調べ、datetime が `+00:00`（既存の jsonable_encoder と
    同じ表現）のままで、pydantic v2 既定の `Z` サフィックスに変わっていないことを確認する。
    `r.json()` 経由（Python の datetime へパース後）ではこの表現差は消えてしまうため、
    必ず生テキストで確認する。"""
    assert _UTC_OFFSET_SUFFIX.search(raw_text), (
        f"{field_label}: 応答本文に '+00:00' 形式の datetime が見当たらない（表現が変わった疑い）: {raw_text[:500]}"
    )
    z_hit = _Z_SUFFIX.search(raw_text)
    assert not z_hit, (
        f"{field_label}: datetime が 'Z' サフィックスに正規化されている"
        "（response_model 付与で jsonable_encoder 時代の '+00:00' 表現から変わった＝ Codex RV HIGH の回帰）: "
        f"{raw_text[:500]}"
    )


def test_datetime_wire_format_preserved_with_response_model(client):
    """Codex RV HIGH（フェーズ7-1 再RV・2026-07-16）: response_model 付与ルートでも datetime の
    ワイヤー表現（`+00:00`）が response_model 非付与時と変わらないことを、実応答の生テキストで確認する。
    `sherpa.schemas.WireDateTime`（PlainSerializer）がこれを保証する。対象: `AnnouncementOut`
    （created_at/updated_at）・`AuditRow`（created_at）・`UserRow`（last_login_at）。
    """
    # ---- AnnouncementOut.created_at / updated_at（POST /admin/announcements・response_model 付与）----
    marker = f"schema-dt-{_sfx()}"
    r = client.post("/admin/announcements", json={"title": f"{marker}", "body": "本文"})
    assert r.status_code == 200, r.text
    aid = r.json()["announcement"]["id"]
    try:
        _assert_wire_datetime_preserved(r.text, field_label="AnnouncementOut (POST /admin/announcements)")

        # ---- AuditRow.created_at（GET /admin/audit・response_model 付与）----
        # 上の POST で書かれた announcement.created 監査行を対象に絞って実データを確実に得る。
        r = client.get("/admin/audit", params={"action": "announcement.created", "limit": 5})
        assert r.status_code == 200, r.text
        rows = r.json()["rows"]
        assert rows, "announcement.created の監査行が見当たらない（前提が崩れている）"
        _assert_wire_datetime_preserved(r.text, field_label="AuditRow (GET /admin/audit)")
    finally:
        store.delete_announcement(aid)

    # ---- UserRow.last_login_at（GET /admin/users・response_model 付与）----
    uid = f"schema-dt-{_sfx()}"
    pw = "Schema-Contract-Pw!9"
    store.upsert_user(uid, email=f"{uid}@x.local", display_name=uid,
                      password_hash=auth.hash_password(pw), role="user", status="active")
    register_test_uid(uid)
    lr = client.post("/auth/login", json={"username": uid, "password": pw})
    assert lr.status_code == 200, lr.text
    r = client.get("/admin/users")
    assert r.status_code == 200
    assert f'"uid":"{uid}"' in r.text, f"作成した {uid} が /admin/users 応答に見当たらない: {r.text[:300]}"
    _assert_wire_datetime_preserved(r.text, field_label="UserRow.last_login_at (GET /admin/users)")


# ===================================================================================
# 管理者:ユーザー管理
# ===================================================================================

def test_admin_users_list_create_patch(client):
    uid = f"schema-{_sfx()}"
    try:
        r = client.get("/admin/users")
        assert r.status_code == 200
        _validate(sc.AdminUsersListResponse, r.json())

        r = client.post("/admin/users", json={"uid": uid, "password": "Schema-Contract-Pw!9",
                                              "role": "user"})
        assert r.status_code == 200, r.text
        register_test_uid(uid)
        _validate(sc.AdminUserCreateResponse, r.json())

        r = client.patch(f"/admin/users/{uid}", json={"display_name": "テスト太郎"})
        assert r.status_code == 200, r.text
        _validate(sc.AdminUserPatchResponse, r.json())
    finally:
        register_test_uid(uid)


# ===================================================================================
# 管理者:監査ログ・利用統計
# ===================================================================================

def test_admin_audit_list(client):
    r = client.get("/admin/audit", params={"limit": 5})
    assert r.status_code == 200
    _validate(sc.AdminAuditListResponse, r.json())


def test_admin_usage_stats(client):
    r = client.get("/admin/usage/stats")
    assert r.status_code == 200
    _validate(sc.AdminUsageStatsResponse, r.json())


# ===================================================================================
# 個人ワークスペース
# ===================================================================================

def test_workspace_files_lifecycle(client):
    r = client.post("/workspace/files",
                    files={"file": (f"schema-{_sfx()}.txt", io.BytesIO(b"hello world"), "text/plain")})
    assert r.status_code == 200, r.text
    _validate(sc.WorkspaceFileUploadResponse, r.json())
    file_id = r.json()["id"]

    r = client.get("/workspace/files")
    assert r.status_code == 200
    _validate(sc.WorkspaceFilesListResponse, r.json())

    r = client.get("/workspace/search", params={"q": "hello"})
    assert r.status_code == 200
    _validate(sc.WorkspaceSearchResponse, r.json())

    r = client.delete(f"/workspace/files/{file_id}")
    assert r.status_code == 200, r.text
    _validate(sc.WorkspaceFileDeleteResponse, r.json())


# ===================================================================================
# 資料フォルダ(World)管理
# ===================================================================================

def test_worlds_list_options_fs(client):
    r = client.get("/worlds")
    assert r.status_code == 200
    _validate(sc.WorldsListResponse, r.json())

    r = client.get("/world-options")
    assert r.status_code == 200
    _validate(sc.WorldOptionsResponse, r.json())

    r = client.get("/fs/list")
    assert r.status_code == 200
    _validate(sc.FsListResponse, r.json())


def _wait_for_ingest_idle(client, wid, *, timeout=60.0):
    """ING-3: 背景実行の受付後、完了（または失敗）まで status をポーリングする（テスト専用）。

    `running_progress is None` かつ `last_run_status` が確定していれば「今は動いていない」と
    判定する（`_ingest_summary` の契約どおり）。受付（run_id 確保）と実処理
    （`store.upsert_world` 等）が完全に非同期化されたため、登録直後の一瞬は world 行自体が
    まだ存在せず 404 を返しうる——エラーではなく「まだ作成中」としてポーリングを続ける。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        r = client.get(f"/worlds/{wid}/status")
        if r.status_code == 404:
            time.sleep(0.2)
            continue
        assert r.status_code == 200, r.text
        last = r.json()
        if last.get("running_progress") is None and last.get("last_run_status") is not None:
            return last
        time.sleep(0.2)
    raise AssertionError(f"取り込みが {timeout}s 以内に完了しませんでした: {last}")


@pytest.fixture(scope="module")
def registered_world(client):
    """スキーマ検証専用の使い捨て world を実際の POST /worlds で登録する（実測担保のため）。

    ING-3＝登録は即受付・取り込みは背景実行のため、受付応答は `WorldIngestAcceptedResponse` を
    検証するだけに留め、以後の完了待ちは `_wait_for_ingest_idle`（status ポーリング）に委ねる。
    """
    from sherpa import worlds as worlds_mod
    from _world_registry import register_test_world

    root = tempfile.mkdtemp(prefix="sherpa_schema_world_")
    (Path(root) / "note.md").write_text("スキーマ検証用の最小文書。", encoding="utf-8")
    r = client.post("/worlds", json={"path": root})
    assert r.status_code == 202, r.text
    payload = r.json()
    _validate(sc.WorldIngestAcceptedResponse, payload)
    wid = payload["world_id"]
    register_test_world(wid)
    _wait_for_ingest_idle(client, wid)
    yield wid, root
    try:
        worlds_mod.delete(wid)
    except Exception:
        pass
    import shutil as _shutil
    _shutil.rmtree(root, ignore_errors=True)


def test_world_create_existing_branch(client, registered_world):
    """`registered_world` fixture が新規登録・背景実行の完走を検証済み。ここでは同じ root へ
    再度 POST し、既登録分岐（受付応答＝`WorldIngestAcceptedResponse`）を検証する。"""
    wid, root = registered_world
    r = client.post("/worlds", json={"path": root})
    assert r.status_code == 202, r.text
    _validate(sc.WorldIngestAcceptedResponse, r.json())
    assert r.json()["world_id"] == wid
    _wait_for_ingest_idle(client, wid)


def test_world_status(client, registered_world):
    wid, _root = registered_world
    r = client.get(f"/worlds/{wid}/status")
    assert r.status_code == 200, r.text
    _validate(sc.WorldStatusResponse, r.json())


def test_world_diff(client, registered_world):
    wid, root = registered_world
    r = client.post("/worlds/diff", json={"path": root})
    assert r.status_code == 200, r.text
    _validate(sc.WorldDiffResponse, r.json())

    r = client.get(f"/worlds/{wid}/diff")
    assert r.status_code == 200, r.text
    _validate(sc.WorldDiffResponse, r.json())


def test_world_refresh(client, registered_world):
    wid, _root = registered_world
    r = client.post(f"/worlds/{wid}/refresh")
    assert r.status_code == 202, r.text
    _validate(sc.WorldIngestAcceptedResponse, r.json())
    _wait_for_ingest_idle(client, wid)


# ===================================================================================
# ナレッジグラフ・範囲・取込プレビュー
# ===================================================================================

def test_graph_get(client):
    ensure_v1()
    r = client.get("/graph", params={"world": V})
    assert r.status_code == 200
    _validate(sc.GraphResponse, r.json())


def test_graph_facets(client):
    r = client.get("/graph/facets")
    assert r.status_code == 200
    _validate(sc.GraphFacetsResponse, r.json())


def test_graph_search(client):
    ensure_v1()
    r = client.get("/graph/search", params={"world": V, "relationship": ["COPIES"]})
    assert r.status_code == 200, r.text
    _validate(sc.GraphSearchResponse, r.json())


def test_graph_ask(client):
    ensure_v1()
    r = client.post("/graph/ask", json={"question": "消費税率について", "world": V})
    assert r.status_code == 200, r.text
    _validate(sc.GraphAskResponse, r.json())


def test_scopes(client):
    ensure_v1()
    r = client.get("/scopes", params={"world": V})
    assert r.status_code == 200
    _validate(sc.ScopesResponse, r.json())


def test_ingest_preview(client):
    ensure_v1()
    r = client.get("/ingest/preview", params={"world": V})
    assert r.status_code == 200
    _validate(sc.IngestPreviewResponse, r.json())


def test_admin_es_search(client):
    ensure_v1()
    r = client.get("/admin/es/search", params={"world": V, "query": "税"})
    assert r.status_code == 200, r.text
    _validate(sc.EsSearchResponse, r.json())


# ===================================================================================
# 会話管理・会話共有
# ===================================================================================

def test_conversations_list_and_detail(client):
    seeded_cid: int | None = None
    r = client.get("/conversations")
    assert r.status_code == 200
    rows = r.json()
    if not rows:
        conv = store.create_conversation(user_id="admin", world=V, title="スキーマ契約テスト用 seed")
        seeded_cid = conv["id"]
    try:
        if seeded_cid is not None:
            store.add_message(seeded_cid, "user", "スキーマ契約テスト用メッセージ")
        r = client.get("/conversations")
        assert r.status_code == 200
        rows = r.json()
        _validate(list[sc.ConversationSummary], rows)
        assert rows, "/conversations が空（seed 経路の前提が崩れている）"

        cid = rows[0]["id"]
        r = client.get(f"/conversations/{cid}")
        assert r.status_code == 200
        _validate(sc.ConversationDetailResponse, r.json())
    finally:
        if seeded_cid is not None:
            store.delete_conversation(seeded_cid, user_id="admin")


def test_users_suggest(client):
    r = client.get("/users/suggest", params={"q": "adm"})
    assert r.status_code == 200
    _validate(sc.UsersSuggestResponse, r.json())


def test_conversation_share_create(client):
    invitee = f"schema-{_sfx()}"
    store.upsert_user(invitee, email=f"{invitee}@x.local", display_name=invitee,
                      password_hash=auth.hash_password("Schema-Contract-Pw!9"),
                      role="user", status="active")
    register_test_uid(invitee)
    conv = store.create_conversation(user_id="admin", world=V, title="スキーマ共有テスト")
    cid = conv["id"]
    try:
        r = client.post(f"/conversations/{cid}/shares", json={"invitee_user_ids": [invitee]})
        assert r.status_code == 200, r.text
        _validate(sc.ShareCreateResponse, r.json())
    finally:
        store.delete_conversation(cid, user_id="admin")


# ===================================================================================
# チャット
# ===================================================================================

def test_chat_turns_lifecycle(client):
    r = client.post("/chat/turns", json={"message": "こんにちは", "world": V, "knowledge": False})
    assert r.status_code == 200, r.text
    _validate(sc.ChatTurnStartResponse, r.json())
    turn_id = r.json()["turn_id"]

    r = client.get("/chat/turns/running")
    assert r.status_code == 200
    _validate(sc.ChatTurnsRunningResponse, r.json())

    r = client.post(f"/chat/turns/{turn_id}/stop")
    assert r.status_code == 200
    _validate(sc.ChatTurnStopResponse, r.json())

    # RV: background thread が完走してから関数を抜ける（そのまま抜けると背景スレッドが後続テストと
    # 並行して DB へ書き込み続け、他テストの並行操作と競合して deadlock を誘発しうる・
    # tests/api/test_chat_turns.py::_wait_turn_done と同じ流儀＝タイムアウトなら明示的に失敗させる）。
    from sherpa import chat_turns
    deadline = time.time() + 10.0
    while time.time() < deadline:
        rec = chat_turns.get_turn(turn_id)
        if rec is None or rec.buffer.done:
            return
        time.sleep(0.02)
    raise AssertionError(f"turn {turn_id} が時間内に完了しなかった")


def test_chat_turns_running_populated(client):
    """`chat_turns.start_turn` を直接呼び、意図的にブロックさせた実行中ターンを1件作って
    `GET /chat/turns/running` の実応答（要素あり）を検証する（`ChatTurnRunning` 型の実測）。"""
    from sherpa import chat_turns

    conv = store.create_conversation(user_id="admin", world=V, title="スキーマ running テスト")
    cid = conv["id"]
    release = None
    rec = None
    try:
        import threading
        release = threading.Event()

        def run_fn_factory(conversation_id):
            def run(stop_event, emit):
                release.wait(timeout=5.0)
            return run

        rec = chat_turns.start_turn(uid="admin", conversation_factory=lambda: cid,
                                    run_fn_factory=run_fn_factory)
        r = client.get("/chat/turns/running")
        assert r.status_code == 200
        payload = r.json()
        _validate(sc.ChatTurnsRunningResponse, payload)
        assert any(t["turn_id"] == rec.turn_id for t in payload["turns"]), "実行中ターンが見当たらない"
    finally:
        if release is not None:
            release.set()
        if rec is not None:
            deadline = time.time() + 5.0
            while time.time() < deadline:
                cur = chat_turns.get_turn(rec.turn_id)
                if cur is None or cur.buffer.done:
                    break
                time.sleep(0.02)
        store.delete_conversation(cid, user_id="admin")


# ===================================================================================
# 外部連携 API キー（管理者発行・利用者自己発行・8ルート）
# ===================================================================================

def test_ext_keys_admin_and_self_lifecycle(client):
    """外部連携 API キー管理の8ルート（POST/GET/DELETE/POST-recover × 管理者/利用者本人）を
    実測で検証する（ルート増加時の追随漏れをここで検知する）。

    後始末は三重構造:
      - 最内 finally は HTTP 経由のベストエフォート後始末（DELETE・自己発行キーは
        `user_api_keys_allowed` が真であることを要求するため、設定復元より前に試みる）。
      - 中間 try/finally は「絶対に失敗しない」ことを最優先する層——backstop（各 op_id を
        **独立に**試す。片方の `revoke_unconfirmed_key_by_client_op_id` 呼び出しが例外を
        投げても、もう片方の backstop は必ず実行する）と未失効の残存確認は try 側に置き、
        設定復元はその finally 側に置く。backstop 呼び出しや残存確認の assert がここで落ちても、
        設定復元だけは切り離された finally で独立して必ず実行される（try 側の失敗が設定復元を
        道連れにしない）。
    `client_op_id` は各 POST の**前**に確保する——POST 呼び出し自体が例外/タイムアウトで
    応答を失っても、サーバー側では実際に作成されている可能性がある状態を、確保済みの
    `client_op_id` から backstop で後始末できるようにするため（try 内で例外が起きた場合に
    op_id 自体が未定義になる隙を無くす）。
    設定の復元値は `store.get_system_settings()` の生値（`None`＝未設定 を含む）を使う——
    GET `/admin/settings` の応答（`ext_keys.user_api_keys_allowed`）は
    `bool(sysset.get(...) or False)` で常に真偽値へ丸められており、生値が実は「未設定
    （`None`）」だった場合でもそれを拾って「明示的に False」として書き戻してしまう
    （未設定という状態そのものを壊す）。復元後は PUT の応答が 200 であることに加え、
    `store.get_system_settings()` の生値が実際に復元前の値と一致することまで確認する
    （PUT が 200 を返しても、内部の分岐次第で意図と違う値が保存される回帰を見逃さない）。"""
    sfx = _sfx()
    orig_allowed = store.get_system_settings().get("user_api_keys_allowed")
    op_id_admin = str(uuid.uuid4())   # POST 前に確保（曖昧な発行結果の backstop 用）。
    op_id_self = str(uuid.uuid4())
    admin_key_id: int | None = None
    self_key_id: int | None = None
    try:
        try:
            assert client.put("/admin/settings",
                              json={"user_api_keys_allowed": True}).status_code == 200

            r = client.post("/ext/v1/admin/keys",
                            json={"label": f"schema-admin-{sfx}", "client_op_id": op_id_admin})
            assert r.status_code == 200, r.text
            _validate(sc.ExtKeyCreatedResponse, r.json())
            admin_key_id = r.json()["id"]

            r = client.get("/ext/v1/admin/keys")
            assert r.status_code == 200
            _validate(sc.ExtKeyListResponse, r.json())

            r = client.post("/ext/v1/admin/keys/recover", json={"client_op_id": op_id_admin})
            assert r.status_code == 200, r.text
            _validate(sc.ExtKeyRecoverResponse, r.json())
            # 回復エンドポイントは「未確認だったか」を問わず、client_op_id が一致し未失効なら
            # 常に失効させる（曖昧な結果かどうかの判断はクライアント側の責務）。
            assert r.json()["found"] is True
            assert r.json()["id"] == admin_key_id

            r = client.delete(f"/ext/v1/admin/keys/{admin_key_id}")
            assert r.status_code == 200, r.text   # 直前の回復で既に失効済み＝冪等な失効を検証。
            _validate(sc.ExtKeyRevokeResponse, r.json())

            r = client.post("/ext/v1/keys",
                            json={"label": f"schema-self-{sfx}", "client_op_id": op_id_self})
            assert r.status_code == 200, r.text
            _validate(sc.ExtKeyCreatedResponse, r.json())
            self_key_id = r.json()["id"]

            r = client.get("/ext/v1/keys")
            assert r.status_code == 200
            _validate(sc.ExtKeyListResponse, r.json())

            r = client.post("/ext/v1/keys/recover", json={"client_op_id": op_id_self})
            assert r.status_code == 200, r.text
            _validate(sc.ExtKeyRecoverResponse, r.json())
            assert r.json()["found"] is True
            assert r.json()["id"] == self_key_id

            r = client.delete(f"/ext/v1/keys/{self_key_id}")
            assert r.status_code == 200, r.text   # 直前の回復で既に失効済み＝冪等な失効を検証。
            _validate(sc.ExtKeyRevokeResponse, r.json())
        finally:
            if admin_key_id is not None:
                client.delete(f"/ext/v1/admin/keys/{admin_key_id}")
            if self_key_id is not None:
                client.delete(f"/ext/v1/keys/{self_key_id}")
    finally:
        try:
            # store 直呼びの backstop（compat モードの合成 uid は "admin" 固定・管理者発行は
            # owner_uid IS NULL・自己発行は owner_uid=uid という排他条件に対応）。片方が例外を
            # 投げても、もう片方の backstop は独立に試す（収集した例外は下の assert でまとめて
            # 報告する・沈黙させない）。
            backstop_errors: list[Exception] = []
            for op_id, kwargs in ((op_id_admin, {"created_by": "admin"}),
                                  (op_id_self, {"owner_uid": "admin"})):
                try:
                    store.revoke_unconfirmed_key_by_client_op_id(op_id, "cleanup", **kwargs)
                except Exception as e:   # 握り潰さず記録して継続（もう片方の backstop も必ず試す）。
                    backstop_errors.append(e)
            remaining = [k["id"] for k in store.list_api_keys()
                        if k["client_op_id"] in (op_id_admin, op_id_self)
                        and k["revoked_at"] is None]
            assert not remaining and not backstop_errors, (
                f"backstop 後も未失効のキーが残っている: id={remaining}・"
                f"backstop 自体の例外: {backstop_errors}")
        finally:
            # 上のブロック（backstop・残存確認）が何で落ちても、設定復元は必ず実行する。
            r = client.put("/admin/settings", json={"user_api_keys_allowed": orig_allowed})
            assert r.status_code == 200, r.text
            restored = store.get_system_settings().get("user_api_keys_allowed")
            assert restored == orig_allowed, (
                f"設定復元 PUT は 200 だったが DB 生値が復元前の値と一致しない: "
                f"復元前={orig_allowed!r} 復元後={restored!r}")


def test_ext_keys_route_set_matches_mocked_registry():
    """`/ext/v1/*keys*`（管理者/利用者の外部連携 API キー管理）ルート集合を、
    `tests/e2e/mock_api.py` の `MOCKED` レジストリと機械的に一致させる。新規ルートが
    追加/削除されればこのテストが赤くなるため、この docstring・冒頭の「ルート総数」表記が
    実装から追随漏れするのを防ぐ（対象集合一致の機械固定）。"""
    ext_key_routes_in_mocked = {
        (m, p) for (m, p) in mock_api.MOCKED if p.startswith("/ext/v1/") and "keys" in p
    }
    expected = {
        ("POST", "/ext/v1/admin/keys"), ("GET", "/ext/v1/admin/keys"),
        ("DELETE", "/ext/v1/admin/keys/{key_id}"), ("POST", "/ext/v1/admin/keys/recover"),
        ("POST", "/ext/v1/keys"), ("GET", "/ext/v1/keys"),
        ("DELETE", "/ext/v1/keys/{key_id}"), ("POST", "/ext/v1/keys/recover"),
    }
    assert ext_key_routes_in_mocked == expected


def test_mocked_registry_route_count_matches_docstring_claim():
    """このファイル・`sherpa/schemas.py` の docstring が「`MOCKED` の全ルート」と主張している
    実際の件数を機械的に固定する（新規ルート追加時に更新漏れがあれば、ここで検知される）。"""
    assert len(mock_api.MOCKED) == 62
