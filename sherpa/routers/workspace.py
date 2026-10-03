"""個人ワークスペースのエンドポイント: `POST/GET /workspace/files`・`DELETE /workspace/files/{file_id}`・`GET /workspace/files/{file_id}/download`・`GET /workspace/search`。
sweep/GC 系（`_sweep_expired_workspace`・`_gc_orphan_workspace_files`・`_run_workspace_maintenance`・`_sweep_expired_on_startup`）は lifespan.py が `api._X()` で呼ぶため api.py に残す。
`sherpa.api` を import しない。
設計: docs/design/users.md「個人の作業領域（workspace）」
"""
from __future__ import annotations

import hashlib
import logging
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from sherpa import store, text_encoding, workspace_limits
from sherpa.deps import _current_user, ensure_workspace
from sherpa.schemas import (
    WorkspaceFileDeleteResponse,
    WorkspaceFilesListResponse,
    WorkspaceFileUploadResponse,
    WorkspaceSearchResponse,
)

_log = logging.getLogger("sherpa")

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
router = APIRouter()

# 個人 workspace のアップロード許可拡張子＝grep 検索対象の一元定義。全て平文テキストのみ。
# アップロード許可と grep 対象を同じ集合にして、上げたのに検索に掛からない状態をなくす。workspace 専用の判定で、共有 KB grep（`grep_tool._TEXT_EXT`）とは独立（workspace を RAG に索引化しない）。
_WORKSPACE_ALLOWED_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".cbl", ".cob", ".cobol", ".cpy", ".copybook", ".jcl",
    ".sql", ".py", ".sh", ".bat",
}
# grep 対象＝アップロード許可と同じ集合。
_WORKSPACE_SEARCHABLE_EXT = _WORKSPACE_ALLOWED_EXT
# アップロードファイル名の安全チェック（パス成分・制御文字を拒否）。
_SAFE_FILENAME_CHARS = set("._-() ")


def _workspace_files_dir(uid: str) -> Path:
    """個人 workspace の files/ ディレクトリ（必ず base 配下に閉じる）。files/ 自体が symlink なら全エンドポイント（upload/list/delete/search/download）で fail-closed にする（`_confined_path` は symlink 先を信頼ルートにしてしまうため）。"""
    d = ensure_workspace(uid) / "files"
    if d.is_symlink():
        raise HTTPException(404, "ファイルが見つかりません")
    return d


def _confined_path(files_dir: Path, filename: str) -> Path | None:
    """ファイル名を resolve して files_dir 配下に収まることを確認する（symlink 脱出防止）。収まれば確認済みパスを返し、問題があれば None。"""
    target = (files_dir / filename).resolve()
    try:
        target.relative_to(files_dir.resolve())
        return target
    except ValueError:
        return None


def _safe_workspace_filename(raw_name: str) -> str | None:
    """ブラウザアップロード名を workspace 直下の安全なファイル名に正規化する。日本語名は許可し、パス成分は落とし、制御文字・隠しファイル・特殊記号は拒否する。"""
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


# 個人 workspace（アップロード・一覧・削除・grep）。workspace ファイルは共有 KB（ES/Neo4j）へ索引化せず、RAG の引用元に出さない。検索結果は「個人ファイル内ヒット」として別枠で返す。

