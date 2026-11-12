"""「ファイルの状態を更新」: 資料フォルダを歩き、増えたファイルを台帳へ足し、なくなったファイルの行を消す。
変換・索引・資料どうしの関係には触れない。内容が変わったファイルは件数と名前を返すだけで、台帳を書かない。
設計: docs/design/scope.md「ファイルの状態を更新」
"""
from __future__ import annotations

import logging
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from . import corpus_docs, scope_infer, store, worlds
from .ingest import archive_extract, importance, name_rows, text_kind
from .store import ingest as ingest_store

_log = logging.getLogger("sherpa")

PARTIAL_STATUS = "partial"          # 台帳の status。画面は「一部使えます」
_LOCK_TIMEOUT_MS = 10_000           # 台帳を読む・書くときの短いロックの待ち上限
_WALK_DEADLINE_SEC = 600
_STUCK_GRACE_SEC = 60
_RESULT_TTL_SEC = 300
_PROGRESS_INTERVAL = 500
_NAMES_CAP = 1000                   # 応答に載せる名前の上限（種類ごと・件数は `*_total` で別に返す）

MSG_ACTIVE = "取り込み中のため、この操作はできません。終わってからお試しください"
MSG_NEVER_INGESTED = "まだ取り込みが終わっていません。先に「今すぐ更新」を押してください"
MSG_NO_DETAIL = "前回の取り込みの明細がありません。先に「今すぐ更新」を押してください"
MSG_STUCK = "確認が終わりませんでした。しばらくしてからお試しください"
MSG_CONFLICT = "他の取り込み処理と競合しました。もう一度お試しください"


