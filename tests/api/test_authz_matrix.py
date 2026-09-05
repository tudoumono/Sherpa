"""認可マトリクス完全版（役割 × 全ルート・refactoring-plan フェーズ7 S4）。

`tests/api/test_auth_snapshot.py`（未ログイン×全ルートのステータス snapshot）を土台に、
役割（admin / user / 未ログイン / 互換モード）× 全 APIRoute（`_authz_probe._api_route_keys()`）
へ拡張する。プローブ機構（プレースホルダ・最小 body・request ヘルパ）は
`tests/api/_authz_probe.py` を共有する。

POLICY: 各ルートの認可区分を機械的に列挙した表（`(METHOD, PATH) -> 区分`）。
区分は 'admin'（`_require_admin` 必須）／'login'（ログインのみ必須）／
'ext_key'（`X-API-Key` ヘッダ必須・cookie 無関係）／'special:<期待値>'（個別の固定期待値を持つ
公開系・test_auth_snapshot.EXPECTED 側で既に snapshot 済み＝ここでは POLICY の完全性チェック用に
存在を認識するだけで、追加のループ検証はしない）。
`test_policy_covers_all_routes` が「新規ルート追加時に POLICY 更新を強制する」ガード。

区分の初期値は handler ソースの `_require_admin` 呼び出し（38 出現・うち `GET /announcements` は
`include_unpublished=true` の時だけの**条件付き**admin＝別テストで扱い、POLICY 上は 'login' に
分類）・`require_api_key` 呼び出し（capabilities/doc を含め5出現）・
`docs/proposals/2026-07-02-リファクタリング計画.md`
フェーズ7事前分析が特定した公開/special 5 ルートで機械的に埋め、残りを 'login' とした
（実測: admin 37・ext_key 5・special 5・login 39 = 86）。利用者本人による自己発行/一覧/失効の
3ルート（Cookie 認証・admin 不要）を追加し、login は42へ（86+3=89）。曖昧な発行結果の回復専用
エンドポイントを管理者/利用者それぞれに1本ずつ追加し、admin は38・login は43へ（89+2=91）。

**副作用対策（DENY_ONLY）**: 「許可側」probe（ログイン済みセッションで実際に handler の中まで
実行させ、401/403 で無いことを確認する）は、実行しても安全なルートに限る。安全性の判定基準は
「未登録 world / 存在しない ID をプレースホルダに使うことで、実処理に届く前に 404（またはそれに
相当する検証エラー）で止まることが保証されている」こと（`tests/api/_authz_probe.py` の
`_PLACEHOLDERS` 参照）。これを満たさない・または実 TestClient で確認した結果 安全と言えない
ルートは `DENY_ONLY` に列挙し、許可側 probe をスキップする（拒否側＝未ログイン401 の検証は
`_current_user`/`_require_admin`/`require_api_key` が handler の先頭で必ず先に評価されるため、
DENY_ONLY 対象でも安全に実行できる）。DENY_ONLY の内訳（実 TestClient で確認済み・2026-07-14）:
  - LLM/Codex を実起動しうる重い実行系: `/chat`・`/chat/stream`（GET・SSE 生成器を最後まで
    消費すると同じ経路を通る）・`/chat/turns`（バックグラウンドスレッドで実行が続く）・
    `/qa/run`・`/troubleshoot/run`・`/impact/run`・`/graph/ask`・`/admin/usage/chat`。
  - 外部 API へ実接続する系（コスト/レイテンシ・キーが env にあれば実課金systemもあり得る）:
    `/settings/test`・`/settings/bedrock-models/verify`・`GET /settings/bedrock-models`
    （保存済みキー無しでも env フォールバックで control-plane を叩く実装・キャッシュ無しの初回は必ず
    外向き通信が発生する）。
  - 共有 fixtures world を実際に再構築してしまう: `POST /ingest/rerun`（world 未指定時は既定 "v1"＝
    他テストが使う共有 world をフルリビルドする・404 での保護が無い）。
  - プレースホルダで守れない実書き込み: `POST /admin/announcements`（実在チェックが無く必ず行を
    作る）・`POST /ext/v1/admin/keys`（同様に必ずキーを発行する）。
  - 実行しても安全に見えて実は落とし穴があった（実 TestClient で発見・2026-07-14）:
    `POST /conversations/{cid}/shares`・`POST /conversation-shares/{share_id}/revoke`
    （所有権チェックが 403 を返す＝ auth ゲート由来の 403 と区別できない・フレッシュな
    admin セッションでも「その会話/共有を持っていない」ため同じ 403 になる）、
    `POST /auth/change-password`（不一致パスワードは意図的に **401** を返す設計＝未ログイン検知と
    区別できない）。
  他の全 admin/login 系ミューテーション（`POST /admin/users`・`PATCH /admin/users/{uid}`・
  `PUT /admin/settings`・`PATCH`/`DELETE /admin/announcements/{id}`・
  `DELETE /ext/v1/admin/keys/{key_id}`・`DELETE /worlds/{wid}`・`POST /worlds`・`POST /worlds/diff`・
  `POST /worlds/{wid}/*`・`DELETE`/`PATCH`/`POST /conversations/{cid}(/pin)`・
  `DELETE /workspace/files/{file_id}`・`POST /workspace/files`・`POST /chat/stream/stop`・
  `POST /chat/turns/{turn_id}/stop`・`PUT /settings`）は実 TestClient で「許可側 probe をしても
  実データを作らず/壊さず、401/403 も返さない」ことを確認済み（2026-07-14・本ファイル作成時）。

**IDOR / 所有権系は対象外**（既存の個別テスト tests/api/test_impact_idor.py 等へ委譲）。
`POST /conversations/{cid}/shares` 等の所有権 403 も同じ理由で DENY_ONLY（このマトリクスは
「認証ゲートの有無」を見るものであり、ビジネスロジック上の所有権判定は別の関心事）。

**条件付き認可**: `GET /announcements?include_unpublished=true` は admin のみ（`test_..._conditional`
で個別検証）。POLICY 上は 'login'（`include_unpublished` 省略時は誰でも見られるため）。

**互換モード**（`SHERPA_AUTH_DISABLED=1`）: cookie 無しでも合成 admin として全ゲートを通過する
（`ext_key` は cookie と無関係な別ゲートのため互換モードでも 401 のまま）。
"""
from __future__ import annotations

