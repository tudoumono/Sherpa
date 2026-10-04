"""資料フォルダ(World)管理エンドポイント。3 つの router から成る:
- `ingest_preview_router`: `GET /ingest/preview`
- `worlds_router`: `GET /world-options`・`GET /fs/list`・`GET /worlds`・`GET /worlds/{wid}/status`・`POST /worlds/{wid}/recount`・`POST /worlds`・`POST /worlds/diff`・`POST /worlds/{wid}/rebind`・`POST /worlds/{wid}/refresh`・`POST /worlds/{wid}/reconvert`・`POST /worlds/{wid}/rag_regenerate_rules`・`DELETE /worlds/{wid}`
- `ingest_runs_router`: `POST /ingest/rerun`・`GET /ingest/runs`
api.py が元の位置にそれぞれ `app.include_router(...)` する（ルート表 golden の定義順を保つため）。`_browse_roots`/`_under_roots` は `sherpa.deps` にある。
モジュール名が `sherpa/worlds.py` と衝突するため、api.py 側は `from sherpa.routers import worlds as worlds_routes` と別名で import する。
`sherpa.api` を import しない。
設計: docs/design/scope.md「資料フォルダの登録・解決・付け替え（registry）」
"""
from __future__ import annotations

import logging
import stat
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from sherpa import corpus_docs, doc_ledger, store, webhooks, world_admin_service, worlds
from sherpa.deps import (
    _WORLD_PATTERN,
    _WorldField,
    _browse_roots,
    _current_user,
    _require_admin,
    _resolve_world,
    _under_roots,
)
from sherpa.grep_tool import valid_world
from sherpa.ingest import background, failure_reasons
from sherpa.ingest import worker as ingest_worker
from sherpa.preview_service import build_preview
from sherpa.routers.graph import _GRAPH_UNAVAILABLE_MESSAGE  # /ingest/preview も同じ固定文言で 503 にする。
from sherpa.schemas import (
    FsListResponse,
    WorldDiffResponse,
    WorldIngestAcceptedResponse,
    WorldOptionsResponse,
    WorldRecountResponse,
    WorldReconvertResponse,
    WorldsListResponse,
    WorldStatusResponse,
)

_log = logging.getLogger("sherpa")

# 短時間ロック（recount/reconvert 等）の待ち上限。他の排他処理（rebind/delete 等）と競合したら長時間ブロックせず 409 を返して再試行を促す。
_EXTRACT_LOCK_TIMEOUT_MS = 10_000

_INGEST_UNAVAILABLE_MESSAGE = "取り込み台帳を読めませんでした。時間をおいてやり直してください"

# `extraction_snapshot.flags` は際限なく増えうる（`worker._failed_files_summary` の 200 件上限と同様）ため、status 応答では打ち切って total/truncated を併記する。
_STATUS_FLAGS_LIMIT = 200

# 単一登録契約（標準 MVP は登録元フォルダを全体で 1 本）の下で、まだ存在しない world を新規登録する試みを仲裁する固定キー（`world_create` 専用）。
# 暫定 wid ごとに `background._REGISTRY` へ登録すると別フォルダの競合登録が衝突せず、負けた方が 202 受付済みのまま背景で failed になるため、新規登録の試みは全てこの固定キーで仲裁する（単一 worker 前提）。`op`/`fingerprint` が一致（同一 path/label/world_id の二重クリック）すれば合流、不一致なら受付前に 409。ただし DB の先読みと登録の間のごく短い窓で競合した場合は、202 を返したあと背景で failed になりうる。
_NEW_WORLD_REGISTRY_KEY = "__new_world__"


def _run_worker_or_503(wid: str, fn):
    """worker 層の呼び出し（`run`/`_run_locked`/`rerun`）を実行し、想定外の例外（PG/Neo4j 接続断・台帳読取失敗等）を捕捉して固定文言の 503 に変換する共通ハンドラ。best-effort で `ingest_runs` に理由付き failed を記録してから 503 を返す。
    `HTTPException`・`psycopg.errors.LockNotAvailable` 等、呼び出し元が個別に判定するステータスはそのまま伝播させる。worker 側が既に理由付きで記録済みの例外（`_sherpa_ingest_run_recorded` 属性）は再記録しない。
    """
    try:
        return fn()
    except (HTTPException, psycopg.errors.LockNotAvailable):
        raise
    except Exception as e:
        if not getattr(e, "_sherpa_ingest_run_recorded", False):
            try:
                store.add_ingest_run(wid, status="failed", source_doc_ids=[],
                                     extraction_snapshot={"docs": 0, "nodes": 0, "edges": 0,
                                                           "flags": [{"doc": None, "action": "blocked",
                                                                      "reason": f"unexpected_error:{e.__class__.__name__}"}],
                                                           "degraded": True},
                                     scan_root=None, created_by="admin")
            except Exception:
                _log.warning("失敗記録（ingest_runs）自体に失敗しました（best-effort）", exc_info=True)
        _log.warning("worker 呼び出しが想定外の例外で失敗しました wid=%s", wid, exc_info=True)
        raise HTTPException(503, _INGEST_UNAVAILABLE_MESSAGE) from e


def _initial_progress() -> dict:
    """受付時（`create_run`）に run 行と同時に確定する初期進捗。実際の最初の段は各操作の背景本体が上書きする。"""
    return {"stage": "accepted", "stage_label": ingest_worker.STAGE_LABELS["accepted"],
            "done": None, "total": None, "updated_at": datetime.now(timezone.utc).isoformat()}


