"""影響分析・トラブルシュート・QA レンズのエンドポイント。
`impact_router`（`POST /impact/run`・`GET /impact/{aid}`・`GET /impact/{aid}/export.xlsx`・`GET /scopes`）と `lens_router`（`POST /troubleshoot/run`・`POST /qa/run`）の 2 router 構成（ルート表 golden の定義順を保つため分けている）。
分析結果のプロセス内キャッシュ `_analyses`/`_analyses_lock`/`_seq`/`_ANALYSES_TTL_SECONDS`（所有者照合・TTL 掃除つき）もこのモジュールに置き、api.py が再エクスポートする。
`sherpa.api` を import しない。
設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
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
from sherpa.ingest.world_neo4j import (
    GRAPH_OVERLOAD_USER_MESSAGE,
    GRAPH_SCHEMA_ERA_USER_MESSAGE,
    GraphQueryOverloadError,
    GraphSchemaEraError,
)
from sherpa.lens_service import run_qa, run_troubleshoot
from sherpa.schemas import ScopesResponse

_log = logging.getLogger("sherpa")

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
impact_router = APIRouter()
lens_router = APIRouter()

_analyses: dict[int, dict] = {}  # aid -> {"owner_uid", "created_at", "result"}（所有者照合・TTL 掃除は `_impact_gc_expired`）。
_analyses_lock = threading.Lock()  # sync endpoint は threadpool で並行実行されうるため、dict 操作を排他する。
_seq = [0]
_ANALYSES_TTL_SECONDS = 24 * 3600  # 分析結果の生存期間（in-memory・単一 worker 前提）。


def _impact_gc_expired() -> None:
    """期限切れ（`_ANALYSES_TTL_SECONDS` 超過）の分析結果を `_analyses` から掃除する（best-effort）。`_analyses_lock` を保持して行う。"""
    now = time.time()
    with _analyses_lock:
        expired = [aid for aid, entry in _analyses.items() if now - entry["created_at"] > _ANALYSES_TTL_SECONDS]
        for aid in expired:
            _analyses.pop(aid, None)


class ImpactReq(BaseModel):
    start: str
    world: str | None = _WorldField  # 登録ディレクトリ識別子。
    include_deprecated: bool = False  # 既定は active のみ（廃止/隠しを除く）。
    scope_paths: list[str] = Field(default_factory=list)  # フォルダ prefix（空＝world 全体）。


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
    """指定ノード（start）を起点に影響分析を実行し analysis_id を返す（結果はサーバのメモリに保持され再起動で消える）。"""
    u = _current_user(request)  # 認証確認のみ（admin 不要）。
    w = _resolve_world(req.world)
    sp = validated_scope(w, req.scope_paths) or None
    try:
        with neo4j_session() as s:
            result = run_impact(s, req.start, w,
                                scope_prefixes=sp, include_deprecated=req.include_deprecated)
    except GraphQueryOverloadError as e:
        # Neo4j の安全弁: 空/部分結果を「影響なし」と誤読させないため、平文（専門用語なし）の 503 に変換する。他の未処理例外は FastAPI の既定 500 に任せる。
        _log.warning("impact/run が Neo4j 安全弁で失敗（fail-loud・reason=%s・world=%s）", e.reason, w)
        raise HTTPException(503, GRAPH_OVERLOAD_USER_MESSAGE) from e
    except GraphSchemaEraError as e:
        # 旧世代の実データがある world も 503（原因は安全弁ではなく再取り込み未了）。
        _log.warning("impact/run がスキーマ世代不一致で失敗（fail-loud・world=%s・stored=%s）", w, e.stored_era)
        raise HTTPException(503, GRAPH_SCHEMA_ERA_USER_MESSAGE) from e
    _impact_gc_expired()  # 新規実行のたびに期限切れエントリを一括掃除する。
    with _analyses_lock:  # 採番＋保存を排他する（重い `run_impact` は lock の外で完了済み）。
        _seq[0] += 1
        aid = _seq[0]
        _analyses[aid] = {"owner_uid": u["uid"], "created_at": time.time(), "result": result}
    return {"analysis_id": aid, "status": "done",
            "count": len(result["items"]),
            "presumed": len(result.get("presumed") or [])}


def _impact_owned_or_404(aid: int, uid: str) -> dict:
    """`aid` の分析結果を所有者照合つきで返す。未所持/他ユーザー所持/期限切れはいずれも 404（ID の存在を秘匿する）。admin も本人以外の結果は参照できない。照合と期限切れ削除は `_analyses_lock` 下で行う。"""
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
    """analysis_id で影響分析結果を取得する（サーバのメモリ上・再起動で消える・本人の実行分のみ）。"""
    u = _current_user(request)  # 認証確認（user 可）。
    return _impact_owned_or_404(aid, u["uid"])


@impact_router.get("/impact/{aid}/export.xlsx", tags=["影響分析"])
def impact_export(aid: int, request: Request):
    """影響分析結果を Excel（xlsx）で出力し、利用者の個人 workspace/outputs 配下に保存してダウンロードを返す。"""
    u = _current_user(request)  # 認証を存在チェックより先に行う（ID 探索を防ぐ）。
    uid = u["uid"]
    result = _impact_owned_or_404(aid, uid)  # 本人の実行分のみ。
    # workspace パスは uid の slug 制約で path injection を防ぐ。`ensure_workspace` で outputs/ を確保する。
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
    """範囲セレクタ用のツリー。world のフォルダ prefix（件数つき）を返す。"""
    _current_user(request)  # ログイン必須（auth 有効時）。
    return scope_mod.scope_tree(_resolve_world(world))


@lens_router.post("/troubleshoot/run", tags=["トラブルシュート・QA"])
def troubleshoot_run(req: TroubleshootReq, request: Request):
    """症状（symptom）からナレッジグラフを辿って原因候補を調査するトラブルシュートレンズ。"""
    _current_user(request)  # ログイン必須（auth 有効時）。
    w = _resolve_world(req.world)
    sp = validated_scope(w, req.scope_paths) or None
    try:
        with neo4j_session() as s:
            return run_troubleshoot(s, req.symptom, w, scope_paths=sp)
    except GraphSchemaEraError as e:
        # 旧世代の実データがある world は impact/run と同じ 503。
        _log.warning("troubleshoot/run がスキーマ世代不一致で失敗（fail-loud・world=%s・stored=%s）", w, e.stored_era)
        raise HTTPException(503, GRAPH_SCHEMA_ERA_USER_MESSAGE) from e


@lens_router.post("/qa/run", tags=["トラブルシュート・QA"])
def qa_run(req: QaReq, request: Request):
    """仕様問い合わせ（QA）レンズ。grep/ES のみで根拠付き回答を返す（Neo4j は開かない）。"""
    _current_user(request)  # ログイン必須（auth 有効時）。
    w = _resolve_world(req.world)
    sp = validated_scope(w, req.scope_paths) or None  # qa は Neo4j を開かない（grep/ES のみ）。
    return run_qa(req.question, w, scope_paths=sp)
