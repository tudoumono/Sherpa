"""取り込み・抽出プレビュー（読み取り専用）。資料の一覧・フォルダ・重要度の診断（`build_preview`）と、資料フォルダのグラフ
（`world_graph.build_world`）の要素・関係・件数（`build_preview_graph`）を画面が描ける形に整形する。Neo4j は使わず、書き込みもしない。
資料の一覧は取り込みの台帳から出し、台帳が空のときだけ期限つきで資料フォルダを歩く。グラフは画面が求めたときだけ組み立てる。
取り込み中・一度も完了していない資料フォルダ・資料が多い資料フォルダではグラフを組み立てず、件数は取り込みの記録から出す。
設計: docs/design/scope.md「範囲（scope）＝フォルダ部分木のフィルタ」・docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import dataclasses
import os
import stat
import threading
import time
from pathlib import Path
from typing import NamedTuple

from . import corpus_docs, doc_ledger, scope_infer as si, store, worlds
from .ingest import importance, text_kind, world_graph_service


def _build(world: str, *, files=None):
    """有効グラフの構築を共通入口（worker と同じ）へ委譲する。`files` は `build_effective_world` へそのまま渡す（materialize 済み list）。"""
    if not worlds.world_dir(world):
        return [], [], []
    return world_graph_service.build_effective_world(world, files=files)


def _counts(nodes, edges, *, doc_count: int) -> dict:
    """組み立てたグラフからの件数サマリ。"""
    return {
        "entities": len(nodes),
        "relations": len(edges),
        "deprecated": sum(1 for n in nodes if n.get("status", "active") == "deprecated"),
        "hidden": sum(1 for n in nodes if n.get("status", "active") == "hidden_candidate"),
        "documents": doc_count,
    }


def _preview_entities_relations(nodes, edges) -> tuple[list, list]:
    """`build_preview` の entities／relations の整形（純粋な整形部分）。"""
    by_cid = {n["cid"]: n for n in nodes}

    entities = [{
        "name": n["name"], "label": n["label"],
        "status": n.get("status", "active"), "value": n.get("value"),
        "top_scope": n.get("top_scope"), "phase": n.get("phase"), "path": n.get("path"),
        "analyzer": n.get("analyzer"),
    } for n in nodes]
    entities.sort(key=lambda e: (e["label"], e["name"]))

    relations = []
    for e in edges:
        src, dst = by_cid.get(e["src"]), by_cid.get(e["dst"])
        relations.append({
            "type": e["type"], "src": src["name"] if src else e["src"].rsplit("#", 1)[-1],
            "dst": dst["name"] if dst else e["dst"].rsplit("#", 1)[-1],
            "src_label": src["label"] if src else "?", "dst_label": dst["label"] if dst else "?",
            "status": e.get("status", "active"), "doc": e.get("doc", ""),
        })
    relations.sort(key=lambda r: (r["type"], r["src"]))
    return entities, relations


# 資料フォルダ → グラフ構築結果のプロセス内キャッシュ（単一 worker 前提）。
# キャッシュ対象はグラフ構築のみ（文書一覧・重要度・診断は毎回計算する）
_GRAPH_VIEW_CACHE: dict[str, dict] = {}

# miss 時の構築を 1 本のロックで直列化する（single-flight）
_GRAPH_VIEW_LOCK = threading.Lock()

# `_GRAPH_VIEW_LOCK` 保持中の DB プローブに課す有限 timeout（秒）
_LOCK_PROBE_TIMEOUT_S = 5

# グラフを組み立てないときに画面へ返す理由（平文）
_NOTE_RUNNING = "取り込み中のため、要素と関係の詳細は取り込みが終わってから表示します。"
_NOTE_NOT_COMPLETED = "まだ取り込みが完了していないため、要素と関係の詳細は表示できません。取り込みが終わると表示されます。"
_NOTE_LAST_UNFINISHED = "前回の取り込みが最後まで終わっていないため、要素と関係の詳細は表示できません。取り込みが終わると表示されます。"


def _current_world_status(world: str, *, connect_timeout: float | None = None,
                          statement_timeout_ms: int | None = None) -> dict:
    """現在の資料フォルダの世代プローブ（`last_sig`／`last_synced_at`／`root_path`）。未登録（行なし）は `sig=""`。
    DB 例外は握りつぶさず呼び出し元（router）が 503 へ変換する。timeout は `store.get_world_status_row` へそのまま渡す。
    """
    row = store.get_world_status_row(world, connect_timeout=connect_timeout,
                                     statement_timeout_ms=statement_timeout_ms) or {}
    return {"sig": row.get("last_sig") or "", "synced_at": row.get("last_synced_at"),
            "root_path": row.get("root_path")}


def _resolved_root(root_path) -> bool:
    """`root_path` が今も到達可能な実ディレクトリか（symlink は拒否）。"""
    if not root_path:
        return False
    try:
        st = Path(root_path).stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode)


def _cached_view(world: str, sig: str, synced_at) -> dict | None:
    """`sig`／`synced_at` の両方が一致するキャッシュがあれば bundle dict を返す（無ければ None）。
    `sig` だけの一致では `A→""→A` の往復を見逃すため、`synced_at` との複合キーで世代を判定する。
    bundle の `files` は常に `None`（キャッシュヒット時は木を歩いていない）。
    """
    cached = _GRAPH_VIEW_CACHE.get(world)
    if sig and cached is not None and cached["sig"] == sig and cached["synced_at"] == synced_at:
        return {"raw_nodes": cached["raw_nodes"], "raw_edges": cached["raw_edges"],
                "raw_flags": cached["raw_flags"], "files": None, "sig": sig, "synced_at": synced_at}
    return None


class _GraphTooLarge(Exception):
    """組み立てる前の資料フォルダの列挙が、期限を超えたかファイルの件数が `GRAPH_PREVIEW_MAX_DOCS` を超えた。"""


def _build_full_view(world: str, status: dict) -> dict:
    """グラフを 1 回構築する。
    登録済みで `root_path` が到達可能なら、構築全体をその root に `pin_world_root` で固定する。戻り値 `resolved` が False の結果はキャッシュへ公開しない。
    pin 済み root から `safe_files` を 1 回だけ materialize し、構築と文書列挙・重要度解決・診断で使い回す。
    """
    resolved = _resolved_root(status["root_path"])

    def _do_build():
        wd = worlds.world_dir(world)
        try:
            files = list(si.safe_files(wd, deadline=si.user_op_deadline(), also=worlds.archives_dir(world))) if wd else []
        except si.ScopeWalkDeadlineExceeded as e:
            raise _GraphTooLarge from e
        if sum(1 for _, rel in files if not importance.is_importance_control_path(rel)) > GRAPH_PREVIEW_MAX_DOCS:
            raise _GraphTooLarge
        return files, _build(world, files=files)

    if resolved:
        with worlds.pin_world_root(world, status["root_path"]):
            files, (nodes, edges, flags) = _do_build()
    else:
        files, (nodes, edges, flags) = _do_build()
    return {"raw_nodes": nodes, "raw_edges": edges, "raw_flags": flags, "files": files, "resolved": resolved}


def _build_and_publish(world: str, status: dict) -> dict:
    """未キャッシュ時の構築＋（安全なら）公開。呼び出し元が `_GRAPH_VIEW_LOCK` を保持し、二重チェック（`_cached_view`）済みであること。
    `resolved=False` の結果と `sig` が空の間は公開しない。公開直前に世代を取り直し、構築開始時から動いていなければ書く（動いていれば捨てる）。
    """
    sig, synced_at = status["sig"], status["synced_at"]
    built = _build_full_view(world, status)
    resolved = built.pop("resolved")
    if sig and resolved:
        post = _current_world_status(world, connect_timeout=_LOCK_PROBE_TIMEOUT_S,
                                     statement_timeout_ms=_LOCK_PROBE_TIMEOUT_S * 1000)
        if post["sig"] == sig and post["synced_at"] == synced_at:
            _GRAPH_VIEW_CACHE[world] = {"sig": sig, "synced_at": synced_at,
                                        "raw_nodes": built["raw_nodes"], "raw_edges": built["raw_edges"],
                                        "raw_flags": built["raw_flags"]}
        else:
            _GRAPH_VIEW_CACHE.pop(world, None)
    built["sig"], built["synced_at"] = sig, synced_at
    return built


def _get_graph_bundle(world: str) -> dict:
    """`_GRAPH_VIEW_CACHE` のヒット確認〜single-flight 構築を 1 箇所に集約する。
    戻り値は生出力（`raw_nodes`／`raw_edges`／`raw_flags`）＋`files`（実構築時のみ非 None）＋`sig`／`synced_at`。
    `_GRAPH_VIEW_LOCK` を取ったら status を取り直し（有限 timeout）、二重チェックしてから構築する。
    構築の直前に取り込みの実行中かと署名を確かめ直し、実行中か署名が空で組み立てないときは `{"graph_note": 理由}` だけを返す。
    """
    status = _current_world_status(world)
    sig, synced_at = status["sig"], status["synced_at"]
    if not sig:
        _GRAPH_VIEW_CACHE.pop(world, None)

    hit = _cached_view(world, sig, synced_at)
    if hit is not None:
        return hit
    with _GRAPH_VIEW_LOCK:
        status = _current_world_status(world, connect_timeout=_LOCK_PROBE_TIMEOUT_S,
                                       statement_timeout_ms=_LOCK_PROBE_TIMEOUT_S * 1000)
        sig, synced_at = status["sig"], status["synced_at"]
        if not sig:
            _GRAPH_VIEW_CACHE.pop(world, None)
        hit = _cached_view(world, sig, synced_at)  # 待機中に他スレッドが公開したかもしれない
        if hit is not None:
            return hit
        note = _running_note(world, probe_timeout=_LOCK_PROBE_TIMEOUT_S)
        if note:
            return {"graph_note": note}
        if sig:
            try:
                return _build_and_publish(world, status)
            except _GraphTooLarge:
                return {"graph_note": _NOTE_GRAPH_TOO_MANY}
    # 守ること: 時間の上限の無い照会はロックの外で行う
    return {"graph_note": _NOTE_NOT_COMPLETED if store.get_latest_published_run_summary(world) is None else _NOTE_LAST_UNFINISHED}


def _running_note(world: str, probe_timeout: float | None = None) -> str | None:
    """最新の run が queued／extracting なら「取り込み中」の理由（平文）、そうでなければ None。"""
    probe = {} if probe_timeout is None else {"connect_timeout": probe_timeout, "statement_timeout_ms": probe_timeout * 1000}
    latest = store.get_latest_run_summary(world, **probe)
    return _NOTE_RUNNING if (latest or {}).get("status") in ("queued", "extracting") else None


def _hold_graph_note(world: str, sig: str) -> str | None:
    """グラフを組み立てない理由（平文）。組み立ててよいなら None。
    取り込み中（最新の run が queued／extracting）と、署名（`sig`）が空の資料フォルダでは組み立てない。
    署名が空でも、完了した取り込みの記録があれば「前回の取り込みが最後まで終わっていない」、記録が無ければ「まだ完了していない」を返す。
    """
    note = _running_note(world)
    if note:
        return note
    if not sig:
        return _NOTE_NOT_COMPLETED if store.get_latest_published_run_summary(world) is None else _NOTE_LAST_UNFINISHED
    return None


def _recorded_counts(world: str, doc_count: int) -> dict:
    """グラフを組み立てないときの件数。要素・関係は最後に完了した取り込みの記録（`published_snapshot`）から出し、記録が無い・廃止/隠しの内訳は None（不明）。"""
    published = store.get_latest_published_run_summary(world)
    snap = (published or {}).get("published_snapshot")
    snap = snap if isinstance(snap, dict) else {}
    nodes, edges = snap.get("nodes"), snap.get("edges")
    return {"entities": nodes if isinstance(nodes, int) else None,
            "relations": edges if isinstance(edges, int) else None,
            "deprecated": None, "hidden": None, "documents": doc_count}


GRAPH_PREVIEW_MAX_DOCS = 5000  # 台帳の件数がこれを超える資料フォルダでは、要素と関係を組み立てない

_NOTE_GRAPH_TOO_MANY = "資料が多いため、要素と関係の一覧は出しません。件数は最後に完了した取り込みの記録です。"
_NOTE_GRAPH_NO_COUNT = "資料の件数を確認できなかったため、要素と関係の一覧は出しません。件数は最後に完了した取り込みの記録です。"
_NOTE_LIST_PENDING = "まだ取り込みが終わっていないため、資料の一覧は取り込みのあとに出します。"

_STATE_LABELS = {"ready": "使えます", "unreadable": "読み取れません", "unknown": "状態を確認できませんでした"}


class _LedgerView(NamedTuple):
    """台帳から作った資料の一覧の元（`rows`＝(状態以外の行, 台帳の status)）と、その資料のフォルダ（祖先込み）。作ったあとは変えない。"""
    rows: list
    folders: frozenset


# 資料フォルダごとの台帳由来の一覧の元。鍵＝`_view_key`で、変われば作り直す。
_LEDGER_VIEW_CACHE: dict[str, tuple[tuple, _LedgerView]] = {}
_LEDGER_VIEW_LOCKS: dict[str, threading.Lock] = {}
_LEDGER_VIEW_LOCKS_GUARD = threading.Lock()


def _world_lock(locks: dict, world: str) -> threading.Lock:
    with _LEDGER_VIEW_LOCKS_GUARD:
        return locks.setdefault(world, threading.Lock())


def _with_ancestors(folders) -> frozenset:
    """フォルダの rel_path 群 → 祖先を含む集合（root 直下の `""` は含めない）。"""
    out: set[str] = set()
    for f in folders:
        while f and f not in out:
            out.add(f)
            f = f.rpartition("/")[0]
    return frozenset(out)


def _ledger_row_view(r: dict) -> tuple[dict, str | None]:
    """台帳の 1 行 → (状態以外の画面の行, 台帳の status)。範囲メタは名前から導き、重要度は台帳の列をそのまま通す。派生 MD があれば読み取り方法の要約（`provenance`）を付ける。"""
    name = r["name"]
    meta = si.rel_scope_meta(name)
    out = {"name": name, "doctype": r.get("doctype"), "branch": r.get("branch"), "analyzer": None,
           "top_scope": meta["top_scope"], "phase": meta["phase"], "category": meta["category"],
           "folder": "/".join(name.split("/")[:-1])}
    if r.get("importance") is not None:
        out["importance"] = r["importance"]
        out["importance_source"] = r.get("importance_source")
        if r.get("importance_reason"):
            out["importance_reason"] = r["importance_reason"]
    prov = corpus_docs.provenance_summary(r.get("md_path"))
    if prov:
        out["provenance"] = prov
    return out, r.get("status")


def _read_ledger_view(world: str) -> _LedgerView | None:
    """台帳の全行から一覧の元を作る。台帳が空なら None。秘匿名と重要度の設定ファイルは出さない。"""
    with store.world_swap_lock_shared(world, timeout_ms=doc_ledger.SWAP_LOCK_WAIT_MS):
        total, ledger = store.count_and_list_documents(world, limit=None)
    if total == 0:
        return None
    rows = [_ledger_row_view(r) for r in ledger
            if not text_kind.is_sensitive_doc_id(r["name"]) and not importance.is_importance_control_path(r["name"])]
    return _LedgerView(rows, _with_ancestors({row["folder"] for row, _ in rows}))


def _view_key(world: str) -> tuple:
    """台帳ビューの鍵＝(台帳の変更印, 最新の取り込みの記録の id と状態)。"""
    latest = store.get_latest_run_summary(world) or {}
    return store.ledger_stamp(world), (latest.get("id"), latest.get("status"))


def _ledger_view(world: str) -> _LedgerView | None:
    """台帳があれば資料フォルダごとに 1 回だけ作って使い回し、台帳か最新の取り込みの記録が変わったら作り直す。台帳が空なら None。
    ① 鍵（`_view_key`）を引く（引けない→キャッシュせず作る） ② 同じ鍵のキャッシュがあれば返す ③ 無ければ資料フォルダごとのロックで 1 本だけ、鍵を引き直してから作る。
    守ること: 鍵は台帳の全件読みより先に引く（古い鍵に新しい中身が付くのは許すが、逆は許さない）。
    """
    try:
        stamp = _view_key(world)
    except Exception:
        _LEDGER_VIEW_CACHE.pop(world, None)
        return _read_ledger_view(world)
    if not stamp[0][0]:
        _LEDGER_VIEW_CACHE.pop(world, None)
        return None
    hit = _LEDGER_VIEW_CACHE.get(world)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    with _world_lock(_LEDGER_VIEW_LOCKS, world):
        try:
            stamp = _view_key(world)
        except Exception:
            _LEDGER_VIEW_CACHE.pop(world, None)
            return _read_ledger_view(world)
        if not stamp[0][0]:
            _LEDGER_VIEW_CACHE.pop(world, None)
            return None
        hit = _LEDGER_VIEW_CACHE.get(world)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        view = _read_ledger_view(world)
        if view is None:
            _LEDGER_VIEW_CACHE.pop(world, None)
            return None
        _LEDGER_VIEW_CACHE[world] = (stamp, view)
        return view


def _row_with_state(row: dict, status: str | None, blocked) -> dict:
    """台帳の status と直近の取り込みの blocked（`{doc: reason}`・確認できなければ None）から、画面の行の `state`／`label`／`reason` を決める。
    確認できないときは、ソース枝の indexed を「状態を確認できませんでした」にする（黙って「使えます」にしない）。
    """
    state, reason = ("unreadable" if status == "unreadable" else "ready"), None
    if blocked is None:
        if row["branch"] == "source" and status == "indexed":
            state = "unknown"
    else:
        reason = blocked.get(row["name"])
        if reason:
            state = "unreadable"
    return {**row, "state": state, "label": _STATE_LABELS[state], "reason": reason}


def _preview_documents(world: str, root, sig: str) -> tuple[list, frozenset, str | None]:
    """画面の資料の一覧と、その資料のフォルダ、一覧を出せない理由。
    台帳があれば台帳だけから出す（資料フォルダは歩かない）。台帳が空（一度も取り込んでいない）ときだけ、期限つきで資料フォルダを歩いて分類する。
    期限を超えたときは一覧を空にして理由（平文）を返す。
    """
    view = _ledger_view(world)
    if view is not None:
        blocked = corpus_docs.last_run_blocked_docs(world)
        return [_row_with_state(row, status, blocked) for row, status in view.rows], view.folders, None
    if not root:
        return [], frozenset(), None
    try:
        files = list(si.safe_files(root, deadline=si.user_op_deadline(), also=worlds.archives_dir(world)))
    except si.ScopeWalkDeadlineExceeded:
        return [], frozenset(), _NOTE_LIST_PENDING
    docs = doc_ledger.preview_documents(world, root=root, files=files, sig=sig or None)
    return docs, _with_ancestors({d["folder"] for d in docs}), None


FOLDERS_MAX = 5000  # 資料の画面のツリーに出すフォルダの上限（超えたら `folders_truncated`）
_DEADLINE_CHECK_ENTRIES = 256  # フォルダ内のエントリを数えながら期限を再確認する間隔


def _scan_tree(top, deadline: float, *, listing: bool, out: dict) -> bool:
    """`top` 配下を 1 回歩き、フォルダ（`listing` のとき）と重要度の設定ファイルの診断を `out` に足す。期限を超えたら歩くのをやめて False（歩き終えたら True）。
    シンボリックリンクは辿らない・版管理の記録のフォルダには入らない・読めないフォルダは飛ばす。
    フォルダの一覧には名前が `.` で始まるフォルダとその配下を出さない（重要度の設定ファイルは、そこでも探す）。
    """
    top = Path(top)
    try:
        if not stat.S_ISDIR(os.lstat(top).st_mode):
            return True
        top_resolved = top.resolve()
    except OSError:
        out["root_unreadable"] = True
        return True
    stack = [(top, True)]
    while stack:
        if time.monotonic() > deadline:
            return False
        current, listed = stack.pop()
        try:
            entries = []
            with os.scandir(current) as it:
                for i, e in enumerate(it):
                    if i and i % _DEADLINE_CHECK_ENTRIES == 0 and time.monotonic() > deadline:
                        return False
                    entries.append(e)
            entries.sort(key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if si.is_vcs_dir_name(e.name):
                        continue
                    child_listed = listed and not e.name.startswith(".")
                    if listing and child_listed:
                        if len(out["folders"]) >= FOLDERS_MAX:
                            out["folders_truncated"] = True
                        else:
                            out["folders"].append(Path(e.path).relative_to(top).as_posix())
                    stack.append((Path(e.path), child_listed))
                elif importance.is_importance_control_path(e.name) and e.is_file(follow_symlinks=False):
                    p = Path(e.path)
                    if text_kind.is_noise(p.name, p.suffix.lower()):
                        continue
                    rp = p.resolve()
                    if rp.is_relative_to(top_resolved):
                        _rules, diags = importance.parse_control_file(rp, config_rel=p.relative_to(top).as_posix())
                        out["diagnostics"].extend(dataclasses.asdict(d) for d in diags)
            except OSError:
                continue
    return True


def scan_folders_and_diagnostics(world: str, root, deadline: float) -> dict:
    """資料フォルダを 1 回歩き、フォルダの木と重要度の設定ファイルの診断をまとめて集める。期限を超えたらそこまでの結果を `partial=True` で返す。
    戻り値: `{"folders": 並べたフォルダ, "folders_truncated": 上限で打ち切ったか, "partial": 期限で止まったか, "diagnostics": [...], "root_unreadable": 読めなかったか}`。
    フォルダの木は `root` だけ（アーカイブの展開先は出さない）。診断は展開先の設定ファイルも対象にする。
    """
    out = {"folders": [], "folders_truncated": False, "diagnostics": [], "root_unreadable": False}
    complete = _scan_tree(root, deadline, listing=True, out=out)
    archives = worlds.archives_dir(world)
    if complete and archives is not None and Path(archives).is_dir():
        complete = _scan_tree(archives, deadline, listing=False, out=out)
    out["folders"] = sorted(out["folders"])
    out["partial"] = not complete or out["root_unreadable"]
    return out


# 資料フォルダごとの走査結果。鍵＝資料フォルダの世代（`sig`＋`synced_at`）。途中で止まった結果も同じ世代の間は使い回す。
_SCAN_CACHE: dict[str, tuple[tuple, dict]] = {}
_SCAN_LOCKS: dict[str, threading.Lock] = {}


def _folders_and_diagnostics(world: str, root, status: dict) -> dict:
    """`scan_folders_and_diagnostics` を世代ごとに 1 回だけ行って使い回す（`sig` が空の間と、`root` を読めなかったときは使い回さない）。
    同じ世代の走査は資料フォルダごとのロックで 1 本にする。期限は `scope_infer.user_op_deadline()`。
    """
    if not root:
        return {"folders": [], "folders_truncated": False, "partial": False, "diagnostics": [], "root_unreadable": False}
    key = (status["sig"], status["synced_at"])
    cacheable = bool(status["sig"])
    if cacheable:
        hit = _SCAN_CACHE.get(world)
        if hit is not None and hit[0] == key:
            return hit[1]
    with _world_lock(_SCAN_LOCKS, world):
        if cacheable:
            hit = _SCAN_CACHE.get(world)
            if hit is not None and hit[0] == key:
                return hit[1]
        scan = scan_folders_and_diagnostics(world, root, si.user_op_deadline())
        if cacheable and not scan["root_unreadable"]:
            _SCAN_CACHE[world] = (key, scan)
        return scan


def build_preview(world: str | None = None) -> dict:
    """抽出プレビュー（読み取り専用）。資料の一覧・フォルダ・重要度の診断と、取り込みの記録の件数を返す。要素・関係は返さない（`build_preview_graph`）。
    資料の一覧は台帳から出し（資料フォルダの世代ごとに使い回す）、台帳が空のときだけ期限つきで資料フォルダを歩く（期限を超えたら一覧は空にして `documents_note` に理由を返す）。
    フォルダの木と重要度の診断は期限つきの 1 回の走査で集める（止まったら `folders_partial`）。台帳の資料のフォルダは必ず木に入れる。
    `graph_note` は要素・関係を今は組み立てられない理由（取り込み中など）で、組み立てられるなら None。
    """
    world = world or worlds.default_world()
    status = _current_world_status(world)
    sig = status["sig"]
    wd = worlds.world_dir(world)
    preview_docs, doc_folders, docs_note = _preview_documents(world, wd, sig)
    scan = _folders_and_diagnostics(world, wd, status)
    return {"world": world, "label": worlds.world_label(world),
            "counts": _recorded_counts(world, len(preview_docs)),
            "documents": preview_docs, "documents_note": docs_note,
            # `folders`: 資料の画面のツリー用のフォルダの一覧（資料の無いフォルダも含む）
            "folders": sorted(set(scan["folders"]) | doc_folders),
            "folders_truncated": scan["folders_truncated"], "folders_partial": scan["partial"],
            "issues": [], "entities": [], "relations": [],
            "graph_note": _hold_graph_note(world, sig),
            # `importance_diagnostics`: 重要度設定ファイルの構文診断（`issues` はグラフ構築の警告で別物）
            "importance_diagnostics": scan["diagnostics"]}


def build_preview_graph(world: str | None = None) -> dict:
    """資料フォルダのグラフ（要素・関係・件数）。画面が「関係を確認」を押したときだけ呼ぶ。
    取り込み中・一度も完了していない資料フォルダ・台帳の件数が `GRAPH_PREVIEW_MAX_DOCS` を超える資料フォルダ（件数を引けないときも）では
    組み立てず、`graph_note` に理由を平文で返す（要素・関係は空・件数は取り込みの記録から）。組み立てるときは `_get_graph_bundle`（世代キャッシュ）を使う。
    """
    world = world or worlds.default_world()
    sig = _current_world_status(world)["sig"]
    try:
        doc_count = store.ledger_stamp(world)[0]
    except Exception:
        doc_count = None
    note = _hold_graph_note(world, sig)
    if note is None and doc_count is None:
        note = _NOTE_GRAPH_NO_COUNT
    elif note is None and doc_count > GRAPH_PREVIEW_MAX_DOCS:
        note = _NOTE_GRAPH_TOO_MANY
    bundle = None if note else _get_graph_bundle(world)
    if bundle is not None and bundle.get("graph_note"):
        note, bundle = bundle["graph_note"], None
    if bundle is None:
        return {"world": world, "counts": _recorded_counts(world, doc_count or 0),
                "entities": [], "relations": [], "issues": [], "graph_note": note}
    entities, relations = _preview_entities_relations(bundle["raw_nodes"], bundle["raw_edges"])
    return {"world": world, "counts": _counts(bundle["raw_nodes"], bundle["raw_edges"], doc_count=doc_count),
            "entities": entities, "relations": relations, "issues": bundle["raw_flags"], "graph_note": None}
