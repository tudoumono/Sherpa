#!/usr/bin/env python3
"""実 API で台帳 3 構成を計測し、回答・記録・採点をリポジトリ外へ保存する。"""
from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from scripts.bench import ledger_scoring as scoring
except ImportError:
    import ledger_scoring as scoring

ROOT = Path(__file__).resolve().parents[2]
STYLES = scoring.STYLES
SYSTEMS = {"code": 10, "multihop": 15, "jemhop": 10}
DEFAULT_MODEL = "gpt-5.6-sol"
MODEL = DEFAULT_MODEL
SELF_SYSTEM = "self"
AUTH_MODES = ("chatgpt", "apikey")
CLI_VERSION = "0.162.1"
PROJECT = "sherpa-ledger-eval"
DATABASE = "sherpa_ledger_eval"
# 初期化（スキーマ適用・DB 作成）を利用者が承認した専用ストアの組（Compose project・Postgres のポート）。
APPROVED_STORES = {("sherpa-ledger-eval", 15433), ("sherpa-ledger-eval-api", 15434), ("sherpa-ledger-eval-api2", 15435),
                   ("sherpa-ledger-eval-sub", 15436), ("sherpa-ledger-eval-sub-self", 15437),
                   ("sherpa-ledger-eval-luna", 15438), ("sherpa-ledger-eval-luna-self", 15439),
                   ("sherpa-ledger-eval-luna2", 15440),
                   ("sherpa-ledger-eval-r2-self", 15441), ("sherpa-ledger-eval-r2-three", 15442),
                   ("sherpa-ledger-eval-rep-self", 15443), ("sherpa-ledger-eval-rep-three", 15444),
                   ("sherpa-ledger-eval-c2-self", 15445), ("sherpa-ledger-eval-c2-three", 15446),
                   ("sherpa-ledger-eval-c3-self-r1", 15447), ("sherpa-ledger-eval-c3-three-r1", 15448),
                   ("sherpa-ledger-eval-c3-self-r2", 15449), ("sherpa-ledger-eval-c3-three-r2", 15450),
                   ("sherpa-ledger-eval-b1-a", 15451), ("sherpa-ledger-eval-b1-b", 15452), ("sherpa-ledger-eval-b1-c", 15453),
                   ("sherpa-ledger-eval-b1-d", 15454), ("sherpa-ledger-eval-b1-e", 15455),
                   ("sherpa-ledger-eval-g1-a", 15456), ("sherpa-ledger-eval-g1-b", 15457), ("sherpa-ledger-eval-g1-c", 15458), ("sherpa-ledger-eval-g1-d", 15459), ("sherpa-ledger-eval-g1-e", 15460), ("sherpa-ledger-eval-g1-f", 15461), ("sherpa-ledger-eval-g1-g", 15462), ("sherpa-ledger-eval-g1-h", 15463),
                   ("sherpa-ledger-eval-d1-a", 15464), ("sherpa-ledger-eval-d1-b", 15465), ("sherpa-ledger-eval-d1-t", 15466),
                   ("sherpa-ledger-eval-f1-a", 15467), ("sherpa-ledger-eval-f1-b", 15468),
                   ("sherpa-ledger-eval-cc-a", 15495), ("sherpa-ledger-eval-cc-b", 15496), ("sherpa-ledger-eval-cc-c", 15497), ("sherpa-ledger-eval-cc-d", 15498)}
LIMIT_RE = scoring.LIMIT_RE
PATH_RE = re.compile(r"(?:[\w@+%.,~=-]+/)*[\w@+%.,~=-]+\.(?:md|txt|cbl|cob|cpy|jcl|"
                     r"csv|log|json|pdf|docx?|xlsx?|pptx?|sql|py|java|js|ts|yaml|yml)", re.I)


EvalError = scoring.EvalError
UsageLimit = scoring.UsageLimit
InfrastructureStop = scoring.InfrastructureStop
StallTimeout = scoring.StallTimeout


def require_free_port(port: int) -> None:
    """専用ポートの loopback・wildcard の占有を確認する。"""
    for host in ("127.0.0.1", "0.0.0.0"):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                raise EvalError(f"専用ポートが使用中: {port}") from None


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as out:
        out.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    tmp.replace(path)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def outside_repo(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any((p / ".git").exists() for p in (resolved, *resolved.parents)):
        raise EvalError("評価データ・結果はリポジトリ外を指定してください")
    return resolved


def relative_path(value: str) -> str:
    path = urllib.parse.unquote(value).replace("\\", "/").strip(" `\"'")
    path = re.sub(r"(?::\d+(?:[-–]\d+)?|#L\d+(?:-L?\d+)?)$", "", path)
    while path.startswith("./"):
        path = path[2:]
    return path


def read_manifest(path: Path) -> tuple[dict, str]:
    raw = outside_repo(path).read_bytes()
    manifest = json.loads(raw)
    root = outside_repo(Path(manifest["corpus_root"]))
    if not root.is_dir():
        raise EvalError("corpus_root がありません")
    files = manifest.get("corpus_files")
    if not isinstance(files, list) or not files or any(not isinstance(p, str) for p in files):
        raise EvalError("corpus_files に全資料の相対パスを指定してください")
    normalized = [relative_path(p) for p in files]
    if len(set(normalized)) != len(normalized):
        raise EvalError("corpus_files のパスが重複しています")
    corpus_hash = hashlib.sha256()
    for p in sorted(normalized):
        candidate = root / p
        if Path(p).is_absolute() or ".." in Path(p).parts or not candidate.is_file():
            raise EvalError("corpus_files に範囲外・存在しないパスがあります")
        if not candidate.resolve().is_relative_to(root):
            raise EvalError("corpus_files が登録ルート外を参照しています")
        corpus_hash.update(p.encode() + b"\0" + hashlib.sha256(candidate.read_bytes()).digest())
    if {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()} != set(normalized):
        raise EvalError("登録ルートに corpus_files 外のファイルがあります。正解・manifest は外に置いてください")
    manifest["corpus_files"] = normalized
    manifest["corpus_sha256"] = corpus_hash.hexdigest()
    ids = set()
    for q in manifest["questions"]:
        key = (q["system"], q["id"])
        if (q["system"] not in SYSTEMS and q["system"] != SELF_SYSTEM) or key in ids or not re.fullmatch(r"[\w.-]+", str(q["id"])):
            raise EvalError("質問の system/id が不正・重複しています")
        ids.add(key)
        if not isinstance(q.get("question"), str) or not q["question"].strip():
            raise EvalError("空の質問は実行できません")
        if not isinstance(q.get("hop"), int) or q["hop"] < 0:
            raise EvalError("hop は 0 以上の整数です")
        expected = [relative_path(p) for p in q["expected_paths"]]
        if not set(expected).issubset(normalized):
            raise EvalError("正解パスが corpus_files にありません")
        q["expected_paths"] = sorted(set(expected))
        scopes = q.setdefault("scope_paths", [q["system"]])
        # self 系統は登録ルート全体が資料フォルダなので、空＝全体を許す（範囲はフォルダで指定し、ファイルは指定できない）。
        if (not scopes and q["system"] != SELF_SYSTEM) or any(Path(p).is_absolute() or ".." in Path(p).parts for p in scopes):
            raise EvalError("scope_paths は登録ルート内の相対パスです")
        if scopes and any(not any(p == s or p.startswith(s.rstrip("/") + "/") for s in scopes) for p in expected):
            raise EvalError("正解パスが質問の範囲から外れています")
    manifest["corpus_root"] = str(root)
    return manifest, hashlib.sha256(raw).hexdigest()


def check_manifest(manifest: dict) -> list[dict]:
    """実行前の点検。manifest の全問題を読むだけで検査し、問題ごとの指摘（error）を返す。モデル・API・Docker は使わない。
    scope_paths の既定値補完はせず、書かれていない・フォルダでないものを指摘する（API が 422 で落ちる前に見つける）。"""
    files = manifest.get("corpus_files")
    files = [relative_path(p) for p in files if isinstance(p, str)] if isinstance(files, list) else []
    known, folders = set(files), {parent for p in files for parent in map(str, Path(p).parents) if parent != "."}
    findings, seen = [], set()
    for index, q in enumerate(manifest.get("questions") or []):
        if not isinstance(q, dict):
            findings.append({"question": f"#{index}", "error": "問題が辞書ではありません"})
            continue
        system, qid = q.get("system"), q.get("id")
        name = f"{system}/{qid}"
        errors = []
        if system not in SYSTEMS and system != SELF_SYSTEM:
            errors.append(f"system が不正: {system!r}")
        if not isinstance(qid, (str, int)) or not re.fullmatch(r"[\w.-]+", str(qid)):
            errors.append(f"id が不正: {qid!r}")
        if (system, qid) in seen:
            errors.append("system/id が重複")
        seen.add((system, qid))
        if not isinstance(q.get("question"), str) or not q["question"].strip():
            errors.append("question が空")
        if not isinstance(q.get("hop"), int) or isinstance(q.get("hop"), bool) or q["hop"] < 0:
            errors.append(f"hop は 0 以上の整数: {q.get('hop')!r}")
        expected = q.get("expected_paths")
        if not isinstance(expected, list) or any(not isinstance(p, str) for p in expected):
            errors.append("expected_paths は文字列のリスト")
            expected = []
        scopes = q.get("scope_paths")
        if not isinstance(scopes, list) or any(not isinstance(p, str) for p in scopes):
            errors.append("scope_paths がない（既定値の補完に頼らず明記する）")
            scopes = []
        elif not scopes and system != SELF_SYSTEM:
            errors.append("scope_paths が空（self 系統以外は系統のフォルダが必要）")
        for scope in scopes:
            norm = scope.rstrip("/")
            if Path(scope).is_absolute() or ".." in Path(scope).parts:
                errors.append(f"scope_paths が登録ルート外: {scope}")
            elif norm not in folders:
                errors.append(f"scope_paths が corpus_files のフォルダでない: {scope}")
        missing = sorted({relative_path(p) for p in expected} - known)
        if missing:
            errors.append(f"expected_paths が corpus_files にない: {len(missing)} 件（例 {missing[0]}）")
        if scopes and not missing:
            outside = [p for p in map(relative_path, expected) if not any(p.startswith(s.rstrip("/") + "/") for s in scopes)]
            if outside:
                errors.append(f"expected_paths が scope_paths の外: {len(outside)} 件（例 {outside[0]}）")
        if q.get("null") is True and expected:
            errors.append("null の問題は expected_paths が空")
        elif q.get("null") is not True and not expected:
            errors.append("expected_paths が空なのに null でない")
        findings.append({"question": name, "error": "; ".join(errors) or None})
    return findings


def predicted_paths(answer: dict, corpus_files: list[str]) -> list[str]:
    """本文と参照資料から実在・非実在を含む資料パス集合を得る。"""
    body = answer.get("body", answer.get("headline", "")) or ""
    raw = set()
    covered = []
    by_name: dict[str, list[str]] = collections.defaultdict(list)
    for p in corpus_files:
        by_name[Path(p).name].append(p)
    candidates = set(corpus_files) | {name for name, paths in by_name.items() if len(paths) == 1}
    for p in candidates:
        for match in re.finditer(r"(?<![\w/])" + re.escape(p) + r"(?![\w/])", body):
            raw.add(p)
            covered.append(match.span())
    for match in PATH_RE.finditer(body):
        if not any(start <= match.start() and match.end() <= end for start, end in covered):
            raw.add(match.group())
    for key in ("referenced_docs", "sources", "sources_unverified"):
        for ref in answer.get(key) or []:
            if not isinstance(ref, dict):
                continue
            if isinstance(ref.get("download_url"), str):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(ref["download_url"]).query)
                if query.get("rel"):
                    raw.update(query["rel"])
                    continue
            value = ref.get("path") or ref.get("doc_id")
            if isinstance(value, str) and value:
                raw.add(value)
    known = set(corpus_files)
    out = set()
    for p in raw:
        p = relative_path(p)
        if p in known:
            out.add(p)
            continue
        matches = [k for k in known if p.endswith("/" + k)]
        if len(matches) == 1:
            out.add(matches[0])
        elif "/" not in p and len(by_name[p]) == 1:
            out.add(by_name[p][0])
        else:
            out.add(p)
    return sorted(out)


