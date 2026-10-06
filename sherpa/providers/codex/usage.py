"""トークン使用量の集計（親・子スレッド）と、MCP の予算 env・サイドカーの読取、env の整数解析。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import json
from pathlib import Path

from ... import agentic_search, metering
from ... import depth_profile as depth_profile_mod
from ...env_int import env_int
from ..base import _usage_meta
from .activity import _is_child_session_meta


# Codex 経路の 1 件あたりのツール結果の上限（バイト）。管理画面の基準値と min() で結ぶ（256KiB 級を渡すと文脈枠を使い切るため 64KiB に固定）。
_CODEX_MCP_TOOL_BUDGET_CEILING_BYTES = 64 * 1024


def _resolve_mcp_budget(system_settings: dict | None, depth_profile: str | None = None) -> dict[str, str]:
    """MCP 子プロセスへ渡す `SHERPA_MCP_TOOL_BUDGET_BYTES`／`_MAX_HITS`/`_WINDOW_CAP` を1回だけ解決する。
    1件あたりのバイト予算は `agentic_search.effective_tool_result_max_bytes` の実効値と `_CODEX_MCP_TOOL_BUDGET_CEILING_BYTES` の min()。累計のバイト予算は持たない。
    hits/window は API 経路と同じ `depth_profile.scaled_ratio(depth_profile.effective_base(...), profile, abs_max=...)` で深さ連動込みの実効値を解決する。`depth_profile` は `ctx.scope_meta.get("depth_profile")` の値（`None`＝標準）。
    ツール呼び出し回数の上限は渡さない。
    """
    sysset = system_settings
    if sysset is None:
        try:
            from ... import store as _store
            sysset = _store.get_system_settings()
        except Exception:
            sysset = {}
    per_result = agentic_search.effective_tool_result_max_bytes(sysset)
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


def _usage_from_turn_completed(event: dict, model: str | None, *, codex_model_provider: str | None = None,
                               system_settings: dict | None = None) -> dict | None:
    """`codex exec --json` の `turn.completed` イベントから usage を取り出す。usage が無い/型不正なら None。
    `codex_model_provider`/`system_settings`: 呼び出し元が `"ollama"`/`"openai"` と設定スナップショットを渡し、`agent_constructs.is_local` の4値判定へ委ねる（Codex は常に `provider_id="codex"` を名乗るため実際の接続先はここでしか分からない）。
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
                       cache_write_tokens=metering.cache_write_from_usage(u, u.get("input_tokens_details")),
                       is_local=agent_constructs.is_local("codex", codex_model_provider=codex_model_provider,
                                                          system_settings=system_settings))


def _accumulate_codex_usage(prev: dict | None, new: dict | None) -> dict | None:
    """自動継続で attempt をまたいだときの usage は最新の snapshot を採用する（足し合わせない）。`turn.completed.usage` はセッション累計で、継続 attempt の値は前 attempt の分を含むため。`None` の attempt は無視する。"""
    return prev if new is None else new


def _read_mcp_sidecar(path: Path) -> tuple[list, list, dict | None, list, dict]:
    """`mcp_server.py` が書いたサイドカー（子エージェント＝`spawn_agent` された worker/evaluator が読んだ doc_id・ask_user の質問）を読む。子の MCP 呼出は親の `--json` に現れないため、これが唯一の観測経路。
    サイドカーが無い／壊れているときは fail-open（空リスト／None を返すだけで例外は出さない）。
    戻り値: `(read_doc_ids, listed_doc_ids, ask_user_question_or_none, error_codes, limits)`。
    `ask_user_question` は最初の1件だけ。`error_codes` は子が受け取った障害の閉じたコード（`mcp_server._SIDECAR_ERROR_CODES`・出現順・重複なし）。
    `limits` は `{"kind": "limit", "field": ...}` を集計した `{"tool_result_clipped": 件数, "total_budget_hit": bool, "duplicate_tool_call": 件数, "search_truncated": 件数, "tool_calls_exhausted": bool}`（利用統計「打切りの内訳」へ合流させる）。
    """
    reads: list = []
    listed: list = []
    ask: dict | None = None
    error_codes: list = []
    limits = {"tool_result_clipped": 0, "total_budget_hit": False, "duplicate_tool_call": 0,
             "search_truncated": 0, "tool_calls_exhausted": False, "coverage_write_failed": 0}
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
                    elif field == "coverage_write_failed":
                        limits["coverage_write_failed"] += 1
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError は行の読取自体で起きうる。ここまでに集めた分は返す（部分的な fail-open）。
        pass
    return reads, listed, ask, error_codes, limits


_CHILD_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
# 4 種とは別に持つキャッシュ書き込み量の欄（返されなければ None＝不明）。
CACHE_WRITE_KEY = "cache_write_tokens"


def _collect_child_token_usage(codex_home: Path, child_thread_ids: set,
                                parent_thread_id: str | None = None,
                                min_mtime: float | None = None) -> tuple[dict, int, int, int]:
    """`spawn_agent` した子スレッドの usage を、子ごとの session JSONL（`codex_home/sessions/**/*.jsonl`）から集める。親の `turn.completed.usage` には子の分が含まれない。
    「起動を検出した」ことと「usage を読めた」ことは別に扱う。
    子の検出（`detected_ids`）は2通りの和集合: 
    (a) 新形式: 先頭行 `session_meta.payload.parent_thread_id` が親 thread id と一致し、`thread_source == "subagent"`、かつ mtime が `min_mtime` 以降（resume で前ターンの子を再計上しないため）。
    (b) 旧形式: `session_meta.payload.id` が `spawn_agent` の `collab_tool_call` から捕捉した `child_thread_ids` に含まれる。
    検出した子のうち最後の `token_count`（`payload.info.total_token_usage`）があるものだけ `found_ids` に入れ、usage を1子につき1回だけ合算する。検出できたのに usage が取れなかった id（rollout が見つからない旧形式 id を含む）は `missing` に残す（推定で埋めない）。壊れた JSONL・欠損・存在しない codex_home は fail-open。
    戻り値: `(totals, found, missing, detected)`（`detected` は `found + missing` と一致）。
    `totals[CACHE_WRITE_KEY]` は usage を読めた子がすべてキャッシュ書き込み量を返したときだけ入れ、1 体でも返さない（または読めた子が 0 体）なら項目ごと載せない（不明）。
    """
    totals = dict.fromkeys(_CHILD_USAGE_KEYS, 0)
    if not child_thread_ids and not parent_thread_id:
        return totals, 0, 0, 0
    cache_write: int | None = 0
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
                # 子判定は `activity.py::_is_child_session_meta` の1箇所だけに持つ（二重実装しない）。
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
                    cache_write = metering.sum_cache_write(
                        cache_write, metering.cache_write_from_usage(
                            last_usage, last_usage.get("input_tokens_details")))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    if found_ids and cache_write is not None:
        totals[CACHE_WRITE_KEY] = cache_write
    all_detected = detected_ids | child_thread_ids  # rollout 自体が見つからなかった旧形式 id も含める
    return totals, len(found_ids), len(all_detected - found_ids), len(all_detected)