import time

import pytest

from _authz_probe import _api_route_keys, _request
from _test_users import register_test_uid


@pytest.fixture(autouse=True)
def _isolate_process_env():
    """RV 派生修正（2026-07-14 フェーズ7）: 許可側 probe は任意の handler を実際に実行するため、
    **隠れた import 副作用によるプロセス env 汚染**を各テスト後に巻き戻す。実測した実害:
    GET /admin/settings → markitdown_ocr_arm の遅延 import → 依存 magika が import 時に
    `load_dotenv(find_dotenv())` を実行し、リポジトリ `.env` の OPENAI_API_KEY=sk-REPLACE_ME 等を
    プロセス env へ注入（親ディレクトリ探索のため worktree でも発生）→ 以後の別テスト
    （test_chat_m8::test_provider_seam_uniform 等）の「env キー無し」前提が順序依存で崩れる。
    ここで env を完全スナップショット/復元し、本ファイルの probe が他テストへ状態を残さないことを
    構造的に保証する（monkeypatch と違い handler 内部の os.environ 変異も巻き戻せる）。"""
    import os

    snapshot = dict(os.environ)
    yield
    for k in set(os.environ) - set(snapshot):
        del os.environ[k]
    for k, v in snapshot.items():
        if os.environ.get(k) != v:
            os.environ[k] = v


IMPORT_ERROR: Exception | None = None
try:
    from fastapi.testclient import TestClient

    from sherpa import agentic_search, auth, store
    from sherpa.api import app
