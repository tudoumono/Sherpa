"""認証スナップショット（未ログイン×全ルートの期待ステータス表・refactoring-plan フェーズ0）。

認可は route metadata でなく handler 内の `_current_user` / `_require_admin` の**手呼び**で
実装されているため、ルート表（test_route_snapshot）だけでは認可退行を検知できない。
そこで「認証有効モード（既定・ログイン必須）×未ログイン」で全 APIRoute を叩き、実測ステータスを
表駆動で固定する。**現状の実挙動をそのまま golden 化するもので、挙動は変更しない。**

測り方（決定的にするための約束）:
  - GET は共通のダミー query（`query`/`q`/`rel`）を常に付す。これらを必須にしている
    エンドポイント（/admin/es/search・/workspace/search・/documents/download）でも
    422（query 検証）で止まらず、認証チェックまで到達する。`/chat/stream` のように
    追加の必須 query（`message`）を持つルートは `_EXTRA_QUERY` で個別に補う。
  - POST/PUT/PATCH は既定で空 body（`{}`）で叩く。**Pydantic の必須フィールドを持つ body は
    空 `{}` だと FastAPI の body validation が handler 内の認証チェックより先に走り、
    401 でなく 422 で止まってしまう**＝認証が壊れて誰でも叩けるようになっても 422 のまま
    残ってしまい認可退行を検出できない（Codex RV 指摘）。そこで対象ルートには `_JSON_BODY` で
    **各 Pydantic モデルの必須フィールドだけを満たす最小 payload**（`sherpa/api.py` の
    `...Req(BaseModel)` 定義から確認）を用意し、body validation を通過させて認証チェックまで
    到達させる。値そのものは認証チェックの前に評価されない（`_current_user` が handler の
    先頭で呼ばれるため）ので、型さえ満たせばよい。
  - multipart（`/workspace/files`）は `files=` で最小ファイルを添付し、`UploadFile = File(...)`
    の必須検証を通過させる。
  - path パラメータはダミー値で埋める。redirect は追わない（公開系の 302/307 を実測で固定）。

公開系（`/healthz`=200・`/`=307 redirect・`/share/*`=302 redirect・`/auth/logout`=200）は
現状どおりの非 401 を記録する。`/auth/login` はログイン自体のエンドポイント（保護ルートではない）
のため、空 body のまま 422（LoginReq の必須フィールド未充足）を現状どおり記録する
＝**保護ルートで 422 が残っているのはこの1件のみ**（他は全て本ファイルの修正で 401 に揃えた）。
`/ui` mount（StaticFiles）は APIRoute でないため対象外。`/docs`・`/docs/oauth2-redirect` は
自前ルート化されており APIRoute として対象に含まれる（いずれも認証不要の公開系＝200）。ReDoc
（`/redoc`）は提供しない＝ルート自体が存在しない（404）。

新しいエンドポイントを足す/消すと EXPECTED の更新を強制される（ルート集合の一致も assert する）
＝認可漏れの構造的な気づきになる。新しい POST/PUT/PATCH に必須フィールドを持つ body がある場合は
`_JSON_BODY` に最小 payload を追加すること（さもないと 422 のまま固定され、認可退行を検出できない
表になってしまう）。

プローブ基盤（`_PLACEHOLDERS`/`_JSON_BODY`/`_EXTRA_QUERY`/`_MULTIPART_ROUTES`/`_fill`/
`_api_route_keys`/`_request`）は `tests/api/_authz_probe.py` へ抽出済み（フェーズ7 S4・
`tests/api/test_authz_matrix.py` と共有）。ロジックは変更していない（挙動不変のまま緑維持）。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from sherpa.api import app
from _authz_probe import _api_route_keys, _request

# 未ログインで叩いたときの実測ステータス（(METHOD, PATH) → status）。
# 生成元は下記の実挙動（refactoring-plan フェーズ1 スナップショット・Codex RV 指摘を反映）。
# 挙動変更時のみ更新する。
EXPECTED: dict[tuple[str, str], int] = {
    ("POST", "/auth/login"): 422,       # ログイン自体のエンドポイント（保護ルートではない・対象外）
    ("GET", "/auth/me"): 401,
    ("POST", "/auth/logout"): 200,
    ("POST", "/auth/change-password"): 401,
    ("GET", "/admin/users"): 401,
    ("POST", "/admin/users"): 401,
    ("PATCH", "/admin/users/{uid}"): 401,
    ("GET", "/admin/audit"): 401,
    ("GET", "/admin/audit/verify"): 401,
    ("GET", "/admin/audit/export"): 401,
    ("GET", "/admin/usage/stats"): 401,
    ("POST", "/admin/usage/chat"): 401,
    ("GET", "/admin/improvement-log/export"): 401,
    ("GET", "/admin/settings"): 401,
    ("PUT", "/admin/settings"): 401,   # 全体設定（S1）・全フィールド optional＝空 body でも認証チェックまで到達
    # SET-2c: 接続先の接続テスト（admin 専用）。全フィールド optional（provider は既定 "openai"）＝
    # 空 body でも認証チェックまで到達する。
    ("POST", "/admin/settings/openai-endpoint-test"): 401,
    ("GET", "/announcements"): 401,
    ("GET", "/notifications"): 401,
    ("POST", "/admin/announcements"): 401,
    ("PATCH", "/admin/announcements/{id}"): 401,
    ("DELETE", "/admin/announcements/{id}"): 401,
    ("GET", "/users/suggest"): 401,
    ("POST", "/conversations/{cid}/shares"): 401,
    ("GET", "/share/conversations/{token}"): 302,
    ("POST", "/conversation-shares/{share_id}/revoke"): 401,
    # SH-1/SH-2（2026-08-23-共有フォーク.md・2026-09-05実装）。
    ("POST", "/conversations/{wid}/fork"): 401,
    ("POST", "/conversation-shares/{share_id}/refresh"): 401,
    ("GET", "/conversations/{cid}/shares"): 401,
    ("POST", "/workspace/files"): 401,
    ("GET", "/workspace/files"): 401,
    ("DELETE", "/workspace/files/{file_id}"): 401,
    ("GET", "/workspace/files/{file_id}/download"): 401,   # P1-c（Codex 強化計画 Phase1・作成ファイル DL）
    ("GET", "/workspace/search"): 401,
    ("POST", "/impact/run"): 401,
    ("GET", "/impact/{aid}"): 401,
    ("GET", "/impact/{aid}/export.xlsx"): 401,
    ("GET", "/scopes"): 401,
    ("GET", "/chat/tools-availability"): 401,   # SC-6e: 検索経路の実接続可用性（ログイン必須）
    ("POST", "/chat"): 401,
    ("GET", "/chat/stream"): 401,
    ("POST", "/chat/stream/stop"): 401,
    ("POST", "/chat/turns"): 401,
    ("GET", "/chat/turns/{turn_id}/stream"): 401,
    ("GET", "/chat/turns/running"): 401,
    ("POST", "/chat/turns/{turn_id}/stop"): 401,
    ("POST", "/chat/{conversation_id}/messages/{message_id}/feedback"): 401,
    ("GET", "/config"): 401,
    ("GET", "/settings"): 401,
    ("GET", "/settings/bedrock-models"): 401,
    ("POST", "/settings/bedrock-models/verify"): 401,
    ("PUT", "/settings"): 401,
    ("POST", "/settings/test"): 401,
    ("GET", "/conversations"): 401,
    ("GET", "/conversations/{cid}"): 401,
    ("DELETE", "/conversations/{cid}"): 401,
    ("POST", "/conversations/{cid}/pin"): 401,
    ("PATCH", "/conversations/{cid}"): 401,
    ("POST", "/troubleshoot/run"): 401,
    ("POST", "/qa/run"): 401,
    ("GET", "/documents/download"): 401,
    ("GET", "/ingest/preview"): 401,
    ("GET", "/documents"): 401,
    ("GET", "/admin/es/search"): 401,
    ("GET", "/world-options"): 401,
    ("GET", "/fs/list"): 401,
    ("GET", "/worlds"): 401,
    ("GET", "/worlds/{wid}/status"): 401,
    ("POST", "/worlds/{wid}/recount"): 401,
    ("POST", "/worlds"): 401,
    ("POST", "/worlds/diff"): 401,
    ("GET", "/worlds/{wid}/diff"): 401,
    ("POST", "/worlds/{wid}/rebind"): 401,
    ("POST", "/worlds/{wid}/refresh"): 401,
    ("POST", "/worlds/{wid}/reconvert"): 401,
    ("POST", "/worlds/{wid}/rag_regenerate_rules"): 401,
    ("DELETE", "/worlds/{wid}"): 401,
    ("GET", "/graph"): 401,
    ("GET", "/graph/facets"): 401,
    ("GET", "/graph/search"): 401,
    ("POST", "/graph/ask"): 401,
    ("POST", "/ingest/rerun"): 401,
    ("GET", "/ingest/runs"): 401,
    ("GET", "/healthz"): 200,
    # Swagger UI は自前ルート化のため APIRoute として EXPECTED に載る（認証不要の公開系）。
    # ReDoc（/redoc）は提供しない＝ルートが存在しないため EXPECTED にも載せない（404 は
    # `_api_route_keys()` の対象外＝そもそもプローブされない）。
    ("GET", "/docs"): 200,
    ("GET", "/docs/oauth2-redirect"): 200,
    ("GET", "/health/summary"): 401,
    ("GET", "/admin/health"): 401,
    ("GET", "/"): 307,
    ("POST", "/ext/v1/convert"): 401,               # E1: X-API-Key 認証（Cookie セッションではない）
    ("POST", "/ext/v1/search"): 401,                # E2c: X-API-Key 認証
    ("GET", "/ext/v1/capabilities"): 401,           # X-API-Key 認証（discovery）
    ("GET", "/ext/v1/doc"): 401,                    # X-API-Key 認証（原本取得）
    ("POST", "/ext/v1/research"): 401,              # PART-4: X-API-Key 認証（AI 下調べ検索）
    ("GET", "/ext/v1/openapi.json"): 401,           # E2c: X-API-Key 認証（匿名面を作らない・D6）
    ("POST", "/ext/v1/admin/keys"): 401,
    ("GET", "/ext/v1/admin/keys"): 401,
    ("DELETE", "/ext/v1/admin/keys/{key_id}"): 401,
    ("POST", "/ext/v1/admin/keys/recover"): 401,
    # 利用者本人による自己発行/一覧/失効/回復。
    # Cookie セッション認証（`_current_user`）＝admin 系と同じ 401（X-API-Key ではない）。
    ("POST", "/ext/v1/keys"): 401,
    ("GET", "/ext/v1/keys"): 401,
    ("DELETE", "/ext/v1/keys/{key_id}"): 401,
    ("POST", "/ext/v1/keys/recover"): 401,
}


def test_route_set_matches_expected_table():
    """ルート集合が EXPECTED の (method, path) 集合と一致する（追加/削除時に表更新を強制）。"""
    actual = _api_route_keys()
    expected = set(EXPECTED)
    assert actual == expected, (
        "ルート増減あり: EXPECTED 表を更新すること。\n"
        f"表に無い新規ルート: {sorted(actual - expected)}\n"
        f"表にあるが消えたルート: {sorted(expected - actual)}"
    )


def test_unauthenticated_status_snapshot():
    """未ログインで各ルートを叩いた実測ステータスが EXPECTED と一致する（認可退行の検知）。"""
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    mismatches = []
    for (method, path), want in EXPECTED.items():
        got = _request(client, method, path).status_code
        if got != want:
            mismatches.append(f"{method} {path}: 期待 {want} 実測 {got}")
    assert not mismatches, "未ログイン時のステータスが変化:\n" + "\n".join(mismatches)
