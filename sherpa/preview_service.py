"""取り込み・抽出プレビュー（読み取り専用）。資料フォルダのグラフ（`world_graph.build_world`）を画面が描ける形
（エンティティ／関係／状態＋件数）に整形する。Neo4j は使わず、書き込みもしない。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from pathlib import Path

from . import doc_ledger, scope_infer as si, store, worlds
from .ingest import world_graph_service

# 種別ラベル → 表示の日本語
_TYPE_JA = {
    "Module": "プログラム", "Copybook": "コピーブック", "DataItem": "項目",
    "Document": "文書", "Batch": "バッチ", "Table": "テーブル", "Config": "設定",
}


def _build(world: str, *, files=None):
    """有効グラフの構築を共通入口（worker と同じ）へ委譲する。`files` は `build_effective_world` へそのまま渡す（materialize 済み list）。"""
    if not worlds.world_dir(world):
        return [], [], []
    return world_graph_service.build_effective_world(world, files=files)


def _counts(nodes, edges, world, *, doc_count: int | None = None) -> dict:
    """件数サマリ。`doc_count` を渡すと文書一覧の再走査をしない。"""
    return {
        "entities": len(nodes),
        "relations": len(edges),
        "deprecated": sum(1 for n in nodes if n.get("status", "active") == "deprecated"),
        "hidden": sum(1 for n in nodes if n.get("status", "active") == "hidden_candidate"),
        "documents": doc_count if doc_count is not None else len(doc_ledger.documents_for(world)),
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


def _graph_signature(payload: dict) -> str:
    """応答本体（`signature` を除く全フィールド）を丸ごと署名する決定的な値（ETag 用）。
    nodes／edges は各要素を正規化 JSON にして sorted（並び順に依存しない）。
    """
    canon = dict(payload)
    for key in ("nodes", "edges"):
        if key in canon:
            canon[key] = sorted(json.dumps(x, sort_keys=True, ensure_ascii=True, default=str) for x in canon[key])
    blob = json.dumps(canon, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _select_top_nodes(nodes, edges, limit):
    """次数上位 `limit` ノードと、その間の辺だけに絞る。決定的（次数降順→名前昇順→id 昇順）。
    `limit` が None／0 以下、または全件が収まる場合は truncated=False で素通し。返値 `(nodes, edges, truncated)`。
    """
    total = len(nodes)
    if not limit or limit <= 0 or total <= limit:
        return nodes, edges, False
    deg = {n["id"]: 0 for n in nodes}
    for e in edges:
        if e["source"] in deg:
            deg[e["source"]] += 1
        if e["target"] in deg:
            deg[e["target"]] += 1
    ranked = sorted(nodes, key=lambda n: (-deg[n["id"]], n.get("name") or "", n["id"]))
    keep = {n["id"] for n in ranked[:limit]}
    kept_nodes = [n for n in nodes if n["id"] in keep]
    kept_edges = [e for e in edges if e["source"] in keep and e["target"] in keep]
    return kept_nodes, kept_edges, True


# 資料フォルダ → limit 適用前の全体 view（プロセス内キャッシュ・単一 worker 前提）。
# `build_preview` と共有する。キャッシュ対象はグラフ構築のみ（文書一覧・重要度・診断は毎回計算する）
_GRAPH_VIEW_CACHE: dict[str, dict] = {}

# miss 時の構築を 1 本のロックで直列化する（single-flight）
_GRAPH_VIEW_LOCK = threading.Lock()

# `_GRAPH_VIEW_LOCK` 保持中の DB プローブに課す有限 timeout（秒）
_LOCK_PROBE_TIMEOUT_S = 5


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
        return {"out_nodes": cached["out_nodes"], "out_edges": cached["out_edges"], "counts": cached["counts"],
                "total_nodes": cached["total_nodes"], "total_edges": cached["total_edges"],
                "signature": cached["signature"], "raw_nodes": cached["raw_nodes"],
                "raw_edges": cached["raw_edges"], "raw_flags": cached["raw_flags"],
                "files": None, "sig": sig, "synced_at": synced_at}
    return None


def _build_full_view(world: str, status: dict, *, need_files: bool = False) -> dict:
    """limit 適用前の全体 view を 1 回構築する。
    登録済みで `root_path` が到達可能なら、構築全体をその root に `pin_world_root` で固定する。戻り値 `resolved` が False の結果はキャッシュへ公開しない。
    `need_files=True`（`build_preview` 用）は pin 済み root から `safe_files` を 1 回だけ materialize し、構築と文書列挙・重要度解決・診断で使い回す。
    """
    resolved = _resolved_root(status["root_path"])
    files = None
    wd_used = None

    def _do_build():
        nonlocal files, wd_used
        if need_files:
            wd_used = worlds.world_dir(world)
            files = list(si.safe_files(wd_used, also=worlds.archives_dir(world))) if wd_used else []
            return _build(world, files=files)
        return _build(world)

    if resolved:
        with worlds.pin_world_root(world, status["root_path"]):
            nodes, edges, flags = _do_build()
    else:
        nodes, edges, flags = _do_build()
    by_cid = {n["cid"]: n for n in nodes}
    out_nodes = [{"id": n["cid"], "name": n["name"], "type": n["label"],
                  "type_ja": _TYPE_JA.get(n["label"], n["label"]),
                  "status": n.get("status", "active"),
                  "value": n.get("value"), "top_scope": n.get("top_scope"), "path": n.get("path")}
                 for n in nodes]
    ids = set(by_cid)
    out_edges = [{"source": e["src"], "target": e["dst"], "type": e["type"],
                  "status": e.get("status", "active")}
                 for e in edges if e["src"] in ids and e["dst"] in ids]
    # `files` があれば、それで documents 件数を数える（`counts` は共有キャッシュに入るため正確な件数が要る）
    doc_count = len(doc_ledger.documents_for(world, root=wd_used, files=files)) if files is not None else None
    counts = _counts(nodes, edges, world, doc_count=doc_count)  # 同じ build を使い回す
    total_nodes, total_edges = len(out_nodes), len(out_edges)
    # 署名は limit 適用前（全体）の応答本体を丸ごと対象にする
    full_payload = {"world": world, "counts": counts, "nodes": out_nodes, "edges": out_edges,
                    "total_nodes": total_nodes, "total_edges": total_edges, "truncated": False}
    signature = _graph_signature(full_payload)
    return {"out_nodes": out_nodes, "out_edges": out_edges, "counts": counts,
            "total_nodes": total_nodes, "total_edges": total_edges, "signature": signature,
            "raw_nodes": nodes, "raw_edges": edges, "raw_flags": flags,
            "files": files, "resolved": resolved}


def _build_and_publish(world: str, status: dict, *, need_files: bool = False) -> dict:
    """未キャッシュ時の構築＋（安全なら）公開。呼び出し元が `_GRAPH_VIEW_LOCK` を保持し、二重チェック（`_cached_view`）済みであること。
    `resolved=False` の結果と `sig` が空の間は公開しない。公開直前に世代を取り直し、構築開始時から動いていなければ書く（動いていれば捨てる）。
    """
    sig, synced_at = status["sig"], status["synced_at"]
    built = _build_full_view(world, status, need_files=need_files)
    resolved = built.pop("resolved")
    if sig and resolved:
        post = _current_world_status(world, connect_timeout=_LOCK_PROBE_TIMEOUT_S,
                                     statement_timeout_ms=_LOCK_PROBE_TIMEOUT_S * 1000)
        if post["sig"] == sig and post["synced_at"] == synced_at:
            _GRAPH_VIEW_CACHE[world] = {"sig": sig, "synced_at": synced_at,
                                        "out_nodes": built["out_nodes"], "out_edges": built["out_edges"],
                                        "counts": built["counts"], "total_nodes": built["total_nodes"],
                                        "total_edges": built["total_edges"], "signature": built["signature"],
                                        "raw_nodes": built["raw_nodes"], "raw_edges": built["raw_edges"],
                                        "raw_flags": built["raw_flags"]}
        else:
            _GRAPH_VIEW_CACHE.pop(world, None)
    built["sig"], built["synced_at"] = sig, synced_at
    return built


def _get_graph_bundle(world: str, *, need_files: bool = False) -> dict:
    """`_GRAPH_VIEW_CACHE` のヒット確認〜single-flight 構築を 1 箇所に集約する（`graph_view`・`build_preview` 共有）。
    戻り値は変換済み view（`out_nodes`／`out_edges`／`counts`／`total_*`／`signature`）＋生出力（`raw_*`）＋`files`（実構築時のみ非 None）＋`sig`／`synced_at`。
    `_GRAPH_VIEW_LOCK` を取ったら status を取り直し（有限 timeout）、二重チェックしてから構築する。
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
        return _build_and_publish(world, status, need_files=need_files)


