"""`CodexProvider`（Codex を頭脳にする exec 核）。

Codex CLI サブプロセスの起動・思考イベントへの変換・実行ごとの作業領域管理・headline/progress 判定をまとめる。
同時実行は uid 単位で直列化しない（実行ごとに専用の作業領域 `authoring/run-<乱数>` を割り当てる）。
`CodexProvider.run`/`_run_authoring` は分割しない（SSE 生成器の try/finally が唯一のクリーンアップ保証であり、`'ws_authoring' in dir()` や last-message tempfile の `unlink` 2箇所もこのフレームに依存する）。
`_gather` は `_run_authoring` 内でのみ `from sherpa import agents as _facade` で遅延 import し `_facade._gather(ctx)` と実行時解決する（差し替えを効かせる・循環 import 回避）。
設計: docs/design/codex.md「1ターンの流れ」
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
import tempfile
import time
import threading
from pathlib import Path
from typing import Iterator

from ... import agentic_search, codex_agents_md, codex_skills, model_catalog
from ... import depth_profile as depth_profile_mod
from ... import investigation_ledger
from ... import investigation_state
from ... import layer as layer_mod
from ... import workspace_limits
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

# skills_base。`sherpa/` 配下を指す（本モジュールは `sherpa/providers/codex/` にあるため `parents[2]`）。
_SKILLS_BASE = Path(__file__).resolve().parents[2] / "skills_base"

# `--output-schema` に渡す固定スキーマファイル（同じディレクトリに同梱）。v2 は v1 の3キーに `claims`（確定/推定/不明の主張配列）を足した版で、`SHERPA_CODEX_OUTPUT_SCHEMA` の値で選ぶ。
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


_MCP_SIDECAR_NAME = ".mcp_sidecar.jsonl"  # sandbox 有効時は codex_home 配下（run_dir の外）
# 資料作成（author）専用の推論の強さ。通常レンズの基準値（管理画面）とは別軸。
_REASONING_AUTHOR = "medium"
# MCP 付き Codex 経路で決まった手順の下調べ（`_gather` の `ctx.dispatch`）を省くレンズ。impact は省かない（グラフでたどる影響一覧は Codex のツールでは作れず、回答と並べて表示するため）。
_PRESEARCH_SKIP_LENSES = frozenset({"qa", "troubleshoot", "author"})
# `codex_error_info` がこの値のとき、ツール結果だけで文脈枠を使い切った。利用者向け文言は「範囲を絞れ」を案内し、終了理由は `budget`（打切りの内訳）に数える。
_CONTEXT_WINDOW_EXCEEDED_CODE = "context_window_exceeded"
# `codex_error_info` を持たない CLI 版でも文脈枠超過を見分ける分類。`error.message` は保存もログ出力もせず、この判定にだけ使う（資料名・抜粋が混ざり得るため）。
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
# モデルの文脈窓（`model_context_window`）は Codex CLI に渡さず、CLI 自身の判断に任せる（Sherpa 側で窓を判定・登録・上書きしない）。


# Codex 経路の 1 件あたりのツール結果の上限（バイト）。管理画面の基準値と min() で結ぶ（256KiB 級を渡すと文脈枠を使い切るため 64KiB に固定）。
_CODEX_MCP_TOOL_BUDGET_CEILING_BYTES = 64 * 1024


def _resolve_mcp_budget_env(system_settings: dict | None, model: str | None,
                            ollama_base_url: str | None, depth_profile: str | None = None) -> dict[str, str]:
    """`_resolve_mcp_budget` のラッパー（env dict だけを使う呼び出し元用）。"""
    return _resolve_mcp_budget(system_settings, model, ollama_base_url, depth_profile)


def _resolve_mcp_budget(system_settings: dict | None, model: str | None,
                        ollama_base_url: str | None, depth_profile: str | None = None
                        ) -> dict[str, str]:
    """MCP 子プロセスへ渡す `SHERPA_MCP_TOOL_BUDGET_BYTES`／`_MAX_HITS`/`_WINDOW_CAP` を1回だけ解決する。
    1件あたりのバイト予算は `agentic_search.effective_tool_result_max_bytes` の実効値と `_CODEX_MCP_TOOL_BUDGET_CEILING_BYTES` の min()。`provider` は Codex(Ollama) のときだけ "ollama"、それ以外は "openai"。累計のバイト予算は持たない。
    hits/window は API 経路と同じ `depth_profile.scaled_ratio(depth_profile.effective_base(...), profile, abs_max=...)` で深さ連動込みの実効値を解決する。`depth_profile` は `ctx.scope_meta.get("depth_profile")` の値（`None`＝標準）。
    ツール呼び出し回数の上限は渡さない。
    """
    provider = "ollama" if ollama_base_url is not None else "openai"
    sysset = system_settings
    if sysset is None:
        try:
            from ... import store as _store
            sysset = _store.get_system_settings()
        except Exception:
            sysset = {}
    per_result = agentic_search.effective_tool_result_max_bytes(
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
    """失敗ログにフルパス（uid を含む）を出さず、`run_dir` からの相対部分だけを run_dir の識別子（`run-<乱数>`）に付けて返す。相対化できなければ識別子だけを返す。"""
    try:
        rel = Path(fp).resolve().relative_to(run_dir.resolve())
        return f"{run_dir.name}/{rel}"
    except (OSError, ValueError):
        return run_dir.name


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
        # UnicodeDecodeError は行の読取自体で起きうる。ここまでに集めた分は返す（部分的な fail-open）。
        pass
    return reads, listed, ask, error_codes, limits


_CHILD_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


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
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    all_detected = detected_ids | child_thread_ids  # rollout 自体が見つからなかった旧形式 id も含める
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


_SESSION_REAP_ATTEMPTS = 5  # 固定回数（無限リトライにしない）
_SESSION_REAP_INTERVAL_S = 0.05  # 数十ミリ秒


_STARTUP_STDERR_MAX_BYTES = 4096
_STARTUP_STDERR_MAX_LINES = 20


def _log_startup_stderr(f, returncode, got_any_line: bool, conv, uid) -> None:
    """Codex が `--json` のイベントを1件も出さずに異常終了したときだけ、stderr の先頭を伏せ字にかけてログに残す。正常終了・イベントが出た後の失敗では読まない。どちらでもファイルは閉じて捨てる。"""
    try:
        if returncode not in (None, 0) and not got_any_line:
            f.seek(0)
            head = f.read(_STARTUP_STDERR_MAX_BYTES).decode("utf-8", "replace")
            lines = [ln.rstrip() for ln in head.splitlines() if ln.strip()][:_STARTUP_STDERR_MAX_LINES]
            if lines:
                _log.warning("codex startup failure: returncode=%s conv=%s uid=%s stderr=\n%s",
                             returncode, conv, uid, agentic_search._redact("\n".join(lines)))
    except Exception:
        pass
    finally:
        try:
            f.close()
        except Exception:
            pass


# モデルの思考の区切りの制御記号（例 `<|channel|>`・`<think>`）。表示の1行要約からだけ外す。
_CONTROL_MARKER_RE = re.compile(r"<\|?/?[A-Za-z_]{1,24}\|?>")


def _strip_control_markers(text: str) -> str:
    return _CONTROL_MARKER_RE.sub("", text)

def _kill_session_fallback_killpg(sid: int, reason: str) -> None:
    """pidfd/`/proc` が使えない環境（macOS 等）向けの縮退経路。`_kill_session` と同じ前提（reap する前に呼ぶ）のもと `os.killpg` で group ごと SIGKILL する。setpgid で group を抜けた孤児には届かない縮退版であることを毎回 1 回だけ警告する。
    `start_new_session=True` なので pgid == sid。getpgid は使わない（macOS ではゾンビのリーダーで ESRCH になるため）。
    """
    try:
        os.killpg(sid, signal.SIGKILL)
    except ProcessLookupError:
        return  # group にもう誰もいない＝回収するものが無い
    except Exception as exc:
        _log.warning(
            "codex session reap: killpg フォールバックにも失敗しました（%s・%s）sid=%s",
            reason, type(exc).__name__, sid)
        return
    _log.warning(
        "codex session reap: pidfd/proc 非対応環境のため killpg フォールバックへ縮退しました"
        "（%s・setpgid で group を抜けた子は回収できません）sid=%s", reason, sid)


def _kill_session(sid: int) -> None:
    """`sid` を session id に持つプロセスを、残りが無くなるまで数回 SIGKILL する。呼び出し側は `start_new_session=True` で起動した Popen の pid を渡す。
    プロセスグループでなくセッションで回収するのは、サンドボックス内の子が `setpgid(0,0)` で別グループへ移ると `_killpg` が届かないため（SIGKILL だけが届く）。
    呼び出し側は sid の元の Popen を `wait()` で reap する前に呼ぶこと（reap 後は pid が再利用されうる）。sid 自身（リーダー）は対象から除く。
    判定から送信までの pid 再利用による誤爆は、判定前に確保した pidfd 越しに `pidfd_send_signal` で送って防ぐ。pidfd や `/proc` が使えない環境は `_kill_session_fallback_killpg` へ縮退する。自分自身とこのセッションに属さないプロセスには触れない。
    """
    if sid <= 0:
        return
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        _kill_session_fallback_killpg(sid, "pidfd 未対応")
        return
    my_pid = os.getpid()
    try:
        for _ in range(_SESSION_REAP_ATTEMPTS):
            try:
                candidates = [e for e in os.listdir("/proc") if e.isdigit()]
            except OSError:
                _kill_session_fallback_killpg(sid, "/proc 未対応")
                return
            matched = 0
            for entry in candidates:
                pid = int(entry)
                # sid 自身（リーダー）は呼び出し側の `_killpg`/`proc.wait()` が担当するため対象から除く。
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
                    continue  # ESRCH 等（走査中に対象が消えた）は通常経路——次候補へ
                try:
                    try:
                        with open(f"/proc/{entry}/stat", "r", errors="replace") as f:
                            raw = f.read()
                    except OSError:
                        continue  # 走査中にプロセスが消えるのは通常経路
                    # comm フィールドは括弧内で空白/括弧を含み得るため、最後の ')' を境に固定オフセットで読む。境より後ろ: state ppid pgrp session ...
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
    """途中停止: 別スレッドで stop_event を監視し、立ったら `_killpg` で子プロセスごと殺して stdout を EOF にし、ブロック中の read を解放する。`stop_event` が None でも常に起動する（下の pipe 閉じ役のため）。プロセスが自然終了したらスレッドも自分で抜ける（daemon・呼び出し側は join 不要）。
    終了検知に `proc.poll()` は使わない（reap してしまい、`_attempt` の finally の「`_kill_session` → `proc.wait()`」順序が崩れるため）。`os.waitid(..., WNOWAIT)` で reap せずに確認する。
    リーダーの終了/停止を検知したら reap する前に `_kill_session` を呼ぶ（別グループの子が pipe を握ったまま残ると read ループが EOF にならないため）。
    `reap_lock`/`reaped`: finally 側の reap とこのスレッドの判定を直列化し、`reaped["done"]` が立っている（または waitid が ECHILD）なら何もせず戻る（reap 後の pid 再利用で無関係なプロセスを殺さないため）。
    """
    # macOS の CPython には os.waitid が無いため、停止操作だけを見る（終了後の片付けは finally 側の `_kill_session`）。
    _can_peek_exit = hasattr(os, "waitid")

    def _watch(_proc=proc, _ev=stop_event, _lock=reap_lock, _reaped=reaped):
        while True:
            with _lock:
                if _reaped["done"]:
                    return
                exited = False
                if _can_peek_exit:
                    try:
                        exited = os.waitid(
                            os.P_PID, _proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
                    except ChildProcessError:
                        return  # 既に finally で reap 済み（ECHILD）＝ sid には触れない
                if exited:
                    _kill_session(_proc.pid)  # pipe を握ったまま残る子を片付けて EOF にする
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


def _spawn_wall_clock_watcher(proc, deadline_mono: float, reap_lock, reaped, hit_state: dict) -> "threading.Thread":
    """1ターン全体（自動継続を含む）の壁時計上限。`deadline_mono`（`time.monotonic()` 基準）に達したら、停止操作と同じ手順（`_killpg` → `_kill_session`）でこの attempt のプロセスを打ち切る。
    `reap_lock`/`reaped` を `_attempt` の finally・`_spawn_stop_watcher` と共有し、二重キルや reap 後の pid 再利用への誤送信を避ける。
    `hit_state["hit"]`: この関数が実際に打ち切った時だけ True を書く。attempt をまたいで同じ dict を渡す。
    """
    def _watch(_proc=proc, _deadline=deadline_mono, _lock=reap_lock, _reaped=reaped, _hit=hit_state):
        while True:
            with _lock:
                if _reaped["done"]:
                    return
            remaining = _deadline - time.monotonic()
            if remaining <= 0:
                with _lock:
                    if not _reaped["done"]:
                        _hit["hit"] = True
                        _killpg(_proc)
                        _kill_session(_proc.pid)
                return
            time.sleep(min(remaining, 0.3))
    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    return t


_LAST_MESSAGE_MAX_BYTES = 16 * 1024 * 1024  # 最終メッセージの保険読取のメモリ保護（回答の長さを切る目的ではない）


def _read_last_message_fallback(path: Path) -> str | None:
    """`-o <path>` で Codex が書く最終メッセージファイルを読む（`--json` の `agent_message` 抽出が空だった時の保険）。無い/空/読取失敗は None。ファイルの削除は呼び出し側の責務。
    `.tmp/` は Codex の書込対象のため、`O_NOFOLLOW` で symlink を拒否し、通常ファイルのみ・サイズ上限つきで読む。
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
# LLM を使わず決定的に、「結論を含む最後の agent_message」を優先し、末尾の作業宣言を落として選ぶ。
# 語尾は「次アクション動詞」の curated list に限定する（汎用の「〜します」を全部弾くと所見まで落ちるため）。
_PROGRESS_VERBS = (
    "確認します", "切り分けます", "調べます", "特定します", "検討します", "探します",
    "洗い出します", "整理します", "確かめます", "突き止めます", "チェックします", "見ていきます",
    "精査します", "分析します", "追います", "たどります", "把握します", "収集します", "集めます",
    "比較します", "検証します", "調査します", "確認していきます", "見ます",
)
_PROGRESS_END_RE = re.compile(
    "(?:" + "|".join(map(re.escape, _PROGRESS_VERBS)) + r")[。.!！\s]*$")
# 語尾が作業宣言でも単文の事実記述は progress と誤判定しない。手順マーカー（順序表現）で始まる文は作業宣言とする。
_PROGRESS_MARKERS = (
    "まず", "次に", "続いて", "これから", "今から", "この後", "最後に", "では", "それでは",
)
# 「調べてから伝える」型の宣言語尾。手順マーカーで始まる文に限って次アクション扱いにする（`_PROGRESS_VERBS` には入れない）。
_REPORT_BACK_VERBS = ("報告します", "お伝えします", "まとめます", "回答します", "共有します")
_REPORT_BACK_END_RE = re.compile(
    "(?:" + "|".join(map(re.escape, _REPORT_BACK_VERBS)) + r")[。.!！\s]*$")
# 報告系語尾に付けるマーカーからは「最後に」を除く（結論の締めのため）。
_REPORT_BACK_MARKERS = tuple(m for m in _PROGRESS_MARKERS if m != "最後に")


def _is_next_action_sentence(s: str) -> bool:
    """文が次アクション宣言か（作業宣言語尾、または手順マーカー付きの「結果を報告します」型）。"""
    return bool(_PROGRESS_END_RE.search(s)) or (
        s.startswith(_REPORT_BACK_MARKERS) and bool(_REPORT_BACK_END_RE.search(s)))


def _is_progress_only(text: str) -> bool:
    """text の全ての文が次アクション宣言なら True（結論文が1つも無い）。
    句点/改行で文に割り、(a) いずれかの文が手順マーカーで始まる、または (b) 文が2つ以上ある、のどちらかを満たすときだけ「作業宣言だけの message」とみなす。単文・マーカー無しは False。
    """
    sents = [s.strip() for s in re.split(r"[。\n]+", text) if s.strip()]
    if not sents:
        return True
    if not all(_is_next_action_sentence(s) for s in sents):
        return False
    return any(s.startswith(_PROGRESS_MARKERS) for s in sents) or len(sents) >= 2


def _trim_trailing_progress(text: str) -> str:
    """単一段落（改行なし）の平文に限り、末尾の連続する作業宣言文を落として結論で締める。
    改行や箇条書きを含む場合、または全部が作業宣言で空になる場合は元文を返す。
    """
    if "\n" in text:
        return text
    parts = [p for p in re.findall(r"[^。]*。|[^。]+$", text) if p.strip()]
    while len(parts) > 1 and _PROGRESS_END_RE.search(parts[-1].strip()):
        parts.pop()
    return "".join(parts).strip() or text