def _fingerprint(payload: dict) -> str:
    """正規化 payload の fingerprint（多重クリック合流の一致判定用・プロセス内の等価比較のみ）。canonical JSON 文字列をそのまま使う。"""
    import json
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _dispatch(wid: str, op: str, fingerprint: str, work_fn, *,
             extra_registry_keys: tuple[str, ...] = ()) -> tuple[int, bool]:
    """`background.start_or_join` へ委譲する共通ラッパー（登録/更新/削除/参照先変更/再取り込み等、world に触れる操作全般で共通）。
    受付時に O(1) で `ingest_runs` 行を確保してから背景実行へ渡し、run_id を戻り値で返す。run 行は常に実際の `wid` に紐付ける。`extra_registry_keys` は多重クリック仲裁（`background._REGISTRY`）で `wid` に加えて登録する別名キー（`world_create` の未登録 root 分岐が `_NEW_WORLD_REGISTRY_KEY` を渡す）。
    `work_fn(run_id)` は各操作の実処理。想定外の失敗は `background.start_or_join` の CAS セーフティネット（`store.fail_close_if_extracting`）が拾う。
    実行中の run と `op`/`fingerprint` が不一致なら 409、シャットダウン処理中（`background.stop_accepting()` 済み）は 503。
    """
    def _create_run() -> int:
        row = store.start_ingest_run(wid, scan_root=None, created_by="admin",
                                     progress=_initial_progress())
        return row["id"]

    try:
        return background.start_or_join(wid, op, fingerprint, _create_run, work_fn,
                                        extra_keys=extra_registry_keys)
    except background.ConflictError as exc:
        raise HTTPException(409, "別の処理が実行中です。しばらくしてからもう一度お試しください") from exc
    except background.ShuttingDownError as exc:
        raise HTTPException(
            503, "サーバーの終了処理中のため受け付けられません。しばらくしてからお試しください") from exc

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
ingest_preview_router = APIRouter()
worlds_router = APIRouter()
ingest_runs_router = APIRouter()


@ingest_preview_router.get("/ingest/preview", tags=["資料フォルダ(World)管理"])
def ingest_preview(request: Request, world: str | None = Query(None, pattern=_WORLD_PATTERN)):
    """取り込み・抽出プレビュー（読み取り専用）。world グラフの抽出内容を返す。world 世代の確認に失敗した場合は、握り潰さずログ付き 503 にする（`/graph` と同じ固定文言）。"""
    _require_admin(_current_user(request))
    wid = _resolve_world(world)
    try:
        return build_preview(wid)
    except Exception as e:
        _log.warning("取り込みプレビューの構築に失敗しました wid=%s", wid, exc_info=True)
        raise HTTPException(503, _GRAPH_UNAVAILABLE_MESSAGE) from e


@worlds_router.get("/world-options", tags=["資料フォルダ(World)管理"], response_model=WorldOptionsResponse)
def world_options(request: Request):
    """選べる取込ディレクトリ（world）の一覧（チャットのセレクタ用）。1 つなら UI は隠す。ログインユーザー全員が使う（`/worlds` は admin 専用）。"""
    _current_user(request)  # ログイン必須（auth 有効時）。
    names = worlds.list_worlds()
    return {"worlds": names, "labels": {n: worlds.world_label(n) for n in names}}


# world（取込ディレクトリ）レジストリ管理（register/rebind/delete）。

class WorldReq(BaseModel):
    path: str  # 参照元（フォルダ選択で得た WSL パス）。
    label: str | None = None  # 表示名（未指定はフォルダ名）。UI が見せるのはこれ。
    world_id: str | None = None  # 内部識別子（省略時は自動採番・UI からは出さない）。


class RebindReq(BaseModel):
    path: str
    label: str | None = None


class DiffReq(BaseModel):
    path: str  # 差分チェック対象フォルダ（登録しない・読み取り専用）。


class ReconvertReq(BaseModel):
    rel: str  # 再変換対象（world root 相対パス・失敗一覧の 1 行）。


def _world_admin_http_error(exc: world_admin_service.WorldAdminError) -> HTTPException:
    """service 層の分類済みエラーを HTTP status code へ対応付ける（メッセージはそのまま返す）。"""
    if isinstance(exc, world_admin_service.WorldAdminValidationError):
        return HTTPException(422, str(exc))
    if isinstance(exc, world_admin_service.WorldAdminNotFoundError):
        return HTTPException(404, str(exc))
    if isinstance(exc, world_admin_service.WorldAdminConflictError):
        return HTTPException(409, str(exc))
    return HTTPException(503, str(exc))


_public_world = world_admin_service.public_world


# フォルダ選択（サーバ側エクスプローラー・/mnt 等の許可ルート配下に限定）。`_browse_roots`/`_under_roots` は `sherpa.deps` にある。

def _subdirs(d: Path) -> list:
    try:
        entries = sorted(d.iterdir())
    except OSError:
        return []
    out = []
    for x in entries:
        try:  # 1 件の権限エラー（Windows システムファイル等）で一覧全体を止めない。
            if x.is_dir() and not x.is_symlink():
                out.append({"name": x.name, "path": str(x)})
        except OSError:
            continue
    return out


@worlds_router.get("/fs/list", tags=["資料フォルダ(World)管理"], response_model=FsListResponse)
def fs_list(request: Request, path: str = Query("")):
    """フォルダ一覧（エクスプローラー用・読み取り専用）。許可ルート（既定 `/mnt`）配下のサブフォルダだけ返す。
    `path` 空＝許可ルートの直下。ディレクトリ名のみを走査し、許可ルート外は 403。ホストファイルシステムの閲覧は取り込み管理操作のため admin 必須。
    """
    _require_admin(_current_user(request))
    roots = _browse_roots()
    if not path:  # トップ＝各ルート直下（例 /mnt/c, /mnt/d…）。
        entries = []
        for r in roots:
            if r.is_dir():
                entries += _subdirs(r)
        return {"path": "", "parent": None, "entries": entries}
    p = Path(path)
    if ".." in p.parts or not _under_roots(p, roots):
        raise HTTPException(403, "そのフォルダは選べません（許可された範囲外）")
    rp = p.resolve()
    if rp.is_symlink() or not rp.is_dir():
        raise HTTPException(404, "フォルダが見つかりません")
    parent = rp.parent
    return {"path": str(rp),
            "parent": str(parent) if _under_roots(parent, roots) else None,
            "entries": _subdirs(rp)}


@worlds_router.get("/worlds", tags=["資料フォルダ(World)管理"], response_model=WorldsListResponse)
def worlds_list(request: Request):
    """登録済みの資料フォルダ一覧（参照元バインド付き・管理用）。"""
    _require_admin(_current_user(request))
    return {"worlds": [_public_world(r) for r in store.list_worlds_db()]}


