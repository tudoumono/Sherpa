"""Codex × MCP 連携。

`sherpa.mcp_server`（stdio MCP サーバ）を codex exec に登録するための env/config 組み立て（`_mcp_env`／`_mcp_config_args`）と、
Codex の `graph_neighbors`／`ask_user` 呼び出し結果を思考イベント/UI カードへ変換するヘルパをまとめる。
依存は一方向（sandbox → mcp）。本モジュールは `sandbox.py` を import しない。
設計: docs/design/codex.md「MCP の道具」
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# ---- Codex × MCP（Codex は常に MCP 付きで起動する）----
_MCP_PASSTHROUGH = ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "ES_URL",
                    "SHERPA_USE_FIXTURES", "SHERPA_DERIVED_DIR", "SHERPA_KB_DIR")


def _toml_str(s) -> str:
    """TOML basic string へエスケープする（\\ と " と 改行）。codex の -c は値を TOML として解釈する。"""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _abs_kb_or_derived(raw_value: str | None, subpath: str) -> str:
    """SHERPA_KB_DIR/SHERPA_DERIVED_DIR をサーバの実効解釈と一致する絶対パスへ変換する。
    未設定/空は repo ルート基準の既定、明示値（相対/絶対）は `Path(value).resolve()`（サーバプロセスの cwd 基準）。
    """
    from ... import worlds
    if raw_value:
        return str(Path(raw_value).resolve())
    return str((worlds._repo_root() / subpath).resolve())


def _mcp_env(world: str, scope_paths, ask_disabled: bool = False, layer=None) -> dict:
    """MCP サーバ（Sherpa 側プロセス）が world/scope と Neo4j/ES を解決するための env。

    MCP サブプロセスは cwd=authoring で走り、PG creds を持たない（`_MCP_PASSTHROUGH` に含めない）。
    (a) KB/派生ディレクトリは絶対パス化して渡す（`_abs_kb_or_derived`）。
    (b) `SHERPA_MCP_WORLD_ROOT` にサーバ側で解決した world root の絶対パスを渡す。
    (c) `ask_disabled`（確認ID 付き再送）なら `SHERPA_MCP_ASK_DISABLED=1` を渡し、ツール自体を隠す。
    (d) 有効アーム・旧形式変換バックエンド・実効拡張子集合・VLM 可用性は親プロセスの実効値スナップショット（`SHERPA_MCP_ARMS`/`SHERPA_MCP_LEGACY_BACKEND`/`SHERPA_LEGACY_EXTS`/`SHERPA_VLM_USABLE`）を渡す。
    (e) `SHERPA_OFFICE_COM_URL`/`SHERPA_OFFICE_COM_TOKEN` は渡さない（共有シークレットを露出させない）。
    (f) `layer` は `layer.normalize_layer()` で検証し、`"docs"/"code"` のときだけ `SHERPA_MCP_LAYER` を渡す（不正値は `ValueError`）。qa レンズだけが実値を渡す。
    """
    from ... import worlds
    from ...ingest import arms as ingest_arms
    from ...ingest.arms import legacy_convert, vision_arm
    env = {k: os.environ[k] for k in _MCP_PASSTHROUGH if k in os.environ}
    # 接続先は親が解決した ES/Neo4j の URL を明示して渡す（ポート変数だけの構成で MCP 側が既定ポートへ繋ぐのを防ぐ）。
    from sherpa import es_index as _es
    from sherpa.ingest import world_neo4j as _neo
    env["ES_URL"] = _es._url()
    env["NEO4J_URI"] = _neo.default_neo4j_uri()
    env["SHERPA_MCP_WORLD"] = world
    if scope_paths:
        env["SHERPA_MCP_SCOPE"] = "\n".join(scope_paths)
    if layer is not None:
        from ... import layer as layer_mod
        # 不正な内部値は Codex 起動前に ValueError で拒否する（黙って both へ丸めない）。
        normalized_layer = layer_mod.normalize_layer(layer)
        if normalized_layer != "both":
            env["SHERPA_MCP_LAYER"] = normalized_layer
    env["SHERPA_KB_DIR"] = _abs_kb_or_derived(env.get("SHERPA_KB_DIR"), "data/kb")
    env["SHERPA_DERIVED_DIR"] = _abs_kb_or_derived(env.get("SHERPA_DERIVED_DIR"), "data/derived")
    if ask_disabled:
        env["SHERPA_MCP_ASK_DISABLED"] = "1"
    env["SHERPA_MCP_ARMS"] = ",".join(ingest_arms.enabled_arm_names())  # 実効アームのスナップショット
    env["SHERPA_MCP_LEGACY_BACKEND"] = legacy_convert.legacy_backend_name()  # 実効バックエンドのスナップショット
    # 実効拡張子集合のスナップショット（URL/TOKEN を渡さずに済む）。
    env["SHERPA_LEGACY_EXTS"] = ",".join(sorted(legacy_convert.legacy_exts()))
    # vision（VLM）の実効可用性スナップショット（1bit のみ・secrets は渡さない）。
    env["SHERPA_VLM_USABLE"] = "1" if vision_arm.resolve_vlm() is not None else "0"
    try:
        wd = worlds.world_dir(world)
        if wd:
            env["SHERPA_MCP_WORLD_ROOT"] = str(Path(wd).resolve())
    except Exception:
        pass  # 解決不可はサブプロセス側の通常解決に委ねる
    return env