def graph_view(world=None, limit=None) -> dict:
    """ナレッジグラフを可視化用（nodes／edges）に整形する（読み取り専用）。
    `limit`（None／0 以下＝全件）指定時は次数上位のノードだけに絞り、`total_nodes`／`total_edges`／`truncated` を返す。
    `signature` と `counts` は limit に依存しない全体の値。構築は世代が変わらない限りキャッシュを使い、`limit` の絞り込みは毎回行う。
    """
    world = world or worlds.default_world()
    bundle = _get_graph_bundle(world)
    view_nodes, view_edges, truncated = _select_top_nodes(bundle["out_nodes"], bundle["out_edges"], limit)
    return {"world": world, "nodes": view_nodes, "edges": view_edges, "counts": bundle["counts"],
            "total_nodes": bundle["total_nodes"], "total_edges": bundle["total_edges"],
            "truncated": truncated, "signature": bundle["signature"]}


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


def build_preview(world: str | None = None) -> dict:
    """抽出プレビュー（読み取り専用）。エンティティ／関係／状態と件数サマリを返す。
    グラフ部分は `graph_view` と同じキャッシュを共有する。文書一覧・重要度解決・重要度診断はキャッシュせず毎回計算する。
    cache miss 時は bundle の `files` を使い回して木を 1 回だけ歩き、cache hit 時は文書一覧のために `safe_files` を 1 回歩く。
    """
    world = world or worlds.default_world()
    bundle = _get_graph_bundle(world, need_files=True)
    entities, relations = _preview_entities_relations(bundle["raw_nodes"], bundle["raw_edges"])

    wd = worlds.world_dir(world)
    if wd:
        files = bundle["files"] if bundle["files"] is not None else list(
            si.safe_files(wd, also=worlds.archives_dir(world)))
        # 世代の `sig`（未登録は空文字）を重要度解決へ渡し、署名のための再走査を避ける
        preview_docs = doc_ledger.preview_documents(world, root=wd, files=files, sig=bundle["sig"] or None)
        diagnostics = doc_ledger.control_diagnostics(world, root=wd, files=files)
        folders, folders_truncated = list_folders(wd)
    else:
        preview_docs, diagnostics = [], []
        folders, folders_truncated = [], False

    return {"world": world, "label": worlds.world_label(world),
            "counts": _counts(bundle["raw_nodes"], bundle["raw_edges"], world, doc_count=len(preview_docs)),
            "documents": preview_docs,
            # `folders`: 資料の画面のツリー用のフォルダの一覧（資料の無いフォルダも含む）
            "folders": folders, "folders_truncated": folders_truncated,
            "issues": bundle["raw_flags"], "entities": entities, "relations": relations,
            # `importance_diagnostics`: 重要度設定ファイルの構文診断（`issues` はグラフ構築の警告で別物）
            "importance_diagnostics": diagnostics}
