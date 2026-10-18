#!/usr/bin/env python3
"""台帳評価の採点規則: 試行の原因別分類・サーバ側の道具回数・再試行の選び方・集計表。

ランナー（ledger_eval.py）と再分類（ledger_reclassify.py）が同じ規則を使う。ここは保存済みの記録を読むだけで、
モデルや API は呼ばない。
"""
from __future__ import annotations

import ast
import collections
import json
from pathlib import Path
import re
import statistics
import time

LIMIT_RE = re.compile(r"usage[_ ]limit|rate[_ ]limit|quota[_ ]exceeded|too many requests|"
                      r"insufficient_quota|利用枠.{0,24}(?:超|上限|達|制限|使い切)|HTTP[/ ](?:\S+ )?429|"
                      r'"status(?:_code)?"\s*:\s*429', re.I)
CAPACITY_RE = re.compile(r"at capacity|overloaded", re.I)

COMPLETE = "complete"
PRODUCT_PARTIAL = "product_partial"
INFRASTRUCTURE = "infrastructure"
STOPPED = "stopped"
UNVERIFIED = "unverified"
CLASSES = (COMPLETE, PRODUCT_PARTIAL, INFRASTRUCTURE, STOPPED, UNVERIFIED)
LABELS = {COMPLETE: "完答(a)", PRODUCT_PARTIAL: "製品部分(b)", INFRASTRUCTURE: "基盤障害(c)",
          STOPPED: "停止(d)", UNVERIFIED: "原因未確定"}
# 通常の Recall/Precision に入れる分類。基盤障害・停止・原因未確定は入れない。
COUNTED = frozenset({COMPLETE, PRODUCT_PARTIAL})
DEFAULT_RETRY = (INFRASTRUCTURE, STOPPED)
PARTIAL_REASONS = ("review_reverted", "stopped_early", "invalid_output")

# 版の指紋。metadata.json の条件のうち、違えば別の計測として扱う項目。
FINGERPRINT_KEYS = ("commit", "cli", "cli_sha256", "model", "depth", "mode", "schema_level", "manifest_sha256",
                    "corpus_sha256", "review_caps", "es", "seed", "project", "database", "ports", "auth_mode", "diff_sha256",
                    "fingerprint")
SYSTEMS = ("code", "multihop", "jemhop")
STYLES = ("json", "plan_gate", "plan_main")


class EvalError(RuntimeError):
    pass


class UsageLimit(EvalError):
    pass


class InfrastructureStop(EvalError):
    pass


class StallTimeout(EvalError):
    """ターンの進行（SSE のイベント）が --stall-timeout 秒途絶えた。"""


def _json(line: str):
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _info_text(info) -> str | None:
    if isinstance(info, dict) and info:
        return str(next(iter(info)))
    return info if isinstance(info, str) else None


def error_kind(message: str | None, info) -> str:
    """CLI 終端のエラーを capacity / usage_limit / service_error に分ける。"""
    code = _info_text(info)
    if code == "server_overloaded" or CAPACITY_RE.search(message or ""):
        return "capacity"
    if code in ("usage_limit_exceeded", "usage_limit") or LIMIT_RE.search(message or ""):
        return "usage_limit"
    return "service_error"


def session_dir_for(root: Path, conversation_id) -> Path | None:
    if conversation_id is None:
        return None
    found = sorted(Path(root).glob(f"runtime/users/*/workspace/.codex-sessions/{conversation_id}"))
    return found[0] if found else None


def read_cli_terminal(session_dir: Path | None) -> dict:
    """親 rollout 末尾の task_complete.error と、最後に観測した利用枠の状態を読む（本文は読み出さない）。"""
    out = {"rollouts": 0, "parent_found": False, "task_complete_count": 0, "terminal_error": None,
           "errors": [], "child_errors": 0, "rate_limits": None}
    files = sorted(Path(session_dir).glob("sessions/**/rollout-*.jsonl")) if session_dir else []
    out["rollouts"] = len(files)
    parent_tasks, any_rate, parent_rate = [], None, None
    for path in files:
        parent, tasks, rate = True, [], None
        with path.open(encoding="utf-8", errors="replace") as fh:
            for index, line in enumerate(fh):
                if index == 0:
                    meta = _json(line) or {}
                    payload = meta.get("payload") if meta.get("type") == "session_meta" else None
                    if isinstance(payload, dict) and (payload.get("parent_thread_id")
                                                      or payload.get("thread_source") == "subagent"):
                        parent = False
                    continue
                if '"task_complete"' in line:
                    event = _json(line)
                    payload = event.get("payload") if event and event.get("type") == "event_msg" else None
                    if isinstance(payload, dict) and payload.get("type") == "task_complete":
                        tasks.append(payload)
                elif '"rate_limits"' in line:
                    event = _json(line)
                    payload = event.get("payload") if event and event.get("type") == "event_msg" else None
                    if isinstance(payload, dict) and payload.get("type") == "token_count" \
                            and isinstance(payload.get("rate_limits"), dict):
                        rate = payload["rate_limits"]
        if rate is not None:
            any_rate = rate
        if parent:
            out["parent_found"] = True
            parent_tasks.extend(tasks)
            parent_rate = rate if rate is not None else parent_rate
        else:
            out["child_errors"] += sum(1 for t in tasks if t.get("error"))
    out["task_complete_count"] = len(parent_tasks)
    for task in parent_tasks:
        error = task.get("error")
        if error:
            info = error.get("codex_error_info") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else str(error)
            out["errors"].append({"message": (message or "")[:300], "code": _info_text(info)})
    if parent_tasks and parent_tasks[-1].get("error"):
        out["terminal_error"] = out["errors"][-1]
    out["rate_limits"] = parent_rate if parent_rate is not None else any_rate
    return out


