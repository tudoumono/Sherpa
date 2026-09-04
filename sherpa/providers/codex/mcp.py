"""Codex × MCP（真のグラフ・エージェント Phase2b・既定ON＝SHERPA_CODEX_MCP）連携（リファクタリング計画
フェーズ5 S9・`sherpa/agents.py` から純移動）。

`sherpa.mcp_server`（stdio MCP サーバ）を codex exec に登録するための env/config 組み立て
（`_mcp_env`／`_mcp_config_args`）、Codex の `graph_neighbors`／`ask_user` ツール呼び出し結果を
思考イベント/UI カードへ変換するヘルパ（`_mcp_neighbors_from`／`_apply_codex_neighbors`／
`_codex_ask_question`／`_codex_ask_capture`）をまとめる。`sherpa/agents.py` が facade として
本モジュールから再エクスポートする。利用者はいずれも providers パッケージ内の兄弟
（`codex/provider.py` の `CodexProvider._run_authoring`＝S10 で移動済み・`codex/sandbox.py`＝
直接 import）。

移動した10名: `_MCP_PASSTHROUGH`・`_codex_mcp_enabled`・`_toml_str`・`_abs_kb_or_derived`・
`_mcp_env`・`_mcp_neighbors_from`・`_apply_codex_neighbors`・`_codex_ask_question`・
`_codex_ask_capture`・`_mcp_config_args`。

**相対 import の深さ調整（純移動の範囲内・S5〜S8 と同じ判断）**: `_abs_kb_or_derived`・`_mcp_env`・
`_codex_ask_question` 内の `from . import worlds` 等は、本モジュールが `sherpa` から2階層深い
（providers→codex）ため `from ... import worlds` 等に変更した（参照先は変わらず
`sherpa.worlds`/`sherpa.ingest.arms`/`sherpa.agentic_search`）。

**facade 実行時解決は不要**: この10名は互いにのみ依存し（`_mcp_config_args` が同モジュール内の
`_mcp_env`/`_toml_str` を呼ぶだけ）、providers パッケージ内の他モジュールへは依存しない。
本モジュールは `codex/sandbox.py` を import しない（依存は一方向 **sandbox → mcp**）。
`codex/sandbox.py` は S8 時点の暫定として facade 実行時解決で `_mcp_env`/`_toml_str` を呼んでいたが、
RV（2026-07-14・LOW）で `from .mcp import _mcp_env, _toml_str` の直接 import に更新済み
（両名ともテストが facade 属性を patch する名前ではない＝素通り懸念なし・sandbox.py docstring 参照）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# ---- Codex × MCP（真のグラフ・エージェント Phase2b・既定ON＝SHERPA_CODEX_MCP）----
# 既定 ON：Codex は事実の前渡しをやめ、MCP ツール(sherpa)で自律調査する（agentic 主軸・ROADMAP §3）。
# 無効化は `SHERPA_CODEX_MCP=0`（従来の事実前渡し経路に戻す）。
# TOGGLE-RM（2026-09-03）: 旧 `SHERPA_SEARCH_RAG_GREP` の透過はここから撤去した——親プロセスの
# `grep_tool.rag_grep_enabled()` が常時 True へ固定されたため、MCP サブプロセス（同じコードベース）
# も常に rag 側を見る。透過しなくても親子で判定が食い違わない。
_MCP_PASSTHROUGH = ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "ES_URL",
                    "SHERPA_USE_FIXTURES", "SHERPA_DERIVED_DIR", "SHERPA_KB_DIR")


def _codex_mcp_enabled() -> bool:
    # 既定 ON。明示的な falsy（0/false/no/off/空）だけ無効化。
    return os.environ.get("SHERPA_CODEX_MCP", "1").strip().lower() not in ("0", "false", "no", "off", "")


def _toml_str(s) -> str:
    """TOML basic string へエスケープ（\\ と " と 改行）。codex の -c は値を TOML として解釈する。"""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _abs_kb_or_derived(raw_value: str | None, subpath: str) -> str:
    """SHERPA_KB_DIR/SHERPA_DERIVED_DIR をサーバの実効解釈と一致する絶対パスへ変換する。

    RV MEDIUM（2026-07-03 再検証）: 明示的に相対値が設定されている場合、サーバ本体
    （`worlds._kb()`/`worlds.derived_dir()`）はそれを**サーバプロセスの cwd 基準**で解釈する。
    ここで repo ルート基準に変換してしまうと、サーバ本体と MCP サブプロセスが**別ディレクトリ**を
    見てしまう不整合が生じる（旧実装の不具合）。そのため:
    - **未設定/空文字**（空文字は未設定と同じ扱い＝`worlds._kb()` と同じ判定）: `worlds._repo_root()`
      基準の既定（`worlds._kb()`/`derived_dir()` の新既定と一致）。
    - **明示値**（相対/絶対いずれも）: `Path(value).resolve()`＝**サーバプロセスの cwd 基準**で絶対化
      （サーバ本体の実効解釈と常に一致させる）。
    """
    from ... import worlds
    if raw_value:
        return str(Path(raw_value).resolve())
    return str((worlds._repo_root() / subpath).resolve())


