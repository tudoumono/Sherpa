"""個人ワークスペースエンドポイント（フェーズ3スライス3・純移動）。

`POST/GET /workspace/files`・`DELETE /workspace/files/{file_id}`・
`GET /workspace/files/{file_id}/download`・`GET /workspace/search` を api.py から抽出する。
ロジックは変更しない（コード移動のみ）。ルート表 golden の定義順を保つため、api.py 側は
削除したブロックの元位置に `app.include_router(workspace.router)` を1回だけ置く。

sweep/GC 系（`_sweep_expired_workspace`・`_gc_orphan_workspace_files`・
`_run_workspace_maintenance`・`_sweep_expired_on_startup`）は lifespan.py が
`api._X()` 属性参照で呼ぶ契約・既存テストの monkeypatch/import 互換のため api.py に残す
（このモジュールへは移動しない）。

このモジュールは `sherpa.api` を import しない（循環回避）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from sherpa import store
from sherpa.deps import _current_user, ensure_workspace
from sherpa.schemas import (
    WorkspaceFileDeleteResponse,
    WorkspaceFilesListResponse,
    WorkspaceFileUploadResponse,
    WorkspaceSearchResponse,
)

_log = logging.getLogger("sherpa")

# router に tags を持たせない: 各エンドポイントの `tags=["個人ワークスペース"]` と結合されて
# 二重化してしまう（ルート表 golden 不一致の原因）ため、tags 指定は各デコレータ側のみに残す
# （system.py:42-44 と同じパターン）。
router = APIRouter()

# アップロード上限（10 MB）。
_WORKSPACE_MAX_BYTES = int(os.environ.get("SHERPA_WORKSPACE_MAX_BYTES", str(10 * 1024 * 1024)))
# アップロードファイルの有効期間（日数）。0 = 無期限（NULL）。デフォルト 90 日（W4）。
_WORKSPACE_TTL_DAYS = int(os.environ.get("SHERPA_WORKSPACE_TTL_DAYS", "90") or 0)
# 個人 workspace のアップロード許可拡張子 = grep 検索対象の一元定義（W2: 単一真実源）。
# 全て平文テキストファイルのみ（バイナリ不可）。
# アップロード許可と grep 対象を **同じ集合** にすることで「上げたが検索に掛からない」不整合を排除する。
# 注: これは workspace 専用の判定。共有 KB grep（grep_tool._TEXT_EXT）とは独立しており、
#     workspace を RAG（ES/Neo4j）に索引化しない不変条件を壊さない。
_WORKSPACE_ALLOWED_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".cbl", ".cob", ".cobol", ".cpy", ".copybook", ".jcl",
    ".sql", ".py", ".sh", ".bat",
}
# grep 対象 = アップロード許可と同じ集合（W2: 一元化）。binaries が将来追加されても grep から除外できる入口。
_WORKSPACE_SEARCHABLE_EXT = _WORKSPACE_ALLOWED_EXT
# アップロードファイル名の安全チェック（パス成分・制御文字を拒否）。
_SAFE_FILENAME_CHARS = set("._-() ")


def _workspace_files_dir(uid: str) -> Path:
    """個人 workspace の files/ ディレクトリ（必ず base 配下に閉じる）。

    RV r3 LOW: files/ 自体が symlink なら全エンドポイント（upload/list/delete/search/download）
    一律で fail-closed。親が symlink だと _confined_path は symlink 先を信頼ルートにしてしまう
    （agents.py の ws_files 検査と同じ穴）。HTTP API からは symlink を作れないため、これは
    FS 側の異常状態＝処理を進めない。
    """
    d = ensure_workspace(uid) / "files"
    if d.is_symlink():
        raise HTTPException(404, "ファイルが見つかりません")
    return d


def _confined_path(files_dir: Path, filename: str) -> Path | None:
    """ファイル名を resolve して files_dir 配下に収まることを確認（symlink 脱出防止）。
    収まれば確認済みパスを返す。問題があれば None。"""
    target = (files_dir / filename).resolve()
    try:
        target.relative_to(files_dir.resolve())
        return target
    except ValueError:
        return None


def _safe_workspace_filename(raw_name: str) -> str | None:
    """ブラウザアップロード名を workspace 直下の安全なファイル名に正規化する。

    日本語名は許可する。パス成分は落とし、制御文字・隠しファイル・特殊記号は拒否する。
    """
    raw = (raw_name or "").strip()
    if not raw:
        return None
    name = Path(raw.replace("\\", "/")).name.strip()
    name = unicodedata.normalize("NFKC", name)
    if not name or name in (".", "..") or name.startswith(".") or len(name) > 128:
        return None
    if "/" in name or "\\" in name:
        return None
    for ch in name:
        if ord(ch) < 32 or ord(ch) == 127:
            return None
        if ch.isalnum() or ch in _SAFE_FILENAME_CHARS:
            continue
        return None
    return name


# ===== 個人 workspace（アップロード・一覧・削除・grep）=====
# 不変条件: workspace ファイルは共有 KB（ES/Neo4j）へ索引化しない。RAG 引用元に出さない。
# 検索結果は「個人ファイル内ヒット」として別枠で返す（/documents の RAG citation とは別）。

@router.post("/workspace/files", tags=["個人ワークスペース"], response_model=WorkspaceFileUploadResponse)
async def workspace_file_upload(request: Request, file: UploadFile = File(...)):
    """個人 workspace へファイルをアップロード（current user のみ）。
    - パストラバーサル・危険なファイル名・サイズ超過・許可外拡張子を拒否。
    - 同名は上書き（台帳は upsert）。
    - 不変条件: ES/Neo4j への索引化は絶対しない（台帳のみ）。

    本文のチャンク読みに `await file.read()` が要るため `async def` にしている＝この関数自身は
    FastAPI の自動 threadpool 実行の対象外になる（`routers/chat.py::chat_message_feedback` と
    同じ理由）。認証・sha256・advisory lock 待ち・書込・監査はいずれも同期呼び出しのため、
    単一 worker の event loop を塞がないよう `run_in_threadpool` に委譲する。
    """
    u = await run_in_threadpool(_current_user, request)
    uid = u["uid"]

    # ファイル名の安全確認。
    raw_name = (file.filename or "").strip()
    if not raw_name:
        raise HTTPException(422, "ファイル名が空です")
    # Path 成分を取り出しベース名のみ使う（パストラバーサル防止）。
    safe_name = _safe_workspace_filename(raw_name)
    if not safe_name:
        raise HTTPException(422, "使用できないファイル名です（日本語・英数字・記号 ._-()スペースのみ）")
    ext = Path(safe_name).suffix.lower()
    if ext not in _WORKSPACE_ALLOWED_EXT:
        raise HTTPException(422, f"この形式のファイルは受け付けていません（{ext}）")

    # ファイルデータ読み込み（上限を超えた時点で中断して 413）。
    # await file.read() で全読みすると巨大リクエストで OOM になるため、チャンク読みで上限監視する。
    chunks: list[bytes] = []
    total = 0
    chunk_size = 65536  # 64KB チャンク。
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > _WORKSPACE_MAX_BYTES:
            raise HTTPException(413, f"ファイルサイズが上限（{_WORKSPACE_MAX_BYTES // 1024 // 1024}MB）を超えています")
        chunks.append(chunk)
    data = b"".join(chunks)

    def _finalize() -> dict:
        """workspace dir 確認・sha256・advisory lock 待ち・書込・台帳登録・監査を
        まとめて threadpool へ退避する（event loop を塞がない）。応答契約・エラー形は不変。
        """
        # workspace ディレクトリを冪等確保。
        files_dir = _workspace_files_dir(uid)
        dest = _confined_path(files_dir, safe_name)
        if dest is None:
            raise HTTPException(422, "ファイルパスが不正です")

        # sha256 計算。
        sha = hashlib.sha256(data).hexdigest()
        size = len(data)

        # W4: expires_at を計算（TTL_DAYS=0 は無期限=NULL）。
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=_WORKSPACE_TTL_DAYS)
            if _WORKSPACE_TTL_DAYS > 0 else None
        )

        # ファイル書き込み＋台帳登録を advisory lock で直列化（sweep との TOCTOU 防止）。
        # workspace_file_lock は (uid, rel_path) 単位の Postgres advisory lock。
        # sweep も同じ lock を取るため、「書き込み→DB 登録」と「claim→再確認→unlink」は同時実行されない。
        with store.workspace_file_lock(uid, safe_name):
            dest.write_bytes(data)
            # 台帳に登録（upsert）。
            # 不変条件: record_workspace_file は personal_workspace_files のみ書く。ES/Neo4j とは独立。
            row = store.record_workspace_file(uid, safe_name, str(dest), size, sha, expires_at=expires_at)

        # 監査（ファイル内容は保存しない・redacted）。
        try:
            store.audit(uid, "workspace.file_uploaded", "workspace_file", f"pwf:{row['id']}",
                        detail={"rel_path": safe_name, "size_bytes": size, "sha256": sha[:16] + "…"},
                        outcome="success", severity="info")
        except Exception:
            _log.warning("audit write failed for workspace.file_uploaded (best-effort)")

        return {"ok": True, "id": row["id"], "rel_path": row["rel_path"],
                "size_bytes": row["size_bytes"], "sha256": row["sha256"]}

    return await run_in_threadpool(_finalize)


@router.get("/workspace/files", tags=["個人ワークスペース"], response_model=WorkspaceFilesListResponse)
def workspace_file_list(request: Request):
    """個人 workspace のファイル一覧（current user のみ）。"""
    u = _current_user(request)
    rows = store.list_workspace_files(u["uid"])
    return {"files": [
        {"id": r["id"], "rel_path": r["rel_path"],
         "size_bytes": r["size_bytes"], "created_at": str(r["created_at"]),
         "expires_at": str(r["expires_at"]) if r.get("expires_at") is not None else None}
        for r in rows
    ]}


@router.delete("/workspace/files/{file_id}", tags=["個人ワークスペース"], response_model=WorkspaceFileDeleteResponse)
def workspace_file_delete(file_id: int, request: Request):
    """個人 workspace ファイルを削除（current user・本人のみ）。物理ファイルも削除。"""
    u = _current_user(request)
    uid = u["uid"]
    # 台帳から論理削除（所有者確認込み）。
    row = store.delete_workspace_file(uid, file_id)
    if not row:
        raise HTTPException(404, "ファイルが見つかりません（または削除済み）")
    # 物理ファイルを削除（best-effort：台帳は削除済みなのでファイルが消えなくてもエラーにしない）。
    try:
        p = Path(row["original_path"])
        # relative_to で workspace/files 配下に収まるか確認してから削除（symlink 脱出防止・prefix 衝突対策）。
        files_dir = _workspace_files_dir(uid)
        try:
            p.resolve().relative_to(files_dir.resolve())
            p.unlink(missing_ok=True)
        except ValueError:
            _log.warning("workspace delete: path outside files_dir, skipping unlink: %s", p)
    except Exception as e:
        _log.warning("workspace file physical delete failed for uid=%s file_id=%s: %s", uid, file_id, e)
    # 監査。
    try:
        store.audit(uid, "workspace.file_deleted", "workspace_file", f"pwf:{file_id}",
                    detail={"rel_path": row["rel_path"]},
                    outcome="success", severity="info")
    except Exception:
        _log.warning("audit write failed for workspace.file_deleted (best-effort)")
    return {"ok": True, "id": file_id, "rel_path": row["rel_path"]}


@router.get("/workspace/files/{file_id}/download", tags=["個人ワークスペース"])
def workspace_file_download(file_id: int, request: Request):
    """個人 workspace ファイルの DL（current user・本人のみ）。

    P1-c（Codex 強化計画 Phase1）: Codex が authoring/ に作成し files/ へ移動・台帳登録したファイルを
    チャットの「作成したファイル」カードから取得する DL 先（既存アップロードファイルにも同様に使える）。
    `doc_download`（共有 KB 原本DL）と同じ fail-closed 監査の流儀＝監査に失敗したら DL を許可しない。
    """
    u = _current_user(request)
    uid = u["uid"]
    row = store.get_workspace_file(uid, file_id)
    if not row:
        raise HTTPException(404, "ファイルが見つかりません（または削除済み）")
    files_dir = _workspace_files_dir(uid)   # RV r2/r3: symlink な files/ はヘルパー側で一律 404
    if not files_dir.is_dir():
        raise HTTPException(404, "ファイルが見つかりません")
    p = _confined_path(files_dir, row["rel_path"])
    # RV LOW: _confined_path は resolve 済みパスを返すため p.is_symlink() は常に False（検査が無効）。
    # symlink 拒否は未解決パス側で行う（files_dir 内 symlink 経由の別ファイル取得を fail-closed で遮断）。
    if p is None or (files_dir / row["rel_path"]).is_symlink() or not p.is_file():
        raise HTTPException(404, "ファイルが見つかりません")
    try:
        store.audit(uid, "workspace.file_downloaded", "workspace_file", f"pwf:{file_id}",
                    detail={"rel_path": row["rel_path"]}, outcome="success")
    except Exception:
        # fail-closed: 監査できない DL は許可しない（2026-07-01-監査ログ強化.md §4・doc_download と同方針）。
        _log.critical("audit write failed for workspace.file_downloaded – blocking download")
        raise HTTPException(500, "ダウンロード処理中にエラーが発生しました")
    return FileResponse(p, filename=row["rel_path"])


@router.get("/workspace/search", tags=["個人ワークスペース"], response_model=WorkspaceSearchResponse)
def workspace_search(request: Request, q: str = Query(..., min_length=1)):
    """個人 workspace の全文 grep（current user のみ）。
    不変条件: 検索範囲は current user の workspace/files/ 配下のみ。共有 KB を混ぜない。
    返り値は「個人ファイル内ヒット」として明示ラベルを付す（RAG citation ではない）。
    W1: 台帳上 status='uploaded' のファイルのみを grep 対象にする（FS 残骸ヒット防止）。
    W2: grep 対象拡張子 = _WORKSPACE_SEARCHABLE_EXT（_WORKSPACE_ALLOWED_EXT と同一・一元定義）。
    """
    u = _current_user(request)
    uid = u["uid"]
    # 空白のみの q は min_length=1 をパスするが全行マッチになる（Codex RV LOW）。
    if not q.strip():
        raise HTTPException(422, "検索語が空です")
    ensure_workspace(uid)  # 存在しない場合は冪等作成。
    files_dir = _workspace_files_dir(uid)
    if not files_dir.is_dir():
        return {"query": q, "source": "個人ファイル内ヒット", "hits": []}

    # W1: 台帳から「生きた」rel_path 集合を取得。この集合外のファイルは検索しない。
    # 論理削除済み（status='deleted'）のファイルは FS に残っていても検索対象外になる。
    live_paths = store.live_workspace_rel_paths(uid)

    q_stripped = q.strip()
    q_lower = q_stripped.lower()
    hits = []
    seen: set[tuple] = set()
    files_dir_resolved = files_dir.resolve()
    # 台帳に載っている live rel_path を直接イテレートして grep する（FS rglob を使わない・W1）。
    for rel_path in sorted(live_paths):
        # symlink 脱出防止: resolve 前の raw パスで is_symlink() を確認する。
        # _confined_path は内部で resolve() してから relative_to チェックするため、
        # symlink alias（files/ 内部の symlink → 別ファイル/残骸）を弾けない可能性がある。
        # raw パスで先に拒否して二重防御する（Codex RV MEDIUM）。
        raw = files_dir / rel_path
        if raw.is_symlink():
            _log.warning("workspace search: symlink rejected for uid=%s rel=%s", uid, rel_path)
            continue
        p = _confined_path(files_dir, rel_path)
        if p is None:
            # パス閉じ込め違反（uid slug 制約 + rel_path は自アップロード由来なので通常起きない）。
            _log.warning("workspace search: confined_path failed for uid=%s rel=%s", uid, rel_path)
            continue
        if not p.is_file():
            # 台帳にはあるが物理ファイルが消えている（best-effort 削除の逆パターン）。スキップ。
            continue
        # symlink 脱出防止（二重確認）：resolve して files_dir 配下か再確認。
        try:
            p.resolve().relative_to(files_dir_resolved)
        except ValueError:
            continue
        # W2: workspace 専用の拡張子判定（_WORKSPACE_SEARCHABLE_EXT = _WORKSPACE_ALLOWED_EXT）。
        # 共有 KB の grep_tool._TEXT_EXT は使わない（RAG 隔離・workspace 専用判定に閉じる）。
        ext = p.suffix.lower()
        if ext not in _WORKSPACE_SEARCHABLE_EXT:
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, ln in enumerate(lines):
            if q_lower not in ln.lower():
                continue
            s = max(0, i - 1)
            e = min(len(lines), i + 3)
            key = (str(p), s, e)
            if key in seen:
                continue
            seen.add(key)
            hits.append({
                "rel_path": rel_path,    # 台帳の rel_path（ファイル名・物理パスは出さない）。
                "line": i + 1,
                "text": "\n".join(lines[s:e]).strip(),
                "match": q_stripped,
            })
            if len(hits) >= 50:
                break
        if len(hits) >= 50:
            break

    # 不変条件の注記: このヒットは個人 workspace 専用。共有 KB の /documents/download とは無関係。
    # live_paths は personal_workspace_files 台帳のみ由来。ES/Neo4j は参照していない。
    return {"query": q_stripped, "source": "個人ファイル内ヒット", "hits": hits}
