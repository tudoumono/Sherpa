"""チャット系エンドポイント。`chat_router` は `GET /chat/tools-availability`・`POST /chat/turns`・`GET /chat/turns/{turn_id}/stream`・`GET /chat/turns/running`・`POST /chat/turns/{turn_id}/stop` の 5 ルート。
背景実行のヘルパ `_persist_turn_crash`/`_turn_run_fn` もここに置き、api.py が再エクスポートする（tests が `api._persist_turn_crash(...)` を参照する）。
`sherpa.api` を import しない。
設計: docs/design/chat.md「1ターンの流れ」
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StrictBool, field_validator
from starlette.concurrency import run_in_threadpool

from sherpa import agentic_search, answer_shape, chat_turns, llm, store
from sherpa import stop_kind as stop_kind_mod
from sherpa import tools_pref as tools_pref_mod
from sherpa.agents import get_provider
from sherpa.providers import plain_provider_for
from sherpa.chat_router import extract_slash_lens as _extract_slash_lens
from sherpa.chat_service import _ensure_conversation, _save_investigation_record, stream_message
from sherpa.deps import _USERS_DIR, _WorldField, _current_user, _resolve_world, neo4j_session, validated_scope
from sherpa.schemas import ChatTurnsRunningResponse, ChatTurnStartResponse, ChatTurnStopResponse

_log = logging.getLogger("sherpa")

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
chat_router = APIRouter()


class ChatReq(BaseModel):
    message: str
    world: str | None = _WorldField
    conversation_id: int | None = None
    # 資料参照トグル。既定 True。復元した過去会話は保存された値をそのまま使う。
    knowledge: bool = True
    scope_paths: list[str] = Field(default_factory=list)
    # 探す対象。既定 both＝フィルタなし。
    layer: Literal["docs", "code", "both"] = "both"
    # 調べ方の明示指定。既定 None（省略）＝自動。受理する値は 4 値＋省略のみで、"auto" は受理しない。
    lens: Literal["impact", "troubleshoot", "qa", "author"] | None = None
    # 調べる深さ。既定 "standard"（見直し 2 回・`depth_profile.review_rounds_for`）。
    depth_profile: Literal["quick", "standard", "deep", "max"] = "standard"
    personal: bool = False  # 個人ファイル参照トグル（既定OFF）。
    # Codex の Web 検索をこのチャットで希望するか（既定OFF）。管理者許可と、頭脳が Codex であることが揃わなければサーバ側で常に無効化される（`sherpa/providers/codex/sandbox.py::_web_search_disabled_value` が唯一の判定点）。
    web_search: bool = False
    # 検索経路トグル。既定/省略/null は全 ON。対象は grep/fulltext（ES・全文＋ベクトル）/graph の 3 経路のみ（list_docs/read_around/ask_user は常時ON）で、3 つとも false は 422。
    # キーを `Literal["grep","fulltext","graph"]`・値を `StrictBool` にする（素の `dict[str, bool]` は非 bool 値を静かに bool へ変換するため）。未知キーも型で 422 になる。
    tools: dict[Literal["grep", "fulltext", "graph"], StrictBool] | None = None
    # 利用者が実際に切り替えた軸（画面のチップ操作履歴）。会話メタ（`answer.scope.tools_explicit`）へそのまま残す復元専用の記録で、実行にも 422 判定にも使わない。省略（`None`）は記録なし。
    tools_explicit: list[Literal["grep", "fulltext", "graph"]] | None = None

    @field_validator("tools")
    @classmethod
    def _v_tools(cls, v):
        # 欠落キーを埋めずに生の dict をそのまま保持する（`normalize_tools_pref` は構造検証だけに使い戻り値は捨てる）。埋めると「明示的に true」が失われ、可用性 422 判定（`unavailable_explicit_tools`）が省略キーまで誤検知する。
        if v is not None:
            tools_pref_mod.normalize_tools_pref(v)
        return v


def _knowledge_for_settings(settings: dict, requested: bool) -> bool:
    """`_knowledge_for`/`_prepare_agentic_snapshot` が共有する判定本体（settings は呼び出し側が読んだものを渡す）。
    資料参照は利用者の要求どおり（構成による強制はしない）。オフのターンは `chat_service.stream_message` が簡易と同じ AI を道具なしで呼ぶ。
    設計: docs/design/chat.md「1ターンの流れ」
    """
    return bool(requested)


def _knowledge_for(uid: str, requested: bool) -> bool:
    """このユーザーの実行構成で実際に使う `knowledge`（資料参照）の値（単体呼び出し用・自分で settings を読む）。実 HTTP 入口は `_prepare_agentic_snapshot` が同じ判定（`_knowledge_for_settings`）を、Provider 構築と同じ 1 回の settings 読み取りから行う。"""
    try:
        settings = store.get_settings(uid)
    except Exception:
        return bool(requested)  # 設定を読めないときは要求どおりにする。
    return _knowledge_for_settings(settings, requested)


def _check_chat_write(user: dict, conversation_id: int | None) -> None:
    """チャット書き込み権限チェック。受領共有への追記・他人会話へのアクセスを 403/404 で拒否する。"""
    if conversation_id is None:
        return  # 新規会話は常に許可する。
    # まず読める会話か確認する（他人の ID 直アクセスは None）。
    conv_data = store.get_conversation_for_read(user["uid"], conversation_id)
    if not conv_data:
        # 存在するが別人の会話、または削除済み。
        raise HTTPException(404, "会話が見つかりません")
    if conv_data.get("share_status") == "unavailable":
        raise HTTPException(403, "この共有は利用できません（期限切れ・取消済み）")
    # received_share への追記は拒否する。
    if conv_data["conversation"].get("origin") == "received_share":
        raise HTTPException(403, "共有された会話への追記はできません（読み取り専用）")
    # own でも所有者以外は書き込み不可。
    if not store.owns_conversation(user["uid"], conversation_id):
        raise HTTPException(403, "この会話への書き込み権限がありません")


def _validate_tools_availability(tools: dict | None, availability: dict | None = None) -> None:
    """検索経路トグルで明示的に ON 指定したツールが実接続で到達不可なら 422（ツール名つき）にする。省略/False のキーは対象外。各エンドポイントの `if knowledge:` 分岐内（response 作成前）で呼ぶ。
    `availability`（省略可）: 呼び出し元がターン先頭で 1 回だけ計算した snapshot。この判定と実行本体へ同じ snapshot を渡し、受付時と実行時で可用性の判定が食い違わないようにする。
    """
    bad = agentic_search.unavailable_explicit_tools(tools, availability=availability)
    if bad:
        raise HTTPException(
            422, f"検索経路 {', '.join(bad)} は現在利用できません（接続を確認してください）")


def _prepare_agentic_snapshot(uid: str, requested_knowledge: bool, web_search: bool):
    """`/chat/turns` の準備手順。
    ① ユーザ設定を一度だけ読み、knowledge の実効値（`_knowledge_for_settings`）と Provider 構築の両方へ同じスナップショットを渡す。
    ② 同一のスナップショットから Provider を一度だけ組み立てる。
    ③ `_agentic_target_check`（接続先の I/O-free allowlist 検証）→ `tool_availability`（ES/Neo4j への実接続チェック）の順で呼ぶ（不許可の接続先へ通信する前に拒否するため）。
    返り値 `(knowledge, provider, settings, sys_settings, tools_availability)`。`knowledge` は各エンドポイントの分岐へ、残り 3 つは実行本体（`_turn_run_fn`）へそのまま渡す。
    `store.get_settings` の失敗は捕捉せず伝播させる（500 で停止）。knowledge の実効値が False のときは接続先検証・可用性確認を行わず、`(False, plain_provider_for の結果, settings, sys_settings, None)` を返す。
    `_agentic_target_check` が `llm.PreflightRejected`（`SsrfBlocked` を含む）を送出した場合は捕捉し、固定文言の `HTTPException(422)` に変換する（例外の生文言は応答に含めない）。それ以外の例外は伝播して 500 のままにする。
    """
    settings = store.get_settings(uid)
    knowledge = _knowledge_for_settings(settings, requested_knowledge)
    if not knowledge:
        # 資料参照オフも受付時の設定の写しから頭脳（`PlainChatProvider`）を作り、実行本体へ渡す（実行時に設定を読み直さない）。
        sys_settings = store._read_system_settings_fresh()
        return False, plain_provider_for(settings, sys_settings), settings, sys_settings, None
    # `stream_message` と同じ上書き（実行時にも同じ値で冪等に上書きされる）。
    settings = {**settings, "codex_web_search": bool(web_search)}
    sys_settings = store._read_system_settings_fresh()
    provider = get_provider(settings, system_settings=sys_settings)
    try:
        provider._agentic_target_check()
    except llm.PreflightRejected:
        raise HTTPException(422, "資料参照の接続先が許可されていません（設定画面で確認してください）")
    tools_availability = agentic_search.tool_availability()
    return True, provider, settings, sys_settings, tools_availability


@chat_router.get("/chat/tools-availability", tags=["チャット"])
def chat_tools_availability(request: Request):
    """検索経路 3 種（grep／全文・ベクトル(ES)／グラフ）が実際に接続できるかを返す（ログイン必須・world/会話に依存しない）。"""
    _current_user(request)
    return agentic_search.tool_availability()


# チャットターンのバックグラウンド実行。送信するとサーバ側の background thread としてターンを起動し、HTTP 接続（SSE 購読）の有無と無関係に完走・DB 永続する。
# 設計: docs/design/chat.md「停止と同時実行」


def _persist_turn_crash(conversation_id: int, message: str, uid: str, world: str,
                        personal: bool, exc: Exception, *,
                        knowledge: bool = False, lens: str | None = None,
                        saved_user_id: int | None = None, saved_user_personal: bool | None = None,
                        recovered_result: dict | None = None) -> dict | None:
    """例外時に、回収済みの部分回答または本文なしのエラーを保存して返す。
    設計: docs/design/chat.md「仕上げと保存」
    """
    if recovered_result and recovered_result.get("saved_message"):
        return recovered_result["saved_message"]
    user_msg_id = None
    user_msg_personal = personal
    assistant_msg_id = None

    # personal ターンは user メッセージ保存の成否と無関係に会話フラグを立てる（冪等・fail-closed。受領共有は会話フラグだけでブロックしているため）。
    if personal:
        try:
            store.set_contains_personal_workspace(conversation_id)
        except Exception as flag_exc:
            _log.warning("turn crash personal flag set failed (best-effort): %s", flag_exc)

    # (a) このターンの user メッセージ ID を確定する。失敗したら (b)(c) は行わない（ID が無いまま assistant 行・監査を作ると別ターンへ誤って紐付けうるため・fail-closed）。
    try:
        if saved_user_id is not None:
            user_msg_id = saved_user_id
            user_msg_personal = bool(saved_user_personal)
        else:
            saved_user = store.add_message(conversation_id, "user", message, personal=personal)
            user_msg_id = saved_user["id"]
            user_msg_personal = personal
    except Exception as persist_exc:
        _log.warning("turn crash user message persistence failed (best-effort, "
                    "original error still re-raised): %s", persist_exc)
        return

    # ② 回収済みの回答またはエラーを保存する。
    try:
        headline = f"エラーが発生しました（{type(exc).__name__}）。もう一度お試しください。"
        crash_lens = lens if knowledge else "chat"
        env = {"lens": crash_lens, "headline": headline, "summary": {"total": 0}, "data": {}, "sources": [],
               "completion": "failed", "agentic_failure": "error"}
        answer_shape.seal(env)
        # provider.run() が通信例外で落ちた honest failure の経路。例外の型だけで timeout／transport_error を判別する（`_finalize` を経由しないため `stop_kind_mod.from_exception` を直接使う）。それ以外の例外型は `stop_kind` を立てず NULL のままにする。
        _crash_stop_kind = stop_kind_mod.from_exception(exc)
        if _crash_stop_kind:
            env["stop_kind"] = _crash_stop_kind
        trace = None
        record = None
        if recovered_result and recovered_result.get("result"):
            result = recovered_result["result"]
            env = dict(result["env"])
            notice = f"回答の処理または保存でエラーが発生しました（{type(exc).__name__}）。回収済みの回答を残しています。"
            answer_shape.add_notice(env, "recovered_error", notice)
            stopped = recovered_result.get("stopped")
            env["completion"] = "stopped" if stopped else "partial"
            env["stop_kind"] = "stopped_by_user" if stopped else (_crash_stop_kind or "codex_partial")
            for key in ("_terminal", "_personal_rounds", "_evidence_committed"):
                env.pop(key, None)
            crash_lens = result["decision"]["lens"]
            env["lens"] = crash_lens
            record = result.get("investigation_record")
            from sherpa.chat_service import _cap_trace_v2, _mark_investigation_recorded
            _mark_investigation_recorded(env, record)
            trace = _cap_trace_v2(recovered_result.get("trace") or {})
            user_msg_personal = bool(user_msg_personal or recovered_result.get("personal")
                                     or store.conversation_is_personal_tainted(conversation_id))
            if user_msg_personal:
                store.set_contains_personal_workspace(conversation_id)
                store.set_message_personal(user_msg_id)
            answer_shape.seal(env)
            headline = env["headline"]
        saved_assistant = store.add_message(conversation_id, "assistant", headline, lens=crash_lens,
                                            route=env.get("route"), trace=trace,
                                            answer=env, personal=user_msg_personal)
        saved_assistant = _save_investigation_record(record, saved_assistant["id"], conversation_id) or saved_assistant
        assistant_msg_id = saved_assistant["id"]
    except Exception as persist_exc:
        _log.warning("turn crash assistant message persistence failed (best-effort, "
                    "original error still re-raised): %s", persist_exc)

    # ③ 監査を保存する。
    try:
        store.audit(uid, "chat.turn", "conversation", f"conv:{conversation_id}",
                   detail={"lens": "error", "world": world, "error": type(exc).__name__,
                           "message_id_user": user_msg_id, "message_id_assistant": assistant_msg_id,
                           "personal": user_msg_personal},
                   outcome="error", severity="warning")
    except Exception as audit_exc:
        _log.warning("turn crash audit failed (best-effort): %s", audit_exc)

    return saved_assistant if assistant_msg_id is not None else None


def _turn_run_fn(message: str, world: str, uid: str,
                 scope_paths: list, knowledge: bool, personal: bool, layer: str = "both",
                 lens: str | None = None, web_search: bool = False,
                 depth_profile: str = "standard", tools: dict | None = None,
                 tools_explicit: list | None = None,
                 tools_availability: dict | None = None,
                 provider=None, settings: dict | None = None, sys_settings: dict | None = None):
    """バックグラウンド実行本体を作る（conversation_id 確定後に呼ばれるファクトリで、`chat_turns.start_turn` の `run_fn_factory` として渡す）。knowledge の有無で neo4j_session の要否が変わる。
    `web_search`・`depth_profile`・`tools`・`tools_explicit` は `ChatReq` の値をそのまま転送する。
    `tools_availability`・`provider`/`settings`/`sys_settings`（既定 `None`）: 受付時（`chat_turns_start`）に組み立てた同一のスナップショットをそのまま `stream_message` へ転送する（背景実行は応答後に時間が空きうるため、ここで再取得して受付時と判定が食い違わないようにする）。
    """
    def make_run(conversation_id: int):
        def run(stop_event: threading.Event, emit) -> None:
            # このターン自身が保存した user 行の id/personal を `stream_message` から直接受け取る（本文一致で推測しない）。
            saved_user: dict = {}
            recovered: dict = {}

            def _on_user_saved(message_id, is_personal):
                saved_user["id"] = message_id
                saved_user["personal"] = is_personal

            try:
                if not knowledge:
                    for evt in stream_message(None, message, world,
                                              conversation_id=conversation_id, knowledge=False,
                                              user_id=uid, personal=personal,
                                              users_dir=str(_USERS_DIR), stop_event=stop_event,
                                              on_user_saved=_on_user_saved, web_search=web_search,
                                              tools_availability=tools_availability,
                                              provider=provider, settings=settings, sys_settings=sys_settings,
                                              recovered_result=recovered):
                        emit(evt)
                        if evt.get("type") == "answer" and recovered:
                            recovered["delivered"] = True
                    return
                with neo4j_session() as s:
                    for evt in stream_message(s, message, world,
                                              conversation_id=conversation_id,
                                              scope_paths=scope_paths, layer=layer, lens=lens, knowledge=True,
                                              user_id=uid, personal=personal,
                                              users_dir=str(_USERS_DIR), stop_event=stop_event,
                                              on_user_saved=_on_user_saved, web_search=web_search,
                                              depth_profile=depth_profile, tools=tools,
                                              tools_explicit=tools_explicit,
                                              tools_availability=tools_availability,
                                              provider=provider, settings=settings, sys_settings=sys_settings,
                                              recovered_result=recovered):
                        emit(evt)
                        if evt.get("type") == "answer" and recovered:
                            recovered["delivered"] = True
            except Exception as e:
                # `neo4j_session()`/`stream_message` 自体が例外を投げて DB に何も残らないことがあるため、`on_user_saved` が発火済みなら `_persist_turn_crash` にその id を再利用させ、未発火なら新規に保存する（best-effort で永続してから re-raise する）。
                partial = _persist_turn_crash(conversation_id, message, uid, world, personal, e,
                                    knowledge=knowledge, lens=lens,
                                    saved_user_id=saved_user.get("id"),
                                    saved_user_personal=saved_user.get("personal"),
                                    recovered_result=recovered)
                if partial is not None and recovered and not recovered.get("delivered"):
                    emit({"type": "answer", "conversation_id": conversation_id, "message": partial})
                raise
        return run
    return make_run


@chat_router.post("/chat/turns", tags=["チャット"], response_model=ChatTurnStartResponse)
def chat_turns_start(req: ChatReq, request: Request):
    """チャットターンをバックグラウンドで開始する（画面遷移しても止まらない）。
    返り値 `{turn_id, conversation_id}` の `turn_id` で `GET /chat/turns/{turn_id}/stream` を購読する（途中からでも続きから追従できる）。同時実行数の上限（既定は 1 ユーザー 2・全体 8・管理画面の「同時実行の上限」で変更可）を超えると 429 を返し、そのとき会話は作られない。
    """
    u = _current_user(request)
    _check_chat_write(u, req.conversation_id)
    uid = u["uid"]
    w = _resolve_world(req.world)
    # settings を一度だけ読み、knowledge の実効値と Provider を同じスナップショットから準備する。接続先検証→可用性チェックの順で行い、受付（422判定）と背景実行本体（`_turn_run_fn`）へ同じ Provider/settings/snapshot を渡す。
    knowledge, provider, settings, sys_settings, tools_availability = _prepare_agentic_snapshot(
        uid, req.knowledge, req.web_search)
    if knowledge:
        validated_scope(w, req.scope_paths)  # 実在 world のみ＋scope 検証（開始前に弾く）。
        _validate_tools_availability(req.tools, availability=tools_availability)  # 明示ON指定の不達ツールは 422（ツール名つき）。

    def _make_conversation() -> int:
        # `chat_turns.start_turn` が枠を予約した後・lock の外で呼ぶ（DB I/O をロック保持中に行わない）。会話タイトルはスラッシュ接頭辞を除去した後の本文から作る。
        _, title_message = _extract_slash_lens(req.message)
        return _ensure_conversation(req.conversation_id, title_message, w, uid)

    run_fn_factory = _turn_run_fn(req.message, w, uid, req.scope_paths, knowledge, req.personal,
                                  layer=req.layer, lens=req.lens, web_search=req.web_search,
                                  depth_profile=req.depth_profile, tools=req.tools,
                                  tools_explicit=req.tools_explicit,
                                  tools_availability=tools_availability,
                                  provider=provider, settings=settings, sys_settings=sys_settings)
    try:
        # `known_conversation_id`: リクエストが既存会話への継続（`req.conversation_id` 明示）のときだけ渡す。新規会話（None）は会話単位の排他判定の対象外。
        rec = chat_turns.start_turn(uid=uid, conversation_factory=_make_conversation,
                                    run_fn_factory=run_fn_factory,
                                    known_conversation_id=req.conversation_id)
    except chat_turns.TurnLimitError as e:
        if e.scope == "conversation":
            raise HTTPException(429, "この会話の別の回答を実行中です。終わってからもう一度お試しください。")
        raise HTTPException(429, "実行中の回答が終わってからもう一度お試しください。")
    return {"turn_id": rec.turn_id, "conversation_id": rec.conversation_id}


@chat_router.get("/chat/turns/{turn_id}/stream", tags=["チャット"])
def chat_turns_stream(turn_id: str, request: Request, cursor: int = Query(0, ge=0)):
    """ターンの思考イベントを cursor から replay→追従する SSE。切断は購読解除にすぎず、ターンはサーバ側で続く。完了済みターンへの購読は残イベントを replay して終了する。所有者以外・存在しない turn_id は 404（存在有無を教えない）。"""
    u = _current_user(request)
    gen = chat_turns.iter_sse(turn_id, u["uid"], cursor)
    if gen is None:
        raise HTTPException(404, "ターンが見つかりません")
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@chat_router.get("/chat/turns/running", tags=["チャット"], response_model=ChatTurnsRunningResponse)
def chat_turns_running(request: Request, all: bool = Query(False)):
    """現在ユーザーが実行中（未完了）のターン一覧。トップバーの「回答作成中」表示と、会話を開いたときの自動再購読に使う。
    `all=true` は管理者専用（それ以外は 403）で、全員分を `uid` 付きで返す（既定は本人分のみ・`uid` は null）。固まったターンを管理者が `/chat/turns/{turn_id}/stop` で解放するために使う。
    """
    u = _current_user(request)
    if all and u.get("role") != "admin":
        raise HTTPException(403, "管理者のみ")
    recs = chat_turns.list_running(u["uid"], all_users=all)
    return {"turns": [{"turn_id": r.turn_id, "conversation_id": r.conversation_id,
                       "started_at": r.started_at.isoformat(), **({"uid": r.uid} if all else {})}
                      for r in recs]}


@chat_router.post("/chat/turns/{turn_id}/stop", tags=["チャット"], response_model=ChatTurnStopResponse)
def chat_turns_stop(turn_id: str, request: Request):
    """実行中ターンを停止する（本人のターン・管理者は全員のターン）。停止までに頭脳が停止の終端（途中までの回答）を返していればその assistant メッセージを保存し、返していなければ保存しない。どちらも監査には `chat.turn` として `stopped: true` 付きで記録する。存在しない/他人/完了済みはすべて `{"ok": false}`。"""
    u = _current_user(request)
    return {"ok": chat_turns.stop_turn(turn_id, u["uid"], is_admin=u.get("role") == "admin")}


# 回答ごとの利用者フィードバック。

# 本文サイズ上限（バイト）。FastAPI の自動 `Body()` パース（本文全体をバッファしてから検証する）より前に、チャンク読みで打ち切る（`routers/audit_usage.py::_read_capped_json_body` と同じ）。
_FEEDBACK_BODY_MAX_BYTES = 65_536  # 64KiB。
_FEEDBACK_TAGS_MAX = 4
_FEEDBACK_BODY_PARSE_ERROR_MSG = "リクエスト本文が解析できません（UTF-8 の JSON オブジェクトのみ受理します）"


async def _read_capped_feedback_body(request: Request) -> dict:
    """本文をチャンク読みで `_FEEDBACK_BODY_MAX_BYTES` まで読み、UTF-8 の JSON オブジェクトとして解析する。"""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _FEEDBACK_BODY_MAX_BYTES:
            raise HTTPException(
                413, f"リクエスト本文が上限（{_FEEDBACK_BODY_MAX_BYTES // 1024}KiB）を超えています")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        text = raw.decode("utf-8")
        data = json.loads(text) if text else {}
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise HTTPException(400, _FEEDBACK_BODY_PARSE_ERROR_MSG)
    if not isinstance(data, dict):
        raise HTTPException(400, _FEEDBACK_BODY_PARSE_ERROR_MSG)
    return data


_FEEDBACK_REQUEST_BODY_SCHEMA = {
    "type": "object",
    "required": ["rating"],
    "properties": {
        "rating": {"type": "string", "enum": ["up", "down"]},
        "tags": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": list(store.MESSAGE_FEEDBACK_TAGS)},
            "maxItems": _FEEDBACK_TAGS_MAX,
            "description": "定型タグ。重複は自動的にまとめる。省略/null は空配列扱い。",
        },
        "comment": {
            "type": ["string", "null"],
            "description": (f"一言（任意）。前後の空白を除いて"
                            f"{store.MESSAGE_FEEDBACK_COMMENT_MAX_LEN}字を超える場合は拒否する。"),
        },
    },
}


@chat_router.post(
    "/chat/{conversation_id}/messages/{message_id}/feedback", tags=["チャット"],
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
        "schema": _FEEDBACK_REQUEST_BODY_SCHEMA}}}},
    responses={
        400: {"description": "リクエスト本文が解析できません"},
        403: {"description": "共有された会話にはフィードバックを送信できません"},
        404: {"description": "会話またはメッセージが見つかりません"},
        413: {"description": "リクエスト本文がサイズ上限を超えています"},
        422: {"description": "rating・タグ・一言のいずれかが不正です（送信値は反射しません）"},
    },
)
async def chat_message_feedback(conversation_id: int, message_id: int, request: Request):
    """回答ごとの利用者フィードバック（👍/👎＋定型タグ＋任意の一言）を投稿する。会話の所有者のみ投稿できる（共有された会話の閲覧者は 403）。同じ利用者が同じメッセージへ再送すると上書きする（1 利用者×1 メッセージにつき最新 1 件）。タグは重複をまとめたうえで最大 4 件。入力不正の 422 は固定文言のみで、送信値は反射しない。
    """
    u = await run_in_threadpool(_current_user, request)

    body = await _read_capped_feedback_body(request)
    rating = body.get("rating")
    if rating not in ("up", "down"):
        raise HTTPException(422, "rating は up/down のいずれかで指定してください")
    tags_in = body.get("tags")
    if tags_in is None:
        tags_in = []
    if not isinstance(tags_in, list) or not all(isinstance(t, str) for t in tags_in):
        raise HTTPException(422, "タグは文字列の配列で指定してください")
    if any(t not in store.MESSAGE_FEEDBACK_TAGS for t in tags_in):
        raise HTTPException(422, "タグが不正です")
    tags = sorted(dict.fromkeys(tags_in))  # 重複をまとめる（保存・集計とも一意にする）。
    if len(tags) > _FEEDBACK_TAGS_MAX:
        raise HTTPException(422, f"タグは{_FEEDBACK_TAGS_MAX}件以内にしてください")
    comment_in = body.get("comment")
    if comment_in is not None and not isinstance(comment_in, str):
        raise HTTPException(422, "一言は文字列で指定してください")
    comment = (comment_in or "").strip() or None
    if comment and len(comment) > store.MESSAGE_FEEDBACK_COMMENT_MAX_LEN:
        raise HTTPException(422, f"一言は{store.MESSAGE_FEEDBACK_COMMENT_MAX_LEN}文字以内にしてください")

    def _persist() -> dict:
        if not store.owns_conversation(u["uid"], conversation_id):
            conv = store.get_conversation_for_read(u["uid"], conversation_id)
            if conv and conv["conversation"].get("origin") == "received_share":
                raise HTTPException(403, "共有された会話にはフィードバックを送信できません")
            raise HTTPException(404, "会話が見つかりません")
        if not store.owns_assistant_message(u["uid"], conversation_id, message_id):
            raise HTTPException(404, "メッセージが見つかりません")
        return store.upsert_message_feedback(message_id, u["uid"], rating, tags, comment)

    fb = await run_in_threadpool(_persist)
    return {"ok": True, "message_id": message_id, "rating": fb["rating"],
           "tags": fb["tags"], "comment": fb["comment"]}
