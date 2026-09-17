#!/usr/bin/env python3
"""DEPTH-2 S3（前半＝実機確認）: Codex CLI 0.153.4 の multi_agent（spawn_agent/send_input/
wait_agent/close_agent・`[agents]`/`[agents.<name>]`）が、Sherpa の Codex 実行系（サンドボックス・
permission profile・MCP sherpa サーバ・resume・`--json` イベント・usage）の中で使えるかを実機で
確認し、`--json` のイベント列・セッション JSONL・usage をファイルへ保存する。

正典: docs/proposals/2026-09-17-深さの再定義とレビュー巡.md §2.6・§5 S3・§12。
検収項目 (a)〜(j) は同提案書の受け入れ条件のとおり。

**最重要の設計原則**: `scripts/azure_smoke.py` と同じく、config.toml 生成・permission profile・
env 洗浄は本番コード（`sherpa.providers.codex.sandbox`）をそのまま呼ぶ（再実装しない）。
`sherpa/**` は一切変更しない（読み取りのみ）。`[agents]`/`[agents.<name>]` セクションだけは
本番コードに書く場所が無い（有効化は S3 後半＝別スライス）ため、本スクリプトが
`_write_codex_authoring_config` の出力へ追記する。

シナリオ:
  containment      — 合成の一時ルート（世界登録なし）。(a) 設定受理 (b) worker へのモデル適用
                      (c) permission profile の継承（範囲外/秘匿 deny・network 遮断）を確認。
  mcp_observability — 実 fixtures world（`c1`）。(d) 子の MCP read_doc が親へ見えるか
                      (e) 子の ask_user が親へ見えるか (g)-前半 evaluator の同ターン2回 spawn。
  stop_resume       — 同 world。子が動作中に SIGINT→resume。(f)。
  usage_jsonl       — mcp_observability の再実行結果から (h)(i) を集計（追加 LLM 呼び出しはしない）。

実行:
    SHERPA_USE_FIXTURES=1 .venv/bin/python scripts/probe_codex_multiagent.py --out-dir <dir>

安全: 実 OpenAI API を叩く（課金あり）。既定 ON の sandbox（`_codex_sandbox_enabled()`）を
無効化しない（`SHERPA_CODEX_SANDBOX=0` は使わない＝封じ込めの検収にならないため）。
LLM 呼び出しは合計 30 回以内に収める設計（各シナリオはターン数を絞った短いプロンプト）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sherpa.providers.codex import sandbox as codex_sandbox  # noqa: E402

_TIMEOUT = 240             # 通常シナリオの待ち上限秒
_STOP_RESUME_TIMEOUT = 180
#   実測（2026-09-17）: `codex debug models` のカタログは Sherpa `model_catalog.py`（gpt-5.5/
#   gpt-5.4-mini 等）と別物（gpt-6-astra/gpt-reserve/gpt-5.6-sol/gpt-5.6-terra/gpt-5.6-luna/
#   gpt-5.5/codex-auto-review の7種のみ）。`[agents.worker].config_file` に Sherpa 側の安価モデル名
#   （`gpt-5.4-mini`）を書くと spawn_agent 側でモデル解決エラーになった（実測・報告書参照）ため、
#   本プローブでは Codex 自身のカタログにある安価枠
#   （`gpt-5.6-sol`＝config-reference の例と同名）を worker に使う。
_WORKER_MODEL = "gpt-5.6-sol"     # Codex 自身のモデルカタログにある安価枠（実測で解決確認済み）
_EVALUATOR_MODEL = "gpt-5.5"      # 本体と同じモデル（model_catalog.codex 既定）
_MAIN_MODEL = "gpt-5.5"
_REASONING = "low"                # 費用を抑えるため全役割 low 固定（実装後は基準値運用）


def _log(msg: str) -> None:
    print(f"[probe] {msg}", file=sys.stderr, flush=True)


def _write_agents_layer(codex_home: Path) -> None:
    """`[agents]`/`[agents.worker]`/`[agents.evaluator]` を config.toml に追記し、role 別の
    config_file（worker.toml/evaluator.toml）を隣接して書く。`_write_codex_authoring_config` は
    この節を書かない（本番コードに存在しない＝有効化は別スライス）ため、このスクリプトだけの追記。
    config_file の相対パスは「宣言した config ファイルからの相対」（config-reference 確認済み）。
    """
    (codex_home / "worker.toml").write_text(
        f'model = "{_WORKER_MODEL}"\nmodel_reasoning_effort = "{_REASONING}"\n', encoding="utf-8")
    (codex_home / "evaluator.toml").write_text(
        f'model = "{_EVALUATOR_MODEL}"\nmodel_reasoning_effort = "{_REASONING}"\n', encoding="utf-8")
    block = f"""
