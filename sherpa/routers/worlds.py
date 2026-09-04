"""資料フォルダ(World)管理エンドポイント（フェーズ3スライス6・純移動）。

`ingest_preview_router`（`GET /ingest/preview`）・`worlds_router`（`GET /world-options`・
`GET /fs/list`・`GET /worlds`・`GET /worlds/{wid}/status`・`POST /worlds/{wid}/recount`・
`POST /worlds`・`POST /worlds/diff`・`GET /worlds/{wid}/diff`・
`POST /worlds/{wid}/rebind`・`POST /worlds/{wid}/refresh`・
`POST /worlds/{wid}/reconvert`・`POST /worlds/{wid}/rag_regenerate_rules`・`DELETE /worlds/{wid}`）・
`ingest_runs_router`（`POST /ingest/rerun`・`GET /ingest/runs`）の3 router 構成。

ルート表 golden の定義順を保つため、api.py 側は削除したブロックの元位置にそれぞれ
`app.include_router(worlds_routes.ingest_preview_router)`（`/documents/download` の直後・
`/documents` の直前）・`app.include_router(worlds_routes.worlds_router)`（`/admin/es/search`
の直後・`/graph` の直前・13ルートは定義順連続のため1本で足りる）・
`app.include_router(worlds_routes.ingest_runs_router)`（`/graph/ask` の直後・`/healthz` の
直前）を置く。

`_browse_roots`/`_under_roots` は `sherpa.deps` へ移動済み（このモジュールの `fs_list`/
`_resolve_root` と、api.py 残留の `_warn_browse_roots_missing`〔lifespan 起動処理〕が共用する
ため）。`_warn_browse_roots_missing` 自体はここへは移さず api.py に残る。

`_knowledge_status_summary`（`graph_ask` 専用）は物理的にはこのブロックの真ん中にあったが、
フェーズ3スライス5で `sherpa/routers/graph.py` へ既に移動済み（このモジュールの範囲外）。

モジュール basename が既存の `sherpa/worlds.py`（世界レジストリ・鏡モデル本体）と衝突するため、
api.py 側は `from sherpa.routers import worlds as worlds_routes` のように別名 import し、
api.py モジュールレベルの裸の `worlds` 名（`sherpa.worlds`）を上書きしない。このモジュール内は
`from sherpa import worlds` のモジュール import のまま（basename 衝突は import パスが違うため
発生しない）。

ロジックは変更しない（コード移動のみ）。このモジュールは `sherpa.api` を import しない
（循環回避）。
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
from sherpa.routers.graph import _GRAPH_UNAVAILABLE_MESSAGE   # /ingest/preview も同じ固定文言で503化（RV1是正#6）
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

# 短時間ロック（recount/reconvert 等）を待たせすぎない上限（他の排他処理（rebind/delete 等）と
# 競合した場合は長時間ブロックせず 409 を返し再試行を促す）。
_EXTRACT_LOCK_TIMEOUT_MS = 10_000

_INGEST_UNAVAILABLE_MESSAGE = "取り込み台帳を読めませんでした。時間をおいてやり直してください"

# `extraction_snapshot.flags` は run 中に発生した警告/停止をファイル・構文・参照ごとに際限なく
# 積みうる（`worker._failed_files_summary` の 200 件上限と同じ考え方）。status 応答はこの生 flags を
# 素通しせず、ここで打ち切って total/truncated を併記する。
_STATUS_FLAGS_LIMIT = 200

# 単一登録契約（標準MVPは登録元フォルダを全体で1本）の下で「まだ存在しない world」を新規登録
# する試みを仲裁する固定キー（`world_create` 専用）。未登録 root への登録は、登録先が
# 決まる前は世界ごとに異なる暫定 wid で `background._REGISTRY` へ登録すると、**別々の**
# 新規登録要求（別フォルダ・別 world_id）がそれぞれ別キーとなり、in-process レジストリでは
# 何も競合しない——実際の単一登録判定は `worlds.register()` の DB advisory lock まで遅延され、
# 負けた方は 202 受付済みのまま背景で汎用理由の failed に化ける（利用者は 409 で即座に
# 知ることができない）。単一 worker 前提（このプロセス内で完結する）のため、新規登録の
# 試みは全てこの固定キーで仲裁する——`op`/`fingerprint` が一致（同一 path/label/world_id の
# 二重クリック）すれば合流、不一致（別フォルダ等の真の競合）なら受付前に 409。
_NEW_WORLD_REGISTRY_KEY = "__new_world__"


def _run_worker_or_503(wid: str, fn):
    """worker 層の呼び出し（`run`/`_run_locked`/`rerun`）を実行し、想定外の例外
    （PG/Neo4j 接続断・台帳読取失敗等）を捕捉して固定文言の 503 に変換する（共通ハンドラ）。
    未処理のまま伝播すると生の 500 になり、利用者に「何が起きたか」も「やり直せばよいこと」も
    伝わらない。best-effort で `ingest_runs` に理由付き failed を記録してから 503 を返す
    （`HTTPException`・`psycopg.errors.LockNotAvailable` 等、呼び出し元が個別に判定したい
    ステータスは対象外＝そのまま伝播させる）。worker 側（`_run_locked` の pg_replace 失敗等）が
    既に詳細な理由付きで `ingest_runs` へ記録済みの例外は、`_sherpa_ingest_run_recorded`
    属性で示される——ここでの汎用な再記録はスキップする（1回の取り込みで ingest_runs に
    2件残る二重記録を避ける）。
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
    """受付時（`create_run`）に run 行と同時に確定する初期進捗（ING-3）。
    実際の最初の段（scanning/concept_extract 等）は各操作の背景本体がすぐに上書きする——ここは
    「受け付けた」ことだけを表す共通の初期値。"""
    return {"stage": "accepted", "stage_label": ingest_worker.STAGE_LABELS["accepted"],
            "done": None, "total": None, "updated_at": datetime.now(timezone.utc).isoformat()}