def score_answer(answer: dict, question: dict, corpus_files: list[str]) -> dict:
    pred = set(predicted_paths(answer, corpus_files))
    gold = set(question["expected_paths"])
    common = pred & gold
    body = answer.get("body", answer.get("headline", "")) or ""
    null = question.get("null", False)
    text_answer = question.get("answer")
    return {"predicted_paths": sorted(pred), "expected_paths": sorted(gold),
            "missing_paths": sorted(gold - pred), "extra_paths": sorted(pred - gold),
            "recall": len(common) / len(gold) if gold else None,
            "precision": len(common) / len(pred) if pred else None,
            "answer_contains": str(text_answer).casefold() in body.casefold()
            if text_answer is not None and not null else None,
            "null_answer": bool(re.search(r"該当(?:する.{0,15})?なし|該当.{0,15}(?:ない|ありません)|"
                                           r"根拠.{0,15}(?:ない|ありません)|no (?:answer|relevant|evidence)|"
                                           r"not (?:found|answerable)|insufficient (?:information|evidence)", body, re.I))
            if null else None, "null_no_evidence": not pred if null else None}


def evidence_reach(answer: dict, gold: set[str], corpus_files: list[str]) -> dict:
    """正解の資料が (body)本文 (referenced)参照資料一覧 (claims)data.claims の evidence_refs のどこに出たか。"""
    body = answer.get("body", answer.get("headline", "")) or ""
    claims = (answer.get("data") or {}).get("claims") if isinstance(answer.get("data"), dict) else None
    refs = [r for c in claims or [] if isinstance(c, dict) for r in c.get("evidence_refs") or [] if isinstance(r, str)]
    found = {"body": predicted_paths({"body": body}, corpus_files),
             "referenced": predicted_paths({k: answer.get(k) for k in ("sources", "referenced_docs")}, corpus_files),
             "claims": predicted_paths({"referenced_docs": [{"path": r} for r in refs]}, corpus_files)}
    out = {"expected": len(gold)}
    for name, paths in found.items():
        hits = len(gold & set(paths))
        out[name] = {"hits": hits, "recall": hits / len(gold) if gold else None}
    return out


def review_gain(target: Path, gold: set[str], final_body: str, corpus_files: list[str], requests, cap) -> dict:
    """見直しを頼む直前の候補(review-<K>.md)と最終の回答本文を同じ基準（本文のみ）で採点し、増えた正解の資料数を出す。"""
    files = sorted((p for p in target.glob("review-*.md") if p.stem[7:].isdecimal()), key=lambda p: int(p.stem[7:]))

    def score(text: str) -> dict:
        pred = set(predicted_paths({"body": text}, corpus_files))
        hits = len(pred & gold)
        return {"hits": hits, "recall": hits / len(gold) if gold else None, "precision": hits / len(pred) if pred else None}

    candidates = [{"k": int(p.stem[7:]), **score(p.read_text(encoding="utf-8"))} for p in files]
    final = score(final_body)
    before = candidates[0] if candidates else None

    def delta(key):
        if before and final[key] is not None and before[key] is not None:
            return final[key] - before[key]
        return None

    return {"review_cap": cap, "requests": requests if isinstance(requests, int) else (len(candidates) or None),
            "candidates": candidates, "before": before, "final": final,
            "recall_delta": delta("recall"), "precision_delta": delta("precision"),
            "gained_hits": final["hits"] - before["hits"] if before else None}


def _stage(paths, gold: set[str]) -> dict | None:
    """予測した資料の集合を正解と照らす。集合が得られなければ欠測。"""
    if paths is None:
        return None
    hits = len(gold & set(paths))
    return {"hits": hits, "pred": len(paths), "recall": hits / len(gold) if gold else None,
            "precision": hits / len(paths) if paths else None}


def _found_doc_paths(answer: dict, corpus_files: list[str]) -> list[str] | None:
    found = answer.get("found_docs")
    if not isinstance(found, list):
        return None
    refs = [{"path": d} if isinstance(d, str) else d for d in found if isinstance(d, (str, dict))]
    return predicted_paths({"referenced_docs": refs}, corpus_files)


def reach_stages(target: Path, answer: dict, gold: set[str], corpus_files: list[str]) -> dict:
    """採用・加工・最終の各段の到達（正解の資料に対する hits/pred/recall/precision）と、加工での欠落・追加。
    adopted.md・composed.md の無い古い記録は該当の段を欠測にする。"""
    def text(name: str):
        return (target / name).read_text(encoding="utf-8") if (target / name).is_file() else None

    adopted_text, composed_text = text("adopted.md"), text("composed.md")
    adopted = predicted_paths({"body": adopted_text}, corpus_files) if adopted_text is not None else None
    composed = predicted_paths({"body": composed_text}, corpus_files) if composed_text is not None else None
    claims = (answer.get("data") or {}).get("claims") if isinstance(answer.get("data"), dict) else None
    refs = [r for c in claims or [] if isinstance(c, dict) for r in c.get("evidence_refs") or [] if isinstance(r, str)]
    body = answer.get("body", answer.get("headline", "")) or ""
    final = predicted_paths(answer, corpus_files)
    stages = {"adopted": adopted, "composed": composed,
              "final_body": predicted_paths({"body": body}, corpus_files),
              "final_sources": predicted_paths({k: answer.get(k) for k in ("sources", "referenced_docs", "sources_unverified")},
                                              corpus_files),
              "final": final, "claims": predicted_paths({"referenced_docs": [{"path": r} for r in refs]}, corpus_files),
              "found_docs": _found_doc_paths(answer, corpus_files)}
    lost = added = None
    if adopted is not None:
        lost_paths = sorted((set(adopted) & gold) - set(final))
        new = set(final) - set(adopted)
        lost = {"paths": lost_paths, "count": len(lost_paths)}
        added = {"gold": len(new & gold), "non_gold": len(new - gold)}
    return {"reach_v2": {name: _stage(paths, gold) for name, paths in stages.items()},
            "lost_in_processing": lost, "added_in_processing": added}