_END_RE = re.compile(r"sherpa\.codex: end conv=(\d+) .*?returncode=(-?\d+) .*?events=(\{[^}]*\})")


def read_codex_log(path: Path | None, conversation_id) -> dict | None:
    """ランナーのログ末尾の CLI 終了行（終了コードとイベント件数）。無ければ None。"""
    if path is None or conversation_id is None or not Path(path).is_file():
        return None
    found = None
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            match = _END_RE.search(line)
            if match and match.group(1) == str(conversation_id):
                try:
                    events = ast.literal_eval(match.group(3))
                except (ValueError, SyntaxError):
                    events = {}
                found = {"returncode": int(match.group(2)), "events": events if isinstance(events, dict) else {}}
    return found


def classify_trial(result: dict, answer: dict | None, cli: dict | None = None, cli_exit: dict | None = None) -> dict:
    """1 試行を (a)完答 (b)製品の部分回答 (c)基盤障害 (d)停止 のいずれかに分ける。

    APIの codex_error_code が空であることや result.status=ok だけでは判定しない。
    CLI 終端の診断・終了コード・通知（notices）・保存回答の completion を順に見る。
    """
    status = result.get("status")
    warnings: list[str] = []

    def done(cls: str, reason: str, **evidence) -> dict:
        return {"class": cls, "reason": reason, "retriable": cls != COMPLETE and cls != PRODUCT_PARTIAL,
                "evidence": {"status": status, **evidence}, "warnings": warnings}

    if status == "interrupted":
        return done(STOPPED, "interrupted", stop_drained=result.get("stop_drained"))
    if status in (None, "running"):
        return done(STOPPED, "unfinished")
    if status == "usage_limit":
        return done(INFRASTRUCTURE, "usage_limit", limit_evidence=result.get("limit_evidence"))
    if status == "failed" and result.get("stalled"):
        return done(INFRASTRUCTURE, "stall_timeout", stall_seconds=result.get("stall_seconds"))
    if status == "failed":
        return done(INFRASTRUCTURE, f"runner_error:{result.get('error_type')}", error_type=result.get("error_type"))
    if not isinstance(answer, dict):
        return done(UNVERIFIED, "answer_missing")
    completion, stop_kind = answer.get("completion"), answer.get("stop_kind")
    kinds = sorted({n.get("kind") for n in answer.get("notices") or [] if isinstance(n, dict) and n.get("kind")})
    base = {"completion": completion, "stop_kind": stop_kind, "notice_kinds": kinds}
    terminal = (cli or {}).get("terminal_error")
    if completion == "stopped":
        return done(STOPPED, "answer_stopped", **base)
    if completion != "complete":
        if terminal:
            kind = error_kind(terminal.get("message"), terminal.get("code"))
            return done(INFRASTRUCTURE, f"cli_{kind}", cli_error=terminal, **base)
        if completion == "failed" or answer.get("agentic_failure") or answer.get("codex_error_code"):
            return done(INFRASTRUCTURE, "service_failure", agentic_failure=bool(answer.get("agentic_failure")),
                        codex_error_code=answer.get("codex_error_code"), **base)
        events = (cli_exit or {}).get("events") or {}
        if cli_exit and cli_exit.get("returncode") not in (0, None) and (events.get("turn.failed") or events.get("error")):
            return done(INFRASTRUCTURE, "cli_exit_nonzero", cli_exit=cli_exit, **base)
        if "answer_recovery" in kinds:
            clean = bool(cli and cli.get("parent_found"))
            return done(UNVERIFIED, "answer_recovery_cli_clean" if clean else "answer_recovery_without_cli_evidence", **base)
    elif terminal:
        warnings.append("cli_error_with_complete_answer")
    if completion == "complete":
        return done(COMPLETE, "complete", **base)
    if completion == "partial":
        for reason in PARTIAL_REASONS:
            if reason in kinds:
                return done(PRODUCT_PARTIAL, reason, **base)
        return done(PRODUCT_PARTIAL, "partial_other", **base)
    return done(UNVERIFIED, f"completion_{completion}", **base)


def end_state(classification: dict, answer: dict | None) -> dict:
    """試行の終わり方。classification を主にし、回答の completion・stop_kind・notices の kind を並べる。
    回答が無い項目は None（欠測）で、完了や 0 にしない。完了と言えるのは class が complete のときだけ。"""
    have = isinstance(answer, dict)
    kinds = sorted({n.get("kind") for n in answer.get("notices") or [] if isinstance(n, dict) and n.get("kind")}) \
        if have and isinstance(answer.get("notices"), list) else None
    return {"class": classification.get("class"), "reason": classification.get("reason"),
            "completed": classification.get("class") == COMPLETE,
            "counted": classification.get("class") in COUNTED,
            "completion": answer.get("completion") if have else None,
            "stop_kind": answer.get("stop_kind") if have else None,
            "notice_kinds": kinds}