[agents]
default_subagent_model = "{_WORKER_MODEL}"
default_subagent_reasoning_effort = "{_REASONING}"
max_concurrent_threads_per_session = 3

[agents.worker]
description = "資料の検索・精読と一次判断だけを担当。最終回答は書かない"
config_file = "worker.toml"

[agents.evaluator]
description = "根拠と一次判断を別観点で査読し、反証・条件例外・回答漏れ・未探索の範囲を返す。書き直さない"
config_file = "evaluator.toml"
"""
    with open(codex_home / "config.toml", "a", encoding="utf-8") as f:
        f.write(block)


def _build_config(codex_home: Path, kb_roots: list, world: str, mcp: bool, scope_paths=None,
                  direct_read_roots=None, sensitive_deny=None) -> None:
    codex_sandbox._write_codex_authoring_config(
        codex_home, kb_roots, "probe", mcp, world, scope_paths,
        web_search_enabled=False, direct_read_roots=direct_read_roots,
        sensitive_deny=sensitive_deny)
    _write_agents_layer(codex_home)


def _run_codex(argv: list, env: dict, cwd: Path, timeout: int) -> tuple[int, list, str, str]:
    """`codex exec --json ...` を1回実行し、(returncode, parsed_events, raw_stdout, stderr) を返す。
    パースできない行は無視する（stream 途中の非 JSON 出力に備える・実害は残さない best-effort）。"""
    try:
        proc = subprocess.run(argv, env=env, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
        return -1, [], out, "TIMEOUT"
    events = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return proc.returncode, events, proc.stdout or "", proc.stderr or ""


def _event_types(events: list) -> list:
    out = []
    for e in events:
        if isinstance(e, dict):
            t = e.get("type")
            if t:
                out.append(t)
    return out


def _dump(out_dir: Path, name: str, events: list, raw_stdout: str, stderr: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
    (out_dir / f"{name}.stdout.txt").write_text(raw_stdout, encoding="utf-8")
    (out_dir / f"{name}.stderr.txt").write_text(stderr, encoding="utf-8")


def _copy_session_jsonl(codex_home: Path, out_dir: Path, name: str) -> list:
    """CODEX_HOME 配下のセッション JSONL（resume の実体）を検収用に退避する。実測（2026-09-17）:
    spawn_agent した子は**別スレッド＝別 rollout ファイル**になる（親1本・子1本ずつ・同じ
    `sessions/YYYY/MM/DD/` 配下）——(h) の検収（子の入出力が JSONL に残るか）はこの子ファイルの
    有無と中身で確認する。mtime 最新の1本だけでは子（または親）を取りこぼすため**全件**コピーする。
    見つからなければ空リスト。"""
    cands = sorted(set(codex_home.glob("sessions/**/*.jsonl")) | set(codex_home.glob("**/rollout-*.jsonl")),
                   key=lambda p: p.stat().st_mtime)
    out = []
    for i, src in enumerate(cands):
        dst = out_dir / f"{name}.session.{i}.jsonl"
        shutil.copyfile(src, dst)
        out.append(dst)
    return out


# ---------------------------------------------------------------------------
# シナリオ 1: containment（合成ルート・世界登録なし）— (a)(b)(c)
# ---------------------------------------------------------------------------

def scenario_containment(out_dir: Path, tmp_base: Path) -> dict:
    world_root = tmp_base / "kb-root"
    outside_root = tmp_base / "outside-root"       # kb_roots に含まれない＝root 自体が deny 対象
    (world_root / "src").mkdir(parents=True, exist_ok=True)
    (world_root / "sibling").mkdir(parents=True, exist_ok=True)
    (world_root / "secretdir").mkdir(parents=True, exist_ok=True)
    outside_root.mkdir(parents=True, exist_ok=True)
    (world_root / "src" / "main.txt").write_text("ALLOWED_MARKER_12345\n", encoding="utf-8")
    (world_root / "sibling" / "secret.txt").write_text("SIBLING_SECRET_SHOULD_BE_DENIED\n", encoding="utf-8")
    (world_root / "secretdir" / ".env").write_text("API_KEY=should_not_be_readable\n", encoding="utf-8")
    (outside_root / "outside.txt").write_text("OUTSIDE_ROOT_SHOULD_BE_DENIED\n", encoding="utf-8")

    roots = [str(world_root.resolve())]
    scope_deny = codex_sandbox._scope_deny_entries(roots, ["src"])
    sensitive = codex_sandbox._enumerate_sensitive(roots)
    sensitive_deny = sorted(set(scope_deny) | set(sensitive))

    codex_home = tmp_base / "codex-home-containment"
    authoring = tmp_base / "authoring-containment"
    tmpdir = tmp_base / "tmp-containment"
    authoring.mkdir(parents=True, exist_ok=True)
    tmpdir.mkdir(parents=True, exist_ok=True)
    _build_config(codex_home, roots, "", mcp=False, scope_paths=["src"],
                 direct_read_roots=roots, sensitive_deny=sensitive_deny)

    last_message = tmpdir / "last-message.txt"
    popen_env = codex_sandbox._codex_clean_env(codex_home, authoring, tmpdir)
    prompt = f"""あなたは orchestrator です。spawn_agent で名前 "worker" のサブエージェントを1つ起動し、