def _mcp_env(world: str, scope_paths, ask_disabled: bool = False, layer=None) -> dict:
    """MCP サーバ（Sherpa 側プロセス）が world/scope と Neo4j/ES を解決するための env。

    RV HIGH（2026-07-03・4頭脳比較で発覚の実機バグ）: MCP サブプロセスは cwd=authoring で走る。
    (a) SHERPA_KB_DIR/SHERPA_DERIVED_DIR を相対値のまま（または未設定で相対既定のまま）渡すと、
    サブプロセス側で cwd=authoring 基準に解決され、派生MD（Office 文書の本文）ディレクトリが
    見つからず Office 文書が丸ごと台帳から脱落する（source/.txt 等は wd 直下を直接見るため
    影響を受けず、「Office だけ消える」非対称な症状になっていた＝実機で確定）。
    ここで絶対パス化してから渡す（`_abs_kb_or_derived` 参照・サーバの実効解釈と一致させる）。
    (b) さらに SHERPA_MCP_WORLD_ROOT（サーバ側で registry 込みで解決した world root の絶対パス）を
    新設して渡す。MCP サブプロセスは PG creds を持たない（`_MCP_PASSTHROUGH` に含めない設計）ため
    registry 解決（Postgres 接続）に依存させず、サーバ側の解決結果をそのまま使わせる
    （`_kb_read_roots()` と同じ「サーバ側で解決した絶対パスを渡す」パターン）。
    (c) S2 RV HIGH（2026-07-07）: `ask_disabled`（＝この実行が確認ID 付き再送＝ラッパーは ask_user を
    無視する）なら SHERPA_MCP_ASK_DISABLED=1 を渡す。プロンプト指示だけに頼らず、MCP サーバ側で
    ツール自体を隠す＋万一呼ばれても防御応答を返す二段構え（tool 非公開＝最強のガード）。
    (d) W0 RV High（2026-07-08）: MCP サブプロセスは PG creds を持たない（`_MCP_PASSTHROUGH` に非含）ため
    system_settings を読めず、有効アーム（S1 `arms_enabled`）・旧形式変換バックエンド（W0 `legacy_backend`）
    は env フォールバックに落ちる。親プロセス（API リクエスト時点）の**実効値スナップショット**を
    SHERPA_ARMS/SHERPA_LEGACY_BACKEND として渡すことで、サブプロセス側は env フォールバックだけで
    親と同じ実効値に一致する（list_docs から .doc が消えるのに grep はヒットする、といった不一致を防ぐ）。
    会話の途中で admin が設定を変えても**このスポーンには反映されない**（次スポーンから反映＝許容）。
    (e) W1 RV Med（2026-07-08・token 漏洩対策）: `SHERPA_OFFICE_COM_URL`/`SHERPA_OFFICE_COM_TOKEN` は
    **MCP サブプロセスへ渡さない**（Codex の sandbox 無効時 fallback 実行環境の env/コマンドラインに
    共有シークレットが露出するのを避ける＝MCP は変換を実行しない読み取り専用なので不要）。代わりに
    親プロセスが `legacy_convert.legacy_exts()` を呼んで得た**実効拡張子集合のスナップショット**を
    `SHERPA_LEGACY_EXTS`（例 `.doc,.xls,.ppt`）として渡す。サブプロセス側の `legacy_exts()` はこの env が
    設定されていれば最優先で信じ、healthz へは一切 probe しない（office_com の到達性判定に secrets 不要
    になる）。これにより `SHERPA_SOFFICE_BIN` の透過も不要になった（legacy_exts はもう soffice 検出を
    経由しない）ため削除した。tesseract バイナリパス env の透過も、tesseract 直の `ocr` アーム撤去
    （2026-07-08）に伴い削除した。
    (f) RV Med（Codex gpt-5.5/xhigh・2026-07-08 R1）: MCP サブプロセスは PG creds を持たないため
    `vision` の VLM 実効可用性（system_settings.vlm・cloud_allowed 等）を読めず、常にローカル既定
    （usable）にフォールバックして親と view が乖離しうる（例: 親が openai・cloud_allowed=false で
    unusable でも MCP は既定 ollama で usable と誤判定）。親プロセスが計算した
    `vision_arm.resolve_vlm() is not None` の1bit スナップショットを `SHERPA_VLM_USABLE`
    （`"1"`/`"0"`）として渡す（secrets は渡さない・`vision_arm._vlm_usable_override` が最優先で読む）。
    (g) 探す対象（層フィルタ）: `layer`（省略可・既定 `None`＝both＝渡さない）は
    `layer.normalize_layer()` で検証してから `"docs"/"code"` のときだけ `SHERPA_MCP_LAYER` を渡す
    （不正な内部値は Codex 起動前に `ValueError`＝fail-loud）。呼び出し元（`_run_authoring`）は
    qa レンズのときだけ実際の値を渡す——impact/troubleshoot（グラフ traversal）・author（Codex
    自身の追加探索は正典 §1.8 の既知の非対称性）はこの引数を省略（`None`）にすることで both のまま渡す。
    """
    from ... import worlds
    from ...ingest import arms as ingest_arms
    from ...ingest.arms import legacy_convert, vision_arm
    env = {k: os.environ[k] for k in _MCP_PASSTHROUGH if k in os.environ}
    # 接続先は「親が解決した値」を明示して渡す（2026-08-18・RV MED）。ポートを 1 変数（SHERPA_ES_PORT /
    # SHERPA_NEO4J_BOLT_PORT）で決める構成では ES_URL/NEO4J_URI が環境に無く、上の透過だけだと MCP 側が
    # 既定ポート（9200/7687）へ繋ぎに行き、親アプリと接続先が食い違う。親と同じ関数で解決した URL を渡す。
    from sherpa import es_index as _es
    from sherpa.ingest import world_neo4j as _neo
    env["ES_URL"] = _es._url()
    env["NEO4J_URI"] = _neo.default_neo4j_uri()
    env["SHERPA_MCP_WORLD"] = world
    if scope_paths:
        env["SHERPA_MCP_SCOPE"] = "\n".join(scope_paths)
    if layer is not None:
        from ... import layer as layer_mod
        # 不正な内部値は Codex 起動前に ValueError で拒否する（HTTP 入口は pydantic Literal で
        # 別途 422 にする・ここに届くのは呼び出し側のバグ＝黙って both へ丸めない）。呼び出し元
        # （_run_authoring）の既存 broad except が「profile config 書込失敗→決定的回答」の
        # fail-closed 経路へ合流させる。
        normalized_layer = layer_mod.normalize_layer(layer)
        if normalized_layer != "both":
            env["SHERPA_MCP_LAYER"] = normalized_layer
    env["SHERPA_KB_DIR"] = _abs_kb_or_derived(env.get("SHERPA_KB_DIR"), "data/kb")
    env["SHERPA_DERIVED_DIR"] = _abs_kb_or_derived(env.get("SHERPA_DERIVED_DIR"), "data/derived")
    if ask_disabled:
        env["SHERPA_MCP_ASK_DISABLED"] = "1"
    env["SHERPA_ARMS"] = ",".join(ingest_arms.enabled_arm_names())             # 実効アームのスナップショット
    env["SHERPA_LEGACY_BACKEND"] = legacy_convert.legacy_backend_name()        # 実効バックエンドのスナップショット
    # 実効拡張子集合のスナップショット（office_com の URL/TOKEN を渡さずに済む＝W1 RV Med。soffice 検出/healthz
    # probe をサブプロセス側で一切行わせない＝legacy_exts() がこの env を最優先で信じる）。
    env["SHERPA_LEGACY_EXTS"] = ",".join(sorted(legacy_convert.legacy_exts()))
    # vision（VLM）の実効可用性スナップショット（(f) RV Med・secrets は渡さず1bitのみ）。
    env["SHERPA_VLM_USABLE"] = "1" if vision_arm.resolve_vlm() is not None else "0"
    try:
        wd = worlds.world_dir(world)
        if wd:
            env["SHERPA_MCP_WORLD_ROOT"] = str(Path(wd).resolve())
    except Exception:
        pass   # 解決不可はサブプロセス側の通常解決（fixtures/KB フォールバック等）に委ねる
    return env