def _integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def observed_tools(call_stats) -> dict:
    """answer.call_stats.tools（サーバ側の記録）を道具名で合算する。外側の exec とは加算しない。

    calls 合計と記録された calls を照合し、missing が 0 のときだけ完全な観測とする。
    call_stats が無い・壊れているときは回数を None（ES 未使用の 0 とは区別）にする。
    """
    diagnostics = {"call_stats_present": isinstance(call_stats, dict), "invalid_rows": 0,
                   "duplicate_pairs": 0, "duplicate_conflicts": 0}
    if not isinstance(call_stats, dict):
        return {"by_role_tool": None, "by_tool": None, "es_search_calls": None,
                "observation_complete": False, "diagnostics": diagnostics}
    rows = call_stats.get("tools")
    if not isinstance(rows, list):
        rows, diagnostics["tools_missing"] = [], True
    pairs: dict[tuple[str, str], int] = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("tool"), str) or not row["tool"]
                or row.get("role") not in ("parent", "child", "undetermined") or not _integer(row.get("calls"))):
            diagnostics["invalid_rows"] += 1
            continue
        pair = (row["role"], row["tool"].rsplit("__", 1)[-1])
        if pair in pairs:
            diagnostics["duplicate_pairs"] += 1
            if pairs[pair] != row["calls"]:
                diagnostics["duplicate_conflicts"] += 1
            continue
        pairs[pair] = row["calls"]
    by_tool: collections.Counter = collections.Counter()
    for (_role, tool), calls in pairs.items():
        by_tool[tool] += calls
    total = sum(pairs.values())
    recorded, missing = call_stats.get("calls"), call_stats.get("missing")
    matches = total == recorded if _integer(recorded) else None
    usable = not (diagnostics["invalid_rows"] or diagnostics["duplicate_conflicts"] or diagnostics.get("tools_missing"))
    diagnostics.update(recorded_calls=recorded if _integer(recorded) else None, summed_calls=total,
                       calls_match=matches, missing=missing if _integer(missing) else None)
    return {"by_role_tool": [{"role": role, "tool": tool, "calls": calls} for (role, tool), calls in sorted(pairs.items())],
            "by_tool": dict(sorted(by_tool.items())) if usable else None,
            "es_search_calls": by_tool.get("es_search", 0) if usable else None,
            "observation_complete": usable and matches is True and missing == 0,
            "diagnostics": diagnostics}


def fingerprint_mismatch(recorded: dict, current: dict) -> list[str]:
    return [key for key in FINGERPRINT_KEYS if recorded.get(key) != current.get(key)]


def check_same_version(recorded: dict, current: dict) -> None:
    diff = fingerprint_mismatch(recorded, current)
    if diff:
        raise EvalError("版の指紋が既存結果と異なります（" + ", ".join(diff) + "）。既存結果へ混ぜず、"
                        "新しい出力フォルダを --out に指定して計測してください")


def attempt_dirs(base: Path) -> list[tuple[int, Path]]:
    """試行番号と保存先。1 回目は質問のフォルダ直下、再試行は retries/<番号>/（2 以降）。"""
    found = []
    if (base / "result.json").is_file():
        found.append((1, base))
    for path in (base / "retries").glob("*") if (base / "retries").is_dir() else ():
        if path.name.isdecimal() and int(path.name) >= 2 and (path / "result.json").is_file():
            found.append((int(path.name), path))
    return sorted(found)


def load_attempts(base: Path, root: Path, *, fresh: bool = False, codex_log: Path | None = None) -> list[dict]:
    """質問 1 件の全試行を番号順に読む。保存済みの分類があれば使い、無ければ記録から分類する。"""
    log = codex_log if codex_log is not None else Path(root) / "runtime/run/codex.log"
    attempts = []
    for number, path in attempt_dirs(base):
        result = json.loads((path / "result.json").read_text(encoding="utf-8"))
        saved = result.get("classification")
        if not fresh and isinstance(saved, dict) and saved.get("class") in CLASSES:
            classification = saved
        else:
            answer_path = path / "answer.json"
            answer = json.loads(answer_path.read_text(encoding="utf-8")) if answer_path.is_file() else None
            conversation = result.get("conversation_id")
            cli = read_cli_terminal(session_dir_for(root, conversation))
            classification = classify_trial(result, answer, cli, read_codex_log(log, conversation))
        attempts.append({"n": number, "dir": path, "result": result, "classification": classification})
    return attempts


def effective_attempt(attempts: list[dict]) -> dict:
    """最新の有効（完答または製品の部分回答）な試行。無ければ最新の試行。"""
    valid = [a for a in attempts if a["classification"]["class"] in COUNTED]
    return valid[-1] if valid else attempts[-1]


def rate_metrics(result: dict) -> dict:
    """Recall を入力 100 万 token あたり・1 分あたりにした効率。分母が無い・0 なら欠測。"""
    recall, tokens, wall = result.get("recall"), result.get("input_tokens"), result.get("wall_seconds")
    return {"recall_per_mtoken": recall / (tokens / 1e6)
            if isinstance(recall, (int, float)) and isinstance(tokens, int) and tokens > 0 else None,
            "recall_per_min": recall / (wall / 60)
            if isinstance(recall, (int, float)) and isinstance(wall, (int, float)) and wall > 0 else None}