except Exception as e:  # pragma: no cover
    IMPORT_ERROR = e
    TestClient = None  # type: ignore[assignment]
    auth = None  # type: ignore[assignment]
    store = None  # type: ignore[assignment]
    app = None  # type: ignore[assignment]
    agentic_search = None  # type: ignore[assignment]


# ===== POLICY 表（85 ルート・機械的初期値＋条件付き admin は 'login' へ寄せた手動補正）=====
POLICY: dict[tuple[str, str], str] = {
    # ---- admin（_require_admin 必須・38）----
    ("GET", "/admin/audit"): "admin",
    ("GET", "/admin/audit/verify"): "admin",
    ("GET", "/admin/audit/export"): "admin",
    ("GET", "/admin/usage/stats"): "admin",
    ("POST", "/admin/usage/chat"): "admin",
    ("GET", "/admin/improvement-log/export"): "admin",
    ("GET", "/admin/settings"): "admin",
    ("PUT", "/admin/settings"): "admin",
    ("POST", "/admin/settings/openai-endpoint-test"): "admin",
    ("GET", "/admin/users"): "admin",
    ("POST", "/admin/users"): "admin",
    ("PATCH", "/admin/users/{uid}"): "admin",
    ("POST", "/admin/announcements"): "admin",
    ("PATCH", "/admin/announcements/{id}"): "admin",
    ("DELETE", "/admin/announcements/{id}"): "admin",
    ("GET", "/admin/es/search"): "admin",
    ("GET", "/admin/health"): "admin",
    ("GET", "/fs/list"): "admin",
    ("GET", "/graph"): "admin",
    ("GET", "/graph/facets"): "admin",
    ("GET", "/graph/search"): "admin",
    ("POST", "/graph/ask"): "admin",
    ("GET", "/ingest/preview"): "admin",
    ("GET", "/ingest/runs"): "admin",
    ("POST", "/ingest/rerun"): "admin",
    ("GET", "/worlds"): "admin",
    ("POST", "/worlds"): "admin",
    ("POST", "/worlds/diff"): "admin",
    ("GET", "/worlds/{wid}/status"): "admin",
    ("POST", "/worlds/{wid}/recount"): "admin",
    ("GET", "/worlds/{wid}/diff"): "admin",
    ("POST", "/worlds/{wid}/rebind"): "admin",
    ("POST", "/worlds/{wid}/refresh"): "admin",
    ("POST", "/worlds/{wid}/reconvert"): "admin",
    ("POST", "/worlds/{wid}/rag_regenerate_rules"): "admin",
    ("DELETE", "/worlds/{wid}"): "admin",
    ("POST", "/ext/v1/admin/keys"): "admin",
    ("GET", "/ext/v1/admin/keys"): "admin",
    ("DELETE", "/ext/v1/admin/keys/{key_id}"): "admin",
    ("POST", "/ext/v1/admin/keys/recover"): "admin",
    # ---- ext_key（X-API-Key ヘッダ必須・cookie は無関係・6）----
    ("POST", "/ext/v1/convert"): "ext_key",
    ("POST", "/ext/v1/search"): "ext_key",
    ("GET", "/ext/v1/capabilities"): "ext_key",
    ("GET", "/ext/v1/doc"): "ext_key",
    ("POST", "/ext/v1/research"): "ext_key",
    ("GET", "/ext/v1/openapi.json"): "ext_key",
    # ---- special（個別固定期待値・test_auth_snapshot.EXPECTED で snapshot 済み・7）----
    ("POST", "/auth/login"): "special:422",
    ("POST", "/auth/logout"): "special:200",
    ("GET", "/healthz"): "special:200",
    ("GET", "/"): "special:307",
    ("GET", "/share/conversations/{token}"): "special:302",
    # Swagger UI（自前ルート化・認証不要の公開系ドキュメント面）。ReDoc（/redoc）は提供しない＝
    # ルートが存在しないため POLICY にも載せない。
    ("GET", "/docs"): "special:200",
    ("GET", "/docs/oauth2-redirect"): "special:200",
    # ---- login（ログインのみ必須・残り・39）----
    ("GET", "/auth/me"): "login",
    ("POST", "/auth/change-password"): "login",
    ("GET", "/announcements"): "login",   # 条件付き admin（include_unpublished=true）は別テストで検証
    ("GET", "/notifications"): "login",   # NOTIFY-1: admin 限定イベントは role で内容を絞る（別テストで検証）
    ("GET", "/users/suggest"): "login",
    ("POST", "/conversations/{cid}/shares"): "login",
    ("POST", "/conversation-shares/{share_id}/revoke"): "login",
    # SH-1/SH-2（2026-08-23-共有フォーク.md・2026-09-05実装）。
    ("POST", "/conversations/{wid}/fork"): "login",
    ("POST", "/conversation-shares/{share_id}/refresh"): "login",
    ("GET", "/conversations/{cid}/shares"): "login",
    ("POST", "/workspace/files"): "login",
    ("GET", "/workspace/files"): "login",
    ("DELETE", "/workspace/files/{file_id}"): "login",
    ("GET", "/workspace/files/{file_id}/download"): "login",
    ("GET", "/workspace/search"): "login",
    ("POST", "/impact/run"): "login",
    ("GET", "/impact/{aid}"): "login",
    ("GET", "/impact/{aid}/export.xlsx"): "login",
    ("GET", "/scopes"): "login",
    ("GET", "/chat/tools-availability"): "login",   # SC-6e: 検索経路の実接続可用性
    ("POST", "/chat"): "login",
    ("GET", "/chat/stream"): "login",
    ("POST", "/chat/stream/stop"): "login",
    ("POST", "/chat/turns"): "login",
    ("GET", "/chat/turns/{turn_id}/stream"): "login",
    ("GET", "/chat/turns/running"): "login",
    ("POST", "/chat/turns/{turn_id}/stop"): "login",
    ("POST", "/chat/{conversation_id}/messages/{message_id}/feedback"): "login",
    ("GET", "/config"): "login",
    ("GET", "/settings"): "login",
    ("GET", "/settings/bedrock-models"): "login",
    ("POST", "/settings/bedrock-models/verify"): "login",
    ("PUT", "/settings"): "login",
    ("POST", "/settings/test"): "login",
    ("GET", "/conversations"): "login",
    ("GET", "/conversations/{cid}"): "login",
    ("DELETE", "/conversations/{cid}"): "login",
    ("POST", "/conversations/{cid}/pin"): "login",
    ("PATCH", "/conversations/{cid}"): "login",
    ("POST", "/troubleshoot/run"): "login",
    ("POST", "/qa/run"): "login",
    ("GET", "/documents/download"): "login",
    ("GET", "/documents"): "login",
    ("GET", "/world-options"): "login",
    ("GET", "/health/summary"): "login",
    # 利用者本人による自己発行/一覧/失効/回復。Cookie セッション認証（`_current_user`）・admin 不要。
    ("POST", "/ext/v1/keys"): "login",
    ("GET", "/ext/v1/keys"): "login",
    ("DELETE", "/ext/v1/keys/{key_id}"): "login",
    ("POST", "/ext/v1/keys/recover"): "login",
}