def _fingerprint(payload: dict) -> str:
    """正規化 payload の fingerprint（ING-3・多重クリック合流の一致判定に使う）。
    プロセス内の等価比較にしか使わない（暗号学的な強度・衝突耐性は不要）ため、
    canonical JSON 文字列そのものを使う。"""
    import json
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _dispatch(wid: str, op: str, fingerprint: str, work_fn, *,
             extra_registry_keys: tuple[str, ...] = ()) -> tuple[int, bool]:
    """`background.start_or_join` へ委譲する共通ラッパー（ING-3・登録/更新/グラフ生成/削除/
    参照先変更/再取り込み/業務語対応の承認・無効化——world に触れる操作全般で共通）。

    受付時に O(1) で `ingest_runs` 行を確保してから（`create_run`）背景実行へ渡す——run_id は
    必ず受付時点（この関数の戻り値）で判明する（旧 `report_run_id` コールバック方式は撤去）。
    run 行自体は常に実際の `wid` に紐付ける——`extra_registry_keys` は in-process 多重クリック
    仲裁（`background._REGISTRY`）で `wid` に**加えて**登録する別名キー（`world_create` の
    未登録 root 分岐が固定キー `_NEW_WORLD_REGISTRY_KEY` を渡す）。`wid` 自体は常にキーの
    1つ——固定キーへの置き換えではなく別名の追加のため、World 行が出現した後に `wid` だけで
    来る別リクエスト（通常の register/delete 等）も同じ進行中の登録を検出できる。

    `work_fn(run_id)` は各操作の実処理そのもの——run_id 確定後の失敗記録は各操作自身の
    責務（`_run_locked`／各 `_run_*_background` の `finish_ingest_run` 系）だが、それすら
    果たされなかった想定外のケースは `background.start_or_join` 自身の CAS セーフティネット
    （`store.fail_close_if_extracting`）が拾う——ここでの二重の best-effort 記録は
    不要になった（旧実装の一発 INSERT フォールバックは撤去）。

    実行中の run と `op`/`fingerprint` が不一致なら 409（「別の処理が実行中」の平文）。
    シャットダウン処理中（`background.stop_accepting()` 済み）は 503。
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

# router に tags を持たせない: 各エンドポイントの `tags=["資料フォルダ(World)管理"]` と結合されて
# 二重化してしまう（ルート表 golden 不一致の原因）ため、tags 指定は各デコレータ側のみに残す
# （system.py:42-44 と同じパターン）。
ingest_preview_router = APIRouter()
worlds_router = APIRouter()
ingest_runs_router = APIRouter()


@ingest_preview_router.get("/ingest/preview", tags=["資料フォルダ(World)管理"])
def ingest_preview(request: Request, world: str | None = Query(None, pattern=_WORLD_PATTERN)):
    """取り込み・抽出プレビュー（A17・read-only）。world グラフの抽出内容を返す。

    `build_preview` は world 世代のプローブ（`store.get_world_status_row`）をキャッシュの鍵として
    読む（`preview_service._get_graph_bundle`・`/graph` の `graph_view` と共有する仕組み）——ここが
    失敗（DB 不調等）した場合は握り潰さずログ付き 503 にする（`/graph` と同じ固定文言・silent
    degradation なしの家風どおり・RV1是正#6・`sherpa/routers/graph.py::graph_get` 参照）。
    """
    _require_admin(_current_user(request))
    wid = _resolve_world(world)
    try:
        return build_preview(wid)
    except Exception as e:
        _log.warning("取り込みプレビューの構築に失敗しました wid=%s", wid, exc_info=True)
        raise HTTPException(503, _GRAPH_UNAVAILABLE_MESSAGE) from e


@worlds_router.get("/world-options", tags=["資料フォルダ(World)管理"], response_model=WorldOptionsResponse)
def world_options(request: Request):
    """選べる取込ディレクトリ（world）の一覧（registry ∪ KB/fixtures・チャットのセレクタ用）。1つなら UI は隠す。

    ログインユーザー全員が使う world セレクタ（`/worlds` は admin 専用のため別物）。旧称は `/versions`（撤去済）。
    """
    _current_user(request)   # ログイン必須（auth 有効時）
    names = worlds.list_worlds()
    return {"worlds": names, "labels": {n: worlds.world_label(n) for n in names}}


# ===== world（取込ディレクトリ）レジストリ管理（鏡モデル・register/rebind/delete）=====

class WorldReq(BaseModel):
    path: str                                # 参照元（フォルダ選択で得た WSL パス）
    label: str | None = None                 # 表示名（未指定はフォルダ名）。UI が見せるのはこれ
    world_id: str | None = None              # 内部識別子（省略時は自動採番＝UI からは出さない）


class RebindReq(BaseModel):
    path: str
    label: str | None = None


class DiffReq(BaseModel):
    path: str                                # 差分チェック対象フォルダ（登録はしない・read-only）


class ReconvertReq(BaseModel):
    rel: str                                 # 再変換対象（world root 相対パス・失敗一覧の1行）


def _world_admin_http_error(exc: world_admin_service.WorldAdminError) -> HTTPException:
    """service層の分類済みエラーをHTTP status codeへ対応付ける（メッセージはそのまま返す）。"""
    if isinstance(exc, world_admin_service.WorldAdminValidationError):
        return HTTPException(422, str(exc))
    if isinstance(exc, world_admin_service.WorldAdminNotFoundError):
        return HTTPException(404, str(exc))
    if isinstance(exc, world_admin_service.WorldAdminConflictError):
        return HTTPException(409, str(exc))
    return HTTPException(503, str(exc))


_public_world = world_admin_service.public_world


# ===== フォルダ選択（サーバ側エクスプローラー・/mnt 等の許可ルート配下に限定）=====
# _browse_roots / _under_roots は sherpa.deps へ移動済み（上記 import 済み・api.py 残留の
# _warn_browse_roots_missing と共用のため）。

def _subdirs(d: Path) -> list:
    try:
        entries = sorted(d.iterdir())
    except OSError:
        return []
    out = []
    for x in entries:
        try:                                          # 1件の権限エラー（Windows システムファイル等）で一覧全体を止めない
            if x.is_dir() and not x.is_symlink():
                out.append({"name": x.name, "path": str(x)})
        except OSError:
            continue
    return out


@worlds_router.get("/fs/list", tags=["資料フォルダ(World)管理"], response_model=FsListResponse)
def fs_list(request: Request, path: str = Query("")):
    """フォルダ一覧（エクスプローラー用・read-only）。許可ルート（既定 `/mnt`）配下のサブフォルダだけ返す。

    `path` 空＝許可ルートの直下（Windows ドライブ等）。物理走査だが**ディレクトリ名のみ**・許可ルート外は 403。
    ホストファイルシステム閲覧は取り込み管理操作＝admin 必須。
    """
    _require_admin(_current_user(request))
    roots = _browse_roots()
    if not path:                                   # トップ＝各ルート直下（例 /mnt/c, /mnt/d…）
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
    """取り込み状況の要約（**正直化**）: インデックス件数・未対応(Office/PDF)件数・関係グラフ・ES全文索引のチャンク数。

    `row`＝呼び出し元が既に取得済みの world 登録行（`store.get_world(wid)`）。ここでは読み直さない
    （呼び出し元ごとに `store.get_world` を重複して叩かない）。

    最終 ingest run の status と warn/blocked 理由も含める（半壊状態の可視化・監査#5）。
    `last_run_warnings`＝reason 文字列のみの一覧（doc は含まない）。対象ファイルが特定できる
    blocked flag（例: 不可読コードによる全体停止＝`unreadable_code_file`）は `last_run_blocked`
    （`doc`/`reason` 併記）で別途通す——`last_run_warnings` だけでは対象ファイルが画面に届かない。
    どちらも元の `extraction_snapshot.flags`（無制限に増えうる）を `_STATUS_FLAGS_LIMIT` 件で
    打ち切ってから導出し、`last_run_flags_total`/`last_run_flags_truncated` で打切りの有無を示す
    （200件超の world でも status 応答サイズが頭打ちになる）。

    `scanned`〜`unreadable`（`corpus_docs.scan_report`
    相当）は**ここではフォルダを歩かない**（`row["last_scan_report"]` を読むだけ）。graph/ES 件数も
    `graph_view()`（全グラフ再構築＋文書木の再走査）や ES live `_count` を**一切呼ばない**——graph は
    最新の**反映済み** run（`store.get_latest_published_run_summary`）の `published_snapshot`
    から、ES は別クエリ（`store.get_latest_es_run_summary`）の `extraction_snapshot.es` から読む
    （Neo4j load は失敗時 tx ロールバックする契約のため直近の反映済み run が「今実際に Neo4j に
    ある内容」の正しい近似になる——ただし graph と ES は完了境界が異なる〔台帳 replace 失敗の run は
    Neo4j へは反映済みでも ES へは未到達〕ため、別々の run を指しうる）。`store.get_latest_run_summary`
    は `source_doc_ids` を含まない狭い SELECT（`last_run_status`/`last_run_warnings`/`failed_files`/
    `stage_summary` はこちら＝run の成否を問わない最新の試行）。DB 呼び出しの例外はここでは
    捕捉せず呼び出し元へ伝播させる（全ゼロへ縮退して「集計できた」ように見せない・明示エラー）。
    キャッシュが無い（一度も成功同期していない）world は全ゼロ＋`counts_as_of=None`（「未集計」・
    呼び出し元は自動では歩かず、利用者が「再集計」を押すまで待つ）。
    ING-1: `failed_files`／`partial_extraction_suspected`／`stage_summary` は最新 run の
    `extraction_snapshot` 由来（run 自体が無い/該当データが無ければ None）。`failure_reason_catalog`／
    `partial_extraction_advice` は常時同じ静的な辞書（`sherpa.ingest.failure_reasons` が単一の
    真実源・利用者向け平文の原因＋対処）。

    ING-3: `last_run_id`＝最新 run の id（背景実行の受付応答〔`run_id`〕と対応付けるため）。
    `running_progress`＝実行中（`status='extracting'` かつ `progress` が実際にある）時だけ
    `{stage, stage_label, done, total, updated_at}`——資料画面はこのフィールドがある時だけ
    数秒間隔でポーリングし、無くなったら止める契約。
    """
    rep = row.get("last_scan_report")
    counts_as_of = row.get("last_scan_report_at")
    if not isinstance(rep, dict):
        rep = corpus_docs.empty_scan_report()
        counts_as_of = None
    last = store.get_latest_run_summary(wid)
    snap = (last or {}).get("extraction_snapshot")
    snap = snap if isinstance(snap, dict) else {}    # JSONB は dict 以外もありうる（RV Low・500 にしない）
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
    stage_summary = None
    if isinstance(office_md_stage, dict) or isinstance(es_stage, dict) or isinstance(neo4j_stage, dict):
        stage_summary = {
            "office_md": office_md_stage if isinstance(office_md_stage, dict) else None,
            "es": es_stage if isinstance(es_stage, dict) else None,
            "neo4j": neo4j_stage if isinstance(neo4j_stage, dict) else None,
        }
    published = store.get_latest_published_run_summary(wid)
    graph_nodes = graph_edges = 0
    if published:
        pub_snap = published.get("published_snapshot")
        pub_snap = pub_snap if isinstance(pub_snap, dict) else {}
        graph_nodes = pub_snap.get("nodes") or 0
        graph_edges = pub_snap.get("edges") or 0
    es_chunks = None
    # graph（Neo4j）と ES は反映の完了境界が異なる——台帳（PG）replace 失敗の run は Neo4j へは
    # 反映済み（`published_snapshot`/`published_at` が立つ）でも、ES 段（replace 成功後）へは
    # 一切到達していない。`get_latest_published_run_summary` をそのまま ES にも使うと、この run が
    # 「最新反映」を名乗って実際に ES へ触れた直近の run（＝有効な es.chunks を持つ run）を隠して
    # しまうため、ES 専用の別クエリ（`extraction_snapshot ? 'es'` で絞り込み済み）を使う。
    es_run = store.get_latest_es_run_summary(wid)
    if es_run:
        es_ext = es_run.get("extraction_snapshot")
        es_ext = es_ext if isinstance(es_ext, dict) else {}
        pub_es = es_ext.get("es")
        # bulk 投入が実際に成功した（`available is True` かつ `error` 無し）時だけ chunks を件数として
        # 見せる——delete_failed で旧索引が残ったまま「0件」を返す・bulk_errors で投入予定件数を
        # 実成功件数と偽って見せる、のどちらも防ぐ（不明な時は None＝UI「不明」表示）。
        if isinstance(pub_es, dict) and pub_es.get("available") is True and not pub_es.get("error"):
            es_chunks = pub_es.get("chunks")
    # ING-3: 実行中（`status='extracting'`）の run だけ進捗を載せる——完了済み run は
    # `finish_ingest_run` が `progress` を NULL へ戻す契約のため、ここで status を見るまでもなく
    # 自然に None になるが、`extracting` は `reflect=False`（staging・テスト専用経路）の**成功時
    # 終端状態**でもある（実行中とは限らない）ため、`progress` 自体の有無で最終判定する
    # （進捗が無ければ「今は動いていない」と扱う＝実害の無い保守的な判定）。
    last_status = (last or {}).get("status")
    running_progress = (last or {}).get("progress") if last_status == "extracting" else None
    running_progress = running_progress if isinstance(running_progress, dict) else None
    return {**rep, "counts_as_of": str(counts_as_of) if counts_as_of else None,
            "graph_nodes": graph_nodes, "graph_edges": graph_edges, "es_chunks": es_chunks,
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


def _ingest_summary_after_mutation(wid: str) -> dict:
    """変更操作（register/refresh/rebind/extract/concepts/recount 等）の直後に呼ぶ `_ingest_summary`
    ラッパー。行を取り直してから渡す——`GET /worlds/{wid}/status` のような「1回だけ読む」制約は
    無い（変更操作自体が既に相応の コストを払っている）。DB 例外は 503 に変換する
    （全ゼロへ縮退しない・明示エラー方針）。
    """
    try:
        row = store.get_world(wid) or {}
        return _ingest_summary(wid, row)
    except Exception as e:
        raise HTTPException(503, _INGEST_UNAVAILABLE_MESSAGE) from e


@worlds_router.get("/worlds/{wid}/status", tags=["資料フォルダ(World)管理"], response_model=WorldStatusResponse)
def world_status(wid: str, request: Request):
    """資料フォルダの取り込み状況（read-only）。何件インデックスされ、何が未対応で、グラフに何ノード出来たか。

    `store.get_world_status_row` は**1回だけ**呼び、その行を
    `_ingest_summary` へそのまま渡す（`world_admin_service.status_row` 経由だと内部でもう1回
    `store.get_world` を呼ぶ・二重読みを避ける）。`get_world()` と違い `last_manifest`
    （world 全ファイル分の JSONB）を持たない狭い SELECT——status はこの列を使わない。
    参照元の到達確認も `worlds.world_dir()`（pin/MCP override・fixtures フォールバック込みの
    多段解決）ではなく、この行の `root_path` へ `stat(follow_symlinks=False)` を1回投げるだけに
    する——status はフォルダを歩かない/深く解決しない契約のため、record 通りの絶対パスが直接
    開けるかだけを見れば十分。同じ stat 結果へ `stat.S_ISDIR` も適用する（登録後に
    root が通常ファイル/symlink へ置換されていても stat 自体は成功しうるため、種別まで見ないと
    誤って到達可能と判定してしまう・追加の I/O は発生しない）。
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
    """取り込み集計（`corpus_docs.scan_report`）を明示的にやり直す（ING-2・**唯一の明示的な実走査**）。

    `GET /worlds/{wid}/status` はキャッシュを読むだけでフォルダを歩かない——保存済みキャッシュが
    無い（未集計）world や、キャッシュ後に手動でファイルを直接触った world 等、明示的に最新化したい
    場合にこれを呼ぶ。参照元フォルダを1回走査して `worlds.last_scan_report` を更新するだけで、
    グラフ/台帳/ES には一切触れない（read-only な再集計）。

    走査自体は排他ロックの**外**で行う——2TB 級の root では走査に長時間かかりうるため、
    その間ずっと排他ロックを保持すると、同じ鍵を共有する検索（`research_service` の共有ロック）が
    timeout 到達で 503 になり、timeout 指定の無い sync/rebind/delete は走査終了までブロックされる。
    代わりに: ①短いロックで binding（`root_path`）と世代（`last_sig`・`created_at`・`updated_at`・
    `last_synced_at`・`last_scan_report_at`）を読む → ②ロック外で `worlds.pin_world_root` により
    その root を固定して走査する（走査中に他プロセスが rebind しても `world_dir()` の再解決を
    止めているので別 root を混ぜて数えない） → ③再度短いロックで①のすべてが一致する場合だけ
    1回 UPDATE する（`store.set_scan_report_if_unchanged`）。`last_sig` だけを見ると、走査中に
    別の sync が pre-invalidate→同一内容へ再確定した場合に一致してしまう ABA が起こりうるため、
    行が最後に書かれた時刻列（`updated_at`＝bind/rebind・`last_synced_at`＝sig 確定・
    `last_scan_report_at`＝前回集計時刻）と `created_at`（delete→同じ world_id で再登録された場合に
    変わる）も合わせて比較する——いずれか1つでも変化していれば「その間に何かが起きた」と検知できる。
    不一致は 409 で終え、再試行はしない——古い走査結果を新しい世代へ誤って結び付けない。

    走査中に root 自体が消失・別ディレクトリへ置換されても、上の binding/世代比較だけでは
    検知できない場合がある（同じパスへ別ディレクトリが再作成された等）。走査の直前・直後に
    `lstat` して同一ディレクトリ実体（`st_dev`/`st_ino` の一致）であることも確認し、消失・
    置換・走査自体の例外はいずれも 503 とし、集計結果を保存しない（全ゼロの走査結果を
    「正しく再集計できた」かのように確定させない）。pre/post どちらの `lstat` 結果にも
    `stat.S_ISDIR` を適用する——通常ファイル/symlink へ置換された場合、置換前後で
    `st_dev`/`st_ino` が一致することもありうる（置換後さらに元と同じ実体へ戻された場合等）ため、
    同一性比較だけでは「置換され続けている」ケースを見逃しうる。
    """
    _require_admin(_current_user(request))
    if not valid_world(wid):
        raise HTTPException(422, "不正な識別子")
    try:
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
    """資料フォルダを登録して取り込む（**冪等**・ING-3＝即受付・取り込みは背景で継続）。

    未登録→登録＋取り込み／登録済み（同一フォルダ）→登録はスキップしてリラン（変更検知で
    再取り込み）——この判定自体は軽い（root 解決＋point lookup のみ）ため、ここで同期的に行い
    受付応答の `world_id` を確定する。実際の取り込み（`worlds.register`/`ingest_worker.sync`）は
    `sherpa.ingest.background` の world 単位レジストリへ委ねる——多重クリックは新しい run を
    始めず既存 run の `run_id` へ合流する（`joined=True`）。

    単一登録契約（標準MVPは登録元フォルダを全体で1本に固定する・鏡モデル）の最終判定は
    `worlds.register()` が world_lock 保持中に原子的に行う。**未登録 root への新規登録**は
    確定 `wid` に加えて固定キー（`_NEW_WORLD_REGISTRY_KEY`）を別名として同じ `_BgRun` に
    登録する（`extra_registry_keys`）——別フォルダ・別 world_id への競合登録要求は、それぞれ
    別の暫定 wid を持つため wid 単位だけでは衝突が見えず、固定キーが無いと両方 202 受付済みに
    なった上で片方が背景で汎用理由の failed に化けていた（利用者は 409 で即座に知ることが
    できなかった）。`wid` も同時にキーとして登録する（固定キーへの置き換えではない）ため、
    World 行が実際に出現した後に別リクエストが `wid` だけで来ても（通常の register/delete 等）
    同じ進行中の登録を検出し、`op`/`fingerprint`（canonical root・実効 label/world_id から
    生成）が一致（同一内容の二重クリック）すれば合流、不一致（別 world_id での既存 wid への
    横取り等）なら run 作成前に 409——別 run を生成しない。ただし、この仲裁は「同時に
    in-flight な2要求」の衝突だけを閉じる——後発リクエストの軽い先読み
    （`store.list_worlds_db()`）が先発の実 DB 登録完了と僅かにすれ違う極小窓は残り、その場合は
    従来どおり背景側の `worlds.register()` が `WorldConflict` で検出しログに残す（クライアントは
    status ポーリングで「進んでいない」ことに気付く）。
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
        # 未登録 root＝新規登録の枠を巡る仲裁（docstring参照）。`wid` にこのキーを別名として
        # 追加する（置き換えない）。
        extra_registry_keys = (_NEW_WORLD_REGISTRY_KEY,)

    display_label = req.label or Path(root).name

    def _work(run_id):
        # `world_id=wid`（req.world_id ではなく受付時に確定した値）・`root=root`（canonical・
        # 再解決しない）——受付応答の判断（登録先 wid・fingerprint）に使った値と実際に
        # 登録される値の食い違いを避ける。
        world_admin_service.register_or_rerun(req.path, label=req.label, world_id=wid,
                                              root=root, run_id=run_id)

    fp = _fingerprint({"root": root, "label": display_label, "world_id": wid})
    run_id, joined = _dispatch(wid, "register", fp, _work, extra_registry_keys=extra_registry_keys)
    return {"ok": True, "world_id": wid, "run_id": run_id, "joined": joined,
            "note": "既存の取り込みに合流しました。" if joined
                    else "受け付けました。状況は取り込み状況でご確認ください。"}


@worlds_router.post("/worlds/diff", tags=["資料フォルダ(World)管理"], response_model=WorldDiffResponse)
def world_diff_path(req: DiffReq, request: Request):
    """差分チェック（**read-only・登録しない**）。選んだフォルダ現状 vs 取り込み済みを比較し追加/削除/変更を返す。

    未登録フォルダなら全ファイルが「追加」（＝登録したら入る件数のプレビュー）。グラフ/台帳/ES には一切書かない。
    """
    _require_admin(_current_user(request))
    try:
        return {"ok": True, **world_admin_service.diff_path(req.path)}
    except world_admin_service.WorldAdminError as exc:
        raise _world_admin_http_error(exc) from exc


@worlds_router.get("/worlds/{wid}/diff", tags=["資料フォルダ(World)管理"], response_model=WorldDiffResponse)
def world_diff_id(wid: str, request: Request):
    """登録済み資料フォルダの差分チェック（read-only）。今のフォルダ内容と取り込み済みの差を返す（「今すぐ更新」前の確認用）。"""
    _require_admin(_current_user(request))
    try:
        return {"ok": True, **world_admin_service.diff_world(wid)}
    except world_admin_service.WorldAdminError as exc:
        raise _world_admin_http_error(exc) from exc


@worlds_router.post("/worlds/{wid}/rebind", tags=["資料フォルダ(World)管理"],
                    response_model=WorldIngestAcceptedResponse, status_code=202)
def world_rebind(wid: str, req: RebindReq, request: Request):
    """参照先パス変更を即受付する（ING-3＝背景実行）。**その world を全削除して新パスから作り直し**
    （破棄→再作成・他 world は無傷・fail-closed＝取り込み失敗時は旧状態へ復元。復元自体も再構築
    のため最悪2回のフル取り込みになりうる・同じ run の内部段として扱い第2 run は作らない）。

    受付前に登録有無（404）に加え、参照先パスの実在性（`stat(follow_symlinks=False)`＋
    `S_ISDIR`・`world_admin_service.resolve_root`）も同期に検証する——単純な誤入力
    （フォルダ選び間違い等）は 422 で即座に返し、200番台の受付だけして即失敗するのを避ける。
    単一登録契約との衝突（極小窓の同時登録等）は背景実行の world_lock 内で再検証する
    （defense-in-depth・ここでの検証は「速い誤りの弾き」が目的でこれを置き換えない）。多重クリック
    （同じ path/label）は既存 run へ合流、異なる path/label は 409。
    """
    _require_admin(_current_user(request))
    try:
        world_admin_service.ensure_registered(wid)
        world_admin_service.resolve_root(req.path)   # 同期の実在性検証（stat＋S_ISDIR）
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
    """「今すぐ取り込み直す」を即受付する（ユーザ意図の実行・ING-3＝背景実行）。

    **変更があった時だけ**再取り込み（変更検知・即反映ライブ鏡）——判定・実行本体は
    `world_admin_service.refresh`/`ingest_worker.sync` にそのまま任せ、
    `sherpa.ingest.background` の world 単位レジストリで多重クリックを合流させる（folder poller
    の定期 sync も同じ op で参加するため、無害に合流する）。
    """
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
    """`world_rag_regenerate_rules` の背景実行本体（L5・§8.6-2「規則版で再生成」）。

    LLM 成形キャッシュの一掃・rag.md の規則版への作り直し・ES 反映は
    `ingest_worker.regenerate_rag_rule_only` へ一元委譲する（`store.world_lock` は同関数が
    内部で確保する・`_run_concepts_confirm_background` 等と違い `_run_locked` は経由しない
    ——世界全体の再構築ではなく rag.md 層だけの軽い作り直しのため）。
    """
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
    """rag.md の LLM 成形を一掃し規則版へ作り直すことを即受付する（管理者の明示操作・ING-3＝
    背景実行・L5・§8.6-2）。監査要件等で LLM 出力を今すぐ一掃したい場合に使う。

    LLM 成形の既定トグル（`rag_llm_render`）自体は変えない——一掃は時点操作であり、トグルが
    ON のままなら次回のバックグラウンド後追いパスが改めて LLM 成形を試みうる（恒久的に止めたい
    場合はトグルも OFF にすること）。
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
    """1ファイルの変換をやり直す（ING-1・失敗一覧の「再変換」ボタン）。

    部分 derive の新機構は持たない——対象 rel の旧形式変換キャッシュ（安定して壊れた変換結果が
    再ビルドをまたいで残っていた場合の痕跡）だけ落としてから、`_run_locked` を直接1回実行する
    （world 全体を作り直す）。IR 構築失敗などキャッシュを持たない失敗は、通常の sync でも原本
    mtime/size が不変なら再試行されない（`sync()` の変更検知契約）ため、無条件で全再構築する
    ここでの実行自体が再試行の主因になる。

    world/root/rel の再検証からキャッシュ削除・`_run_locked` 実行までを既存
    `store.world_lock(wid)` 1区間で完結させる（他の背景実行と同じ確立済みパターン）。
    `sync(force=True)` を経由しない——`sync()` 自身が `world_state()` で1回全木走査した上で
    `run()`→`_run_locked()` がさらに1回走査する二重走査になる上、ロック取得も別区間になり
    検証と実行の間に他の sync/rebind が割り込みうる。`_run_locked` は
    `status="failed"` のみを返す契約（`sync()` 固有の `"unavailable"` は無い）ため、root 消失時も
    503 として扱われる（従来 `sync()` 経由だと `unavailable` が 200 成功扱いになっていた）。
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
                # キャッシュ削除の失敗を無視しない——落とせなかった旧形式キャッシュを
                # 抱えたまま再構築すると、安定して壊れたファイルが再変換されないまま「再変換した」
                # ことになってしまう。sync 前に 503 で止める。
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
    """PART-6: 削除 run の terminal 化を Webhook 通知する（`_run_delete_background` の post-event
    位置＝実処理の成否が分かる箇所からのみ呼ぶ・best-effort・削除自体の成否には影響させない）。
    `status` は `ingest_runs.status` と同じ語彙（"auto_published"|"failed"）。

    RV是正#5: 呼び出し元は「run の terminal 化（`finish_ingest_run*`）が実際に成功した」場合
    だけこれを呼ぶこと——terminal 化自体が失敗した（run 行が `status='extracting'` のまま）のに
    通知だけ送ると、通知内容と DB の実際の状態が食い違う。
    """
    try:
        webhooks.notify_run_terminal(wid, run_id, "delete", status)
    except Exception:
        _log.warning("Webhook 通知の起動に失敗しました（削除自体は継続）: world=%s", wid, exc_info=True)


def _run_delete_background(wid: str, u: dict | None, run_id: int) -> None:
    """`world_delete` の背景実行本体（派生物wipe＋レジストリ削除・ING-3）。

    world_lock は `world_admin_service.delete`→`worlds.delete` が内部で1回だけ取得する（R3-S3の
    契約はそのまま・ここで重ねて取らない）。`run_id` は呼び出し元（`_dispatch`）が受付時に
    O(1) で確保済み（ここで新しい行は作らない）。進捗は「deleting」の1段のみ（削除はファイル
    単位の内訳を持たない）。

    fail-closed 契約は不変（グラフ削除に失敗したら `world_admin_service.delete` が例外を投げ、
    world 行は残る）。成功時は `world_admin_service.delete(run_id=run_id)` が world 行 DELETE と
    run 完了 UPDATE を同一トランザクションで確定する。失敗時はここで `finish_ingest_run`
    により run を理由付きで閉じる——world 行は残るため、以後の status ポーリングで理由を確認
    できる。post-event 監査（`world.deleted`）は実処理の成否が分かるここで行う（HTTP 応答は
    受付時点で返却済みのため、ここが唯一の機会）。
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
    except world_admin_service.WorldAdminError as exc:   # グラフ削除失敗＝fail-closed（行は残す）
        # RV是正#5: 通知は「terminal 更新（`finish_ingest_run`）が実際に成功した」ことだけを条件に
        # する——`finish_ingest_run` 自体が例外で失敗した場合（下の except で best-effort ログの
        # み）、run 行は `status='extracting'` のままなので `failed` を通知してはいけない
        # （通知内容と DB の実際の状態が食い違う）。
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
    # 成功: world 行の削除と run 完了は `world_admin_service.delete`（`worlds.delete` →
    # `store.finish_ingest_run_and_delete_world`）が同一トランザクションで既に確定済み。
    try:
        store.audit(uid, "world.deleted", "world", f"world:{wid}", outcome="success", severity="critical")
    except Exception:
        pass
    _notify_delete_terminal(wid, run_id, "auto_published")