def _role_tokens(agents, role: str) -> dict | None:
    """activity.agents のうち role が一致するものの input・cached・output の合計。activity が無ければ欠測。"""
    if not isinstance(agents, list):
        return None
    picked = [a for a in agents if isinstance(a, dict) and a.get("role") == role]
    if not picked:
        return {"input": 0, "cached": 0, "output": 0}
    return {out: _token_sum(picked, key) for out, key in
            (("input", "input_tokens"), ("cached", "cached_input_tokens"), ("output", "output_tokens"))}


def extra_metrics(target: Path, result: dict, corpus_files: list[str]) -> dict:
    """保存済みの回答・stats・計画書・見直し候補から追加指標を作る。記録の無い項目は None（欠測）。"""
    def read(name: str):
        return load(target / name) if (target / name).is_file() else None

    answer, stats = read("answer.json"), read("stats.json")
    gold = set(result.get("expected_paths") or [])
    gate = stats.get("gate") if isinstance(stats, dict) and isinstance(stats.get("gate"), dict) else None
    closed_rows = gate.get("closed_rows") if gate and isinstance(gate.get("closed_rows"), list) else None
    plan = target / "plan.md"
    closed = (sum(bool(re.match(r"\s*-\s*\[-\]", line)) for line in plan.read_text(encoding="utf-8").splitlines())
              if plan.is_file() else len(closed_rows) if closed_rows is not None else None)
    reasons = (dict(collections.Counter(str(r.get("reason")) for r in closed_rows if isinstance(r, dict)))
               if closed_rows is not None else None)
    final_body = (answer.get("body", answer.get("headline", "")) or "") if isinstance(answer, dict) else ""
    timeline = read("timeline.json")
    activity = answer.get("activity") if isinstance(answer, dict) else None
    agents = activity.get("agents") if isinstance(activity, dict) else None
    threads = (timeline.get("children") or {}).get("threads") if isinstance(timeline, dict) \
        and isinstance(timeline.get("children"), dict) else None
    if not isinstance(threads, int) and isinstance(agents, list):
        threads = sum(isinstance(a, dict) and a.get("role") == "child" for a in agents)
    stage = reach_stages(target, answer, gold, corpus_files) if isinstance(answer, dict) else {}

    def block(key: str, kind):
        value = stats.get(key) if isinstance(stats, dict) else None
        return value if isinstance(value, kind) else None

    return {**stage, "target_check": block("target_check", dict), "gate_log": block("gate_log", list),
            "plan_normalizations": block("plan_normalizations", (dict, list)),
            "candidate": block("candidate", dict), "compose": block("compose", dict),
            "review_semantics": block("review", dict), "trimmed": block("trimmed", list),
            "subagent_threads": threads if isinstance(threads, int) else None,
            "tokens_parent": _role_tokens(agents, "parent"), "tokens_child": _role_tokens(agents, "child"),
            "evidence_reach": evidence_reach(answer, gold, corpus_files) if isinstance(answer, dict) else None,
            "graph": stats.get("graph") if isinstance(stats, dict) and isinstance(stats.get("graph"), dict) else None,
            "gate": gate, "gate_closed_count": closed, "gate_closed_reasons": reasons,
            "review": review_gain(target, gold, final_body, corpus_files, result.get("review_requests"),
                                  stats.get("review_cap") if isinstance(stats, dict) else None)
            if isinstance(answer, dict) else None}


def stream_events(stream, out, started: float, timeout: float, stall_timeout: float):
    """SSE を 1 イベントずつ返して out に追記する。壁時計の上限超過は EvalError、進行（data 行）が
    stall_timeout 秒途絶えたら StallTimeout（keepalive のコメント行は進行に数えない）。"""
    progress = time.monotonic()
    try:
        for line in stream:
            now = time.monotonic()
            if now - started > timeout:
                raise EvalError("試行の壁時計上限を超えました")
            if not line.startswith(b"data:"):
                if now - progress > stall_timeout:
                    raise StallTimeout(f"{stall_timeout:g} 秒間イベントがありません")
                continue
            event = json.loads(line[5:].decode())
            out.write(json.dumps(event, ensure_ascii=False) + "\n")
            out.flush()
            progress = now
            yield event
    except socket.timeout:
        raise StallTimeout(f"{stall_timeout:g} 秒間イベントがありません") from None


def validate_run_conditions(answer: dict, stats, style: str, model: str = MODEL) -> None:
    """実行時のモデル・深さ・台帳構成が指定条件に一致することを確かめる。"""
    settings = (answer.get("activity") or {}).get("settings") or {}
    if settings.get("model") != model or settings.get("depth") != "standard":
        raise EvalError("実行時のモデル・深さが指定条件と異なるか欠測です")
    if settings.get("mode") != "standard" or settings.get("schema_level") != 2:
        raise EvalError("実行時のモード・出力スキーマが指定条件と異なるか欠測です")
    if not isinstance(stats, dict) or stats.get("style") != style:
        raise EvalError("実行時の台帳構成が指定条件と異なるか欠測です")


def _git(root: Path, *argv: str) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(["git", "-C", str(root), *argv], env=env, capture_output=True, timeout=120)
    if result.returncode:
        raise EvalError("計測 checkout の状態を確認できません")
    return result.stdout


def dirty_state(root: Path) -> dict:
    """未コミット差分（追跡対象・バイナリ含む）の sha256 と変更ファイル一覧。未追跡は bench・sherpa・tests 配下だけ一覧にする。"""
    diff = _git(root, "diff", "HEAD", "--binary")
    changed = sorted(_git(root, "diff", "HEAD", "--name-only").decode("utf-8", errors="replace").splitlines())
    untracked = sorted(_git(root, "ls-files", "--others", "--exclude-standard", "--",
                            "scripts/bench", "sherpa", "tests").decode("utf-8", errors="replace").splitlines())
    return {"diff_sha256": hashlib.sha256(diff).hexdigest(), "changed_files": changed, "untracked_files": untracked}


def fixed_checkout(root: Path, expected_commit: str | None = None, *, allow_dirty: bool = False,
                   expected_diff: str | None = None) -> str:
    """追跡対象の未コミット変更と計測中の HEAD 変更を拒否する。
    allow_dirty のときは未コミット差分を許し、expected_diff（計測開始時の差分の sha256）との一致を確かめる。"""
    commit = _git(root, "rev-parse", "HEAD").decode().strip()
    if allow_dirty:
        if expected_diff is not None and dirty_state(root)["diff_sha256"] != expected_diff:
            raise EvalError("計測 checkout の未コミット差分が計測開始時から変わりました")
        dirty = ""
    else:
        dirty = _git(root, "status", "--porcelain", "--untracked-files=no").decode().strip()
    if dirty or (expected_commit is not None and commit != expected_commit):
        raise EvalError("計測 checkout に追跡対象の未コミット変更または HEAD 変更があります")
    return commit


def checkout_guard(runner) -> str:
    """Runner の計測条件（--allow-dirty なら差分の sha256 も）で checkout を確かめる。"""
    if getattr(runner, "allow_dirty", False):
        return fixed_checkout(ROOT, runner.commit, allow_dirty=True, expected_diff=runner.diff_sha256)
    return fixed_checkout(ROOT, runner.commit)


SECRET_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD")


def env_identity(env: dict) -> dict:
    """API へ渡す SHERPA_ の環境変数。秘密（名前に KEY・TOKEN・SECRET・PASSWORD）は名前だけで、値は記録・ハッシュに入れない。"""
    plain = {k: v for k, v in sorted(env.items()) if k.startswith("SHERPA_") and not SECRET_RE.search(k)}
    secret = sorted(k for k in env if k.startswith("SHERPA_") and SECRET_RE.search(k))
    digest = hashlib.sha256(json.dumps({"values": plain, "secret_names": secret}, sort_keys=True).encode()).hexdigest()
    return {"sherpa_env": plain, "sherpa_env_secret_names": secret, "sherpa_env_sha256": digest}


def _token_sum(agents, key: str):
    """全エージェントの値が整数で揃うときだけ合計する。欠測を 0 にしない。"""
    if agents and all(isinstance(a.get("tokens", {}).get(key), int) for a in agents):
        return sum(a["tokens"][key] for a in agents)
    return None


def _new_input_tokens(agents):
    """キャッシュに当たらなかった入力（入力合計 - キャッシュ分）。どちらかが欠測なら欠測。"""
    total, cached = _token_sum(agents, "input_tokens"), _token_sum(agents, "cached_input_tokens")
    return max(0, total - cached) if total is not None and cached is not None else None