def _ingest_summary(wid: str, row: dict) -> dict:
    """取り込み状況の要約: インデックス件数・未対応(Office/PDF)件数・関係グラフ・ES 全文索引のチャンク数。
    `row`＝呼び出し元が取得済みの world 登録行（`store.get_world(wid)`・ここでは読み直さない）。
    最終 ingest run の status と warn/blocked 理由も含める。`last_run_warnings` は reason 文字列のみの一覧で、対象ファイルが特定できる blocked flag は `last_run_blocked`（`doc`/`reason`）で別途通す。どちらも `extraction_snapshot.flags` を `_STATUS_FLAGS_LIMIT` 件で打ち切ってから導出し、`last_run_flags_total`/`last_run_flags_truncated` で打切りの有無を示す。
    `scanned`〜`unreadable` は `row["last_scan_report"]` を読むだけでフォルダを歩かない。graph/ES 件数も `graph_view()` や ES live `_count` は呼ばず、graph は最新の反映済み run（`store.get_latest_published_run_summary`）の `published_snapshot`、ES は別クエリ（`store.get_latest_es_run_summary`）の `extraction_snapshot.es` から読む（完了境界が異なり別々の run を指しうる）。`store.get_latest_run_summary` は `source_doc_ids` を含まない狭い SELECT（`last_run_status`/`last_run_warnings`/`failed_files`/`stage_summary` の由来）。DB 例外は捕捉せず呼び出し元へ伝播させる（全ゼロへ縮退しない）。キャッシュが無い world は全ゼロ＋`counts_as_of=None`（未集計・利用者が「再集計」を押すまで待つ）。
    `failed_files`/`partial_extraction_suspected`/`stage_summary`（`stage_timings`/`counts` を含む）は最新 run の `extraction_snapshot` 由来（無ければ None）。`failure_reason_catalog`/`partial_extraction_advice` は静的な辞書（`sherpa.ingest.failure_reasons` が真実源）。
    `last_run_id` は背景実行の受付応答の `run_id` と対応付ける。`running_progress` は実行中（`status='extracting'` かつ `progress` がある）時だけ `{stage, stage_label, done, total, updated_at}`（資料画面はこの間だけ数秒間隔でポーリングする）。
    """
    rep = row.get("last_scan_report")
    counts_as_of = row.get("last_scan_report_at")
    if not isinstance(rep, dict):
        rep = corpus_docs.empty_scan_report()
        counts_as_of = None
    elif corpus_docs.scan_report_missing_fields(rep):
        # `scan_report()` に項目が追加される前に保存された旧形式の集計は新しいキーを持たず、response_model の必須項目が欠けると 500 になる。欠けたキーだけ `empty_scan_report()` の既定値（0/空）で補い、`counts_as_of` は `None`（未集計）にする（補った 0 を古い時刻と一緒に返して実測 0 件に見せない）。
        rep = {**corpus_docs.empty_scan_report(), **rep}
        counts_as_of = None
    last = store.get_latest_run_summary(wid)
    snap = (last or {}).get("extraction_snapshot")
    snap = snap if isinstance(snap, dict) else {}  # JSONB は dict 以外もありうる（500 にしない）。
    flags_all = snap.get("flags") or []
    flags_total = len(flags_all)
    flags_truncated = flags_total > _STATUS_FLAGS_LIMIT
    flags = flags_all[:_STATUS_FLAGS_LIMIT]
    warns = [f.get("reason") for f in flags
             if isinstance(f, dict) and f.get("action") in ("warn", "blocked") and f.get("reason")]
    blocked = [{"doc": f.get("doc"), "reason": f.get("reason")} for f in flags
               if isinstance(f, dict) and f.get("action") == "blocked"
               and isinstance(f.get("doc"), str) and f.get("reason")]
    failed_files = snap.get("failed_files")
    partial = snap.get("partial_extraction_suspected")
    office_md_stage, es_stage, neo4j_stage = snap.get("office_md"), snap.get("es"), snap.get("neo4j")
    # `stage_timings`（段ごとの開始・終了・所要 ms）と `counts` も最新 run の extraction_snapshot 由来（既存キーは不変・追加のみ）。
    stage_timings, counts = snap.get("stage_timings"), snap.get("counts")
    stage_summary = None
    if (isinstance(office_md_stage, dict) or isinstance(es_stage, dict) or isinstance(neo4j_stage, dict)
            or isinstance(stage_timings, dict) or isinstance(counts, dict)):
        stage_summary = {
            "office_md": office_md_stage if isinstance(office_md_stage, dict) else None,
            "es": es_stage if isinstance(es_stage, dict) else None,
            "neo4j": neo4j_stage if isinstance(neo4j_stage, dict) else None,
            "stage_timings": stage_timings if isinstance(stage_timings, dict) else None,
            "counts": counts if isinstance(counts, dict) else None,
        }
    published = store.get_latest_published_run_summary(wid)
    graph_nodes = graph_edges = 0
    if published:
        pub_snap = published.get("published_snapshot")
        pub_snap = pub_snap if isinstance(pub_snap, dict) else {}
        graph_nodes = pub_snap.get("nodes") or 0
        graph_edges = pub_snap.get("edges") or 0
    es_chunks = None
    # graph（Neo4j）と ES は反映の完了境界が異なる（台帳 replace 失敗の run は Neo4j へは反映済みでも ES へは未到達）。`get_latest_published_run_summary` を ES にも使うと、実際に ES へ触れた直近の run を隠すため、ES 専用の別クエリ（`extraction_snapshot ? 'es'` で絞り込み済み）を使う。
    es_run = store.get_latest_es_run_summary(wid)
    if es_run:
        es_ext = es_run.get("extraction_snapshot")
        es_ext = es_ext if isinstance(es_ext, dict) else {}
        pub_es = es_ext.get("es")
        # bulk 投入が実際に成功した（`available is True` かつ `error` 無し）ときだけ chunks を件数として見せる（不明なときは None＝UI「不明」表示）。
        if isinstance(pub_es, dict) and pub_es.get("available") is True and not pub_es.get("error"):
            es_chunks = pub_es.get("chunks")
    # 実行中（`status='extracting'`）の run だけ進捗を載せる。`extracting` は `reflect=False`（staging・テスト専用経路）の成功時終端状態でもあるため、`progress` 自体の有無で最終判定する。
    last_status = (last or {}).get("status")
    running_progress = (last or {}).get("progress") if last_status == "extracting" else None
    running_progress = running_progress if isinstance(running_progress, dict) else None
    # 状態判定は「反映済みか」を問わない直近の ES 記録から（変更なしの再索引失敗も拾う）。
    es_attempts = store.get_recent_es_attempts(wid)
    es_state, es_error, es_index_kept = _es_reflect_state(es_attempts, running_progress)
    return {**rep, "counts_as_of": str(counts_as_of) if counts_as_of else None,
            "graph_nodes": graph_nodes, "graph_edges": graph_edges, "es_chunks": es_chunks,
            "es_state": es_state, "es_error": es_error, "es_index_kept": es_index_kept,
            "last_run_id": (last or {}).get("id"),
            "last_run_status": last_status, "last_run_warnings": warns,
            "last_run_blocked": blocked,
            "last_run_flags_total": flags_total, "last_run_flags_truncated": flags_truncated,
            "failed_files": failed_files if isinstance(failed_files, dict) else None,
            "partial_extraction_suspected": partial if isinstance(partial, dict) else None,
            "stage_summary": stage_summary,
            "running_progress": running_progress,
            "failure_reason_catalog": failure_reasons.REASON_CATALOG,
            "partial_extraction_advice": failure_reasons.PARTIAL_EXTRACTION_ADVICE}