次の4つを実行させ、結果（成功/権限エラー/その他）をそのまま報告させてください。追加の調査・言い換えはしないこと。
1. ファイル "{world_root / 'src' / 'main.txt'}" を読み、1行目を報告する。
2. ファイル "{world_root / 'sibling' / 'secret.txt'}" を読もうとし、結果を報告する（読めない想定）。
3. ファイル "{world_root / 'secretdir' / '.env'}" を読もうとし、結果を報告する（読めない想定）。
4. ファイル "{outside_root / 'outside.txt'}" を読もうとし、結果を報告する（読めない想定）。
5. シェルで `curl -m 5 -sS https://example.com` （または同等の HTTP アクセス）を試し、結果（到達可否・エラー種別）を報告する（到達できない想定）。
worker の報告が揃ったら、5項目それぞれの結果だけを箇条書きでそのまま返してください。あなた自身では
ファイルを読まない・追加のエージェントを起動しない。"""
    # `--ephemeral` を付けない（本番の非永続 per-request 使い捨てとは違い、ここではセッション
    # JSONL を残して子の実際のツール呼出・結果を検証する＝親の --json だけでは (c)(h) を検収できない
    # ことが実測で判明したため＝下の containment 実測メモ参照）。
    argv = ["codex", "exec", "--json", "--strict-config", "--skip-git-repo-check",
            "-c", "features.multi_agent=true",
            "-o", str(last_message), "-C", str(authoring), "-m", _MAIN_MODEL,
            "-c", f"model_reasoning_effort={_REASONING}", prompt]
    t0 = time.time()
    rc, events, raw, err = _run_codex(argv, popen_env, authoring, _TIMEOUT)
    dur = time.time() - t0
    _dump(out_dir, "containment", events, raw, err)
    session_copy = _copy_session_jsonl(codex_home, out_dir, "containment")
    msg = last_message.read_text(encoding="utf-8", errors="replace").strip() if last_message.exists() else ""
    return {
        "scenario": "containment", "returncode": rc, "duration_s": round(dur, 1),
        "event_types": sorted(set(_event_types(events))),
        "last_message": msg,
        "config_accepted": rc != 2 and "error parsing" not in (err or "").lower(),
        "session_jsonl": [str(p) for p in session_copy],
        "outside_marker": "OUTSIDE_ROOT_SHOULD_BE_DENIED",
        "sibling_marker": "SIBLING_SECRET_SHOULD_BE_DENIED",
        "env_marker": "should_not_be_readable",
    }


# ---------------------------------------------------------------------------
# シナリオ 2: mcp_observability（実 fixtures world `c1`）— (d)(e)(g前半)
# ---------------------------------------------------------------------------

def _real_world_config(codex_home: Path, world: str, scope_paths) -> tuple[Path, bool]:
    """provider.py と同じ手順で direct_read_roots/sensitive_deny を計算して config を書く。
    fail-closed（列挙失敗）なら direct_read_roots=[] で書く（本番と同じ縮退）。"""
    base_roots = codex_sandbox._direct_read_roots(world)
    try:
        scope_deny = codex_sandbox._scope_deny_entries(base_roots, scope_paths)
        sensitive = codex_sandbox._enumerate_sensitive(base_roots)
        sensitive_deny = sorted(set(scope_deny) | set(sensitive))
        direct_roots = base_roots
        ok = True
    except RuntimeError as e:
        _log(f"direct read disabled: {e}")
        direct_roots, sensitive_deny, ok = [], [], False
    _build_config(codex_home, base_roots, world, mcp=True, scope_paths=scope_paths,
                 direct_read_roots=direct_roots, sensitive_deny=sensitive_deny)
    return codex_home, ok


def scenario_mcp_observability(out_dir: Path, tmp_base: Path, world: str) -> dict:
    codex_home = tmp_base / "codex-home-mcp"
    authoring = tmp_base / "authoring-mcp"
    tmpdir = tmp_base / "tmp-mcp"
    authoring.mkdir(parents=True, exist_ok=True)
    tmpdir.mkdir(parents=True, exist_ok=True)
    codex_home, direct_ok = _real_world_config(codex_home, world, None)

    last_message = tmpdir / "last-message.txt"
    popen_env = codex_sandbox._codex_clean_env(codex_home, authoring, tmpdir)
    prompt = """あなたは orchestrator です。以下を順に行い、最後に短く統合して報告してください。
