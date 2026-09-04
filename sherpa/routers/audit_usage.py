"""管理者:監査ログ + 管理者:利用統計エンドポイント（フェーズ3スライス2・純移動）。

`GET /admin/audit`・`GET /admin/audit/verify`・`GET /admin/audit/export`・
`GET /admin/usage/stats` を api.py から抽出する。ロジックは変更しない（コード移動のみ）。
ルート表 golden の定義順を保つため、api.py 側は削除したブロックの元位置に
`app.include_router(audit_usage.audit_usage_router)` を1回だけ置く。

このモジュールは `sherpa.api` を import しない（循環回避）。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from sherpa import store, usage_chat
from sherpa.deps import _current_user, _require_admin
from sherpa.schemas import AdminAuditListResponse, AdminUsageStatsResponse, UsageChatResponse

_log = logging.getLogger("sherpa")

# router に tags を持たせない: 各エンドポイントの `tags=["管理者:監査ログ"]`/`["管理者:利用統計"]` と
# 結合されて二重化してしまう（ルート表 golden 不一致の原因）ため、tags 指定は各デコレータ側のみに
# 残す（system.py:42-44 と同じパターン）。
audit_usage_router = APIRouter()


# ===== 管理者: 監査ログ閲覧 =====

@audit_usage_router.get("/admin/audit", tags=["管理者:監査ログ"], response_model=AdminAuditListResponse)
def admin_audit_list(
    request: Request,
    actor: str | None = Query(None),
    action: str | None = Query(None),
    resource_type: str | None = Query(None),
    resource_id: str | None = Query(None),
    outcome: str | None = Query(None),
    severity: str | None = Query(None),
    time_from: str | None = Query(None),     # ISO 8601 文字列
    time_to: str | None = Query(None),
    request_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """監査ログ閲覧（管理者のみ）。actor/action/resource等でフィルタし、閲覧自体を admin.audit_viewed として記録（fail-closed）。"""
    u = _current_user(request)
    _require_admin(u)

    rows = store.list_audit(
        actor=actor, action=action, resource_type=resource_type,
        resource_id=resource_id, outcome=outcome, severity=severity,
        time_from=time_from, time_to=time_to, request_id=request_id,
        limit=limit, offset=offset,
    )
    filters = {k: v for k, v in {
        "actor": actor, "action": action, "resource_type": resource_type,
        "resource_id": resource_id, "outcome": outcome, "severity": severity,
        "time_from": time_from, "time_to": time_to, "request_id": request_id,
        "limit": limit, "offset": offset,
    }.items() if v is not None and v != 0}

    # 閲覧自体を監査（fail-closed: 書けなければ応答しない）
    try:
        store.audit(u["uid"], "admin.audit_viewed", "audit_log", None,
                    detail={"filters": filters, "result_count": len(rows)},
                    outcome="success", severity="critical")
    except Exception:
        _log.critical("audit write failed for admin.audit_viewed – fail-closed")
        raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed）")

    return {"rows": rows, "count": len(rows), "offset": offset, "limit": limit}


@audit_usage_router.get("/admin/audit/verify", tags=["管理者:監査ログ"])
def admin_audit_verify(request: Request):
    """監査ログの hash-chain 整合性を検証（管理者のみ・§Phase2 改ざん検知）。
    ok=false かつ broken_at にズレた行 id を返す。検証自体も監査に残す。"""
    u = _current_user(request)
    _require_admin(u)
    result = store.verify_audit_chain()
    try:
        store.audit(u["uid"], "admin.audit_verified", "audit_log", None,
                    detail=result, outcome="success" if result.get("ok") else "failure",
                    severity="critical")
    except Exception:
        _log.critical("audit write failed for admin.audit_verified – fail-closed")
        raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed）")
    return result


_AUDIT_EXPORT_FIELDS = [
    "id", "created_at", "actor_user_id", "action", "resource_type", "resource_id",
    "outcome", "reason", "severity", "request_id", "session_id", "ip_hash",
    "user_agent", "detail", "before_state", "after_state",
]


def _audit_export_rows(**filters) -> list[dict]:
    """監査ログを export 用にページング取得する。"""
    rows: list[dict] = []
    offset = 0
    page = 500
    max_rows = min(int(filters.pop("max_rows", 50000) or 50000), 50000)
    while len(rows) < max_rows:
        batch = store.list_audit(limit=min(page, max_rows - len(rows)), offset=offset, **filters)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def _audit_export_clean(row: dict) -> dict:
    clean = {}
    for k in _AUDIT_EXPORT_FIELDS:
        v = row.get(k)
        if k in ("detail", "before_state", "after_state"):
            v = store._redact(v) if v is not None else None
        if isinstance(v, datetime):
            v = v.astimezone(timezone.utc).isoformat()
        clean[k] = v
    return clean


def _audit_export_filename(fmt: str) -> str:
    return f"sherpa-audit-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.{fmt}"


# S5: 監査エクスポートの本文結合（include_chat_content=1 の時だけ）。保存済み audit_log.detail 自体は
# 変更しない（hash-chain・append-only）。あくまでエクスポート出力にその場で足すだけ。
_CHAT_CONTENT_DELETED_PLACEHOLDER = "（削除済み）"
_CHAT_CONTENT_PERSONAL_PLACEHOLDER = "（個人ファイル参照ターン・本文はエクスポート対象外）"


def _chat_content_for_export(msg: dict | None, *, turn_personal: bool = False) -> str | None:
    """メッセージ1件をエクスポート用の文字列に落とす（削除済み/個人参照ターンはプレースホルダ）。

    RV HIGH（2026-07-03）: `msg` が存在しても、所属会話が soft-delete 済み（受領共有ラッパーが
    生きているため messages 行自体は物理的に残っている＝`store.get_messages_by_ids` の
    `conv_deleted` フラグ）なら削除済み扱いにする（存在しない id と同じプレースホルダに統一）。

    RV MEDIUM（2026-07-03）: personal 判定は message 個々の flag だけでなく、呼出元が渡す
    `turn_personal`（chat.turn.detail.personal＝ターン全体の記録時点の判定）との OR にする。
    片方の flag だけが立って片方が欠けても（例: 個人参照ターンの assistant 側だけ personal が
    立ち loss/クリア等で user 側が立っていない等）両側ともプレースホルダに落とす。
    """
    if msg is None or msg.get("conv_deleted"):
        return _CHAT_CONTENT_DELETED_PLACEHOLDER
    if turn_personal or msg.get("personal"):
        # 越境防止: 個人 workspace 参照ターンの本文は admin エクスポートにも平文で出さない
        # （sanitized share と同じ posture・[[feedback_received_share_route_trace_leak]] と同種の判断）。
        return _CHAT_CONTENT_PERSONAL_PLACEHOLDER
    return msg.get("content")


def _join_chat_content(rows: list[dict]) -> None:
    """`chat.turn` 行の detail に messages 台帳の本文（user prompt / assistant 回答）を join する。

    N+1 を避けるため、対象行全体から message_id を一括収集して1回で取得する
    （`store.get_messages_by_ids`）。行の `detail` dict はここで初めて拡張する（呼出側の
    `_audit_export_clean` は既に redaction 済みの dict を渡してくる＝この関数は追加キーの
    付与のみ行い、既存キーには触れない）。
    """
    ids: set[int] = set()
    for r in rows:
        if r.get("action") != "chat.turn":
            continue
        d = r.get("detail") or {}
        if d.get("message_id_user"):
            ids.add(d["message_id_user"])
        if d.get("message_id_assistant"):
            ids.add(d["message_id_assistant"])
    if not ids:
        return
    msgs = store.get_messages_by_ids(list(ids))
    for r in rows:
        if r.get("action") != "chat.turn":
            continue
        d = dict(r.get("detail") or {})
        turn_personal = bool(d.get("personal"))
        uid_msg, aid_msg = d.get("message_id_user"), d.get("message_id_assistant")
        d["user_prompt"] = _chat_content_for_export(
            msgs.get(uid_msg), turn_personal=turn_personal) if uid_msg else None
        d["assistant_answer"] = _chat_content_for_export(
            msgs.get(aid_msg), turn_personal=turn_personal) if aid_msg else None
        r["detail"] = store._redact(d)   # 追加後もう一度 redaction（既存の流儀と同じ多層防御）


@audit_usage_router.get("/admin/audit/export", tags=["管理者:監査ログ"])
def admin_audit_export(
    request: Request,
    format: str = Query("csv", pattern="^(csv|jsonl)$"),
    actor: str | None = Query(None),
    action: str | None = Query(None),
    resource_type: str | None = Query(None),
    resource_id: str | None = Query(None),
    outcome: str | None = Query(None),
    severity: str | None = Query(None),
    time_from: str | None = Query(None),
    time_to: str | None = Query(None),
    request_id: str | None = Query(None),
    include_chat_content: bool = Query(False),
):
    """監査ログを CSV / JSONL でエクスポート（admin のみ）。

    `include_chat_content=1`（S5）を指定した時だけ、`chat.turn` 行に messages 台帳から
    ユーザーのプロンプト・AI の回答（headline）を join して含める。画面（audit.html）は本文を
    出さずシンプルなまま＝本文が要る調査はこのオプション付きエクスポートで行う。未指定なら
    従来どおり本文を含まない（完全互換）。個人参照ターンはプレースホルダに落ちる（越境防止）。
    """
    u = _current_user(request)
    _require_admin(u)
    filters = {
        "actor": actor, "action": action, "resource_type": resource_type,
        "resource_id": resource_id, "outcome": outcome, "severity": severity,
        "time_from": time_from, "time_to": time_to, "request_id": request_id,
    }
    filters = {k: v for k, v in filters.items() if v is not None}
    rows = [_audit_export_clean(r) for r in _audit_export_rows(**filters)]
    if include_chat_content:
        _join_chat_content(rows)

    try:
        store.audit(u["uid"], "admin.audit_exported", "audit_log", None,
                    detail={"format": format, "filters": filters, "result_count": len(rows),
                            "include_chat_content": include_chat_content},
                    outcome="success", severity="critical")
    except Exception:
        _log.critical("audit write failed for admin.audit_exported – fail-closed")
        raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed）")

    filename = _audit_export_filename(format)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if format == "jsonl":
        body = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
        if body:
            body += "\n"
        return Response(content=body, media_type="application/x-ndjson; charset=utf-8",
                        headers=headers)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_AUDIT_EXPORT_FIELDS)
    writer.writeheader()
    for r in rows:
        row = {
            k: json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v
            for k, v in r.items()
        }
        writer.writerow(row)
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers=headers)


# ===== 管理者: 利用統計 =====

@audit_usage_router.get("/admin/usage/stats", tags=["管理者:利用統計"], response_model=AdminUsageStatsResponse)
def admin_usage_stats(request: Request, days: int = Query(30, ge=1, le=365)):
    """利用統計（管理者のみ）。よく使うユーザーを見つけてヒアリング候補にするための集計。

    メッセージ本文・会話タイトルは一切含めない（プライバシー＝件数・日時・種別のみ）。
    閲覧自体を admin.usage_viewed として記録する（audit.html の慣行に合わせる・fail-closed）。
    """
    u = _current_user(request)
    _require_admin(u)
    result = store.usage_stats(days=days)
    try:
        store.audit(u["uid"], "admin.usage_viewed", "usage", None, detail={"days": days},
                    outcome="success", severity="info")
    except Exception:
        _log.critical("audit write failed for admin.usage_viewed – fail-closed")
        raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed）")
    return result


_HISTORY_HARD_CAP = 10_000

# `POST /admin/usage/chat` の生 body 読み込み上限（バイト）。正当な入力（質問2000字＋履歴20件×
# 各4000字）は utf-8 最悪見積もりでも1MiBに満たないため、大きく余裕を持たせつつ、
# `_HISTORY_HARD_CAP`（10000件）判定に到達する前の巨大な body でメモリを圧迫できないよう
# 上限を設ける（`workspace.py::workspace_file_upload` と同じ、チャンク読みで打ち切る流儀）。
# この上限は**バイト数**（デコード前の生 body）を見る＝`QUESTION_MAX_LEN`/`HISTORY_ITEM_MAX_LEN`
# 等の**文字数**上限より先に・独立に評価される（文字数はデコード後でなければ数えられないため、
# 文字数チェックに到達する前にバイト数だけで打ち切れることがこの上限の存在意義そのもの）。
_USAGE_CHAT_BODY_MAX_BYTES = 1_048_576   # 1MiB

# Unicode の BOM（byte order mark）バイト列。`json.loads()` は bytes を渡すと BOM や NUL バイトの
# 並びから UTF-8/16/32 を暗黙に推測する（RFC 8259 の sniffing）ため、UTF-8 以外（またはUTF-8+BOM）
# の入力を意図せず受理してしまう。本文は明示的に UTF-8（BOM なし）としてのみデコードし、それ以外は
# 固定文言の 400 として拒否する（`_read_capped_json_body` 参照）。
_BOM_PREFIXES = (b"\xef\xbb\xbf", b"\xfe\xff", b"\xff\xfe", b"\x00\x00\xfe\xff")

_BODY_PARSE_ERROR_MSG = "リクエスト本文が解析できません（UTF-8・BOM なしの JSON のみ受理します）"

# `admin_usage_chat` の実際の受理契約を機械可読な JSON Schema で表す（`openapi_extra` として
# 付与する・`response_model` に対する request 版）。ここに表せるのは「通常の受理契約」
# （質問必須・履歴は {role, content} オブジェクトの配列で件数・文字数上限あり）までで、
# 二段構えの防御的上限（`history` が `_HISTORY_HARD_CAP` 超過→422・本文サイズが
# `_USAGE_CHAT_BODY_MAX_BYTES` 超過→413）は JSON Schema の「妥当性」の外側（DoS 対策の
# 別レイヤ）にあるため、`responses`（下記デコレータ）の説明文で別途明記する。
_USAGE_CHAT_REQUEST_BODY_SCHEMA = {
    "type": "object",
    "required": ["question"],
    "properties": {
        "question": {
            "type": "string",
            "description": (
                f"質問文。前後の空白を除いた長さが1〜{usage_chat.QUESTION_MAX_LEN}字である必要が"
                "ある（前後の空白のみを理由に超過している場合は空白を除けば受理される・空白のみは"
                "拒否）。"
            ),
        },
        "history": {
            "type": ["array", "null"],
            "maxItems": usage_chat.HISTORY_MAX_ITEMS,
            "description": "直近の会話履歴。省略/null は空履歴扱い。",
            "items": {
                "type": "object",
                "required": ["role", "content"],
                "properties": {
                    "role": {
                        "type": "string",
                        # 決定（RV9 #3）: enum は「正規化後（サーバが受理・比較に使う）値」を表す
                        # ものとして維持する（JSON Schema には enum を保ったまま「大小文字・前後
                        # 空白を無視する」ことを移植可能な形で表現する標準的な手段が無いため、
                        # pattern で近似するより description で明示する方が正確）。ワイヤ上の
                        # 実際の受理範囲（大文字・前後空白を許容）は description に明記する。
                        "enum": list(usage_chat._HISTORY_ROLES),
                        "description": (
                            "role（正規化後の値を列挙）。実際の送信値はこの一覧と完全一致して"
                            "いなくてもよい——サーバは前後の空白を除去し小文字化してから判定する"
                            "ため、例えば \" USER \" や \"Assistant\" も受理され、それぞれ"
                            "\"user\"／\"assistant\" として扱われる。"
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            f"前後の空白を除いて{usage_chat.HISTORY_ITEM_MAX_LEN}字を超える場合は"
                            "拒否せず、末尾を切り詰めて（省略の印を付けて）受理する。"
                        ),
                    },
                },
            },
        },
        # STAT-2: 画面の「今回だけ OpenAI／Ollama で」トグルによる、リクエスト単位の一時的な
        # プロバイダ上書き（保存しない）。省略/null は管理者全体の専用設定
        # （`system_settings["usage_chat_provider"]`）に従う。
        "provider": {
            "type": ["string", "null"],
            "enum": [*usage_chat._USAGE_CHAT_PROVIDERS, None],
            "description": (
                "今回だけ使う AI（省略/null は管理画面の設定に従う）。"
                f"指定する場合は {'/'.join(usage_chat._USAGE_CHAT_PROVIDERS)} のいずれか——前後の"
                "空白を除去し小文字化してから判定するため、例えば \" OpenAI \" や \"OLLAMA\" も"
                "受理される。それ以外の値（空文字を含む）は 400。保存はしない。"
            ),
        },
    },
}


async def _read_capped_json_body(request: Request) -> Any:
    """`request` の本文をチャンク読みで `_USAGE_CHAT_BODY_MAX_BYTES` まで読み、UTF-8（BOM なし）
    として明示的にデコードしてから JSON として解析する。

    FastAPI の `Body()` 依存性注入（内部で `await request.json()` を呼ぶ）はハンドラ本体に
    到達する前に本文全体を無条件にバッファするため、サイズ上限を効かせるにはそれより前に
    自前でチャンク読みする必要がある（`workspace.py::workspace_file_upload` と同じ理由）。

    サイズ上限超過は `HTTPException(413)`（固定文言・値は反射しない・意図的に監査を経由しない——
    `_HISTORY_HARD_CAP` 超過と同じ、サイズそのものが乱用のシグナルという扱い）。本文が空なら
    `None`。BOM 付き・UTF-8 として不正・JSON として解析できない・ネストが深すぎる
    （`RecursionError`）・巨大な整数リテラル（`json.loads` の数値パースが送出する素の
    `ValueError`）のいずれも同じ固定文言 `_BODY_PARSE_ERROR_MSG`（詳細を反射しない）の
    `ValueError` を送出する（呼び出し元が入力検証エラーとして 400・監査ありに変換する）。
    `json.JSONDecodeError` は `ValueError` の派生のため、`ValueError` 一括捕捉で両方を含む。
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _USAGE_CHAT_BODY_MAX_BYTES:
            raise HTTPException(
                413, f"リクエスト本文が上限（{_USAGE_CHAT_BODY_MAX_BYTES // (1024 * 1024)}MiB）"
                    "を超えています")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        return None
    if raw.startswith(_BOM_PREFIXES):
        raise ValueError(_BODY_PARSE_ERROR_MSG)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(_BODY_PARSE_ERROR_MSG) from e
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as e:
        raise ValueError(_BODY_PARSE_ERROR_MSG) from e


