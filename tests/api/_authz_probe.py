"""認可プローブ共通基盤（フェーズ7 S4・test_auth_snapshot.py から抽出）。

`tests/api/test_auth_snapshot.py`（未ログイン×全ルートのステータス snapshot）と
`tests/api/test_authz_matrix.py`（役割×全ルートの認可マトリクス）が共有する「全 APIRoute を
決定的に叩く」ための土台（プレースホルダ・最小 body・request ヘルパ・ルート列挙）を1本化する。
ロジックは test_auth_snapshot.py から移動のみ（挙動は変更しない）。

測り方の約束（両テストファイル共通）:
  - GET は共通のダミー query（`query`/`q`/`rel`）を常に付す。追加の必須 query を持つルートは
    `_EXTRA_QUERY` で個別に補う。
  - POST/PUT/PATCH は既定で空 body（`{}`）で叩く。Pydantic の必須フィールドを持つ body は
    `_JSON_BODY` で**各モデルの必須フィールドだけを満たす最小 payload**を用意し、
    body validation を通過させて handler 内の認証チェックまで到達させる
    （さもないと 401 でなく 422 で止まり、認可退行を検出できない）。
  - multipart（`UploadFile = File(...)`）は `_MULTIPART_ROUTES` で最小ファイルを添付する。
  - path パラメータは `_PLACEHOLDERS` のダミー値で埋める。

S4 で `id`/`key_id` を "1" から大きな未使用値へ変更した理由: test_authz_matrix.py は
（test_auth_snapshot.py と異なり）ログイン済みセッションで実際に handler の中まで実行させる
「許可側」probe も行う。dev の Postgres は共有・汚染された実データを持つため（既存メモ参照）、
"1" のような小さい ID が実在の `announcements`/`ext_api_keys` 行と衝突すると、admin probe が
本物の行を書き換え/失効させてしまいかねない。実在しないことがほぼ確実な大きな ID にして
「対象が見つからない → 404（副作用なし）」を保証する。
"""
from __future__ import annotations

import re

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from sherpa.api import app

_PLACEHOLDERS = {
    "uid": "admin", "cid": "1", "token": "tok", "share_id": "1",
    "file_id": "1", "aid": "1", "wid": "w1", "source_id": "1",
    "id": "999999999", "turn_id": "x", "key_id": "999999999",
    "conversation_id": "999999999", "message_id": "999999999",
}

# POST/PUT/PATCH の最小有効 body（Pydantic の必須フィールドだけ満たし、handler 先頭の
# `_current_user`/`_require_admin` まで到達させる）。値は認証チェックより前に評価されないため、
# 型さえ満たせば内容は任意。ここに無いルートは全フィールドが optional（既定 `{}` で
# validation を通る）か、body 自体が無いため既定の空 `{}` のままで良い。
# 対応する Pydantic モデルは sherpa/api.py・sherpa/routers/*.py（ExtSearchReq のみ sherpa/ext_api.py）参照:
#   PasswordChangeReq / UserCreateReq / ShareCreateReq / ImpactReq / ChatReq / ChatStreamStopReq /
#   TestReq / BedrockVerifyReq / RenameReq / TroubleshootReq / QaReq / WorldReq / DiffReq /
#   DisableReq / RebindReq / ReconvertReq / GraphAskReq / AnnouncementCreateReq
# （`/admin/usage/chat`・`POST /chat/{conversation_id}/messages/{message_id}/feedback` は
#   body の型を固定しない自前パース（`routers/audit_usage.py::admin_usage_chat`・
#   `routers/chat.py::chat_message_feedback`）のため対象外・下の最小 body は認証チェックへ
#   到達させるためだけの任意の妥当な値）
_JSON_BODY: dict[tuple[str, str], dict] = {
    ("POST", "/auth/change-password"): {
        "current_password": "x", "new_password": "x", "confirm_password": "x"},
    ("POST", "/admin/users"): {"uid": "probeuser", "password": "x"},
    ("POST", "/admin/announcements"): {"title": "x", "body": "x"},
    ("POST", "/conversations/{cid}/shares"): {
        "invitee_user_ids": ["x"], "expires_at": "2999-01-01T00:00:00+00:00"},
    ("POST", "/impact/run"): {"start": "x"},
    ("POST", "/chat"): {"message": "x"},
    # RV MEDIUM（2026-07-03再検証）: stream_id は UUID相当の形式制約（最短8文字）を持つため、
    # 1文字の "x" では body validation（422）で止まってしまい認証チェックまで届かない。
    ("POST", "/chat/stream/stop"): {"stream_id": "x1234567"},
    ("POST", "/chat/turns"): {"message": "x"},   # ChatReq と同一モデル（背景実行版チャット送信）
    ("POST", "/chat/{conversation_id}/messages/{message_id}/feedback"): {"rating": "up"},
    ("POST", "/settings/test"): {"provider": "openai"},
    ("POST", "/settings/bedrock-models/verify"): {"model_id": "x"},
    ("PATCH", "/conversations/{cid}"): {"title": "x"},
    ("POST", "/troubleshoot/run"): {"symptom": "x"},
    ("POST", "/qa/run"): {"question": "x"},
    ("POST", "/worlds"): {"path": "/tmp/sherpa-auth-snapshot-probe"},
    ("POST", "/worlds/diff"): {"path": "/tmp/sherpa-auth-snapshot-probe"},
    ("POST", "/worlds/{wid}/rebind"): {"path": "/tmp/sherpa-auth-snapshot-probe"},
    ("POST", "/worlds/{wid}/reconvert"): {"rel": "x"},
    ("POST", "/graph/ask"): {"question": "x"},
    ("POST", "/admin/usage/chat"): {"question": "x"},
    ("POST", "/ext/v1/admin/keys"): {"label": "x"},
    ("POST", "/ext/v1/search"): {"world": "x", "query": "x"},
    ("POST", "/ext/v1/keys"): {"label": "x"},
    # `client_op_id` は必須かつ UUID 形式のみ受理（さもないと 422 で止まり 401 を検出できない）。
    ("POST", "/ext/v1/admin/keys/recover"): {"client_op_id": "00000000-0000-4000-8000-000000000000"},
    ("POST", "/ext/v1/keys/recover"): {"client_op_id": "00000000-0000-4000-8000-000000000000"},
}