@router.post("/workspace/files", tags=["個人ワークスペース"], response_model=WorkspaceFileUploadResponse)
async def workspace_file_upload(request: Request, file: UploadFile = File(...)):
    """個人 workspace へファイルをアップロードする（current user のみ）。
    - パストラバーサル・危険なファイル名・サイズ超過・許可外拡張子を拒否する。
    - 同名は上書きする（台帳は upsert）。
    - ES/Neo4j へは索引化しない（台帳のみ）。
    """
    u = await run_in_threadpool(_current_user, request)
    uid = u["uid"]

    # ファイル名の安全確認。
    raw_name = (file.filename or "").strip()
    if not raw_name:
        raise HTTPException(422, "ファイル名が空です")
    # Path 成分を取り出してベース名のみ使う（パストラバーサル防止）。
    safe_name = _safe_workspace_filename(raw_name)
    if not safe_name:
        raise HTTPException(422, "使用できないファイル名です（日本語・英数字・記号 ._-()スペースのみ）")
    ext = Path(safe_name).suffix.lower()
    if ext not in _WORKSPACE_ALLOWED_EXT:
        raise HTTPException(422, f"この形式のファイルは受け付けていません（{ext}）")

    # アップロード上限・有効期間（日数・0 は無期限）は管理画面の設定。
    max_bytes = workspace_limits.max_bytes()
    ttl_days = workspace_limits.ttl_days()

    # チャンク読みで上限を監視し、超えた時点で中断して 413 にする（全読みでの OOM を避ける）。
    chunks: list[bytes] = []
    total = 0
    chunk_size = 65536  # 64KB チャンク。
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, f"ファイルサイズが上限（{max_bytes // 1024 // 1024}MB）を超えています")
        chunks.append(chunk)
    data = b"".join(chunks)

    def _finalize() -> dict:
        """workspace dir 確認・sha256・advisory lock 待ち・書込・台帳登録・監査をまとめて threadpool へ退避する（event loop を塞がない）。"""
        # workspace ディレクトリを冪等に確保する。
        files_dir = _workspace_files_dir(uid)
        dest = _confined_path(files_dir, safe_name)
        if dest is None:
            raise HTTPException(422, "ファイルパスが不正です")

        # sha256 を計算する。
        sha = hashlib.sha256(data).hexdigest()
        size = len(data)

        # expires_at を計算する（TTL_DAYS=0 は無期限=NULL）。
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=ttl_days)
            if ttl_days > 0 else None
        )

        # ファイル書き込み＋台帳登録を、(uid, rel_path) 単位の advisory lock（`workspace_file_lock`）で直列化する。sweep も同じ lock を取る。
        with store.workspace_file_lock(uid, safe_name):
            dest.write_bytes(data)
            # 台帳に登録する（upsert）。`record_workspace_file` は personal_workspace_files だけを書く。
            row = store.record_workspace_file(uid, safe_name, str(dest), size, sha, expires_at=expires_at)

        # 監査（ファイル内容は保存しない）。
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
    """個人 workspace のファイル一覧を返す（current user のみ）。"""
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
    """個人 workspace ファイルを削除する（本人のみ）。物理ファイルも削除する。"""
    u = _current_user(request)
    uid = u["uid"]
    # 台帳から論理削除する（所有者確認込み）。
    row = store.delete_workspace_file(uid, file_id)
    if not row:
        raise HTTPException(404, "ファイルが見つかりません（または削除済み）")
    # 物理ファイルを削除する（best-effort: 台帳は削除済みなので、ファイルが消えなくてもエラーにしない）。
    try:
        p = Path(row["original_path"])
        # relative_to で workspace/files 配下に収まることを確認してから削除する（symlink 脱出・prefix 衝突対策）。
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
    """個人 workspace ファイルをダウンロードする（本人のみ）。Codex が作成して files/ へ移し台帳登録したファイルを、チャットの「作成したファイル」カードから取得する先でもある。監査に失敗したらダウンロードを許可しない。"""
    u = _current_user(request)
    uid = u["uid"]
    row = store.get_workspace_file(uid, file_id)
    if not row:
        raise HTTPException(404, "ファイルが見つかりません（または削除済み）")
    files_dir = _workspace_files_dir(uid)  # symlink な files/ はヘルパー側で一律 404。
    if not files_dir.is_dir():
        raise HTTPException(404, "ファイルが見つかりません")
    p = _confined_path(files_dir, row["rel_path"])
    # `_confined_path` は resolve 済みパスを返すため `p.is_symlink()` は常に False になる。symlink 拒否は未解決パス側で行う。
    if p is None or (files_dir / row["rel_path"]).is_symlink() or not p.is_file():
        raise HTTPException(404, "ファイルが見つかりません")
    try:
        store.audit(uid, "workspace.file_downloaded", "workspace_file", f"pwf:{file_id}",
                    detail={"rel_path": row["rel_path"]}, outcome="success")
    except Exception:
        # fail-closed: 監査できないダウンロードは許可しない（`doc_download` と同じ）。
        _log.critical("audit write failed for workspace.file_downloaded – blocking download")
        raise HTTPException(500, "ダウンロード処理中にエラーが発生しました")
    return FileResponse(p, filename=row["rel_path"])