def measure(answer: dict, stats: dict | None, timeline: dict | None) -> dict:
    """tool_counts は CLI 外側の観測。Sherpa 道具別の回数と ES 利用はサーバ側の call_stats から数える。"""
    activity = answer.get("activity")
    server = scoring.observed_tools(answer.get("call_stats"))
    recorded = server["diagnostics"]
    agents = activity.get("agents") if isinstance(activity, dict) else None
    tools: dict[str, int] = collections.Counter()
    if agents is not None:
        for agent in agents:
            for name, value in agent.get("tools", {}).items():
                tools[name] += value.get("calls", 0)
    inv = answer.get("investigation") or {}
    total = inv.get("rows")
    if total is None:
        total = (stats or {}).get("json_population_count")
    if total is None and isinstance(inv.get("counts"), dict):
        non_terminal, missing = inv.get("non_terminal", []), inv.get("missing", [])
        if len(non_terminal) < 50 and len(missing) < 50 and not inv.get("invalid_total"):
            total = sum(inv["counts"].values()) + len(set(non_terminal + missing))
    unconfirmed = inv.get("unconfirmed_items")
    plan = (stats or {}).get("plan") or {}
    style = (stats or {}).get("style")
    attempts = (stats or {}).get("attempts")
    requests = (stats or {}).get("ledger_review_requests") if style == "json" else plan.get("review_requests")
    entries = (stats or {}).get("json_review_entries") if style == "json" else plan.get("plan_review_lines")
    return {"unconfirmed_rate": len(unconfirmed) / total
            if isinstance(unconfirmed, list) and total else None,
            "unconfirmed_count": len(unconfirmed) if isinstance(unconfirmed, list) else None,
            "population_count": total,
            "review_requests": requests if style in ("json", "plan_gate") else None,
            "review_entries": entries if style in ("json", "plan_main") else None,
            "review_count": requests if style == "plan_gate" else entries,
            "review_rounds": (inv.get("review") or {}).get("rounds") if style == "json"
            else plan.get("review_rounds") if style == "plan_gate" else plan.get("plan_review_lines"),
            "review_growth_rounds": plan.get("review_rounds") if style == "plan_gate" else None,
            "spawn_count": sum(v for k, v in tools.items() if k.endswith("spawn_agent")) if agents else None,
            "child_threads": sum(a.get("role") == "child" for a in agents) if agents else None,
            "auto_continue_count": (stats or {}).get("auto_continue_count"),
            "attempts": attempts, "resume_count": (stats or {}).get("resume_count"),
            "input_tokens": _token_sum(agents, "input_tokens"),
            "cached_input_tokens": _token_sum(agents, "cached_input_tokens"),
            "new_input_tokens": _new_input_tokens(agents),
            "output_tokens": _token_sum(agents, "output_tokens"),
            "tool_counts": dict(tools) if agents else None,
            "server_tool_counts": server["by_tool"], "server_tool_roles": server["by_role_tool"],
            "tool_observation_complete": server["observation_complete"],
            "tool_calls_recorded": recorded.get("recorded_calls"), "tool_calls_missing": recorded.get("missing"),
            "es_search_calls": server["es_search_calls"],
            "timeline": timeline.get("total") if timeline else None}


