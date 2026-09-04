"""Sherpa MCP サーバ（stdio・自前実装・SDK 非依存）。

Codex（自律エージェント）に Sherpa の read-only ツール一家（grep / 精読 / グラフ近傍 / ES）を渡すための
**MCP(stdio) サーバ**。ツールの実装は `agentic_search.run_tool` を**そのまま再利用**（cloud LLM と実装共通＝
ツールの中身は1本・rv 方針）。Codex は別プロセスの自律エージェントなので API の function-calling では渡せず、
MCP がツールを渡す唯一の素直な口（network 不要＝本サーバ＝Sherpa 側プロセスが Neo4j/ES に接続する）。

- **world / scope / layer は起動時の環境変数で固定**（`SHERPA_MCP_WORLD` / `SHERPA_MCP_SCOPE` /
  `SHERPA_MCP_LAYER`）＝Codex/LLM には選ばせない（範囲外探索を防ぐ・read-only は run_tool 側で担保）。
- トランスポート＝**改行区切り JSON-RPC 2.0**（MCP stdio）。stdin から1行1メッセージ、stdout に応答、ログは stderr。
- `ask_user` は Codex にも公開する（S2・ask_user-improvements.md）。codex exec は非対話だが、質問は
  ラッパー（`agents._run_authoring` の mcp_tool_call 監視）が question イベントとしてフロントへ届ける
  ＝ここはツール結果（「届いた・調査をやめて要約せよ」）を返すだけ。乱用ガードの1つ＝**1実行1回**は
  本サーバのプロセス寿命（codex exec 1回＝1プロセス）で数える。
- `SHERPA_MCP_ASK_DISABLED=1`（確認ID 付き再送＝ラッパーが ask_user を無視する実行）が立っていたら
  **ask_user を tools/list から外す**（呼べる道具を最初から見せない＝最強のガード・RV HIGH 2026-07-07）。
  それでも呼ばれたら（Codex がプロンプト指示に反した場合の防御）初回でも `_ASK_RESULT_AGAIN` を返す
  ＝質問カードを出さないまま調査を打ち切らせない（ラッパー側 `_ask_disabled` と同じフラグを共有）。
"""
from __future__ import annotations

import json
import os
import sys

from . import agentic_search, es_index
from .ingest.world_neo4j import GraphSchemaEraError

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "sherpa", "version": "0.1.0"}

# S2: ask_user は検索ツールではない（run_tool に実装は無い）＝ここでは Codex に返す**ツール結果**だけを持つ。
# 1実行1回まで（この MCP サーバは codex exec 1回につき1プロセスなので、プロセス寿命＝1実行＝モジュール変数で数える）。
_ASK_STATE = {"count": 0}
_ASK_RESULT_FIRST = "質問はユーザーに届きました。追加調査はせず、ここで簡潔に現状を要約して終了してください。"
_ASK_RESULT_AGAIN = "既に質問済みです。調査を続けて回答をまとめてください。"


def _tool_defs() -> list:
    """公開ツール定義（schema は agentic_search と共通＝二重管理しない）。ES はインデックスがある時だけ。
    S2 RV HIGH: `SHERPA_MCP_ASK_DISABLED=1`（確認ID 付き再送）の実行では ask_user 自体を外す
    （呼べる道具を最初から見せない＝プロンプト指示だけに頼らない最強のガード）。
    探す対象（層）が限定されている間は `graph_neighbors` 自体を外す（呼べる道具を最初から見せない・
    `run_tool` 側の拒否と多層防御・迂回路を tools/list の時点で塞ぐ）。"""
    defs = [
        {"name": "list_docs", "description": agentic_search._DESC_LIST_DOCS,
         "inputSchema": agentic_search._PARAMS_LIST_DOCS},
        # K6（`docs/proposals/2026-09-04-グラフのソース正典化.md` §3・§4b S1）: list_docs と同じ
        # 台帳ベースの土台系ツール（ES/graph 可用性に依存しない）＝常に公開する。
        {"name": "folder_tree", "description": agentic_search._DESC_FOLDER_TREE,
         "inputSchema": agentic_search._PARAMS_FOLDER_TREE},
        {"name": "ripgrep_search", "description": agentic_search._DESC_SEARCH,
         "inputSchema": agentic_search._PARAMS_SEARCH},
        {"name": "read_around", "description": agentic_search._DESC_READ,
         "inputSchema": agentic_search._PARAMS_READ},
        # GEN-DIFF（`docs/proposals/2026-09-03-世代間diff比較.md` §5）: ES/graph の可用性に依存しない
        # 土台系ツール（read_around 等と同じ扱い）＝常に公開する。
        {"name": "compare_documents", "description": agentic_search._DESC_COMPARE,
         "inputSchema": agentic_search._PARAMS_COMPARE},
    ]
    if _layer() in (None, "both"):
        defs.append({"name": "graph_neighbors", "description": agentic_search._DESC_GRAPH,
                     "inputSchema": agentic_search._PARAMS_GRAPH})
    if not _ask_disabled():
        # S2: ask_user を Codex にも公開（description は agentic と同じ制約文言＝_DESC_ASK・schema も共通）。
        defs.append({"name": "ask_user", "description": agentic_search._DESC_ASK,
                     "inputSchema": agentic_search._PARAMS_ASK})
    if es_index.available():
        # K6 で folder_tree を list_docs の直後に挿入したため、ripgrep_search の直後（read_around の
        # 直前）は index 3 になった（list_docs, folder_tree, ripgrep_search, [es_search], read_around, ...）。
        defs.insert(3, {"name": "es_search", "description": agentic_search._DESC_ES,
                        "inputSchema": agentic_search._PARAMS_SEARCH})
    return defs