def _mcp_neighbors_from(item: dict) -> list:
    """完了した graph_neighbors の mcp_tool_call item から neighbors（compact view）を取り出す（A2）。

    result.content[].text は mcp_server が `json.dumps({"neighbors":[...]})` した文字列。壊れていれば []。
    """
    try:
        text = item["result"]["content"][0]["text"]
        data = json.loads(text)
        ns = data.get("neighbors", []) if isinstance(data, dict) else []   # JSON が list/str でも .get で落とさない（RV LOW）
        return ns if isinstance(ns, list) else []
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return []


def _apply_codex_neighbors(env: dict, mcp_neighbors: list, lens) -> None:
    """A2: troubleshoot で Codex が graph_neighbors で実際に引いた近傍を UI カード(candidates)に反映。

    `_gather`（決定的）由来の candidates を **Codex の実調査由来で上書き**し、name で重複排除＋
    `summary.total` を整合させる（保存 JSON も Codex 由来で一貫・RV LOW）。troubleshoot 以外／近傍無しは無変更。
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
    """S2（ask_user-improvements.md）: Codex の mcp_tool_call(ask_user) item を、フロントに出せる
    question イベントへ丸める（ask_user 以外／非 dict は None）。

    生成は cloud LLM の ask_user と同じ `agentic_search._question_from_args` を再利用＝prompt/options を
    clip し interaction_id を付与した安全な形（chat_service が answer.question として保存＝S1 と同じ保存形）。
    """
    if not isinstance(item, dict) or item.get("tool") != "ask_user":
        return None
    from ... import agentic_search
    args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
    return agentic_search._question_from_args(args)


def _codex_ask_capture(item: dict, ask_disabled: bool) -> dict | None:
    """S2 ガード②（確認ID 付き再送では ask_user を無視）の判定を1箇所に集約した純粋関数。

    `_run_authoring`（Popen 必須・ソース検査でしか検証できない）から guard 判定だけを切り出し、
    Codex サブプロセス無しで実行ベース検証できるようにする（RV Low-3・2026-07-07）。
    ガード③（1実行1回）は呼び出し側が「既に codex_question が not None なら呼ばない」で担保する
    （捕捉した瞬間に break するので同一実行内でこの関数が2回意味を持つことはない）。
    """
    if ask_disabled:
        return None
    return _codex_ask_question(item)


def _mcp_config_args(world: str, scope_paths, ask_disabled: bool = False, layer=None) -> list:
    """codex exec に sherpa MCP サーバ(stdio)を登録する -c 引数（per-request＝~/.codex 設定を汚さない）。"""
    py = sys.executable or "python3"
    env = _mcp_env(world, scope_paths, ask_disabled, layer=layer)
    env_toml = "{" + ", ".join(f"{k} = {_toml_str(v)}" for k, v in env.items()) + "}"
    # MCP ツール承認は approval_policy と別系統（codex 0.139）。default_tools_approval_mode="approve" で
    # **sandbox(-s read-only) を保ったまま**自動承認（非対話 exec は回答者が居らず prompt だと cancel になる）。
    return ["-c", f"mcp_servers.sherpa.command={_toml_str(py)}",
            "-c", 'mcp_servers.sherpa.args = ["-m", "sherpa.mcp_server"]',
            "-c", f"mcp_servers.sherpa.env = {env_toml}",
            "-c", 'mcp_servers.sherpa.default_tools_approval_mode = "approve"',
            "-c", 'approval_policy = "never"']