@router.get("/workspace/search", tags=["個人ワークスペース"], response_model=WorkspaceSearchResponse)
def workspace_search(request: Request, q: str = Query(..., min_length=1)):
    """個人 workspace の全文 grep（current user のみ）。検索範囲は current user の workspace/files/ 配下だけで、共有 KB は混ぜない。返り値は「個人ファイル内ヒット」として明示ラベルを付ける（RAG citation ではない）。
    台帳上 status='uploaded' のファイルだけを対象にし、対象の拡張子はアップロードで許可される拡張子と同じ。
    """
    u = _current_user(request)
    uid = u["uid"]
    # 空白のみの q は min_length=1 を通るが全行にマッチしてしまうため拒否する。
    if not q.strip():
        raise HTTPException(422, "検索語が空です")
    ensure_workspace(uid)  # 存在しない場合は冪等に作成する。
    files_dir = _workspace_files_dir(uid)
    if not files_dir.is_dir():
        return {"query": q, "source": "個人ファイル内ヒット", "hits": []}

    # 台帳から「生きた」rel_path 集合を取得し、この集合外（論理削除済みを含む）のファイルは検索しない。
    live_paths = store.live_workspace_rel_paths(uid)

    q_stripped = q.strip()
    q_lower = q_stripped.lower()
    hits = []
    seen: set[tuple] = set()
    files_dir_resolved = files_dir.resolve()
    # 台帳に載っている live rel_path を直接イテレートして grep する（FS の rglob は使わない）。
    for rel_path in sorted(live_paths):
        # symlink 脱出防止: resolve 前の raw パスで `is_symlink()` を確認する（`_confined_path` は内部で resolve するため symlink alias を弾けない可能性がある）。
        raw = files_dir / rel_path
        if raw.is_symlink():
            _log.warning("workspace search: symlink rejected for uid=%s rel=%s", uid, rel_path)
            continue
        p = _confined_path(files_dir, rel_path)
        if p is None:
            # パス閉じ込め違反（uid slug 制約と、rel_path が自アップロード由来のため通常は起きない）。
            _log.warning("workspace search: confined_path failed for uid=%s rel=%s", uid, rel_path)
            continue
        if not p.is_file():
            # 台帳にはあるが物理ファイルが消えている（best-effort 削除の逆パターン）。スキップする。
            continue
        # symlink 脱出防止（二重確認）: resolve して files_dir 配下かを再確認する。
        try:
            p.resolve().relative_to(files_dir_resolved)
        except ValueError:
            continue
        # workspace 専用の拡張子判定（`_WORKSPACE_SEARCHABLE_EXT`）。共有 KB の `grep_tool._TEXT_EXT` は使わない。
        ext = p.suffix.lower()
        if ext not in _WORKSPACE_SEARCHABLE_EXT:
            continue
        try:
            raw_bytes = p.read_bytes()
            enc = text_encoding.detect_bytes(raw_bytes, complete=True)
            lines = text_encoding.decode(raw_bytes, enc).splitlines()
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
                "rel_path": rel_path,  # 台帳の rel_path（ファイル名のみ・物理パスは出さない）。
                "line": i + 1,
                "text": "\n".join(lines[s:e]).strip(),
                "match": q_stripped,
            })
            if len(hits) >= 50:
                break
        if len(hits) >= 50:
            break

    # このヒットは個人 workspace 専用（`live_paths` は personal_workspace_files 台帳のみ由来で ES/Neo4j は参照しない）。
    return {"query": q_stripped, "source": "個人ファイル内ヒット", "hits": hits}