class API:
    def __init__(self, port: int, timeout: float):
        self.url = f"http://127.0.0.1:{port}"
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method: str, path: str, payload=None, *, stream=False, timeout=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            response = self.opener.open(req, timeout=timeout if timeout is not None else self.timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 or LIMIT_RE.search(body):
                raise UsageLimit(f"HTTP {exc.code}: 利用枠・制限応答を検出") from None
            raise EvalError(f"API {method} {path}: HTTP {exc.code}") from None
        if stream:
            return response
        with response:
            return json.load(response)

    def login(self, secret: dict) -> None:
        result = self.request("POST", "/auth/login", {"username": "admin", "password": secret["password"]})
        if result.get("must_change_password"):
            new = secrets.token_urlsafe(30) + "aA7!"
            self.request("POST", "/auth/change-password", {"current_password": secret["password"],
                         "new_password": new, "confirm_password": new})
            secret["password"] = new


def select_questions(manifest: dict, include_unreviewed: bool = False) -> tuple[list[dict], dict]:
    """評価に使う問いと、系統ごとの問数。利用者の確認前の正解（needs_user_review）は既定で除く。
    self 系統は他の系統と別の manifest で評価する（問数は manifest から数える）。"""
    all_questions = manifest.get("questions", [])
    questions = [q for q in all_questions if include_unreviewed or not q.get("needs_user_review")]
    kinds = {q["system"] for q in all_questions}
    if SELF_SYSTEM in kinds and kinds != {SELF_SYSTEM}:
        raise EvalError("self 系統は他の系統と同じ manifest に混ぜず、別の manifest で評価してください")
    if kinds == {SELF_SYSTEM}:
        return questions, {SELF_SYSTEM: sum(q["system"] == SELF_SYSTEM for q in questions)}
    if manifest.get("subset_of"):  # 問いを絞った manifest（利用者が規模を決める）。問数は manifest から数える
        return questions, dict(collections.Counter(q["system"] for q in questions))
    return questions, dict(SYSTEMS)


class Runner:
    def __init__(self, args, manifest: dict, digest: str):
        self.args, self.manifest, self.digest = args, manifest, digest
        self.questions, self.systems = select_questions(manifest, getattr(args, "include_unreviewed", False))
        self.all_questions = self.questions
        self.only_questions = getattr(args, "questions", None)
        if self.only_questions:
            known = {str(q["id"]) for q in self.questions} | {f'{q["system"]}/{q["id"]}' for q in self.questions}
            if set(self.only_questions) - known:
                raise EvalError("--questions に未知の質問があります: " + ", ".join(sorted(set(self.only_questions) - known)))
            self.questions = [q for q in self.questions if str(q["id"]) in self.only_questions
                              or f'{q["system"]}/{q["id"]}' in self.only_questions]
        self.styles = tuple(getattr(args, "styles", None) or STYLES)
        self.rep = getattr(args, "rep", None)
        self.allow_dirty = bool(getattr(args, "allow_dirty", False))
        self.gate_dry_run = bool(getattr(args, "gate_dry_run", False))
        self.model = getattr(args, "model", None) or DEFAULT_MODEL
        self.auth_mode = getattr(args, "auth_mode", None)
        self.codex_home = getattr(args, "codex_home", None)
        self.project = getattr(args, "project", None) or PROJECT
        if self.auth_mode == "apikey" and self.codex_home is None:
            raise EvalError("API キーで回すときは評価専用の --codex-home を指定してください（個人の CODEX_HOME を使わない）")
        self.out = outside_repo(args.out)
        corpus = Path(manifest["corpus_root"])
        if self.out.is_relative_to(corpus) or corpus.is_relative_to(self.out):
            raise EvalError("出力先と登録資料ルートを重ねないでください")
        self.diff_sha256, dirty = None, None
        if self.allow_dirty:
            self.commit = fixed_checkout(ROOT, allow_dirty=True)
            dirty = dirty_state(ROOT)
            self.diff_sha256 = dirty["diff_sha256"]
        else:
            self.commit = fixed_checkout(ROOT)
        self.out.mkdir(parents=True, exist_ok=True)
        self.state = self.out / "runtime"
        self.state.mkdir(exist_ok=True)
        self.credential = self.state / "admin-private.json"
        if not self.credential.exists():
            self.credential.touch(mode=0o600)
            dump(self.credential, {"password": secrets.token_urlsafe(30) + "aA7!"})
        self.credential.chmod(0o600)
        self.secret = load(self.credential)
        self.env = {k: v for k, v in os.environ.items() if k in
                    {"PATH", "HOME", "USER", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "LANG", "CODEX_HOME"}}
        if self.codex_home is not None:
            self.env["CODEX_HOME"] = str(self.codex_home.expanduser().resolve())
        binary = shutil.which("codex", path=self.env.get("PATH"))
        if not binary:
            raise EvalError("Codex CLI がありません")
        self.codex = Path(binary).resolve()
        self.env["PATH"] = str(self.codex.parent) + os.pathsep + self.env.get("PATH", "")
        self.env.update({"PGHOST": "127.0.0.1", "PGPORT": str(args.pg_port), "PGDATABASE": DATABASE,
                         "PGUSER": "sherpa", "PGPASSWORD": "sherpa_dev", "NEO4J_PASSWORD": "sherpa_dev",
                         "NEO4J_URI": f"bolt://127.0.0.1:{args.neo_bolt_port}",
                         "SHERPA_NEO4J_HTTP_PORT": str(args.neo_http_port),
                         "SHERPA_NEO4J_BOLT_PORT": str(args.neo_bolt_port), "SHERPA_ES_PORT": str(args.es_port),
                         "ES_URL": f"http://127.0.0.1:{args.es_port}", "SHERPA_ENV": "development",
                         "SHERPA_USERS_DIR": str(self.state / "users"), "SHERPA_DERIVED_DIR": str(self.state / "derived"),
                         "SHERPA_OBSERVATION_DIR": str(self.state / "observations"),
                         "SHERPA_KB_DIR": str(self.state / "unused-kb"),
                         "RUN_DIR": str(self.state / "run"), "SHERPA_DISABLE_EMBED": "1",
                         "SHERPA_ADMIN_PASSWORD": self.secret["password"], "SHERPA_AGENT": "codex",
                         "SHERPA_BROWSE_ROOTS": str(Path(manifest["corpus_root"]).parent),
                         "PYTHONDONTWRITEBYTECODE": "1"})
        self.compose = ["docker", "compose", "--env-file", "/dev/null", "-f", str(ROOT / "docker-compose.yml"),
                        "-p", self.project]
        self.api = API(args.api_port, args.timeout)
        conditions = {"manifest_sha256": digest, "corpus_sha256": manifest["corpus_sha256"],
                      "model": self.model, "cli": CLI_VERSION, "depth": "standard",
                      "mode": "standard", "schema_level": 2,
                      "cli_path": str(self.codex), "cli_sha256": hashlib.sha256(self.codex.read_bytes()).hexdigest(),
                      "review_caps": {s: getattr(args, "review_cap", None) if getattr(args, "review_cap", None) is not None else 1
                                      for s in STYLES},
                      "es": "BM25 only",
                      "commit": self.commit,
                      "seed": manifest.get("seed"), "project": self.project, "database": DATABASE,
                      "ports": {k: getattr(args, k) for k in ("pg_port", "neo_http_port", "neo_bolt_port", "es_port", "api_port")}}
        if self.allow_dirty:  # 指定しないときの指紋を変えないため、差分を許したときだけ条件に入れる
            conditions["diff_sha256"] = self.diff_sha256
            conditions["changed_files"] = dirty["changed_files"]
        if self.gate_dry_run:  # 指定しないときの指紋を変えないため、指定したときだけ条件に入れる
            conditions["gate_dry_run"] = True
        if self.auth_mode is not None:  # 既存の結果（記録なし）の指紋を変えないため、明示されたときだけ条件に入れる
            conditions["auth_mode"] = self.auth_mode
        fingerprint = hashlib.sha256(json.dumps(conditions, sort_keys=True).encode()).hexdigest()
        self.fingerprint = fingerprint
        meta_path = self.out / "metadata.json"
        self.retry_from = getattr(args, "retry_from", None)
        self.retry_only = frozenset(getattr(args, "only", None) or ()) if self.retry_from else frozenset()
        self.consecutive_infra, self.usage_stop = 0, False
        if self.retry_from and not meta_path.exists():
            raise EvalError("再試行元に metadata.json がありません")
        if meta_path.exists():
            scoring.check_same_version(load(meta_path), {**conditions, "fingerprint": fingerprint})
        else:
            extra = {"untracked_files": dirty["untracked_files"]} if dirty else {}
            dump(meta_path, {**conditions, "fingerprint": fingerprint, "codex_home": str(self.env.get("CODEX_HOME") or ""),
                             "rep": self.rep, **env_identity(self.base_env()), **extra,
                             "question_ids":
                             [f'{q["system"]}/{q["id"]}' for q in self.all_questions],
                             "pilot_reuse": "同一 fingerprint の完答・製品の部分回答の試行を再利用。smoke は別保存。"})

    def base_env(self) -> dict:
        env = dict(self.env)
        if getattr(self.args, "review_cap", None) is not None:
            env["SHERPA_LEDGER_REVIEW_CAP"] = str(self.args.review_cap)
        if getattr(self, "gate_dry_run", False):
            env["SHERPA_LEDGER_GATE_DRYRUN"] = "1"
        return env

    def server_env(self, style: str) -> dict:
        """API サーバに渡す環境変数。"""
        return {**self.base_env(), "SHERPA_LEDGER_STYLE": style, "SHERPA_LEDGER_EVAL_DIR": str(self.out / "records" / style)}

    def command(self, argv: list[str], *, timeout=180) -> str:
        result = subprocess.run(argv, cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise EvalError(f"外部コマンド失敗: {Path(argv[0]).name} (exit {result.returncode})")
        return result.stdout

    def prepare(self) -> None:
        checkout_guard(self)
        if not self.args.allow_schema_bootstrap:
            raise EvalError("専用 DB の初期スキーマ作成を承認後 --allow-schema-bootstrap を指定してください")
        version = self.command([str(self.codex), "--version"])
        if not re.search(r"\b" + re.escape(CLI_VERSION) + r"\b", version):
            raise EvalError("Codex CLI の版が 0.162.1 ではありません")
        if not (Path(self.env.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json").is_file():  # 存在の確認だけ。中身は読まない
            raise EvalError("Codex の認証ファイルがありません（--codex-home の場所で codex login を済ませてください）")
        containers = self.containers()
        own = {p.get("PublishedPort") for c in containers for p in c.get("Publishers") or []}
        for port in [self.args.pg_port, self.args.neo_http_port, self.args.neo_bolt_port, self.args.es_port]:
            if port in own:
                continue
            require_free_port(port)
        owner = self.state / "owner.json"
        if owner.exists() and load(owner).get("out") != str(self.out):
            raise EvalError("専用 project が別の評価出力に紐づいています")
        if containers:
            self.check_database(unowned=not owner.exists())
        self.command([*self.compose, "up", "-d", "--wait", "postgres", "neo4j", "elasticsearch"], timeout=300)
        self.ensure_database()
        self.check_database(unowned=not owner.exists())
        dump(owner, {"project": self.project, "database": DATABASE, "out": str(self.out)})
        dump(self.state / "prepared.json", {"manifest_sha256": self.digest, "schema_bootstrap_approved": True,
                                           "project": self.project, "database": DATABASE, "pg_port": self.args.pg_port})

    def containers(self) -> list[dict]:
        raw = self.command([*self.compose, "ps", "--all", "--format", "json"]).strip()
        if not raw:
            return []
        containers = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines()]
        for c in containers:
            labels = json.loads(self.command(["docker", "inspect", "--format", "{{json .Config.Labels}}", c["ID"]]))
            if labels.get("com.docker.compose.project") != self.project:
                raise EvalError("Docker project の一致を確認できません")
            expected = {"postgres": {self.args.pg_port}, "neo4j": {self.args.neo_http_port, self.args.neo_bolt_port},
                        "elasticsearch": {self.args.es_port}}
            published = {p.get("PublishedPort") for p in c.get("Publishers") or [] if p.get("PublishedPort")}
            if c.get("Service") not in expected or published != expected[c["Service"]]:
                raise EvalError("Docker project の専用ポート構成が一致しません")
        return containers

    def check_database(self, *, unowned=False) -> None:
        if self.env.get("PGDATABASE") != DATABASE or (getattr(self, "project", PROJECT), self.args.pg_port) not in APPROVED_STORES:
            raise EvalError("スキーマ適用は承認対象の専用 DB/ポートに限ります")
        import psycopg
        with psycopg.connect(host="127.0.0.1", port=self.args.pg_port, user="sherpa", password="sherpa_dev",
                             dbname=DATABASE) as conn:
            if conn.execute("SELECT current_database()").fetchone()[0] != DATABASE:
                raise EvalError("接続先 DB が専用 DB と一致しません")
            if unowned:
                for table in ("worlds", "conversations", "messages", "documents"):
                    exists = conn.execute("SELECT to_regclass(%s)", ("public." + table,)).fetchone()[0]
                    if exists and conn.execute(f"SELECT EXISTS (SELECT 1 FROM {table})").fetchone()[0]:
                        raise EvalError("所有記録のない専用 DB に既存データがあるため停止します")

    def ensure_database(self) -> None:
        if (getattr(self, "project", PROJECT), self.args.pg_port) not in APPROVED_STORES or self.env.get("PGDATABASE") != DATABASE:
            raise EvalError("承認対象外の DB への初期化を拒否しました")
        import psycopg
        with psycopg.connect(host="127.0.0.1", port=self.args.pg_port, user="sherpa", password="sherpa_dev",
                             dbname="postgres", autocommit=True) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DATABASE,)).fetchone():
                conn.execute("CREATE DATABASE sherpa_ledger_eval")

    @contextlib.contextmanager
    def server(self, style: str):
        checkout_guard(self)
        prepared = self.state / "prepared.json"
        if not prepared.exists():
            raise EvalError("先に prepare を実行してください")
        approval = load(prepared)
        if (not approval.get("schema_bootstrap_approved") or approval.get("project") != self.project
                or approval.get("database") != DATABASE or approval.get("pg_port") != self.args.pg_port
                or (getattr(self, "project", PROJECT), self.args.pg_port) not in APPROVED_STORES):
            raise EvalError("専用 DB の初期化承認記録が一致しません")
        if len(self.containers()) != 3:
            raise EvalError("専用 Docker project の 3 ストアを確認できません")
        self.check_database()
        require_free_port(self.args.api_port)
        env = self.server_env(style)
        with (self.state / f"api-{style}.log").open("ab") as log:
            proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "sherpa.api:app", "--host", "127.0.0.1",
                                     "--port", str(self.args.api_port), "--workers", "1", "--no-access-log"],
                                    cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 120
                while True:
                    if proc.poll() is not None:
                        raise EvalError("API 起動失敗（runtime ログ参照）")
                    try:
                        if self.api.request("GET", "/healthz", timeout=3).get("ok"):
                            break
                    except (EvalError, urllib.error.URLError):
                        pass
                    if time.monotonic() > deadline:
                        raise EvalError("API 起動待ちが上限を超えました")
                    time.sleep(1)
                self.api.login(self.secret)
                dump(self.credential, self.secret)
                self.credential.chmod(0o600)
                yield
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    def configure(self) -> None:
        self.api.request("PUT", "/admin/settings", {"openai_api_key": None, "ollama_url": None, "cloud_provider": None,
                         "openai_base_url": None, "openai_endpoint_kind": None, "embed_provider": "auto",
                         "codex_mode": "standard", "codex_worker_model": self.model,
                         "model_catalog": {"codex": {"codex": {"allowed": [self.model], "default": self.model}}}})
        self.api.request("PUT", "/settings", {"agent": "codex", "codex_model_provider": "openai"})
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(host="127.0.0.1", port=self.args.pg_port, user="sherpa", password="sherpa_dev",
                             dbname=DATABASE, row_factory=dict_row) as conn:
            rows = conn.execute("SELECT key, value FROM system_settings WHERE key IN "
                                "('openai_api_key','ollama_url','embed_provider','model_catalog')").fetchall()
        values = {r["key"]: r["value"] for r in rows}
        if values.get("openai_api_key") or values.get("ollama_url") or values.get("embed_provider") not in (None, "auto"):
            raise EvalError("DB の埋め込み接続先が未設定であることを確認できません")
        dump(self.out / "bm25-verification.json", {"disable_embed": True, "endpoint_unset_db_verified": True,
                                                   "model": self.model, "es": "BM25 only"})

    def ingest(self) -> None:
        worlds = self.api.request("GET", "/worlds")
        existing = worlds.get("worlds", []) if isinstance(worlds, dict) else worlds
        if existing:
            if len(existing) != 1 or existing[0].get("root_path") != self.manifest["corpus_root"]:
                raise EvalError("登録済み資料フォルダが manifest と違います")
            self.world = existing[0].get("world_id")
            self.check_ingested()
            return
        accepted = self.api.request("POST", "/worlds", {"world_id": "ledger-eval", "label": "評価用資料",
                                    "path": self.manifest["corpus_root"]})
        self.world = accepted["world_id"]
        deadline = time.monotonic() + self.args.ingest_timeout
        while time.monotonic() < deadline:
            runs = self.api.request("GET", f"/ingest/runs?world={self.world}")["runs"]
            row = next((r for r in runs if r["id"] == accepted["run_id"]), None)
            if row and row.get("status") != "extracting":
                dump(self.out / "ingest.json", row)
                if row["status"] not in ("auto_published", "auto_published_with_flags"):
                    raise EvalError("資料の取り込みが失敗しました（ingest.json 参照）")
                self.check_ingested()
                return
            time.sleep(2)
        raise EvalError("取り込み待ちが上限を超えました")

    def check_ingested(self) -> None:
        status = self.api.request("GET", f"/worlds/{self.world}/status")
        dump(self.out / "world-status.json", status)
        if (status.get("last_run_status") not in ("auto_published", "auto_published_with_flags")
                or status.get("es_state") != "ok" or not status.get("es_chunks")):
            raise EvalError("資料フォルダの取り込み・ES 索引の完了を確認できません")

    def trial(self, style: str, q: dict, phase: str, after_stall: bool = False) -> dict | None:
        """1 質問を実行する。既存の有効な試行は再利用し、再試行は retries/<番号>/ に別の試行として残す。
        無応答で止めた試行は同じ指紋のまま 1 回だけ自動で再試行する（after_stall はその 2 回目）。"""
        checkout_guard(self)
        base = self.out / ("smoke" if phase == "smoke" else "trials") / style / q["system"] / str(q["id"])
        attempts = scoring.load_attempts(base, self.out, codex_log=self.state / "run" / "codex.log")
        if attempts:
            chosen = scoring.effective_attempt(attempts)
            state = chosen["classification"]
            if state["class"] in scoring.COUNTED:
                if chosen["result"].get("fingerprint") != self.fingerprint:
                    raise EvalError("版の指紋が違う試行は混ぜられません。別の出力フォルダを指定してください")
                return {**chosen["result"], "classification": state}
            if not self.retry_from and not after_stall:
                raise EvalError(f'{style}/{q["system"]}/{q["id"]} は {scoring.LABELS[state["class"]]}'
                                f'（{state["reason"]}）で終わっています。retry で --retry-from <出力フォルダ> --only <分類> '
                                "を指定すると別の試行番号で再実行します")
            if self.retry_from and not after_stall and state["class"] not in self.retry_only:
                return None
            number = max(a["n"] for a in attempts) + 1
            phase = chosen["result"].get("phase", phase)
            target = base / "retries" / str(number)
        elif self.retry_from:
            return None
        else:
            number, target = 1, base
        target.mkdir(parents=True, exist_ok=True)
        result_path = target / "result.json"
        started = time.monotonic()
        started_at = time.time()
        api_log = self.state / f"api-{style}.log"
        log_offset = api_log.stat().st_size if api_log.exists() else 0
        result = {"style": style, "system": q["system"], "id": q["id"], "hop": q["hop"],
                  "type": q.get("type"), "phase": phase, "attempt_no": number,
                  "fingerprint": self.fingerprint, "status": "running", "rep": getattr(self, "rep", None),
                  "run_seq": scoring.next_run_seq(self.out), "started_at": started_at}
        dump(result_path, result)
        try:
            turn = self.api.request("POST", "/chat/turns", {"message": q["question"], "world": self.world,
                                    "scope_paths": q["scope_paths"], "depth_profile": "standard",
                                    "lens": "investigate", "personal": False, "knowledge": True, "web_search": False})
            result.update(turn)
            dump(result_path, result)
            events = []
            stall_timeout = getattr(self.args, "stall_timeout", 600)
            with self.api.request("GET", f'/chat/turns/{turn["turn_id"]}/stream', stream=True,
                                  timeout=stall_timeout) as stream:
                with (target / "events.jsonl").open("w", encoding="utf-8") as out:
                    for event in stream_events(stream, out, started, self.args.timeout, stall_timeout):
                        events.append(event)
                        if event.get("type") in ("error", "warning") and LIMIT_RE.search(json.dumps(event, ensure_ascii=False)):
                            result["limit_evidence"] = "error_event"
                            raise UsageLimit("イベントで利用枠・制限応答を検出")
            conv = self.api.request("GET", f'/conversations/{turn["conversation_id"]}')
            dump(target / "conversation.json", conv)
            assistants = [m for m in conv.get("messages", []) if m.get("role") == "assistant"]
            answer = assistants[-1].get("answer") if assistants else None
            if not isinstance(answer, dict):
                raise EvalError("保存済み回答がありません")
            dump(target / "answer.json", answer)
            dump(target / "activity.json", answer.get("activity"))
            body = answer.get("body", answer.get("headline", "")) or ""
            with api_log.open("rb") as log:
                log.seek(log_offset)
                log_limit = LIMIT_RE.search(log.read().decode("utf-8", errors="replace"))
            answer_failed = answer.get("completion") in ("failed", "stopped") or answer.get("agentic_failure")
            if log_limit or (answer_failed and LIMIT_RE.search(body)):
                result["limit_evidence"] = "runtime_error_log" if log_limit else "failed_answer"
                raise UsageLimit("回答・イベントで利用枠・制限応答を検出")
            stem = f'conv-{turn["conversation_id"]}'
            records = self.out / "records" / style
            stats_path = records / (stem + ".stats.json")
            stats = load(stats_path) if stats_path.exists() else None
            if stats is not None:
                dump(target / "stats.json", stats)
            plan_path = records / (stem + ".plan.md")
            if plan_path.exists():
                (target / "plan.md").write_bytes(plan_path.read_bytes())
            for review in records.glob(stem + ".review-*.md"):
                (target / review.name[len(stem) + 1:]).write_bytes(review.read_bytes())
            for name in ("adopted", "composed"):
                if (records / f"{stem}.{name}.md").is_file():
                    (target / f"{name}.md").write_bytes((records / f"{stem}.{name}.md").read_bytes())
            session_dir = self.state / "users" / "admin" / "workspace" / ".codex-sessions" / str(turn["conversation_id"])
            cli = scoring.read_cli_terminal(session_dir)
            cli_exit = scoring.read_codex_log(self.state / "run" / "codex.log", turn["conversation_id"])
            result["status"] = "ok"
            classification = scoring.classify_trial(result, answer, cli, cli_exit)
            dump(target / "classification.json", {**classification, "cli_terminal": cli, "cli_exit": cli_exit})
            if classification["reason"] == "cli_usage_limit":
                result["limit_evidence"] = "cli_terminal_error"
                raise UsageLimit("CLI 終端で利用枠・制限を検出")
            result["observed_style"] = stats.get("style") if isinstance(stats, dict) else None
            if classification["class"] in scoring.COUNTED:
                if not body.strip():
                    raise EvalError("空回答を完答・部分回答として扱えません")
                validate_run_conditions(answer, stats, style, self.model)
            timeline = None
            if list(session_dir.glob("sessions/**/*.jsonl")):
                timeline = json.loads(self.command([sys.executable, str(ROOT / "scripts/turn_timeline.py"),
                                                   str(session_dir), "--json"]))
                dump(target / "timeline.json", timeline)
            checkout_guard(self)
            # 基盤障害・停止の試行の Recall/Precision は参考値で、集計は classification を見て除外する。
            result.update(score_answer(answer, q, self.manifest["corpus_files"]), **measure(answer, stats, timeline))
            result.update(extra_metrics(target, result, self.manifest["corpus_files"]))
            result["classification"] = classification
            result["end_state"] = scoring.end_state(classification, answer)
            result["status"] = "ok" if classification["class"] in scoring.COUNTED else classification["class"]
            self.usage_stop = self.usage_stop or scoring.reached(cli["rate_limits"])
        except UsageLimit:
            result["status"] = "usage_limit"
            result.setdefault("limit_evidence", "api_error")
            result["classification"] = scoring.classify_trial(result, None)
            result["end_state"] = scoring.end_state(result["classification"], None)
            self.stop_trial(result)
            raise
        except StallTimeout:
            result.update(status="failed", error_type="StallTimeout", stalled=True,
                          stall_seconds=getattr(self.args, "stall_timeout", 600))
            result["classification"] = scoring.classify_trial(result, None)
            result["end_state"] = scoring.end_state(result["classification"], None)
            self.stop_trial(result)
        except Exception as exc:
            result.update(status="failed", error_type=type(exc).__name__)
            result["classification"] = scoring.classify_trial(result, None)
            result["end_state"] = scoring.end_state(result["classification"], None)
            self.stop_trial(result)
            raise
        except BaseException:
            result["status"] = "interrupted"
            result["classification"] = scoring.classify_trial(result, None)
            result["end_state"] = scoring.end_state(result["classification"], None)
            self.stop_trial(result)
            raise
        finally:
            result["wall_seconds"] = time.monotonic() - started
            result["ended_at"] = time.time()
            result.update(scoring.rate_metrics(result))
            result["stall_count"] = sum(1 for a in attempts if a["result"].get("stalled")) + bool(result.get("stalled"))
            dump(result_path, result)
        if result.get("stalled") and not after_stall:
            return self.trial(style, q, phase, after_stall=True)
        if result["classification"]["class"] in (scoring.INFRASTRUCTURE, scoring.UNVERIFIED):
            self.consecutive_infra += 1
            if self.consecutive_infra >= self.args.max_infra_streak:
                raise InfrastructureStop(f"基盤障害・原因未確定が {self.consecutive_infra} 件続いたため停止します。"
                                         "原因を確認後、retry で再実行してください")
        else:
            self.consecutive_infra = 0
        return result

    def stop_trial(self, result: dict) -> None:
        turn_id = result.get("turn_id")
        if turn_id is None:
            return
        try:
            stopped = self.api.request("POST", f"/chat/turns/{turn_id}/stop", timeout=5)
            result["stop_requested"] = bool(stopped.get("ok"))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                running = self.api.request("GET", "/chat/turns/running", timeout=3).get("turns", [])
                if not any(t.get("turn_id") == turn_id for t in running):
                    result["stop_drained"] = True
                    return
                time.sleep(.5)
            result["stop_drained"] = False
        except Exception:
            result["stop_requested"] = None

    def execute(self, phase: str) -> None:
        scoring.usage_gate(self.out, clear=getattr(self.args, "clear_usage_stop", False))
        if phase == "retry":
            self.execute_retry()
            return
        per_system = 1 if phase == "smoke" else 2 if phase == "pilot" else None
        if phase == "run":
            counts = collections.Counter(q["system"] for q in self.all_questions)
            if dict(counts) != self.systems or not all(self.systems.values()):
                raise EvalError("本番は code10 / multihop15 / jemhop10 問（self は確認済みの問い）が必要です")
            # 問いを絞った manifest は利用者が規模を決めているので、pilot の見込みを前提にしない
            if not self.manifest.get("subset_of") and not (self.out / "pilot-estimate.json").exists():
                raise EvalError("先に pilot を完了し、報告書冒頭へ所要時間見込みを記載してください")
            if not self.args.estimate_recorded:
                raise EvalError("報告書冒頭への時間見込み記載後 --estimate-recorded を指定してください")
        selected = []
        for system in self.systems:
            questions = [q for q in self.questions if q["system"] == system]
            if self.only_questions:
                selected.extend(questions)
                continue
            if per_system and len(questions) < per_system and not self.manifest.get("subset_of"):
                raise EvalError("smoke/pilot の系統ごとの必要問数が足りません")
            selected.extend(questions[:per_system] if per_system else questions)
        try:
            for style in self.styles:
                with self.server(style):
                    self.configure()
                    self.ingest()
                    for q in selected:
                        self.trial(style, q, phase)
                        self.summarize()
                        print(f'{phase}: {style}/{q["system"]}/{q["id"]} 保存済み', flush=True)
                        self.stop_if_usage_reached(phase)
            if phase == "pilot":
                rows = self.rows()
                by_group = {(s, y): [r["wall_seconds"] for r in rows
                                    if r["style"] == s and r["system"] == y and r["counted"]]
                            for s in STYLES for y in self.systems}
                estimate = sum(statistics.mean(times) * self.systems[y] for (s, y), times in by_group.items() if times)
                dump(self.out / "pilot-estimate.json", {"fingerprint": self.fingerprint,
                     "pilot_trials": sum(map(len, by_group.values())), "total_trials": len(STYLES) * sum(self.systems.values()),
                     "estimated_total_seconds": estimate, "pilot_reused": True,
                     "estimated_remaining_seconds": max(0, estimate - sum(sum(v) for v in by_group.values())),
                     "group_mean_seconds": {f"{s}/{y}": statistics.mean(t) for (s, y), t in by_group.items() if t},
                     "tokens": pilot_tokens(rows, self.systems)})
        except UsageLimit:
            self.record_usage_stop(phase)
            raise
        finally:
            self.summarize()

    def stop_if_usage_reached(self, phase: str) -> None:
        if self.usage_stop:
            raise UsageLimit("利用枠の到達フラグを検出しました。新規試行を止めます")

    def record_usage_stop(self, phase: str) -> None:
        reset = ((scoring.latest_rate_limits(self.out) or {}).get("primary") or {}).get("resets_at")
        dump(self.out / "stopped.json", {"reason": "usage_limit", "phase": phase, "resets_at": reset})

    def execute_retry(self) -> None:
        """基盤障害・停止の試行だけを、元の記録を残したまま別の試行番号で再実行する。"""
        rows = self.rows()
        wanted = set(scoring.select_retries(rows, self.retry_only))
        questions = {(q["system"], str(q["id"])): q for q in self.questions}
        phases = {(r["style"], r["system"], str(r["id"])): r.get("phase", "pilot") for r in rows}
        plan = [(style, questions[(system, qid)], phases[(style, system, qid)])
                for style in STYLES for (s, system, qid) in sorted(wanted) if s == style and (system, qid) in questions]
        print(f"再試行の対象: {len(plan)} 件（{', '.join(sorted(self.retry_only))}）", flush=True)
        try:
            for style in self.styles:
                todo = [(q, phase) for s, q, phase in plan if s == style]
                if not todo:
                    continue
                with self.server(style):
                    self.configure()
                    self.ingest()
                    for q, phase in todo:
                        result = self.trial(style, q, phase)
                        self.summarize()
                        if result is not None:
                            label = scoring.LABELS[result["classification"]["class"]]
                            print(f'retry: {style}/{q["system"]}/{q["id"]} 保存済み（{label}）', flush=True)
                        self.stop_if_usage_reached("retry")
        except UsageLimit:
            self.record_usage_stop("retry")
            raise
        finally:
            self.summarize()

    def rows(self) -> list[dict]:
        files = self.manifest["corpus_files"]
        return scoring.collect(self.out, codex_log=self.state / "run" / "codex.log",
                               enrich=lambda target, result: extra_metrics(target, result, files))

    def summarize(self) -> None:
        rows = self.rows()
        with (self.out / "scores.jsonl").open("w", encoding="utf-8") as out:
            for row in rows:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
        (self.out / "summary.md").write_text(scoring.summary_markdown(rows, tuple(self.systems), self.model), encoding="utf-8")
        (self.out / "behavior.md").write_text(scoring.behavior_markdown(rows, tuple(self.systems)), encoding="utf-8")
        other = getattr(self.args, "compare_with", None)
        if other is not None:
            other = other.expanduser().resolve()
            (self.out / "compare.md").write_text(scoring.compare_markdown(
                rows, scoring.collect(other, codex_log=other / "runtime/run/codex.log"), self.out.name, other.name),
                encoding="utf-8")