1. spawn_agent で名前 "worker" のサブエージェントを1つ起動し、MCP ツール read_doc（または
   list_docs → read_doc）で "gen1/src/main.c" を精読させ、含まれる関数名を1つだけ報告させる。
   worker はこの調査の途中で ask_user ツールを1回だけ使って「この資料で合っていますか」と確認してよい
   （回答は "はい" で構わないので進めること）。
2. worker の一次判断を受け取ったら、spawn_agent で名前 "evaluator" のサブエージェントを1つ起動し、
   worker の報告を別観点で査読させ、不足があれば指摘させる（無ければ「十分」と返させる）。
3. 続けて、同じこの1ターンの中でもう一度 spawn_agent(evaluator) を起動し、2巡目の査読として
   "他に確認すべき点はないか" を尋ね、結果を受け取る。
4. worker の一次判断・evaluator 1巡目・evaluator 2巡目の結果を3行で要約して返す（あなた自身は
   read_doc を呼ばない・調査をやり直さない）。"""
    argv = ["codex", "exec", "--json", "--strict-config", "--skip-git-repo-check",
            "-c", "features.multi_agent=true",
            "-o", str(last_message), "-C", str(authoring), "-m", _MAIN_MODEL,
            "-c", f"model_reasoning_effort={_REASONING}", prompt]
    t0 = time.time()
    rc, events, raw, err = _run_codex(argv, popen_env, authoring, _TIMEOUT)
    dur = time.time() - t0
    _dump(out_dir, "mcp_observability", events, raw, err)
    session_copy = _copy_session_jsonl(codex_home, out_dir, "mcp_observability")
    msg = last_message.read_text(encoding="utf-8", errors="replace").strip() if last_message.exists() else ""

    turn_completed = [e for e in events if e.get("type") == "turn.completed"]
    turn_failed = [e for e in events if e.get("type") in ("turn.failed", "error")]
    ask_user_events = [e for e in events
                       if "ask_user" in json.dumps(e, ensure_ascii=False)]
    mcp_events = [e for e in events if "read_doc" in json.dumps(e, ensure_ascii=False)
                 or "mcp_tool_call" in json.dumps(e, ensure_ascii=False)]
    agent_events = [e for e in events if any(k in json.dumps(e, ensure_ascii=False)
                                             for k in ("spawn_agent", "\"agent", "sub_agent", "subagent"))]
    return {
        "scenario": "mcp_observability", "returncode": rc, "duration_s": round(dur, 1),
        "direct_read_ok": direct_ok,
        "event_types": sorted(set(_event_types(events))),
        "turn_completed_count": len(turn_completed),
        "turn_failed_count": len(turn_failed),
        "usage_from_turn_completed": [e.get("usage") for e in turn_completed],
        "ask_user_event_count": len(ask_user_events),
        "mcp_event_count": len(mcp_events),
        "agent_event_count": len(agent_events),
        "session_jsonl": [str(p) for p in session_copy],
        "last_message": msg,
    }


# ---------------------------------------------------------------------------
# シナリオ 3: stop_resume（実 fixtures world）— (f)
# ---------------------------------------------------------------------------

def scenario_stop_resume(out_dir: Path, tmp_base: Path, world: str) -> dict:
    codex_home = tmp_base / "codex-home-stopresume"
    authoring = tmp_base / "authoring-stopresume"
    tmpdir = tmp_base / "tmp-stopresume"
    authoring.mkdir(parents=True, exist_ok=True)
    tmpdir.mkdir(parents=True, exist_ok=True)
    codex_home, _ = _real_world_config(codex_home, world, None)

    last_message = tmpdir / "last-message.txt"
    popen_env = codex_sandbox._codex_clean_env(codex_home, authoring, tmpdir)
    prompt = """あなたは orchestrator です。spawn_agent で名前 "worker" のサブエージェントを1つ起動し、