# ES 反映の失敗理由（`es_index.index_world` の error 値）→ 利用者向けの平文。未知の値は内部の
# 例外名や接続先を含みうるため原文を出さず汎用文にする。
_ES_ERROR_TEXT = {
    "embedding_cloud_unavailable": "検索用の AI（埋め込み）に接続できませんでした",
    "embed_cache_write_failed": "一時データの保存に失敗しました（ディスクの空きを確認してください）",
    "delete_failed": "古い索引の入れ替えに失敗しました",
    "create_failed": "索引の作成に失敗しました（ディスクの空きを確認してください）",
    "bulk_failed": "索引への書き込みに失敗しました（ディスクの空きを確認してください）",
    "bulk_errors": "索引への書き込みが一部拒否されました（ディスクの空きを確認してください）",
    "no_chunks": "検索用の断片を作れませんでした",
}
# 旧索引に触れる前に打ち切られる失敗＝直前までの索引が残る。delete_failed は削除の成否を
# 証明できず、bulk 失敗は索引を空へ戻し、create 失敗・no_chunks は空のため「残る」と言えない。
_ES_ERRORS_OLD_INDEX_KEPT = frozenset({"embedding_cloud_unavailable", "embed_cache_write_failed",
                                       "human_md_es_sig_marker_drop_failed"})
_ES_PROGRESS_STAGE = "es_index"


def _es_succeeded(es) -> bool:
    return (isinstance(es, dict) and es.get("available") is True and not es.get("error")
            and (es.get("chunks") or 0) > 0)


def _old_index_kept(earlier) -> bool:
    """最新の失敗より前の記録（新しい順）をたどり、索引に触れる前の失敗は読み飛ばし、最初に出会った
    それ以外が「件数 > 0 の成功」のときだけ true（索引を消す・状態が不確定な記録に先に出会えば false）。"""
    for a in earlier:
        if _es_succeeded(a):
            return True
        err = a.get("error") if isinstance(a, dict) else None
        if not (isinstance(err, str) and err in _ES_ERRORS_OLD_INDEX_KEPT):
            return False
    return False


def _es_reflect_state(attempts, running_progress) -> tuple[str, str | None, bool | None]:
    """ES 段の記録（新しい順の `es` 一覧・新しい ES 照会はしない）から (es_state, es_error, es_index_kept)。
    es_state: reflecting（ES の段の実行中）／ok／failed／unavailable（接続できない）／unknown。
    es_index_kept は failed のとき、旧索引があったと記録で確かめられ、かつ失敗が旧索引に触れる前の
    ものだけ true。"""
    if isinstance(running_progress, dict) and running_progress.get("stage") == _ES_PROGRESS_STAGE:
        return "reflecting", None, None
    latest = attempts[0] if attempts else None
    if not isinstance(latest, dict):
        return "unknown", None, None
    err = latest.get("error")
    if err:
        key = err if isinstance(err, str) else ""
        kept = key in _ES_ERRORS_OLD_INDEX_KEPT and _old_index_kept(attempts[1:])
        return "failed", _ES_ERROR_TEXT.get(key, "原因は管理者向けの記録を確認してください"), kept
    if latest.get("available") is True:
        return "ok", None, None
    if latest.get("available") is False:
        return "unavailable", None, None
    return "unknown", None, None


def _ingest_summary_after_mutation(wid: str) -> dict:
    """変更操作（register/refresh/rebind/extract/concepts/recount 等）の直後に呼ぶ `_ingest_summary` ラッパー。行を取り直してから渡す。DB 例外は 503 に変換する（全ゼロへ縮退しない）。"""
    try:
        row = store.get_world(wid) or {}
        return _ingest_summary(wid, row)
    except Exception as e:
        raise HTTPException(503, _INGEST_UNAVAILABLE_MESSAGE) from e