# 「許可側」probe をスキップするルート（理由はモジュール docstring 参照）。
DENY_ONLY: set[tuple[str, str]] = {
    ("GET", "/chat/stream"),
    # SC-6e: ES/Neo4j への実接続を試みる（`agentic_search.tool_availability()`）ため、
    # 許可側 probe の所要時間・結果がテスト環境の外部到達性に左右される（本テストは認可判定の
    # 固定が目的で、可用性判定の実測は tests/api/test_scope_mc.py 側が担う）。
    ("GET", "/chat/tools-availability"),
    ("GET", "/settings/bedrock-models"),
    ("POST", "/auth/change-password"),
    ("POST", "/qa/run"),
    ("POST", "/troubleshoot/run"),
    ("POST", "/impact/run"),
    ("POST", "/graph/ask"),
    ("POST", "/admin/usage/chat"),
    ("POST", "/ingest/rerun"),
    ("POST", "/admin/announcements"),
    ("POST", "/ext/v1/admin/keys"),
    ("POST", "/conversations/{cid}/shares"),
    ("POST", "/conversation-shares/{share_id}/revoke"),
    # SH-2（refresh・一覧）も同じ所有権 403 パターン（フレッシュな probe セッションは対象の
    # 会話/共有を持たない＝auth ゲート由来の403と区別できない）。SH-1（fork）は対象なし
    # （wid=999999999 は必ず 404 の LookupError になり所有権チェックの 403 分岐に届かないため
    # 安全に許可側 probe できる＝DENY_ONLY に入れない）。
    ("POST", "/conversation-shares/{share_id}/refresh"),
    ("GET", "/conversations/{cid}/shares"),
    # system_settings.user_api_keys_allowed は既定 false（テスト環境でも未設定）のため、
    # 許可側 probe（一意の新規ログインユーザー）は4ルートとも毎回403（機能無効）を返す——
    # これは認可ゲート由来の403と区別できない（`ext_key_admin_create` と同様のパターン）。
    ("POST", "/ext/v1/keys"),
    ("GET", "/ext/v1/keys"),
    ("DELETE", "/ext/v1/keys/{key_id}"),
    ("POST", "/ext/v1/keys/recover"),
}

