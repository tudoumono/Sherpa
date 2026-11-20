"""つながりの確認のうち、調べものの道具のサーバー（`sherpa.mcp_server`）そのものへの直接の呼び出し。モデルを通さず、お金はかからない。
本物のターンと同じコマンド・同じ env で子プロセスを起こし、stdio の JSON-RPC で `initialize` → `tools/list` → `tools/call` を送る。
成否は応答の形（`error`・`isError`）だけで決め、道具の結果の本文・資料の名前は結果にも記録にも出さない（出すのは道具の名前・件数・エラーの種類だけ）。
台帳の道具と質問の道具は呼ばない。台帳・呼び出しの記録・読取の記録の置き場は渡さない＝何も書かない。
設計: docs/design/operations.md「システム状態とつながりの確認」
"""
from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import connectivity
from .connectivity_base import _Incomplete, _Ng, _Skip, classify

TOTAL_TIMEOUT_S = 90.0
CALL_TIMEOUT_S = 20.0
_START_TIMEOUT_S = 20.0
_GRAPH_START_TRIES = 10
_SERVER_CMD = [sys.executable, "-m", "sherpa.mcp_server"]

# 道具のまとまりごとの行（id・表示名）。上から順に流す。
ROWS = (
    ("tools_list", "道具の一覧を取れるか"),
    ("tools_docs", "資料の一覧と検索（道具）"),
    ("tools_read", "資料を読む（道具）"),
    ("tools_es", "全文検索（道具）"),
    ("tools_graph", "つながりの検索（道具）"),
    ("tools_office", "Office・PDF を読む（道具）"),
)
BASE_TOOLS = ("list_docs", "folder_tree", "name_lookup", "ripgrep_search", "glob_search",
              "doc_outline", "read_doc", "read_around", "file_head")
_OFFICE_BY_SUFFIX = {
    ".xlsx": "xlsx_sheets", ".xlsm": "xlsx_sheets", ".xls": "xlsx_sheets",
    ".docx": "docx_paragraphs", ".doc": "docx_paragraphs",
    ".pptx": "pptx_slides", ".ppt": "pptx_slides",
    ".pdf": "pdf_pages",
}
_ERROR_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")