def collect(root: Path, phase_dir: str = "trials", *, fresh: bool = False, codex_log: Path | None = None,
            server_tools: bool = False, enrich=None) -> list[dict]:
    """質問ごとの有効試行を 1 行にする。障害・停止の試行の時間は cost_seconds に全試行ぶん残す。

    server_tools=True は、保存済み回答の call_stats から道具回数を取り直して行へ上書きする
    （CLI 外側の観測から es_search_calls を出していた古い記録の再集計用）。
    enrich は (試行のフォルダ, result) を受けて追加指標の辞書を返す関数。保存済みの記録から取り直して行へ上書きする。
    """
    rows = []
    for base in sorted((Path(root) / phase_dir).glob("*/*/*")):
        if not base.is_dir():
            continue
        attempts = load_attempts(base, root, fresh=fresh, codex_log=codex_log)
        if not attempts:
            continue
        chosen = effective_attempt(attempts)
        cost: dict[str, float] = collections.defaultdict(float)
        for a in attempts:
            wall = a["result"].get("wall_seconds")
            if isinstance(wall, (int, float)):
                cost[a["classification"]["class"]] += wall
        cls = chosen["classification"]["class"]
        tools = {}
        answer_path = chosen["dir"] / "answer.json"
        if server_tools and answer_path.is_file():
            seen = observed_tools(json.loads(answer_path.read_text(encoding="utf-8")).get("call_stats"))
            tools = {"es_search_calls": seen["es_search_calls"], "server_tool_counts": seen["by_tool"],
                     "server_tool_roles": seen["by_role_tool"], "tool_observation_complete": seen["observation_complete"],
                     "tool_calls_recorded": seen["diagnostics"].get("recorded_calls"),
                     "tool_calls_missing": seen["diagnostics"].get("missing")}
        extra = enrich(chosen["dir"], chosen["result"]) if enrich else {}
        saved = json.loads(answer_path.read_text(encoding="utf-8")) if answer_path.is_file() else None
        extra = {**extra, "end_state": end_state(chosen["classification"], saved)}
        stalls = sum(1 for a in attempts if a["result"].get("stalled") or a["classification"].get("reason") == "stall_timeout")
        row = {**chosen["result"], **extra, **tools}
        rows.append({**row, **rate_metrics(row), "stall_count": stalls,
                     "class": cls, "classification": chosen["classification"],
                     "counted": cls in COUNTED, "attempt_no": chosen["n"], "attempts_total": len(attempts),
                     "attempt_classes": [a["classification"]["class"] for a in attempts],
                     "cost_seconds": dict(cost)})
    return rows


def select_retries(rows: list[dict], only) -> list[tuple[str, str, str]]:
    """再試行の対象（構成・系統・質問ID）。有効な試行がまだ無いものだけで、指定した分類に限る。"""
    unknown = set(only) - set(CLASSES)
    if unknown or COMPLETE in only:
        raise EvalError("--only には " + ", ".join(c for c in CLASSES if c != COMPLETE) + " を指定できます")
    return [(r["style"], r["system"], str(r["id"])) for r in rows if r["class"] in only]


def reached(rate_limits, now: float | None = None) -> bool:
    """利用枠の到達フラグ。使用率の高さだけでは到達としない。リセット時刻を過ぎた古い記録は到達としない。"""
    if not isinstance(rate_limits, dict):
        return False
    if not (rate_limits.get("rate_limit_reached_type") or rate_limits.get("spend_control_reached")):
        return False
    reset = (rate_limits.get("primary") or {}).get("resets_at")
    return not (isinstance(reset, (int, float)) and (time.time() if now is None else now) >= reset)