# RV MED（2026-07-14 フェーズ7 1巡目）: DENY_ONLY を4本減らし、専用 body で「外部接続・実行・
# DB 変異の**前に**決定的に返る分岐」を突いて許可側 probe を成立させる（実装読解＋実 TestClient で
# 確認済み・2026-07-14）:
#   - verify: model_id 形式不正 → BEDROCK_MODEL_ID_RE 不一致で 200 {"ok": False}（外部呼び出し前。
#     レート制限は uid 単位＝probe の uid は毎回一意なので非干渉・仮に 429 でも not in(401,403)）
#   - settings/test: 未知 provider → graph_extract._probe 到達前に 422
#   - chat / chat/turns: 存在しない conversation_id → handler 先頭の _check_chat_write が 404
#     （provider 実行・会話作成の前）
ALLOW_PROBE_BODY: dict[tuple[str, str], dict] = {
    ("POST", "/settings/bedrock-models/verify"): {"model_id": "x"},
    ("POST", "/settings/test"): {"provider": "__authz_probe__"},
    # 未指定なら OpenaiEndpointTestReq.provider の既定値 "openai" になり、
    # DB に中央 OpenAI キーが残っていれば（他テストの副作用等）実 API へ到達しうる（このルートは
    # 422 検証を通過すると `graph_extract._probe` を呼ぶ）。/settings/test と同じ「未知 provider→
    # 通信前 422」の安全な body を明示する（admin 認可の ALLOW 確認自体は 200/404/422 いずれでも
    # 401/403 でなければ成立するため、実通信を避けつつ認可判定だけを固定できる）。
    ("POST", "/admin/settings/openai-endpoint-test"): {"provider": "__authz_probe__"},
    ("POST", "/chat"): {"message": "authz-probe", "conversation_id": 999999999},
    ("POST", "/chat/turns"): {"message": "authz-probe", "conversation_id": 999999999},
}

ADMIN_ROUTES = frozenset(k for k, v in POLICY.items() if v == "admin")
LOGIN_ROUTES = frozenset(k for k, v in POLICY.items() if v == "login")
EXT_KEY_ROUTES = frozenset(k for k, v in POLICY.items() if v == "ext_key")
SPECIAL_ROUTES = frozenset(k for k, v in POLICY.items() if v.startswith("special:"))