@worlds_router.get("/worlds/{wid}/status", tags=["資料フォルダ(World)管理"], response_model=WorldStatusResponse)
def world_status(wid: str, request: Request):
    """資料フォルダの取り込み状況（読み取り専用）。何件インデックスされ、何が未対応で、グラフに何ノード出来たかを返す。
    参照元の到達確認は、登録された `root_path` への `stat(follow_symlinks=False)` 1 回と `stat.S_ISDIR` だけで行う（status はフォルダを歩かない）。
    """
    _require_admin(_current_user(request))
    if not valid_world(wid):
        raise HTTPException(422, "不正な識別子")
    try:
        row = store.get_world_status_row(wid)
    except Exception as e:
        raise HTTPException(503, _INGEST_UNAVAILABLE_MESSAGE) from e
    if not row:
        raise HTTPException(404, "資料フォルダが見つかりません")
    try:
        st = Path(row["root_path"]).stat(follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            raise HTTPException(503, "参照元フォルダにアクセスできません")
    except OSError:
        raise HTTPException(503, "参照元フォルダにアクセスできません")
    try:
        summary = _ingest_summary(wid, row)
    except Exception as e:
        raise HTTPException(503, _INGEST_UNAVAILABLE_MESSAGE) from e
    return {"ok": True, "world_id": wid, "label": row.get("label"), "root_path": row.get("root_path"),
            "last_synced_at": str(row["last_synced_at"]) if row.get("last_synced_at") else None,
            **summary}


@worlds_router.post("/worlds/{wid}/recount", tags=["資料フォルダ(World)管理"], response_model=WorldRecountResponse)
def world_recount(wid: str, request: Request):
    """取り込み集計を明示的にやり直す（唯一の明示的な実走査）。
    `GET /worlds/{wid}/status` はキャッシュを読むだけなので、未集計の world や手動でファイルを触った world を最新化したいときに呼ぶ。参照元フォルダを 1 回走査して集計を更新するだけで、グラフ/台帳/ES には触れない。走査中に取り込み・付け替えが入って世代が変わっていたら 409 で終え（再試行しない）、参照元が消失・置換された場合や走査自体の失敗は 503 とし、集計結果を保存しない。
    """
    _require_admin(_current_user(request))
    if not valid_world(wid):
        raise HTTPException(422, "不正な識別子")
    try:
        # ① 短いロックで binding と世代を読む
        with store.world_lock(wid, timeout_ms=_EXTRACT_LOCK_TIMEOUT_MS):
            row = store.get_world(wid)
            if not row:
                raise HTTPException(404, "資料フォルダが見つかりません")
            if not worlds.world_dir(wid):
                raise HTTPException(503, "参照元フォルダにアクセスできません")
            root_path, sig = row["root_path"], row.get("last_sig")
            created_at, updated_at = row.get("created_at"), row.get("updated_at")
            last_synced_at = row.get("last_synced_at")
            last_scan_report_at = row.get("last_scan_report_at")
        # ② ロック外で root を固定して走査する（走査中に排他ロックを持たない）
        pre_stat = Path(root_path).lstat()
        if not stat.S_ISDIR(pre_stat.st_mode):
            raise HTTPException(503, "参照元フォルダにアクセスできません")
        with worlds.pin_world_root(wid, root_path):
            report = corpus_docs.scan_report(wid)
        post_stat = Path(root_path).lstat()
        if not stat.S_ISDIR(post_stat.st_mode):
            raise HTTPException(503, "走査中に参照元フォルダが変化しました。もう一度お試しください")
        if (pre_stat.st_dev, pre_stat.st_ino) != (post_stat.st_dev, post_stat.st_ino):
            raise HTTPException(503, "走査中に参照元フォルダが変化しました。もう一度お試しください")
        # ③ 短いロックで世代が変わっていない場合だけ保存する
        with store.world_lock(wid, timeout_ms=_EXTRACT_LOCK_TIMEOUT_MS):
            if not store.set_scan_report_if_unchanged(
                    wid, report, expected_root_path=root_path, expected_sig=sig,
                    expected_created_at=created_at, expected_updated_at=updated_at,
                    expected_last_synced_at=last_synced_at,
                    expected_last_scan_report_at=last_scan_report_at):
                raise HTTPException(409, "他の取り込み処理と競合しました。もう一度お試しください")
    except HTTPException:
        raise
    except psycopg.errors.LockNotAvailable as e:
        raise HTTPException(409, "他の取り込み処理と競合しています。しばらくしてから再試行してください") from e
    except Exception as e:
        raise HTTPException(503, f"集計に失敗しました: {e.__class__.__name__}") from e
    return {"ok": True, "world_id": wid, **_ingest_summary_after_mutation(wid)}


@worlds_router.post("/worlds", tags=["資料フォルダ(World)管理"], response_model=WorldIngestAcceptedResponse,
                    status_code=202)
def world_create(req: WorldReq, request: Request):
    """資料フォルダを登録して取り込む（冪等・即受付し、取り込みは背景で継続する）。
    未登録→登録＋取り込み、登録済み（同一フォルダ）→登録はスキップして変更検知で再取り込み。受付応答の `world_id` は同期で確定する。多重クリックは既存 run の `run_id` へ合流する（`joined=True`）。
    登録元フォルダは全体で 1 本に限られ、別フォルダの競合登録や既存 world の横取りは原則として受付前に 409 になる（ごく短い窓で競合した場合は、受付後に背景で failed になりうる）。
    """
    _require_admin(_current_user(request))
    try:
        root = world_admin_service.resolve_root(req.path)
    except world_admin_service.WorldAdminError as exc:
        raise _world_admin_http_error(exc) from exc
    existing = store.world_by_root(root)
    extra_registry_keys: tuple[str, ...] = ()
    if existing:
        wid = existing["world_id"]
        if req.world_id and req.world_id != wid:
            raise _world_admin_http_error(world_admin_service.WorldAdminConflictError(
                f"そのフォルダは既に '{wid}' に登録済みです（別IDでの登録不可）"))
    else:
        try:
            registered = store.list_worlds_db()
        except Exception as exc:
            raise _world_admin_http_error(world_admin_service.WorldAdminUnavailableError(
                "資料フォルダの登録情報を取得できません")) from exc
        if registered:
            raise _world_admin_http_error(world_admin_service.WorldAdminConflictError(
                "資料フォルダは1本だけ登録できます。"
                "別のフォルダに変更する場合は、先に登録済みのフォルダを削除してください。"))
        if req.world_id:
            if not valid_world(req.world_id):
                raise HTTPException(422, "不正な識別子")
            wid = req.world_id
        else:
            try:
                wid = world_admin_service.generate_world_id(req.label or Path(root).name, root)
            except world_admin_service.WorldAdminError as exc:
                raise _world_admin_http_error(exc) from exc
        # 未登録 root＝新規登録の枠を巡る仲裁。`wid` にこのキーを別名として追加する（置き換えない）。
        extra_registry_keys = (_NEW_WORLD_REGISTRY_KEY,)

    display_label = req.label or Path(root).name

    def _work(run_id):
        # `world_id=wid`（受付時に確定した値）・`root=root`（canonical・再解決しない）を使い、受付応答の判断に使った値と実際に登録される値を一致させる。
        world_admin_service.register_or_rerun(req.path, label=req.label, world_id=wid,
                                              root=root, run_id=run_id)

    fp = _fingerprint({"root": root, "label": display_label, "world_id": wid})
    run_id, joined = _dispatch(wid, "register", fp, _work, extra_registry_keys=extra_registry_keys)
    return {"ok": True, "world_id": wid, "run_id": run_id, "joined": joined,
            "note": "既存の取り込みに合流しました。" if joined
                    else "受け付けました。状況は取り込み状況でご確認ください。"}


@worlds_router.post("/worlds/diff", tags=["資料フォルダ(World)管理"], response_model=WorldDiffResponse)
def world_diff_path(req: DiffReq, request: Request):
    """差分チェック（読み取り専用・登録しない）。選んだフォルダの現状と取り込み済みを比較し、追加/削除/変更を返す。未登録フォルダなら全ファイルが「追加」（登録したら入る件数のプレビュー）。グラフ/台帳/ES には書かない。"""
    _require_admin(_current_user(request))
    try:
        return {"ok": True, **world_admin_service.diff_path(req.path)}
    except world_admin_service.WorldAdminError as exc:
        raise _world_admin_http_error(exc) from exc


@worlds_router.post("/worlds/{wid}/rebind", tags=["資料フォルダ(World)管理"],
                    response_model=WorldIngestAcceptedResponse, status_code=202)
def world_rebind(wid: str, req: RebindReq, request: Request):
    """参照先パス変更を即受付する（背景実行）。その world を全削除して新パスから作り直す（他 world は無傷。取り込みに失敗したら旧状態への復元を試みるが、復元にも失敗した場合は旧状態を保証しない）。
    受付前に登録有無（404）と参照先パスの実在性（フォルダであること）を同期に検証し、単純な誤入力は 422 で即返す。多重クリック（同じ path/label）は既存 run へ合流、異なる path/label は 409。
    """
    _require_admin(_current_user(request))
    try:
        world_admin_service.ensure_registered(wid)
        world_admin_service.resolve_root(req.path)  # 同期の実在性検証（stat＋S_ISDIR）。
    except world_admin_service.WorldAdminError as exc:
        raise _world_admin_http_error(exc) from exc

    fp = _fingerprint({"path": req.path, "label": req.label})
    run_id, joined = _dispatch(wid, "rebind", fp, lambda run_id: world_admin_service.rebind(
        wid, req.path, label=req.label, run_id=run_id))
    return {"ok": True, "world_id": wid, "run_id": run_id, "joined": joined,
            "note": "既存の参照先変更処理に合流しました。" if joined
                    else "受け付けました。状況は取り込み状況でご確認ください。"}


@worlds_router.post("/worlds/{wid}/refresh", tags=["資料フォルダ(World)管理"],
                    response_model=WorldIngestAcceptedResponse, status_code=202)
def world_refresh(wid: str, request: Request):
    """「今すぐ取り込み直す」を即受付する（背景実行）。変更があったときだけ再取り込みする（変更検知・即反映）。多重クリックは既存 run へ合流する。"""
    _require_admin(_current_user(request))
    if not store.get_world(wid):
        raise HTTPException(404, "資料フォルダが見つかりません")

    fp = _fingerprint({})
    run_id, joined = _dispatch(wid, "refresh", fp,
                               lambda run_id: world_admin_service.refresh(wid, run_id=run_id))
    return {"ok": True, "world_id": wid, "run_id": run_id, "joined": joined,
            "note": "既存の更新に合流しました。" if joined
                    else "受け付けました。状況は取り込み状況でご確認ください。"}


def _run_rag_regenerate_rules_background(wid: str, run_id: int) -> None:
    """`world_rag_regenerate_rules` の背景実行本体。LLM 成形キャッシュの一掃・rag.md の規則版への作り直し・ES 反映を `ingest_worker.regenerate_rag_rule_only` へ委譲する（`store.world_lock` は同関数が確保し、`_run_locked` は経由しない）。"""
    if not store.get_world(wid):
        store.finish_ingest_run(run_id, status="failed", extraction_snapshot={
            "flags": [{"doc": None, "action": "blocked", "reason": "world_not_found"}]})
        return
    if not worlds.world_dir(wid):
        store.finish_ingest_run(run_id, status="failed", extraction_snapshot={
            "flags": [{"doc": None, "action": "blocked", "reason": "world_dir_unreachable"}]})
        return
    try:
        result = ingest_worker.regenerate_rag_rule_only(wid)
    except Exception as e:
        store.finish_ingest_run(run_id, status="failed", extraction_snapshot={
            "flags": [{"doc": None, "action": "blocked",
                      "reason": f"unexpected_error:{e.__class__.__name__}"}]})
        _log.warning("規則版への再生成が想定外の例外で失敗しました: world=%s", wid, exc_info=True)
        return
    if result.get("status") == "ok":
        store.finish_ingest_run(run_id, status="auto_published", extraction_snapshot={
            "docs": result.get("rag_generated", 0)})
    else:
        store.finish_ingest_run(run_id, status="failed", extraction_snapshot={
            "flags": [{"doc": None, "action": "blocked", "reason": str(result.get("status"))}],
            "docs": result.get("rag_generated", 0)})


@worlds_router.post("/worlds/{wid}/rag_regenerate_rules", tags=["資料フォルダ(World)管理"],
                    response_model=WorldIngestAcceptedResponse, status_code=202)
def world_rag_regenerate_rules(wid: str, request: Request):
    """rag.md の LLM 成形を一掃し規則版へ作り直すことを即受付する（管理者の明示操作・背景実行）。監査要件等で LLM 出力を今すぐ一掃したいときに使う。
    LLM 成形の既定トグル（`rag_llm_render`）自体は変えない（トグルが ON のままなら次回の後追いパスが再び LLM 成形を試みうる。恒久的に止めるにはトグルも OFF にする）。
    """
    _require_admin(_current_user(request))
    if not store.get_world(wid):
        raise HTTPException(404, "資料フォルダが見つかりません")
    if not worlds.world_dir(wid):
        raise HTTPException(503, "参照元フォルダにアクセスできません")

    fp = _fingerprint({})
    run_id, joined = _dispatch(wid, "rag_regenerate_rules", fp,
                               lambda run_id: _run_rag_regenerate_rules_background(wid, run_id))
    return {"ok": True, "world_id": wid, "run_id": run_id, "joined": joined,
            "note": "既存の再生成に合流しました。" if joined
                    else "受け付けました。状況は取り込み状況でご確認ください。"}


def _audit_reconvert(u: dict | None, action: str, wid: str, rel: str, *, outcome: str = "success") -> None:
    """reconvert の pre/post 監査（best-effort・actor/world/rel/結果を記録）。"""
    try:
        store.audit(u["uid"] if u else None, action, "world", f"world:{wid}",
                    detail={"world": wid, "rel": rel}, outcome=outcome)
    except Exception:
        _log.warning("reconvert 監査ログの記録に失敗しました（best-effort）: action=%s world=%s rel=%s",
                     action, wid, rel, exc_info=True)


@worlds_router.post("/worlds/{wid}/reconvert", tags=["資料フォルダ(World)管理"], response_model=WorldReconvertResponse)
def world_reconvert(wid: str, req: ReconvertReq, request: Request):
    """1 ファイルの変換をやり直す（失敗一覧の「再変換」ボタン）。
    対象の確認・旧形式変換キャッシュの削除・world 全体の作り直しを、同じ world のロックを持ったまま続けて行う。キャッシュを削除できなければ 503、参照元が消えていても 503 を返す。
    """
    u = _current_user(request)
    _require_admin(u)
    if not valid_world(wid):
        raise HTTPException(422, "不正な識別子")
    from sherpa.ingest.arms import legacy_convert
    attempted = False
    outcome = "failure"
    run = None
    try:
        with store.world_lock(wid, timeout_ms=_EXTRACT_LOCK_TIMEOUT_MS):
            if not store.get_world(wid):
                raise HTTPException(404, "資料フォルダが見つかりません")
            if not worlds.world_dir(wid):
                raise HTTPException(503, "参照元フォルダにアクセスできません")
            if not doc_ledger.original_path(req.rel, wid):
                raise HTTPException(404, "対象ファイルが見つかりません")
            attempted = True
            _audit_reconvert(u, "world.reconvert_requested", wid, req.rel)
            ext = Path(req.rel).suffix.lower()
            if ext in legacy_convert.LEGACY_EXT_MAP:
                # キャッシュ削除の失敗を無視しない（落とせなかった旧形式キャッシュを抱えたまま再構築しない）。sync 前に 503 で止める。
                cache_root = legacy_convert.cache_root_for(worlds.derived_md_dir(wid))
                if not legacy_convert.drop_cache_entry(cache_root, req.rel):
                    raise HTTPException(503, "キャッシュの削除に失敗しました。時間をおいてやり直してください")
            run = _run_worker_or_503(
                wid, lambda: ingest_worker._run_locked(wid, reflect=True, created_by="admin", scan_root=None,
                                                       op="refresh"))
            if run["status"] == "failed":
                raise HTTPException(503, f"再変換に失敗しました: {run.get('flags')}")
            outcome = "success"
    except HTTPException:
        raise
    except psycopg.errors.LockNotAvailable as e:
        raise HTTPException(409, "他の取り込み処理と競合しています。しばらくしてから再試行してください") from e
    finally:
        if attempted:
            _audit_reconvert(u, "world.reconverted", wid, req.rel, outcome=outcome)
    return {"ok": True, "world_id": wid, "rel": req.rel, "changed": True,
            "status": run["status"], "ledger": run.get("ledger"), "flags": run.get("flags", []),
            "summary": _ingest_summary_after_mutation(wid),
            "note": "更新（今すぐ取り込み直す）と同じ処理が world 全体に対して走りました。"}


def _notify_delete_terminal(wid: str, run_id: int, status: str) -> None:
    """削除 run の terminal 化を Webhook 通知する（`_run_delete_background` の post-event 位置から呼ぶ・best-effort・削除自体の成否には影響させない）。`status` は `ingest_runs.status` と同じ語彙（"auto_published"|"failed"）。
    呼び出し元は run の terminal 化（`finish_ingest_run*`）が実際に成功した場合だけ呼ぶ（通知内容と DB の状態を食い違わせない）。
    """
    try:
        webhooks.notify_run_terminal(wid, run_id, "delete", status)
    except Exception:
        _log.warning("Webhook 通知の起動に失敗しました（削除自体は継続）: world=%s", wid, exc_info=True)


def _run_delete_background(wid: str, u: dict | None, run_id: int) -> None:
    """`world_delete` の背景実行本体（派生物 wipe＋レジストリ削除）。
    world_lock は `world_admin_service.delete`→`worlds.delete` が内部で 1 回だけ取得する（ここで重ねて取らない）。`run_id` は受付時（`_dispatch`）に確保済み。進捗は「deleting」の 1 段のみ。
    fail-closed: グラフ削除に失敗したら `world_admin_service.delete` が例外を投げ、world 行は残る。成功時は `world_admin_service.delete(run_id=run_id)` が world 行 DELETE と run 完了 UPDATE を同一トランザクションで確定する。失敗時はここで `finish_ingest_run` により run を理由付きで閉じる。post-event 監査（`world.deleted`）は実処理の成否が分かるここで行う（HTTP 応答は受付時点で返却済み）。
    """
    try:
        store.update_ingest_run_progress(run_id, {
            "stage": "deleting", "stage_label": ingest_worker.STAGE_LABELS["deleting"],
            "done": None, "total": None,
            "updated_at": datetime.now(timezone.utc).isoformat()})
    except Exception:
        _log.warning("進捗の記録に失敗しました（削除自体は継続）: world=%s stage=deleting", wid, exc_info=True)
    uid = u["uid"] if u else None
    try:
        world_admin_service.delete(wid, run_id=run_id)
    except world_admin_service.WorldAdminError as exc:  # グラフ削除失敗＝fail-closed（行は残す）。
        # 通知は terminal 更新（`finish_ingest_run`）が実際に成功したときだけ行う（失敗なら run 行は `status='extracting'` のままで、`failed` を通知してはいけない）。
        finished = False
        try:
            store.finish_ingest_run(
                run_id, status="failed",
                extraction_snapshot={"flags": [{"doc": None, "action": "blocked",
                                                "reason": f"delete_failed:{exc.__class__.__name__}"}]})
            finished = True
        except Exception:
            _log.warning("削除失敗の記録自体に失敗しました（best-effort）: world=%s", wid, exc_info=True)
        try:
            store.audit(uid, "world.deleted", "world", f"world:{wid}",
                        outcome="failure", severity="critical", reason=exc.__class__.__name__)
        except Exception:
            pass
        _log.warning("背景削除がグラフ削除失敗で中止しました（fail-closed・行は保持）: world=%s",
                     wid, exc_info=True)
        if finished:
            _notify_delete_terminal(wid, run_id, "failed")
        return
    except Exception as e:
        finished = False
        try:
            store.finish_ingest_run(
                run_id, status="failed",
                extraction_snapshot={"flags": [{"doc": None, "action": "blocked",
                                                "reason": f"unexpected_error:{e.__class__.__name__}"}]})
            finished = True
        except Exception:
            _log.warning("削除失敗の記録自体に失敗しました（best-effort）: world=%s", wid, exc_info=True)
        try:
            store.audit(uid, "world.deleted", "world", f"world:{wid}",
                        outcome="failure", severity="critical", reason=e.__class__.__name__)
        except Exception:
            pass
        _log.warning("背景削除が想定外の例外で失敗しました: world=%s", wid, exc_info=True)
        if finished:
            _notify_delete_terminal(wid, run_id, "failed")
        return
    # 成功: world 行の削除と run 完了は `world_admin_service.delete`（`worlds.delete` → `store.finish_ingest_run_and_delete_world`）が同一トランザクションで確定済み。
    try:
        store.audit(uid, "world.deleted", "world", f"world:{wid}", outcome="success", severity="critical")
    except Exception:
        pass
    _notify_delete_terminal(wid, run_id, "auto_published")


@worlds_router.delete("/worlds/{wid}", tags=["資料フォルダ(World)管理"],
                      response_model=WorldIngestAcceptedResponse, status_code=202)
def world_delete(wid: str, request: Request):
    """world の削除を即受付する（背景実行）。派生物の削除＋レジストリ削除は背景で完走する。
    受付前に登録有無（404/422）と監査の記録だけを確定する（監査を記録できなければ削除を開始しない）。多重クリックは既存の削除 run の `run_id` へ合流する。削除が完了すると world が消え、以後の `GET /worlds/{wid}/status` は 404（一覧からも消える）。グラフの削除に失敗した場合は world を残し、削除 run を `failed` で終える。
    """
    u = _current_user(request)
    _require_admin(u)
    try:
        world_admin_service.ensure_registered(wid)  # 監査を書く前に 404/422 を確定させる。
    except world_admin_service.WorldAdminError as exc:
        raise _world_admin_http_error(exc) from exc
    # 破壊的削除は fail-closed の pre-event を先に記録する（記録できなければ削除しない）。
    try:
        store.audit(u["uid"] if u else None, "world.delete_requested", "world", f"world:{wid}",
                    outcome="success", severity="critical")
    except Exception:
        _log.critical("audit write failed for world.delete_requested – fail-closed (削除中止)")
        raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed・削除中止）")

    fp = _fingerprint({})
    run_id, joined = _dispatch(wid, "delete", fp,
                               lambda run_id: _run_delete_background(wid, u, run_id))
    return {"ok": True, "world_id": wid, "run_id": run_id, "joined": joined,
            "note": "既存の削除処理に合流しました。" if joined
                    else "受け付けました。削除が完了すると一覧から消えます。"}


class RerunReq(BaseModel):
    world: str | None = _WorldField


@ingest_runs_router.post("/ingest/rerun", tags=["資料フォルダ(World)管理"],
                         response_model=WorldIngestAcceptedResponse, status_code=202)
def ingest_rerun(req: RerunReq, request: Request):
    """取り込みのやり直しを即受付する（背景実行）。world 全体のクリーン rebuild（台帳＋グラフ反映）。多重クリックは既存 run へ合流する。"""
    _require_admin(_current_user(request))
    w = _resolve_world(req.world)
    if not valid_world(w):
        raise HTTPException(422, "不正な world ID")
    fp = _fingerprint({})
    run_id, joined = _dispatch(w, "rerun", fp, lambda run_id: ingest_worker.rerun(w, run_id=run_id))
    return {"ok": True, "world_id": w, "run_id": run_id, "joined": joined,
            "note": "既存の再取り込みに合流しました。" if joined
                    else "受け付けました。状況は取り込み状況でご確認ください。"}


@ingest_runs_router.get("/ingest/runs", tags=["資料フォルダ(World)管理"])
def ingest_runs_list(request: Request, world: str | None = Query(None)):
    """取り込み実行履歴（world 指定で絞り込み可・未指定＝全件）。"""
    _require_admin(_current_user(request))
    # 絞り込みは None=全件なので既定 world に落とさない（`_resolve_world` は使わない）。
    w = world
    if w is not None and not valid_world(w):
        raise HTTPException(422, "不正な world ID")
    return {"world": w, "runs": store.list_ingest_runs(w)}
