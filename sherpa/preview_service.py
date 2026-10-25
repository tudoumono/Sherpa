"""取り込み・抽出プレビュー（読み取り専用）。資料の一覧・フォルダ・重要度の診断と、資料フォルダのグラフ
（`world_graph.build_world`）の要素・関係・件数を画面が描ける形に整形する。Neo4j は使わず、書き込みもしない。
取り込み中・一度も完了していない資料フォルダではグラフを組み立てず、件数は取り込みの記録から出す。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

from . import doc_ledger, scope_infer as si, store, worlds
from .ingest import world_graph_service


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


def _build_full_view(world: str, status: dict) -> dict:
    """グラフを 1 回構築する。
    登録済みで `root_path` が到達可能なら、構築全体をその root に `pin_world_root` で固定する。戻り値 `resolved` が False の結果はキャッシュへ公開しない。
    pin 済み root から `safe_files` を 1 回だけ materialize し、構築と文書列挙・重要度解決・診断で使い回す。
    """
    resolved = _resolved_root(status["root_path"])

    def _do_build():
        wd = worlds.world_dir(world)
        files = list(si.safe_files(wd, also=worlds.archives_dir(world))) if wd else []
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
            return _build_and_publish(world, status)
    # 守ること: 時間の上限の無い照会はロックの外で行う
    return {"graph_note": _NOTE_NOT_COMPLETED if store.get_latest_published_run_summary(world) is None else _NOTE_LAST_UNFINISHED}


FOLDERS_MAX = 5000  # 資料の画面のツリーに出すフォルダの上限（超えたら `folders_truncated`）


def list_folders(root) -> tuple[list[str], bool]:
    """`root` 配下のフォルダ（中身が空のものも含む）の相対パスを並べて返す。戻り値は `(フォルダ, 上限で打ち切ったか)`。
    シンボリックリンクは辿らない・名前が `.` で始まるフォルダと版管理の記録のフォルダ（`CVS` など）は出さない・読めないフォルダは飛ばす。
    """
    root = Path(root)
    out: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted((e for e in it), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink() or not e.is_dir(follow_symlinks=False) or e.name.startswith(".") or si.is_vcs_dir_name(e.name):
                    continue
            except OSError:
                continue
            if len(out) >= FOLDERS_MAX:
                return sorted(out), True
            p = Path(e.path)
            out.append(p.relative_to(root).as_posix())
            stack.append(p)
    return sorted(out), False


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


def build_preview(world: str | None = None) -> dict:
    """抽出プレビュー（読み取り専用）。資料の一覧・フォルダ・重要度の診断と、要素／関係／件数サマリを返す。
    取り込み中・一度も完了していない資料フォルダではグラフを組み立てず、`graph_note` に理由を平文で返す（要素・関係は空・件数は取り込みの記録から）。
    組み立てるときは世代（`last_sig`＋`last_synced_at`）が変わらない間 `_GRAPH_VIEW_CACHE` を使う。文書一覧・重要度解決・重要度診断はキャッシュせず毎回計算する。
    cache miss 時は bundle の `files` を使い回して木を 1 回だけ歩き、cache hit・組み立てないときは文書一覧のために `safe_files` を 1 回歩く。
    """
    world = world or worlds.default_world()
    sig = _current_world_status(world)["sig"]
    note = _hold_graph_note(world, sig)
    bundle = None if note else _get_graph_bundle(world)
    if bundle is not None and bundle.get("graph_note"):
        note, bundle = bundle["graph_note"], None
    if bundle is not None:
        sig = bundle["sig"]
        entities, relations = _preview_entities_relations(bundle["raw_nodes"], bundle["raw_edges"])
    else:
        entities, relations = [], []

    wd = worlds.world_dir(world)
    if wd:
        files = bundle["files"] if bundle is not None and bundle["files"] is not None else list(
            si.safe_files(wd, also=worlds.archives_dir(world)))
        # 世代の `sig`（未登録は空文字）を重要度解決へ渡し、署名のための再走査を避ける
        preview_docs = doc_ledger.preview_documents(world, root=wd, files=files, sig=sig or None)
        diagnostics = doc_ledger.control_diagnostics(world, root=wd, files=files)
        folders, folders_truncated = list_folders(wd)
    else:
        preview_docs, diagnostics = [], []
        folders, folders_truncated = [], False

    if bundle is not None:
        counts = _counts(bundle["raw_nodes"], bundle["raw_edges"], doc_count=len(preview_docs))
    else:
        counts = _recorded_counts(world, len(preview_docs))
    return {"world": world, "label": worlds.world_label(world),
            "counts": counts,
            "documents": preview_docs,
            # `folders`: 資料の画面のツリー用のフォルダの一覧（資料の無いフォルダも含む）
            "folders": folders, "folders_truncated": folders_truncated,
            "issues": bundle["raw_flags"] if bundle is not None else [],
            "entities": entities, "relations": relations,
            # `graph_note`: 要素・関係を組み立てていない理由（組み立てたときは None）
            "graph_note": note,
            # `importance_diagnostics`: 重要度設定ファイルの構文診断（`issues` はグラフ構築の警告で別物）
            "importance_diagnostics": diagnostics}
