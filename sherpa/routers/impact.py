"""影響分析＋トラブルシュート・QA レンズ エンドポイント（フェーズ3スライス5・純移動）。

`impact_router`（`POST /impact/run`・`GET /impact/{aid}`・`GET /impact/{aid}/export.xlsx`・
`GET /scopes`）と `lens_router`（`POST /troubleshoot/run`・`POST /qa/run`）の2 router 構成。
会話管理ブロックが両者の間に挟まるため（golden の定義順維持のため）1 router にまとめられない。
プロセス内状態 `_analyses`/`_analyses_lock`/`_seq`/`_ANALYSES_TTL_SECONDS`（IDOR対策・TTL掃除つき
分析結果キャッシュ）もこのモジュールへ純移動する。api.py 側は
`from sherpa.routers.impact import _analyses, _ANALYSES_TTL_SECONDS, ...` で再エクスポートし、
`tests/api/test_impact_idor.py` の `api._analyses[...]` 参照は同一オブジェクトの属性参照として
互換のまま動く。ロジックは変更しない（コード移動のみ）。

ルート表 golden の定義順を保つため、api.py 側は削除したブロックの元位置にそれぞれ
`app.include_router(impact.impact_router)`（旧 `/workspace/search` の直後・`/chat` の直前）・
`app.include_router(impact.lens_router)`（旧 `/conversations/{cid}`（PATCH）の直後・
`/documents/download` の直前）を置く。

このモジュールは `sherpa.api` を import しない（循環回避）。
"""
from __future__ import annotations

import logging
import threading
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from sherpa import store
from sherpa import scope as scope_mod
from sherpa.deps import (
    _WORLD_PATTERN,
    _WorldField,
    _current_user,
    _resolve_world,
    ensure_workspace,
    neo4j_session,
    validated_scope,
)
from sherpa.export_excel import build_xlsx
from sherpa.impact_service import run_impact
from sherpa.ingest.world_neo4j import GRAPH_OVERLOAD_USER_MESSAGE, GraphQueryOverloadError
from sherpa.lens_service import run_qa, run_troubleshoot
from sherpa.schemas import ScopesResponse

_log = logging.getLogger("sherpa")

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# router に tags を持たせない: 各エンドポイントの `tags=["影響分析"]`（等）と結合されて
# 二重化してしまう（ルート表 golden 不一致の原因）ため、tags 指定は各デコレータ側のみに残す
# （system.py:42-44 と同じパターン）。
impact_router = APIRouter()
lens_router = APIRouter()

_analyses: dict[int, dict] = {}   # aid -> {"owner_uid", "created_at", "result"}（IDOR対策・TTL掃除は _impact_gc_expired）
_analyses_lock = threading.Lock()   # sync endpoint は threadpool で並行実行され得るため dict 操作を排他する（監査台帳 MED-1）
_seq = [0]
_ANALYSES_TTL_SECONDS = 24 * 3600   # 分析結果の生存期間（監査台帳#1・in-memory/単一worker前提は変えない）


def _impact_gc_expired() -> None:
    """期限切れ（`_ANALYSES_TTL_SECONDS` 超過）の分析結果を `_analyses` から掃除する（軽量・呼び出し時 best-effort）。

    `_analyses_lock` を保持して行う（反復中に別リクエストが辞書を変更する競合を防ぐ・監査台帳 MED-1）。
    """
    now = time.time()
    with _analyses_lock:
        expired = [aid for aid, entry in _analyses.items() if now - entry["created_at"] > _ANALYSES_TTL_SECONDS]
        for aid in expired:
            _analyses.pop(aid, None)


class ImpactReq(BaseModel):
    start: str
    world: str | None = _WorldField      # = 登録ディレクトリ識別子
    include_deprecated: bool = False     # 既定は active のみ（廃止/隠しを既定除外・AT-5）
    scope_paths: list[str] = Field(default_factory=list)   # フォルダ prefix（空＝world 全体）


class TroubleshootReq(BaseModel):
    symptom: str
    world: str | None = _WorldField
    scope_paths: list[str] = Field(default_factory=list)


class QaReq(BaseModel):
    question: str
    world: str | None = _WorldField
    scope_paths: list[str] = Field(default_factory=list)