def _pick_codex_headline(completed: list[str], partial: str = "", prefer_marker: str | None = None) -> str:
    """集めた複数の agent_message から headline を決定的に選ぶ（LLM 不使用）。
    `prefer_marker`（素の Codex 用）: この文字列を含む message があれば、その最後のものを優先する。
    ① 結論を含む最後の message を優先する。② その末尾に連なる作業宣言文は落とす（`_trim_trailing_progress`）。③ どれも作業宣言だけなら最後の message をそのまま返す。
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


# 「作業報告＋次アクション」型の途中経過（例:「関連資料を確認しました。次に影響範囲を調べます。」）。
# 完了形の作業報告が全部で、明示的な次アクション文を伴うときだけ途中経過とみなす（自動継続の判定専用・見出しの選び方は変えない）。
_REPORT_VERBS = (
    "確認しました", "確認済みです", "調べました", "調査しました", "特定しました", "把握しました",
    "整理しました", "洗い出しました", "検索しました", "取得しました", "読みました", "精読しました",
    "収集しました", "集めました", "検証しました", "比較しました", "分析しました", "チェックしました",
    "たどりました", "追いました", "見ました", "見つけました", "確かめました",
)
_REPORT_END_RE = re.compile(
    "(?:" + "|".join(map(re.escape, _REPORT_VERBS)) + r")[。.!！\s]*$")


def _is_report_with_next_action(text: str) -> bool:
    """全文が「完了形の作業報告」または「次アクション宣言」で、両方を少なくとも1文ずつ含む。
    次アクション宣言だけの message は `_is_progress_only` の領分（単文の事実記述を結論扱いにするため）。
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
    """集めた agent_message が1件以上あり、連結したテキストが途中経過（作業宣言だけ、または作業報告＋次アクション）で結論文が1つも無いなら True。
    message 単位の保護: 単文・手順マーカーで始まらない・`_PROGRESS_END_RE` に合う message が1つでもあれば False（結論あり）。
    それ以外は全 message を連結して判定する（作業報告と次アクションが別 message に分かれていても拾うため）。空/空白のみの message は対象外、1件も無ければ False。
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


# 原本と変換済みテキストの保護。読み取り専用はサンドボックス（permission profile の read）が強制し、指示は多層防御として全モード・全レンズに常置する。
_READ_ONLY_SENTENCE = (
    "原本と変換済みテキストは読むだけで、書き換え・上書き・移動・削除・名前の変更を絶対にしない。"
    "Word・Excel などの原本を自分で変換しない（変換済みテキストを読む）。"
)
# 設計書と実装の両面で確かめる（全モード共通）。AGENTS.md にも同じ規律があるが、書出し失敗でも消えないようプロンプトにも常置する。
_BOTH_SIDES_SENTENCE = (
    "設計書と実装（ソース）の両方で確かめ、それぞれの根拠（ファイル:行）を示して答える。"
    "食い違えば両方を並べて『食い違い』と書く（実装を正とする）。見たソースが画面・バッチ・SQL の"
    "どれかを明記し、一部のソースだけで全体を判断しない（確かめられなかった点はそう書く）。"
)
# 作成の依頼（lens=author）以外のターンはファイルを作らせない。作っても成果物に登録せず、作業フォルダごと消す（`_run_authoring`）。
_NO_FILES_SENTENCE = (
    "この依頼ではファイルを作らない（作業用のファイルは `.tmp/` の下に作る・終われば消える）。"
    "ファイルでほしいと頼まれたら、内容は本文に書き、『資料を作成』を選んで依頼し直すよう案内する。"
)

_CONTINUE_PROMPT = (
    "続けてください。途中経過の報告ではなく、調査を最後まで進めて最終回答（結論と根拠）を書いてください。"
)
# 出力スキーマ有効時（`_schema_on`）だけ使う継続プロンプト（AGENTS.md が構造化応答 `status`／`answer`／`next_step` を求めるのはスキーマ有効時だけのため）。
_CONTINUE_PROMPT_SCHEMA = (
    "続けてください。途中経過の報告ではなく、調査を最後まで進めて `status` を `final` にした"
    "最終回答（結論と根拠）を書いてください。ただし全件・一覧・すべての依頼で対象範囲の確認が"
    "終わっていなければ `final` にせず、`in_progress` のまま `next_step` に残りを書いてください。"
)

# 調査台帳ゲート: `status=final` をそのまま信じず、台帳（`run_dir/.tmp/investigation/`）が完了しているかを確認してから受理する。
# 既存の自動継続（`SHERPA_CODEX_AUTO_CONTINUE`）とは独立の上限。
_LEDGER_CONTINUE_CAP = 10
_LEDGER_MANIFEST_MISSING_PROMPT = (
    "調査台帳を ledger_manifest_set と ledger_item_put で登録してから続けてください。"
    "ファイルを直接書かないでください。"
)
# manifest.json が存在するが内容が規約に合わない（必須キー欠落・`items` が空等）場合は「未作成」と区別し、通常の台帳継続と同じ枠（`_LEDGER_CONTINUE_CAP`）で修復を促す（壊れた台帳から final を生成しない）。
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
    # 引数はモデル生成で型の保証がない。非文字列を集合照合に通すと TypeError でストリーム処理ごと止まるため、先に型で弾く。
    return f"状態: {status}" if isinstance(status, str) and status in known else ""


def _ledger_tool_detail_review(a: dict) -> str:
    """`verdict`（閉じた語彙）だけを出す。purpose/summary/perspectives 等のモデル生成文字列は出さない（`_ledger_tool_detail_item` と同じ契約）。"""
    verdict = a.get("verdict")
    return f"判断: {verdict}" if isinstance(verdict, str) and verdict in investigation_ledger.REVIEW_VERDICTS else ""


# 「思考の流れ」の台帳ツール行の補足。id・subject・reason 等のモデル生成文字列は出さない（件数と状態語彙の閉集合だけ）。
_LEDGER_TOOL_DETAILS = {
    "ledger_manifest_set": _ledger_tool_detail_manifest,
    "ledger_item_put": _ledger_tool_detail_item,
    "ledger_status": lambda a: "",
    "ledger_review_put": _ledger_tool_detail_review,
}


def _ledger_continue_prompt(verdict: investigation_ledger.Verdict) -> str:
    """未完了の台帳へ、idと未充足の根拠種別だけを返す。本文・pathは含めない。
    `verdict.review_missing`/`verdict.review_pending_ids`（`require_review=True` の `ledger_complete()` だけが立てる）が立っていれば、中間の見直しが必要な旨を追記する（item id のみ）。
    """
    def _join(ids: tuple) -> str:
        return "、".join(ids) if ids else "なし"
    unsatisfied = "、".join(f"{item_id}（{'・'.join(kinds)} が未確認）"
                         for item_id, kinds in sorted(verdict.unsatisfied.items())) or "なし"
    _review_note = ""
    if verdict.review_missing:
        _review_note = "回答を確定する前に ledger_review_put で中間の見直しを1件書いてください。"
    elif verdict.review_pending_ids:
        _review_note = (f"見直しで足したと申告した項目が未終端です: "
                        f"{_join(verdict.review_pending_ids)}。終端にしてください。")
    return (
        "調査台帳に未完了の項目があります。"
        f"未完了: {_join(verdict.non_terminal_ids)}。"
        f"無効: {_join(verdict.invalid_ids)}。"
        f"欠落: {_join(verdict.missing_ids)}。"
        f"未充足: {unsatisfied}。"
        f"{_review_note}"
        "これらを終端状態にしてから最終回答を返してください。台帳に無い新しい主張は書かないこと。"
    )


# 「見直しの一巡」。台帳が complete と判定した直後、回答を確定する前に一度だけ Codex へ続きを頼み、計画外の発見・前提との食い違い・中身を調べていない語が残っていないかを点検させる。
# 閉じた定数文で、本文・資料名は含めない（調査中の指示を足して結論を急がせない）。名前は `_ledger_review_*`（見直し役の巡数 `_review_rounds` とは別の概念）。
_LEDGER_REVIEW_PROMPT = (
    "回答を確定する前の点検です（調べ方を変える指示ではありません）。"
    "下書きの回答・根拠について次の2点を確かめてください。"
    "1) 台帳のどの項目にも入らない発見（想定外の区分の値・分岐・呼び出し先など）や、"
    "計画の前提と食い違う事実が無いか。"
    "2) 定義はあるのに中身をまだ調べていない語（参照・分類名）が回答に残っていないか。"
    "1か2に当てはまるものがあれば、ledger_manifest_set で目録にその項目を足してから調べ、"
    "それから答え直してください。当てはまるものが無ければ答え直さなくてかまいません。"
    "答え直すときは、質問の形に合わせ（一覧なら一覧・範囲なら範囲）、分類名や参照で止めず"
    "具体的な値まで展開し、台帳と根拠にある事実以外は新たに足さないでください。"
)
# 見直しを頼む回数の上限（2回まで）。台帳継続の上限（`_LEDGER_CONTINUE_CAP`）とは別枠。目録を増やさずに台帳を未完了へ戻した見直しも1回に数える。
_LEDGER_REVIEW_CAP = 2


# 台帳にまだ解決していない「追加の観点」の義務（`investigation_ledger.pending_continuation_review()`）が残っていたら、回答の末尾に定型文「追加で調べますか？」を付ける（AI の自由記述文はそのまま流さない）。
# `extra_perspectives` の正規化は `investigation_ledger.sanitize_review_text_list` を「続き」の注入文・調査の記録の Markdown 表示と共通で使う。
# 義務の判定は `pending_continuation_review()` に一本化する。
_REVIEW_CONTINUATION_HEADER = "追加で調べられる観点"
_REVIEW_CONTINUATION_FOOTER = "続けて調べる場合は『続き』と送ってください。"


def _review_continuation_note(reviews: tuple[dict, ...]) -> str:
    """`reviews`（`load_reviews()` の戻り値・記録順）に未解決の「追加の観点」の義務があれば、回答末尾に付ける定型文を返す（無ければ空文字・純関数）。"""
    pending = investigation_ledger.pending_continuation_review(reviews)
    if pending is None:
        return ""
    extras_text = investigation_ledger.sanitize_review_text_list(pending.get("extra_perspectives"))
    if not extras_text:
        return ""
    return (f"{_REVIEW_CONTINUATION_HEADER}: " + extras_text + "。"
           + _REVIEW_CONTINUATION_FOOTER)


def _ledger_review_is_worse(pre: dict | None, post: dict | None) -> bool:
    """見直しの一巡の後の回答候補（`_candidate_final()` の戻り値）が、見直し前より悪くなったか（空になった・主張や根拠の数が減った）を判定する純関数。
    `post is pre`（見直しが新しい final を積まなかった場合を含む）は悪化なし、`pre` が無ければ悪化なし。
    """
    if pre is None or post is None or post is pre:
        return False
    if not (post.get("answer") or "").strip():
        return True
    pre_claims = pre.get("claims") or []
    post_claims = post.get("claims") or []
    if len(post_claims) < len(pre_claims):
        return True

    def _evidence_count(claims: list) -> int:
        return sum(len(c.get("evidence_refs") or []) for c in claims if isinstance(c, dict))

    return _evidence_count(post_claims) < _evidence_count(pre_claims)


# 「確認できなかった項目」節。AI は使わず、`item.reason`（モデルの自由記述）を転記せず、状態／閉じた理由語彙（`unverified` の `reason` のみ）から定型文へ機械的に変換する。
_UNCONFIRMED_STATUS_PHRASES = {
    "not_found_in_scope": "登録範囲内では見つかりませんでした",
    "unreadable": "読み取れませんでした",
    "unavailable": "確認できませんでした（利用できません）",
}
_UNVERIFIED_REASON_PHRASES = {
    "search_truncated": "検索が上限に達し、途中までしか確認できませんでした",
    "search_error": "検索が失敗し、確認できませんでした",
    "no_hits_only": "手がかりが見つからず確認できませんでした",
    "timeout": "確認が時間切れになりました",
    "unreadable": "資料を読み取れませんでした",
    # シェル（grep 等）で調べたが台帳へ記録が付かなかった場合もこの理由になり得るため、「未着手」と決めつけない言い回しにする。
    "not_searched": "この項目を調べた記録がありません",
}
_UNCONFIRMED_ITEMS_FALLBACK_PHRASE = "確認できませんでした"
# 台帳が未完了のまま受理されたターンの、登録済みだが非終端（pending/in_progress）の item・item ファイルが無い item（`missing_ids`）向けの定型文。
_UNCONFIRMED_ITEMS_INCOMPLETE_PHRASE = "調べ終わっていません"
_UNCONFIRMED_ITEMS_HEADER = "確認できなかった項目:"


def _unconfirmed_items_list(snapshot: investigation_ledger.LedgerSnapshot) -> list[dict]:
    """台帳（`apply_unverified_downgrades` 適用後）の登録済み item から、「確認できなかった項目」の構造化リストを組み立てる（件名と定型文だけ・item の本文/引用/reason の生テキストは使わない）。要素は `{"item": 件名, "reason": 定型文}`。対象が無ければ空リスト。
    `_unconfirmed_items_section`（headline 末尾の文章版）と Codex ジョブ API の `unconfirmed_items`（構造化版）が、この同じリストを写して共有する。
    対象: ① 確認できなかった終端状態（`UNCONFIRMED_STATUSES`）の item ② 登録済みだが非終端（`NON_TERMINAL_STATUSES`）の item ③ item ファイル自体が無い item（`missing_ids`）。壊れた item（`invalid_ids`）は対象外。
    """
    manifest_ids = set(snapshot.manifest["items"]) if snapshot.manifest is not None else set()
    out: list[dict] = []
    for item_id in sorted(manifest_ids):
        item = snapshot.items.get(item_id)
        if item is None:
            if item_id in snapshot.invalid_ids:
                continue
            out.append({"item": item_id, "reason": _UNCONFIRMED_ITEMS_INCOMPLETE_PHRASE})
            continue
        status = item.get("status")
        subject = (item.get("subject") or "").strip() or item_id
        if status in investigation_ledger.NON_TERMINAL_STATUSES:
            out.append({"item": subject, "reason": _UNCONFIRMED_ITEMS_INCOMPLETE_PHRASE})
            continue
        if status not in investigation_ledger.UNCONFIRMED_STATUSES:
            continue
        if status == "unverified":
            phrase = _UNVERIFIED_REASON_PHRASES.get(item.get("reason"), _UNCONFIRMED_ITEMS_FALLBACK_PHRASE)
        else:
            phrase = _UNCONFIRMED_STATUS_PHRASES.get(status, _UNCONFIRMED_ITEMS_FALLBACK_PHRASE)
        out.append({"item": subject, "reason": phrase})
    return out


def _format_unconfirmed_items_section(items: list[dict]) -> str:
    """`_unconfirmed_items_list` の戻り値を見出し＋箇条書きの文章に整形する（チャットの headline 末尾に付ける版）。対象が無ければ空文字列。"""
    if not items:
        return ""
    lines = [f"- {it['item']}（{it['reason']}）" for it in items]
    return _UNCONFIRMED_ITEMS_HEADER + "\n" + "\n".join(lines)


def _unconfirmed_items_section(snapshot: investigation_ledger.LedgerSnapshot) -> str:
    """`_unconfirmed_items_list(snapshot)` を文章に整形する（`_format_unconfirmed_items_section` の薄いラッパー）。"""
    return _format_unconfirmed_items_section(_unconfirmed_items_list(snapshot))


def _ledger_source_required_extra(world: str, scope_paths, layer) -> tuple[str, ...]:
    """このターンの台帳ゲートへ渡す `required_extra`。範囲にソースがある調査は、item の `required_checks` に source が無くても完了判定へ source を足す（ソースは常に必須）。
    `layer` は MCP へ実際に渡す実効の層（qa 以外は `None`）を渡すこと。
    `layer == "docs"` なら source を必須にしない（MCP のソース読取が層で拒否されるため）。それ以外の層では `_scope_evidence_kinds(world, scope_paths, layer)` の1つ目の要素（登録範囲に実在するか）で判定する。走査が判定不能（`None`）なら source を必須のままにする。
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
    """`prev_snapshot` から `curr_snapshot` への間に「進捗」があったかを判定する純関数（台帳ゲートの無進捗 streak リセット判定）。
    未解決集合 U = 非終端 ∪ 欠落 ∪ 無効 で比較し、`U_prev − U_curr` が空でなければ進捗あり。
    未解決集合が縮んでいなくても、`no_progress()` の結果を登録済み id に限定してから登録済みの非終端集合と比較する。
    `required_extra`: 呼び出し側の `ledger_complete()` と同じ値を渡すこと（片方だけに足すと `stalled` と `curr_verdict.non_terminal_ids` が食い違う）。
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
    """`root` 配下（`manifest.json`・`items/`・配下ファイル）に symlink が1つでもあれば `True`。model-shell は cwd 配下に書けるため、host 側ファイルへの symlink で退避経由に本文が実体化するのを防ぐ。
    `followlinks=False` で辿らず各エントリの symlink 判定だけで検出する。列挙失敗は握りつぶさず（`os.walk` の `onerror` で再送出し）`True` を返す（fail-closed）。
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
    """`src` の台帳の正規ファイル（`manifest.json`・`items/*.json`・`coverage.jsonl`・`reviews.jsonl`）だけを `dst` へコピーする。それ以外のファイル・ディレクトリは無視する。
    `coverage.jsonl`（項目ごとの未確認）と `reviews.jsonl`（中間の見直し）も「続き」で消えないよう持ち越す。
    各ファイルをコピーする直前に `os.path.islink()` で個別確認し、symlink を検出したら `OSError` を送出して中止する（`_retire_investigation_ledger` の `except OSError` が fail-closed・警告1行にする）。
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
    coverage_src = src / "coverage.jsonl"
    if coverage_src.is_file():
        if coverage_src.is_symlink():
            raise OSError(f"refusing to copy symlink: {coverage_src.name}")
        shutil.copy2(coverage_src, dst / "coverage.jsonl")
    reviews_src = src / "reviews.jsonl"
    if reviews_src.is_file():
        if reviews_src.is_symlink():
            raise OSError(f"refusing to copy symlink: {reviews_src.name}")
        shutil.copy2(reviews_src, dst / "reviews.jsonl")


def _restore_investigation_ledger(retired_dir: Path, investigation_dir: Path, tmp_root: Path) -> bool:
    """`retired_dir`（前ターンの退避）を `investigation_dir` へ原子的に復元する。`tmp_root` 配下の一時ディレクトリへ `_copy_investigation_contract_files` で全部コピーしてから `os.replace` で置き換える（途中失敗で部分復元が残り、退避を上書きしないため）。
    失敗したら一時ディレクトリを消し、`investigation_dir` を空に戻す。fail-open（例外を投げず警告1行）。戻り値: 復元できたら `True`。呼び出し側は `False` のとき、このターンでは退避台帳を削除・置換しないこと。
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
                                 *, required_extra: tuple[str, ...] = (),
                                 require_review: bool = False) -> bool:
    """未完了（または manifest 破損等で判定不能）の調査台帳を `ledger_home/investigation` へ退避する。complete なら退避先を削除する（ただし未解決の「追加の観点」の義務が残っていれば complete でも退避する）。
    `required_extra`: 台帳ゲートに渡したものと同じ値を渡すこと。`require_review`（既定 `False`）: 台帳ゲートと同じ値を渡すこと（`True` なら `reviews.jsonl` を読んで `ledger_complete(..., reviews=..., require_review=True)` で判定する）。
    退避の要否は `investigation_ledger.pending_continuation_review(reviews)` で内部で決める（回答末尾の定型文・継続プロンプト・完了判定と同じ純関数）。
    通常終了時は戻り値（実際に退避できたか）を `env["investigation"]["retained"]` に使う。
    「未作成の空ディレクトリ」と「作業データのある無効台帳」（manifest または items 配下にファイルがある）を区別し、後者は退避する。台帳ルート配下に symlink があれば退避を拒否する（`_investigation_tree_has_symlink`）。
    fail-open。戻り値: 退避（コピー）を実際に行ったら `True`、delete のみ／何もしない場合は `False`。
    """
    retire_dir = ledger_home / "investigation"
    try:
        reviews = investigation_ledger.load_reviews(investigation_dir) if require_review else ()
        verdict = investigation_ledger.ledger_complete(
            investigation_ledger.load_ledger(investigation_dir), required_extra=required_extra,
            reviews=reviews, require_review=require_review)
        force_retain = investigation_ledger.pending_continuation_review(reviews) is not None
        if verdict.complete and not force_retain:
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
        # 原子的な置換: 一時ディレクトリへ丸ごとコピーしてから `os.replace`（同一ファイルシステム上の rename）で置き換える。
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


# ---- 出力スキーマ（`--output-schema`）----
# `_OUTPUT_SCHEMA_PATH` の3キーちょうど（strict・additionalProperties: false）と対応させる。
_STRUCTURED_KEYS = {"status", "answer", "next_step"}
_STRUCTURED_STATUSES = {"final", "in_progress"}
# v2（output_schema_v2.json）の4キーちょうど・主張1件の閉じたキー集合と語彙。
_STRUCTURED_KEYS_V2 = _STRUCTURED_KEYS | {"claims"}
# `evidence_kinds`（7つ目のキー）は主張1件が実際に開いて確認した根拠の種別（`investigation_state.EVIDENCE_KINDS` の閉集合）。6キー形（`_CLAIM_KEYS_LEGACY`）も受理し、その場合は `evidence_kinds=None`（未申告＝最終ゲートは格下げも不足の計上もしない）にする。
_CLAIM_KEYS = {"id", "status", "text", "evidence_refs", "reason", "reason_code", "evidence_kinds"}
_CLAIM_KEYS_LEGACY = _CLAIM_KEYS - {"evidence_kinds"}
_CLAIM_STATUSES = {"confirmed", "inferred", "unknown"}
_CLAIM_UNKNOWN_REASON_CODES = {
    "not_found_in_scope", "unexplored", "insufficient", "conflict", "budget", "unreadable"}


def _parse_structured(text: str | None) -> dict | None:
    """`--output-schema` で固定した3キー JSON かどうかを検証する（純関数）。
    キー集合の完全一致・`status` の値・`answer`/`next_step` の型が全て合うときだけ dict を返す。それ以外（構文エラー・途中で切れた JSON・キーの過不足・不正な型・不正な status 値）は None（呼び出し側は「未完了」として扱う）。
    """
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or set(obj.keys()) != _STRUCTURED_KEYS:
        return None
    # 先に str 型を確認してから集合照合する（非 hashable 値で TypeError にしない）。
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
    """v2 の主張1件を検証する（`investigation_state.parse_claims` と同じ規約: キー集合の完全一致・`status` の閉じた語彙・`unknown` だけ `reason_code` を閉じた語彙から要求）。不正なら None（呼び出し元は主張配列全体を無効とする）。
    confirmed には非空の `evidence_refs` を要求する（参照の実在チェックはしない）。inferred は空白のみでない `reason` を必須にする。
    `evidence_kinds`（閉集合の list）は7キー形でだけ検証する。6キー形は `evidence_kinds=None` を補って返す（最終ゲートは未申告として扱う）。
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
    """v2 出力スキーマ（`claims` を持つ4キー）を検証する。v1 の3キー形（`claims` 無し）も `_parse_structured` と同じ検証で読み、返す dict に `claims: []` を補う。
    v2 の4キー形は主張配列の各要素も `_parse_claim` で検証し、1件でも不正なら主張構造だけ `claims: []` に落とす（`status`/`answer`/`next_step` はそのまま返す＝回答本文を巻き添えにしない）。
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
            # 不正な主張構造は本文を巻き添えにせず主張だけを捨てる。
            _log.warning("codex v2: invalid claim dropped (claims emptied, answer kept)")
            return {**obj, "claims": []}
        parsed_claims.append(parsed)
    return {**obj, "claims": parsed_claims}


# Codex は自分の MCP/直読の履歴を残さないため、主張自身が申告する `evidence_kinds`（`_parse_claim` が検証）を根拠種別の唯一の入力にする。
# レンズ別必須種別・「範囲に無い」の除外・確定の格下げ文言は API 側と同じ語彙・文言（`investigation_state.demote_reason_for_missing_kinds`）を使う。
def _apply_codex_evidence_gate(claims: list[dict], *, lens: str, world: str, scope_paths,
                               layer, personal_facts: str) -> tuple[list[dict], dict, tuple, tuple]:
    """確定主張のうち必須の根拠種別を欠くものを推定へ格下げし、ターン単位の不足も併せて返す。
    戻り値: `(格下げ済みの claims コピー, envelope 用 evidence_gate meta, ターン単位の不足種別, 範囲に無い種別)`。後2つは headline 注記（`_evidence_gate_note`）用の生の種別名。
    `unavailable`: 登録範囲にその種別が存在しない（該当なし＝不足に数えない）。
    `personal_facts` があれば `log_config` は主張単位・ターン単位の両方で充足済みとして扱う。
    meta の `applied`: 未申告（`evidence_kinds is None`）の主張が無く、ターン単位の判定を確定的に行えたか（1件でも未申告があれば `missing_codes` は空のまま `applied=False`）。
    meta の `demoted`: 格下げした主張の件数。呼び出し側はこの件数で「一部の主張は推定に留めた」注記を前置する。
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
    """区切りを `/` に統一し先頭の `./` を除く（台帳 item の `evidence.path` と claim の `evidence_refs` の表記ゆれを吸収する）。"""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _parse_evidence_ref(ref) -> tuple[str, int] | None:
    """claim の `evidence_refs` 1件（`"path:line"` 形式）を `(正規化 path, line)` に分解する。形式に合わなければ `None`（台帳のどの `evidence` とも一致しない扱い・例外は投げない）。"""
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
    """台帳の manifest に登録された item（`snapshot.manifest["items"]`）だけの `evidence` を `(正規化 path, line)` の集合にまとめる。未登録 item の evidence は裏付けとして採用しない。manifest が無い／`items` が空なら集合は空（全 confirmed が格下げされる）。"""
    manifest_ids = set(snapshot.manifest["items"]) if snapshot.manifest is not None else set()
    locations = set()
    for item_id, item in snapshot.items.items():
        if item_id not in manifest_ids:
            continue
        for ev in item.get("evidence") or []:
            locations.add((_normalize_evidence_path(ev["path"]), ev["line"]))
    return locations


# `_claims_vs_ledger` が confirmed を推定へ格下げするときの理由文の先頭に付ける固定文（既存の `reason` があれば括弧書きで残す）。
_LEDGER_UNMATCHED_REASON = "根拠が調査台帳に無いため確定できません"


def _claims_vs_ledger(claims: list[dict], snapshot: investigation_ledger.LedgerSnapshot, *,
                      manifest_file_exists: bool) -> tuple[list[dict], dict]:
    """最終回答の `claims` を調査台帳と突き合わせる（claims は台帳からの投影）。
    `evidence_refs` が1件も台帳の `evidence`（`path`/`line`）に一致しない confirmed 主張は、推定（inferred）へ格下げする（`status`/`reason`/`reason_code` を書き換える）。一部だけ一致する confirmed は維持する。`inferred`／`unknown` は対象外。
    `manifest_file_exists`（`(investigation_dir / "manifest.json").is_file()`）で `snapshot.manifest is None` の原因を区別する:
    - ファイルが無い＝台帳を作らなかった依頼: 対応関係を適用せず `claims` を無変更で返す。
    - ファイルはあるが内容不正／symlink＝壊れた台帳: 登録集合を空として扱い、confirmed を全て格下げする。
    戻り値: `(格下げ済みの claims コピー, 統計 dict)`。`manifest_state` は `"absent"`／`"invalid"`／`"valid"`。`absent` は `{"ledger": False, "manifest_state": "absent"}`、それ以外は `{"ledger": True, "checked": 検査した confirmed 件数, "downgraded": 格下げ件数, "unmatched_refs": 台帳に一致しなかった evidence_refs の延べ件数, "manifest_state": ...}`。
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


# 格下げは起きたがターン全体では種別が揃っている（不足の注記が出ない）ときの前置文（本文は書き換えない）。
_DEMOTED_CLAIMS_NOTE = "一部の内容は必要な根拠の種別が揃っていないため、確定ではなく推定として扱っています。"