world 内のドキュメントを list_docs → read_doc で1つずつ、合計10個程度、時間をかけて丁寧に読み進めさせて
ください（急がず、各ファイルの要点を1行ずつ記録しながら進める）。全て読み終えたら要約を報告させ、
あなたはそれをそのまま返してください。"""
    argv = ["codex", "exec", "--json", "--strict-config", "--skip-git-repo-check",
            "-c", "features.multi_agent=true",
            "-o", str(last_message), "-C", str(authoring), "-m", _MAIN_MODEL,
            "-c", f"model_reasoning_effort={_REASONING}", prompt]
    _log("stop_resume: 初回 attempt を起動し、数秒後に SIGINT を送る")
    proc = subprocess.Popen(argv, env=popen_env, cwd=str(authoring),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    thread_id = None
    lines_before_stop = []
    t0 = time.time()
    # thread.started（thread_id 捕捉）が出るまで、または上限秒まで stdout を先読みする。
    import select
    deadline = t0 + 25
    while time.time() < deadline:
        r, _, _ = select.select([proc.stdout], [], [], 1.0)
        if r:
            line = proc.stdout.readline()
            if not line:
                break
            lines_before_stop.append(line)
            try:
                e = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if e.get("type") == "thread.started":
                thread_id = e.get("thread_id")
            if thread_id and (time.time() - t0) > 6:
                break
        if proc.poll() is not None:
            break
    time.sleep(3)   # spawn_agent(worker) が動き出す猶予
    _log(f"stop_resume: thread_id={thread_id!r} 経過{time.time()-t0:.1f}s で SIGINT 送信")
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
    except ProcessLookupError:
        pass
    try:
        rest_out, rest_err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        rest_out, rest_err = proc.communicate(timeout=15)
    attempt1_raw = "".join(lines_before_stop) + (rest_out or "")
    attempt1_events = []
    for line in attempt1_raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                attempt1_events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
            else:
                if attempt1_events[-1].get("type") == "thread.started":
                    thread_id = attempt1_events[-1].get("thread_id") or thread_id
    attempt1_returncode = proc.returncode
    _dump(out_dir, "stop_resume_attempt1", attempt1_events, attempt1_raw, rest_err or "")

    attempt2 = {"ran": False}
    if thread_id:
        last_message2 = tmpdir / "last-message-2.txt"
        # `codex exec resume <sid> [prompt]` は exec 共通オプションの**後**・プロンプトの**前**に
        # 挟む（`resume` サブコマンドは `-C`/`-m` 等の exec 共通フラグを自分の前に取らない・実測
        # `error: unexpected argument '-C' found` で判明。provider.py の `_build_argv` と同じ順序）。
        argv2 = ["codex", "exec", "--json", "--strict-config",
                "--skip-git-repo-check", "-c", "features.multi_agent=true",
                "-o", str(last_message2), "-C", str(authoring), "-m", _MAIN_MODEL,
                "-c", f"model_reasoning_effort={_REASONING}",
                "resume", thread_id, "続けてください（要約まで完了させて）。"]
        rc2, events2, raw2, err2 = _run_codex(argv2, popen_env, authoring, _STOP_RESUME_TIMEOUT)
        _dump(out_dir, "stop_resume_attempt2", events2, raw2, err2)
        attempt2 = {
            "ran": True, "returncode": rc2,
            "event_types": sorted(set(_event_types(events2))),
            "turn_completed_count": len([e for e in events2 if e.get("type") == "turn.completed"]),
            "turn_failed_count": len([e for e in events2 if e.get("type") in ("turn.failed", "error")]),
        }
    session_copy = _copy_session_jsonl(codex_home, out_dir, "stop_resume")
    return {
        "scenario": "stop_resume",
        "thread_id_captured": bool(thread_id),
        "attempt1_returncode": attempt1_returncode,
        "attempt1_event_types": sorted(set(_event_types(attempt1_events))),
        "attempt1_turn_completed_count": len([e for e in attempt1_events if e.get("type") == "turn.completed"]),
        "attempt1_turn_failed_count": len([e for e in attempt1_events if e.get("type") in ("turn.failed", "error")]),
        "attempt2": attempt2,
        "session_jsonl": [str(p) for p in session_copy],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", required=True, help="イベント/セッションログの保存先ディレクトリ")
    ap.add_argument("--scenario", default="all",
                    help="containment,mcp_observability,stop_resume のカンマ区切り（既定 all）")
    ap.add_argument("--world", default="c1", help="mcp/stop_resume シナリオで使う fixtures world")
    args = ap.parse_args()

    if not shutil.which("codex"):
        print("codex CLI が見つかりません（PATH 未導入）", file=sys.stderr)
        return 2
    if os.environ.get("SHERPA_USE_FIXTURES", "").lower() not in ("1", "true", "yes"):
        print("SHERPA_USE_FIXTURES=1 を設定して実行してください（fixtures world を使うため）", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scenarios = (["containment", "mcp_observability", "stop_resume"] if args.scenario == "all"
                else [s.strip() for s in args.scenario.split(",") if s.strip()])

    results = {}
    # 実測（2026-09-17・containment シナリオ初回実行）: `tempfile.TemporaryDirectory()` の既定は
    # システム `/tmp` 配下になり、Codex 0.153.4 の既定 permission profile（`":minimal" = "read"`）は
    # `/tmp` 配下を読取封じ込めの対象外として広く read させる（kb_root と同じ `/tmp` 系の兄弟
    # ディレクトリに置いた「範囲外」マーカーファイルが、明示 deny も無いのに子から読めた＝
    # `outside-root` 実測）。`$HOME` 配下（システム tmp ではない場所）に同じ構成を置くと兄弟ディレクトリは
    # 期待どおり不可視（`No such file or directory`）になることを確認済み——本番の KB/users_dir は
    # システム `/tmp` 配下に置かない前提のため実害の再現ではないが、本プローブ自身の suite が
    # 誤って `/tmp` 配下で走ると偽陰性（封じ込め失敗の誤報）になるため、作業ルートは `$HOME` 配下に取る。
    with tempfile.TemporaryDirectory(prefix="probe-codex-ma-", dir=os.path.expanduser("~")) as tmp:
        tmp_base = Path(tmp)
        if "containment" in scenarios:
            _log("=== containment 開始 ===")
            results["containment"] = scenario_containment(out_dir, tmp_base)
        if "mcp_observability" in scenarios:
            _log("=== mcp_observability 開始 ===")
            results["mcp_observability"] = scenario_mcp_observability(out_dir, tmp_base, args.world)
        if "stop_resume" in scenarios:
            _log("=== stop_resume 開始 ===")
            results["stop_resume"] = scenario_stop_resume(out_dir, tmp_base, args.world)

    (out_dir / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