# GET の追加必須 query（共通ダミー `query`/`q`/`rel` だけでは満たせないルート専用）。
_EXTRA_QUERY: dict[tuple[str, str], dict] = {
    ("GET", "/chat/stream"): {"message": "x"},
    ("GET", "/ext/v1/doc"): {"world": "x", "path": "x"},
}

# multipart（`UploadFile = File(...)`）で叩く必要があるルート。
_MULTIPART_ROUTES = {("POST", "/workspace/files"), ("POST", "/ext/v1/convert")}


def _fill(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: _PLACEHOLDERS.get(m.group(1), "x"), path)


def _add_route_keys(keys: set[tuple[str, str]], r: APIRoute) -> None:
    for m in r.methods:
        if m not in ("HEAD", "OPTIONS"):
            keys.add((m, r.path))


def _api_route_keys() -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for r in app.routes:
        if isinstance(r, APIRoute):
            _add_route_keys(keys, r)
        elif hasattr(r, "effective_route_contexts"):
            # FastAPI 0.139+ の遅延 `app.include_router()`（`_IncludedRouter`・E1 の ext_api.router で
            # 初導入）。app.routes には未展開のまま積まれるため、実体の APIRoute を展開して数える
            # （さもないと /ext/v1/convert がルート集合チェックから漏れて認可退行検知が効かない）。
            for ctx in r.effective_route_contexts():
                if isinstance(ctx.original_route, APIRoute):
                    _add_route_keys(keys, ctx.original_route)
    return keys


def _request(client: TestClient, method: str, path: str, *, extra_query: dict | None = None,
             json_override: dict | None = None):
    """`(method, path)` を `_PLACEHOLDERS`/`_JSON_BODY`/`_EXTRA_QUERY`/`_MULTIPART_ROUTES` に
    従って埋めて叩く。`extra_query` は呼び出し側が追加の query を足したい場合（例:
    `include_unpublished=true` の条件付き認可テスト）に使う。`json_override` は非 GET/DELETE の
    JSON body を丸ごと差し替える（RV MED 2026-07-14: 認可マトリクスの「安全な許可側 probe」用＝
    handler 到達後・外部接続/実行前の決定的分岐で返させる専用 body）。"""
    url = _fill(path)
    key = (method, path)
    if key in _MULTIPART_ROUTES:
        return client.post(url, files={"file": ("probe.txt", b"probe", "text/plain")})
    if method == "GET":
        params = {"query": "x", "q": "x", "rel": "x", **_EXTRA_QUERY.get(key, {}), **(extra_query or {})}
        return client.get(url, params=params)
    if method == "DELETE":
        return client.delete(url)
    body = json_override if json_override is not None else _JSON_BODY.get(key, {})
    return client.request(method, url, json=body)