# 成果物の move／台帳登録に1件でも失敗したとき、回答本文の末尾に付ける固定文。
_CREATED_FILES_FAILURE_NOTE = "（作成したファイルの一部を保存できませんでした。管理者に確認してください）"
# 壁時計上限（`SHERPA_CODEX_WALL_CLOCK_LIMIT_S`）で打ち切った時に headline へ付ける注記。
_WALL_CLOCK_LIMIT_NOTE = "（時間の上限に達したため、ここまでの結果で打ち切りました）"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """env の整数解析（`agentic_search._env_int` と同型・循環 import 回避のため独立実装）。範囲 [lo, hi] 外・非整数は既定値へ戻す（既定値自体も [lo, hi] にクランプ）。実行のたびに呼ばれる。"""
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
    """`_mcp_budget_env` の文字列値（数値の文字列、または mcp 無効時の `"-"`）を `activity.settings`（JSON 数値）へ変換する。数値でなければ None（推定で埋めない）。"""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# 永続 CODEX_HOME（`.codex-sessions/{conversation_id}`）は同一会話の複数ターンが同じ固定パスを共有するため、同一会話の2実行が重なると config.toml の再作成や session JSONL への書込、終了時の削除が競合する。
# conversation_id をキーにした非ブロッキング lock で、同一会話の永続 CODEX_HOME を使う実行だけを直列化する（別会話・非永続セッションは対象外）。
_CONVERSATION_LOCKS: dict = {}
_CONVERSATION_LOCKS_GUARD = threading.Lock()


def _conversation_lock(conversation_id) -> threading.Lock:
    with _CONVERSATION_LOCKS_GUARD:
        lk = _CONVERSATION_LOCKS.get(conversation_id)
        if lk is None:
            lk = _CONVERSATION_LOCKS[conversation_id] = threading.Lock()
        return lk