def latest_rate_limits(root: Path) -> dict | None:
    files = sorted(Path(root).glob("runtime/users/*/workspace/.codex-sessions/*/sessions/**/rollout-*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:6]:
        found = None
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"rate_limits"' in line:
                    event = _json(line)
                    payload = event.get("payload") if event else None
                    if isinstance(payload, dict) and isinstance(payload.get("rate_limits"), dict):
                        found = payload["rate_limits"]
        if found is not None:
            return found
    return None


def usage_gate(root: Path, *, clear: bool = False, now: float | None = None) -> None:
    """新規試行の入口。到達で止めた記録、または最後に観測した到達フラグがあれば UsageLimit。"""
    stopped = Path(root) / "stopped.json"
    if stopped.exists():
        if not clear:
            raise UsageLimit("利用枠で停止した記録があります。枠のリセットを確認後 --clear-usage-stop で解除してください")
        n = 1
        while (Path(root) / f"stopped-{n}.json").exists():
            n += 1
        stopped.rename(Path(root) / f"stopped-{n}.json")
    if reached(latest_rate_limits(root), now):
        raise UsageLimit("最後に観測した利用枠が到達フラグ付きです。リセット時刻の後に再試行してください")


def _mean(rows: list[dict], key: str) -> str:
    values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    return f"{statistics.mean(values):.3f} (n={len(values)})" if values else "—"


def _groups(rows: list[dict], style: str, systems=SYSTEMS):
    for group in ["all", *systems, "hop1", "hop2", "hop3", "hop4+"]:
        yield group, [r for r in rows if r["style"] == style and
                      (group == "all" or r["system"] == group or
                       (group.startswith("hop") and (r["hop"] >= 4 if group == "hop4+" else r["hop"] == int(group[3:]))))]


def review_caps_line(rows: list[dict]) -> str:
    """見直しの上限は固定の文言にせず、試行の review_semantics（製品が記録した値）から構成ごとに出す。"""
    parts = []
    for style in STYLES:
        sem = [r["review_semantics"] for r in rows if r.get("style") == style and isinstance(r.get("review_semantics"), dict)]
        fields = [f"{key}={_join_values(sem, key)}" for key in ("app_review_cap", "instruction_review_cap", "app_gating")
                  if _join_values(sem, key) != "—"]
        if fields:
            parts.append(f"{style}: " + " ".join(fields))
    return ("見直し上限（試行の記録 review_semantics）: " + " / ".join(parts) + "。") if parts \
        else "見直し上限: 試行に記録が無い（古い記録）。"


def summary_markdown(rows: list[dict], systems=SYSTEMS, model: str | None = None) -> str:
    text = ["# 台帳 3 構成の集計", "", f"条件: Codex CLI 0.162.1 / {model or '—'} / ES BM25 のみ / 深さ standard。",
            review_caps_line(rows) + "未定義・欠測は —。", "",
            "各質問は最新の有効な試行（完答または製品の部分回答）を使う。件数は質問単位の有効試行の分類。",
            "Recall・Precision・未確認率・秒などの平均は完答(a)と製品部分(b)だけ。基盤障害(c)・停止(d)・原因未確定は入れず、"
            "時間は障害秒（再試行を含む全試行ぶん）として別に残す。", "",
            "ES回数はサーバ側の記録（answer.call_stats）から数え、観測が完全な試行だけを平均に入れる。"
            "正解 0 件の Recall・参照 0 件の Precision は欠測で、0 や 1 にしない。", "",
            "|構成|系統・hop|完答(a)|製品部分(b)|基盤障害(c)|停止(d)|原因未確定|Recall|Precision|Recall(aのみ)|Precision(aのみ)|"
            "未確認率|見直し計数|秒|入力token|ES回数|障害秒(c)|停止秒(d)|",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for style in STYLES:
        for group, selected in _groups(rows, style, systems):
            counts = collections.Counter(r["class"] for r in selected)
            counted = [r for r in selected if r["counted"]]
            complete = [r for r in selected if r["class"] == COMPLETE]
            es = [r for r in counted if r.get("tool_observation_complete")]

            def cost(cls: str) -> float:
                return sum(r.get("cost_seconds", {}).get(cls, 0.0) for r in selected)

            text.append(f"|{style}|{group}|" + "|".join(str(counts.get(c, 0)) for c in CLASSES[:2] + (INFRASTRUCTURE, STOPPED, UNVERIFIED))
                        + "|" + "|".join([_mean(counted, "recall"), _mean(counted, "precision"),
                                          _mean(complete, "recall"), _mean(complete, "precision"),
                                          _mean(counted, "unconfirmed_rate"), _mean(counted, "review_count"),
                                          _mean(counted, "wall_seconds"), _mean(counted, "input_tokens"),
                                          _mean(es, "es_search_calls"), f"{cost(INFRASTRUCTURE):.1f}",
                                          f"{cost(STOPPED):.1f}"]) + "|")
    return ("\n".join(text) + "\n" + metrics_markdown(rows, systems) + stages_markdown(rows, systems)
            + target_gate_markdown(rows))


def behavior_markdown(rows: list[dict], systems=SYSTEMS) -> str:
    text = ["# 台帳の挙動", "", "完答(a)と製品部分(b)だけ。欠測は —、0 件とは区別する。"
            "A の最終点検要求と台帳記録件数、B の要求と追加ありの回数は別欄。", "",
            "|構成|系統|見直し要求|見直し記録件数|追加あり回数(B)|spawn|自動継続|resume|該当なし回答|該当なし根拠ゼロ|",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for style in STYLES:
        for system in systems:
            selected = [r for r in rows if r["style"] == style and r["system"] == system and r["counted"]]
            text.append(f"|{style}|{system}|" + "|".join(
                _mean(selected, key) for key in ("review_requests", "review_entries", "review_growth_rounds",
                                                 "spawn_count", "auto_continue_count", "resume_count",
                                                 "null_answer", "null_no_evidence")) + "|")
    return "\n".join(text) + "\n"


PUBLIC_SYSTEMS = ("multihop", "jemhop")
OWN_SYSTEMS = ("self", "code")


def _scopes(rows: list[dict], style: str, systems):
    """構成ごとの集計単位: 全体・コーパス別（公開＝multihop/jemhop、独自＝self/code）・系統別。"""
    mine = [r for r in rows if r["style"] == style]
    yield "all", mine
    yield "公開", [r for r in mine if r["system"] in PUBLIC_SYSTEMS]
    yield "独自", [r for r in mine if r["system"] in OWN_SYSTEMS]
    for system in systems:
        yield system, [r for r in mine if r["system"] == system]


def _values(rows: list[dict], key: str) -> list[float]:
    return [r[key] for r in rows if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]


def _mean_plain(rows: list[dict], key: str) -> str:
    values = _values(rows, key)
    return f"{statistics.mean(values):.3f}" if values else "—"


def percentile(values: list[float], q: float) -> float | None:
    """線形補間の百分位（q は 0〜1）。空なら None。"""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _fmt(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _nested(row: dict, *keys):
    value = row
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _nested_mean(rows: list[dict], *keys) -> str:
    values = [v for v in (_nested(r, *keys) for r in rows) if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return f"{statistics.mean(values):.3f}" if values else "—"


def _rate(rows: list[dict], numerator: str) -> str:
    """graph の記録がある試行を合算した率（分子 ÷ impact_calls）。呼び出しが 0 回なら欠測。"""
    graphs = [r["graph"] for r in rows if isinstance(r.get("graph"), dict)]
    calls = sum(g.get("impact_calls") for g in graphs if isinstance(g.get("impact_calls"), int))
    top = sum(g.get(numerator) for g in graphs if isinstance(g.get(numerator), int))
    return f"{top / calls:.3f} ({top}/{calls})" if calls else "—"


def metrics_markdown(rows: list[dict], systems=SYSTEMS) -> str:
    """追加指標の表。平均・百分位は完答(a)と製品部分(b)だけ。記録の無い試行は欠測として母数に入れない。"""
    text = ["", "## 追加指標", "",
            "範囲は 全体・コーパス別（公開＝multihop・jemhop、独自＝self・code）・系統別。完答(a)と製品部分(b)だけを数え、"
            "古い記録で項目が無い試行は母数に入れない（—）。", "",
            "### 精度・時間・効率", "",
            "|構成|範囲|試行|Recall|Precision|秒p50|秒p95|入力token平均|未確認率|Recall/百万token|Recall/分|総試行(再試行込)|無応答|",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    scoped = [(style, scope, selected, [r for r in selected if r["counted"]])
              for style in STYLES for scope, selected in _scopes(rows, style, systems)]
    for style, scope, selected, counted in scoped:
        if not selected:
            continue
        walls = _values(counted, "wall_seconds")
        text.append(f"|{style}|{scope}|{len(counted)}|{_mean_plain(counted, 'recall')}|{_mean_plain(counted, 'precision')}|"
                    f"{_fmt(percentile(walls, .5), 1)}|{_fmt(percentile(walls, .95), 1)}|"
                    f"{_fmt(statistics.mean(_values(counted, 'input_tokens')), 0) if _values(counted, 'input_tokens') else '—'}|"
                    f"{_mean_plain(counted, 'unconfirmed_rate')}|{_mean_plain(counted, 'recall_per_mtoken')}|"
                    f"{_mean_plain(counted, 'recall_per_min')}|{sum(r.get('attempts_total') or 0 for r in selected)}|"
                    f"{sum(r.get('stall_count') or 0 for r in selected)}|")
    text += ["", "### 根拠の到達（正解の資料の Recall）", "",
             "(a) 本文に出た資料 (b) 参照資料一覧（sources・referenced_docs）にある資料 (c) data.claims の evidence_refs にある資料。", "",
             "|構成|範囲|(a)本文|(b)参照一覧|(c)claims|", "|---|---|---:|---:|---:|"]
    for style, scope, selected, counted in scoped:
        if selected:
            text.append(f"|{style}|{scope}|" + "|".join(_nested_mean(counted, "evidence_reach", part, "recall")
                                                         for part in ("body", "referenced", "claims")) + "|")
    text += ["", "### グラフ・ゲート・見直し", "",
             "グラフ率は impact_calls で割った合算。closed は計画書の [-] の行数。見直しの増分は、見直し前の候補(review-1)と"
             "最終の回答本文を同じ基準で採点した正解資料の件数差。", "",
             "|構成|範囲|impact_incomplete率|depth打ち切り率|closed平均|graph未完で受理|見直し回数|見直しで増えた正解|1回あたり|",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for style, scope, selected, counted in scoped:
        if not selected:
            continue
        gates = [r["gate"] for r in counted if isinstance(r.get("gate"), dict)]
        reviewed = [r["review"] for r in counted if isinstance(r.get("review"), dict)
                    and isinstance(r["review"].get("gained_hits"), int)]
        requests = sum(v["requests"] for v in reviewed if isinstance(v.get("requests"), int))
        gained = sum(v["gained_hits"] for v in reviewed)
        text.append(f"|{style}|{scope}|{_rate(counted, 'impact_incomplete')}|{_rate(counted, 'impact_depth_truncated')}|"
                    f"{_mean_plain(counted, 'gate_closed_count')}|"
                    f"{sum(bool(g.get('accepted_with_graph_incomplete')) for g in gates)}/{len(gates)}|"
                    f"{requests if reviewed else '—'}|{gained if reviewed else '—'}|"
                    f"{_fmt(gained / requests) if reviewed and requests else '—'}|")
    return "\n".join(text) + "\n"


def _question_key(row: dict) -> tuple[str, str]:
    return row["system"], str(row["id"])


def compare_markdown(rows_a: list[dict], rows_b: list[dict], label_a: str = "A", label_b: str = "B") -> str:
    """同じ質問・同じ構成を別の出力フォルダで 2 回回した結果の比較。差は A − B。完答(a)と製品部分(b)が両方にある質問だけ。"""
    text = ["# 反復の比較", "", f"A={label_a} / B={label_b}。差は A − B。両方で有効（完答または製品部分）な質問だけを比べる。", "",
            "## 構成ごとの差", "",
            "|構成|比較できた質問|Recall A平均|Recall B平均|Recall差の平均|Recall差の標準偏差|Precision差の平均|Precision差の標準偏差|",
            "|---|---:|---:|---:|---:|---:|---:|---:|"]
    pairs: dict[str, list[tuple[tuple[str, str], dict, dict]]] = {}
    for style in STYLES:
        a = {_question_key(r): r for r in rows_a if r["style"] == style and r["counted"]}
        b = {_question_key(r): r for r in rows_b if r["style"] == style and r["counted"]}
        pairs[style] = [(k, a[k], b[k]) for k in sorted(a.keys() & b.keys())]

    def diffs(items, key):
        return [x[key] - y[key] for _k, x, y in items
                if isinstance(x.get(key), (int, float)) and isinstance(y.get(key), (int, float))]

    def mean_sd(values):
        return (_fmt(statistics.mean(values)) if values else "—",
                _fmt(statistics.stdev(values)) if len(values) > 1 else "—")

    for style in STYLES:
        items = pairs[style]
        ra = _values([x for _k, x, _y in items], "recall")
        rb = _values([y for _k, _x, y in items], "recall")
        rm, rs = mean_sd(diffs(items, "recall"))
        pm, ps = mean_sd(diffs(items, "precision"))
        text.append(f"|{style}|{len(items)}|{_fmt(statistics.mean(ra)) if ra else '—'}|"
                    f"{_fmt(statistics.mean(rb)) if rb else '—'}|{rm}|{rs}|{pm}|{ps}|")
    text += ["", "## 質問ごとの差", "", "|構成|系統|質問|Recall A|Recall B|Recall差|Precision A|Precision B|Precision差|",
             "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for style in STYLES:
        for (system, qid), x, y in pairs[style]:
            def cell(key):
                a, b = x.get(key), y.get(key)
                ok = isinstance(a, (int, float)) and isinstance(b, (int, float))
                return f"{_fmt(a) if isinstance(a, (int, float)) else '—'}|{_fmt(b) if isinstance(b, (int, float)) else '—'}|" \
                       f"{_fmt(a - b) if ok else '—'}"
            text.append(f"|{style}|{system}|{qid}|{cell('recall')}|{cell('precision')}|")
    text += ["", "## 構成間の勝ち負け（質問ごとに Recall が高い方）", "",
             "各回について、X と Y の両方で有効な質問を比べる。同点は引き分け。", "",
             "|回|構成X|構成Y|Xの勝ち|Yの勝ち|引き分け|", "|---|---|---|---:|---:|---:|"]
    for label, rows in ((label_a, rows_a), (label_b, rows_b)):
        recall = {style: {_question_key(r): r.get("recall") for r in rows if r["style"] == style and r["counted"]
                          and isinstance(r.get("recall"), (int, float))} for style in STYLES}
        for i, x in enumerate(STYLES):
            for y in STYLES[i + 1:]:
                common = recall[x].keys() & recall[y].keys()
                win_x = sum(recall[x][k] > recall[y][k] for k in common)
                win_y = sum(recall[x][k] < recall[y][k] for k in common)
                text.append(f"|{label}|{x}|{y}|{win_x}|{win_y}|{len(common) - win_x - win_y}|")
    return "\n".join(text) + "\n"


def next_run_seq(root: Path) -> int:
    """出力フォルダ内の全試行（再試行・smoke を含む）の run_seq の最大値 + 1。"""
    seqs = [0]
    for pattern in ("trials/*/*/*/result.json", "trials/*/*/*/retries/*/result.json",
                    "smoke/*/*/*/result.json", "smoke/*/*/*/retries/*/result.json"):
        for path in Path(root).glob(pattern):
            try:
                value = json.loads(path.read_text(encoding="utf-8")).get("run_seq")
            except (OSError, ValueError):
                continue
            if isinstance(value, int) and not isinstance(value, bool):
                seqs.append(value)
    return max(seqs) + 1


REACH_STAGES = ("adopted", "composed", "final_body", "final_sources", "final", "claims", "found_docs")


def _join_values(items: list[dict], key: str) -> str:
    seen = sorted({str(i[key]) for i in items if key in i and i[key] is not None})
    return "/".join(seen) if seen else "—"


def _total(rows: list[dict], getter) -> int | None:
    values = [v for v in (getter(r) for r in rows) if isinstance(v, int) and not isinstance(v, bool)]
    return sum(values) if values else None


def _ratio(num: int | None, den: int | None) -> str:
    return f"{num / den:.3f}" if num is not None and den else "—"


def _stage_scopes(rows: list[dict], style: str, systems):
    mine = [r for r in rows if r.get("style") == style and r.get("counted")]
    yield "all", mine
    for system in systems:
        yield system, [r for r in mine if r.get("system") == system]


def stages_markdown(rows: list[dict], systems=SYSTEMS) -> str:
    """採用・加工・最終の各段の到達、加工での欠落、子のスレッド・トークン、見直しの意味、反復番号ごとの値。
    完答(a)と製品部分(b)だけ。記録の無い試行（古い出力）は母数に入れない。"""
    text = ["", "## 到達の段階別集計", "",
            "段: adopted=アプリが採用した回答（加工前）/ composed=回答加工の直後 / final_body=最終の本文だけ / "
            "final_sources=参照資料一覧だけ / final=本文＋参照資料一覧 / claims=data.claims の evidence_refs / "
            "found_docs=検索で見つかっただけの候補。延べは試行をまたいで合算（正解数・予測数・命中数）、平均は問題ごとの値の平均。", "",
            "|構成|範囲|段|試行|命中|予測|正解|延べRecall|延べPrecision|平均Recall|平均Precision|",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for style in STYLES:
        for scope, selected in _stage_scopes(rows, style, systems):
            if not selected:
                continue
            for stage in REACH_STAGES:
                have = [r for r in selected if isinstance(_nested(r, "reach_v2", stage), dict)]
                if not have:
                    text.append(f"|{style}|{scope}|{stage}|0|—|—|—|—|—|—|—|")
                    continue
                cell = lambda key: _total(have, lambda r: _nested(r, "reach_v2", stage, key))  # noqa: E731
                gold = _total(have, lambda r: len(r.get("expected_paths") or []))
                hits, pred = cell("hits"), cell("pred")
                text.append(f"|{style}|{scope}|{stage}|{len(have)}|{hits}|{pred}|{gold}|{_ratio(hits, gold)}|"
                            f"{_ratio(hits, pred)}|{_nested_mean(have, 'reach_v2', stage, 'recall')}|"
                            f"{_nested_mean(have, 'reach_v2', stage, 'precision')}|")
    text += ["", "### 加工での欠落・追加", "",
             "lost=採用には出ていたが最終に無い正解の資料 / added=採用に無く最終にある資料（正解・正解以外）。", "",
             "|構成|範囲|試行|lost延べ|lostのあった試行|added正解延べ|added正解以外延べ|", "|---|---|---:|---:|---:|---:|---:|"]
    for style in STYLES:
        for scope, selected in _stage_scopes(rows, style, systems):
            have = [r for r in selected if isinstance(r.get("lost_in_processing"), dict)]
            if not selected:
                continue
            lost = _total(have, lambda r: r["lost_in_processing"].get("count"))
            text.append(f"|{style}|{scope}|{len(have)}|{lost if lost is not None else '—'}|"
                        f"{sum(bool(r['lost_in_processing'].get('count')) for r in have) if have else '—'}|"
                        f"{_total(have, lambda r: _nested(r, 'added_in_processing', 'gold')) if have else '—'}|"
                        f"{_total(have, lambda r: _nested(r, 'added_in_processing', 'non_gold')) if have else '—'}|")
    text += ["", "### 子（下調べ役）のスレッドとトークン", "",
             "子のスレッド数は timeline の children.threads（無ければ activity の子の数）。spawn_count は使わない。"
             "トークンは親・子それぞれ activity の agents の合計。", "",
             "|構成|範囲|試行|子のスレッドのあった試行|親input|親cached|親output|子input|子cached|子output|",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for style in STYLES:
        for scope, selected in _stage_scopes(rows, style, systems):
            if not selected:
                continue
            threads = [r for r in selected if isinstance(r.get("subagent_threads"), int)]

            def tok(side, key):
                value = _total(selected, lambda r: _nested(r, f"tokens_{side}", key))
                return value if value is not None else "—"

            text.append(f"|{style}|{scope}|{len(threads)}|{sum(r['subagent_threads'] > 0 for r in threads) if threads else '—'}|"
                        + "|".join(str(tok(side, key)) for side in ("parent", "child")
                                   for key in ("input", "cached", "output")) + "|")
    text += ["", "### 見直しの意味（製品の記録）", "",
             "|構成|試行|app_review_cap|app要求回数(実際)の合計|app要求回数の平均|instruction_review_cap|app_gating|",
             "|---|---:|---|---:|---:|---|---|"]
    for style in STYLES:
        sem = [r["review_semantics"] for r in rows if r.get("style") == style and r.get("counted")
               and isinstance(r.get("review_semantics"), dict)]
        requests = [{"v": s.get("app_review_requests")} for s in sem]
        total = _total(requests, lambda r: r["v"])
        text.append(f"|{style}|{len(sem)}|{_join_values(sem, 'app_review_cap')}|{total if total is not None else '—'}|"
                    f"{_mean_plain([{'v': s.get('app_review_requests')} for s in sem], 'v')}|"
                    f"{_join_values(sem, 'instruction_review_cap')}|{_join_values(sem, 'app_gating')}|")
    text += ["", "### 反復番号ごと", ""]
    reps = sorted({r["rep"] for r in rows if r.get("rep") is not None}, key=str)
    if not reps:
        text.append("反復番号の記録なし。")
    else:
        text += ["|rep|構成|試行|平均Recall|平均Precision|final延べRecall|lost延べ|", "|---|---|---:|---:|---:|---:|---:|"]
        for rep in reps:
            for style in STYLES:
                selected = [r for r in rows if r.get("rep") == rep and r.get("style") == style and r.get("counted")]
                if not selected:
                    continue
                have = [r for r in selected if isinstance(_nested(r, "reach_v2", "final"), dict)]
                gold = _total(have, lambda r: len(r.get("expected_paths") or []))
                lost = _total([r for r in selected if isinstance(r.get("lost_in_processing"), dict)],
                              lambda r: r["lost_in_processing"].get("count"))
                text.append(f"|{rep}|{style}|{len(selected)}|{_mean_plain(selected, 'recall')}|{_mean_plain(selected, 'precision')}|"
                            f"{_ratio(_total(have, lambda r: _nested(r, 'reach_v2', 'final', 'hits')), gold)}|"
                            f"{lost if lost is not None else '—'}|")
    return "\n".join(text) + "\n"


GATE_VERDICT_KEYS = ("decision", "verdict", "status", "outcome")


def target_gate_markdown(rows: list[dict]) -> str:
    """製品が記録した target_check と gate_log の集計。完答(a)と製品部分(b)だけ。記録の無い試行は欠測として別に数える。"""
    counted = [r for r in rows if r.get("counted")]
    text = ["", "## 対象の確認（target_check）と関門の判定（gate_log）", "",
            "完答(a)と製品部分(b)だけ。記録の無い試行は「記録なし」で、0 件とは区別する。", ""]
    checks = [r["target_check"] for r in counted if isinstance(r.get("target_check"), dict)]
    text += [f"target_check: 記録あり {len(checks)} 試行 / 記録なし {len(counted) - len(checks)} 試行。", ""]
    if checks:
        status = collections.Counter(str(c.get("status")) for c in checks)
        decision = collections.Counter(str(c.get("decision")) for c in checks)
        dry = collections.Counter("dry_run" if c.get("dry_run") else "実運用" if c.get("dry_run") is False else "不明"
                                  for c in checks)
        missing = [c.get("missing_count") for c in checks]
        known = [m for m in missing if isinstance(m, int) and not isinstance(m, bool)]
        text += ["|status|試行|", "|---|---:|", *(f"|{k}|{v}|" for k, v in sorted(status.items())), "",
                 "|decision|試行|", "|---|---:|", *(f"|{k}|{v}|" for k, v in sorted(decision.items())), "",
                 "|dry_run の別|試行|", "|---|---:|", *(f"|{k}|{v}|" for k, v in sorted(dry.items())), "",
                 f"missing の延べ: {sum(known) if known else '—'}"
                 f"（missing_count の記録あり {len(known)} / {len(checks)} 試行、1 件以上あった試行 {sum(m > 0 for m in known)}）。", ""]
    logs = [r["gate_log"] for r in counted if isinstance(r.get("gate_log"), list)]
    text += [f"gate_log: 記録あり {len(logs)} 試行 / 記録なし {len(counted) - len(logs)} 試行。", ""]
    if logs:
        verdicts = collections.Counter(
            str(next((e[k] for k in GATE_VERDICT_KEYS if e.get(k) is not None), "判定の記録なし"))
            if isinstance(e, dict) else str(e) for log in logs for e in log)
        text += ["判定は各 final の記録の " + " / ".join(GATE_VERDICT_KEYS) + " のうち最初にある値。", "",
                 "|判定|件数|", "|---|---:|", *(f"|{k}|{v}|" for k, v in sorted(verdicts.items())), ""]
    return "\n".join(text) + "\n"
