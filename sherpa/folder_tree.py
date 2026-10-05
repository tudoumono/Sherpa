"""`folder_tree` ツール本体: world のフォルダ階層を深さ上限つきで俯瞰する、LLM・索引を使わない決定的な木。

`doc_ledger` が返す rel_path 一覧から、フォルダ単位で直下/再帰ファイル数・直下サブフォルダ数を集計するだけで、
フォルダ名の意味解釈はしない（クエリ時にエージェントが解釈する）。
"""
from __future__ import annotations

from . import doc_ledger
from . import layer as layer_mod
from . import scope as scope_mod

# フォルダ列挙件数の安全弁。超過分は打ち切り、`folders_truncated` で申告する
_MAX_FOLDERS = 500

# `tool_result_max_bytes` が省略されたときの既定（`agentic_search.TOOL_RESULT_MAX_BYTES` と同値・循環 import 回避のため複製）
_DEFAULT_TOOL_RESULT_MAX_BYTES = 262144

_DEPTH_DEFAULT = 3
_DEPTH_MIN = 1
_DEPTH_MAX = 10


def _clamp_depth(raw) -> int:
    try:
        d = int(raw)
    except (TypeError, ValueError):
        return _DEPTH_DEFAULT
    return max(_DEPTH_MIN, min(d, _DEPTH_MAX))


def build(world: str, args: dict, *, scope_paths=None, deadline: float | None = None, layer=None,
         tool_result_max_bytes: int | None = None) -> dict:
    """`folder_tree` ツール本体。

    `path_prefix` 配下・`depth`（省略時3・1〜10 にクランプ）までの各フォルダについて、パス・直下ファイル数・配下（再帰）
    ファイル数・直下サブフォルダ数を返す。`scope_paths` と `layer`（`list_docs` と同じ確定判定）は硬いフィルタ。
    `folders` は件数上限と `tool_result_max_bytes`（`path` の累積バイト）で打ち切る。

    戻り値: `{"path_prefix", "depth", "count", "folders": [...], "folders_truncated"}`。`folders` の各要素:
    `{"path", "depth", "direct_files", "total_files", "subfolders", "truncated"}`。要素の `truncated` は深さ上限で配下を
    表示していない意味、`folders_truncated` は件数/バイトの安全弁で一部フォルダを返していない意味（別の事実）。
    `count` は打ち切り前の総フォルダ数。
    """
    args = args or {}
    prefix = str(args.get("path_prefix") or "").strip().strip("/")
    depth = _clamp_depth(args.get("depth"))
    sp = scope_mod.normalize_scope_paths(scope_paths) or None
    tr_max_bytes = (tool_result_max_bytes if tool_result_max_bytes is not None
                   else _DEFAULT_TOOL_RESULT_MAX_BYTES)

    rows = doc_ledger.documents_for(world, deadline=deadline)
    base_parts = prefix.split("/") if prefix else []
    base_depth = len(base_parts)

    # フォルダ集計: {folder_parts: {"direct", "total", "children"}}。集計は depth でクランプせず、出力時にだけ絞る
    agg: dict = {}
    for r in rows:
        rel = r.get("name")
        if not rel:
            continue
        if not scope_mod.in_scope(rel, sp):
            continue
        if prefix and not scope_mod.in_scope(rel, [prefix]):
            continue
        if not layer_mod.in_layer_code(r.get("branch") == "source", layer):
            continue
        dir_parts = rel.split("/")[:-1]  # ファイル名を除いたフォルダ階層
        if len(dir_parts) <= base_depth:
            continue
        for d in range(base_depth + 1, len(dir_parts) + 1):
            key = tuple(dir_parts[:d])
            entry = agg.setdefault(key, {"direct": 0, "total": 0, "children": set()})
            entry["total"] += 1
            if d == len(dir_parts):
                entry["direct"] += 1
            else:
                entry["children"].add(dir_parts[d])

    within_depth = {k: v for k, v in agg.items() if len(k) - base_depth <= depth}
    ordered_keys = sorted(within_depth.keys())
    total_count = len(ordered_keys)

    folders = []
    cum_bytes = 0
    for key in ordered_keys:
        if len(folders) >= _MAX_FOLDERS:
            break
        path = "/".join(key)
        # バイト予算はエントリの `path` の累積バイト数で判定する
        item_bytes = len(path.encode("utf-8"))
        if cum_bytes + item_bytes > tr_max_bytes:
            break
        entry = within_depth[key]
        rel_depth = len(key) - base_depth
        folders.append({
            "path": path,
            "depth": rel_depth,
            "direct_files": entry["direct"],
            "total_files": entry["total"],
            "subfolders": len(entry["children"]),
            "truncated": rel_depth == depth and bool(entry["children"]),
        })
        cum_bytes += item_bytes

    return {
        "path_prefix": prefix,
        "depth": depth,
        "count": total_count,
        "folders": folders,
        "folders_truncated": total_count > len(folders),
    }