class CodexProvider(Provider):
    """Codex を頭脳にするエージェント中核。
    取得（Neo4j/grep）は本物のツールで実行しつつ、Codex 自身も原文を grep/参照で裏取りする。Codex の実コマンド実行・推論・回答を `--json` から拾い、1つずつ思考ノードに流す。失敗/未導入は決定的回答にフォールバックする。
    既定 reasoning は `depth_profile.CODEX_REASONING_DEFAULT`。推論レベルは調べる深さでは変えず、管理画面の基準値で固定する（`depth_profile.codex_reasoning_for`）。
    設計: docs/design/codex.md「頭脳の選択」
    """
    label, model = "Codex", "gpt-5.5"
    provider_id = "codex"

    def __init__(self, reasoning: str | None = None, model: str | None = None,
                web_search: bool | None = None, ollama_base_url: str | None = None,
                openai_api_key: str | None = None, system_settings: dict | None = None):
        self._reason = reasoning or depth_profile_mod.CODEX_REASONING_DEFAULT
        # チャットの Codex モデルは選択可。argv `-m` に渡すため、先頭ハイフン/空白/制御文字/過大長は `model_catalog.CODEX_MODEL_NAME_RE` で弾く。
        # 未指定（None/空文字）だけを既定 "gpt-5.5" へ解決する。不正な非空値は黙って別モデルへ置換せず `InvalidModelNameError`（`ValueError` のサブクラス）を送出する（`_select_provider` がこの型だけ捕捉して `_UnwiredProvider` にする）。
        if model and not model_catalog.CODEX_MODEL_NAME_RE.fullmatch(model):
            raise model_catalog.InvalidModelNameError(f"不正な Codex モデル名です: {model!r}")
        self.model = model or "gpt-5.5"
        # ユーザーの希望（設定 codex_web_search）。実際に効くかは管理者フラグ次第（`_web_search_disabled_value` が admin 許可と AND する）。
        self._web_search = bool(web_search)
        # Codex(Ollama) 構成のとき、Codex CLI を向ける Ollama の接続先。None＝Codex(OpenAI)。`_select_provider` が SSRF ガード（`llm.assert_ollama_url_allowed`）を通してから渡す。
        self._ollama_base_url = ollama_base_url or None
        # Codex(OpenAI) 構成で接続先が既定以外（Azure 等）のときだけ `_select_provider` が渡す（それ以外は None）。カスタム model_provider は子プロセスの env からキーを読むため、この構成のときだけ `_codex_clean_env` にこの値を渡す。
        self._openai_api_key = openai_api_key or None
        # `_select_provider` が key/model 解決に使ったのと同じ system_settings スナップショット。config.toml 生成・web_search 注記へも渡す。省略時（`None`）は `llm.py` が都度読み直す。
        self._system_settings = system_settings
        # 既定は空（`run()` を経由せず `_prompt`/`_prompt_mcp` を直接呼ぶ場合用）。`run()` 冒頭で `ctx.history` から設定し直される。
        self._history: list = []

    def _history_block(self) -> str:
        """直前ターンの履歴を Codex プロンプトへ前置するテキスト（会話継続）。`self._history` が空なら空文字列。"""
        if not self._history:
            return ""
        lines = [f"{'ユーザー' if h.get('role') == 'user' else 'アシスタント'}: {h.get('content', '')}"
                for h in self._history]
        return "【直前の会話（参考・新しいものが下）】\n" + "\n".join(lines) + "\n\n"

    def _prompt(self, message, lens, env, world):
        sys = (self.system_prompt + "\n\n") if self.system_prompt else ""  # 回答方針を前置
        # cwd が workspace/authoring/ のため KB パスは絶対パスで渡す。出典列挙/文体等の共通ルールは AGENTS.md にあり、ここには質問固有部分と、AGENTS.md の書込失敗時でも消えない containment/grounding（KB 以外を読まない・確定と推定を分ける）の短縮形を置く。
        # 探す対象（層）が限定されたターンは、この直接 grep 経路を呼び出し元が実行しない（MCP 経由のときだけ実行する）。
        base = (
            "あなたは社内ナレッジ調査エージェントです。以下の資料フォルダ"
            f"（{_kb_hint_abs(world)}）を **grep やファイル参照で実際に調べてください**。"
            "Excel/Word/PowerPoint/PDF は Python（openpyxl・python-docx・python-pptx・pdfplumber）で"
            "開いて読んでよい。"
            f"{_READ_ONLY_SENTENCE}{_BOTH_SIDES_SENTENCE}"
            "**指定資料フォルダ以外は読まない。確定した事実と推定は分けて書く**（詳細ルールは AGENTS.md）。"
            "**途中経過だけの応答（「次に〜を調べます」など）で終えない。調査を最後まで進めてから、"
            "結論と根拠を最終回答として書く。**"
            # この経路（MCP 無効・直接 grep/ファイル参照）で読んだ資料も、回答末尾の固定書式で Sherpa（`citations.parse_referenced_doc_lines`）に出典へ変換させる。
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
                # 履歴があれば【依頼】の前に前置する。
                f"{self._history_block()}【依頼】{message}\n【参考（構造化済みの事実）】{_facts(lens, env)}")
        return sys + base + _NO_FILES_SENTENCE + (
            "ユーザの質問に答えてください。"
            "下の『参考（構造化済みの事実）』は補助に使ってよいが、件数・対象名は事実のまま。\n\n"
            # 履歴があれば【質問】の前に前置する。
            f"{self._history_block()}【質問】{message}\n【参考（構造化済みの事実）】{_facts(lens, env)}")

    def _prompt_mcp(self, message, lens, world, direct_read: bool = True, layer=None):
        """MCP 版プロンプト。事実を前渡しせず、Codex に MCP ツールで自律調査させる。MCP ツール固有の使い分けと、containment/grounding の短縮形を置く（共通ルールは AGENTS.md）。
        `direct_read`（既定 True）: 原本直読（permission profile で KB／派生ルートを read し、コードインタープリターで直接開く）の可否。`_run_authoring` が秘匿列挙と範囲（`_scope_deny_entries`）の成否から計算して渡し、失敗した（fail-closed）ターンだけ False（MCP のみへ縮退）。
        """
        sysp = (self.system_prompt + "\n\n") if self.system_prompt else ""
        _read_block = (
            "**原本は直接読んでよい（読取専用・指定された資料フォルダと派生フォルダの中だけ・"
            "秘匿名のファイル（.env／鍵／credentials 等）は読まない）。"
            f"{_READ_ONLY_SENTENCE}"
            "旧形式など読取ツールで開けない原本は read_doc で変換済みテキストを読む。"
            # 主従は決めない。まず読取ツールで原本を読み、突合・集計など定型外だけ Python を使う。
            "まず読取ツール（xlsx_sheets／xlsx_range／docx_paragraphs／pptx_slides／"
            "pdf_pages／file_head）で原本を読む。複数ファイルの突合・集計など定型外の作業"
            "だけ Python（openpyxl・python-docx・python-pptx・pdfplumber。集計は pandas）"
            "で開く。テキスト・コードはそのまま読んでよい。"
            "派生 MD／rag.md は補助。台帳・検索・グラフ・出典の確定は MCP ツールで行う。"
            # 直読した資料は MCP の結果に載らないため、回答末尾に固定書式の行を書かせ、Sherpa（`citations.parse_referenced_doc_lines`）が台帳で実在確認したものだけ出典へ昇格する。
            "回答の最後に『参照した資料:』の行を置き、実際に開いて根拠にした資料を1行1件、"
            "資料フォルダからの相対パス（例 `4期更改/02_設計/xxx.xlsx`）で列挙する。"
            "Sherpaがこれを出典（原本ダウンロード）に変換する。派生MD／rag.mdを見た場合も原本のパスで書く。**"
            # 質問の型に合う調査スキルへ誘導する（直読不許可のターン（direct_read=False）では入れない）。
            "**質問の型（資料一覧／仕様の問い合わせ／影響範囲／原因調査／比較）に合う"
            " `.agents/skills` の investigate-* スキルを読んで、その手順（ツールで当たり→"
            "原本の中身を確かめる→答える）どおりに進める。**"
            if direct_read else
            "**今回は原本の直接読み取りは使えない。資料の本文は MCP のツールで読む（KB 外は読まない）。**"
            f"{_READ_ONLY_SENTENCE}"
        )
        # 層（探す対象）は Codex に強制しない（直読は層に関係なく read）。限定されたターンだけ案内する。
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
            # 影響を問う質問の分解の型。症状語で検索を乱発させず、変更対象と影響先の「接続（経路）」の有無を根拠に答えさせる。
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
            # ask_user の使用条件（agentic と同じ制約）＋乱用ガード（確認ID 付きは再質問しない・1回まで）。lens 別の例を示し、ユーザー主導の確認要求も発動手段にする。
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
            # author は MCP ツールで根拠を集めたうえで成果物ファイルを authoring 直下に作る。仕様が曖昧な場面が多いため、着手前の確認を促す。
            return sysp + base + (
                " 調べた内容を根拠に、**成果物ファイルをこのディレクトリ（authoring 直下）に作成してください**。"
                "**仕様（列構成・粒度・対象範囲など）が曖昧で結果が大きく変わる場合は、着手前に ask_user で確認する**。"
                "Excel/Word/PowerPoint 等を作る場合は `.agents/skills` 配下のスキル（xlsx/docx/pptx の"
                " SKILL.md）を確認して活用する。"
                # スライド/プレゼンは既定 Marp、後で PowerPoint 編集するなら python-pptx。Codex は marp の .md を書くだけでよい（レンダは Sherpa が完了後に自動実行する）。
                "**スライド・プレゼン資料は見た目重視の marp スキル（HTML/PDF/PPTX）を既定で使う**。"
                "marp スキルでは Marp 形式の `.md` を書くだけでよく、レンダ（HTML/PDF/PPTX への変換）は"
                "この作業の完了後に Sherpa 側が自動で行う（自分でレンダコマンドを実行する必要は無い）。"
                "「あとで PowerPoint で編集したい」と明示された場合だけ、"
                "marp を使わず pptx スキル（python-pptx）で作る。"
                "最後に**作成したファイル名**と**内容の要約**を"
                "日本語で報告してください。\n\n"
                # 履歴があれば【依頼】の前に前置する。
                f"{self._history_block()}【依頼】{message}")
        # 履歴があれば【質問】の前に前置する。
        return sysp + base + " " + _NO_FILES_SENTENCE + f"\n\n{self._history_block()}【質問】{message}"

    def _prompt_plain(self, message, lens, world, mcp: bool = True):
        """素の Codex（`plain`）向けプロンプト。Sherpa の調べ方の上乗せ（MCP ツール一覧・list_docs 誘導・investigate スキル誘導・台帳・影響調査の手順・原因調査の症状語の指示）は持たず、Codex 本来の調べ方（シェルで直接読む）に任せる。
        containment（範囲・秘匿は読まない）・出典書式・ask_user の使い方は AGENTS.md（`codex_agents_md.AGENTS_MD_PLAIN`）と重複しても多層防御として置く。
        """
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
            "- ソース（.c・.bas・COBOL・JCL など）は Shift_JIS（CP932）のことが多い。日本語の語で"
            "探すときは CP932 と確認したソースに `rg -E sjis` を使う（英数字の名前はそのままで当たる）。"
            "rg が無ければ `grep -rn` を使い、探す語を `iconv -t CP932` で変換してから探す。"
            "cat／sed で中身を読むときも CP932 のファイルだけ `iconv -f CP932 -t UTF-8` を通す"
            "（UTF-8 のファイルには使わない）。\n"
            "**指定された資料フォルダ・変換済みテキスト以外（このディレクトリの外・ユーザー"
            "workspace・秘匿名のファイル（.env／鍵／credentials 等）等）は絶対に読まない。**"
            f"{_BOTH_SIDES_SENTENCE}"
            f"{_READ_ONLY_SENTENCE}"
            "作業用のファイルは"
            "このディレクトリ直下に作らず `.tmp/` の下に作る。"
            + ("MCP サーバ『sherpa』の graph_neighbors（呼び出し／コピー／参照のつながり）の"
               "ツールも使ってよいが、必須ではない。" if mcp else "") +
            "利用者向けに整理して日本語で答えてください。確定した事実と推定は分けて書く。"
            # 出典の書き方は `_prompt_mcp` の `_read_block` と同じ文言。
            "回答の最後に『参照した資料:』の行を置き、実際に開いて根拠にした資料を1行1件、"
            "資料フォルダからの相対パス（例 `4期更改/02_設計/xxx.xlsx`）で列挙する。"
            "Sherpaがこれを出典（原本ダウンロード）に変換する。派生MD／rag.mdを見た場合も原本のパスで書く。"
            # ask_user の使い方・確認ID の再質問禁止は `_prompt_mcp` と同じ文言。
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
            # author 向けの成果物の作り方（marp/pptx の使い分け）は `_prompt_mcp` と同じ文言。
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
        # ナレッジ参照オフでは Codex CLI を起動しない（read-only でも KB を覗けてしまうため）。通常この経路には来ない（`routers/chat.py::_knowledge_for` が資料参照 ON を強制する）。内部経路や古いクライアントが knowledge=False で呼んだ場合の安全網。
        return ("Codex は常に社内資料を参照して回答します。"
                "資料を参照しない雑談は OpenAI／ローカルLLM を選んでください。")

    def run(self, ctx: Ctx) -> Iterator[dict]:
        # 分岐前に確定させる（`_prompt`/`_prompt_mcp` が `_run_authoring` から参照する）。
        self._history = list(ctx.history or [])
        if not ctx.knowledge:  # ナレッジ参照オフ＝素の会話（Codex を grep なしで・authoring 不使用）
            yield from _plain_run(self, ctx); return
        yield from self._run_authoring(ctx)

    def _run_authoring(self, ctx: Ctx) -> Iterator[dict]:
        decision = env = None
        _turn_t0 = time.monotonic()  # `sherpa.usage` ログ 1 行の elapsed（このターン全体）
        # 素の Codex モード（`codex_mode`）はターンの最初に1回だけ決め、プロンプト・AGENTS.md・スキル配備・出力スキーマ・multi_agent・台帳・MCP env・codex.log 開始行・activity.settings で同じ値を使う。`standard`（既定）はこのフラグが常に偽。
        _plain = codex_mode(self._system_settings) == "plain"
        # 下調べ（`_gather` の `ctx.dispatch`）を省くのは、Codex を MCP 付きで起動する見込み（CLI が有る・MCP 有効）で、レンズが `_PRESEARCH_SKIP_LENSES` のときだけ。
        # `ws_authoring`/`run_dir`/`_codex_home_ok` はこの時点で計算できないため含めない。判定は `_gather` の前に1回だけ行い、後段（`mcp` 変数・起動ガード・未応答時のノード文言）でも同じ値を使う。
        _codex_bin = shutil.which("codex")
        _mcp_enabled = _codex_mcp_enabled()
        _skip_lenses = _PRESEARCH_SKIP_LENSES if (_codex_bin and _mcp_enabled) else frozenset()
        # `_gather` は `from sherpa import agents as _facade` を関数内で遅延 import し、facade 属性経由で実行時解決する（差し替えを効かせる・循環 import 回避）。
        from sherpa import agents as _facade
        for ev in _facade._gather(ctx, skip_presearch_lenses=_skip_lenses):
            if isinstance(ev, dict) and ev.get("type") == "_env":
                decision, env = ev["decision"], ev["env"]
            else:
                yield ev
        if env is None:  # _gather が clarify question を出して停止＝確認待ち
            return
        _skip_presearch = decision["lens"] in _skip_lenses

        yield _node("codex", "think", "Codex が調べる", "資料を調べています", "active")
        answer, ran = None, False
        # 「CLI はあるが認証が無い」と codex exec は即座に非ゼロ終了し JSON を1行も出さない。起動前ガードで一度も起動していないケースと区別するため、if ブロック内でだけ True にする（スキップされた経路は False のまま＝既存の決定的回答フォールバック）。
        _codex_silent_failure = False
        # 自動継続の `env["codex_stopped_early"]` 判定用フラグも既定 False（`_agent_msgs` 等は if ブロック内にしか無い）。
        _codex_stopped_early = False
        # Popen を試みていない経路（`ws_authoring`/`run_dir` が None・shutil.which 不在等）は技術的失敗ではないため既定 False（第3分岐で参照するためここで定義する）。
        _stream_error = False
        codex_question = None  # ask_user 由来の question（出たら env/_result を出さずターン終了）
        codex_usage = None  # turn.completed の usage（best-effort・出なければ None）
        # 利用統計 activity: Codex 専用区間の開始/終了（`time.monotonic()`）。開始は最初の `subprocess.Popen` 直前（1回だけ）、終了は直近の `proc.wait()` 直後（attempt ごとに更新）。準備や後処理を含めない。
        _agent_start_mono: float | None = None
        _agent_end_mono: float | None = None
        # 1ターン全体（自動継続・台帳継続を含む）の壁時計上限（既定90分・0=無制限・`SHERPA_CODEX_WALL_CLOCK_LIMIT_S`）。`_agent_start_mono` を起点にし、継続 attempt も同じ起点からの残り時間で打ち切る。
        _wall_clock_limit_s = _env_int("SHERPA_CODEX_WALL_CLOCK_LIMIT_S", 90 * 60, 0, 24 * 3600)
        _wall_clock_state = {"hit": False}  # いずれかの attempt が上限で打ち切られたら True（attempt をまたいで保持）
        # resume 試行が失敗し新規セッションへ切り替わったら True（usage のターン差分判定に使う）。
        _resume_fallback_happened = False
        # サイドカーの吸収を許可してよいかの唯一のゲート。sandbox 有効時は事前 unlink・設定生成が両方成功した時、非サンドボックス経路は env 配線が済んだ時点で立てる（`_absorb_mcp_sidecar` 参照）。
        _sidecar_init_ok = False
        # `graph_neighbors` の mcp_tool_call item が旧世代グラフの構造化エラー（`_graph_schema_era_from_item`）を運んできたら捕まえる。調査は止めない（Codex は grep/原本読取ツールを使える）。「縮退した」という印として、終了後の env に冒頭告知と統計フラグを載せる。
        _graph_schema_era_error = None
        # 子（worker/evaluator）がサイドカー経由で報告した障害コード（`mcp_server._SIDECAR_ERROR_CODES`）。
        _mcp_error_codes: list = []
        # MCP ツール結果のバイト予算（`mcp_server.py` の `{"kind":"limit",...}`）: 1件あたりのクリップ件数（累算）と、累計予算到達（bool）。`env["limits"]` へ合流させる。
        _mcp_tool_result_clipped = 0
        _mcp_total_budget_hit = False
        # 同一クエリの重複実行の抑止（`field: "duplicate_tool_call"`）の件数。`env["limits"]` へ合流させる。
        _mcp_duplicate_tool_call = 0
        # `run_tool()` 自身の内部切り詰め（`field: "search_truncated"`）の件数。`env["limits"]["search_truncated"]` へ合流させる。
        _mcp_search_truncated = 0
        # ツール呼び出し回数の上限到達（`field: "tool_calls_exhausted"`）。bool・一度立てば真のまま。`env["limits"]` へ載せる。
        _mcp_tool_calls_exhausted = False
        # 確認ID 付き再送（前の質問への回答）では ask_user を無視する（再質問ループ防止）。
        _ask_disabled = bool(re.search(r"確認ID[:：]", ctx.message or ""))
        mcp_neighbors: list = []  # Codex が graph_neighbors で引いた近傍（UI カードに反映）
        # MCP の read 系ツール（read_doc/read_around/doc_outline/compare_documents）の引数から集めた doc_id。attempt をまたいで合算し、最終 answer の「参照した資料:」の解析結果に合流させ、機械検証してから env["sources"] へ足す。
        _mcp_read_docs: list = []
        # `xlsx_sheets`（シート一覧のみ）は上と分けて集める。sources には合流させるが、根拠ゲート（sources_verified）には数えない。
        _mcp_listed_docs: list = []
        # MCP ツール呼び出しの並走計測（run 全体の合算値）。item id は codex exec プロセスごとに振り直されるため、id の集合（`seen`/`open`）は `_attempt` 内のローカル変数として作り直し、attempt 終了時（finally）にこの dict へ合算する。attempt は逐次実行のため、`max_in_flight` は attempt ごとの最大値の最大を取る。
        _mcp_calls = {"total": 0, "max_in_flight": 0}
        # `codex.log` 向け: `--json` イベントの種類別件数（attempt をまたいで合算・観測専用）。
        _event_type_counts: dict[str, int] = {}
        # `spawn_agent` した子スレッドの id（`collab_tool_call` item から捕捉・run 全体で合算する set）。multi_agent 無効では常に空。子検出の片方（旧形式）にすぎず、`_collect_child_token_usage` は親の `thread_id` があれば `parent_thread_id` 突合（新形式）でも子を見つける。
        _child_thread_ids: set = set()
        # 利用統計 activity: このターンで「親」として使った thread_id を時刻順に重複なく記録する（run 全体で合算）。resume が失敗して新規スレッドへフォールバックしたターンは2件になるため、`thread_id` が上書きされる前に控える。
        _all_parent_thread_ids: list = []
        _child_usage_totals = {k: 0 for k in _CHILD_USAGE_KEYS}
        _child_usage_found = 0
        _child_usage_missing = 0
        # 起動を検出できた子の総数（found + missing）。`spawn_agents`（codex.log 終了行）で使う。
        _child_usage_detected = 0
        codex_created_files: list[str] = []  # 実行後に台帳登録する新規ファイルの絶対パス
        _any_new_ws = False  # codex 未インストール時の NameError 防止
        _created_file_rows: list[dict] = []  # 台帳登録に成功した行（env["created_files"] 用）
        # move／台帳登録が1件でも失敗したら True（run_dir を消さず回収用に残し、回答本文へ注記を足す判定に使う）。
        _created_files_failed = False
        # 専用 authoring ディレクトリを cwd にする（個人アップロード files/ から分離・KB は絶対パスでプロンプトに渡す）。symlink が混入していると封じ込めが崩れるため `_safe_workspace_authoring` で symlink 拒否＋fail-closed。
        users_dir = Path(os.environ.get("SHERPA_USERS_DIR", "data/users")).resolve()
        uid = ctx.uid or "admin"
        ws_authoring = _safe_workspace_authoring(users_dir, uid)  # None＝fail-closed（Codex 起動しない）
        # 実行ごとの専用作業領域（cwd/書込 root）。None＝run dir が作れない＝fail-closed（Codex を起動しない）。
        run_dir = _safe_run_authoring(users_dir, uid)
        # 会話単位ロック（`_session_persistence_enabled` のときだけ後段で実値になる）。finally が参照できるようここで既定値を確定する（`_conv_lock_acquired` は自分が取得できた時だけ True）。
        _conv_lock = None
        _conv_lock_acquired = False
        # 調査台帳: `.tmp/investigation/` 作成前に早期 return する経路でも finally が安全に参照できるよう既定値を先に確定する。
        _investigation_dir = None  # run_dir/.tmp/investigation（.tmp 作成直後に確定）
        _ledger_home = None  # workspace/.codex-sessions/{cid}（永続会話のみ）
        # 台帳の完了判定へ足す追加の必須根拠種別。`ledger_complete`/`no_progress`/`_retire_investigation_ledger` の全呼び出しへ同じ値を渡す（ターンの最初に1回だけ決める）。早期 return でも finally が参照できるよう先に空で確定し、`sp`/`decision["lens"]` の確定後に上書きする。
        _ledger_required_extra: tuple[str, ...] = ()
        # 中間の見直しを完了判定へ必須にするか（`_schema_v2 and mcp` の確定後に上書きする）。早期 return 経路向けに先に確定する。
        _ledger_require_review = False
        # 台帳に残っている「追加の観点」の義務の解決を要求するか（既定False・義務の有無は `investigation_ledger.pending_continuation_review()` が見直しの列だけから決める）。
        _ledger_require_continuation_resolved = False
        _investigation_restored = False
        _investigation_verdict = None  # investigation_ledger.Verdict（ゲート確定後に埋める）
        # `_investigation_verdict` と対になる、降格適用済みの LedgerSnapshot（「確認できなかった項目」節が読む・ゲート確定後に埋める）。
        _investigation_snapshot = None
        # このターン確定時点の中間の見直し（`load_reviews` の戻り値・ゲート確定後に埋める）と、末尾へ付ける「追加で調べますか？」の定型文（無ければ空文字）。
        _investigation_reviews: tuple[dict, ...] = ()
        _review_continuation_note_text = ""
        # `_result` の env とは別項目として chat_service へ渡す台帳の正規形（manifest/items/coverage）。`env["investigation"]` を組み立てるのと同じ箇所で埋める。ゲートが走らなかったターンは `None`（chat_service は DB 保存をスキップする）。
        _investigation_record_payload: dict | None = None
        # このターンの開始時点（前ターンの退避台帳の復元直後）で既に `not_found_in_scope` だった item の id 集合。`apply_unverified_downgrades` はこの id を降格対象から除く。`_investigation_dir` 確定後に一度だけ埋める。
        _investigation_pre_turn_not_found_ids: frozenset[str] = frozenset()
        _ledger_continuations = 0
        # 「見直しの一巡」の状態。`_ledger_review_requests`＝見直しを頼んだ回数（上限 `_LEDGER_REVIEW_CAP`）、`_ledger_review_rounds`＝そのうち目録が増えた回数、`_ledger_review_items_added`＝見直しで足された item の延べ件数、`_ledger_review_attempted`＝1回でも頼んだか。
        _ledger_review_requests = 0
        _ledger_review_rounds = 0
        _ledger_review_items_added = 0
        _ledger_review_attempted = False
        # 最後に頼んだ見直しの直前の状態（台帳ゲートを通った完成回答・`_structured_answers` の長さと有効範囲・実行ごとの状態）。台帳ループを抜けた直後の安全弁が、見直し以後が失敗・未完了・確認・回答なし・悪化で終わったときにこの状態へ戻す。
        # `_ledger_review_reverted` は、その後にサイドカーの確認（ask_user）を採らない印（`_read_mcp_sidecar` は毎回先頭から読み直すため）。
        _ledger_review_pre_candidate: dict | None = None
        _ledger_review_pre_len = 0
        _ledger_review_pre_valid_from = 0
        _ledger_review_pre_turn_failed = False
        _ledger_review_pre_turn_failed_code = None
        _ledger_review_pre_attempt_msgs_start = 0
        _ledger_review_pre_latest_structured = None
        _ledger_review_pre_codex_question = None
        _ledger_review_reverted = False
        _investigation_stopped_reason = None
        # 通常終了時は `env["investigation"]` を組み立てる前に退避（`_retire_investigation_ledger`）を実行してこの flag を立てる。外側 finally の退避（切断・例外時のフォールバック）は flag が立っていれば実行しない。
        _investigation_retire_done = False
        # 復元（前ターンの退避台帳→run_dir）が途中で失敗したターンは、この run では退避（削除・置換）を一切行わず、次ターンの「続き」で復元を再試行できるよう元の退避台帳を保持する。`_investigation_retire_done` を早期に立てて通常終了・finally 双方の退避を止める。
        _investigation_restore_failed = False
        # 台帳登録（files/ move）まで完了した後で必ず削除する（正常終了・停止・例外のいずれでも。クライアント切断の GeneratorExit でも finally は実行される）。会話ロックの解放もこの finally で行い、成果物の move／台帳登録・最終回答（`_result`）の送出までロックを保持する。
        try:
            # 会話継続（Codex ネイティブ resume）: conversation_id があるターンだけセッションを永続化する。conversation_id 無しの直接呼出しは per-request 使い捨て CODEX_HOME＋`--ephemeral`。
            _persist_session = ctx.conversation_id is not None
            resume_sid = ctx.codex_session_id if _persist_session else None
            thread_id = None  # 捕捉した Codex session/thread id（`_session_persistence_enabled` のときだけ env に載せる）
            # `SHERPA_CODEX_SANDBOX=0` は常に `--ephemeral` で resume 不能のため、DB へ永続化してよいかはこの専用フラグで判定する。
            _session_persistence_enabled = _persist_session and _codex_sandbox_enabled()
            # 永続 CODEX_HOME を使う実行だけ、同一会話単位で非ブロッキング lock を取る。削除エンドポイント（`routers/conversations.py::conversation_delete`）も DB 変更前に同じロックを取る。`.codex-sessions/{cid}` の mkdir や会話の生存確認はロック取得後に行う（削除との競合で孤児ディレクトリを作らないため）。
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
            # 永続 CODEX_HOME（`.codex-sessions/{cid}`）は固定パスのため、symlink を事前に仕込まれると封じ込めが崩れる。`ws_authoring` と同じ fail-closed 契約で、安全確認できなければ Codex を起動しない（決定的回答へ）。
            # ロック取得後に会話の生存（所有 DB 行）も再確認する（削除済みなら mkdir せず実行しない）。DB 到達不可はこの確認の対象外（fail-open）。
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
            _auto_continue_count = 0  # Codex を起動しない経路でも参照するため起動条件の外で初期化
            _multi_agent_enabled = False  # env["codex_multi_agent"] 用: Codex を起動しない経路では常に偽
            if _codex_bin and ws_authoring is not None and run_dir is not None and _codex_home_ok:
                # agent_message は run 中に複数届く（作業宣言＋結論）。全部集めて後で結論を選ぶ（`_pick_codex_headline`）。try の外で初期化する（Popen 失敗の except 経路でも使うため）。
                _agent_msgs: list[str] = []
                _agent_partial = ""
                # stream 読取が途中例外で終わったか。例外時は完全版が入り得る `-o` 最終メッセージファイルを先に試す。
                _stream_error = False
                # 出力スキーマ有効時（`_schema_on`）だけ使う状態: `_latest_structured` は最新 attempt の最終出力の検証結果（合格した dict・不合格/欠落は None）。`_structured_answers` は attempt をまたいで合格した dict を積む。
                _latest_structured: dict | None = None
                _structured_answers: list[dict] = []
                # 台帳ゲートが「未完了のため受理しない」と判断した final は、見出し/主張候補として拾わない。`_structured_answers[:_structured_answers_valid_from]` は拒否済み扱いで、`_pick_structured_headline`/`_pick_structured_claims` はこの境界より後だけを走査する（台帳ゲートが継続を発行する直前に境界を更新する）。
                _structured_answers_valid_from = 0
                mcp = _mcp_enabled  # MCP ツールで自律調査（既定ON）
                sp = (ctx.scope_meta or {}).get("scope_paths")
                # Codex 自身の追加探索（MCP／直接grep）への層フィルタは qa レンズだけに渡す。author・impact/troubleshoot は対象外。
                _layer = (ctx.scope_meta or {}).get("layer") if decision["lens"] == "qa" else None
                # 台帳の完了判定へ足す source の必須化は、ターンの最初に1回だけ決め、このターンの台帳ゲート呼び出し全て（required_extra・AGENTS.md の台帳段落・ledger_status への env）へ同じ値を渡す。判定は MCP へ実際に渡す実効の層＝`_layer` を使う。MCP 無効なら台帳自体を作らないため計算しない。plain も台帳ツールを出さないため計算しない。
                if mcp and not _plain:
                    _ledger_required_extra = _ledger_source_required_extra(ctx.world, sp, _layer)
                _layer_restricted = _layer not in (None, "both")
                # 層のフィルタは MCP ツール側（run_tool）だけが担う。MCP 無効・sandbox 無効の構成では層の指定をツールに渡す経路が無いため、黙って無視せず実行前に正直に失敗する。
                _layer_enforcement_ready = mcp and _codex_sandbox_enabled()
                if _layer_restricted and not _layer_enforcement_ready:
                    # 黙って層を無視した回答を返さず、実行せず正直に失敗を伝える（Codex CLI は起動しない）。利用者向け文言は専門用語ゼロ（MCP/sandbox を出さない）。具体的な理由は decision.reason（監査・管理者ログ専用）にだけ残す。
                    msg = "この構成では探す対象の限定はできません。管理者に設定の確認を依頼してください。"
                    yield _node("codex", "think", "Codex が調べる", "探す対象の限定に対応していません", "done")
                    yield {"type": "answer_delta", "text": msg}
                    env = {"lens": decision["lens"], "headline": msg, "summary": {"total": 0},
                          "data": {}, "sources": [],
                          "agentic_failure": "error",  # 実行していないターン＝完了として数えない
                          "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world,
                                                              lens=decision["lens"])}
                    _reason = ("MCP 無効時は探す対象の限定に対応できません" if not mcp
                              else "sandbox 無効時は探す対象の限定に対応できません")
                    yield {"type": "_result", "env": env,
                          "decision": {"lens": decision["lens"], "input": ctx.message, "reason": _reason}}
                    return
                # authoring/ = Codex の書込先（cwd）。files/ = ユーザーアップロード（cwd 外・Codex から隔離）。files/ の symlink チェックはアップロード grep 側（`chat_service._personal_grep_hits`）で行う。
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
                    # `.agents`（配備したスキル）配下・ルート直下の AGENTS.md・`.mcp_sidecar.jsonl` は台帳登録スキャン対象外にする（AGENTS.md は write_agents_md() が毎回書くため、除外しないと毎回新規ファイルとして登録される。サイドカーはフォールバック経路で run_dir 直下に残る場合の保険）。
                    _before_ws_files = {
                        p for p in run_dir.rglob("*")
                        if p.is_file() and not p.is_symlink()
                        and p.relative_to(run_dir) not in (Path("AGENTS.md"), Path(_MCP_SIDECAR_NAME))
                        and not ({".tmp", ".agents"} & set(p.relative_to(run_dir).parts))
                    }
                # reasoning=minimal は image_gen/web_search と非互換で API 400 になるため low へ引き上げる。author（作成）は専用の `_REASONING_AUTHOR` を使う。通常レンズは現行のまま。
                _is_author = decision["lens"] == "author"
                # 調べる深さ: 通常レンズの基準値だけ管理画面の基準値編集（system_settings）を反映する。クイックだけ `codex_reasoning_for` が1段下げ、標準以上と author は基準値のまま `codex exec` へ渡す。
                _base_reason = (_REASONING_AUTHOR if _is_author
                               else depth_profile_mod.effective_base(
                                   self._system_settings, "codex_reasoning", self._reason))
                # author は専用の推論設定を持ち、クイックの1段下げも通さない。素の Codex（plain）は「調べる深さ」を効かせない（基準値のまま）。
                _reason_raw = _base_reason if (_is_author or _plain) else depth_profile_mod.codex_reasoning_for(
                    _base_reason, (ctx.scope_meta or {}).get("depth_profile"))
                _reason = "low" if str(_reason_raw).lower() == "minimal" else _reason_raw
                # usage メタへ足す「実際に codex exec へ渡した model_reasoning_effort」（`_reason`）と基準値（minimal→low の丸め前）（`_base_reason`）。一致なら `reasoning_base` は省略する（`usage_reasoning_extras` の契約）。
                _usage_depth_extra = depth_profile_mod.usage_reasoning_extras(
                    (ctx.scope_meta or {}).get("depth_profile"), _base_reason, _reason)
                # multi_agent は既定で常時有効にする（深さに関わらず）。判定は `codex_multi_agent_enabled`（sandbox.py・唯一の真実源）に委ねる: サンドボックス無効は対象外、既定 OpenAI・Azure・Ollama は常に対象、独自エンドポイント（custom）は `codex_worker_model` の明示設定が無いと対象外。
                # `_review_rounds` は AGENTS.md へ埋め込む見直しの回数（クイック 0／標準 2／深く 4／最大は管理画面の設定値で頭打ち）。Codex 自身にこの回数は強制されない（指示のみ）。
                # plain では下調べ役・見直し役を使わず、`codex_multi_agent_enabled` に関わらず常に偽。
                _multi_agent_enabled = False if _plain else codex_multi_agent_enabled(
                    ollama_base_url=self._ollama_base_url, system_settings=self._system_settings)
                _review_rounds = 0 if _plain else depth_profile_mod.review_rounds_for(
                    (ctx.scope_meta or {}).get("depth_profile"), self._system_settings)
                # 共通上限（管理画面の設定値）に余地があるターンだけ、本体の自己判断による見直し追加1回を AGENTS.md で許可する（`_review_rounds` は `review_rounds_for` が上限で頭打ち済み）。
                _review_rounds_escalation = (not _plain) and _review_rounds < depth_profile_mod.effective_max_review_rounds(
                    self._system_settings)
                # 原本直読の read root と秘匿 deny の列挙は、プロンプトの文言（direct_read フラグ）と permission profile（`_write_codex_authoring_config`）の両方が使うため、プロンプト組立の前に1回だけ計算する。列挙失敗（RuntimeError＝fail-closed）時は両方とも「直読不可」に揃える。
                _base_roots = _direct_read_roots(ctx.world)
                _direct_roots, _deny_roots = _base_roots, []
                _venv_for_deny = _venv_root()
                try:
                    _scope_deny = _scope_deny_entries(_base_roots, sp)
                    if _base_roots and all(r in _scope_deny for r in _base_roots):
                        raise RuntimeError("scope_enum_failed:no_scope_in_roots")  # 範囲がどの root にも無い
                    _sensitive_deny = _scope_deny + _enumerate_sensitive(
                        _base_roots + ([str(_venv_for_deny)] if _venv_for_deny else []))
                    _direct_read_ok = True
                except RuntimeError as e:
                    _direct_roots, _deny_roots, _sensitive_deny, _direct_read_ok = [], _base_roots, [], False
                    _log.warning("codex direct read disabled: %s", e)
                if not _direct_read_ok and (not mcp or _plain):
                    # MCP 無効の構成と素の Codex（plain）は直接参照だけが資料を読む手段のため、直読を許可しないと何も調べられない。黙って空振りの回答を返さず、実行前に正直に失敗する（利用者向け文言は専門用語ゼロ）。
                    msg = "この資料フォルダは今回読み取りの準備ができませんでした。管理者に確認を依頼してください。"
                    yield _node("codex", "think", "Codex が調べる", "資料の読み取り準備に失敗", "done")
                    yield {"type": "answer_delta", "text": msg}
                    env = {"lens": decision["lens"], "headline": msg, "summary": {"total": 0},
                          "data": {}, "sources": [],
                          "agentic_failure": "error",  # 実行していないターン＝完了として数えない
                          "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world,
                                                              lens=decision["lens"])}
                    yield {"type": "_result", "env": env,
                          "decision": {"lens": decision["lens"], "input": ctx.message,
                                       "reason": ("素の Codex で" if _plain else "MCP 無効の構成で")
                                                 + "直読の準備（秘匿ファイル列挙／範囲）に失敗"}}
                    return
                # `--ephemeral`（セッションをディスクに残さない）と `-o`（最終メッセージのファイル出力＝JSON 抽出が空だった時の保険）は sandbox/fallback どちらでも共通。`.tmp/` は台帳登録スキャンから除外済み。
                # run_dir は実行ごとの新規作成（`mkdir(exist_ok=False)`）のため前ターンの残存はなく、symlink にすり替わっていた場合は rmtree が例外を送出する（fail-closed のまま残す）。
                _tmp = run_dir / ".tmp"
                if _tmp.exists() or _tmp.is_symlink():
                    shutil.rmtree(_tmp)
                _tmp.mkdir(parents=True, exist_ok=True)
                # 調査台帳の置き場。`.tmp` と同じく成果物登録スキャンの対象外。空の `items/` を毎 run 用意する。
                _investigation_dir = _tmp / "investigation"
                (_investigation_dir / "items").mkdir(parents=True, exist_ok=True)
                # 前ターン退避の扱い（会話 id が無い非永続ターンは何もしない）: 永続台帳ディレクトリ（`workspace/.codex-sessions/{cid}`）に未完了台帳が残っていれば、「続き」宣言のときだけ復元し、それ以外は消す。判定は `_session_persistence_enabled`（conversation_id あり かつ サンドボックス有効）で行う（`SHERPA_CODEX_SANDBOX=0` は常に `--ephemeral` で `.codex-sessions/{cid}` を作らない）。
                # 素の Codex（plain）は台帳を使わず、standard が退避した台帳を復元も削除も退避もしない。
                if _session_persistence_enabled and not _plain:
                    _ledger_home = _safe_codex_sessions_home(users_dir, uid, ctx.conversation_id)
                if _ledger_home is not None:
                    _retired_investigation = _ledger_home / "investigation"
                    if _retired_investigation.is_dir():
                        # 退避先に symlink が1つでもあれば復元せず削除する（「続き」宣言かどうかに関わらず）。
                        if _investigation_tree_has_symlink(_retired_investigation):
                            _log.warning(
                                "investigation ledger restore skipped: symlink detected in retired dir")
                            _remove_dir_best_effort(_retired_investigation)
                        elif not mcp:
                            # 台帳は MCP の台帳ツールでしか更新できないため、MCP 無効の run では復元も削除もせず退避先をそのまま残す。
                            _log.info("investigation ledger restore skipped: mcp disabled")
                        elif (ctx.message or "").strip().startswith(("続き", "つづき", "続けて")):
                            if _restore_investigation_ledger(_retired_investigation, _investigation_dir, _tmp):
                                _investigation_restored = True
                            else:
                                # 復元が途中で失敗したターンでは、この run の退避（削除・置換）を一切行わず、元の退避台帳を保持する（次ターンの「続き」で再試行できる）。
                                _investigation_restore_failed = True
                                _investigation_retire_done = True
                        else:
                            _remove_dir_best_effort(_retired_investigation)
                # 復元直後・まだ何も探していない時点の状態を控える（`apply_unverified_downgrades` の降格対象から除く id）。
                _investigation_pre_turn_not_found_ids = frozenset(
                    item_id for item_id, item in
                    investigation_ledger.load_ledger(_investigation_dir).items.items()
                    if item.get("status") == "not_found_in_scope")
                # 「続き」での追加の観点: 復元後の `_investigation_dir` から見直しを読み、義務（`investigation_ledger.pending_continuation_review()`）が残っていれば、Codex へ「これを新しい項目として調べてから答えて」と伝える（復元される台帳は完了扱いのため、これが無いとすぐ `final` を返してしまう）。
                # 復元が行われなかった・失敗したターンは `_investigation_dir` が空のままで `pending_continuation_review` は `None` を返し、注入しない側へ倒れる。
                _investigation_reviews_for_prompt = investigation_ledger.load_reviews(_investigation_dir)
                _continuation_review_note = ""
                if (not _plain
                        and (ctx.message or "").strip().startswith(("続き", "つづき", "続けて"))):
                    _pending_review = investigation_ledger.pending_continuation_review(
                        _investigation_reviews_for_prompt)
                    # 義務が残っているかどうかは `pending_continuation_review()` だけを根拠に決める。このターンで `complete` を受理する前に義務の解決を要求するかどうか（`ledger_complete()` 呼び出し全てへ渡す・退避判断も同じ関数）。
                    _ledger_require_continuation_resolved = _pending_review is not None
                    if _pending_review is not None:
                        # 末尾の定型文（`_review_continuation_note`）と同じ `sanitize_review_text_list` を通す。
                        _extras_text = investigation_ledger.sanitize_review_text_list(
                            _pending_review.get("extra_perspectives"))
                        if _extras_text:
                            _continuation_review_note = (
                                f"前回の見直しで追加に調べられるとした観点: {_extras_text}。"
                                "これを新しい調査項目として ledger_manifest_set に足してから調べて"
                                "ください。")
                # MCP でも FS でも同じプロンプト組み立て（personal_facts を注入）。
                if mcp or _plain:
                    _codex_msg = ctx.message
                    if ctx.personal_facts:
                        _codex_msg = (f"{ctx.message}\n\n"
                                      f"【個人ファイル内ヒット（本人のみ・共有不可）】\n{ctx.personal_facts}")
                    if _continuation_review_note:
                        _codex_msg = f"{_codex_msg}\n\n【前回の続き】\n{_continuation_review_note}"
                    if _plain:
                        prompt = self._prompt_plain(_codex_msg, decision["lens"], ctx.world, mcp=mcp)
                    else:
                        prompt = self._prompt_mcp(_codex_msg, decision["lens"], ctx.world,
                                                  direct_read=_direct_read_ok, layer=_layer)
                else:
                    prompt = self._prompt(ctx.message, decision["lens"], env, ctx.world)
                _last_message_path = _tmp / f"last-message-{hashlib.sha1(os.urandom(8)).hexdigest()[:12]}.txt"
                # MCP ツール結果の予算・grep ヒット上限・読み取り窓を親側で1回だけ解決し、子プロセスの env として渡す（`mcp_server.py::_env_budget_bytes` が最優先で読む）。
                _mcp_budget_env: dict[str, str]
                _mcp_budget_env = _resolve_mcp_budget(
                    self._system_settings, self.model, self._ollama_base_url,
                    None if _plain else (ctx.scope_meta or {}).get("depth_profile")) if mcp else {}
                if mcp and _plain:
                    # 素の Codex モード: MCP サーバへツールの絞り込みを渡す。台帳のディレクトリ・required_extra は渡さない（台帳ツール自体を出さない）。
                    _mcp_budget_env["SHERPA_MCP_TOOLSET"] = "plain"
                elif mcp:
                    # 解決済み（realpath）のパスを渡す（MCP サーバ側が書込のたびに「書込先の経路に symlink が無い」ことを検査するため）。
                    _mcp_budget_env["SHERPA_MCP_LEDGER_DIR"] = os.path.realpath(_investigation_dir)
                    # `ledger_status`（mcp_server.py）の自己確認も provider 側のゲートと同じ required_extra を見られるようにする（ターンの最初に決めた値・カンマ区切り・空なら空文字列）。
                    _mcp_budget_env["SHERPA_MCP_LEDGER_REQUIRED_EXTRA"] = ",".join(_ledger_required_extra)
                codex_home = None
                # サイドカーの置き場（`_sidecar_path`）は、本サーバ側は書けるが model-shell からは書込許可外の場所に決める。両分岐の中で確定し、`_absorb_mcp_sidecar` のガードはこの変数と `_sidecar_init_ok` を見る。既定は非サンドボックス経路の置き場（サンドボックス有効時のみ codex_home 配下へ差し替える）。
                _sidecar_path = _tmp / _MCP_SIDECAR_NAME
                if _codex_sandbox_enabled():
                    # permission profile で読取を KB(RO)＋authoring(RW) に封じ込め＋env 洗浄する。CODEX_HOME は authoring の外（workspace 直下・`:root=deny` で shell から不可視）。
                    # conversation_id があるターンは会話ごとの固定ディレクトリ（`workspace/.codex-sessions/{cid}`）を CODEX_HOME にして毎ターン再利用する（`sessions/` 配下の JSONL が resume の実体＝finally では削除しない）。無い場合は per-request 使い捨て（実行後 rmtree・`--ephemeral`）。
                    # `_safe_persistent_codex_home` は外側で検証済み（ここで再計算しない）。ここに来た時点で `_session_persistence_enabled` かつ `_codex_home_ok` は保証済み。
                    if _session_persistence_enabled:
                        codex_home = _safe_persistent_codex_home
                    else:
                        _rand = hashlib.sha1(os.urandom(8)).hexdigest()[:12]
                        codex_home = users_dir / uid / "workspace" / f".codexhome-{_rand}"
                    # codex_home（`:root deny`＝model-shell から不可視・MCP サーバは別プロセスなので書ける）配下に置く。run_dir 直下だと Codex の shell ツールが偽の `{"kind":"read",...}`/`{"kind":"ask_user",...}` 行を追記でき、未読資料を根拠ゲートへ通したり任意の確認カードでターンを潰せてしまう。
                    _sidecar_path = codex_home / _MCP_SIDECAR_NAME
                    argv_base = ["codex", "exec", "--json", "--strict-config", "--skip-git-repo-check",
                                "-o", str(_last_message_path),
                                "-C", str(run_dir), "-m", self.model,
                                "-c", f"model_reasoning_effort={_reason}"]
                    # 管理者環境の既定値に依存させず明示指定する。無効時（`codex_worker_model` 未設定の独自エンドポイント等）も明示的に false にする（`[agents.*]` の層が無いまま機能だけ有効になるのを防ぐ）。
                    argv_base += ["-c", f"features.multi_agent={'true' if _multi_agent_enabled else 'false'}"]
                    if not _session_persistence_enabled:
                        argv_base.append("--ephemeral")
                    # `self._openai_api_key` は Codex(OpenAI) 構成で接続先が Azure 等の時だけ `_select_provider` が渡す（それ以外は常に None＝env に渡さない）。
                    popen_env = _codex_clean_env(codex_home, run_dir, _tmp,
                                                 openai_api_key=self._openai_api_key)
                else:
                    # フォールバック（SHERPA_CODEX_SANDBOX=0）＝`-s workspace-write`（読取全開・多層防御は OS ユーザ分離に依存）。resume 非対応（常に使い捨て）で、ここで捕捉する thread_id は env に載らない。
                    resume_sid = None
                    argv_base = ["codex", "exec", "--json", "--skip-git-repo-check",
                                "--ephemeral", "-o", str(_last_message_path),
                                "-s", "workspace-write", "-C", str(run_dir),
                                "-m", self.model, "-c", f"model_reasoning_effort={_reason}"]
                    # このフォールバック経路は常にサンドボックス無効（`_multi_agent_enabled` は常に偽）。config.toml を書かないため `[agents.*]` の層が無く、明示的に無効化する。
                    argv_base += ["-c", "features.multi_agent=false"]
                    # `--strict-config` が無い経路（config.toml でなく -c）なので同等をここで足す。
                    argv_base += _web_search_c_args(self._web_search, self._system_settings)
                    if mcp:
                        # `.tmp/`（run_dir 配下）はこの経路では model-shell からも見える（封じ込めが無い前提の経路）。`_sidecar_append` が書く内容は doc_id／ツール名／種別／時刻（と ask_user の質問）だけで本文は書かないが、shell が偽の行を追記できる可能性は残る。
                        _sidecar_env = {"SHERPA_MCP_SIDECAR": str(_sidecar_path)}
                        argv_base += _mcp_config_args(ctx.world, sp, _ask_disabled, layer=_layer,
                                                      extra_env={**_mcp_budget_env, **_sidecar_env})
                        popen_env = {**os.environ, **_mcp_env(ctx.world, sp, _ask_disabled, layer=_layer),
                                    **_mcp_budget_env, **_sidecar_env}
                        # `.tmp/` は run_dir 生成のたびに空から始まるため、前ターン残骸の事前 unlink は不要。ここで吸収を許可してよい。
                        _sidecar_init_ok = True
                    else:
                        popen_env = None
                # 出力スキーマ: OpenAI 系構成のみ（Codex(Ollama) は対象外）。`SHERPA_CODEX_OUTPUT_SCHEMA=0` で無効化・`=1` で v1。既定は v2（`claims` 付き）。
                # plain は env の値に関わらず常に 0（平文の回答・`_pick_codex_headline` を使う）。台帳ゲート（`_schema_v2 and mcp and ...`）・claims の格下げ判定は `_schema_v2` が偽になることで無効になる。
                _schema_level = 0 if _plain else _env_int("SHERPA_CODEX_OUTPUT_SCHEMA", 2, 0, 2)
                _schema_on = self._ollama_base_url is None and _schema_level >= 1
                _schema_v2 = _schema_on and _schema_level == 2
                # ログ・利用統計には実際に効いている値を出す（Codex(Ollama) は出力スキーマを使わない＝0）。
                _schema_level_effective = 2 if _schema_v2 else (1 if _schema_on else 0)
                if _schema_on:
                    argv_base += ["--output-schema",
                                 str(_OUTPUT_SCHEMA_PATH_V2 if _schema_v2 else _OUTPUT_SCHEMA_PATH)]
                # 中間の見直し（`ledger_review_put`）を完了判定へ必須にするのは Codex の標準モード（`_schema_v2 and mcp`）だけ。素の Codex・台帳を使わない構成・Codex(Ollama) は対象外。このターンの台帳ゲート呼び出し全て（while ループ・退避・最終判定）へ同じ値を渡す。`ledger_status`（MCP の自己確認）にも同じ値を env で伝える（`SHERPA_CODEX_SANDBOX=0` の経路だけは env が乗らず自己確認がわずかに楽観的になるが、実際のゲートは `_ledger_require_review` を直接使う）。
                _ledger_require_review = _schema_v2 and mcp
                if mcp and not _plain:
                    _mcp_budget_env["SHERPA_MCP_LEDGER_REQUIRE_REVIEW"] = "1" if _ledger_require_review else "0"
                    # `ledger_status` の自己確認にも同じ義務フラグを伝える。
                    _mcp_budget_env["SHERPA_MCP_LEDGER_REQUIRE_CONTINUATION_RESOLVED"] = (
                        "1" if _ledger_require_continuation_resolved else "0")
                _codex_run_started_at = time.monotonic()
                # 子 rollout の mtime 下限（壁時計）。`_collect_child_token_usage` の新形式判定はこれより前のファイルを前ターンの子として除外する。
                _turn_started_wall = time.time()
                _codex_config_kind = "ollama" if self._ollama_base_url is not None \
                    else _openai_endpoint_kind(self._system_settings)
                _depth_label = (ctx.scope_meta or {}).get("depth_profile") or "standard"
                # 予算/上限の各値は `_mcp_budget_env`（`_resolve_mcp_budget` が1回だけ解決した実効値）からそのまま読む（ログ用に別計算しない）。mcp 無効時は各キーとも "-"。`budget_total`／`max_calls`／`window_source`／`window_cli` は行の形（キー名）だけ残し、値は常に "none"（統計側がキー名を読むため）。
                _codex_mode_label = "plain" if _plain else "standard"
                _log_codex.info(
                    "start conv=%s uid=%s mode=%s config=%s multi_agent=%s depth=%s review_rounds=%s "
                    "schema_level=%s model=%s reasoning=%s budget_per_result=%s budget_total=%s "
                    "max_hits=%s window_cap=%s max_calls=%s window_source=%s window_cli=%s",
                    ctx.conversation_id, uid, _codex_mode_label, _codex_config_kind, _multi_agent_enabled,
                    _depth_label, _review_rounds, _schema_level_effective, self.model, _reason,
                    _mcp_budget_env.get("SHERPA_MCP_TOOL_BUDGET_BYTES", "-"),
                    "none",
                    _mcp_budget_env.get("SHERPA_MCP_TOOL_MAX_HITS", "-"),
                    _mcp_budget_env.get("SHERPA_MCP_TOOL_WINDOW_CAP", "-"),
                    "none",
                    "none",
                    "none")
                # 利用統計 activity.settings: 開始行と同じ実行時解決済み値をそのまま使う（"-" は取れない値として None）。
                _activity_settings = {
                    "provider": "codex", "mode": _codex_mode_label, "config": _codex_config_kind,
                    "model": self.model,
                    "reasoning": _reason, "depth": _depth_label, "review_rounds": _review_rounds,
                    "schema_level": _schema_level_effective, "multi_agent": _multi_agent_enabled,
                    "budget_per_result": _int_or_none(_mcp_budget_env.get("SHERPA_MCP_TOOL_BUDGET_BYTES")),
                    "max_hits": _int_or_none(_mcp_budget_env.get("SHERPA_MCP_TOOL_MAX_HITS")),
                    "window_cap": _int_or_none(_mcp_budget_env.get("SHERPA_MCP_TOOL_WINDOW_CAP")),
                }
                # prepare/agent（`ctx.turn_started_mono`/`_agent_start_mono`/`_agent_end_mono` から）は最初の Popen・最後の wait が確定してから finally ブロックでまとめて計算する。

                def _build_argv(use_resume: bool) -> list:
                    """codex exec を組み立てる。resume 分岐は `codex exec resume [SESSION_ID] [PROMPT]` の位置引数どおり、共通オプションの後・末尾プロンプトの前に `resume <sid>` を挿む。resume 先 id は `thread_id`（`thread.started` で捕捉した最新値）を優先し、未捕捉なら `resume_sid` を使う。
                    プロンプト本文は argv に載せず（`ps` で読まれるため）、`-` だけを置いて標準入力から読ませる。本文は `_attempt` が Popen 後に `proc.stdin` へ書く。
                    """
                    av = list(argv_base)
                    if use_resume:
                        sid = thread_id or resume_sid
                        if sid:
                            av += ["resume", sid]
                    av.append("-")
                    return av

                got_any_line = False  # resume 試行で1行も --json イベントを受け取れなければ resume 失敗とみなす
                attempt_returncode = None  # fallback 判定用
                # 自動継続がツール未実行のまま宣言だけを繰り返すのを打ち切るための per-attempt フラグ（attempt 開始ごとに False へ戻す）。
                _attempt_ran_tools = False
                # item id は codex exec プロセスごとに振り直されるため、継続 attempt が同じ id を使うとノードを上書きして履歴が消える。2回目以降の attempt の node id 接頭辞に使う連番（初回=1・以降 +1）。
                _attempt_no = 0
                # `_needs_continuation`／`codex_stopped_early` の判定を最新 attempt の message だけに絞る境界（この attempt 開始時点の `_agent_msgs` の長さ）。`_pick_codex_headline` は `_agent_msgs` 全件を見る。
                _attempt_msgs_start = 0
                # attempt 開始時に消せなかった前 attempt の `-o` 本文（吸収で読み飛ばす対象・無ければ None）。
                _stale_last_message = None
                # トップレベル `turn.failed`／`error` イベントを見た attempt かどうか（agent_message が無いこの種の失敗は「stdout に JSON が無い」判定では拾えない）。診断コード（`error.code`）だけ控え、本文はログにも利用者向け文言にも貼らない。
                _turn_failed = False
                _turn_failed_code = None
                # `error.message`（無ければトップレベル `message`）を伏せ字＋先頭300文字だけ控える（warning ログと `env["codex_error_code"]` の付随情報専用）。

                def _attempt(use_resume: bool, prompt_text: str | None = None):
                    """1回分の codex exec 実行（node/answer_delta を yield）。proc はこの1回限りのローカル状態。`prompt_text` は自動継続用（省略時は通常プロンプト）。"""
                    nonlocal got_any_line, ran, codex_question, codex_usage, thread_id, attempt_returncode
                    nonlocal _agent_partial, _stream_error, _graph_schema_era_error
                    nonlocal _attempt_ran_tools, _attempt_no, _attempt_msgs_start, _stale_last_message
                    nonlocal _turn_failed, _turn_failed_code
                    nonlocal _agent_start_mono, _agent_end_mono
                    got_any_line = False
                    attempt_returncode = None
                    _stderr_f = None
                    _attempt_ran_tools = False
                    _turn_failed = False
                    _turn_failed_code = None
                    _attempt_no += 1
                    # 前 attempt の未完 message（item.updated だけで completed が来なかった分）は履歴へ退避してから境界を引く（最新 attempt の判定に前 attempt の途中経過が混ざらないように）。
                    if _agent_partial.strip():
                        _agent_msgs.append(_agent_partial)
                    _agent_partial = ""
                    _attempt_msgs_start = len(_agent_msgs)
                    # `-o` は attempt をまたいで同じパス。前 attempt の内容を残すと、何も書かずに終わった attempt が古い文を回答として吸収してしまう。消せないときは残った本文を控え、終了後の吸収でその本文だけ読み飛ばす。
                    _stale_last_message = None
                    try:
                        _last_message_path.unlink(missing_ok=True)
                    except OSError as exc:
                        _stale_last_message = _read_last_message_fallback(_last_message_path)
                        _log.warning("codex last-message cleanup failed: %s errno=%s conv=%s uid=%s",
                                     type(exc).__name__, getattr(exc, "errno", None), ctx.conversation_id, uid)
                    # このプロセス内だけで完結する id 集合（run-level `_mcp_calls` への合算は finally で行う）。
                    _attempt_mcp_seen: set = set()
                    _mcp_read_done: set = set()  # 収集済み item id（同じ item の再送で二重に数えない）
                                                     # item id は attempt ごとに振り直される
                    _attempt_mcp_open: set = set()
                    _attempt_mcp_max_in_flight = 0
                    argv = _build_argv(use_resume)
                    _stdin_text = prompt if prompt_text is None else prompt_text
                    proc = None
                    # finally（reap する側）と監視スレッド（waitid で覗く側）の間で「回収済みか」を直列化する。回収済みの後は sid が再利用され得るため、判定・実行はこのロックの中でだけ行う。
                    _reap_lock = threading.Lock()
                    _reaped = {"done": False}
                    try:
                        # Popen 直前の最終防衛線（`_select_provider` の選択時チェックを迂回する経路があっても、起動直前にもう一度確認する）。Codex(Ollama) 構成は OpenAI 系 I/O ではないため対象外。
                        if self._ollama_base_url is None:
                            from ... import llm
                            llm.assert_openai_io_allowed()
                        # start_new_session で独立プロセスグループにし、停止/後始末で MCP subprocess / shell child まで group ごと確実に殺す。
                        # stderr は名前の無い一時ファイルへ向け、`--json` のイベントを1件も出さずに異常終了したときだけ先頭を読んでログに残す（`_log_startup_stderr`）。それ以外は読まずに捨てる（応答が流れ始めた後の stderr には資料名・本文が混ざり得るため）。
                        if _agent_start_mono is None:  # 利用統計 activity: 最初の Popen だけを起点にする
                            _agent_start_mono = time.monotonic()
                        _stderr_f = tempfile.TemporaryFile()
                        proc = subprocess.Popen(
                            argv, env=popen_env, cwd=str(run_dir), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=_stderr_f, text=True,
                            start_new_session=True)
                        # プロンプト本文を標準入力へ書いて即座に閉じる（argv には `-` しか載っていない）。書き込みは別スレッドにする（本文が大きいとパイプバッファでブロックし、直後の `proc.stdout` の読み取りと双方向でデッドロックするため）。
                        def _write_stdin(_p=proc, _text=_stdin_text):
                            try:
                                _p.stdin.write(_text)
                            except Exception:
                                pass
                            finally:
                                try:
                                    _p.stdin.close()
                                except Exception:
                                    pass
                        threading.Thread(target=_write_stdin, daemon=True).start()
                        # 途中停止の有無によらず常に起動する（別グループの子が pipe を握って離さないケースの pipe 閉じ役も兼ねる・`_spawn_stop_watcher` 参照）。
                        _spawn_stop_watcher(proc, ctx.stop_event, _reap_lock, _reaped)
                        if _wall_clock_limit_s > 0:
                            # 1ターン全体（自動継続込み）の壁時計上限。継続 attempt も最初の Popen 直前に確定した `_agent_start_mono` からの残り時間で打ち切る。
                            _spawn_wall_clock_watcher(
                                proc, _agent_start_mono + _wall_clock_limit_s, _reap_lock, _reaped,
                                _wall_clock_state)
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
                            if e.get("type") == "thread.started":  # session/thread id 捕捉（resume 先の id）
                                thread_id = e.get("thread_id") or thread_id
                                continue
                            if e.get("type") == "turn.completed":  # ターンのトークン使用量（item ではない）
                                _u = _usage_from_turn_completed(
                                    e, self.model,
                                    codex_model_provider="ollama" if self._ollama_base_url is not None else "openai",
                                    system_settings=self._system_settings)
                                # 自動継続の attempt をまたいで合算する。
                                codex_usage = _accumulate_codex_usage(codex_usage, _u)
                                continue
                            if e.get("type") in ("turn.failed", "error"):  # 失敗終了の明示
                                _turn_failed = True
                                if _turn_failed_code is None:
                                    _err = e.get("error")
                                    _err_dict = _err if isinstance(_err, dict) else {}
                                    # `codex_error_info`（`error` dict 内・トップレベルのどちらでも拾う）は `error.code` より粒度が細かい診断コード（例 "context_window_exceeded"）。取れたら優先する。
                                    _info = _err_dict.get("codex_error_info") or e.get("codex_error_info")
                                    _turn_failed_code = (
                                        _info if isinstance(_info, str) and _info
                                        else (_err_dict.get("code") or e.get("code")))
                                    if not _turn_failed_code:
                                        # `codex_error_info` を持たない CLI 版のための救済。本文は保存もログ出力もせず、固定語彙へ分類するためだけに読む。
                                        _turn_failed_code = _classify_turn_failure(
                                            _err_dict.get("message") or e.get("message"))
                                    _log.warning("codex turn failed: code=%s conv=%s uid=%s",
                                                 _turn_failed_code, ctx.conversation_id, uid)
                                continue
                            item = e.get("item") or {}
                            it = item.get("type")
                            iid = item.get("id")
                            if not iid:  # id 無し item でも node を上書き衝突させない
                                iid = f"cx-auto-{node_n}"
                                node_n += 1
                            if _attempt_no > 1:  # 2回目以降の attempt は id 空間を分離する（前 attempt のノードを上書きしない）
                                iid = f"a{_attempt_no}-{iid}"
                            if it in ("web_search", "file_change"):  # ネイティブ Web 検索／ファイル変更もツール実行（継続打ち切り判定用・表示ノードは追加しない）
                                _attempt_ran_tools = True
                            if it == "command_execution":  # Codex 自身の grep/参照を逐次表示
                                ran = True
                                _attempt_ran_tools = True
                                label, detail = _humanize_cmd(item.get("command", ""))
                                if item.get("status") == "completed" or e.get("type") == "item.completed":
                                    ec = item.get("exit_code")
                                    yield _node(f"cx-{iid}", "tool", label,
                                                detail + (f"  → exit {ec}" if ec is not None else ""), "done")
                                else:
                                    yield _node(f"cx-{iid}", "tool", label, detail, "active")
                            elif it == "mcp_tool_call":  # Codex の MCP ツール呼びを可視化＋近傍を収集
                                ran = True
                                _attempt_ran_tools = True
                                tool = item.get("tool", "")
                                a = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}  # 非 dict 引数で落とさない
                                done = e.get("type") == "item.completed" or item.get("status") in ("completed", "failed")
                                # 並走計測（このプロセス内のみ・run 全体への合算は `_attempt` の finally）。id が無い item は対象外。初見かつ未完了のときだけ in-flight に加える（初見でいきなり完了した item は total には数えるが in-flight 幅には寄与しない）。再送は seen 済みなので二重に数えない。
                                _mcp_id = item.get("id")
                                # read 系ツールの引数から実際に読んだ資料の doc_id を集める（「参照した資料:」の記載漏れの補完）。読取が成功して完了した item だけ（失敗・エラー結果・進行中は出典にも根拠にも載せない）。
                                _read_ok = (e.get("type") == "item.completed"
                                            and item.get("status") not in ("failed", "error")
                                            and not (isinstance(item.get("result"), dict) and item["result"].get("isError"))
                                            and not item.get("error"))
                                if _read_ok and (not _mcp_id or _mcp_id not in _mcp_read_done):
                                    if _mcp_id:
                                        _mcp_read_done.add(_mcp_id)
                                    # 原本読取ツール（xlsx_range/docx_paragraphs/pptx_slides/pdf_pages/file_head）も doc_id 引数を取る読取ツールとして収集する。`xlsx_sheets` はシート一覧だけで本文を読んでいないため、`_mcp_listed_docs`（sources には合流するが sources_verified には数えない）へ分ける。
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
                                    # ask_user は question 優先（agentic の {"question":..}→return と同じ意味論）。確認ID 付き再送では無視し、1実行1回（codex_question is None で強制）。質問を捕まえたらループを抜け、finally で proc を後始末してから emit してターンを終える。
                                    if codex_question is None:
                                        codex_question = _codex_ask_capture(item, _ask_disabled)
                                    # 捕捉して break する場合は item.completed を待たずに抜けるため、実際の done フラグに関わらずノードを "done" で確定表示する。
                                    node_done = done or (codex_question is not None)
                                    yield _node(f"cx-{iid}", "tool", "ユーザに確認",
                                                f"「{str(a.get('prompt') or '確認が必要です')[:60]}」",
                                                "done" if node_done else "active")
                                    if codex_question is not None:
                                        break
                                    continue
                                # folder_tree/compare_documents は MCP 経由で Codex にも公開済み（`mcp_server.py::_tool_defs`）のため、表示用ラベル辞書にも対応を持たせる。
                                tlabel = {"graph_neighbors": "関係グラフをたどる", "ripgrep_search": "資料を検索（語句そのまま）",
                                          "es_search": "資料を検索（全文）", "read_around": "該当箇所を精読",
                                          "list_docs": "資料の一覧を確認", "folder_tree": "フォルダ構成を確認",
                                          "compare_documents": "世代間の差分を比較",
                                          "read_doc": "文書を通読", "doc_outline": "見出し構造を確認",
                                          "glob_search": "ファイル名で検索",
                                          # agentic_search の `_ORIGINAL_READ_LABELS`／改善ログの `_TOOL_CALL_LABELS` と同じ文言（`xlsx_sheets` はシート一覧のみで `xlsx_range` とは別ラベル・`_FILES_READ_LABEL` からも外れる）。
                                          "xlsx_sheets": "原本のシート一覧を確認", "xlsx_range": "原本を読む（Excel）",
                                          "docx_paragraphs": "原本を読む（Word）",
                                          "pptx_slides": "原本を読む（PowerPoint）",
                                          "pdf_pages": "原本を読む（PDF）",
                                          "file_head": "原本を読む（先頭）",
                                          # 調査台帳の MCP ツール（`mcp_server._LEDGER_TOOLS`）。引数の本文は出さず、件数・状態語彙（閉集合）だけを表示する。
                                          "ledger_manifest_set": "調査台帳に項目を登録",
                                          "ledger_item_put": "調査台帳の項目を更新",
                                          "ledger_status": "調査台帳の状態を確認",
                                          "ledger_review_put": "回答前に中間の見直しを記録"}.get(
                                              tool, "その他の処理")
                                if tool in _LEDGER_TOOL_DETAILS:
                                    detail = _LEDGER_TOOL_DETAILS[tool](a)
                                else:
                                    detail = "「" + str(a.get("name") or a.get("query") or a.get("doc_id")
                                                        or a.get("path_prefix") or a.get("name_pattern")
                                                        or a.get("pattern") or "") + "」"
                                if done and tool == "graph_neighbors" and item.get("status") == "completed":
                                    # 旧世代グラフの構造化エラー（`mcp_server.py::handle` が isError で返す）を先に見る。検知したら `_mcp_neighbors_from` は呼ばない。
                                    _era_err = _graph_schema_era_from_item(
                                        item, ctx.world, decision.get("lens") if decision else None)
                                    if _era_err is not None:
                                        _graph_schema_era_error = _era_err
                                    else:
                                        mcp_neighbors.extend(_mcp_neighbors_from(item))
                                yield _node(f"cx-{iid}", "tool", tlabel, detail, "done" if done else "active")
                            elif (it == "collab_tool_call" and e.get("type") == "item.completed"
                                  and item.get("tool") == "spawn_agent"):
                                # 子スレッド id の捕捉のみ（表示ノードは追加しない）。multi_agent 無効ではこのイベント自体が出ない。
                                for _tid in item.get("receiver_thread_ids") or []:
                                    if isinstance(_tid, str) and _tid:
                                        _child_thread_ids.add(_tid)
                            elif it == "reasoning" and e.get("type") == "item.completed":
                                txt = [ln for ln in (_strip_control_markers(ln).strip() for ln in
                                                     (item.get("text") or "").splitlines()) if ln]
                                if txt:
                                    yield _node(f"cx-{iid}", "think", "考える", txt[-1][:80], "done")
                            elif it == "agent_message" and e.get("type") in ("item.completed", "item.updated"):
                                # 最後の1件で上書きせず集める（完了分はリストへ・未完分は partial に保持）。結論の選択は loop 後に `_pick_codex_headline` で決定的に行う。
                                _txt = (item.get("text") or "").strip()
                                if e.get("type") == "item.completed":
                                    if _txt:
                                        _agent_msgs.append(_txt)
                                    _agent_partial = ""
                                else:  # item.updated＝成長中の未完 message（打ち切り保険）
                                    _agent_partial = _txt
                    except Exception:
                        _stream_error = True
                    finally:
                        # このプロセスで観測した分だけ run-level へ合算する（例外で打ち切られても、見えていた分は計測に残す）。
                        _mcp_calls["total"] += len(_attempt_mcp_seen)
                        _mcp_calls["max_in_flight"] = max(_mcp_calls["max_in_flight"], _attempt_mcp_max_in_flight)
                        if proc:
                            try:
                                _killpg(proc)  # group ごと（MCP child 含む）確実に後始末
                            except Exception:
                                pass
                            # proc（このセッションのリーダー）を `wait()` で回収する前に呼ぶ（回収後は pid が再利用されうるため）。監視スレッドと直列化するため `_reap_lock` の中で行う。
                            with _reap_lock:
                                _kill_session(proc.pid)  # setpgid で group を抜けた孤児を session 単位で回収
                                try:
                                    proc.wait(timeout=5)
                                except Exception:
                                    pass
                                _reaped["done"] = True
                            attempt_returncode = proc.returncode  # fallback 判定の材料（回収後の値）
                            # 利用統計 activity: 直近の wait 完了直後を終端にする（attempt ごとに更新＝最後の attempt の終端が残る）。
                            _agent_end_mono = time.monotonic()
                        if _stderr_f is not None:
                            _log_startup_stderr(_stderr_f, attempt_returncode, got_any_line,
                                                ctx.conversation_id, uid)

                def _absorb_last_message_fallback() -> None:
                    """attempt が `--json` に agent_message を出さず `-o` 最終メッセージファイルにだけ結論を書いたケースを拾う（毎 attempt 終了直後に呼ぶ）。`_last_message_path` は attempt をまたいで使い回すため、最新 attempt の分（`_agent_msgs[_attempt_msgs_start:]`）と同一（strip 比較）なら追加しない（過去の attempt と同文だからと落とすと、この attempt の結論が判定対象から消える）。"""
                    _fb = _read_last_message_fallback(_last_message_path)
                    if _fb and _fb == _stale_last_message:
                        return
                    if _fb and _fb.strip() not in {m.strip() for m in _agent_msgs[_attempt_msgs_start:]}:
                        _agent_msgs.append(_fb)

                def _continuation_msgs() -> list[str]:
                    """`_needs_continuation` へ渡す completed message＝最新 attempt の分（`_agent_msgs[_attempt_msgs_start:]`）。その attempt が agent_message を1つも出さず（`_agent_partial` も空）に終わった場合は、それ以前の蓄積（`_agent_msgs` 全件）で判定する。"""
                    latest = _agent_msgs[_attempt_msgs_start:]
                    return latest if (latest or _agent_partial) else _agent_msgs

                def _update_structured_state() -> None:
                    """`_schema_on` のとき、最新 attempt の最終出力（`-o` を第一候補・無ければ最後の完了 agent_message）を `_parse_structured` で検証し、`_latest_structured` を更新する（継続要否の判定はこの1件だけを見る）。毎 attempt 終了直後に呼ぶ。`_schema_on` が偽なら何もしない。
                    見出し候補（`_structured_answers`）には最新 attempt の agent_message 全件を順に検証して合格したものを積む（後続の message が壊れていても先の有効な `final` を失わないため）。最終候補（`-o` 優先）は、最後の agent_message と同一テキストでない別ソースのときだけ追加で積む。
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
                        _fb = None  # `_absorb_last_message_fallback` と同じ staleness 規則
                    _last_msg = _latest_msgs[-1] if _latest_msgs else None
                    _final_text = _fb or _last_msg
                    _latest_structured = _parse_fn(_final_text) if _final_text else None
                    if _latest_structured is not None and _final_text != _last_msg:
                        _structured_answers.append(_latest_structured)

                def _continuation_pending() -> bool:
                    """継続要否。`_schema_on` は最新 attempt の構造化出力の status で判定する（無し/不正/`in_progress` はすべて未完了）。無効時は平文ヒューリスティック。"""
                    if _schema_on:
                        return _latest_structured is None or _latest_structured["status"] == "in_progress"
                    return _needs_continuation(_continuation_msgs(), _agent_partial)

                def _candidate_final() -> dict | None:
                    """`_structured_answers_valid_from` 以降で最後の `final`（見出し/主張として採用しうる候補）。台帳ゲートは `_latest_structured` ではなくこの候補の有無で判定する（final の後に in_progress が出ても、その final は見出し選択の対象になり得るため）。`_pick_structured_headline`/`_pick_structured_claims` もこの関数を使い、ゲートと見出し選択が別々の final を見ないようにする。"""
                    # 回答の後に届いた通知（`<subagent_notification>`）への短い返事も `final` で来る。最後の1件を鵜呑みにせず、出典の行（『参照した資料』）を持つ最後の final、次に主張（`claims`）を持つ最後の final を優先する。
                    _finals = [s for s in _structured_answers[_structured_answers_valid_from:]
                               if s.get("status") == "final"]
                    for _has in (lambda s: "参照した資料" in (s.get("answer") or ""),
                                 lambda s: bool(s.get("claims"))):
                        for s in reversed(_finals):
                            if _has(s):
                                return s
                    return _finals[-1] if _finals else None

                def _pick_structured_headline() -> str | None:
                    """`_schema_on` の見出し。構造化 message に `final` があれば最後の `final` の `answer`、無ければ最後の構造化 message の `answer`。構造化 message が一度も無くても何か出力はあった（`_agent_msgs`/`_agent_partial` が非空）ときだけ固定文言（`_codex_stopped_early` が立つ）。出力そのものが無ければ None（無出力失敗の判定へ委ねる）。"""
                    # `answer` が空の構造化 message は「本文なし」＝固定文言に落とす（空文字を返すと dispatch の決定的見出しが正常回答として出てしまう）。`_structured_answers_valid_from` より前（台帳ゲートが拒否した final を含む）は候補にせず、台帳ゲートを通った final（`_candidate_final`）を優先する。
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
                    """子（worker/evaluator）のサイドカーを毎 attempt 終了直後（自動継続の判定より前）に取り込む（待つと、子の `ask_user` があっても親が in_progress のまま自動継続を回してしまう）。読んだ doc_id は `_mcp_read_docs`/`_mcp_listed_docs` へ重複なく合流させる。
                    ガードは `_sidecar_path`（決定済みか）と `_sidecar_init_ok`（このターンの初期化が無事終わったか）の2つ。sandbox 有効時は事前 unlink・設定生成が両方成功した時、非サンドボックス経路は `.tmp/` への env 配線が済んだ時に `_sidecar_init_ok` を立てる。
                    非サンドボックス経路（`codex_home is None`）は model-shell が同じファイルへ偽の行を追記し得るため、読取記録（read/listed）・確認カード（ask_user）・障害コード（error）は吸収せず、数値の計数（limit）だけを取り込む。
                    `_sidecar_init_ok` は、前ターンの残骸 unlink 失敗や設定生成失敗の際に、finally の無条件呼び出しが残骸や存在しないサイドカーを読むのを防ぐ。
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
                        # サイドカーが model-shell から不可視（permission profile の `:root deny` 配下）のときだけ、出典・会話の制御に効く行を信用する。
                        for _d in _sc_reads:
                            if _d not in _mcp_read_docs:
                                _mcp_read_docs.append(_d)
                        for _d in _sc_listed:
                            if _d not in _mcp_listed_docs and _d not in _mcp_read_docs:
                                _mcp_listed_docs.append(_d)
                        if codex_question is None and _sc_ask is not None and not _ledger_review_reverted:
                            codex_question = _sc_ask
                    # `_read_mcp_sidecar` は毎回先頭から全件を読み直すため、`+=` で加算せず最新の全量スナップショットで上書きする（二重計上しない）。
                    _mcp_tool_result_clipped = _sc_limits.get("tool_result_clipped", 0)
                    if _sc_limits.get("total_budget_hit"):
                        _mcp_total_budget_hit = True
                    _mcp_duplicate_tool_call = _sc_limits.get("duplicate_tool_call", 0)
                    _mcp_search_truncated = _sc_limits.get("search_truncated", 0)
                    if _sc_limits.get("tool_calls_exhausted"):
                        _mcp_tool_calls_exhausted = True

                def _pick_structured_claims() -> list[dict]:
                    """`_pick_structured_headline`（`_candidate_final`）と同じ選び方（`final` を優先・無ければ最後の構造化 message）で、その message の `claims` を返す（v1 形・`_schema_v2` 無効時は空リスト）。台帳ゲートを通っていない final の claims も `_candidate_final` の境界で除外する。"""
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
                    # AGENTS.md はベストエフォート（書けなくても Codex 実行は継続・fail-open）。気づけるよう warning は残す（containment/grounding の短縮形はプロンプトにも置いてある）。
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
                    # スキル配備もベストエフォート（fail-open）。knowledge=ON の Codex 実行全部で配備する（author に限定しない）。plain は investigate-* スキルを置かない（xlsx/docx/pptx/marp は配備する）。
                    try:
                        codex_skills.deploy_skills(run_dir, uid, users_dir,
                                                   skip_prefix="investigate-" if _plain else None)
                    except Exception as e:
                        _log.warning("skills deploy failed (fail-open): %s", e)
                    # profile config はここで書く（FileExistsError 等は fail-closed で例外→answer=None→CODEX_HOME 削除→決定的回答）。marp/Chromium を read root に足す必要は無い（レンダは Sherpa 本体側）。
                    # 会話ごとの CODEX_HOME は毎ターン再利用するため、前ターンの config.toml（creds を含む）の残骸が無いことを確認してから書く。
                    if codex_home is not None:
                        try:
                            (codex_home / "config.toml").unlink(missing_ok=True)
                        except Exception:
                            pass
                        # 永続 CODEX_HOME は毎ターン再利用するため、前ターンのサイドカー残骸を吸収しないよう config.toml と同じくここで空から始める（`missing_ok=True`）。unlink が失敗したら、このターンは `_absorb_mcp_sidecar` を無効のままにする（`_sidecar_init_ok` を立てない）。本文・パスは伏せ、例外型と errno だけ warning に残す。
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
                        # ここまで（事前 unlink・設定生成）が両方例外なく終わって初めて、このターンのサイドカー吸収を許可する。
                        if _sidecar_unlink_ok:
                            _sidecar_init_ok = True
                        # 接続先が Azure 等へリダイレクトされて web_search が強制 OFF になっている時だけ、理由を1回（このターンにつき1回）伝える。Codex(Ollama) 構成は対象外。
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
                    # この attempt が捕捉した thread_id を控える（直後の resume 失敗判定で `thread_id` が None へリセットされる前に控える）。
                    if thread_id and thread_id not in _all_parent_thread_ids:
                        _all_parent_thread_ids.append(thread_id)
                    # resume を試みて1行も --json イベントが出なかった（セッション消失等）場合、履歴 priming 済みのプロンプトで新規セッションへ即座にフォールバックする。ask_user 確認で終了した/途中停止されたターンは再試行しない。
                    # 「非ゼロ終了かつ agent_message が1つも無い」場合も resume 失敗とみなす。retry は resume 試行時に1回だけ。
                    _stopped = (ctx.stop_event is not None and ctx.stop_event.is_set()) or _wall_clock_state["hit"]
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
                        # 失敗した resume attempt の構造化状態（`_latest_structured`・`_structured_answers`）は新規セッションへ持ち越さない。
                        _structured_answers.clear()
                        _structured_answers_valid_from = 0
                        _latest_structured = None
                        _resume_fallback_happened = True
                        yield from _attempt(False)
                        _absorb_last_message_fallback()
                        _update_structured_state()
                        _absorb_mcp_sidecar()
                        # 利用統計 activity: フォールバック後の新しい thread_id も控える（前段の attempt 分と合わせて2件を1つの parent エントリへ合算する）。
                        if thread_id and thread_id not in _all_parent_thread_ids:
                            _all_parent_thread_ids.append(thread_id)
                    # 自動継続（「途中経過で止まった」を検出）と台帳ゲート（「`final` だが台帳が未完了」を検出）は1つの while ループで扱う。毎周「今の状態がどちらの継続を必要とするか」を判定し直す。上限は別枠（`_continue_limit`＝自動継続／`_LEDGER_CONTINUE_CAP`＝台帳／`_LEDGER_REVIEW_CAP`＝見直しの一巡）。優先順位: 台帳未完了（`final` かつ未完了）＞ 見直しの一巡（台帳完了直後・上限内の1回）＞ 途中経過（`final` でない）。
                    # 台帳ゲート分岐のトリガーは `_latest_structured` ではなく `_candidate_final()`（`_pick_structured_headline` と同じ採用候補の選び方）を使う（ゲートを通っていない final が受理されるのを防ぐ）。
                    _continue_limit = _env_int("SHERPA_CODEX_AUTO_CONTINUE", 3, 0, 5)
                    _ledger_manifest_retry_used = False
                    _ledger_no_progress_streak = 0
                    # attempt の種類を問わず、ループの各周の先頭で `_ledger_progressed`（前周と今周の `LedgerSnapshot` の比較）を見て、進捗があれば streak をリセットする（自動継続の間に起きた進捗を見逃さないため）。
                    _ledger_prev_snapshot: investigation_ledger.LedgerSnapshot | None = None
                    # 見直し（`reviews.jsonl`）の件数も進捗の判定材料に加える（見直しだけでは item の状態が変わらず `_ledger_progressed` が検知できないため）。
                    _ledger_prev_reviews: tuple[dict, ...] = ()
                    while True:
                        _stopped_for_continue = (
                            (ctx.stop_event is not None and ctx.stop_event.is_set())
                            or _wall_clock_state["hit"])
                        if codex_question is not None or _stopped_for_continue:
                            break
                        if not (_session_persistence_enabled and (thread_id or resume_sid)):
                            break
                        # 台帳の書込手段は MCP の台帳ツールだけのため、MCP 無効の構成では指示文と同じ条件でゲートも無効にする。
                        _ledger_gate_active = _schema_v2 and mcp and _investigation_dir is not None
                        _ledger_verdict = None
                        if _ledger_gate_active:
                            _ledger_snapshot = investigation_ledger.load_ledger(_investigation_dir)
                            _ledger_reviews = (investigation_ledger.load_reviews(_investigation_dir)
                                               if _ledger_require_review else ())
                            _ledger_verdict = investigation_ledger.ledger_complete(
                                _ledger_snapshot, required_extra=_ledger_required_extra,
                                reviews=_ledger_reviews, require_review=_ledger_require_review,
                                require_continuation_resolved=_ledger_require_continuation_resolved)
                            if (_ledger_prev_snapshot is not None
                                    and (_ledger_progressed(_ledger_prev_snapshot, _ledger_snapshot,
                                                            required_extra=_ledger_required_extra)
                                         or len(_ledger_reviews) > len(_ledger_prev_reviews))):
                                _ledger_no_progress_streak = 0
                            _ledger_prev_snapshot = _ledger_snapshot
                            _ledger_prev_reviews = _ledger_reviews
                        _has_candidate_final = _candidate_final() is not None
                        if _ledger_gate_active and _has_candidate_final:
                            # ---- 台帳ゲート分岐（`_schema_v2` のときだけ効かせる）----
                            if _ledger_verdict.complete:
                                # ---- 見直しの一巡 ----
                                # 完了と判定した回答の候補を受け取る前に、上限に達していなければ一度だけ Codex へ続きを頼む（台帳継続と同じ resume の流儀・別枠の上限 `_LEDGER_REVIEW_CAP`＝頼んだ回数で数える）。
                                if _ledger_review_requests >= _LEDGER_REVIEW_CAP:
                                    break
                                # 前の見直し以後が失敗・未完了・悪化のまま台帳が完了した場合は、次の見直しを頼まず、ループ後の安全弁で前の見直しの直前へ戻す。
                                if (_ledger_review_pre_candidate is not None
                                        and (_turn_failed or _continuation_pending()
                                             or _ledger_review_is_worse(_ledger_review_pre_candidate,
                                                                        _candidate_final()))):
                                    break
                                _ledger_review_requests += 1
                                _ledger_review_attempted = True
                                _ledger_review_pre_candidate = _candidate_final()
                                _ledger_review_pre_len = len(_structured_answers)
                                _ledger_review_pre_valid_from = _structured_answers_valid_from
                                _ledger_review_pre_turn_failed = _turn_failed
                                _ledger_review_pre_turn_failed_code = _turn_failed_code
                                _ledger_review_pre_attempt_msgs_start = _attempt_msgs_start
                                _ledger_review_pre_latest_structured = _latest_structured
                                _ledger_review_pre_codex_question = codex_question
                                _ledger_review_ids_before = (
                                    set(_ledger_snapshot.manifest["items"])
                                    if _ledger_snapshot.manifest is not None else set())
                                yield _node(
                                    f"ledger-review-{_ledger_review_requests}", "think", "回答前の点検",
                                    f"確定する前に見直します（{_ledger_review_requests}/"
                                    f"{_LEDGER_REVIEW_CAP}）", "done")
                                yield from _attempt(True, prompt_text=_LEDGER_REVIEW_PROMPT)
                                _absorb_last_message_fallback()
                                _update_structured_state()
                                _absorb_mcp_sidecar()
                                _ledger_review_snapshot_after = investigation_ledger.load_ledger(
                                    _investigation_dir)
                                _ledger_review_ids_after = (
                                    set(_ledger_review_snapshot_after.manifest["items"])
                                    if _ledger_review_snapshot_after.manifest is not None else set())
                                _ledger_review_added_ids = _ledger_review_ids_after - _ledger_review_ids_before
                                if _ledger_review_added_ids:
                                    # 目録が増えた＝見直した: 台帳継続へ戻り、増えた項目を調べさせてから改めて完了判定へ戻る。`_ledger_review_pre_candidate`/`_ledger_review_pre_len` はループを抜けるまで保持し、悪化していればループ後の安全弁で切り戻す。
                                    _ledger_review_rounds += 1
                                    _ledger_review_items_added += len(_ledger_review_added_ids)
                                    continue
                                # 目録が増えず、見直しの実行自体が失敗/未完了/確認で終わったらループを抜ける（ループ後の安全弁が見直し前へ戻す）。
                                if _turn_failed or codex_question is not None or _continuation_pending():
                                    break
                                # 目録は増えなかったが台帳が完了でなくなった（item を in_progress へ差し戻した等）ときは、通常の台帳ゲートへ戻す（打ち切り理由を "cap" にしない）。
                                _ledger_review_verdict_after = investigation_ledger.ledger_complete(
                                    _ledger_review_snapshot_after, required_extra=_ledger_required_extra,
                                    reviews=(investigation_ledger.load_reviews(_investigation_dir)
                                            if _ledger_require_review else ()),
                                    require_review=_ledger_require_review,
                                    require_continuation_resolved=_ledger_require_continuation_resolved)
                                if not _ledger_review_verdict_after.complete:
                                    continue
                                break
                            # 台帳起因の催促（未完了／内容不正／不存在の3種）はどれも上限を超えたら発行しない。「不存在は1回だけ」は cap の内側の追加制約（cap 到達後は催促せず cap で受理する）。
                            if _ledger_continuations >= _LEDGER_CONTINUE_CAP:
                                _investigation_stopped_reason = "cap"
                                break
                            if _ledger_verdict.manifest_invalid:
                                # 「ファイル不存在」（台帳を作らない依頼＝1回だけ催促して受理・fail-open）と「ファイルは存在するが内容が規約に合わない」（壊れた台帳＝cap まで修復を促す）を区別する。`_retire_investigation_ledger` と同じ「ファイル存在」の基準を使う。
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
                                # この継続を「進捗なし」の1回として仮計上する（次周の先頭の進捗比較で何か終端化していれば 0 へ戻る）。
                                _ledger_no_progress_streak += 1
                            # この final は拒否済み＝以後の見出し/主張候補から除外する（`_pick_structured_headline`/`_pick_structured_claims` はこの境界より後だけを見る）。
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
                        # ---- 自動継続分岐（「途中経過で止まった」を検出）----
                        # 正常終了（returncode 0）で agent_message が「作業宣言だけ」（結論文が1つも無い、または構造化出力が in_progress/不正）なら、Codex セッションの続きを自動で呼ぶ。`got_any_line` を要求するのは、継続 attempt 自身が無出力で終わったときに古い作業宣言だけを根拠に空振りを繰り返さないため。判定は `_continuation_msgs()`（直前の attempt の message だけ）で行う。
                        if not (attempt_returncode == 0 and got_any_line and _continuation_pending()):
                            break
                        if _auto_continue_count >= _continue_limit:
                            break
                        # この if を通過＝実際に continuation attempt を1回発行する（limits 計測）。
                        _auto_continue_count += 1
                        yield _node(f"cx-continue-{_auto_continue_count}", "think", "続きを実行",
                                   f"途中経過で止まったため続きを調べます（{_auto_continue_count}/"
                                   f"{_continue_limit}）", "done")
                        yield from _attempt(
                            True, prompt_text=_CONTINUE_PROMPT_SCHEMA if _schema_on else _CONTINUE_PROMPT)
                        _absorb_last_message_fallback()
                        _update_structured_state()
                        _absorb_mcp_sidecar()
                        # ツールを1つも呼ばずに終わった continuation は打ち切る（同じ宣言の空振りを繰り返さない）。ただし、これがまだ `_continuation_pending()` かつ final 候補が無い場合だけ。`_candidate_final()` で final 候補の有無を確認し、候補があるならループ先頭の台帳ゲートへ戻す（台帳ゲートを通っていない final が採用されるのを防ぐ）。
                        if (not _attempt_ran_tools and _continuation_pending()
                                and _candidate_final() is None):
                            break
                    # 見直しの一巡の安全弁（悪化したら見直し前の回答を使う）: 最後の見直し以後（見直しの実行と、目録が増えたときの台帳継続）が失敗・未完了・確認・回答なし・悪化で終わったら、見直しの直前（台帳ゲートを通った完成回答）へ戻す。見直し前の完成回答は `_structured_answers[:_ledger_review_pre_len]` に残っているので、それより後を切り、有効範囲の起点も見直し前へ戻す。見直しで足した項目はディスクの台帳に残り、調べ終わっていなければ「確認できなかった項目」の節に出る。
                    if _ledger_review_pre_candidate is not None:
                        _ledger_review_post = _candidate_final()
                        if (_ledger_review_post is None or _turn_failed or codex_question is not None
                                or _continuation_pending()
                                or _ledger_review_is_worse(_ledger_review_pre_candidate, _ledger_review_post)):
                            del _structured_answers[_ledger_review_pre_len:]
                            _structured_answers_valid_from = min(
                                _structured_answers_valid_from, _ledger_review_pre_valid_from)
                            _turn_failed = _ledger_review_pre_turn_failed
                            _turn_failed_code = _ledger_review_pre_turn_failed_code
                            _attempt_msgs_start = _ledger_review_pre_attempt_msgs_start
                            _latest_structured = _ledger_review_pre_latest_structured
                            codex_question = _ledger_review_pre_codex_question
                            _ledger_review_reverted = True
                    # 台帳の最終判定: `_schema_v2` が有効なときだけ記録する。ゲート自体が1回も継続を発行しなくても（resume 不能・ask_user/停止で打ち切り等）、受理する回答の実状を記録する（`env["investigation"]`／退避判定の根拠）。
                    if _schema_v2 and mcp and _investigation_dir is not None:
                        # 完了判定の前に、coverage.jsonl の記録に基づき `not_found_in_scope` を必要なら `unverified` へ機械的に置き換える（on-disk の item を書き換える・確認済みの状態は変えない）。以降の再読込はこの書換え後の状態を見る。
                        _investigation_snapshot = investigation_ledger.apply_unverified_downgrades(
                            _investigation_dir, investigation_ledger.load_ledger(_investigation_dir),
                            exclude_ids=_investigation_pre_turn_not_found_ids)
                        # 中間の見直し（本文を持つため台帳 snapshot とは別に読む）。末尾の「追加で調べますか？」注記と退避の判定の両方が `investigation_ledger.pending_continuation_review()` 経由で使う。
                        _investigation_reviews = (investigation_ledger.load_reviews(_investigation_dir)
                                                  if _ledger_require_review else ())
                        _review_continuation_note_text = _review_continuation_note(_investigation_reviews)
                        _investigation_verdict = investigation_ledger.ledger_complete(
                            _investigation_snapshot, required_extra=_ledger_required_extra,
                            reviews=_investigation_reviews, require_review=_ledger_require_review,
                            require_continuation_resolved=_ledger_require_continuation_resolved)
                        if _investigation_stopped_reason is None:
                            if _investigation_verdict.complete:
                                _investigation_stopped_reason = "complete"
                            elif _investigation_verdict.manifest_invalid:
                                _investigation_stopped_reason = "ledger_missing"
                            else:
                                # ゲート未実行（`_schema_v2` 無効・resume 不能等）で打ち切り条件のどれにも該当しないまま終わった残余ケースは、「これ以上は続けられない」として cap 側へ寄せる（4分類のみ使う）。
                                _investigation_stopped_reason = "cap"
                except Exception:
                    answer = None
                    _stream_error = True
                finally:
                    # サイドカーは codex_home の削除・後始末より前に必ず一度吸収する（`_attempt()` の途中で例外が起きた経路の取りこぼし防止・fail-open）。
                    _absorb_mcp_sidecar()
                    if codex_home is not None:
                        # 利用統計 activity の対象親（このターンで使った全ての親・resume 失敗のフォールバックで切り替わった分も含む）。現在の `thread_id` が記録漏れで欠けていない保険としてここでも足す。
                        _activity_parent_ids = list(_all_parent_thread_ids)
                        if thread_id and thread_id not in _activity_parent_ids:
                            _activity_parent_ids.append(thread_id)
                        if _child_thread_ids or thread_id or _activity_parent_ids:
                            # 子の session JSONL（`sessions/**`）は非永続セッションだと直後の rmtree で消えるため、削除より前に読む。読めなくても fail-open。`thread_id`（今回の親）だけでも呼ぶ（`_child_thread_ids` が空でも `parent_thread_id` 突合で子を拾える）。
                            try:
                                (_child_usage_totals, _child_usage_found, _child_usage_missing,
                                 _child_usage_detected) = (
                                    _collect_child_token_usage(codex_home, _child_thread_ids, thread_id,
                                                               _turn_started_wall))
                            except Exception:
                                pass
                            # 利用統計 activity: codex_home 削除より前に、同じ fail-open 方針で要約する（読めなくても本体ターンは落とさず、型名/errno だけ warning ログに出す）。settings は開始行の直後で確定済みの値を渡す。prepare/agent はここで初めて確定する。prepare は `ctx.turn_started_mono`／`_agent_start_mono` のどちらかが無ければキー自体を置かない（0埋めしない）。
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
                            # セッション実体（`sessions/` の JSONL）は次ターンの resume のために保持する。creds を含む config.toml・`auth.json`（実 `~/.codex/auth.json` への symlink）・サイドカーは毎ターン削除する（次ターンは再作成する・前ターンの読取記録を持ち越さない）。
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
                            # per-request CODEX_HOME（profile＋auth symlink）を後始末する（symlink の指す先は消えない）。サイドカーはこの codex_home 配下なので rmtree で併せて消える。
                            try:
                                shutil.rmtree(codex_home, ignore_errors=True)
                            except Exception:
                                pass
                # サブプロセス後始末の直後、以降のどの分岐（schema-era エラーの re-raise・ask_user の早期 return・通常終了）を通っても必ず1回だけ出す（MCP ツールの並走計測・「1実行あたり1行」を保つため早期 return より前に置く）。UI・env には載せない。
                _log.info("codex mcp calls: total=%d max_in_flight=%d conv=%s uid=%s",
                          _mcp_calls["total"], _mcp_calls["max_in_flight"], ctx.conversation_id, uid)
                # codex.log 終了行（1実行1回・本文/資料名は書かない）。usage 合計は turn.completed の最新 snapshot（セッション累計であり足し算ではない）。
                _codex_usage_total_tokens = (
                    (codex_usage.get("input_tokens", 0) + codex_usage.get("output_tokens", 0))
                    if codex_usage else 0)
                # 本文・資料名・秘密は載せない（固定語彙のコードと件数・所要時間だけ）。トークン内訳は親（`codex_usage`）・子合算（`_child_usage_totals`）ともこのターンで確定済みの集計値をそのまま出す（cached は input に、reasoning_output は output に包含される内訳・二重計上しない）。
                # 台帳の終了状態は env["investigation"]["stopped_reason"] の4値より粗い3値（missing/complete/incomplete）。詳細（no_progress/cap 等）は env 側にだけ持つ。
                if not _schema_v2:
                    _ledger_log_state = "off"  # 台帳を使わない構成（Codex(Ollama)・素の Codex 等）
                elif _investigation_verdict is None or _investigation_verdict.manifest_invalid:
                    _ledger_log_state = "missing"
                elif _investigation_verdict.complete:
                    _ledger_log_state = "complete"
                else:
                    _ledger_log_state = "incomplete"
                # claims_downgraded（末尾ログ用）: この行は `if answer:` 分岐（claims 確定）より前に出るため、ここでも `_claims_vs_ledger` を呼んで求める。根拠種別ゲートを経ていない生の confirmed 主張が対象のため、実際に envelope へ適用される件数の上限値になる。
                _claims_for_log = _pick_structured_claims() if _schema_on else []
                _, _log_ledger_check = _claims_vs_ledger(
                    _claims_for_log,
                    investigation_ledger.load_ledger(_investigation_dir)
                    if _investigation_dir is not None
                    else investigation_ledger.LedgerSnapshot(manifest=None, items={}, invalid_ids=()),
                    # `_retire_investigation_ledger`/台帳ゲート本体と同じ「ファイル存在」基準（`load_ledger` の `manifest=None` だけでは「ファイル無し」と「内容不正」を区別できない）。
                    manifest_file_exists=(
                        (_investigation_dir / "manifest.json").is_file()
                        if _investigation_dir is not None else False))
                _claims_downgraded_for_log = _log_ledger_check.get("downgraded", 0)
                # 確認できなかった item の件数（`unverified`/`not_found_in_scope`/`unreadable`/`unavailable`・降格適用後）を codex.log 終了行へ残す（本文・subject は含めず件数のみ）。
                _ledger_unconfirmed_for_log = (
                    sum(_investigation_verdict.terminal_counts.get(s, 0)
                       for s in investigation_ledger.UNCONFIRMED_STATUSES)
                    if _investigation_verdict is not None else 0)
                # exec_failed/sandbox_failed（サンドボックスの実行中検知）。`env["activity"]` は直前の finally ブロックで確定済み（未確定＝要約自体が失敗したターンは `exec_failure_counts(None)` が (0, 0) を返す）。
                _exec_failed_for_log, _sandbox_failed_for_log = (
                    _codex_exec_failure_counts(env.get("activity")))
                _log_codex.info(
                    "end conv=%s uid=%s returncode=%s thread_id=%s events=%s mcp_calls=%d "
                    "spawn_agents=%d usage_tokens=%d input=%d cached_input=%d output=%d "
                    "reasoning_output=%d child_input=%d child_cached_input=%d child_output=%d "
                    "child_reasoning_output=%d children_found=%d children_missing=%d "
                    "error_code=%s clipped=%d budget_hit=%s elapsed=%.1fs ledger=%s continuations=%d "
                    "ledger_unconfirmed=%d claims_downgraded=%d exec_failed=%d sandbox_failed=%d "
                    "ledger_review_attempted=%s ledger_review_rounds=%d ledger_review_items_added=%d",
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
                    _ledger_unconfirmed_for_log, _claims_downgraded_for_log,
                    _exec_failed_for_log, _sandbox_failed_for_log,
                    _ledger_review_attempted, _ledger_review_rounds, _ledger_review_items_added)
                if _sandbox_failed_for_log:
                    # api.log（`_log`＝"sherpa" ロガー）へ一目で気付ける形で残す。本文・資料名は含めない（件数と会話IDのみ）。
                    _log.warning(
                        "Codex のサンドボックスでコマンドが失敗しています"
                        "（make doctor の「Codex のサンドボックス」を確認）conv=%s 回数=%d",
                        ctx.conversation_id, _sandbox_failed_for_log)
                # ask_user が出たターンは question 優先＝env/_result・成果物台帳登録を出さずここで終了する（回答は chat.js の整形再送＝新 codex exec で拾う）。proc は直上の finally で後始末済み。chat_service はこの question を answer.question として保存する。
                if codex_question is not None:
                    # 親ノード（"Codex が調べる"）も "active" のまま止まっているため、通常経路の完了 yield と同様にここで "done" に確定させる。
                    yield _node("codex", "think", "Codex が調べる", "ユーザに確認するため終了しました", "done")
                    # `-o` 一時ファイル（last-message-*.txt）の削除を通常経路と同じ best-effort で先に消す（早期 return で .tmp/ に蓄積しないように）。
                    try:
                        _last_message_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    # 利用統計 activity: この経路は env/_result を出さないため、finally で確定済みの `env["activity"]` は question イベント経由で運ぶ（運ばないと chat_service の確認カード保存側が source:"none" で作り直す）。
                    if env.get("activity") is not None:
                        codex_question["activity"] = env["activity"]
                    yield codex_question
                    return
                # 調査台帳（回答 envelope への記録）: 本文・path は含めず、id 一覧は先頭50件に打ち切る。ゲートが1度も走らなかった（`_investigation_dir` が未確定＝早期 return 済み）ターンには載せない。
                if _investigation_verdict is not None:
                    # 退避処理をここで先に実行して成否を確定し、`retained` に実際の値を書く（予測値ではない）。外側 finally はこの turn では二重に実行しない（`_investigation_retire_done` を見る・切断/例外時のフォールバックのみ）。復元が途中で失敗したターン（`_investigation_retire_done` は復元失敗時点で立っている）は退避を行わず元の退避台帳を保持する。
                    _investigation_retained = False
                    if _ledger_home is not None and not _investigation_retire_done:
                        # 退避の判断は `_retire_investigation_ledger` 内部で `pending_continuation_review()` を直接使う（単一の純関数に一本化）。
                        _investigation_retained = _retire_investigation_ledger(
                            _investigation_dir, _ledger_home, required_extra=_ledger_required_extra,
                            require_review=_ledger_require_review)
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
                        # 見直しの一巡の有無・回数・足した項目数（本文なし・件数だけ）。
                        "review": {
                            "attempted": _ledger_review_attempted,
                            "rounds": _ledger_review_rounds,
                            "items_added": _ledger_review_items_added,
                        },
                        # 中間の見直し（本体別枠・本文なし・件数と未充足 id だけ）。
                        "mid_review": {
                            "count": len(_investigation_reviews),
                            "missing": _investigation_verdict.review_missing,
                            "pending": list(_investigation_verdict.review_pending_ids[:_ledger_id_report_limit]),
                        },
                    }
                    env["limits"] = {**(env.get("limits") or {}),
                                     "ledger_incomplete": not _investigation_verdict.complete}
                    # この時点の台帳の正規形を env とは別項目に積む。本文・path は `_investigation_snapshot` 側の検証（`validate_item`）で除外済み。`load_coverage` は item ごとの outcome タプルのみ（本文を持たない）。`reviews`（本文を持つ・`validate_review_entry()` 済みの正規形のみ）も調査の記録の一部として積む（`investigation_record_render.py` の素材）。
                    _investigation_record_payload = {
                        "complete": _investigation_verdict.complete,
                        "manifest": _investigation_snapshot.manifest,
                        "items": dict(_investigation_snapshot.items),
                        "coverage": {k: list(v) for k, v in
                                    investigation_ledger.load_coverage(_investigation_dir).items()},
                        "reviews": list(_investigation_reviews),
                    }
                # `_schema_on` は構造化 message から見出しを選ぶ（生 JSON をそのまま出さない・平文ヒューリスティックへは戻さない）。無効時は現行どおりの選び方（下記）。
                if _schema_on:
                    answer = _pick_structured_headline()
                else:
                    # 集めた agent_message から結論を優先して headline を選ぶ（進行中の作業宣言を見出しにしない・最後の1件を鵜呑みにしない）。
                    _picked = _pick_codex_headline(_agent_msgs, _agent_partial,
                                                   prefer_marker="参照した資料") or None
                    # `-o` は保険。--json の agent_message から拾えなかった時だけ最終メッセージファイルを読む。使い終わったら必ず削除する（.tmp/ に溜め続けないため）。途中例外時は完全版が入り得る `-o` を先に試し、空/無いときだけ pick に委ねる。正常終了時は pick が主・`-o` は従。
                    if _stream_error:
                        answer = _read_last_message_fallback(_last_message_path) or _picked
                    else:
                        answer = _picked or _read_last_message_fallback(_last_message_path)
                try:
                    _last_message_path.unlink(missing_ok=True)
                except Exception:
                    pass
                # codex exec を実際に起動した（attempt_returncode is not None）のに stdout に JSON を1行も出さず（got_any_line=False）、answer も得られない場合だけ「正直に伝える」文言に切り替える。ユーザーの stop_event・壁時計上限（`_wall_clock_state`）による打ち切りは失敗ではないため対象外。
                _stopped_final = (ctx.stop_event is not None and ctx.stop_event.is_set()) or _wall_clock_state["hit"]
                # 最新 attempt（継続 attempt を含む）が `turn.failed`／`error` で閉じ、その attempt 自身は agent_message を1つも出さなかった（`_agent_msgs[_attempt_msgs_start:]` も `_agent_partial` も空）場合は、過去 attempt の古い回答が `answer` に残っていても明示失敗として扱う（古い途中経過を見出しにしないため）。利用者の明示停止は対象外。
                _turn_failed_no_new_message = (
                    _turn_failed and not _agent_msgs[_attempt_msgs_start:] and not _agent_partial
                    and not _stopped_final)
                if _turn_failed_no_new_message:
                    answer = None
                # `turn.failed`／`error` で閉じた attempt は、JSON が読めていても（got_any_line=True）agent_message が無いままの失敗のため、`_turn_failed` を OR で加える。
                if (not answer and (not got_any_line or _turn_failed) and attempt_returncode is not None
                        and not _stopped_final):
                    _codex_silent_failure = True
                # 自動継続を尽くしてもなお（上限0・セッション非永続・継続 attempt が無出力/異常終了を含む）作業宣言だけなら、本文（headline）は書き換えず印だけ立てる（「途中までの結果」と伝えて続きを促す）。利用者の明示停止は途中結果として扱わない。
                _codex_stopped_early = (
                    _continuation_pending()
                    and not _stopped_final and not _turn_failed_no_new_message)
                # run_dir の新規ファイルを検出して台帳登録する（personal_workspace_files に登録・ES/Neo4j には一切書かない）。Codex の cwd = run_dir のため、個人アップロード（files/）は読み取り・書き込み不可。
                if run_dir.is_dir():
                    # `.tmp`（TMPDIR）配下・`.agents`（配備したスキル）配下・ルート直下の AGENTS.md・`.mcp_sidecar.jsonl` は台帳登録しない（before 側と対）。
                    _after_ws_files = {
                        p for p in run_dir.rglob("*")
                        if p.is_file() and not p.is_symlink()
                        and p.relative_to(run_dir) not in (Path("AGENTS.md"), Path(_MCP_SIDECAR_NAME))
                        and not ({".tmp", ".agents"} & set(p.relative_to(run_dir).parts))
                    }
                    new_authoring = sorted(_after_ws_files - _before_ws_files)
                    if decision["lens"] != "author" and new_authoring:
                        # 作成の依頼（画面の「資料を作成」／依頼文が作成と判定）以外では成果物にしない。登録せず、run_dir の削除で一緒に消す。
                        _log.info("codex created %d file(s) in a non-authoring turn; discarded",
                                  len(new_authoring))
                        new_authoring = []
                    for fp in new_authoring:
                        codex_created_files.append(str(fp))
                    _any_new_ws = bool(new_authoring)
                    # Marp レンダは sandbox の外＝Sherpa 本体が network 隔離（unshare）下で実行する（Codex は .md を書くだけ）。ベストエフォート（fail-open）: 失敗しても .md 自体は台帳登録対象に入っている。
                    try:
                        from ... import marp_render
                        _mds = [p for p in new_authoring if p.suffix == ".md"]
                        _rendered = marp_render.render_outputs(
                            [p for p in _mds if marp_render.is_marp_markdown(p)],
                            marp_bin=_marp_bin(), chrome_path=_detect_chrome_path(),
                            theme_dirs=[run_dir / ".agents" / "skills" / "marp" / "themes",
                                        _SKILLS_BASE / "marp" / "themes"],
                            containment_root=run_dir)  # 入出力を run_dir 内実体に強制
                        codex_created_files.extend(str(p) for p in _rendered)
                        _any_new_ws = _any_new_ws or bool(_rendered)
                    except Exception as e:
                        _log.warning("marp_render: レンダ処理が例外で終了（fail-open）: %s", e)
            # 台帳登録（authoring/ の成果物を files/ に移動して台帳登録）。files/ に移すことで既存の grep/delete/TTL 機構をそのまま使う。authoring/ に中間生成物が残らないため、次回 Codex 実行時も個人ファイルは見えない。
            if codex_created_files and 'ws_authoring' in dir():
                try:
                    from ... import store as _store
                    import datetime as _dt
                    import shutil as _shutil
                    _ttl_days = workspace_limits.ttl_days()
                    _expires = (
                        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=_ttl_days)
                        if _ttl_days > 0 else None
                    )
                    # ws_files が有効（非 symlink）なら files/ に移動して登録する。symlink の場合は登録スキップ（fail-closed）。files/ 移動時に同名ファイルが存在する場合は別名化する（上書き禁止）。
                    _dest_dir = ws_files if (ws_files is not None and ws_files.is_dir()) else None
                    if _dest_dir is None:
                        # symlink or files/ が使えない → fail-closed（登録なし・grep/delete 対象外）。成果物は run_dir に残ったままなので、保存失敗として run_dir を消さず残し、回答へ注記する。
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
                                # 同名回避の名前確定も lock 内で行う（並行 HTTP upload と衝突して live ファイルを move で上書きするのを防ぐ）。候補名ごとに lock を取り、「物理未存在かつ生きた台帳なし」を確認できた名前にだけ move+登録する。
                                _i = 0
                                while _i <= 10000:  # 無限ループ防止
                                    _rel = _p.name if _i == 0 else f"{_stem}_{_i}{_suf}"
                                    _dst = _dest_dir / _rel
                                    with _store.workspace_file_lock(uid, _rel):
                                        if _dst.exists() or not _store.no_live_upload_for_path(uid, _rel):
                                            _i += 1
                                            continue  # この名前は埋まっている → 次 suffix へ
                                        _shutil.move(str(_p), str(_dst))
                                        try:
                                            _data = _dst.read_bytes()
                                            _sha = hashlib.sha256(_data).hexdigest()
                                            _row = _store.record_workspace_file(
                                                uid, _rel, str(_dst), len(_data), _sha, expires_at=_expires)
                                            _created_file_rows.append(_row)  # created_files カード用
                                        except Exception:
                                            # move は成功したが台帳登録に失敗した場合は、files/ に台帳の無い孤児を残さないよう同じ lock 内で run_dir 側へ戻す（回収は run_dir 保持側の責務に一本化する）。
                                            try:
                                                _shutil.move(str(_dst), str(_p))
                                            except Exception as _move_back_err:
                                                # 差し戻し（2回目の move）にも失敗した場合は、黙って握り潰さず明示的に記録する。`OSError`/`shutil.Error` は絶対パスを本文に含むため、相対パスと型・errno だけ残す。
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
                                # 通常の move 失敗も、差し戻し失敗と同じく型と errno だけ記録する（フルパスは出さない）。
                                _created_files_failed = True
                                _log.warning("codex created file move/registration failed for %s: type=%s errno=%s",
                                            _masked_run_dir_path(_fp, run_dir),
                                            type(e).__name__, getattr(e, "errno", None))
                except Exception as e:
                    # 個別ファイルのループへ入る前の設定段階（store import・_expires 計算等）の失敗。この時点で全件が run_dir に残ったまま。
                    _created_files_failed = True
                    _log.warning("codex created files registration setup failed (run_dir=%s): %s",
                                run_dir.name, e)
            # limits（利用統計「打ち切りの内訳」）: 自動継続はこの `run()` 自身が判定しているためここで数える（`search_truncated`／`tool_result_clipped`／`duplicate_tool_call` は `mcp_server.py` がサイドカー経由で報告した値を下で合流させる）。
            if _auto_continue_count and isinstance(env, dict):
                env["limits"] = {**(env.get("limits") or {}), "auto_continues": _auto_continue_count}
            # 深さ案内（`chat_service._depth_actually_helps`）が本体と同じ判定を使うための受け渡し口。`codex_multi_agent_enabled` の結果を usage の is_local から再現できない（Azure も既定 OpenAI と同じ "cloud"）ため、結果そのものを渡す。
            if isinstance(env, dict):
                env["codex_multi_agent"] = _multi_agent_enabled
            # グラフ・全文検索の縮退（親が検知した世代不一致＋子がサイドカーで報告した障害コード）を1つの印にまとめる。世代不一致が1件でもあれば「再取り込み待ち」を優先する。通知文言・グラフ側の計数は `chat_service._finalize`（`_apply_graph_degraded`）が env のこの印から組む。
            if isinstance(env, dict):
                # 事前検索（`chat_service._dispatch`）が既に世代不一致を立てていれば、子の接続断で上書きしない（「再取り込み待ち」を優先する）。
                _era = (_graph_schema_era_error is not None
                        or agentic_search.GRAPH_REINGEST_ERROR_CODE in _mcp_error_codes
                        or env.get("graph_degraded") == agentic_search.GRAPH_REINGEST_ERROR_CODE)
                if _era:
                    env["graph_degraded"] = agentic_search.GRAPH_REINGEST_ERROR_CODE
                elif "graph_unavailable" in _mcp_error_codes:
                    env["graph_degraded"] = "graph_unavailable"
                if any(c in _mcp_error_codes for c in ("es_unavailable", "es_query_failed")):
                    env["limits"] = {**(env.get("limits") or {}), "backend_unavailable_fulltext": True}
                # MCP ツール結果のバイト予算（per-call クリップ／累計予算到達）を利用統計「打切りの内訳」へ合流させる（`_CONTEXT_WINDOW_EXCEEDED_CODE` 分岐が既に `total_budget_hit` を立てていれば上書きしない）。
                if _mcp_tool_result_clipped:
                    env["limits"] = {**(env.get("limits") or {}),
                                     "tool_result_clipped": (env.get("limits") or {}).get(
                                         "tool_result_clipped", 0) + _mcp_tool_result_clipped}
                if _mcp_total_budget_hit:
                    env["limits"] = {**(env.get("limits") or {}), "total_budget_hit": True}
                # 同一クエリの重複実行の抑止（`mcp_server.py::_is_duplicate_tool_call`）の件数も同じ経路で合流させる。
                if _mcp_duplicate_tool_call:
                    env["limits"] = {**(env.get("limits") or {}),
                                     "duplicate_tool_call": (env.get("limits") or {}).get(
                                         "duplicate_tool_call", 0) + _mcp_duplicate_tool_call}
                # `run_tool()` 自身の内部切り詰め（grep ヒット上限・件数上限等）も同じ経路で合流させる（API 経路の `_record_run_tool_limits` と同じ語彙 `search_truncated`）。
                if _mcp_search_truncated:
                    env["limits"] = {**(env.get("limits") or {}),
                                     "search_truncated": (env.get("limits") or {}).get(
                                         "search_truncated", 0) + _mcp_search_truncated}
                # MCP ツール呼び出し回数の上限到達も同じ経路で合流させる（`total_budget_hit` と同じ bool 方式）。
                if _mcp_tool_calls_exhausted:
                    env["limits"] = {**(env.get("limits") or {}), "tool_calls_exhausted": True}
            # troubleshoot は Codex が実際に引いた近傍を UI カードにする（`_gather` 由来を Codex の実調査由来で上書きする）。
            _apply_codex_neighbors(env, mcp_neighbors, decision.get("lens") if decision else None)
            # turn.completed の usage は Codex CLI の契約でセッション累計（`codex exec resume` は前回までの累計を復元してから加算する）。累計値そのものは env["codex_usage_total"] に必ず残す（次ターンの差分計算の元・usage が取れなければ両方載せない）。resume が効いた（フォールバックしていない）ターンは、前ターンの累計（ctx.codex_usage_prev_total）との差分を answer.usage にする（session_id が今回の resume 先と一致する時だけ。新規セッション・フォールバック・prev 無し・session_id 不一致は累計をそのまま使う）。
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
                # 差分計算／累計そのものの両方に同じ深さメタを載せる（`_usage_depth_extra` は `_reason`/`_base_reason` 確定時に計算済み）。
                env["usage"].update(_usage_depth_extra)
                # 子スレッド（`spawn_agent`）の usage を加算する。`env["codex_usage_total"]`（次ターンの差分計算の元）は親のスナップショットのまま変えない（子の usage は別系統の累計のため）。合算は `env["usage"]`（このターンの表示・計上値）だけに行い、内訳（親／子／未取得件数）を別途残す。multi_agent 無効では `_child_usage_found`/`_child_usage_missing` が両方 0 のままで素通りする。
                # 本体ターンは巡ごとの内訳を取れないため、巡別の `chat-round` は記録しない。親＋子の usage 合計だけをこのターンの正本として `env["usage"]`/`sherpa.usage` ログに残す。
                if _child_usage_found or _child_usage_missing:
                    # 親分の内訳は `codex_usage`（セッション累計）ではなく `env["usage"]`（このターンの計上値）から作る（resume ターンでは `codex_usage` に前ターン分が混入するため）。
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
            # 捕捉した session/thread id を env に載せる（chat_service が `store.set_session_id` で永続化し次ターンの resume 判定に使う）。ゲートは `_session_persistence_enabled`（conversation_id あり かつ サンドボックス有効）を使う（`SHERPA_CODEX_SANDBOX=0` は使い捨て thread_id のため DB に保存すると次回の resume が必ず失敗する）。
            if _session_persistence_enabled and thread_id:
                env["codex_session_id"] = thread_id
            # Codex がファイルを作成した場合は env に記録する（chat_service が contains_personal を立てる）。files/ 外への書き込みも含めて codex_wrote_files フラグを立てる。
            if codex_created_files or _any_new_ws:
                env["codex_wrote_files"] = [Path(f).name for f in codex_created_files] or True
            # 台帳登録に成功したファイルを UI の「作成したファイル」カード用に env へ載せる（既存の /workspace/files DL API を再利用・rel_path は同名衝突回避後の最終名）。
            if _created_file_rows:
                env["created_files"] = [
                    {"name": r["rel_path"], "download_url": f"/workspace/files/{r['id']}/download"}
                    for r in _created_file_rows
                ]
            if answer:
                # 直読した資料は MCP の結果に載らず env["sources"] に反映されない。回答末尾の「参照した資料:」ブロックを解析し、read 系 MCP ツール引数から拾った doc_id（記載漏れの補完・出現順で後ろに合流）と合わせて機械検証（実在・文書種別・scope・秘匿名除外）を通ったものだけを sources の先頭へ足す（`_gather` 由来の既存 sources は後ろに残す・doc_id 重複は除外）。verified が0件なら参照ブロックの記載を消さず本文をそのまま残す。
                _body, _listed_lines = parse_referenced_doc_lines(answer)
                _ref_candidates: list = list(_listed_lines)
                _ref_seen: set[str] = set()
                for _r in _mcp_read_docs:
                    if _r and _r not in _ref_seen:
                        _ref_seen.add(_r)
                        _ref_candidates.append(_r)
                # `xlsx_sheets`（シート一覧のみ）の doc_id も参照候補（sources）には合流させる。ただし「参照した資料:」にも書かれておらず本文精読ツール（`_mcp_read_docs`）でも読まれていない doc_id は、根拠ゲート（sources_verified）に数えない。参照ブロック＋本文精読ツールだけで確定する「精読済み」集合を先に確定させ、`xlsx_sheets` を足した後の verified との差分（`_listed_only_ids`）として区別する。
                _verified_before_listed = set(verified_referenced_docs(_ref_candidates, ctx.world, sp))
                for _r in _mcp_listed_docs:
                    if _r and _r not in _ref_seen:
                        _ref_seen.add(_r)
                        _ref_candidates.append(_r)
                _verified_refs = verified_referenced_docs(_ref_candidates, ctx.world, sp)
                _listed_only_ids = set(_verified_refs) - _verified_before_listed
                if _verified_refs and ctx.make_sources:
                    _ref_sources, _ = _verified_sources(ctx.make_sources, set(_verified_refs), ctx.world, sp)
                    # 参照ブロックの記載順（→ MCP 引数の順）に並べ直し、既存（`_gather` 由来）と重複する資料は先頭側（参照した資料）を残す。
                    _order = {d: i for i, d in enumerate(_verified_refs)}
                    _ref_sources = sorted(_ref_sources, key=lambda s: _order.get(s.get("doc_id"), len(_order)))
                    _ref_ids = {s.get("doc_id") for s in _ref_sources}
                    env["sources"] = _ref_sources + [s for s in (env.get("sources") or [])
                                                     if s.get("doc_id") not in _ref_ids]
                    # 実際に開いて根拠にした資料＝API 経路の「精読済み」と同じ意味＝出典の 2 区分（根拠／参考）に載せる。`xlsx_sheets` だけで到達した doc_id（`_listed_only_ids`）は除く。
                    env["sources_verified"] = sorted(_ref_ids - _listed_only_ids)
                env["codex_referenced_docs"] = {"listed": len(_ref_candidates), "verified": len(_verified_refs)}
                env["headline"] = _body if (_verified_refs and _body.strip()) else answer  # 空本文には差し替えない
                if _schema_on:
                    # v2 のときだけ主張配列を持つ（v1 は常に空リスト）。区分と理由コードを envelope にも載せる（共有・監査で消えないよう `sherpa/store/shares.py::_safe_claim` が既知フィールドのみで再構築する）。
                    _claims = _pick_structured_claims()
                    if _claims:
                        # API 経路と同じ最終ゲートを Codex にも掛ける。確定主張のうち必須の根拠種別を欠くものを推定へ格下げし、ターン単位の不足を headline 冒頭に前置する（本文は書き換えない・作成系は成果物の中身に混ざるため注記を出さない）。
                        _claims, _gate_meta, _gate_missing, _gate_unavailable = _apply_codex_evidence_gate(
                            _claims, lens=decision["lens"], world=ctx.world, scope_paths=sp,
                            layer=layer_mod.effective_layer(ctx.scope_meta, decision["lens"]),
                            personal_facts=ctx.personal_facts)
                        # 台帳突合（claims は台帳からの投影）は根拠種別ゲートの後に適用する（両方の格下げが独立に効く）。
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
                            # 種別ゲート・台帳突合のどちらかで confirmed が1件でも格下げされたら同じ注記を出す（二重には付けない・`_gate_missing` があるターンは既に注記が格下げを示唆しているため重ねない）。
                            _any_claims_demoted = (
                                _gate_meta["demoted"] > 0
                                or _claims_ledger_check.get("downgraded", 0) > 0)
                            if not _gate_missing and _any_claims_demoted:
                                _gate_note = _DEMOTED_CLAIMS_NOTE + _gate_note
                            if _gate_note:
                                env["headline"] = f"{_gate_note}\n\n{env['headline']}"
                # 実際に回答を生成できたターンは、`_dispatch` がツール遮断時に立てた `agentic_failure`（`agentic_search.tools_blocked_env`）を消す（Codex は遮断状態を見ずに調査を続行し得るため）。
                env.pop("agentic_failure", None)
                # 台帳の確認できなかった項目を、回答の末尾へ機械的に付ける（AI は使わない・無ければ付けない）。`_investigation_snapshot` は降格適用後の状態で `env["investigation"]["counts"]` と一致する。同じリストを `env["investigation"]["unconfirmed_items"]` にも構造化形で載せる（Codex ジョブ API の `unconfirmed_items` の正本・チャット画面には出さない内部キー）。
                if _investigation_snapshot is not None:
                    _unconfirmed_items = _unconfirmed_items_list(_investigation_snapshot)
                    env.setdefault("investigation", {})["unconfirmed_items"] = _unconfirmed_items
                    _unconfirmed_section = _format_unconfirmed_items_section(_unconfirmed_items)
                    if _unconfirmed_section:
                        env["headline"] = f"{env['headline']}\n\n{_unconfirmed_section}"
                # 「追加で調べますか？」: 最後の中間の見直しが mostly_answered かつ extra_perspectives を挙げていたら定型文を付ける（AI の自由記述はそのまま流さない・`_review_continuation_note_text` は観点を短く切って並べた決定的な文字列）。台帳は既に退避済みで、「続き」で復元されれば `_investigation_dir`／`reviews.jsonl` がそのまま戻る。
                if _review_continuation_note_text:
                    env["headline"] = f"{env['headline']}\n\n{_review_continuation_note_text}"
                # 自動継続を尽くしてもなお進行中の宣言文が headline に残ったターンは、本文を書き換えず `_codex_stopped_early` を根拠に envelope へ印を付ける。chat_service._finalize が予算到達時の途中結果・出典0件時の案内と同形式（headline 直下の独立注記＋案内ボタン）で UI に出す（`stop_reason` の閉じた語彙とは別のマーカー）。
                if _codex_stopped_early:
                    env["codex_stopped_early"] = True
                yield _node("codex", "think", "Codex が調べる",
                            "調べて回答をまとめました" if ran else "回答をまとめました", "done")
            elif _codex_silent_failure:
                # 利用統計の終了理由分布（`stop_kind_mod.resolve`）がこの分岐を `codex_silent` と判定できるよう印を立てる。
                env["codex_silent_failure"] = True
                # `_gather` が組み立てた決定的回答をそのまま返さず、利用者に「AI が答えていない」ことが伝わるよう `_UnwiredProvider` と同じ文体の正直な文言に上書きする。summary/sources は `_gather` の実結果のまま残すが、sources が空なら data も `{}` へ揃える（`chat_service._no_genuine_results` の honest failure 規約と一致させる）。
                # 無出力失敗はプロキシ/CA 証明書の不備・sandbox の起動失敗・CLI のクラッシュでも起きるため、認証だけに断定せず、観測事実（応答を返す前に終了）を述べて考えられる原因を複数挙げる。returncode は利用者向け本文には出さずログにだけ残す。stderr は破棄している。
                # `turn.failed`／`error` で閉じた attempt（agent_message 無し）は「回答を返せずに終了しました」とし、既存の無出力失敗と文言を分ける（スキーマ違反なども原因になり得るため）。
                _reason = ("回答を返せずに終了しました" if _turn_failed
                          else "応答を返す前に終了しました")
                if _turn_failed_code:
                    # 閉集合ではない生の診断コード（統計・分類には使わない・管理者向け補助情報）。
                    env["codex_error_code"] = _turn_failed_code
                if _turn_failed_code == _CONTEXT_WINDOW_EXCEEDED_CODE:
                    # 1回の調査で集めたツール結果だけで文脈枠を使い切った場合は、認証/ネットワーク不調と混同させず、範囲を絞る具体的な次の一手を示す。
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
                # 本文（answer）は空だが、完全な沈黙（`_codex_silent_failure`）ではないケース（command_execution 等は実行できたが結論の agent_message が無いまま打ち切られた）。silent failure 分岐は headline で既に告知しているため対象外のまま、env["headline"] が `_gather` の headline のままの場合にも注記を出す（presearch を省いたターンは `_NO_PRESEARCH_HEADLINE` のまま）。`_stream_error` は Popen 完走前の例外でも立つため、`attempt_returncode is None` のまま `_codex_silent_failure` が計算されずここに落ちても終了理由の分布から漏らさない（`stop_kind.resolve` の codex_silent 判定に必要な印）。
                if _stream_error:
                    env["codex_silent_failure"] = True
                if _codex_stopped_early:
                    env["codex_stopped_early"] = True
                # presearch を省いたターンは決定的回答へ「切替」ようが無い（下調べを実行していない）ため、決定的回答を使ったかのような文言にしない。
                _no_answer_detail = ("（未応答のため回答を出せませんでした）" if _skip_presearch
                                     else "（未応答のため決定的回答に切替）")
                yield _node("codex", "think", "Codex が調べる", _no_answer_detail, "done")
            if _created_files_failed:
                # headline がどの分岐で組み立てられていても、保存できなかった成果物がある事実は一律に伝える。
                env["headline"] = f"{env['headline']}\n\n{_CREATED_FILES_FAILURE_NOTE}"
            if _wall_clock_state["hit"]:
                # headline がどの分岐で組み立てられていても、時間の上限で打ち切った事実は一律に伝える。「打ち切りの内訳」（利用統計）へも記録する。
                env["headline"] = f"{env['headline']}\n\n{_WALL_CLOCK_LIMIT_NOTE}"
                env["limits"] = {**(env.get("limits") or {}), "wall_clock_hit": True}
            yield {"type": "answer_delta", "text": env["headline"]}  # Codex は一括→フロントで段階表示
            yield {"type": "_result", "env": env, "decision": decision,
                  "investigation_record": _investigation_record_payload}
        finally:
            # 台帳の退避・削除が終わるまで会話ロックを保持する（先に解放すると、同じ会話の「続き」ターンが未作成またはコピー途中の退避先を復元処理で参照しうる）。内側 `try/finally` で、退避／run_dir 削除の途中で例外が起きても最後に必ずロックを解放する。
            try:
                if run_dir is not None:
                    # 調査台帳の退避: run_dir 削除より前に、完了せずターンが終わった台帳だけ永続領域へコピーする。`_investigation_verdict`（本体ループが正常に完走したときだけ埋まる）には頼らない（yield で generator が close された場合に未完了台帳が失われるため）。通常終了時は env 構築の直前で退避済み（`_investigation_retire_done`）で、ここは切断・例外で先に到達しなかった場合だけのフォールバック（二重には実行しない）。
                    if (_investigation_dir is not None and _ledger_home is not None
                            and not _investigation_retire_done):
                        _retire_investigation_ledger(
                            _investigation_dir, _ledger_home, required_extra=_ledger_required_extra,
                            require_review=_ledger_require_review)
                    _release_active_run_dir(run_dir)
                    if _created_files_failed:
                        # 保存に失敗した成果物がある run_dir は削除せず回収用に残す（`_cleanup_stale_run_dirs` の24時間しきい値で最終的に掃除される）。
                        _log.warning(
                            "codex run dir kept for recovery due to created-file save failure: %s",
                            run_dir.name)
                    else:
                        _remove_dir_best_effort(run_dir)
            finally:
                if _conv_lock_acquired:
                    _conv_lock.release()