assert DENY_ONLY <= (ADMIN_ROUTES | LOGIN_ROUTES), "DENY_ONLY は admin/login ルートのみ対象"
assert set(ALLOW_PROBE_BODY).isdisjoint(DENY_ONLY), "ALLOW_PROBE_BODY と DENY_ONLY は排他"
assert set(ALLOW_PROBE_BODY) <= (ADMIN_ROUTES | LOGIN_ROUTES), "ALLOW_PROBE_BODY は認可対象ルートのみ"


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    if IMPORT_ERROR is not None:
        pytest.skip(f"infra down: {IMPORT_ERROR}")
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"infra down: {e}")   # 不可なら可視の skip（silent-green 根絶）


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _mk_user(uid: str, password: str, *, role: str = "user") -> None:
    store.upsert_user(
        uid,
        email=f"{uid}@authz-matrix.local",
        display_name=uid.upper(),
        password_hash=auth.hash_password(password),
        role=role,
        status="active",
    )
    register_test_uid(uid)   # teardown で自動削除（tests/_test_users.py）


def _login(uid: str, password: str) -> TestClient:
    c = _client()
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, f"login failed for {uid}: {r.status_code} {r.text}"
    return c


def _fresh_user(role: str) -> TestClient:
    sfx = _sfx()
    uid, pw = f"authzmx{role[0]}{sfx}", f"authzmx-{role}-pw-{sfx}"
    _mk_user(uid, pw, role=role)
    return _login(uid, pw)


def test_policy_covers_all_routes():
    """POLICY のキー集合が全 APIRoute と一致する（新規ルート追加時に表更新を強制＝認可漏れの構造的防止）。"""
    actual = _api_route_keys()
    expected = set(POLICY)
    assert actual == expected, (
        "ルート増減あり: POLICY 表を更新すること。\n"
        f"表に無い新規ルート: {sorted(actual - expected)}\n"
        f"表にあるが消えたルート: {sorted(expected - actual)}"
    )
    # 区分の内訳が事前分析の実測（admin=旧38＋SET-2cの接続テスト1＋STAT-1の利用統計チャット1・
    # ext_key=旧5＋PART-4の POST /ext/v1/research・special 7（旧5＋Swagger UI 自前ルート化分の
    # 2件・ReDoc は提供しないため対象外）・login=旧39＋PART-5の利用者自己発行3ルート＋回復1ルート）
    # と一致することも併せて固定する（区分の取り違えに気づけるように）。
    # admin=改善ログエクスポート1件追加（41）・login=回答フィードバック投稿1件追加（44）。
    # admin=取り込み可視化パックの再集計/再変換2ルート追加で43。
    # login=検索経路の実接続可用性（GET /chat/tools-availability・SC-6e）1件追加で45。
    # admin=L5「規則版で再生成」（POST /worlds/{wid}/rag_regenerate_rules）1件追加で44。
    # login=NOTIFY-1（GET /notifications）1件追加で46。
    # admin=GRAPH-SRC（2026-09-04・K9-K11）で旧・意味層フル抽出/対応橋の4ルート（extract・
    # concepts/propose・confirm・disable）を撤去し40。
    # login=SH-1/SH-2（2026-09-05・共有フォーク＋再共有）で3ルート追加（fork・refresh・共有一覧）し49。
    assert len(ADMIN_ROUTES) == 40
    assert len(EXT_KEY_ROUTES) == 6
    assert len(SPECIAL_ROUTES) == 7
    assert len(LOGIN_ROUTES) == 49


def test_admin_routes_deny_for_anon_and_nonadmin_user():
    """admin 区分: 未ログインは401・一般ユーザーは403（全38ルート・拒否側は常に安全に実 probe できる）。"""
    if not _try_init():
        pytest.skip("infra down")
    anon = _client()
    user = _fresh_user("user")
    mismatches = []
    for method, path in sorted(ADMIN_ROUTES):
        got = _request(anon, method, path).status_code
        if got != 401:
            mismatches.append(f"anon {method} {path}: 期待 401 実測 {got}")
        got = _request(user, method, path).status_code
        if got != 403:
            mismatches.append(f"user {method} {path}: 期待 403 実測 {got}")
    assert not mismatches, "admin ゲートの拒否側が変化:\n" + "\n".join(mismatches)


