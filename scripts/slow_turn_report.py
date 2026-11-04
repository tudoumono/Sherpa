"""回答に時間がかかる原因の調べ（`scripts/slow_turn_report.sh`）。
設計: docs/design/codex.md「MCP の道具」

① 資料の置き場の速さ: 資料フォルダごとに、原本・派生の Markdown・RAG 用の置き場の種類、フォルダを全部歩く時間
   （`ripgrep_search` が毎回行う `search_cache.tree_signature` と同じ歩き方）、資料 1 件があるかの確かめの時間
   （`es_search` が使う `documents.existing_rels`）、`rg` 1 回の時間を測る。
② 道具のサーバーの試し: 回答のときと同じ環境変数で `sherpa.mcp_server` を起動し、起動・`es_search`・`ripgrep_search` の
   時間と失敗の種類を測る。DB の接続設定を道具のサーバーへ渡しているかも見る。
③ 直近の回答の内訳: 会話ごとの Codex のセッション記録から、回答ごとの時間・道具（シェルのコマンドを含む）ごとの回数と時間を集める。

守ること:
- DB・原本を書き換えない（②の文字列検索だけは、回答のときと同じく派生の側の検索の使い回し用の置き場に書く）。
  DB の表・索引を作る処理（`store.db.init_schema`）は通らない。
- 資料のファイル名・フォルダ名・パス・ホスト名・質問や回答の文を出力に出さない（資料フォルダは W1・W2… と呼ぶ）。
"""
from __future__ import annotations

import argparse
import logging
import importlib.util
import json
import os
import random
import re
import selectors
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_JST = timezone(timedelta(hours=9))
_NETWORK_FS = ("nfs", "nfs4", "cifs", "smb", "smbfs", "smb2", "smb3", "9p", "drvfs", "fuse", "sshfs", "afpfs",
               "webdav", "davfs", "v9fs")