class FileStateError(Exception):
    """HTTP 応答に写す失敗。`status`＝404/409/503・`message`＝利用者向けの平文。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class _Plan:
    """段①で読んだ世代と台帳。段③の書き込みが、読んだ時点から変わっていないことの確認に使う。"""
    root: str
    expected: dict
    manifest: dict
    ledger_names: list


@dataclass
class _Job:
    world: str
    state: str = "scanning"
    scanned: int = 0
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    result: dict | None = None
    message: str | None = None


_JOBS: dict[str, _Job] = {}
_LOCK = threading.Lock()


def _begin(wid: str) -> _Plan:
    """段①: 短いロックで参照先・世代・前回の明細・台帳の名前を読む。押せない状態は `FileStateError`。"""
    try:
        with store.world_lock(wid, timeout_ms=_LOCK_TIMEOUT_MS):
            row = store.get_world(wid)
            if not row:
                raise FileStateError(404, "資料フォルダが見つかりません")
            if not worlds.world_dir(wid):
                raise FileStateError(503, "参照元フォルダにアクセスできません")
            latest = store.get_latest_run_summary(wid) or {}
            if latest.get("status") in ingest_store.ACTIVE_STATUSES:
                raise FileStateError(409, MSG_ACTIVE)
            if not row.get("last_synced_at") or not row.get("last_sig"):
                raise FileStateError(409, MSG_NEVER_INGESTED)
            manifest = row.get("last_manifest")
            if not isinstance(manifest, dict):
                raise FileStateError(409, MSG_NO_DETAIL)
            expected = {k: row.get(k) for k in ("root_path", "last_sig", "created_at", "updated_at", "last_synced_at")}
            expected["ledger_stamp"] = store.ledger_stamp(wid)
            return _Plan(row["root_path"], expected, manifest, store.list_document_names(wid))
    except psycopg.errors.LockNotAvailable as e:
        raise FileStateError(409, MSG_ACTIVE) from e
    except FileStateError:
        raise
    except Exception as e:
        _log.warning("file_state: world=%s 台帳を読めませんでした (%s)", wid, e.__class__.__name__, exc_info=True)
        raise FileStateError(503, "取り込み台帳を読めませんでした。時間をおいてやり直してください") from e


def _new_row(rel: str, rp: Path) -> dict | None:
    """増えたファイル 1 件の台帳の行（status＝一部使えます）。対象外（秘匿名・設定ファイル・アーカイブ・種類が決まらない）は None。
    種類は名前で決め、決まらないものだけ先頭を読む。中身は変換しない。
    """
    if (importance.is_importance_control_path(rel) or text_kind.is_sensitive_doc_id(rel)
            or archive_extract.archive_kind(rel) is not None):
        return None
    kind = corpus_docs.classify_by_name(rel)
    if kind is None:
        res = corpus_docs.classify_file(rp, rel, rp.suffix.lower())
        if res.get("sensitive") or res["kind"] == "unreadable" or res.get("unreadable_reason") or res["doctype"] is None:
            return None
        kind = (res["doctype"], "source" if res["kind"] == "code" else "office")
    return {"name": rel, "layer": "version", "scope_path": scope_infer.rel_scope_meta(rel)["top_scope"],
            "doctype": kind[0], "branch": kind[1], "original_path": None, "md_path": None, "status": PARTIAL_STATUS}


def _has_live_archive(rel: str, main: dict) -> bool:
    """展開先の `rel` の元になるアーカイブが、いま原本にあるか（元が消えた展開先は「なくなった」に数える）。"""
    parts = rel.split("/")
    return any("/".join(parts[:i]) in main and archive_extract.archive_kind(parts[i - 1]) for i in range(1, len(parts)))


def _walk(job: _Job, plan: _Plan) -> dict:
    """段②: ロックなしで時刻と大きさだけを見る。アーカイブの展開先も歩く（歩かないと中の行が全部「なくなった」になる）。"""
    root = Path(plan.root)
    pre = root.lstat()
    if not stat.S_ISDIR(pre.st_mode):
        raise FileStateError(503, "参照元フォルダにアクセスできません")
    deadline = time.monotonic() + _WALK_DEADLINE_SEC
    skipped: dict = {}
    main: dict[str, Path] = {}
    changed: list[str] = []
    for rp, rel in scope_infer.safe_files(root, deadline=deadline, skipped=skipped):
        main[rel] = rp
        prev = plan.manifest.get(rel)
        if prev is not None:
            try:
                st = rp.stat()
                if list(prev) != [st.st_mtime_ns, st.st_ctime_ns, st.st_size]:
                    changed.append(rel)
            except OSError:
                pass
        if len(main) % _PROGRESS_INTERVAL == 0:
            job.scanned = len(main)
    archive_rels: set[str] = set()
    archives = worlds.archives_dir(job.world)
    if archives.is_dir():
        for _rp, rel in scope_infer.safe_files(archives, deadline=deadline, skipped=skipped):
            archive_rels.add(rel)
    job.scanned = len(main) + len(archive_rels)
    post = root.lstat()
    if not stat.S_ISDIR(post.st_mode) or (pre.st_dev, pre.st_ino) != (post.st_dev, post.st_ino):
        raise FileStateError(503, "確認中に参照元フォルダが変化しました。もう一度お試しください")
    if not main and plan.ledger_names:
        raise FileStateError(503, "参照元フォルダにファイルが見つかりませんでした。アクセスできるか確認してください")
    complete = not (skipped.get("unreadable_dir") or skipped.get("unreadable_file"))
    present = set(main) | {a for a in archive_rels if _has_live_archive(a, main)}
    ledger = set(plan.ledger_names)
    removed = sorted(n for n in plan.ledger_names if n not in present) if complete else []
    add_rows = []
    skipped_new = 0
    for rel in sorted(r for r in main if r not in ledger and r not in plan.manifest and r not in archive_rels):
        row = _new_row(rel, main[rel])
        if row is not None:
            add_rows.append(row)
        elif not (importance.is_importance_control_path(rel) or text_kind.is_sensitive_doc_id(rel)):
            skipped_new += 1
    return {"add_rows": add_rows, "skipped_new_total": skipped_new, "removed": removed, "changed": sorted(changed), "total": len(main),
            "incomplete": not complete}


def _commit(wid: str, plan: _Plan, found: dict, job: _Job | None = None) -> dict:
    """段③: 短いロックで、世代が同じで取り込みが動いていないときだけ差分を 1 トランザクションで書く。違えば書かず 409。
    `job` を渡すと、現行のジョブのときだけ書く（置き換えられた古いジョブは書かない）。
    """
    try:
        with store.world_lock(wid, timeout_ms=_LOCK_TIMEOUT_MS):
            latest = store.get_latest_run_summary(wid) or {}
            if latest.get("status") in ingest_store.ACTIVE_STATUSES:
                raise FileStateError(409, MSG_CONFLICT)
            if found["add_rows"] or found["removed"]:
                with _LOCK:
                    if job is not None and (_JOBS.get(wid) is not job or not _within_deadline(job, time.monotonic())):
                        raise FileStateError(409, MSG_CONFLICT)
                    if not store.apply_file_state_delta(
                            wid, add_rows=found["add_rows"], remove_names=found["removed"], expected=plan.expected,
                            add_names=name_rows.build_rows(found["add_rows"], [])):
                        raise FileStateError(409, MSG_CONFLICT)
    except psycopg.errors.LockNotAvailable as e:
        raise FileStateError(409, MSG_CONFLICT) from e
    added = [r["name"] for r in found["add_rows"]]
    out = {"added": added[:_NAMES_CAP], "changed": found["changed"][:_NAMES_CAP], "removed": found["removed"][:_NAMES_CAP],
           "added_total": len(added), "changed_total": len(found["changed"]), "removed_total": len(found["removed"]),
           "skipped_new_total": found["skipped_new_total"], "total": found["total"]}
    if found["incomplete"]:
        out["note"] = "読めないフォルダがあったため、なくなったファイルの確認は省きました"
    return out


def _run(job: _Job, plan: _Plan) -> None:
    try:
        result = _commit(job.world, plan, _walk(job, plan), job)
        job.result, job.state = result, "ready"
    except FileStateError as e:
        job.message, job.state = e.message, ("conflict" if e.status == 409 else "failed")
    except Exception as e:
        _log.warning("file_state: world=%s 確認に失敗しました (%s)", job.world, e.__class__.__name__, exc_info=True)
        job.message, job.state = f"確認できませんでした（{e.__class__.__name__}）", "failed"
    finally:
        job.finished = time.monotonic()


def _snapshot(job: _Job) -> dict:
    out = {"state": job.state, "scanned": job.scanned}
    if job.state == "ready":
        out.update(job.result or {})
    elif job.state in ("failed", "conflict"):
        out["message"] = job.message
    return out


def _within_deadline(job: _Job, now: float) -> bool:
    """未完了のジョブが、歩く期限と猶予の内側か（外なら止まったとみなす）。"""
    return now - job.started <= _WALK_DEADLINE_SEC + _STUCK_GRACE_SEC


def request(wid: str, *, start: bool = False) -> dict:
    """確認を始める（動いていればそれに合流）。終わっていれば結果を返す。`start=True` は終わった結果を捨てて新しく始める。
    押せない状態・競合は `FileStateError`（呼び出し元が 404/409/503 にする）。
    """
    now = time.monotonic()
    with _LOCK:
        for k in [k for k, j in _JOBS.items() if j.finished and now - j.finished > _RESULT_TTL_SEC]:
            _JOBS.pop(k, None)
        job = _JOBS.get(wid)
        if job is not None and job.finished and start:
            job = None
        if job is not None and not job.finished and not _within_deadline(job, now):
            return {"state": "failed", "scanned": job.scanned, "message": MSG_STUCK}
        if job is not None:
            if job.state == "conflict":
                raise FileStateError(409, job.message or MSG_CONFLICT)
            return _snapshot(job)
    plan = _begin(wid)
    with _LOCK:
        job = _JOBS.get(wid)
        if job is not None and not job.finished:
            if _within_deadline(job, time.monotonic()):
                return _snapshot(job)
            return {"state": "failed", "scanned": job.scanned, "message": MSG_STUCK}
        job = _JOBS[wid] = _Job(world=wid)
        threading.Thread(target=_run, args=(job, plan), name="file-state", daemon=True).start()
        return _snapshot(job)
