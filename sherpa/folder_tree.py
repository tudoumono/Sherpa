"""`folder_tree` ツール本体: world のフォルダ階層を深さ上限つきで俯瞰する、LLM・索引を使わない決定的な木。

`doc_ledger.listing_documents` が返す rel_path 一覧から、フォルダ単位で直下/再帰ファイル数・直下サブフォルダ数を集計するだけで、
フォルダ名の意味解釈はしない（クエリ時にエージェントが解釈する）。
"""
from __future__ import annotations

import json

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
         tool_result_max_bytes: int | None = None, rules=None) -> dict:
    """`folder_tree` ツール本体。管理者の除外（`rules`）の下のファイルは数えない。

    `path_prefix` 配下・`depth`（省略時3・1〜10 にクランプ）までの各フォルダについて、パス・直下ファイル数・配下（再帰）
    ファイル数・直下サブフォルダ数を返す。`scope_paths` と `layer`（`list_docs` と同じ確定判定）は硬いフィルタ。
    `folders` は `path` の昇順固定で、`offset`（省略時 0）から件数上限と `tool_result_max_bytes`（要素ごとの JSON バイトの累積）まで返す。
    1 ページに最低 1 件は入れる（続きの位置が進まなくなるのを防ぐ）。

    戻り値: `{"path_prefix", "depth", "count", "offset", "folders": [...], "folders_truncated", "next_offset"}`。
    `next_offset` は残りがあるときだけ int（`offset` に渡すと続きが取れる）、無ければ None。`folders` の各要素:
    `{"path", "depth", "direct_files", "total_files", "subfolders", "truncated"}`。要素の `truncated` は深さ上限で配下を
    表示していない意味、`folders_truncated` は件数/バイトの安全弁で一部フォルダを返していない意味（別の事実）。
    `count` は打ち切り前の総フォルダ数。
    一覧を最後の取り込みの台帳から出したときだけ `ledger_at`（その取り込みの時点・ISO 8601）を付ける。
    """
    args = args or {}
    prefix = str(args.get("path_prefix") or "").strip().strip("/")
    depth = _clamp_depth(args.get("depth"))
    sp = scope_mod.normalize_scope_paths(scope_paths) or None
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    tr_max_bytes = (tool_result_max_bytes if tool_result_max_bytes is not None
                   else _DEFAULT_TOOL_RESULT_MAX_BYTES)

    listing = doc_ledger.listing_documents(world, deadline=deadline)
    rows = listing.rows
    base_parts = prefix.split("/") if prefix else []
    base_depth = len(base_parts)

    # フォルダ集計: {folder_parts: {"direct", "total", "children"}}。集計は depth でクランプせず、出力時にだけ絞る
    agg: dict = {}
    unknown = 0
    partial = 0
    for r in rows:
        rel = r.get("name")
        if not rel:
            continue
        if not scope_mod.in_scope(rel, sp):
            continue
        if rules is not None and rules.excluded(rel):
            continue
        if prefix and not scope_mod.in_scope(rel, [prefix]):
            continue
        if not layer_mod.in_layer_row(r, layer):
            continue
        if doc_ledger.is_kind_unknown(r):
            unknown += 1
        if r.get("state") == "partial":
            partial += 1
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

    result = {
        "path_prefix": prefix,
        "depth": depth,
        "count": total_count,
        "offset": offset,
        "folders": [],
        "folders_truncated": False,
        "next_offset": None,
    }
    if listing.ledger_at:
        result["ledger_at"] = listing.ledger_at
    if partial:
        result["partial"] = partial
        result["partial_notice"] = doc_ledger.partial_notice(partial)
    if unknown:
        result["kind_unknown"] = unknown
        result["kind_unknown_notice"] = doc_ledger.kind_unknown_notice(unknown)
    used = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))  # フォルダ以外の欄の分

    folders = []
    for key in ordered_keys[offset:]:
        if len(folders) >= _MAX_FOLDERS:
            break
        entry = within_depth[key]
        rel_depth = len(key) - base_depth
        item = {
            "path": "/".join(key),
            "depth": rel_depth,
            "direct_files": entry["direct"],
            "total_files": entry["total"],
            "subfolders": len(entry["children"]),
            "truncated": rel_depth == depth and bool(entry["children"]),
        }
        # バイト予算は要素ごとの JSON の実バイト数（区切りの ", " を含む）で累積する
        item_bytes = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + (2 if folders else 0)
        if folders and used + item_bytes > tr_max_bytes:
            break
        folders.append(item)
        used += item_bytes

    shown_end = offset + len(folders)
    more = shown_end < total_count
    result["folders"] = folders
    result["folders_truncated"] = more
    result["next_offset"] = shown_end if more else None
    return result