@impact_router.post("/impact/run", tags=["影響分析"])
def impact_run(req: ImpactReq, request: Request):
    """指定ノード（start）を起点に影響分析を実行し analysis_id を返す（結果は in-memory・単一 worker 前提）。"""
    u = _current_user(request)   # 認証確認のみ（admin 不要・user 可）
    w = _resolve_world(req.world)
    sp = validated_scope(w, req.scope_paths) or None
    try:
        with neo4j_session() as s:
            result = run_impact(s, req.start, w,
                                scope_prefixes=sp, include_deprecated=req.include_deprecated)
    except GraphQueryOverloadError as e:
        # secRV 範囲外是正（2026-07-19・影響分析の Neo4j 安全弁）: 空/部分結果を「影響なし」と
        # 誤読させない＝fail-loud で 503（平文・専門用語ゼロ）へ変換する。他の未処理例外は
        # 従来どおり FastAPI の既定 500 に委ねる（本エンドポイント固有の縮退はここだけ）。
        _log.warning("impact/run が Neo4j 安全弁で失敗（fail-loud・reason=%s・world=%s）", e.reason, w)
        raise HTTPException(503, GRAPH_OVERLOAD_USER_MESSAGE) from e
    _impact_gc_expired()   # 新規実行のたびに期限切れエントリを一括掃除（監査台帳#1 TTL）
    with _analyses_lock:   # 採番＋保存を排他（監査台帳 MED-1・重い run_impact は lock 外で既に完了済み）
        _seq[0] += 1
        aid = _seq[0]
        _analyses[aid] = {"owner_uid": u["uid"], "created_at": time.time(), "result": result}
    return {"analysis_id": aid, "status": "done",
            "count": len(result["items"]),
            "presumed": len(result.get("presumed") or [])}


def _impact_owned_or_404(aid: int, uid: str) -> dict:
    """`aid` の分析結果を所有者照合つきで返す。未所持/他ユーザー所持/期限切れはいずれも 404
    （403 でなく 404＝ID の存在自体を秘匿する。既存 impact_export のコメントと同じ思想・監査台帳#1 IDOR対策）。
    admin も本人以外の結果は参照不可（台帳に別段の記載なし＝最小権限）。

    照合＋期限切れ削除は `_analyses_lock` 下で行う（監査台帳 MED-1・他リクエストとの競合を防ぐ）。
    """
    with _analyses_lock:
        entry = _analyses.get(aid)
        if entry is None:
            raise HTTPException(404, "not found")
        if time.time() - entry["created_at"] > _ANALYSES_TTL_SECONDS:
            _analyses.pop(aid, None)
            raise HTTPException(404, "not found")
        if entry["owner_uid"] != uid:
            raise HTTPException(404, "not found")
        return entry["result"]


@impact_router.get("/impact/{aid}", tags=["影響分析"])
def impact_get(aid: int, request: Request):
    """analysis_id で影響分析結果を取得（in-memory・サーバ再起動で消える）。本人の実行分のみ（IDOR対策）。"""
    u = _current_user(request)   # 認証確認（user 可）
    return _impact_owned_or_404(aid, u["uid"])


@impact_router.get("/impact/{aid}/export.xlsx", tags=["影響分析"])
def impact_export(aid: int, request: Request):
    """影響分析結果を Excel（xlsx）で出力し、利用者の個人 workspace/outputs 配下に保存してダウンロードを返す。"""
    u = _current_user(request)   # 認証を存在チェックより先に（ID 探索を防ぐ）
    uid = u["uid"]
    result = _impact_owned_or_404(aid, uid)   # 本人の実行分のみ（IDOR対策・監査台帳#1）
    # workspace パスは uid slug 制約で path injection を防ぐ（uid は _UID_PATTERN 互換）。
    # ensure_workspace で outputs/ ディレクトリが確保されていることを保証。
    out = ensure_workspace(uid) / "outputs" / f"impact_{aid}.xlsx"
    build_xlsx(result, out)
    try:
        store.audit(uid, "document.downloaded", "document", f"impact:{aid}",
                    detail={"download_type": "export_xlsx", "analysis_id": aid},
                    outcome="success")
    except Exception:
        _log.warning("audit write failed for document.downloaded (best-effort)")
    return FileResponse(out, filename=out.name, media_type=_XLSX)


@impact_router.get("/scopes", tags=["範囲"], response_model=ScopesResponse)
def scopes(request: Request, world: str | None = Query(None, pattern=_WORLD_PATTERN)):
    """範囲セレクタ用のツリー（D）。world のフォルダ prefix（件数つき）を返す。"""
    _current_user(request)   # ログイン必須（auth 有効時）
    return scope_mod.scope_tree(_resolve_world(world))


@lens_router.post("/troubleshoot/run", tags=["トラブルシュート・QA"])
def troubleshoot_run(req: TroubleshootReq, request: Request):
    """症状（symptom）からナレッジグラフを辿って原因候補を調査するトラブルシュートレンズ。"""
    _current_user(request)   # ログイン必須（auth 有効時）
    w = _resolve_world(req.world)
    sp = validated_scope(w, req.scope_paths) or None
    with neo4j_session() as s:
        return run_troubleshoot(s, req.symptom, w, scope_paths=sp)


@lens_router.post("/qa/run", tags=["トラブルシュート・QA"])
def qa_run(req: QaReq, request: Request):
    """仕様問い合わせ（QA）レンズ。grep/ES のみで根拠付き回答を返す（Neo4jは開かない）。"""
    _current_user(request)   # ログイン必須（auth 有効時）
    w = _resolve_world(req.world)
    sp = validated_scope(w, req.scope_paths) or None   # qa は Neo4j を開かない（grep/ES のみ）
    return run_qa(req.question, w, scope_paths=sp)