def _graph_schema_era_from_item(item: dict, world: str, lens: str | None):
    """完了した `graph_neighbors`・`graph_resolve`・`graph_impact` の mcp_tool_call item が構造化エラー（`graph_reingest_required`・`isError: true`）を運んでいれば `GraphSchemaEraError` を再構成する。
    それ以外（通常結果・壊れた形・対象外のツール）は None。
    """
    try:
        res = item["result"]
        if not res.get("isError"):
            return None
        text = res["content"][0]["text"]
        data = json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
    if not isinstance(data, dict) or data.get("error") != "graph_reingest_required":
        return None
    from ...ingest.world_neo4j import GraphSchemaEraError
    return GraphSchemaEraError(world, data.get("stored_era"), lens=lens)


def _mcp_neighbors_from(item: dict) -> list:
    """完了した graph_neighbors の mcp_tool_call item から neighbors（compact view）を取り出す。壊れていれば []。"""
    try:
        text = item["result"]["content"][0]["text"]
        data = json.loads(text)
        ns = data.get("neighbors", []) if isinstance(data, dict) else []  # JSON が list/str でも .get で落とさない
        return ns if isinstance(ns, list) else []
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return []


# 結果の要約の上限。会話の保存が思考ノードの補足を切る長さ（`chat_service._MAX_TRACE_DETAIL_CHARS`）と同じ
GRAPH_SUMMARY_MAX_CHARS = 200