def pilot_tokens(rows: list[dict], systems) -> dict:
    """予備計測の試行ごとのトークン（新規入力・キャッシュ入力・出力）と、構成×系統ごとの合計・全体の見込み（費用の見込み用）。
    欠測の試行は合計に入れず、欠測数を残す。"""
    keys = ("new_input_tokens", "cached_input_tokens", "output_tokens")
    per_trial, groups = [], {}
    for r in rows:
        if not r.get("counted"):
            continue
        vals = {k: r.get(k) for k in keys}
        per_trial.append({"style": r["style"], "system": r["system"], "id": r["id"], **vals})
        g = groups.setdefault(f'{r["style"]}/{r["system"]}', {"trials": 0, "missing": 0, **{k: 0 for k in keys}})
        g["trials"] += 1
        if all(isinstance(v, int) for v in vals.values()):
            for k in keys:
                g[k] += vals[k]
        else:
            g["missing"] += 1
    total = {k: sum(g[k] for g in groups.values()) for k in keys}
    estimate = {k: 0.0 for k in keys}
    for name, g in groups.items():
        ok = g["trials"] - g["missing"]
        system = name.split("/", 1)[1]
        if ok and system in systems:
            for k in keys:
                estimate[k] += g[k] / ok * systems[system]
    return {"per_trial": per_trial, "by_group": groups, "pilot_total": total, "estimated_total": estimate,
            "note": "新規入力=入力合計-キャッシュ入力。estimated_total は構成×系統の1試行平均×問数の合計。"}