# Codex が MCP の道具のサーバーへそのまま渡す環境変数（それ以外は設定ファイルの env だけが渡る）
_CODEX_MCP_BASE_ENV = ("HOME", "LOGNAME", "PATH", "SHELL", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR", "TZ")
_DB_ENV_KEYS = ("SHERPA_PG_DSN", "DATABASE_URL", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE")
_KNOWN_PROGRAMS = frozenset({"rg", "grep", "egrep", "fgrep", "cat", "sed", "head", "tail", "find", "ls", "wc", "awk",
                             "python", "python3", "nl", "sort", "uniq", "xargs", "file", "iconv", "git", "jq",
                             "pdftotext", "tr", "cut", "stat", "du", "tree", "less", "more", "od", "xxd", "strings",
                             "bash", "sh", "zsh", "echo", "printf", "test", "diff", "nkf"})
_EXEC_TOOLS = frozenset({"exec", "exec_command", "shell", "local_shell"})
_STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
_DEADLINE_LIKE_S = 110.0
_MCP_CALL_TIMEOUT_S = 300.0


def _code(value) -> str:
    """失敗の種類を表す短い記号だけを通す（文やパスは出さない）。"""
    s = str(value or "")
    return s if s and len(s) <= 60 and all(c.isascii() and (c.isalnum() or c in "_-.") for c in s) else "その他"


def _sec(v: float | None) -> str:
    return "-" if v is None else (f"{v:.1f}秒" if v < 120 else f"{v / 60:.1f}分")


# ---------------------------------------------------------------------------
# ① 資料の置き場の速さ
# ---------------------------------------------------------------------------

def fs_type(path: Path) -> str:
    """`path` が載っているファイルシステムの種類（Linux の /proc/mounts で一番長く一致するマウント）。分からなければ「不明」。"""
    try:
        target = os.path.realpath(path)
        best, best_type = "", None
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                    best, best_type = mnt, parts[2]
        return best_type or "不明"
    except OSError:
        return "不明"


def walk_dirs(root: Path, timeout_s: float) -> dict:
    """`search_cache.tree_signature` と同じく、フォルダごとに stat と scandir をして木を歩く（版管理のフォルダには入らない）。フォルダ・ファイルの数と時間。"""
    from sherpa import scope_infer
    t0 = time.monotonic()
    dirs = files = 0
    stack = [str(root)]
    done = True
    while stack:
        if time.monotonic() - t0 > timeout_s:
            done = False
            break
        d = stack.pop()
        try:
            os.stat(d)
            with os.scandir(d) as it:
                dirs += 1
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not scope_infer.is_vcs_dir_name(e.name):
                                stack.append(e.path)
                        else:
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return {"dirs": dirs, "files": files, "seconds": time.monotonic() - t0, "done": done}


def rg_once(rg: str, root: Path, timeout_s: float) -> dict:
    """見つからない語で `rg` を 1 回かけ、全部を読み終えるまでの時間を測る。"""
    token = "zq" + "".join(random.choice("0123456789abcdef") for _ in range(16))
    t0 = time.monotonic()
    try:
        subprocess.run([rg, "-c", "--no-messages", "--", token, str(root)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout_s, check=False)
        return {"seconds": time.monotonic() - t0, "done": True}
    except subprocess.TimeoutExpired:
        return {"seconds": timeout_s, "done": False}


def exists_check(world: str, sample: int = 50) -> dict | None:
    """取り込みの台帳から資料を `sample` 件選び、`documents.existing_rels`（es_search が使う確かめ）の時間を測る。"""
    from sherpa import doc_ledger, documents
    rels = doc_ledger.ledger_rels(world)
    if not rels:
        return None
    picked = random.sample(sorted(rels), min(sample, len(rels)))
    t0 = time.monotonic()
    found = documents.existing_rels(world, picked)
    sec = time.monotonic() - t0
    return {"n": len(picked), "found": len(found), "seconds": sec, "per_file_ms": sec * 1000 / len(picked)}


def _in_child(kind: str, target: str, timeout_s: float, extra: list[str] | None = None) -> dict | list | None:
    """ネットワークの上や大きな記録で止まりうる調べを別のプロセスで動かし、`timeout_s` で止める。止めたら打ち切りの印だけ返す。"""
    # 子はプロセスのまとまりごと止める。ファイルシステムの読み込みで固まった子は、回収を待ちきれなければ置いて進む
    proc = None

    def _stop() -> None:
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    try:
        # 起動する間は止める合図を保留し、片付けられる状態になってから受け取る
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
        try:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--_child", kind, "--_target",
                                     target, *(extra or [])], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    env=os.environ.copy(), start_new_session=True)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _stop()
        return {"seconds": timeout_s, "done": False, "dirs": 0, "files": 0, "child_timeout": True}
    except BaseException:
        _stop()  # 中断されたときも子のまとまりを残さない
        raise
    try:
        return json.loads(out.decode("utf-8")) if proc.returncode == 0 else None
    except ValueError:
        return None


_PLACE_NAMES = ("原本", "派生の Markdown", "RAG 用")


def measure_place(world: str, index: int, skip: set, rg_timeout: float, walk_timeout: float) -> dict:
    """置き場 1 つ（`_PLACE_NAMES[index]`）の場所の解決・種類・歩く時間・rg 1 回（別のプロセスの中で動かす）。"""
    from sherpa import rg_search, worlds
    p = (worlds.world_dir, worlds.derived_md_dir, worlds.derived_rag_dir)[index](world)
    if not p or not Path(p).is_dir():
        return {"missing": True}
    p = Path(p)
    fst = fs_type(p)
    row = {"fs": fst, "network": fst.split(".")[0] in _NETWORK_FS}
    if "walk" not in skip:
        row["walk"] = walk_dirs(p, walk_timeout)
    rg = rg_search.find_rg()
    if "rg" not in skip and rg:
        row["rg"] = rg_once(rg, p, rg_timeout)
    return row


def place_rows(world: str, label: str, skip: set, rg_timeout: float, walk_timeout: float) -> list[dict]:
    """資料フォルダ 1 つの置き場（原本・派生の Markdown・RAG 用）ごとの測った値。置き場ごとに別のプロセスで測り、止まったら打ち切る。"""
    budget = walk_timeout + rg_timeout + 30
    rows = []
    for i, name in enumerate(_PLACE_NAMES):
        got = _in_child("place", world, budget, ["--_index", str(i), "--skip", ",".join(sorted(skip)),
                                                  "--rg-timeout", str(rg_timeout), "--walk-timeout", str(walk_timeout)])
        if got is None:
            rows.append({"world": label, "place": name, "failed": True})
        elif got.get("child_timeout"):
            rows.append({"world": label, "place": name, "child_timeout": budget})
        else:
            rows.append({"world": label, "place": name, **got})
    if "walk" not in skip:
        got = _in_child("exists", world, walk_timeout)
        if got is None:
            rows[0]["exists_error"] = True
        elif got.get("done") is False:
            rows[0]["exists_timeout"] = walk_timeout
        else:
            rows[0]["exists"] = got.get("result")
    return rows


# ---------------------------------------------------------------------------
# ② 道具のサーバーの試し
# ---------------------------------------------------------------------------

def mcp_env(world: str) -> dict:
    """回答のときに Codex が道具のサーバーへ渡すのと同じ環境変数（`providers/codex/sandbox.py` の設定ファイルの env）。"""
    from sherpa.providers.codex import mcp as codex_mcp
    menv = codex_mcp._mcp_env(world, None)
    menv.setdefault("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    menv.setdefault("PYTHONPATH", str(_ROOT))
    base = {k: os.environ[k] for k in _CODEX_MCP_BASE_ENV if k in os.environ}
    return {**base, **menv}


class _Rpc:
    """改行区切り JSON-RPC で道具のサーバーと話す（応答待ちに打ち切りを付ける）。"""

    def __init__(self, env: dict, cwd: str):
        self.p = subprocess.Popen([sys.executable, "-m", "sherpa.mcp_server"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=cwd)
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.p.stdout, selectors.EVENT_READ)
        self.buf = b""

    def send(self, msg: dict) -> None:
        self.p.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def wait(self, rid: int, timeout_s: float) -> tuple[dict | None, float]:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == rid:
                    return msg, time.monotonic() - t0
            if not self.sel.select(timeout=max(0.1, timeout_s - (time.monotonic() - t0))):
                continue
            chunk = os.read(self.p.stdout.fileno(), 65536)
            if not chunk:
                return None, time.monotonic() - t0
            self.buf += chunk
        return None, timeout_s

    def close(self) -> None:
        try:
            self.p.kill()
        except OSError:
            pass
        self.p.wait(timeout=10)


def _tool_outcome(msg: dict | None) -> dict:
    """道具の結果から、失敗の種類・件数・期限切れの印だけを取り出す（中身は出さない）。"""
    if msg is None:
        return {"status": "応答なし"}
    if "error" in msg:
        return {"status": "失敗", "code": _code((msg.get("error") or {}).get("code"))}
    res = msg.get("result") or {}
    text = "".join(c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict))
    try:
        body = json.loads(text)
    except ValueError:
        body = {}
    out = {"status": "失敗" if res.get("isError") else "成功"}
    if isinstance(body, dict):
        if body.get("error"):
            out["code"] = _code(body.get("error"))
        if isinstance(body.get("hits"), list):
            out["hits"] = len(body["hits"])
        for k in ("degrade_reason", "mode_used"):
            if body.get(k):
                out[k] = _code(body.get(k))
        if body.get("deadline_exceeded"):
            out["deadline_exceeded"] = True
    return out


def mcp_trial(world: str) -> dict:
    """道具のサーバーを起動し、起動・es_search（既定の方式と語の一致だけ）・ripgrep_search の時間を測る。"""
    env = mcp_env(world)
    out = {"db_env_passed": any(k in env for k in _DB_ENV_KEYS), "calls": []}
    token = "zq" + "".join(random.choice("0123456789abcdef") for _ in range(16))
    with tempfile.TemporaryDirectory() as cwd:
        rpc = _Rpc(env, cwd)
        try:
            rpc.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "slow-turn-report", "version": "0"}}})
            msg, sec = rpc.wait(1, 120)
            out["startup_s"] = sec
            if msg is None:
                out["startup_failed"] = True
                return out
            rpc.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            rpc.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            msg, sec = rpc.wait(2, 60)
            out["tools_list_s"] = sec
            trials = [("es_search（既定の方式）", "es_search", {"query": "仕様"}),
                      ("es_search（語の一致だけ）", "es_search", {"query": "仕様", "mode": "keyword"}),
                      ("ripgrep_search（見つからない語）", "ripgrep_search", {"query": token})]
            for i, (label, tool, args) in enumerate(trials, start=3):
                rpc.send({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": tool, "arguments": args}})
                msg, sec = rpc.wait(i, _MCP_CALL_TIMEOUT_S)
                out["calls"].append({"label": label, "seconds": sec, **_tool_outcome(msg)})
                if msg is None:
                    break  # 1 件ずつ処理するサーバーなので、止まったら後ろも待たされる
        finally:
            rpc.close()
    return out


# ---------------------------------------------------------------------------
# ③ 直近の回答の内訳
# ---------------------------------------------------------------------------

def _load_trace_module():
    spec = importlib.util.spec_from_file_location("conversation_trace", _ROOT / "scripts" / "conversation_trace.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["conversation_trace"] = mod
    spec.loader.exec_module(mod)
    return mod


_JS_TOOL_RE = re.compile(r"tools\.([A-Za-z0-9_]+)\s*\(")
_JS_CMD_RE = re.compile(r"""\bcmd\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`)""")


def _js_string(lit: str) -> str:
    """JavaScript の文字列リテラルの中身（引用符の種類によらず、先頭のプログラム名が読めれば足りる）。"""
    if lit.startswith('"'):
        try:
            return json.loads(lit)
        except ValueError:
            return lit[1:-1]
    return lit[1:-1]


def _shell_key(ct, command: str | None) -> str:
    prog = ct._program(command)
    return "シェル " + (prog if prog in _KNOWN_PROGRAMS else "その他")


def _tool_name(name: str) -> str:
    short = name[len("mcp__sherpa__"):] if name.startswith("mcp__sherpa__") else name
    return short if short.isascii() and len(short) <= 40 else "その他"


def exec_parts(ct, code: str) -> list[str]:
    """Codex の `exec`（道具を呼ぶ JavaScript）の中で呼んだ道具を順に並べる。シェルのコマンドは先頭のプログラム名だけ。"""
    cmds = [_js_string(m.group(1)) for m in _JS_CMD_RE.finditer(code)]
    out, ci = [], 0
    for name in _JS_TOOL_RE.findall(code):
        if name == "exec_command":
            out.append(_shell_key(ct, cmds[ci] if ci < len(cmds) else None))
            ci += 1
        else:
            out.append(_tool_name(name))
    return out


def _exec_inputs(threads) -> dict:
    """セッション記録の `exec` の呼び出しごとの JavaScript（呼び出しの ID → 中身）。"""
    out = {}
    for th in threads:
        for _, rec in th.events:
            p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if p.get("type") == "custom_tool_call" and isinstance(p.get("call_id"), str) and isinstance(p.get("input"), str):
                out[(th.tid, p["call_id"])] = p["input"]
    return out


def _call_parts(ct, c, inputs: dict) -> list[str]:
    """1 回の呼び出しで使った道具（`exec` は中で呼んだ道具・シェルのコマンドは `シェル <プログラム>`）。"""
    if c.name in _EXEC_TOOLS:
        code = inputs.get((c.thread.tid, c.call_id))
        if code is not None and _JS_TOOL_RE.search(code):
            return exec_parts(ct, code)
        return [_shell_key(ct, c.command)]
    return [_tool_name(str(c.name or ""))]


def _call_key(parts: list[str]) -> str:
    """集計の行の名前。道具が 1 種類ならその名前、2 種類以上なら名前を並べる（3 種類まで）。"""
    uniq = sorted(set(parts)) or ["-"]
    return "・".join(uniq[:3]) + ("…" if len(uniq) > 3 else "")


def _busy_seconds(calls) -> float:
    """道具が動いていた時間（呼び出しの区間を重ねずに足した長さ）。"""
    spans = sorted((c.ts, c.ts + (c.duration_ms or 0) / 1000) for c in calls if c.duration_ms)
    total, cur_s, cur_e = 0.0, None, None
    for s, e in spans:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


_SESSION_DIR_MAX_BYTES = 300 * 1024 * 1024


def _dir_bytes(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*.jsonl") if f.is_file() and not f.is_symlink())


def recent_turns(days: int, timeout_s: float) -> dict:
    """直近 `days` 日の回答ごとの時間と、道具ごとの回数・時間を集める（全体で `timeout_s` 秒まで・大きすぎるセッション記録は飛ばす）。"""
    t0 = time.monotonic()
    skipped_large = 0
    timed_out = False
    ct = _load_trace_module()
    until = datetime.now(_JST).date()
    since = until - timedelta(days=max(1, days) - 1)
    ids, over = ct.select_conversation_ids(since, until)
    masker = ct.Masker(mask=True, mask_models=True)
    turns, tools = [], {}
    for cid in ids:
        if time.monotonic() - t0 > timeout_s:
            timed_out = True
            break
        data = ct.load_conversation(int(cid))
        conv = data.get("conversation") or {}
        uid = conv.get("user_id")
        threads = []
        if isinstance(uid, str):
            conv_dir = ct._users_dir() / uid / "workspace" / ".codex-sessions" / str(cid)
            if conv_dir.is_dir() and not conv_dir.is_symlink():
                if _dir_bytes(conv_dir) > _SESSION_DIR_MAX_BYTES:
                    skipped_large += 1
                else:
                    threads, _ = ct.read_threads(conv_dir)
        inputs = _exec_inputs(threads)
        for no, t in enumerate(ct._turn_pairs(data), 1):
            u, a = t["user"], t["assistant"]
            if a is None:
                continue
            answer = a.get("answer") if isinstance(a.get("answer"), dict) else {}
            end = ct._epoch(a.get("created_at"))
            start = ct._epoch((u or {}).get("created_at")) if u else None
            dur_ms = answer.get("duration_ms") if isinstance(answer.get("duration_ms"), int) else None
            if start is None and end is not None and dur_ms is not None:
                start = end - dur_ms / 1000
            if end is None or start is None:
                continue
            w_start, w_end = start - ct._WINDOW_BEFORE_S, end + ct._WINDOW_AFTER_S
            tt = [th for th in threads if any(w_start <= ep <= w_end for ep, _ in th.events)]
            calls = ct.collect_calls(tt, w_start, w_end) if tt else []
            stages = ct.build_stages(tt, w_start, w_end, masker) if tt else []
            parts = {id(c): _call_parts(ct, c, inputs) for c in calls}
            longest = max(calls, key=lambda c: c.duration_ms or 0, default=None)
            row = {"conv": cid, "turn": no, "seconds": (dur_ms / 1000) if dur_ms else end - start,
                   "calls": len(calls), "tool_uses": sum(len(v) for v in parts.values()),
                   "batched": sum(1 for v in parts.values() if sum(1 for x in v if not x.startswith("シェル")) >= 2),
                   "busy_s": _busy_seconds(calls),
                   "longest_s": (longest.duration_ms or 0) / 1000 if longest else 0,
                   "longest_tool": _call_key(parts[id(longest)]) if longest else "-",
                   "deadline_like": sum(1 for c in calls if (c.duration_ms or 0) / 1000 >= _DEADLINE_LIKE_S),
                   "errors": sum(1 for c in calls if c.is_error),
                   "stages": len(stages), "agents": len({c.thread.tid for c in calls}),
                   "session_found": bool(tt)}
            turns.append(row)
            slow = row["seconds"] >= 600
            for c in calls:
                e = tools.setdefault(_call_key(parts[id(c)]), {"calls": 0, "total_s": 0.0, "max_s": 0.0, "errors": 0,
                                                        "deadline_like": 0, "slow_turn_calls": 0})
                s = (c.duration_ms or 0) / 1000
                e["calls"] += 1
                e["total_s"] += s
                e["max_s"] = max(e["max_s"], s)
                e["errors"] += int(bool(c.is_error))
                e["deadline_like"] += int(s >= _DEADLINE_LIKE_S)
                e["slow_turn_calls"] += int(slow)
    return {"since": since.isoformat(), "until": until.isoformat(), "conversations": len(ids), "truncated": over,
            "turns": turns, "tools": tools, "skipped_large": skipped_large, "timed_out": timed_out}


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------

def render(places: list, mcp: list, recent: dict | None, *, detail: bool) -> str:
    L = [f"# 回答の時間の調べ  {datetime.now(_JST):%Y-%m-%d %H:%M}（日本時間）"]
    if places:
        L += ["", "## ① 資料の置き場の速さ",
              "| 資料フォルダ | 置き場 | 種類 | フォルダ数 | ファイル数 | 全部歩く時間 | rg 1回 | 1件の確かめ |",
              "|---|---|---|---:|---:|---:|---:|---:|"]
        for r in places:
            if r.get("missing"):
                L.append(f"| {r['world']} | {r['place']} | 無し | - | - | - | - | - |")
                continue
            if r.get("failed") or r.get("child_timeout"):
                why = "測れない" if r.get("failed") else f"{_sec(r['child_timeout'])} 以上応答なし（打ち切り）"
                L.append(f"| {r['world']} | {r['place']} | {why} | - | - | - | - | - |")
                continue
            w, g, ex = r.get("walk"), r.get("rg"), r.get("exists")
            kind = r["fs"] + ("（ネットワーク）" if r["network"] else "")
            walk_s = "-" if not w else _sec(w["seconds"]) + ("" if w["done"] else " 以上（打ち切り）")
            rg_s = "-" if not g else _sec(g["seconds"]) + ("" if g["done"] else " 以上（打ち切り）")
            ex_s = "-" if not ex else f"{ex['per_file_ms']:.0f}ms（{ex['n']}件で{_sec(ex['seconds'])}）"
            if r.get("exists_error"):
                ex_s = "台帳が読めない"
            if r.get("exists_timeout"):
                ex_s = f"{_sec(r['exists_timeout'])} 以上（打ち切り）"
            L.append(f"| {r['world']} | {r['place']} | {kind} | {w['dirs'] if w else '-'} | {w['files'] if w else '-'} "
                     f"| {walk_s} | {rg_s} | {ex_s} |")
    if mcp:
        L += ["", "## ② 道具のサーバーの試し（回答のときと同じ環境変数で起動・範囲は全体・既定の深さ）"]
        for m in mcp:
            if m.get("error"):
                L.append(f"- {m['world']}: 試せませんでした（{m['error']}）")
                continue
            L.append(f"- {m['world']}: 起動 {_sec(m.get('startup_s'))}"
                     + ("（応答なし）" if m.get("startup_failed") else "")
                     + f"・DB の接続設定を道具のサーバーへ {'渡している' if m['db_env_passed'] else '渡していない'}")
            for c in m["calls"]:
                extra = "・".join(f"{k}={c[k]}" for k in ("code", "hits", "degrade_reason", "mode_used") if k in c)
                L.append(f"  - {c['label']}: {_sec(c['seconds'])}  {c['status']}"
                         + (f"（{extra}）" if extra else "") + ("・期限切れ" if c.get("deadline_exceeded") else ""))
    if recent is not None:
        turns = recent["turns"]
        L += ["", f"## ③ 直近の回答（{recent['since']}〜{recent['until']}・会話 {recent['conversations']} 件・回答 {len(turns)} 件"
              + ("・上限で一部だけ" if recent["truncated"] else "")
              + ("・時間の上限で途中まで" if recent.get("timed_out") else "")
              + (f"・記録が大きすぎて飛ばした会話 {recent['skipped_large']} 件" if recent.get("skipped_large") else "") + "）"]
        if turns:
            secs = sorted(t["seconds"] for t in turns)
            L.append(f"- 回答の時間: 中央 {_sec(secs[len(secs) // 2])}・最長 {_sec(secs[-1])}・10 分以上 "
                     f"{sum(1 for s in secs if s >= 600)} 件・セッション記録の無い回答 {sum(1 for t in turns if not t['session_found'])} 件")
            L += ["", "遅い回答（上から 10 件）",
                  "| 会話 | 回答 | 時間 | 呼び出し（中の道具） | まとめて呼んだ | 道具が動いた時間 | 最長の 1 回 | 110秒以上 | 失敗 | 段 | エージェント |",
                  "|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|"]
            for t in sorted(turns, key=lambda t: -t["seconds"])[:10 if not detail else len(turns)]:
                L.append(f"| {t['conv']} | {t['turn']} | {_sec(t['seconds'])} | {t['calls']}（{t['tool_uses']}） | {t['batched']} | {_sec(t['busy_s'])} "
                         f"| {_sec(t['longest_s'])} {t['longest_tool']} | {t['deadline_like']} | {t['errors']} "
                         f"| {t['stages']} | {t['agents']} |")
            L += ["", "道具ごと（合計の時間が長い順）",
                  "| 道具 | 回数 | 合計 | 最長 | 110秒以上 | 失敗 | 10分以上の回答の中 |", "|---|---:|---:|---:|---:|---:|---:|"]
            for k, e in sorted(recent["tools"].items(), key=lambda kv: -kv[1]["total_s"])[:12 if not detail else None]:
                L.append(f"| {k} | {e['calls']} | {_sec(e['total_s'])} | {_sec(e['max_s'])} | {e['deadline_like']} "
                         f"| {e['errors']} | {e['slow_turn_calls']} |")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回答に時間がかかる原因の調べ（読むだけ）")
    ap.add_argument("--world", help="1 つの資料フォルダに絞る")
    ap.add_argument("--days", type=int, default=2, help="③で見る期間（日・既定 2）")
    ap.add_argument("--skip", default="", help="飛ばす調べ（walk,rg,mcp,turns をカンマ区切り）")
    ap.add_argument("--rg-timeout", type=float, default=120.0)
    ap.add_argument("--walk-timeout", type=float, default=300.0)
    ap.add_argument("--turns-timeout", type=float, default=300.0)
    ap.add_argument("--out-dir")
    ap.add_argument("--_child", help=argparse.SUPPRESS)
    ap.add_argument("--_target", help=argparse.SUPPRESS)
    ap.add_argument("--_index", type=int, default=0, help=argparse.SUPPRESS)
    ns = ap.parse_args(argv)
    skip = {s.strip() for s in ns.skip.split(",") if s.strip()}
    if ns._child:
        # 調べでは DB の表・索引を作る処理を通らない（作ってある前提で読むだけ・無ければ読み込みが失敗する）
        from sherpa.store import db as _db
        _db._inited = True
    if ns._child == "place":
        print(json.dumps(measure_place(ns._target, ns._index, skip, ns.rg_timeout, ns.walk_timeout)))
        return 0
    if ns._child == "mcp":
        print(json.dumps(mcp_trial(ns._target)))
        return 0
    if ns._child == "turns":
        print(json.dumps(recent_turns(int(ns._target), ns.turns_timeout)))
        return 0
    if ns._child == "exists":
        t0 = time.monotonic()
        print(json.dumps({"result": exists_check(ns._target), "seconds": time.monotonic() - t0}))
        return 0
    if ns._child == "worlds":
        from sherpa import worlds
        print(json.dumps(worlds.discover_world_ids()))
        return 0
    if ns.world:
        ids = [ns.world]
    else:
        got = _in_child("worlds", "-", 60)
        if not isinstance(got, list):
            print("資料フォルダの一覧を読めませんでした（WORLD=<名前> で 1 つに絞ってください）", file=sys.stderr)
            return 1
        ids = got
    labels = {w: f"W{i}" for i, w in enumerate(ids, 1)}
    places, mcp = [], []
    for w in ids:
        if not ({"walk", "rg"} <= skip):
            print(f"{labels[w]} の置き場を測っています…", file=sys.stderr)
            places += place_rows(w, labels[w], skip, ns.rg_timeout, ns.walk_timeout)
        if "mcp" not in skip:
            print(f"{labels[w]} の道具のサーバーを試しています…", file=sys.stderr)
            budget = 120 + 60 + 3 * _MCP_CALL_TIMEOUT_S + 30
            got = _in_child("mcp", w, budget)
            if got is None:
                mcp.append({"world": labels[w], "error": "起動できない"})
            elif got.get("child_timeout"):
                mcp.append({"world": labels[w], "error": f"{_sec(budget)} 以上応答なし（打ち切り）"})
            else:
                mcp.append({"world": labels[w], **got})
    recent = None
    if "turns" not in skip:
        print("直近の回答を集めています…", file=sys.stderr)
        got = _in_child("turns", str(ns.days), ns.turns_timeout + 30,
                        ["--turns-timeout", str(ns.turns_timeout)])
        if got is None:
            print("直近の回答を集められませんでした", file=sys.stderr)
        elif got.get("child_timeout"):
            print(f"直近の回答の集計が {_sec(ns.turns_timeout + 30)} で終わらず打ち切りました（DAYS を短くしてください）", file=sys.stderr)
        else:
            recent = got
    run_dir = Path(os.environ.get("RUN_DIR") or (_ROOT / "data" / "run"))
    run_dir = run_dir if run_dir.is_absolute() else _ROOT / run_dir
    out_dir = Path(ns.out_dir) if ns.out_dir else run_dir / "slow-turn-report"
    out_dir = out_dir if out_dir.is_absolute() else _ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(_JST).strftime("%Y%m%d-%H%M%S")
    detail_path = out_dir / f"slow-turn-report-{stamp}-detail.txt"
    detail_path.write_text(render(places, mcp, recent, detail=True), encoding="utf-8")
    sys.stdout.write(render(places, mcp, recent, detail=False))
    shown = detail_path.relative_to(_ROOT) if detail_path.is_relative_to(_ROOT) else f"（--out-dir で指定した場所）/{detail_path.name}"
    print(f"\n詳しい結果: {shown}")
    return 0


class _Stopped(BaseException):
    """止める合図（SIGTERM・SIGHUP・SIGQUIT）を受け取った。待っている子のまとまりを止めてから終わる。"""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def _on_stop_signal(signum, _frame):
    raise _Stopped(signum)


def _main_quiet() -> int:
    """Sherpa の記録の出力を止め、止まったときは例外の種類の名前だけを出す（資料フォルダ名やパスを端末に出さない）。"""
    logging.disable(logging.CRITICAL)
    for sig in _STOP_SIGNALS:
        signal.signal(sig, _on_stop_signal)
    try:
        return main()
    except KeyboardInterrupt:
        return 130
    except _Stopped as e:
        return 128 + e.signum
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, SystemExit):
            raise
        print(f"調べの途中で止まりました（{_code(type(e).__name__)}）", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_main_quiet())