def _world() -> str:
    return os.environ.get("SHERPA_MCP_WORLD", "v1")


def _scope():
    raw = os.environ.get("SHERPA_MCP_SCOPE", "")
    paths = [s.strip() for s in raw.split("\n") if s.strip()]
    return paths or None


def _layer():
    """探す対象（層フィルタ）。未設定は `None`（`agentic_search.run_tool` 側で both 扱い）。
    親プロセス（`codex/mcp.py::_mcp_env`）が qa レンズのときだけ渡す＝impact/troubleshoot・author は
    未設定のまま（グラフ traversal 非適用・Codex 自身の追加探索の既知の非対称性）。"""
    return os.environ.get("SHERPA_MCP_LAYER") or None


def _ask_disabled() -> bool:
    """S2 RV HIGH: この実行（確認ID 付き再送）ではラッパーが ask_user を無視する＝サーバ側でも隠す。"""
    return os.environ.get("SHERPA_MCP_ASK_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _ok(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def handle(req: dict) -> dict | None:
    """JSON-RPC 1件を処理して応答 dict を返す。**通知（id 無し）は None＝応答しない**（MCP 準拠）。"""
    method = req.get("method")
    rid = req.get("id")
    is_notification = "id" not in req
    if method == "initialize":
        # protocolVersion はクライアント要求をそのまま返す（緩い交渉・未指定なら既定）。
        pv = (req.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        return _ok(rid, {"protocolVersion": pv, "capabilities": {"tools": {}},
                         "serverInfo": SERVER_INFO})
    if method == "tools/list":
        return _ok(rid, {"tools": _tool_defs()})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "ask_user":
            # S2: ask_user はユーザーへ届ける「質問」＝検索ツールではない。実際の質問カード表示は
            # ラッパー（agents._run_authoring）が question イベントで行う。ここは Codex に「届いた・
            # 追加調査せず要約して終了せよ」を返すだけ（乱用ガード③: 2回目以降は別文言で調査続行を促す）。
            # RV HIGH: 確認ID 付き再送（tools/list で隠しても、プロンプト指示に反して呼ばれる場合の防御）は
            # 初回でも _ASK_RESULT_AGAIN＝質問カードを出さずに調査を打ち切らせない（ラッパーも無視するため）。
            if _ask_disabled():
                return _ok(rid, {"content": [{"type": "text", "text": _ASK_RESULT_AGAIN}], "isError": False})
            _ASK_STATE["count"] += 1
            text = _ASK_RESULT_FIRST if _ASK_STATE["count"] == 1 else _ASK_RESULT_AGAIN
            return _ok(rid, {"content": [{"type": "text", "text": text}], "isError": False})
        try:
            result, _docs, _cites, _cards = agentic_search.run_tool(name, args, _world(), _scope(), layer=_layer())
        except GraphSchemaEraError as e:
            # RV是正（rv-periphery #11）: `graph_neighbors`（`lens_service.neighbor_cards` 経由）が
            # 検知した旧世代グラフは、汎用の JSON-RPC プロトコルエラー（-32603・`serve()` の
            # broad except）に丸めず、通常のツール結果と同じ経路（`isError` あり・
            # `content[].text` に安定した機械可読コード）で返す——Codex 側の `item["result"]` に
            # そのまま載るため（JSON-RPC のプロトコルエラーは Codex 自身の item 表現が保証されて
            # いない）、`providers/codex/mcp.py::_graph_schema_era_from_item` が読み取って
            # `GraphSchemaEraError` を再構成できる。
            err_body = {"error": "graph_reingest_required", "world": e.world, "stored_era": e.stored_era}
            return _ok(rid, {"content": [{"type": "text", "text": json.dumps(err_body, ensure_ascii=False)}],
                             "isError": True})
        # MCP 標準＝content[].text。Codex が読む本文＝run_tool の結果（graph_neighbors は compact neighbors）。
        return _ok(rid, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                         "isError": bool(isinstance(result, dict) and result.get("error"))})
    if is_notification:                       # notifications/initialized 等＝応答しない
        return None
    return _err(rid, -32601, f"method not found: {method}")


def serve(stdin=None, stdout=None) -> None:
    """stdin から改行区切り JSON-RPC を読み、stdout に応答を書く（MCP stdio ループ）。"""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue                          # 壊れた行は黙って捨てる（プロトコルを落とさない）
        try:
            resp = handle(req)
        except Exception as e:                # ツール例外でもサーバは落とさない（その応答だけ error）
            resp = _err(req.get("id"), -32603, f"{type(e).__name__}: {e}")
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
