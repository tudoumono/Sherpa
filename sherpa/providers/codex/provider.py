"""`CodexProvider`（`sherpa/agents.py` から re-export される exec 核）。

Codex CLI サブプロセスの起動・思考イベントへの変換・実行ごとの作業領域管理・headline/progress 判定など、
Codex(gpt-5.5) を頭脳にする実行本体一式をまとめる。`sherpa/agents.py` が facade として本モジュール
から再エクスポートするため、まだ agents.py に残る `_select_provider`/`get_provider`/`provider_info`
（`AGENT_PROVIDERS`・`_UnwiredProvider` も同様）は無改修で動く。

**同時実行は uid 単位で直列化しない**: 実行ごとに専用の作業領域（`sandbox._safe_run_authoring` の
`authoring/run-<乱数>`）を割り当てるため、同一 uid の複数実行が snapshot・files/ move・
`.agents` rebuild で交差する心配が無い。同時実行数はチャットの受付上限（`chat_turns` 側・別契約）
だけで決まる。

**`CodexProvider.run`/`_run_authoring` は分割しない**: SSE 生成器の try/finally が唯一の
クリーンアップ保証（`run_dir` 後始末＝`_run_authoring` 本体を包む frame・attempt ループの finally＝
`_killpg`→`proc.wait(5)`・その外側の finally＝非永続セッションのみ `shutil.rmtree(codex_home)`／
永続セッションは `config.toml`・`auth.json` の削除）のため、関数を丸ごと移し生成器フレームを
分割するヘルパ抽出はしない。`'ws_authoring' in dir()`（台帳登録ゲート・フレーム内省が必要）、
last-message tempfile の `unlink` 2箇所（ask_user 早期 return・通常経路）もこの制約に従う。

**本モジュールは `sherpa` から2階層深い（providers→codex）**ため、パスの `parents[N]` は
agents.py 基準の N から +2 する（`sherpa/` 配下基準＝`parents[2]`・`_SKILLS_BASE`／repo root 基準＝
`parents[3]`・`sandbox.py` docstring 参照）。相対 import も
`from ... import marp_render`／`from ... import store as _store`／
`from ... import codex_agents_md, codex_skills` になる（参照先は変わらず
`sherpa.marp_render`/`sherpa.store`/`sherpa.codex_agents_md`/`sherpa.codex_skills`）。

**`_gather` は `_run_authoring` 内でのみ遅延 import する**（危険な継ぎ目）: `tests/unit/
test_agents_seams.py`・`tests/unit/test_agents_author.py::
test_gather_seam_intercepted_by_codex_provider` 等が `agents._gather` を monkeypatch して
`CodexProvider().run()`（→`_run_authoring`）経由の介入を検証する。本モジュールは agents.py が
facade re-export のためモジュールレベルで import するため、逆にモジュールレベルで
`from sherpa import agents` すると循環 import になる。そのため `_run_authoring` 内でのみ関数内
遅延 import `from sherpa import agents as _facade` して `_facade._gather(ctx)` と実行時解決する。
`CodexProvider.run`/`_run_authoring` が呼ぶ `_plain_run`・`_node`・`.sandbox` の各関数等、本モジュール内の
他の呼び出しは直接（`_plain_run`/`_node`/`_usage_meta`は base.py から直接 import）でよい
（危険な継ぎ目リストに無い）。

依存: `..base`（`Provider`/`Ctx`/`_log`/`_node`/`_plain_run`/`_usage_meta`）・`..prompts`
（`_facts`/`_kb_hint_abs`）・同一パッケージの `.sandbox`（サンドボックス/Marp バイナリ検出/
web_search 引数/authoring config 書込み）・`.mcp`（MCP env/config/neighbors/ask_user 変換）は
兄弟モジュールとして直接 import する（危険な継ぎ目リストに無い）。
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
import threading
from pathlib import Path
from typing import Iterator

from ... import agentic_search, codex_agents_md, codex_skills, model_catalog
from ... import depth_profile as depth_profile_mod
from ... import investigation_ledger
from ... import investigation_state
from ... import layer as layer_mod
from ...mcp_server import COMPARE_DOC_ID_ARGS, LISTED_DOC_TOOLS, READ_DOC_TOOLS
from ..base import (
    Ctx,
    Provider,
    _evidence_gate_note,
    _KINDS_OUTSIDE_LEDGER,
    _log,
    _log_codex,
    _log_chat_usage,
    _node,
    _plain_run,
    _scope_evidence_kinds,
    _usage_meta,
    _verified_sources,
)
from ..prompts import _facts, _kb_hint_abs
from .activity import (
    _is_child_session_meta,
    exec_failure_counts as _codex_exec_failure_counts,
    summarize_turn as _summarize_codex_activity,
)
from .citations import parse_referenced_doc_lines, verified_referenced_docs
from .mcp import (
    _apply_codex_neighbors,
    _codex_ask_capture,
    _codex_mcp_enabled,
    _graph_schema_era_from_item,
    _mcp_config_args,
    _mcp_env,
    _mcp_neighbors_from,
)
from .sandbox import (
    _codex_clean_env,
    _codex_sandbox_enabled,
    _detect_chrome_path,
    _direct_read_roots,
    _enumerate_sensitive,
    _kb_read_roots,
    _marp_bin,
    _openai_endpoint_kind,
    _release_active_run_dir,
    _remove_dir_best_effort,
    _safe_codex_sessions_home,
    _safe_run_authoring,
    _safe_workspace_authoring,
    _scope_deny_entries,
    _venv_root,
    _web_search_c_args,
    _web_search_endpoint_note,
    _write_codex_authoring_config,
    codex_mode,
    codex_multi_agent_enabled,
)

# 明示変更(a): skills_base（危険地雷1の5番目）。本モジュールは `sherpa` から2階層深い
# （providers→codex）ため、agents.py 基準の `Path(__file__).resolve().parent`（＝<repo>/sherpa）と
# 同じ場所を指すには `parents[2]` にする（モジュール docstring 参照）。
_SKILLS_BASE = Path(__file__).resolve().parents[2] / "skills_base"

# `--output-schema` に渡す固定スキーマファイル（docs/proposals/2026-09-08-Codex出力スキーマ.md §2-1）。
# パッケージ内に同梱（本モジュールと同じディレクトリ）。v2（DEPTH-2 S1・
# docs/proposals/2026-09-17-深さの再定義とレビュー巡.md §2.5）は v1 の3キーに `claims`
# （確定/推定/不明の主張配列）を足した版——`SHERPA_CODEX_OUTPUT_SCHEMA` の値で選ぶ。
_OUTPUT_SCHEMA_PATH = Path(__file__).resolve().parent / "output_schema.json"
_OUTPUT_SCHEMA_PATH_V2 = Path(__file__).resolve().parent / "output_schema_v2.json"


def _humanize_cmd(command: str):
    """Codex が実行したシェルコマンド → 画面の言葉＋実コマンド（detail）。"""
    inner = command
    m = re.search(r'-lc\s+"(.*)"\s*$', command) or re.search(r"-lc\s+'(.*)'\s*$", command)
    if m:
        inner = m.group(1)
    low = inner.lower()
    if "grep" in low or low.startswith("rg ") or " rg " in low:
        label = "ファイルを検索（grep）"
    elif any(k in low for k in ("cat ", "sed ", "head ", "tail ", "less ", "nl ")):
        label = "ファイルを参照"
    elif low.startswith(("ls", "find")) or " find " in low:
        label = "ファイル一覧"
    else:
        label = "コマンド実行"
    return label, inner.strip()[:140]


_MCP_SIDECAR_NAME = ".mcp_sidecar.jsonl"   # DEPTH-2 S3b: sandbox 有効時は codex_home 配下（run_dir の外）
# MCP 付き Codex 経路で決まった手順の下調べ（`_gather` の `ctx.dispatch`）を省くレンズ。MCP 版
# プロンプトは下調べの結果を Codex に渡さない（`_prompt_mcp`）うえ、qa／troubleshoot／author の下調べは
# 語ごとに資料フォルダ全体を読み直す grep で、大きなフォルダでは数分かかる。impact は省かない——
# グラフでたどる影響一覧（経路つき・網羅）は Codex のツールでは作れず、回答と並べて表示するため
# （下調べはグラフだけ・per-query timeout 付き）。
_PRESEARCH_SKIP_LENSES = frozenset({"qa", "troubleshoot", "author"})
# `codex_error_info`（`turn.failed`/`error` イベント）がこの値のとき、1回の調査で集めたツール結果
# だけで Codex の文脈枠を使い切った（会話履歴の蓄積ではない）——利用者向け文言は「範囲を絞れ」を
# 案内し、終了理由は認証/ネットワーク不調と区別して `budget`（打切りの内訳）に数える。
_CONTEXT_WINDOW_EXCEEDED_CODE = "context_window_exceeded"
# `codex_error_info` を持たない CLI 版でも文脈枠超過を見分けるための分類（`error.message` は
# **保存もログ出力もせず**ここでの判定にだけ使う——本文に資料名・抜粋が混ざり得るため）。
_TURN_FAILURE_PATTERNS = (
    (("ran out of room", "context window", "context_window"), _CONTEXT_WINDOW_EXCEEDED_CODE),
)


def _classify_turn_failure(message) -> str | None:
    """`turn.failed` の本文を固定語彙のコードへ分類する（該当しなければ `None`）。"""
    if not isinstance(message, str) or not message:
        return None
    low = message.lower()
    for needles, code in _TURN_FAILURE_PATTERNS:
        if any(n in low for n in needles):
            return code
    return None
# モデルの文脈窓（`model_context_window`）は Codex CLI に渡さない＝CLI 自身の判断（既定 272,000
# tokens・その 0.9 倍で自動圧縮）に任せる。Sherpa 側で窓を判定・登録・上書きしない（利用者裁定
# 「AI が持つ文脈窓を Sherpa が制限しない」）。大きな値を渡して CLI に切り詰めさせる方式は、実環境の
# CLI（0.147.0）が切り詰めず圧縮が一度も起きないまま API 上限で失敗したため採らない。


# Codex 経路の 1 件あたりのツール結果の上限（バイト）。管理画面の基準値（既定 256KiB・API 経路向け）を
# そのまま Codex に渡すと、日本語の設計書 256KiB は数万〜十数万トークンになり、十数件で文脈枠を
# 使い切る（実環境 0.11.0: 61 呼出・256KiB 級 15 件で context_window_exceeded・回答なし）。V0.9 が
# 使っていた値（64KiB）に固定し、管理画面の基準値とは min() で結ぶ。
_CODEX_MCP_TOOL_BUDGET_CEILING_BYTES = 64 * 1024


def _resolve_mcp_budget_env(system_settings: dict | None, model: str | None,
                            ollama_base_url: str | None, depth_profile: str | None = None) -> dict[str, str]:
    """`_resolve_mcp_budget` のラッパー（呼び出し元が env dict だけを使う場合の別名）。"""
    return _resolve_mcp_budget(system_settings, model, ollama_base_url, depth_profile)


def _resolve_mcp_budget(system_settings: dict | None, model: str | None,
                        ollama_base_url: str | None, depth_profile: str | None = None
                        ) -> dict[str, str]:
    """MCP 子プロセスへ渡す `SHERPA_MCP_TOOL_BUDGET_BYTES`／`_MAX_HITS`/`_WINDOW_CAP` を1回だけ
    解決する。

    1件あたりのバイト予算は `agentic_search.resolve_tool_result_budgets` の実効値（system_settings
    の基準値）と `_CODEX_MCP_TOOL_BUDGET_CEILING_BYTES` の min()。モデルの窓由来の上限（旧 BUDGET-2）
    は撤去済み——モデルの文脈窓（Codex CLI 任せ・Sherpa は渡さない）とは無関係。`provider` は
    `resolve_tool_result_budgets` への配線に使う語彙——Codex(Ollama) 構成のときだけ "ollama"、
    それ以外（OpenAI/Azure）は "openai"。累計（1 run 全体）のバイト予算は Codex 経路では渡さない
    （呼び出し元 docstring・モジュール docstring 参照）——管理画面の `agentic_budget_total` 設定は
    API 経路（`sherpa/agentic_search.py` の `resolve_tool_result_budgets` 呼び出し）にのみ効く。

    hits/window の2件は `OpenAIProvider._agentic_loop`（API 経路）と同じ関数・同じ引数の組み合わせ
    （`depth_profile.scaled_ratio(depth_profile.effective_base(...), profile, abs_max=...)`）で
    深さ連動込みの実効値を解決する——Codex 経路だけ `mcp_server.py::run_tool` が管理画面の基準値
    （`depth_base_grep_max_hits`/`depth_base_read_window`）も調べる深さの倍率も無視して env 既定
    （`SHERPA_GREP_MAX_HITS`/`SHERPA_READ_WINDOW`）に固定されていた不整合の是正。専用の頭打ちは
    設けない（バイト予算と違い Codex CLI 側の既知の不安定要因が無いため）。`depth_profile`
    （引数名は `ctx.scope_meta.get("depth_profile")` の値・省略時は `None`＝標準として扱う）。

    ツール呼び出し回数の上限は渡さない（`SHERPA_MCP_TOOL_MAX_CALLS` は撤去済み）——クイックも
    含め、調査を終了させる上限は設けず速さは見直し回数・推論段で表現する
    （`docs/proposals/2026-09-21-調査台帳を文脈の外に置く.md` §2）。
    """
    provider = "ollama" if ollama_base_url is not None else "openai"
    sysset = system_settings
    if sysset is None:
        try:
            from ... import store as _store
            sysset = _store.get_system_settings()
        except Exception:
            sysset = {}
    per_result, _total = agentic_search.resolve_tool_result_budgets(
        sysset, provider=provider, model=model, ollama_base_url=ollama_base_url)
    per_result = min(per_result, _CODEX_MCP_TOOL_BUDGET_CEILING_BYTES)
    max_hits = depth_profile_mod.scaled_ratio(
        depth_profile_mod.effective_base(sysset, "grep_max_hits", agentic_search.MAX_HITS),
        depth_profile, abs_max=agentic_search.MAX_HITS_ABS_MAX)
    window_cap = depth_profile_mod.scaled_ratio(
        depth_profile_mod.effective_base(sysset, "read_window", agentic_search.READ_WINDOW),
        depth_profile, abs_max=agentic_search.READ_WINDOW_ABS_MAX)
    return {
        "SHERPA_MCP_TOOL_BUDGET_BYTES": str(per_result),
        "SHERPA_MCP_TOOL_MAX_HITS": str(max_hits),
        "SHERPA_MCP_TOOL_WINDOW_CAP": str(window_cap),
    }


def _masked_run_dir_path(fp: str, run_dir: Path) -> str:
    """失敗ログに `users_dir`/uid を含むフルパスをそのまま出さない（サーバのファイルシステム配置・
    uid をログに露出させない）。`run_dir` からの相対部分だけを、run_dir 自身の識別子
    （`run-<乱数>`＝uid を含まない）に付けて返す。相対化できない（run_dir 外のパス等）場合は
    run_dir の識別子だけを返す。"""
    try:
        rel = Path(fp).resolve().relative_to(run_dir.resolve())
        return f"{run_dir.name}/{rel}"
    except (OSError, ValueError):
        return run_dir.name


def _usage_from_turn_completed(event: dict, model: str | None, *, codex_model_provider: str | None = None,
                               system_settings: dict | None = None) -> dict | None:
    """Codex `codex exec --json` の `turn.completed` イベントから usage を取り出す。

    実ログ形: `{"type":"turn.completed","usage":{"input_tokens":..,"cached_input_tokens":..,
    "output_tokens":..,"reasoning_output_tokens":..}}`。usage が無い/型不正なら None（best-effort）。

    `codex_model_provider`/`system_settings`: 呼び出し元（`_run_authoring`）が
    `self._ollama_base_url is not None` から求めた `"ollama"`/`"openai"` と、接続先解決用の
    設定スナップショットをそのまま渡す契約（`agent_constructs.is_local` の4値判定
    （local/on_prem/cloud/cloud_compat・接続先ホストの判定は `llm.endpoint_locality`）へ委ねる・
    Codex は常に `provider_id="codex"` を名乗るため、実際の接続先はここでしか分からない）。
    """
    if not isinstance(event, dict) or event.get("type") != "turn.completed":
        return None
    u = event.get("usage")
    if not isinstance(u, dict):
        return None
    from ... import agent_constructs
    return _usage_meta("codex", model,
                       input_tokens=u.get("input_tokens"),
                       cached_input_tokens=u.get("cached_input_tokens"),
                       output_tokens=u.get("output_tokens"),
                       reasoning_output_tokens=u.get("reasoning_output_tokens"),
                       is_local=agent_constructs.is_local("codex", codex_model_provider=codex_model_provider,
                                                          system_settings=system_settings))


def _accumulate_codex_usage(prev: dict | None, new: dict | None) -> dict | None:
    """自動継続で attempt をまたいだときの usage＝**最新の snapshot** を採用する（足し合わせない）。

    Codex CLI の `turn.completed.usage` はセッション累計（`last_total_token_usage.total`）で、
    `codex exec resume` はロールアウトから前回までの累計を復元してから加算する。継続 attempt の値は
    前 attempt の分を既に含むため、足すと二重計上になる。`None`（usage 無し）の attempt は無視する。
    """
    return prev if new is None else new


def _read_mcp_sidecar(path: Path) -> tuple[list, list, dict | None, list, dict]:
    """DEPTH-2 S3b: `sherpa/mcp_server.py` が書いたサイドカー（子エージェント＝`spawn_agent`
    された worker/evaluator が読んだ doc_id・ask_user の質問）を読む。呼び出し元は sandbox 有効時
    （sandbox 有効時は codex_home 配下＝model-shell の書込許可領域の外。無効時＝`SHERPA_CODEX_SANDBOX=0`
    の緊急避難経路は run_dir 配下 `.tmp/`＝この経路自体に封じ込めが無いため shell からも書けるが、
    元々 OS ユーザ分離のみに頼る経路として許容している）。子の MCP 呼出は
    親の `--json` に構造化イベントとして現れない（実機確認済み・`docs/notes/
    2026-09-17-DEPTH-2-S3-Codex-multi_agent-実機確認.md` (d)(e)）ため、これが唯一の観測経路。

    サイドカーが無い／壊れている（存在しない・行が壊れた JSON・型不正・不正 UTF-8 バイト列）ときは
    **fail-open**（既存の「親の --json だけを見る」観測にそのまま落ちる＝空リスト／None を返すだけで
    例外は出さない・回答処理を例外終了させない）。

    戻り値: `(read_doc_ids, listed_doc_ids, ask_user_question_or_none, error_codes, limits)`。
    `ask_user_question` は最初の1件だけ（1実行1回＝`mcp_server.py` 側の既存ガードと同じ数え方）。
    `error_codes` は子が受け取った障害の閉じたコード（`mcp_server._SIDECAR_ERROR_CODES`・出現順・
    重複なし）——親の調査状態（縮退の通知・統計）へ合流させるための唯一の観測経路。
    `limits`（Azure 実機の context_window_exceeded 是正・同一クエリの重複実行の抑止・`run_tool()`
    自身の内部切り詰め・ツール呼び出し回数の上限到達）は `mcp_server.py` が書いた
    `{"kind": "limit", "field": "tool_result_clipped"|"total_budget_hit"|"duplicate_tool_call"|
    "search_truncated"|"tool_calls_exhausted"}` を集計した `{"tool_result_clipped": <件数>,
    "total_budget_hit": <bool>, "duplicate_tool_call": <件数>, "search_truncated": <件数>,
    "tool_calls_exhausted": <bool>}`——利用統計「打切りの内訳」（`env["limits"]`）へ合流させるための
    唯一の観測経路。
    """
    reads: list = []
    listed: list = []
    ask: dict | None = None
    error_codes: list = []
    limits = {"tool_result_clipped": 0, "total_budget_hit": False, "duplicate_tool_call": 0,
             "search_truncated": 0, "tool_calls_exhausted": False}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("kind")
                if kind == "read":
                    d = entry.get("doc_id")
                    if isinstance(d, str) and d:
                        reads.append(d)
                elif kind == "listed":
                    d = entry.get("doc_id")
                    if isinstance(d, str) and d:
                        listed.append(d)
                elif kind == "ask_user" and ask is None:
                    q = entry.get("question")
                    if isinstance(q, dict):
                        ask = q
                elif kind == "error":
                    c = entry.get("code")
                    if isinstance(c, str) and c and c not in error_codes:
                        error_codes.append(c)
                elif kind == "limit":
                    field = entry.get("field")
                    if field == "tool_result_clipped":
                        limits["tool_result_clipped"] += 1
                    elif field == "total_budget_hit":
                        limits["total_budget_hit"] = True
                    elif field == "duplicate_tool_call":
                        limits["duplicate_tool_call"] += 1
                    elif field == "search_truncated":
                        limits["search_truncated"] += 1
                    elif field == "tool_calls_exhausted":
                        limits["tool_calls_exhausted"] = True
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError は `for line in f` の読取自体（`json.loads` の外）で起きうる
        # （不正 UTF-8 バイト列を含む行）。ここまでに集めた分は返す（部分的な fail-open）。
        pass
    return reads, listed, ask, error_codes, limits


_CHILD_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def _collect_child_token_usage(codex_home: Path, child_thread_ids: set,
                                parent_thread_id: str | None = None,
                                min_mtime: float | None = None) -> tuple[dict, int, int, int]:
    """DEPTH-2 S3b: `spawn_agent` した子スレッドの usage を、子ごとの session JSONL
    （`codex_home/sessions/**/*.jsonl`）から集める。親の `turn.completed.usage` には子の分が
    含まれない契約（実機確認・約33%の過小計上・
    `docs/notes/2026-09-17-DEPTH-2-S3-Codex-multi_agent-実機確認.md` (g)）。

    「起動を検出した」ことと「usage を読めた」ことは別の判定として区別する——子がまだ走行中
    （token_count がまだ無い）・usage を書く前に落ちた rollout は、起動自体は確認できるが usage
    は取れない。前者だけで missing を判定すると「起動していない」と「起動したが usage 未取得」が
    区別できず、後者を無かったことにしてしまう。

    子の**検出**（`detected_ids`）は2通りの和集合（どちらかで拾えれば検出とみなす・`--json` の
    出力形式が変わっても rollout の `session_meta` は CLI 自身が書く一次記録として安定している
    ため、こちらを主に使う）:
    (a) 新形式: 先頭行 `session_meta.payload.parent_thread_id` が今回の親 thread id と一致し、
        かつ `payload.thread_source == "subagent"`、かつファイルの mtime が `min_mtime` 以降
        （resume は同じ thread id を跨ターンで使い回す——`session_meta.payload.id` だけで
        絞ると、resume で続いた別ターンの古い子を今ターンの子として再計上してしまう。
        `min_mtime`＝今ターン開始時刻を渡すことで前ターン分を除外する）。
    (b) 旧形式: 先頭行 `session_meta.payload.id` が `spawn_agent` の `collab_tool_call` item から
        捕捉した `child_thread_ids` に含まれる（この id 自体が今ターンの捕捉分のみのため
        `min_mtime` は不要）。

    検出した子のうち、各ファイルの**最後**の `token_count` イベント
    （`payload.info.total_token_usage`＝そのスレッドのセッション累計）が実際にあるものだけ
    `found_ids` へ入れ usage を1子につき1回だけ合算する（同じ子 id のファイルが複数（mtime
    新しい方を優先）見つかっても二重に数えない）。`child_thread_ids`（旧形式の期待値）に載って
    いるのに rollout 自体が見つからない id も含め、検出できたのに usage が取れなかった id を
    `missing` に残す（**推定で埋めない**）。壊れた JSONL・欠損フィールド・存在しない codex_home
    はすべて fail-open（例外を出さず「見つからなかった」扱いにする）。

    戻り値: `(totals, found, missing, detected)`——`detected` は起動を確認できた子の総数
    （呼び出し側の `spawn_agents` に使う・`found + missing` と一致する）。
    """
    totals = {k: 0 for k in _CHILD_USAGE_KEYS}
    if not child_thread_ids and not parent_thread_id:
        return totals, 0, 0, 0
    detected_ids: set = set()
    found_ids: set = set()
    remaining_old = set(child_thread_ids)
    try:
        cands = sorted(codex_home.glob("sessions/**/*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        cands = []
    for path in cands:
        if not remaining_old and not parent_thread_id:
            break
        try:
            with open(path, "r", encoding="utf-8") as f:
                first = f.readline()
                meta = json.loads(first)
                payload = meta.get("payload") if isinstance(meta, dict) else None
                tid = payload.get("id") if isinstance(payload, dict) else None
                if tid is None or tid in detected_ids:
                    continue
                # 子判定は `activity.py::_is_child_session_meta` 1箇所だけに持つ（新形式/旧形式の
                # 和集合・`codex/activity.py` の要約もこの関数を呼ぶ＝判定の二重実装をしない）。
                # `tid in remaining_old` と `tid in child_thread_ids` は上の
                # `tid in detected_ids` ガードにより本判定時点で同値（見つかった旧形式 id は
                # 同じ行で `detected_ids`/`remaining_old` 双方に反映されるため）。
                if not _is_child_session_meta(payload, tid, child_thread_ids=child_thread_ids,
                                              parent_thread_id=parent_thread_id, min_mtime=min_mtime,
                                              file_mtime=path.stat().st_mtime):
                    continue
                detected_ids.add(tid)
                remaining_old.discard(tid)
                last_usage = None
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    ep = e.get("payload") if isinstance(e, dict) else None
                    if isinstance(ep, dict) and ep.get("type") == "token_count":
                        info = ep.get("info")
                        u = info.get("total_token_usage") if isinstance(info, dict) else None
                        if isinstance(u, dict):
                            last_usage = u
                if last_usage is not None:
                    found_ids.add(tid)
                    for k in _CHILD_USAGE_KEYS:
                        totals[k] += int(last_usage.get(k) or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    all_detected = detected_ids | child_thread_ids   # rollout 自体が見つからなかった旧形式 id も含める
    return totals, len(found_ids), len(all_detected - found_ids), len(all_detected)


def _killpg(proc) -> None:
    """MCP subprocess / shell child まで確実に殺す（creds env の寿命を延ばさない）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


_SESSION_REAP_ATTEMPTS = 5      # 固定回数（無限リトライにしない）
_SESSION_REAP_INTERVAL_S = 0.05  # 数十ミリ秒


def _kill_session(sid: int) -> None:
    """`sid` を session id に持つプロセスを、残りが無くなるまで数回 SIGKILL する。呼び出し側は
    `start_new_session=True` で起動した Popen の pid を渡す（setsid の仕様上、その pid がそのまま
    session id になる）。

    なぜプロセスグループでなくセッションで回収するか: codex サンドボックス内部の子が
    `setpgid(0,0)` で別プロセスグループへ移った場合、`_killpg`（プロセスグループ宛）はもう届かない。
    さらにその子は新しい PID 名前空間の init として振る舞うためシグナルハンドラが無く、SIGTERM も
    効かない（SIGKILL だけが届く）。setsid が作った session だけは setpgid の影響を受けず残るため、
    session 番号でだけは串刺しに捕捉できる。

    **呼び出し側の前提**: このプロセス（sid の元になった Popen）を `wait()` で回収する**前**に
    呼ぶこと。回収する（reap する）までその pid はゾンビとしてカーネルに予約され続け、同じ番号で
    別のセッションが新規に作られることは無い——先に回収してしまうと、その隙に pid が無関係な
    プロセスへ再利用され、たまたま同じ番号で新しいセッションが立っていた場合に誤爆しうる（呼び
    出し側の責務・本関数はこの前提の成立を検証しない）。この前提により sid 自身（リーダー）は
    呼び出しが返るまでゾンビのまま存在し続けるため、対象からは除く（呼び出し側の `_killpg`/
    `wait()` が別途担当・含めると reap されるまで毎回自分自身にマッチし続け、他に何も残って
    いなくても毎回 attempts の上限まで回ってしまう）。

    判定（session 一致）から送信（SIGKILL）までの間に対象自身が終わり pid が再利用されると、
    無関係な別プロセスへ誤って送ってしまう——これは判定より前に確保した pidfd 越しに
    `pidfd_send_signal` で送ることで防ぐ（pidfd は確保した瞬間のプロセスに固定され、番号の
    再利用があっても別プロセスには届かない）。

    pidfd が使えない環境（関数が無い・カーネル未対応＝ENOSYS）や `/proc` が無い/読めない環境では
    何もしない（fail-open。pid 番号だけで送る旧方式へは戻さない）。自分自身（Sherpa のプロセス）と
    このセッションに属さないプロセスには絶対に触らない。
    """
    if sid <= 0:
        return
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        _log.warning("codex session reap: pidfd 未対応環境のため何もしません（AttributeError）sid=%s", sid)
        return
    my_pid = os.getpid()
    try:
        for _ in range(_SESSION_REAP_ATTEMPTS):
            try:
                candidates = [e for e in os.listdir("/proc") if e.isdigit()]
            except OSError:
                return   # /proc が無い/読めない環境（fail-open・何もしない）
            matched = 0
            for entry in candidates:
                pid = int(entry)
                # sid 自身（セッションリーダー）は呼び出し側の `_killpg`/`proc.wait()` が
                # 別途担当する——ここで対象にすると、呼び出し側がまだ reap していない間は
                # 毎回自分自身にマッチし続け、他に何も残っていなくても毎回 attempts の上限
                # まで回ってしまう（早期 return が効かなくなる）。
                if pid == my_pid or pid == sid:
                    continue
                try:
                    fd = os.pidfd_open(pid, 0)
                except OSError as exc:
                    if getattr(exc, "errno", None) == errno.ENOSYS:
                        _log.warning(
                            "codex session reap: pidfd_open が ENOSYS（カーネル未対応）のため中断します sid=%s",
                            sid)
                        return
                    continue   # ESRCH 等（走査中に対象が消えた）は通常経路——次候補へ
                try:
                    try:
                        with open(f"/proc/{entry}/stat", "r", errors="replace") as f:
                            raw = f.read()
                    except OSError:
                        continue   # 走査中にプロセスが消えるのは通常経路
                    # comm フィールドは括弧内で空白/括弧を含み得るため、最後の ')' を境に固定
                    # オフセットで読む（man proc(5) の推奨手順）。境より後ろ: state ppid pgrp session ...
                    paren = raw.rfind(")")
                    if paren == -1:
                        continue
                    fields = raw[paren + 2:].split()
                    if len(fields) < 4:
                        continue
                    try:
                        proc_sid = int(fields[3])
                    except ValueError:
                        continue
                    if proc_sid != sid:
                        continue
                    matched += 1
                    try:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    except OSError:
                        pass
                finally:
                    os.close(fd)
            if not matched:
                return
            time.sleep(_SESSION_REAP_INTERVAL_S)
    except Exception as exc:
        _log.warning("codex session reap failed: %s errno=%s sid=%s",
                     type(exc).__name__, getattr(exc, "errno", None), sid)


