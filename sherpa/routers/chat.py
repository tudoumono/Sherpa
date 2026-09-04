"""チャット系エンドポイント（フェーズ3スライス7・純移動）。

`chat_router`（`GET /chat/tools-availability`・`POST /chat`・`GET /chat/stream`・
`POST /chat/stream/stop`・`POST /chat/turns`・`GET /chat/turns/{turn_id}/stream`・
`GET /chat/turns/running`・`POST /chat/turns/{turn_id}/stop`）の8ルート。golden の定義順で
連続しているため router は1本で足りる。api.py 側は
`app.include_router(chat.chat_router)` を旧位置（`app.include_router(impact.impact_router)` の
直後・`app.include_router(system.settings_router)` の直前）に置く。

途中停止レジストリ `_STREAM_STOP_LOCK`/`_STREAM_STOP_EVENTS`/`_STREAM_ID_PATTERN`、および
背景実行（覗き窓方式）ヘルパ `_persist_turn_crash`/`_turn_run_fn`（docs/proposals/
2026-07-03-チャット背景実行.md 正典）もこのモジュールへ純移動する。api.py 側は
`from sherpa.routers.chat import ChatReq, _STREAM_STOP_EVENTS, _STREAM_STOP_LOCK, _persist_turn_crash`
で再エクスポートし、`tests/api/test_chat_m8.py` の `api._STREAM_STOP_EVENTS[...]` / `test_chat_turns.py`
の `api._persist_turn_crash(...)` は同一オブジェクトの参照として互換のまま動く。ロジックは変更しない
（コード移動のみ）。

このモジュールは `sherpa.api` を import しない（循環回避）。
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

from sherpa import agent_constructs, agentic_search, chat_turns, llm, store
from sherpa import tools_pref as tools_pref_mod
from sherpa.agents import get_provider
from sherpa.chat_router import extract_slash_lens as _extract_slash_lens
from sherpa.chat_service import _ensure_conversation, handle_message, stream_message
from sherpa.deps import _USERS_DIR, _WORLD_PATTERN, _WorldField, _current_user, _resolve_world, neo4j_session, validated_scope
from sherpa.schemas import ChatTurnsRunningResponse, ChatTurnStartResponse, ChatTurnStopResponse

_log = logging.getLogger("sherpa")

# router に tags を持たせない: 各エンドポイントの `tags=["チャット"]` と結合されて二重化してしまう
# （ルート表 golden 不一致の原因）ため、tags 指定は各デコレータ側のみに残す（system.py:42-44 と同じパターン）。
chat_router = APIRouter()


class ChatReq(BaseModel):
    message: str
    world: str | None = _WorldField
    conversation_id: int | None = None
    knowledge: bool = False
    scope_paths: list[str] = Field(default_factory=list)
    # 探す対象（調べ方ブロック §3.4）。既定 both＝フィルタなし（既存挙動と完全同一）。
    layer: Literal["docs", "code", "both"] = "both"
    # 調べ方の明示指定（調べ方ブロック §3.1）。既定 None（省略）＝自動（既存の Tier1〜3 判定）。
    # 正典の値は4値＋省略のみ（RV1 #12）："auto" は非正典の互換値のため受理しない（UI も既に省略）。
    lens: Literal["impact", "troubleshoot", "qa", "author"] | None = None
    # 調べる深さ（調べ方ブロック §3.2・SC-6c）。既定 "standard"＝既存の挙動（env 既定値）と完全同一。
    depth_profile: Literal["standard", "deep", "max"] = "standard"
    personal: bool = False    # Feature B: 個人ファイル参照トグル（既定OFF）
    # WEB-1: Codex の Web 検索をこのチャットで希望するか（既定OFF）。管理者許可・
    # 頭脳が Codex（Azure 等でないこと）が揃わなければ、サーバ側で常に無効化される
    # （`sherpa/providers/codex/sandbox.py::_web_search_disabled_value` が唯一の判定点）。
    web_search: bool = False
    # 検索経路トグル（調べ方ブロック §3.6・SC-6e）。既定/省略/null は全 ON＝既存挙動と完全同一。
    # grep/fulltext（ES・全文＋ベクトル）/graph の3経路のみが対象（list_docs/read_around/ask_user
    # は常時ON）。3つとも false は 422。
    # SC-6e: キーを `Literal["grep","fulltext","graph"]`・値を `StrictBool` にする——
    # 素の `dict[str, bool]` は pydantic の型強制（coercion）がバリデータより先に走り、
    # `"false"`/`0`/`"yes"` のような非 bool 値を静かに bool へ変換してしまう
    # （`tools_pref.normalize_tools_pref` の「bool 以外は不正」という契約と食い違う）。
    # 未知キーも型自体で 422 になる（`normalize_tools_pref` の未知キー検査と二重になるが、
    # GET /chat/stream 側の手組み dict はこの型強制を経ないため、そちらの検査は引き続き必要）。
    tools: dict[Literal["grep", "fulltext", "graph"], StrictBool] | None = None

    @field_validator("tools")
    @classmethod
    def _v_tools(cls, v):
        # SC-6e: 欠落キーを埋めずに生の dict をそのまま保持する（`normalize_tools_pref` は
        # 構造検証（未知キー・非bool・3つとも false）だけに使い、戻り値は捨てる）——欠落キーを
        # 埋めてしまうと「明示的に true と指定したか」が失われ、後段の可用性 422 判定
        # （`unavailable_explicit_tools`）が省略キーまで誤検知してしまう。
        if v is not None:
            tools_pref_mod.normalize_tools_pref(v)
        return v


def _knowledge_for_settings(settings: dict, requested: bool) -> bool:
    """`_knowledge_for`/`_prepare_agentic_snapshot` が共有する判定本体（settings は呼び出し側が
    既に読んだものを渡す——ここでは読み直さない）。

    Codex 構成は**常に資料参照ON**（決定 2026-08-15）。Codex CLI は read-only 実行でも自分で
    grep/ファイル参照ができるため、「参照オフのつもりなのに KB を覗く」状態を作らない
    （旧実装は参照オフのとき定型文で断っていた＝別頭脳へ誘導していた）。
    画面もCodex選択中はトグルをON固定・操作不可にするが、UIを信頼せずここでも強制する（多層防御）。
    """
    if agent_constructs.construct_id(settings).startswith("codex"):
        return True
    return bool(requested)


def _knowledge_for(uid: str, requested: bool) -> bool:
    """このユーザーの実行構成で実際に使う `knowledge`（資料参照）の値（単体呼び出し用・自分で
    settings を読む）。実HTTP入口（`/chat`・`/chat/stream`・`/chat/turns`）はもう本関数を呼ばない
    ——`_prepare_agentic_snapshot` が同じ判定（`_knowledge_for_settings`）を、Provider 構築と
    同じ1回の settings 読み取りから行う（別々に読むと、その間に admin が構成を変えた場合、
    knowledge 判定と実際に構築される Provider の種類が食い違いうるため）。
    """
    try:
        settings = store.get_settings(uid)
    except Exception:
        return bool(requested)                       # 設定を読めない時は要求どおり（可用性優先）
    return _knowledge_for_settings(settings, requested)


def _check_chat_write(user: dict, conversation_id: int | None) -> None:
    """チャット書き込み権限チェック。受領共有への追記・他人会話へのアクセスを 403/404 で拒否。"""
    if conversation_id is None:
        return   # 新規会話は常に許可
    # まず読める会話か確認（他人の ID 直アクセスは None）。
    conv_data = store.get_conversation_for_read(user["uid"], conversation_id)
    if not conv_data:
        # 存在するが別人の会話 or 削除済み。
        raise HTTPException(404, "会話が見つかりません")
    if conv_data.get("share_status") == "unavailable":
        raise HTTPException(403, "この共有は利用できません（期限切れ・取消済み）")
    # received_share への追記は拒否。
    if conv_data["conversation"].get("origin") == "received_share":
        raise HTTPException(403, "共有された会話への追記はできません（読み取り専用）")
    # own でも所有者以外は書き込み不可。
    if not store.owns_conversation(user["uid"], conversation_id):
        raise HTTPException(403, "この会話への書き込み権限がありません")


def _validate_tools_availability(tools: dict | None, availability: dict | None = None) -> None:
    """検索経路トグルで明示的に ON 指定したツールが実接続で到達不可なら 422（ツール名つき）。

    省略/False のキーは対象外（可用分だけを黙って使う既存契約のまま・SC-6e）。
    `validated_scope` と同じ「response 作成前に弾く」位置（各エンドポイントの `if knowledge:`
    分岐内）で呼ぶ。

    `availability`（省略可・既定 `None`）: 呼び出し元（各エンドポイント）がターン先頭で1回だけ
    計算した snapshot。この422判定と実行本体（`handle_message`/`stream_message`/背景ターン）が
    別々に可用性を再取得すると、TTLキャッシュの境界を挟んで受付時と実行時の判定が食い違い、
    明示 `graph:true` が422を素通りした直後にグラフが不達として黙って無効化される窓ができる
    ——各エンドポイントは本関数へも `Ctx.tools_availability` へも同じ snapshot を渡す。
    """
    bad = agentic_search.unavailable_explicit_tools(tools, availability=availability)
    if bad:
        raise HTTPException(
            422, f"検索経路 {', '.join(bad)} は現在利用できません（接続を確認してください）")


def _prepare_agentic_snapshot(uid: str, requested_knowledge: bool, web_search: bool):
    """3つの実HTTP入口（`/chat`・`/chat/stream`・`/chat/turns`）が共有する準備手順（SC-6e）。

    ユーザ設定を一度だけ読み、knowledge の実効値（`_knowledge_for_settings`・Codex構成は常時ON）
    と Provider 構築の両方へ**同じスナップショット**を渡す——呼び出し元が別途 `_knowledge_for`
    などで独立に settings を読み直すと、その間に admin が構成を変えた場合（例: codex→openai）
    knowledge 判定と実際に構築される Provider の種類が食い違いうる（Codex常時ON契約が壊れる／
    逆に非Codexなのに knowledge=True のまま実行される）。

    同一のユーザ設定／システム設定スナップショットから Provider を一度だけ組み立て、
    `_agentic_target_check`（接続先の I/O-free allowlist 検証）→`tool_availability`
    （ES/Neo4j への実接続チェック）の順で呼ぶ——この順序を守らないと、不許可の接続先
    （例: 非allowlist Ollama URL）を拒否する前に ES/Neo4j への通信が発生してしまう
    （`providers/base.py::Provider._agentic_target_check` docstring 参照）。

    返り値 `(knowledge, provider, settings, sys_settings, tools_availability)`。`knowledge`
    は各エンドポイントの分岐へ、残り3つは実行本体（`handle_message`/`stream_message`/
    `_turn_run_fn`）へそのまま渡す契約——受付時422判定と実行本体とで別々に settings/Provider を
    組み立てると、その間に admin 保存が挟まった場合に新旧混在の接続先/鍵で動きうる
    （`handle_message`/`stream_message` の `provider`/`settings`/`sys_settings` 引数 docstring
    参照）。

    settings の読み取り（`store.get_settings`）は捕捉せずそのまま伝播させる（500 で停止）——
    ここで例外を飲んで `(bool(requested_knowledge), None, None, None, None)` のような楽観的な
    値へフォールバックすると、`knowledge=True` の受付が読み取り失敗後も継続し、実行本体
    （`handle_message`/`stream_message`/背景 worker）が `settings=None` を受け取って**自分で
    settings を再読取**することになる。この2回目の読み取りは受付から時間が経った後（`/chat/turns`
    はターン受理後の背景スレッド・`/chat/stream` は SSE 開始後・`/chat` も会話/user行保存後）
    に起こるため、単一スナップショット契約（本関数が1回だけ読んだ値を実行本体までそのまま
    渡す契約）と `_agentic_target_check → tool_availability` の順序保証の両方を迂回してしまう。
    knowledge の実効値が `False` のときは Provider を準備せず `(False, None, None, None, None)`
    を返す（knowledge オフはナレッジ参照系の I/O・接続先検証を一切しないため対象外）。

    `_agentic_target_check` が `llm.PreflightRejected`（`SsrfBlocked` を含む＝接続先が許可
    ポリシーを満たさない）を送出した場合は、ここで捕捉し安全な固定文言つきの `HTTPException(422)`
    へ変換する——未捕捉のまま伝播させると FastAPI の既定エラーハンドラが `500
    text/plain "Internal Server Error"` にしてしまい、画面は「応答の形式が不正です」としか
    表示できない（`web/common.js` 参照）。例外の生文言は応答に含めない（`_redact_url_for_error`
    等で既に伏せられているとはいえ、HTTP 応答へ生の例外文言をそのまま載せない多層防御）。
    それ以外の未知例外（settings 読み取り失敗を含む）はここでは捕捉せず、そのまま伝播させて
    500 のままにする（想定外の失敗を誤って「設定の問題」と偽装しない）。
    """
    settings = store.get_settings(uid)
    knowledge = _knowledge_for_settings(settings, requested_knowledge)
    if not knowledge:
        return False, None, None, None, None
    # WEB-1: `handle_message`/`stream_message` と同じ上書き（実行時にもう一度同じ値で
    # 冪等に上書きされる）。
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
    """検索経路3種（grep／全文・ベクトル(ES)／グラフ）の実接続可用性（SC-6e）。

    調べ方ブロックの「詳細」チップが、到達不可なツールを表示・選択させないために使う唯一の
    真実源——実行側（デフォルトツール構築・非agentic `_dispatch`/`_gather`）と同じ
    `agentic_search.tool_availability()` を返すだけ（ログイン必須・世界／会話には依存しない）。
    """
    _current_user(request)
    return agentic_search.tool_availability()


@chat_router.post("/chat", tags=["チャット"])
def chat(req: ChatReq, request: Request):
    """同期チャット。knowledge=true でナレッジグラフ／検索を使う回答、false は素の対話。"""
    u = _current_user(request)
    _check_chat_write(u, req.conversation_id)
    uid = u["uid"]
    w = _resolve_world(req.world)
    # SC-6e: settings を一度だけ読み、knowledge の実効値（Codex構成は常にON・多層防御）と
    # Provider を同じスナップショットから準備する。接続先検証（I/O-free）→可用性チェック
    # （ES/Neo4j）の順で行い、受付（422判定）と実行本体（handle_message）へ同じ
    # Provider/settings/snapshot を渡す——別々に取得すると knowledge 判定・可用性判定の
    # いずれも admin 更新を挟んで食い違い得る。
    knowledge, provider, settings, sys_settings, tools_availability = _prepare_agentic_snapshot(
        uid, req.knowledge, req.web_search)
    if not knowledge:
        return handle_message(None, req.message, w,
                              conversation_id=req.conversation_id, knowledge=False,
                              user_id=uid, personal=req.personal,
                              users_dir=str(_USERS_DIR), web_search=req.web_search,
                              tools_availability=tools_availability,
                              provider=provider, settings=settings, sys_settings=sys_settings)
    validated_scope(w, req.scope_paths)            # ナレッジ参照は実在 world のみ＋scope 検証（正規化は handle_message 側）
    _validate_tools_availability(req.tools, availability=tools_availability)   # SC-6e: 明示ON指定の不達ツールは422（ツール名つき）
    with neo4j_session() as s:
        return handle_message(s, req.message, w,
                              conversation_id=req.conversation_id,
                              scope_paths=req.scope_paths, layer=req.layer, lens=req.lens, knowledge=True,
                              user_id=uid, personal=req.personal,
                              users_dir=str(_USERS_DIR), web_search=req.web_search,
                              depth_profile=req.depth_profile, tools=req.tools,
                              tools_availability=tools_availability,
                              provider=provider, settings=settings, sys_settings=sys_settings)


# UI フィードバック1（途中停止・2026-07-03）: EventSource.close() はクライアント側の接続を閉じるだけで、
# サーバ側の StreamingResponse（sync generator）は次のチャンク送信を試みるまで切断に気づけない
# （starlette は ASGI spec>=2.4 では disconnect を能動的に監視しない・iterate_in_threadpool は generator の
# next() をスレッドプールへ都度投げるだけで、ブロッキング呼び出し中はキャンセルできない＝調査済）。
# 特に CodexProvider はサブプロセスの stdout をブロッキング read しているため、フラグを立てるだけでは
# 次の行が来るまで反応できない。そこで stream_id で per-stream の threading.Event を登録し、
# `/chat/stream/stop` から明示的に set する（CodexProvider 側は set を検知して即 _killpg・詳細は agents.py）。
_STREAM_STOP_LOCK = threading.Lock()
_STREAM_STOP_EVENTS: dict[str, tuple[str, threading.Event]] = {}   # stream_id -> (uid, Event)
# RV MEDIUM（2026-07-03再検証）: stream_id はクライアント生成の相関IDのため、UUID相当（十分なエントロピー・
# ログ/URLに安全な文字集合）に形式を制約する（無制限文字列を受理しない）。crypto.randomUUID() 由来（36桁）と、
# それが使えない古いブラウザ向けの chat.js フォールバック（`${Date.now()}-${random.toString(36)}`）の両方を通す。
_STREAM_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$"


@chat_router.get("/chat/stream", tags=["チャット"])
def chat_stream(request: Request, message: str = Query(...),
                world: str | None = Query(None, pattern=_WORLD_PATTERN),
                conversation_id: int | None = None, knowledge: bool = False,
                personal: bool = False,                       # Feature B: 個人ファイル参照トグル
                scope_paths: list[str] = Query(default_factory=list),
                # 探す対象（調べ方ブロック §3.4）。既定 both＝フィルタなし（既存挙動と完全同一）。
                layer: Literal["docs", "code", "both"] = "both",
                # 調べ方の明示指定（調べ方ブロック §3.1）。既定 None（省略）＝自動（RV1 #12・"auto" は非受理）。
                lens: Literal["impact", "troubleshoot", "qa", "author"] | None = Query(None),
                # 調べる深さ（調べ方ブロック §3.2・SC-6c）。既定 "standard"＝既存の挙動と完全同一。
                depth_profile: Literal["standard", "deep", "max"] = "standard",
                # WEB-1: Codex の Web 検索をこのチャットで希望するか（既定OFF・`ChatReq.web_search` と同じ契約）。
                web_search: bool = False,
                # 検索経路トグル（調べ方ブロック §3.6・SC-6e）。`ChatReq.tools` の各キーを個別 query
                # param に分解したもの（GET はネスト構造を持てないため）。既定 None＝省略（全ON）。
                # `bool = True` ではなく `bool | None = None` にする（SC-6e）: 「省略」と
                # 「明示的に true」を区別できないと、可用性 422 判定（`unavailable_explicit_tools`）が
                # 省略キーまで誤って対象にしてしまう（`ChatReq.tools` の生 dict 保持と同じ理由）。
                tools_grep: bool | None = None, tools_fulltext: bool | None = None,
                tools_graph: bool | None = None,
                # UI フィードバック1: 途中停止用の相関ID（クライアント生成・UUID相当に形式制約＝RV MEDIUM）
                stream_id: str | None = Query(None, pattern=_STREAM_ID_PATTERN)):
    """チャットの SSE ストリーミング版（`/chat` と同じ意味論・逐次イベントで返す）。"""
    u = _current_user(request)
    _check_chat_write(u, conversation_id)
    w = _resolve_world(world)
    uid = u["uid"]
    # 明示指定されたキーだけを残す（欠落キーを埋めない・`ChatReq.tools` と同じ生 dict 契約）。
    tools_raw = {k: v for k, v in
                {"grep": tools_grep, "fulltext": tools_fulltext, "graph": tools_graph}.items()
                if v is not None} or None
    try:
        tools_pref_mod.normalize_tools_pref(tools_raw)   # 構造検証のみ（3つとも false 等）・戻り値は使わない
    except ValueError as e:
        raise HTTPException(422, str(e))
    # SC-6e: settings を一度だけ読み、knowledge の実効値（Codex構成は常にON・多層防御）と
    # Provider を同じスナップショットから準備する。接続先検証（I/O-free）→可用性チェック
    # （ES/Neo4j）の順で行い、受付（422判定）と実行本体（stream_message・SSE closure）へ同じ
    # Provider/settings/snapshot を渡す——別々に取得すると knowledge 判定・可用性判定の
    # いずれも admin 更新を挟んで食い違い得る。
    knowledge, provider, settings, sys_settings, tools_availability = _prepare_agentic_snapshot(
        uid, knowledge, web_search)
    if knowledge:
        validated_scope(w, scope_paths)               # 実在 world のみ＋scope 検証（response 作成前に弾く）
        _validate_tools_availability(tools_raw, availability=tools_availability)   # SC-6e: 明示ON指定の不達ツールは422（ツール名つき）
    stop_event = None
    if stream_id:
        stop_event = threading.Event()
        with _STREAM_STOP_LOCK:
            # RV MEDIUM（2026-07-03再検証）: 同じ stream_id が既に使用中なら後勝ちで上書きせず拒否する
            # （上書きすると、先勝ちストリームの Event 参照が失われ、`/chat/stream/stop` から止められなく
            # なる＝停止不能バグ）。クライアント生成の相関IDが衝突するのは通常起こらないはずなので
            # 409 は素直な異常系応答（クライアントは新しい stream_id で再試行すればよい）。
            if stream_id in _STREAM_STOP_EVENTS:
                raise HTTPException(409, "この stream_id は既に使用中です")
            _STREAM_STOP_EVENTS[stream_id] = (uid, stop_event)

    def gen():
        try:
            if not knowledge:
                for evt in stream_message(None, message, w,
                                          conversation_id=conversation_id, knowledge=False,
                                          user_id=uid, personal=personal,
                                          users_dir=str(_USERS_DIR), stop_event=stop_event,
                                          web_search=web_search, tools_availability=tools_availability,
                                          provider=provider, settings=settings, sys_settings=sys_settings):
                    yield f"data: {json.dumps(evt, ensure_ascii=False, default=str)}\n\n"
                return
            with neo4j_session() as s:                       # session は generator の内側で（streaming 中は開いたまま）
                for evt in stream_message(s, message, w,
                                          conversation_id=conversation_id,
                                          scope_paths=scope_paths, layer=layer, lens=lens, knowledge=True,
                                          user_id=uid, personal=personal,
                                          users_dir=str(_USERS_DIR), stop_event=stop_event,
                                          web_search=web_search, depth_profile=depth_profile,
                                          tools=tools_raw, tools_availability=tools_availability,
                                          provider=provider, settings=settings, sys_settings=sys_settings):
                    yield f"data: {json.dumps(evt, ensure_ascii=False, default=str)}\n\n"
        finally:
            if stream_id:
                with _STREAM_STOP_LOCK:
                    # RV MEDIUM（2026-07-03再検証）: 登録されている Event が「自分がここで作った Event と
                    # 同一オブジェクト」の場合のみ pop する（`is` で同一性判定）。上の重複拒否で通常は
                    # あり得ないが、念のための多層防御＝万一何らかの経路で再登録が起きていても、
                    # 無条件 pop で「他人（後発）の登録」を巻き添えに消して停止不能にする事故を防ぐ。
                    entry = _STREAM_STOP_EVENTS.get(stream_id)
                    if entry is not None and entry[1] is stop_event:
                        _STREAM_STOP_EVENTS.pop(stream_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ChatStreamStopReq(BaseModel):
    stream_id: str = Field(pattern=_STREAM_ID_PATTERN)


@chat_router.post("/chat/stream/stop", tags=["チャット"])
def chat_stream_stop(req: ChatStreamStopReq, request: Request):
    """UI フィードバック1: ストリーミング中のチャットを途中停止する（本人のストリームのみ）。

    対応する `stream_id` が見つからない/既に完了している/他人のストリームの場合も `{"ok": false}` を
    返すだけでエラーにしない（クリック競合・二重送信・タイミングのずれで普通に起こり得るため）。
    存在有無を教えないことで他人のストリームの探索にも使えないようにする。
    """
    u = _current_user(request)
    with _STREAM_STOP_LOCK:
        entry = _STREAM_STOP_EVENTS.get(req.stream_id)
    if entry is None:
        return {"ok": False}
    owner_uid, event = entry
    if owner_uid != u["uid"]:
        return {"ok": False}
    event.set()
    return {"ok": True}


# ===== チャットターンのバックグラウンド実行（覗き窓方式・docs/proposals/2026-07-03-チャット背景実行.md 正典）=====
# 送信＝サーバ側 background thread としてターンを起動し、HTTP 接続（SSE 購読）の有無と無関係に必ず
# 完走・DB永続する。`/chat`・`/chat/stream`（＋ `/chat/stream/stop`）は後方互換のため残置（フロントは
# こちらの新方式へ全面移行するが、API としての退役はしない＝挙動もテストも変更しない）。


def _persist_turn_crash(conversation_id: int, message: str, uid: str, world: str,
                        personal: bool, exc: Exception, *,
                        saved_user_id: int | None = None, saved_user_personal: bool | None = None) -> None:
    """background thread が `stream_message` へ辿り着く前/途中で例外を投げると、
    `POST /chat/turns` の応答で返した conversation_id なのに会話が空のまま（user メッセージすら
    無い）になり得る。best-effort で (a)(b)(c) を行う。ここでの失敗は握り潰す（呼び出し側＝
    `_turn_run_fn.run()` が元の例外を re-raise するため、chat_turns 側の error イベント＋
    mark_done による枠解放は従来どおり動く）。

    assistant 側のエラー応答は user 行の実際の personal 値を継承する（固定で False にすると、
    個人参照ターンのクラッシュ時だけ改善ログ等の後段処理が個人情報を非個人として扱ってしまう）。
    chat.turn 監査にも message_id_user/message_id_assistant を残す（無いと改善ログ抽出が
    「対応付け不明」として fail-closed 除外してしまい、正常なターンまで欠落する）。

    `saved_user_id`/`saved_user_personal`（呼び出し元＝`_turn_run_fn.make_run` が
    `stream_message(..., on_user_saved=...)` のコールバックでこのターン専用に直接受け取った値）が
    あれば最優先でそのまま使う。`on_user_saved` は user 行保存の直後に同期で呼ばれるため、
    `saved_user_id` が無い（`None`）ならこの run は user 行をまだ一度も保存していないことが
    確定している——本文一致で他ターンの行を探すと、同一利用者が同文で2ターンを並走させた場合
    （`chat_turns.py` は同一利用者の複数ターン並走を許す）に他方の行（personal 値・ID とも別物）
    を誤って対応付けてしまうため、探索はせず新規に保存する。
    """
    user_msg_id = None
    user_msg_personal = personal
    assistant_msg_id = None

    # personal ターンは user メッセージ保存の成否と**無関係に**会話フラグを立てる（冪等・
    # fail-closed）。stream_message が user 保存直後・フラグ更新前にクラッシュした場合、受領共有は
    # 会話フラグだけでブロックしているため、ここで立て直さないと personal な質問文が共有先に
    # 見え得る。
    if personal:
        try:
            store.set_contains_personal_workspace(conversation_id)
        except Exception as flag_exc:
            _log.warning("turn crash personal flag set failed (best-effort): %s", flag_exc)

    # (a) このターンの user メッセージ ID を確定する。ここが失敗したら、以降の (b)(c) は行わない
    # ——ID の無いまま assistant 行・監査を作ると、後から別ターンの行に誤って紐付ける余地を
    # 残すより「何も残さない」方が安全（fail-closed）。
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

    # (b) assistant 側にエラーの最小 envelope を保存する（busy 応答と同じ最小形）。
    try:
        headline = f"エラーが発生しました（{type(exc).__name__}）。もう一度お試しください。"
        env = {"lens": "chat", "headline": headline, "summary": {"total": 0}, "data": {}, "sources": []}
        saved_assistant = store.add_message(conversation_id, "assistant", headline, lens="chat",
                                            answer=env, personal=user_msg_personal)
        assistant_msg_id = saved_assistant["id"]
    except Exception as persist_exc:
        _log.warning("turn crash assistant message persistence failed (best-effort, "
                    "original error still re-raised): %s", persist_exc)

    # (c) 監査（可能なら）。失敗しても元の例外の re-raise は妨げない。
    try:
        store.audit(uid, "chat.turn", "conversation", f"conv:{conversation_id}",
                   detail={"lens": "error", "world": world, "error": type(exc).__name__,
                           "message_id_user": user_msg_id, "message_id_assistant": assistant_msg_id,
                           "personal": user_msg_personal},
                   outcome="error", severity="warning")
    except Exception as audit_exc:
        _log.warning("turn crash audit failed (best-effort): %s", audit_exc)


def _turn_run_fn(message: str, world: str, uid: str,
                 scope_paths: list, knowledge: bool, personal: bool, layer: str = "both",
                 lens: str | None = None, web_search: bool = False,
                 depth_profile: str = "standard", tools: dict | None = None,
                 tools_availability: dict | None = None,
                 provider=None, settings: dict | None = None, sys_settings: dict | None = None):
    """バックグラウンド実行本体を作る（conversation_id 確定後に呼ばれるファクトリ・MEDIUM Codex RV
    修正で予約方式になったため、`chat_turns.start_turn` の `run_fn_factory` として渡す）。
    `/chat/stream` の `gen()` と**同一の呼び分け**（knowledge の有無で neo4j_session の要否が変わる）
    をそのまま踏襲する。
    `web_search`（WEB-1・既定 False）は `ChatReq.web_search` をそのまま転送する。
    `depth_profile`（SC-6c・既定 "standard"）は `ChatReq.depth_profile` をそのまま転送する。
    `tools`（SC-6e・既定 None＝全ON）は `ChatReq.tools` をそのまま転送する。
    `tools_availability`（SC-6e・既定 `None`）: 呼び出し元（`chat_turns_start`）が受付時の422判定
    （`_validate_tools_availability`）と同時に計算した snapshot をそのまま転送する——背景実行は
    `POST /chat/turns` 応答後さらに時間が空きうるため、ここで独自に再取得すると受付時からの
    可用性の変化を拾ってしまい、明示 ON のツールが黙って無効化される窓が広がる。
    `provider`/`settings`/`sys_settings`（SC-6e・既定 `None`）: 呼び出し元（`chat_turns_start`）が
    受付段階（`_prepare_agentic_snapshot`）で組み立てた同一の Provider/設定スナップショットを
    そのまま `stream_message` へ転送する（`tools_availability` と同じ理由）。
    """
    def make_run(conversation_id: int):
        def run(stop_event: threading.Event, emit) -> None:
            # このターン自身が保存した user 行の id/personal を stream_message から直接受け取る
            # （本文一致で推測しない＝同一利用者が同文で2ターンを並走させても取り違えない）。
            saved_user: dict = {}

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
                                              provider=provider, settings=settings, sys_settings=sys_settings):
                        emit(evt)
                    return
                with neo4j_session() as s:
                    for evt in stream_message(s, message, world,
                                              conversation_id=conversation_id,
                                              scope_paths=scope_paths, layer=layer, lens=lens, knowledge=True,
                                              user_id=uid, personal=personal,
                                              users_dir=str(_USERS_DIR), stop_event=stop_event,
                                              on_user_saved=_on_user_saved, web_search=web_search,
                                              depth_profile=depth_profile, tools=tools,
                                              tools_availability=tools_availability,
                                              provider=provider, settings=settings, sys_settings=sys_settings):
                        emit(evt)
            except Exception as e:
                # neo4j_session()/stream_message 自体が例外を投げると DB に何も残らない
                # おそれがある。on_user_saved が既に発火していれば（saved_user["id"] が入って
                # いれば）このターンの user 行は保存済みなので _persist_turn_crash はその id を
                # 再利用し、まだなら（コールバック前にクラッシュ）新規に保存する。best-effort で
                # 永続してから re-raise する（chat_turns.start_turn 側の error イベント＋枠解放は
                # 従来どおり）。
                _persist_turn_crash(conversation_id, message, uid, world, personal, e,
                                    saved_user_id=saved_user.get("id"),
                                    saved_user_personal=saved_user.get("personal"))
                raise
        return run
    return make_run


@chat_router.post("/chat/turns", tags=["チャット"], response_model=ChatTurnStartResponse)
def chat_turns_start(req: ChatReq, request: Request):
    """チャットターンをバックグラウンドで開始する（画面遷移しても止まらない・覗き窓方式）。

    返り値 `{turn_id, conversation_id}` の `turn_id` で `GET /chat/turns/{turn_id}/stream` を購読する
    （途中からでも cursor で replay→追従）。同時実行数の上限（1ユーザー2・全体8）を超えると 429
    （MEDIUM・Codex RV 修正: 予約方式＝上限判定と枠の登録が atomic なので、429 のときは会話が
    一切作られない）。
    """
    u = _current_user(request)
    _check_chat_write(u, req.conversation_id)
    uid = u["uid"]
    w = _resolve_world(req.world)
    # SC-6e: settings を一度だけ読み、knowledge の実効値（Codex構成は常にON・多層防御）と
    # Provider を同じスナップショットから準備する。接続先検証（I/O-free）→可用性チェック
    # （ES/Neo4j）の順で行い、受付（422判定）と背景実行本体（_turn_run_fn）へ同じ
    # Provider/settings/snapshot を渡す——背景実行は POST 応答後さらに時間が空きうるため、
    # 別々に取得すると受付時からの可用性の変化を拾ってしまい、明示 ON のツールが黙って
    # 無効化される窓が広がる（knowledge 判定も同様に admin 更新を挟んで食い違い得る）。
    knowledge, provider, settings, sys_settings, tools_availability = _prepare_agentic_snapshot(
        uid, req.knowledge, req.web_search)
    if knowledge:
        validated_scope(w, req.scope_paths)               # 実在 world のみ＋scope 検証（開始前に弾く）
        _validate_tools_availability(req.tools, availability=tools_availability)   # SC-6e: 明示ON指定の不達ツールは422（ツール名つき）

    def _make_conversation() -> int:
        # `chat_turns.start_turn` が枠を予約した**後**・lock の**外**で呼ばれる（DB I/O をロック
        # 保持中に行わない）。`_ensure_conversation` は chat_service の既存ヘルパーそのもの
        # （挙動・実装ともに変更なし）。会話タイトルはスラッシュ接頭辞除去後の本文を使う
        # （RV1 #10・`/chat`・`/chat/stream` は `stream_message`/`handle_message` 内の
        # `_resolve_lens` が既に除去済みの本文でタイトルを作るため、ここだけ raw な
        # `req.message` を使うと `/影響 ...` がそのままタイトルに残ってしまっていた）。
        _, title_message = _extract_slash_lens(req.message)
        return _ensure_conversation(req.conversation_id, title_message, w, uid)

    run_fn_factory = _turn_run_fn(req.message, w, uid, req.scope_paths, knowledge, req.personal,
                                  layer=req.layer, lens=req.lens, web_search=req.web_search,
                                  depth_profile=req.depth_profile, tools=req.tools,
                                  tools_availability=tools_availability,
                                  provider=provider, settings=settings, sys_settings=sys_settings)
    try:
        rec = chat_turns.start_turn(uid=uid, conversation_factory=_make_conversation,
                                    run_fn_factory=run_fn_factory)
    except chat_turns.TurnLimitError:
        raise HTTPException(429, "実行中の回答が終わってからもう一度お試しください。")
    return {"turn_id": rec.turn_id, "conversation_id": rec.conversation_id}


@chat_router.get("/chat/turns/{turn_id}/stream", tags=["チャット"])
def chat_turns_stream(turn_id: str, request: Request, cursor: int = Query(0, ge=0)):
    """ターンの思考イベントを cursor から replay→追従する SSE（「途中から読める尾行」）。

    切断は購読解除にすぎない（ターンはサーバ側で続く）。完了済みターンへの購読は残イベントを
    replay してそのまま終了する。所有者以外・存在しない turn_id は 404（`/conversations/{cid}` と
    同じ「存在有無を教えない」流儀）。
    """
    u = _current_user(request)
    gen = chat_turns.iter_sse(turn_id, u["uid"], cursor)
    if gen is None:
        raise HTTPException(404, "ターンが見つかりません")
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@chat_router.get("/chat/turns/running", tags=["チャット"], response_model=ChatTurnsRunningResponse)
def chat_turns_running(request: Request):
    """現在ユーザーが実行中（未完了）のターン一覧。トップバーの「回答作成中」表示・会話を開いた際の
    自動再購読の両方で使う（サーバ再起動でレジストリが空になれば自然に「実行中なし」に戻る）。"""
    u = _current_user(request)
    recs = chat_turns.list_running(u["uid"])
    return {"turns": [{"turn_id": r.turn_id, "conversation_id": r.conversation_id,
                       "started_at": r.started_at.isoformat()} for r in recs]}


@chat_router.post("/chat/turns/{turn_id}/stop", tags=["チャット"], response_model=ChatTurnStopResponse)
def chat_turns_stop(turn_id: str, request: Request):
    """実行中ターンを停止する（本人のターンのみ）。既存 `/chat/stream/stop` と同じ挙動（stop_event を
    set するだけ＝assistant は保存されず、停止も clarify と同格に監査へ記録される・chat_service 側）。
    存在しない/他人/完了済みはすべて `{"ok": false}`（存在有無を教えない）。"""
    u = _current_user(request)
    return {"ok": chat_turns.stop_turn(turn_id, u["uid"])}


# ===== 回答ごとの利用者フィードバック =====

# 本文サイズ上限（バイト）。正当な入力（rating＋タグ最大4件＋一言500字）は UTF-8 最悪見積もりでも
# 数KB に収まるため大きく余裕を持たせつつ、FastAPI の自動 Body() パース（本文全体を無条件に
# バッファしてから検証する）より前に打ち切れる上限を設ける（`routers/audit_usage.py::
# _read_capped_json_body` と同じ、チャンク読みで打ち切る流儀）。
_FEEDBACK_BODY_MAX_BYTES = 65_536   # 64KiB
_FEEDBACK_TAGS_MAX = 4
_FEEDBACK_BODY_PARSE_ERROR_MSG = "リクエスト本文が解析できません（UTF-8 の JSON オブジェクトのみ受理します）"


async def _read_capped_feedback_body(request: Request) -> dict:
    """本文をチャンク読みで `_FEEDBACK_BODY_MAX_BYTES` まで読み、UTF-8 の JSON オブジェクトとして
    解析する。FastAPI の `Body()` 依存性注入は本文全体を無条件にバッファしてから検証するため、
    サイズ上限を効かせるにはそれより前に自前でチャンク読みする必要がある。
    """
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
    """回答ごとの利用者フィードバック（👍/👎＋定型タグ＋任意の一言）。会話の**所有者のみ**投稿できる
    （共有された会話の閲覧者は403・共有＝閲覧専用という既存契約と揃える）。同じ利用者が同じ
    メッセージへ再送すると上書きする（1利用者×1メッセージにつき最新1件のみ）。タグは重複を
    まとめたうえで最大4件（定型タグ自体が4種のみのため、それ以上は入力ミス）。入力不正の 422 は
    固定文言のみで送信値そのものは反射しない。

    本文のチャンク読みに `await request.stream()` が要るため `async def` にしている＝この関数
    自身は FastAPI の自動 threadpool 実行の対象外になる（`routers/audit_usage.py::
    admin_usage_chat` と同じ理由）。認証・DB 読み書きはいずれも同期呼び出しのため、単一 worker の
    event loop を塞がないよう `run_in_threadpool` に委譲する。
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
    tags = sorted(dict.fromkeys(tags_in))   # 重複をまとめる（保存・集計とも一意にする）
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