def _graph_tool_summary(tool: str, item: dict) -> str | None:
    """完了した `graph_resolve`／`graph_impact` の mcp_tool_call item の結果を、思考ノードの補足（再表示でも残る）用の 1 文に要約する。
    件数・調査の完了／未完了（`coverage.complete` と `limits[].kind`）・先頭の数件の識別子と所属パス。壊れた形・エラー結果は None。
    結果はツールが範囲・秘匿の除外を通した後のものなので、ここでは再検査しない。
    """
    try:
        data = json.loads(item["result"]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
    field, noun = ("candidates", "候補") if tool == "graph_resolve" else ("impact", "影響先")
    rows = data.get(field) if isinstance(data, dict) else None
    if not isinstance(rows, list) or data.get("error"):
        return None
    count = data.get("count")
    cov = data.get("coverage") if isinstance(data.get("coverage"), dict) else {}
    kinds = [str(x.get("kind")) for x in cov.get("limits") or [] if isinstance(x, dict)]
    state = "調査は完了" if cov.get("complete", True) else "未完了（" + "・".join(kinds) + "）"
    head = [f"{r.get('path')} ({r.get('canonical_id')})" for r in rows[:3] if isinstance(r, dict)]
    text = f"{noun}{count if isinstance(count, int) else len(rows)}件{'以上' if count is None else ''}・{state}"
    if tool == "graph_impact" and isinstance(data.get("start"), dict) and data["start"].get("path"):
        text = f"起点 {data['start']['path']}・{text}"  # 検証を通った結果の起点だけ（引数の識別子は記録しない）
    if head:
        text += "：" + "、".join(head)
    return text[:GRAPH_SUMMARY_MAX_CHARS]


def _graph_tool_failure(item: dict) -> str | None:
    """完了した `graph_resolve`／`graph_impact` の結果が失敗応答（`error`・`error_code`・旧世代）なら、思考ノードに残す固定の文言を返す。
    起点が見つからない・範囲外・秘匿のときも識別子や理由の詳細は記録しない。成功・壊れた形は None。
    """
    try:
        data = json.loads(item["result"]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        data = None
    if not isinstance(data, dict) or not (data.get("error") or data.get("error_code")):
        return None
    return "起点が見つかりませんでした" if data.get("error") == "graph_start_not_found" else "調べられませんでした"


def _apply_codex_neighbors(env: dict, mcp_neighbors: list, lens) -> None:
    """troubleshoot で Codex が graph_neighbors で実際に引いた近傍を UI カード(candidates)に反映する。
    `_gather` 由来の candidates を Codex の実調査由来で上書きし、name で重複排除して `summary.total` を整合させる。troubleshoot 以外／近傍無しは無変更。
    """
    if not (mcp_neighbors and lens == "troubleshoot" and isinstance(env, dict)):
        return
    seen, uniq = set(), []
    for n in mcp_neighbors:
        nm = n.get("name") if isinstance(n, dict) else None
        if nm and nm not in seen:
            seen.add(nm)
            uniq.append(n)
    env.setdefault("data", {})["candidates"] = uniq
    env.setdefault("summary", {})["total"] = len(uniq)


def _codex_ask_question(item: dict) -> dict | None:
    """Codex の mcp_tool_call(ask_user) item を、フロントに出せる question イベントへ丸める（ask_user 以外／非 dict は None）。
    生成は `agentic_search._question_from_args` を再利用する。
    """
    if not isinstance(item, dict) or item.get("tool") != "ask_user":
        return None
    from ... import agentic_search
    args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
    return agentic_search._question_from_args(args)


def _codex_ask_capture(item: dict, ask_disabled: bool) -> dict | None:
    """確認ID 付き再送では ask_user を無視する判定を1箇所に集約した純粋関数。
    1実行1回の担保は呼び出し側（既に codex_question があれば呼ばない）。
    """
    if ask_disabled:
        return None
    return _codex_ask_question(item)


def _mcp_config_args(world: str, scope_paths, ask_disabled: bool = False, layer=None,
                     extra_env: dict | None = None) -> list:
    """codex exec に sherpa MCP サーバ(stdio)を登録する -c 引数（per-request＝~/.codex 設定を汚さない）。
    `extra_env`（省略可）: `_mcp_env` の結果に上書きマージする追加 env。
    """
    py = sys.executable or "python3"
    env = _mcp_env(world, scope_paths, ask_disabled, layer=layer)
    if extra_env:
        env.update(extra_env)
    env_toml = "{" + ", ".join(f"{k} = {_toml_str(v)}" for k, v in env.items()) + "}"
    # MCP ツール承認は approval_policy と別系統。default_tools_approval_mode="approve" で sandbox(-s read-only) を保ったまま自動承認する。
    return ["-c", f"mcp_servers.sherpa.command={_toml_str(py)}",
            "-c", 'mcp_servers.sherpa.args = ["-m", "sherpa.mcp_server"]',
            "-c", f"mcp_servers.sherpa.env = {env_toml}",
            "-c", 'mcp_servers.sherpa.default_tools_approval_mode = "approve"',
            "-c", 'approval_policy = "never"']