def test_admin_routes_allow_for_admin():
    """admin 区分: admin セッションは401/403以外（DENY_ONLY を除く・許可側 probe）。"""
    if not _try_init():
        pytest.skip("infra down")
    admin = _fresh_user("admin")
    mismatches = []
    for method, path in sorted(ADMIN_ROUTES - DENY_ONLY):
        got = _request(admin, method, path,
                       json_override=ALLOW_PROBE_BODY.get((method, path))).status_code
        if got in (401, 403):
            mismatches.append(f"admin {method} {path}: 401/403 のまま（意図せずゲートされている） got {got}")
    assert not mismatches, "admin セッションがゲートで弾かれている:\n" + "\n".join(mismatches)


def test_login_routes_deny_for_anon():
    """login 区分: 未ログインは401（全39ルート・拒否側は常に安全に実 probe できる）。"""
    if not _try_init():
        pytest.skip("infra down")
    anon = _client()
    mismatches = []
    for method, path in sorted(LOGIN_ROUTES):
        got = _request(anon, method, path).status_code
        if got != 401:
            mismatches.append(f"anon {method} {path}: 期待 401 実測 {got}")
    assert not mismatches, "login ゲートの拒否側が変化:\n" + "\n".join(mismatches)


def test_login_routes_allow_for_user_and_admin():
    """login 区分: user・admin いずれのセッションも401/403以外（DENY_ONLY を除く・許可側 probe）。"""
    if not _try_init():
        pytest.skip("infra down")
    user = _fresh_user("user")
    admin = _fresh_user("admin")
    mismatches = []
    for label, client in (("user", user), ("admin", admin)):
        for method, path in sorted(LOGIN_ROUTES - DENY_ONLY):
            got = _request(client, method, path,
                           json_override=ALLOW_PROBE_BODY.get((method, path))).status_code
            if got in (401, 403):
                mismatches.append(f"{label} {method} {path}: 401/403 のまま（意図せずゲートされている） got {got}")
    assert not mismatches, "ログイン済みセッションがゲートで弾かれている:\n" + "\n".join(mismatches)


def test_chat_tools_availability_authz(monkeypatch):
    """SC-6e: `GET /chat/tools-availability` は `DENY_ONLY`（ES/Neo4j への実接続で許可側 probe
    の所要時間・結果が外部到達性に左右されるため）に載っており、上の
    `test_login_routes_allow_for_user_and_admin` の一括 sweep からは除外されている。ここでは
    `agentic_search.tool_availability()` を stub して実接続を避けつつ、login ゲートそのもの
    （一般ユーザー200・匿名401）だけを個別に固定する。"""
    if not _try_init():
        pytest.skip("infra down")
    monkeypatch.setattr(agentic_search, "tool_availability",
                        lambda: {"grep": True, "fulltext": True, "graph": True})
    anon = _client()
    r_anon = _request(anon, "GET", "/chat/tools-availability")
    assert r_anon.status_code == 401

    user = _fresh_user("user")
    r_user = _request(user, "GET", "/chat/tools-availability")
    assert r_user.status_code == 200
    assert r_user.json() == {"grep": True, "fulltext": True, "graph": True}


def test_ext_key_routes_reject_all_cookie_sessions():
    """ext_key 区分: X-API-Key 無しは cookie セッションの役割に関わらず全て401（互換モードでも同様・別テスト）。"""
    if not _try_init():
        pytest.skip("infra down")
    anon = _client()
    user = _fresh_user("user")
    admin = _fresh_user("admin")
    mismatches = []
    for label, client in (("anon", anon), ("user", user), ("admin", admin)):
        for method, path in sorted(EXT_KEY_ROUTES):
            got = _request(client, method, path).status_code
            if got != 401:
                mismatches.append(f"{label} {method} {path}: 期待 401 実測 {got}")
    assert not mismatches, "ext_key ゲート（X-API-Key必須）が cookie で迂回できている:\n" + "\n".join(mismatches)