def _spawn_stop_watcher(proc, stop_event, reap_lock, reaped) -> "threading.Thread":
    """途中停止: `for line in proc.stdout` はブロッキング read のため、
    `stop_event` を単にチェックするだけでは（次の行が来るまで）反応できない。別スレッドで stop_event を
    監視し、立ったら即 `_killpg` で子プロセスごと殺す＝stdout を EOF にしてブロック中の read を
    即座に解放する（サブプロセスを安全に打ち切る唯一の確実な方法・EventSource.close() はサーバ側の
    ブロッキング処理を止めない＝調査済）。`stop_event` が None（途中停止を使わない呼び出し）でも
    常に起動する——下記の pipe 閉じ役を自然終了のケースでも必要とするため。

    プロセスが自然終了した場合もスレッドは自分で抜ける（daemon なのでプロセス全体の終了も妨げない）。
    呼び出し側（`CodexProvider.run`）は生成したスレッドを明示的に join する必要はない（自然終了/kill
    いずれでも自己終結する）。

    終了検知に `proc.poll()` を使わない: `.poll()` は完了を検知すると同時にプロセスを reap して
    しまう（`_attempt` の finally が「`_kill_session` を呼んでから `proc.wait()` で reap する」
    順序を前提にしているのに、このスレッドが先に reap してしまうと sid の pid が空く隙ができる）。
    `os.waitid(..., WNOWAIT)` で reap せずに終了だけを確認する（実際の reap は finally の
    `proc.wait()` の1箇所だけに保つ）。

    リーダーの終了/停止を検知したら（reap する前に）`_kill_session` を呼んでから抜ける:
    別プロセスグループへ移った子が `proc.stdout`（pipe）を継承したまま生き残っていると、
    リーダー自身が終わっても pipe の書き込み端が閉じず、`for line in proc.stdout` が EOF に
    ならないまま止まり続け、`_attempt` の finally（後始末そのもの）に到達できなくなる。ここで
    先にその子を session ごと片付けることで pipe を閉じさせ、read ループを EOF で終わらせる
    （finally 側の `_kill_session` 呼び出しは残したまま＝二重に呼んでも冪等・無害）。

    `reap_lock`/`reaped`（`_attempt` が持つ、この attempt 専用のロックと「回収済み」の印）:
    finally（reap する側）とこのスレッド（waitid で覗く側）は非同期に動くため、finally が
    `_kill_session`→`wait()`（reap）を終えた直後にこのスレッドが古い判定のまま `_kill_session`
    を呼ぶと、reap 後は sid が再利用され得るぶん無関係なプロセスを殺しかねない。判定から
    `_kill_session` 実行までを `reap_lock` で finally 側と直列化し、`reaped["done"]` が立って
    いれば（または waitid が ECHILD＝既に reap 済みなら）このスレッドは何もせず戻る。
    """
    def _watch(_proc=proc, _ev=stop_event, _lock=reap_lock, _reaped=reaped):
        while True:
            with _lock:
                if _reaped["done"]:
                    return
                try:
                    exited = os.waitid(
                        os.P_PID, _proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
                except ChildProcessError:
                    return   # 既に別経路（finally）で reap 済み（ECHILD）＝ sid には触れない
                if exited:
                    _kill_session(_proc.pid)   # pipe を握ったまま残る子を片付けて EOF にする
                    return
            if _ev is not None and _ev.wait(timeout=0.3):
                with _lock:
                    if not _reaped["done"]:
                        _killpg(_proc)
                        _kill_session(_proc.pid)
                return
            elif _ev is None:
                time.sleep(0.3)
    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    return t


_LAST_MESSAGE_MAX_BYTES = 16 * 1024 * 1024   # 最終メッセージの保険読取のメモリ保護（回答の長さを切る目的ではない）


def _read_last_message_fallback(path: Path) -> str | None:
    """§3: `-o <path>` で Codex が書く最終メッセージファイルを読む（`--json` の
    `agent_message` 抽出が空だった時の保険）。無い/空/読取失敗は None（呼び出し側は既存の
    決定的回答フォールバックへ委ねる）。ファイルの削除は呼び出し側の責務（ここでは行わない）。

    `.tmp/` は authoring 配下（Codex の書込対象）＝サブプロセスや将来の
    変更で symlink が紛れ込む余地を否定できないため、`O_NOFOLLOW` で symlink を拒否（TOCTOU の無い
    アトミックな判定）・通常ファイルのみ・サイズ上限つきで読む（巨大ファイル/デバイスファイル等を
    誤って answer に取り込まない）。
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0 or st.st_size > _LAST_MESSAGE_MAX_BYTES:
            return None
        data = os.read(fd, st.st_size)
    except Exception:
        return None
    finally:
        os.close(fd)
    txt = data.decode("utf-8", errors="replace").strip()
    return txt or None


# ---- 回答 headline の選び方（進行中の作業宣言を見出しにしない）----
# Codex は調査中に「これから〜する」という進行形の作業宣言を agent_message として複数回出すことがあり、
# run が途中終了すると **最後に届いた作業宣言**（実例:「…根拠の有無を切り分けます」）が
# env["headline"] になってしまう（結論でなく本文途中の一文が見出しに出る）。LLM を使わず決定的に、
# 「結論を含む最後の agent_message」を優先し、末尾の作業宣言を落として選ぶ。
# 注: 語尾は「これから調べる」という**次アクション動詞**に限定する（curated list）。汎用の「〜します」
# 全部を弾くと所見（「波及します」「影響します」等）まで落ちて結論を消してしまうため入れない。
_PROGRESS_VERBS = (
    "確認します", "切り分けます", "調べます", "特定します", "検討します", "探します",
    "洗い出します", "整理します", "確かめます", "突き止めます", "チェックします", "見ていきます",
    "精査します", "分析します", "追います", "たどります", "把握します", "収集します", "集めます",
    "比較します", "検証します", "調査します", "確認していきます", "見ます",
)
_PROGRESS_END_RE = re.compile(
    "(?:" + "|".join(map(re.escape, _PROGRESS_VERBS)) + r")[。.!！\s]*$")
# 語尾が作業宣言でも「単文の事実記述」（例:「NIGHTLY は税率マスタを起動時に
# 確認します。」）を progress と誤判定して結論を捨てないよう、判定を絞る。手順マーカー（これから何をやる、
# という順序表現）で始まる文は明確に作業宣言。
_PROGRESS_MARKERS = (
    "まず", "次に", "続いて", "これから", "今から", "この後", "最後に", "では", "それでは",
)
# 「調べてから伝える」型の宣言語尾（「これから関連資料を確認し、結果を報告します」）。手順マーカーで
# 始まる文に限って次アクション扱いにする。`_PROGRESS_VERBS` には入れない: 結論の末尾文
# （「影響範囲は夜間バッチのみであることを共有します」）を `_trim_trailing_progress` が落としてしまう。
_REPORT_BACK_VERBS = ("報告します", "お伝えします", "まとめます", "回答します", "共有します")
_REPORT_BACK_END_RE = re.compile(
    "(?:" + "|".join(map(re.escape, _REPORT_BACK_VERBS)) + r")[。.!！\s]*$")
# 報告系語尾に付けるマーカーからは「最後に」を除く: 「最後に、影響は夜間バッチのみであることを共有します」
# は結論の締めであって次アクションではない。
_REPORT_BACK_MARKERS = tuple(m for m in _PROGRESS_MARKERS if m != "最後に")


def _is_next_action_sentence(s: str) -> bool:
    """文が次アクション宣言か（作業宣言語尾・または手順マーカー付きの「結果を報告します」型）。"""
    return bool(_PROGRESS_END_RE.search(s)) or (
        s.startswith(_REPORT_BACK_MARKERS) and bool(_REPORT_BACK_END_RE.search(s)))


def _is_progress_only(text: str) -> bool:
    """text の全ての文が次アクション宣言（作業を『これからやる』）なら True＝結論文が1つも無い。

    句点/改行で文に割り、いずれも進行形の作業宣言で終わることが前提。そのうえで High-1（RV）:
    「単文の事実記述」を巻き込まない（＝新しい方の message を残すのを安全側とする）ため、
    (a) いずれかの文が手順マーカー（まず/次に/…）で始まる、または (b) 文が2つ以上ある、
    のいずれかを満たすときだけ「作業宣言だけの message」とみなす。単文・マーカー無しは False。
    """
    sents = [s.strip() for s in re.split(r"[。\n]+", text) if s.strip()]
    if not sents:
        return True
    if not all(_is_next_action_sentence(s) for s in sents):
        return False
    return any(s.startswith(_PROGRESS_MARKERS) for s in sents) or len(sents) >= 2


def _trim_trailing_progress(text: str) -> str:
    """単一段落（改行なし）の平文に限り、末尾の連続する作業宣言文を落として結論で締める。

    改行や箇条書き（Markdown）を含む場合は構造を壊さないためそのまま返す（②の安全側）。
    末尾を削って空になる（＝全部が作業宣言）の場合も元文を返す（呼び出し側の _is_progress_only 判定で
    別 message が選ばれるため通常ここには来ないが、保険）。
    """
    if "\n" in text:
        return text
    parts = [p for p in re.findall(r"[^。]*。|[^。]+$", text) if p.strip()]
    while len(parts) > 1 and _PROGRESS_END_RE.search(parts[-1].strip()):
        parts.pop()
    return "".join(parts).strip() or text


def _pick_codex_headline(completed: list[str], partial: str = "", prefer_marker: str | None = None) -> str:
    """集めた複数の agent_message から headline を決定的に選ぶ（LLM 不使用）。

    `prefer_marker`（素の Codex 用）: この文字列を含む message があれば、その最後のものを優先する
    （回答の後に届いた通知への短い返事などを回答として拾わない・指示で最終回答に必ず付けさせる行）。

    ①結論を含む最後の message を優先（末尾が作業宣言でも、その中の結論／それ以前の結論を拾う）。
    ②その message の末尾に連なる作業宣言文は落とす（`_trim_trailing_progress`）。
    ③どの message も作業宣言だけなら、最後の message をそのまま返す（本文先頭＝best effort）。
    `partial`＝item.updated だけ来て item.completed が来なかった未完 message（打ち切り時の保険）。
    """
    msgs = [m.strip() for m in [*completed, partial] if m and m.strip()]
    if not msgs:
        return ""
    if prefer_marker:
        for m in reversed(msgs):
            if prefer_marker in m and not _is_progress_only(m):
                return _trim_trailing_progress(m)
    for m in reversed(msgs):
        if not _is_progress_only(m):
            return _trim_trailing_progress(m)
    return msgs[-1]


# 「作業報告＋次アクション」型の途中経過（例:「現在、関連資料を確認しました。次に影響範囲を調べます。」）。
# 完了形の作業報告は `_is_progress_only` では結論文に見えるため、明示的な次アクション文を伴い、
# 残りの文が全部この完了形の作業報告なら途中経過とみなす（自動継続の判定専用・見出しの選び方は変えない）。
# 語尾は「調べ終えた作業の報告」に限定する curated list（「〜であることを確認しました」のような
# 所見も巻き込むが、次アクション文を伴う時だけ効くので、続けさせて損は無い）。
_REPORT_VERBS = (
    "確認しました", "確認済みです", "調べました", "調査しました", "特定しました", "把握しました",
    "整理しました", "洗い出しました", "検索しました", "取得しました", "読みました", "精読しました",
    "収集しました", "集めました", "検証しました", "比較しました", "分析しました", "チェックしました",
    "たどりました", "追いました", "見ました", "見つけました", "確かめました",
)
_REPORT_END_RE = re.compile(
    "(?:" + "|".join(map(re.escape, _REPORT_VERBS)) + r")[。.!！\s]*$")


def _is_report_with_next_action(text: str) -> bool:
    """全文が「完了形の作業報告」または「次アクション宣言」で、**両方を少なくとも1文ずつ**含む。

    次アクション宣言だけの message は `_is_progress_only` の領分（単文・マーカー無しの「〜を確認します」は
    事実記述の可能性があるため、そちらでは意図的に結論扱い）。ここで作業報告文も必須にするのは、
    その単文事実記述をこの規則で拾い直して結論を続行させないため。
    """
    sents = [s.strip() for s in re.split(r"[。\n]+", text) if s.strip()]
    if not sents:
        return False
    has_next = any(_is_next_action_sentence(s) for s in sents)
    has_report = any(_REPORT_END_RE.search(s) and not _is_next_action_sentence(s) for s in sents)
    if not (has_next and has_report):
        return False
    return all(_is_next_action_sentence(s) or _REPORT_END_RE.search(s) for s in sents)


def _needs_continuation(completed: list[str], partial: str = "") -> bool:
    """集めた agent_message が1件以上あり、それらを1本に連結したテキストが途中経過（作業宣言だけ・
    または作業報告＋次アクション）で結論文が1つも無いなら True。

    まず message 単位で保護する——**単文・手順マーカー（`_PROGRESS_MARKERS`）で始まらない・
    `_PROGRESS_END_RE` に合う** message（例:「NIGHTLY は税率マスタを起動時に確認します。」のような
    単文の事実記述）が1つでもあれば、連結を待たず False（結論あり）とする。`_is_progress_only` の
    単文保護と同じ判定を、連結前の各 message にも及ぼす（連結すると他の message の語尾に埋もれて
    見落とすため）。

    それ以外は**全 message を連結**して判定する——「資料を確認しました」（作業報告）と
    「次に調べます」（次アクション）が別 message に分かれていても、連結すれば
    `_is_report_with_next_action` が拾える（message 単位の判定だと前者が単文の結論扱いになり
    見落とす）。作業宣言だけの message は `_pick_codex_headline` が規則③（最後の1件をそのまま返す）
    に落ちる条件と同じ（空/空白のみの message は対象外・1件も無ければ False＝別経路（silent failure
    等）に任せる）。
    """
    msgs = [m.strip() for m in [*completed, partial] if m and m.strip()]
    if not msgs:
        return False
    for m in msgs:
        sents = [s.strip() for s in re.split(r"[。\n]+", m) if s.strip()]
        if (len(sents) == 1 and not sents[0].startswith(_PROGRESS_MARKERS)
                and _PROGRESS_END_RE.search(sents[0])):
            return False
    joined = "\n".join(msgs)
    return _is_progress_only(joined) or _is_report_with_next_action(joined)


# 原本と変換済みテキストの保護。読み取り専用はサンドボックス（permission profile の read）が強制し、
# 指示は多層防御として全モード・全レンズに常置する。
_READ_ONLY_SENTENCE = (
    "原本と変換済みテキストは読むだけで、書き換え・上書き・移動・削除・名前の変更を絶対にしない。"
    "Word・Excel などの原本を自分で変換しない（変換済みテキストを読む）。"
)
# 設計書と実装の両面で確かめる（全モード共通）。AGENTS.md にも同じ規律があるが、書出し失敗
# （fail-open）でも消えないようプロンプトにも常置する。
_BOTH_SIDES_SENTENCE = (
    "設計書と実装（ソース）の両方で確かめ、それぞれの根拠（ファイル:行）を示して答える。"
    "食い違えば両方を並べて『食い違い』と書く（実装を正とする）。見たソースが画面・バッチ・SQL の"
    "どれかを明記し、一部のソースだけで全体を判断しない（確かめられなかった点はそう書く）。"
)
# 作成の依頼（lens=author＝画面で「資料を作成」を選んだ／依頼文が作成と判定された）以外のターンは
# ファイルを作らせない。作っても Sherpa は成果物に登録せず、作業フォルダごと消す（`_run_authoring`）。
_NO_FILES_SENTENCE = (
    "この依頼ではファイルを作らない（作業用のファイルは `.tmp/` の下に作る・終われば消える）。"
    "ファイルでほしいと頼まれたら、内容は本文に書き、『資料を作成』を選んで依頼し直すよう案内する。"
)

_CONTINUE_PROMPT = (
    "続けてください。途中経過の報告ではなく、調査を最後まで進めて最終回答（結論と根拠）を書いてください。"
)
# 出力スキーマ有効時（`_schema_on`）だけ使う継続プロンプト——AGENTS.md が構造化応答（`status`／`answer`／
# `next_step`）を求めているのはスキーマ有効時だけなので、継続の催促もその語彙に合わせる（§2-3）。
_CONTINUE_PROMPT_SCHEMA = (
    "続けてください。途中経過の報告ではなく、調査を最後まで進めて `status` を `final` にした"
    "最終回答（結論と根拠）を書いてください。ただし全件・一覧・すべての依頼で対象範囲の確認が"
    "終わっていなければ `final` にせず、`in_progress` のまま `next_step` に残りを書いてください。"
)

# 調査台帳ゲート（docs/proposals/2026-09-21-調査台帳を文脈の外に置く.md §2/§4/§6・
# `investigation_ledger.py` が判定の純関数部分）: `status=final` を機械的にそのまま信じず、台帳
# （`run_dir/.tmp/investigation/`）が完了しているかを確認してから受理する。既存の自動継続
# （`SHERPA_CODEX_AUTO_CONTINUE`・「in_progress のまま尽きた」を検出する別枠）とは独立の上限。
_LEDGER_CONTINUE_CAP = 10
_LEDGER_MANIFEST_MISSING_PROMPT = (
    "調査台帳を ledger_manifest_set と ledger_item_put で登録してから続けてください。"
    "ファイルを直接書かないでください。"
)
# RV 中-2（2026-09-22 4巡目是正）: manifest.json が**存在するが内容が規約に合わない**（必須キー
# 欠落・`items` が空等）場合は「未作成」（1回だけ催促）と区別する——正典§4「壊れた台帳から final
# を生成しない」に沿って、通常の台帳継続と同じ枠（`_LEDGER_CONTINUE_CAP`）で修復を促す。
_LEDGER_MANIFEST_INVALID_PROMPT = (
    "manifest.json が規約に合いません。ledger_manifest_set に question_kind と全 id の items を"
    "渡して修復してください（items は空にしない）。ファイルを直接書かないでください。"
)


def _ledger_tool_detail_manifest(a: dict) -> str:
    items = a.get("items")
    return f"{len(items)}件" if isinstance(items, list) else ""


def _ledger_tool_detail_item(a: dict) -> str:
    status = a.get("status")
    known = investigation_ledger.NON_TERMINAL_STATUSES | investigation_ledger.TERMINAL_STATUSES
    # 引数はモデル生成で型の保証がない。非文字列（list/dict は unhashable）を集合照合に通すと
    # TypeError がストリーム処理ごと Codex を止めるため、先に型で弾く。
    return f"状態: {status}" if isinstance(status, str) and status in known else ""


# 「思考の流れ」の台帳ツール行の補足。id・subject・reason 等のモデル生成文字列は出さない
# （件数と状態語彙＝閉集合だけ）。
_LEDGER_TOOL_DETAILS = {
    "ledger_manifest_set": _ledger_tool_detail_manifest,
    "ledger_item_put": _ledger_tool_detail_item,
    "ledger_status": lambda a: "",
}


def _ledger_continue_prompt(verdict: investigation_ledger.Verdict) -> str:
    """未完了の台帳へ、idと未充足の根拠種別だけを返す。本文・pathは含めない。"""
    def _join(ids: tuple) -> str:
        return "、".join(ids) if ids else "なし"
    unsatisfied = "、".join(f"{item_id}（{'・'.join(kinds)} が未確認）"
                         for item_id, kinds in sorted(verdict.unsatisfied.items())) or "なし"
    return (
        "調査台帳に未完了の項目があります。"
        f"未完了: {_join(verdict.non_terminal_ids)}。"
        f"無効: {_join(verdict.invalid_ids)}。"
        f"欠落: {_join(verdict.missing_ids)}。"
        f"未充足: {unsatisfied}。"
        "これらを終端状態にしてから最終回答を返してください。台帳に無い新しい主張は書かないこと。"
    )


def _ledger_source_required_extra(world: str, scope_paths, layer) -> tuple[str, ...]:
    """このターンの台帳ゲートへ渡す `required_extra`（CLAUDE.md コンセプト「ソースは常に必須」）。
    範囲にソースがある調査は、item 自身の `required_checks` 宣言に source が無くても
    `ledger_complete()`/`no_progress()` の完了判定へ source を機械的に足す。`layer` は呼び出し側が
    MCP へ実際に渡す実効の層（qa 以外は層なし＝`None`）を渡すこと——`_apply_codex_evidence_gate`
    用の `layer_mod.effective_layer` とは異なり、author にも scope_meta の生の layer をそのまま
    渡さない（author への MCP 探索は層を強制しないため、scope_meta.layer=docs でも実際には
    source を探索できる）。

    `layer == "docs"` なら、走査の成否に関わらず source を必須にしない——MCP のソース読取
    （`ripgrep_search`／`read_around` 等）そのものが層で拒否されるため、範囲に実在しても
    Codex は読めない。ここで判定不能（`None`）を安全側＝必須へ倒すと、資料だけの調査でも
    解決できない催促を出し続けてしまう。

    それ以外の層では `_apply_codex_evidence_gate` と同じ `_scope_evidence_kinds(world,
    scope_paths, layer)` の1つ目の要素（登録範囲に実在するか）で判定する（判定基準を2通り
    持たない）。走査が判定不能（`None`）なら fail-safe で source を必須のままにする
    （`_apply_codex_evidence_gate` の `unavailable` 判定と同じ安全側）。
    """
    if layer == "docs":
        return ()
    scope_kinds = _scope_evidence_kinds(world, scope_paths, layer)
    if scope_kinds is None or "source" in scope_kinds[0]:
        return ("source",)
    return ()


def _ledger_progressed(
        prev_snapshot: investigation_ledger.LedgerSnapshot,
        curr_snapshot: investigation_ledger.LedgerSnapshot,
        *, required_extra: tuple[str, ...] = ()) -> bool:
    """`prev_snapshot` から `curr_snapshot` への間に「進捗」があったかを判定する純関数
    （台帳ゲートの無進捗 streak リセット判定・RV 中-1/中-2・2026-09-22 7巡目是正）。

    RV 中-1（7巡目）: 比較対象を非終端集合だけにすると、登録済みだがファイル未作成
    （`missing`）や壊れている（`invalid`）item が解決しても「終端集合が縮んだ」ことにならず
    見逃す——未解決集合 U = 非終端 ∪ 欠落 ∪ 無効（`ledger_complete()` の各報告欄はすでに登録
    集合との積集合＝未登録 item は混ざらない）で比較し、`U_prev − U_curr` が空でなければ
    進捗ありとする（欠落だった item にファイルが作られた／無効だった item が有効になった／
    非終端が終端になった、いずれも「前は未解決だったが今は解決した」という共通の形）。

    RV 中-2（7巡目）: 未解決集合が縮んでいなくても、`no_progress()`（status/evidence/reason が
    個々に無変化かの判定）の結果を**登録済み id に限定**してから、登録済みの非終端集合と比較する
    ——`no_progress()` 自体は `snapshot.items`（未登録 item も含む）を見るため、絞り込まずに
    比較すると未登録 item の有無・変化がノイズになり、登録済み item が実際には無進捗でも
    「一致しない」と誤認して常に進捗ありと判定してしまう。

    `required_extra`: 呼び出し側の `ledger_complete()` と**同じ値**を渡すこと——`ledger_complete`
    と `no_progress` のどちらかにだけ足すと、追加種別で非終端扱いになった item を「進捗あり」と
    誤判定する（このターンで一度も変わっていないのに、片方の判定だけが未充足を追加で見るため
    `stalled` と `curr_verdict.non_terminal_ids` が食い違う）。
    """
    prev_verdict = investigation_ledger.ledger_complete(prev_snapshot, required_extra=required_extra)
    curr_verdict = investigation_ledger.ledger_complete(curr_snapshot, required_extra=required_extra)
    prev_unresolved = (set(prev_verdict.non_terminal_ids) | set(prev_verdict.missing_ids)
                       | set(prev_verdict.invalid_ids))
    curr_unresolved = (set(curr_verdict.non_terminal_ids) | set(curr_verdict.missing_ids)
                       | set(curr_verdict.invalid_ids))
    if prev_unresolved - curr_unresolved:
        return True
    registered = set(curr_snapshot.manifest["items"]) if curr_snapshot.manifest is not None else set()
    stalled = set(investigation_ledger.no_progress(
        prev_snapshot, curr_snapshot, required_extra=required_extra)) & registered
    return stalled != set(curr_verdict.non_terminal_ids)


def _investigation_tree_has_symlink(root: Path) -> bool:
    """`root` 自身（`manifest.json`・`items/`・配下ファイルを含む）に symlink が1つでもあれば
    `True`（RV 高-1・2026-09-22 6巡目是正）。model-shell は cwd（`.tmp/investigation/` 配下）に
    書けるため、model からは不可視のホスト側ファイルへの symlink を仕込める——Sherpa（親権限）の
    `copytree` がそれを辿ると、model からは読めないファイル本文が退避経由で実体化し、次ターンの
    「続き」復元後に model が読めてしまう。`followlinks=False`（`sandbox.py::
    _restore_removable_permissions` と同じ流儀）で辿らず、各エントリの symlink 判定だけで検出する
    ——fail-closed（列挙自体が失敗する場合も疑わしいとして `True`）。

    RV 高-1（2026-09-22 8巡目是正）: `os.walk` は既定で列挙エラーを無視する（`onerror=None`）——
    `root`（またはその配下）が読取/実行権を操作されて列挙できない（例: `chmod 0111` で実行のみ・
    listdir 不可にしつつ、既知ファイル名の `manifest.json` は symlink のまま既知パスとして到達可能
    にする）と、`os.walk` は何も見つけずに空のまま完走し、この関数は symlink を1つも検出できずに
    `False` を返してしまう。`onerror` に再送出するコールバックを渡し、列挙失敗を握りつぶさず
    `except OSError: return True` へ委ねる（列挙できない木は「疑わしい」として拒否する）。
    """
    def _reraise(exc: OSError) -> None:
        raise exc
    try:
        if root.is_symlink():
            return True
        for dirpath, dirnames, filenames in os.walk(root, onerror=_reraise, followlinks=False):
            for name in dirnames + filenames:
                if os.path.islink(os.path.join(dirpath, name)):
                    return True
    except OSError:
        return True
    return False


def _copy_investigation_contract_files(src: Path, dst: Path) -> None:
    """`src` の台帳の正規ファイル（`manifest.json`・`items/*.json`）だけを `dst` へコピーする
    （RV 高-1・2026-09-22 6巡目是正）。それ以外のファイル・ディレクトリは無視する——退避・復元の
    対象を正典§3の正規形に限定し、無関係なファイルが紛れ込む経路を塞ぐ。

    RV 高-1（2026-09-22 8巡目是正）: 呼び出し側の `_investigation_tree_has_symlink`（木の走査）が
    列挙不可なディレクトリで symlink を見落とす可能性が別途あるため（`_investigation_tree_has_
    symlink` 側の `onerror` 再送出で塞いだが、木の走査に単一点障害を作らない多層防御として）、
    ここでも各ファイルを**コピーする直前**に `os.path.islink()` で個別確認する——`Path.is_file()`・
    `Path.is_symlink()` は既知パスへの `lstat` だけで済み、親ディレクトリの列挙権限に依存しない。
    symlink を検出したら `OSError` を送出して中止する（呼び出し側 `_retire_investigation_ledger`
    の `except OSError` が拾って fail-closed・警告1行にする）。
    """
    dst.mkdir(parents=True, exist_ok=True)
    manifest_src = src / "manifest.json"
    if manifest_src.is_file():
        if manifest_src.is_symlink():
            raise OSError(f"refusing to copy symlink: {manifest_src.name}")
        shutil.copy2(manifest_src, dst / "manifest.json")
    items_src = src / "items"
    if items_src.is_dir():
        items_dst = dst / "items"
        items_dst.mkdir(parents=True, exist_ok=True)
        for item_path in items_src.glob("*.json"):
            if item_path.is_file():
                if item_path.is_symlink():
                    raise OSError(f"refusing to copy symlink: {item_path.name}")
                shutil.copy2(item_path, items_dst / item_path.name)


def _restore_investigation_ledger(retired_dir: Path, investigation_dir: Path, tmp_root: Path) -> bool:
    """`retired_dir`（前ターンの退避）を `investigation_dir` へ**原子的に**復元する
    （RV 中-1・2026-09-22 10巡目是正）。`tmp_root` 配下の一時ディレクトリへ
    `_copy_investigation_contract_files` で全部コピーしてから `os.replace` で
    `investigation_dir` に置き換える——items が複数ある退避台帳を `investigation_dir` へ直接
    コピーすると、途中（例: 2件目）で `OSError` が起きたとき部分復元（manifest と一部の item
    だけ）が run_dir に残ってしまい、このターン終了時の退避（通常終了・finally の両方）が
    その部分台帳で元の（より完全な）退避台帳を上書き・削除してしまう。

    失敗したら一時ディレクトリを消し、`investigation_dir` を空（`items/` だけの新規台帳として
    扱える状態）に戻す——部分復元は run_dir に一切残さない。fail-open（例外を投げない・
    失敗はここで1行警告する）。呼び出し側は戻り値が `False` のとき、このターンでは退避台帳を
    削除・置換しないこと（次ターンの「続き」でもう一度復元を試せるよう保持する）。

    戻り値: 復元できたら `True`。
    """
    tmp_dir = tmp_root / f".investigation.restore-{os.getpid()}-{os.urandom(4).hex()}"
    try:
        if tmp_dir.exists():
            _remove_dir_best_effort(tmp_dir)
        _copy_investigation_contract_files(retired_dir, tmp_dir)
        if investigation_dir.exists():
            _remove_dir_best_effort(investigation_dir)
        os.replace(tmp_dir, investigation_dir)
        return True
    except OSError as e:
        _log.warning("investigation ledger restore failed: %s", type(e).__name__)
        _remove_dir_best_effort(tmp_dir)
        if investigation_dir.exists():
            _remove_dir_best_effort(investigation_dir)
        (investigation_dir / "items").mkdir(parents=True, exist_ok=True)
        return False


def _retire_investigation_ledger(investigation_dir: Path, ledger_home: Path,
                                 *, required_extra: tuple[str, ...] = ()) -> bool:
    """未完了（または manifest 破損等で判定不能）の調査台帳を `ledger_home/investigation` へ
    退避する（正典§3「置き場所」/§4「寿命」）。complete なら退避先を削除する。

    `required_extra`: 呼び出し元（`_run_authoring`）のターン内で台帳ゲートへ渡したものと**同じ値**
    を渡すこと——ここだけ渡さないと、ゲート側は未完了（追加種別が未充足）と判定した台帳を、ここは
    complete と誤判定して削除してしまう（次ターンの「続き」で復元できなくなる）。

    RV 中-3（2026-09-22 3巡目是正）: 通常終了時はこの関数の**戻り値**（実際に退避を実行できたか）
    を `env["investigation"]["retained"]` にそのまま使う——退避前に式で予測した値を使わない
    （manifest 未作成のまま催促後に受理したケース・コピー失敗のケースで実態と食い違っていた）。

    RV 高-2（2026-09-22 3巡目是正）: 「未作成の空ディレクトリ」（manifest ファイルも item ファイルも
    無い）と「作業データのある無効台帳」（manifest ファイルが存在する、または items 配下に1件でも
    ファイルがある——有効/無効を問わない）を区別し、**後者は退避する**（`ledger_complete()` の
    `manifest_invalid` だけで判定すると、manifest が壊れているだけで items にデータがある台帳が
    退避されず run_dir ごと失われていた）。

    RV 高-1（2026-09-22 6巡目是正）: 台帳ルート配下に symlink が1つでもあれば退避を拒否する
    （`_investigation_tree_has_symlink`）。コピー対象は `_copy_investigation_contract_files` に
    限定し、規約外のファイル・symlink が退避先に紛れ込まないようにする。

    fail-open（失敗しても例外を投げない・呼び出し側はログだけ見る）。戻り値: 退避（コピー）を
    実際に行ったら `True`——delete のみ／何もしない（空ディレクトリ・symlink 検出）場合は
    `False`。
    """
    retire_dir = ledger_home / "investigation"
    try:
        verdict = investigation_ledger.ledger_complete(
            investigation_ledger.load_ledger(investigation_dir), required_extra=required_extra)
        if verdict.complete:
            if retire_dir.exists():
                _remove_dir_best_effort(retire_dir)
            return False
        manifest_file_exists = (investigation_dir / "manifest.json").is_file()
        items_dir = investigation_dir / "items"
        has_any_item_file = items_dir.is_dir() and any(items_dir.glob("*.json"))
        if not (manifest_file_exists or has_any_item_file) or not investigation_dir.exists():
            return False
        if _investigation_tree_has_symlink(investigation_dir):
            _log.warning("investigation ledger retire skipped: symlink detected under investigation dir")
            return False
        # 原子的な置換: 一時ディレクトリへ丸ごとコピーしてから `os.replace`（同一ファイルシステム上
        # の rename＝1回の原子操作）で置き換える——退避先を直接 rmtree→copytree すると、その間
        # だけ「無い」→「一部だけある」という中間状態が実ファイルシステム上に存在する。
        tmp_retire_dir = ledger_home / f".investigation.tmp-{os.getpid()}-{os.urandom(4).hex()}"
        if tmp_retire_dir.exists():
            _remove_dir_best_effort(tmp_retire_dir)
        _copy_investigation_contract_files(investigation_dir, tmp_retire_dir)
        if retire_dir.exists():
            _remove_dir_best_effort(retire_dir)
        os.replace(tmp_retire_dir, retire_dir)
        return True
    except OSError as e:
        _log.warning("investigation ledger retire failed: %s", type(e).__name__)
        return False


# ---- 出力スキーマ（`--output-schema`・docs/proposals/2026-09-08-Codex出力スキーマ.md §2-3）----
# `_OUTPUT_SCHEMA_PATH` の3キーちょうど（strict・additionalProperties: false）と対応させる。
_STRUCTURED_KEYS = {"status", "answer", "next_step"}
_STRUCTURED_STATUSES = {"final", "in_progress"}
# v2（DEPTH-2 S1・output_schema_v2.json）の4キーちょうど・主張1件の閉じたキー集合と語彙。
_STRUCTURED_KEYS_V2 = _STRUCTURED_KEYS | {"claims"}
# S1b（実装ベース探索の回復・Codex 経路の根拠種別）: `evidence_kinds`（7つ目のキー）は主張1件が
# 実際に開いて確認した根拠の種別（`investigation_state.EVIDENCE_KINDS` の閉集合）。旧テンプレート・
# 実機の崩れた応答で送られてくる6キー形（`_CLAIM_KEYS_LEGACY`）も後方互換で受理し、その場合は
# `evidence_kinds=None`（未申告＝最終ゲートは判定不能として格下げも不足の計上もしない）にする。
_CLAIM_KEYS = {"id", "status", "text", "evidence_refs", "reason", "reason_code", "evidence_kinds"}
_CLAIM_KEYS_LEGACY = _CLAIM_KEYS - {"evidence_kinds"}
_CLAIM_STATUSES = {"confirmed", "inferred", "unknown"}
_CLAIM_UNKNOWN_REASON_CODES = {
    "not_found_in_scope", "unexplored", "insufficient", "conflict", "budget", "unreadable"}


def _parse_structured(text: str | None) -> dict | None:
    """`--output-schema` で固定した3キー JSON かどうかを検証する（純関数）。

    キー集合の完全一致・`status` の値・`answer`/`next_step` の型が全て合うときだけ dict を返す。
    それ以外（構文エラー・途中で切れた JSON・キーの過不足・不正な型・不正な status 値）は None——
    呼び出し側はこれを「未完了」として扱う（壊れた JSON を平文ヒューリスティックへ戻さない・§2-3）。
    """
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or set(obj.keys()) != _STRUCTURED_KEYS:
        return None
    # `in` は set の要素比較にハッシュ化を要る——`status` が list/dict 等の非 hashable 値だと
    # `not in _STRUCTURED_STATUSES` 自体が TypeError で落ちる（壊れた入力を None に丸めるはずが
    # 例外で伝播してしまう）。先に str 型を確認してから集合照合する。
    _status = obj.get("status")
    if not isinstance(_status, str) or _status not in _STRUCTURED_STATUSES:
        return None
    if not isinstance(obj.get("answer"), str):
        return None
    _next = obj.get("next_step")
    if _next is not None and not isinstance(_next, str):
        return None
    return obj


def _parse_claim(item) -> dict | None:
    """v2 の主張1件（DEPTH-2 S1・§2.2/§2.5）を検証する（`investigation_state.parse_claims` と
    同じ規約——キー集合の完全一致・`status` の閉じた語彙・`unknown` だけ `reason_code` を閉じた
    語彙から要求）。不正なら None（呼び出し元は主張配列全体を無効として扱う）。

    RV C1（confirmed の裏付け）: Codex の `evidence_refs` は `investigation_state.Evidence.ev_id`
    のような機械的に検証できる調査内 ID を持たない（Codex 自身が MCP で読んだ資料集合を、この
    純関数からは参照できない）——参照の実在チェックまでは行わず、confirmed には**非空**の
    `evidence_refs` だけを要求する（空＝裏付けを一つも挙げない確定主張を拒否する・API/Ollama 側
    の `investigation_state.parse_claims`+`InvestigationState.set_claims` と揃える範囲）。
    RV C4: inferred は空白のみでない `reason` を必須にする（理由の無い推定を拒否する）。

    S1b: `evidence_kinds`（この主張が実際に確認した根拠種別・閉集合の list）は7キー形でだけ
    検証する。6キー形（`evidence_kinds` 無し・旧テンプレート／崩れた応答との後方互換）は
    `evidence_kinds=None` を補って返す——最終ゲート（`_apply_codex_evidence_gate`）は None を
    「未申告」として扱い、格下げも turn 単位の不足判定にも数えない。
    """
    if not isinstance(item, dict):
        return None
    keys = set(item.keys())
    if keys == _CLAIM_KEYS:
        kinds_raw = item.get("evidence_kinds")
        if not isinstance(kinds_raw, list) or not all(
                isinstance(k, str) and k in investigation_state.EVIDENCE_KINDS for k in kinds_raw):
            return None
        evidence_kinds = kinds_raw
    elif keys == _CLAIM_KEYS_LEGACY:
        evidence_kinds = None
    else:
        return None
    cid, status, text = item.get("id"), item.get("status"), item.get("text")
    if not isinstance(cid, str) or not cid.strip():
        return None
    if not isinstance(status, str) or status not in _CLAIM_STATUSES:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    refs = item.get("evidence_refs")
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        return None
    reason = item.get("reason")
    if not isinstance(reason, str):
        return None
    reason_code = item.get("reason_code")
    if not isinstance(reason_code, str):
        return None
    if status == "unknown":
        if reason_code not in _CLAIM_UNKNOWN_REASON_CODES:
            return None
    elif reason_code:
        return None
    if status == "confirmed" and not any(r.strip() for r in refs):
        return None
    if status == "inferred" and not reason.strip():
        return None
    return {**item, "evidence_kinds": evidence_kinds}


def _parse_structured_v2(text: str | None) -> dict | None:
    """v2 出力スキーマ（`claims` を持つ4キー）を検証する。v1 の3キー形（`claims` 無し）も
    後方互換で読める——その場合は `_parse_structured` と同じ検証をそのまま使い、返す dict に
    `claims: []` を補う（呼び出し側は常に `.get("claims", [])` で読める）。

    v2 の4キー形は主張配列の各要素も `_parse_claim` で検証する——1件でも不正なら**主張構造だけ**
    `claims: []` に落とし、`status`/`answer`/`next_step` は正規の値としてそのまま返す（崩れた
    主張を成功扱いにしないのは主張構造の話であって、完成した回答本文まで巻き添えで捨てて
    継続判定・見出しの固定文言化を誘発してはならない・§2.5）。
    """
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    if set(obj.keys()) == _STRUCTURED_KEYS:
        v1 = _parse_structured(text)
        if v1 is None:
            return None
        return {**v1, "claims": []}
    if set(obj.keys()) != _STRUCTURED_KEYS_V2:
        return None
    _status = obj.get("status")
    if not isinstance(_status, str) or _status not in _STRUCTURED_STATUSES:
        return None
    if not isinstance(obj.get("answer"), str):
        return None
    _next = obj.get("next_step")
    if _next is not None and not isinstance(_next, str):
        return None
    claims = obj.get("claims")
    if not isinstance(claims, list):
        return None
    parsed_claims = []
    for item in claims:
        parsed = _parse_claim(item)
        if parsed is None:
            # 不正な主張構造は本文を巻き添えにせず主張だけを捨てる。無言の縮退にしない。
            _log.warning("codex v2: invalid claim dropped (claims emptied, answer kept)")
            return {**obj, "claims": []}
        parsed_claims.append(parsed)
    return {**obj, "claims": parsed_claims}


# S1b（実装ベース探索の回復・Codex 経路の根拠種別判定）: API 経路（`providers/base.py`）は
# `InvestigationState.evidence` から主張ごとの根拠種別を導けるが、Codex は自分の MCP/直読の
# 履歴をこの純関数から参照できる形で残さない——そのため主張自身が申告する `evidence_kinds`
# （`_parse_claim` が検証）を根拠種別の唯一の入力にする。判定の規律（レンズ別必須種別・
# 「範囲に無い」の除外・確定の格下げ文言）は API 側と同じ語彙・同じ文言（`investigation_state.
# demote_reason_for_missing_kinds`）を使うが、実装は別（データソースが違う以上、共通化すると
# 「ある」を「ない」と誤認させる暗黒の抽象化になる）。
def _apply_codex_evidence_gate(claims: list[dict], *, lens: str, world: str, scope_paths,
                               layer, personal_facts: str) -> tuple[list[dict], dict, tuple, tuple]:
    """確定主張のうち必須の根拠種別を欠くものを推定へ格下げし、ターン単位の不足も併せて返す。

    戻り値: `(格下げ済みの claims コピー, envelope 用 evidence_gate meta, ターン単位の不足種別,
    範囲に無い種別)`——後2つは呼び出し側の headline 注記（`_evidence_gate_note`）用の生の
    種別名（envelope の `missing_codes` は集計用の閉集合コードへ変換済みのため、注記側では
    使い回さず別途返す）。

    `unavailable`: 今回の登録範囲にその種別が存在しない（該当なし＝不足に数えない）。
    `personal_facts`（個人ファイルの grep ヒット）があれば `log_config` は主張単位・ターン単位の
    両方で充足済みとして扱う（API 側 `_turn_evidence_kinds`／`_claim_kind_gap` と同じ規則——
    個人ファイルは共有 KB の台帳にも主張の申告にも載らないため、ここでしか数えられない）。
    `applied`（meta 内）: 未申告（`evidence_kinds is None`＝旧6キー形／崩れた応答）の主張が
    無く、ターン単位の判定を確定的に行えたか——1件でも未申告があれば `missing_codes` は
    空のまま `applied=False` にする（判定不能を「不足0件」と誤表示しない）。
    `demoted`（meta 内）: 格下げした主張の件数。ターン全体では種別が揃っていても（主張ごとの
    申告の和集合は必須を満たす）個々の主張が欠く場合があり、そのときは不足の注記が出ないため、
    呼び出し側はこの件数で「一部の主張は推定に留めた」注記を別途前置する。
    """
    required_all = investigation_state.required_evidence_kinds(lens)
    scope_kinds = _scope_evidence_kinds(world, scope_paths, layer)
    unavailable = tuple(
        k for k in required_all
        if scope_kinds is not None and k not in scope_kinds[0]
        and not (k in _KINDS_OUTSIDE_LEDGER and personal_facts))
    required = tuple(k for k in required_all if k not in unavailable)
    outside_ledger_seen = set(_KINDS_OUTSIDE_LEDGER) if personal_facts else set()
    declared_union: set = set(outside_ledger_seen)
    any_undeclared = False
    demoted = 0
    out = []
    for c in claims:
        kinds = c.get("evidence_kinds")
        if kinds is None:
            any_undeclared = True
            out.append(c)
            continue
        declared_union |= set(kinds)
        lacking = [k for k in required if k not in kinds and k not in outside_ledger_seen]
        if c.get("status") == "confirmed" and lacking:
            c = {**c, "status": "inferred",
                 "reason": investigation_state.demote_reason_for_missing_kinds(lacking),
                 "reason_code": ""}
            demoted += 1
        out.append(c)
    turn_missing = (() if any_undeclared
                    else tuple(k for k in required if k not in declared_union))
    meta = {"missing_codes": [investigation_state.missing_code_for_kind(k) for k in turn_missing],
            "unavailable": list(unavailable), "applied": not any_undeclared, "demoted": demoted}
    return out, meta, turn_missing, unavailable


def _normalize_evidence_path(path: str) -> str:
    """区切りを `/` に統一し先頭の `./` を除く——台帳 item の `evidence.path` と claim の
    `evidence_refs` から取り出した path の表記ゆれ（相対参照の書き方の違い）を吸収して
    同一視するための正規化（`_claims_vs_ledger` が両者を突き合わせる前に適用する）。
    """
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _parse_evidence_ref(ref) -> tuple[str, int] | None:
    """claim の `evidence_refs` 1件（`"path:line"` 形式・`codex_agents_md.
    _investigation_ledger_paragraph` が Codex に指示する書式）を `(正規化 path, line)` に分解する。
    形式に合わない（文字列でない・コロンが無い・行が整数でない等）場合は `None`——台帳のどの
    `evidence` とも一致しない扱いになる（fail-safe・例外を投げない）。
    """
    if not isinstance(ref, str):
        return None
    idx = ref.rfind(":")
    if idx <= 0 or idx == len(ref) - 1:
        return None
    path_part, line_part = ref[:idx], ref[idx + 1:]
    try:
        line = int(line_part)
    except ValueError:
        return None
    return (_normalize_evidence_path(path_part), line)


def _ledger_evidence_locations(snapshot: investigation_ledger.LedgerSnapshot) -> set:
    """台帳の **manifest に登録された** item（`snapshot.manifest["items"]`）だけの `evidence` を
    `(正規化 path, line)` の集合にまとめる。未登録 item（`investigation_ledger.ledger_complete` の
    `unregistered_ids` と同じ——親が manifest に登録していない・子が勝手に作った可能性がある）の
    evidence は裏付けとして採用しない——採用には manifest 登録が必要という台帳の契約を、claims
    の裏付け判定でも一貫させる（`snapshot.items` は `load_ledger` が正規形を検証済みのため、
    登録済み item の要素の型は信頼してよい）。manifest が無い／`items` が空なら登録集合が空——
    その場合はどの item の evidence も採用されない（全 confirmed が格下げされる）。
    """
    manifest_ids = set(snapshot.manifest["items"]) if snapshot.manifest is not None else set()
    locations = set()
    for item_id, item in snapshot.items.items():
        if item_id not in manifest_ids:
            continue
        for ev in item.get("evidence") or []:
            locations.add((_normalize_evidence_path(ev["path"]), ev["line"]))
    return locations


# `_claims_vs_ledger` が confirmed を推定へ格下げするときの理由文の先頭に付ける固定文
# （`_apply_codex_evidence_gate`/`investigation_state.demote_reason_for_missing_kinds` と同じ
# 「理由を上書きではなく明示する」流儀・既存の `reason` があれば括弧書きで残す）。
_LEDGER_UNMATCHED_REASON = "根拠が調査台帳に無いため確定できません"


def _claims_vs_ledger(claims: list[dict], snapshot: investigation_ledger.LedgerSnapshot, *,
                      manifest_file_exists: bool) -> tuple[list[dict], dict]:
    """最終回答の `claims` を調査台帳と突き合わせる（`docs/proposals/2026-09-21-調査台帳を文脈の外に
    置く.md` §3「claims は台帳からの投影」・§6 ステップ4後半）。

    Codex 自身が申告する `evidence_refs` は `investigation_state.Evidence.ev_id` のような機械的に
    検証できる調査内 ID を持たない（`_parse_claim` の docstring）——台帳という別経路の記録
    （`item.evidence` の `path`/`line`）と突き合わせることで、初めて裏付けの実在を確認できる。
    `evidence_refs` が1件も台帳の `evidence` に一致しない confirmed 主張は、裏付けを示せないため
    推定（inferred）へ格下げする（`status`/`reason`/`reason_code` を書き換える・他のキーは保持）。
    一部の `evidence_refs` だけが一致する confirmed は確定のまま維持する。

    `manifest_file_exists`（`(investigation_dir / "manifest.json").is_file()`・台帳ゲート本体
    （`_investigation_tree_has_symlink` 呼び出し元・`_retire_investigation_ledger` の判定）と
    同じ基準）は、`snapshot.manifest is None` の2通りの原因を区別するために要る:
    - ファイル自体が無い（`manifest_file_exists=False`）＝台帳を作らなかった依頼——この
      対応関係を適用せず `claims` を無変更で返す（正典§3）。
    - ファイルはあるが内容が規約に合わない／symlink（`manifest_file_exists=True` かつ
      `snapshot.manifest is None`）＝「壊れた台帳」——台帳を作らなかった扱いにはできない
      （正典§4「壊れた台帳から final を生成しない」と同じ原則）。登録集合を空として扱い、
      confirmed はどの evidence も裏付けに使えない（`_ledger_evidence_locations` は
      `snapshot.manifest is None` のとき登録集合を空にする＝自然に全 confirmed が格下げされる）。
    `confirmed` 以外（`inferred`／`unknown`）は対象外（台帳との対応関係を要求されるのは
    confirmed だけ）。

    戻り値: `(格下げ済みの claims コピー, 統計 dict)`。`manifest_state` は
    `"absent"`（ファイル無し）／`"invalid"`（ファイルはあるが内容不正）／`"valid"`（正規形）の
    いずれか。`absent` は `{"ledger": False, "manifest_state": "absent"}`、それ以外は
    `{"ledger": True, "checked": 検査した confirmed 件数, "downgraded": 格下げ件数,
    "unmatched_refs": 台帳に一致しなかった evidence_refs の延べ件数, "manifest_state": ...}`。
    """
    if not manifest_file_exists:
        return claims, {"ledger": False, "manifest_state": "absent"}
    manifest_state = "valid" if snapshot.manifest is not None else "invalid"
    locations = _ledger_evidence_locations(snapshot)
    checked = downgraded = unmatched_refs = 0
    out = []
    for c in claims:
        if c.get("status") != "confirmed":
            out.append(c)
            continue
        checked += 1
        matched = False
        for ref in (c.get("evidence_refs") or []):
            if _parse_evidence_ref(ref) in locations:
                matched = True
            else:
                unmatched_refs += 1
        if not matched:
            reason = (c.get("reason") or "").strip()
            new_reason = (f"{_LEDGER_UNMATCHED_REASON}（{reason}）" if reason
                         else _LEDGER_UNMATCHED_REASON)
            c = {**c, "status": "inferred", "reason": new_reason, "reason_code": ""}
            downgraded += 1
        out.append(c)
    return out, {"ledger": True, "checked": checked, "downgraded": downgraded,
                "unmatched_refs": unmatched_refs, "manifest_state": manifest_state}


# 格下げは起きたがターン全体では種別が揃っている（不足の注記が出ない）ときの前置文
# （本文は書き換えない・`data.claims` は画面に描画されないため、ここで伝えないと格下げが見えない）。
_DEMOTED_CLAIMS_NOTE = "一部の内容は必要な根拠の種別が揃っていないため、確定ではなく推定として扱っています。"


# 成果物の move／台帳登録に1件でも失敗したとき、回答本文の末尾に付ける固定文
# （`_created_files_failed` 判定・headline がどの分岐で組み立てられていても一律に付く）。
_CREATED_FILES_FAILURE_NOTE = "（作成したファイルの一部を保存できませんでした。管理者に確認してください）"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """env の整数解析（`agentic_search._env_int` と同型・循環 import 回避のため独立実装）。

    範囲 [lo, hi] 外・非整数は既定値へ戻す（既定値自体も [lo, hi] にクランプ）。呼び出し側
    （`_run_authoring`）が実行のたびに呼ぶため、`monkeypatch.setenv` 後の値にも追随する。
    """
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


def _int_or_none(raw) -> int | None:
    """`_mcp_budget_env` の文字列値（数値の文字列、または mcp 無効時の `"-"`）を
    `activity.settings`（利用統計・JSON 数値）へ変換する。数値でなければ None（取れない値を
    推定で埋めない＝§3.1「settings は開始行と同じ値」の数値化のみを行う）。"""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# 永続 CODEX_HOME（`.codex-sessions/{conversation_id}`）は同一会話の複数ターンにまたがって
# 同じ固定パスを共有する。同一会話の2実行が重なると、config.toml の
# unlink→再作成が競合し（直前の unlink は「今すぐ空いている」ことしか保証せず、もう一方の
# プロセスの書込と時間的に競合しうる＝O_EXCL は排他にならない）、別ターンの world/layer/MCP 設定
# で起動しうる・session JSONL への同時書込・終了時の finally が相手の config.toml/auth.json を
# 巻き添えで削除しうる。run dir（実行ごとに専用ディレクトリ）とは別に、conversation_id をキーに
# した非ブロッキング lock で「同一会話の永続 CODEX_HOME を使う実行」だけを直列化する（別会話・
# 非永続セッションは対象外＝互いに無関係な run dir／使い捨て CODEX_HOME を使うため衝突しない）。
_CONVERSATION_LOCKS: dict = {}
_CONVERSATION_LOCKS_GUARD = threading.Lock()


def _conversation_lock(conversation_id) -> threading.Lock:
    with _CONVERSATION_LOCKS_GUARD:
        lk = _CONVERSATION_LOCKS.get(conversation_id)
        if lk is None:
            lk = _CONVERSATION_LOCKS[conversation_id] = threading.Lock()
        return lk


class CodexProvider(Provider):
    """Codex(gpt-5.5) を**エージェント中核**に（設計どおり）。

    取得（Neo4j/grep）は本物のツールで実行しつつ、**Codex 自身も原文を grep/参照で裏取り**する。
    Codex の **実コマンド実行（grep 等）・推論・回答**を `--json` から拾い **1つずつ思考ノードに流す**
    （ユーザは Codex の作業を逐次見られる）。失敗/未導入は決定的回答にフォールバック。
    既定 reasoning=low（`SHERPA_CODEX_REASONING` で変更可。RV依頼の xhigh とは別運用）。
    推論レベルは調べる深さでは変えない——管理画面の基準値で固定（`depth_profile.codex_reasoning_for`）。
    """
    label, model = "Codex", "gpt-5.5"
    provider_id = "codex"

    def __init__(self, reasoning: str | None = None, model: str | None = None,
                web_search: bool | None = None, ollama_base_url: str | None = None,
                openai_api_key: str | None = None, system_settings: dict | None = None):
        self._reason = reasoning or os.environ.get("SHERPA_CODEX_REASONING", "low")
        # チャットの Codex モデルは選択可（RV/委譲の固定運用とは別）。argv `-m` に渡すので
        # 先頭ハイフン/空白/制御文字/過大長は弾く（flag 混同・不正値の防止）。
        # `model_catalog.CODEX_MODEL_NAME_RE` を使う（`sherpa/model_catalog.py::validate_catalog` が
        # 管理者カタログへ課す文法と同じパターン＝管理画面で保存できるモデル名と揃える）。
        # 未指定（None/空文字）だけを既定 "gpt-5.5" へ解決する。
        # **不正な非空値**（grandfather された旧値・破損 DB・接続確認の直接入力等）は黙って
        # 別モデルへ置換しない＝ honest failure として `InvalidModelNameError`（`ValueError` の
        # サブクラス）を送出する（呼び出し側 `sherpa/providers/__init__.py::_select_provider` が
        # モデル名専用のこの型だけを捕捉し `_UnwiredProvider` として正直に失敗を伝える）。
        # 表示したモデルと実行モデルが食い違う事故を防ぐ。
        if model and not model_catalog.CODEX_MODEL_NAME_RE.fullmatch(model):
            raise model_catalog.InvalidModelNameError(f"不正な Codex モデル名です: {model!r}")
        self.model = model or "gpt-5.5"
        # §5-1: ユーザーの希望（設定 codex_web_search）。実際に効くかは管理者フラグ次第
        # （_web_search_disabled_value が admin 許可と AND する）。
        self._web_search = bool(web_search)
        # Codex(Ollama) 構成（`agent_constructs`）のとき、Codex CLI を Ollama へ向ける接続先。
        # None＝Codex(OpenAI)＝従来どおり Codex の既定プロバイダ（OpenAI）を使う。
        # 値は `providers/__init__.py::_select_provider` が SSRF ガード（llm.assert_ollama_url_allowed）
        # を通してから渡す＝ここでは検証済みの前提。
        self._ollama_base_url = ollama_base_url or None
        # Codex(OpenAI) 構成で、接続先が既定(api.openai.com)以外
        # （Azure 等）にリダイレクトされている時**だけ** `_select_provider` が解決して渡す（それ以外は
        # 常に None のまま＝既定の Codex(OpenAI)・Codex(Ollama) は無改修・回帰ゼロ）。カスタム
        # model_provider（`sandbox._openai_compat_provider_lines`）は `env_key` で子プロセスの env から
        # キーを読む設計のため、この構成の時だけ `_codex_clean_env` にこの値を渡して env に注入する
        # （既定は引き続き auth.json 経由・env にキーを置かない現行方針を維持）。
        self._openai_api_key = openai_api_key or None
        # `_select_provider` が key/model 解決に使ったのと同じ system_settings スナップショットを、
        # config.toml 生成（`_write_codex_authoring_config`）・web_search 注記
        # （`_web_search_endpoint_note`）へもそのまま渡す。省略時（`None`）は従来どおり呼び出しごとに
        # `llm.py` が都度読み直す。
        self._system_settings = system_settings
        # 既定は空（`run()` を経由せず `_prompt`/`_prompt_mcp` を直接叩くテスト向けの安全な
        # フォールバック・`_history` は `run()` 冒頭で `ctx.history` から設定し直される）。
        self._history: list = []

    def _history_block(self) -> str:
        """直前ターンの履歴を Codex プロンプトへ前置するテキスト（会話継続）。

        `self._history` が空なら空文字列を返す＝呼び出し側の出力は従来と完全同一になる。
        """
        if not self._history:
            return ""
        lines = [f"{'ユーザー' if h.get('role') == 'user' else 'アシスタント'}: {h.get('content', '')}"
                for h in self._history]
        return "【直前の会話（参考・新しいものが下）】\n" + "\n".join(lines) + "\n\n"

    def _prompt(self, message, lens, env, world):
        sys = (self.system_prompt + "\n\n") if self.system_prompt else ""   # 回答方針（#2）を前置
        # cwd が workspace/authoring/ のため KB パスは絶対パスで渡す。
        # §2: 出典列挙/文体等の共通ルールは AGENTS.md へ移した（質問固有部分のみここに残す）。
        # ただし containment/grounding（KB 以外を読まない・確定と推定を分ける）は
        # AGENTS.md 書込失敗時（fail-open）でも消えないよう、短縮形をここにも常置する（多層防御・
        # AGENTS.md と重複しても害はない＝独立性を優先）。
        # 探す対象（層フィルタ）が限定されているターンは、この直接 grep 経路（MCP 無効時）自体を
        # 呼び出し元（_run_authoring）が実行しない契約——ここはプロンプト指示による迂回可能な
        # ソフト制御を持たない（正典 §3.4「範囲と同じ硬いフィルタ」・MCP 経由のときだけ実行する）。
        base = (
            "あなたは社内ナレッジ調査エージェントです。以下の資料フォルダ"
            f"（{_kb_hint_abs(world)}）を **grep やファイル参照で実際に調べてください**。"
            "Excel/Word/PowerPoint/PDF は Python（openpyxl・python-docx・python-pptx・pdfplumber）で"
            "開いて読んでよい。"
            f"{_READ_ONLY_SENTENCE}{_BOTH_SIDES_SENTENCE}"
            "**指定資料フォルダ以外は読まない。確定した事実と推定は分けて書く**（詳細ルールは AGENTS.md）。"
            "**途中経過だけの応答（「次に〜を調べます」など）で終えない。調査を最後まで進めてから、"
            "結論と根拠を最終回答として書く。**"
            # この経路（MCP 無効・直接 grep/ファイル参照）で読んだ資料も
            # 同様に、回答末尾の固定書式で Sherpa（citations.parse_referenced_doc_lines）に出典（原本DL）へ
            # 変換させる。
            "回答の最後に『参照した資料:』の行を置き、実際に開いて根拠にした資料を1行1件、"
            "資料フォルダからの相対パス（例 `4期更改/02_設計/xxx.xlsx`）で列挙する。"
            "Sherpaがこれを出典（原本ダウンロード）に変換する。"
        )
        if lens == "author":
            # author は回答でなく成果物ファイルを作る。
            return sys + base + (
                "調べた内容を根拠に、**成果物ファイルをこのディレクトリ（authoring 直下）に作成してください**。"
                "Excel/Word/PowerPoint 等を作る場合は `.agents/skills` 配下のスキル（xlsx/docx/pptx の"
                " SKILL.md）を確認して活用する。下の『参考（構造化済みの事実）』は補助に使ってよいが、"
                "件数・対象名は事実のまま。最後に**作成したファイル名**と**内容の要約**を"
                "日本語で報告してください。\n\n"
                # 履歴があれば【依頼】の前に前置（空文字なら従来と完全同一の出力）。
                f"{self._history_block()}【依頼】{message}\n【参考（構造化済みの事実）】{_facts(lens, env)}")
        return sys + base + _NO_FILES_SENTENCE + (
            "ユーザの質問に答えてください。"
            "下の『参考（構造化済みの事実）』は補助に使ってよいが、件数・対象名は事実のまま。\n\n"
            # 履歴があれば【質問】の前に前置（空文字なら従来と完全同一の出力）。
            f"{self._history_block()}【質問】{message}\n【参考（構造化済みの事実）】{_facts(lens, env)}")

    def _prompt_mcp(self, message, lens, world, direct_read: bool = True, layer=None):
        """MCP 版プロンプト。事実を前渡しせず、Codex に MCP ツールで自律調査させる。
        §2: 出典列挙/文体等の共通ルールは AGENTS.md へ移した（ここは MCP ツール固有の使い分け
        ＋ containment/grounding の短縮形を常置＝AGENTS.md 書込失敗時の多層防御）。

        `direct_read`（既定 True・提案書 2026-09-10-Codex原本直読と調査スキル §2-4）: 原本直読
        （permission profile で KB／派生ルートを read し、範囲は兄弟 deny・秘匿は個別 deny で表したうえで
        コードインタープリターで直接開く）の可否。`_run_authoring` が秘匿列挙と範囲（`_scope_deny_entries`）
        の成否から計算して渡す——失敗した（fail-closed）ターンだけ False（MCP のみへ縮退）。省略時
        （既存呼び出し・単体テスト）は True＝現行の主経路（直読可）を案内する。"""
        sysp = (self.system_prompt + "\n\n") if self.system_prompt else ""
        _read_block = (
            "**原本は直接読んでよい（読取専用・指定された資料フォルダと派生フォルダの中だけ・"
            "秘匿名のファイル（.env／鍵／credentials 等）は読まない）。"
            f"{_READ_ONLY_SENTENCE}"
            "旧形式など読取ツールで開けない原本は read_doc で変換済みテキストを読む。"
            # 主従は決めない——「まず読取ツールで原本を読む→
            # 突合・集計など定型外だけ Python」の順。毎回 Python を書かせない＝トークンと
            # 実行時間を削り、再現性を上げる。
            "まず読取ツール（xlsx_sheets／xlsx_range／docx_paragraphs／pptx_slides／"
            "pdf_pages／file_head）で原本を読む。複数ファイルの突合・集計など定型外の作業"
            "だけ Python（openpyxl・python-docx・python-pptx・pdfplumber。集計は pandas）"
            "で開く。テキスト・コードはそのまま読んでよい。"
            "派生 MD／rag.md は補助。台帳・検索・グラフ・出典の確定は MCP ツールで行う。"
            # 直読した資料は MCP の結果に載らず出典（原本DL）に
            # 自動では出ない——回答末尾に固定書式の行を書かせ、Sherpa（citations.parse_referenced_doc_lines）
            # が台帳で実在確認したものだけ出典へ昇格する。
            "回答の最後に『参照した資料:』の行を置き、実際に開いて根拠にした資料を1行1件、"
            "資料フォルダからの相対パス（例 `4期更改/02_設計/xxx.xlsx`）で列挙する。"
            "Sherpaがこれを出典（原本ダウンロード）に変換する。派生MD／rag.mdを見た場合も原本のパスで書く。**"
            # 質問の型に合う調査スキルへ誘導する（「まずツールで
            # 当たりを付ける→原本の中身を確かめる」の順を具体化した手順書。読ませても直読不許可の
            # ターン（direct_read=False）ではノイズ＝else 側には入れない）。
            "**質問の型（資料一覧／仕様の問い合わせ／影響範囲／原因調査／比較）に合う"
            " `.agents/skills` の investigate-* スキルを読んで、その手順（ツールで当たり→"
            "原本の中身を確かめる→答える）どおりに進める。**"
            if direct_read else
            "**今回は原本の直接読み取りは使えない。資料の本文は MCP のツールで読む（KB 外は読まない）。**"
            f"{_READ_ONLY_SENTENCE}"
        )
        # 層（探す対象）は Codex に強制しない（直読は層に関係なく read）——限定されたターンだけ案内する。
        _layer_block = {
            "docs": "探す対象として資料（設計書・仕様書などのドキュメント）が指定されている＝直読でも資料を優先して見る。",
            "code": "探す対象としてソース（プログラム・JCL・コピーブック）が指定されている＝直読でもソースを優先して見る。",
        }.get(layer, "")
        base = (
            "あなたは社内ナレッジ調査エージェントです。MCP サーバ『sherpa』のツール"
            "（list_docs＝文書台帳の一覧/件数／ripgrep_search＝全文grep／glob_search＝ファイル名パターン／"
            "doc_outline＝見出し構造／read_doc＝通読（続きは start_line）／read_around＝周辺精読／"
            "graph_neighbors＝関係グラフの関連部品／es_search＝日本語全文検索／"
            "xlsx_sheets＝Excelのシート一覧／xlsx_range＝Excelのセル範囲／"
            "docx_paragraphs＝Wordの段落・表／pptx_slides＝PowerPointのスライド／"
            "pdf_pages＝PDFのページ／file_head＝テキスト・コードの先頭）を使って、"
            f"資料（{_kb_hint_abs(world)}）と関係グラフを**自分で調べてください**。"
            f"{_read_block}"
            "まずツール（台帳・全文検索・グラフ）で当たりを付けてから、原本の中身を確かめて答える。"
            f"{_BOTH_SIDES_SENTENCE}"
            f"{_layer_block}"
            "確定した事実と推定は分けて書く（詳細ルールは AGENTS.md）。"
            "検索ヒットや精読結果に text_truncated が付いていたら、その本文は途中で切れている。"
            "結論を出す前に read_around か read_doc で続きを読む。続きを取得する手段が無い打ち切り"
            "（file_truncated・pdf_pages の text_truncated・compare_documents／graph_neighbors／glob_search／doc_outline の truncated・folder_tree の folders_truncated・xlsx_sheets／ripgrep_search／es_search の truncated＝ヒット数上限）は"
            "その範囲を未確認として明示し、全件性を主張しない。"
            "**ドキュメント数・一覧・どんな資料があるか・フォルダ構成といった台帳質問は、まず list_docs を使う**"
            "（grep は本文中の一致しか探せず件数/一覧には答えられない）。フォルダ名・ファイル名はパスに含まれる"
            "ので、名前の部分一致は list_docs の name_pattern で当てる（grep で本文からは探さない）。"
            "表記が揺れそうな語は短い部分語で試す（例:「4期更改」がヒットしなければ「4期」）。"
            "**件数を答えるときは list_docs の path_prefix でフォルダを確定してから数え、どのフォルダを数えたかを"
            "回答に明示する**（曖昧なら『4期更改』と『4期保守』のように候補フォルダ別の内訳で答える）。"
            "**一覧を求められたら該当する全件を各項目のパス付きで列挙する（省略しない・件数と一致させる）。**"
            "全件・一覧の完了は対象範囲の確認を終えてからで、検索3回や件数だけの取得では完了とせず、"
            "中断（利用者停止・通信エラー・予算到達）のときは確認済み／未確認／理由を分けて書き、"
            "部分結果を「全件」と断定しない。"
            "原因の手がかりや関連部品（呼び出し/コピー/参照/関連文書）をたどるときは graph_neighbors を使う。"
            # 影響を問う質問の分解の型。表層の症状語で検索を乱発させず、変更対象と
            # 影響先の「接続（経路）」の有無を根拠に答えさせる。
            "**影響を問う質問（「〜を変えたら」「〜に影響ある？」「〜が落ちる？」など）では、"
            "①変更対象（例: 税率）に依存する部品・記述を特定 → ②影響先（例: 夜間バッチ＝JCL/ジョブ）を特定 → "
            "③両者の接続（COPIES／INVOKES／ACCESSES／CONTAINS＝構造的な依存の経路）を graph_neighbors で"
            "当たる。graph_neighbors は近傍ごとに辺の種類と向き（from→to）を返す——COPIES／INVOKES／"
            "ACCESSES／CONTAINS だけで構成された経路は根拠にしてよい。影響は矢印をさかのぼる（A →COPIES→ B は"
            "B を変えると A が影響を受ける・変更対象から出ていく矢印の先は影響先ではない）。経路に DOCUMENTS"
            "（言及）・CORRESPONDS_TO の辺や unverified の辺（裏付け原本が実在確認できない）が 1 本でも含まれる"
            "近傍は候補どまり＝原本で確認する。経路の先の"
            "実際の記述を引用したいときだけ原本を開き、接続の有無を根拠として答える（向きは平易語で・"
            "内部のエッジ名は本文に出さない）。"
            "質問中の症状表現（落ちる/止まる/エラー/停止 等）をそのまま検索語にしない**"
            "（原因調査＝トラブルシュートだと明示された時のみ症状語で探してよい）。"
            # ask_user の使用条件（agentic と同じ制約）＋乱用ガード（確認ID 付きは再質問しない・1回まで）。
            # 発動基準を具体化（lens 別の例）＋ユーザー主導の確認要求を確実な発動手段にする。
            "調査範囲・目的・選択肢が曖昧で、確認しないと結果が大きく変わる場合だけ ask_user でユーザに確認する"
            "（例: 影響分析で起点や影響先が複数候補に割れるとき、確実な波及が0件で要確認だけになったときは、"
            "対象の絞り込みを ask_user で確認してよい）。"
            "**依頼文に「確認してから進めて」（同義: 確認してから／聞いてから進めて）が含まれる場合は、"
            "調査より先に必ず ask_user で要件を確認してから進める**"
            "（通常はシステムが先に確認カードを出すので、届いた依頼にこの句が残っていて「確認ID:」が"
            "無いときだけ自分で ask_user する）。"
            "（質問は1実行につき1回まで・質問後は追加調査をせず、ここまでに確認できたことをまとめて終了する）。"
            "**ただし依頼に「確認ID:」が含まれる場合は前の質問への回答なので、上の指示より再質問禁止を優先し、"
            "ask_user は使わずその回答に従って進める**（同じことを再度聞かない＝再質問ループ防止）。"
            "**途中経過だけの応答（「次に〜を調べます」など）で終えない。調査を最後まで進めてから、"
            "結論と根拠を最終回答として書く。**"
        )
        if lens == "author":
            # author は MCP ツールで根拠を集めたうえで成果物ファイルを authoring 直下に作る。
            # author は列構成・粒度など仕様が曖昧な場面が多い＝着手前の確認が「作ってから直す」より安い。
            return sysp + base + (
                " 調べた内容を根拠に、**成果物ファイルをこのディレクトリ（authoring 直下）に作成してください**。"
                "**仕様（列構成・粒度・対象範囲など）が曖昧で結果が大きく変わる場合は、着手前に ask_user で確認する**。"
                "Excel/Word/PowerPoint 等を作る場合は `.agents/skills` 配下のスキル（xlsx/docx/pptx の"
                " SKILL.md）を確認して活用する。"
                # スライド/プレゼンは既定 Marp（見た目重視）・後で PowerPoint 編集なら python-pptx。
                # Codex は marp の .md を書くだけでよい（レンダは Sherpa 側が完了後に自動実行するので、
                # marp CLI の有無をここで判断する必要は無い）。
                "**スライド・プレゼン資料は見た目重視の marp スキル（HTML/PDF/PPTX）を既定で使う**。"
                "marp スキルでは Marp 形式の `.md` を書くだけでよく、レンダ（HTML/PDF/PPTX への変換）は"
                "この作業の完了後に Sherpa 側が自動で行う（自分でレンダコマンドを実行する必要は無い）。"
                "「あとで PowerPoint で編集したい」と明示された場合だけ、"
                "marp を使わず pptx スキル（python-pptx）で作る。"
                "最後に**作成したファイル名**と**内容の要約**を"
                "日本語で報告してください。\n\n"
                # 履歴があれば【依頼】の前に前置（空文字なら従来と完全同一の出力）。
                f"{self._history_block()}【依頼】{message}")
        # R1a: 履歴があれば【質問】の前に前置（空文字なら従来と完全同一の出力）。
        return sysp + base + " " + _NO_FILES_SENTENCE + f"\n\n{self._history_block()}【質問】{message}"

    def _prompt_plain(self, message, lens, world, mcp: bool = True):
        """素の Codex（`plain`・docs/proposals/2026-09-24-素のCodexモード.md §1.2）向けプロンプト。

        Sherpa 側の調べ方の上乗せ（MCP ツール一覧と使い分け・list_docs 誘導・investigate スキル
        誘導・台帳・影響調査の手順の長文・原因調査の症状語の指示）は持たない——Codex 本来の調べ方
        （シェルで直接読む）に任せる。containment（範囲・秘匿は読まない）・出典書式・ask_user の
        使い方は AGENTS.md（`codex_agents_md.AGENTS_MD_PLAIN`）と重複しても多層防御として常置する
        （`_prompt_mcp` と同じ理由・AGENTS.md 書込失敗時の保険）。"""
        sysp = (self.system_prompt + "\n\n") if self.system_prompt else ""
        from ... import worlds
        base = (
            "あなたは社内ナレッジ調査エージェントです。シェル（rg・sed・cat など）で次の資料"
            "フォルダを直接読んで調べてください。\n"
            f"- 原本: {_kb_hint_abs(world, layout_hint=False)}\n"
            "- 変換済みテキスト（Word・Excel・PowerPoint・PDF・画像。原本の代わりにこちらを読む。どちらも"
            "原本と同じフォルダ構成・UTF-8）: "
            f"{worlds.derived_rag_dir(world)}（「<元のファイル名>.rag.md」・正本）と "
            f"{worlds.derived_md_dir(world)}（「<元のファイル名>.md」）\n"
            "- ソース（.c・.bas・COBOL・JCL など）は Shift_JIS のことが多い。日本語の語で探すときは "
            "`rg -E sjis` を使う（英数字の名前はそのままで当たる）。rg が無ければ `grep -rn` を使い、"
            "Shift_JIS のソースは探す語を `iconv -t SJIS` で変換してから探す。\n"
            "**指定された資料フォルダ・変換済みテキスト以外（このディレクトリの外・ユーザー"
            "workspace・秘匿名のファイル（.env／鍵／credentials 等）等）は絶対に読まない。**"
            f"{_BOTH_SIDES_SENTENCE}"
            f"{_READ_ONLY_SENTENCE}"
            "作業用のファイルは"
            "このディレクトリ直下に作らず `.tmp/` の下に作る。"
            + ("MCP サーバ『sherpa』の graph_neighbors（呼び出し／コピー／参照のつながり）の"
               "ツールも使ってよいが、必須ではない。" if mcp else "") +
            "利用者向けに整理して日本語で答えてください。確定した事実と推定は分けて書く。"
            # 出典の書き方は _prompt_mcp の _read_block と同じ文言（そのまま流用）。
            "回答の最後に『参照した資料:』の行を置き、実際に開いて根拠にした資料を1行1件、"
            "資料フォルダからの相対パス（例 `4期更改/02_設計/xxx.xlsx`）で列挙する。"
            "Sherpaがこれを出典（原本ダウンロード）に変換する。派生MD／rag.mdを見た場合も原本のパスで書く。"
            # ask_user の使い方・確認ID の再質問禁止は _prompt_mcp と同じ文言（そのまま流用）。
            "調査範囲・目的・選択肢が曖昧で、確認しないと結果が大きく変わる場合だけ ask_user でユーザに確認する"
            "（例: 影響分析で起点や影響先が複数候補に割れるとき、確実な波及が0件で要確認だけになったときは、"
            "対象の絞り込みを ask_user で確認してよい）。"
            "**依頼文に「確認してから進めて」（同義: 確認してから／聞いてから進めて）が含まれる場合は、"
            "調査より先に必ず ask_user で要件を確認してから進める**"
            "（通常はシステムが先に確認カードを出すので、届いた依頼にこの句が残っていて「確認ID:」が"
            "無いときだけ自分で ask_user する）。"
            "（質問は1実行につき1回まで・質問後は追加調査をせず、ここまでに確認できたことをまとめて終了する）。"
            "**ただし依頼に「確認ID:」が含まれる場合は前の質問への回答なので、上の指示より再質問禁止を優先し、"
            "ask_user は使わずその回答に従って進める**（同じことを再度聞かない＝再質問ループ防止）。"
            "**途中経過だけの応答（「次に〜を調べます」など）で終えない。調査を最後まで進めてから、"
            "結論と根拠を最終回答として書く。**"
        )
        if lens == "author":
            # author 向けの成果物の作り方（marp/pptx の使い分け）は _prompt_mcp と同じ文言（そのまま流用）。
            return sysp + base + (
                " 調べた内容を根拠に、**成果物ファイルをこのディレクトリ（authoring 直下）に作成してください**。"
                "**仕様（列構成・粒度・対象範囲など）が曖昧で結果が大きく変わる場合は、着手前に ask_user で確認する**。"
                "Excel/Word/PowerPoint 等を作る場合は `.agents/skills` 配下のスキル（xlsx/docx/pptx の"
                " SKILL.md）を確認して活用する。"
                "**スライド・プレゼン資料は見た目重視の marp スキル（HTML/PDF/PPTX）を既定で使う**。"
                "marp スキルでは Marp 形式の `.md` を書くだけでよく、レンダ（HTML/PDF/PPTX への変換）は"
                "この作業の完了後に Sherpa 側が自動で行う（自分でレンダコマンドを実行する必要は無い）。"
                "「あとで PowerPoint で編集したい」と明示された場合だけ、"
                "marp を使わず pptx スキル（python-pptx）で作る。"
                "最後に**作成したファイル名**と**内容の要約**を"
                "日本語で報告してください。\n\n"
                f"{self._history_block()}【依頼】{message}")
        return sysp + base + " " + _NO_FILES_SENTENCE + f"\n\n{self._history_block()}【質問】{message}"

    def _plain_text(self, message: str = "") -> str:
        # ナレッジ参照オフでは Codex CLI を起動しない（read-only でも grep/ファイル読取が可能で
        # KB を覗けてしまうため）。
        # Codex 構成は資料参照ON固定になったため、通常この経路には来ない
        # （画面はトグルをON固定・`routers/chat.py::_knowledge_for` がサーバ側でも強制）。
        # 内部経路や古いクライアントが knowledge=False で呼んだ場合の安全網としてだけ残す。
        return ("Codex は常に社内資料を参照して回答します。"
                "資料を参照しない雑談は OpenAI／ローカルLLM を選んでください。")

    def run(self, ctx: Ctx) -> Iterator[dict]:
        # `_GenProvider.run()` と同じく分岐前に確定させる（`_prompt`/`_prompt_mcp` が
        # `_run_authoring` から参照する）。
        self._history = list(ctx.history or [])
        if not ctx.knowledge:                          # ナレッジ参照オフ＝素の会話（Codex を grep なしで・authoring 不使用）
            yield from _plain_run(self, ctx); return
        yield from self._run_authoring(ctx)

    def _run_authoring(self, ctx: Ctx) -> Iterator[dict]:
        decision = env = None
        _turn_t0 = time.monotonic()   # `sherpa.usage` ログ 1 行の elapsed（このターン全体）
        # 素の Codex モード（`codex_mode`・docs/proposals/2026-09-24-素のCodexモード.md §1.1）:
        # ターンの最初に1回だけ決め、以下の全箇所（プロンプト・AGENTS.md・スキル配備・出力
        # スキーマ・multi_agent・台帳・MCP env・codex.log 開始行・activity.settings）で同じ値を
        # 使う。`standard`（既定）はこのフラグが常に偽のまま＝以降の分岐は全て else 側（現行の
        # まま・比較の基準）を通る。
        _plain = codex_mode(self._system_settings) == "plain"
        # 決まった手順の下調べ（`_gather` の `ctx.dispatch`）を省くのは、Codex を MCP 付きで起動する
        # 見込みのターン（CLI が有る・MCP 有効）で、レンズが `_PRESEARCH_SKIP_LENSES` のときだけ。
        # `ws_authoring`/`run_dir`/`_codex_home_ok`（実際に起動できるか）はこの時点では計算できない
        # ため含めない——それらで結局起動しなかったターンも省いたまま扱う。判定は `_gather` の前に
        # 1回だけ行い、後段（`mcp` 変数・起動ガード・未応答時のノード文言）でも同じ値を使う。
        _codex_bin = shutil.which("codex")
        _mcp_enabled = _codex_mcp_enabled()
        _skip_lenses = _PRESEARCH_SKIP_LENSES if (_codex_bin and _mcp_enabled) else frozenset()
        # シーム規則（モジュール docstring 参照）: `_gather` は「危険な継ぎ目」（複数テストが
        # `agents._gather` を monkeypatch して介入を検証する）。本モジュールは agents.py（facade）
        # からモジュールレベルで import されるため、逆にモジュールレベルで `from sherpa import agents`
        # すると循環 import になる → 関数内で遅延 import し facade 属性経由で実行時解決する。
        from sherpa import agents as _facade
        for ev in _facade._gather(ctx, skip_presearch_lenses=_skip_lenses):
            if isinstance(ev, dict) and ev.get("type") == "_env":
                decision, env = ev["decision"], ev["env"]
            else:
                yield ev
        if env is None:                                # _gather が clarify question を出して停止＝確認待ち
            return
        _skip_presearch = decision["lens"] in _skip_lenses

        yield _node("codex", "think", "Codex が調べる", "資料を調べています", "active")
        answer, ran = None, False
        # 閉域キットが Codex CLI を同梱している場合（scripts/install_offline_kit.sh 7b）、
        # 「CLI はあるが認証が無い」状態が起こりうる。
        # このとき codex exec は即座に非ゼロ終了・stdout に JSON を1行も出さない（実測）。
        # 起動前ガード（shutil.which 不在・config書込み例外・.codex-sessions symlink 等）で
        # 一度も codex exec を起動していないケースと区別するため、if ブロック内でだけ True にする
        # （if ブロックが丸ごとスキップされた経路ではこの既定値 False のまま＝既存の決定的回答
        # フォールバックを維持・tests/unit/test_codex_resume.py の pinned "dispatch-headline" と非衝突）。
        _codex_silent_failure = False
        # 同じ理由（if ブロックが丸ごとスキップされる経路がある）で、自動継続の
        # `env["codex_stopped_early"]` 判定用フラグも既定 False にしておく——`_agent_msgs` 等が
        # 存在するのは if ブロック内だけのため、実測値への上書きもそこでだけ行う。
        _codex_stopped_early = False
        # if ブロックが丸ごとスキップされる経路（`ws_authoring`/`run_dir` が None・shutil.which 不在等）
        # では Popen 自体を試みていない＝技術的失敗ではないため既定 False（第3分岐で参照するため
        # ここで定義しておく必要がある・if ブロック内だけで代入すると NameError になる）。
        _stream_error = False
        codex_question = None                                    # ask_user 由来の question（出たら env/_result を出さずターン終了）
        codex_usage = None                                       # turn.completed の usage（best-effort・出なければ None）
        # 利用統計 activity（正典§3.1）: Codex 専用区間の開始/終了（`time.monotonic()`）。開始は
        # 最初の `subprocess.Popen` 直前（1回だけ設定）・終了は直近の `proc.wait()` 直後（attempt
        # ごとに更新）——準備（スキル配備・config生成）や後処理（子 usage 走査）を含めない。
        _agent_start_mono: float | None = None
        _agent_end_mono: float | None = None
        # resume 試行が失敗し新規セッションへ切り替わったら True にする（if ブロックが丸ごと
        # スキップされる経路もあるためここで既定 False・usage のターン差分判定に使う）。
        _resume_fallback_happened = False
        # サイドカーの吸収を許可してよいかの唯一のゲート（既定 False＝`if` ブロックが丸ごと
        # スキップされる経路では finally の無条件呼び出しを無効化する）。sandbox 有効時は
        # 事前 unlink・設定生成が両方成功した時、非サンドボックス（フォールバック）経路は
        # `.tmp/` が run_dir 生成のたびに空から始まるため env 配線が済んだ時点で立てる
        # （`codex_home is None` かどうかでは判定しない——`_absorb_mcp_sidecar` 参照）。
        _sidecar_init_ok = False
        # `graph_neighbors` の mcp_tool_call item が旧世代
        # グラフの構造化エラー（`_graph_schema_era_from_item`）を運んできたら、ここへ捕まえておく。
        # 検知しても調査は止めない（§0(c)・グラフ不調は回答不能の理由にしない）——Codex は同じ
        # MCP から grep/原本読取ツールを引き続き使えるため、このフラグは「縮退した」という印
        # だけに使い、終了後の env に冒頭告知と統計フラグとして載せる。
        _graph_schema_era_error = None
        # 子（`spawn_agent` された worker/evaluator）がサイドカー経由で報告した障害コード
        # （`mcp_server._SIDECAR_ERROR_CODES`・親の `--json` には現れない）。
        _mcp_error_codes: list = []
        # MCP ツール結果のバイト予算（`mcp_server.py` の `{"kind":"limit",...}`）——1件あたりの
        # クリップ件数（累算）と、累計予算到達（bool・一度立てば真のまま）。利用統計「打切りの
        # 内訳」（`env["limits"]`）へそのまま合流させる。
        _mcp_tool_result_clipped = 0
        _mcp_total_budget_hit = False
        # 同一クエリの重複実行の抑止（`mcp_server.py` の `{"kind":"limit","field":
        # "duplicate_tool_call"}`）——件数を累算し、上の2つと同じ経路で `env["limits"]` へ合流させる。
        _mcp_duplicate_tool_call = 0
        # `mcp_server.py` が `run_tool()` 自身の内部切り詰め（grep ヒット上限・件数上限等）を
        # 検知して書いた `{"kind":"limit","field":"search_truncated"}` の件数——上の3つと同じ
        # 経路で `env["limits"]["search_truncated"]` へ合流させる（API 経路の
        # `agentic_search._record_run_tool_limits` と同じ判定・同じ語彙）。
        _mcp_search_truncated = 0
        # ツール呼び出し回数の上限到達（`mcp_server.py` の `{"kind":"limit",
        # "field":"tool_calls_exhausted"}`・クイックを本当に速くする・変更D③）——bool・一度立てば
        # 真のまま。上の `_mcp_total_budget_hit` と同じ合流方式で `env["limits"]` へ載せる。
        _mcp_tool_calls_exhausted = False
        # ガード: 確認ID 付き再送（前の質問への回答）では ask_user を無視＝再質問ループ防止
        # （chat.js が回答再送に `確認ID: {interaction_id}` を必ず含める・chat_router の marker と同流儀）。
        _ask_disabled = bool(re.search(r"確認ID[:：]", ctx.message or ""))
        mcp_neighbors: list = []                                 # Codex が graph_neighbors で引いた近傍（UI カードに反映）
        # MCP の read 系ツール（read_doc/read_around/
        # doc_outline/compare_documents）の引数から集めた doc_id。attempt をまたいで合算する（自動継続の
        # 複数 codex exec プロセスにまたがるため）。最終 answer の「参照した資料:」ブロックの解析結果に
        # 合流させ、機械検証してから env["sources"] へ足す（原本直読は MCP の結果に載らず出典に出ない穴の
        # 補完）。
        _mcp_read_docs: list = []
        # `xlsx_sheets`（シート一覧のみ・本文は読んでいない）は上と分けて集める——
        # sources（参照候補）には合流させるが、根拠ゲート（sources_verified）には数えない
        # （`xlsx_sheets` の呼び出しだけを根拠に「精読済み」を偽装させない）。
        _mcp_listed_docs: list = []
        # MCP ツール呼び出しの並走計測（run 全体の合算値）。item id は codex exec プロセスごとに
        # 振り直される（`item_0` 等が採番し直される）ため、id の集合（`seen`/`open`）は `_attempt`
        # （1プロセス=1回の codex exec）内のローカル変数として毎回作り直し、attempt 終了時（finally）
        # にこの run-level dict へ合算する——resume 失敗時のフォールバック再試行・自動継続
        # （`_CONTINUE_PROMPT` ループ）はいずれも複数プロセスにまたがるため、id をまたいで共有すると
        # 別プロセスの同名 id を同一呼び出しと誤認し、総数を過少計上する。attempt は逐次実行（同時に
        # 走らない）ため、`max_in_flight` は attempt ごとの最大値の**最大**（合計ではない）を取る。
        _mcp_calls = {"total": 0, "max_in_flight": 0}
        # `codex.log`（運用ログ）向け: `--json` イベントの種類別件数（attempt をまたいで合算・
        # `_absorb_last_message_fallback`/turn.completed 等の既存カウンタとは独立の観測専用の集計）。
        _event_type_counts: dict[str, int] = {}
        # DEPTH-2 S3b/S6: `spawn_agent` した子スレッドの id（`collab_tool_call` item から捕捉・run 全体で
        # 合算＝attempt をまたいでも良い＝多重 spawn/継続でも同じ子を重複して数えない set）。
        # multi_agent 無効（サンドボックス無効・OpenAI custom 未設定等）では `collab_tool_call`
        # item 自体が出ないため常に空のまま。`--json` の `spawn_agent` item は CLI のバージョンに
        # よって出ないことがあるため、これは子検出の**片方**（旧形式）に過ぎない——
        # `_collect_child_token_usage` は空でも `thread_id`（親）があれば rollout の
        # `parent_thread_id` 突合（新形式）で子を見つける。
        _child_thread_ids: set = set()
        # 利用統計 activity（正典§3.1）: このターンで「親」として使った thread_id を時刻順に
        # 重複なく記録する（run 全体で合算）。通常は1件だが、resume が失敗して新規スレッドへ
        # フォールバックしたターンは2件になる——`thread_id`（nonlocal）はフォールバックで
        # 上書きされるため、切り替わる**前**に控えておかないと前の親とその子の消費が要約から
        # 落ちる（`activity.summarize_turn` は複数の親を1つの parent エントリへ合算する）。
        _all_parent_thread_ids: list = []
        _child_usage_totals = {k: 0 for k in _CHILD_USAGE_KEYS}
        _child_usage_found = 0
        _child_usage_missing = 0
        # 起動を検出できた子の総数（found + missing）。usage を読めたかどうかとは別の集計——
        # `spawn_agents`（codex.log 終了行）はこちらを使う（usage 未取得の子を
        # 「起動していない」扱いにしない）。
        _child_usage_detected = 0
        codex_created_files: list[str] = []                      # 実行後に台帳登録する新規ファイルの絶対パス
        _any_new_ws = False                                       # codex 未インストール時の NameError 防止
        _created_file_rows: list[dict] = []                       # 台帳登録に成功した行（env["created_files"] 用）
        # move／台帳登録が1件でも失敗したら True（run_dir を消さず回収用に残す・
        # 回答本文へ注記を足す判定に使う）。
        _created_files_failed = False
        # 専用 authoring ディレクトリを cwd に。個人アップロード(files/)から分離。
        # KB は絶対パスでプロンプトに渡す。authoring/workspace に symlink が
        #   混入していると封じ込めが崩れるため、_safe_workspace_authoring で symlink 拒否＋fail-closed。
        users_dir = Path(os.environ.get("SHERPA_USERS_DIR", "data/users")).resolve()
        uid = ctx.uid or "admin"
        ws_authoring = _safe_workspace_authoring(users_dir, uid)   # None＝fail-closed（Codex 起動しない）
        # 実行ごとの専用作業領域（cwd/書込 root）。同一 uid の複数実行が別々の run dir を使うため、
        # 直列化 lock は不要（sandbox._safe_run_authoring 参照）。None＝run dir が作れない＝
        # ws_authoring is None と同じ fail-closed（Codex を起動しない）。
        run_dir = _safe_run_authoring(users_dir, uid)
        # 会話単位ロック（`_session_persistence_enabled` の時だけ後段で実値になる）。ここで
        # 既定値を確定しておく——ブロック内の代入より前で例外が起きても finally が参照できるよう
        # にする（`_conv_lock_acquired` は実際に自分が取得できた時だけ True＝busy 早期 return では
        # 他者が保持するロックを誤って解放しない）。
        _conv_lock = None
        _conv_lock_acquired = False
        # 調査台帳（正典§3/§4・`investigation_ledger.py`）: `.tmp/investigation/` 作成前に早期
        # return する経路（busy・MCP 無効等）でも finally が安全に参照できるよう既定値を先に確定する。
        _investigation_dir = None                 # run_dir/.tmp/investigation（.tmp 作成直後に確定）
        _ledger_home = None                        # workspace/.codex-sessions/{cid}（永続会話のみ）
        # 台帳の完了判定へ足す追加の必須根拠種別（`ledger_complete`/`no_progress`/
        # `_retire_investigation_ledger` の全呼び出しへ同じ値を渡す・ターンの最初に1回だけ決める）。
        # `sp`/`decision["lens"]` が確定した直後に実値へ上書きする——それより前に例外・早期
        # return（busy・MCP 無効等）が起きても finally が空タプルを安全に参照できるよう先に確定する。
        _ledger_required_extra: tuple[str, ...] = ()
        _investigation_restored = False
        _investigation_verdict = None              # investigation_ledger.Verdict（ゲート確定後に埋める）
        _ledger_continuations = 0
        _investigation_stopped_reason = None
        # RV 中-3（2026-09-22 3巡目是正）: 通常終了時は `env["investigation"]` を組み立てる前に
        # 退避（`_retire_investigation_ledger`）を実行し、この flag を立てる——外側 finally の
        # 退避（切断・例外時のフォールバック）はこの flag が立っていれば二重に実行しない。
        _investigation_retire_done = False
        # RV 中-1（2026-09-22 10巡目是正）: 復元（前ターンの退避台帳→run_dir）が途中で失敗した
        # ターンは、この run では退避（削除・置換）を一切行わない——元の（より完全な）退避台帳を
        # 保持し、次ターンの「続き」でもう一度復元を試せるようにする。`_investigation_retire_done`
        # を早期に立てて通常終了・finally 双方の退避呼び出しを止める（下の復元処理で設定する）。
        _investigation_restore_failed = False
        # 台帳登録（files/ move）まで完了した後で必ず削除する（正常終了・停止・例外の
        # いずれでも同様＝GeneratorExit（クライアント切断相当の generator.close()）が本体のどの
        # yield 点で飛んできても finally は必ず実行される）。会話ロックの解放もこの finally で行い、
        # 成果物の move／台帳登録・最終回答（`_result`）の送出までロックを保持する
        # （途中で解放すると、同じ会話の次ターンが古い `codex_session_id`／履歴のまま
        # 割り込める窓ができる）。
        try:
            # 会話継続（Codex ネイティブ resume）: conversation_id があるターンだけセッションを
            # 永続化する（chat_service 経由のチャット呼び出しは常に有り。conversation_id 無しの直接呼出し
            # ＝既存テスト等は従来どおり per-request 使い捨て CODEX_HOME＋`--ephemeral` のまま・無改修）。
            _persist_session = ctx.conversation_id is not None
            resume_sid = ctx.codex_session_id if _persist_session else None
            thread_id = None   # 捕捉した Codex session/thread id（_session_persistence_enabled の時だけ env に載せる）
            # `SHERPA_CODEX_SANDBOX=0`（緊急避難経路）は常に `--ephemeral`
            # 実行のため、そこで捕捉した thread_id は resume 不能（ディスクに残らない）。この専用フラグで
            # 「DB へ永続化してよいか」を判定する（`_persist_session` 単独だと fallback 経路の使い捨て
            # thread_id まで DB に保存し、サンドボックス復帰後の resume が永久に失敗し続ける穴があった）。
            _session_persistence_enabled = _persist_session and _codex_sandbox_enabled()
            # 永続 CODEX_HOME を使う実行だけ、同一会話単位で非ブロッキング lock を取る
            # （run dir 自体は実行ごとに独立なので対象外）。削除エンドポイント
            # （routers/conversations.py::conversation_delete）も同じロックを DB 変更前に取る——
            # ロック取得前に `.codex-sessions/{cid}` の mkdir や会話の生存確認を行うと、削除と
            # 競合して「削除直後に孤児ディレクトリを作り直す」窓ができるため、ロック取得後まで遅らせる。
            _conv_lock = _conversation_lock(ctx.conversation_id) if _session_persistence_enabled else None
            if _conv_lock is not None:
                _conv_lock_acquired = _conv_lock.acquire(blocking=False)
            if _conv_lock is not None and not _conv_lock_acquired:
                msg = "この会話の別の回答を実行中です。終わってからもう一度お試しください。"
                yield _node("codex", "think", "Codex が調べる",
                           "（この会話の別の回答を実行中のため今回は実行しません）", "done")
                yield {"type": "answer_delta", "text": msg}
                sm = layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world, lens=decision["lens"])
                sm["source"] = "busy"
                env = {"lens": decision["lens"], "headline": msg, "summary": {"total": 0},
                      "data": {}, "sources": [], "busy": True, "scope": sm}
                yield {"type": "_result", "env": env,
                      "decision": {"lens": decision["lens"], "input": ctx.message,
                                  "reason": "同一会話の Codex 実行が進行中"}}
                return
            # 永続 CODEX_HOME（`.codex-sessions/{cid}`）は固定パスのため、
            # 事前に symlink を仕込まれると（未検証のまま書込むと）封じ込めが崩れる。`ws_authoring` と
            # 同じ fail-closed 契約＝安全確認できなければ Codex を起動しない（このターンは決定的回答へ）。
            # ロック取得後に会話の生存（所有 DB 行）も再確認する: チャット受付からここまでの間に
            # 会話が削除されていれば mkdir せず実行しない（削除側は同じロックを取ってから DB を消すため、
            # ここに到達している時点で削除が完了していれば所有行は既に無い）。DB 到達不可（一時的な
            # 障害）はこの確認の対象外＝fail-open で実行を続ける（`owns_conversation` を新たに必須の
            # 単一障害点にしない・system_settings 読取と同じ方針＝上の `sysset` 解決参照）。
            _safe_persistent_codex_home = None
            _codex_home_ok = True
            if _session_persistence_enabled:
                from ... import store as _store
                try:
                    _conv_alive = _store.owns_conversation(uid, ctx.conversation_id)
                except Exception:
                    _conv_alive = True
                if not _conv_alive:
                    _codex_home_ok = False
                else:
                    _safe_persistent_codex_home = _safe_codex_sessions_home(users_dir, uid, ctx.conversation_id)
                    _codex_home_ok = _safe_persistent_codex_home is not None
            _auto_continue_count = 0   # limits（利用統計計測）: Codex を起動しない経路でも参照するため起動条件の外で初期化
            _multi_agent_enabled = False   # env["codex_multi_agent"] 用: Codex を起動しない経路では常に偽
            if _codex_bin and ws_authoring is not None and run_dir is not None and _codex_home_ok:
                # agent_message は run 中に複数届く（作業宣言＋結論）。最後の1件を鵜呑みに
                # せず全部集めて後で結論を選ぶ（`_pick_codex_headline`）。try の外で初期化＝Popen 失敗の
                # except 経路でも NameError にしない。
                _agent_msgs: list[str] = []
                _agent_partial = ""
                # stream 読取が途中例外で終わったか。例外時は集めた _agent_msgs が
                # 進行中の作業宣言だけの可能性があるため、完全版が入り得る `-o` 最終メッセージファイルを先に試す。
                _stream_error = False
                # 出力スキーマ有効時（`_schema_on`）だけ使う状態（§2-3）: `_latest_structured` は最新
                # attempt の最終出力を検証した結果（合格した dict・不合格/欠落は None）。`_structured_answers`
                # は attempt をまたいで合格した dict を積む（見出し選択・§2-5 用）。
                _latest_structured: dict | None = None
                _structured_answers: list[dict] = []
                # RV 高-1（2026-09-22）: 台帳ゲートが「未完了のため受理しない」と判断した final は
                # 見出し/主張候補として二度と拾わない——`_structured_answers[:_structured_answers_valid_from]`
                # は拒否済み扱い。`_pick_structured_headline`/`_pick_structured_claims` はこの境界より
                # 後だけを走査する（台帳ゲートが継続を発行する直前にこの境界を更新する）。
                _structured_answers_valid_from = 0
                mcp = _mcp_enabled                                  # MCP ツールで自律調査（既定ON）
                sp = (ctx.scope_meta or {}).get("scope_paths")
                # Codex 自身の追加探索（MCP／直接grep）への層フィルタは qa レンズだけに渡す（探す対象）。
                # author は Codex の追加探索が正典 §1.8 の既知の非対称性（agentic_search.run_tool を
                # 経由しない構成）のため対象外・impact/troubleshoot は非適用（layer.applies_to_lens と
                # 同じ結論だが author も除外するためここでは共通ヘルパーを使わず明示判定する）。
                _layer = (ctx.scope_meta or {}).get("layer") if decision["lens"] == "qa" else None
                # 台帳の完了判定へ足す source の必須化は、ターンの最初に1回だけ決め、このターンの
                # 台帳ゲート呼び出し全て（required_extra・AGENTS.md の台帳段落・ledger_status への
                # env）へ同じ値を渡す（後段で値がぶれると `_ledger_progressed` の前後比較が誤判定
                # する）。判定は MCP へ実際に渡す実効の層＝`_layer` を使う——`_apply_codex_evidence_
                # gate` 用の `layer_mod.effective_layer` は author にも scope_meta.layer をそのまま
                # 返すが、author への MCP 探索は `_layer` が常に None（層なし）＝実際にはソースを
                # 探索できる。台帳の書込手段は MCP ツールだけのため、MCP 無効なら台帳自体を作らない
                # ＝計算しない（既定の空のまま）。plain も台帳ツール自体を出さないため同様に
                # 計算しない（`_scope_evidence_kinds` の範囲全木走査を省く）。
                if mcp and not _plain:
                    _ledger_required_extra = _ledger_source_required_extra(ctx.world, sp, _layer)
                _layer_restricted = _layer not in (None, "both")
                # 層のフィルタは MCP ツール側（run_tool）だけが担う（直読は層に関係なく read・Codex に層は
                # 強制しない）。MCP 無効・sandbox 無効の構成では層の指定をツールに渡す経路が無いため、
                # 黙って無視せず実行前に正直に失敗する。
                _layer_enforcement_ready = mcp and _codex_sandbox_enabled()
                if _layer_restricted and not _layer_enforcement_ready:
                    # 黙って層を無視した回答を返さず、実行せず正直に失敗を伝える（未計測＝Codex CLI を
                    # 一度も起動しない）。利用者向け文言・進捗表示は専門用語ゼロ（MCP/sandbox を出さない・
                    # docs/04 §6）——具体的な理由は decision.reason（監査・管理者ログ専用）にだけ残す。
                    msg = "この構成では探す対象の限定はできません。管理者に設定の確認を依頼してください。"
                    yield _node("codex", "think", "Codex が調べる", "探す対象の限定に対応していません", "done")
                    yield {"type": "answer_delta", "text": msg}
                    env = {"lens": decision["lens"], "headline": msg, "summary": {"total": 0},
                          "data": {}, "sources": [],
                          "agentic_failure": "error",   # 実行していないターン＝完了として数えない
                          "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world,
                                                              lens=decision["lens"])}
                    _reason = ("MCP 無効時は探す対象の限定に対応できません" if not mcp
                              else "sandbox 無効時は探す対象の限定に対応できません")
                    yield {"type": "_result", "env": env,
                          "decision": {"lens": decision["lens"], "input": ctx.message, "reason": _reason}}
                    return
                # authoring/ = Codex の書込先（cwd）。files/ = ユーザーアップロード（cwd 外・Codex から隔離）。
                # BLOCKER-2: files/ ディレクトリ自体が symlink でも authoring/ は分離されているので安全。
                # files/ の symlink チェックはアップロード grep 側（chat_service._personal_grep_hits）で行う。
                ws_files = users_dir / uid / "workspace" / "files"
                if ws_files.is_symlink():
                    ws_files = None  # type: ignore[assignment]
                else:
                    ws_files.mkdir(parents=True, exist_ok=True)
                # 実行前の run_dir スナップショット（新規ファイル検出用）。
                _before_ws_files: set = set()
                _before_ledger_files: set = set()
                if ws_files is not None and ws_files.is_dir():
                    _before_ledger_files = set(ws_files.iterdir())
                if run_dir.is_dir():
                    # `.agents`（配備したスキル）配下も `.tmp` 同様に台帳登録スキャン対象外。
                    # ルート直下の AGENTS.md も対象外: スナップショット後に write_agents_md() が書くため、
                    # 除外しないと初回実行で「新規ファイル」誤認 → files/ へ move（run_dir から消える）→
                    # 次回また書かれて再検出…と毎回 AGENTS_N.md が台帳に蓄積する。
                    # `.mcp_sidecar.jsonl` も同様に対象外——sandbox 有効時は codex_home 配下に置くため
                    # ここには現れないが、フォールバック（`SHERPA_CODEX_SANDBOX=0`）で run_dir 直下に
                    # 残る場合に備えた保険（除外しないと成果物台帳登録・`codex_wrote_files` が立ち、
                    # 共有 KB だけを読む会話でも個人由来扱いになって通常共有を阻害する）。
                    _before_ws_files = {
                        p for p in run_dir.rglob("*")
                        if p.is_file() and not p.is_symlink()
                        and p.relative_to(run_dir) not in (Path("AGENTS.md"), Path(_MCP_SIDECAR_NAME))
                        and not ({".tmp", ".agents"} & set(p.relative_to(run_dir).parts))
                    }
                # reasoning=minimal は image_gen/web_search と非互換で API 400 になる（実証済）→ low へ引き上げ。
                # author（作成）のときは intent 連動パラメータ `SHERPA_CODEX_REASONING_AUTHOR`
                # （既定 medium）を使う。通常レンズは現行のまま（低負荷優先）。
                _is_author = decision["lens"] == "author"
                # 調べる深さ（調べ方ブロック §3.2）: 通常レンズの基準値だけ管理画面の基準値編集
                # （system_settings）を反映する（author 専用の env は別軸のため対象外・§1.6 の
                # `SHERPA_CODEX_REASONING` に対応する基準値のみ）。推論レベルは通常レンズの
                # クイックだけ `codex_reasoning_for` が1段下げる——標準以上と author はこの
                # 基準値のまま `codex exec` へ渡す。
                _base_reason = (os.environ.get("SHERPA_CODEX_REASONING_AUTHOR", "medium") if _is_author
                               else depth_profile_mod.effective_base(
                                   self._system_settings, "codex_reasoning", self._reason))
                # author（作成系）は専用の推論設定（`SHERPA_CODEX_REASONING_AUTHOR`）を持ち、
                # 通常レンズの基準値とは別軸＝クイックの1段下げも通さない（専用設定をそのまま使う）。
                # 素の Codex（plain）は「調べる深さ」を効かせない（推論レベルも基準値のまま）。
                _reason_raw = _base_reason if (_is_author or _plain) else depth_profile_mod.codex_reasoning_for(
                    _base_reason, (ctx.scope_meta or {}).get("depth_profile"))
                _reason = "low" if str(_reason_raw).lower() == "minimal" else _reason_raw
                # 利用統計の拡充: usage メタへ足す「実際に codex exec へ渡した
                # model_reasoning_effort」（`_reason`）と、基準値（minimal→low の丸め前）
                # （`_base_reason`）。一致（minimal 以外の通常ケース）なら `reasoning_base` は
                # 省略する（`usage_reasoning_extras` の契約）。
                _usage_depth_extra = depth_profile_mod.usage_reasoning_extras(
                    (ctx.scope_meta or {}).get("depth_profile"), _base_reason, _reason)
                # DEPTH-2 S6（§2.6・§5 S6）: multi_agent は既定で常時有効にする（深さに関わらず・
                # 「本体が worker を兼ねる」縮退は採らない裁定）。判定は `codex_multi_agent_enabled`
                # （sandbox.py・唯一の真実源＝doctor と条件式を共有し食い違いを防ぐ）に委ねる:
                # サンドボックス無効（フォールバック経路）は対象外。接続先は既定 OpenAI・Azure・
                # Ollama は常に対象（Azure/Ollama は worker/evaluator を本体と同じデプロイ名／
                # モデルタグへ倒す）。独自エンドポイント（custom）だけ `codex_worker_model` の明示
                # 設定が無いと対象外——本体のモデル名を流用できる保証が無いため（決定2026-09-19）。
                # `_review_rounds` は AGENTS.md へ埋め込む見直しの回数
                # （クイック 0／標準 2／深く 4／最大は管理画面の設定値。固定の段もその設定値で
                # 頭打ち）——Codex 自身はこの回数を強制されない
                # （spawn_agent の呼出上限を Sherpa 側が数えて止める仕組みは無い・指示のみ）。
                # plain（素の Codex モード）では下調べ役・見直し役を使わない——`codex_multi_agent_
                # enabled` の結果に関わらず常に偽（config.toml の [agents.*]・argv の
                # -c features.multi_agent=true・AGENTS.md の役割段落が出ない）。
                _multi_agent_enabled = False if _plain else codex_multi_agent_enabled(
                    ollama_base_url=self._ollama_base_url, system_settings=self._system_settings)
                _review_rounds = 0 if _plain else depth_profile_mod.review_rounds_for(
                    (ctx.scope_meta or {}).get("depth_profile"), self._system_settings)
                # S1b（実装ベース探索の回復・深さの1段引き上げ）: 共通上限（管理画面の設定値）に
                # 余地があるターンだけ、本体の自己判断による見直し追加1回を AGENTS.md で許可する
                # （`_review_rounds` 自体は `review_rounds_for` が既に上限で頭打ち済み——上限一杯の
                # ターンでこの余地判定が偽になり、引き上げの許可を出さない）。
                _review_rounds_escalation = (not _plain) and _review_rounds < depth_profile_mod.effective_max_review_rounds(
                    self._system_settings)
                # 原本直読の read root と秘匿 deny の列挙は、プロンプトの文言（direct_read フラグ）と
                # permission profile（_write_codex_authoring_config・後段）の両方が使うため、
                # プロンプト組立の前に1回だけ計算する（提案書 2026-09-10-Codex原本直読と調査スキル）。列挙失敗（RuntimeError＝fail-closed）時は両方とも
                # 「直読不可」に揃える——プロンプトだけ楽観的、profile だけ悲観的、という食い違いを防ぐ。
                _base_roots = _direct_read_roots(ctx.world)
                _direct_roots, _deny_roots = _base_roots, []
                _venv_for_deny = _venv_root()
                try:
                    _scope_deny = _scope_deny_entries(_base_roots, sp)
                    if _base_roots and all(r in _scope_deny for r in _base_roots):
                        raise RuntimeError("scope_enum_failed:no_scope_in_roots")   # 範囲がどの root にも無い
                    _sensitive_deny = _scope_deny + _enumerate_sensitive(
                        _base_roots + ([str(_venv_for_deny)] if _venv_for_deny else []))
                    _direct_read_ok = True
                except RuntimeError as e:
                    _direct_roots, _deny_roots, _sensitive_deny, _direct_read_ok = [], _base_roots, [], False
                    _log.warning("codex direct read disabled: %s", e)
                if not _direct_read_ok and (not mcp or _plain):
                    # MCP 無効の構成と素の Codex（plain）は直接参照だけが資料を読む手段＝直読を許可しないと
                    # 何も調べられない。
                    # 黙って空振りの回答を返さず、実行前に正直に失敗する（利用者向け文言は専門用語ゼロ）。
                    msg = "この資料フォルダは今回読み取りの準備ができませんでした。管理者に確認を依頼してください。"
                    yield _node("codex", "think", "Codex が調べる", "資料の読み取り準備に失敗", "done")
                    yield {"type": "answer_delta", "text": msg}
                    env = {"lens": decision["lens"], "headline": msg, "summary": {"total": 0},
                          "data": {}, "sources": [],
                          "agentic_failure": "error",   # 実行していないターン＝完了として数えない
                          "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world,
                                                              lens=decision["lens"])}
                    yield {"type": "_result", "env": env,
                          "decision": {"lens": decision["lens"], "input": ctx.message,
                                       "reason": ("素の Codex で" if _plain else "MCP 無効の構成で")
                                                 + "直読の準備（秘匿ファイル列挙／範囲）に失敗"}}
                    return
                # MCP でも FS でも同じプロンプト組み立て（personal_facts を注入）。
                if mcp or _plain:
                    _codex_msg = ctx.message
                    if ctx.personal_facts:
                        _codex_msg = (f"{ctx.message}\n\n"
                                      f"【個人ファイル内ヒット（本人のみ・共有不可）】\n{ctx.personal_facts}")
                    if _plain:
                        prompt = self._prompt_plain(_codex_msg, decision["lens"], ctx.world, mcp=mcp)
                    else:
                        prompt = self._prompt_mcp(_codex_msg, decision["lens"], ctx.world,
                                                  direct_read=_direct_read_ok, layer=_layer)
                else:
                    prompt = self._prompt(ctx.message, decision["lens"], env, ctx.world)
                # §3: --ephemeral（セッションをディスクに残さない）と -o（最終メッセージのファイル
                # 出力＝JSON 抽出が空だった時の保険）は sandbox/fallback どちらでも共通。.tmp/ は既存の
                # run_dir 新規ファイル走査（台帳登録スキャン）から除外済みのディレクトリ（既存の挙動を流用）。
                # 正典 §3.4「範囲と同じ硬いフィルタ」: run_dir は実行ごとの新規作成（`mkdir(exist_ok=False)`）
                # のため前ターンの残存はあり得ないが、symlink にすり替わっていた場合は rmtree が
                # 例外を送出する＝fail-closed のまま残す（多層防御）。
                _tmp = run_dir / ".tmp"
                if _tmp.exists() or _tmp.is_symlink():
                    shutil.rmtree(_tmp)
                _tmp.mkdir(parents=True, exist_ok=True)
                # 調査台帳（正典§3「置き場所」）: `.tmp` と同じく成果物登録スキャンの対象外
                # （`.tmp` 自体が既に除外済み）。空の `items/` を毎 run 用意する。
                _investigation_dir = _tmp / "investigation"
                (_investigation_dir / "items").mkdir(parents=True, exist_ok=True)
                # 前ターン退避（正典§4「寿命」・会話 id が無い非永続ターンは何もしない）: 永続台帳
                # ディレクトリ（`workspace/.codex-sessions/{cid}`）に未完了台帳が残っていれば、
                # 「続き」宣言のときだけ復元し、それ以外は消す（前ターンの作業台帳を無断で
                # 持ち越さない）。`_persist_session` 単独ではなく `_session_persistence_enabled`
                # （conversation_id あり **かつ** サンドボックス有効）で判定する——
                # `SHERPA_CODEX_SANDBOX=0`（緊急避難経路）は常に `--ephemeral`＝resume 不能で
                # `.codex-sessions/{cid}` 自体を作らない契約（`codex_home` の判定と同じ理由・
                # `test_codex_resume.py::test_fallback_sandbox_disabled_does_not_persist_ephemeral_session_id`）。
                # 素の Codex（plain）は台帳を使わない＝standard が「続き」のために退避した台帳を
                # 復元も削除も退避もしない（mode を戻して「続き」を送れば standard で再開できる）。
                if _session_persistence_enabled and not _plain:
                    _ledger_home = _safe_codex_sessions_home(users_dir, uid, ctx.conversation_id)
                if _ledger_home is not None:
                    _retired_investigation = _ledger_home / "investigation"
                    if _retired_investigation.is_dir():
                        # RV 高-1（2026-09-22 6巡目是正）: 退避先に symlink が1つでもあれば
                        # 復元せず削除する（「続き」宣言かどうかに関わらず・model が仕込んだ
                        # symlink を退避経由で次ターンへ持ち越さない多層防御の片側）。
                        if _investigation_tree_has_symlink(_retired_investigation):
                            _log.warning(
                                "investigation ledger restore skipped: symlink detected in retired dir")
                            _remove_dir_best_effort(_retired_investigation)
                        elif not mcp:
                            # 台帳は MCP の台帳ツールでしか更新できない——MCP 無効の run では復元も
                            # 削除もせず退避先をそのまま残す（復元すると更新手段の無い旧台帳が
                            # claims の突合だけに使われ、新しい根拠の主張が降格される）。
                            _log.info("investigation ledger restore skipped: mcp disabled")
                        elif (ctx.message or "").strip().startswith(("続き", "つづき", "続けて")):
                            if _restore_investigation_ledger(_retired_investigation, _investigation_dir, _tmp):
                                _investigation_restored = True
                            else:
                                # RV 中-1（10巡目是正）: 復元が途中で失敗したターンでは、この run
                                # の退避（削除・置換）を一切行わない——元の退避台帳を保持し、次
                                # ターンの「続き」でもう一度復元を試せるようにする。
                                _investigation_restore_failed = True
                                _investigation_retire_done = True
                        else:
                            _remove_dir_best_effort(_retired_investigation)
                _last_message_path = _tmp / f"last-message-{hashlib.sha1(os.urandom(8)).hexdigest()[:12]}.txt"
                # MCP ツール結果の予算・grep ヒット上限・読み取り窓を親側で1回だけ解決し、子プロセスの
                # env として渡す（`mcp_server.py::_env_budget_bytes` が最優先で読む・§3.4 の
                # `effective_tool_result_max_bytes`/`_max_total_bytes` の呼び出し方を踏襲）。
                _mcp_budget_env: dict[str, str]
                _mcp_budget_env = _resolve_mcp_budget(
                    self._system_settings, self.model, self._ollama_base_url,
                    None if _plain else (ctx.scope_meta or {}).get("depth_profile")) if mcp else {}
                if mcp and _plain:
                    # 素の Codex モード（§3）: MCP サーバへツールの絞り込みを渡す
                    # （`SHERPA_MCP_LEDGER_REQUIRED_EXTRA` と同じ受け渡しの仕組み）。台帳の
                    # ディレクトリ・required_extra は渡さない（台帳ツール自体を出さないため）。
                    _mcp_budget_env["SHERPA_MCP_TOOLSET"] = "plain"
                elif mcp:
                    # 解決済み（realpath）のパスを渡す——MCP サーバ側は「書込先の経路に symlink が無い」
                    # ことを書込のたびに検査するため、配置由来の symlink をここで畳んでおく。
                    _mcp_budget_env["SHERPA_MCP_LEDGER_DIR"] = os.path.realpath(_investigation_dir)
                    # `ledger_status`（mcp_server.py）が自己確認する完了判定も、provider 側のゲートと
                    # 同じ required_extra を見られるようにする——渡さないと Codex は自己確認で
                    # complete:true を見て final を出し、provider のゲートに差し戻される往復が
                    # 無駄に1回増える。ターンの最初に決めた値をそのまま渡す（カンマ区切り・空なら
                    # 空文字列＝mcp_server.py 側は未設定と同じ扱いにする）。
                    _mcp_budget_env["SHERPA_MCP_LEDGER_REQUIRED_EXTRA"] = ",".join(_ledger_required_extra)
                codex_home = None
                # サイドカーの置き場（`_sidecar_path`）はどちらの経路でも「本サーバ側は書けるが
                # model-shell からは書込許可外」の場所に決める——両分岐の中で確定させ、決定ロジックを
                # 一本化する（`_absorb_mcp_sidecar` のガードは `codex_home` の有無ではなくこの変数と
                # `_sidecar_init_ok` を見る）。既定は非サンドボックス経路の置き場（下でサンドボックス
                # 有効時のみ codex_home 配下へ差し替える）。
                _sidecar_path = _tmp / _MCP_SIDECAR_NAME
                if _codex_sandbox_enabled():
                    # 検証済 recipe: permission profile で読取を KB(RO)＋authoring(RW) に封じ込め＋env 洗浄。
                    # CODEX_HOME は authoring の外（workspace 直下・`:root=deny` で shell から不可視）。
                    # conversation_id があるターンは会話ごとの固定ディレクトリ
                    # （`workspace/.codex-sessions/{cid}`）を CODEX_HOME にして毎ターン再利用する
                    # （`sessions/` 配下の JSONL が resume の実体＝下の finally では削除しない）。
                    # 無い場合（conversation_id 無しの直接呼出し・既存テスト等）は従来どおり per-request
                    # 使い捨て（実行後 rmtree・`--ephemeral`）のまま無改修。
                    # `_safe_persistent_codex_home` は外側で既に symlink/workspace外
                    # 逸脱を検証済み（ここで再計算しない＝検証と使用の間で別パスを組み立てて TOCTOU を
                    # 生まない）。ここに来ている時点で `_session_persistence_enabled` かつ `_codex_home_ok`
                    # （＝`_safe_persistent_codex_home is not None`）は保証済み。
                    if _session_persistence_enabled:
                        codex_home = _safe_persistent_codex_home
                    else:
                        _rand = hashlib.sha1(os.urandom(8)).hexdigest()[:12]
                        codex_home = users_dir / uid / "workspace" / f".codexhome-{_rand}"
                    # codex_home（permission profile 上 `:root deny`＝model-shell から不可視・MCP サーバは
                    # profile 外の別プロセスなので書ける）配下に置く——run_dir 直下だと Codex の shell
                    # ツールが偽の `{"kind":"read",...}`/`{"kind":"ask_user",...}` 行を追記でき、未読資料を
                    # 根拠ゲート（sources_verified）へ通したり任意文面の確認カードでターンを潰せてしまう。
                    _sidecar_path = codex_home / _MCP_SIDECAR_NAME
                    argv_base = ["codex", "exec", "--json", "--strict-config", "--skip-git-repo-check",
                                "-o", str(_last_message_path),
                                "-C", str(run_dir), "-m", self.model,
                                "-c", f"model_reasoning_effort={_reason}"]
                    # 管理者環境の既定値に依存させない明示指定（提案書 §2.6・CLI 0.153.4 は
                    # 既定 enabled だが将来/別環境の既定変更に頼らない）。無効時（`codex_worker_model`
                    # 未設定の独自エンドポイント等）も明示的に false にする——`[agents.*]` の層を
                    # 書かない構成で true のまま CLI 既定に委ねると、worker/evaluator の解決先が
                    # 無いまま機能だけが有効という食い違いが起きる。
                    argv_base += ["-c", f"features.multi_agent={'true' if _multi_agent_enabled else 'false'}"]
                    if not _session_persistence_enabled:
                        argv_base.append("--ephemeral")
                    # `self._openai_api_key` は Codex(OpenAI) 構成で接続先が Azure 等の時だけ
                    # `_select_provider` が解決して渡す（それ以外は常に None＝在来どおり env に渡さない）。
                    popen_env = _codex_clean_env(codex_home, run_dir, _tmp,
                                                 openai_api_key=self._openai_api_key)
                else:
                    # フォールバック（SHERPA_CODEX_SANDBOX=0）＝旧 `-s workspace-write`（読取全開・多層防御は OS ユーザ分離に依存）。
                    # この緊急避難経路は対象外＝resume 非対応のまま（既存どおり常に使い捨て）。
                    # `_session_persistence_enabled` は既に False（サンドボックス無効
                    # なので）＝ここで捕捉する thread_id は env に載らない（下の env 組立部分を参照）。
                    resume_sid = None
                    argv_base = ["codex", "exec", "--json", "--skip-git-repo-check",
                                "--ephemeral", "-o", str(_last_message_path),
                                "-s", "workspace-write", "-C", str(run_dir),
                                "-m", self.model, "-c", f"model_reasoning_effort={_reason}"]
                    # このフォールバック経路は常にサンドボックス無効
                    # （`codex_multi_agent_enabled` が常に False を返す＝`_multi_agent_enabled` は
                    # ここで常に偽）——config.toml 自体を書かない経路のため `[agents.*]` の層が無く、
                    # 管理者環境の既定値に依存させず明示的に無効化する（有効なフリだけして spawn が
                    # 毎ターン失敗するのを防ぐ）。
                    argv_base += ["-c", "features.multi_agent=false"]
                    # §5-1: --strict-config が無い経路（config.toml でなく -c）なので同等をここで足す。
                    argv_base += _web_search_c_args(self._web_search, self._system_settings)
                    if mcp:
                        # `.tmp/`（run_dir 配下）はこの経路（`-s workspace-write`＝読取全開・多層防御は
                        # OS ユーザ分離のみ）では model-shell からも見える——封じ込めが無い前提の経路
                        # のため、sandbox 有効時の codex_home 配下と同じ「shell から不可視」は成立しない。
                        # `_sidecar_append`（mcp_server.py）が書く内容は元々 doc_id／ツール名／種別／
                        # 時刻（と ask_user の質問）だけで本文は書かない契約だが、この経路では shell が
                        # 同じファイルへ偽の行を追記できる可能性も封じ込められない——run_dir 直下に
                        # 置いていた従来の実害無し（誰も書かなかった）から、露出面が変わる点として
                        # 明示しておく。
                        _sidecar_env = {"SHERPA_MCP_SIDECAR": str(_sidecar_path)}
                        argv_base += _mcp_config_args(ctx.world, sp, _ask_disabled, layer=_layer,
                                                      extra_env={**_mcp_budget_env, **_sidecar_env})
                        popen_env = {**os.environ, **_mcp_env(ctx.world, sp, _ask_disabled, layer=_layer),
                                    **_mcp_budget_env, **_sidecar_env}
                        # `.tmp/` は run_dir 生成のたびに空から始まる（rmtree+mkdir 済み・上記）ため、
                        # sandbox 経路の config.toml のような前ターン残骸の事前 unlink は不要——
                        # ここで吸収を即座に許可してよい。
                        _sidecar_init_ok = True
                    else:
                        popen_env = None
                # 出力スキーマ（§2-1）: OpenAI 系構成のみ（Codex(Ollama) は未確認のため対象外）・
                # 退避口 env `SHERPA_CODEX_OUTPUT_SCHEMA=0` で無効化・`=1` で v1 に戻せる。
                # DEPTH-2 S1（§2.5）→ 初期構成の既定（決定2026-09-19）: 既定は v2
                # （`claims` 付き）——気づかないと効かない既定は初期構成で ON にする方針のため、
                # 1（v1）は逃げ道として残すだけで既定にはしない。plain（素の Codex モード）では
                # env の値に関わらず常に 0（平文の回答・既存の非スキーマ経路 `_pick_codex_headline`
                # を使う）——台帳ゲート（`_schema_v2 and mcp and ...`）・claims の格下げ判定は
                # `_schema_v2` が常に偽になることで自然に無効化される。
                _schema_level = 0 if _plain else _env_int("SHERPA_CODEX_OUTPUT_SCHEMA", 2, 0, 2)
                _schema_on = self._ollama_base_url is None and _schema_level >= 1
                _schema_v2 = _schema_on and _schema_level == 2
                if _schema_on:
                    argv_base += ["--output-schema",
                                 str(_OUTPUT_SCHEMA_PATH_V2 if _schema_v2 else _OUTPUT_SCHEMA_PATH)]
                _codex_run_started_at = time.monotonic()
                # 子 rollout の mtime 下限（壁時計・resume は thread id を使い回すため、
                # `_collect_child_token_usage` の新形式判定はこれより前に書かれたファイルを
                # 前ターンの子として除外する）。
                _turn_started_wall = time.time()
                _codex_config_kind = "ollama" if self._ollama_base_url is not None \
                    else _openai_endpoint_kind(self._system_settings)
                _depth_label = (ctx.scope_meta or {}).get("depth_profile") or "standard"
                # 予算/上限の各値は `_mcp_budget_env`（`_resolve_mcp_budget` が1回だけ解決した実効値）
                # からそのまま読む（ログ用に別計算しない＝食い違いを作らない）。mcp 無効時は env が
                # 空のため各キーとも "-"（該当なし）になる。`budget_total`／`max_calls` は Codex 経路
                # では撤去済み（`_resolve_mcp_budget` は該当キーを env に載せない）——行の形（キー名）
                # は統計側が読むため変えず、値は常に "none" を出す。`window_source`/`window_cli` も
                # 同じく行の形だけ残し常に "none"（窓は Codex CLI 任せ＝Sherpa は渡さない）。
                _codex_mode_label = "plain" if _plain else "standard"
                _log_codex.info(
                    "start conv=%s uid=%s mode=%s config=%s multi_agent=%s depth=%s review_rounds=%s "
                    "schema_level=%s model=%s reasoning=%s budget_per_result=%s budget_total=%s "
                    "max_hits=%s window_cap=%s max_calls=%s window_source=%s window_cli=%s",
                    ctx.conversation_id, uid, _codex_mode_label, _codex_config_kind, _multi_agent_enabled,
                    _depth_label, _review_rounds, _schema_level, self.model, _reason,
                    _mcp_budget_env.get("SHERPA_MCP_TOOL_BUDGET_BYTES", "-"),
                    "none",
                    _mcp_budget_env.get("SHERPA_MCP_TOOL_MAX_HITS", "-"),
                    _mcp_budget_env.get("SHERPA_MCP_TOOL_WINDOW_CAP", "-"),
                    "none",
                    "none",
                    "none")
                # 利用統計 activity.settings（正典§3.1）: 上の開始行と同じ実行時解決済み値を
                # そのまま使う（食い違いを作らない・"-" は未解決＝取れない値として None にする＝
                # 推定で埋めない）。
                _activity_settings = {
                    "provider": "codex", "mode": _codex_mode_label, "config": _codex_config_kind,
                    "model": self.model,
                    "reasoning": _reason, "depth": _depth_label, "review_rounds": _review_rounds,
                    "schema_level": _schema_level, "multi_agent": _multi_agent_enabled,
                    "budget_per_result": _int_or_none(_mcp_budget_env.get("SHERPA_MCP_TOOL_BUDGET_BYTES")),
                    "max_hits": _int_or_none(_mcp_budget_env.get("SHERPA_MCP_TOOL_MAX_HITS")),
                    "window_cap": _int_or_none(_mcp_budget_env.get("SHERPA_MCP_TOOL_WINDOW_CAP")),
                }
                # prepare/agent（正典§3.1・`ctx.turn_started_mono`/`_agent_start_mono`/
                # `_agent_end_mono` から）は最初の Popen・最後の wait が確定してから finally
                # ブロックでまとめて計算する（ここではまだ `_agent_start_mono` が定まっていない）。

                def _build_argv(use_resume: bool, prompt_text: str | None = None) -> list:
                    """resume 分岐は `codex exec resume [SESSION_ID] [PROMPT]` の位置引数どおり、
                    exec 共通オプションの後・末尾プロンプトの前に `resume <sid>` を挿む。resume 先 id は
                    `thread_id`（`thread.started` で捕捉した最新値）を優先し、未捕捉なら呼び出し時点の
                    `resume_sid` に落ちる（自動継続はフレッシュ実行で捕捉した thread_id で resume する）。
                    `prompt_text` 省略時は通常の質問プロンプト（`prompt`）を使う（継続 attempt だけ別文言）。"""
                    av = list(argv_base)
                    if use_resume:
                        sid = thread_id or resume_sid
                        if sid:
                            av += ["resume", sid]
                    av.append(prompt if prompt_text is None else prompt_text)
                    return av

                got_any_line = False   # resume 試行で1行も --json イベントを受け取れなければ resume 失敗とみなす
                attempt_returncode = None   # fallback 判定の将来耐性（下の呼出側コメント参照）
                # 自動継続がツール未実行のまま宣言だけを繰り返す（正常な手順説明相手に無駄打ちする）のを
                # 打ち切るための per-attempt フラグ（attempt 開始ごとに False へ戻す）。
                _attempt_ran_tools = False
                # item id は codex exec プロセスごとに振り直される（`item_0` 等）ため、継続 attempt が
                # 初回 attempt と同じ id を使うとノードを上書きし、保存ログから前回分の履歴が消える。
                # 2回目以降の attempt でだけ node id に付ける接頭辞の元になる連番（初回=1・以降 _attempt
                # 呼び出しごとに +1）。
                _attempt_no = 0
                # `_needs_continuation`／`codex_stopped_early` の判定を「最新 attempt の message だけ」
                # に絞るための境界（`_agent_msgs` へのこの attempt 開始時点の長さ）。`_pick_codex_headline`
                # は従来どおり `_agent_msgs` 全件を見る（headline の選び方は変えない）。
                _attempt_msgs_start = 0
                # attempt 開始時に消せなかった前 attempt の `-o` 本文（吸収で読み飛ばす対象・無ければ None）。
                _stale_last_message = None
                # §2-10: トップレベル `turn.failed`／`error` イベントを見た attempt かどうか（agent_message
                # が1つも無いこの種の失敗は、既存の「stdout に JSON が1行も無い」判定では拾えない）。
                # 診断コード（`error.code`。無ければ None）だけ控える——本文はログにも利用者向け文言にも貼らない。
                _turn_failed = False
                _turn_failed_code = None
                # `error.message`（無ければトップレベル `message`）を伏せ字＋先頭300文字だけ控える
                # （利用者向け本文には出さない・warning ログと `env["codex_error_code"]` の付随情報専用）。

                def _attempt(use_resume: bool, prompt_text: str | None = None):
                    """1回分の codex exec 実行（node/answer_delta を yield）。proc はこの1回限りの
                    ローカル状態（呼出側は再試行のたびに新しい Popen を張るだけでよい）。`prompt_text` は
                    自動継続用（省略時は通常プロンプト）。"""
                    nonlocal got_any_line, ran, codex_question, codex_usage, thread_id, attempt_returncode
                    nonlocal _agent_partial, _stream_error, _graph_schema_era_error
                    nonlocal _attempt_ran_tools, _attempt_no, _attempt_msgs_start, _stale_last_message
                    nonlocal _turn_failed, _turn_failed_code
                    nonlocal _agent_start_mono, _agent_end_mono
                    got_any_line = False
                    attempt_returncode = None
                    _attempt_ran_tools = False
                    _turn_failed = False
                    _turn_failed_code = None
                    _attempt_no += 1
                    # 前 attempt の未完 message（item.updated だけで completed が来なかった分）は履歴へ
                    # 退避してから境界を引く。残したままだと最新 attempt の判定に前 attempt の途中経過が
                    # 混ざり、完成した回答でも継続／`codex_stopped_early` になる。
                    if _agent_partial.strip():
                        _agent_msgs.append(_agent_partial)
                    _agent_partial = ""
                    _attempt_msgs_start = len(_agent_msgs)
                    # `-o` は attempt をまたいで同じパス。前 attempt の内容を残すと、この attempt が
                    # 何も書かずに終わったとき古い文を最新 attempt の回答として吸収してしまう。消せない
                    # ときは残った本文を控え、終了後の吸収でその本文だけは読み飛ばす。
                    _stale_last_message = None
                    try:
                        _last_message_path.unlink(missing_ok=True)
                    except OSError as exc:
                        _stale_last_message = _read_last_message_fallback(_last_message_path)
                        _log.warning("codex last-message cleanup failed: %s errno=%s conv=%s uid=%s",
                                     type(exc).__name__, getattr(exc, "errno", None), ctx.conversation_id, uid)
                    # このプロセス（1回の codex exec）内だけで完結する id 集合（run-level `_mcp_calls`
                    # への合算は finally で行う）。
                    _attempt_mcp_seen: set = set()
                    _mcp_read_done: set = set()      # 収集済み item id（同じ item の再送で二重に数えない・
                                                     # item id は attempt（codex exec プロセス）ごとに振り直される）
                    _attempt_mcp_open: set = set()
                    _attempt_mcp_max_in_flight = 0
                    argv = _build_argv(use_resume, prompt_text)
                    proc = None
                    # finally（reap する側）と監視スレッド（waitid で覗く側）の間で「回収済みか」
                    # を直列化する。回収済みの後は sid が再利用され得るため、回収の前後は
                    # このロックの中でだけ判定・実行する（`_spawn_stop_watcher` 側の docstring も参照）。
                    _reap_lock = threading.Lock()
                    _reaped = {"done": False}
                    try:
                        # Popen 直前の最終防衛線（`_select_provider` の選択時チェックを迂回する経路が
                        # あっても、実際にプロセスを起動する直前でもう一度確認する・多層防御）。
                        # Codex(Ollama) 構成（`self._ollama_base_url` あり）は OpenAI 系 I/O ではないため
                        # 対象外。
                        if self._ollama_base_url is None:
                            from ... import llm
                            llm.assert_openai_io_allowed()
                        # start_new_session で独立プロセスグループにし、停止/後始末で
                        #   MCP subprocess / shell child まで group ごと確実に殺す（creds env の寿命を延ばさない）。
                        # stderr は捨てる（保存もログ出力もしない）——Codex CLI の stderr には
                        # 資料名・本文・環境変数値が混ざり得る一方、伏せ字（`agentic_search._redact`）は
                        # 秘密の既知パターンしか落とせない。失敗の原因は `--json` の `codex_error_info`
                        # （固定語彙）と Codex CLI 自身の rollout JSONL に残る。
                        if _agent_start_mono is None:   # 利用統計 activity: 最初の Popen だけを起点にする
                            _agent_start_mono = time.monotonic()
                        proc = subprocess.Popen(
                            argv, env=popen_env, cwd=str(run_dir), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            start_new_session=True)
                        # 途中停止の有無によらず常に起動する（別グループの子が pipe を握って
                        # 離さないケースの pipe 閉じ役も兼ねる・_spawn_stop_watcher 参照）。
                        _spawn_stop_watcher(proc, ctx.stop_event, _reap_lock, _reaped)
                        node_n = 0
                        for line in proc.stdout:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                e = json.loads(line)
                            except ValueError:
                                continue
                            got_any_line = True
                            _et = e.get("type")
                            if isinstance(_et, str):
                                _event_type_counts[_et] = _event_type_counts.get(_et, 0) + 1
                            if e.get("type") == "thread.started":       # session/thread id 捕捉（resume 先の id）
                                thread_id = e.get("thread_id") or thread_id
                                continue
                            if e.get("type") == "turn.completed":            # ターンのトークン使用量（item ではない）
                                _u = _usage_from_turn_completed(
                                    e, self.model,
                                    codex_model_provider="ollama" if self._ollama_base_url is not None else "openai",
                                    system_settings=self._system_settings)
                                # 自動継続の attempt をまたいで合算する（単発 attempt のみの run では
                                # 従来どおり最初で唯一の値がそのまま codex_usage になる）。
                                codex_usage = _accumulate_codex_usage(codex_usage, _u)
                                continue
                            if e.get("type") in ("turn.failed", "error"):        # §2-10: 失敗終了の明示
                                _turn_failed = True
                                if _turn_failed_code is None:
                                    _err = e.get("error")
                                    _err_dict = _err if isinstance(_err, dict) else {}
                                    # `codex_error_info`（`error` dict 内・トップレベルのどちらに
                                    # 出ても拾う）は `error.code`（無ければ閉じていない）より粒度が
                                    # 細かい診断コード（例 "context_window_exceeded"）——取れたら優先する。
                                    _info = _err_dict.get("codex_error_info") or e.get("codex_error_info")
                                    _turn_failed_code = (
                                        _info if isinstance(_info, str) and _info
                                        else (_err_dict.get("code") or e.get("code")))
                                    if not _turn_failed_code:
                                        # `codex_error_info` を持たない CLI 版のための救済——本文は
                                        # 保存もログ出力もせず、固定語彙へ**分類するためだけ**に読む
                                        # （message には資料名・抜粋が混ざり得る）。
                                        _turn_failed_code = _classify_turn_failure(
                                            _err_dict.get("message") or e.get("message"))
                                    _log.warning("codex turn failed: code=%s conv=%s uid=%s",
                                                 _turn_failed_code, ctx.conversation_id, uid)
                                continue
                            item = e.get("item") or {}
                            it = item.get("type")
                            iid = item.get("id")
                            if not iid:                                      # id 無し item でも node を上書き衝突させない
                                iid = f"cx-auto-{node_n}"
                                node_n += 1
                            if _attempt_no > 1:                               # 2回目以降の attempt は id 空間を分離（前 attempt のノードを上書きしない）
                                iid = f"a{_attempt_no}-{iid}"
                            if it in ("web_search", "file_change"):     # ネイティブ Web 検索／ファイル変更もツール実行（継続打ち切り判定用・表示ノードは追加しない）
                                _attempt_ran_tools = True
                            if it == "command_execution":                       # Codex 自身の grep/参照を逐次表示
                                ran = True
                                _attempt_ran_tools = True
                                label, detail = _humanize_cmd(item.get("command", ""))
                                if item.get("status") == "completed" or e.get("type") == "item.completed":
                                    ec = item.get("exit_code")
                                    yield _node(f"cx-{iid}", "tool", label,
                                                detail + (f"  → exit {ec}" if ec is not None else ""), "done")
                                else:
                                    yield _node(f"cx-{iid}", "tool", label, detail, "active")
                            elif it == "mcp_tool_call":                          # Codex の MCP ツール呼びを可視化＋近傍を収集
                                ran = True
                                _attempt_ran_tools = True
                                tool = item.get("tool", "")
                                a = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}  # 非 dict 引数で落とさない
                                done = e.get("type") == "item.completed" or item.get("status") in ("completed", "failed")
                                # 並走計測（このプロセス内のみ・run 全体への合算は _attempt の finally）。
                                # id が無い item は開始/完了を対応付けられないため対象外。初見かつ未完了の
                                # ときだけ in-flight に加える（初見でいきなり完了した item は total には
                                # 数えるが in-flight 幅には寄与しない）。2回目以降の見た目（例: item.updated
                                # の再送）は seen 済みなので無視され、二重に数えない。
                                _mcp_id = item.get("id")
                                # read 系ツールの引数から実際に読んだ資料の doc_id を集める（「参照した
                                # 資料:」の記載漏れの補完）。**読取が成功して完了した** item だけ（失敗・
                                # エラー結果・進行中は読めていない＝出典にも「根拠」にも載せない）。
                                _read_ok = (e.get("type") == "item.completed"
                                            and item.get("status") not in ("failed", "error")
                                            and not (isinstance(item.get("result"), dict) and item["result"].get("isError"))
                                            and not item.get("error"))
                                if _read_ok and (not _mcp_id or _mcp_id not in _mcp_read_done):
                                    if _mcp_id:
                                        _mcp_read_done.add(_mcp_id)
                                    # 原本読取ツール（xlsx_range/docx_paragraphs/
                                    # pptx_slides/pdf_pages/file_head）も doc_id 引数を取る読取
                                    # ツール——これらを収集対象に含めないと、Codex が MCP 経由で
                                    # 原本を直接読んでも出典収集から漏れる。
                                    # `xlsx_sheets` はシート一覧を返すだけで本文を
                                    # 読んでいない——本文精読ツールと同じ扱いにすると「シート一覧を
                                    # 見ただけ」で `sources_verified`（根拠ゲート）へ数えられて
                                    # しまうため、`_mcp_listed_docs`（sources には合流するが
                                    # sources_verified には数えない）へ分ける。
                                    if tool in READ_DOC_TOOLS:
                                        _d = a.get("doc_id")
                                        if isinstance(_d, str) and _d:
                                            _mcp_read_docs.append(_d)
                                    elif tool in LISTED_DOC_TOOLS:
                                        _d = a.get("doc_id")
                                        if isinstance(_d, str) and _d:
                                            _mcp_listed_docs.append(_d)
                                    elif tool == "compare_documents":
                                        for _k in COMPARE_DOC_ID_ARGS:
                                            _d = a.get(_k)
                                            if isinstance(_d, str) and _d:
                                                _mcp_read_docs.append(_d)
                                if _mcp_id and _mcp_id not in _attempt_mcp_seen:
                                    _attempt_mcp_seen.add(_mcp_id)
                                    if not done:
                                        _attempt_mcp_open.add(_mcp_id)
                                        _attempt_mcp_max_in_flight = max(
                                            _attempt_mcp_max_in_flight, len(_attempt_mcp_open))
                                elif _mcp_id and done:
                                    _attempt_mcp_open.discard(_mcp_id)
                                if tool == "ask_user":
                                    # ask_user は question 優先（agentic の {"question":..}→return と同じ意味論）。
                                    # ガード②確認ID 付き再送では無視／③1実行1回（codex_question is None で enforce）。
                                    # 質問を捕まえたらループを抜け、finally で proc を後始末してから emit → ターン終了する。
                                    if codex_question is None:
                                        codex_question = _codex_ask_capture(item, _ask_disabled)
                                    # 捕捉して break する場合は item.completed を待たずに
                                    # ループを抜けるため、実際の done フラグに関わらずノードを "done" で確定表示する
                                    # （さもないと「ユーザに確認」が実行中表示のまま履歴保存される）。
                                    node_done = done or (codex_question is not None)
                                    yield _node(f"cx-{iid}", "tool", "ユーザに確認",
                                                f"「{str(a.get('prompt') or '確認が必要です')[:60]}」",
                                                "done" if node_done else "active")
                                    if codex_question is not None:
                                        break
                                    continue
                                # folder_tree/compare_documents は MCP 経由で
                                # Codex にも公開済み（`mcp_server.py::_tool_defs`）のため、この表示用ラベル
                                # 辞書にも対応を持たせる（`improvement_log._TOOL_CALL_LABELS` の集計対象でもある）。
                                tlabel = {"graph_neighbors": "関係グラフをたどる", "ripgrep_search": "資料を検索（語句そのまま）",
                                          "es_search": "資料を検索（全文）", "read_around": "該当箇所を精読",
                                          "list_docs": "資料の一覧を確認", "folder_tree": "フォルダ構成を確認",
                                          "compare_documents": "世代間の差分を比較",
                                          "read_doc": "文書を通読", "doc_outline": "見出し構造を確認",
                                          "glob_search": "ファイル名で検索",
                                          # agentic_search
                                          # の `_ORIGINAL_READ_LABELS`／改善ログの `_TOOL_CALL_LABELS` と
                                          # 同じ文言（`xlsx_sheets` はシート一覧のみ＝本文精読の
                                          # `xlsx_range` とは別ラベル・`_FILES_READ_LABEL` からも外れる）。
                                          "xlsx_sheets": "原本のシート一覧を確認", "xlsx_range": "原本を読む（Excel）",
                                          "docx_paragraphs": "原本を読む（Word）",
                                          "pptx_slides": "原本を読む（PowerPoint）",
                                          "pdf_pages": "原本を読む（PDF）",
                                          "file_head": "原本を読む（先頭）",
                                          # 調査台帳の MCP ツール（`mcp_server._LEDGER_TOOLS`）。引数の
                                          # 本文は出さず、件数・状態語彙（閉集合）だけを表示する。
                                          "ledger_manifest_set": "調査台帳に項目を登録",
                                          "ledger_item_put": "調査台帳の項目を更新",
                                          "ledger_status": "調査台帳の状態を確認"}.get(tool, "その他の処理")
                                if tool in _LEDGER_TOOL_DETAILS:
                                    detail = _LEDGER_TOOL_DETAILS[tool](a)
                                else:
                                    detail = "「" + str(a.get("name") or a.get("query") or a.get("doc_id")
                                                        or a.get("path_prefix") or a.get("name_pattern")
                                                        or a.get("pattern") or "") + "」"
                                if done and tool == "graph_neighbors" and item.get("status") == "completed":
                                    # 旧世代グラフの構造化エラー
                                    # （`mcp_server.py::handle` が isError で返す）を先に見る——
                                    # 検知したら `_mcp_neighbors_from` は呼ばない（近傍データではない）。
                                    _era_err = _graph_schema_era_from_item(
                                        item, ctx.world, decision.get("lens") if decision else None)
                                    if _era_err is not None:
                                        _graph_schema_era_error = _era_err
                                    else:
                                        mcp_neighbors.extend(_mcp_neighbors_from(item))
                                yield _node(f"cx-{iid}", "tool", tlabel, detail, "done" if done else "active")
                            elif (it == "collab_tool_call" and e.get("type") == "item.completed"
                                  and item.get("tool") == "spawn_agent"):
                                # DEPTH-2 S3b/S6: 子スレッド id の捕捉のみ（表示ノードは追加しない・
                                # 巡ごとの右ペイン表示は対象外＝§2.8「Codex は巡境界イベントが無い」
                                # ため用意していない）。multi_agent 無効ではこのイベント自体が
                                # 出ないため素通りする。
                                for _tid in item.get("receiver_thread_ids") or []:
                                    if isinstance(_tid, str) and _tid:
                                        _child_thread_ids.add(_tid)
                            elif it == "reasoning" and e.get("type") == "item.completed":
                                txt = (item.get("text") or "").strip().splitlines()
                                if txt:
                                    yield _node(f"cx-{iid}", "think", "考える", txt[-1][:80], "done")
                            elif it == "agent_message" and e.get("type") in ("item.completed", "item.updated"):
                                # 最後の1件で上書きせず集める（完了分はリストへ・未完分は partial に保持）。
                                # 結論の選択は loop 後に `_pick_codex_headline` で決定的に行う。
                                _txt = (item.get("text") or "").strip()
                                if e.get("type") == "item.completed":
                                    if _txt:
                                        _agent_msgs.append(_txt)
                                    _agent_partial = ""
                                else:                                    # item.updated＝成長中の未完 message（打ち切り保険）
                                    _agent_partial = _txt
                    except Exception:
                        _stream_error = True
                    finally:
                        # このプロセスで観測した分だけ run-level へ合算する（例外で打ち切られても、
                        # それまでに実際に見えていた分は計測に残す）。
                        _mcp_calls["total"] += len(_attempt_mcp_seen)
                        _mcp_calls["max_in_flight"] = max(_mcp_calls["max_in_flight"], _attempt_mcp_max_in_flight)
                        if proc:
                            try:
                                _killpg(proc)                        # group ごと（MCP child 含む）確実に後始末
                            except Exception:
                                pass
                            # proc（このセッションのリーダー）を wait() で回収する前に呼ぶ:
                            # 回収するまで pid はゾンビとしてカーネルに予約され続け、同じ番号で
                            # 別セッションが新規に作られることは無い（＝sid の再利用が起きない）。
                            # 先に回収すると、その隙に pid が無関係なプロセスへ再利用され、偶然
                            # 同じ番号で新しいセッションが立っていた場合に誤爆しうる。監視スレッドと
                            # 直列化するため _reap_lock の中で行う（上記コメント参照）。
                            with _reap_lock:
                                _kill_session(proc.pid)   # setpgid で group を抜けた孤児を session 単位で回収
                                try:
                                    proc.wait(timeout=5)
                                except Exception:
                                    pass
                                _reaped["done"] = True
                            attempt_returncode = proc.returncode   # RV再検証 LOW-4: fallback 判定の材料（回収後の値）
                            # 利用統計 activity: 直近の wait 完了直後を終端にする（attempt ごとに
                            # 更新＝最後の attempt の終端が残る）。
                            _agent_end_mono = time.monotonic()

                def _absorb_last_message_fallback() -> None:
                    """attempt が `--json` に agent_message を出さず `-o` 最終メッセージファイルにだけ
                    結論を書いたケースを拾う（毎 attempt 終了直後に呼ぶ）。`_last_message_path` は
                    attempt をまたいで同じパスを使い回す（Codex が上書きする）ため、直前に既に
                    `_agent_msgs` へ入っている内容と同一（strip 比較）なら追加しない——正常系では
                    `-o` の内容は最後の agent_message と一致するので二重追加にならない。重複判定は
                    **最新 attempt の分（`_agent_msgs[_attempt_msgs_start:]`）だけ**と比べる: 過去の
                    attempt と同文だからと落とすと、この attempt の結論が判定対象から消える。
                    """
                    _fb = _read_last_message_fallback(_last_message_path)
                    if _fb and _fb == _stale_last_message:
                        return
                    if _fb and _fb.strip() not in {m.strip() for m in _agent_msgs[_attempt_msgs_start:]}:
                        _agent_msgs.append(_fb)

                def _continuation_msgs() -> list[str]:
                    """`_needs_continuation` へ渡す completed message＝最新 attempt の分
                    （`_agent_msgs[_attempt_msgs_start:]`）。ただしその attempt が agent_message を
                    1つも出さず（`_agent_partial` も空＝crash・無出力終了）に終わった場合は新しい
                    情報が無い＝それ以前の蓄積（`_agent_msgs` 全件）で判定する（直前の attempt が
                    作業宣言だけで止まっていたなら、その状態がまだ有効という意味）。
                    """
                    latest = _agent_msgs[_attempt_msgs_start:]
                    return latest if (latest or _agent_partial) else _agent_msgs

                def _update_structured_state() -> None:
                    """§2-3: `_schema_on` のとき、最新 attempt の最終出力（`-o` を第一候補・無ければ
                    最新 attempt の最後の完了 agent_message）を `_parse_structured` で検証し、
                    `_latest_structured` を更新する（継続要否・§2-4 の判定はこの1件だけを見る）。
                    呼び出しは毎 attempt 終了直後（`_absorb_last_message_fallback` と同じ場所）。
                    `_schema_on` が偽なら何もしない（`_latest_structured` は使われない）。

                    見出し候補（`_structured_answers`・§2-5）には、最終候補だけでなく最新 attempt の
                    agent_message 全件を順に検証して合格したものを積む——同一 attempt 内で先に有効な
                    `final`（や `in_progress`）が出ていても、後続の message が壊れた JSON だと最終候補
                    （末尾）だけを見る判定ではその final を拾えず失う。最終候補（`-o` 優先）はこの全件
                    ループに含まれない別ソースのときだけ追加で積む——最後の agent_message と同一テキスト
                    なら、全件ループで既に1回積んでいるため二重に積まない。
                    """
                    nonlocal _latest_structured
                    if not _schema_on:
                        return
                    _parse_fn = _parse_structured_v2 if _schema_v2 else _parse_structured
                    _latest_msgs = _agent_msgs[_attempt_msgs_start:]
                    for _m in _latest_msgs:
                        _parsed = _parse_fn(_m)
                        if _parsed is not None:
                            _structured_answers.append(_parsed)
                    _fb = _read_last_message_fallback(_last_message_path)
                    if _fb and _fb == _stale_last_message:
                        _fb = None   # `_absorb_last_message_fallback` と同じ staleness 規則
                    _last_msg = _latest_msgs[-1] if _latest_msgs else None
                    _final_text = _fb or _last_msg
                    _latest_structured = _parse_fn(_final_text) if _final_text else None
                    if _latest_structured is not None and _final_text != _last_msg:
                        _structured_answers.append(_latest_structured)

                def _continuation_pending() -> bool:
                    """継続要否（§2-4）。`_schema_on` は最新 attempt の構造化出力の status で判定
                    （無し/不正/`in_progress` はすべて未完了）。無効時は現行の平文ヒューリスティックのまま。"""
                    if _schema_on:
                        return _latest_structured is None or _latest_structured["status"] == "in_progress"
                    return _needs_continuation(_continuation_msgs(), _agent_partial)

                def _candidate_final() -> dict | None:
                    """`_structured_answers_valid_from` 以降で最後の `final`（見出し/主張として
                    採用しうる候補）。台帳ゲートは「`_latest_structured` が final かどうか」では
                    なくこの候補の有無で判定する（RV 高-1・2026-09-22 3巡目是正）——同一 attempt が
                    final の後に in_progress を出すと `_latest_structured` は in_progress になるが、
                    その final は `_structured_answers` に残ったままヘッドライン選択の対象になり得る
                    （台帳ゲートが一度も評価しない「未評価の final」が採用されてしまうバグ）。
                    `_pick_structured_headline`/`_pick_structured_claims` もこの関数を使う——
                    採用候補の final は必ずこの1つの選び方を経由させ、ゲートと見出し選択が別々の
                    final を見て食い違う経路を作らない。"""
                    # 回答の後に届いた通知（下調べ役・見直し役の完了＝`<subagent_notification>`）への
                    # 短い返事も `final` で来る——最後の1件を鵜呑みにせず、出典の行（『参照した資料』）
                    # を持つ最後の final、次に主張（`claims`）を持つ最後の final を優先する。
                    _finals = [s for s in _structured_answers[_structured_answers_valid_from:]
                               if s.get("status") == "final"]
                    for _has in (lambda s: "参照した資料" in (s.get("answer") or ""),
                                 lambda s: bool(s.get("claims"))):
                        for s in reversed(_finals):
                            if _has(s):
                                return s
                    return _finals[-1] if _finals else None

                def _pick_structured_headline() -> str | None:
                    """§2-3/5: `_schema_on` の見出し——構造化 message に `final` があれば最後の
                    `final` の `answer`／無ければ最後の構造化 message の `answer`／構造化 message が
                    一度も無ければ、何かしら出力はあった（`_agent_msgs`/`_agent_partial` が非空）ときだけ
                    固定文言（不正な最終出力のまま尽きたケース＝`_codex_stopped_early` が立つ）。
                    出力そのものが無ければ None（無出力失敗の判定へ委ねる・§2-10）。
                    """
                    # `answer` が空の構造化 message は「本文なし」＝固定文言に落とす（空文字を返すと
                    # 後段の `if answer:` から外れて dispatch の決定的見出しが正常回答として出てしまう）。
                    # RV 高-1: `_structured_answers_valid_from` より前（台帳ゲートが拒否した final を
                    # 含む）は候補にしない。台帳ゲートを通った final（`_candidate_final`）を優先する。
                    _empty = "回答を取り出せませんでした。もう一度お試しください。"
                    _cand = _candidate_final()
                    if _cand is not None:
                        return _cand["answer"].strip() or _empty
                    _valid = _structured_answers[_structured_answers_valid_from:]
                    if _valid:
                        return _valid[-1]["answer"].strip() or _empty
                    if _agent_msgs or _agent_partial:
                        return _empty
                    return None

                def _absorb_mcp_sidecar() -> None:
                    """DEPTH-2 S3b是正: 子（`spawn_agent` された worker/evaluator）のサイドカーを
                    **毎 attempt 終了直後**（自動継続の判定より前）に取り込む。ここで読まずに
                    ループの外（このターンの最後）まで待つと、子の `ask_user` があっても
                    親は in_progress のまま自動継続を回してしまう（親自身の item stream には
                    出ない＝`codex_question` が立たないまま次 attempt へ進む）。読んだ doc_id は
                    `_mcp_read_docs`/`_mcp_listed_docs` へ重複なく合流させる（親自身の観測と同じ
                    集合＝二重計上にならない）。

                    ガードは `_sidecar_path`（None でなく決定済みか）と `_sidecar_init_ok`（このターンの
                    初期化が無事終わったか）の2つ——`codex_home` の有無では判定しない（sandbox 有効／
                    無効いずれの経路も `_sidecar_path` を持つため）。sandbox 有効時は事前 unlink・
                    設定生成が両方成功した時だけ、非サンドボックス（`SHERPA_CODEX_SANDBOX=0`）経路は
                    `.tmp/` への env 配線が済んだ時だけ `_sidecar_init_ok` を立てる（provider.py の
                    argv 組立部分参照）。

                    非サンドボックス経路（`codex_home is None`）は run_dir（`.tmp/` 含む）が
                    model-shell から書込全開のため、同じファイルへ偽の行を追記され得る——**この経路では
                    読取記録（read/listed）・確認カード（ask_user）・障害コード（error）を吸収しない**。未読の doc_id が
                    `sources_verified`（根拠として利用者に見せる出典）へ通る・任意文面の確認カードで
                    ターンを潰される、偽の障害コードで誤った案内（「グラフは再取り込み待ち」等）を出される、という実害は
                    統計の欠落より重いため。取り込むのは数値の計数（limit）だけ＝偽装できても件数が狂う
                    だけで、出典・会話の制御・利用者への案内は汚染されない。

                    前ターンの残骸 unlink が失敗した場合や、設定生成自体が失敗して `_attempt` を
                    一度も呼んでいない場合に、finally の無条件呼び出しが前ターンの残骸や存在しない
                    サイドカーを読んでしまうのを `_sidecar_init_ok` が防ぐ。
                    """
                    nonlocal codex_question, _mcp_tool_result_clipped, _mcp_total_budget_hit, \
                        _mcp_duplicate_tool_call, _mcp_search_truncated, _mcp_tool_calls_exhausted
                    if _sidecar_path is None or not _sidecar_init_ok:
                        return
                    _sc_reads, _sc_listed, _sc_ask, _sc_errors, _sc_limits = _read_mcp_sidecar(_sidecar_path)
                    if codex_home is not None:
                        for _c in _sc_errors:
                            if _c not in _mcp_error_codes:
                                _mcp_error_codes.append(_c)
                        # サイドカーが model-shell から不可視（permission profile の `:root deny` 配下）
                        # のときだけ、出典・会話の制御に効く行を信用する。
                        for _d in _sc_reads:
                            if _d not in _mcp_read_docs:
                                _mcp_read_docs.append(_d)
                        for _d in _sc_listed:
                            if _d not in _mcp_listed_docs and _d not in _mcp_read_docs:
                                _mcp_listed_docs.append(_d)
                        if codex_question is None and _sc_ask is not None:
                            codex_question = _sc_ask
                    # `_read_mcp_sidecar` は毎回ファイル先頭から全件を読み直す（増分読取ではない）
                    # ——`_absorb_mcp_sidecar` は1ターン内で複数回（attempt ごと・finally の
                    # 取りこぼし防止）呼ばれるため、`+=` で加算すると同じ行を毎回二重に数えて
                    # しまう。最新の全量スナップショットで**上書き**する（単調増加のため後読みが
                    # 常に正しい最新値）。
                    _mcp_tool_result_clipped = _sc_limits.get("tool_result_clipped", 0)
                    if _sc_limits.get("total_budget_hit"):
                        _mcp_total_budget_hit = True
                    _mcp_duplicate_tool_call = _sc_limits.get("duplicate_tool_call", 0)
                    _mcp_search_truncated = _sc_limits.get("search_truncated", 0)
                    if _sc_limits.get("tool_calls_exhausted"):
                        _mcp_tool_calls_exhausted = True

                def _pick_structured_claims() -> list[dict]:
                    """DEPTH-2 S1（§2.5）: `_pick_structured_headline`（`_candidate_final`）と同じ
                    選び方（`final` を優先・無ければ最後の構造化 message）で、その message が持つ
                    `claims` を返す（v1 形・`_schema_v2` 無効時は常に空リスト）。RV 高-1: 台帳ゲート
                    を通っていない final の claims も `_candidate_final` の境界で除外する。"""
                    if not _schema_v2:
                        return []
                    _cand = _candidate_final()
                    if _cand is not None:
                        return _cand.get("claims") or []
                    _valid = _structured_answers[_structured_answers_valid_from:]
                    if _valid:
                        return _valid[-1].get("claims") or []
                    return []

                try:
                    # AGENTS.md はベストエフォート（書けなくても Codex 実行自体は継続・fail-open）。
                    # fail-open でも気づけるよう warning は残す（containment/grounding の短縮形は
                    # _prompt/_prompt_mcp に常置済みなので、書込失敗時も丸裸にはならない＝多層防御）。
                    try:
                        if _plain:
                            codex_agents_md.write_agents_md(run_dir, plain=True)
                        else:
                            codex_agents_md.write_agents_md(run_dir, output_schema=_schema_on,
                                                            direct_read=_direct_read_ok,
                                                            mcp=mcp,
                                                            output_schema_v2=_schema_v2,
                                                            multi_agent=_multi_agent_enabled,
                                                            review_rounds=_review_rounds,
                                                            layer=_layer,
                                                            review_rounds_escalation=_review_rounds_escalation,
                                                            source_required=bool(_ledger_required_extra))
                    except Exception as e:
                        _log.warning("AGENTS.md write failed (fail-open, prompt still has containment): %s", e)
                    # スキル配備（案A′ ベース＋個人オーバーレイ）も同じくベストエフォート（fail-open）。
                    # knowledge=ON の Codex 実行全部で配備する（author レンズに限定しない・progressive disclosure）。
                    # plain は investigate-* スキル（原本を Python で開く前提の調査手順書）を置かない
                    # （成果物用の xlsx/docx/pptx/marp は今どおり配備する）。
                    try:
                        codex_skills.deploy_skills(run_dir, uid, users_dir,
                                                   skip_prefix="investigate-" if _plain else None)
                    except Exception as e:
                        _log.warning("skills deploy failed (fail-open): %s", e)
                    # profile config はここで書く（FileExistsError 等は fail-closed で
                    #   例外→except で answer=None→finally で CODEX_HOME 削除→決定的回答へ。古い config での起動を防ぐ）。
                    # marp/Chromium を read root に追加する必要は無い
                    # （Codex は .md を書くだけ・レンダは Sherpa 本体側で行う。marp_render.py 参照）。
                    # 会話ごとの CODEX_HOME は毎ターン再利用するため、前ターンの config.toml
                    #   （creds を含む・毎ターン即時削除している＝下の finally 参照）が残骸として
                    #   居ないことをまず確認してから書く（`_write_codex_authoring_config` 自体の
                    #   O_EXCL fail-closed は変更しない＝正規のターン跨ぎ再利用のための cleanup）。
                    if codex_home is not None:
                        try:
                            (codex_home / "config.toml").unlink(missing_ok=True)
                        except Exception:
                            pass
                        # 永続 CODEX_HOME は毎ターン再利用するため、前ターンのサイドカー残骸
                        #   （finally が走らない終了・unlink 失敗等で残った場合）を吸収してしまうと
                        #   未読 doc_id が根拠ゲートを通ったり前ターンの ask_user で今ターンが潰れる。
                        #   config.toml と同じくここで空から始める（非永続の使い捨て codex_home では
                        #   新規ディレクトリのため無害＝missing_ok=True）。unlink 自体が失敗した場合
                        #   （権限等）は残骸が居るか分からない＝このターンは `_absorb_mcp_sidecar` を
                        #   無効のままにする（`_sidecar_init_ok` を立てない・fail-open で親の観測のみ続行）。
                        #   本文・パスは伏せ、例外型と errno だけ warning に残す。
                        try:
                            _sidecar_path.unlink(missing_ok=True)
                            _sidecar_unlink_ok = True
                        except Exception as e:
                            _sidecar_unlink_ok = False
                            _log.warning(
                                "mcp sidecar pre-unlink failed (sidecar absorb disabled this turn): %s errno=%s",
                                type(e).__name__, getattr(e, "errno", None))
                        _write_codex_authoring_config(
                            codex_home, _kb_read_roots(ctx.world), _reason,
                            mcp, ctx.world, sp, self._web_search, _ask_disabled,
                            ollama_base_url=self._ollama_base_url, system_settings=self._system_settings,
                            layer=_layer, direct_read_roots=_direct_roots, sensitive_deny=_sensitive_deny,
                            sidecar_path=str(_sidecar_path) if mcp else None,
                            deny_roots=_deny_roots,
                            multi_agent=_multi_agent_enabled, orchestrator_model=self.model,
                            extra_mcp_env=_mcp_budget_env if mcp else None)
                        # ここまで（事前 unlink・設定生成）が両方例外を出さずに終わって初めて、
                        # このターンのサイドカー吸収を許可する（`_write_codex_authoring_config` が
                        # 例外を投げたら outer except へ抜けるため下の行は実行されない＝
                        # フラグは既定 False のまま・finally の `_absorb_mcp_sidecar` は無効化される）。
                        if _sidecar_unlink_ok:
                            _sidecar_init_ok = True
                        # Azure OpenAI 対応: 接続先が Azure 等へリダイレクトされていて、そのせいで
                        # web_search が強制 OFF になっている時だけ、理由を1回（このターンにつき1回・
                        # `_write_codex_authoring_config` 呼び出しはこの1箇所だけで resume 再試行でも
                        # 再呼出されない）伝える。Codex(Ollama) 構成（`_ollama_base_url` あり）は対象外。
                        if self._ollama_base_url is None:
                            _ws_note = _web_search_endpoint_note(
                                self._web_search, _openai_endpoint_kind(self._system_settings),
                                self._system_settings)
                            if _ws_note:
                                yield _node("web_search_endpoint", "think", "Web検索の制限", _ws_note, "done")
                    yield from _attempt(bool(resume_sid))
                    _absorb_last_message_fallback()
                    _update_structured_state()
                    _absorb_mcp_sidecar()
                    # 利用統計 activity: この attempt が捕捉した thread_id を控える——直後の resume
                    # 失敗判定で `thread_id` が None へリセットされる前に必ず控える（後で上書きされる
                    # と、この attempt 分の消費が要約から落ちる）。
                    if thread_id and thread_id not in _all_parent_thread_ids:
                        _all_parent_thread_ids.append(thread_id)
                    # resume を試みて1行も --json イベントが出なかった（＝セッション消失等で resume
                    # 失敗・実機確認済み: `codex exec resume <消失id>` は空 stdout・exit 1）場合、
                    # R1a 履歴 priming（プロンプトには self._history が既に前置済み）で新規セッションへ
                    # 即座にフォールバックする。ask_user 確認で終了した/途中停止されたターンは再試行しない。
                    # RV再検証 LOW-4: 将来の Codex CLI が失敗時に何らかの JSON（例: エラー系 item）を
                    # 1行以上出すようになっても取りこぼさないよう、「非ゼロ終了かつ agent_message が
                    # 1つも無い」場合も resume 失敗とみなす（`got_any_line` 単独判定の将来耐性・
                    # retry はこれまでどおり resume 試行時に1回だけ）。
                    _stopped = ctx.stop_event is not None and ctx.stop_event.is_set()
                    _no_agent_output = not _agent_msgs and not _agent_partial
                    _resume_attempt_failed = (not got_any_line) or (
                        attempt_returncode not in (0, None) and _no_agent_output)
                    if resume_sid and _resume_attempt_failed and codex_question is None and not _stopped:
                        _log.warning(
                            "codex resume failed (no output) sid=%s conv=%s uid=%s; falling back to a fresh session",
                            resume_sid, ctx.conversation_id, uid)
                        _agent_msgs.clear()
                        _agent_partial, _stream_error = "", False
                        mcp_neighbors.clear()
                        codex_usage, ran, codex_question, thread_id = None, False, None, None
                        # 失敗した resume attempt の構造化状態（古い session の final/in_progress）を
                        # 新規セッションへ持ち越さない——`_latest_structured` を残すとフォールバック後
                        # 最初の `_continuation_pending()` 判定が旧 session の値で決まってしまい、
                        # `_structured_answers` を残すと `_pick_structured_headline` が旧 final を
                        # 見出しに選び直してしまう。
                        _structured_answers.clear()
                        _structured_answers_valid_from = 0
                        _latest_structured = None
                        _resume_fallback_happened = True
                        yield from _attempt(False)
                        _absorb_last_message_fallback()
                        _update_structured_state()
                        _absorb_mcp_sidecar()
                        # 利用統計 activity: フォールバック後の新しい thread_id も控える（前段の
                        # attempt 分と合わせて2件になる＝`activity.summarize_turn` が1つの parent
                        # エントリへ合算する）。
                        if thread_id and thread_id not in _all_parent_thread_ids:
                            _all_parent_thread_ids.append(thread_id)
                    # 自動継続（「途中経過で止まった」を検出）と台帳ゲート（「`final` だが台帳が
                    # 未完了」を検出）は1つの while ループへ統合する（RV 高-1・2026-09-22 是正）。
                    # 台帳ゲートの継続 attempt が `in_progress` を返した場合に自動継続判定
                    # （`_continuation_pending()`）へ戻れないと、拒否したはずの古い `final` が
                    # `_pick_structured_headline`/`_pick_structured_claims` に拾われてしまう
                    # （`_structured_answers` は「final を返した」事実だけを見て最後の final を
                    # 選ぶため、台帳ゲートが破棄したことを知らない）——毎周「今の状態がどちらの
                    # 継続を必要とするか」を判定し直すことで、取りこぼしを無くす。上限は別枠のまま
                    # （`_continue_limit`＝自動継続／`_LEDGER_CONTINUE_CAP`＝台帳）。優先順位:
                    # 台帳未完了（`final` かつ未完了）＞ 途中経過（`final` でない）。
                    #
                    # RV 高-1（2026-09-22 3巡目是正）: 台帳ゲート分岐のトリガーは
                    # `_latest_structured`（直近 attempt の最後のメッセージ）ではなく
                    # `_candidate_final()`（`_pick_structured_headline` と同じ「採用候補」選び方）
                    # を使う——同一 attempt が final の直後に in_progress を出すと
                    # `_latest_structured` は in_progress になるが、その final は
                    # `_structured_answers` に残ったまま見出し選択の対象になり得る。採用候補の
                    # final にも必ず台帳ゲートを適用することで、一度もゲートを通っていない
                    # 「未評価の final」が受理されるのを防ぐ。
                    _continue_limit = _env_int("SHERPA_CODEX_AUTO_CONTINUE", 3, 0, 5)
                    _ledger_manifest_retry_used = False
                    _ledger_no_progress_streak = 0
                    # RV 中-1（2026-09-22 5巡目是正）: 台帳継続の attempt 前後だけで進捗を比較すると、
                    # 「台帳継続（無更新）→自動継続（終端化）→台帳継続（無更新）」のように**自動継続の
                    # 間に進捗が起きたケース**を見逃す（台帳ゲート分岐に戻ったときの `_ledger_verdict`
                    # は毎回ディスクから読み直すため、直前の自動継続の結果を反映済み＝streak 側の
                    # 比較基準が古いまま「今回も無変化」と誤認していた）。attempt の種類を問わず、
                    # ループの各周の先頭で `_ledger_progressed`（前周と今周の `LedgerSnapshot` の
                    # 比較・純関数）を見て、進捗があれば streak をリセットする。
                    _ledger_prev_snapshot: investigation_ledger.LedgerSnapshot | None = None
                    while True:
                        _stopped_for_continue = ctx.stop_event is not None and ctx.stop_event.is_set()
                        if codex_question is not None or _stopped_for_continue:
                            break
                        if not (_session_persistence_enabled and (thread_id or resume_sid)):
                            break
                        # 台帳の書込手段は MCP の台帳ツールだけ（ファイル直接書込は禁止）——MCP 無効の
                        # 構成では台帳を作れないので、指示文と同じ条件でゲートも無効にする。
                        _ledger_gate_active = _schema_v2 and mcp and _investigation_dir is not None
                        _ledger_verdict = None
                        if _ledger_gate_active:
                            _ledger_snapshot = investigation_ledger.load_ledger(_investigation_dir)
                            _ledger_verdict = investigation_ledger.ledger_complete(
                                _ledger_snapshot, required_extra=_ledger_required_extra)
                            if (_ledger_prev_snapshot is not None
                                    and _ledger_progressed(_ledger_prev_snapshot, _ledger_snapshot,
                                                           required_extra=_ledger_required_extra)):
                                _ledger_no_progress_streak = 0
                            _ledger_prev_snapshot = _ledger_snapshot
                        _has_candidate_final = _candidate_final() is not None
                        if _ledger_gate_active and _has_candidate_final:
                            # ---- 台帳ゲート分岐（正典§2/§4/§6・`_schema_v2` のときだけ効かせる）----
                            if _ledger_verdict.complete:
                                break
                            # RV 中-2（4巡目是正・5巡目で発行前の共通上限判定へ整理）: 台帳起因の
                            # 催促（未完了／内容不正／不存在の3種）はどれもここで上限を超えたら
                            # 発行しない——「不存在は1回だけ」は cap の内側の追加制約として働く
                            # （進捗を伴う継続を重ねた後に manifest が消えても、cap 到達後は
                            # 催促せず cap で受理する）。
                            if _ledger_continuations >= _LEDGER_CONTINUE_CAP:
                                _investigation_stopped_reason = "cap"
                                break
                            if _ledger_verdict.manifest_invalid:
                                # 「ファイル不存在」（台帳を作らない依頼＝1回だけ催促して受理・
                                # fail-open）と「ファイルは存在するが内容が規約に合わない」（壊れた
                                # 台帳＝正典§4「壊れた台帳から final を生成しない」に沿って cap
                                # まで修復を促す）を区別する。`_retire_investigation_ledger` の
                                # 判定と同じ「ファイル存在」の基準を使う。
                                if (_investigation_dir / "manifest.json").is_file():
                                    _ledger_prompt_text = _LEDGER_MANIFEST_INVALID_PROMPT
                                else:
                                    if _ledger_manifest_retry_used:
                                        _investigation_stopped_reason = "ledger_missing"
                                        break
                                    _ledger_manifest_retry_used = True
                                    _ledger_prompt_text = _LEDGER_MANIFEST_MISSING_PROMPT
                            else:
                                if _ledger_no_progress_streak >= 2:
                                    _investigation_stopped_reason = "no_progress"
                                    break
                                _ledger_prompt_text = _ledger_continue_prompt(_ledger_verdict)
                                # この継続を「進捗なし」の1回として仮計上する——次周の先頭（上の
                                # 進捗比較）で実際に何か終端化していれば 0 へ戻る。
                                _ledger_no_progress_streak += 1
                            # RV 高-1: この final は拒否済み＝以後の見出し/主張候補から除外する
                            # （`_pick_structured_headline`/`_pick_structured_claims` はこの境界
                            # より後だけを見る）。次に final が来ても再拒否されればここで再度進む。
                            _structured_answers_valid_from = len(_structured_answers)
                            _ledger_continuations += 1
                            yield _node(
                                f"ledger-continue-{_ledger_continuations}", "think", "調査台帳を確認",
                                f"未完了の調査項目があるため続けます（{_ledger_continuations}/"
                                f"{_LEDGER_CONTINUE_CAP}）", "done")
                            yield from _attempt(True, prompt_text=_ledger_prompt_text)
                            _absorb_last_message_fallback()
                            _update_structured_state()
                            _absorb_mcp_sidecar()
                            continue
                        # ---- 自動継続分岐（旧ループ・「途中経過で止まった」を検出）----
                        # 正常終了（returncode 0）で agent_message が「作業宣言だけ」（結論文が
                        # 1つも無い＝_pick_codex_headline が規則③に落ちる、または構造化出力が
                        # in_progress/不正）なら、Codex セッションの続きを自動で呼ぶ（利用者の
                        # 「続けて」連投をシステム側で肩代わりする）。`got_any_line` を要求するのは、
                        # 継続 attempt 自身が無出力で終わった場合に古い（蓄積済みの）作業宣言だけを
                        # 根拠に空振りを繰り返さないため。判定は `_continuation_msgs()`＝**直前の
                        # attempt の message だけ**（前の attempt の作業宣言と連結して誤判定しない
                        # ため）。
                        if not (attempt_returncode == 0 and got_any_line and _continuation_pending()):
                            break
                        if _auto_continue_count >= _continue_limit:
                            break
                        # limits（利用統計「打ち切りの内訳」計測・制限自体は変えない）: この if を
                        # 通過＝実際に continuation attempt を1回発行する。
                        _auto_continue_count += 1
                        yield _node(f"cx-continue-{_auto_continue_count}", "think", "続きを実行",
                                   f"途中経過で止まったため続きを調べます（{_auto_continue_count}/"
                                   f"{_continue_limit}）", "done")
                        yield from _attempt(
                            True, prompt_text=_CONTINUE_PROMPT_SCHEMA if _schema_on else _CONTINUE_PROMPT)
                        _absorb_last_message_fallback()
                        _update_structured_state()
                        _absorb_mcp_sidecar()
                        # ツールを1つも呼ばずに終わった continuation は打ち切る（同じ宣言の空振りを
                        # 繰り返さない）——ただし、これがまだ `_continuation_pending()`（in_progress
                        # のまま等）**かつ** final 候補が無い場合だけ（RV 高-1・2026-09-22 4巡目
                        # 是正）。`_continuation_pending()` は直近 attempt の最後のメッセージしか
                        # 見ないため、この attempt が final の直後に in_progress を出した場合でも
                        # True になる——`_candidate_final()`（見出し選択と同じ「採用候補」選び方）で
                        # final 候補の有無を確認し、候補があるならここで打ち切らずループ先頭の台帳
                        # ゲートへ戻す（打ち切ると、台帳ゲートを一度も通っていない final が
                        # `_pick_structured_headline` に採用されてしまう）。
                        if (not _attempt_ran_tools and _continuation_pending()
                                and _candidate_final() is None):
                            break
                    # 台帳の最終判定（正典§4「壊れた台帳から final を生成しない」）: `_schema_v2`
                    # 有効なときだけ記録する（§2「無効なら従来どおり」＝env に新しいキーを足さない）。
                    # ゲート自体が1回も継続を発行しなくても（resume 不能・ask_user/停止で打ち切り等）、
                    # 受理する回答の実状を記録する——`env["investigation"]`/退避判定の根拠。
                    if _schema_v2 and mcp and _investigation_dir is not None:
                        _investigation_verdict = investigation_ledger.ledger_complete(
                            investigation_ledger.load_ledger(_investigation_dir),
                            required_extra=_ledger_required_extra)
                        if _investigation_stopped_reason is None:
                            if _investigation_verdict.complete:
                                _investigation_stopped_reason = "complete"
                            elif _investigation_verdict.manifest_invalid:
                                _investigation_stopped_reason = "ledger_missing"
                            else:
                                # ゲート未実行（`_schema_v2` 無効・resume 不能等）で打ち切り条件の
                                # どれにも該当しないまま終わった残余ケース——独自の第5分類は増やさず
                                # 「これ以上は続けられない」として cap 側へ寄せる（正典§2の4分類のみ使う）。
                                _investigation_stopped_reason = "cap"
                except Exception:
                    answer = None
                    _stream_error = True
                finally:
                    # DEPTH-2 S3b是正: サイドカーは codex_home の削除・後始末より**前**に必ず一度
                    # 吸収する（例外が `_attempt()` の途中で起きて、ループ内の毎 attempt 分の
                    # `_absorb_mcp_sidecar()` 呼出まで届かなかった経路の取りこぼし防止・fail-open）。
                    _absorb_mcp_sidecar()
                    if codex_home is not None:
                        # 利用統計 activity の対象親（このターンで使った全ての親・resume 失敗の
                        # フォールバックで切り替わった分も含む）。現在の `thread_id` が
                        # `_all_parent_thread_ids` の記録漏れ（想定外の経路）で欠けていないことの
                        # 保険としてここでも一応足す。
                        _activity_parent_ids = list(_all_parent_thread_ids)
                        if thread_id and thread_id not in _activity_parent_ids:
                            _activity_parent_ids.append(thread_id)
                        if _child_thread_ids or thread_id or _activity_parent_ids:
                            # DEPTH-2 S3b: 子の session JSONL（`sessions/**`）は非永続セッションだと
                            # 直後の rmtree で消える——読むのは削除より**前**（この if ブロックの中の
                            # どちらの分岐よりも前）でなければならない。読めなくても fail-open
                            # （本体ターンの正常終了を妨げない）。`thread_id`（今回の親 thread id）
                            # だけでも呼ぶ——`_child_thread_ids` が空（`spawn_agent` item が出ない
                            # CLI バージョン）でも、子 rollout の `parent_thread_id` 突合で拾える。
                            try:
                                (_child_usage_totals, _child_usage_found, _child_usage_missing,
                                 _child_usage_detected) = (
                                    _collect_child_token_usage(codex_home, _child_thread_ids, thread_id,
                                                               _turn_started_wall))
                            except Exception:
                                pass
                            # 利用統計 activity（正典§3.1）: 同じ理由（codex_home 削除より前）・
                            # 同じ fail-open 方針で要約する——読めなくても本体ターンは落とさない
                            # （型名/errno だけ warning ログに出す・本文/パスは出さない）。settings は
                            # 開始行の直後で確定済みの値をそのまま渡す。prepare/agent はここで初めて
                            # 確定する（`_agent_start_mono`/`_agent_end_mono` は最後の attempt の
                            # finally まで更新され続けるため）——prepare は `ctx.turn_started_mono`／
                            # `_agent_start_mono` のどちらかが無ければキー自体を置かない（0埋めしない・
                            # chat_service 側の `_finalize_activity_phases` が post の計算可否を
                            # このキーの有無で判定する）。
                            try:
                                _activity_agent_ms = (
                                    max(0, round((_agent_end_mono - _agent_start_mono) * 1000))
                                    if (_agent_start_mono is not None and _agent_end_mono is not None)
                                    else 0)
                                _activity_phases_ms = {"agent": _activity_agent_ms}
                                if ctx.turn_started_mono is not None and _agent_start_mono is not None:
                                    _activity_phases_ms["prepare"] = max(
                                        0, round((_agent_start_mono - ctx.turn_started_mono) * 1000))
                                env["activity"] = _summarize_codex_activity(
                                    codex_home, parent_thread_ids=_activity_parent_ids,
                                    child_thread_ids=_child_thread_ids,
                                    turn_started_wall=_turn_started_wall, settings=_activity_settings,
                                    phases_ms=_activity_phases_ms)
                            except Exception as _act_exc:
                                _log.warning("codex activity summarize failed: %s errno=%s",
                                            type(_act_exc).__name__, getattr(_act_exc, "errno", None))
                        if _persist_session:
                            # セッション実体（`sessions/` の JSONL）は次ターンの resume の
                            # ために保持する。creds を含む config.toml だけ即時削除し露出窓を1ターン分に
                            # 限定する（retention のスイープはディレクトリ全体を対象にする＝別途 api.py）。
                            # `auth.json`（実 `~/.codex/auth.json` への
                            # symlink・`_write_codex_authoring_config` が張る）も同じ理由で毎ターン削除する
                            # （放置すると永続 CODEX_HOME に無期限残存＝次ターンは `_write_codex_authoring_config`
                            # が `dst.exists()` を見て再作成するので消しても実害は無い）。サイドカーも同じ
                            # 理由で毎ターン削除する（codex_home 自体は resume のため残るので rmtree では
                            # 消えない＝前ターンの読取記録を次ターンへ持ち越さない）。
                            try:
                                (codex_home / "config.toml").unlink(missing_ok=True)
                            except Exception:
                                pass
                            try:
                                (codex_home / "auth.json").unlink(missing_ok=True)
                            except Exception:
                                pass
                            try:
                                _sidecar_path.unlink(missing_ok=True)
                            except Exception:
                                pass
                        else:
                            # per-request CODEX_HOME（profile＋auth symlink）を後始末（symlink target は
                            # 消えない）。サイドカーはこの codex_home 配下＝rmtree で併せて消える。
                            try:
                                shutil.rmtree(codex_home, ignore_errors=True)
                            except Exception:
                                pass
                # サブプロセス後始末の直後・以降のどの分岐（schema-era エラーの re-raise・ask_user の
                # 早期 return・通常終了）を通っても必ず1回だけ出す（実環境で「モデルが複数の MCP ツールを
                # 同時に呼ぶか」を見るための計測。UI・env には載せない・「1実行あたり1行」を保つため
                # 早期 return より前に置く）。
                _log.info("codex mcp calls: total=%d max_in_flight=%d conv=%s uid=%s",
                          _mcp_calls["total"], _mcp_calls["max_in_flight"], ctx.conversation_id, uid)
                # codex.log 終了行（1実行1回・本文/資料名は書かない）。usage 合計は turn.completed の
                # 最新 snapshot（`_accumulate_codex_usage` と同じくセッション累計であり足し算ではない）。
                _codex_usage_total_tokens = (
                    (codex_usage.get("input_tokens", 0) + codex_usage.get("output_tokens", 0))
                    if codex_usage else 0)
                # 本文・資料名・秘密は載せない（固定語彙のコードと件数・所要時間だけ）。トークン内訳
                # （入力/キャッシュ済み入力/出力/推論出力）は親（`codex_usage`）・子合算
                # （`_child_usage_totals`）ともこのターンで既に確定済みの集計値をそのまま出す
                # （ログ用に別計算しない）。cached は input に、reasoning_output は output に
                # それぞれ包含される内訳であって別枠ではない（二重計上しない・`_usage_meta` と同じ契約）。
                # 台帳の終了状態（正典§6「codex.log 終了行」）: 語彙は env["investigation"]["stopped_reason"]
                # の4値より粗い3値（missing/complete/incomplete）——ログ行は運用の一目確認用、
                # 打ち切り理由の詳細（no_progress/cap 等）は env 側にだけ持つ。
                if _investigation_verdict is None or _investigation_verdict.manifest_invalid:
                    _ledger_log_state = "missing"
                elif _investigation_verdict.complete:
                    _ledger_log_state = "complete"
                else:
                    _ledger_log_state = "incomplete"
                # claims_downgraded（末尾ログ用）: この行は `if answer:` 分岐（claims 確定・
                # `env["data"]["claims"]` 適用）より前に出るため、ここでも `_claims_vs_ledger` を
                # 呼んで求める——根拠種別ゲート（`_apply_codex_evidence_gate`）は経ていない生の
                # confirmed 主張が対象のため、実際に envelope へ適用される件数の上限値になる
                # （種別ゲートで先に推定へ落ちる分もここでは confirmed のまま数える）。
                _claims_for_log = _pick_structured_claims() if _schema_on else []
                _, _log_ledger_check = _claims_vs_ledger(
                    _claims_for_log,
                    investigation_ledger.load_ledger(_investigation_dir)
                    if _investigation_dir is not None
                    else investigation_ledger.LedgerSnapshot(manifest=None, items={}, invalid_ids=()),
                    # `_retire_investigation_ledger`/台帳ゲート本体と同じ「ファイル存在」基準
                    # （`(investigation_dir / "manifest.json").is_file()`）——`load_ledger` が
                    # 返す `manifest=None` だけでは「ファイル無し」と「内容不正」を区別できない。
                    manifest_file_exists=(
                        (_investigation_dir / "manifest.json").is_file()
                        if _investigation_dir is not None else False))
                _claims_downgraded_for_log = _log_ledger_check.get("downgraded", 0)
                # exec_failed/sandbox_failed（サンドボックスの実行中検知・依頼文の背景）:
                # `env["activity"]` は直前の finally ブロックで確定済み（未確定＝要約自体が失敗した
                # ターンは `exec_failure_counts(None)` が (0, 0) を返す＝fail-open）。
                _exec_failed_for_log, _sandbox_failed_for_log = (
                    _codex_exec_failure_counts(env.get("activity")))
                _log_codex.info(
                    "end conv=%s uid=%s returncode=%s thread_id=%s events=%s mcp_calls=%d "
                    "spawn_agents=%d usage_tokens=%d input=%d cached_input=%d output=%d "
                    "reasoning_output=%d child_input=%d child_cached_input=%d child_output=%d "
                    "child_reasoning_output=%d children_found=%d children_missing=%d "
                    "error_code=%s clipped=%d budget_hit=%s elapsed=%.1fs ledger=%s continuations=%d "
                    "claims_downgraded=%d exec_failed=%d sandbox_failed=%d",
                    ctx.conversation_id, uid, attempt_returncode, thread_id, _event_type_counts,
                    _mcp_calls["total"], _child_usage_detected, _codex_usage_total_tokens,
                    (codex_usage.get("input_tokens", 0) if codex_usage else 0),
                    (codex_usage.get("cached_input_tokens", 0) if codex_usage else 0),
                    (codex_usage.get("output_tokens", 0) if codex_usage else 0),
                    (codex_usage.get("reasoning_output_tokens", 0) if codex_usage else 0),
                    _child_usage_totals.get("input_tokens", 0),
                    _child_usage_totals.get("cached_input_tokens", 0),
                    _child_usage_totals.get("output_tokens", 0),
                    _child_usage_totals.get("reasoning_output_tokens", 0),
                    _child_usage_found, _child_usage_missing,
                    _turn_failed_code, _mcp_tool_result_clipped, _mcp_total_budget_hit,
                    time.monotonic() - _codex_run_started_at, _ledger_log_state, _ledger_continuations,
                    _claims_downgraded_for_log, _exec_failed_for_log, _sandbox_failed_for_log)
                if _sandbox_failed_for_log:
                    # api.log（`_log`＝"sherpa" ロガー・WARNING 以上は run ログにも残る契約）へ
                    # 一目で気付ける形で残す。本文・資料名は含めない（件数と会話IDのみ）。
                    _log.warning(
                        "Codex のサンドボックスでコマンドが失敗しています"
                        "（make doctor の「Codex のサンドボックス」を確認）conv=%s 回数=%d",
                        ctx.conversation_id, _sandbox_failed_for_log)
                # ask_user が出たターンは question 優先＝env/_result・成果物台帳登録を出さずここで終了する
                # （agentic の {"question":..}→return と同じ意味論・回答は chat.js の整形再送＝新 codex exec で拾う）。
                # proc は直上の finally で後始末済み。chat_service はこの question を answer.question として保存する。
                if codex_question is not None:
                    # 親ノード（"Codex が調べる"）も冒頭で "active" のまま止まっているので、
                    # 通常経路の完了 yield（下の if answer/else ブロック）と同様にここで "done" に確定させる。
                    yield _node("codex", "think", "Codex が調べる", "ユーザに確認するため終了しました", "done")
                    # 早期 return が `-o` 一時ファイル（last-message-*.txt）の削除を
                    # バイパスして .tmp/ に蓄積し得た。通常経路（下の unlink）と同じ best-effort で先に消す。
                    try:
                        _last_message_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    # 利用統計 activity（正典§3.1）: この経路は env/_result を出さないため、finally
                    # ブロックで確定済みの `env["activity"]` は question イベント経由で運ばないと
                    # 消える（chat_service の確認カード保存側が source:"none" で作り直してしまう）。
                    if env.get("activity") is not None:
                        codex_question["activity"] = env["activity"]
                    yield codex_question
                    return
                # 調査台帳（正典§3/§4・回答 envelope への記録）: 本文・path は含めず、id 一覧は
                # 肥大化防止で先頭50件に打ち切る。ゲートが1度も走らなかった（`_investigation_dir`
                # が未確定＝早期 return 済み経路）ターンには載せない。
                if _investigation_verdict is not None:
                    # RV 中-3（2026-09-22 3巡目是正）: 退避処理をここで**先に実行**して成否を確定
                    # してから `retained` に実際の値を書く（予測値ではない）——manifest 未作成の
                    # まま催促後に受理したケース・コピー失敗のケースで「退避されていないのに
                    # true」という食い違いがあった。外側 finally はこの turn では二重に実行しない
                    # （`_investigation_retire_done` を見る・切断/例外時のフォールバックのみ働く）。
                    # RV 中-1（10巡目是正）: 復元が途中で失敗したターン（`_investigation_retire_
                    # done` は復元失敗時点で既に立てている）は退避を一切行わない——元の退避台帳を
                    # 保持する。
                    _investigation_retained = False
                    if _ledger_home is not None and not _investigation_retire_done:
                        _investigation_retained = _retire_investigation_ledger(
                            _investigation_dir, _ledger_home, required_extra=_ledger_required_extra)
                        _investigation_retire_done = True
                    _ledger_id_report_limit = 50
                    env["investigation"] = {
                        "complete": _investigation_verdict.complete,
                        "manifest_invalid": _investigation_verdict.manifest_invalid,
                        "counts": dict(_investigation_verdict.terminal_counts),
                        "non_terminal": list(_investigation_verdict.non_terminal_ids[:_ledger_id_report_limit]),
                        "invalid": list(_investigation_verdict.invalid_ids[:_ledger_id_report_limit]),
                        "missing": list(_investigation_verdict.missing_ids[:_ledger_id_report_limit]),
                        "continuations": _ledger_continuations,
                        "stopped_reason": _investigation_stopped_reason,
                        "retained": _investigation_retained,
                        "restored": _investigation_restored,
                    }
                    env["limits"] = {**(env.get("limits") or {}),
                                     "ledger_incomplete": not _investigation_verdict.complete}
                # §2-3/5: `_schema_on` は構造化 message から見出しを選ぶ（生 JSON をそのまま出さない・
                # 平文ヒューリスティックへは戻さない）。無効時は現行どおりの選び方（下記）。
                if _schema_on:
                    answer = _pick_structured_headline()
                else:
                    # 集めた agent_message から結論を優先して headline を選ぶ
                    # （進行中の作業宣言を見出しにしない・最後の1件を鵜呑みにしない）。
                    _picked = _pick_codex_headline(_agent_msgs, _agent_partial,
                                                   prefer_marker="参照した資料") or None
                    # §3: -o は保険。--json の agent_message から拾えなかった時だけ最終メッセージ
                    # ファイルを読む（既存の JSON 経路が主）。読んでも読まなくても使い終わったら必ず削除する
                    # （.tmp/ は台帳登録スキャン対象外＝放置すると溜まり続けるため）。
                    # 途中例外時は集めた _agent_msgs が進行中の作業宣言だけの可能性が
                    # あるため、完全版が入り得る `-o` 最終メッセージを**先に**試し、空/無いときだけ pick に委ねる。
                    # 正常終了時は現行どおり pick が主・`-o` は従（fallback）。
                    if _stream_error:
                        answer = _read_last_message_fallback(_last_message_path) or _picked
                    else:
                        answer = _picked or _read_last_message_fallback(_last_message_path)
                try:
                    _last_message_path.unlink(missing_ok=True)
                except Exception:
                    pass
                # codex exec を実際に起動した（attempt_returncode is not None＝Popen が完走した）
                # にもかかわらず stdout に JSON を1行も出さず（got_any_line=False）、answer も得られない
                # 場合だけ「正直に伝える」文言へ切り替える対象とする。ユーザーの stop_event による打ち切り
                # は失敗ではないため対象外（途中で殺しただけで agent_message が無いのは想定内の挙動）。
                _stopped_final = ctx.stop_event is not None and ctx.stop_event.is_set()
                # §2-10: 最新 attempt（継続 attempt を含む）が `turn.failed`／`error` で閉じ、
                # その attempt 自身は agent_message を1つも出さなかった（`_agent_msgs[_attempt_msgs_start:]`
                # も `_agent_partial` も空）場合、過去 attempt の（古い）回答が `answer` に残っていても
                # 明示失敗として扱う——`_pick_structured_headline`／`_pick_codex_headline` は attempt を
                # またいだ蓄積から拾うため、放置すると「実際には答えられなかった」ターンで古い途中経過
                # （in_progress の answer 等）をそのまま見出しにしてしまう。利用者の明示停止は対象外。
                _turn_failed_no_new_message = (
                    _turn_failed and not _agent_msgs[_attempt_msgs_start:] and not _agent_partial
                    and not _stopped_final)
                if _turn_failed_no_new_message:
                    answer = None
                # §2-10: `turn.failed`／`error`（トップレベルイベント）で閉じた attempt は、JSON 自体は
                # 読めていても（got_any_line=True）agent_message が無いままの失敗——`not got_any_line`
                # 単独では拾えないため `_turn_failed` を OR で加える。
                if (not answer and (not got_any_line or _turn_failed) and attempt_returncode is not None
                        and not _stopped_final):
                    _codex_silent_failure = True
                # 自動継続を尽くしてもなお（上限0・セッション非永続で1回も継続できなかった場合・継続
                # attempt が無出力/異常終了で終わった場合を含む）作業宣言だけなら、本文（headline）は
                # 書き換えず印だけ立てる＝「途中までの結果」と伝えて続きを促す。利用者の明示停止は
                # 途中結果として扱わない。
                _codex_stopped_early = (
                    _continuation_pending()
                    and not _stopped_final and not _turn_failed_no_new_message)
                # Feature A: run_dir の新規ファイルを検出して台帳登録する。
                # Codex の cwd = run_dir のため、personal アップロード（files/）は読み取り・書き込み不可。
                # 台帳登録: run_dir の新規ファイルを personal_workspace_files に登録（ES/Neo4j には一切書かない）。
                if run_dir.is_dir():
                    # `.tmp`（TMPDIR）配下は Codex の一時ファイル＝台帳登録しない（成果物のみ登録）。
                    # `.agents`（配備したスキル）配下も同様に対象外（スキルコピーが
                    # 成果物として files/ に誤って登録されないように・毎回作り直しなので前後で常に差分が出る）。
                    # ルート直下の AGENTS.md・`.mcp_sidecar.jsonl` も対象外（before 側と対・理由はそちらのコメント参照）。
                    _after_ws_files = {
                        p for p in run_dir.rglob("*")
                        if p.is_file() and not p.is_symlink()
                        and p.relative_to(run_dir) not in (Path("AGENTS.md"), Path(_MCP_SIDECAR_NAME))
                        and not ({".tmp", ".agents"} & set(p.relative_to(run_dir).parts))
                    }
                    new_authoring = sorted(_after_ws_files - _before_ws_files)
                    if decision["lens"] != "author" and new_authoring:
                        # 作成の依頼（画面の「資料を作成」／依頼文が作成と判定）以外では成果物に
                        # しない——登録せず、run_dir の削除で一緒に消す。
                        _log.info("codex created %d file(s) in a non-authoring turn; discarded",
                                  len(new_authoring))
                        new_authoring = []
                    for fp in new_authoring:
                        codex_created_files.append(str(fp))
                    _any_new_ws = bool(new_authoring)
                    # Marp レンダは sandbox の外＝Sherpa 本体が network 隔離
                    # （unshare）下で実行する。Codex は .md を書くだけ（sandbox から marp/Chromium を
                    # 見せる必要が無くなり攻撃面も縮小・RUNTIME-SANDBOX §10.3 の未解決問題を回避）。
                    # ベストエフォート（fail-open）: 失敗しても .md 自体は既に台帳登録対象に入っている。
                    try:
                        from ... import marp_render
                        _mds = [p for p in new_authoring if p.suffix == ".md"]
                        _rendered = marp_render.render_outputs(
                            [p for p in _mds if marp_render.is_marp_markdown(p)],
                            marp_bin=_marp_bin(), chrome_path=_detect_chrome_path(),
                            theme_dirs=[run_dir / ".agents" / "skills" / "marp" / "themes",
                                        _SKILLS_BASE / "marp" / "themes"],
                            containment_root=run_dir)   # 入出力を run_dir 内実体に強制
                        codex_created_files.extend(str(p) for p in _rendered)
                        _any_new_ws = _any_new_ws or bool(_rendered)
                    except Exception as e:
                        _log.warning("marp_render: レンダ処理が例外で終了（fail-open）: %s", e)
            # 台帳登録（Codex が authoring/ に置いたファイルを files/ に移動して台帳登録）。
            # Codex 生成物を authoring/ → files/ に移動することで、既存の grep/delete/TTL 機構をそのまま使う。
            # authoring/ に中間生成物が残らないため、次回 Codex 実行時も個人ファイルは見えない。
            if codex_created_files and 'ws_authoring' in dir():
                try:
                    from ... import store as _store
                    import datetime as _dt
                    import shutil as _shutil
                    _ttl_days = int(os.environ.get("SHERPA_WORKSPACE_TTL_DAYS", "90") or 0)
                    _expires = (
                        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=_ttl_days)
                        if _ttl_days > 0 else None
                    )
                    # ws_files が有効（非 symlink）なら files/ に移動して登録。
                    # MEDIUM fix: ws_files が symlink の場合は登録スキップ（fail-closed）。
                    # HIGH fix: files/ 移動時に同名ファイルが存在する場合は別名化（上書き禁止）。
                    _dest_dir = ws_files if (ws_files is not None and ws_files.is_dir()) else None
                    if _dest_dir is None:
                        # symlink or files/ が使えない → fail-closed（登録なし・grep/delete 対象外）。
                        # 成果物は run_dir に残ったまま（move していない）なので、保存失敗として
                        # run_dir を消さず残す・回答へ注記する（黙って削除して見せかけの成功にしない）。
                        _created_files_failed = True
                        _log.warning("codex created files could not be registered: files/ unavailable "
                                    "(run_dir=%s)", run_dir.name)
                    else:
                        for _fp in codex_created_files:
                            try:
                                _p = Path(_fp)
                                if not _p.is_file():
                                    continue
                                _stem, _suf = _p.stem, _p.suffix
                                # 同名回避の**名前確定も lock 内**で行う（並行 HTTP upload と衝突して
                                #   live ファイルを move で上書きするのを防ぐ）。候補名ごとに lock を取り、
                                #   lock 内で「物理未存在かつ生きた台帳なし」を確認できた名前にだけ move+登録する。
                                _i = 0
                                while _i <= 10000:                       # 無限ループ防止
                                    _rel = _p.name if _i == 0 else f"{_stem}_{_i}{_suf}"
                                    _dst = _dest_dir / _rel
                                    with _store.workspace_file_lock(uid, _rel):
                                        if _dst.exists() or not _store.no_live_upload_for_path(uid, _rel):
                                            _i += 1
                                            continue                     # この名前は埋まっている → 次 suffix へ
                                        _shutil.move(str(_p), str(_dst))
                                        try:
                                            _data = _dst.read_bytes()
                                            _sha = hashlib.sha256(_data).hexdigest()
                                            _row = _store.record_workspace_file(
                                                uid, _rel, str(_dst), len(_data), _sha, expires_at=_expires)
                                            _created_file_rows.append(_row)   # P1-c: created_files カード用
                                        except Exception:
                                            # move は成功したが台帳登録に失敗＝files/ に台帳の無い孤児を
                                            # 残さない。同じ lock 内で run_dir 側へ戻す（回収は run_dir
                                            # 保持側の責務に一本化する）。
                                            try:
                                                _shutil.move(str(_dst), str(_p))
                                            except Exception as _move_back_err:
                                                # 差し戻し（2回目の move）にも失敗＝台帳の無い
                                                # ファイルが files/ に取り残る最終形。黙って握り
                                                # 潰さず明示的に記録する（相対パスのみ）。例外を
                                                # そのまま文字列化すると `OSError`/`shutil.Error` は
                                                # 失敗した絶対パスを本文に含むため、型と errno だけ
                                                # 残す（フルパスは出さない）。
                                                _created_files_failed = True
                                                _log.warning(
                                                    "codex created file could not be moved back to "
                                                    "run_dir after registration failure (orphaned in "
                                                    "files/): %s: type=%s errno=%s",
                                                    _masked_run_dir_path(_fp, run_dir),
                                                    type(_move_back_err).__name__,
                                                    getattr(_move_back_err, "errno", None))
                                            raise
                                    break
                            except Exception as e:
                                # 通常の move 失敗（`shutil.move`/`shutil.Error` は失敗した src/dst の
                                # 絶対パスを文字列表現に含む）も、差し戻し失敗と同じく型と errno だけ
                                # 記録する（フルパスは出さない）。
                                _created_files_failed = True
                                _log.warning("codex created file move/registration failed for %s: type=%s errno=%s",
                                            _masked_run_dir_path(_fp, run_dir),
                                            type(e).__name__, getattr(e, "errno", None))
                except Exception as e:
                    # 個別ファイルのループへ入る前の設定段階（store import・_expires 計算等）の失敗。
                    # この時点ではどの成果物も files/ へ移されていない＝全件が run_dir に残ったまま。
                    _created_files_failed = True
                    _log.warning("codex created files registration setup failed (run_dir=%s): %s",
                                run_dir.name, e)
            # limits（利用統計「打ち切りの内訳」計測・制限自体は変えない）: 自動継続はこの `run()`
            # 自身が判定しているためここで数える（`search_truncated`／`tool_result_clipped`／
            # `duplicate_tool_call` は `mcp_server.py` がサイドカー経由で報告した値を下で合流させる）。
            if _auto_continue_count and isinstance(env, dict):
                env["limits"] = {**(env.get("limits") or {}), "auto_continues": _auto_continue_count}
            # 深さ案内（chat_service._depth_actually_helps・§2.3）が本体と同じ判定を使うための
            # 唯一の受け渡し口。`codex_multi_agent_enabled`（sandbox.py）の結果を usage の
            # is_local から再現できない（Azure も既定 OpenAI と同じ "cloud"）ため、ここで結果
            # そのものを渡す。
            if isinstance(env, dict):
                env["codex_multi_agent"] = _multi_agent_enabled
            # グラフ・全文検索の縮退（親が検知した世代不一致＋子がサイドカーで報告した障害コード）を
            # 1つの印にまとめる。世代不一致が1件でもあれば「再取り込み待ち」を優先する（接続断より
            # 利用者の次の一手が具体的なため）。通知文言・グラフ側の計数は `chat_service._finalize`
            # （`_apply_graph_degraded`）が env のこの印から組む。
            if isinstance(env, dict):
                # 事前検索（`chat_service._dispatch`）が既に世代不一致を立てていれば、子の接続断で
                # 上書きしない（「再取り込み待ち」の方が利用者の次の一手が具体的＝優先する）。
                _era = (_graph_schema_era_error is not None
                        or agentic_search.GRAPH_REINGEST_ERROR_CODE in _mcp_error_codes
                        or env.get("graph_degraded") == agentic_search.GRAPH_REINGEST_ERROR_CODE)
                if _era:
                    env["graph_degraded"] = agentic_search.GRAPH_REINGEST_ERROR_CODE
                elif "graph_unavailable" in _mcp_error_codes:
                    env["graph_degraded"] = "graph_unavailable"
                if any(c in _mcp_error_codes for c in ("es_unavailable", "es_query_failed")):
                    env["limits"] = {**(env.get("limits") or {}), "backend_unavailable_fulltext": True}
                # MCP ツール結果のバイト予算（`mcp_server.py` の per-call クリップ／累計予算到達）を
                # 利用統計「打切りの内訳」へ合流させる（`_CONTEXT_WINDOW_EXCEEDED_CODE` 分岐が
                # 既に `total_budget_hit` を立てていれば上書きしない＝一度立った印を消さない）。
                if _mcp_tool_result_clipped:
                    env["limits"] = {**(env.get("limits") or {}),
                                     "tool_result_clipped": (env.get("limits") or {}).get(
                                         "tool_result_clipped", 0) + _mcp_tool_result_clipped}
                if _mcp_total_budget_hit:
                    env["limits"] = {**(env.get("limits") or {}), "total_budget_hit": True}
                # 同一クエリの重複実行の抑止（`mcp_server.py::_is_duplicate_tool_call`）の件数も
                # 同じ経路で合流させる（上の `tool_result_clipped` と同じ加算方式）。
                if _mcp_duplicate_tool_call:
                    env["limits"] = {**(env.get("limits") or {}),
                                     "duplicate_tool_call": (env.get("limits") or {}).get(
                                         "duplicate_tool_call", 0) + _mcp_duplicate_tool_call}
                # `run_tool()` 自身の内部切り詰め（grep ヒット上限・件数上限等・`mcp_server.py`
                # が `agentic_search._SEARCH_TRUNCATED_TOOLS`/`_BYTE_CLIP_TOOLS` と同じ判定で
                # 報告）も同じ経路で合流させる——API 経路の `_record_run_tool_limits` と同じ
                # 語彙（`search_truncated`）に揃える。
                if _mcp_search_truncated:
                    env["limits"] = {**(env.get("limits") or {}),
                                     "search_truncated": (env.get("limits") or {}).get(
                                         "search_truncated", 0) + _mcp_search_truncated}
                # MCP ツール呼び出し回数の上限到達（クイックを本当に速くする・変更D③）も同じ
                # 経路で合流させる（`total_budget_hit` と同じ bool 方式）。
                if _mcp_tool_calls_exhausted:
                    env["limits"] = {**(env.get("limits") or {}), "tool_calls_exhausted": True}
            # A2: troubleshoot は Codex が実際に引いた近傍を UI カードにする（_gather 由来を Codex 実調査由来で上書き）。
            _apply_codex_neighbors(env, mcp_neighbors, decision.get("lens") if decision else None)
            # turn.completed から拾った usage は Codex CLI の契約でセッション累計
            # （last_total_token_usage.total・`codex exec resume` は前回までの累計を復元してから加算する）。
            # 累計値そのものは env["codex_usage_total"] に必ず残す（次ターンの差分計算の元になる・
            # usage が取れなければ両方載せない）。resume が効いた（フォールバックしていない）ターンは、
            # 前ターンの累計（ctx.codex_usage_prev_total）との差分を answer.usage にする——session_id が
            # 今回の resume 先と一致する時だけ（新規セッション・フォールバック・prev 無し・session_id
            # 不一致はいずれも新規セッション相当として累計をそのまま使う＝そのセッションでの初回は
            # 累計と差分が一致する）。
            if codex_usage:
                env["codex_usage_total"] = {
                    "session_id": thread_id,
                    "input_tokens": codex_usage.get("input_tokens"),
                    "cached_input_tokens": codex_usage.get("cached_input_tokens"),
                    "output_tokens": codex_usage.get("output_tokens"),
                    "reasoning_output_tokens": codex_usage.get("reasoning_output_tokens"),
                }
                _prev_total = ctx.codex_usage_prev_total
                if (resume_sid and not _resume_fallback_happened and _prev_total
                        and _prev_total.get("session_id") == resume_sid):
                    env["usage"] = _usage_meta(
                        "codex", codex_usage.get("model"),
                        input_tokens=max(0, (codex_usage.get("input_tokens") or 0)
                                         - (_prev_total.get("input_tokens") or 0)),
                        cached_input_tokens=max(0, (codex_usage.get("cached_input_tokens") or 0)
                                                - (_prev_total.get("cached_input_tokens") or 0)),
                        output_tokens=max(0, (codex_usage.get("output_tokens") or 0)
                                          - (_prev_total.get("output_tokens") or 0)),
                        reasoning_output_tokens=max(0, (codex_usage.get("reasoning_output_tokens") or 0)
                                                   - (_prev_total.get("reasoning_output_tokens") or 0)),
                        is_local=codex_usage.get("is_local"))
                else:
                    env["usage"] = codex_usage
                # 差分計算／累計そのものの両方に同じ深さメタを載せる
                # （`_usage_depth_extra` はこのターンの `_reason`/`_base_reason` 確定時に計算済み）。
                env["usage"].update(_usage_depth_extra)
                # DEPTH-2 S3b/S6: 子スレッド（`spawn_agent`）の usage を加算する。`env["codex_usage_total"]`
                # （次ターンの差分計算の元）は**親のスナップショットのまま変えない**——子の usage は
                # 子スレッドのセッション累計であって親の累計とは別系統のため、ここへ混ぜると次ターンの
                # 差分計算が破綻する。合算は `env["usage"]`（このターンの表示・計上値）だけに行い、
                # 内訳（親／子／未取得件数）を別途残す。multi_agent 無効（`collab_tool_call` が
                # 一度も出ない実行）は `_child_usage_found`/`_child_usage_missing` が両方 0 の
                # まま＝このブロックは素通りする。
                # 計測の正本＝§2.8: 本体ターン（`turn.completed`）は境界イベントが無く巡（worker/
                # evaluator の spawn 単位）ごとの内訳を取れないため、巡別の `chat-round` は記録しない
                # （API/Ollama 経路の巡ループ・`providers/base.py::_agentic_run` とは異なる）——ここで
                # 合算する親＋子の usage 合計だけを、このターンの正本として `env["usage"]`/`sherpa.usage`
                # ログに残す。
                if _child_usage_found or _child_usage_missing:
                    # DEPTH-2 S3b是正: 親分の内訳は `codex_usage`（セッション累計）ではなく
                    # `env["usage"]`（直前の分岐で resume 差分 or 累計そのものに確定済みの
                    # このターンの計上値）から作る——resume ターンでは `codex_usage` に前ターン分が
                    # 混入しており、そのまま使うと内訳の親分だけ合計より過大になる。
                    _parent_only = {k: env["usage"].get(k) or 0 for k in _CHILD_USAGE_KEYS}
                    env["codex_usage_children"] = {
                        "found": _child_usage_found, "missing": _child_usage_missing,
                        **_child_usage_totals,
                    }
                    env["usage"]["codex_usage_breakdown"] = {
                        "parent": _parent_only, "children": dict(_child_usage_totals),
                        "children_found": _child_usage_found, "children_missing": _child_usage_missing,
                    }
                    for _k in _CHILD_USAGE_KEYS:
                        env["usage"][_k] = (env["usage"].get(_k) or 0) + _child_usage_totals.get(_k, 0)
                # Codex 経路も `sherpa.usage` ログ 1 行（kind=chat・深さ・推論レベル付き）を出す。
                _log_chat_usage(env["usage"], time.monotonic() - _turn_t0, ctx.world)
            # 捕捉した session/thread id を env に載せる（chat_service が `store.set_session_id` で永続化・
            # 次ターンの resume 判定に使う）。ゲートは `_persist_session` 単独ではなく
            # `_session_persistence_enabled`（=conversation_id あり **かつ** サンドボックス有効）を使う。
            # `SHERPA_CODEX_SANDBOX=0`（緊急避難経路）は常に `--ephemeral` 実行＝ディスクに残らない使い捨て
            # thread_id なので、ここで DB に保存すると次回サンドボックス復帰後の resume が必ず失敗する
            # （その thread_id は永遠に resume 不能）。conversation_id 無しの直接呼出しでも当然載せない。
            if _session_persistence_enabled and thread_id:
                env["codex_session_id"] = thread_id
            # Feature A/C: Codex がファイルを作成した場合は env に記録（chat_service が contains_personal を立てる）。
            # HIGH 3 fix: files/ 外への書き込みも含めて codex_wrote_files フラグを立てる。
            if codex_created_files or _any_new_ws:
                env["codex_wrote_files"] = [Path(f).name for f in codex_created_files] or True
            # P1-c: 台帳登録に成功したファイルを UI の「作成したファイル」カード用に env へ載せる
            # （既存の /workspace/files DL API を再利用・rel_path は同名衝突回避後の最終名）。
            if _created_file_rows:
                env["created_files"] = [
                    {"name": r["rel_path"], "download_url": f"/workspace/files/{r['id']}/download"}
                    for r in _created_file_rows
                ]
            if answer:
                # 直読した資料は MCP の結果に載らず
                # env["sources"] に反映されない——回答末尾の「参照した資料:」ブロックを解析し、read 系
                # MCP ツール引数から拾った doc_id（参照ブロックの記載漏れの補完・出現順で後ろに合流）と
                # 合わせて機械検証（実在・文書種別・scope・秘匿名除外）を通ったものだけを sources の
                # 先頭へ足す（`_gather` 由来の既存 sources は後ろに残す・doc_id 重複は除外）。
                # verified が0件なら参照ブロックの記載を消しても出典が何も出ない＝本文はそのまま残す
                # （記載が消えて何も出典に出ないより、本文に根拠パスが残るほうを優先）。
                _body, _listed_lines = parse_referenced_doc_lines(answer)
                _ref_candidates: list = list(_listed_lines)
                _ref_seen: set[str] = set()
                for _r in _mcp_read_docs:
                    if _r and _r not in _ref_seen:
                        _ref_seen.add(_r)
                        _ref_candidates.append(_r)
                # `xlsx_sheets`（シート一覧のみ）の doc_id も参照候補（sources）
                # には合流させる——実在する資料を出典から隠す理由はない。ただし「参照した資料:」
                # にも書かれておらず、本文精読ツール（`_mcp_read_docs`）でも読まれていない doc_id
                # は、シート一覧を見ただけで根拠ゲート（sources_verified）に数えない——ここまでの
                # 候補（参照ブロック＋本文精読ツール）だけで確定する「精読済み」集合を先に確定させ、
                # `xlsx_sheets` を足した後の verified との差分（`_listed_only_ids`）として区別する。
                _verified_before_listed = set(verified_referenced_docs(_ref_candidates, ctx.world, sp))
                for _r in _mcp_listed_docs:
                    if _r and _r not in _ref_seen:
                        _ref_seen.add(_r)
                        _ref_candidates.append(_r)
                _verified_refs = verified_referenced_docs(_ref_candidates, ctx.world, sp)
                _listed_only_ids = set(_verified_refs) - _verified_before_listed
                if _verified_refs and ctx.make_sources:
                    _ref_sources, _ = _verified_sources(ctx.make_sources, set(_verified_refs), ctx.world, sp)
                    # 参照ブロックの記載順（→ MCP 引数の順）に並べ直し、既存（_gather 由来）と重複する
                    # 資料は先頭側（参照した資料）を残す。
                    _order = {d: i for i, d in enumerate(_verified_refs)}
                    _ref_sources = sorted(_ref_sources, key=lambda s: _order.get(s.get("doc_id"), len(_order)))
                    _ref_ids = {s.get("doc_id") for s in _ref_sources}
                    env["sources"] = _ref_sources + [s for s in (env.get("sources") or [])
                                                     if s.get("doc_id") not in _ref_ids]
                    # 実際に開いて根拠にした資料＝API 経路の「精読済み」と同じ意味＝出典の 2 区分
                    # （根拠／参考）に載せる（画面・共有・改善ログは既存の sources_verified の扱いのまま）。
                    # `xlsx_sheets` だけで到達した doc_id（`_listed_only_ids`）は除く。
                    env["sources_verified"] = sorted(_ref_ids - _listed_only_ids)
                env["codex_referenced_docs"] = {"listed": len(_ref_candidates), "verified": len(_verified_refs)}
                env["headline"] = _body if (_verified_refs and _body.strip()) else answer   # 空本文には差し替えない
                if _schema_on:
                    # DEPTH-2 S1（§2.5）: v2 のときだけ主張配列を持つ（v1 は常に空リスト＝
                    # `_pick_structured_claims` が既に `_schema_v2` で分岐済み）。区分と理由コードを
                    # envelope にも載せる——共有（sanitized share）・監査で消えないようにする
                    # （`sherpa/store/shares.py::_safe_claim` が既知フィールドのみで再構築する）。
                    _claims = _pick_structured_claims()
                    if _claims:
                        # S1b: API 経路と同じ最終ゲート（§0(b)）を Codex にも掛ける——確定主張の
                        # うち必須の根拠種別を欠くものを推定へ格下げし、ターン単位の不足を
                        # headline 冒頭に前置する（本文は書き換えない・作成系は成果物の中身に
                        # 混ざるため注記を出さない＝API 側 `lens != "author"` と同じ規律）。
                        _claims, _gate_meta, _gate_missing, _gate_unavailable = _apply_codex_evidence_gate(
                            _claims, lens=decision["lens"], world=ctx.world, scope_paths=sp,
                            layer=layer_mod.effective_layer(ctx.scope_meta, decision["lens"]),
                            personal_facts=ctx.personal_facts)
                        # 台帳突合（§3「claims は台帳からの投影」）は根拠種別ゲートの後に適用する
                        # ——両方の格下げが独立に効く（種別は揃っているが台帳に無い参照、種別が
                        # 欠けている参照、のどちらも確定を維持しない）。
                        _claims, _claims_ledger_check = _claims_vs_ledger(
                            _claims,
                            investigation_ledger.load_ledger(_investigation_dir)
                            if mcp and _investigation_dir is not None
                            else investigation_ledger.LedgerSnapshot(manifest=None, items={}, invalid_ids=()),
                            manifest_file_exists=(
                                (_investigation_dir / "manifest.json").is_file()
                                if mcp and _investigation_dir is not None else False))
                        env.setdefault("data", {})["claims"] = _claims
                        env["data"]["evidence_gate"] = _gate_meta
                        if _investigation_verdict is not None:
                            env["investigation"]["claims_check"] = _claims_ledger_check
                        env["limits"] = {**(env.get("limits") or {}),
                                         "claims_unmatched": _claims_ledger_check.get("downgraded", 0) > 0}
                        if decision["lens"] != "author":
                            _gate_note = _evidence_gate_note(_gate_missing, _gate_unavailable)
                            # 種別ゲート・台帳突合のどちらかで confirmed が1件でも格下げされたら
                            # 同じ注記を出す（二重には付けない・`_gate_missing` があるターンは
                            # その注記が既に格下げを示唆しているため重ねない）。
                            _any_claims_demoted = (
                                _gate_meta["demoted"] > 0
                                or _claims_ledger_check.get("downgraded", 0) > 0)
                            if not _gate_missing and _any_claims_demoted:
                                _gate_note = _DEMOTED_CLAIMS_NOTE + _gate_note
                            if _gate_note:
                                env["headline"] = f"{_gate_note}\n\n{env['headline']}"
                # 実際に回答を生成できたターン＝`_dispatch` がツール遮断時に立てた
                # `agentic_failure`（`agentic_search.tools_blocked_env`）が残っていれば消す
                # （Codex は遮断状態を見ずに調査を続行し得るため、結果が出た後の事実で上書きする）。
                env.pop("agentic_failure", None)
                # 自動継続を尽くしてもなお進行中の宣言文（「次に○○します」等）がそのまま headline に
                # 残ったターン——本文は書き換えない（`answer` は既存どおりそのまま使う）。`_codex_stopped_early`
                # だけを根拠に envelope へ印を付け、chat_service._finalize が予算到達時の途中結果・出典0件時の案内と同形式
                # （headline 直下の独立注記＋案内ボタン）で UI に出す（`stop_reason` の閉じた語彙とは
                # 無関係の別マーカー＝Codex CLI はここを経由しない agentic_search とは別の実行系のため）。
                if _codex_stopped_early:
                    env["codex_stopped_early"] = True
                yield _node("codex", "think", "Codex が調べる",
                            "調べて回答をまとめました" if ran else "回答をまとめました", "done")
            elif _codex_silent_failure:
                # 利用統計の終了理由分布（`stop_kind_mod.resolve`）がこの分岐を
                # `codex_silent` と判定できるよう印を立てる（値の意味づけは chat_service 側）。
                env["codex_silent_failure"] = True
                # `_gather` が組み立てた決定的回答をそのまま返さない＝利用者に「AI が答えていない」
                # ことが伝わるよう `_UnwiredProvider` と同じ文体の正直な文言に上書きする
                # （summary/sources は `_gather` の実結果のまま残すが、sources が空なら data も
                # `{}` へ揃える＝`chat_service._no_genuine_results` の honest failure 規約と一致させ、
                # 通常の0件検索結果と誤認されて retry_hints・確定文言が付かないようにする）。
                # 同じ無出力失敗はプロキシ/CA 証明書の不備・sandbox の起動失敗・
                # CLI 自体のクラッシュでも起きるため、認証だけに断定しない（閉域ではむしろ
                # プロキシ/ネットワーク要因の方が現実的で、認証と決め打つと現場を誤誘導する）。
                # 観測事実（応答を返す前に終了）を
                # 述べたうえで、考えられる原因を複数挙げる（断定しない）。判別材料（returncode）は
                # 意味が伝わらない利用者向け本文には出さず、ログにだけ残す。stderr は現状 DEVNULL で破棄
                # している（先頭行を出すには stdout/stderr 同時 PIPE 読み取りが要り、デッドロック回避の
                # 追加実装が必要になるため今回のスコープでは見送り＝秘密が混ざり得る文言を利用者へ出さない
                # という制約自体は満たしたまま）。
                # §2-10: `turn.failed`／`error` で閉じた attempt（agent_message 無し）は
                # 「回答を返せずに終了しました」——認証/ネットワーク以外にスキーマ違反
                # （`invalid_json_schema` 等）も原因になり得るため、既存の無出力失敗と文言を分ける。
                _reason = ("回答を返せずに終了しました" if _turn_failed
                          else "応答を返す前に終了しました")
                if _turn_failed_code:
                    # 閉集合ではない生の診断コード（統計・分類には使わない・管理者向け補助情報）。
                    env["codex_error_code"] = _turn_failed_code
                if _turn_failed_code == _CONTEXT_WINDOW_EXCEEDED_CODE:
                    # 1回の調査で集めたツール結果だけで文脈枠を使い切った（会話履歴の蓄積では
                    # ない）——認証/ネットワーク不調と混同させず、範囲を絞る具体的な次の一手を示す。
                    env["headline"] = (
                        "調べる範囲が広すぎて、今回は回答をまとめられませんでした。"
                        "範囲（フォルダ）を絞るか、質問を分けてやり直してください。"
                    )
                    env["limits"] = {**(env.get("limits") or {}), "total_budget_hit": True}
                else:
                    env["headline"] = (
                        f"Codex に接続できませんでした（Codex CLI が{_reason}）。考えられる原因はいくつかあります: "
                        "認証が設定されていない（`codex login`）／プロキシや CA 証明書などのネットワーク設定が"
                        "不足している／サンドボックスの起動に失敗した／Codex CLI 自体が異常終了した、のいずれかです。"
                        "管理者にログの確認を依頼してください。詳細は管理者向けの Codex ログ（codex.log）を参照してください。"
                    )
                if not env.get("sources"):
                    env["data"] = {}
                _log.warning(
                    "codex silent failure: returncode=%s turn_failed=%s code=%s conv=%s uid=%s",
                    attempt_returncode, _turn_failed, _turn_failed_code,
                    ctx.conversation_id, uid)
                yield _node("codex", "think", "Codex が調べる",
                            "応答がありませんでした（原因未特定・決定的回答は使いません）", "done")
            else:
                # 本文（answer）は空だが、上の `_codex_silent_failure`（応答を1行も返さない完全な
                # 沈黙）には該当しないケース——command_execution 等は実行できたが結論の
                # agent_message が無いまま（利用者の明示停止等で）打ち切られた。silent failure 分岐は
                # headline 自体で「Codex に接続できませんでした」と既に告知しているため対象外のまま、
                # ここは env["headline"] が `_gather` の headline のままの場合にも注記を出す
                # （presearch を省いたターンは決定的回答ではなく `_NO_PRESEARCH_HEADLINE` のまま）。
                # `_stream_error` は Popen 完走前（authoring 設定書き込み等）の例外でも立つため、
                # `attempt_returncode is None` のまま `_codex_silent_failure` が計算されずここに落ちても
                # 終了理由の分布から漏らさない（`stop_kind.resolve` の codex_silent 判定に必要な印）。
                if _stream_error:
                    env["codex_silent_failure"] = True
                if _codex_stopped_early:
                    env["codex_stopped_early"] = True
                # presearch を省いたターンは決定的回答へ「切替」ようが無い（そもそも下調べを
                # 実行していない）——決定的回答を使ったかのような文言にしない。
                _no_answer_detail = ("（未応答のため回答を出せませんでした）" if _skip_presearch
                                     else "（未応答のため決定的回答に切替）")
                yield _node("codex", "think", "Codex が調べる", _no_answer_detail, "done")
            if _created_files_failed:
                # headline がどの分岐（answer/silent_failure/未応答）で組み立てられていても、
                # 保存できなかった成果物がある事実は一律に伝える。
                env["headline"] = f"{env['headline']}\n\n{_CREATED_FILES_FAILURE_NOTE}"
            yield {"type": "answer_delta", "text": env["headline"]}   # Codex は一括→フロントで段階表示
            yield {"type": "_result", "env": env, "decision": decision}
        finally:
            # RV 中-1（2026-09-22 2巡目是正）: 台帳の退避・削除が終わるまで会話ロックを保持する。
            # ロックを先に解放すると、同じ会話の「続き」ターンが直後にロックを取得して復元処理
            # （`.tmp` 作成直後の retired-investigation 読み取り）へ進み、未作成またはコピー途中の
            # 退避先を参照しうる（単一 worker でもストリームはスレッドプールで並行するため、
            # プロセス内で競合しうる）。内側 `try/finally` で、退避／run_dir 削除の途中で例外が
            # 起きても最後に必ずロックを解放する。
            try:
                if run_dir is not None:
                    # 調査台帳の退避（正典§3「置き場所」/§4「寿命」）: run_dir 削除より前・完了せず
                    # ターンが終わった台帳だけ永続領域へコピーする。RV 高-3（2026-09-22）:
                    # `_investigation_verdict`（本体ループが正常に完走したときだけ埋まる）には
                    # 頼らない——`_attempt()` 内の yield で generator が close された場合、判定行
                    # に到達せず run_dir が消えると未完了台帳が失われる。RV 中-3（3巡目是正）:
                    # 通常終了時は既に env 構築の直前で `_retire_investigation_ledger` を実行済み
                    # （`_investigation_retire_done`）——ここは切断・例外で先に到達しなかった場合
                    # だけのフォールバックで、二重には実行しない。
                    if (_investigation_dir is not None and _ledger_home is not None
                            and not _investigation_retire_done):
                        _retire_investigation_ledger(
                            _investigation_dir, _ledger_home, required_extra=_ledger_required_extra)
                    _release_active_run_dir(run_dir)
                    if _created_files_failed:
                        # 保存に失敗した成果物がある run_dir は削除せず回収用に残す
                        # （`_cleanup_stale_run_dirs` の24時間しきい値で最終的に掃除される）。
                        _log.warning(
                            "codex run dir kept for recovery due to created-file save failure: %s",
                            run_dir.name)
                    else:
                        _remove_dir_best_effort(run_dir)
            finally:
                if _conv_lock_acquired:
                    _conv_lock.release()
