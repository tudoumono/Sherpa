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
                    "SHERPA_USE_FIXTURES", "SHERPA_DERIVED_DIR", "SHERPA_KB_DIR", "SHERPA_DISABLE_EMBED")
# 外へ出る経路の設定（プロキシ・社内 CA）。Codex 本体（`sandbox._codex_clean_env`）と MCP サブプロセスの両方へ、親環境にあるときだけ渡す。
NETWORK_PASSTHROUGH_ENV: tuple[str, ...] = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
)


# 道具のサーバー（MCP）が読む待ち時間の env。親の値を子へそのまま渡す（秘密ではない）。
TUNING_PASSTHROUGH_ENV: tuple[str, ...] = ("SHERPA_MCP_TOOL_DEADLINE_SEC", "SHERPA_SEARCH_EMBED_TIMEOUT_SEC")

# モデルの実行シェルへ渡す env を最小（PATH・HOME・LANG など）に絞る。プロキシの認証つき URL・接続用の API キーは Codex 本体と道具のサーバーだけが受け取り、シェルへは渡さない。
SHELL_ENV_POLICY_LINES: list[str] = ['', '[shell_environment_policy]', 'inherit = "core"']


# 値が秘密になり得る変数（パスワード・資格情報入りの URL）。Codex の引数へ値を出さず、名前だけ `env_vars` で渡す。
SECRET_PASSTHROUGH_ENV: tuple[str, ...] = ("NEO4J_PASSWORD", "NEO4J_URI", "ES_URL", "SHERPA_MCP_SETTINGS")


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


def _settings_snapshot_env(excluded_key: str) -> dict:
    """親が読んだ全体設定と管理者の設定の写しを env にする。DB が読めないときは空の写しを渡す。"""
    from ... import store
    try:
        system = {k: v for k, v in store.get_system_settings().items() if k != excluded_key}
        user = store.get_settings()
    except Exception:
        system, user = {}, {}
    return {store.CHILD_SNAPSHOT_ENV: json.dumps({"system": system, "user": user})}


def _mcp_env(world: str, scope_paths, ask_disabled: bool = False, layer=None, rules=None) -> dict:
    """MCP サーバ（Sherpa 側プロセス）が world/scope と Neo4j/ES を解決するための env。

    MCP サブプロセスは cwd=authoring で走り、PG creds を持たない（`_MCP_PASSTHROUGH` に含めない）。
    (a) KB/派生ディレクトリは絶対パス化して渡す（`_abs_kb_or_derived`）。
    (b) `SHERPA_MCP_WORLD_ROOT` にサーバ側で解決した world root の絶対パスを渡す。
    (c) `ask_disabled`（確認ID 付き再送）なら `SHERPA_MCP_ASK_DISABLED=1` を渡し、ツール自体を隠す。
    (d) 有効アーム・旧形式変換バックエンド・実効拡張子集合・VLM 可用性は親プロセスの実効値スナップショット（`SHERPA_MCP_ARMS`/`SHERPA_MCP_LEGACY_BACKEND`/`SHERPA_LEGACY_EXTS`/`SHERPA_VLM_USABLE`）を渡す。
    (e) `SHERPA_OFFICE_COM_URL`/`SHERPA_OFFICE_COM_TOKEN` は渡さない（共有シークレットを露出させない）。
    (h) `rules`（省略可）: 1 回の依頼で読んだ管理者の除外の規則。渡されたら読み直さずその写しを子へ渡す（permission profile の deny と同じスナップショット）。
    (f) `layer` は `layer.normalize_layer()` で検証し、`"docs"/"code"` のときだけ `SHERPA_MCP_LAYER` を渡す（不正値は `ValueError`）。qa レンズだけが実値を渡す。
    (i) 全体設定と管理者の設定は、親が読んだ写しを `SHERPA_MCP_SETTINGS` で渡す（埋め込みの接続先と鍵を子が DB なしで使う・鍵を含むので引数に出さない）。
    (g) 管理者の優先・除外（`path_rules`）は、設定がある資料フォルダだけ `SHERPA_MCP_PATH_RULES` に JSON の写しを渡す（子は DB を読めない）。
    """
    from ... import worlds
    from ...ingest import arms as ingest_arms
    from ...ingest.arms import legacy_convert, vision_arm
    from ... import proxy_bypass
    proxy_bypass.apply()  # 子へ渡す NO_PROXY に社内の宛先を足してから読む
    env = {k: os.environ[k] for k in _MCP_PASSTHROUGH if k in os.environ}
    env.update({k: v for k in NETWORK_PASSTHROUGH_ENV + TUNING_PASSTHROUGH_ENV if (v := os.environ.get(k))})  # 空文字は未設定と同じ
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
    from ... import path_rules
    env.update(path_rules.snapshot_env(world, rules))  # 管理者の優先・除外のスナップショット（子は DB を読めない・設定があるときだけ）
    env["SHERPA_MCP_LEGACY_BACKEND"] = legacy_convert.legacy_backend_name()  # 実効バックエンドのスナップショット
    # 実効拡張子集合のスナップショット（URL/TOKEN を渡さずに済む）。
    env["SHERPA_LEGACY_EXTS"] = ",".join(sorted(legacy_convert.legacy_exts()))
    # vision（VLM）の実効可用性スナップショット（1bit のみ・secrets は渡さない）。
    env["SHERPA_VLM_USABLE"] = "1" if vision_arm.resolve_vlm() is not None else "0"
    env.update(_settings_snapshot_env(path_rules.SETTINGS_KEY))
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
                     extra_env: dict | None = None, rules=None) -> list:
    """codex exec に sherpa MCP サーバ(stdio)を登録する -c 引数（per-request＝~/.codex 設定を汚さない）。
    `extra_env`（省略可）: `_mcp_env` の結果に上書きマージする追加 env。
    """
    from ... import store
    py = sys.executable or "python3"
    env = _mcp_env(world, scope_paths, ask_disabled, layer=layer, rules=rules)
    if extra_env:
        env.update(extra_env)
    # 経路の設定（プロキシ URL は認証情報を含み得る）と DB の秘密は値を引数へ出さない。名前だけ渡し、値は Codex を起こす env から受け継がせる。
    by_name = NETWORK_PASSTHROUGH_ENV + SECRET_PASSTHROUGH_ENV
    forwarded = [k for k in by_name if k in env]
    env_toml = "{" + ", ".join(f"{k} = {_toml_str(v)}" for k, v in env.items()
                               if k not in by_name) + "}"
    env_vars_toml = "[" + ", ".join(_toml_str(k) for k in forwarded) + "]"
    # MCP ツール承認は approval_policy と別系統。default_tools_approval_mode="approve" で sandbox(-s read-only) を保ったまま自動承認する。
    return ["-c", f"mcp_servers.sherpa.command={_toml_str(py)}",
            "-c", 'mcp_servers.sherpa.args = ["-m", "sherpa.mcp_server"]',
            "-c", f"mcp_servers.sherpa.env = {env_toml}",
            "-c", f"mcp_servers.sherpa.env_vars = {env_vars_toml}",
            # 設定の写しは鍵を含む。Codex のシェルの env には載せない。
            "-c", 'shell_environment_policy.inherit = "core"',
            "-c", f"shell_environment_policy.exclude = [{_toml_str(store.CHILD_SNAPSHOT_ENV)}]",
            "-c", 'mcp_servers.sherpa.default_tools_approval_mode = "approve"',
            "-c", 'approval_policy = "never"']