@worlds_router.delete("/worlds/{wid}", tags=["資料フォルダ(World)管理"],
                      response_model=WorldIngestAcceptedResponse, status_code=202)
def world_delete(wid: str, request: Request):
    """world の削除を即受付する（ING-3＝背景実行）。派生物 wipe＋レジストリ削除は背景で完走する。

    受付前に登録有無（404/422）と pre-event 監査（fail-closed・記録できなければ削除を開始しない）
    だけをここで確定させる。実削除（`world_admin_service.delete`・グラフ削除失敗時は行を残す
    fail-closed 契約は不変）は世界単位の背景実行へ委譲する——多重クリックは新しい run を始めず
    既存の削除 run の `run_id` へ合流する。進捗は「検索用データを削除しています」の1段のみ。
    削除完了後は world 行自体が消えるため、以後の `GET /worlds/{wid}/status` は 404
    （一覧からも消える）。
    """
    u = _current_user(request)
    _require_admin(u)
    try:
        world_admin_service.ensure_registered(wid)   # 監査を書く前に 404/422 を確定させる
    except world_admin_service.WorldAdminError as exc:
        raise _world_admin_http_error(exc) from exc
    # RV HIGH: 破壊的削除は fail-closed の pre-event を先に記録（記録できなければ削除しない）。
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
    """取り込みのやり直しを即受付する（ING-3＝背景実行）。**world 全体のクリーン rebuild**
    （台帳＋グラフ反映・`ingest_worker.rerun`＝`run` と同一経路）。多重クリックは既存 run へ合流する。
    """
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
    # 絞り込みは None=全件なので既定 world に落とさない（_resolve_world は使わない）。
    w = world
    if w is not None and not valid_world(w):
        raise HTTPException(422, "不正な world ID")
    return {"world": w, "runs": store.list_ingest_runs(w)}