def test_announcements_include_unpublished_requires_admin():
    """条件付き認可: `GET /announcements?include_unpublished=true` は admin のみ（user は403・admin は非401/403）。"""
    if not _try_init():
        pytest.skip("infra down")
    anon = _client()
    user = _fresh_user("user")
    admin = _fresh_user("admin")
    r = anon.get("/announcements", params={"include_unpublished": "true"})
    assert r.status_code == 401, f"anon: 期待401 実測{r.status_code}"
    r = user.get("/announcements", params={"include_unpublished": "true"})
    assert r.status_code == 403, f"user: 期待403 実測{r.status_code}（include_unpublished は admin 限定のはず）"
    r = admin.get("/announcements", params={"include_unpublished": "true"})
    assert r.status_code not in (401, 403), f"admin: 401/403 のまま got {r.status_code}"


def test_compat_mode_bypasses_gates_but_not_ext_key(auth_disabled, monkeypatch):
    """互換モード（SHERPA_AUTH_DISABLED=1）: cookie 無しで admin/login は通過・ext_key は401のまま。

    RV MED（2巡目）: 互換モードの実行主体は**合成 admin（uid='admin'・共有 dev データの実所有者）**の
    ため、変異系（POST/PUT/PATCH/DELETE）の許可側 probe を回すと placeholder ID（cid=1・file_id=1・
    wid=w1 等）に実在行があった場合に**実データを変更/削除しうる**（multipart upload も admin 所有の
    行を作る・admin はテストユーザー cleanup の対象外）。互換モードで pin すべき性質は
    「認証ゲートが素通しになる」ことだけで、それは **GET（read-only）だけで全ゲート実装
    （_current_user/_require_admin）を通過検証できる**（変異系も同じゲート関数を handler 先頭で呼ぶ＝
    メソッドによる分岐は無い）。fresh ユーザーでの変異系許可側 probe は上の admin/login テストが
    実施済みのため、ここは GET に限定する。

    RV LOW（3巡目）: GET でも /admin/audit・/admin/audit/verify・/admin/audit/export・
    /admin/usage/stats は監査行を書き、ext_key 拒否ループの POST 2本も認証失敗の append-only
    監査行を作る（業務データ破壊は無いが、合成 admin 名義の監査集計に順序依存を残す）。
    本テストの関心は「ゲートの素通し」であって監査記録ではないため、`store.audit` を
    facade monkeypatch で no-op 化して隔離する（fail-closed 系は `_audit_insert` 直呼びで
    別経路＝ここでは影響しない・パッチは monkeypatch により本テスト内で自動復元）。"""
    if not _try_init():
        pytest.skip("infra down")
    monkeypatch.setattr(store, "audit", lambda *a, **k: None)
    anon = _client()
    mismatches = []
    for method, path in sorted((ADMIN_ROUTES | LOGIN_ROUTES) - DENY_ONLY):
        if method != "GET":
            continue   # 変異系は合成 admin＝実データ所有者のため probe しない（docstring 参照）
        got = _request(anon, method, path,
                       json_override=ALLOW_PROBE_BODY.get((method, path))).status_code
        if got in (401, 403):
            mismatches.append(f"compat {method} {path}: 401/403 のまま got {got}")
    for method, path in sorted(EXT_KEY_ROUTES):
        got = _request(anon, method, path).status_code
        if got != 401:
            mismatches.append(f"compat ext_key {method} {path}: 期待401 実測{got}")
    assert not mismatches, "互換モードのゲート挙動が変化:\n" + "\n".join(mismatches)