class _Down(Exception):
    """道具のサーバーが使えなくなった（起動できない・応答が無い・途中で終わった）。以降の行は確かめきれない。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Server:
    """道具のサーバーの子プロセス 1 本。1 行 1 件の JSON を送り、応答を待ち時間つきで受ける。"""

    def __init__(self, env: dict, cwd: str):
        try:
            self._p = subprocess.Popen(_SERVER_CMD, env=env, cwd=cwd,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       text=True, start_new_session=True)
        except OSError:
            raise _Down("子プロセスを起こせない")
        self._q: queue.Queue = queue.Queue()
        self._id = 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        try:
            for line in self._p.stdout:
                self._q.put(line)
        finally:
            self._q.put(None)

    def rpc(self, method: str, params: dict, timeout_s: float) -> dict:
        """要求を 1 件送り、同じ id の応答を返す。待ち時間を過ぎる・相手が終わる・読めない応答は `_Down`。"""
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        end = time.monotonic() + timeout_s
        while True:
            try:
                line = self._q.get(timeout=max(0.0, end - time.monotonic()))
            except queue.Empty:
                raise _Down("応答が無い（時間切れ）")
            if line is None:
                raise _Down("子プロセスが途中で終わった")
            try:
                resp = json.loads(line)
            except ValueError:
                raise _Down("応答を読めない")
            if isinstance(resp, dict) and resp.get("id") == self._id:
                return resp

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _send(self, msg: dict) -> None:
        try:
            self._p.stdin.write(json.dumps(msg) + "\n")
            self._p.stdin.flush()
        except (OSError, ValueError):
            raise _Down("子プロセスへ送れない")

    def close(self) -> None:
        """子プロセスを必ず終わらせる（標準入力を閉じ、プロセスグループごと止める）。"""
        try:
            self._p.stdin.close()
        except OSError:
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                self._p.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._p.pid, sig)
                except OSError:
                    pass
        try:
            self._p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class _Call:
    """道具 1 回の結果。`ok` は応答の形だけで決める。`body` は次の呼び出しの材料にだけ使い、どこにも出さない。"""

    def __init__(self, ok: bool, kind: str | None, body):
        self.ok, self.kind, self.body = ok, kind, body

    @property
    def not_found(self) -> bool:
        from .graph_tools import START_NOT_FOUND_ERROR
        return not self.ok and self.kind == START_NOT_FOUND_ERROR


def _kind_of(body) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    code = body.get("error_code") if isinstance(body, dict) else None
    for v in (code, err):
        if isinstance(v, str) and _ERROR_KIND_RE.match(v):
            return v
    return "other"


class _Session:
    """1 回の確認で共有する、道具のサーバーと、道具の一覧・資料の一覧などの材料。期限（全体の上限）を持つ。"""

    def __init__(self, server: _Server, deadline: float):
        self.server, self.deadline = server, deadline
        self.tools: set[str] = set()
        self.docs: list[dict] = []

    def call(self, name: str, args: dict) -> _Call:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _Down("全体の上限時間で打ち切り")
        resp = self.server.rpc("tools/call", {"name": name, "arguments": args}, min(CALL_TIMEOUT_S, remaining))
        if "error" in resp:
            code = resp["error"].get("code") if isinstance(resp["error"], dict) else None
            return _Call(False, f"rpc_{code}" if isinstance(code, int) else "rpc_error", None)
        result = resp.get("result")
        if not isinstance(result, dict):
            return _Call(False, "bad_shape", None)
        body = None
        try:
            body = json.loads(result["content"][0]["text"])
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        failed = bool(result.get("isError")) or (isinstance(body, dict) and bool(body.get("error")))
        return _Call(not failed, _kind_of(body) if failed else None, body)


def _fail(calls: list[tuple[str, _Call]]) -> None:
    """失敗した道具があれば、名前とエラーの種類だけを理由にして `_Ng` を投げる。"""
    bad = [f"{n}（{c.kind}）" for n, c in calls if not c.ok]
    if bad:
        raise _Ng("道具がエラーを返した: " + "・".join(bad))


def _row_list(s: _Session, paid: bool) -> dict:
    resp = s.server.rpc("tools/list", {}, min(CALL_TIMEOUT_S, max(0.1, s.deadline - time.monotonic())))
    tools = (resp.get("result") or {}).get("tools") if isinstance(resp.get("result"), dict) else None
    if "error" in resp or not isinstance(tools, list):
        raise _Ng("道具の一覧を読めない")
    s.tools = {t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)}
    missing = [n for n in BASE_TOOLS if n not in s.tools]
    if missing:
        raise _Ng("土台の道具が一覧に無い: " + "・".join(missing))
    return {"note": f"{len(s.tools)}本"}


def _row_docs(s: _Session, paid: bool) -> dict:
    calls = [("list_docs", s.call("list_docs", {"limit": 50})),
             ("folder_tree", s.call("folder_tree", {"depth": 1})),
             ("ripgrep_search", s.call("ripgrep_search", {"query": "確認"}))]
    listed = calls[0][1]
    if listed.ok and isinstance(listed.body, dict) and isinstance(listed.body.get("docs"), list):
        s.docs = [d for d in listed.body["docs"] if isinstance(d, dict) and isinstance(d.get("rel_path"), str)]
    _fail(calls)
    return {"note": "list_docs・folder_tree・ripgrep_search"}


def _pick_doc(s: _Session, wanted) -> dict | None:
    """一覧から、読める状態（ready）を優先して `wanted(拡張子)` に合う資料を 1 件選ぶ。"""
    ranked = sorted(s.docs, key=lambda d: d.get("state") != "ready")
    return next((d for d in ranked if wanted(Path(d["rel_path"]).suffix.lower())), None)


def _row_read(s: _Session, paid: bool) -> dict:
    if not s.docs:
        raise _Skip("資料が無いため流していない")
    text_doc = _pick_doc(s, lambda ext: ext not in _OFFICE_BY_SUFFIX)
    doc = text_doc or s.docs[0]
    did = doc["rel_path"]
    calls = [("doc_outline", s.call("doc_outline", {"doc_id": did})),
             ("read_doc", s.call("read_doc", {"doc_id": did, "start_line": 1}))]
    if text_doc is not None:
        calls.append(("file_head", s.call("file_head", {"doc_id": did, "max_bytes": 200})))
    _fail(calls)
    return {"note": "・".join(n for n, _c in calls)}


def _row_es(s: _Session, paid: bool) -> dict:
    if "es_search" not in s.tools:
        raise _Skip("全文検索の索引が無いため流していない")
    _fail([("es_search", s.call("es_search", {"query": "確認", "mode": "keyword"}))])
    return {"note": "es_search"}


def _graph_start(s: _Session) -> str | None:
    """起点にする名前。資料の名前（拡張子なし）を `name_lookup` で引き、台帳に実在した名前だけを使う。ソース・テキストの資料から先に試す。
    同じ呼び出しは道具のサーバーが断るので、サーバーと同じ見方（`tool_dispatch.dedup_args`）で重なる名前を先に除いてから数える。
    """
    from . import tool_dispatch
    stems: dict[str, str] = {}
    for d in sorted(s.docs, key=lambda d: Path(d["rel_path"]).suffix.lower() in _OFFICE_BY_SUFFIX):
        stem = Path(d["rel_path"]).stem
        key = json.dumps(tool_dispatch.dedup_args("name_lookup", {"name": stem}), sort_keys=True, ensure_ascii=False)
        stems.setdefault(key, stem)
    for stem in list(stems.values())[:_GRAPH_START_TRIES]:
        got = s.call("name_lookup", {"name": stem})
        _fail([("name_lookup", got)])
        matches = got.body.get("matches") if got.ok and isinstance(got.body, dict) else None
        if isinstance(matches, list) and matches and isinstance(matches[0], dict) and isinstance(matches[0].get("name"), str):
            return matches[0]["name"]
    return None


def _row_graph(s: _Session, paid: bool) -> dict:
    wanted = [n for n in ("graph_neighbors", "graph_resolve", "graph_impact") if n in s.tools]
    if not wanted:
        raise _Skip("つながりの検索の道具が一覧に無いため流していない")
    name = _graph_start(s)
    if name is None:
        raise _Skip("起点にする名前が無いため流していない")
    calls: list[tuple[str, _Call]] = []
    canonical = None
    if "graph_neighbors" in s.tools:
        calls.append(("graph_neighbors", s.call("graph_neighbors", {"name": name})))
    if "graph_resolve" in s.tools:
        got = s.call("graph_resolve", {"name": name})
        calls.append(("graph_resolve", got))
        cands = got.body.get("candidates") if got.ok and isinstance(got.body, dict) else None
        if isinstance(cands, list) and cands and isinstance(cands[0], dict):
            canonical = cands[0].get("canonical_id")
    if "graph_impact" in s.tools and isinstance(canonical, str):
        calls.append(("graph_impact", s.call("graph_impact", {"canonical_id": canonical, "limit": 5})))
    _fail([(n, c) for n, c in calls if not c.not_found])  # 起点が見つからない応答は、道具が動いて答えた＝失敗にしない
    note = "・".join(n for n, _c in calls)
    not_run = [n for n in wanted if n not in {c for c, _ in calls}]
    return {"note": note + (f"（{'・'.join(not_run)} は起点の候補が無く流していない）" if not_run else "")}


def _row_office(s: _Session, paid: bool) -> dict:
    by_tool: dict[str, str] = {}
    for d in s.docs:
        tool = _OFFICE_BY_SUFFIX.get(Path(d["rel_path"]).suffix.lower())
        if tool and tool in s.tools:
            by_tool.setdefault(tool, d["rel_path"])
    if not by_tool:
        raise _Skip("Office・PDF の資料が無いため流していない")
    calls = [(tool, s.call(tool, {"doc_id": did})) for tool, did in by_tool.items()]
    _fail(calls)
    return {"note": "・".join(n for n, _c in calls)}


_ROW_FNS = {"tools_list": _row_list, "tools_docs": _row_docs, "tools_read": _row_read,
            "tools_es": _row_es, "tools_graph": _row_graph, "tools_office": _row_office}


def _server_env() -> dict:
    """本物のターンが道具のサーバーを起こすのと同じ env（`_mcp_env`）。台帳・質問を出さず（`plan`・質問の無効化）、
    呼び出しの記録・読取の記録・台帳の置き場は渡さない。
    """
    from .providers.codex.mcp import _mcp_env
    env = _mcp_env(connectivity._mcp_world(), None, True)
    env["SHERPA_MCP_TOOLSET"] = "plan"
    env.setdefault("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    env.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    return env


def rows_incomplete(reason: str, ids=None) -> list[dict]:
    """道具のサーバーへの直接の確認の行（`ids` 省略は全部）を「確かめきれなかった」にした配列。"""
    out = []
    for cid, label in ROWS:
        if ids is None or cid in ids:
            r = connectivity._result(cid, label, False)
            r["reason"] = reason
            out.append(r)
    return out


def _run_row(cid: str, label: str, s: _Session) -> dict:
    """行 1 つを流して結果の行にする。道具のサーバーが使えなくなったら `_Down` をそのまま出す。"""
    r = connectivity._result(cid, label, False)
    t0 = time.monotonic()
    try:
        got = _ROW_FNS[cid](s, False)
        r["status"] = "ok"
        if got.get("note"):
            r["note"] = got["note"]
    except _Skip as e:
        r.update(status="skipped", reason=e.reason)
    except _Ng as e:
        r.update(status="ng", reason=e.reason)
    except _Incomplete as e:
        r.update(status="incomplete", reason=str(e))
    except _Down:
        raise
    except Exception as e:
        r.update(status="ng", reason=classify(e))
    r["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return r


def run_server_checks(*, total_timeout_s: float = TOTAL_TIMEOUT_S) -> list[dict]:
    """道具のサーバーを起こして行ごとに確認する。起こせない・途中で使えなくなった・上限時間を過ぎたときは、残りの行を「確かめきれなかった」にする。
    ① 子プロセスを起こして `initialize` ② 行を上から順に流す ③ 子プロセスを必ず終わらせる。
    """
    deadline = time.monotonic() + total_timeout_s
    out: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="sherpa-conn-tools-") as tmp:
        server = None
        try:
            server = _Server(_server_env(), tmp)
            resp = server.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                             "clientInfo": {"name": "connectivity", "version": "0"}},
                              _START_TIMEOUT_S)
            if "error" in resp:
                raise _Down("初期化に失敗した")
            server.notify("notifications/initialized")
            s = _Session(server, deadline)
            for cid, label in ROWS:
                out.append(_run_row(cid, label, s))
        except _Down as e:
            done = {r["id"] for r in out}
            out += rows_incomplete(f"確かめきれなかった（{e.reason}）", {c for c, _l in ROWS} - done)
        except Exception:
            done = {r["id"] for r in out}
            out += rows_incomplete("確かめきれなかった（道具のサーバーを起こせない）", {c for c, _l in ROWS} - done)
        finally:
            if server is not None:
                server.close()
    return out
