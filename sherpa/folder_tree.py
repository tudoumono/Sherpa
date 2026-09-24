"""`folder_tree` ツール（K6）本体: world のフォルダ階層を深さ上限つきで俯瞰する、LLM 非使用・
索引不使用の決定的な木。

正典: `docs/proposals/2026-09-04-グラフのソース正典化.md` §3 K6・§4b S1。`list_docs`（ls 相当・
フラット一覧）に対する tree 相当——`doc_ledger`（文書台帳の走査）が返す rel_path 一覧から
フォルダ単位で直下/再帰ファイル数・直下サブフォルダ数を集計するだけで、フォルダ名の**意味解釈は
しない**（クエリ時にエージェントの LLM が解釈する・K6・§5「フォルダ意味ノードの事前計算はしない」）。

独立モジュールにする理由: `compare_docs.py`（GEN-DIFF）と同じ判断——集計ロジックがそれなりの
分量になり単体テストしやすいこと、登録元（`agentic_search.py`/`mcp_server.py`）はこちらを
import する側（逆は循環 import になるため不可）。
"""
from __future__ import annotations

from . import doc_ledger
from . import layer as layer_mod
from . import scope as scope_mod

# フォルダ列挙件数の安全弁（K6 仕様の例示値）。超過分は打ち切り、`folders_truncated` で申告する
# （黙って一部だけ返して全件のように見せない・list_docs の count/limit と同じ「打ち切り前の総数を
# 別に持つ」流儀）。
_MAX_FOLDERS = 500

# RV是正（rv-periphery #2）: `tool_result_max_bytes`（呼び出し元＝`run_tool` が run 単位で解決した
# 実効値）を省略された場合だけのフォールバック既定。本モジュールは `agentic_search` を import しない
# （逆方向の import のため循環になる・モジュール docstring 参照）ため、`agentic_search.
# TOOL_RESULT_MAX_BYTES` と同じ値をここに複製する（`compare_docs.py` 冒頭の `_env_int` 複製と
# 同じ判断＝循環回避のため値だけ独立に持つ）。
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
    """`folder_tree` ツール本体（決定的・LLM 呼び出しゼロ）。

    `path_prefix`（省略可）配下・`depth`（省略時3・1〜10 にクランプ）までの各フォルダについて、
    パス・直下ファイル数・配下（再帰）ファイル数・直下サブフォルダ数を返す。`scope_paths`
    （会話ターン全体にかかる硬いフィルタ・他ツールと同型）で範囲外は最初から数えない。
    `layer`（省略可・`"docs"|"code"|"both"`）は `list_docs`/`glob_search` と同じ硬いフィルタ
    （`layer_mod.in_layer_code`・`doc_ledger` 行の `branch=="source"` で確定判定）——探す対象が
    限定されている間、限定外の層の件数が folder_tree のフォルダ集計に紛れ込まないようにする。

    `tool_result_max_bytes`（省略可・既定 `None`＝モジュール既定 `_DEFAULT_TOOL_RESULT_MAX_BYTES`）:
    呼び出し元（`run_tool`）が run 単位で解決したツール結果1件あたりのバイト予算の実効値
    （RV是正・rv-periphery #2）。`_MAX_FOLDERS`（件数上限）だけでは、フォルダ数が多い巨大な
    world で `path` の合計バイト数が実効予算を超えうる——`folders` はエントリ単位でこの予算まで
    詰め、超えた時点で打ち切る（`doc_outline` の見出し累積バイト打切りと同じ流儀）。

    戻り値: `{"path_prefix", "depth", "count", "folders": [...], "folders_truncated"}`。
    `folders` の各要素: `{"path", "depth", "direct_files", "total_files", "subfolders", "truncated"}`
    ——`truncated`（フォルダ単位）は「深さ上限に達し、まだ配下があるのに表示していない」の意味、
    `folders_truncated`（全体・件数上限とバイト上限のどちらでも真になる）は「列挙件数/バイト予算の
    安全弁で一部フォルダ自体を返していない」の意味——別の事実なので取り違えない（黙って消さない＝
    両方明示する）。`count` は打切り前の総フォルダ数のまま（バイト打切りでも減らさない）。
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

    # フォルダ集計: {folder_parts(tuple): {"direct": 直下ファイル数, "total": 配下再帰ファイル数,
    # "children": 直下サブフォルダ名の集合}}。集計自体は depth でクランプしない（深さ上限フォルダの
    # total_files/subfolders が「その下は数えていない過小申告」にならないようにする）——出力時にだけ
    # depth でフィルタする。
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
        dir_parts = rel.split("/")[:-1]                # ファイル名を除いたフォルダ階層
        if len(dir_parts) <= base_depth:
            continue                                    # path_prefix 直下の裸ファイル＝フォルダを持たない
        for d in range(base_depth + 1, len(dir_parts) + 1):
            key = tuple(dir_parts[:d])
            entry = agg.setdefault(key, {"direct": 0, "total": 0, "children": set()})
            entry["total"] += 1
            if d == len(dir_parts):
                entry["direct"] += 1
            else:
                entry["children"].add(dir_parts[d])     # 深さ d+1 の直下サブフォルダ名

    within_depth = {k: v for k, v in agg.items() if len(k) - base_depth <= depth}
    ordered_keys = sorted(within_depth.keys())
    total_count = len(ordered_keys)

    folders = []
    cum_bytes = 0
    for key in ordered_keys:
        if len(folders) >= _MAX_FOLDERS:                # 件数の安全弁（従来どおり）
            break
        path = "/".join(key)
        # RV是正（rv-periphery #2）: バイト予算はエントリ単位で `path`（唯一の可変長フィールド・
        # 他フィールドは小さい固定幅の整数/真偽値）の累積バイト数だけで判定する（`doc_outline` の
        # 見出しバイト累積と同じ簡略化）。
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