@audit_usage_router.post(
    "/admin/usage/chat", tags=["管理者:利用統計"], response_model=UsageChatResponse,
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
        "schema": _USAGE_CHAT_REQUEST_BODY_SCHEMA}}}},
    responses={
        400: {"description": "質問が空、またはリクエスト本文/質問/会話履歴が不正・上限"
                             "（文字数・件数）を超えています。一時上書き（provider）が"
                             "\"openai\"/\"ollama\" のどちらでもない場合（空文字・型不正な値を"
                             "含む）も同様に 400 です"},
        413: {"description": "リクエスト本文がサイズ上限を超えています"},
        422: {"description": f"会話履歴が防御的な上限（{_HISTORY_HARD_CAP}件）を超えています"
                             "（業務上限を超える400とは別の、意図的に監査を経由しない拒否）"},
        500: {"description": "監査ログの記録に失敗しました（fail-closed）。実送信前（pending 記録前、"
                             "または pending 記録後でも AI が未接続と判明し実送信していない場合を含む）"
                             "の失敗なら AI へは送信していません。実送信を試みた後の結果記録の失敗なら"
                             "送信は完了している可能性があり、再試行は重複送信になり得ます"
                             "（detail の文言でどちらかを区別できます）"},
        502: {"description": "AI プロバイダへの送信を行ったが失敗しました"
                             "（タイムアウト・HTTP エラー・不正な応答・別プロバイダへは自動フォールバックしません）。"
                             "応答本文の detail は固定文言で、実際に送信を試みた provider_used/"
                             "endpoint_kind（openai 使用時のみ意味を持つ）を同梱します"},
        503: {"description": "利用統計チャットに使う AI（管理画面の専用設定・OpenAI/Ollama）が"
                             "未設定/未接続です。応答本文の detail は固定文言で、送信先が確定した"
                             "後の拒否なら実際の送信先を provider_used/endpoint_kind に同梱します"
                             "（送信先が確定する前の拒否＝専用設定の保存値自体が不正な場合は両方"
                             " null。一時上書き（provider）自体の値が不正な場合はサーバ側設定の"
                             "不備ではなく利用者入力の不備のため 400 になります）"},
    },
)
async def admin_usage_chat(request: Request):
    """利用統計チャット（管理者のみ）。直近の利用統計（件数・日時・トークン量のみ）と改善ログの
    要約（フィードバック件数・タグ分布・stop_reason 分布・honest_failure 率・所要時間の分布のみ。
    質問/一言コメントは👎が付いたものだけ先頭100字に切り詰めて含まれることがある）を根拠に、
    自然言語の質問へ AI が回答する（会話本文・会話タイトルを丸ごとコンテキストに含めることはない）。

    リクエスト body の型は固定しない（`_USAGE_CHAT_REQUEST_BODY_SCHEMA` 参照）——固定すると
    型不正な値・トップレベルがオブジェクトでない body（配列・文字列・`null` 等）がハンドラに
    到達する前に FastAPI/pydantic の自動 422 で弾かれ、「入力検証を含む全経路を監査する」契約
    （下記 `_audit`）を経由しない盲点になる。トップレベルがオブジェクトでない場合も含め、
    型不正はすべてここ（認証チェック後）で 400（監査あり）として拒否する（値そのものは
    反射しない）。唯一の例外が `history` の件数上限超過（`_HISTORY_HARD_CAP`）と本文サイズ
    超過（`_USAGE_CHAT_BODY_MAX_BYTES`）: どちらも意図的に監査を経由しない
    422/413（DoS 対策の防御的な上限）のまま拒否する。

    プロバイダ失敗時は別プロバイダへ自動フォールバックしない（明示エラー・503/502）。
    成功/失敗（入力検証エラー・想定外の例外を含む）の全経路で admin.usage_chat_asked を監査する
    （fail-closed・盲点を作らない）。入力検証段階で終わる経路は1行（400/500）。実送信
    （`answer_usage_question`）まで進む経路は2行（送信前の pending 行＋結果行〔成功/503/502/500〕・
    下記 `_audit`/`request_token` 参照）。監査の書き込みは同期 DB 呼び出しのため
    `run_in_threadpool` 経由で行う（下記参照）。

    非同期ハンドラ内の同期呼び出しについて: このハンドラは本文読み込み（`_read_capped_json_body`）
    以外は全て同期の DB/LLM 呼び出しである。`def`（同期）ハンドラなら FastAPI が自動的に
    threadpool 実行してくれるが、本文のチャンク読みに `await request.stream()` が要るため
    `async def` にしている＝この関数自身は自動 threadpool の対象外になる。そのため、認証
    （`_current_user`/`_require_admin`）・監査（`_audit`）・本処理（`answer_usage_question`。
    設定取得を含め最大60秒の LLM 呼び出しを伴う）を明示的に `run_in_threadpool` へ委譲する
    （単一 worker プロセス構成のため、これを怠ると1回の利用統計チャット呼び出し中、他の全
    API・healthz が応答不能になる）。`answer_usage_question` は `metering.acc_begin()`/
    `acc_add()`/`acc_end()` という thread-local な累積カウンタを1回の呼び出し内で使い切る契約
    （`metering.py` 参照）のため、設定取得も含めて丸ごと1回の `run_in_threadpool` 呼び出しに
    収める（`_run_answer` 参照）——`acc_begin`/`acc_end` を別々の `run_in_threadpool` 呼び出しに
    分けると、それぞれ異なるスレッドで実行されうるため thread-local が別物になり集計が壊れる。
    """
    def _authn() -> dict:
        u = _current_user(request)
        _require_admin(u)
        return u
    u = await run_in_threadpool(_authn)

    history_truncated = False
    question_raw: Any = None
    history_raw: Any = None
    # `validate_request` 成功後にのみ正規化済み history（常に list）を入れる。監査の
    # history_len は、成功していればこちら（`None` の受理も含め常に整合した件数）を優先する
    # （生値が `null`/省略のときに `history_len` を `None`（不明）でなく実際の 0 として
    # 記録するため）。
    history_normalized: list | None = None
    # STAT-2: 画面の「今回だけ」トグルによる一時プロバイダ上書き（`validate_provider_override`
    # 検証後の値・保存しない）。監査に残す＝どちらの AI へ送ったかの証跡（None＝管理画面の
    # 専用設定どおり）。
    provider_override: str | None = None
    # 実際に使われた（使おうとした）provider/接続先種別。成功時は `answer_usage_question` の
    # 戻り値から、503/502（送信先が確定した後の失敗）は例外の `.provider`/`.endpoint_kind`
    # （`usage_chat._unavailable`/`LLMCallFailedError` 参照）から設定する。送信先が確定する前の
    # 失敗（専用設定の保存値自体が不正）だけ None のまま——一時上書き自体の値が不正な場合は
    # ここへ到達する前に 400（入力検証エラー）で止まる。結果監査 detail と応答本文の
    # 両方に載せる。
    provider_used: str | None = None
    endpoint_kind_used: str | None = None
    # pending 行と結果行を1つの送信として結び付ける相関 ID（`store.audit` の request_id 列）。
    request_token = uuid.uuid4().hex

    def _audit(outcome: str, status_code: int | None, reason: str | None = None, *,
              pre_send: bool, improvement_log_failed: bool = False) -> None:
        try:
            # 監査用の長さは文字列以外に対して `str(...)`/`len()` を無条件には適用しない
            # （巨大な dict/list の全体文字列化を避ける・型不正な値は None として記録する）。
            q_len = len(question_raw) if isinstance(question_raw, str) else None
            h_len = len(history_normalized) if history_normalized is not None else (
                len(history_raw) if isinstance(history_raw, list) else None)
            store.audit(u["uid"], "admin.usage_chat_asked", "usage", None,
                        detail={"question_len": q_len, "history_len": h_len,
                                "status_code": status_code, "reason": reason,
                                "history_truncated": history_truncated,
                                "provider_override": provider_override,
                                "provider_used": provider_used, "endpoint_kind": endpoint_kind_used,
                                "improvement_log_failed": improvement_log_failed},
                        outcome=outcome, severity="info", request_id=request_token)
        except Exception:
            _log.critical("audit write failed for admin.usage_chat_asked – fail-closed")
            # `pre_send`（呼び出し元が渡す）は「実送信の前か後か」ではなく「未送信であると
            # 断定できるか」を表す: pending 行（実送信より前）に加え、`LLMUnavailableError`
            # （`_resolve_cfg`/`complete_json` 内部の権威あるガードが実送信前に拒否した場合
            # だけ送出される・`usage_chat.answer_usage_question` 参照）による失敗も、pending
            # 行の書き込み後ではあるが実際には一度も送信していないため True にする。それ以外
            # （実送信を試みた `LLMCallFailedError`／成功／想定外の例外）は、送信自体は完了
            # している可能性がある（この監査書き込みが失敗しただけ）ため、再試行が重複送信に
            # なり得ることを明示する。
            if pre_send:
                raise HTTPException(
                    500, "監査ログの記録に失敗しました（fail-closed・AI へは送信していません）")
            raise HTTPException(
                500, "送信結果の監査記録に失敗しました（AI への送信は完了している可能性があります・"
                    "再試行は重複送信になり得ます）")

    async def _audit_async(outcome: str, status_code: int | None, reason: str | None = None, *,
                           pre_send: bool, improvement_log_failed: bool = False) -> None:
        await run_in_threadpool(_audit, outcome, status_code, reason, pre_send=pre_send,
                                improvement_log_failed=improvement_log_failed)

    # 検証と本処理を別々の try に分ける（`ValueError` の捕捉範囲を検証段階だけに絞るため）。
    # 各 try は末尾に `except Exception:` を持つ＝どちらの段でも「決めた例外型以外の想定外の
    # 例外」を含めて監査してから re-raise する（盲点ゼロ）。1本の try に両方まとめてしまうと、
    # 本処理側（`answer_usage_question`・設定取得）が何らかの理由で `ValueError` を投げた場合に
    # 「入力検証エラー」と誤分類され、400（＋その例外文）をクライアントへ返してしまう
    # （本処理側の内部エラーは 500 が正しい）。
    try:
        body = await _read_capped_json_body(request)
        if not isinstance(body, dict):
            raise ValueError("リクエスト本文はオブジェクト形式で指定してください")
        question_raw = body.get("question")
        history_raw = body.get("history")
        if isinstance(history_raw, list) and len(history_raw) > _HISTORY_HARD_CAP:
            # 極端な件数超過は業務上限（400・監査あり）とは意図的に別扱い（docstring 参照）。
            raise HTTPException(422, f"会話履歴は{_HISTORY_HARD_CAP}件以内にしてください")
        # `validate_request` の戻り値（trim 済み question/history）をそのまま以降の送信に使う。
        # 生値を以降で使うと、trim 前提の上限チェックを前後の空白パディングで迂回されうる
        # （検証は trim 後、送信は trim 前、という不一致を作らない）。
        question, history, history_truncated = usage_chat.validate_request(question_raw, history_raw)
        history_normalized = history
        # STAT-2: 一時プロバイダ上書き（`question`/`history` と同じ 400・監査ありの扱い）。
        provider_override = usage_chat.validate_provider_override(body.get("provider"))
    except HTTPException:
        raise
    except ValueError as e:
        await _audit_async("failure", 400, "invalid_input", pre_send=True)
        raise HTTPException(400, str(e))
    except Exception:
        await _audit_async("failure", 500, "unexpected_error", pre_send=True)
        raise

    # fail-closed（外部送信の監査もれ防止）: 実送信（`answer_usage_question`）の**前**に
    # pending 行を確保する。監査ログは hash-chain の追記専用台帳（改ざん検知のため UPDATE
    # しない）のため、「1行を後から書き換える」のではなく pending 行→結果行の2行構成にし、
    # 同じ `request_id`（`request_token`）で対応付ける。これが無いと、送信は成功したのに
    # 結果行の書き込みだけが失敗した場合（DB 一時障害等）、統計データは外部 AI へ渡った事実が
    # 監査に一切残らないまま 500 を返すことになる（fail-closed の実効性が送信後の監査書き込みの
    # 成否に依存してしまっていた＝この pending 行の存在自体が「実送信を試みた」ことの証跡になる）。
    # この書き込みに失敗した場合は `_audit` 自身が 500 を送出し、実送信（下記）は行われない。
    await _audit_async("pending", None, None, pre_send=True)

    def _run_answer() -> dict:
        # 設定取得（DB）〜 answer_usage_question（DB usage_stats・最大60秒の LLM 呼び出し）まで
        # 丸ごと1回の `run_in_threadpool` 呼び出しに収める（関数 docstring の metering 契約参照）。
        # STAT-2: 利用者の個人設定（`store.get_settings`）は使わない＝実行構成に依存しない契約。
        return usage_chat.answer_usage_question(
            question, history, system_settings=store.get_system_settings(), user_id=u["uid"],
            provider_override=provider_override)

    try:
        result = await run_in_threadpool(_run_answer)
    except usage_chat.LLMUnavailableError as e:
        # pending 行の書き込み後だが、`LLMUnavailableError` は実送信前のガードが拒否した
        # ことしか意味しない（`usage_chat.answer_usage_question` 参照）＝未送信を断定できる。
        # 送信先が確定した後の拒否なら `.provider`/`.endpoint_kind`（`usage_chat._unavailable`
        # 参照）に実際の送信先が入る。送信先が確定する前の拒否（専用設定の保存値自体が不正）
        # はどちらも属性を持たず `None` のまま（`usage_chat._unavailable_invalid_provider_value`
        # 参照・一時上書き自体の値が不正な場合は 400 で止まりここへは来ない）——監査 detail・
        # 応答本文の両方にこの値をそのまま載せる（fixed 文言の `detail` 自体は変えず、
        # `provider_used`/`endpoint_kind` を項目として同梱するだけ）。`improvement_log_failed`
        # は例外側に載せてある（`notes` は例外経路では戻り値として届かないため）——改善ログの
        # 要約取得に失敗した直後に LLM も失敗した場合でも監査 detail から失われないようにする。
        provider_used = getattr(e, "provider", None)
        endpoint_kind_used = getattr(e, "endpoint_kind", None)
        improvement_log_failed = getattr(e, "improvement_log_failed", False)
        await _audit_async("failure", 503, "llm_unavailable", pre_send=True,
                           improvement_log_failed=improvement_log_failed)
        return JSONResponse(status_code=503, content={
            "detail": str(e), "provider_used": provider_used, "endpoint_kind": endpoint_kind_used})
    except usage_chat.LLMCallFailedError as e:
        # 実送信を試みた上での失敗＝送信先は必ず確定している（`usage_chat.answer_usage_question`
        # 参照）。
        provider_used = getattr(e, "provider", None)
        endpoint_kind_used = getattr(e, "endpoint_kind", None)
        improvement_log_failed = getattr(e, "improvement_log_failed", False)
        await _audit_async("failure", 502, "llm_call_failed", pre_send=False,
                           improvement_log_failed=improvement_log_failed)
        return JSONResponse(status_code=502, content={
            "detail": str(e), "provider_used": provider_used, "endpoint_kind": endpoint_kind_used})
    except Exception:
        await _audit_async("failure", 500, "unexpected_error", pre_send=False)
        raise
    # 監査の pending/失敗行は「実際に使った provider が未確定」だが、成功行はここで確定する
    # （`_audit` のクロージャがこの後の呼び出しで拾う）。改善ログの要約取得に失敗していた場合
    # （notes が非空）も監査に残す（`answer_usage_question` 自体は fail-open で成功しているため
    # outcome は success のまま・detail だけで区別する）。
    provider_used = result["provider"]
    endpoint_kind_used = result["endpoint_kind"]
    notes = result["notes"]
    await _audit_async("success", 200, pre_send=False, improvement_log_failed=bool(notes))
    return {"answer": result["answer"], "provider_used": provider_used, "endpoint_kind": endpoint_kind_used,
            "notes": notes}