def only_classes(value: str) -> tuple[str, ...]:
    names = tuple(v.strip() for v in value.split(",") if v.strip())
    allowed = tuple(c for c in scoring.CLASSES if c != scoring.COMPLETE)
    if not names or any(n not in allowed for n in names):
        raise argparse.ArgumentTypeError("--only は " + ", ".join(allowed) + " のコンマ区切りです")
    return names


def id_list(value: str) -> tuple[str, ...]:
    names = tuple(v.strip() for v in value.split(",") if v.strip())
    if not names or len(set(names)) != len(names):
        raise argparse.ArgumentTypeError("--questions は重複のない id のコンマ区切りです")
    return names


def style_list(value: str) -> tuple[str, ...]:
    names = tuple(v.strip() for v in value.split(",") if v.strip())
    if not names or len(set(names)) != len(names) or any(n not in STYLES for n in names):
        raise argparse.ArgumentTypeError("--styles は " + ", ".join(STYLES) + " の重複のないコンマ区切りです")
    return names


def run_check(path: Path) -> int:
    """check: manifest を読み取りだけで点検して一覧を出す。指摘があれば終了コード 2。"""
    try:
        findings = check_manifest(json.loads(path.expanduser().read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        print(f"評価停止: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for f in findings:
        print(f'{"NG" if f["error"] else "OK"}  {f["question"]}' + (f'  {f["error"]}' if f["error"] else ""))
    bad = sum(bool(f["error"]) for f in findings)
    print(f"点検: {len(findings)} 問中 {bad} 問に指摘")
    return 2 if bad or not findings else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("check", "prepare", "smoke", "pilot", "run", "retry", "summarize", "all"))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--allow-schema-bootstrap", action="store_true")
    parser.add_argument("--estimate-recorded", action="store_true")
    parser.add_argument("--retry-from", type=Path,
                        help="retry: 再試行元の出力フォルダ（版の指紋が一致するものだけ）。記録はここへ追記する")
    parser.add_argument("--only", type=only_classes,
                        help="retry: 再試行する分類のコンマ区切り（既定 infrastructure,stopped）")
    parser.add_argument("--clear-usage-stop", action="store_true",
                        help="利用枠で止めた記録を別名へ退避して解除する（リセット確認後に利用者が指定）")
    parser.add_argument("--max-infra-streak", type=int, default=2,
                        help="基盤障害・原因未確定がこの件数続いたら停止（既定 2）")
    parser.add_argument("--review-cap", type=int, choices=range(0, 7), default=None,
                        help="見直しの上限を 3 構成とも この回数に上書きする（既定は深さ標準の 1 回）")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Codex（本体・下調べ役）のモデル。API キーで回すときは gpt-5.5 など。実行時の実モデルもこの値で検査する")
    parser.add_argument("--codex-home", type=Path, default=None,
                        help="評価用の CODEX_HOME（既定は環境変数 CODEX_HOME か ~/.codex）。認証ファイルは存在確認だけで中身は読まない")
    parser.add_argument("--auth-mode", choices=AUTH_MODES, default=None,
                        help="認証の種類の明示（metadata に記録するだけ。認証ファイルの中身は見ない）")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="追跡対象の未コミット変更を許す。HEAD と git diff HEAD の sha256・変更ファイルを条件に記録し、各段で同一を確かめる")
    parser.add_argument("--gate-dry-run", action="store_true",
                        help="B（plan_gate）の判定を記録だけして差し戻さない（SHERPA_LEDGER_GATE_DRYRUN=1 を API に渡す）。条件と指紋に入れる")
    parser.add_argument("--questions", type=id_list, default=None,
                        help="この起動で回す質問の id のコンマ区切り（system/id も可）。指紋には入れず、残りは同じ出力フォルダで後から回せる")
    parser.add_argument("--styles", type=style_list, default=None,
                        help="回す構成と順番のコンマ区切り（例 json,plan_gate,plan_main。既定はこの順）")
    parser.add_argument("--rep", type=int, default=None, help="反復番号。metadata と各 result.json に記録する")
    parser.add_argument("--include-unreviewed", action="store_true",
                        help="needs_user_review の付いた問い（利用者の確認前の正解）も評価に含める")
    parser.add_argument("--project", default=PROJECT,
                        help="専用ストアの Compose project 名。初期化は APPROVED_STORES の組（project・pg-port）だけ")
    parser.add_argument("--pg-port", type=int, default=15433)
    parser.add_argument("--neo-http-port", type=int, default=17475)
    parser.add_argument("--neo-bolt-port", type=int, default=17688)
    parser.add_argument("--es-port", type=int, default=19201)
    parser.add_argument("--api-port", type=int, default=18072)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--stall-timeout", type=float, default=600,
                        help="ターンの進行（SSE のイベント）がこの秒数途絶えたら止めて基盤障害にし、同じ指紋で 1 回だけ自動再試行する")
    parser.add_argument("--compare-with", type=Path, default=None,
                        help="summarize: 同じ質問・構成を別の出力フォルダで回した結果と並べた compare.md を書く")
    parser.add_argument("--ingest-timeout", type=float, default=1800)
    args = parser.parse_args(argv)
    ports = [args.pg_port, args.neo_http_port, args.neo_bolt_port, args.es_port, args.api_port]
    if len(set(ports)) != len(ports) or any(not 1024 <= p <= 65535 for p in ports):
        parser.error("専用ポートは重複のない 1024〜65535 です")
    if args.phase == "check":
        return run_check(args.manifest)
    if args.phase == "retry":
        if args.retry_from is None:
            parser.error("retry には --retry-from が必要です")
        if args.out is not None and args.out.expanduser().resolve() != args.retry_from.expanduser().resolve():
            parser.error("再試行は元の出力フォルダへ別の試行番号で記録します。--out は省略するか --retry-from と同じにしてください")
        args.out = args.retry_from
        args.only = args.only or scoring.DEFAULT_RETRY
    else:
        if args.compare_with is not None and args.phase != "summarize":
            parser.error("--compare-with は summarize でだけ使えます")
        if args.retry_from is not None or args.only is not None:
            parser.error("--retry-from / --only は retry でだけ使えます")
        if args.out is None:
            parser.error("--out が必要です")
    try:
        manifest, digest = read_manifest(args.manifest)
        runner = Runner(args, manifest, digest)
        if args.phase in ("prepare", "all"):
            runner.prepare()
        elif (args.phase in ("smoke", "pilot", "run") and args.allow_schema_bootstrap
              and not (runner.state / "prepared.json").exists()):
            runner.prepare()
        if args.phase == "all":
            for phase in ("smoke", "pilot"):
                runner.execute(phase)
            print("pilot 完了。pilot-estimate.json の見込みを報告書冒頭に記載後 run を実行してください。")
        elif args.phase in ("smoke", "pilot", "run", "retry"):
            runner.execute(args.phase)
        elif args.phase == "summarize":
            runner.summarize()
        return 0
    except UsageLimit as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except InfrastructureStop as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except (EvalError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"評価停止: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
